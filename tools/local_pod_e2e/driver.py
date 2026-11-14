#!/usr/bin/env python3
"""The supervisor-side driver of the local pod end-to-end run (docs/pod-backend-local-e2e.md).

It runs INSIDE the main container of the manager stand-in, as the supervisor identity (uid 10001, groups 10001 and 10010, started
through ``/usr/local/bin/factory-entrypoint`` like the manager's own children), and goes through the REAL code path of a pod
session: ``podexecution.activate`` and ``podexecution.agent_kwargs`` (so ``backend_for`` with ``FACTORY_POD_BACKEND=on``, the launcher
socket, the spool and the pool), then ``agent.run_agent`` on a throwaway git worktree, with the job's state in the real ``state.State``.
The agent is the stub ``/usr/local/bin/stub-claude`` of the test image (``FACTORY_AGENT_CMD``).

Subcommands (every one prints exactly one line ``E2E_RESULT <json>`` at the end, and never a token, a key or an environment):

    session NAME [--stub "mode=ok ..."] [--phase build-0] [--timeout 120] [--detach]
    sweep NAME [--gone-after SECONDS]            one pass of the orphan sweep (podsweep) over NAME's state, as the manager would
    recover NAME                                 State.recover over NAME's state through the launcher's reader (recovery's own call)
    inspect                                      the pool's snapshot, the launcher's pod list, the spool's sessions
    breaker-clear                                clear the stop-the-world breaker marker the isolation failure writes
"""

import argparse
import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from factory import agent, gitsafe, launcher_client, podbackend, podcontract, podexecution, podhandle, podliveness, podpool, podsweep, stopworld
from factory.state import State

BASE = Path("/data/e2e")
REPOSITORY = "e2e/pod"
STUB_AGENT = "/usr/local/bin/stub-claude"
JOB = "job-1"


def emit(document):
    sys.stdout.write("E2E_RESULT " + json.dumps(document, sort_keys=True, default=str) + "\n")
    sys.stdout.flush()


def environment():
    """The supervisor's environment: what the manager gives its children, plus the stub agent."""
    env = dict(os.environ)
    env["FACTORY_AGENT_CMD"] = STUB_AGENT
    instance = BASE / "manager-instance"
    BASE.mkdir(parents=True, exist_ok=True)
    if not instance.exists():
        instance.write_text(os.urandom(6).hex())
    env[podcontract.MANAGER_INSTANCE_ENV] = instance.read_text().strip()
    return env


def git(cwd, *args, check=True):
    done = subprocess.run(
        ["git", "-c", "user.name=e2e", "-c", "user.email=e2e@example.invalid", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if check and done.returncode:
        raise RuntimeError(f"git {args[0]} failed: {done.stderr.strip()[:200]}")
    return done.stdout.strip()


def make_tree(name):
    """A throwaway repository and ONE linked worktree for the session, under the data directory (the pod's path layout)."""
    root = BASE / name
    if root.exists():
        subprocess.run(["rm", "-rf", str(root)], check=True)
    repo = root / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("e2e\n")
    (repo / "src").mkdir()
    (repo / "src" / "app.txt").write_text("v1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    worktree = root / "wt"
    git(repo, "worktree", "add", "-q", "-b", "job", str(worktree))
    gitsafe.adopt(str(repo), str(worktree))  # what the runner does for a worktree it has just created
    run_dir = root / "run"
    run_dir.mkdir()
    return root, repo, worktree, run_dir, base


def spool_sessions(spool):
    try:
        return sorted(entry.name for entry in os.scandir(spool) if len(entry.name) == 32)
    except OSError:
        return None


def relay_view(spool, session):
    relay = podliveness.read_relay(spool, session)
    return {
        "read": relay.read,
        "state": relay.state,
        "released": relay.released,
        "revoked": relay.revoked,
        "reject": getattr(relay, "reject", None),
        "preflight_code": getattr(relay, "preflight_code", None),
    }


def pods_view(client):
    try:
        raw = client.list_pods()
    except Exception as error:  # noqa: BLE001
        return {"error": type(error).__name__}
    return {"count": len(raw), "names": sorted(item.get("name", "?") for item in raw if isinstance(item, dict))}


def pool_view(pool):
    snapshot = pool.snapshot() if pool is not None else None
    if snapshot is None:
        return None
    return {key: snapshot[key] for key in ("ceiling", "in_use", "free", "pacing_tokens")} | {"holders": len(snapshot["holders"])}


def read_probe(worktree):
    path = worktree / "probe-result.json"
    if path.is_file():
        try:
            return json.loads(path.read_text())
        except ValueError:
            return {"unparsable": True}
    return None


def tree_files(worktree):
    out = {}
    for path in sorted(worktree.rglob("*")):
        relative = path.relative_to(worktree)
        if relative.parts and relative.parts[0] == ".git":
            continue
        if path.is_symlink():
            out[str(relative)] = "symlink"
        elif path.is_file():
            out[str(relative)] = path.stat().st_size
    return out


def cmd_session(args):
    env = environment()
    os.environ.update(env)
    root, repo, worktree, run_dir, base = make_tree(args.name)
    run_id = f"run-{args.name}"[:50]
    (root / "driver.pid").write_text(str(os.getpid()))
    deactivate = podexecution.activate(args.repo, run_id, env)
    state = State(run_dir, {"jobs": [{"id": JOB}]})
    state.update(JOB, status="running")
    spool = env.get(podcontract.ENV_MANAGER["spool_dir"]) or podcontract.SPOOL_DIR
    client = launcher_client.LauncherClient.from_environment(env)
    before = spool_sessions(spool) or []
    pool = podbackend.current_pool()
    kwargs = podexecution.agent_kwargs("pod", env, job_id=JOB, attempt=0, base=base, spent_usd=0, budget_usd=None)
    out = {
        "scenario": args.name,
        "backend": type(kwargs.get("backend")).__name__ if kwargs else None,
        "stub": args.stub,
        "phase": args.phase,
        "timeout": args.timeout,
        "pool_before": pool_view(pool),
    }
    started = time.monotonic()
    handle_seen = {}

    def on_start(handle):
        handle_seen["pod"] = type(handle).__name__
        if isinstance(handle, podhandle.PodHandle):
            handle_seen["session"] = handle.session
        state.record_started(JOB, handle)
        # A line the harness waits for before it kills the pod or the supervisor.
        sys.stdout.write(f"E2E_STARTED {handle_seen.get('session', '?')}\n")
        sys.stdout.flush()

    prompt = f"STUB {args.stub}\nDo the task.\n"
    try:
        result, usage = agent.run_agent(
            prompt,
            worktree,
            run_dir / "logs" / f"{args.phase}",
            args.phase,
            timeout=args.timeout,
            on_start=on_start,
            **kwargs,
        )
        out["outcome"] = "success"
        out["result"] = result
        out["usage"] = {key: usage.get(key) for key in ("phase", "model", "total_cost_usd", "num_turns", "subtype", "footprint")}
    except BaseException as error:  # noqa: BLE001 - the harness records every way a session can end
        out["outcome"] = "error"
        out["error_type"] = type(error).__name__
        out["error_text"] = str(error)[:300]
        usage_of = getattr(error, "usage", None)
        out["error_has_usage"] = bool(usage_of)
        out["api_error"] = getattr(error, "api_error", None)
    finally:
        out["elapsed_seconds"] = round(time.monotonic() - started, 1)
        with contextlib.suppress(Exception):
            state.end_session(JOB)
        job = state.data["jobs"][JOB]
        out["state_pod_recorded_after"] = job.get("pod") is not None
        out["state_pid_recorded_after"] = job.get("pid") is not None
    after = spool_sessions(spool) or []
    new = sorted(set(after) - set(before))
    out["spool_new_sessions"] = len(new)
    session = handle_seen.get("session") or (new[0] if new else None)
    out["session"] = session
    if session:
        out["relay"] = relay_view(spool, session)
        out["session_dir_entries"] = sorted(os.listdir(os.path.join(spool, session)))
    out["pods_after"] = pods_view(client)
    out["pool_after"] = pool_view(pool)
    out["files"] = tree_files(worktree)
    out["file_hello"] = (worktree / "hello.txt").read_text() if (worktree / "hello.txt").is_file() else None
    out["probe"] = read_probe(worktree)
    out["outside_file_written"] = (root / "outside-the-worktree.txt").exists()
    out["git_hook_applied"] = any(path.exists() for path in ((repo / ".git" / "hooks" / "pre-commit"), (repo / ".git" / "worktrees" / "wt" / "hooks" / "pre-commit"), (worktree / ".git" / "hooks" / "pre-commit")))
    log = run_dir / "logs" / f"{args.phase}.jsonl"
    out["transcript_bytes"] = log.stat().st_size if log.exists() else None
    out["transcript_has_result"] = bool(log.exists() and agent.last_result(log))
    out["stopworld"] = bool(stopworld.read_state(os.environ.get("FACTORY_DATA", "/data")))
    deactivate()
    emit(out)
    return 0


def _recorded_and_live(name):
    root = BASE / name
    state_file = root / "run" / "state.json"
    pidfile = root / "driver.pid"

    def recorded():
        return podsweep.read_recorded([(REPOSITORY, f"run-{name}"[:50], state_file)], True)

    def supervisor_live(_repo, _run):
        try:
            os.kill(int(pidfile.read_text()), 0)
        except (OSError, ValueError):
            return False
        return True

    return recorded, supervisor_live


def cmd_sweep(args):
    env = environment()
    os.environ.update(env)
    sweeper = podexecution.sweeper(env)
    if sweeper is None:
        emit({"error": "no sweeper"})
        return 1
    # The real Sweeper, with the gone-after shortened for the harness (the real one is podcontract.ORPHAN_SILENCE_SECONDS).
    sweeper = podsweep.Sweeper(sweeper._list, sweeper._delete, sweeper._spool, gone_after=args.gone_after)
    recorded, live = _recorded_and_live(args.name)
    passes = []
    for _ in range(args.passes):
        report = sweeper.run_once(recorded, live)
        passes.append(
            {
                "line": report.line(),
                "deleted": dict(report.deleted),
                "released": report.released,
                "skipped": report.skipped,
                "failed": report.failed,
                "terminating": report.terminating,
                "unreadable": report.unreadable,
                "incomplete": report.incomplete,
            }
        )
        if args.passes > 1:
            time.sleep(args.pause)
    client = launcher_client.LauncherClient.from_environment(env)
    emit({"sweep": passes, "census": sweeper.census(), "pods_after": pods_view(client)})
    return 0


def cmd_recover(args):
    env = environment()
    os.environ.update(env)
    root = BASE / args.name
    deactivate = podexecution.activate(REPOSITORY, f"run-{args.name}"[:50], env)
    out = {}
    try:
        state = State(root / "run", {"jobs": [{"id": JOB}]})
        out["job_before"] = state.data["jobs"][JOB].get("status")
        out["pod_recorded_before"] = state.data["jobs"][JOB].get("pod") is not None
        try:
            out["recovered"] = state.recover()
            out["recover_error"] = None
        except Exception as error:  # noqa: BLE001
            out["recovered"] = None
            out["recover_error"] = f"{type(error).__name__}: {str(error)[:160]}"
        out["job_after"] = state.data["jobs"][JOB].get("status")
        if state.data["jobs"][JOB].get("pod") is not None:
            out["pod_status"] = podhandle.pod_status(state.data["jobs"][JOB]["pod"], None)
    finally:
        deactivate()
    emit(out)
    return 0


def cmd_inspect(_args):
    env = environment()
    os.environ.update(env)
    spool = env.get(podcontract.ENV_MANAGER["spool_dir"]) or podcontract.SPOOL_DIR
    client = launcher_client.LauncherClient.from_environment(env)
    pool = podpool.from_env(env, live_count=podpool.launcher_live_count(client))
    sessions = spool_sessions(spool) or []
    emit(
        {
            "pool": pool_view(pool),
            "pods": pods_view(client),
            "info": client.info(),
            "spool_sessions": len(sessions),
            "relay": {session: relay_view(spool, session) for session in sessions[:20]},
            "stopworld": bool(stopworld.read_state(os.environ.get("FACTORY_DATA", "/data"))),
        }
    )
    return 0


def cmd_pool_refuse(_args):
    """The pool's refusals against the real ledger on the real volume (the ceiling is the stand-in's FACTORY_MAX_POD_SESSIONS)."""
    env = environment()
    os.environ.update(env)
    client = launcher_client.LauncherClient.from_environment(env)
    pool = podpool.from_env(env, live_count=podpool.launcher_live_count(client))
    time.sleep(pool.interval * 2)  # let the pacing bucket fill (burst 2) after earlier sessions

    def ask(job, **extra):
        request = podpool.Request(repo="e2e/pool", run="run-pool", job=job, phase="build", attempt=0, estimate_usd=1.0, timeout_seconds=120, **extra)
        return pool.admit(request)

    held, codes = [], {}
    for job in ("a", "b"):
        admission = ask(job)
        codes[f"admit {job}"] = "granted" if admission.reservation is not None else admission.refusal.code
        if admission.reservation is not None:
            held.append(admission.reservation)
    third = ask("c")
    codes["third over the ceiling"] = "granted" if third.reservation is not None else third.refusal.code
    if third.reservation is not None:
        held.append(third.reservation)
    again = ask("b")
    codes["same session again"] = "granted" if again.reservation is not None else again.refusal.code
    over_budget = podpool.Request(repo="e2e/pool", run="run-pool2", job="d", phase="build", attempt=0, estimate_usd=8.0, timeout_seconds=120, spent_usd=1.0, budget_usd=5.0)
    budget = pool.admit(over_budget)
    codes["estimate over the run budget"] = "granted" if budget.reservation is not None else budget.refusal.code
    during = pool_view(pool)
    for reservation in held:
        reservation.cancel()
    after = pool_view(pool)
    freed = ask("e")
    codes["after release (a token was given back)"] = "granted" if freed.reservation is not None else freed.refusal.code
    if freed.reservation is not None:
        freed.reservation.cancel()
    final = pool_view(pool)
    expected = {"admit a": "granted", "admit b": "granted", "third over the ceiling": "ceiling", "same session again": "duplicate", "estimate over the run budget": "budget"}
    ok = all(codes.get(key) == value for key, value in expected.items()) and during["in_use"] == 2 and after["in_use"] == 0 and final["in_use"] == 0
    emit({"ok": ok, "refusals": codes, "during": during, "after_release": after, "final": final})
    return 0


def cmd_breaker_clear(_args):
    stopworld.Breaker(os.environ.get("FACTORY_DATA", "/data")).clear()
    emit({"cleared": True})
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    session = sub.add_parser("session")
    session.add_argument("name")
    session.add_argument("--stub", default="mode=ok")
    session.add_argument("--phase", default="build-0")
    session.add_argument("--timeout", type=int, default=120)
    session.add_argument("--repo", default=REPOSITORY)
    session.set_defaults(func=cmd_session)
    sweep = sub.add_parser("sweep")
    sweep.add_argument("name")
    sweep.add_argument("--gone-after", type=float, default=5.0)
    sweep.add_argument("--passes", type=int, default=1)
    sweep.add_argument("--pause", type=float, default=6.0)
    sweep.set_defaults(func=cmd_sweep)
    recover = sub.add_parser("recover")
    recover.add_argument("name")
    recover.set_defaults(func=cmd_recover)
    sub.add_parser("inspect").set_defaults(func=cmd_inspect)
    sub.add_parser("breaker-clear").set_defaults(func=cmd_breaker_clear)
    sub.add_parser("pool-refuse").set_defaults(func=cmd_pool_refuse)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

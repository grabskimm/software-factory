"""Run with python3 -m factory; all paths and subprocess arguments stay literal."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import capacity, epic, gitsafe, history, stopworld
from .github import create_label, default_branch, inspect_pr, lookup_failure
from .issue_comments import for_run as commenter_for
from .issue_labels import PUBLISHED_STATUSES, IssueLabels, read_run
from .issue_numbers import issues_argument
from .model import validate_plan
from .observed_issues import for_run as observed_for
from .phases import DEFAULT_PROBE_URL, AgentOptions, positive_budget
from .planner import command, plan, planner_usage_records, run_screened, snapshot
from .publish_block import planned_lines, publish_hint
from .runner import Runner
from .shell import set_auth
from .state import State, atomic_json
from .status import planner_record

PHASE_NAMES = ("plan", "build", "review", "verify", "preflight")
# With --repo these neither create the managed clone nor run git in it: they may be
# the last commands that touch a repository before its clone is removed.
NO_GIT_COMMANDS = {"recover", "purge-clone", "reconcile"}
# Commands that must not create the run directory or its marker file.
NO_RUN_DIR_WRITES = {"status", "drain", "stop", "purge", "recover", "purge-clone", "reconcile"}
DEFAULT_PLAN_LIMIT = 30
NOTHING_TO_RECOVER = "Nothing to recover"
NOTHING_TO_REMOVE = "Nothing to remove"
NOTHING_TO_RECONCILE = "Nothing to reconcile"
LEASE_BUSY = "Repository lease is busy; nothing reconciled"


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def planner_usage(directory):
    """The planner's usage record of the run, if it has any (best effort, read-only).

    ``plan`` writes ``planner-usage.json`` and every ``plan --append-to`` a file of its own: with
    more than one record the result is the first with ``total_cost_usd`` and the token counts of
    its ``usage`` summed over all of them (``status.planner_record``).
    """
    return planner_record(planner_usage_records(directory))


def budget(value):
    try:
        return positive_budget(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def add_budget(parser, *phases):
    for phase in phases:
        parser.add_argument(
            f"--budget-{phase}",
            type=budget,
            metavar="USD",
            help=f"per-invocation dollar cap for the {phase} phase "
            "(overrides the repo config; default: built-in)",
        )


def add_verify_domains(parser):
    parser.add_argument(
        "--verify-domain",
        action="append",
        default=[],
        dest="verify_domains",
        metavar="HOST",
        help="host the verify-phase sandbox may reach (repeatable; adds to the repo config)",
    )


def agent_options(args, cfg):
    """Repo-config options with the command-line overrides layered on top."""
    from .config import hidden_paths

    base = cfg.agent_options()
    domains = tuple(
        dict.fromkeys([*base.verify_domains, *(getattr(args, "verify_domains", ()) or ())])
    )
    budgets = dict(base.budgets)
    for phase in PHASE_NAMES:
        value = getattr(args, f"budget_{phase}", None)
        if value is not None:
            budgets[phase] = value
    probe = getattr(args, "probe_url", None)
    return AgentOptions(
        verify_domains=domains,
        probe_url=probe or base.probe_url or DEFAULT_PROBE_URL,
        models=dict(base.models),
        efforts=dict(base.efforts),
        budgets=budgets,
        addendum=base.addendum,
        repo_skills=base.repo_skills,
        repo_skills_budget_kib=base.repo_skills_budget_kib,
        default_model=base.default_model,
        default_effort=base.default_effort,
        aliases=dict(base.aliases),
        allowed_models=base.allowed_models,
        max_effort=base.max_effort,
        # Managed mode with a separate worker identity: keep the workers of this
        # repository out of every other repository's data (see config.hidden_paths).
        hidden_paths=(
            (lambda: hidden_paths(cfg))
            if cfg.repo and cfg.worker_gid is not None
            else None
        ),
    )


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        metavar="ORG/NAME",
        help="target repository: its config is ${FACTORY_CONFIG}/repos/<org>__<name>.toml "
        "and its managed clone lives under ${FACTORY_DATA}. Without it the checkout "
        "containing the current directory is used (local mode).",
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=None,
        help="run directory; its basename is the run name (default in local mode: "
        "factory/runs/pilot). Required with --repo, except for purge-clone, which takes none.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    planning = commands.add_parser("plan")
    planning.add_argument(
        "--limit",
        type=positive,
        default=None,
        help="plan at most this many open issues, newest first (default 30)",
    )
    planning.add_argument(
        "--issues",
        type=issues_argument,
        default=None,
        metavar="N,N,...",
        help="plan exactly these issues (at most 100), in this order, instead of every open issue; "
        "a closed, missing, ignored or pull request number is dropped with one line on stderr. "
        "Cannot be combined with --limit",
    )
    planning.add_argument(
        "--append-to",
        default=None,
        metavar="RUN",
        help="plan the --issues for this existing, started run instead of planning a new one: the "
        "planner sees the run's jobs and the result is written as a plan fragment into the run's "
        "inbox/ for the supervisor to ingest. Takes no repository lease; refused when the run is "
        "missing, not started, draining or full, or over its budget. Needs --issues; "
        "cannot be combined with --limit or --use-snapshot",
    )
    planning.add_argument("--use-snapshot", action="store_true")
    planning.add_argument("--timeout", type=positive, default=1800)
    add_budget(planning, "plan")
    running = commands.add_parser("run")
    running.add_argument("--plan-file", type=Path)
    running.add_argument("--concurrency", type=positive, default=2)
    running.add_argument("--verification-concurrency", type=positive, default=1)
    running.add_argument("--review-concurrency", type=positive, default=2)
    running.add_argument(
        "--max-active-agents",
        type=positive,
        help="cap combined build/review/verification workers "
        "(default: resources.max_active_agents from the repo config)",
    )
    running.add_argument("--timeout", type=positive, default=1800)
    running.add_argument(
        "--review-rounds",
        type=int,
        choices=range(4),
        default=1,
        help="fix rounds after initial independent review (0-3)",
    )
    add_budget(running, "build", "review", "verify")
    add_verify_domains(running)
    status = commands.add_parser("status")
    status.add_argument("--json", action="store_true", dest="as_json")
    status.add_argument(
        "--offline",
        action="store_true",
        help="do not ask GitHub about published PRs (merged/closed/CI state)",
    )
    commands.add_parser(
        "drain",
        help="finish active phases and release the supervisor without starting more work",
    )
    stopping = commands.add_parser(
        "stop",
        help="stop one running job: its current step may finish within a grace period "
        "(FACTORY_STOP_GRACE_SECONDS, default 120), then its worker is terminated; the job becomes "
        "'stopped' (retry or resume continues it) and its pull request and branch are left alone",
    )
    stopping.add_argument("job")
    stopping.add_argument(
        "--reason",
        help="why, as data for the run record (at most 200 characters; never logged or put in a label)",
    )
    stopping.add_argument(
        "--principal",
        help="who asks, recorded with the request (the manager passes the authenticated operator; default 'cli')",
    )
    stopping.add_argument("--json", action="store_true", dest="as_json")
    resuming = commands.add_parser("resume")
    resuming.add_argument("job")
    resuming.add_argument(
        "--phase", choices=("review", "verify", "publish"), required=True
    )
    resuming.add_argument(
        "--reason", help="changed premise for repeating the same phase/head"
    )
    importing = commands.add_parser("import-evidence")
    importing.add_argument("job")
    importing.add_argument("--file", type=Path, required=True)
    blocker = commands.add_parser("record-blocker")
    blocker.add_argument("--file", type=Path, required=True)
    capabilities = commands.add_parser("record-capabilities")
    capabilities.add_argument("--file", type=Path, required=True)
    preflight = commands.add_parser("preflight")
    preflight.add_argument("job")
    preflight.add_argument("--phase", choices=("build", "verify"), required=True)
    preflight.add_argument("--timeout", type=positive, default=120)
    preflight.add_argument(
        "--probe-url",
        default=None,
        help="https URL the capability probe downloads "
        "(default: preflight.probe_url from the repo config; must be on the verify allowlist)",
    )
    add_budget(preflight, "preflight")
    add_verify_domains(preflight)
    retry = commands.add_parser("retry")
    retry.add_argument("job")
    retry.add_argument(
        "--reason", help="what changed since the previous failed attempt"
    )
    retry.add_argument(
        "--followup",
        action="store_true",
        help="a follow-up session on a published pull request (the manager's @mention follow-up): only a "
        "pr_ready or pr_incomplete job whose open pull request still has the retained head; the context the "
        "manager stored for it is read by the builder as untrusted data",
    )
    releasing = commands.add_parser(
        "followup-release",
        help="put a job whose follow-up session never began back as the published job it was "
        "(the manager gives a start that never took hold up with this); changes nothing otherwise",
    )
    releasing.add_argument("job")
    blocking = commands.add_parser(
        "block",
        help="mark a queued job blocked, with a reason, because it can never start "
        "(the manager does this when a dependency's pull request was closed)",
    )
    blocking.add_argument("job")
    blocking.add_argument("--reason", required=True)
    purge = commands.add_parser(
        "purge",
        help="remove a finished run: worktrees, local factory/<run>/* branches and the run directory",
    )
    purge.add_argument(
        "--unchanged-since",
        type=int,
        metavar="NS",
        default=None,
        help="refuse unless the run's state, plan and supervisor-control files are no newer than "
        "this many nanoseconds since the epoch (the manager's janitor passes what it saw)",
    )
    commands.add_parser(
        "recover",
        help="mark the running jobs of a dead supervisor interrupted, so retry or resume can "
        "start from them; refuses while a child may be alive",
    )
    commands.add_parser(
        "reconcile",
        help="idle bookkeeping of a run nobody supervises: follow the published pull requests "
        "(status label of their issues once they landed or were dropped) and complete an "
        "incomplete one that a person marked factory-verified; refuses while a supervisor holds the lease",
    )
    commands.add_parser(
        "purge-clone",
        help="with --repo and no --run-dir: remove the managed clone once no run directory "
        "or linked worktree is left",
    )
    return parser


def local_config():
    """Local mode: no per-repo config file; every key keeps its default."""
    from .config import RepoConfig

    return RepoConfig()


def ensure_clone(cfg, paths):
    """The managed clone of the target repository, created on first use."""
    clone = Path(paths.clone)
    if (clone / ".git").exists():
        return clone
    clone.parent.mkdir(parents=True, exist_ok=True)
    command(["git", "clone", f"https://github.com/{cfg.repo}.git", str(clone)], clone.parent, timeout=900)
    return clone


def data_root_of(paths):
    """``$FACTORY_DATA``: the repository's storage root is ``repos/<org>/<name>`` below it."""
    return Path(paths.root).parents[2]


def holds_lease_for(lock):
    """Whether this process holds a real cross-host lease (a local flock is not one)."""
    from .locks import BlobLeaseLock

    return lambda: isinstance(lock, BlobLeaseLock)


def prepare_worker_access(cfg, paths, repo, directory):
    """Open exactly the paths a separate worker identity needs, in the managed data tree.

    Everything Factory creates is private (umask 077). The worker must pass
    through the data directories to its worktree, and read the clone's git
    directory (objects and worktree metadata) that the worktree points into.
    """
    from . import permissions

    gid = cfg.worker_gid
    data_root = data_root_of(paths)
    permissions.traversable_chain(Path(paths.clone), gid, data_root)
    permissions.traversable_chain(directory, gid, data_root)
    # New objects and worktree metadata stay group-readable too.
    command(["git", "config", "core.sharedRepository", "group"], repo)
    permissions.share_git_dir(Path(repo) / ".git", gid)


def raw_path(path):
    """Absolute but unresolved: purge must see a symlinked run directory as one."""
    return Path(os.path.abspath(path))


def pr_snapshots(directory, data, budget_seconds=30.0):
    """Live ``gh pr view`` results for published jobs, and the lookups that failed.

    Returns ``(snapshots, failures)``. Each failure is ``{"job", "error", "reason"}``
    (``github.lookup_failure``): the exception class and a reason code, never the message.
    Merged and closed jobs need no lookup. The whole refresh is bounded: an unreachable
    GitHub or a failing token broker stops it after the first timeout or authentication
    failure instead of waiting once per job, and every job it did not reach (here, or
    because the time budget ran out) is recorded as failed too.
    """
    from .github_auth import GitHubAuthError

    pending = [
        (key, item["pr"])
        for key, item in data.get("jobs", {}).items()
        if item.get("pr") and item.get("status") not in {"merged", "closed"}
    ]
    snapshots = {}
    failures = []
    deadline = time.monotonic() + budget_seconds
    for index, (key, url) in enumerate(pending):
        if time.monotonic() >= deadline:
            error, reason, unreached = None, "time_budget_exhausted", pending[index:]
        else:
            try:
                snapshots[key] = inspect_pr(directory, url)
                continue
            except (subprocess.TimeoutExpired, GitHubAuthError) as caught:
                failure = lookup_failure(key, caught)
                failures.append(failure)
                error, reason = caught, "not_reached_after_" + failure["reason"]
                unreached = pending[index + 1 :]
            except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as caught:
                failures.append(lookup_failure(key, caught))
                continue
        failures.extend(lookup_failure(other, error, reason) for other, _ in unreached)
        break
    return snapshots, failures


def main():
    parser = build_parser()
    args = parser.parse_args()
    try:
        run(args)
    except (
        ValueError,
        KeyError,
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as error:
        print(f"factory: {error}", file=sys.stderr)
        raise SystemExit(1)


def check_purge_clone_args(args):
    """purge-clone acts on a repository's storage, not on a run: --repo yes, --run-dir no."""
    if not args.repo:
        raise ValueError("purge-clone needs --repo: local mode has no managed clone")
    if args.run_dir is not None:
        raise ValueError("purge-clone takes no --run-dir")


def resolve_run_dir(args, cfg):
    """(resolved, unresolved) run directory; purge must see a symlinked one as such."""
    from .config import RUN_SLUG, check_run_slug, data_paths

    run_dir = args.run_dir
    appending = getattr(args, "append_to", None)
    if appending is not None:
        # The run is named by --append-to; --run-dir, when given, must be that run.
        check_run_slug(appending)
        if run_dir is None:
            run_dir = (
                data_paths(cfg).run_dir(appending)
                if cfg.repo
                else Path("factory/runs") / appending
            )
        elif run_dir.resolve().name != appending:
            raise ValueError("--append-to must name the run of --run-dir")
    if run_dir is None:
        if cfg.repo:
            raise ValueError("--run-dir is required with --repo")
        run_dir = Path("factory/runs/pilot")
    directory = run_dir.resolve()
    if not RUN_SLUG.fullmatch(directory.name):
        raise ValueError("run directory basename must be a short alphanumeric slug")
    return directory, raw_path(run_dir)


def git_common_dir(repo):
    """The absolute common git directory of ``repo``.

    A git failure is worded as ``purge._git_in`` words it (``git <args> failed: <detail>``, ``git <arg>
    could not run: <ExceptionName>``) so ``operations.refusal_cause`` reads it as ``git_failed``; the
    detail is one line of at most 300 characters, never the argv or the raw exception text.
    """
    try:
        common = Path(command(["git", "rev-parse", "--git-common-dir"], repo))
    except subprocess.CalledProcessError as error:
        detail = " ".join(str(error.stderr or "").split())[:300]
        raise RuntimeError(f"git rev-parse --git-common-dir failed: {detail or error.returncode}") from None
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"git rev-parse could not run: {type(error).__name__}") from None
    return common if common.is_absolute() else (repo / common).resolve()


def append_with_pods(args, cfg, paths, directory):
    """``append_command`` with the pod backend wired in, as every other command that runs an agent has it.

    An append runs a planner session, and a repository with ``execution = "pod"`` runs that session in a pod: ``backend_for``
    asks the wired-in scope, so without ``podexecution.activate`` the append is refused with "the pod backend is not configured
    for this session" and the label trigger's top-up retries it every minute (seen on production, 2026-11-10, right after
    ``execution = "pod"`` was set). The append returns before the common path of ``run`` that activates it, so it does it here.
    With the manager's switch off this does nothing, as everywhere.
    """
    from . import podexecution

    undo = podexecution.activate(cfg.repo, None if directory is None else directory.name)
    try:
        return append_command(args, cfg, paths, directory)
    finally:
        undo()


def run(args):
    from . import locks, podexecution
    from .agent import kill_live_agents
    from .config import data_paths, load_repo_config
    from .github_auth import from_environment
    from .locks import LockError, lock_from_env

    if args.command == "purge-clone":
        check_purge_clone_args(args)
    cfg = (
        load_repo_config(
            args.repo, log=lambda message: print(f"factory: {message}", file=sys.stderr)
        )
        if args.repo
        else local_config()
    )
    if cfg.repo:
        set_auth(from_environment(cfg.repo, cfg.installation_id))
    paths = data_paths(cfg) if cfg.repo else None
    directory = unresolved = None  # purge-clone has no run
    if args.command != "purge-clone":
        directory, unresolved = resolve_run_dir(args, cfg)
    if cfg.worker_gid is not None:
        # Files the supervisor writes stay private to it; see permissions.py.
        from . import permissions

        permissions.private_umask()
    if args.command == "plan" and args.append_to is not None:
        # No repository lease and no run directory writes before the refusals: see append.py.
        return append_with_pods(args, cfg, paths, directory)
    if args.command not in NO_RUN_DIR_WRITES:
        directory.mkdir(parents=True, exist_ok=True)
        marker = directory / ".factory-run"
        if not marker.exists():
            marker.write_text("factory run directory\n")
    # Answered before the lease is taken, so that asking about a repository or run
    # that was never used creates nothing (not even the lock file).
    if args.command == "recover" and not (directory / "state.json").is_file():
        print(NOTHING_TO_RECOVER)
        return
    if args.command == "reconcile" and not (directory / "state.json").is_file():
        print(NOTHING_TO_RECONCILE)  # no job ever started: nothing was published
        return
    if args.command == "purge-clone" and not Path(paths.root).is_dir():
        print(NOTHING_TO_REMOVE)
        return

    if args.command == "status":
        if not (directory / "state.json").exists():
            selected = json.loads((directory / "plan.json").read_text())
            print(
                f"Planned {len(selected['issues'])} issues in {len(selected['jobs'])} jobs"
            )
            # A job planned to change workflow files is known before anything is built.
            for line in planned_lines(selected):
                print(line)
            return
        from .control import supervisor_status
        from .status import format_status, status_report

        selected = validate_plan(json.loads((directory / "plan.json").read_text()))
        data = json.loads((directory / "state.json").read_text())
        snapshots, failures = ({}, []) if args.offline else pr_snapshots(directory, data)
        report = status_report(
            selected,
            data,
            planner_usage=planner_usage(directory),
            pr_snapshots=snapshots,
            refresh_failures=failures,
        )
        # `status` only reads: it holds no repository lock, so it never writes issue labels
        # (the manager's Viewer role reaches this path too). `run` reconciles them under the lock.
        report["supervisor"] = supervisor_status(directory)
        report["capacity"] = capacity.snapshot_from_env()
        report["breaker"] = stopworld.read_from_env()
        print(json.dumps(report, indent=2) if args.as_json else format_status(report))
        return

    if cfg.repo:
        repo = (
            Path(paths.clone)
            if args.command in {"drain", "stop", "purge", *NO_GIT_COMMANDS}
            else ensure_clone(cfg, paths)
        )
        lock_path = Path(paths.root) / "factory.lock"
    else:
        repo = Path(command(["git", "rev-parse", "--show-toplevel"], Path.cwd()))
        lock_path = None
    # Only drain and the local-mode lock file need it; the managed lock lives in the data tree.
    common = None
    # A managed purge needs no git directory of its own: a run whose clone step failed
    # has no clone, and purge_run knows what to do about that.
    no_clone = args.command == "purge" and not Path(repo).exists()
    if not (cfg.repo and (args.command in NO_GIT_COMMANDS or no_clone)):
        common = git_common_dir(repo)
    if args.command == "stop":
        stop_command(args, directory, common)
        return
    if args.command == "drain":
        from .control import request_drain

        owner = request_drain(directory, common)
        print(
            f"Drain requested for supervisor PID {owner['pid']} (invocation {owner['token']}). "
            "Active phases will finish and preserve checkpoints. Wait for supervisor exit before recovery; "
            "this does not mark the work complete."
        )
        return

    # One supervisor per repository: a local flock, or a blob lease with fencing
    # when FACTORY_LOCK_ACCOUNT_URL is set. Held for the whole command.
    lock = lock_from_env(cfg.slug if cfg.repo else "local", lock_path or common / "factory.lock")
    holds_lease = holds_lease_for(lock)
    if cfg.worker_gid is not None and cfg.repo and args.command not in {"purge", *NO_GIT_COMMANDS}:
        prepare_worker_access(cfg, paths, repo, directory)
    # If the lease is lost the lock layer kills whatever worker this command started.
    locks.register_kill_hook(kill_live_agents)
    # With the manager's pod switch on (never by default) the pod backend is wired in: its reader and terminator,
    # its kill hook and the repository and run of every session. With it off this does nothing.
    deactivate_pods = podexecution.activate(cfg.repo, None if directory is None else directory.name)
    try:
        with lock:
            if args.command == "purge-clone":
                purge_clone_command(paths)
            else:
                execute(args, cfg, repo, directory, lock, holds_lease, unresolved)
    except locks.LockBusy:
        if args.command != "reconcile":
            raise
        # A supervisor (maybe of another host) works here: idle bookkeeping simply has nothing to do.
        print(LEASE_BUSY)
    except LockError as error:
        raise RuntimeError(str(error)) from None
    finally:
        locks.unregister_kill_hook(kill_live_agents)
        deactivate_pods()


def execute(args, cfg, repo, directory, lock, holds_lease, unresolved):
    from .config import validate_base_branch

    options = agent_options(args, cfg)
    if cfg.setup_commands and args.command in {"plan", "run"}:
        print(
            "factory: setup_commands is configured but this version does not run it; "
            "prepare dependencies in the worker image or the clone",
            file=sys.stderr,
        )
    if args.command == "purge":
        return purge_command(args, repo, directory, unresolved, holds_lease, cfg, lock)
    if args.command == "recover":
        return recover_command(args, directory, holds_lease, cfg, repo, lock)
    if args.command == "reconcile":
        return reconcile_command(cfg, repo, directory, lock)
    if args.command in {"plan", "run"} and cfg.repo:
        # Closing keywords only work against the default branch (item 0).
        validate_base_branch(
            cfg,
            default_branch(repo, cfg.repo),
            log=lambda message: print(f"factory: {message}", file=sys.stderr),
        )
    if args.command == "plan":
        return plan_command(args, cfg, repo, directory, options, lock)
    selected = load_plan(args, directory)
    if args.command in RECOVERY_COMMANDS:
        return recovery_command(
            args, cfg, repo, directory, selected, lock, holds_lease, options
        )
    if args.command == "retry":
        return retry_command(args, cfg, repo, directory, selected, holds_lease, lock)
    if args.command == "block":
        return block_command(args, cfg, repo, directory, selected, holds_lease, lock)
    if args.command == "followup-release":
        return followup_release_command(args, cfg, repo, directory, selected, holds_lease, lock)
    return run_command(args, cfg, repo, directory, selected, lock, options)


RECOVERY_COMMANDS = {
    "resume",
    "import-evidence",
    "record-blocker",
    "record-capabilities",
    "preflight",
}


def archive_before_purge(cfg):
    """The ``before_remove`` hook of ``purge``: a redacted, compressed copy of the run's logs, best effort.

    Off in local mode (no managed data directory) and with FACTORY_LOGS_ARCHIVE=off. Success prints nothing; a
    failure is one fixed line on stderr, never the error text, and the purge goes on (``logarchive.py``, docs/manager.md "Log retention").
    """
    if cfg is None or not getattr(cfg, "repo", ""):
        return None

    def hook(run_dir):
        from . import logarchive

        try:
            config = logarchive.Config.from_env(os.environ)
            if not config.enabled:
                return
            backend = logarchive.backend_for(config, run_dir.parents[4], os.environ)
            logarchive.archive_run(run_dir, cfg.repo, backend)  # silent on success: purge prints what it always did
        except Exception as error:  # the archive is a courtesy: say so once, without the error text
            print(f"{logarchive.FAILED_MARKER} ({type(error).__name__}); the logs go with the run", file=sys.stderr)

    return hook


def worker_remover_for(cfg):
    """The remover that deletes the worker's own files under a run directory, or None when no worker identity is set."""
    if cfg is None or cfg.worker_uid is None or cfg.worker_gid is None:
        return None
    from .purge import worker_remover

    return worker_remover(cfg.worker_uid, cfg.worker_gid)


def purge_command(args, repo, directory, unresolved, holds_lease, cfg=None, lock=None):
    from .purge import purge_run

    # The issues' labels are settled from the plan and state, which go with the run directory.
    run_files = read_run(directory) if cfg is not None else None
    summary = purge_run(
        repo,
        unresolved,
        holds_lease=holds_lease,
        unchanged_since=args.unchanged_since,
        before_remove=archive_before_purge(cfg),
        as_worker=worker_remover_for(cfg),
    )
    print(
        f"Purged run {directory.name}: {len(summary['worktrees'])} worktrees, "
        f"{len(summary['branches'])} branches, run dir removed={summary['removed_run_dir']}"
    )
    for warning in summary.get("warnings", ()):
        print(f"factory: {warning}", file=sys.stderr)
    if run_files is not None and summary["removed_run_dir"]:
        selected, states, others = run_files
        # The run is gone but its history stays (history.py): what each job last was, from the
        # persisted state (no GitHub call). The manager's janitor adds why it purged.
        recorder = history.recorder_for(cfg, directory, selected)
        if recorder is not None:
            recorder.purged(selected, states)
        # Best effort and never fatal, like every label write. The run's own clone may be
        # gone (a failed clone step), so gh falls back to the runs directory.
        labels = IssueLabels(
            repo if Path(repo).is_dir() else directory.parent,
            cfg.repo or None,
            selected,
            fence=lock.assert_held if lock else None,
        )
        # A lookup that failed is simply absent from the handoffs: that job's label stays as it is.
        handoffs, _ = labels.live_handoffs(states, statuses=PUBLISHED_STATUSES)
        labels.settle_after_purge(states, handoffs, skip=others)


def reconcile_command(cfg, repo, directory, lock):
    """Idle bookkeeping for one run, under the repository lease (nothing here changes the run).

    The state and plan files are only read. Four best-effort GitHub steps, each swallowing its own
    failures like every label write: the status label of the issues of published jobs whose pull
    request was merged or closed (``IssueLabels.settle_idle``), the issues opened for what finished jobs
    observed (``IssueLabels.observe_jobs``), the completion label of
    incomplete pull requests (``completion.complete_verified``), and the ``pending`` ``factory/gate``
    of published jobs that wait for the required CI checks (``ci_gate.resolve_pending``).
    """
    from . import ci_gate, completion, epic_gate, epic_phases, trigger
    from .shell import get_auth

    run_files = read_run(directory)
    if run_files is None:
        print(NOTHING_TO_RECONCILE)
        return
    selected, states, others = run_files
    # The clone may be gone (a failed clone step): gh then runs from the run directory.
    cwd = repo if Path(repo).is_dir() else directory
    fence = lock.assert_held if lock else None
    labels = IssueLabels(
        cwd, cfg.repo or None, selected, fence=fence, observed=observed_for(cwd, cfg, directory, fence)
    )
    handoffs, _ = labels.live_handoffs(states, statuses=PUBLISHED_STATUSES)
    labels.settle_idle(states, handoffs, skip=others)
    # What a finished job observed and no issue carries yet (a flush that GitHub refused is made up for here).
    labels.observe_jobs(states)
    if cfg.repo:
        reader = trigger.GithubReader(cfg.repo, get_auth(), cwd)
        epic_settings = epic_gate.settings_of(cfg)
        epic_active = epic_settings.enabled and trigger.settings_of(cfg).enabled

        def phase_links(job):
            """The parent link of a phase issue's job, from the state of GitHub now (``epic_phases.phase_link``)."""
            return epic_phases.phase_link(job, reader, epic_settings, epic_active)

        completion.complete_verified(
            cfg.repo, cwd, selected, states, reader, trigger.settings_of(cfg), labels, fence=fence, links=phase_links
        )
        ci_gate.resolve_pending(cfg.repo, cwd, selected, states, reader, labels, fence=fence, skip=others)
    print(f"Reconciled run {directory.name}")


def purge_clone_command(paths):
    from .purge import purge_clone

    summary = purge_clone(Path(paths.root), data_root_of(paths))
    print("Removed clone" if summary["removed"] else NOTHING_TO_REMOVE)


def recover_command(args, directory, holds_lease, cfg=None, repo=None, lock=None):
    """Turn a dead supervisor's running jobs into 'interrupted', the state retry and resume start from.

    This is the recovery every mutating command already does first (State.recover,
    with its refusals for a live child and for a foreign pid without the lease),
    run on its own so the manager can do it after a restart before deciding what
    to run. The state is re-checked under the lease: a purge may have won the race.
    """
    if not (directory / "state.json").is_file():
        print(NOTHING_TO_RECOVER)
        return
    selected = load_plan(args, directory)
    state = State(directory, selected)
    history.attach(state, cfg, directory, selected)
    if cfg is not None and repo is not None:
        # A crashed run's jobs become 'interrupted' here: their issues go to factory:blocked
        # even when the run is never resumed (the manager recovers after every restart).
        # recover needs no clone: without one, gh runs from the run directory instead.
        cwd = repo if Path(repo).is_dir() else directory
        fence = lock.assert_held if lock else None
        state.observer = IssueLabels(
            cwd,
            cfg.repo or None,
            selected,
            fence=fence,
            commenter=commenter_for(cwd, cfg, directory, fence),
            observed=observed_for(cwd, cfg, directory, fence),
        ).on_job
    interrupted = state.recover(holds_lease=holds_lease)
    print(f"Recovered {len(interrupted)} interrupted job(s)")


def planner_worktree(cfg, repo, directory, name="planner"):
    """Create (or reset) the read-only planner's worktree on the latest base."""
    base = cfg.base_branch
    command(["git", "fetch", "origin", base], repo)
    tree = directory / "worktrees" / name
    branch = f"factory/{directory.name}/{name}"
    if tree.exists():
        # The planner ran in this tree: its git directory comes from the clone, not from its pointer.
        gitdir = gitsafe.adopt(repo, tree)
        actual = gitsafe.git(["branch", "--show-current"], tree, gitdir)
        dirty = gitsafe.git(["status", "--porcelain"], tree, gitdir)
        if actual != branch or dirty:
            raise ValueError("existing planner worktree has unexpected branch or changes")
        gitsafe.git(["reset", "--hard", f"origin/{base}"], tree, gitdir)
        return tree
    exists = (
        subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=repo,
            check=False,
        ).returncode
        == 0
    )
    if exists:
        command(["git", "worktree", "add", str(tree), branch], repo)
    else:
        command(
            ["git", "worktree", "add", "-b", branch, str(tree), f"origin/{base}"],
            repo,
        )
    gitsafe.adopt(repo, tree)
    return tree


def share_planner_tree(cfg, directory, tree, *also):
    """Let a separate worker identity reach the planner's worktree (a no-op without worker.gid)."""
    from . import permissions

    if cfg.worker_gid is not None:
        for shared in (directory, directory / "worktrees", *also):
            permissions.traversable(shared, cfg.worker_gid)
        permissions.share_worktree(tree, cfg.worker_gid)


def label_authority(cfg, cwd):
    """Who may choose the model and effort of an issue: the people allowed to start a build (``trigger.LabelAuthority``).

    Applying a label needs only triage access, so the planner asks GitHub who applied each choice
    label and holds them to ``auto_build_min_permission``, the trigger's own bar, with the repository's
    own identity (the App token, or the ambient login in local mode).
    """
    from . import trigger
    from .shell import get_auth

    reader = trigger.GithubReader(cfg.repo or None, get_auth(), cwd)
    return trigger.LabelAuthority(reader, trigger.settings_of(cfg).min_permission)


def phase_checker(cfg, cwd):
    """``issues -> {number: stamp or None}``: which captured issues are phase issues of an epic (``epic_phases.plan_stamps``).

    One read of each issue that looks like a phase, with the repository's own identity. Where epic mode or label triggers are
    off (the pass that opens phase issues does not run) it answers nothing and makes no GitHub call.
    """
    from . import epic_phases, trigger
    from .shell import get_auth

    if not (epic.enabled(cfg) and trigger.settings_of(cfg).enabled):
        return lambda issues: {}
    reader = trigger.GithubReader(cfg.repo or None, get_auth(), cwd)
    return lambda issues: epic_phases.plan_stamps(reader, issues)


def ensure_choice_labels(repo, cfg, options, fence=None):
    """Create the ``factory-model:<alias>`` and ``factory-effort:<level>`` labels (best effort, never fatal).

    One per alias in force and one per effort, with fixed colours (``model_choice.label_styles``), so a
    person picks them from the repository's label list instead of typing them. A failure (a token
    that cannot write labels, a timeout) costs one fixed line on stderr and stops the attempt: planning
    goes on, and the labels are tried again at the next plan.
    """
    from .model_choice import label_styles

    try:
        for name, (color, description) in label_styles(options).items():
            if fence is not None:
                fence()
            create_label(repo, cfg.repo or None, name, color, description, timeout=30)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"factory: model label sync failed ({type(error).__name__})", file=sys.stderr)


def append_command(args, cfg, paths, directory):
    """``plan --issues N,... --append-to RUN``: write a plan fragment into the run's inbox."""
    from . import append
    from .config import validate_base_branch

    if args.issues is None:
        raise ValueError("--append-to needs --issues")
    if args.limit is not None or args.use_snapshot:
        raise ValueError("--append-to cannot be combined with --limit or --use-snapshot")
    options = agent_options(args, cfg)
    epic_on = epic.can_plan(cfg)
    if not directory.is_dir():
        append.refuse("run not found")  # before the lock file would create anything
    with append.append_lock(directory):
        # Refusals first: nothing below runs (no clone, no planner, no inbox write) for them.
        plan_now, waiting, states = append.check_run(directory, cfg.run_budget_usd)
        numbers = append.candidates(plan_now, args.issues)
        if cfg.repo:
            repo = ensure_clone(cfg, paths)
        else:
            repo = Path(command(["git", "rev-parse", "--show-toplevel"], Path.cwd()))
        if cfg.repo:
            validate_base_branch(
                cfg,
                default_branch(repo, cfg.repo),
                log=lambda message: print(f"factory: {message}", file=sys.stderr),
            )
            if cfg.worker_gid is not None:
                prepare_worker_access(cfg, paths, repo, directory)
        name = append.new_name()
        work = directory / append.WORKDIR / name
        work.mkdir(parents=True, exist_ok=True)
        tree = planner_worktree(cfg, repo, directory, append.PLANNER_TREE)
        share_planner_tree(cfg, directory, tree, work.parent, work)
        issues, prs = snapshot(tree, work, None, cfg.repo or None, numbers, cfg.auto_build_label)
        authority = label_authority(cfg, tree)
        # The planner is a worker session too: it takes a pod budget slot while it runs.
        with capacity.hold(
            capacity.pool_from_env(),
            capacity.Owner(cfg.repo or "local", directory.name, "plan"),
            capacity.wait_seconds(),
        ):
            result, usage = run_screened(
                tree,
                work,
                issues,
                prs,
                args.timeout,
                options,
                cfg.worker_uid,
                cfg.worker_gid,
                context=append.context(plan_now, waiting, states),
                authority=authority,
                epic_on=epic_on,
                execution=cfg.execution,
            )
        if usage is not None:  # None: every issue was refused for its labels, so no planner ran
            append.write_usage(directory, name, usage)
        stamps = phase_checker(cfg, tree)(issues)
        epic.downgrade(result, only_invalid=epic_on, nested=stamps)
        epic.bind_phases(result, stamps)
        raw = append.validated(append.build_fragment(result, issues), name, plan_now)
        path = append.write_fragment(directory, name, raw)
    ensure_choice_labels(repo, cfg, options)
    document = json.loads(raw)
    print(
        f"Appended {len(document['issues'])} issues ({len(document['jobs'])} jobs, "
        f"{len(document['dispositions'])} dispositions) to the run: {path}"
    )
    # Appended jobs may start building at once: a person hears about a workflow change now.
    for line in planned_lines(document):
        print(line)


def verified_text(directory, issues):
    """``{issue: (title, body)}`` from the start stamp of a run the manager began for a phase issue, or None when the run has none.

    The stamp holds the title and body the manager verified against the ledger (``epic_phases.prepare_start``): the planner reads those and
    nothing from GitHub (``planner.snapshot_issues``). A stamp that is not valid, or that does not name exactly the issue to plan, refuses
    the plan (``ValueError``): a run that carries one is never planned from anything else.
    """
    from . import epic_phases

    stamp = epic_phases.run_stamp(directory)
    if stamp is None:
        return None
    if issues != [stamp["child"]]:
        raise ValueError("the epic start stamp does not name the issue to plan")
    return {stamp["child"]: (stamp["title"], stamp["body"])}


def plan_command(args, cfg, repo, directory, options, lock=None):
    if (directory / "plan.json").exists() or (directory / "state.json").exists():
        raise ValueError("run already planned; use a new run directory")
    if args.issues is not None and args.limit is not None:
        raise ValueError("--issues and --limit cannot be combined")
    verified = verified_text(directory, args.issues)
    tree = planner_worktree(cfg, repo, directory)
    share_planner_tree(cfg, directory, tree)
    # The planner is a worker session too: it takes a pod budget slot while it runs.
    with capacity.hold(
        capacity.pool_from_env(),
        capacity.Owner(cfg.repo or "local", directory.name, "plan"),
        capacity.wait_seconds(),
    ):
        result = plan(
            tree,
            directory,
            args.limit or DEFAULT_PLAN_LIMIT,
            args.timeout,
            args.use_snapshot,
            options=options,
            slug=cfg.repo or None,
            artifact_roots=cfg.artifact_roots,
            user=cfg.worker_uid,
            group=cfg.worker_gid,
            numbers=args.issues,
            authority=label_authority(cfg, tree),
            epic_on=epic.can_plan(cfg),
            trigger_label=cfg.auto_build_label,
            phases=phase_checker(cfg, tree),
            execution=cfg.execution,
            verified=verified,
        )
    recorder = history.recorder_for(cfg, directory, result)
    if recorder is not None:
        recorder.planned(result, planner_usage(directory))
    # Status labels on the planned issues: best-effort, never fatal.
    fence = lock.assert_held if lock else None
    ensure_choice_labels(repo, cfg, options, fence)
    IssueLabels(
        repo,
        cfg.repo or None,
        result,
        fence=fence,
        commenter=commenter_for(repo, cfg, directory, fence),
    ).sync_plan()
    print(
        f"Planned {len(result['issues'])} issues in {len(result['jobs'])} jobs: {directory / 'plan.json'}"
    )
    # Fixed wording and plain job ids only: this output goes to the manager's log.
    for line in planned_lines(result):
        print(line)


def original_plan(live):
    """The plan as imported: ``live`` without its applied fragments' jobs, issues and dispositions."""
    ledger = live.get("fragments") if isinstance(live, dict) else None
    if not ledger:
        return live
    try:
        removed = {job for entry in ledger for job in entry["jobs"]}
        owned = {
            number
            for job in live["jobs"]
            if job["id"] in removed
            for number in job["issues"]
        } | {number for entry in ledger for number in entry.get("dispositions", [])}
        original = {key: value for key, value in live.items() if key != "fragments"}
        original["jobs"] = [job for job in live["jobs"] if job["id"] not in removed]
        original["issues"] = [i for i in live["issues"] if i["number"] not in owned]
        original["dispositions"] = [
            d for d in live["dispositions"] if d["issue"] not in owned
        ]
    except (KeyError, TypeError):
        return live
    return original


def load_plan(args, directory):
    """The validated plan, imported first when asked; its digest pins it for the run."""
    plan_path = directory / "plan.json"
    if args.command == "run" and args.plan_file:
        imported = validate_plan(json.loads(args.plan_file.read_text()))
        if not plan_path.exists():
            atomic_json(plan_path, imported)
        else:
            live = json.loads(plan_path.read_text())
            # The live plan.json is kept when the import is it or the original it grew from.
            if live != imported and original_plan(live) != imported:
                raise ValueError("run already has a different plan; use a new run directory")
    selected = validate_plan(json.loads(plan_path.read_text()))
    # Prevent editing objective, issue ownership, or paths underneath persisted jobs.
    digest = hashlib.sha256(json.dumps(selected, sort_keys=True).encode()).hexdigest()
    digest_path = directory / "plan.sha256"
    # An ingested plan fragment moves the pin: the supervisor writes the new digest to
    # plan.sha256.next before the plan, so a crash in between still matches it.
    pending = directory / "plan.sha256.next"
    announced = pending.read_text() if pending.is_file() else None
    if digest_path.exists() and digest_path.read_text() != digest and announced != digest:
        raise ValueError("plan changed after execution began; create a new run directory")
    digest_path.write_text(digest)
    return selected


def recovery_command(args, cfg, repo, directory, selected, lock, holds_lease, options):
    state = State(directory, selected)
    history.attach(state, cfg, directory, selected)
    state.observer = IssueLabels(
        repo,
        cfg.repo or None,
        selected,
        fence=lock.assert_held,
        commenter=commenter_for(repo, cfg, directory, lock.assert_held),
        observed=observed_for(repo, cfg, directory, lock.assert_held),
    ).on_job
    state.recover(holds_lease=holds_lease)
    if args.command == "resume":
        from .recovery import queue_resume

        queue_resume(
            repo, directory, selected, state, args.job, args.phase, args.reason, cfg=cfg
        )
        print(f"{args.job}: queued {args.phase}; earlier checkpoints retained")
    elif args.command == "import-evidence":
        from .recovery import import_evidence

        import_evidence(repo, directory, selected, state, args.job, args.file, cfg=cfg)
        print(f"{args.job}: evidence imported; publication queued")
    elif args.command == "record-blocker":
        from .blockers import record_blocker

        record_blocker(state, json.loads(args.file.read_text()))
        print("Shared blocker recorded; job gates unchanged")
    elif args.command == "record-capabilities":
        from .blockers import record_capabilities

        record_capabilities(state, json.loads(args.file.read_text()))
        print("Environment capabilities recorded; job gates unchanged")
    else:
        from .preflight import probe_worker

        runner = Runner(repo, directory, selected, options=options, cfg=cfg, lock=lock)
        if args.job not in runner.jobs:
            raise ValueError("unknown job")
        worktree = runner.prepare_worktree(runner.jobs[args.job])
        with capacity.hold(
            capacity.pool_from_env(),
            capacity.Owner(cfg.repo or "local", args.job, "preflight"),
            capacity.wait_seconds(),
        ):
            report = probe_worker(runner, args.job, args.phase, worktree, args.timeout)
        print(json.dumps(report, indent=2))


def stop_command(args, directory, common):
    """Ask the live supervisor of the run to stop one job; takes no repository lease, like ``drain``.

    Only a job that is running can be stopped. The request is recorded in the supervisor's control record and
    nothing else happens here: the supervisor lets the job's current step finish within the grace period, then
    terminates the worker's process group and records the job as ``stopped`` (``Runner.service_stops``).
    Asking again while the request is pending changes nothing and prints the first answer.
    """
    from .control import request_stop, stop_grace_seconds

    path = directory / "state.json"
    if not path.is_file():
        raise ValueError("this run has not started: no job is running")
    jobs = json.loads(path.read_text()).get("jobs")
    item = jobs.get(args.job) if isinstance(jobs, dict) else None
    if not isinstance(item, dict):
        raise ValueError("unknown job")
    status = item.get("status")
    if status != "running":
        word = status if isinstance(status, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,31}", status) else "unknown"
        raise ValueError(f"job is not running (status {word}); only a running job can be stopped")
    request = request_stop(directory, common, args.job, args.principal or "cli", args.reason)
    answer = {
        "job": args.job,
        "stop": "requested" if request["created"] else "already_requested",
        "requested_at": request["requested_at"],
        "requested_by": request["by"],
        "grace_seconds": stop_grace_seconds(),
    }
    if args.as_json:
        print(json.dumps(answer))
        return
    print(
        f"Stop {'requested' if request['created'] else 'was already requested'} for {args.job}. "
        f"Its current step may finish within {answer['grace_seconds']:g} s, then its worker is terminated; "
        "the job becomes 'stopped' and the pull request and branch are left alone. "
        "retry or resume continues it."
    )


def retry_command(args, cfg, repo, directory, selected, holds_lease, lock=None):
    state = State(directory, selected)
    history.attach(state, cfg, directory, selected)
    fence = lock.assert_held if lock else None
    state.observer = IssueLabels(
        repo,
        cfg.repo or None,
        selected,
        fence=fence,
        commenter=commenter_for(repo, cfg, directory, fence),
        observed=observed_for(repo, cfg, directory, fence),
    ).on_job
    state.recover(holds_lease=holds_lease)
    if args.job not in state.data["jobs"]:
        raise ValueError("unknown job")
    item = state.data["jobs"][args.job]
    followup = None
    if args.followup:
        # A follow-up session on a published pull request: only a green or incomplete one, and only while the
        # pull request still has the retained head. It needs no failing check (a CI repair does).
        if item["status"] not in {"pr_ready", "pr_incomplete"}:
            raise ValueError("a follow-up needs a published job (pr_ready or pr_incomplete)")
        from .ci_recovery import validate_followup
        from .followup_context import CONTEXT_FILE
        from .followup_context import read as read_context

        head = validate_followup(repo, selected, state, args.job, cfg.base_branch)
        document = read_context(directory, args.job)  # raises when the manager's context file is missing or not trusted
        asked = document.get("request")
        request = asked.get("comment_id") if isinstance(asked, dict) else None
        if not isinstance(request, int) or isinstance(request, bool):
            raise ValueError("follow-up context names no request")
        followup = {
            "from_status": item["status"],
            "from_phase": item.get("phase"),
            "commit": head,
            "context": CONTEXT_FILE,
            "request": request,
        }
    elif item["status"] not in {
        "failed",
        "blocked",
        "interrupted",
        "stopped",
        "needs_human_publish",
        "pr_incomplete",
        "pr_ready",
    }:
        raise ValueError(
            "only failed, blocked, interrupted, stopped, needs_human_publish, incomplete, or CI-failing "
            "pr_ready jobs may be retried"
        )
    elif item["status"] == "interrupted" and item.get("followup"):
        followup = resumable_followup(directory, args.job, item["followup"])  # the restart resumes the same session
    # Repeating implementation after a completed attempt must have a
    # changed premise. Environment-only gaps should use resume instead.
    if item.get("attempt", 0) > 0 and not (args.reason and args.reason.strip()):
        raise ValueError(
            "retry requires --reason describing what changed; use resume for a saved phase"
        )
    ci_repair = None
    if item["status"] == "pr_ready" and not args.followup:
        from .ci_recovery import diagnose_retry

        ci_repair = diagnose_retry(
            repo, selected, state, args.job, args.reason, cfg.base_branch
        )
    retries = list(item.get("retry_history", []))
    retries.append(
        {
            "commit": item.get("commit"),
            "phase": item.get("phase"),
            "reason": args.reason,
            "previous_error": item.get("error"),
            "ci_repair": ci_repair,
        }
    )
    state.update(
        args.job,
        status="queued",
        error=None,
        resume_phase=None,
        retry_history=retries,
        retry_reason=args.reason,
        ci_repair=ci_repair,
        followup=followup,
        **({"stop": None} if item.get("stop") else {}),  # the record of a stop does not outlive it
        # A rebuild makes a new commit: a parked publish and its refused push no longer apply.
        human_publish=None,
        push_error=None,
        push_rejection=None,
    )
    print(f"{args.job} queued; existing worktree and branch will be reused")


def resumable_followup(directory, job, record):
    """The follow-up record of an interrupted job while its session can still be resumed, else None.

    The builder reads the manager's context file; once the manager gave the session up it removed that file, and a
    retry that carried the record forward would be queued only to fail in the builder ("follow-up context unusable").
    Without a readable context that names the same request the retry is an ordinary one.
    """
    from .followup_context import read as read_context

    try:
        asked = read_context(directory, job).get("request")
    except (ValueError, OSError):
        return None
    named = asked.get("comment_id") if isinstance(asked, dict) else None
    named_int = isinstance(named, int) and not isinstance(named, bool)
    return record if isinstance(record, dict) and named_int and named == record.get("request") else None


def followup_release_command(args, cfg, repo, directory, selected, holds_lease, lock=None):
    """Put a job whose follow-up session never began back as the published job it was (the manager's give-up).

    Under the repository lease like ``retry``. Only a job that ``retry --followup`` queued and that built nothing since
    (queued or waiting for a build, still on the retained head) is touched: its status and phase come back from the
    follow-up record and the record is dropped, so a later mention, ``retry`` or ``run`` meets an ordinary published
    job. Any other job (a session that already built a commit, a job that moved on, a job without a record) stays
    exactly as it is: ``run`` finishes the one that built. The line printed says which.
    """
    state = State(directory, selected)
    fence = lock.assert_held if lock else None
    state.observer = IssueLabels(
        repo,
        cfg.repo or None,
        selected,
        fence=fence,
        commenter=commenter_for(repo, cfg, directory, fence),
        observed=observed_for(repo, cfg, directory, fence),
    ).on_job
    state.recover(holds_lease=holds_lease)
    if args.job not in state.data["jobs"]:
        raise ValueError("unknown job")
    item = state.data["jobs"][args.job]
    record = item.get("followup")
    untouched = (
        isinstance(record, dict)
        and "outcome" not in record
        and record.get("from_status") in {"pr_ready", "pr_incomplete"}
        and item["status"] in {"queued", "waiting_build"}
        and item.get("commit") == record.get("commit")
    )
    if not untouched:
        print(f"{args.job} followup kept")
        return
    state.update(
        args.job,
        status=record["from_status"],
        phase=record.get("from_phase"),
        error=None,
        retry_reason=None,
        pending_phase=None,
        worker_phase=None,
        waiting_for=None,
        waiting_owner=None,
        followup=None,
    )
    print(f"{args.job} followup released")


def block_command(args, cfg, repo, directory, selected, holds_lease, lock=None):
    """Mark a queued job ``blocked`` with a reason; its issue gets ``factory:blocked`` (best effort).

    Under the repository lease like ``retry``. Only a queued job of a started run can be blocked:
    anything else is already moving, finished or waiting for a person. ``retry`` undoes it.
    """
    reason = " ".join(str(args.reason).split())[:500]
    if not reason:
        raise ValueError("block requires a --reason")
    if not (directory / "state.json").is_file():
        raise ValueError("run has not started")
    state = State(directory, selected)
    history.attach(state, cfg, directory, selected)
    fence = lock.assert_held if lock else None
    state.observer = IssueLabels(
        repo,
        cfg.repo or None,
        selected,
        fence=fence,
        commenter=commenter_for(repo, cfg, directory, fence),
        observed=observed_for(repo, cfg, directory, fence),
    ).on_job
    state.recover(holds_lease=holds_lease)
    if args.job not in state.data["jobs"]:
        raise ValueError("unknown job")
    if state.data["jobs"][args.job]["status"] != "queued":
        raise ValueError("only a queued job may be blocked")
    state.update(
        args.job, status="blocked", error=reason, waiting_for=None, waiting_owner=None
    )
    print(f"{args.job} blocked")


def run_command(args, cfg, repo, directory, selected, lock, options):
    runner = Runner(
        repo,
        directory,
        selected,
        args.concurrency,
        args.verification_concurrency,
        args.timeout,
        args.review_rounds,
        review_concurrency=args.review_concurrency,
        max_active_agents=args.max_active_agents,
        options=options,
        cfg=cfg,
        lock=lock,
        capacity=capacity.pool_from_env(),
    )
    runner.run()
    for key, item in runner.state.data["jobs"].items():
        # Status and PR only: job errors can carry agent-authored text (for
        # example a builder's blocker), which must not reach supervisor logs.
        # `factory status` shows the detail to the operator.
        print(f"{key}: {item['status']} {item.get('pr') or ''}".rstrip())
        if item["status"] == "needs_human_publish":
            # Fixed wording from the job id and Factory's own clone, branch and bundle names only.
            print(f"{key}: {publish_hint(key, item.get('human_publish'))}")
    if runner.drained:
        print(
            "Supervisor drained; checkpoints retained. Drain does not mark work complete. Inspect status, use recovery commands, then explicitly run again."
        )
        return
    if any(
        item["status"] not in {"pr_ready", "merged", "closed"}
        for item in runner.state.data["jobs"].values()
    ):
        raise SystemExit(2)


if __name__ == "__main__":
    main()

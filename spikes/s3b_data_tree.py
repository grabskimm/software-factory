#!/usr/bin/env python3
"""Spike S3b: does the data-tree permission scheme work with real second-user ids?

The data-tree permission scheme makes the supervisor (uid 10001, group 10001,
supplementary 10010) and the worker (uid 10002, group 10010 only) share one group, and
opens exactly the paths the worker needs: the data directories are traversable (0710) but
not listable, the clone's git directory is group-readable, the worktree is group-writable
and setgid, and git needs `safe.directory` because the worktree belongs to another uid.
`s3_uid_split.py` covers the uid/capability part on a local directory; this script runs the
REAL Factory code (`factory.cli.prepare_worker_access`, `planner_worktree`, `run_agent`)
against a directory of your choice, so that it can be pointed at the NFS mount.

Run it inside the Factory container on ACA as the `factory` user (see spikes/README.md):

    /opt/factory/bin/python /opt/factory-spikes/s3b_data_tree.py --data-dir /data/spike-s3b
    /opt/factory/bin/python /opt/factory-spikes/s3b_data_tree.py          # local reference

`--data-dir` must be a directory the supervisor can create (it is made group 10010, 0710, like
the data root the image entrypoint prepares). Exit 0 and `"pass": true` = every check held.

The worker is `run_agent(..., user=10002, group=10010)` running THIS file as its "agent" (the
`-p` flag marks the worker role); it needs no Anthropic key and makes no network call. Its
observations are written to a file in the worktree and read back by the supervisor.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

RESULT_NAME = "s3b-worker.json"
PROBES_NAME = "s3b-probes.json"


def worker_role():
    """What the unprivileged worker does. Writes RESULT_NAME in its working directory."""
    here = Path.cwd()
    out = {"uid": os.getuid(), "gid": os.getgid(), "groups": os.getgroups()}

    def git(*args):
        done = subprocess.run(
            ["git", *args], cwd=here, capture_output=True, text=True, check=False
        )
        return done.returncode, (done.stdout + done.stderr).strip()[:300]

    out["git_status"] = git("status", "--porcelain")
    out["git_log"] = git("log", "-1", "--format=%H")
    out["git_cat_file"] = git("cat-file", "-p", "HEAD")
    (here / "worker-wrote.txt").write_text("written by the worker\n")
    (here / "subdir").mkdir(exist_ok=True)
    (here / "subdir" / "nested.txt").write_text("nested\n")
    out["git_status_after_write"] = git("status", "--porcelain")
    probes = {}
    # The worker's environment is an allowlist, so the supervisor hands the paths over in a file.
    for label, path in json.loads((here / PROBES_NAME).read_text()).items():
        try:
            if os.path.isdir(path):
                os.listdir(path)
            else:
                open(path, "rb").read(1)
            probes[label] = "READABLE"
        except OSError as error:
            probes[label] = type(error).__name__
    out["private_probes"] = probes
    (here / RESULT_NAME).write_text(json.dumps(out))


def git(*args, cwd, check=True):
    return subprocess.run(
        ["git", "-c", "user.email=s3b@example.invalid", "-c", "user.name=s3b", *args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
    )


def supervisor_role(args):
    from factory import cli, permissions
    from factory.agent import AgentError, run_agent
    from factory.config import RepoConfig, data_paths

    permissions.private_umask()  # what every Factory command does first
    created = args.data_dir is None
    base = Path(args.data_dir) if args.data_dir else Path(tempfile.mkdtemp(prefix="s3b-"))
    base.mkdir(parents=True, exist_ok=True)
    # The data root belongs to the operator and must already let the worker group traverse it
    # (the image entrypoint does this for /data); a spike directory gets the same treatment.
    os.chown(base, -1, args.worker_gid)
    os.chmod(base, 0o710)

    cfg = RepoConfig(
        "spike/s3b", worker_uid=args.worker_uid, worker_gid=args.worker_gid
    )
    paths = data_paths(cfg, base, env={})
    report = {
        "data_dir": str(base),
        "fstype": fstype(base),
        "supervisor": {"uid": os.getuid(), "gid": os.getgid(), "groups": os.getgroups()},
        "checks": {},
    }
    checks = report["checks"]

    def check(name, ok, detail=""):
        checks[name] = {"ok": bool(ok), "detail": str(detail)[:300]}

    try:
        # A local bare "origin" with one commit on main stands in for GitHub.
        seed = base / "seed"
        origin = base / "origin.git"
        seed.mkdir()
        git("init", "-q", "-b", "main", cwd=seed)
        (seed / "README.md").write_text("spike\n")
        (seed / "run.sh").write_text("#!/bin/sh\necho hi\n")
        (seed / "run.sh").chmod(0o755)
        git("add", "-A", cwd=seed)
        git("commit", "-q", "-m", "seed", cwd=seed)
        git("clone", "-q", "--bare", str(seed), str(origin), cwd=base)

        clone = paths.clone
        clone.parent.mkdir(parents=True, exist_ok=True)
        git("clone", "-q", str(origin), str(clone), cwd=base)
        directory = paths.run_dir("s3b")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "plan.json").write_text("{}\n")  # supervisor-private run state
        cli.prepare_worker_access(cfg, paths, clone, directory)
        for shared in (directory, directory / "worktrees"):
            shared.mkdir(parents=True, exist_ok=True)
            permissions.traversable(shared, args.worker_gid)
        tree = cli.planner_worktree(cfg, clone, directory)
        permissions.share_worktree(tree, args.worker_gid)  # as cli.execute and Runner do
        check("supervisor prepared clone, run directory and worktree", True)

        os.environ["FACTORY_AGENT_CMD"] = str(Path(__file__).resolve())
        probes_file = tree / PROBES_NAME
        probes_file.write_text(
            json.dumps(
                {
                    "clone directory listing": str(clone),
                    "clone working-tree file": str(clone / "README.md"),
                    "run state file": str(directory / "plan.json"),
                    "run directory listing": str(directory),
                }
            )
        )
        permissions.share_file(probes_file, args.worker_gid)
        try:
            run_agent(
                "s3b probe",
                tree,
                directory / "0-plan",
                "plan",
                timeout=120,
                user=args.worker_uid,
                group=args.worker_gid,
            )
        except (AgentError, RuntimeError) as error:
            # Expected: this "agent" returns no structured result; only its observations matter.
            # Kept in the report so that a worker that failed to start is visible.
            report["agent_exit"] = f"{type(error).__name__}: {str(error)[:200]}"

        result_path = tree / RESULT_NAME
        check("worker ran and left its observations", result_path.exists())
        worker = json.loads(result_path.read_text()) if result_path.exists() else {}
        report["worker"] = worker
        check(
            "worker identity is exactly (uid, gid) with no supplementary groups",
            worker.get("uid") == args.worker_uid
            and worker.get("gid") == args.worker_gid
            and worker.get("groups") == [],
            f"{worker.get('uid')}/{worker.get('gid')} {worker.get('groups')}",
        )
        check("worker: git status works in the worktree (safe.directory)", worker.get("git_status", [1])[0] == 0, worker.get("git_status"))
        check("worker: git log works through the shared git directory", worker.get("git_log", [1])[0] == 0, worker.get("git_log"))
        check("worker: git cat-file reads objects", worker.get("git_cat_file", [1])[0] == 0, worker.get("git_cat_file"))
        probes = worker.get("private_probes", {})
        check(
            "worker cannot list or read the supervisor's private paths",
            bool(probes) and all(value == "PermissionError" for value in probes.values()),
            probes,
        )
        check(
            "worker's own files are visible to git (worktree is writable)",
            "worker-wrote.txt" in worker.get("git_status_after_write", [0, ""])[1],
            worker.get("git_status_after_write"),
        )

        # What the worker wrote is readable by the supervisor, and group-owned.
        wrote = tree / "worker-wrote.txt"
        nested = tree / "subdir" / "nested.txt"
        check("supervisor reads the worker's file", wrote.exists() and wrote.read_text() == "written by the worker\n")
        check(
            "new files stay in the shared group",
            wrote.exists() and os.stat(wrote).st_gid == args.worker_gid and nested.exists() and os.stat(nested).st_gid == args.worker_gid,
            f"{os.stat(wrote).st_gid if wrote.exists() else '?'}",
        )
        done = git("status", "--porcelain", cwd=tree, check=False)
        check("supervisor runs git status on the worker's worktree", done.returncode == 0, done.stderr)
        # The supervisor can still fetch and add another worktree after the worker ran
        # (objects the worker could not write must not have changed ownership).
        done = git("fetch", "-q", "origin", "main", cwd=clone, check=False)
        check("supervisor can fetch into the shared clone", done.returncode == 0, done.stderr)
        second = directory / "worktrees" / "second"
        done = git("worktree", "add", "-q", "-b", "factory/s3b/second", str(second), "origin/main", cwd=clone, check=False)
        check("supervisor can add a second worktree", done.returncode == 0, done.stderr)
        # The exec bit survives the shared checkout (S2 checks this on NFS for one uid).
        check("exec bit is kept in the worktree", os.access(tree / "run.sh", os.X_OK))
    finally:
        if created and not args.keep:
            shutil.rmtree(base, ignore_errors=True)
        elif not args.keep:
            for leftover in ("repos", "seed", "origin.git"):
                shutil.rmtree(base / leftover, ignore_errors=True)
    report["pass"] = all(item["ok"] for item in checks.values()) and bool(checks)
    print(json.dumps(report, indent=2))
    return 0 if report["pass"] else 1


def fstype(path):
    best = ("", "unknown")
    real = os.path.realpath(path)
    try:
        for line in open("/proc/self/mountinfo"):
            fields = line.split()
            mount = fields[4]
            if real == mount or real.startswith(mount.rstrip("/") + "/"):
                kind = fields[fields.index("-") + 1]
                if len(mount) >= len(best[0]):
                    best = (mount, kind)
    except OSError as error:
        return f"unknown ({type(error).__name__})"
    return best[1]


def main():
    if "-p" in sys.argv[1:]:
        worker_role()
        return 0
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", help="directory to run in (default: a temporary directory)")
    parser.add_argument("--worker-uid", type=int, default=10002)
    parser.add_argument("--worker-gid", type=int, default=10010)
    parser.add_argument("--keep", action="store_true", help="leave the files behind")
    return supervisor_role(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())

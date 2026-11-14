#!/usr/bin/env python3
"""Spike S2: is an Azure Files NFS mount good enough for Factory's run data?

Run it inside the Factory container on ACA against a directory on the NFS volume
(the app mounts it at /data), as the unprivileged `factory` user:

    python3 s2_nfs.py /data/spike-s2

A fresh directory is created under the given path and removed afterwards. To get
the local-disk reference numbers on the same replica, run it a second time on an
ephemeral directory (one you create with mktemp -d) and compare. Exit 0 = every check
passed. The report is one JSON object on stdout; progress lines go to stderr.

Checks: git worktree add/remove, 100 atomic os.replace
rounds under a concurrent reader, flock mutual exclusion (including release when
the holder is killed), the exec bit and symlinks (also through git), and the time
`git status` takes in a medium repository.
"""

import json
import multiprocessing
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

FILES = 3000  # "medium repo": number of tracked files
REPLACE_ROUNDS = 100
GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "spike",
    "GIT_AUTHOR_EMAIL": "spike@example.invalid",
    "GIT_COMMITTER_NAME": "spike",
    "GIT_COMMITTER_EMAIL": "spike@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def log(message):
    print(message, file=sys.stderr, flush=True)


def git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, env=GIT_ENV, text=True, capture_output=True, check=True, timeout=600
    ).stdout


def mount_info(path):
    best = None
    for line in Path("/proc/mounts").read_text().splitlines():
        fields = line.split()
        point = fields[1].replace("\\040", " ")
        if str(path).startswith(point.rstrip("/") + "/") or str(path) == point:
            if best is None or len(point) > len(best[1]):
                best = (fields[0], point, fields[2], fields[3])
    return None if best is None else dict(zip(("device", "point", "fstype", "options"), best))


def build_repo(root):
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    for index in range(FILES):
        directory = repo / f"d{index % 50}"
        directory.mkdir(exist_ok=True)
        (directory / f"f{index}.txt").write_text(f"file {index}\n" * 5)
    tool = repo / "tool.sh"
    tool.write_text("#!/bin/sh\necho exec-ok\n")
    tool.chmod(0o755)
    (repo / "link-to-tool").symlink_to("tool.sh")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "spike")
    return repo


def check_git(root):
    result = {}
    started = time.monotonic()
    repo = build_repo(root)
    result["build_and_commit_seconds"] = round(time.monotonic() - started, 2)
    tree = root / "tree"
    started = time.monotonic()
    git(repo, "worktree", "add", "-q", "-b", "spike-branch", str(tree))
    result["worktree_add_seconds"] = round(time.monotonic() - started, 2)
    samples = []
    for _ in range(3):
        started = time.monotonic()
        dirty = git(tree, "status", "--porcelain")
        samples.append(time.monotonic() - started)
    result["git_status_seconds"] = round(min(samples), 3)
    result["git_status_clean"] = dirty == ""
    (tree / "d1" / "f1.txt").write_text("changed\n")
    result["git_sees_modification"] = "f1.txt" in git(tree, "status", "--porcelain")
    mode = (tree / "tool.sh").stat().st_mode
    result["exec_bit_kept_by_git"] = bool(mode & stat.S_IXUSR)
    result["exec_runs"] = subprocess.run([str(tree / "tool.sh")], capture_output=True, text=True).stdout.strip() == "exec-ok"
    link = tree / "link-to-tool"
    result["symlink_kept_by_git"] = link.is_symlink() and os.readlink(link) == "tool.sh"
    other = root / "abs-link"
    other.symlink_to(tree / "tool.sh")
    result["absolute_symlink"] = os.readlink(other) == str(tree / "tool.sh")
    git(repo, "worktree", "remove", "--force", str(tree))
    git(repo, "worktree", "prune")
    result["worktree_removed"] = not tree.exists()
    result["pass"] = all(
        result[key]
        for key in (
            "git_status_clean",
            "git_sees_modification",
            "exec_bit_kept_by_git",
            "exec_runs",
            "symlink_kept_by_git",
            "absolute_symlink",
            "worktree_removed",
        )
    )
    return result


def _writer(path, rounds):
    for index in range(rounds):
        tmp = f"{path}.tmp{os.getpid()}"
        with open(tmp, "w") as handle:
            handle.write(json.dumps({"n": index, "pad": "x" * 4096}))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)


def _reader(path, stop, queue):
    reads = bad = 0
    while not stop.is_set():
        try:
            with open(path) as handle:
                json.loads(handle.read())
            reads += 1
        except FileNotFoundError:
            bad += 1
        except ValueError:
            bad += 1
    queue.put((reads, bad))


def check_replace(root):
    path = str(root / "state.json")
    _writer(path, 1)
    stop = multiprocessing.Event()
    queue = multiprocessing.Queue()
    reader = multiprocessing.Process(target=_reader, args=(path, stop, queue))
    reader.start()
    started = time.monotonic()
    _writer(path, REPLACE_ROUNDS)
    elapsed = time.monotonic() - started
    stop.set()
    reads, bad = queue.get(timeout=60)
    reader.join(timeout=60)
    final = json.loads(Path(path).read_text())["n"]
    return {
        "rounds": REPLACE_ROUNDS,
        "seconds": round(elapsed, 2),
        "reader_reads": reads,
        "reader_torn_or_missing": bad,
        "final_value": final,
        "pass": bad == 0 and final == REPLACE_ROUNDS - 1,
    }


def _hold(path, ready):
    import fcntl

    handle = open(path, "w")
    fcntl.flock(handle, fcntl.LOCK_EX)
    ready.set()
    time.sleep(120)


def check_flock(root):
    import fcntl

    path = str(root / "repo.lock")
    ready = multiprocessing.Event()
    holder = multiprocessing.Process(target=_hold, args=(path, ready))
    holder.start()
    result = {}
    try:
        if not ready.wait(30):
            return {"pass": False, "error": "holder did not take the lock"}
        probe = open(path, "w")
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result["excludes_second_holder"] = False
        except BlockingIOError:
            result["excludes_second_holder"] = True
        os.kill(holder.pid, signal.SIGKILL)
        holder.join(timeout=30)
        deadline = time.monotonic() + 30
        released = False
        while time.monotonic() < deadline:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                released = True
                break
            except BlockingIOError:
                time.sleep(0.2)
        result["released_when_holder_killed"] = released
    finally:
        if holder.is_alive():
            holder.kill()
    result["pass"] = bool(result.get("excludes_second_holder") and result.get("released_when_holder_killed"))
    return result


def check_ownership(root):
    probe = root / "owner-probe"
    probe.write_text("x")
    info = probe.stat()
    return {
        "process_uid": os.getuid(),
        "file_uid": info.st_uid,
        "file_gid": info.st_gid,
        "mode": oct(stat.S_IMODE(info.st_mode)),
        "pass": info.st_uid == os.getuid(),
    }


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    base = Path(sys.argv[1])
    base.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="s2-", dir=base))
    report = {"path": str(base), "mount": mount_info(root.resolve()), "files": FILES}
    try:
        for name, check in (
            ("ownership", check_ownership),
            ("flock", check_flock),
            ("os_replace", check_replace),
            ("git", check_git),
        ):
            log(f"running {name} ...")
            try:
                report[name] = check(root)
            except Exception as error:  # report, do not hide, any failure of a check
                report[name] = {"pass": False, "error": f"{type(error).__name__}: {error}"}
    finally:
        shutil.rmtree(root, ignore_errors=True)
    report["pass"] = all(report[name].get("pass") for name in ("ownership", "flock", "os_replace", "git"))
    print(json.dumps(report, indent=2))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())

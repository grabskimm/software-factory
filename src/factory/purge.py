"""Remove everything a finished or abandoned run left behind: worktrees, local branches, run dir.

Nothing is retained after a run (no archive), and Git never deletes worktrees or
local branches by itself. Purge is the only code that deletes them, so it checks
twice before touching anything: the run must be safely dead, and every path and
ref it is about to delete must provably belong to this run. The caller holds the
repository lock (or lease), which is what stops a supervisor starting meanwhile.

`purge_clone` is the second step: once no run is left for a repository, its
managed clone is only a cache, and it is removed on the same terms.
"""

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import gitsafe, podhandle
from .control import supervisor_status
from .state import pid_status

SLUG = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,49}")
# A directory without one of these is not a Factory run; never rmtree it.
# `.factory-run` is written when a run directory is created, so a run whose `plan`
# failed before producing plan.json can still be purged.
RUN_MARKERS = ("plan.json", "state.json", ".factory-run")
# What counts as a sign of life in a run directory (also read by the manager's janitor).
ACTIVITY_FILES = ("state.json", "plan.json", "supervisor-control.json")
GIT_TIMEOUT = 120
# A clone is renamed to this sibling before it is deleted: it leaves its real name
# in one step, and a deletion that fails halfway can be finished by the next call.
CLONE_LEFTOVER = "clone.removing"


# The wording of the refusals ``operations.CAUSES`` recognises (the manager's janitor maps the first
# ``factory: `` line of a failed purge's stderr to a fixed ``cause=`` code). Constants so that
# tests/test_operations.py builds its messages from the real text; ``.format`` fills the templates.
MSG_UNCHANGED_SINCE = "the run was used after it was judged finished; not purged"
MSG_CHILD_ALIVE = "{key}: child process group {pid} is still alive"
MSG_FOREIGN_PID = (
    "{key}: child pid {pid!s:.20} was recorded on another host (or cannot "
    "be verified); purge needs the repository lease"
)
MSG_SUPERVISOR_LIVE = "supervisor {pid} is still {status}; wait for it to stop"
MSG_CHECKED_OUT = "{name} is checked out in {where}"
MSG_OTHER_REPO = "the run belongs to a different repository than --repo"
MSG_WORKTREE_OTHER_REPO = "worktree {name} belongs to a different repository than --repo"
MSG_STATE_UNREADABLE = "state.json is unreadable, so recorded child processes cannot be checked"
MSG_GIT_FAILED = "git {command} failed: {detail}"
MSG_GIT_COULD_NOT_RUN = "git {command} could not run: {error}"
MSG_REMOVE_FAILED = "could not remove run directory: {reason}"


class PurgeRefused(RuntimeError):
    """The run is not safely dead, or something to delete is not provably the run's."""


def newest_activity_ns(directory):
    """Newest modification time (ns) of the run's state, plan and supervisor-control files.

    A run directory with none of them (a plan that failed early) falls back to its own
    time; None when the directory is gone. Every state-changing command takes the lease
    and rewrites state.json, so a changed value means somebody used the run.
    """
    newest = None
    for name in ACTIVITY_FILES:
        try:
            stamp = os.stat(Path(directory) / name).st_mtime_ns
        except OSError:
            continue
        newest = stamp if newest is None else max(newest, stamp)
    if newest is not None:
        return newest
    try:
        return os.stat(directory).st_mtime_ns
    except OSError:
        return None


# A worker session builds its home (``.home-<job>-<phase>/.claude/...``) inside the run directory, and the tools it
# runs create directories there that only the worker uid may enter (mode 0700). The supervisor, a different uid in the
# same group, cannot delete them (EPERM), which would leave the run directory behind for ever.
EXECNOCAPS = Path(__file__).with_name("execnocaps.py")
_REMOVE_AS_WORKER = "import shutil, sys; shutil.rmtree(sys.argv[1], ignore_errors=True)"
REMOVE_TIMEOUT = 300


def worker_remover(uid, gid):
    """``remove(path)``: delete what the worker identity ``uid:gid`` may delete under ``path``, best effort.

    Runs ``shutil.rmtree`` (which works on directory descriptors and never follows a link) in a child that has
    switched to the worker's uid and gid, with no environment and no inherited group. The supervisor holds
    CAP_SETUID/CAP_SETGID for exactly this (it starts workers the same way). Nothing here raises: whatever the child
    cannot delete stays, and the caller's own deletion decides whether the run directory can go.
    """

    identity = {"user": uid, "group": gid, "extra_groups": []} if uid is not None and gid is not None else {}

    def remove(path):
        try:
            subprocess.run(
                # Through the same wrapper that starts workers: it clears the ambient capabilities the supervisor passes
                # on (CAP_SETUID/SETGID/KILL survive a change between two non-zero uids), and refuses to run if it cannot.
                [sys.executable, os.fspath(EXECNOCAPS), "--", sys.executable, "-I", "-c", _REMOVE_AS_WORKER, os.fspath(path)],
                **identity,
                env={},
                cwd="/",
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=REMOVE_TIMEOUT,
                check=False,
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            pass

    return remove


def _closed_directories(entry):
    """The directories under ``entry`` that this process may not list (a worker's 0700 directories), topmost first.

    The walk never follows a link and never descends into a directory it cannot list: the worker identity is handed that
    directory itself, because it may traverse the run's own directories (group ``--x``) but not list them, so a removal
    started at a top-level entry of the run (mode 0710) could not even see what is inside.
    """
    found = []
    stack = [entry]
    while stack:
        path = stack.pop()
        try:
            with os.scandir(path) as listing:
                children = [child.path for child in listing if child.is_dir(follow_symlinks=False)]
        except PermissionError:
            found.append(path)
            continue
        except FileNotFoundError:
            continue
        stack.extend(children)
    return found


def _remove_entry(entry, as_worker):
    """``shutil.rmtree``; when the supervisor is refused and a worker identity is known, the worker removes what only it may.

    The first refusal means a directory the supervisor may not list (the worker made it 0700). Each such directory is
    handed to ``as_worker`` (which removes it, or empties it when its parent forbids the worker to unlink it), then the
    supervisor removes the rest. A refusal that remains reaches the caller.
    """
    try:
        shutil.rmtree(entry)
        return
    except PermissionError:
        if as_worker is None:
            raise
    for path in _closed_directories(entry):
        as_worker(path)
    try:
        shutil.rmtree(entry)  # what the supervisor owns goes now; a PermissionError here is real and reaches the caller
    except FileNotFoundError:
        pass  # the worker identity could delete the whole entry: nothing is left, which is the goal


def _remove_run(run_dir, as_worker):
    """``_remove_run_directory`` with a failure worded as ``could not remove run directory: <reason>`` (``refusal_cause``)."""
    try:
        _remove_run_directory(run_dir, as_worker)
    except OSError as error:
        raise RuntimeError(MSG_REMOVE_FAILED.format(reason=error.strerror)) from None


def _remove_run_directory(run_dir, as_worker=None):
    """Delete the run directory so that an interrupted deletion can be repeated.

    ``_run_directory`` refuses a directory without a marker, so the markers go last:
    whatever a failed or killed deletion leaves behind still identifies itself as a
    Factory run, and the next call finishes the job. ``as_worker(path)`` (``worker_remover``) deletes what only
    the worker uid may delete.
    """
    markers = set(RUN_MARKERS)
    # By name within each group: the order is the same on every file system, so a repeated
    # purge retraces the same steps.
    for entry in sorted(run_dir.iterdir(), key=lambda item: (item.name in markers, item.name)):
        if entry.is_symlink() or not entry.is_dir():
            entry.unlink()
        else:
            _remove_entry(entry, as_worker)
    run_dir.rmdir()


def _git_in(repo):
    """Default git runner: argv list only, no shell, bounded time, stdout returned.

    ``repo`` is the main checkout: ``gitsafe`` runs it with hooks and fsmonitor off and without
    an inherited ``GIT_DIR`` (the redirecting variables of the caller are dropped there).
    """

    def git(*args):
        try:
            return gitsafe.repo_git(args, repo, timeout=GIT_TIMEOUT)
        except subprocess.CalledProcessError as error:
            detail = " ".join((error.stderr or "").split())[:300]
            raise RuntimeError(
                MSG_GIT_FAILED.format(command=" ".join(args[:2]), detail=detail or error.returncode)
            ) from None
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError(
                MSG_GIT_COULD_NOT_RUN.format(command=args[0], error=type(error).__name__)
            ) from None

    return git


def _run_directory(directory):
    """The resolved run directory, or None when there is none (already purged)."""
    candidate = Path(directory)
    if not SLUG.fullmatch(candidate.name):
        raise PurgeRefused("run directory basename must be a short alphanumeric slug")
    if candidate.is_symlink():
        raise PurgeRefused("run directory is a symlink; refusing to purge through it")
    if not os.path.lexists(candidate):
        return None
    resolved = candidate.resolve(strict=True)
    if not resolved.is_dir() or not SLUG.fullmatch(resolved.name):
        raise PurgeRefused("run directory is not a plain run directory")
    if not any((resolved / marker).is_file() for marker in RUN_MARKERS):
        raise PurgeRefused(
            "run directory has no plan.json or state.json; refusing to delete it"
        )
    return resolved


def _children(run_dir):
    """(job key, pid, pid_host, pod) for every recorded child; an unreadable record refuses."""
    path = run_dir / "state.json"
    if not path.exists():
        return []
    try:
        jobs = json.loads(path.read_text())["jobs"]
        return [
            (key, job.get("pid"), job.get("pid_host"), job.get("pod")) for key, job in jobs.items()
        ]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise PurgeRefused(MSG_STATE_UNREADABLE) from None


def _assert_dead(run_dir, holds_lease, pod_reader=None):
    lease = None  # asked at most once, and only when a foreign pid needs it
    for key, pid, pid_host, pod in _children(run_dir):
        status = pid_status(pid, pid_host)
        if status == "alive":
            raise PurgeRefused(MSG_CHILD_ALIVE.format(key=key, pid=pid))
        if status == "unknown":
            if lease is None:
                lease = callable(holds_lease) and holds_lease() is True
            if not lease:
                raise PurgeRefused(MSG_FOREIGN_PID.format(key=key, pid=pid))
        # A lease says nothing about a pod: only a pod read as dead lets the run directory go.
        if pod is not None:
            pod_state = podhandle.pod_status(pod, pod_reader)
            if pod_state != podhandle.DEAD:
                raise PurgeRefused(str(podhandle.still_live(key, pod, pod_state, "purging")))
    try:
        supervisor = supervisor_status(run_dir)
    except (ValueError, OSError) as error:
        raise PurgeRefused(f"supervisor control record unusable: {error}") from None
    if (
        supervisor
        and supervisor.get("status") in {"running", "draining"}
        and supervisor.get("active")
    ):
        raise PurgeRefused(
            MSG_SUPERVISOR_LIVE.format(pid=supervisor.get("pid"), status=supervisor["status"])
        )


def _registered_worktrees(git):
    """[(path, branch ref or None)] from `git worktree list`; the first is the main checkout."""
    trees = []
    for block in git("worktree", "list", "--porcelain").split("\n\n"):
        fields = dict(
            line.split(" ", 1) if " " in line else (line, "")
            for line in block.splitlines()
        )
        if "worktree" in fields:
            trees.append((Path(fields["worktree"]), fields.get("branch")))
    return trees


def _assert_belongs(run_dir, repo):
    """Refuse a run directory that belongs to a different repository than ``repo``.

    Purging with the wrong ``--repo`` would delete another repository's run
    directory (and uncommitted worktree contents) while leaving that
    repository's worktree registrations and branches dangling.
    """
    common = Path(_git_in(repo)("rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    control = run_dir / "supervisor-control.json"
    try:
        recorded = json.loads(control.read_text())["current"].get("common_dir")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        recorded = None
    if recorded and Path(recorded).resolve() != common:
        raise PurgeRefused(MSG_OTHER_REPO)
    trees = run_dir / "worktrees"
    for entry in trees.iterdir() if trees.is_dir() else ():
        pointer = entry / ".git"
        if pointer.is_symlink() or not pointer.is_file():
            continue
        text = pointer.read_text(errors="replace").strip()
        if text.startswith("gitdir:"):
            target = Path(text.split(":", 1)[1].strip()).resolve()
            if not target.is_relative_to(common / "worktrees"):
                raise PurgeRefused(MSG_WORKTREE_OTHER_REPO.format(name=entry.name))


def _inside(path, root):
    try:
        return Path(path).resolve().is_relative_to(root)
    except (OSError, RuntimeError):
        return False


def _before_removal(hook, run_dir):
    """Run the caller's last look at a run directory that is about to go (the log archive); a failure never stops the purge."""
    if hook is not None:
        with contextlib.suppress(Exception):
            hook(run_dir)


def purge_run(repo, directory, *, git=None, holds_lease=None, unchanged_since=None, before_remove=None, as_worker=None):
    """Delete the run's worktrees, local `factory/<run>/*` branches and run directory.

    `repo` is the main checkout. Refuses (PurgeRefused, a RuntimeError) before
    changing anything if a child or the supervisor may be alive, or a branch to
    delete is checked out anywhere that is not one of this run's own worktrees.
    `holds_lease` is a callable returning True only while the caller holds the
    repository lease; it is what lets a pid recorded on another host count as
    dead. `git(*args)` runs git in `repo` and returns stdout (tests inject it).
    Remote branches and remote-tracking refs are never touched. Safe to repeat:
    the run directory is removed last (its markers last of all), and a missing one
    means nothing is left. ``unchanged_since`` (ns, from ``newest_activity_ns``) makes
    the purge refuse when the run was used after the caller decided to purge it; the
    caller holds the lease, so nothing can use the run between this check and the end.
    A run directory without a clone (a plan whose clone step failed) has no worktrees
    or branches to remove, and is deleted after the liveness check alone.
    ``before_remove(run_dir)``, when given, is called once every check has passed, right before the run
    directory is deleted (the log archive copies the logs there); whatever it raises is swallowed, so it
    can never make a purge fail. ``as_worker(path)`` (``worker_remover``) deletes, as the worker identity, what the
    supervisor may not (the worker's own 0700 directories); without it such a run directory is refused.
    """
    summary = {"worktrees": [], "branches": [], "removed_run_dir": False}
    run_dir = _run_directory(directory)
    if run_dir is None:
        return summary
    checkout = Path(repo).resolve()
    if checkout == run_dir or checkout.is_relative_to(run_dir):
        raise PurgeRefused("the repository checkout is inside the run directory")
    if unchanged_since is not None:
        current = newest_activity_ns(run_dir)
        if current is None or current > unchanged_since:
            raise PurgeRefused(MSG_UNCHANGED_SINCE)
    if not os.path.lexists(checkout) and not (run_dir / "worktrees").exists():
        _assert_dead(run_dir, holds_lease)
        _before_removal(before_remove, run_dir)
        _remove_run(run_dir, as_worker)
        summary["removed_run_dir"] = True
        return summary
    # Whose run is it? Checked first so a wrong --repo is reported as such.
    _assert_belongs(run_dir, checkout)
    _assert_dead(run_dir, holds_lease)

    git = git or _git_in(checkout)
    prefix = f"refs/heads/factory/{run_dir.name}/"
    trees = _registered_worktrees(git)
    mine = run_dir / "worktrees"
    doomed = [path for path, _ in trees[1:] if _inside(path, mine) and path != mine]
    branches = [
        ref
        for ref in git("for-each-ref", "--format=%(refname)", prefix).splitlines()
        if ref.startswith(prefix) and len(ref) > len(prefix)
    ]
    for index, (path, branch) in enumerate(trees):
        if branch in branches and (index == 0 or path not in doomed):
            where = "the main checkout" if index == 0 else str(path)
            name = branch.removeprefix("refs/heads/")
            raise PurgeRefused(MSG_CHECKED_OUT.format(name=name, where=where))

    for path in doomed:
        # A worktree whose directory is already gone is cleared by prune below.
        if path.exists():
            try:
                git("worktree", "remove", "--force", str(path))
            except RuntimeError:
                # A removal killed halfway leaves a directory without its .git pointer,
                # which git refuses to touch ever after. It is inside this run's own
                # worktrees (checked above) and goes with the run; prune clears the entry.
                if (path / ".git").exists():
                    raise
                shutil.rmtree(path)
        summary["worktrees"].append(str(path))
    git("worktree", "prune")
    for ref in branches:
        name = ref.removeprefix("refs/heads/")
        git("branch", "-D", name)
        summary["branches"].append(name)

    _before_removal(before_remove, run_dir)
    _remove_run(run_dir, as_worker)
    summary["removed_run_dir"] = True
    try:
        # Drop registrations of anything that lived inside the directory but outside
        # worktrees/. Everything the run owned is already gone, so a failure here is
        # reported, not fatal.
        git("worktree", "prune")
    except RuntimeError as error:
        summary["warnings"] = [f"worktree prune after removal failed: {error}"]
    return summary


def _storage_root(root, data_root):
    """(data root, repository root): ``repos/<org>/<name>`` below it, no symlink on the way.

    Lexical apart from the symlink walk, so a path that merely looks like it is
    inside the data root cannot pass.
    """
    top = Path(os.path.abspath(data_root))
    storage = Path(os.path.abspath(root))
    try:
        parts = storage.relative_to(top).parts
    except ValueError:
        raise PurgeRefused("the repository storage root is not inside the data root") from None
    if len(parts) != 3 or parts[0] != "repos":
        raise PurgeRefused("the repository storage root is not <data>/repos/<org>/<name>")
    current = top
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise PurgeRefused("the repository storage path goes through a symlink")
    return top, storage


def _plain_directory(path, what):
    """Refuse a symlink (even one to a directory) or a non-directory; never follow either."""
    if path.is_symlink():
        raise PurgeRefused(f"{what} is a symlink; refusing to remove through it")
    if not path.is_dir():
        raise PurgeRefused(f"{what} is not a directory")


def _assert_no_runs(runs):
    """A run directory, of any kind, means the clone is still wanted."""
    if not os.path.lexists(runs):
        return
    _plain_directory(runs, "the runs directory")
    if any(runs.iterdir()):
        raise PurgeRefused("a run directory remains for this repository; purge it first")


def _assert_no_linked_worktrees(clone, git):
    """Refuse while git lists any worktree besides the clone itself (a prunable one too).

    The first entry must be the clone: a broken ``.git`` makes git walk up and
    answer for whatever repository encloses it, which says nothing about this one.
    """
    try:
        trees = _registered_worktrees(git or _git_in(clone))
    except RuntimeError:
        trees = []
    if not trees or trees[0][0].resolve() != clone.resolve():
        raise PurgeRefused("the clone's worktrees could not be listed")
    if len(trees) > 1:
        raise PurgeRefused(f"the clone still has {len(trees) - 1} linked worktree(s)")


def _assert_removable(top, root, git):
    """Every reason not to delete ``<root>/clone``, checked before anything changes."""
    clone = root / "clone"
    _plain_directory(clone, "the clone")
    if clone.resolve(strict=True) != top.resolve(strict=True) / clone.relative_to(top):
        raise PurgeRefused("the clone does not resolve inside the data root")
    dot_git = clone / ".git"
    if dot_git.is_symlink() or not dot_git.is_dir():
        raise PurgeRefused("the clone has no .git directory; refusing to delete it")
    _assert_no_runs(root / "runs")
    _assert_no_linked_worktrees(clone, git)


def _remove_tree(path):
    _plain_directory(path, "the clone being removed")
    try:
        shutil.rmtree(path)
    except OSError as error:
        raise RuntimeError(f"could not remove the clone: {error.strerror}") from None


def purge_clone(root, data_root, *, git=None):
    """Delete the managed clone ``<root>/clone`` once nothing needs it any more.

    `root` is the repository's storage root (``<data>/repos/<org>/<name>``) and
    `data_root` the data directory it must lie in. Refuses (PurgeRefused, a
    RuntimeError) before changing anything if a run directory remains, git still
    lists a linked worktree, the clone is not a plain directory holding a ``.git``
    directory, or any part of its path is a symlink. Symlinks inside the clone
    are removed, never followed. Safe to repeat: no clone means nothing to do,
    and ``ensure_clone`` makes a new one on first use. `git(*args)` runs git in
    the clone and returns stdout (tests inject it). Returns ``{"removed": bool}``.
    """
    top, storage = _storage_root(root, data_root)
    clone, leftover = storage / "clone", storage / CLONE_LEFTOVER
    if os.path.lexists(clone):
        _assert_removable(top, storage, git)
    removed = False
    if os.path.lexists(leftover):  # an earlier attempt failed halfway
        _remove_tree(leftover)
        removed = True
    if os.path.lexists(clone):
        try:
            os.rename(clone, leftover)
        except OSError as error:
            raise RuntimeError(f"could not set the clone aside: {error.strerror}") from None
        _remove_tree(leftover)
        removed = True
    return {"removed": removed}

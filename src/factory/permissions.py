"""File modes for the worker/supervisor user split (``worker.uid`` / ``worker.gid``).

The supervisor and the worker share one group. Worktrees and the per-invocation
home are group-writable (and setgid, so files the worker creates stay in the
group); everything else under the run directory stays private to the
supervisor: the run directory is traversable by the group (``0710``) but not
listable, and the files in it are created ``0600`` (see ``private_umask``), so
the worker can reach the paths it is given and nothing else.
"""

import os
import stat

PRIVATE_UMASK = 0o077
WORKER_UMASK = 0o002


def private_umask():
    """Make files the supervisor creates unreadable to the worker's group."""
    os.umask(PRIVATE_UMASK)


def _ensure(target, gid, bits):
    """Give ``target`` group ``gid`` and at least ``bits``, touching it only when it lacks them.

    A non-root process may change the group or mode of its own files only. After a build the
    worktree also holds entries created by the worker uid (for example ``.claude``): with the
    setgid directories and the worker's umask they already carry the worker group and group
    write, and the supervisor, not being their owner, gets EPERM from chown/chmod. Skipping
    entries that need nothing avoids that; an entry that does need a change and belongs to
    another uid cannot be fixed by this process and is left to its owner. A failure on an
    entry the supervisor owns is still raised.
    """
    info = os.lstat(target)
    mode = stat.S_IMODE(info.st_mode)
    try:
        if info.st_gid != gid:
            os.chown(target, -1, gid)
        if mode & bits != bits:
            os.chmod(target, mode | bits)
    except PermissionError:
        if info.st_uid == os.geteuid():
            raise


def traversable(path, gid):
    """Directory the worker may pass through but not list or read: ``0710``."""
    os.chown(path, -1, gid)
    os.chmod(path, 0o710)


def share_directory(path, gid):
    """Directory the worker may use fully: group read/write, setgid so new files stay in the group."""
    os.chown(path, -1, gid)
    os.chmod(path, 0o2770)


def share_file(path, gid):
    """A file the worker only needs to read (settings, system prompt): ``0640``."""
    os.chown(path, -1, gid)
    os.chmod(path, 0o640)


def share_tree(root, gid):
    """Make a worktree group-writable for the worker, without following symlinks."""
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in [".", *dirs]:
            target = os.path.join(current, name) if name != "." else current
            if os.path.islink(target):
                continue
            _ensure(target, gid, 0o2770)
        for name in files:
            target = os.path.join(current, name)
            if os.path.islink(target):
                continue
            _ensure(target, gid, 0o660)


def traversable_chain(path, gid, stop):
    """``0710`` on ``path`` and every parent below ``stop`` (exclusive).

    The data root itself belongs to the operator and must already let the
    worker group traverse it; everything Factory creates beneath it is private
    to the supervisor (``private_umask``) unless this opens a path through it.
    """
    path, stop = os.path.abspath(path), os.path.abspath(stop)
    while path != stop and path.startswith(stop + os.sep):
        traversable(path, gid)
        path = os.path.dirname(path)


def share_git_dir(git_dir, gid):
    """Let the worker group read a shared git directory (objects, refs, worktree metadata).

    Workers read objects through their linked worktrees but cannot write there
    (the sandbox denies it). Directories get ``g+rxs`` and files ``g+r`` so that
    both existing and, with ``core.sharedRepository=group``, later objects stay
    readable; nothing is made writable.
    """
    for current, dirs, files in os.walk(git_dir, followlinks=False):
        for name in [".", *dirs]:
            target = os.path.join(current, name) if name != "." else current
            if os.path.islink(target):
                continue
            _ensure(target, gid, 0o2750)
        for name in files:
            target = os.path.join(current, name)
            if os.path.islink(target):
                continue
            _ensure(target, gid, 0o640)


def share_worktree(worktree, gid):
    """``share_tree`` plus the worktree's administrative directory in the shared git directory.

    ``git worktree add`` creates ``<git dir>/worktrees/<name>`` (``HEAD``, ``commondir``,
    ``gitdir``, ``index``) private to the supervisor even with ``core.sharedRepository=group``,
    and a linked worktree is unusable ("not a git repository") for another uid without read
    access to it. The worker only reads it: the sandbox keeps the shared git directory
    read-only for every phase.
    """
    share_tree(worktree, gid)
    marker = os.path.join(worktree, ".git")
    if not os.path.isfile(marker) or os.path.islink(marker):
        return
    with open(marker) as handle:
        first = handle.readline().strip()
    prefix = "gitdir:"
    if not first.startswith(prefix):
        return
    admin = os.path.normpath(os.path.join(worktree, first[len(prefix):].strip()))
    if os.path.isdir(admin) and not os.path.islink(admin):
        share_git_dir(admin, gid)


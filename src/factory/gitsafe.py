"""The one way a supervisor runs git on a tree a worker has touched.

A worker owns the files of its worktree, and in a pod it would own a whole ``.git``. Git
reads ``core.fsmonitor``, ``core.hooksPath`` and the hook directory from the repository it
discovers, and every one of those names a program to run, as the supervisor, with the
supervisor's environment (the GitHub credentials). So no supervisor call lets git discover
anything:

* ``GIT_DIR`` is the per-worktree administrative directory of the main clone
  (``<repo>/.git/worktrees/<name>``) and ``GIT_WORK_TREE`` the worktree, both explicit; a
  ``GIT_DIR`` or ``GIT_WORK_TREE`` of the caller's own environment is dropped;
* every call carries ``-c core.fsmonitor=false -c core.hooksPath=/dev/null`` (also the
  read-only ones: ``status`` and ``diff`` run the fsmonitor program, ``status`` runs the
  ``post-index-change`` hook) and ``GIT_CONFIG_NOSYSTEM=1``;
* a nested repository is a second source of programs. When the index holds a gitlink, or the
  worktree holds an untracked repository, ``status``, ``diff`` and ``add`` start a CHILD git
  inside that directory, which reads the nested repository's own configuration and
  ``.gitattributes`` (a ``filter.<name>.clean`` program) and runs it as the supervisor. A worker
  can write such a ``.git`` anywhere in its worktree, and the supervisor's own ``add --all``
  turns it into a gitlink. So every call carries ``-c diff.ignoreSubmodules=dirty`` and
  ``status`` and ``diff`` get ``--ignore-submodules=dirty`` on the command line (a
  ``.gitmodules`` ``ignore`` setting of the worktree overrides the configuration, not the
  option), which starts no child and still shows a moved gitlink. ``add`` has no such option, so
  before any other command that reads the worktree the call refuses (``GitSafetyError``) a
  nested repository whose ``.git`` is not a file that points into the ``modules`` directory of
  the trusted git directory (what ``git submodule update`` writes, in a place a worker cannot
  write). ``-c submodule.recurse=false -c fetch.recurseSubmodules=false`` keep ``fetch`` and
  the checkout commands out of submodules;
* the environment is ``shell.supervisor_env()``, never the process environment;
* before each call the ``.git`` pointer of the worktree must still read
  ``gitdir: <the administrative directory>``, otherwise the call raises
  ``GitSafetyError`` (the job fails) and git is not started.

The administrative directory is learned from the MAIN clone (the ``gitdir`` back-pointer of
``<repo>/.git/worktrees/<name>``, which a worker cannot write), never from the pointer file
of the worktree after a worker has run. ``CalledProcessError`` carries the argv the CALLER
passed, without the injected ``-c`` pairs, so code that classifies a failure by its command
(``Runner.push_or_park`` tests ``["git", "push"]``) keeps working.

A call in the supervisor's own clone (``repo_git``) gets the same ``-c`` pairs and the same
environment, minus the explicit ``GIT_DIR``: it reads the common configuration that every
worktree shares.
"""

import os
import stat
import subprocess
import threading
from pathlib import Path

from .shell import PROCESS_AUTH, supervisor_env

# Prepended to every call. A caller's own ``-c`` pairs follow them (callers are code, not data).
# ``diff.ignoreSubmodules`` and the two recurse settings reach the child git that git starts inside
# a submodule (``-c`` pairs travel in ``GIT_CONFIG_PARAMETERS``).
HARDENING = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "diff.ignoreSubmodules=dirty",
    "-c",
    "fetch.recurseSubmodules=false",
    "-c",
    "submodule.recurse=false",
)

# What a nested repository could make git run is stopped at the command line for these (``dirty``
# keeps a moved gitlink visible and starts no child git; the worktree's ``.gitmodules`` cannot
# override a command-line option).
IGNORE_SUBMODULES = {"status", "diff"}
IGNORE_OPTION = "--ignore-submodules=dirty"

# Subcommands that never read the worktree or refresh the index, so no child git can start from them,
# plus ``status`` and ``diff``, which the command-line option above already keeps out of submodules.
# Anything else, and any call whose subcommand cannot be told, is refused while a nested repository
# the supervisor did not create is present (``check_nested``).
NO_WORKTREE_READ = frozenset(
    {
        "rev-parse",
        "merge-base",
        "branch",
        "fetch",
        "push",
        "show",
        "show-ref",
        "rev-list",
        "log",
        "cat-file",
        "for-each-ref",
        "ls-tree",
        "ls-remote",
        "bundle",
        "symbolic-ref",
        "update-ref",
        "config",
        "remote",
        "worktree",
        "status",
        "diff",
    }
)

# Variables that point git at some other repository, index, object store or config set.
# None of them is ever needed by a supervisor call (the GitHub App credential helper uses
# ``GIT_CONFIG_COUNT`` and friends, which stay).
REDIRECTS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
    "GIT_CONFIG_PARAMETERS",
)

# Fixed texts: a failure never echoes a path or a byte a worker chose.
POINTER_CHANGED = "worktree .git pointer was changed"
NOT_REGISTERED = "worktree is not registered with the repository"
RECORDED_DIFFERS = "worktree git directory differs from the recorded one"
NESTED_REPOSITORY = "worktree holds a nested git repository that is not a submodule of it"

POINTER_LIMIT = 4096  # a gitdir pointer is one short line; read no more than this of a worker's file
NESTING_LIMIT = 8  # submodules inside submodules; deeper is refused


class GitSafetyError(RuntimeError):
    """A tree could not be proven to be the one the supervisor created (the message is fixed)."""


def _environment(auth, tree=None, gitdir=None):
    env = supervisor_env(auth)
    for name in REDIRECTS:
        env.pop(name, None)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    if gitdir is not None:
        env["GIT_DIR"] = os.fspath(gitdir)
        env["GIT_WORK_TREE"] = os.path.abspath(tree)
    return env


def _subcommand(args):
    """Index of the git subcommand in ``args`` (after leading ``-c <pair>`` and ``-C <path>``), or None."""
    skip = False
    for index, word in enumerate(args):
        if skip:
            skip = False
        elif word in ("-c", "-C"):
            skip = True
        elif not word.startswith("-"):
            return index
    return None


def _ignoring_submodules(args):
    """``args`` with ``--ignore-submodules=dirty`` after ``status`` or ``diff`` unless the caller chose one."""
    index = _subcommand(args)
    if index is None or args[index] not in IGNORE_SUBMODULES:
        return list(args)
    options = args[index + 1 :]
    if "--" in options:
        options = options[: options.index("--")]
    if any(word.startswith("--ignore-submodules") for word in options):
        return list(args)
    return [*args[: index + 1], IGNORE_OPTION, *args[index + 1 :]]


def _execute(args, cwd, env, timeout, check, encoding, errors):
    caller = ["git", *args]
    try:
        result = subprocess.run(
            ["git", *HARDENING, *_ignoring_submodules(args)],
            cwd=cwd,
            env=env,
            text=True,
            encoding=encoding,
            errors=errors,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise subprocess.TimeoutExpired(caller, error.timeout, output=error.output, stderr=error.stderr) from None
    result.args = caller
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, caller, output=result.stdout, stderr=result.stderr)
    return result


def _first_line(path):
    """The first line of a small file as text, or None when it cannot be read."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(POINTER_LIMIT)
    except OSError:
        return None
    return os.fsdecode(data.split(b"\n", 1)[0]).strip()


def check_pointer(worktree, gitdir):
    """Raise ``GitSafetyError`` unless ``<worktree>/.git`` still points at ``gitdir``.

    A linked worktree has a ``.git`` FILE reading ``gitdir: <gitdir>``; a plain checkout (the
    main clone, which a worker never works in) has a ``.git`` directory that must be ``gitdir``.
    Anything else, a symlink included, is a changed pointer.
    """
    marker = os.path.join(worktree, ".git")
    expected = os.fspath(gitdir)
    try:
        mode = os.lstat(marker).st_mode
        if stat.S_ISDIR(mode):
            same = os.path.realpath(marker) == expected
        elif stat.S_ISREG(mode):
            with os.fdopen(os.open(marker, os.O_RDONLY | os.O_NOFOLLOW), "rb") as handle:
                content = handle.read(POINTER_LIMIT + 1)
            same = content.rstrip(b"\n") == os.fsencode(f"gitdir: {expected}")
        else:
            same = False
    except OSError:
        same = False
    if not same:
        raise GitSafetyError(POINTER_CHANGED)


def _modules_roots(gitdir):
    """The ``modules`` directories git writes submodule repositories into: not writable by a worker."""
    roots = [os.path.realpath(os.path.join(gitdir, "modules"))]
    common = _first_line(os.path.join(gitdir, "commondir"))
    if common:
        roots.append(os.path.realpath(os.path.join(gitdir, common, "modules")))
    return roots


def _submodule_gitdir(directory, roots):
    """The trusted git directory that ``directory/.git`` points to, or None when it has no ``.git``.

    Raises ``GitSafetyError`` for a ``.git`` that is a directory, a symlink, or a file that does not
    point into one of the ``roots`` (the ``modules`` directories of a trusted git directory).
    """
    marker = os.path.join(directory, ".git")
    try:
        mode = os.lstat(marker).st_mode
    except (FileNotFoundError, NotADirectoryError):
        return None  # nothing for a child git to enter
    except OSError:
        raise GitSafetyError(NESTED_REPOSITORY) from None
    line = ""
    if stat.S_ISREG(mode):
        try:
            with os.fdopen(os.open(marker, os.O_RDONLY | os.O_NOFOLLOW), "rb") as handle:
                line = os.fsdecode(handle.read(POINTER_LIMIT).split(b"\n", 1)[0]).strip()
        except OSError:
            line = ""
    if not line.startswith("gitdir:"):
        raise GitSafetyError(NESTED_REPOSITORY)
    target = Path(os.path.realpath(os.path.join(directory, line[len("gitdir:") :].strip())))
    if not any(target != Path(root) and target.is_relative_to(root) for root in roots):
        raise GitSafetyError(NESTED_REPOSITORY)
    return os.fspath(target)


def check_nested(worktree, gitdir, *, auth=PROCESS_AUTH, timeout=120, depth=0):
    """Raise ``GitSafetyError`` when ``worktree`` holds a nested repository that is not a submodule of it.

    Candidates are the gitlinks of the index and the untracked nested repositories (``ls-files``
    names those with a trailing slash; an ignored one is never staged and never visited). A
    candidate passes when ``<path>/.git`` is absent or a file pointing into the ``modules``
    directory of the trusted git directory, and the path itself is not reached through a symlink.
    A submodule that passes is checked the same way with its own trusted git directory (a
    submodule of a submodule starts a child git of its own).
    """
    if depth > NESTING_LIMIT:
        raise GitSafetyError(NESTED_REPOSITORY)
    env = _environment(auth, worktree, gitdir)
    listing = ("utf-8", "surrogateescape")
    stage = _execute(["ls-files", "--stage", "-z"], worktree, env, timeout, True, *listing)
    others = _execute(["ls-files", "--others", "--exclude-standard", "-z"], worktree, env, timeout, True, *listing)
    candidates = [entry.split("\t", 1)[1] for entry in stage.stdout.split("\0") if entry.startswith("160000 ")]
    candidates += [entry.rstrip("/") for entry in others.stdout.split("\0") if entry.endswith("/")]
    if not candidates:
        return
    root = os.path.realpath(worktree)
    roots = _modules_roots(gitdir)
    for path in candidates:
        directory = os.path.join(root, path)
        if os.path.realpath(directory) != os.path.normpath(directory):
            raise GitSafetyError(NESTED_REPOSITORY)
        inner = _submodule_gitdir(directory, roots)
        if inner is not None:
            check_nested(directory, inner, auth=auth, timeout=timeout, depth=depth + 1)


def run(args, worktree, gitdir, *, auth=PROCESS_AUTH, timeout=120, check=True, encoding=None, errors=None):
    """``git <args>`` in ``worktree`` with ``gitdir`` explicit; a ``CompletedProcess`` (see the module text)."""
    check_pointer(worktree, gitdir)
    env = _environment(auth, worktree, gitdir)
    argv = list(args)
    index = _subcommand(argv)
    if index is None or argv[index] not in NO_WORKTREE_READ:
        check_nested(worktree, gitdir, auth=auth, timeout=timeout)
    return _execute(argv, worktree, env, timeout, check, encoding, errors)


def git(args, worktree, gitdir, *, auth=PROCESS_AUTH, timeout=120):
    """stdout of ``git <args>`` in ``worktree`` without the final newline; raises on a non-zero exit."""
    return run(args, worktree, gitdir, auth=auth, timeout=timeout).stdout.rstrip("\n")


def repo_run(args, repo, *, auth=PROCESS_AUTH, timeout=120, check=True, encoding=None, errors=None):
    """``git <args>`` in the supervisor's OWN clone (not a tree a worker touched)."""
    return _execute(list(args), repo, _environment(auth), timeout, check, encoding, errors)


def repo_git(args, repo, *, auth=PROCESS_AUTH, timeout=120):
    """stdout of ``git <args>`` in the supervisor's own clone without the final newline."""
    return repo_run(args, repo, auth=auth, timeout=timeout).stdout.rstrip("\n")


def resolve_gitdir(repo, worktree, *, auth=PROCESS_AUTH):
    """The administrative directory of ``worktree`` according to the main clone ``repo``.

    Matches the ``gitdir`` back-pointer of every ``<common>/worktrees/<name>`` on the worktree's
    real path. The main checkout itself (a ``.git`` directory that is the common directory) is
    its own git directory. Raises ``GitSafetyError`` for a tree the clone does not know.
    """
    common = os.path.realpath(repo_git(["rev-parse", "--path-format=absolute", "--git-common-dir"], repo, auth=auth))
    tree = os.path.realpath(worktree)
    root = os.path.join(common, "worktrees")
    try:
        names = sorted(os.listdir(root))
    except OSError:
        names = []
    found = []
    for name in names:
        back = _first_line(os.path.join(root, name, "gitdir"))
        if back and os.path.basename(back) == ".git" and os.path.realpath(os.path.dirname(back)) == tree:
            found.append(os.path.realpath(os.path.join(root, name)))
    if len(found) == 1:
        return found[0]
    marker = os.path.join(tree, ".git")
    if not found and os.path.isdir(marker) and not os.path.islink(marker) and os.path.realpath(marker) == common:
        return common
    raise GitSafetyError(NOT_REGISTERED)


# Trees this process has learned a trusted git directory for, by real path. ``agent.py`` reads a
# tree it is only handed a path of (it has no state and no main clone); the supervisor
# registers a worktree here when it creates or first uses it, before any worker has run there.
_TRUSTED = {}
_TRUSTED_LOCK = threading.Lock()


def trust(worktree, gitdir):
    with _TRUSTED_LOCK:
        _TRUSTED[os.path.realpath(worktree)] = os.fspath(gitdir)


def forget(worktree):
    with _TRUSTED_LOCK:
        _TRUSTED.pop(os.path.realpath(worktree), None)


def trusted(worktree):
    """The registered git directory of ``worktree``, or None."""
    with _TRUSTED_LOCK:
        return _TRUSTED.get(os.path.realpath(worktree))


def adopt(repo, worktree, *, auth=PROCESS_AUTH):
    """Resolve ``worktree`` from the main clone ``repo`` afresh and register it (a worktree just created)."""
    gitdir = resolve_gitdir(repo, worktree, auth=auth)
    trust(worktree, gitdir)
    return gitdir


def gitdir_of(repo, worktree, recorded=None, *, auth=PROCESS_AUTH):
    """The trusted git directory of ``worktree``: the registered one, else resolved from ``repo``.

    ``recorded`` (the job state's ``gitdir``) is checked against the resolution, never trusted
    on its own; a difference raises ``GitSafetyError`` and registers nothing.
    """
    known = trusted(worktree)
    if known is not None:
        return known
    gitdir = resolve_gitdir(repo, worktree, auth=auth)
    if recorded is not None and os.path.realpath(recorded) != gitdir:
        raise GitSafetyError(RECORDED_DIFFERS)
    trust(worktree, gitdir)
    return gitdir


def checkout_gitdir(worktree):
    """Git directory for code that has only a path (``agent.py``): None when ``worktree`` holds no git.

    A registered worktree gives its registered directory. A plain checkout (a ``.git`` directory,
    which is the main clone or a test fixture) is its own git directory. A ``.git`` file, a
    linked worktree nobody registered, is refused: its pointer is exactly what a worker can rewrite.
    """
    known = trusted(worktree)
    if known is not None:
        return known
    marker = os.path.join(worktree, ".git")
    try:
        mode = os.lstat(marker).st_mode
    except OSError:
        return None
    if stat.S_ISDIR(mode):
        return os.path.realpath(marker)
    raise GitSafetyError(NOT_REGISTERED)

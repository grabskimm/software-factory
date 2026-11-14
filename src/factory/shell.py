"""Supervisor-side git/gh subprocesses.

GitHub credentials reach git and gh only through the environment of these
supervisor-side children (see ``github_auth``): never argv, ``.git/config``, a
remote URL, ``hosts.yml`` or a run file. Workers never go through here:
``agent.run_agent`` builds its own allowlisted environment.
"""

import contextlib
import os
import subprocess

_auth = None


def set_auth(auth):
    """Install the process-wide GitHub auth (``None`` means the ambient login)."""
    global _auth
    _auth = auth


def get_auth():
    return _auth


@contextlib.contextmanager
def using_auth(auth):
    previous = get_auth()
    set_auth(auth)
    try:
        yield auth
    finally:
        set_auth(previous)


# Secrets the supervisor holds for other purposes: git and gh (and every hook,
# credential helper or submodule transport they spawn) must not inherit them.
WITHHELD = ("GITHUB_APP_PRIVATE_KEY", "ANTHROPIC_API_KEY", "FACTORY_MANAGER_METRICS_TOKEN")
AUTHOR_VARIABLES = (
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_AUTHOR_DATE",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
    "GIT_COMMITTER_DATE",
)


_PROCESS_AUTH = object()  # "use the process-wide auth", the default of ``auth=``
PROCESS_AUTH = _PROCESS_AUTH  # the same sentinel for modules that forward ``auth=`` (gitsafe)


def supervisor_env(auth=_PROCESS_AUTH):
    """Environment for a supervisor-side git/gh child.

    ``auth`` overrides the process-wide identity for one call (the manager watches several
    repositories at once, each with its own installation token; ``None`` is the ambient login).

    Never contains the App private key or the worker's API key. When an App
    identity is active the inherited git author/committer variables are dropped:
    they outrank ``git -c user.name=...`` and would let commits be attributed to
    someone other than the App.
    """
    chosen = _auth if auth is _PROCESS_AUTH else auth
    env = {k: v for k, v in os.environ.items() if k not in WITHHELD}
    if chosen is not None:
        env.update(chosen.env_for_supervisor_subprocess())
        if chosen.commit_identity() is not None:
            for name in AUTHOR_VARIABLES:
                env.pop(name, None)
    return env


def identity_args():
    """``git -c user.name=... -c user.email=...`` for commits, or [] for the ambient identity."""
    return list(_auth.git_identity_args()) if _auth is not None else []


def command(args, cwd, timeout=120, auth=_PROCESS_AUTH):
    result = subprocess.run(
        args,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
        timeout=timeout,
        env=supervisor_env(auth),
    )
    return result.stdout.strip()

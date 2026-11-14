"""GitHub identity for supervisor-side ``git`` and ``gh`` child processes.

Only the supervisor talks to GitHub; workers never receive credentials. This
module decides which identity those supervisor children use:

* ``AmbientAuth``: today's behaviour, the operator's own ``gh``/``git`` login.
* ``AppAuth``: an org-owned GitHub App. Installation tokens come from
  ``tools/token_broker.py`` (the only code that needs ``cryptography``), are
  cached in memory only, and reach children through environment variables, so
  nothing is ever written to ``.git/config``, a remote URL, ``hosts.yml`` or a
  run file. Nothing here may put a token in an exception, repr or log line.
"""

import abc
import json
import os
import re
import shlex
import subprocess
import threading
import time
from datetime import datetime, timezone

KEY_ENV = "GITHUB_APP_PRIVATE_KEY"
APP_ENV = (
    "FACTORY_GITHUB_APP_ID",
    "FACTORY_GITHUB_APP_SLUG",
    "FACTORY_GITHUB_APP_USER_ID",
    "FACTORY_TOKEN_BROKER",
)
# GitHub tokens last an hour; renew while a long git push still has time left.
REFRESH_MARGIN_SECONDS = 600
# Below this a token that survived a failed refresh is no longer worth handing out.
USABLE_MARGIN_SECONDS = 60
BROKER_TIMEOUT_SECONDS = 60
# git asks this helper for credentials; it reads the token from the environment
# of the git process, so the token itself is never part of any config value.
CREDENTIAL_HELPER = '!f(){ echo username=x-access-token; echo "password=$GH_TOKEN"; }; f'
GITHUB_HOST = "github.com"
# The broker needs the key and enough environment to run and reach GitHub; the
# supervisor's other secrets (for example the Anthropic key) stay behind.
_BROKER_ENV_KEYS = tuple(
    name
    for base in ("PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "ALL_PROXY")
    for name in (base, base.lower())
)
_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")
_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,99}")
_HOST = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
_NUMBER = re.compile(r"[1-9][0-9]{0,18}")
_TOKEN = re.compile(r"[\x21-\x7e]+")
_BROKER_MESSAGE = re.compile(r"token_broker: [\x20-\x7e]{1,200}")
# The broker's diagnosis for a 422 caused by a permission the installation has not granted:
# "permission missing: <name>[, <name>...]" and an optional parenthesised remark. Only the
# names are taken from it, and only if each is a plain lowercase identifier.
_PERMISSION_MISSING = re.compile(
    r"token_broker: permission missing: (?P<names>[a-z_]{1,40}(?:, [a-z_]{1,40}){0,15})(?: \([\x20-\x7e]{1,120}\))?"
)


class GitHubAuthError(RuntimeError):
    """The identity could not be established. The message never contains a secret."""


class PermissionMissingError(GitHubAuthError):
    """The installation has not granted permissions that Factory requests (the broker saw HTTP 422).

    ``permissions`` holds their names, each a lowercase identifier. Until an organisation owner
    accepts them on the installation no token can be minted, whatever else is configured.
    """

    def __init__(self, message, permissions):
        super().__init__(message)
        self.permissions = tuple(permissions)


class GitHubAuth(abc.ABC):
    """Which GitHub identity supervisor-side git/gh children run as."""

    @abc.abstractmethod
    def env_for_supervisor_subprocess(self):
        """Additional environment variables for a supervisor git/gh child (never for a worker)."""

    @abc.abstractmethod
    def commit_identity(self):
        """``(name, email)`` for commits the supervisor makes, or None to keep git's own."""

    def git_identity_args(self):
        """``git`` options (placed right after ``git``) that apply the commit identity."""
        identity = self.commit_identity()
        if identity is None:
            return []
        name, email = identity
        return ["-c", f"user.name={name}", "-c", f"user.email={email}"]


class AmbientAuth(GitHubAuth):
    """The user's own gh/git login: no extra environment, no forced identity."""

    def env_for_supervisor_subprocess(self):
        return {}

    def commit_identity(self):
        return None

    def __repr__(self):
        return "AmbientAuth()"


def _positive_int(value, name):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and _NUMBER.fullmatch(value):
        number = int(value)
    else:
        raise ValueError(f"{name} must be a positive integer")
    if number <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return number


class AppAuth(GitHubAuth):
    """GitHub App installation tokens from the token broker, cached in memory only."""

    def __init__(
        self,
        app_id,
        installation_id,
        repo,
        app_slug,
        app_user_id,
        broker_cmd,
        clock=time.time,
        run=subprocess.run,
        environ=None,
        github_host=GITHUB_HOST,
    ):
        if not isinstance(github_host, str) or not _HOST.fullmatch(github_host):
            raise ValueError("github_host must be a bare hostname")
        self.github_host = github_host
        self.app_id = _positive_int(app_id, "app_id")
        self.installation_id = _positive_int(installation_id, "installation_id")
        self.app_user_id = _positive_int(app_user_id, "app_user_id")
        if not isinstance(repo, str) or not _REPO.fullmatch(repo) or repo.split("/")[1] in (".", ".."):
            raise ValueError("repo must look like org/name")
        if not isinstance(app_slug, str) or not _SLUG.fullmatch(app_slug):
            raise ValueError("app_slug must contain only letters, digits and hyphens")
        if isinstance(broker_cmd, (str, bytes)) or not broker_cmd or not all(
            isinstance(part, str) and part for part in broker_cmd
        ):
            raise ValueError("broker_cmd must be a non-empty list of strings")
        self.repo, self.app_slug = repo, app_slug
        self._broker_cmd = tuple(broker_cmd)
        self._clock, self._run, self._environ = clock, run, environ
        self._lock = threading.Lock()
        # (token, expires_at epoch seconds); replaced as one tuple so an unlocked
        # reader never sees a token paired with another token's expiry.
        self._cached = None

    def __repr__(self):
        # Deliberately no token, and no key: a repr can end up in any log line.
        return (
            f"AppAuth(app_id={self.app_id}, installation_id={self.installation_id}, "
            f"repo={self.repo!r}, app_slug={self.app_slug!r})"
        )

    def env_for_supervisor_subprocess(self):
        """Token for gh, and a credential helper for git that only answers for GitHub.

        An empty ``credential.helper`` first drops every inherited helper, so a
        ``store``, ``cache`` or keychain helper in the host's gitconfig can
        neither persist the token nor be consulted. The helper itself is scoped
        to the GitHub host, so a submodule or remote on another host that asks
        for credentials gets nothing.
        """
        token = self._token()
        scoped = f"credential.https://{self.github_host}.helper"
        return {
            "GH_TOKEN": token,
            "GH_PROMPT_DISABLED": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "3",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "",
            "GIT_CONFIG_KEY_1": scoped,
            "GIT_CONFIG_VALUE_1": "",
            "GIT_CONFIG_KEY_2": scoped,
            "GIT_CONFIG_VALUE_2": CREDENTIAL_HELPER,
        }

    def commit_identity(self):
        name = f"{self.app_slug}[bot]"
        return name, f"{self.app_user_id}+{name}@users.noreply.github.com"

    def _token(self):
        """A token valid for at least the refresh margin; one broker call serves all threads."""
        cached = self._cached
        if cached is not None and self._clock() < cached[1] - REFRESH_MARGIN_SECONDS:
            return cached[0]
        with self._lock:
            cached = self._cached
            if cached is not None and self._clock() < cached[1] - REFRESH_MARGIN_SECONDS:
                return cached[0]
            try:
                self._cached = self._mint()
            except GitHubAuthError:
                # A refresh blip must not fail work while the old token still works;
                # the next call tries again.
                if cached is not None and self._clock() < cached[1] - USABLE_MARGIN_SECONDS:
                    return cached[0]
                raise
            return self._cached[0]

    def probe(self):
        """Mint a fresh token now, whatever the cache holds, to prove the identity works.

        The token is discarded: the cache is neither read nor replaced, so a health check never
        leaves a live installation token in this process, and a slow broker call does not hold
        the lock that token requests take. ``GitHubAuthError`` (``PermissionMissingError`` for a
        permission the installation has not granted) is raised on failure. Nothing about the
        token is returned.
        """
        self._mint()

    def _mint(self):
        environ = os.environ if self._environ is None else self._environ
        key = environ.get(KEY_ENV)
        if not key:
            raise GitHubAuthError(f"{KEY_ENV} is not set in the supervisor environment")
        child_env = {name: environ[name] for name in _BROKER_ENV_KEYS if name in environ}
        child_env[KEY_ENV] = key  # the only way the key reaches the broker: never argv
        command = [
            *self._broker_cmd,
            "--app-id", str(self.app_id),
            "--installation-id", str(self.installation_id),
            "--repo", self.repo,
        ]  # fmt: skip
        # Failures are recorded and raised after the except block so the error
        # carries no __context__/__cause__ that could hold broker output.
        failure, done, missing = None, None, ()
        try:
            done = self._run(
                command,
                env=child_env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=BROKER_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            failure = f"token broker timed out after {BROKER_TIMEOUT_SECONDS}s"
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            failure = f"token broker could not be run ({type(error).__name__})"
        if failure is None and done.returncode != 0:
            failure = f"token broker failed (exit {done.returncode}){_broker_hint(done.stderr)}"
            missing = _missing_permissions(done.stderr)
        if failure is not None:
            if missing:
                raise PermissionMissingError(failure, missing)
            raise GitHubAuthError(failure)
        parsed = _parse_broker_output(done.stdout)
        if parsed is None:
            raise GitHubAuthError("token broker returned an invalid response")
        token, expires_at = parsed
        if self._clock() >= expires_at:
            raise GitHubAuthError("token broker returned an already expired token")
        return token, expires_at


def _broker_hint(stderr):
    """Relay the broker's own one-line diagnosis, which its contract keeps secret-free.

    Anything else (several lines, no prefix) is dropped: the broker is a separate
    program and its output is not trusted to be safe to forward.
    """
    text = stderr.strip() if isinstance(stderr, str) else ""
    return f": {text}" if _BROKER_MESSAGE.fullmatch(text) else ""


def _missing_permissions(stderr):
    """The permission names from the broker's ``permission missing:`` line, or () for anything else."""
    text = stderr.strip() if isinstance(stderr, str) else ""
    found = _PERMISSION_MISSING.fullmatch(text) if _BROKER_MESSAGE.fullmatch(text) else None
    return tuple(found.group("names").split(", ")) if found else ()


def _parse_broker_output(text):
    """``(token, expires_at epoch)`` from the broker's JSON line, or None if malformed."""
    try:
        document = json.loads(text)
        token, expires = document["token"], document["expires_at"]
        if not (isinstance(token, str) and _TOKEN.fullmatch(token) and isinstance(expires, str)):
            return None
        moment = datetime.fromisoformat(expires)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return token, moment.timestamp()
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
        return None


def app_configured(env=os.environ):
    """Whether any GitHub App variable is set, so ``from_environment`` would not return ambient auth."""
    return any((env.get(name) or "").strip() for name in APP_ENV)


def from_environment(repo, installation_id, env=os.environ):
    """Choose the identity from the environment; a half-configured App fails loudly.

    No App variable set -> ambient credentials. All set -> AppAuth. Anything in
    between raises ValueError, because silently falling back to the operator's
    own login would make the factory act as the wrong identity.
    """
    if not app_configured(env):
        return AmbientAuth()
    values = {name: (env.get(name) or "").strip() for name in APP_ENV}
    missing = [name for name in APP_ENV if not values[name]]
    if missing:
        raise ValueError("incomplete GitHub App configuration; missing: " + ", ".join(missing))
    if installation_id is None:
        raise ValueError("GitHub App is configured but the repo config has no installation_id")
    try:
        broker_cmd = shlex.split(values["FACTORY_TOKEN_BROKER"])
    except ValueError:
        raise ValueError("FACTORY_TOKEN_BROKER is not a valid command line") from None
    return AppAuth(
        _positive_int(values["FACTORY_GITHUB_APP_ID"], "FACTORY_GITHUB_APP_ID"),
        _positive_int(installation_id, "installation_id"),
        repo,
        values["FACTORY_GITHUB_APP_SLUG"],
        _positive_int(values["FACTORY_GITHUB_APP_USER_ID"], "FACTORY_GITHUB_APP_USER_ID"),
        broker_cmd,
    )

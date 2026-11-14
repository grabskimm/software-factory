"""Per-repository GitHub App health, shown as the ``github`` field of ``GET /repos``.

A GitHub App installation that has not accepted a permission Factory now requests makes
every token request fail, and so every job. This module finds that out before the first
job: a background thread of the manager mints a token for each configured repository
(through ``github_auth.AppAuth.probe``, so the same broker and the same full permission
set a job uses) once at start and then every ``CHECK_SECONDS``, and remembers one
fixed-vocabulary string per repository:

* ``ok``: a token with the full permission set could be minted.
* ``permission missing: actions, issues``: the installation has not granted those
  permissions (names are lowercase identifiers; see ``tools/token_broker.py``).
* ``unavailable: <reason>``: no token could be minted for another reason; the reason is
  one of the fixed strings below, never text from the broker, GitHub or the config.
* ``checking``: no result yet (the first pass has not reached the repository).
* ``ambient``: no GitHub App is configured, so supervisor children use the operator's own
  ``gh`` login and there is nothing to check.

``GET /repos`` only reads the remembered string: it never mints a token. Nothing here
puts a token, a key or broker output into the string, the log line or an exception.
The class takes the manager only as a source of its configuration directory, environment,
output sink and shutdown event, so it does not import ``manager``.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Callable

from . import github_auth
from .config import load_repo_config

if TYPE_CHECKING:
    from .manager import Manager

# Seconds between passes. A fixed constant, not a setting: the cost is one broker call per
# repository, and a permission accepted by the organisation owner shows up within this time.
CHECK_SECONDS = 300.0

OK = "ok"
AMBIENT = "ambient"
CHECKING = "checking"
MISSING_PREFIX = "permission missing: "
NO_BROKER = "unavailable: token broker failed"
NO_INSTALLATION = "unavailable: no installation_id in the repo config"
BAD_APP_CONFIG = "unavailable: invalid GitHub App configuration"
BAD_REPO_CONFIG = "unavailable: invalid repository configuration"
CHECK_FAILED = "unavailable: check failed"


class GithubHealth:
    """What ``GET /repos`` reports as ``github`` for each repository, and the thread that keeps it current.

    ``auth_for(repo, installation_id, env=...)`` builds the identity (``github_auth.from_environment``
    by default); tests inject a fake. ``check_all`` is one pass and is what tests drive; ``start``
    runs it in a daemon thread that ends when ``manager.closing`` is set.
    """

    def __init__(
        self,
        manager: Manager,
        *,
        auth_for: Callable[..., object] = github_auth.from_environment,
        wait: Callable[[float], bool] | None = None,
        interval: float = CHECK_SECONDS,
    ):
        self.manager = manager
        self._auth_for = auth_for
        self._wait = wait or manager.closing.wait  # True means "stop now"
        self._interval = interval
        self._mutex = threading.Lock()
        self._states: dict[str, str] = {}
        self._auths: dict[tuple, object] = {}  # (repo, installation id) -> identity, reused so its cache is
        self._thread: threading.Thread | None = None

    # -- what GET /repos reads ------------------------------------------------------

    def status(self, repo: str) -> str:
        """The remembered state of ``repo``; never mints a token and never waits for one."""
        if not github_auth.app_configured(self.manager.env):
            return AMBIENT
        with self._mutex:
            return self._states.get(repo, CHECKING)

    # -- the thread -----------------------------------------------------------------

    def start(self) -> None:
        """Begin checking in the background; a no-op without a GitHub App, twice, or after shutdown began.

        Called by ``serve()`` after the listening line, never by ``Manager.__init__``, so
        building a manager (as every test does) starts no thread.
        """
        if (
            self._thread is not None
            or self.manager.closing.is_set()
            or not github_auth.app_configured(self.manager.env)
        ):
            return
        self._thread = threading.Thread(target=self._loop, name="factory-manager-github-health", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while True:
            try:
                self.check_all()
            except Exception as error:  # one line naming the type; the loop carries on
                self.manager.sink.warn(f"github health pass failed ({type(error).__name__})")
            if self._wait(self._interval):
                return

    # -- one pass -------------------------------------------------------------------

    def check_all(self) -> dict:
        """Check every configured repository once; returns ``{repo: state}``.

        The list is read again on every pass, so a repository registered after start is covered.
        """
        results = {}
        for repo in self.manager.configured_repos():
            if self.manager.closing.is_set():
                break
            try:
                results[repo] = self.check(repo)
            except Exception as error:  # a bug in one repository's check must not hide the others
                self.manager.sink.warn(f"github health check failed ({type(error).__name__})")
                results[repo] = self._record(repo, CHECK_FAILED)
        return results

    def check(self, repo: str) -> str:
        """Check one repository now, remember the result and return it."""
        return self._record(repo, self._evaluate(repo))

    def _evaluate(self, repo: str) -> str:
        try:
            cfg = load_repo_config(repo, config_dir=self.manager.settings.config_dir)
        except ValueError:
            return BAD_REPO_CONFIG
        if not github_auth.app_configured(self.manager.env):
            return AMBIENT
        if cfg.installation_id is None:
            return NO_INSTALLATION
        key = (repo, cfg.installation_id)
        with self._mutex:
            auth = self._auths.get(key)
        if auth is None:
            try:
                auth = self._auth_for(repo, cfg.installation_id, env=self.manager.env)
            except ValueError:  # a half-configured App; the message is not repeated
                return BAD_APP_CONFIG
            with self._mutex:
                auth = self._auths.setdefault(key, auth)
        if isinstance(auth, github_auth.AmbientAuth):
            return AMBIENT
        try:
            auth.probe()
        except github_auth.PermissionMissingError as error:
            return MISSING_PREFIX + ", ".join(error.permissions)
        except github_auth.GitHubAuthError:
            return NO_BROKER
        return OK

    def _record(self, repo: str, state: str) -> str:
        """Remember ``state``; a change (and only a change) is one fixed-format stdout line."""
        with self._mutex:
            changed = self._states.get(repo) != state
            self._states[repo] = state
        if changed:
            # Everything after "status=" is the value, which holds spaces. The repo name was
            # validated by check_repo when the configuration file was listed.
            self.manager.sink.line(f"github repo={repo} status={state}")
        return state

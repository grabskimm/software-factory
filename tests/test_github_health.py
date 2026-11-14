"""The ``github`` field of ``GET /repos`` and the background check behind it (src/factory/github_health.py).

The identity is a fake with a scripted ``probe()``, so no broker runs and nothing sleeps: a pass
is driven directly with ``check_all()``, and the thread tests inject a ``wait`` or use the real
``manager.closing`` event. One end-to-end test runs the real ``from_environment`` and ``AppAuth``
against the stand-in broker script of tests/test_github_auth.py. Every failure carries a canary
that must never reach the response, stdout or stderr.
"""

import json
import os
import shlex
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from factory import github_auth
from factory import github_health as health_module
from factory import manager as mgr
from factory.github_auth import AmbientAuth, GitHubAuthError, PermissionMissingError
from factory.github_health import GithubHealth
from tests.test_github_auth import FAKE_BROKER, FAKE_KEY
from tests.test_manager import VIEWER, ServerCase

CANARY = "CANARY-5c2e-broker-output"
APP = {
    "FACTORY_GITHUB_APP_ID": "123",
    "FACTORY_GITHUB_APP_SLUG": "factory-app",
    "FACTORY_GITHUB_APP_USER_ID": "789",
    "FACTORY_TOKEN_BROKER": "/nonexistent/token-broker",
}
MISSING = PermissionMissingError(
    f"token broker failed (exit 1): token_broker: permission missing: actions, issues ({CANARY})",
    ("actions", "issues"),
)


class FakeAuth:
    """An identity whose ``probe()`` raises or returns whatever ``outcomes[repo]`` says at that moment."""

    def __init__(self, repo, outcomes, on_probe=None):
        self.repo, self.outcomes, self.on_probe = repo, outcomes, on_probe
        self.probes = 0

    def probe(self):
        self.probes += 1
        if self.on_probe is not None:
            self.on_probe(self.repo)
        outcome = self.outcomes.get(self.repo)
        if isinstance(outcome, BaseException):
            raise outcome


class AuthFor:
    """Stands in for ``github_auth.from_environment``: records what it was asked and hands out FakeAuths."""

    def __init__(self, outcomes=None, refuse=None):
        self.outcomes = {} if outcomes is None else outcomes
        self.refuse = refuse or {}  # repo -> exception raised instead of an identity
        self.built = []  # (repo, installation id, env passed)
        self.auths = {}
        self.on_probe = None

    def __call__(self, repo, installation_id, env=None):
        self.built.append((repo, installation_id, env))
        if repo in self.refuse:
            raise self.refuse[repo]
        auth = FakeAuth(repo, self.outcomes, lambda name: self.on_probe and self.on_probe(name))
        self.auths[(repo, installation_id)] = auth
        return auth

    @property
    def probes(self):
        return sum(auth.probes for auth in self.auths.values())


class HealthBase(ServerCase):
    def github_lines(self):
        return [line for line in self.out.getvalue().splitlines() if line.startswith("github ")]

    def assert_canary_free(self, *texts):
        for text in (self.out.getvalue(), self.err.getvalue(), *texts):
            self.assertNotIn(CANARY, text)
            self.assertNotIn("Traceback", text)


class HealthCase(HealthBase):
    """A real server whose manager has the GitHub App variables set and a fake identity factory."""

    def setUp(self):
        super().setUp()
        self.write_config("org/name", 111)
        self.write_config("org/other", 222)
        self.serve(env=self.env(**APP))
        self.install()

    def write_config(self, repo, installation_id=None):
        lines = [f'repo = "{repo}"'] + ([f"installation_id = {installation_id}"] if installation_id else [])
        (self.config_dir / "repos" / f"{repo.replace('/', '__')}.toml").write_text("\n".join(lines) + "\n")

    def install(self, outcomes=None, refuse=None, **options):
        self.auth_for = AuthFor(outcomes, refuse)
        self.health = GithubHealth(self.manager, auth_for=self.auth_for, **options)
        self.manager.github_health = self.health  # GET /repos reads the attribute on every request
        return self.health

    def listing(self):
        status, payload, _ = self.call("GET", "/repos", principal=VIEWER)
        self.assertEqual(status, 200)
        return {entry["repo"]: entry for entry in payload["repos"]}

class ReposFieldTests(HealthCase):
    def test_before_the_first_pass_every_repository_is_checking_and_nothing_is_probed(self):
        entries = self.listing()
        self.assertEqual({repo: entry["github"] for repo, entry in entries.items()}, {"org/name": "checking", "org/other": "checking"})
        self.assertEqual(self.auth_for.built, [])

    def test_a_pass_reports_ok_or_the_missing_permissions_per_repository(self):
        self.install({"org/other": MISSING})
        self.assertEqual(self.health.check_all(), {"org/name": "ok", "org/other": "permission missing: actions, issues"})
        entries = self.listing()
        self.assertEqual(entries["org/name"]["github"], "ok")
        self.assertEqual(entries["org/other"]["github"], "permission missing: actions, issues")
        # The new field is additive: the rest of the entry is what it always was.
        self.assertEqual(entries["org/name"]["base_branch"], "main")
        self.assertEqual(entries["org/name"]["runs"], [])
        self.assertIsNone(entries["org/name"]["supervisor"])
        self.assert_canary_free(json.dumps(entries))

    def test_a_single_missing_permission_reads_as_the_issue_example(self):
        self.install({"org/name": PermissionMissingError("token broker failed (exit 1)", ("actions",))})
        self.health.check_all()
        self.assertEqual(self.listing()["org/name"]["github"], "permission missing: actions")

    def test_any_other_failure_is_one_fixed_string_that_carries_none_of_its_text(self):
        for error in (
            GitHubAuthError(f"token broker failed (exit 1): token_broker: GitHub API returned HTTP 401 {CANARY}"),
            GitHubAuthError(f"token broker timed out after 60s {CANARY}"),
            GitHubAuthError(f"GITHUB_APP_PRIVATE_KEY is not set in the supervisor environment {CANARY}"),
        ):
            with self.subTest(str(error)[:30]):
                self.install({"org/name": error})
                self.health.check_all()
                entries = self.listing()
                self.assertEqual(entries["org/name"]["github"], "unavailable: token broker failed")
                self.assertEqual(entries["org/other"]["github"], "ok")
                self.assert_canary_free(json.dumps(entries))

    def test_get_repos_never_mints_a_token(self):
        for _ in range(5):
            self.listing()
        self.health.check_all()
        probes = self.auth_for.probes
        for _ in range(5):
            self.listing()
        self.assertEqual(self.auth_for.probes, probes)
        self.assertEqual(probes, 2)

    def test_a_later_pass_turns_a_fixed_permission_green_without_a_restart(self):
        outcomes = {"org/other": MISSING}
        self.install(outcomes)
        self.health.check_all()
        self.assertEqual(self.listing()["org/other"]["github"], "permission missing: actions, issues")
        del outcomes["org/other"]  # the organisation owner accepted the permissions
        self.health.check_all()
        self.assertEqual(self.listing()["org/other"]["github"], "ok")

    def test_a_repository_registered_later_is_covered_by_the_next_pass(self):
        self.health.check_all()
        self.write_config("org/late", 333)
        self.assertEqual(self.listing()["org/late"]["github"], "checking")
        self.health.check_all()
        self.assertEqual(self.listing()["org/late"]["github"], "ok")

    def test_a_repository_without_an_installation_id_is_flagged_and_never_probed(self):
        self.write_config("org/other")  # App variables are set, the config names no installation
        self.install()
        self.health.check_all()
        self.assertEqual(self.listing()["org/other"]["github"], "unavailable: no installation_id in the repo config")
        self.assertEqual([built[0] for built in self.auth_for.built], ["org/name"])

    def test_a_half_configured_app_is_flagged_without_repeating_the_message(self):
        self.install(refuse={"org/other": ValueError(f"incomplete GitHub App configuration; missing: {CANARY}")})
        self.health.check_all()
        entries = self.listing()
        self.assertEqual(entries["org/other"]["github"], "unavailable: invalid GitHub App configuration")
        self.assertEqual(entries["org/name"]["github"], "ok")
        self.assert_canary_free(json.dumps(entries))

    def test_an_invalid_repository_config_keeps_its_error_entry_and_gets_no_github_field(self):
        (self.config_dir / "repos" / "org__other.toml").write_text('repo = "org/other"\nbogus_key = 1\n')
        self.install()
        self.assertEqual(self.health.check("org/other"), "unavailable: invalid repository configuration")
        _, payload, _ = self.call("GET", "/repos")
        self.assertIn({"repo": "org/other", "error": "invalid configuration"}, payload["repos"])

    def test_an_unexpected_failure_in_one_repository_does_not_hide_the_others(self):
        self.install({"org/name": RuntimeError(f"bug {CANARY}")})
        self.assertEqual(self.health.check_all(), {"org/name": "unavailable: check failed", "org/other": "ok"})
        self.assertIn("factory-manager: github health check failed (RuntimeError)", self.err.getvalue())
        self.assert_canary_free()

    def test_the_identity_is_built_once_per_repository_and_installation(self):
        self.health.check_all()
        self.health.check_all()
        self.assertEqual(sorted(built[:2] for built in self.auth_for.built), [("org/name", 111), ("org/other", 222)])
        self.assertTrue(all(built[2] is self.manager.env for built in self.auth_for.built))
        self.assertEqual(self.auth_for.probes, 4, "every pass probes again")
        self.write_config("org/name", 444)  # the installation id changed in the repo config
        self.health.check_all()
        self.assertEqual(self.auth_for.built[-1][:2], ("org/name", 444))

    def test_an_ambient_identity_is_reported_as_ambient(self):
        health = GithubHealth(self.manager, auth_for=lambda repo, installation_id, env=None: AmbientAuth())
        self.assertEqual(health.check("org/name"), "ambient")

    def test_an_unknown_repository_is_checking(self):
        self.assertEqual(self.health.status("org/zzz"), "checking")


class AmbientDeploymentTests(HealthBase):
    """No App variable at all: the operator's own gh login is used, so there is nothing to check."""

    def test_every_repository_is_ambient_without_any_thread_or_probe(self):
        auth_for = AuthFor()
        self.manager.github_health = GithubHealth(self.manager, auth_for=auth_for)
        status, payload, _ = self.call("GET", "/repos", principal=VIEWER)
        self.assertEqual(status, 200)
        self.assertEqual([entry["github"] for entry in payload["repos"]], ["ambient", "ambient"])
        self.manager.github_health.start()
        self.assertIsNone(self.manager.github_health._thread)
        self.assertEqual(auth_for.built, [])
        self.assertEqual(self.github_lines(), [])

    def test_a_manager_never_starts_the_check_by_itself(self):
        self.assertIsNone(self.manager.github_health._thread)
        with_app = mgr.Manager(self.manager.settings, env=self.env(**APP), stdout=self.out, stderr=self.err)
        self.assertIsNone(with_app.github_health._thread, "only serve() starts it")


class LogLineTests(HealthCase):
    def test_a_line_is_written_when_a_result_changes_and_not_when_it_repeats(self):
        outcomes = {"org/other": MISSING}
        self.install(outcomes)
        self.health.check_all()
        self.health.check_all()
        self.assertEqual(
            self.github_lines(),
            [
                "github repo=org/name status=ok",
                "github repo=org/other status=permission missing: actions, issues",
            ],
        )
        del outcomes["org/other"]
        self.health.check_all()
        self.assertEqual(self.github_lines()[2:], ["github repo=org/other status=ok"])
        self.assert_canary_free()

    def test_get_repos_writes_no_github_line(self):
        self.health.check_all()
        before = self.github_lines()
        self.listing()
        self.assertEqual(self.github_lines(), before)


class ThreadTests(HealthCase):
    def test_the_thread_checks_at_once_and_then_every_interval(self):
        waits = []

        def wait(seconds):
            waits.append(seconds)
            return len(waits) >= 3  # two more passes, then stop

        health = self.install(wait=wait)
        health.start()
        health._thread.join(30)
        self.assertFalse(health._thread.is_alive())
        self.assertEqual(waits, [health_module.CHECK_SECONDS] * 3)
        self.assertEqual(self.auth_for.probes, 6)  # three passes over two repositories
        self.assertEqual(health_module.CHECK_SECONDS, 300.0)

    def test_the_thread_is_a_daemon_and_ends_when_shutdown_begins(self):
        first = threading.Event()
        health = self.install()
        self.auth_for.on_probe = lambda repo: first.set()
        health.start()
        self.assertTrue(health._thread.daemon)
        self.assertTrue(first.wait(30))
        self.manager.closing.set()  # the default wait is manager.closing.wait
        health._thread.join(30)
        self.assertFalse(health._thread.is_alive())

    def test_start_is_idempotent_and_refused_once_shutdown_began(self):
        health = self.install(wait=lambda seconds: True)
        health.start()
        thread = health._thread
        health.start()
        self.assertIs(health._thread, thread)
        thread.join(30)
        other = self.install(wait=lambda seconds: True)
        self.manager.closing.set()
        other.start()
        self.assertIsNone(other._thread)

    def test_shutdown_stops_a_pass_between_repositories(self):
        health = self.install()
        self.auth_for.on_probe = lambda repo: self.manager.closing.set()
        self.assertEqual(health.check_all(), {"org/name": "ok"})
        self.assertEqual(self.auth_for.probes, 1)

    def test_a_pass_that_fails_as_a_whole_is_one_stderr_line_and_the_loop_goes_on(self):
        calls = []

        def wait(seconds):
            calls.append(seconds)
            return len(calls) >= 2

        health = self.install(wait=wait)
        with mock.patch.object(self.manager, "configured_repos", side_effect=[OSError(CANARY), ["org/name"]]):
            health.start()
            health._thread.join(30)
        self.assertEqual(len(calls), 2)
        self.assertIn("factory-manager: github health pass failed (OSError)", self.err.getvalue())
        self.assertEqual(health.status("org/name"), "ok")
        self.assert_canary_free()

    def test_serve_starts_the_check_after_the_listening_line(self):
        first = threading.Event()
        health = self.install(wait=lambda seconds: True)
        self.auth_for.on_probe = lambda repo: first.set()
        server = mgr.make_server(self.manager)  # its own listener on a free port; the case's one keeps running
        threading.Thread(target=lambda: (first.wait(30), server.shutdown()), daemon=True).start()
        with mock.patch.object(mgr.signal, "signal"):
            self.assertEqual(mgr.serve(self.manager, server), 0)
        self.assertIsNotNone(health._thread)
        health._thread.join(30)
        lines = self.out.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("factory-manager listening "), lines)
        self.assertIn("github repo=org/name status=ok", lines[1:])


class EndToEndTests(HealthBase):
    """The real ``from_environment``, ``AppAuth`` and a stand-in broker process, behind a real ``GET /repos``."""

    def test_the_brokers_permission_line_reaches_get_repos_and_a_fix_turns_it_green(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        state = Path(temp.name)
        (state / "broker.py").write_text(FAKE_BROKER)

        def script(**control):
            (state / "control.json").write_text(json.dumps({"expect_key": FAKE_KEY, **control}))

        # The broker fails with its permission line; a token on its stdout must still not travel.
        line = "token_broker: permission missing: actions, issues (HTTP 422: the installation has not granted them)\n"
        script(exit=1, stdout=f'{{"token": "{CANARY}"}}\n', stderr=line)
        (self.config_dir / "repos" / "org__name.toml").write_text('repo = "org/name"\ninstallation_id = 111\n')
        command = " ".join(shlex.quote(part) for part in (sys.executable, str(state / "broker.py"), str(state)))
        self.serve(env=self.env(**{**APP, "FACTORY_TOKEN_BROKER": command}))
        health = self.manager.github_health

        def github():
            _, payload, _ = self.call("GET", "/repos", principal=VIEWER)
            return {row["repo"]: row for row in payload["repos"]}["org/name"]["github"], json.dumps(payload)

        with mock.patch.dict(os.environ, {github_auth.KEY_ENV: FAKE_KEY}):
            health.check("org/name")
            value, body = github()
            self.assertEqual(value, "permission missing: actions, issues")
            self.assertNotIn(CANARY, body + self.out.getvalue() + self.err.getvalue())

            script(stdout=json.dumps({"token": "ghs_FAKE_end_to_end", "expires_at": "2999-01-01T00:00:00Z"}) + "\n")
            health.check("org/name")
            value, body = github()
            self.assertEqual(value, "ok")
            self.assertNotIn("ghs_FAKE_end_to_end", body + self.out.getvalue() + self.err.getvalue())
        self.assertEqual(
            self.github_lines(),
            ["github repo=org/name status=permission missing: actions, issues", "github repo=org/name status=ok"],
        )

if __name__ == "__main__":
    unittest.main()

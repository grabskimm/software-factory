"""Tests for factory.github_auth. No cryptography needed: the broker is a tiny fake script.

All keys and tokens are obviously fake. The point of most tests is the negative
space: the token and the key must not leak into errors, reprs, argv or other
environment variables.
"""

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
import unittest
from datetime import datetime, timezone
from pathlib import Path

from factory import github_auth
from factory.github_auth import (
    AmbientAuth,
    AppAuth,
    GitHubAuth,
    GitHubAuthError,
    PermissionMissingError,
    app_configured,
    from_environment,
)

FAKE_TOKEN = "ghs_FAKE_token_for_unit_tests_0001"
FAKE_TOKEN_2 = "ghs_FAKE_token_for_unit_tests_0002"
FAKE_KEY = "-----BEGIN RSA PRIVATE KEY-----\nFAKEFAKEFAKEFAKEFAKE\n-----END RSA PRIVATE KEY-----\n"
T0 = 1_800_000_000.0
HOUR = 3600
EXPECTED_HELPER = '!f(){ echo username=x-access-token; echo "password=$GH_TOKEN"; }; f'

# Stand-in for tools/token_broker.py. The first argument is a state directory
# holding control.json (what to do) and calls.jsonl (what was observed).
FAKE_BROKER = r"""
import json, os, sys, time
state = sys.argv[1]
control = json.load(open(os.path.join(state, "control.json")))
call = {
    "argv": sys.argv[2:],
    "key_ok": os.environ.get("GITHUB_APP_PRIVATE_KEY") == control.get("expect_key"),
    "env_keys": sorted(os.environ),
}
with open(os.path.join(state, "calls.jsonl"), "a") as handle:
    handle.write(json.dumps(call) + "\n")
time.sleep(control.get("sleep", 0))
sys.stderr.write(control.get("stderr", ""))
sys.stdout.write(control.get("stdout", ""))
sys.exit(control.get("exit", 0))
"""


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


class BrokerCase(unittest.TestCase):
    """Common fixture: a fake broker script plus an AppAuth wired to it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        (self.state / "broker.py").write_text(FAKE_BROKER)
        self.clock = Clock()
        self.environ = {
            "PATH": os.environ.get("PATH", ""),
            "GITHUB_APP_PRIVATE_KEY": FAKE_KEY,
            "ANTHROPIC_API_KEY": "fake-anthropic-key",
            "GH_TOKEN": "fake-ambient-gh-token",
            "SOMETHING_ELSE": "x",
        }
        self.script(token=FAKE_TOKEN)

    def script(self, token=FAKE_TOKEN, ttl=HOUR, **overrides):
        control = {
            "expect_key": FAKE_KEY,
            "stdout": json.dumps({"token": token, "expires_at": iso(self.clock.now + ttl)}) + "\n",
        }
        control.update(overrides)
        (self.state / "control.json").write_text(json.dumps(control))

    def calls(self):
        path = self.state / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def auth(self, **overrides):
        options = dict(
            app_id=123,
            installation_id=456,
            repo="acme/widgets",
            app_slug="factory-app",
            app_user_id=789,
            broker_cmd=[sys.executable, str(self.state / "broker.py"), str(self.state)],
            clock=self.clock,
            environ=self.environ,
        )
        options.update(overrides)
        return AppAuth(**options)

    def assert_no_secret(self, error):
        rendered = "\n".join(
            [str(error), repr(error), "".join(traceback.format_exception(error))]
            + [str(part) for part in error.args]
        )
        for secret in (FAKE_TOKEN, FAKE_TOKEN_2, FAKE_KEY, "FAKEFAKEFAKE", "fake-anthropic-key"):
            self.assertNotIn(secret, rendered)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__, "no chained exception may carry broker output")


class IdentityTests(BrokerCase):
    def test_ambient_changes_nothing(self):
        auth = AmbientAuth()
        self.assertIsInstance(auth, GitHubAuth)
        self.assertEqual(auth.env_for_supervisor_subprocess(), {})
        self.assertIsNone(auth.commit_identity())
        self.assertEqual(auth.git_identity_args(), [])

    def test_app_commit_identity_is_the_bot_noreply_address(self):
        auth = self.auth()
        self.assertEqual(
            auth.commit_identity(),
            ("factory-app[bot]", "789+factory-app[bot]@users.noreply.github.com"),
        )
        self.assertEqual(
            auth.git_identity_args(),
            ["-c", "user.name=factory-app[bot]", "-c", "user.email=789+factory-app[bot]@users.noreply.github.com"],
        )
        self.assertEqual(self.calls(), [], "identity needs no token and no broker call")

    def test_the_interface_cannot_be_used_directly(self):
        with self.assertRaises(TypeError):
            GitHubAuth()

    def test_constructor_rejects_bad_values(self):
        bad = [
            {"app_id": 0}, {"app_id": -1}, {"app_id": True}, {"app_id": "x"}, {"app_id": 1.5},
            {"installation_id": 0}, {"installation_id": None},
            {"app_user_id": 0}, {"app_user_id": "12a"},
            {"repo": "widgets"}, {"repo": "a/b/c"}, {"repo": "a/b\n"}, {"repo": "-a/b"}, {"repo": "a/.."},
            {"app_slug": ""}, {"app_slug": "x[bot]"}, {"app_slug": "a b"}, {"app_slug": "a\nb"},
            {"app_slug": "x;y"},
            {"broker_cmd": []}, {"broker_cmd": "python broker.py"}, {"broker_cmd": [""]}, {"broker_cmd": [1]},
        ]  # fmt: skip
        for overrides in bad:
            with self.subTest(overrides):
                with self.assertRaises(ValueError):
                    self.auth(**overrides)

    def test_repr_and_str_never_show_the_token_or_key(self):
        auth = self.auth()
        token = auth.env_for_supervisor_subprocess()["GH_TOKEN"]
        for text in (repr(auth), str(auth), f"{auth}", "%r" % (auth,)):
            self.assertNotIn(token, text)
            self.assertNotIn(FAKE_KEY, text)
            self.assertNotIn("FAKEFAKE", text)
            self.assertTrue(text.startswith("AppAuth("))
        self.assertEqual(repr(AmbientAuth()), "AmbientAuth()")


class EnvTests(BrokerCase):
    def test_env_is_exactly_the_documented_set(self):
        env = self.auth().env_for_supervisor_subprocess()
        self.assertEqual(
            env,
            {
                "GH_TOKEN": FAKE_TOKEN,
                "GH_PROMPT_DISABLED": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_COUNT": "3",
                "GIT_CONFIG_KEY_0": "credential.helper",
                "GIT_CONFIG_VALUE_0": "",
                "GIT_CONFIG_KEY_1": "credential.https://github.com.helper",
                "GIT_CONFIG_VALUE_1": "",
                "GIT_CONFIG_KEY_2": "credential.https://github.com.helper",
                "GIT_CONFIG_VALUE_2": EXPECTED_HELPER,
            },
        )

    def test_token_appears_only_as_gh_token(self):
        """Nothing that git would persist (config values) may contain the token."""
        env = self.auth().env_for_supervisor_subprocess()
        for name, value in env.items():
            if name != "GH_TOKEN":
                self.assertNotIn(FAKE_TOKEN, value)
        self.assertIn("$GH_TOKEN", env["GIT_CONFIG_VALUE_2"])

    def test_each_call_returns_a_fresh_dict(self):
        auth = self.auth()
        first = auth.env_for_supervisor_subprocess()
        first["GH_TOKEN"] = "mutated"
        first["EXTRA"] = "1"
        self.assertEqual(auth.env_for_supervisor_subprocess()["GH_TOKEN"], FAKE_TOKEN)
        self.assertNotIn("EXTRA", auth.env_for_supervisor_subprocess())

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_the_credential_helper_hands_git_the_token_from_the_environment(self):
        env = self.auth().env_for_supervisor_subprocess()
        home = self.state / "home"
        home.mkdir()
        child_env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(home),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            **env,
        }
        done = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            env=child_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        answer = dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)
        self.assertEqual(answer["username"], "x-access-token")
        self.assertEqual(answer["password"], FAKE_TOKEN)

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_a_hostile_global_helper_is_neither_asked_nor_given_the_token(self):
        """An inherited `store` helper must not persist the token; other hosts get nothing."""
        env = self.auth().env_for_supervisor_subprocess()
        home = self.state / "hostile-home"
        home.mkdir()
        stored = self.state / "stolen-credentials"
        (home / ".gitconfig").write_text(f"[credential]\n\thelper = store --file={stored}\n")
        child_env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1", **env}

        def git_credential(action, text):
            return subprocess.run(
                ["git", "credential", action], input=text, env=child_env, capture_output=True, text=True, timeout=30
            )

        github = "protocol=https\nhost=github.com\n\n"
        filled = git_credential("fill", github)
        self.assertEqual(filled.returncode, 0, filled.stderr)
        self.assertIn(f"password={FAKE_TOKEN}", filled.stdout)
        # git reports success to every configured helper: nothing may be stored
        self.assertEqual(git_credential("approve", filled.stdout).returncode, 0)
        self.assertFalse(stored.exists(), "an inherited store helper persisted the token")
        # a host that challenges for credentials gets none
        other = git_credential("fill", "protocol=https\nhost=gitlab.example.invalid\n\n")
        self.assertNotEqual(other.returncode, 0)
        self.assertNotIn(FAKE_TOKEN, other.stdout + other.stderr)

    def test_github_host_is_validated_and_configurable(self):
        env = self.auth(github_host="ghe.example.com").env_for_supervisor_subprocess()
        self.assertEqual(env["GIT_CONFIG_KEY_2"], "credential.https://ghe.example.com.helper")
        for bad in ("", "a b", "x/y", "-x", "https://github.com", "a" * 300):
            with self.subTest(host=bad), self.assertRaises(ValueError):
                self.auth(github_host=bad)

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_git_config_files_never_contain_the_token(self):
        """The env-only helper leaves no trace in config files, even after a fill."""
        env = self.auth().env_for_supervisor_subprocess()
        repo = self.state / "repo"
        base = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(self.state / "home"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
        (self.state / "home").mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], env=base, check=True, capture_output=True)
        subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            env={**base, **env},
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
        listing = subprocess.run(
            ["git", "config", "--list", "--show-origin"],
            env=base,
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertNotIn(FAKE_TOKEN, listing)
        self.assertNotIn("credential.helper", listing, "nothing persisted; the helper exists only in the child env")
        for path in self.state.rglob("*"):
            if path.is_file() and path.name not in ("control.json", "calls.jsonl"):
                self.assertNotIn(FAKE_TOKEN.encode(), path.read_bytes(), str(path))


class CacheTests(BrokerCase):
    def test_the_token_is_cached_between_calls(self):
        auth = self.auth()
        for _ in range(5):
            self.assertEqual(auth.env_for_supervisor_subprocess()["GH_TOKEN"], FAKE_TOKEN)
        self.assertEqual(len(self.calls()), 1)

    def test_broker_receives_flags_and_the_key_only_through_its_environment(self):
        self.auth().env_for_supervisor_subprocess()
        (call,) = self.calls()
        self.assertEqual(
            call["argv"],
            ["--app-id", "123", "--installation-id", "456", "--repo", "acme/widgets"],
        )
        self.assertTrue(call["key_ok"], "the key must arrive in the child's environment")
        for part in call["argv"]:
            self.assertNotIn("FAKE", part)
        # An allowlist, not a copy: other secrets of the supervisor stay behind.
        self.assertNotIn("ANTHROPIC_API_KEY", call["env_keys"])
        self.assertNotIn("GH_TOKEN", call["env_keys"])
        self.assertNotIn("SOMETHING_ELSE", call["env_keys"])
        self.assertIn("PATH", call["env_keys"])

    def test_refreshes_exactly_ten_minutes_before_expiry(self):
        auth = self.auth()
        self.assertEqual(auth.env_for_supervisor_subprocess()["GH_TOKEN"], FAKE_TOKEN)
        self.clock.now = T0 + HOUR - 601
        self.assertEqual(auth.env_for_supervisor_subprocess()["GH_TOKEN"], FAKE_TOKEN)
        self.assertEqual(len(self.calls()), 1)
        self.clock.now = T0 + HOUR - 600
        self.script(token=FAKE_TOKEN_2, ttl=HOUR)
        self.assertEqual(auth.env_for_supervisor_subprocess()["GH_TOKEN"], FAKE_TOKEN_2)
        self.assertEqual(len(self.calls()), 2)
        # and the new token is cached for its own full life
        self.clock.now += HOUR - 700
        self.assertEqual(auth.env_for_supervisor_subprocess()["GH_TOKEN"], FAKE_TOKEN_2)
        self.assertEqual(len(self.calls()), 2)

    def test_many_threads_share_one_broker_call(self):
        self.script(sleep=0.4)
        auth = self.auth()
        count = 24
        barrier = threading.Barrier(count)
        results, errors = [], []

        def worker():
            try:
                barrier.wait(10)
                results.append(auth.env_for_supervisor_subprocess()["GH_TOKEN"])
            except Exception as error:  # surfaced below
                errors.append(error)

        threads = [threading.Thread(target=worker) for _ in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(results, [FAKE_TOKEN] * count)
        self.assertEqual(len(self.calls()), 1)

    def test_many_threads_share_one_refresh_too(self):
        auth = self.auth()
        auth.env_for_supervisor_subprocess()
        self.clock.now = T0 + HOUR - 100
        self.script(token=FAKE_TOKEN_2, sleep=0.3)
        count = 16
        barrier = threading.Barrier(count)
        results = []

        def worker():
            barrier.wait(10)
            results.append(auth.env_for_supervisor_subprocess()["GH_TOKEN"])

        threads = [threading.Thread(target=worker) for _ in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(results, [FAKE_TOKEN_2] * count)
        self.assertEqual(len(self.calls()), 2)

    def test_expires_at_with_an_offset_or_without_a_zone_is_understood(self):
        # All four spellings mean 08:00 UTC; a missing zone is read as UTC.
        for text in ("2027-01-15T08:00:00Z", "2027-01-15T08:00:00+00:00", "2027-01-15T10:00:00+02:00", "2027-01-15T08:00:00"):
            with self.subTest(text):
                (self.state / "calls.jsonl").unlink(missing_ok=True)
                start = datetime(2027, 1, 15, 7, 0, tzinfo=timezone.utc).timestamp()
                self.clock.now = start
                self.script(stdout=json.dumps({"token": FAKE_TOKEN, "expires_at": text}))
                auth = self.auth()
                auth.env_for_supervisor_subprocess()
                self.clock.now = start + 49 * 60  # 07:49, a minute before the refresh point
                auth.env_for_supervisor_subprocess()
                self.assertEqual(len(self.calls()), 1)
                self.clock.now = start + 50 * 60  # 07:50 = 08:00 minus the ten minute margin
                auth.env_for_supervisor_subprocess()
                self.assertEqual(len(self.calls()), 2)


class FailureTests(BrokerCase):
    def assert_fails(self, auth=None, contains=None):
        auth = auth or self.auth()
        with self.assertRaises(GitHubAuthError) as caught:
            auth.env_for_supervisor_subprocess()
        self.assertIsInstance(caught.exception, RuntimeError)
        self.assert_no_secret(caught.exception)
        if contains:
            self.assertIn(contains, str(caught.exception))
        return caught.exception

    def test_broker_exit_status_is_reported_without_its_stdout(self):
        # A broken broker that dumps a token on stdout and still fails.
        self.script(exit=1, stdout=FAKE_TOKEN + "\n", stderr="token_broker: GitHub API returned HTTP 401 (JWT rejected)\n")
        error = self.assert_fails(contains="exit 1")
        self.assertIn("HTTP 401", str(error), "the broker's own one-line diagnosis is relayed")

    def test_only_the_brokers_own_prefixed_single_line_message_is_relayed(self):
        cases = {
            "token on stderr": FAKE_TOKEN + "\n",
            "key on stderr": FAKE_KEY,
            "two lines": "token_broker: first\ntoken_broker: second\n",
            "traceback": f"Traceback (most recent call last):\n  File x\nValueError: {FAKE_TOKEN}\n",
            "prefixed secret multi": f"token_broker: ok\n{FAKE_TOKEN}",
            "too long": f"token_broker: {'a' * 300}",
            "control chars": "token_broker: bad\x1b[31m\x00",
        }
        for label, stderr in cases.items():
            with self.subTest(label):
                self.script(exit=1, stderr=stderr)
                error = self.assert_fails(contains="exit 1")
                self.assertNotIn("token_broker:", str(error))

    MISSING_LINE = "token_broker: permission missing: actions, issues (HTTP 422: the installation has not granted them)\n"

    def test_a_permission_the_installation_has_not_granted_is_a_typed_error(self):
        self.script(exit=1, stdout=FAKE_TOKEN + "\n", stderr=self.MISSING_LINE)
        error = self.assert_fails(contains="exit 1")
        self.assertIsInstance(error, PermissionMissingError)
        self.assertEqual(error.permissions, ("actions", "issues"))
        self.assertEqual(
            str(error),
            "token broker failed (exit 1): token_broker: permission missing: actions, issues "
            "(HTTP 422: the installation has not granted them)",
        )

    def test_the_permission_names_are_read_from_a_name_list_with_or_without_a_remark(self):
        for stderr, names in (
            ("token_broker: permission missing: actions\n", ("actions",)),
            ("token_broker: permission missing: pull_requests, checks\n", ("pull_requests", "checks")),
            ("token_broker: permission missing: issues (any remark)\n", ("issues",)),
        ):
            with self.subTest(stderr):
                self.script(exit=1, stderr=stderr)
                self.assertEqual(self.assert_fails().permissions, names)

    def test_anything_but_the_exact_permission_line_stays_a_plain_error(self):
        cases = {
            "upper case": "token_broker: permission missing: Actions\n",
            "a digit": "token_broker: permission missing: actions2\n",
            "injected text": "token_broker: permission missing: actions; rm -rf /\n",
            "no names": "token_broker: permission missing: \n",
            "empty name": "token_broker: permission missing: actions, , issues\n",
            "separator without a space": "token_broker: permission missing: actions,issues\n",
            "an overlong name": f"token_broker: permission missing: {'a' * 41}\n",
            "too many names": "token_broker: permission missing: " + ", ".join(["a"] * 17) + "\n",
            "two lines": self.MISSING_LINE + "token_broker: permission missing: contents\n",
            "text before the names": "token_broker: GitHub said permission missing: actions\n",
            "no broker prefix": "permission missing: actions\n",
            "text after the names": "token_broker: permission missing: actions and then some\n",
            "control character": "token_broker: permission missing: actions\x1b[31m\n",
        }
        for label, stderr in cases.items():
            with self.subTest(label):
                self.script(exit=1, stderr=stderr)
                error = self.assert_fails()
                self.assertNotIsInstance(error, PermissionMissingError)

    def test_a_permission_line_with_a_zero_exit_status_is_not_a_failure_report(self):
        self.script(exit=0, stderr=self.MISSING_LINE)  # a valid token on stdout wins
        self.assertEqual(self.auth().env_for_supervisor_subprocess()["GH_TOKEN"], FAKE_TOKEN)

    def test_the_typed_error_carries_no_secret_and_no_chained_exception(self):
        self.script(exit=1, stdout=FAKE_TOKEN + "\n", stderr=self.MISSING_LINE)
        error = self.assert_fails()
        self.assert_no_secret(error)
        self.assertEqual(self.calls()[0]["argv"][-2:], ["--repo", "acme/widgets"])

    def test_invalid_responses_never_leak_the_token(self):
        good_expiry = iso(T0 + HOUR)
        outputs = {
            "not json": f"{FAKE_TOKEN} not json {{",
            "truncated json": f'{{"token": "{FAKE_TOKEN}", "expires_at": ',
            "empty": "",
            "array": json.dumps([FAKE_TOKEN]),
            "missing token": json.dumps({"tokens": FAKE_TOKEN, "expires_at": good_expiry}),
            "missing expiry": json.dumps({"token": FAKE_TOKEN}),
            "empty token": json.dumps({"token": "", "expires_at": good_expiry}),
            "non string token": json.dumps({"token": 12345, "expires_at": good_expiry}),
            "token with whitespace": json.dumps({"token": FAKE_TOKEN + " x", "expires_at": good_expiry}),
            "token with newline": json.dumps({"token": FAKE_TOKEN + "\nGH_TOKEN=evil", "expires_at": good_expiry}),
            "bad expiry": json.dumps({"token": FAKE_TOKEN, "expires_at": "tomorrow-ish"}),
            "non string expiry": json.dumps({"token": FAKE_TOKEN, "expires_at": 1893456000}),
            "null expiry": json.dumps({"token": FAKE_TOKEN, "expires_at": None}),
            "deep nesting": "[" * 100000 + FAKE_TOKEN,
        }
        for label, stdout in outputs.items():
            with self.subTest(label):
                self.script(stdout=stdout)
                self.assert_fails(contains="invalid response")

    def test_an_already_expired_token_is_refused(self):
        self.script(ttl=-5)
        self.assert_fails(contains="expired")

    def test_missing_key_fails_before_running_the_broker(self):
        del self.environ["GITHUB_APP_PRIVATE_KEY"]
        self.assert_fails(contains="GITHUB_APP_PRIVATE_KEY")
        self.environ["GITHUB_APP_PRIVATE_KEY"] = ""
        self.assert_fails(contains="GITHUB_APP_PRIVATE_KEY")
        self.assertEqual(self.calls(), [])

    def test_a_broker_that_cannot_be_started(self):
        auth = self.auth(broker_cmd=[str(self.state / "does-not-exist")])
        self.assert_fails(auth, contains="could not be run")

    def test_a_broker_that_hangs_is_timed_out_and_its_partial_output_is_not_leaked(self):
        def hanging_run(command, **kwargs):
            self.assertEqual(kwargs["timeout"], github_auth.BROKER_TIMEOUT_SECONDS)
            raise subprocess.TimeoutExpired(command, kwargs["timeout"], output=FAKE_TOKEN.encode(), stderr=FAKE_KEY.encode())

        self.assert_fails(self.auth(run=hanging_run), contains="timed out")

    def test_run_receives_the_key_only_in_env_and_never_a_shell(self):
        seen = {}

        def spying_run(command, **kwargs):
            seen["command"], seen["kwargs"] = command, kwargs
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"token": FAKE_TOKEN, "expires_at": iso(T0 + HOUR)}), ""
            )

        self.auth(run=spying_run).env_for_supervisor_subprocess()
        self.assertIsInstance(seen["command"], list)
        self.assertFalse(seen["kwargs"].get("shell", False))
        self.assertNotIn(FAKE_KEY, " ".join(seen["command"]))
        self.assertEqual(seen["kwargs"]["env"]["GITHUB_APP_PRIVATE_KEY"], FAKE_KEY)
        self.assertEqual(seen["kwargs"]["stdin"], subprocess.DEVNULL)
        self.assertTrue(seen["kwargs"]["capture_output"])

    def test_failures_are_not_cached_and_the_next_call_retries(self):
        self.script(exit=1)
        auth = self.auth()
        self.assert_fails(auth)
        self.script(token=FAKE_TOKEN_2)
        self.assertEqual(auth.env_for_supervisor_subprocess()["GH_TOKEN"], FAKE_TOKEN_2)
        self.assertEqual(len(self.calls()), 2)

    def test_a_failed_refresh_keeps_serving_the_old_token_while_it_is_still_valid(self):
        auth = self.auth()
        self.assertEqual(auth.env_for_supervisor_subprocess()["GH_TOKEN"], FAKE_TOKEN)
        self.script(exit=1)
        self.clock.now = T0 + HOUR - 300  # inside the refresh margin, still valid
        self.assertEqual(auth.env_for_supervisor_subprocess()["GH_TOKEN"], FAKE_TOKEN)
        self.assertEqual(len(self.calls()), 2, "every call inside the margin retries the refresh")
        self.clock.now = T0 + HOUR - 30  # too close to expiry to hand out
        self.assert_fails(auth)

    def test_threads_all_see_the_error_when_the_broker_fails(self):
        self.script(exit=1, sleep=0.2)
        auth = self.auth()
        outcomes = []
        barrier = threading.Barrier(6)

        def worker():
            barrier.wait(10)
            try:
                auth.env_for_supervisor_subprocess()
                outcomes.append("ok")
            except GitHubAuthError:
                outcomes.append("error")

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(outcomes, ["error"] * 6)


class ProbeTests(BrokerCase):
    def test_probe_mints_even_when_the_cache_is_fresh_and_discards_the_new_token(self):
        auth = self.auth()
        self.assertEqual(auth.env_for_supervisor_subprocess()["GH_TOKEN"], FAKE_TOKEN)
        self.script(token=FAKE_TOKEN_2)
        self.assertIsNone(auth.probe())
        self.assertEqual(len(self.calls()), 2, "a valid cached token says nothing about the grant today")
        self.assertEqual(
            auth.env_for_supervisor_subprocess()["GH_TOKEN"],
            FAKE_TOKEN,
            "the probed token must not replace the cached one",
        )
        self.assertEqual(len(self.calls()), 2, "the earlier cached token was served without another call")

    def test_probe_on_a_cold_cache_leaves_it_cold(self):
        auth = self.auth()
        self.assertIsNone(auth.probe())
        self.assertEqual(len(self.calls()), 1)
        self.script(token=FAKE_TOKEN_2)
        self.assertEqual(
            auth.env_for_supervisor_subprocess()["GH_TOKEN"],
            FAKE_TOKEN_2,
            "the probed token was discarded, so the first real request mints its own",
        )
        self.assertEqual(len(self.calls()), 2)

    def test_probe_never_stores_a_token_anywhere_on_the_object(self):
        auth = self.auth()
        auth.probe()
        self.assertNotIn(FAKE_TOKEN, repr(vars(auth)), "no attribute may hold the probed token")

    def test_probe_asks_the_broker_with_the_installation_and_the_repo(self):
        self.auth().probe()
        (call,) = self.calls()
        self.assertEqual(call["argv"], ["--app-id", "123", "--installation-id", "456", "--repo", "acme/widgets"])
        self.assertTrue(call["key_ok"])

    def test_a_failed_probe_raises_and_is_not_cached(self):
        auth = self.auth()
        self.script(exit=1, stdout=FAKE_TOKEN + "\n", stderr="token_broker: GitHub API returned HTTP 401\n")
        with self.assertRaises(GitHubAuthError) as caught:
            auth.probe()
        self.assert_no_secret(caught.exception)
        self.script(token=FAKE_TOKEN_2)
        self.assertEqual(auth.env_for_supervisor_subprocess()["GH_TOKEN"], FAKE_TOKEN_2)
        self.assertEqual(len(self.calls()), 2)

    def test_a_failed_probe_leaves_a_valid_cached_token_in_place(self):
        auth = self.auth()
        auth.env_for_supervisor_subprocess()
        self.script(exit=1, stderr=FailureTests.MISSING_LINE)
        with self.assertRaises(PermissionMissingError) as caught:
            auth.probe()
        self.assertEqual(caught.exception.permissions, ("actions", "issues"))
        self.assert_no_secret(caught.exception)
        self.assertEqual(auth.env_for_supervisor_subprocess()["GH_TOKEN"], FAKE_TOKEN)
        self.assertEqual(len(self.calls()), 2, "the cached token was served without another broker call")

    def test_a_probe_does_not_take_the_token_request_lock(self):
        auth = self.auth()
        finished = threading.Event()

        def probe():
            auth.probe()
            finished.set()

        thread = threading.Thread(target=probe, daemon=True)
        with auth._lock:  # a token request is in flight: the probe must not wait for it
            thread.start()
            done = finished.wait(10)  # bounded, so a regression fails fast instead of hanging CI
        thread.join(10)
        self.assertTrue(done, "the probe waited for the token request lock")
        self.assertEqual(len(self.calls()), 1)


class FromEnvironmentTests(unittest.TestCase):
    FULL = {
        "FACTORY_GITHUB_APP_ID": "123",
        "FACTORY_GITHUB_APP_SLUG": "factory-app",
        "FACTORY_GITHUB_APP_USER_ID": "789",
        "FACTORY_TOKEN_BROKER": "/opt/token-broker/bin/python /opt/token-broker/token_broker.py",
    }

    def test_nothing_configured_means_ambient(self):
        for env in ({}, {"PATH": "/usr/bin"}, {name: "" for name in self.FULL}, {name: "  " for name in self.FULL}):
            with self.subTest(env):
                self.assertIsInstance(from_environment("acme/widgets", None, env), AmbientAuth)
                self.assertIsInstance(from_environment("acme/widgets", 456, env), AmbientAuth)

    def test_app_configured_is_true_as_soon_as_any_variable_is_set(self):
        for env in ({}, {"PATH": "/usr/bin"}, {name: "" for name in self.FULL}, {name: "  " for name in self.FULL}):
            with self.subTest(env):
                self.assertFalse(app_configured(env))
        for name in self.FULL:
            with self.subTest(name):
                self.assertTrue(app_configured({name: self.FULL[name]}))
        self.assertTrue(app_configured(self.FULL))

    def test_fully_configured_builds_an_app_auth(self):
        auth = from_environment("acme/widgets", 456, self.FULL)
        self.assertIsInstance(auth, AppAuth)
        self.assertEqual((auth.app_id, auth.installation_id, auth.app_user_id), (123, 456, 789))
        self.assertEqual((auth.repo, auth.app_slug), ("acme/widgets", "factory-app"))
        self.assertEqual(auth._broker_cmd, ("/opt/token-broker/bin/python", "/opt/token-broker/token_broker.py"))
        self.assertEqual(auth.commit_identity(), ("factory-app[bot]", "789+factory-app[bot]@users.noreply.github.com"))

    def test_the_broker_command_is_shell_split(self):
        env = dict(self.FULL, FACTORY_TOKEN_BROKER="python3 '/opt/my dir/token_broker.py' --api-url \"https://ghe.example/api/v3\"")
        auth = from_environment("acme/widgets", 456, env)
        self.assertEqual(
            auth._broker_cmd,
            ("python3", "/opt/my dir/token_broker.py", "--api-url", "https://ghe.example/api/v3"),
        )

    def test_installation_id_may_be_a_digit_string_from_a_loose_config(self):
        self.assertEqual(from_environment("acme/widgets", "456", self.FULL).installation_id, 456)

    def test_a_half_configured_app_always_fails_loudly(self):
        for name in self.FULL:
            with self.subTest(missing=name):
                env = {k: v for k, v in self.FULL.items() if k != name}
                with self.assertRaises(ValueError) as caught:
                    from_environment("acme/widgets", 456, env)
                self.assertIn(name, str(caught.exception))
            with self.subTest(only=name):
                with self.assertRaises(ValueError):
                    from_environment("acme/widgets", 456, {name: self.FULL[name]})
            with self.subTest(blank=name):
                with self.assertRaises(ValueError):
                    from_environment("acme/widgets", 456, dict(self.FULL, **{name: "  "}))

    def test_app_variables_without_an_installation_id_fail_instead_of_falling_back(self):
        with self.assertRaises(ValueError) as caught:
            from_environment("acme/widgets", None, self.FULL)
        self.assertIn("installation_id", str(caught.exception))

    def test_ids_must_be_positive_integers(self):
        for name in ("FACTORY_GITHUB_APP_ID", "FACTORY_GITHUB_APP_USER_ID"):
            for value in ("0", "-1", "abc", "1.5", "1e3", "12 34", "+5", "007", "٣", "0x10"):
                with self.subTest(name=name, value=value):
                    with self.assertRaises(ValueError) as caught:
                        from_environment("acme/widgets", 456, dict(self.FULL, **{name: value}))
                    self.assertIn(name, str(caught.exception))
        for value in (0, -4, True, False, 1.5, "x", "0", ""):
            with self.subTest(installation_id=value):
                with self.assertRaises(ValueError):
                    from_environment("acme/widgets", value, self.FULL)

    def test_bad_slug_repo_or_broker_command_is_rejected(self):
        for overrides in (
            {"FACTORY_GITHUB_APP_SLUG": "bad slug"},
            {"FACTORY_GITHUB_APP_SLUG": "x[bot]"},
            {"FACTORY_TOKEN_BROKER": "python 'unterminated"},
        ):
            with self.subTest(overrides):
                with self.assertRaises(ValueError):
                    from_environment("acme/widgets", 456, dict(self.FULL, **overrides))
        with self.assertRaises(ValueError):
            from_environment("not-a-repo", 456, self.FULL)

    def test_the_process_environment_is_the_default_source(self):
        original = dict(os.environ)
        try:
            for name in self.FULL:
                os.environ.pop(name, None)
            self.assertIsInstance(from_environment("acme/widgets", 456), AmbientAuth)
            os.environ["FACTORY_GITHUB_APP_ID"] = "123"
            with self.assertRaises(ValueError):
                from_environment("acme/widgets", 456)
        finally:
            os.environ.clear()
            os.environ.update(original)

    def test_errors_do_not_echo_the_values_they_reject(self):
        env = dict(self.FULL, FACTORY_GITHUB_APP_ID="not-a-number-SECRETISH")
        with self.assertRaises(ValueError) as caught:
            from_environment("acme/widgets", 456, env)
        self.assertNotIn("SECRETISH", str(caught.exception))


class PackageBoundaryTests(unittest.TestCase):
    def test_the_factory_package_never_imports_cryptography_or_tools(self):
        root = Path(github_auth.__file__).parent
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    top = name.split(".")[0]
                    self.assertNotIn(top, ("cryptography", "tools", "token_broker"), f"{path.name} imports {name}")

    def test_importing_github_auth_loads_no_third_party_code(self):
        done = subprocess.run(
            [sys.executable, "-c",
             "import sys; import factory.github_auth; "
             "sys.exit(1 if 'cryptography' in sys.modules else 0)"],
            env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
            capture_output=True,
            timeout=60,
        )  # fmt: skip
        self.assertEqual(done.returncode, 0, done.stderr)


if __name__ == "__main__":
    unittest.main()

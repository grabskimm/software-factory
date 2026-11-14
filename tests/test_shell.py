"""Supervisor-side subprocess environment: what git and gh inherit."""

import os
import unittest
from unittest.mock import patch

from factory import shell
from factory.github_auth import GitHubAuth


class IdentityAuth(GitHubAuth):
    def env_for_supervisor_subprocess(self):
        return {"GH_TOKEN": "fake-token-for-tests"}

    def commit_identity(self):
        return "app[bot]", "1+app[bot]@users.noreply.github.com"

    def git_identity_args(self):
        name, email = self.commit_identity()
        return ["-c", f"user.name={name}", "-c", f"user.email={email}"]


def child_env(name):
    return dict(
        line.split("=", 1)
        for line in shell.command(["env"], os.getcwd()).splitlines()
        if "=" in line
    ).get(name)


class SupervisorEnvTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(shell.set_auth, shell.get_auth())
        shell.set_auth(None)

    def test_children_never_inherit_the_app_key_or_the_worker_key(self):
        with patch.dict(os.environ, {"GITHUB_APP_PRIVATE_KEY": "fake-pem", "ANTHROPIC_API_KEY": "fake-key", "PATH": os.environ["PATH"]}):
            for auth in (None, IdentityAuth()):
                with self.subTest(auth=type(auth).__name__):
                    shell.set_auth(auth)
                    self.assertIsNone(child_env("GITHUB_APP_PRIVATE_KEY"))
                    self.assertIsNone(child_env("ANTHROPIC_API_KEY"))

    def test_the_token_reaches_only_children_started_here(self):
        shell.set_auth(IdentityAuth())
        self.assertEqual(child_env("GH_TOKEN"), "fake-token-for-tests")
        shell.set_auth(None)
        with patch.dict(os.environ):
            os.environ.pop("GH_TOKEN", None)
            self.assertIsNone(child_env("GH_TOKEN"))

    def test_inherited_author_variables_cannot_override_the_app_identity(self):
        shell.set_auth(IdentityAuth())
        with patch.dict(os.environ, {"GIT_AUTHOR_NAME": "Ops Person", "GIT_COMMITTER_EMAIL": "ops@corp.invalid"}):
            self.assertIsNone(child_env("GIT_AUTHOR_NAME"))
            self.assertIsNone(child_env("GIT_COMMITTER_EMAIL"))

    def test_ambient_auth_leaves_the_operators_git_identity_alone(self):
        with patch.dict(os.environ, {"GIT_AUTHOR_NAME": "Ops Person"}):
            self.assertEqual(child_env("GIT_AUTHOR_NAME"), "Ops Person")

    def test_using_auth_restores_the_previous_auth(self):
        outer = IdentityAuth()
        shell.set_auth(outer)
        with shell.using_auth(None):
            self.assertIsNone(shell.get_auth())
        self.assertIs(shell.get_auth(), outer)
        # identity_args follows the active auth: bot identity, then none for ambient
        self.assertEqual(
            shell.identity_args(),
            ["-c", "user.name=app[bot]", "-c", "user.email=1+app[bot]@users.noreply.github.com"],
        )
        with shell.using_auth(None):
            self.assertEqual(shell.identity_args(), [])


if __name__ == "__main__":
    unittest.main()

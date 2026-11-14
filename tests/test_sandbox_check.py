"""``factory-sandbox-check`` (image/sandbox-check.sh) and the harness it runs, tools/sandbox_e2e.py.

The image sets FACTORY_REQUIRE_WORKER_IDENTITY=1, and run_agent refuses a call without a
worker identity when it is set. The check already runs as the worker, and sandbox_e2e.py
calls run_agent without one, so the wrapper must clear the flag; otherwise no probe ever
reaches claude and the check reports a false FAIL. sandbox_e2e.py must also say so when
run_agent stops before claude starts, instead of printing only a list of FAIL lines.
"""

import importlib.util
import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from factory import gitsafe

ROOT = Path(__file__).resolve().parent.parent
CHECK = ROOT / "image" / "sandbox-check.sh"
E2E = ROOT / "tools" / "sandbox_e2e.py"
SETPRIV = "/usr/bin/setpriv"


def load_e2e():
    spec = importlib.util.spec_from_file_location("sandbox_e2e_under_test", E2E)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SandboxCheckScriptTest(unittest.TestCase):
    def run_with_stub_setpriv(self):
        """Run the script with setpriv replaced by a stub that prints what it received."""
        script = CHECK.read_text()
        code = [line for line in script.splitlines() if not line.lstrip().startswith("#")]
        self.assertEqual(sum(line.count(SETPRIV) for line in code), 1, "exactly one setpriv exec")
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp) / "setpriv"
            stub.write_text(
                "#!/bin/sh\n"
                'printf "FLAG=%s\\n" "${FACTORY_REQUIRE_WORKER_IDENTITY-unset}"\n'
                'printf "HOME=%s\\n" "$HOME"\n'
                'for arg in "$@"; do printf "ARG=%s\\n" "$arg"; done\n'
            )
            stub.chmod(0o755)
            copy = Path(tmp) / "sandbox-check.sh"
            copy.write_text(script.replace(SETPRIV, str(stub)))
            env = dict(os.environ, FACTORY_REQUIRE_WORKER_IDENTITY="1", HOME="/root")
            done = subprocess.run(
                ["sh", str(copy), "--extra"], env=env, capture_output=True, text=True, timeout=30
            )
        self.assertEqual(done.returncode, 0, done.stderr)
        lines = done.stdout.splitlines()
        args = [line[len("ARG="):] for line in lines if line.startswith("ARG=")]
        return lines, args

    def test_the_check_clears_the_worker_identity_requirement_for_its_child(self):
        lines, _ = self.run_with_stub_setpriv()
        self.assertIn("FLAG=0", lines)
        self.assertIn("HOME=/tmp", lines)

    def test_the_check_still_runs_the_harness_as_the_worker_without_capabilities(self):
        _, args = self.run_with_stub_setpriv()
        for flag in (
            "--reuid=10002",
            "--regid=10010",
            "--clear-groups",
            "--inh-caps=-all",
            "--ambient-caps=-all",
            "--no-new-privs",
        ):
            self.assertIn(flag, args)
        separator = args.index("--")
        self.assertEqual(
            args[separator + 1 :],
            ["/opt/factory/bin/python", "/opt/factory-tools/sandbox_e2e.py", "--extra"],
        )


class SandboxE2eHarnessTest(unittest.TestCase):
    def setUp(self):
        self.e2e = load_e2e()
        self.tmp = Path(tempfile.mkdtemp(prefix="sandbox-e2e-test-"))
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)], check=False)
        self.api = self.e2e.FakeApi([])
        self.addCleanup(self.api.close)

    def invoke(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.e2e.invoke(self.api, self.tmp, "review-0", self.tmp)
        return output.getvalue()

    def test_a_refusal_before_claude_starts_is_reported_as_a_harness_failure(self):
        with patch.dict(os.environ, {"FACTORY_REQUIRE_WORKER_IDENTITY": "1"}):
            output = self.invoke()
        self.assertIn("HARNESS run_agent stopped before claude reached the fake API", output)
        self.assertIn("[worker] uid and gid", output)
        self.assertEqual(self.api.requests, [])

    def test_the_expected_error_after_claude_reached_the_api_stays_silent(self):
        def reached_the_api_then_failed(*args, **kwargs):
            self.api.requests.append({"path": "/v1/messages", "key": self.e2e.KEY})
            raise self.e2e.AgentError("no structured result")

        with patch.dict(os.environ, {}), patch.object(self.e2e, "run_agent", reached_the_api_then_failed):
            output = self.invoke()
        self.assertEqual(output, "")


class SandboxE2eRealWorktreeTest(unittest.TestCase):
    """make_repo builds a real linked worktree, and run_agent only runs git on a registered one.

    A bare directory with no ``.git`` (the harness test above) short-circuits before that check, so
    this drives the path the script itself takes, with a stub ``claude`` in place of the real CLI.
    """

    def setUp(self):
        self.e2e = load_e2e()
        self.tmp = Path(tempfile.mkdtemp(prefix="sandbox-e2e-real-"))
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)], check=False)
        self.api = self.e2e.FakeApi([])
        self.addCleanup(self.api.close)
        self.root = self.tmp / "case"
        self.main, self.worktree = self.e2e.make_repo(self.root)
        self.addCleanup(gitsafe.forget, self.worktree)
        self.started = self.tmp / "claude-started-in"
        stub = self.tmp / "bin"
        stub.mkdir()
        claude = stub / "claude"
        claude.write_text(
            "#!/bin/sh\n"
            f"pwd > {self.started}\n"
            """printf '%s\\n' '{"type":"result","subtype":"success","is_error":false,"result":"not json"}'\n"""
        )
        claude.chmod(0o755)
        self.path = os.pathsep.join([str(stub), os.environ.get("PATH", os.defpath)])

    def invoke(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"PATH": self.path}), redirect_stdout(output):
            os.environ.pop("FACTORY_REQUIRE_WORKER_IDENTITY", None)
            self.e2e.invoke(self.api, self.root, "review-0", self.worktree)
        return output.getvalue()

    def test_make_repo_registers_the_worktree_it_creates(self):
        self.assertTrue((self.worktree / ".git").is_file(), "a linked worktree, not a bare directory")
        self.assertIsNotNone(gitsafe.trusted(self.worktree))
        self.assertEqual(gitsafe.trusted(self.worktree), gitsafe.resolve_gitdir(self.main, self.worktree))

    def test_run_agent_reaches_claude_in_the_worktree_make_repo_created(self):
        self.invoke()
        self.assertEqual(self.started.read_text().strip(), os.path.realpath(self.worktree))

    def test_an_unregistered_worktree_is_refused_before_claude_starts(self):
        gitsafe.forget(self.worktree)
        with self.assertRaises(gitsafe.GitSafetyError):
            self.invoke()
        self.assertFalse(self.started.exists())


if __name__ == "__main__":
    unittest.main()

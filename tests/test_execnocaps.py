"""The wrapper that strips ambient capabilities before a worker runs anything."""

import contextlib
import io
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from factory import agent, execnocaps

WRAPPER = Path(execnocaps.__file__)


def run_wrapper(*command, env=None):
    return subprocess.run(
        [sys.executable, "-I", "-S", str(WRAPPER), "--", *command],
        capture_output=True,
        text=True,
        env=env if env is not None else dict(os.environ),
        timeout=60,
    )


class ExecTests(unittest.TestCase):
    def test_runs_the_command_with_its_arguments_environment_and_exit_status(self):
        env = {**os.environ, "SPIKE_VALUE": "7"}
        done = run_wrapper(
            sys.executable, "-c",
            "import os, sys; print(os.environ['SPIKE_VALUE'], sys.argv[1:]); sys.exit(3)",
            "a", "--flag",
            env=env,
        )  # fmt: skip
        self.assertEqual(done.returncode, 3)
        self.assertEqual(done.stdout.strip(), "7 ['a', '--flag']")

    def test_the_process_that_runs_the_command_has_no_ambient_capabilities(self):
        done = run_wrapper("grep", "^CapAmb", "/proc/self/status")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(int(done.stdout.split(":")[1], 16), 0)

    def test_a_missing_command_is_reported_without_a_traceback(self):
        done = run_wrapper("definitely-not-a-command-xyz")
        self.assertEqual(done.returncode, execnocaps.EXIT_NOT_FOUND)
        self.assertIn("cannot execute the worker command", done.stderr)
        self.assertNotIn("Traceback", done.stderr)

    def test_no_command_is_refused(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(execnocaps.main(["--"]), execnocaps.EXIT_REFUSED)
        self.assertIn("usage", err.getvalue())


class FailClosedTests(unittest.TestCase):
    def test_ambient_capabilities_are_cleared_before_exec(self):
        masks = iter([0xE0, 0])
        order = []
        with (
            patch.object(execnocaps, "ambient_set", side_effect=lambda: next(masks)),
            patch.object(execnocaps, "clear_ambient", side_effect=lambda: order.append("clear")),
            patch("os.execvp", side_effect=lambda *a: order.append("exec")),
        ):
            execnocaps.main(["--", "worker"])
        self.assertEqual(order, ["clear", "exec"])

    def test_nothing_runs_when_the_set_cannot_be_cleared(self):
        err = io.StringIO()
        with (
            patch.object(execnocaps, "ambient_set", return_value=0xE0),
            patch.object(execnocaps, "clear_ambient", side_effect=OSError(1, "no")),
            patch("os.execvp", side_effect=AssertionError("must not exec")),
            contextlib.redirect_stderr(err),
        ):
            status = execnocaps.main(["--", "worker"])
        self.assertEqual(status, execnocaps.EXIT_REFUSED)
        self.assertIn("refusing to start the worker", err.getvalue())

    def test_nothing_runs_when_capabilities_remain_after_clearing(self):
        with (
            patch.object(execnocaps, "ambient_set", return_value=0xE0),
            patch.object(execnocaps, "clear_ambient"),
            patch("os.execvp", side_effect=AssertionError("must not exec")),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(execnocaps.main(["--", "worker"]), execnocaps.EXIT_REFUSED)

    def test_nothing_runs_when_the_status_cannot_be_read_on_linux(self):
        with (
            patch.object(execnocaps, "ambient_set", return_value=None),
            patch("sys.platform", "linux"),
            patch("os.execvp", side_effect=AssertionError("must not exec")),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(execnocaps.main(["--", "worker"]), execnocaps.EXIT_REFUSED)


class WiringTests(unittest.TestCase):
    def test_worker_commands_go_through_the_wrapper(self):
        command = agent.drop_capabilities_first(["claude", "-p"])
        self.assertEqual(command[:3], [sys.executable, "-I", "-S"])
        self.assertEqual(Path(command[3]), WRAPPER)
        self.assertEqual(command[4:], ["--", "claude", "-p"])

    def test_cleanup_reports_a_group_it_may_not_signal_and_does_not_raise(self):
        err = io.StringIO()
        with (
            patch("os.killpg", side_effect=PermissionError(1, "Operation not permitted")),
            contextlib.redirect_stderr(err),
        ):
            self.assertIs(agent.signal_group(4242), False)
        self.assertIn("could not signal worker process group 4242", err.getvalue())


if __name__ == "__main__":
    unittest.main()

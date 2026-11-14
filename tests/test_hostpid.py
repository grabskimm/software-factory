"""Host-scoped child PIDs: a pid from another replica is 'unknown', never 'dead'."""

import contextlib
import io
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from factory import state as state_module
from factory.state import (
    HOST_ID,
    State,
    alive,
    atomic_json,
    boot_id,
    host_id,
    pid_status,
)

OTHER_HOST = "replica-b/11111111-2222-3333-4444-555555555555"
PLAN = {"jobs": [{"id": "a"}, {"id": "b"}]}


def kill_group(pgid):
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGKILL)


def sleeper(test):
    """A real child in its own session/process group, killed when the test ends."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        start_new_session=True,
    )

    def stop():
        kill_group(child.pid)
        child.wait()

    test.addCleanup(stop)
    return child


def dead_pid():
    """The pid of a process group that has exited and been reaped."""
    child = subprocess.Popen(
        [sys.executable, "-c", "pass"], start_new_session=True
    )
    child.wait()
    return child.pid


class HostIdTests(unittest.TestCase):
    def test_host_id_is_hostname_slash_boot_id(self):
        self.assertEqual(host_id(), f"{socket.gethostname()}/{boot_id()}")
        self.assertEqual(HOST_ID, host_id())

    def test_boot_id_is_read_and_stripped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "boot_id"
            path.write_text("  abc-123\n")
            with patch.object(state_module, "BOOT_ID_PATH", str(path)):
                self.assertEqual(boot_id(), "abc-123")
                self.assertTrue(host_id().endswith("/abc-123"))

    def test_boot_id_falls_back_to_unknown_when_unreadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty"
            empty.write_text("\n")
            binary = Path(tmp) / "binary"
            binary.write_bytes(b"\xff\xfe\x00")
            for path in (Path(tmp) / "missing", empty, binary, Path(tmp)):
                with self.subTest(path=path.name):
                    with patch.object(state_module, "BOOT_ID_PATH", str(path)):
                        self.assertEqual(boot_id(), "unknown")
                        self.assertEqual(
                            host_id(), f"{socket.gethostname()}/unknown"
                        )

    def test_host_id_can_be_overridden_for_tests(self):
        with patch.object(state_module, "host_id", return_value=OTHER_HOST):
            group = os.getpgrp()  # real and alive, but this process now claims another host
            self.assertEqual(pid_status(group, OTHER_HOST), "alive")
            self.assertEqual(pid_status(group, HOST_ID), "unknown")


class PidStatusTests(unittest.TestCase):
    def test_no_pid_is_dead(self):
        for pid in (None, 0, ""):
            with self.subTest(pid=pid):
                self.assertEqual(pid_status(pid), "dead")
                self.assertEqual(pid_status(pid, OTHER_HOST), "dead")

    def test_same_host_alive_and_dead(self):
        child = sleeper(self)
        for pid_host in (None, host_id()):
            with self.subTest(pid_host=pid_host):
                self.assertEqual(pid_status(child.pid, pid_host), "alive")
        kill_group(child.pid)
        child.wait()
        for pid_host in (None, host_id()):
            with self.subTest(pid_host=pid_host, exited=True):
                self.assertEqual(pid_status(child.pid, pid_host), "dead")

    def test_group_is_alive_while_any_member_runs_after_the_leader_exits(self):
        leader = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import subprocess, sys; "
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])",
            ],
            start_new_session=True,
        )
        self.addCleanup(kill_group, leader.pid)
        leader.wait()
        self.assertEqual(pid_status(leader.pid), "alive")

    def test_foreign_host_is_unknown_even_when_the_number_is_live_or_unused(self):
        child = sleeper(self)
        self.assertEqual(pid_status(child.pid, OTHER_HOST), "unknown")
        self.assertEqual(pid_status(dead_pid(), OTHER_HOST), "unknown")
        # A same-hostname container from before a reboot is also a different host.
        rebooted = f"{socket.gethostname()}/{'0' * 8}-old-boot"
        self.assertNotEqual(rebooted, host_id())
        self.assertEqual(pid_status(child.pid, rebooted), "unknown")
        self.assertEqual(pid_status(child.pid, ""), "unknown")
        self.assertEqual(pid_status(child.pid, 7), "unknown")

    def test_foreign_pid_is_never_probed(self):
        with patch("factory.state.alive") as probe:
            self.assertEqual(pid_status(4242, OTHER_HOST), "unknown")
        probe.assert_not_called()

    def test_corrupt_pid_records_are_unknown_not_dead_and_never_crash(self):
        for pid in ("123", -5, True, 2**70, 1.5, [1]):
            with self.subTest(pid=pid):
                self.assertEqual(pid_status(pid), "unknown")

    def test_alive_keeps_its_boolean_contract(self):
        child = sleeper(self)
        self.assertIs(alive(child.pid), True)
        self.assertIs(alive(None), False)
        self.assertIs(alive(0), False)
        kill_group(child.pid)
        child.wait()
        self.assertIs(alive(child.pid), False)

    def test_permission_error_still_means_alive(self):
        with patch("os.killpg", side_effect=PermissionError):
            self.assertEqual(pid_status(4242), "alive")
            self.assertIs(alive(4242), True)


class KillGroupTests(unittest.TestCase):
    def test_true_when_signalled_false_when_the_group_is_gone(self):
        child = sleeper(self)
        self.assertIs(state_module.kill_group(child.pid), True)
        child.wait()
        self.assertIs(state_module.kill_group(child.pid), False)

    def test_other_errors_propagate(self):
        with patch("os.killpg", side_effect=PermissionError), self.assertRaises(PermissionError):
            state_module.kill_group(4242)

    def test_one_unsignalable_group_is_reported_and_does_not_stop_the_rest(self):
        from factory import agent

        signalled = []

        def killpg(group, sig):
            signalled.append(group)
            if group == 11:
                raise PermissionError(1, "Operation not permitted")

        with agent._LIVE_LOCK:
            agent._LIVE.update({11, 22})
        self.addCleanup(agent._LIVE.difference_update, {11, 22})
        err = io.StringIO()
        with patch("os.killpg", side_effect=killpg), contextlib.redirect_stderr(err):
            agent.kill_live_agents()
        self.assertEqual(sorted(signalled), [11, 22])
        self.assertIn(
            "could not kill worker process group 11: Operation not permitted", err.getvalue()
        )

    def test_runner_kill_children_reports_and_continues(self):
        from types import SimpleNamespace

        from factory.runner import Runner

        jobs = {"a": {"pid": 11, "pid_host": None}, "b": {"pid": 22, "pid_host": None}}
        fake = SimpleNamespace(state=SimpleNamespace(data={"jobs": jobs}))
        signalled = []

        def killpg(group, sig):
            signalled.append(group)
            if group == 11:
                raise PermissionError(1, "Operation not permitted")

        err = io.StringIO()
        with (
            patch("factory.runner.pid_status", return_value="alive"),
            patch("os.killpg", side_effect=killpg),
            contextlib.redirect_stderr(err),
        ):
            Runner.kill_children(fake)
        self.assertEqual(signalled, [11, 22])
        self.assertIn("could not kill process group 11", err.getvalue())


class RecoverTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.state = State(self.directory, PLAN)

    def reload(self):
        return State(self.directory, PLAN).data["jobs"]

    def run_job(self, key, pid, pid_host):
        self.state.update(key, status="running", pid=pid, pid_host=pid_host)

    def assert_untouched(self, key, pid, pid_host, status="running"):
        job = self.reload()[key]
        self.assertEqual(
            (job["status"], job["pid"], job.get("pid_host")), (status, pid, pid_host)
        )

    def test_same_host_dead_pid_is_interrupted_and_clears_both_fields(self):
        pid = dead_pid()
        self.run_job("a", pid, host_id())
        self.run_job("b", pid, None)  # state written before pid_host existed
        self.state.recover()
        for job in self.reload().values():
            self.assertEqual(job["status"], "interrupted")
            self.assertIsNone(job["pid"])
            self.assertIsNone(job["pid_host"])
            self.assertIn("Supervisor interrupted", job["error"])

    def test_same_host_live_child_blocks_even_with_a_lease(self):
        child = sleeper(self)
        for lease in (None, lambda: True):
            with self.subTest(lease=lease):
                self.run_job("a", child.pid, host_id())
                with self.assertRaisesRegex(
                    RuntimeError, rf"a: child process group {child.pid} is still alive"
                ):
                    self.state.recover(holds_lease=lease)
                self.assert_untouched("a", child.pid, host_id())

    def test_foreign_pid_without_the_lease_blocks_and_changes_nothing(self):
        self.run_job("a", 4242, OTHER_HOST)
        for lease in (None, lambda: False, lambda: 1, lambda: "yes", True):
            with self.subTest(lease=lease):
                with self.assertRaisesRegex(
                    RuntimeError, "another host.*repository lease"
                ):
                    self.state.recover(holds_lease=lease)
                self.assert_untouched("a", 4242, OTHER_HOST)

    def test_foreign_pid_with_the_lease_is_dead_and_the_job_is_interrupted(self):
        self.run_job("a", 4242, OTHER_HOST)
        self.state.recover(holds_lease=lambda: True)
        job = self.reload()["a"]
        self.assertEqual(job["status"], "interrupted")
        self.assertIsNone(job["pid"])
        self.assertIsNone(job["pid_host"])

    def test_foreign_pid_that_is_live_here_is_still_just_a_foreign_pid(self):
        child = sleeper(self)
        self.run_job("a", child.pid, OTHER_HOST)
        with self.assertRaisesRegex(RuntimeError, "another host"):
            self.state.recover()
        self.state.recover(holds_lease=lambda: True)  # never probed, never killed
        self.assertEqual(self.reload()["a"]["status"], "interrupted")
        self.assertEqual(pid_status(child.pid), "alive")

    def test_one_blocking_job_blocks_recovery_of_all_jobs(self):
        child = sleeper(self)
        self.run_job("a", 4242, OTHER_HOST)
        self.run_job("b", child.pid, host_id())
        with self.assertRaisesRegex(RuntimeError, "still alive"):
            self.state.recover(holds_lease=lambda: True)
        self.assert_untouched("a", 4242, OTHER_HOST)
        self.assert_untouched("b", child.pid, host_id())

    def test_lease_is_asked_once_and_only_for_foreign_pids(self):
        calls = []

        def lease():
            calls.append(1)
            return True

        self.run_job("a", dead_pid(), host_id())
        self.state.recover(holds_lease=lease)
        self.assertEqual(calls, [])
        self.run_job("a", 4242, OTHER_HOST)
        self.run_job("b", 4343, OTHER_HOST)
        self.state.recover(holds_lease=lease)
        self.assertEqual(calls, [1])

    def test_a_failing_lease_check_fails_closed(self):
        self.run_job("a", 4242, OTHER_HOST)

        def broken():
            raise OSError("lease backend unreachable")

        with self.assertRaises(OSError):
            self.state.recover(holds_lease=broken)
        self.assert_untouched("a", 4242, OTHER_HOST)

    def test_stale_foreign_pid_on_a_finished_job_is_cleared_only_under_the_lease(self):
        self.state.update("a", status="failed", pid=4242, pid_host=OTHER_HOST)
        with self.assertRaisesRegex(RuntimeError, "another host"):
            self.state.recover()
        self.state.recover(holds_lease=lambda: True)
        job = self.reload()["a"]
        self.assertEqual(job["status"], "failed")  # not resurrected as interrupted
        self.assertIsNone(job["pid"])
        self.assertIsNone(job["pid_host"])
        self.state.recover()  # no foreign pid left: no lease needed any more

    def test_corrupt_pid_blocks_without_the_lease(self):
        self.run_job("a", "not-a-pid", None)
        with self.assertRaisesRegex(RuntimeError, "cannot be verified"):
            self.state.recover()
        self.assert_untouched("a", "not-a-pid", None)

    def test_old_state_without_pid_host_still_loads_and_recovers(self):
        directory = self.directory / "old"
        directory.mkdir()
        old = {
            "version": 1,
            "jobs": {
                key: {
                    "status": "running",
                    "attempt": 1,
                    "pid": dead_pid(),
                    "created_at": "2025-01-01T00:00:00Z",
                    "updated_at": "2025-01-01T00:00:00Z",
                    "status_started_at": "2025-01-01T00:00:00Z",
                }
                for key in ("a", "b")
            },
        }
        atomic_json(directory / "state.json", old)
        state = State(directory, PLAN)
        self.assertNotIn("pid_host", state.data["jobs"]["a"])
        state.recover()
        reloaded = json.loads((directory / "state.json").read_text())
        for job in reloaded["jobs"].values():
            self.assertEqual(job["status"], "interrupted")
            self.assertIsNone(job["pid"])
            self.assertIsNone(job["pid_host"])
            self.assertEqual(job["attempt"], 1)

    def test_old_state_with_a_live_unqualified_pid_still_blocks(self):
        child = sleeper(self)
        self.state.update("a", status="running", pid=child.pid)  # no pid_host at all
        with self.assertRaisesRegex(RuntimeError, "still alive"):
            self.state.recover(holds_lease=lambda: True)

    def test_patched_alive_still_drives_recovery_for_existing_callers(self):
        self.state.update("a", status="running", pid=99)
        with patch("factory.state.alive", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "still alive"):
                self.state.recover()
        with patch("factory.state.alive", return_value=False):
            self.state.recover()
        self.assertEqual(self.reload()["a"]["status"], "interrupted")

    def test_recovery_without_arguments_keeps_working_for_idle_states(self):
        self.state.recover()
        self.assertEqual(
            {job["status"] for job in self.reload().values()}, {"queued"}
        )


class RecordPidTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.state = State(self.directory, PLAN)

    def test_record_pid_stores_pid_with_this_host_and_persists(self):
        self.state.record_pid("a", 1234)
        job = State(self.directory, PLAN).data["jobs"]["a"]
        self.assertEqual((job["pid"], job["pid_host"]), (1234, host_id()))

    def test_record_pid_uses_the_overridable_host_id(self):
        with patch.object(state_module, "host_id", return_value=OTHER_HOST):
            self.state.record_pid("a", 1234)
        self.assertEqual(self.state.data["jobs"]["a"]["pid_host"], OTHER_HOST)

    def test_record_none_and_clear_pid_remove_both_fields(self):
        for clear in (
            lambda: self.state.record_pid("a", None),
            lambda: self.state.clear_pid("a"),
        ):
            self.state.record_pid("a", 1234)
            clear()
            job = State(self.directory, PLAN).data["jobs"]["a"]
            self.assertIsNone(job["pid"])
            self.assertIsNone(job["pid_host"])

    def test_record_pid_does_not_disturb_other_job_fields_or_jobs(self):
        self.state.update("a", status="running", attempt=2, phase="build-0")
        self.state.record_pid("a", 1234)
        job = self.state.data["jobs"]["a"]
        self.assertEqual(
            (job["status"], job["attempt"], job["phase"]), ("running", 2, "build-0")
        )
        self.assertNotIn("pid", self.state.data["jobs"]["b"])

    def test_record_pid_on_a_recorded_child_round_trips_through_recover(self):
        child = sleeper(self)
        self.state.update("a", status="running")
        self.state.record_pid("a", child.pid)
        with self.assertRaisesRegex(RuntimeError, "still alive"):
            self.state.recover()
        kill_group(child.pid)
        child.wait()
        self.state.recover()
        self.assertEqual(self.state.data["jobs"]["a"]["status"], "interrupted")


if __name__ == "__main__":
    unittest.main()

"""Memory guard: process-tree reading against a fake /proc, the sampling watchdog, and the
sweep that kills every process of a tripped worker's tree (fake /proc and real processes)."""

import errno
import io
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from factory import capacity, memwatch
from factory.memwatch import Sample, Watchdog, read_tree

MIB = 1024 * 1024
PAGE = 4096
TICKS = 100
BIG = 10 * MIB // PAGE  # resident pages of a process that is over a 1 MiB limit
STOP, KILL = signal.SIGSTOP, signal.SIGKILL
# This process and its parent as the fake /proc tests see them: above the largest pid the kernel
# hands out (2**22), so no fake pid in these tests can ever be a real process, or the test itself.
OWN = (4_200_001, 4_200_002)


def stat_text(pid, ppid, session, cpu_ticks=0, comm="agent", state="S", started=1000):
    """The bytes of a /proc/<pid>/stat line (utime = cpu_ticks, starttime = started)."""
    name = comm if isinstance(comm, bytes) else comm.encode()
    fields = [state, str(ppid), str(pid), str(session), *["0"] * 7, str(cpu_ticks), "0", "0", "0", "20", "0", "1", "0"]
    return b"".join([f"{pid} (".encode(), name, f") {' '.join([*fields, str(started)])}\n".encode()])


def entry(root, pid, ppid, session, rss_pages, cpu_ticks=0, comm="agent", state="S", started=1000):
    """One fake /proc/<pid> with stat and statm (resident = rss_pages)."""
    directory = Path(root) / str(pid)
    directory.mkdir()
    (directory / "stat").write_bytes(stat_text(pid, ppid, session, cpu_ticks, comm, state, started))
    (directory / "statm").write_text(f"90000 {rss_pages} 10 0 0 0 0\n")


def alive(pid):
    """Whether a real process is still running (a zombie is not)."""
    try:
        text = Path(f"/proc/{pid}/stat").read_bytes()
    except OSError:
        return False
    return text.rpartition(b")")[2].split()[0].decode() not in ("Z", "X")


class FakeKernel:
    """The ``kill`` handed to a watchdog over a fake /proc: records each signal and, as the kernel
    would, makes SIGKILL remove the process (unless it is declared immortal)."""

    def __init__(self, proc, on_signal=None):
        self.proc = Path(proc)
        self.on_signal = on_signal
        self.calls = []
        self.identities = set()
        self.immortal = set()

    def __call__(self, pid, started, sig=KILL):
        self.calls.append((pid, sig))
        self.identities.add((pid, started))
        if self.on_signal is not None:
            self.on_signal(pid, sig)
        if sig == KILL and pid not in self.immortal:
            shutil.rmtree(self.proc / str(pid), ignore_errors=True)
        return True

    def sent(self, sig):
        return [pid for pid, got in self.calls if got == sig]

    def signalled(self):
        return {pid for pid, _ in self.calls}


class ProcCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.proc = Path(temp.name)
        for patcher in (
            patch.object(capacity, "_NOTICED", set()),
            patch.object(memwatch, "_own_pids", return_value=OWN),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def tree(self, root=100):
        return read_tree(root, self.proc, page_size=PAGE, clock_ticks=TICKS)

    def guard(self, kernel, limit_mb=1, timeout=2.0, on_exceed=None, root=100):
        return Watchdog(
            root,
            limit_mb,
            on_exceed or (lambda sig: None),
            proc_root=self.proc,
            page_size=PAGE,
            clock_ticks=TICKS,
            kill=kernel,
            kill_timeout=timeout,
        )


class ReadTreeTests(ProcCase):
    def test_sums_descendants_and_reparented_orphans_but_not_strangers(self):
        entry(self.proc, 100, 1, 100, 100, cpu_ticks=200)
        entry(self.proc, 101, 100, 100, 10, cpu_ticks=50)
        entry(self.proc, 102, 101, 102, 20)  # grandchild that started its own session
        entry(self.proc, 103, 1, 100, 5)  # orphan: parent died, same session
        entry(self.proc, 200, 1, 200, 5000)  # unrelated process
        found = self.tree()
        self.assertEqual(found, Sample((100 + 10 + 20 + 5) * PAGE, 2.5, 4))
        self.assertEqual([pid for pid, _ in found.members], [100, 101, 102, 103])

    def test_an_orphan_of_a_descendants_own_session_counts(self):
        entry(self.proc, 100, 1, 100, 1)
        entry(self.proc, 101, 100, 101, 1)  # setsid
        entry(self.proc, 102, 1, 101, 7)  # its child, orphaned: parent is init, session is 101
        entry(self.proc, 103, 1, 103, 99)  # stranger
        self.assertEqual(self.tree().rss_bytes, 9 * PAGE)

    def test_command_names_with_spaces_and_parentheses_do_not_shift_fields(self):
        entry(self.proc, 100, 1, 100, 7, cpu_ticks=100, comm="node (worker) x")
        self.assertEqual(self.tree(), Sample(7 * PAGE, 1.0, 1))

    def test_a_command_name_cannot_forge_the_fields_after_it(self):
        forged = b"x) R 9 9 9 0 0 0 0 0 0 0 999 999 999 999 0 0 0 0 999 ("
        entry(self.proc, 100, 1, 100, 7, cpu_ticks=100, comm=forged)
        entry(self.proc, 101, 100, 100, 3, comm=b"a)")
        self.assertEqual(self.tree(), Sample(10 * PAGE, 1.0, 2))

    def test_a_command_name_that_is_not_utf8_does_not_hide_the_process(self):
        entry(self.proc, 100, 1, 100, 7, comm=b"\xff\xfe(\x80")
        entry(self.proc, 101, 100, 100, 3, comm=b"\xc3\x28 \xa0\xa1")
        entry(self.proc, 102, 101, 102, 2, comm=b"\xed\xa0\x80")
        found = self.tree()
        self.assertEqual((found.rss_bytes, found.processes), (12 * PAGE, 3))

    def test_a_process_that_vanished_or_an_unreadable_proc_is_not_a_sample(self):
        entry(self.proc, 200, 1, 200, 5)
        self.assertIsNone(self.tree(root=100))
        self.assertIsNone(read_tree(100, self.proc / "missing", page_size=PAGE, clock_ticks=TICKS))

    def test_a_member_whose_files_vanish_midway_is_skipped(self):
        entry(self.proc, 100, 1, 100, 10)
        entry(self.proc, 101, 100, 100, 10)
        (self.proc / "101" / "statm").unlink()
        self.assertEqual(self.tree().rss_bytes, 10 * PAGE)

    def test_a_directory_whose_name_only_looks_like_a_number_is_ignored(self):
        entry(self.proc, 100, 1, 100, 10)
        entry(self.proc, "²", 100, 100, 99)  # superscript two: str.isdigit() is true for it
        self.assertEqual(self.tree().rss_bytes, 10 * PAGE)

    def test_a_pid_that_was_reused_is_not_the_worker(self):
        entry(self.proc, 100, 1, 100, 10, started=1000)
        self.assertIsNotNone(read_tree(100, self.proc, PAGE, TICKS, root_started=1000))
        self.assertIsNone(read_tree(100, self.proc, PAGE, TICKS, root_started=999))

    def test_this_process_and_its_ancestors_are_never_part_of_the_tree(self):
        me, _ = OWN
        entry(self.proc, 100, 1, 100, 1)
        entry(self.proc, me, 1, 100, 50)  # claims the worker's session: cannot happen for real
        entry(self.proc, 500, me, me, 50)  # one of its children
        self.assertEqual(self.tree().rss_bytes, PAGE)

    def test_supported_needs_the_per_process_files(self):
        self.assertFalse(memwatch.supported(self.proc))
        entry(self.proc, os.getpid(), 1, 1, 1)
        self.assertTrue(memwatch.supported(self.proc))


class WatchdogTests(ProcCase):
    def watchdog(self, limit_mb, killed):
        return Watchdog(
            100,
            limit_mb,
            killed.append,
            proc_root=self.proc,
            page_size=PAGE,
            clock_ticks=TICKS,
            kill=FakeKernel(self.proc),
        )

    def test_stops_the_worker_once_when_a_sample_is_over_the_limit(self):
        entry(self.proc, 100, 1, 100, 50 * MIB // PAGE)
        killed = []
        guard = self.watchdog(40, killed)
        guard.sample()
        guard.sample()
        self.assertEqual(killed, [STOP, KILL])  # once: the group is frozen, then killed after the sweep
        self.assertEqual(guard.exceeded, 50 * MIB)
        self.assertEqual(guard.exceeded_message(), "worker exceeded its memory limit: peak 50 MiB over 40 MiB")

    def test_under_the_limit_or_without_one_nothing_is_stopped(self):
        entry(self.proc, 100, 1, 100, 50 * MIB // PAGE)
        for limit in (50, 51, None, 0):
            killed = []
            self.watchdog(limit, killed).sample()
            self.assertEqual(killed, [], limit)

    def test_a_failed_sample_never_stops_anything(self):
        killed = []
        guard = self.watchdog(1, killed)
        self.assertIsNone(guard.sample())
        self.assertEqual((killed, guard.samples, guard.footprint()), ([], 0, None))

    def test_footprint_keeps_the_peak_and_the_cpu_high_water_mark(self):
        killed = []
        guard = self.watchdog(500, killed)
        entry(self.proc, 100, 1, 100, 30 * MIB // PAGE, cpu_ticks=300)
        guard.sample()
        (self.proc / "100" / "statm").write_text(f"1 {10 * MIB // PAGE} 0 0 0 0 0\n")
        (self.proc / "100" / "stat").write_bytes(stat_text(100, 1, 100, cpu_ticks=100, comm="a"))
        guard.sample()
        self.assertEqual(
            guard.footprint(),
            {"peak_rss_mb": 30.0, "cpu_seconds": 3.0, "samples": 2, "limit_mb": 500},
        )

    def test_the_sampling_thread_stops_the_worker_and_ends(self):
        entry(self.proc, 100, 1, 100, 50 * MIB // PAGE)
        stopped = threading.Event()

        def group_killed(sig):
            if sig == KILL:
                stopped.set()

        guard = Watchdog(
            100,
            10,
            group_killed,
            interval=0.01,
            proc_root=self.proc,
            page_size=PAGE,
            clock_ticks=TICKS,
            kill=FakeKernel(self.proc),
        )
        guard.start()
        self.assertTrue(stopped.wait(10))
        guard.stop()
        self.assertFalse(guard._thread.is_alive())

    def test_a_pid_reused_after_the_first_sample_is_not_measured_or_stopped(self):
        entry(self.proc, 100, 1, 100, 10 * MIB // PAGE, started=1000)
        killed = []
        guard = self.watchdog(40, killed)
        self.assertIsNotNone(guard.sample())
        shutil.rmtree(self.proc / "100")
        entry(self.proc, 100, 1, 100, 500 * MIB // PAGE, started=5000)  # a stranger got the number
        self.assertIsNone(guard.sample())
        self.assertEqual((killed, guard.exceeded, guard.samples), ([], None, 1))

    def test_a_guard_that_never_sees_its_worker_says_so_once(self):
        err = io.StringIO()
        guard = self.watchdog(40, [])
        with redirect_stderr(err):
            for _ in range(4):
                guard.sample()
        self.assertEqual(
            err.getvalue().splitlines(),
            [
                (
                    "factory: memory guard cannot read this worker's process tree; "
                    "the session runs without a memory limit or footprint record"
                )
            ],
        )

    def test_a_worker_that_was_seen_and_then_exited_is_not_a_blind_guard(self):
        entry(self.proc, 100, 1, 100, 1)
        err = io.StringIO()
        guard = self.watchdog(40, [])
        with redirect_stderr(err):
            guard.sample()
            shutil.rmtree(self.proc / "100")
            for _ in range(4):
                guard.sample()
        self.assertEqual(err.getvalue(), "")

    def test_start_without_proc_runs_unlimited_and_says_so_once(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertIsNone(memwatch.start(100, 40, lambda sig: None, proc_root=self.proc))
            self.assertIsNone(memwatch.start(100, 40, lambda sig: None, proc_root=self.proc))
        self.assertEqual(
            err.getvalue().splitlines(),
            [
                (
                    "factory: memory guard unavailable: /proc is not readable; "
                    "sessions run without a memory limit or footprint record"
                )
            ],
        )


class SweepTests(ProcCase):
    """What the watchdog does to the tree once a sample is over the limit (fake /proc, fake kernel)."""

    def setUp(self):
        super().setUp()
        patcher = patch.object(memwatch, "KILL_POLL_SECONDS", 0.001)
        patcher.start()
        self.addCleanup(patcher.stop)

    def escapes(self):
        entry(self.proc, 100, 1, 100, BIG)  # the worker, a session leader
        entry(self.proc, 101, 100, 100, 1)  # a child in its group
        entry(self.proc, 102, 101, 102, 1)  # setsid: own session and group
        entry(self.proc, 103, 102, 103, 1)  # and again, one level further
        entry(self.proc, 104, 1, 102, 1)  # an orphan of 102's session (its parent died)
        entry(self.proc, 200, 1, 200, 1)  # a stranger

    def test_every_process_of_the_tree_is_killed_one_by_one_and_nothing_else(self):
        self.escapes()
        kernel = FakeKernel(self.proc)
        self.guard(kernel).sample()
        self.assertEqual(set(kernel.sent(KILL)), {100, 101, 102, 103, 104})
        self.assertNotIn(200, kernel.signalled())
        self.assertEqual(os.listdir(self.proc), ["200"])

    def test_the_group_is_frozen_before_anything_is_killed_and_killed_after_the_tree(self):
        self.escapes()
        order = []
        kernel = FakeKernel(self.proc, on_signal=lambda pid, sig: order.append(("tree", sig)))
        self.guard(kernel, on_exceed=lambda sig: order.append(("group", sig))).sample()
        self.assertEqual(order[0], ("group", STOP))
        self.assertEqual(order[-1], ("group", KILL))
        self.assertEqual([entry for entry in order if entry[0] == "group"], [("group", STOP), ("group", KILL)])
        self.assertLess(order.index(("tree", STOP)), order.index(("tree", KILL)))
        self.assertLess(order.index(("tree", KILL)), order.index(("group", KILL)))

    def test_the_group_is_still_killed_when_the_sweep_cannot_read_proc(self):
        self.escapes()
        real, scans, order = memwatch._process_table, [], []

        def table(root):
            scans.append(root)
            return real(root) if len(scans) == 1 else None  # the sample, then nothing

        kernel = FakeKernel(self.proc)
        with patch.object(memwatch, "_process_table", table):
            self.guard(kernel, on_exceed=order.append).sample()
        self.assertEqual(order, [STOP, KILL])
        self.assertEqual(kernel.calls, [])

    def test_processes_are_frozen_before_any_is_killed(self):
        self.escapes()
        kernel = FakeKernel(self.proc)
        self.guard(kernel).sample()
        signals = [sig for _, sig in kernel.calls]
        self.assertLess(max(i for i, sig in enumerate(signals) if sig == STOP), signals.index(KILL))
        self.assertEqual(sorted(kernel.sent(STOP)), sorted(kernel.sent(KILL)))

    def test_what_the_tree_spawns_while_it_is_being_stopped_is_killed_too(self):
        self.escapes()
        spawned = []

        def fork_before_the_freeze_lands(pid, sig):
            if sig == STOP and pid == 101 and 105 not in spawned:
                spawned.append(105)
                entry(self.proc, 105, 101, 105, 1)  # a child 101 forked first, in its own session
            if sig == STOP and pid == 105 and 106 not in spawned:
                spawned.append(106)
                entry(self.proc, 106, 105, 106, 1)  # and a grandchild of that one

        kernel = FakeKernel(self.proc, on_signal=fork_before_the_freeze_lands)
        self.guard(kernel).sample()
        self.assertEqual(set(kernel.sent(KILL)), {100, 101, 102, 103, 104, 105, 106})
        signals = [sig for _, sig in kernel.calls]
        self.assertEqual(sorted(kernel.sent(STOP)), sorted(kernel.sent(KILL)))
        self.assertLess(max(i for i, sig in enumerate(signals) if sig == STOP), signals.index(KILL))

    def test_a_process_spawned_between_two_kill_passes_is_found_by_the_next_one(self):
        self.escapes()
        kills = []

        def after_the_first_kill(pid, sig):
            if sig == KILL:
                kills.append(pid)
                if len(kills) == 1:
                    entry(self.proc, 108, 102, 108, 1)  # 102 forked it before it was killed
                if kills.count(102) == 2:
                    kernel.immortal.discard(102)  # 102 takes two passes to die

        kernel = FakeKernel(self.proc, on_signal=after_the_first_kill)
        kernel.immortal.add(102)
        self.guard(kernel).sample()
        self.assertIn(108, kernel.sent(KILL))
        self.assertEqual(os.listdir(self.proc), ["200"])

    def test_a_reused_pid_is_never_signalled_but_a_new_child_at_a_dead_members_number_is(self):
        entry(self.proc, 100, 1, 100, BIG, started=1000)
        entry(self.proc, 101, 1, 101, 1, started=2000)  # the number 101 now belongs to another process
        entry(self.proc, 106, 101, 106, 1, started=2100)  # that process's child
        entry(self.proc, 107, 1, 101, 1, started=2200)  # a process in that process's session
        entry(self.proc, 301, 1, 300, 1, started=1500)  # an orphan of the session led by member 300 (gone)
        entry(self.proc, 500, 100, 100, 1, started=3000)  # a new child of the worker, at a dead member's number
        kernel = FakeKernel(self.proc)
        members = ((100, 1000), (101, 1000), (300, 1000), (500, 1000))
        self.guard(kernel)._kill_tree(Sample(0, 0.0, 0, members))
        self.assertEqual(set(kernel.sent(KILL)), {100, 301, 500})
        self.assertEqual(kernel.identities, {(100, 1000), (301, 1500), (500, 3000)})

    def test_pid_1_this_process_and_its_ancestors_are_never_signalled_or_adopted(self):
        me, parent = OWN
        entry(self.proc, 100, 1, 100, BIG)
        entry(self.proc, me, parent, 100, 1)  # in the worker's session: cannot happen for real
        entry(self.proc, parent, 1, 100, 1)  # nor can its parent (the manager, say)
        entry(self.proc, 1, 0, 100, 1)  # nor init
        entry(self.proc, 500, me, me, 1)  # a child of this process (another worker, say)
        kernel = FakeKernel(self.proc)
        self.guard(kernel).sample()
        self.assertEqual(kernel.signalled(), {100})

    def test_the_sweep_is_bounded_and_never_leaves_a_process_frozen(self):
        self.escapes()
        kernel = FakeKernel(self.proc)
        kernel.immortal.add(102)  # uninterruptible sleep, say
        err = io.StringIO()
        start = time.monotonic()
        with redirect_stderr(err):
            self.guard(kernel, timeout=0.2).sample()
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual(
            err.getvalue().splitlines(),
            ["factory: memory guard could not stop every process of an over-limit worker"],
        )
        self.assertEqual(kernel.sent(STOP).count(102), 1)
        self.assertGreater(kernel.sent(KILL).count(102), 1)  # it kept trying until time ran out
        self.assertEqual([sig for pid, sig in kernel.calls if pid == 102][-1], KILL)

    def test_an_error_while_sweeping_still_unfreezes_what_was_stopped(self):
        self.escapes()
        failures = []

        def fail_once(pid, sig):
            if sig == KILL and not failures:
                failures.append(pid)
                raise RuntimeError("signal failed")

        kernel = FakeKernel(self.proc, on_signal=fail_once)
        with self.assertRaises(RuntimeError):
            self.guard(kernel).sample()
        self.assertEqual(set(kernel.sent(KILL)), {100, 101, 102, 103, 104})

    def test_a_failing_group_signal_does_not_skip_the_sweep(self):
        self.escapes()

        attempts = []

        def broken(sig):
            attempts.append(sig)
            raise RuntimeError("group signal failed")

        kernel = FakeKernel(self.proc)
        with self.assertRaises(RuntimeError):
            self.guard(kernel, on_exceed=broken).sample()
        self.assertEqual(set(kernel.sent(KILL)), {100, 101, 102, 103, 104})
        self.assertEqual(attempts, [STOP, KILL])  # the group is still sent its last SIGKILL

    def test_when_proc_becomes_unreadable_mid_sweep_what_was_frozen_is_still_killed(self):
        self.escapes()
        real, scans = memwatch._process_table, []

        def table(root):
            scans.append(root)
            return real(root) if len(scans) <= 2 else None  # the sample, one sweep scan, then nothing

        kernel = FakeKernel(self.proc)
        with patch.object(memwatch, "_process_table", table):
            self.guard(kernel).sample()
        self.assertEqual(set(kernel.sent(STOP)), {100, 101, 102, 103, 104})
        self.assertEqual(set(kernel.sent(KILL)), set(kernel.sent(STOP)))

    def test_a_fake_proc_can_never_reach_a_real_process(self):
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)
        entry(self.proc, sleeper.pid, 1, sleeper.pid, BIG)
        guard = Watchdog(
            sleeper.pid, 1, lambda sig: None, proc_root=self.proc, page_size=PAGE, clock_ticks=TICKS, kill_timeout=0
        )
        with redirect_stderr(io.StringIO()):
            guard.sample()
        self.assertIsNotNone(guard.exceeded)
        self.assertIsNone(sleeper.poll())


class KillPidTests(unittest.TestCase):
    """The real signal path: identity check, pidfd, and what it refuses."""

    def setUp(self):
        patcher = patch.object(capacity, "_NOTICED", set())
        patcher.start()
        self.addCleanup(patcher.stop)

    def sleeper(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        return process, memwatch._stat_fields("/proc", process.pid).started

    def test_signals_a_process_whose_start_time_matches(self):
        process, started = self.sleeper()
        self.assertTrue(memwatch.kill_pid(process.pid, started))
        self.assertEqual(process.wait(timeout=10), -signal.SIGKILL)

    def test_a_different_start_time_means_the_pid_was_reused_and_nothing_is_sent(self):
        process, started = self.sleeper()
        self.assertFalse(memwatch.kill_pid(process.pid, started + 1))
        self.assertIsNone(process.poll())

    def test_without_pidfd_the_start_time_is_still_checked(self):
        process, started = self.sleeper()
        unsupported = OSError(errno.ENOSYS, "Function not implemented")
        with patch.object(os, "pidfd_open", side_effect=unsupported, create=True):
            self.assertFalse(memwatch.kill_pid(process.pid, started + 1))
            self.assertIsNone(process.poll())
            self.assertTrue(memwatch.kill_pid(process.pid, started))
        self.assertEqual(process.wait(timeout=10), -signal.SIGKILL)

    def test_a_filtered_pidfd_signal_falls_back_to_the_plain_one_with_the_same_check(self):
        process, started = self.sleeper()
        filtered = PermissionError(errno.EPERM, "Operation not permitted")
        with patch.object(signal, "pidfd_send_signal", side_effect=filtered, create=True):
            self.assertFalse(memwatch.kill_pid(process.pid, started + 1))
            self.assertIsNone(process.poll())
            self.assertTrue(memwatch.kill_pid(process.pid, started))
        self.assertEqual(process.wait(timeout=10), -signal.SIGKILL)

    def test_init_this_process_and_its_parent_are_refused_before_any_syscall(self):
        with (
            patch.object(os, "kill") as kill,
            patch.object(os, "pidfd_open", create=True) as opener,
        ):
            for pid in (0, 1, os.getpid(), os.getppid()):
                self.assertFalse(memwatch.kill_pid(pid, 1), pid)
        kill.assert_not_called()
        opener.assert_not_called()

    def test_a_process_that_is_already_gone_is_not_signalled(self):
        process, started = self.sleeper()
        process.kill()
        process.wait(timeout=10)
        self.assertFalse(memwatch.kill_pid(process.pid, started))

    def test_a_process_that_may_not_be_signalled_is_reported_once_without_values(self):
        process, started = self.sleeper()
        denied = PermissionError(errno.EPERM, "Operation not permitted")
        err = io.StringIO()
        with (
            patch.object(signal, "pidfd_send_signal", side_effect=denied, create=True),
            patch.object(os, "kill", side_effect=denied),
            redirect_stderr(err),
        ):
            self.assertFalse(memwatch.kill_pid(process.pid, started))
            self.assertFalse(memwatch.kill_pid(process.pid, started))
        self.assertEqual(
            err.getvalue().splitlines(),
            ["factory: memory guard could not signal a worker process: Operation not permitted"],
        )
        self.assertIsNone(process.poll())


def read_lines(stream, count, timeout=20.0):
    """The lines read from a pipe until there are ``count`` of them, and whether it reached EOF.

    Every complete line of the last chunk is returned, so there can be more than ``count`` (a
    caller that needs "at least"). Never blocks past ``timeout``.
    """
    deadline = time.monotonic() + timeout
    lines, buffer, eof = [], b"", False
    while len(lines) < count:
        left = deadline - time.monotonic()
        if left <= 0 or not select.select([stream.fileno()], [], [], left)[0]:
            break
        chunk = os.read(stream.fileno(), 65536)
        if not chunk:
            eof = True
            break
        buffer = b"".join([buffer, chunk])
        *done, buffer = buffer.split(b"\n")
        lines.extend(line.decode() for line in done)
    return lines, eof


ESCAPER = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
print(child.pid, flush=True)
time.sleep(60)
"""

CHAIN = """
import os, time
def spawn(depth):
    if os.fork() == 0:
        os.setsid()
        print(os.getpid(), flush=True)
        if depth:
            spawn(depth - 1)
        time.sleep(60)
        os._exit(0)
spawn(5)
time.sleep(60)
"""

# Each child reports its pid with one write: a line from another child cannot land inside it.
FLOOD = """
import os, time
for _ in range(200):
    if os.fork() == 0:
        os.setsid()
        os.write(1, b"%d\\n" % os.getpid())
        time.sleep(30)
        os._exit(0)
    time.sleep(0.01)
time.sleep(30)
"""

# The worker is itself the forker, and its children detach: they are in the worker's group until
# they call setsid. Whatever it forks after a sample was read must still end up dead.
GROUP_FORKER = """
import os, time
for _ in range(2000):
    if os.fork() == 0:
        os.setsid()
        os.write(1, b"%d\\n" % os.getpid())
        time.sleep(30)
        os._exit(0)
    time.sleep(0.0005)
time.sleep(30)
"""

# A process that renames itself to bytes that are not UTF-8 (and hold a parenthesis).
RENAME = """
try:
    import ctypes
    renamed = ctypes.CDLL(None).prctl(15, b"\\xff\\xfe(\\xfd) ", 0, 0, 0) == 0
except (ImportError, OSError, AttributeError):
    renamed = False
import time
print("named" if renamed else "unsupported", flush=True)
time.sleep(60)
"""


@unittest.skipUnless(memwatch.supported(), "needs a Linux /proc")
class RealProcessTests(unittest.TestCase):
    """The watchdog against processes that really escape the worker's group."""

    def setUp(self):
        self.tracked, self.workers = [], []
        self.addCleanup(self.reap)
        patcher = patch.object(capacity, "_NOTICED", set())
        patcher.start()
        self.addCleanup(patcher.stop)

    def reap(self):
        for pid, started in self.tracked:
            memwatch.kill_pid(pid, started)
        for worker in self.workers:
            worker.kill()
            worker.wait(timeout=10)
            worker.stdout.close()

    def track(self, pid):
        stat = memwatch._stat_fields("/proc", pid)
        if stat is not None:
            self.tracked.append((pid, stat.started))

    def spawn(self, code):
        """A worker as run_agent starts it: its own session, stdout on a pipe the whole tree shares."""
        worker = subprocess.Popen(
            [sys.executable, "-c", code], stdout=subprocess.PIPE, start_new_session=True
        )
        self.workers.append(worker)
        self.track(worker.pid)
        return worker

    def trip(self, worker):
        guard = Watchdog(worker.pid, 1, lambda sig: os.killpg(worker.pid, sig))
        guard.sample()
        self.assertIsNotNone(guard.exceeded, "the sample did not see a worker over 1 MiB")
        return guard

    def test_a_descendant_in_its_own_session_is_killed_with_the_worker(self):
        worker = self.spawn(ESCAPER)
        lines, _ = read_lines(worker.stdout, 1)
        escaped = int(lines[0])
        self.track(escaped)
        self.assertNotEqual(os.getpgid(escaped), worker.pid)  # outside the worker's process group
        self.trip(worker)
        self.assertFalse(alive(escaped))
        worker.wait(timeout=10)

    def test_a_chain_of_descendants_each_in_a_session_of_its_own_is_killed(self):
        worker = self.spawn(CHAIN)
        lines, _ = read_lines(worker.stdout, 6)
        self.assertEqual(len(lines), 6)
        pids = [int(line) for line in lines]
        for pid in pids:
            self.track(pid)
        self.assertEqual(len({os.getsid(pid) for pid in pids}), 6)
        self.trip(worker)
        self.assertEqual([pid for pid in pids if alive(pid)], [])

    def test_a_process_that_keeps_forking_in_a_group_of_its_own_is_stopped_for_good(self):
        flood = f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-c', {FLOOD!r}], start_new_session=True)\ntime.sleep(60)\n"
        worker = self.spawn(flood)
        lines, _ = read_lines(worker.stdout, 15)
        self.assertGreaterEqual(len(lines), 15, "the forker did not get going")
        self.trip(worker)
        rest, eof = read_lines(worker.stdout, 10_000, timeout=10)
        self.assertTrue(eof, "something of the tree still holds the worker's stdout")
        pids = [int(line) for line in [*lines, *rest]]
        self.assertGreaterEqual(len(pids), 15)
        for pid in pids:
            self.track(pid)
        self.assertEqual([pid for pid in pids if alive(pid)], [])

    def test_children_that_detach_from_a_forking_worker_while_it_is_stopped_do_not_survive(self):
        worker = self.spawn(GROUP_FORKER)
        lines, _ = read_lines(worker.stdout, 300)
        self.assertGreaterEqual(len(lines), 300, "the forker did not get going")
        self.trip(worker)
        rest, eof = read_lines(worker.stdout, 10_000, timeout=10)
        self.assertTrue(eof, "something of the tree still holds the worker's stdout")
        pids = [int(line) for line in [*lines, *rest]]
        for pid in pids:
            self.track(pid)
        self.assertEqual([pid for pid in pids if alive(pid)], [])
        self.assertEqual(worker.wait(timeout=10), -signal.SIGKILL)

    def named(self, worker):
        lines, _ = read_lines(worker.stdout, 1)
        if lines != ["named"]:
            self.skipTest("this platform cannot give a process a name that is not UTF-8")
        data = Path(f"/proc/{worker.pid}/stat").read_bytes()
        if b"\xff\xfe" not in data:
            self.skipTest("/proc does not show the name the process was given")

    def test_a_worker_with_a_command_name_that_is_not_utf8_is_still_measured_and_stopped(self):
        worker = self.spawn(RENAME)
        self.named(worker)
        found = read_tree(worker.pid)
        self.assertIsNotNone(found)
        self.assertEqual(found.processes, 1)
        self.assertGreater(found.rss_bytes, MIB)
        self.trip(worker)
        self.assertEqual(worker.wait(timeout=10), -signal.SIGKILL)

    def test_a_descendant_with_a_command_name_that_is_not_utf8_is_summed_and_killed(self):
        parent = (
            f"import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, '-c', {RENAME!r}])\n"
            f"time.sleep(60)\n"
        )
        worker = self.spawn(parent)
        lines, _ = read_lines(worker.stdout, 1)
        if lines != ["named"]:
            self.skipTest("this platform cannot give a process a name that is not UTF-8")
        found = read_tree(worker.pid)
        self.assertEqual(found.processes, 2)
        pids = [pid for pid, _ in found.members]
        for pid in pids:
            self.track(pid)
        self.trip(worker)
        self.assertEqual([pid for pid in pids if alive(pid)], [])


if __name__ == "__main__":
    unittest.main()

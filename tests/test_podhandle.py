"""The additive pod handle (slice 5 of the pod backend): state, fail-closed recovery, stop paths, the registry.

Everything about the launcher is CONTRACT-TESTED AGAINST AN ASSUMED API: the launcher client of slice 11 does
not exist yet, so ``reader`` and ``terminate`` are injected callables and ``FakeLauncher`` below is a guess at
what the launcher will answer (NotFound, a pod phase, or no answer at all). No test calls Azure, Kubernetes or
Anthropic, and none starts a pod.
"""

import ast
import contextlib
import copy
import inspect
import io
import json
import signal
import tempfile
import threading
import time
import unittest
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from factory import locks, operations, podcontract, podhandle, recovery
from factory import runner as runner_module
from factory.purge import PurgeRefused, purge_run
from factory.runner import PendingStop, Runner
from factory.state import State, atomic_json, host_id, pid_status
from factory.status import format_status, status_report
from tests.test_factory import assignment
from tests.test_hostpid import OTHER_HOST, dead_pid, sleeper
from tests.test_locks import BlobTestCase

PLAN = {"jobs": [{"id": "a"}, {"id": "b"}]}
STATUS_PLAN = {
    "jobs": [
        {"id": "a", "likely_paths": ["src/a"], "depends_on": []},
        {"id": "b", "likely_paths": ["src/b"], "depends_on": []},
    ]
}
CREATED = "2026-11-09T10:00:00.000000Z"
SRC = Path(__file__).resolve().parent.parent / "src" / "factory"


def handle(number=1, launcher="launcher-1"):
    """A valid handle; the session id is the number as 32 hex digits, so every call is deterministic."""
    session = f"{number:032x}"
    return podhandle.PodHandle(
        name=podcontract.pod_name(session),
        uid=f"{number:08x}-2222-3333-4444-555555555555",
        session=session,
        launcher=launcher,
        created_at=CREATED,
    )


class LauncherDown(Exception):
    """The launcher did not answer (assumed: a refused connection or a timeout)."""


class FakeLauncher:
    """The ASSUMED launcher API: ``status`` answers a pod phase or NotFound, ``terminate`` deletes; both can be down.

    ``reader`` is the adapter slice 11 will provide, written here as the contract says: NotFound, a terminal
    phase and a pod with another uid are ``dead``; Pending and Running are ``alive``; a phase it does not know is
    ``unknown``; no answer raises.
    """

    def __init__(self):
        self.pods = {}  # pod name -> {"uid": ..., "phase": ...}
        self.down = False
        self.deleted = []

    def add(self, pod, phase="Running"):
        self.pods[pod.name] = {"uid": pod.uid, "phase": phase}

    def status(self, pod):
        if self.down:
            raise LauncherDown
        found = self.pods.get(pod.name)
        if found is None or found["uid"] != pod.uid:
            return "NotFound"
        return found["phase"]

    def reader(self, pod):
        phase = self.status(pod)
        if phase in {"NotFound", "Succeeded", "Failed"}:
            return "dead"
        if phase in {"Pending", "Running"}:
            return "alive"
        return "unknown"

    def terminate(self, pod):
        if self.down:
            raise LauncherDown
        self.deleted.append(pod.session)
        self.pods.pop(pod.name, None)  # NotFound is success


class StepClock:
    """A clock that reads ``start`` and then moves ``step`` forward on every reading (nothing really waits)."""

    def __init__(self, start=0.0, step=1.0):
        self.now, self.step = start, step

    def __call__(self):
        value = self.now
        self.now += self.step
        return value


class Blocker:
    """A terminator that blocks, once it has started, until the test releases it (a launcher that is slow)."""

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = []

    def __call__(self, pod):
        self.started.set()
        self.release.wait(10)  # a guard, never the synchronisation: the test releases it
        self.finished.append(pod.session)


@contextlib.contextmanager
def spy_joins(wait=0.02):
    """Record the timeout of every join of a pod-terminate thread; the real join waits only ``wait`` seconds."""
    recorded = []
    real = threading.Thread.join

    def join(self, timeout=None):
        if self.name != "factory-pod-terminate":
            return real(self, timeout)
        recorded.append(timeout)
        return real(self, wait)

    with patch.object(threading.Thread, "join", join):
        yield recorded


class GapMutex:
    """The state mutex with a hook after every OUTERMOST release: the gap in which a check made under it goes stale."""

    def __init__(self, gap):
        self.inner = threading.RLock()
        self.local = threading.local()
        self.gap = gap

    def __enter__(self):
        self.inner.acquire()
        self.local.depth = getattr(self.local, "depth", 0) + 1
        return self

    def __exit__(self, *exc):
        self.local.depth -= 1
        self.inner.release()
        if self.local.depth == 0:
            self.gap()
        return False


class PodCase(unittest.TestCase):
    def setUp(self):
        podhandle.reset()
        self.addCleanup(podhandle.reset)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.launcher = FakeLauncher()

    def state(self, plan=None):
        return State(self.directory, plan or PLAN)

    def reload(self, plan=None):
        return State(self.directory, plan or PLAN).data["jobs"]

    @contextlib.contextmanager
    def stderr(self):
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            yield captured


class PodHandleTests(unittest.TestCase):
    def test_a_valid_record_round_trips(self):
        record = handle(7).record()
        self.assertEqual(set(record), set(podhandle.FIELDS))
        self.assertEqual(podhandle.PodHandle.from_record(record), handle(7))
        self.assertEqual(podhandle.PodHandle.from_record(handle(7)), handle(7))
        record["name"] = "changed"
        self.assertEqual(handle(7).record()["name"], podcontract.pod_name(f"{7:032x}"))  # a fresh dict every call

    def test_the_name_must_be_the_contract_name_of_the_session(self):
        record = handle(1).record()
        for bad in ("kube-apiserver", podcontract.pod_name(f"{2:032x}"), "", "factory-w-"):
            with self.subTest(name=bad):
                with self.assertRaisesRegex(ValueError, "^invalid pod handle$"):
                    podhandle.PodHandle.from_record({**record, "name": bad})

    def test_every_other_field_is_checked_and_a_bad_record_is_never_echoed(self):
        record = handle(1).record()
        secret = "S3CRET-VALUE-NOT-TO-BE-ECHOED"
        bad_values = {
            "uid": ["", "has space", "../etc", f"{secret}\n", 7, None, "x" * 65],
            "session": ["", "ABC", "g" * 32, "a" * 31, 5],
            "launcher": ["", "a b", "-leading", None, ["x"]],
            "created_at": ["", "yesterday", "2026-11-09 10:00:00", None, 20261109],
        }
        for field, values in bad_values.items():
            for value in values:
                with self.subTest(field=field, value=repr(value)[:20]):
                    with self.assertRaises(ValueError) as caught:
                        podhandle.PodHandle.from_record({**record, field: value})
                    self.assertEqual(str(caught.exception), "invalid pod handle")

    def test_the_record_keys_are_a_closed_set(self):
        record = handle(1).record()
        missing = {key: value for key, value in record.items() if key != "uid"}
        for bad in (missing, {**record, "extra": "x"}, {}, [], "pod", 5, ("a",), None):
            with self.subTest(bad=repr(bad)[:30]):
                with self.assertRaises(ValueError):
                    podhandle.PodHandle.from_record(bad)

    def test_a_handle_is_immutable_hashable_and_compared_by_value(self):
        one = handle(1)
        with self.assertRaises(AttributeError):
            one.uid = "other"
        self.assertEqual(one, handle(1))
        self.assertEqual(len({one, handle(1), handle(2)}), 2)

    def test_label_names_the_session_or_a_fixed_phrase(self):
        self.assertEqual(podhandle.label(handle(3).record()), f"pod session {3:032x}")
        for bad in ({}, "SECRET-TEXT", {"name": "SECRET-TEXT"}):
            self.assertEqual(podhandle.label(bad), "pod session (unreadable record)")


class PodStatusTests(PodCase):
    def test_no_pod_is_dead_and_the_reader_is_not_asked(self):
        asked = []
        self.assertEqual(podhandle.pod_status(None, asked.append), "dead")
        self.assertEqual(asked, [])

    def test_without_a_reader_the_status_is_unknown_never_dead(self):
        self.assertEqual(podhandle.pod_status(handle(1).record()), "unknown")
        self.assertEqual(podhandle.pod_status(handle(1).record(), None), "unknown")

    def test_alive_and_dead_come_from_the_reader_and_it_receives_a_handle(self):
        seen = []

        def reader(pod):
            seen.append(pod)
            return "alive" if pod.session == handle(1).session else "dead"

        self.assertEqual(podhandle.pod_status(handle(1).record(), reader), "alive")
        self.assertEqual(podhandle.pod_status(handle(2), reader), "dead")
        self.assertEqual(seen, [handle(1), handle(2)])

    def test_a_reader_that_cannot_answer_is_unknown(self):
        def explode(pod):
            raise LauncherDown

        self.assertEqual(podhandle.pod_status(handle(1).record(), explode), "unknown")
        for answer in (None, "", "ALIVE", "Running", True, 1, ["alive"], {"alive": True}, "unknown"):
            with self.subTest(answer=repr(answer)):
                self.assertEqual(podhandle.pod_status(handle(1).record(), lambda pod, a=answer: a), "unknown")

    def test_a_corrupt_record_is_unknown_and_the_reader_is_never_asked_about_it(self):
        asked = []
        for bad in ({}, "pod", 0, {"name": "x"}, {**handle(1).record(), "name": "kube-apiserver"}, [1]):
            with self.subTest(bad=repr(bad)[:30]):
                self.assertEqual(podhandle.pod_status(bad, asked.append), "unknown")
        self.assertEqual(asked, [])

    def test_the_installed_reader_is_the_default_and_an_explicit_one_wins(self):
        podhandle.install(reader=lambda pod: "dead")
        self.assertEqual(podhandle.pod_status(handle(1).record()), "dead")
        self.assertEqual(podhandle.pod_status(handle(1).record(), lambda pod: "alive"), "alive")
        podhandle.reset()
        self.assertEqual(podhandle.pod_status(handle(1).record()), "unknown")

    def test_the_fake_launcher_contract_maps_to_the_three_answers(self):
        pod = handle(1)
        self.assertEqual(podhandle.pod_status(pod.record(), self.launcher.reader), "dead")  # NotFound
        for phase, expected in (
            ("Pending", "alive"),
            ("Running", "alive"),
            ("Succeeded", "dead"),
            ("Failed", "dead"),
            ("Unknown", "unknown"),
        ):
            with self.subTest(phase=phase):
                self.launcher.add(pod, phase)
                self.assertEqual(podhandle.pod_status(pod.record(), self.launcher.reader), expected)
        other = podhandle.PodHandle(**{**pod.record(), "uid": "99999999-2222-3333-4444-555555555555"})
        self.assertEqual(podhandle.pod_status(other.record(), self.launcher.reader), "dead")  # a replaced pod
        self.launcher.down = True
        self.assertEqual(podhandle.pod_status(pod.record(), self.launcher.reader), "unknown")


class StateRecordTests(PodCase):
    def test_record_pod_stores_the_handle_and_leaves_the_pid_fields_alone(self):
        state = self.state()
        state.update("a", status="running")
        before = dict(state.data["jobs"]["a"])
        state.record_pod("a", handle(1))
        job = self.reload()["a"]
        self.assertEqual(job["pod"], handle(1).record())
        self.assertEqual(set(job["pod"]), {"name", "uid", "session", "launcher", "created_at"})
        self.assertEqual({k: v for k, v in job.items() if k not in {"pod", "updated_at", "phase_events"}},
                         {k: v for k, v in before.items() if k not in {"updated_at", "phase_events"}})
        self.assertNotIn("pid", job)

    def test_record_pod_accepts_the_record_and_refuses_a_bad_one_before_writing(self):
        state = self.state()
        state.record_pod("a", handle(2).record())
        self.assertEqual(state.data["jobs"]["a"]["pod"], handle(2).record())
        written = (self.directory / "state.json").read_text()
        for bad in ({}, {**handle(1).record(), "uid": "bad uid"}, "pod", None):
            with self.subTest(bad=repr(bad)[:30]):
                with self.assertRaises(ValueError):
                    state.record_pod("b", bad)
        self.assertEqual((self.directory / "state.json").read_text(), written)

    def test_a_job_never_has_both_a_pid_and_a_pod(self):
        state = self.state()
        state.record_pid("a", 1234)
        with self.assertRaisesRegex(RuntimeError, "a process group is recorded"):
            state.record_pod("a", handle(1))
        self.assertNotIn("pod", self.reload()["a"])
        self.assertEqual(self.reload()["a"]["pid"], 1234)
        state.record_pod("b", handle(2))
        with self.assertRaisesRegex(RuntimeError, "a pod session is recorded"):
            state.record_pid("b", 4321)
        self.assertNotIn("pid", self.reload()["b"])
        self.assertEqual(self.reload()["b"]["pod"], handle(2).record())

    def test_a_second_different_pod_is_refused_and_the_same_one_is_not(self):
        state = self.state()
        state.record_pod("a", handle(1))
        state.record_pod("a", handle(1))
        with self.assertRaisesRegex(RuntimeError, "another pod session"):
            state.record_pod("a", handle(2))
        self.assertEqual(self.reload()["a"]["pod"], handle(1).record())

    def test_clear_pod_removes_it_and_is_a_no_op_for_a_job_that_has_none(self):
        state = self.state()
        state.record_pod("a", handle(1))
        state.clear_pod("a")
        self.assertIsNone(self.reload()["a"]["pod"])
        written = (self.directory / "state.json").read_text()
        state.clear_pod("a")
        state.clear_pod("b")  # never had one: no write, no new key
        self.assertEqual((self.directory / "state.json").read_text(), written)
        self.assertNotIn("pod", self.reload()["b"])

    def test_record_started_routes_a_pid_to_the_pid_and_a_handle_to_the_pod(self):
        state = self.state()
        state.record_started("a", 1234)
        self.assertEqual((state.data["jobs"]["a"]["pid"], state.data["jobs"]["a"]["pid_host"]), (1234, host_id()))
        state.record_started("a", None)
        self.assertEqual((state.data["jobs"]["a"]["pid"], state.data["jobs"]["a"]["pid_host"]), (None, None))
        self.assertNotIn("pod", state.data["jobs"]["a"])
        state.record_started("b", handle(1))
        state.record_started("b", handle(1).record())
        self.assertEqual(state.data["jobs"]["b"]["pod"], handle(1).record())
        self.assertNotIn("pid", state.data["jobs"]["b"])

    def test_end_session_forgets_the_pid_always_and_the_pod_only_when_it_is_known_to_be_gone(self):
        podhandle.install(reader=self.launcher.reader, terminate=self.launcher.terminate)
        state = self.state()
        state.record_pid("a", 1234)
        state.end_session("a")
        self.assertEqual((self.reload()["a"].get("pid"), self.reload()["a"].get("pod")), (None, None))
        self.assertEqual(self.launcher.deleted, [])  # a pid session never asks the launcher
        state.record_pod("b", handle(1))  # the launcher knows no such pod: dead
        state.end_session("b")
        self.assertIsNone(self.reload()["b"]["pod"])
        self.assertEqual(self.launcher.deleted, [])
        state.record_pod("b", handle(2))
        self.launcher.add(handle(2))  # still there: the terminator deletes it and confirms
        state.end_session("b")
        self.assertEqual((self.launcher.deleted, self.reload()["b"]["pod"]), ([handle(2).session], None))
        state.record_pod("b", handle(3))
        self.launcher.add(handle(3))
        self.launcher.down = True  # nothing can confirm: the record stays
        with self.stderr():
            state.end_session("b")
        self.assertEqual(self.reload()["b"]["pod"], handle(3).record())

    def race(self, first, second):
        """Run ``first`` and ``second`` on one job so that ``second`` runs in the gap right after ``first`` lets go of the mutex.

        With a check and a write that release the mutex between them (the old code) the gap is between the two,
        and both calls succeed; with one critical section it is after the write, and the second call must refuse.
        """
        state = self.state()
        rendezvous = threading.Barrier(2, timeout=5)
        second_done = threading.Event()
        gaps = []

        def gap():
            if threading.current_thread().name != "first" or gaps:
                return
            gaps.append(1)
            rendezvous.wait()  # the first call has let go of the mutex: now the second may run
            second_done.wait(5)  # and the first does not go on until the second has run to its end

        state.mutex = GapMutex(gap)
        outcomes = {}

        def call(name, action):
            try:
                action(state)
                outcomes[name] = "recorded"
            except RuntimeError:
                outcomes[name] = "refused"
            finally:
                if name == "second":
                    second_done.set()

        threads = [
            threading.Thread(target=call, args=(name, action), name=name, daemon=True)
            for name, action in (("first", first), ("second", second))
        ]
        threads[0].start()
        rendezvous.wait()
        threads[1].start()
        for thread in threads:
            thread.join(5)
        self.assertEqual([thread.is_alive() for thread in threads], [False, False])
        job = state.data["jobs"]["a"]
        return outcomes, job.get("pid"), job.get("pod")

    def test_a_racing_record_pod_cannot_slip_in_behind_a_record_pid(self):
        outcomes, pid, pod = self.race(lambda state: state.record_pid("a", 4242), lambda state: state.record_pod("a", handle(1)))
        self.assertEqual(outcomes, {"first": "recorded", "second": "refused"})
        self.assertEqual((pid, pod), (4242, None))

    def test_a_racing_record_pid_cannot_slip_in_behind_a_record_pod(self):
        outcomes, pid, pod = self.race(lambda state: state.record_pod("a", handle(1)), lambda state: state.record_pid("a", 4242))
        self.assertEqual(outcomes, {"first": "recorded", "second": "refused"})
        self.assertEqual((pid, pod), (None, handle(1).record()))

    def test_a_refusal_writes_nothing_and_runs_no_observer(self):
        state = self.state()
        seen = []
        state.observer = lambda key, previous, current: seen.append(key)
        state.record_pid("a", 1234)
        written = (self.directory / "state.json").read_text()
        seen.clear()
        with self.assertRaises(RuntimeError):
            state.record_pod("a", handle(1))
        self.assertEqual((seen, (self.directory / "state.json").read_text()), ([], written))
        state.record_pod("b", handle(2))
        self.assertEqual(seen, ["b"])  # the observers still run, after the write


class RecoverTests(PodCase):
    def running(self, state, key, pod=None):
        state.update(key, status="running")
        if pod is not None:
            state.record_pod(key, pod)

    def test_a_launcher_that_is_down_refuses_and_changes_nothing(self):
        state = self.state()
        self.running(state, "a", handle(1))
        self.launcher.add(handle(1))
        self.launcher.down = True
        for lease in (None, lambda: True):  # a lease says nothing about a pod
            with self.subTest(lease=lease):
                with self.assertRaises(podhandle.PodStillLive) as caught:
                    state.recover(holds_lease=lease, pod_reader=self.launcher.reader)
                self.assertEqual(caught.exception.status, "unknown")
                self.assertIn("cannot be verified", str(caught.exception))
                self.assertIn(handle(1).session, str(caught.exception))
                job = self.reload()["a"]
                self.assertEqual((job["status"], job["pod"]), ("running", handle(1).record()))
                self.assertNotIn("error", job)

    def test_no_reader_at_all_refuses_like_a_launcher_that_is_down(self):
        state = self.state()
        self.running(state, "a", handle(1))
        with self.assertRaises(podhandle.PodStillLive) as caught:
            state.recover()
        self.assertEqual(caught.exception.status, "unknown")
        self.assertEqual(self.reload()["a"]["status"], "running")

    def test_an_alive_pod_refuses_like_a_live_pid(self):
        state = self.state()
        self.running(state, "a", handle(1))
        self.launcher.add(handle(1), "Running")
        with self.assertRaisesRegex(podhandle.PodStillLive, "still alive; wait or stop it before resuming") as caught:
            state.recover(holds_lease=lambda: True, pod_reader=self.launcher.reader)
        self.assertEqual(caught.exception.status, "alive")
        self.assertIsInstance(caught.exception, RuntimeError)
        self.assertEqual(self.reload()["a"]["status"], "running")

    def test_an_unreadable_record_refuses_and_never_echoes_it(self):
        state = self.state()
        self.running(state, "a")
        state.update("a", pod={"name": "SECRET-TEXT"})
        with self.assertRaises(podhandle.PodStillLive) as caught:
            state.recover(pod_reader=self.launcher.reader)
        self.assertNotIn("SECRET-TEXT", str(caught.exception))
        self.assertEqual(self.reload()["a"]["status"], "running")

    def test_a_dead_pod_proceeds_and_marks_the_job_interrupted(self):
        for phase in ("NotFound", "Succeeded", "Failed"):
            with self.subTest(phase=phase):
                podhandle.reset()
                state = self.state()
                self.running(state, "a", handle(1))
                if phase != "NotFound":
                    self.launcher.add(handle(1), phase)
                self.assertEqual(state.recover(pod_reader=self.launcher.reader), ["a"])
                job = self.reload()["a"]
                self.assertEqual(job["status"], "interrupted")
                self.assertEqual((job["pod"], job["pid"], job["pid_host"]), (None, None, None))
                self.assertIn("Supervisor interrupted", job["error"])
                state.update("a", status="queued")
                self.launcher.pods.clear()

    def test_the_installed_reader_is_what_recover_uses_by_default(self):
        state = self.state()
        self.running(state, "a", handle(1))
        self.launcher.add(handle(1))
        podhandle.install(reader=self.launcher.reader)
        with self.assertRaises(podhandle.PodStillLive):
            state.recover()
        self.launcher.pods.clear()
        self.assertEqual(state.recover(), ["a"])

    def test_one_pod_that_cannot_be_read_stops_the_whole_pass_before_any_change(self):
        state = self.state()
        self.running(state, "a", handle(1))  # dead
        self.running(state, "b", handle(2))
        self.launcher.add(handle(2))
        with self.assertRaises(podhandle.PodStillLive):
            state.recover(pod_reader=self.launcher.reader)
        jobs = self.reload()
        self.assertEqual((jobs["a"]["status"], jobs["b"]["status"]), ("running", "running"))
        self.assertEqual(jobs["a"]["pod"], handle(1).record())

    def test_a_dead_pod_of_a_job_that_is_not_running_is_forgotten(self):
        state = self.state()
        state.update("a", status="failed", error="boom")
        state.record_pod("a", handle(1))
        self.assertEqual(state.recover(pod_reader=self.launcher.reader), [])
        job = self.reload()["a"]
        self.assertEqual((job["status"], job["error"], job["pod"]), ("failed", "boom", None))

    def test_a_pod_that_is_not_dead_blocks_even_a_job_that_is_not_running(self):
        state = self.state()
        state.update("a", status="failed", error="boom")
        state.record_pod("a", handle(1))
        self.launcher.add(handle(1))
        with self.assertRaises(podhandle.PodStillLive):
            state.recover(pod_reader=self.launcher.reader)

    def test_a_pid_job_next_to_a_pod_job_keeps_its_own_rules(self):
        child = sleeper(self)
        state = self.state()
        self.running(state, "a")
        state.record_pid("a", child.pid)
        self.running(state, "b", handle(1))
        with self.assertRaisesRegex(RuntimeError, rf"a: child process group {child.pid} is still alive"):
            state.recover(pod_reader=self.launcher.reader)


    def both_state(self, pid, pod=None):
        """A state file that records a pid AND a pod (a stale key left by a rollback, a hand edit): never written by this code."""
        data = {"version": 1, "jobs": {"a": {"status": "running", "attempt": 1, "pid": pid, "pid_host": host_id()}, "b": {"status": "queued", "attempt": 0}}}
        data["jobs"]["a"]["pod"] = (pod or handle(1)).record()
        atomic_json(self.directory / "state.json", data)
        return self.state()

    def test_a_state_file_with_both_a_pid_and_a_pod_loads_and_each_field_keeps_its_rule(self):
        child = sleeper(self)
        for pid, reader, expected in (
            (child.pid, self.launcher.reader, "child process group"),  # the pid is alive, the pod is dead
            (dead_pid(), None, "cannot be verified"),  # the pid is dead, the pod cannot be read
        ):
            with self.subTest(expected=expected):
                state = self.both_state(pid)
                with self.assertRaisesRegex(RuntimeError, expected):
                    state.recover(pod_reader=reader)
                job = self.reload()["a"]
                self.assertEqual((job["status"], job["pid"], job["pod"]), ("running", pid, handle(1).record()))

    def test_with_both_a_pid_and_a_pod_dead_recover_forgets_both(self):
        state = self.both_state(dead_pid())
        self.assertEqual(state.recover(pod_reader=self.launcher.reader), ["a"])
        job = self.reload()["a"]
        self.assertEqual((job["status"], job["pid"], job["pod"]), ("interrupted", None, None))


class CompatibilityTests(PodCase):
    """A state file written before this slice behaves exactly as before; one with a pod loads everywhere."""

    OLD_RUNNING = {
        "version": 1,
        "jobs": {
            "a": {
                "status": "running",
                "attempt": 1,
                "created_at": "2026-11-01T00:00:00Z",
                "updated_at": "2026-11-01T00:00:00Z",
                "status_started_at": "2026-11-01T00:00:00Z",
                "phase": "build-0",
                "worker_phase": "build",
                "pid": None,
                "pid_host": None,
            },
            "b": {"status": "queued", "attempt": 0, "created_at": "2026-11-01T00:00:00Z"},
        },
    }

    def write(self, data):
        atomic_json(self.directory / "state.json", data)

    def test_an_old_state_file_recovers_exactly_as_before_and_gains_no_pod_key(self):
        data = copy.deepcopy(self.OLD_RUNNING)
        data["jobs"]["a"].update(pid=dead_pid(), pid_host=host_id())
        self.write(data)
        state = self.state()
        self.assertEqual(state.recover(), ["a"])
        job = self.reload()["a"]
        self.assertEqual((job["status"], job["pid"], job["pid_host"]), ("interrupted", None, None))
        self.assertEqual(job["error"], "Supervisor interrupted. Inspect retained worktree then retry explicitly.")
        for key in ("a", "b"):
            self.assertNotIn("pod", self.reload()[key])  # nothing new was written for a job that never had a pod
        state.record_pid("a", 4242)
        state.end_session("a")  # a job that never had a pod: the pid is forgotten, no pod key appears
        self.assertEqual(self.reload()["a"]["pid"], None)
        self.assertNotIn("pod", self.reload()["a"])

    def test_an_old_state_file_with_a_live_pid_still_refuses_with_the_old_text(self):
        child = sleeper(self)
        data = copy.deepcopy(self.OLD_RUNNING)
        data["jobs"]["a"].update(pid=child.pid, pid_host=host_id())
        self.write(data)
        with self.assertRaisesRegex(RuntimeError, rf"a: child process group {child.pid} is still alive; wait or stop it"):
            self.state().recover(pod_reader=self.launcher.reader)
        data["jobs"]["a"].update(pid=child.pid, pid_host=OTHER_HOST)
        self.write(data)
        with self.assertRaisesRegex(RuntimeError, "recorded on another host.*recovery needs the repository lease"):
            self.state().recover(pod_reader=self.launcher.reader)
        self.assertEqual(self.state().recover(holds_lease=lambda: True, pod_reader=self.launcher.reader), ["a"])

    def pod_state(self):
        data = copy.deepcopy(self.OLD_RUNNING)
        data["jobs"]["a"]["pod"] = handle(1).record()
        data["jobs"]["a"]["pid"] = None
        self.write(data)
        return data

    def test_a_state_file_with_a_pod_loads_through_state_status_purge_and_recovery(self):
        self.pod_state()
        state = self.state()
        self.assertEqual(state.data["jobs"]["a"]["pod"], handle(1).record())
        report = status_report(STATUS_PLAN, state.data, pod_reader=self.launcher.reader)
        self.assertEqual(report["active_worker_count"], 0)  # the launcher says NotFound: dead
        json.dumps(report)
        with self.assertRaisesRegex(ValueError, "pod worker is still alive or cannot be verified"):
            recovery._context(Path("/nonexistent"), PLAN, state, "a")
        run = self.directory / "r1"
        run.mkdir()
        atomic_json(run / "plan.json", PLAN)
        atomic_json(run / "state.json", state.data)
        with self.assertRaisesRegex(PurgeRefused, "cannot be verified"):
            purge_run(self.directory / "no-checkout", run)

    def test_the_previous_image_sees_no_pid_and_marks_the_running_job_interrupted(self):
        """Rollback. The previous readers never look at ``pod``: they see ``pid`` None, which is ``dead``.

        The image itself is not run here (UNVERIFIED): this pins the input its recovery decides on, and the test
        below pins that no reader in this tree subscripts the new key either.
        """
        job = self.pod_state()["jobs"]["a"]
        self.assertEqual(pid_status(job.get("pid"), job.get("pid_host")), "dead")

    def test_no_reader_indexes_the_pod_key_so_an_absent_key_can_never_raise(self):
        offenders = []
        for path in sorted(SRC.glob("*.py")):
            if path.name == "podcontract.py":  # its ``env`` table has a key of that name; it is not job state
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and node.slice.value == "pod":
                    offenders.append(f"{path.name}:{node.lineno}")
        self.assertEqual(offenders, [])


class StatusTests(PodCase):
    def report(self, job, reader=None):
        data = {"version": 1, "jobs": {"a": job, "b": {"status": "queued", "attempt": 0}}}
        return status_report(STATUS_PLAN, data, pod_reader=reader)

    def running_pod_job(self, **extra):
        return {"status": "running", "attempt": 1, "phase": "build-0", "pod": handle(1).record(), **extra}

    def test_an_alive_pod_is_a_live_worker(self):
        self.launcher.add(handle(1))
        report = self.report(self.running_pod_job(), self.launcher.reader)
        self.assertEqual(report["active_workers"], {"build": 1})
        self.assertEqual((report["active_worker_count"], report["active_worker_pods_unknown"]), (1, 0))
        self.assertTrue(report["jobs"][0]["active_worker"])

    def test_a_pod_that_cannot_be_read_counts_as_live_and_as_unknown(self):
        self.launcher.down = True
        for reader in (self.launcher.reader, None):  # a launcher that is down, and no reader at all
            with self.subTest(reader=reader):
                report = self.report(self.running_pod_job(), reader)
                self.assertEqual((report["active_worker_count"], report["active_worker_pods_unknown"]), (1, 1))
                self.assertIn("Pod workers that cannot be read: 1", format_status(report))
                json.dumps(report)

    def test_a_dead_pod_is_not_a_live_worker(self):
        report = self.report(self.running_pod_job(), self.launcher.reader)
        self.assertEqual((report["active_worker_count"], report["active_worker_pods_unknown"]), (0, 0))
        self.assertNotIn("cannot be read", format_status(report))

    def test_a_job_with_both_a_pid_and_a_pod_is_live_when_either_one_is(self):
        both = self.running_pod_job(pid=99, pid_host=None)
        for pid_alive, pod_reader, live in (
            (True, self.launcher.reader, 1),  # the pod is dead, the process group is not: still a live worker
            (False, self.launcher.reader, 0),
            (False, None, 1),  # the pod cannot be read
        ):
            with self.subTest(pid_alive=pid_alive, reader=pod_reader), patch("factory.state.alive", return_value=pid_alive):
                self.assertEqual(self.report(both, pod_reader)["active_worker_count"], live)
        self.launcher.add(handle(1))
        with patch("factory.state.alive", return_value=False):
            self.assertEqual(self.report(both, self.launcher.reader)["active_worker_count"], 1)

    def test_the_janitor_treats_an_unreadable_pod_as_a_live_worker(self):
        report = self.report(self.running_pod_job())
        self.assertTrue(operations.has_live_worker(report))

    def test_a_pod_on_a_job_that_is_not_running_is_not_read(self):
        asked = []
        report = self.report({"status": "failed", "attempt": 1, "pod": handle(1).record()}, asked.append)
        self.assertEqual((report["active_worker_count"], report["active_worker_pods_unknown"]), (0, 0))
        self.assertEqual(asked, [])

    def test_a_pid_job_is_unchanged_and_the_new_counter_is_zero(self):
        for pid, alive in ((99, True), (99, False)):
            with patch("factory.state.alive", return_value=alive):
                report = self.report({"status": "running", "phase": "build-0", "pid": pid})
            self.assertEqual(report["active_worker_count"], 1 if alive else 0)
            self.assertEqual(report["active_worker_pods_unknown"], 0)
        self.assertEqual(self.report({"status": "running", "worker_phase": "build"})["active_worker_count"], 1)
        self.assertNotIn("cannot be read", format_status(self.report({"status": "queued", "attempt": 0})))


class PurgeTests(PodCase):
    def run_directory(self, pod):
        state = self.state()
        state.update("a", status="running")
        if pod is not None:
            state.record_pod("a", pod)
        run = self.directory / "runs" / "r1"
        run.mkdir(parents=True)
        atomic_json(run / "plan.json", PLAN)
        atomic_json(run / "state.json", state.data)
        return run

    def purge(self, run, **kwargs):
        return purge_run(self.directory / "no-checkout", run, **kwargs)

    def test_purge_refuses_while_a_pod_is_alive_or_cannot_be_read_and_deletes_nothing(self):
        run = self.run_directory(handle(1))
        self.launcher.add(handle(1))
        podhandle.install(reader=self.launcher.reader)
        for lease in (None, lambda: True):
            with self.subTest(state="alive", lease=lease):
                with self.assertRaisesRegex(PurgeRefused, "still alive; wait or stop it before purging"):
                    self.purge(run, holds_lease=lease)
        self.launcher.down = True
        for lease in (None, lambda: True):
            with self.subTest(state="unknown", lease=lease):
                with self.assertRaisesRegex(PurgeRefused, "cannot be verified.*not purging until it can"):
                    self.purge(run, holds_lease=lease)
        podhandle.reset()  # no reader at all
        with self.assertRaisesRegex(PurgeRefused, "cannot be verified"):
            self.purge(run)
        self.assertTrue((run / "state.json").is_file())

    def test_purge_removes_the_run_once_the_pod_is_dead(self):
        run = self.run_directory(handle(1))
        podhandle.install(reader=self.launcher.reader)  # NotFound
        self.assertTrue(self.purge(run)["removed_run_dir"])
        self.assertFalse(run.exists())

    def test_a_run_without_a_pod_purges_as_before(self):
        run = self.run_directory(None)
        self.assertTrue(self.purge(run)["removed_run_dir"])


class RecoveryContextTests(PodCase):
    def context(self, status="pr_incomplete", execution=False):
        state = self.state()
        state.update("a", status=status)
        state.record_pod("a", handle(1))
        return lambda: recovery._context(Path("/nonexistent"), PLAN, state, "a", execution=execution)

    def test_a_pod_that_is_alive_or_cannot_be_read_refuses_the_resume(self):
        call = self.context()
        podhandle.install(reader=self.launcher.reader)
        self.launcher.add(handle(1))
        with self.assertRaisesRegex(ValueError, "a: pod worker is still alive or cannot be verified"):
            call()
        self.launcher.down = True
        with self.assertRaisesRegex(ValueError, "pod worker is still alive or cannot be verified"):
            call()
        podhandle.reset()
        with self.assertRaisesRegex(ValueError, "pod worker is still alive or cannot be verified"):
            call()

    def test_a_dead_pod_lets_the_checks_go_on(self):
        call = self.context()
        podhandle.install(reader=self.launcher.reader)
        with self.assertRaisesRegex(ValueError, "job has no retained worktree and branch"):
            call()

    def test_the_execution_mode_looks_only_at_the_job_it_is_about(self):
        state = self.state()
        state.update("a", status="pr_incomplete")
        state.update("b", status="failed")
        state.record_pod("b", handle(2))
        podhandle.install(reader=lambda pod: "alive")
        with self.assertRaisesRegex(ValueError, "job has no retained worktree and branch"):
            recovery._context(Path("/nonexistent"), PLAN, state, "a", execution=True)
        with self.assertRaisesRegex(ValueError, "b: pod worker is still alive"):
            recovery._context(Path("/nonexistent"), PLAN, state, "a", execution=False)


def fake_runner(jobs, term_wait=5.0):
    """The part of a Runner that the stop and kill paths read: its state and the SIGTERM wait."""
    return SimpleNamespace(state=SimpleNamespace(data={"jobs": jobs}), stop_term_wait=term_wait)


class StopTests(PodCase):
    def setUp(self):
        super().setUp()
        self.terminated = []

        def terminate(pod):
            self.terminated.append(pod.session)
            self.launcher.terminate(pod)

        self.terminate = terminate
        podhandle.install(reader=self.launcher.reader, terminate=terminate)

    def stop(self, jobs, pending, now, term_wait=5.0):
        Runner.enforce_stop(fake_runner(jobs, term_wait), "a", pending, now)

    def test_a_pod_is_terminated_at_the_sigterm_step_and_again_at_the_sigkill_step(self):
        jobs = {"a": {"status": "running", "pod": handle(1).record()}}
        pending = PendingStop({}, deadline=10.0)
        self.stop(jobs, pending, 9.0)
        self.assertEqual((self.terminated, pending.terminated_at), ([], None))  # the grace period is not over
        self.stop(jobs, pending, 10.0)
        self.assertEqual((self.terminated, pending.terminated_at), ([handle(1).session], 10.0))
        self.stop(jobs, pending, 14.9)  # the SIGTERM wait is not over
        self.assertEqual(self.terminated, [handle(1).session])
        self.stop(jobs, pending, 15.0)
        self.assertEqual(self.terminated, [handle(1).session] * 2)
        self.assertTrue(pending.killed)
        self.stop(jobs, pending, 99.0)  # nothing after the kill step
        self.assertEqual(self.terminated, [handle(1).session] * 2)

    def test_a_pod_is_terminated_without_being_probed_first(self):
        podhandle.install(reader=lambda pod: self.fail("the stop path must not probe"), terminate=self.terminate)
        jobs = {"a": {"status": "running", "pod": handle(1).record()}}
        self.stop(jobs, PendingStop({}, deadline=0.0), 1.0)
        self.assertEqual(self.terminated, [handle(1).session])

    def test_a_launcher_that_is_down_is_reported_and_the_second_step_tries_again(self):
        jobs = {"a": {"status": "running", "pod": handle(1).record()}}
        pending = PendingStop({}, deadline=0.0)
        self.launcher.down = True
        with self.stderr() as captured:
            self.stop(jobs, pending, 1.0)
            self.launcher.down = False
            self.stop(jobs, pending, 6.0)
        self.assertIn(f"factory: could not terminate pod session {handle(1).session}: LauncherDown", captured.getvalue())
        self.assertEqual(self.terminated, [handle(1).session] * 2)
        self.assertEqual(self.launcher.deleted, [handle(1).session])  # the retry got through

    def test_a_pid_job_still_gets_sigterm_then_sigkill_to_its_group(self):
        jobs = {"a": {"status": "running", "pid": 4242, "pid_host": None}}
        pending, signals = PendingStop({}, deadline=1.0), []
        with (
            patch.object(runner_module, "pid_status", return_value="alive"),
            patch.object(runner_module, "signal_group", lambda pid, sig=signal.SIGKILL: signals.append((pid, sig))),
        ):
            self.stop(jobs, pending, 1.0)
            self.stop(jobs, pending, 5.9)
            self.stop(jobs, pending, 6.0)
        self.assertEqual(signals, [(4242, signal.SIGTERM), (4242, signal.SIGKILL)])
        self.assertEqual(self.terminated, [])

    def test_a_job_with_no_worker_is_left_alone(self):
        for job in ({"status": "running"}, {"status": "running", "pid": None, "pod": None}, {}):
            with self.subTest(job=job):
                pending = PendingStop({}, deadline=0.0)
                self.stop({"a": job}, pending, 100.0)
                self.assertEqual((self.terminated, pending.terminated_at), ([], None))

    def test_kill_children_terminates_a_recorded_pod_whatever_the_reader_says(self):
        self.launcher.add(handle(1))
        jobs = {
            "a": {"pod": handle(1).record()},
            "b": {"pod": handle(2).record()},  # the launcher has never heard of it: terminate is idempotent
            "c": {"pid": None},
            "d": {"pod": None},
        }
        Runner.kill_children(fake_runner(jobs))
        self.assertEqual(sorted(self.terminated), sorted([handle(1).session, handle(2).session]))
        self.assertEqual(self.launcher.pods, {})

    def test_kill_children_still_kills_process_groups_exactly_as_before(self):
        jobs = {"a": {"pid": 11, "pid_host": None}, "b": {"pod": handle(1).record()}}
        killed = []
        with (
            patch.object(runner_module, "pid_status", return_value="alive"),
            patch.object(runner_module, "kill_group", killed.append),
        ):
            Runner.kill_children(fake_runner(jobs))
        self.assertEqual(killed, [11])
        self.assertEqual(self.terminated, [handle(1).session])

    def test_an_unreadable_pod_record_is_reported_and_does_not_stop_the_others(self):
        jobs = {"a": {"pod": {"name": "SECRET-TEXT"}}, "b": {"pod": handle(2).record()}}
        with self.stderr() as captured:
            Runner.kill_children(fake_runner(jobs))
        self.assertEqual(self.terminated, [handle(2).session])
        self.assertIn("unreadable record", captured.getvalue())
        self.assertNotIn("SECRET-TEXT", captured.getvalue())

    def test_the_abort_path_terminates_pods_even_when_a_process_group_cannot_be_signalled(self):
        jobs = {"a": {"pid": 11, "pid_host": None}, "b": {"pod": handle(1).record()}}
        with (
            patch.object(runner_module, "pid_status", return_value="alive"),
            patch("os.killpg", side_effect=ProcessLookupError),
            self.assertRaises(ProcessLookupError),
        ):
            Runner.abort_workers(fake_runner(jobs))
        self.assertEqual(self.terminated, [handle(1).session])

    def test_the_abort_path_signals_groups_as_before_and_terminates_pods(self):
        jobs = {"a": {"pid": 11, "pid_host": None}, "b": {"pod": handle(1).record()}, "c": {}}
        signalled = []
        with (
            patch.object(runner_module, "pid_status", return_value="alive"),
            patch("os.killpg", lambda pid, sig: signalled.append((pid, sig))),
        ):
            Runner.abort_workers(fake_runner(jobs))
        self.assertEqual(signalled, [(11, signal.SIGTERM)])
        self.assertEqual(self.terminated, [handle(1).session])

    def both(self):
        """A job that records a pid AND a pod: never written by this code, so every consumer must fail closed on it."""
        return {"a": {"status": "running", "pid": 4242, "pid_host": None, "pod": handle(1).record()}}

    def test_send_stop_acts_on_the_pod_and_on_the_group_when_given_both(self):
        signals = []
        with patch.object(runner_module, "signal_group", lambda pid, sig=signal.SIGKILL: signals.append((pid, sig))):
            runner_module.send_stop(4242, handle(1).record(), signal.SIGTERM)
            self.assertEqual((signals, self.terminated), ([(4242, signal.SIGTERM)], [handle(1).session]))
            runner_module.send_stop(None, handle(1).record(), signal.SIGKILL)  # a pod alone: no group is signalled
            runner_module.send_stop(4242, None, signal.SIGKILL)  # a pid alone, exactly as before
        self.assertEqual(signals, [(4242, signal.SIGTERM), (4242, signal.SIGKILL)])
        self.assertEqual(self.terminated, [handle(1).session] * 2)

    def test_a_job_with_both_is_stopped_through_both_at_each_step(self):
        jobs, pending, signals = self.both(), PendingStop({}, deadline=1.0), []
        with (
            patch.object(runner_module, "pid_status", return_value="alive"),
            patch.object(runner_module, "signal_group", lambda pid, sig=signal.SIGKILL: signals.append((pid, sig))),
        ):
            self.stop(jobs, pending, 1.0)
            self.assertEqual((signals, self.terminated), ([(4242, signal.SIGTERM)], [handle(1).session]))
            self.stop(jobs, pending, 6.0)
        self.assertEqual(signals, [(4242, signal.SIGTERM), (4242, signal.SIGKILL)])
        self.assertEqual(self.terminated, [handle(1).session] * 2)

    def test_a_pid_that_is_not_alive_is_never_signalled_but_the_pod_is_still_terminated(self):
        signals = []
        with (
            patch.object(runner_module, "pid_status", return_value="dead"),  # the pid may belong to another process now
            patch.object(runner_module, "signal_group", lambda pid, sig=signal.SIGKILL: signals.append((pid, sig))),
        ):
            self.stop(self.both(), PendingStop({}, deadline=0.0), 1.0)
        self.assertEqual((signals, self.terminated), ([], [handle(1).session]))

    def test_kill_children_handles_both_fields_of_one_job(self):
        killed = []
        with (
            patch.object(runner_module, "pid_status", return_value="alive"),
            patch.object(runner_module, "kill_group", killed.append),
        ):
            Runner.kill_children(fake_runner(self.both()))
        self.assertEqual((killed, self.terminated), ([4242], [handle(1).session]))

    def test_the_abort_path_handles_both_fields_of_one_job(self):
        signalled = []
        with (
            patch.object(runner_module, "pid_status", return_value="alive"),
            patch("os.killpg", lambda pid, sig: signalled.append((pid, sig))),
        ):
            Runner.abort_workers(fake_runner(self.both()))
        self.assertEqual((signalled, self.terminated), ([(4242, signal.SIGTERM)], [handle(1).session]))

    def test_the_abort_path_signals_the_groups_before_it_waits_for_a_pod(self):
        blocker = Blocker()
        self.addCleanup(blocker.release.set)
        podhandle.install(terminate=blocker)
        seen = []

        def killpg(pid, sig):
            seen.append((pid, sig, blocker.started.is_set()))  # no pod has been asked yet: a hung one holds nothing back
            blocker.release.set()  # only now may the terminator go on

        with (
            patch.object(runner_module, "pid_status", return_value="alive"),
            patch("os.killpg", killpg),
            patch.object(podhandle, "ABORT_TIMEOUT", 1.0),
        ):
            Runner.abort_workers(fake_runner({"a": {"pid": 11, "pid_host": None}, "b": {"pod": handle(1).record()}}))
        self.assertEqual(seen, [(11, signal.SIGTERM, False)])
        self.assertEqual(blocker.finished, [handle(1).session])

    def test_the_abort_path_waits_for_the_pods_within_one_deadline(self):
        release = threading.Event()
        self.addCleanup(release.set)
        podhandle.install(terminate=lambda pod: release.wait(30))
        jobs = {"a": {"pod": handle(1).record()}, "b": {"pod": handle(2).record()}, "c": {"pod": handle(3).record()}}
        with patch.object(podhandle, "ABORT_TIMEOUT", 7.0), spy_joins() as joins:
            Runner.abort_workers(fake_runner(jobs))
        self.assertEqual(len(joins), 3)
        self.assertTrue(all(timeout is not None and 0.0 <= timeout <= 7.0 for timeout in joins), joins)
        self.assertEqual(podhandle.ABORT_TIMEOUT, 10.0)  # the default, once the patch is gone

    def test_no_terminator_installed_is_reported_for_every_pod_and_raises_nothing(self):
        podhandle.reset()
        jobs = {"a": {"pod": handle(1).record()}}
        with self.stderr() as captured:
            Runner.kill_children(fake_runner(jobs))
            self.stop(jobs, PendingStop({}, deadline=0.0), 1.0)
        self.assertEqual(captured.getvalue().count("no terminator installed"), 2)


class RunnerSessionTests(PodCase):
    """A real Runner: what ``invoke``, ``execute`` and the preflight probe record through ``on_start`` and forget."""

    def setUp(self):
        super().setUp()
        self.runner = Runner(self.directory / "repo", self.directory / "run", assignment(), timeout=10)
        self.key = "fix-a"
        guidance = patch.object(Runner, "guidance", return_value="")
        guidance.start()
        self.addCleanup(guidance.stop)
        self.terminated = []

        def terminate(pod):
            self.terminated.append(pod.session)
            self.launcher.terminate(pod)

        podhandle.install(reader=self.launcher.reader, terminate=terminate)

    def job(self):
        return self.runner.state.data["jobs"][self.key]

    def invoke(self, started, outcome=None, during=None):
        """One ``Runner.invoke`` with a fake backend: it hands ``started`` to ``on_start``, runs ``during``, then ends."""
        seen = {}

        def fake_run_agent(prompt, cwd, prefix, phase, timeout, on_start, **kwargs):
            on_start(started)
            seen.update(pid=self.job().get("pid"), pod=self.job().get("pod"))
            if during is not None:
                during()
            if isinstance(outcome, Exception):
                raise outcome
            return {"summary": "x"}, {"total_cost_usd": 0.0}

        with patch.object(runner_module, "run_agent", fake_run_agent):
            try:
                self.runner.invoke(self.key, "build", "prompt", self.directory)
            except RuntimeError:
                if not isinstance(outcome, RuntimeError):
                    raise
        return seen

    def test_a_pod_that_the_launcher_reports_gone_is_forgotten_without_asking_it_to_delete(self):
        seen = self.invoke(handle(1))
        self.assertEqual(seen, {"pid": None, "pod": handle(1).record()})
        self.assertIsNone(self.job().get("pod"))
        self.assertIsNone(self.job().get("pid"))
        self.assertEqual(self.terminated, [])

    def test_a_pod_that_is_still_there_is_forgotten_once_the_terminator_confirmed_the_deletion(self):
        self.launcher.add(handle(1))
        self.invoke(handle(1))
        self.assertEqual((self.terminated, self.launcher.pods), ([handle(1).session], {}))
        self.assertIsNone(self.job().get("pod"))

    def test_a_pod_is_forgotten_when_the_session_raises_too(self):
        self.launcher.add(handle(1))
        seen = self.invoke(handle(1), RuntimeError("boom"))
        self.assertEqual(seen["pod"], handle(1).record())
        self.assertEqual(self.terminated, [handle(1).session])
        self.assertIsNone(self.job().get("pod"))

    def test_a_pod_whose_termination_failed_stays_recorded_and_recovery_refuses_on_it(self):
        """The launcher is down when the session ends: the pod may still run, so nothing may forget it."""
        self.launcher.add(handle(1))

        def launcher_goes_down():
            self.launcher.down = True

        with self.stderr() as captured:
            self.invoke(handle(1), during=launcher_goes_down)
        self.assertEqual(self.job()["pod"], handle(1).record())
        self.assertIsNone(self.job().get("pid"))
        self.assertIn(f"factory: could not terminate pod session {handle(1).session}: LauncherDown", captured.getvalue())
        self.assertEqual(State(self.directory / "run", assignment()).data["jobs"][self.key]["pod"], handle(1).record())
        with self.assertRaises(podhandle.PodStillLive) as caught:  # the next recover, in this or in a new process
            self.runner.state.recover(pod_reader=self.launcher.reader)
        self.assertEqual(caught.exception.status, "unknown")
        self.launcher.down = False
        with self.assertRaises(podhandle.PodStillLive) as caught:  # back up, and the pod is still there
            self.runner.state.recover(pod_reader=self.launcher.reader)
        self.assertEqual(caught.exception.status, "alive")
        self.launcher.pods.clear()  # an operator deleted it
        self.runner.state.recover(pod_reader=self.launcher.reader)
        self.assertIsNone(self.job().get("pod"))

    def test_a_session_that_failed_and_whose_pod_could_not_be_deleted_keeps_the_pod_and_the_error(self):
        self.launcher.add(handle(1))
        with self.stderr():
            seen = self.invoke(handle(1), RuntimeError("boom"), during=lambda: setattr(self.launcher, "down", True))
        self.assertEqual(seen["pod"], handle(1).record())
        self.assertEqual(self.job()["pod"], handle(1).record())

    def test_with_no_reader_and_no_terminator_installed_a_pod_stays_recorded(self):
        podhandle.reset()  # the state of this tree: nothing can confirm a deletion, so nothing is forgotten
        with self.stderr() as captured:
            self.invoke(handle(1))
        self.assertEqual(self.job()["pod"], handle(1).record())
        self.assertIn("no terminator installed", captured.getvalue())

    def test_no_session_starts_while_the_pod_of_an_earlier_one_is_not_confirmed_gone(self):
        self.launcher.add(handle(1))
        with self.stderr():
            self.invoke(handle(1), during=lambda: setattr(self.launcher, "down", True))
            started = []
            with patch.object(runner_module, "run_agent", lambda *args, **kwargs: started.append(1)):
                with self.assertRaisesRegex(RuntimeError, "not confirmed gone"):
                    self.runner.invoke(self.key, "review", "prompt", self.directory)
        self.assertEqual((started, self.job()["pod"]), ([], handle(1).record()))
        self.assertEqual((self.terminated, self.launcher.deleted), ([handle(1).session] * 2, []))  # asked twice, both failed
        self.launcher.down = False  # the launcher is back: the pod is deleted, the record goes, the session may start
        self.invoke(handle(2))
        self.assertEqual(self.launcher.deleted, [handle(1).session])
        self.assertIsNone(self.job().get("pod"))

    def test_a_stale_record_of_a_pod_that_is_dead_does_not_block_the_next_session(self):
        self.runner.state.record_pod(self.key, handle(1))  # the launcher knows no such pod: it is dead
        seen = self.invoke(handle(2))
        self.assertEqual(seen["pod"], handle(2).record())
        self.assertEqual((self.terminated, self.job().get("pod")), ([], None))

    def test_a_pid_handed_to_on_start_is_recorded_exactly_as_before(self):
        podhandle.install(
            reader=lambda pod: self.fail("a pid session never asks the launcher"),
            terminate=lambda pod: self.fail("a pid session never asks the launcher"),
        )
        seen = self.invoke(4242)
        self.assertEqual(seen, {"pid": 4242, "pod": None})
        self.assertIsNone(self.job().get("pid"))
        self.assertNotIn("pod", self.job())

    def execute(self, launcher_down):
        """``Runner.execute`` of a build phase that records a pod and fails."""

        def build(runner, job, worktree):
            runner.state.record_pod(job["id"], handle(1))
            self.launcher.down = launcher_down
            raise RuntimeError("phase failed")

        with (
            patch.object(Runner, "park_if_ignored", return_value=False),
            patch.object(Runner, "prepare_worktree", return_value=str(self.directory)),
            patch.object(Runner, "build", build),
            self.stderr() as captured,
        ):
            self.runner.execute({"id": self.key}, "build")
        return captured.getvalue()

    def test_execute_forgets_a_pod_it_could_confirm_gone_and_keeps_one_it_could_not(self):
        self.launcher.add(handle(1))
        self.execute(launcher_down=False)
        self.assertEqual((self.job()["status"], self.job()["error"], self.job()["pod"]), ("failed", "phase failed", None))
        self.assertEqual(self.terminated, [handle(1).session])
        self.launcher.add(handle(1))
        self.assertIn("could not terminate", self.execute(launcher_down=True))
        self.assertEqual((self.job()["status"], self.job()["pod"]), ("failed", handle(1).record()))

    def test_the_preflight_probe_keeps_a_pod_it_could_not_confirm_gone(self):
        from factory.preflight import probe_worker

        for down, kept in ((False, None), (True, handle(1).record())):
            with self.subTest(launcher_down=down):
                self.launcher.add(handle(1))
                self.launcher.down = False

                def fake_run_agent(prompt, cwd, prefix, phase, timeout, on_start, **kwargs):
                    on_start(handle(1))
                    self.launcher.down = down
                    raise ValueError("probe failed")

                with (
                    patch("factory.preflight.run_agent", fake_run_agent),
                    self.stderr(),
                    self.assertRaisesRegex(ValueError, "probe failed"),
                ):
                    probe_worker(self.runner, self.key, "build", self.directory)
                self.assertEqual(self.job().get("pod"), kept)
                self.runner.state.update(self.key, pod=None)


class RegistryTests(PodCase):
    def setUp(self):
        super().setUp()
        self.calls = []

    def terminate(self, pod):
        self.calls.append(pod.session)

    def test_every_registered_handle_is_terminated_once_and_none_after_unregister(self):
        for number in (1, 2, 3):
            podhandle.register(handle(number))
        podhandle.register(handle(2).record())  # registering twice must not terminate twice
        self.assertEqual(podhandle.kill_live_pods(self.terminate), 0)
        self.assertEqual(sorted(self.calls), sorted(handle(n).session for n in (1, 2, 3)))
        self.calls.clear()
        podhandle.unregister(handle(2))
        podhandle.unregister(handle(2))  # a second time is ignored
        podhandle.unregister({"name": "garbage"})
        podhandle.kill_live_pods(self.terminate)
        self.assertEqual(sorted(self.calls), sorted(handle(n).session for n in (1, 3)))
        self.calls.clear()
        podhandle.unregister(handle(1))
        podhandle.unregister(handle(3))
        podhandle.kill_live_pods(self.terminate)
        self.assertEqual(self.calls, [])

    def test_a_handle_that_does_not_validate_is_not_registered(self):
        with self.assertRaises(ValueError):
            podhandle.register({"name": "x"})
        self.assertEqual(podhandle.live(), [])

    def test_one_failing_termination_is_reported_and_stops_no_other(self):
        for number in (1, 2, 3):
            podhandle.register(handle(number))

        def flaky(pod):
            self.calls.append(pod.session)
            if pod.session == handle(2).session:
                raise LauncherDown

        with self.stderr() as captured:
            self.assertEqual(podhandle.kill_live_pods(flaky), 1)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(
            captured.getvalue(), f"factory: could not terminate pod session {handle(2).session}: LauncherDown\n"
        )

    def test_a_hung_termination_holds_up_no_other_one(self):
        release = threading.Event()
        self.addCleanup(release.set)
        for number in (1, 2, 3):
            podhandle.register(handle(number))

        def hang_on_first(pod):
            if pod.session == handle(1).session:
                release.wait(30)
            self.calls.append(pod.session)

        self.assertEqual(podhandle.kill_live_pods(hang_on_first, timeout=0.3), 1)
        self.assertEqual(sorted(self.calls), sorted(handle(n).session for n in (2, 3)))

    def test_without_a_terminator_the_failure_is_reported_for_every_session(self):
        podhandle.register(handle(1))
        podhandle.register(handle(2))
        with self.stderr() as captured:
            self.assertEqual(podhandle.kill_live_pods(), 2)
        self.assertIn("could not terminate 2 pod session(s): no terminator installed", captured.getvalue())
        self.assertEqual(podhandle.kill_live_pods(self.terminate, timeout=1), 0)

    def test_the_installed_terminator_is_the_default(self):
        podhandle.install(terminate=self.terminate)
        podhandle.register(handle(1))
        podhandle.kill_live_pods()
        self.assertEqual(self.calls, [handle(1).session])

    def test_the_messages_carry_no_pod_content(self):
        podhandle.register(handle(1))

        def leaky(pod):
            raise RuntimeError(f"token={pod.uid} secret-body")

        with self.stderr() as captured:
            podhandle.kill_live_pods(leaky)
        self.assertNotIn("secret-body", captured.getvalue())
        self.assertNotIn(handle(1).uid, captured.getvalue())


class WaitTests(PodCase):
    """``terminate_all`` returns only when every termination has finished or one shared deadline has passed."""

    def hung(self):
        release = threading.Event()
        self.addCleanup(release.set)  # the daemon threads end with the test
        return lambda pod: release.wait(30)

    def test_the_call_waits_for_a_terminator_that_has_not_finished(self):
        calls = {
            "terminate_all": lambda blocker: podhandle.terminate_all([handle(1)], blocker, timeout=10),
            "kill_live_pods": lambda blocker: podhandle.kill_live_pods(blocker, timeout=10),
            "terminate_recorded": lambda blocker: podhandle.terminate_recorded(
                [{"pod": handle(1).record()}], blocker, timeout=10
            ),
        }
        for name, call in calls.items():
            with self.subTest(call=name):
                podhandle.reset()
                podhandle.register(handle(1))
                blocker, results = Blocker(), []
                self.addCleanup(blocker.release.set)
                caller = threading.Thread(target=lambda: results.append(call(blocker)), daemon=True)
                caller.start()
                self.assertTrue(blocker.started.wait(5))
                caller.join(0.2)  # the terminator is blocked: the call must still be waiting for it
                self.assertTrue(caller.is_alive(), f"{name} returned before the termination had finished")
                self.assertEqual(blocker.finished, [])
                blocker.release.set()
                caller.join(5)
                self.assertFalse(caller.is_alive())
                self.assertEqual((results, blocker.finished), ([0], [handle(1).session]))

    def test_the_timeout_is_one_deadline_for_the_whole_wait_not_one_per_thread(self):
        clock = StepClock(start=100.0, step=1.0)
        with spy_joins() as joins:
            failed = podhandle.terminate_all([handle(n) for n in range(1, 6)], self.hung(), timeout=10.0, clock=clock)
        self.assertEqual(failed, 5)  # every hung call counts as failed
        # The deadline is 110, read once at 100; each join then waits for what is LEFT of it (the clock reads 101, 102, ...).
        self.assertEqual(joins, [9.0, 8.0, 7.0, 6.0, 5.0])

    def test_a_deadline_that_has_passed_leaves_nothing_to_wait_for(self):
        with spy_joins() as joins:
            podhandle.terminate_all([handle(n) for n in range(1, 4)], self.hung(), timeout=10.0, clock=StepClock(0.0, 50.0))
        self.assertEqual(joins, [0.0, 0.0, 0.0])  # never negative

    def test_without_a_timeout_every_join_waits_for_ever(self):
        with spy_joins() as joins:
            podhandle.terminate_all([handle(1), handle(2)], self.hung(), clock=StepClock())
        self.assertEqual(joins, [None, None])

    def test_the_deadline_reaches_the_threads_through_the_registry_and_the_recorded_jobs(self):
        for n in (1, 2, 3):
            podhandle.register(handle(n))
        with spy_joins() as joins:
            podhandle.kill_live_pods(self.hung(), timeout=10.0, clock=StepClock(100.0, 1.0))
        self.assertEqual(joins, [9.0, 8.0, 7.0])
        jobs = [{"pod": handle(n).record()} for n in (1, 2, 3)]
        with spy_joins() as joins:
            self.assertEqual(podhandle.terminate_recorded(jobs, self.hung(), timeout=10.0, clock=StepClock(100.0, 1.0)), 3)
        self.assertEqual(joins, [9.0, 8.0, 7.0])

    def test_the_clock_is_the_monotonic_one_by_default(self):
        for function in (podhandle.terminate_all, podhandle.terminate_recorded, podhandle.kill_live_pods):
            self.assertIs(inspect.signature(function).parameters["clock"].default, time.monotonic)


class LeaseLossTests(BlobTestCase):
    """Losing the repository lease runs the kill hooks; the pod hook terminates every registered pod."""

    def setUp(self):
        super().setUp()
        podhandle.reset()
        self.addCleanup(podhandle.reset)
        self.terminated = []
        hook = partial(podhandle.kill_live_pods, lambda pod: self.terminated.append(pod.session))
        locks.register_kill_hook(hook)
        self.addCleanup(locks.unregister_kill_hook, hook)

    def lose_the_lease(self):
        lock = self.make(on_lost=None).acquire()  # None selects the default: run the kill hooks, then exit
        self.server.break_lease("locks", "acme__widgets")
        self.make().acquire()
        with patch("os._exit") as fake_exit:
            with self.assertRaises(locks.LockLost):
                lock.renew()
        fake_exit.assert_called_once_with(75)

    def test_losing_the_lease_terminates_every_registered_pod_once(self):
        for number in (1, 2):
            podhandle.register(handle(number))
        self.lose_the_lease()
        self.assertEqual(sorted(self.terminated), sorted(handle(n).session for n in (1, 2)))

    def test_a_released_pod_is_not_terminated_and_with_none_left_nothing_is(self):
        for number in (1, 2):
            podhandle.register(handle(number))
        podhandle.unregister(handle(1))
        self.lose_the_lease()
        self.assertEqual(self.terminated, [handle(2).session])

    def test_the_process_does_not_exit_while_a_termination_is_still_under_way(self):
        """``exit_process_on_lost`` calls ``os._exit`` right after the hooks return and the terminate threads are daemons."""
        blocker = Blocker()
        self.addCleanup(blocker.release.set)
        slow_hook = partial(podhandle.kill_live_pods, blocker)
        locks.register_kill_hook(slow_hook)
        self.addCleanup(locks.unregister_kill_hook, slow_hook)
        podhandle.register(handle(1))
        lock = self.make(on_lost=None).acquire()
        self.server.break_lease("locks", "acme__widgets")
        self.make().acquire()
        exits, outcome = [], []

        def fake_exit(code):
            exits.append((code, list(blocker.finished)))  # what had finished when the process went away

        def renew():
            try:
                lock.renew()
            except locks.LockLost:
                outcome.append("lost")

        with patch("os._exit", fake_exit):
            thread = threading.Thread(target=renew, daemon=True)
            thread.start()
            self.assertTrue(blocker.started.wait(5))
            thread.join(0.2)  # the termination is blocked: the process must still be there
            self.assertEqual(exits, [])
            self.assertTrue(thread.is_alive())
            blocker.release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual((outcome, exits), (["lost"], [(75, [handle(1).session])]))

    def test_with_every_pod_released_nothing_is_terminated(self):
        podhandle.register(handle(1))
        podhandle.unregister(handle(1))
        self.lose_the_lease()
        self.assertEqual(self.terminated, [])


if __name__ == "__main__":
    unittest.main()

"""Pod-side liveness (src/factory/podliveness.py, slice 19 of the pod backend, docs/pod-backend.md).

What the supervisor may conclude about a pod session from the relay's record, Kubernetes' account of the pod and
the clock, without trusting the pod's text. CONTRACT-TESTED AGAINST ASSUMED SHAPES: the pure decision is driven
with hand-built answers and a real spool directory in a temporary directory; one class runs the real launcher over
a real unix socket against the in-memory fake of the Kubernetes API (tests/fake_kube.py). Nothing needs a network
beyond 127.0.0.1 or a cluster. Time is an argument: nothing sleeps.

Every rule is pinned by a test that fails when the rule is removed:

* a heartbeat is evidence of life only: a fresh one never outranks Kubernetes saying the pod is over, never makes
  a session ``finished`` and no verdict is ever "dead";
* only the relay can say a session finished, and it is read AFTER Kubernetes (a result that lands between the two
  reads is not lost);
* what cannot be read is ``unreadable`` and nothing is terminated on it; a MISSING record is ``lost``;
* the silence window runs from the later of the last heartbeat and the creation, to the second;
* a heartbeat dated in the future is ignored; the deadline ends every session;
* ``enforce`` revokes before it terminates and never lets a failed marker stop the termination.
"""

import ast
import contextlib
import io
import itertools
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from factory import launcher_client, podhandle, podliveness as pl, podstatus
from factory import podcontract as pc
from factory import sessionrecord as sr
from tests import fake_kube
from tests.fake_kube import Step
from tests.test_launcher import ServerHarness, TOKEN, launch_request, pod_name

SESSION = fake_kube.session_id(1)
OTHER = fake_kube.session_id(2)
UID = "11111111-1111-4111-8111-111111111111"
NOW = 1_800_000_000
DEADLINE = NOW + 7200
LIVE = {"state": "running", "detail": "running", "final": False, "exit_code": None, "present": True, "terminating": False}


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pod(state="running", final=False, present=True, terminating=False):
    return {"state": state, "detail": state, "final": final, "exit_code": None, "present": present, "terminating": terminating}


def relay(state="streaming", heartbeat=None, deadline=DEADLINE, **fields):
    return pl.Relay("ok", state=state, deadline=deadline, heartbeat=heartbeat, **fields)


def handle(session=SESSION, created=NOW - 100):
    return podhandle.PodHandle(
        name=pc.pod_name(session), uid=UID, session=session, launcher="launcher", created_at=iso(created)
    )


class SpoolCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "spool"
        self.root.mkdir()

    def make(self, session=SESSION, *, state="streaming", heartbeat=None, deadline=DEADLINE, revoke=False, released=False, reject=None):
        sr.write(self.root, sr.build(session, "t" * 43, deadline, pc.repo_key("octo/widgets"), "cwd", "abcdef012345"))
        with sr.session_dir(self.root, session) as fd:
            document = sr.new_state()
            document.update(state=state, reject=reject)
            sr.write_state_at(fd, document)
            if heartbeat is not None:
                sr.write_heartbeat_at(fd, heartbeat)
            if revoke:
                sr.mark_at(fd, sr.MARKER_REVOKE)
            if released:
                sr.mark_at(fd, sr.MARKER_RELEASED)


class DecisionTests(unittest.TestCase):
    """``assess``: the pure decision from a ``Relay``, the launcher's answer, the clock and the creation."""

    def verdict(self, relay_value, pod_value, now=NOW, created=NOW - 100):
        return pl.assess(relay_value, pod_value, now, created)

    def test_a_relay_that_finished_is_finished_whatever_kubernetes_and_the_clock_say(self):
        for pod_value in (None, LIVE, pod("gone", True, False), pod("oom", True), pod("evicted", True, True, True)):
            for now in (NOW, DEADLINE + 10, NOW + 100_000):
                with self.subTest(pod=pod_value, now=now):
                    result = self.verdict(relay("finished", heartbeat=1), pod_value, now=now)
                    self.assertEqual((result.state, result.action, result.reason), ("finished", "collect", "result"))

    def test_the_relays_reject_code_comes_with_a_finished_session(self):
        result = self.verdict(relay("finished", reject="transcript_cap"), LIVE)
        self.assertEqual((result.state, result.reject), ("finished", "transcript_cap"))

    def test_a_released_session_has_nothing_left_to_do(self):
        result = self.verdict(relay("finished", released=True), pod("gone", True, False))
        self.assertEqual((result.state, result.action), ("released", "none"))

    def test_a_fresh_heartbeat_with_a_running_pod_is_running(self):
        result = self.verdict(relay(heartbeat=NOW - 10), LIVE)
        self.assertEqual((result.state, result.action, result.reason, result.silence), ("running", "wait", "heartbeat", 10))

    def test_silence_is_measured_to_the_second_and_ends_at_the_orphan_window(self):
        window = pc.ORPHAN_SILENCE_SECONDS
        old = NOW - 10 * window  # the pod is older than the window: only the heartbeat can keep it young
        before = self.verdict(relay(heartbeat=NOW - window + 1), LIVE, created=old)
        after = self.verdict(relay(heartbeat=NOW - window), LIVE, created=old)
        self.assertEqual(before.state, "running")
        self.assertEqual((after.state, after.action, after.reason, after.silence), ("silent", "terminate", "no_heartbeat", window))

    def test_the_window_is_the_contracts_and_longer_than_a_few_heartbeats(self):
        self.assertEqual(pc.ORPHAN_SILENCE_SECONDS, 300)
        self.assertGreaterEqual(pc.ORPHAN_SILENCE_SECONDS, 3 * pc.HEARTBEAT_SECONDS)

    def test_a_pod_that_never_heartbeats_is_silent_from_its_creation(self):
        window = pc.ORPHAN_SILENCE_SECONDS
        young = self.verdict(relay(), LIVE, created=NOW - window + 1)
        old = self.verdict(relay(), LIVE, created=NOW - window)
        self.assertEqual((young.state, young.reason), ("running", "not_heard_yet"))
        self.assertEqual((old.state, old.action, old.reason), ("silent", "terminate", "never_heard"))

    def test_silence_runs_from_the_later_of_the_heartbeat_and_the_creation(self):
        window = pc.ORPHAN_SILENCE_SECONDS
        # a heartbeat older than the creation (a pod created again under a recycled record) does not age the pod
        self.assertEqual(self.verdict(relay(heartbeat=NOW - 10 * window), LIVE, created=NOW - 5).state, "running")
        # a young heartbeat on an old pod keeps it running
        self.assertEqual(self.verdict(relay(heartbeat=NOW - 5), LIVE, created=NOW - 10 * window).state, "running")

    def test_a_heartbeat_from_the_future_is_ignored(self):
        window = pc.ORPHAN_SILENCE_SECONDS
        future = NOW + pl.FUTURE_SKEW_SECONDS + 1
        result = self.verdict(relay(heartbeat=future), LIVE, created=NOW - window)
        self.assertEqual((result.state, result.reason), ("silent", "never_heard"))
        within_skew = self.verdict(relay(heartbeat=NOW + pl.FUTURE_SKEW_SECONDS), LIVE, created=NOW - 10 * window)
        self.assertEqual(within_skew.state, "running")

    def test_a_heartbeat_never_outranks_kubernetes_saying_the_pod_is_over(self):
        for state in ("succeeded", "failed_exit", "oom", "evicted", "deadline", "gone", "starting_error"):
            with self.subTest(state=state):
                result = self.verdict(relay(heartbeat=NOW), pod(state, True, state != "gone"))
                self.assertEqual((result.state, result.reason), ("lost", state))
                self.assertNotEqual(result.action, "collect", "no result in the relay: nothing to collect")

    def test_a_pod_that_is_gone_is_failed_and_one_that_is_still_listed_is_cleaned_up(self):
        gone = self.verdict(relay(heartbeat=NOW), pod("gone", True, False))
        listed = self.verdict(relay(heartbeat=NOW), pod("succeeded", True, True))
        self.assertEqual((gone.action, listed.action), ("fail", "terminate"))

    def test_a_terminating_pod_is_not_over_even_though_it_reads_as_final(self):
        result = self.verdict(relay(heartbeat=NOW - 10_000), pod("evicted", True, True, True))
        self.assertEqual((result.state, result.action), ("terminating", "wait"))

    def test_a_pod_waiting_for_a_node_is_starting_until_the_scheduling_window_and_the_launch_grace_pass(self):
        window = pc.SCHEDULING_TIMEOUT_SECONDS + pc.LAUNCH_GRACE_SECONDS
        for state in ("pending", "unschedulable"):
            with self.subTest(state=state):
                early = self.verdict(relay("bound"), pod(state), created=NOW - window + 1)
                late = self.verdict(relay("bound"), pod(state), created=NOW - window)
                self.assertEqual((early.state, early.action, early.reason), ("starting", "wait", "waiting_for_node"))
                self.assertEqual((late.state, late.action, late.reason), ("silent", "terminate", "never_started"))

    def test_an_image_pull_error_has_the_shorter_window(self):
        window = pc.PULL_ERROR_SECONDS + pc.LAUNCH_GRACE_SECONDS
        self.assertLess(window, pc.SCHEDULING_TIMEOUT_SECONDS + pc.LAUNCH_GRACE_SECONDS)
        early = self.verdict(relay("bound"), pod("starting_error"), created=NOW - window + 1)
        late = self.verdict(relay("bound"), pod("starting_error"), created=NOW - window)
        self.assertEqual((early.state, late.state, late.reason), ("starting", "silent", "never_started"))

    def test_a_pod_whose_age_is_unknown_is_starting_not_silent(self):
        result = self.verdict(relay("bound"), pod("pending"), created=None)
        self.assertEqual((result.state, result.reason, result.silence), ("starting", "age_unknown", None))

    def test_a_pod_that_cannot_be_read_is_unreadable_and_nothing_is_terminated_on_a_guess(self):
        silent = relay(heartbeat=NOW - 10_000)
        for answer in (None, {}, {"state": "running"}, {**LIVE, "state": "dancing"}, {**LIVE, "present": "yes"}, "running", []):
            with self.subTest(answer=answer):
                result = self.verdict(silent, answer, created=NOW - 10_000)
                self.assertEqual((result.state, result.action, result.reason), ("unreadable", "wait", "launcher_unreadable"))

    def test_the_deadline_ends_a_session_that_still_heartbeats(self):
        fresh = relay(heartbeat=DEADLINE - 1)
        self.assertEqual(self.verdict(fresh, LIVE, now=DEADLINE - 1).state, "running")
        result = self.verdict(fresh, LIVE, now=DEADLINE)
        self.assertEqual((result.state, result.action, result.reason), ("expired", "terminate", "deadline"))
        self.assertEqual(self.verdict(relay("expired", heartbeat=NOW), LIVE).state, "expired")

    def test_an_expired_or_revoked_session_is_ended_even_when_the_launcher_cannot_be_asked(self):
        for relay_value, state in ((relay("expired"), "expired"), (relay(revoked=True), "revoked"), (relay("revoked"), "revoked")):
            with self.subTest(state=state):
                result = self.verdict(relay_value, None)
                self.assertEqual((result.state, result.action), (state, "terminate"))

    def test_a_revoked_session_whose_pod_is_gone_only_needs_to_be_failed(self):
        result = self.verdict(relay(revoked=True), pod("gone", True, False))
        self.assertEqual((result.state, result.action), ("revoked", "fail"))

    def test_a_missing_record_means_the_spool_was_replaced_and_the_session_is_lost(self):
        running = self.verdict(pl.MISSING, LIVE)
        gone = self.verdict(pl.MISSING, pod("gone", True, False))
        self.assertEqual((running.state, running.reason, running.action), ("lost", "no_session_record", "terminate"))
        self.assertEqual((gone.state, gone.action), ("lost", "fail"))
        self.assertEqual(self.verdict(pl.MISSING, None).state, "unreadable")

    def test_a_spool_that_cannot_be_read_is_unreadable_never_lost_or_finished(self):
        for pod_value in (LIVE, pod("succeeded", True), pod("gone", True, False)):
            with self.subTest(pod=pod_value):
                result = self.verdict(pl.UNREAD, pod_value)
                self.assertEqual((result.state, result.action, result.reason), ("unreadable", "wait", "relay_unreadable"))
        self.assertEqual(self.verdict("not a relay", LIVE).state, "unreadable")

    def test_every_answer_is_in_the_closed_sets_and_no_verdict_is_dead(self):
        relays = [pl.MISSING, pl.UNREAD]
        for state, beat, revoked, released in itertools.product(
            sr.STATES, (None, NOW - 10, NOW - 1000, NOW + 10_000), (False, True), (False, True)
        ):
            relays.append(relay(state, heartbeat=beat, revoked=revoked, released=released))
        pods = [None, *(pod(state, final, present, term) for state in podstatus.STATES for final in (False, True) for present in (False, True) for term in (False, True))]
        for relay_value, pod_value, created, now in itertools.product(relays, pods, (None, NOW - 5, NOW - 5000), (NOW, DEADLINE)):
            result = pl.assess(relay_value, pod_value, now, created)
            self.assertIn(result.state, pl.STATES)
            self.assertIn(result.action, pl.ACTIONS)
            self.assertIn(result.reason, pl.REASONS)
            self.assertEqual(result.action == "collect", result.state == "finished")
            self.assertNotEqual(result.state, "dead")

    def test_a_heartbeat_never_makes_a_session_finished(self):
        for beat in (None, NOW, NOW - 1000):
            result = self.verdict(relay("streaming", heartbeat=beat), LIVE)
            self.assertNotEqual((result.state, result.action), ("finished", "collect"))

    def test_the_vocabularies_are_closed_and_the_states_have_the_documented_names(self):
        self.assertEqual(
            pl.STATES,
            ("starting", "running", "terminating", "silent", "expired", "revoked", "finished", "released", "lost", "unreadable"),
        )
        self.assertEqual(pl.ACTIONS, ("wait", "collect", "terminate", "fail", "none"))
        self.assertEqual(len(set(pl.REASONS)), len(pl.REASONS))
        self.assertTrue(set(podstatus.STATES) <= set(pl.REASONS))


class ObserveTests(SpoolCase):
    """``observe``: Kubernetes first, then the relay's files, from a real spool directory."""

    def observe(self, answer, session=SESSION, now=NOW, created=NOW - 100):
        calls = []

        def pod_state(name, uid):
            calls.append((name, uid))
            if isinstance(answer, BaseException):
                raise answer
            return answer(name, uid) if callable(answer) else answer

        result = pl.observe(handle(session, created), self.root, pod_state, now)
        self.calls = calls
        return result

    def test_a_running_pod_with_a_recent_heartbeat_is_running(self):
        self.make(heartbeat=NOW - 20)
        result = self.observe(LIVE)
        self.assertEqual((result.state, result.silence), ("running", 20))
        self.assertEqual(self.calls, [(pc.pod_name(SESSION), UID)], "the launcher is asked for exactly this pod and uid")

    def test_the_heartbeat_the_relay_wrote_is_what_counts_not_anything_in_a_file_a_pod_could_shape(self):
        self.make(heartbeat=NOW - 10)
        path = self.root / SESSION / "last_heartbeat"
        for junk in (b"99999999999999999999999999999\n", b"-1\n", b"1e9\n", b"\xff\xfe", b"" , b"12 34\n"):
            path.write_bytes(junk)
            result = self.observe(LIVE, created=NOW - 10 * pc.ORPHAN_SILENCE_SECONDS)
            with self.subTest(junk=junk):
                self.assertEqual((result.state, result.reason), ("silent", "never_heard"), "an unusable heartbeat file is no heartbeat")

    def test_a_pod_the_launcher_cannot_answer_for_is_unreadable(self):
        self.make(heartbeat=NOW - 5000)
        for answer in (launcher_client.Unavailable(), RuntimeError("boom"), OSError()):
            with self.subTest(answer=type(answer).__name__):
                self.assertEqual(self.observe(answer).state, "unreadable")

    def test_a_record_that_does_not_validate_is_unreadable_and_the_launcher_is_not_asked(self):
        for record in ({"name": "factory-w-" + "a" * 32}, None, "x", {**handle().record(), "uid": 5}):
            with self.subTest(record=record):
                calls = []
                result = pl.observe(record, self.root, lambda name, uid: calls.append(name) or LIVE, NOW)
                self.assertEqual((result.state, calls), ("unreadable", []))

    def test_a_session_with_no_record_is_lost_and_one_with_a_broken_record_is_unreadable(self):
        self.assertEqual(self.observe(LIVE).state, "lost")
        self.make()
        (self.root / SESSION / "record.json").write_text("{not json")
        self.assertEqual(self.observe(LIVE).state, "unreadable")

    def test_the_markers_and_the_state_are_read(self):
        self.make(revoke=True)
        self.assertEqual(self.observe(LIVE).state, "revoked")
        self.make(OTHER, state="finished", released=True)
        self.assertEqual(self.observe(LIVE, session=OTHER).state, "released")
        third = fake_kube.session_id(3)
        self.make(third, state="finished", reject="delta_cap")
        result = self.observe(LIVE, session=third)
        self.assertEqual((result.state, result.reject), ("finished", "delta_cap"))

    def test_the_relay_is_read_after_kubernetes_so_a_result_that_lands_in_between_is_not_lost(self):
        self.make(state="streaming", heartbeat=NOW)

        def pod_finishes_while_we_ask(name, uid):
            # the wrapper posts its result and exits: the relay stores `finished`, then Kubernetes reports Succeeded
            with sr.session_dir(self.root, SESSION) as fd:
                document = sr.new_state()
                document.update(state="finished", ended_at=NOW)
                sr.write_state_at(fd, document)
            return pod("succeeded", True)

        result = self.observe(pod_finishes_while_we_ask)
        self.assertEqual((result.state, result.action), ("finished", "collect"))

    def test_a_pod_that_succeeded_without_a_result_is_lost(self):
        self.make(state="streaming", heartbeat=NOW)
        result = self.observe(pod("succeeded", True))
        self.assertEqual((result.state, result.reason), ("lost", "succeeded"))

    def test_probe_binds_the_spool_the_client_and_a_clock(self):
        self.make(heartbeat=NOW - 7)
        probe = pl.Probe(self.root, lambda name, uid: LIVE, clock=lambda: NOW)
        self.assertEqual(probe.assess(handle().record()).silence, 7)


class ReadRelayTests(SpoolCase):
    def test_a_directory_that_is_a_link_or_a_file_is_not_a_session(self):
        (self.root / SESSION).symlink_to(self.root)
        self.assertEqual(pl.read_relay(self.root, SESSION).read, "unreadable")
        (self.root / SESSION).unlink()
        (self.root / SESSION).write_text("x")
        self.assertIn(pl.read_relay(self.root, SESSION).read, ("unreadable", "missing"))

    def test_a_session_id_that_is_not_hex_is_never_a_path(self):
        self.assertEqual(pl.read_relay(self.root, "../etc").read, "unreadable")
        self.assertEqual(pl.read_relay(self.root, "A" * 32).read, "unreadable")

    def test_epoch_of_reads_the_state_timestamp_and_nothing_else(self):
        self.assertEqual(pl.epoch_of("1970-01-01T00:00:10Z"), 10)
        self.assertEqual(pl.epoch_of("1970-01-01T00:00:10.5Z"), 10)
        for text in (None, 5, "yesterday", "1970-01-01 00:00:10", "2026-13-01T00:00:00Z", "1970-01-01T00:00:10+00:00"):
            self.assertIsNone(pl.epoch_of(text))


class EnforceTests(SpoolCase):
    def test_a_termination_revokes_first_then_terminates_and_reports_the_confirmation(self):
        self.make()
        order = []

        def terminate(target):
            order.append(("terminate", target.session, sr.is_marked(self.root, SESSION, sr.MARKER_REVOKE)))

        verdict = pl.Verdict("silent", "no_heartbeat", "terminate", silence=999)
        self.assertTrue(pl.enforce(verdict, handle().record(), self.root, terminate))
        self.assertEqual(order, [("terminate", SESSION, True)], "the marker was already there when the pod was asked to go")

    def test_a_termination_that_fails_is_not_confirmed_and_never_raises(self):
        self.make()

        def refuse(_target):
            raise launcher_client.Unavailable()

        with contextlib.redirect_stderr(io.StringIO()) as captured:
            result = pl.enforce(pl.Verdict("expired", "deadline", "terminate"), handle(), self.root, refuse)
        self.assertFalse(result)
        self.assertEqual(captured.getvalue(), f"factory: could not terminate pod session {SESSION}: Unavailable\n")
        self.assertTrue(sr.is_marked(self.root, SESSION, sr.MARKER_REVOKE), "the revoke stands: the pod is cut off")

    def test_a_marker_that_cannot_be_written_does_not_stop_the_termination(self):
        asked = []  # no record, no directory: the marker cannot be written
        verdict = pl.Verdict("lost", "no_session_record", "terminate")
        self.assertTrue(pl.enforce(verdict, handle(), self.root, asked.append))
        self.assertEqual(len(asked), 1)
        self.assertFalse(pl.revoke(self.root, SESSION))

    def test_only_a_terminate_verdict_touches_the_pod(self):
        self.make()
        asked = []
        expected = {"wait": False, "collect": True, "fail": True, "none": True}
        for action, nothing_left in expected.items():
            with self.subTest(action=action):
                verdict = pl.Verdict("running", "heartbeat", action)
                self.assertEqual(pl.enforce(verdict, handle(), self.root, asked.append), nothing_left)
        self.assertEqual(asked, [])
        self.assertFalse(sr.is_marked(self.root, SESSION, sr.MARKER_REVOKE), "nothing was revoked either")

    def test_a_record_that_does_not_validate_is_never_passed_to_the_terminator(self):
        asked = []
        self.assertFalse(pl.enforce(pl.Verdict("silent", "no_heartbeat", "terminate"), {"name": "x"}, self.root, asked.append))
        self.assertEqual(asked, [])

    def test_a_stale_heartbeat_can_lead_to_a_termination_but_never_to_a_dead_pod(self):
        # the whole point of the module: silence terminates, it does not let a record be forgotten
        self.make(heartbeat=NOW - 10_000)
        verdict = pl.observe(handle(created=NOW - 20_000), self.root, lambda name, uid: LIVE, NOW)
        self.assertEqual((verdict.state, verdict.action), ("silent", "terminate"))
        self.assertEqual(podhandle.pod_status(handle(), lambda _pod: "alive"), "alive")
        self.assertIsNone(getattr(podhandle, "SILENT", None))


class RealLauncherTests(ServerHarness):
    """The verdicts against the real launcher over a real socket and the fake API: a launched pod, its record, time."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.spool = Path(tmp.name)

    def launched(self, client, n=1, scenario=None):
        if scenario is not None:
            self.fake.script(pod_name(n), scenario)
        reply = client.launch(launch_request(n), TOKEN)
        session = fake_kube.session_id(n)
        record = podhandle.PodHandle(reply.name, reply.uid, session, "launcher", iso(NOW - 10))
        sr.write(self.spool, sr.build(session, "t" * 43, DEADLINE, pc.repo_key(fake_kube.REPOSITORY), "cwd", fake_kube.INSTANCE))
        sr.bind(self.spool, session, reply.name, reply.uid)
        with sr.session_dir(self.spool, session) as fd:
            document = sr.new_state()
            document.update(state="streaming")
            sr.write_state_at(fd, document)
        return record

    def test_a_running_pod_goes_silent_and_is_terminated_through_the_client(self):
        self.start_server()
        client = launcher_client.LauncherClient(
            self.config.socket_path, launcher_uid=os.getuid(), sleep=self.clock.advance, clock=self.clock
        )
        record = self.launched(client, scenario=(Step(0, fake_kube.running()),))
        with sr.session_dir(self.spool, record.session) as fd:
            sr.write_heartbeat_at(fd, NOW - 5)
        probe_at = {"now": NOW}
        probe = pl.Probe(self.spool, client.pod_state, clock=lambda: probe_at["now"])
        self.assertEqual(probe.assess(record).state, "running")
        probe_at["now"] = NOW - 5 + pc.ORPHAN_SILENCE_SECONDS
        verdict = probe.assess(record)
        self.assertEqual((verdict.state, verdict.action), ("silent", "terminate"))
        self.assertTrue(pl.enforce(verdict, record, self.spool, client.terminate), "the client returned once the pod was gone")
        self.assertEqual(self.fake.pods(), {})
        self.assertTrue(sr.is_marked(self.spool, record.session, sr.MARKER_REVOKE))
        self.assertEqual(probe.assess(record).state, "revoked")

    def test_a_pod_that_ran_out_of_memory_is_lost_not_finished(self):
        self.start_server()
        client = self.client()
        record = self.launched(client, scenario=(Step(0, fake_kube.oom_killed()),))
        result = pl.Probe(self.spool, client.pod_state, clock=lambda: NOW).assess(record)
        self.assertEqual((result.state, result.reason, result.action), ("lost", "oom", "terminate"))

    def test_a_pod_that_is_gone_from_the_api_is_failed(self):
        self.start_server()
        client = self.client()
        record = self.launched(client, scenario=(Step(0, fake_kube.running()),))
        client.delete_pod(record.name, record.uid)
        self.clock.advance(pc.TERMINATION_GRACE_SECONDS)
        result = pl.Probe(self.spool, client.pod_state, clock=lambda: NOW).assess(record)
        self.assertEqual((result.state, result.reason, result.action), ("lost", "gone", "fail"))

    def test_an_unreachable_launcher_is_unreadable(self):
        client = launcher_client.LauncherClient(
            str(self.spool / "absent.sock"), launcher_uid=os.getuid(), sleep=lambda _s: None, retries=0
        )
        self.make_record_only()
        result = pl.Probe(self.spool, client.pod_state, clock=lambda: NOW).assess(handle())
        self.assertEqual(result.state, "unreadable")

    def make_record_only(self):
        sr.write(self.spool, sr.build(SESSION, "t" * 43, DEADLINE, pc.repo_key("octo/widgets"), "cwd", "abcdef012345"))


class ShapeTests(unittest.TestCase):
    def source(self):
        return Path(pl.__file__).read_text(encoding="utf-8")

    def test_the_module_is_standard_library_and_the_package_only(self):
        modules = set()
        for node in ast.walk(ast.parse(self.source())):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
            if isinstance(node, ast.ImportFrom):
                modules.add("." * node.level + (node.module or ""))
        self.assertTrue(modules <= set(sys.stdlib_module_names) | {"."}, modules)

    def test_it_never_prints_logs_reads_the_environment_or_opens_a_socket(self):
        tree = ast.parse(self.source())
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        self.assertFalse({"print", "open", "exec", "eval"} & names)
        self.assertFalse({"environ", "getenv", "socket", "connect", "system", "Popen", "run"} & attributes)

    def test_the_windows_are_the_contracts_and_not_retyped_numbers(self):
        numbers = {
            node.value
            for node in ast.walk(ast.parse(self.source()))
            if isinstance(node, ast.Constant) and type(node.value) is int and node.value > 1
        }
        self.assertLessEqual(numbers, {pl.FUTURE_SKEW_SECONDS, 6}, "6 is the number of fields of a timestamp; every window is the contract's")
        for name in ("ORPHAN_SILENCE_SECONDS", "SCHEDULING_TIMEOUT_SECONDS", "PULL_ERROR_SECONDS", "LAUNCH_GRACE_SECONDS"):
            self.assertIn(f"pc.{name}", self.source())

    def test_the_closed_reasons_are_documented_in_the_design_record(self):
        text = (Path(__file__).resolve().parents[1] / "docs" / "pod-backend.md").read_text(encoding="utf-8")
        for word in (*pl.STATES, *pl.REASONS):
            self.assertIn(f"`{word}`", text, word)
        self.assertIn("podliveness.py", text)


if __name__ == "__main__":
    unittest.main()

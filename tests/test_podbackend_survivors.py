"""Protections of the pod backend that a reviewer's mutations found untested (fixes of the second review).

* the relay record's deadline is computed from the clock of the launch request, not of the moment the record was first
  written, so waiting for spool space or for launcher capacity does not eat the session's time limit;
* a breaker that trips AFTER the pool admitted the session (while the bundle waits for spool space) still stops the launch;
* a pod that was fetched and then says nothing is failed as ``silent`` at ``ORPHAN_SILENCE_SECONDS`` and is not left to run
  to the time limit;
* a pod that exists but whose handle could not be made is deleted by its NAME on release.
"""

import math
import unittest
from unittest.mock import patch

from factory import apierror, bundle, launcher_client, podbackend, podhandle, stopworld
from factory import podcontract as pc
from factory import sessionrecord as sr
from factory.agent import AgentError, AgentTimeout
from tests.test_podbackend import EPOCH, SUCCEEDED, Fresh, report_of


class RecordDeadlineTests(Fresh):
    def expected(self, now_clock):
        seconds = self.backend._seconds(self.backend.prepare(self.request()))
        return (
            math.ceil(EPOCH + now_clock)
            + pc.SCHEDULING_TIMEOUT_SECONDS
            + min(seconds, pc.DEADLINE_CLAMP_SECONDS)
            + pc.DEADLINE_MARGIN_SECONDS
        )

    def watch_asks(self):
        """For every launch request: the clock and the record's deadline at that moment."""
        asks = []
        launch = self.launcher.launch

        def launching(request, token):
            asks.append((self.clock(), sr.read(self.spool, request["session_id"])["deadline"]))
            return launch(request, token)

        self.launcher.launch = launching
        return asks

    def test_waiting_for_spool_space_and_for_capacity_does_not_shorten_the_sessions_time(self):
        asks = self.watch_asks()
        real = bundle.stage
        calls = []

        def stage(*args, **kwargs):
            calls.append(1)
            if len(calls) < 40:  # 39 waits of SPOOL_POLL_SECONDS
                raise bundle.SpoolFull(10, 0)
            return real(*args, **kwargs)

        self.launcher.failures.extend([launcher_client.Capacity()] * 20)
        self.happy()
        self.script = {}
        self.steps_to_streaming()
        self.at(30, lambda: (self.pod.finish(report_of()), setattr(self.launcher, "state", SUCCEEDED)))
        started = self.clock()
        with patch.object(bundle, "stage", stage):
            self.run_session()
        waited = self.clock() - started
        self.assertGreater(waited, 39 * podbackend.SPOOL_POLL_SECONDS)  # the waits really moved the fake clock
        self.assertEqual(len(asks), 21)
        for clock, deadline in asks:
            self.assertEqual(deadline, self.expected(clock), "the deadline is the one of the moment of the ask")
        first, last = asks[0], asks[-1]
        self.assertGreater(last[1], first[1])
        self.assertGreaterEqual(last[1] - last[0] - EPOCH, pc.SCHEDULING_TIMEOUT_SECONDS + self.timeout)

    def test_the_rest_of_the_record_is_unchanged_by_the_refresh(self):
        asks = []
        launch = self.launcher.launch

        def launching(request, token):
            asks.append(sr.read(self.spool, request["session_id"]))
            return launch(request, token)

        self.launcher.launch = launching
        self.happy()
        self.run_session()
        record = asks[0]
        self.assertEqual(record["session"], self.session)
        self.assertEqual((record["pod_name"], record["pod_uid"]), (None, None))
        self.assertEqual(record["manager"], "abcdef012345")
        self.assertEqual(record["write_scope"], "cwd")
        self.assertEqual(record["token_sha256"], sr.token_hash(self.launcher.tokens[0]))

    def test_a_record_that_cannot_be_rewritten_fails_the_session_before_any_pod(self):
        # The first write is the initial one; only the refresh goes wrong.
        calls = []
        original = sr.write_at

        def flaky(fd, record):
            calls.append(1)
            if len(calls) > 1:
                raise OSError("disk says POISON")
            return original(fd, record)

        with patch.object(sr, "write_at", flaky):
            with self.assertRaises(AgentError) as caught:
                self.run_session()
        self.assertEqual(str(caught.exception), podbackend.RECORD)
        self.assertNotIn("POISON", str(caught.exception))
        self.assertEqual(self.launcher.requests, [])
        self.assertEqual(self.pool.snapshot()["in_use"], 0)


class LaunchBreakerTests(Fresh):
    def test_a_breaker_that_trips_after_the_pool_admitted_the_session_still_stops_the_launch(self):
        real = bundle.stage
        calls = []

        def stage(*args, **kwargs):
            calls.append(1)
            if len(calls) < 2:
                raise bundle.SpoolFull(10, 0)  # one wait, during which the breaker trips
            return real(*args, **kwargs)

        self.early.append(lambda: stopworld.Breaker(self.data).trip(apierror.SPEND_LIMIT))
        with patch.object(bundle, "stage", stage), self.assertRaises(AgentError) as caught:
            self.run_session()
        self.assertEqual(str(caught.exception), podbackend.TRIPPED)
        self.assertEqual(self.launcher.requests, [], "no pod is asked for after the trip")
        self.assertEqual(self.pool.snapshot()["in_use"], 0)


class SilentTests(Fresh):
    def test_a_pod_that_fetched_its_bundle_and_then_says_nothing_fails_as_silent_before_the_time_limit(self):
        self.at(1, lambda: (self.pod.preflight(), self.pod.fetch()))
        with self.assertRaises(RuntimeError) as caught:  # no result in the transcript: ``backend._no_result``
            self.run_session()
        self.assertEqual(str(caught.exception).split(";")[0], podbackend.FAILURES["silent"])
        spent = sum(self.sleeps)
        self.assertGreater(spent, pc.ORPHAN_SILENCE_SECONDS)
        self.assertLess(spent, self.timeout, "it was not left to run to the time limit")

    def test_the_same_pod_that_keeps_heartbeating_is_not_called_silent(self):
        self.at(1, lambda: (self.pod.preflight(), self.pod.fetch()))
        for tick in range(2, 400, 10):
            self.at(tick, lambda: self.pod.heartbeat())
        with self.assertRaises(AgentTimeout):
            self.run_session()


class ReleaseByNameTests(Fresh):
    def test_a_pod_whose_handle_could_not_be_made_is_deleted_by_its_name(self):
        with patch.object(podbackend.PodBackend, "_stamp", side_effect=RuntimeError("no stamp")):
            with self.assertRaises(RuntimeError):
                self.run_session()
        self.assertEqual(self.launcher.deleted, [pc.pod_name(self.session)])
        self.assertEqual(podhandle.live(), [])
        self.assertTrue(sr.is_marked(self.spool, self.session, sr.MARKER_RELEASED))
        self.assertEqual(self.launcher.terminated, [], "there was no handle to terminate")


if __name__ == "__main__":
    unittest.main()

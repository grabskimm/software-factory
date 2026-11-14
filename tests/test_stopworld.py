"""The stop-the-world breaker (src/factory/stopworld.py) and how the manager and its operations loop use it.

The API errors are INJECTED, ASSUMED shapes (docs/capacity.md, "Stop-the-world breaker"). The clock is fake
and nothing sleeps. The manager API runs on the same fakes as tests/test_manager.py.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import unittest.mock
from datetime import datetime, timezone
from pathlib import Path

import factory
from factory import apierror, status, stopworld
from factory import operations as ops
from factory.agent import AgentError
from factory.manager import TRIGGER_PAUSE_FILE, ApiError, Child
from factory.runner import Runner
from factory.state import State
from tests import test_manager as tm
from tests.test_operations import CANARY, OpsCase, failure, text

MINUTE = 60.0


class FakeClock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def stamp(self, seconds_ago=0.0):
        return datetime.fromtimestamp(self.now - seconds_ago, timezone.utc).isoformat().replace("+00:00", "Z")


class BreakerCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.clock = FakeClock()
        self.breaker = stopworld.Breaker(self.directory, self.clock)

    def observe(self, code, ident, ago=0.0):
        return self.breaker.observe(code, ident, self.clock.now - ago)


class TripRuleTests(BreakerCase):
    def test_a_spend_limit_trips_at_once(self):
        self.assertTrue(self.observe("spend_limit", "j1"))

    def test_auth_trips_on_the_second_event_within_five_minutes_not_the_first(self):
        self.assertFalse(self.observe("auth", "j1"))
        self.clock.now += 4 * MINUTE
        self.assertTrue(self.observe("auth", "j2"))

    def test_two_auth_events_more_than_five_minutes_apart_do_not_trip(self):
        self.assertFalse(self.observe("auth", "j1"))
        self.clock.now += 5 * MINUTE + 1
        self.assertFalse(self.observe("auth", "j2"))
        self.clock.now += 2 * MINUTE
        self.assertTrue(self.observe("auth", "j3"))  # j2 and j3 are two minutes apart

    def test_one_failure_seen_again_counts_once(self):
        self.assertFalse(self.observe("auth", "j1"))
        for _ in range(5):
            self.assertFalse(self.observe("auth", "j1"))

    def test_an_unclassified_429_storm_trips_at_three_within_five_minutes(self):
        self.assertFalse(self.observe("rate_limit_unknown", "j1"))
        self.clock.now += 2 * MINUTE
        self.assertFalse(self.observe("rate_limit_unknown", "j2"))
        self.clock.now += 2 * MINUTE
        self.assertTrue(self.observe("rate_limit_unknown", "j3"))

    def test_three_unclassified_429s_spread_over_more_than_five_minutes_do_not_trip(self):
        for index in range(3):
            self.assertFalse(self.observe("rate_limit_unknown", f"j{index}"))
            self.clock.now += 3 * MINUTE

    def test_the_counts_of_two_codes_are_not_added(self):
        self.assertFalse(self.observe("auth", "j1"))
        self.assertFalse(self.observe("rate_limit_unknown", "j2"))
        self.assertFalse(self.observe("rate_limit_unknown", "j3"))

    def test_a_rate_limit_with_a_known_retry_after_overloaded_other_and_none_never_trip(self):
        for code in ("rate_limit", "overloaded", "other", "none", "isolation_failed", "bogus"):
            for index in range(20):
                with self.subTest(code=code, index=index):
                    self.assertFalse(self.observe(code, f"{code}{index}"))

    def test_a_failure_older_than_the_freshness_window_is_history(self):
        self.assertFalse(self.observe("spend_limit", "old", ago=stopworld.FRESH_SECONDS + 1))
        self.assertTrue(self.observe("spend_limit", "fresh", ago=stopworld.FRESH_SECONDS - 1))

    def test_the_rules_are_the_documented_ones(self):
        self.assertEqual(stopworld.RULES["spend_limit"], (1, 0))
        self.assertEqual(stopworld.RULES["auth"], (2, 300))
        self.assertEqual(stopworld.RULES["rate_limit_unknown"], (3, 300))
        self.assertEqual(set(stopworld.RULES), {"spend_limit", "auth", "rate_limit_unknown"})
        self.assertEqual(stopworld.KINDS, (*apierror.CODES, "isolation_failed"))


class CountingWindowTests(BreakerCase):
    """The counting rules measure their 5 minutes between the events' own times, not from the time they are read.

    A poll of 300 seconds or more (``FACTORY_MANAGER_POLL_SECONDS`` allows an hour) and a manager restart both read
    a failure late; ``BreakerCase.observe`` reads one that happened ``ago`` seconds before the fake clock.
    """

    def test_two_auth_events_ten_seconds_apart_trip_when_read_together_400_seconds_late(self):
        self.assertFalse(self.observe("auth", "a1", ago=400))
        self.assertTrue(self.observe("auth", "a2", ago=390))

    def test_the_order_the_events_are_read_in_does_not_matter(self):
        self.assertFalse(self.observe("auth", "a2", ago=390))
        self.assertTrue(self.observe("auth", "a1", ago=400))

    def test_read_as_they_happen_or_read_late_the_same_two_events_give_the_same_answer(self):
        self.assertFalse(self.observe("auth", "a1"))
        self.clock.now += 10
        self.assertTrue(self.observe("auth", "a2"))  # in real time
        late = stopworld.Breaker(self.directory, self.clock)
        self.assertFalse(late.observe("auth", "a1", self.clock.now - 400, self.clock.now))
        self.assertTrue(late.observe("auth", "a2", self.clock.now - 390, self.clock.now))  # 10 s apart, read late

    def test_two_auth_events_400_seconds_apart_do_not_trip_in_real_time(self):
        self.assertFalse(self.observe("auth", "a1"))
        self.clock.now += 400
        self.assertFalse(self.observe("auth", "a2"))

    def test_two_auth_events_400_seconds_apart_do_not_trip_read_late(self):
        self.assertFalse(self.observe("auth", "a1", ago=850))
        self.assertFalse(self.observe("auth", "a2", ago=450))
        self.assertFalse(self.observe("auth", "a3", ago=10))  # 440 s after a2, 840 s after a1

    def test_the_window_is_five_minutes_between_the_events_and_the_edge_counts(self):
        edge = stopworld.Breaker(self.directory, self.clock)
        self.assertFalse(edge.observe("auth", "a1", self.clock.now - 600, self.clock.now))
        self.assertTrue(edge.observe("auth", "a2", self.clock.now - 300, self.clock.now))  # exactly 300 s apart
        past = stopworld.Breaker(self.directory, self.clock)
        self.assertFalse(past.observe("auth", "a1", self.clock.now - 600, self.clock.now))
        self.assertFalse(past.observe("auth", "a2", self.clock.now - 299, self.clock.now))  # 301 s apart

    def test_three_unclassified_429s_within_five_minutes_trip_when_read_late(self):
        self.assertFalse(self.observe("rate_limit_unknown", "j1", ago=500))
        self.assertFalse(self.observe("rate_limit_unknown", "j2", ago=400))
        self.assertTrue(self.observe("rate_limit_unknown", "j3", ago=300))

    def test_three_unclassified_429s_within_five_minutes_trip_whatever_the_order_they_are_read_in(self):
        self.assertFalse(self.observe("rate_limit_unknown", "j1", ago=300))
        self.assertFalse(self.observe("rate_limit_unknown", "j2", ago=500))
        self.assertTrue(self.observe("rate_limit_unknown", "j3", ago=400))  # the middle one comes last

    def test_three_unclassified_429s_spread_over_more_than_five_minutes_do_not_trip_read_late(self):
        self.assertFalse(self.observe("rate_limit_unknown", "j1", ago=800))
        self.assertFalse(self.observe("rate_limit_unknown", "j2", ago=450))
        self.assertFalse(self.observe("rate_limit_unknown", "j3", ago=100))  # 700 s from the first to the last

    def test_two_close_unclassified_429s_are_not_three_when_the_third_is_far_off(self):
        self.assertFalse(self.observe("rate_limit_unknown", "j1", ago=600))
        self.assertFalse(self.observe("rate_limit_unknown", "j2", ago=590))
        self.assertFalse(self.observe("rate_limit_unknown", "j3", ago=100))  # 500 s after the first: not three in 5 minutes
        self.assertFalse(self.observe("rate_limit_unknown", "j4", ago=95))
        self.assertTrue(self.observe("rate_limit_unknown", "j5", ago=90))  # j3, j4 and j5 are

    def test_an_event_older_than_the_freshness_window_never_counts_beside_a_close_one(self):
        old = stopworld.FRESH_SECONDS + 1
        self.assertFalse(self.observe("auth", "old1", ago=old + 10))
        self.assertFalse(self.observe("auth", "old2", ago=old))  # ten seconds after old1: both are history
        self.assertFalse(self.observe("auth", "fresh1", ago=old - 5))  # five seconds after old2, which was not counted
        self.assertTrue(self.observe("auth", "fresh2", ago=old - 15))

    def test_old_unclassified_429s_never_count_toward_three(self):
        old = stopworld.FRESH_SECONDS + 1
        for index in range(3):
            self.assertFalse(self.observe("rate_limit_unknown", f"old{index}", ago=old + index))
        self.assertFalse(self.observe("rate_limit_unknown", "fresh1", ago=old - 10))
        self.assertFalse(self.observe("rate_limit_unknown", "fresh2", ago=old - 20))  # two fresh and three old are not three
        self.assertTrue(self.observe("rate_limit_unknown", "fresh3", ago=old - 30))


class MarkerTests(BreakerCase):
    def test_trip_sets_its_own_marker_not_the_manual_pause_marker(self):
        self.assertFalse(self.breaker.tripped())
        self.assertIsNone(self.breaker.state())
        self.assertTrue(self.breaker.trip("spend_limit"))
        self.assertTrue(self.breaker.tripped())
        self.assertEqual(sorted(os.listdir(self.directory)), ["spend-breaker"])
        self.assertNotEqual(stopworld.MARKER, TRIGGER_PAUSE_FILE)
        state = self.breaker.state()
        self.assertEqual(state["code"], "spend_limit")
        self.assertIsNotNone(stopworld.parse_epoch(state["at"]))
        self.assertEqual(oct((self.directory / "spend-breaker").stat().st_mode & 0o777), "0o600")

    def test_a_trip_is_idempotent_and_the_first_code_wins(self):
        self.assertTrue(self.breaker.trip("auth"))
        self.assertFalse(self.breaker.trip("spend_limit"))
        self.assertEqual(self.breaker.state()["code"], "auth")

    def test_only_a_tripping_code_may_set_it_and_isolation_failed_may(self):
        for code in ("rate_limit", "overloaded", "other", "none", "", "x"):
            with self.subTest(code=code), self.assertRaises(ValueError):
                self.breaker.trip(code)
        self.assertFalse(self.breaker.tripped())
        self.assertTrue(self.breaker.trip("isolation_failed"))

    def test_a_new_process_finds_the_pause_where_the_old_one_left_it(self):
        self.breaker.trip("spend_limit")
        again = stopworld.Breaker(self.directory, self.clock)
        self.assertTrue(again.tripped())
        self.assertEqual(again.state()["code"], "spend_limit")
        self.assertFalse(again.trip("spend_limit"))  # not a second trip

    def test_an_unreadable_marker_still_means_tripped(self):
        for content in (b"", b"not json", b"[1]", b'{"code": 7, "at": "x"}', b'{"code": "bogus"}'):
            with self.subTest(content=content):
                (self.directory / "spend-breaker").write_bytes(content)
                self.assertTrue(self.breaker.tripped())
                self.assertEqual(self.breaker.state(), {"code": None, "at": None})
        (self.directory / "spend-breaker").unlink()
        (self.directory / "spend-breaker").symlink_to(self.directory / "elsewhere")
        self.assertTrue(self.breaker.tripped())
        self.assertEqual(self.breaker.state(), {"code": None, "at": None})

    def test_the_status_reads_the_marker_of_the_data_directory_from_the_environment(self):
        self.assertIsNone(stopworld.read_from_env({}))
        self.assertIsNone(stopworld.read_from_env({"FACTORY_DATA": str(self.directory)}))
        self.breaker.trip("auth")
        self.assertEqual(stopworld.read_from_env({"FACTORY_DATA": str(self.directory)})["code"], "auth")

    def test_clearing_removes_the_marker_stamps_the_clear_and_is_idempotent(self):
        self.breaker.trip("spend_limit")
        self.clock.now += 100
        self.assertTrue(self.breaker.clear())
        self.assertFalse(self.breaker.tripped())
        self.assertEqual(sorted(os.listdir(self.directory)), ["spend-breaker-cleared"])
        self.assertFalse(self.breaker.clear())
        self.assertFalse(self.breaker.clear())

    def test_a_failure_recorded_before_the_clear_cannot_trip_it_again_but_a_later_one_can(self):
        """The failed job stays in the run's state after the Operator raised the limit: it must not re-trip."""
        self.assertTrue(self.observe("spend_limit", "j1", ago=30))
        self.breaker.trip("spend_limit")
        self.breaker.clear()
        again = stopworld.Breaker(self.directory, self.clock)  # a restart forgot the identities
        self.assertFalse(again.observe("spend_limit", "j1", self.clock.now - 30, self.clock.now))
        stamp = again.cleared_at()
        self.assertFalse(again.observe("spend_limit", "j1b", stamp, self.clock.now))  # at the clear exactly
        self.assertTrue(again.observe("spend_limit", "j2", stamp + 1, self.clock.now))

    def test_a_clear_forgets_the_events_counted_toward_the_old_trip(self):
        self.assertFalse(self.observe("auth", "j1"))
        self.breaker.trip("auth")
        self.clock.now += 1
        self.breaker.clear()
        self.clock.now += 1
        self.assertFalse(self.observe("auth", "j2"))  # one event after the clear is not two


class OperationsBreakerCase(OpsCase):
    """The operations loop reading ``api_error`` from ``status --json`` of the runs it started."""

    def setUp(self):
        super().setUp()
        self.time.now = time.time()  # the exit rows of the child registry carry the real time
        self.clock = FakeClock(self.time.now)
        self.make_run()
        self.child = self.add_child()

    def failed(self, code, ident="a", ago=10.0, attempt=1, status_="failed"):
        return {
            "id": ident,
            "status": status_,
            "attempt": attempt,
            "error": CANARY,
            "next_action": CANARY,
            "api_error": code,
            "api_error_at": self.clock.stamp(ago),
        }

    def set_report(self, *jobs, repo="org/name", run="r1"):
        self.runner.reports[(repo, run)] = {
            "jobs": list(jobs),
            "cost_usd": 0.0,
            "active_worker_count": 1,
            "supervisor": None,
            "blockers": {},
        }

    def trips(self, code):
        return self.manager.metrics["factory_stop_world_trips_total"].value(code=code)

    def errors(self, code, repo="org/name"):
        return self.manager.metrics["factory_api_errors_total"].value(repo=repo, code=code)

    def drains(self):
        return self.names("drain")


class SpendLimitTripTests(OperationsBreakerCase):
    def test_a_spend_limit_pauses_the_trigger_through_its_own_marker_and_drains_every_active_run(self):
        self.make_run(run="r2", repo="org/other")
        self.add_child(repo="org/other", run="r2")
        self.add_child(kind="append", run="r1")  # an append is not a supervisor: nothing to drain there
        self.set_report(self.failed("spend_limit"))
        self.assertFalse(self.manager.trigger_paused())
        self.ops.check_api_errors()
        self.assertTrue(self.manager.trigger_paused())
        self.assertTrue((self.data_dir / "spend-breaker").is_file())
        self.assertFalse((self.data_dir / TRIGGER_PAUSE_FILE).exists())  # the manual marker is not touched
        self.assertEqual(sorted(self.drains()), [("org/name", "r1"), ("org/other", "r2")])
        for repo, run in (("org/name", "r1"), ("org/other", "r2")):
            self.assertTrue((self.run_dir(run, repo) / "spend-breaker-drained").is_file())
        self.assertEqual(self.trips("spend_limit"), 1)
        self.assertEqual(self.errors("spend_limit"), 1)
        self.assertEqual(self.lines()[0], "breaker code=spend_limit repo=org/name run=r1 action=trip")
        self.assertIn("breaker repo=org/name run=r1 action=drain", self.lines())

    def test_the_trip_drains_in_the_same_pass_and_a_second_pass_does_nothing_more(self):
        self.set_report(self.failed("spend_limit"))
        self.ops.check_api_errors()
        calls = list(self.runner.calls)
        self.ops.check_api_errors()
        self.ops.check_api_errors()
        self.assertEqual(self.runner.calls, calls)  # tripped: nothing is read and nothing is drained twice
        self.assertEqual(self.trips("spend_limit"), 1)
        self.assertEqual(len(self.drains()), 1)

    def test_a_restart_with_the_marker_keeps_the_pause_and_does_not_drain_the_same_process_again(self):
        self.set_report(self.failed("spend_limit"))
        self.ops.check_api_errors()
        second = self.new_manager()
        self.assertTrue(second.trigger_paused())
        second.children.register(self.child)
        self.runner.calls.clear()
        self.new_ops(second).check_api_errors()
        self.assertEqual(self.runner.calls, [])  # the marker in the run directory answered
        self.assertEqual(second.metrics["factory_stop_world_trips_total"].value(code="spend_limit"), 0)

    def test_a_run_started_by_an_operator_after_the_trip_is_drained_again(self):
        self.set_report(self.failed("spend_limit"))
        self.ops.check_api_errors()
        second = self.new_manager()
        replacement = Child("run", "org/name", "r1", tm.FakeProcess(7100), "2999-01-01T00:00:00Z")
        second.children.register(replacement)
        self.new_ops(second).check_api_errors()
        self.assertEqual(len(self.drains()), 2)

    def test_a_failed_drain_is_tried_again_on_the_next_pass(self):
        self.set_report(self.failed("spend_limit"))
        self.runner.override[("drain",)] = failure(1)
        self.ops.check_api_errors()
        self.assertIn("breaker repo=org/name run=r1 action=drain-failed rc=1", self.lines())
        self.assertFalse((self.run_dir() / "spend-breaker-drained").exists())
        del self.runner.override[("drain",)]
        self.ops.check_api_errors()
        self.assertEqual(len(self.drains()), 2)
        self.assertTrue((self.run_dir() / "spend-breaker-drained").is_file())

    def test_a_marker_that_cannot_be_written_is_retried_not_forgotten(self):
        self.set_report(self.failed("spend_limit"))
        with unittest.mock.patch.object(stopworld.Breaker, "trip", side_effect=OSError(13, CANARY)):
            self.ops.check_api_errors()
        self.assertFalse(self.manager.breaker.tripped())
        self.assertTrue(any("cannot write the breaker marker (PermissionError)" in line for line in self.warnings()))
        self.ops.check_api_errors()  # the same failure is not read again, but the pending trip is retried
        self.assertTrue(self.manager.breaker.tripped())
        self.assertEqual(self.trips("spend_limit"), 1)
        self.assertEqual(len(self.drains()), 1)


class CountingTripTests(OperationsBreakerCase):
    def test_auth_trips_on_the_second_failure_within_five_minutes_and_not_on_the_first(self):
        self.set_report(self.failed("auth", "a", ago=200))
        self.ops.check_api_errors()
        self.assertFalse(self.manager.trigger_paused())
        self.assertEqual(self.drains(), [])
        self.set_report(self.failed("auth", "a", ago=200), self.failed("auth", "b", ago=100))
        self.ops.check_api_errors()
        self.assertTrue(self.manager.trigger_paused())
        self.assertEqual(self.trips("auth"), 1)
        self.assertEqual(self.errors("auth"), 2)
        self.assertEqual(len(self.drains()), 1)

    def test_two_auth_failures_more_than_five_minutes_apart_do_not_trip(self):
        self.set_report(self.failed("auth", "a", ago=400), self.failed("auth", "b", ago=60))
        self.ops.check_api_errors()
        self.assertFalse(self.manager.trigger_paused())

    def test_two_auth_failures_ten_seconds_apart_trip_when_a_slow_poll_reads_them_400_seconds_late(self):
        self.set_report(self.failed("auth", "a", ago=400), self.failed("auth", "b", ago=390))
        self.ops.check_api_errors()
        self.assertTrue(self.manager.trigger_paused())
        self.assertEqual((self.trips("auth"), self.errors("auth")), (1, 2))

    def test_the_jobs_of_a_run_are_not_listed_in_time_order_and_the_trip_does_not_depend_on_it(self):
        self.set_report(self.failed("auth", "a", ago=100), self.failed("auth", "b", ago=350))
        self.ops.check_api_errors()
        self.assertTrue(self.manager.trigger_paused())  # b happened first, 250 s before a, and is read after it

    def test_an_unclassified_429_storm_trips_at_three(self):
        self.set_report(*(self.failed("rate_limit_unknown", name, ago=90) for name in ("a", "b")))
        self.ops.check_api_errors()
        self.assertFalse(self.manager.trigger_paused())
        self.set_report(*(self.failed("rate_limit_unknown", name, ago=90) for name in ("a", "b", "c")))
        self.ops.check_api_errors()
        self.assertTrue(self.manager.trigger_paused())
        self.assertEqual(self.manager.breaker.state()["code"], "rate_limit_unknown")

    def test_a_retried_job_is_a_new_event_but_a_job_seen_again_is_not(self):
        self.set_report(self.failed("auth", "a", ago=100, attempt=1))
        self.ops.check_api_errors()
        self.ops.check_api_errors()
        self.assertFalse(self.manager.trigger_paused())  # the same failure twice is one
        self.set_report(self.failed("auth", "a", ago=20, attempt=2))
        self.ops.check_api_errors()
        self.assertTrue(self.manager.trigger_paused())

    def test_rate_limit_overloaded_and_other_only_count_in_a_metric(self):
        for code in ("rate_limit", "overloaded", "other"):
            self.set_report(*(self.failed(code, f"{code}{index}", ago=10 + index) for index in range(6)))
            self.ops.check_api_errors()
        self.assertFalse(self.manager.trigger_paused())
        self.assertEqual(self.drains(), [])
        self.assertEqual([self.errors(code) for code in ("rate_limit", "overloaded", "other")], [6, 6, 6])
        self.assertEqual(self.lines(), [])


class WhatIsReadTests(OperationsBreakerCase):
    def test_a_failure_older_than_the_freshness_window_neither_trips_nor_counts(self):
        self.set_report(self.failed("spend_limit", ago=stopworld.FRESH_SECONDS + 60))
        self.ops.check_api_errors()
        self.assertFalse(self.manager.trigger_paused())
        self.assertEqual(self.errors("spend_limit"), 0)

    def test_a_failure_from_before_the_clear_does_not_trip_again(self):
        self.set_report(self.failed("spend_limit", ago=30))
        self.ops.check_api_errors()
        self.assertTrue(self.manager.trigger_paused())
        self.manager.breaker.clear()
        second = self.new_manager()
        second.children.register(self.child)
        self.new_ops(second).check_api_errors()  # a restart: the failed job is still in the run's state
        self.assertFalse(second.trigger_paused())
        self.assertEqual(len(self.drains()), 1)

    def test_a_run_that_exited_between_two_passes_is_read_once(self):
        self.child.process.returncode = 1
        self.set_report(self.failed("spend_limit"))
        self.ops.check_api_errors()
        self.assertTrue(self.manager.trigger_paused())
        self.assertEqual(self.names("status"), [("org/name", "r1")])
        self.assertEqual(self.drains(), [])  # nothing runs: nothing to drain

    def test_an_exited_run_is_not_read_twice(self):
        self.child.process.returncode = 0
        self.set_report(self.failed("overloaded"))
        self.ops.check_api_errors()
        self.ops.check_api_errors()
        self.assertEqual(self.names("status"), [("org/name", "r1")])

    def test_an_exited_run_whose_status_could_not_be_read_is_tried_again(self):
        self.child.process.returncode = 1
        self.runner.override[("status",)] = failure(1)
        self.ops.check_api_errors()
        self.assertFalse(self.manager.trigger_paused())
        del self.runner.override[("status",)]
        self.set_report(self.failed("spend_limit"))
        self.ops.check_api_errors()
        self.assertTrue(self.manager.trigger_paused())  # not missed because the first read failed
        self.assertEqual(self.names("status"), [("org/name", "r1")] * 2)

    def status_tails(self):
        return [argv[argv.index("status"):] for argv in self.runner.argvs if "status" in argv]

    def test_the_breaker_reads_a_running_run_offline_so_a_tick_asks_github_nothing(self):
        self.set_report(self.failed("overloaded"))
        self.ops.check_api_errors()
        self.assertEqual(self.status_tails(), [["status", "--json", "--offline"]])

    def test_the_breaker_reads_an_exited_run_offline_too(self):
        self.child.process.returncode = 1
        self.set_report(self.failed("overloaded"))
        self.ops.check_api_errors()
        self.assertEqual(self.status_tails(), [["status", "--json", "--offline"]])

    def test_a_plan_or_append_child_is_not_read(self):
        plan = self.add_child(kind="plan", run="r9")
        plan.process.returncode = 0
        self.child.process.returncode = 0
        self.manager.children.reap()
        self.set_report(self.failed("overloaded"))
        self.ops.check_api_errors()
        self.assertEqual(self.names("status"), [("org/name", "r1")])

    def test_only_a_failed_jobs_closed_code_is_read_and_nothing_else_is_trusted(self):
        hostile = [
            self.failed("spend_limit\nrm -rf /", "a"),
            self.failed("../../etc/passwd", "b"),
            self.failed(7, "c"),
            self.failed("none", "d"),
            self.failed(None, "e"),
            {**self.failed("spend_limit", "f"), "api_error_at": "yesterday"},
            {**self.failed("spend_limit", "g"), "api_error_at": None},
            {**self.failed("spend_limit", "h"), "api_error_at": 12345},
            "not a job",
            None,
            [],
        ]
        self.set_report(*hostile)
        self.ops.check_api_errors()
        self.assertFalse(self.manager.trigger_paused())
        self.assertEqual([self.errors(code) for code in apierror.CODES], [0] * len(apierror.CODES))
        self.assertEqual(self.lines(), [])  # CANARY never reached a line (also asserted for every test)

    def test_a_status_that_cannot_be_read_does_nothing(self):
        for broken in (failure(1), text("not json"), text("[1]")):
            self.runner.override[("status",)] = broken
            self.ops.check_api_errors()
        self.runner.override[("status",)] = text(json.dumps({"jobs": "nope"}))
        self.ops.check_api_errors()
        self.assertFalse(self.manager.trigger_paused())

    def test_the_guardrail_tick_runs_the_cost_guardrail_and_the_breaker(self):
        self.policies["org/name"] = ops.Policy(run_budget_usd=5.0)
        self.set_report(self.failed("spend_limit"))
        self.runner.reports[("org/name", "r1")]["cost_usd"] = 9.0
        self.ops.guardrail_pass()
        self.assertTrue(self.manager.trigger_paused())
        self.assertTrue((self.run_dir() / "budget-drained").is_file())
        self.assertTrue((self.run_dir() / "spend-breaker-drained").is_file())
        self.assertEqual(len(self.drains()), 2)  # one for the budget, one for the breaker: two reasons, two markers


class GuardrailThreadTests(OperationsBreakerCase):
    """``Operations.start()`` hands the guardrail thread ``guardrail_pass``: the cost guardrail AND the breaker.

    ``LoopTests`` drives ``_loop`` and the classes above drive the passes, so only a real thread started the way
    ``ThreadTests`` (tests/test_operations.py) starts them shows that the wiring in ``start()`` is the right pass.
    Real threads and events, no sleeping: the guardrail thread's wait lets one interval pass and then says stop.
    """

    GUARDRAIL = "factory-manager-guardrail"

    def setUp(self):
        super().setUp()
        # As ThreadTests: intervals far longer than any test and no trigger thread. The manager of the parent
        # class is replaced so that the settings say so, and the run it set up gets its child again.
        self.manager = self.new_manager(
            lease_wait_seconds=3600.0, poll_seconds=3600.0, janitor_seconds=86400.0, trigger_seconds=0.0
        )
        self.child = self.add_child()
        (self.run_dir() / "state.json").unlink()  # not started: the recovery thread has nothing to read
        self.addCleanup(self.stop_threads)

    def stop_threads(self):
        self.manager.closing.set()
        if self.manager.operations is not None:
            self.assertEqual(self.manager.operations.join(30), 0)

    def test_the_guardrail_thread_runs_the_cost_guardrail_and_then_the_breaker(self):
        self.policies["org/name"] = ops.Policy(run_budget_usd=5.0)
        self.set_report(self.failed("spend_limit"))
        self.runner.reports[("org/name", "r1")]["cost_usd"] = 9.0
        waits, passed = [], threading.Event()

        def wait(seconds):
            if threading.current_thread().name != self.GUARDRAIL:
                return self.manager.closing.wait(seconds)  # the janitor sits in its wait until shutdown
            waits.append(seconds)
            if len(waits) == 1:
                return False  # the first interval is over: one pass
            passed.set()
            return True  # the pass is done: stop

        operations = ops.Operations(
            self.manager, clock=self.time.clock, wait=wait, policy=lambda cfg: self.policies.get(cfg.repo, ops.Policy())
        )
        self.manager.operations = operations
        operations.start()
        self.assertTrue(passed.wait(10))
        self.assertEqual(waits, [3600.0, 3600.0])
        # the breaker half: the marker is set, the trigger is paused and the run is asked to drain for it
        self.assertTrue((self.data_dir / "spend-breaker").is_file())
        self.assertTrue(self.manager.trigger_paused())
        self.assertTrue((self.run_dir() / "spend-breaker-drained").is_file())
        self.assertEqual(self.trips("spend_limit"), 1)
        # the cost guardrail half ran in the same pass
        self.assertTrue((self.run_dir() / "budget-drained").is_file())
        self.assertEqual(self.drains(), [("org/name", "r1")] * 2)  # one for the budget, one for the breaker
        self.assertEqual(self.warnings(), [])


class ManagerApiTests(tm.ServerCase):
    """``POST /spend-breaker``, ``GET /repos`` and the trigger pause as two markers."""

    PAUSE = "/trigger-pause"
    CLEAR = "/spend-breaker"

    def listing(self):
        status_code, payload, _ = self.call("GET", "/repos", principal=tm.VIEWER)
        self.assertEqual(status_code, 200)
        return payload

    def test_repos_reports_the_pause_as_either_marker_and_the_breaker_apart(self):
        payload = self.listing()
        self.assertEqual((payload["trigger_paused"], payload["breaker"]), (False, None))
        self.manager.breaker.trip("spend_limit")
        payload = self.listing()
        self.assertIs(payload["trigger_paused"], True)
        self.assertEqual(payload["breaker"]["code"], "spend_limit")
        self.assertEqual(sorted(payload["breaker"]), ["at", "code"])
        self.assertFalse((self.data_dir / TRIGGER_PAUSE_FILE).exists())

    def test_the_operator_clears_it_and_the_pause_ends_with_it(self):
        self.manager.breaker.trip("auth")
        self.assertEqual(self.call("POST", self.CLEAR, {"clear": True})[:2], (200, {"breaker": None, "trigger_paused": False}))
        self.assertFalse(self.manager.trigger_paused())
        self.assertEqual(self.listing()["breaker"], None)
        self.assertEqual(self.call("POST", self.CLEAR, {"clear": True})[0], 200)  # idempotent

    def test_clearing_it_leaves_a_manual_pause_alone(self):
        self.call("POST", self.PAUSE, {"paused": True})
        self.manager.breaker.trip("auth")
        self.assertEqual(self.call("POST", self.CLEAR, {"clear": True})[:2], (200, {"breaker": None, "trigger_paused": True}))
        self.assertTrue(self.manager.trigger_paused())

    def test_only_an_operator_may_clear_it(self):
        self.manager.breaker.trip("spend_limit")
        for principal in (tm.VIEWER, tm.NO_ROLE, tm.principal_header(["Some.Other.Role"])):
            with self.subTest(principal=principal[:8]):
                self.assertEqual(self.call("POST", self.CLEAR, {"clear": True}, principal=principal)[:2], (403, {"error": "forbidden"}))
        self.assertEqual(self.call("POST", self.CLEAR, {"clear": True}, principal=None)[0], 401)
        self.assertEqual(self.call("GET", self.CLEAR, principal=tm.OPERATOR)[0], 405)
        self.assertTrue(self.manager.breaker.tripped())

    def test_the_body_must_say_clear_true(self):
        self.manager.breaker.trip("spend_limit")
        for body in ({}, {"clear": False}, {"clear": "yes"}, {"clear": 1}, {"clear": None}, {"clear": True, "x": 1}, {"paused": False}, []):
            with self.subTest(body=body):
                self.assertEqual(self.call("POST", self.CLEAR, body)[0], 400)
        self.assertEqual(self.call("POST", self.CLEAR, raw=b"{not json")[0], 400)
        self.assertTrue(self.manager.breaker.tripped())

    def test_every_clear_is_audited_and_runs_no_command(self):
        self.manager.breaker.trip("spend_limit")
        before = list(self.runner.calls)
        self.call("POST", self.CLEAR, {"clear": True})
        self.call("POST", self.CLEAR, {"clear": True}, principal=tm.VIEWER)
        self.assertEqual(self.runner.calls, before)
        lines = self.audit_lines()
        self.assertEqual(
            lines[0], f'audit principal={tm.OID} method=POST path=/spend-breaker status=200 argv=["spend-breaker","clear"]'
        )
        self.assertEqual(lines[1], f"audit principal={tm.OID} method=POST path=/spend-breaker status=403 argv=[]")

    def test_a_marker_that_cannot_be_removed_is_a_500_without_details(self):
        with unittest.mock.patch.object(stopworld.Breaker, "clear", side_effect=PermissionError("secret-path")):
            status_code, payload, _ = self.call("POST", self.CLEAR, {"clear": True})
        self.assertEqual((status_code, payload), (500, {"error": "could not clear the breaker"}))
        self.assertNotIn("secret-path", self.out.getvalue() + self.err.getvalue())

    def test_the_breaker_survives_a_manager_restart(self):
        self.manager.breaker.trip("spend_limit")
        self.serve()
        self.assertTrue(self.manager.trigger_paused())
        self.assertEqual(self.listing()["breaker"]["code"], "spend_limit")

    def test_the_deploy_gates_pause_then_resume_leaves_a_tripped_breaker_set_and_the_gate_succeeds(self):
        """The gate's resume posts ``paused: false`` on every automatic promotion: it clears only the manual marker."""
        self.manager.breaker.trip("spend_limit")
        self.assertEqual(self.call("POST", self.PAUSE, {"paused": True})[:2], (200, {"trigger_paused": True}))
        self.assertTrue((self.data_dir / TRIGGER_PAUSE_FILE).is_file())  # the manual marker is set although the breaker is
        # the reply keeps reporting the manual marker, so the gate's own check (`is paused`) passes
        self.assertEqual(self.call("POST", self.PAUSE, {"paused": False})[:2], (200, {"trigger_paused": False}))
        self.assertFalse((self.data_dir / TRIGGER_PAUSE_FILE).exists())
        self.assertTrue(self.manager.breaker.tripped())
        self.assertTrue(self.manager.trigger_paused())
        self.assertIs(self.listing()["trigger_paused"], True)

    def test_the_manual_marker_and_the_breaker_are_two_files(self):
        self.manager.set_trigger_pause(True)
        self.assertTrue(self.manager.trigger_pause_set())
        self.assertFalse(self.manager.breaker.tripped())
        self.manager.set_trigger_pause(False)
        self.manager.breaker.trip("auth")
        self.assertFalse(self.manager.trigger_pause_set())
        self.assertTrue(self.manager.trigger_paused())

    def test_in_process_starts_are_refused_while_only_the_breaker_is_set_and_an_operator_request_is_not(self):
        self.make_run()
        self.manager.breaker.trip("spend_limit")
        target = self.manager.target_for("org/name", "r1")
        for start in (self.manager.start_run, lambda t: self.manager.start_plan(t, {"issues": [1]})):
            with self.assertRaises(ApiError) as caught:
                start(target)
            self.assertEqual(caught.exception.status, 503)
        self.assertEqual(self.spawner.calls, [])
        self.assertEqual(self.call("POST", self.path("run"))[0], 202)  # an Operator's own request is not blocked


class RecordingTests(unittest.TestCase):
    """The runner records only the closed code and when it was seen; status shows it for a failed job only."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        plan = {"jobs": [{"id": "a", "likely_paths": [], "depends_on": []}]}
        self.state = State(Path(temp.name), plan)
        self.runner = types.SimpleNamespace(state=self.state)
        self.runner.api_error_fields = lambda key, error: Runner.api_error_fields(self.runner, key, error)
        self.runner.park_if_ignored = lambda job: False
        self.runner.settle_stop = lambda key: None

    def run_failure(self, error):
        def boom(job):
            raise error

        self.runner.prepare_worktree = boom
        Runner.execute(self.runner, {"id": "a"}, "build")
        return self.state.data["jobs"]["a"]

    def test_a_failure_with_an_api_error_records_the_code_and_the_time_and_no_text(self):
        record = self.run_failure(AgentError(f"agent failed (subtype=success); {CANARY}", None, "spend_limit"))
        self.assertEqual((record["status"], record["api_error"]), ("failed", "spend_limit"))
        self.assertIsNotNone(stopworld.parse_epoch(record["api_error_at"]))
        self.assertNotIn(CANARY, json.dumps({key: value for key, value in record.items() if key.startswith("api_error")}))

    def test_a_failure_without_one_records_nothing_and_clears_an_earlier_code(self):
        record = self.run_failure(RuntimeError("boom"))
        self.assertNotIn("api_error", record)  # nothing is added to a job that never had one
        self.run_failure(AgentError("x", None, "auth"))
        record = self.run_failure(AgentError("y", None, None))
        self.assertEqual((record["api_error"], record["api_error_at"]), (None, None))

    def test_a_code_outside_the_closed_set_is_not_recorded(self):
        for code in ("none", "bogus", 7, "spend_limit\n"):
            with self.subTest(code=code):
                record = self.run_failure(AgentError("x", None, code))
                self.assertNotEqual(record.get("api_error"), code)


class StatusTests(unittest.TestCase):
    PLAN = {"jobs": [{"id": "a", "likely_paths": [], "depends_on": []}]}

    def row(self, **job):
        data = {"version": 1, "jobs": {"a": {"attempt": 1, **job}}}
        return status.status_report(self.PLAN, data)["jobs"][0]

    def test_a_failed_job_shows_the_code_and_when(self):
        row = self.row(status="failed", error="x", api_error="spend_limit", api_error_at="2026-11-09T10:00:00Z")
        self.assertEqual((row["api_error"], row["api_error_at"]), ("spend_limit", "2026-11-09T10:00:00Z"))
        json.dumps(row)
        report = status.status_report(self.PLAN, {"jobs": {"a": {"status": "failed", "attempt": 1, "api_error": "auth"}}})
        self.assertIn("  API error: auth", status.format_status(report))

    def test_a_job_that_is_not_failed_or_a_code_that_is_not_closed_shows_nothing(self):
        for job in (
            {"status": "queued", "api_error": "spend_limit", "api_error_at": "2026-11-09T10:00:00Z"},
            {"status": "failed", "api_error": "none"},
            {"status": "failed", "api_error": "../x"},
            {"status": "failed", "api_error": 5},
            {"status": "failed"},
        ):
            with self.subTest(job=job):
                row = self.row(**job)
                self.assertEqual((row["api_error"], row["api_error_at"]), (None, None))

    def test_the_report_text_says_when_the_breaker_is_tripped(self):
        report = status.status_report(self.PLAN, {"jobs": {"a": {"status": "queued", "attempt": 0}}})
        self.assertNotIn("breaker", status.format_status(report))
        report["breaker"] = {"code": "spend_limit", "at": "2026-11-09T10:00:00Z"}
        text = status.format_status(report)
        self.assertIn("Stop-the-world breaker: tripped (spend_limit) since 2026-11-09T10:00:00Z", text)
        report["breaker"] = {"code": None, "at": None}
        self.assertIn("tripped (unknown);", status.format_status(report))


class CliStatusTests(unittest.TestCase):
    """``status --json`` carries the breaker of the data directory, like the pod budget."""

    def status_json(self, data):
        from tests.test_factory import assignment

        run = data.parent / "run1"
        run.mkdir(exist_ok=True)
        plan = assignment()
        (run / "plan.json").write_text(json.dumps(plan))
        State(run, plan)
        source = str(Path(factory.__file__).resolve().parent.parent)
        env = {**os.environ, "FACTORY_DATA": str(data), "PYTHONPATH": source}
        done = subprocess.run(
            [sys.executable, "-m", "factory", "--run-dir", str(run), "status", "--offline", "--json"],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        return json.loads(done.stdout)

    def test_the_report_shows_the_breaker_only_while_it_is_tripped(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp) / "data"
            data.mkdir()
            self.assertIsNone(self.status_json(data)["breaker"])
            stopworld.Breaker(data).trip("spend_limit")
            report = self.status_json(data)
            self.assertEqual(report["breaker"]["code"], "spend_limit")
            self.assertEqual(sorted(report["breaker"]), ["at", "code"])


if __name__ == "__main__":
    unittest.main()

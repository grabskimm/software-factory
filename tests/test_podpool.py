"""The pod pool (slice 18): ceiling, pacing, reserve-on-admit, crash recovery and the call contract.

CONTRACT-TESTED AGAINST ASSUMED SHAPES: the launcher is a fake with an ``info`` method (the real one answers the same
two numbers over a socket, ``tests/test_launcher.py``), the state directory is a temporary directory on the test
machine's file system, and time is a fake clock. Cross-process behaviour is real: child processes hold real ``flock``s
and are killed. Nothing here starts a pod, calls Azure, Kubernetes or Anthropic, or sleeps for pacing.
"""

import ast
import contextlib
import errno
import gc
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from factory import capacity, podexecution, podpool, stopworld
from tests import test_launcher
from factory import podcontract as pc
from factory.podpool import (
    BUDGET,
    BUDGET_HELD,
    CEILING,
    PACING,
    QUEUED,
    REPO_CAP,
    ConfigRefused,
    Lost,
    PodPool,
    Refused,
    Request,
    Timeout,
    Unavailable,
)

SRC = Path(__file__).resolve().parent.parent / "src"
POOL_SOURCE = SRC / "factory" / "podpool.py"


class Clock:
    """A settable wall clock; one test advances it, the pool reads it."""

    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def request(repo="org/app", run="run-1", job="job-1", phase="build", attempt=0, estimate=5.0, timeout=3600, **extra):
    return Request(repo, run, job, phase, attempt, estimate, timeout, **extra)


class PoolCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.clock = Clock()
        self.held = []
        # Every notice and log line of a test is captured: a test of the pool never writes to the terminal.
        patcher = patch.object(capacity, "_NOTICED", set())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.err = io.StringIO()
        redirect = contextlib.redirect_stderr(self.err)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        self.addCleanup(self.release_all)

    def release_all(self):
        for reservation in self.held:
            reservation.release()

    def pool(self, ceiling=8, **kwargs):
        kwargs.setdefault("burst", 100)  # pacing is its own tests' business
        kwargs.setdefault("clock", self.clock)
        return PodPool(self.root / "pods", ceiling, **kwargs)

    def take(self, pool, **kwargs):
        admission = pool.admit(request(**kwargs))
        self.assertIsNone(admission.refusal, admission.refusal)
        self.held.append(admission.reservation)
        return admission.reservation

    def refusal(self, pool, **kwargs):
        admission = pool.admit(request(**kwargs))
        self.assertIsNone(admission.reservation)
        return admission.refusal

    def files(self):
        return sorted(path.name for path in (self.root / "pods").glob("r-*"))


# ---------------------------------------------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------------------------------------------


class RequestTests(unittest.TestCase):
    def test_a_valid_request_has_exact_micro_dollar_amounts_rounded_the_safe_way(self):
        value = request(estimate=0.1, spent_usd=0.2, budget_usd=0.3)
        self.assertEqual((value.estimate_micro, value.spent_micro, value.budget_micro), (100000, 200000, 300000))
        self.assertEqual(request(estimate=0.1).estimate_micro + request(estimate=0.2).estimate_micro, 300000)
        # Spent and the estimate round up, the budget down: an amount is never understated in the pool's favour.
        value = request(estimate=0.0000001, spent_usd=0.0000001, budget_usd=0.9999999)
        self.assertEqual((value.estimate_micro, value.spent_micro, value.budget_micro), (1, 1, 999999))

    def test_every_invalid_field_is_a_value_error(self):
        bad = {
            "repo": ["", "no-slash", "org/", "a/b/c", "../etc/passwd", None, 5],
            "run": ["", "-x", "a b", "r" * 51, None],
            "job": ["", "j/k", None],
            "phase": ["", "deploy", None, "Build"],
            "attempt": [-1, 10000, True, 1.5, "1", None],
            "estimate": [0, -1, float("nan"), float("inf"), "5", True, None, 100001, 10**30, 10**400, -(10**400)],
            "timeout": [0, -5, True, 1.5, "60", None],
            "spent_usd": [-0.01, float("nan"), float("inf"), "1", True, 100001, 10**400],
            "budget_usd": [0, -3, float("nan"), float("inf"), "1", True, 0.0000001, 100001, 10**400],
        }
        for field, values in bad.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    request(**{field: value})

    def test_an_integer_beyond_the_float_range_is_a_value_error_not_an_overflow(self):
        for rounding in (podpool.decimal.ROUND_CEILING, podpool.decimal.ROUND_FLOOR):
            for value in (10**400, -(10**400)):
                with self.subTest(value=value), self.assertRaises(ValueError) as caught:
                    podpool._micros(value, rounding)
                self.assertEqual(str(caught.exception), "invalid amount")

    def test_an_error_never_echoes_the_value(self):
        marker = "SECRET ISSUE TEXT!"  # not a valid repository, run, job or phase
        for field in ("repo", "run", "job", "phase"):
            with self.subTest(field=field), self.assertRaises(ValueError) as caught:
                request(**{field: marker})
            self.assertNotIn(marker, str(caught.exception))

    def test_it_is_built_from_the_closed_launch_request_of_the_launcher(self):
        launch = {
            "session_id": "0" * 32,
            "repository": "org/app",
            "run_id": "run-1",
            "job_id": "job-1",
            "attempt": 2,
            "phase": "review",
            "resource_class": "standard",
            "timeout_seconds": 900,
            "manager_instance": "1" * 12,
        }
        built = Request.for_launch(launch, 4.0, spent_usd=1.5, budget_usd=10)
        self.assertEqual(built, Request("org/app", "run-1", "job-1", "review", 2, 4.0, 900, 1.5, 10))
        self.assertEqual(set(launch) - {"session_id", "resource_class", "manager_instance"}, {"repository", "run_id", "job_id", "attempt", "phase", "timeout_seconds"})

    def test_the_hold_is_the_pod_deadline_plus_the_two_graces_and_never_more_than_the_maximum(self):
        short = request(timeout=100)
        self.assertEqual(short.hold_seconds, 100 + pc.DEADLINE_MARGIN_SECONDS + pc.TERMINATION_GRACE_SECONDS + pc.LAUNCH_GRACE_SECONDS)
        for timeout in (1, 3600, pc.DEADLINE_CLAMP_SECONDS, 10**7):
            self.assertLessEqual(request(timeout=timeout).hold_seconds, podpool.MAX_HOLD_SECONDS)

    def test_a_session_is_identified_by_repository_run_job_phase_and_attempt(self):
        base = request().identity
        for change in ({"repo": "org/other"}, {"run": "run-2"}, {"job": "job-2"}, {"phase": "review"}, {"attempt": 1}):
            self.assertNotEqual(request(**change).identity, base, change)
        self.assertEqual(request(estimate=9).identity, base, "the amounts are not the identity")


class ConfigTests(PoolCase):
    def test_the_ceiling_is_a_plain_integer_and_anything_else_refuses(self):
        for good, expected in (("8", 8), ("0", 0), ("1", 1), ("00008", 8), ("1024", 1024)):
            self.assertEqual(podpool.parse_ceiling({podpool.ENV_CEILING: good}), expected)
        for bad in ("abc", "-1", "8.0", " 8", "8 ", "8\n", "1025", "99999", "100000", "0x10", "٣", "8,9", "١٢"):
            with self.subTest(value=bad), self.assertRaises(ConfigRefused) as caught:
                podpool.parse_ceiling({podpool.ENV_CEILING: bad})
            self.assertEqual(caught.exception.code, "ceiling_invalid")
            self.assertNotIn(bad.strip() or "x", str(caught.exception).replace("ceiling_invalid", "")) if bad.strip() else None
        for unset in ({}, {podpool.ENV_CEILING: ""}):
            with self.assertRaises(ConfigRefused) as caught:
                podpool.parse_ceiling(unset)
            self.assertEqual(caught.exception.code, "ceiling_unset")

    def test_the_switch_off_is_inert_and_creates_nothing(self):
        for env in (
            {},
            {"FACTORY_POD_BACKEND": "off"},
            {"FACTORY_POD_BACKEND": "ON"},
            {"FACTORY_POD_BACKEND": "true"},
            {"FACTORY_POD_BACKEND": "on ", "FACTORY_DATA": str(self.root), podpool.ENV_CEILING: "8"},
            {"FACTORY_DATA": str(self.root), podpool.ENV_CEILING: "8"},
        ):
            with self.subTest(env=env):
                self.assertIsNone(podpool.from_env(env))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_the_switch_reads_exactly_as_the_execution_setting_does(self):
        for value in ("on", "off", "ON", "On", "1", "true", "", " on", "on\n", None):
            env = {} if value is None else {"FACTORY_POD_BACKEND": value}
            self.assertEqual(podpool.from_env({**env, "FACTORY_DATA": str(self.root), podpool.ENV_CEILING: "3"}) is not None, podexecution.switch_is_on(env), value)

    def test_the_switch_on_builds_the_pool_from_the_environment(self):
        env = {"FACTORY_POD_BACKEND": "on", "FACTORY_DATA": str(self.root), podpool.ENV_CEILING: "3"}
        pool = podpool.from_env(env, clock=self.clock)
        self.assertEqual((pool.ceiling, pool.directory, pool.breaker_dir), (3, self.root / ".capacity" / "pods", str(self.root)))
        self.assertTrue(pool.directory.is_dir())
        self.assertEqual((pool.per_repo_cap, pool.burst, pool.interval), (pc.PER_REPO_CAP, pc.PACING_BURST, pc.PACING_INTERVAL_SECONDS))
        explicit = podpool.from_env({**env, "FACTORY_CAPACITY_DIR": str(self.root / "elsewhere")})
        self.assertEqual(explicit.directory, self.root / "elsewhere" / "pods")

    def test_the_switch_on_without_a_usable_guard_refuses_instead_of_running_unguarded(self):
        base = {"FACTORY_POD_BACKEND": "on", "FACTORY_DATA": str(self.root), podpool.ENV_CEILING: "3"}
        cases = {
            "ceiling_unset": {k: v for k, v in base.items() if k != podpool.ENV_CEILING},
            "ceiling_invalid": {**base, podpool.ENV_CEILING: "many"},
            "state_dir": {k: v for k, v in base.items() if k != "FACTORY_DATA"},
        }
        for code, env in cases.items():
            with self.subTest(code=code), self.assertRaises(ConfigRefused) as caught:
                podpool.from_env(env)
            self.assertEqual(caught.exception.code, code)
        blocked = self.root / "file"
        blocked.write_text("x")
        with self.assertRaises(ConfigRefused) as caught:
            podpool.from_env({**base, "FACTORY_DATA": str(blocked)})
        self.assertEqual(caught.exception.code, "state_dir")

    def test_the_switch_on_without_a_data_directory_refuses_even_when_the_ledger_has_its_own(self):
        # The ledger directory may be FACTORY_CAPACITY_DIR; the stop-the-world marker lives only under FACTORY_DATA. A pool
        # built without it would never consult the breaker, so it is not built.
        base = {"FACTORY_POD_BACKEND": "on", "FACTORY_CAPACITY_DIR": str(self.root / "ledger"), podpool.ENV_CEILING: "3"}
        for env in (base, {**base, "FACTORY_DATA": ""}):
            with self.subTest(env=env), self.assertRaises(ConfigRefused) as caught:
                podpool.from_env(env)
            self.assertEqual(caught.exception.code, "state_dir")
        self.assertFalse((self.root / "ledger").exists(), "a refused configuration creates nothing")
        pool = podpool.from_env({**base, "FACTORY_DATA": str(self.root)})
        self.assertEqual((pool.directory, pool.breaker_dir), (self.root / "ledger" / "pods", str(self.root)))

    def test_a_poisoned_environment_is_never_echoed(self):
        marker = "SECRETVALUE"
        with self.assertRaises(ConfigRefused) as caught:
            podpool.parse_ceiling({podpool.ENV_CEILING: marker})
        self.assertNotIn(marker, str(caught.exception))

    def test_the_constructor_refuses_nonsense(self):
        for kwargs in (
            {"ceiling": -1},
            {"ceiling": 1025},
            {"ceiling": True},
            {"ceiling": 2.0},
            {"ceiling": 2, "per_repo_cap": 0},
            {"ceiling": 2, "burst": 0},
            {"ceiling": 2, "interval": 0},
            {"ceiling": 2, "interval": True},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                PodPool(self.root / "x", **kwargs)

    def test_the_defaults_are_the_contracts(self):
        pool = PodPool(self.root / "x", 8)
        self.assertEqual((pool.per_repo_cap, pool.burst, pool.interval), (pc.PER_REPO_CAP, pc.PACING_BURST, pc.PACING_INTERVAL_SECONDS))
        self.assertEqual((pc.PER_REPO_CAP, pc.PACING_BURST, pc.PACING_INTERVAL_SECONDS), (4, 2, 15))

    def test_the_refusal_codes_are_a_closed_set_with_a_fixed_text_each(self):
        self.assertEqual(
            podpool.REFUSALS,
            ("budget_held", "repo_cap", "ceiling", "pacing", "queued", "breaker", "duplicate", "invalid", "unavailable", "budget"),
        )
        self.assertEqual(set(podpool.REFUSALS), set(podpool._TEXT))
        self.assertFalse(set(podpool.WAITING) & set(podpool.FINAL))
        for code in podpool.REFUSALS:
            self.assertEqual(podpool.Refusal(code).waiting, code in podpool.WAITING)
            self.assertEqual(str(Refused(code)), podpool._TEXT[code])


# ---------------------------------------------------------------------------------------------------------------
# The ceiling, the per-repository cap and the launcher's count
# ---------------------------------------------------------------------------------------------------------------


class CeilingTests(PoolCase):
    def test_the_ceiling_admits_that_many_sessions_and_refuses_the_next(self):
        pool = self.pool(3, per_repo_cap=10)
        for index in range(3):
            self.take(pool, job=f"j{index}")
        refusal = self.refusal(pool, job="j3")
        self.assertEqual((refusal.code, refusal.in_use, refusal.ceiling), (CEILING, 3, 3))
        self.assertTrue(refusal.waiting)
        self.assertEqual(refusal.text, "pod pool: the pod ceiling is reached (3/3 sessions)")

    def test_a_release_frees_exactly_one_slot_and_a_second_release_frees_nothing(self):
        pool = self.pool(2, per_repo_cap=10)
        first = self.take(pool, job="a")
        self.take(pool, job="b")
        self.assertEqual(self.refusal(pool, job="c").code, CEILING)
        first.release()
        first.release()
        self.take(pool, job="c")
        self.assertEqual(self.refusal(pool, job="d").code, CEILING)

    def test_a_ceiling_of_zero_refuses_everything_and_is_a_pause(self):
        pool = self.pool(0)
        refusal = self.refusal(pool)
        self.assertEqual((refusal.code, refusal.in_use, refusal.ceiling), (CEILING, 0, 0))
        self.assertEqual(self.files(), [])

    def test_a_ceiling_of_one_is_exclusive(self):
        pool = self.pool(1)
        reservation = self.take(pool)
        self.assertEqual(self.refusal(pool, job="other").code, CEILING)
        reservation.release()
        self.take(pool, job="other")

    def test_a_ceiling_lowered_below_the_live_count_refuses_until_enough_end(self):
        wide = self.pool(4, per_repo_cap=10)
        reservations = [self.take(wide, job=f"j{index}") for index in range(3)]
        narrow = self.pool(2, per_repo_cap=10)
        self.assertEqual(self.refusal(narrow, job="new").code, CEILING)
        reservations[0].release()
        self.assertEqual(self.refusal(narrow, job="new").code, CEILING, "2 live of 2 allowed")
        reservations[1].release()
        self.take(narrow, job="new")

    def test_the_per_repository_cap_holds_below_the_ceiling_and_is_per_repository(self):
        pool = self.pool(8, per_repo_cap=2)
        self.take(pool, job="a")
        self.take(pool, job="b")
        refusal = self.refusal(pool, job="c")
        self.assertEqual((refusal.code, refusal.in_use, refusal.ceiling), (REPO_CAP, 2, 2))
        self.take(pool, repo="org/other", job="c")

    def test_the_per_repository_cap_counts_the_repositories_case_exactly(self):
        pool = self.pool(8, per_repo_cap=1)
        self.take(pool, repo="org/app", job="a")
        self.take(pool, repo="org/App", job="a")  # a different string: the ledger does not fold case, the launcher's labels do
        self.assertEqual(self.refusal(pool, repo="org/app", job="b").code, REPO_CAP)

    def test_the_launchers_count_is_a_floor_for_the_ceiling(self):
        count = [0]
        pool = self.pool(3, live_count=lambda: count[0], per_repo_cap=10)
        self.take(pool, job="a")
        count[0] = 3  # two pods nobody here holds a reservation for (a crashed supervisor's)
        refusal = self.refusal(pool, job="b")
        self.assertEqual((refusal.code, refusal.in_use), (CEILING, 3))
        count[0] = 2
        self.take(pool, job="b")  # max(2 held, 2 launcher) = 2 < 3
        count[0] = 3
        self.assertEqual(self.refusal(pool, job="c").code, CEILING)
        count[0] = 0  # the launcher says nothing is live: the reservations alone still count
        self.take(pool, job="c")
        self.assertEqual(self.refusal(pool, job="d").code, CEILING)

    def test_the_launchers_count_is_not_added_to_the_reservations_that_cover_the_same_pods(self):
        pool = self.pool(4, live_count=lambda: 2, per_repo_cap=10)
        self.take(pool, job="a")
        self.take(pool, job="b")  # two reservations and two live pods are the SAME two sessions
        self.take(pool, job="c")  # a third: max(3, 2) = 3 < 4
        self.take(pool, job="d")
        self.assertEqual(self.refusal(pool, job="e").code, CEILING)

    def test_an_unreadable_launcher_count_refuses_and_creates_nothing(self):
        def broken():
            raise OSError("socket")

        for reader in (broken, lambda: None, lambda: -1, lambda: True, lambda: "3", lambda: 2.5, lambda: [1]):
            pool = self.pool(4, live_count=reader)
            self.assertEqual(self.refusal(pool).code, podpool.UNAVAILABLE)
            self.assertEqual(self.files(), [])
        self.assertNotIn("socket", self.err.getvalue())

    def test_the_launcher_adapter_adds_live_and_inflight_and_rejects_anything_else(self):
        class Client:
            def __init__(self, info):
                self.info_value = info

            def info(self):
                if isinstance(self.info_value, Exception):
                    raise self.info_value
                return self.info_value

        self.assertEqual(podpool.launcher_live_count(Client({"live": 3, "inflight": 2, "max_pods": 8}))(), 5)
        for bad in (
            {"live": None, "inflight": 0},
            {"live": 1},
            {"inflight": 1},
            {"live": True, "inflight": 0},
            {"live": -1, "inflight": 0},
            {"live": "1", "inflight": 0},
            {},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                podpool.launcher_live_count(Client(bad))()
        pool = self.pool(4, live_count=podpool.launcher_live_count(Client({"live": None, "inflight": 0})))
        self.assertEqual(self.refusal(pool).code, podpool.UNAVAILABLE, "a launcher that cannot list is not a count of zero")
        pool = self.pool(4, live_count=podpool.launcher_live_count(Client(ConnectionError("down"))))
        self.assertEqual(self.refusal(pool).code, podpool.UNAVAILABLE)

    def test_no_launcher_count_is_asked_when_the_request_is_invalid_or_the_breaker_is_tripped(self):
        calls = []
        pool = self.pool(4, live_count=lambda: calls.append(1) or 0, breaker_dir=str(self.root))
        self.assertEqual(pool.admit("not a request").refusal.code, podpool.INVALID)
        (self.root / stopworld.MARKER).write_text("{}")
        self.assertEqual(self.refusal(pool).code, podpool.BREAKER)
        self.assertEqual(calls, [])


class BreakerTests(PoolCase):
    def test_a_tripped_breaker_refuses_and_a_cleared_one_admits(self):
        pool = self.pool(4, breaker_dir=str(self.root))
        marker = self.root / stopworld.MARKER
        marker.write_text(json.dumps({"code": "spend_limit", "at": "2026-11-09T10:00:00Z"}))
        refusal = self.refusal(pool)
        self.assertEqual(refusal.code, podpool.BREAKER)
        self.assertFalse(refusal.waiting)
        self.assertEqual(self.files(), [])
        marker.unlink()
        self.take(pool)

    def test_an_unreadable_marker_still_means_tripped(self):
        pool = self.pool(4, breaker_dir=str(self.root))
        (self.root / stopworld.MARKER).write_bytes(b"\xff\x00garbage")
        self.assertEqual(self.refusal(pool).code, podpool.BREAKER)

    def test_without_a_breaker_directory_it_is_not_consulted(self):
        (self.root / stopworld.MARKER).write_text("{}")
        self.take(self.pool(4))

    def test_a_running_session_is_not_affected_by_a_trip_and_adoption_is_never_refused_for_it(self):
        pool = self.pool(4, breaker_dir=str(self.root))
        running = self.take(pool)
        (self.root / stopworld.MARKER).write_text("{}")
        self.assertEqual(self.refusal(pool, job="other").code, podpool.BREAKER)
        adopted = pool.adopt(request(job="found"))
        self.assertIsNotNone(adopted.reservation, "a pod that exists is counted whatever the breaker says")
        self.held.append(adopted.reservation)
        running.release()


# ---------------------------------------------------------------------------------------------------------------
# Pacing
# ---------------------------------------------------------------------------------------------------------------


class PacingTests(PoolCase):
    def paced(self, ceiling=50):
        return PodPool(self.root / "pods", ceiling, per_repo_cap=50, burst=2, interval=15, clock=self.clock)

    def test_a_burst_of_two_then_one_per_interval(self):
        pool = self.paced()
        self.take(pool, job="a")
        self.take(pool, job="b")
        refusal = self.refusal(pool, job="c")
        self.assertEqual((refusal.code, refusal.retry_after), (PACING, 15))
        self.assertTrue(refusal.waiting)
        self.assertIn("retry in 15 s", refusal.text)
        self.clock.advance(14)
        self.assertEqual(self.refusal(pool, job="c").retry_after, 1)
        self.clock.advance(1)
        self.take(pool, job="c")
        self.assertEqual(self.refusal(pool, job="d").retry_after, 15)

    def test_the_bucket_refills_gradually_and_never_beyond_the_burst(self):
        pool = self.paced()
        for name in "ab":
            self.take(pool, job=name)
        self.clock.advance(7.5)  # half a token
        self.assertEqual(self.refusal(pool, job="c").retry_after, 8)
        self.clock.advance(7.5 + 15 * 100)  # a long quiet time still gives two tokens, not a hundred
        for name in "cd":
            self.take(pool, job=name)
        self.assertEqual(self.refusal(pool, job="e").code, PACING)

    def test_a_refused_request_consumes_no_token(self):
        pool = PodPool(self.root / "pods", 1, per_repo_cap=50, burst=2, interval=15, clock=self.clock)
        self.take(pool, job="a")  # one token left, the ceiling is full
        for _ in range(10):
            self.assertEqual(self.refusal(pool, job="b").code, CEILING)
        self.held.pop().release()
        self.take(pool, job="b").release()  # the second token was still there
        self.assertEqual(self.refusal(pool, job="c").code, PACING)

    def test_a_budget_or_cap_refusal_consumes_no_token(self):
        pool = self.paced()
        for _ in range(5):
            self.assertEqual(self.refusal(pool, job="x", estimate=50, budget_usd=10).code, BUDGET)
        self.take(pool, job="a")
        self.take(pool, job="b")

    def test_cancel_gives_the_token_back_only_when_no_request_was_sent(self):
        pool = self.paced()
        first = self.take(pool, job="a")
        second = self.take(pool, job="b")
        self.assertEqual(self.refusal(pool, job="c").code, PACING)
        first.cancel()  # never requested: refunded
        third = self.take(pool, job="c")
        self.assertEqual(self.refusal(pool, job="d").code, PACING)
        second.requested()
        second.cancel()  # the launcher may have been asked: the token stays spent
        self.assertEqual(self.refusal(pool, job="d").code, PACING)
        third.cancel()
        self.take(pool, job="d")

    def test_a_refund_never_exceeds_the_burst(self):
        pool = self.paced()
        reservation = self.take(pool, job="a")
        self.clock.advance(15 * 10)
        reservation.cancel()
        saved = json.loads((self.root / "pods" / podpool.PACING_NAME).read_text())
        self.assertLessEqual(saved["tokens"], pool.burst, "the file never holds more than the burst")
        self.take(pool, job="b")
        self.take(pool, job="c")
        self.assertEqual(self.refusal(pool, job="d").code, PACING)

    def test_cancel_and_release_are_idempotent_and_a_second_cancel_refunds_nothing(self):
        pool = self.paced()
        first = self.take(pool, job="a")
        self.take(pool, job="b")
        first.cancel()
        first.cancel()
        first.release()
        self.take(pool, job="c")
        self.assertEqual(self.refusal(pool, job="d").code, PACING)

    def test_a_clock_that_goes_backwards_neither_refills_nor_freezes_the_bucket(self):
        pool = self.paced()
        self.take(pool, job="a")
        self.take(pool, job="b")
        self.clock.advance(-3600)
        self.assertEqual(self.refusal(pool, job="c").code, PACING, "going back refunds nothing")
        self.clock.advance(14)
        self.assertEqual(self.refusal(pool, job="c").retry_after, 1)
        self.clock.advance(1)
        self.take(pool, job="c")  # and the bucket did not wait for the old time to come round again

    def test_a_missing_bucket_is_full_and_a_corrupt_one_is_empty_and_refills(self):
        pool = self.paced()
        self.assertFalse((self.root / "pods" / podpool.PACING_NAME).exists())
        self.take(pool, job="a")
        for content in ("", "not json", "[]", '{"tokens": "x", "at": 1}', '{"tokens": NaN, "at": 1}', '{"tokens": -5, "at": 1}', '{"tokens": 1e99, "at": 1}', '{"tokens": 1}'):
            (self.root / "pods" / podpool.PACING_NAME).write_text(content)
            refusal = self.refusal(pool, job="x")
            self.assertEqual((refusal.code, refusal.retry_after), (PACING, 15), content)
        self.clock.advance(15)
        self.take(pool, job="x")  # the request that was refused is the one waiting, and it goes first

    def test_a_lowered_burst_caps_a_saved_bucket(self):
        wide = PodPool(self.root / "pods", 50, per_repo_cap=50, burst=10, interval=15, clock=self.clock)
        self.take(wide, job="a")  # 9 saved
        narrow = self.paced()
        self.take(narrow, job="b")
        self.take(narrow, job="c")
        self.assertEqual(self.refusal(narrow, job="d").code, PACING)

    def test_an_unwritable_bucket_refuses_and_leaves_no_reservation_behind(self):
        pool = self.paced()
        with patch.object(podpool.os, "replace", side_effect=OSError("read-only")):
            self.assertEqual(self.refusal(pool).code, podpool.UNAVAILABLE)
        self.assertEqual(self.files(), [])
        self.assertEqual([path.name for path in (self.root / "pods").glob("*.tmp")], [], "the temporary file is removed")
        self.take(pool)

    def test_adoption_takes_no_token(self):
        pool = self.paced()
        for name in "abc":
            admission = pool.adopt(request(job=name))
            self.held.append(admission.reservation)
        self.take(pool, job="d")
        self.take(pool, job="e")


# ---------------------------------------------------------------------------------------------------------------
# Reserve-on-admit: the spend
# ---------------------------------------------------------------------------------------------------------------


class BudgetTests(PoolCase):
    def test_spent_plus_reserved_plus_the_estimate_must_fit_the_budget_exactly(self):
        pool = self.pool(8)
        self.take(pool, job="a", estimate=4, spent_usd=2, budget_usd=10)
        self.take(pool, job="b", estimate=4, spent_usd=2, budget_usd=10)  # 2 + 4 + 4 = 10: fits exactly
        refusal = self.refusal(pool, job="c", estimate=0.01, spent_usd=2, budget_usd=10)
        self.assertEqual(refusal.code, BUDGET_HELD)
        self.assertTrue(refusal.waiting)

    def test_a_run_that_cannot_fit_even_alone_is_a_final_refusal(self):
        pool = self.pool(8)
        refusal = self.refusal(pool, estimate=5, spent_usd=6, budget_usd=10)
        self.assertEqual(refusal.code, BUDGET)
        self.assertFalse(refusal.waiting)
        self.assertEqual(self.refusal(pool, estimate=10.01, budget_usd=10).code, BUDGET)
        self.assertEqual(self.refusal(pool, estimate=5, spent_usd=10, budget_usd=10).code, BUDGET)
        self.assertEqual(self.files(), [])

    def test_a_release_gives_the_estimate_back(self):
        pool = self.pool(8)
        first = self.take(pool, job="a", estimate=6, budget_usd=10)
        self.assertEqual(self.refusal(pool, job="b", estimate=6, budget_usd=10).code, BUDGET_HELD)
        first.release()
        self.take(pool, job="b", estimate=6, budget_usd=10)

    def test_a_cancel_gives_the_estimate_back_too(self):
        pool = self.pool(8)
        first = self.take(pool, job="a", estimate=6, budget_usd=10)
        first.cancel()
        self.take(pool, job="b", estimate=6, budget_usd=10)

    def test_only_the_same_runs_reservations_count_against_its_budget(self):
        pool = self.pool(8, per_repo_cap=10)
        self.take(pool, run="run-1", job="a", estimate=9, budget_usd=10)
        self.take(pool, run="run-2", job="a", estimate=9, budget_usd=10)
        self.take(pool, repo="org/other", run="run-1", job="a", estimate=9, budget_usd=10)
        self.assertEqual(self.refusal(pool, run="run-1", job="b", estimate=2, budget_usd=10).code, BUDGET_HELD)

    def test_without_a_budget_there_is_no_spend_limit_but_the_estimate_is_still_recorded(self):
        pool = self.pool(8, per_repo_cap=10)
        self.take(pool, job="a", estimate=1000)
        self.take(pool, job="b", estimate=1000)
        self.assertEqual(pool.snapshot()["reserved_usd"], 2000)

    def test_the_arithmetic_is_exact_where_floats_are_not(self):
        pool = self.pool(8, per_repo_cap=10)
        self.take(pool, job="a", estimate=0.1, budget_usd=0.3)
        self.take(pool, job="b", estimate=0.2, budget_usd=0.3)  # 0.1 + 0.2 > 0.3 in binary floating point
        self.assertEqual(self.refusal(pool, job="c", estimate=0.01, budget_usd=0.3).code, BUDGET_HELD)

    def test_a_request_whose_budget_changed_is_judged_by_its_own_numbers(self):
        pool = self.pool(8, per_repo_cap=10)
        self.take(pool, job="a", estimate=8, budget_usd=100)
        self.assertEqual(self.refusal(pool, job="b", estimate=3, budget_usd=10).code, BUDGET_HELD)
        self.take(pool, job="b", estimate=3, budget_usd=100)

    def test_the_budget_is_checked_before_the_slot_so_a_final_refusal_is_not_hidden_by_a_full_pool(self):
        pool = self.pool(1)
        self.take(pool)
        self.assertEqual(self.refusal(pool, job="b", estimate=50, budget_usd=10).code, BUDGET)

    def test_an_adoption_is_counted_against_the_budget_even_when_it_does_not_fit(self):
        pool = self.pool(8, per_repo_cap=10)
        adopted = pool.adopt(request(job="found", estimate=20, spent_usd=0, budget_usd=10))
        self.held.append(adopted.reservation)
        self.assertEqual(self.refusal(pool, job="b", estimate=1, budget_usd=10).code, BUDGET_HELD)


# ---------------------------------------------------------------------------------------------------------------
# Fairness
# ---------------------------------------------------------------------------------------------------------------


class FairnessTests(PoolCase):
    def test_the_repository_holding_fewer_slots_goes_first_when_a_slot_frees(self):
        pool = self.pool(3, per_repo_cap=3)
        a1, a2 = self.take(pool, repo="org/a", job="a1"), self.take(pool, repo="org/a", job="a2")
        self.take(pool, repo="org/c", job="c1")
        self.assertEqual(self.refusal(pool, repo="org/a", job="a3").code, CEILING)  # a3 queues first
        self.clock.advance(1)
        self.assertEqual(self.refusal(pool, repo="org/b", job="b1").code, CEILING)
        self.held.remove(a1)
        a1.release()
        self.assertEqual(self.refusal(pool, repo="org/a", job="a3").code, QUEUED, "a holds 1, b holds 0: b is first")
        self.take(pool, repo="org/b", job="b1")
        self.assertEqual(self.refusal(pool, repo="org/a", job="a3").code, CEILING)
        del a2

    def test_the_oldest_waiter_of_equal_repositories_goes_first(self):
        pool = self.pool(1, per_repo_cap=3)
        holder = self.take(pool, repo="org/x", job="x")
        self.assertEqual(self.refusal(pool, repo="org/a", job="a").code, CEILING)
        self.clock.advance(1)
        self.assertEqual(self.refusal(pool, repo="org/b", job="b").code, CEILING)
        holder.release()
        self.assertEqual(self.refusal(pool, repo="org/b", job="b").code, QUEUED)
        self.take(pool, repo="org/a", job="a")

    def test_a_waiter_that_stops_asking_loses_its_place_after_the_ticket_time(self):
        pool = self.pool(1, per_repo_cap=3)
        holder = self.take(pool, repo="org/x", job="x")
        self.assertEqual(self.refusal(pool, repo="org/a", job="a").code, CEILING)
        self.clock.advance(1)
        self.assertEqual(self.refusal(pool, repo="org/b", job="b").code, CEILING)
        holder.release()
        self.clock.advance(podpool.TICKET_TTL_SECONDS + 1)  # a never came back
        self.take(pool, repo="org/b", job="b")

    def test_withdraw_removes_a_waiting_request_at_once(self):
        pool = self.pool(1, per_repo_cap=3)
        holder = self.take(pool, repo="org/x", job="x")
        self.assertEqual(self.refusal(pool, repo="org/a", job="a").code, CEILING)
        self.clock.advance(1)
        self.assertEqual(self.refusal(pool, repo="org/b", job="b").code, CEILING)
        self.assertEqual(len(pool.snapshot()["waiting"]), 2)
        pool.withdraw(request(repo="org/a", job="a"))
        self.assertEqual([w["repo"] for w in pool.snapshot()["waiting"]], ["org/b"])
        pool.withdraw(request(repo="org/a", job="a"))  # a second withdraw is fine
        holder.release()
        self.take(pool, repo="org/b", job="b")

    def test_a_waiter_of_a_repository_at_its_cap_does_not_stand_in_front_of_others(self):
        pool = self.pool(3, per_repo_cap=2)
        self.take(pool, repo="org/a", job="a1")
        self.take(pool, repo="org/a", job="a2")
        c1 = self.take(pool, repo="org/c", job="c1")
        self.assertEqual(self.refusal(pool, repo="org/a", job="a3").code, REPO_CAP)  # a ticket for a repo at its cap
        self.clock.advance(1)
        self.assertEqual(self.refusal(pool, repo="org/b", job="b1").code, CEILING)
        c1.release()
        self.take(pool, repo="org/b", job="b1")

    def test_waiters_of_a_repository_at_its_cap_do_not_take_the_place_of_a_repository_that_can_use_the_slot(self):
        # x holds 2 (its cap) and has the oldest waiter; y holds 1 and has two waiters; 2 slots are free. Counting x's waiter
        # the fair share is [ya, x3] (y's second pick ties with x's holdings and the older ticket wins), so yb would queue
        # behind a request that the repository cap refuses anyway. Without it the order is [ya, yb].
        pool = self.pool(5, per_repo_cap=2)
        self.take(pool, repo="org/x", job="x1")
        self.take(pool, repo="org/x", job="x2")
        self.take(pool, repo="org/y", job="y1")
        z1, z2 = self.take(pool, repo="org/z", job="z1"), self.take(pool, repo="org/z", job="z2")
        self.assertEqual(self.refusal(pool, repo="org/x", job="x3").code, REPO_CAP)  # the oldest ticket, and useless
        self.clock.advance(1)
        self.assertEqual(self.refusal(pool, repo="org/y", job="ya").code, CEILING)
        self.clock.advance(1)
        self.assertEqual(self.refusal(pool, repo="org/y", job="yb").code, CEILING)
        for reservation in (z1, z2):
            self.held.remove(reservation)
            reservation.release()
        self.take(pool, repo="org/y", job="yb")
        self.assertEqual(self.refusal(pool, repo="org/y", job="ya").code, REPO_CAP, "y is at its own cap now")

    def test_the_ticket_is_dropped_when_the_request_is_admitted(self):
        pool = self.pool(1, per_repo_cap=3)
        holder = self.take(pool, job="a")
        self.assertEqual(self.refusal(pool, job="b").code, CEILING)
        self.assertEqual(len(pool.snapshot()["waiting"]), 1)
        holder.release()
        self.take(pool, job="b")
        self.assertEqual(pool.snapshot()["waiting"], [])


# ---------------------------------------------------------------------------------------------------------------
# Duplicates, adoption and the reservation object
# ---------------------------------------------------------------------------------------------------------------


class ReservationTests(PoolCase):
    def test_a_session_holds_at_most_one_reservation(self):
        pool = self.pool(8)
        first = self.take(pool)
        refusal = self.refusal(pool, estimate=1)
        self.assertEqual(refusal.code, podpool.DUPLICATE)
        self.assertFalse(refusal.waiting)
        self.take(pool, attempt=1)  # a retry of the job is another session
        first.release()
        self.take(pool)

    def test_the_context_manager_releases_on_every_way_out(self):
        pool = self.pool(1)
        with pool.admit(request()).reservation:
            self.assertEqual(self.refusal(pool, job="b").code, CEILING)
        self.take(pool, job="b").release()
        with contextlib.suppress(RuntimeError), pool.admit(request()).reservation:
            raise RuntimeError("the session failed")
        self.take(pool, job="c")

    def test_a_reservation_that_is_dropped_without_a_release_is_released_by_the_collector(self):
        pool = self.pool(1)
        pool.admit(request())  # the Admission and its Reservation are garbage at once
        gc.collect()
        self.take(pool, job="b")

    def test_concurrent_double_release_unlinks_once_and_never_raises(self):
        pool = self.pool(8)
        reservation = self.take(pool)
        errors = []

        def release():
            try:
                reservation.release()
            except BaseException as error:  # noqa: BLE001
                errors.append(error)

        threads = [threading.Thread(target=release) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual((errors, self.files()), ([], []))

    def test_requested_is_written_to_the_ledger_and_is_idempotent(self):
        pool = self.pool(8)
        reservation = self.take(pool)
        self.assertFalse(reservation.launch_requested)
        self.assertFalse(pool.snapshot()["holders"][0]["requested"])
        reservation.requested()
        reservation.requested()
        self.assertTrue(reservation.launch_requested)
        self.assertTrue(pool.snapshot()["holders"][0]["requested"])

    def test_requested_after_the_ledger_entry_was_removed_by_anyone_is_lost(self):
        pool = self.pool(8)
        reservation = self.take(pool)
        (self.root / "pods" / self.files()[0]).unlink()  # not expired: an operator cleaned the directory
        with self.assertRaises(Lost):
            reservation.requested()
        self.assertFalse(reservation.launch_requested)

    def test_the_reservation_lock_is_on_a_close_on_exec_descriptor(self):
        pool = self.pool(8)
        reservation = self.take(pool)
        self.assertFalse(os.get_inheritable(reservation._descriptor), "a forked worker would keep a dead supervisor's slot alive")

    def test_requested_after_a_release_is_lost_and_nothing_may_be_launched(self):
        pool = self.pool(8)
        reservation = self.take(pool)
        reservation.release()
        with self.assertRaises(Lost):
            reservation.requested()
        self.assertEqual(self.files(), [])

    def test_requested_fails_closed_when_the_ledger_cannot_be_locked(self):
        pool = self.pool(8)
        reservation = self.take(pool)
        outside = os.open(self.root / "pods" / podpool.LOCK_NAME, os.O_RDWR)
        self.addCleanup(os.close, outside)
        import fcntl

        fcntl.flock(outside, fcntl.LOCK_EX)
        with patch.object(podpool, "LOCK_WAIT_SECONDS", 0.05), self.assertRaises(Unavailable):
            reservation.requested()
        self.assertFalse(reservation.launch_requested)

    def test_adopting_a_running_pod_counts_it_and_may_exceed_the_ceiling(self):
        pool = self.pool(1, per_repo_cap=10)
        self.take(pool, job="a")
        adopted = pool.adopt(request(job="found"))
        self.held.append(adopted.reservation)
        self.assertTrue(adopted.reservation.launch_requested)
        snapshot = pool.snapshot()
        self.assertEqual((snapshot["in_use"], snapshot["free"]), (2, 0))
        self.assertEqual(self.refusal(pool, job="new").code, CEILING)
        self.assertEqual(pool.adopt(request(job="found")).refusal.code, podpool.DUPLICATE)
        self.assertEqual(pool.adopt("nope").refusal.code, podpool.INVALID)

    def test_an_adoption_does_not_refund_a_token_when_cancelled(self):
        pool = PodPool(self.root / "pods", 50, per_repo_cap=50, burst=2, interval=15, clock=self.clock)
        adopted = pool.adopt(request(job="found")).reservation
        adopted.cancel()
        self.take(pool, job="a")
        self.take(pool, job="b")
        self.assertEqual(self.refusal(pool, job="c").code, PACING, "an adoption took no token and refunded none")

    def test_invalid_requests_are_refused_not_raised(self):
        pool = self.pool(8)
        for junk in (None, "x", {"repo": "org/app"}, 5, object()):
            self.assertEqual(pool.admit(junk).refusal.code, podpool.INVALID)
        self.assertEqual(self.files(), [])


# ---------------------------------------------------------------------------------------------------------------
# Crash, restart and expiry: nothing leaks
# ---------------------------------------------------------------------------------------------------------------

CHILD = textwrap.dedent(
    """
    import sys, time
    sys.path.insert(0, sys.argv[1])
    from factory.podpool import PodPool, Request
    directory, ceiling, repo, job, mode = sys.argv[2], int(sys.argv[3]), sys.argv[4], sys.argv[5], sys.argv[6]
    pool = PodPool(directory, ceiling, per_repo_cap=50, burst=1000)
    admission = pool.admit(Request(repo, "run-1", job, "build", 0, 1.0, 600, 0, 5))
    if admission.reservation is None:
        print("DENIED " + admission.refusal.code, flush=True)
        sys.exit(0)
    if mode == "requested":
        admission.reservation.requested()
    print("ADMITTED", flush=True)
    if mode == "spawn":
        import subprocess
        print(subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"]).pid, flush=True)
    if mode == "exit":
        import os
        os._exit(0)
    sys.stdin.read()
    admission.reservation.release()
    """
)


class ProcessCase(PoolCase):
    def start(self, ceiling, repo, job, mode="hold"):
        script = self.root / "child.py"
        script.write_text(CHILD)
        process = subprocess.Popen(
            [sys.executable, "-I", str(script), str(SRC), str(self.root / "pods"), str(ceiling), repo, job, mode],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self.addCleanup(process.kill)
        self.addCleanup(process.wait)
        self.addCleanup(process.stdout.close)
        self.addCleanup(process.stdin.close)
        return process, process.stdout.readline().strip()


class CrashTests(ProcessCase):
    def real_pool(self, ceiling, **kwargs):
        return PodPool(self.root / "pods", ceiling, per_repo_cap=50, burst=1000, clock=self.clock, **kwargs)

    def test_supervisors_in_separate_processes_share_one_ceiling(self):
        first, answer = self.start(2, "org/a", "a1")
        self.assertEqual(answer, "ADMITTED")
        self.assertEqual(self.start(2, "org/b", "b1")[1], "ADMITTED")
        self.assertEqual(self.start(2, "org/c", "c1")[1], "DENIED ceiling")
        first.stdin.close()
        first.wait()
        self.assertEqual(self.start(2, "org/c", "c1")[1], "ADMITTED")

    def test_a_killed_supervisor_leaks_nothing_and_its_files_are_reclaimed(self):
        victim, answer = self.start(1, "org/a", "a1")
        self.assertEqual(answer, "ADMITTED")
        pool = self.real_pool(1)
        self.assertEqual(pool.snapshot()["in_use"], 1)
        self.assertEqual(self.refusal(pool, repo="org/b", job="b1").code, CEILING)
        victim.send_signal(signal.SIGKILL)
        victim.wait()
        self.assertEqual(len(self.files()), 1, "the file is still there: a kill removes no file")
        self.assertEqual(pool.snapshot()["in_use"], 0, "and a snapshot already does not count it")
        self.assertEqual(len(self.files()), 1, "a snapshot removes nothing")
        self.take(pool, repo="org/b", job="b1")
        self.assertEqual(len(self.files()), 1, "the admission removed the dead one")

    def test_a_manager_restart_starts_with_an_empty_pool_whatever_the_ledger_held(self):
        started = [self.start(8, "org/a", f"a{index}", mode="requested") for index in range(3)]
        self.assertEqual([answer for _, answer in started], ["ADMITTED"] * 3)
        for process, _ in started:
            process.kill()
            process.wait()
        self.assertEqual(len(self.files()), 3)
        restarted = self.real_pool(8)
        reclaimed = restarted.sweep()
        self.assertEqual(len(reclaimed), 3)
        self.assertEqual({item["why"] for item in reclaimed}, {"dead"})
        self.assertTrue(all(item["entry"]["requested"] for item in reclaimed), "the orphan sweep is told a launch may have happened")
        self.assertEqual(self.files(), [])
        self.assertEqual(restarted.sweep(), [])

    def test_a_dead_reservation_that_never_requested_a_launch_is_reported_as_such(self):
        process, answer = self.start(8, "org/a", "a1", mode="exit")
        self.assertEqual(answer, "ADMITTED")
        process.wait()
        reclaimed = self.real_pool(8).sweep()
        self.assertEqual([(item["why"], item["entry"]["requested"], item["entry"]["job"]) for item in reclaimed], [("dead", False, "a1")])
        self.assertIn("reclaimed 1 reservation(s) of a dead supervisor and 0 expired", self.err.getvalue())

    def test_a_dead_reservations_budget_is_released_too(self):
        victim, _ = self.start(8, "org/a", "a1")
        pool = self.real_pool(8)
        self.assertEqual(self.refusal(pool, repo="org/a", job="a2", estimate=5, budget_usd=5.5).code, BUDGET_HELD)
        victim.kill()
        victim.wait()
        self.take(pool, repo="org/a", job="a2", estimate=5, budget_usd=5.5)

    def test_a_surviving_child_of_a_dead_supervisor_does_not_keep_its_reservation_alive(self):
        victim, answer = self.start(1, "org/a", "a1", mode="spawn")
        self.assertEqual(answer, "ADMITTED")
        sleeper = int(victim.stdout.readline())
        self.addCleanup(self._reap, sleeper)
        pool = self.real_pool(1)
        self.assertEqual(self.refusal(pool, repo="org/b", job="b1").code, CEILING)
        victim.kill()
        victim.wait()
        os.kill(sleeper, 0)  # the child outlived its parent, as a worker would
        self.take(pool, repo="org/b", job="b1")  # and the lock descriptor did not go with it

    @staticmethod
    def _reap(pid):
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)

    def test_a_dead_file_with_garbage_is_removed_and_never_counted(self):
        pool = self.real_pool(1)
        pool.prepare()
        (self.root / "pods" / "r-garbage").write_bytes(b"\xff\x00{{{")
        (self.root / "pods" / "r-empty").write_bytes(b"")
        self.assertEqual(pool.snapshot()["in_use"], 0)
        self.take(pool)
        self.assertEqual(len(self.files()), 1)

    def test_a_held_file_that_is_not_a_valid_entry_still_occupies_a_slot_until_it_expires(self):
        import fcntl

        pool = self.real_pool(1)
        pool.prepare()
        path = self.root / "pods" / "r-garbage"
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        os.write(descriptor, b"{not json")
        self.assertEqual(self.refusal(pool).code, CEILING, "an unreadable entry must never free a slot")
        self.assertEqual(pool.snapshot()["holders"][0]["repo"], None)
        self.clock.now = os.stat(path).st_mtime + podpool.MAX_HOLD_SECONDS + 1
        self.take(pool)
        self.assertFalse(path.exists())

    def test_a_reservation_expires_after_the_pods_deadline_even_when_its_holder_is_wedged(self):
        pool = self.pool(1)
        wedged = self.take(pool, timeout=100)
        hold = request(timeout=100).hold_seconds
        self.clock.advance(hold)
        self.assertEqual(self.refusal(pool, job="b").code, CEILING, "not yet: the pod may still run")
        self.clock.advance(1)
        self.assertEqual(pool.snapshot()["in_use"], 0)
        replacement = self.take(pool, job="b")
        with self.assertRaises(Lost):
            wedged.requested()  # the wedged holder wakes up and must not start a pod the ledger no longer counts
        self.assertEqual(self.files(), [replacement._path.name])
        self.assertIn("1 expired", self.err.getvalue())

    def test_a_wedged_holders_late_release_does_not_free_a_newer_reservation_of_the_same_session(self):
        pool = self.pool(8)
        old = self.take(pool, timeout=100)
        self.clock.advance(request(timeout=100).hold_seconds + 1)
        new = self.take(pool, timeout=100)  # the same session identity, after the old one expired
        old.release()
        self.assertEqual(self.files(), [new._path.name])
        self.assertEqual(self.refusal(pool).code, podpool.DUPLICATE)

    def test_a_late_release_cannot_delete_a_reservation_admitted_between_its_check_and_its_unlink(self):
        pool = self.pool(8)
        old = self.take(pool, timeout=100)
        self.clock.advance(request(timeout=100).hold_seconds + 1)  # expired, its holder wedged, not yet swept
        between = []
        real = old._still_listed

        def still_listed():
            answer = real()
            # What another supervisor does at this very moment: sweep the expired file and admit the same session again.
            pool.sweep()
            between.append(pool.admit(request(timeout=100)))
            return answer

        old._still_listed = still_listed
        with patch.object(podpool, "LOCK_WAIT_SECONDS", 0.05):
            old.release()
        (admission,) = between
        if admission.reservation is not None:
            self.held.append(admission.reservation)
            self.assertEqual(self.files(), [admission.reservation._path.name], "the newer reservation survived")
        else:
            self.assertEqual(admission.refusal.code, podpool.UNAVAILABLE, "the ledger lock was held across check and unlink")

    def test_a_release_that_cannot_lock_the_ledger_never_raises_and_leaves_an_unheld_file_for_the_next_scan(self):
        import fcntl

        pool = self.pool(8)
        reservation = self.take(pool)
        outside = os.open(self.root / "pods" / podpool.LOCK_NAME, os.O_RDWR)
        self.addCleanup(os.close, outside)
        fcntl.flock(outside, fcntl.LOCK_EX)
        with patch.object(podpool, "LOCK_WAIT_SECONDS", 0.05):
            reservation.release()
            reservation.release()
        self.assertEqual(len(self.files()), 1, "left in place: nothing may touch the ledger without its lock")
        self.assertIsNone(reservation._descriptor)
        fcntl.flock(outside, fcntl.LOCK_UN)
        self.assertEqual([item["why"] for item in pool.sweep()], ["dead"], "closed, so nobody holds it: the next scan removes it")
        self.assertEqual(self.files(), [])

    def blocked_refund(self, pool):
        """Patch the pool's refund so it stops inside the first call; returns (calls, entered, proceed)."""
        calls, entered, proceed = [], threading.Event(), threading.Event()

        def refund():
            calls.append(1)
            entered.set()
            proceed.wait(5)

        patcher = patch.object(pool, "_refund", refund)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(proceed.set)
        return calls, entered, proceed

    def test_two_cancels_of_one_reservation_refund_one_token(self):
        pool = self.pool(8)
        reservation = self.take(pool)
        calls, entered, proceed = self.blocked_refund(pool)
        first = threading.Thread(target=reservation.cancel)
        first.start()
        self.assertTrue(entered.wait(5))
        second = threading.Thread(target=reservation.cancel)  # arrives while the first is still refunding
        second.start()
        second.join(1)
        proceed.set()
        first.join(5)
        second.join(5)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.files(), [])

    def test_two_concurrent_cancels_give_the_real_bucket_one_token_back(self):
        pool = PodPool(self.root / "pods", 50, per_repo_cap=50, burst=3, interval=15, clock=self.clock)
        spent = [self.take(pool, job="a"), self.take(pool, job="b"), self.take(pool, job="c")]
        gate = threading.Barrier(2)

        def cancel():
            gate.wait(5)
            spent[0].cancel()

        threads = [threading.Thread(target=cancel) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        saved = json.loads((self.root / "pods" / podpool.PACING_NAME).read_text())
        self.assertEqual(saved["tokens"], 1.0)

    def test_a_launch_cannot_be_recorded_while_a_cancel_is_refunding_and_a_cancel_cannot_refund_after_one(self):
        pool = self.pool(8)
        reservation = self.take(pool)
        calls, entered, proceed = self.blocked_refund(pool)
        canceller = threading.Thread(target=reservation.cancel)
        canceller.start()
        self.assertTrue(entered.wait(5))
        with self.assertRaises(Lost):
            reservation.requested()  # the token is being given back: no request may be sent on this reservation
        proceed.set()
        canceller.join(5)
        self.assertFalse(reservation.launch_requested)
        other = self.take(pool, job="b")
        other.requested()
        other.cancel()
        self.assertEqual(len(calls), 1, "a reservation whose launch was recorded refunds nothing")

    def test_the_hold_time_follows_the_session_timeout_and_the_clamp(self):
        pool = self.pool(8, per_repo_cap=10)
        long_session = self.take(pool, job="long", timeout=10**6)
        self.clock.advance(podpool.MAX_HOLD_SECONDS + 1)
        self.assertEqual(pool.snapshot()["in_use"], 0)
        with self.assertRaises(Lost):
            long_session.requested()


# ---------------------------------------------------------------------------------------------------------------
# Races
# ---------------------------------------------------------------------------------------------------------------


class RaceTests(ProcessCase):
    def race(self, count, make):
        """Run ``make(index)`` in ``count`` threads released together; returns the admissions."""
        barrier = threading.Barrier(count)
        results = [None] * count

        def work(index):
            barrier.wait()
            results[index] = make(index)

        threads = [threading.Thread(target=work, args=(index,)) for index in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        for admission in results:
            if admission.reservation is not None:
                self.held.append(admission.reservation)
        return results

    def test_sixteen_threads_cannot_take_more_than_the_ceiling(self):
        pool = self.pool(5, per_repo_cap=100)
        results = self.race(16, lambda index: pool.admit(request(repo=f"org/r{index % 4}", job=f"j{index}")))
        self.assertEqual(sum(1 for r in results if r.reservation is not None), 5)
        self.assertEqual({r.refusal.code for r in results if r.reservation is None} - {CEILING, QUEUED}, set())
        self.assertEqual(len(self.files()), 5)

    def test_one_session_asked_twice_at_once_is_admitted_once(self):
        pool = self.pool(8)
        results = self.race(8, lambda index: pool.admit(request()))
        self.assertEqual(sum(1 for r in results if r.reservation is not None), 1)
        self.assertEqual({r.refusal.code for r in results if r.reservation is None}, {podpool.DUPLICATE})

    def test_the_budget_cannot_be_overspent_by_a_race(self):
        pool = self.pool(50, per_repo_cap=100)
        results = self.race(12, lambda index: pool.admit(request(job=f"j{index}", estimate=4, spent_usd=1, budget_usd=10)))
        self.assertEqual(sum(1 for r in results if r.reservation is not None), 2)  # 1 + 4 + 4 = 9; a third would be 13
        self.assertEqual({r.refusal.code for r in results if r.reservation is None}, {BUDGET_HELD})

    def test_the_burst_cannot_be_exceeded_by_a_race(self):
        pool = PodPool(self.root / "pods", 50, per_repo_cap=100, burst=2, interval=15, clock=self.clock)
        results = self.race(10, lambda index: pool.admit(request(job=f"j{index}")))
        self.assertEqual(sum(1 for r in results if r.reservation is not None), 2)

    def test_separate_pool_objects_over_one_directory_serialise_on_the_same_lock(self):
        pools = [self.pool(3, per_repo_cap=100) for _ in range(12)]
        results = self.race(12, lambda index: pools[index].admit(request(job=f"j{index}")))
        self.assertEqual(sum(1 for r in results if r.reservation is not None), 3)

    def test_separate_processes_cannot_take_more_than_the_ceiling(self):
        script = self.root / "child.py"
        script.write_text(CHILD)
        processes = []
        for index in range(8):
            process = subprocess.Popen(
                [sys.executable, "-I", str(script), str(SRC), str(self.root / "pods"), "3", f"org/r{index}", f"j{index}", "hold"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            self.addCleanup(process.kill)
            self.addCleanup(process.wait)
            self.addCleanup(process.stdout.close)
            self.addCleanup(process.stdin.close)
            processes.append(process)
        answers = [process.stdout.readline().strip() for process in processes]
        self.assertEqual(sum(1 for answer in answers if answer == "ADMITTED"), 3, answers)
        self.assertEqual({answer for answer in answers if answer != "ADMITTED"} - {"DENIED ceiling", "DENIED queued"}, set())

    def test_a_scan_never_removes_a_reservation_that_is_being_created(self):
        pool = self.pool(40, per_repo_cap=100)
        stop = threading.Event()
        errors = []

        def sweeper():
            while not stop.is_set():
                for item in pool.sweep():
                    errors.append(item)

        thread = threading.Thread(target=sweeper)
        thread.start()
        try:
            for index in range(30):
                self.take(pool, job=f"j{index}")
        finally:
            stop.set()
            thread.join()
        self.assertEqual(errors, [], "a live reservation was reclaimed as dead")
        self.assertEqual(len(self.files()), 30)


# ---------------------------------------------------------------------------------------------------------------
# Failing closed
# ---------------------------------------------------------------------------------------------------------------


class FailClosedTests(PoolCase):
    def test_a_state_directory_that_cannot_be_used_refuses(self):
        blocked = self.root / "blocked"
        blocked.write_text("a file where the directory should be")
        pool = PodPool(blocked / "pods", 8, clock=self.clock)
        self.assertEqual(pool.admit(request()).refusal.code, podpool.UNAVAILABLE)
        self.assertIsNone(pool.snapshot())
        self.assertEqual(pool.sweep(), [])
        self.assertIn("refusing pod sessions", self.err.getvalue())

    def test_a_lock_that_cannot_be_taken_refuses_instead_of_waiting_for_ever(self):
        import fcntl

        pool = self.pool(8)
        self.take(pool, job="a")
        outside = os.open(self.root / "pods" / podpool.LOCK_NAME, os.O_RDWR)
        self.addCleanup(os.close, outside)
        fcntl.flock(outside, fcntl.LOCK_EX)
        with patch.object(podpool, "LOCK_WAIT_SECONDS", 0.05):
            self.assertEqual(pool.admit(request(job="b")).refusal.code, podpool.UNAVAILABLE)
            self.assertIsNone(pool.snapshot())
        fcntl.flock(outside, fcntl.LOCK_UN)
        self.take(pool, job="b")

    def test_a_lock_file_that_is_a_symlink_is_not_followed(self):
        pool = self.pool(8)
        pool.prepare()
        target = self.root / "elsewhere"
        target.write_text("x")
        os.symlink(target, self.root / "pods" / podpool.LOCK_NAME)
        self.assertEqual(pool.admit(request()).refusal.code, podpool.UNAVAILABLE)

    def test_a_failure_while_writing_the_reservation_leaves_no_file_and_no_slot(self):
        pool = self.pool(1)
        with patch.object(podpool, "_write", side_effect=OSError("disk full")):
            self.assertEqual(pool.admit(request()).refusal.code, podpool.UNAVAILABLE)
        self.assertEqual(self.files(), [])
        self.take(pool)

    def deny_opening(self, path, error):
        """Make ``os.open`` of exactly ``path`` fail with ``error`` while everything else works."""
        real = os.open

        def opener(target, *args, **kwargs):
            if Path(target) == path:
                raise OSError(error, os.strerror(error))
            return real(target, *args, **kwargs)

        return patch.object(podpool.os, "open", side_effect=opener)

    def test_a_reservation_file_that_cannot_be_opened_still_counts_as_a_slot_and_is_not_removed(self):
        for error in (errno.EACCES, errno.EPERM, errno.ELOOP, errno.EIO):
            with self.subTest(error=errno.errorcode[error]):
                pool = PodPool(self.root / f"pods-{error}", 1, burst=100, clock=self.clock)
                held = pool.admit(request()).reservation
                self.addCleanup(held.release)
                with self.deny_opening(held._path, error):
                    self.assertEqual(pool.admit(request(job="b")).refusal.code, CEILING, "an unreadable entry never frees a slot")
                    self.assertEqual(pool.snapshot()["in_use"], 1)
                    self.assertEqual(pool.sweep(), [])
                self.assertTrue(held._path.exists())

    def test_running_out_of_descriptors_while_counting_refuses_instead_of_guessing(self):
        pool = self.pool(8)
        held = self.take(pool)
        for error in (errno.EMFILE, errno.ENFILE):
            with self.subTest(error=errno.errorcode[error]), self.deny_opening(held._path, error):
                self.assertEqual(pool.admit(request(job="b")).refusal.code, podpool.UNAVAILABLE)
                self.assertIsNone(pool.snapshot())
                self.assertEqual(pool.sweep(), [])
        self.assertEqual(self.files(), [held._path.name], "nothing was granted and nothing was removed")

    def test_a_reservation_that_vanishes_between_the_listing_and_the_opening_counts_nothing(self):
        pool = self.pool(1)
        held = self.take(pool)
        with self.deny_opening(held._path, errno.ENOENT):
            self.assertEqual(pool.snapshot()["in_use"], 0)

    def test_a_symlink_or_a_directory_in_the_ledger_counts_as_a_slot_and_is_left_alone(self):
        pool = self.pool(2)
        pool.prepare()
        target = self.root / "elsewhere"
        target.write_text("{}")
        os.symlink(target, self.root / "pods" / "r-link")
        (self.root / "pods" / "r-dir").mkdir()
        self.assertEqual(pool.snapshot()["in_use"], 2)
        self.assertEqual(pool.admit(request()).refusal.code, CEILING)
        self.assertEqual(pool.sweep(), [])
        self.assertEqual(self.files(), ["r-dir", "r-link"])
        self.assertEqual(target.read_text(), "{}")

    def test_a_refusal_never_names_the_repository_run_or_job(self):
        pool = self.pool(1)
        self.take(pool, repo="org/secret-app", run="run-secret", job="job-secret")
        for _ in range(3):
            self.refusal(pool, repo="org/other-secret", run="r2", job="j2")
        output = self.err.getvalue()
        for text in ("secret", "org/", "run-", "job-"):
            self.assertNotIn(text, output)
        self.assertNotIn("secret", self.refusal(pool, repo="org/x", job="y").text)

    def test_the_snapshot_writes_and_removes_nothing(self):
        pool = self.pool(8)
        self.take(pool, job="a")
        (self.root / "pods" / "r-dead").write_text("{}")
        before = {path.name: path.stat().st_mtime_ns for path in (self.root / "pods").iterdir()}
        snapshot = pool.snapshot()
        after = {path.name: path.stat().st_mtime_ns for path in (self.root / "pods").iterdir()}
        self.assertEqual(before, after)
        self.assertEqual(snapshot["in_use"], 1)
        self.assertEqual(snapshot["holders"][0]["repo"], "org/app")
        self.assertEqual({"ceiling", "in_use", "free", "per_repo_cap", "pacing_tokens", "reserved_usd", "holders", "waiting"}, set(snapshot))

    def test_nothing_is_written_to_the_ledger_by_a_pool_that_is_never_asked(self):
        self.pool(8)
        self.assertFalse((self.root / "pods").exists())


# ---------------------------------------------------------------------------------------------------------------
# The call contract of the backend: hold()
# ---------------------------------------------------------------------------------------------------------------


class HoldTests(PoolCase):
    def test_hold_admits_at_once_and_releases_on_exit(self):
        pool = self.pool(1)
        with podpool.hold(pool, request(), 60) as reservation:
            reservation.requested()
            self.assertEqual(self.refusal(pool, job="b").code, CEILING)
        self.take(pool, job="b")

    def test_hold_releases_when_the_block_raises(self):
        pool = self.pool(1)
        with self.assertRaises(KeyError), podpool.hold(pool, request(), 60):
            raise KeyError("the launch failed")
        self.take(pool, job="b")

    def test_hold_waits_for_a_release_and_polls_with_the_given_sleep(self):
        pool = self.pool(1)
        holder = self.take(pool, job="a")
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 3:
                holder.release()

        ticks = iter(range(1000))
        with podpool.hold(pool, request(job="b"), 60, sleep=sleep, clock=lambda: next(ticks)) as reservation:
            self.assertIsNotNone(reservation)
        self.assertEqual(sleeps, [float(podpool.POLL_SECONDS)] * 3)

    def test_hold_sleeps_the_pacing_hint_but_never_more_than_the_maximum(self):
        pool = PodPool(self.root / "pods", 50, per_repo_cap=50, burst=1, interval=15, clock=self.clock)
        self.take(pool, job="a")
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            self.clock.advance(15)

        ticks = iter(range(0, 1000, 1))
        with podpool.hold(pool, request(job="b"), 600, sleep=sleep, clock=lambda: next(ticks)):
            pass
        self.assertEqual(sleeps, [podpool.MAX_POLL_SECONDS])

    def test_hold_times_out_with_the_last_code_and_withdraws_its_ticket(self):
        pool = self.pool(1)
        self.take(pool, job="a")
        ticks = iter(range(0, 1000, 10))
        with self.assertRaises(Timeout) as caught, podpool.hold(pool, request(job="b"), 25, sleep=lambda s: None, clock=lambda: next(ticks)):
            self.fail("not admitted")
        self.assertEqual(caught.exception.code, CEILING)
        self.assertEqual(str(caught.exception), podpool._TEXT[CEILING])
        self.assertEqual(pool.snapshot()["waiting"], [])
        self.assertIsInstance(caught.exception, Refused)

    def test_hold_raises_a_final_refusal_at_once_without_waiting(self):
        pool = self.pool(8)
        slept = []
        ticks = iter(range(0, 10000, 100))  # a pool that waited would run out of the 600 s at once and say Timeout
        with self.assertRaises(Refused) as caught, podpool.hold(pool, request(estimate=50, budget_usd=10), 600, sleep=slept.append, clock=lambda: next(ticks)):
            self.fail("not admitted")
        self.assertEqual((caught.exception.code, type(caught.exception), slept), (BUDGET, Refused, []))

    def test_hold_without_a_pool_refuses_instead_of_running_unguarded(self):
        with self.assertRaises(Refused) as caught, podpool.hold(None, request(), 60):
            self.fail("not admitted")
        self.assertEqual(caught.exception.code, podpool.UNAVAILABLE)

    def test_hold_never_starts_a_session_on_an_unavailable_pool(self):
        pool = PodPool(self.root / "blocked" / "pods", 8, clock=self.clock)
        (self.root / "blocked").write_text("x")
        with self.assertRaises(Refused) as caught, podpool.hold(pool, request(), 60):
            self.fail("not admitted")
        self.assertEqual(caught.exception.code, podpool.UNAVAILABLE)


# ---------------------------------------------------------------------------------------------------------------
# Against the real launcher and client (over a real socket, the fake Kubernetes API behind them)
# ---------------------------------------------------------------------------------------------------------------


class LauncherSeamTests(test_launcher.ServerHarness):
    """The pool's launcher count and the launch request it is built from, against ``launcher.py`` as slice 11 wrote it."""

    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.ledger = Path(temp.name) / "pods"
        self.start_server()
        self.client_ = self.client()
        self.held = []
        self.addCleanup(lambda: [r.release() for r in self.held])

    def pool(self, ceiling):
        return PodPool(self.ledger, ceiling, per_repo_cap=50, burst=100, clock=self.clock, live_count=podpool.launcher_live_count(self.client_))

    def test_the_count_is_live_plus_inflight_of_the_real_info_answer(self):
        info = self.client_.info()
        self.assertEqual((info["live"], info["inflight"]), (0, 0))
        self.client_.launch(test_launcher.launch_request(1), test_launcher.TOKEN)
        self.client_.launch(test_launcher.launch_request(2), test_launcher.TOKEN)
        self.assertEqual(podpool.launcher_live_count(self.client_)(), 2)

    def test_pods_nobody_holds_a_reservation_for_occupy_the_ceiling(self):
        for number in (1, 2, 3):  # a crashed supervisor's pods: launched, no reservation anywhere
            self.client_.launch(test_launcher.launch_request(number), test_launcher.TOKEN)
        pool = self.pool(3)
        self.assertEqual(pool.admit(request()).refusal.code, CEILING)
        self.assertEqual(self.pool(4).admit(request()).refusal, None)

    def test_a_request_built_from_the_launch_request_is_launched_after_requested_and_released_after_the_block(self):
        pool = self.pool(2)
        launch = test_launcher.launch_request(1, repository="org/app", run_id="run-1", job_id="job-1", attempt=0)
        with podpool.hold(pool, Request.for_launch(launch, 4.0, spent_usd=1.0, budget_usd=10), 60) as reservation:
            reservation.requested()
            launched = self.client_.launch(launch, test_launcher.TOKEN)
            self.assertEqual(launched.name, test_launcher.pod_name(1))
            self.assertEqual(pool.snapshot()["in_use"], 1)
        self.assertEqual(pool.snapshot()["in_use"], 0)
        self.assertEqual(self.client_.info()["live"], 1, "the pod outlives the reservation until its session ends: the launcher's count covers it")
        self.assertEqual(pool.admit(request(job="two")).refusal, None)

    def test_a_launcher_that_is_denied_or_down_is_a_refusal_not_a_count_of_zero(self):
        self.client_.deny("test_deny")
        self.assertEqual(self.client_.info()["denied"], True)
        self.assertEqual(self.pool(2).admit(request()).refusal, None, "a denied launcher still answers info; the launch itself is what it refuses")
        down = PodPool(
            self.ledger,
            2,
            clock=self.clock,
            live_count=podpool.launcher_live_count(
                test_launcher.launcher_client.LauncherClient(str(Path(self.directory) / "absent.sock"), launcher_uid=os.getuid(), sleep=test_launcher.noop, retries=0)
            ),
        )
        with contextlib.redirect_stderr(io.StringIO()), patch.object(capacity, "_NOTICED", set()):
            self.assertEqual(down.admit(request()).refusal.code, podpool.UNAVAILABLE)


# ---------------------------------------------------------------------------------------------------------------
# What the module is allowed to be
# ---------------------------------------------------------------------------------------------------------------


class SourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(POOL_SOURCE.read_text())
        cls.imports = set()
        for node in ast.walk(cls.tree):
            if isinstance(node, ast.Import):
                cls.imports |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                cls.imports.add(("." * node.level) + (node.module or ""))
                if node.level:
                    cls.imports |= {"." + alias.name for alias in node.names}

    def test_it_imports_the_standard_library_and_three_modules_of_this_package_and_nothing_else(self):
        outside = {name for name in self.imports if not name.startswith(".")} - set(sys.stdlib_module_names)
        self.assertEqual(outside, set())
        package = {name for name in self.imports if name.startswith(".") and name != "."}
        self.assertEqual(package, {".capacity", ".stopworld", ".podcontract"})

    def test_it_starts_no_process_and_opens_no_socket(self):
        for forbidden in ("subprocess", "socket", "urllib", "http", "ssl", "multiprocessing", "ctypes"):
            self.assertNotIn(forbidden, self.imports)
        names = {node.id for node in ast.walk(self.tree) if isinstance(node, ast.Name)}
        self.assertFalse({"eval", "exec", "compile", "__import__", "open"} & names, "files are opened with os.open or pathlib")

    def test_it_reads_the_environment_only_in_its_three_configuration_functions(self):
        text = POOL_SOURCE.read_text()
        readers = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef):
                if any(ast.unparse(inner) == "os.environ" for inner in ast.walk(node)):
                    readers.add(node.name)
        self.assertEqual(readers, {"parse_ceiling", "pool_directory", "from_env"})
        self.assertIn('ENV_CEILING = pc.ENV_MANAGER["max_pod_sessions"]', text)
        self.assertIn('pc.ENV_MANAGER["pod_backend"]', text)

    def test_every_printed_line_is_a_fixed_phrase_or_integers(self):
        printed = [node for node in ast.walk(self.tree) if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) in {"print", "_log", "notice"}]
        self.assertGreaterEqual(len(printed), 4)
        for call in printed:
            for argument in call.args:
                for part in ast.walk(argument):
                    if isinstance(part, ast.FormattedValue):
                        shown = ast.unparse(part.value)
                        self.assertRegex(shown, r"^(message|dead|len\(reclaimed\) - dead|refusal\.text)$", ast.unparse(call))

    def test_no_state_dir_name_collides_with_the_in_process_pool(self):
        self.assertNotIn(podpool.SUBDIRECTORY, ("slots", "waiting"))
        self.assertEqual(Path(podpool.pool_directory({"FACTORY_DATA": "/d"})), Path("/d/.capacity/pods"))
        self.assertIsNone(podpool.pool_directory({}))

    def test_importing_the_module_changes_nothing(self):
        done = subprocess.run(
            [sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(SRC)!r}); import factory.podpool"],
            capture_output=True,
            text=True,
            env={"PATH": os.environ.get("PATH", ""), "FACTORY_DATA": "/nonexistent-dir-for-test", "FACTORY_POD_BACKEND": "on"},
        )
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))
        self.assertFalse(os.path.exists("/nonexistent-dir-for-test"))

    def test_the_two_error_hierarchies_keep_fixed_messages(self):
        self.assertEqual(str(Lost()), "pod pool: the reservation is gone")
        self.assertEqual(str(Unavailable()), "pod pool: its state cannot be used")
        self.assertEqual(str(ConfigRefused("state_dir")), "pod pool configuration refused: state_dir")


if __name__ == "__main__":
    unittest.main()

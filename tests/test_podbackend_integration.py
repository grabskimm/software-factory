"""The pod backend wired to the slices that were delivered "not wired in" (slice 12b, the integration pass).

CONTRACT-TESTED against in-memory fakes and the real modules, never a cluster. Four seams are pinned here, each by tests that
fail when the protection is removed:

* the pod worker (slice 10b): the manifest's ``run`` section is exactly what ``podworker.read_run`` accepts, the report is read
  by ``podreport.loads`` only, every ``outcome``, delta status and wrapper stage exit becomes the outcome ``LocalBackend`` gives
  (a real wrapper runs the real bundle in ``EndToEnd``, against a real relay over a real socket and a real child process);
* the pod pool (slice 18): admitted before anything is staged, ``requested()`` immediately before the launch request, a
  ``cancel()`` only for a launch that was not sent, released on every path, a refusal is a fixed text and starts nothing;
* the orphan sweep (slice 20): the session is protected from before the launch until after ``released`` is written, and the
  sweeper of the manager is built only for the switch on;
* pod-side liveness (slice 19): the verdicts end a session, ``enforce`` writes the revoke marker BEFORE it terminates, and a pod
  that merely waited long for its node is not taken for a silent one.
"""

import contextlib
import dataclasses
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from factory import agent, bundle, gitsafe, launcher_client, podbackend, podexecution, podhandle, podliveness, podpool, podsweep, podworker, stopworld
from factory import podcontract as pc
from factory import sessionrecord as sr
from factory.agent import AgentError, AgentTimeout
from tests import test_podbackend as tb
from tests import test_podworker as tw
from tests import test_podworker_run as twr
from tests.fake_blob import FakeClock
from tests.test_podbackend import (
    EMPTY_DELTA,
    EPOCH,
    INSTANCE,
    POD_UID,
    REPOSITORY,
    RUN_ID,
    SUCCEEDED,
    FakeLauncher,
    delta_of,
    report_of,
    result_line,
    status,
)

ROOT = Path(__file__).resolve().parent.parent
SWITCH = pc.ENV_MANAGER["pod_backend"]


class Case(tb.Fixture, unittest.TestCase):
    """The fixture of ``test_podbackend`` over the fake launcher, with the sweep's protection reset."""

    def setUp(self):
        podhandle.reset()
        podsweep.reset()
        self.addCleanup(podhandle.reset)
        self.addCleanup(podsweep.reset)
        self.build(FakeLauncher(self), FakeClock(1000.0))

    def ledger(self):
        return self.pool.snapshot()

    def assertNothingHeld(self):
        self.assertEqual(self.ledger()["in_use"], 0)
        self.assertEqual(podsweep.protected(), frozenset())


# --- the pod worker (slice 10b) -----------------------------------------------------------------------------------------------


class RunSection(Case):
    def test_the_manifests_run_keys_are_the_ones_the_wrapper_requires_and_nothing_else(self):
        self.assertEqual(podbackend.RUN_KEYS, podworker.RUN_KEYS)
        spec = self.backend.prepare(self.request())
        self.assertEqual(tuple(self.backend.manifest(spec)["run"]), podworker.RUN_KEYS)

    def test_the_wrapper_accepts_the_manifest_the_backend_writes(self):
        spec = self.backend.prepare(self.request())
        manifest = self.backend.manifest(spec)
        prepared = types.SimpleNamespace(manifest=manifest, worktree=os.fspath(self.tree))
        run = podworker.read_run(prepared, podworker.load_support())
        self.assertEqual(run.argv, tuple(spec.argv))
        self.assertEqual(run.cwd, os.fspath(self.tree))
        self.assertEqual(run.prompt, b"PROMPT TEXT")
        self.assertEqual((run.timeout, run.memory_limit_mb), (600, None))

    def test_a_key_the_wrapper_does_not_know_is_refused_by_it_so_the_test_above_would_fail_with_one(self):
        spec = self.backend.prepare(self.request())
        manifest = self.backend.manifest(spec)
        manifest["run"]["phase"] = "build"
        prepared = types.SimpleNamespace(manifest=manifest, worktree=os.fspath(self.tree))
        with self.assertRaises(podworker.Failure):
            podworker.read_run(prepared, podworker.load_support())

    def test_the_wrapper_stage_codes_are_the_wrappers_own(self):
        self.assertEqual(podbackend.WRAPPER_STAGES, podworker.STAGES)

    def test_the_time_limit_is_clamped_to_what_the_wrapper_accepts(self):
        spec = dataclasses.replace(self.backend.prepare(self.request()), timeout=10**6)
        self.assertEqual(self.backend.manifest(spec)["run"]["timeout"], pc.DEADLINE_CLAMP_SECONDS)
        prepared = types.SimpleNamespace(manifest=self.backend.manifest(spec), worktree=os.fspath(self.tree))
        podworker.read_run(prepared, podworker.load_support())
        short = dataclasses.replace(spec, timeout=1)
        self.assertEqual(self.backend.manifest(short)["run"]["timeout"], podbackend.MIN_LAUNCH_SECONDS)

    def test_the_memory_limit_is_the_requests_or_none_never_a_share_of_the_manager_pods(self):
        spec = self.backend.prepare(self.request())
        self.assertEqual(spec.memory_limit_mb, 0)  # not what ``capacity`` derives from this pod's resources
        self.assertIsNone(self.backend.manifest(spec)["run"]["memory_limit_mb"])
        explicit = self.backend.prepare(self.request(memory_limit_mb=2048))
        self.assertEqual(self.backend.manifest(explicit)["run"]["memory_limit_mb"], 2048)
        huge = dataclasses.replace(explicit, memory_limit_mb=10**9)
        self.assertEqual(self.backend.manifest(huge)["run"]["memory_limit_mb"], podbackend.MAX_MEMORY_LIMIT_MB)
        for spec_limit in (True, -1, "x"):
            with self.subTest(limit=spec_limit):
                self.assertIsNone(self.backend.manifest(dataclasses.replace(explicit, memory_limit_mb=spec_limit))["run"]["memory_limit_mb"])


class Outcomes(Case):
    """Every outcome of the report becomes what ``LocalBackend`` gives for the same end."""

    def snapshot(self):
        return {str(p.relative_to(self.tree)): p.read_bytes() for p in sorted(self.tree.rglob("*")) if p.is_file()}

    def delivered(self, **changes):
        data, count = delta_of(("file", "a.txt", b"edited\n"))
        self.happy(data, count, report=report_of(count, data=data, **changes))
        return data

    def test_the_agent_exit_code_is_the_report_exit_code(self):
        self.delivered(exit_code=3)
        with self.assertRaises(AgentError) as caught:
            self.run_session()
        self.assertEqual(str(caught.exception), f"agent exited 3 after a success result; see {self.prefix}.stderr")
        self.assertEqual((self.tree / "a.txt").read_bytes(), b"edited\n")  # the edits stay, as in process
        self.assertReleased(delivered=True)

    def test_a_timeout_outcome_is_the_agent_timeout_and_the_edits_made_stay_in_the_worktree_as_in_process(self):
        self.delivered(outcome="timeout", exit_code=-15)
        with self.assertRaises(AgentTimeout) as caught:
            self.run_session()
        self.assertEqual(caught.exception.args, AgentTimeout(self.timeout).args)
        self.assertEqual((self.tree / "a.txt").read_bytes(), b"edited\n")
        self.assertEqual(self.jsonl().read_bytes(), result_line())
        self.assertReleased(delivered=True)

    def test_a_memory_limit_outcome_is_the_memory_guard_error_with_the_spend_kept_and_the_numbers_of_the_report(self):
        self.delivered(outcome="memory_limit", exit_code=-9, exceeded_mb=5000)
        with self.assertRaises(AgentError) as caught:
            self.run_session()
        self.assertEqual(
            str(caught.exception), f"worker exceeded its memory limit: peak 5000 MiB over 4096 MiB; see {self.prefix}.stderr"
        )
        self.assertEqual(caught.exception.usage["total_cost_usd"], 0.5)
        self.assertEqual(caught.exception.usage["footprint"]["limit_mb"], 4096)
        self.assertEqual((self.tree / "a.txt").read_bytes(), b"edited\n")

    def test_a_memory_limit_outcome_without_numbers_falls_back_to_the_pod_level_text(self):
        self.delivered(outcome="memory_limit", exit_code=-9, exceeded_mb=None)
        with self.assertRaises(AgentError) as caught:
            self.run_session()
        self.assertIn("worker exceeded its memory limit: the pod was killed at its standard class limit", str(caught.exception))

    def test_a_terminated_outcome_is_a_retryable_failure_that_names_nothing_of_the_pod(self):
        self.delivered(outcome="terminated", exit_code=-15)
        with self.assertRaises(AgentError) as caught:
            self.run_session()
        self.assertEqual(
            str(caught.exception), f"{podbackend.FAILURES['terminated']}; see {self.prefix}.jsonl and {self.prefix}.stderr"
        )
        self.assertEqual(caught.exception.usage["total_cost_usd"], 0.5)  # the transcript had a result: the spend is kept
        self.assertEqual((self.tree / "a.txt").read_bytes(), b"edited\n")  # the work up to the signal is not lost

    def test_a_delta_status_that_is_not_ok_is_refused_before_the_delta_is_opened(self):
        for status_word, text in (("too_large", "delta_too_large"), ("failed", "delta_pod_failed")):
            with self.subTest(status_word):
                self.setUp()
                before = self.snapshot()
                self.happy(report=report_of(status=status_word))
                with patch.object(bundle, "apply_delta") as applied, patch.object(self.backend, "_open_out", wraps=self.backend._open_out) as opened:
                    with self.assertRaises(AgentError) as caught:
                        self.run_session()
                applied.assert_not_called()
                self.assertNotIn(tb.sr.OUT_DELTA, [call.args[1] for call in opened.call_args_list])
                self.assertEqual(str(caught.exception), f"{podbackend.FAILURES[text]}; see {self.prefix}.jsonl and {self.prefix}.stderr")
                self.assertEqual(self.snapshot(), before)
                self.assertReleased(delivered=True)

    def test_the_numbers_apply_delta_declares_are_the_reports_delta_numbers(self):
        data, count = delta_of(("file", "a.txt", b"x\n"), ("file", "b.txt", b"y\n"))
        report = report_of(count, data=data)
        self.assertEqual(report["delta"], {"status": "ok", "records": count, "bytes": len(data)})
        self.happy(data, count, report=report)
        with patch.object(bundle, "apply_delta", wraps=bundle.apply_delta) as applied:
            self.run_session()
        self.assertEqual((applied.call_args.kwargs["declared_records"], applied.call_args.kwargs["declared_bytes"]), (count, len(data)))

    def test_streams_that_differ_from_what_the_wrapper_says_the_relay_accepted_fail_the_session_and_apply_nothing(self):
        for name, streamed in (
            ("transcript", {"transcript": 1, "stderr": 0}),
            ("stderr", {"transcript": len(result_line()), "stderr": 7}),
        ):
            with self.subTest(name):
                self.setUp()
                before = self.snapshot()
                data, count = delta_of(("file", "a.txt", b"edited\n"))
                self.happy(data, count, report=report_of(count, data=data, streamed=streamed))
                with self.assertRaises(AgentError) as caught:
                    self.run_session()
                self.assertIn(podbackend.FAILURES["streams"], str(caught.exception))
                self.assertEqual(self.snapshot(), before)

    def test_dropped_output_with_no_result_event_is_the_local_no_result_message_and_with_one_it_is_a_success(self):
        dropped = {"transcript": True, "stderr": False}
        self.happy(transcript=b'{"type":"system"}\n', report=report_of(dropped=dropped))
        with self.assertRaises(RuntimeError) as caught:
            self.run_session()
        self.assertEqual(str(caught.exception), f"agent produced no result event; see {self.prefix}.jsonl")
        self.setUp()
        self.happy(report=report_of(dropped=dropped))
        result, _ = self.run_session()
        self.assertEqual(result, {"ok": True})

    def test_a_pod_that_succeeded_with_no_result_in_the_relay_is_a_failed_session(self):
        self.at(1, lambda: setattr(self.launcher, "state", SUCCEEDED))
        with self.assertRaises(RuntimeError) as caught:
            self.run_session()
        self.assertIn(podbackend.FAILURES["ended"], str(caught.exception))
        self.assertReleased(delivered=False)


class WrapperStages(Case):
    def exit_with(self, stage):
        self.at(1, lambda: setattr(self.launcher, "state", status("failed_exit", final=True, exit_code=podworker.STAGES[stage])))

    def test_every_stage_but_the_canary_is_a_fixed_failure_that_names_the_stage_only(self):
        for stage in podworker.STAGES:
            if stage == "canary":
                continue
            with self.subTest(stage):
                self.setUp()
                self.exit_with(stage)
                with self.assertRaises(RuntimeError) as caught:
                    self.run_session()
                self.assertIn(f"the pod worker failed at its {stage} stage", str(caught.exception))
                self.assertEqual(self.launcher.denied, [])

    def test_a_canary_stage_exit_is_an_isolation_failure_even_when_the_wrapper_could_not_post_why(self):
        self.exit_with("canary")
        with self.assertRaises(AgentError) as caught:
            self.run_session()
        self.assertIn("the pod's network isolation check failed (unknown)", str(caught.exception))
        self.assertEqual(self.launcher.denied, [podbackend.ISOLATION_REASON])
        self.assertIsNotNone(stopworld.read_state(self.data))
        self.assertReleased(delivered=False)

    def test_a_stage_exit_with_a_result_in_the_relay_is_still_collected(self):
        # The wrapper posted its result, then met a 410 on a retry: stage ``gone``. The relay's ``finished`` decides.
        self.happy()
        self.at(3, lambda: setattr(self.launcher, "state", status("failed_exit", final=True, exit_code=podworker.STAGES["gone"])))
        result, _ = self.run_session()
        self.assertEqual(result, {"ok": True})


# --- the pod pool (slice 18) --------------------------------------------------------------------------------------------------


class Pool(Case):
    def order(self):
        """Record the order of ``requested`` and the launch request."""
        calls = []
        original = podpool.Reservation.requested
        launch = self.launcher.launch

        def requested(reservation):
            calls.append("requested")
            return original(reservation)

        def launched(request, token):
            calls.append("launch")
            return launch(request, token)

        self.launcher.launch = launched
        patcher = patch.object(podpool.Reservation, "requested", requested)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_a_session_holds_one_reservation_for_its_whole_run_and_gives_it_back(self):
        seen = []
        self.launcher.on_poll = lambda: seen.append(self.ledger()["holders"])
        self.happy()
        self.run_session()
        self.assertTrue(seen)
        for holders in seen:
            self.assertEqual(len(holders), 1)
            self.assertEqual((holders[0]["job"], holders[0]["phase"], holders[0]["attempt"]), ("job-a", "build", 1))
            self.assertTrue(holders[0]["requested"])
        self.assertNothingHeld()

    def test_the_pool_is_asked_before_anything_is_staged_or_written(self):
        order = []
        with patch.object(podpool, "hold", side_effect=lambda *a, **k: order.append("hold") or (_ for _ in ()).throw(podpool.Refused("budget"))):
            with patch.object(bundle, "stage", side_effect=lambda *a, **k: order.append("stage")):
                with self.assertRaises(AgentError):
                    self.run_session()
        self.assertEqual(order, ["hold"])
        self.assertEqual(os.listdir(self.spool), [])  # no session record either

    def test_requested_is_called_immediately_before_the_launch_request_and_only_then(self):
        calls = self.order()
        self.happy()
        self.run_session()
        self.assertEqual(calls, ["requested", "launch"])

    def test_a_pool_that_lost_the_reservation_starts_nothing(self):
        with patch.object(podpool.Reservation, "requested", side_effect=podpool.Lost()):
            with self.assertRaises(AgentError) as caught:
                self.run_session()
        self.assertEqual(str(caught.exception), podbackend.POOL_LOST)
        self.assertEqual(self.launcher.requests, [])
        self.assertNothingHeld()

    def test_a_ledger_that_cannot_be_written_starts_nothing(self):
        with patch.object(podpool.Reservation, "requested", side_effect=podpool.Unavailable()):
            with self.assertRaises(AgentError) as caught:
                self.run_session()
        self.assertEqual(str(caught.exception), podbackend.POOL_REFUSED.format(code="unavailable"))
        self.assertEqual(self.launcher.requests, [])
        self.assertNothingHeld()

    def test_a_launch_that_did_not_happen_gives_the_pacing_token_back_and_one_that_was_sent_does_not(self):
        full = self.ledger()["pacing_tokens"]
        with patch.object(bundle, "stage", side_effect=bundle.BundleTooLarge("x")):
            with self.assertRaises(AgentError):
                self.run_session()
        self.assertEqual(self.ledger()["pacing_tokens"], full)
        self.assertEqual(self.launcher.requests, [])
        self.setUp()
        self.launcher.failures.append(launcher_client.Rejected("denied", ()))
        with self.assertRaises(AgentError):
            self.run_session()
        self.assertEqual(self.ledger()["pacing_tokens"], full - 1)
        self.assertNothingHeld()

    def test_the_slot_is_released_on_every_way_out(self):
        cases = {
            "a launcher that refuses": lambda: self.launcher.failures.append(launcher_client.Rejected("denied", ())),
            "a launcher that does not answer": lambda: self.launcher.failures.append(launcher_client.Unavailable()),
            "an untrusted socket": lambda: self.launcher.failures.append(launcher_client.ServerRefused()),
            "a failure to bind": lambda: patch.object(sr, "bind", side_effect=OSError()).start(),
            "an on_start that raises": lambda: None,
        }
        for name, arrange in cases.items():
            with self.subTest(name):
                self.setUp()
                self.addCleanup(patch.stopall)
                arrange()
                self.happy()

                def boom(started):
                    raise KeyboardInterrupt

                with self.assertRaises((AgentError, KeyboardInterrupt)):
                    if name == "an on_start that raises":
                        agent.run_agent("p", self.tree, self.prefix, "build", 600, boom, backend=self.backend)
                    else:
                        self.run_session()
                patch.stopall()
                self.assertNothingHeld()

    def test_a_launcher_at_capacity_gives_the_slot_back_while_it_waits_keeps_the_token_spent_and_asks_the_pool_again(self):
        full = self.ledger()["pacing_tokens"]
        self.launcher.failures.extend([launcher_client.Capacity(), launcher_client.Capacity()])
        held, tokens = [], []
        original = self.sleep

        def sleeping(seconds):
            if seconds == podbackend.CAPACITY_POLL_SECONDS:
                held.append(self.ledger()["in_use"])
                tokens.append(self.ledger()["pacing_tokens"])
            return original(seconds)

        self.backend = self.make_backend(sleep=sleeping)
        self.happy()
        self.script = {}
        self.steps_to_streaming()
        self.at(3, lambda: (self.pod.finish(report_of()), setattr(self.launcher, "state", SUCCEEDED)))
        self.run_session()
        self.assertGreaterEqual(len(held), 2)
        self.assertEqual(set(held), {0})  # nothing is reserved while the session waits for the launcher
        self.assertLess(max(tokens), full)  # the request was sent: the token is not refunded
        self.assertEqual(len(self.launcher.requests), 3)
        self.assertNothingHeld()

    def test_a_pool_at_its_ceiling_is_waited_on_nothing_is_staged_and_the_wait_ends_with_a_fixed_text(self):
        self.pool = podpool.PodPool(self.base / "zero", 0, clock=lambda: EPOCH + self.clock(), breaker_dir=self.data, sleep=self.sleep)
        self.pool.prepare()
        self.backend = self.make_backend(pool=self.pool)
        with self.assertRaises(AgentError) as caught:
            self.run_session()
        self.assertEqual(str(caught.exception), podbackend.POOL_WAITED.format(code="ceiling"))
        self.assertGreaterEqual(sum(self.sleeps), podbackend.CAPACITY_WAIT_SECONDS)
        self.assertEqual(self.launcher.requests, [])
        self.assertEqual(os.listdir(self.spool), [])
        self.assertNothingHeld()

    def test_the_ceiling_the_launcher_counts_is_honoured_through_the_live_count(self):
        counted = podpool.PodPool(
            self.base / "live", 4, clock=lambda: EPOCH + self.clock(), breaker_dir=self.data, sleep=self.sleep, live_count=lambda: 4
        )
        counted.prepare()
        self.backend = self.make_backend(pool=counted)
        with self.assertRaises(AgentError) as caught:
            self.run_session()
        self.assertEqual(str(caught.exception), podbackend.POOL_WAITED.format(code="ceiling"))

    def test_a_ceiling_that_clears_while_waiting_lets_the_session_through(self):
        state = {"live": 8}
        counted = podpool.PodPool(
            self.base / "clears", 4, clock=lambda: EPOCH + self.clock(), breaker_dir=self.data, sleep=self.sleep, live_count=lambda: state["live"]
        )
        counted.prepare()
        self.backend = self.make_backend(pool=counted)
        self.early.append(lambda: state.update(live=0) if sum(self.sleeps) > 20 else None)
        self.happy()
        result, _ = self.run_session()
        self.assertEqual(result, {"ok": True})
        self.assertGreater(sum(self.sleeps), 20)

    def test_the_run_budget_is_checked_with_the_phases_estimate_the_runs_spend_and_the_repositorys_budget(self):
        estimate = float(self.backend.prepare(self.request()).argv[self.backend.prepare(self.request()).argv.index("--max-budget-usd") + 1])
        self.backend = self.make_backend(budget_usd=estimate + 1, spent_usd=lambda: 2.0)  # 2 + estimate > estimate + 1
        with self.assertRaises(AgentError) as caught:
            self.run_session()
        self.assertEqual(str(caught.exception), podbackend.POOL_REFUSED.format(code="budget"))
        self.assertEqual(self.launcher.requests, [])
        self.assertLess(sum(self.sleeps), 10)  # a final refusal is not waited on
        self.setUp()
        self.backend = self.make_backend(budget_usd=estimate + 10, spent_usd=2.0)
        self.happy()
        result, _ = self.run_session()
        self.assertEqual(result, {"ok": True})

    def test_a_budget_held_by_another_session_of_the_run_is_a_wait_that_ends_when_it_is_released(self):
        spec = self.backend.prepare(self.request())
        estimate = self.backend._estimate(spec)
        other = podpool.Request(REPOSITORY, RUN_ID, "job-b", "build", 1, estimate, 600, 0, estimate * 1.5)
        held = self.pool.admit(other).reservation
        self.assertIsNotNone(held)
        self.backend = self.make_backend(budget_usd=estimate * 1.5)
        self.early.append(lambda: held.release() if sum(self.sleeps) > 20 else None)
        self.happy()
        result, _ = self.run_session()
        self.assertEqual(result, {"ok": True})
        self.assertGreater(sum(self.sleeps), 20)

    def test_a_cost_that_cannot_be_read_or_a_command_line_with_no_estimate_starts_nothing(self):
        def broken():
            raise OSError("POISON")

        self.backend = self.make_backend(spent_usd=broken)
        with self.assertRaises(AgentError) as caught:
            self.run_session()
        self.assertEqual(str(caught.exception), podbackend.BUDGET_UNKNOWN)
        self.assertNotIn("POISON", str(caught.exception))
        self.assertEqual(self.launcher.requests, [])
        spec = dataclasses.replace(self.backend.prepare(self.request()), argv=("claude",))
        with self.assertRaises(AgentError) as caught:
            self.backend._estimate(spec)
        self.assertEqual(str(caught.exception), podbackend.BUDGET_UNKNOWN)

    def test_the_breaker_is_refused_by_the_pool_with_the_breakers_own_text(self):
        stopworld.Breaker(self.data).trip(stopworld.ISOLATION_FAILED)
        with patch.object(self.backend, "_refuse_tripped"):  # past the backend's own check: the pool's is the one asked
            with self.assertRaises(AgentError) as caught:
                self.run_session()
        self.assertEqual(str(caught.exception), podbackend.TRIPPED)
        self.assertEqual(self.launcher.requests, [])
        self.assertNothingHeld()

    def test_a_session_that_already_holds_a_reservation_is_refused_as_a_duplicate(self):
        spec = self.backend.prepare(self.request())
        twin = podpool.Request(REPOSITORY, RUN_ID, "job-a", "build", 1, self.backend._estimate(spec), 600)
        held = self.pool.admit(twin).reservation
        self.addCleanup(held.release)
        with self.assertRaises(AgentError) as caught:
            self.run_session()
        self.assertEqual(str(caught.exception), podbackend.POOL_REFUSED.format(code="duplicate"))
        self.assertEqual(self.launcher.requests, [])

    def test_a_refusal_code_outside_the_closed_set_never_reaches_a_message(self):
        self.assertEqual(podbackend._closed("POISON"), "unavailable")
        for code in podpool.REFUSALS:
            self.assertEqual(podbackend._closed(code), code)

    def test_the_pool_messages_hold_only_a_closed_code(self):
        for template in (podbackend.POOL_WAITED, podbackend.POOL_REFUSED):
            for code in podpool.REFUSALS:
                self.assertRegex(template.format(code=code), r"^[a-z ()_]+; nothing was started$")


# --- the orphan sweep (slice 20) ---------------------------------------------------------------------------------------------


class Sweep(Case):
    def test_the_session_is_protected_from_before_the_pod_until_after_released_is_written(self):
        seen = {}
        launch = self.launcher.launch

        def launching(request, token):
            seen["at_launch"] = request["session_id"] in podsweep.protected()
            return launch(request, token)

        self.launcher.launch = launching
        mark = sr.mark

        def marking(spool, session, name):
            if name == sr.MARKER_RELEASED:
                seen["at_released"] = session in podsweep.protected()
            return mark(spool, session, name)

        self.launcher.on_poll = lambda: seen.setdefault("while_waiting", self.session in podsweep.protected())
        self.happy()
        with patch.object(sr, "mark", marking):
            self.run_session()
        self.assertEqual(seen, {"at_launch": True, "while_waiting": True, "at_released": True})
        self.assertEqual(podsweep.protected(), frozenset())

    def test_the_protection_is_dropped_on_a_failure_too(self):
        self.launcher.failures.append(launcher_client.Rejected("denied", ()))
        with self.assertRaises(AgentError):
            self.run_session()
        self.assertEqual(podsweep.protected(), frozenset())

    def listing(self, state="running"):
        return [
            {
                "name": pc.pod_name(self.session),
                "uid": POD_UID,
                "state": state,
                "final": False,
                "terminating": False,
                "manager": INSTANCE,
                "created": EPOCH,
                "deadline": EPOCH + 100_000,
            }
        ]

    def test_a_sweep_pass_in_the_middle_of_a_session_does_not_touch_its_pod_but_does_once_it_is_over(self):
        deleted = []
        sweeper = podsweep.Sweeper(lambda: self.listing(), lambda name, uid=None: deleted.append(name), self.spool, clock=lambda: EPOCH + self.clock() + 10_000)
        passes = []
        self.launcher.on_poll = lambda: passes.append(sweeper.run_once(lambda: podsweep.Recorded({}, True), lambda repo, run: True).total())
        self.happy()
        self.run_session()
        self.assertTrue(passes)
        self.assertEqual(sum(passes), 0)
        self.assertEqual(deleted, [])
        # The same pass over the same listing, with no launch of this process holding it, would have deleted it.
        sweeper.run_once(lambda: podsweep.Recorded({}, True), lambda repo, run: True)
        self.assertEqual(deleted, [pc.pod_name(self.session)])

    def test_the_sweeper_of_the_manager_is_built_only_for_the_switch_on_and_a_usable_environment(self):
        client = FakeLauncher(self)
        client.list_pods = lambda: []
        on = {SWITCH: "on", pc.ENV_MANAGER["spool_dir"]: "/spool"}
        built = podbackend.build_sweeper(on, client=client)
        self.assertIsInstance(built, podsweep.Sweeper)
        self.assertEqual(built._spool, "/spool")
        for value in (None, "", "off", "ON", "true"):
            env = {k: v for k, v in on.items() if k != SWITCH}
            if value is not None:
                env[SWITCH] = value
            with self.subTest(value=value):
                self.assertIsNone(podbackend.build_sweeper(env, client=client))
        self.assertIsNone(podbackend.build_sweeper({**on, pc.ENV_MANAGER["spool_dir"]: "relative"}, client=client))
        self.assertIsNone(podbackend.build_sweeper({**on, pc.ENV_MANAGER["launcher_socket"]: "relative.sock"}))

    def test_the_sweeper_is_wired_to_the_launchers_listing_and_deletion_and_the_spool(self):
        client = FakeLauncher(self)
        client.list_pods = lambda: self.listing()
        built = podbackend.build_sweeper({SWITCH: "on"}, client=client)
        self.assertEqual(built._list, client.list_pods)
        self.assertEqual(built._delete, client.delete_pod)
        self.assertEqual(built._spool, pc.SPOOL_DIR)

    def test_podexecution_builds_the_sweeper_only_for_the_switch_on(self):
        with patch.object(podbackend, "build_sweeper", return_value="swept") as built:
            self.assertIsNone(podexecution.sweeper({}))
            self.assertIsNone(podexecution.sweeper({SWITCH: "off"}))
            built.assert_not_called()
            self.assertEqual(podexecution.sweeper({SWITCH: "on"}), "swept")


# --- pod-side liveness (slice 19) --------------------------------------------------------------------------------------------


class Liveness(Case):
    def watch_terminations(self):
        """Record, at the moment of each termination, whether the session's ``revoke`` marker already existed."""
        seen = []
        terminate = self.launcher.terminate

        def terminating(handle):
            seen.append(self.marked(sr.MARKER_REVOKE))
            return terminate(handle)

        self.launcher.terminate = terminating
        return seen

    def test_a_pod_that_goes_silent_is_revoked_before_it_is_terminated_and_terminated_once(self):
        seen = self.watch_terminations()
        self.steps_to_streaming()
        with self.assertRaises(RuntimeError) as caught:
            self.run_session()
        self.assertIn(podbackend.FAILURES["silent"], str(caught.exception))
        self.assertEqual(seen, [True])
        self.assertEqual(len(self.launcher.terminated), 1)
        self.assertEqual(podhandle.live(), [])

    def test_enforce_is_the_function_that_terminates_for_a_terminate_verdict(self):
        self.steps_to_streaming()
        with patch.object(podliveness, "enforce", wraps=podliveness.enforce) as enforced:
            with self.assertRaises(RuntimeError):
                self.run_session()
        enforced.assert_called_once()
        verdict, handle, spool, terminate = enforced.call_args.args
        self.assertEqual((verdict.state, verdict.reason, verdict.action), ("silent", "no_heartbeat", "terminate"))
        self.assertEqual(handle.session, self.session)
        self.assertEqual(os.fspath(spool), os.fspath(self.spool))

    def test_a_terminate_verdict_that_could_not_confirm_the_pod_gone_leaves_the_handle_registered_for_the_kill_hook(self):
        self.launcher.terminate_error = launcher_client.Unavailable()
        self.steps_to_streaming()
        with self.assertRaises(RuntimeError):
            self.run_session()
        self.assertEqual([h.session for h in podhandle.live()], [self.session])

    def test_a_pod_that_never_got_a_node_is_ended_at_the_scheduling_window_and_revoked_first(self):
        seen = self.watch_terminations()
        self.launcher.state = status("pending")
        self.timeout = 7200
        with self.assertRaises(RuntimeError) as caught:
            self.run_session(timeout=7200)
        self.assertIn(podbackend.FAILURES["not_started"], str(caught.exception))
        window = pc.SCHEDULING_TIMEOUT_SECONDS + pc.LAUNCH_GRACE_SECONDS
        self.assertGreaterEqual(sum(self.sleeps), window)
        self.assertLess(sum(self.sleeps), window + 10)
        self.assertEqual(seen, [True])

    def test_a_pod_that_waited_long_for_its_node_is_not_taken_for_a_silent_one(self):
        """``podliveness`` measures silence from the creation: a pod that got its node after 400 s has not been heard in
        300 s and still has not misbehaved. The supervisor's own bounds (the fetch, the heartbeat) decide that case."""
        self.timeout = 7200
        self.launcher.state = status("pending")
        wait = 200  # polls of 2 s: 400 s

        def node():
            self.launcher.state = status("running")

        def start():
            self.assertEqual(self.pod.preflight(), 200)
            self.fetched = self.pod.fetch()

        self.at(wait, node)
        self.at(wait + 1, start)
        self.at(wait + 2, lambda: (self.pod.chunk("transcript", result_line()), self.pod.heartbeat()))
        self.at(wait + 3, lambda: (self.pod.finish(report_of()), setattr(self.launcher, "state", SUCCEEDED)))
        result, _ = self.run_session(timeout=7200)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(self.marked(sr.MARKER_REVOKE), False)

    def test_a_pod_that_runs_but_never_fetches_is_ended_by_the_start_bound_not_by_the_creation_silence(self):
        self.timeout = 7200
        with self.assertRaises(RuntimeError) as caught:
            self.run_session(timeout=7200)
        self.assertIn(podbackend.FAILURES["not_started"], str(caught.exception))
        self.assertGreater(sum(self.sleeps), podbackend.START_BOUND_SECONDS)

    def test_a_session_record_that_disappears_is_a_lost_session(self):
        self.steps_to_streaming()
        self.at(3, lambda: shutil.rmtree(self.spool / self.session))
        with self.assertRaises(RuntimeError) as caught:
            self.run_session()
        self.assertIn(podbackend.FAILURES["record_lost"], str(caught.exception))

    def test_an_expired_relay_session_ends_the_session_with_the_revoke_first(self):
        seen = self.watch_terminations()
        self.steps_to_streaming()
        self.at(3, lambda: self.clock.advance(10_000))
        self.timeout = 3600
        with self.assertRaises(RuntimeError) as caught:
            self.run_session(timeout=3600)
        self.assertIn(podbackend.FAILURES["expired"], str(caught.exception))
        self.assertEqual(seen, [True])

    def test_a_revoked_session_ends_the_session(self):
        self.steps_to_streaming()
        self.at(3, lambda: sr.mark(self.spool, self.session, sr.MARKER_REVOKE))
        with self.assertRaises(RuntimeError) as caught:
            self.run_session()
        self.assertIn(podbackend.FAILURES["revoked"], str(caught.exception))

    def test_a_pod_that_is_terminating_is_waited_on_for_its_grace_and_may_still_post_its_result(self):
        self.steps_to_streaming()
        self.at(3, lambda: setattr(self.launcher, "state", status("running", final=True, terminating=True)))
        self.at(8, lambda: (self.pod.finish(report_of()), setattr(self.launcher, "state", SUCCEEDED)))
        result, _ = self.run_session()
        self.assertEqual(result, {"ok": True})

    def test_a_probe_reads_the_pod_before_the_relay(self):
        order = []
        read = podliveness.read_relay
        self.launcher.on_poll = lambda: order.append("pod")
        with patch.object(podliveness, "read_relay", side_effect=lambda *a: order.append("relay") or read(*a)):
            self.happy()
            self.run_session()
        self.assertEqual(order[:2], ["pod", "relay"])

    def test_the_time_limit_has_a_margin_for_the_wrapper_to_stop_the_agent_and_post_the_delta(self):
        self.timeout = 30
        self.steps_to_streaming()
        with self.assertRaises(AgentTimeout):
            self.run_session(timeout=30)
        # The session started at the second poll; the supervisor's own backstop is the limit the wrapper was given plus the margin.
        self.assertGreaterEqual(sum(self.sleeps) - 4, podbackend.MIN_LAUNCH_SECONDS + podbackend.TIMEOUT_MARGIN_SECONDS)
        self.assertLess(sum(self.sleeps) - 4, podbackend.MIN_LAUNCH_SECONDS + podbackend.TIMEOUT_MARGIN_SECONDS + 10)


# --- the wiring ---------------------------------------------------------------------------------------------------------------


class Wiring(unittest.TestCase):
    def setUp(self):
        podhandle.reset()
        podbackend.clear_scope()
        podbackend.clear_pool()
        self.addCleanup(podhandle.reset)
        self.addCleanup(podbackend.clear_scope)
        self.addCleanup(podbackend.clear_pool)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = os.path.realpath(self.temp.name)
        self.hooks = []
        for name, effect in (
            ("register_kill_hook", lambda hook: self.hooks.append(hook) or hook),
            ("unregister_kill_hook", lambda hook: self.hooks.remove(hook)),
        ):
            patcher = patch(f"factory.locks.{name}", side_effect=effect)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.env = {SWITCH: "on", pc.ENV_MANAGER["max_pod_sessions"]: "4", "FACTORY_DATA": self.data}

    class Client(FakeLauncher):
        def __init__(self, live=0, inflight=0):
            super().__init__(unittest.TestCase())
            self.counts = {"live": live, "inflight": inflight}

        def info(self):
            return dict(self.counts)

    def test_activate_builds_the_one_pool_with_the_launchers_count_and_deactivate_removes_it(self):
        client = self.Client(live=3, inflight=1)
        undo = podbackend.activate(REPOSITORY, RUN_ID, self.env, client=client)
        pool = podbackend.current_pool()
        self.assertIsInstance(pool, podpool.PodPool)
        self.assertEqual(pool.ceiling, 4)
        refused = pool.admit(podpool.Request(REPOSITORY, RUN_ID, "job-a", "build", 1, 1.0, 600))
        self.assertEqual(refused.refusal.code, "ceiling")  # the launcher's 3 + 1 fill the ceiling
        undo()
        self.assertIsNone(podbackend.current_pool())

    def test_without_a_ceiling_no_pool_is_built_and_every_session_is_refused_never_run_unguarded(self):
        env = {k: v for k, v in self.env.items() if k != pc.ENV_MANAGER["max_pod_sessions"]}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            podbackend.activate(REPOSITORY, RUN_ID, env, client=self.Client())
        self.assertEqual(err.getvalue(), "factory: pod backend has no pod pool: its configuration was refused\n")
        self.assertIsNone(podbackend.current_pool())
        env = {**env, pc.MANAGER_INSTANCE_ENV: INSTANCE, pc.ENV_MANAGER["launcher_socket"]: "/run/factory/launcher.sock", "FACTORY_DATA": pc.SCRATCH_DIR}
        chosen = podexecution.backend_for("pod", env, job_id="job-a", attempt=1)
        self.assertEqual(chosen.reason, podexecution.REFUSED_UNCONFIGURED)

    def test_a_ceiling_that_is_not_a_plain_integer_builds_no_pool(self):
        for value in ("8.0", " 8", "-1", "", "x", "99999"):
            with self.subTest(value=value):
                with contextlib.redirect_stderr(io.StringIO()):
                    podbackend.activate(REPOSITORY, RUN_ID, {**self.env, pc.ENV_MANAGER["max_pod_sessions"]: value}, client=self.Client())
                self.assertIsNone(podbackend.current_pool())

    def test_the_kill_hook_the_reader_and_the_terminator_are_installed_for_the_process(self):
        client = self.Client()
        undo = podbackend.activate(REPOSITORY, RUN_ID, self.env, client=client)
        handle = podhandle.PodHandle(pc.pod_name("1" * 32), POD_UID, "1" * 32, "launcher", "2026-01-01T00:00:00Z")
        self.assertEqual(podhandle.pod_status(handle.record()), "alive")
        podhandle.register(handle)
        self.assertEqual(len(self.hooks), 1)
        self.assertEqual(self.hooks[0](), 0)
        self.assertEqual(client.terminated, [handle])
        undo()
        self.assertEqual(self.hooks, [])
        self.assertEqual(podhandle.pod_status(handle.record()), "unknown")

    def test_the_backend_the_environment_builds_carries_the_pool_and_the_budget_seams(self):
        client = self.Client()
        podbackend.activate(REPOSITORY, RUN_ID, self.env, client=client)
        env = {**self.env, pc.MANAGER_INSTANCE_ENV: INSTANCE, "FACTORY_DATA": pc.SCRATCH_DIR}
        chosen = podbackend.PodBackend.from_environment(env, job_id="job-a", attempt=1, client=client, spent_usd=lambda: 1.5, budget_usd=9.0)
        self.assertIs(chosen._pool, podbackend.current_pool())
        self.assertEqual((chosen._spent(), chosen._budget), (1.5, 9.0))
        podbackend.clear_pool()  # a process whose pool was not built cannot run a session
        with self.assertRaises(podbackend.PodConfigError):
            podbackend.PodBackend.from_environment(env, job_id="job-a", attempt=1, client=client)

    def test_podexecution_hands_the_budget_seams_to_the_backend_and_nothing_to_in_process(self):
        with patch.object(podbackend.PodBackend, "from_environment", return_value="built") as built:
            env = {SWITCH: "on"}
            kwargs = podexecution.agent_kwargs("pod", env, job_id="j", attempt=1, spent_usd=3.0, budget_usd=7.0)
        self.assertEqual(kwargs, {"backend": "built"})
        self.assertEqual((built.call_args.kwargs["spent_usd"], built.call_args.kwargs["budget_usd"]), (3.0, 7.0))
        self.assertEqual(podexecution.agent_kwargs("inprocess", {SWITCH: "on"}, job_id="j", attempt=1, spent_usd=3.0, budget_usd=7.0), {})


class FreshInterpreter(unittest.TestCase):
    """The default stays off and inert: with the switch off no pod module is imported, nothing is built."""

    def run_code(self, code, **env):
        environ = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        environ.pop(SWITCH, None)
        environ.update(env)
        done = subprocess.run([sys.executable, "-c", code], env=environ, capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def test_no_pod_module_is_imported_with_the_switch_off(self):
        out = self.run_code(
            "import sys\n"
            "from factory import cli, runner, planner, preflight, podexecution, backend, agent\n"
            "assert podexecution.agent_kwargs('inprocess', {}, job_id='a', attempt=1, spent_usd=1, budget_usd=2) == {}\n"
            "assert podexecution.backend_for('pod', {}, spent_usd=1).reason == podexecution.REFUSED_SWITCH_OFF\n"
            "assert podexecution.activate('o/r', 'run-1', {})() is None\n"
            "assert podexecution.sweeper({}) is None and podexecution.sweeper({'FACTORY_POD_BACKEND': 'off'}) is None\n"
            "banned = ['podbackend', 'podpool', 'podsweep', 'podliveness', 'podreport', 'podstatus', 'podworker', 'launcher_client', 'relay', 'bundle']\n"
            "loaded = [m for m in banned if 'factory.' + m in sys.modules]\n"
            "print(loaded)\n"
        )
        self.assertEqual(out.strip(), "[]")

    def test_the_switch_on_loads_the_backend_and_only_then(self):
        out = self.run_code(
            "import sys\n"
            "from factory import podexecution\n"
            "assert 'factory.podbackend' not in sys.modules\n"
            "podexecution.sweeper({'FACTORY_POD_BACKEND': 'on'})\n"
            "print('factory.podbackend' in sys.modules, 'factory.podsweep' in sys.modules)\n"
        )
        self.assertEqual(out.strip(), "True True")

    def test_the_pool_reads_nothing_and_creates_nothing_while_the_switch_is_off(self):
        with tempfile.TemporaryDirectory() as data:
            before = set(os.listdir(data))
            self.assertIsNone(podpool.from_env({"FACTORY_DATA": data}))
            self.assertIsNone(podpool.from_env({"FACTORY_DATA": data, SWITCH: "off"}))
            self.assertEqual(set(os.listdir(data)), before)


# --- one real wrapper against the backend ----------------------------------------------------------------------------------


CLAUDE_STUB = """
import json, os, sys, hashlib
prompt = sys.stdin.buffer.read()
mode = os.environ.get("STUB_MODE", "")
open("README", "w").write("changed by the agent\\n")
os.makedirs("src", exist_ok=True)
open("src/new.py", "w").write("print(2)\\n")
sys.stderr.write("a warning\\n")
print(json.dumps({"type": "system", "prompt_sha": hashlib.sha256(prompt).hexdigest()}), flush=True)
print(json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "structured_output": {"ok": True, "prompt_bytes": len(prompt)},
    "total_cost_usd": 0.25, "num_turns": 2, "duration_ms": 10,
    "usage": {"input_tokens": 10, "output_tokens": 5},
}), flush=True)
"""


class Hand:
    """``FakeLauncher.case`` for the end-to-end class: the launch hands the pod its credentials, the backend's sleep runs the pod."""

    def __init__(self, case):
        self.case = case

    def on_launch(self, session, token):
        self.case.session, self.case.token = session, token


class EndToEnd(twr.RunBase):
    """The REAL wrapper (part A and part B) runs the bundle the REAL backend staged, against the real relay over a socket.

    The supervisor and the pod share the data directory's path (``/data`` in production), so the test sets the supervisor's
    tree aside for the length of the pod's run and puts it back before the backend looks at the result.
    """

    stub_environment = {}

    def setUp(self):
        super().setUp()
        podhandle.reset()
        podsweep.reset()
        self.addCleanup(podhandle.reset)
        self.addCleanup(podsweep.reset)
        self.data = Path(os.path.realpath(self.pod))
        self.tree = self.data / "worktrees" / "job"
        self.prefix = self.data / "repos" / "octo" / "widgets" / "runs" / RUN_ID / "job-a" / "1-build"
        for directory in (self.tree / "src", self.prefix.parent):
            directory.mkdir(parents=True)
        (self.tree / "README").write_text("hello\n")
        (self.tree / "src" / "a.py").write_text("print(1)\n")
        fakegit = self.base / "fakegit" / ".git"
        fakegit.mkdir(parents=True)
        (fakegit / "HEAD").write_text("ref: refs/heads/job\n")
        for target, name, value in (
            (bundle, "build_git_dir", {"return_value": os.fspath(fakegit)}),
            (gitsafe, "checkout_gitdir", {"return_value": os.fspath(self.base / "unused")}),
            (agent, "git_common_dir", {"return_value": None}),
        ):
            patcher = patch.object(target, name, **value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.binary = self.base / "bin" / "claude"
        self.binary.parent.mkdir()
        self.agent_script(CLAUDE_STUB)
        patcher = patch.dict(os.environ, {"FACTORY_AGENT_CMD": os.fspath(self.binary)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fake_clock = FakeClock(1000.0)
        self.session = self.token = None
        self.ran = False
        self.worker_result = None
        self.launcher = FakeLauncher(Hand(self))
        self.pool = podpool.PodPool(self.base / "pool", 8, breaker_dir=self.data)
        self.pool.prepare()
        self.backend = podbackend.PodBackend(
            client=self.launcher,
            pool=self.pool,
            spool=self.spool,
            data_root=self.data,
            scope=podbackend.Scope(REPOSITORY, RUN_ID),
            job_id="job-a",
            attempt=1,
            manager_instance=INSTANCE,
            breaker_dir=self.data,
            clock=self.fake_clock,
            sleep=self.sleep,
            poll_seconds=2.0,
        )

    def agent_script(self, body):
        self.binary.write_text(f"#!{sys.executable}\n{textwrap.dedent(body)}")
        os.chmod(self.binary, 0o755)

    def sleep(self, seconds):
        self.fake_clock.advance(seconds)
        if self.session is not None and not self.ran:
            self.ran = True
            self.run_pod()

    def run_pod(self):
        aside = self.base / "supervisor-data"
        os.rename(self.data, aside)
        self.data.mkdir()
        try:
            self.write_key()
            env = self.environment(self.session, FACTORY_POD_UID=POD_UID)
            self.worker_result = self.run_worker(
                self.session, self.token, runner=None, fresh=False, tuning=self.tuning(), env=env
            )
        finally:
            shutil.rmtree(self.data)
            os.rename(aside, self.data)
        self.launcher.state = SUCCEEDED

    def run_session(self):
        return agent.run_agent("PROMPT TEXT", self.tree, self.prefix, "build", 600, backend=self.backend)

    def test_a_session_runs_through_the_real_wrapper_and_the_delta_lands_in_the_supervisors_worktree(self):
        result, usage = self.run_session()
        self.assertEqual(self.worker_result.code, 0, self.worker_result.err)
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["prompt_bytes"], len(b"PROMPT TEXT"))
        self.assertEqual((usage["phase"], usage["total_cost_usd"]), ("build", 0.25))
        self.assertEqual((self.tree / "README").read_text(), "changed by the agent\n")
        self.assertEqual((self.tree / "src" / "new.py").read_text(), "print(2)\n")
        self.assertEqual((self.tree / "src" / "a.py").read_text(), "print(1)\n")
        self.assertEqual(agent.sibling(self.prefix, ".stderr").read_text(), "a warning\n")
        self.assertEqual(json.loads(agent.sibling(self.prefix, ".jsonl").read_text().splitlines()[-1])["subtype"], "success")
        self.assertTrue(sr.is_marked(self.spool, self.session, sr.MARKER_RELEASED))
        self.assertFalse(sr.is_marked(self.spool, self.session, sr.MARKER_REVOKE))
        self.assertEqual(self.pool.snapshot()["in_use"], 0)
        self.assertEqual(podsweep.protected(), frozenset())
        self.assertEqual(podhandle.live(), [])

    def test_the_wrapper_validated_the_manifest_the_backend_wrote_and_ran_the_agent_with_its_argv(self):
        self.run_session()
        self.assertEqual(self.popen.calls[0][0][0], os.fspath(self.binary))
        self.assertIn("--max-budget-usd", self.popen.calls[0][0])
        self.assertEqual(self.worker_result.err, "")
        self.assertEqual(
            self.worker_result.out.splitlines(),
            ["podworker: started", "podworker: preflight ok", "podworker: bundle fetched", "podworker: exit 0", "podworker: result posted"],
        )

    def test_an_agent_exit_status_reaches_the_caller_as_it_does_in_process(self):
        self.agent_script(CLAUDE_STUB + "\nsys.exit(3)\n")
        with self.assertRaises(AgentError) as caught:
            self.run_session()
        self.assertEqual(str(caught.exception), f"agent exited 3 after a success result; see {self.prefix}.stderr")
        self.assertEqual((self.tree / "README").read_text(), "changed by the agent\n")  # the edits stay, as in process

    def test_an_agent_killed_by_a_signal_is_a_negative_exit_code(self):
        self.agent_script(CLAUDE_STUB + "\nimport signal\nos.kill(os.getpid(), signal.SIGKILL)\n")
        with self.assertRaises(AgentError) as caught:
            self.run_session()
        self.assertEqual(str(caught.exception), f"agent exited -9 after a success result; see {self.prefix}.stderr")

    def test_an_agent_that_wrote_no_result_event_is_the_local_no_result_failure(self):
        self.agent_script("import sys\nsys.stdin.buffer.read()\nprint('{}', flush=True)\n")
        with self.assertRaises(RuntimeError) as caught:
            self.run_session()
        self.assertEqual(str(caught.exception), f"agent produced no result event; see {self.prefix}.jsonl")
        self.assertEqual(self.worker_result.code, 0)

    def test_an_agent_that_deletes_and_adds_files_and_links_is_applied_with_the_reports_numbers(self):
        self.agent_script(
            CLAUDE_STUB.replace('open("README", "w")', 'os.unlink("src/a.py"); os.symlink("README", "alias"); open("README", "w")')
        )
        self.run_session()
        self.assertFalse((self.tree / "src" / "a.py").exists())
        self.assertEqual(os.readlink(self.tree / "alias"), "README")


if __name__ == "__main__":
    unittest.main()

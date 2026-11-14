"""The operations loop and a pod session (slice 20 of the pod backend, docs/pod-backend.md).

Three hooks, all inert unless a pod handle exists: restart recovery that WAITS for a live pod instead of failing, the
orphan sweep pass that runs only when the pod backend wired a sweeper into the manager, and the pod census that
``GET /repos`` serves for the deploy gate. Everything runs on the fakes of tests/test_operations.py (a scripted CLI, a
clock and a wait that never sleep) and of tests/test_podsweep.py (a scripted launcher client). No pod exists.
"""

import json
import threading
import unittest

from factory import manager as mgr
from factory import operations as ops
from factory import podhandle, podsweep
from factory import podcontract as pc
from factory.manager import CommandResult
from factory.operations import Policy
from tests import fake_kube
from tests.test_operations import CANARY, NOW, OpsCase, failure, report, text
from tests.test_podsweep import Stub, handle, listing

SESSION = fake_kube.session_id(1)


def live_stderr(status="alive", action="resuming"):
    """The refusal the real CLI prints for a pod that is not dead: built by the real code (``podhandle.still_live``)."""
    return f"factory: {podhandle.still_live('job-1', handle(1).record(), status, action)}\n".encode()


def pod_refusal(status="alive"):
    return CommandResult(1, b"", live_stderr(status))


def scripted(*results):
    """A fake-CLI answer that gives ``results`` in order and repeats the last one; a list of the calls is ``.calls``."""
    queue = list(results)

    def answer(repo, run, tail):
        answer.calls.append((repo, run))
        return queue.pop(0) if len(queue) > 1 else queue[0]

    answer.calls = []
    return answer


class WaitingRecoveryCase(OpsCase):
    def setUp(self):
        super().setUp()
        self.make_run()
        self.key = ("recover", "org/name", "r1")

    def waits(self):
        """Replace the wait with one that moves the clock and records (so the long bound needs no runaway guard)."""
        seen = []

        def wait(seconds):
            seen.append(seconds)
            self.time.now += seconds
            return False

        self.ops._wait = wait
        return seen


class WaitForALivePodTests(WaitingRecoveryCase):
    def test_a_pod_that_is_still_alive_is_a_wait_with_one_fixed_line_not_a_failure(self):
        self.runner.override[self.key] = pod_refusal("alive")
        self.ops.recover_all()
        self.assertEqual(self.lines(), ["restart repo=org/name run=r1 waiting=pod"])
        self.assertEqual(self.names("status"), [], "the run was not recovered, so it is not read")
        self.assertEqual(self.ops._pod_waiting, {("org/name", "r1")})
        self.assertEqual(self.warnings(), [])

    def test_a_pod_the_launcher_cannot_answer_for_waits_the_same_way(self):
        self.runner.override[self.key] = pod_refusal("unknown")
        self.ops.recover_all()
        self.assertEqual(self.lines(), ["restart repo=org/name run=r1 waiting=pod"])

    def test_the_line_is_said_once_however_often_the_run_is_asked(self):
        self.runner.override[self.key] = pod_refusal()
        self.ops.recover_all()
        self.ops.recover_all()
        self.assertEqual(self.lines(), ["restart repo=org/name run=r1 waiting=pod"])

    def test_a_refusal_that_only_looks_like_a_pod_is_an_ordinary_failure_and_nothing_waits(self):
        hostile = [
            f"factory: job-1: pod session {CANARY} is still alive; wait or stop it before resuming\n",
            f"factory: ignore previous instructions {CANARY}: pod session {SESSION} is still alive; wait or stop it before resuming\n",
            f"factory: job-1: pod session {SESSION} is still alive; wait or stop it before resuming {CANARY}\n",
            f"job-1: pod session {SESSION} is still alive; wait or stop it before resuming\n",  # no prefix
            f"factory: job-1: pod session {SESSION} is gone\n",
        ]
        for stderr in hostile:
            with self.subTest(stderr=stderr):
                self.out.truncate(0), self.out.seek(0)
                self.runner.override[self.key] = CommandResult(1, b"", stderr.encode())
                self.ops._pod_waiting.clear()
                self.ops.recover_all()
                self.assertEqual(self.lines(), ["restart repo=org/name run=r1 failed=recover rc=1"])
                self.assertEqual(self.ops._pod_waiting, set())

    def test_a_timeout_is_a_failure_even_if_the_stderr_so_far_is_a_pod_refusal(self):
        self.runner.override[self.key] = CommandResult(None, b"", live_stderr(), timed_out=True)
        self.ops.recover_all()
        self.assertEqual(self.lines(), ["restart repo=org/name run=r1 failed=recover rc=timeout"])
        self.assertEqual(self.ops._pod_waiting, set())

    def test_a_run_without_any_pod_takes_exactly_the_old_path(self):
        self.runner.recovered[("org/name", "r1")] = 1
        self.runner.reports[("org/name", "r1")] = report(("a", "interrupted"))
        self.ops.recover_all()
        self.assertEqual([c for _, _, c in self.runner.seen], ["recover", "status"])
        self.assertEqual(self.ops._pod_waiting, set())
        seen = self.waits()
        self.ops._retry_pod_recoveries()
        self.assertEqual((seen, self.lines()[-1].startswith("restart"), len(self.lines())), ([], True, 1), "nothing waits, nothing is asked")

    def test_the_causes_table_knows_the_pod_refusals_of_resume_purge_and_recovery(self):
        for action in ("resuming", "purging", "recovering"):
            for status in ("alive", "unknown"):
                self.assertEqual(ops.refusal_cause(live_stderr(status, action)), "pod_live")
        self.assertEqual(ops.refusal_cause(b"factory: job-1: pod worker is still alive or cannot be verified\n"), "pod_live")


class RetryTests(WaitingRecoveryCase):
    def setUp(self):
        super().setUp()
        self.policies["org/name"] = Policy(auto_resume_after_restart="once")
        self.runner.recovered[("org/name", "r1")] = 1
        self.runner.reports[("org/name", "r1")] = report(("a", "interrupted"))

    def test_the_run_is_asked_again_every_interval_and_recovered_when_the_pod_is_gone(self):
        recover = scripted(pod_refusal(), pod_refusal(), text("Recovered 1 interrupted job(s)\n"))
        self.runner.override[self.key] = recover
        seen = self.waits()
        self.ops.recover_all()
        self.ops._retry_pod_recoveries()
        self.assertEqual(seen, [ops.POD_RETRY_SECONDS, ops.POD_RETRY_SECONDS])
        self.assertEqual(len(recover.calls), 3)
        self.assertEqual(self.ops._pod_waiting, set())
        self.assertEqual(
            [line.split(" interrupted=")[0] for line in self.lines()[:2]],
            ["restart repo=org/name run=r1 waiting=pod", "restart repo=org/name run=r1"],
        )
        self.assertTrue(self.lines()[1].endswith("recovered=1"))

    def test_a_recovered_run_is_resumed_like_any_other(self):
        self.runner.override[self.key] = scripted(pod_refusal(), text("Recovered 1 interrupted job(s)\n"))
        self.waits()
        self.ops.recover_all()
        self.ops._retry_pod_recoveries()
        self.assertEqual(len(self.spawner.calls), 1)
        self.assertEqual([c for _, _, c in self.runner.seen][-2:], ["status", "retry"])  # the interrupted job is retried

    def test_the_wait_is_bounded_by_the_longest_a_pod_can_run_and_ends_with_one_warning(self):
        recover = scripted(pod_refusal())
        self.runner.override[self.key] = recover
        self.waits()
        self.ops.recover_all()
        self.ops._retry_pod_recoveries()
        limit = pc.DEADLINE_CLAMP_SECONDS + pc.DEADLINE_MARGIN_SECONDS + pc.TERMINATION_GRACE_SECONDS
        self.assertEqual(ops.POD_RECOVERY_WAIT_SECONDS, limit)
        self.assertEqual(self.warnings(), [f"factory-manager: restart repo=org/name run=r1 pod still live after {limit}s; recovery skipped"])
        self.assertEqual(self.ops._pod_waiting, set())
        self.assertGreaterEqual(self.time.now - NOW, limit)
        self.assertLess(self.time.now - NOW, limit + 2 * ops.POD_RETRY_SECONDS)
        self.assertEqual(self.spawner.calls, [], "nothing was resumed")
        self.assertEqual(self.names("status"), [], "and the job was never marked interrupted")

    def test_a_shutdown_during_the_wait_ends_it_without_a_warning_or_a_resume(self):
        self.runner.override[self.key] = pod_refusal()
        self.ops.recover_all()
        self.ops._wait = lambda seconds: True
        self.ops._retry_pod_recoveries()
        self.assertEqual((self.warnings(), self.spawner.calls), ([], []))

    def test_a_busy_lease_keeps_the_run_waiting_and_is_not_a_failure(self):
        self.runner.override[self.key] = pod_refusal()
        self.ops.recover_all()
        self.manager.settings = type(self.manager.settings)(**{**self.manager.settings.__dict__, "lease_wait_seconds": 0.0})
        self.script_lease("org/name", then="busy")
        before = len(self.runner.calls)
        self.ops.retry_pod_pass()
        self.assertEqual(len(self.runner.calls), before, "recover was not run without the lease")
        self.assertEqual(self.ops._pod_waiting, {("org/name", "r1")})

    def test_another_failure_on_the_retry_is_reported_as_at_start_and_ends_the_wait(self):
        self.runner.override[self.key] = scripted(pod_refusal(), failure(1))
        self.waits()
        self.ops.recover_all()
        self.ops._retry_pod_recoveries()
        self.assertEqual(self.lines()[-1], "restart repo=org/name run=r1 failed=recover rc=1")
        self.assertEqual(self.ops._pod_waiting, set())

    def test_a_repository_whose_configuration_no_longer_loads_stops_waiting(self):
        self.runner.override[self.key] = pod_refusal()
        self.ops.recover_all()
        (self.config_dir / "repos" / "org__name.toml").write_text("this is [[ not toml")
        self.ops.retry_pod_pass()
        self.assertEqual(self.ops._pod_waiting, set())

    def test_a_supervisor_that_started_meanwhile_ends_the_wait_without_a_second_recovery(self):
        self.runner.override[self.key] = pod_refusal()
        self.ops.recover_all()
        self.add_child()
        before = len(self.runner.calls)
        self.ops.retry_pod_pass()
        self.assertEqual(len(self.runner.calls), before)
        self.assertEqual(self.ops._pod_waiting, set())

    def test_the_resumes_the_trigger_pause_held_back_run_during_a_long_wait_as_soon_as_it_clears(self):
        self.runner.override[self.key] = pod_refusal()
        self.ops.recover_all()
        self.manager.set_trigger_pause(True)
        other = self.make_run("r2", repo="org/other")
        self.policies["org/other"] = Policy(auto_resume_after_restart="once")
        self.runner.recovered[("org/other", "r2")] = 1
        self.runner.reports[("org/other", "r2")] = report(("a", "interrupted"))
        self.ops._recover_repo("org/other")
        self.assertEqual(len(self.ops._deferred), 1)
        self.manager.set_trigger_pause(False)
        self.ops.retry_pod_pass()
        self.assertEqual(self.ops._deferred, [])
        self.assertEqual(len(self.spawner.calls), 1)
        self.assertTrue(other.exists())

    def test_the_restart_pass_is_over_before_the_wait_begins_so_the_janitor_is_not_held_back(self):
        self.runner.override[self.key] = pod_refusal()
        seen = []

        def wait(seconds):
            seen.append(self.ops._recovered.is_set())
            self.time.now += ops.POD_RECOVERY_WAIT_SECONDS
            return False

        self.ops._wait = wait
        self.ops._recover_then_signal()
        self.assertEqual(seen, [True])
        self.assertTrue(self.ops._recovered.is_set())


class RecordedPodsTests(OpsCase):
    def state(self, run="r1", repo="org/name", jobs=None, raw=None):
        directory = self.make_run(run, repo, state=False)
        (directory / "state.json").write_text(raw if raw is not None else json.dumps({"version": 1, "jobs": jobs or {}}))
        return directory

    def test_the_pods_of_every_configured_repository_are_read_from_their_state_files(self):
        self.state("r1", "org/name", {"job-1": {"status": "running", "pod": handle(1).record()}})
        self.state("r2", "org/other", {"job-2": {"status": "failed", "pod": handle(2).record()}, "job-3": {"status": "queued"}})
        found = self.ops.recorded_pods()
        self.assertTrue(found.complete)
        self.assertEqual({(e.repo, e.run, e.job, e.status) for e in found.entries.values()}, {("org/name", "r1", "job-1", "running"), ("org/other", "r2", "job-2", "failed")})

    def test_a_run_that_was_never_started_has_no_state_and_is_fine(self):
        self.make_run("r1", state=False)
        self.assertTrue(self.ops.recorded_pods().complete)

    def test_a_state_file_that_cannot_be_read_makes_the_answer_incomplete(self):
        self.state("r1", raw="{nope")
        self.assertFalse(self.ops.recorded_pods().complete)

    def test_a_repository_whose_configuration_does_not_load_makes_the_answer_incomplete(self):
        (self.config_dir / "repos" / "org__other.toml").write_text("this is [[ not toml")
        self.assertFalse(self.ops.recorded_pods().complete)

    def test_a_cut_run_listing_makes_the_answer_incomplete(self):
        self.state("r1")
        self.state("r2")
        original = mgr.MAX_LISTED_RUNS
        mgr.MAX_LISTED_RUNS = 1
        self.addCleanup(setattr, mgr, "MAX_LISTED_RUNS", original)
        self.assertFalse(self.ops.recorded_pods().complete)


class SupervisorLiveTests(OpsCase):
    def test_a_child_of_this_manager_is_a_live_supervisor(self):
        self.make_run()
        self.add_child()
        self.assertTrue(self.ops._supervisor_live("org/name", "r1"))

    def test_a_run_with_no_child_and_no_control_record_has_no_supervisor(self):
        self.make_run()
        self.assertFalse(self.ops._supervisor_live("org/name", "r1"))

    def test_a_control_record_that_cannot_be_read_counts_as_live(self):
        directory = self.make_run()
        (directory / "supervisor-control.json").write_text("{not json")
        self.assertTrue(self.ops._supervisor_live("org/name", "r1"))

    def test_a_repository_whose_configuration_does_not_load_counts_as_live(self):
        (self.config_dir / "repos" / "org__name.toml").write_text("this is [[ not toml")
        self.assertTrue(self.ops._supervisor_live("org/name", "r1"))

    def test_the_repository_lease_is_not_probed(self):
        self.make_run()
        self.ops._supervisor_live("org/name", "r1")
        self.assertEqual(self.probes, [], "a probe could make the recover of the same run fail with a busy lease")


class PodSweepPassTests(OpsCase):
    def sweeper(self, stub, **options):
        return podsweep.Sweeper(stub.list_pods, stub.delete_pod, None, clock=self.time.clock, **options)

    def setUp(self):
        super().setUp()
        podsweep.reset()
        self.addCleanup(podsweep.reset)

    def test_no_sweeper_means_nothing_happens(self):
        self.assertIsNone(self.manager.pods)
        self.ops.pod_sweep_pass()
        self.assertEqual((self.lines(), self.warnings()), ([], []))

    def test_a_pass_deletes_an_unowned_pod_and_says_so_in_one_fixed_line(self):
        stub = Stub([listing(1, created=int(NOW) - 10 * pc.LAUNCH_GRACE_SECONDS, deadline=int(NOW) + 3600)])
        self.manager.pods = self.sweeper(stub)
        self.make_run()
        (self.run_dir() / "state.json").write_text(json.dumps({"version": 1, "jobs": {}}))
        self.ops.pod_sweep_pass()
        self.assertEqual(stub.deleted, [(pc.pod_name(SESSION), "11111111-1111-4111-8111-111111111111", None)])
        self.assertEqual(self.lines(), ["pod sweep deleted=1 released=0 failed=0 incomplete=0 unreadable=0"])

    def test_a_pod_a_running_job_records_is_kept_while_its_run_has_a_supervisor(self):
        stub = Stub([listing(1, created=int(NOW) - 10_000, deadline=int(NOW) + 3600)])
        self.manager.pods = self.sweeper(stub)
        directory = self.make_run()
        (directory / "state.json").write_text(json.dumps({"version": 1, "jobs": {"job-1": {"status": "running", "pod": handle(1).record()}}}))
        self.add_child()
        for _ in range(3):
            self.ops.pod_sweep_pass()
            self.time.now += pc.ORPHAN_SILENCE_SECONDS
        self.assertEqual((stub.deleted, self.lines()), ([], []))

    def test_the_same_pod_goes_once_its_run_has_had_no_supervisor_for_the_silence_window(self):
        stub = Stub([listing(1, created=int(NOW) - 10_000, deadline=int(NOW) + 100_000)])
        self.manager.pods = self.sweeper(stub)
        directory = self.make_run()
        (directory / "state.json").write_text(json.dumps({"version": 1, "jobs": {"job-1": {"status": "running", "pod": handle(1).record()}}}))
        self.ops.pod_sweep_pass()
        self.assertEqual(stub.deleted, [])
        self.time.now += pc.ORPHAN_SILENCE_SECONDS
        self.ops.pod_sweep_pass()
        self.assertEqual(len(stub.deleted), 1)
        self.assertEqual(self.lines(), ["pod sweep deleted=1 released=0 failed=0 incomplete=0 unreadable=0"])

    def test_an_unreadable_state_file_stops_the_sweep_from_deleting_what_it_cannot_attribute(self):
        stub = Stub([listing(1, created=int(NOW) - 10_000)])
        self.manager.pods = self.sweeper(stub)
        directory = self.make_run()
        (directory / "state.json").write_text("{nope")
        self.ops.pod_sweep_pass()
        self.assertEqual(stub.deleted, [])
        self.assertEqual(self.lines(), ["pod sweep deleted=0 released=0 failed=0 incomplete=1 unreadable=0"])

    def test_a_pass_that_raises_is_one_fixed_line_from_the_guard_and_the_loop_goes_on(self):
        class Broken:
            def run_once(self, recorded, live):
                raise RuntimeError(CANARY)

            def census(self):
                return {"live": 0, "final": 0, "unknown": False}

        self.manager.pods = Broken()
        self.ops._guard("podsweep", self.ops.pod_sweep_pass)
        self.assertEqual(self.warnings(), ["factory-manager: operations podsweep failed (RuntimeError)"])

    def test_the_thread_exists_only_when_a_sweeper_is_wired_in_and_runs_at_the_contracts_interval(self):
        self.manager.pods = self.sweeper(Stub())
        waits = []

        def wait(seconds):
            waits.append((seconds, threading.current_thread().name))
            return self.manager.closing.wait(seconds)

        operations = ops.Operations(self.manager, wait=wait, policy=lambda cfg: Policy())
        self.manager.operations = operations
        self.addCleanup(self.manager.closing.set)
        operations.start()
        self.manager.terminate_children(grace=0.1)
        self.assertEqual(operations.join(30), 0)
        self.assertIn("factory-manager-podsweep", [t.name for t in operations.threads])
        self.assertIn((pc.ORPHAN_SWEEP_SECONDS, "factory-manager-podsweep"), waits)
        self.assertEqual(self.warnings(), [])


class WorkerPodsEndpointTests(OpsCase):
    def test_the_manager_serves_no_census_without_a_sweeper_and_the_census_with_one(self):
        self.assertIsNone(self.manager.worker_pods())
        stub = Stub([listing(1), listing(2, state="succeeded", final=True)])
        self.manager.pods = podsweep.Sweeper(stub.list_pods, stub.delete_pod, None)
        self.assertEqual(self.manager.worker_pods(), {"live": 1, "final": 1, "unknown": False})

    def test_a_census_that_raises_or_is_not_a_mapping_is_busy_never_idle(self):
        class Raises:
            def census(self):
                raise RuntimeError(CANARY)

        class Odd:
            def census(self):
                return "none"

        for pods in (Raises(), Odd()):
            self.manager.pods = pods
            self.assertEqual(self.manager.worker_pods(), {"live": 0, "final": 0, "unknown": True})


if __name__ == "__main__":
    unittest.main()

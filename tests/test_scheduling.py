"""Deterministic pool admission tests; no agents, GitHub, or live runs."""

import copy
import dataclasses
import tempfile
import threading
import unittest
from concurrent.futures import Future, wait
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from factory.capacity import Owner, SlotPool
from factory.runner import Runner
from tests.test_factory import assignment


class ImmediatePool:
    """Complete tasks immediately; the scheduler still tracks/removes each Future."""

    def __init__(self, max_workers):
        self.max_workers = max_workers

    def submit(self, fn, *args):
        future = Future()
        future.set_result(fn(*args))
        return future

    def shutdown(self, wait):
        pass


class Control:
    def draining(self):
        return False

    def admission(self):
        return nullcontext(True)

    def stop_requests(self):
        return []


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.plan = assignment()
        for key in ("fix-b", "fix-c"):
            job = copy.deepcopy(self.plan["jobs"][0])
            job["id"] = key
            self.plan["jobs"].append(job)
        self.runner = Runner(self.directory, self.directory, self.plan, concurrency=2)
        self.runner.state.update("fix-c", status="merged")

    def queue(self, key, phase, when):
        self.runner.queue_phase(key, phase)
        self.runner.state.update(key, status_started_at=when)

    def run_pool(self, execute=None):
        calls = []

        def finish(job, phase):
            calls.append((job["id"], phase))
            if execute:
                execute(job, phase)
            else:
                self.runner.state.update(job["id"], status="pr_ready")

        with (
            patch("factory.runner.ThreadPoolExecutor", ImmediatePool),
            patch("factory.recovery.validate_resume"),
            patch.object(self.runner, "execute", side_effect=finish),
            patch("factory.runner.wait", wraps=wait) as reap,
        ):
            self.runner.run_pool(Control())
        self.reap_count = reap.call_count
        return calls

    def test_repeated_earlier_plan_repairs_do_not_retake_shared_builder_path(self):
        self.queue("fix-a", "build", "2020-01-01T08:00:00Z")
        self.queue("fix-b", "build", "2020-01-01T09:00:00Z")
        attempts = 0

        def repair(job, phase):
            nonlocal attempts
            if job["id"] == "fix-a":
                attempts += 1
                if attempts < 3:
                    self.runner.queue_phase("fix-a", "build")
                    return
            self.runner.state.update(job["id"], status="pr_ready")

        self.assertEqual(
            self.run_pool(repair),
            [
                ("fix-a", "build"),
                ("fix-b", "build"),
                ("fix-a", "build"),
                ("fix-a", "build"),
            ],
        )
        self.assertEqual(self.reap_count, 4)  # Shared path permits only one at a time.

    def test_every_phase_prefers_oldest_waiter_and_preserves_capacity(self):
        self.runner.jobs["fix-b"]["likely_paths"] = ["unrelated"]
        for phase in ("build", "review", "verify", "publish"):
            with self.subTest(phase=phase):
                self.runner.limits[phase] = 1
                self.queue("fix-a", phase, "2020-01-01T10:00:00Z")
                self.queue("fix-b", phase, "2020-01-01T09:00:00Z")
                self.assertEqual(self.run_pool(), [("fix-b", phase), ("fix-a", phase)])
                self.assertEqual(self.reap_count, 2)

    def test_blocker_updates_and_restart_preserve_waiting_age(self):
        self.queue("fix-a", "build", "2020-01-01T10:00:00Z")
        self.queue("fix-b", "build", "2020-01-01T09:00:00Z")
        self.runner.state.update("fix-b", waiting_for="builder path overlap: fix-a")
        self.runner = Runner(self.directory, self.directory, self.plan)
        self.assertEqual(self.run_pool(), [("fix-b", "build"), ("fix-a", "build")])

    def test_equal_instants_use_plan_order_with_timezone_normalization(self):
        self.queue("fix-a", "build", "2020-01-01T10:00:00+01:00")
        self.queue("fix-b", "build", "2020-01-01T09:00:00Z")
        self.assertEqual(self.run_pool(), [("fix-a", "build"), ("fix-b", "build")])

    def test_legacy_unknown_ages_get_a_turn_without_fabricating_a_clock(self):
        for unknown in (None, "bad clock", 42, "2020-01-01T01:00:00"):
            with self.subTest(unknown=unknown):
                self.queue("fix-a", "build", "2020-01-01T10:00:00Z")
                self.queue("fix-b", "build", unknown)
                self.assertEqual(self.runner.waiting_jobs("build"), ["fix-b", "fix-a"])
                self.assertEqual(
                    self.runner.state.data["jobs"]["fix-b"]["status_started_at"],
                    unknown,
                )
        self.queue("fix-a", "build", None)
        self.assertEqual(self.runner.waiting_jobs("build"), ["fix-a", "fix-b"])

    def test_oldest_dependency_blocked_job_does_not_stop_unrelated_build(self):
        self.queue("fix-a", "build", "2020-01-01T09:00:00Z")
        self.queue("fix-b", "build", "2020-01-01T10:00:00Z")
        self.runner.jobs["fix-a"]["depends_on"] = ["fix-c"]
        self.runner.state.update("fix-c", status="pr_incomplete")
        self.assertEqual(self.run_pool(), [("fix-b", "build")])
        self.assertEqual(
            self.runner.state.data["jobs"]["fix-a"]["waiting_for"],
            "dependency merge: fix-c",
        )

    def test_phase_priority_and_total_agent_limit_remain_enforced(self):
        self.runner.max_active_agents = 1
        self.queue("fix-a", "build", "2020-01-01T08:00:00Z")
        self.queue("fix-b", "review", "2020-01-01T10:00:00Z")
        self.assertEqual(self.run_pool(), [("fix-b", "review"), ("fix-a", "build")])
        self.assertEqual(self.reap_count, 2)

    def test_queued_retry_and_waiting_phase_share_one_order(self):
        self.queue("fix-b", "build", "2020-01-01T09:00:00Z")
        self.runner.state.update("fix-a", status="queued")
        self.runner.state.update("fix-a", status_started_at="2020-01-01T10:00:00Z")
        self.assertEqual(self.run_pool(), [("fix-b", "build"), ("fix-a", "build")])


class PodBudgetTests(unittest.TestCase):
    """Runners of several repositories share one pod worker budget (capacity.SlotPool)."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.now = 1000.0
        self.pool = SlotPool(self.root / "capacity", 1, clock=lambda: self.now)
        self.held = []
        self.addCleanup(lambda: [lease.release() for lease in self.held])

    def foreign(self, repo, job):
        lease, reason = self.pool.try_acquire(Owner(repo, job, "build"))
        self.assertIsNone(reason)
        self.held.append(lease)
        return lease

    def runner(self, keys=("fix-a",), name="run", repo="", pool=None, **options):
        plan = assignment()
        template = plan["jobs"][0]
        plan["jobs"] = []
        for index, key in enumerate(keys):
            job = copy.deepcopy(template)
            job.update(id=key, likely_paths=[f"area-{index}"])
            plan["jobs"].append(job)
        directory = self.root / name
        directory.mkdir()
        runner = Runner(directory, directory, plan, capacity=pool or self.pool, **options)
        if repo:  # only the pod budget sees the repository name; no GitHub labels are involved
            runner.cfg = dataclasses.replace(runner.cfg, repo=repo)
        for index, key in enumerate(keys):
            runner.queue_phase(key, "build")
            runner.state.update(key, status_started_at=f"2020-01-01T0{index}:00:00Z")
        return runner

    def run_pool(self, runner, control=None, **patches):
        calls = []

        def finish(job, phase):
            calls.append((job["id"], phase))
            runner.state.update(job["id"], status="pr_ready")

        with (
            patch("factory.runner.ThreadPoolExecutor", ImmediatePool),
            patch("factory.recovery.validate_resume"),
            patch.object(runner, "execute", side_effect=finish),
        ):
            runner.run_pool(control or Control())
        return calls

    def test_budget_held_by_other_repositories_is_waited_for_not_exited_on(self):
        other = self.foreign("org/other", "x")
        runner = self.runner()
        waits = []

        class Stop(threading.Event):
            def wait(self, timeout=None):
                waits.append(runner.state.data["jobs"]["fix-a"].get("waiting_for"))
                other.release()
                return False

        runner.stop = Stop()
        self.assertEqual(self.run_pool(runner), [("fix-a", "build")])
        self.assertEqual(waits, ["pod worker budget: 1/1 workers in use"])
        job = runner.state.data["jobs"]["fix-a"]
        self.assertEqual((job["waiting_for"], job["waiting_owner"]), (None, None))
        self.assertEqual(self.pool.snapshot()["in_use"], 0)

    def test_the_reason_and_owner_are_what_status_shows_while_waiting(self):
        self.foreign("org/other", "x")
        runner = self.runner()

        class Drain:
            flag = False

            def draining(self):
                return self.flag

            def admission(self):
                return nullcontext(not self.flag)

            def stop_requests(self):
                return []

        control = Drain()

        class Stop(threading.Event):
            def wait(self, timeout=None):
                control.flag = True
                return False

        runner.stop = Stop()
        self.assertEqual(self.run_pool(runner, control), [])
        job = runner.state.data["jobs"]["fix-a"]
        self.assertEqual(job["status"], "waiting_build")  # a drain leaves queued work queued
        self.assertEqual(job["waiting_for"], "pod worker budget: 1/1 workers in use")
        from factory.status import status_report

        row = status_report(runner.plan, runner.state.data)["jobs"][0]
        self.assertEqual(row["waits"], ["pod worker budget: 1/1 workers in use"])

    def test_a_run_never_holds_more_slots_than_the_budget(self):
        pool = SlotPool(self.root / "two", 2, clock=lambda: self.now)
        runner = self.runner(("fix-a", "fix-b", "fix-c"), pool=pool, concurrency=3)
        gate = threading.Event()
        observed = []
        real_wait = wait

        def execute(job, phase):
            gate.wait(10)
            runner.state.update(job["id"], status="pr_ready")

        def spying_wait(active, **kwargs):
            states = runner.state.data["jobs"]
            observed.append((len(active), pool.snapshot()["in_use"], states["fix-c"].get("waiting_for")))
            gate.set()
            return real_wait(active, **kwargs)

        with (
            patch.object(runner, "execute", side_effect=execute),
            patch("factory.recovery.validate_resume"),
            patch("factory.runner.wait", spying_wait),
        ):
            runner.run_pool(Control())
        self.assertEqual(observed[0], (2, 2, "pod worker budget: 2/2 workers in use"))
        self.assertTrue(all(in_use <= 2 for _, in_use, _ in observed))
        self.assertEqual(
            {k: v["status"] for k, v in runner.state.data["jobs"].items()},
            {"fix-a": "pr_ready", "fix-b": "pr_ready", "fix-c": "pr_ready"},
        )
        self.assertEqual(pool.snapshot()["in_use"], 0)

    def test_one_refusal_per_tick_keeps_the_pool_traffic_small(self):
        self.foreign("org/other", "x")
        runner = self.runner(("fix-a", "fix-b", "fix-c"), concurrency=3)
        asked = []
        real = runner.capacity.try_acquire
        runner.capacity.try_acquire = lambda owner: (asked.append(owner.job), real(owner))[1]

        control = type("Drain", (), {"flag": False})()
        control.draining = lambda: control.flag
        control.admission = lambda: nullcontext(not control.flag)
        control.stop_requests = lambda: []

        class StopThenDrain(threading.Event):
            def wait(self, timeout=None):
                control.flag = True
                return False

        runner.stop = StopThenDrain()
        self.run_pool(runner, control)
        self.assertEqual(asked, ["fix-a"])  # one ticket and one probe, not three
        for key in ("fix-a", "fix-b", "fix-c"):
            self.assertEqual(
                runner.state.data["jobs"][key]["waiting_for"], "pod worker budget: 1/1 workers in use"
            )

    def test_two_repositories_compete_for_one_slot_and_take_turns(self):
        first = self.runner(("fix-a",), name="one", repo="org/a")
        second = self.runner(("fix-a",), name="two", repo="org/b")
        seen = []
        calls = []

        def execute(job, phase):
            # While org/a's worker holds the only slot, org/b is refused with the pod reason.
            seen.append(second.admit("fix-a", "build")[1])
            calls.append("org/a")
            first.state.update("fix-a", status="pr_ready")

        with (
            patch("factory.runner.ThreadPoolExecutor", ImmediatePool),
            patch("factory.recovery.validate_resume"),
            patch.object(first, "execute", side_effect=execute),
        ):
            first.run_pool(Control())
        self.assertEqual(seen, ["pod worker budget: 1/1 workers in use"])
        self.assertEqual(self.run_pool(second), [("fix-a", "build")])

    def test_the_slot_is_returned_when_a_worker_fails(self):
        runner = self.runner()

        def boom(job, phase):
            raise RuntimeError("worker crashed")

        with (
            patch("factory.recovery.validate_resume"),
            patch.object(runner, "execute", side_effect=boom),
        ):
            with self.assertRaises(RuntimeError):
                runner.run_pool(Control())
        self.assertEqual(self.pool.snapshot()["in_use"], 0)
        self.assertEqual(self.pool.waiters(), [])

    def test_a_failed_resume_check_returns_the_slot(self):
        runner = self.runner(("fix-a", "fix-b"))
        with (
            patch("factory.runner.ThreadPoolExecutor", ImmediatePool),
            patch("factory.recovery.validate_resume", side_effect=ValueError("stale")),
        ):
            runner.state.update("fix-a", pending_phase="review")
            runner.queue_phase("fix-a", "review")
            runner.run_pool(Control())
        self.assertEqual(runner.state.data["jobs"]["fix-a"]["status"], "failed")
        self.assertEqual(self.pool.snapshot()["in_use"], 0)

    def test_without_a_pool_nothing_changes(self):
        runner = self.runner(("fix-a", "fix-b"))
        runner.capacity = None
        self.assertEqual(self.run_pool(runner), [("fix-a", "build"), ("fix-b", "build")])
        self.assertEqual(self.pool.snapshot()["in_use"], 0)

    def test_publishing_takes_no_pod_slot(self):
        self.foreign("org/other", "x")
        runner = self.runner()
        lease, reason = runner.admit("fix-a", "publish")
        self.assertIsNone(reason)
        self.assertEqual(self.pool.snapshot()["in_use"], 1)
        lease.release()
        self.assertEqual(runner.admit("fix-a", "build")[1], "pod worker budget: 1/1 workers in use")


class SharedPathTests(unittest.TestCase):
    """Overlapping jobs never have concurrent unmerged builds or pull requests."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)

    def make(self, paths, concurrency=2):
        self.directory = Path(tempfile.mkdtemp(dir=self.directory))
        self.plan = assignment()
        template = self.plan["jobs"][0]
        self.plan["jobs"] = []
        for key, likely in paths.items():
            job = copy.deepcopy(template)
            job.update(id=key, likely_paths=likely)
            self.plan["jobs"].append(job)
        self.runner = Runner(
            self.directory, self.directory, self.plan, concurrency=concurrency
        )
        self.builds = []

    def run_pool(self):
        def finish(job, phase):
            self.builds.append(job["id"])
            self.runner.state.update(
                job["id"],
                status="pr_ready",
                branch=f"factory/x/{job['id']}",
                pr=f"https://github.com/o/r/pull/{len(self.builds)}",
            )

        with (
            patch("factory.runner.ThreadPoolExecutor", ImmediatePool),
            patch("factory.recovery.validate_resume"),
            patch.object(self.runner, "execute", side_effect=finish),
        ):
            self.runner.run_pool(Control())

    def jobs(self):
        return self.runner.state.data["jobs"]

    def test_four_jobs_on_the_same_files_build_one_at_a_time_as_siblings_merge(self):
        same = ["src/app.py", "tests/test_app.py"]
        self.make({f"job-{n}": same for n in range(1, 5)})
        self.run_pool()
        self.assertEqual(self.builds, ["job-1"])
        for key in ("job-2", "job-3", "job-4"):
            self.assertEqual(self.jobs()[key]["status"], "queued")
            self.assertEqual(self.jobs()[key]["waiting_for"], "shared paths: job-1")
        for expected in ("job-2", "job-3", "job-4"):
            self.jobs()[self.builds[-1]]["status"] = "merged"
            self.run_pool()
            self.assertEqual(self.builds[-1], expected)
        self.assertEqual(self.builds, ["job-1", "job-2", "job-3", "job-4"])

    def test_later_waiters_name_the_first_unmerged_sibling(self):
        self.make({f"job-{n}": ["src"] for n in range(1, 4)})
        self.run_pool()
        self.jobs()["job-1"]["status"] = "merged"
        self.run_pool()
        self.assertEqual(self.builds, ["job-1", "job-2"])
        self.assertEqual(self.jobs()["job-3"]["waiting_for"], "shared paths: job-2")

    def test_disjoint_jobs_still_build_in_parallel(self):
        self.make({"job-1": ["src/a"], "job-2": ["src/b"], "job-3": ["src/a/x.py"]})
        self.run_pool()
        self.assertEqual(self.builds, ["job-1", "job-2"])
        self.assertEqual(self.jobs()["job-3"]["waiting_for"], "shared paths: job-1")

    def test_repository_wide_and_unknown_paths_serialize_against_everything(self):
        for wide in (["."], []):
            with self.subTest(wide=wide):
                self.make({"job-1": ["src/a"], "job-2": wide})
                self.run_pool()
                self.assertEqual(self.builds, ["job-1"])
                self.assertEqual(
                    self.jobs()["job-2"]["waiting_for"], "shared paths: job-1"
                )
                self.jobs()["job-1"]["status"] = "merged"
                self.run_pool()
                self.assertEqual(self.builds, ["job-1", "job-2"])

    def test_a_wide_job_in_flight_holds_back_the_others(self):
        self.make({"job-1": ["."], "job-2": ["src/a"], "job-3": ["docs"]})
        self.run_pool()
        self.assertEqual(self.builds, ["job-1"])
        self.assertEqual(self.jobs()["job-3"]["waiting_for"], "shared paths: job-1")

    def test_a_sibling_that_is_closed_or_failed_without_a_branch_releases_the_wait(self):
        self.make({"job-1": ["src"], "job-2": ["src"]})
        for released in (
            {"status": "closed", "branch": "b", "pr": "u"},
            {"status": "failed", "error": "boom"},
            {"status": "blocked"},
        ):
            with self.subTest(released=released):
                self.jobs()["job-2"].update(status="queued", waiting_for=None)
                for field in ("branch", "pr", "error"):
                    self.jobs()["job-1"].pop(field, None)
                self.jobs()["job-1"].update(released)
                self.assertEqual(
                    self.runner.schedule_blocker("job-2", "build", {}), None
                )

    def test_a_failed_sibling_with_a_branch_still_holds_the_wait(self):
        self.make({"job-1": ["src"], "job-2": ["src"]})
        self.jobs()["job-1"].update(status="failed", branch="b")
        self.assertEqual(
            self.runner.schedule_blocker("job-2", "build", {}), "shared paths: job-1"
        )

    def test_actual_paths_count_and_a_sibling_depending_on_the_job_cannot_deadlock(self):
        self.make({"job-1": ["src/a"], "job-2": ["docs"], "job-3": ["src/a"]})
        self.jobs()["job-1"].update(status="pr_ready", branch="b")
        self.jobs()["job-2"]["actual_paths"] = ["src/a/deep.py"]
        self.assertEqual(
            self.runner.schedule_blocker("job-2", "build", {}), "shared paths: job-1"
        )
        # job-1 now depends on job-3 and holds a branch: job-3 must not wait for it.
        self.runner.jobs["job-1"]["depends_on"] = ["job-3"]
        self.assertIsNone(self.runner.schedule_blocker("job-3", "build", {}))

    def test_only_the_build_of_a_fresh_job_waits(self):
        self.make({"job-1": ["src"], "job-2": ["src"]})
        self.jobs()["job-1"].update(status="pr_ready", branch="b")
        self.jobs()["job-2"].update(branch="c")  # already has its own branch: repaired in place
        self.assertIsNone(self.runner.schedule_blocker("job-2", "build", {}))
        self.jobs()["job-2"].pop("branch")
        self.assertIsNone(self.runner.schedule_blocker("job-2", "review", {}))

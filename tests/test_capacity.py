"""Pod worker budget: resource reading, derived defaults, fair admission across repositories.

No real cgroups and no sleeping: resources come from fake trees, time from a fake clock,
cross-process behaviour from real child processes that block on a pipe.
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from factory import capacity
from factory.capacity import (
    CapacityTimeout,
    Lease,
    Owner,
    Resources,
    SlotPool,
    admission_order,
    derive_budget,
    derive_memory_limit_mb,
    detect_resources,
    reason_text,
)

GIB = 1024**3
LONG_REPO = f"{'o' * 39}/{'n' * 100}"  # 140 characters: valid for check_repo, longer than the display limit


def write(root, name, text):
    path = Path(root) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class FreshNotices(unittest.TestCase):
    """Each test sees the once-per-process notices again and captures them."""

    def setUp(self):
        patcher = patch.object(capacity, "_NOTICED", set())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.err = io.StringIO()
        redirect = redirect_stderr(self.err)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)


class ResourceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.cgroup = Path(temp.name) / "cgroup"
        self.proc = Path(temp.name) / "proc"
        self.cgroup.mkdir()
        self.proc.mkdir()

    def detect(self, host_cpus=16):
        return detect_resources(self.cgroup, self.proc, host_cpus=host_cpus)

    def test_cgroup_v2_limits_beat_the_host(self):
        write(self.cgroup, "cpu.max", "300000 100000\n")
        write(self.cgroup, "memory.max", f"{8 * GIB}\n")
        write(self.proc, "meminfo", f"MemTotal: {64 * GIB // 1024} kB\n")
        self.assertEqual(self.detect(), Resources(3.0, 8 * GIB, "cgroup2"))

    def test_cgroup_v1_limits_are_read(self):
        write(self.cgroup, "cpu/cpu.cfs_quota_us", "200000\n")
        write(self.cgroup, "cpu/cpu.cfs_period_us", "100000\n")
        write(self.cgroup, "memory/memory.limit_in_bytes", f"{4 * GIB}\n")
        write(self.proc, "meminfo", f"MemTotal: {64 * GIB // 1024} kB\n")
        self.assertEqual(self.detect(), Resources(2.0, 4 * GIB, "cgroup1"))

    def test_unlimited_cgroups_fall_back_to_the_host(self):
        write(self.cgroup, "cpu.max", "max 100000\n")
        write(self.cgroup, "memory.max", "max\n")
        write(self.proc, "meminfo", f"MemTotal: {32 * GIB // 1024} kB\n")
        self.assertEqual(self.detect(host_cpus=4), Resources(4, 32 * GIB, "cgroup2"))

    def test_unlimited_v1_memory_is_capped_by_the_host(self):
        write(self.cgroup, "cpu/cpu.cfs_quota_us", "-1\n")
        write(self.cgroup, "cpu/cpu.cfs_period_us", "100000\n")
        write(self.cgroup, "memory/memory.limit_in_bytes", "9223372036854771712\n")
        write(self.proc, "meminfo", f"MemTotal: {16 * GIB // 1024} kB\n")
        self.assertEqual(self.detect(host_cpus=8), Resources(8, 16 * GIB, "cgroup1"))

    def test_nothing_readable_leaves_memory_unknown(self):
        self.assertEqual(self.detect(host_cpus=2), Resources(2, None, "host"))


class DerivationTests(FreshNotices):
    def test_the_deployed_pod_keeps_two_default_repositories_unqueued(self):
        # 6 vCPU / 24 GiB, measured figures (1.5 GiB and 400 millicores per worker, docs/capacity.md, "Measurements"):
        # each repo's defaults allow max_active_agents = 6 workers, so two busy repositories run unqueued and a third queues.
        budget = derive_budget(6, 24 * GIB)
        self.assertEqual(budget, 14)
        self.assertGreaterEqual(budget, 2 * 6)
        self.assertLess(budget, 3 * 6)
        # Four fair shares of 1.5 GiB each: finite, so a runaway is stopped while the pod is busy.
        self.assertEqual(derive_memory_limit_mb(24 * GIB, budget), 4 * 1536)

    def test_the_memory_ceiling_is_a_few_fair_shares_and_never_more_than_the_pod_can_give(self):
        self.assertEqual(derive_memory_limit_mb(24 * GIB), 21 * 1024)  # no worker count: all but the reserve
        self.assertEqual(derive_memory_limit_mb(24 * GIB, 8), 4 * 2688)  # fewer workers, bigger shares
        self.assertEqual(derive_memory_limit_mb(24 * GIB, 100), 4 * 1536)  # a share is never below the estimate
        self.assertEqual(derive_memory_limit_mb(24 * GIB, 4), 21 * 1024)  # capped by what is left
        self.assertEqual(derive_memory_limit_mb(4 * GIB, 3), 3 * 1024)
        self.assertEqual(derive_memory_limit_mb(1 * GIB, 1), 768)

    def test_budget_is_the_tighter_of_cpu_and_memory_and_never_below_one(self):
        self.assertEqual(derive_budget(64, 8 * GIB), 4)  # memory binds: (8 - 2 reserve) GiB at 1.5 GiB each
        self.assertEqual(derive_budget(2, 64 * GIB), 5)  # cpu binds: 400 millicores each
        self.assertEqual(derive_budget(0.5, 1 * GIB), 1)
        self.assertEqual(derive_budget(1, 512 * 1024**2), 1)
        self.assertIsNone(derive_budget(None, None))

    def test_small_pods_keep_a_quarter_of_memory_and_unknown_memory_has_no_limit(self):
        self.assertEqual(derive_memory_limit_mb(4 * GIB), 3 * 1024)
        self.assertIsNone(derive_memory_limit_mb(None))
        self.assertIsNone(derive_memory_limit_mb(None, 5))

    def test_operator_settings_set_or_turn_off_each_limit(self):
        pod = Resources(6, 24 * GIB, "cgroup2")
        self.assertEqual(capacity.total_budget({}, pod), 14)
        self.assertEqual(capacity.total_budget({"FACTORY_MAX_TOTAL_WORKERS": "7"}, pod), 7)
        for off in ("0", "off", "OFF"):
            self.assertIsNone(capacity.total_budget({"FACTORY_MAX_TOTAL_WORKERS": off}, pod))
            self.assertIsNone(capacity.worker_memory_limit_mb({"FACTORY_WORKER_MEMORY_MB": off}, pod))
        self.assertEqual(capacity.worker_memory_limit_mb({}, pod), 4 * 1536)
        self.assertEqual(capacity.worker_memory_limit_mb({"FACTORY_WORKER_MEMORY_MB": "3072"}, pod), 3072)

    def test_the_default_memory_ceiling_follows_the_worker_budget_in_force(self):
        pod = Resources(6, 24 * GIB, "cgroup2")
        limit = capacity.worker_memory_limit_mb
        self.assertEqual(limit({"FACTORY_MAX_TOTAL_WORKERS": "4"}, pod), 21 * 1024)  # 5.25 GiB shares
        self.assertEqual(limit({"FACTORY_MAX_TOTAL_WORKERS": "100"}, pod), 4 * 1536)
        self.assertEqual(limit({"FACTORY_MAX_TOTAL_WORKERS": "off"}, pod), 4 * 1536)  # estimate still applies
        self.assertEqual(limit({}, Resources(2, 24 * GIB, "cgroup2")), 17203)  # cpu-bound: 5 workers, (24 GiB - 3 GiB reserve) / 5 = 4,300 MiB shares
        self.assertEqual(self.err.getvalue(), "")

    def test_invalid_setting_uses_the_default_and_never_echoes_the_value(self):
        pod = Resources(6, 24 * GIB, "cgroup2")
        env = {"FACTORY_MAX_TOTAL_WORKERS": "lots-and-lots-secret"}
        self.assertEqual(capacity.total_budget(env, pod), 14)
        self.assertEqual(capacity.total_budget(env, pod), 14)
        lines = self.err.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("FACTORY_MAX_TOTAL_WORKERS is not a positive integer", lines[0])
        self.assertNotIn("secret", self.err.getvalue())

    def test_unreadable_pod_runs_unlimited_and_says_so_once(self):
        unknown = Resources(None, None, "host")
        self.assertIsNone(capacity.total_budget({}, unknown))
        self.assertIsNone(capacity.total_budget({}, unknown))
        self.assertIsNone(capacity.worker_memory_limit_mb({}, unknown))
        self.assertEqual(
            self.err.getvalue().splitlines(),
            [
                "factory: capacity: pod worker budget off: the pod's cpu and memory could not be read",
                "factory: capacity: memory guard limit off: the pod's memory could not be read",
            ],
        )

    def test_wait_seconds_default_and_override(self):
        self.assertEqual(capacity.wait_seconds({}), 900)
        self.assertEqual(capacity.wait_seconds({"FACTORY_CAPACITY_WAIT_SECONDS": "30"}), 30)
        self.assertEqual(capacity.wait_seconds({"FACTORY_CAPACITY_WAIT_SECONDS": "x"}), 900)


def waiter(repo, job, since, phase="build"):
    return {"repo": repo, "job": job, "phase": phase, "since": since, "key": f"{repo}|{job}|{phase}"}


class AdmissionOrderTests(unittest.TestCase):
    def test_least_loaded_repository_goes_first_and_picks_interleave(self):
        holders = [{"repo": "org/a"}] * 4
        waiters = [
            waiter("org/a", "a5", 1),
            waiter("org/a", "a6", 2),
            waiter("org/b", "b1", 3),
            waiter("org/b", "b2", 4),
            waiter("org/c", "c1", 5),
        ]
        order = admission_order(holders, waiters, 4)
        self.assertEqual(
            order,
            ["org/b|b1|build", "org/c|c1|build", "org/b|b2|build", "org/a|a5|build"],
        )

    def test_ties_go_to_the_oldest_ticket(self):
        waiters = [waiter("org/b", "b1", 9), waiter("org/a", "a1", 5), waiter("org/c", "c1", 7)]
        self.assertEqual(
            admission_order([], waiters, 2), ["org/a|a1|build", "org/c|c1|build"]
        )

    def test_only_free_slots_many_are_offered(self):
        waiters = [waiter("org/a", "a1", 1), waiter("org/b", "b1", 2)]
        self.assertEqual(admission_order([], waiters, 0), [])
        self.assertEqual(len(admission_order([], waiters, 1)), 1)

    def test_a_heavy_repository_cannot_starve_a_light_one(self):
        holders = [{"repo": "org/heavy"}] * 3
        waiters = [waiter("org/heavy", f"h{i}", i) for i in range(5)] + [waiter("org/light", "l1", 99)]
        self.assertEqual(admission_order(holders, waiters, 1), ["org/light|l1|build"])


class PoolCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name) / "capacity"
        self.now = 1000.0
        self.leases = []
        patcher = patch.object(capacity, "_NOTICED", set())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.release_all)

    def release_all(self):
        for lease in self.leases:
            lease.release()

    def pool(self, total, ttl=15.0):
        return SlotPool(self.directory, total, clock=lambda: self.now, ticket_ttl=ttl)

    def take(self, pool, repo, job, phase="build"):
        lease, reason = pool.try_acquire(Owner(repo, job, phase))
        if lease is not None:
            self.leases.append(lease)
        return lease, reason


class SlotPoolTests(PoolCase):
    def test_surplus_requests_are_refused_with_a_clear_reason(self):
        pool = self.pool(3)
        granted = [self.take(pool, "org/a", f"j{i}")[0] for i in range(3)]
        self.assertTrue(all(granted))
        lease, reason = self.take(pool, "org/b", "late")
        self.assertIsNone(lease)
        self.assertEqual(reason, "pod worker budget: 3/3 workers in use")
        granted[0].release()
        lease, reason = self.take(pool, "org/b", "late")
        self.assertIsNotNone(lease)
        self.assertIsNone(reason)
        self.assertEqual(pool.snapshot()["in_use"], 3)

    def test_release_is_idempotent_and_frees_exactly_one_slot(self):
        pool = self.pool(1)
        lease, _ = self.take(pool, "org/a", "j1")
        lease.release()
        lease.release()
        self.assertEqual(pool.snapshot()["in_use"], 0)
        with pool.try_acquire(Owner("org/a", "j2", "build"))[0] as again:
            self.assertIsInstance(again, Lease)
            self.assertEqual(pool.snapshot()["in_use"], 1)
        self.assertEqual(pool.snapshot()["in_use"], 0)

    def test_threads_never_exceed_the_budget(self):
        pool = self.pool(3)
        counters = {"now": 0, "peak": 0}
        mutex = threading.Lock()

        def worker():
            for index in range(4):
                owner = Owner("org/a", f"j{threading.get_ident()}-{index}", "build")
                while True:
                    lease, _ = pool.try_acquire(owner)
                    if lease is not None:
                        break
                with mutex:
                    counters["now"] += 1
                    counters["peak"] = max(counters["peak"], counters["now"])
                with mutex:
                    counters["now"] -= 1
                lease.release()

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertLessEqual(counters["peak"], 3)
        self.assertEqual(pool.snapshot()["in_use"], 0)

    def test_fair_queue_across_three_repositories(self):
        pool = self.pool(2)
        held = [self.take(pool, "org/a", "a1")[0], self.take(pool, "org/a", "a2")[0]]
        self.assertTrue(all(held))
        for repo, job in (("org/a", "a3"), ("org/b", "b1"), ("org/c", "c1")):
            self.now += 1
            self.assertIsNone(self.take(pool, repo, job)[0])
        held[0].release()
        self.now += 1
        # org/a holds one slot, org/b and org/c none: a3 must wait behind both.
        lease, reason = self.take(pool, "org/a", "a3")
        self.assertIsNone(lease)
        self.assertEqual(reason, "pod worker budget: 1/2 workers in use; 2 waiting ahead")
        lease, _ = self.take(pool, "org/c", "c1")
        self.assertIsNone(lease)  # b1 is older than c1
        self.assertIsNotNone(self.take(pool, "org/b", "b1")[0])
        self.assertEqual(pool.snapshot()["in_use"], 2)
        held[1].release()
        self.assertIsNone(self.take(pool, "org/c", "c1")[0])  # same load, but a3 is older
        self.assertIsNotNone(self.take(pool, "org/a", "a3")[0])

    def test_ticket_age_survives_refresh_and_stale_tickets_stop_counting(self):
        pool = self.pool(1, ttl=15.0)
        blocker, _ = self.take(pool, "org/a", "a1")
        self.assertIsNone(self.take(pool, "org/b", "b1")[0])
        self.now += 10
        self.assertIsNone(self.take(pool, "org/c", "c1")[0])
        self.assertEqual(sorted(w["job"] for w in pool.waiters()), ["b1", "c1"])
        self.now += 10  # b1 was last refreshed 20 s ago: its supervisor is gone
        self.assertIsNone(self.take(pool, "org/c", "c1")[0])
        self.assertEqual([w["job"] for w in pool.waiters()], ["c1"])
        waiting = pool.snapshot()["waiting"]
        self.assertEqual(waiting[0]["since"], 1010.0)  # first seen time is kept
        blocker.release()
        self.assertIsNotNone(self.take(pool, "org/c", "c1")[0])

    def test_lowering_the_total_never_admits_beyond_it(self):
        big = self.pool(3)
        for index in range(3):
            self.take(big, "org/a", f"j{index}")
        small = self.pool(2)
        lease, reason = self.take(small, "org/b", "b1")
        self.assertIsNone(lease)
        self.assertEqual(reason, "pod worker budget: 3/2 workers in use")
        self.leases[0].release()
        lease, _ = self.take(small, "org/b", "b1")
        self.assertIsNone(lease)  # two still held, budget two
        self.leases[1].release()
        self.assertIsNotNone(self.take(small, "org/b", "b1")[0])
        self.assertEqual(small.snapshot()["in_use"], 2)

    def test_a_repository_slug_longer_than_the_display_limit_is_admitted_like_any_other(self):
        pool = self.pool(2)
        self.assertIsNotNone(self.take(pool, "org/a", "a1")[0])
        lease, reason = self.take(pool, LONG_REPO, "job-1")
        self.assertIsNotNone(lease, reason)
        self.assertEqual(pool.snapshot()["in_use"], 2)

    def test_a_long_slug_queues_is_shown_shortened_and_is_admitted_in_turn(self):
        pool = self.pool(1)
        blocker = self.take(pool, "org/a", "a1")[0]
        lease, reason = self.take(pool, LONG_REPO, "job-1")
        self.assertIsNone(lease)
        self.assertEqual(reason, "pod worker budget: 1/1 workers in use")
        shown = pool.snapshot()["waiting"][0]["repo"]
        self.assertLessEqual(len(shown), 100)
        self.assertTrue(shown.startswith(f"{'o' * 39}/{'n' * 24}..."), shown)
        blocker.release()
        self.assertIsNotNone(self.take(pool, LONG_REPO, "job-1")[0])
        self.assertEqual(pool.waiters(), [])
        self.assertEqual(pool.holders()[0]["repo"], shown)  # holders and waiters group alike

    def test_withdrawing_a_long_slug_removes_its_tickets_only(self):
        pool = self.pool(1)
        self.take(pool, "org/a", "a1")
        self.assertIsNone(self.take(pool, LONG_REPO, "job-1")[0])
        self.assertIsNone(self.take(pool, "org/b", "b1")[0])
        pool.withdraw(LONG_REPO)
        self.assertEqual([w["job"] for w in pool.waiters()], ["b1"])

    def test_long_values_stay_distinct_and_unexpected_ones_are_not_shown(self):
        first, second = f"{LONG_REPO}a", f"{LONG_REPO}b"
        self.assertNotEqual(capacity._clean(first), capacity._clean(second))
        self.assertLessEqual(len(capacity._clean(first)), 100)
        self.assertEqual(capacity._clean("org/short"), "org/short")
        for odd in ("x" * 600, "has space", "line\nbreak", "", None, 7):
            self.assertEqual(capacity._clean(odd), "?")

    def test_a_failing_truncate_still_frees_the_slot_and_a_corrupt_ticket_restarts_the_wait(self):
        pool = self.pool(1)
        lease, _ = self.take(pool, "org/a", "a1")
        with patch.object(capacity.os, "ftruncate", side_effect=OSError("boom")):
            lease.release()
        self.assertEqual(pool.snapshot()["in_use"], 0)
        blocker, _ = self.take(pool, "org/a", "a2")
        self.assertIsNone(self.take(pool, "org/b", "b1")[0])
        ticket = next(pool.waiting.iterdir())
        ticket.write_text("{not json")
        self.now += 5
        self.assertIsNone(self.take(pool, "org/b", "b1")[0])
        self.assertEqual(pool.snapshot()["waiting"][0]["since"], 1005.0)
        blocker.release()

    def test_snapshot_lists_holders_and_waiters_without_writing(self):
        pool = self.pool(1)
        self.take(pool, "org/a", "a1", "review")
        self.now += 5
        self.take(pool, "org/b", "b1", "verify")
        before = sorted(p.name for p in self.directory.rglob("*"))
        snapshot = pool.snapshot()
        self.assertEqual(sorted(p.name for p in self.directory.rglob("*")), before)
        self.assertEqual((snapshot["total"], snapshot["in_use"], snapshot["free"]), (1, 1, 0))
        self.assertEqual(
            [(h["repo"], h["job"], h["phase"]) for h in snapshot["holders"]],
            [("org/a", "a1", "review")],
        )
        self.assertEqual(
            [(w["repo"], w["job"], w["phase"]) for w in snapshot["waiting"]],
            [("org/b", "b1", "verify")],
        )

    def test_hostile_metadata_is_not_passed_on(self):
        pool = self.pool(1)
        self.take(pool, "org/a", "a1")
        slot = next(pool.slots.glob("slot-*"))
        # A holder file naming something that is not a plain identifier reads as "?"
        # (written through a second descriptor: the holder's lock is untouched).
        descriptor = os.open(slot, os.O_RDWR)
        try:
            os.ftruncate(descriptor, 0)
            os.pwrite(descriptor, json.dumps({"repo": "x y\nz", "job": 7, "phase": "build"}).encode(), 0)
        finally:
            os.close(descriptor)
        holder = pool.holders()[0]
        self.assertEqual((holder["repo"], holder["job"], holder["phase"]), ("?", "?", "build"))

    def test_an_unusable_state_directory_admits_without_a_limit_and_says_so(self):
        self.directory.mkdir(parents=True)
        (self.directory / "slots").write_text("not a directory")
        pool = self.pool(1)
        err = io.StringIO()
        with redirect_stderr(err):
            first, reason = pool.try_acquire(Owner("org/a", "a1", "build"))
            second, _ = pool.try_acquire(Owner("org/a", "a2", "build"))
        self.assertIsNone(reason)
        self.assertTrue(first is not None and second is not None)
        self.assertEqual(
            err.getvalue().splitlines(),
            [
                "factory: capacity: pod worker budget unavailable: "
                "its state directory is not usable; running without it"
            ],
        )

    def test_reason_text_is_counts_only(self):
        self.assertEqual(reason_text(6, 6), "pod worker budget: 6/6 workers in use")
        self.assertEqual(reason_text(4, 6, 2), "pod worker budget: 4/6 workers in use; 2 waiting ahead")


CHILD = textwrap.dedent(
    """
    import sys
    from factory.capacity import Owner, SlotPool

    directory, total, repo, job = sys.argv[1:5]
    lease, reason = SlotPool(directory, int(total)).try_acquire(Owner(repo, job, "build"))
    print("ADMITTED" if lease is not None else f"DENIED {reason}", flush=True)
    sys.stdin.read()  # hold the slot until the parent closes our stdin
    """
)


class CrossProcessTests(PoolCase):
    def start(self, total, repo, job):
        script = self.directory.parent / "child.py"
        script.write_text(CHILD)
        process = subprocess.Popen(
            [sys.executable, "-I", str(script), str(self.directory), str(total), repo, job],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(process.kill)
        self.addCleanup(process.wait)
        self.addCleanup(process.stdout.close)
        self.addCleanup(process.stdin.close)
        return process, process.stdout.readline().strip()

    def test_supervisors_in_separate_processes_share_one_budget(self):
        first, answer = self.start(2, "org/a", "a1")
        self.assertEqual(answer, "ADMITTED")
        second, answer = self.start(2, "org/b", "b1")
        self.assertEqual(answer, "ADMITTED")
        _, answer = self.start(2, "org/c", "c1")
        self.assertEqual(answer, "DENIED pod worker budget: 2/2 workers in use")
        self.assertEqual(self.pool(2).snapshot()["in_use"], 2)
        first.stdin.close()
        first.wait()
        self.assertEqual(self.start(2, "org/c", "c1")[1], "ADMITTED")

    def test_a_killed_supervisor_leaks_no_slot(self):
        victim, answer = self.start(1, "org/a", "a1")
        self.assertEqual(answer, "ADMITTED")
        self.assertEqual(self.pool(1).snapshot()["in_use"], 1)
        victim.kill()
        victim.wait()
        self.assertEqual(self.pool(1).snapshot()["in_use"], 0)
        self.assertEqual(self.start(1, "org/b", "b1")[1], "ADMITTED")


class FakePool:
    """Answers each try_acquire with the next scripted outcome."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.withdrawn = []

    def try_acquire(self, owner):
        return self.outcomes.pop(0)

    def withdraw(self, repo=None):
        self.withdrawn.append(repo)


class HoldTests(FreshNotices):
    owner = Owner("org/a", "plan-1", "plan")

    def test_no_pool_holds_nothing(self):
        with capacity.hold(None, self.owner, 5) as lease:
            self.assertIsNone(lease)

    def test_waits_for_a_slot_then_releases_it(self):
        lease = MagicMock()
        pool = FakePool(
            [(None, "pod worker budget: 1/1 workers in use")] * 2 + [(lease, None)]
        )
        sleeps = []
        clock = iter(range(100))
        with capacity.hold(pool, self.owner, 50, sleep=sleeps.append, clock=lambda: next(clock)):
            lease.release.assert_not_called()
        lease.release.assert_called_once_with()
        self.assertEqual(sleeps, [1.0, 1.0])
        self.assertEqual(
            self.err.getvalue().splitlines(),
            ["factory: pod worker budget: 1/1 workers in use; waiting for a slot"],
        )

    def test_gives_up_with_a_clear_error_after_the_wait_limit(self):
        pool = FakePool([(None, "pod worker budget: 1/1 workers in use")] * 10)
        clock = iter(range(0, 1000, 20))
        with self.assertRaises(CapacityTimeout) as caught:
            with capacity.hold(pool, self.owner, 30, sleep=lambda s: None, clock=lambda: next(clock)):
                self.fail("must not run without a slot")
        self.assertEqual(
            str(caught.exception),
            "pod worker budget: 1/1 workers in use; no slot became free within 30 s",
        )
        self.assertEqual(pool.withdrawn, ["org/a"])


class FromEnvironmentTests(FreshNotices):
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name)

    def test_no_data_directory_means_no_pool(self):
        self.assertIsNone(capacity.pool_from_env({"FACTORY_MAX_TOTAL_WORKERS": "3"}))

    def test_pool_lives_under_the_data_directory_and_honours_the_total(self):
        pool = capacity.pool_from_env({"FACTORY_DATA": str(self.data), "FACTORY_MAX_TOTAL_WORKERS": "3"})
        self.assertEqual((pool.directory, pool.total), (self.data / ".capacity", 3))
        self.assertTrue((self.data / ".capacity" / "slots").is_dir())

    def test_explicit_directory_wins_and_zero_turns_the_budget_off(self):
        elsewhere = self.data / "elsewhere"
        env = {"FACTORY_DATA": str(self.data), "FACTORY_CAPACITY_DIR": str(elsewhere), "FACTORY_MAX_TOTAL_WORKERS": "2"}
        self.assertEqual(capacity.pool_from_env(env).directory, elsewhere)
        env["FACTORY_MAX_TOTAL_WORKERS"] = "0"
        self.assertIsNone(capacity.pool_from_env(env))

    def test_uncreatable_directory_runs_without_a_budget(self):
        blocker = self.data / "file"
        blocker.write_text("x")
        env = {"FACTORY_CAPACITY_DIR": str(blocker / "inside"), "FACTORY_MAX_TOTAL_WORKERS": "2"}
        self.assertIsNone(capacity.pool_from_env(env))
        self.assertIn("pod worker budget off: its state directory cannot be created", self.err.getvalue())

    def test_snapshot_needs_an_existing_pool_and_reads_it(self):
        env = {"FACTORY_DATA": str(self.data), "FACTORY_MAX_TOTAL_WORKERS": "2"}
        self.assertIsNone(capacity.snapshot_from_env(env))
        pool = capacity.pool_from_env(env)
        lease, _ = pool.try_acquire(Owner("org/a", "a1", "build"))
        self.addCleanup(lease.release)
        snapshot = capacity.snapshot_from_env(env)
        self.assertEqual((snapshot["total"], snapshot["in_use"]), (2, 1))

    def test_factory_run_builds_its_runner_with_the_pod_pool(self):
        from factory import cli

        env = {"FACTORY_DATA": str(self.data), "FACTORY_MAX_TOTAL_WORKERS": "3"}
        args = SimpleNamespace(
            concurrency=2,
            verification_concurrency=1,
            timeout=60,
            review_rounds=1,
            review_concurrency=None,
            max_active_agents=None,
        )
        with patch.dict("os.environ", env), patch.object(cli, "Runner") as runner:
            runner.return_value.state.data = {"jobs": {}}
            runner.return_value.drained = False
            cli.run_command(args, SimpleNamespace(), Path("."), Path("."), {}, None, None)
        pool = runner.call_args.kwargs["capacity"]
        self.assertEqual((pool.total, pool.directory), (3, self.data / ".capacity"))


class StatusCommandTests(PoolCase):
    def test_status_json_reports_the_pod_budget_and_its_holders(self):
        from factory.state import State
        from tests.test_factory import assignment

        run = self.directory.parent / "run1"
        run.mkdir()
        plan = assignment()
        (run / "plan.json").write_text(json.dumps(plan))
        State(run, plan)
        data = self.directory.parent / "data"
        pool = capacity.pool_from_env({"FACTORY_DATA": str(data), "FACTORY_MAX_TOTAL_WORKERS": "3"})
        lease, _ = pool.try_acquire(Owner("org/a", "a1", "review"))
        self.addCleanup(lease.release)
        env = {**os.environ, "FACTORY_DATA": str(data), "FACTORY_MAX_TOTAL_WORKERS": "3"}
        done = subprocess.run(
            [sys.executable, "-m", "factory", "--run-dir", str(run), "status", "--offline", "--json"],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        report = json.loads(done.stdout)
        self.assertEqual((report["capacity"]["total"], report["capacity"]["in_use"]), (3, 1))
        self.assertEqual(report["capacity"]["holders"][0]["repo"], "org/a")
        self.assertEqual(report["footprint"], {})


if __name__ == "__main__":
    unittest.main()

"""The history ledger: bounded append-only storage, what is recorded and what never is, and the hooks.

Everything runs in temp directories on the real ``Ledger`` and a real ``State``; the clock of the ledger is
injected where a test needs months or days to pass, so nothing sleeps.
"""

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from factory import control, history, reason_codes
from factory.config import RepoConfig
from factory.history import Ledger, Limits
from factory.runner import Runner
from factory.state import State

CANARY = "CANARY: 5f0e agent-authored text."
PR = "https://github.com/org/name/pull/7"
JAN = 1_767_225_600.0  # 2026-01-01T00:00:00Z
FEB = JAN + 31 * 86400
DAY = 86400.0


def plan_of(*jobs):
    """A plan the way State and the recorder read it: jobs with ids and issue numbers."""
    return {"jobs": [{"id": job, "issues": [number]} for number, job in enumerate(jobs, 1)]}


class TempCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.data = self.root / "data"
        self.data.mkdir()
        self.history = self.data / "history"

    def segments(self):
        return sorted(p.name for p in self.history.iterdir() if p.name.startswith("events-"))

    def raw(self):
        return b"".join((self.history / name).read_bytes() for name in self.segments())


class LimitsTests(unittest.TestCase):
    def test_defaults_and_overrides(self):
        self.assertEqual(history.limits_from_env({}), Limits(64 * 1024 * 1024, 365))
        env = {"FACTORY_HISTORY_MAX_BYTES": "2097152", "FACTORY_HISTORY_RETENTION_DAYS": "30"}
        self.assertEqual(history.limits_from_env(env), Limits(2097152, 30))

    def test_a_value_out_of_range_or_not_a_number_means_the_default(self):
        for value in ("abc", "0", "-5", "1.5", "nan", "inf", "99999999999999999999"):
            with self.subTest(value):
                env = {"FACTORY_HISTORY_MAX_BYTES": value, "FACTORY_HISTORY_RETENTION_DAYS": value}
                self.assertEqual(history.limits_from_env(env), Limits())

    def test_a_segment_is_an_eighth_of_the_cap(self):
        self.assertEqual(Limits(8 * 1024 * 1024).segment_bytes, 1024 * 1024)
        self.assertEqual(Limits(1024).segment_bytes, history.MIN_SEGMENT_BYTES)


class LedgerTests(TempCase):
    def test_an_entry_is_one_json_line_in_a_private_directory(self):
        ledger = Ledger(self.data)
        self.assertTrue(ledger.append("run_started", "org/name", "r1"))
        self.assertEqual(oct(self.history.stat().st_mode & 0o777), "0o700")
        segment = self.history / self.segments()[0]
        self.assertEqual(oct(segment.stat().st_mode & 0o777), "0o600")
        lines = segment.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        event = json.loads(lines[0])
        self.assertEqual((event["v"], event["kind"], event["repo"], event["run"]), (1, "run_started", "org/name", "r1"))
        self.assertTrue(event["at"].endswith("Z"))

    def test_the_history_lives_outside_the_repository_directories(self):
        Ledger(self.data).append("run_started", "org/name", "r1")
        self.assertEqual(self.history, self.data / "history")
        self.assertFalse((self.data / "repos").exists())

    def test_a_restart_reads_what_an_earlier_instance_wrote(self):
        Ledger(self.data).append("run_started", "org/name", "r1")
        Ledger(self.data).append("run_finished", "org/name", "r1", result="drained")
        events = Ledger(self.data).read().events
        self.assertEqual([event["kind"] for event in events], ["run_started", "run_finished"])

    def test_the_ledger_is_never_created_by_reading(self):
        ledger = Ledger(self.data)
        self.assertEqual(ledger.read().events, [])
        self.assertEqual(ledger.signature(), ())
        self.assertFalse(self.history.exists())

    def test_the_size_is_bounded_and_the_oldest_entries_go_first(self):
        limits = Limits(max_bytes=4096)  # segments of 512 bytes
        ledger = Ledger(self.data, limits)
        for number in range(200):
            self.assertTrue(ledger.append("job", "org/name", "r1", job="a", status="running", attempt=number))
            self.assertLessEqual(sum(size for _, size, _ in ledger.signature()), limits.max_bytes)
        attempts = [event["attempt"] for event in ledger.read().events]
        self.assertEqual(attempts[-1], 199)  # the newest is there
        self.assertNotIn(0, attempts)  # the oldest was dropped
        self.assertEqual(attempts, sorted(attempts))
        self.assertGreater(len(self.segments()), 1)

    def test_a_new_month_starts_a_new_segment(self):
        now = [JAN + 3600]
        ledger = Ledger(self.data, clock=lambda: now[0])
        ledger.append("run_started", "org/name", "r1")
        now[0] = FEB + 3600
        ledger.append("run_finished", "org/name", "r1", result="stopped")
        self.assertEqual(self.segments(), ["events-202601-001.jsonl", "events-202602-001.jsonl"])
        self.assertTrue(ledger.read().events[0]["at"].startswith("2026-01-"))

    def test_a_segment_older_than_the_retention_is_dropped(self):
        now = [JAN + 3600]
        ledger = Ledger(self.data, Limits(retention_days=30), clock=lambda: now[0])
        ledger.append("run_started", "org/name", "old")
        old = self.history / self.segments()[0]
        os.utime(old, (JAN, JAN))
        now[0] = JAN + 40 * DAY
        ledger.append("run_started", "org/name", "new")  # rotates and prunes
        self.assertNotIn(old.name, self.segments())
        self.assertEqual([event["run"] for event in ledger.read().events], ["new"])

    def test_an_explicit_prune_applies_the_retention_without_a_write(self):
        now = [JAN + 3600]
        ledger = Ledger(self.data, Limits(retention_days=30), clock=lambda: now[0])
        ledger.append("run_started", "org/name", "r1")
        os.utime(self.history / self.segments()[0], (JAN, JAN))
        now[0] = JAN + 40 * DAY
        ledger.prune()
        self.assertEqual(self.segments(), [])

    def test_an_explicit_prune_never_empties_the_ledger_because_of_its_size(self):
        ledger = Ledger(self.data, Limits(max_bytes=256))
        for _ in range(3):  # about 330 bytes in one segment: over the cap, and the only segment
            ledger.append("job", "org/name", "r1", job="a", status="running")
        ledger.prune()
        self.assertEqual(len(ledger.read().events), 3)

    def test_a_symlinked_history_directory_is_refused(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        os.symlink(elsewhere, self.history)
        ledger = Ledger(self.data)
        self.assertFalse(ledger.append("run_started", "org/name", "r1"))
        self.assertEqual(ledger.read().events, [])
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_a_symlinked_segment_is_never_written_through(self):
        victim = self.root / "victim.txt"
        victim.write_text("keep\n")
        self.history.mkdir(mode=0o700)
        month = "202601"
        os.symlink(victim, self.history / f"events-{month}-001.jsonl")
        ledger = Ledger(self.data, clock=lambda: JAN + 3600)
        ledger.append("run_started", "org/name", "r1")
        self.assertEqual(victim.read_text(), "keep\n")
        self.assertEqual([event["run"] for event in ledger.read().events], ["r1"])

    def test_a_torn_last_line_and_unknown_entries_are_skipped_not_fatal(self):
        ledger = Ledger(self.data)
        ledger.append("run_started", "org/name", "r1")
        segment = self.history / self.segments()[0]
        with segment.open("ab") as handle:
            handle.write(b'{"v":9,"at":"2026-01-02T00:00:00Z","kind":"run_finished","repo":"org/name","run":"r1","new":"field"}\n')
            handle.write(b'{"v":1,"at":"2026-01-02T00:00:00Z","kind":"from_the_future","repo":"org/name","run":"r1"}\n')
            handle.write(b'{"v":1,"at":"nonsense","kind":"run_started","repo":"org/name","run":"r1"}\n')
            handle.write(b'{"v":1,"at":"2026-01-02T00:00:00Z","kind":"run_started","repo":"../x","run":"r1"}\n')
            handle.write(b"not json\n")
            handle.write(b'{"v":1,"at":"2026-01-02T00:00:00Z","kind":"run_start')  # torn: no newline
        events = ledger.read().events
        self.assertEqual([event["kind"] for event in events], ["run_started", "run_finished"])
        self.assertEqual(events[1]["new"], "field")  # unknown fields are tolerated, not interpreted

    def test_a_read_is_capped_and_says_older_entries_were_left(self):
        ledger = Ledger(self.data)
        for number in range(50):
            ledger.append("job", "org/name", "r1", job="a", status="running", attempt=number)
        snapshot = ledger.read(max_bytes=600)
        self.assertTrue(snapshot.truncated)
        self.assertLessEqual(snapshot.bytes_read, 600)
        attempts = [event["attempt"] for event in snapshot.events]
        self.assertEqual(attempts[-1], 49)
        self.assertGreater(attempts[0], 0)  # the cut fell on a line start: every line is whole
        self.assertFalse(ledger.read().truncated)

    def test_a_line_over_the_cap_is_refused_whole(self):
        ledger = Ledger(self.data)
        self.assertFalse(ledger.append("purged", "org/name", "r1", jobs=[{"id": "x"}] * 5000))
        self.assertEqual(ledger.read().events, [])

    def test_text_with_newlines_cannot_split_a_line(self):
        ledger = Ledger(self.data)
        ledger.append("operator", "org/name", "r1", action="drain", principal="a\nb\x00c d")
        self.assertEqual(len(self.raw().splitlines()), 1)

    def test_concurrent_writers_never_tear_a_line(self):
        ledger = Ledger(self.data)

        def write(worker):
            for number in range(25):
                ledger.append("job", "org/name", f"r{worker}", job="a", status="running", attempt=number)

        threads = [threading.Thread(target=write, args=(worker,)) for worker in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(len(ledger.read().events), 200)
        for line in self.raw().splitlines():
            json.loads(line)

    def test_signature_changes_with_every_write(self):
        ledger = Ledger(self.data)
        ledger.append("run_started", "org/name", "r1")
        before = ledger.signature()
        ledger.append("run_finished", "org/name", "r1", result="stopped")
        self.assertNotEqual(ledger.signature(), before)

    def test_writing_never_raises_even_when_the_directory_cannot_be_made(self):
        blocker = self.root / "file"
        blocker.write_text("x")
        self.assertFalse(Ledger(blocker).append("run_started", "org/name", "r1"))


class RecorderTests(TempCase):
    def setUp(self):
        super().setUp()
        self.cfg = RepoConfig(repo="org/name")
        self.run_dir = self.data / "repos" / "org" / "name" / "runs" / "r1"
        self.run_dir.mkdir(parents=True)
        self.plan = plan_of("fix-a", "fix-b")
        self.env = {"FACTORY_DATA": str(self.data)}

    def attach(self, **kwargs):
        state = State(self.run_dir, self.plan)
        recorder = history.attach(state, kwargs.get("cfg", self.cfg), kwargs.get("directory", self.run_dir), self.plan, kwargs.get("env", self.env))
        return state, recorder

    def events(self):
        return Ledger(self.data).read().events

    def test_each_real_transition_is_one_entry(self):
        state, recorder = self.attach()
        self.assertIsNotNone(recorder)
        state.update("fix-a", attempt=1)
        state.update("fix-a", status="running", worker_phase="build", phase="build")
        state.update("fix-a", status="running", worker_phase="review", phase="review")
        state.update("fix-a", status="pr_ready", pr=PR, worker_phase=None, reason_code="ci_pending")
        events = [event for event in self.events() if event["kind"] == "job"]
        self.assertEqual([e.get("phase") for e in events], [None, "build", "review", "review"])
        self.assertEqual([e["status"] for e in events], ["queued", "running", "running", "pr_ready"])
        self.assertEqual([e["attempt"] for e in events], [1, 1, 1, 1])
        self.assertEqual(events[-1]["pr"], PR)
        self.assertEqual(events[-1]["reason_code"], "ci_pending")
        self.assertEqual(events[0]["issues"], [1])
        self.assertTrue(all(e["job"] == "fix-a" and e["repo"] == "org/name" and e["run"] == "r1" for e in events))

    def test_a_job_a_plan_fragment_added_later_carries_its_issues_too(self):
        state, _ = self.attach()
        self.plan["jobs"].append({"id": "fix-c", "issues": [5, 6]})  # the runner extends the plan it shares with the recorder
        state.add_jobs([self.plan["jobs"][-1]])
        state.update("fix-c", status="running", worker_phase="build", phase="build", attempt=1)
        event = [event for event in self.events() if event["kind"] == "job"][0]
        self.assertEqual((event["job"], event["issues"]), ("fix-c", [5, 6]))

    def test_an_update_that_changes_nothing_that_matters_writes_nothing(self):
        state, _ = self.attach()
        state.update("fix-a", status="running", worker_phase="build", phase="build", attempt=1)
        before = len(self.events())
        state.update("fix-a", waiting_for="a slot", waiting_owner="factory")
        state.update("fix-a", usage=[{"total_cost_usd": 0.5}])
        self.assertEqual(len(self.events()), before)

    def test_the_cost_and_the_active_time_are_numbers_from_the_state(self):
        state, _ = self.attach()
        state.update("fix-a", status="running", worker_phase="build", phase="build", attempt=1)
        state.update("fix-a", usage=[{"total_cost_usd": 0.5}, {"total_cost_usd": 0.25}])
        state.update("fix-a", status="pr_ready", worker_phase=None, pr=PR)
        last = [event for event in self.events() if event["kind"] == "job"][-1]
        self.assertEqual(last["cost_usd"], 0.75)
        self.assertIsInstance(last["active_seconds"], float)

    def test_no_agent_authored_text_reaches_the_ledger(self):
        state, recorder = self.attach()
        state.update(
            "fix-a",
            status="failed",
            error=CANARY,
            retry_reason=CANARY,
            review={"pass": False, "findings": [CANARY]},
            verification={"complete": False, "blocker": CANARY},
            blockers=[CANARY],
            usage=[{"total_cost_usd": 1, "note": CANARY}],
            waiting_for=CANARY,
        )
        recorder.planned({"jobs": [{"id": "fix-a", "issues": [1], "objective": CANARY}]}, {"total_cost_usd": 2, "note": CANARY})
        recorder.purged(self.plan, {"fix-a": {"status": "failed", "error": CANARY, "pr": CANARY}})
        recorder.run_finished(CANARY)
        history.record_operator(Ledger(self.data), "org/name", "r1", CANARY, "someone", job=CANARY, status=200)
        self.assertGreater(len(self.events()), 3)
        self.assertNotIn(b"CANARY", self.raw())

    def test_a_value_outside_the_vocabulary_is_dropped_not_stored(self):
        state, _ = self.attach()
        state.update(
            "fix-a",
            status="Not A Status",
            phase="x" * 100,
            pr="https://evil.example/\n<script>",
            reason_code="made_up_code",
            attempt=1,
        )
        event = [event for event in self.events() if event["kind"] == "job"][0]
        for field in ("status", "phase", "pr", "reason_code"):
            self.assertNotIn(field, event)

    def test_every_reason_code_is_kept_and_nothing_else(self):
        state, _ = self.attach()
        for number, code in enumerate(reason_codes.ORDER, 1):
            state.update("fix-a", status="pr_incomplete", pr=PR, attempt=number, reason_code=code)
        kept = [event.get("reason_code") for event in self.events() if event["kind"] == "job"]
        self.assertEqual(kept, list(reason_codes.ORDER))

    def test_the_hook_runs_before_the_label_observer_and_a_failure_of_either_is_swallowed(self):
        state, _ = self.attach()
        order = []

        def failing_ledger(key, previous, current):
            order.append("ledger")
            raise RuntimeError("boom")

        state.ledger = failing_ledger
        state.observer = lambda key, previous, current: order.append("observer")
        state.update("fix-a", status="running", worker_phase="build", phase="build")
        self.assertEqual(order, ["ledger", "observer"])
        self.assertEqual(state.data["jobs"]["fix-a"]["status"], "running")

    def test_a_run_planned_records_its_issues_jobs_and_the_planner_cost(self):
        _, recorder = self.attach()
        recorder.planned(self.plan, {"total_cost_usd": 0.4})
        event = [event for event in self.events() if event["kind"] == "planned"][0]
        self.assertEqual(event["issues"], [1, 2])
        self.assertEqual(event["jobs"], [{"id": "fix-a", "issues": [1]}, {"id": "fix-b", "issues": [2]}])
        self.assertEqual(event["planner_cost_usd"], 0.4)

    def usage_of(self, phase, fresh=1, write=2, read=3, output=4, split=None):
        counts = {
            "input_tokens": fresh,
            "output_tokens": output,
            "cache_creation_input_tokens": write,
            "cache_read_input_tokens": read,
        }
        if split is not None:
            counts["cache_creation"] = {"ephemeral_5m_input_tokens": split[0], "ephemeral_1h_input_tokens": split[1]}
        return {"phase": phase, "total_cost_usd": 0.5, "usage": counts}

    def test_a_job_entry_carries_the_cumulative_token_counts_of_each_phase(self):
        state, _ = self.attach()
        state.update("fix-a", status="running", worker_phase="build", phase="build", attempt=1)
        first = self.usage_of("build", 10, 90, 900, 40, split=(80, 10))
        state.update("fix-a", status="running", worker_phase="review", phase="review", usage=[first])
        state.update(
            "fix-a",
            status="pr_ready",
            worker_phase=None,
            pr=PR,
            usage=[first, self.usage_of("build", 1, 1, 1, 1), self.usage_of("review", 5, 6, 7, 8)],
        )
        jobs = [event for event in self.events() if event["kind"] == "job"]
        self.assertNotIn("tokens", jobs[0])  # no usage yet: the entry is what it was before this field existed
        self.assertEqual(
            jobs[1]["tokens"],
            {
                "build": {
                    "input_tokens": 10,
                    "output_tokens": 40,
                    "cache_creation_input_tokens": 90,
                    "cache_read_input_tokens": 900,
                    "ephemeral_5m_input_tokens": 80,
                    "ephemeral_1h_input_tokens": 10,
                }
            },
        )
        last = jobs[-1]["tokens"]
        self.assertEqual(last["build"]["cache_read_input_tokens"], 901)
        self.assertEqual(
            last["review"],
            {"input_tokens": 5, "output_tokens": 8, "cache_creation_input_tokens": 6, "cache_read_input_tokens": 7},
        )
        self.assertEqual(jobs[-1]["cost_usd"], 1.5)

    def test_a_job_without_usage_data_writes_exactly_what_it_wrote_before(self):
        state, _ = self.attach()
        state.update("fix-a", status="running", worker_phase="build", phase="build", attempt=1)
        state.update("fix-a", status="pr_ready", pr=PR, usage=[{"phase": "build", "total_cost_usd": 0.5}, {"phase": "build", "usage": {}}])
        for event in (event for event in self.events() if event["kind"] == "job"):
            self.assertNotIn("tokens", event)
        self.assertNotIn(b"tokens", self.raw())

    def test_token_counts_outside_the_vocabulary_are_dropped_and_the_entry_stays_small(self):
        state, _ = self.attach()
        hostile = {
            "phase": "build",
            "usage": {
                "input_tokens": True,
                "output_tokens": -3,
                "cache_read_input_tokens": 2.5,
                "cache_creation_input_tokens": CANARY,
                "secret_tokens": 99,
            },
        }
        odd = {"phase": CANARY, "usage": {"input_tokens": 7}}
        state.update("fix-a", status="running", worker_phase="build", phase="build", attempt=1, usage=[hostile, odd])
        event = [event for event in self.events() if event["kind"] == "job"][0]
        self.assertNotIn("tokens", event)  # every count was invalid or of no phase: nothing is written
        self.assertNotIn(b"secret_tokens", self.raw())
        self.assertNotIn(b"CANARY", self.raw())
        every = [self.usage_of(phase, 10**12, 10**12, 10**12, 10**12, split=(10**12, 10**12)) for phase in ("plan", "build", "review", "verify", "preflight")]
        state.update("fix-a", status="pr_ready", pr=PR, usage=every)
        line = max(self.raw().splitlines(), key=len)
        self.assertLess(len(line), history.MAX_LINE // 4)
        written = [event for event in self.events() if event["kind"] == "job"][-1]
        self.assertEqual(sorted(written["tokens"]), ["build", "plan", "preflight", "review", "verify"])

    def test_the_vocabulary_of_tokens_is_closed(self):
        self.assertEqual(history.tokens_of({"input_tokens": 1, "x": 2}), {"input_tokens": 1})
        self.assertIsNone(history.tokens_of({"input_tokens": True}))
        self.assertIsNone(history.tokens_of("x"))
        self.assertEqual(history.phase_tokens_of({"build": {"output_tokens": 2}, "elsewhere": {"output_tokens": 9}}), {"build": {"output_tokens": 2}})
        self.assertIsNone(history.phase_tokens_of({"elsewhere": {"output_tokens": 9}}))
        self.assertIsNone(history.phase_tokens_of([1]))
        self.assertIsNone(history.tokens_of({"input_tokens": 2**60}))

    def test_a_planned_entry_carries_the_planner_token_counts(self):
        _, recorder = self.attach()
        recorder.planned(self.plan, self.usage_of("plan", 4, 40, 400, 20))
        event = [event for event in self.events() if event["kind"] == "planned"][0]
        self.assertEqual(
            event["planner_tokens"],
            {"input_tokens": 4, "output_tokens": 20, "cache_creation_input_tokens": 40, "cache_read_input_tokens": 400},
        )
        self.assertEqual(event["planner_cost_usd"], 0.5)

    def test_a_planned_entry_without_usage_data_has_no_planner_tokens(self):
        _, recorder = self.attach()
        recorder.planned(self.plan, {"total_cost_usd": 0.4})
        recorder.planned(self.plan, {"total_cost_usd": 0.4, "usage": {"input_tokens": "many"}})
        recorder.planned(self.plan, None)
        planned = [event for event in self.events() if event["kind"] == "planned"]
        self.assertEqual(len(planned), 3)
        for event in planned:
            self.assertNotIn("planner_tokens", event)

    def test_a_purge_records_what_each_job_last_was(self):
        _, recorder = self.attach()
        recorder.purged(self.plan, {"fix-a": {"status": "pr_ready", "pr": PR, "reason_code": "ci_failed"}})
        event = [event for event in self.events() if event["kind"] == "purged"][0]
        self.assertEqual(event["reason"], "manual")
        self.assertEqual(
            event["jobs"],
            [{"id": "fix-a", "status": "pr_ready", "pr": PR, "code": "ci_failed"}, {"id": "fix-b", "status": "not_started"}],
        )

    def test_nothing_is_written_without_a_repository_or_without_the_data_directory_or_outside_it(self):
        for label, kwargs in {
            "local mode": {"cfg": RepoConfig(repo="")},
            "no FACTORY_DATA": {"env": {}},
            "blank FACTORY_DATA": {"env": {"FACTORY_DATA": "  "}},
            "no config": {"cfg": None},
            "another data directory": {"env": {"FACTORY_DATA": str(self.root / "elsewhere")}},
            "a run directory that is not a managed run": {"directory": self.root / "scratch" / "r1"},
        }.items():
            with self.subTest(label):
                state, recorder = self.attach(**kwargs)
                self.assertIsNone(recorder)
                self.assertIsNone(state.ledger)
                state.update("fix-a", status="running", worker_phase="build", phase="build")
        self.assertFalse(self.history.exists())
        self.assertFalse((self.root / "elsewhere").exists())

    def test_the_bounds_come_from_the_environment(self):
        recorder = history.recorder_for(
            self.cfg, self.run_dir, self.plan, {**self.env, "FACTORY_HISTORY_MAX_BYTES": "2097152", "FACTORY_HISTORY_RETENTION_DAYS": "10"}
        )
        self.assertEqual(recorder.ledger.limits, Limits(2097152, 10))


class SupervisorTests(TempCase):
    """``Runner.run`` records when the supervisor started and how it ended; the pool itself is not exercised."""

    def setUp(self):
        super().setUp()
        self.cfg = RepoConfig(repo="org/name")
        self.run_dir = self.data / "repos" / "org" / "name" / "runs" / "r1"
        self.run_dir.mkdir(parents=True)
        self.common = self.root / "git"
        self.common.mkdir()
        patcher = patch.dict(os.environ, {"FACTORY_DATA": str(self.data)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def runner(self):
        runner = Runner(self.root, self.run_dir, plan_of("fix-a"), cfg=self.cfg)
        self.assertIsNotNone(runner.recorder)
        return runner

    def results(self):
        return [(e["kind"], e.get("result")) for e in Ledger(self.data).read().events if e["kind"].startswith("run_")]

    def run_with(self, runner, pool):
        with patch.object(Runner, "git", return_value=str(self.common)), patch.object(Runner, "reconcile"), patch.object(
            Runner, "run_pool", pool
        ):
            runner.run()

    def test_a_run_that_ends_normally_is_stopped(self):
        runner = self.runner()
        self.run_with(runner, lambda self_, ctl: None)
        self.assertEqual(self.results(), [("run_started", None), ("run_finished", "stopped")])
        self.assertFalse(runner.drained)

    def test_a_drained_run_is_drained(self):
        runner = self.runner()

        def pool(self_, ctl):
            control.request_drain(self.run_dir, self.common)
            ctl.draining()

        self.run_with(runner, pool)
        self.assertEqual(self.results(), [("run_started", None), ("run_finished", "drained")])
        self.assertTrue(runner.drained)

    def test_a_run_that_dies_with_an_exception_is_interrupted(self):
        runner = self.runner()

        def pool(self_, ctl):
            raise RuntimeError("worker pool failed")

        with self.assertRaises(RuntimeError):
            self.run_with(runner, pool)
        self.assertEqual(self.results(), [("run_started", None), ("run_finished", "interrupted")])

    def test_a_supervisor_that_never_got_the_run_records_nothing(self):
        runner = self.runner()
        owned = control.RunControl(self.run_dir, self.common)  # another supervisor owns the run
        with patch.object(Runner, "git", return_value=str(self.common)), owned, self.assertRaises(RuntimeError):
            runner.run()
        self.assertEqual(self.results(), [])

    def test_the_ledger_never_stops_the_run_when_it_cannot_be_written(self):
        runner = self.runner()
        self.history.write_text("a file where the directory should be\n")
        self.run_with(runner, lambda self_, ctl: None)  # no exception
        self.assertFalse(runner.drained)
        self.assertEqual(self.history.read_text(), "a file where the directory should be\n")


class WordingTests(unittest.TestCase):
    """The ledger records token counts, so no text may say it never holds tokens: it means credentials and secrets."""

    ROOT = Path(__file__).resolve().parents[1]

    def texts(self):
        return {
            "history.py docstring": history.__doc__,
            "docs/architecture.md": (self.ROOT / "docs" / "architecture.md").read_text(),
            "docs/manager.md": (self.ROOT / "docs" / "manager.md").read_text(),
            "docs/operations.md": (self.ROOT / "docs" / "operations.md").read_text(),
        }

    def test_no_text_says_the_ledger_never_holds_tokens(self):
        for name, text in self.texts().items():
            flat = " ".join(text.split())
            with self.subTest(text=name):
                self.assertNotRegex(flat, r"transcripts? or tokens")
                self.assertNotRegex(flat, r"a prompt or a token")
                self.assertNotRegex(flat, r"or a token: the ledger")

    def test_each_says_credentials_and_secrets_are_never_recorded_and_counts_are_numbers(self):
        for name, text in self.texts().items():
            flat = " ".join(text.split())
            with self.subTest(text=name):
                self.assertRegex(flat, r"(?i)token counts?")
                self.assertRegex(flat, r"(?i)credential")
                self.assertRegex(flat, r"(?i)(numbers?, not (a )?secrets?|not secrets?)")


class IsIntTests(unittest.TestCase):
    def test_a_bool_is_not_an_integer_for_a_count_an_id_or_a_version(self):
        self.assertIs(history.is_int(3), True)
        for value in (True, False, 3.0, "3", None, [3]):
            with self.subTest(value=value):
                self.assertIs(history.is_int(value), False)
        self.assertIsNone(history.count_of(True))
        self.assertEqual(history.issues_of([True, 5, False]), [5])


class NumberOfTests(unittest.TestCase):
    def test_a_finite_non_negative_number_is_a_float_and_nothing_else_is_a_number(self):
        self.assertEqual(history.number_of(3), 3.0)
        self.assertEqual(history.number_of(0.1234567), 0.123457)
        for value in (True, False, -1, -0.5, float("nan"), float("inf"), "3", None, [3], {}):
            with self.subTest(value=value):
                self.assertIsNone(history.number_of(value))

    def test_an_integer_too_large_for_a_float_is_not_a_number_and_never_raises(self):
        for value in (10**400, 2**1100, -(10**400), 10**309):
            with self.subTest(digits=len(str(value))):
                self.assertIsNone(history.number_of(value))
        self.assertEqual(history.number_of(10**300), float(10**300))


class ParseLinesTests(unittest.TestCase):
    def line(self, **fields):
        return json.dumps({"v": 1, "kind": "job", "at": "2026-11-08T10:00:00Z", "repo": "org/name", "run": "run-a", **fields}).encode()

    def test_a_kind_that_is_not_a_string_skips_the_line_and_never_raises(self):
        hostile = [self.line(kind=kind) for kind in (["job"], {}, {"job": 1}, [], 7, None, True, 1.5)]
        good = self.line()
        events = history.parse_lines(b"\n".join([*hostile, good]))
        self.assertEqual([event["kind"] for event in events], ["job"])

    def test_a_cost_beyond_the_range_of_a_float_is_dropped_from_the_job_not_raised(self):
        raw = b"\n".join([self.line(kind="planned", planner_cost_usd=10**400), self.line(job="j1", status="merged", cost_usd=10**400, active_seconds=10**400)])
        events = history.parse_lines(raw)
        self.assertEqual(len(events), 2)
        run = history.fold(events)[("org/name", "run-a")]
        self.assertEqual((run["jobs"]["j1"]["cost_usd"], run["jobs"]["j1"]["active_seconds"]), (0.0, None))
        self.assertEqual(run["planner_cost_usd"], 0.0)


class LookupFreshTests(unittest.TestCase):
    """``lookup_fresh``: whether a status read confirmed one job's pull request word with GitHub."""

    def test_the_answer_is_per_job_when_the_report_names_the_failed_ones(self):
        document = {"live_refresh": {"ok": False, "failed": [{"job": "b", "error": "TimeoutExpired", "reason": "timeout"}]}}
        self.assertIs(history.lookup_fresh(document, "a"), True)
        self.assertIs(history.lookup_fresh(document, "b"), False)

    def test_a_read_where_every_lookup_worked_confirms_every_job(self):
        self.assertIs(history.lookup_fresh({"live_refresh": {"ok": True, "failed": []}}, "a"), True)

    def test_a_read_that_is_not_ok_and_names_no_job_confirms_none(self):
        for failed in ([], None, "b", [None], [{"job": 7}], [{"error": "x"}]):
            with self.subTest(failed=failed):
                self.assertIs(history.lookup_fresh({"live_refresh": {"ok": False, "failed": failed}}, "a"), False)

    def test_a_read_that_says_nothing_about_its_lookups_leaves_it_open(self):
        for document in (None, [], {}, {"live_refresh": None}, {"live_refresh": "ok"}):
            with self.subTest(document=document):
                self.assertIsNone(history.lookup_fresh(document, "a"))

    def test_a_job_without_a_usable_id_is_not_confirmed_by_a_partial_failure(self):
        document = {"live_refresh": {"ok": False, "failed": [{"job": "b"}]}}
        for job_id in (None, ["a"], 7):
            with self.subTest(job_id=job_id):
                self.assertIs(history.lookup_fresh(document, job_id), False)


class FoldTests(unittest.TestCase):
    """The read side: events become runs and jobs with outcomes, costs and durations."""

    def ledger_events(self, *entries, start=JAN):
        """Events for repo org/name, run r1, written through a real ledger on a moving clock."""
        with tempfile.TemporaryDirectory() as temp:
            now = [start]
            ledger = Ledger(Path(temp), clock=lambda: now[0])
            for step, (kind, fields) in enumerate(entries):
                now[0] = start + step * 60
                ledger.append(kind, "org/name", fields.pop("run", "r1"), **fields)
            return ledger.read().events

    def one(self, *entries, **kwargs):
        runs = history.fold(self.ledger_events(*entries, **kwargs))
        return runs[("org/name", "r1")]

    def test_a_merged_run(self):
        run = self.one(
            ("planned", {"issues": [1, 2], "jobs": [{"id": "a", "issues": [1]}, {"id": "b", "issues": [2]}], "planner_cost_usd": 0.5}),
            ("run_started", {}),
            ("job", {"job": "a", "status": "running", "phase": "build", "attempt": 1, "cost_usd": 1.0}),
            ("job", {"job": "a", "status": "merged", "phase": "publish", "attempt": 1, "pr": PR, "cost_usd": 2.0}),
            ("job", {"job": "b", "status": "merged", "attempt": 1, "cost_usd": 3.0}),
            ("run_finished", {"result": "stopped"}),
        )
        self.assertEqual(run["outcome"], "merged")
        self.assertEqual(run["issues"], [1, 2])
        self.assertEqual(run["cost_usd"], 5.5)  # the planner and the latest cost of each job
        self.assertEqual(run["duration_seconds"], 300.0)
        self.assertEqual(run["last_result"], "stopped")
        self.assertEqual(run["jobs"]["a"]["pr"], PR)
        self.assertEqual(run["jobs"]["a"]["attempt"], 1)
        self.assertEqual([step["status"] for step in run["jobs"]["a"]["timeline"]], ["running", "merged"])

    def test_the_fold_keeps_the_latest_token_counts_of_a_job_and_the_planner(self):
        build = {"input_tokens": 1, "output_tokens": 2, "cache_creation_input_tokens": 3, "cache_read_input_tokens": 4}
        more = {"input_tokens": 10, "output_tokens": 20, "cache_creation_input_tokens": 30, "cache_read_input_tokens": 40}
        run = self.one(
            ("planned", {"issues": [1], "jobs": [{"id": "a", "issues": [1]}], "planner_cost_usd": 0.5, "planner_tokens": more}),
            ("job", {"job": "a", "status": "running", "phase": "build", "attempt": 1, "tokens": {"build": build}}),
            ("job", {"job": "a", "status": "running", "phase": "review", "attempt": 1, "tokens": {"build": more, "review": build, "bogus": build}}),
            ("job", {"job": "a", "status": "pr_ready", "attempt": 1, "pr": PR}),  # no tokens on this one: the last counts stay
        )
        self.assertEqual(run["planner_tokens"], more)
        self.assertEqual(run["jobs"]["a"]["tokens"], {"build": more, "review": build})

    def test_a_ledger_written_before_the_token_fields_still_reads_and_folds_with_none(self):
        run = self.one(
            ("planned", {"issues": [1], "jobs": [{"id": "a", "issues": [1]}], "planner_cost_usd": 0.5}),
            ("job", {"job": "a", "status": "merged", "phase": "publish", "attempt": 1, "cost_usd": 2.0, "pr": PR}),
        )
        self.assertEqual(run["planner_tokens"], {})
        self.assertEqual(run["jobs"]["a"]["tokens"], {})
        self.assertEqual(run["cost_usd"], 2.5)

    def test_bad_token_fields_in_a_line_already_on_disk_fold_as_none(self):
        run = self.one(
            ("planned", {"issues": [1], "jobs": [{"id": "a", "issues": [1]}], "planner_tokens": {"input_tokens": "5"}}),
            ("job", {"job": "a", "status": "running", "attempt": 1, "tokens": {"build": {"input_tokens": True}, "x": 1}}),
            ("job", {"job": "a", "status": "running", "attempt": 1, "tokens": "many"}),
        )
        self.assertEqual(run["planner_tokens"], {})
        self.assertEqual(run["jobs"]["a"]["tokens"], {})

    def test_a_job_whose_pull_request_a_person_merged_is_merged_by_what_github_said(self):
        run = self.one(
            ("job", {"job": "a", "status": "pr_ready", "pr": PR}),
            ("pr_observed", {"job": "a", "pr": PR, "handoff_status": "merged", "observed_at": "2026-01-01T00:05:00Z", "fresh": True}),
        )
        self.assertEqual(run["jobs"]["a"]["status"], "pr_ready")
        self.assertEqual(run["jobs"]["a"]["outcome"], "merged")
        self.assertEqual(run["jobs"]["a"]["observed_at"], "2026-01-01T00:05:00Z")
        self.assertEqual(run["outcome"], "merged")

    def test_a_failed_lookup_does_not_replace_what_github_confirmed_nor_its_time(self):
        run = self.one(
            ("job", {"job": "a", "status": "pr_ready", "pr": PR}),
            ("pr_observed", {"job": "a", "pr": PR, "handoff_status": "merged", "observed_at": "2026-01-01T00:10:00Z", "fresh": True}),
            ("pr_observed", {"job": "a", "pr": PR, "handoff_status": "published", "observed_at": "2026-01-01T00:25:00Z", "fresh": False}),
            ("purged", {"reason": "abandoned", "jobs": [{"id": "a", "status": "pr_ready", "handoff": "published"}], "fresh": False}),
        )
        job = run["jobs"]["a"]
        self.assertEqual((job["handoff_status"], job["observed_at"], job["observed_fresh"]), ("merged", "2026-01-01T00:10:00Z", True))
        self.assertEqual((job["outcome"], run["outcome"]), ("merged", "merged"))

    def test_a_status_read_without_lookup_information_confirms_nothing_when_a_purge_is_recorded(self):
        # record_purge fails closed, like the pull request observations: no word about the lookups, no confirmation
        with tempfile.TemporaryDirectory() as temp:
            ledger = Ledger(Path(temp))
            report = {"jobs": [{"id": "a", "status": "pr_ready", "handoff_status": "published", "pr": PR}]}
            history.record_purge(ledger, "org/name", "r1", "abandoned", report)
            purged = [event for event in ledger.read().events if event["kind"] == "purged"][0]
        self.assertIs(purged["jobs"][0]["fresh"], False)
        self.assertIsNone(purged.get("fresh"))

    def test_a_purge_entry_is_settled_by_its_own_freshness_before_the_events(self):
        run = self.one(
            ("pr_observed", {"job": "a", "pr": PR, "handoff_status": "merged", "observed_at": "2026-01-01T00:10:00Z", "fresh": True}),
            (
                "purged",
                {
                    "reason": "abandoned",
                    "jobs": [
                        {"id": "a", "status": "pr_ready", "handoff": "published", "fresh": False},
                        {"id": "b", "status": "pr_ready", "handoff": "merged", "fresh": True},
                        {"id": "c", "status": "pr_ready", "handoff": "published"},
                    ],
                    "fresh": False,
                },
            ),
        )
        jobs = run["jobs"]
        self.assertEqual((jobs["a"]["handoff_status"], jobs["a"]["observed_fresh"]), ("merged", True))
        self.assertEqual((jobs["b"]["handoff_status"], jobs["b"]["observed_fresh"]), ("merged", True))  # not the event's False
        self.assertEqual((jobs["c"]["handoff_status"], jobs["c"]["observed_fresh"]), ("published", False))  # no entry flag: the event's

    def test_a_failed_lookup_is_shown_as_not_confirmed_when_nothing_better_is_known(self):
        run = self.one(
            ("job", {"job": "a", "status": "pr_ready", "pr": PR}),
            ("pr_observed", {"job": "a", "pr": PR, "handoff_status": "published", "observed_at": "2026-01-01T00:25:00Z", "fresh": False}),
        )
        job = run["jobs"]["a"]
        self.assertEqual((job["handoff_status"], job["observed_at"], job["observed_fresh"]), ("published", "2026-01-01T00:25:00Z", False))
        self.assertEqual(history.public_job(run, job)["observed_fresh"], False)

    def test_a_later_confirmed_word_replaces_an_earlier_one_and_a_guess_after_a_guess_is_the_newer_guess(self):
        run = self.one(
            ("pr_observed", {"job": "a", "pr": PR, "handoff_status": "published", "observed_at": "2026-01-01T00:01:00Z", "fresh": False}),
            ("pr_observed", {"job": "a", "pr": PR, "handoff_status": "ci_failed", "observed_at": "2026-01-01T00:02:00Z", "fresh": False}),
            ("pr_observed", {"job": "a", "pr": PR, "handoff_status": "published", "observed_at": "2026-01-01T00:03:00Z", "fresh": True}),
            ("pr_observed", {"job": "a", "pr": PR, "handoff_status": "merged", "observed_at": "2026-01-01T00:04:00Z", "fresh": True}),
        )
        job = run["jobs"]["a"]
        self.assertEqual((job["handoff_status"], job["observed_at"], job["observed_fresh"]), ("merged", "2026-01-01T00:04:00Z", True))

    def test_a_confirmed_purge_word_replaces_an_earlier_confirmed_one_with_the_time_of_the_purge(self):
        run = self.one(
            ("pr_observed", {"job": "a", "pr": PR, "handoff_status": "published", "observed_at": "2026-01-01T00:00:00Z", "fresh": True}),
            ("purged", {"reason": "terminal", "jobs": [{"id": "a", "status": "pr_ready", "handoff": "merged"}], "fresh": True}),
        )
        job = run["jobs"]["a"]
        self.assertEqual((job["handoff_status"], job["observed_at"], job["observed_fresh"]), ("merged", "2026-01-01T00:01:00Z", True))

    def test_a_run_purged_before_its_jobs_finished_was_abandoned(self):
        run = self.one(
            ("job", {"job": "a", "status": "running", "phase": "build"}),
            ("purged", {"reason": "abandoned", "jobs": [{"id": "a", "status": "running"}]}),
        )
        self.assertEqual(run["jobs"]["a"]["outcome"], "abandoned")
        self.assertEqual(run["outcome"], "abandoned")
        self.assertEqual((run["purge_reason"], run["purged_at"] is not None), ("abandoned", True))

    def test_a_run_with_a_job_still_going_has_no_outcome_yet(self):
        run = self.one(("job", {"job": "a", "status": "merged"}), ("job", {"job": "b", "status": "running"}))
        self.assertIsNone(run["outcome"])
        self.assertEqual((run["jobs"]["a"]["outcome"], run["jobs"]["b"]["outcome"]), ("merged", None))

    def test_the_outcome_of_a_run_is_the_least_successful_one_of_its_jobs(self):
        for statuses, wanted in {
            ("merged", "closed"): "closed",
            ("merged", "failed"): "blocked",
            ("merged", "blocked", "closed"): "blocked",
            ("merged", "needs_human_publish"): "blocked",
            ("merged", "merged"): "merged",
        }.items():
            with self.subTest(statuses):
                entries = [("job", {"job": f"j{n}", "status": status}) for n, status in enumerate(statuses)]
                self.assertEqual(self.one(*entries)["outcome"], wanted)

    def test_two_entries_for_one_purge_make_one_purge_and_the_one_that_knows_why_wins(self):
        run = self.one(
            ("job", {"job": "a", "status": "pr_ready", "pr": PR}),
            ("purged", {"reason": "manual", "jobs": [{"id": "a", "status": "pr_ready", "pr": PR}]}),
            ("purged", {"reason": "terminal", "jobs": [{"id": "a", "status": "merged", "handoff": "merged"}]}),
        )
        self.assertEqual(run["purge_reason"], "terminal")
        self.assertEqual(run["purged_at"], "2026-01-01T00:01:00Z")  # the first of the two entries
        self.assertEqual(run["jobs"]["a"]["outcome"], "merged")
        self.assertEqual(run["outcome"], "merged")

    def test_a_run_name_planned_again_after_its_purge_is_a_new_run(self):
        run = self.one(
            ("job", {"job": "a", "status": "merged"}),
            ("purged", {"reason": "terminal", "jobs": [{"id": "a", "status": "merged"}]}),
            ("planned", {"issues": [9], "jobs": [{"id": "z", "issues": [9]}]}),
        )
        self.assertIsNone(run["purged_at"])
        self.assertEqual(list(run["jobs"]), ["z"])
        self.assertEqual(run["issues"], [9])

    def test_the_operator_entries_keep_the_principal_and_are_bounded(self):
        entries = [("operator", {"action": "drain", "principal": f"user-{n}", "http": 200}) for n in range(30)]
        run = self.one(*entries)
        self.assertEqual(len(run["operators"]), history.MAX_OPERATORS)
        self.assertEqual(run["operators"][-1], {"at": run["operators"][-1]["at"], "action": "drain", "principal": "user-29", "http": 200})

    def test_a_timeline_is_bounded(self):
        entries = [("job", {"job": "a", "status": "running", "attempt": n}) for n in range(history.MAX_TIMELINE + 20)]
        run = self.one(*entries)
        self.assertEqual(len(run["jobs"]["a"]["timeline"]), history.MAX_TIMELINE)
        self.assertEqual(run["jobs"]["a"]["timeline"][-1]["attempt"], history.MAX_TIMELINE + 19)

    def test_the_run_list_embeds_a_bounded_number_of_jobs(self):
        entries = [("job", {"job": f"j{n}", "status": "merged"}) for n in range(8)]
        shown = history.public_run(self.one(*entries), 3)
        self.assertEqual((shown["jobs_total"], shown["jobs_truncated"], len(shown["jobs"])), (8, True, 3))

    def test_outcome_table(self):
        cases = {
            ("merged", None, False): "merged",
            ("closed", None, False): "closed",
            ("pr_ready", "merged", False): "merged",
            ("pr_incomplete", "closed", False): "closed",
            ("pr_ready", "published", False): None,
            ("pr_ready", "ci_failed", False): None,
            ("running", None, False): None,
            ("queued", None, False): None,
            ("interrupted", None, False): None,
            ("failed", None, False): "blocked",
            ("blocked", None, False): "blocked",
            ("needs_human_publish", None, False): "blocked",
            ("stopped", None, False): "stopped",
            ("stopped", None, True): "stopped",
            ("running", None, True): "abandoned",
            ("failed", None, True): "abandoned",
            ("pr_ready", "published", True): "abandoned",
            ("pr_ready", "merged", True): "merged",
            ("not_started", None, True): "abandoned",
        }
        for (status, handoff, purged), wanted in cases.items():
            with self.subTest(status=status, handoff=handoff, purged=purged):
                self.assertEqual(history.outcome_of(status, handoff, purged), wanted)

    def test_every_outcome_is_a_known_word(self):
        self.assertEqual(set(history.OUTCOME_OF_STATUS.values()) | {"abandoned"}, set(history.OUTCOMES))

    def test_a_purged_run_without_jobs_was_abandoned(self):
        self.assertEqual(self.one(("purged", {"reason": "manual", "jobs": []}))["outcome"], "abandoned")
        self.assertIsNone(self.one(("run_started", {}))["outcome"])


if __name__ == "__main__":
    unittest.main()

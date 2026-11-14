"""The label watcher tops up a running run (docs/manager.md, "Label-triggered builds": top-up).

A fake supervisor is a registered ``run`` child (nothing is spawned for it) whose run directory
holds a real ``plan.json`` and ``state.json``; the append is a recording fake process. Everything
else is the harness of tests/test_trigger_loop.py: a fake GitHub reader, a clock that never sleeps
and single passes (``trigger_pass``) driven directly.
"""

import json
from unittest.mock import patch

from factory import append, fragments
from factory import operations as ops
from factory.control import RunControl, request_drain
from factory.manager import ApiError
from tests.test_factory import assignment
from tests.test_trigger_loop import LABEL, REPO, TriggerCase

RUN = "auto-20270101T000000Z"


class TopUpCase(TriggerCase):
    """A running supervisor (one run, ``building`` of its jobs holding build workers) and helpers."""

    def make_run(self, numbers=(1,), building=1, phase="build", run=RUN):
        """``plan.json`` with one job per issue; the first ``building`` jobs run in ``phase``, the rest are merged."""
        directory = self.run_dir(run)
        directory.mkdir(parents=True, exist_ok=True)
        plan = assignment()
        plan["issues"] = [{"number": n, "title": "t", "body": "b"} for n in numbers]
        plan["jobs"] = [self.job(n) for n in numbers]
        plan["dispositions"] = [{"issue": n, "status": "job", "job": f"fix-{n}", "reason": "ok"} for n in numbers]
        (directory / "plan.json").write_text(json.dumps(plan))
        states = {}
        for position, number in enumerate(numbers):
            if position < building:
                states[f"fix-{number}"] = {"status": "running", "worker_phase": phase, "phase": phase}
            else:
                states[f"fix-{number}"] = {"status": "merged"}
        (directory / "state.json").write_text(json.dumps({"version": 1, "jobs": states}))
        self.supervisor = self.add_child(REPO, run, "run")
        return directory

    @staticmethod
    def job(number):
        return {
            "id": f"fix-{number}",
            "issues": [number],
            "context_issues": [],
            "objective": f"Fix {number}",
            "likely_paths": [f"src/{number}"],
            "acceptance": ["works"],
            "depends_on": [],
            "feature": False,
        }

    def write_fragment(self, number, run=RUN, name=None):
        """What a finished ``plan --append-to`` leaves: a validated fragment for one issue in the run's inbox."""
        directory = self.run_dir(run)
        plan = json.loads((directory / "plan.json").read_text())
        name = name or f"append-20270101T0000000000{number:02d}-aaaaaa"
        document = append.build_fragment(
            {
                "jobs": [self.job(number)],
                "dispositions": [{"issue": number, "status": "job", "reason": "ok"}],
            },
            [{"number": number, "title": "t", "body": "b"}],
        )
        append.write_fragment(directory, name, append.validated(document, name, plan))

    def appends(self):
        return self.spawned("--append-to")

    def append_line(self, count, run=RUN):
        return f"trigger repo={REPO} issues={count} run={run} appending"

    def skip_reason(self, reason):
        return f"trigger repo={REPO} skipped reason={reason}"


class StartTests(TopUpCase):
    def test_a_second_issue_starts_an_append_without_waiting_for_the_repository_to_go_idle(self):
        self.configure("auto_build_concurrency = 2\n")
        directory = self.make_run()
        self.reader.label(8, LABEL)
        self.ops.trigger_pass()
        self.assertEqual(
            self.appends(),
            [["fake-cli", "--repo", REPO, "--run-dir", str(directory), "plan", "--timeout", "1800", "--issues=8", "--append-to", RUN]],
        )
        self.assertEqual(self.trigger_lines(), [self.append_line(1)])
        self.assertNotIn(REPO, self.probes)  # the supervisor holds the lease: no probe
        self.assertEqual(self.spawner.calls[0][1], directory / "append.log")
        self.assertEqual(self.reader.removed, [])
        self.assertEqual(self.spawned("run"), [])  # no second supervisor, no plan of a new run

    def test_the_append_is_planned_with_the_configured_session_timeout(self):
        self.configure("auto_build_concurrency = 2\nauto_build_timeout_seconds = 3600\n")
        self.make_run()
        self.reader.label(8, LABEL)
        self.ops.trigger_pass()
        [argv] = self.appends()
        self.assertEqual(argv[argv.index("--timeout") + 1], "3600")

    def test_the_append_has_its_own_registry_slot_and_the_supervisor_keeps_its_own(self):
        self.configure("auto_build_concurrency = 2\n")
        self.make_run()
        self.reader.label(8, LABEL)
        self.ops.trigger_pass()
        children = self.manager.children
        self.assertIs(children.get(REPO, RUN), self.supervisor)  # pause and status still reach the supervisor
        self.assertEqual(children.active_for_repo(REPO, "append").kind, "append")
        self.assertIs(children.active_for_repo(REPO), self.supervisor)
        self.assertEqual(len(children.running()), 2)
        self.spawner.processes[0].returncode = 3
        children.reap()
        self.assertEqual(children.last_exit(REPO, RUN, kind="append")[-1]["returncode"], 3)
        self.assertEqual(children.last_exit(REPO, RUN), [])  # the supervisor has not exited
        children.forget(REPO, RUN)
        self.assertEqual(children.last_exit(REPO, RUN, kind="append"), [])

    def test_at_most_the_free_slots_and_max_issues_are_appended_lowest_first(self):
        self.configure("auto_build_concurrency = 4\nauto_build_max_issues = 5\n")
        self.make_run()
        for number in (12, 10, 11, 13):
            self.reader.label(number, LABEL)
        self.ops.trigger_pass()
        self.assertEqual([argv[-3] for argv in self.appends()], ["--issues=10,11,12"])  # 4 - 1 building = 3 free
        self.assertEqual(self.trigger_lines(), [self.append_line(3)])

    def test_max_issues_caps_below_the_free_slots(self):
        self.configure("auto_build_concurrency = 6\nauto_build_max_issues = 2\n")
        self.make_run(building=0)
        for number in (10, 11, 12):
            self.reader.label(number, LABEL)
        self.ops.trigger_pass()
        self.assertEqual([argv[-3] for argv in self.appends()], ["--issues=10,11"])

    def test_only_build_workers_hold_a_slot(self):
        self.configure("auto_build_concurrency = 1\n")
        self.make_run(phase="verify")
        self.reader.label(8, LABEL)
        self.ops.trigger_pass()
        self.assertEqual(len(self.appends()), 1)

    def test_a_running_run_with_nothing_labelled_starts_nothing_quietly(self):
        self.configure("auto_build_concurrency = 2\n")
        self.make_run()
        self.ops.trigger_pass()
        self.assertEqual((self.spawner.calls, self.trigger_lines()), ([], []))

    def test_a_supervisor_whose_run_has_no_state_yet_is_busy_and_nothing_is_read(self):
        self.reader.label(8, LABEL)
        self.add_child(REPO, "not-written-yet", "run")
        self.ops.trigger_pass()
        self.assertEqual((self.reader.calls, self.spawner.calls), ([], []))
        self.assertEqual(self.trigger_lines(), [self.skip_reason("busy")])

    def test_a_running_plan_is_still_busy(self):
        self.reader.label(8, LABEL)
        self.add_child(REPO, "x", "plan")
        self.ops.trigger_pass()
        self.assertEqual((self.reader.calls, self.spawner.calls), ([], []))
        self.assertEqual(self.trigger_lines(), [self.skip_reason("busy")])


class SkipTests(TopUpCase):
    def check(self, reason):
        self.reader.label(8, LABEL)
        self.ops.trigger_pass()
        self.ops.trigger_pass()
        self.assertEqual(self.spawner.calls, [])
        self.assertEqual(self.trigger_lines(), [self.skip_reason(reason)])  # said once
        self.assertEqual(self.reader.removed, [])

    def test_no_free_slot_is_slots_full(self):
        self.configure("auto_build_concurrency = 1\n")
        self.make_run()
        self.check("slots_full")

    def test_the_run_at_the_job_cap_is_run_full(self):
        self.configure("auto_build_concurrency = 3\n")
        self.make_run(numbers=(1, 2), building=1)
        with patch.object(fragments, "MAX_RUN_JOBS", 2):
            self.check("run_full")

    def test_waiting_fragments_count_toward_the_job_cap(self):
        self.configure("auto_build_concurrency = 3\n")
        self.make_run()
        self.write_fragment(2)
        with patch.object(fragments, "MAX_RUN_JOBS", 2):
            self.check("run_full")

    def test_a_run_whose_cost_reached_the_budget_is_budget(self):
        self.configure("auto_build_concurrency = 3\n")
        self.policies[REPO] = ops.Policy(604800, 5.0, "never")
        directory = self.make_run()
        data = json.loads((directory / "state.json").read_text())
        data["jobs"]["fix-1"]["usage"] = [{"total_cost_usd": 5.0}]
        (directory / "state.json").write_text(json.dumps(data))
        self.check("budget")

    def test_a_draining_supervisor_is_draining(self):
        self.configure("auto_build_concurrency = 3\n")
        directory = self.make_run()
        common = directory.parent / "git"
        with RunControl(directory, common):
            request_drain(directory, common)
            self.check("draining")

    def test_a_run_below_its_budget_is_topped_up(self):
        self.configure("auto_build_concurrency = 3\n")
        self.policies[REPO] = ops.Policy(604800, 5.0, "never")
        self.make_run()
        self.reader.label(8, LABEL)
        self.ops.trigger_pass()
        self.assertEqual(len(self.appends()), 1)


class LabelTests(TopUpCase):
    def start(self, number=8):
        self.configure("auto_build_concurrency = 3\n")
        self.make_run()
        self.reader.label(number, LABEL)
        self.ops.trigger_pass()
        return self.appends()[-1]

    def test_the_label_stays_until_a_fragment_exists_and_is_then_removed(self):
        self.start()
        self.assertEqual(self.reader.removed, [])
        self.ops.trigger_pass()  # still appending, no fragment yet
        self.assertEqual((self.reader.removed, len(self.appends())), ([], 1))
        self.write_fragment(8)
        self.ops.trigger_pass()
        self.assertEqual(self.reader.removed, [8])
        self.assertNotIn(LABEL, self.reader.labels_of(8))

    def test_a_finished_append_is_judged_and_a_new_one_may_follow(self):
        argv = self.start()
        self.write_fragment(8)
        self.finish(argv, 0)
        self.ops.trigger_pass()
        self.assertEqual(self.reader.removed, [8])
        self.assertEqual(len(self.trigger_lines()), 1)  # no failure line
        self.reader.label(9, LABEL)
        self.ops.trigger_pass()
        self.assertEqual([argv[-3] for argv in self.appends()], ["--issues=8", "--issues=9"])

    def test_the_issue_is_in_the_run_after_the_supervisor_ingested_the_fragment(self):
        argv = self.start()
        self.write_fragment(8)
        directory = self.run_dir(RUN)
        plan = json.loads((directory / "plan.json").read_text())
        plan["issues"].append({"number": 8, "title": "t", "body": "b"})
        (directory / "plan.json").write_text(json.dumps(plan))
        for path in (directory / "inbox").iterdir():
            path.rename(path.with_name(path.name + ".applied"))
        self.finish(argv, 0)
        self.ops.trigger_pass()
        self.assertEqual(self.reader.removed, [8])
        self.assertEqual(len(self.appends()), 1)


class FailureTests(TopUpCase):
    def start(self):
        self.configure("auto_build_concurrency = 3\n")
        self.make_run()
        self.reader.label(8, LABEL)
        self.ops.trigger_pass()
        return self.appends()[-1]

    def test_a_non_zero_exit_keeps_the_label_and_backs_off(self):
        argv = self.start()
        self.finish(argv, 3)
        self.ops.trigger_pass()
        self.assertEqual(self.trigger_lines()[-1], f"trigger repo={REPO} run={RUN} append failed rc=3 backoff=60s")
        self.assertEqual((self.reader.removed, len(self.appends())), ([], 1))
        self.ops.trigger_pass()
        self.assertEqual(self.trigger_lines()[-1], self.skip_reason("backoff"))
        self.assertEqual(len(self.appends()), 1)
        self.time.now += 61
        self.ops.trigger_pass()
        self.assertEqual(len(self.appends()), 2)  # the label is tried again once the backoff is over

    def test_a_failed_append_is_counted_as_a_plan_failure_other(self):
        counter = self.manager.metrics["factory_plan_failures_total"]
        self.finish(self.start(), 3)
        self.ops.trigger_pass()
        self.assertEqual(counter.value(repo=REPO, plan_failure_code="other"), 1)
        self.assertEqual(counter.series_labels(), [(REPO, "other")])

    def test_an_append_that_exits_zero_without_a_fragment_is_a_failure(self):
        argv = self.start()
        self.finish(argv, 0)
        self.ops.trigger_pass()
        self.assertEqual(
            self.trigger_lines()[-1], f"trigger repo={REPO} run={RUN} append failed rc=0 backoff=60s"
        )
        self.assertEqual(self.reader.removed, [])
        self.ops.trigger_pass()
        self.assertEqual(self.trigger_lines()[-1], self.skip_reason("backoff"))

    def test_the_backoff_doubles_with_each_failure(self):
        self.finish(self.start(), 1)
        self.ops.trigger_pass()
        self.assertTrue(self.trigger_lines()[-1].endswith("append failed rc=1 backoff=60s"))
        self.time.now += 61
        self.ops.trigger_pass()  # the next try
        self.spawner.processes[-1].returncode = 1
        self.ops.trigger_pass()
        self.assertTrue(self.trigger_lines()[-1].endswith("append failed rc=1 backoff=120s"))

    def test_an_append_the_manager_cannot_start_backs_off(self):
        self.configure("auto_build_concurrency = 3\n")
        self.make_run()
        self.reader.label(8, LABEL)
        error = ApiError(500, "could not start the append process")
        with patch.object(self.manager, "start_append", side_effect=error):
            self.ops.trigger_pass()
        self.assertEqual(
            self.trigger_lines(), [f"trigger repo={REPO} run={RUN} append start refused status=500 backoff=60s"]
        )


class HoldTests(TopUpCase):
    """A set whose appends keep failing is held like a plan (plan_holds.py), and a held set is never appended."""

    def fail_append(self):
        self.ops.trigger_pass()  # starts an append (the repository backoff is over)
        self.spawner.processes[-1].returncode = 1
        self.ops.trigger_pass()  # sees it end
        self.time.now += 4000

    def test_a_set_whose_append_failed_twice_is_not_appended_again_until_a_change(self):
        self.configure("auto_build_concurrency = 3\n")
        self.make_run()
        self.reader.label(8, LABEL)
        self.fail_append(), self.fail_append()
        self.assertEqual(len(self.appends()), 2)
        self.reader.calls.clear()
        for _ in range(3):
            self.ops.trigger_pass()
            self.time.now += 4000
        self.assertEqual(len(self.appends()), 2)  # held: nothing is sent into the running run
        self.assertEqual([call for call in self.reader.calls if call[0] in ("actor", "permission")], [])
        self.assertEqual(len([line for line in self.trigger_lines() if " held " in line]), 1)
        self.reader.label(8, LABEL, updated="2")  # an edit lets exactly one more through
        self.ops.trigger_pass()
        self.assertEqual(len(self.appends()), 3)

    def test_a_held_issue_is_skipped_and_an_unheld_one_is_still_appended(self):
        self.configure("auto_build_concurrency = 3\nauto_build_max_issues = 1\n")
        self.make_run()
        self.reader.label(8, LABEL)
        self.fail_append(), self.fail_append()
        self.reader.label(9, LABEL)
        self.ops.trigger_pass()
        self.assertEqual([argv[-3] for argv in self.appends()], ["--issues=8", "--issues=8", "--issues=9"])

    def test_a_good_append_clears_the_entry(self):
        self.configure("auto_build_concurrency = 3\n")
        self.make_run()
        self.reader.label(8, LABEL)
        self.fail_append()
        self.ops.trigger_pass()
        self.write_fragment(8)
        self.ops.trigger_pass()
        self.spawner.processes[-1].returncode = 0
        self.ops.trigger_pass()
        hold_file = self.data_dir / "repos" / REPO / "plan-holds.json"
        self.assertEqual(json.loads(hold_file.read_text())["sets"], {})


class OnePerPassTests(TopUpCase):
    def test_a_pass_never_starts_two_appends_for_one_repository(self):
        self.configure("auto_build_concurrency = 5\nauto_build_max_issues = 1\n")
        self.make_run()
        for number in (8, 9, 10):
            self.reader.label(number, LABEL)
        self.ops.trigger_pass()
        self.assertEqual([argv[-3] for argv in self.appends()], ["--issues=8"])
        self.ops.trigger_pass()  # the append is still running
        self.assertEqual(len(self.appends()), 1)
        self.assertEqual(self.trigger_lines()[-1], self.skip_reason("busy"))

    def test_another_operations_object_cannot_start_a_second_append_either(self):
        self.configure("auto_build_concurrency = 5\n")
        self.make_run()
        self.reader.label(8, LABEL)
        self.ops.trigger_pass()
        other = self.new_ops(self.manager, github=lambda cfg: self.readers[cfg.repo])
        other.trigger_pass()
        self.assertEqual(len(self.appends()), 1)

    def test_the_manager_refuses_a_second_append_a_missing_supervisor_and_an_operator_start(self):
        self.configure("auto_build_concurrency = 5\n")
        directory = self.make_run()
        target = self.manager.target_for(REPO, RUN)
        self.manager.start_append(target, [8])
        with self.assertRaises(ApiError) as caught:
            self.manager.start_append(target, [9])
        self.assertEqual(caught.exception.status, 409)
        for start in (self.manager.start_plan, self.manager.start_run):  # as the HTTP actions are
            with self.assertRaises(ApiError) as caught:
                start(self.manager.target_for(REPO, "other" if start == self.manager.start_plan else RUN), {})
            self.assertEqual(caught.exception.status, 409)
        self.assertNotIn(REPO, self.probes)
        self.supervisor.process.returncode = 0
        self.spawner.processes[0].returncode = 0
        with self.assertRaises(ApiError) as caught:  # no supervisor any more: the lease is nobody's to assume
            self.manager.start_append(target, [9])
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(len(self.spawner.calls), 1)
        self.assertTrue((directory / "append.log").parent.is_dir())

    def test_a_paused_trigger_starts_no_append(self):
        self.configure("auto_build_concurrency = 5\n")
        target = self.manager.target_for(REPO, RUN)
        self.make_run()
        self.manager.set_trigger_pause(True)
        with self.assertRaises(ApiError) as caught:
            self.manager.start_append(target, [8])
        self.assertEqual(caught.exception.status, 503)


class RestartTests(TopUpCase):
    def test_a_fragment_already_in_the_inbox_only_has_its_label_removed(self):
        self.configure("auto_build_concurrency = 3\n")
        self.make_run()
        self.write_fragment(8)  # the append of a previous process; its label removal never happened
        self.reader.label(8, LABEL)
        fresh = self.new_ops(self.manager, github=lambda cfg: self.readers[cfg.repo])
        fresh.trigger_pass()
        self.assertEqual(self.reader.removed, [8])
        self.assertEqual(self.spawner.calls, [])

    def test_the_label_is_removed_even_when_the_run_is_full_or_has_no_free_slot(self):
        self.configure("auto_build_concurrency = 1\n")
        self.make_run()
        self.write_fragment(8)
        self.reader.label(8, LABEL)
        self.ops.trigger_pass()
        self.assertEqual((self.reader.removed, self.spawner.calls), ([8], []))
        self.assertEqual(self.trigger_lines(), [self.skip_reason("slots_full")])

    def test_an_issue_in_plan_json_is_never_planned_again(self):
        self.configure("auto_build_concurrency = 3\n")
        self.make_run(numbers=(1, 2))
        self.reader.label(2, LABEL)
        self.ops.trigger_pass()
        self.assertEqual((self.reader.removed, self.spawner.calls), ([2], []))

    def test_only_issues_outside_the_run_and_its_waiting_fragments_are_appended(self):
        self.configure("auto_build_concurrency = 6\n")
        self.make_run(numbers=(1, 2))
        self.write_fragment(3)
        for number in (2, 3, 4):
            self.reader.label(number, LABEL)
        self.ops.trigger_pass()
        self.assertEqual([argv[-3] for argv in self.appends()], ["--issues=4"])
        self.assertEqual(sorted(self.reader.removed), [2, 3])
        self.assertIn(LABEL, self.reader.labels_of(4))

    def test_nothing_in_the_decision_is_remembered_between_passes(self):
        self.configure("auto_build_concurrency = 3\n")
        self.make_run()
        self.reader.label(8, LABEL)
        self.ops.trigger_pass()
        self.spawner.processes[0].returncode = 1  # a process that died with the manager
        self.write_fragment(8)
        fresh = self.new_ops(self.manager, github=lambda cfg: self.readers[cfg.repo])
        fresh.trigger_pass()
        self.assertEqual(self.reader.removed, [8])
        self.assertEqual(len(self.appends()), 1)


class SpendCapTests(TopUpCase):
    def test_an_append_is_not_started_while_the_planner_spend_of_the_last_hour_is_at_the_cap(self):
        import os

        self.configure("auto_build_concurrency = 2\n")
        directory = self.make_run()
        (directory / "planner-usage.json").write_text(json.dumps({"total_cost_usd": 2.0}))
        os.utime(directory / "planner-usage.json", (self.time.now - 60, self.time.now - 60))
        self.reader.label(8, LABEL)
        self.ops.trigger_pass()
        self.ops.trigger_pass()
        self.assertEqual(self.appends(), [])
        self.assertEqual(self.trigger_lines(), [self.skip_reason(ops.PLANNER_SPEND_REASON)])
        self.time.now += 3600  # the record leaves the window
        self.ops.trigger_pass()
        self.assertEqual(len(self.appends()), 1)

import ast
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from factory import status as status_module
from factory.blockers import record_blocker, record_capabilities
from factory.state import State
from factory.status import (
    cache_hit_rate,
    format_status,
    planner_record,
    status_report,
    token_summary,
    usage_tokens,
)


def plan():
    return {
        "jobs": [
            {"id": "a", "likely_paths": ["src/a"], "depends_on": []},
            {"id": "b", "likely_paths": ["src"], "depends_on": ["a"]},
        ]
    }


class TimeoutStatusTests(unittest.TestCase):
    def row(self, error):
        data = {"version": 1, "jobs": {"a": {"status": "failed", "attempt": 1, "error": error}, "b": {"status": "queued", "attempt": 0}}}
        return status_report(plan(), data)["jobs"][0]

    def test_the_fixed_timeout_reason_is_reported_as_a_timeout(self):
        row = self.row("timed out after 1800 s")
        self.assertEqual((row["failure"], row["timeout_seconds"]), ("timeout", 1800))
        self.assertIn("auto_build_timeout_seconds", row["next_action"])
        self.assertIn("split the issue", row["next_action"])
        self.assertEqual(row["owner"], "operator")
        json.dumps(row)  # status --json

    def test_any_other_failure_is_not(self):
        for error in ("boom", "Command 'x' timed out after 1800 seconds", "baseline: timed out after 5 s", None):
            with self.subTest(error=error):
                row = self.row(error)
                self.assertEqual((row["failure"], row["timeout_seconds"]), (None, None))
                self.assertNotIn("auto_build_timeout_seconds", row["next_action"])


class HandoffStatusTests(unittest.TestCase):
    def snapshot(self, state="OPEN", conclusion="SUCCESS"):
        return {
            "state": state,
            "statusCheckRollup": [
                {"__typename": "CheckRun", "name": "tests", "status": "COMPLETED", "conclusion": conclusion}
            ],
        }

    def data(self, status="pr_ready"):
        return {"version": 1, "jobs": {"a": {"status": status, "attempt": 1, "pr": "https://example.invalid/pr/1"}, "b": {"status": "queued", "attempt": 0}}}

    def test_handoff_states_come_from_the_pr_check_rollup(self):
        cases = [
            (self.snapshot(), "published", "pr_ready"),
            (self.snapshot(conclusion="FAILURE"), "ci_failed", "pr_ready"),
            (self.snapshot(state="MERGED"), "merged", "merged"),
            (self.snapshot(state="CLOSED"), "closed", "closed"),
        ]
        for snapshot, handoff, status in cases:
            with self.subTest(handoff=handoff):
                row = status_report(plan(), self.data(), pr_snapshots={"a": snapshot})["jobs"][0]
                self.assertEqual((row["handoff_status"], row["status"]), (handoff, status))

    def reviewed(self):
        data = self.data()
        data["jobs"]["a"].update(review={"pass": True}, verification={"complete": True})
        return data

    def ran(self, commit):
        """The verification record of a verify phase that ran: it always carries assertions."""
        return {"complete": True, "commit": commit, "assertions": ["the scenario passed"]}

    def required_snapshot(self, required, rollup_conclusion="FAILURE"):
        snapshot = self.snapshot(conclusion=rollup_conclusion)
        snapshot["statusCheckRollup"].append(
            {"__typename": "CheckRun", "name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}
        )
        snapshot["requiredChecks"] = required
        return snapshot

    def test_only_required_checks_decide_ci_failed(self):
        def row(snapshot):
            return status_report(plan(), self.reviewed(), pr_snapshots={"a": snapshot})["jobs"][0]

        # "tests" fails but only "ci" is required: published, the failure is informational
        published = row(self.required_snapshot([{"name": "ci", "bucket": "pass"}]))
        self.assertEqual(published["handoff_status"], "published")
        self.assertEqual(published["ci_basis"], "required")
        self.assertEqual(published["other_failing_checks"], ["tests"])
        text = format_status(status_report(plan(), self.data(), pr_snapshots={"a": self.required_snapshot([{"name": "ci", "bucket": "pass"}])}))
        self.assertIn("Other failing checks (not required, informational): tests", text)
        self.assertNotIn("fell back", text)
        failed = row(self.required_snapshot([{"name": "tests", "bucket": "fail"}]))
        self.assertEqual(failed["handoff_status"], "ci_failed")
        self.assertIn("retry a --reason", failed["next_action"])

    def criteria_plan(self):
        spec = plan()
        spec["jobs"][0]["acceptance"] = [
            "plain",
            {"text": "scan", "tag": "ci", "checks": ["ci"]},
            {"text": "ghost", "tag": "ci", "checks": ["missing"]},
            {"text": "flow", "tag": "ci", "workflow": "release"},
            {"text": "prod\x1b[31m", "tag": "operator"},
        ]
        return spec

    def test_criteria_show_tags_and_ci_without_a_matching_required_check_is_unproven(self):
        snapshot = self.required_snapshot([{"name": "ci", "bucket": "pass"}])
        report = status_report(self.criteria_plan(), self.reviewed(), pr_snapshots={"a": snapshot})
        row = report["jobs"][0]
        self.assertEqual(
            [(c["tag"], c["proof"]) for c in row["criteria"]],
            [
                ("sandbox", "pending"),  # no commit recorded: acceptance does not match the head
                ("ci", "satisfied"),
                ("ci", "unproven"),
                ("ci", "unproven"),
                ("operator", "unproven"),
            ],
        )
        self.assertEqual(row["criteria"][1]["checks"], ["ci"])
        self.assertEqual(row["criteria"][3]["workflow"], "release")
        self.assertEqual(json.loads(json.dumps(report))["jobs"][0]["criteria"][2]["proof"], "unproven")
        text = format_status(report)
        self.assertIn("Criterion [ci] satisfied: scan (checks: ci)", text)
        self.assertIn("Criterion [ci] unproven: ghost (checks: missing)", text)
        self.assertIn("Criterion [ci] unproven: flow (workflow: release)", text)
        self.assertIn("Criterion [sandbox] pending: plain", text)
        self.assertNotIn("\x1b", text)

    def test_operator_criteria_are_counted_so_a_reviewer_sees_that_a_person_has_items(self):
        row = status_report(self.criteria_plan(), self.reviewed())["jobs"][0]
        self.assertEqual(row["needs_a_person"], 1)
        text = format_status(status_report(self.criteria_plan(), self.reviewed()))
        self.assertIn("Needs a person: 1 operator criterion in the PR checklist", text)
        spec = self.criteria_plan()
        spec["jobs"][0]["acceptance"].append({"text": "paid step", "tag": "operator", "reason": "costs money"})
        report = status_report(spec, self.reviewed())
        self.assertEqual(report["jobs"][0]["needs_a_person"], 2)
        self.assertEqual(json.loads(json.dumps(report))["jobs"][0]["needs_a_person"], 2)
        self.assertIn("Needs a person: 2 operator criteria in the PR checklist", format_status(report))
        # counting never changes what holds the job back
        self.assertEqual(
            status_report(spec, self.reviewed())["jobs"][0]["next_action"],
            status_report(self.criteria_plan(), self.reviewed())["jobs"][0]["next_action"],
        )
        plain = status_report(plan(), self.reviewed())
        self.assertEqual(plain["jobs"][0]["needs_a_person"], 0)
        self.assertNotIn("Needs a person", format_status(plain))

    def test_ci_criterion_is_failed_for_a_failing_required_check_and_unproven_without_a_snapshot(self):
        failing = self.required_snapshot([{"name": "ci", "bucket": "fail"}])
        row = status_report(self.criteria_plan(), self.reviewed(), pr_snapshots={"a": failing})["jobs"][0]
        self.assertEqual(row["criteria"][1]["proof"], "failed")
        running = self.required_snapshot([{"name": "ci", "bucket": "pending"}])
        row = status_report(self.criteria_plan(), self.reviewed(), pr_snapshots={"a": running})["jobs"][0]
        self.assertEqual(row["criteria"][1]["proof"], "pending")
        for snapshots in ({}, {"a": self.required_snapshot([])}, {"a": {"state": "OPEN", "requiredChecks": None}}):
            row = status_report(self.criteria_plan(), self.reviewed(), pr_snapshots=snapshots)["jobs"][0]
            self.assertEqual(row["criteria"][1]["proof"], "unproven")

    def test_a_ci_criterion_naming_the_factory_gate_is_never_satisfied(self):
        spec = plan()
        spec["jobs"][0]["acceptance"] = [{"text": "circular", "tag": "ci", "checks": ["factory/gate"]}]
        snapshot = self.required_snapshot([{"name": "factory/gate", "bucket": "pass"}])
        row = status_report(spec, self.reviewed(), pr_snapshots={"a": snapshot})["jobs"][0]
        self.assertEqual(row["criteria"][0]["proof"], "unproven")

    def test_a_draft_held_only_by_outstanding_criteria_does_not_ask_for_a_phase_to_resume(self):
        data = self.data("pr_incomplete")
        data["jobs"]["a"].update(
            commit="abc", reviewed_commit="abc", review={"pass": True}, verification={"complete": True, "commit": "abc"}
        )
        row = status_report(plan(), data)["jobs"][0]
        self.assertIn("criteria proven elsewhere", row["next_action"])
        self.assertNotIn("resume the incomplete phase", row["next_action"])
        # only a ci criterion can hold a draft back; an operator criterion never does
        self.assertIn("elsewhere (ci) are outstanding", row["next_action"])
        self.assertNotIn("operator", row["next_action"])
        stale = self.data("pr_incomplete")
        stale["jobs"]["a"].update(commit="abc", reviewed_commit="old", review={"pass": True}, verification={"complete": True, "commit": "abc"})
        self.assertIn("Inspect retained evidence", status_report(plan(), stale)["jobs"][0]["next_action"])

    def test_sandbox_criterion_is_satisfied_by_acceptance_on_the_exact_head(self):
        data = self.reviewed()
        data["jobs"]["a"].update(commit="abc", verification=self.ran("abc"))
        row = status_report(self.criteria_plan(), data)["jobs"][0]
        self.assertEqual(row["criteria"][0]["proof"], "satisfied")

    # ---- a skipped verification proves nothing
    def sandbox_proof(self, verification, commit="abc"):
        data = self.reviewed()
        data["jobs"]["a"].update(commit=commit, verification=verification)
        report = status_report(self.criteria_plan(), data)
        self.assertEqual(json.loads(json.dumps(report)), report)
        return report

    def test_sandbox_criterion_is_unproven_when_verification_was_skipped(self):
        skipped = {"complete": True, "commit": "abc", "skipped": True, "scenario": "not required (verify.when)."}
        report = self.sandbox_proof(skipped)
        row = report["jobs"][0]
        self.assertEqual(row["criteria"][0]["proof"], "unproven")
        self.assertIs(row["verification_skipped"], True)
        text = format_status(report)
        self.assertIn("Criterion [sandbox] unproven: plain", text)
        self.assertNotIn("Criterion [sandbox] satisfied", text)
        self.assertIn("Verification: not run for this job (verify.when)", text)
        # the marker decides even when the record carries assertions
        marked = self.sandbox_proof(dict(skipped, assertions=["something"]))
        self.assertEqual(marked["jobs"][0]["criteria"][0]["proof"], "unproven")

    def test_a_complete_record_without_assertions_is_treated_as_skipped(self):
        # older retained state and imported evidence for a job that needed no verification
        for legacy in (
            {"complete": True, "commit": "abc"},
            {"complete": True, "commit": "abc", "assertions": []},
            {"complete": True, "commit": "abc", "assertions": [" ", 3]},
        ):
            with self.subTest(record=legacy):
                row = self.sandbox_proof(legacy)["jobs"][0]
                self.assertEqual(row["criteria"][0]["proof"], "unproven")
                self.assertIs(row["verification_skipped"], True)

    def test_verification_that_ran_keeps_its_sandbox_criteria_satisfied(self):
        report = self.sandbox_proof(self.ran("abc"))
        row = report["jobs"][0]
        self.assertEqual(row["criteria"][0]["proof"], "satisfied")
        self.assertIs(row["verification_skipped"], False)
        self.assertNotIn("Verification: not run", format_status(report))
        # an incomplete or stale record is not a skipped one: it stays pending
        for record, commit in (({"complete": False, "commit": "abc", "blocker": "x"}, "abc"), (self.ran("old"), "abc")):
            row = self.sandbox_proof(record, commit)["jobs"][0]
            self.assertEqual(row["criteria"][0]["proof"], "pending")
            self.assertIs(row["verification_skipped"], False)

    def test_the_not_run_line_is_said_only_where_it_explains_an_unproven_sandbox_row(self):
        skipped = {"complete": True, "commit": "abc", "skipped": True}
        # a stale skipped record leaves the sandbox row pending: nothing is unproven because of it yet
        stale = self.sandbox_proof(dict(skipped, commit="old"))
        self.assertEqual(stale["jobs"][0]["criteria"][0]["proof"], "pending")
        self.assertNotIn("Verification: not run", format_status(stale))
        # a job with no sandbox criterion has nothing the skipped verification could have proven
        spec = plan()
        spec["jobs"][0]["acceptance"] = [{"text": "scan", "tag": "ci", "checks": ["ci"]}, {"text": "prod", "tag": "operator"}]
        data = self.reviewed()
        data["jobs"]["a"].update(commit="abc", verification=skipped)
        report = status_report(spec, data)
        self.assertIs(report["jobs"][0]["verification_skipped"], True)  # the field stays a fact about the record
        self.assertNotIn("Verification: not run", format_status(report))

    # ---- a skipped required check counts as passing
    def ci_proof(self, required):
        snapshot = self.required_snapshot(required)
        spec = plan()
        spec["jobs"][0]["acceptance"] = [{"text": "both", "tag": "ci", "checks": ["ci", "scan"]}]
        report = status_report(spec, self.reviewed(), pr_snapshots={"a": snapshot})
        self.assertEqual(json.loads(json.dumps(report)), report)
        return report["jobs"][0]["criteria"][0]["proof"]

    def test_a_skipped_required_check_counts_as_passing_like_github_and_the_gate(self):
        skipping = {"name": "scan", "bucket": "skipping"}
        self.assertEqual(self.ci_proof([{"name": "ci", "bucket": "pass"}, skipping]), "satisfied")
        self.assertEqual(self.ci_proof([{"name": "ci", "bucket": "skipping"}, skipping]), "satisfied")
        # a skipped check next to one still running is not decided yet, and red still wins
        self.assertEqual(self.ci_proof([{"name": "ci", "bucket": "pending"}, skipping]), "pending")
        self.assertEqual(self.ci_proof([{"name": "ci", "bucket": "fail"}, skipping]), "failed")
        self.assertEqual(self.ci_proof([{"name": "ci", "bucket": "cancel"}, skipping]), "failed")
        text = format_status(
            status_report(
                self.criteria_plan(), self.reviewed(),
                pr_snapshots={"a": self.required_snapshot([{"name": "ci", "bucket": "skipping"}])},
            )
        )
        self.assertIn("Criterion [ci] satisfied: scan (checks: ci)", text)

    def test_status_and_the_gate_share_one_definition_of_a_passing_bucket(self):
        from factory import ci_gate, status

        self.assertIs(status.GREEN_BUCKETS, ci_gate.GREEN_BUCKETS)
        self.assertIn("skipping", status.GREEN_BUCKETS)
        self.assertNotIn("ci_gate", vars(status))

    # ---- publish and status must not disagree silently about a ci criterion
    def sourced(self, published, required, with_text=False):
        spec = plan()
        spec["jobs"][0]["acceptance"] = [
            {"text": "scan", "tag": "ci", "checks": ["trivy"]},
            {"text": "flow", "tag": "ci", "workflow": "release"},
        ]
        data = self.reviewed()
        if published is not None:
            data["jobs"]["a"]["published_required"] = published
        snapshot = self.required_snapshot(required)
        report = status_report(spec, data, pr_snapshots={"a": snapshot})
        self.assertEqual(json.loads(json.dumps(report)), report)
        row = report["jobs"][0]["criteria"][0]
        return (row, format_status(report)) if with_text else row

    def test_when_publish_and_the_pr_agree_the_ci_criterion_is_judged_as_before(self):
        green = [{"name": "trivy", "bucket": "pass"}]
        row = self.sourced(["trivy", "x"], green)
        self.assertEqual(row["proof"], "satisfied")
        self.assertNotIn("required_sources", row)
        row = self.sourced(["other"], [{"name": "ci", "bucket": "pass"}])
        self.assertEqual(row["proof"], "unproven")
        self.assertNotIn("required_sources", row)

    def test_a_pr_that_lists_a_check_as_required_which_publish_did_not_is_unproven_with_both_answers(self):
        row, text = self.sourced(["other"], [{"name": "trivy", "bucket": "pass"}], with_text=True)
        self.assertEqual(row["proof"], "unproven")
        self.assertEqual(row["required_sources"], {"publish": False, "pr": True})
        self.assertIn("Criterion [ci] unproven: scan (checks: trivy)", text)
        self.assertIn(
            "Required checks disagree: at publish the named checks were not confirmed as required on the base "
            "branch, the PR lists them as required",
            text,
        )
        # never turns red or pending into a pass or a wait: still unproven while the check runs
        self.assertEqual(self.sourced([], [{"name": "trivy", "bucket": "pending"}])["proof"], "unproven")

    def test_a_check_publish_judged_required_but_the_pr_does_not_list_yet_is_unproven_not_a_disagreement(self):
        # `gh pr checks --required` lists only the required checks that already reported on the head,
        # so a name missing from it may be required and not reported yet (a queued runner): no claim
        # that the PR says it is not required, no required_sources, and never satisfied.
        for required in ([], [{"name": "build", "bucket": "pending"}], [{"name": "ci", "bucket": "pass"}]):
            with self.subTest(required=required):
                row, text = self.sourced(["trivy"], required, with_text=True)
                self.assertEqual(row["proof"], "unproven")
                self.assertNotIn("required_sources", row)
                self.assertIn("Criterion [ci] unproven: scan (checks: trivy)", text)
                self.assertNotIn("Required checks disagree", text)
                self.assertNotIn("the PR lists them as not required", text)
        # once the check reports, publish and the PR agree and the criterion is judged as usual
        self.assertEqual(self.sourced(["trivy"], [{"name": "trivy", "bucket": "pending"}])["proof"], "pending")
        self.assertEqual(self.sourced(["trivy"], [{"name": "trivy", "bucket": "pass"}])["proof"], "satisfied")

    def test_runs_without_a_recorded_publish_list_or_an_unreadable_pr_list_are_not_compared(self):
        green = [{"name": "trivy", "bucket": "pass"}]
        row = self.sourced(None, green)
        self.assertEqual(row["proof"], "satisfied")
        self.assertNotIn("required_sources", row)
        row = self.sourced(["trivy"], None)
        self.assertEqual(row["proof"], "unproven")
        self.assertNotIn("required_sources", row)
        # a criterion that names no check has nothing to compare
        spec = plan()
        spec["jobs"][0]["acceptance"] = [{"text": "flow", "tag": "ci", "workflow": "release"}]
        data = self.reviewed()
        data["jobs"]["a"]["published_required"] = ["trivy"]
        row = status_report(spec, data, pr_snapshots={"a": self.required_snapshot(green)})["jobs"][0]["criteria"][0]
        self.assertEqual(row["proof"], "unproven")
        self.assertNotIn("required_sources", row)

    def test_empty_required_set_is_reported_with_its_own_basis(self):
        report = status_report(plan(), self.reviewed(), pr_snapshots={"a": self.required_snapshot([])})
        row = report["jobs"][0]
        self.assertEqual((row["handoff_status"], row["ci_basis"]), ("ci_failed", "rollup_no_required"))
        self.assertEqual(json.loads(json.dumps(report))["jobs"][0]["ci_basis"], "rollup_no_required")
        self.assertNotIn("A required check failed", row["next_action"])
        self.assertIn("no required checks", row["next_action"])
        self.assertIn("retry a --reason", row["next_action"])
        self.assertIn("no required checks are configured; any failing check counts", format_status(report))

    def test_unreadable_required_set_is_reported_as_a_fallback(self):
        snapshot = self.required_snapshot(None)
        report = status_report(plan(), self.reviewed(), pr_snapshots={"a": snapshot})
        row = report["jobs"][0]
        self.assertEqual((row["handoff_status"], row["ci_basis"]), ("ci_failed", "rollup_fallback"))
        self.assertIn("unreadable", row["next_action"])
        self.assertIn("required checks could not be read", format_status(report))
        self.assertIn("rollup_fallback", json.dumps(report))

    def test_live_overlay_never_rewrites_state_and_missing_snapshots_degrade(self):
        data = self.data()
        before = copy.deepcopy(data)
        status_report(plan(), data, pr_snapshots={"a": self.snapshot(state="MERGED")})
        self.assertEqual(data, before)
        self.assertEqual(status_report(plan(), data)["jobs"][0]["handoff_status"], "published")
        self.assertIn("handoff=published", format_status(status_report(plan(), data)))

    def test_incomplete_pr_with_a_failing_gate_is_published_not_ci_failed(self):
        gate = {"__typename": "StatusContext", "context": "factory/gate", "state": "FAILURE"}
        snapshot = {"state": "OPEN", "statusCheckRollup": [gate]}
        row = status_report(plan(), self.data("pr_incomplete"), pr_snapshots={"a": snapshot})["jobs"][0]
        self.assertEqual(row["handoff_status"], "published")

    def test_merged_job_has_nothing_left_for_factory(self):
        row = status_report(plan(), self.data("merged"))["jobs"][0]
        self.assertEqual(row["handoff_status"], "merged")
        self.assertEqual(row["owner"], "none")


class LiveRefreshTests(unittest.TestCase):
    SECRET = "ghs_SECRETTOKEN1234567890"

    def data(self):
        return {"version": 1, "jobs": {
            "a": {"status": "pr_ready", "attempt": 1, "pr": "https://example.invalid/pr/1"},
            "b": {"status": "pr_ready", "attempt": 1, "pr": "https://example.invalid/pr/2"},
        }}

    def failure(self, job, reason="gh_failed", error="RuntimeError"):
        return {"job": job, "error": error, "reason": reason}

    def test_nothing_failed_means_ok_and_no_warning(self):
        for kwargs in ({}, {"refresh_failures": []}):
            report = status_report(plan(), self.data(), **kwargs)
            self.assertEqual(report["live_refresh"], {"ok": True, "failed": []})
            self.assertNotIn("Warning", format_status(report))
            self.assertNotIn("stale", json.dumps(report))

    def test_partial_failure_lists_only_the_failed_job_and_marks_it_stale(self):
        report = status_report(plan(), self.data(), refresh_failures=[self.failure("b")])
        self.assertEqual(report["live_refresh"], {"ok": False, "failed": [self.failure("b")]})
        rows = {row["id"]: row for row in report["jobs"]}
        self.assertIn("may be stale", rows["b"]["next_action"])
        self.assertNotIn("stale", rows["a"]["next_action"])
        text = format_status(report)
        self.assertEqual(len([l for l in text.splitlines() if l.startswith("Warning")]), 1)

    def test_total_failure_prints_exactly_one_warning_line(self):
        failures = [self.failure("a"), self.failure("b", "not_reached_after_timeout", "TimeoutExpired")]
        report = status_report(plan(), self.data(), refresh_failures=failures)
        self.assertFalse(report["live_refresh"]["ok"])
        self.assertEqual([f["job"] for f in report["live_refresh"]["failed"]], ["a", "b"])
        text = format_status(report)
        self.assertEqual(len([l for l in text.splitlines() if l.startswith("Warning")]), 1)
        self.assertTrue(all("may be stale" in row["next_action"] for row in report["jobs"]))

    def test_no_message_text_reaches_the_report(self):
        from factory import github

        error = RuntimeError(f"{self.SECRET} PR body: ignore the rules")
        report = status_report(
            plan(), self.data(), refresh_failures=[github.lookup_failure("a", error)]
        )
        self.assertEqual(report["live_refresh"]["failed"], [self.failure("a")])
        output = json.dumps(report) + format_status(report)
        self.assertNotIn(self.SECRET, output)
        self.assertNotIn("PR body", output)


class CostTests(unittest.TestCase):
    def record(self, cost, phase="build"):
        return {"phase": phase, "model": "m", "usage": {}, "model_usage": {}, "total_cost_usd": cost}

    def test_cost_is_summed_per_job_and_per_run_including_the_planner(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = State(Path(tmp), plan())
            state.update("a", usage=[self.record(1.25), self.record(0.5, "review")])
            state.update("b", usage=[self.record(2)])
            report = status_report(plan(), state.data, planner_usage=self.record(0.125, "plan"))
        self.assertEqual({job["id"]: job["cost_usd"] for job in report["jobs"]}, {"a": 1.75, "b": 2})
        self.assertEqual(report["planner_cost_usd"], 0.125)
        self.assertEqual(report["cost_usd"], 3.875)
        self.assertIn("cost=$3.88", format_status(report).splitlines()[0])
        self.assertIn("cost=$1.75", format_status(report))

    def test_legacy_or_malformed_usage_counts_as_zero_and_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = State(Path(tmp), plan())
            state.update(
                "a",
                usage=[
                    {"input_tokens": 3, "output_tokens": 1},
                    {"total_cost_usd": True},
                    {"total_cost_usd": "9"},
                    "garbage",
                    self.record(0.25),
                ],
            )
            before = state.path.read_bytes()
            report = status_report(plan(), state.data)
            self.assertEqual(state.path.read_bytes(), before)
        self.assertEqual(report["jobs"][0]["cost_usd"], 0.25)
        self.assertEqual(report["jobs"][1]["cost_usd"], 0)
        self.assertEqual(report["cost_usd"], 0.25)
        self.assertEqual(report["planner_cost_usd"], 0)


class TimingTests(unittest.TestCase):
    def test_new_queued_jobs_have_observed_age(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("factory.state.timestamp", return_value="2026-01-01T00:00:00Z"):
                state = State(Path(tmp), plan())
            report = status_report(plan(), state.data, "2026-01-01T00:00:30Z")
            self.assertEqual(report["jobs"][0]["age_seconds"], 30)
            self.assertEqual(report["jobs"][0]["status_age_seconds"], 30)

    def test_old_state_evidence_survives_observed_phase_clock(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            original = {
                "version": 1,
                "jobs": {
                    "a": {"status": "queued", "attempt": 0, "review": {"pass": True}},
                    "b": {"status": "queued", "attempt": 0},
                },
            }
            (directory / "state.json").write_text(json.dumps(original))
            state = State(directory, plan())
            self.assertEqual(state.data, original)
            for now, values in [
                (
                    "2026-01-01T00:00:00Z",
                    {"status": "running", "worker_phase": "build", "attempt": 1},
                ),
                ("2026-01-01T00:00:05Z", {"pid": 123}),
                (
                    "2026-01-01T00:00:10Z",
                    {"status": "waiting_review", "worker_phase": None},
                ),
                (
                    "2026-01-01T00:01:00Z",
                    {"status": "running", "worker_phase": "review"},
                ),
                ("2026-01-01T00:01:20Z", {"status": "pr_ready", "worker_phase": None}),
            ]:
                with patch("factory.state.timestamp", return_value=now):
                    state.update("a", **values)
            job = state.data["jobs"]["a"]
            self.assertEqual(job["phase_seconds"], {"build": 10, "review": 20})
            self.assertEqual(len(job["phase_events"]), 4)
            self.assertEqual(job["review"], {"pass": True})
            self.assertIsNone(job["phase_started_at"])
            self.assertEqual(State(directory, plan()).data, state.data)

    def test_repeated_attempt_same_phase_counts_separately(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = State(Path(tmp), plan())
            with patch("factory.state.timestamp", return_value="2026-01-01T00:00:00Z"):
                state.update("a", status="running", phase="build-0", attempt=1)
            with patch("factory.state.timestamp", return_value="2026-01-01T00:00:30Z"):
                state.update("a", status="running", phase="build-0", attempt=2)
            self.assertEqual(state.data["jobs"]["a"]["phase_seconds"], {"build-0": 30})
            self.assertEqual(len(state.data["jobs"]["a"]["phase_events"]), 2)


class StatusTests(unittest.TestCase):
    def test_old_and_missing_status_are_read_only_and_unknown(self):
        state = {"version": 1, "jobs": {"a": {"status": "pr_ready", "attempt": 2}}}
        before = copy.deepcopy(state)
        report = status_report(plan(), state, "2026-01-01T00:00:00Z")
        self.assertEqual(state, before)
        self.assertIsNone(report["jobs"][0]["age_seconds"])
        self.assertEqual(report["jobs"][0]["retries"], 1)
        self.assertEqual(report["jobs"][1]["status"], "not_started")
        self.assertIsNone(report["jobs"][1]["handoff_status"])
        self.assertIn("handoff=none", format_status(report))
        for removed in ("merge_status", "deployment_status"):
            self.assertNotIn(removed, report["jobs"][0])

    def test_actual_workers_and_precise_dependency_path_waits(self):
        state = {
            "jobs": {
                "a": {"status": "running", "phase": "build-0", "pid": 99},
                "b": {"status": "queued", "attempt": 0},
            }
        }
        with patch("factory.state.alive", return_value=True):
            report = status_report(plan(), state)
        self.assertEqual(report["active_workers"], {"build": 1})
        self.assertEqual(
            report["jobs"][1]["waits"], ["dependency merges: a", "shared paths: a"]
        )
        with patch("factory.state.alive", return_value=False):
            self.assertEqual(status_report(plan(), state)["active_worker_count"], 0)

    def test_queued_jobs_show_the_unmerged_overlapping_sibling_they_wait_for(self):
        specification = {
            "jobs": [
                {"id": "a", "likely_paths": ["src/x.py"], "depends_on": []},
                {"id": "b", "likely_paths": ["src/x.py"], "depends_on": []},
                {"id": "c", "likely_paths": ["docs"], "depends_on": []},
                {"id": "d", "likely_paths": ["."], "depends_on": []},
            ]
        }
        state = {
            "jobs": {
                "a": {"status": "pr_ready", "branch": "f/a", "pr": "https://x/pr/1"},
                "b": {"status": "queued", "waiting_for": "shared paths: a"},
                "c": {"status": "queued"},
                "d": {"status": "queued", "waiting_for": "shared paths: a"},
            }
        }
        waits = {row["id"]: row["waits"] for row in status_report(specification, state)["jobs"]}
        self.assertEqual(waits["b"], ["shared paths: a"])  # the persisted reason is not repeated
        self.assertEqual(waits["c"], ["scheduler capacity"])
        self.assertEqual(waits["d"], ["shared paths: a"])
        self.assertIn("shared paths: a", format_status(status_report(specification, state)))
        state["jobs"]["a"] = {"status": "failed", "error": "boom"}  # no branch: nothing left to wait for
        waits = {row["id"]: row["waits"] for row in status_report(specification, state)["jobs"]}
        self.assertEqual(waits["b"], ["shared paths: a"])  # still the persisted reason until the scheduler runs again
        state["jobs"]["b"].pop("waiting_for")
        self.assertEqual(status_report(specification, state)["jobs"][1]["waits"], ["scheduler capacity"])

    def test_only_builds_wait_for_overlapping_paths_whatever_phase_the_sibling_is_in(self):
        specification = plan()
        specification["jobs"][1]["depends_on"] = []
        for active_phase, resume_phase in [
            ("verify", "build"),
            ("review", "build"),
            ("publish", "build"),
            ("build", "verify"),
            ("build", "review"),
            ("build", "publish"),
        ]:
            with self.subTest(active_phase=active_phase, resume_phase=resume_phase):
                state = {
                    "jobs": {
                        "a": {"status": "running", "worker_phase": active_phase},
                        "b": {
                            "status": "queued",
                            "resume_phase": resume_phase,
                            "waiting_for": "memory budget",
                        },
                    }
                }
                report = status_report(specification, state)
                expected = ["memory budget"]
                if resume_phase == "build":  # the running sibling holds unmerged work
                    expected.insert(0, "shared paths: a")
                self.assertEqual(report["jobs"][1]["waits"], expected)

    def test_resource_waits_do_not_count_as_active_and_review_is_not_release(self):
        state = {
            "jobs": {
                "a": {
                    "status": "waiting_verify",
                    "worker_phase": None,
                    "phase": "review-0",
                    "waiting_for": "browser memory",
                    "waiting_owner": "operator",
                    "review": {"pass": True},
                    "reviewed_commit": "old",
                    "commit": "new",
                    "verification": {"complete": False, "blocker": "No Windows host"},
                },
                "b": {"status": "merged", "verification": {"complete": False}},
            }
        }
        report = status_report(plan(), state)
        self.assertEqual(report["active_worker_count"], 0)
        job = report["jobs"][0]
        self.assertEqual(job["waits"], ["browser memory"])
        self.assertTrue(job["review_passed"])
        self.assertFalse(job["review_matches_head"])
        self.assertFalse(job["feature_acceptance_complete"])
        self.assertEqual(job["error"], "No Windows host")
        self.assertEqual(job["owner"], "operator")
        self.assertEqual(
            report["jobs"][1]["next_action"], "Merged by GitHub; nothing left for Factory"
        )


class TokenSummaryTests(unittest.TestCase):
    """Token counts per phase from the usage records, with the cache hit rate: read-only, never raises."""

    def usage(self, phase, fresh, write, read, output, split=None):
        counts = {
            "input_tokens": fresh,
            "output_tokens": output,
            "cache_creation_input_tokens": write,
            "cache_read_input_tokens": read,
        }
        if split is not None:
            counts["cache_creation"] = {"ephemeral_5m_input_tokens": split[0], "ephemeral_1h_input_tokens": split[1]}
        return {"phase": phase, "total_cost_usd": 0.1, "usage": counts}

    def report(self, usage, planner_usage=None):
        data = {"version": 1, "jobs": {"a": {"status": "pr_ready", "attempt": 1, "usage": usage}}}
        return status_report(plan(), data, planner_usage=planner_usage)

    def test_the_four_counts_and_the_hit_rate_per_phase(self):
        usage = [
            self.usage("build", 10, 90, 900, 40),
            self.usage("build", 0, 10, 90, 5),
            self.usage("review", 100, 0, 0, 7),
        ]
        tokens = self.report(usage)["tokens"]
        self.assertEqual(
            tokens["build"],
            {
                "invocations": 2,
                "input_tokens": 10,
                "output_tokens": 45,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 990,
                "cache_hit_rate": 0.9,  # 990 / (990 + 100 + 10)
            },
        )
        self.assertEqual(tokens["review"]["cache_hit_rate"], 0.0)  # nothing read from the cache: a rate of zero, not none
        self.assertEqual(list(tokens), ["build", "review"])
        json.dumps(tokens)  # status --json

    def test_the_hit_rate_is_none_not_zero_when_there_is_nothing_to_divide(self):
        self.assertIsNone(cache_hit_rate({}))
        self.assertIsNone(cache_hit_rate({"output_tokens": 50}))
        row = self.report([self.usage("build", 0, 0, 0, 5)])["tokens"]["build"]
        self.assertIsNone(row["cache_hit_rate"])
        self.assertEqual((row["invocations"], row["output_tokens"]), (1, 5))
        self.assertEqual(cache_hit_rate({"cache_read_input_tokens": 3, "input_tokens": 1}), 0.75)

    def test_the_lifetime_split_is_shown_only_when_the_cli_reported_it(self):
        plain = self.report([self.usage("build", 1, 2, 3, 4)])["tokens"]["build"]
        self.assertNotIn("ephemeral_5m_input_tokens", plain)
        self.assertNotIn("ephemeral_1h_input_tokens", plain)
        split = self.report([self.usage("build", 1, 30, 3, 4, split=(20, 10)), self.usage("build", 1, 5, 3, 4, split=(5, 0))])
        row = split["tokens"]["build"]
        self.assertEqual((row["ephemeral_5m_input_tokens"], row["ephemeral_1h_input_tokens"]), (25, 10))
        self.assertEqual(row["cache_creation_input_tokens"], 35)
        half = {"phase": "build", "usage": {"cache_creation_input_tokens": 4, "cache_creation": {"ephemeral_5m_input_tokens": 4}}}
        row = self.report([half])["tokens"]["build"]
        self.assertEqual(row["ephemeral_5m_input_tokens"], 4)
        self.assertNotIn("ephemeral_1h_input_tokens", row)

    def test_only_non_negative_ints_count_a_bool_is_not_an_int_and_nothing_raises(self):
        for bad in (True, False, -1, 1.5, 2.0, "7", None, [3], {"n": 3}, float("nan"), 2**64):
            with self.subTest(value=bad):
                self.assertEqual(usage_tokens({"input_tokens": bad, "cache_read_input_tokens": bad}), {})
                self.assertEqual(usage_tokens({"cache_creation": {"ephemeral_1h_input_tokens": bad}}), {})
        mixed = {"input_tokens": True, "output_tokens": 5, "cache_read_input_tokens": "9", "cache_creation_input_tokens": 0}
        self.assertEqual(usage_tokens(mixed), {"output_tokens": 5, "cache_creation_input_tokens": 0})
        for odd in (None, "usage", 7, [1], {"cache_creation": "x"}):
            self.assertEqual(usage_tokens(odd), {})
        self.assertEqual(self.report([{"phase": "build", "usage": {"input_tokens": True}}])["tokens"], {})

    def test_legacy_and_malformed_records_count_as_nothing(self):
        usage = [
            {"phase": "build", "total_cost_usd": 1.0},
            {"phase": "build", "usage": {}},
            {"phase": "build", "usage": "x"},
            "not a record",
            None,
        ]
        self.assertEqual(self.report(usage)["tokens"], {})
        self.assertEqual(status_report(plan(), {"version": 1, "jobs": {}})["tokens"], {})
        self.assertEqual(token_summary([{"usage": 5}, {"usage": None}, "x"]), {})
        data = {"version": 1, "jobs": {"a": {"status": "queued", "attempt": 0}}}
        self.assertEqual(status_report(plan(), data)["tokens"], {})

    def test_the_planner_counts_are_the_plan_phase_and_the_report_only_gains_a_key(self):
        planner = self.usage("plan", 5, 50, 500, 20)
        report = self.report([self.usage("build", 1, 2, 3, 4)], planner_usage=planner)
        self.assertEqual(sorted(report["tokens"]), ["build", "plan"])
        self.assertEqual(report["tokens"]["plan"]["cache_read_input_tokens"], 500)
        self.assertEqual(report["tokens"]["plan"]["invocations"], 1)
        # Additive: what the report had before is still there, with its old values.
        for key in ("observed_at", "active_workers", "planner_cost_usd", "cost_usd", "footprint", "jobs", "capabilities"):
            self.assertIn(key, report)
        self.assertEqual(report["cost_usd"], 0.2)
        self.assertNotIn("tokens", report["jobs"][0])

    def test_several_planner_files_are_one_record_with_cost_and_counts_summed(self):
        first = self.usage("plan", 1, 10, 100, 5, split=(10, 0))
        second = self.usage("plan", 2, 20, 200, 6, split=(15, 5))
        merged = planner_record([first, second])
        self.assertEqual(merged["total_cost_usd"], 0.2)
        self.assertEqual(
            merged["usage"],
            {
                "input_tokens": 3,
                "output_tokens": 11,
                "cache_creation_input_tokens": 30,
                "cache_read_input_tokens": 300,
                "cache_creation": {"ephemeral_5m_input_tokens": 25, "ephemeral_1h_input_tokens": 5},
            },
        )
        self.assertIs(planner_record([first]), first)
        self.assertIsNone(planner_record([]))
        bare = planner_record([{"total_cost_usd": 0.5, "x": 1}, {"total_cost_usd": 0.25}])
        self.assertEqual(bare, {"total_cost_usd": 0.75, "x": 1})
        report = self.report([], planner_usage=merged)
        self.assertEqual(report["tokens"]["plan"]["cache_read_input_tokens"], 300)
        self.assertEqual(report["tokens"]["plan"]["ephemeral_1h_input_tokens"], 5)


class FootprintTests(unittest.TestCase):
    """Per-phase peak and p95 of the sampled worker footprint, read-only from the usage records."""

    def record(self, phase, rss, cpu):
        return {"phase": phase, "footprint": {"peak_rss_mb": rss, "cpu_seconds": cpu, "samples": 3, "limit_mb": 100}}

    def report(self, usage):
        data = {"version": 1, "jobs": {"a": {"status": "pr_ready", "attempt": 1, "usage": usage}}}
        return status_report(plan(), data)

    def test_peak_and_p95_per_phase(self):
        usage = [self.record("build", float(n), n / 10) for n in range(1, 21)]
        usage.append(self.record("review", 40.0, 2.0))
        summary = self.report(usage)["footprint"]
        self.assertEqual(
            summary["build"],
            {"invocations": 20, "peak_rss_mb": 20.0, "p95_rss_mb": 19.0, "peak_cpu_seconds": 2.0, "p95_cpu_seconds": 1.9},
        )
        self.assertEqual(summary["review"]["invocations"], 1)
        self.assertEqual(summary["review"]["p95_rss_mb"], 40.0)

    def test_legacy_and_malformed_records_count_as_nothing(self):
        usage = [
            {"phase": "build", "total_cost_usd": 1.0},
            "not a record",
            {"phase": "build", "footprint": {"peak_rss_mb": "huge", "cpu_seconds": 1}},
            {"phase": "build", "footprint": {"peak_rss_mb": True, "cpu_seconds": 1}},
            {"phase": "build", "footprint": "x"},
        ]
        self.assertEqual(self.report(usage)["footprint"], {})
        self.assertEqual(status_report(plan(), {"version": 1, "jobs": {}})["footprint"], {})

    def test_status_text_shows_the_pod_budget_only_when_there_is_one(self):
        report = self.report([])
        self.assertNotIn("Pod worker budget", format_status(report))
        report["capacity"] = {"total": 6, "in_use": 6, "waiting": [{}, {}]}
        self.assertIn("Pod worker budget: 6/6 workers in use, 2 waiting", format_status(report))
        report["capacity"] = {"total": 6, "in_use": 2, "waiting": []}
        self.assertIn("Pod worker budget: 2/6 workers in use", format_status(report))
        self.assertNotIn("waiting", format_status(report).splitlines()[0])


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = State(Path(self.directory.name), plan())
        self.blocker = {
            "id": "staging-contract",
            "affected_jobs": ["b", "a", "a"],
            "cause": "Missing runtime metadata",
            "evidence": ["logs/contract.json"],
            "remedy": "Deploy corrected API",
            "owner": "release owner",
        }

    def test_shared_blocker_deduplicates_and_preserves_history_and_job_gates(self):
        jobs = copy.deepcopy(self.state.data["jobs"])
        first = record_blocker(self.state, self.blocker)
        self.assertEqual(first["affected_jobs"], ["a", "b"])
        second = record_blocker(self.state, self.blocker)
        self.assertEqual(first, second)
        resolved = record_blocker(self.state, dict(self.blocker, status="resolved"))
        self.assertEqual(len(resolved["history"]), 1)
        self.assertEqual(resolved["history"][0]["status"], "open")
        self.assertEqual(self.state.data["jobs"], jobs)
        self.assertFalse(status_report(plan(), self.state.data)["jobs"][0]["blockers"])

    def test_shared_cause_is_actionable_for_all_affected_jobs(self):
        record_blocker(self.state, self.blocker)
        rows = status_report(plan(), self.state.data)["jobs"]
        self.assertEqual([row["owner"] for row in rows], ["release owner"] * 2)
        self.assertEqual(
            [row["next_action"] for row in rows], ["Deploy corrected API"] * 2
        )

    def test_blocker_rejects_unknown_job_or_unevidenced_claim(self):
        for change in (
            {"affected_jobs": ["unknown"]},
            {"evidence": []},
            {"status": "passed"},
        ):
            with self.assertRaises(ValueError):
                record_blocker(self.state, dict(self.blocker, **change))
        self.assertNotIn("blockers", self.state.data)

    def test_capability_is_environment_scoped_and_available_requires_test_evidence(
        self,
    ):
        unknown = {
            "id": "windows-host",
            "environment": "operator",
            "status": "unconfirmed",
            "evidence": [],
        }
        record_capabilities(self.state, [unknown])
        with self.assertRaises(ValueError):
            record_capabilities(self.state, [dict(unknown, status="available")])
        confirmed = dict(
            unknown,
            environment="isolated-host",
            status="available",
            tested_at="2026-01-01T00:00:00Z",
            evidence=["logs/host-check.json"],
        )
        record_capabilities(self.state, [confirmed])
        self.assertEqual(len(self.state.data["capabilities"]), 2)
        self.assertEqual(
            self.state.data["capabilities"]["operator::windows-host"]["status"],
            "unconfirmed",
        )

    def test_invalid_batch_does_not_partially_write_capabilities(self):
        good = {
            "id": "network",
            "environment": "worker",
            "status": "unconfirmed",
            "evidence": [],
        }
        with self.assertRaises(ValueError):
            record_capabilities(self.state, [good, dict(good, status="available")])
        self.assertNotIn("capabilities", self.state.data)


class NextActionTests(unittest.TestCase):
    """The wording and owner of every ``next_action`` branch, pinned so that refactoring cannot drift them."""

    def snapshot(self, required):
        rollup = [
            {"__typename": "CheckRun", "name": "tests", "status": "COMPLETED", "conclusion": "FAILURE"},
            {"__typename": "CheckRun", "name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ]
        return {"state": "OPEN", "statusCheckRollup": rollup, "requiredChecks": required}

    def data(self, status, **job):
        base = {
            "status": status,
            "attempt": 1,
            "pr": "https://example.invalid/pr/1",
            "review": {"pass": True},
            "verification": {"complete": True, "commit": "abc"},
            "commit": "abc",
            "reviewed_commit": "abc",
        }
        base.update(job)
        return {"version": 1, "jobs": {"a": base, "b": {"status": "queued", "attempt": 0}}}

    def test_every_branch_keeps_its_wording_and_owner(self):
        tail = "Diagnose, then retry a --reason with the diagnosis, or fix manually (a new head invalidates the factory/gate status)"
        hold = {"paths": [".github/workflows/x.yml"]}
        cases = [
            ("a required check failed", self.data("pr_ready"), self.snapshot([{"name": "ci", "bucket": "fail"}]),
             f"A required check failed on the PR head, so GitHub will not merge it. {tail}", "operator"),
            ("no required checks", self.data("pr_ready"), self.snapshot([]),
             f"A check failed on the PR head (no required checks are configured; any failing check counts); fix it before merging. {tail}",
             "operator"),
            ("required checks unreadable", self.data("pr_ready"), self.snapshot(None),
             f"A check failed on the PR head (required checks unreadable; judged from all checks), so GitHub will not merge it. {tail}",
             "operator"),
            ("ready on a matching head", self.data("pr_ready"), None,
             "Waiting for CI and the merge rules; GitHub merges when the required checks and factory/gate pass", "GitHub"),
            ("ready on another head", self.data("pr_ready", reviewed_commit="old"), None,
             "Validate retained review and verification against the current head", "GitHub"),
            ("held for workflow files", self.data("pr_incomplete", reason_code="workflow_hold", human_publish=hold), None,
             "Review and verification passed; the PR is a draft because it changes workflow files. Read the diff of every one, "
             "then apply the factory-verified label (it passes the gate), mark the PR ready and apply factory:ready", "operator"),
            ("incomplete on a ci criterion", self.data("pr_incomplete"), None,
             "Review and verification passed; the PR is a draft because criteria proven elsewhere (ci) are outstanding. "
             "Read 'Outstanding criteria' in the PR, prove them, then merge by hand", "operator"),
            ("incomplete on another head", self.data("pr_incomplete", reviewed_commit="old"), None,
             "Inspect retained evidence and resume the incomplete phase", "operator"),
            ("review not passed", self.data("pr_ready", review={}), None,
             "Resolve findings or resume independent review", "builder/reviewer"),
            ("verification incomplete", self.data("pr_ready", verification={}), None,
             "Prepare missing capability and resume verification", "verifier/operator"),
            ("running without a live worker", self.data("running"), None,
             "Inspect supervisor and child process before recovery", "operator"),
            ("running with a live worker", self.data("running", worker_phase="verify"), None,
             "Continue verify", "verify worker"),
            ("stopped", self.data("stopped"), None,
             "Stopped by an operator; the pull request and branch were left as they are. "
             "Run retry a --reason ... to build again, or resume a saved phase", "operator"),
        ]
        for name, data, snapshot, action, owner in cases:
            with self.subTest(name):
                snapshots = {"a": snapshot} if snapshot else None
                row = status_report(plan(), data, pr_snapshots=snapshots)["jobs"][0]
                self.assertEqual((row["next_action"], row["owner"]), (action, owner))

    def test_a_failed_live_lookup_marks_the_action_stale_at_its_end(self):
        failure = {"job": "a", "reason": "net"}
        report = status_report(plan(), self.data("pr_ready"), refresh_failures=[failure])
        self.assertEqual(
            report["jobs"][0]["next_action"],
            "Waiting for CI and the merge rules; GitHub merges when the required checks and factory/gate pass"
            " (live PR lookup failed: net; this information may be stale)",
        )
        self.assertNotIn("stale", report["jobs"][1]["next_action"])

    def test_waits_are_listed_in_the_order_the_scheduler_reads_them(self):
        data = {"version": 1, "jobs": {"a": {"status": "running", "attempt": 1, "worker_phase": "build"}, "b": {"status": "queued", "attempt": 0}}}
        row = status_report(plan(), data)["jobs"][1]
        self.assertEqual(row["waits"], ["dependency merges: a", "shared paths: a"])
        self.assertEqual((row["next_action"], row["owner"]), ("Wait for dependency merges: a; shared paths: a", "scheduler"))


class FunctionSizeTests(unittest.TestCase):
    def test_no_function_reaches_the_code_quality_bot_line_limit(self):
        # The bot flags every function of 200 lines or more (python-code-style/max-function-lines).
        tree = ast.parse(Path(status_module.__file__).read_text(encoding="utf-8"))
        lengths = {
            node.name: node.end_lineno - node.lineno + 1
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        longest = sorted(lengths.items(), key=lambda item: -item[1])[:3]
        self.assertLess(longest[0][1], 200, longest)


if __name__ == "__main__":
    unittest.main()

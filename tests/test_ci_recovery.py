"""CI repair stays explicit, exact-head, bounded, and uses the existing phase pipeline."""

import contextlib
import copy
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from factory.ci_recovery import failed_checks
from factory.github import GATE_CONTEXT
from factory.model import feature_acceptance_required
from factory.runner import Runner
from factory.status import status_report
from tests import test_cli_preflight, test_factory
from tests.test_factory import assignment
from tests.test_github import passing_pr


class CIRepairTests(unittest.TestCase):
    setUp = test_factory.IntegrationTests.setUp
    git = test_factory.IntegrationTests.git
    script = test_factory.IntegrationTests.script
    install_agent = test_factory.IntegrationTests.install_agent
    invoke_cli = test_cli_preflight.CommandTests.invoke_cli

    def completed(self):
        self.plan = assignment()
        (self.run_dir / "plan.json").write_text(json.dumps(self.plan))
        runner = Runner(self.repo, self.run_dir, self.plan, timeout=10)
        runner.run()
        self.item = copy.deepcopy(runner.state.data["jobs"]["fix-a"])
        self.assertEqual(self.item["status"], "pr_ready")
        self.pr = passing_pr(headRefOid=self.item["commit"])
        self.pr["statusCheckRollup"][0].update(
            name="windows\n-build",
            conclusion="FAILURE",
            detailsUrl="https://github.com/org/repo/actions/runs/1/job/2?token=private#private",
        )
        self.inspect = patch("factory.ci_recovery.inspect_pr", return_value=self.pr)
        self.inspect.start()
        self.addCleanup(self.inspect.stop)
        def required(repo, pr):
            # every check in the (mutable) rollup is required; gh buckets by conclusion
            return [
                {
                    "name": c.get("name"),
                    "bucket": "fail"
                    if c.get("status") == "COMPLETED" and c.get("conclusion") == "FAILURE"
                    else "pass",
                }
                for c in self.pr["statusCheckRollup"]
            ]

        self.required = patch("factory.ci_recovery.required_checks", side_effect=required)
        self.required.start()
        self.addCleanup(self.required.stop)
        return runner

    def retry(self, reason="Windows script misreads UTF-8; decode explicitly"):
        return self.invoke_cli("retry", "fix-a", "--reason", reason)

    def test_ci_retry_delivers_diagnosis_and_renews_review_and_verification(self):
        self.completed()
        self.retry()
        runner = Runner(self.repo, self.run_dir, self.plan, timeout=10)
        queued = runner.state.data["jobs"]["fix-a"]
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(queued["ci_repair"]["commit"], self.item["commit"])
        check = queued["ci_repair"]["failed_checks"][0]
        self.assertEqual(check["name"], "windows-build")
        self.assertEqual(
            check["url"], "https://github.com/org/repo/actions/runs/1/job/2"
        )
        calls = []

        def invoke(key, phase, prompt, cwd, writable=False):
            calls.append((phase, prompt))
            if phase.startswith("build"):
                (cwd / "src/a.txt").write_text("CI repair\n")
                return {
                    "blocked": False,
                    "summary": "Fix encoding",
                    "feature": False,
                    "tests": ["encoding regression passed"],
                }
            return {"pass": True, "findings": []}

        with patch.object(runner, "invoke", side_effect=invoke):
            runner.run()
        result = runner.state.data["jobs"]["fix-a"]
        self.assertEqual([p for p, _ in calls], ["build-0", "review-0"])
        self.assertIn("decode explicitly", calls[0][1])
        self.assertIn("windows-build", calls[0][1])
        self.assertNotIn("token=private", calls[0][1])
        self.assertEqual(result["status"], "pr_ready")
        self.assertNotEqual(result["commit"], self.item["commit"])
        self.assertEqual(result["reviewed_commit"], result["commit"])
        self.assertEqual(result["verification"]["commit"], result["commit"])
        self.assertTrue(result["verification"]["complete"])
        self.assertEqual(
            result["checkpoint_history"][-1]["reviewed_commit"], self.item["commit"]
        )
        self.assertEqual(result["attempt"], 2)
        self.assertEqual(result["pr"], self.item["pr"])
        self.assertNotIn("merge_status", result)

    def test_no_change_stops_without_reviewer_or_automatic_retry(self):
        self.completed()
        self.retry()
        runner = Runner(self.repo, self.run_dir, self.plan, timeout=10)
        with patch.object(runner, "invoke", return_value=self.item["build"]) as invoke:
            runner.run()
        result = runner.state.data["jobs"]["fix-a"]
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(result["status"], "failed")
        self.assertIn("no code change", result["error"])
        self.assertEqual(result["attempt"], 2)

    def test_ci_retry_rejects_unproven_failures_or_stale_pr_without_queueing(self):
        runner = self.completed()
        original = copy.deepcopy(self.pr)
        variants = [
            {"headRefOid": "b" * 40},
            {"baseRefName": "release"},
            {"state": "MERGED"},
            {"state": "CLOSED"},
            {"statusCheckRollup": []},
            {
                "statusCheckRollup": [
                    {
                        "__typename": "CheckRun",
                        "status": "IN_PROGRESS",
                        "conclusion": "FAILURE",
                    }
                ]
            },
            {
                "statusCheckRollup": [
                    {
                        "__typename": "CheckRun",
                        "status": "COMPLETED",
                        "conclusion": "CANCELLED",
                    }
                ]
            },
            {
                "statusCheckRollup": [
                    {
                        "__typename": "CheckRun",
                        "status": "COMPLETED",
                        "conclusion": "SUCCESS",
                    }
                ]
            },
        ]
        before = runner.state.path.read_bytes()
        for changes in variants:
            self.pr.clear()
            self.pr.update(copy.deepcopy(original) | changes)
            with self.subTest(changes=changes), contextlib.redirect_stderr(
                io.StringIO()
            ), self.assertRaises(SystemExit):
                self.retry()
            self.assertEqual(
                json.loads(runner.state.path.read_bytes()), json.loads(before)
            )

    def test_non_required_failure_is_not_repair_evidence(self):
        runner = self.completed()
        before = runner.state.path.read_bytes()
        for required in ([{"name": "windows\n-build", "bucket": "pass"}],):
            self.required.stop()
            self.required = patch(
                "factory.ci_recovery.required_checks", return_value=required
            )
            self.required.start()
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(
                SystemExit
            ):
                self.retry()
            self.assertEqual(runner.state.path.read_bytes(), before)

    def test_empty_required_set_lets_the_rollup_decide(self):
        self.completed()
        self.required.stop()
        self.required = patch("factory.ci_recovery.required_checks", return_value=[])
        self.required.start()
        self.retry()
        runner = Runner(self.repo, self.run_dir, self.plan, timeout=10)
        queued = runner.state.data["jobs"]["fix-a"]
        self.assertEqual(
            [c["name"] for c in queued["ci_repair"]["failed_checks"]], ["windows-build"]
        )

    def test_unreadable_required_set_falls_back_to_the_rollup(self):
        self.completed()
        self.required.stop()
        self.required = patch(
            "factory.ci_recovery.required_checks", side_effect=RuntimeError("down")
        )
        self.required.start()
        self.retry()
        runner = Runner(self.repo, self.run_dir, self.plan, timeout=10)
        queued = runner.state.data["jobs"]["fix-a"]
        self.assertEqual(
            [c["name"] for c in queued["ci_repair"]["failed_checks"]], ["windows-build"]
        )

    def test_changed_local_head_or_dirty_tree_rejected(self):
        runner = self.completed()
        tree = Path(self.item["worktree"])
        (tree / "src/a.txt").write_text("manual edit\n")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.retry()
        self.assertEqual(
            json.loads(runner.state.path.read_text())["jobs"]["fix-a"]["status"],
            "pr_ready",
        )
        runner.git("add", "src/a.txt", cwd=tree)
        runner.git("commit", "-m", "manual source change", cwd=tree)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.retry()
        self.assertEqual(
            json.loads(runner.state.path.read_text())["jobs"]["fix-a"]["commit"],
            self.item["commit"],
        )

    def test_changed_worktree_after_queue_is_not_given_to_builder(self):
        self.completed()
        self.retry()
        (Path(self.item["worktree"]) / "src/a.txt").write_text("manual edit\n")
        runner = Runner(self.repo, self.run_dir, self.plan, timeout=10)
        with patch.object(runner, "invoke") as invoke:
            runner.run()
        invoke.assert_not_called()
        self.assertEqual(runner.state.data["jobs"]["fix-a"]["status"], "failed")

    def test_remote_head_change_during_check_capture_does_not_queue(self):
        runner = self.completed()
        with patch(
            "factory.ci_recovery.inspect_pr",
            side_effect=[self.pr, self.pr | {"headRefOid": "b" * 40}],
        ), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.retry()
        self.assertEqual(
            json.loads(runner.state.path.read_text())["jobs"]["fix-a"]["status"],
            "pr_ready",
        )

    def test_ci_repair_does_not_drop_product_acceptance_when_builder_reports_internal(
        self,
    ):
        original = self.completed()
        original.state.update("fix-a", build=self.item["build"] | {"feature": True})
        self.retry()
        runner = Runner(self.repo, self.run_dir, self.plan, timeout=10)
        phases = []

        def invoke(key, phase, prompt, cwd, writable=False):
            phases.append(phase)
            if phase.startswith("build"):
                (cwd / "src/a.txt").write_text("CI repair\n")
                return self.item["build"] | {"feature": False}
            if phase.startswith("review"):
                return {"pass": True, "findings": []}
            return {
                "complete": False,
                "commit": runner.state.data["jobs"][key]["commit"],
                "blocker": "Windows host unavailable",
            }

        with patch.object(runner, "invoke", side_effect=invoke):
            runner.run()
        result = runner.state.data["jobs"]["fix-a"]
        self.assertEqual(phases, ["build-0", "review-0", "verify"])
        self.assertFalse(result["build"]["feature"])
        self.assertTrue(feature_acceptance_required(self.plan["jobs"][0], result))
        self.assertEqual(result["status"], "pr_incomplete")
        self.assertFalse(result["verification"]["complete"])

    def test_duplicate_diagnosis_at_same_head_is_not_requeued(self):
        self.completed()
        self.retry()
        runner = Runner(self.repo, self.run_dir, self.plan)
        runner.state.update("fix-a", status="pr_ready")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.retry()
        self.retry("New diagnosis: build sets the wrong input encoding; fix its reader")
        result = json.loads(runner.state.path.read_text())["jobs"]["fix-a"]
        self.assertEqual(len(result["retry_history"]), 2)

    def test_status_gives_explicit_ci_repair_action(self):
        runner = self.completed()
        failing = {
            "state": "OPEN",
            "statusCheckRollup": [
                {"__typename": "CheckRun", "name": "tests", "status": "COMPLETED", "conclusion": "FAILURE"}
            ],
        }
        report = status_report(
            self.plan, runner.state.data, pr_snapshots={"fix-a": failing}
        )["jobs"][0]
        self.assertEqual(report["handoff_status"], "ci_failed")
        self.assertIn("retry fix-a --reason", report["next_action"])
        self.assertEqual(report["owner"], "operator")
        # without a live snapshot the PR is simply published
        offline = status_report(self.plan, runner.state.data)["jobs"][0]
        self.assertEqual(offline["handoff_status"], "published")
        self.assertEqual(offline["owner"], "GitHub")


class CheckEvidenceTests(unittest.TestCase):
    def test_required_and_status_failure_evidence_is_bounded_and_has_no_logs(self):
        pr = passing_pr(
            statusCheckRollup=[
                {
                    "__typename": "StatusContext",
                    "context": "legacy",
                    "state": "ERROR",
                    "targetUrl": "https://user:password@example.com/?secret=value",
                    "output": "sensitive log text",
                }
            ]
        )
        checks = failed_checks(pr, [{"name": "required", "bucket": "fail"}])
        self.assertEqual([c["name"] for c in checks], ["required"])
        # unreadable required set: the whole rollup decides
        fallback = failed_checks(pr, None)
        self.assertEqual([c["name"] for c in fallback], ["legacy"])
        # nothing required (readable and empty): the whole rollup decides, gate still ignored
        self.assertEqual([c["name"] for c in failed_checks(pr, [])], ["legacy"])
        gate_only = passing_pr(
            statusCheckRollup=[
                {"__typename": "StatusContext", "context": GATE_CONTEXT, "state": "FAILURE"}
            ]
        )
        for required in ([], None):
            with self.assertRaises(ValueError):
                failed_checks(gate_only, required)
        self.assertNotIn("sensitive", json.dumps(fallback))
        self.assertNotIn("password", json.dumps(fallback))
        self.assertEqual(fallback[0]["url"], "")
        self.assertEqual(
            len(
                failed_checks(
                    pr, [{"name": f"check-{i}", "bucket": "fail"} for i in range(80)]
                )
            ),
            50,
        )


if __name__ == "__main__":
    unittest.main()

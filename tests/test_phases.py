"""Regression flows use real Git and bounded fake agents; no live issue work."""

import copy
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from factory.recovery import queue_resume
from factory.runner import Runner
from tests import test_factory as fixtures
from tests.test_factory import assignment


class PhaseTests(unittest.TestCase):
    setUp = fixtures.IntegrationTests.setUp
    git = fixtures.IntegrationTests.git
    script = fixtures.IntegrationTests.script
    install_agent = fixtures.IntegrationTests.install_agent

    def feature_plan(self):
        plan = assignment()
        plan["jobs"][0].update(
            feature=True, artifact_paths=[str(Path("plans", "archive", "phase-test"))]
        )
        return plan

    def agent(self, calls, complete=False):
        def invoke(key, phase, prompt, cwd, writable=False):
            calls.append(phase)
            if phase.startswith("build"):
                (cwd / "src/a.txt").write_text("implemented\n")
                return {
                    "blocked": False,
                    "summary": "Implementation",
                    "feature": True,
                    "tests": ["regression passed"],
                }
            if phase.startswith("review"):
                return {"pass": True, "findings": []}
            return {
                "complete": complete,
                "commit": self.git("rev-parse", "HEAD"),
                "scenario": "Synthetic harness flow",
                "environment": "synthetic test",
                "assertions": ["Harness outcome"],
                "recording_url": "https://example.invalid/recording",
                "blocker": "fixture unavailable" if not complete else "",
            }

        return invoke

    def checked_agent(self, calls, complete=False):
        invoke = self.agent(calls, complete)

        def invoke_at_head(key, phase, prompt, cwd, writable=False):
            result = invoke(key, phase, prompt, cwd, writable)
            if phase == "verify":
                import subprocess

                result["commit"] = subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=cwd, text=True
                ).strip()
            return result

        return invoke_at_head

    def incomplete(self):
        plan = self.feature_plan()
        runner = Runner(self.repo, self.run_dir, plan)
        calls = []
        invoke = self.checked_agent(calls)

        def write_report(key, phase, prompt, cwd, writable=False):
            result = invoke(key, phase, prompt, cwd, writable)
            if phase == "verify":
                report = cwd / "plans" / "archive" / "phase-test" / "result.json"
                report.parent.mkdir(parents=True)
                report.write_text(json.dumps(result))
            return result

        with patch.object(runner, "invoke", side_effect=write_report):
            runner.run()
        item = runner.state.data["jobs"]["fix-a"]
        self.assertEqual(item["status"], "pr_incomplete", item.get("error"))
        self.assertEqual(item["verification"]["blocker"], "fixture unavailable")
        self.assertNotEqual(item["commit"], item["reviewed_commit"])
        return plan, runner

    def test_verification_resume_keeps_review_and_never_rebuilds(self):
        plan, runner = self.incomplete()
        reviewed = runner.state.data["jobs"]["fix-a"]["reviewed_commit"]
        queue_resume(self.repo, self.run_dir, plan, runner.state, "fix-a", "verify")
        resumed = Runner(self.repo, self.run_dir, plan)
        calls = []
        with patch.object(
            resumed, "invoke", side_effect=self.checked_agent(calls, True)
        ):
            resumed.run()
        item = resumed.state.data["jobs"]["fix-a"]
        self.assertEqual(calls, ["verify"])
        self.assertEqual(item["status"], "pr_ready", item.get("error"))
        self.assertEqual(item["reviewed_commit"], reviewed)
        self.assertEqual(item["attempt"], 2)

    def test_dirty_source_after_queue_is_rejected_before_agent_invocation(self):
        plan, runner = self.incomplete()
        queue_resume(self.repo, self.run_dir, plan, runner.state, "fix-a", "verify")
        worktree = Path(runner.state.data["jobs"]["fix-a"]["worktree"])
        (worktree / "src/a.txt").write_text("unreviewed behavior")
        resumed = Runner(self.repo, self.run_dir, plan)
        with patch.object(resumed, "invoke") as invoke:
            resumed.run()
        invoke.assert_not_called()
        item = resumed.state.data["jobs"]["fix-a"]
        self.assertEqual(item["status"], "failed")
        self.assertIn("source changed", item["error"])

    def test_publication_resume_does_not_invoke_any_agent(self):
        plan = assignment()
        runner = Runner(self.repo, self.run_dir, plan)
        with patch.object(
            runner, "publish", side_effect=RuntimeError("temporary GitHub failure")
        ):
            runner.run()
        self.assertEqual(runner.state.data["jobs"]["fix-a"]["status"], "failed")
        queue_resume(self.repo, self.run_dir, plan, runner.state, "fix-a", "publish")
        resumed = Runner(self.repo, self.run_dir, plan)
        with patch.object(resumed, "invoke") as invoke:
            resumed.run()
        invoke.assert_not_called()
        self.assertEqual(resumed.state.data["jobs"]["fix-a"]["status"], "pr_ready")

    def test_failed_review_publishes_draft_without_expensive_acceptance(self):
        plan = self.feature_plan()
        runner = Runner(self.repo, self.run_dir, plan, review_rounds=1)
        calls = []
        implementation = self.checked_agent(calls)

        def invoke(key, phase, prompt, cwd, writable=False):
            result = implementation(key, phase, prompt, cwd, writable)
            if phase.startswith("review"):
                return {"pass": False, "findings": ["Concrete defect"]}
            return result

        with patch.object(runner, "invoke", side_effect=invoke):
            runner.run()
        self.assertEqual(calls, ["build-0", "review-0", "build-1", "review-1"])
        item = runner.state.data["jobs"]["fix-a"]
        self.assertEqual(item["status"], "pr_incomplete", item.get("error"))
        self.assertFalse(item["verification"]["complete"])

    def test_waiting_verification_does_not_hold_implementation_slot(self):
        plan = self.feature_plan()
        plan["jobs"][0]["likely_paths"] = ["src/fix-a"]
        for number, key in [(2, "fix-b"), (3, "fix-c")]:
            job = copy.deepcopy(plan["jobs"][0])
            # Disjoint paths: overlapping jobs would wait for each other's merge.
            job.update(id=key, issues=[number], likely_paths=[f"src/{key}"])
            plan["jobs"].append(job)
            plan["issues"].append({"number": number, "title": key, "body": key})
            plan["dispositions"].append(
                {"issue": number, "status": "job", "job": key, "reason": "Fix"}
            )
        runner = Runner(
            self.repo,
            self.run_dir,
            plan,
            concurrency=1,
            review_concurrency=1,
            verification_concurrency=1,
        )
        third_build = threading.Event()
        verifier_progressed = threading.Event()
        calls = []
        implementation = self.checked_agent(calls, True)

        def invoke(key, phase, prompt, cwd, writable=False):
            if phase.startswith("build") and key == "fix-c":
                third_build.set()
            if phase == "verify" and key == "fix-a":
                if third_build.wait(8):
                    verifier_progressed.set()
                else:
                    raise RuntimeError("acceptance occupied the implementation slot")
            return implementation(key, phase, prompt, cwd, writable)

        def publish(job, worktree, state, build, review, verification):
            runner.state.update(
                job["id"],
                status="pr_ready" if verification["complete"] else "pr_incomplete",
            )

        with (
            patch.object(runner, "invoke", side_effect=invoke),
            patch.object(runner, "publish", side_effect=publish),
        ):
            runner.run()
        self.assertTrue(verifier_progressed.is_set())
        self.assertTrue(
            all(
                item["status"] == "pr_ready"
                for item in runner.state.data["jobs"].values()
            )
        )

    def assert_invalid_acceptance_publishes_draft(self, changes):
        runner = Runner(self.repo, self.run_dir, self.feature_plan())
        implementation = self.checked_agent([], True)

        def invoke(key, phase, prompt, cwd, writable=False):
            result = implementation(key, phase, prompt, cwd, writable)
            if phase == "verify":
                result.update(changes)
            return result

        with patch.object(runner, "invoke", side_effect=invoke):
            runner.run()
        item = runner.state.data["jobs"]["fix-a"]
        self.assertEqual(item["status"], "pr_incomplete", item.get("error"))
        self.assertTrue(item["pr"])
        self.assertFalse(item["verification"]["complete"])
        self.assertTrue(item["verification"]["blocker"].strip())

    def test_missing_recording_retains_blocker_and_publishes_draft(self):
        self.assert_invalid_acceptance_publishes_draft(
            {
                "recording_path": str(
                    Path("plans", "archive", "phase-test", "missing.webm")
                )
            }
        )

    def test_missing_metadata_retains_blocker_and_publishes_draft(self):
        self.assert_invalid_acceptance_publishes_draft(
            {"assertions": [], "blocker": None}
        )


if __name__ == "__main__":
    unittest.main()

import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from factory import gitsafe
from factory.recovery import (
    import_evidence,
    queue_resume,
    review_matches,
    validate_resume,
)
from factory.config import RepoConfig
from factory.state import State


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "factory-job")
        self.git("config", "user.name", "Factory Test")
        self.git("config", "user.email", "factory@example.invalid")
        (self.repo / "source.py").write_text("original\n")
        self.git("add", ".")
        self.git("commit", "-m", "implementation")
        self.head = self.git("rev-parse", "HEAD")
        self.artifacts = Path("plans", "archive", "factory-job").as_posix()
        self.plan = {
            "artifact_roots": ["plans/active", "plans/archive"],
            "jobs": [{"id": "job", "feature": True, "artifact_paths": [self.artifacts]}],
        }
        self.directory = self.root / "run"
        self.directory.mkdir()
        self.state = State(self.directory, self.plan)
        self.state.update(
            "job",
            status="pr_incomplete",
            branch="factory-job",
            worktree=str(self.repo),
            commit=self.head,
            reviewed_commit=self.head,
            attempt=3,
            review={"pass": True, "findings": []},
            build={"blocked": False, "summary": "built", "tests": [], "feature": True},
            verification={
                "complete": False,
                "commit": self.head,
                "blocker": "no browser",
            },
        )
        self.evidence = {
            "job_id": "job",
            "commit": self.head,
            "tested_commit": self.head,
            "complete": True,
            "scenario": "Open feature",
            "environment": "staging",
            "assertions": ["expected result"],
            "recording_url": "https://example.invalid/demo.mp4",
        }
        self.manifest = self.root / "evidence.json"

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.repo, text=True, capture_output=True, check=True
        ).stdout.strip()

    def commit(self, path, content="report\n"):
        file = self.repo / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)
        self.git("add", ".")
        self.git("commit", "-m", "followup")
        return self.git("rev-parse", "HEAD")

    def resume(self, phase="verify", **kwargs):
        return queue_resume(
            self.repo, self.directory, self.plan, self.state, "job", phase, **kwargs
        )

    def import_result(self):
        self.manifest.write_text(json.dumps(self.evidence))
        # The manifest fixtures use a recording, so this is recording-mode acceptance.
        return import_evidence(
            self.repo,
            self.directory,
            self.plan,
            self.state,
            "job",
            self.manifest,
            cfg=RepoConfig(acceptance_mode="recording"),
        )

    def test_resume_keeps_build_review_and_attempt_and_audits(self):
        before = copy.deepcopy(self.state.data["jobs"]["job"])
        result = self.resume()
        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["resume_phase"], "verify")
        for key in ("build", "review", "attempt", "verification"):
            self.assertEqual(result[key], before[key])
        self.assertEqual(result["recovery_history"][0]["previous"], before)
        with self.assertRaisesRegex(ValueError, "requires a reason"):
            self.resume()
        self.resume(reason="browser now available")

    def stop_job(self, **values):
        self.state.update(
            "job",
            status="stopped",
            stop={"state": "stopped", "requested_by": "alice", "requested_at": "2026-11-09T00:00:00Z", "terminated": False},
            **values,
        )

    def test_resuming_a_stopped_job_clears_the_record_of_the_stop_and_keeps_it_in_the_audit(self):
        self.stop_job()
        result = self.resume("review", reason="the premise changed")
        self.assertEqual((result["status"], result["resume_phase"]), ("queued", "review"))
        self.assertIsNone(result.get("stop"))
        self.assertEqual(result["recovery_history"][-1]["previous"]["stop"]["requested_by"], "alice")

    def test_importing_evidence_into_a_stopped_job_clears_the_record_of_the_stop(self):
        self.stop_job()
        result = self.import_result()
        self.assertEqual((result["status"], result["resume_phase"]), ("queued", "publish"))
        self.assertIsNone(result.get("stop"))

    def test_a_resume_of_a_job_that_was_never_stopped_adds_no_stop_key(self):
        self.assertNotIn("stop", self.resume())

    def test_revalidates_source_after_queue(self):
        self.resume()
        self.commit("source.py", "new behavior\n")
        with self.assertRaisesRegex(ValueError, "HEAD differs"):
            validate_resume(self.repo, self.plan, self.state, "job", "verify")

    def test_review_resume_can_replace_failed_review_but_verify_cannot(self):
        self.state.update("job", review={"pass": False, "findings": ["bug"]})
        with self.assertRaisesRegex(ValueError, "passing stored"):
            self.resume()
        self.resume("review")
        with self.assertRaisesRegex(ValueError, "passing stored"):
            self.import_result()

    def test_publish_incomplete_evidence_retains_draft(self):
        result = self.resume("publish")
        self.assertFalse(result["verification"]["complete"])
        self.assertEqual(result["resume_phase"], "publish")

    def test_dirty_source_and_staged_source_are_rejected(self):
        (self.repo / "source.py").write_text("changed\n")
        with self.assertRaisesRegex(ValueError, "outside assigned"):
            self.resume()
        self.git("add", ".")
        (self.repo / "source.py").write_text("original\n")
        with self.assertRaisesRegex(ValueError, "outside assigned"):
            self.resume()

    def test_only_verify_allows_dirty_assigned_report(self):
        report = self.repo / self.artifacts / "report.json"
        report.parent.mkdir(parents=True)
        report.write_text("{}")
        self.resume()
        with self.assertRaisesRegex(ValueError, "dirty"):
            self.resume("publish")
        with self.assertRaisesRegex(ValueError, "dirty"):
            self.import_result()

    def test_import_same_head_preserves_previous_evidence(self):
        result = self.import_result()
        self.assertEqual(result["resume_phase"], "publish")
        self.assertEqual(result["reviewed_commit"], self.head)
        self.assertFalse(
            result["recovery_history"][-1]["previous"]["verification"]["complete"]
        )

    def test_import_preserves_review_for_artifact_descendant(self):
        final = self.commit(f"{self.artifacts}/report.json")
        self.evidence["commit"] = final
        result = self.import_result()
        self.assertEqual(result["commit"], final)
        self.assertTrue(
            review_matches(self.repo, result, self.plan["jobs"][0], self.plan)
        )
        self.assertEqual(result["reviewed_commit"], self.head)

    def test_review_matches_when_the_job_state_records_the_gitdir_of_its_worktree(self):
        # ``Runner`` records ``gitdir`` (a ``worktrees/<name>`` directory) in every job state that has a worktree,
        # and the review check runs in the main checkout, whose git directory is another one.
        final = self.commit(f"{self.artifacts}/report.json")
        tree = self.root / "tree"
        self.git("worktree", "add", "-b", "job-tree", str(tree), self.head)
        recorded = gitsafe.resolve_gitdir(self.repo, tree)
        self.assertNotEqual(recorded, gitsafe.resolve_gitdir(self.repo, self.repo))
        self.assertIsNone(gitsafe.trusted(self.repo))  # a cold process: nothing hides the recorded value
        state = {
            "commit": final,
            "reviewed_commit": self.head,
            "review": {"pass": True, "findings": []},
            "gitdir": recorded,
        }
        self.assertTrue(review_matches(self.repo, state, self.plan["jobs"][0], self.plan))

    def test_source_edit_and_revert_cannot_masquerade_as_artifacts(self):
        self.commit("source.py", "new behavior\n")
        self.evidence["commit"] = self.commit("source.py", "original\n")
        with self.assertRaisesRegex(ValueError, "outside assigned"):
            self.import_result()

    def test_symlink_artifact_is_rejected_even_after_removal(self):
        link = self.repo / self.artifacts / "link"
        link.parent.mkdir(parents=True)
        link.symlink_to(self.repo / "source.py")
        self.git("add", ".")
        self.git("commit", "-m", "symlink")
        link.unlink()
        self.git("add", ".")
        self.git("commit", "-m", "remove symlink")
        self.evidence["commit"] = self.git("rev-parse", "HEAD")
        with self.assertRaisesRegex(ValueError, "non-executable"):
            self.import_result()

    def test_manifest_rejects_wrong_job_stale_head_and_missing_evidence(self):
        for key, value in (
            ("job_id", "other"),
            ("commit", "0" * 40),
            ("tested_commit", None),
            ("assertions", []),
            ("recording_url", "http://example.invalid/demo"),
        ):
            with self.subTest(key=key):
                original = self.evidence[key]
                self.evidence[key] = value
                with self.assertRaises(ValueError):
                    self.import_result()
                self.evidence[key] = original

    def test_traversal_root_and_symlink_dirty_report_rejected(self):
        self.plan["jobs"][0]["artifact_paths"] = [
            Path("plans", "archive", "..", "..", "source.py").as_posix()
        ]
        self.evidence["commit"] = self.commit("source.py", "different\n")
        with self.assertRaisesRegex(ValueError, "traverse"):
            self.import_result()

    def test_import_checks_existing_pr_exact_head(self):
        self.state.update("job", pr="https://github.com/example/repo/pull/1")
        from factory import recovery

        real_command = recovery.command

        def command(args, cwd):
            if args[0] == "gh":
                return json.dumps(
                    {
                        "headRefOid": "0" * 40,
                        "headRefName": "factory-job",
                        "baseRefName": "main",
                        "state": "OPEN",
                    }
                )
            return real_command(args, cwd)

        with (
            patch("factory.recovery.command", side_effect=command),
            self.assertRaisesRegex(ValueError, "PR head"),
        ):
            self.import_result()

    def test_execution_validation_allows_other_workers_but_not_own_live_child(self):
        self.state.data["jobs"]["other"] = {"status": "running", "pid": 456}
        self.state.update("job", status="waiting_verify")
        with patch("factory.state.alive", side_effect=lambda pid: bool(pid)):
            validate_resume(
                self.repo, self.plan, self.state, "job", "verify", execution=True
            )
            with self.assertRaisesRegex(ValueError, "still alive"):
                self.resume()
            self.state.update("job", pid=123)
            with self.assertRaisesRegex(ValueError, "still alive"):
                validate_resume(
                    self.repo, self.plan, self.state, "job", "verify", execution=True
                )

    def test_live_child_prevents_recovery(self):
        self.state.update("job", pid=123)
        with (
            patch("factory.state.alive", return_value=True),
            self.assertRaisesRegex(ValueError, "still alive"),
        ):
            self.resume()


if __name__ == "__main__":
    unittest.main()

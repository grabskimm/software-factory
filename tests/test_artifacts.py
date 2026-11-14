import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from factory.artifacts import finalize_verification
from factory.config import RepoConfig
from factory.runner import Runner
from tests.test_factory import assignment


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.git("init", "-b", "main")
        self.git("config", "user.email", "factory@example.invalid")
        self.git("config", "user.name", "Factory Test")
        (self.repo / "source.ts").write_text("original behavior\n")
        self.git("add", ".")
        self.git("commit", "-m", "reviewed behavior")
        self.tested = self.git("rev-parse", "HEAD")
        self.directory = Path("plans", "archive", "factory-test-fix")
        (self.repo / self.directory).mkdir(parents=True)
        self.recording = (self.directory / "recording sample.webm").as_posix()
        self.job = {
            "id": "fix",
            "artifact_paths": [
                Path("plans", "active", "factory-test-fix").as_posix(),
                self.directory.as_posix(),
                "scripts/recordings/factory-test-fix.ts",
            ],
        }
        self.evidence = {
            "complete": True,
            "commit": self.tested,
            "scenario": "Open app, exercise behavior, assert result",
            "environment": "local web and staging services",
            "assertions": ["Expected result displayed"],
            "recording_path": self.recording,
        }
        self.slug = patch(
            "factory.artifacts.command", return_value="example/actual-repo"
        )
        self.slug.start()
        self.addCleanup(self.slug.stop)

    def git(self, *args, cwd=None):
        return subprocess.run(
            ["git", *args],
            cwd=cwd or self.repo,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.rstrip("\n")

    def finalize(self):
        return finalize_verification(
            self.repo,
            self.repo,
            self.job,
            self.evidence,
            self.tested,
            self.git,
            plan={"artifact_roots": ["plans/active", "plans/archive"], "jobs": [self.job]},
        )

    def video(self):
        (self.repo / self.recording).write_bytes(b"inspected recording fixture")

    def test_publishes_only_artifacts_and_preserves_tested_revision(self):
        self.video()
        (self.repo / self.directory / "result.json").write_text('{"passed":true}\n')
        (self.repo / self.directory / "scenario.ts").write_text(
            "// recorded scenario\n"
        )
        evidence, final_commit = self.finalize()
        self.assertNotEqual(final_commit, self.tested)
        self.assertEqual(self.git("rev-parse", "HEAD^"), self.tested)
        self.assertEqual(evidence["tested_commit"], self.tested)
        self.assertEqual(evidence["commit"], final_commit)
        self.assertEqual(evidence["artifact_commit"], final_commit)
        self.assertEqual(
            evidence["recording_url"],
            f"https://github.com/example/actual-repo/blob/{final_commit}/"
            f"{self.directory.as_posix()}/recording%20sample.webm",
        )
        self.assertEqual(
            self.git("diff", self.tested, final_commit, "--", "source.ts"), ""
        )
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertGreater(
            int(self.git("cat-file", "-s", f"{final_commit}:{self.recording}")), 0
        )

    def test_source_edit_is_rejected_without_committing_any_artifacts(self):
        self.video()
        (self.repo / "source.ts").write_text("changed behavior\n")
        with self.assertRaisesRegex(ValueError, "outside assigned plan"):
            self.finalize()
        self.assertEqual(self.git("rev-parse", "HEAD"), self.tested)

    def test_staged_source_edit_cannot_hide_behind_restored_worktree(self):
        self.video()
        (self.repo / "source.ts").write_text("staged behavior\n")
        self.git("add", "source.ts")
        (self.repo / "source.ts").write_text("original behavior\n")
        self.assertEqual(self.git("diff", "HEAD", "--", "source.ts"), "")
        with self.assertRaisesRegex(ValueError, "outside assigned plan"):
            self.finalize()
        self.assertEqual(self.git("rev-parse", "HEAD"), self.tested)

    def test_script_artifact_path_is_not_a_writable_plan_directory(self):
        self.video()
        script = self.repo / self.job["artifact_paths"][2]
        script.parent.mkdir(parents=True)
        script.write_text("changed runtime script\n")
        with self.assertRaisesRegex(ValueError, "outside assigned plan"):
            self.finalize()

    def test_rejects_traversal_absolute_and_unassigned_recordings(self):
        self.video()
        for path in [
            (self.directory / "../../../source.ts").as_posix(),
            str(self.repo / self.recording),
            Path("plans", "archive", "another-job", "recording.webm").as_posix(),
        ]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.evidence["recording_path"] = path
                self.finalize()
        self.assertEqual(self.git("rev-parse", "HEAD"), self.tested)

    def test_rejects_symlinks_in_recording_and_supporting_artifacts(self):
        recording = self.repo / self.recording
        recording.symlink_to(self.repo / "source.ts")
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.finalize()
        recording.unlink()
        self.video()
        (self.repo / self.directory / "still.png").symlink_to(self.repo / "source.ts")
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.finalize()

    def test_rejects_symlinked_parent_directory(self):
        linked = self.repo / self.directory / "linked"
        linked.symlink_to(self.repo, target_is_directory=True)
        self.evidence["recording_path"] = (
            self.directory / "linked/source.ts"
        ).as_posix()
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.finalize()

    def test_rejects_missing_empty_or_incomplete_recordings(self):
        with self.assertRaisesRegex(ValueError, "nonempty regular"):
            self.finalize()
        (self.repo / self.recording).touch()
        with self.assertRaisesRegex(ValueError, "nonempty regular"):
            self.finalize()
        self.video()
        self.evidence["complete"] = False
        with self.assertRaisesRegex(ValueError, "completed verification"):
            self.finalize()
        self.assertEqual(self.git("rev-parse", "HEAD"), self.tested)

    def test_existing_https_evidence_can_publish_assigned_report(self):
        self.evidence.pop("recording_path")
        self.evidence["recording_url"] = "https://example.invalid/recording.mp4"
        evidence, final_commit = self.finalize()
        self.assertEqual(final_commit, self.tested)
        self.assertEqual(evidence, self.evidence)
        (self.repo / self.directory / "result.json").write_text("{}")
        evidence, final_commit = self.finalize()
        self.assertNotEqual(final_commit, self.tested)
        self.assertEqual(evidence["recording_url"], self.evidence["recording_url"])
        self.assertEqual(evidence["tested_commit"], self.tested)

    def test_incomplete_report_retains_real_blocker_without_recording(self):
        self.evidence = {
            "complete": False,
            "commit": self.tested,
            "blocker": "Windows host unavailable",
        }
        (self.repo / self.directory / "result.json").write_text('{"complete":false}')
        evidence, final_commit = self.finalize()
        self.assertNotEqual(final_commit, self.tested)
        self.assertFalse(evidence["complete"])
        self.assertEqual(evidence["blocker"], "Windows host unavailable")
        self.assertEqual(evidence["tested_commit"], self.tested)
        self.assertNotIn("recording_url", evidence)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_incomplete_report_cannot_publish_source_edits(self):
        self.evidence = {
            "complete": False,
            "commit": self.tested,
            "blocker": "browser unavailable",
        }
        (self.repo / "source.ts").write_text("changed source")
        with self.assertRaisesRegex(ValueError, "outside assigned"):
            self.finalize()
        self.assertEqual(self.git("rev-parse", "HEAD"), self.tested)

    def test_rejects_verifier_commit_before_publishing(self):
        self.video()
        self.git("add", ".")
        self.git("commit", "-m", "unexpected verifier commit")
        with self.assertRaisesRegex(ValueError, "changed HEAD"):
            self.finalize()

    def test_runner_publishes_artifact_head_and_retains_reviewed_behavior_sha(self):
        origin = self.repo / "origin.git"
        self.git("init", "--bare", str(origin))
        self.git("remote", "add", "origin", str(origin))
        self.git("push", "-u", "origin", "main")
        run_dir = self.repo / "pilot"
        run_dir.mkdir()
        plan = assignment()
        plan["jobs"][0].update(feature=True, artifact_paths=self.job["artifact_paths"])

        def invoke(key, phase, prompt, cwd, writable=False):
            (run_dir / key).mkdir(exist_ok=True)
            if phase.startswith("build"):
                (cwd / "source.ts").write_text("fixed behavior\n")
                return {
                    "blocked": False,
                    "summary": "Fix behavior",
                    "feature": True,
                    "tests": ["focused test passed"],
                }
            if phase.startswith("review"):
                return {"pass": True, "findings": []}
            (cwd / self.directory).mkdir(parents=True)
            (cwd / self.recording).write_bytes(b"inspected recording fixture")
            return {**self.evidence, "commit": self.git("rev-parse", "HEAD", cwd=cwd)}

        runner = Runner(
            self.repo, run_dir, plan, cfg=RepoConfig(acceptance_mode="recording")
        )
        with (
            patch.object(runner, "invoke", side_effect=invoke),
            patch.object(runner, "lookup_pr", return_value=None),
            patch(
                "factory.runner.command", return_value="https://example.invalid/pr/1"
            ),
            patch("factory.github.command"),
        ):
            runner.run()
        state = runner.state.data["jobs"]["fix-a"]
        self.assertEqual(state["status"], "pr_ready", state.get("error"))
        self.assertEqual(state["commit"], state["verification"]["commit"])
        self.assertEqual(
            state["reviewed_commit"], state["verification"]["tested_commit"]
        )
        self.assertNotEqual(state["commit"], state["reviewed_commit"])
        self.assertEqual(
            self.git("rev-parse", f"origin/{state['branch']}"), state["commit"]
        )
        self.assertIn(state["commit"], state["verification"]["recording_url"])


if __name__ == "__main__":
    unittest.main()

"""Manual plans get only uniquely assigned artifact folders, never broad source access."""

import copy
import unittest
from pathlib import PurePosixPath

from factory.artifact_paths import artifact_roots
from factory.artifacts import finalize_verification
from factory.model import validate_plan
from factory.recovery import review_matches
from tests import test_artifacts, test_recovery
from tests.test_factory import assignment


def plan_path(*parts):
    return "/".join(("plans", *parts))


class RootTests(unittest.TestCase):
    def setUp(self):
        self.job = {
            "id": "job",
            "likely_paths": [
                "src",
                plan_path("active", "job"),
                plan_path("archive", "job"),
            ],
        }
        self.plan = {"artifact_roots": ["plans/active", "plans/archive"], "jobs": [self.job]}

    def roots(self):
        return [str(root) for root in artifact_roots(self.job, self.plan)]

    def test_missing_field_derives_exact_owned_roots_without_plan_mutation(self):
        before = copy.deepcopy(self.plan)
        self.assertEqual(
            self.roots(), [plan_path("active", "job"), plan_path("archive", "job")]
        )
        self.assertEqual(self.plan, before)

    def test_explicit_empty_denies_and_explicit_roots_never_expand(self):
        self.job["artifact_paths"] = []
        self.assertEqual(self.roots(), [])
        self.job["artifact_paths"] = [
            plan_path("archive", "job", "evidence"),
            "scripts/scenario.ts",
        ]
        self.assertEqual(self.roots(), [plan_path("archive", "job", "evidence")])

    def test_shared_ancestor_descendant_and_explicit_other_roots_excluded(self):
        for other in [
            {"likely_paths": [plan_path("active", "job")]},
            {"likely_paths": [plan_path("active")]},
            {"likely_paths": [plan_path("active", "job", "report.md")]},
            {
                "likely_paths": [],
                "artifact_paths": [plan_path("active", "job", "evidence")],
            },
        ]:
            with self.subTest(other=other):
                self.plan["jobs"] = [self.job, {"id": "other", **other}]
                self.assertEqual(self.roots(), [plan_path("archive", "job")])
        self.plan["jobs"] = [self.job, {"id": "other", "likely_paths": ["."]}]
        self.assertEqual(self.roots(), [])

    def test_shared_architecture_folder_denied_but_job_folders_retained(self):
        shared = plan_path("active", "native-cli-shared")
        jobs = [
            {
                "id": key,
                "likely_paths": [
                    shared,
                    plan_path("active", f"native-cli-{key}"),
                    plan_path("archive", f"native-cli-{key}"),
                ],
            }
            for key in ("a00", "r01")
        ]
        plan = {"artifact_roots": ["plans/active", "plans/archive"], "jobs": jobs}
        for job in jobs:
            self.assertEqual(
                artifact_roots(job, plan),
                [
                    PurePosixPath("plans", "active", f"native-cli-{job['id']}"),
                    PurePosixPath("plans", "archive", f"native-cli-{job['id']}"),
                ],
            )

    def test_broad_nonplan_and_file_assignments_never_become_roots(self):
        self.job["likely_paths"] = [
            ".",
            "plans",
            plan_path("active"),
            plan_path("active", "README.md"),
            "src/folder",
            plan_path("active", "job", "report.md"),
            plan_path("active", "job", "subfolder"),
        ]
        self.assertEqual(self.roots(), [])

    def test_non_normalized_or_pattern_paths_and_invalid_explicit_type_rejected(self):
        for value in [
            plan_path("active", "job", ""),
            "./" + plan_path("active", "job"),
            plan_path("", "active", "job"),
            plan_path("active", "..", "job"),
            "/" + plan_path("active", "job"),
            plan_path("active", "*"),
            "plans\\active\\job",
        ]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.job["likely_paths"] = [value]
                self.roots()
        self.job["artifact_paths"] = None
        with self.assertRaisesRegex(ValueError, "must be a list"):
            self.roots()

    def test_inference_requires_unchanged_job_in_supplied_plan(self):
        with self.assertRaisesRegex(ValueError, "exact job"):
            artifact_roots(self.job, {"artifact_roots": ["plans/active", "plans/archive"], "jobs": []})
        with self.assertRaisesRegex(ValueError, "exact job"):
            artifact_roots(
                self.job | {"likely_paths": [plan_path("active", "foreign")]}, self.plan
            )

    def test_validate_plan_rejects_invalid_artifact_declaration_without_rewriting_it(
        self,
    ):
        plan = assignment()
        plan["jobs"][0]["artifact_paths"] = None
        before = copy.deepcopy(plan)
        with self.assertRaisesRegex(ValueError, "must be a list"):
            validate_plan(plan)
        self.assertEqual(plan, before)


class InferredArtifactTests(unittest.TestCase):
    git = test_artifacts.ArtifactTests.git

    def setUp(self):
        test_artifacts.ArtifactTests.setUp(self)
        self.job.pop("artifact_paths")
        self.job["likely_paths"] = [str(self.directory)]
        self.plan = {"artifact_roots": ["plans/active", "plans/archive"], "jobs": [self.job]}
        self.evidence = {
            "complete": False,
            "commit": self.tested,
            "blocker": "Windows host unavailable",
        }

    def finalize(self):
        return finalize_verification(
            self.repo,
            self.repo,
            self.job,
            self.evidence,
            self.tested,
            self.git,
            plan=self.plan,
        )

    def test_owned_incomplete_report_commits_preserving_review_and_blocker(self):
        before = copy.deepcopy(self.plan)
        report = self.repo / self.directory / "result.json"
        report.write_text('{"complete":false}')
        result, head = self.finalize()
        self.assertNotEqual(head, self.tested)
        self.assertEqual(result["tested_commit"], self.tested)
        self.assertFalse(result["complete"])
        self.assertEqual(result["blocker"], self.evidence["blocker"])
        self.assertTrue(
            review_matches(
                self.repo,
                {
                    "commit": head,
                    "reviewed_commit": self.tested,
                    "review": {"pass": True, "findings": []},
                },
                self.job,
                self.plan,
            )
        )
        self.assertEqual(self.plan, before)

    def test_source_foreign_and_shared_changes_rejected_before_commit(self):
        for value in [
            "source.ts",
            plan_path("archive", "foreign", "report.md"),
            str(self.directory / "report.md"),
        ]:
            path = self.repo / value
            path.parent.mkdir(parents=True, exist_ok=True)
            original = path.read_bytes() if path.exists() else None
            path.write_text("not allowed")
            if value == str(self.directory / "report.md"):
                self.plan["jobs"].append(
                    {"id": "other", "likely_paths": [str(self.directory / "child")]}
                )
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "outside assigned"
            ):
                self.finalize()
            self.assertEqual(self.git("rev-parse", "HEAD"), self.tested)
            if original is None:
                path.unlink()
            else:
                path.write_bytes(original)

    def test_symlink_and_executable_artifacts_rejected(self):
        path = self.repo / self.directory / "report.md"
        path.symlink_to(self.repo / "source.ts")
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.finalize()
        path.unlink()
        path.write_text("report")
        path.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "non-executable"):
            self.finalize()
        self.assertEqual(self.git("rev-parse", "HEAD"), self.tested)

    def test_explicit_empty_denies_inferred_folder(self):
        self.job["artifact_paths"] = []
        (self.repo / self.directory / "report.md").write_text("report")
        with self.assertRaisesRegex(ValueError, "outside assigned"):
            self.finalize()

    def test_staged_executable_mode_cannot_hide_behind_non_executable_worktree(self):
        path = self.repo / self.directory / "report.md"
        path.write_text("report")
        self.git("add", ".")
        self.git("update-index", "--chmod=+x", str(self.directory / "report.md"))
        self.git("config", "core.filemode", "false")
        with self.assertRaisesRegex(ValueError, "non-executable"):
            self.finalize()
        self.assertEqual(self.git("rev-parse", "HEAD"), self.tested)


class InferredRecoveryTests(unittest.TestCase):
    git = test_recovery.RecoveryTests.git
    commit = test_recovery.RecoveryTests.commit
    resume = test_recovery.RecoveryTests.resume
    import_result = test_recovery.RecoveryTests.import_result

    def setUp(self):
        test_recovery.RecoveryTests.setUp(self)
        self.plan["jobs"][0].pop("artifact_paths")
        self.plan["jobs"][0]["likely_paths"] = [self.artifacts]

    def test_resume_and_import_agree_on_owned_artifact_descendants(self):
        path = self.repo / self.artifacts / "report.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"complete":false}')
        self.resume()
        self.evidence["commit"] = self.commit(f"{self.artifacts}/report.json")
        self.evidence.update(complete=False, blocker="Windows host unavailable")
        result = self.import_result()
        self.assertFalse(result["verification"]["complete"])
        self.assertEqual(result["reviewed_commit"], self.head)
        self.assertTrue(
            review_matches(self.repo, result, self.plan["jobs"][0], self.plan)
        )

    def test_shared_root_denies_dirty_resume_and_committed_import(self):
        self.plan["jobs"].append({"id": "other", "likely_paths": [self.artifacts]})
        path = self.repo / self.artifacts / "report.json"
        path.parent.mkdir(parents=True)
        path.write_text("{}")
        with self.assertRaisesRegex(ValueError, "outside assigned"):
            self.resume()
        self.evidence["commit"] = self.commit(f"{self.artifacts}/report.json")
        with self.assertRaisesRegex(ValueError, "outside assigned"):
            self.import_result()

    def test_executable_history_rejected_even_after_removal(self):
        path = self.repo / self.artifacts / "report.json"
        path.parent.mkdir(parents=True)
        path.write_text("{}")
        path.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "non-executable"):
            self.resume()
        self.git("add", ".")
        self.git("commit", "-m", "executable artifact")
        path.unlink()
        self.git("add", ".")
        self.git("commit", "-m", "remove artifact")
        self.evidence["commit"] = self.git("rev-parse", "HEAD")
        with self.assertRaisesRegex(ValueError, "non-executable"):
            self.import_result()

    def test_explicit_empty_denies_recovery_from_owned_likely_folder(self):
        self.plan["jobs"][0]["artifact_paths"] = []
        self.evidence["commit"] = self.commit(f"{self.artifacts}/report.json")
        with self.assertRaisesRegex(ValueError, "outside assigned"):
            self.import_result()

    test_symlink_history_rejected_even_after_removal = (
        test_recovery.RecoveryTests.test_symlink_artifact_is_rejected_even_after_removal
    )


if __name__ == "__main__":
    unittest.main()

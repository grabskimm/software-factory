"""Issues added by a plan fragment are labelled at ingestion (best effort): a job is planned, an investigation blocked."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from factory.issue_labels import LABEL_BLOCKED, LABEL_PLANNED
from factory.runner import Runner
from tests.test_factory import assignment
from tests.test_plan_fragments import fragment, job


class FragmentPlannedLabelTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.inbox = self.directory / "inbox"
        self.inbox.mkdir(mode=0o700)
        plan = assignment()
        (self.directory / "plan.json").write_text(json.dumps(plan))
        self.runner = Runner(self.directory, self.directory, plan)
        self.log = io.StringIO()

    def drop(self, name, document):
        temp = self.inbox / f".{name}.tmp"
        temp.write_bytes(json.dumps(document).encode())
        os.replace(temp, self.inbox / f"fragment-{name}.json")

    def tick(self):
        with contextlib.redirect_stderr(self.log):
            self.runner.ingest_fragments()

    def inbox_names(self):
        return sorted(p.name for p in self.inbox.iterdir())

    def test_new_issues_are_labelled_planned_after_the_state_holds_the_jobs(self):
        seen = []

        def record(wanted):
            seen.append(
                (
                    dict(wanted),
                    sorted(self.runner.state.data["jobs"]),
                    [j["id"] for j in self.runner.plan["jobs"]],
                )
            )

        self.drop("one", fragment(job("fix-b", 2), job("fix-c", 3)))
        with patch.object(self.runner.issue_labels, "sync", side_effect=record):
            self.tick()
        self.assertEqual(
            seen,
            [
                (
                    {2: LABEL_PLANNED, 3: LABEL_PLANNED},
                    ["fix-a", "fix-b", "fix-c"],
                    ["fix-a", "fix-b", "fix-c"],
                )
            ],
        )
        self.assertEqual(self.inbox_names(), ["fragment-one.json.applied"])

    def test_an_investigation_in_a_fragment_is_blocked_and_only_the_job_is_planned(self):
        seen = []
        self.drop(
            "one",
            fragment(
                job("fix-b", 2),
                dispositions=[{"issue": 3, "status": "investigation", "reason": "Which flag should it use?"}],
                issues=[{"number": n, "title": f"Issue {n}", "body": "body"} for n in (2, 3)],
            ),
        )
        with patch.object(self.runner.issue_labels, "sync", side_effect=lambda wanted: seen.append(dict(wanted))):
            self.tick()
        self.assertEqual(seen, [{2: LABEL_PLANNED, 3: LABEL_BLOCKED}])
        self.assertEqual(self.inbox_names(), ["fragment-one.json.applied"])

    def test_labels_are_written_through_the_fenced_path(self):
        self.drop("one", fragment(job("fix-b", 2)))
        written = []
        labels = self.runner.issue_labels
        with (
            patch.object(self.runner, "assert_held") as held,
            patch.object(labels, "fence", held),
            patch.object(labels, "_ensure"),
            patch.object(labels, "_current", return_value=None),
            patch.object(labels, "_write", side_effect=lambda n, label: written.append((n, label))),
        ):
            self.tick()
        self.assertEqual(written, [(2, LABEL_PLANNED)])
        held.assert_called()

    def test_a_raising_label_sync_does_not_fail_the_ingestion(self):
        self.drop("one", fragment(job("fix-b", 2)))
        with patch.object(self.runner.issue_labels, "sync", side_effect=RuntimeError("boom")):
            self.tick()
        self.assertIn("decision=applied jobs=fix-b reason=ok", self.log.getvalue())
        self.assertEqual(self.inbox_names(), ["fragment-one.json.applied"])
        self.assertEqual([j["id"] for j in self.runner.plan["jobs"]], ["fix-a", "fix-b"])
        self.assertIn("fix-b", self.runner.jobs)
        self.assertEqual(self.runner.state.data["jobs"]["fix-b"]["status"], "queued")
        self.assertEqual(self.runner.issue_labels.owned["fix-b"], [2])
        on_disk = json.loads((self.directory / "plan.json").read_text())
        self.assertEqual([j["id"] for j in on_disk["jobs"]], ["fix-a", "fix-b"])

    def test_a_github_failure_does_not_fail_the_ingestion(self):
        self.drop("one", fragment(job("fix-b", 2)))
        labels = self.runner.issue_labels
        with (
            patch.object(labels, "_ensure", side_effect=OSError("github down")),
            patch.object(labels, "_current", side_effect=OSError("github down")),
            patch.object(labels, "_write", side_effect=OSError("github down")),
            patch.object(labels, "_gh", side_effect=OSError("github down")),
        ):
            self.tick()
        self.assertIn("decision=applied jobs=fix-b reason=ok", self.log.getvalue())
        self.assertEqual(self.inbox_names(), ["fragment-one.json.applied"])
        self.assertEqual(self.runner.state.data["jobs"]["fix-b"]["status"], "queued")
        self.assertEqual([j["id"] for j in self.runner.plan["jobs"]], ["fix-a", "fix-b"])

    def test_a_quarantined_fragment_triggers_no_label_sync(self):
        # issue 1 already belongs to fix-a: the fragment is refused whole
        self.drop("one", fragment(job("fix-b", 1)))
        with patch.object(self.runner.issue_labels, "sync") as sync:
            self.tick()
        sync.assert_not_called()
        self.assertEqual(self.inbox_names(), ["fragment-one.json.rejected"])

    def test_a_failed_state_write_triggers_no_label_sync(self):
        self.drop("one", fragment(job("fix-b", 2)))
        with (
            patch.object(self.runner.state, "add_jobs", side_effect=OSError("disk")),
            patch.object(self.runner.issue_labels, "sync") as sync,
        ):
            self.tick()
        sync.assert_not_called()
        self.assertIn("reason=commit-failed", self.log.getvalue())
        self.assertEqual(self.inbox_names(), ["fragment-one.json"])


if __name__ == "__main__":
    unittest.main()

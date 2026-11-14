"""``run --plan-file`` after a plan fragment was applied accepts the originally imported plan."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from factory import cli, fragments
from factory.runner import Runner
from tests.test_factory import assignment
from tests.test_plan_fragments import job


class PlanFileAfterFragmentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.file = self.directory / "import.json"
        self.original = assignment()
        self.original["issues"].append({"number": 3, "title": "Z", "body": "z"})
        self.original["jobs"].append(
            {**self.original["jobs"][0], "id": "fix-z", "issues": [3]}
        )
        self.original["dispositions"].append(
            {"issue": 3, "status": "job", "job": "fix-z", "reason": "x"}
        )
        self.file.write_text(json.dumps(self.original))
        self.run_dir = self.directory / "run"
        self.run_dir.mkdir()
        self.plan_path = self.run_dir / "plan.json"

    def load(self, document=None):
        if document is not None:
            self.file.write_text(json.dumps(document))
        args = SimpleNamespace(command="run", plan_file=self.file)
        return cli.load_plan(args, self.run_dir)

    def apply_fragment(self):
        plan = self.load()
        merged = fragments.merge(
            plan, "fragment-one.json", "0" * 64, [{"number": 2, "title": "B", "body": "b"}],
            [job("fix-b", 2)], 10,
        )
        Runner(self.run_dir, self.run_dir, plan).commit_fragment(merged)
        return merged

    def test_import_is_accepted_after_a_fragment_and_plan_is_kept(self):
        merged = self.apply_fragment()
        live = self.plan_path.read_text()
        plan = self.load()
        self.assertEqual(plan, json.loads(live))
        self.assertEqual(plan["jobs"], merged["jobs"])
        self.assertEqual(self.plan_path.read_text(), live)
        saved = json.loads(live)
        self.assertEqual([j["id"] for j in saved["jobs"]], ["fix-a", "fix-z", "fix-b"])
        self.assertEqual([i["number"] for i in saved["issues"]], [1, 3, 2])
        self.assertEqual(len(saved["dispositions"]), 3)
        self.assertEqual(saved["fragments"][0]["jobs"], ["fix-b"])

    def test_any_other_difference_is_refused_after_a_fragment(self):
        self.apply_fragment()
        changes = {
            "objective": lambda p: p["jobs"][0].update(objective="Other"),
            "paths": lambda p: p["jobs"][0].update(likely_paths=["docs"]),
            "extra job": lambda p: (
                p["issues"].append({"number": 4, "title": "C", "body": "c"}),
                p["jobs"].append({**p["jobs"][0], "id": "fix-c", "issues": [4]}),
                p["dispositions"].append(
                    {"issue": 4, "status": "job", "job": "fix-c", "reason": "x"}
                ),
            ),
            "missing job": lambda p: p.update(
                jobs=p["jobs"][:1], issues=p["issues"][:1], dispositions=p["dispositions"][:1]
            ),
            "fragment job folded in": lambda p: (
                p["issues"].append({"number": 2, "title": "B", "body": "b"}),
                p["jobs"].append({**p["jobs"][0], "id": "fix-b", "issues": [2]}),
                p["dispositions"].append(
                    {"issue": 2, "status": "job", "job": "fix-b", "reason": "x"}
                ),
            ),
        }
        live = self.plan_path.read_text()
        for name, change in changes.items():
            document = copy.deepcopy(self.original)
            change(document)
            with self.subTest(name), self.assertRaisesRegex(ValueError, "different plan"):
                self.load(document)
            self.assertEqual(self.plan_path.read_text(), live)

    def test_without_fragments_behaviour_is_unchanged(self):
        self.load()
        self.assertEqual(self.load(), self.original)
        other = copy.deepcopy(self.original)
        other["jobs"][0]["objective"] = "Other"
        with self.assertRaisesRegex(ValueError, "different plan"):
            self.load(other)
        self.assertEqual(json.loads(self.plan_path.read_text()), self.original)

    def test_digest_pin_still_guards_the_live_plan(self):
        self.apply_fragment()
        self.load()
        pin = (self.run_dir / "plan.sha256").read_text()
        self.assertEqual(pin, fragments.plan_digest(json.loads(self.plan_path.read_text())))
        live = json.loads(self.plan_path.read_text())
        live["jobs"][0]["objective"] = "Edited"
        self.plan_path.write_text(json.dumps(live))
        with self.assertRaisesRegex(ValueError, "plan changed after execution began"):
            cli.load_plan(SimpleNamespace(command="recover", plan_file=None), self.run_dir)
        with self.assertRaisesRegex(ValueError, "different plan"):
            self.load()


if __name__ == "__main__":
    unittest.main()

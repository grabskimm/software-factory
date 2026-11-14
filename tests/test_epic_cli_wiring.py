"""Epic phases, slice 2 of #30: the three call sites in ``cli.py`` that make the links work end to end.

The helpers are tested one by one elsewhere (``tests/test_epic_links.py``); these cases run the commands themselves, so that a call
site that stopped passing the helper on is caught: ``plan_command`` (the stamp of a phase issue and the nested-epic block),
``append_command`` (the same for an appended issue) and ``reconcile_command`` (the live parent link of a completion).
"""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from factory import cli, epic, epic_phases, fragments
from factory.config import RepoConfig
from factory.epic import PhaseLink
from factory.epic_gate import PLAN_APPROVED
from factory.issue_comments import DEFAULT_TRIGGER_LABEL
from tests.test_epic import disposition
from tests.test_epic_links import dispositions_for, job, stamp_for
from tests.test_epic_phases import REPO, PhaseCase, PhaseReader
from tests.test_plan_append import RESULT_B, AppendCase
from tests.test_plan_issues import entry
from tests.test_reconcile import HEAD, ReconcileCase, body_of, plan_for


class PlanCommandTests(PhaseCase):
    """``cli.plan_command`` over a captured issue list that holds phase issues of Factory (a stub planner, the real planning code)."""

    def setUp(self):
        super().setUp()
        self.reader.add_child(1, 1)  # a phase issue the planner will call an epic
        self.reader.add_child(2, 2)  # one a person labelled for the run: planned as a job

    def captured(self, numbers):
        label = {"name": DEFAULT_TRIGGER_LABEL}
        return [
            {"number": n, "title": "T", "body": self.reader.children[n]["body"] if n in self.reader.children else "b", "comments": [], "labels": [label]}
            for n in numbers
        ]

    def plan(self, answer, cfg, numbers=(1, 2, 3)):
        work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        directory = work / "run"
        directory.mkdir()
        (directory / "issues.json").write_text(json.dumps(self.captured(numbers)))
        (directory / "open-prs.json").write_text("[]")
        args = SimpleNamespace(issues=None, limit=None, timeout=10, use_snapshot=True)
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(cli, "planner_worktree", return_value=work))
            stack.enter_context(patch.object(cli, "share_planner_tree"))
            stack.enter_context(patch.object(cli, "label_authority", return_value=lambda number, labels: True))
            stack.enter_context(patch.object(cli, "IssueLabels"))
            stack.enter_context(patch.object(cli, "ensure_choice_labels"))
            stack.enter_context(patch.object(cli, "commenter_for"))
            stack.enter_context(patch("factory.history.recorder_for", return_value=None))
            stack.enter_context(patch("factory.planner.run_agent", lambda *a, **k: (answer, {"phase": "plan"})))
            stack.enter_context(patch("factory.planner.repo_guidance", return_value=""))
            stack.enter_context(patch("factory.trigger.GithubReader", return_value=self.reader))
            stack.enter_context(patch("factory.shell.get_auth", return_value=None))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            cli.plan_command(args, cfg, work, directory, None)
        return json.loads((directory / "plan.json").read_text())

    def answer(self):
        a, c = job("a", [2]), job("c", [3])
        return {"version": 1, "jobs": [a, c], "dispositions": [disposition(1), *dispositions_for([a, c])]}

    def test_the_plan_command_blocks_a_nested_epic_and_records_the_stamp_of_a_phase_job(self):
        answer = self.answer()
        written = self.plan(answer, RepoConfig(repo=REPO))
        reasons = {item["issue"]: (item["status"], item["reason"]) for item in written["dispositions"]}
        self.assertEqual(reasons[1], ("blocked", epic.NESTED_REASON))
        by_id = {item["id"]: item for item in written["jobs"]}
        self.assertEqual(by_id["a"]["epic_phase"], stamp_for(child=2, phase=2, digest=self.reader.digest()))
        self.assertNotIn("epic_phase", by_id["c"])
        self.assertEqual(self.reader.calls.count("phase_issue"), 2)  # the two that look like phases are read, the plain issue is not

    def test_where_epic_mode_is_off_nothing_is_a_phase_and_nothing_is_read(self):
        answer = self.answer()
        written = self.plan(answer, RepoConfig(repo=REPO, epic_mode=False))
        self.assertTrue(all("epic_phase" not in item for item in written["jobs"]))
        self.assertEqual({item["issue"]: item["status"] for item in written["dispositions"]}[1], "blocked")  # today's block, not the nested one
        self.assertEqual(self.reader.calls, [])


class AppendCommandTests(AppendCase):
    """``cli.append_command`` (``plan --append-to``): an appended phase issue gets its stamp, and an epic for one is blocked."""

    def setUp(self):
        super().setUp()
        self.phases = PhaseReader()
        self.phases.scene(7, PLAN_APPROVED, epic.LABEL_APPROVE)
        self.phases.add_child(2, 2)
        self.serve(n1=entry("one"), n2=entry("two", epic.LABEL_PHASE), n3=entry("three"))

    def appended(self):
        with patch("factory.trigger.GithubReader", return_value=self.phases), patch("factory.shell.get_auth", return_value=None):
            self.append(issues=(2,))
        (path,) = self.fragments()
        _, jobs, dispositions = fragments.parse(path.read_bytes(), 10)
        return jobs, dispositions

    def test_an_appended_phase_issue_carries_its_stamp(self):
        jobs, _ = self.appended()
        (item,) = jobs
        self.assertEqual(item["epic_phase"], stamp_for(child=2, phase=2, digest=self.phases.digest()))
        self.assertEqual(self.phases.calls.count("phase_issue"), 1)

    def test_an_epic_for_an_appended_phase_issue_is_blocked_with_the_fixed_reason(self):
        self.result = {"version": 1, "jobs": [], "dispositions": [disposition(2)]}
        jobs, dispositions = self.appended()
        self.assertEqual(jobs, [])
        self.assertEqual([(item["issue"], item["status"], item["reason"]) for item in dispositions], [(2, "blocked", epic.NESTED_REASON)])

    def test_an_appended_plain_issue_has_no_stamp_and_asks_nothing(self):
        self.result = RESULT_B
        self.serve(n1=entry("one"), n2=entry("two"), n3=entry("three"))
        jobs, _ = self.appended()
        self.assertNotIn("epic_phase", jobs[0])
        self.assertEqual(self.phases.calls, [])


class ReconcileLinkTests(ReconcileCase):
    """``cli.reconcile_command``: the completion of an incomplete phase pull request writes the parent link proven live NOW."""

    def setUp(self):
        super().setUp()
        plan = plan_for(("fix", [202]))
        plan["jobs"][0]["epic_phase"] = stamp_for()
        self.world.pull(7, head=HEAD, body=body_of("Refs #202, refs #7"))
        self.world.label_pull(7)
        self.world.data["issues"] = {"202": ["factory:pr-open"]}
        self.directory = self.run_dir("r1", plan, {"fix": ("pr_incomplete", 7, HEAD)})

    def first_line(self, link):
        with patch.object(epic_phases, "phase_link", return_value=link) as live:
            self.reconcile(self.directory)
        [body] = self.world.writes("body")
        return body[2].split("\n", 1)[0], live

    def test_the_completion_writes_the_line_of_the_live_link(self):
        line, live = self.first_line(PhaseLink(7, 202, True))
        self.assertEqual(line, "Closes #202, closes #7")
        (called,) = live.call_args_list
        found, reader, settings, active = called.args
        self.assertEqual((found["epic_phase"], type(reader).__name__, settings.enabled, active), (stamp_for(), "GithubReader", True, True))

    def test_a_link_that_does_not_hold_leaves_the_parent_as_a_reference(self):
        line, _ = self.first_line(PhaseLink(7, 202, False))
        self.assertEqual(line, "Closes #202, refs #7")

    def test_where_epic_mode_is_off_the_link_is_asked_as_inactive(self):
        self.cfg = RepoConfig(repo="org/name", epic_mode=False)
        _, live = self.first_line(PhaseLink(7, 202, False))
        self.assertEqual(live.call_args.args[3], False)


if __name__ == "__main__":
    unittest.main()

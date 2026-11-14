"""Hand-off to GitHub: ready PR, Closes #N, labels, factory/gate status, lock fencing, auth."""

import contextlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from factory import checks_ran, epic, epic_phases, permissions
from factory.agent import run_agent
from factory.config import RepoConfig
from factory.epic import PhaseLink
from factory.github import TITLE_LIMIT, pr_title
from factory.github_auth import GitHubAuth
from factory.issue_labels import STATUS_LABELS
from factory.locks import LockLost
from factory.model import criteria
from factory.operator_items import operator_items, ticked_ids
from factory.runner import Runner, ensure_labels
from factory.shell import using_auth
from factory.status import status_report

from tests import test_factory as fixtures
from tests.test_factory import assignment

TOKEN = "fake-installation-token-for-tests-only"


def from_issue_labels():
    """True when the caller is the issue-label sync, whose fence checks are not counted."""
    frame = sys._getframe(2)
    while frame is not None:
        if frame.f_globals.get("__name__") == "factory.issue_labels":
            return True
        frame = frame.f_back
    return False


def is_label_sync(call):
    """A gh call that only maintains the factory:* status labels on issues."""
    args = call["args"]
    if args[:1] == ["api"]:
        return any("/issues/" in a for a in args)
    return args[:2] == ["label", "create"] and args[2] in STATUS_LABELS


class RecordingLock:
    """A RepoLock stand-in: counts assert_held calls and can start failing.

    The issue-label sync checks the lock too, but those checks are not counted (the tests
    below count the fenced steps of publishing); they fail once the next counted check would.
    """

    def __init__(self, fail_after=None):
        self.calls = 0
        self.fail_after = fail_after

    def assert_held(self):
        if from_issue_labels():
            if self.fail_after is not None and self.calls >= self.fail_after:
                raise LockLost("lease lost")
            return
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise LockLost("lease lost")


class FakeAppAuth(GitHubAuth):
    """What AppAuth injects: a token for git/gh only, plus the bot commit identity."""

    def env_for_supervisor_subprocess(self):
        return {
            "GH_TOKEN": TOKEN,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": '!f(){ echo username=x-access-token; echo "password=$GH_TOKEN"; }; f',
        }

    def commit_identity(self):
        return "factory-app[bot]", "42+factory-app[bot]@users.noreply.github.com"

    def git_identity_args(self):
        name, email = self.commit_identity()
        return ["-c", f"user.name={name}", "-c", f"user.email={email}"]


class HandoffTests(unittest.TestCase):
    setUp = fixtures.IntegrationTests.setUp
    git = fixtures.IntegrationTests.git
    script = fixtures.IntegrationTests.script
    install_agent = fixtures.IntegrationTests.install_agent
    gh_calls = fixtures.IntegrationTests.gh_calls
    gate_statuses = fixtures.IntegrationTests.gate_statuses

    def runner(self, cfg=None, plan=None, **kwargs):
        cfg = cfg or RepoConfig(repo="org/name")
        return Runner(self.repo, self.run_dir, plan or assignment(), timeout=10, cfg=cfg, **kwargs)

    def calls(self, *prefix):
        return [c for c in self.gh_calls() if c["args"][: len(prefix)] == list(prefix)]

    def reject_review(self):
        """Builder works; every review fails, so the job ends incomplete."""
        self.install_agent(
            """
prompt = sys.stdin.read()
if 'independent reviewer' in prompt:
    emit({'pass': False, 'findings': ['still wrong']})
else:
    pathlib.Path('src/a.txt').write_text('fixed\\n')
    emit({'summary': 'Fix A', 'blocked': False, 'blocker': '', 'tests': ['t'], 'feature': False})
"""
        )

    # ---- ready PR
    def test_ready_pr_closes_issues_carries_labels_and_passes_the_gate_at_the_exact_head(self):
        runner = self.runner()
        runner.run()
        state = runner.state.data["jobs"]["fix-a"]
        self.assertEqual(state["status"], "pr_ready", state.get("error"))
        create = self.calls("pr", "create")[0]
        args = create["args"]
        self.assertNotIn("--draft", args)
        self.assertEqual(args[args.index("--base") + 1], "main")
        self.assertEqual(args[args.index("-R") + 1], "org/name")
        labels = [args[i + 1] for i, a in enumerate(args) if a == "--label"]
        self.assertEqual(labels, ["factory:ready"])
        self.assertIn("Closes #1", create["body"])
        self.assertNotIn("Refs #1", create["body"])
        self.assertNotIn("Opt-in merging", create["body"])
        statuses = self.gate_statuses()
        sha = state["commit"]
        self.assertEqual([s[0] for s in statuses], ["pending", "success"])
        self.assertEqual(statuses[-1][1], f"review+verify passed @{sha[:7]}")
        self.assertTrue(all(s[2] == f"repos/org/name/statuses/{sha}" for s in statuses))
        # the statuses are posted with the gate context and nothing else is merged or closed
        flat = " ".join(" ".join(c["args"]) for c in self.gh_calls())
        for forbidden in ("pr merge", "--auto", "--squash", "issue close"):
            self.assertNotIn(forbidden, flat)

    def test_context_issues_are_referenced_not_closed(self):
        plan = assignment()
        plan["issues"].append({"number": 2, "title": "Context", "body": "ctx"})
        plan["jobs"][0]["context_issues"] = [2]
        plan["dispositions"].append({"issue": 2, "status": "covered", "reason": "context"})
        self.runner(plan=plan).run()
        body = self.calls("pr", "create")[0]["body"]
        self.assertIn("Closes #1", body)
        self.assertIn("Refs #2", body)
        self.assertNotIn("Closes #2", body)

    # ---- criteria proven elsewhere never make a job ready by themselves
    def publish_with(self, acceptance, required=()):
        plan = assignment()
        plan["jobs"][0]["acceptance"] = acceptance
        with patch("factory.runner.base_required_checks", return_value=set(required)):
            runner = self.runner(plan=plan)
            runner.run()
        return runner.state.data["jobs"]["fix-a"]

    def test_operator_criterion_with_complete_verification_is_ready_with_a_checklist(self):
        state = self.publish_with(["plain", {"text": "prod behaviour", "tag": "operator"}])
        self.assertEqual(state["status"], "pr_ready", state.get("error"))
        create = self.calls("pr", "create")[0]
        self.assertNotIn("--draft", create["args"])
        self.assertIn("factory:ready", create["args"])
        self.assertIn("Closes #1", create["body"])
        self.assertNotIn("Outstanding criteria", create["body"])
        self.assertNotIn("criteria proven elsewhere are outstanding", create["body"])
        self.assertIn("- [operator] prod behaviour", create["body"])
        self.assertEqual([s[0] for s in self.gate_statuses()], ["pending", "success"])

    # ---- the parent link of a phase issue (epic mode, #30)
    def test_publish_writes_the_parent_link_of_a_phase_issue_into_the_first_line(self):
        with patch.object(Runner, "phase_link", return_value=PhaseLink(7, 1, False)):
            state = self.publish_with(["plain"])
        self.assertEqual(state["status"], "pr_ready", state.get("error"))
        self.assertEqual(self.calls("pr", "create")[0]["body"].splitlines()[0], "Closes #1, refs #7")

    def test_publish_of_the_last_phase_closes_the_parent_when_the_live_proof_holds(self):
        plan = assignment()
        plan["jobs"][0]["epic_phase"] = epic.phase_stamp(7, 1, 1, 1, "ab" * 32)
        seen = []

        def proof(job, reader, settings, active):
            seen.append((job["id"], active))
            return PhaseLink(7, 1, True)

        with patch.object(epic_phases, "phase_link", proof), patch("factory.runner.base_required_checks", return_value=set()):
            runner = self.runner(plan=plan)
            runner.run()
        self.assertEqual(runner.state.data["jobs"]["fix-a"]["status"], "pr_ready")
        self.assertEqual(self.calls("pr", "create")[0]["body"].splitlines()[0], "Closes #1, closes #7")
        self.assertEqual(seen, [("fix-a", True)])

    def test_publish_of_an_ordinary_job_asks_no_proof_and_keeps_its_line(self):
        with patch.object(epic_phases, "phase_link", side_effect=AssertionError("not a phase")):
            state = self.publish_with(["plain"])
        self.assertEqual(state["status"], "pr_ready", state.get("error"))
        self.assertEqual(self.calls("pr", "create")[0]["body"].splitlines()[0], "Closes #1")

    def assert_not_ready(self, criterion):
        state = self.publish_with(["plain", criterion], required={"other"})
        self.assertEqual(state["status"], "pr_incomplete", state.get("error"))
        self.assertEqual([s[0] for s in self.gate_statuses()], ["pending", "failure"])

    def test_workflow_only_ci_criterion_is_not_ready(self):
        self.assert_not_ready({"text": "flow", "tag": "ci", "workflow": "release"})

    def test_ci_criterion_naming_a_check_that_is_not_required_is_not_ready(self):
        self.assert_not_ready({"text": "scan", "tag": "ci", "checks": ["trivy"]})

    def test_ci_criterion_covered_by_required_checks_is_ready_but_the_gate_stays_pending(self):
        state = self.publish_with(
            ["plain", {"text": "scan", "tag": "ci", "checks": ["trivy"]}], required={"trivy", "x"}
        )
        self.assertEqual(state["status"], "pr_ready", state.get("error"))
        statuses = self.gate_statuses()
        sha = state["commit"]
        self.assertEqual([s[0] for s in statuses], ["pending", "pending"])
        self.assertEqual(statuses[-1][1], f"awaiting required CI: trivy @{sha[:7]} [ci_pending]")
        self.assertTrue(all(s[2] == f"repos/org/name/statuses/{sha}" for s in statuses))
        body = self.calls("pr", "create")[0]["body"]
        self.assertIn("Closes #1", body)
        self.assertIn("stays pending until the required checks", body)

    def test_the_body_says_where_each_check_ran(self):
        state = self.publish_with(
            [
                "plain",
                {"text": "scan", "tag": "ci", "checks": ["trivy"]},
                {"text": "prod behaviour", "tag": "operator", "reason": "needs production"},
            ],
            required={"trivy", "x"},
        )
        self.assertEqual(state["status"], "pr_ready", state.get("error"))
        body = self.calls("pr", "create")[0]["body"]
        self.assertEqual(body.count(checks_ran.HEADING), 1)
        section = body.split(checks_ran.HEADING, 1)[1].split("```json", 1)[0]
        self.assertIn("- **Sandbox** (a worker session", section)
        self.assertIn("- **CI** (the base branch's required checks", section)
        self.assertIn("awaiting on this head: `trivy`", section)
        self.assertIn("`factory/gate` stays pending until they pass", section)
        self.assertIn("- **Operator**", section)
        self.assertIn("Factory proved none of them", section)
        self.assertIn("prod behaviour", section)
        # inside the Verification section, ahead of the verdict dump; the checklist is still the last block
        self.assertLess(body.index("## Verification"), body.index(checks_ran.HEADING))
        self.assertEqual(body.count(checks_ran.CI_BEGIN), 1)
        self.assertEqual(body.count("\n## Needs a person\n"), 1)
        self.assertGreater(body.index("\n## Needs a person\n"), body.index(checks_ran.CI_END))
        self.assertEqual(state["verification"]["complete"], True)

    def test_a_draft_body_says_the_gate_does_not_wait_for_ci(self):
        self.assert_not_ready({"text": "scan", "tag": "ci", "checks": ["trivy"]})
        body = self.calls("pr", "create")[0]["body"]
        self.assertIn("does not wait for CI on this head", body)
        self.assertNotIn("awaiting on this head", body)

    def test_a_job_with_no_ci_criterion_says_the_gate_waits_for_no_check(self):
        state = self.publish_with(["plain"], required={"x"})
        self.assertEqual(state["status"], "pr_ready", state.get("error"))
        body = self.calls("pr", "create")[0]["body"]
        self.assertIn("waits for no check here, because no `ci` criterion relies on one", body)
        self.assertIn("no criterion of this job needs one", body)

    def test_ci_criterion_without_any_required_check_is_unproven_and_never_pending(self):
        state = self.publish_with(["plain", {"text": "scan", "tag": "ci", "checks": ["trivy"]}])
        self.assertEqual(state["status"], "pr_incomplete", state.get("error"))
        statuses = self.gate_statuses()
        self.assertEqual([s[0] for s in statuses], ["pending", "failure"])
        self.assertIn("criteria proven elsewhere are outstanding", statuses[-1][1])
        create = self.calls("pr", "create")[0]
        self.assertIn("--draft", create["args"])
        outstanding = create["body"].split("Outstanding criteria")[1]
        self.assertIn("- [ci] scan (checks: trivy)", outstanding)
        # only a ci criterion can be outstanding now: the sentence must not name a person
        lead = outstanding.split("\n")[0]
        self.assertIn("no required check of the base branch proves", lead)
        self.assertNotIn("person", lead)

    def test_an_unproven_ci_criterion_beside_an_operator_one_is_a_draft_that_never_lists_the_operator_one(self):
        state = self.publish_with(
            [
                "plain",
                {"text": "scan", "tag": "ci", "checks": ["trivy"]},
                {"text": "prod behaviour", "tag": "operator", "reason": "needs production"},
            ],
            required={"other"},
        )
        self.assertEqual(state["status"], "pr_incomplete", state.get("error"))
        statuses = self.gate_statuses()
        self.assertEqual([s[0] for s in statuses], ["pending", "failure"])
        self.assertIn("criteria proven elsewhere are outstanding", statuses[-1][1])
        create = self.calls("pr", "create")[0]
        self.assertIn("--draft", create["args"])
        self.assertNotIn("factory:ready", create["args"])
        self.assertTrue(create["body"].startswith("Refs #1"))
        outstanding, checklist = create["body"].split("Outstanding criteria")[1].split("## Needs a person")
        self.assertIn("- [ci] scan (checks: trivy)", outstanding)
        self.assertNotIn("prod behaviour", outstanding)
        self.assertNotIn("[operator]", outstanding)
        # the operator criterion is still on the person's checklist, with its reason
        self.assertIn("- [ ] prod behaviour (why: needs production)", checklist)

    def test_publish_records_the_required_checks_it_judged_for_status_to_compare(self):
        criterion = {"text": "scan", "tag": "ci", "checks": ["trivy"]}
        state = self.publish_with(["plain", criterion], required={"x", "trivy"})
        self.assertEqual(state["published_required"], ["trivy", "x"])

    def test_publish_records_an_empty_required_list_rather_than_leaving_it_out(self):
        state = self.publish_with(["plain", {"text": "scan", "tag": "ci", "checks": ["trivy"]}])
        self.assertEqual(state["published_required"], [])
        self.assertEqual(state["status"], "pr_incomplete")

    def test_a_pr_that_lists_a_check_publish_did_not_judge_required_is_unproven_in_status(self):
        criterion = {"text": "scan", "tag": "ci", "checks": ["trivy"]}
        runner_state = self.publish_with(["plain", criterion], required={"trivy"})
        plan = assignment()
        plan["jobs"][0]["acceptance"] = ["plain", criterion]
        data = {"version": 1, "jobs": {"fix-a": runner_state}}
        agree = {"state": "OPEN", "requiredChecks": [{"name": "trivy", "bucket": "pass"}]}
        row = status_report(plan, data, pr_snapshots={"fix-a": agree})["jobs"][0]
        self.assertEqual(row["criteria"][1]["proof"], "satisfied")
        differ = {"state": "OPEN", "requiredChecks": [{"name": "build", "bucket": "pass"}]}
        row = status_report(plan, data, pr_snapshots={"fix-a": differ})["jobs"][0]
        # not listed on the PR yet is not a disagreement: unproven, and no claim about the PR's rules
        self.assertEqual(row["criteria"][1]["proof"], "unproven")
        self.assertNotIn("required_sources", row["criteria"][1])
        listed = {"state": "OPEN", "requiredChecks": [{"name": "trivy", "bucket": "pass"}]}
        data = {"version": 1, "jobs": {"fix-a": {**runner_state, "published_required": ["other"]}}}
        row = status_report(plan, data, pr_snapshots={"fix-a": listed})["jobs"][0]
        self.assertEqual(row["criteria"][1]["proof"], "unproven")
        self.assertEqual(row["criteria"][1]["required_sources"], {"publish": False, "pr": True})

    def test_a_plain_job_still_gets_success_when_required_checks_exist(self):
        state = self.publish_with(["plain"], required={"trivy"})
        self.assertEqual(state["status"], "pr_ready", state.get("error"))
        self.assertEqual([s[0] for s in self.gate_statuses()], ["pending", "success"])

    # ---- incomplete PR
    def test_incomplete_pr_is_a_draft_that_cannot_pass_the_gate(self):
        self.reject_review()
        runner = Runner(
            self.repo, self.run_dir, assignment(), timeout=10, review_rounds=0, cfg=RepoConfig(repo="org/name")
        )
        runner.run()
        state = runner.state.data["jobs"]["fix-a"]
        self.assertEqual(state["status"], "pr_incomplete", state.get("error"))
        args = self.calls("pr", "create")[0]["args"]
        self.assertIn("--draft", args)
        labels = [args[i + 1] for i, a in enumerate(args) if a == "--label"]
        self.assertEqual(labels, [])
        body = self.calls("pr", "create")[0]["body"]
        self.assertIn("Refs #1", body)
        self.assertNotIn("Closes #1", body)
        self.assertEqual([s[0] for s in self.gate_statuses()], ["pending", "failure"])

    def test_incomplete_outcome_carries_its_reason_code_in_gate_pr_body_state_and_log(self):
        self.reject_review()
        runner = Runner(
            self.repo, self.run_dir, assignment(), timeout=10, review_rounds=0, cfg=RepoConfig(repo="org/name")
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            runner.run()
        state = runner.state.data["jobs"]["fix-a"]
        self.assertEqual(state["reason_code"], "real_failure")
        failure = self.gate_statuses()[-1]
        self.assertEqual(failure[0], "failure")
        self.assertEqual(
            failure[1], f"incomplete: review or verification did not pass @{state['commit'][:7]} [real_failure]"
        )
        self.assertLessEqual(len(failure[1]), 140)
        body = self.calls("pr", "create")[0]["body"]
        self.assertEqual(body.count("**Reason code**: `real_failure`"), 1)
        lines = [l for l in out.getvalue().splitlines() if l.startswith("factory: reason_code ")]
        self.assertEqual(
            lines,
            [
                f"factory: reason_code job=fix-a issues=1 commit={state['commit'][:7]} code=real_failure "
                "review_findings=1 new_failures=0 env_failures=0 outstanding=0 awaited=0"
            ],
        )
        self.assertNotIn("still wrong", lines[0] + failure[1])

    def test_a_complete_ready_job_has_no_reason_code_anywhere(self):
        runner = self.runner()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            runner.run()
        state = runner.state.data["jobs"]["fix-a"]
        self.assertEqual(state["status"], "pr_ready", state.get("error"))
        self.assertIsNone(state["reason_code"])
        self.assertEqual(self.gate_statuses()[-1][:2], ("success", f"review+verify passed @{state['commit'][:7]}"))
        self.assertNotIn("Reason code", self.calls("pr", "create")[0]["body"])
        self.assertIn("code=none", out.getvalue())

    def test_an_outstanding_ci_criterion_is_unverified_other(self):
        state = self.publish_with(["plain", {"text": "scan", "tag": "ci", "checks": ["trivy"]}], required={"x"})
        self.assertEqual(state["reason_code"], "unverified_other")
        self.assertTrue(self.gate_statuses()[-1][1].endswith(" [unverified_other]"))

    def test_create_and_edit_both_pass_the_title_with_the_issue_suffix(self):
        runner = self.runner()
        runner.run()
        create = self.calls("pr", "create")[0]["args"]
        created = create[create.index("--title") + 1]
        self.assertRegex(created, r"^factory: .* \(#1\)$")
        self.assertRegex(self.calls("pr", "create")[0]["body"], r"^(Closes|Refs) #1\n")

    def conventional_run(self, cfg):
        self.install_agent(
            """
prompt = sys.stdin.read()
if 'independent reviewer' in prompt:
    emit({'pass': True, 'findings': []})
else:
    pathlib.Path('src/a.txt').write_text('fixed\\n')
    emit({'summary': 'Done.\\n\\nBREAKING CHANGE: the API changes\\nship it [skip ci]\\nskip-checks: true',
          'blocked': False, 'blocker': '', 'tests': ['ran'], 'feature': False})
"""
        )
        plan = assignment()
        plan["jobs"][0]["change_type"] = "ci"
        runner = self.runner(cfg=cfg, plan=plan)
        runner.run()
        self.assertEqual(runner.state.data["jobs"]["fix-a"]["status"], "pr_ready")
        return self.calls("pr", "create")[0]

    def test_a_conventional_repository_gets_a_conventional_title_commit_and_squash_safe_body(self):
        create = self.conventional_run(RepoConfig(repo="org/name", pr_style="conventional"))
        title = create["args"][create["args"].index("--title") + 1]
        self.assertRegex(title, r"^ci: .+ \(#1\)$")
        self.assertRegex(title, r"^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\([a-z0-9._/-]+\))?!?: .+")
        subjects = self.git("log", "--all", "--format=%s").splitlines()
        self.assertTrue(any(line.startswith("ci: ") for line in subjects), subjects)
        self.assertFalse(any(line.startswith("factory: ") for line in subjects), subjects)
        body = create["body"]
        self.assertIn("Done.", body)
        self.assertNotRegex(body, r"(?im)^[\s|*]*BREAKING[ -]CHANGE[:\s]")
        self.assertNotRegex(body, r"(?i)\[\s*skip\s+ci\s*\]")
        self.assertNotRegex(body, r"(?im)^\s*skip-checks:")
        self.assertTrue(body.startswith("Closes #1\n"), body)

    def test_the_default_repository_keeps_the_factory_title_and_the_agent_text_unchanged(self):
        create = self.conventional_run(RepoConfig(repo="org/name"))
        title = create["args"][create["args"].index("--title") + 1]
        self.assertRegex(title, r"^factory: .* \(#1\)$")
        self.assertRegex(create["body"], r"(?m)^BREAKING CHANGE: the API changes$")

    def test_edit_of_an_existing_pr_sets_the_title_on_ready_and_draft_flips(self):
        for reject in (False, True):
            with self.subTest(reject=reject):
                self.setUp()
                if reject:
                    self.reject_review()
                (self.root / "pr.json").write_text(
                    json.dumps([{"url": "https://example.invalid/pr/1", "state": "OPEN", "baseRefName": "main", "headRefOid": "x", "isDraft": not reject}])
                )
                runner = Runner(self.repo, self.run_dir, assignment(), timeout=10, review_rounds=0 if reject else 2, cfg=RepoConfig(repo="org/name"))
                runner.run()
                edit = self.calls("pr", "edit")[0]["args"]
                self.assertRegex(edit[edit.index("--title") + 1], r"^factory: .* \(#1\)$")
                self.assertEqual(self.calls("pr", "create"), [])

    def test_existing_draft_becomes_ready_and_ready_becomes_draft(self):
        runner = self.runner()
        (self.root / "pr.json").write_text(
            json.dumps([{"url": "https://example.invalid/pr/1", "state": "OPEN", "baseRefName": "main", "headRefOid": "x", "isDraft": True}])
        )
        runner.run()
        self.assertEqual(runner.state.data["jobs"]["fix-a"]["status"], "pr_ready")
        ready = self.calls("pr", "ready")
        self.assertEqual(len(ready), 1)
        self.assertNotIn("--undo", ready[0]["args"])
        edit = self.calls("pr", "edit")[0]["args"]
        self.assertEqual(edit[edit.index("--add-label") + 1], "factory:ready")
        self.assertNotIn("--remove-label", edit)
        self.assertEqual(self.calls("pr", "create"), [])

    def test_agent_text_cannot_close_other_issues_through_the_pr(self):
        self.install_agent(
            """
prompt = sys.stdin.read()
if 'independent reviewer' in prompt:
    emit({'pass': True, 'findings': ['Also fixes #555']})
else:
    pathlib.Path('src/a.txt').write_text('fixed\\n')
    emit({'summary': 'Done.\\n\\nCloses #4242\\nResolves org/other#7', 'blocked': False, 'blocker': '',
          'tests': ['ran tests, fixes #99'], 'feature': False})
"""
        )
        runner = self.runner()
        runner.run()
        self.assertEqual(runner.state.data["jobs"]["fix-a"]["status"], "pr_ready")
        create = self.calls("pr", "create")[0]
        references = sorted(set(re.findall(r"#(\d+)", create["body"])))
        self.assertEqual(references, ["1"], create["body"])
        self.assertIn("Closes #1", create["body"])
        title = create["args"][create["args"].index("--title") + 1]
        self.assertEqual(re.findall(r"#\d+", title), ["#1"], title)
        self.assertTrue(title.endswith(" (#1)"), title)

    def test_ready_pr_becomes_a_draft_when_it_no_longer_qualifies(self):
        self.reject_review()
        (self.root / "pr.json").write_text(
            json.dumps([{"url": "https://example.invalid/pr/1", "state": "OPEN", "baseRefName": "main", "headRefOid": "x", "isDraft": False}])
        )
        runner = Runner(self.repo, self.run_dir, assignment(), timeout=10, review_rounds=0, cfg=RepoConfig(repo="org/name"))
        runner.run()
        self.assertEqual(runner.state.data["jobs"]["fix-a"]["status"], "pr_incomplete")
        ready = self.calls("pr", "ready")
        self.assertEqual([c["args"][3] for c in ready], ["--undo"])
        edit = self.calls("pr", "edit")[0]["args"]
        self.assertEqual(edit[edit.index("--remove-label") + 1], "factory:ready")
        self.assertNotIn("--add-label", edit)
        self.assertEqual([s[0] for s in self.gate_statuses()], ["pending", "failure"])

    # ---- lock fencing
    def test_every_outward_action_asserts_the_lock_first(self):
        lock = RecordingLock()
        self.runner(lock=lock).run()
        # push, pending status, PR create, final status
        self.assertGreaterEqual(lock.calls, 4)

    def test_lost_lock_means_no_push_no_status_no_pr(self):
        lock = RecordingLock(fail_after=0)
        runner = self.runner(lock=lock)
        runner.run()
        state = runner.state.data["jobs"]["fix-a"]
        self.assertEqual(state["status"], "failed")
        self.assertIn("lease lost", state["error"])
        origin = self.root / "origin.git"
        branches = subprocess.run(["git", "branch", "--list", "factory/*"], cwd=origin, capture_output=True, text=True).stdout
        self.assertEqual(branches.strip(), "")
        self.assertEqual(self.gh_calls(), [])

    def test_losing_the_lock_at_any_point_stops_every_later_outward_action(self):
        """The Nth assert_held fails: nothing guarded by it, or after it, may happen."""
        # Fenced steps of publishing a new ready PR, in order:
        #   1 push   2 pending status   3 labels   4 PR create   5 final status
        origin = self.root / "origin.git"
        for allowed in range(0, 6):
            with self.subTest(assertions_allowed=allowed):
                pr = self.root / "pr.json"
                if pr.exists():
                    pr.unlink()
                before = len(self.gh_calls())
                runner = Runner(
                    self.repo, self.root / f"lost{allowed}", assignment(), timeout=10,
                    cfg=RepoConfig(repo="org/name"), lock=RecordingLock(fail_after=allowed),
                )
                runner.run()
                state = runner.state.data["jobs"]["fix-a"]
                kinds = [tuple(c["args"][:2]) for c in self.gh_calls()[before:] if not is_label_sync(c)]
                statuses = [s[0] for s in self.gate_statuses()[len([k for k in self.gate_statuses()]) - kinds.count(("api", "-X")):]]
                pushed = subprocess.run(
                    ["git", "branch", "--list", f"factory/lost{allowed}/*"],
                    cwd=origin, capture_output=True, text=True,
                ).stdout.strip()
                self.assertEqual(bool(pushed), allowed >= 1, "push")
                self.assertEqual(("api", "-X") in kinds, allowed >= 2, "pending status")
                self.assertEqual(("label", "create") in kinds, allowed >= 3, "labels")
                self.assertEqual(("pr", "create") in kinds, allowed >= 4, "PR create")
                self.assertEqual(kinds.count(("api", "-X")), {0: 0, 1: 0}.get(allowed, 1 if allowed < 5 else 2), "statuses")
                if allowed < 5:
                    self.assertEqual(state["status"], "failed", state.get("error"))
                    self.assertIn("lease lost", state["error"])
                else:
                    self.assertEqual(state["status"], "pr_ready", state.get("error"))
                    self.assertEqual(statuses, ["pending", "success"])

    def test_labels_and_draft_toggles_are_fenced_too(self):
        timeline = []
        test = self

        class TimedLock(RecordingLock):
            def assert_held(self):
                super().assert_held()
                timeline.append(len(test.gh_calls()))

        (self.root / "pr.json").write_text(
            json.dumps([{"url": "u", "state": "OPEN", "baseRefName": "main", "headRefOid": "x", "isDraft": True}])
        )
        self.runner(lock=TimedLock()).run()
        calls = self.gh_calls()
        # an assertion ran with exactly this many gh calls behind it, right before the first
        # call of each mutating group (the issue-label sync has its own calls and fences)
        for group in (("label", "create"), ("pr", "edit"), ("pr", "ready")):
            first = next(
                i for i, c in enumerate(calls) if tuple(c["args"][:2]) == group and not is_label_sync(c)
            )
            self.assertIn(first, timeline, f"unfenced: {group}")

    # ---- base branch
    def test_base_branch_override_drives_worktree_base_and_pr_target(self):
        self.git("branch", "release", "main")
        self.git("push", "origin", "release")
        cfg = RepoConfig(repo="org/name", base_branch="release")
        with patch.dict(os.environ, {"FAKE_BASE": "release"}):
            runner = self.runner(cfg=cfg)
            runner.run()
        state = runner.state.data["jobs"]["fix-a"]
        self.assertEqual(state["status"], "pr_ready", state.get("error"))
        self.assertEqual(state["base"], self.git("rev-parse", "origin/release"))
        args = self.calls("pr", "create")[0]["args"]
        self.assertEqual(args[args.index("--base") + 1], "release")

    def test_existing_pr_against_another_base_is_refused(self):
        (self.root / "pr.json").write_text(
            json.dumps([{"url": "u", "state": "OPEN", "baseRefName": "dev", "headRefOid": "x", "isDraft": False}])
        )
        runner = self.runner()
        runner.run()
        state = runner.state.data["jobs"]["fix-a"]
        self.assertEqual(state["status"], "failed")
        self.assertIn("does not target main", state["error"])

    # ---- identity and credentials
    def test_token_reaches_supervisor_git_and_gh_but_never_workers_files_or_git_config(self):
        env_dump = self.binary / "worker-env.json"
        self.install_agent(
            f"""
prompt = sys.stdin.read()
pathlib.Path({str(env_dump)!r}).write_text(json.dumps(dict(os.environ)))
if 'independent reviewer' in prompt:
    emit({{'pass': True, 'findings': []}})
else:
    pathlib.Path('src/a.txt').write_text('fixed\\n')
    emit({{'summary': 'Fix A', 'blocked': False, 'blocker': '', 'tests': ['t'], 'feature': False}})
"""
        )
        with using_auth(FakeAppAuth()):
            runner = self.runner()
            runner.run()
        state = runner.state.data["jobs"]["fix-a"]
        self.assertEqual(state["status"], "pr_ready", state.get("error"))
        # supervisor-side gh got it (and a commit made by the supervisor carries the bot identity)
        self.assertTrue(self.gh_calls() and all(c["token"] == TOKEN for c in self.gh_calls()))
        author = self.git("log", "-1", "--format=%an <%ae>", state["branch"])
        self.assertEqual(author, "factory-app[bot] <42+factory-app[bot]@users.noreply.github.com>")
        # the worker's environment has no GitHub credential of any kind
        worker_env = json.loads(env_dump.read_text())
        self.assertNotIn(TOKEN, json.dumps(worker_env))
        for key in worker_env:
            self.assertFalse(key.startswith(("GH_", "GITHUB_", "GIT_CONFIG")), key)
        # nothing under the run directory contains the token
        for path in self.run_dir.rglob("*"):
            if path.is_file():
                self.assertNotIn(TOKEN.encode(), path.read_bytes(), str(path))
        # no git config (repo, worktrees, remote URLs) holds it either
        for tree in [self.repo, *Path(self.run_dir / "worktrees").iterdir()]:
            config = subprocess.run(["git", "config", "--list", "--show-origin"], cwd=tree, capture_output=True, text=True, check=True).stdout
            self.assertNotIn(TOKEN, config, str(tree))
            self.assertNotIn("credential.helper", config, str(tree))
        remotes = self.git("remote", "-v")
        self.assertNotIn(TOKEN, remotes)

    def test_worker_env_is_built_without_consulting_the_supervisor_auth(self):
        recorded = self.binary / "env.json"
        self.install_agent(
            f"pathlib.Path({str(recorded)!r}).write_text(json.dumps(sorted(os.environ)))\nemit({{'ok': True}})"
        )
        with using_auth(FakeAppAuth()):
            run_agent("p", self.repo, self.run_dir / "x", "build")
        keys = json.loads(recorded.read_text())
        self.assertFalse([k for k in keys if k.startswith(("GH_", "GIT_CONFIG", "GITHUB_"))])

    def test_ambient_auth_leaves_the_environment_alone(self):
        runner = self.runner()
        runner.run()
        ambient = os.environ.get("GH_TOKEN")
        self.assertTrue(self.gh_calls())
        self.assertTrue(all(c["token"] == ambient for c in self.gh_calls()))
        self.assertTrue(all(c["token"] != TOKEN for c in self.gh_calls()))


class VerifyPolicyTests(unittest.TestCase):
    setUp = fixtures.IntegrationTests.setUp
    git = fixtures.IntegrationTests.git
    script = fixtures.IntegrationTests.script
    install_agent = fixtures.IntegrationTests.install_agent
    gh_calls = fixtures.IntegrationTests.gh_calls

    def run_with(self, cfg, feature, verify_result):
        plan = assignment()
        plan["jobs"][0]["feature"] = feature
        phases = []
        self.runs = getattr(self, "runs", 0) + 1
        directory = self.root / f"run{self.runs}"
        directory.mkdir()
        runner = Runner(self.repo, directory, plan, timeout=10, cfg=cfg)

        def invoke(key, phase, prompt, cwd):
            phases.append(phase)
            if phase.startswith("build"):
                (cwd / "src/a.txt").write_text("implemented\n")
                return {"blocked": False, "summary": "s", "feature": feature, "tests": ["t"]}
            if phase.startswith("review"):
                return {"pass": True, "findings": []}
            return dict(verify_result, commit=self.git("-C", str(cwd), "rev-parse", "HEAD"))

        runner.invoke = invoke
        runner.run()
        return phases, runner.state.data["jobs"]["fix-a"]

    def test_verify_when_never_skips_verification_even_for_features(self):
        phases, state = self.run_with(RepoConfig(repo="o/n", verify_when="never"), True, {})
        self.assertNotIn("verify", phases)
        self.assertEqual(state["status"], "pr_ready", state.get("error"))
        # machine-readable, so `status` never shows a sandbox criterion satisfied by nothing
        self.assertIs(state["verification"]["skipped"], True)
        self.assertTrue(state["verification"]["complete"])

    def test_verify_when_always_verifies_internal_changes(self):
        ok = {"complete": True, "scenario": "ran tests", "environment": "ci", "assertions": ["passed"]}
        phases, state = self.run_with(RepoConfig(repo="o/n", verify_when="always"), False, ok)
        self.assertIn("verify", phases)
        self.assertEqual(state["status"], "pr_ready", state.get("error"))
        self.assertNotIn("skipped", state["verification"])

    def test_verify_when_feature_is_the_default(self):
        phases, _ = self.run_with(RepoConfig(repo="o/n"), False, {})
        self.assertNotIn("verify", phases)
        ok = {"complete": True, "scenario": "ran tests", "environment": "ci", "assertions": ["passed"]}
        phases, _ = self.run_with(RepoConfig(repo="o/n"), True, ok)
        self.assertIn("verify", phases)

    def test_tests_mode_needs_no_recording(self):
        ok = {"complete": True, "scenario": "ran tests", "environment": "ci", "assertions": ["passed"]}
        _, state = self.run_with(RepoConfig(repo="o/n", verify_when="always"), False, ok)
        self.assertTrue(state["verification"]["complete"])

    def test_recording_mode_requires_an_https_recording(self):
        ok = {"complete": True, "scenario": "ran tests", "environment": "ci", "assertions": ["passed"]}
        cfg = RepoConfig(repo="o/n", verify_when="always", acceptance_mode="recording")
        _, state = self.run_with(cfg, False, ok)
        self.assertFalse(state["verification"]["complete"])
        self.assertEqual(state["status"], "pr_incomplete")
        _, state = self.run_with(
            RepoConfig(repo="o/n", verify_when="always", acceptance_mode="recording"),
            False,
            dict(ok, recording_url="http://insecure.invalid/x"),
        )
        self.assertFalse(state["verification"]["complete"])


class NeedsAPersonTests(unittest.TestCase):
    """``operator`` criteria are a checklist in the PR body; they never decide anything."""

    setUp = fixtures.IntegrationTests.setUp
    git = fixtures.IntegrationTests.git
    script = fixtures.IntegrationTests.script
    install_agent = fixtures.IntegrationTests.install_agent
    gh_calls = fixtures.IntegrationTests.gh_calls
    gate_statuses = fixtures.IntegrationTests.gate_statuses
    reject_review = HandoffTests.reject_review
    calls = HandoffTests.calls

    HEADING = "\n## Needs a person\n"

    def plan(self, acceptance, feature=False):
        plan = assignment()
        plan["jobs"][0]["acceptance"] = acceptance
        plan["jobs"][0]["feature"] = feature
        return plan

    def runner(self, acceptance, **kwargs):
        cfg = kwargs.pop("cfg", RepoConfig(repo="org/name"))
        return Runner(self.repo, self.run_dir, self.plan(acceptance), timeout=10, cfg=cfg, **kwargs)

    def run_job(self, runner, required=()):
        with patch("factory.runner.base_required_checks", return_value=set(required)):
            runner.run()
        return runner.state.data["jobs"]["fix-a"]

    def section(self, body):
        return body.rsplit(self.HEADING, 1)[1]

    def tasks(self, body):
        return [line for line in self.section(body).splitlines() if line.startswith("- [")]

    def outcome(self, acceptance, required=(), reject=False):
        """Everything that decides readiness for one run in a fresh repository."""
        self.setUp()
        if reject:
            self.reject_review()
        runner = self.runner(acceptance, review_rounds=0 if reject else 2)
        state = self.run_job(runner, required)
        create = self.calls("pr", "create")[0]
        args = create["args"]
        return {
            "status": state["status"],
            "complete": state.get("verification", {}).get("complete"),
            "draft": "--draft" in args,
            "labels": [args[i + 1] for i, a in enumerate(args) if a == "--label"],
            "first_line": create["body"].splitlines()[0],
            "gates": [(g[0], g[1].replace(state["commit"][:7], "SHA")) for g in self.gate_statuses()],
        }

    # ---- rendering
    def test_operator_criteria_are_an_unchecked_checklist_with_their_reasons(self):
        runner = self.runner(
            [
                "plain",
                {"text": "corepack can fetch pnpm", "tag": "operator", "reason": "needs a pnpm repository"},
                {"text": "prod login works", "tag": "operator"},
            ]
        )
        state = self.run_job(runner)
        self.assertEqual(state["status"], "pr_ready", state.get("error"))
        body = self.calls("pr", "create")[0]["body"]
        self.assertEqual(body.splitlines()[0], "Closes #1")
        tasks = self.tasks(body)
        self.assertEqual(len(tasks), 2)
        self.assertTrue(all(task.startswith("- [ ] ") for task in tasks))
        self.assertIn("corepack can fetch pnpm (why: needs a pnpm repository)", tasks[0])
        self.assertIn("prod login works (why: Only a person can prove this.)", tasks[1])
        self.assertEqual(len(set(re.findall(r"factory:operator-item:(op-[0-9a-f]{10})", "".join(tasks)))), 2)
        self.assertNotIn("[x]", body)
        # the checklist is the last section, after everything Factory already wrote
        self.assertGreater(body.rindex(self.HEADING), body.index("## Verification"))
        self.assertTrue(body.endswith("\n".join(tasks)))

    def test_a_plan_without_operator_criteria_gets_no_checklist(self):
        self.run_job(self.runner(["plain"]))
        self.assertNotIn("Needs a person", self.calls("pr", "create")[0]["body"])

    def test_the_first_line_and_the_existing_sections_are_untouched(self):
        job = dict(assignment()["jobs"][0], acceptance=["plain", {"text": "prod", "tag": "operator"}])
        args = (
            job,
            {"commit": "a" * 40},
            {"summary": "Done", "tests": ["t"]},
            {"pass": True, "findings": []},
            {"complete": True},
            True,
        )
        shell = SimpleNamespace(directory=Path("run-1"))
        with patch("factory.runner.checklist_for", return_value=""):
            before = Runner.pr_body(shell, *args)
        after = Runner.pr_body(shell, *args)
        self.assertTrue(after.startswith(before))
        self.assertEqual(after[len(before):len(before) + 2], "\n\n")
        self.assertTrue(after[len(before) + 2:].startswith("## Needs a person\n"))
        self.assertEqual(after.splitlines()[0], "Closes #1")
        self.assertIn("## Acceptance\n\n- [sandbox] plain\n- [operator] prod\n", after)

    # ---- readiness
    def test_operator_criteria_change_nothing_about_readiness(self):
        extra = {"text": "prod behaviour", "tag": "operator", "reason": "needs production"}
        cases = {
            "ready": {"acceptance": ["plain"]},
            "ci covered: ready, gate pending": {
                "acceptance": ["plain", {"text": "scan", "tag": "ci", "checks": ["trivy"]}],
                "required": {"trivy"},
            },
            "ci not required: draft": {
                "acceptance": ["plain", {"text": "scan", "tag": "ci", "checks": ["trivy"]}],
                "required": {"other"},
            },
            "review rejected: draft": {"acceptance": ["plain"], "reject": True},
        }
        expected = {
            "ready": ("pr_ready", False),
            "ci covered: ready, gate pending": ("pr_ready", False),
            "ci not required: draft": ("pr_incomplete", True),
            "review rejected: draft": ("pr_incomplete", True),
        }
        for name, case in cases.items():
            with self.subTest(case=name):
                kwargs = {k: v for k, v in case.items() if k != "acceptance"}
                without = self.outcome(case["acceptance"], **kwargs)
                with_item = self.outcome([*case["acceptance"], extra], **kwargs)
                self.assertEqual((without["status"], without["draft"]), expected[name])
                self.assertEqual(without, with_item)

    def test_operator_items_cannot_rescue_or_fail_a_verification(self):
        ok = {"complete": True, "scenario": "ran tests", "environment": "ci", "assertions": ["passed"]}
        for verdict, status in ((ok, "pr_ready"), ({"complete": False, "blocker": "tests failed"}, "pr_incomplete")):
            with self.subTest(status=status):
                results = []
                for acceptance in (["plain"], ["plain", {"text": "prod", "tag": "operator"}]):
                    self.setUp()
                    state, _ = self.run_with_stub(acceptance, verdict)
                    results.append((state["status"], state["verification"]["complete"]))
                self.assertEqual(results[0], results[1])
                self.assertEqual(results[1][0], status)

    def test_an_operator_only_plan_is_ready_and_says_nothing_was_proven(self):
        runner = self.runner([{"text": "prod login works", "tag": "operator", "reason": "needs production"}])
        state = self.run_job(runner)
        self.assertEqual(state["status"], "pr_ready", state.get("error"))
        create = self.calls("pr", "create")[0]
        self.assertNotIn("--draft", create["args"])
        self.assertEqual(create["body"].splitlines()[0], "Closes #1")
        self.assertEqual([g[0] for g in self.gate_statuses()], ["pending", "success"])
        self.assertIn("Factory proved none of them itself", create["body"])
        self.assertEqual(len(self.tasks(create["body"])), 1)

    def test_an_operator_only_plan_is_ready_when_verification_runs_and_passes(self):
        """verify.when=always: the verifier gets no criteria at all, and a passing verdict wins."""
        ok = {"complete": True, "scenario": "ran tests", "environment": "ci", "assertions": ["suite passed"]}
        state, prompts = self.run_with_stub([{"text": "prod login works", "tag": "operator"}], ok)
        self.assertEqual(state["status"], "pr_ready", state.get("error"))
        self.assertTrue(state["verification"]["complete"])
        self.assertEqual(set(prompts), {"build", "review", "verify"})
        for phase in ("review", "verify"):
            self.assertEqual(json.loads(prompts[phase].split("\nAssignment:\n", 1)[1].splitlines()[0])["acceptance"], [])
        create = self.calls("pr", "create")[0]
        self.assertNotIn("--draft", create["args"])
        self.assertEqual(create["body"].splitlines()[0], "Closes #1")
        self.assertEqual([g[0] for g in self.gate_statuses()], ["pending", "success"])
        self.assertIn("Factory proved none of them itself", create["body"])
        self.assertEqual(len(self.tasks(create["body"])), 1)

    def test_an_operator_only_verification_must_still_report_what_it_ran(self):
        """With no criterion left in the assignment, empty assertions would make the job a draft."""
        operator = {"text": "prod login works", "tag": "operator"}
        bare = {"complete": True, "scenario": "ran tests", "environment": "ci", "assertions": []}
        state, prompts = self.run_with_stub([operator], bare)
        self.assertEqual(state["status"], "pr_incomplete")
        self.assertFalse(state["verification"]["complete"])
        self.assertIn("lacks required metadata", state["verification"]["blocker"])
        # so the verifier is told, in the very prompt that lists nothing to assert, to report its checks
        verify = prompts["verify"]
        self.assertEqual(json.loads(verify.split("\nAssignment:\n", 1)[1].splitlines()[0])["acceptance"], [])
        self.assertIn("assertions` still describe the checks you actually ran", verify)
        self.assertIn("no `assertions` as incomplete", verify)

    def test_an_operator_only_plan_still_needs_review_and_verification(self):
        self.reject_review()
        state = self.run_job(self.runner([{"text": "prod", "tag": "operator"}], review_rounds=0))
        self.assertEqual(state["status"], "pr_incomplete")
        self.assertIn("--draft", self.calls("pr", "create")[0]["args"])
        self.setUp()
        verdict = {"complete": False, "blocker": "tests failed"}
        state, _ = self.run_with_stub([{"text": "prod", "tag": "operator"}], verdict)
        self.assertEqual(state["status"], "pr_incomplete")

    # ---- rebuilding the body
    def plant_body(self, body):
        path = self.root / "pr.json"
        prs = json.loads(path.read_text())
        prs[0]["body"] = body
        path.write_text(json.dumps(prs))

    def republish(self, runner):
        runner.queue_phase("fix-a", "publish")
        return self.run_job(runner)

    def two_items(self):
        return [
            "plain",
            {"text": "first thing", "tag": "operator", "reason": "needs production"},
            {"text": "second thing", "tag": "operator"},
        ]

    def test_a_rebuilt_body_is_byte_identical(self):
        runner = self.runner(self.two_items())
        self.run_job(runner)
        created = self.calls("pr", "create")[0]["body"]
        self.plant_body(created)
        state = self.republish(runner)
        self.assertEqual(state["status"], "pr_ready", state.get("error"))
        edits = self.calls("pr", "edit")
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]["body"], created)
        self.assertEqual(len(self.calls("pr", "create")), 1)

    def test_a_persons_tick_survives_a_rebuild_and_factory_ticks_nothing(self):
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=newline):
                self.setUp()
                runner = self.runner(self.two_items())
                self.run_job(runner)
                created = self.calls("pr", "create")[0]["body"]
                self.assertNotIn("[x]", created)
                lines = created.split("\n")
                index = next(i for i, line in enumerate(lines) if "first thing" in line and line.startswith("- [ ]"))
                lines[index] = lines[index].replace("- [ ]", "- [X]", 1)
                edited = newline.join(lines)
                # an id the plan no longer has is dropped, not carried over
                edited = f"{edited}{newline}- [x] gone <!-- factory:operator-item:op-aaaaaaaaaa -->"
                self.plant_body(edited)
                self.republish(runner)
                rebuilt = self.calls("pr", "edit")[0]["body"]
                tasks = self.tasks(rebuilt)
                self.assertEqual(len(tasks), 2)
                self.assertTrue(tasks[0].startswith("- [x] first thing"), tasks)
                self.assertTrue(tasks[1].startswith("- [ ] second thing"), tasks)
                self.assertNotIn("op-aaaaaaaaaa", rebuilt)
                self.assertEqual(rebuilt.count("[x]"), 1)
                # the same body rebuilt again keeps the tick, and still adds none
                self.plant_body(rebuilt)
                self.republish(runner)
                self.assertEqual(self.calls("pr", "edit")[1]["body"], rebuilt)

    def test_a_tick_made_while_the_labels_are_prepared_is_not_lost(self):
        """The live body is read right before the edit, not before the label step."""
        runner = self.runner(self.two_items())
        self.run_job(runner)
        created = self.calls("pr", "create")[0]["body"]
        self.plant_body(created)
        real = ensure_labels
        test = self

        def person_ticks_first_item(*args, **kwargs):
            lines = created.split("\n")
            index = next(i for i, line in enumerate(lines) if "first thing" in line and line.startswith("- [ ]"))
            lines[index] = lines[index].replace("- [ ]", "- [x]", 1)
            test.plant_body("\n".join(lines))
            return real(*args, **kwargs)

        with patch("factory.runner.ensure_labels", person_ticks_first_item):
            state = self.republish(runner)
        self.assertEqual(state["status"], "pr_ready", state.get("error"))
        tasks = self.tasks(self.calls("pr", "edit")[0]["body"])
        self.assertTrue(tasks[0].startswith("- [x] first thing"), tasks)
        self.assertTrue(tasks[1].startswith("- [ ] second thing"), tasks)

    def test_nothing_on_github_sits_between_reading_the_body_and_editing_it(self):
        runner = self.runner(self.two_items())
        self.run_job(runner)
        self.plant_body(self.calls("pr", "create")[0]["body"])
        before = len(self.gh_calls())
        self.republish(runner)
        kinds = [tuple(call["args"][:2]) for call in self.gh_calls()[before:]]
        edit = kinds.index(("pr", "edit"))
        self.assertEqual(kinds[edit - 1], ("pr", "list"))
        # the label step comes before that read, not between it and the edit
        self.assertLess(kinds.index(("label", "create")), edit - 1)

    def test_a_body_with_no_checklist_or_an_unreadable_body_carries_nothing(self):
        runner = self.runner(self.two_items())
        self.run_job(runner)
        created = self.calls("pr", "create")[0]["body"]
        for planted in (None, "", "just text", 7):
            with self.subTest(body=planted):
                self.plant_body(planted)
                self.republish(runner)
                self.assertEqual(self.calls("pr", "edit")[-1]["body"], created)

    def test_a_tick_in_an_earlier_section_is_not_a_tick(self):
        runner = self.runner(self.two_items())
        self.run_job(runner)
        created = self.calls("pr", "create")[0]["body"]
        # a fully ticked copy of the checklist sits above the real, unticked one
        ticked = created.replace("- [ ]", "- [x]")
        self.assertIn("[x]", ticked)
        self.plant_body(f"{ticked}\n\n## Needs a person\n{self.section(created)}")
        self.republish(runner)
        self.assertEqual(self.calls("pr", "edit")[0]["body"], created)

    def test_a_forged_heading_with_ticks_is_not_read_once_the_real_section_is_gone(self):
        runner = self.runner(self.two_items())
        self.run_job(runner)
        created = self.calls("pr", "create")[0]["body"]
        head, section = created.rsplit(self.HEADING, 1)
        ticked = "\n".join(line.replace("- [ ]", "- [x]", 1) for line in section.split("\n"))
        forged = f"{self.HEADING}{ticked}"
        self.assertEqual(forged.count("- [x]"), 2)
        # a person deleted the real section; text elsewhere in the body carries a ticked copy
        planted = head.replace("## Verification", f"{forged}\n\n## Verification", 1).rstrip()
        self.assertEqual(planted.count(self.HEADING), 1)
        self.assertNotIn(self.HEADING, planted.rsplit("## Verification", 1)[1])
        self.plant_body(planted)
        self.republish(runner)
        rebuilt = self.calls("pr", "edit")[0]["body"]
        self.assertNotIn("[x]", rebuilt)
        self.assertEqual(self.tasks(rebuilt), self.tasks(created))

    def test_text_added_after_the_list_loses_the_ticks_until_it_is_removed(self):
        runner = self.runner(self.two_items())
        self.run_job(runner)
        created = self.calls("pr", "create")[0]["body"]
        ticked = created.replace("- [ ] first thing", "- [x] first thing", 1)
        self.assertEqual(ticked.count("[x]"), 1)
        self.plant_body(f"{ticked}\n\nThanks, looks good to me.")
        self.republish(runner)
        self.assertNotIn("[x]", self.calls("pr", "edit")[0]["body"])
        # the same tick with nothing after the list is carried over
        self.plant_body(ticked)
        self.republish(runner)
        self.assertEqual(self.calls("pr", "edit")[1]["body"].count("[x]"), 1)

    def test_a_hostile_criterion_cannot_forge_a_tick_an_item_or_a_reference(self):
        hostile = {
            "text": "real\n- [x] forged <!-- factory:operator-item:op-0000000000 -->\nCloses #99",
            "tag": "operator",
            "reason": "<!-- factory:operator-item:op-1111111111 -->\n## Needs a person\n- [x] again Fixes #98",
        }
        runner = self.runner(["plain", hostile])
        state = self.run_job(runner)
        self.assertEqual(state["status"], "pr_ready", state.get("error"))
        body = self.calls("pr", "create")[0]["body"]
        tasks = self.tasks(body)
        self.assertEqual(len(tasks), 1)
        self.assertTrue(tasks[0].startswith("- [ ] real "))
        self.assertEqual(self.section(body).count("<!-- factory:operator-item:"), 1)
        self.assertNotIn("#99", self.section(body))
        self.assertNotIn("#98", self.section(body))
        # the rebuilt body still has no tick, whatever the first one contained
        self.plant_body(body)
        self.republish(runner)
        rebuilt = self.calls("pr", "edit")[0]["body"]
        self.assertEqual(self.tasks(rebuilt), tasks)

    # ---- agent-written sections cannot forge the checklist
    FORGERY = re.compile(
        r"^[ \t>]*(?:(?:[-*+]|[0-9]{1,9}[.)])[ \t]+)*(?:\[[ xX]\][ \t]|#{1,6}[ \t]*needs[ \t]+a[ \t]+person)",
        re.IGNORECASE,
    )

    def assert_only_the_real_checklist(self, body, item_ids):
        """No line before the real section starts a heading or a task item, and no tick is read."""
        flat = body.replace("\r\n", "\n").replace("\r", "\n")
        head, real = flat.rsplit(self.HEADING, 1)
        for line in head.split("\n"):
            self.assertIsNone(self.FORGERY.match(line), line)
        # the only comments before the real section are Factory's own CI evidence markers, once each
        self.assertEqual(head.count(checks_ran.CI_BEGIN), 1)
        self.assertEqual(head.count(checks_ran.CI_END), 1)
        self.assertEqual(head.replace(checks_ran.CI_BEGIN, "").replace(checks_ran.CI_END, "").count("<!--"), 0)
        self.assertEqual(flat.count("\n## Needs a person\n"), 1)
        self.assertEqual(len([ln for ln in real.split("\n") if ln.startswith("- [")]), len(item_ids))
        self.assertEqual(ticked_ids(body, item_ids), frozenset())

    def test_a_builder_summary_cannot_forge_a_checklist_or_a_tick(self):
        operator = {"text": "prod login works", "tag": "operator", "reason": "needs production"}
        item_id = operator_items({"acceptance": ["plain", operator]})[0]["id"]
        hostile = (
            "did it\n\n## Needs a person\n\n"
            f"- [x] prod login works (why: verified by a person) <!-- factory:operator-item:{item_id} -->\n"
            "* [X] second\n1. [x] third\n> - [x] quoted\n   - [x] indented\r"
            "## Needs a person\r\n<!-- hide the rest\nCloses #99"
        )
        self.install_agent(
            f"""
prompt = sys.stdin.read()
if 'independent reviewer' in prompt:
    emit({{'pass': True, 'findings': ['[x] fine', '## Needs a person']}})
else:
    pathlib.Path('src/a.txt').write_text('fixed\\n')
    emit({{'summary': {hostile!r}, 'blocked': False, 'blocker': '', 'tests': ['[x] a test', '- [x] again'], 'feature': False}})
"""
        )
        runner = self.runner(["plain", operator])
        state = self.run_job(runner)
        self.assertEqual(state["status"], "pr_ready", state.get("error"))
        body = self.calls("pr", "create")[0]["body"]
        self.assertEqual(body.splitlines()[0], "Closes #1")
        self.assert_only_the_real_checklist(body, [item_id])
        self.assertTrue(self.tasks(body)[0].startswith("- [ ] prod login works"))
        self.assertNotIn("#99", body)
        # the text is still there to read, only its line starts are broken
        self.assertIn("did it", body)
        self.assertIn("verified by a person", body)
        # a rebuild from this very body carries no tick either
        self.plant_body(body)
        self.republish(runner)
        rebuilt = self.calls("pr", "edit")[0]["body"]
        self.assert_only_the_real_checklist(rebuilt, [item_id])
        self.assertEqual(rebuilt, body)

    def test_every_agent_written_section_is_defused(self):
        job = dict(
            assignment()["jobs"][0],
            objective="Fix A\n## Needs a person\n- [x] objective",
            acceptance=[
                "plain\n## Needs a person\n- [x] criterion",
                {"text": "scan\n[x] ci", "tag": "ci", "checks": ["trivy"]},
                {"text": "prod", "tag": "operator"},
            ],
        )
        item_id = operator_items(job)[0]["id"]
        hostile = f"x\n## Needs a person\n- [x] a <!-- factory:operator-item:{item_id} -->\n<!-- c"
        verification = {
            "complete": True,
            "recording_url": f"https://example.invalid/r\n{hostile}",
            "blocker": hostile,
            "baseline": {"status": hostile, "reason": hostile, "base": hostile, "cache": hostile},
        }
        shell = SimpleNamespace(directory=Path("run-1"))
        for ready, outstanding in ((True, ()), (False, criteria(job)[:2])):
            with self.subTest(ready=ready):
                body = Runner.pr_body(
                    shell,
                    job,
                    {"commit": "a" * 40, "additional_paths": [hostile]},
                    {"summary": hostile, "tests": [hostile, "[x] t"]},
                    {"pass": True, "findings": [hostile, "[x] f"]},
                    verification,
                    ready,
                    outstanding=outstanding,
                    awaited=[hostile.split("\n")[0]],
                )
                self.assertEqual(body.splitlines()[0], "Closes #1" if ready else "Refs #1")
                self.assert_only_the_real_checklist(body, [item_id])

    # ---- what review and verification are asked
    def run_with_stub(self, acceptance, verify_result):
        prompts = {}
        runner = self.runner(acceptance, cfg=RepoConfig(repo="org/name", verify_when="always"))

        def invoke(key, phase, prompt, cwd):
            prompts[phase.split("-")[0]] = prompt
            if phase.startswith("build"):
                (cwd / "src/a.txt").write_text("implemented\n")
                return {"blocked": False, "summary": "s", "feature": False, "tests": ["t"]}
            if phase.startswith("review"):
                return {"pass": True, "findings": []}
            return dict(verify_result, commit=self.git("-C", str(cwd), "rev-parse", "HEAD"))

        runner.invoke = invoke
        return self.run_job(runner), prompts

    def test_review_and_verify_prompts_do_not_ask_for_operator_criteria(self):
        operator = {"text": "prod login works", "tag": "operator", "reason": "needs production"}
        ci = {"text": "scan", "tag": "ci", "checks": ["trivy"]}
        ok = {"complete": True, "scenario": "ran tests", "environment": "ci", "assertions": ["passed"]}
        _, prompts = self.run_with_stub(["plain", operator, ci], ok)
        self.assertEqual(set(prompts), {"build", "review", "verify"})

        def assigned(prompt):
            return json.loads(prompt.split("\nAssignment:\n", 1)[1].splitlines()[0])["acceptance"]

        def apart(prompt):
            return json.loads(prompt.split("blocker):\n", 1)[1].splitlines()[0])

        self.assertEqual(assigned(prompts["review"]), ["plain", ci])
        self.assertEqual(assigned(prompts["verify"]), ["plain", ci])
        self.assertEqual(apart(prompts["review"]), ["prod login works"])
        self.assertEqual(apart(prompts["verify"]), ["prod login works"])
        for phase in ("review", "verify"):
            self.assertIn("never make complete false", prompts[phase])
        # the builder still sees the whole assignment and no separate list
        self.assertEqual(assigned(prompts["build"]), ["plain", operator, ci])
        self.assertNotIn("Operator-only items (", prompts["build"])

    def test_no_phase_prompt_carries_a_raw_over_long_or_misplaced_reason(self):
        huge = "X" * 50000
        operator = {"text": "prod login works", "tag": "operator", "reason": huge}
        sandbox = {"text": "tests pass", "tag": "sandbox", "reason": huge}
        ok = {"complete": True, "scenario": "ran tests", "environment": "ci", "assertions": ["passed"]}
        _, prompts = self.run_with_stub([sandbox, operator], ok)
        self.assertEqual(set(prompts), {"build", "review", "verify"})
        for phase, prompt in prompts.items():
            with self.subTest(phase=phase):
                self.assertNotIn("X" * 400, prompt)
                self.assertLess(len(prompt), 20000)

    def test_prompts_are_unchanged_for_a_plan_without_operator_criteria(self):
        ok = {"complete": True, "scenario": "ran tests", "environment": "ci", "assertions": ["passed"]}
        _, prompts = self.run_with_stub(["plain"], ok)
        for prompt in prompts.values():
            self.assertNotIn("Operator-only items (", prompt)


class WorkerIdentityTests(unittest.TestCase):
    def test_user_and_group_reach_popen_with_no_extra_groups_and_group_umask(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            captured = {}

            class Popen:
                pid = 4242
                returncode = 0

                def __init__(self, *a, **kw):
                    captured.update(kw)

                def communicate(self, *a, **kw):
                    return (b"", b"")

            (root / "work").mkdir()
            with (
                patch("factory.agent.git_common_dir", return_value=None),
                patch("factory.agent.subprocess.Popen", Popen),
                patch("factory.agent.os.killpg"),
                patch("factory.agent.permissions.os.chown"),
                patch("factory.agent.last_result", return_value={"is_error": False, "subtype": "success", "structured_output": {"ok": 1}}),
            ):
                run_agent("p", root / "work", root / "run" / "0-build-0", "build", user=10002, group=10010)
            self.assertEqual(captured["user"], 10002)
            self.assertEqual(captured["group"], 10010)
            self.assertEqual(captured["extra_groups"], [])
            self.assertEqual(captured["umask"], 0o002)

    def test_without_a_worker_identity_popen_gets_no_user_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            captured = {}

            class Popen:
                pid = 4243
                returncode = 0

                def __init__(self, *a, **kw):
                    captured.update(kw)

                def communicate(self, *a, **kw):
                    return (b"", b"")

            (root / "work").mkdir()
            with (
                patch("factory.agent.git_common_dir", return_value=None),
                patch("factory.agent.subprocess.Popen", Popen),
                patch("factory.agent.os.killpg"),
                patch("factory.agent.last_result", return_value={"is_error": False, "subtype": "success", "structured_output": {"ok": 1}}),
            ):
                run_agent("p", root / "work", root / "run" / "0-build-0", "build")
            for key in ("user", "group", "extra_groups", "umask"):
                self.assertNotIn(key, captured)

    def test_permission_helpers_set_the_documented_modes(self):
        gid = os.getgid()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "run"
            tree = root / "tree"
            (tree / "sub").mkdir(parents=True)
            run.mkdir()
            (tree / "a.txt").write_text("a")
            (tree / "sub" / "b.sh").write_text("b")
            (tree / "sub" / "b.sh").chmod(0o700)
            (tree / "link").symlink_to(run)
            secret = run / "state.json"
            secret.write_text("{}")
            permissions.traversable(run, gid)
            permissions.share_tree(tree, gid)
            mode = lambda p: stat.S_IMODE(os.lstat(p).st_mode)  # noqa: E731
            self.assertEqual(mode(run), 0o710)
            self.assertTrue(mode(tree) & 0o2770 == 0o2770)
            self.assertTrue(mode(tree / "sub") & 0o2770 == 0o2770)
            self.assertTrue(mode(tree / "a.txt") & 0o660 == 0o660)
            # group read/write added, the owner's execute bit kept
            self.assertTrue(mode(tree / "sub" / "b.sh") & 0o760 == 0o760)
            # a symlink out of the tree is never followed or changed
            self.assertEqual(mode(run), 0o710)
            job = root / "job"
            job.mkdir()
            home = job / "home"
            home.mkdir()
            settings = job / "s.json"
            settings.write_text("{}")
            permissions.traversable(job, gid)
            permissions.share_directory(home, gid)
            permissions.share_file(settings, gid)
            self.assertEqual(mode(job), 0o710)
            self.assertEqual(mode(home), 0o2770)
            self.assertEqual(mode(settings), 0o640)

    def test_a_linked_worktrees_admin_directory_is_made_readable_for_the_group(self):
        gid = os.getgid()
        previous = os.umask(0o077)  # what every Factory command runs under
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                main = root / "main"
                main.mkdir()
                git = ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t"]
                subprocess.run([*git, "init", "-q", "-b", "main"], cwd=main, check=True)
                (main / "f.txt").write_text("x")
                subprocess.run([*git, "add", "-A"], cwd=main, check=True)
                subprocess.run([*git, "commit", "-q", "-m", "c"], cwd=main, check=True)
                tree = root / "wt"
                subprocess.run([*git, "worktree", "add", "-q", "-b", "job", str(tree)], cwd=main, check=True)
                admin = main / ".git" / "worktrees" / "wt"
                mode = lambda p: stat.S_IMODE(os.lstat(p).st_mode)  # noqa: E731
                # git creates it private to the supervisor: another uid cannot use the worktree
                self.assertEqual(mode(admin / "commondir") & 0o070, 0)
                permissions.share_worktree(tree, gid)
                self.assertEqual(mode(admin) & 0o750, 0o750)
                for name in ("HEAD", "commondir", "gitdir"):
                    self.assertEqual(mode(admin / name) & 0o040, 0o040, name)
                self.assertEqual(mode(admin) & 0o020, 0, "the worker only reads it")
                self.assertTrue(mode(tree) & 0o2770 == 0o2770)
        finally:
            os.umask(previous)

    def test_sharing_a_plain_directory_as_a_worktree_is_harmless(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / "plain"
            (tree / ".git").mkdir(parents=True)
            permissions.share_worktree(tree, os.getgid())  # .git is a directory, not a link file

    def test_private_umask_makes_supervisor_files_unreadable_to_the_group(self):
        previous = os.umask(0o022)
        try:
            permissions.private_umask()
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "state.json"
                path.write_text("{}")
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        finally:
            os.umask(previous)


JOB = {
    "id": "j",
    "objective": "Do the thing",
    "acceptance": ["it works"],
    "issues": [7],
    "context_issues": [],
}


class BodyAndTitleBuilderTests(unittest.TestCase):
    def body(self, ready=True, ignored=(), unread=(), **changes):
        runner = Runner.__new__(Runner)
        runner.directory = Path("run")
        job = {**JOB, **changes}
        return runner.pr_body(
            job,
            {"commit": "a" * 40},
            {"summary": "Done", "tests": ["t"]},
            {"pass": ready, "findings": []},
            {"complete": ready},
            ready,
            ignored,
            unread,
        )

    def test_ready_pr_starts_with_closes(self):
        self.assertEqual(self.body().splitlines()[0], "Closes #7")

    def test_incomplete_ignored_and_unread_issues_are_only_referenced(self):
        self.assertEqual(self.body(ready=False).splitlines()[0], "Refs #7")
        self.assertEqual(self.body(ignored=[7]).splitlines()[0], "Refs #7")
        self.assertEqual(self.body(unread=[7]).splitlines()[0], "Refs #7")

    def test_several_issues_repeat_the_keyword_on_one_line(self):
        self.assertEqual(self.body(issues=[7, 8]).splitlines()[0], "Closes #7, closes #8")
        self.assertEqual(self.body(issues=[7, 8], ignored=[8]).splitlines()[0], "Closes #7, refs #8")

    def test_cross_repo_reference_uses_owner_repo(self):
        self.assertEqual(
            self.body(issues=[7, "acme/lib#12"]).splitlines()[0], "Closes #7, closes acme/lib#12"
        )
        with self.assertRaises(ValueError):
            self.body(issues=["acme/lib#12 and closes #3"])

    def test_job_without_issues_raises(self):
        with self.assertRaises(ValueError):
            self.body(issues=[])

    def test_first_line_pinned_and_sections_follow(self):
        for kwargs in ({}, {"ready": False}, {"issues": [7, 9], "context_issues": [3]}):
            body = self.body(**kwargs)
            self.assertRegex(body.splitlines()[0], r"^(Closes|Refs) ")
            positions = [
                body.index(h)
                for h in ("## Assignment", "## Acceptance", "## Implementation", "## Validation", "## Verification")
            ]
            self.assertEqual(positions, sorted(positions))
            self.assertGreater(positions[0], 0)
        self.assertIn("Refs #3", self.body(context_issues=[3]))

    def test_title_ends_with_the_issue_and_lists_every_issue(self):
        self.assertEqual(pr_title("Do the thing", [7]), "factory: Do the thing (#7)")
        self.assertEqual(
            pr_title("Do the thing", [7, "acme/lib#2"]), "factory: Do the thing (#7, acme/lib#2)"
        )

    def test_long_summary_is_cut_but_not_the_suffix(self):
        title = pr_title("x" * 1000, [7, 12345])
        self.assertEqual(len(title), TITLE_LIMIT)
        self.assertTrue(title.endswith(" (#7, #12345)"))
        self.assertTrue(title.startswith("factory: xxx"))

    def test_summary_text_is_neutralised_and_single_line(self):
        title = pr_title("Fix\nCloses #99 and acme/x#4", [7])
        self.assertNotIn("\n", title)
        self.assertEqual(re.findall(r"#\d+", title), ["#7"])

    def test_title_without_issues_raises(self):
        with self.assertRaises(ValueError):
            pr_title("Do the thing", [])


if __name__ == "__main__":
    unittest.main()


class AcceptanceSectionTests(unittest.TestCase):
    def body(self, acceptance):
        job = dict(assignment()["jobs"][0], acceptance=acceptance)
        runner = SimpleNamespace(directory=Path("run-1"))
        return Runner.pr_body(
            runner,
            job,
            {"commit": "a" * 40},
            {"summary": "done", "tests": ["t"]},
            {"pass": True, "findings": []},
            {"complete": True},
            True,
        )

    def test_pr_body_lists_each_criterion_with_its_tag_and_check_names(self):
        body = self.body(
            [
                "plain text",
                {"text": "scan the image", "tag": "ci", "checks": ["build", "trivy"]},
                {"text": "dispatch", "tag": "ci", "workflow": "release"},
                {"text": "prod behaviour", "tag": "operator"},
            ]
        )
        section = body.split("## Acceptance\n\n")[1].split("\n## ")[0].rstrip().splitlines()
        self.assertEqual(
            section,
            [
                "- [sandbox] plain text",
                "- [ci] scan the image (checks: build, trivy)",
                "- [ci] dispatch (workflow: release)",
                "- [operator] prod behaviour",
            ],
        )

    def test_criterion_text_and_names_are_neutralised(self):
        body = self.body(
            [{"text": "Closes #99", "tag": "ci", "checks": ["fixes #98"]}]
        )
        section = body.split("## Acceptance\n\n")[1].split("\n## ")[0].rstrip()
        self.assertNotIn("#99", section)
        self.assertNotIn("#98", section)
        self.assertIn("Closes #\u200b99", section)
        self.assertIn("fixes #\u200b98", section)

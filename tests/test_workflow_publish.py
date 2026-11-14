"""Jobs that change .github/workflows: marked before the build, parked instead of failed, resumable.

The Factory App has no ``workflows`` permission, so GitHub refuses its push of such a job after a
full build, review and verification. These tests use a local bare origin with a ``pre-receive`` hook
that refuses the way GitHub does, and the fake agent and fake ``gh`` of the other integration tests.
"""

import contextlib
import copy
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from factory import cli, issue_comments, operations, publish_block
from factory.config import RepoConfig
from factory.issue_labels import JOB_STATUS_LABELS, LABEL_BLOCKED, label_for_job
from factory.recovery import queue_resume
from factory.runner import Runner
from factory.status import format_status, status_report

from tests import test_factory as fixtures
from tests.test_factory import assignment

APP_REFUSAL = (
    "refusing to allow a GitHub App to create or update workflow "
    "`.github/workflows/x.yml` without `workflows` permission"
)
WORKFLOW_FILE = ".github/workflows/x.yml"
TOKEN_LIKE = "ghs_abcdefghijklmnopqrstuvwxyz0123456789"

BUILDS_A_WORKFLOW = """
prompt = sys.stdin.read()
with pathlib.Path(__file__).with_name('prompts.log').open('a') as handle:
    handle.write(json.dumps(prompt))
    handle.write('\\n')
if 'independent reviewer' in prompt:
    result = {'pass': True, 'findings': []}
else:
    pathlib.Path('src/a.txt').write_text('fixed\\n')
    pathlib.Path('.github/workflows').mkdir(parents=True, exist_ok=True)
    pathlib.Path('.github/workflows/x.yml').write_text('name: x\\n')
    result = {'summary': 'Fix A and the workflow', 'blocked': False, 'blocker': '', 'tests': ['t'], 'feature': False}
emit(result)
"""

DROPS_THE_WORKFLOW = BUILDS_A_WORKFLOW.replace(
    "pathlib.Path('.github/workflows/x.yml').write_text('name: x\\n')",
    "pathlib.Path('.github/workflows/x.yml').unlink()",
).replace("'fixed\\n'", "'fixed again\\n'")

REVIEW_FAILS = BUILDS_A_WORKFLOW.replace("{'pass': True, 'findings': []}", "{'pass': False, 'findings': ['still wrong']}")


def workflow_plan(paths=("src", WORKFLOW_FILE)):
    plan = assignment()
    plan["jobs"][0]["likely_paths"] = list(paths)
    return plan


class PathTests(unittest.TestCase):
    def test_workflow_paths_are_the_ones_under_or_containing_the_workflow_directory(self):
        found = publish_block.workflow_paths(
            [
                ".github/workflows/deploy.yml",
                ".github/workflows",
                ".github/workflows/",
                ".github",
                "./.github/workflows/ci.yml",
                ".",
                "src",
                ".github/CODEOWNERS",
                ".github/workflows-notes/x.md",
                "docs/.github/workflows/x.yml",
                "",
                None,
                7,
            ]
        )
        self.assertEqual(
            found,
            [".github/workflows/deploy.yml", ".github/workflows", ".github/workflows/", ".github", "./.github/workflows/ci.yml"],
        )
        self.assertEqual(publish_block.workflow_paths("not a list"), [])

    def test_planned_and_actual_paths_both_count_and_repeats_are_dropped(self):
        job = {"likely_paths": ["src", ".github/workflows/a.yml"]}
        state = {"actual_paths": ["src/a.txt", ".github/workflows/a.yml", ".github/workflows/b.yml"]}
        self.assertEqual(
            publish_block.publish_blocked_paths(job, state),
            [".github/workflows/a.yml", ".github/workflows/b.yml"],
        )
        self.assertEqual(publish_block.publish_blocked_paths({"likely_paths": ["."]}, {}), [])
        self.assertEqual(publish_block.publish_blocked_paths({}, None), [])


class StderrTests(unittest.TestCase):
    def test_the_stderr_is_redacted_of_tokens_urls_credentials_and_literal_secrets(self):
        text = "\n".join(
            [
                f"remote: using {TOKEN_LIKE}",
                "To https://x-access-token:hunter2hunter2@github.com/org/name.git",
                "remote: Authorization: Bearer abcdefghijklmnop1234",
                "remote: SOME_SETTING=private-value",
                "remote: the value super-secret-literal leaked",
                f" ! [remote rejected] x -> x ({APP_REFUSAL})",
            ]
        )
        kept = publish_block.redact_git_error(text, secrets=["super-secret-literal"])
        for leaked in (TOKEN_LIKE, "hunter2hunter2", "abcdefghijklmnop1234", "private-value", "super-secret-literal"):
            self.assertNotIn(leaked, kept)
        self.assertIn("[redacted]", kept)
        self.assertIn("https://[redacted]@github.com/org/name.git", kept)
        self.assertIn("without `workflows` permission", kept)

    def test_the_secret_values_of_the_process_are_redacted_by_default(self):
        with patch.dict(os.environ, {"PUSH_API_TOKEN": "process-held-credential-value"}):
            kept = publish_block.redact_git_error("remote: process-held-credential-value was echoed")
        self.assertNotIn("process-held-credential-value", kept)

    def test_control_characters_are_removed_and_the_size_is_capped_to_the_tail(self):
        noisy = "".join(["head-marker\n", "filler line\n" * 600, "\x1b[31mred\x1b[0m\r\x07 tail-marker\n"])
        kept = publish_block.redact_git_error(noisy)
        self.assertLessEqual(len(kept), publish_block.MAX_ERROR_CHARS)
        self.assertNotIn("\x1b", kept)
        self.assertNotIn("\x07", kept)
        self.assertNotIn("\r", kept)
        self.assertTrue(kept.endswith("tail-marker"))
        self.assertNotIn("head-marker", kept)
        self.assertEqual(publish_block.redact_git_error(b"bytes \xff text"), "bytes � text")
        self.assertEqual(publish_block.redact_git_error(None), "")

    def test_the_workflow_rule_is_recognised_tolerantly_and_nothing_else_is(self):
        workflow = [
            f" ! [remote rejected] b -> b ({APP_REFUSAL})",
            "remote: Refusing to allow an OAuth App to create or update workflow `.github/workflows/x.yml` without `workflow` scope",
            "refusing to allow a bot to update workflow .github/workflows/x.yml without the workflows permission",
            "remote: error: workflow files cannot be changed by this app without the `workflows` permission",
        ]
        other = [
            " ! [rejected] b -> b (non-fast-forward)",
            "git@github.com: Permission denied (publickey).",
            "remote: Permission to org/name.git denied to factory-app[bot].",
            "remote: error: GH006: Protected branch update failed for refs/heads/main.",
            "remote: the workflow succeeded",
            "",
        ]
        for text in workflow:
            with self.subTest(text=text):
                self.assertEqual(publish_block.classify_push_error(text), publish_block.REJECTION_WORKFLOWS)
        for text in other:
            with self.subTest(text=text):
                self.assertEqual(publish_block.classify_push_error(text), publish_block.REJECTION_OTHER)
        self.assertEqual(publish_block.classify_push_error(None), publish_block.REJECTION_OTHER)


class TableTests(unittest.TestCase):
    """The new status must not show as 'other', get the wrong exit code or be left unexplained."""

    def test_the_status_has_a_label_and_a_comment_kind(self):
        self.assertEqual(JOB_STATUS_LABELS["needs_human_publish"], LABEL_BLOCKED)
        self.assertEqual(label_for_job({"status": "needs_human_publish"}), LABEL_BLOCKED)
        kind = issue_comments.JOB_STATUS_KINDS["needs_human_publish"]
        self.assertEqual(kind, issue_comments.KIND_NEEDS_PUBLISH)
        self.assertIn(kind, issue_comments.KINDS)
        self.assertIn("needs_human_publish", issue_comments.CODES)

    def test_the_comment_says_a_person_must_publish_and_does_not_offer_a_rebuild(self):
        reason = issue_comments.sanitize_reason(publish_block.WORKFLOWS_REASON)
        self.assertEqual(reason, publish_block.WORKFLOWS_REASON)
        body = issue_comments.render(
            1, issue_comments.KIND_NEEDS_PUBLISH, "needs_human_publish", reason, "run-1"
        )
        self.assertIn("kind=needs_human_publish", body)
        self.assertIn(publish_block.WORKFLOWS_REASON, body)
        self.assertIn("Code: `needs_human_publish`", body)
        self.assertIn("A person who may change workflow files first reads the diff of every workflow file", body)
        self.assertIn("runs its on-push workflows with repository secrets", body)
        self.assertIn("from the Factory clone, never from the retained worktree", body)
        self.assertIn("cannot merge until a person applies the `factory-verified` label", body)
        self.assertIn("never given the `workflows` permission", body)
        self.assertNotIn("re-apply", body)
        # the retry sentence stays for the outcomes it is true for
        for kind in (issue_comments.KIND_BLOCKED, issue_comments.KIND_FAILED):
            self.assertIn("re-apply", issue_comments.render(1, kind, kind, reason, "run-1"))

    def test_the_restart_line_counts_the_status_apart_from_other(self):
        self.assertIn("needs_human_publish", operations.JOB_STATUSES)
        report = {"jobs": [{"id": "a", "status": "needs_human_publish"}, {"id": "b", "status": "mystery"}]}
        counts = operations.count_jobs(report)
        self.assertEqual((counts["needs_human_publish"], counts["other"]), (1, 1))
        line = operations.restart_line("org/name", "r1", counts, 0)
        self.assertIn(" blocked=0 needs_human_publish=1 stopped=0 merged=0 ", line)


class StatusTests(unittest.TestCase):
    def plan(self, paths):
        return {"jobs": [{"id": "a", "likely_paths": paths, "depends_on": []}]}

    def test_a_job_planned_to_change_workflows_is_marked_and_reported_before_it_builds(self):
        for status in ("not_started", "queued"):
            with self.subTest(status=status):
                data = {"version": 1, "jobs": {"a": {"status": status, "attempt": 0}}}
                report = status_report(self.plan(["src", ".github/workflows/deploy.yml"]), data)
                row = report["jobs"][0]
                self.assertTrue(row["needs_human_publish"])
                self.assertEqual(row["workflow_paths"], [".github/workflows/deploy.yml"])
                self.assertEqual(row["status"], status)
                text = format_status(report)
                self.assertIn("Workflow files: the Factory App cannot push them", text)
                self.assertIn("paths: .github/workflows/deploy.yml", text)

    def test_other_jobs_are_not_marked(self):
        data = {"version": 1, "jobs": {"a": {"status": "queued", "attempt": 0}}}
        for paths in (["src"], ["."], [".github/CODEOWNERS"]):
            with self.subTest(paths=paths):
                report = status_report(self.plan(paths), data)
                self.assertFalse(report["jobs"][0]["needs_human_publish"])
                self.assertEqual(report["jobs"][0]["workflow_paths"], [])
                self.assertNotIn("Workflow files:", format_status(report))

    def test_a_published_job_is_no_longer_marked(self):
        data = {"version": 1, "jobs": {"a": {"status": "pr_ready", "attempt": 1, "pr": "https://example.invalid/pr/1"}}}
        report = status_report(self.plan([".github/workflows"]), data)
        self.assertFalse(report["jobs"][0]["needs_human_publish"])

    def test_a_parked_job_tells_the_operator_what_to_do(self):
        held = {
            "branch": "factory/run-1/a",
            "bundle": "/runs/run-1/a/a.bundle",
            "repository": "/data/org/name/clone",
            "commit": "c" * 40,
        }
        job = {
            "status": "needs_human_publish",
            "attempt": 1,
            "commit": "c" * 40,
            "error": publish_block.WORKFLOWS_REASON,
            "push_error": f"remote: {APP_REFUSAL}",
            "push_rejection": "workflows_permission",
            "human_publish": held,
        }
        report = status_report(self.plan(["src"]), {"version": 1, "jobs": {"a": job}})
        row = report["jobs"][0]
        self.assertEqual(row["owner"], "operator")
        self.assertTrue(row["needs_human_publish"])
        self.assertEqual(row["push_rejection"], "workflows_permission")
        for part in (
            "cannot push workflow files",
            "review the diff of every .github/workflows file first",
            "a push runs the branch's on-push workflows with repository secrets",
            "push factory/run-1/a with your own credentials from the Factory clone /data/org/name/clone",
            "never with git inside the retained worktree",
            "resume a --phase publish",
            "cannot merge until a person applies the factory-verified label",
            "/runs/run-1/a/a.bundle",
        ):
            self.assertIn(part, row["next_action"])
        self.assertNotIn("git -C", row["next_action"])
        text = format_status(report)
        self.assertIn(f"Detail: {publish_block.WORKFLOWS_REASON}", text)
        self.assertIn("Push refused: remote: refusing to allow", text)
        # a branch or path that is not a plain name is never printed
        job["human_publish"] = {"branch": "x; rm -rf /", "bundle": "a b", "repository": "/x; touch /tmp/p"}
        row = status_report(self.plan(["src"]), {"version": 1, "jobs": {"a": job}})["jobs"][0]
        self.assertIn(
            "push the job branch with your own credentials from the Factory clone of the repository",
            row["next_action"],
        )
        self.assertNotIn("touch", row["next_action"])
        self.assertNotIn("rm -rf", row["next_action"])
        self.assertNotIn("bundle", row["next_action"])


class PlannedLinesTests(unittest.TestCase):
    """Acceptance: a planned workflow change is reported before anything is built, on the operator's path."""

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), patch("sys.argv", ["factory", *args]):
            try:
                cli.main()
                code = 0
            except SystemExit as exit_:
                code = exit_.code
        return code, out.getvalue(), err.getvalue()

    def test_only_marked_jobs_are_named_and_only_by_a_plain_id(self):
        plan = {
            "jobs": [
                {"id": "ci-job", "likely_paths": ["src", WORKFLOW_FILE]},
                {"id": "plain", "likely_paths": ["src", "."]},
                {"id": "x; rm -rf /\nforged: ok", "likely_paths": [".github"]},
                "not a job",
            ]
        }
        lines = publish_block.planned_lines(plan)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("ci-job: changes files under .github/workflows; the Factory App cannot push them"))
        self.assertTrue(lines[1].startswith("a job: changes files"))
        self.assertNotIn(WORKFLOW_FILE, "".join(lines), "no path of the plan is echoed")
        self.assertNotIn("rm -rf", "".join(lines))
        self.assertEqual(publish_block.planned_lines({"jobs": [{"id": "a", "likely_paths": ["src"]}]}), [])
        self.assertEqual(publish_block.planned_lines(None), [])

    def test_plan_prints_the_marked_jobs_after_its_one_summary_line(self):
        for paths, marked in ((("src", WORKFLOW_FILE), True), (("src",), False)):
            with self.subTest(paths=paths), tempfile.TemporaryDirectory() as root:
                directory = Path(root) / "r1"
                directory.mkdir()
                args = SimpleNamespace(issues=None, limit=None, timeout=10, use_snapshot=False)
                with contextlib.ExitStack() as stack:
                    stack.enter_context(patch.object(cli, "planner_worktree", return_value=Path(root)))
                    stack.enter_context(patch.object(cli, "share_planner_tree"))
                    stack.enter_context(patch.object(cli, "plan", return_value=workflow_plan(paths)))
                    stack.enter_context(patch.object(cli, "IssueLabels"))
                    stack.enter_context(patch.object(cli, "ensure_choice_labels"))
                    stack.enter_context(patch.object(cli, "commenter_for"))
                    out = io.StringIO()
                    stack.enter_context(contextlib.redirect_stdout(out))
                    cli.plan_command(args, RepoConfig(), Path(root), directory, None)
                lines = out.getvalue().splitlines()
                self.assertRegex(lines[0], r"^Planned 1 issues in 1 jobs: \S+$")
                self.assertEqual(len(lines), 2 if marked else 1, out.getvalue())
                if marked:
                    self.assertTrue(lines[1].startswith("fix-a: changes files under .github/workflows"))
                    self.assertNotIn(WORKFLOW_FILE, lines[1])

    def test_status_before_the_first_run_marks_the_job_through_the_cli(self):
        for paths, marked in ((("src", WORKFLOW_FILE), True), (("src",), False)):
            with self.subTest(paths=paths), tempfile.TemporaryDirectory() as root:
                directory = Path(root) / "r1"
                directory.mkdir()
                (directory / "plan.json").write_text(json.dumps(workflow_plan(paths)))
                self.assertFalse((directory / "state.json").exists())
                for extra in ([], ["--json"]):
                    code, out, err = self.run_cli("--run-dir", str(directory), "status", *extra)
                    self.assertEqual(code, 0, err)
                    lines = out.splitlines()
                    self.assertEqual(lines[0], "Planned 1 issues in 1 jobs")
                    if marked:
                        self.assertEqual(len(lines), 2, out)
                        self.assertTrue(lines[1].startswith("fix-a: changes files under .github/workflows"), out)
                        self.assertIn("a person publishes this job's branch", lines[1])
                    else:
                        self.assertEqual(len(lines), 1, out)
                self.assertFalse((directory / "state.json").exists(), "status only reads")


class WorkflowPublishTests(unittest.TestCase):
    setUp = fixtures.IntegrationTests.setUp
    git = fixtures.IntegrationTests.git
    script = fixtures.IntegrationTests.script
    install_agent = fixtures.IntegrationTests.install_agent
    gh_calls = fixtures.IntegrationTests.gh_calls
    gate_statuses = fixtures.IntegrationTests.gate_statuses

    def runner(self, plan=None, directory=None):
        return Runner(
            self.repo,
            directory or self.run_dir,
            plan or workflow_plan(),
            timeout=10,
            cfg=RepoConfig(repo="org/name"),
        )

    def refuse_workflows(self, message=APP_REFUSAL, only_workflows=True):
        """A pre-receive hook on the origin that refuses like GitHub; ``allow-workflows`` lifts it."""
        origin = self.root / "origin.git"
        check = (
            "git diff --name-only refs/heads/main \"$new\" | grep -q '^\\.github/workflows/' || continue"
            if only_workflows
            else ""
        )
        hook = origin / "hooks" / "pre-receive"
        hook.write_text(
            f"""#!/bin/sh
while read old new ref; do
  echo "$ref $new" >> {origin}/pushes.log
  [ -f {origin}/allow-workflows ] && continue
  {check}
  cat >&2 <<'MESSAGE'
{message}
MESSAGE
  exit 1
done
exit 0
"""
        )
        hook.chmod(0o755)

    def pushes(self):
        log = self.root / "origin.git" / "pushes.log"
        return log.read_text().splitlines() if log.exists() else []

    def prompts(self):
        log = self.binary / "prompts.log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def calls(self, *prefix):
        return [c for c in self.gh_calls() if c["args"][: len(prefix)] == list(prefix)]

    def parked(self, plan=None):
        self.install_agent(BUILDS_A_WORKFLOW)
        self.refuse_workflows()
        runner = self.runner(plan)
        runner.run()
        return runner, runner.state.data["jobs"]["fix-a"]

    # ---- acceptance: reported before it builds
    def test_the_builder_is_told_a_person_publishes_but_only_for_a_marked_job(self):
        self.install_agent(BUILDS_A_WORKFLOW)
        runner = self.runner(workflow_plan())
        row = status_report(runner.plan, runner.state.data)["jobs"][0]
        self.assertTrue(row["needs_human_publish"])
        self.assertEqual(self.prompts(), [], "reported before any agent ran")
        runner.run()
        builder, reviewer = self.prompts()
        self.assertIn("Publishing note: the Factory App cannot push changes under .github/workflows", builder)
        self.assertIn(WORKFLOW_FILE, builder)
        self.assertNotIn("Publishing note", reviewer)
        # no note for a job the plan does not expect to touch workflows
        (self.binary / "prompts.log").unlink()
        plain = self.runner(workflow_plan(["src"]), directory=self.root / "plain")
        plain.run()
        self.assertNotIn("Publishing note", self.prompts()[0])

    def test_workflow_files_are_noticed_after_the_build_when_the_plan_did_not_predict_them(self):
        runner, state = self.parked(workflow_plan(["src"]))
        self.assertIn(WORKFLOW_FILE, state["actual_paths"])
        row = status_report(runner.plan, runner.state.data)["jobs"][0]
        self.assertEqual(row["workflow_paths"], [WORKFLOW_FILE])

    # ---- acceptance: a rejected push is reported with its reason and the commit is preserved
    def test_a_push_refused_for_workflow_files_parks_the_finished_work_with_the_reason(self):
        runner, state = self.parked()
        self.assertEqual(state["status"], "needs_human_publish", state.get("error"))
        self.assertEqual(state["error"], "rejected: workflow files need the workflows permission")
        self.assertEqual(state["push_rejection"], "workflows_permission")
        self.assertIn("without `workflows` permission", state["push_error"])
        self.assertIn("remote rejected", state["push_error"])
        # the commit is preserved: retained worktree and branch at the reviewed commit, and a bundle
        worktree = Path(state["worktree"])
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=worktree, text=True, capture_output=True, check=True)
        self.assertEqual(head.stdout.strip(), state["commit"])
        self.assertTrue(state["review"]["pass"])
        held = state["human_publish"]
        self.assertEqual(held["commit"], state["commit"])
        self.assertEqual(held["branch"], state["branch"])
        self.assertEqual(held["bundle"], str(self.run_dir / "fix-a" / "fix-a.bundle"))
        self.assertEqual(held["repository"], str(self.repo), "the person is pointed at the clone, not the worktree")
        verified = subprocess.run(["git", "bundle", "verify", held["bundle"]], cwd=self.repo, text=True, capture_output=True)
        self.assertEqual(verified.returncode, 0, verified.stderr)
        heads = subprocess.run(["git", "bundle", "list-heads", held["bundle"]], cwd=self.repo, text=True, capture_output=True, check=True)
        self.assertIn(f"{state['commit']} refs/heads/{state['branch']}", heads.stdout)
        # nothing was published: no pull request, no gate status, nothing on the origin
        self.assertEqual(self.calls("pr", "create"), [])
        self.assertEqual(self.gate_statuses(), [])
        origin = subprocess.run(["git", "branch", "--list", "factory/*"], cwd=self.root / "origin.git", text=True, capture_output=True)
        self.assertEqual(origin.stdout.strip(), "")
        # the issue is labelled blocked and gets the one outcome comment, from fixed wording
        labels = json.loads((self.root / "issue-labels.json").read_text())
        self.assertEqual(labels["1"], ["factory:blocked"])
        posts = [
            c for c in self.calls("api")
            if "-X" in c["args"] and "POST" in c["args"] and any(a.endswith("/issues/1/comments") for a in c["args"])
        ]
        self.assertEqual(len(posts), 1)
        body = next(a for a in posts[0]["args"] if a.startswith("body="))
        self.assertIn("kind=needs_human_publish", body)
        self.assertIn(publish_block.WORKFLOWS_REASON, body)
        # status shows the operator what to do
        row = status_report(runner.plan, runner.state.data)["jobs"][0]
        self.assertIn(f"push {state['branch']} with your own credentials from the Factory clone {self.repo}", row["next_action"])
        self.assertNotIn(state["worktree"], row["next_action"], "the worker-writable worktree is never named")

    def test_the_run_command_exits_nonzero_and_prints_the_commands_for_a_parked_job(self):
        self.install_agent(BUILDS_A_WORKFLOW)
        self.refuse_workflows()
        args = SimpleNamespace(
            concurrency=2, verification_concurrency=1, timeout=10, review_rounds=1,
            review_concurrency=None, max_active_agents=None,
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as exit_:
            cli.run_command(args, RepoConfig(repo="org/name"), self.repo, self.run_dir, workflow_plan(), None, None)
        self.assertEqual(exit_.exception.code, 2)
        lines = [line for line in out.getvalue().splitlines() if "starting" not in line]
        self.assertEqual(lines[0], "fix-a: needs_human_publish")
        self.assertIn("review the diff of every .github/workflows file first", lines[1])
        self.assertIn(f"from the Factory clone {self.repo}, never with git inside the retained worktree", lines[1])
        self.assertIn("resume fix-a --phase publish", lines[1])
        self.assertNotIn("remote", out.getvalue())  # git's output stays out of the supervisor log

    def test_retry_is_still_possible_for_a_parked_job(self):
        runner, state = self.parked()
        args = SimpleNamespace(job="fix-a", reason="the workflow part moves to a separate job", followup=False)
        with contextlib.redirect_stdout(io.StringIO()):
            cli.retry_command(args, RepoConfig(repo="org/name"), self.repo, self.run_dir, runner.plan, lambda: True)
        after = json.loads((self.run_dir / "state.json").read_text())["jobs"]["fix-a"]
        self.assertEqual(after["status"], "queued")
        for name in ("human_publish", "push_error", "push_rejection"):
            self.assertIsNone(after[name], name)
        self.assertEqual(after["retry_history"][-1]["reason"], "the workflow part moves to a separate job")

    def test_any_other_refusal_fails_with_the_redacted_stderr_kept(self):
        self.install_agent(BUILDS_A_WORKFLOW)
        self.refuse_workflows(
            message=f"remote: branch protection says no, token {TOKEN_LIKE} was rejected", only_workflows=False
        )
        runner = self.runner()
        runner.run()
        state = runner.state.data["jobs"]["fix-a"]
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["push_rejection"], "other")
        self.assertTrue(state["error"].startswith("git push failed (exit 1): "))
        self.assertIn("branch protection says no", state["push_error"])
        saved = (self.run_dir / "state.json").read_text()
        self.assertNotIn(TOKEN_LIKE, saved)
        self.assertIn("[redacted]", state["push_error"])
        self.assertNotIn("human_publish", state)
        self.assertFalse((self.run_dir / "fix-a" / "fix-a.bundle").exists())

    def test_unfinished_work_refused_for_workflows_fails_with_the_reason_and_is_not_parked(self):
        self.install_agent(REVIEW_FAILS)
        self.refuse_workflows()
        runner = self.runner()
        runner.run()
        state = runner.state.data["jobs"]["fix-a"]
        self.assertEqual(state["status"], "failed")
        self.assertTrue(state["error"].startswith(publish_block.WORKFLOWS_REASON), state["error"])
        self.assertEqual(state["push_rejection"], "workflows_permission")
        self.assertNotIn("human_publish", state)
        self.assertTrue(Path(state["worktree"]).is_dir())
        # the same rule as the hint: git runs in the Factory clone, never in the retained worktree
        self.assertTrue(
            state["error"].endswith("so the commit stays on the local job branch in the Factory clone (retry, or inspect it there)"),
            state["error"],
        )
        self.assertNotIn("retained worktree", state["error"])
        self.assertNotIn("by hand", state["error"])
        branches = subprocess.run(
            ["git", "rev-parse", f"refs/heads/{state['branch']}"], cwd=self.repo, text=True, capture_output=True, check=True
        )
        self.assertEqual(branches.stdout.strip(), state["commit"], "the commit is on a branch of the Factory clone")

    # ---- the way back: a person pushes, then resume --phase publish
    def person_pushes(self, state):
        """A person with the right to push workflow files, from their own clone (no tracking ref here)."""
        (self.root / "origin.git" / "allow-workflows").touch()
        subprocess.run(
            ["git", "push", str(self.root / "origin.git"), f"{state['branch']}:refs/heads/{state['branch']}"],
            cwd=state["worktree"], capture_output=True, check=True,
        )

    def resume(self, runner, **values):
        queue_resume(self.repo, self.run_dir, runner.plan, runner.state, "fix-a", "publish", cfg=RepoConfig(repo="org/name"), **values)
        resumed = self.runner()
        resumed.run()
        return resumed, resumed.state.data["jobs"]["fix-a"]

    def test_resume_after_a_person_pushed_publishes_a_draft_a_person_must_release(self):
        runner, parked = self.parked()
        self.assertEqual(parked["status"], "needs_human_publish")
        self.person_pushes(parked)
        attempts = self.pushes()
        resumed, state = self.resume(runner)
        self.assertEqual(state["status"], "pr_incomplete", state.get("error"))
        self.assertEqual(self.pushes(), attempts, "Factory did not push the branch again")
        # Fail closed: not ready, so no factory:ready label, no auto-merge enrolment and no gate success
        create = self.calls("pr", "create")
        self.assertEqual(len(create), 1)
        self.assertIn("--draft", create[0]["args"])
        self.assertNotIn("--label", create[0]["args"])
        self.assertNotIn("factory:ready", create[0]["args"])
        statuses = self.gate_statuses()
        self.assertEqual([s[0] for s in statuses], ["pending", "failure"])
        self.assertEqual(statuses[-1][2], f"repos/org/name/statuses/{state['commit']}")
        self.assertIn("workflow files changed: a person must review the diff", " ".join(statuses[-1]))
        # a deliberate hold is not "unverified": it has its own reason code everywhere
        self.assertEqual(state["reason_code"], "workflow_hold")
        self.assertTrue(statuses[-1][1].endswith(" [workflow_hold]"), statuses[-1][1])
        self.assertLessEqual(len(statuses[-1][1]), 140)
        body = (self.run_dir / "fix-a" / "pr-body.md").read_text()
        self.assertIn("**Reason code**: `workflow_hold`", body)
        self.assertEqual(body.splitlines()[0], "Refs #1")
        self.assertIn("Published as a draft because it changes workflow files (`.github/workflows/x.yml`)", body)
        self.assertIn("a person must read the diff of every one of them", body)
        for key in ("human_publish", "push_error", "push_rejection"):
            self.assertIsNone(state[key])
        self.assertEqual(state["error"], None)
        row = status_report(resumed.plan, resumed.state.data)["jobs"][0]
        self.assertEqual(row["reason_code"], "workflow_hold")
        self.assertIn("changes workflow files", row["next_action"])
        self.assertIn("factory-verified", row["next_action"])
        self.assertEqual(row["owner"], "operator")

    def test_a_workflow_change_is_a_draft_a_person_must_release_even_when_the_push_was_accepted(self):
        self.install_agent(BUILDS_A_WORKFLOW)  # the origin refuses nothing here
        runner = self.runner(workflow_plan(["src"]))
        runner.run()
        state = runner.state.data["jobs"]["fix-a"]
        self.assertEqual(state["status"], "pr_incomplete", state.get("error"))
        self.assertIn(WORKFLOW_FILE, state["actual_paths"])
        create = self.calls("pr", "create")
        self.assertIn("--draft", create[0]["args"])
        self.assertNotIn("--label", create[0]["args"])
        self.assertEqual([s[0] for s in self.gate_statuses()], ["pending", "failure"])
        # republishing the same head keeps it a draft without the label, whatever the job state says
        self.assertEqual(publish_block.review_hold({"actual_paths": [WORKFLOW_FILE]}), [WORKFLOW_FILE])

    def test_a_job_that_does_not_change_workflow_files_is_not_held(self):
        self.assertEqual(publish_block.review_hold({"actual_paths": ["src/a.txt", ".github/CODEOWNERS"]}), [])
        self.assertEqual(publish_block.review_hold({"likely_paths": [WORKFLOW_FILE]}), [], "an estimate holds nothing")
        self.assertEqual(publish_block.review_hold({}), [])
        self.assertEqual(publish_block.review_hold(None), [])
        # a parked record alone holds, even if the paths were lost
        self.assertEqual(publish_block.review_hold({"human_publish": {"commit": "c"}}), [".github/workflows"])

    def test_the_hold_follows_what_the_head_changes_not_the_paths_a_retry_once_touched(self):
        self.assertEqual(publish_block.review_hold({"actual_paths": [WORKFLOW_FILE]}, ["src/a.txt"]), [])
        self.assertEqual(publish_block.review_hold({"actual_paths": []}, [WORKFLOW_FILE]), [WORKFLOW_FILE])
        self.assertEqual(publish_block.review_hold({"actual_paths": [WORKFLOW_FILE]}, []), [])
        # git could not say (None): the recorded paths decide, the safe direction
        self.assertEqual(publish_block.review_hold({"actual_paths": [WORKFLOW_FILE]}, None), [WORKFLOW_FILE])
        self.assertEqual(
            publish_block.review_hold({"actual_paths": [WORKFLOW_FILE], "held_paths": []}), [WORKFLOW_FILE],
            "an older publish's record never releases a new head whose diff could not be read",
        )
        # status has no worktree: it shows what the last publish recorded, else the safe fallback
        self.assertEqual(publish_block.recorded_hold({"actual_paths": [WORKFLOW_FILE], "held_paths": []}), [])
        self.assertEqual(publish_block.recorded_hold({"held_paths": [WORKFLOW_FILE]}), [WORKFLOW_FILE])
        self.assertEqual(publish_block.recorded_hold({"actual_paths": [WORKFLOW_FILE]}), [WORKFLOW_FILE])
        self.assertEqual(publish_block.recorded_hold({"actual_paths": [WORKFLOW_FILE], "held_paths": None}), [WORKFLOW_FILE])
        self.assertEqual(publish_block.recorded_hold({"held_paths": [], "human_publish": {"commit": "c"}}), [".github/workflows"])
        self.assertEqual(publish_block.recorded_hold(None), [])
        # a parked record still holds, whatever the diff says
        self.assertEqual(publish_block.review_hold({"human_publish": {"commit": "c"}}, ["src/a.txt"]), [".github/workflows"])

    def test_a_job_retried_to_drop_its_workflow_change_is_published_like_any_other(self):
        runner, parked = self.parked()
        self.assertIn(WORKFLOW_FILE, parked["actual_paths"])
        args = SimpleNamespace(job="fix-a", reason="drop the workflow change from this job", followup=False)
        with contextlib.redirect_stdout(io.StringIO()):
            cli.retry_command(args, RepoConfig(repo="org/name"), self.repo, self.run_dir, runner.plan, lambda: True)
        self.install_agent(DROPS_THE_WORKFLOW)
        retried = self.runner()
        retried.run()
        state = retried.state.data["jobs"]["fix-a"]
        self.assertIn(WORKFLOW_FILE, state["actual_paths"], "the scope the job once touched is still recorded")
        changed = subprocess.run(
            ["git", "diff", "--name-only", "main", state["commit"]], cwd=self.repo, text=True, capture_output=True, check=True
        )
        self.assertEqual(changed.stdout.split(), ["src/a.txt"], "the head no longer changes a workflow file")
        self.assertEqual(state["status"], "pr_ready", state.get("error"))
        self.assertEqual(state["held_paths"], [])
        self.assertEqual(publish_block.recorded_hold(state), [])
        create = self.calls("pr", "create")
        self.assertEqual(len(create), 1)
        self.assertNotIn("--draft", create[0]["args"])
        self.assertEqual(self.gate_statuses()[-1][0], "success")
        body = (self.run_dir / "fix-a" / "pr-body.md").read_text()
        self.assertNotIn("workflow files", body)
        row = status_report(retried.plan, retried.state.data)["jobs"][0]
        self.assertNotIn("workflow files", row["next_action"])
        # held back for some other reason, status does not blame workflow files either
        data = copy.deepcopy(retried.state.data)
        data["jobs"]["fix-a"]["status"] = "pr_incomplete"
        row = status_report(retried.plan, data)["jobs"][0]
        self.assertNotIn("changes workflow files", row["next_action"])
        self.assertIn("criteria proven elsewhere", row["next_action"])
        data["jobs"]["fix-a"]["held_paths"] = [WORKFLOW_FILE]
        row = status_report(retried.plan, data)["jobs"][0]
        self.assertIn("changes workflow files", row["next_action"])

    def test_a_retried_job_that_still_changes_a_workflow_file_stays_held(self):
        runner, parked = self.parked()
        args = SimpleNamespace(job="fix-a", reason="try again", followup=False)
        with contextlib.redirect_stdout(io.StringIO()):
            cli.retry_command(args, RepoConfig(repo="org/name"), self.repo, self.run_dir, runner.plan, lambda: True)
        (self.root / "origin.git" / "allow-workflows").touch()
        retried = self.runner()
        retried.run()
        state = retried.state.data["jobs"]["fix-a"]
        self.assertEqual(state["status"], "pr_incomplete", state.get("error"))
        self.assertEqual(state["held_paths"], [WORKFLOW_FILE])
        self.assertIn("--draft", self.calls("pr", "create")[0]["args"])
        self.assertEqual(self.gate_statuses()[-1][0], "failure")

    def test_a_head_whose_diff_cannot_be_read_is_held_on_the_recorded_paths(self):
        runner, parked = self.parked()
        self.person_pushes(parked)
        real = Runner.git

        def unreadable(self, *args, **kwargs):
            if args[:1] == ("diff",):
                raise subprocess.CalledProcessError(128, ["git", *args], stderr="fatal: bad object")
            return real(self, *args, **kwargs)

        with patch.object(Runner, "git", unreadable):
            resumed, state = self.resume(runner)
        self.assertEqual(state["status"], "pr_incomplete", state.get("error"))
        self.assertEqual(state["held_paths"], [WORKFLOW_FILE])
        self.assertEqual(self.gate_statuses()[-1][0], "failure")
        self.assertIn("--draft", self.calls("pr", "create")[0]["args"])

    def test_resume_before_a_person_pushed_parks_it_again(self):
        runner, parked = self.parked()
        first = self.pushes()
        resumed, state = self.resume(runner, reason="trying again before the branch was pushed")
        self.assertEqual(state["status"], "needs_human_publish")
        self.assertEqual(len(self.pushes()), len(first) + 1, "the push was attempted once more")
        self.assertEqual(state["human_publish"]["commit"], parked["commit"])
        self.assertTrue(Path(state["human_publish"]["bundle"]).is_file())
        self.assertEqual(self.calls("pr", "create"), [])

    def test_resume_refuses_to_overwrite_a_branch_a_person_changed(self):
        runner, parked = self.parked()
        worktree = Path(parked["worktree"])
        (self.root / "origin.git" / "allow-workflows").touch()
        other = self.root / "elsewhere"
        subprocess.run(["git", "clone", "-q", str(self.root / "origin.git"), str(other)], check=True, capture_output=True)
        for args in (["config", "user.email", "p@example.invalid"], ["config", "user.name", "P"], ["checkout", "-q", "-b", parked["branch"]]):
            subprocess.run(["git", *args], cwd=other, check=True, capture_output=True)
        (other / "person.txt").write_text("a person's own commit\n")
        subprocess.run(["git", "add", "."], cwd=other, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "person"], cwd=other, check=True, capture_output=True)
        subprocess.run(["git", "push", "-q", "origin", parked["branch"]], cwd=other, check=True, capture_output=True)
        resumed, state = self.resume(runner)
        self.assertEqual(state["status"], "failed")
        self.assertIn("has commits the local job branch lacks", state["error"])
        self.assertEqual(self.calls("pr", "create"), [])
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=worktree, text=True, capture_output=True, check=True)
        self.assertEqual(head.stdout.strip(), parked["commit"])

    def test_the_first_publish_of_a_commit_never_reads_the_origin_first(self):
        self.install_agent(
            BUILDS_A_WORKFLOW.replace("pathlib.Path('.github/workflows/x.yml').write_text('name: x\\n')", "pass")
        )
        self.refuse_workflows()
        runner = self.runner()
        with patch.object(Runner, "remote_branch", side_effect=AssertionError("fetched the origin")):
            runner.run()
        self.assertEqual(runner.state.data["jobs"]["fix-a"]["status"], "pr_ready")


if __name__ == "__main__":
    unittest.main()

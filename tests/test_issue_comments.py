"""The outcome comment on issues (src/factory/issue_comments.py), with a fake ``gh``.

The fake keeps each issue's labels in $FAKE_DIR/labels.json and its comments in comments.json,
and logs every call to calls.jsonl. Comments are authored by a ``Bot`` user.
"""

import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from factory import issue_comments, issue_labels, plan_failure, planner
from factory.config import RepoConfig
from factory.issue_comments import (
    IssueComments,
    marker,
    render,
    render_held,
    sanitize_reason,
    split_questions,
    without_factory_comments,
)
from factory.issue_labels import IssueLabels
from factory.locks import LockLost

SECRET = "SECRET-ISSUE-TEXT-do-not-print"

FAKE_GH = r"""
import json, os, pathlib, re, sys
root = pathlib.Path(os.environ['FAKE_DIR'])
args = sys.argv[1:]
with open(root / 'calls.jsonl', 'a') as log:
    log.write(json.dumps(args) + '\n')
def load(name):
    store = root / name
    return json.loads(store.read_text()) if store.exists() else {}
def save(name, data):
    (root / name).write_text(json.dumps(data))
mode = os.environ.get('FAKE_COMMENT_FAIL')
path = next((a for a in args[1:] if a.startswith('repos/')), '')
is_comment = '/comments' in path
if is_comment and mode == '403':
    sys.stderr.write('HTTP 403 ' + os.environ['FAKE_SECRET'])
    sys.exit(1)
if is_comment and mode == '404':
    sys.stderr.write('HTTP 404 ' + os.environ['FAKE_SECRET'])
    sys.exit(1)
if is_comment and mode == 'garbage' and '--paginate' in args:
    print('this is not json ' + os.environ['FAKE_SECRET'])
    sys.exit(0)
if is_comment and mode == 'timeout':
    import time
    time.sleep(60)
m = re.fullmatch(r'repos/[^/]+/[^/]+/issues/(\d+)/comments', path)
c = re.fullmatch(r'repos/[^/]+/[^/]+/issues/comments/(\d+)', path)
body = next((a.split('=', 1)[1] for a in args if a.startswith('body=')), None)
if args == ['api', 'user', '--jq', '.login']:
    print(os.environ.get('FAKE_LOGIN', 'factory-bot[bot]'))
elif args[:1] == ['api'] and m and '--paginate' in args:
    jq = args[args.index('--jq') + 1]
    login = re.search(r'\.user\.login == "(.*?)"', jq).group(1)
    for item in load('comments.json').get(m.group(1), []):
        if item['user'].get('login', 'factory-bot[bot]') == login and item['body'].startswith(
            re.search(r'startswith\("(.*?)"\)', ' '.join(args)).group(1)
        ):
            print(json.dumps({'id': item['id'], 'body': item['body']}))
elif args[:1] == ['api'] and m and args[1:3] == ['-X', 'POST']:
    data = load('comments.json')
    mine = data.setdefault(m.group(1), [])
    new_id = 1000 + sum(len(v) for v in data.values())
    mine.append({'id': new_id, 'body': body, 'user': {'type': 'Bot'}})
    save('comments.json', data)
    print(new_id)
elif args[:1] == ['api'] and c and args[1:3] == ['-X', 'PATCH']:
    data = load('comments.json')
    for items in data.values():
        for item in items:
            if item['id'] == int(c.group(1)):
                item['body'] = body
    save('comments.json', data)
    print('{}')
elif args[:1] == ['api'] and re.fullmatch(r'repos/[^/]+/[^/]+/issues/(\d+)', path):
    number = re.search(r'(\d+)$', path).group(1)
    states = root / 'states.json'
    state = json.loads(states.read_text()).get(number, 'open') if states.exists() else 'open'
    print(json.dumps({'state': state, 'labels': load('labels.json').get(number, [])}))
elif args[:1] == ['api'] or args[:2] == ['label', 'create']:
    pass
else:
    raise RuntimeError(args)
"""

BLOCKED_REASON = "This is a five-phase redesign, not one bounded fix. Split it into phases first."
QUESTION_REASON = (
    "The bug is still present, but the fix needs a decision. "
    "Where should GET /repos take the refresh flag from? "
    "Should it be persisted between requests?"
)


def plan_with(*dispositions):
    plan = {
        "version": 1,
        "artifact_roots": [],
        "issues": [{"number": n, "title": f"T{n}", "body": ""} for n in (1, 2)],
        "jobs": [
            {
                "id": "fix-a",
                "issues": [2],
                "context_issues": [7],
                "objective": "x",
                "likely_paths": ["src"],
                "acceptance": ["a"],
                "depends_on": [],
                "feature": False,
            }
        ],
        "dispositions": [
            {"issue": 2, "status": "job", "job": "fix-a", "reason": "Concrete fix"},
            *dispositions,
        ],
    }
    return plan


def blocked_plan(reason=BLOCKED_REASON, status="blocked"):
    return plan_with({"issue": 1, "status": status, "reason": reason})


class FakeGhCase(unittest.TestCase):
    def setUp(self):
        issue_labels.reset_report()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text(f"#!{sys.executable}\n" + FAKE_GH)
        gh.chmod(0o755)
        self.work = self.root / "work"
        self.work.mkdir()
        env = patch.dict(
            os.environ,
            {
                "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                "FAKE_DIR": str(self.root),
                "FAKE_SECRET": SECRET,
            },
        )
        env.start()
        self.addCleanup(env.stop)
        for name in ("FAKE_COMMENT_FAIL", issue_comments.ENV_SWITCH, issue_comments.ENV_CAP):
            os.environ.pop(name, None)

    def calls(self):
        log = self.root / "calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def comment_writes(self):
        return [
            c for c in self.calls() if "-X" in c and any("/comments" in a for a in c)
        ]

    def comments(self, number):
        store = self.root / "comments.json"
        return json.loads(store.read_text()).get(str(number), []) if store.exists() else []

    def set_labels(self, number, *names):
        store = self.root / "labels.json"
        data = json.loads(store.read_text()) if store.exists() else {}
        data[str(number)] = list(names)
        store.write_text(json.dumps(data))

    def set_state(self, number, state):
        store = self.root / "states.json"
        data = json.loads(store.read_text()) if store.exists() else {}
        data[str(number)] = state
        store.write_text(json.dumps(data))

    def issue_reads(self, number):
        return [
            c
            for c in self.calls()
            if c[:1] == ["api"] and "-X" not in c and c[1] == f"repos/org/name/issues/{number}"
        ]

    def labeller(self, plan=None, run="run-1", fence=None, **comment_args):
        commenter = IssueComments(self.work, "org/name", run, fence=fence, **comment_args)
        labels = IssueLabels(
            self.work, "org/name", plan or blocked_plan(), fence=fence, commenter=commenter
        )
        return labels, commenter

    def quietly(self, function, *args, **kwargs):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result = function(*args, **kwargs)
        return result, err.getvalue()


class OutcomeCommentTests(FakeGhCase):
    def test_blocked_disposition_comments_once(self):
        labels, _ = self.labeller()
        labels.sync_plan()
        mine = self.comments(1)
        self.assertEqual(len(mine), 1)
        body = mine[0]["body"]
        self.assertTrue(body.startswith(marker(1, "blocked") + "\n"))
        self.assertIn("**Factory: blocked**", body)
        self.assertIn("five-phase redesign", body)
        self.assertIn("What would unblock it:", body)
        self.assertIn("Run: `run-1`", body)
        self.assertIn("Code: `blocked`", body)
        self.assertIn("re-apply the `factory-build` label", body)
        self.assertEqual(self.comments(2), [])  # a job with no failure gets no comment

    def test_the_cause_is_read_from_the_raw_reason_not_the_sanitised_text(self):
        # a job id such as gh-12 is rewritten by sanitize_reason (GH-<digit> -> a fullwidth hyphen), which
        # used to turn a closed-dependency block into a plain "try again" footer
        _, commenter = self.labeller()
        self.assertTrue(commenter.announce(7, "blocked", "blocked", "dependency gh-12 was closed without merging"))
        (comment,) = self.comments(7)
        self.assertIn("Retrying the old plan cannot help", comment["body"])
        self.assertNotIn("To ask Factory to try again", comment["body"])

    def test_a_run_does_not_write_back_the_issue_a_replan_took_into_another_run(self):
        closed = "dependency job-1 was closed without merging"
        plan = plan_with({"issue": 7, "status": "job"}, {"issue": 8, "status": "job"})
        plan["jobs"] = [
            {"id": "job-3", "issues": [7, 8], "depends_on": [], "likely_paths": [], "acceptance": []},
        ]
        states = {"job-3": {"status": "blocked", "error": closed}}
        labels, _ = self.labeller(plan)
        self.assertEqual(labels.stale_issues(states), {7, 8})
        self.assertEqual(labels.stale_issues({"job-3": {"status": "failed", "error": closed}}), set())
        self.assertEqual(labels.stale_issues({"job-3": {"status": "blocked", "error": "tests failed"}}), set())
        # issue 7 was re-planned into another run (its label is that run's now); 8 was not
        self.set_labels(7, "factory:building")
        labels.reconcile(states, skip={7})
        self.assertEqual([c["body"] for c in self.comments(7)], [])
        self.assertEqual(len(self.comments(8)), 1)
        written = [c for c in self.calls() if "-X" in c and any("/issues/7/labels" in a for a in c)]
        self.assertEqual(written, [])

    def test_investigation_comments_once_with_numbered_questions(self):
        labels, _ = self.labeller(blocked_plan(QUESTION_REASON, "investigation"))
        labels.sync_plan()
        (comment,) = self.comments(1)
        body = comment["body"]
        self.assertIn("**Factory: needs a decision**", body)
        self.assertIn("1. Where should GET /repos take the refresh flag from?", body)
        self.assertIn("2. Should it be persisted between requests?", body)
        self.assertIn("Code: `investigation`", body)
        labels.reconcile({})
        self.assertEqual(len(self.comments(1)), 1)

    def test_an_investigation_gets_the_blocked_label_and_one_needs_decision_comment(self):
        labels, _ = self.labeller(blocked_plan(QUESTION_REASON, "investigation"))
        labels.sync_plan()
        added = [
            a
            for c in self.calls()
            if "-X" in c and "POST" in c and "repos/org/name/issues/1/labels" in c
            for a in c
            if a.startswith("labels[]=")
        ]
        self.assertEqual(added, ["labels[]=factory:blocked"])
        (comment,) = self.comments(1)
        self.assertIn("**Factory: needs a decision**", comment["body"])
        # the same after a restart: the label is derived again, the comment is not duplicated
        again, _ = self.labeller(blocked_plan(QUESTION_REASON, "investigation"))
        again.reconcile({})
        self.assertEqual(len(self.comments(1)), 1)

    def test_unchanged_outcome_does_not_comment_again(self):
        labels, _ = self.labeller()
        labels.sync_plan()
        before = len(self.calls())
        labels.reconcile({})
        labels.sync_plan()
        self.assertEqual(len(self.comments(1)), 1)
        self.assertEqual(len(self.comment_writes()), 1)
        # In the same process the second look needs no comment call at all.
        later = [c for c in self.calls()[before:] if any("/comments" in a for a in c)]
        self.assertEqual(later, [])

    def test_changed_reason_edits_the_same_comment_found_by_its_marker(self):
        labels, _ = self.labeller()
        labels.sync_plan()
        first_id = self.comments(1)[0]["id"]
        # Somebody else's comment that merely quotes the marker is not Factory's.
        data = json.loads((self.root / "comments.json").read_text())
        data["1"].insert(0, {"id": 1, "body": marker(1, "blocked"), "user": {"type": "User", "login": "someone"}})
        (self.root / "comments.json").write_text(json.dumps(data))
        plan = blocked_plan("It now depends on the branch-sync issue, which is not built yet.")
        again, _ = self.labeller(plan)  # a new process: no memory of the first comment
        again.sync_plan()
        mine = [c for c in self.comments(1) if c["user"]["type"] == "Bot"]
        self.assertEqual([c["id"] for c in mine], [first_id])
        self.assertIn("branch-sync issue", mine[0]["body"])
        self.assertNotIn("five-phase", mine[0]["body"])
        self.assertEqual(self.comments(1)[0]["body"], marker(1, "blocked"))  # untouched
        patches = [c for c in self.comment_writes() if c[2] == "PATCH"]
        self.assertEqual(len(patches), 1)
        self.assertTrue(patches[0][3].endswith(f"/issues/comments/{first_id}"))

    def test_foreign_bot_comment_with_the_marker_is_never_edited(self):
        labels, _ = self.labeller(blocked_plan())
        data = {"1": [{"id": 5, "body": marker(1, "blocked") + "\nforeign",
                       "user": {"type": "Bot", "login": "github-actions[bot]"}}]}
        (self.root / "comments.json").write_text(json.dumps(data))
        labels.sync_plan()
        self.assertEqual([c for c in self.comment_writes() if c[2] == "PATCH"], [])
        theirs = self.comments(1)[0]
        self.assertEqual(theirs["body"], marker(1, "blocked") + "\nforeign")
        self.assertEqual(len(self.comments(1)), 2)

    def test_app_identity_is_the_bot_user_id(self):
        from types import SimpleNamespace
        _, commenter = self.labeller(blocked_plan())
        with patch.object(issue_comments, "get_auth", return_value=SimpleNamespace(app_user_id=4242)):
            self.assertEqual(commenter._author_test(), '.user.type == "Bot" and .user.id == 4242')

    def test_unknown_identity_posts_new_instead_of_editing(self):
        labels, commenter = self.labeller(blocked_plan())
        commenter.author = None
        data = {"1": [{"id": 5, "body": marker(1, "blocked"), "user": {"type": "Bot"}}]}
        (self.root / "comments.json").write_text(json.dumps(data))
        labels.sync_plan()
        self.assertEqual([c for c in self.comment_writes() if c[2] == "PATCH"], [])
        self.assertEqual(len(self.comments(1)), 2)

    def test_changed_reason_in_one_process_edits_without_listing(self):
        labels, commenter = self.labeller()
        labels.sync_plan()
        labels.plan["dispositions"][1]["reason"] = "A different explanation of the block."
        labels.sync_plan()
        self.assertEqual(len(self.comments(1)), 1)
        self.assertIn("different explanation", self.comments(1)[0]["body"])

    def test_restart_with_same_persisted_state_does_not_duplicate(self):
        first, _ = self.labeller()
        first.sync_plan()
        for _ in range(2):
            restarted, _ = self.labeller()
            restarted.reconcile({})
        self.assertEqual(len(self.comments(1)), 1)
        self.assertEqual(len(self.comment_writes()), 1)

    def test_different_outcome_kinds_are_separate_comments(self):
        labels, _ = self.labeller(blocked_plan(), run="run-1")
        labels.sync_plan()
        labels.on_job(
            "fix-a", {"status": "running"}, {"status": "failed", "error": "tests did not pass"}
        )
        self.assertEqual(len(self.comments(1)), 1)
        (failed,) = self.comments(2)
        self.assertIn("**Factory: failed**", failed["body"])
        self.assertIn("tests did not pass", failed["body"])
        self.assertIn(marker(2, "failed"), failed["body"])

    def test_a_timed_out_job_tells_the_reader_to_raise_the_timeout_or_split_the_issue(self):
        labels, _ = self.labeller(blocked_plan(), run="run-1")
        labels.on_job("fix-a", {"status": "running"}, {"status": "failed", "error": "timed out after 1800 s"})
        (failed,) = self.comments(2)
        body = failed["body"]
        self.assertIn("**Factory: failed**", body)
        self.assertIn("timed out after 1800 s", body)
        self.assertIn("The agent session ran out of time", body)
        self.assertIn("raise `auto_build_timeout_seconds`", body)
        self.assertIn("split the issue", body)
        self.assertNotIn("An operator should look at the run", body)

    def test_only_the_exact_timeout_reason_gets_the_timeout_advice(self):
        labels, _ = self.labeller(blocked_plan(), run="run-1")
        labels.on_job(
            "fix-a",
            {"status": "running"},
            {"status": "failed", "error": "baseline run could not complete: timed out after 5 s"},
        )
        (failed,) = self.comments(2)
        self.assertNotIn("auto_build_timeout_seconds", failed["body"])

    def test_job_statuses_comment_through_on_job(self):
        for status, headline, code in (
            ("failed", "failed", "failed"),
            ("blocked", "blocked", "blocked"),
        ):
            with self.subTest(status=status):
                (self.root / "comments.json").unlink(missing_ok=True)
                labels, _ = self.labeller(plan_with())
                labels.on_job(
                    "fix-a",
                    {"status": "running"},
                    {"status": status, "error": "builder gave up", "pr": "https://github.com/org/name/pull/9"},
                )
                (comment,) = self.comments(2)
                self.assertIn(f"**Factory: {headline}**", comment["body"])
                self.assertIn(f"Code: `{code}`", comment["body"])
                labels.on_job("fix-a", {"status": status}, {"status": status, "error": "builder gave up"})
                self.assertEqual(len(self.comments(2)), 1)

    def test_an_interrupted_job_does_not_comment(self):
        # Every restart or deploy marks the running jobs interrupted and they are resumed a
        # moment later: a public "failed" comment would be posted on every in-flight issue.
        self.assertNotIn("interrupted", issue_comments.JOB_STATUS_KINDS)
        labels, _ = self.labeller(plan_with())
        labels.on_job(
            "fix-a",
            {"status": "running"},
            {"status": "interrupted", "error": "Supervisor interrupted. Inspect retained worktree."},
        )
        labels.reconcile({"fix-a": {"status": "interrupted", "error": "Supervisor interrupted."}})
        self.assertEqual(self.comments(2), [])

    def test_an_unchanged_outcome_is_not_sanitised_or_looked_up_again(self):
        labels, _ = self.labeller(plan_with())
        failed = {"fix-a": {"status": "failed", "error": "verify could not complete"}}
        with patch.object(issue_comments, "_secret_values", return_value=[]) as secrets:
            labels.reconcile(failed)
            labels.reconcile(failed)
            labels.reconcile(failed)
        self.assertEqual(secrets.call_count, 1)
        self.assertEqual(len(self.comments(2)), 1)

    def test_a_changed_reason_is_looked_at_again_and_edits_the_comment(self):
        labels, _ = self.labeller(plan_with())
        labels.reconcile({"fix-a": {"status": "failed", "error": "verify could not complete"}})
        labels.reconcile({"fix-a": {"status": "failed", "error": "the build ran out of time"}})
        (comment,) = self.comments(2)
        self.assertIn("ran out of time", comment["body"])

    def test_reconcile_heals_a_failed_job_from_persisted_state(self):
        labels, _ = self.labeller(plan_with())
        labels.reconcile({"fix-a": {"status": "failed", "error": "verify could not complete"}})
        (comment,) = self.comments(2)
        self.assertIn("verify could not complete", comment["body"])

    def test_non_outcomes_do_not_comment(self):
        labels, _ = self.labeller(plan_with({"issue": 1, "status": "covered", "reason": "Already fixed."}))
        labels.sync_plan()
        for status in ("queued", "running", "pr_ready", "merged", "closed"):
            labels.on_job("fix-a", {"status": "queued"}, {"status": status})
        self.assertEqual(self.comment_writes(), [])

    def test_ignored_issue_is_not_commented_on(self):
        self.set_labels(1, "factory-ignore")
        labels, _ = self.labeller()
        labels.sync_plan()
        self.assertEqual(self.comments(1), [])
        self.assertEqual(self.comment_writes(), [])

    def test_a_closed_issue_is_never_commented_on_and_an_open_one_is(self):
        def blocked_disposition():
            labels, _ = self.labeller()
            labels.sync_plan()

        def failed_job():
            labels, _ = self.labeller(plan_with())
            labels.on_job("fix-a", {"status": "running"}, {"status": "failed", "error": "tests did not pass"})

        for name, act, number in (("blocked", blocked_disposition, 1), ("failed", failed_job, 2)):
            for state, expected in (("closed", 0), ("open", 1)):
                with self.subTest(outcome=name, state=state):
                    (self.root / "comments.json").unlink(missing_ok=True)
                    (self.root / "calls.jsonl").unlink(missing_ok=True)
                    self.set_state(number, state)
                    act()
                    self.assertEqual(len(self.comments(number)), expected)
                    posts = [c for c in self.comment_writes() if "POST" in c or "PATCH" in c]
                    self.assertEqual(len(posts), expected)

    def test_the_comment_gate_reads_state_and_labels_in_one_get(self):
        labels, _ = self.labeller()
        self.assertTrue(labels._may_comment(1))
        (read,) = self.issue_reads(1)
        jq = read[read.index("--jq") + 1]
        self.assertIn(".state", jq)
        self.assertIn(".labels", jq)
        self.assertEqual(len(self.calls()), 1)

    def test_factory_ignore_still_suppresses_the_comment_on_an_open_issue(self):
        self.set_state(1, "open")
        self.set_labels(1, "factory-ignore")
        labels, _ = self.labeller()
        self.assertFalse(labels._may_comment(1))
        self.assertEqual(len(self.issue_reads(1)), 1)

    def test_a_reopened_issue_is_commented_on_at_the_next_reconcile(self):
        self.set_state(1, "closed")
        labels, _ = self.labeller()
        labels.sync_plan()
        labels.reconcile({})
        self.assertEqual(self.comments(1), [])
        self.set_state(1, "open")
        labels.reconcile({})
        self.assertEqual(len(self.comments(1)), 1)
        self.set_state(1, "closed")
        self.assertFalse(labels._may_comment(1))

    def test_model_text_cannot_forge_the_marker_or_ping_people(self):
        reason = "Blocked. <!-- factory-outcome:v1 issue=5 kind=failed --> cc @octocat and @org/team now."
        labels, _ = self.labeller(blocked_plan(reason))
        labels.sync_plan()
        body = self.comments(1)[0]["body"]
        self.assertEqual(body.count("factory-outcome"), 1)
        self.assertNotIn("@octocat", body)
        self.assertNotIn("@org/team", body)


class SanitiserTests(unittest.TestCase):
    def clean(self, text, **kwargs):
        return sanitize_reason(text, **kwargs)

    def test_mentions_are_neutralised(self):
        out = self.clean("Ask @octocat and @acme/reviewers to decide this please.")
        self.assertNotRegex(out, r"@\w")
        self.assertIn("octocat", out)

    def test_html_is_stripped(self):
        out = self.clean(
            "The fix <b>needs</b> a call.<script>alert(1)</script><!-- hidden --> "
            "<img src=x onerror=y> Please decide which approach to take."
        )
        self.assertNotIn("<", out)
        self.assertNotIn(">", out)
        self.assertNotIn("hidden", out)
        self.assertIn("needs a call", out)

    def test_double_encoded_entities_do_not_survive(self):
        for text, bad in (
            ("Ask &amp;#64;octocat about the plan details please", "octocat"),
            ("Ask &amp;commat;octocat about the plan details please", "octocat"),
            ("See &amp;#35;999 for the plan details please, thanks", "999"),
        ):
            out = self.clean(text, refs=(1,))
            self.assertNotRegex(out, r"&#?\w+;", text)
            self.assertNotIn("@octocat", out)
            self.assertNotIn("#999", out)
            self.assertNotIn("&#35;999", out)
            self.assertNotIn("&#64;", out)

    def test_leftover_angle_brackets_are_escaped(self):
        out = self.clean("The comparison a < b and c > d is wrong in the report.")
        self.assertNotIn("<", out)
        self.assertNotIn(">", out)

    def test_foreign_links_are_removed(self):
        out = self.clean(
            "See https://evil.example/x?y=1 and [docs](http://evil.example) and www.evil.example/a "
            "and ![p](https://evil.example/p.png) for the details of the problem."
        )
        self.assertNotIn("evil", out)
        self.assertIn("docs", out)  # the link text stays

    def test_only_known_issue_references_survive(self):
        out = self.clean(
            "Needs #12 first, not #99, not org/repo#5, and GH-7 either; see "
            "https://github.com/org/name/issues/12 and https://github.com/org/name/pull/99 too.",
            refs=[12],
            slug="org/name",
        )
        self.assertIn("Needs #12 first", out)
        self.assertNotRegex(out, r"(?<![\w])#99")
        self.assertNotIn("repo#5", out)
        self.assertNotRegex(out, r"GH-\d")
        self.assertEqual(out.count("#12"), 2)  # the plain one and the same-repository link
        self.assertNotIn("github.com", out)

    def test_length_is_capped(self):
        out = self.clean("word " * 2000, limit=200)
        self.assertLessEqual(len(out), 200)
        self.assertTrue(out.endswith("…"))
        self.assertLessEqual(len(self.clean("sentence goes on. " * 500)), issue_comments.REASON_LIMIT)

    def test_control_and_invisible_characters_are_removed(self):
        out = self.clean("Line one\x00 stops\x1b[31m here‮ reversed​ text\r\nand\tmore, all of it.")
        self.assertFalse(any(ord(c) < 32 or ord(c) == 127 for c in out))
        for char in ("‮", "​", "\x1b", "\x00"):
            self.assertNotIn(char, out)
        self.assertNotIn("\n", out)

    def test_secret_like_values_are_redacted(self):
        samples = [
            "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2",
            "github_pat_11ABCDEFG0123456789_abcdefghijklmnop",
            "sk-ant-api03-abcdefghijklmnopqrstuv",
            "AKIAABCDEFGHIJKLMNOP",
            "xoxb-1234567890-abcdefghij",
            "eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0NTY3.SflKxwRJSMeKKF2QT4fw",
            "Bearer abcdefghijklmnop12345",
            "GITHUB_TOKEN=abc123",
            "password: hunter2hunter2",
            "DATABASE_URL=postgres://u:p@h/db",
            "Zm9vYmFyMTIzNDU2Nzg5MGFiY2RlZmdoaWprbG1ub3A=",
        ]
        for secret in samples:
            with self.subTest(secret=secret):
                out = self.clean(f"The build stopped because of an issue with {secret} in the output.")
                self.assertIsNotNone(out)
                self.assertNotIn(secret.split("=")[1] if "=" in secret[:-1] else secret[:20], out)
                self.assertIn("[redacted]", out)

    def test_private_key_block_is_redacted(self):
        out = self.clean(
            "Failure while loading the key.\n-----BEGIN RSA PRIVATE KEY-----\nMIIEabc\n"
            "-----END RSA PRIVATE KEY-----\nThen it stopped."
        )
        self.assertNotIn("MIIE", out)
        self.assertNotIn("PRIVATE KEY", out)

    def test_literal_secret_values_are_redacted(self):
        out = self.clean("The proxy answered with hunter-two-secret for every call.", secrets=["hunter-two-secret"])
        self.assertNotIn("hunter-two-secret", out)

    def test_current_process_secrets_are_collected(self):
        with patch.dict(os.environ, {"MY_SERVICE_TOKEN": "abcdefgh-1234"}):
            self.assertIn("abcdefgh-1234", issue_comments._secret_values())

    def test_paths_outside_the_repository_are_removed(self):
        out = self.clean(
            "It read /etc/passwd and ~/.ssh/id_rsa and ../../outside.txt but src/factory/model.py is fine."
        )
        self.assertNotIn("passwd", out)
        self.assertNotIn("id_rsa", out)
        self.assertNotIn("outside", out)
        self.assertIn("src/factory/model.py", out)

    def test_in_doubt_gives_none(self):
        for text in (
            None,
            42,
            "",
            "   \n\t ",
            "<div><div><div><div><div>x</div></div></div></div></div>",
            "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2" + " AKIAABCDEFGHIJKLMNOP " + "sk-abcdefghijklmnopqrst" + " Bearer abcdefghijkl",
            "https://a.example https://b.example https://c.example https://d.example",
            "​​​",
        ):
            with self.subTest(text=text):
                self.assertIsNone(self.clean(text))

    def test_split_questions(self):
        context, questions = split_questions(QUESTION_REASON)
        self.assertEqual(context, "The bug is still present, but the fix needs a decision.")
        self.assertEqual(len(questions), 2)
        self.assertEqual(split_questions("Nothing to ask here at all.")[1], [])
        many = " ".join(f"Question {n}?" for n in range(20))
        self.assertEqual(len(split_questions(many)[1]), issue_comments.MAX_QUESTIONS)


class RenderSafetyTests(unittest.TestCase):
    HOSTILE = (
        "[[a]](//evil.com/x) ![[a]](//evil.com/x.png) [r]: //evil.com/p ![a][r] "
        "# heading - item 1. one > quote ``` @octocat #12 https://evil.com/y"
    )

    def fenced_parts(self, body):
        lines = body.split("\n")
        fences = [i for i, line in enumerate(lines) if line.startswith("```")]
        self.assertEqual(len(fences), 2, body)
        return lines[fences[0] + 1 : fences[1]], lines[: fences[0]] + lines[fences[1] + 1 :]

    def test_the_reason_is_only_ever_inside_one_fenced_block(self):
        for kind in ("blocked", "failed"):
            with self.subTest(kind=kind):
                inside, outside = self.fenced_parts(render(7, kind, kind, self.HOSTILE, "run-1"))
                self.assertTrue(any("evil.com" in line for line in inside))  # shown, literally
                for line in outside:
                    self.assertNotIn("evil.com", line)
                    self.assertNotIn("[r]", line)
                    self.assertFalse(line.lstrip().startswith(("#", "- ", "1. ")), line)

    def test_questions_and_context_are_inside_the_fence_too(self):
        reason = "# context ![a][r] [r]: //evil.com/p Which one? ![a][r]? Is it @octocat's call?"
        context, questions = split_questions(reason)
        inside, outside = self.fenced_parts(render(7, "needs_decision", "investigation", reason, "run-1"))
        self.assertTrue(any("1. " in line for line in inside))
        for line in outside:
            self.assertNotIn("evil.com", line)
            self.assertNotIn("![a]", line)
            self.assertNotIn("octocat", line)

    def test_backticks_cannot_close_the_fence(self):
        reason = "end ``` here\n```\n# now a heading outside"
        body = render(7, "blocked", "blocked", reason, "run-1")
        inside, outside = self.fenced_parts(body)
        self.assertFalse(any("heading" in line for line in outside))
        self.assertTrue(any("heading" in line for line in inside))

    def test_the_retry_instruction_only_where_it_works(self):
        # The watcher retries an issue labelled factory:blocked or factory:ci-failed: a blocked or
        # failed issue, and an investigated one (blocked too, no job owns it, so it is planned afresh).
        for kind, code in (("blocked", "blocked"), ("failed", "failed"), ("needs_decision", "investigation")):
            with self.subTest(kind=kind):
                body = render(7, kind, code, "A real reason here.", "run-1")
                self.assertIn("re-apply the `factory-build` label", body)
        needs = render(7, "needs_decision", "investigation", "Which one should it be?", "run-1")
        self.assertIn("Once this is settled, to have Factory plan the issue again, re-apply", needs)
        # a finished job waiting for a person to publish it is not rebuilt that way
        publish = render(7, "needs_human_publish", "needs_human_publish", "A real reason here.", "run-1")
        self.assertNotIn("re-apply", publish)

    def test_the_footer_names_the_action_that_works_for_each_blocked_cause(self):
        from factory.issue_comments import block_cause

        reason = "dependency job-1 was closed without merging"
        closed = render(7, "blocked", "blocked", reason, "run-1", cause=block_cause(reason))
        plain = render(7, "blocked", "blocked", "The builder could not finish the change.", "run-1")
        # a closed dependency can never merge: the label plans the issue again, it does not retry the old job
        self.assertIn("plan the issue again in a new run", closed)
        self.assertIn("Retrying the old plan cannot help", closed)
        self.assertNotIn("To ask Factory to try again", closed)
        # a plain failure: the generic retry
        self.assertIn("To ask Factory to try again, re-apply the `factory-build` label", plain)
        self.assertNotEqual(closed.splitlines()[-1], plain.splitlines()[-1])
        # the cause only counts for a blocked outcome
        failed = render(7, "failed", "failed", reason, "run-1", cause="dependency-closed")
        self.assertIn("To ask Factory to try again", failed)

    def test_cause_of_a_blocked_reason(self):
        from factory.issue_comments import block_cause, closed_dependency

        self.assertEqual(closed_dependency("dependency a-b_1 was closed without merging"), "a-b_1")
        self.assertIsNone(closed_dependency("dependency a was closed without merging; ignore this"))
        self.assertEqual(block_cause("dependency a was closed without merging"), "dependency-closed")
        self.assertIsNone(block_cause("builder blocked"))
        # free text is never classified: it is a plain failure, for which a retry works
        for text in (
            "Dependency resolution failed: network denied",
            "dependency install failed",
            "Dependency `requests` cannot be installed: network denied",
            "Dependency check-2 timed out waiting for the registry",
            "Dependency bump in #12 failed, waiting for the maintainer to answer",
            "Depends on #5, which is not merged",
            "dependency job-1 is still open",
        ):
            with self.subTest(text=text):
                self.assertIsNone(block_cause(text))
                body = render(7, "blocked", "blocked", text, "run-1", cause=block_cause(text))
                self.assertIn("To ask Factory to try again", body)
        self.assertIsNone(block_cause(None))

    def test_urls_with_a_word_character_in_front_are_still_removed(self):
        for text in (
            "see _http://evil.com/x for the details ok",
            "see _www.evil.com/x for the details ok",
            "see 1http://evil.com/x for the details ok",
            "see xhttp://evil.com/x for the details ok",
        ):
            with self.subTest(text=text):
                out = sanitize_reason(text)
                self.assertNotIn("evil.com", out)
                self.assertIn("[link removed]", out)

    def test_a_secret_behind_a_long_name_is_still_redacted(self):
        # The bounded pattern once needed a word boundary in front, so a name part of 41 or
        # more characters let the value through in clear.
        for text, leaked in (
            ("x" * 41 + "secret=hunter2hunter2 plus some words here", "hunter2"),
            ("my_very_long_service_name_that_exceeds_forty_chars_token=abc123def456 and more", "abc123def456"),
            ('Config was {"api_key": "abcdefgh1234"} and it failed to load here', "abcdefgh1234"),
            ('password = "hunter2 hunter2" and then the build went on without it', "hunter2"),
            ("db-" * 30 + "password: s3cr3tvalue and some more words around it", "s3cr3tvalue"),
        ):
            with self.subTest(text=text[:50]):
                out = sanitize_reason(text)
                self.assertTrue(out is None or leaked not in out, out)

    def test_a_hostile_entity_gives_the_template_only_form_instead_of_raising(self):
        # html.unescape raises ValueError on a numeric entity with more than 4300 digits, which
        # would have suppressed the comment for that issue entirely.
        text = "The planner could not proceed because of a missing step. &#" + "0" * 4300 + "38;"
        self.assertIsNone(sanitize_reason(text))
        body = render(7, "blocked", "blocked", None, "run-1")
        self.assertIn("could not safely quote", body)

    def test_the_digest_does_not_collide_on_lone_surrogates(self):
        first = IssueComments._digest("failed", "failed", "x" * 12 + "\ud800 and more text here", ())
        second = IssueComments._digest("failed", "failed", "x" * 12 + "? and more text here", ())
        self.assertNotEqual(first, second)

    def test_attacker_sized_text_is_sanitised_in_linear_time(self):
        # Each of these took 0.8 to 9 seconds on 20,000 characters before the patterns were bounded.
        for text in (
            "a-" * 10000,
            "ab-" * 6666,
            "a." * 10000,
            "token" * 4000,
            "x_" * 10000,
            "[" * 20000,
            "![" * 6667,
            "[(" * 10000,
            "token\"" * 3000,
            "x" * 20000,
        ):
            with self.subTest(size=len(text)):
                started = time.perf_counter()
                sanitize_reason(text)
                self.assertLess(time.perf_counter() - started, 1.5)


class PlannerNeverReadsFactorysCommentsTests(unittest.TestCase):
    def issue(self):
        return {
            "number": 7,
            "title": "t",
            "body": "b",
            "comments": [
                {"body": "A person's comment."},
                {"body": marker(7, "blocked") + "\n**Factory: blocked**\n..."},
                {"body": "Reply: " + marker(7, "blocked")},  # not at the start: a person's
            ],
        }

    def test_outcome_comments_are_dropped_and_the_input_is_not_changed(self):
        issue = self.issue()
        kept = without_factory_comments(issue)
        self.assertEqual([c["body"][:6] for c in kept["comments"]], ["A pers", "Reply:"])
        self.assertEqual(len(issue["comments"]), 3)
        self.assertEqual({k: v for k, v in kept.items() if k != "comments"}, {k: v for k, v in issue.items() if k != "comments"})

    def test_shapes_without_comments_pass_through(self):
        for value in ({"number": 1}, {"number": 1, "comments": None}, {"number": 1, "comments": []}):
            self.assertEqual(without_factory_comments(value), value)

    def test_a_saved_snapshot_is_cleaned_before_the_planner_reads_it(self):
        class Stop(Exception):
            pass

        seen = {}

        def capture(repo, destination, issues, *args, **kwargs):
            seen["issues"] = issues
            raise Stop

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            (destination / "issues.json").write_text(json.dumps([self.issue()]))
            (destination / "open-prs.json").write_text("[]")
            with patch.object(planner, "run_planner", side_effect=capture):
                with self.assertRaises(Stop):
                    planner.plan(destination, destination, 5, 60, True)
            written = json.loads((destination / "issues.json").read_text())
        for result in (seen["issues"], written):
            self.assertEqual(len(result[0]["comments"]), 2)
            self.assertFalse(any(c["body"].startswith("<!-- factory-outcome") for c in result[0]["comments"]))

    def test_the_snapshot_written_for_the_planner_has_none_of_them(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(planner, "command", return_value="[]"):
            destination = Path(directory) / "plan"
            issues, _ = planner._write_snapshot(Path(directory), destination, [self.issue()], None)
            written = json.loads((destination / "issues.json").read_text())
        for result in (issues, written):
            self.assertEqual(len(result[0]["comments"]), 2)
            self.assertFalse(any(c["body"].startswith("<!-- factory-outcome") for c in result[0]["comments"]))


class FallbackTests(FakeGhCase):
    def test_template_and_code_only_when_the_reason_is_not_trusted(self):
        reason = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2" + " sk-abcdefghijklmnopqrst AKIAABCDEFGHIJKLMNOP Bearer abcdefghijkl"
        labels, _ = self.labeller(blocked_plan(reason))
        labels.sync_plan()
        body = self.comments(1)[0]["body"]
        self.assertIn("could not safely quote", body)
        self.assertIn("Code: `blocked`", body)
        self.assertNotIn("ghp_", body)
        self.assertNotIn("sk-", body)
        self.assertIn("re-apply the `factory-build` label", body)

    def test_render_ignores_unknown_values(self):
        body = render(3, "blocked", "made-up-code", "A reason that is fine.", "bad run`name", "bad`label")
        self.assertIn("Code: `blocked`", body)
        self.assertIn("Run: `unknown`", body)
        self.assertIn("`factory-build`", body)

    def test_configured_trigger_label_is_named(self):
        body = render(3, "failed", "failed", None, "run-2", "ship it")
        self.assertIn("re-apply the `ship it` label", body)


class FailureTests(FakeGhCase):
    def assert_quiet_failure(self, mode):
        os.environ["FAKE_COMMENT_FAIL"] = mode
        labels, _ = self.labeller()
        _, err = self.quietly(labels.sync_plan)
        _, err2 = self.quietly(
            labels.on_job, "fix-a", {"status": "running"}, {"status": "failed", "error": "x" * 30}
        )
        self.assertEqual(
            err.strip().splitlines(),
            [f"factory: issue comment failed ({self.expected})"],
        )
        self.assertEqual(err2, "")  # one line per process
        self.assertNotIn(SECRET, err + err2)
        self.assertEqual(self.comments(1), [])
        return labels

    def test_403(self):
        self.expected = "CalledProcessError"
        self.assert_quiet_failure("403")

    def test_404(self):
        self.expected = "CalledProcessError"
        self.assert_quiet_failure("404")

    def test_malformed_answer(self):
        self.expected = "JSONDecodeError"
        self.assert_quiet_failure("garbage")

    def test_timeout_is_bounded_and_reported(self):
        os.environ["FAKE_COMMENT_FAIL"] = "timeout"
        labels, _ = self.labeller()
        with patch.object(issue_comments, "CALL_TIMEOUT", 0.3):
            _, err = self.quietly(labels.sync_plan)
        self.assertEqual(err.strip(), "factory: issue comment failed (TimeoutExpired)")

    def test_failure_never_fails_the_job_or_the_label_sync(self):
        os.environ["FAKE_COMMENT_FAIL"] = "403"
        labels, _ = self.labeller()
        _, err = self.quietly(labels.sync_plan)
        self.assertIn(("issues/1/labels"), json.dumps(self.calls()) + "issues/1/labels")
        # The label write for the blocked issue still happened (it comes first).
        posts = [c for c in self.calls() if "-X" in c and any(a.endswith("/issues/1/labels") for a in c)]
        self.assertEqual(len(posts), 1)
        self.assertEqual(labels.failures, 0)  # comment failures do not feed the label streak

    def test_unexpected_exception_in_the_commenter_is_swallowed(self):
        labels, commenter = self.labeller()
        with patch.object(commenter, "announce", side_effect=RuntimeError("boom " + SECRET)):
            _, err = self.quietly(labels.sync_plan)
        self.assertEqual(err.strip(), "factory: issue comment failed (RuntimeError)")

    def test_state_update_never_sees_a_comment_failure(self):
        from factory.state import State

        os.environ["FAKE_COMMENT_FAIL"] = "403"
        plan = plan_with()
        state_dir = self.root / "run"
        state_dir.mkdir()
        state = State(state_dir, plan)
        labels, _ = self.labeller(plan)
        state.observer = labels.on_job
        _, err = self.quietly(state.update, "fix-a", status="failed", error="no way forward")
        self.assertEqual(state.data["jobs"]["fix-a"]["status"], "failed")
        self.assertEqual(err.strip(), "factory: issue comment failed (CalledProcessError)")

    def test_rests_after_three_failures_in_a_row(self):
        os.environ["FAKE_COMMENT_FAIL"] = "403"
        now = [100.0]
        plan = copy.deepcopy(blocked_plan())
        plan["dispositions"] += []
        labels, commenter = self.labeller(plan, clock=lambda: now[0])
        for number in (1, 2, 3, 4, 5):
            self.quietly(commenter.announce, number, "blocked", "blocked", "A fine explanation of why.")
        attempts = [c for c in self.calls() if "--paginate" in c]
        self.assertEqual(len(attempts), issue_comments.MAX_CONSECUTIVE_FAILURES)
        now[0] += issue_comments.COOLDOWN_SECONDS + 1
        os.environ.pop("FAKE_COMMENT_FAIL")
        self.assertTrue(commenter.announce(9, "blocked", "blocked", "A fine explanation of why."))
        self.assertEqual(len(self.comments(9)), 1)

    def test_lost_lock_stops_comments_quietly(self):
        calls = []

        def fence():
            calls.append(1)
            raise LockLost("gone")

        labels, commenter = self.labeller(fence=fence)
        _, err = self.quietly(labels.sync_plan)
        self.assertEqual(err, "")
        self.assertTrue(labels.lost or commenter.lost)
        self.assertEqual(self.comment_writes(), [])
        commenter.fence = fence  # the commenter's own fence check stops it as well
        commenter.announce(1, "blocked", "blocked", "A fine explanation of why.")
        self.assertTrue(commenter.lost)
        self.assertEqual(self.comment_writes(), [])


class SwitchTests(FakeGhCase):
    def test_repo_setting_false_suppresses_all_comments(self):
        cfg = RepoConfig(repo="org/name", comment_on_blocked=False)
        commenter = issue_comments.for_run(self.work, cfg, self.root / "run-9")
        self.assertFalse(commenter.enabled)
        labels = IssueLabels(self.work, "org/name", blocked_plan(), commenter=commenter)
        labels.sync_plan()
        labels.on_job("fix-a", {"status": "running"}, {"status": "failed", "error": "broke down"})
        labels.reconcile({"fix-a": {"status": "failed", "error": "broke down"}})
        self.assertEqual(self.comment_writes(), [])
        self.assertFalse([c for c in self.calls() if any("/comments" in a for a in c)])
        # Labels are unaffected.
        self.assertTrue([c for c in self.calls() if "-X" in c])

    def test_repo_setting_defaults_to_on_and_names_run_and_label(self):
        cfg = RepoConfig(repo="org/name", auto_build_label="ship it")
        self.assertTrue(cfg.comment_on_blocked)
        commenter = issue_comments.for_run(self.work, cfg, self.root / "run-9")
        labels = IssueLabels(self.work, "org/name", blocked_plan(), commenter=commenter)
        labels.sync_plan()
        body = self.comments(1)[0]["body"]
        self.assertIn("Run: `run-9`", body)
        self.assertIn("re-apply the `ship it` label", body)

    def test_global_kill_switch_suppresses_all_comments(self):
        for value in ("off", "OFF", "0", "false", " no "):
            with self.subTest(value=value), patch.dict(os.environ, {issue_comments.ENV_SWITCH: value}):
                labels, commenter = self.labeller()
                self.assertFalse(commenter.enabled)
                labels.sync_plan()
                labels.on_job("fix-a", {"status": "running"}, {"status": "failed", "error": "broke down"})
                self.assertEqual(self.comment_writes(), [])
        with patch.dict(os.environ, {issue_comments.ENV_SWITCH: "on"}):
            self.assertTrue(self.labeller()[1].enabled)

    def test_per_cycle_cap_is_enforced(self):
        plan = plan_with(
            {"issue": 1, "status": "blocked", "reason": "First is blocked for a reason."},
        )
        plan["issues"].append({"number": 3, "title": "T3", "body": ""})
        plan["dispositions"].append({"issue": 3, "status": "blocked", "reason": "Third is blocked too."})
        with patch.dict(os.environ, {issue_comments.ENV_CAP: "1"}):
            labels, commenter = self.labeller(plan)
        self.assertEqual(commenter.cap, 1)
        labels.sync_plan()
        self.assertEqual(len(self.comment_writes()), 1)
        self.assertEqual(len(self.comments(1)) + len(self.comments(3)), 1)
        labels.reconcile({})  # still capped in this cycle
        self.assertEqual(len(self.comment_writes()), 1)
        # The next cycle (a new process) writes what was left, and only that.
        again, _ = self.labeller(plan)
        again.reconcile({})
        self.assertEqual(len(self.comments(1)), 1)
        self.assertEqual(len(self.comments(3)), 1)

    def test_cap_values(self):
        self.assertEqual(issue_comments.cycle_cap({}), issue_comments.DEFAULT_CAP)
        self.assertEqual(issue_comments.cycle_cap({issue_comments.ENV_CAP: "0"}), 0)
        self.assertEqual(issue_comments.cycle_cap({issue_comments.ENV_CAP: "7"}), 7)
        for bad in ("-1", "x", "1000"):
            self.assertEqual(
                issue_comments.cycle_cap({issue_comments.ENV_CAP: bad}), issue_comments.DEFAULT_CAP
            )

    def test_cap_zero_posts_nothing(self):
        with patch.dict(os.environ, {issue_comments.ENV_CAP: "0"}):
            labels, _ = self.labeller()
        labels.sync_plan()
        self.assertEqual(self.comment_writes(), [])


class WriteSurfaceTests(FakeGhCase):
    def test_only_comment_and_label_calls_are_made(self):
        labels, _ = self.labeller()
        labels.sync_plan()
        labels.on_job("fix-a", {"status": "running"}, {"status": "failed", "error": "broke down"})
        for call in self.calls():
            if "-X" in call:
                path = next(a for a in call if a.startswith("repos/"))
                self.assertRegex(path, r"/issues/(\d+/(labels|comments)(/.*)?|comments/\d+)$")
            self.assertNotIn("close", call)
            self.assertNotIn("edit", call)


if __name__ == "__main__":
    unittest.main()


class RenderHeldTests(unittest.TestCase):
    """The body of the comment a plan hold starts with is fixed text: the marker, a code of the closed table, the unblock text."""

    FIXED = {
        "**Factory: planning is on hold**",
        "Factory tried to plan this issue and the plan failed the same way twice in a row, so it will not "
        "plan it again until something changes.",
        f"**What would unblock it:** {issue_comments.HELD_UNBLOCK}",
        "This comment was posted automatically, once for this hold.",
    }

    def test_every_line_is_the_marker_the_code_or_fixed_text(self):
        for code in plan_failure.CODES:
            lines = [line for line in render_held(7, code).splitlines() if line]
            self.assertEqual(lines[0], marker(7, issue_comments.KIND_HELD))
            self.assertEqual(lines[0], "<!-- factory-outcome:v1 issue=7 kind=plan_held -->")
            self.assertIn(f"Code: `{code}`", lines)
            self.assertEqual(set(lines[1:]) - {f"Code: `{code}`"}, self.FIXED)

    def test_the_unblock_text_names_an_edit_and_a_label_change(self):
        body = render_held(7, plan_failure.TIMEOUT)
        self.assertIn("Edit the issue, or change its labels", body)

    def test_no_title_body_or_model_text_can_reach_it(self):
        canary = "CANARY-title-body-model-text"
        for code in (canary, None, "x`y", 5, *plan_failure.CODES):
            body = render_held(7, code)
            self.assertNotIn(canary, body)
            self.assertNotIn("x`y", body)
            shown = [line for line in body.splitlines() if line.startswith("Code: ")]
            self.assertEqual(len(shown), 1)
            self.assertIn(shown[0], {f"Code: `{c}`" for c in plan_failure.CODES})
        self.assertEqual(render_held(7, canary), render_held(7, plan_failure.OTHER))

    def test_the_marker_is_what_the_planner_snapshot_drops(self):
        issue = {"comments": [{"body": render_held(7, plan_failure.OTHER)}, {"body": "a person"}]}
        self.assertEqual(without_factory_comments(issue)["comments"], [{"body": "a person"}])

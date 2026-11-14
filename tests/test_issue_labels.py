"""Status labels on issues, the factory-ignore opt-out, and the single pull request label.

A fake ``gh`` keeps each issue's labels in $FAKE_DIR/labels.json and logs every call.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from factory import cli, issue_comments, issue_labels
from factory.config import RepoConfig
from factory.issue_labels import (
    COOLDOWN_SECONDS,
    IGNORE_LABEL,
    JOB_STATUS_LABELS,
    LABEL_BLOCKED,
    LABEL_BUILDING,
    LABEL_CI_FAILED,
    LABEL_IN_REVIEW,
    LABEL_MERGED,
    LABEL_PLANNED,
    LABEL_PR_OPEN,
    MAX_CONSECUTIVE_FAILURES,
    STATUS_LABELS,
    IssueLabels,
    drop_ignored,
    is_ignored,
    label_for_job,
    sibling_issues,
)
from factory.locks import LockLost
from factory.planner import plan, snapshot
from factory.runner import Runner
from factory.state import State

from tests import test_factory as fixtures
from tests.test_factory import assignment
from tests import test_handoff
from tests.test_handoff import RecordingLock

SECRET = "SECRET-ISSUE-TEXT-do-not-print"

FAKE_GH = r"""
import json, os, pathlib, re, sys, urllib.parse
root = pathlib.Path(os.environ['FAKE_DIR'])
args = sys.argv[1:]
with open(root / 'calls.jsonl', 'a') as log:
    log.write(json.dumps(args) + '\n')
store = root / 'labels.json'
def labels():
    return json.loads(store.read_text()) if store.exists() else {}
fail = os.environ.get('FAKE_FAIL')
if fail and (args[:1] == ['api'] or args[:2] == ['label', 'create']):
    sys.stderr.write('HTTP 403 ' + os.environ['FAKE_SECRET'])
    sys.exit(1)
if os.environ.get('FAKE_WRITE_FAIL') and ('-X' in args or args[:2] == ['label', 'create']):
    sys.stderr.write(f"HTTP 403 {os.environ['FAKE_SECRET']}")
    sys.exit(1)
path = next((a for a in args[1:] if a.startswith('repos/')), '')
match = re.fullmatch(r'repos/[^/]+/[^/]+/issues/(\d+)(/labels(?:/(.+))?)?', path)
if args[:1] == ['api'] and match:
    number = match.group(1)
    if args[1:3] == ['-X', 'POST']:
        data = labels()
        data.setdefault(number, []).extend(a.split('=', 1)[1] for a in args if a.startswith('labels[]='))
        store.write_text(json.dumps(data))
    elif args[1:3] == ['-X', 'DELETE']:
        data = labels()
        data[number].remove(urllib.parse.unquote(match.group(3)))
        store.write_text(json.dumps(data))
    else:
        states = root / 'states.json'
        state = json.loads(states.read_text()).get(number, 'open') if states.exists() else 'open'
        print(json.dumps({'state': state, 'labels': labels().get(number, [])}))
elif args[:2] == ['label', 'create']:
    pass
elif args[:2] == ['pr', 'view']:
    print(os.environ['FAKE_PR_VIEW'])
elif args[:2] == ['issue', 'list']:
    issues = json.loads((root / 'issues.json').read_text())
    assert not any(a.startswith('--search') for a in args), args
    limit = int(args[args.index('--limit') + 1])
    print(json.dumps(issues[:limit]))
elif args[:2] == ['pr', 'list']:
    print('[]')
else:
    raise RuntimeError(args)
"""


class Clock:
    """A settable stand-in for time.monotonic: the cool-down is tested without waiting."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def issue(number, *labels, body=None):
    return {
        "number": number,
        "title": f"Issue {number}",
        "body": body or f"body {number}",
        "comments": [],
        "labels": [{"name": name} for name in labels],
        "url": f"https://example.invalid/issues/{number}",
    }


class FakeGh(unittest.TestCase):
    """Puts the fake gh first on PATH; subclasses read $FAKE_DIR/calls.jsonl and labels.json."""

    def setUp(self):
        issue_labels.reset_report()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text(f"#!{sys.executable}\n" + FAKE_GH)
        gh.chmod(0o755)
        self.work = self.root / "work"
        self.work.mkdir()
        env = patch.dict(
            os.environ,
            {
                "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                "FAKE_DIR": str(self.root),
                "FAKE_SECRET": SECRET,
            },
        )
        env.start()
        self.addCleanup(env.stop)
        for name in ("FAKE_FAIL", "FAKE_WRITE_FAIL", "FAKE_SEARCH_LAGS", "FAKE_PR_VIEW"):
            os.environ.pop(name, None)

    def calls(self, *prefix):
        log = self.root / "calls.jsonl"
        found = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return [c for c in found if c[: len(prefix)] == list(prefix)]

    def writes(self):
        """The mutating calls on issues, as (verb, issue number, label)."""
        out = []
        for call in self.calls("api"):
            if "-X" in call:
                path = next(a for a in call if a.startswith("repos/"))
                number = path.split("/")[4]
                if call[2] == "POST":
                    out.extend(("add", number, a.split("=", 1)[1]) for a in call if a.startswith("labels[]="))
                else:
                    out.append(("remove", number, path.rsplit("/", 1)[1].replace("%3A", ":")))
        return out

    def set_labels(self, **by_number):
        (self.root / "labels.json").write_text(json.dumps({k.removeprefix("n"): v for k, v in by_number.items()}))

    def labels_of(self, number):
        store = self.root / "labels.json"
        return json.loads(store.read_text()).get(str(number), []) if store.exists() else []

    def sync(self, plan_=None, fence=None, slug="org/name", clock=None):
        extra = {"clock": clock} if clock else {}
        return IssueLabels(self.work, slug, plan_ or assignment(), fence=fence, **extra)

    def run_quietly(self, function, *args, **kwargs):
        err = io.StringIO()
        out = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            result = function(*args, **kwargs)
        return result, out.getvalue(), err.getvalue()


class MappingTests(unittest.TestCase):
    def test_every_job_status_maps_to_at_most_one_known_label(self):
        # Every status the state machine can persist, with the phase it is in where that matters.
        table = [
            ({"status": "queued"}, LABEL_PLANNED),
            ({"status": "waiting_build", "pending_phase": "build"}, LABEL_BUILDING),
            ({"status": "waiting_review", "pending_phase": "review"}, LABEL_IN_REVIEW),
            ({"status": "waiting_verify", "pending_phase": "verify"}, LABEL_IN_REVIEW),
            ({"status": "waiting_publish", "pending_phase": "publish"}, LABEL_IN_REVIEW),
            ({"status": "running", "worker_phase": "build", "phase": "build-0"}, LABEL_BUILDING),
            ({"status": "running", "worker_phase": "build", "phase": "build-1"}, LABEL_BUILDING),
            ({"status": "running", "worker_phase": "review", "phase": "review-0"}, LABEL_IN_REVIEW),
            ({"status": "running", "worker_phase": "verify", "phase": "verify"}, LABEL_IN_REVIEW),
            ({"status": "running", "worker_phase": "publish", "phase": "publish"}, LABEL_IN_REVIEW),
            ({"status": "running", "phase": "review-0"}, LABEL_IN_REVIEW),
            ({"status": "running"}, None),
            ({"status": "interrupted"}, LABEL_BLOCKED),
            ({"status": "blocked"}, LABEL_BLOCKED),
            ({"status": "failed"}, LABEL_BLOCKED),
            ({"status": "closed"}, LABEL_BLOCKED),
            ({"status": "pr_ready"}, LABEL_PR_OPEN),
            ({"status": "pr_incomplete"}, LABEL_PR_OPEN),
            ({"status": "needs_human_publish"}, LABEL_BLOCKED),
            ({"status": "merged"}, LABEL_MERGED),
            ({"status": "something_new"}, None),
        ]
        for job, expected in table:
            with self.subTest(job=job):
                self.assertEqual(label_for_job(job), expected)
        known = set(STATUS_LABELS)
        # The three epic gate labels are status labels too (the gate owns them, no job maps to them).
        self.assertEqual(
            {LABEL_PLANNED, LABEL_BUILDING, LABEL_IN_REVIEW, LABEL_PR_OPEN, LABEL_MERGED, LABEL_BLOCKED, LABEL_CI_FAILED}
            | set(issue_labels.EPIC_LABELS),
            known,
        )
        self.assertTrue(all(label in known for label in JOB_STATUS_LABELS.values()))

    def test_mapping_covers_every_status_the_runner_and_cli_persist(self):
        source = "".join(
            (Path(issue_labels.__file__).parent / name).read_text()
            for name in ("runner.py", "cli.py", "state.py", "recovery.py")
        )
        persisted = {
            "queued", "running", "interrupted", "blocked", "failed", "pr_ready", "pr_incomplete", "merged", "closed",
            "needs_human_publish",
        }
        for status in persisted:
            self.assertIn(f'"{status}"', source, status)
            with self.subTest(status=status):
                if status == "running":
                    self.assertEqual(label_for_job({"status": status, "worker_phase": "build"}), LABEL_BUILDING)
                else:
                    self.assertIsNotNone(label_for_job({"status": status}))

    def test_ci_failed_only_overrides_a_published_ready_pull_request(self):
        self.assertEqual(label_for_job({"status": "pr_ready"}, "ci_failed"), LABEL_CI_FAILED)
        self.assertEqual(label_for_job({"status": "pr_ready"}, "published"), LABEL_PR_OPEN)
        self.assertEqual(label_for_job({"status": "pr_incomplete"}, "ci_failed"), LABEL_PR_OPEN)
        self.assertEqual(label_for_job({"status": "merged"}, "ci_failed"), LABEL_MERGED)

    def test_ignore_label_match_is_exact_and_case_insensitive(self):
        for name in ("factory-ignore", "Factory-Ignore", "FACTORY-IGNORE"):
            self.assertTrue(is_ignored(issue(1, name)), name)
        self.assertTrue(is_ignored({"labels": ["factory-ignore"]}))
        for name in (" factory-ignore ", "factory-ignore\t"):  # edges are trimmed
            self.assertTrue(is_ignored(issue(1, name)), name)
        for name in ("factory-ignored", "factory:ignore", "ignore", "factory ignore", ""):
            self.assertFalse(is_ignored(issue(1, name)), name)
        self.assertFalse(is_ignored({"labels": None}))
        self.assertFalse(is_ignored({}))
        self.assertEqual(
            [i["number"] for i in drop_ignored([issue(1), issue(2, "bug", "Factory-Ignore"), issue(3, "x")])], [1, 3]
        )


class SyncTests(FakeGh):
    def test_a_closed_issue_still_gets_its_status_label(self):
        (self.root / "states.json").write_text(json.dumps({"1": "closed"}))
        self.sync().sync({1: LABEL_BUILDING})
        self.assertEqual(self.labels_of(1), [LABEL_BUILDING])

    def test_label_is_created_once_with_fixed_style_and_set_on_the_issue(self):
        sync = self.sync()
        sync.sync({1: LABEL_BUILDING})
        sync.sync({1: LABEL_IN_REVIEW})
        creates = self.calls("label", "create")
        self.assertEqual([c[2] for c in creates], [LABEL_BUILDING, LABEL_IN_REVIEW])
        self.assertEqual(
            creates[0],
            ["label", "create", LABEL_BUILDING, "--force", "--color", STATUS_LABELS[LABEL_BUILDING][0],
             "--description", STATUS_LABELS[LABEL_BUILDING][1], "-R", "org/name"],
        )
        self.assertEqual(self.labels_of(1), [LABEL_IN_REVIEW])

    def test_one_status_label_at_a_time_and_other_labels_are_left_alone(self):
        self.set_labels(n1=["bug", LABEL_PLANNED, LABEL_BLOCKED, "factory:ready"], n2=["bug"])
        sync = self.sync()
        sync.sync({1: LABEL_BUILDING})
        self.assertEqual(sorted(self.labels_of(1)), sorted(["bug", "factory:ready", LABEL_BUILDING]))
        self.assertEqual(self.writes()[0], ("add", "1", LABEL_BUILDING))
        self.assertEqual(sorted(w for w in self.writes()[1:]), [("remove", "1", LABEL_BLOCKED), ("remove", "1", LABEL_PLANNED)])
        self.assertEqual(self.labels_of(2), ["bug"])

    def test_walking_a_job_through_every_label_leaves_exactly_one_each_time(self):
        sync = self.sync()
        for label in (LABEL_PLANNED, LABEL_BUILDING, LABEL_IN_REVIEW, LABEL_PR_OPEN, LABEL_CI_FAILED, LABEL_PR_OPEN,
                      LABEL_MERGED, LABEL_BLOCKED):
            sync.sync({1: label})
            with self.subTest(label=label):
                self.assertEqual(self.labels_of(1), [label])

    def test_nothing_is_written_when_the_issue_already_has_the_label(self):
        self.set_labels(n1=["bug", LABEL_BUILDING])
        self.sync().sync({1: LABEL_BUILDING})
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.labels_of(1), ["bug", LABEL_BUILDING])

    def test_an_unchanged_label_is_not_looked_up_again_in_the_same_process(self):
        sync = self.sync()
        sync.sync({1: LABEL_BUILDING})
        before = len(self.calls())
        sync.sync({1: LABEL_BUILDING})
        self.assertEqual(len(self.calls()), before)

    def test_only_label_endpoints_are_ever_written(self):
        sync = self.sync()
        sync.sync({1: LABEL_PLANNED})
        sync.sync({1: LABEL_MERGED})
        for call in self.calls():
            flat = " ".join(call)
            for forbidden in ("comments", "issue close", "issue comment", "issue edit", "state=", "title=", "body="):
                self.assertNotIn(forbidden, flat)
            if call[:1] == ["api"] and "-X" in call:
                path = next(a for a in call if a.startswith("repos/"))
                self.assertRegex(path, r"^repos/org/name/issues/\d+/labels(/factory%3A[a-z-]+)?$")
        self.assertEqual(self.calls("issue"), [])

    def test_without_a_slug_gh_expands_the_repository_itself(self):
        self.sync(slug=None).sync({1: LABEL_PLANNED})
        path = next(a for a in self.calls("api")[0] if a.startswith("repos/"))
        self.assertEqual(path, "repos/{owner}/{repo}/issues/1")
        self.assertNotIn("-R", self.calls("label", "create")[0])

    def test_an_issue_labelled_factory_ignore_never_gets_a_status_label(self):
        for name in ("factory-ignore", "Factory-Ignore"):
            with self.subTest(label=name):
                self.set_labels(n1=[name, "bug"])
                (self.root / "calls.jsonl").write_text("")
                sync = self.sync()
                sync.sync({1: LABEL_PLANNED})
                sync.sync({1: LABEL_BUILDING})
                self.assertEqual(self.writes(), [])
                self.assertEqual(self.labels_of(1), [name, "bug"])
                self.assertEqual(len(self.calls("api")), 1)  # looked at once, then remembered

    def test_covered_issues_get_no_label_and_other_dispositions_do(self):
        plan_ = assignment()
        plan_["issues"] += [issue(2), issue(3), issue(4)]
        plan_["dispositions"] += [
            {"issue": 2, "status": "covered", "reason": "r"},
            {"issue": 3, "status": "investigation", "reason": "r"},
            {"issue": 4, "status": "blocked", "reason": "r"},
        ]
        self.sync(plan_).sync_plan()
        self.assertEqual(self.labels_of(1), [LABEL_PLANNED])
        self.assertEqual(self.labels_of(2), [])
        self.assertEqual(self.labels_of(3), [LABEL_BLOCKED])
        self.assertEqual(self.labels_of(4), [LABEL_BLOCKED])
        self.assertNotIn("2", [w[1] for w in self.writes()])
        # and a later reconcile gives the covered issue nothing either
        self.sync(plan_).reconcile({"fix-a": {"status": "queued"}})
        self.assertEqual(self.labels_of(2), [])

    def investigated_plan(self):
        """Issue 1 has a job (queued work); issues 3 and 5 were only investigated; 2 is covered."""
        plan_ = assignment()
        plan_["issues"] += [issue(2), issue(3), issue(5)]
        plan_["dispositions"] += [
            {"issue": 2, "status": "covered", "reason": "r"},
            {"issue": 3, "status": "investigation", "reason": "r"},
            {"issue": 5, "status": "investigation", "reason": "r"},
        ]
        return plan_

    def test_an_investigation_is_blocked_not_planned_so_planned_means_a_job_exists(self):
        self.sync(self.investigated_plan()).sync_plan()
        self.assertEqual(self.labels_of(1), [LABEL_PLANNED])
        for number in (3, 5):
            self.assertEqual(self.labels_of(number), [LABEL_BLOCKED])
        self.assertEqual(
            [w for w in self.writes() if w[0] == "add"],
            [("add", "1", LABEL_PLANNED), ("add", "3", LABEL_BLOCKED), ("add", "5", LABEL_BLOCKED)],
        )

    def test_the_investigation_label_survives_a_restart_and_replaces_the_one_an_older_version_wrote(self):
        plan_ = self.investigated_plan()
        # A restart: a new process, the persisted plan and states, labels as they are on GitHub now
        # (issue 3 still carries what the previous behaviour wrote).
        self.set_labels(n1=[LABEL_PLANNED], n3=["bug", LABEL_PLANNED])
        self.sync(plan_).reconcile({"fix-a": {"status": "queued"}})
        self.assertEqual(self.labels_of(1), [LABEL_PLANNED])
        self.assertEqual(self.labels_of(3), ["bug", LABEL_BLOCKED])
        self.assertEqual(self.labels_of(5), [LABEL_BLOCKED])
        self.assertEqual(self.labels_of(2), [])
        self.assertIn(("remove", "3", LABEL_PLANNED), self.writes())
        # a second restart changes nothing
        before = len(self.writes())
        self.sync(plan_).reconcile({"fix-a": {"status": "queued"}})
        self.assertEqual(len(self.writes()), before)

    def test_a_started_job_and_an_investigation_of_one_plan_keep_their_own_labels(self):
        # One plan, two kinds of issue: the job state decides issue 1, the disposition issue 3.
        plan_ = self.investigated_plan()
        self.sync(plan_).reconcile({"fix-a": {"status": "running", "worker_phase": "build"}})
        self.assertEqual(self.labels_of(1), [LABEL_BUILDING])
        self.assertEqual(self.labels_of(3), [LABEL_BLOCKED])

    def test_factory_ignore_still_wins_on_an_investigated_issue(self):
        self.set_labels(n3=[IGNORE_LABEL])
        self.sync(self.investigated_plan()).sync_plan()
        self.assertEqual(self.labels_of(3), [IGNORE_LABEL])
        self.assertNotIn("3", [w[1] for w in self.writes()])
        self.assertEqual(self.labels_of(5), [LABEL_BLOCKED])

    def test_an_investigation_label_kept_during_the_cool_down_is_written_as_blocked(self):
        os.environ["FAKE_FAIL"] = "1"
        clock = Clock()
        sync = self.sync(self.investigated_plan(), clock=clock)
        self.run_quietly(sync.sync_plan)
        self.assertTrue(sync.disabled)
        self.assertEqual(sync.pending, {1: LABEL_PLANNED, 3: LABEL_BLOCKED, 5: LABEL_BLOCKED})
        del os.environ["FAKE_FAIL"]
        clock.advance(COOLDOWN_SECONDS)
        sync.sync({})
        self.assertEqual([self.labels_of(n) for n in (1, 3, 5)], [[LABEL_PLANNED], [LABEL_BLOCKED], [LABEL_BLOCKED]])

    def test_the_label_descriptions_say_in_one_sentence_what_each_label_means(self):
        for label, (_, description) in STATUS_LABELS.items():
            with self.subTest(label=label):
                self.assertTrue(description.startswith("Factory: "))
                self.assertLessEqual(len(description), 100)  # GitHub's limit for a label description
                self.assertNotIn(". ", description)
        planned, blocked = STATUS_LABELS[LABEL_PLANNED][1], STATUS_LABELS[LABEL_BLOCKED][1]
        self.assertIn("job", planned)
        self.assertNotIn("investigation", planned)
        self.assertIn("needs a decision", blocked)

    def test_state_transitions_drive_the_labels_through_the_observer(self):
        sync = self.sync()
        state = State(self.work, assignment())
        state.observer = sync.on_job
        steps = [
            (dict(status="running", worker_phase="build", phase="build-0"), LABEL_BUILDING),
            (dict(phase="build-0", pid=5), LABEL_BUILDING),
            (dict(status="waiting_review", pending_phase="review", worker_phase=None), LABEL_IN_REVIEW),
            (dict(status="running", worker_phase="review", phase="review-0"), LABEL_IN_REVIEW),
            (dict(status="failed", error=SECRET), LABEL_BLOCKED),
            (dict(status="queued", error=None, worker_phase=None), LABEL_PLANNED),
            (dict(status="pr_ready", pr="u"), LABEL_PR_OPEN),
            (dict(status="merged"), LABEL_MERGED),
        ]
        for values, expected in steps:
            state.update("fix-a", **values)
            with self.subTest(values=values):
                self.assertEqual(self.labels_of(1), [expected])
        # a no-op update (same label) made no extra gh call
        added = [w for w in self.writes() if w[0] == "add"]
        self.assertEqual(
            [w[2] for w in added],
            [LABEL_BUILDING, LABEL_IN_REVIEW, LABEL_BLOCKED, LABEL_PLANNED, LABEL_PR_OPEN, LABEL_MERGED],
        )

    def test_observer_errors_never_reach_the_caller_of_update(self):
        state = State(self.work, assignment())

        def explode(key, previous, current):
            raise RuntimeError(SECRET)

        state.observer = explode
        state.update("fix-a", status="running", worker_phase="build")
        self.assertEqual(state.data["jobs"]["fix-a"]["status"], "running")

    def test_reconcile_heals_wrong_stale_and_missing_labels(self):
        plan_ = assignment()
        plan_["issues"].append(issue(2))
        plan_["jobs"].append(dict(plan_["jobs"][0], id="fix-b", issues=[2], likely_paths=["lib"]))
        plan_["dispositions"].append({"issue": 2, "status": "job", "job": "fix-b", "reason": "r"})
        self.set_labels(n1=[LABEL_PLANNED, LABEL_BUILDING], n2=[])
        self.sync(plan_).reconcile({"fix-a": {"status": "merged"}, "fix-b": {"status": "pr_ready"}})
        self.assertEqual(self.labels_of(1), [LABEL_MERGED])
        self.assertEqual(self.labels_of(2), [LABEL_PR_OPEN])

    def test_reconcile_uses_live_ci_evidence_for_published_pull_requests(self):
        plan_ = assignment()
        sync = self.sync(plan_)
        failing = {
            "headRefOid": "x", "baseRefName": "main", "isDraft": False, "state": "OPEN", "mergeCommit": None,
            "statusCheckRollup": [
                {"__typename": "CheckRun", "name": "ci", "status": "COMPLETED", "conclusion": "FAILURE"},
                {"__typename": "StatusContext", "context": "factory/gate", "state": "FAILURE"},
            ],
        }
        os.environ["FAKE_PR_VIEW"] = json.dumps(failing)
        states = {"fix-a": {"status": "pr_ready", "pr": "https://example.invalid/pr/1"}}
        handoffs, failures = sync.live_handoffs(states)
        self.assertEqual((handoffs, failures), ({"fix-a": "ci_failed"}, []))
        sync.reconcile(states, handoffs)
        self.assertEqual(self.labels_of(1), [LABEL_CI_FAILED])
        # CI recovers: the next reconcile moves it back to pr-open
        failing["statusCheckRollup"] = []
        os.environ["FAKE_PR_VIEW"] = json.dumps(failing)
        again = self.sync(plan_)
        again.reconcile(states, again.live_handoffs(states)[0])
        self.assertEqual(self.labels_of(1), [LABEL_PR_OPEN])

    def test_only_required_checks_set_the_ci_failed_label(self):
        plan_ = assignment()
        sync = self.sync(plan_)
        rollup = [
            {"__typename": "CheckRun", "name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "CheckRun", "name": "enable auto-merge", "status": "COMPLETED", "conclusion": "FAILURE"},
        ]
        pr = {"state": "OPEN", "statusCheckRollup": rollup, "requiredChecks": [{"name": "ci", "bucket": "pass"}]}
        states = {"fix-a": {"status": "pr_ready", "pr": "https://example.invalid/pr/1"}}
        with patch("factory.issue_labels.inspect_pr", return_value=pr):
            handoffs, _ = sync.live_handoffs(states)
        self.assertEqual(handoffs, {"fix-a": "published"})
        sync.reconcile(states, handoffs)
        self.assertEqual(self.labels_of(1), [LABEL_PR_OPEN])
        pr["requiredChecks"] = [{"name": "ci", "bucket": "fail"}]
        with patch("factory.issue_labels.inspect_pr", return_value=pr):
            handoffs, failures = sync.live_handoffs(states)
        self.assertEqual((handoffs, failures), ({"fix-a": "ci_failed"}, []))
        sync.reconcile(states, handoffs)
        self.assertEqual(self.labels_of(1), [LABEL_CI_FAILED])

    def test_empty_required_set_sets_the_ci_failed_label(self):
        sync = self.sync(assignment())
        rollup = [
            {"__typename": "CheckRun", "name": "lint", "status": "COMPLETED", "conclusion": "FAILURE"},
        ]
        pr = {"state": "OPEN", "statusCheckRollup": rollup, "requiredChecks": []}
        states = {"fix-a": {"status": "pr_ready", "pr": "https://example.invalid/pr/1"}}
        with patch("factory.issue_labels.inspect_pr", return_value=pr):
            handoffs, _ = sync.live_handoffs(states)
        self.assertEqual(handoffs, {"fix-a": "ci_failed"})
        sync.reconcile(states, handoffs)
        self.assertEqual(self.labels_of(1), [LABEL_CI_FAILED])

    def test_statuses_choose_which_published_jobs_are_looked_up(self):
        sync = self.sync()
        states = {
            "fix-a": {"status": "pr_ready", "pr": "https://example.invalid/pr/1"},
            "fix-b": {"status": "pr_incomplete", "pr": "https://example.invalid/pr/2"},
            "fix-c": {"status": "queued", "pr": "https://example.invalid/pr/3"},
            "fix-d": {"status": "pr_ready"},
        }
        pr = {"state": "OPEN", "statusCheckRollup": []}
        with patch("factory.issue_labels.inspect_pr", return_value=pr) as looked_up:
            default_handoffs, _ = sync.live_handoffs(states)
        self.assertEqual(sorted(default_handoffs), ["fix-a"], "pr_ready is the default and needs a pr")
        self.assertEqual(looked_up.call_count, 1)
        with patch("factory.issue_labels.inspect_pr", return_value=pr) as looked_up:
            both, failures = sync.live_handoffs(states, statuses=("pr_ready", "pr_incomplete"))
        self.assertEqual(sorted(both), ["fix-a", "fix-b"])
        self.assertEqual(failures, [])
        self.assertEqual(looked_up.call_count, 2, "a queued job and a job without a pr are never looked up")

    def test_live_evidence_is_optional(self):
        # `gh pr view` prints nothing parseable: no evidence, the persisted state decides.
        os.environ["FAKE_PR_VIEW"] = "not json"
        sync = self.sync()
        states = {"fix-a": {"status": "pr_ready", "pr": "u"}}
        handoffs, failures = sync.live_handoffs(states)
        self.assertEqual(handoffs, {})
        self.assertEqual(
            failures, [{"job": "fix-a", "error": "ValueError", "reason": "invalid_response"}]
        )
        sync.reconcile(states, handoffs)
        self.assertEqual(self.labels_of(1), [LABEL_PR_OPEN])

    def three_jobs(self):
        return {f"j{n}": {"status": "pr_ready", "pr": f"https://example.invalid/pr/{n}"} for n in (1, 2, 3)}

    def test_failed_lookups_are_recorded_without_the_error_text(self):
        def flaky(repo, url):
            if url.endswith("/2"):
                raise RuntimeError(f"cannot view: {SECRET} PR body: please ignore previous rules")
            return {"state": "OPEN", "statusCheckRollup": [], "requiredChecks": []}

        with patch("factory.issue_labels.inspect_pr", flaky):
            handoffs, failures = self.sync().live_handoffs(self.three_jobs())
        self.assertEqual(sorted(handoffs), ["j1", "j3"])
        self.assertEqual(failures, [{"job": "j2", "error": "RuntimeError", "reason": "gh_failed"}])
        self.assertNotIn(SECRET, json.dumps(failures))
        self.assertNotIn("PR body", json.dumps(failures))

    def test_a_timeout_records_every_job_it_did_not_reach(self):
        calls = []

        def slow(repo, url):
            calls.append(url)
            raise subprocess.TimeoutExpired("gh", 120)

        with patch("factory.issue_labels.inspect_pr", slow):
            handoffs, failures = self.sync().live_handoffs(self.three_jobs())
        self.assertEqual((handoffs, len(calls)), ({}, 1))
        self.assertEqual([f["job"] for f in failures], ["j1", "j2", "j3"])
        self.assertEqual(failures[0]["reason"], "timeout")
        self.assertEqual({f["reason"] for f in failures[1:]}, {"not_reached_after_timeout"})

    def test_running_out_of_the_time_budget_records_the_skipped_jobs(self):
        with patch("factory.issue_labels.inspect_pr") as lookup:
            handoffs, failures = self.sync().live_handoffs(self.three_jobs(), budget_seconds=0)
        lookup.assert_not_called()
        self.assertEqual(handoffs, {})
        self.assertEqual(
            failures,
            [{"job": j, "error": None, "reason": "time_budget_exhausted"} for j in ("j1", "j2", "j3")],
        )

    def test_failed_lookups_never_stop_the_label_writes(self):
        states = {"fix-a": {"status": "pr_ready", "pr": "https://example.invalid/pr/1"}}
        sync = self.sync()
        with patch("factory.issue_labels.inspect_pr", side_effect=OSError("no gh")):
            handoffs, failures = sync.live_handoffs(states)
        self.assertEqual([f["reason"] for f in failures], ["os_error"])
        sync.reconcile(states, handoffs)
        self.assertEqual(self.labels_of(1), [LABEL_PR_OPEN])


class BestEffortTests(FakeGh):
    def test_a_failing_gh_is_swallowed_with_exactly_one_fixed_line(self):
        os.environ["FAKE_FAIL"] = "1"
        plan_ = assignment()
        plan_["issues"] += [issue(2), issue(3)]
        plan_["jobs"][0]["issues"] = [1, 2, 3]
        plan_["dispositions"] = [
            {"issue": n, "status": "job", "job": "fix-a", "reason": "r"} for n in (1, 2, 3)
        ]
        sync = self.sync(plan_)
        _, out, err = self.run_quietly(sync.sync, {1: LABEL_BUILDING, 2: LABEL_BUILDING, 3: LABEL_BUILDING})
        self.assertEqual(err, "factory: issue label sync failed (CalledProcessError)\n")
        self.assertEqual(out, "")
        self.assertNotIn(SECRET, err + out)

    def test_at_most_one_line_per_process_across_syncs_and_objects(self):
        os.environ["FAKE_FAIL"] = "1"
        _, _, first = self.run_quietly(self.sync().sync, {1: LABEL_BUILDING})
        _, _, second = self.run_quietly(self.sync().sync, {1: LABEL_MERGED})
        self.assertEqual(first, "factory: issue label sync failed (CalledProcessError)\n")
        self.assertEqual(second, "")

    def test_it_stops_trying_after_three_consecutive_failures(self):
        os.environ["FAKE_FAIL"] = "1"
        sync = self.sync()
        self.run_quietly(sync.sync, {n: LABEL_BUILDING for n in range(1, 9)})
        self.assertEqual(len(self.calls("label", "create")), 3)
        self.assertTrue(sync.disabled)

    def test_a_success_resets_the_failure_count(self):
        sync = self.sync()
        sync.failures = 2
        sync.sync({1: LABEL_BUILDING})
        self.assertEqual(sync.failures, 0)

    def test_timeouts_and_garbage_are_swallowed_too(self):
        error = subprocess.TimeoutExpired(["gh"], 30)
        for effect in (error, OSError("no gh"), json.JSONDecodeError("x", "y", 0)):
            with self.subTest(effect=type(effect).__name__):
                issue_labels.reset_report()
                with patch("factory.issue_labels.command", side_effect=effect):
                    _, _, err = self.run_quietly(self.sync().sync, {1: LABEL_BUILDING})
                self.assertEqual(err, f"factory: issue label sync failed ({type(effect).__name__})\n")
        with patch("factory.issue_labels.command", return_value="{}"):
            issue_labels.reset_report()
            _, _, err = self.run_quietly(self.sync().sync, {1: LABEL_BUILDING})
            self.assertEqual(err, "factory: issue label sync failed (ValueError)\n")

    def test_three_failures_rest_the_labeller_and_the_cool_down_writes_what_was_kept(self):
        os.environ["FAKE_FAIL"] = "1"
        clock = Clock()
        sync = self.sync(clock=clock)
        self.run_quietly(sync.sync, {n: LABEL_BUILDING for n in range(1, 6)})
        self.assertTrue(sync.disabled)
        calls = len(self.calls())
        self.assertEqual(calls, 3)
        # While it rests nothing reaches GitHub, and a newer wish replaces a kept one.
        _, out, err = self.run_quietly(sync.sync, {4: LABEL_IN_REVIEW})
        clock.advance(COOLDOWN_SECONDS - 1)
        self.run_quietly(sync.sync, {5: LABEL_MERGED})
        self.assertEqual((out, err), ("", ""))
        self.assertTrue(sync.disabled)
        self.assertEqual(len(self.calls()), calls)
        # The cool-down is over and GitHub answers again: the first update that arrives flushes
        # the kept ones, including the three whose writes failed.
        del os.environ["FAKE_FAIL"]
        clock.advance(1)
        self.assertFalse(sync.disabled)
        sync.sync({})
        self.assertEqual(
            [self.labels_of(n) for n in range(1, 6)],
            [[LABEL_BUILDING], [LABEL_BUILDING], [LABEL_BUILDING], [LABEL_IN_REVIEW], [LABEL_MERGED]],
        )
        self.assertEqual(sync.failures, 0)

    def test_a_failed_probe_after_the_cool_down_rests_again_after_one_call(self):
        os.environ["FAKE_FAIL"] = "1"
        clock = Clock()
        sync = self.sync(clock=clock)
        self.run_quietly(sync.sync, {n: LABEL_BUILDING for n in range(1, 6)})
        calls = len(self.calls())
        clock.advance(COOLDOWN_SECONDS)
        self.assertFalse(sync.disabled)
        self.run_quietly(sync.sync, {})
        # One more call (not three), then it rests for a whole cool-down again.
        self.assertEqual(len(self.calls()), calls + 1)
        self.assertTrue(sync.disabled)
        self.run_quietly(sync.sync, {6: LABEL_BUILDING})
        self.assertEqual(len(self.calls()), calls + 1)
        clock.advance(COOLDOWN_SECONDS)
        del os.environ["FAKE_FAIL"]
        sync.sync({})
        self.assertEqual([self.labels_of(n) for n in range(1, 7)], [[LABEL_BUILDING]] * 6)

    def test_the_ignore_re_check_reads_on_while_the_label_writes_rest(self):
        # An App without issues: write: every read answers, every write is refused.
        os.environ["FAKE_WRITE_FAIL"] = "1"
        self.set_labels(n1=[IGNORE_LABEL])
        clock = Clock()
        sync = self.sync(clock=clock)
        self.run_quietly(sync.sync, {n: LABEL_BUILDING for n in (2, 3, 4)})
        self.assertTrue(sync.disabled)
        resting_until = sync.suspended_until
        before = len(self.calls())
        found, out, err = self.run_quietly(sync.ignored_among, [1, 2])
        self.assertEqual(found, {1})
        self.assertEqual((out, err), ("", ""))
        # Two reads, no write attempt, and the write cool-down is neither ended nor restarted.
        self.assertEqual(len(self.calls()) - before, 2)
        self.assertEqual(len(self.calls("label", "create")), 3)
        self.assertEqual((sync.failures, sync.suspended_until), (3, resting_until))
        self.assertTrue(sync.disabled)
        # The opt-out is lifted again: the next look sees it, still during the rest.
        self.set_labels(n1=[])
        self.assertEqual(self.run_quietly(sync.ignored_among, [1, 2])[0], set())
        # A read that worked is no success for the writes: once the rest is over the first
        # failed write rests again at once.
        clock.advance(COOLDOWN_SECONDS)
        self.assertFalse(sync.disabled)
        calls = len(self.calls("label", "create"))
        self.run_quietly(sync.sync, {})
        self.assertEqual(len(self.calls("label", "create")), calls + 1)
        self.assertTrue(sync.disabled)

    def test_failed_ignore_reads_rest_on_their_own_and_leave_the_writes_alone(self):
        os.environ["FAKE_FAIL"] = "1"
        self.set_labels(n1=[IGNORE_LABEL])
        clock = Clock()
        sync = self.sync(clock=clock)
        sync.ignored.add(9)
        found, _, err = self.run_quietly(sync.ignored_among, [1, 2, 3, 4, 5, 9])
        # Only what this process saw earlier counts; three attempts, then the reads rest.
        self.assertEqual(found, {9})
        self.assertEqual(err, "factory: issue label sync failed (CalledProcessError)\n")
        self.assertEqual(len(self.calls()), 3)
        self.assertEqual(sync.failures, 0)
        self.assertFalse(sync.disabled)
        self.run_quietly(sync.ignored_among, [1, 2])
        self.assertEqual(len(self.calls()), 3)
        # After the rest one probe goes out; the read that works ends the streak.
        del os.environ["FAKE_FAIL"]
        clock.advance(COOLDOWN_SECONDS)
        self.assertEqual(self.run_quietly(sync.ignored_among, [1, 2])[0], {1})
        self.assertEqual((len(self.calls()), sync.read_failures), (5, 0))

    def test_with_unread_the_issues_that_could_not_be_read_are_reported_apart_from_ignored_ones(self):
        os.environ["FAKE_FAIL"] = "1"
        sync = self.sync()
        sync.ignored.add(9)  # an opt-out this process saw earlier still counts as ignored
        (found, unread), _, _ = self.run_quietly(sync.ignored_among, [1, 2, 9], with_unread=True)
        self.assertEqual((found, unread), ({9}, {1, 2}))
        # Without the flag the answer keeps its old shape: a set of the ignored issues.
        self.assertEqual(self.run_quietly(sync.ignored_among, [1, 2, 9])[0], {9})

    def test_with_unread_nothing_is_unread_when_every_look_worked(self):
        self.set_labels(n1=[IGNORE_LABEL])
        sync = self.sync()
        (found, unread), _, err = self.run_quietly(sync.ignored_among, [1, 2], with_unread=True)
        self.assertEqual((found, unread, err), ({1}, set(), ""))

    def test_with_unread_a_lost_lock_leaves_every_issue_unread_without_a_call(self):
        sync = self.sync()
        sync.lost = True
        before = len(self.calls())
        (found, unread), _, _ = self.run_quietly(sync.ignored_among, [3, 4], with_unread=True)
        self.assertEqual((found, unread), (set(), {3, 4}))
        self.assertEqual(len(self.calls()), before)

    def test_a_lost_lock_stays_off_after_the_cool_down(self):
        lost = []

        def fence():
            if lost:
                raise LockLost("lease lost")

        clock = Clock()
        sync = self.sync(fence=fence, clock=clock)
        lost.append(1)
        self.run_quietly(sync.sync, {1: LABEL_BUILDING})
        clock.advance(COOLDOWN_SECONDS * 10)
        lost.clear()
        self.assertTrue(sync.disabled)
        sync.sync({1: LABEL_BUILDING, 2: LABEL_BUILDING})
        self.assertEqual(self.calls(), [])

    def test_the_cool_down_prints_no_second_line_and_live_evidence_waits_for_it(self):
        os.environ["FAKE_FAIL"] = "1"
        clock = Clock()
        sync = self.sync(clock=clock)
        _, _, err = self.run_quietly(sync.sync, {n: LABEL_BUILDING for n in range(1, 4)})
        self.assertEqual(err, "factory: issue label sync failed (CalledProcessError)\n")
        clock.advance(COOLDOWN_SECONDS)
        _, _, err = self.run_quietly(sync.sync, {1: LABEL_BUILDING})
        self.assertEqual(err, "")
        # No pull request lookups while the labeller rests.
        os.environ["FAKE_PR_VIEW"] = json.dumps({"state": "OPEN"})
        self.assertTrue(sync.disabled)
        states = {"fix-a": {"status": "pr_ready", "pr": "https://example.invalid/pr/1"}}
        self.assertEqual(sync.live_handoffs(states), ({}, []))
        self.assertEqual(self.calls("pr", "view"), [])

    def test_a_lost_lock_stops_label_writes_silently(self):
        calls = []

        def fence():
            calls.append(1)
            raise LockLost("lease lost")

        sync = self.sync(fence=fence)
        _, out, err = self.run_quietly(sync.sync, {1: LABEL_BUILDING, 2: LABEL_BUILDING})
        self.assertEqual((out, err), ("", ""))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.calls(), [])

    def test_the_fence_runs_before_any_outward_call(self):
        seen = []
        sync = self.sync(fence=lambda: seen.append(len(self.calls())))
        sync.sync({1: LABEL_BUILDING})
        self.assertEqual(seen, [0])


class HandoffFixture(unittest.TestCase):
    """The hand-off tests' Runner fixtures, without inheriting (and rerunning) their tests."""

    setUp = fixtures.IntegrationTests.setUp
    git = fixtures.IntegrationTests.git
    script = fixtures.IntegrationTests.script
    install_agent = fixtures.IntegrationTests.install_agent
    gh_calls = fixtures.IntegrationTests.gh_calls
    gate_statuses = fixtures.IntegrationTests.gate_statuses
    runner = test_handoff.HandoffTests.runner
    calls = test_handoff.HandoffTests.calls
    reject_review = test_handoff.HandoffTests.reject_review


class RunnerLabelTests(HandoffFixture):
    """Through the real Runner with the shared fake gh (tests/test_factory.py)."""

    def setUp(self):
        super().setUp()
        issue_labels.reset_report()
        os.environ.pop("FAKE_ISSUE_API_FAIL", None)
        self.addCleanup(os.environ.pop, "FAKE_ISSUE_API_FAIL", None)

    def labels(self, number=1):
        store = self.root / "issue-labels.json"
        return json.loads(store.read_text()).get(str(number), []) if store.exists() else []

    def label_adds(self):
        return [
            a.split("=", 1)[1]
            for c in self.calls("api")
            if "-X" in c["args"] and c["args"][c["args"].index("-X") + 1] == "POST"
            for a in c["args"]
            if a.startswith("labels[]=")
        ]

    def test_a_run_labels_the_issue_building_in_review_and_pr_open(self):
        runner = self.runner()
        _, err = self.capture(runner.run)
        self.assertEqual(runner.state.data["jobs"]["fix-a"]["status"], "pr_ready")
        self.assertEqual(self.label_adds(), [LABEL_PLANNED, LABEL_BUILDING, LABEL_IN_REVIEW, LABEL_PR_OPEN])
        self.assertEqual(self.labels(), [LABEL_PR_OPEN])
        self.assertEqual(err, "")

    def capture(self, function):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result = function()
        return result, err.getvalue()

    def test_gh_failures_do_not_stop_the_job_and_print_one_line(self):
        os.environ["FAKE_ISSUE_API_FAIL"] = "1"
        runner = self.runner()
        _, err = self.capture(runner.run)
        state = runner.state.data["jobs"]["fix-a"]
        self.assertEqual(state["status"], "pr_ready", state.get("error"))
        self.assertEqual(self.calls("pr", "create")[0]["args"][:2], ["pr", "create"])
        self.assertEqual(self.gate_statuses()[-1][0], "success")
        self.assertEqual(err, "factory: issue label sync failed (CalledProcessError)\n")
        self.assertNotIn("SECRET-ISSUE-TEXT", err)

    def test_the_run_start_reconcile_heals_a_missed_update(self):
        runner = self.runner()
        runner.state.update("fix-a", status="merged")
        (self.root / "issue-labels.json").write_text(json.dumps({"1": [LABEL_BUILDING, "bug"]}))
        runner.reconcile()
        self.assertEqual(sorted(self.labels()), sorted(["bug", LABEL_MERGED]))

    def test_the_run_start_reconcile_leaves_an_issue_a_replan_took_into_another_run(self):
        closed = "dependency job-0 was closed without merging"
        runner = self.runner()
        runner.state.update("fix-a", status="blocked", error=closed)
        # a re-plan put issue 1 into a sibling run, whose job is building it now
        sibling = self.run_dir.parent / "auto-sibling"
        sibling.mkdir()
        (sibling / "plan.json").write_text(json.dumps(assignment()))
        (self.root / "issue-labels.json").write_text(json.dumps({"1": [LABEL_BUILDING, "bug"]}))
        runner.reconcile()
        self.assertEqual(sorted(self.labels()), sorted(["bug", LABEL_BUILDING]))  # not turned back to blocked
        # with no sibling the same stale state is still written (the issue has no other owner)
        for entry in sibling.iterdir():
            entry.unlink()
        sibling.rmdir()
        runner.reconcile()
        self.assertEqual(sorted(self.labels()), sorted(["bug", LABEL_BLOCKED]))

    def test_the_run_start_reconcile_relabels_an_investigated_issue_blocked(self):
        plan_ = assignment()
        plan_["issues"].append(issue(2))
        plan_["dispositions"].append({"issue": 2, "status": "investigation", "reason": "r"})
        runner = self.runner(plan=plan_)
        (self.root / "issue-labels.json").write_text(json.dumps({"1": [], "2": [LABEL_PLANNED, "bug"]}))
        runner.reconcile()
        self.assertEqual(self.labels(1), [LABEL_PLANNED])  # the queued job
        self.assertEqual(sorted(self.labels(2)), sorted(["bug", LABEL_BLOCKED]))

    def test_a_merged_pull_request_found_by_reconcile_labels_the_issue_merged(self):
        runner = self.runner()
        runner.state.update("fix-a", status="blocked", branch="factory/manual-fix", error="old")
        (self.root / "pr.json").write_text(
            json.dumps([{"url": "https://example.invalid/pr/1", "state": "MERGED", "baseRefName": "main",
                         "headRefOid": "x"}])
        )
        runner.reconcile()
        self.assertEqual(runner.state.data["jobs"]["fix-a"]["status"], "merged")
        self.assertEqual(self.labels(), [LABEL_MERGED])

    def test_an_issue_labelled_ignore_after_planning_is_parked_before_it_is_built(self):
        store = self.root / "issue-labels.json"
        store.write_text(json.dumps({"1": ["Factory-Ignore", LABEL_PLANNED, "bug"]}))
        runner = self.runner()
        _, err = self.capture(runner.run)
        state = runner.state.data["jobs"]["fix-a"]
        self.assertEqual(state["status"], "blocked")
        self.assertEqual(
            state["error"],
            "issue #1 was labelled factory-ignore after planning; not built. "
            "Remove the label and retry the job to build it",
        )
        # No builder, no worktree, no pull request, no branch on the remote.
        self.assertFalse(state.get("usage"))
        self.assertFalse((self.run_dir / "worktrees" / "fix-a").exists())
        self.assertEqual(self.calls("pr", "create"), [])
        # No status label is added to the ignored issue, and the stale one is gone.
        self.assertEqual(self.label_adds(), [])
        self.assertEqual(sorted(self.labels()), ["Factory-Ignore", "bug"])
        self.assertEqual(err, "")

    def test_a_parked_job_is_built_after_the_label_is_removed_and_the_job_retried(self):
        (self.root / "issue-labels.json").write_text(json.dumps({"1": [IGNORE_LABEL]}))
        self.capture(self.runner().run)
        (self.root / "issue-labels.json").write_text(json.dumps({"1": []}))
        runner = self.runner()
        runner.state.update("fix-a", status="queued", error=None)
        self.capture(runner.run)
        self.assertEqual(runner.state.data["jobs"]["fix-a"]["status"], "pr_ready")
        self.assertEqual(self.labels(), [LABEL_PR_OPEN])

    def test_a_job_with_one_ignored_issue_among_several_is_parked_whole(self):
        plan_ = assignment()
        plan_["issues"].append({"number": 2, "title": "Fix B", "body": "B"})
        plan_["jobs"][0]["issues"] = [1, 2]
        plan_["dispositions"].append({"issue": 2, "status": "job", "job": "fix-a", "reason": "r"})
        (self.root / "issue-labels.json").write_text(json.dumps({"2": [IGNORE_LABEL]}))
        runner = self.runner(plan=plan_)
        self.capture(runner.run)
        state = runner.state.data["jobs"]["fix-a"]
        self.assertEqual((state["status"], self.calls("pr", "create")), ("blocked", []))
        self.assertIn("issue #2 ", state["error"])
        self.assertNotIn("#1", state["error"])
        self.assertEqual(self.labels(1), [LABEL_BLOCKED])
        self.assertEqual(self.labels(2), [IGNORE_LABEL])

    def test_a_late_ignore_label_still_parks_the_job_while_the_label_writes_rest(self):
        # The App lacks issues: write: reads answer, every label write is refused, so the run
        # start (reconcile) makes three failed writes and the labeller rests before any build.
        os.environ["FAKE_ISSUE_WRITE_FAIL"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_ISSUE_WRITE_FAIL", None)
        plan_ = assignment()
        for number in (2, 3):
            plan_["issues"].append({"number": number, "title": f"Fix {number}", "body": "B"})
            plan_["dispositions"].append({"issue": number, "status": "job", "job": "fix-a", "reason": "r"})
        plan_["jobs"][0]["issues"] = [1, 2, 3]
        (self.root / "issue-labels.json").write_text(json.dumps({"3": [IGNORE_LABEL]}))
        runner = self.runner(plan=plan_)
        _, err = self.capture(runner.run)
        self.assertTrue(runner.issue_labels.disabled)
        state = runner.state.data["jobs"]["fix-a"]
        self.assertEqual((state["status"], self.calls("pr", "create")), ("blocked", []))
        self.assertIn("issue #3 ", state["error"])
        self.assertEqual(self.labels(3), [IGNORE_LABEL])
        self.assertEqual(err, "factory: issue label sync failed (CalledProcessError)\n")

    def test_a_late_ignore_label_still_turns_closes_into_refs_while_the_label_writes_rest(self):
        os.environ["FAKE_ISSUE_WRITE_FAIL"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_ISSUE_WRITE_FAIL", None)
        runner = self.runner()
        runner.issue_labels.failures = MAX_CONSECUTIVE_FAILURES
        runner.issue_labels.suspended_until = runner.issue_labels.clock() + COOLDOWN_SECONDS
        publish = runner.publish

        def late_label(*args):
            (self.root / "issue-labels.json").write_text(json.dumps({"1": [IGNORE_LABEL]}))
            return publish(*args)

        runner.publish = late_label
        self.capture(runner.run)
        self.assertTrue(runner.issue_labels.disabled)
        self.assertEqual(runner.state.data["jobs"]["fix-a"]["status"], "pr_ready")
        body = self.calls("pr", "create")[0]["body"]
        self.assertIn("Refs #1", body)
        self.assertNotIn("Closes #1", body)

    def test_a_label_added_while_the_job_builds_turns_closes_into_refs(self):
        runner = self.runner()
        publish = runner.publish

        def late_label(*args):
            (self.root / "issue-labels.json").write_text(json.dumps({"1": [IGNORE_LABEL]}))
            return publish(*args)

        runner.publish = late_label
        _, err = self.capture(runner.run)
        self.assertEqual(runner.state.data["jobs"]["fix-a"]["status"], "pr_ready")
        body = self.calls("pr", "create")[0]["body"]
        self.assertIn("Refs #1", body)
        self.assertNotIn("Closes #1", body)
        self.assertNotIn("closes the issues above", body)
        self.assertEqual(err, "")

    def test_without_a_late_label_the_pull_request_still_closes_the_issue(self):
        self.runner().run()
        body = self.calls("pr", "create")[0]["body"]
        self.assertIn("Closes #1", body)
        self.assertNotIn(IGNORE_LABEL, body)

    def test_an_unreadable_ignore_label_does_not_stop_the_job_but_it_does_not_close_the_issue(self):
        os.environ["FAKE_ISSUE_API_FAIL"] = "1"
        runner = self.runner()
        _, err = self.capture(runner.run)
        # Building is not stopped on a guess ...
        self.assertEqual(runner.state.data["jobs"]["fix-a"]["status"], "pr_ready")
        # ... but closing the issue is, because nobody can undo it: the label could not be read.
        body = self.calls("pr", "create")[0]["body"]
        # The body starts with the issue-reference line (#77), here only a Refs.
        self.assertTrue(body.startswith("Refs #1\n\n> **Merging this pull request does not close the issue it references (#1).**"), body[:120])
        self.assertIn("\n\n## Assignment", body)
        self.assertNotIn("Closes #1", body)
        self.assertIn(f"Could not check `{IGNORE_LABEL}` before publishing, so only referenced: #1", body)
        self.assertNotIn("Labelled", body)
        self.assertEqual(err, "factory: issue label sync failed (CalledProcessError)\n")

    def test_a_job_left_interrupted_by_a_failed_recover_is_blocked_at_the_next_run(self):
        from types import SimpleNamespace

        cfg = RepoConfig(repo="org/name")
        (self.run_dir / "plan.json").write_text(json.dumps(assignment()))
        runner = self.runner()
        runner.state.update("fix-a", status="running", phase="build", worker_phase="build")
        self.assertEqual(self.labels(), [LABEL_BUILDING])
        # `recover` in a deployment without a clone: gh runs from the run directory, and the
        # issue API refuses. The job is interrupted all the same; only a line is printed.
        os.environ["FAKE_ISSUE_API_FAIL"] = "1"
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()) as err:
            cli.recover_command(
                SimpleNamespace(command="recover"), self.run_dir, None, cfg, self.root / "no-clone", None
            )
        self.assertEqual(out.getvalue(), "Recovered 1 interrupted job(s)\n")
        self.assertEqual(err.getvalue(), "factory: issue label sync failed (CalledProcessError)\n")
        self.assertEqual(State(self.run_dir, assignment()).data["jobs"]["fix-a"]["status"], "interrupted")
        self.assertEqual(self.labels(), [LABEL_BUILDING])
        # The next run (GitHub answers again) re-derives every label from the persisted state.
        del os.environ["FAKE_ISSUE_API_FAIL"]
        issue_labels.reset_report()
        later = self.runner()
        later.reconcile()
        self.assertEqual(later.state.data["jobs"]["fix-a"]["status"], "interrupted")
        self.assertEqual(self.labels(), [LABEL_BLOCKED])

    def test_a_lost_lock_writes_no_issue_label(self):
        runner = self.runner(lock=RecordingLock(fail_after=0))
        _, err = self.capture(runner.run)
        self.assertEqual(self.gh_calls(), [])
        self.assertEqual(err, "")

    def test_output_and_pr_text_carry_no_issue_text(self):
        plan_ = assignment()
        plan_["issues"][0]["body"] = SECRET
        plan_["issues"][0]["title"] = SECRET
        runner = self.runner(plan=plan_)
        _, err = self.capture(runner.run)
        label_calls = [c for c in self.gh_calls() if c["args"][:1] == ["api"] or c["args"][:2] == ["label", "create"]]
        self.assertTrue(label_calls)
        self.assertNotIn(SECRET, json.dumps(label_calls))
        self.assertNotIn(SECRET, err)


class PullRequestLabelTests(HandoffFixture):
    """Only factory:ready is put on a pull request."""

    def create_labels(self):
        args = self.calls("pr", "create")[0]["args"]
        return [args[i + 1] for i, a in enumerate(args) if a == "--label"]

    def test_a_ready_pr_gets_only_factory_ready(self):
        self.runner().run()
        self.assertEqual(self.create_labels(), ["factory:ready"])
        creates = [c["args"][2] for c in self.calls("label", "create")]
        self.assertNotIn("factory", creates)
        self.assertFalse([n for n in creates if n.startswith("factory:run/") or n == "factory:incomplete"])

    def test_an_incomplete_pr_is_an_unlabelled_draft(self):
        self.reject_review()
        Runner(self.repo, self.run_dir, assignment(), timeout=10, review_rounds=0, cfg=RepoConfig(repo="org/name")).run()
        self.assertIn("--draft", self.calls("pr", "create")[0]["args"])
        self.assertEqual(self.create_labels(), [])

    def test_no_code_still_adds_the_dropped_pull_request_labels(self):
        source = "".join(p.read_text() for p in (Path(issue_labels.__file__).parent).glob("*.py"))
        for gone in ("factory:run/", "run_label", "LABEL_INCOMPLETE", "LABEL_FACTORY", "factory:incomplete"):
            self.assertNotIn(gone, source)


class IgnoreFilterTests(FakeGh):
    def write_issues(self, issues):
        (self.root / "issues.json").write_text(json.dumps(issues))

    def snap(self, limit):
        dest = self.work / "snap"
        issues, _ = snapshot(self.work, dest, limit, "org/name")
        return issues, dest

    def test_the_plain_listing_is_used_not_the_lagging_search_index(self):
        self.write_issues([issue(1), issue(2, "factory-ignore")])
        self.snap(30)
        (call,) = self.calls("issue", "list")
        self.assertFalse([a for a in call if a.startswith("--search")])
        self.assertEqual(call[call.index("--state") + 1], "open")
        self.assertEqual(call[call.index("--limit") + 1], "30")
        self.assertEqual(call[call.index("-R") + 1], "org/name")

    def test_ignored_issues_are_skipped_in_any_case_and_exact_names_only(self):
        self.write_issues(
            [issue(1, "factory-ignore"), issue(2, "Factory-Ignore"), issue(3, "FACTORY-IGNORE", "bug"),
             issue(4, "factory-ignored"), issue(5, "bug"), issue(6), issue(7, " factory-ignore ")]
        )
        issues, dest = self.snap(30)
        self.assertEqual([i["number"] for i in issues], [4, 5, 6])
        saved = json.loads((dest / "issues.json").read_text())
        self.assertEqual([i["number"] for i in saved], [4, 5, 6])

    def test_the_limit_counts_only_issues_that_remain(self):
        self.write_issues([issue(1, "factory-ignore"), issue(2, "factory-ignore"), issue(3), issue(4), issue(5)])
        issues, _ = self.snap(2)
        self.assertEqual([i["number"] for i in issues], [3, 4])
        limits = [int(c[c.index("--limit") + 1]) for c in self.calls("issue", "list")]
        self.assertEqual(limits, [2, 4])  # asked again for more, never fewer

    def test_many_ignored_issues_do_not_starve_the_plan(self):
        self.write_issues([issue(n, "factory-ignore") for n in range(1, 41)] + [issue(41), issue(42)])
        issues, _ = self.snap(2)
        self.assertEqual([i["number"] for i in issues], [41, 42])

    def planned(self, issues, use_snapshot=False, limit=30):
        self.write_issues(issues)
        dest = self.work / "run"
        chosen = [i for i in issues if not is_ignored(i)]
        result = {
            "version": 1,
            "jobs": [
                {"id": "fix-a", "issues": [chosen[0]["number"]], "context_issues": [], "objective": "Fix",
                 "likely_paths": ["src"], "acceptance": ["ok"], "depends_on": [], "feature": False}
            ],
            "dispositions": [
                {"issue": i["number"], "status": "job" if n == 0 else "covered", "reason": "r",
                 **({"job": "fix-a"} if n == 0 else {})}
                for n, i in enumerate(chosen)
            ],
        }
        seen = {}

        def fake_agent(prompt, *args, **kwargs):
            seen["prompt"] = prompt
            return dict(result), {"phase": "plan"}

        with patch("factory.planner.run_agent", fake_agent), patch("factory.planner.repo_guidance", return_value=""):
            if use_snapshot:
                dest.mkdir(parents=True)
                (dest / "issues.json").write_text(json.dumps(issues))
                (dest / "open-prs.json").write_text("[]")
            return plan(self.work, dest, limit, use_snapshot=use_snapshot, slug="org/name"), dest, seen["prompt"]

    def test_an_ignored_issue_never_reaches_plan_dispositions_or_the_planner_prompt(self):
        for use_snapshot in (False, True):
            with self.subTest(use_snapshot=use_snapshot):
                shutil_target = self.work / "run"
                if shutil_target.exists():
                    import shutil

                    shutil.rmtree(shutil_target)
                issues = [issue(1), issue(2, "Factory-Ignore", body=SECRET), issue(3)]
                result, dest, prompt = self.planned(issues, use_snapshot)
                self.assertEqual([i["number"] for i in result["issues"]], [1, 3])
                self.assertEqual([d["issue"] for d in result["dispositions"]], [1, 3])
                for name in ("plan.json", "issues.json"):
                    text = (dest / name).read_text()
                    self.assertNotIn(SECRET, text, name)
                    self.assertNotIn("Issue 2", text, name)
                self.assertNotIn(SECRET, prompt)
                self.assertNotIn("Issue 2", prompt)

    def test_ignored_issues_never_get_a_status_label_from_plan(self):
        issues = [issue(1), issue(2, "factory-ignore"), issue(3)]
        result, _, _ = self.planned(issues)
        self.set_labels(n2=["factory-ignore"])
        self.sync(result).sync_plan()
        self.assertEqual(self.labels_of(1), [LABEL_PLANNED])
        self.assertEqual(self.labels_of(2), ["factory-ignore"])
        self.assertNotIn("2", [w[1] for w in self.writes()])

    def test_planning_prints_no_issue_text(self):
        issues = [issue(1, body=SECRET), issue(2, "factory-ignore", body=SECRET + "2")]
        (_, out, err) = self.run_quietly(self.planned, issues)
        self.assertNotIn(SECRET, out + err)


class SiblingIssuesTests(unittest.TestCase):
    """``sibling_issues`` names the issues of the other runs next to a run directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runs = Path(self.tmp.name)

    def write_plan(self, name, issues=(), dispositions=()):
        run = self.runs / name
        run.mkdir()
        body = {
            "jobs": [{"issues": list(issues)}],
            "dispositions": [{"issue": number} for number in dispositions],
        }
        (run / "plan.json").write_text(json.dumps(body))
        return run

    def test_it_lists_the_jobs_and_dispositions_of_the_other_runs_only(self):
        mine = self.write_plan("mine", issues=[1, 2])
        self.write_plan("other", issues=[3, 4], dispositions=[5])
        self.assertEqual(sibling_issues(mine), {3, 4, 5})

    def test_a_json_boolean_is_not_an_issue_number(self):
        mine = self.write_plan("mine", issues=[1])
        self.write_plan("other", issues=[True, 7], dispositions=[False])
        self.assertEqual(sibling_issues(mine), {7})

    def test_an_unreadable_or_malformed_plan_names_nothing(self):
        mine = self.write_plan("mine", issues=[1])
        (self.runs / "broken").mkdir()
        (self.runs / "broken" / "plan.json").write_text("{not json")
        (self.runs / "noplan").mkdir()
        (self.runs / "wrongshape").mkdir()
        (self.runs / "wrongshape" / "plan.json").write_text(json.dumps({"jobs": 3}))
        self.write_plan("good", issues=[9])
        self.assertEqual(sibling_issues(mine), {9})

    def test_a_missing_runs_directory_names_nothing(self):
        self.assertEqual(sibling_issues(self.runs / "gone" / "run"), set())


class OperationsTextTests(unittest.TestCase):
    def test_docs_describe_the_labels_and_the_ignore_opt_out(self):
        root = Path(__file__).resolve().parents[1]
        text = {name: (root / name).read_text() for name in ("docs/configuration.md", "docs/operations.md", "docs/handoff.md")}
        for name, body in text.items():
            self.assertIn("factory-ignore", body, name)
        for label in STATUS_LABELS:
            self.assertIn(label, text["docs/handoff.md"])
        self.assertIn("issues (**write**", text["docs/handoff.md"])
        self.assertNotIn("factory:run/", "".join(text.values()))
        self.assertNotIn("factory:incomplete", "".join(text.values()))
        self.assertNotIn("three labels", text["docs/handoff.md"])

    def test_docs_say_what_a_pause_or_an_undrained_shutdown_leaves(self):
        # A pause or an undrained shutdown ends a job with a live worker `failed` (`agent exited -15`), and a
        # failed job comments; only a supervisor killed before it records the end leaves `interrupted`, which
        # does not. The docs must not say that every restart marks the running jobs `interrupted`.
        self.assertIn("failed", issue_comments.JOB_STATUS_KINDS)
        self.assertNotIn("interrupted", issue_comments.JOB_STATUS_KINDS)
        root = Path(__file__).resolve().parents[1]
        names = ("docs/configuration.md", "docs/operations.md", "infra/README.md")
        text = {name: " ".join((root / name).read_text().split()) for name in names}
        stale = (
            "every restart or deploy marks the running jobs",
            "a restart marks every running job so",
            "routine after every restart",
            "as after any restart",
        )
        for name, body in text.items():
            for phrase in stale:
                self.assertNotIn(phrase, body, name)
        for name in ("docs/operations.md", "infra/README.md"):
            self.assertIn("agent exited -15", text[name], name)
        operations = (root / "docs/operations.md").read_text()
        start = operations.index("A job that is `interrupted` does **not** comment")
        paragraph = " ".join(operations[start : operations.index("\n\n", start)].split())
        self.assertIn("`pause`", paragraph)
        self.assertIn("agent exited -15", paragraph)


if __name__ == "__main__":
    unittest.main()

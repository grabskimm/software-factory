"""Idle reconcile and the completion label, through ``cli.reconcile_command`` against a fake ``gh``.

The fake keeps one repository ("org/name") in $FAKE_DIR/world.json: issues and their labels, pull
requests (state, head, body), who applied the completion label, collaborator permissions. Every
mutating call is appended to ``writes``. ``fail`` makes every call fail with a 403.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from factory import cli, issue_labels
from factory.completion import COMPLETION_LABEL, rewritten_body
from factory.config import RepoConfig
from factory.locks import LockLost

SECRET = "SECRET-do-not-print"
HEAD = "a" * 40
MOVED = "b" * 40

FAKE_GH = r"""
import json, os, pathlib, re, sys, urllib.parse
root = pathlib.Path(os.environ['FAKE_DIR'])
args = sys.argv[1:]
store = root / 'world.json'
world = json.loads(store.read_text())
world.setdefault('calls', []).append(args)
def save():
    store.write_text(json.dumps(world))
def fail(message):
    save()
    sys.stderr.write(message + ' ' + os.environ.get('FAKE_SECRET', '') + '\n')
    sys.exit(1)
if world.get('fail') or (world.get('fail_writes') and ('-X' in args)):
    fail('HTTP 403')
if args[:2] == ['label', 'create']:
    save()
elif args[:2] == ['pr', 'view']:
    number = args[2].rsplit('/', 1)[1]
    pull = world['pulls'][number]
    state = 'MERGED' if pull['merged'] else pull['state'].upper()
    save()
    print(json.dumps({'headRefOid': pull['head'], 'baseRefName': 'main', 'isDraft': True, 'state': state,
                      'statusCheckRollup': [], 'mergeCommit': None, 'mergeable': 'UNKNOWN', 'mergeStateStatus': 'UNKNOWN'}))
elif args[:2] == ['pr', 'checks']:
    number = args[2].rsplit('/', 1)[1]
    checks = world.get('checks', {}).get(number, [])
    save()
    print(json.dumps(checks))
elif args[:2] == ['api', 'user']:
    save()
    print(world.get('login', 'factory-bot'))
elif args[:1] == ['api']:
    path = next(a for a in args[1:] if a.startswith('repos/'))
    route, _, query = path.partition('?')
    params = dict(urllib.parse.parse_qsl(query))
    verb = args[args.index('-X') + 1] if '-X' in args else 'GET'
    fields = [a for a in args if '=' in a and not a.startswith(('repos/', '--'))]
    prefix = 'repos/org/name'
    rest = route[len(prefix):]
    out = None
    if rest == '':
        out = {'archived': False, 'private': world.get('private', True), 'visibility': 'private'}
    elif m := re.fullmatch(r'/issues/(\d+)', rest):
        out = world['issues'].get(m.group(1), [])
    elif m := re.fullmatch(r'/issues/(\d+)/labels', rest):
        world['writes'].append(['label-add', m.group(1), [f.split('=', 1)[1] for f in fields]])
        world['issues'].setdefault(m.group(1), []).extend(f.split('=', 1)[1] for f in fields)
    elif m := re.fullmatch(r'/issues/(\d+)/labels/(.+)', rest):
        name = urllib.parse.unquote(m.group(2))
        world['writes'].append(['label-remove', m.group(1), name])
        world['issues'][m.group(1)].remove(name)
    elif rest == '/issues':
        wanted = params['labels']
        rows = [{'number': int(n), 'pr': True, 'labels': [wanted], 'updated': '1'}
                for n, labelled in world['labelled'].items() if wanted in labelled and world['pulls'][n]['state'] == 'open']
        save()
        for row in rows:
            print(json.dumps(row))
        sys.exit(0)
    elif m := re.fullmatch(r'/issues/(\d+)/events', rest):
        save()
        for event in world['events'].get(m.group(1), []):
            print(json.dumps(event))
        sys.exit(0)
    elif m := re.fullmatch(r'/collaborators/([^/]+)/permission', rest):
        user = urllib.parse.unquote(m.group(1))
        if user not in world['permissions']:
            fail('gh: Not Found (HTTP 404)')
        out = world['permissions'][user]
    elif m := re.fullmatch(r'/pulls/(\d+)', rest):
        pull = world['pulls'][m.group(1)]
        if verb == 'PATCH':
            body = next(f.split('=', 1)[1] for f in fields if f.startswith('body='))
            world['writes'].append(['body', m.group(1), body])
            pull['body'] = body
        else:
            out = {'state': pull['state'], 'merged': pull['merged'], 'head': pull['head'], 'body': pull['body']}
    elif m := re.fullmatch(r'/commits/(.+)/status', rest):
        seen = [w for w in world['writes'] if w[0] == 'status' and w[1] == m.group(1) and w[3] == 'factory/gate']
        out = {'state': seen[-1][2], 'description': seen[-1][4], 'created': world.get('created', {}).get(m.group(1), '2026-01-01T00:00:00Z')} if seen else {}
    elif m := re.fullmatch(r'/commits/(.+)/statuses', rest):
        seen = [w for w in world['writes'] if w[0] == 'status' and w[1] == m.group(1) and w[3] == 'factory/gate']
        if seen:
            who = seen[-1][5] if len(seen[-1]) > 5 else {'type': 'Bot', 'id': 4242, 'login': 'factory-bot'}
            out = {'state': seen[-1][2], 'description': seen[-1][4],
                   'created': world.get('created', {}).get(m.group(1), '2026-01-01T00:00:00Z'),
                   'ctype': who['type'], 'cid': who['id'], 'clogin': who['login']}
        else:
            out = {}
    elif m := re.fullmatch(r'/statuses/(.+)', rest):
        data = dict(f.split('=', 1) for f in fields)
        world['writes'].append(['status', m.group(1), data['state'], data['context'], data['description']])
    else:
        fail('unexpected ' + path)
    save()
    if out is not None:
        if '--jq' in args and args[args.index('--jq') + 1] == '{state: .state, labels: [.labels[].name]}':
            print(json.dumps({'state': 'open', 'labels': out}))
        elif '--jq' in args and args[args.index('--jq') + 1] == '.role_name // .permission // "none"':
            print(out)
        else:
            print(json.dumps(out))
else:
    fail('unexpected ' + ' '.join(args))
"""


def body_of(first, tail="\n\n## Assignment\n\nFix it\n\n## Verification\n\n```json\n{}\n```\n"):
    return first + tail


class World:
    """The fake repository: edit ``data``, ``save`` it, read it back after the run."""

    def __init__(self, root):
        self.path = Path(root) / "world.json"
        self.data = {
            "issues": {},
            "pulls": {},
            "labelled": {},
            "events": {},
            "permissions": {"alice": "write", "bob": "triage", "carol": "admin"},
            "writes": [],
        }

    def pull(self, number, state="open", merged=False, head=HEAD, body=""):
        self.data["pulls"][str(number)] = {"state": state, "merged": merged, "head": head, "body": body}

    def label_pull(self, number, login="alice", kind="User"):
        self.data["labelled"][str(number)] = [COMPLETION_LABEL]
        self.data["events"][str(number)] = [
            {"id": 1, "event": "labeled", "label": COMPLETION_LABEL, "login": login, "type": kind}
        ]

    def save(self):
        self.path.write_text(json.dumps(self.data))

    def load(self):
        return json.loads(self.path.read_text())

    def writes(self, *kinds):
        return [w for w in self.load()["writes"] if not kinds or w[0] in kinds]


def plan_for(*jobs):
    """``jobs``: (job id, [issue numbers])."""
    numbers = [n for _, issues in jobs for n in issues]
    return {
        "version": 1,
        "artifact_roots": ["plans/active"],
        "issues": [{"number": n, "title": f"Issue {n}", "body": "b"} for n in numbers],
        "jobs": [
            {
                "id": job,
                "issues": issues,
                "context_issues": [],
                "objective": "Fix",
                "likely_paths": ["src"],
                "acceptance": ["works"],
                "depends_on": [],
                "feature": False,
            }
            for job, issues in jobs
        ],
        "dispositions": [{"issue": n, "status": "job", "job": job, "reason": "r"} for job, issues in jobs for n in issues],
    }


class ReconcileCase(unittest.TestCase):
    def setUp(self):
        issue_labels.reset_report()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text(f"#!{sys.executable}\n" + FAKE_GH)
        gh.chmod(0o755)
        env = patch.dict(
            os.environ,
            {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_DIR": str(self.root), "FAKE_SECRET": SECRET},
        )
        env.start()
        self.addCleanup(env.stop)
        self.runs = self.root / "runs"
        self.runs.mkdir()
        self.world = World(self.root)
        self.cfg = RepoConfig(repo="org/name")

    def run_dir(self, name, plan, jobs):
        """``jobs``: id -> (status, pull number, commit)."""
        directory = self.runs / name
        directory.mkdir()
        (directory / "plan.json").write_text(json.dumps(plan))
        states = {}
        for key, (status, number, commit) in jobs.items():
            states[key] = {"status": status, "attempt": 1}
            if number is not None:
                states[key]["pr"] = f"https://github.com/org/name/pull/{number}"
            if commit is not None:
                states[key]["commit"] = commit
        (directory / "state.json").write_text(json.dumps({"version": 1, "jobs": states}))
        return directory

    def reconcile(self, directory, lock=None):
        self.world.save()
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            cli.reconcile_command(self.cfg, self.root / "no-clone", directory, lock)
        text = err.getvalue()
        self.assertNotIn(SECRET, text + out.getvalue())
        return text.splitlines()

    def issue_labels(self, number):
        return self.world.load()["issues"].get(str(number), [])


class IdleLabelTests(ReconcileCase):
    def test_a_merged_pull_request_sets_factory_merged_without_purging_the_run(self):
        self.world.pull(7, state="closed", merged=True)
        self.world.data["issues"] = {"1": ["factory:pr-open", "bug"]}
        directory = self.run_dir("r1", plan_for(("fix", [1])), {"fix": ("pr_ready", 7, HEAD)})
        self.assertEqual(self.reconcile(directory), [])
        self.assertEqual(self.issue_labels(1), ["bug", "factory:merged"])
        self.assertTrue(directory.is_dir())
        self.assertEqual(json.loads((directory / "state.json").read_text())["jobs"]["fix"]["status"], "pr_ready")

    def test_an_incomplete_pull_request_that_was_merged_gets_the_label_too(self):
        self.world.pull(7, state="closed", merged=True)
        self.world.data["issues"] = {"1": ["factory:pr-open"]}
        directory = self.run_dir("r1", plan_for(("fix", [1])), {"fix": ("pr_incomplete", 7, HEAD)})
        self.reconcile(directory)
        self.assertEqual(self.issue_labels(1), ["factory:merged"])

    def test_an_open_pull_request_changes_nothing(self):
        self.world.pull(7)
        self.world.data["issues"] = {"1": ["factory:pr-open"]}
        directory = self.run_dir("r1", plan_for(("fix", [1])), {"fix": ("pr_ready", 7, HEAD)})
        self.reconcile(directory)
        self.assertEqual(self.world.writes(), [])

    def test_a_pull_request_closed_unmerged_clears_the_status_label_only(self):
        self.world.pull(7, state="closed")
        self.world.data["issues"] = {"1": ["factory:pr-open", "bug"]}
        directory = self.run_dir("r1", plan_for(("fix", [1])), {"fix": ("pr_incomplete", 7, HEAD)})
        self.reconcile(directory)
        self.assertEqual(self.issue_labels(1), ["bug"])

    def test_a_sibling_runs_issue_and_a_factory_ignore_issue_are_not_touched(self):
        for number in (7, 8, 9):
            self.world.pull(number, state="closed")
        self.world.data["issues"] = {
            "1": ["factory:pr-open"],
            "2": ["factory:pr-open"],
            "3": ["factory:pr-open", "factory-ignore"],
        }
        self.run_dir("sibling", plan_for(("other", [2])), {"other": ("running", None, None)})
        directory = self.run_dir(
            "r1",
            plan_for(("a", [1]), ("b", [2]), ("c", [3])),
            {"a": ("pr_ready", 7, HEAD), "b": ("pr_ready", 8, HEAD), "c": ("pr_ready", 9, HEAD)},
        )
        self.reconcile(directory)
        self.assertEqual(self.issue_labels(1), [])
        self.assertEqual(self.issue_labels(2), ["factory:pr-open"])
        self.assertEqual(self.issue_labels(3), ["factory:pr-open", "factory-ignore"])
        self.assertEqual({w[1] for w in self.world.writes()}, {"1"})

    def test_a_merged_pull_request_does_not_label_a_sibling_runs_or_an_ignored_issue(self):
        for number in (7, 8):
            self.world.pull(number, state="closed", merged=True)
        self.world.data["issues"] = {"1": ["factory-ignore", "factory:pr-open"], "2": ["factory:pr-open"]}
        self.run_dir("sibling", plan_for(("other", [2])), {"other": ("running", None, None)})
        directory = self.run_dir(
            "r1", plan_for(("a", [1]), ("b", [2])), {"a": ("pr_ready", 7, HEAD), "b": ("pr_ready", 8, HEAD)}
        )
        self.reconcile(directory)
        self.assertEqual(self.world.writes("label-add", "label-remove"), [])

    def test_an_issue_another_job_of_the_run_still_holds_keeps_its_label(self):
        self.world.pull(7, state="closed")
        self.world.pull(8)
        self.world.data["issues"] = {"1": ["factory:pr-open"]}
        directory = self.run_dir(
            "r1", plan_for(("a", [1]), ("b", [2])), {"a": ("pr_ready", 7, HEAD), "b": ("pr_ready", 8, HEAD)}
        )
        # job b names issue 1 as well, through the plan of a second job on the same issue
        plan = plan_for(("a", [1]), ("b", [2]))
        plan["jobs"][1]["issues"] = [1, 2]
        (directory / "plan.json").write_text(json.dumps(plan))
        self.reconcile(directory)
        self.assertEqual(self.issue_labels(1), ["factory:pr-open"])

    def test_failing_writes_are_swallowed_with_one_fixed_line_and_a_cool_down(self):
        jobs = ("a", "b", "c", "d", "e")
        for number in range(1, 6):
            self.world.pull(number, state="closed", merged=True)
        self.world.data["issues"] = {str(n): ["factory:pr-open"] for n in range(1, 6)}
        self.world.data["fail_writes"] = True
        plan = plan_for(*((job, [n]) for n, job in enumerate(jobs, 1)))
        states = {job: ("pr_ready", n, HEAD) for n, job in enumerate(jobs, 1)}
        directory = self.run_dir("r1", plan, states)
        lines = self.reconcile(directory)  # does not raise
        self.assertEqual(lines, ["factory: issue label sync failed (CalledProcessError)"])
        self.assertEqual(self.world.writes(), [])
        posts = [c for c in self.world.load()["calls"] if c[:3] == ["api", "-X", "POST"]]
        self.assertEqual(len(posts), issue_labels.MAX_CONSECUTIVE_FAILURES)  # then the cool-down: no more tries
        self.assertTrue(directory.is_dir())

    def test_failing_reads_never_raise(self):
        self.world.pull(7, state="closed", merged=True)
        self.world.data["fail"] = True
        directory = self.run_dir("r1", plan_for(("fix", [1])), {"fix": ("pr_ready", 7, HEAD)})
        self.assertEqual(self.reconcile(directory), [])
        self.assertTrue(directory.is_dir())

    def test_a_lost_lock_stops_the_writes_quietly(self):
        self.world.pull(7, state="closed", merged=True)
        self.world.data["issues"] = {"1": ["factory:pr-open"]}
        directory = self.run_dir("r1", plan_for(("fix", [1])), {"fix": ("pr_ready", 7, HEAD)})

        class Lost:
            @staticmethod
            def assert_held():
                raise LockLost("gone")

        self.assertEqual(self.reconcile(directory, Lost), [])
        self.assertEqual(self.world.writes(), [])

    def test_a_run_without_a_usable_plan_prints_nothing_to_reconcile(self):
        directory = self.runs / "r1"
        directory.mkdir()
        self.assertEqual(self.reconcile(directory), [])
        self.assertEqual(self.world.writes(), [])


class CompletionTests(ReconcileCase):
    FIRST = "Refs #1"

    def setUp(self):
        super().setUp()
        self.body = body_of(self.FIRST)
        self.world.pull(7, head=HEAD, body=self.body)
        self.world.label_pull(7)
        self.directory = self.run_dir("r1", plan_for(("fix", [1])), {"fix": ("pr_incomplete", 7, HEAD)})
        self.world.data["issues"] = {"1": ["factory:pr-open"]}

    def test_an_authorised_user_turns_refs_into_closes_and_the_gate_is_posted_on_the_head(self):
        self.assertEqual(self.reconcile(self.directory), [])
        [body] = self.world.writes("body")
        self.assertEqual(body[2], body_of("Closes #1"))
        self.assertEqual(body[2].split("\n", 1)[1], self.body.split("\n", 1)[1])
        [status] = self.world.writes("status")
        self.assertEqual(status[1:4], [HEAD, "success", "factory/gate"])
        self.assertIn("@alice", status[4])
        self.assertIn("by hand", status[4])
        self.assertLessEqual(len(status[4]), 140)
        # nothing else: no push, no draft or label change on the pull request
        calls = [c for c in self.world.load()["calls"] if c[:1] != ["api"] and c[:2] not in (["pr", "view"], ["pr", "checks"], ["label", "create"])]
        self.assertEqual(calls, [])

    def test_the_other_job_issues_stay_refs_when_ignored_and_a_second_pass_is_harmless(self):
        plan = plan_for(("fix", [1, 2]))
        (self.directory / "plan.json").write_text(json.dumps(plan))
        self.world.pull(7, head=HEAD, body=body_of("Refs #1, refs #2"))
        self.world.data["issues"] = {"1": ["factory:pr-open"], "2": ["factory-ignore"]}
        self.reconcile(self.directory)
        [body] = self.world.writes("body")
        self.assertEqual(body[2].split("\n", 1)[0], "Closes #1, refs #2")
        self.world.data = self.world.load()
        self.reconcile(self.directory)  # already rewritten: no second body write and no second status
        self.assertEqual(len(self.world.writes("body")), 1)
        self.assertEqual(len(self.world.writes("status")), 1)

    def test_an_unreadable_gate_status_posts_nothing(self):
        path = self.root / "bin" / "gh"
        path.write_text(path.read_text().replace("elif m := re.fullmatch(r'/commits/(.+)/status', rest):", "elif m := re.fullmatch(r'/commits/(.+)/status', rest):\n        fail('HTTP 403')"))
        self.reconcile(self.directory)
        self.assertEqual(self.world.writes("status"), [])

    def test_an_unreadable_issue_label_keeps_refs(self):
        # The late factory-ignore read of issue 1 cannot be parsed: closing cannot be undone, so Refs stays.
        self.world.data["issues"] = {"1": "not a list"}
        self.reconcile(self.directory)
        self.assertEqual(self.world.writes("body"), [])
        self.assertEqual(len(self.world.writes("status")), 1)

    def test_a_user_below_the_minimum_permission_is_ignored(self):
        for login in ("bob", "mallory"):  # triage; and a user GitHub does not know
            self.world.label_pull(7, login=login)
            self.reconcile(self.directory)
        self.assertEqual(self.world.writes("body", "status"), [])

    def test_a_bot_is_ignored_whatever_it_may_do(self):
        self.world.data["permissions"]["factory-bot"] = "admin"
        self.world.label_pull(7, login="factory-bot", kind="Bot")
        self.reconcile(self.directory)
        self.assertEqual(self.world.writes("body", "status"), [])

    def test_the_minimum_permission_comes_from_the_repository_config(self):
        self.cfg = RepoConfig(repo="org/name", auto_build_min_permission="maintain")
        self.reconcile(self.directory)
        self.assertEqual(self.world.writes("body", "status"), [])  # alice holds write only
        self.world.label_pull(7, login="carol")
        self.reconcile(self.directory)
        self.assertEqual(len(self.world.writes("body")), 1)

    def test_a_minimum_below_write_on_a_public_repository_is_refused(self):
        self.cfg = RepoConfig(repo="org/name", auto_build_min_permission="triage")
        self.world.data["private"] = False
        self.world.label_pull(7, login="bob")
        self.reconcile(self.directory)
        self.assertEqual(self.world.writes("body", "status"), [])

    def test_a_moved_head_is_refused_with_one_fixed_line_and_no_write(self):
        self.world.pull(7, head=MOVED, body=self.body)
        lines = self.reconcile(self.directory)
        self.assertEqual(lines, ["factory: verified completion refused: the pull request head is not the published commit (#7)"])
        self.assertEqual(self.world.writes("body", "status"), [])

    def test_no_label_no_action_and_a_job_that_is_not_incomplete_is_not_considered(self):
        self.world.data["labelled"] = {}
        self.reconcile(self.directory)
        self.assertEqual(self.world.writes("body", "status"), [])
        self.run_dir("r2", plan_for(("ready", [3])), {"ready": ("pr_ready", 7, HEAD)})
        self.world.label_pull(7)
        self.reconcile(self.runs / "r2")
        self.assertEqual(self.world.writes("body", "status"), [])

    def test_a_closed_pull_request_is_not_completed(self):
        self.world.pull(7, state="closed", head=HEAD, body=self.body)
        self.reconcile(self.directory)
        self.assertEqual(self.world.writes("body", "status"), [])

    def test_a_body_that_no_longer_starts_as_published_is_left_alone(self):
        self.world.pull(7, head=HEAD, body=body_of("Some human wrote this"))
        lines = self.reconcile(self.directory)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("factory: verified completion skipped"))
        self.assertEqual(self.world.writes("body", "status"), [])

    def test_failures_are_swallowed_and_never_leak_text(self):
        self.world.data["fail"] = True
        lines = self.reconcile(self.directory)
        self.assertTrue(lines)
        self.assertTrue(all(line.startswith("factory: ") for line in lines))
        self.assertTrue(all(len(line) < 100 for line in lines))

    def test_a_failed_body_write_posts_no_gate(self):
        original = self.world.data
        path = self.root / "bin" / "gh"
        path.write_text(path.read_text().replace("if verb == 'PATCH':", "if verb == 'PATCH':\n            fail('HTTP 403')"))
        self.reconcile(self.directory)
        self.assertEqual(self.world.writes("status"), [])
        self.assertIs(original, self.world.data)


class RewriteTests(unittest.TestCase):
    def test_only_the_first_line_changes_and_line_endings_are_kept(self):
        for first, issues, closing, expected in (
            ("Refs #1", [1], lambda n: True, "Closes #1"),
            ("Refs #1, refs #2", [1, 2], lambda n: n == 1, "Closes #1, refs #2"),
            ("Refs #1, refs #2", [1, 2], lambda n: False, "Refs #1, refs #2"),
            ("Closes #1", [1], lambda n: True, "Closes #1"),
        ):
            with self.subTest(first=first):
                for ending in ("\n", "\r\n"):
                    body = first + ending + "\nrest\nRefs #1\n"
                    self.assertEqual(rewritten_body(body, issues, closing), expected + ending + "\nrest\nRefs #1\n")

    def test_a_first_line_that_is_not_the_published_one_is_refused(self):
        self.assertIsNone(rewritten_body("Closes #9\n\nrest", [1], lambda n: True))
        self.assertIsNone(rewritten_body("", [1], lambda n: True))


    def test_the_closing_notice_under_the_first_line_follows_the_rewritten_line(self):
        from factory.github import closing_notice

        body = f"Refs #1\n\n{closing_notice('Refs #1')}\n\n## Assignment\n\nwork\n"
        done = rewritten_body(body, [1], lambda n: True)
        self.assertEqual(done, f"Closes #1\n\n{closing_notice('Closes #1')}\n\n## Assignment\n\nwork\n")
        kept = rewritten_body(body, [1], lambda n: False)  # an ignored issue stays Refs: the notice stays as it was
        self.assertEqual(kept, body)


if __name__ == "__main__":
    unittest.main()

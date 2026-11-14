"""``plan --issues N,N,...``: exactly the listed issues, with one fixed line per issue left out.

A fake ``gh`` serves a small GitHub from $FAKE_DIR/github.json (issues by number: state, pull
request or not, labels, text) and logs every call, so the tests can show which issues were
fetched at all. The number parser is shared with the manager's ``issues`` plan field.
"""

import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from factory import cli, plan_failure
from factory.issue_numbers import (
    MAX_ISSUE_NUMBER,
    MAX_PLAN_ISSUES,
    check_issue_numbers,
    issues_argument,
    issues_flag,
    parse_issue_numbers,
)
from factory.planner import plan, snapshot, snapshot_issues

SECRET = "SECRET-ISSUE-TEXT-do-not-print"

FAKE_GH = r"""
import json, os, pathlib, re, sys
root = pathlib.Path(os.environ['FAKE_DIR'])
args = sys.argv[1:]
with open(root / 'calls.jsonl', 'a') as log:
    log.write(json.dumps(args) + '\n')
data = json.loads((root / 'github.json').read_text())
issues = data['issues']
path = next((a for a in args[1:] if a.startswith('repos/')), '')
match = re.fullmatch(r'repos/[^/]+/[^/]+/issues/(\d+)', path)
if args[:1] == ['api'] and match:
    if data.get('api_fails'):
        sys.stderr.write('HTTP 500 server error ' + os.environ['FAKE_SECRET'])
        sys.exit(1)
    item = issues.get(match.group(1))
    if item is None:
        sys.stderr.write('gh: Not Found (HTTP 404)')
        sys.exit(1)
    print(json.dumps({'state': item['state'], 'pr': item.get('pr', False), 'labels': item['labels']}))
elif args[:2] == ['issue', 'view']:
    item = dict(issues[args[2]])
    labels = item['labels'] + item.get('late_labels', [])
    print(json.dumps({'number': int(args[2]), 'title': item['title'], 'body': item['body'], 'comments': [],
                      'labels': [{'name': name} for name in labels], 'url': 'https://example.invalid/' + args[2]}))
elif args[:2] == ['pr', 'list']:
    print('[]')
elif args[:2] == ['label', 'create']:
    pass
else:
    raise RuntimeError(args)
"""


def entry(title, *labels, state="open", pr=False, late=()):
    return {"title": title, "body": SECRET + title, "labels": list(labels), "state": state, "pr": pr,
            "late_labels": list(late)}


class FakeGh(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text(f"#!{sys.executable}\n" + FAKE_GH)
        gh.chmod(0o755)
        self.work = self.root / "work"
        self.work.mkdir()
        patcher = patch.dict(
            os.environ,
            {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_DIR": str(self.root), "FAKE_SECRET": SECRET},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.github = {"issues": {}}

    def serve(self, **issues):
        self.github["issues"] = {name.removeprefix("n"): item for name, item in issues.items()}
        (self.root / "github.json").write_text(json.dumps(self.github))

    def calls(self):
        path = self.root / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def fetched(self):
        """Numbers of the issues whose text was asked for."""
        return [int(call[2]) for call in self.calls() if call[:2] == ["issue", "view"]]

    def capture(self, function, *args, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                result = function(*args, **kwargs)
            finally:
                self.out, self.err = out.getvalue(), err.getvalue()
        return result


class SnapshotIssuesTests(FakeGh):
    def test_the_listed_issues_are_captured_in_the_order_given(self):
        self.serve(n5=entry("five"), n2=entry("two", "bug"), n9=entry("nine"))
        issues = self.capture(snapshot_issues, self.work, [9, 2, 5], "org/name")
        self.assertEqual([i["number"] for i in issues], [9, 2, 5])
        self.assertEqual(self.fetched(), [9, 2, 5])
        self.assertEqual(self.err, "")

    def test_every_dropped_issue_gets_one_fixed_line_and_is_never_fetched_by_text(self):
        self.serve(
            n1=entry("closed one", state="closed"),
            n2=entry("ignored", "Factory-Ignore"),
            n3=entry("a pull request", pr=True),
            n4=entry("kept"),
        )
        issues = self.capture(snapshot_issues, self.work, [1, 2, 3, 4, 99], "org/name")
        self.assertEqual([i["number"] for i in issues], [4])
        self.assertEqual(
            self.err.splitlines(),
            [
                "factory: plan issue=1 dropped reason=closed",
                "factory: plan issue=2 dropped reason=ignored",
                "factory: plan issue=3 dropped reason=not-an-issue",
                "factory: plan issue=99 dropped reason=missing",
            ],
        )
        self.assertEqual(self.fetched(), [4])  # only the kept issue's title and body were ever read
        self.assertNotIn(SECRET, self.out + self.err)
        self.assertFalse([c for c in self.calls() if "--search" in c or c[:2] == ["issue", "list"]])  # never by title

    def test_the_ignore_label_is_checked_again_on_the_full_read(self):
        self.serve(n1=entry("raced", late=["factory-ignore"]), n2=entry("fine"))
        issues = self.capture(snapshot_issues, self.work, [1, 2], "org/name")
        self.assertEqual([i["number"] for i in issues], [2])
        self.assertEqual(self.err.splitlines(), ["factory: plan issue=1 dropped reason=ignored"])

    def test_nothing_left_is_an_error_with_a_fixed_message(self):
        self.serve(n1=entry("closed", state="closed"))
        with self.assertRaises(ValueError) as caught:
            self.capture(snapshot_issues, self.work, [1, 404], "org/name")
        self.assertEqual(str(caught.exception), "none of the listed issues can be planned")

    def test_a_failure_that_is_not_a_404_fails_the_plan_instead_of_planning_less(self):
        self.serve(n1=entry("one"))
        self.github["api_fails"] = True
        (self.root / "github.json").write_text(json.dumps(self.github))
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            self.capture(snapshot_issues, self.work, [1], "org/name")
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertNotIn(SECRET, self.out + self.err)

    def test_the_repository_is_named_on_every_call(self):
        self.serve(n1=entry("one"))
        self.capture(snapshot_issues, self.work, [1], "org/name")
        head, view = self.calls()
        self.assertIn("repos/org/name/issues/1", head)
        self.assertEqual(view[view.index("-R") + 1], "org/name")

    def test_snapshot_with_numbers_writes_only_the_kept_issues(self):
        self.serve(n1=entry("kept"), n2=entry("ignored", "factory-ignore"))
        destination = self.work / "snap"
        issues, prs = self.capture(snapshot, self.work, destination, 30, "org/name", [1, 2])
        self.assertEqual([i["number"] for i in issues], [1])
        saved = json.loads((destination / "issues.json").read_text())
        self.assertEqual([i["number"] for i in saved], [1])
        self.assertNotIn("ignored", (destination / "issues.json").read_text())
        self.assertEqual(json.loads((destination / "open-prs.json").read_text()), [])
        self.assertEqual(prs, [])

    def test_the_plain_snapshot_is_unchanged_when_no_numbers_are_given(self):
        self.serve()
        (self.root / "issues.json").write_text("[]")
        # the plain listing path is covered in tests/test_issue_labels.py; here: numbers=None must not use `issue view`
        with patch("factory.planner.command", side_effect=["[]", "[]"]) as command:
            snapshot(self.work, self.work / "snap", 5, "org/name")
        self.assertEqual(command.call_args_list[0].args[0][:3], ["gh", "issue", "list"])


class PlanWithIssuesTests(FakeGh):
    def test_the_planner_sees_only_the_listed_issues(self):
        self.serve(n1=entry("one"), n2=entry("two"), n3=entry("three", "factory-ignore"))
        seen = {}

        def fake_agent(prompt, *args, **kwargs):
            seen["prompt"] = prompt
            plan_document = {
                "version": 1,
                "jobs": [{"id": "fix-a", "issues": [2], "context_issues": [], "objective": "Fix", "likely_paths": ["src"],
                          "acceptance": ["ok"], "depends_on": [], "feature": False}],
                "dispositions": [{"issue": 2, "status": "job", "reason": "r", "job": "fix-a"}],
            }
            return plan_document, {"phase": "plan"}

        with patch("factory.planner.run_agent", fake_agent), patch("factory.planner.repo_guidance", return_value=""):
            result = self.capture(plan, self.work, self.work / "run", 30, slug="org/name", numbers=[2, 3])
        self.assertEqual([i["number"] for i in result["issues"]], [2])
        self.assertEqual(self.fetched(), [2])
        self.assertEqual(self.err.splitlines(), ["factory: plan issue=3 dropped reason=ignored"])
        self.assertNotIn("three", seen["prompt"])
        self.assertNotIn("three", (self.work / "run" / "issues.json").read_text())

    def test_a_directory_written_with_a_trailing_slash_does_not_reject_the_plan(self):
        # seen in production, 2026-11-09: the planner named "docs/decisions/" in all six attempts, the validator wanted the canonical
        # spelling, and the whole plan failed with "artifact paths must be normalized and cannot traverse directories" every time
        self.serve(n2=entry("two"))

        def fake_agent(prompt, *args, **kwargs):
            document = {
                "version": 1,
                "jobs": [{"id": "fix-a", "issues": [2], "context_issues": [], "objective": "Fix",
                          "likely_paths": ["docs/decisions/", "./pipelines", "src//x", "src/./y", "pipelines"],
                          "acceptance": ["ok"], "depends_on": [], "feature": False}],
                "dispositions": [{"issue": 2, "status": "job", "reason": "r", "job": "fix-a"}],
            }
            return document, {"phase": "plan"}

        with patch("factory.planner.run_agent", fake_agent), patch("factory.planner.repo_guidance", return_value=""):
            result = self.capture(plan, self.work, self.work / "run", 30, slug="org/name", numbers=[2])
        self.assertEqual(result["jobs"][0]["likely_paths"], ["docs/decisions", "pipelines", "src/x", "src/y"])  # spelled canonically, deduplicated
        stored = json.loads((self.work / "run" / "plan.json").read_text())
        self.assertEqual(stored["jobs"][0]["likely_paths"], ["docs/decisions", "pipelines", "src/x", "src/y"])

    def test_a_path_that_is_wrong_for_more_than_its_spelling_still_fails_the_plan(self):
        self.serve(n2=entry("two"))
        for bad in ("../outside", "/etc/passwd", "src/*.py", "a\\b", "src/../x"):
            with self.subTest(path=bad):
                def fake_agent(prompt, *args, **kwargs):
                    document = {
                        "version": 1,
                        "jobs": [{"id": "fix-a", "issues": [2], "context_issues": [], "objective": "Fix", "likely_paths": ["src", bad],
                                  "acceptance": ["ok"], "depends_on": [], "feature": False}],
                        "dispositions": [{"issue": 2, "status": "job", "reason": "r", "job": "fix-a"}],
                    }
                    return document, {"phase": "plan"}

                with patch("factory.planner.run_agent", fake_agent), patch("factory.planner.repo_guidance", return_value=""):
                    with self.assertRaises(ValueError):
                        self.capture(plan, self.work, self.work / "run", 30, slug="org/name", numbers=[2])

    def test_the_planner_prompt_states_the_session_time_limit(self):
        self.serve(n2=entry("two"))
        seen = {}

        def fake_agent(prompt, *args, **kwargs):
            seen["prompt"] = prompt
            return {"version": 1, "jobs": [], "dispositions": [{"issue": 2, "status": "blocked", "reason": "r"}]}, {"phase": "plan"}

        for timeout, minutes in ((1800, "30"), (5400, "90")):
            with patch("factory.planner.run_agent", fake_agent), patch("factory.planner.repo_guidance", return_value=""):
                self.capture(plan, self.work, self.work / f"run-{timeout}", 30, timeout=timeout, slug="org/name", numbers=[2])
            self.assertIn("Session time limit: the agent sessions of the run that follows", seen["prompt"])
            self.assertIn(f"killed after {timeout} seconds ({minutes} minutes)", seen["prompt"])
            self.assertIn("Do not group issues into one job when the group would need more than that", seen["prompt"])
            self.assertIn("One issue is always one job", seen["prompt"])
            self.assertIn("disposition reason", seen["prompt"])
            self.assertNotIn("Split work you expect", seen["prompt"])

    def run_failing(self, document=None, error=None, **options):
        """``plan --use-snapshot`` in a run directory that holds only the captured evidence: the files the failure added."""
        run = self.work / "run"
        run.mkdir()
        (run / "issues.json").write_text(json.dumps(
            [{"number": 2, "title": SECRET, "body": SECRET, "comments": [], "labels": [], "url": ""}]))
        (run / "open-prs.json").write_text("[]")

        def fake_agent(prompt, *args, **kwargs):
            if error is not None:
                raise error
            return document, {"phase": "plan"}

        before = {path.name for path in run.iterdir()}
        with patch("factory.planner.run_agent", fake_agent), patch("factory.planner.repo_guidance", return_value=""):
            with self.assertRaises(Exception):
                self.capture(plan, run, run, 30, use_snapshot=True, **options)
        self.assertEqual({path.name for path in run.iterdir()} - before, {plan_failure.FILE})
        self.assertFalse((run / "plan.json").exists())
        return (run / plan_failure.FILE).read_text()

    @staticmethod
    def document(paths=("src",), **job):
        job = {"id": "fix-a", "issues": [2], "context_issues": [], "objective": "Fix", "likely_paths": list(paths),
               "acceptance": ["ok"], "depends_on": [], "feature": False, **job}
        return {"version": 1, "jobs": [job], "dispositions": [{"issue": 2, "status": "job", "reason": "r", "job": "fix-a"}]}

    def test_a_plan_that_fails_leaves_one_closed_code_and_no_plan(self):
        from subprocess import TimeoutExpired

        from factory.agent import AgentError

        cases = [
            ("invalid_plan_paths", dict(document=self.document(("src", "../outside")))),
            ("invalid_plan_paths", dict(document=self.document(("/etc/passwd",)))),
            ("schema_invalid", dict(document=self.document(feature="yes"))),
            ("schema_invalid", dict(document=self.document(id="Not A Slug"))),
            ("schema_invalid", dict(document={"version": 2, "jobs": [], "dispositions": []})),
            ("schema_invalid", dict(document={"version": 1})),
            ("planner_error", dict(error=AgentError(SECRET))),
            ("planner_error", dict(error=RuntimeError(SECRET))),
            ("timeout", dict(error=TimeoutExpired(["agent", SECRET], 5))),
        ]
        for code, options in cases:
            with self.subTest(code=code, case=sorted(options)):
                shutil.rmtree(self.work / "run", ignore_errors=True)
                self.assertEqual(self.run_failing(**options), code + "\n")
                self.assertIn(code, plan_failure.CODES)
                self.assertNotIn(SECRET, (self.work / "run" / plan_failure.FILE).read_text())

    def test_a_missing_capture_is_a_snapshot_failure(self):
        run = self.work / "run"
        run.mkdir()
        with self.assertRaises(OSError):
            self.capture(plan, self.work, run, 30, use_snapshot=True)
        self.assertEqual(sorted(path.name for path in run.iterdir()), [plan_failure.FILE])
        self.assertEqual(plan_failure.read(run), plan_failure.SNAPSHOT_FAILED)

    def test_a_failed_snapshot_from_github_is_a_snapshot_failure(self):
        self.github["api_fails"] = True
        self.serve(n2=entry("two"))
        (self.root / "github.json").write_text(json.dumps({**self.github, "api_fails": True}))
        with self.assertRaises(Exception):
            self.capture(plan, self.work, self.work / "run", 30, slug="org/name", numbers=[2])
        self.assertEqual(plan_failure.read(self.work / "run"), plan_failure.SNAPSHOT_FAILED)
        self.assertFalse((self.work / "run" / "plan.json").exists())

    def test_a_plan_that_succeeds_removes_an_earlier_code_and_writes_none(self):
        self.serve(n2=entry("two"))
        run = self.work / "run"
        run.mkdir()
        plan_failure.write(run, plan_failure.TIMEOUT)
        with patch("factory.planner.run_agent", lambda *a, **k: (self.document(), {"phase": "plan"})), patch(
            "factory.planner.repo_guidance", return_value=""
        ):
            self.capture(plan, self.work, run, 30, slug="org/name", numbers=[2])
        self.assertFalse((run / plan_failure.FILE).exists())
        self.assertTrue((run / "plan.json").is_file())

    def test_the_code_file_helpers_never_record_text_outside_the_table(self):
        run = self.work / "run"
        plan_failure.write(run, f"timeout {SECRET}")
        self.assertEqual((run / plan_failure.FILE).read_text(), "other\n")
        self.assertEqual(plan_failure.read(run), "other")
        self.assertEqual(plan_failure.read(self.work / "absent"), "other")

    def test_plan_command_refuses_issues_together_with_limit_before_touching_anything(self):
        directory = self.root / "run"
        args = SimpleNamespace(limit=5, issues=[1], timeout=10, use_snapshot=False)
        with self.assertRaises(ValueError) as caught:
            cli.plan_command(args, SimpleNamespace(), self.work, directory, None)
        self.assertEqual(str(caught.exception), "--issues and --limit cannot be combined")
        self.assertFalse(directory.exists())
        self.assertEqual(self.calls(), [])


class ArgumentTests(unittest.TestCase):
    def parse(self, *argv):
        return cli.build_parser().parse_args(["--repo", "org/name", "--run-dir", "/tmp/r1", "plan", *argv])

    def test_a_plain_plan_keeps_its_behaviour(self):
        args = self.parse()
        self.assertIsNone(args.issues)
        self.assertIsNone(args.limit)  # the command applies the default of 30
        self.assertEqual(cli.DEFAULT_PLAN_LIMIT, 30)
        self.assertEqual(self.parse("--limit", "7").limit, 7)

    def test_issues_are_parsed_deduplicated_and_ordered(self):
        self.assertEqual(self.parse("--issues", "3,5,7").issues, [3, 5, 7])
        self.assertEqual(self.parse("--issues=7,3,7,3,9").issues, [7, 3, 9])
        self.assertEqual(self.parse("--issues", " 4 , 2").issues, [4, 2])
        self.assertEqual(self.parse("--issues", "12").issues, [12])

    def test_bad_lists_are_a_usage_error(self):
        for bad in ("", ",", "3,", ",3", "3,,4", "a", "3,x", "#3", "-3", "+3", "0", "3.5", "１２", "3;4", "3 4", "٣",
                    str(MAX_ISSUE_NUMBER + 1), "9" * 30):
            with self.subTest(bad), contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as caught:
                self.parse(f"--issues={bad}")
            self.assertEqual(caught.exception.code, 2)
            self.assertNotIn("SECRET", err.getvalue())

    def test_a_huge_list_is_refused_and_the_largest_allowed_one_is_not(self):
        numbers = list(range(1, MAX_PLAN_ISSUES + 1))
        self.assertEqual(self.parse("--issues", ",".join(map(str, numbers))).issues, numbers)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.parse("--issues", ",".join(map(str, range(1, MAX_PLAN_ISSUES + 2))))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.parse("--issues", ",".join(["1"] * 100_000))  # duplicates do not make it small enough to be read


class IssueNumberTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_issue_numbers("1,2,3"), [1, 2, 3])
        self.assertEqual(parse_issue_numbers("2147483647"), [MAX_ISSUE_NUMBER])
        self.assertEqual(parse_issue_numbers("007"), [7])
        for bad in ("", "1,,2", "1,a", None, 5, "1" * 11):
            with self.subTest(repr(bad)), self.assertRaises(ValueError):
                parse_issue_numbers(bad)

    def test_check_accepts_only_real_ints_and_removes_duplicates(self):
        self.assertEqual(check_issue_numbers([5, 3, 5, 1]), [5, 3, 1])
        for bad in ([], (), "1,2", None, [True], [1.0], ["1"], [0], [-1], [MAX_ISSUE_NUMBER + 1], [None], [[1]],
                    list(range(1, MAX_PLAN_ISSUES + 2))):
            with self.subTest(repr(bad)[:40]), self.assertRaises(ValueError):
                check_issue_numbers(bad)

    def test_the_messages_never_echo_the_value(self):
        for bad in ("3,SECRET", ["SECRET"], [10**30]):
            with self.assertRaises(ValueError) as caught:
                parse_issue_numbers(bad) if isinstance(bad, str) else check_issue_numbers(bad)
            self.assertNotIn("SECRET", str(caught.exception))
            self.assertNotIn(str(10**30), str(caught.exception))

    def test_argparse_type_and_flag(self):
        self.assertEqual(issues_argument("1,2"), [1, 2])
        with self.assertRaises(argparse.ArgumentTypeError):
            issues_argument("x")
        self.assertEqual(issues_flag([3, 5, 7]), "--issues=3,5,7")


if __name__ == "__main__":
    unittest.main()

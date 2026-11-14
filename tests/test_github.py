"""gh helpers: JSON parsing, required checks, hand-off status, gate status, labels."""

import re
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from factory import github
from factory.github import (
    GATE_CONTEXT,
    ci_assessment,
    default_branch,
    ensure_labels,
    failed_checks,
    handoff_status,
    neutralise_refs,
    parse_gh_json,
    post_gate_status,
    repo_args,
    repo_path,
    required_checks,
)


def passing_pr(**changes):
    """A published PR as ``gh pr view --json`` reports it."""
    pr = {
        "headRefOid": "a" * 40,
        "baseRefName": "main",
        "isDraft": False,
        "state": "OPEN",
        "statusCheckRollup": [
            {
                "__typename": "CheckRun",
                "name": "tests",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            },
            {"__typename": "StatusContext", "context": GATE_CONTEXT, "state": "SUCCESS"},
        ],
        "mergeCommit": {"oid": "b" * 40},
    }
    pr.update(changes)
    return pr


def check_run(name, conclusion, status="COMPLETED"):
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion}


class ParsingTests(unittest.TestCase):
    def test_required_checks_empty_or_invalid_json_is_actionable(self):
        for output in ("", "not-json", "[null]"):
            result = subprocess.CompletedProcess([], 1, output, "")
            with (
                patch("factory.github.subprocess.run", return_value=result),
                self.assertRaisesRegex(
                    ValueError, "(required checks|required check response)"
                ),
            ):
                required_checks(Path("/repo"), "pr")

    def test_explicit_no_required_checks_remains_supported(self):
        result = subprocess.CompletedProcess([], 1, "", "no required checks reported")
        with patch("factory.github.subprocess.run", return_value=result):
            self.assertEqual(required_checks(Path("/repo"), "pr"), [])

    def test_json_parser_rejects_wrong_container(self):
        with self.assertRaisesRegex(
            ValueError, "find existing PR.*unexpected JSON type"
        ):
            parse_gh_json("null", "find existing PR", list)

    def test_parse_errors_never_echo_the_payload(self):
        with self.assertRaises(ValueError) as caught:
            parse_gh_json("PRIVATE {not json", "op", dict)
        self.assertNotIn("PRIVATE", str(caught.exception))


class HandoffTests(unittest.TestCase):
    def test_states(self):
        self.assertEqual(handoff_status(passing_pr()), "published")
        self.assertEqual(handoff_status(passing_pr(state="MERGED")), "merged")
        self.assertEqual(handoff_status(passing_pr(state="CLOSED")), "closed")
        failing = passing_pr(statusCheckRollup=[check_run("tests", "FAILURE")])
        self.assertEqual(handoff_status(failing), "ci_failed")
        self.assertEqual(handoff_status(None), "published")

    def test_only_positive_failures_count(self):
        rollup = [
            check_run("pending", None, status="IN_PROGRESS"),
            check_run("cancelled", "CANCELLED"),
            check_run("neutral", "NEUTRAL"),
            check_run("skipped", "SKIPPED"),
            {"__typename": "StatusContext", "context": "ci/x", "state": "PENDING"},
        ]
        self.assertEqual(failed_checks(rollup), [])
        self.assertEqual(
            failed_checks(rollup + [check_run("bad", "TIMED_OUT")]), ["bad"]
        )
        self.assertEqual(
            failed_checks(
                [{"__typename": "StatusContext", "context": "ci/y", "state": "ERROR"}]
            ),
            ["ci/y"],
        )

    def test_the_factory_gate_is_not_ci(self):
        incomplete = passing_pr(
            statusCheckRollup=[
                check_run("tests", "SUCCESS"),
                {"__typename": "StatusContext", "context": GATE_CONTEXT, "state": "FAILURE"},
            ]
        )
        self.assertEqual(handoff_status(incomplete), "published")

    def test_non_required_failure_does_not_fail_the_handoff(self):
        pr = passing_pr(
            statusCheckRollup=[
                check_run("tests", "SUCCESS"),
                check_run("enable auto-merge", "FAILURE"),
            ],
            requiredChecks=[{"name": "tests", "bucket": "pass"}],
        )
        self.assertEqual(handoff_status(pr), "published")
        self.assertEqual(
            ci_assessment(pr),
            {"basis": "required", "failed": [], "informational": ["enable auto-merge"]},
        )

    def test_failing_required_check_fails_the_handoff(self):
        pr = passing_pr(
            statusCheckRollup=[
                check_run("tests", "FAILURE"),
                check_run("enable auto-merge", "FAILURE"),
            ],
            requiredChecks=[{"name": "tests", "bucket": "fail"}],
        )
        self.assertEqual(handoff_status(pr), "ci_failed")
        assessment = ci_assessment(pr)
        self.assertEqual(assessment["failed"], ["tests"])
        self.assertEqual(assessment["informational"], ["enable auto-merge"])

    def test_empty_required_set_lets_the_whole_rollup_decide(self):
        pr = passing_pr(
            statusCheckRollup=[check_run("lint", "FAILURE")], requiredChecks=[]
        )
        self.assertEqual(handoff_status(pr), "ci_failed")
        self.assertEqual(
            ci_assessment(pr),
            {"basis": "rollup_no_required", "failed": ["lint"], "informational": []},
        )

    def test_gate_alone_failing_is_never_ci_failed_under_any_basis(self):
        gate = {"__typename": "StatusContext", "context": GATE_CONTEXT, "state": "FAILURE"}
        for required in ([], None, [{"name": "tests", "bucket": "pass"}]):
            pr = passing_pr(statusCheckRollup=[gate], requiredChecks=required)
            self.assertEqual(handoff_status(pr), "published")
            self.assertEqual(ci_assessment(pr)["failed"], [])

    def test_unreadable_required_set_falls_back_to_the_rollup(self):
        failing = passing_pr(statusCheckRollup=[check_run("lint", "FAILURE")])
        for unread in (None, "garbage"):
            pr = dict(failing, requiredChecks=unread)
            self.assertEqual(handoff_status(pr), "ci_failed")
            self.assertEqual(ci_assessment(pr)["basis"], "rollup_fallback")
        self.assertEqual(handoff_status(passing_pr(requiredChecks=None)), "published")

    def test_gate_is_never_a_failure_as_required_check_or_in_the_rollup(self):
        pr = passing_pr(
            statusCheckRollup=[
                {"__typename": "StatusContext", "context": GATE_CONTEXT, "state": "FAILURE"}
            ],
            requiredChecks=[{"name": GATE_CONTEXT, "bucket": "fail"}],
        )
        self.assertEqual(handoff_status(pr), "published")
        self.assertEqual(handoff_status(dict(pr, requiredChecks=None)), "published")

    def test_inspect_pr_attaches_required_set_or_none_when_unreadable(self):
        view = '{"state": "OPEN", "statusCheckRollup": []}'
        required = [{"name": "tests", "bucket": "pass"}]
        with (
            patch("factory.github.command", return_value=view),
            patch("factory.github.required_checks", return_value=required),
        ):
            self.assertEqual(github.inspect_pr(Path("/repo"), "pr")["requiredChecks"], required)
        for error in (RuntimeError("gh"), ValueError("json"), OSError("no gh")):
            with (
                patch("factory.github.command", return_value=view),
                patch("factory.github.required_checks", side_effect=error),
            ):
                self.assertIsNone(github.inspect_pr(Path("/repo"), "pr")["requiredChecks"])

    def test_malformed_rollups_are_tolerated(self):
        self.assertEqual(failed_checks(None), [])
        self.assertEqual(failed_checks(["x", 3, None]), [])


CLOSING = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b[:\s]+(?:[\w.-]+/[\w.-]+)?#\d+", re.IGNORECASE
)


class ClosingNoticeTests(unittest.TestCase):
    """The line under the first line says in words whether merging closes the issue (Refs leaves it open), without a keyword."""

    FIRSTS = (
        "Closes #7",
        "Refs #7",
        "Closes #7, closes #8",
        "Closes #7, refs #8",
        "Refs #7, refs #8",
        "Refs #7, closes #8",
        "Closes org/repo#9, refs #3",
        "Closes #5, closes #3",
    )

    def test_a_closing_line_says_which_issues_are_closed_and_which_only_mentioned(self):
        self.assertEqual(github.closing_notice("Closes #7"), "> **Merging this pull request closes these issues: #7.**")
        self.assertEqual(
            github.closing_notice("Closes #7, refs #8"),
            "> **Merging this pull request closes these issues: #7.** It only mentions #8, which stays open.",
        )
        self.assertIn("#7, #8.", github.closing_notice("Closes #7, closes #8"))
        self.assertIn("org/repo#9", github.closing_notice("Closes org/repo#9, refs #3"))

    def test_a_refs_only_line_says_plainly_that_the_issue_stays_open_and_what_to_do(self):
        text = github.closing_notice("Refs #7")
        self.assertIn("does not close the issue it references (#7)", text)
        self.assertIn("could not prove the change complete", text)
        self.assertIn("`factory-verified`", text)
        self.assertIn("issues it references (#7, #8)", github.closing_notice("Refs #7, refs #8"))

    def test_the_notice_never_holds_a_closing_keyword_before_an_issue_number(self):
        """GitHub reads a keyword without understanding a negation: 'does not close #7' would close #7."""
        for first in self.FIRSTS:
            with self.subTest(first=first):
                notice = github.closing_notice(first)
                self.assertEqual(CLOSING.findall(notice), [], notice)
                self.assertTrue(notice.startswith(github.NOTICE_MARK))
                self.assertNotIn("\n", notice)

    def test_no_line_or_unreadable_line_gives_no_notice(self):
        for first in ("", "nothing here", "Part of #7"):
            self.assertEqual(github.closing_notice(first), "")

    def test_refresh_rewrites_the_notice_to_match_a_new_first_line_and_only_that_line(self):
        old = github.closing_notice("Refs #7")
        rest = f"\n\n{old}\n\n## Assignment\n\nbody\nRefs #1\n"
        new = github.refresh_notice("Closes #7", rest)
        self.assertEqual(new, f"\n\n{github.closing_notice('Closes #7')}\n\n## Assignment\n\nbody\nRefs #1\n")
        self.assertEqual(github.refresh_notice("Refs #7", new).count("does not close the issue"), 1)

    def test_refresh_keeps_line_endings_and_leaves_a_body_without_a_notice_alone(self):
        rest = f"\r\n\r\n{github.closing_notice('Refs #7')}\r\n\r\nrest"
        self.assertEqual(github.refresh_notice("Closes #7", rest).split("\r\n")[2], github.closing_notice("Closes #7"))
        plain = "\n\n## Assignment\n\nbody"
        self.assertEqual(github.refresh_notice("Closes #7", plain), plain)
        self.assertEqual(github.refresh_notice("nothing", f"\n\n{github.closing_notice('Refs #7')}\nx"), f"\n\n{github.closing_notice('Refs #7')}\nx")


class NeutraliseTests(unittest.TestCase):
    def test_agent_text_can_no_longer_reference_or_close_an_issue(self):
        hostile = [
            "Closes #77",
            "fixes #78 and resolves org/other#79",
            "Resolved: #80",
            "see https://github.com/org/other/issues/81 and https://github.com/o/r/pull/82",
            "GH-83 closes GH-84",
            "closes\n#85",
        ]
        for text in hostile:
            with self.subTest(text=text):
                clean = neutralise_refs(text)
                self.assertEqual(CLOSING.findall(clean), [], clean)
                self.assertNotRegex(clean, r"#\d")
                self.assertNotRegex(clean, r"/(?:issues|pull)/\d")
                self.assertNotRegex(clean, r"GH-\d")
        # the text still reads the same once the invisible characters are ignored
        self.assertEqual(neutralise_refs("Closes #77").replace("\u200b", ""), "Closes #77")

    def test_ordinary_text_is_unchanged(self):
        for text in ("Fix the parser", "uses C# and F#", "a#b", "version 1.2.3", "path/to/file.py", "&#39;"):
            self.assertEqual(neutralise_refs(text), text)

    def test_non_strings_are_coerced(self):
        self.assertEqual(neutralise_refs(12), "12")


class GhCallTests(unittest.TestCase):
    def test_gate_status_is_posted_with_the_factory_context_on_the_exact_sha(self):
        with patch("factory.github.command") as run:
            post_gate_status(Path("/r"), "org/name", "c" * 40, "success", "review+verify passed @ccccccc")
        args = run.call_args.args[0]
        self.assertEqual(args[:5], ["gh", "api", "-X", "POST", f"repos/org/name/statuses/{'c' * 40}"])
        self.assertIn("context=factory/gate", args)
        self.assertIn("state=success", args)
        self.assertIn("description=review+verify passed @ccccccc", args)

    def test_gate_status_state_is_validated_and_description_bounded(self):
        with self.assertRaises(ValueError):
            post_gate_status(Path("/r"), "o/n", "c" * 40, "merged", "x")
        with patch("factory.github.command") as run:
            post_gate_status(Path("/r"), None, "c" * 40, "pending", "d" * 500)
        args = run.call_args.args[0]
        self.assertEqual(args[4], f"repos/{{owner}}/{{repo}}/statuses/{'c' * 40}")
        self.assertEqual(len([a for a in args if a.startswith("description=")][0]), len("description=") + 140)

    def test_base_required_checks_never_counts_the_factory_gate_as_proof(self):
        protection = '{"contexts": ["build", "factory/gate"], "checks": [{"context": "lint"}]}'
        rules = (
            '[{"type": "required_status_checks", "parameters": {"required_status_checks": '
            '[{"context": "factory/gate"}, {"context": "scan"}]}}]'
        )
        outputs = iter([protection, rules])

        def fake(*args, **kwargs):
            return subprocess.CompletedProcess(args[0], 0, stdout=next(outputs), stderr="")

        with patch("factory.github.supervisor_env", return_value={}), patch("factory.github.subprocess.run", side_effect=fake):
            names = github.base_required_checks(Path("/r"), "o/n", "main")
        self.assertEqual(names, {"build", "lint", "scan"})

    def test_base_required_checks_survives_a_hung_or_missing_gh(self):
        for error in (subprocess.TimeoutExpired("gh", 120), FileNotFoundError("gh")):
            with patch("factory.github.supervisor_env", return_value={}), patch(
                "factory.github.subprocess.run", side_effect=error
            ):
                self.assertEqual(github.base_required_checks(Path("/r"), "o/n", "main"), set())

    def test_repo_helpers(self):
        self.assertEqual(repo_args("o/n"), ["-R", "o/n"])
        self.assertEqual(repo_args(None), [])
        self.assertEqual(repo_path("o/n"), "repos/o/n")

    def test_the_only_pull_request_label_is_created_idempotently(self):
        self.assertEqual(list(github.LABEL_STYLES), ["factory:ready"])
        with patch("factory.github.command") as run:
            ensure_labels(Path("/r"), "o/n", ["factory:ready"])
        self.assertEqual(
            [c.args[0] for c in run.call_args_list],
            [["gh", "label", "create", "factory:ready", "--force", "--color", "0e8a16", "--description", "Factory: review and verification passed", "-R", "o/n"]],
        )
        with patch("factory.github.command") as run:
            ensure_labels(Path("/r"), None, ["factory:ready"])
        self.assertNotIn("-R", run.call_args.args[0])
        with self.assertRaises(KeyError):
            ensure_labels(Path("/r"), None, ["factory:run/r1"])

    def test_the_dropped_pull_request_labels_are_gone(self):
        for name in ("LABEL_FACTORY", "LABEL_INCOMPLETE", "run_label", "RUN_LABEL_STYLE", "LABEL_LIMIT"):
            self.assertFalse(hasattr(github, name), name)

    def test_default_branch(self):
        with patch("factory.github.command", return_value="trunk") as run:
            self.assertEqual(default_branch(Path("/r"), "o/n"), "trunk")
        self.assertIn("o/n", run.call_args.args[0])
        with patch("factory.github.command", return_value=""), self.assertRaises(ValueError):
            default_branch(Path("/r"))

    def test_module_has_no_merge_or_close_operations(self):
        source = Path(github.__file__).read_text()
        for forbidden in ('"merge"', '"close"', "--auto", "--squash", "enable_pr_auto_merge"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()

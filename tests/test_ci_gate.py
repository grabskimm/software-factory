"""The pending ``factory/gate`` and its resolution through ``cli.reconcile_command`` against a fake ``gh``.

Uses the fake repository of ``test_reconcile``: ``world.data["checks"][<pull number>]`` is what
``gh pr checks --required`` prints, ``world.data["created"][<sha>]`` the time of the newest gate
status, and every status Factory posts is a ``status`` entry of ``writes``.
"""

import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from factory import checks_ran, ci_gate, cli
from factory.issue_labels import IssueLabels
from factory.locks import LockLost
from factory.model import outstanding_criteria
from factory.trigger import GithubReader

from tests.test_reconcile import HEAD, MOVED, ReconcileCase, plan_for

CI = {"text": "ci proves it", "tag": "ci", "checks": ["build"]}
STATUS_ROUTE = "repos/org/name/statuses/"
PULL_7_ROUTE = "repos/org/name/pulls/7"
THIRD = "c" * 40


def stamp(delta):
    return (datetime.now(timezone.utc) - delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def check(name, bucket):
    return {"name": name, "bucket": bucket, "state": bucket.upper()}


def ci_plan():
    plan = plan_for(("fix", [1]))
    plan["jobs"][0]["acceptance"] = ["works", CI]
    return plan


CHECKLIST = (
    "## Needs a person\n\nFactory proved none of them itself.\n\n"
    "- [x] try it by hand <!-- factory:operator-item:op-0123456789 -->\n"
)


def published_body(awaited=("build",)):
    """A body as ``Runner.publish`` wrote it: the CI block awaiting, a person's tick at the end."""
    return "\n".join(
        [
            "Closes #1",
            "",
            "## Verification",
            "",
            checks_ran.HEADING,
            "",
            "- **Sandbox** (a worker session): complete.",
            checks_ran.ci_awaiting(list(awaited), True),
            "- **Operator** (a person): none.",
            "",
            "```json",
            "{}",
            "```",
            "",
            CHECKLIST,
        ]
    )


class PendingGateCase(ReconcileCase):
    def setUp(self):
        super().setUp()
        self.world.pull(7, head=HEAD, body=published_body())
        self.world.data["issues"] = {"1": ["factory:pr-open"]}
        self.directory = self.run_dir("r1", ci_plan(), {"fix": ("pr_ready", 7, HEAD)})
        self.await_gate()

    def reconcile(self, directory, lock=None):
        lines = super().reconcile(directory, lock)
        self.world.data = self.world.load()  # what the "process" wrote is what the next one starts from
        return lines

    def await_gate(self, sha=HEAD, description=None, age=timedelta(minutes=1), state="pending", creator=None):
        self.seeded = getattr(self, "seeded", 0) + 1
        description = description or ci_gate.awaiting_description(["build"], sha)
        entry = ["status", sha, state, "factory/gate", description]
        if creator is not None:
            entry.append(creator)  # default: Factory's own bot user
        self.world.data["writes"].append(entry)
        self.world.data.setdefault("created", {})[sha] = stamp(age)

    def checks(self, *checks):
        self.world.data["checks"] = {"7": list(checks)}

    def posted(self):
        return [w[1:5] for w in self.world.writes("status")][self.seeded :]  # the seeded statuses excluded

    def reconcile_again(self):
        return self.reconcile(self.directory)


class StatusRecordTests(unittest.TestCase):
    def reader(self, output):
        reader = GithubReader("org/name", None, ".")
        reader._gh = lambda args: output
        return reader

    PAGE_WITHOUT = '{"state":null,"description":null,"created":null,"ctype":null,"cid":null,"clogin":null}'
    PAGE_WITH = (
        '{"state":"pending","description":"awaiting required CI: build @aaaaaaa","created":"2026-01-01T00:00:00Z",'
        '"ctype":"Bot","cid":4242,"clogin":"factory-bot"}'
    )

    def test_the_gate_status_is_found_on_a_later_page(self):
        # More than a page of other contexts' statuses sit above it: --paginate runs the filter per page.
        record = self.reader(self.PAGE_WITHOUT + "\n" + self.PAGE_WITH).status_record(HEAD, "factory/gate")
        self.assertEqual(record[0], "pending")
        self.assertEqual(record[3], {"type": "Bot", "id": 4242, "login": "factory-bot"})

    def test_the_newest_page_with_the_context_wins(self):
        newer = self.PAGE_WITH.replace("pending", "failure").replace("awaiting required CI: build", "required CI failed: build")
        record = self.reader(newer + "\n" + self.PAGE_WITH).status_record(HEAD, "factory/gate")
        self.assertEqual(record[0], "failure")

    def test_no_status_of_the_context_at_all(self):
        record = self.reader(self.PAGE_WITHOUT + "\n" + self.PAGE_WITHOUT).status_record(HEAD, "factory/gate")
        self.assertEqual(record[:3], (None, None, None))

    def test_an_unexpected_answer_raises(self):
        for output in ("[]", "", "7"):
            with self.subTest(output=output), self.assertRaises(ValueError):
                self.reader(output).status_record(HEAD, "factory/gate")


class ResolveTests(PendingGateCase):
    def test_all_required_checks_green_turn_pending_into_success_on_the_same_sha(self):
        # Factory's own gate is a required check of the ruleset: it must not hold itself back.
        self.checks(check("build", "pass"), check("lint", "skipping"), check("factory/gate", "pending"))
        self.assertEqual(self.reconcile(self.directory), [])
        [[sha, state, context, description]] = self.posted()
        self.assertEqual((sha, state, context), (HEAD, "success", "factory/gate"))
        self.assertIn(HEAD[:7], description)
        self.assertNotIn("[", description)  # a success carries no reason code
        self.assertEqual(self.world.writes("label-add"), [])

    def test_a_red_required_check_turns_pending_into_failure_and_labels_the_issue(self):
        self.checks(check("build", "fail"), check("lint", "pass"))
        self.assertEqual(self.reconcile(self.directory), [])
        [[sha, state, _, description]] = self.posted()
        self.assertEqual((sha, state), (HEAD, "failure"))
        self.assertIn("build", description)
        self.assertTrue(description.startswith(ci_gate.FAILED_CI))
        self.assertTrue(description.endswith(" [ci_failed]"))
        self.assertEqual(self.issue_labels(1), ["factory:ci-failed"])

    def test_a_red_check_wins_over_one_that_still_runs(self):
        self.checks(check("build", "pending"), check("lint", "fail"))
        self.reconcile(self.directory)
        self.assertEqual([p[1] for p in self.posted()], ["failure"])

    def test_running_checks_within_the_bound_post_nothing_new(self):
        self.checks(check("build", "pending"), check("lint", "pass"))
        self.assertEqual(self.reconcile(self.directory), [])
        self.assertEqual(self.posted(), [])
        self.assertEqual(self.issue_labels(1), ["factory:pr-open"])

    def test_running_checks_past_the_bound_time_out_as_a_failure(self):
        self.await_gate(age=timedelta(seconds=ci_gate.CI_WAIT_SECONDS + 600))
        self.checks(check("build", "pending"))
        self.reconcile(self.directory)
        [[sha, state, _, description]] = self.posted()
        self.assertEqual((sha, state), (HEAD, "failure"))
        self.assertIn("timed out waiting for CI", description)
        self.assertIn("build", description)
        self.assertTrue(description.endswith(" [ci_failed]"))
        self.assertLessEqual(len(description), 140)
        self.assertEqual(self.issue_labels(1), ["factory:pr-open"])  # a timeout is not a red check

    def test_green_wins_over_the_bound(self):
        self.await_gate(age=timedelta(days=2))
        self.checks(check("build", "pass"))
        self.reconcile(self.directory)
        self.assertEqual([p[1] for p in self.posted()], ["success"])

    def test_an_awaited_check_that_is_not_reported_is_not_green(self):
        self.checks(check("lint", "pass"))
        self.reconcile(self.directory)
        self.assertEqual(self.posted(), [])

    def test_no_required_checks_prove_nothing(self):
        self.checks()
        self.reconcile(self.directory)
        self.assertEqual(self.posted(), [])

    def test_a_moved_head_is_never_gated(self):
        self.world.pull(7, head=MOVED)
        self.checks(check("build", "pass"))
        self.reconcile(self.directory)
        self.assertEqual(self.posted(), [])
        self.assertEqual([w for w in self.world.writes("status") if w[1] == MOVED], [])
        self.assertEqual(self.world.writes("label-add", "label-remove"), [])
        # the same for a red check and for the bound running out
        self.await_gate(age=timedelta(days=2))
        self.checks(check("build", "fail"))
        self.reconcile_again()
        self.assertEqual(self.posted(), [])

    def test_a_closed_or_merged_pull_request_is_left_alone(self):
        self.checks(check("build", "pass"))
        self.world.pull(7, state="closed", head=HEAD)
        self.reconcile(self.directory)
        self.assertEqual(self.posted(), [])

    def test_only_an_awaiting_pending_gate_is_resolved(self):
        self.checks(check("build", "pass"))
        for state, description in (
            ("pending", f"publishing @{HEAD[:7]}"),
            ("pending", f"merged main@abcdef0; review and verification pending @{HEAD[:7]}"),
            ("failure", f"incomplete: review or verification did not pass @{HEAD[:7]}"),
            ("success", f"review+verify passed @{HEAD[:7]}"),
        ):
            with self.subTest(state=state, description=description):
                self.world.data["writes"] = [["status", HEAD, state, "factory/gate", description]]
                self.reconcile_again()
                self.assertEqual(self.world.writes("status"), [["status", HEAD, state, "factory/gate", description]])

    # -- trust: only Factory's own awaiting gate on a job it published as complete ------------

    FOREIGN = {"type": "User", "id": 9, "login": "mallory"}

    def test_an_awaiting_status_written_by_someone_else_is_never_promoted(self):
        self.await_gate(creator=self.FOREIGN)
        self.checks(check("build", "pass"))
        lines = self.reconcile(self.directory)
        self.assertEqual(self.posted(), [])
        self.assertIn(ci_gate.FOREIGN_LINE, "\n".join(lines) + "\n".join(self.world.load().get("stderr", [])))

    def test_the_app_identity_is_matched_by_bot_type_and_user_id(self):
        app = SimpleNamespace(app_user_id=4242)
        with patch("factory.ci_gate.get_auth", return_value=app):
            self.checks(check("build", "pass"))
            self.await_gate(creator={"type": "Bot", "id": 99, "login": "factory-bot"})  # another app, same name
            self.reconcile(self.directory)
            self.assertEqual(self.posted(), [])
            self.await_gate(creator={"type": "User", "id": 4242, "login": "factory-bot"})  # a person with that id
            self.reconcile_again()
            self.assertEqual(self.posted(), [])
            self.await_gate(creator={"type": "Bot", "id": 4242, "login": "factory-bot"})
            self.reconcile_again()
            self.assertEqual([p[1] for p in self.posted()], ["success"])

    def test_a_job_published_as_incomplete_is_never_promoted_even_with_a_genuine_awaiting_status(self):
        # A job whose review or verification failed was published as pr_incomplete with a failure
        # gate; an "awaiting required CI" pending written over it must not turn into success.
        incomplete = self.run_dir("r2", ci_plan(), {"fix": ("pr_incomplete", 7, HEAD)})
        self.checks(check("build", "pass"))
        self.reconcile(incomplete)
        self.assertEqual(self.posted(), [])

    def test_a_check_name_listed_twice_is_red_if_any_entry_is_red(self):
        for first, second in (("fail", "pass"), ("pass", "fail")):
            with self.subTest(order=(first, second)):
                self.assertEqual(
                    ci_gate.judge([check("build", first), check("build", second)], ["build"]), ("failure", ["build"])
                )
        self.assertEqual(ci_gate.judge([check("build", "pass"), check("build", "pending")], ["build"]), (None, []))
        self.assertEqual(ci_gate.judge([check("build", "pass"), check("build", "skipping")], ["build"]), ("success", ["build"]))
        self.checks(check("build", "fail"), check("build", "pass"))
        self.reconcile(self.directory)
        self.assertEqual([p[1] for p in self.posted()], ["failure"])

    # -- a failure posted for CI is not final --------------------------------------------------

    def fail_gate(self, description, age=timedelta(minutes=5)):
        self.world.data["writes"] = [["status", HEAD, "failure", "factory/gate", description]]
        self.world.data.setdefault("created", {})[HEAD] = stamp(age)
        self.world.data["issues"] = {"1": ["factory:ci-failed"]}
        self.seeded = 1

    def test_a_red_check_that_is_re_run_green_turns_the_failure_into_success_and_clears_the_label(self):
        self.fail_gate(ci_gate._description(ci_gate.FAILED_CI, ["build"], HEAD))
        self.checks(check("build", "pass"))
        self.reconcile(self.directory)
        [[sha, state, _, description]] = self.posted()
        self.assertEqual((sha, state), (HEAD, "success"))
        self.assertEqual(self.issue_labels(1), ["factory:pr-open"])

    def test_failures_that_carry_the_code_still_recover(self):
        for prefix in (ci_gate.FAILED_CI, ci_gate.TIMED_OUT):
            with self.subTest(prefix=prefix):
                self.fail_gate(ci_gate._description(prefix, ["build"], HEAD, "ci_failed"), age=timedelta(days=1))
                self.checks(check("build", "pass"))
                self.reconcile_again()
                self.assertEqual([p[1] for p in self.posted()][-1:], ["success"])

    def test_an_awaiting_gate_with_the_code_is_still_resolved(self):
        self.await_gate(description=ci_gate.awaiting_description(["build"], HEAD, "ci_pending"))
        self.assertTrue(ci_gate.is_awaiting(ci_gate.awaiting_description(["build"], HEAD, "ci_pending")))
        self.checks(check("build", "pass"))
        self.reconcile(self.directory)
        self.assertEqual([p[1] for p in self.posted()], ["success"])

    def test_a_timeout_failure_recovers_when_ci_finishes_green_later(self):
        self.fail_gate(ci_gate._description(ci_gate.TIMED_OUT, ["build"], HEAD), age=timedelta(days=1))
        self.checks(check("build", "pass"))
        self.reconcile(self.directory)
        self.assertEqual([p[1] for p in self.posted()], ["success"])

    def test_a_failure_that_is_still_red_or_still_running_posts_nothing_again(self):
        self.fail_gate(ci_gate._description(ci_gate.FAILED_CI, ["build"], HEAD))
        for bucket in ("fail", "pending"):
            self.checks(check("build", bucket))
            self.reconcile_again()
            self.assertEqual(self.posted(), [])
        self.assertEqual(self.issue_labels(1), ["factory:ci-failed"])

    def test_a_failure_about_review_or_verification_is_never_recovered(self):
        self.fail_gate(f"incomplete: review or verification did not pass @{HEAD[:7]}")
        self.checks(check("build", "pass"))
        self.reconcile(self.directory)
        self.assertEqual(self.posted(), [])

    def test_a_failure_written_by_someone_else_is_not_recovered(self):
        self.fail_gate(ci_gate._description(ci_gate.FAILED_CI, ["build"], HEAD))
        self.world.data["writes"][0].append(self.FOREIGN)
        self.checks(check("build", "pass"))
        self.reconcile(self.directory)
        self.assertEqual(self.posted(), [])

    # -- labels: written first, retried, and only for the run's own issues --------------------

    def test_a_label_that_did_not_land_keeps_the_gate_pending_and_is_retried(self):
        original = IssueLabels._apply
        failed = []

        def flaky(labels, number, label):
            if label == "factory:ci-failed" and not failed:
                failed.append(number)
                labels.missed[number] = label
                return False
            return original(labels, number, label)

        self.checks(check("build", "fail"))
        with patch.object(IssueLabels, "_apply", flaky):
            self.reconcile(self.directory)
            self.assertEqual(self.posted(), [])  # the label failed: no failure status yet
            self.reconcile_again()
        self.assertEqual([p[1] for p in self.posted()], ["failure"])
        self.assertEqual(self.issue_labels(1), ["factory:ci-failed"])

    def flaky_label(self, wanted_label, only_issue=None, times=1, always=False):
        """A patch of ``IssueLabels._apply`` whose write of ``wanted_label`` fails ``times`` times."""
        original = IssueLabels._apply
        state = {"left": times}

        def apply(labels, number, label):
            if label == wanted_label and (only_issue is None or number == only_issue) and (always or state["left"] > 0):
                state["left"] -= 1
                labels.missed[number] = label
                return False
            return original(labels, number, label)

        return patch.object(IssueLabels, "_apply", apply)

    def multi_issue_directory(self):
        plan = plan_for(("fix", [3, 4]))  # not 1: the run of setUp owns it, and a sibling's issue is skipped
        plan["jobs"][0]["acceptance"] = ["works", CI]
        self.world.data["issues"] = {"1": ["factory:pr-open"], "3": ["factory:pr-open"], "4": ["factory:pr-open"]}
        self.world.data["writes"] = [w for w in self.world.data["writes"] if w[0] != "status"]
        self.await_gate()
        self.seeded = 1
        return self.run_dir("multi", plan, {"fix": ("pr_ready", 7, HEAD)})

    def test_a_label_miss_on_the_lowest_issue_is_not_forgotten_when_a_later_issue_succeeds(self):
        # IssueLabels forgets a missed write when a later write succeeds; one sync over [1, 2]
        # could therefore post the failure with issue 1 still factory:pr-open.
        directory = self.multi_issue_directory()
        self.checks(check("build", "fail"))
        with self.flaky_label("factory:ci-failed", only_issue=3):
            self.reconcile(directory)
            self.assertEqual(self.posted(), [])
            self.reconcile(directory)
        self.assertEqual([p[1] for p in self.posted()], ["failure"])
        self.assertEqual((self.issue_labels(3), self.issue_labels(4)), (["factory:ci-failed"], ["factory:ci-failed"]))

    def test_recovery_posts_success_only_once_the_pr_open_label_is_back(self):
        self.fail_gate(ci_gate._description(ci_gate.FAILED_CI, ["build"], HEAD))
        self.checks(check("build", "pass"))
        with self.flaky_label("factory:pr-open"):
            self.reconcile(self.directory)
            self.assertEqual(self.posted(), [])  # the gate is a failure already: waiting costs nothing
            self.reconcile_again()
        self.assertEqual([p[1] for p in self.posted()], ["success"])
        self.assertEqual(self.issue_labels(1), ["factory:pr-open"])

    def test_a_green_gate_after_a_failure_whose_post_did_not_land_clears_factory_ci_failed(self):
        # Tick 1 labelled the issue factory:ci-failed and then the failure post failed: the gate is
        # still the awaiting pending one. CI is re-run green before tick 2.
        self.world.data["issues"] = {"1": ["factory:ci-failed"]}
        self.checks(check("build", "pass"))
        self.reconcile(self.directory)
        self.assertEqual([p[1] for p in self.posted()], ["success"])
        self.assertEqual(self.issue_labels(1), ["factory:pr-open"])

    def test_a_label_that_can_never_land_does_not_leave_a_red_pr_pending_for_ever(self):
        self.checks(check("build", "fail"))
        with self.flaky_label("factory:ci-failed", always=True):
            lines = self.reconcile(self.directory)  # within the bound: held back, and said so
            self.assertEqual(self.posted(), [])
            self.await_gate(age=timedelta(seconds=ci_gate.CI_WAIT_SECONDS + 600))
            self.reconcile_again()  # past the bound: the failure is posted without its label
        self.assertEqual([p[1] for p in self.posted()], ["failure"])
        self.assertEqual(self.issue_labels(1), ["factory:pr-open"])

    def test_a_held_back_failure_says_so_in_one_fixed_line(self):
        self.checks(check("build", "fail"))
        with self.flaky_label("factory:ci-failed", always=True):
            lines = self.reconcile(self.directory)
        self.assertIn(ci_gate.HELD_LINE, "\n".join(lines))

    def test_a_sibling_runs_issue_is_not_relabelled_but_the_gate_is_still_failed(self):
        self.run_dir("sibling", plan_for(("other", [1])), {"other": ("running", None, None)})
        self.world.data["issues"] = {"1": ["factory:building"]}
        self.checks(check("build", "fail"))
        self.reconcile(self.directory)
        self.assertEqual([p[1] for p in self.posted()], ["failure"])
        self.assertEqual(self.issue_labels(1), ["factory:building"])

    def test_a_job_without_ci_criteria_or_without_a_commit_is_not_looked_at(self):
        plain = self.run_dir("r2", plan_for(("fix", [1])), {"fix": ("pr_ready", 7, HEAD)})
        self.checks(check("build", "pass"))
        self.world.data["calls"] = []
        self.reconcile(plain)
        self.assertEqual([c for c in self.world.load()["calls"] if "/commits/" in " ".join(c)], [])
        nocommit = self.run_dir("r3", ci_plan(), {"fix": ("pr_ready", 7, None)})
        self.world.data["calls"] = []
        self.reconcile(nocommit)
        self.assertEqual([c for c in self.world.load()["calls"] if "/commits/" in " ".join(c)], [])
        self.assertEqual(self.posted(), [])

    def test_failures_are_one_fixed_line_and_post_nothing(self):
        self.world.data["fail"] = True
        lines = self.reconcile(self.directory)
        self.assertTrue(lines)
        self.assertTrue(all(line.startswith("factory: ") and len(line) < 100 for line in lines))
        self.assertEqual(self.posted(), [])


class EvidenceTests(PendingGateCase):
    """Resolving the gate also reports, in the pull request body, which required checks ran on the head."""

    def body_before(self):
        return self.world.data["pulls"]["7"]["body"]

    def written(self):
        return [write[2] for write in self.world.writes("body")]

    def block_of(self, body):
        start = body.index(checks_ran.CI_BEGIN)
        return body[start : body.index(checks_ran.CI_END) + len(checks_ran.CI_END)]

    def test_a_green_gate_replaces_only_the_ci_block_with_the_head_and_each_check(self):
        before = self.body_before()
        self.checks(check("build", "pass"), check("lint", "skipping"), check("factory/gate", "pending"))
        self.reconcile(self.directory)
        [after] = self.written()
        block = self.block_of(after)
        self.assertIn(f"on head `{HEAD}`", block)
        self.assertIn("every required check passed", block)
        self.assertIn("  - `build`: pass", block)
        self.assertIn("  - `lint`: skipping", block)
        self.assertNotIn("factory/gate`: ", block)  # Factory's own gate is not evidence
        self.assertNotIn("awaiting on this head", block)
        # every other byte is unchanged: the head of the body, its tail, the person's tick included
        start, stop = before.index(checks_ran.CI_BEGIN), before.index(checks_ran.CI_END) + len(checks_ran.CI_END)
        self.assertEqual(after, f"{before[:start]}{block}{before[stop:]}")
        self.assertTrue(after.endswith(CHECKLIST))
        self.assertEqual(self.world.load()["pulls"]["7"]["body"], after)

    def test_a_red_check_is_reported_with_its_conclusion(self):
        self.checks(check("build", "fail"), check("lint", "pass"))
        self.reconcile(self.directory)
        [after] = self.written()
        block = self.block_of(after)
        self.assertIn("a required check failed", block)
        self.assertIn("  - `build`: fail", block)
        self.assertIn("  - `lint`: pass", block)
        self.assertIn("awaited by the gate: `build`", block)

    def test_the_timeout_is_reported_with_what_had_reported(self):
        self.await_gate(age=timedelta(seconds=ci_gate.CI_WAIT_SECONDS + 600))
        self.checks(check("build", "pending"))
        self.reconcile(self.directory)
        [after] = self.written()
        block = self.block_of(after)
        self.assertIn("timed out waiting for the required checks", block)
        self.assertIn("  - `build`: pending", block)

    def test_a_check_that_has_not_reported_is_said_so(self):
        self.await_gate(age=timedelta(seconds=ci_gate.CI_WAIT_SECONDS + 600))
        self.checks()
        self.reconcile(self.directory)
        [after] = self.written()
        self.assertIn("no required check had reported on this head", self.block_of(after))

    def test_a_recovered_failure_rewrites_the_same_block_in_place(self):
        self.checks(check("build", "fail"))
        self.reconcile(self.directory)
        self.assertIn("a required check failed", self.written()[-1])
        # the failure Factory posted stands until the same head's checks all pass: the re-run is green
        self.checks(check("build", "pass"))
        self.reconcile_again()
        bodies = self.written()
        self.assertEqual(len(bodies), 2)
        self.assertEqual(bodies[-1].count(checks_ran.CI_BEGIN), 1)
        self.assertEqual(bodies[-1].count(checks_ran.CI_END), 1)
        self.assertIn("every required check passed", bodies[-1])
        self.assertNotIn("a required check failed", bodies[-1])
        self.assertTrue(bodies[-1].endswith(CHECKLIST))
        self.assertEqual([w[2] for w in self.world.writes("status")][-1], "success")

    def test_an_identical_block_is_not_written_again(self):
        self.checks(check("build", "pass"))
        self.reconcile(self.directory)
        self.assertEqual(len(self.written()), 1)
        # the same result again (a second pass that resolves the same way) writes nothing
        self.world.data["writes"] = [w for w in self.world.data["writes"] if w[0] != "status"]
        self.await_gate()
        self.seeded = 1
        self.reconcile_again()
        self.assertEqual(len(self.written()), 1)
        self.assertEqual([p[1] for p in self.posted()], ["success"])

    def test_an_unresolved_gate_writes_no_evidence(self):
        self.checks(check("build", "pending"))
        self.reconcile(self.directory)
        self.assertEqual(self.written(), [])

    def test_a_failed_body_write_changes_nothing_about_the_gate_or_the_labels(self):
        path = self.root / "bin" / "gh"
        path.write_text(path.read_text().replace("if verb == 'PATCH':", "if verb == 'PATCH':\n            fail('HTTP 403')"))
        self.checks(check("build", "fail"))
        lines = self.reconcile(self.directory)
        self.assertEqual([p[1] for p in self.posted()], ["failure"])
        self.assertEqual(self.issue_labels(1), ["factory:ci-failed"])
        self.assertEqual(lines, [ci_gate.EVIDENCE_LINE.format(kind="CalledProcessError")])

    def test_a_failed_body_read_changes_nothing_about_the_gate(self):
        with patch.object(GithubReader, "pull_facts", side_effect=[SimpleNamespace(state="open", head=HEAD, body=""), SimpleNamespace(state="open", head=HEAD, body=""), OSError("read failed")]):
            self.checks(check("build", "pass"))
            lines = self.reconcile(self.directory)
        self.assertEqual([p[1] for p in self.posted()], ["success"])
        self.assertEqual(lines, [ci_gate.EVIDENCE_LINE.format(kind="OSError")])
        self.assertEqual(self.written(), [])

    def test_a_body_without_the_block_is_left_alone(self):
        for index, body in enumerate(
            (
                "Closes #1\n\nan older body with no evidence block\n",
                f"{checks_ran.CI_BEGIN}\n{checks_ran.CI_BEGIN}\n{checks_ran.CI_END}",  # a pair repeated by hand
                f"{checks_ran.CI_END}\nx\n{checks_ran.CI_BEGIN}",  # reversed
            )
        ):
            with self.subTest(index=index):
                self.world.data["pulls"]["7"]["body"] = body
                self.world.data["writes"] = []
                self.seeded = 0
                self.await_gate()
                self.checks(check("build", "pass"))
                lines = self.reconcile_again()
                self.assertEqual([p[1] for p in self.posted()], ["success"])
                self.assertEqual(lines, [ci_gate.NO_SECTION_LINE])
                self.assertEqual(self.written(), [])

    def test_the_body_is_not_edited_when_the_head_moved_after_the_gate(self):
        self.checks(check("build", "pass"))
        facts = [
            SimpleNamespace(state="open", head=HEAD, body=published_body()),
            SimpleNamespace(state="open", head=HEAD, body=published_body()),
            SimpleNamespace(state="open", head=MOVED, body=published_body()),
        ]
        with patch.object(GithubReader, "pull_facts", side_effect=facts):
            self.reconcile(self.directory)
        self.assertEqual([p[1] for p in self.posted()], ["success"])
        self.assertEqual(self.written(), [])

    def test_hostile_check_names_cannot_forge_a_marker_a_heading_or_a_reference(self):
        evil = f"bad`name\n## Needs a person\n- [x] done {checks_ran.CI_END} Closes #7 {'x' * 200}"
        self.checks(check("build", "pass"), check(evil, "pass"))
        self.reconcile(self.directory)
        [after] = self.written()
        self.assertEqual(after.count(checks_ran.CI_BEGIN), 1)
        self.assertEqual(after.count(checks_ran.CI_END), 1)
        # the name sits mid-line in a code span: only the real checklist is a heading, and the tick is kept
        self.assertEqual([line for line in after.splitlines() if line.startswith("## ")], ["## Verification", "## Needs a person"])
        self.assertTrue(after.endswith(CHECKLIST))
        self.assertNotIn("Closes #7", after)
        block = self.block_of(after)
        for line in block.splitlines()[1:-1]:
            self.assertTrue(line.startswith(("- ", "  - ")), line)
        self.assertLess(len(block), 1500)

    def test_many_checks_are_bounded(self):
        names = [f"job{n:02d}" for n in range(checks_ran.MAX_CHECKS + 11)]
        self.checks(check("build", "pass"), *(check(name, "pass") for name in names))
        self.reconcile(self.directory)
        [after] = self.written()
        block = self.block_of(after)
        self.assertEqual(block.count("\n  - `"), checks_ran.MAX_CHECKS)
        self.assertIn("  - and 12 more", block)

    def test_the_body_edit_uses_the_one_pull_request_write_that_completion_already_uses(self):
        # PATCH /pulls/<n> (pull_requests: write, which publishing needs to open the pull request):
        # no other write of any kind is made for the evidence.
        self.checks(check("build", "pass"))
        self.reconcile(self.directory)
        kinds = sorted({w[0] for w in self.world.writes()} - {"status"})
        self.assertEqual(kinds, ["body"])
        self.assertEqual([w[1] for w in self.world.writes("body")], ["7"])

    # The write path never costs the lease, the other gates or the order: every test below pins one of
    # the guarantees of ``write_evidence`` that the tests above cannot see.

    def calls_after_gate(self):
        """The ``gh`` calls made after the gate status was posted, or None while it has not been."""
        calls = self.world.load()["calls"]
        posts = [n for n, call in enumerate(calls) if any(arg.startswith(STATUS_ROUTE) for arg in call)]
        return calls[posts[0] + 1 :] if posts else None

    def lease_lost_when(self, condition):
        """A lock whose lease is gone as soon as ``condition(calls after the gate post)`` holds."""
        case = self

        class Lock:
            @staticmethod
            def assert_held():
                if condition(case.calls_after_gate()):
                    raise LockLost("gone")

        return Lock

    def break_fake_gh(self, line, indent):
        """Make the fake ``gh`` fail as soon as it reaches ``line`` of its source (the body is ``indent`` deep)."""
        path = self.root / "bin" / "gh"
        source = path.read_text()
        self.assertIn(line, source)
        path.write_text(source.replace(line, f"{line}\n{indent}fail('HTTP 500')", 1))

    def test_a_lease_lost_once_the_gate_is_posted_reads_and_writes_nothing_more(self):
        self.checks(check("build", "pass"))
        lines = self.reconcile(self.directory, self.lease_lost_when(lambda after: after is not None))
        self.assertEqual([p[1] for p in self.posted()], ["success"])  # the gate stands
        self.assertEqual(lines, [])  # quiet: not an evidence failure, the lost lease is simply the end
        self.assertEqual(self.calls_after_gate(), [])  # not even the read of the body
        self.assertEqual(self.written(), [])

    def test_a_lease_lost_between_the_read_and_the_write_of_the_body_writes_nothing(self):
        def read_the_body(after):
            return after is not None and any(PULL_7_ROUTE in call and "-X" not in call for call in after)

        self.checks(check("build", "pass"))
        lines = self.reconcile(self.directory, self.lease_lost_when(read_the_body))
        self.assertEqual([p[1] for p in self.posted()], ["success"])
        self.assertEqual(lines, [])
        after = self.calls_after_gate()
        self.assertEqual(len(after), 1)  # the read of the body, and then nothing
        self.assertEqual([call for call in after if "-X" in call], [])
        self.assertEqual(self.written(), [])

    def test_a_gate_that_did_not_post_writes_no_evidence(self):
        self.break_fake_gh("elif m := re.fullmatch(r'/statuses/(.+)', rest):", " " * 8)
        self.checks(check("build", "pass"))
        lines = self.reconcile(self.directory)
        self.assertEqual(self.posted(), [])
        self.assertEqual(lines, [ci_gate.FAILED_LINE.format(kind="CalledProcessError")])
        self.assertEqual(self.written(), [])
        self.assertEqual([call for call in self.world.load()["calls"] if "-X" in call and "PATCH" in call], [])

    def test_a_pull_request_that_is_no_longer_open_on_the_head_is_not_edited(self):
        for state in ("closed", "merged"):
            with self.subTest(state=state):
                self.world.data["writes"] = []
                self.seeded = 0
                self.await_gate()
                self.checks(check("build", "pass"))
                facts = [SimpleNamespace(state="open", head=HEAD, body=published_body())] * 2
                facts.append(SimpleNamespace(state=state, head=HEAD, body=published_body()))
                with patch.object(GithubReader, "pull_facts", side_effect=facts):
                    lines = self.reconcile_again()
                self.assertEqual([p[1] for p in self.posted()], ["success"])
                self.assertEqual(lines, [])
                self.assertEqual(self.written(), [])

    def three_jobs(self):
        """Three published jobs, one pull request each, every gate awaiting: ``(directory, {number: head})``."""
        heads = {7: HEAD, 8: MOVED, 9: THIRD}
        plan = plan_for(("a", [2]), ("b", [3]), ("c", [4]))
        for job in plan["jobs"]:
            job["acceptance"] = ["works", CI]
        self.world.data["issues"].update({number: ["factory:pr-open"] for number in ("2", "3", "4")})
        for number in (8, 9):
            self.world.pull(number, head=heads[number], body=published_body())
            self.await_gate(sha=heads[number])
        self.world.data["checks"] = {str(number): [check("build", "pass")] for number in heads}
        states = {key: ("pr_ready", number, heads[number]) for key, number in zip("abc", heads)}
        return self.run_dir("three", plan, states), heads

    def assert_all_three_gates_posted(self, heads):
        self.assertEqual([p[:2] for p in self.posted()], [[head, "success"] for head in heads.values()])

    def test_failed_body_writes_never_delay_the_gates_of_the_other_jobs(self):
        directory, heads = self.three_jobs()
        self.break_fake_gh("if verb == 'PATCH':", " " * 12)  # every body write fails
        lines = self.reconcile(directory)
        self.assert_all_three_gates_posted(heads)
        self.assertEqual(lines, [ci_gate.EVIDENCE_LINE.format(kind="CalledProcessError")] * 3)
        self.assertEqual(self.written(), [])

    def test_failed_body_reads_never_delay_the_gates_of_the_other_jobs(self):
        directory, heads = self.three_jobs()
        reads = {}

        def facts(number):
            reads[number] = reads.get(number, 0) + 1
            if reads[number] == 3:  # the third read of a pull request is the evidence's
                raise OSError("read failed")
            return SimpleNamespace(state="open", head=heads[number], body=published_body())

        with patch.object(GithubReader, "pull_facts", side_effect=facts):
            lines = self.reconcile(directory)
        self.assert_all_three_gates_posted(heads)
        self.assertEqual(lines, [ci_gate.EVIDENCE_LINE.format(kind="OSError")] * 3)
        self.assertEqual(self.written(), [])


class RestartTests(PendingGateCase):
    def snapshot(self):
        return {p.name: p.read_bytes() for p in sorted(self.directory.rglob("*")) if p.is_file()}, sorted(
            p.name for p in self.directory.rglob("*")
        )

    def test_reconcile_is_a_command_that_writes_nothing_into_the_run_directory(self):
        self.assertIn("reconcile", cli.NO_RUN_DIR_WRITES)

    def test_a_new_process_resolves_from_github_and_the_run_directory_alone(self):
        before = self.snapshot()
        self.checks(check("build", "pending"))
        self.reconcile(self.directory)  # first process: still running
        self.assertEqual(self.posted(), [])
        self.checks(check("build", "pass"))
        self.reconcile_again()  # a new process: nothing but GitHub and the run directory carries over
        self.assertEqual([p[1] for p in self.posted()], ["success"])
        self.reconcile_again()  # and once more: resolved, so nothing is posted twice
        self.reconcile_again()
        self.assertEqual([p[1] for p in self.posted()], ["success"])
        self.assertEqual(self.snapshot(), before)

    def test_a_red_result_is_not_repeated_either(self):
        self.checks(check("build", "fail"))
        for _ in range(3):
            self.reconcile_again()
        self.assertEqual([p[1] for p in self.posted()], ["failure"])
        self.assertEqual(len(self.world.writes("label-add")), 1)

    def test_the_bound_survives_a_restart_because_it_runs_from_the_status_time(self):
        self.checks(check("build", "pending"))
        self.reconcile(self.directory)
        self.assertEqual(self.posted(), [])
        self.world.data["created"][HEAD] = stamp(timedelta(seconds=ci_gate.CI_WAIT_SECONDS + 5))
        self.reconcile(self.directory)
        self.assertEqual([p[1] for p in self.posted()], ["failure"])
        self.assertEqual(self.snapshot()[1], ["plan.json", "state.json"])


class UnitTests(PendingGateCase):
    def test_a_lost_lock_stops_the_step_quietly(self):
        class Lost:
            @staticmethod
            def assert_held():
                from factory.locks import LockLost

                raise LockLost("gone")

        self.checks(check("build", "pass"))
        self.assertEqual(self.reconcile(self.directory, Lost), [])
        self.assertEqual(self.posted(), [])

    def test_awaited_checks_are_only_those_covered_by_required_checks(self):
        job = ci_plan()["jobs"][0]
        self.assertEqual(ci_gate.awaited_checks(job, {"build", "x"}), ["build"])
        # nothing required: the ci criterion is unproven (outstanding), and nothing is awaited
        self.assertEqual(ci_gate.awaited_checks(job, set()), [])
        self.assertEqual([c["tag"] for c in outstanding_criteria(job, set())], ["ci"])
        self.assertEqual(outstanding_criteria(job, {"build"}), [])

    def test_the_description_names_the_checks_and_fits_the_status_limit(self):
        text = ci_gate.awaiting_description(["a" * 100, "b" * 100], HEAD)
        self.assertLessEqual(len(text), 140)
        self.assertTrue(text.startswith(ci_gate.AWAITING))
        self.assertTrue(text.endswith(f" @{HEAD[:7]}"))
        self.assertIn("build, lint", ci_gate.awaiting_description(["build", "lint"], HEAD))

    def test_the_reader_returns_the_status_time(self):
        reader = GithubReader("org/name", None, self.root)
        self.world.save()
        self.assertEqual(reader.status_detail(HEAD, "factory/gate")[:2], ("pending", ci_gate.awaiting_description(["build"], HEAD)))
        self.assertIsNotNone(reader.status_detail(HEAD, "factory/gate")[2])
        self.assertEqual(reader.status_of(HEAD, "factory/gate")[0], "pending")
        self.assertEqual(reader.status_detail(MOVED, "factory/gate"), (None, None, None))

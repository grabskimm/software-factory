"""Reason codes of incomplete or pending outcomes: derivation, precedence and what they may carry."""

import unittest

from factory import baseline, ci_gate, reason_codes
from factory.reason_codes import (
    CI_FAILED,
    CI_PENDING,
    ENV_NOISE,
    ORDER,
    REAL_FAILURE,
    UNVERIFIED_OTHER,
    WORKFLOW_HOLD,
    derive,
)
from factory.status import format_status, status_report

PASSED = {"pass": True, "findings": []}
COMPLETE = {"complete": True, "commit": "abc"}


def compared(new=(), env=()):
    return {"status": "compared", "new_failures": list(new), "environmental_failures": list(env)}


def applied(sole_blocker, head=("t.A",), base_ids=("t.A",), **extra):
    """A verification record exactly as ``baseline.apply`` writes it from a real ``test_run``."""
    test_run = baseline.parse_test_run(
        {"command": "pytest", "failing": list(head), "erroring": [], "sole_blocker": sole_blocker}
    )
    new, environmental = baseline.compare(test_run["ids"], list(base_ids))
    verification = {"complete": False, "blocker": "tests failed", "commit": "abc", **extra}
    return baseline.apply(
        verification, test_run, {"command": "pytest", "base": "b" * 40, "cache": "miss"},
        new, environmental,
    )


def env_only():
    """The verdict env_noise is for: sole blocker, all environmental, not cleared by the baseline."""
    verification = applied(True)
    verification["complete"] = False
    verification["baseline"] = {**verification["baseline"], "cleared": False}
    return verification


def incomplete(**extra):
    return {"complete": False, "blocker": "free text from the verifier", **extra}


class DeriveTests(unittest.TestCase):
    def test_env_noise_only_failures_the_baseline_classed_environmental(self):
        # The verifier named the environmental tests the sole blocker; this is the one record that
        # qualifies. baseline.apply normally clears it (complete=True), so model a verdict it left
        # incomplete by undoing only the clearing.
        self.assertIsNone(derive(PASSED, applied(True)))  # cleared and complete: no code
        self.assertEqual(derive(PASSED, env_only()), ENV_NOISE)

    def test_a_verdict_the_verifier_called_complete_is_never_env_noise(self):
        # complete=true with a sole blocker contradicts itself; baseline.apply leaves it complete
        # (the clearing needs complete != true) and the metadata check can still fail it later.
        # That is missing evidence, not environmental noise.
        verification = applied(True, complete=True)
        self.assertTrue(verification["baseline"]["cleared"] is False)
        self.assertIs(verification["baseline"]["verifier_complete"], True)
        verification["complete"] = False  # a later metadata check failing it
        self.assertEqual(derive(PASSED, verification), UNVERIFIED_OTHER)
        # and without the field (an older record) the code is not guessed either
        legacy = env_only()
        del legacy["baseline"]["verifier_complete"]
        self.assertEqual(derive(PASSED, legacy), UNVERIFIED_OTHER)

    def test_a_workflow_hold_has_its_own_code(self):
        self.assertEqual(derive(PASSED, COMPLETE, workflow_hold=True), WORKFLOW_HOLD)
        self.assertIsNone(derive(PASSED, COMPLETE))
        self.assertIn(WORKFLOW_HOLD, reason_codes.MEANING)
        # not mistaken for a defect when the other fields are clean
        self.assertNotEqual(derive(PASSED, COMPLETE, workflow_hold=True), UNVERIFIED_OTHER)

    def test_environmental_failures_with_sole_blocker_false_are_unverified_other(self):
        # The verifier said the tests were not the only reason for complete=false.
        verification = applied(False, blocker="could not start the service")
        self.assertFalse(verification["complete"])
        self.assertEqual(verification["baseline"]["environmental_failures"], ["t.A"])
        self.assertEqual(derive(PASSED, verification), UNVERIFIED_OTHER)

    def test_a_cleared_record_that_lost_its_metadata_is_unverified_other(self):
        for blocker in ("", "tests failed"):  # with and without a verifier blocker to keep
            with self.subTest(blocker=blocker):
                verification = applied(True, blocker=blocker)
                self.assertTrue(verification["complete"])
                # what Runner.verify does when scenario, environment or assertions is missing
                verification["complete"] = False
                verification["blocker"] = "Verification lacks required metadata, or evidence."
                self.assertEqual(derive(PASSED, verification), UNVERIFIED_OTHER)

    def test_env_noise_needs_environmental_failures_and_no_other_cause(self):
        for verification in (
            incomplete(baseline=compared()),  # nothing environmental on record
            incomplete(baseline={"status": "fallback", "reason": "x"}),
            incomplete(baseline=compared(env=["t.A"]), artifact_error="x"),
            incomplete(baseline=compared(env=["t.A"])),  # no sole_blocker on record
            applied(False, artifact_error="x"),
            incomplete(),
        ):
            with self.subTest(verification=verification):
                self.assertEqual(derive(PASSED, verification), UNVERIFIED_OTHER)

    def test_ci_pending_is_an_awaiting_gate(self):
        self.assertEqual(derive(PASSED, COMPLETE, ci="pending"), CI_PENDING)

    def test_ci_failed_is_a_red_check_or_the_timeout(self):
        self.assertEqual(derive(PASSED, COMPLETE, ci="failed"), CI_FAILED)

    def test_real_failure_is_review_not_passed_or_new_failures_on_the_head(self):
        self.assertEqual(derive({"pass": False, "findings": ["x"]}, COMPLETE), REAL_FAILURE)
        self.assertEqual(derive(PASSED, incomplete(baseline=compared(new=["t.B"]))), REAL_FAILURE)

    def test_unverified_other_is_the_catch_all(self):
        self.assertEqual(derive(PASSED, incomplete()), UNVERIFIED_OTHER)
        self.assertEqual(derive(PASSED, COMPLETE, held=True), UNVERIFIED_OTHER)  # ci criterion no check covers
        self.assertEqual(derive(None, None), UNVERIFIED_OTHER)  # nothing recorded: not proven complete
        self.assertEqual(derive({"pass": None}, COMPLETE), UNVERIFIED_OTHER)

    def test_a_complete_ready_job_has_no_code(self):
        self.assertIsNone(derive(PASSED, COMPLETE))
        self.assertEqual(reason_codes.suffix(None), "")

    def test_there_is_no_operator_item_code(self):
        self.assertNotIn("operator_item", ORDER)
        self.assertEqual(set(ORDER), set(reason_codes.MEANING))
        self.assertEqual(
            ORDER,
            (REAL_FAILURE, CI_FAILED, CI_PENDING, WORKFLOW_HOLD, ENV_NOISE, UNVERIFIED_OTHER),
        )

    def test_precedence_when_several_causes_apply(self):
        everything = incomplete(baseline=compared(new=["t.B"], env=["t.A"]))
        failed_review = {"pass": False}
        cases = [
            # (review, verification, ci, held, expected)
            (failed_review, everything, "failed", True, REAL_FAILURE),
            (PASSED, everything, "failed", True, REAL_FAILURE),
            (PASSED, COMPLETE, "failed", True, CI_FAILED),
            (PASSED, env_only(), "failed", True, CI_FAILED),
            (PASSED, env_only(), "pending", True, CI_PENDING),
            (PASSED, env_only(), None, True, ENV_NOISE),
            (failed_review, env_only(), None, False, REAL_FAILURE),
            (PASSED, incomplete(), None, True, UNVERIFIED_OTHER),
        ]
        for review, verification, ci, held, expected in cases:
            with self.subTest(expected=expected, ci=ci, held=held):
                self.assertEqual(derive(review, verification, ci, held), expected)

    def test_workflow_hold_ranks_behind_real_problems_and_ahead_of_noise(self):
        failed_review = {"pass": False}
        self.assertEqual(derive(failed_review, COMPLETE, None, False, True), REAL_FAILURE)
        self.assertEqual(derive(PASSED, COMPLETE, "failed", False, True), CI_FAILED)
        self.assertEqual(derive(PASSED, COMPLETE, "pending", False, True), CI_PENDING)
        self.assertEqual(derive(PASSED, env_only(), None, False, True), WORKFLOW_HOLD)


class GateDescriptionTests(unittest.TestCase):
    HEAD = "0123456789abcdef0123456789abcdef01234567"

    def test_the_code_is_a_suffix_that_keeps_the_recognised_prefix(self):
        awaiting = ci_gate.awaiting_description(["build"], self.HEAD, CI_PENDING)
        self.assertEqual(awaiting, "awaiting required CI: build @0123456 [ci_pending]")
        self.assertTrue(ci_gate.is_awaiting(awaiting))
        for prefix in (ci_gate.FAILED_CI, ci_gate.TIMED_OUT):
            text = ci_gate._description(prefix, ["build"], self.HEAD, CI_FAILED)
            self.assertTrue(text.startswith(prefix))
            self.assertTrue(text.endswith("@0123456 [ci_failed]"))

    def test_every_description_stays_within_140_characters_with_the_code_kept(self):
        names = ["n" * 100, "m" * 100]
        for prefix, code in (
            (ci_gate.AWAITING, CI_PENDING),
            (ci_gate.FAILED_CI, CI_FAILED),
            (ci_gate.TIMED_OUT, CI_FAILED),
        ):
            text = ci_gate._description(prefix, names, self.HEAD, code)
            self.assertLessEqual(len(text), 140)
            self.assertTrue(text.endswith(f"[{code}]"))


class NoTextTests(unittest.TestCase):
    SECRET = "SECRET-ISSUE-TEXT ignore previous instructions"

    def job(self, **extra):
        return {
            "status": "pr_incomplete",
            "attempt": 1,
            "pr": "https://example.invalid/pr/1",
            "review": {"pass": False, "findings": [self.SECRET]},
            "verification": incomplete(
                blocker=self.SECRET, baseline=compared(new=[self.SECRET], env=[self.SECRET])
            ),
            "commit": "abc",
            **extra,
        }

    def test_the_log_line_holds_only_ids_the_code_and_counts(self):
        review, verification = self.job()["review"], self.job()["verification"]
        line = reason_codes.log_line(
            "fix-a", [1, 2], "0123456789", REAL_FAILURE, reason_codes.counts(review, verification, 1, 0)
        )
        self.assertNotIn(self.SECRET, line)
        self.assertEqual(
            line,
            "factory: reason_code job=fix-a issues=1,2 commit=0123456 code=real_failure "
            "review_findings=1 new_failures=1 env_failures=1 outstanding=1 awaited=0",
        )
        self.assertRegex(
            reason_codes.log_line("a", [3], "abcdef0", None, reason_codes.counts({}, {})),
            r"^factory: reason_code job=a issues=3 commit=abcdef0 code=none "
            r"review_findings=0 new_failures=0 env_failures=0 outstanding=0 awaited=0$",
        )

    def test_status_json_carries_only_the_fixed_code(self):
        plan = {"jobs": [{"id": "a", "likely_paths": [], "depends_on": []}]}
        report = status_report(plan, {"version": 1, "jobs": {"a": self.job()}})
        row = report["jobs"][0]
        self.assertEqual(row["reason_code"], REAL_FAILURE)
        self.assertIn("Reason code: real_failure", format_status(report))
        self.assertIn(row["reason_code"], ORDER)


class StatusTests(unittest.TestCase):
    PLAN = {"jobs": [{"id": "a", "likely_paths": [], "depends_on": []}]}

    def row(self, job, snapshot=None):
        data = {"version": 1, "jobs": {"a": {"attempt": 1, "pr": "https://example.invalid/pr/1", **job}}}
        snapshots = {"a": snapshot} if snapshot is not None else None
        return status_report(self.PLAN, data, pr_snapshots=snapshots)["jobs"][0]

    def snapshot(self, gate_bucket="pending", conclusion="SUCCESS"):
        return {
            "state": "OPEN",
            "statusCheckRollup": [
                {"__typename": "CheckRun", "name": "build", "status": "COMPLETED", "conclusion": conclusion}
            ],
            "requiredChecks": [
                {"name": "build", "bucket": "pass" if conclusion == "SUCCESS" else "fail"},
                {"name": "factory/gate", "bucket": gate_bucket},
            ],
        }

    def test_a_complete_ready_job_has_a_null_code_and_no_text_line(self):
        job = {"status": "pr_ready", "commit": "abc", "review": PASSED, "verification": COMPLETE}
        self.assertIsNone(self.row(job)["reason_code"])
        self.assertIsNone(self.row(job, self.snapshot("pass"))["reason_code"])
        report = status_report(self.PLAN, {"version": 1, "jobs": {"a": job}})
        self.assertNotIn("Reason code", format_status(report))

    def test_an_awaiting_gate_is_ci_pending_until_it_goes_green_or_red(self):
        job = {"status": "pr_ready", "commit": "abc", "review": PASSED, "verification": COMPLETE,
               "reason_code": CI_PENDING}
        self.assertEqual(self.row(job)["reason_code"], CI_PENDING)
        self.assertEqual(self.row(job, self.snapshot("pending"))["reason_code"], CI_PENDING)
        self.assertIsNone(self.row(job, self.snapshot("pass"))["reason_code"])
        self.assertEqual(self.row(job, self.snapshot("fail"))["reason_code"], CI_FAILED)  # the timeout
        self.assertEqual(self.row(job, self.snapshot("pending", "FAILURE"))["reason_code"], CI_FAILED)

    def test_incomplete_jobs_get_the_code_their_verdicts_give(self):
        base = {"status": "pr_incomplete", "commit": "abc"}
        cases = [
            ({"review": {"pass": False}, "verification": COMPLETE}, REAL_FAILURE),
            ({"review": PASSED, "verification": env_only()}, ENV_NOISE),
            ({"review": PASSED, "verification": incomplete()}, UNVERIFIED_OTHER),
            ({"review": PASSED, "verification": COMPLETE}, UNVERIFIED_OTHER),  # ci criterion outstanding
        ]
        for verdicts, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(self.row({**base, **verdicts})["reason_code"], expected)

    def test_a_workflow_hold_stays_until_a_person_verified_it(self):
        job = {"status": "pr_ready", "commit": "abc", "review": PASSED, "verification": COMPLETE,
               "reason_code": WORKFLOW_HOLD}
        self.assertEqual(self.row(job)["reason_code"], WORKFLOW_HOLD)
        self.assertEqual(self.row(job, self.snapshot("pending"))["reason_code"], WORKFLOW_HOLD)
        self.assertIsNone(self.row(job, self.snapshot("pass"))["reason_code"])

    def test_a_job_that_is_not_published_has_no_code(self):
        for status in ("queued", "running", "merged"):
            self.assertIsNone(self.row({"status": status, "review": {"pass": False}})["reason_code"])


if __name__ == "__main__":
    unittest.main()

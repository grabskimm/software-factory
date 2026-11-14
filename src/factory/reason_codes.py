"""Fixed reason codes for the incomplete or pending outcome of a published job.

A published job is held back (a draft PR, or a ``factory/gate`` that is not ``success``) for one of
a fixed set of reasons. Each outcome carries exactly one of these codes, worked out from data that
already exists (the review and verification verdicts in ``state.json``, the baseline record inside
the verification, and the gate outcome ``Runner.publish`` / ``ci_gate.resolve_pending`` write). No
GitHub call is made here, and no issue, criterion, PR or agent text is read into the code.

Codes:

* ``real_failure``: review did not pass, or verification found tests that fail on the head and
  not on the base (a real failure of a ``sandbox`` criterion).
* ``ci_failed``: a required check is red, or the CI wait timed out (``ci_gate`` failure).
* ``ci_pending``: the gate is an awaiting gate (``ci_gate.AWAITING``): required checks not done.
* ``workflow_hold``: review and verification passed and nothing is outstanding, but the head changes
  workflow files, so a person must read the diff and apply ``factory-verified`` before it can merge
  (#72, ``publish_block.review_hold``). It is deliberate, not a defect: it has its own code so that
  rates over the codes (the false-incomplete rate) can keep it apart from a real failure.
* ``env_noise``: verification is incomplete only because of failures that the baseline comparison
  classed as environmental (they fail on the base commit too): the verifier reported them as the
  sole blocker (``test_run.sole_blocker``, kept in the baseline record), the verifier itself called
  its verdict incomplete (``verifier_complete`` false: a verdict that says complete and still names
  a sole blocker contradicts itself, and a later missing-evidence failure on it is not noise),
  there are no new failures, and the baseline did not already clear the verdict. A verdict the baseline cleared that is
  incomplete again (missing metadata, artifact error) is ``unverified_other``.
* ``unverified_other``: incomplete for any other reason (the catch-all, meant to shrink).

Precedence, when several causes apply at once (the first that applies wins; ``ORDER`` is the one
definition, ``docs/handoff.md`` and ``docs/verification-tiers.md`` repeat it):

1. ``real_failure``: a defect in the change itself outranks everything about CI or the environment.
2. ``ci_failed``: a red required check (or a timeout) outranks one that is merely outstanding.
3. ``ci_pending``: evidence still to come outranks noise that cannot be fixed by the change.
4. ``workflow_hold``: a deliberate hold for a person, behind anything that is actually wrong.
5. ``env_noise``
6. ``unverified_other``: only when nothing above applies and the outcome is still not complete.

A complete job (review passed, verification complete, nothing held back, gate ``success``) has no
code: ``None`` here, ``null`` in ``status --json`` and nothing in the gate description.

There is deliberately no ``operator_item`` code: ``operator`` criteria are a checklist that never
makes a PR incomplete.
"""

REAL_FAILURE = "real_failure"
CI_FAILED = "ci_failed"
CI_PENDING = "ci_pending"
WORKFLOW_HOLD = "workflow_hold"
ENV_NOISE = "env_noise"
UNVERIFIED_OTHER = "unverified_other"

ORDER = (REAL_FAILURE, CI_FAILED, CI_PENDING, WORKFLOW_HOLD, ENV_NOISE, UNVERIFIED_OTHER)

MEANING = {
    REAL_FAILURE: "review did not pass, or a sandbox criterion really failed",
    CI_FAILED: "a required check is red, or waiting for CI timed out",
    CI_PENDING: "required checks have not finished on this head",
    WORKFLOW_HOLD: "the head changes workflow files: a person must review the diff and apply factory-verified",
    ENV_NOISE: "verification incomplete only because of failures that also fail on the base",
    UNVERIFIED_OTHER: "incomplete for any other reason",
}

LOG_LINE = "factory: reason_code job={job} issues={issues} commit={commit} code={code} {counts}"


def suffix(code):
    """The stable gate-description suffix (`` [code]``); empty without a code."""
    return f" [{code}]" if code in ORDER else ""


def _baseline(verification):
    record = verification.get("baseline") if isinstance(verification, dict) else None
    return record if isinstance(record, dict) and record.get("status") == "compared" else {}


def _count(value):
    return len(value) if isinstance(value, list) else 0


def derive(review, verification, ci=None, held=False, workflow_hold=False):
    """The reason code of an outcome, or ``None`` when nothing holds the job back.

    ``review`` and ``verification`` are the verdicts from ``state.json``. ``ci`` is ``"failed"``
    for a red required check or the CI timeout failure, ``"pending"`` for an awaiting gate, else
    ``None``. ``held`` is True when something else keeps the job from being ready (a ``ci``
    criterion that no required check covers). ``workflow_hold`` is True when the head changes workflow
    files and so waits for a person. Precedence is ``ORDER``.
    """
    review = review if isinstance(review, dict) else {}
    verification = verification if isinstance(verification, dict) else {}
    review_ok = review.get("pass") is True
    complete = verification.get("complete") is True
    base = _baseline(verification)
    causes = set()
    if review.get("pass") is False or (not complete and _count(base.get("new_failures"))):
        causes.add(REAL_FAILURE)
    if ci == "failed":
        causes.add(CI_FAILED)
    if ci == "pending":
        causes.add(CI_PENDING)
    if workflow_hold:
        causes.add(WORKFLOW_HOLD)
    if (
        not complete
        and not base.get("new_failures")
        and _count(base.get("environmental_failures"))
        and not verification.get("artifact_error")
        # The verifier named the tests the only reason for complete=false, and the baseline did not
        # clear them already. A record the baseline cleared (``cleared``) that is incomplete again
        # lost something else (metadata, evidence for the commit): that is not environmental noise.
        and base.get("sole_blocker") is True
        and base.get("verifier_complete") is False
        and base.get("cleared") is not True
    ):
        causes.add(ENV_NOISE)
    if not causes and not (review_ok and complete and not held):
        causes.add(UNVERIFIED_OTHER)
    return next((code for code in ORDER if code in causes), None)


def counts(review, verification, outstanding=0, awaited=0):
    """Fixed ``name=number`` pairs for the log line: numbers only, never text."""
    base = _baseline(verification)
    findings = review.get("findings") if isinstance(review, dict) else None
    return (
        f"review_findings={_count(findings)} new_failures={_count(base.get('new_failures'))} "
        f"env_failures={_count(base.get('environmental_failures'))} "
        f"outstanding={int(outstanding)} awaited={int(awaited)}"
    )


def log_line(job, issues, commit, code, counts_text):
    """The one fixed-format line per published job: ids, the code and counts only."""
    numbers = ",".join(str(n) for n in issues if isinstance(n, int)) or "none"
    return LOG_LINE.format(
        job=job, issues=numbers, commit=str(commit)[:7], code=code or "none", counts=counts_text
    )

"""The ``pending`` outcome of ``factory/gate``: waiting for the required checks of the exact head.

Review and verification passed and every ``sandbox`` criterion passed, but a ``ci`` criterion is
covered by required checks of the base branch that nobody has seen finish yet. ``Runner.publish``
then posts ``factory/gate`` as ``pending`` with a description that starts with ``AWAITING`` and
names the awaited checks (``awaiting_description``), instead of ``success``. A required pending
status simply blocks the merge until it resolves.

This module is the one step that resolves it (``cli.reconcile_command`` runs it under the
repository lease, so the manager's existing idle-reconcile loop is the only caller; no thread):

* **Which jobs**: ``pr_ready`` jobs of the run (a job Factory published as incomplete never had an
  awaiting gate and is never promoted) whose newest ``factory/gate`` status on the job's published
  commit is a ``pending`` one that Factory wrote as an awaiting gate, **written by Factory's own
  identity** (the App's bot user; a status that merely has the right text, from anyone else, is
  ignored). State comes from GitHub (that status, its time and its author) plus the read-only run
  directory; nothing is written to the run directory, so a restart resumes exactly where it was.
* **Exact head only**: only while the pull request is open and its head is still the job's
  published commit, before and after the checks are read. A moved head is never gated (nothing is
  posted for either SHA): Factory never posts a gate for a head it did not verify.
* **Outcome** from ``gh pr checks --required`` (Factory's own gate excluded): all green and every
  awaited check reported -> ``success``; any red -> ``failure`` and the job's issues get
  ``factory:ci-failed`` (each label is written and checked first, and the failure is posted only once
  all are in place, so a label that did not land is retried on the next pass; after ``CI_WAIT_SECONDS`` the
  failure is posted anyway, so a red pull request is never left pending for ever); otherwise it stays pending and
  posts nothing, until ``CI_WAIT_SECONDS`` after the awaiting status was posted, when it posts a
  ``failure`` that says it timed out waiting for CI. A check name that appears more than once
  counts as green only when every entry is green.
* **Recovery**: a ``failure`` that this module itself posted for CI (a red check, or the timeout)
  is not final. If the required checks of the same head later all pass (a flaky check re-run
  green, CI that finished after the bound), it posts ``success`` and puts the issues back to
  ``factory:pr-open``. A failure about review or verification is never touched.
* **Evidence in the pull request**: once the gate is posted (success, failure or the timeout), the
  pull request body's CI evidence block (``checks_ran``, between its two fixed markers) is replaced in
  place with the head commit, the outcome and each required check's name and conclusion, so the body
  says which checks ran where. Nothing else of the body changes, a body without the block (an older
  one, or edited by hand) is left alone, and an identical block is not written again. This is a
  report, never part of the gate: a failed read or write prints one fixed line and changes nothing
  about the status, the labels or the other jobs. It needs no permission beyond the ones publishing
  already uses (``pull_requests: write``, the same ``PATCH`` as ``completion``).

Everything is best effort like the status labels: a failed read or write is one fixed line (the
exception class only), three failures in a row end the step, and nothing here stops a loop.
"""

import re
import sys
from datetime import datetime, timezone

from . import checks_ran
from .github import GATE_CONTEXT, GREEN_BUCKETS, post_gate_status, required_checks
from .issue_labels import LABEL_CI_FAILED, LABEL_PR_OPEN
from .locks import LockError
from .model import criteria
from .reason_codes import CI_FAILED, CI_PENDING, suffix
from .shell import get_auth
from .trigger import pull_number

_SHA = re.compile(r"[A-Za-z0-9._-]{1,64}")
AWAITING = "awaiting required CI: "
TIMED_OUT = "timed out waiting for CI: "
FAILED_CI = "required CI failed: "
RESOLVABLE = ("pr_ready",)  # only a job published as complete ever had an awaiting gate
CI_WAIT_SECONDS = 3600  # how long a pending gate waits for the required checks
MAX_FAILURES = 3

FAILED_LINE = "factory: ci gate failed ({kind})"
FOREIGN_LINE = "factory: ci gate ignored a status that Factory did not write"
HELD_LINE = "factory: ci gate held back until its issue label lands"
EVIDENCE_LINE = "factory: ci evidence not written to the pull request body ({kind})"
NO_SECTION_LINE = "factory: ci evidence not written: the pull request body has no evidence block"


def awaited_checks(job, required):
    """The required checks the job's ``ci`` criteria rely on, sorted (empty: nothing to await).

    Only a criterion that names checks, every one of them required on the base branch, counts
    (the others are outstanding and keep the job incomplete: see ``model.outstanding_criteria``).
    An ``operator`` criterion is never awaited and never outstanding: it is a checklist item for a
    person (``operator_items``), and its tick, which anyone who can edit the pull request body
    can make, is only carried over into the next body and never reaches ``factory/gate``.
    """
    required = set(required or ())
    names = set()
    for item in criteria(job):
        if item["tag"] == "ci" and item["checks"] and all(n in required for n in item["checks"]):
            names.update(item["checks"])
    return sorted(names)


def _description(prefix, names, sha, code=None):
    """``prefix names @sha [code]``: the code is a stable suffix, the prefix stays first."""
    tail = f" @{sha[:7]}" + suffix(code)
    room = 140 - len(prefix) - len(tail)
    text = ", ".join(names)
    if len(text) > room:
        text = text[: max(room - 1, 0)].rstrip() + "…"
    return prefix + text + tail


def awaiting_description(names, sha, code=None):
    """The pending description: it names the awaited checks (cut to GitHub's 140 characters).

    ``code`` (``Runner.publish`` passes ``ci_pending``) is appended as a `` [code]`` suffix, so the
    description still starts with ``AWAITING`` and ``is_awaiting`` still recognises it.
    """
    return _description(AWAITING, names, sha, code)


def is_awaiting(description):
    """True for a pending description that ``awaiting_description`` wrote."""
    return isinstance(description, str) and description.startswith(AWAITING)


def named_checks(job):
    """Every check name a ``ci`` criterion of the job relies on, sorted (from the plan, not GitHub)."""
    return sorted({n for item in criteria(job) if item["tag"] == "ci" for n in item["checks"]})


def candidates(slug, selected, states):
    """``(job, pull request number, URL, published commit)`` of every ``pr_ready`` job of the plan."""
    jobs = {job["id"]: job for job in selected["jobs"]}
    found = []
    for key, state in states.items():
        if key not in jobs or not isinstance(state, dict) or state.get("status") not in RESOLVABLE:
            continue
        if not named_checks(jobs[key]):
            continue  # no ci criterion names a check: its gate never waits for CI
        commit, url = state.get("commit"), state.get("pr")
        number = pull_number({"jobs": {key: state}}, key, slug)
        if number is not None and isinstance(commit, str) and _SHA.fullmatch(commit):
            found.append((jobs[key], number, url, commit))
    return found


def _age(created, now):
    """Seconds since the ISO timestamp ``created``, or None when it cannot be read."""
    try:
        then = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
    except ValueError:
        return None
    if then.tzinfo is None:
        return None
    return (now - then).total_seconds()


def judge(checks, awaited):
    """``("success"|"failure"|None, [check names])`` of the required checks (the gate excluded).

    ``failure`` names the red checks; ``success`` needs at least one check, all of them green and
    every awaited one reported; anything else (running, cancelled, not reported yet, none) is None.
    A name that appears more than once (two workflows with a job of one name, a check run and a
    status with one context) is red if any entry is red and green only if every entry is green:
    the order of the list must never decide.
    """
    seen = {}
    for check in checks:
        name = check.get("name")
        if isinstance(name, str) and name != GATE_CONTEXT:
            seen.setdefault(name, []).append(check.get("bucket"))
    red = sorted(name for name, buckets in seen.items() if "fail" in buckets)
    if red:
        return "failure", red
    if (
        seen
        and all(all(bucket in GREEN_BUCKETS for bucket in buckets) for buckets in seen.values())
        and set(awaited) <= set(seen)
    ):
        return "success", sorted(seen)
    return None, []


def _label_all(guarded, labels, issues, label):
    """Write ``label`` to each issue, one sync at a time, and say whether every write landed.

    One sync per issue, checked straight away: ``IssueLabels`` forgets a missed write as soon as a
    later write succeeds, so a single sync over several issues cannot tell.
    """
    landed = True
    for issue in issues:
        guarded(labels.sync, {issue: label})
        if issue in labels.missed or issue in labels.pending:
            landed = False
    return landed


def _factory_test(reader):
    """``creator -> bool``: True only for Factory's own identity, False for everyone else.

    With the Factory App that is its bot user (type ``Bot`` and the App's user id); with an
    ambient ``gh`` login (development) that login. When the identity cannot be told, nothing
    passes: a status is never trusted on its text alone.
    """
    auth = get_auth()
    user_id = getattr(auth, "app_user_id", None)
    if type(user_id) is int and user_id > 0:
        return lambda creator: creator.get("type") == "Bot" and creator.get("id") == user_id
    if auth is None or type(auth).__name__ == "AmbientAuth":
        login = reader.login()
        if login:
            return lambda creator: creator.get("login") == login
    return lambda creator: False


def resolve_pending(
    slug, cwd, selected, states, reader, labels, fence=None, out=None, now=None, wait=CI_WAIT_SECONDS, skip=()
):
    """Resolve the pending gates of the ``pr_ready`` jobs of this run. Returns ``[(PR URL, state)]`` posted.

    ``reader`` is a ``trigger.GithubReader``, ``labels`` the run's ``IssueLabels`` (for
    ``factory:ci-failed``). ``now`` is a timezone-aware time (tests); ``wait`` the bound in seconds.
    ``skip`` are issue numbers another run of the repository owns: their labels are not touched.
    """
    out = out or sys.stderr
    now = now or datetime.now(timezone.utc)
    done, failures = [], 0
    is_factory = None  # asked once, and only when a gate of this run needs resolving

    def guarded(action, *args):
        nonlocal failures
        try:
            if fence is not None:
                fence()
            result = action(*args)
            failures = 0
            return True, result
        except LockError:
            failures = MAX_FAILURES
        except Exception as error:  # noqa: BLE001
            print(FAILED_LINE.format(kind=type(error).__name__), file=out)
            failures += 1
        return False, None

    def write_evidence(number, commit, outcome, checks, awaited):
        """Replace the CI evidence block of the pull request body; best effort, outside ``guarded``.

        Read as late as possible (a tick a person makes between this read and the write is lost, as at
        publish: GitHub has no compare-and-set), written only while the pull request is open on
        ``commit``, and never counted as a failure of the step: the gate is already posted.
        """
        try:
            if fence is not None:
                fence()
            pull = reader.pull_facts(number)
            if not (pull.state == "open" and pull.head == commit):
                return
            body = checks_ran.replace_ci(
                pull.body, checks_ran.ci_result(commit, outcome, checks, awaited)
            )
            if body is None:
                print(NO_SECTION_LINE, file=out)
            elif body != pull.body:
                if fence is not None:
                    fence()
                reader.set_pull_body(number, body)
        except LockError:
            return  # the next guarded call of the loop sees the lost lease and ends the step
        except Exception as error:  # noqa: BLE001
            print(EVIDENCE_LINE.format(kind=type(error).__name__), file=out)

    for job, number, url, commit in candidates(slug, selected, states):
        if failures >= MAX_FAILURES:
            break
        ok, gate = guarded(reader.status_record, commit, GATE_CONTEXT)
        if not ok:
            continue
        state, description, created, creator = gate
        awaiting = state == "pending" and is_awaiting(description)
        recovering = state == "failure" and isinstance(description, str) and description.startswith(
            (FAILED_CI, TIMED_OUT)
        )
        if not (awaiting or recovering):
            continue  # resolved already, never awaiting, or a failure about review or verification
        if is_factory is None:
            ok, is_factory = guarded(_factory_test, reader)
            if not ok:
                is_factory = None
                continue
        if not is_factory(creator):
            print(FOREIGN_LINE, file=out)  # the right words from anyone else prove nothing
            continue
        ok, pull = guarded(reader.pull_facts, number)
        if not (ok and pull.state == "open" and pull.head == commit):
            continue
        ok, checks = guarded(required_checks, cwd, url)
        if not ok:
            continue
        ok, again = guarded(reader.pull_facts, number)
        if not (ok and again.state == "open" and again.head == commit):
            continue  # the head moved while the checks were read: they may be another head's
        awaited = named_checks(job)
        verdict, names = judge(checks, awaited)
        outcome = verdict or "timeout"
        sha = commit[:7]
        issues = [issue for issue in job["issues"] if issue not in skip]
        if recovering:
            if verdict != "success":
                continue  # still red or still running: the failure stands, nothing is posted again
            new_state, new_description = "success", f"review+verify+required CI passed @{sha}"
            if description.startswith(FAILED_CI) and issues:
                if not _label_all(guarded, labels, issues, LABEL_PR_OPEN):
                    print(HELD_LINE, file=out)  # the gate is a failure already: waiting costs nothing
                    continue
        elif verdict == "success":
            new_state, new_description = "success", f"review+verify+required CI passed @{sha}"
            if issues:
                # Best effort and normally a no-op (the issue is already factory:pr-open): it heals a
                # factory:ci-failed left by a failure whose status post did not land earlier.
                _label_all(guarded, labels, issues, LABEL_PR_OPEN)
        elif verdict == "failure":
            new_state, new_description = "failure", _description(FAILED_CI, names, commit, CI_FAILED)
            if issues and not _label_all(guarded, labels, issues, LABEL_CI_FAILED):
                age = _age(created, now)
                if age is None or age < wait:
                    print(HELD_LINE, file=out)  # the gate stays pending, the next pass retries the label
                    continue
                # Past the bound: a failure without its label beats a red pull request that stays
                # pending for ever with nobody told.
        else:
            age = _age(created, now)
            if age is None or age < wait:
                continue  # still running within the bound: nothing new
            new_state, new_description = "failure", _description(TIMED_OUT, awaited, commit, CI_FAILED)
        if guarded(post_gate_status, cwd, slug, commit, new_state, new_description)[0]:
            done.append((url, new_state))
            write_evidence(number, commit, outcome, checks, awaited)
    return done

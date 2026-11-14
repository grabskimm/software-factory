"""Explicit, diagnosed CI repair using the existing bounded builder attempt."""

import subprocess
from urllib.parse import urlsplit, urlunsplit

from .github import GATE_CONTEXT, inspect_pr, required_checks
from .recovery import validate_resume


def _label(value):
    return "".join(c for c in str(value or "") if c.isprintable())[:200]


def _url(value):
    # Check metadata only: never ingest raw logs, query credentials or URL userinfo.
    try:
        parsed = urlsplit(str(value or ""))
        if parsed.scheme == "https" and parsed.hostname == "github.com":
            return urlunsplit(("https", "github.com", parsed.path[:500], "", ""))
    except ValueError:
        pass
    return ""


def failed_checks(pr, required):
    """Only positive failure evidence qualifies; pending/unknown/cancelled is not a defect.

    ``required`` is the base branch's required checks (``gh pr checks --required``): when
    readable and non-empty, only those can fail the PR. ``None`` (unreadable) or ``[]`` (nothing
    gates the merge) lets the whole rollup decide.
    """
    failures = []
    rollup = pr.get("statusCheckRollup")
    if not isinstance(rollup, list) or any(not isinstance(c, dict) for c in rollup):
        raise ValueError("CI repair requires a valid GitHub check rollup")
    for check in rollup:
        if required:
            break
        kind = check.get("__typename")
        conclusion = (
            check.get("conclusion") if kind == "CheckRun" else check.get("state")
        )
        # Factory's own gate status is not CI: a failing gate means "incomplete".
        if (check.get("name") or check.get("context")) == GATE_CONTEXT:
            continue
        if (
            kind == "CheckRun"
            and check.get("status") == "COMPLETED"
            and conclusion in {"FAILURE", "TIMED_OUT"}
        ) or (kind == "StatusContext" and conclusion in {"FAILURE", "ERROR"}):
            failures.append(
                {
                    "name": _label(check.get("name") or check.get("context")),
                    "result": conclusion,
                    "url": _url(check.get("detailsUrl") or check.get("targetUrl")),
                }
            )
    urls = {
        c.get("name") or c.get("context"): _url(c.get("detailsUrl") or c.get("targetUrl"))
        for c in rollup
    }
    for check in required or []:
        if check.get("name") == GATE_CONTEXT:
            continue
        if check.get("bucket") == "fail":
            failures.append(
                {
                    "name": _label(check.get("name")),
                    "result": "required check failed",
                    "url": urls.get(check.get("name"), ""),
                }
            )
    if not failures:
        raise ValueError(
            "CI repair requires a failing exact-head check; pending, cancelled, missing or passing checks do not qualify"
        )
    return sorted(failures, key=lambda c: (c["name"], c["result"]))[:50]


def conflicts_with_base(pr):
    return pr.get("mergeable") == "CONFLICTING" or pr.get("mergeStateStatus") == "DIRTY"


def validate_followup(repo, plan, state, key, base_branch="main"):
    """The retained exact head of a published job whose pull request is still open on the base branch.

    A follow-up session (the manager's @mention follow-up) builds on the pull request's own branch, so the head on
    GitHub must be the commit this job retained: anything else was pushed by somebody else, and the supervisor
    never builds on top of it. Unlike a CI repair it needs no failing check. Mutates nothing.
    """
    _, item, _, head = validate_resume(repo, plan, state, key, "review")
    if not item.get("pr"):
        raise ValueError("a follow-up requires an existing PR")
    pr = inspect_pr(repo, item["pr"])
    if (
        pr.get("state") != "OPEN"
        or pr.get("baseRefName") != base_branch
        or pr.get("headRefOid") != head
    ):
        raise ValueError(
            f"a follow-up requires an open {base_branch} PR matching the retained exact head"
        )
    return head


def diagnose_retry(repo, plan, state, key, reason, base_branch="main"):
    """Revalidate retained and remote heads before queuing one operator-diagnosed repair."""
    if not reason or not reason.strip():
        raise ValueError(
            "CI repair requires --reason with a sanitized diagnosis and proposed code fix"
        )
    _, item, _, head = validate_resume(repo, plan, state, key, "review")
    if not item.get("pr"):
        raise ValueError("CI repair requires an existing PR")
    pr = inspect_pr(repo, item["pr"])
    if (
        pr.get("state") != "OPEN"
        or pr.get("baseRefName") != base_branch
        or pr.get("headRefOid") != head
    ):
        raise ValueError(
            f"CI repair requires an open {base_branch} PR matching the retained exact head"
        )
    try:
        required = required_checks(repo, item["pr"])
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError):
        required = None  # unreadable: judge from the whole rollup, as before
    current = inspect_pr(repo, item["pr"])
    if any(
        current.get(field) != pr.get(field)
        for field in ("headRefOid", "baseRefName", "state")
    ):
        raise ValueError(
            "PR changed while inspecting CI repair evidence; inspect again"
        )
    if conflicts_with_base(current):
        # A sibling PR merged and this one no longer merges. GitHub does not re-run CI on a
        # conflicting PR, so the old (green) checks stay: this is a base-sync retry, not a CI
        # repair. The supervisor merges the base before the builder runs; no diagnosis recorded.
        return None
    evidence = {
        "commit": head,
        "diagnosis": reason.strip(),
        "failed_checks": failed_checks(current, required),
    }
    if any(
        event.get("ci_repair") == evidence for event in item.get("retry_history", [])
    ):
        raise ValueError(
            "this CI repair was already attempted at the same head; provide a changed diagnosis before retrying"
        )
    return evidence

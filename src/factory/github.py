"""GitHub inspection and hand-off helpers (supervisor side, through ``gh``).

Factory stops at a published pull request plus a ``factory/gate`` commit
status. It never merges and never closes issues: GitHub auto-merge, the
rulesets and ``Closes #N`` do that.
"""

import json
import re
import subprocess

from .shell import command, supervisor_env

GATE_CONTEXT = "factory/gate"
LABEL_READY = "factory:ready"
# Buckets of ``gh pr checks`` that satisfy a required check: GitHub treats a skipped required
# check as passing. ``ci_gate.judge`` and ``status.criteria_report`` both read this one constant.
GREEN_BUCKETS = ("pass", "skipping")

# The only label Factory puts on a pull request: handoff/factory-automerge.yml enrols
# a pull request in auto-merge only if it carries it. Nothing else depends on a pull
# request label (the draft state shows an incomplete one), so there are no others.
# name -> (colour, description). Fixed, so `label create --force` is idempotent and
# never recolours a label an operator already looks at.
LABEL_STYLES = {
    LABEL_READY: ("0e8a16", "Factory: review and verification passed"),
}


_ZWSP = "\u200b"
_ISSUE_URL = re.compile(r"(https?://[\w.-]+/[\w.-]+/[\w.-]+/(?:issues|pull)/)(?=\d)")
_REFERENCE = re.compile(r"(?<![\w&])((?:[\w.-]+/[\w.-]+)?#)(?=\d)")
_GH_NUMBER = re.compile(r"\b(GH-)(?=\d)", re.IGNORECASE)


def neutralise_refs(text):
    """Break issue references in agent-written text so GitHub reads no closing keyword in it.

    Only the structured ``Closes #N`` lines Factory builds from the plan's own
    issue numbers may close issues. A prompt-injected issue could otherwise make
    a builder's summary say ``Closes #other`` and have GitHub close it on merge.
    A zero-width space after ``#`` (or ``issues/``) stops the reference from
    resolving without changing how the text reads.
    """
    plain = str(text)
    plain = _ISSUE_URL.sub(lambda m: m.group(1) + _ZWSP, plain)
    plain = _REFERENCE.sub(lambda m: m.group(1) + _ZWSP, plain)
    return _GH_NUMBER.sub(lambda m: m.group(1) + _ZWSP, plain)


_CROSS_REPO_ISSUE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]{0,9}")
TITLE_LIMIT = 256  # GitHub's pull request title limit
TITLE_PREFIX = "factory: "
_ELLIPSIS = "\u2026"


def issue_reference(issue):
    """``#N`` for a plan issue number, ``owner/repo#N`` for a cross-repository one.

    Only a positive int or a strictly shaped ``owner/repo#N`` string is accepted, so
    nothing but a plan's own issue identifiers ever reaches a closing line or title.
    """
    if type(issue) is int and issue > 0:
        return f"#{issue}"
    if isinstance(issue, str) and _CROSS_REPO_ISSUE.fullmatch(issue):
        return issue
    raise ValueError(f"not an issue reference: {issue!r}")


def _require_issues(issues):
    issues = list(issues or [])
    if not issues:
        raise ValueError("a pull request needs at least one plan issue; the job has none")
    return issues


def closing_line(issues, closing):
    """The first line of a PR body: ``Closes #A, closes #B, refs #C``.

    ``closing(issue)`` says whether GitHub may close that issue on merge (``Closes``) or
    it is only referenced (``Refs``). GitHub needs the keyword once per reference.
    """
    parts = []
    for issue in _require_issues(issues):
        keyword = "Closes" if closing(issue) else "Refs"
        parts.append(f"{keyword if not parts else keyword.lower()} {issue_reference(issue)}")
    return ", ".join(parts)


_CLOSING_PART = re.compile(r"(?i)\b(closes|refs) ((?:[\w.-]+/[\w.-]+)?#\d+)")
NOTICE_MARK = "> **Merging this pull request"
_NOTICE_LINE = re.compile(r"\A((?:\r?\n)+)> \*\*Merging this pull request[^\r\n]*")


def closing_notice(first):
    """One plain line that says whether merging closes the issue, built from the first line of the body (what GitHub acts on).

    ``Closes #a, refs #b`` and ``Refs #c`` are easy to misread: the second leaves the issue open after the merge. So the
    line under it says so in words. It never puts a closing keyword (close, closes, fix, resolve ...) right before an issue
    number, because GitHub reads such a keyword without understanding a negation: "does not close #7" would close #7.
    """
    closes, refs = [], []
    for keyword, reference in _CLOSING_PART.findall(first):
        (closes if keyword.lower() == "closes" else refs).append(reference)
    if closes:
        text = f"{NOTICE_MARK} closes these issues: {', '.join(closes)}.**"
        if refs:
            text += f" It only mentions {', '.join(refs)}, which stays open."
        return text
    if not refs:
        return ""
    plural = len(refs) > 1
    return (
        f"{NOTICE_MARK} does not close the issue{'s' if plural else ''} it references ({', '.join(refs)}).** "
        "Factory could not prove the change complete (the reason is under Verification below). If you have checked it "
        "yourself, apply the label `factory-verified`: the first line then becomes a closing line. Otherwise close the "
        f"issue{'s' if plural else ''} by hand after the merge."
    )


def refresh_notice(first, rest):
    """``rest`` (the body after its first line) with the closing notice rewritten to match ``first``; unchanged without one.

    Used wherever Factory rewrites a first line later (the completion label, the epic demotion), so that the words under
    it never disagree with the line GitHub acts on. A body that has no notice (published before it existed) is not given one.
    """
    notice = closing_notice(first)
    found = _NOTICE_LINE.match(rest)
    if found is None or not notice:
        return rest
    return found.group(1) + notice + rest[found.end() :]


def pr_title(summary, issues, limit=TITLE_LIMIT):
    """``factory: <summary> (#N[, #M])``: the summary is neutralised and cut, the suffix never is."""
    suffix = " (" + ", ".join(issue_reference(i) for i in _require_issues(issues)) + ")"
    text = " ".join(neutralise_refs(summary).split())
    room = limit - len(TITLE_PREFIX) - len(suffix)
    if len(text) > room:
        text = text[: room - 1].rstrip() + _ELLIPSIS if room > 0 else ""
    return (TITLE_PREFIX + text).rstrip() + suffix


def parse_gh_json(output, operation, expected_type):
    """Keep malformed GitHub output actionable without copying potentially private data."""
    if not output.strip():
        raise ValueError(
            f"{operation}: gh returned empty JSON; check GitHub access and retry"
        )
    try:
        value = json.loads(output)
    except json.JSONDecodeError:
        raise ValueError(
            f"{operation}: gh returned invalid JSON; check GitHub CLI/API availability and retry"
        ) from None
    if not isinstance(value, expected_type):
        raise ValueError(f"{operation}: gh returned an unexpected JSON type")
    return value


def repo_args(slug):
    """``-R org/name`` when the repository is named; otherwise gh infers it from the cwd."""
    return ["-R", slug] if slug else []


def repo_path(slug):
    """REST path prefix; gh expands the placeholders from the cwd when no slug is given."""
    return f"repos/{slug}" if slug else "repos/{owner}/{repo}"


def lookup_failure(job, error=None, reason=None):
    """What a failed live PR lookup records: job id, exception class and a reason code.

    Never the exception message: a ``gh`` error can quote a token, issue text or a PR body.
    ``error`` is None for a job the refresh never reached because its time budget ran out.
    """
    from .github_auth import GitHubAuthError

    if reason is None:
        if isinstance(error, GitHubAuthError):
            reason = "auth_failed"
        elif isinstance(error, subprocess.TimeoutExpired):
            reason = "timeout"
        elif isinstance(error, ValueError):
            reason = "invalid_response"
        elif isinstance(error, (RuntimeError, subprocess.SubprocessError)):
            reason = "gh_failed"
        elif isinstance(error, OSError):
            reason = "os_error"
        else:
            reason = "lookup_failed"
    return {
        "job": job,
        "error": type(error).__name__ if error is not None else None,
        "reason": reason,
    }


def inspect_pr(repo, pr_url):
    fields = "headRefOid,baseRefName,isDraft,state,statusCheckRollup,mergeCommit,mergeable,mergeStateStatus"
    pr = parse_gh_json(
        command(["gh", "pr", "view", pr_url, "--json", fields], repo),
        "inspect PR checks",
        dict,
    )
    # The checks that gate the merge decide ci_failed. ``None`` means the required set
    # could not be read, so callers fall back to the whole rollup and say so.
    try:
        pr["requiredChecks"] = required_checks(repo, pr_url)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError):
        pr["requiredChecks"] = None
    return pr


def required_checks(repo, pr):
    result = subprocess.run(
        ["gh", "pr", "checks", pr, "--required", "--json", "name,bucket,state"],
        cwd=repo,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
        env=supervisor_env(),
    )
    if result.returncode not in {0, 1, 8}:
        raise RuntimeError(f"cannot inspect required checks: {result.stderr.strip()}")
    # gh exits 1 with this diagnostic for a repository without required checks.
    if (
        not result.stdout.strip()
        and "no required checks reported" in result.stderr.lower()
    ):
        return []
    checks = parse_gh_json(result.stdout, "inspect required checks", list)
    if any(not isinstance(check, dict) for check in checks):
        # External JSON validation uses ValueError consistently at the CLI boundary.
        raise ValueError("invalid required check response")  # noqa: TRY004
    return checks


def base_required_checks(repo, slug, branch):
    """Names of the status checks the base branch requires (branch protection and rulesets).

    Fail-safe: a lookup that fails or returns something unexpected contributes nothing, so
    a check that cannot be confirmed as required is never treated as required.
    """
    names = set()

    def read(path, kind):
        try:
            result = subprocess.run(
                ["gh", "api", path],
                cwd=repo,
                text=True,
                capture_output=True,
                timeout=120,
                check=False,
                env=supervisor_env(),
            )
        except (subprocess.SubprocessError, OSError):
            return None  # a hung or missing gh contributes nothing, as documented
        if result.returncode != 0:
            return None
        try:
            return parse_gh_json(result.stdout, "inspect base branch rules", kind)
        except ValueError:
            return None

    base = f"{repo_path(slug)}/branches/{branch}"
    protection = read(f"{base}/protection/required_status_checks", dict)
    if protection:
        for name in protection.get("contexts") or ():
            if isinstance(name, str):
                names.add(name)
        for check in protection.get("checks") or ():
            if isinstance(check, dict) and isinstance(check.get("context"), str):
                names.add(check["context"])
    rules = read(f"{repo_path(slug)}/rules/branches/{branch}", list)
    for rule in rules or ():
        if isinstance(rule, dict) and rule.get("type") == "required_status_checks":
            parameters = rule.get("parameters")
            for check in (parameters or {}).get("required_status_checks") or ():
                if isinstance(check, dict) and isinstance(check.get("context"), str):
                    names.add(check["context"])
    # factory/gate is Factory's own signal, posted after it has judged the job ready: a
    # criterion can never be proven by it (that would be circular).
    names.discard(GATE_CONTEXT)
    return names


def default_branch(repo, slug=None):
    """The repository's default branch name (closing keywords only work against it)."""
    name = command(
        [
            "gh",
            "repo",
            "view",
            *([slug] if slug else []),
            "--json",
            "defaultBranchRef",
            "--jq",
            ".defaultBranchRef.name",
        ],
        repo,
    )
    if not name:
        raise ValueError("cannot determine the repository's default branch")
    return name


def failed_checks(rollup):
    """Positively failed CI checks in a ``statusCheckRollup``, excluding Factory's own gate.

    Pending, cancelled or unknown results are not failures, and the gate status
    is Factory's own signal, not CI.
    """
    failures = []
    for check in rollup if isinstance(rollup, list) else []:
        if not isinstance(check, dict):
            continue
        kind = check.get("__typename")
        name = check.get("name") or check.get("context")
        if name == GATE_CONTEXT:
            continue
        if kind == "CheckRun":
            if check.get("status") == "COMPLETED" and check.get("conclusion") in {
                "FAILURE",
                "TIMED_OUT",
            }:
                failures.append(name)
        elif kind == "StatusContext" and check.get("state") in {"FAILURE", "ERROR"}:
            failures.append(name)
    return failures


def ci_assessment(pr):
    """Which failing checks decide ``ci_failed``, and which are shown for information only.

    With a readable required set (``pr["requiredChecks"]``, from ``gh pr checks --required``)
    only failing required checks count; other failing rollup checks are informational.
    A readable but empty set (nothing gates the merge) lets the whole rollup decide
    (``basis`` is ``rollup_no_required``); an unreadable one does too (``rollup_fallback``).
    ``factory/gate`` is Factory's own signal and never counts, in either mode.
    """
    pr = pr or {}
    rollup_failed = failed_checks(pr.get("statusCheckRollup"))
    required = pr.get("requiredChecks")
    if not isinstance(required, list):
        return {"basis": "rollup_fallback", "failed": rollup_failed, "informational": []}
    if not required:
        return {
            "basis": "rollup_no_required",
            "failed": sorted(set(rollup_failed)),
            "informational": [],
        }
    failed = sorted(
        {
            check.get("name")
            for check in required
            if isinstance(check, dict)
            and check.get("bucket") == "fail"
            and check.get("name") != GATE_CONTEXT
        }
    )
    return {
        "basis": "required",
        "failed": failed,
        "informational": [name for name in rollup_failed if name not in failed],
    }


def handoff_status(pr):
    """Where a published PR stands: published, ci_failed, merged or closed."""
    state = (pr or {}).get("state")
    if state == "MERGED":
        return "merged"
    if state == "CLOSED":
        return "closed"
    return "ci_failed" if ci_assessment(pr)["failed"] else "published"


def post_gate_status(repo, slug, sha, state, description):
    """Post the ``factory/gate`` commit status on the exact head SHA."""
    if state not in {"pending", "success", "failure", "error"}:
        raise ValueError("invalid commit status state")
    command(
        [
            "gh",
            "api",
            "-X",
            "POST",
            f"{repo_path(slug)}/statuses/{sha}",
            "-f",
            f"state={state}",
            "-f",
            f"context={GATE_CONTEXT}",
            "-f",
            f"description={description[:140]}",
        ],
        repo,
    )


def create_label(repo, slug, name, color, description, timeout=None):
    """One ``gh label create --force`` (idempotent: the colour and description are fixed)."""
    extra = {} if timeout is None else {"timeout": timeout}
    command(
        [
            "gh",
            "label",
            "create",
            name,
            "--force",
            "--color",
            color,
            "--description",
            description,
            *repo_args(slug),
        ],
        repo,
        **extra,
    )


def ensure_labels(repo, slug, labels):
    """Create missing labels with fixed colours (``--force`` then changes nothing on repeat)."""
    for name in labels:
        color, description = LABEL_STYLES[name]
        create_label(repo, slug, name, color, description)

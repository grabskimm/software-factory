"""Work the Factory App cannot push: workflow files, and what to keep when GitHub refuses it.

The Factory App has no ``workflows`` permission, on purpose (``docs/handoff.md``): with it, a
worker-influenced branch could change the deployment pipeline. GitHub therefore refuses any push by
the App that creates or updates a file under ``.github/workflows``. This module holds the small,
pure pieces the runner and ``status`` share:

* ``publish_blocked_paths``: which paths of a job (its planned ``likely_paths`` plus the paths a
  build actually changed) are, or contain, such files. Derived whenever it is needed, never stored in
  the pinned plan and never taken from the planner as a flag.
* ``redact_git_error``: the stderr of a refused ``git push`` made safe to keep in the job state.
  Credentials reach git only through an environment credential helper, so a token should never be
  in that text, but nothing here relies on it: secret patterns and the literal secret values of the
  process are redacted, control characters are removed and the size is capped.
* ``classify_push_error``: whether the refusal is the workflow-file rule. The wording is GitHub's
  and has not been confirmed against a live rejection here, so the match is tolerant; a text that
  does not match is simply some other failure.
* ``safe_ref``: a branch name or path that may be shown in an operator hint.
* ``review_hold``, ``recorded_hold``: whether a job's pull request must wait for a person's review (see there).

Nothing in here talks to git or GitHub.
"""

import re
import unicodedata
from pathlib import PurePosixPath

from . import issue_comments

# Directories whose files the Factory App is refused when it pushes them.
BLOCKED_PATHS = (".github/workflows",)

REJECTION_WORKFLOWS = "workflows_permission"
REJECTION_OTHER = "other"
# The reason Factory states when the rejection is the workflow-file rule (job ``error`` and the
# one comment on the issue).
WORKFLOWS_REASON = "rejected: workflow files need the workflows permission"

MAX_ERROR_CHARS = 2000  # what a job state keeps of a refused push's stderr
MAX_INPUT = 20_000  # what is looked at: the tail, where git prints the refusal
MAX_SHOWN = 5  # paths named in a prompt or a status line
MAX_SUMMARY = 300
_REDACTED = "[redacted]"

# GitHub's refusal reads, for an App: "refusing to allow a GitHub App to create or update workflow
# `.github/workflows/x.yml` without `workflows` permission" (an OAuth token: "... without
# `workflow` scope"). Every part is optional on purpose, and the quantifiers are bounded.
_WORKFLOW_REFUSED = (
    re.compile(r"(?i)refusing to allow[^\n]{0,200}\bworkflows?\b"),
    re.compile(r"(?i)\bworkflows?\b[^\n]{0,160}\bwithout\b[^\n]{0,40}\b(?:permission|scope)\b"),
)
# user:password@ inside a remote URL, whatever the scheme.
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{0,30}://)[^/\s@]{1,200}@")
_SAFE_REF = re.compile(r"[A-Za-z0-9/][A-Za-z0-9._/@+-]{0,299}")


def workflow_paths(paths):
    """The entries of ``paths`` that are, or contain, a file the Factory App cannot push.

    A path equal to or under ``.github/workflows`` counts, and so does ``.github`` itself (it
    contains it). ``.`` does not: it is the planner's "repository-wide" estimate, which says
    nothing about workflows. Order is kept, repeats are dropped; non-text entries are ignored.
    """
    found = []
    blocked = [PurePosixPath(path) for path in BLOCKED_PATHS]
    for path in paths if isinstance(paths, (list, tuple)) else ():
        if not isinstance(path, str) or not path.strip() or path in found:
            continue
        candidate = PurePosixPath(path)
        for item in blocked:
            below = candidate == item or item in candidate.parents
            above = candidate in item.parents and candidate != PurePosixPath(".")
            if below or above:
                found.append(path)
                break
    return found


def publish_blocked_paths(job, state=None):
    """The planned paths of ``job`` plus the paths its build changed that the App cannot push."""
    planned = job.get("likely_paths") if isinstance(job, dict) else None
    actual = state.get("actual_paths") if isinstance(state, dict) else None
    return workflow_paths(
        [
            *(planned if isinstance(planned, list) else ()),
            *(actual if isinstance(actual, list) else ()),
        ]
    )


def safe_ref(value):
    """``value`` when it is a plain branch name or path that is safe to print, else None."""
    return value if isinstance(value, str) and _SAFE_REF.fullmatch(value) else None


def review_hold(state, changed=None):
    """The workflow paths a person must review before this job's pull request can merge, else [].

    A job whose head changes files the Factory App cannot push, or that was parked for a person to
    push, is not handed to GitHub's auto-merge by Factory: a person pushed the branch, and nothing
    but a person has read the diff of the workflow files, so the pull request is published as a
    draft whose ``factory/gate`` fails until a person applies the ``factory-verified`` label.

    ``changed`` are the paths the head being published really changes against its base, read from
    git at publish time (never the planner's estimate): ``actual_paths`` only grows over retries, so
    a job retried to drop its workflow change would stay held by it. When git could not say
    (``changed`` is None) ``actual_paths`` decide, which is the safe direction: a path once changed
    counts.
    """
    checked = state if isinstance(state, dict) else {}
    found = workflow_paths(changed if changed is not None else checked.get("actual_paths"))
    if not found and checked.get("human_publish"):
        found = list(BLOCKED_PATHS)
    return found


def recorded_hold(state):
    """What held the head of a job already published: the paths its last publish recorded (``held_paths``).

    For ``status``, which has no worktree to ask. A state from before the paths were recorded falls
    back to ``review_hold``.
    """
    recorded = state.get("held_paths") if isinstance(state, dict) else None
    return review_hold(state, recorded if isinstance(recorded, list) else None)


def hold_gate_description(sha):
    """The ``factory/gate`` failure text for a pull request held for a person (a status keeps 140 characters)."""
    return f"workflow files changed: a person must review the diff and apply factory-verified @{sha[:7]}"


def planned_lines(plan):
    """One fixed-wording line per job of ``plan`` whose planned paths include files the App cannot push.

    Printed by ``plan`` and by ``status`` before a run has started, so a person hears of it before
    anything is built. Only the job id (when it is a plain name) is shown, never a path.
    """
    jobs = plan.get("jobs") if isinstance(plan, dict) else None
    lines = []
    for job in jobs if isinstance(jobs, list) else ():
        if isinstance(job, dict) and publish_blocked_paths(job):
            name = safe_ref(job.get("id")) or "a job"
            lines.append(
                f"{name}: changes files under .github/workflows; the Factory App cannot push them, "
                "so a person publishes this job's branch after it is built and checked"
            )
    return lines


def publish_hint(key, held):
    """What a person does with a job parked in ``needs_human_publish`` (``held`` is its ``human_publish``).

    Built only from the job id (a validated slug) and from the clone, branch and bundle path when
    they are plain names, so nothing a model or an issue wrote is printed. Git runs in the
    repository's own clone, never in the retained worktree: a worker can write that directory,
    including the ``.git`` file that says where its repository is, and git run there with a
    person's credentials would execute whatever that file points to.
    """
    record = held if isinstance(held, dict) else {}
    branch = safe_ref(record.get("branch")) or "the job branch"
    clone = safe_ref(record.get("repository"))
    bundle = safe_ref(record.get("bundle"))
    where = f"the Factory clone {clone}" if clone else "the Factory clone of the repository"
    kept = f"; the commits are also in the bundle {bundle}" if bundle else ""
    return (
        "review the diff of every .github/workflows file first, because a push runs the branch's "
        "on-push workflows with repository secrets; inspect it and "
        f"push {branch} with your own credentials from {where}, never with git inside the retained "
        "worktree (a worker can write it); then "
        f"resume {key} --phase publish, which opens a draft pull request that cannot merge until "
        f"a person applies the factory-verified label{kept}"
    )


def _plain(text):
    """``text`` without control characters; newlines stay, tabs and other line breaks become spaces."""
    out = []
    for char in text:
        category = unicodedata.category(char)
        if char == "\t" or category in ("Zl", "Zp", "Zs"):
            out.append(" ")
        elif char == "\n" or category[0] != "C":
            out.append(char)
    return "".join(out)


def redact_git_error(text, limit=MAX_ERROR_CHARS, secrets=None):
    """The stderr of a failed git command, safe to keep: redacted, plain and at most ``limit`` characters.

    Only the tail is read (git prints the refusal last). ``secrets`` are literal values to remove;
    left out, the secret values of this process are used (``issue_comments._secret_values``). The
    result may be empty. Never raises for a text it cannot read: that is the empty string.
    """
    decoded = text.decode("utf-8", errors="replace") if isinstance(text, bytes) else text
    if not isinstance(decoded, str):
        return ""
    cleaned = _plain(decoded[-MAX_INPUT:])
    for value in issue_comments._secret_values() if secrets is None else secrets:
        if value:
            cleaned = cleaned.replace(value, _REDACTED)
    cleaned = _URL_USERINFO.sub(rf"\1{_REDACTED}@", cleaned)
    for pattern in issue_comments._SECRETS:
        cleaned = pattern.sub(_REDACTED, cleaned)
    lines = [line.rstrip() for line in cleaned.split("\n")]
    cleaned = "\n".join(line for line in lines if line)
    if len(cleaned) > limit:
        cleaned = f"…{cleaned[-(limit - 1):]}"
    return cleaned


def classify_push_error(text):
    """``REJECTION_WORKFLOWS`` when a refused push's stderr is the workflow-file rule, else ``REJECTION_OTHER``."""
    if isinstance(text, str):
        tail = text[-MAX_INPUT:]
        if any(pattern.search(tail) for pattern in _WORKFLOW_REFUSED):
            return REJECTION_WORKFLOWS
    return REJECTION_OTHER


def one_line(text, limit=MAX_SUMMARY):
    """The last ``limit`` characters of an already redacted text on a single line."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else f"…{flat[-(limit - 1):]}"

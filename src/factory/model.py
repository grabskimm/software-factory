"""Validation and conservative scheduling, independent of external services."""

import re
import unicodedata
from pathlib import PurePosixPath

from . import epic
from .artifact_paths import artifact_roots
from .conventions import TYPES as CHANGE_TYPES
from .model_choice import check_choice


TAGS = ("sandbox", "ci", "operator")
DEFAULT_TAG = "sandbox"
MAX_CHECKS = 20
MAX_NAME = 120
MAX_REASON = 300
# Printable, non-space characters that still draw nothing: Hangul fillers and the blank braille
# cell (``str.isprintable`` already rejects control, format and separator characters). Written as
# escapes so that an editor or formatter cannot silently drop them: U+115F, U+1160 (Hangul choseong
# and jungseong fillers), U+3164, U+FFA0 (Hangul fillers), U+2800 (blank braille pattern).
_BLANK_LOOKING = frozenset("\u115f\u1160\u3164\uffa0\u2800")
_CRITERION_KEYS = {"text", "tag", "checks", "workflow", "reason"}


def _name(value):
    """A check or workflow name: untrusted text, kept to one printable line."""
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_NAME
        and value == value.strip()
        and value.isprintable()
    )


def _shows(char):
    """Whether one character of a reason draws something.

    A combining or nonspacing mark (category ``M``: variation selectors, the combining grapheme
    joiner, Khmer inherent vowels...) is printable but draws nothing on its own, so a reason made
    only of marks shows nothing, like one made only of spaces.
    """
    return (
        char.isprintable()
        and not char.isspace()
        and char not in _BLANK_LOOKING
        and unicodedata.category(char)[0] != "M"
    )


def _clean_reason(reason):
    """An ``operator`` reason as kept text: cut to ``MAX_REASON``, or ``None`` when it shows nothing.

    The reason is optional decoration from a model, so it can never cost a whole plan: an
    over-long one is cut to at most ``MAX_REASON`` characters, ending in ``...`` so the cut is
    visible, and one with no visible character (empty, whitespace, control, format or
    blank-looking characters, or only combining marks) is dropped, which gives the checklist its fixed sentence. What is
    kept is judged after the cut, and cutting an already cut reason changes nothing.
    """
    cut = len(reason) > MAX_REASON
    kept = reason[: MAX_REASON - 3] if cut else reason
    if not any(_shows(c) for c in kept):
        return None
    return f"{kept.rstrip()}..." if cut else reason


def criterion(item):
    """The canonical ``{text, tag, checks[, workflow][, reason]}`` of one acceptance criterion.

    A plain string and an object without a tag are ``sandbox`` criteria. The tag says where
    a criterion is proven, never whether: ``ci`` names the required checks of the base
    branch (``checks``) or a workflow (``workflow``) that prove it on the exact head, and
    needs at least one of them. An ``operator`` criterion may say why a person must prove it
    (``reason``, optional): a reason that is over-long, shows nothing or sits on another tag
    is cut or dropped (``_clean_reason``) rather than rejected, but one that is not a string
    is a shape error. Everything here is untrusted data: it is only validated and displayed,
    never executed. Errors are fixed strings that do not echo the input.
    """
    if isinstance(item, str):
        item = {"text": item}
    if not isinstance(item, dict) or set(item) - _CRITERION_KEYS:
        raise ValueError("invalid acceptance criterion: unsupported shape")
    text = item.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("invalid acceptance criterion: text must be non-empty")
    tag = item.get("tag", DEFAULT_TAG)
    if not isinstance(tag, str) or tag not in TAGS:
        raise ValueError("invalid acceptance criterion: unknown tag")
    checks = item.get("checks", [])
    workflow = item.get("workflow")
    if (
        not isinstance(checks, list)
        or len(checks) > MAX_CHECKS
        or not all(_name(name) for name in checks)
        or len(set(checks)) != len(checks)
        or (workflow is not None and not _name(workflow))
    ):
        raise ValueError("invalid acceptance criterion: invalid check or workflow name")
    if tag != "ci" and (checks or workflow is not None):
        raise ValueError("invalid acceptance criterion: check names are only for ci")
    if tag == "ci" and not checks and workflow is None:
        raise ValueError("invalid acceptance criterion: ci needs check or workflow names")
    reason = item.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise ValueError("invalid acceptance criterion: reason must be text")
    if tag != "operator":
        reason = None
    elif reason is not None:
        reason = _clean_reason(reason)
    result = {"text": text, "tag": tag, "checks": list(checks)}
    if workflow is not None:
        result["workflow"] = workflow
    if reason is not None:
        result["reason"] = reason
    return result


def criteria(job):
    """The validated, canonical acceptance criteria of a job; plain strings stay valid."""
    items = job.get("acceptance")
    if not isinstance(items, list) or not items:
        raise ValueError("missing acceptance criteria")
    return [criterion(item) for item in items]


def describe_criterion(item):
    """One display line: ``[tag] text`` plus any check or workflow names."""
    item = criterion(item)
    names = []
    if item["checks"]:
        names.append("checks: " + ", ".join(item["checks"]))
    if item.get("workflow"):
        names.append("workflow: " + item["workflow"])
    return f"[{item['tag']}] {item['text']}" + (f" ({'; '.join(names)})" if names else "")


def outstanding_criteria(job, required):
    """The ``ci`` criteria nothing in Factory or GitHub would prove before this job merges.

    ``required`` is the set of check names the base branch requires (empty when unknown or
    unreadable). A ``ci`` criterion is proven by GitHub only when it names checks and every one
    of them is required, so a workflow-only one or one with a check that is not required is
    outstanding. ``sandbox`` criteria are covered by verification. An ``operator`` criterion is
    never outstanding: a person checks it from the pull request checklist
    (``operator_items``), and it never decides whether the job is ready. Returns the canonical
    criteria, never executes them.
    """
    required = set(required or ())
    return [
        item
        for item in criteria(job)
        if item["tag"] == "ci"
        and not (item["checks"] and all(name in required for name in item["checks"]))
    ]


def validate_plan(plan):
    if plan.get("version") != 1:
        raise ValueError("plan version must be 1")
    issues = [issue["number"] for issue in plan["issues"]]
    if (
        not issues
        or any(type(n) is not int or n <= 0 for n in issues)
        or len(set(issues)) != len(issues)
    ):
        raise ValueError("selected issue numbers must be positive and unique")
    jobs = {}
    owners = {}
    for job in plan["jobs"]:
        key = job["id"]
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,59}", key) or key in jobs:
            raise ValueError("job IDs must be unique short lowercase slugs")
        jobs[key] = job
        if not job["objective"] or not job["acceptance"] or not job["issues"]:
            raise ValueError(f"{key}: missing objective, acceptance, or issues")
        try:
            criteria(job)
        except ValueError as error:
            raise ValueError(f"{key}: {error}") from None
        if type(job.get("feature")) is not bool:
            raise ValueError(f"{key}: feature must be boolean")
        if "change_type" in job and job["change_type"] not in CHANGE_TYPES:
            raise ValueError(f"{key}: change_type must be a Conventional Commits type")
        if not job["likely_paths"]:
            raise ValueError(
                f"{key}: at least one likely path required; use '.' for repository-wide"
            )
        for path in job["likely_paths"]:
            p = PurePosixPath(path)
            if (
                not path
                or p.is_absolute()
                or ".." in p.parts
                or "\\" in path
                or any(c in path for c in "*?[]")
            ):
                raise ValueError(
                    f"{key}: paths must be literal repository-relative paths"
                )
        for number in job["issues"]:
            if number not in issues or number in owners:
                raise ValueError(f"{key}: unknown or multiply owned issue {number}")
            owners[number] = key
        if any(type(n) is not int or n <= 0 for n in job.get("context_issues", [])):
            raise ValueError(f"{key}: invalid context issue")
        if "choice" in job:
            try:
                check_choice(job["choice"])
            except ValueError as error:
                raise ValueError(f"{key}: {error}") from None
        if "epic_phase" in job:
            # The phase stamp (epic.bind_phases): valid, and the job is exactly that phase issue and nothing else.
            try:
                stamp = epic.check_phase_stamp(job["epic_phase"])
            except ValueError as error:
                raise ValueError(f"{key}: {error}") from None
            if job["issues"] != [stamp["child"]]:
                raise ValueError(f"{key}: invalid epic phase stamp")
    dispositions = {}
    for item in plan["dispositions"]:
        number = item["issue"]
        if number not in issues or number in dispositions or not item.get("reason"):
            raise ValueError(
                "dispositions must cover selected issues exactly once with reasons"
            )
        if item["status"] == "job":
            if item.get("job") != owners.get(number) or number not in owners:
                raise ValueError(
                    f"issue {number}: disposition does not match job ownership"
                )
        elif (
            item["status"] not in {"investigation", "covered", "blocked", epic.STATUS}
            or number in owners
        ):
            raise ValueError(f"issue {number}: invalid disposition")
        if item["status"] == epic.STATUS:
            # The phase plan and the questions of an epic: bounded, canonical, never echoed in an error.
            try:
                epic.validate(item.get("epic"))
            except ValueError as error:
                raise ValueError(f"issue {number}: {error}") from None
        elif "epic" in item:
            raise ValueError(f"issue {number}: invalid disposition")
        dispositions[number] = item
    if set(dispositions) != set(issues):
        raise ValueError("every selected issue needs a disposition")
    visited, visiting = set(), set()

    def visit(key):
        if key not in jobs or key in visiting:
            raise ValueError(f"unknown or cyclic dependency: {key}")
        if key in visited:
            return
        visiting.add(key)
        for dependency in jobs[key].get("depends_on", []):
            visit(dependency)
        visiting.remove(key)
        visited.add(key)

    for key in jobs:
        visit(key)
    for job in plan["jobs"]:
        artifact_roots(job, plan)
    return plan


def overlaps(left, right):
    for a in left:
        for b in right:
            pa, pb = PurePosixPath(a), PurePosixPath(b)
            if pa == pb or pa in pb.parents or pb in pa.parents:
                return True
    return False


def job_paths(spec, state):
    """Planned plus actual paths of a job; no known path means the whole repository."""
    paths = list(spec.get("likely_paths") or []) + list(state.get("actual_paths") or [])
    return paths or ["."]


def holds_unmerged_work(state):
    """Whether a job is running or has a branch or pull request that is neither merged nor closed.

    A job that failed or was blocked before it ever made a branch holds nothing.
    """
    status = state.get("status")
    if status in {"merged", "closed"}:
        return False
    return status == "running" or bool(state.get("branch") or state.get("pr"))


def shared_path_blockers(key, jobs, states):
    """Siblings, in plan order, that a not yet started job must wait for.

    A sibling blocks while it holds unmerged work (``holds_unmerged_work``) and its paths overlap
    this job's (``.`` or unknown paths overlap everything). A sibling that depends on this job,
    directly or not, never blocks it: that wait could only deadlock. Only a queued job without
    a branch of its own waits; one that already has a branch is repaired in place.
    """
    state = states.get(key) or {}
    if state.get("status") != "queued" or state.get("branch") or state.get("pr"):
        return []
    dependents, grew = set(), True
    while grew:
        grew = False
        for other, spec in jobs.items():
            if other not in dependents and any(
                d == key or d in dependents for d in spec.get("depends_on", [])
            ):
                dependents.add(other)
                grew = True
    paths = job_paths(jobs[key], state)
    return [
        other
        for other, spec in jobs.items()
        if other != key
        and other not in dependents
        and holds_unmerged_work(states.get(other) or {})
        and overlaps(paths, job_paths(spec, states.get(other) or {}))
    ]


def feature_acceptance_required(job, state):
    """A later repair cannot remove acceptance already required by this assignment."""
    build = state.get("build")
    return bool(
        job["feature"]
        or (build is not None and build.get("feature", True))
        or any(
            checkpoint.get("build", {}).get("feature") is True
            for checkpoint in state.get("checkpoint_history", [])
            if checkpoint.get("build") is not None
        )
    )


def verification_skipped(verification):
    """Whether a complete verification record is one where nothing was run (``verify.when``).

    ``Runner.review`` marks such a record ``skipped: True``. A complete record without that
    marker and without any outcome assertion (kept by an older run, or imported as evidence for
    a job that did not need verification) is treated the same way: a verification that ran always
    carries assertions, so one without them proves nothing about the criteria.
    """
    if not isinstance(verification, dict) or verification.get("complete") is not True:
        return False
    if verification.get("skipped") is True:
        return True
    assertions = verification.get("assertions")
    return not (
        isinstance(assertions, list)
        and any(isinstance(item, str) and item.strip() for item in assertions)
    )


def verify_required(when, job, state):
    """Whether the verify phase runs for this job (``verify.when`` in the repo config)."""
    if when == "always":
        return True
    if when == "never":
        return False
    return feature_acceptance_required(job, state)


def runnable(job, state, active, jobs):
    return (
        state[job["id"]]["status"] == "queued"
        and all(state[d]["status"] == "merged" for d in job.get("depends_on", []))
        and not any(
            overlaps(job["likely_paths"], jobs[key]["likely_paths"]) for key in active
        )
    )

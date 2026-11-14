"""Epic mode, the plan side: the ``epic`` plan disposition and its bounded phase plan.

A planner that finds an issue too large for one change but decomposable returns the disposition
``epic`` instead of a dead-end ``blocked``. The disposition carries a phase plan and numbered questions::

    {"issue": 30, "status": "epic", "reason": "why one change cannot hold it",
     "epic": {"phases": [{"title": "...", "objective": "...", "paths": ["src/x"],
                          "acceptance": ["..."], "depends_on": [1], "cost": "M"}],
              "questions": ["..."]}}

This module is pure (standard library only, no GitHub and no other Factory module at import time), so
``model.py``, ``fragments.py``, ``issue_comments.py`` and ``issue_labels.py`` can all use it:

* ``validate`` bounds the payload (at most ``MAX_PHASES`` phases and ``MAX_QUESTIONS`` questions, a
  length cap per field, ``depends_on`` naming only earlier phases) and returns the canonical payload.
  Nothing here is echoed into an error message: the model's text is untrusted data.
* ``hidden`` and ``parse_hidden`` carry the (already sanitised) plan inside the one plan comment as a
  hidden marker, so that the plan can be found again from GitHub alone after the run directory is
  purged. The comment is rendered in ``issue_comments.py`` and its hidden marker starts with the shared
  outcome marker prefix, so ``without_factory_comments`` keeps it away from the planner.
* ``downgrade`` is the one fall-back: where epic mode cannot work (switched off, Factory cannot comment,
  or no gate pass runs because ``auto_build`` is off) the planner's ``epic`` becomes today's ``blocked``,
  never a silent dead end. Where it can work, an ``epic`` whose payload breaks ``validate`` (the schema
  cannot express every rule) becomes ``blocked`` too, so that one bad epic never fails a whole plan.

The human gate (needs-info, awaiting-approval, plan-approved) is in ``epic_gate.py``; the phase issues that follow an
approval, and the status comment that is their ledger, are in ``epic_phases.py``. This module also holds the two
pure pieces both of them and the reader share: the digest of a plan (``digest``) and the marker line of a phase
issue (``phase_marker``, ``parse_phase_marker``) with the hash of its text (``content_hash``).

The links between a phase's pull request and its issues (slice 2) are pure here as well. ``phase_stamp`` is what a plan
records on the job of a phase issue (decided at plan time from the issue itself, ``epic_phases.plan_stamps``), ``first_line`` is
the ONE place that builds the first line of a pull request body (the runner's publish, the completion of an incomplete pull
request and the demotion of a stale one all go through it), and ``bind_phases`` and ``downgrade(nested=...)`` keep a phase
from being planned as an epic of its own or grouped with other issues.
"""

import base64
import binascii
import hashlib
import json
import os
import re
from dataclasses import dataclass

KIND = "epic"
STATUS = "epic"


class PlanSearchTruncated(Exception):
    """The search for Factory's plan comment stopped at the page bound, so "not found" proves nothing. Carries no text."""


# The label a person applies to approve the plan. Human-owned: Factory only makes sure it exists.
LABEL_APPROVE = "factory-approve-plan"
APPROVE_COLOR = "0e8a16"
APPROVE_DESCRIPTION = "Factory: apply to approve the phase plan in the Factory comment (write access or above)"

ENV_SWITCH = "FACTORY_EPIC_MODE"
_OFF = {"0", "off", "false", "no", "disabled"}

MAX_PHASES = 8
MAX_QUESTIONS = 10
MAX_TITLE = 100
MAX_OBJECTIVE = 400
MAX_PATHS = 6
MAX_PATH = 120
MAX_ACCEPTANCE = 5
MAX_CRITERION = 200
MAX_QUESTION = 300
COSTS = ("S", "M", "L")

_REQUIRED = {"title", "objective", "paths", "acceptance", "cost"}
_OPTIONAL = {"depends_on"}

LABEL_PHASE = "factory:epic-phase"  # the origin label of a phase issue; the name is also in issue_labels.py
HIDDEN_PREFIX = "<!-- factory-epic-plan:v1 "
HIDDEN_SUFFIX = " -->"
_HIDDEN = re.compile(r"<!-- factory-epic-plan:v1 ([A-Za-z0-9_-]{1,60000}) -->")

PHASE_PREFIX = "<!-- factory-epic-phase:v1 "
_PHASE = re.compile(
    r"<!-- factory-epic-phase:v1 parent=([0-9]{1,9}) phase=([0-9]{1,2}) of=([0-9]{1,2}) plan=([0-9a-f]{64}) -->"
)

BEGIN = "<!-- epic:begin -->"
END = "<!-- epic:end -->"

# Fixed reasons of a phase issue that cannot be planned the usual way. No model text and no issue reference (the reason is
# rendered by the blocked-comment path like any other).
NESTED_REASON = (
    "This issue is one phase of an epic plan that Factory opened, and a phase is not split into further phases, so nothing "
    "was planned for it. Ask for a smaller change in a new issue, or build the phase by hand."
)
GROUPED_REASON = (
    "This issue is one phase of an epic plan, and a phase is planned and built on its own: the planner grouped it with other "
    "issues in one job, so none of them was planned. Ask for a new plan."
)
DEPENDENCY_REASON = (
    "A job it depends on was blocked because it grouped a phase issue of an epic plan with other issues, and a phase is "
    "planned and built on its own. Ask for a new plan."
)


def switched_off(env=None):
    """True when the deployment-wide kill switch (``FACTORY_EPIC_MODE=off``) is set."""
    source = os.environ if env is None else env
    return (source.get(ENV_SWITCH) or "").strip().lower() in _OFF


def enabled(cfg, env=None):
    """Whether epic mode is on for a repository config: anything but a real ``True`` is off."""
    return getattr(cfg, "epic_mode", True) is True and not switched_off(env)


def can_plan(cfg, env=None):
    """Whether a planner's ``epic`` may stand: epic mode is on, Factory can comment AND a gate pass runs.

    An epic whose questions nobody can see would pause silently, so without a comment channel
    (``comment_on_blocked = false`` or ``FACTORY_ISSUE_COMMENTS=off``) the disposition is downgraded.
    So is an epic nothing would move on: the gate pass is part of the label-trigger pass, so a repository
    with ``auto_build = false`` keeps today's ``blocked``.
    """
    from . import issue_comments, trigger  # imported late: both import this module

    return (
        enabled(cfg, env)
        and getattr(cfg, "comment_on_blocked", True) is True
        and not issue_comments.switched_off(env)
        and trigger.settings_of(cfg).enabled
    )


def _text(value, cap):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= cap


def _texts(value, low, high, cap):
    return (
        isinstance(value, list)
        and low <= len(value) <= high
        and all(_text(item, cap) for item in value)
    )


def validate(payload):
    """The canonical ``{"phases": [...], "questions": [...]}`` of an epic payload, or ``ValueError``.

    Phases are numbered from 1 in the order given; ``depends_on`` lists phase numbers and may name
    only earlier phases (so the order is a valid delivery order and cannot cycle). Errors are fixed
    strings that never carry the payload.
    """
    if not isinstance(payload, dict) or not payload.keys() <= {"phases", "questions"}:
        raise ValueError("invalid epic: unsupported shape")
    phases = payload.get("phases")
    if not isinstance(phases, list) or not 1 <= len(phases) <= MAX_PHASES:
        raise ValueError(f"invalid epic: between 1 and {MAX_PHASES} phases are required")
    clean = []
    for number, phase in enumerate(phases, 1):
        if not isinstance(phase, dict) or not _REQUIRED <= phase.keys() <= _REQUIRED | _OPTIONAL:
            raise ValueError("invalid epic: phase fields")
        if not _text(phase["title"], MAX_TITLE) or not _text(phase["objective"], MAX_OBJECTIVE):
            raise ValueError("invalid epic: phase title or objective")
        if not _texts(phase["paths"], 1, MAX_PATHS, MAX_PATH):
            raise ValueError("invalid epic: phase paths")
        if not _texts(phase["acceptance"], 1, MAX_ACCEPTANCE, MAX_CRITERION):
            raise ValueError("invalid epic: phase acceptance")
        if phase["cost"] not in COSTS:
            raise ValueError("invalid epic: phase cost")
        depends = phase.get("depends_on", [])
        if (
            not isinstance(depends, list)
            or not all(isinstance(item, int) and not isinstance(item, bool) and 1 <= item < number for item in depends)
            or len(set(depends)) != len(depends)
        ):
            raise ValueError("invalid epic: phase dependencies")
        clean.append(
            {
                "title": phase["title"],
                "objective": phase["objective"],
                "paths": list(phase["paths"]),
                "acceptance": list(phase["acceptance"]),
                "depends_on": sorted(depends),
                "cost": phase["cost"],
            }
        )
    questions = payload.get("questions", [])
    if not _texts(questions, 0, MAX_QUESTIONS, MAX_QUESTION):
        raise ValueError("invalid epic: questions")
    return {"phases": clean, "questions": list(questions)}


def question_count(item):
    """How many numbered questions an epic disposition carries (0 when it has none or is malformed)."""
    payload = item.get("epic") if isinstance(item, dict) else None
    questions = payload.get("questions") if isinstance(payload, dict) else None
    return len(questions) if isinstance(questions, list) else 0


def hidden(issue, clean):
    """The hidden marker line that carries a (sanitised) epic payload for ``issue``."""
    document = {"v": 1, "issue": issue, "phases": clean["phases"], "questions": clean["questions"]}
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"{HIDDEN_PREFIX}{base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')}{HIDDEN_SUFFIX}"


def parse_hidden(body):
    """``{"issue", "phases", "questions"}`` from a plan comment's hidden marker, or None.

    Anything that does not decode, parse and validate again is None: the caller then knows nothing
    about the plan (the gate treats that as "unknown", never as "no questions").
    """
    if not isinstance(body, str):
        return None
    for line in body.splitlines()[:4]:
        found = _HIDDEN.fullmatch(line.strip())
        if found is None:
            continue
        data = found.group(1)
        try:
            document = json.loads(base64.urlsafe_b64decode(f"{data}{'=' * (-len(data) % 4)}").decode("utf-8"))
            issue = document["issue"]
            if document["v"] != 1 or not isinstance(issue, int) or isinstance(issue, bool) or issue <= 0:
                return None
            clean = validate({"phases": document["phases"], "questions": document["questions"]})
        except (binascii.Error, ValueError, KeyError, TypeError, RecursionError):
            return None
        return {"issue": issue, **clean}
    return None


def digest(plan):
    """The sha256 (hex) of the hidden payload of a parsed plan (``parse_hidden``): what a phase issue is bound to.

    The visible text of the plan comment is not part of it, so a change of the template does not make existing
    phase issues look like another plan's; any change of the phases, their order or the questions does.
    """
    line = hidden(plan["issue"], {"phases": plan["phases"], "questions": plan["questions"]})
    return hashlib.sha256(line.encode("ascii")).hexdigest()


def phase_marker(parent, phase, count, plan_digest):
    """The first line of a phase issue: which parent, which phase of how many, and which plan (its digest)."""
    return f"{PHASE_PREFIX}parent={int(parent)} phase={int(phase)} of={int(count)} plan={plan_digest} -->"


def parse_phase_marker(line):
    """``(parent, phase, count, digest)`` of a phase issue's first line, or None for anything else."""
    found = _PHASE.fullmatch(line.strip()) if isinstance(line, str) else None
    if found is None:
        return None
    parent, phase, count = int(found.group(1)), int(found.group(2)), int(found.group(3))
    return (parent, phase, count, found.group(4)) if parent > 0 and 1 <= phase <= count <= MAX_PHASES else None


def content_hash(title, body):
    """The sha256 (hex) of a phase issue's title and body, line endings normalised: compared later, never re-rendered."""
    clean = str(body).replace("\r\n", "\n").strip()
    text = f"{str(title).strip()}\0{clean}"
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _valid(payload):
    """Whether ``validate`` accepts the payload (it never raises)."""
    try:
        validate(payload)
    except ValueError:
        return False
    return True


def downgrade(plan, only_invalid=False, nested=()):
    """Turn ``epic`` dispositions of a planner result into ``blocked``, keeping the reason (in place).

    Every one by default (epic mode cannot work here). With ``only_invalid`` just those whose payload
    ``validate`` refuses: the planner schema cannot express the dependency order, the uniqueness or the
    non-blank rules, and one such epic must not fail the plan of its whole batch.

    ``nested`` holds the numbers of the issues that are phase issues of an epic (or that could not be shown not to be, see
    ``epic_phases.plan_stamps``): an ``epic`` for one of those is ALWAYS blocked, with the fixed ``NESTED_REASON`` instead
    of the planner's text, so that a phase is never split into grandchildren.
    """
    for item in plan.get("dispositions", ()):
        if isinstance(item, dict) and item.get("status") == STATUS:
            if item.get("issue") in nested:
                item["reason"] = NESTED_REASON
            elif only_invalid and _valid(item.get("epic")):
                continue
            item["status"] = "blocked"
            item.pop("epic", None)
    return plan


# --- the links between a phase's pull request and its issues -------------------------------------------------------

_HEX64 = re.compile(r"[0-9a-f]{64}")
_STAMP_KEYS = {"parent", "child", "phase", "of", "digest"}


def phase_stamp(parent, child, phase, count, plan_digest):
    """What a plan records on the job of a phase issue: which parent, which issue, which phase of how many, which plan."""
    return {"parent": parent, "child": child, "phase": phase, "of": count, "digest": plan_digest}


def check_phase_stamp(value):
    """A copy of a well-formed phase stamp, or ``ValueError`` (a fixed message).

    All numbers are real ints (``type()``, a bool is not a number), the phase is within the plan and the digest is a sha256.
    """
    if not isinstance(value, dict) or set(value) != _STAMP_KEYS:
        raise ValueError("invalid epic phase stamp")
    parent, child, phase, count, plan_digest = (value[key] for key in ("parent", "child", "phase", "of", "digest"))
    numbers = (parent, child, phase, count)
    if (
        any(type(number) is not int or number <= 0 for number in numbers)  # type(), not isinstance: a bool is not a number
        or parent == child
        or not phase <= count <= MAX_PHASES
        or not isinstance(plan_digest, str)
        or _HEX64.fullmatch(plan_digest) is None
    ):
        raise ValueError("invalid epic phase stamp")
    return dict(value)


def recorded_stamp(job):
    """The valid phase stamp recorded on a planned job, or None (no record, or not well formed)."""
    try:
        return check_phase_stamp(job.get("epic_phase")) if isinstance(job, dict) and "epic_phase" in job else None
    except ValueError:
        return None


STAMP_FILE = "epic-phase"  # the file a started phase's run directory holds (written beside the trigger-run marker, by the manager)
MAX_STAMP_TEXT = 70000  # title and body of a phase issue as recorded in the stamp; a phase issue's own text is a few hundred characters


def start_stamp(parent, child, phase, count, plan_digest, title, body):
    """What the manager records in the run directory of a phase it starts itself: the phase stamp plus the VERIFIED title and body.

    The title and body were hashed against the ledger right before the start (an edited phase issue is never started), so the planner's
    input for this issue comes from here and never from the issue's text or comments on GitHub (``planner.snapshot_issues``).
    """
    return {**phase_stamp(parent, child, phase, count, plan_digest), "title": title, "body": body}


def check_start_stamp(value):
    """A copy of a well-formed start stamp, or ``ValueError`` (a fixed message): the phase stamp's keys and two bounded strings."""
    if not isinstance(value, dict) or set(value) != _STAMP_KEYS | {"title", "body"}:
        raise ValueError("invalid epic start stamp")
    try:
        check_phase_stamp({key: value[key] for key in _STAMP_KEYS})
    except ValueError:
        raise ValueError("invalid epic start stamp") from None
    title, body = value["title"], value["body"]
    if not (isinstance(title, str) and isinstance(body, str) and title.strip() and 0 < len(title) <= 400 and len(body) <= MAX_STAMP_TEXT):
        raise ValueError("invalid epic start stamp")
    return dict(value)


@dataclass(frozen=True)
class PhaseLink:
    """The parent link of a phase issue's pull request. ``closes_parent`` is only ever True for the last phase, computed live."""

    parent: int
    child: int
    closes_parent: bool = False


def _applies(issues, link):
    """Whether the link belongs to this job: only when the job's issues are exactly the phase issue (never a grouped or appended one)."""
    return link is not None and list(issues) == [link.child]


def first_line(issues, closing, link=None):
    """The first line of a pull request body: the ONE place it is built (publish, completion, demotion).

    Without a ``link`` it is ``github.closing_line``: ``Closes #A, closes #B, refs #C``, ``closing(issue)`` saying whether
    GitHub may close that issue on merge. For the job of a phase issue (``link``, and ONLY when the job's issues are exactly
    ``[link.child]``) the child follows ``closing`` and the parent is mentioned in the same line: ``Closes #c, refs #P``, and
    ``Closes #c, closes #P`` only for the last phase whose link was computed live (``link.closes_parent``) and only when the
    child itself is closed by this pull request: a pull request that is not complete closes neither.
    """
    from .github import closing_line  # imported late: this module stays free of other Factory modules at import time

    if not _applies(issues, link):
        return closing_line(issues, closing)
    shut = bool(closing(link.child))
    wanted = {link.child: shut, link.parent: shut and link.closes_parent}
    return closing_line([link.child, link.parent], wanted.__getitem__)


def linked_forms(issues, link):
    """The first lines Factory itself writes for a linked job, whatever the live answer was: what a rewrite may recognise."""
    if not _applies(issues, link):
        return ()
    return (
        first_line(issues, lambda _: False, link),
        first_line(issues, lambda _: True, PhaseLink(link.parent, link.child, False)),
        first_line(issues, lambda _: True, PhaseLink(link.parent, link.child, True)),
    )


def demoted_first_line(first, link):
    """``first`` with the parent only referenced (``Closes #c, closes #P`` becomes ``Closes #c, refs #P``), or None when it is anything else."""
    closing = first_line([link.child], lambda _: True, PhaseLink(link.parent, link.child, True))
    if first != closing:
        return None
    return first_line([link.child], lambda _: True, PhaseLink(link.parent, link.child, False))


def bind_phases(plan, stamps):
    """Record the phase stamp on the job of each phase issue of a planner result, and block what cannot carry it (in place).

    ``stamps`` maps an issue number to its stamp, or to None for an issue that is a phase (or may be one) but has no readable
    marker: it is then planned as an ordinary job with no parent link. Whatever ``epic_phase`` the planner wrote is dropped:
    only Factory records it. A phase is planned and built on its own, so a job that groups a phase issue with other issues
    is not run: its issues and, one by one, the issues of every job that depends on it get a ``blocked`` disposition (as
    ``model_choice.assign`` does for issues that disagree about the model).
    """
    jobs = plan.get("jobs", [])
    for job in jobs:
        job.pop("epic_phase", None)
    dropped = {}
    for job in jobs:
        known = [number for number in job["issues"] if number in stamps]
        if not known:
            continue
        if len(job["issues"]) > 1:
            dropped[job["id"]] = GROUPED_REASON
        elif stamps[known[0]] is not None:
            job["epic_phase"] = dict(stamps[known[0]])
    grew = True
    while grew:
        grew = False
        for job in jobs:
            if job["id"] not in dropped and any(name in dropped for name in job.get("depends_on", [])):
                dropped[job["id"]] = DEPENDENCY_REASON
                grew = True
    blocked = {number: dropped[job["id"]] for job in jobs if job["id"] in dropped for number in job["issues"]}
    plan["jobs"] = [job for job in jobs if job["id"] not in dropped]
    plan["dispositions"] = [
        {"issue": item["issue"], "status": "blocked", "reason": blocked[item["issue"]]} if item.get("issue") in blocked else item
        for item in plan.get("dispositions", [])
    ]
    return plan


def planner_prompt(text, on):
    """The planner prompt with its epic block kept (markers removed) or cut, so that a repository
    without epic mode gets the prompt it always had."""
    start = text.find(BEGIN)
    stop = text.find(END)
    if start < 0 or stop < start:
        return text
    before, block, after = text[:start], text[start + len(BEGIN) : stop], text[stop + len(END) :]
    return "".join((before, block.strip("\n"), after) if on else (before.rstrip("\n"), after))

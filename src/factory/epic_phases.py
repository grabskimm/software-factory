"""Epic mode, phase delivery: after an approval Factory opens the phase issues one at a time and keeps a status comment.

An approved epic (the parent) is an open issue that carries ``factory:plan-approved`` and ``factory-approve-plan`` and whose
approval still stands (``epic_gate.standing``: the gate's own decision, asked again from GitHub on every pass; the label is only
how a parent is FOUND, never the authority). For such a parent this pass

* opens ONE phase issue per pass, never more than the plan has phases, phase 1 after the approval and phase k+1 only when phase k
  is MERGED: the phase issue is closed AND a pull request of this repository that GitHub lists as closing it is merged
  (``GithubReader.closing_pulls``, checked again with ``pull_state``). A phase issue only carries the origin label
  ``factory:epic-phase``. Factory never applies the trigger label to it: the manager starts the phase itself (slice 3 below; this pass
  only reports that a phase is startable) and a person with write access can still apply the trigger label to run one, as for any issue;
* keeps ONE status comment on the parent (second Factory comment, hidden marker ``<!-- factory-outcome:v1 issue=P kind=epic-status -->``,
  edited in place): a checklist, a state word, a fixed code and a hidden ledger. The plan comment is never touched (editing it would
  void the approval). The text is a pure function of the plan's size and the evidence: ints and fixed words, no model text.

Nothing is trusted that a person can forge or undo:

* A phase issue is Factory's only when its AUTHOR is Factory's own identity (by id) AND its first line is the marker
  ``<!-- factory-epic-phase:v1 parent=P phase=N of=M plan=<digest> -->`` of this parent and of the CURRENT plan (``epic.digest`` of the
  hidden payload). A plan that changes in any way has a new digest, so existing phase issues are old-generation and ignored.
* Children are found without trusting a label: the ledger's numbers first, united with a bounded listing of the issues Factory's own
  identity opened. Removing the origin label makes no child invisible and causes no second one. Two valid children for one phase,
  a deleted child, a search that ran out of pages, an unreadable search or an unknown identity create NOTHING (the first three are
  reported in the status comment, the page bound as ``listing-truncated``: the listing reads at most ``trigger.MAX_PHASE_PAGES`` pages).
* The ledger (phase to child number, plan digest, hash of the child's title and body at creation, closing pull request, merged,
  first sighting of a closed-unmerged state) is monotone: a merged phase is a durable fact (re-checked with ``pull_state``, never
  undone by a label or a deleted issue). A comment of anyone else with the marker is never read as the ledger (author id).
* A phase issue closed without a merged closing pull request is not a halt at first sight: GitHub needs a moment to link the pull
  request. It must stay so for ``GRACE_SECONDS`` after it was closed and on two reads (the first sighting is kept in the ledger).
  Missing or truncated evidence never halts. A halt is only REPORTED in the status comment; only a stall relabels the parent (slice 3).
* The only model-derived text is the plan's, already sanitised, sanitised again here (``issue_comments.sanitize_epic``) and put in
  ONE fenced block of the phase issue; the title is the sanitised phase title. Edits of a child are noticed by comparing the hash of
  its title and body with the ledger's, never by rendering it again.

``decide`` is the pure part, ``read_parent`` reads the facts, ``run_pass`` is what the manager calls (bounded: at most
``MAX_READS_PER_PASS`` parents read per pass, round robin, an approved parent with an open phase again after ``REREAD_SECONDS``).

Slice 2 adds the links between a phase's pull request and its issues. ``plan_stamps`` decides at plan time, from the issue itself (the
origin label or the marker as the first line, AND Factory's own identity as author, one read of the issue), which issues of a plan are
phases: the plan records a stamp on the job (``epic.bind_phases``) and a planner's ``epic`` for a phase is blocked (``epic.downgrade``).
``phase_link`` is what the runner's publish and the completion of an incomplete pull request ask: the parent link of that job, computed
live from the current state (``epic.first_line`` builds the line). Only the last phase can say ``Closes`` for the parent, and only when
the parent is open, not ignored, the approval still stands (``epic_gate.standing``), the phase issue is Factory's valid child of the
CURRENT plan and every earlier phase is merged (the same ``read_parent`` evidence as the pass above); anything else, or anything that
cannot be read, says ``Refs``. The pass also takes ``Closes`` back from an open pull request of a halted epic and of a plan that was
replaced (``_demote_all``), through ``set_pull_body``. It does the same for a parent that cannot be proven any more: ignored, without
the approval label, with an approval that does not stand, without a valid plan comment or carrying the origin label (``_withdraw``: the
pull requests of the unmerged phase issues of the ledger, which is read without ``standing``), and, through a sweep of the open phase
issues (``_sweep``), for a parent that lost ``factory:plan-approved`` and is therefore never listed. A read that fails or stops at a bound
demotes nothing. Nor is the open pull request of a phase issue that was DELETED found (GitHub lists the pull requests of an issue only
while it exists, ``_live_children``): it keeps ``closes #<parent>``, a case ``docs/operations.md`` lists as not covered. The pass writes
issues (phase issues, the status comment) and now also the first line of such a pull request body:
no new GitHub App permission (``issues: write`` and ``pull_requests: write`` already exist).

Slice 3 starts the phase itself. The manager, not a person's label, begins the build of the open phase issue (``prepare_start`` and
``operations._start_phase``): its authority is the approval, proven again from GitHub and the ledger at the moment of the start, never
the label and never a stamp. A start needs ALL of: the approval stands (``read_parent``), every earlier phase is merged, exactly one valid
open phase issue whose title and body still hash to the ledger's, no ``factory-ignore`` and no status label and no trigger label on it, no
pull request that closes it, no run that lists it, no durable ``started`` record and fewer than ``MAX_PLAN_ATTEMPTS`` recorded attempts, and
fewer open pull requests of Factory's own than ``epic_max_open_prs`` (a read that cannot be completed holds the start). The attempt is
written to the ledger BEFORE the start, so a crash can only cost an attempt, never a second run; one phase is started per repository per
pass. The run directory then holds a stamp (``epic.start_stamp``: parent, phase, plan digest and the VERIFIED title and body) beside the
trigger-run marker (written and removed together), and the planner reads that text, never the issue's text or comments
(``planner.snapshot_issues``). The plan's ``run`` starts later (the planner takes minutes, a pause or a restart can intervene), so the approval and the phase issue are proven again right before that paid build too (``may_build``, ``operations._epic_build_proven``), and a build that is refused is never picked up again. The same verdict (``operations._epic_verdict``) stands before every other start of that run by the manager: the restart's auto-resume and a dependency resume leave a run that cannot be proven as it is. A phase that was started and has no open or merged pull request and no run in progress on two reads (a
planner that did not build it, a job that failed, was blocked or stopped, or ran out of budget, three attempts that never planned) is
``stalled``: it is reported in the status comment and the parent moves from ``factory:plan-approved`` to ``factory:blocked`` through
``epic_gate.apply``; Factory never starts it again (``started`` is in the ledger, which outlives a purge). That relabel is the one new
writer of this slice; the labels, the status comment and the first line of a pull request body remain the only things the pass writes.
"""

import base64
import binascii
import calendar
import dataclasses
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import epic, epic_gate, issue_comments, trigger
from .github import refresh_notice
from .followup_reply import FollowupReplies
from .issue_labels import (
    EPIC_PHASE_LABEL_STYLE,
    IGNORE_LABEL,
    LABEL_BLOCKED,
    LABEL_EPIC_PHASE,
    LABEL_PLAN_APPROVED,
    STATUS_LABELS,
)

KIND_STATUS = "epic-status"
LEDGER_PREFIX = "<!-- factory-epic-ledger:v1 "
LEDGER_SUFFIX = " -->"
_LEDGER = re.compile(r"<!-- factory-epic-ledger:v1 ([A-Za-z0-9_-]{1,20000}) -->")
_HEX64 = re.compile(r"[0-9a-f]{64}")

MAX_PLAN_ATTEMPTS = 3  # plan starts recorded per phase issue before Factory reports it stalled
STALL_SECONDS = 120  # a stalled phase must be seen on two reads at least this far apart (the first sighting is in the ledger)
READY_SECONDS = 360.0  # a phase the pass found startable is believed this long; the start proves everything again anyway
GRACE_SECONDS = 600  # a closed phase issue without a merged closing pull request is not a halt before this long after it was closed
REREAD_SECONDS = 120.0  # an approved parent with a phase in progress is read again this often (a merge does not touch the parent)
MAX_READS_PER_PASS = 10  # approved parents read per pass, round robin
MAX_DROPS_PER_PASS = 30  # parents without a valid plan comment looked at per pass (one call each)
MAX_SWEPT_PER_PASS = 10  # open phase issues of a parent that is not listed, looked at per pass
MAX_SEEN = 4096
MAX_STATUS = 8000  # a status comment is a few lines per phase; anything longer is a bug, not a comment

STATE_PROGRESS = "in-progress"
STATE_DONE = "done"
STATE_HALTED = "halted"
STATE_STOPPED = "stopped"
STATE_STALLED = "stalled"
STATE_UNKNOWN = "unknown"  # the facts are incomplete: nothing is created and the status comment is left as it is
CODE_OPEN = "phase-open"
CODE_PENDING = "phase-pending"
CODE_CHECKING = "checking"
CODE_DONE = "all-merged"
CODE_CLOSED = "closed-unmerged"
CODE_DELETED = "child-deleted"
CODE_DUPLICATE = "duplicate-children"
CODE_INCOMPLETE = "incomplete"
CODE_RUNNING = "phase-running"
CODE_HELD = "start-held"  # the open pull request cap (or a read that could not finish) holds the start
CODE_STALLED = "phase-stalled"
CODE_ATTEMPTS = "plan-attempts"
CODE_TRUNCATED = "listing-truncated"  # the search for the next phase issue ran out of pages: its absence is not proven, so nothing is opened

_NEXT = {
    CODE_OPEN: "Factory starts the build of the open phase issue itself once every condition is proven again from GitHub: the approval still "
    "stands, the issue is unedited and not ignored, and fewer pull requests of Factory are open than the repository allows. It never applies "
    "the `{label}` label; a person with write access may apply that label to build the phase by hand instead. The next phase is opened "
    "after the pull request of this one is merged.",
    CODE_RUNNING: "Factory started the build of the open phase issue. The next phase is opened after its pull request is merged. Factory "
    "never merges anything.",
    CODE_HELD: "Factory holds the start of the open phase issue: as many pull requests of Factory are open as the repository allows "
    "(`epic_max_open_prs`), or they could not all be read. It looks again soon and starts the phase when fewer are open.",
    CODE_STALLED: "The build of a phase issue was started and there is no open or merged pull request and no run in progress for it, so "
    "Factory starts nothing more and moved the parent to `factory:blocked`. Factory never starts that phase again. A person looks at the "
    "phase issue, then applies the `{label}` label to the parent for a new plan, or builds the phase by hand.",
    CODE_ATTEMPTS: "Factory tried to plan the open phase issue several times and no run began, so it starts nothing more and moved the parent "
    "to `factory:blocked`. A person looks at the phase issue, then applies the `{label}` label to the parent for a new plan, or builds the "
    "phase by hand.",
    CODE_PENDING: "The next phase issue is being opened.",
    CODE_CHECKING: "A phase issue was closed. Factory is checking that a merged pull request closed it before it opens the next phase.",
    CODE_DONE: "Every phase is merged. Factory does not close the parent issue; a person does that.",
    CODE_CLOSED: "A phase issue was closed without a merged pull request, so Factory opens nothing more. A person "
    "reopens the phase issue to go on with it; Factory looks again within an hour. If it was closed on "
    "purpose, a person applies the `{label}` label to the parent for a new plan, or opens and builds the "
    "remaining phases by hand.",
    CODE_DELETED: "A phase issue no longer exists. Factory never opens it again and opens nothing more. GitHub no "
    "longer lists its open pull request, so Factory cannot rewrite that pull request's first line: a "
    "person looks at it. To resume, a person applies the `{label}` label to the parent for a new plan, or "
    "opens and builds the remaining phases by hand.",
    CODE_DUPLICATE: "More than one issue claims the same phase, so Factory opens nothing more. A person keeps one of them and deletes the "
    "first line (the hidden marker line) of the other, so that it no longer reads as a phase issue of this plan; an administrator can "
    "delete the other issue instead. Closing it does not help, because Factory counts closed phase issues too. Factory looks again "
    "within an hour.",
    CODE_TRUNCATED: "Factory could not prove that the next phase issue does not exist yet: it reads a limited number of "
    "the issues and pull requests it opened itself since the plan was last edited, and there are more "
    "than that. So it opens nothing more. To resume, a person applies the `{label}` label to the parent "
    "for a new plan, or opens and builds the remaining phases by hand.",
}
FOOTER = (
    "This comment was posted automatically and is edited in place. Factory opens one phase issue at a time, only after the pull "
    "request of the previous phase is merged, starts the build of the open one only while every condition is proven again, and never "
    "merges or closes anything."
)
_FORBIDDEN = re.compile(r"[@>\x00-\x08\x0b-\x1f\x7f]|https?:|www\.", re.I)


# --- the ledger: what the status comment remembers ------------------------------------------------------


def _positive(value):
    return type(value) is int and value > 0  # deliberately not isinstance: a bool (True is 1) is never a number here


def _entry(item):
    """One validated ledger entry ``{"child", "hash", "pr", "merged", "neg", "started", "plans", "stall"}`` or None.

    ``started`` (a run began for the phase issue: durable, it outlives the run directory), ``plans`` (plan starts recorded before they
    were made) and ``stall`` (first sighting of a stalled phase) are absent in a ledger written before the automatic start: they read as
    ``False``, ``0`` and ``None``.
    """
    if not isinstance(item, dict):
        return None
    child, content, pr, merged, neg = (item.get(key) for key in ("child", "hash", "pr", "merged", "neg"))
    started, plans, stall = item.get("started", False), item.get("plans", 0), item.get("stall")
    ok = (
        _positive(child)
        and isinstance(started, bool)
        and type(plans) is int  # type(), not isinstance: a bool is not a count
        and 0 <= plans <= MAX_PLAN_ATTEMPTS
        and (stall is None or (type(stall) is int and stall >= 0))  # type(), not isinstance: a bool is not a time
        and isinstance(content, str)
        and (content == "" or _HEX64.fullmatch(content) is not None)
        and (pr is None or _positive(pr))
        and isinstance(merged, bool)
        and (not merged or pr is not None)
        and (neg is None or (type(neg) is int and neg >= 0))  # type(), not isinstance: a bool is not a time
    )
    if not ok:
        return None
    return {"child": child, "hash": content, "pr": pr, "merged": merged, "neg": neg, "started": started, "plans": plans, "stall": stall}


def encode_ledger(parent, plan_digest, entries):
    """The hidden ledger line for a parent: ``{phase: entry}`` of the plan with this digest."""
    phases = [{"n": number, **entry} for number, entry in sorted(entries.items())]
    document = {"v": 1, "parent": parent, "digest": plan_digest, "phases": phases}
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"{LEDGER_PREFIX}{base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')}{LEDGER_SUFFIX}"


def parse_ledger(line):
    """``{"parent", "digest", "phases": {n: entry}}`` from a ledger line, or None for anything that does not parse and validate."""
    found = _LEDGER.fullmatch(line.strip()) if isinstance(line, str) else None
    if found is None:
        return None
    data = found.group(1)
    try:
        document = json.loads(base64.urlsafe_b64decode(f"{data}{'=' * (-len(data) % 4)}").decode("utf-8"))
        parent, plan_digest, items = document["parent"], document["digest"], document["phases"]
        if document["v"] != 1 or not _positive(parent) or not isinstance(plan_digest, str) or not isinstance(items, list):
            return None
        phases = {}
        for item in items:
            entry = _entry(item)
            number = item.get("n") if isinstance(item, dict) else None
            if entry is None or not _positive(number) or number > epic.MAX_PHASES or number in phases:
                return None
            phases[number] = entry
    except (binascii.Error, ValueError, KeyError, TypeError, RecursionError):
        return None
    return {"parent": parent, "digest": plan_digest, "phases": phases}


# --- the pure decision -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Child:
    """A valid phase issue of the current plan, as read now: its number, state, close time (epoch seconds) and content hash."""

    number: int
    state: str  # "open" or "closed"
    closed_at: int | None
    content: str


@dataclass(frozen=True)
class View:
    """What is known of one phase. ``evidence`` is about the phase being merged: ``merged`` (``pr`` is proven), ``none`` or ``unknown``."""

    children: tuple = ()
    gone: bool = False  # the ledger's child does not exist (or is not Factory's)
    evidence: str = "none"
    pr: int | None = None
    started: bool = False  # a run listed the open phase issue, or Factory's own status label is on it (the ledger's record is added in ``decide``)
    stalled: str = ""  # ``phase-stalled`` or ``plan-attempts`` when the phase issue shows no way to a pull request on this read


@dataclass(frozen=True)
class Row:
    phase: int
    kind: str  # merged, open, stalled, checking, halted, gone, duplicate or none
    child: object = 0  # a number, or a tuple of numbers for ``duplicate``
    pr: int | None = None
    edited: bool = False
    started: bool = False
    unrecorded: bool = False  # the ledger holds no hash of the issue's text: Factory cannot tell whether it was edited, so it never starts it


@dataclass(frozen=True)
class Outcome:
    entries: dict  # the new ledger: phase -> entry
    rows: tuple
    state: str
    code: str
    create: int | None  # the phase to open now, or None


def _kept(prev, started=False):
    """The fields of an entry that only grow: ``started`` and ``plans`` never go back, ``stall`` is the first sighting of a stalled phase."""
    return {
        "started": bool(prev and prev.get("started")) or started,
        "plans": prev.get("plans", 0) if prev else 0,
        "stall": prev.get("stall") if prev else None,
    }


def decide(count, previous, views, creatable, now, truncated=False, made=None):
    """The new ledger, the checklist rows, the state and the phase to open now. Pure; ``views`` run up to the first phase not merged.

    A phase issue's content hash is the one thing a person with triage rights could pick for Factory, so it is never taken from GitHub at first
    sight: it is the ledger's, or the one this process computed when it created the issue (``made``: ``{phase: (child, hash)}``), or empty.
    An empty hash is never started (``_startable``): it is reported and the ordinary label path is left.

    Phases go in order and the walk stops at the first one that is not merged: a phase is never opened, or looked at, before
    the one before it is merged. ``now`` is wall time (epoch seconds). ``creatable`` is False when the search for a phase
    issue could not be completed; ``truncated`` says that the cause was the page bound (the state is then ``unknown`` with
    the code ``listing-truncated``, which the status comment reports, instead of ``incomplete``, which it does not).
    """
    entries, rows = {}, []
    state, code, create = STATE_DONE, CODE_DONE, None
    for number in range(1, count + 1):
        view, prev = views.get(number) or View(), previous.get(number)
        kids = view.children
        if view.evidence == "merged" and view.pr is not None:
            child = kids[0].number if len(kids) == 1 else (prev["child"] if prev else 0)
            content = prev["hash"] if prev else (kids[0].content if len(kids) == 1 else "")
            if not _positive(child):
                state, code = STATE_UNKNOWN, CODE_INCOMPLETE
                rows.append(Row(number, "none"))
                break
            entries[number] = {"child": child, "hash": content, "pr": view.pr, "merged": True, "neg": None, **{**_kept(prev, True), "stall": None}}
            rows.append(Row(number, "merged", child, view.pr))
            continue
        if view.evidence == "unknown" and prev is not None and prev["merged"]:
            entries[number] = dict(prev)  # a claim that could not be checked now is kept, not unlocked and not forgotten
            rows.append(Row(number, "checking", prev["child"]))
            state, code = STATE_PROGRESS, CODE_CHECKING
            break
        if len(kids) > 1:
            rows.append(Row(number, "duplicate", tuple(sorted(kid.number for kid in kids))))
            state, code = STATE_STOPPED, CODE_DUPLICATE
            break
        if len(kids) == 1:
            kid = kids[0]
            wrote = (made or {}).get(number)
            kept = prev["hash"] if prev and prev["hash"] else (wrote[1] if wrote and wrote[0] == kid.number else "")
            edited = bool(kept) and kept != kid.content
            entry = {"child": kid.number, "hash": kept, "pr": None, "merged": False, "neg": None, **_kept(prev, view.started)}
            entries[number] = entry
            if kid.state == "open":
                state, code = _open_state(entry, view, now, rows, number, kid, edited)
                break
            first = prev["neg"] if prev else None
            if view.evidence == "unknown":
                rows.append(Row(number, "checking", kid.number, edited=edited))  # no definite answer: the first sighting is forgotten
                state, code = STATE_PROGRESS, CODE_CHECKING
            elif first is None:
                entry["neg"] = int(now)
                rows.append(Row(number, "checking", kid.number, edited=edited))
                state, code = STATE_PROGRESS, CODE_CHECKING
            elif kid.closed_at is not None and now - kid.closed_at >= GRACE_SECONDS and now > first:
                entry["neg"] = first
                rows.append(Row(number, "halted", kid.number, edited=edited))
                state, code = STATE_HALTED, CODE_CLOSED
            else:
                entry["neg"] = first
                rows.append(Row(number, "checking", kid.number, edited=edited))
                state, code = STATE_PROGRESS, CODE_CHECKING
            break
        if view.gone or prev is not None:
            if prev is not None:
                entries[number] = dict(prev)
            rows.append(Row(number, "gone", prev["child"] if prev else 0))
            state, code = STATE_HALTED, CODE_DELETED
            break
        rows.append(Row(number, "none"))
        if creatable:
            state, code, create = STATE_PROGRESS, CODE_PENDING, number
        else:
            state, code = STATE_UNKNOWN, (CODE_TRUNCATED if truncated else CODE_INCOMPLETE)
        break
    rows.extend(Row(number, "none") for number in range(len(rows) + 1, count + 1))
    return Outcome(entries, tuple(rows), state, code, create)


# --- the texts ------------------------------------------------------------------------------------------------


def _label(trigger_label):
    text = str(trigger_label)
    return text if issue_comments._LABEL_NAME.fullmatch(text) else issue_comments.DEFAULT_TRIGGER_LABEL


def render_child(parent, number, count, plan_digest, phase, trigger_label, slug=None, secrets=()):
    """``(title, body)`` of the phase issue for one phase of a validated plan, from a fixed template.

    The only model-derived text is the phase (title, objective, paths, acceptance), made safe by ``sanitize_epic`` and put in ONE
    fenced block; the title is the sanitised phase title. ``ValueError`` when the phase cannot be made safe.
    """
    clean = issue_comments.sanitize_epic(
        {"phases": [{**phase, "depends_on": []}], "questions": []}, refs=(parent,), slug=slug, secrets=secrets
    )
    if clean is None:
        raise ValueError("not a valid phase")
    item = clean["phases"][0]
    block = [
        f"Objective: {item['objective']}",
        f"Size: {item['cost']}",
        f"Touches: {', '.join(item['paths'])}",
        "Acceptance:",
        *[f"- {criterion}" for criterion in item["acceptance"]],
    ]
    lines = [
        epic.phase_marker(parent, number, count, plan_digest),
        f"**Factory: phase {number} of {count}** (part of #{parent})",
        "",
        f"Factory opened this issue for one phase of the plan a person approved on #{parent}. The description below comes from "
        "that plan, which a model wrote: nobody has checked it, and it may be wrong or incomplete.",
        "",
        *issue_comments._fenced(block),
        "",
        "**To run this phase**, Factory starts it itself once every check passes. A person with write access may instead apply the "
        f"`{_label(trigger_label)}` label to this issue to build it by hand; Factory never applies that label. Factory opens the next phase only "
        "after the pull request of this one is merged. Factory never merges or closes anything.",
        "",
        "This issue was opened automatically by Factory.",
    ]
    return f"Phase {number}/{count}: {item['title']}", "\n".join(lines)


def _open_state(entry, view, now, rows, number, kid, edited):
    """The row, state and code of an open phase issue, and its ``stall`` sighting (set on ``entry``): a stall needs two reads apart."""
    first = entry["stall"]
    entry["stall"] = (first if first is not None else int(now)) if view.stalled else None
    confirmed = bool(view.stalled) and first is not None and now - first >= STALL_SECONDS and now > first
    if confirmed:
        rows.append(Row(number, "stalled", kid.number, edited=edited, started=entry["started"]))
        return STATE_STALLED, view.stalled
    rows.append(Row(number, "open", kid.number, edited=edited, started=entry["started"], unrecorded=not entry["hash"]))
    return STATE_PROGRESS, (CODE_RUNNING if entry["started"] else CODE_OPEN)


def _row_text(row):
    where = f"Phase {row.phase}: "
    if row.kind == "merged":
        return f"- [x] {where}#{row.child}, pull request #{row.pr} merged"
    if row.kind == "open":
        return f"- [ ] {where}#{row.child} open{', build started' if row.started else ''}{', edited since Factory opened it' if row.edited else ''}{', its text was not recorded, so Factory will not start it' if row.unrecorded else ''}"
    if row.kind == "stalled":
        return f"- [ ] {where}#{row.child} stalled: started, but no pull request and no run in progress"
    if row.kind == "checking":
        return f"- [ ] {where}#{row.child} closed, checking for a merged pull request"
    if row.kind == "halted":
        return f"- [ ] {where}#{row.child} closed without a merged pull request"
    if row.kind == "gone":
        return f"- [ ] {where}#{row.child} no longer exists"
    if row.kind == "duplicate":
        return f"- [ ] {where}more than one issue claims it ({', '.join(f'#{n}' for n in row.child)})"
    return f"- [ ] {where}not opened yet"


def render_status(parent, plan_digest, outcome, trigger_label):
    """The body of the status comment: a pure function of the plan's size and the evidence (ints and fixed words only).

    ``ValueError`` for a body that breaks the rules (a mention, a link, a control character, or too long), so an edit of the
    template cannot reach GitHub unnoticed; the tests render every state.
    """
    head = [
        issue_comments.marker(parent, KIND_STATUS),
        encode_ledger(parent, plan_digest, outcome.entries),
    ]
    lines = [
        "**Factory: phase delivery**",
        "",
        f"State: `{outcome.state}` · Code: `{outcome.code}`",
        "",
        f"Phases ({len(outcome.rows)}), delivered one at a time in this order:",
        "",
        *[_row_text(row) for row in outcome.rows],
        "",
        f"**What happens next:** {_NEXT.get(outcome.code, '').format(label=_label(trigger_label))}".rstrip(),
        "",
        FOOTER,
    ]
    body = "\n".join([*head, *lines])
    if len(body) > MAX_STATUS or _FORBIDDEN.search("\n".join(lines)):
        raise ValueError("the status comment breaks the template rules")
    return body


# --- reading ----------------------------------------------------------------------------------------------------


class StatusFailed(Exception):
    """The status comment could not be written (the cause is the writer's ``last_error``). Carries no text."""


class PhaseState:
    """What one manager remembers of a repository between passes. Nothing here is needed to be right, only to be cheap."""

    def __init__(self):
        self.seen = {}  # parent -> ((updated_at, rescan bucket), when it was read, whether to look again soon)
        self.verified = set()  # (parent, digest, phase, pull request) proven merged in this process
        self.cursor = 0  # the last parent looked at: the next pass starts after it (round robin)
        self.parents = {}  # phase issue -> the parent its marker names (0: not Factory's, or no marker); a marker never changes
        self.swept = {}  # open phase issue -> ((updated_at, rescan bucket), when to try again or None) of the last sweep
        self.ready = {}  # parent -> when its open phase issue was last found startable (the start proves it again before it believes it)
        self.holds = {}  # parent -> when the open pull request cap last held its start (the status comment says so)
        self.made = {}  # (parent, digest, phase) -> (child, hash) of a phase issue this process created: what a later pass may trust if the ledger write failed

    def made_for(self, parent, plan_digest):
        return {phase: found for (owner, digest, phase), found in self.made.items() if owner == parent and digest == plan_digest}

    def record_made(self, parent, plan_digest, phase, child, content):
        if len(self.made) >= MAX_SEEN:
            self.made.clear()
        self.made[(parent, plan_digest, phase)] = (child, content)

    def remember(self, number, mark, now, watch):
        if len(self.seen) >= MAX_SEEN:
            self.seen.clear()
        self.seen[number] = (mark, now, watch)

    def parent_of(self, child, found):
        if len(self.parents) >= MAX_SEEN:
            self.parents.clear()
        self.parents[child] = found

    def sweep_remember(self, child, mark, retry):
        if len(self.swept) >= MAX_SEEN:
            self.swept.clear()
        self.swept[child] = (mark, retry)


@dataclass
class Reading:
    verdict: str  # "ok", "junk" (no valid Factory plan comment) or "off" (the approval does not stand)
    count: int = 0
    digest: str = ""
    plan: object = None
    status: object = None  # the trigger.StatusComment found, or None
    previous: dict = field(default_factory=dict)
    views: dict = field(default_factory=dict)
    creatable: bool = False
    truncated: bool = False  # the listing of Factory's own issues ended at the page bound (``creatable`` is then False)
    older: dict = field(default_factory=dict)  # the ledger of an EARLIER plan of this parent (another digest): phase -> entry


@dataclass(frozen=True)
class RunFacts:
    """What the run directories say about one phase issue (``operations._epic_runs``). The default is "no run known".

    ``found``: a plan of this repository lists the issue. ``live``: a run can still build or publish it (a job that is not over, a pull
    request, a person's pending publish, a plan being made). ``dead``: it ended without a pull request (the planner chose no job for it, or
    its job failed, was blocked, stopped or closed, or the run's budget was drained).
    """

    found: bool = False
    live: bool = False
    dead: bool = False


@dataclass(frozen=True)
class Start:
    """A phase the manager may begin now, everything proven again: the stamp (``epic.start_stamp``) goes to the run directory."""

    parent: int
    child: int
    phase: int
    count: int
    digest: str
    title: str
    body: str

    def stamp(self):
        return epic.start_stamp(self.parent, self.child, self.phase, self.count, self.digest, self.title, self.body)


def _epoch(stamp):
    """Epoch seconds of a GitHub timestamp (``2027-01-01T00:00:10Z``), or None."""
    text = stamp if isinstance(stamp, str) else ""
    if epic_gate._STAMP.fullmatch(text) is None:
        return None
    return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ"))


def _valid_children(found, parent, count, plan_digest):
    """``{phase: {number: PhaseIssue}}``: the listed issues that are Factory's own AND carry this parent's marker of the current plan."""
    by_phase = {}
    for row in found:
        parsed = epic.parse_phase_marker(row.line) if row.ours else None
        if parsed is not None and parsed[0] == parent and parsed[2] == count and parsed[3] == plan_digest:
            by_phase.setdefault(parsed[1], {})[row.number] = row
    return by_phase


def _merged(reader, state, key, pull):
    """Whether pull request ``pull`` is merged, checked with ``pull_state`` (cached once proven); raises on a failed read."""
    if (*key, pull) in state.verified:
        return True
    if reader.pull_state(pull) != "merged":
        return False
    state.verified.add((*key, pull))
    return True


def _closing_evidence(reader, state, key, child):
    """``(evidence, pull request)`` for a closed phase issue: ``merged`` (a closing pull request of this repository is merged), ``none`` or ``unknown``."""
    closing = reader.closing_pulls(child)
    here = str(reader.repo).casefold()
    for pull, where, status in closing.pulls:
        if status == "MERGED" and where == here and _merged(reader, state, key, pull):
            return "merged", pull
    return ("none" if closing.complete else "unknown"), None


def _factory_labelled(reader, child):
    """Whether one of the status labels on the phase issue was applied by Factory's own identity (a person's label proves nothing)."""
    _, names = reader.issue_state(child)
    wanted = [name for name in STATUS_LABELS if name in {item.strip().casefold() for item in names}]
    me = reader.creator_login().strip().casefold()
    if not wanted or not me:
        return False
    actors = reader.label_actors(child, wanted)
    return any(actor is not None and actor.kind == "Bot" and actor.login.casefold() == me for actor in actors.values())


def _progress(reader, runs, child, prev):
    """``(started, stalled)`` of the open phase issue ``child``. Raises on a failed read (the parent is then not decided).

    Started: the ledger says so, a run lists the issue, or Factory's own status label is on it. Stalled (code, or ""): it was started and
    no run can still bring a pull request (and GitHub lists no open or merged pull request that closes it), or three recorded plan
    starts never produced a run.
    """
    facts = runs(child) if runs is not None else RunFacts()
    started = bool(prev and prev["started"]) or facts.found
    if not started and prev is not None and prev["plans"] > 0:
        started = _factory_labelled(reader, child)
    if facts.live:
        return started, ""
    if started:
        closing = reader.closing_pulls(child)
        has_pull = any(status in ("OPEN", "MERGED") for _, _, status in closing.pulls)
        return True, (CODE_STALLED if closing.complete and not has_pull else "")
    attempts = prev is not None and prev["plans"] >= MAX_PLAN_ATTEMPTS and not facts.found
    return False, (CODE_ATTEMPTS if attempts else "")


def _view(reader, state, parent, plan_digest, number, prev, listed, runs=None):
    """The ``View`` of phase ``number``. Reads only what the phase needs: nothing for a merged phase that is still proven."""
    key = (parent, plan_digest, number)
    if prev is not None and prev["merged"] and _merged(reader, state, key, prev["pr"]):
        return View(evidence="merged", pr=prev["pr"])  # a claim that is not true (a forged ledger) falls through to the evidence
    children, gone = dict(listed), False
    if prev is not None and prev["child"] not in children:
        row = reader.phase_issue(prev["child"])
        if row is None or not row.ours:
            gone = True
        else:
            children[row.number] = row
    kids = tuple(
        Child(row.number, row.state, _epoch(row.closed_at), row.content) for _, row in sorted(children.items())
    )
    if len(kids) == 1 and kids[0].state == "closed":
        evidence, pull = _closing_evidence(reader, state, key, kids[0].number)
        return View(kids, gone, evidence, pull)
    if len(kids) == 1:
        started, stalled = _progress(reader, runs, kids[0].number, prev)
        return View(kids, gone, started=started, stalled=stalled)
    return View(kids, gone)


def read_parent(reader, issue, settings, state, runs=None):
    """The ``Reading`` of one approved parent (a ``trigger.IssueFacts``). Raises on a GitHub failure: nothing is decided then.

    ``epic.PlanSearchTruncated`` and ``epic_gate.Truncated`` mean a bound was reached: nothing is decided and the stall is reported.
    ``runs(child)`` (optional) answers ``RunFacts`` from the run directories, for the open phase issue's progress.
    """
    number = issue.number
    plan = reader.plan_comment(number)
    if plan is None or plan.plan is None:
        return Reading("junk")  # no valid Factory plan comment (or one for another issue): cheap to drop
    stands, complete = epic_gate.standing(reader, issue, settings, plan)
    if not complete:
        raise epic_gate.Truncated()
    if not stands:
        return Reading("off")
    count, plan_digest = len(plan.plan["phases"]), plan.digest
    status = reader.epic_status(number)
    ledger = parse_ledger(status.ledger) if status is not None else None
    mine = ledger is not None and ledger["parent"] == number
    previous = ledger["phases"] if mine and ledger["digest"] == plan_digest else {}
    older = ledger["phases"] if mine and ledger["digest"] != plan_digest else {}
    try:
        found, truncated = reader.phase_issues(number, since=plan.updated)
        creatable = not truncated
    except LookupError:  # the identity of Factory's own writes is unknown: nothing is searched and nothing is opened
        found, truncated, creatable = [], False, False
    listed = _valid_children(found, number, count, plan_digest)
    views = {}
    for phase in range(1, count + 1):
        views[phase] = _view(reader, state, number, plan_digest, phase, previous.get(phase), listed.get(phase, {}), runs)
        if views[phase].evidence != "merged":
            break
    return Reading("ok", count, plan_digest, plan, status, previous, views, creatable, truncated, older)


# --- writing --------------------------------------------------------------------------------------------------------


class EpicStatus(FollowupReplies):
    """Posts or edits Factory's one status comment per epic, best effort and fenced, as the reader's identity.

    The plumbing is ``FollowupReplies``' (it is built on ``IssueComments``: the marker search by author, the failure cooldown, the
    per-cycle cap, ``FACTORY_ISSUE_COMMENTS=off``); only the marker and the way it is driven differ.
    """

    def _marker(self, issue, kind):
        return issue_comments.marker(issue, kind)

    def write(self, parent, body):
        """Make the parent carry this body. Never raises: ``written``, ``unchanged``, ``off``, ``cap``, ``cooldown`` or ``failed``."""
        if type(parent) is not int or parent <= 0:  # type(), not isinstance: a bool is not an issue number
            return "off"
        with self.mutex:
            if not self.active:
                return self._refusal()
            self.last_error = None
            key = (parent, KIND_STATUS)
            # A long-lived process must not trust what it wrote earlier: a person may have deleted the comment since.
            self.ids.pop(key, None)
            self.done.pop(key, None)
            before = self.written
            if not self._attempt(self._write, parent, KIND_STATUS, body):
                return "failed"
            return "written" if self.written > before else "unchanged"


def _open_phase(reader, status, issue, reading, number, trigger_label):
    """Open phase ``number`` and return ``(child number, content hash)``, or None when a guard says not now.

    Right before the write: the writer may write, the parent is still open, not ignored and still carries both labels, and
    a fresh search by marker finds no issue for this phase (a crash after an earlier write, or a second manager, must not
    leave two). Raises on a GitHub failure.
    """
    parent = issue.number
    if not status.active:
        return None
    live_state, live_names = reader.issue_state(parent)
    live = {name.strip().casefold() for name in live_names}
    if live_state != "open" or IGNORE_LABEL in live or LABEL_PLAN_APPROVED not in live or epic.LABEL_APPROVE not in live:
        return None
    found, truncated = reader.phase_issues(parent, since=reading.plan.updated)
    if truncated or _valid_children(found, parent, reading.count, reading.digest).get(number):
        return None
    phase = reading.plan.plan["phases"][number - 1]
    title, body = render_child(
        parent, number, reading.count, reading.digest, phase, trigger_label,
        slug=str(reader.repo), secrets=issue_comments._secret_values(),
    )
    color, description = EPIC_PHASE_LABEL_STYLE
    reader.ensure_label(LABEL_EPIC_PHASE, color, description)
    return reader.create_issue(title, body, (LABEL_EPIC_PHASE,)), epic.content_hash(title, body)


def _round_robin(numbers, cursor):
    return [*(n for n in numbers if n > cursor), *(n for n in numbers if n <= cursor)]


def run_pass(
    reader, settings, status, state, now, decided, noted, failed, allowed=lambda: True,
    trigger_label=issue_comments.DEFAULT_TRIGGER_LABEL, runs=None,
):
    """One phase pass: every approved parent that is due is read once, bounded, and gets at most ONE new phase issue.

    ``decided(number, text)`` is told of each phase issue opened (a fixed line), ``noted(number, text)`` of a state a person has to
    look at (halted or stopped; the caller rate-limits it), ``failed(number, error)`` of each parent whose read or write failed
    (nothing is decided for it, the next pass looks again) and, with ``epic_gate.Truncated``, of each whose read stopped at a bound.
    ``now`` is wall time. A parent whose ``updated_at`` did not change is not read again within ``epic_gate.RESCAN_SECONDS``,
    except one with a phase in progress, which is read again after ``REREAD_SECONDS`` (a merge does not touch the parent).
    ``allowed()`` is asked once, only when some parent has to be read or swept. Returns how many full reads were made.
    ``runs(child)`` answers ``RunFacts`` from the run directories (see ``read_parent``). A parent whose open phase issue is startable is
    remembered in ``state.ready`` for the manager's start (``prepare_start``), which proves it again; this pass never starts anything.

    A pass makes at most ``MAX_READS_PER_PASS`` full reads and ``MAX_DROPS_PER_PASS`` cheap drops. A full read is counted
    before it is made, whatever its end: a read that raises, stops at a bound or finds that the approval does not stand
    costs calls too, so parents that keep failing cannot make a pass unbounded. They do not starve the others either: the
    cursor moves on with each parent looked at, so the next pass starts after the last one tried.

    A parent that cannot be proven now (``factory-ignore``, no approval label, an approval that does not stand, no valid plan
    comment, or the origin label forged onto it: such an issue is never read as a parent) is not settled, but a pull request that
    was published with ``Closes`` for it must not keep it: ``_withdraw`` takes it
    back (a cheap drop each; a failed call is tried again after ``REREAD_SECONDS``). A parent that lost ``factory:plan-approved``
    is not listed at all, so ``_sweep`` looks at the open phase issues instead (at most ``MAX_SWEPT_PER_PASS`` per pass).

    Nothing can be opened or written while the status writer may not write (it is the ledger), so the pass ends at once then, and
    ends when the writer reaches its per-pass cap (``FACTORY_ISSUE_COMMENTS_PER_CYCLE``): the parents not reached come first next pass.
    """
    if not status.active:
        return 0
    listed = {}
    for issue in reader.open_issues(LABEL_PLAN_APPROVED):
        listed.setdefault(issue.number, issue)
    for number in [key for key in state.seen if key not in listed]:
        del state.seen[number]  # closed, or no longer approved
    for table in (state.ready, state.holds):
        for number in [key for key in table if key not in listed]:
            del table[number]
    reads = drops = 0
    answer = []

    def permitted():
        if not answer:
            answer.append(bool(allowed()))
        return answer[0]

    for number in _round_robin(sorted(listed), state.cursor):
        issue = listed[number]
        mark = (issue.updated, int(now // epic_gate.RESCAN_SECONDS))
        known = state.seen.get(number)
        if known is not None and known[0] == mark and not (known[2] and now - known[1] >= REREAD_SECONDS):
            continue
        names = {name.strip().casefold() for name in issue.labels if isinstance(name, str)}
        if reads >= MAX_READS_PER_PASS or drops >= MAX_DROPS_PER_PASS or not status.active:
            break
        if not permitted():
            return 0
        if LABEL_EPIC_PHASE in names or IGNORE_LABEL in names or epic.LABEL_APPROVE not in names:
            # Never read or settled as a parent: the origin label makes it a phase (or a parent with a forged label), and an ignored one or
            # one without the approval label is no approved parent. In every case a pull request published earlier must not keep
            # ``Closes`` for it, and a label on a parent never decides that: the withdraw runs (a real phase issue has no status comment).
            drops += 1
            state.remember(number, mark, now, not _withdraw(reader, number, decided, failed))
            continue
        state.cursor = number
        reads += 1  # counted before the call, as the gate pass does: a read that raises or ends "off" cost calls too
        try:
            reading = read_parent(reader, issue, settings, state, runs)
        except (epic.PlanSearchTruncated, epic_gate.Truncated):
            failed(number, epic_gate.Truncated())
            state.remember(number, mark, now, False)
            continue
        except Exception as error:  # noqa: BLE001 - one parent never stops the others; the caller names the type only
            failed(number, error)
            continue
        if reading.verdict == "junk":
            reads, drops = reads - 1, drops + 1  # no valid plan comment: one cheap drop, not a full read
        if reading.verdict != "ok":
            state.ready.pop(number, None)
            state.remember(number, mark, now, not _withdraw(reader, number, decided, failed))  # no proof: take ``Closes`` back
            continue
        try:
            watch = _settle(reader, status, issue, reading, now, decided, noted, failed, trigger_label, state)
        except Exception as error:  # noqa: BLE001
            failed(number, error)
            continue
        if watch is not None:
            state.remember(number, mark, now, watch)
    _sweep(reader, state, listed, now, permitted, status, decided, failed)
    return reads


def _held(state, number, now):
    return number in state.holds and now - state.holds[number] <= READY_SECONDS


def _settle(reader, status, issue, reading, now, decided, noted, failed, trigger_label, state):
    """Decide, open the phase that is due and bring the status comment up to date. The parent's ``watch`` flag, or None to read it again at once."""
    number = issue.number
    made = state.made_for(number, reading.digest)
    outcome = decide(reading.count, reading.previous, reading.views, reading.creatable, now, reading.truncated, made)
    earlier = _live_children(reader, reading.older)  # raises on a failed read: the caller reports it and the next pass tries again
    if earlier and not _demote_all(reader, number, earlier, decided, failed):
        return None  # an earlier plan's pull request may still close the parent: the old ledger stays and the next pass tries again
    if outcome.create is not None:
        opened = _open_phase(reader, status, issue, reading, outcome.create, trigger_label)
        if opened is None:
            return None  # a guard said not now: nothing was written, look again next pass
        child, content = opened
        state.record_made(number, reading.digest, outcome.create, child, content)  # before the status write: a failed write must not lose it
        made = state.made_for(number, reading.digest)
        decided(number, f"phase={outcome.create} child={child} created")
        views = {**reading.views, outcome.create: View((Child(child, "open", None, content),))}
        outcome = decide(reading.count, reading.previous, views, False, now, made=made)
    if outcome.code == CODE_OPEN:
        state.ready[number] = now  # startable as far as this read shows: the manager's start proves it again
        if _held(state, number, now):
            outcome = dataclasses.replace(outcome, code=CODE_HELD)
    else:
        state.ready.pop(number, None)
    if outcome.state == STATE_UNKNOWN:
        failed(number, epic_gate.Truncated())  # the facts are incomplete: nothing is created
        if outcome.code != CODE_TRUNCATED:
            return False  # the comment stays as it is; only a search that ran out of pages is reported in it (below)
    body = render_status(number, reading.digest, outcome, trigger_label)
    if reading.status is None or reading.status.body != body:
        result = status.write(number, body)
        if result == "failed":
            failed(number, StatusFailed())
            return None
        if result not in ("written", "unchanged"):
            return None  # off, cap or cooldown: the same body is tried again next pass
    if outcome.state in (STATE_HALTED, STATE_STOPPED, STATE_STALLED):
        noted(number, f"state={outcome.state} code={outcome.code}")
    if outcome.state in (STATE_HALTED, STATE_STOPPED):
        _demote_all(reader, number, _halted_children(outcome), decided, failed)  # best effort: the status comment is written already
    if outcome.state == STATE_STALLED:
        _block_parent(reader, number, outcome, decided)  # raises on a failed write: the caller reports it and the next pass tries again
    return outcome.state == STATE_PROGRESS


def _block_parent(reader, parent, outcome, decided):
    """A stalled epic: ``factory:plan-approved`` becomes ``factory:blocked`` (``epic_gate.apply``), once the status comment says why.

    Looked at again right before the write, as the gate does: a parent that was closed, ignored or already moved is left alone. A person
    applies the trigger label for a new plan (``factory:blocked`` is in the retry set); Factory never starts the phase again.
    """
    live_state, names = reader.issue_state(parent)
    live = {name.strip().casefold() for name in names}
    if live_state != "open" or IGNORE_LABEL in live or LABEL_PLAN_APPROVED not in live:
        return
    epic_gate.apply(reader, parent, names, LABEL_BLOCKED)
    decided(parent, f"state={outcome.state} code={outcome.code} parent relabelled")


# --- slice 3: starting a phase ---------------------------------------------------------------------------------------


def _names(values):
    return {name.strip().casefold() for name in values if isinstance(name, str)}


def _child_blocks_start(names, trigger_label):
    """Whether labels on the phase issue keep the manager from starting it: ``factory-ignore``, the trigger label (a person's own request is
    the ordinary label path's, and two runs must not race) or any status label (a run, or a person, has been here)."""
    wanted = _label(trigger_label).strip().casefold()
    status = {name for name in names if name.startswith(trigger.STATUS_PREFIX) and name != LABEL_EPIC_PHASE}
    return IGNORE_LABEL in names or wanted in names or bool(status)


def _parent_standing(reader, parent):
    """The live label names of the parent when it is open, not ignored, not an origin-labelled issue and carries both labels, else None."""
    live_state, names = reader.issue_state(parent)
    live = _names(names)
    ok = live_state == "open" and not live & {IGNORE_LABEL, LABEL_EPIC_PHASE} and {LABEL_PLAN_APPROVED, epic.LABEL_APPROVE} <= live
    return names if ok else None


def _startable(reading, outcome):
    """``(phase, child, ledger entry)`` of the one phase the ledger and the evidence allow to start, or None (a fixed reason is not needed)."""
    row = next((item for item in outcome.rows if item.kind == "open"), None)
    if outcome.state != STATE_PROGRESS or outcome.code != CODE_OPEN or row is None or row.edited:
        return None
    prev, kids = reading.previous.get(row.phase), reading.views[row.phase].children
    if prev is None or len(kids) != 1 or prev["child"] != kids[0].number or not prev["hash"] or prev["hash"] != kids[0].content:
        return None
    if prev["started"] or prev["plans"] >= MAX_PLAN_ATTEMPTS:
        return None
    return row.phase, kids[0].number, prev


def _verified_text(reader, child, expected):
    """``(title, body)`` of the open phase issue read NOW, or None: Factory's own, open, and a title and body that still hash to the ledger's.

    The hash covers the whole body, the marker line (parent, phase, plan digest) included, so an issue that hashes to the ledger's is the
    issue Factory opened for this phase of this plan. An edit, however small, fails closed.
    """
    row = reader.phase_issue(child, text=True)
    if row is None or not row.ours or row.state != "open" or len(row.text) != 2:
        return None
    title, body = row.text
    if row.content != expected or epic.content_hash(title, body) != expected:
        return None
    return title, body


def prepare_start(reader, settings, status, state, now, parent, runs, open_prs, trigger_label=issue_comments.DEFAULT_TRIGGER_LABEL):
    """``(Start or None, held)``: everything proven again from GitHub, the attempt recorded in the ledger, for the open phase of ``parent``.

    This is the manager's start and its ONLY authority (the approval; a stamp or ``state.ready`` is never evidence). Nothing is believed
    from an earlier pass: the parent's labels, the approval (``read_parent``: ``standing``), the ledger, every earlier phase merged, the
    phase issue's labels, title and body (hashed against the ledger), its closing pull requests, the run directories (``runs``), the
    open pull request cap (``open_prs() -> (numbers, truncated)``, a read that fails or is cut off HOLDS) are read now. Then the attempt
    (``plans`` + 1) is written to the status comment, and only if that write succeeded is a ``Start`` returned: a crash after it costs an
    attempt, never a second run. ``held`` is True when only the cap kept the start (the status comment says so). Raises on a failed read.
    """
    if _parent_standing(reader, parent) is None:
        return None, False
    names = reader.issue_state(parent)[1]
    reading = read_parent(reader, trigger.IssueFacts(parent, tuple(names), ""), settings, state, runs)
    if reading.verdict != "ok":
        return None, False
    outcome = decide(reading.count, reading.previous, reading.views, False, now, reading.truncated)
    chosen = _startable(reading, outcome)
    if chosen is None:
        return None, False
    phase, child, prev = chosen
    verified = _verified_text(reader, child, prev["hash"])
    if verified is None or _child_blocks_start(_names(reader.issue_state(child)[1]), trigger_label):
        return None, False
    closing = reader.closing_pulls(child)
    facts = runs(child) if runs is not None else RunFacts()
    if closing.pulls or not closing.complete or facts.found or facts.live:
        return None, False
    try:
        numbers, truncated = open_prs()
    except Exception:  # noqa: BLE001 - an unknown identity or a failed read: the cap cannot be checked, so the start is held
        numbers, truncated = [], True
    if truncated or len(numbers) >= settings.max_open_prs:
        state.holds[parent] = now
        return None, True
    state.holds.pop(parent, None)
    entries = {**outcome.entries, phase: {**outcome.entries[phase], "plans": prev["plans"] + 1}}
    body = render_status(parent, reading.digest, dataclasses.replace(outcome, entries=entries), trigger_label)
    if status.write(parent, body) != "written":
        return None, False  # the attempt is not on record: nothing is started
    # Once more right before the start, as the gate does before its write: a parent or a phase issue that changed in the meantime wins.
    if _parent_standing(reader, parent) is None:
        return None, False
    live_state, live_names = reader.issue_state(child)
    if live_state != "open" or _child_blocks_start(_names(live_names), trigger_label):
        return None, False
    return Start(parent, child, phase, reading.count, reading.digest, verified[0], verified[1]), False


def in_flight(reader, settings, parent, now, runs=None):
    """Whether a phase of the approved epic ``parent`` is in flight (its build started, or a closed phase issue is being checked): a new plan waits.

    Asked before the trigger label on an approved parent is honoured (``operations._candidates``). Fails closed: anything that cannot be
    read is "in flight". A parent whose approval does not stand has nothing in flight.
    """
    try:
        names = reader.issue_state(parent)[1]
        reading = read_parent(reader, trigger.IssueFacts(parent, tuple(names), ""), settings, PhaseState(), runs)
        if reading.verdict != "ok":
            return False
        outcome = decide(reading.count, reading.previous, reading.views, False, now, reading.truncated)
    except Exception:  # noqa: BLE001 - see above
        return True
    return outcome.state == STATE_PROGRESS and outcome.code in (CODE_RUNNING, CODE_CHECKING)


def may_build(reader, settings, stamp):
    """Whether the build of a phase that the manager planned (``stamp``, read from its run directory) may start now: the approval, proven again.

    The plan was started under the approval, but the paid ``run`` starts later (a planner that takes many minutes, a pause, a backoff, a
    restart), so the standing is asked again from GitHub right before it, with the same checks as the start: epic mode on, the parent open,
    not ignored, not an origin-labelled issue, carrying both labels, its approval standing (``epic_gate.standing``) for the plan the stamp
    names, and the phase issue still Factory's own, open, not ignored, with this parent's and this plan's marker and no pull request that
    closes it. False when any of that is not so. Raises when a read fails or is cut off (``epic_gate.Truncated``): the caller holds the build for a failure that can pass and refuses it for a cut-off or a 404/410 (``operations._epic_build_proven``).
    (The open pull request cap is a condition of starting a phase, not of finishing one that was started under it.)
    """
    parent, child = stamp["parent"], stamp["child"]
    if not settings.enabled or _parent_standing(reader, parent) is None:
        return False
    names = reader.issue_state(parent)[1]
    plan = reader.plan_comment(parent)
    if plan is None or plan.plan is None or plan.digest != stamp["digest"] or len(plan.plan["phases"]) != stamp["of"]:
        return False
    stands, complete = epic_gate.standing(reader, trigger.IssueFacts(parent, tuple(names), ""), settings, plan)
    if not complete:
        raise epic_gate.Truncated()
    if not stands:
        return False
    row = reader.phase_issue(child)
    marker = epic.parse_phase_marker(row.line) if row is not None and row.ours else None
    if marker != (parent, stamp["phase"], stamp["of"], stamp["digest"]) or row.state != "open":
        return False
    if IGNORE_LABEL in _names(reader.issue_state(child)[1]):
        return False
    closing = reader.closing_pulls(child)
    if not closing.complete:
        raise epic_gate.Truncated()
    return not closing.pulls


def write_stamp(run_dir, start):
    """Write the stamp of a started phase into its run directory (a new file, owner only): the manager's side of ``run_stamp``."""
    descriptor = os.open(Path(run_dir) / epic.STAMP_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(json.dumps(start.stamp(), sort_keys=True).encode("utf-8"))


def run_stamp(directory):
    """The valid start stamp of a run directory, None when it has none, ``ValueError`` (a fixed message) when the file is not a valid stamp."""
    path = Path(directory) / epic.STAMP_FILE
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ValueError("invalid epic start stamp") from error
    try:
        return epic.check_start_stamp(json.loads(raw.decode("utf-8")))
    except (ValueError, RecursionError) as error:  # a JSON error and a bad encoding are ValueErrors
        raise ValueError("invalid epic start stamp") from error


# --- slice 2: the links between a phase's pull request and its issues --------------------------------------------------


def _may_be_phase(issue):
    """Whether a captured issue looks like a phase issue: the origin label, or the marker as the first line of its body."""
    labels = issue.get("labels")
    names = {label.get("name") if isinstance(label, dict) else label for label in (labels if isinstance(labels, list) else [])}
    folded = {name.strip().casefold() for name in names if isinstance(name, str)}
    body = issue.get("body")
    first = body.split("\n", 1)[0] if isinstance(body, str) else ""
    return LABEL_EPIC_PHASE in folded or first.lstrip().startswith(epic.PHASE_PREFIX)


def plan_stamps(reader, issues):
    """``{issue number: stamp or None}`` for the captured ``issues`` of a plan that are phase issues of an epic.

    An issue is a phase when the origin label is on it or its body starts with the phase marker, AND Factory's own identity opened
    it (the author's id, from one read of the issue itself: ``GithubReader.phase_issue``); a label or a marker of anyone else's
    makes nothing a phase. Only an issue that looks like a phase in the capture is read (no call for the others). The value is the
    stamp (``epic.phase_stamp``) when the marker is readable and valid, else None: a phase without a parent link. An issue whose
    read failed is in the result with None too: it is neither shown to be a phase nor shown not to be, so it is never linked and
    an ``epic`` for it is blocked (a forgery or a failure can only make Factory block an epic or omit a ``Closes``).
    """
    found = {}
    for issue in issues:
        number = issue.get("number") if isinstance(issue, dict) else None
        if type(number) is not int or number <= 0 or not _may_be_phase(issue):  # type(), not isinstance: a bool is not an issue number
            continue
        try:
            row = reader.phase_issue(number)
        except Exception:  # noqa: BLE001 - see above: the safe direction, and nothing of the error is kept
            found[number] = None
            continue
        if row is None or not row.ours:
            continue
        marker = epic.parse_phase_marker(row.line)
        labelled = LABEL_EPIC_PHASE in {name.strip().casefold() for name in row.labels}
        if marker is None and not labelled:
            continue
        try:
            found[number] = epic.check_phase_stamp(epic.phase_stamp(marker[0], number, marker[1], marker[2], marker[3])) if marker else None
        except ValueError:
            found[number] = None
    return found


def _closes_parent(reader, settings, stamp):
    """Whether a pull request of the LAST phase may say ``Closes`` for the parent now, from the current state. Raises on a failed read.

    The origin label on the parent says no, as it does for the pass (``run_pass`` never proves such a parent and takes ``Closes`` back).
    """
    parent, child, phase, count = stamp["parent"], stamp["child"], stamp["phase"], stamp["of"]
    live_state, names = reader.issue_state(parent)
    live = {name.strip().casefold() for name in names}
    if live_state != "open" or LABEL_EPIC_PHASE in live or IGNORE_LABEL in live or LABEL_PLAN_APPROVED not in live or epic.LABEL_APPROVE not in live:
        return False
    reading = read_parent(reader, trigger.IssueFacts(parent, tuple(names), ""), settings, PhaseState())
    if reading.verdict != "ok" or reading.digest != stamp["digest"] or reading.count != count:
        return False
    if any(reading.views.get(earlier) is None or reading.views[earlier].evidence != "merged" for earlier in range(1, phase)):
        return False
    view = reading.views.get(phase)
    # ``children`` are the valid ones of the current plan (Factory's own, with this parent's marker and the current digest)
    return view is not None and len(view.children) == 1 and view.children[0].number == child and view.children[0].state == "open"


def phase_link(job, reader, settings, active=True):
    """The ``epic.PhaseLink`` of a planned job, or None when the job is not exactly one phase issue.

    Non-last phases and every case that cannot be proven say ``Refs`` for the parent (``closes_parent`` False): the proof is made
    live, here, on every call, never taken from the stamp. ``active`` is False where epic mode or label triggers are off, and
    ``reader`` None where no GitHub client could be made: both are ``Refs``. This never raises.
    """
    stamp = epic.recorded_stamp(job)
    if stamp is None or list(job.get("issues", ())) != [stamp["child"]]:
        return None
    closes = False
    if active and reader is not None and stamp["phase"] == stamp["of"]:
        try:
            closes = _closes_parent(reader, settings, stamp)
        except Exception:  # noqa: BLE001 - unreadable evidence is ``Refs``, the safe direction; nothing of the error is kept
            closes = False
    return epic.PhaseLink(stamp["parent"], stamp["child"], closes)


def _halted_children(outcome):
    """The phase issues a halted or stopped epic leaves open to a pull request: every one the ledger knows that is not merged or gone.

    A gone one (``child-deleted``) is not asked about, as in ``_live_children``: its open pull request is not found and keeps its line.
    """
    gone = {row.phase for row in outcome.rows if row.kind == "gone"}
    numbers = [entry["child"] for phase, entry in sorted(outcome.entries.items()) if not entry["merged"] and phase not in gone]
    numbers.extend(number for row in outcome.rows if row.kind == "duplicate" for number in row.child)
    return list(dict.fromkeys(numbers))


def _live_children(reader, entries):
    """The phase issues of a ledger's ``entries`` (phase -> entry) whose pull requests the pass can ask GitHub about.

    An entry counts when it is not merged and its issue exists and is Factory's own (one read of the issue, ``phase_issue``). An
    issue that was deleted or transferred is left out because GitHub cannot list pull requests for it (the call fails for good) and
    asking would only wedge the caller: the new plan would never start. That is a limit and NOT a proof that nothing could close the
    parent through it. An open pull request whose first line still says ``Closes #<deleted>, closes #<parent>`` is not found through
    the deleted issue (this pass does not search for it), so it is not rewritten and closes the parent when it is merged, whether
    the epic is halted (``child-deleted``), its plan was replaced or the parent cannot be proven. ``docs/operations.md`` lists it as
    not covered: a person who deletes a phase issue also looks at its open pull request. An issue that is not Factory's is left
    out too: Factory publishes ``closes #<parent>`` only through its own phase issues. Raises on a failed read.
    """
    children = []
    for _, entry in sorted(entries.items()):
        row = None if entry["merged"] else reader.phase_issue(entry["child"])
        if row is not None and row.ours:
            children.append(entry["child"])
    return children


def _demote_pull(reader, number, link):
    """Rewrite the first line of an open pull request from ``Closes`` to ``Refs`` for the parent; True when it was rewritten."""
    facts = reader.pull_facts(number)
    if facts.state != "open":
        return False
    first, separator, rest = facts.body.partition("\n")
    carriage = "\r" if first.endswith("\r") else ""
    wanted = epic.demoted_first_line(first.removesuffix("\r"), link)
    if wanted is None:
        return False
    reader.set_pull_body(number, f"{wanted}{carriage}{separator}{refresh_notice(wanted, rest)}")
    return True


def _demote_child(reader, parent, child, decided):
    """Demote every open pull request of this repository that GitHub lists as closing the phase issue ``child``. Raises on a failed call."""
    here = str(reader.repo).casefold()
    link = epic.PhaseLink(parent, child)
    for number, where, status in reader.closing_pulls(child).pulls:
        if status == "OPEN" and where == here and _demote_pull(reader, number, link):
            decided(parent, f"pull={number} parent link demoted")


def _demote_all(reader, parent, children, decided, failed):
    """Take ``Closes`` for ``parent`` back from every open pull request of these phase issues. False when a GitHub call failed.

    The pull requests are the ones GitHub lists as closing the issue (open, of this repository); only a first line that is exactly
    what the last phase publishes (``Closes #c, closes #P``) is touched, through ``set_pull_body`` and nothing else of the body.
    """
    clean = True
    for child in children:
        try:
            _demote_child(reader, parent, child, decided)
        except Exception as error:  # noqa: BLE001 - reported by type; the other phase issues are still looked at
            failed(parent, error)
            clean = False
    return clean


def _withdraw(reader, parent, decided, failed):
    """Take ``Closes`` for ``parent`` back from the open pull requests of the unmerged phase issues its ledger knows. True when done.

    For a parent the pass cannot settle (ignored, no approval label, an approval that does not stand, no valid plan comment): the
    ledger is Factory's own status comment (found by marker and identity), readable without ``standing``. A phase issue that no
    longer exists, or is not Factory's, is left out (``_live_children``: the pull request of a deleted one is not found, so it
    keeps its line). False when a GitHub call failed: the caller looks again soon.
    """
    try:
        status = reader.epic_status(parent)
        ledger = parse_ledger(status.ledger) if status is not None else None
        if ledger is None or ledger["parent"] != parent:
            return True
        children = _live_children(reader, ledger["phases"])
    except Exception as error:  # noqa: BLE001 - reported by type; the next pass looks again
        failed(parent, error)
        return False
    return _demote_all(reader, parent, children, decided, failed)


def _orphan(reader, state, listed, child, decided):
    """One open phase issue: take ``Closes`` back for its parent when that parent is open and no longer carries ``factory:plan-approved``.

    False when there was nothing to look at (the issue is not Factory's, or its parent is one the pass above listed), True when the
    parent was looked at.
    """
    parent = state.parents.get(child)
    if parent is None:
        row = reader.phase_issue(child)
        marker = epic.parse_phase_marker(row.line) if row is not None and row.ours else None
        parent = marker[0] if marker is not None else 0  # a label on someone else's issue says nothing
        state.parent_of(child, parent)
    if parent == 0 or parent in listed:
        return False
    live_state, names = reader.issue_state(parent)
    if live_state == "open" and LABEL_PLAN_APPROVED not in {name.strip().casefold() for name in names}:
        _demote_child(reader, parent, child, decided)
    return True


def _sweep(reader, state, listed, now, permitted, status, decided, failed):
    """Take ``Closes`` back for a parent that lost ``factory:plan-approved``: the parent pass lists only parents that carry it.

    The open issues with the origin label are listed (one call; a label on someone else's issue is checked by author, see
    ``_orphan``). One that is not Factory's is never looked at again. One whose parent is listed costs no call once known and is looked at
    again the moment its parent drops out of the listing. The others are looked at, at most ``MAX_SWEPT_PER_PASS`` per pass, and then not again until
    the issue or the hour changes (a failed one after ``REREAD_SECONDS``). A closed parent needs nothing: the ``Closes`` of a pull
    request cannot close it again.
    """
    open_phases = reader.open_issues(LABEL_EPIC_PHASE)
    numbers = {facts.number for facts in open_phases}
    for child in [key for key in state.swept if key not in numbers]:
        del state.swept[child]
    left = MAX_SWEPT_PER_PASS
    for facts in sorted(open_phases, key=lambda item: item.number):
        child = facts.number
        parent = state.parents.get(child)
        if parent is not None and (parent == 0 or parent in listed):
            state.swept.pop(child, None)  # the pass above looks at it (or it is not Factory's): nothing remembered, nothing to ask
            continue
        mark = (facts.updated, int(now // epic_gate.RESCAN_SECONDS))
        known = state.swept.get(child)
        if known is not None and known[0] == mark and (known[1] is None or now < known[1]):
            continue
        if left <= 0 or not status.active or not permitted():
            return
        left -= 1
        try:
            looked = _orphan(reader, state, listed, child, decided)
        except Exception as error:  # noqa: BLE001 - one phase issue never stops the others; the caller names the type only
            failed(child, error)
            state.sweep_remember(child, mark, now + REREAD_SECONDS)
            continue
        if looked:
            state.sweep_remember(child, mark, None)

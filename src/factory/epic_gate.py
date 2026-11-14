"""Epic mode, the human gate: a pure state machine derived from GitHub, moved on by a read-only manager pass.

An issue the planner called an ``epic`` carries ONE status label that says where its plan stands:

* ``factory:needs-info``: the plan comment has numbered questions that nobody authorised has answered;
* ``factory:awaiting-approval``: no open questions are left, a person has to approve the plan;
* ``factory:plan-approved``: a person approved. The gate is done with it; ``epic_phases.py`` then opens the phase
  issues one at a time, and asks ``standing`` (below) on every pass whether the approval still stands.

``issue_labels.py`` sets the first label when the plan is made. From then on THIS pass owns the label, and
the run directory is not read at all: every fact comes from GitHub (the labels, Factory's own plan comment
and its hidden marker, the comments after it, the event that applied the approval label, the collaborator
permission), so the gate keeps working after the run is purged and after a manager restart.

Authority, fail closed. A person counts only when ``trigger.Actor.is_user`` (never a bot, Factory's own
comments included) AND their repository permission is at or above ``auto_build_min_permission`` (the rule
of the trigger label). Whatever is unclear is "no": a lookup that fails, an unknown or custom role, more
events or comments than the pages bound, a missing plan comment, a missing timestamp. A "no" that came from
a bound (not from a person's lack of access) is never silent: the pass reports it (``Truncated``).

Drive-by commenters cannot use up the permission lookups. The first ``MAX_REPLY_LOGINS`` distinct commenters
are looked up whoever they are; after that only commenters GitHub itself calls an owner, member or
collaborator (``author_association``, which nobody can set for themselves) are looked up, up to
``MAX_TRUSTED_REPLY_LOGINS``. The association never authorises anyone, only the permission lookup does.

* ``needs-info`` -> ``awaiting-approval``: an authorised person commented after the plan comment's last
  edit. The words of the comment are never read (the projection of ``GithubReader.issue_replies`` has no
  text), so a reply can only move the state one step and cannot approve anything.
* ``awaiting-approval`` -> ``plan-approved``: ``factory-approve-plan`` is on the issue NOW, its newest
  applier is authorised, and it was applied after the plan comment's last edit and, when the plan had
  questions, after the first authorised answer. An approval given earlier, or by someone who cannot approve
  (a triager can apply labels), changes nothing; so does a label that was removed after it was applied.
* ``factory-ignore`` and a closed issue win over everything: nothing is written.

``decide`` is the pure part, ``gather`` reads the facts, ``run_pass`` is what the manager calls. Like the rest of
this pass it writes only the status label (``apply``); ``standing`` writes nothing. ``apply`` is also the one writer ``epic_phases.py`` uses to
move the parent of a stalled epic from ``plan-approved`` to ``factory:blocked`` (slice 3: no new GitHub App permission).
"""

import re
from dataclasses import dataclass

from . import epic, trigger
from .issue_labels import (
    IGNORE_LABEL,
    LABEL_AWAITING_APPROVAL,
    LABEL_NEEDS_INFO,
    LABEL_PLAN_APPROVED,
    STATUS_LABELS,
)

NEEDS_INFO = LABEL_NEEDS_INFO
AWAITING_APPROVAL = LABEL_AWAITING_APPROVAL
PLAN_APPROVED = LABEL_PLAN_APPROVED
EPIC_STATUS_LABELS = (NEEDS_INFO, AWAITING_APPROVAL, PLAN_APPROVED)

MAX_PER_PASS = 20  # issues READ per pass; one whose updated_at did not change is skipped for free and does not count
RESCAN_SECONDS = 3600.0  # an issue nothing happened on is read again after this long anyway (permissions change)
MAX_SEEN = 4096

_STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


DEFAULT_MAX_OPEN_PRS = 3  # ``epic_max_open_prs``: Factory's open pull requests at which no further phase is started
MAX_OPEN_PRS = 50


@dataclass(frozen=True)
class EpicSettings:
    enabled: bool = True
    min_permission: str = "write"
    max_open_prs: int = DEFAULT_MAX_OPEN_PRS


def _max_open_prs(value):
    """``epic_max_open_prs`` as the pass reads it: absent is the default; anything outside the contract is the most restrictive, 1."""
    if value is None:
        return DEFAULT_MAX_OPEN_PRS
    return value if type(value) is int and 1 <= value <= MAX_OPEN_PRS else 1  # type(), not isinstance: a bool is not a count


def settings_of(cfg, env=None):
    """The gate settings of a repository config: epic mode on (``epic.enabled``) and the trigger label's permission.

    A permission outside the known levels is ``admin`` (``trigger.settings_of``), the safe side.
    """
    return EpicSettings(
        enabled=epic.enabled(cfg, env),
        min_permission=trigger.settings_of(cfg).min_permission,
        max_open_prs=_max_open_prs(getattr(cfg, "epic_max_open_prs", None)),
    )


def authorised(actor, permission, minimum):
    """Whether ``actor`` (a ``trigger.Actor`` or None) is a person whose ``permission`` reaches ``minimum``."""
    return actor is not None and actor.is_user and trigger.permission_ok(permission, minimum)


def state_of(names):
    """The gate state an issue's label names put it in: ``needs-info``, ``awaiting-approval`` or None.

    None also for an issue that is approved, or ignored, or in neither state: the pass has nothing to do.
    Both gate labels at once (a person's mistake) count as ``needs-info``, the lower state. A phase issue of an epic
    (``factory:epic-phase``) never has a gate: a phase is not split into further phases, so a gate label on one (put there by
    hand, or by a plan from before this rule) is refused here, whatever the labels say otherwise.
    """
    folded = {name.strip().casefold() for name in names if isinstance(name, str)}
    if PLAN_APPROVED in folded or IGNORE_LABEL in folded or epic.LABEL_PHASE in folded:
        return None
    if NEEDS_INFO in folded:
        return NEEDS_INFO
    return AWAITING_APPROVAL if AWAITING_APPROVAL in folded else None


@dataclass(frozen=True)
class Facts:
    """What ``decide`` needs. Every field defaults to the answer that changes nothing."""

    state: str | None = None  # NEEDS_INFO or AWAITING_APPROVAL, from the labels
    open: bool = True
    ignored: bool = False
    complete: bool = False  # every lookup this decision needs finished and was understood
    plan_found: bool = False  # Factory's own plan comment was found
    authorised_reply: bool = False  # an authorised person commented after the plan comment's last edit
    approval_present: bool = False  # the approval label is on the issue now
    approval_authorised: bool = False  # its newest applier is authorised
    approval_current: bool = False  # ... and applied after the plan (and, with questions, after the first answer)


def decide(facts):
    """The state the issue moves to (a gate label), or None to leave it as it is. Pure."""
    if facts.state not in (NEEDS_INFO, AWAITING_APPROVAL) or not facts.open or facts.ignored:
        return None
    if not (facts.complete and facts.plan_found):
        return None
    if facts.state == NEEDS_INFO:
        return AWAITING_APPROVAL if facts.authorised_reply else None
    approved = facts.approval_present and facts.approval_authorised and facts.approval_current
    return PLAN_APPROVED if approved else None


class Truncated(Exception):
    """The read of an issue stopped at a bound, so a "no" may hide an authorised answer. Carries no text."""


def _stamp(text):
    return text if isinstance(text, str) and _STAMP.fullmatch(text) else None


class _Permissions:
    """The permission of each login, asked once per read; a read asks for at most ``MAX_REPLY_LOGINS`` of them."""

    def __init__(self, reader):
        self.reader = reader
        self.known = {}

    def of(self, login):
        key = login.casefold()
        if key not in self.known:
            self.known[key] = self.reader.permission(login)
        return self.known[key]


def _first_authorised_reply(reader, number, plan, settings, permissions):
    """``(reply or None, complete)``: the oldest authorised comment after the plan comment's last edit.

    ``complete`` is False when the answer may be incomplete: more comments than the pages bound, or
    commenters that were not looked up (more than ``MAX_REPLY_LOGINS`` distinct ones, the later ones owners,
    members or collaborators only, up to ``MAX_TRUSTED_REPLY_LOGINS``) without an authorised one among those
    looked at. A reply found is final even then: a commenter skipped could only have been EARLIER, which
    moves the approval floor earlier, never later, so using the later one is the stricter reading.
    """
    replies, truncated = reader.issue_replies(number, plan.id, since=plan.updated)
    checked, skipped = set(), False
    for reply in replies:
        stamp = _stamp(reply.created)
        key = reply.login.casefold()
        if stamp is None or stamp <= plan.updated or key == plan.login.casefold() or key in checked:
            continue
        actor = trigger.Actor(reply.login, reply.type)
        if not actor.is_user or reply.is_bot:
            continue
        bound = trigger.MAX_TRUSTED_REPLY_LOGINS if reply.trusted else trigger.MAX_REPLY_LOGINS
        if len(checked) >= bound:
            skipped = True  # not looked up: a "no" at the end of the list is then unknown, not certain
            continue
        checked.add(key)
        if authorised(actor, permissions.of(reply.login), settings.min_permission):
            return reply, True
    return None, not (truncated or skipped)


def gather(reader, issue, settings, plan=None):
    """The ``Facts`` of one open issue (``trigger.IssueFacts``). Raises on a GitHub failure: the caller decides nothing then.

    ``plan`` is Factory's plan comment when the caller has read it already (``standing``); otherwise it is read here.
    """
    names = {name.strip().casefold() for name in issue.labels if isinstance(name, str)}
    state = state_of(names)
    if state is None:
        return Facts(state=None, ignored=IGNORE_LABEL in names, complete=True)
    approval = epic.LABEL_APPROVE in names
    if state == AWAITING_APPROVAL and not approval:
        return Facts(state=state, complete=True)  # nothing to decide: no GitHub call at all
    try:
        comment = plan if plan is not None else reader.plan_comment(issue.number)
    except epic.PlanSearchTruncated:
        return Facts(state=state, complete=False)  # the plan comment may be past the page bound: decide nothing, say so
    if comment is None:
        return Facts(state=state, complete=True)
    permissions = _Permissions(reader)
    questions = comment.questions != 0  # has questions, or the marker cannot be read: then an answer is required
    reply, complete = None, True
    if state == NEEDS_INFO or questions:
        reply, complete = _first_authorised_reply(reader, issue.number, comment, settings, permissions)
    if state == NEEDS_INFO:
        return Facts(state=state, complete=complete, plan_found=True, authorised_reply=reply is not None)
    actor = reader.label_actor(issue.number, epic.LABEL_APPROVE)  # None too when the events overflow the bound
    who = actor is not None and actor.is_user
    who = who and authorised(actor, permissions.of(actor.login), settings.min_permission)
    # The approval has to be newer than what it approves: the plan comment's last edit and, when the plan
    # had questions, the first authorised answer. Anything that cannot be dated is not current.
    floors = [_stamp(comment.updated)]
    if questions:
        floors.append(_stamp(reply.created) if reply is not None else None)
    applied = _stamp(actor.created) if actor is not None else None
    current = None not in floors and applied is not None and applied > max(floors)
    return Facts(
        state=state,
        complete=complete,
        plan_found=True,
        approval_present=True,
        approval_authorised=bool(who),
        approval_current=current,
    )


def standing(reader, issue, settings, plan):
    """``(stands, complete)``: whether the approval of an issue that carries ``factory:plan-approved`` still stands.

    It is the gate's own decision, asked again from the facts GitHub shows now: the facts are gathered as if the issue
    still waited for approval, and the approval stands only when ``decide`` would move it to ``plan-approved`` and
    every lookup finished. So the label alone is never the authority (a triager can apply it by hand, an approval
    given before the plan's last edit or before the questions were answered does not count, a label that was
    removed does not count). The caller has checked the labels and that the issue is open and not ignored.
    ``plan`` is Factory's plan comment of the issue (a ``trigger.PlanComment``). Raises on a GitHub failure.
    """
    forced = trigger.IssueFacts(issue.number, (AWAITING_APPROVAL, epic.LABEL_APPROVE), issue.updated)
    facts = gather(reader, forced, settings, plan)
    return facts.complete and facts.plan_found and decide(facts) == PLAN_APPROVED, facts.complete


def apply(reader, number, names, target):
    """Move an issue to the ``target`` gate label: the new label first, then the other gate labels off."""
    color, description = STATUS_LABELS[target]
    reader.ensure_label(target, color, description)
    reader.add_label(number, target)
    folded = {name.strip().casefold() for name in names if isinstance(name, str)}
    for stale in EPIC_STATUS_LABELS:
        if stale != target and stale in folded:
            reader.remove_label(number, stale)


def run_pass(reader, settings, decided, failed, seen, now, allowed=lambda: True):
    """One gate pass: every open issue that carries a gate label is judged once, bounded.

    ``decided(number, label)`` is told of each move, ``failed(number, error)`` of each issue whose read or
    write failed (nothing is decided for it, the next pass looks again) and, with ``Truncated``, of each
    issue whose read stopped at a bound (nothing is decided, it is read again when it changes or hourly). ``seen`` maps an issue number to
    ``(updated_at, rescan bucket)``: an unchanged issue is not read again within ``RESCAN_SECONDS``.
    ``allowed()`` is asked once, only when some issue has to be read (the repository may rule the pass out).
    Returns how many issues were read.
    """
    listed = {}
    for label in (NEEDS_INFO, AWAITING_APPROVAL):
        for issue in reader.open_issues(label):
            listed.setdefault(issue.number, issue)
    for number in [key for key in seen if key not in listed]:
        del seen[number]  # closed, or no longer in a gate state
    read = 0
    for number in sorted(listed):
        issue = listed[number]
        mark = (issue.updated, int(now // RESCAN_SECONDS))
        if seen.get(number) == mark:
            continue
        if read >= MAX_PER_PASS:
            break
        if read == 0 and not allowed():
            return 0
        read += 1
        try:
            facts = gather(reader, issue, settings)
            target = decide(facts)
            if target is not None:
                # A read can take many calls: look at the issue once more right before the write, so that a
                # `factory-ignore`, a close or a changed gate label in that window wins over the stale decision.
                live_state, live_names = reader.issue_state(number)
                live = {name.strip().casefold() for name in live_names}
                if live_state != "open" or IGNORE_LABEL in live or state_of(live) != facts.state:
                    continue  # nothing is written; the issue is read again when it changes
                apply(reader, number, live_names, target)
                decided(number, target)
            elif not facts.complete:
                failed(number, Truncated())  # nothing is decided, but the stall is visible; the issue is not read again at once
        except Exception as error:  # noqa: BLE001 - one issue never stops the others; the caller names the type only
            failed(number, error)
            continue
        if len(seen) >= MAX_SEEN:
            seen.clear()
        seen[number] = mark
    return read

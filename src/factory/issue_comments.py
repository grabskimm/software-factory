"""One best-effort comment on an issue that reached an outcome a human has to act on.

When Factory will not build an issue (the plan disposition is ``blocked`` or ``investigation``)
or a job that owns it ends ``failed`` or ``blocked``, the only other trace is a
``factory:blocked`` label and a ``plan.json`` inside the pod. ``IssueComments`` posts ONE comment
from the Factory App saying what happened, why, what would unblock it and which run it was, from a
fixed template. It is the only code that writes an outcome comment (the one other comment is the status comment of an approved
epic, ``epic_phases.EpicStatus``, built on it with its own fixed template); nothing else about an issue changes
(no close, no title or body edit, and ``issue_labels.py`` still owns the labels).

* **Same machinery as the labels.** ``IssueLabels`` (``issue_labels.py``) calls this from the
  persisted-state hooks it already has (``sync_plan``, ``reconcile``, ``on_job``), through its fenced
  ``gh``. It is best effort and never a control of a job: every failure (403, 404, a timeout, a
  malformed answer) is swallowed after ONE fixed-format line on stderr per process,
  ``factory: issue comment failed (<exception class>)``: no issue text, no response body, no token.
  After three consecutive failures it rests for five minutes, so an unreachable GitHub cannot add a
  timeout per issue to a run. A lost repository lock stops it for good.
* **Idempotent.** The comment starts with a hidden marker naming the issue and the outcome kind
  (``blocked``, ``needs_decision``, ``failed``). Before writing, the issue's comments are searched for
  a comment of Factory's own identity (the App's bot user, or the ambient ``gh`` login; never
  another bot's) that starts with that exact marker: found with the
  same text, nothing is posted; found with another text, that comment is edited; not found, one is
  posted. So an unchanged outcome posts nothing, a changed reason edits the same comment, and a
  restart (a new process with the same persisted state) never duplicates it. A process also
  remembers what it wrote, so an unchanged outcome costs no ``gh`` call at all after the first.
* **Switches and limits.** ``comment_on_blocked = false`` in a repository's config, or the
  deployment-wide ``FACTORY_ISSUE_COMMENTS=off`` in the environment, suppress every comment. At most
  ``FACTORY_ISSUE_COMMENTS_PER_CYCLE`` comments are written (posted or edited) per supervisor
  invocation (default 5); the rest wait for the next ``run``'s reconcile. An issue labelled
  ``factory-ignore`` is never commented on.
* **A plan hold** (``plan_holds``) has its own fixed body, ``render_held`` (the marker ``kind=plan_held``, a code of
  ``plan_failure.CODES`` and fixed text, no reason at all), written by ``hold_comments.HoldComments`` with the same
  idempotency, switches and limits.
* **The reason is model output derived from untrusted issue text.** It is rendered inside ONE fenced
  code block (nothing renders, links or notifies inside one), and ``sanitize_reason`` also caps it,
  removes control characters, raw HTML, links and ``@mentions``, keeps only ``#N`` references to
  issues Factory itself knows (the plan's), redacts secret and environment-value patterns and paths
  outside the repository, and returns None when in doubt, in which case the comment carries only the
  template and the fixed disposition code. See ``docs/operations.md`` ("Comments on issues") for the
  threat model.
"""

import hashlib
import html
import json
import os
import re
import sys
import threading
import time
import unicodedata
from pathlib import Path

from . import epic, plan_failure
from .github import repo_path
from .locks import LockError
from .session_timeout import CONFIG_KEY as TIMEOUT_KEY
from .session_timeout import seconds_of as timeout_seconds
from .shell import command, get_auth

KIND_BLOCKED = "blocked"
KIND_NEEDS_DECISION = "needs_decision"
KIND_FAILED = "failed"
KIND_NEEDS_PUBLISH = "needs_human_publish"
KIND_EPIC = epic.KIND
KIND_HELD = "plan_held"  # a plan hold started (``plan_holds``): not a key of KINDS, it has its own fixed body (``render_held``)

# kind -> (headline, what would unblock it). Fixed text: nothing a model wrote goes in here.
KINDS = {
    KIND_BLOCKED: (
        "blocked",
        "Resolve what the reason names (for example merge the issue or pull request it depends on, "
        "or split the request into smaller issues), or edit the issue so that it can be built as "
        "one bounded change.",
    ),
    KIND_NEEDS_DECISION: (
        "needs a decision",
        "Answer the question(s) above in a comment on this issue, or edit the issue to settle "
        "them. Then a maintainer can ask Factory to plan the issue again.",
    ),
    KIND_FAILED: (
        "failed",
        "An operator should look at the run (`factory status`) and decide whether to retry it; "
        "the issue may need a change if the cause lies in what it asks for.",
    ),
    KIND_EPIC: (
        "needs a phased plan",
        "A person with write access answers the numbered questions (if any), then applies the "
        f"`{epic.LABEL_APPROVE}` label to approve the plan, or re-applies the trigger label to have the issue "
        "planned again. After the approval Factory opens the phases as issues, one at a time; a person applies the "
        "trigger label to each to have it built.",
    ),
    KIND_NEEDS_PUBLISH: (
        "built and checked, but a person has to publish it",
        "The change passed review and verification, but the Factory App is not allowed to push "
        "workflow files, so its branch is kept in the Factory run. A person who may change "
        "workflow files first reads the diff of every workflow file in it (pushing the branch "
        "runs its on-push workflows with repository secrets), then pushes that branch with "
        "their own credentials from the Factory clone, never from the retained worktree. The "
        "operator then runs `factory resume` for the job with `--phase publish`: Factory opens "
        "a draft pull request that cannot merge until a person applies the `factory-verified` "
        "label. Factory is never given the `workflows` permission.",
    ),
}
# What would unblock a job that failed because an agent session ran out of time (``session_timeout``).
TIMEOUT_UNBLOCK = (
    f"The agent session ran out of time. If the work is one coherent change, raise `{TIMEOUT_KEY}` in "
    "this repository's Factory configuration (60 to 86400 seconds, default 1800) and ask for another "
    "attempt; if it is really several changes, split the issue into smaller issues that each fit in one session."
)
CAUSE_TIMEOUT = "timeout"
# The only codes a comment may print: plan dispositions and job statuses, never free text.
CODES = {"blocked", "investigation", "failed", "needs_human_publish", "epic"}

DISPOSITION_KINDS = {"blocked": KIND_BLOCKED, "investigation": KIND_NEEDS_DECISION, "epic": KIND_EPIC}
# ``interrupted`` is deliberately absent: every restart or deploy marks the running jobs
# ``interrupted`` and they are resumed seconds later (auto resume, or a person), so a public
# "failed" comment would be posted on every in-flight issue and never retracted. The job's
# label already says it needs attention; a job that ends ``failed`` or ``blocked`` comments. ``stopped`` is
# absent for the same reason in the other direction: an operator did it on purpose and knows why.
JOB_STATUS_KINDS = {
    "blocked": KIND_BLOCKED,
    "failed": KIND_FAILED,
    "needs_human_publish": KIND_NEEDS_PUBLISH,
}

ENV_SWITCH = "FACTORY_ISSUE_COMMENTS"
ENV_CAP = "FACTORY_ISSUE_COMMENTS_PER_CYCLE"
DEFAULT_CAP = 5
MAX_CAP = 100
_OFF = {"0", "off", "false", "no", "disabled"}

DEFAULT_TRIGGER_LABEL = "factory-build"

# Why a job is blocked on a dependency. The text of the first is fixed: the trigger loop writes it when it
# blocks a queued job (``Operations._block``) and reads it back to hand a retry to a fresh plan.
CLOSED_DEPENDENCY_REASON = "dependency {} was closed without merging"
_CLOSED_DEPENDENCY = re.compile(r"dependency ([A-Za-z0-9][A-Za-z0-9_-]{0,63}) was closed without merging")
CAUSE_DEPENDENCY_CLOSED = "dependency-closed"


def closed_dependency(reason):
    """The id of the dependency a blocked job's reason says was closed without merging, else None."""
    found = _CLOSED_DEPENDENCY.fullmatch(reason.strip()) if isinstance(reason, str) else None
    return found.group(1) if found else None


def block_cause(reason):
    """``dependency-closed`` or None (any other cause) for the RAW reason of a blocked outcome.

    Only the fixed sentence ``Operations._block`` writes is recognised. A still-open dependency is not told
    apart from free text on purpose: the job resumes by itself once it has merged, and a heuristic over text
    a model wrote would put the wrong advice under an ordinary failure.
    """
    return CAUSE_DEPENDENCY_CLOSED if closed_dependency(reason) else None


def failure_cause(reason):
    """``timeout`` for the RAW reason of a failed outcome that is exactly the fixed timeout reason, else None."""
    return CAUSE_TIMEOUT if timeout_seconds(reason) is not None else None


CALL_TIMEOUT = 20  # seconds per gh call
MAX_CONSECUTIVE_FAILURES = 3
COOLDOWN_SECONDS = 300

REASON_LIMIT = 900
QUESTION_LIMIT = 300
MAX_QUESTIONS = 5
MAX_INPUT = 20_000
MIN_KEPT = 12  # fewer visible characters than this after cleaning: in doubt
MAX_PLACEHOLDERS = 3  # more redactions than this: in doubt

_REDACTED = "[redacted]"
_LINK = "[link removed]"
_PATH = "[path removed]"
_PLACEHOLDERS = (_REDACTED, _LINK, _PATH)
_AT = "＠"  # fullwidth @: reads the same, pings nobody
_HASH = "＃"
_ELLIPSIS = "…"

_COMMENT_OPEN = re.compile(r"<!--.*?(?:-->|$)", re.S)
_TAG = re.compile(r"</?[A-Za-z!?][^>]*(?:>|$)")
_SECRETS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{16,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    # Both name parts are bounded: an unbounded ``[\w-]*`` before the keyword made this pattern
    # quadratic (seconds on 20,000 characters of "a-a-a-...").
    # No leading \b: the keyword matches inside a long name too (a name part of 41 or more
    # characters made the bounded form skip the value, which was then posted in clear). The
    # name key may be quoted ("api_key": "...") and the value may be a quoted string with spaces.
    re.compile(
        r"(?i)[\w-]{0,40}(?:token|secret|passw(?:or)?d|api[_-]?key|private[_-]?key|credential)"
        r"[\w-]{0,40}[\"']?\s*[=:]\s*(?:\"[^\"\n]{0,200}\"|'[^'\n]{0,200}'|\S+)"
    ),
    re.compile(r"\b[A-Z_][A-Z0-9_]{2,}=\S*"),
    re.compile(
        r"(?<![A-Za-z0-9+/_-])(?=[A-Za-z0-9+/_-]*[A-Za-z])(?=[A-Za-z0-9+/_-]*\d)"
        r"[A-Za-z0-9+/_-]{32,}"
    ),
]
_SECRET_NAME = re.compile(r"(?i)token|secret|passw|api_?key|private_?key|credential")
_MD_LINK = re.compile(r"!?\[([^\]]{0,300})\]\([^)]{0,2000}\)")
# No leading word boundary: "_http://x" and "1http://x" are autolinked by GitHub too. The scheme
# length is bounded so the scan stays linear.
_URL = re.compile(
    r"(?i)(?:[a-z][a-z0-9+.-]{0,30}://|www\.|(?:mailto|javascript|data|vbscript|file):)\S*"
)
_PATH_OUTSIDE = re.compile(
    r"(?<![\w./:-])(?:~/|(?:\.\./)+|/(?:home|root|data|etc|tmp|var|proc|run|usr|opt|mnt|srv|sys|dev"
    r"|workspace|app)\b)\S*"
)
_MENTION = re.compile(r"@(?=[A-Za-z0-9_])")
_REF = re.compile(r"(?<![\w&])#(\d{1,10})\b")
_GH_NUMBER = re.compile(r"(?i)\bGH-(?=\d)")
_SENTENCE_END = re.compile(r"(?<=[.?!])\s+")
_RUN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,49}")
_LABEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.:-]{0,49}")


def _secret_values():
    """Literal secret values of this process (sensitive-looking environment, the GitHub token)."""
    found = {
        value
        for name, value in os.environ.items()
        if _SECRET_NAME.search(name) and len(value) >= 8
    }
    try:
        auth = get_auth()
        if auth is not None:
            found.update(
                value
                for name, value in auth.env_for_supervisor_subprocess().items()
                if _SECRET_NAME.search(name) and isinstance(value, str) and len(value) >= 8
            )
    except Exception:  # noqa: BLE001, S110
        pass
    return sorted(found, key=len, reverse=True)


MAX_UNESCAPE = 5
_LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}(?:\[bot\])?")
_ENTITY = re.compile(r"&(?=#|[A-Za-z][A-Za-z0-9]*;)")


def _clean_characters(text):
    out = []
    for char in text:
        category = unicodedata.category(char)
        if char in "\n\r\t" or category in ("Zl", "Zp", "Zs"):
            out.append(" ")
        elif category[0] != "C":
            out.append(char)
    return "".join(out)


def sanitize_reason(text, refs=(), slug=None, limit=REASON_LIMIT, secrets=(), min_kept=MIN_KEPT):
    """The text made safe to post, or None when it cannot be trusted at all.

    ``refs`` are the issue and pull request numbers Factory itself knows: only a ``#N`` for one
    of them stays a reference (and a link to one of them in ``slug`` becomes that ``#N``).
    ``secrets`` are literal values to redact. None means "post only the template and the code".
    ``min_kept`` is how many visible characters must survive the cleaning (short fields such as a phase
    title or a path pass a smaller one).
    """
    if not isinstance(text, str):
        return None
    text = text[:MAX_INPUT]
    try:
        for _ in range(MAX_UNESCAPE):  # "&amp;#64;" must not survive as a working entity
            decoded = html.unescape(text)
            if decoded == text:
                break
            text = decoded
        else:
            return None  # still decoding after every pass: not an explanation
    except ValueError:
        return None  # an entity with thousands of digits raises: not an explanation either
    original = len(text)
    if not original:
        return None
    text = _clean_characters(text)
    before = len(text)
    text = _COMMENT_OPEN.sub(" ", text)
    text = _TAG.sub(" ", text)
    if (before - len(text)) * 2 > before:
        return None  # mostly markup: not an explanation
    for value in secrets:
        text = text.replace(value, _REDACTED)
    for pattern in _SECRETS:
        text = pattern.sub(_REDACTED, text)
    text = _MD_LINK.sub(lambda m: m.group(1), text)
    known = {int(n) for n in refs if type(n) is int}
    own = re.compile(
        rf"(?i)https?://github\.com/{re.escape(slug)}/(?:issues|pull)/(\d{{1,10}})(?![\w/#?])"
        if slug
        else r"(?!x)x"
    )

    def own_link(match):
        number = int(match.group(1))
        return f"#{number}" if number in known else _LINK

    text = own.sub(own_link, text)
    text = _URL.sub(_LINK, text)
    text = _PATH_OUTSIDE.sub(_PATH, text)
    text = _MENTION.sub(_AT, text)
    text = _GH_NUMBER.sub("GH－", text)
    text = _REF.sub(lambda m: m.group(0) if int(m.group(1)) in known else _HASH + m.group(1), text)
    text = re.sub(r"(?<=\w)#(?=\d)", _HASH, text)
    text = _ENTITY.sub("&amp;", text)  # whatever still looks like an entity stays literal
    text = text.replace("<", "&lt;").replace(">", "&gt;")
    text = " ".join(text.split())
    visible = text
    for placeholder in _PLACEHOLDERS:
        visible = visible.replace(placeholder, " ")
    if len(visible.strip()) < min_kept:
        return None
    if sum(text.count(placeholder) for placeholder in _PLACEHOLDERS) > MAX_PLACEHOLDERS:
        return None
    return _cap(text, limit)


def _cap(text, limit):
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:") + _ELLIPSIS


def split_questions(text):
    """``(context, questions)`` of a cleaned reason: the sentences that end in ``?`` are questions."""
    parts = [part for part in _SENTENCE_END.split(text) if part]
    questions = [_cap(part, QUESTION_LIMIT) for part in parts if part.endswith("?")]
    context = " ".join(part for part in parts if not part.endswith("?"))
    return context, questions[:MAX_QUESTIONS]


_WITHHELD = "(withheld)"
_EPIC_MIN_KEPT = 3  # a phase title or a path can be short; the placeholder and redaction limits still apply


def sanitize_epic(payload, refs=(), slug=None, secrets=()):
    """The epic payload with every text made safe (``sanitize_reason``), or None when it is not a valid payload.

    A field that cannot be trusted at all becomes ``(withheld)`` (a path or a criterion is dropped when
    others remain), so the shape stays valid and the numbering of phases and questions never shifts. The
    result is what the comment shows AND what its hidden marker carries.
    """
    try:
        clean = epic.validate(payload)
    except ValueError:
        return None

    def safe(text, limit):
        return sanitize_reason(text, refs, slug, limit=limit, secrets=secrets, min_kept=_EPIC_MIN_KEPT)

    def many(items, limit):
        kept = [value for value in (safe(item, limit) for item in items) if value is not None]
        return kept or [_WITHHELD]

    phases = [
        {
            "title": safe(phase["title"], epic.MAX_TITLE) or _WITHHELD,
            "objective": safe(phase["objective"], epic.MAX_OBJECTIVE) or _WITHHELD,
            "paths": many(phase["paths"], epic.MAX_PATH),
            "acceptance": many(phase["acceptance"], epic.MAX_CRITERION),
            "depends_on": phase["depends_on"],
            "cost": phase["cost"],
        }
        for phase in clean["phases"]
    ]
    questions = [safe(question, epic.MAX_QUESTION) or _WITHHELD for question in clean["questions"]]
    return {"phases": phases, "questions": questions}


MARKER_PREFIX = "<!-- factory-outcome:v1 "


def marker(issue, kind):
    return f"{MARKER_PREFIX}issue={issue} kind={kind} -->"


def without_factory_comments(issue):
    """The issue without Factory's own outcome comments, for the planner's snapshot.

    Those comments carry sanitised, model-derived text under Factory's identity (and go stale
    once the blocker is gone): handing them back to the planner would let that text steer the
    next plan with Factory's authority. A comment is Factory's if it starts with the outcome
    marker; the worst a person gains by forging the marker is hiding their own comment.
    """
    comments = issue.get("comments") if isinstance(issue, dict) else None
    if not isinstance(comments, list):
        return issue
    kept = [
        comment
        for comment in comments
        if not (
            isinstance(comment, dict)
            and isinstance(comment.get("body"), str)
            and comment["body"].startswith(MARKER_PREFIX)
        )
    ]
    return issue if len(kept) == len(comments) else {**issue, "comments": kept}


HELD_UNBLOCK = (
    "Edit the issue, or change its labels (add a label and remove it again, for instance). Either one lets Factory "
    "plan the issue once more; there is no timed retry."
)


def render_held(issue, code):
    """The comment body of a plan hold: the marker, the fixed failure code and fixed text, nothing else.

    No issue title or body, no model output, no run name and no exception text goes in: the only variable parts are the issue
    number (in the marker) and ``code`` (forced into ``plan_failure.CODES``).
    """
    code = code if code in plan_failure.CODES else plan_failure.OTHER
    return "\n".join(
        [
            marker(issue, KIND_HELD),
            "**Factory: planning is on hold**",
            "",
            "Factory tried to plan this issue and the plan failed the same way twice in a row, so it will not "
            "plan it again until something changes.",
            "",
            f"**What would unblock it:** {HELD_UNBLOCK}",
            "",
            f"Code: `{code}`",
            "",
            "This comment was posted automatically, once for this hold.",
        ]
    )


def _fenced(lines):
    """Reason-derived text as one fenced code block.

    GitHub renders nothing inside a code block: no links or images, no headings or lists, no
    ``@mention`` notification, no ``#N`` or URL autolink. What a model wrote (from an issue an
    attacker may control) can be read, but it cannot act. Backticks are replaced so the text can
    never close the fence, and every line is one line so it can never start a new block.
    """
    body = "\n".join(" ".join(line.replace("`", "'").split()) for line in lines)
    return ["```text", body, "```"]


MAX_BODY = 60_000  # GitHub refuses a comment over 65,536 characters


def _epic_phase_lines(clean):
    lines = []
    for number, phase in enumerate(clean["phases"], 1):
        depends = ", ".join(str(item) for item in phase["depends_on"]) or "none"
        lines += [
            f"{number}. [{phase['cost']}] {phase['title']}",
            f"Objective: {phase['objective']}",
            f"Touches: {', '.join(phase['paths'])}",
            f"Acceptance: {'; '.join(phase['acceptance'])}",
            f"Depends on phase: {depends}",
            "",
        ]
    return lines[:-1]


def _render_epic(issue, code, reason, run, label, clean):
    """The plan comment of an epic: a fixed template around fenced, sanitised, model-derived text.

    Everything a model wrote (phases, questions, the reason) sits inside fenced code blocks, and
    the hidden marker line carries the same sanitised plan for a later reader. The text of this comment
    is a pure function of the plan: the gate state (needs info, awaiting approval, approved) is in labels.
    """
    headline, unblock = KINDS[KIND_EPIC]
    questions = clean["questions"] if clean else []
    lines = [marker(issue, KIND_EPIC)]
    hidden = epic.hidden(issue, clean) if clean else None
    lines += [hidden] if hidden else []
    lines += [
        f"**Factory: {headline}**",
        "",
        "Factory judged this issue too large for one change, but it can be split. The phases below are a "
        "proposal for a person to review; nothing is built from this plan yet.",
        "",
    ]
    if reason is not None:
        lines += ["Why it does not fit one change:", "", *_fenced([reason]), ""]
    if clean is None:
        lines += [
            "Factory could not safely quote its plan here; the disposition code below says what it decided.",
            "",
        ]
    else:
        count = len(clean["phases"])
        lines += [
            f"Proposed phases ({count}), each independently shippable, in delivery order "
            "(size: S small, M medium, L large):",
            "",
            *_fenced(_epic_phase_lines(clean)),
            "",
        ]
    if questions:
        lines += [
            "Open questions (numbered; Factory does not guess):",
            "",
            *_fenced([f"{number}. {question}" for number, question in enumerate(questions, 1)]),
            "",
        ]
    steps = [
        "**What happens next:**",
        "",
        *(
            [
                "1. A person with write access answers the questions in a comment on this issue. That moves "
                "the issue from `factory:needs-info` to `factory:awaiting-approval`; Factory never reads the "
                "words of the answer.",
                "2. If the answers change the plan, re-apply the "
                f"`{label}` label (remove it and add it again): Factory plans the issue again and updates this comment.",
                f"3. When the plan is right, a person with write access applies the `{epic.LABEL_APPROVE}` label. "
                "The issue then shows `factory:plan-approved`.",
            ]
            if questions
            else [
                f"1. To approve the plan, a person with write access applies the `{epic.LABEL_APPROVE}` label. "
                "The issue then shows `factory:plan-approved`.",
                f"2. To have Factory plan the issue again, re-apply the `{label}` label (remove it and add it again).",
            ]
        ),
        "",
        f"**What would unblock it:** {unblock}",
    ]
    shown_run = run if isinstance(run, str) and _RUN_NAME.fullmatch(run) else "unknown"
    shown_code = code if code in CODES else KIND_EPIC
    lines += [
        *steps,
        "",
        f"Run: `{shown_run}` · Code: `{shown_code}`",
        "",
        "Approval and answers count only from people with the repository permission the trigger label needs; "
        "bots, including Factory itself, never count. This comment was posted automatically.",
    ]
    body = "\n".join(lines)
    if len(body) > MAX_BODY and hidden:  # cannot happen within the caps; a comment GitHub refuses is worse
        body = body.replace(f"{hidden}\n", "", 1)
    return body


def render(issue, kind, code, reason, run, trigger_label=DEFAULT_TRIGGER_LABEL, cause=None, payload=None):
    """The comment body. ``reason`` is already sanitised (or None for the template-only form).

    ``payload`` is the sanitised epic payload (``sanitize_epic``) of an ``epic`` comment, or None.
    """
    if kind == KIND_EPIC:
        label = trigger_label if _LABEL_NAME.fullmatch(str(trigger_label)) else DEFAULT_TRIGGER_LABEL
        return _render_epic(issue, code, reason, run, label, payload)
    headline, unblock = KINDS[kind]
    if kind == KIND_FAILED and cause == CAUSE_TIMEOUT:
        unblock = TIMEOUT_UNBLOCK
    code = code if code in CODES else kind
    run = run if isinstance(run, str) and _RUN_NAME.fullmatch(run) else "unknown"
    label = trigger_label if _LABEL_NAME.fullmatch(str(trigger_label)) else DEFAULT_TRIGGER_LABEL
    lines = [marker(issue, kind), f"**Factory: {headline}**", ""]
    if reason is None:
        lines.append(
            "Factory could not safely quote its own explanation here; the disposition code below "
            "says what it decided."
        )
    elif kind == KIND_NEEDS_DECISION:
        context, questions = split_questions(reason)
        shown = ([context] if context else []) + (
            ["", *(f"{n}. {question}" for n, question in enumerate(questions, 1))]
            if questions
            else []
        )
        if questions:
            lines += ["Factory needs a decision on:", ""]
        elif context:
            lines += ["Factory's explanation:", ""]
        else:
            lines.append("Factory is waiting for a decision.")
        if shown:
            lines += _fenced(shown)
    else:
        lines += ["Factory's explanation:", "", *_fenced([reason])]
    footer = "This comment was posted automatically."
    if kind in (KIND_BLOCKED, KIND_NEEDS_DECISION, KIND_FAILED):
        # Re-applying the trigger label asks for another attempt on an issue labelled
        # ``factory:blocked`` or ``factory:ci-failed`` (``trigger.classify``): a blocked or failed
        # one, and an issue the planner only investigated (no job owns it, so it is planned
        # afresh). A finished job waiting for a person to publish it is not retried that way: a
        # rebuild would hit the same rule. What the label does depends on the cause: a job whose
        # dependency was closed without merging can never run under its old plan, so the label makes
        # a new plan instead of a retry (``Operations._owner``).
        cause = cause if kind == KIND_BLOCKED else None
        if cause == CAUSE_DEPENDENCY_CLOSED:
            footer = (
                "Retrying the old plan cannot help: its dependency can never merge. "
                f"To have Factory plan the issue again in a new run against the current main, re-apply the `{label}` label "
                f"(remove it and add it again). {footer}"
            )
        else:
            ask = (
                "Once this is settled, to have Factory plan the issue again,"
                if kind == KIND_NEEDS_DECISION
                else "To ask Factory to try again,"
            )
            footer = f"{ask} re-apply the `{label}` label (remove it and add it again). {footer}"
    lines += [
        "",
        f"**What would unblock it:** {unblock}",
        "",
        f"Run: `{run}` · Code: `{code}`",
        "",
        footer,
    ]
    return "\n".join(lines)


_reported = False
_report_lock = threading.Lock()


def reset_report():
    """Allow the next failure to print its line again (tests; a process is one run)."""
    global _reported
    with _report_lock:
        _reported = False


def _report(error):
    global _reported
    with _report_lock:
        if _reported:
            return
        _reported = True
    print(f"factory: issue comment failed ({type(error).__name__})", file=sys.stderr)


def switched_off(env=None):
    """True when the deployment-wide kill switch (``FACTORY_ISSUE_COMMENTS=off``) is set."""
    env = os.environ if env is None else env
    return (env.get(ENV_SWITCH) or "").strip().lower() in _OFF


def cycle_cap(env=None):
    """Comments written per supervisor invocation at most (``FACTORY_ISSUE_COMMENTS_PER_CYCLE``)."""
    env = os.environ if env is None else env
    text = (env.get(ENV_CAP) or "").strip()
    if not text:
        return DEFAULT_CAP
    try:
        value = int(text)
    except ValueError:
        return DEFAULT_CAP
    return value if 0 <= value <= MAX_CAP else DEFAULT_CAP


class IssueComments:
    """Posts or edits Factory's one comment per (issue, outcome kind), best effort and fenced."""

    def __init__(
        self,
        repo,
        slug,
        run,
        enabled=True,
        trigger_label=DEFAULT_TRIGGER_LABEL,
        fence=None,
        clock=time.monotonic,
        env=None,
    ):
        self.repo, self.slug, self.run = repo, slug, run
        self.trigger_label = trigger_label
        self.fence = fence
        self.clock = clock
        self.enabled = bool(enabled) and not switched_off(env)
        self.cap = cycle_cap(env)
        self.written = 0
        self.done = {}  # (issue, kind) -> the body this process saw or wrote last
        self.seen = {}  # (issue, kind) -> digest of the raw inputs that produced that body
        self.ids = {}  # (issue, kind) -> id of Factory's comment, once known
        self.failures = 0
        self.lost = False
        self.suspended_until = None
        self.author = False  # False: not asked yet; None: unknown; else the jq test of our own comments
        self.mutex = threading.RLock()

    @property
    def active(self):
        """False while nothing may be written: switched off, capped, lost or cooling down."""
        if not self.enabled or self.lost or self.written >= self.cap:
            return False
        if self.suspended_until is not None:
            if self.clock() < self.suspended_until:
                return False
            self.suspended_until = None
            self.failures = MAX_CONSECUTIVE_FAILURES - 1  # on probation: one failure rests again
        return True

    def announce(self, issue, kind, code, reason, refs=(), allow=None, payload=None):
        """Make the issue carry the comment for this outcome. True when it is in place.

        Never raises. ``reason`` is the raw text (a plan reason or a job error); it is sanitised
        here. ``refs`` are the numbers Factory knows (see ``sanitize_reason``). ``allow(issue)``
        is asked only when GitHub would be touched (not for an outcome already in place); a False
        answer, or an exception, skips the comment. ``payload`` is the raw epic payload of an ``epic`` outcome.
        """
        if kind not in KINDS or type(issue) is not int or issue <= 0:
            return False
        with self.mutex:
            if not self.active:
                return False
            key = (issue, kind)
            digest = self._digest(kind, code, reason, refs, payload)
            if self.seen.get(key) == digest and key in self.done:
                return True  # the same raw inputs as last time: nothing to sanitise or look up
            try:
                secrets = _secret_values()
                clean = sanitize_reason(reason, refs, self.slug, secrets=secrets)
                plan = sanitize_epic(payload, refs, self.slug, secrets) if kind == KIND_EPIC else None
                # The cause is read from the raw reason: sanitising rewrites some job ids (``gh-12``).
                body = render(
                    issue, kind, code, clean, self.run, self.trigger_label,
                    cause=block_cause(reason) or failure_cause(reason), payload=plan,
                )
            except Exception as error:  # noqa: BLE001
                _report(error)
                return False
            if self.done.get(key) == body:
                self.seen[key] = digest
                return True
            try:
                if allow is not None and not allow(issue):
                    return False
            except Exception as error:  # noqa: BLE001
                _report(error)
                return False
            placed = self._attempt(self._write, issue, kind, body)
            if placed and self.done.get(key) == body:
                self.seen[key] = digest
            return placed

    @staticmethod
    def _digest(kind, code, reason, refs, payload=None):
        raw = reason if isinstance(reason, str) else ""
        numbers = ",".join(str(n) for n in sorted(n for n in refs if type(n) is int))
        extra = json.dumps(payload, sort_keys=True, default=str) if payload is not None else ""
        text = f"{kind}\0{code}\0{numbers}\0{raw}\0{extra}"
        return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()

    def _attempt(self, action, *args):
        try:
            if self.fence is not None:
                self.fence()
            action(*args)
            self.failures = 0
            return True
        except LockError:
            self.lost = True
        except Exception as error:  # noqa: BLE001
            self._failed(error)
            self.failures += 1
            if self.failures >= MAX_CONSECUTIVE_FAILURES:
                self.suspended_until = self.clock() + COOLDOWN_SECONDS
        return False

    def _failed(self, error):
        """How a failed write is reported (a subclass that has its own log overrides this)."""
        _report(error)

    def _marker(self, issue, kind):
        """The marker a comment of this key starts with (a subclass with its own comment kind overrides this)."""
        return marker(issue, kind)

    def _write(self, issue, kind, body):
        key = (issue, kind)
        path = f"{repo_path(self.slug)}/issues/{issue}/comments"
        known = self.ids.get(key)
        existing = None
        if known is None:
            found = self._find(path, self._marker(issue, kind))  # None too when we cannot tell who we are
            if found is not None:
                known, existing = found
        if known is not None and existing == body:
            self.ids[key], self.done[key] = known, body
            return
        if self.written >= self.cap:
            return
        if known is not None:
            self._gh(["api", "-X", "PATCH", f"{repo_path(self.slug)}/issues/comments/{known}",
                      "-f", f"body={body}"])
            self.ids[key] = known
        else:
            answer = self._gh(["api", "-X", "POST", path, "--jq", ".id", "-f", f"body={body}"])
            try:
                self.ids[key] = int(answer.strip())
            except ValueError:
                self.ids.pop(key, None)  # the next look finds it by its marker
        self.written += 1
        self.done[key] = body

    def _author_test(self):
        """The jq test that a comment was written by Factory itself, or None when unknown.

        With the Factory App that is the App's bot user id; with an ambient ``gh`` login, that login.
        A comment of any other author (another App, a workflow, a person) is never edited.
        """
        if self.author is False:
            found = None
            user_id, ambient = self._identity()
            if type(user_id) is int and user_id > 0:
                found = f'.user.type == "Bot" and .user.id == {user_id}'
            elif ambient:
                login = self._gh(["api", "user", "--jq", ".login"]).strip()  # a failure: ask again later
                if _LOGIN.fullmatch(login):
                    found = f'.user.login == "{login}"'
            self.author = found
        return self.author

    @staticmethod
    def _identity():
        """``(the App's bot user id or None, whether the login is to be asked)`` for the process's own GitHub identity."""
        auth = get_auth()
        return getattr(auth, "app_user_id", None), auth is None or type(auth).__name__ == "AmbientAuth"

    def _find(self, path, wanted):
        """``(id, body)`` of Factory's comment with this marker, or None. Lowest id wins."""
        author = self._author_test()
        if author is None:
            return None
        output = self._gh(
            [
                "api",
                "--paginate",
                path,
                "--jq",
                f'.[] | select(({author}) and (.body | startswith("{wanted}")))'
                " | {id, body} | tojson",
            ]
        )
        best = None
        for line in output.splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if (
                not isinstance(item, dict)
                or type(item.get("id")) is not int
                or not isinstance(item.get("body"), str)
            ):
                raise ValueError("unexpected comment list")
            if best is None or item["id"] < best[0]:
                best = (item["id"], item["body"])
        return best

    def _gh(self, args):
        return command(["gh", *args], self.repo, timeout=CALL_TIMEOUT)


def for_run(repo, cfg, directory, fence=None):
    """The commenter of one run's supervisor invocation, configured from the repository's config."""
    return IssueComments(
        repo,
        cfg.repo or None,
        Path(directory).name,
        enabled=cfg.comment_on_blocked,
        trigger_label=cfg.auto_build_label,
        fence=fence,
    )

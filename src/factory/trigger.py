"""Label-triggered builds: the decisions, and the thin GitHub reader behind them.

The manager watches every registered repository for open issues that carry the trigger label
(``factory-build`` by default), so that nobody calls ``plan`` or ``run`` by hand. This module is
the part that needs no threads and no manager:

* **Eligibility** (``classify``): an issue is eligible when it is open, carries the trigger label,
  does not carry ``factory-ignore`` and carries no ``factory:*`` status label. Ignore always wins.
  The one exception is the retry request: the trigger label on an issue whose only status labels
  are ``factory:blocked``, ``factory:ci-failed``, ``factory:needs-info``, ``factory:awaiting-approval`` and/or
  ``factory:observed`` or ``factory:epic-phase`` asks for another attempt (the epic labels: a re-plan of an epic after its
  questions were answered; the other two: a person asks to have an issue Factory opened itself built).
* **Authority** (``permission_ok``, ``latest_actor``): applying a label needs only triage access, so
  the user who applied it (the newest ``labeled`` event with that name) must hold a repository
  permission of at least ``auto_build_min_permission`` (``write`` by default). Bots are never
  accepted. A minimum below ``write`` is refused unless the repository is private (``minimum_allowed``).
  ``LabelAuthority`` applies the same bar to the model and effort labels at plan time.
* **Naming and pacing** (``run_name``, ``Backoff``, ``LineLimiter``): ``auto-<UTC timestamp>`` run
  names, an exponential per-repository backoff capped at an hour, and a limiter so that an
  unchanged log line is not repeated every cycle.
* **Reading GitHub** (``GithubReader``): a few calls through the same ``gh`` and App-token wiring
  the status labels use (``shell.command`` with the repository's own auth). Only ids, label names,
  logins and permission names are ever taken out of an answer; ``--jq`` projects the rest away
  before it leaves ``gh``, so an issue title or body never reaches this process.

The loop that uses all of it is in ``operations.py``. Nothing here writes to a log.
"""

import json
import math
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from .model import holds_unmerged_work
from .config import (
    DEFAULT_MIN_PERMISSION,
    DEFAULT_TRIGGER_CONCURRENCY,
    DEFAULT_TRIGGER_LABEL,
    DEFAULT_TRIGGER_MAX_ISSUES,
    DEFAULT_PLANNER_USD_PER_HOUR,
    DEFAULT_TRIGGER_TIMEOUT_SECONDS,
    MAX_TRIGGER_CONCURRENCY,
    MAX_TRIGGER_MAX_ISSUES,
    MAX_TRIGGER_TIMEOUT_SECONDS,
    MIN_TRIGGER_TIMEOUT_SECONDS,
    PERMISSION_LEVELS,
)
from .github import repo_path
from .issue_comments import closed_dependency, marker
from . import epic
from .issue_labels import (
    IGNORE_LABEL,
    LABEL_AWAITING_APPROVAL,
    LABEL_BLOCKED,
    LABEL_CI_FAILED,
    LABEL_EPIC_PHASE,
    LABEL_NEEDS_INFO,
    LABEL_OBSERVED,
    LABEL_PLAN_APPROVED,
)
from .shell import command

RUN_PREFIX = "auto-"
RETRY_REASON = "label-triggered retry"
STATUS_PREFIX = "factory:"
# Status labels on which re-applying the trigger label asks for another attempt. The two epic labels are
# here because answering an epic's questions is followed by a re-plan (a fresh plan folds the answers
# in). ``factory:plan-approved`` is here too: a person re-applying the trigger label to an approved epic asks for
# a new plan, but only while no phase is in flight (``operations._candidates`` asks ``epic_phases.in_flight`` and
# fails closed). ``factory:observed`` is the origin
# label of an issue Factory opened for a worker's observation (``observed_issues.py``) and ``factory:epic-phase``
# that of a phase issue of an approved epic plan (``epic_phases.py``): such an issue is not in flight, a person
# applying the trigger label is the request to plan it.
RETRY_STATUS_LABELS = frozenset(
    {
        LABEL_BLOCKED,
        LABEL_CI_FAILED,
        LABEL_NEEDS_INFO,
        LABEL_AWAITING_APPROVAL,
        LABEL_PLAN_APPROVED,
        LABEL_OBSERVED,
        LABEL_EPIC_PHASE,
    }
)

BACKOFF_BASE_SECONDS = 60.0
BACKOFF_CAP_SECONDS = 3600.0
REPEAT_SECONDS = 3600.0
NEVER = float("inf")

PAGE_SIZE = 100
MAX_ISSUE_PAGES = 10  # 1000 open issues carrying the trigger label; the rest wait for the next pass
MAX_EVENT_PAGES = 30  # 3000 events on one issue; past that the newest labeler cannot be known
CALL_TIMEOUT = 30
MAX_PLAN_BYTES = 16 * 1024 * 1024
MAX_PHASE_PAGES = 5  # 500 issues of Factory's own identity updated since an epic's approval are scanned for its phase issues
MAX_PULL_PAGES = 5  # 500 open pull requests are scanned for the Factory App's own; the rest wait
MAX_CLOSED_PULL_PAGES = 5  # 500 closed pull requests are scanned for the Factory App's own (false-incomplete rate)
MAX_COMMIT_PAGES = 3  # 300 commits of one pull request
MAX_REPLY_LOGINS = 10  # distinct commenters of one epic issue whose permission is looked up per read, whoever they are
MAX_TRUSTED_REPLY_LOGINS = 40  # the same bound counting only commenters GitHub itself calls an owner, member or collaborator
TRUSTED_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")  # ``author_association``: set by GitHub, never by the commenter
MAX_REMARK_PAGES = 10  # 1000 comments of one kind updated since the cursor; past that the pass says "truncated"
MAX_REVIEW_PAGES = 30  # reviews cannot be asked "since": all of them are read, so room for 3000
MAX_TEXT_CHARS = 4000  # the text of one comment that leaves ``gh`` for a follow-up session's context
MAX_FAILED_CHECKS = 20  # failing checks of a pull request that are looked at for a follow-up
MAX_LOG_CHARS = 4000  # the tail of one failing job's log that is kept
MAX_SEARCHED_CHARS = 8000  # only this much of a comment is searched for a mention (a 65 KB hostile one is slow in jq)
SINCE_KINDS = ("issue", "review_comment")  # the comment lists that take ``since`` (the reviews list does not)
REMARK_KINDS = ("issue", "review_comment", "review")  # conversation comments, review comments, review bodies
_REMARK_PATHS = {"issue": "issues/{}/comments", "review_comment": "pulls/{}/comments", "review": "pulls/{}/reviews"}

_APP_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,99}")
_LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_NOT_FOUND = re.compile(r"\bHTTP 404\b")  # what gh prints: "gh: Not Found (HTTP 404)"
_GONE = re.compile(r"\bHTTP (?:404|410)\b")
_EVENT_ID = re.compile(r"[0-9]{1,20}")


# --- settings ----------------------------------------------------------------------


@dataclass(frozen=True)
class TriggerSettings:
    """What the trigger loop needs to know about one repository."""

    enabled: bool = True
    label: str = DEFAULT_TRIGGER_LABEL
    min_permission: str = DEFAULT_MIN_PERMISSION
    concurrency: int = DEFAULT_TRIGGER_CONCURRENCY
    max_issues: int = DEFAULT_TRIGGER_MAX_ISSUES
    timeout: int = DEFAULT_TRIGGER_TIMEOUT_SECONDS  # seconds per agent session (``--timeout``)
    planner_usd_per_hour: float = DEFAULT_PLANNER_USD_PER_HOUR  # 0: no cap on the last hour's planner spend


def _count(value: object, default: int, high: int) -> int:
    ok = isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= high
    return value if ok else default  # type: ignore[return-value]


def _session_timeout(value: object) -> int:
    ok = (
        isinstance(value, int)
        and not isinstance(value, bool)
        and MIN_TRIGGER_TIMEOUT_SECONDS <= value <= MAX_TRIGGER_TIMEOUT_SECONDS
    )
    return value if ok else DEFAULT_TRIGGER_TIMEOUT_SECONDS  # type: ignore[return-value]


def _hourly_cap(value: object) -> float:
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
    return float(value) if ok else DEFAULT_PLANNER_USD_PER_HOUR  # type: ignore[arg-type]


def settings_of(cfg: object) -> TriggerSettings:
    """The trigger settings of a repository config, through ``getattr`` like ``operations.policy_of``.

    A config object that does not carry a setting means its default; a value outside the
    contract falls back to the *safe* side: anything but ``True`` is off, an unknown permission
    is ``admin``, an unusable label is the default one.
    """
    label = getattr(cfg, "auto_build_label", DEFAULT_TRIGGER_LABEL)
    level = getattr(cfg, "auto_build_min_permission", DEFAULT_MIN_PERMISSION)
    return TriggerSettings(
        enabled=getattr(cfg, "auto_build", True) is True,
        label=label if isinstance(label, str) and label.strip() else DEFAULT_TRIGGER_LABEL,
        min_permission=level if level in PERMISSION_LEVELS else "admin",
        concurrency=_count(
            getattr(cfg, "auto_build_concurrency", None), DEFAULT_TRIGGER_CONCURRENCY, MAX_TRIGGER_CONCURRENCY
        ),
        max_issues=_count(
            getattr(cfg, "auto_build_max_issues", None), DEFAULT_TRIGGER_MAX_ISSUES, MAX_TRIGGER_MAX_ISSUES
        ),
        timeout=_session_timeout(getattr(cfg, "auto_build_timeout_seconds", None)),
        planner_usd_per_hour=_hourly_cap(getattr(cfg, "planner_max_usd_per_hour", DEFAULT_PLANNER_USD_PER_HOUR)),
    )


# --- eligibility -------------------------------------------------------------------


@dataclass(frozen=True)
class IssueFacts:
    """What the trigger reads of one open issue: its number, its label names and its last update."""

    number: int
    labels: tuple = ()
    updated: str = ""


def _folded(names: object) -> set:
    return {name.strip().casefold() for name in names if isinstance(name, str)}


def classify(facts: IssueFacts, label: str) -> str | None:
    """``None`` (not triggered), ``ignored``, ``in-flight``, ``retry`` or ``eligible``.

    ``factory-ignore`` wins over everything. Any ``factory:*`` label means the issue is in flight
    or done, except that an issue whose status labels are all ``factory:blocked``, ``factory:ci-failed``,
    ``factory:needs-info``, ``factory:awaiting-approval``, ``factory:plan-approved``, ``factory:observed`` or ``factory:epic-phase`` is a request for
    another attempt (the epic labels: the re-plan of an epic once its questions are answered; ``factory:observed``: an
    issue Factory opened itself for an observation, which a person now asks to have built).
    """
    names = _folded(facts.labels)
    if label.strip().casefold() not in names:
        return None
    if IGNORE_LABEL in names:
        return "ignored"
    status = {name for name in names if name.startswith(STATUS_PREFIX)}
    if not status:
        return "eligible"
    return "retry" if status <= RETRY_STATUS_LABELS else "in-flight"


def pick(numbers: list, limit: int) -> list:
    """At most ``limit`` issue numbers, the lowest (oldest) first; the rest wait for the next cycle."""
    return sorted(set(numbers))[:limit]


# --- who applied the label ---------------------------------------------------------


@dataclass(frozen=True)
class Actor:
    login: str
    kind: str  # GitHub's actor type: "User", "Bot", "Organization", ...
    event: str = ""  # the id of the ``labeled`` event: a label applied again is a new event
    created: str = ""  # when that event happened (ISO 8601, UTC), or "" when GitHub did not say

    @property
    def is_user(self) -> bool:
        return self.kind == "User" and bool(_LOGIN.fullmatch(self.login))


def latest_actor(events: list, label: str) -> Actor | None:
    """The actor of the newest ``labeled`` event for ``label`` (events oldest first), or None."""
    wanted = label.strip().casefold()
    found = None
    for event in events:
        if not isinstance(event, dict) or event.get("event") != "labeled":
            continue
        name = event.get("label")
        if isinstance(name, str) and name.strip().casefold() == wanted:
            login, kind, ident = event.get("login"), event.get("type"), event.get("id")
            created = event.get("created")
            found = Actor(
                login if isinstance(login, str) else "",
                kind if isinstance(kind, str) else "",
                str(ident) if _EVENT_ID.fullmatch(str(ident)) else "",
                created if isinstance(created, str) else "",
            )
    return found


def level_rank(level: object) -> int:
    """Position in read < triage < write < maintain < admin; -1 for anything else (custom roles, ``none``)."""
    return PERMISSION_LEVELS.index(level) if level in PERMISSION_LEVELS else -1


def permission_ok(level: object, minimum: str) -> bool:
    """Whether a user holding ``level`` may trigger a build under the repository's minimum."""
    rank = level_rank(level)
    return rank >= 0 and rank >= level_rank(minimum) >= 0


def minimum_allowed(minimum: str, private: bool) -> bool:
    """A minimum below ``write`` is only allowed on a private repository (checked at poll time)."""
    return private or level_rank(minimum) >= level_rank("write") >= 0


AUTHORITY_UNKNOWN = "could not establish who applied the model or effort label"


class LabelAuthority:
    """Whether whoever applied a label may choose what a build uses (the ``factory-model:`` and ``factory-effort:`` labels).

    The bar is the trigger's own: applying a label needs only triage access, so a label counts only
    when its newest applier (``latest_actor``) is a user, not a bot, who holds at least
    ``auto_build_min_permission`` (``permission_ok``), on a repository where that minimum is allowed
    at all (``minimum_allowed``). Everything else fails closed: no ``labeled`` event found, too many
    events to find it, an unknown or custom role. A read that fails raises ``RuntimeError`` with a
    fixed message (never the ``gh`` error), so nothing is planned on a guess.

    One instance serves one plan: the repository's visibility and each login's role are read once.
    """

    def __init__(self, reader: object, minimum: str):
        self.reader, self.minimum = reader, minimum
        self._private: bool | None = None
        self._levels: dict = {}

    def __call__(self, number: int, labels: list) -> bool:
        """True when the newest applier of every label in ``labels`` (issue ``number``) is allowed."""
        try:
            return self._allowed(number, labels)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            raise RuntimeError(AUTHORITY_UNKNOWN) from None

    def _allowed(self, number: int, labels: list) -> bool:
        if self._private is None:
            self._private = self.reader.repo_facts().private
        if not minimum_allowed(self.minimum, self._private):
            return False
        actors = self.reader.label_actors(number, labels)
        for label in labels:
            actor = actors.get(label)
            if actor is None or not actor.is_user:
                return False
            if actor.login not in self._levels:
                self._levels[actor.login] = self.reader.permission(actor.login)
            if not permission_ok(self._levels[actor.login], self.minimum):
                return False
        return True


# --- runs --------------------------------------------------------------------------


def run_name(now: float, taken: Callable[[str], bool]) -> str:
    """``auto-<UTC timestamp>``, with ``-2``, ``-3``... when that name is already used."""
    base = RUN_PREFIX + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    name, counter = base, 1
    while taken(name):
        counter += 1
        name = f"{base}-{counter}"
    return name


def plan_issue_numbers(document: object) -> list:
    """The issue numbers a ``plan.json`` lists (positive ints only; anything malformed is skipped)."""
    issues = document.get("issues") if isinstance(document, dict) else None
    found = []
    for item in issues if isinstance(issues, list) else []:
        number = item.get("number") if isinstance(item, dict) else None
        if type(number) is int and number > 0 and number not in found:
            found.append(number)
    return found


def job_owning(document: object, number: int) -> str | None:
    """The id of the job of a ``plan.json`` whose ``issues`` hold ``number``, or None."""
    jobs = document.get("jobs") if isinstance(document, dict) else None
    for job in jobs if isinstance(jobs, list) else []:
        owned = job.get("issues") if isinstance(job, dict) else None
        if isinstance(owned, list) and number in owned and isinstance(job.get("id"), str):
            return job["id"]
    return None


def has_disposition(document: object, number: int, statuses: tuple | None = None) -> bool:
    """Whether a ``plan.json`` records a disposition for ``number``, of one of ``statuses`` when given.

    The statuses are ``investigation``, ``covered``, ``blocked`` and ``epic`` (any of them when ``statuses`` is None).
    """
    dispositions = document.get("dispositions") if isinstance(document, dict) else None
    if not isinstance(dispositions, list):
        return False
    for item in dispositions:
        if not isinstance(item, dict):
            continue
        issue = item.get("issue")
        if not isinstance(issue, int) or isinstance(issue, bool) or issue != number:
            continue
        if statuses is None or item.get("status") in statuses:
            return True
    return False


def read_plan(path: Path) -> dict | None:
    """A run's ``plan.json`` (or ``state.json``) as a dict, or None when it is missing, huge or not JSON."""
    try:
        if path.stat().st_size > MAX_PLAN_BYTES:
            return None
        document = json.loads(path.read_text())
    except (OSError, ValueError, RecursionError):
        return None
    return document if isinstance(document, dict) else None


def building_workers(states: object) -> int:
    """How many jobs of a run's ``state.json`` hold a build worker right now.

    A job counts when it is ``running`` in a build phase (``build``, ``build-fix``...); a running job
    that records no phase at all counts too, since it holds a slot of some kind. Verification and
    review workers do not count: they have their own limits.
    """
    count = 0
    for job in states.values() if isinstance(states, dict) else ():
        if not (isinstance(job, dict) and job.get("status") == "running"):
            continue
        phase = job.get("worker_phase") or job.get("phase")
        count += not isinstance(phase, str) or phase.split("-", 1)[0] in ("build", "")
    return count


# --- jobs that wait for a dependency merge -------------------------------------------

_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_PULL_URL = re.compile(r"https://[A-Za-z0-9.-]+/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([0-9]{1,9})")


def waiting_on_dependencies(plan: object, state: object) -> list:
    """``(job id, [dependency ids])`` for every queued job whose ``depends_on`` holds an unmerged job.

    Both documents come from the run directory; anything malformed is skipped. A dependency counts
    as unmerged unless the persisted state says ``merged`` (``run`` refreshes that from GitHub at
    its start, so the state of a run that is idle may lag behind a merge).
    """
    specs = plan.get("jobs") if isinstance(plan, dict) else None
    states = state.get("jobs") if isinstance(state, dict) else None
    if not isinstance(specs, list) or not isinstance(states, dict):
        return []
    found = []
    for spec in specs:
        ident = spec.get("id") if isinstance(spec, dict) else None
        own = states.get(ident) if isinstance(ident, str) else None
        wanted = spec.get("depends_on") if isinstance(spec, dict) else None
        if not (isinstance(own, dict) and own.get("status") == "queued" and isinstance(wanted, list)):
            continue
        unmerged = []
        for dependency in wanted:
            if not (isinstance(dependency, str) and _JOB_ID.fullmatch(dependency)):
                unmerged = []
                break  # a dependency we cannot name is one we cannot judge: leave the job alone
            theirs = states.get(dependency)
            if not (isinstance(theirs, dict) and theirs.get("status") == "merged"):
                unmerged.append(dependency)
        if unmerged and isinstance(ident, str) and _JOB_ID.fullmatch(ident):
            found.append((ident, unmerged))
    return found


SHARED_PATHS_PREFIX = "shared paths: "


def waiting_on_shared_paths(plan: object, state: object) -> list:
    """``(job id, [sibling ids])`` for every queued job whose persisted wait is ``shared paths: <job>``.

    The scheduler writes that reason when an overlapping sibling still holds unmerged work. Jobs that
    also wait on ``depends_on`` are left to ``waiting_on_dependencies``. Names we cannot read, or
    that are no job of the run, leave the job alone.
    """
    specs = plan.get("jobs") if isinstance(plan, dict) else None
    states = state.get("jobs") if isinstance(state, dict) else None
    if not isinstance(specs, list) or not isinstance(states, dict):
        return []
    dependent = {job for job, _ in waiting_on_dependencies(plan, state)}
    found = []
    for spec in specs:
        ident = spec.get("id") if isinstance(spec, dict) else None
        own = states.get(ident) if isinstance(ident, str) else None
        if not (isinstance(own, dict) and own.get("status") == "queued" and _JOB_ID.fullmatch(ident)):
            continue
        reason = own.get("waiting_for")
        if ident in dependent or not (isinstance(reason, str) and reason.startswith(SHARED_PATHS_PREFIX)):
            continue
        names = [name.strip() for name in reason[len(SHARED_PATHS_PREFIX) :].split(",")]
        if names and all(_JOB_ID.fullmatch(n) and isinstance(states.get(n), dict) and n != ident for n in names):
            found.append((ident, names))
    return found


def shared_path_released(state: object, sibling: str) -> bool:
    """Whether the persisted state alone shows that a sibling no longer holds unmerged work."""
    states = state.get("jobs") if isinstance(state, dict) else None
    job = states.get(sibling) if isinstance(states, dict) else None
    return isinstance(job, dict) and not holds_unmerged_work(job)


def pull_number(state: object, dependency: str, repo: str) -> int | None:
    """The number of the pull request a job published (from its ``pr`` URL, in this repository only)."""
    states = state.get("jobs") if isinstance(state, dict) else None
    job = states.get(dependency) if isinstance(states, dict) else None
    url = job.get("pr") if isinstance(job, dict) else None
    found = _PULL_URL.fullmatch(url) if isinstance(url, str) else None
    if found is None or found.group(1).casefold() != repo.casefold():
        return None
    number = int(found.group(2))
    return number if number > 0 else None


# Fixed words after ``reason=`` in the log lines about a retry (never free text).
REASON_DEPENDENCY_CLOSED = "dependency-closed"  # the job's dependency was closed unmerged: a fresh plan, no retry
REASON_REFUSED = "cli-refused"  # the CLI refused to retry the job


def blocked_by_closed_dependency(state: object, job: str) -> bool:
    """Whether the persisted state says this job is blocked because a dependency was closed without merging."""
    states = state.get("jobs") if isinstance(state, dict) else None
    own = states.get(job) if isinstance(states, dict) else None
    if not (isinstance(own, dict) and own.get("status") == "blocked"):
        return False
    return closed_dependency(own.get("error")) is not None


def dependency_status(state: object, dependency: str) -> str | None:
    """The persisted status of a job (``closed`` and ``merged`` are final), or None."""
    states = state.get("jobs") if isinstance(state, dict) else None
    job = states.get(dependency) if isinstance(states, dict) else None
    status = job.get("status") if isinstance(job, dict) else None
    return status if isinstance(status, str) else None


# --- pacing ------------------------------------------------------------------------


class Backoff:
    """Per-key exponential backoff: 60 s, 120 s, 240 s ... capped at an hour; a success forgets it."""

    def __init__(self, clock: Callable[[], float], base: float = BACKOFF_BASE_SECONDS, cap: float = BACKOFF_CAP_SECONDS):
        self._clock, self._base, self._cap = clock, base, cap
        self._failures: dict = {}
        self._until: dict = {}

    def failed(self, key: object) -> float:
        """Record a failure; the seconds to wait before the next attempt."""
        count = self._failures.get(key, 0) + 1
        self._failures[key] = count
        delay = min(self._cap, self._base * 2 ** min(count - 1, 30))
        self._until[key] = self._clock() + delay
        return delay

    def succeeded(self, key: object) -> None:
        self._failures.pop(key, None)
        self._until.pop(key, None)

    def remaining(self, key: object) -> float:
        return max(0.0, self._until.get(key, 0.0) - self._clock())

    def failures(self, key: object) -> int:
        return self._failures.get(key, 0)


@dataclass
class LineLimiter:
    """Lets a log line through only when it differs from the last one of its key or enough time passed."""

    clock: Callable[[], float]
    repeat: float = REPEAT_SECONDS
    keep: int = 4096
    _last: dict = field(default_factory=dict)

    def allow(self, key: object, text: str, repeat: float | None = None) -> bool:
        now = self.clock()
        before = self._last.get(key)
        wait = self.repeat if repeat is None else repeat
        if before is not None and before[0] == text and now - before[1] < wait:
            return False
        if len(self._last) >= self.keep:
            self._last.clear()  # a bounded memory beats a perfect one
        self._last[key] = (text, now)
        return True

    def forget(self, match: Callable[[object], bool]) -> None:
        for key in [key for key in self._last if match(key)]:
            del self._last[key]


# --- GitHub ------------------------------------------------------------------------


@dataclass(frozen=True)
class RepoFacts:
    archived: bool
    private: bool


@dataclass(frozen=True)
class Remark:
    """One comment, review comment or review body, without its text: who wrote it, when, and whether it mentions the App."""

    kind: str  # one of REMARK_KINDS
    id: int
    login: str
    type: str  # GitHub's account type: "User", "Bot", ...
    updated: str
    mention: bool
    created: str = ""
    association: str = ""  # GitHub's ``author_association`` of the comment (``""`` when not asked for or not a string)

    @property
    def is_bot(self) -> bool:
        return self.type == "Bot" or self.login.endswith("[bot]")

    @property
    def trusted(self) -> bool:
        """Whether GitHub calls the author an owner, member or collaborator: never enough to authorise, only to look further."""
        return self.association in TRUSTED_ASSOCIATIONS


@dataclass(frozen=True)
class PlanComment:
    """Factory's own epic plan comment on an issue: who, when and how many questions, never the plan's prose."""

    id: int
    login: str
    created: str
    updated: str
    questions: int | None  # from the hidden marker's validated plan; None when it cannot be read
    # The validated plan of the hidden marker, only when the marker names THIS issue, and its digest. They are not
    # part of equality: they are what ``epic_phases.py`` reads, the rest is what the gate reads.
    plan: dict | None = field(default=None, compare=False)
    digest: str = field(default="", compare=False)


@dataclass(frozen=True)
class RemarkText:
    """One comment with its (capped) text: only ``GithubReader.remark_texts`` makes these, for a follow-up session's context."""

    kind: str
    id: int
    login: str
    type: str
    created: str
    text: str

    @property
    def is_bot(self) -> bool:
        return self.type == "Bot" or self.login.endswith("[bot]")


@dataclass(frozen=True)
class FailedCheck:
    """A failing check run of a pull request's head commit: its name, conclusion and (Actions) job id."""

    name: str
    conclusion: str
    job_id: int


def mention_pattern(slug: str) -> str:
    """``@<slug>`` as a whole token, to be matched case-insensitively.

    Not preceded by a letter, digit, ``_``, ``@``, ``/``, ``.`` or ``-`` (an address or a path is no
    mention) and not followed by a letter, digit, ``_``, ``-`` or ``/`` (``@slug-two`` and a team
    name ``@slug/team`` are other accounts). Plain enough for jq's regular expressions and Python's.
    """
    if not _APP_SLUG.fullmatch(slug):
        raise ValueError("the App slug may hold only letters, digits and hyphens")
    plain = rf"(^|[^A-Za-z0-9_@/.-])@{slug}($|[^A-Za-z0-9_/-])"
    # ``_@slug_`` is an emphasised mention (GitHub notifies it); a lone ``_`` in front still is no boundary
    # (``foo_@slug`` is an identifier). ``*@slug*`` needs nothing: ``*`` is a boundary already.
    emphasised = rf"(^|[^A-Za-z0-9_@/.-])_{{1,2}}@{slug}_{{1,2}}($|[^A-Za-z0-9_/-])"
    return f"{plain}|{emphasised}"


# What does not count as addressing the App: a quoted line (the maintainer is replying to someone else's
# words), a fenced block and an inline code span. They are cut out before the mention test, inside ``gh``.
_QUOTE = r"(^|\n)[ \t]{0,3}>[^\n]*"
_FENCE = r"(```|~~~)[\s\S]*?(```|~~~|\z)"
_CODE_SPAN = r"`+[^`\n]*`+"


@dataclass(frozen=True)
class PullFacts:
    state: str  # "open", "closed" (unmerged) or "merged"
    head: str  # the head commit SHA
    body: str


@dataclass(frozen=True)
class ClosedPull:
    """A closed pull request of the Factory App, as the false-incomplete rate reads it: numbers and times only."""

    number: int
    merged_at: str  # "" when it was closed without merging
    closed_at: str
    head: str  # the head commit SHA when it closed


@dataclass(frozen=True)
class GateStatus:
    """One ``factory/gate`` commit status: its state, its description (Factory's own words), when and by whom."""

    state: str
    description: str
    created: str
    creator: dict  # {"type", "id", "login"}


@dataclass(frozen=True)
class StatusComment:
    """Factory's own status comment on an epic's parent issue: its id, when it was last edited and its whole body.

    The body is Factory's own fixed text (ints and fixed words, see ``epic_phases.render_status``).
    """

    id: int
    updated: str
    ledger: str  # the hidden ledger line ("" when the comment has none)
    body: str


@dataclass(frozen=True)
class PhaseIssue:
    """One issue as the phase pass reads it: never its text, only what that text is checked against.

    ``ours`` is True only when Factory's own identity opened it and it is an issue (not a pull request).
    ``line`` is the first line of the body (the phase marker an author check then trusts), ``content`` the
    ``epic.content_hash`` of its title and body. ``labels`` are the label names (only ``phase_issue`` reads them,
    for the plan-time question whether an issue is a phase: ``epic_phases.plan_stamps``). ``text`` is the title and body, kept only
    by ``phase_issue(number, text=True)`` for an issue that is Factory's own: the start of a phase hashes it against the ledger and
    records it in the run's stamp, and nothing else ever reads it.
    """

    number: int
    state: str  # "open" or "closed"
    closed_at: str  # "" while it is open
    ours: bool
    line: str
    content: str
    labels: tuple = ()
    text: tuple = field(default=(), compare=False, repr=False)  # (title, body) of an issue read with ``phase_issue(text=True)``; never printed


@dataclass(frozen=True)
class ClosingPulls:
    """The pull requests GitHub says close an issue: ``(number, repository, state)`` with a lower-case repository
    and an upper-case state (``MERGED``, ``OPEN``, ``CLOSED``, anything else is ``UNKNOWN``). Never a title or a body.

    ``complete`` is False when GitHub has more references than were read or one row was not understood: the
    absence of a merged pull request then proves nothing.
    """

    pulls: tuple
    complete: bool


# What the phase pass asks GraphQL for, one issue: the pull requests that close it (the REST API has no such
# field). Numbers, states and repository names only. ``includeClosedPrs`` is REQUIRED: it defaults to false and
# GitHub then leaves every merged or closed pull request out of the answer, so the one pull request this read
# looks for would never be listed (GitHub's own MCP server asks for it for the same reason). ``orderByState``
# lists the open ones first, which makes it less likely that ``first`` cuts a merged one off.
CLOSING_FIRST = 20
CLOSING_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){"
    f"issue(number:$number){{closedByPullRequestsReferences(first:{CLOSING_FIRST},includeClosedPrs:true,orderByState:true)"
    "{totalCount nodes{number state repository{nameWithOwner}}}}}}"
)
_CLOSING_JQ = (
    ".data.repository.issue.closedByPullRequestsReferences | {total: .totalCount, "
    "prs: [.nodes[] | {n: .number, s: .state, r: .repository.nameWithOwner}]}"
)
_CLOSING_STATES = ("MERGED", "OPEN", "CLOSED")


def _lines(output: str) -> list:
    """One JSON document per non-empty line (what ``gh api --jq '.[] | {...}'`` prints)."""
    rows = []
    for line in output.splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _is_missing(error: subprocess.CalledProcessError) -> bool:
    return _NOT_FOUND.search(str(error.stderr or "")) is not None


def is_gone(error: BaseException) -> bool:
    """Whether a ``gh`` failure is GitHub's definite "not found" or "gone" (HTTP 404 or 410): the thing that was read does not exist (any more).

    Never true for a timeout, a rate limit, a 5xx or anything else that can pass, and only the status is looked at, never a text of GitHub's.
    """
    return isinstance(error, subprocess.CalledProcessError) and _GONE.search(str(error.stderr or "")) is not None


class GithubReader:
    """Reads (and two writes: the trigger label's removal and a pull request body) against one repository, as the Factory App or the ambient ``gh`` login.

    Every method raises on a failure it cannot interpret (the caller reports only the exception
    type). ``run`` is injectable so that tests can use a fake ``gh`` without a process.
    """

    def __init__(self, repo: str, auth: object, cwd: Path, run: Callable = command):
        self.repo, self._auth, self._cwd, self._run = repo, auth, cwd, run

    def _gh(self, args: list) -> str:
        return self._run(["gh", *args], self._cwd, timeout=CALL_TIMEOUT, auth=self._auth)

    def call(self, args: list) -> str:
        """Run one ``gh`` command as this reader's identity; ``followup_reply.py`` reads and writes its comment with it."""
        return self._gh(args)

    def repo_facts(self) -> RepoFacts:
        jq = "{archived: .archived, private: .private, visibility: .visibility}"
        data = json.loads(self._gh(["api", repo_path(self.repo), "--jq", jq]))
        # Private only when GitHub says so twice: a missing or odd field never lowers the bar.
        private = data.get("private") is True and data.get("visibility", "private") == "private"
        return RepoFacts(archived=data.get("archived") is not False, private=private)

    def open_issues(self, label: str) -> list:
        """Open issues (never pull requests) that carry ``label``: number, label names, last update."""
        return [IssueFacts(*row) for row in self._open_labelled(label, pulls=False)]

    def issue_facts(self, number: int) -> IssueFacts:
        """The labels and last update of one issue, from a single fresh read (what ``open_issues`` says of it)."""
        jq = "{labels: [.labels[].name], updated: .updated_at}"
        found = json.loads(self._gh(["api", f"{repo_path(self.repo)}/issues/{int(number)}", "--jq", jq]))
        names = found.get("labels") if isinstance(found, dict) else None
        updated = found.get("updated") if isinstance(found, dict) else None
        if not isinstance(names, list) or any(not isinstance(n, str) for n in names) or not isinstance(updated, str):
            raise ValueError("unexpected issue answer")
        return IssueFacts(int(number), tuple(names), updated)

    def issue_state(self, number: int) -> tuple:
        """``(state, label names)`` of one issue, from a single fresh read (``state`` is ``open`` or ``closed``)."""
        output = self._gh(
            ["api", f"{repo_path(self.repo)}/issues/{int(number)}", "--jq", "{state: .state, labels: [.labels[].name]}"]
        )
        found = json.loads(output)
        state = found.get("state") if isinstance(found, dict) else None
        names = found.get("labels") if isinstance(found, dict) else None
        if not isinstance(state, str) or not isinstance(names, list) or any(not isinstance(n, str) for n in names):
            raise ValueError("unexpected issue answer")
        return state.lower(), tuple(names)

    def open_pulls(self, label: str) -> set:
        """Numbers of the open pull requests (never issues) that carry ``label``."""
        return {row[0] for row in self._open_labelled(label, pulls=True)}

    def _open_labelled(self, label: str, pulls: bool) -> list:
        jq = ".[] | {number: .number, pr: (.pull_request != null), labels: [.labels[].name], updated: .updated_at}"
        found = []
        for page in range(1, MAX_ISSUE_PAGES + 1):
            path = (
                f"{repo_path(self.repo)}/issues?state=open&labels={quote(label, safe='')}"
                f"&per_page={PAGE_SIZE}&page={page}"
            )
            rows = _lines(self._gh(["api", path, "--jq", jq]))
            for row in rows:
                number = row.get("number")
                if row.get("pr") is pulls and type(number) is int and number > 0:
                    names = tuple(name for name in row.get("labels", ()) if isinstance(name, str))
                    updated = row.get("updated")
                    found.append((number, names, updated if isinstance(updated, str) else ""))
            if len(rows) < PAGE_SIZE:
                break
        return found

    def _label_events(self, number: int) -> list | None:
        """The issue's events (oldest first, paged), or None when there are more than ``MAX_EVENT_PAGES`` pages."""
        jq = (
            ".[] | {id: .id, event: .event, label: (.label.name // \"\"), login: (.actor.login // \"\"), "
            "type: (.actor.type // \"\"), created: (.created_at // \"\")}"
        )
        events: list = []
        for page in range(1, MAX_EVENT_PAGES + 1):
            path = f"{repo_path(self.repo)}/issues/{number}/events?per_page={PAGE_SIZE}&page={page}"
            rows = _lines(self._gh(["api", path, "--jq", jq]))
            events.extend(rows)
            if len(rows) < PAGE_SIZE:
                return events
        return None

    def label_actor(self, number: int, label: str) -> Actor | None:
        """Who applied ``label`` last, from the issue's events (oldest first, paged).

        Fails closed: with more events than ``MAX_EVENT_PAGES`` pages the newest labeling cannot
        be told from an old one, so nobody is returned.
        """
        events = self._label_events(number)
        return latest_actor(events, label) if events is not None else None

    def label_actors(self, number: int, labels: list) -> dict:
        """``label -> Actor | None`` for several labels of one issue, from one read of its events (same fail-closed rule)."""
        events = self._label_events(number)
        return {label: latest_actor(events, label) if events is not None else None for label in labels}

    def permission(self, login: str) -> str:
        """The user's role on the repository (``admin``, ``maintain``, ``write``, ``triage``, ``read``) or ``none``."""
        if not _LOGIN.fullmatch(login):
            return "none"
        path = f"{repo_path(self.repo)}/collaborators/{quote(login, safe='')}/permission"
        try:
            role = self._gh(["api", path, "--jq", '.role_name // .permission // "none"'])
        except subprocess.CalledProcessError as error:
            if _is_missing(error):
                return "none"
            raise
        return role.strip()

    def pulls_by(self, login: str, max_pages: int = MAX_PULL_PAGES) -> list:
        """Numbers of the open pull requests authored by the bot account ``login`` (most recently updated first)."""
        return [number for number, _ in self.pulls_with_updates(login, max_pages)]

    def pulls_with_updates(self, login: str, max_pages: int = MAX_PULL_PAGES, bot: bool = True) -> list:
        """``[(number, updated_at)]`` of the open pull requests authored by ``login``, most recently updated first.

        Any comment or review bumps a pull request's ``updated_at``, so a caller can skip one whose value did not
        change since it last read it, without a call per pull request. ``bot=False`` is for a deployment without an
        App identity (``mention_handle``): the author is then a ``User`` account instead of a ``Bot``.
        """
        account = "Bot" if bot else "User"
        jq = (
            '.[] | {number: .number, state: .state, login: (.user.login // ""), type: (.user.type // ""), '
            'updated: (.updated_at // "")}'
        )
        found = []
        for page in range(1, max_pages + 1):
            path = (
                f"{repo_path(self.repo)}/pulls?state=open&sort=updated&direction=desc"
                f"&per_page={PAGE_SIZE}&page={page}"
            )
            rows = _lines(self._gh(["api", path, "--jq", jq]))
            for row in rows:
                number = row.get("number")
                mine = (
                    isinstance(row.get("login"), str)
                    and row["login"].casefold() == login.casefold()
                    and row.get("type") == account
                )
                updated = row.get("updated")
                if mine and row.get("state") == "open" and type(number) is int and number > 0:
                    found.append((number, updated if isinstance(updated, str) else ""))
            if len(rows) < PAGE_SIZE:
                break
        return found

    def open_pulls_of(self, max_pages: int = MAX_PULL_PAGES) -> tuple:
        """``(numbers, truncated)``: the open pull requests Factory's own App identity authored, for the open-pull-request cap.

        ``truncated`` is True when the page bound was reached with a full last page (more may exist), and ``LookupError`` is raised
        where Factory has no App identity (an ambient ``gh`` login cannot tell Factory's pull requests from a person's): the caller
        then holds. Only numbers leave ``gh``.
        """
        login = self.creator_login()
        if self.app_user_id is None or not login:
            raise LookupError("the identity of the writer is unknown")
        jq = '.[] | {number: .number, login: (.user.login // ""), type: (.user.type // "")}'
        numbers = []
        for page in range(1, max_pages + 1):
            path = f"{repo_path(self.repo)}/pulls?state=open&per_page={PAGE_SIZE}&page={page}"
            rows = _lines(self._gh(["api", path, "--jq", jq]))
            for row in rows:
                number = row.get("number")
                mine = isinstance(row.get("login"), str) and row["login"].casefold() == login.casefold() and row.get("type") == "Bot"
                if mine and type(number) is int and number > 0:  # type(), not isinstance: a bool is not a pull request number
                    numbers.append(number)
            if len(rows) < PAGE_SIZE:
                return numbers, False
        return numbers, True

    def remarks(
        self,
        number: int,
        kind: str,
        slug: str,
        max_pages: int | None = None,
        since: str | None = None,
        on_truncated: Callable[[str], None] | None = None,
    ) -> list:
        """The comments of one ``kind`` on a pull request, oldest first, paged and bounded.

        ``since`` (an ISO time) asks GitHub only for the conversation and review comments updated at or after
        it, so a long pull request is not read from its first comment on every pass; the reviews list has no
        such parameter. ``on_truncated(kind)`` is called when the page bound was reached with a full last page:
        the newest comments of that read were not seen, and the caller says so.

        Whether a text mentions ``@<slug>`` is decided inside ``gh`` (``--jq``), after a quoted line, a fenced
        block and an inline code span are cut out, and only the first ``MAX_SEARCHED_CHARS`` characters are searched
        (``gsub`` slows down with many matches, and a comment may be 65 KB of hostile markup): only the id, the author's login and type, the dates and that
        one yes/no leave it, never the text itself.
        """
        if max_pages is None:
            max_pages = MAX_REVIEW_PAGES if kind == "review" else MAX_REMARK_PAGES
        pattern = json.dumps(mention_pattern(slug))
        text = (
            f'(.body // "") | .[0:{MAX_SEARCHED_CHARS}] | gsub({json.dumps(_QUOTE)}; "\n") | gsub({json.dumps(_FENCE)}; " ") '
            f'| gsub({json.dumps(_CODE_SPAN)}; " ") | test({pattern}; "i")'
        )
        jq = (
            '.[] | {id: .id, login: (.user.login // ""), type: (.user.type // ""), '
            'updated: (.updated_at // .submitted_at // ""), created: (.created_at // .submitted_at // ""), '
            f"mention: ({text})}}"
        )
        window = f"&since={quote(since, safe='')}" if since and kind in SINCE_KINDS else ""
        found = []
        for page in range(1, max_pages + 1):
            path = (
                f"{repo_path(self.repo)}/{_REMARK_PATHS[kind].format(int(number))}"
                f"?per_page={PAGE_SIZE}&page={page}{window}"
            )
            rows = _lines(self._gh(["api", path, "--jq", jq]))
            for row in rows:
                ident, login, account, updated, created = (
                    row.get(key) for key in ("id", "login", "type", "updated", "created")
                )
                if type(ident) is int and ident > 0 and all(isinstance(v, str) for v in (login, account, updated, created)):
                    found.append(Remark(kind, ident, login, account, updated, row.get("mention") is True, created))
            if len(rows) < PAGE_SIZE:
                break
            if page == max_pages and on_truncated is not None:
                on_truncated(kind)
        return sorted(found, key=lambda remark: remark.id)

    def remark_texts(self, number: int, kind: str, after_id: int, max_pages: int | None = None) -> list:
        """The comments of one ``kind`` on a pull request with an id above ``after_id``, with their text, oldest first.

        The one read that lets a comment's words into this process, for the context of a follow-up session
        (``followup_context.py``): each text is cut to ``MAX_TEXT_CHARS`` inside ``gh``, the pages are bounded as in
        ``remarks``, and the caller treats every word of it as untrusted data.
        """
        if max_pages is None:
            max_pages = MAX_REVIEW_PAGES if kind == "review" else MAX_REMARK_PAGES
        jq = (
            f".[] | select(.id > {int(after_id)}) | {{id: .id, login: (.user.login // \"\"), type: (.user.type // \"\"), "
            f"created: (.created_at // .submitted_at // \"\"), text: ((.body // \"\") | .[0:{MAX_TEXT_CHARS}])}}"
        )
        found = []
        for page in range(1, max_pages + 1):
            path = f"{repo_path(self.repo)}/{_REMARK_PATHS[kind].format(int(number))}?per_page={PAGE_SIZE}&page={page}"
            rows = _lines(self._gh(["api", path, "--jq", jq]))
            for row in rows:
                ident, login, account, created, text = (row.get(key) for key in ("id", "login", "type", "created", "text"))
                if isinstance(ident, int) and not isinstance(ident, bool) and ident > 0 and all(isinstance(v, str) for v in (login, account, created, text)):
                    found.append(RemarkText(kind, ident, login, account, created, text))
            if len(rows) < PAGE_SIZE:
                break
        return sorted(found, key=lambda remark: remark.id)

    def failed_checks(self, sha: str) -> list:
        """The failing check runs (failure or timed out) of a commit, as ``[FailedCheck]``; at most ``MAX_FAILED_CHECKS``."""
        jq = (
            '.check_runs[] | select(.conclusion == "failure" or .conclusion == "timed_out") '
            '| {id: .id, name: (.name // ""), conclusion: .conclusion}'
        )
        path = f"{repo_path(self.repo)}/commits/{quote(str(sha), safe='')}/check-runs?per_page={PAGE_SIZE}"
        found = []
        for row in _lines(self._gh(["api", path, "--jq", jq])):
            ident, name, conclusion = row.get("id"), row.get("name"), row.get("conclusion")
            if isinstance(ident, int) and not isinstance(ident, bool) and ident > 0 and isinstance(name, str) and isinstance(conclusion, str):
                found.append(FailedCheck(name, conclusion, ident))
        return found[:MAX_FAILED_CHECKS]

    def job_log_tail(self, job_id: int, limit: int = MAX_LOG_CHARS) -> str:
        """The last ``limit`` characters of a failing job's log, or "" when there is none (not an Actions job, expired, gone).

        The log is plain text that ``gh`` follows a redirect for: it cannot be cut inside ``gh``, so it is read whole and
        only its tail is kept. A missing log is no error. Every word of it is untrusted data.
        """
        path = f"{repo_path(self.repo)}/actions/jobs/{int(job_id)}/logs"
        try:
            text = self._gh(["api", path])
        except subprocess.CalledProcessError as error:
            if _is_missing(error) or "HTTP 410" in str(error.stderr or ""):
                return ""
            raise
        return text[-limit:]

    def pull_state(self, number: int) -> str:
        """``merged``, ``closed`` (without merging) or ``open`` for one pull request of the repository.

        Only the state and the merged flag leave ``gh`` (``--jq``): never a title or a body.
        Anything else is a ``ValueError``, which the caller reports by its type.
        """
        jq = "{state: .state, merged: .merged}"
        data = json.loads(self._gh(["api", f"{repo_path(self.repo)}/pulls/{int(number)}", "--jq", jq]))
        if not isinstance(data, dict):
            raise ValueError("unexpected pull request answer")
        if data.get("merged") is True:
            return "merged"
        if data.get("state") in ("open", "closed"):
            return "open" if data["state"] == "open" else "closed"
        raise ValueError("unexpected pull request state")

    def pull_facts(self, number: int) -> "PullFacts":
        """State, head commit and body of one pull request: what completing it by hand needs.

        The body is read only to be written back with its first line changed; it is never logged.
        Anything unexpected is a ``ValueError``, which the caller reports by its type.
        """
        jq = "{state: .state, merged: .merged, head: .head.sha, body: (.body // \"\")}"
        data = json.loads(self._gh(["api", f"{repo_path(self.repo)}/pulls/{int(number)}", "--jq", jq]))
        if not (isinstance(data, dict) and isinstance(data.get("head"), str) and isinstance(data.get("body"), str)):
            raise ValueError("unexpected pull request answer")
        state = "merged" if data.get("merged") is True else data.get("state")
        if state not in ("open", "closed", "merged"):
            raise ValueError("unexpected pull request state")
        return PullFacts(state, data["head"], data["body"])

    def status_of(self, sha: str, context: str) -> tuple:
        """``(state, description)`` of the newest commit status of ``context`` on ``sha``, or ``(None, None)``.

        The combined status keeps one row per context (the newest), so repeated posts do not matter.
        Anything unexpected is a ``ValueError``, which the caller reports by its type.
        """
        return self.status_detail(sha, context)[:2]

    def status_detail(self, sha: str, context: str) -> tuple:
        """``(state, description, created_at)`` of the newest status of ``context`` on ``sha`` (None for each when there is none)."""
        jq = (
            f"[.statuses[] | select(.context == {json.dumps(context)})] | first // {{}} "
            "| {state: .state, description: .description, created: .created_at}"
        )
        path = f"{repo_path(self.repo)}/commits/{quote(str(sha), safe='')}/status?per_page=100"
        data = json.loads(self._gh(["api", path, "--jq", jq]))
        if not isinstance(data, dict):
            raise ValueError("unexpected commit status answer")
        state, description = data.get("state"), data.get("description")
        created = data.get("created")
        return (
            state if isinstance(state, str) else None,
            description if isinstance(description, str) else None,
            created if isinstance(created, str) else None,
        )

    def status_record(self, sha: str, context: str) -> tuple:
        """``(state, description, created_at, creator)`` of the newest status of ``context`` on ``sha``.

        ``creator`` is ``{"type", "id", "login"}`` of whoever wrote it (None for each when there is
        none). The combined status answer does not say who wrote a status; the list does, newest
        first, so this reads the list.
        """
        jq = (
            f"[.[] | select(.context == {json.dumps(context)})] | first // {{}} "
            "| {state: .state, description: .description, created: .created_at, "
            "ctype: .creator.type, cid: .creator.id, clogin: .creator.login}"
        )
        path = f"{repo_path(self.repo)}/commits/{quote(str(sha), safe='')}/statuses?per_page=100"
        # --paginate runs the filter once per page: the first page that has a status of this context
        # holds the newest one (the list is newest first), so a busy commit cannot push it off the page.
        data = None
        for line in self._gh(["api", "--paginate", path, "--jq", jq]).splitlines():
            if not line.strip():
                continue
            page = json.loads(line)
            if not isinstance(page, dict):
                raise ValueError("unexpected commit status answer")
            if data is None:
                data = page
            if isinstance(page.get("state"), str):
                data = page
                break
        if data is None:
            raise ValueError("unexpected commit status answer")

        def text(key):
            value = data.get(key)
            return value if isinstance(value, str) else None

        cid = data.get("cid")
        creator = {"type": text("ctype"), "id": cid if type(cid) is int else None, "login": text("clogin")}
        return text("state"), text("description"), text("created"), creator

    @property
    def app_user_id(self) -> int | None:
        """The Factory App's bot user id, or None (ambient ``gh`` login, or an identity that does not say)."""
        user_id = getattr(self._auth, "app_user_id", None)
        return user_id if type(user_id) is int and user_id > 0 else None

    def closed_pulls(self, login: str, since: str, max_pages: int = MAX_CLOSED_PULL_PAGES) -> tuple:
        """``([ClosedPull], truncated)``: closed pull requests authored by the bot ``login`` and updated since ``since``.

        Newest update first, so paging stops at the first pull request not updated since ``since`` (a pull request
        is never merged or closed after its last update). ``truncated`` is True when the page bound was reached
        before that point. Only numbers, times and the head SHA leave ``gh`` (``--jq``), never a title or a body.
        """
        jq = (
            '.[] | {number: .number, login: (.user.login // ""), type: (.user.type // ""), '
            'merged: (.merged_at // ""), closed: (.closed_at // ""), updated: (.updated_at // ""), '
            'head: (.head.sha // "")}'
        )
        found = []
        for page in range(1, max_pages + 1):
            path = (
                f"{repo_path(self.repo)}/pulls?state=closed&sort=updated&direction=desc"
                f"&per_page={PAGE_SIZE}&page={page}"
            )
            rows = _lines(self._gh(["api", path, "--jq", jq]))
            for row in rows:
                updated, number = row.get("updated"), row.get("number")
                if isinstance(updated, str) and updated and updated < since:
                    return found, False  # ISO timestamps in UTC order as text: everything after is older
                mine = (
                    isinstance(row.get("login"), str)
                    and row["login"].casefold() == login.casefold()
                    and row.get("type") == "Bot"
                )
                strings = [row.get(key) for key in ("merged", "closed", "head")]
                if mine and type(number) is int and number > 0 and all(isinstance(v, str) for v in strings):
                    found.append(ClosedPull(number, *strings))
            if len(rows) < PAGE_SIZE:
                return found, False
        return found, True

    def pull_commits(self, number: int, max_pages: int = MAX_COMMIT_PAGES) -> list:
        """The commit SHAs of a pull request, oldest first (at most ``max_pages`` pages of the list)."""
        shas: list = []
        for page in range(1, max_pages + 1):
            path = f"{repo_path(self.repo)}/pulls/{int(number)}/commits?per_page={PAGE_SIZE}&page={page}"
            rows = _lines(self._gh(["api", path, "--jq", ".[] | {sha: .sha}"]))
            shas.extend(row["sha"] for row in rows if isinstance(row.get("sha"), str))
            if len(rows) < PAGE_SIZE:
                break
        return shas

    def gate_history(self, sha: str, context: str) -> list:
        """Every status of ``context`` on ``sha`` as ``[GateStatus]``, newest first (the whole list, paged)."""
        jq = (
            f'.[] | select(.context == {json.dumps(context)}) | {{state: .state, description: (.description // ""), '
            'created: (.created_at // ""), ctype: (.creator.type // ""), cid: .creator.id, '
            'clogin: (.creator.login // "")}'
        )
        path = f"{repo_path(self.repo)}/commits/{quote(str(sha), safe='')}/statuses?per_page=100"
        found = []
        for row in _lines(self._gh(["api", "--paginate", path, "--jq", jq])):
            strings = [row.get(key) for key in ("state", "description", "created", "ctype", "clogin")]
            if all(isinstance(v, str) for v in strings):
                cid = row.get("cid")
                creator = {"type": strings[3], "id": cid if type(cid) is int else None, "login": strings[4]}
                found.append(GateStatus(strings[0], strings[1], strings[2], creator))
        return found

    def login(self) -> str:
        """The login of the identity ``gh`` is authenticated as (ambient authentication only)."""
        return self._gh(["api", "user", "--jq", ".login"]).strip()

    def set_pull_body(self, number: int, body: str) -> None:
        """Replace the description of a pull request (the only pull request write of this reader)."""
        self._gh(["api", "-X", "PATCH", f"{repo_path(self.repo)}/pulls/{int(number)}", "-f", f"body={body}"])

    def add_label(self, number: int, label: str) -> None:
        """Put a label on an issue (a label the repository does not have yet is created by GitHub)."""
        path = f"{repo_path(self.repo)}/issues/{int(number)}/labels"
        self._gh(["api", "-X", "POST", path, "-f", f"labels[]={label}"])

    def ensure_label(self, label: str, color: str, description: str) -> None:
        """Create the label with Factory's fixed colour and description (``--force``: idempotent)."""
        self._gh(["label", "create", label, "--force", "--color", color, "--description", description, "-R", self.repo])

    def _identity(self) -> tuple:
        """``(the App's bot user id or None, the ambient login in lower case or None)``: who Factory's own writes are."""
        app = self.app_user_id
        return app, (None if app is not None else self.login().strip().casefold())

    @staticmethod
    def _is_ours(identity: tuple, uid: object, login: str, account: str) -> bool:
        """Whether an author is Factory's own: the App's bot user (by id, never by name), or the ambient ``gh`` login."""
        app, me = identity
        return (account == "Bot" and uid == app) if app is not None else bool(me) and login.casefold() == me

    def _own_comment(self, number: int, wanted: str, line_prefix: str, keep_body: bool = False) -> dict | None:
        """The lowest-id comment of Factory's own on an issue whose body starts with ``wanted``, or None.

        Found by its hidden marker AND by who wrote it (``_is_ours``): a comment that only starts with the marker,
        written by a person or another App, is not it, and without a known Factory identity nobody is found.
        Only ids, the author, two times and the hidden line that starts with ``line_prefix`` leave ``gh``; the whole
        body only with ``keep_body`` (Factory's own fixed status text, for a comparison).

        Every page of comments is read, up to ``MAX_REMARK_PAGES``; when the bound is reached with a full last page
        and nothing was found, ``epic.PlanSearchTruncated`` is raised (not found would prove nothing).
        """
        # One row per comment (the filter is in Python, not in jq): a page whose comments are all someone
        # else's still counts as a full page, so the walk goes on to the page that holds the comment.
        jq = "".join(
            (
                f'.[] | (.body // "") as $b | ($b | startswith({json.dumps(wanted)})) as $m | ',
                '{id: .id, login: (.user.login // ""), type: (.user.type // ""), uid: .user.id, ',
                'created: (.created_at // ""), updated: (.updated_at // ""), match: $m, ',
                f'plan: (if $m then ($b | split("\\n")[0:3] | map(select(startswith({json.dumps(line_prefix)}))) | first // "") else "" end)',
                ', body: (if $m then $b else "" end)}' if keep_body else "}",
            )
        )
        identity = self._identity()
        best = None
        last_page_full = False
        for page in range(1, MAX_REMARK_PAGES + 1):
            path = f"{repo_path(self.repo)}/issues/{int(number)}/comments?per_page={PAGE_SIZE}&page={page}"
            rows = _lines(self._gh(["api", path, "--jq", jq]))
            for row in rows:
                if row.get("match") is not True:
                    continue
                ident, login, account = row.get("id"), row.get("login"), row.get("type")
                created, updated, plan = row.get("created"), row.get("updated"), row.get("plan")
                body = row.get("body", "")
                if not (
                    isinstance(ident, int)
                    and not isinstance(ident, bool)
                    and ident > 0
                    and all(isinstance(v, str) for v in (login, account, created, updated, plan, body))
                ):
                    continue
                if self._is_ours(identity, row.get("uid"), login, account) and (best is None or ident < best["id"]):
                    best = {"id": ident, "login": login, "created": created, "updated": updated, "plan": plan, "body": body}
            last_page_full = len(rows) >= PAGE_SIZE
            if not last_page_full:
                break
        if best is None and last_page_full:
            raise epic.PlanSearchTruncated()
        return best

    def plan_comment(self, number: int) -> "PlanComment | None":
        """Factory's own epic plan comment on an issue (the lowest id of its comments), or None.

        See ``_own_comment`` for how it is found and what leaves ``gh``. The hidden plan line is Factory's own
        sanitised text, parsed and validated again here, and counts only when it names THIS issue: a comment
        whose marker carries another issue's plan has no readable plan (the gate then requires an answer).
        """
        best = self._own_comment(number, marker(int(number), epic.KIND), "<!-- factory-epic-plan:v1 ")
        if best is None:
            return None
        parsed = epic.parse_hidden(best["plan"])
        if parsed is not None and parsed["issue"] != int(number):
            parsed = None
        return PlanComment(
            best["id"],
            best["login"],
            best["created"],
            best["updated"],
            len(parsed["questions"]) if parsed else None,
            parsed,
            epic.digest(parsed) if parsed else "",
        )

    def epic_status(self, number: int) -> "StatusComment | None":
        """Factory's own status comment on an epic's parent issue (``epic_phases.py``), or None. Same search as the plan comment."""
        found = self._own_comment(
            number, marker(int(number), "epic-status"), "<!-- factory-epic-ledger:v1 ", keep_body=True
        )
        return None if found is None else StatusComment(found["id"], found["updated"], found["plan"], found["body"])

    def creator_login(self) -> str:
        """The login Factory's own issues are listed under (``<slug>[bot]``, or the ambient ``gh`` login); "" when unknown."""
        if self.app_user_id is not None:
            slug = getattr(self._auth, "app_slug", None)
            return f"{slug}[bot]" if isinstance(slug, str) and _APP_SLUG.fullmatch(slug) else ""
        login = self.login().strip()
        return login if _LOGIN.fullmatch(login) else ""

    def _phase_row(self, row: object, identity: tuple, text: bool = False) -> "PhaseIssue | None":
        """One ``phase_issues`` or ``phase_issue`` row as a ``PhaseIssue``, or None when it is not understood."""
        if not isinstance(row, dict):
            return None
        number, state, closed = row.get("number"), row.get("state"), row.get("closed")
        login, account, title, body = (row.get(key) for key in ("login", "type", "title", "body"))
        if not (
            type(number) is int  # type(), not isinstance: a bool is not an issue number
            and number > 0
            and state in ("open", "closed")
            and all(isinstance(v, str) for v in (closed, login, account, title, body))
        ):
            return None
        ours = row.get("pr") is not True and self._is_ours(identity, row.get("uid"), login, account)
        # Nothing of a text that is not Factory's own is kept: the first line and a hash, never the text.
        line = body.split("\n", 1)[0][:200].strip() if ours else ""
        content = epic.content_hash(title, body) if ours else ""
        names = row.get("labels")
        labels = tuple(names) if isinstance(names, list) and all(isinstance(name, str) for name in names) else ()
        return PhaseIssue(number, state, closed, ours, line, content, labels if ours else (), (title, body) if text and ours else ())

    def phase_issues(self, parent: int, since: str = "", max_pages: int = MAX_PHASE_PAGES) -> tuple:
        """``([PhaseIssue], truncated)``: issues Factory's own identity opened (open or closed) whose body starts with the phase marker of ``parent``.

        The listing is narrowed to Factory's login (``creator_login``; "" there raises ``LookupError``: nothing
        is listed blind) and, with ``since``, to issues updated since then; every row's author is checked again
        by id (``PhaseIssue.ours``). Only the rows that start with the marker of this parent bring their title and
        body into this process (to be hashed and dropped; a pull request's text never does). Every listed item
        is printed, pull requests included (Factory's own pull requests are listed with its issues): the end of
        the listing is decided on the raw page, never on the rows that are kept, and ``truncated`` is True when
        the page bound was reached with a full last page: the rest was not seen.
        """
        login = self.creator_login()
        if not login:
            raise LookupError("the identity of the writer is unknown")
        prefix = f"{epic.PHASE_PREFIX}parent={int(parent)} "
        jq = "".join(
            (
                '.[] | (.body // "") as $b | (.pull_request != null) as $p | ',
                f'(($p | not) and ($b | startswith({json.dumps(prefix)}))) as $m | ',
                '{number: .number, state: .state, closed: (.closed_at // ""), login: (.user.login // ""), ',
                'type: (.user.type // ""), uid: .user.id, pr: $p, ',
                'title: (if $m then (.title // "") else "" end), body: (if $m then $b else "" end), match: $m}',
            )
        )
        window = f"&since={quote(since, safe='')}" if since else ""
        identity = self._identity()
        found = []
        for page in range(1, max_pages + 1):
            path = (
                f"{repo_path(self.repo)}/issues?state=all&creator={quote(login, safe='')}"
                f"&per_page={PAGE_SIZE}&page={page}{window}"
            )
            rows = _lines(self._gh(["api", path, "--jq", jq]))
            for row in rows:
                parsed = self._phase_row(row, identity) if isinstance(row, dict) and row.get("match") is True else None
                if parsed is not None and parsed.ours:
                    found.append(parsed)
            if len(rows) < PAGE_SIZE:
                return found, False
        return found, True

    def phase_issue(self, number: int, text: bool = False) -> "PhaseIssue | None":
        """One issue as a ``PhaseIssue`` (a ledger's child, read by number), or None when it does not exist (404 or 410).

        ``text`` keeps the title and body of an issue that is Factory's own (``PhaseIssue.text``); no other issue's text is ever kept.
        """
        jq = "".join(
            (
                '(.body // "") as $b | ($b | startswith(', json.dumps(epic.PHASE_PREFIX), ')) as $m | ',
                '{number: .number, state: .state, closed: (.closed_at // ""), login: (.user.login // ""), ',
                'type: (.user.type // ""), uid: .user.id, pr: (.pull_request != null), labels: [.labels[].name], ',
                'title: (if $m then (.title // "") else "" end), body: (if $m then $b else "" end)}',
            )
        )
        try:
            output = self._gh(["api", f"{repo_path(self.repo)}/issues/{int(number)}", "--jq", jq])
        except subprocess.CalledProcessError as error:
            if _is_missing(error) or re.search(r"\bHTTP 410\b", str(error.stderr or "")):
                return None
            raise
        found = self._phase_row(json.loads(output), self._identity(), text)
        if found is None:
            raise ValueError("unexpected issue answer")
        return found

    def closing_pulls(self, number: int) -> "ClosingPulls":
        """The pull requests that close an issue, from GraphQL (``closedByPullRequestsReferences``): numbers, states and repositories.

        Anything unexpected is a ``ValueError`` and a failed call raises: the caller then knows nothing (never
        "not merged"). ``complete`` is False when there are more references than ``CLOSING_FIRST`` or a node
        was not understood. A state other than MERGED, OPEN or CLOSED is ``UNKNOWN``, which never counts as merged.
        """
        owner, _, name = self.repo.partition("/")
        output = self._gh(
            [
                "api", "graphql", "-f", f"query={CLOSING_QUERY}", "-f", f"owner={owner}", "-f", f"name={name}",
                "-F", f"number={int(number)}", "--jq", _CLOSING_JQ,
            ]
        )
        data = json.loads(output)
        total, nodes = data.get("total") if isinstance(data, dict) else None, data.get("prs") if isinstance(data, dict) else None
        # type(), not isinstance, in the two checks below: a bool (True is 1) is never a count or a pull request number
        if type(total) is not int or total < 0 or not isinstance(nodes, list):
            raise ValueError("unexpected closing pull request answer")
        pulls, understood = [], True
        for node in nodes:
            n, state, where = (node.get(key) for key in ("n", "s", "r")) if isinstance(node, dict) else (None, None, None)
            if type(n) is int and n > 0 and isinstance(where, str) and where:
                pulls.append((n, where.casefold(), state if state in _CLOSING_STATES else "UNKNOWN"))
            else:
                understood = False
        return ClosingPulls(tuple(pulls), understood and total == len(nodes))

    def create_issue(self, title: str, body: str, labels: tuple) -> int:
        """Open a new issue with these labels (applied in the same request) and return its number.

        Only for the phase issues of an approved epic plan (``epic_phases.py``): fixed template, sanitised text.
        The arguments are an argv list for ``gh``, never a shell string, and every value goes in with ``-f`` (a raw field), never
        ``-F``, so a title or body that starts with ``@`` is sent as text and never read as a file.
        """
        args = ["api", "-X", "POST", f"{repo_path(self.repo)}/issues", "--jq", ".number", "-f", f"title={title}", "-f", f"body={body}"]
        for label in labels:
            args.extend(["-f", f"labels[]={label}"])
        number = int(self._gh(args).strip())
        if number <= 0:
            raise ValueError("unexpected issue answer")
        return number

    def issue_replies(self, number: int, after_id: int, since: str = "", max_pages: int = MAX_REMARK_PAGES) -> tuple:
        """``([Remark], truncated)``: the conversation comments of an issue with an id above ``after_id``, oldest first.

        Who wrote each (login, account type and GitHub's association) and when; never the text. ``since`` (an ISO time) asks GitHub only
        for the comments updated at or after it. ``truncated`` is True when the page bound was reached with a full
        last page: the newest comments were not seen.
        """
        jq = (
            '.[] | {id: .id, login: (.user.login // ""), type: (.user.type // ""), '
            'created: (.created_at // ""), updated: (.updated_at // ""), assoc: (.author_association // "")}'
        )
        window = f"&since={quote(since, safe='')}" if since else ""
        found = []
        for page in range(1, max_pages + 1):
            path = f"{repo_path(self.repo)}/issues/{int(number)}/comments?per_page={PAGE_SIZE}&page={page}{window}"
            rows = _lines(self._gh(["api", path, "--jq", jq]))
            for row in rows:
                ident, login, account, created, updated = (row.get(key) for key in ("id", "login", "type", "created", "updated"))
                if (
                    isinstance(ident, int)
                    and not isinstance(ident, bool)
                    and ident > after_id
                    and all(isinstance(v, str) for v in (login, account, created, updated))
                ):
                    assoc = row.get("assoc")
                    found.append(
                        Remark("issue", ident, login, account, updated, False, created, assoc if isinstance(assoc, str) else "")
                    )
            if len(rows) < PAGE_SIZE:
                return sorted(found, key=lambda remark: remark.id), False
        return sorted(found, key=lambda remark: remark.id), True

    def remove_label(self, number: int, label: str) -> None:
        """Take the trigger label off an issue; a label that is already gone is fine."""
        path = f"{repo_path(self.repo)}/issues/{number}/labels/{quote(label, safe='')}"
        try:
            self._gh(["api", "-X", "DELETE", path])
        except subprocess.CalledProcessError as error:
            if not _is_missing(error):
                raise

"""The follow-up pass: ``@<app slug>`` mentions on open Factory pull requests become durable requests.

A person with write access who comments ``@<slug> ...`` on a pull request the Factory App opened is asking for a
follow-up. This module only **records** such a request (phase 1); ``followup_serve.py`` (phase 2) turns the oldest
queued request into a session on the pull request's own branch and records the outcome in the same file, and
``followup_reply.py`` (phase 3) tells the pull request what became of a settled request. The pass is
part of the manager's trigger loop (``operations.py``). It is OFF unless a repository sets ``followup = true``.
A deployment without an App identity sets ``mention_handle``: that account name is then the mention token and the
author of the pull requests to follow, and the account's own comments are always ignored.

For every open pull request authored by the App (never a closed one, never another author's) it reads, paged and
bounded, the conversation comments, the review comments and the review bodies (``trigger.GithubReader.remarks``;
whether a text mentions the App is decided inside ``gh``, so a comment text never reaches this process). Each
comment newer than the PR's cursor gets exactly one decision, in this order:

1. ``skipped reason=bot``: the author is a bot account (Factory's own comments included), or
   ``skipped reason=self``: the author is the ``mention_handle`` account itself;
2. ``skipped reason=no-mention``: no whole-token, case-insensitive ``@<slug>``;
3. ``skipped reason=unauthorised``: the collaborator permission of the author is below
   ``followup_min_permission`` (a failing lookup raises: nothing is decided, the comment is retried next pass);
4. ``skipped reason=cap``: ``followup_max_sessions`` requests were already queued for the pull request;
5. ``skipped reason=cooldown``: the same user got a request queued less than ``followup_cooldown_seconds`` ago;
6. ``request queued``: a request record (pull request, comment id, kind, permission level, time) is added.

A comment that is seen again with another ``updated_at`` than its fingerprint was *edited*: it never queues
anything (``skipped reason=edited``, logged when the edited text mentions the App). A comment without a fingerprint
is new even below the cursor (a pending review gets its ids when started, so it can be submitted after later
comments were decided); only ids at or below the per-kind ``floor`` (raised when old fingerprints are forgotten)
count as old.

State is one file per pull request, ``<repo data dir>/followups/pr-<N>.json``: the cursors and floors, one ``updated_at``
fingerprint per comment, the per-user cooldown times, the counter of queued requests with the cap, the requests
and the phase 2 fields: per request a ``status`` (``queued``, ``running``, ``done``, ``refused``, ``failed``), the
number of ``attempts``, ``resumes`` (restarts of the supervisor of a session that stopped early), ``not_before`` (a
retry time after a refused start), a fixed ``outcome`` code and the ``commit_before`` of the session; per pull request ``served`` (the highest comment id of each kind that was handed
to a session) and the ``lock`` (``<run>/<job>/<comment id>``) of the session in progress. It is replaced atomically (temp file, fsync, rename), the directory and
the file are opened without following symlinks and must belong to this process's user, and its size is capped.
All decisions of a pull request are persisted in that one write *before* any line is logged, so a crash between
reading and persisting repeats the decisions (no loss, no duplicate), and a crash after persisting repeats nothing.
Comment text is never logged and never stored.
"""

from __future__ import annotations

import calendar
import errno
import json
import os
import re
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import trigger
from .config import (
    DEFAULT_FOLLOWUP_COOLDOWN_SECONDS,
    DEFAULT_FOLLOWUP_MAX_SESSIONS,
    DEFAULT_MIN_PERMISSION,
    MAX_FOLLOWUP_COOLDOWN_SECONDS,
    MAX_FOLLOWUP_MAX_SESSIONS,
    PERMISSION_LEVELS,
)

STATE_DIR = "followups"
STATE_VERSION = 1
MAX_STATE_BYTES = 256 * 1024
MAX_FINGERPRINTS = 3000  # the oldest are forgotten first; an old comment never triggers, known or not
MAX_REQUESTS = 100
MAX_COOLDOWNS = 200
MAX_PULLS_PER_PASS = 50  # open Factory pull requests READ per pass (most recently updated first); one whose
#                          updated_at did not change is skipped for free and does not count, so none starves
MAX_COMMENT_AGE_SECONDS = 24 * 3600  # a comment created longer ago is recorded as seen, never decided
MAX_REQUEST_AGE_SECONDS = 24 * 3600  # a request still queued after this long is refused at serve time, never started
SINCE_OVERLAP_SECONDS = 60  # the window starts this long before the newest update seen (late-visible comments)
RESCAN_SECONDS = 3600  # a pull request nothing happened on is read again after this long anyway
MAX_TIME_CHARS = 40
MAX_LOGIN_CHARS = 100
LOCK_CHARS = 200
_LOCK = re.compile(r"([A-Za-z0-9][A-Za-z0-9_-]{0,49})/([A-Za-z0-9][A-Za-z0-9_-]{0,63})/([0-9]{1,20})")
_STATE_FILE = re.compile(r"pr-([0-9]{1,9})\.json")
_HEX = re.compile(r"[0-9a-f]{0,64}")
_HANDLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")

# The status of a request, and the fixed codes recorded as its outcome (never free text, never a comment's words).
QUEUED, RUNNING, DONE, REFUSED, FAILED = "queued", "running", "done", "refused", "failed"
STATUSES = (QUEUED, RUNNING, DONE, REFUSED, FAILED)
OUTCOME_PUSHED = "pushed"  # the session made a new commit; review, verification and the gate follow as usual
OUTCOME_NO_CHANGE = "no-change"  # the session made no commit; job and issue status are unchanged
OUTCOME_FAILED = "session-failed"  # the session ended failed, blocked or parked; the job shows why
OUTCOME_LOST = "session-lost"  # the lock outlived its session (run purged, job gone, or interrupted for too long)
OUTCOME_RUN_GONE = "run-gone"  # no retained run holds the job that published the pull request
OUTCOME_HEAD_MOVED = "head-moved"  # the pull request's head is not the commit the job retained
OUTCOME_JOB_STATUS = "job-status"  # the job is not pr_ready or pr_incomplete
OUTCOME_PR_CLOSED = "pr-closed"  # the pull request is no longer open
OUTCOME_CLI_REFUSED = "cli-refused"  # the CLI refused the retry three times
OUTCOME_NOT_AUTHORISED = "not-authorised"  # the author no longer holds ``followup_min_permission`` when the session would start
OUTCOME_EXPIRED = "expired"  # the request waited longer than ``MAX_REQUEST_AGE_SECONDS`` without starting
OUTCOMES = frozenset(
    {
        "", OUTCOME_PUSHED, OUTCOME_NO_CHANGE, OUTCOME_FAILED, OUTCOME_LOST, OUTCOME_RUN_GONE,
        OUTCOME_HEAD_MOVED, OUTCOME_JOB_STATUS, OUTCOME_PR_CLOSED, OUTCOME_CLI_REFUSED, OUTCOME_NOT_AUTHORISED,
        OUTCOME_EXPIRED,
    }
)
MAX_START_ATTEMPTS = 3  # refused retries of one request before it is given up


class StateError(Exception):
    """The state file (or its directory) cannot be trusted: a symlink, a foreign owner, too big, malformed."""


@dataclass(frozen=True)
class FollowupSettings:
    enabled: bool = False
    min_permission: str = DEFAULT_MIN_PERMISSION
    cooldown: int = DEFAULT_FOLLOWUP_COOLDOWN_SECONDS
    max_sessions: int = DEFAULT_FOLLOWUP_MAX_SESSIONS
    handle: str = ""  # ``mention_handle``: the account mentioned, whose pull requests are followed, whose comments are ignored
    reply: bool = True  # ``followup_reply``: say on the pull request what became of a settled request (followup_reply.py)


def _bounded(value: object, default: int, low: int, high: int) -> int:
    ok = isinstance(value, int) and not isinstance(value, bool) and low <= value <= high
    return value if ok else default  # type: ignore[return-value]


def settings_of(cfg: object) -> FollowupSettings:
    """The follow-up settings of a repository config, through ``getattr`` like ``trigger.settings_of``.

    A value outside the contract falls to the safe side: anything but ``True`` is off, an unknown
    permission is ``admin``, a bad cooldown is the default one and a bad cap the default cap. Without the
    key a repository is off: a follow-up starts a session, so it needs an explicit ``followup = true``.
    """
    level = getattr(cfg, "followup_min_permission", DEFAULT_MIN_PERMISSION)
    handle = getattr(cfg, "mention_handle", "")
    return FollowupSettings(
        enabled=getattr(cfg, "followup", False) is True,
        min_permission=level if level in PERMISSION_LEVELS else "admin",
        cooldown=_bounded(
            getattr(cfg, "followup_cooldown_seconds", None),
            DEFAULT_FOLLOWUP_COOLDOWN_SECONDS, 0, MAX_FOLLOWUP_COOLDOWN_SECONDS,
        ),
        max_sessions=_bounded(
            getattr(cfg, "followup_max_sessions", None), DEFAULT_FOLLOWUP_MAX_SESSIONS, 1, MAX_FOLLOWUP_MAX_SESSIONS
        ),
        handle=handle if isinstance(handle, str) and _HANDLE.fullmatch(handle) else "",
        reply=getattr(cfg, "followup_reply", True) is True,
    )


# --- the state file ------------------------------------------------------------------


def _epoch(text: object) -> float | None:
    """Seconds since the epoch of an ISO ``YYYY-MM-DDTHH:MM:SSZ`` time, or None when it is anything else."""
    if not (isinstance(text, str) and len(text) == 20):
        return None
    try:
        return float(calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ")))
    except ValueError:
        return None


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(max(epoch, 0)))


def new_state(number: int) -> dict:
    return {
        "version": STATE_VERSION,
        "pr": number,
        "since": {kind: "" for kind in trigger.REMARK_KINDS},  # newest ``updated`` read per kind (the next window)
        "pr_updated": "",  # the pull request's own updated_at when it was last read completely
        "read_at": 0,  # and when
        "cursors": {kind: 0 for kind in trigger.REMARK_KINDS},
        "floors": {kind: 0 for kind in trigger.REMARK_KINDS},
        "fingerprints": {},
        "cooldowns": {},
        "sessions": 0,
        "max_sessions": None,
        "requests": [],
        "served": {kind: 0 for kind in trigger.REMARK_KINDS},  # highest comment id handed to a session, per kind
        "lock": None,  # ``<run>/<job>/<comment id>`` of the session in progress on this pull request
    }


def _count(value: object) -> bool:
    return type(value) is int and value >= 0


REQUEST_EXTRA = ("status", "attempts", "resumes", "not_before", "started_at", "outcome", "commit_before")


def _extras_ok(extra: dict) -> bool:
    """The phase 2 fields of one request, when present: a known status and outcome, bounded text, plain counts."""
    return (
        extra.get("status", QUEUED) in STATUSES
        and _count(extra.get("attempts", 0))
        and _count(extra.get("resumes", 0))
        and all(isinstance(extra.get(key, ""), str) and len(extra.get(key, "")) <= MAX_TIME_CHARS for key in ("not_before", "started_at"))
        and extra.get("outcome", "") in OUTCOMES
        and isinstance(extra.get("commit_before", ""), str)
        and _HEX.fullmatch(extra.get("commit_before", "")) is not None
    )


def make_lock(run: str, job: str, comment_id: int) -> str:
    return f"{run}/{job}/{int(comment_id)}"


def parse_lock(text: object) -> tuple | None:
    """``(run, job, comment id)`` of a lock, or None for anything that is not exactly what ``make_lock`` writes."""
    found = _LOCK.fullmatch(text) if isinstance(text, str) else None
    return (found[1], found[2], int(found[3])) if found else None


def _validated(document: object, number: int) -> dict:
    """The state of a file as a clean dict, or ``StateError`` for anything that is not exactly what we wrote."""
    if not (isinstance(document, dict) and document.get("version") == STATE_VERSION and document.get("pr") == number):
        raise StateError("unexpected state document")
    state = new_state(number)
    cursors = document.get("cursors")
    if not (isinstance(cursors, dict) and all(_count(cursors.get(kind, 0)) for kind in trigger.REMARK_KINDS)):
        raise StateError("bad cursors")
    state["cursors"] = {kind: cursors.get(kind, 0) for kind in trigger.REMARK_KINDS}
    floors = document.get("floors", {})  # absent in a file written before floors existed: nothing was forgotten
    if not (isinstance(floors, dict) and all(_count(floors.get(kind, 0)) for kind in trigger.REMARK_KINDS)):
        raise StateError("bad floors")
    state["floors"] = {kind: floors.get(kind, 0) for kind in trigger.REMARK_KINDS}
    since = document.get("since", {})  # absent in a file written before the windows existed
    if not (
        isinstance(since, dict)
        and all(isinstance(since.get(kind, ""), str) and len(since.get(kind, "")) <= MAX_TIME_CHARS for kind in trigger.REMARK_KINDS)
    ):
        raise StateError("bad windows")
    state["since"] = {kind: since.get(kind, "") for kind in trigger.REMARK_KINDS}
    pr_updated, read_at = document.get("pr_updated", ""), document.get("read_at", 0)
    if not (isinstance(pr_updated, str) and len(pr_updated) <= MAX_TIME_CHARS and type(read_at) in (int, float) and read_at >= 0):
        raise StateError("bad read marker")
    state["pr_updated"], state["read_at"] = pr_updated, read_at
    prints = document.get("fingerprints")
    if not (isinstance(prints, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in prints.items())):
        raise StateError("bad fingerprints")
    state["fingerprints"] = dict(prints)
    cooldowns = document.get("cooldowns")
    if not (
        isinstance(cooldowns, dict)
        and all(isinstance(k, str) and type(v) in (int, float) for k, v in cooldowns.items())
    ):
        raise StateError("bad cooldowns")
    state["cooldowns"] = dict(cooldowns)
    if not _count(document.get("sessions", 0)):
        raise StateError("bad session counter")
    state["sessions"] = document.get("sessions", 0)
    cap = document.get("max_sessions")
    if cap is not None and not _count(cap):
        raise StateError("bad session cap")
    state["max_sessions"] = cap
    requests = document.get("requests")
    if not (isinstance(requests, list) and len(requests) <= MAX_REQUESTS):
        raise StateError("bad requests")
    for item in requests:
        ok = (
            isinstance(item, dict)
            and item.get("pr") == number
            and item.get("kind") in trigger.REMARK_KINDS
            and type(item.get("comment_id")) is int
            and all(isinstance(item.get(key), str) for key in ("user", "permission", "queued_at"))
        )
        if not ok:
            raise StateError("bad request")
        extra = {key: item[key] for key in REQUEST_EXTRA if key in item}
        if not _extras_ok(extra):
            raise StateError("bad request")
        state["requests"].append(
            {**{key: item[key] for key in ("pr", "kind", "comment_id", "user", "permission", "queued_at")}, **extra}
        )
    served = document.get("served", {})  # absent in a file written by phase 1: nothing was handed to a session
    if not (isinstance(served, dict) and all(_count(served.get(kind, 0)) for kind in trigger.REMARK_KINDS)):
        raise StateError("bad served cursors")
    state["served"] = {kind: served.get(kind, 0) for kind in trigger.REMARK_KINDS}
    lock = document.get("lock")
    if not (lock is None or (isinstance(lock, str) and len(lock) <= LOCK_CHARS and parse_lock(lock) is not None)):
        raise StateError("bad lock")
    state["lock"] = lock
    return state


def state_name(number: int) -> str:
    return f"pr-{int(number)}.json"


def _check_directory(descriptor: int) -> None:
    info = os.fstat(descriptor)
    if info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise StateError("state directory is not private to this user")


def _open_directory(directory: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        descriptor = os.open(directory, flags)
    except FileNotFoundError:
        raise
    except OSError as error:  # a symlink (ELOOP), not a directory, no access
        raise StateError(f"cannot open the state directory: {errno.errorcode.get(error.errno, 'error')}") from None
    try:
        _check_directory(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def load_state(directory: Path, number: int) -> dict:
    """The state of pull request ``number`` (a fresh one when there is none); ``StateError`` when it cannot be trusted."""
    try:
        dir_fd = _open_directory(directory)
    except FileNotFoundError:
        return new_state(number)
    try:
        try:
            descriptor = os.open(
                state_name(number), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd
            )
        except FileNotFoundError:
            return new_state(number)
        except OSError as error:  # a symlink (ELOOP) or a file we cannot open
            raise StateError(f"cannot open the state file: {errno.errorcode.get(error.errno, 'error')}") from None
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise StateError("the state file is not a regular file")
            if info.st_uid != os.geteuid() or info.st_mode & 0o022:
                raise StateError("the state file is not private to this user")
            if info.st_size > MAX_STATE_BYTES:
                raise StateError("the state file is too large")
            raw = handle.read(MAX_STATE_BYTES + 1)
    finally:
        os.close(dir_fd)
    if len(raw) > MAX_STATE_BYTES:
        raise StateError("the state file is too large")
    try:
        document = json.loads(raw)
    except (ValueError, RecursionError):
        raise StateError("the state file is not JSON") from None
    return _validated(document, number)


def _serialise(state: dict) -> bytes:
    """The file's bytes; the oldest fingerprints go first when the size cap would be exceeded."""
    while True:
        data = json.dumps(state, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        if len(data) <= MAX_STATE_BYTES:
            return data
        prints = state["fingerprints"]
        if not prints:
            raise StateError("the state does not fit the size cap")
        _forget(state, sorted(prints, key=_fingerprint_order)[: max(1, len(prints) // 4)])


def _fingerprint_order(key: str) -> tuple:
    kind, _, ident = key.partition(":")
    return (int(ident) if ident.isdigit() else 0, kind)


def _forget(state: dict, keys: list) -> None:
    """Drop fingerprints and raise the floor of their kind: an id at or below the floor is old, whether known or not."""
    for key in keys:
        kind, _, ident = key.partition(":")
        del state["fingerprints"][key]
        if kind in state["floors"] and ident.isdigit():
            state["floors"][kind] = max(state["floors"][kind], int(ident))


def save_state(root: Path, number: int, state: dict) -> None:
    """Replace ``<root>/followups/pr-<N>.json`` atomically: temp file next to it, fsync, rename, fsync the directory.

    ``root`` is the repository's data directory. Nothing is followed through a symlink and a directory or file of
    another owner is refused; a symlink in place of the file is refused rather than replaced.
    """
    data = _serialise(state)
    directory = root / STATE_DIR
    os.makedirs(root, mode=0o700, exist_ok=True)
    try:
        os.mkdir(directory, 0o700)
    except FileExistsError:
        pass
    dir_fd = _open_directory(directory)
    name, temp = state_name(number), f".{state_name(number)}.{secrets.token_hex(4)}.tmp"
    try:
        try:
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                raise StateError("the state file is not a regular file of this user")
        descriptor = os.open(
            temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dir_fd
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except BaseException:
            try:
                os.unlink(temp, dir_fd=dir_fd)
            except FileNotFoundError:
                pass
            raise
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


# --- decisions -----------------------------------------------------------------------


def _decide(
    state: dict, remark: trigger.Remark, now: float, conf: FollowupSettings, permission: Callable[[str], str]
) -> str:
    """The outcome for one comment newer than the cursor; may raise (a failing permission lookup)."""
    if remark.is_bot:
        return "skipped reason=bot"
    if conf.handle and remark.login.casefold() == conf.handle.casefold():
        return "skipped reason=self"  # the handle account's own replies must never start a session
    if not remark.mention:
        return "skipped reason=no-mention"
    level = permission(remark.login)
    if not trigger.permission_ok(level, conf.min_permission):
        return "skipped reason=unauthorised"
    if state["sessions"] >= conf.max_sessions or len(state["requests"]) >= MAX_REQUESTS:
        return "skipped reason=cap"
    user = remark.login.casefold()
    last = state["cooldowns"].get(user)
    if last is not None and now - last < conf.cooldown:
        return "skipped reason=cooldown"
    state["sessions"] += 1
    state["cooldowns"][user] = now
    state["requests"].append(
        {
            "pr": state["pr"],
            "kind": remark.kind,
            "comment_id": remark.id,
            "user": remark.login[:MAX_LOGIN_CHARS],
            "permission": level,
            "queued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        }
    )
    return "request queued"


def evaluate(
    state: dict, remarks: dict, now: float, conf: FollowupSettings, permission: Callable[[str], str]
) -> tuple:
    """Decide every comment in ``remarks`` (``{kind: [Remark, ...]}``, oldest first) and update ``state`` in place.

    Returns ``(outcomes, error)``: one outcome text per decision, in order, and the exception that stopped the
    pass early (a permission lookup that failed) or None. A comment whose decision failed is not recorded: the
    cursor stays before it, so the next pass decides it again (fail closed, nothing lost).
    """
    outcomes: list = []
    prints = state["fingerprints"]
    cutoff = now - MAX_COMMENT_AGE_SECONDS
    baseline = 0
    for kind in trigger.REMARK_KINDS:
        for remark in remarks.get(kind, ()):
            key = f"{kind}:{remark.id}"
            known = prints.get(key)
            if known is not None:  # decided before: only a changed updated_at is of interest, and it never queues
                if known != remark.updated:
                    prints[key] = remark.updated
                    if remark.mention and not remark.is_bot:
                        outcomes.append("skipped reason=edited")
                continue
            if remark.id <= state["floors"][kind]:
                continue  # older than anything still remembered: old, whether it was decided or not
            # Unknown and above the floor: new, even below the cursor (ids of a pending review are assigned when the
            # review is started, so it can be submitted after later comments were decided) ...
            born = _epoch(remark.created or remark.updated)
            if born is None or born < cutoff:
                # ... unless it was written before the bound (or its age cannot be told): the history that was
                # already there when the pass first saw the pull request is a baseline, never a request. It is
                # recorded as seen, so a later edit of it is an edit and not new.
                prints[key] = remark.updated
                state["cursors"][kind] = max(state["cursors"][kind], remark.id)
                baseline += 1
                continue
            try:
                outcomes.append(_decide(state, remark, now, conf, permission))
            except Exception as error:
                if baseline:
                    outcomes.append(f"baseline comments={baseline}")
                return outcomes, error
            prints[key] = remark.updated
            state["cursors"][kind] = max(state["cursors"][kind], remark.id)
    if baseline:
        outcomes.append(f"baseline comments={baseline}")
    return outcomes, None


def _tidy(state: dict, now: float, conf: FollowupSettings) -> None:
    """Bound what the file keeps: expired cooldowns go, and the oldest of the rest and of the fingerprints."""
    cooldowns = state["cooldowns"]
    for user in [user for user, last in cooldowns.items() if now - last >= conf.cooldown]:
        del cooldowns[user]
    for user in sorted(cooldowns, key=cooldowns.get)[: max(0, len(cooldowns) - MAX_COOLDOWNS)]:
        del cooldowns[user]
    prints = state["fingerprints"]
    _forget(state, sorted(prints, key=_fingerprint_order)[: max(0, len(prints) - MAX_FINGERPRINTS)])


def follow_pull(
    reader: object,
    root: Path,
    number: int,
    slug: str,
    now: float,
    conf: FollowupSettings,
    permission: Callable[[str], str],
    line: Callable[[str], None],
    updated: str = "",
) -> bool:
    """One pull request: read, decide, persist, then log. True when it was read; raises on a failure.

    ``updated`` is the pull request's own ``updated_at`` from the listing. When it is the value of the last
    complete read, and that read was less than ``RESCAN_SECONDS`` ago, nothing happened on the pull request and
    it is skipped without a call (False). Otherwise only the comments updated since the last read are asked for
    (``since``, with an overlap and never further back than ``MAX_COMMENT_AGE_SECONDS``).

    The lines of the decisions are written only after the state is on disk, so a line always has a durable
    record behind it. A permission lookup that failed is raised after the decisions before it were persisted.
    """
    state = load_state(root / STATE_DIR, number)
    if updated and state["pr_updated"] == updated and now - state["read_at"] < RESCAN_SECONDS:
        return False
    before = json.dumps(state, sort_keys=True)
    truncated: list = []
    remarks = {}
    for kind in trigger.REMARK_KINDS:
        seen = _epoch(state["since"][kind])
        start = max(now - MAX_COMMENT_AGE_SECONDS, (seen - SINCE_OVERLAP_SECONDS) if seen is not None else 0)
        remarks[kind] = reader.remarks(number, kind, slug, since=_iso(start), on_truncated=truncated.append)
    outcomes, error = evaluate(state, remarks, now, conf, permission)
    outcomes += [f"truncated kind={kind}" for kind in truncated]
    if error is None:
        # Only a complete read moves the windows: a failed lookup repeats the decisions next pass.
        for kind, rows in remarks.items():
            newest = max((remark.updated for remark in rows if _epoch(remark.updated) is not None), default="")
            if newest > state["since"][kind]:
                state["since"][kind] = newest
        state["pr_updated"], state["read_at"] = updated, now
    if json.dumps(state, sort_keys=True) != before:
        state["max_sessions"] = conf.max_sessions
        _tidy(state, now, conf)
        save_state(root, number, state)
    for outcome in outcomes:
        line(outcome)
    if error is not None:
        raise error
    return True


def run_pass(
    reader: object,
    repo: str,
    root: Path,
    slug: str,
    now: float,
    conf: FollowupSettings,
    line: Callable[[int, str], None],
    failed: Callable[[int, Exception], None],
) -> int:
    """Follow every open pull request authored by the App ``slug`` (or by the account ``mention_handle``); how many were read.

    With ``mention_handle`` set, ``slug`` is that handle: the pull requests are those of the *user* account, whose own
    comments are skipped (``skipped reason=self``). Otherwise they are those of the bot ``<slug>[bot]``.

    ``line(number, outcome)`` reports a decision and ``failed(number, error)`` a pull request that could not be
    followed (the others still are). A failure to list the pull requests raises. The listing is most recently
    updated first, and a pull request nothing happened on is skipped without a call and without using the
    per-pass budget (``MAX_PULLS_PER_PASS``), so an old quiet pull request never starves a busy one.
    """
    listed = reader.pulls_with_updates(slug, bot=False) if conf.handle else reader.pulls_with_updates(f"{slug}[bot]")
    levels: dict = {}

    def permission(login: str) -> str:
        if login not in levels:
            levels[login] = reader.permission(login)  # a failure is not cached: the next comment asks again
        return levels[login]

    read = 0
    for number, updated in listed:
        if read >= MAX_PULLS_PER_PASS:
            break  # the rest wait for the next pass: the listing is most recently updated first
        try:
            if follow_pull(reader, root, number, slug, now, conf, permission, lambda text, n=number: line(n, text), updated):
                read += 1
        except Exception as error:
            failed(number, error)
            read += 1
    return read


# --- the phase 2 view of the files ----------------------------------------------------


def numbers_with_state(root: Path) -> list:
    """The pull requests that have a state file, lowest number first (a missing directory means none)."""
    try:
        names = os.listdir(root / STATE_DIR)
    except FileNotFoundError:
        return []
    return sorted(int(found[1]) for found in map(_STATE_FILE.fullmatch, names) if found)


def mutate(root: Path, number: int, change: Callable[[dict], None]) -> dict:
    """Load a pull request's state, apply ``change`` and replace the file. The trigger pass is the only writer."""
    state = load_state(root / STATE_DIR, number)
    change(state)
    save_state(root, number, state)
    return state


def status_of(request: dict) -> str:
    return request.get("status", QUEUED)


def request_for(state: dict, comment_id: int) -> dict | None:
    return next((item for item in state["requests"] if item["comment_id"] == comment_id), None)


def due(request: dict, now: float) -> bool:
    """Whether a queued request may be served now (``not_before`` is the end of the backoff after a refused start)."""
    if status_of(request) != QUEUED:
        return False
    wait = _epoch(request.get("not_before", ""))
    return wait is None or now >= wait


def after(now: float, seconds: float) -> str:
    return _iso(now + seconds)


def stamp(now: float) -> str:
    return _iso(now)


def age(text: str, now: float) -> float | None:
    """Seconds since an ISO time of this file, or None when it is unset or unreadable."""
    moment = _epoch(text)
    return None if moment is None else now - moment

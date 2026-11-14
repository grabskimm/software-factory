"""An append-only history ledger that outlives ``purge`` (docs/manager.md, "History ledger").

``state.json`` and the logs go with the run directory: the janitor purges a run shortly after its
jobs are merged or closed. The ledger is what remains. It lives in ``${FACTORY_DATA}/history``,
outside ``repos/<org>/<name>``, so neither ``purge`` nor ``purge-clone`` touches it, as monthly
segments of JSON lines. Writers (the supervisor, the CLI one-shots and the manager) append single
lines under an advisory lock; readers take no lock and ignore a torn last line.

What it holds is only ids, statuses, numbers, PR URLs, reason codes (``reason_codes.ORDER``), costs,
token counts and durations. Never agent-authored text (job errors, blockers, review findings,
reasons), issue or PR text, transcripts or prompts, and never a credential or secret of any kind
(API keys, access tokens, signatures). Token COUNTS (the input, output, cache write and cache read
tokens of a phase's sessions, ``status.TOKEN_FIELDS``) are numbers, not secrets, and are recorded:
every field is validated against a fixed vocabulary or a strict pattern before it is written, so the
rule that agent text never leaves the run directory still holds. Writing is best effort and never
raises: a ledger that cannot be written costs history, not the run. It is also opt-in: nothing is
written unless the repository is a managed one and ``FACTORY_DATA`` is set (``recorder_for``).

Bounded size: a total byte cap and a retention in days, both configurable (``FACTORY_HISTORY_MAX_BYTES``,
``FACTORY_HISTORY_RETENTION_DAYS``). The oldest segments are removed first.

Stdlib only.
"""

import contextlib
import fcntl
import json
import math
import os
import re
import stat
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import reason_codes
from .config import RUN_SLUG, check_repo, check_run_slug, data_paths
from .phases import PHASES
from .state import elapsed
from .status import CACHE_LIFETIME_FIELDS, TOKEN_FIELDS, cost_usd, token_count, token_totals, usage_tokens

VERSION = 1
HISTORY_DIR = "history"
LOCK_NAME = "history.lock"
SEGMENT = re.compile(r"events-(\d{6})-(\d{3})\.jsonl")
MAX_SEQUENCE = 999
MAX_LINE = 16 * 1024
MIN_SEGMENT_BYTES = 256

ENV_MAX_BYTES = "FACTORY_HISTORY_MAX_BYTES"
ENV_RETENTION_DAYS = "FACTORY_HISTORY_RETENTION_DAYS"
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
MIN_MAX_BYTES = 1024 * 1024
MAX_MAX_BYTES = 8 * 1024**3
DEFAULT_RETENTION_DAYS = 365
MIN_RETENTION_DAYS = 1
MAX_RETENTION_DAYS = 3650
# The most one read looks at, whatever the cap: a request never reads gigabytes.
DEFAULT_READ_BYTES = 8 * 1024 * 1024

MAX_ISSUES = 50
MAX_JOBS = 50
MAX_OPERATORS = 20
MAX_TIMELINE = 200
MAX_PRINCIPAL = 128
# The token count names an entry may carry: the four counts of a session and the cache lifetime split.
TOKEN_NAMES = (*TOKEN_FIELDS, *CACHE_LIFETIME_FIELDS)

JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_WORD = re.compile(r"[a-z][a-z0-9_]{0,31}")
_PHASE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_ACTION = re.compile(r"[a-z][a-z-]{0,31}")
_PR_URL = re.compile(r"https://[A-Za-z0-9.-]{1,100}/[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}/pull/[0-9]{1,9}")

KINDS = frozenset({"planned", "run_started", "run_finished", "job", "pr_observed", "purged", "operator"})
RUN_RESULTS = ("drained", "stopped", "interrupted")
PURGE_REASONS = ("terminal", "abandoned", "manual")

# Outcomes of a job or a run once it is over. ``stopped`` is the single-job stop (``factory stop``: the job's
# ``job`` entry names who stopped it); a job whose status says nothing final has no outcome (None) until it is purged.
OUTCOMES = ("merged", "closed", "blocked", "abandoned", "stopped")
_FINAL = ("merged", "closed")
OUTCOME_OF_STATUS = {
    "merged": "merged",
    "closed": "closed",
    "stopped": "stopped",
    "blocked": "blocked",
    "failed": "blocked",
    "needs_human_publish": "blocked",
}
# The least successful outcome of a run's jobs decides the outcome of the run.
RUN_OUTCOME_ORDER = ("stopped", "abandoned", "blocked", "closed", "merged")


# --- limits ------------------------------------------------------------------------


@dataclass(frozen=True)
class Limits:
    """The size and age bounds of the ledger."""

    max_bytes: int = DEFAULT_MAX_BYTES
    retention_days: int = DEFAULT_RETENTION_DAYS

    @property
    def segment_bytes(self):
        """A segment is closed at this size, so the oldest data is dropped in eighths of the cap."""
        return max(MIN_SEGMENT_BYTES, self.max_bytes // 8)


def _whole(env, name, default, low, high):
    """The integer in ``env[name]`` within [low, high], or ``default`` for anything else."""
    text = str(env.get(name) or "").strip()
    if not text:
        return default
    try:
        value = float(text)
    except ValueError:
        return default
    if not math.isfinite(value) or value != int(value) or not low <= value <= high:
        return default
    return int(value)


def limits_from_env(env=None):
    """The limits both the manager and the CLI use; an unusable value means its default (never an error here:
    the manager rejects a bad value at start, and the CLI must not fail over bookkeeping)."""
    env = os.environ if env is None else env
    return Limits(
        _whole(env, ENV_MAX_BYTES, DEFAULT_MAX_BYTES, MIN_MAX_BYTES, MAX_MAX_BYTES),
        _whole(env, ENV_RETENTION_DAYS, DEFAULT_RETENTION_DAYS, MIN_RETENTION_DAYS, MAX_RETENTION_DAYS),
    )


# --- field validation ----------------------------------------------------------------
# Every value that reaches the ledger passes one of these. A value that does not fit is dropped.


def is_int(value):
    """An integer that is not a bool (``true`` must not pass for a count or an id)."""
    return isinstance(value, int) and not isinstance(value, bool)


def count_of(value):
    return value if is_int(value) and 0 <= value < 2**31 else None


def number_of(value):
    """A finite, non-negative int or float as a float; None for anything else, a bool or an int too large for a float included."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value) or value < 0:
            return None
        return round(float(value), 6)
    except OverflowError:  # an int beyond the range of a float is hostile input, not a number
        return None


def tokens_of(value):
    """The token counts of one entry: a dict of the known count names (``TOKEN_NAMES``) whose values are valid counts.

    A name that is missing or not a non-negative int is dropped (a bool is not one); None when nothing is left.
    """
    if not isinstance(value, dict):
        return None
    found = {name: token_count(value.get(name)) for name in TOKEN_NAMES}
    return {name: count for name, count in found.items() if count is not None} or None


def phase_tokens_of(value):
    """The ``tokens`` of a ``job`` entry: ``{phase: counts}`` for the phases of ``phases.PHASES`` only; None when empty.

    A key that is not a phase is dropped, so the entry has at most one row per phase whatever it was given.
    """
    if not isinstance(value, dict):
        return None
    found = {phase: tokens_of(value.get(phase)) for phase in PHASES}
    return {phase: counts for phase, counts in found.items() if counts is not None} or None


def word_of(value):
    return value if isinstance(value, str) and _WORD.fullmatch(value) else None


def phase_of(value):
    return value if isinstance(value, str) and _PHASE.fullmatch(value) else None


def job_id_of(value):
    return value if isinstance(value, str) and JOB_ID.fullmatch(value) else None


def pr_url_of(value):
    return value if isinstance(value, str) and len(value) <= 300 and _PR_URL.fullmatch(value) else None


def code_of(value):
    return value if value in reason_codes.ORDER else None


def issues_of(value):
    numbers = [n for n in value if is_int(n) and 0 < n < 2**31] if isinstance(value, list) else []
    return numbers[:MAX_ISSUES]


def stamp_of(value):
    return value if isinstance(value, str) and epoch_of(value) is not None else None


def epoch_of(text):
    """Seconds since the epoch of an ISO 8601 timestamp, or None."""
    if not isinstance(text, str) or len(text) > 40:
        return None
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def clean(fields):
    return {key: value for key, value in fields.items() if value is not None and value != []}


def job_view(job_id, status, handoff_status=None, pr=None, reason_code=None):
    """One job of a ``planned`` or ``purged`` event: ids and vocabulary words only."""
    return clean(
        {
            "id": job_id_of(job_id),
            "status": word_of(status),
            "handoff": word_of(handoff_status),
            "pr": pr_url_of(pr),
            "code": code_of(reason_code),
        }
    )


def job_views(rows):
    """``job_view`` of each of ``rows`` (dicts with ``id``, ``status`` and optionally the other fields)."""
    views = []
    for row in rows if isinstance(rows, list) else ():
        if isinstance(row, dict):
            view = job_view(
                row.get("id"), row.get("status"), row.get("handoff_status"), row.get("pr"), row.get("reason_code")
            )
            if "id" in view and "status" in view:
                views.append(view)
        if len(views) >= MAX_JOBS:
            break
    return views


def principal_text(value):
    """The authenticated principal as written to the ledger: printable, clipped."""
    return "".join(ch for ch in str(value)[:MAX_PRINCIPAL] if ch.isprintable())


# --- the ledger ----------------------------------------------------------------------


@dataclass
class Snapshot:
    """What one read of the ledger returned: events oldest first, and whether older ones were left unread."""

    events: list
    truncated: bool
    bytes_read: int


class Ledger:
    """Append-only JSON lines under ``<data>/history``; safe for concurrent processes."""

    def __init__(self, data_dir, limits=None, clock=time.time):
        self.dir = Path(data_dir) / HISTORY_DIR
        self.limits = limits or Limits()
        self._clock = clock

    def _open_dir(self, create):
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            return os.open(self.dir, flags)
        except FileNotFoundError:
            if not create:
                raise
        with contextlib.suppress(FileExistsError):
            os.mkdir(self.dir, 0o700)
        return os.open(self.dir, flags)

    @staticmethod
    def _names(directory):
        return sorted(name for name in os.listdir(directory) if SEGMENT.fullmatch(name))

    @staticmethod
    def _stat(directory, name):
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        return info if stat.S_ISREG(info.st_mode) else None

    def _now(self):
        return datetime.fromtimestamp(self._clock(), timezone.utc).isoformat().replace("+00:00", "Z")

    # -- writing --------------------------------------------------------------------

    def append(self, kind, repo=None, run=None, **fields):
        """Write one event; True when it was written. Never raises: the ledger is best effort."""
        event = {"v": VERSION, "at": self._now(), "kind": kind}
        if repo is not None:
            event["repo"] = repo
        if run is not None:
            event["run"] = run
        event.update({key: value for key, value in fields.items() if value is not None})
        try:
            data = f"{json.dumps(event, separators=(',', ':'))}\n".encode("ascii")
            if len(data) > MAX_LINE:
                return False
            directory = self._open_dir(create=True)
        except (OSError, TypeError, ValueError):
            return False
        try:
            lock = os.open(LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX)
                self._write(directory, data)
            finally:
                os.close(lock)  # closing releases the lock
        except OSError:
            return False
        finally:
            os.close(directory)
        return True

    def _write(self, directory, data):
        name, rotated = self._active(directory)
        if rotated:
            self._prune(directory, spare_newest=False)
        handle = os.open(
            name, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory
        )
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(handle, view) :]
        finally:
            os.close(handle)

    def _active(self, directory):
        """(segment to append to, whether it starts a new one). A segment closes at its size or at the month's end."""
        month = time.strftime("%Y%m", time.gmtime(self._clock()))
        names = self._names(directory)
        if not names:
            return f"events-{month}-001.jsonl", True
        last = SEGMENT.fullmatch(names[-1])
        try:
            info = self._stat(directory, names[-1])
        except FileNotFoundError:  # removed by a prune in another process since the listing
            info = None
        size = info.st_size if info is not None else self.limits.segment_bytes
        if last.group(1) >= month and (size < self.limits.segment_bytes or int(last.group(2)) >= MAX_SEQUENCE):
            return names[-1], False
        if last.group(1) >= month:  # the same (or, with a clock gone backwards, a later) month: next number
            return f"events-{last.group(1)}-{int(last.group(2)) + 1:03d}.jsonl", True
        return f"events-{month}-001.jsonl", True

    def _prune(self, directory, spare_newest):
        """Remove segments older than the retention, then the oldest while the total is over the cap.

        On rotation the cap leaves room for the segment about to start; the newest segment is only kept
        when ``spare_newest`` (an explicit prune never empties the ledger because of its size).
        """
        cutoff = self._clock() - self.limits.retention_days * 86400
        segments = []
        for name in self._names(directory):
            try:
                info = self._stat(directory, name)
            except OSError:
                continue
            if info is None:
                continue
            if info.st_mtime < cutoff:
                self._remove(directory, name)
            else:
                segments.append((name, info.st_size))
        budget = self.limits.max_bytes - (0 if spare_newest else self.limits.segment_bytes)
        total = sum(size for _, size in segments)
        floor = 1 if spare_newest else 0
        while total > budget and len(segments) > floor:
            name, size = segments.pop(0)
            self._remove(directory, name)
            total -= size

    @staticmethod
    def _remove(directory, name):
        try:
            os.unlink(name, dir_fd=directory)
        except OSError:
            return False
        return True

    def prune(self):
        """Apply the retention and the size cap now (the janitor does it once per pass); never raises."""
        try:
            directory = self._open_dir(create=False)
        except OSError:
            return
        try:
            lock = os.open(LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX)
                self._prune(directory, spare_newest=True)
            finally:
                os.close(lock)
        except OSError:
            return
        finally:
            os.close(directory)

    # -- reading --------------------------------------------------------------------

    def signature(self):
        """A value that changes whenever a segment is added, removed or written: ``()`` for no ledger."""
        try:
            directory = self._open_dir(create=False)
        except OSError:
            return ()
        try:
            parts = []
            for name in self._names(directory):
                try:
                    info = self._stat(directory, name)
                except OSError:
                    continue
                if info is not None:
                    parts.append((name, info.st_size, info.st_mtime_ns))
            return tuple(parts)
        except OSError:
            return ()
        finally:
            os.close(directory)

    def describe(self):
        """Size facts for the dashboard: the bytes on disk and the configured bounds."""
        sizes = [size for _, size, _ in self.signature()]
        return {
            "segments": len(sizes),
            "bytes": sum(sizes),
            "max_bytes": self.limits.max_bytes,
            "retention_days": self.limits.retention_days,
        }

    def read(self, max_bytes=DEFAULT_READ_BYTES):
        """The newest events within ``max_bytes`` of ledger data, oldest first. Never raises.

        Segments are read newest first and the last one read may be cut at a line boundary: ``truncated``
        says that older events exist that were not read. Lines that are not a valid event are skipped
        (a torn last line, a newer format).
        """
        try:
            directory = self._open_dir(create=False)
        except OSError:
            return Snapshot([], False, 0)
        chunks = []
        remaining = max_bytes
        truncated = False
        try:
            names = self._names(directory)
            for index in range(len(names) - 1, -1, -1):
                if remaining <= 0:
                    truncated = True
                    break
                data, cut = self._read_tail(directory, names[index], remaining)
                remaining -= len(data)
                chunks.append(data)
                truncated = truncated or cut
        except OSError:
            truncated = True
        finally:
            os.close(directory)
        events = []
        for data in reversed(chunks):
            events.extend(parse_lines(data))
        return Snapshot(events, truncated, max_bytes - remaining)

    @staticmethod
    def _read_tail(directory, name, limit):
        """(the last ``limit`` bytes of a segment from a line start, whether the start of it was cut off)."""
        try:
            handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
        except OSError:
            return b"", False
        try:
            info = os.fstat(handle)
            if not stat.S_ISREG(info.st_mode):
                return b"", False
            want = min(info.st_size, limit)
            offset = info.st_size - want
            data = os.pread(handle, want, offset)
        except OSError:
            return b"", False
        finally:
            os.close(handle)
        if offset > 0:
            _, newline, data = data.partition(b"\n")
            return (data if newline else b""), True
        return data, False


def parse_lines(data):
    """The valid events of a chunk of ledger lines; everything else (torn, unknown, oversize) is skipped."""
    events = []
    for line in data.split(b"\n"):
        if not line or len(line) > MAX_LINE:
            continue
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue
        event = _event(event)
        if event is not None:
            events.append(event)
    return events


def _event(raw):
    """The event as the readers understand it, or None. Unknown fields and unknown versions are tolerated."""
    if not isinstance(raw, dict) or not is_int(raw.get("v")) or raw["v"] < 1:
        return None
    kind = raw.get("kind")
    if not isinstance(kind, str) or kind not in KINDS or epoch_of(raw.get("at")) is None:  # a list or object kind is not hashable
        return None
    try:
        repo = check_repo(raw.get("repo"))
    except ValueError:
        return None
    run = raw.get("run")
    if not isinstance(run, str) or not RUN_SLUG.fullmatch(run):
        return None
    return {**raw, "repo": repo, "run": run}


# --- who records ------------------------------------------------------------------------


def _signature(job):
    return (job.get("status"), job.get("worker_phase") or job.get("phase"), job.get("attempt"), job.get("pr"))


class Recorder:
    """The ledger entries of one managed run. Every method is best effort and never raises."""

    def __init__(self, ledger, repo, run, plan):
        self.ledger, self.repo, self.run = ledger, repo, run
        self.plan = plan  # the live plan: jobs a plan fragment adds later are found in it too

    def issues_of_job(self, job_id):
        jobs = self.plan.get("jobs") if isinstance(self.plan, dict) else None
        for job in jobs if isinstance(jobs, list) else ():
            if isinstance(job, dict) and job.get("id") == job_id:
                return issues_of(job.get("issues"))
        return []

    def emit(self, kind, **fields):
        try:
            return self.ledger.append(kind, self.repo, self.run, **fields)
        except Exception:  # noqa: BLE001  (best effort, whatever went wrong)
            return False

    def on_job(self, key, previous, current):
        """``State.ledger``: one entry for each real transition (status, phase, attempt or pull request)."""
        if not isinstance(current, dict) or _signature(previous or {}) == _signature(current):
            return
        job = job_id_of(key)
        if job is None:
            return
        seconds = current.get("phase_seconds")
        active = sum(v for v in seconds.values() if number_of(v) is not None) if isinstance(seconds, dict) else None
        stop = current.get("stop") if current.get("status") == "stopped" else None
        usage = current.get("usage")
        usage = usage if isinstance(usage, list) else []
        self.emit(
            "job",
            job=job,
            issues=self.issues_of_job(job) or None,
            status=word_of(current.get("status")),
            phase=phase_of(current.get("worker_phase") or current.get("phase")),
            attempt=count_of(current.get("attempt")),
            pr=pr_url_of(current.get("pr")),
            cost_usd=number_of(cost_usd(usage)),
            # Token counts of every session so far (cumulative, like the cost), by phase.
            tokens=phase_tokens_of(token_totals(usage)),
            active_seconds=number_of(active),
            reason_code=code_of(current.get("reason_code")),
            # Who stopped the job: the authenticated operator (or "cli"), never the reason they gave.
            stopped_by=(principal_text(stop.get("requested_by", "")) or None) if isinstance(stop, dict) else None,
        )

    def planned(self, plan, planner_usage=None):
        jobs = plan.get("jobs") if isinstance(plan, dict) else None
        listed = [
            clean({"id": job_id_of(job.get("id")), "issues": issues_of(job.get("issues"))})
            for job in (jobs if isinstance(jobs, list) else ())
            if isinstance(job, dict)
        ]
        numbers = sorted({n for job in listed for n in job.get("issues", [])})
        self.emit(
            "planned",
            issues=numbers[:MAX_ISSUES],
            jobs=[job for job in listed if "id" in job][:MAX_JOBS],
            planner_cost_usd=number_of(cost_usd([planner_usage])) if planner_usage else None,
            planner_tokens=tokens_of(usage_tokens(planner_usage.get("usage"))) if isinstance(planner_usage, dict) else None,
        )

    def run_started(self):
        return self.emit("run_started")

    def run_finished(self, result):
        return self.emit("run_finished", result=result if result in RUN_RESULTS else "interrupted")

    def purged(self, plan, states, reason="manual"):
        """The run is gone: what each job last was (from the persisted state, no GitHub call)."""
        rows = []
        jobs = plan.get("jobs") if isinstance(plan, dict) else None
        for job in jobs if isinstance(jobs, list) else ():
            if not isinstance(job, dict):
                continue
            state = states.get(job.get("id")) if isinstance(states, dict) else None
            state = state if isinstance(state, dict) else {}
            rows.append(
                {
                    "id": job.get("id"),
                    "status": state.get("status", "not_started"),
                    "pr": state.get("pr"),
                    "reason_code": state.get("reason_code"),
                }
            )
        return self.emit("purged", reason=reason if reason in PURGE_REASONS else "manual", jobs=job_views(rows))


def recorder_for(cfg, directory, plan, env=None, limits=None):
    """The ``Recorder`` of a managed run, or None: no repository (local mode), no ``FACTORY_DATA``, or a
    run directory that is not ``<FACTORY_DATA>/repos/<org>/<name>/runs/<run>`` means nothing is written."""
    env = os.environ if env is None else env
    repo = getattr(cfg, "repo", None)
    data = str(env.get("FACTORY_DATA") or "").strip()
    if not repo or not data or "\x00" in data:
        return None
    try:
        check_repo(repo)
        run = check_run_slug(Path(directory).name)
        base = Path(os.path.abspath(data))
        if os.path.realpath(directory) != os.path.realpath(data_paths(cfg, base).run_dir(run)):
            return None
    except (ValueError, TypeError, OSError):
        return None
    return Recorder(Ledger(base, limits or limits_from_env(env)), repo, run, plan)


def attach(state, cfg, directory, plan, env=None):
    """Hook the ledger into ``state`` (``State.ledger``); returns the ``Recorder``, or None when there is none."""
    recorder = recorder_for(cfg, directory, plan, env)
    if recorder is not None:
        state.ledger = recorder.on_job
    return recorder


def record_operator(ledger, repo, run, action, principal, job=None, status=None):
    """The audit of one successful operator action, with the authenticated principal."""
    return ledger.append(
        "operator",
        repo,
        run,
        action=action if isinstance(action, str) and _ACTION.fullmatch(action) else None,
        principal=principal_text(principal),
        job=job_id_of(job),
        http=count_of(status),
    )


def record_observation(ledger, repo, run, row, observed_at=None, fresh=None):
    """A pull request state a status read found (``row`` is a job of ``status --json``): no extra GitHub call.

    ``fresh`` is False when the live lookup failed and the row is only what ``state.json`` says.
    """
    return ledger.append(
        "pr_observed",
        repo,
        run,
        job=job_id_of(row.get("id")),
        pr=pr_url_of(row.get("pr")),
        handoff_status=word_of(row.get("handoff_status")),
        observed_at=stamp_of(observed_at),
        fresh=fresh if isinstance(fresh, bool) else None,
    )


def lookup_fresh(report, job_id):
    """Whether the status read ``report`` confirmed ``job_id``'s pull request word with GitHub: True or False.

    The answer is per job: ``live_refresh.failed`` names the jobs whose lookup failed, and a sibling's failure
    does not make the others' words guesses. None when the report says nothing about its lookups. A report
    that is not ``ok`` but names no failed job is read as failing every job (fail closed).
    """
    refresh = report.get("live_refresh") if isinstance(report, dict) else None
    if not isinstance(refresh, dict):
        return None
    if refresh.get("ok") is True:
        return True
    failed = refresh.get("failed")
    named = set()
    for item in failed if isinstance(failed, list) else ():
        if isinstance(item, dict) and isinstance(item.get("job"), str):
            named.add(item["job"])
    if not named or not isinstance(job_id, str):
        return False
    return job_id not in named


def record_purge(ledger, repo, run, reason, report):
    """The manager's account of a purge: why, and each job as the status read before it saw it (with GitHub's word).

    A job's ``fresh`` is False when that status read could not look its pull request up live (or said nothing
    about its lookups), so its word is only what ``state.json`` says; the event's own ``fresh`` is True only when every lookup of the read succeeded.
    """
    jobs = report.get("jobs") if isinstance(report, dict) else None
    refresh = report.get("live_refresh") if isinstance(report, dict) else None
    views = job_views(jobs)
    for view in views:
        # Fail closed: a report that says nothing about its lookups confirmed nothing with GitHub.
        view["fresh"] = lookup_fresh(report, view["id"]) is True
    return ledger.append(
        "purged",
        repo,
        run,
        reason=reason if reason in PURGE_REASONS else "manual",
        jobs=views,
        fresh=refresh.get("ok") is True if isinstance(refresh, dict) else None,
    )


# --- reading the ledger as runs and jobs ---------------------------------------------------


def outcome_of(status, handoff_status=None, purged=False):
    """The outcome of one job, or None while it is still going.

    GitHub's word wins when the pull request was merged or closed (a job whose pull request a person merged
    stays ``pr_ready`` in ``state.json``). A run that was purged before its jobs finished ended as abandoned.
    """
    for value in (handoff_status, status):
        if value in _FINAL:
            return value
    if status == "stopped":
        return "stopped"
    if purged:
        return "abandoned"
    return OUTCOME_OF_STATUS.get(status)


def run_outcome(jobs, purged):
    """The least successful outcome of the jobs; None while any is still going. A purged run with no jobs was abandoned."""
    outcomes = [job["outcome"] for job in jobs]
    if not outcomes:
        return "abandoned" if purged else None
    if any(outcome is None for outcome in outcomes):
        return None
    return next(outcome for outcome in RUN_OUTCOME_ORDER if outcome in outcomes)


def _new_run(repo, run, at):
    return {
        "repo": repo,
        "run": run,
        "issues": set(),
        "planned_at": None,
        "started_at": None,
        "finished_at": None,
        "last_result": None,
        "first_at": at,
        "last_at": at,
        "invocations": 0,
        "planner_cost_usd": 0.0,
        "planner_tokens": {},
        "purged_at": None,
        "purge_reason": None,
        "operators": [],
        "jobs": {},
    }


def _new_job(job_id, at):
    return {
        "id": job_id,
        "issues": [],
        "status": "queued",
        "phase": None,
        "attempt": 0,
        "pr": None,
        "cost_usd": 0.0,
        "tokens": {},
        "active_seconds": None,
        "reason_code": None,
        "handoff_status": None,
        "observed_at": None,
        "observed_fresh": None,
        "stopped_by": None,
        "stopped_at": None,
        "first_at": at,
        "last_at": at,
        "timeline": [],
    }


def _job(run, job_id, at):
    job = run["jobs"].get(job_id)
    if job is None:
        job = run["jobs"][job_id] = _new_job(job_id, at)
    job["last_at"] = at
    return job


def _settle_handoff(job, handoff_status, fresh, observed_at):
    """Keep GitHub's word on a job's pull request, unless it would replace a confirmed word with a guess.

    ``fresh`` is False when the live lookup failed and the word is only what ``state.json`` says (any pull
    request it knows of is ``published``). Such a word never replaces one GitHub confirmed, and the
    confirmed word keeps its own ``observed_at``, so a reader sees how old it is instead of a recent time
    on a guess. ``observed_fresh`` says which kind of word the job holds now: True (GitHub's), False
    (``state.json``'s) or None (an entry that did not say).
    """
    if handoff_status is None:
        return
    if fresh is False and job["observed_fresh"] is True:
        return
    job["handoff_status"] = handoff_status
    job["observed_at"] = observed_at or job["observed_at"]
    job["observed_fresh"] = fresh


def _fresh_of(value):
    return value if isinstance(value, bool) else None


def _apply(run, event):
    at, kind = event["at"], event["kind"]
    run["last_at"] = at
    if kind == "planned":
        run["planned_at"] = run["planned_at"] or at
        run["issues"].update(issues_of(event.get("issues")))
        run["planner_cost_usd"] = number_of(event.get("planner_cost_usd")) or run["planner_cost_usd"]
        run["planner_tokens"] = tokens_of(event.get("planner_tokens")) or run["planner_tokens"]
        for entry in event.get("jobs") if isinstance(event.get("jobs"), list) else ():
            if isinstance(entry, dict) and job_id_of(entry.get("id")):
                job = _job(run, entry["id"], at)
                job["issues"] = issues_of(entry.get("issues")) or job["issues"]
    elif kind == "run_started":
        run["started_at"] = run["started_at"] or at
        run["invocations"] += 1
    elif kind == "run_finished":
        run["finished_at"] = at
        run["last_result"] = event.get("result") if event.get("result") in RUN_RESULTS else None
    elif kind == "job":
        job_id = job_id_of(event.get("job"))
        if job_id is None:
            return
        job = _job(run, job_id, at)
        _apply_job(run, job, event, at)
    elif kind == "pr_observed":
        job_id = job_id_of(event.get("job"))
        if job_id is None:
            return
        job = _job(run, job_id, at)
        job["pr"] = pr_url_of(event.get("pr")) or job["pr"]
        _settle_handoff(
            job, word_of(event.get("handoff_status")), _fresh_of(event.get("fresh")), stamp_of(event.get("observed_at")) or at
        )
    elif kind == "purged":
        run["purged_at"] = run["purged_at"] or at
        reason = event.get("reason") if event.get("reason") in PURGE_REASONS else "manual"
        # Two writers may record one purge (the CLI and the manager's janitor): the one that knows why wins.
        if run["purge_reason"] in (None, "manual"):
            run["purge_reason"] = reason
        for entry in event.get("jobs") if isinstance(event.get("jobs"), list) else ():
            if isinstance(entry, dict) and job_id_of(entry.get("id")):
                job = _job(run, entry["id"], at)
                job["status"] = word_of(entry.get("status")) or job["status"]
                job["pr"] = pr_url_of(entry.get("pr")) or job["pr"]
                job["reason_code"] = code_of(entry.get("code")) or job["reason_code"]
                fresh = _fresh_of(entry.get("fresh"))  # per job; an entry that did not say takes the event's
                _settle_handoff(job, word_of(entry.get("handoff")), _fresh_of(event.get("fresh")) if fresh is None else fresh, at)
    elif kind == "operator":
        action = event.get("action")
        run["operators"].append(
            clean(
                {
                    "at": at,
                    "action": action if isinstance(action, str) and _ACTION.fullmatch(action) else None,
                    "principal": principal_text(event.get("principal", "")),
                    "job": job_id_of(event.get("job")),
                    "http": count_of(event.get("http")),
                }
            )
        )
        del run["operators"][:-MAX_OPERATORS]


def _apply_job(run, job, event, at):
    job["issues"] = issues_of(event.get("issues")) or job["issues"]
    job["status"] = word_of(event.get("status")) or job["status"]
    job["phase"] = phase_of(event.get("phase"))
    attempt, cost = count_of(event.get("attempt")), number_of(event.get("cost_usd"))
    job["attempt"] = job["attempt"] if attempt is None else attempt
    job["pr"] = pr_url_of(event.get("pr")) or job["pr"]
    job["cost_usd"] = job["cost_usd"] if cost is None else cost
    job["tokens"] = phase_tokens_of(event.get("tokens")) or job["tokens"]
    job["active_seconds"] = number_of(event.get("active_seconds"))
    job["reason_code"] = code_of(event.get("reason_code"))
    if job["status"] == "stopped":
        job["stopped_by"] = principal_text(event.get("stopped_by", "")) or None
        job["stopped_at"] = at
    run["issues"].update(job["issues"])
    job["timeline"].append(
        clean(
            {
                "at": at,
                "status": job["status"],
                "phase": job["phase"],
                "attempt": job["attempt"],
                "pr": job["pr"],
                "cost_usd": job["cost_usd"],
                "reason_code": job["reason_code"],
            }
        )
    )
    del job["timeline"][:-MAX_TIMELINE]


def fold(events):
    """``{(repo, run): summary}`` from events oldest first: jobs with their latest state, the outcomes, cost and duration."""
    runs = {}
    for event in events:
        key = (event["repo"], event["run"])
        run = runs.get(key)
        # A run name that is planned again after its purge is a new run, not the old one coming back.
        if run is None or (event["kind"] == "planned" and run["purged_at"] is not None):
            run = runs[key] = _new_run(event["repo"], event["run"], event["at"])
        _apply(run, event)
    for run in runs.values():
        _finish(run)
    return runs


def _seconds(start, end):
    value = elapsed(start, end)
    return None if value is None else round(value, 3)


def _finish(run):
    purged = run["purged_at"] is not None
    for job in run["jobs"].values():
        job["outcome"] = outcome_of(job["status"], job["handoff_status"], purged)
        job["duration_seconds"] = _seconds(job["first_at"], job["last_at"])
        job["last_epoch"] = round(epoch_of(job["last_at"]) or 0.0, 6)
    jobs = list(run["jobs"].values())
    run["issues"] = sorted(run["issues"])
    run["outcome"] = run_outcome(jobs, purged)
    run["outcomes"] = {name: sum(1 for job in jobs if job["outcome"] == name) for name in OUTCOMES}
    run["cost_usd"] = round(run["planner_cost_usd"] + sum(job["cost_usd"] for job in jobs), 6)
    run["duration_seconds"] = _seconds(run["first_at"], run["last_at"])
    run["last_epoch"] = round(epoch_of(run["last_at"]) or 0.0, 6)
    run["jobs_total"] = len(jobs)


def public_job(run, job):
    """A job as the history endpoints return it: no timeline, with the run it belongs to."""
    return {
        "repo": run["repo"],
        "run": run["run"],
        "job": job["id"],
        "issues": job["issues"],
        "status": job["status"],
        "outcome": job["outcome"],
        "phase": job["phase"],
        "attempt": job["attempt"],
        "pr": job["pr"],
        "handoff_status": job["handoff_status"],
        "observed_at": job["observed_at"],
        "observed_fresh": job["observed_fresh"],
        "reason_code": job["reason_code"],
        # Who stopped the job and when, while it is stopped (a retried job is no longer a stopped one).
        "stopped_by": job["stopped_by"] if job["status"] == "stopped" else None,
        "stopped_at": job["stopped_at"] if job["status"] == "stopped" else None,
        "cost_usd": job["cost_usd"],
        "active_seconds": job["active_seconds"],
        "duration_seconds": job["duration_seconds"],
        "first_at": job["first_at"],
        "last_at": job["last_at"],
        "purged": run["purged_at"] is not None,
        "purge_reason": run["purge_reason"],
    }


def public_run(run, max_jobs):
    """A run as the history endpoints return it, with at most ``max_jobs`` of its jobs."""
    jobs = sorted(run["jobs"].values(), key=lambda job: (job["first_at"], job["id"]))
    listed = [
        {key: value for key, value in public_job(run, job).items() if key not in ("repo", "run")}
        for job in jobs[:max_jobs]
    ]
    return {
        "repo": run["repo"],
        "run": run["run"],
        "issues": run["issues"],
        "outcome": run["outcome"],
        "outcomes": run["outcomes"],
        "planned_at": run["planned_at"],
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
        "last_result": run["last_result"],
        "first_at": run["first_at"],
        "last_at": run["last_at"],
        "duration_seconds": run["duration_seconds"],
        "invocations": run["invocations"],
        "planner_cost_usd": run["planner_cost_usd"],
        "cost_usd": run["cost_usd"],
        "purged_at": run["purged_at"],
        "purge_reason": run["purge_reason"],
        "operators": run["operators"],
        "jobs_total": run["jobs_total"],
        "jobs_truncated": run["jobs_total"] > max_jobs,
        "jobs": listed,
    }

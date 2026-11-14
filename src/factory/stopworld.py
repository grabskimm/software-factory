"""The stop-the-world breaker (issue #146): pause the trigger and drain when sessions cannot succeed.

A spend limit (or a revoked key, or an unclassifiable storm of 429s) fails EVERY session. Retrying
cannot help and only multiplies the failures, so the manager stops starting work, asks the runs in
progress to drain, and waits for a person. This module is the mechanism; ``operations.py`` feeds it
and ``manager.py`` owns the instance and the marker.

Trip rules (the table ``RULES``; ``apierror.py`` makes the codes):

* ``spend_limit``: trips at once. Retrying a spend limit fails until a person raises the limit.
* ``auth``: trips at 2 within 5 minutes of each other, across the manager. A bad key fails every session, but one
  event can be a misread, two in five minutes cannot.
* ``rate_limit_unknown``: trips at 3 within 5 minutes of each other, across the manager.

The 5 minutes are measured between the events' own times, not from the moment they are read: a poll that is
slower than the window (``FACTORY_MANAGER_POLL_SECONDS`` may be an hour) or a restart sees the failures late, and
two that happened 10 seconds apart still count as two. Only ``FRESH_SECONDS`` looks back from the reading.
* ``rate_limit`` (its ``retry-after`` is known), ``overloaded``, ``other`` and ``none`` never trip.
* ``trip(kind)`` also takes ``isolation_failed``, for a later pod backend: one mechanism, no second.

The state is one file, ``spend-breaker``, at the top of the data directory. It is NOT the manual
trigger-pause marker: the deploy gate clears that one after every automatic image promotion, which
would clear a breaker while the spend limit is still hit. A trip never retries and never resumes by
itself; a person clears it (``POST /spend-breaker``) after raising the limit, and the clear is stamped
(``spend-breaker-cleared``) so that the failed jobs still in the run's state cannot trip it again.

Nothing here reads a log, a request or agent text: the inputs are closed codes, ids the supervisor
made and timestamps.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from collections import OrderedDict, namedtuple
from datetime import datetime, timezone

from . import apierror

MARKER = "spend-breaker"
CLEARED = "spend-breaker-cleared"
ISOLATION_FAILED = "isolation_failed"
# The values of the metric label ``code``: the classifier's codes and what only a trip can say.
KINDS = (*apierror.CODES, ISOLATION_FAILED)
# What can trip the breaker; every other code only counts in a metric.
TRIPPING = (apierror.SPEND_LIMIT, apierror.AUTH, apierror.RATE_LIMIT_UNKNOWN, ISOLATION_FAILED)

Rule = namedtuple("Rule", ("count", "window"))
# code -> (events needed, within how many seconds). One event is enough where the count is 1.
RULES = {
    apierror.SPEND_LIMIT: Rule(1, 0),
    apierror.AUTH: Rule(2, 300),
    apierror.RATE_LIMIT_UNKNOWN: Rule(3, 300),
}
# An event older than this is history, not news: a manager that restarts must not trip on a failure from yesterday.
FRESH_SECONDS = 900
MAX_SEEN = 1024
MARKER_READ_BYTES = 256


def parse_epoch(text):
    """Seconds since the epoch of a ``state.timestamp()`` string, else None."""
    if not isinstance(text, str):
        return None
    try:
        moment = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.timestamp() if moment.tzinfo is not None else None


def read_state(directory):
    """``{"code", "at"}`` when the marker exists (both None for a marker that cannot be read), else None.

    The existence of the marker is the breaker: an unreadable one still means tripped (fail closed).
    """
    path = os.path.join(os.fspath(directory), MARKER)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError:
        return {"code": None, "at": None}
    with os.fdopen(descriptor, "rb") as handle:
        raw = handle.read(MARKER_READ_BYTES)
    try:
        body = json.loads(raw.decode("utf-8", errors="replace"))
    except (ValueError, RecursionError):
        body = None
    if not isinstance(body, dict):
        return {"code": None, "at": None}
    code, at = body.get("code"), body.get("at")
    return {
        "code": code if isinstance(code, str) and code in KINDS else None,
        "at": at if isinstance(at, str) and parse_epoch(at) is not None else None,
    }


def read_from_env(env=os.environ):
    """``read_state`` for ``$FACTORY_DATA`` (what ``status`` shows); None when unset or not tripped."""
    directory = env.get("FACTORY_DATA")
    return read_state(directory) if directory else None


def _write_new(path, text):
    """Create ``path`` holding ``text`` exclusively and durably; False when it already exists."""
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    return True


class Breaker:
    """The marker, the trip rules and their in-memory windows for one manager process.

    The marker (and the clear stamp) are files, so a trip survives a restart. The windows of the counting
    rules are memory only: after a restart the operations loop reads the recent failures again and they
    count again, because each event carries its own time and the rules count those times against each other,
    not against the time of the reading.
    """

    def __init__(self, directory, clock=time.time):
        self._directory = os.fspath(directory)
        self._clock = clock
        self._mutex = threading.Lock()
        self._events = {code: [] for code in RULES}
        self._seen = OrderedDict()

    @property
    def path(self):
        return os.path.join(self._directory, MARKER)

    def _stamp(self):
        """The time of a trip or a clear, from the breaker's clock (the wall clock unless a test injects one)."""
        return datetime.fromtimestamp(self._clock(), timezone.utc).isoformat().replace("+00:00", "Z")

    def tripped(self):
        return os.path.lexists(self.path)

    def state(self):
        """``{"code", "at"}`` while tripped, else None."""
        return read_state(self._directory)

    def cleared_at(self):
        """Seconds since the epoch of the last clear, else None (an unreadable stamp counts as no clear)."""
        try:
            with open(os.path.join(self._directory, CLEARED), "rb") as handle:
                return parse_epoch(handle.read(MARKER_READ_BYTES).decode("utf-8", errors="replace"))
        except OSError:
            return None

    def observe(self, code, ident, at=None, now=None):
        """Count one API-error event; True when this event meets the trip rule of its code.

        ``ident`` names the event (a job and its attempt), so one failure seen again counts once. An event
        at or before the last clear, or older than ``FRESH_SECONDS``, is ignored. The counting rules hold when
        ``rule.count`` events (this one and the ones kept before) fall within ``rule.window`` of each other,
        whenever they were read. It does not trip: the caller does, with ``trip``.
        """
        rule = RULES.get(code)
        if rule is None:
            return False
        current = self._clock() if now is None else now
        happened = current if at is None else at
        cleared = self.cleared_at()
        if (cleared is not None and happened <= cleared) or current - happened > FRESH_SECONDS:
            return False
        with self._mutex:
            if ident in self._seen:
                return False
            self._seen[ident] = None
            while len(self._seen) > MAX_SEEN:
                self._seen.popitem(last=False)
            if rule.count <= 1:
                return True
            # The events are counted against each other, never against the time of this call: a poll longer than
            # the window, or a restart, sees them late, but their own times say how far apart they happened.
            # A moment that no event of ``FRESH_SECONDS`` can still reach is dropped; the order they arrive in is
            # free (a run lists its jobs in plan order), so the moments are sorted and every run of ``rule.count``
            # neighbours is tried.
            reach = current - FRESH_SECONDS - rule.window
            moments = sorted(moment for moment in (*self._events[code], happened) if moment >= reach)
            self._events[code] = moments[-MAX_SEEN:]
            return any(last - first <= rule.window for first, last in zip(moments, moments[rule.count - 1 :]))

    def trip(self, kind):
        """Set the marker; True only for the call that set it (the first code wins, a later one changes nothing)."""
        if kind not in TRIPPING:
            raise ValueError("not a code that can trip the breaker")
        return _write_new(self.path, json.dumps({"code": kind, "at": self._stamp()}))

    def clear(self):
        """Remove the marker after stamping the clear; True when it was set. Idempotent."""
        was = self.tripped()
        if was:
            stamp = os.path.join(self._directory, CLEARED)
            temp = f"{stamp}.{os.getpid()}.tmp"
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp)
            _write_new(temp, self._stamp())
            os.replace(temp, stamp)
        with self._mutex:
            self._events = {code: [] for code in RULES}
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.path)
        return was

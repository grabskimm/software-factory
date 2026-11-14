"""The pod pool: a cluster-wide ceiling, pacing and reserve-on-admit for per-session pods.

Slice 18 of the pod backend (``docs/pod-backend.md``, issue #146). The pod backend of slice 12b asks this
module, once per session and BEFORE it asks the launcher for a pod, for a **reservation**: a slot under the
ceiling, a pacing token and the session's spend estimate, granted together or not at all. It is a guard, not a
backend: it launches nothing, imports no launcher or Kubernetes code and is not imported by anything that runs
today. With ``execution = "inprocess"`` (the default) nothing here is reached.

What one admission checks, in this order (the closed refusal codes are ``REFUSALS``):

1. the stop-the-world breaker (``stopworld``): tripped means ``breaker``, final;
2. the request itself is the closed ``Request``, and no live reservation of the same session exists (``duplicate``);
3. the run's budget: ``spent + reserved by the run's other live sessions + this estimate <= run_budget_usd``.
   When even an empty reservation list would not fit it is ``budget`` (final); when it fits only once another
   reservation of the run is released it is ``budget_held`` (wait). Amounts are exact: whole micro-dollars, spent and
   estimates rounded UP, the budget DOWN;
4. the per-repository cap (``repo_cap``, the contract's 4, the launcher enforces its own);
5. the ceiling: live reservations, or the launcher's own count of live and in-flight pods when that is larger (a pod
   whose supervisor crashed holds no reservation but still occupies a node), must be below ``ceiling`` (``ceiling``);
6. pacing: a token bucket of ``PACING_BURST`` tokens refilled one per ``PACING_INTERVAL_SECONDS`` (``pacing``, with
   the seconds until the next token). A refused request consumes no token, estimate or slot (a waiting one refreshes its ticket);
7. fairness: when several requests are admissible the fewest-slots repository goes first, the oldest request breaking
   ties (``capacity.admission_order``, the rule of the in-process pod budget; ``queued``).

**Reservations never leak.** A reservation is a file held under ``flock`` by the process that was granted it; the
kernel drops the lock when the process dies, and the next scan of the ledger (any supervisor's, or ``sweep``)
removes a file nobody holds and reports it. A holder that is alive but wedged is bounded as well: every reservation
expires at the pod's deadline (``activeDeadlineSeconds``) plus the termination grace and the launch grace, a moment
after which the kubelet and the launcher's own sweep have ended the pod, and an expired file is removed even though
its holder is still alive (that holder's next ``requested()`` then fails with ``Lost``, so it cannot start a pod on a
reservation the ledger no longer counts). ``release`` is idempotent and the object is a context manager.

**The ledger is one directory** (``$FACTORY_DATA/.capacity/pods``, the pod-wide state directory of ``capacity``):
``admit.lock`` (one ``flock`` serialising every change, so two supervisors cannot both take the last slot),
``r-<id>`` (one reservation each), ``pacing`` (the bucket) and ``queue/`` (tickets of waiting requests, the
machinery of ``capacity.SlotPool``). The pod-wide budget of ``capacity`` (``FACTORY_MAX_TOTAL_WORKERS``) is NOT
involved: a session in a pod uses none of the manager pod's cpu or memory.

**It fails closed** (``capacity`` fails open, because a local guess must not stop work; spending a cluster node on a
guess is another matter): a state directory that cannot be used, a lock that cannot be taken within
``LOCK_WAIT_SECONDS``, a launcher count that cannot be read, a ceiling that is missing or malformed and a request
that is not valid all refuse, and a refusal never starts a session. Nothing here reads a log, an issue or the
environment outside `from_env`, `parse_ceiling` and `pool_directory`; every message is a fixed word and a number, never a value that was passed in.

The call contract (``docs/pod-backend.md``, "The pod pool"):

* ``pool = podpool.from_env(env, live_count=podpool.launcher_live_count(client))`` once per process, or None while the
  switch is off;
* ``with podpool.hold(pool, request, seconds) as reservation:`` waits for an admission (it raises ``Refused`` for a final
  refusal and ``Timeout`` after ``seconds``) and releases on EVERY way out of the block;
* ``reservation.requested()`` immediately BEFORE the launch request is written to the launcher (raises ``Lost``: do
  not launch);
* ``reservation.cancel()`` when the launch certainly did not happen (the pacing token is given back), the end of the
  block when the session is over;
* ``pool.adopt(request)`` for a pod a restarted manager found alive: counted, never refused.

Standard library only (``capacity`` and ``stopworld`` are this package's own).
"""

import collections
import contextlib
import decimal
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import capacity, stopworld
from . import podcontract as pc

ENV_CEILING = pc.ENV_MANAGER["max_pod_sessions"]
ENV_DATA = "FACTORY_DATA"
SUBDIRECTORY = "pods"
LOCK_NAME = "admit.lock"
PACING_NAME = "pacing"
QUEUE_NAME = "queue"
PREFIX = "r-"

# Largest ceiling accepted; the NodePool limit is a few dozen sessions, a typo of a thousand times that is refused.
MAX_CEILING = 1024
# How long one admission waits for the ledger lock (the critical sections are a few file operations).
LOCK_WAIT_SECONDS = 5.0
# A waiting request keeps its place while it asks again within this time (three pacing intervals).
TICKET_TTL_SECONDS = float(3 * pc.PACING_INTERVAL_SECONDS)
# The longest a reservation can be held: the contract's deadline clamp and margin, the grace and the launch grace.
MAX_HOLD_SECONDS = pc.DEADLINE_CLAMP_SECONDS + pc.DEADLINE_MARGIN_SECONDS + pc.TERMINATION_GRACE_SECONDS + pc.LAUNCH_GRACE_SECONDS
# ``hold`` never sleeps longer than this between two attempts (a waiting request must outlive no ticket).
MAX_POLL_SECONDS = 5.0
POLL_SECONDS = capacity.POLL_SECONDS
MAX_ATTEMPT = 9999
MICRO = 1_000_000
# A request asks for at most this much (dollars); phases cap a session at single digits.
MAX_USD = 100_000
_FILE_BYTES = 4096

# The closed refusal codes. ``WAITING`` ones can clear by themselves; the others will not, and a caller must not poll them.
BREAKER, DUPLICATE, INVALID, UNAVAILABLE, BUDGET = "breaker", "duplicate", "invalid", "unavailable", "budget"
BUDGET_HELD, REPO_CAP, CEILING, PACING, QUEUED = "budget_held", "repo_cap", "ceiling", "pacing", "queued"
WAITING = (BUDGET_HELD, REPO_CAP, CEILING, PACING, QUEUED)
FINAL = (BREAKER, DUPLICATE, INVALID, UNAVAILABLE, BUDGET)
REFUSALS = (*WAITING, *FINAL)

_TEXT = {
    BREAKER: "pod pool: the stop-the-world breaker is tripped",
    DUPLICATE: "pod pool: this session already holds a reservation",
    INVALID: "pod pool: the request is not valid",
    UNAVAILABLE: "pod pool: its state or the launcher's count cannot be read; refusing",
    BUDGET: "pod pool: the run budget cannot cover another session",
    BUDGET_HELD: "pod pool: the run budget is held by the run's running sessions",
    REPO_CAP: "pod pool: the repository is at its per-repository cap",
    CEILING: "pod pool: the pod ceiling is reached",
    PACING: "pod pool: pacing, the next session may start shortly",
    QUEUED: "pod pool: other repositories are ahead",
}

_ATTEMPT_TEXT = "invalid attempt"
_DECIMAL = decimal.Context(prec=60)


class PoolError(Exception):
    """Base of every error here. The message is a fixed phrase: it never carries a value that was passed in."""


class ConfigRefused(PoolError):
    """The pool cannot be configured from the environment. ``code`` is a fixed word."""

    def __init__(self, code):
        super().__init__(f"pod pool configuration refused: {code}")
        self.code = code


class Lost(PoolError):
    """The reservation is no longer in the ledger (released, expired and swept): do not start a pod on it."""

    def __init__(self):
        super().__init__("pod pool: the reservation is gone")


class Unavailable(PoolError):
    """The ledger could not be locked or written."""

    def __init__(self):
        super().__init__("pod pool: its state cannot be used")


class Refused(PoolError):
    """No admission. ``code`` is one of ``REFUSALS``; ``retry_after`` seconds when it is worth asking again."""

    def __init__(self, code, retry_after=None):
        super().__init__(_TEXT[code])
        self.code = code
        self.retry_after = retry_after


class Timeout(Refused):
    """A refusal that could have cleared did not within the time ``hold`` was given."""


def _log(message):
    """One fixed line on stderr: literal words and integers only."""
    print(f"factory: {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# The request and the answer
# ---------------------------------------------------------------------------


def _micros(value, rounding):
    """``value`` dollars as whole micro-dollars, exactly (the shortest decimal form of the float), rounded as asked."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid amount")
    try:
        finite = math.isfinite(value)
    except OverflowError:  # an int beyond the float range is no amount either
        finite = False
    if not finite:
        raise ValueError("invalid amount")
    exact = _DECIMAL.create_decimal(repr(value)).scaleb(6, context=_DECIMAL)
    return int(exact.to_integral_value(rounding=rounding, context=_DECIMAL))


@dataclass(frozen=True)
class Request:
    """What one admission is asked for. Every field is validated when it is built; a bad one is a ValueError.

    ``estimate_usd`` is what this session may spend (the phase's ``--max-budget-usd``), ``spent_usd`` what the run has
    spent so far (the supervisor's own cost record) and ``budget_usd`` the repository's ``run_budget_usd`` or None
    when it has none. ``timeout_seconds`` is the session's, as the launch request carries it.
    """

    repo: str
    run: str
    job: str
    phase: str
    attempt: int
    estimate_usd: float
    timeout_seconds: int
    spent_usd: float = 0
    budget_usd: float | None = None

    def __post_init__(self):
        # The repository, run and job id are validated by the contract's own helper (the one the pod's annotations use).
        pc.worker_annotations(self.repo, self.run, self.job, 0)
        if self.phase not in pc.PHASES:
            raise ValueError("invalid phase")
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int) or not 0 <= self.attempt <= MAX_ATTEMPT:
            raise ValueError(_ATTEMPT_TEXT)
        pc.active_deadline_seconds(self.timeout_seconds)
        estimate = _micros(self.estimate_usd, decimal.ROUND_CEILING)
        if not 0 < estimate <= MAX_USD * MICRO:
            raise ValueError("invalid estimate")
        spent = _micros(self.spent_usd, decimal.ROUND_CEILING)
        if not 0 <= spent <= MAX_USD * MICRO:
            raise ValueError("invalid spent")
        if self.budget_usd is not None and not 0 < _micros(self.budget_usd, decimal.ROUND_FLOOR) <= MAX_USD * MICRO:
            raise ValueError("invalid budget")

    @classmethod
    def for_launch(cls, launch, estimate_usd, spent_usd=0, budget_usd=None):
        """The request of the launcher's closed launch request (``podspec.REQUEST_FIELDS``) plus the three amounts."""
        return cls(
            repo=launch["repository"],
            run=launch["run_id"],
            job=launch["job_id"],
            phase=launch["phase"],
            attempt=launch["attempt"],
            estimate_usd=estimate_usd,
            timeout_seconds=launch["timeout_seconds"],
            spent_usd=spent_usd,
            budget_usd=budget_usd,
        )

    @property
    def estimate_micro(self):
        return _micros(self.estimate_usd, decimal.ROUND_CEILING)

    @property
    def spent_micro(self):
        return _micros(self.spent_usd, decimal.ROUND_CEILING)

    @property
    def budget_micro(self):
        return None if self.budget_usd is None else _micros(self.budget_usd, decimal.ROUND_FLOOR)

    @property
    def identity(self):
        """The session this reservation is for: repository, run, job, phase and attempt."""
        return f"{self.repo}|{self.run}|{self.job}|{self.phase}|{self.attempt}"

    @property
    def hold_seconds(self):
        """How long the reservation can last: the pod's deadline, its termination grace and the launch grace."""
        return pc.active_deadline_seconds(self.timeout_seconds) + pc.TERMINATION_GRACE_SECONDS + pc.LAUNCH_GRACE_SECONDS


@dataclass(frozen=True)
class Refusal:
    """Why there is no reservation: a code of ``REFUSALS``, and the numbers that explain it."""

    code: str
    retry_after: int | None = None
    in_use: int | None = None
    ceiling: int | None = None

    @property
    def waiting(self):
        """Whether asking again can succeed (every other code needs a person or a different request)."""
        return self.code in WAITING

    @property
    def text(self):
        """A fixed sentence for ``status``: counts only, no names."""
        text = _TEXT[self.code]
        if self.in_use is not None and self.ceiling is not None:
            text = f"{text} ({self.in_use}/{self.ceiling} sessions)"
        if self.retry_after is not None:
            text = f"{text}; retry in {self.retry_after} s"
        return text


@dataclass(frozen=True)
class Admission:
    """Exactly one of ``reservation`` and ``refusal``."""

    reservation: "Reservation | None" = None
    refusal: Refusal | None = None


# ---------------------------------------------------------------------------
# The reservation
# ---------------------------------------------------------------------------


class Reservation:
    """One granted slot, pacing token and spend estimate. Hold it for as long as the session may be running."""

    def __init__(self, pool, descriptor, path, inode, data, refundable):
        self._pool = pool
        self._descriptor = descriptor
        self._path = path
        self._inode = inode
        self._data = data
        self._refundable = refundable
        self._lock = threading.Lock()

    @property
    def launch_requested(self):
        """Whether ``requested()`` was called: the launcher may have, or may be about to, create a pod."""
        return bool(self._data["requested"])

    def requested(self):
        """Record, durably in the ledger, that the launch request is about to be written. Raises ``Lost``/``Unavailable``.

        Call it immediately before the request, and do NOT send the request when it raises. A reservation that was
        removed from the ledger (expired while its holder was wedged, or released) is ``Lost``, because the ledger no
        longer counts the pod this would create.
        """
        with self._lock:
            descriptor = self._descriptor
            if descriptor is None:
                raise Lost()
            if self._data["requested"]:
                return
            with self._pool._exclusive():
                if not self._still_listed() or self._pool.clock() > self._data["expires"]:
                    raise Lost()
                data = {**self._data, "requested": True}
                try:
                    _write(descriptor, data)
                except OSError:
                    raise Unavailable() from None
            self._data = data
            self._refundable = False

    def _still_listed(self):
        try:
            return os.stat(self._path).st_ino == self._inode
        except OSError:
            return False

    def cancel(self):
        """Release a reservation whose launch certainly did not happen, and give the pacing token back.

        Only a reservation on which ``requested()`` was never called is refunded: once the request may have reached
        the launcher the token stays spent. Idempotent.
        """
        # The descriptor and the right to a refund are taken out together, once, under the reservation's own lock (the one
        # ``requested()`` holds while it writes): two cancels refund one token, and a cancel that overtakes ``requested()``
        # makes it ``Lost``, one that follows it finds nothing refundable.
        with self._lock:
            descriptor, self._descriptor = self._descriptor, None
            refund, self._refundable = self._refundable and descriptor is not None, False
        if descriptor is None:
            return
        try:
            if refund:
                with contextlib.suppress(PoolError):
                    self._pool._refund()
        finally:
            self._give_back(descriptor)

    def release(self):
        """Give the slot, the token's place and the estimate back. Idempotent, thread-safe, never raises."""
        with self._lock:
            descriptor, self._descriptor = self._descriptor, None
        if descriptor is not None:
            self._give_back(descriptor)

    def _give_back(self, descriptor):
        # Unlink first, close second: a scan that finds the file still locked counts it, one that finds it unlocked
        # removes what is being removed anyway; nobody can see a free slot that is not free. The check of the inode and
        # the unlink are made under the ledger lock, so a sweep and a new admission of the same session cannot fall between
        # them and have their newer file deleted. When the lock cannot be had the file is left: closing the descriptor makes
        # it an unheld file, which the next scan removes.
        try:
            with contextlib.suppress(OSError), self._pool._exclusive():
                if self._still_listed():
                    os.unlink(self._path)
        except PoolError:
            pass
        finally:
            os.close(descriptor)  # closing the descriptor drops the flock

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()

    def __del__(self):
        with contextlib.suppress(Exception):  # at interpreter shutdown the modules this uses may be gone
            self.release()


def _write(descriptor, data):
    payload = json.dumps(data, sort_keys=True).encode()
    os.ftruncate(descriptor, 0)
    os.pwrite(descriptor, payload, 0)


# ---------------------------------------------------------------------------
# The pool
# ---------------------------------------------------------------------------


def _valid_entry(data):
    """The ledger entry as a dict of validated values, or None when the file is not one of ours."""
    if not isinstance(data, dict):
        return None
    try:
        pc.worker_annotations(data["repo"], data["run"], data["job"], 0)
        phase, attempt, estimate = data["phase"], data["attempt"], data["estimate"]
        granted, expires, requested = data["granted"], data["expires"], data["requested"]
    except (KeyError, ValueError, TypeError):
        return None
    if phase not in pc.PHASES or not isinstance(requested, bool):
        return None
    for number in (attempt, estimate):
        if isinstance(number, bool) or not isinstance(number, int) or number < 0:
            return None
    for moment in (granted, expires):
        if isinstance(moment, bool) or not isinstance(moment, (int, float)) or not math.isfinite(moment):
            return None
    return {
        "repo": data["repo"],
        "run": data["run"],
        "job": data["job"],
        "phase": phase,
        "attempt": attempt,
        "estimate": estimate,
        "granted": float(granted),
        "expires": float(expires),
        "requested": requested,
    }


class PodPool:
    """The guard. ``directory`` is the ledger, ``ceiling`` the most sessions at once.

    ``live_count`` is an optional callable returning the launcher's live plus in-flight pod count (see
    ``launcher_live_count``); when it raises or returns anything but a non-negative integer, admission refuses.
    ``breaker_dir`` is the data directory the stop-the-world marker lives in (None: not consulted). ``clock`` is the wall
    clock (seconds since the epoch); a test passes a fake one, and the ledger's times are its readings.
    """

    def __init__(
        self,
        directory,
        ceiling,
        per_repo_cap=pc.PER_REPO_CAP,
        burst=pc.PACING_BURST,
        interval=pc.PACING_INTERVAL_SECONDS,
        clock=time.time,
        live_count=None,
        breaker_dir=None,
        sleep=time.sleep,
    ):
        for name, number, low, high in (
            ("ceiling", ceiling, 0, MAX_CEILING),
            ("per-repository cap", per_repo_cap, 1, MAX_CEILING),
            ("pacing burst", burst, 1, MAX_CEILING),
            ("pacing interval", interval, 1, 86400),
        ):
            if isinstance(number, bool) or not isinstance(number, int) or not low <= number <= high:
                raise ValueError(f"invalid {name}")
        self.directory = Path(directory)
        self.ceiling = ceiling
        self.per_repo_cap = per_repo_cap
        self.burst = burst
        self.interval = interval
        self.clock = clock
        self.live_count = live_count
        self.breaker_dir = breaker_dir
        self._sleep = sleep
        self._queue = capacity.SlotPool(self.directory / QUEUE_NAME, ceiling, clock=clock, ticket_ttl=TICKET_TTL_SECONDS)

    # -- the ledger lock --------------------------------------------------

    def prepare(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    @contextlib.contextmanager
    def _exclusive(self):
        """The one lock every change of the ledger is made under. Raises ``Unavailable`` when it cannot be had."""
        try:
            self.prepare()
            descriptor = os.open(self.directory / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        except OSError:
            raise Unavailable() from None
        try:
            deadline = time.monotonic() + LOCK_WAIT_SECONDS
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise Unavailable() from None
                    self._sleep(0.005)
                except OSError:
                    raise Unavailable() from None
            yield
        finally:
            os.close(descriptor)

    # -- the ledger -------------------------------------------------------

    def _paths(self):
        try:
            return sorted(path for path in self.directory.glob(f"{PREFIX}*") if not path.name.endswith(".tmp"))
        except OSError:
            raise Unavailable() from None

    def _scan(self, now, prune):
        """``(live, reclaimed)``: the live reservations as dicts, and what was found dead or expired.

        Called under the ledger lock. A file nobody holds is a dead supervisor's; a held file past its expiry belongs to a
        wedged one; both are removed when ``prune`` and reported. A held file that is not a valid entry still counts as
        one live slot (``repo`` None, no estimate): an unreadable ledger entry must never free a slot. So does a file that exists
        but cannot be opened or is not a regular file; one that cannot be opened for want of descriptors (EMFILE, ENFILE) makes
        the whole scan ``Unavailable``, because no honest count can be made.
        """
        live, reclaimed = [], []
        for path in self._paths():
            try:
                descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
            except FileNotFoundError:
                continue  # gone since the listing: nothing to count
            except OSError as problem:
                if problem.errno in (errno.EMFILE, errno.ENFILE):
                    raise Unavailable() from None  # out of descriptors: no count can be made, so nobody is admitted on one
                # A file that exists and cannot be opened (a mode change, a symlink, ...) is not known to be free: it counts.
                live.append({"repo": None, "estimate": 0, "requested": True, "path": path})
                continue
            try:
                try:
                    if not stat.S_ISREG(os.fstat(descriptor).st_mode):  # a directory or a pipe is no reservation, nor free
                        live.append({"repo": None, "estimate": 0, "requested": True, "path": path})
                        continue
                except OSError:
                    live.append({"repo": None, "estimate": 0, "requested": True, "path": path})
                    continue
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    held = False
                except BlockingIOError:
                    held = True
                try:
                    raw = os.pread(descriptor, _FILE_BYTES, 0)
                    modified = os.fstat(descriptor).st_mtime
                except OSError:
                    raw, modified = b"", now
                try:
                    entry = _valid_entry(json.loads(raw.decode("utf-8", "replace") or "null"))
                except (ValueError, RecursionError):
                    entry = None
                expires = entry["expires"] if entry else modified + MAX_HOLD_SECONDS
                if held and now <= expires:
                    live.append({**(entry or {"repo": None, "estimate": 0, "requested": True}), "path": path})
                    continue
                reclaimed.append({"why": "expired" if held else "dead", "entry": entry})
                if prune:
                    with contextlib.suppress(OSError):
                        os.unlink(path)
            finally:
                os.close(descriptor)
        return live, reclaimed

    def _report(self, reclaimed):
        if reclaimed:
            dead = sum(1 for item in reclaimed if item["why"] == "dead")
            _log(f"pod pool: reclaimed {dead} reservation(s) of a dead supervisor and {len(reclaimed) - dead} expired")

    def sweep(self):
        """Remove the reservations nobody holds or that expired; returns what was removed.

        Each item is ``{"why": "dead" | "expired", "entry": dict | None}``; ``entry`` carries the repository, run, job, phase
        and attempt, and ``requested``: True means a launch request may have created a pod that nobody tracks (the orphan
        sweep of a later slice looks for it by its labels). Never raises: a ledger that cannot be locked sweeps nothing.
        """
        try:
            with self._exclusive():
                _, reclaimed = self._scan(self.clock(), prune=True)
        except PoolError:
            return []
        self._report(reclaimed)
        return reclaimed

    # -- pacing -----------------------------------------------------------

    def _bucket(self, now, repair=False):
        """``(tokens, at)`` of the bucket after refilling to ``now``. A missing file is a full bucket, a corrupt one empty.

        With ``repair`` (an admission, under the lock) a corrupt bucket, and a bucket stamped in the future because the clock
        went back, are rewritten stamped ``now``: without that a refusal, which saves nothing, would leave the stamp where
        it was and the bucket would never refill.
        """
        path = self.directory / PACING_NAME
        try:
            data = json.loads(path.read_text())
            tokens, at = float(data["tokens"]), float(data["at"])
            if not (math.isfinite(tokens) and math.isfinite(at) and 0 <= tokens <= MAX_CEILING):
                raise ValueError
        except FileNotFoundError:
            return float(self.burst), now
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            tokens, at = 0.0, None  # fail closed: an unreadable bucket is an empty one, which refills on its own
        if at is None or now < at:  # corrupt, or the clock went back: no refill now, and do not wait for the old time
            tokens = min(tokens, float(self.burst))
            if repair:
                self._save_bucket(tokens, now)
            return tokens, now
        return min(float(self.burst), tokens + (now - at) / self.interval), now

    def _save_bucket(self, tokens, now):
        path = self.directory / PACING_NAME
        temp = path.with_name(f"{PACING_NAME}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            temp.write_text(json.dumps({"tokens": round(tokens, 9), "at": now}))
            os.replace(temp, path)
        except OSError:
            with contextlib.suppress(OSError):
                temp.unlink()
            raise Unavailable() from None

    def _refund(self):
        with self._exclusive():
            now = self.clock()
            tokens, _ = self._bucket(now)
            self._save_bucket(min(float(self.burst), tokens + 1), now)

    # -- admission --------------------------------------------------------

    def _external(self):
        """The launcher's live count, or ``Unavailable`` when it cannot be had. None without a ``live_count``."""
        if self.live_count is None:
            return None
        try:
            count = self.live_count()
        except Exception:  # noqa: BLE001 - any failure of the reader is a refusal, never a guess
            raise Unavailable() from None
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise Unavailable()
        return count

    def admit(self, request):
        """Try once, without waiting: an ``Admission`` with a reservation or a refusal. Never raises, never blocks long."""
        try:
            return self._admit(request, adopting=False)
        except (Unavailable, OSError):
            capacity.notice("factory: pod pool: its state or the launcher's count cannot be read; refusing pod sessions")
            return Admission(refusal=Refusal(UNAVAILABLE))

    def adopt(self, request):
        """Count a pod that is already running (a restarted manager found it): never refused for ceiling, budget or pacing.

        The reservation is taken as ``requested`` (a pod exists), takes no pacing token and may put the pool over its
        ceiling (it then refuses every new admission until enough end). It is refused only as ``duplicate`` (the session is
        already counted), ``invalid`` or ``unavailable``.
        """
        try:
            return self._admit(request, adopting=True)
        except (Unavailable, OSError):
            return Admission(refusal=Refusal(UNAVAILABLE))

    def _admit(self, request, adopting):
        if not isinstance(request, Request):
            return Admission(refusal=Refusal(INVALID))
        if not adopting and self.breaker_dir is not None and stopworld.read_state(self.breaker_dir) is not None:
            return Admission(refusal=Refusal(BREAKER))
        external = None if adopting else self._external()
        with self._exclusive():
            now = self.clock()
            live, reclaimed = self._scan(now, prune=True)
            self._report(reclaimed)
            path = self.directory / f"{PREFIX}{hashlib.sha256(request.identity.encode()).hexdigest()[:32]}"
            if any(item["path"] == path for item in live):
                return Admission(refusal=Refusal(DUPLICATE))
            if adopting:
                return self._grant(request, path, now, tokens=None)
            in_use = max(len(live), external or 0)
            refusal, tokens = self._check(request, live, in_use, now)
            if refusal is not None:
                if refusal.code in (REPO_CAP, CEILING, PACING):
                    self._queue._touch(self._owner(request), now)  # a waiting request keeps its place by asking again
                return Admission(refusal=refusal)
            return self._grant(request, path, now, tokens=tokens)

    @staticmethod
    def _owner(request):
        return capacity.Owner(request.repo, f"{request.run}/{request.job}", f"{request.phase}/{request.attempt}")

    def _check(self, request, live, in_use, now):
        """``(refusal, tokens)``: the first refusal for ``request`` (tokens None), or None and the refilled bucket."""
        budget = request.budget_micro
        if budget is not None:
            base = request.spent_micro + request.estimate_micro
            held = sum(item["estimate"] for item in live if item["repo"] == request.repo and item["run"] == request.run)
            if base > budget:
                return Refusal(BUDGET), None
            if base + held > budget:
                return Refusal(BUDGET_HELD), None
        by_repo = collections.Counter(item["repo"] for item in live)
        if by_repo[request.repo] >= self.per_repo_cap:
            return Refusal(REPO_CAP, in_use=by_repo[request.repo], ceiling=self.per_repo_cap), None
        free = self.ceiling - in_use
        if free <= 0:
            return Refusal(CEILING, in_use=in_use, ceiling=self.ceiling), None
        tokens, _ = self._bucket(now, repair=True)
        if tokens < 1 - 1e-9:
            wait = max(1, math.ceil((1 - tokens) * self.interval))
            return Refusal(PACING, retry_after=wait, in_use=in_use, ceiling=self.ceiling), None
        # Fairness among the requests that could be admitted now: capacity's greedy fair share over the live holders.
        # A repository at its own cap cannot use a slot, so its waiters do not stand in front of anyone.
        owner = self._owner(request)
        self._queue._touch(owner, now)
        capped = {capacity._clean(repo) for repo, count in by_repo.items() if repo is not None and count >= self.per_repo_cap}
        holders = [{"repo": capacity._clean(item["repo"])} for item in live]
        waiters = [w for w in self._queue.waiters(now) if w["key"] == owner.key or w["repo"] not in capped]
        order = capacity.admission_order(holders, waiters, min(free, math.floor(tokens + 1e-9)))
        if owner.key not in order:
            return Refusal(QUEUED, in_use=in_use, ceiling=self.ceiling), None
        return None, tokens

    def _grant(self, request, path, now, tokens):
        """Create the reservation file, take its lock, spend the token; the ledger lock is held by the caller.

        ``tokens`` is the refilled bucket, or None for an adoption (no token is spent and the pod exists).
        """
        data = {
            "v": 1,
            "repo": request.repo,
            "run": request.run,
            "job": request.job,
            "phase": request.phase,
            "attempt": request.attempt,
            "estimate": request.estimate_micro,
            "granted": now,
            "expires": now + request.hold_seconds,
            "requested": tokens is None,
            "pid": os.getpid(),
        }
        try:
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        except OSError:  # a file of that name that the scan did not see cannot exist under the lock
            raise Unavailable() from None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _write(descriptor, data)
            if tokens is not None:
                self._save_bucket(max(0.0, tokens - 1), now)
            inode = os.fstat(descriptor).st_ino
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(path)
            os.close(descriptor)
            raise
        self._queue._drop(self._owner(request).key)
        return Admission(reservation=Reservation(self, descriptor, path, inode, data, refundable=tokens is not None))

    def withdraw(self, request):
        """Remove the waiting ticket of ``request``: a session that stopped asking keeps nobody behind it."""
        with contextlib.suppress(OSError, AttributeError):
            self._queue._drop(self._owner(request).key)

    def snapshot(self):
        """Read-only view for ``status``: ceiling, holders, the bucket and the waiting tickets; None when unreadable.

        Changes no reservation, bucket or ticket and removes nothing (taking the ledger lock creates the directory and lock
        file when they are missing); a file nobody holds is not shown. Counts and ids only, no secrets.
        """
        try:
            with self._exclusive():
                now = self.clock()
                live, _ = self._scan(now, prune=False)
                tokens, _ = self._bucket(now)
        except PoolError:
            return None
        waiting = self._queue.waiters(now, prune=False)
        return {
            "ceiling": self.ceiling,
            "in_use": len(live),
            "free": max(0, self.ceiling - len(live)),
            "per_repo_cap": self.per_repo_cap,
            "pacing_tokens": round(tokens, 3),
            "reserved_usd": sum(item["estimate"] for item in live) / MICRO,
            "holders": [
                {
                    "repo": item["repo"],
                    "run": item.get("run"),
                    "job": item.get("job"),
                    "phase": item.get("phase"),
                    "attempt": item.get("attempt"),
                    "estimate_usd": item["estimate"] / MICRO,
                    "requested": item["requested"],
                }
                for item in live
            ],
            "waiting": [{key: waiter[key] for key in ("repo", "job", "phase", "since")} for waiter in waiting],
        }


# ---------------------------------------------------------------------------
# Waiting, the environment and the launcher
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def hold(pool, request, seconds, sleep=time.sleep, clock=time.monotonic):
    """Wait up to ``seconds`` for an admission and release the reservation on every way out of the block.

    ``pool=None`` (the switch is off) is a programming error here: the pod backend only runs with a pool, so it raises
    ``Refused(UNAVAILABLE)``, fail closed. A final refusal raises ``Refused`` at once; a waiting one is retried (sleeping
    its ``retry_after`` or ``POLL_SECONDS``, at most ``MAX_POLL_SECONDS``) and ends as ``Timeout`` carrying the last code.
    """
    if pool is None:
        raise Refused(UNAVAILABLE)
    deadline = clock() + seconds
    while True:
        admission = pool.admit(request)
        if admission.reservation is not None:
            break
        refusal = admission.refusal
        if not refusal.waiting:
            raise Refused(refusal.code)
        if clock() >= deadline:
            pool.withdraw(request)
            raise Timeout(refusal.code, refusal.retry_after)
        pause = min(MAX_POLL_SECONDS, max(float(POLL_SECONDS), float(refusal.retry_after or 0)))
        capacity.notice(f"factory: {refusal.text}; waiting")
        sleep(max(0.0, min(pause, deadline - clock())))
    with admission.reservation as reservation:
        yield reservation


def parse_ceiling(env=None):
    """The configured ceiling, or ``ConfigRefused``: unset, empty and anything that is not a plain integer 0..MAX_CEILING refuse.

    ``0`` is valid and refuses every admission (a person pauses pod sessions without switching the backend off).
    Terraform always sets the name from the NodePool's ceiling local, so an unset value is a misconfiguration, not a default.
    """
    env = os.environ if env is None else env
    raw = env.get(ENV_CEILING)
    if raw is None or raw == "":
        raise ConfigRefused("ceiling_unset")
    if not re.fullmatch(r"[0-9]{1,5}", raw) or int(raw) > MAX_CEILING:
        raise ConfigRefused("ceiling_invalid")
    return int(raw)


def pool_directory(env=None):
    """The ledger directory: the pod-wide state directory of ``capacity`` plus ``pods``; None without a data directory."""
    base = capacity.pool_directory(os.environ if env is None else env)
    return None if base is None else base / SUBDIRECTORY


def from_env(env=None, live_count=None, clock=time.time):
    """The process's pool, or None while ``FACTORY_POD_BACKEND`` is not ``on`` (nothing is read, written or created).

    With the switch on it raises ``ConfigRefused`` (``ceiling_unset``, ``ceiling_invalid``, ``state_dir``) rather than
    run without a guard.
    """
    env = os.environ if env is None else env
    if env.get(pc.ENV_MANAGER["pod_backend"]) != pc.POD_BACKEND_ON:  # the rule of ``podexecution.switch_is_on``
        return None
    ceiling = parse_ceiling(env)
    directory = pool_directory(env)
    breaker_dir = env.get(ENV_DATA)
    if directory is None or not breaker_dir:  # the ledger may sit elsewhere (FACTORY_CAPACITY_DIR), the breaker marker may not
        raise ConfigRefused("state_dir")
    pool = PodPool(directory, ceiling, clock=clock, live_count=live_count, breaker_dir=breaker_dir)
    try:
        pool.prepare()
    except OSError:
        raise ConfigRefused("state_dir") from None
    return pool


def launcher_live_count(client):
    """A ``live_count`` for ``PodPool`` that asks the launcher client (``LauncherClient.info``): live plus in-flight pods.

    The launcher counts the pods it lists plus the launches it has admitted that a listing does not show yet, which is
    what its own cap counts. A reply without both numbers is an error (the pool then refuses), never a zero.
    """

    def count():
        info = client.info()
        live, inflight = info.get("live"), info.get("inflight")
        for number in (live, inflight):
            if isinstance(number, bool) or not isinstance(number, int) or number < 0:
                raise ValueError("launcher count unreadable")
        return live + inflight

    return count

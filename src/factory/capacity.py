"""Pod-wide capacity: how many workers the pod can carry, and who may start one.

Every repository runs its own supervisor process, so per-run limits (``concurrency``,
``max_active_agents``) say nothing about the pod. This module adds two things:

* a **worker budget** shared by all supervisors of the pod: a pool of flock'd slot files
  below ``$FACTORY_DATA/.capacity``. The kernel drops a flock when its holder dies, so a
  killed supervisor leaks nothing. Waiters leave short-lived tickets so slots go to the
  repository holding the fewest first (oldest waiter breaks ties);
* the **resource reading and derivation** behind the defaults (cgroup limits, with the host
  as fallback) for the budget and for the per-session memory limit enforced by
  ``memwatch``.

The footprint of a build, review or verify session was measured on one deployed pod only
(see the figures below), so the per-worker constants are deliberately generous: a default
deployment keeps running as before. Nothing is measured here; ``docs/capacity.md`` says how
to measure on your own pod.

Everything fails open: if the pod's resources or the pool directory cannot be read, work
starts without the limit and one fixed, token-free line says so. No value from the
environment is ever echoed.
"""

import fcntl
import hashlib
import json
import os
import re
import sys
import threading
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

CGROUP_ROOT = "/sys/fs/cgroup"
PROC_ROOT = "/proc"

# Operator settings (documented in docs/capacity.md). 0 or "off" turns a limit off.
ENV_MAX_WORKERS = "FACTORY_MAX_TOTAL_WORKERS"
ENV_WORKER_MEMORY = "FACTORY_WORKER_MEMORY_MB"
ENV_CAPACITY_DIR = "FACTORY_CAPACITY_DIR"
ENV_WAIT_SECONDS = "FACTORY_CAPACITY_WAIT_SECONDS"

# Measured (docs/capacity.md, "Measurements", 2026-11-10, 97 sessions with a footprint record on the deployed pod,
# 29 to 30 per phase for the heaviest repository): p95 resident memory 1,380 to 1,520 MiB for the `factory` repository
# (build, review, verify), 270 MiB for a smaller repository; p95 cpu 450 to 470 cpu-seconds per session against a median wall time
# of 2 to 5 minutes and a p95 of 11 to 28, i.e. about 0.3 to 0.4 cores on average per running worker. A worker is mostly
# waiting on the model API, but a session that builds and tests can use more than the memory figure, which is why the
# default per-session memory limit is several fair shares, not one.
WORKER_CPU_MILLI = 400
WORKER_MEMORY_MIB = 1536
# The default per-session memory limit: this many fair shares ((memory - reserve) / workers,
# never less than WORKER_MEMORY_MIB), never more than everything but the reserve.
MEMORY_CEILING_SHARES = 4
# Memory kept for everything that is not a worker: the manager, one supervisor per
# repository, short CLI commands, git and the page cache.
RESERVE_MAX_MIB = 3072
DEFAULT_WAIT_SECONDS = 900
MIB = 1024 * 1024

TICKET_TTL_SECONDS = 15.0
POLL_SECONDS = 1.0

_OFF, _SET, _DEFAULT = "off", "set", "default"
_NUMBER = re.compile(r"[0-9]{1,9}")
_SAFE = re.compile(r"[A-Za-z0-9_.:/#@-]{1,512}")
_SHOWN_CHARS = 100  # longest value shown as is; longer ones are shortened by ``_clean``
_KEY_CHARS = 2048

_NOTICED = set()
_NOTICE_LOCK = threading.Lock()


def notice(line):
    """Print a fixed diagnostic once per process (stderr); callers pass constant text only."""
    with _NOTICE_LOCK:
        if line in _NOTICED:
            return
        _NOTICED.add(line)
    print(line, file=sys.stderr, flush=True)


def _clean(value):
    """A value read from a pool file, as text safe to print; anything unexpected becomes ``?``.

    A long but well-formed value (a repository slug may be 200 characters) is shortened to a
    prefix and a digest, so distinct values stay distinct. Never use the result to find a
    ticket again: ``Owner.key`` is the identity, this is only for display and grouping.
    """
    if not isinstance(value, str) or not _SAFE.fullmatch(value):
        return "?"
    if len(value) <= _SHOWN_CHARS:
        return value
    return f"{value[:64]}...{hashlib.sha256(value.encode()).hexdigest()[:12]}"


# ---------------------------------------------------------------------------
# Resources of the pod and the defaults derived from them
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Resources:
    cpus: float | None
    memory_bytes: int | None
    source: str  # "cgroup2", "cgroup1" or "host"


def _read(path):
    try:
        return Path(path).read_text().strip()
    except (OSError, UnicodeDecodeError):
        return None


def _positive(text):
    try:
        value = int(text)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _host_cpus():
    try:
        return len(os.sched_getaffinity(0)) or None
    except (AttributeError, OSError):
        return os.cpu_count()


def _host_memory(proc_root):
    text = _read(Path(proc_root) / "meminfo")
    for line in (text or "").splitlines():
        if line.startswith("MemTotal:"):
            fields = line.split()
            kib = _positive(fields[1]) if len(fields) >= 2 else None
            return kib * 1024 if kib else None
    return None


def detect_resources(cgroup_root=CGROUP_ROOT, proc_root=PROC_ROOT, host_cpus=None):
    """The cpu and memory this process may use: cgroup limit when set, else the host's.

    Handles cgroup v2 (``cpu.max``, ``memory.max``) and v1 (``cpu.cfs_quota_us``,
    ``memory.limit_in_bytes``). An unlimited cgroup, or one that cannot be read, falls back
    to the host's cpu count and ``MemTotal``; the smaller of the two always wins.
    """
    root = Path(cgroup_root)
    cpu_limit = memory_limit = None
    source = "host"
    cpu_max = _read(root / "cpu.max")
    if cpu_max is not None:
        source = "cgroup2"
        quota, _, period = cpu_max.partition(" ")
        if _positive(quota) and _positive(period):
            cpu_limit = int(quota) / int(period)
        memory_limit = _positive(_read(root / "memory.max"))
    else:
        quota = _read(root / "cpu" / "cpu.cfs_quota_us")
        period = _read(root / "cpu" / "cpu.cfs_period_us")
        limit = _read(root / "memory" / "memory.limit_in_bytes")
        if quota is not None or limit is not None:
            source = "cgroup1"
        if _positive(quota) and _positive(period):
            cpu_limit = int(quota) / int(period)
        memory_limit = _positive(limit)
    host_cpu = host_cpus if host_cpus is not None else _host_cpus()
    host_memory = _host_memory(proc_root)
    cpus = min((c for c in (cpu_limit, host_cpu) if c), default=None)
    memory = min((m for m in (memory_limit, host_memory) if m), default=None)
    return Resources(cpus, memory, source)


def reserve_bytes(memory_bytes):
    """Memory kept back for the manager, supervisors and short commands (a quarter on small pods)."""
    return min(RESERVE_MAX_MIB * MIB, memory_bytes // 4)


def derive_budget(cpus, memory_bytes):
    """Workers the pod can carry by the estimates above; at least 1; None when nothing is known.

    ``min(cpu / WORKER_CPU_MILLI, (memory - reserve) / WORKER_MEMORY_MIB)``.
    """
    limits = []
    if cpus:
        limits.append(int(cpus * 1000 // WORKER_CPU_MILLI))
    if memory_bytes:
        limits.append((memory_bytes - reserve_bytes(memory_bytes)) // (WORKER_MEMORY_MIB * MIB))
    return max(1, min(limits)) if limits else None


def derive_memory_limit_mb(memory_bytes, workers=None):
    """Default per-session ceiling in MiB; None when the pod's memory is unknown.

    ``MEMORY_CEILING_SHARES`` fair shares, a share being ``(memory - reserve) / workers`` and
    at least ``WORKER_MEMORY_MIB``, and never more than everything but the reserve. Generous
    on purpose (a build may use several times the estimate), yet finite, so it stops a
    runaway while the rest of the pod is still carrying its workers. Without ``workers`` the
    ceiling is everything but the reserve. ``FACTORY_WORKER_MEMORY_MB`` sets it by hand.
    """
    if not memory_bytes:
        return None
    usable = memory_bytes - reserve_bytes(memory_bytes)
    ceiling = usable
    if workers:
        share = max(WORKER_MEMORY_MIB * MIB, usable // workers)
        ceiling = min(usable, MEMORY_CEILING_SHARES * share)
    return max(1, ceiling // MIB)


def _override(env, name):
    """``(mode, value)`` of an operator setting: off (0/off), set (positive integer) or default."""
    raw = (env.get(name) or "").strip().lower()
    if not raw:
        return _DEFAULT, None
    if raw in {"0", "off"}:
        return _OFF, None
    if _NUMBER.fullmatch(raw):
        return _SET, int(raw)
    notice(f"factory: capacity: {name} is not a positive integer, 0 or off; using the derived default")
    return _DEFAULT, None


def total_budget(env=None, resources=None):
    """The pod's worker budget: the operator's number, off (None) or the derived default."""
    env = os.environ if env is None else env
    mode, value = _override(env, ENV_MAX_WORKERS)
    if mode == _OFF:
        return None
    if mode == _SET:
        return value
    resources = resources or detect_resources()
    budget = derive_budget(resources.cpus, resources.memory_bytes)
    if budget is None:
        notice("factory: capacity: pod worker budget off: the pod's cpu and memory could not be read")
    return budget


def _sizing_workers(env, resources):
    """The worker count a fair memory share is computed for: the operator's budget, else derived.

    A budget turned off still has an estimate: it is only the admission limit that goes away.
    """
    mode, value = _override(env, ENV_MAX_WORKERS)
    if mode == _SET:
        return value
    return derive_budget(resources.cpus, resources.memory_bytes)


def worker_memory_limit_mb(env=None, resources=None):
    """Per-session memory limit in MiB: the operator's number, off (None) or the derived ceiling."""
    env = os.environ if env is None else env
    mode, value = _override(env, ENV_WORKER_MEMORY)
    if mode == _OFF:
        return None
    if mode == _SET:
        return value
    resources = resources or detect_resources()
    limit = derive_memory_limit_mb(resources.memory_bytes, _sizing_workers(env, resources))
    if limit is None:
        notice("factory: capacity: memory guard limit off: the pod's memory could not be read")
    return limit


def wait_seconds(env=None):
    """How long a plan or preflight session may wait for a slot (``FACTORY_CAPACITY_WAIT_SECONDS``)."""
    env = os.environ if env is None else env
    raw = (env.get(ENV_WAIT_SECONDS) or "").strip()
    return int(raw) if _NUMBER.fullmatch(raw) else DEFAULT_WAIT_SECONDS


# ---------------------------------------------------------------------------
# Admission order (pure) and the cross-process slot pool
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Owner:
    """Who asks for a slot: a repository (``org/name`` or ``local``), a job and a phase."""

    repo: str
    job: str
    phase: str

    @property
    def key(self):
        return f"{self.repo}|{self.job}|{self.phase}"


def admission_order(holders, waiters, free):
    """Keys of the waiters allowed to try for a slot now, best first, at most ``free`` of them.

    Greedy fair share: each pick goes to the waiter whose repository holds the fewest slots
    (counting the picks already made), the oldest ticket first, then by repository and job.
    A repository that already holds many slots therefore cannot starve one holding none.
    """
    held = {}
    for holder in holders:
        held[holder.get("repo")] = held.get(holder.get("repo"), 0) + 1
    remaining = sorted(waiters, key=lambda w: (w["since"], w["repo"], w["job"], w["phase"]))
    order = []
    while remaining and len(order) < free:
        best = min(remaining, key=lambda w: held.get(w["repo"], 0))
        remaining.remove(best)
        held[best["repo"]] = held.get(best["repo"], 0) + 1
        order.append(best["key"])
    return order


def reason_text(in_use, total, ahead=None):
    """The fixed-format wait reason shown in ``status`` (counts only, no names)."""
    text = f"pod worker budget: {in_use}/{total} workers in use"
    return f"{text}; {ahead} waiting ahead" if ahead else text


class Lease:
    """One held slot; ``release`` is idempotent and also works as a context manager."""

    def __init__(self, descriptor=None):
        self._descriptor = descriptor
        self._lock = threading.Lock()

    def release(self):
        with self._lock:
            descriptor, self._descriptor = self._descriptor, None
        if descriptor is None:
            return
        with suppress(OSError):  # the slot is freed by the close below either way
            os.ftruncate(descriptor, 0)
        os.close(descriptor)  # closing the descriptor drops the flock

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()

    def __del__(self):
        self.release()


class SlotPool:
    """``total`` slots shared by every supervisor of the pod; see the module docstring."""

    def __init__(self, directory, total, clock=time.time, ticket_ttl=TICKET_TTL_SECONDS):
        self.directory = Path(directory)
        self.total = total
        self.clock = clock
        self.ticket_ttl = ticket_ttl
        self.slots = self.directory / "slots"
        self.waiting = self.directory / "waiting"

    def prepare(self):
        for path in (self.slots, self.waiting):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)

    # -- slots ------------------------------------------------------------

    def _slot_files(self):
        try:
            return sorted(self.slots.glob("slot-*"))
        except OSError:
            return []

    def _locked(self, path, read=False):
        """Whether another open file description holds ``path``; ``(locked, metadata)``."""
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return False, {}
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                data = {}
                if read:
                    try:
                        loaded = json.loads(os.pread(descriptor, 4096, 0) or b"{}")
                        data = loaded if isinstance(loaded, dict) else {}
                    except (OSError, ValueError):
                        data = {}
                return True, data
            return False, {}
        finally:
            os.close(descriptor)

    def holders(self):
        """The slots held right now: ``{"slot", "repo", "job", "phase", "since"}`` each."""
        found = []
        for path in self._slot_files():
            locked, data = self._locked(path, read=True)
            if locked:
                found.append(
                    {
                        "slot": path.name,
                        "repo": _clean(data.get("repo")),
                        "job": _clean(data.get("job")),
                        "phase": _clean(data.get("phase")),
                        "since": data.get("since") if isinstance(data.get("since"), (int, float)) else 0,
                    }
                )
        return found

    def _take(self, owner, now):
        """Lock the first free slot below ``total`` without ever exceeding it; None when lost."""
        self.prepare()
        for index in range(self.total):
            path = self.slots / f"slot-{index:03d}"
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(descriptor)
                continue
            others = sum(1 for other in self._slot_files() if other != path and self._locked(other)[0])
            if others + 1 > self.total:
                # Slots above a lowered total are still held: the budget wins.
                os.close(descriptor)
                return None
            payload = json.dumps(
                {"repo": owner.repo, "job": owner.job, "phase": owner.phase, "pid": os.getpid(), "since": now}
            ).encode()
            try:
                os.ftruncate(descriptor, 0)
                os.pwrite(descriptor, payload, 0)
            except OSError:
                os.close(descriptor)
                raise
            return Lease(descriptor)
        return None

    # -- tickets ----------------------------------------------------------

    def _ticket_path(self, key):
        return self.waiting / hashlib.sha256(key.encode()).hexdigest()[:32]

    @staticmethod
    def _first_seen(path, now):
        """The ``since`` of the ticket at ``path``, or ``now`` when there is none worth keeping."""
        try:
            loaded = json.loads(path.read_text())
        except (OSError, ValueError):
            return now
        if isinstance(loaded, dict) and isinstance(loaded.get("since"), (int, float)):
            return loaded["since"]
        return now

    def _touch(self, owner, now):
        """Create or refresh the owner's ticket; the first-seen time (its age) is kept."""
        self.prepare()
        path = self._ticket_path(owner.key)
        payload = {
            "repo": owner.repo,
            "job": owner.job,
            "phase": owner.phase,
            "key": owner.key,
            "since": self._first_seen(path, now),
            "seen": now,
        }
        temp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        temp.write_text(json.dumps(payload))
        os.replace(temp, path)

    def _drop(self, key):
        with suppress(OSError):  # already gone: someone else withdrew it
            self._ticket_path(key).unlink()

    def waiters(self, now=None, prune=True):
        """Live tickets as dicts with a ``key``; stale ones (a dead supervisor) are removed.

        ``repo``, ``job`` and ``phase`` are display text (``_clean``); ``key`` is the owner's
        own ``Owner.key`` as stored in the ticket, so it matches whatever the owner asks with.
        """
        now = self.clock() if now is None else now
        found = []
        try:
            entries = sorted(self.waiting.glob("*"))
        except OSError:
            return found
        for path in entries:
            if path.suffix == ".tmp":
                continue
            try:
                data = json.loads(path.read_text())
                seen, since = float(data["seen"]), float(data["since"])
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if now - seen > self.ticket_ttl:
                if prune:
                    with suppress(OSError):  # a racing supervisor may have removed it
                        path.unlink()
                continue
            owner = Owner(_clean(data.get("repo")), _clean(data.get("job")), _clean(data.get("phase")))
            key = data.get("key")
            if not isinstance(key, str) or not 0 < len(key) <= _KEY_CHARS:
                key = owner.key
            found.append({"repo": owner.repo, "job": owner.job, "phase": owner.phase, "since": since, "key": key})
        return found

    def withdraw(self, repo=None):
        """Remove tickets (all of them, or only those of ``repo``): a supervisor leaving the queue."""
        shown = None if repo is None else _clean(repo)
        for waiter in self.waiters(prune=False):
            if shown is None or waiter["repo"] == shown:
                self._drop(waiter["key"])

    # -- admission --------------------------------------------------------

    def try_acquire(self, owner):
        """Non-blocking: ``(lease, None)`` when admitted, else ``(None, reason)``.

        A pool that cannot be used (unwritable directory, no flock) admits without a limit and
        says so once: the budget must never stop work on a guess.
        """
        try:
            return self._try_acquire(owner)
        except OSError:
            notice("factory: capacity: pod worker budget unavailable: its state directory is not usable; running without it")
            return Lease(), None

    def _try_acquire(self, owner):
        now = self.clock()
        self._touch(owner, now)
        holders = self.holders()
        waiters = self.waiters(now)
        in_use = len(holders)
        free = max(0, self.total - in_use)
        allowed = admission_order(holders, waiters, free)
        if owner.key in allowed:
            lease = self._take(owner, now)
            if lease is not None:
                self._drop(owner.key)
                return lease, None
            return None, reason_text(in_use, self.total)
        everyone = admission_order(holders, waiters, len(waiters))
        ahead = everyone.index(owner.key) if free and owner.key in everyone else 0
        return None, reason_text(in_use, self.total, ahead)

    def snapshot(self):
        """Read-only view for ``status``: totals, holders and waiters (writes nothing)."""
        holders = self.holders()
        waiters = self.waiters(prune=False)
        return {
            "total": self.total,
            "in_use": len(holders),
            "free": max(0, self.total - len(holders)),
            "holders": holders,
            "waiting": [{k: w[k] for k in ("repo", "job", "phase", "since")} for w in waiters],
        }


class CapacityTimeout(RuntimeError):
    """No slot became free within the allowed time (plan and preflight sessions only)."""


@contextmanager
def hold(pool, owner, seconds, sleep=time.sleep, clock=time.monotonic):
    """Block for a slot around a session that runs outside the scheduler (plan, preflight).

    Waits up to ``seconds``, printing the fixed reason once, then raises ``CapacityTimeout``.
    ``pool=None`` (no budget) holds nothing.
    """
    if pool is None:
        yield None
        return
    deadline = clock() + seconds
    while True:
        lease, reason = pool.try_acquire(owner)
        if lease is not None:
            break
        if clock() >= deadline:
            pool.withdraw(owner.repo)
            raise CapacityTimeout(f"{reason}; no slot became free within {seconds} s")
        notice(f"factory: {reason}; waiting for a slot")
        sleep(POLL_SECONDS)
    try:
        yield lease
    finally:
        lease.release()


def pool_directory(env=None):
    """``$FACTORY_CAPACITY_DIR``, else ``$FACTORY_DATA/.capacity``; None without a data directory."""
    env = os.environ if env is None else env
    explicit = env.get(ENV_CAPACITY_DIR)
    if explicit:
        return Path(explicit)
    data = env.get("FACTORY_DATA")
    return Path(data) / ".capacity" if data else None


def pool_from_env(env=None, resources=None):
    """The pod's ``SlotPool``, or None (local mode, budget off, or a directory that cannot be made)."""
    env = os.environ if env is None else env
    directory = pool_directory(env)
    if directory is None:
        return None
    total = total_budget(env, resources)
    if total is None:
        return None
    pool = SlotPool(directory, total)
    try:
        pool.prepare()
    except OSError:
        notice("factory: capacity: pod worker budget off: its state directory cannot be created")
        return None
    return pool


def snapshot_from_env(env=None):
    """The budget's current view for ``status``, or None when there is no pool or it cannot be read."""
    env = os.environ if env is None else env
    directory = pool_directory(env)
    if directory is None or not (directory / "slots").is_dir():
        return None
    total = total_budget(env)
    if total is None:
        return None
    try:
        return SlotPool(directory, total).snapshot()
    except OSError:
        return None

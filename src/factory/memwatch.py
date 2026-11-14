"""Per-session memory guard and footprint record, sampled from ``/proc``.

A worker is a process tree (the agent, its sandbox, the tools it runs). A thread started
right after the worker is spawned samples that tree every few seconds, sums resident set
sizes, remembers the peak and the cpu time, and, when a limit is set and one sample is over
it, stops the session so one runaway cannot take the pod down.

Stopping a session means more than signalling the worker's process group: a descendant that
moved to its own session or group (``setsid``, ``setpgid``) is outside the group but inside the
tree. The watchdog therefore freezes (SIGSTOP) the worker's whole group in one step, so nothing in
it can fork any more, then signals every process of the sampled tree one by one and keeps
sweeping, pass after pass, for whatever the tree spawns meanwhile, until no process of it is left
or ``KILL_TIMEOUT_SECONDS`` run out (then one fixed line says so). A sweep first freezes what it
finds, rescans, and kills only once a scan finds nothing new: a killed parent re-parents its
children, so killing first would let a child that was forked a moment earlier, and detached from
the group since, slip out of the tree. Nothing is killed before everything found is frozen; the
group is sent SIGKILL last, as the catch-all for what a sweep that cannot read ``/proc`` missed.

What this protects against, and what it cannot:

* A process is identified by ``(pid, start time)``, never by pid alone, and it is signalled
  through a pidfd (``pidfd_open`` checked against the start time, then ``pidfd_send_signal``)
  where the kernel has one, so a pid that was reused after the scan is never signalled. Without
  pidfd the start time is re-read right before ``kill`` and only that microsecond window remains.
* pid 1, this process, its parent and every ancestor of this process (the manager) are never
  signalled and never adopted into the tree, whatever ``/proc`` says.
* Membership is by parent pid and by session: a process is in the tree when its parent is, or
  it is in a session led (or once led) by a member. A process that detaches before any scan saw
  it (double fork, then ``setsid``, so its parent is init and its session is its own) is outside
  the tree: neither summed nor killed. Only a cgroup closes that hole; the pod cannot write one.
* The sweep is bounded: it ends after ``KILL_TIMEOUT_SECONDS`` (plus the one pass in flight) and
  ``stop()`` waits at most ``JOIN_SECONDS``. A process in uninterruptible sleep outlives both.

Why not a kernel limit: ``RLIMIT_AS`` is per process, not per tree, and Node reserves huge
virtual ranges; a per-session cgroup needs a writable cgroupfs, which the unprivileged pod
does not have. So this is a sampled guard: a fast allocator can pass the limit between two
samples, which is why the default ceiling is a few fair shares, not the container limit
(``capacity.derive_memory_limit_mb``). Resident sizes double count shared pages, which errs
on the side of stopping a session that is really over the limit.

It fails safe: if ``/proc`` cannot be read the session runs unlimited and one fixed line says
so; a session is only ever stopped by a successful sample that is over the limit. Nothing
here prints anything the worker wrote.

The worker controls what ``/proc`` shows about its own processes (a command name need not be
UTF-8, and may hold spaces, parentheses or newlines), so every file is read as bytes and a
process is never dropped from the sum because of how it is named.
"""

import os
import signal
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from .capacity import MIB, notice

INTERVAL_SECONDS = 2.0
# After a trip: how long to keep sweeping the tree, the pause between two kill passes, and the
# longest ``stop()`` waits for the sweep (it is a daemon thread; the supervisor never hangs).
KILL_TIMEOUT_SECONDS = 4.0
KILL_POLL_SECONDS = 0.05
JOIN_SECONDS = 5
# Process states that hold no memory and cannot be signalled any further.
_GONE_STATES = ("Z", "X", "x")


class Stat(NamedTuple):
    ppid: int
    session: int
    ticks: int
    state: str
    started: int  # start time in clock ticks since boot: with the pid, the identity of a process


@dataclass(frozen=True)
class Sample:
    rss_bytes: int
    cpu_seconds: float
    processes: int
    # ``(pid, start time)`` of every process counted, so a kill can tell a reused pid apart.
    members: tuple = field(default=(), compare=False)


def supported(proc_root="/proc"):
    """Whether the per-process files the guard needs exist (Linux ``/proc``)."""
    root = Path(proc_root)
    return (root / str(os.getpid()) / "statm").is_file() and (root / str(os.getpid()) / "stat").is_file()


def _stat_fields(proc_root, pid):
    """The ``Stat`` of a process, or None when it is gone or unreadable."""
    try:
        data = (Path(proc_root) / str(pid) / "stat").read_bytes()
        # The command name sits in parentheses and may hold anything (spaces, parentheses,
        # bytes that are not UTF-8); the last parenthesis ends it. Nothing is decoded.
        rest = data.rpartition(b")")[2].split()
        return Stat(
            ppid=int(rest[1]),
            session=int(rest[3]),
            ticks=sum(int(rest[index]) for index in (11, 12, 13, 14)),
            state=rest[0].decode("ascii", "replace"),
            started=int(rest[19]),
        )
    except (OSError, ValueError, IndexError):
        return None


def _resident_pages(proc_root, pid):
    try:
        return int((Path(proc_root) / str(pid) / "statm").read_bytes().split()[1])
    except (OSError, ValueError, IndexError):
        return None


def _process_table(proc_root):
    """``{pid: Stat}`` of every readable process, or None when ``proc_root`` cannot be listed."""
    try:
        names = [entry for entry in os.listdir(proc_root) if entry.isascii() and entry.isdigit()]
    except OSError:
        return None
    table = {}
    for name in names:
        stat = _stat_fields(proc_root, name)
        if stat is not None:
            table[int(name)] = stat
    return table


def _own_pids():
    """``(pid, parent pid)`` of this process."""
    return os.getpid(), os.getppid()


def _protected(table):
    """Pids that are never part of a worker tree: init, this process and all its ancestors."""
    me, parent = _own_pids()
    shielded = {1, me}
    pid = table[me].ppid if me in table else parent
    while pid in table and pid not in shielded:
        shielded.add(pid)
        pid = table[pid].ppid
    shielded.add(parent)
    return shielded


def _descendants(table, known, exclude=()):
    """Identities ``(pid, start time)`` in ``table`` that belong to the tree of ``known`` but are not in it.

    A process belongs when its parent is a live member, or it is in a session a member leads, or
    once led (the leader died, the session's processes keep its number; the kernel does not hand
    that number out again while any of them lives). A pid whose start time differs from the
    member's was reused by a stranger: neither its children nor its session are the tree's.
    ``exclude`` pids are never adopted. Linear in the size of ``table``.
    """
    children, sessions = defaultdict(list), defaultdict(list)
    for pid, stat in table.items():
        children[stat.ppid].append(pid)
        sessions[stat.session].append(pid)
    seen, found, queue = set(known), set(), []

    def adopt(pids):
        for pid in pids:
            ident = (pid, table[pid].started)
            if pid not in exclude and ident not in seen:
                seen.add(ident)
                found.add(ident)
                queue.append(pid)

    for pid, started in known:
        if pid in exclude:
            continue
        stat = table.get(pid)
        if stat is None:
            adopt(sessions.get(pid, ()))
        elif stat.started == started:
            queue.append(pid)
    while queue:
        pid = queue.pop()
        adopt(children.get(pid, ()))
        adopt(sessions.get(pid, ()))
    return found


def read_tree(root_pid, proc_root="/proc", page_size=None, clock_ticks=None, root_started=None):
    """Resident memory and cpu time of ``root_pid`` and everything below it, or None.

    A process belongs to the tree when its parent does, or when it is in a session a member
    leads (the worker is started as a session leader, so an orphan that was re-parented still
    counts). ``root_started`` pins the root's identity: a different start time at that pid means
    the number now belongs to someone else, and there is no tree.
    """
    page_size = page_size or os.sysconf("SC_PAGE_SIZE")
    clock_ticks = clock_ticks or os.sysconf("SC_CLK_TCK")
    table = _process_table(proc_root)
    if table is None or root_pid not in table:
        return None
    if root_started is not None and table[root_pid].started != root_started:
        return None
    root = {(root_pid, table[root_pid].started)}
    members = root | _descendants(table, root, _protected(table))
    pages = 0
    for pid, _ in members:
        resident = _resident_pages(proc_root, pid)
        if resident is not None:
            pages += resident
    ticks = sum(table[pid].ticks for pid, _ in members)
    return Sample(pages * page_size, ticks / clock_ticks, len(members), tuple(sorted(members)))


def _pidfd(pid):
    """A handle that names the process at ``pid`` for good (a pid can be reused, a pidfd cannot).

    None where the kernel or its syscall filter has none; ProcessLookupError when it is gone.
    """
    if getattr(os, "pidfd_open", None) is None or getattr(signal, "pidfd_send_signal", None) is None:
        return None
    try:
        return os.pidfd_open(pid)
    except ProcessLookupError:
        raise
    except OSError:
        return None


def _is_process(pid, started):
    """Whether the process now at ``pid`` is the one that started at ``started`` and still runs."""
    stat = _stat_fields("/proc", pid)
    return stat is not None and stat.started == started and stat.state not in _GONE_STATES


def kill_pid(pid, started, sig=signal.SIGKILL):
    """Signal one process if it is still the one that started at ``started``; True when sent.

    Fails closed: init, this process and its parent are refused, and so is a process whose
    start time cannot be read or differs (the pid was reused). With a pidfd the check and the
    signal refer to the same process for good; without one (or where the pidfd signal is
    filtered) the start time is re-read right before ``kill``. A process that may not be
    signalled is reported once, with no values.
    """
    if pid <= 1 or pid in _own_pids():
        return False
    try:
        handle = _pidfd(pid)
    except ProcessLookupError:
        return False
    try:
        if not _is_process(pid, started):
            return False
        if handle is not None:
            try:
                signal.pidfd_send_signal(handle, sig)
                return True
            except ProcessLookupError:
                return False
            except OSError:
                # A filtered syscall, or no permission: the plain call below says which.
                if not _is_process(pid, started):
                    return False
        os.kill(pid, sig)
        return True
    except ProcessLookupError:
        return False
    except OSError as error:
        notice(f"factory: memory guard could not signal a worker process: {error.strerror or 'error'}")
        return False
    finally:
        if handle is not None:
            os.close(handle)


def _never(pid, started, sig=signal.SIGKILL):
    """The kill used with a ``proc_root`` that is not the real ``/proc``: pids there mean nothing."""
    return False


class Watchdog:
    """Samples one worker tree; stops it, once, when a sample is over ``limit_mb``.

    ``on_exceed(sig)`` signals the worker's process group with ``sig``: SIGSTOP first, so that
    nothing in the group can fork any further, and SIGKILL last, once the sweep is over. In between
    the watchdog sweeps the sampled tree through ``kill(pid, started, sig)`` (a descendant in its
    own session or group is not in that group): freeze what is found, rescan until nothing new
    shows up, kill all, and repeat for whatever the tree spawns meanwhile, until no process of it
    is left or ``kill_timeout`` (seconds, ``KILL_TIMEOUT_SECONDS`` by default) runs out. ``kill``
    defaults to ``kill_pid``; with a ``proc_root`` other than the real ``/proc`` it defaults to a
    no-op, so a fake ``/proc`` can never reach a real process.
    """

    def __init__(
        self,
        pid,
        limit_mb,
        on_exceed,
        interval=None,
        proc_root="/proc",
        page_size=None,
        clock_ticks=None,
        kill=None,
        kill_timeout=None,
    ):
        self.pid = pid
        self.limit_bytes = limit_mb * MIB if limit_mb else None
        self.limit_mb = limit_mb or None
        self.on_exceed = on_exceed
        self.interval = INTERVAL_SECONDS if interval is None else interval
        self.proc_root = proc_root
        self.page_size, self.clock_ticks = page_size, clock_ticks
        if kill is None:
            kill = kill_pid if os.fspath(proc_root) == "/proc" else _never
        self.kill = kill
        self.kill_timeout = kill_timeout
        self.root_started = None  # start time of the worker, read by the first sample
        self.misses = 0  # samples that could not read the tree
        self.peak_bytes = 0
        self.cpu_seconds = 0.0
        self.samples = 0
        self.exceeded = None  # resident bytes of the first sample over the limit
        self._stop = threading.Event()
        self._thread = None

    def sample(self):
        """Take one sample, update the record, and stop the worker if it is over the limit."""
        found = read_tree(self.pid, self.proc_root, self.page_size, self.clock_ticks, self.root_started)
        if found is None:
            self._missed()
            return None
        if self.root_started is None:
            # The first sample pins which process this pid is: the worker is not reaped before
            # its supervisor waits for it, so until then no stranger can have the number.
            self.root_started = dict(found.members).get(self.pid)
        self.samples += 1
        self.peak_bytes = max(self.peak_bytes, found.rss_bytes)
        self.cpu_seconds = max(self.cpu_seconds, found.cpu_seconds)
        if self.limit_bytes and found.rss_bytes > self.limit_bytes and self.exceeded is None:
            self.exceeded = found.rss_bytes
            self._stop_tree(found)
        return found

    def _stop_tree(self, found):
        """Freeze the group, sweep the tree, and only then send the group its last SIGKILL.

        Freezing comes first because a kill first re-parents the children of the killed process:
        one that forked after the sample was read and then left the group (``setsid``) would
        belong to no member any more. A signal to the group reaches all of it at once, and a
        fork that races with it either ends up in the group or is retried after the signal. Each
        step runs even when the one before it raised.
        """
        try:
            self.on_exceed(signal.SIGSTOP)
        finally:
            try:
                self._kill_tree(found)
            finally:
                # The catch-all: a sweep that could not read /proc, or lost a process, leaves
                # the group frozen, never running.
                self.on_exceed(signal.SIGKILL)

    def _missed(self):
        """A sample that saw no worker. If none ever did, the guard is blind: say so once."""
        self.misses += 1
        if self.samples == 0 and self.misses == 2:
            notice(
                "factory: memory guard cannot read this worker's process tree; "
                "the session runs without a memory limit or footprint record"
            )

    def _alive(self, known):
        """Identities of the signallable processes of the tree (``known`` grows); None if ``/proc`` is gone."""
        table = _process_table(self.proc_root)
        if table is None:
            return None
        shielded = _protected(table)
        known -= {ident for ident in known if ident[0] in shielded}
        known |= _descendants(table, known, shielded)
        return {
            (pid, started)
            for pid, started in known
            if (stat := table.get(pid)) is not None and stat.started == started and stat.state not in _GONE_STATES
        }

    def _kill_tree(self, found):
        """Stop every process of the sampled tree and what it spawns, until none is left or time is up."""
        known = set(found.members)
        stopped, frozen = set(), set()
        limit = KILL_TIMEOUT_SECONDS if self.kill_timeout is None else self.kill_timeout
        deadline = time.monotonic() + limit
        try:
            while True:
                alive = self._alive(known)
                if alive is None:
                    return
                if not alive:
                    frozen.clear()
                    return
                fresh = alive - stopped
                if fresh and time.monotonic() < deadline:
                    # Frozen processes cannot fork: whatever they spawned before the signal
                    # arrived shows up in the next scan, still attached to its parent.
                    for pid, started in sorted(fresh):
                        self.kill(pid, started, signal.SIGSTOP)
                    stopped |= fresh
                    frozen |= fresh
                    continue
                for pid, started in sorted(alive):
                    self.kill(pid, started, signal.SIGKILL)
                frozen.clear()
                if time.monotonic() >= deadline:
                    notice("factory: memory guard could not stop every process of an over-limit worker")
                    return
                time.sleep(KILL_POLL_SECONDS)
        finally:
            # Never leave a process frozen, whatever ended the sweep.
            for pid, started in sorted(frozen):
                self.kill(pid, started, signal.SIGKILL)

    def _loop(self):
        while True:
            self.sample()
            if self.exceeded is not None or self._stop.wait(self.interval):
                return

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="memwatch", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=JOIN_SECONDS)

    def footprint(self):
        """The fixed, token-free record kept in the usage entry; None when nothing was sampled."""
        if not self.samples:
            return None
        return {
            "peak_rss_mb": round(self.peak_bytes / MIB, 1),
            "cpu_seconds": round(self.cpu_seconds, 2),
            "samples": self.samples,
            "limit_mb": self.limit_mb,
        }

    def exceeded_message(self):
        return (
            f"worker exceeded its memory limit: peak {int(self.exceeded // MIB)} MiB "
            f"over {self.limit_mb} MiB"
        )


def start(pid, limit_mb, on_exceed, proc_root="/proc", interval=None):
    """A running ``Watchdog`` for the worker ``pid``, or None (with one fixed line) if impossible."""
    if not supported(proc_root):
        notice(
            "factory: memory guard unavailable: /proc is not readable; "
            "sessions run without a memory limit or footprint record"
        )
        return None
    watchdog = Watchdog(pid, limit_mb, on_exceed, interval=interval, proc_root=proc_root)
    try:
        return watchdog.start()
    except RuntimeError:
        notice("factory: memory guard unavailable: its thread could not start; session runs without it")
        return None

"""The orphan sweep and the pod census of the pod backend: what is left behind, and what is still running.

Slice 20 of the pod backend (``docs/pod-backend.md``, issue #147). A pod outlives the process that launched it, so
three kinds of thing can be left behind when a supervisor dies, a manager restarts or a launch is cut off between
its two steps: a worker POD (and with it its Secret, which carries an ``ownerReference`` to the pod), a RELAY RECORD
(a session directory in the spool, up to 16 GiB of it is shared with every other session), and a job's state that
still names a pod. This module decides which of them nobody owns any more and removes them, and counts what is
still running so that the deploy gate can wait for it.

WHO OWNS A POD. A pod belongs to the job whose state records its ``pod`` handle (``podhandle``), and to nobody
when no job does. ``read_recorded`` reads every run's ``state.json`` and answers ``Recorded`` (session ->
``Entry``) and whether the answer is COMPLETE: one unreadable state file, one unreadable ``pod`` record, a job
status that is not a plain word (an unknown status is never "not running") or a truncated run listing makes it
incomplete, and an incomplete answer is never a reason to delete anything (a pod that "nobody owns" may be owned
by the file that could not be read). A missing configuration or runs directory is the caller's to report as
incomplete (``Operations.recorded_pods``): a directory that is gone is not an empty set of owners.

WHAT IS SWEPT (a closed set of reasons, ``REASONS``; every one needs the listing to have been read, and none acts on
a pod that is already terminating):

* ``unrecorded``: no job records the pod, it is older than ``LAUNCH_GRACE_SECONDS``, no launch of this process
  is in flight for it (``protect``) and its relay record is not live (see below). The grace is the window between the launcher's answer and the supervisor writing
  the handle into the state; a pod whose creation time cannot be read is never judged by age.
* ``finished``: the pod is over (a final state, not merely being deleted) and no running job can still collect it.
* ``stale_record``: a job that is NOT running still records the pod (the end of a session keeps the record when the
  termination could not be confirmed): the pod is deleted so that the next read says ``dead`` and recovery goes on.
  It gets the same protection as ``unrecorded``: the pod must be older than ``LAUNCH_GRACE_SECONDS`` (an age that
  cannot be read is never judged) and no launch of this process holds it (``protect``). A session that is not run
  by a supervisor (the ``factory preflight`` probe records a pod under a job that is not "running") would
  otherwise be taken for a stale record and deleted mid-session.
* THE RELAY RECORD HOLDS TOO (the cross-process hold, ``Session.live``): ``unrecorded``, ``finished`` and
  ``stale_record`` never apply to a pod whose relay record is still live (readable and not ended, not released, its
  deadline not passed; or unreadable). The sweep runs in the manager, the sessions in other processes (a plan
  session has no job in any state, a preflight probe's job is not "running", a supervisor may have restarted), and
  ``protect`` is process-local: the record, which the supervisor writes before the launch and ends by releasing it,
  is what every process can see. When the spool is configured but cannot be read, no such pod is deleted. A record
  that ended, was released, expired or is missing does not hold, so the pod of a session whose owner is gone is
  swept.
* ``supervisor_gone``: a RUNNING job records the pod and its repository has had no live supervisor for
  ``ORPHAN_SILENCE_SECONDS``, counted by this sweeper's own clock from the first pass that saw it (a restart of the
  manager starts the count again, which only delays). Nobody will collect that session's result.
* ``overdue``: the pod is past its deadline plus the termination grace and the launch grace and is not terminating
  (the kubelet enforces ``activeDeadlineSeconds`` only once it has accepted the pod, and the launcher's own sweep
  is the first line; this is the second).
* ``unrecorded_session`` (a spool directory, not a pod): no job records the session, no pod of that name is listed,
  the directory has been quiet for ``LAUNCH_GRACE_SECONDS`` and the listing is complete. Its Secret, which cannot
  be listed (the launcher's Role has no ``secrets`` list or get), is removed by asking for the derived pod name to be
  deleted (the launcher deletes the Secret by its derived name even when the pod is gone), and the session is
  revoked and released so the relay frees its spool at once.

A pod is deleted through the launcher (``delete_pod``: the pod and its Secret). Before it, the session's relay record
is REVOKED, so that a pod that is still talking is cut off. The delete is accepted, not awaited: a running pod stays
for its termination grace, is then ``terminating`` and is left alone until it is gone.

THE CENSUS (``census``) counts the labelled pods that may still run: ``live`` is every listed pod that is not over
(a terminating pod still runs, so it counts), ``final`` the ones that are over. A launcher that cannot be asked is
``unknown``, never zero: the deploy gate treats ``unknown`` as busy.

Fail closed throughout: anything unreadable is "do not delete". Nothing here prints, reads the environment or opens
the network; the launcher is reached only through the callables the caller injects. The operations hooks import this
module (``Operations.pod_sweep_pass``, ``recorded_pods``) but nothing runs it until ``Manager.pods`` is set by the
pod backend: dormant by default. Standard library only.
"""

import json
import os
import re
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from . import podcontract as pc
from . import podhandle, podliveness, podstatus, sessionrecord

# The reasons a pod or a relay record is removed.
UNRECORDED = "unrecorded"
FINISHED = "finished"
STALE_RECORD = "stale_record"
SUPERVISOR_GONE = "supervisor_gone"
OVERDUE = "overdue"
UNRECORDED_SESSION = "unrecorded_session"
REASONS = (UNRECORDED, FINISHED, STALE_RECORD, SUPERVISOR_GONE, OVERDUE, UNRECORDED_SESSION)

# The launcher lists at most this many pods (``launcher.MAX_LIST_PODS``; a test compares them): a listing that long
# may have been cut, so "no pod of that name" is not a fact then.
LIST_LIMIT = 1000

# The largest state file read (a run with thousands of jobs is well below it); a bigger one is "unreadable".
MAX_STATE_BYTES = 64 * 1024 * 1024

_SESSION_HEX = re.compile(r"[0-9a-f]{32}")
_STATUS = re.compile(r"[a-z_]{1,32}")
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


# --- What the jobs record -----------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    """A job that records a pod: where it is (repository, run, job id) and its raw status word."""

    repo: str
    run: str
    job: str
    status: str
    handle: podhandle.PodHandle

    @property
    def running(self):
        return self.status == "running"


@dataclass(frozen=True)
class Recorded:
    """Every pod handle the job states record, by session id, and whether the answer is complete.

    ``complete`` is False when any state file or pod record could not be read (or the run listing was cut): the
    sweep then deletes nothing for want of an owner.
    """

    entries: dict = field(default_factory=dict)
    complete: bool = True


def _read_state_file(path):
    """The parsed ``state.json`` at ``path`` (None when there is no such file); ValueError/OSError when unreadable."""
    try:
        fd = os.open(path, _READ_FLAGS)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_STATE_BYTES:
            raise ValueError("state is not a readable regular file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(MAX_STATE_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_STATE_BYTES:
        raise ValueError("state is too large")
    return json.loads(raw.decode("utf-8"))


def read_recorded(files, complete=True):
    """``Recorded`` from ``files``, an iterable of ``(repo, run, path of its state.json)``.

    A run without a state file has no jobs (it was never started) and is fine. A file that cannot be read or parsed,
    a ``jobs`` that is not a mapping, and a ``pod`` record that does not validate (it names no session we can match)
    make the answer incomplete, as does a job status that is not a ``[a-z_]{1,32}`` string (missing, non-string,
    upper case, hyphenated, too long: the job is skipped, so its pod has no owner to be judged by);
    ``complete=False`` from the caller (a cut run listing, a missing directory) is kept. Where two jobs
    record the same session, a running one wins. Never raises.
    """
    entries = {}
    for repo, run, path in files:
        try:
            data = _read_state_file(path)
        except (OSError, ValueError, RecursionError):
            complete = False
            continue
        if data is None:
            continue
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, dict):
            complete = False
            continue
        for key, job in jobs.items():
            pod = job.get("pod") if isinstance(job, dict) else None
            if pod is None:
                continue
            try:
                handle = podhandle.PodHandle.from_record(pod)
            except ValueError:
                complete = False
                continue
            status = job.get("status")
            if not isinstance(status, str) or not _STATUS.fullmatch(status):
                complete = False  # an unknown status is not "not running": skip, never delete on it
                continue
            entry = Entry(str(repo), str(run), str(key), status, handle)
            held = entries.get(handle.session)
            if held is None or (entry.running and not held.running):
                entries[handle.session] = entry
    return Recorded(entries, complete)


# --- Launches in flight -------------------------------------------------------------------------

_protected = set()
_protected_lock = threading.Lock()


@contextmanager
def protect(session):
    """Hold a session out of the sweep while a launch of this process is between its pod and its recorded handle.

    The pod backend wraps ``launch`` and ``record_pod`` in it; the launch grace covers a launch that took a moment,
    this covers one that took longer.
    """
    pc.validate_session_id(session)
    with _protected_lock:
        _protected.add(session)
    try:
        yield
    finally:
        with _protected_lock:
            _protected.discard(session)


def protected():
    with _protected_lock:
        return frozenset(_protected)


def reset():
    """Forget every protected session (tests)."""
    with _protected_lock:
        _protected.clear()


# --- Reading the listing and the spool ------------------------------------------------------------


@dataclass(frozen=True)
class Listed:
    """One pod of the launcher's ``list`` answer, validated: only the closed fields survive."""

    name: str
    uid: str | None
    session: str
    state: str
    final: bool
    terminating: bool
    manager: str | None
    created: int | None
    deadline: int | None

    @property
    def over(self):
        """Over for Kubernetes: final, and not merely being deleted (a terminating pod still runs for its grace)."""
        return self.final and not self.terminating


def _epoch(value):
    return value if type(value) is int and 0 < value < 2**40 else None


def listed(pod):
    """A ``Listed`` for one entry of the launcher's ``list`` answer, or None when it is not a valid worker pod."""
    if not isinstance(pod, dict):
        return None
    name = pod.get("name")
    try:
        pc.secret_name(name)
    except ValueError:
        return None
    if pod.get("state") not in podstatus.STATES or not isinstance(pod.get("final"), bool):
        return None
    uid = pod.get("uid")
    manager = pod.get("manager")
    # A listing without ``terminating`` (a launcher older than this slice) reads a final pod as terminating: it may
    # still be running, so it is counted as live and never taken for a finished one.
    terminating = pod["terminating"] if isinstance(pod.get("terminating"), bool) else pod["final"]
    return Listed(
        name=name,
        uid=uid if isinstance(uid, str) and 0 < len(uid) <= 64 else None,
        session=name[len(pc.POD_NAME_PREFIX) :],
        state=pod["state"],
        final=pod["final"],
        terminating=terminating,
        manager=manager if isinstance(manager, str) and len(manager) <= 63 else None,
        created=_epoch(pod.get("created")),
        deadline=_epoch(pod.get("deadline")),
    )


@dataclass(frozen=True)
class Session:
    """A session directory of the spool: its name, the time of its last change, and whether it is over.

    ``ended`` is True for a relay session that is finished, expired, revoked or released, and for one past its
    deadline. A directory with no record (a leftover of a session that never started: the relay keeps it for its
    retention whatever markers it has) and one whose record cannot be read are NOT ended: nothing is concluded from a
    read error. ``released`` is True once the supervisor's ``released`` marker exists.

    ``live`` is True when the relay record says the session may still be in use: it is readable and not ended, or it
    cannot be read (nothing is concluded from a read error). This is the CROSS-PROCESS hold: a session of another
    process (the ``factory plan`` child, a ``factory preflight`` probe, a supervisor that has since restarted) is
    recorded in no job's state, but its relay record, written before the pod is launched and ended by the release
    after it, says it is still in use, so its pod is not an orphan. A record that is missing is not live: no call of
    the pod can succeed against it.
    """

    session: str
    modified: int
    ended: bool
    released: bool
    live: bool = False


def read_sessions(spool, now):
    """The ``Session`` of every session directory in the spool; an unreadable spool raises ``OSError``.

    Only real directories whose name is a session id count; links, files and other names are never looked at (the
    relay's own sweep keeps to the same rule).
    """
    found = []
    with os.scandir(spool) as entries:
        for entry in entries:
            if not _SESSION_HEX.fullmatch(entry.name):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISDIR(info.st_mode):
                continue
            relay = podliveness.read_relay(spool, entry.name)
            if relay.read == "ok":
                ended = (
                    relay.released
                    or relay.revoked
                    or relay.state in sessionrecord.TERMINAL_STATES
                    or (relay.deadline is not None and now >= relay.deadline)
                )
                released = relay.released
                live = not ended
            else:
                ended, released = False, False
                live = relay.read == "unreadable"
            found.append(Session(entry.name, int(info.st_mtime), ended, released, live))
    return found


# --- The sweep ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Action:
    """One removal the sweep decided on: a ``pod`` (delete it) or a ``session`` (revoke, release), and why."""

    kind: str
    session: str
    reason: str
    name: str | None = None
    uid: str | None = None


@dataclass
class Report:
    """What one pass did. Counts only; no name, no session id."""

    deleted: dict = field(default_factory=dict)  # reason -> pods asked to be deleted
    released: int = 0  # relay records revoked and released
    failed: int = 0  # removals that could not be carried out
    skipped: int = 0  # pods that were left alone because their age or owner could not be judged
    terminating: int = 0  # pods already going
    incomplete: bool = False  # the owners could not all be read: nothing was deleted for want of one
    unreadable: bool = False  # the launcher could not be asked: nothing was done

    def total(self):
        return sum(self.deleted.values()) + self.released

    def line(self):
        """The one fixed log line, or None when the pass did nothing worth a line."""
        if not (self.total() or self.failed or self.incomplete or self.unreadable):
            return None
        deleted = sum(self.deleted.values())
        return (
            f"pod sweep deleted={deleted} released={self.released} failed={self.failed} "
            f"incomplete={int(self.incomplete)} unreadable={int(self.unreadable)}"
        )


def plan(
    pods,
    recorded,
    sessions,
    now,
    supervisor_gone,
    hold=frozenset(),
    grace=pc.LAUNCH_GRACE_SECONDS,
    relay_unreadable=False,
):
    """The ``Action`` list for one pass, and the counts of what was left alone: ``(actions, skipped, terminating)``.

    ``pods`` are ``Listed`` (the launcher's listing), ``recorded`` is ``Recorded``, ``sessions`` the spool's
    ``Session`` list or None when the spool could not be read, ``now`` epoch seconds, ``supervisor_gone`` a callable
    ``(repo, run) -> bool`` that is True only for a supervisor that has been gone long enough (see ``Sweeper``), and
    ``hold`` the sessions of launches in flight (this process). Pure: nothing is read or changed.

    A pod whose relay record is LIVE (``Session.live``: not ended, not released, deadline not passed, or not
    readable) is not an orphan whatever the jobs record: the session may belong to a process that records no job
    handle (a plan session, a preflight probe) or to a supervisor that restarted. Its record ends on its own at its
    deadline, and ``overdue`` still applies. ``relay_unreadable`` (the spool is configured but could not be read) is
    "every record may be live": no pod without a running owner is deleted. Both guards protect the reasons
    ``unrecorded``, ``finished`` and ``stale_record``; ``supervisor_gone`` is a RUNNING job's pod and is decided by
    its supervisor's silence.

    A pod that is already terminating is left alone. An incomplete ``recorded`` yields no action at all except
    ``overdue`` (a pod past its own deadline needs no owner). Anything whose age or owner cannot be judged is counted
    as skipped, never deleted.
    """
    actions, skipped, terminating = [], 0, 0
    listed_sessions = {pod.session for pod in pods}
    live = frozenset(found.session for found in sessions if found.live) if sessions is not None else frozenset()
    for pod in pods:
        if pod.terminating:
            terminating += 1
            continue
        owner = recorded.entries.get(pod.session)
        over_deadline = pod.deadline is not None and now >= pod.deadline + pc.TERMINATION_GRACE_SECONDS + grace
        if over_deadline:
            actions.append(Action("pod", pod.session, OVERDUE, pod.name, pod.uid))
            continue
        if pod.session in hold or not recorded.complete:
            skipped += 1
            continue
        reason = _pod_reason(pod, owner, now, grace, supervisor_gone, relay_unreadable or pod.session in live)
        if reason is None:
            skipped += 1
        else:
            actions.append(Action("pod", pod.session, reason, pod.name, pod.uid))
    if sessions is not None and recorded.complete and len(pods) < LIST_LIMIT:
        for found in sessions:
            name = found.session
            if name in recorded.entries or name in listed_sessions or name in hold:
                continue
            if found.ended and not found.released and now - found.modified >= grace:
                actions.append(Action("session", name, UNRECORDED_SESSION))
    return actions, skipped, terminating


def _pod_reason(pod, owner, now, grace, supervisor_gone, relay_live=False):
    """Why a listed, judged pod is to be deleted, or None to leave it alone (``relay_live``: its session may be in use)."""
    if owner is not None and owner.running:
        return SUPERVISOR_GONE if supervisor_gone(owner.repo, owner.run) else None
    if relay_live:
        return None
    if pod.created is None or now - pod.created < grace:
        # Its age cannot be judged, or it may still be between its launch and its recorded handle, or a session that
        # no supervisor runs (a preflight probe) may be using it: stale or unrecorded, a young pod is left alone.
        return None
    if owner is not None:
        return STALE_RECORD
    return FINISHED if pod.over else UNRECORDED


class Sweeper:
    """The orphan sweep for one launcher and one spool; ``run_once`` is one pass (the operations loop drives it).

    ``list_pods`` and ``delete_pod`` are ``LauncherClient.list_pods`` and ``LauncherClient.delete_pod``; ``spool``
    is the spool root (None: no relay records are looked at). A callable that raises is the launcher being
    unreadable. ``clock`` is epoch seconds.
    """

    def __init__(self, list_pods, delete_pod, spool=None, clock=time.time, gone_after=pc.ORPHAN_SILENCE_SECONDS):
        self._list = list_pods
        self._delete = delete_pod
        self._spool = spool
        self._clock = clock
        self._gone_after = gone_after
        self._gone_since = {}  # (repo, run) -> clock() of the first pass that saw no live supervisor

    # -- the census ------------------------------------------------------------------------------

    def census(self):
        """``{"live": n, "final": m, "unknown": bool}`` of the labelled pods (see ``census``)."""
        return census(self._list)

    # -- one pass --------------------------------------------------------------------------------

    def run_once(self, recorded, supervisor_live):
        """One pass: list the pods, read the owners, decide, act. ``recorded()`` returns ``Recorded`` and
        ``supervisor_live(repo, run)`` says whether a supervisor may be running (an exception counts as live).

        The pods are listed BEFORE the owners are read, so a pod launched and recorded in between is never both
        unlisted and unrecorded. Never raises.
        """
        report = Report()
        try:
            raw = self._list()
        except Exception:  # noqa: BLE001 - whatever the client raised, the launcher cannot be asked
            report.unreadable = True
            return report
        now = self._clock()
        pods = [pod for pod in (listed(item) for item in (raw if isinstance(raw, list) else [])) if pod is not None]
        try:
            owners = recorded()
        except Exception:  # noqa: BLE001 - owners that cannot be read are an incomplete answer
            owners = Recorded({}, False)
        report.incomplete = not owners.complete
        sessions, spool_unreadable = None, False
        if self._spool is not None:
            try:
                sessions = read_sessions(self._spool, now)
            except OSError:
                spool_unreadable = True  # any record may be live: nothing without a running owner is deleted
        gone = self._gone(owners, supervisor_live, now)
        actions, report.skipped, report.terminating = plan(
            pods, owners, sessions, now, gone, protected(), pc.LAUNCH_GRACE_SECONDS, spool_unreadable
        )
        for action in actions:
            self._act(action, report)
        return report

    def _gone(self, owners, supervisor_live, now):
        """A predicate: True for a run whose repository has had no live supervisor for ``gone_after`` seconds."""
        seen = {}

        def live(repo, run):
            try:
                return supervisor_live(repo, run) is not False
            except Exception:  # noqa: BLE001 - unknown is live: nothing is deleted on a guess
                return True

        keys = {(entry.repo, entry.run) for entry in owners.entries.values() if entry.running}
        for key in keys:
            seen[key] = live(*key)
        for key in list(self._gone_since):
            if key not in keys or seen.get(key, True):
                del self._gone_since[key]  # a supervisor is back, or no job of the run records a pod any more
        for key, is_live in seen.items():
            if not is_live:
                self._gone_since.setdefault(key, now)
        return lambda repo, run: (repo, run) in self._gone_since and now - self._gone_since[(repo, run)] >= self._gone_after

    def _act(self, action, report):
        if self._spool is not None:
            podliveness.revoke(self._spool, action.session)  # cut the session off first: a pod still talking gets 410
        if action.kind == "pod":
            try:
                self._delete(action.name, action.uid)
            except Exception:  # noqa: BLE001 - the next pass asks again
                report.failed += 1
                return
            report.deleted[action.reason] = report.deleted.get(action.reason, 0) + 1
            return
        # A session with no owner and no listed pod: the derived pod name deletes a Secret that may have outlived its
        # pod (the launcher deletes the Secret by its derived name even when the pod is gone), then the relay is told
        # it may free the directory.
        try:
            self._delete(pc.pod_name(action.session), None)
        except Exception:  # noqa: BLE001
            report.failed += 1
            return
        try:
            sessionrecord.mark(self._spool, action.session, sessionrecord.MARKER_RELEASED)
        except (sessionrecord.RecordError, OSError):
            report.failed += 1
            return
        report.released += 1


# --- The census -----------------------------------------------------------------------------------


def census(list_pods):
    """``{"live": n, "final": m, "unknown": bool}`` of the labelled worker pods; never raises.

    ``live`` counts every listed pod that is not over: a pod being deleted still runs for its grace, so it counts
    until it is gone from the API (a listing that does not say whether a final pod is terminating counts it too). ``unknown`` is True when the launcher could not be asked or answered something that is not a list
    of pods: the deploy gate treats that as busy, never as zero.
    """
    try:
        raw = list_pods()
    except Exception:  # noqa: BLE001 - the launcher cannot be asked
        return {"live": 0, "final": 0, "unknown": True}
    if not isinstance(raw, list):
        return {"live": 0, "final": 0, "unknown": True}
    pods = [pod for pod in (listed(item) for item in raw) if pod is not None]
    unreadable = len(pods) != len(raw)  # an entry that is not a worker pod: the answer is not what it should be
    live = sum(1 for pod in pods if not pod.over)
    return {"live": live, "final": len(pods) - live, "unknown": unreadable or len(raw) >= LIST_LIMIT}

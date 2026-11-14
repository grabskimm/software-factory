"""The pod handle of a job and the registry of this process's live pod sessions.

Slice 5 of the pod backend (``docs/pod-backend.md``). A session that runs as a pod has no process group the
supervisor can probe or signal, and only the launcher can reach the Kubernetes API, so a job records a ``pod``
handle in its state beside ``pid`` and ``pid_host`` (never instead of them): ``{name, uid, session, launcher,
created_at}``, all words, ids and one timestamp, no secret. A job never has both a pid and a pod.

Three things live here:

* ``PodHandle``: the validated shape of that record. A record that does not validate is ``unknown``, never
  ``dead`` and never a handle to terminate: the pod name must be the contract's name of the session
  (``podcontract.pod_name``), so a state file that was edited cannot point a termination at another pod.
* ``pod_status(handle, reader)``: ``"alive"``, ``"dead"`` or ``"unknown"``, mirroring ``state.pid_status``.
  ``reader`` is injected (the launcher client of slice 11 answers it). **No reader, a reader that raises,
  and any answer other than exactly ``"alive"`` or ``"dead"`` are ``unknown``**: every caller that must not
  start a second session on a worktree refuses on ``unknown`` exactly as it refuses on ``alive``.
* The registry (``register``, ``unregister``, ``kill_live_pods``): the handles of the pod sessions this process
  started and has not released. ``locks.register_kill_hook`` runs ``kill_live_agents`` on a lost lease, which
  signals local process groups only; a pod session would keep running and posting while another supervisor may
  start on the same worktree. The pod backend registers ``kill_live_pods`` as a second hook.

The two callables a caller injects (the tests of this module use fakes; ``LauncherClient.reader`` and
``LauncherClient.terminate`` of slice 11 are the real pair):

* ``reader(handle) -> "alive" | "dead" | "unknown"``: ``dead`` is a launcher answer of NotFound (or a pod with
  another uid) or a terminal phase; a pod that is terminating (deleted, still in the API) is ``alive``; anything it
  cannot answer is ``unknown`` (it may also raise).
* ``terminate(handle) -> None``: asks the launcher to delete the pod; NotFound is success, a failure raises.
  It is idempotent, it must be bounded in time (the supervisor's pool loop waits for it), and it returns normally
  only once the launcher has confirmed the deletion or NotFound: that return is what lets a session's end forget
  the record (``confirm_gone``), so a client that returns for a delete that was merely accepted must wait or raise.

Each can also be installed once for the whole process (``install``), which is how the pod backend wires the
launcher client in so that ``State.recover``, ``status``, ``purge`` and ``recovery`` need no new argument.

Fixed, token-free text only: nothing here prints a pod spec, a token or a launcher message. Standard library only.
"""

import re
import sys
import threading
import time
from dataclasses import dataclass

from . import podcontract as pc

FIELDS = ("name", "uid", "session", "launcher", "created_at")
ALIVE, DEAD, UNKNOWN = "alive", "dead", "unknown"

# Seconds, one deadline for all the pods of an abort (``Runner.abort_workers``): the same order as the kill hooks'.
ABORT_TIMEOUT = 10.0

# A pod uid is a Kubernetes UUID, the launcher a short word; neither may carry anything but these characters.
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
# ``state.timestamp()``: UTC, ISO 8601, a trailing Z.
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z")

_INVALID = "invalid pod handle"
_UNREADABLE = "pod session (unreadable record)"


class PodStillLive(RuntimeError):
    """Recovery found a pod session that is alive or whose state cannot be read: wait, never mark it interrupted.

    ``status`` is ``"alive"`` or ``"unknown"``. A distinct type so that a caller that retries (the restart pass of
    a later slice) can tell this refusal from any other. The text is fixed apart from the job key and the
    session id, which is validated hex.
    """

    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class PodHandle:
    """The pod of one session: name, uid, session id, launcher word and creation time, all validated.

    Validated on construction, so a record that fails validation never becomes an instance. Immutable and
    compared by value. ``name`` must be the contract's pod name of ``session``.
    """

    name: str
    uid: str
    session: str
    launcher: str
    created_at: str

    def __post_init__(self):
        values = (self.name, self.uid, self.session, self.launcher, self.created_at)
        if not all(isinstance(value, str) for value in values):
            raise ValueError(_INVALID)
        if not (
            _TOKEN.fullmatch(self.uid)
            and _TOKEN.fullmatch(self.launcher)
            and _TIMESTAMP.fullmatch(self.created_at)
        ):
            raise ValueError(_INVALID)
        try:
            expected = pc.pod_name(self.session)  # also validates the session id
        except ValueError:
            raise ValueError(_INVALID) from None
        if self.name != expected:
            raise ValueError(_INVALID)

    def record(self):
        """The dict that goes into a job's state (a new one every call)."""
        return {field: getattr(self, field) for field in FIELDS}

    @classmethod
    def from_record(cls, value):
        """A handle from a ``PodHandle`` or from a state record; ValueError (fixed text) for anything else.

        The record's keys are a closed set: a missing, an extra or a non-string field is invalid.
        """
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict) or set(value) != set(FIELDS):
            raise ValueError(_INVALID)
        return cls(**value)


def label(record):
    """How a refusal names a recorded pod: its session id, or a fixed phrase when the record is unreadable."""
    try:
        return f"pod session {PodHandle.from_record(record).session}"
    except ValueError:
        return _UNREADABLE


# --- Injection ---------------------------------------------------------------------------------

_installed = {"reader": None, "terminate": None}
_installed_lock = threading.Lock()


def install(reader=None, terminate=None):
    """Install the process-wide reader and terminator (the pod backend does this); None removes one."""
    with _installed_lock:
        _installed["reader"] = reader
        _installed["terminate"] = terminate


def reset():
    """Remove both installed callables and forget every registered handle (tests, and a manager that stops)."""
    install()
    with _live_lock:
        _live.clear()


def _installed_callable(name):
    with _installed_lock:
        return _installed[name]


# --- Status ------------------------------------------------------------------------------------


def pod_status(handle, reader=None):
    """``"alive"``, ``"dead"`` or ``"unknown"`` for a job's recorded pod; it never raises.

    ``handle`` is the record in a job's state: None (no pod) is ``dead``, as ``pid_status`` reads no pid. A
    record that is not a valid handle (corrupt, hand edited) is ``unknown``: it cannot be verified, so it is not
    called dead, and the reader is not asked about it. ``reader`` defaults to the installed one; without one, a
    reader that raises, or an answer that is not exactly ``"alive"`` or ``"dead"``, the pod is ``unknown``.
    """
    if handle is None:
        return DEAD
    try:
        pod = PodHandle.from_record(handle)
    except ValueError:
        return UNKNOWN
    ask = reader if reader is not None else _installed_callable("reader")
    if not callable(ask):
        return UNKNOWN
    try:
        answer = ask(pod)
    except Exception:  # noqa: BLE001 - a reader that cannot answer is "unknown", whatever it raised
        return UNKNOWN
    if answer == ALIVE:
        return ALIVE
    if answer == DEAD:
        return DEAD
    return UNKNOWN


def still_live(key, record, status, action):
    """The refusal for a pod that is not ``dead``: ``PodStillLive`` with the fixed text of ``action``.

    ``action`` is the verb the caller refuses (``resuming``, ``purging``, ``recovering``); the text names the
    job key and the session id only.
    """
    what = label(record)
    if status == ALIVE:
        text = f"{key}: {what} is still alive; wait or stop it before {action}"
    else:
        text = f"{key}: {what} cannot be verified (the launcher did not answer); not {action} until it can"
    return PodStillLive(text, status)


# --- Termination -------------------------------------------------------------------------------


def _terminate_one(call, handle):
    try:
        call(handle)
    except Exception as error:  # noqa: BLE001 - cleanup runs while another error propagates: report, never raise
        print(
            f"factory: could not terminate pod session {handle.session}: {type(error).__name__}",
            file=sys.stderr,
        )
        return False
    return True


def terminate_pod(handle, terminate=None):
    """Ask for one recorded pod to be deleted; True when the terminator ran, False (reported) otherwise.

    Never raises. A record that is not a valid handle is not passed on (it names no pod we may delete).
    ``terminate`` defaults to the installed terminator; without one nothing can be deleted and that is reported.
    """
    try:
        pod = PodHandle.from_record(handle)
    except ValueError:
        print("factory: could not terminate a pod session: unreadable record", file=sys.stderr)
        return False
    call = terminate if terminate is not None else _installed_callable("terminate")
    if not callable(call):
        print(f"factory: could not terminate pod session {pod.session}: no terminator installed", file=sys.stderr)
        return False
    return _terminate_one(call, pod)


def _join_all(threads, deadline, clock):
    """Wait for ``threads`` until one shared ``deadline`` (None waits for ever), not that long for each of them."""
    for thread in threads:
        thread.join(None if deadline is None else max(0.0, deadline - clock()))


def terminate_all(handles, terminate=None, timeout=None, clock=time.monotonic):
    """Terminate every handle, one thread each, so that a hung call holds up no other; the number that failed.

    A lost lease gives the kill hooks only seconds (``locks.KILL_HOOK_TIMEOUT``), so the pods are not asked one
    after the other. The call RETURNS ONLY WHEN every termination has finished or the deadline has passed: the
    process exits right after the hooks return (``locks.exit_process_on_lost``) and these threads are daemons,
    so a call that did not wait would end with the pods still running. ``timeout`` (None waits for ever) is ONE
    deadline for the whole wait, measured from the start of the call with ``clock`` (``time.monotonic``, injected
    by the tests): with N hung calls the wait is ``timeout``, not N times it. A call that has not finished by
    then counts as failed. ``handles`` are ``PodHandle`` objects.
    """
    pods = list(handles)
    if not pods:
        return 0
    call = terminate if terminate is not None else _installed_callable("terminate")
    if not callable(call):
        print(
            f"factory: could not terminate {len(pods)} pod session(s): no terminator installed",
            file=sys.stderr,
        )
        return len(pods)
    deadline = None if timeout is None else clock() + timeout
    outcomes = {}

    def ask(pod):
        outcomes[pod.session] = _terminate_one(call, pod)

    threads = [
        threading.Thread(target=ask, args=(pod,), name="factory-pod-terminate", daemon=True) for pod in pods
    ]
    for thread in threads:
        thread.start()
    _join_all(threads, deadline, clock)
    return sum(1 for pod in pods if outcomes.get(pod.session) is not True)


def terminate_recorded(jobs, terminate=None, timeout=None, clock=time.monotonic):
    """Terminate the pod recorded in each of ``jobs`` (job state dicts); the number that could not be.

    The lease-loss and abort paths of the runner. A job without a pod is skipped; a recorded pod that is not a
    valid handle cannot be terminated and counts as failed (reported). Termination is idempotent, so a pod that
    is already gone, or one also reached through the registry, costs nothing. ``timeout`` is one deadline for the
    whole wait (``terminate_all``).
    """
    handles, unreadable = [], 0
    for job in jobs:
        record = job.get("pod")
        if record is None:
            continue
        try:
            handles.append(PodHandle.from_record(record))
        except ValueError:
            unreadable += 1
            print("factory: could not terminate a pod session: unreadable record", file=sys.stderr)
    return unreadable + terminate_all(handles, terminate, timeout, clock)


def confirm_gone(handle, reader=None, terminate=None):
    """True only when the recorded pod is known to be gone, so that its record may be forgotten; never raises.

    The reader is asked first: ``dead`` (NotFound, a pod with another uid, a terminal phase) is gone and the
    launcher is asked for nothing more. Otherwise the pod is terminated, and only a terminator that returned
    without raising confirms it (its contract: it returns once the launcher confirmed the deletion or NotFound).
    Anything else is NOT gone and the caller keeps the record, so that ``State.recover``, ``purge``, ``recovery``
    and the next session of the job refuse on it: no terminator or no reader installed, a launcher that does not
    answer, a terminator that raised, a corrupt record. A failed termination is reported by ``terminate_pod``.
    No pod (None) is gone.
    """
    if pod_status(handle, reader) == DEAD:
        return True
    return terminate_pod(handle, terminate)


# --- The registry of this process's live pod sessions -------------------------------------------

_live = set()
_live_lock = threading.Lock()


def register(handle):
    """Remember a started pod session so ``kill_live_pods`` can reach it; ``handle`` is a handle or its record."""
    pod = PodHandle.from_record(handle)
    with _live_lock:
        _live.add(pod)


def unregister(handle):
    """Forget a pod session once it is released; unknown handles are ignored."""
    try:
        pod = PodHandle.from_record(handle)
    except ValueError:
        return
    with _live_lock:
        _live.discard(pod)


def live():
    """The registered handles, in no particular order (a copy)."""
    with _live_lock:
        return list(_live)


def kill_live_pods(terminate=None, timeout=None, clock=time.monotonic):
    """Terminate every registered pod session: the lease-loss hook of the pod backend.

    ``locks.register_kill_hook`` takes a callable without arguments, so the backend registers this function
    (installed terminator) or a ``functools.partial`` of it. The registry is left as it is: the backend
    unregisters a session when it releases it. Returns the number of sessions it could not terminate, and only
    after every termination has finished or ``timeout`` (one deadline for the whole wait) has passed.
    """
    return terminate_all(live(), terminate, timeout, clock)

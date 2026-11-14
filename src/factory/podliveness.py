"""Pod-side liveness: what the supervisor may conclude about a pod session without trusting the pod's text.

Slice 19 of the pod backend (``docs/pod-backend.md``, issue #147). The supervisor needs to tell four situations
apart for a session that runs as a pod: it is working, it has not started, it is silent (alive for Kubernetes,
dead for every practical purpose) and it is over. Three sources can answer, and they are weighed here:

* **The relay's own record** (``sessionrecord``): the one-way token state, the ``last_heartbeat`` the relay wrote
  with ITS clock when a call arrived, the deadline of the record and the two markers. The pod chooses nothing in
  it but whether to call: no pod-supplied number, name or text is read (a heartbeat body is never parsed), so a
  pod cannot make itself look younger, older or finished by what it says.
* **Kubernetes' account of the pod** (``podstatus``, through the launcher's ``status`` answer): present or not,
  terminating or not, one of the closed states. It is the only source that can say a pod is over.
* **The clock**: the supervisor's own, against the record's deadline and the contract's windows
  (``HEARTBEAT_SECONDS``, ``ORPHAN_SILENCE_SECONDS``, ``SCHEDULING_TIMEOUT_SECONDS``, ``PULL_ERROR_SECONDS``,
  ``LAUNCH_GRACE_SECONDS``).

THE RULES (each is pinned by a test that fails when it is removed):

* **A heartbeat is evidence of life, never of death, and never of success.** A fresh heartbeat keeps a verdict at
  ``running``; it does not outrank Kubernetes saying the pod is over, and no verdict here is ever "dead": only the
  launcher (``podhandle.pod_status``) may call a pod dead, so a stale heartbeat can lead to a termination request
  (``enforce``) and never to a record being forgotten.
* **Only the relay can say a session finished.** A pod that Kubernetes reports as ``succeeded`` or ``gone`` with
  no result in the relay is ``lost``: the result a session posted is the only proof it worked. The relay is read
  AFTER Kubernetes, so a result that lands between the two reads is seen, not lost.
* **Fail closed on what cannot be read.** A launcher that does not answer, an answer that is not the closed
  shape, or a spool that cannot be read is ``unreadable``: the caller waits, and nothing is terminated on a guess.
  A session record that is MISSING is a different fact (the spool was replaced, so every call of the pod answers
  410): ``lost``.
* **Silence is measured from the later of the last heartbeat and the pod's creation**, so a pod that never
  heartbeats is silent ``ORPHAN_SILENCE_SECONDS`` after it was created (``never_heard``), and a pod that is still
  waiting for a node or an image is ``starting`` until the scheduling or pull window and the launch grace have
  passed (``never_started``). A heartbeat dated in the future (more than ``FUTURE_SKEW_SECONDS`` ahead of the
  clock) is ignored, so a clock jump cannot keep a verdict ``running`` for ever.
* **The deadline ends every session.** ``now >= deadline`` of the record is ``expired`` whatever the heartbeat says;
  a pod that heartbeats for ever is bounded by it.

``enforce`` is the one thing that acts: for a verdict whose action is ``terminate`` it writes the ``revoke`` marker
FIRST (so the pod's next call answers 410 and a result in flight is refused) and then asks for the pod to be
deleted (``podhandle.terminate_pod``), and it reports whether the deletion was confirmed. A marker that cannot be
written never stops the termination.

Nothing here prints, logs, reads the environment or the network. ``podsweep`` imports it (to read and revoke the
relay's record); the pod backend (slice 12b) is its other caller, and it is dormant until the backend is wired in.
Standard library only.
"""

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from . import podcontract as pc
from . import podhandle, podstatus, sessionrecord

# The closed vocabulary of a verdict.
STARTING = "starting"
RUNNING = "running"
TERMINATING = "terminating"
SILENT = "silent"
EXPIRED = "expired"
REVOKED = "revoked"
FINISHED = "finished"
RELEASED = "released"
LOST = "lost"
UNREADABLE = "unreadable"
STATES = (STARTING, RUNNING, TERMINATING, SILENT, EXPIRED, REVOKED, FINISHED, RELEASED, LOST, UNREADABLE)

# What the caller does about it.
WAIT = "wait"
COLLECT = "collect"
TERMINATE = "terminate"
FAIL = "fail"
NONE = "none"
ACTIONS = (WAIT, COLLECT, TERMINATE, FAIL, NONE)

# The closed vocabulary of the reason that goes with a verdict (a Kubernetes state word is a reason too, the
# ones of ``podstatus.STATES``).
REASONS = tuple(dict.fromkeys((
    "result",  # finished: the relay has the result
    "collected",  # released: the supervisor has collected it
    "heartbeat",  # running: the relay heard from the pod within the silence window
    "not_heard_yet",  # running: Kubernetes says it runs, the relay has not heard from it yet, inside the window
    "waiting_for_node",  # starting: no node, image or Secret yet, inside the window
    "age_unknown",  # starting: the pod's creation time is not known, so no window can be measured
    "never_started",  # silent: still not running after the scheduling or pull window and the launch grace
    "never_heard",  # silent: running, and the relay never heard from it
    "no_heartbeat",  # silent: running, and the relay has not heard from it for the silence window
    "deadline",  # expired
    "revoke",  # revoked
    "launcher_unreadable",  # unreadable: Kubernetes' account of the pod could not be had
    "relay_unreadable",  # unreadable: the spool could not be read
    "no_session_record",  # lost: the relay has no record (the spool was replaced)
    "terminating",  # terminating
    *podstatus.STATES,  # lost: the pod is over and posted no result (``gone``, ``oom``, ``evicted``, ``deadline``, ...)
)))

# A heartbeat further than this ahead of the supervisor's clock cannot be genuine (both are written with a clock
# of the same pod): it is ignored rather than believed.
FUTURE_SKEW_SECONDS = 60

# The pod states that mean "not running yet" (the pod exists and may still start), and the ones whose containers are not.
_NOT_RUNNING = ("pending", "unschedulable", "starting_error")

_ISO = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(\.[0-9]{1,6})?Z")

# How a read of the spool can fail without being a verdict of its own.
_READ_ERRORS = (sessionrecord.RecordError, OSError)


@dataclass(frozen=True)
class Relay:
    """What the relay's files say about one session, as the supervisor read them.

    ``read`` is ``"ok"``, ``"missing"`` (no session directory or record) or ``"unreadable"``; the other fields
    are meaningful only for ``"ok"``.
    """

    read: str
    state: str | None = None
    deadline: int | None = None
    heartbeat: int | None = None
    reject: str | None = None
    revoked: bool = False
    released: bool = False


MISSING = Relay("missing")
UNREAD = Relay("unreadable")


@dataclass(frozen=True)
class Verdict:
    """The supervisor's conclusion about one pod session at one moment.

    ``state`` and ``reason`` are from the closed sets above; ``action`` is what the caller does (``collect``
    only for ``finished``, ``terminate`` only while the pod may still be there); ``silence`` is the seconds since
    the later of the last heartbeat and the creation (None when not measurable); ``reject`` is the relay's fixed
    code for a session it cut off, never a pod's text.
    """

    state: str
    reason: str
    action: str
    silence: int | None = None
    reject: str | None = None


# --- Reading ------------------------------------------------------------------------------------


def read_relay(spool, session):
    """The ``Relay`` of a session read from the spool (a path or its descriptor); never raises.

    A session with no directory or no record is ``MISSING``; a file that cannot be read or that is not valid
    (an I/O error, a closed-schema violation) is ``UNREAD``, which is not the same fact: it never makes a session
    lost.
    """
    try:
        with sessionrecord.session_dir(spool, session) as fd:
            record = sessionrecord.read_at(fd)
            state = sessionrecord.read_state_at(fd)
            return Relay(
                read="ok",
                state=state["state"],
                deadline=record["deadline"],
                heartbeat=sessionrecord.read_heartbeat_at(fd),
                reject=state.get("reject"),
                revoked=sessionrecord.marked_at(fd, sessionrecord.MARKER_REVOKE),
                released=sessionrecord.marked_at(fd, sessionrecord.MARKER_RELEASED),
            )
    except sessionrecord.MissingRecord:
        return MISSING
    except _READ_ERRORS:
        return UNREAD


def epoch_of(created_at):
    """Epoch seconds of a handle's ``created_at`` (``state.timestamp()``), None when it is not that shape."""
    if not isinstance(created_at, str):
        return None
    found = _ISO.fullmatch(created_at)
    if found is None:
        return None
    try:
        year, month, day, hour, minute, second = (int(part) for part in found.groups()[:6])
        return int(datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc).timestamp())
    except (ValueError, OverflowError, OSError):
        return None


def valid_pod(pod):
    """Whether ``pod`` has the closed shape of a launcher ``status`` answer; anything else is unreadable."""
    return (
        isinstance(pod, dict)
        and pod.get("state") in podstatus.STATES
        and isinstance(pod.get("final"), bool)
        and isinstance(pod.get("present"), bool)
        and isinstance(pod.get("terminating"), bool)
    )


# --- Deciding -----------------------------------------------------------------------------------


def _heartbeat(relay, now):
    """The heartbeat in epoch seconds, or None when there is none or it is dated in the future."""
    beat = relay.heartbeat
    if beat is None or beat > now + FUTURE_SKEW_SECONDS:
        return None
    return beat


def _start_window(pod_state):
    """Seconds a pod may stay in a not-running state (and without a heartbeat) before it counts as never started."""
    if pod_state == "starting_error":
        return pc.PULL_ERROR_SECONDS + pc.LAUNCH_GRACE_SECONDS
    return pc.SCHEDULING_TIMEOUT_SECONDS + pc.LAUNCH_GRACE_SECONDS


def _over(pod):
    """Whether Kubernetes says the pod is over: final and not merely being deleted (a terminating pod still runs)."""
    return pod["final"] and not pod["terminating"]


def _ended(state, reason, pod, **fields):
    """A verdict for a session that is over without a result: ``terminate`` while the pod may still be there."""
    action = FAIL if pod is not None and not pod["present"] else TERMINATE
    return Verdict(state, reason, action, **fields)


def assess(relay, pod, now, created=None):
    """The ``Verdict`` for one session from what the relay says, what Kubernetes says and the clock; it never raises.

    ``relay`` is a ``Relay``; ``pod`` is the launcher's ``status`` answer (a dict, see ``valid_pod``) or None
    when it could not be had; ``now`` is epoch seconds; ``created`` is the pod's creation in epoch seconds (the
    handle's ``created_at``, see ``epoch_of``) or None.
    """
    if pod is not None and not valid_pod(pod):
        pod = None
    if not isinstance(relay, Relay):
        relay = UNREAD
    if relay.read == "ok":
        if relay.released:
            return Verdict(RELEASED, "collected", NONE)
        if relay.state == "finished":
            return Verdict(FINISHED, "result", COLLECT, reject=relay.reject)
        if relay.revoked or relay.state == "revoked":
            return _ended(REVOKED, "revoke", pod)
        if relay.state == "expired" or (relay.deadline is not None and now >= relay.deadline):
            return _ended(EXPIRED, "deadline", pod)
    if pod is None:
        # Kubernetes could not be asked. Nothing is concluded: the launcher is probably down, and so is the means to act.
        return Verdict(UNREADABLE, "launcher_unreadable", WAIT)
    if relay.read == "missing":
        # The supervisor writes the record before the launch, so a missing one means the spool was replaced: every call
        # of the pod answers 410 and nothing it does can reach the supervisor.
        return _ended(LOST, "no_session_record", pod)
    if relay.read != "ok":
        return Verdict(UNREADABLE, "relay_unreadable", WAIT)
    if _over(pod):
        # Over for Kubernetes and no result in the relay (read after Kubernetes, see ``observe``): it never worked.
        return Verdict(LOST, pod["state"], FAIL if not pod["present"] else TERMINATE)
    if pod["terminating"]:
        return Verdict(TERMINATING, "terminating", WAIT)
    return _alive(relay, pod, now, created)


def _alive(relay, pod, now, created):
    """The verdict for a session whose pod Kubernetes has not ended and whose relay session is not over."""
    beat = _heartbeat(relay, now)
    anchors = [value for value in (beat, created) if value is not None]
    if not anchors:
        return Verdict(STARTING, "age_unknown", WAIT)
    silence = max(0, now - max(anchors))
    if pod["state"] in _NOT_RUNNING and beat is None:
        if silence >= _start_window(pod["state"]):
            return Verdict(SILENT, "never_started", TERMINATE, silence=silence)
        return Verdict(STARTING, "waiting_for_node", WAIT, silence=silence)
    if silence >= pc.ORPHAN_SILENCE_SECONDS:
        return Verdict(SILENT, "never_heard" if beat is None else "no_heartbeat", TERMINATE, silence=silence)
    return Verdict(RUNNING, "heartbeat" if beat is not None else "not_heard_yet", WAIT, silence=silence)


# --- Observing ----------------------------------------------------------------------------------


def observe(handle, spool, pod_state, now=None):
    """Read Kubernetes' account and then the relay for a recorded pod, and return the ``Verdict``; never raises.

    ``handle`` is the job's ``pod`` record (``podhandle.PodHandle`` or its dict); a record that does not validate
    is ``unreadable``. ``pod_state(name, uid)`` is ``LauncherClient.pod_state`` (anything it raises, and any
    answer that is not the closed shape, is Kubernetes being unreadable). ``spool`` is the spool root. Kubernetes is
    read FIRST: a pod that finished and posted its result is over for Kubernetes only after the relay has the result,
    so reading the relay second cannot call such a session lost.
    """
    now = time.time() if now is None else now
    try:
        pod = podhandle.PodHandle.from_record(handle)
    except ValueError:
        return Verdict(UNREADABLE, "launcher_unreadable", WAIT)
    try:
        answer = pod_state(pod.name, pod.uid)
    except Exception:  # noqa: BLE001 - whatever the client raised, Kubernetes could not be asked
        answer = None
    return assess(read_relay(spool, pod.session), answer, now, epoch_of(pod.created_at))


class Probe:
    """``observe`` bound to one spool and one launcher client (``Probe(spool, client.pod_state).assess(handle)``)."""

    def __init__(self, spool, pod_state, clock=time.time):
        self._spool = spool
        self._pod_state = pod_state
        self._clock = clock

    def assess(self, handle):
        return observe(handle, self._spool, self._pod_state, self._clock())


# --- Acting -------------------------------------------------------------------------------------


def revoke(spool, session):
    """Write the ``revoke`` marker of a session; True when it is there afterwards, False (never raising) otherwise."""
    try:
        sessionrecord.mark(spool, session, sessionrecord.MARKER_REVOKE)
    except _READ_ERRORS:
        return False
    return True


def enforce(verdict, handle, spool, terminate=None):
    """Carry out ``verdict.action`` for a recorded pod; True when nothing is left to do, False when the pod may remain.

    For ``terminate``: the ``revoke`` marker is written first (a pod that is still talking is cut off at once, and a
    result in flight is refused), then the pod is asked to go (``podhandle.terminate_pod``: ``terminate`` or the
    installed terminator, which returns only once the pod is gone), and the answer is whether that was confirmed. A
    marker that cannot be written does not stop the termination. For any other action nothing is done and the answer
    is whether the verdict leaves nothing to do (``collect``, ``fail`` and ``none`` do not touch the pod; ``wait``
    leaves the pod to run, so False). Never raises.
    """
    if verdict.action != TERMINATE:
        return verdict.action in (COLLECT, FAIL, NONE)
    try:
        pod = podhandle.PodHandle.from_record(handle)
    except ValueError:
        return False
    revoke(spool, pod.session)
    return podhandle.terminate_pod(pod, terminate)

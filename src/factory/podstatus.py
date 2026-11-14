"""Classify the state of a worker pod into one closed outcome (slice 7 of the pod backend, issue #145).

``classify(pod)`` reads a pod as the Kubernetes API returns it (a dict) and answers one of the
closed set ``STATES``:

``pending``, ``unschedulable``, ``starting_error``, ``running``, ``succeeded``, ``failed_exit``,
``oom``, ``evicted``, ``deadline``, ``gone``.

It is TABLE-DRIVEN: ``ROWS`` is the table, the first row that matches wins, so the order is part of
the table. Every row names the Kubernetes fields it reads (``reads``) and carries the evidence
marker ``ASSUMED``: the shapes below are the documented Kubernetes behaviour as the author knows
it, checked only against the in-memory fake of ``tests/fake_kube.py``. No cluster was consulted. A
recording of each failure mode from a real cluster replaces the marker row by row (slice 22); until
then every row is UNVERIFIED, and the exact Pending reason at a NodePool limit and the shape of a
Karpenter-initiated eviction are the two that are known to be open.

What it is not. The classifier is stateless and has no clock: the timers that turn ``pending``,
``unschedulable`` or a pull error into a failure (the scheduling timeout, the pull and config error
window, the orphan silence) belong to the caller. It never reads events or logs, only the pod. A
pod that cannot be read (not a dict, no status, an unknown phase) is ``pending`` with the detail
``unrecognised``: never ``succeeded`` and never ``running``. A ``NotFound`` answer after the pod was
created is ``gone`` (``classify(None)``), never ``succeeded``: a pod that finished and was deleted, a
node that was deleted and an eviction that completed all look the same from the outside, and only
the result the session posted to the relay can show that a session succeeded.

No free text is ever copied. The ``detail`` of an outcome is a word of this table (a Kubernetes
reason that the table lists, or a fixed word), the exit code is an integer 0 to 255, and every
other string of the pod (messages, names, node names, reasons the table does not list) is ignored.
"""

from collections import namedtuple
from dataclasses import dataclass

PENDING = "pending"
UNSCHEDULABLE = "unschedulable"
STARTING_ERROR = "starting_error"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED_EXIT = "failed_exit"
OOM = "oom"
EVICTED = "evicted"
DEADLINE = "deadline"
GONE = "gone"
STATES = (PENDING, UNSCHEDULABLE, STARTING_ERROR, RUNNING, SUCCEEDED, FAILED_EXIT, OOM, EVICTED, DEADLINE, GONE)

ASSUMED = "ASSUMED"

# Kubernetes words the table matches. Each is a documented value of the field named in the row that uses it.
NOT_FOUND = "response: 404 NotFound on get pod"
EVICTION_REASONS = ("Evicted", "Terminated", "NodeShutdown", "Shutdown")
START_ERROR_REASONS = (
    "ImagePullBackOff",
    "ErrImagePull",
    "ErrImageNeverPull",
    "InvalidImageName",
    "CreateContainerConfigError",
    "CreateContainerError",
    "RunContainerError",
    "CrashLoopBackOff",
)
MAX_ITEMS = 16  # container statuses and conditions that are looked at
MAX_EXIT_CODE = 255


class View:
    """A read-only window on a pod that remembers which dotted paths were read (a test checks the `reads` column).

    `value("status.phase")` walks dicts only and answers None for anything missing or not a dict on
    the way. `each("status.containerStatuses")` answers a View per dict element of a list (at most
    MAX_ITEMS), whose paths are recorded as `status.containerStatuses[].…`.
    """

    def __init__(self, pod, prefix="", touched=None):
        self._pod = pod
        self._prefix = prefix
        self.touched = set() if touched is None else touched

    def value(self, path):
        self.touched.add(f"{self._prefix}{path}")
        found = self._pod
        for step in path.split("."):
            if not isinstance(found, dict):
                return None
            found = found.get(step)
        return found

    def each(self, path):
        self.touched.add(f"{self._prefix}{path}[]")
        found = self._pod
        for step in path.split("."):
            if not isinstance(found, dict):
                return []
            found = found.get(step)
        if not isinstance(found, list):
            return []
        return [View(item, f"{self._prefix}{path}[].", self.touched) for item in found[:MAX_ITEMS] if isinstance(item, dict)]


@dataclass(frozen=True)
class Outcome:
    """What the pod is doing. `final` is True when the state cannot change any more (the pod is over or going away)."""

    state: str
    detail: str
    row: str
    final: bool
    exit_code: int | None = None


def _exit_code(value):
    # A bool is not a number here (json true would otherwise read as exit code 1).
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_EXIT_CODE:
        return None
    return value


def _phase(view):
    return view.value("status.phase")


def _terminated(view):
    """The container statuses (as Views) whose state is terminated."""
    return [c for c in view.each("status.containerStatuses") if isinstance(c.value("state.terminated"), dict)]


def _waiting_reasons(view):
    return [c.value("state.waiting.reason") for c in view.each("status.containerStatuses")]


# Each matcher answers (detail, exit_code) when the row applies, else None.


def _succeeded(view):
    return ("completed", None) if _phase(view) == "Succeeded" else None


def _eviction_reason(view):
    reason = view.value("status.reason")
    return (reason, None) if _phase(view) == "Failed" and reason in EVICTION_REASONS else None


def _deadline_reason(view):
    return ("DeadlineExceeded", None) if view.value("status.reason") == "DeadlineExceeded" else None


def _disruption_condition(view):
    for condition in view.each("status.conditions"):
        if condition.value("type") == "DisruptionTarget" and condition.value("status") == "True":
            return ("DisruptionTarget", None)
    return None


def _oom_killed(view):
    for container in _terminated(view):
        if container.value("state.terminated.reason") == "OOMKilled":
            return ("OOMKilled", _exit_code(container.value("state.terminated.exitCode")))
    return None


def _failed_after_start(view):
    if _phase(view) != "Failed":
        return None
    terminated = _terminated(view)
    if not terminated:
        return None
    return ("exit", _exit_code(terminated[0].value("state.terminated.exitCode")))


def _failed_never_started(view):
    return ("never_started", None) if _phase(view) == "Failed" else None


def _terminating(view):
    deletion = view.value("metadata.deletionTimestamp")
    return ("terminating", None) if isinstance(deletion, str) and deletion else None


def _start_error(view):
    if _phase(view) not in ("Pending", "Running"):
        return None
    for reason in _waiting_reasons(view):
        if reason in START_ERROR_REASONS:
            return (reason, None)
    return None


def _unschedulable(view):
    if _phase(view) != "Pending":
        return None
    for condition in view.each("status.conditions"):
        scheduled = condition.value("type") == "PodScheduled" and condition.value("status") == "False"
        if scheduled and condition.value("reason") in ("Unschedulable", "SchedulingGated"):
            return ("Unschedulable", None)
    return None


def _running(view):
    return ("running", None) if _phase(view) == "Running" else None


def _pending(view):
    return ("waiting", None) if _phase(view) == "Pending" else None


# The table. (id, state, final, reads, evidence, matcher). The first matching row wins.
Row = namedtuple("Row", ("id", "state", "final", "reads", "evidence", "match"))
ROWS = (
    Row("succeeded", SUCCEEDED, True, ("status.phase",), ASSUMED, _succeeded),
    Row("eviction-reason", EVICTED, True, ("status.phase", "status.reason"), ASSUMED, _eviction_reason),
    Row("deadline-reason", DEADLINE, True, ("status.reason",), ASSUMED, _deadline_reason),
    Row(
        "disruption-condition",
        EVICTED,
        True,
        ("status.conditions[]", "status.conditions[].type", "status.conditions[].status"),
        ASSUMED,
        _disruption_condition,
    ),
    Row(
        "oom-killed",
        OOM,
        True,
        (
            "status.containerStatuses[]",
            "status.containerStatuses[].state.terminated",
            "status.containerStatuses[].state.terminated.reason",
            "status.containerStatuses[].state.terminated.exitCode",
        ),
        ASSUMED,
        _oom_killed,
    ),
    Row(
        "failed-exit",
        FAILED_EXIT,
        True,
        (
            "status.phase",
            "status.containerStatuses[]",
            "status.containerStatuses[].state.terminated",
            "status.containerStatuses[].state.terminated.exitCode",
        ),
        ASSUMED,
        _failed_after_start,
    ),
    Row("failed-never-started", STARTING_ERROR, True, ("status.phase",), ASSUMED, _failed_never_started),
    Row("terminating", EVICTED, True, ("metadata.deletionTimestamp",), ASSUMED, _terminating),
    Row(
        "start-error",
        STARTING_ERROR,
        False,
        ("status.phase", "status.containerStatuses[]", "status.containerStatuses[].state.waiting.reason"),
        ASSUMED,
        _start_error,
    ),
    Row(
        "unschedulable",
        UNSCHEDULABLE,
        False,
        (
            "status.phase",
            "status.conditions[]",
            "status.conditions[].type",
            "status.conditions[].status",
            "status.conditions[].reason",
        ),
        ASSUMED,
        _unschedulable,
    ),
    Row("running", RUNNING, False, ("status.phase",), ASSUMED, _running),
    Row("pending", PENDING, False, ("status.phase",), ASSUMED, _pending),
)
GONE_ROW = Row("gone", GONE, True, (NOT_FOUND,), ASSUMED, None)
UNRECOGNISED_ROW = Row("unrecognised", PENDING, False, ("status.phase",), ASSUMED, None)
# Every row in reading order: `gone` answers a NotFound (no pod to read), the table follows, and
# `unrecognised` is the fallback. The two rows at the ends have no matcher.
TABLE = (GONE_ROW, *ROWS, UNRECOGNISED_ROW)


def classify(pod):
    """The `Outcome` of a pod; `None` (the get answered NotFound after the pod was created) is `gone`.

    Never raises: anything that is not a pod dict, or whose phase the table does not know, is
    `pending` with the detail `unrecognised`.
    """
    if pod is None:
        return Outcome(GONE_ROW.state, "not_found", GONE_ROW.id, GONE_ROW.final)
    if isinstance(pod, dict):
        view = View(pod)
        for row in ROWS:
            found = row.match(view)
            if found is not None:
                detail, exit_code = found
                return Outcome(row.state, detail, row.id, row.final, exit_code)
    return Outcome(UNRECOGNISED_ROW.state, "unrecognised", UNRECOGNISED_ROW.id, UNRECOGNISED_ROW.final)

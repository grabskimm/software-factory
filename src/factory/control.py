"""Invocation-scoped graceful drain and single-job stop requests; never signal a PID or edit job state.

A stop request names one job of the run. It is only recorded here, in the control record of the
supervisor that owns the run; that supervisor alone takes it (``RunControl.stop_requests``), lets the
job's current step finish within a grace period, and then stops the job (``Runner.service_stops``).
"""

import fcntl
import json
import math
import os
import re
import secrets
from contextlib import contextmanager
from pathlib import Path

from .state import atomic_json, timestamp

JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
# What an operator may type as the reason of a stop, and the identity recorded with it.
MAX_STOP_REASON = 200
MAX_STOP_PRINCIPAL = 128
MAX_STOP_REQUESTS = 256
# How long a stopping job's worker may finish its current step before its process group is terminated.
ENV_STOP_GRACE = "FACTORY_STOP_GRACE_SECONDS"
DEFAULT_STOP_GRACE = 120.0
MIN_STOP_GRACE = 1.0
MAX_STOP_GRACE = 3600.0


def stop_grace_seconds(env=None):
    """The grace period of a stop: ``FACTORY_STOP_GRACE_SECONDS`` within 1 to 3600, else the default (120).

    An unusable value means the default, never an error: bookkeeping must not fail a run.
    """
    text = str((os.environ if env is None else env).get(ENV_STOP_GRACE) or "").strip()
    try:
        value = float(text)
    except ValueError:
        return DEFAULT_STOP_GRACE
    if not math.isfinite(value) or not MIN_STOP_GRACE <= value <= MAX_STOP_GRACE:
        return DEFAULT_STOP_GRACE
    return value


def clean_text(value, limit):
    """Operator-typed text as it is stored: control, format and separator characters (newlines, escapes, bidi
    overrides) become spaces, runs of spaces collapse, at most ``limit`` characters. None when nothing is left.

    The text is data: it is never put in a log line, a label or a command line of the supervisor.
    """
    if not isinstance(value, str):
        return None
    text = "".join(char if char.isprintable() else " " for char in value[: limit * 4])
    text = " ".join(text.split())[:limit].strip()
    return text or None


@contextmanager
def _mutex(directory):
    with (directory / "supervisor-control.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _read(directory):
    path = directory / "supervisor-control.json"
    if not path.exists():
        raise ValueError("no drain-capable supervisor recorded for this run")
    try:
        document = json.loads(path.read_text())
    except json.JSONDecodeError:
        raise ValueError(
            "invalid supervisor control record; inspect before recovery"
        ) from None
    if (
        not isinstance(document, dict)
        or document.get("version") != 1
        or not isinstance(document.get("current"), dict)
        or not isinstance(document.get("history"), list)
    ):
        raise ValueError("invalid supervisor control record; inspect before recovery")
    return document


def _owner_alive(directory, record):
    pid = record.get("pid")
    if type(pid) is not int or pid <= 0:
        return False
    try:
        with (directory / "supervisor-owner.lock").open("r") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # The ownership lock prevents a reused PID from validating a stale file.
                os.kill(pid, 0)
                return True
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return False
    return False


@contextmanager
def _live_invocation(directory, common):
    """The control document and its current invocation, under the mutex, once proven live for this run.

    Raises ValueError for a run without a drain-capable supervisor, for an invocation that changed between the
    first read and the lock, for a record of another repository or run, and for a supervisor that is not active.
    """
    directory, common = Path(directory).resolve(), Path(common).resolve()
    before = _read(directory)["current"]
    with _mutex(directory):
        document = _read(directory)
        current = document["current"]
        if not before.get("token") or current.get("token") != before["token"]:
            raise ValueError(
                "supervisor invocation changed; inspect status and request drain again"
            )
        if current.get("run_dir") != str(directory) or current.get("common_dir") != str(
            common
        ):
            raise ValueError("supervisor record belongs to another repository or run")
        if current.get("status") not in {"running", "draining"} or not _owner_alive(
            directory, current
        ):
            raise ValueError(
                "recorded supervisor is not active; inspect checkpoints before recovery"
            )
        yield directory, document, current


def request_drain(directory, common):
    """Bypass the repository lock only to request drain of one live invocation."""
    with _live_invocation(directory, common) as (directory, document, current):
        if not current.get("drain_requested_at"):
            current["drain_requested_at"] = timestamp()
            atomic_json(directory / "supervisor-control.json", document)
        return dict(current)


def request_stop(directory, common, job, principal=None, reason=None):
    """Ask the live supervisor of the run to stop one job (the caller has checked that it is running).

    Returns the request: ``requested_at``, ``by`` (the principal, clipped), ``reason`` (sanitised, at most
    ``MAX_STOP_REASON`` characters, or None) and ``created`` (False when the job already had a request in this
    invocation: asking again changes nothing and gets the first answer). The record is data for the supervisor;
    nothing here signals a process or edits a job.
    """
    if not isinstance(job, str) or not JOB_ID.fullmatch(job):
        raise ValueError("invalid job id")
    with _live_invocation(directory, common) as (directory, document, current):
        requests = current.get("stop_requests")
        if not isinstance(requests, dict):
            requests = current["stop_requests"] = {}
        existing = requests.get(job)
        if isinstance(existing, dict):
            return {
                "job": job,
                "requested_at": existing.get("requested_at"),
                "by": existing.get("by"),
                "reason": existing.get("reason"),
                "created": False,
            }
        if len(requests) >= MAX_STOP_REQUESTS:
            raise ValueError("too many stop requests in this supervisor invocation")
        request = {
            "requested_at": timestamp(),
            "by": clean_text(principal, MAX_STOP_PRINCIPAL) or "unknown",
            "reason": clean_text(reason, MAX_STOP_REASON),
        }
        requests[job] = request
        atomic_json(directory / "supervisor-control.json", document)
        return {"job": job, **request, "created": True}


def supervisor_status(directory):
    """Read-only status; old runs without control records remain supported."""
    directory = Path(directory)
    if not (directory / "supervisor-control.json").exists():
        return None
    current = dict(_read(directory)["current"])
    current["active"] = _owner_alive(directory, current)
    if current.get("status") in {"running", "draining"} and not current["active"]:
        current["status"] = "stale"
    return current


class RunControl:
    """Caller owns the repository lock; admission and drain use a separate short lock."""

    def __init__(self, directory, common):
        self.directory = Path(directory).resolve()
        self.common = Path(common).resolve()
        self.token = secrets.token_hex(16)
        self.owner = None
        self.drained = False

    def __enter__(self):
        with _mutex(self.directory):
            self.owner = (self.directory / "supervisor-owner.lock").open("a")
            try:
                try:
                    fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise RuntimeError(
                        "another supervisor owns this run's control record"
                    ) from None
                previous = (
                    _read(self.directory)
                    if (self.directory / "supervisor-control.json").exists()
                    else None
                )
                if previous and (
                    previous["current"].get("common_dir") != str(self.common)
                    or previous["current"].get("run_dir") != str(self.directory)
                ):
                    raise ValueError(
                        "supervisor record belongs to another repository or run"
                    )
                history = (
                    previous["history"] + [previous["current"]] if previous else []
                )
                # Explicit `run` starts a fresh invocation and archives prior drain intent.
                atomic_json(
                    self.directory / "supervisor-control.json",
                    {
                        "version": 1,
                        "history": history,
                        "current": {
                            "token": self.token,
                            "pid": os.getpid(),
                            "common_dir": str(self.common),
                            "run_dir": str(self.directory),
                            "started_at": timestamp(),
                            "status": "running",
                            "drain_requested_at": None,
                        },
                    },
                )
            except BaseException:
                self.owner.close()
                self.owner = None
                raise
        return self

    def _current(self):
        document = _read(self.directory)
        if document["current"].get("token") != self.token:
            raise RuntimeError("supervisor invocation changed unexpectedly")
        return document

    @contextmanager
    def admission(self):
        """Hold until phase submission so no phase is admitted after an accepted request."""
        with _mutex(self.directory):
            document = self._current()
            if document["current"].get("drain_requested_at"):
                self.drained = True
                if document["current"]["status"] != "draining":
                    document["current"]["status"] = "draining"
                    atomic_json(self.directory / "supervisor-control.json", document)
            yield not self.drained

    def draining(self):
        with self.admission() as allowed:
            return not allowed

    def stop_requests(self):
        """``[(job, request)]``: the stop requests this invocation has not looked at yet, oldest first."""
        with _mutex(self.directory):
            requests = self._current()["current"].get("stop_requests")
        return [
            (job, dict(request))
            for job, request in (requests.items() if isinstance(requests, dict) else ())
            if isinstance(request, dict) and not request.get("seen_at")
        ]

    def record_stop(self, job, result):
        """Mark a stop request as taken and say what became of it (``stopping``, ``stopped``, ``finished``...)."""
        with _mutex(self.directory):
            document = self._current()
            requests = document["current"].get("stop_requests")
            request = requests.get(job) if isinstance(requests, dict) else None
            if isinstance(request, dict):
                request["seen_at"] = request.get("seen_at") or timestamp()
                request["result"] = result
                atomic_json(self.directory / "supervisor-control.json", document)

    def __exit__(self, exc_type, exc, traceback):
        try:
            with _mutex(self.directory):
                document = self._current()
                current = document["current"]
                # Include a request accepted immediately before normal shutdown.
                self.drained = bool(current.get("drain_requested_at"))
                current["status"] = (
                    "interrupted"
                    if exc_type
                    else ("drained" if self.drained else "stopped")
                )
                current["finished_at"] = timestamp()
                atomic_json(self.directory / "supervisor-control.json", document)
        finally:
            self.owner.close()

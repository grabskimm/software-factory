"""One supervisor, durable state, and explicit interrupted-job recovery."""

import fcntl
import json
import os
import signal
import socket
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import podhandle


def timestamp():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def elapsed(start, end):
    if not start:
        return None
    try:
        a = datetime.fromisoformat(start.replace("Z", "+00:00"))
        b = datetime.fromisoformat(end.replace("Z", "+00:00"))
        return max(0, (b - a).total_seconds())
    except (TypeError, ValueError):
        return None


def atomic_text(path, text):
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    with temp.open("w") as handle:
        json.dump(value, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


@contextmanager
def lock(path):
    with Path(path).open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(
                "another factory supervisor holds the repository lock"
            ) from None
        yield


def alive(pid):
    if not pid:
        return False
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def kill_group(pgid, sig=signal.SIGKILL):
    """Signal a process group; False when it is already gone, other errors propagate."""
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return False
    return True


BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"


def boot_id():
    """Kernel boot id, so a pid from before a reboot is not mistaken for a local one."""
    try:
        text = Path(BOOT_ID_PATH).read_text().strip()
    except (OSError, ValueError):
        return "unknown"
    return text[:64] or "unknown"


def host_id():
    """'<hostname>/<boot_id>': which kernel and container a recorded pid belongs to.

    A function, not only a constant, so tests (and a future replica manager) can
    override it; every caller must use this, never HOST_ID, for comparisons.
    """
    return f"{socket.gethostname()}/{boot_id()}"


HOST_ID = host_id()


def pid_status(pid, pid_host=None):
    """'alive', 'dead' or 'unknown' for a recorded child process group.

    A pid is only meaningful on the host that recorded it. Another replica's pid
    says nothing about that process here (the number may be unused, or belong to
    an unrelated process), so it is 'unknown', never 'dead'. A missing pid_host
    (state written before this field existed) is read as this host.
    """
    if not pid:
        return "dead"
    if pid_host is not None and pid_host != host_id():
        return "unknown"
    if type(pid) is not int or pid < 0:
        return "unknown"  # corrupt record: cannot verify, so do not call it dead
    try:
        return "alive" if alive(pid) else "dead"
    except (OSError, OverflowError):
        return "unknown"


class State:
    def __init__(self, directory, plan):
        self.path = directory / "state.json"
        self.plan = plan
        self.mutex = threading.RLock()
        # Optional ``observer(key, previous, current)``, called after each update once the
        # state is saved and the mutex released (issue status labels hang off it). It is
        # best-effort: whatever it raises never reaches the caller of ``update``.
        self.observer = None
        # Optional ``ledger(key, previous, current)``: the history ledger (history.py) hangs off it.
        # Same contract as ``observer``, and it runs first: it is a local append that must not wait
        # for the label calls, nor be lost if the process is stopped during them.
        self.ledger = None
        now = timestamp()
        self.data = (
            json.loads(self.path.read_text())
            if self.path.exists()
            else {
                "version": 1,
                "jobs": {job["id"]: self.new_job(now) for job in plan["jobs"]},
            }
        )
        # A crash between writing an ingested plan fragment's jobs into the plan and into
        # the state leaves jobs here that only the plan has; they start queued, as ingestion
        # would have made them.
        for job in plan["jobs"]:
            if job["id"] not in self.data["jobs"] and job.get("fragment"):
                self.data["jobs"][job["id"]] = self.new_job(now)
        if set(self.data["jobs"]) != {job["id"] for job in plan["jobs"]}:
            raise ValueError("state and plan jobs differ; create a new run directory")
        self.save()

    @staticmethod
    def new_job(now):
        return {
            "status": "queued",
            "attempt": 0,
            "created_at": now,
            "updated_at": now,
            "status_started_at": now,
        }

    def add_jobs(self, jobs):
        """Queue jobs appended to the plan (plan fragments); existing jobs are never touched."""
        with self.mutex:
            now = timestamp()
            added = [job["id"] for job in jobs if job["id"] not in self.data["jobs"]]
            for key in added:
                self.data["jobs"][key] = self.new_job(now)
            try:
                self.save()
            except BaseException:
                # The live state must not hold jobs the plan in memory does not know yet:
                # the scheduler indexes both by the same ids. The retry adds them again.
                for key in added:
                    del self.data["jobs"][key]
                raise

    def save(self):
        with self.mutex:
            atomic_json(self.path, self.data)

    def update(self, key, **values):
        self._write(key, values)

    def _write(self, key, values, refuse=None):
        """``update``, which can refuse: ``refuse(job)`` returns the text of a ``RuntimeError``, or None to write.

        The refusal check and the write are one hold of the mutex, so two callers racing on one job cannot both
        pass the check. The observers run after it, outside the mutex.
        """
        with self.mutex:
            reason = refuse(self.data["jobs"][key]) if refuse is not None else None
            if reason is not None:
                raise RuntimeError(reason)
            previous, current = self._update(key, values)
        for hook in (self.ledger, self.observer):
            if hook is not None:
                try:
                    hook(key, previous, current)
                except Exception:  # noqa: BLE001, S110
                    pass

    def _update(self, key, values):
        with self.mutex:
            job = self.data["jobs"][key]
            now = timestamp()
            previous = dict(job)
            job.update(values)
            job.setdefault("created_at", now)
            job["updated_at"] = now
            if previous.get("status") != job.get("status"):
                job["status_started_at"] = now
            old_phase = previous.get("worker_phase") or previous.get("phase")
            new_phase = job.get("worker_phase") or job.get("phase")
            old_active = previous.get("status") == "running"
            new_active = job.get("status") == "running"
            transition = (
                old_phase != new_phase
                or previous.get("status") != job.get("status")
                or old_active != new_active
                or previous.get("attempt") != job.get("attempt")
            )
            if transition:
                duration = elapsed(previous.get("phase_started_at"), now)
                if old_active and old_phase and duration is not None:
                    totals = job.setdefault("phase_seconds", {})
                    totals[old_phase] = totals.get(old_phase, 0) + duration
                event = {
                    "at": now,
                    "status": job.get("status"),
                    "phase": new_phase,
                    "attempt": job.get("attempt", 0),
                    "previous_phase": old_phase,
                    "previous_phase_seconds": duration if old_active else None,
                }
                job.setdefault("phase_events", []).append(event)
                job["phase_started_at"] = now if new_active else None
            elif new_active:
                # Old v1 runs have no clock; start observing now, never invent age.
                job.setdefault("phase_started_at", now)
            self.save()
            return previous, dict(job)

    def record_pid(self, key, pid):
        """Record a child process group together with the host that owns it.

        A job never has both a pid and a pod: while a pod is recorded this refuses, because forgetting a pod
        that may still run would let a second session start on the same worktree. The check and the write are
        one critical section (``_write``), so a racing ``record_pod`` cannot leave both set.
        """
        if pid is None:
            self.clear_pid(key)
            return

        def refuse(job):
            if job.get("pod") is not None:
                return f"{key}: a pod session is recorded; a job has a pid or a pod, never both"
            return None

        self._write(key, {"pid": pid, "pid_host": host_id()}, refuse)

    def clear_pid(self, key):
        self.update(key, pid=None, pid_host=None)

    def record_pod(self, key, handle):
        """Record the pod of the job's running session (``podhandle.PodHandle`` or its record).

        ``pid`` and ``pid_host`` keep their meaning and are never replaced: while a process group is recorded
        this refuses (a job never has both), and so does a second, different pod while one is recorded. A handle
        that does not validate raises ``ValueError`` before anything is written. The checks and the write are
        one critical section (``_write``), so a racing ``record_pid`` cannot leave both set.
        """
        record = podhandle.PodHandle.from_record(handle).record()

        def refuse(job):
            if job.get("pid"):
                return f"{key}: a process group is recorded; a job has a pid or a pod, never both"
            current = job.get("pod")
            if current is not None and current != record:
                return f"{key}: another pod session is already recorded"
            return None

        self._write(key, {"pod": record}, refuse)

    def clear_pod(self, key):
        """Forget the job's pod. A job that has none is left untouched (no write, so old state files stay as they were)."""
        with self.mutex:
            recorded = self.data["jobs"][key].get("pod")
        if recorded is not None:
            self.update(key, pod=None)

    def record_started(self, key, started):
        """Record what a backend's ``on_start`` handed over: a process group id, or a pod handle.

        An int (or None, which clears the pid) goes through ``record_pid`` exactly as before; a
        ``podhandle.PodHandle`` or its record goes through ``record_pod``.
        """
        if isinstance(started, (podhandle.PodHandle, dict)):
            self.record_pod(key, started)
        else:
            self.record_pid(key, started)

    def end_session(self, key):
        """The end of a session: forget its process group, and its pod only once the pod is known to be gone.

        The pod record is forgotten only when the reader reads the pod as dead or the terminator confirmed its
        deletion (``podhandle.confirm_gone``). Otherwise (the launcher is down, the terminator raised or is not
        installed) it STAYS: ``recover``, ``purge``, ``recovery`` and the next session of the job then refuse on
        it, so a pod that may still run never lets a second session start on its worktree. A job with no pod
        is touched exactly as ``clear_pid`` always did. The launcher is asked outside the state mutex.
        """
        self.clear_pid(key)
        pod = self.data["jobs"][key].get("pod")
        if pod is not None and podhandle.confirm_gone(pod):
            self.clear_pod(key)

    def recover(self, holds_lease=None, pod_reader=None):
        """Mark running jobs interrupted, but only when no child can still be alive.

        A pid recorded on another host cannot be probed from here. It is treated
        as dead only if holds_lease() is True: the previous holder's lease then
        expired, and its children died with its container. Otherwise the previous
        holder may still be running somewhere, so recovery refuses.

        A recorded pod is different: a lease says nothing about a pod, which outlives the container that
        launched it. It is read through ``pod_reader`` (default: the installed one, ``podhandle``), and
        only ``dead`` lets recovery go on: an ``alive`` pod and a pod whose state cannot be read (no
        reader, a launcher that does not answer, a corrupt record) both refuse with
        ``podhandle.PodStillLive``, before anything is changed, so a running job stays running and no second
        session can start on its worktree.

        Returns the keys of the jobs this call turned into 'interrupted'.
        """
        lease = None  # asked at most once, and only when a foreign pid needs it
        foreign = []
        dead_pods = []
        for key, job in self.data["jobs"].items():
            pid, pid_host = job.get("pid"), job.get("pid_host")
            status = pid_status(pid, pid_host)
            if status == "alive":
                raise RuntimeError(
                    f"{key}: child process group {pid} is still alive; wait or stop it before resuming"
                )
            if status == "unknown":
                if lease is None:
                    lease = callable(holds_lease) and holds_lease() is True
                if not lease:
                    raise RuntimeError(
                        f"{key}: child pid {pid!s:.20} was recorded on another host "
                        f"(or cannot be verified; host {pid_host!s:.80}); "
                        "recovery needs the repository lease"
                    )
                foreign.append(key)
            pod = job.get("pod")
            if pod is not None:
                pod_state = podhandle.pod_status(pod, pod_reader)
                if pod_state != podhandle.DEAD:
                    raise podhandle.still_live(key, pod, pod_state, "resuming")
                dead_pods.append(key)
        interrupted = []
        for key, job in self.data["jobs"].items():
            if job["status"] == "running":
                interrupted.append(key)
                self.update(
                    key,
                    status="interrupted",
                    pid=None,
                    pid_host=None,
                    error="Supervisor interrupted. Inspect retained worktree then retry explicitly.",
                    **({"stop": None} if job.get("stop") else {}),  # a stop that never took effect is moot
                    **({"pod": None} if key in dead_pods else {}),
                )
            elif key in foreign:
                self.clear_pid(key)
            if key in dead_pods:
                self.clear_pod(key)  # a pod that is gone is forgotten, whatever state its job is in
        return interrupted

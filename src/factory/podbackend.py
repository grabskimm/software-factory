"""The pod backend: one worker session as a pod (slice 12b of the pod backend, ``docs/pod-backend.md``).

``PodBackend`` is the second ``AgentBackend`` (``backend.py``). It reuses ``LocalBackend.prepare`` and
``LocalBackend.collect`` and replaces ``launch``:

1. ``prepare`` is the local one, with no worker identity (the pod has its own), after it refused what a pod cannot
   run: a tripped stop-the-world breaker, a phase outside the contract, a directory outside the data directory.
2. ``launch`` mints a session id (held out of the orphan sweep by ``podsweep.protect`` until the session is
   released), asks the pod pool for a reservation (``podpool.hold``: ceiling, per-repository cap, pacing, run budget),
   mints the one-time relay token, writes the session record into the spool, stages the bundle (``bundle.stage``; its
   manifest's ``run`` section is exactly what ``podworker`` reads), tells the pool the launch request is about to be
   sent (``requested``) and asks the launcher for the pod (``launcher_client``), binds the pod name and uid to the
   record, tells ``on_start`` the pod handle, then polls ``podliveness.Probe`` (the launcher's account of the pod,
   THEN the relay's record) until the session ends. A session that returned a result has its report checked by
   ``podreport.loads``, its transcript and error stream copied to ``<prefix>.jsonl`` and ``<prefix>.stderr`` and
   its file delta validated and applied to the worktree (``bundle.apply_delta``) with the numbers of the report.
   Whatever happened, it revokes, deletes the pod, copies what the relay holds, releases the session, the pool's
   reservation and the sweep's protection, and removes the throwaway home.
3. ``collect`` is the local one: a failure that no result event explains is ``PodOutcome.failure``, raised with the
   usage of the transcript and the API-error code of ``apierror``, so a spend limit that ends a pod session reaches
   the stop-the-world breaker exactly like one that ends a local session.

It is selected only by ``execution = "pod"`` of the repository AND ``FACTORY_POD_BACKEND=on`` of the manager
(``podexecution.backend_for``); nothing here runs at import and nothing imports this module while the switch is off.

Everything a pod session may say about itself is a fixed text. The relay token lives in one local variable from its
creation to the launch request and is never stored, logged or written (only its hash is, in the record). A pod's
transcript, error stream and delta are data: they are copied bounded, never executed and never put in a message.

CONTRACT-TESTED against the in-memory fakes of the launcher, the Kubernetes API and the relay's own code
(``tests/test_podbackend.py``); no pod ran and no cluster was consulted.
"""

import base64
import contextlib
import dataclasses
import functools
import math
import os
import re
import shutil
import stat
import sys
import threading
import time
from pathlib import Path

from . import agent, apierror, backend, bundle, launcher_client, podcontract as pc
from . import podhandle, podliveness, podpool, podreport, podstatus, podsweep, stopworld
from . import sessionrecord as sr
from .backend import AgentHandle, LocalBackend, Outcome
from .phases import phase_name

# --- Numbers (PROVISIONAL, none of them in the contract: the supervisor's own) ------------------------------------

POLL_SECONDS = 2.0
CAPACITY_POLL_SECONDS = 5.0
CAPACITY_WAIT_SECONDS = pc.SCHEDULING_TIMEOUT_SECONDS
SPOOL_POLL_SECONDS = 5.0
SPOOL_WAIT_SECONDS = pc.SCHEDULING_TIMEOUT_SECONDS
# From the launch to the relay saying the bundle was fetched: a node (or a wait for one), the image, the canary and
# the transfer of the bundle (the launcher deletes a pod that waits for a node for ``SCHEDULING_TIMEOUT_SECONDS``).
START_BOUND_SECONDS = pc.SCHEDULING_TIMEOUT_SECONDS + pc.LAUNCH_GRACE_SECONDS + pc.RELAY_TRANSFER_TOTAL_SECONDS
# A pod that is terminating may still post its result during its grace period: wait that long, and a margin.
TERMINATING_WAIT_SECONDS = pc.TERMINATION_GRACE_SECONDS + 15
LAUNCHER_LOST_SECONDS = pc.ORPHAN_SILENCE_SECONDS
MIN_LAUNCH_SECONDS = 60  # the launcher refuses a shorter time limit (``launcher.MIN_TIMEOUT_SECONDS``)
LAUNCHER_WORD = "launcher"  # the ``launcher`` word of the pod handle in job state
ISOLATION_REASON = stopworld.ISOLATION_FAILED
COPY_BYTES = 64 * 1024
# The supervisor waits this long beyond the time limit it gave the wrapper before it gives up on a pod that was
# told to stop itself: the wrapper stops the agent (5 s), drains its pipes, produces the delta and posts the result
# (``podworker``: the stop grace, the drain and flush times and the post, all PROVISIONAL).
TIMEOUT_MARGIN_SECONDS = 120

# The keys of the manifest's ``run`` section: exactly what ``podworker.read_run`` accepts (a test keeps them equal).
RUN_KEYS = ("argv", "env", "cwd", "prompt", "timeout", "memory_limit_mb")
MAX_MEMORY_LIMIT_MB = 1 << 20  # ``podworker.MAX_MEMORY_LIMIT_MB``
# The exit codes of the stages of the wrapper (``podworker.STAGES``; a test keeps them equal): the only numbers a
# wrapper that failed before it posted a result can exit with, and the only words a message may name for one.
WRAPPER_STAGES = {
    "argv": 64,
    "environment": 65,
    "token": 66,
    "import": 67,
    "canary": 68,
    "preflight": 69,
    "wait": 70,
    "bundle": 71,
    "extract": 72,
    "manifest": 73,
    "files": 74,
    "chown": 75,
    "run": 76,
    "internal": 77,
    "key": 78,
    "spawn": 79,
    "result": 80,
    "gone": 81,
}

# The codes the pod worker posts for a failed network canary (``podworker``); the only ones a message may name.
ISOLATION_CODES = (
    "reach_imds",
    "reach_wireserver",
    "reach_hostga",
    "reach_kubelet",
    "reach_manager_api",
    "reach_manager_metrics",
    "reach_forbidden",
    "api_service_open",
    "api_public_open",
    "api_open",
    "control_failed",
    "probe_error",
    "canary_config",
)

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_HEX_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_GITCONFIG = b"[safe]\n\tdirectory = *\n"

# --- Fixed texts (no value of a configuration, an issue, a path or a pod is ever in one) ------------------------------

TRIPPED = "the stop-the-world breaker is tripped; no pod was started"
OUTSIDE = "a pod session ships its directories from the data directory; this session's are elsewhere and nothing was started"
CAPACITY = "no pod capacity became available in time; nothing was started"
POOL_WAITED = "no pod capacity became available in time ({code}); nothing was started"
POOL_REFUSED = "the pod pool refused the session ({code}); nothing was started"
POOL_LOST = "the pod pool no longer holds the session's reservation; nothing was started"
BUDGET_UNKNOWN = "the pod session's budget could not be determined; nothing was started"
SPOOL = "the spool stayed full for too long; nothing was started"
LAUNCHER_UNTRUSTED = "the pod launcher could not be trusted; nothing was sent to it"
LAUNCHER_DOWN = "the pod launcher did not answer the launch; the pod was asked to be deleted"
RECORD = "the pod session could not be recorded; nothing was started"
BOUND = "the pod session could not be bound to its pod; the pod was asked to be deleted"
ISOLATION = (
    "the pod's network isolation check failed ({code}); the launcher is denying launches and the "
    "stop-the-world breaker is tripped"
)
FAILURES = {
    "ended": "the pod ended without returning a result",
    "evicted": "the pod was evicted or deleted before it returned a result",
    "gone": "the pod disappeared before it returned a result",
    "not_scheduled": "the pod could not be scheduled or started",
    "not_started": "the pod did not start in time",
    "silent": "the pod stopped sending heartbeats",
    "launcher_lost": "the pod launcher stopped answering",
    "expired": "the pod session expired before a result was returned",
    "revoked": "the pod session was revoked before a result was returned",
    "report": "the pod's result report is not valid",
    "output": "the pod's output could not be read",
    "streams": "the pod's output does not match what the pod reported sending",
    "terminated": "the pod was stopped before the agent finished",
    "record_lost": "the relay no longer has the pod session's record",
    "relay_lost": "the relay's session record could not be read",
    "delta_rejected": "the pod's file delta was rejected and nothing was applied",
    "delta_too_large": "the pod's file delta was too large and nothing was applied",
    "delta_pod_failed": "the pod could not read its tree to return a file delta; nothing was applied",
    "delta_failed": "the pod's file delta could not be applied",
}
CUT_OFF = "the relay cut the pod session off ({code})"
STAGE_FAILED = "the pod worker failed at its {stage} stage"


class PodConfigError(ValueError):
    """The pod backend cannot be built from this configuration: the text is fixed and names no value."""

    def __init__(self):
        super().__init__("the pod backend is not configured")


@dataclasses.dataclass(frozen=True)
class PodOutcome(Outcome):
    """What a pod session observed: ``Outcome`` and, for an end that no result event explains, a fixed text.

    ``failure`` is one of ``FAILURES`` (or the relay's cut-off): ``collect`` raises it with the usage of the
    transcript and the API-error code of the classifier.
    """

    failure: str | None = None


@dataclasses.dataclass(frozen=True)
class Scope:
    """The repository and the run of this supervisor process: one supervisor serves one of each."""

    repository: str
    run_id: str


_scope = None
_scope_lock = threading.Lock()


def set_scope(repository, run_id):
    """Remember the repository and the run of this process (``activate``); PodConfigError when they are not valid ids."""
    try:
        pc.worker_annotations(repository, run_id, "job", 0)
    except ValueError:
        raise PodConfigError from None
    global _scope
    with _scope_lock:
        _scope = Scope(repository, run_id)


def clear_scope():
    global _scope
    with _scope_lock:
        _scope = None


def current_scope():
    with _scope_lock:
        return _scope



_pool = None
_pool_lock = threading.Lock()


def set_pool(pool):
    """Remember the pod pool of this process (``activate``): one pool serves every session of the supervisor."""
    global _pool
    with _pool_lock:
        _pool = pool


def clear_pool():
    set_pool(None)


def current_pool():
    with _pool_lock:
        return _pool


# --- The backend ------------------------------------------------------------------------------------------------------


@dataclasses.dataclass
class _Run:
    """What one ``launch`` holds. The relay token is not here: it exists only until the launch request is sent."""

    session: str
    launched_at: float = 0.0
    pod: object = None  # ``launcher_client.Launched``
    handle: object = None  # ``podhandle.PodHandle``
    seen_state: str | None = None
    beat: int | None = None
    progress: float = 0.0
    running_since: float | None = None
    terminating_since: float | None = None
    blind_since: float | None = None  # since when the launcher or the relay could not be read
    status: dict | None = None  # the launcher's answer of the poll in progress (None: it could not be had)
    gone: bool = False  # ``podliveness.enforce`` confirmed the pod is gone
    reservation: object = None  # ``podpool.Reservation``
    stack: object = None  # the ``contextlib.ExitStack`` that releases it
    delivered: bool = False
    copied: bool = False
    report: dict | None = None


class PodBackend(LocalBackend):
    """Runs the session as a pod behind the launcher; see the module docstring.

    ``client`` is a ``launcher_client.LauncherClient`` (or anything with its ``launch``, ``pod_state``,
    ``delete_pod``, ``terminate`` and ``deny``). ``pool`` is the process's ``podpool.PodPool``, ``spool`` the
    directory the relay and the supervisor share, ``data_root`` the supervisor's data directory (``/data``: the pod's
    is the same path), ``scope`` the repository and the run, ``job_id``, ``attempt`` and ``base`` (the job's base
    commit, or None) the session's own. ``spent_usd`` (a number, or a callable that returns the run's cost so far)
    and ``budget_usd`` (the repository's ``run_budget_usd`` or None) are what the pool checks the run's budget
    with. ``clock`` (monotonic seconds), ``wall`` (epoch seconds) and ``sleep`` are injectable, so a test never waits.
    """

    def __init__(
        self,
        *,
        client,
        pool,
        spool,
        data_root,
        scope,
        job_id,
        attempt,
        manager_instance,
        base=None,
        breaker_dir=None,
        spent_usd=None,
        budget_usd=None,
        clock=time.monotonic,
        wall=time.time,
        sleep=time.sleep,
        poll_seconds=POLL_SECONDS,
    ):
        self._client = client
        self._pool = pool
        self._spool = os.fspath(spool)
        self._data_root = os.path.realpath(os.fspath(data_root))
        self._scope = scope
        self._job_id = job_id
        self._attempt = attempt
        self._manager = manager_instance
        self._base = base
        self._breaker_dir = None if breaker_dir is None else os.fspath(breaker_dir)
        self._spent = spent_usd
        self._budget = budget_usd
        self._clock = clock
        self._wall = wall
        self._sleep = sleep
        self._poll = poll_seconds
        self._validate()

    def _validate(self):
        scope = self._scope
        if not isinstance(scope, Scope) or not os.path.isabs(self._spool) or not os.path.isabs(self._data_root):
            raise PodConfigError
        if self._pool is None:
            raise PodConfigError
        if self._base is not None and not (isinstance(self._base, str) and _HEX_COMMIT.fullmatch(self._base)):
            raise PodConfigError
        try:
            pc.worker_labels(scope.repository, scope.run_id, self._job_id, self._attempt, pc.PHASES[0], self._manager, "0" * 32)
            pc.worker_annotations(scope.repository, scope.run_id, self._job_id, 0)
        except ValueError:
            raise PodConfigError from None

    @classmethod
    def from_environment(cls, env, *, job_id, attempt, base=None, scope=None, client=None, pool=None, **seams):
        """The backend of the manager's environment, or PodConfigError.

        The launcher socket and the spool come from ``podcontract.ENV_MANAGER``, the manager instance from
        ``FACTORY_MANAGER_INSTANCE``, the data directory from ``FACTORY_DATA`` (which must be the pod's own
        ``/data`` when it is set: a pod lays the session's files out at the same absolute paths). The pool is the
        one ``activate`` built (``current_pool``): a process without one cannot run a pod session.
        """
        chosen = current_scope() if scope is None else scope
        if chosen is None:
            raise PodConfigError
        data = env.get("FACTORY_DATA") or pc.SCRATCH_DIR
        if os.path.realpath(data) != os.path.realpath(pc.SCRATCH_DIR):
            raise PodConfigError
        instance = env.get(pc.MANAGER_INSTANCE_ENV)
        try:
            pc.validate_instance_id(instance)
            launcher = client or launcher_client.LauncherClient.from_environment(env)
        except ValueError:
            raise PodConfigError from None
        seams.setdefault("breaker_dir", data)
        return cls(
            client=launcher,
            pool=current_pool() if pool is None else pool,
            spool=env.get(pc.ENV_MANAGER["spool_dir"]) or pc.SPOOL_DIR,
            data_root=data,
            scope=chosen,
            job_id=job_id,
            attempt=attempt,
            manager_instance=instance,
            base=base,
            **seams,
        )

    # -- prepare -----------------------------------------------------------------------------------------------

    def _require_identity(self, request):
        """The pod runs as its own worker identity: nothing to require of the repository's ``[worker]`` ids."""

    def _breaker_state(self):
        return None if self._breaker_dir is None else stopworld.read_state(self._breaker_dir)

    def _refuse_tripped(self):
        if self._breaker_state() is not None:
            raise agent.AgentError(TRIPPED)

    def _under_data(self, path):
        real = os.path.realpath(os.fspath(path))
        return real == self._data_root or real.startswith(f"{self._data_root}{os.sep}")

    def prepare(self, request):
        self._refuse_tripped()
        root = phase_name(request.phase)
        if root not in pc.PHASES:
            raise ValueError(f"unknown agent phase: {request.phase!r}")
        if not all(self._under_data(path) for path in (request.cwd, request.prefix, *request.read_paths)):
            raise agent.AgentError(OUTSIDE)
        # The pod has its own identity (uid 10002): the repository's worker ids are for local processes. Its memory is
        # its class limit, not a share of THIS pod's: the guard's limit is the one the request names, or none (0), never
        # the one ``capacity`` derives from the manager pod's own resources.
        limit = 0 if request.memory_limit_mb is None else request.memory_limit_mb
        return super().prepare(dataclasses.replace(request, user=None, group=None, memory_limit_mb=limit))

    # -- launch ------------------------------------------------------------------------------------------------

    def launch(self, spec, on_start=None):
        run = _Run(session=pc.new_session_id())
        self._open_outputs(spec)
        # Held out of the orphan sweep from before the pod exists until after ``released`` is written.
        with podsweep.protect(run.session):
            try:
                return self._run(spec, run, on_start)
            finally:
                self._release(spec, run)

    def _open_outputs(self, spec):
        """The two files a backend leaves behind, empty to begin with (as ``LocalBackend`` opens them)."""
        for suffix in (".jsonl", ".stderr"):
            agent.sibling(spec.prefix, suffix).write_bytes(b"")

    @staticmethod
    def _seconds(spec):
        """The session's time limit as the launch request, the record and the wrapper's ``run`` section carry it."""
        return min(max(MIN_LAUNCH_SECONDS, math.ceil(spec.timeout)), pc.DEADLINE_CLAMP_SECONDS)

    def _deadline(self, seconds):
        """The relay record's deadline for a pod launched NOW: the scheduling window, the time limit and the margin."""
        return (
            math.ceil(self._wall())
            + pc.SCHEDULING_TIMEOUT_SECONDS
            + min(seconds, pc.DEADLINE_CLAMP_SECONDS)
            + pc.DEADLINE_MARGIN_SECONDS
        )

    def _refresh_deadline(self, run, seconds):
        """Rewrite the record's deadline from the clock of THIS moment, just before the launch request.

        The record is written before the bundle is staged and the pool's capacity is waited on, and both waits can
        last ten minutes each: a deadline computed before them would eat the session's time limit (the relay and
        ``podliveness`` end a session at its deadline). Nothing else in the record changes.
        """
        try:
            with sr.session_dir(self._spool, run.session) as fd:
                record = sr.read_at(fd)
                record["deadline"] = self._deadline(seconds)
                sr.write_at(fd, record)
        except (OSError, sr.RecordError):
            raise agent.AgentError(RECORD) from None

    def _stamp(self):
        """``created_at`` of the pod handle, on the injected wall clock (``podliveness`` measures silence from it)."""
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._wall()))

    def _run(self, spec, run, on_start):
        seconds = self._seconds(spec)
        request = {
            "session_id": run.session,
            "repository": self._scope.repository,
            "run_id": self._scope.run_id,
            "job_id": self._job_id,
            "attempt": self._attempt,
            "phase": spec.phase,
            "resource_class": spec.resource_class,
            "timeout_seconds": seconds,
            "manager_instance": self._manager,
        }
        waiting_until = self._clock() + CAPACITY_WAIT_SECONDS
        self._admit(spec, run, request, waiting_until)
        token = sr.new_token()
        record = sr.build(
            run.session,
            token,
            self._deadline(seconds),
            pc.repo_key(self._scope.repository),
            spec.write_scope,
            self._manager,
        )
        try:
            sr.write(self._spool, record)
        except (OSError, sr.RecordError):
            raise agent.AgentError(RECORD) from None
        self._stage(spec, run)
        run.pod = self._launch_pod(spec, run, request, token, waiting_until, seconds)
        del token
        run.launched_at = self._clock()
        run.progress = run.launched_at
        run.handle = podhandle.PodHandle(
            name=run.pod.name,
            uid=run.pod.uid,
            session=run.session,
            launcher=LAUNCHER_WORD,
            created_at=self._stamp(),
        )
        podhandle.register(run.handle)
        try:
            sr.bind(self._spool, run.session, run.pod.name, run.pod.uid)
        except (OSError, sr.RecordError):
            raise agent.AgentError(BOUND) from None
        if on_start:
            on_start(AgentHandle(pod=run.handle))
        verdict = self._wait(spec, run)
        return self._conclude(spec, run, verdict)

    # -- the pool ------------------------------------------------------------------------------------------------------------

    @staticmethod
    def _estimate(spec):
        """What the session may spend: the phase's ``--max-budget-usd`` as the command line carries it."""
        argv = list(spec.argv)
        try:
            return float(argv[argv.index("--max-budget-usd") + 1])
        except (ValueError, IndexError):
            raise agent.AgentError(BUDGET_UNKNOWN) from None

    def _admit(self, spec, run, request, waiting_until):
        """Hold a reservation of the pool (``podpool.hold``) until ``_release``; a refusal is a fixed text, nothing started."""
        try:
            spent = self._spent() if callable(self._spent) else (self._spent or 0)
            wanted = podpool.Request.for_launch(
                request, estimate_usd=self._estimate(spec), spent_usd=spent, budget_usd=self._budget
            )
        except agent.AgentError:
            raise
        except Exception:  # noqa: BLE001 - a cost that cannot be read is no reason to start a billed pod
            raise agent.AgentError(BUDGET_UNKNOWN) from None
        stack = contextlib.ExitStack()
        try:
            reservation = stack.enter_context(
                podpool.hold(
                    self._pool,
                    wanted,
                    max(0.0, waiting_until - self._clock()),
                    sleep=self._sleep,
                    clock=self._clock,
                )
            )
        except podpool.Timeout as error:
            raise agent.AgentError(POOL_WAITED.format(code=_closed(error.code))) from None
        except podpool.Refused as error:
            if error.code == podpool.BREAKER:
                raise agent.AgentError(TRIPPED) from None
            raise agent.AgentError(POOL_REFUSED.format(code=_closed(error.code))) from None
        except podpool.PoolError:
            raise agent.AgentError(POOL_REFUSED.format(code=podpool.UNAVAILABLE)) from None
        run.stack, run.reservation = stack, reservation

    def _free(self, run):
        """Give the slot and the estimate back: a launch that certainly did not happen also gets its pacing token back."""
        reservation, stack = run.reservation, run.stack
        run.reservation = run.stack = None
        if reservation is not None:
            reservation.cancel()  # refunds the token only while ``requested`` was never called
        if stack is not None:
            stack.close()

    # -- staging and launching ------------------------------------------------------------------------------------------

    def manifest(self, spec):
        """The manifest of the bundle (``meta/manifest.json``): what the pod worker lays out and runs.

        ``dirs`` and ``files`` are the keys part A of the wrapper reads (the throwaway home and the session's files
        with the modes it accepts: 0600 for the prompt, which only the wrapper reads, 0640 for the rest); ``run``
        is part B's, EXACTLY ``RUN_KEYS`` (``podworker.read_run`` refuses any other key set): the command line and
        the non-secret environment additions of the ``LaunchSpec``, the worktree, the path of the prompt file, the
        time limit and the memory limit. The prefix, the write scope and the phase do not travel: the supervisor
        keeps them. No secret is in it: the Anthropic key is the launcher's and reaches the pod only through the
        Secret.
        """
        prompt = agent.sibling(spec.prefix, ".prompt.txt")
        files = [
            {
                "path": os.fspath(item.path),
                "data": base64.b64encode(item.data).decode("ascii"),
                "mode": 0o600 if Path(item.path) == prompt else 0o640,
            }
            for item in spec.files
        ]
        files.append(
            {
                "path": os.fspath(Path(spec.home) / ".gitconfig"),
                "data": base64.b64encode(_GITCONFIG).decode("ascii"),
                "mode": 0o640,
            }
        )
        return {
            "version": 1,
            "dirs": [os.fspath(spec.home), os.fspath(spec.config_dir)],
            "files": files,
            "run": {
                "argv": list(spec.argv),
                "env": dict(spec.env),
                "cwd": os.fspath(spec.cwd),
                "prompt": os.fspath(prompt),
                "timeout": self._seconds(spec),
                "memory_limit_mb": self._memory_limit(spec),
            },
        }

    @staticmethod
    def _memory_limit(spec):
        """The memory guard's limit in MiB for the wrapper (None, and 0 in a ``LaunchSpec``, is no limit)."""
        limit = spec.memory_limit_mb
        if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
            return min(limit, MAX_MEMORY_LIMIT_MB)
        return None

    def _stage(self, spec, run):
        """Stage the bundle; a full spool is a wait (like capacity), anything else fails the session."""
        deadline = self._clock() + SPOOL_WAIT_SECONDS
        while True:
            try:
                bundle.stage(
                    self._spool,
                    run.session,
                    data_root=self._data_root,
                    worktree=spec.cwd,
                    manifest=self.manifest(spec),
                    scratch=Path(spec.prefix).parent,
                    base=self._base,
                    read_paths=spec.read_paths,
                )
                return
            except bundle.SpoolFull:
                if self._clock() >= deadline:
                    raise agent.AgentError(SPOOL) from None
                self._sleep(SPOOL_POLL_SECONDS)
            except bundle.BundleTooLarge:
                raise agent.AgentError("the session's tree is too large to ship to a pod (bundle too large)") from None
            except bundle.BundleError as error:
                raise agent.AgentError(
                    f"the session's tree could not be shipped to a pod ({error.reason})"
                ) from None
            except (OSError, ValueError):
                raise agent.AgentError("the session's tree could not be shipped to a pod") from None

    def _launch_pod(self, spec, run, request, token, waiting_until, seconds):
        """Ask the launcher for the pod, with the pool told FIRST (``requested``) so that its ledger never undercounts.

        A launcher that answers ``capacity`` created nothing: the reservation (slot and estimate) is given back, the
        pacing token stays spent (the request was sent) and the pool is asked again after a pause.
        """
        while True:
            self._refuse_tripped()
            self._refresh_deadline(run, seconds)
            try:
                run.reservation.requested()
            except podpool.Lost:
                raise agent.AgentError(POOL_LOST) from None
            except podpool.PoolError:
                raise agent.AgentError(POOL_REFUSED.format(code=podpool.UNAVAILABLE)) from None
            try:
                return self._client.launch(request, token)
            except launcher_client.Capacity:
                self._free(run)
                if self._clock() >= waiting_until:
                    raise agent.AgentError(CAPACITY) from None
                self._sleep(CAPACITY_POLL_SECONDS)
                self._admit(spec, run, request, waiting_until)
            except launcher_client.Rejected as error:
                raise agent.AgentError(f"the pod launcher refused the launch ({error.code})") from None
            except launcher_client.ServerRefused:
                raise agent.AgentError(LAUNCHER_UNTRUSTED) from None
            except (launcher_client.LauncherError, ValueError):
                # The launch may have been applied (the answer was lost): its name is derived, so ask for it to go.
                with contextlib.suppress(Exception):
                    self._client.delete_pod(pc.pod_name(run.session))
                raise agent.AgentError(LAUNCHER_DOWN) from None

    # -- waiting ----------------------------------------------------------------------------------------------------------

    def _wait(self, spec, run):
        """Poll until the session ends; the verdict is a tuple whose first item is the kind."""
        probe = podliveness.Probe(self._spool, functools.partial(self._read_pod, run), clock=self._wall)
        while True:
            now = self._clock()
            run.status = None
            liveness = probe.assess(run.handle)
            try:
                state = sr.read_state(self._spool, run.session)
                beat = sr.read_heartbeat(self._spool, run.session)
            except (OSError, sr.RecordError):
                state, beat = None, None
            verdict = self._judge(spec, run, liveness, state, beat, now)
            if verdict is not None:
                return verdict
            self._sleep(self._poll)

    def _read_pod(self, run, name, uid):
        """``LauncherClient.pod_state`` for the probe, keeping the answer: ``podliveness`` says WHEN, this says WHAT ended it."""
        answer = self._client.pod_state(name, uid)
        run.status = answer
        return answer

    def _enforce(self, run, liveness):
        """Carry out a ``terminate`` verdict: the ``revoke`` marker first, then the pod is deleted (``podliveness.enforce``)."""
        if liveness.action == podliveness.TERMINATE:
            run.gone = podliveness.enforce(liveness, run.handle, self._spool, self._client.terminate)

    def _judge(self, spec, run, liveness, state, beat, now):
        """The end of the session as a tuple, or None to wait on. ``liveness`` is the ``podliveness.Verdict`` of this poll."""
        if state is not None:
            kind = state["state"]
            if kind != run.seen_state:
                run.seen_state, run.progress = kind, now
            if beat != run.beat:
                run.beat, run.progress = beat, now
            if kind in ("fetched", "streaming") and run.running_since is None:
                run.running_since = now
        what = liveness.state
        if what == podliveness.FINISHED:
            if liveness.reject is not None:
                return ("rejected", liveness.reject, None if state is None else state["preflight_code"])
            return ("finished",)
        if what != podliveness.UNREADABLE:
            run.blind_since = None
        elif run.blind_since is None:
            run.blind_since = now
        ended = self._ended_by(run, liveness)
        if ended is not None:
            return ended
        if what == podliveness.TERMINATING:
            # Being deleted, still in the API: it may post its result during its grace period.
            if run.terminating_since is None:
                run.terminating_since = now
            if now - run.terminating_since <= TERMINATING_WAIT_SECONDS:
                return None
            return self._ended(run.status)
        run.terminating_since = None
        # Nothing ended it: the supervisor's own bounds (the liveness verdict cannot know when a pod got its node).
        if run.running_since is not None and now - run.running_since > self._seconds(spec) + TIMEOUT_MARGIN_SECONDS:
            return ("timeout",)
        if state is not None and state["state"] in ("fetched", "streaming") and now - run.progress > pc.ORPHAN_SILENCE_SECONDS:
            return ("failure", "silent")
        if run.running_since is None and now - run.launched_at > START_BOUND_SECONDS:
            return ("failure", "not_started")
        if run.blind_since is not None and now - run.blind_since > LAUNCHER_LOST_SECONDS:
            return ("failure", "launcher_lost" if liveness.reason == "launcher_unreadable" else "relay_lost")
        return None

    def _ended_by(self, run, liveness):
        """The end a liveness verdict (other than ``finished``) calls for, acted on; None when it calls for none.

        ``silent`` for ``never_heard`` is not acted on: silence is measured from the pod's creation, which cannot tell
        a pod that waited for its node from one that never started, so the supervisor's own bounds decide that one.
        """
        what, reason = liveness.state, liveness.reason
        if what == podliveness.RELEASED:
            return ("failure", "ended")
        if what == podliveness.REVOKED:
            self._enforce(run, liveness)
            return ("failure", "revoked")
        if what == podliveness.EXPIRED:
            self._enforce(run, liveness)
            return ("failure", "expired")
        if what == podliveness.LOST:
            self._enforce(run, liveness)
            if reason == "no_session_record":
                return ("failure", "record_lost")
            return self._ended(run.status)
        if what == podliveness.SILENT and reason != "never_heard":
            self._enforce(run, liveness)
            return ("failure", "not_started" if reason == "never_started" else "silent")
        return None

    @staticmethod
    def _ended(status):
        """The verdict for a pod that is over (or being deleted for good) and returned no result the relay holds."""
        if status is None:
            return ("failure", "ended")
        kind = status["state"]
        code = status["exit_code"]
        if kind == podstatus.FAILED_EXIT:
            number = code if isinstance(code, int) and not isinstance(code, bool) and code != 0 else 1
            for stage, stage_code in WRAPPER_STAGES.items():
                if number == stage_code:
                    return ("stage", stage)
            return ("exit", number)
        if kind == podstatus.OOM:
            return ("oom", code if isinstance(code, int) and not isinstance(code, bool) and code != 0 else 137)
        if kind == podstatus.DEADLINE:
            return ("timeout",)
        if kind == podstatus.EVICTED:
            return ("failure", "evicted")
        if kind == podstatus.GONE:
            return ("failure", "gone")
        if kind in (podstatus.UNSCHEDULABLE, podstatus.STARTING_ERROR):
            return ("failure", "not_scheduled")
        return ("failure", "ended")

    # -- the end of a session ------------------------------------------------------------------------------------------------

    def _conclude(self, spec, run, verdict):
        kind = verdict[0]
        if kind == "timeout":
            raise agent.AgentTimeout(spec.timeout)
        if kind == "rejected":
            return self._rejected(run, verdict[1], verdict[2])
        if kind == "finished":
            return self._deliver(spec, run)
        if kind == "stage":
            if verdict[1] == "canary":
                # The wrapper exits with this stage when its network check did not pass; when it could not post the
                # reason, this exit is all the supervisor learns, and a check that did not pass is not a check that did.
                return self._isolation_failed(None)
            return PodOutcome(None, failure=STAGE_FAILED.format(stage=verdict[1]))
        # The outputs of every other end are copied by the release, after the pod is gone.
        if kind == "oom":
            return PodOutcome(verdict[1], None, self._oom_message(spec))
        if kind == "exit":
            return PodOutcome(verdict[1])
        return PodOutcome(None, failure=FAILURES[verdict[1]])

    @staticmethod
    def _oom_message(spec):
        limit = pc.CLASSES[spec.resource_class]["memory"]
        return f"worker exceeded its memory limit: the pod was killed at its {spec.resource_class} class limit of {limit}"

    def _exceeded_message(self, spec, report):
        """What ``memwatch`` says when it kills a local session (numbers only), from the report's; else the pod-level text."""
        over = report["exceeded_mb"]
        footprint = report["footprint"]
        limit = footprint["limit_mb"] if footprint is not None and footprint["limit_mb"] else self._memory_limit(spec)
        if over is None or not limit:
            return self._oom_message(spec)
        return f"worker exceeded its memory limit: peak {over} MiB over {limit} MiB"

    def _rejected(self, run, code, preflight_code):
        if code == "preflight_failed":
            return self._isolation_failed(preflight_code)
        return PodOutcome(None, failure=CUT_OFF.format(code=code))

    def _isolation_failed(self, preflight_code):
        named = preflight_code if preflight_code in ISOLATION_CODES else "unknown"
        self._trip_isolation()
        raise agent.AgentError(ISOLATION.format(code=named))

    def _trip_isolation(self):
        """A pod that could reach what it must not: no launch may follow, from this launcher or any supervisor."""
        with contextlib.suppress(Exception):
            self._client.deny(ISOLATION_REASON)
        if self._breaker_dir is not None:
            with contextlib.suppress(Exception):
                stopworld.Breaker(self._breaker_dir).trip(stopworld.ISOLATION_FAILED)

    def _deliver(self, spec, run):
        """The result is in the relay: check the report, copy the outputs, apply the delta, and map the wrapper's outcome."""
        try:
            report = self._read_report(run)
        except (OSError, sr.RecordError, ValueError):
            return PodOutcome(None, failure=FAILURES["report"])
        run.report = report
        run.delivered = True
        code, footprint = report["exit_code"], report["footprint"]
        try:
            self._copy_outputs(spec, run)
        except (OSError, sr.RecordError):
            return PodOutcome(code, footprint, failure=FAILURES["output"])
        problem = self._streams_differ(spec, report) or self._apply_delta(spec, run, report)
        outcome = report["outcome"]
        if outcome == "timeout":
            raise agent.AgentTimeout(spec.timeout)
        if outcome == "memory_limit":
            return PodOutcome(code, footprint, self._exceeded_message(spec, report))
        if outcome == "terminated":
            return PodOutcome(code, footprint, failure=FAILURES["terminated"])
        return PodOutcome(code, footprint, failure=problem)

    @staticmethod
    def _streams_differ(spec, report):
        """The failure text when a copied stream is not the size the wrapper says the relay accepted, else None."""
        try:
            for suffix, name in ((".jsonl", "transcript"), (".stderr", "stderr")):
                if agent.sibling(spec.prefix, suffix).stat().st_size != report["streamed"][name]:
                    return FAILURES["streams"]
        except OSError:
            return FAILURES["output"]
        return None

    def _apply_delta(self, spec, run, report):
        delta = report["delta"]
        if delta["status"] == "too_large":
            return FAILURES["delta_too_large"]
        if delta["status"] != "ok":
            return FAILURES["delta_pod_failed"]
        try:
            found = self._open_out(run, sr.OUT_DELTA)
        except (OSError, sr.RecordError):
            return FAILURES["output"]
        if found is None:
            return FAILURES["report"]
        descriptor, _ = found
        try:
            with os.fdopen(descriptor, "rb") as source:
                bundle.apply_delta(
                    source,
                    spec.cwd,
                    write_scope=spec.write_scope,
                    scratch=Path(spec.prefix).parent,
                    declared_records=delta["records"],
                    declared_bytes=delta["bytes"],
                )
        except bundle.DeltaTooLarge:
            return FAILURES["delta_too_large"]
        except bundle.DeltaApplyFailed:
            return FAILURES["delta_failed"]
        except bundle.BundleError:
            return FAILURES["delta_rejected"]
        except OSError:
            return FAILURES["output"]
        return None

    # -- what the relay holds ------------------------------------------------------------------------------------------------

    def _open_out(self, run, name):
        """``(descriptor, size)`` of the regular file ``out/<name>`` of the session, None when it does not exist.

        Opened relative to the session directory, never following a link; anything but a regular file is an error.
        """
        with sr.session_dir(self._spool, run.session) as session_fd:
            try:
                out_fd = os.open(sr.OUT_DIR, _DIR_FLAGS, dir_fd=session_fd)
            except FileNotFoundError:
                return None
            try:
                descriptor = os.open(name, _READ_FLAGS, dir_fd=out_fd)
            except FileNotFoundError:
                return None
            finally:
                os.close(out_fd)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise OSError("not a regular file")
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor, info.st_size

    def _copy_out(self, run, name, destination, cap):
        """Copy ``out/<name>`` to ``destination`` (empty when the relay has none), 64 KiB at a time, within ``cap``."""
        found = self._open_out(run, name)
        with open(destination, "wb") as sink:
            if found is None:
                return
            descriptor, size = found
            try:
                if size > cap:
                    raise OSError("output over its cap")
                while True:
                    data = os.read(descriptor, COPY_BYTES)
                    if not data:
                        return
                    sink.write(data)
            finally:
                os.close(descriptor)

    def _copy_outputs(self, spec, run):
        """The transcript and the error stream to ``<prefix>.jsonl`` and ``<prefix>.stderr``, once."""
        if run.copied:
            return
        run.copied = True
        self._copy_out(run, sr.OUT_TRANSCRIPT, agent.sibling(spec.prefix, ".jsonl"), pc.TRANSCRIPT_MAX_BYTES)
        self._copy_out(run, sr.OUT_STDERR, agent.sibling(spec.prefix, ".stderr"), pc.STDERR_MAX_BYTES)

    def _read_report(self, run):
        """The pod's report as ``podreport.loads`` validates it; ValueError (``report``) when there is none or it is not valid."""
        found = self._open_out(run, sr.OUT_REPORT)
        if found is None:
            raise ValueError("report")
        descriptor, _ = found
        with os.fdopen(descriptor, "rb") as source:
            raw = source.read(podreport.MAX_BYTES + 1)
        return podreport.loads(raw)

    # -- release ---------------------------------------------------------------------------------------------------------

    def _release(self, spec, run):
        """Everything ``launch`` held, on every path: revoke, delete the pod, copy, release, the pool, remove the home."""
        if not run.delivered:
            with contextlib.suppress(OSError, sr.RecordError):
                sr.mark(self._spool, run.session, sr.MARKER_REVOKE)
        if run.handle is not None:
            # ``run.gone``: ``podliveness.enforce`` already had the pod deleted and confirmed it.
            if run.gone or podhandle.terminate_pod(run.handle, terminate=self._client.terminate):
                podhandle.unregister(run.handle)
        elif run.pod is not None:  # created, but its handle was never made: delete it by its name
            with contextlib.suppress(Exception):
                self._client.delete_pod(run.pod.name, run.pod.uid)
        with contextlib.suppress(OSError, sr.RecordError):
            self._copy_outputs(spec, run)
        with contextlib.suppress(OSError, sr.RecordError):
            sr.mark(self._spool, run.session, sr.MARKER_RELEASED)
        shutil.rmtree(spec.home, ignore_errors=True)
        self._free(run)

    # -- collect ---------------------------------------------------------------------------------------------------------

    def collect(self, spec, outcome):
        failure = getattr(outcome, "failure", None)
        if failure is None:
            return super().collect(spec, outcome)
        raise self._failed(spec, outcome, failure)

    @staticmethod
    def _failed(spec, outcome, failure):
        prefix = spec.prefix
        log = agent.sibling(prefix, ".jsonl")
        message = f"{failure}; see {prefix}.jsonl and {prefix}.stderr"
        event = agent.last_result(log)
        if event is None:
            return backend._no_result(message, prefix)
        usage = agent.usage_record(event, spec.phase, spec.model, outcome.footprint)
        return agent.AgentError(message, usage, backend._api_code(apierror.classify(event)))


def _closed(code):
    """A pool refusal code if it is one of the closed set, else ``unavailable``: only these words reach a message."""
    return code if code in podpool.REFUSALS else podpool.UNAVAILABLE


# --- Wiring (the manager switch is on) ----------------------------------------------------------------------------------


def activate(repository, run_id, env=None, *, client=None, pool=None):
    """Wire the pod backend into this supervisor process: the scope, the pool, the reader and terminator, the kill hook.

    Called once by ``cli.run`` and only when the manager switch is on (``podexecution.activate``). It installs
    the launcher client's ``reader`` and ``terminate`` as the process-wide pair of ``podhandle`` (so recovery,
    ``purge`` and ``status`` can read a recorded pod), registers ``podhandle.kill_live_pods`` as a second hook of
    the lease-loss path, builds the process's one pod pool (``podpool.from_env``: the ceiling, the ledger directory
    and the launcher's live count) and remembers the repository and the run for every session. It returns the
    function that undoes all of it. A repository or run that is not a valid id leaves the scope unset, and a pool
    that cannot be built (no ceiling, no state directory) leaves the pool unset: the sessions are then refused
    (``podexecution``), the reader and the terminator are installed all the same.
    """
    from . import locks

    source = os.environ if env is None else env
    try:
        launcher = client or launcher_client.LauncherClient.from_environment(source)
    except ValueError:  # an unusable socket path: nothing can be read or terminated, so recorded pods stay unknown
        print("factory: pod backend not wired in: the launcher socket path is not valid", file=sys.stderr)
        return lambda: None
    podhandle.install(reader=launcher.reader, terminate=launcher.terminate)
    hook = functools.partial(podhandle.kill_live_pods, launcher.terminate, podhandle.ABORT_TIMEOUT)
    locks.register_kill_hook(hook)
    try:
        set_scope(repository, run_id)
    except PodConfigError:
        clear_scope()
    try:
        built = pool or podpool.from_env(source, live_count=podpool.launcher_live_count(launcher))
    except podpool.ConfigRefused:
        built = None
        print("factory: pod backend has no pod pool: its configuration was refused", file=sys.stderr)
    set_pool(built)

    def deactivate():
        locks.unregister_kill_hook(hook)
        podhandle.install()
        clear_scope()
        clear_pool()

    return deactivate


def build_sweeper(env=None, *, client=None):
    """The ``podsweep.Sweeper`` of the manager (``Manager.pods``), or None when the backend cannot be built here.

    None unless the manager switch is ``on`` and the launcher socket path and the spool directory are usable
    (``podexecution.sweeper`` calls this only for the switch on). The sweeper lists and deletes pods through the
    launcher client and revokes and releases relay records in the spool; it never imports or starts a session.
    """
    source = os.environ if env is None else env
    if source.get(pc.ENV_MANAGER["pod_backend"]) != pc.POD_BACKEND_ON:
        return None
    spool = source.get(pc.ENV_MANAGER["spool_dir"]) or pc.SPOOL_DIR
    if not os.path.isabs(spool):
        return None
    try:
        launcher = client or launcher_client.LauncherClient.from_environment(source)
    except ValueError:
        return None
    return podsweep.Sweeper(launcher.list_pods, launcher.delete_pod, spool)

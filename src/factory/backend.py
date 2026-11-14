"""Execution backends: how one ``claude -p`` session is prepared, launched and collected.

``agent.run_agent`` used to do all of it in one function. It is now three steps behind
``AgentBackend`` so that a session can run somewhere other than a child of the supervisor (the
pod backend of ``docs/pod-backend.md``) without touching the three callers (``Runner.invoke``,
the planner and the preflight probe):

* ``prepare(request) -> LaunchSpec`` validates the request, writes the run artifacts (prompt,
  settings, system prompt, the throwaway home) and describes the session: argv, the non-secret
  environment additions, the files, the limits. Nothing is started.
* ``launch(spec, on_start) -> Outcome`` starts the session, tells ``on_start`` its ``AgentHandle``,
  waits for it (or kills it at the time limit) and releases everything it held.
* ``collect(spec, outcome) -> (result, usage)`` turns the transcript and the outcome into the
  structured result and the usage record, or into the error the callers already know.

``LocalBackend`` is the in-process implementation: the code that used to be ``run_agent``, moved
without a change of behaviour (``tests/test_backend.py`` holds golden captures taken before the
move). The helpers it uses stay in ``agent.py`` and are reached through that module, so a test that
patches ``factory.agent.<name>`` keeps working.

Nothing here prints prompts or agent output; they stay in files under the run directory.
"""

import json
import os
import shutil
import signal
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import NamedTuple, Protocol, runtime_checkable

from . import agent, apierror, capacity, memwatch, permissions, podcontract
from .phases import AgentOptions, domains_for, phase_name, policy_for, settings_for
from .schemas import schema_for

_TOKEN = agent.TOKEN


@dataclass(frozen=True)
class AgentRequest:
    """What a caller of ``run_agent`` asks for (its arguments, unchanged, as one value)."""

    prompt: str
    cwd: Path | str
    prefix: Path | str
    phase: str
    timeout: float = 1800
    options: AgentOptions | None = None
    target: str | None = None
    guidance: str = ""
    read_paths: tuple = ()
    user: int | None = None
    group: int | None = None
    memory_limit_mb: int | None = None
    choice: dict | None = None


class SessionFile(NamedTuple):
    """A file the session needs: an absolute path, its bytes and the mode it is given."""

    path: Path
    data: bytes
    mode: int


@dataclass(frozen=True)
class LaunchSpec:
    """A plain description of one session that a backend can run or ship; it holds no secret.

    ``argv`` is the ``claude`` command line itself (the binary first); ``env`` holds the
    non-secret variables a worker needs besides what its environment inherits: the one secret,
    the Anthropic key, is added by the backend that starts the process. A frozen dataclass only
    stops attributes being rebound, so ``__post_init__`` also makes what the spec holds read-only:
    ``env`` becomes a read-only copy of the mapping given (a ``MappingProxyType``; ``dict(spec.env)``
    is the mutable copy to serialise) and ``argv``, ``files`` and ``read_paths`` become tuples.
    ``files`` are the run artifacts ``prepare`` wrote, as bytes plus the mode a backend that
    creates them elsewhere should give them (``0640`` when a separate worker identity reads them,
    ``0600`` otherwise; the prompt is never shared). ``write_scope`` is the phase policy's word:
    ``none`` means the session may not change the working tree, ``cwd`` that it may.
    ``resource_class`` is a class of ``podcontract.CLASSES``; a process backend ignores it. ``home``
    and ``config_dir`` are the throwaway ``HOME`` and ``CLAUDE_CONFIG_DIR`` of this one invocation.
    """

    phase: str
    model: str
    argv: tuple
    env: Mapping
    cwd: Path
    prefix: Path
    files: tuple
    read_paths: tuple
    prompt: str
    timeout: float
    user: int | None
    group: int | None
    write_scope: str
    resource_class: str
    memory_limit_mb: int | None
    home: Path
    config_dir: Path

    def __post_init__(self):
        # The only way to set a field of a frozen dataclass; the copies keep a caller's own dict or
        # list from changing the description after it was made.
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))
        for name in ("argv", "files", "read_paths"):
            object.__setattr__(self, name, tuple(getattr(self, name)))

    @property
    def separate_identity(self):
        return self.user is not None or self.group is not None


@dataclass(frozen=True)
class AgentHandle:
    """What ``on_start`` receives once a session runs: opaque, except for ``pid`` and ``pod``.

    ``pid`` is the process group id of a locally started worker, which is what the caller records
    to find and signal it again; a session that does not run in a local process has none. ``pod`` is
    the ``podhandle.PodHandle`` of a session that runs as a pod (the pod backend, ``podbackend.py``):
    what the caller records beside, never instead of, a pid. A handle has one of the two.
    """

    pid: int | None = None
    pod: object | None = None


@dataclass(frozen=True)
class Outcome:
    """What ``launch`` observed, as the facts ``collect`` needs (never agent text).

    ``exceeded`` is the memory guard's fixed message when it killed the session, else None.
    """

    returncode: int | None
    footprint: dict | None = None
    exceeded: str | None = None


@runtime_checkable
class AgentBackend(Protocol):
    """The three steps of a session. A second backend provides all three:

    * ``prepare`` raises (``ValueError`` for a bad phase or option, ``AgentError`` for a refused
      deployment) before it writes or starts anything, and returns a ``LaunchSpec``.
    * ``launch`` calls ``on_start(handle)`` at most once, after the session exists and before it is
      awaited; an exception from ``on_start`` ends the session. It leaves the transcript (stream-json
      lines) in ``<prefix>.jsonl`` and the error stream in ``<prefix>.stderr``, raises
      ``agent.AgentTimeout`` when the time limit is reached, and releases whatever it holds (a
      process, a pod, a directory) on every path, a raised exception included.
    * ``collect`` reads only the files under the prefix and the outcome; it returns the structured
      result and the usage record, or raises ``agent.AgentError`` (usage attached) or
      ``RuntimeError`` with the messages the callers handle.
    """

    def prepare(self, request): ...

    def launch(self, spec, on_start=None): ...

    def collect(self, spec, outcome): ...


def _api_code(code):
    """The code that an error carries: None for ``none`` (not an API error)."""
    return None if code == apierror.NONE else code


def _failure(event, prefix, usage):
    # Free-form agent text is never copied into the message: errors reach
    # stdout/stderr and state, and agent output stays in the log files.
    parts = [f"subtype={agent.subtype_of(event.get('subtype'))}"]
    if event.get("is_error") is True:
        parts.append("is_error=true")
    for key in ("terminal_reason", "api_error_status"):
        value = event.get(key)
        if isinstance(value, (str, int)) and _TOKEN.fullmatch(str(value)):
            parts.append(f"{key}={value}")
    return agent.AgentError(
        f"agent failed ({', '.join(parts)}); see {prefix}.jsonl and {prefix}.stderr",
        usage,
        _api_code(apierror.classify(event)),
    )


def _no_result(message, prefix):
    """The error of a session that left no result event: an ``AgentError`` only when its log shows an API error."""
    code = _api_code(apierror.classify(None, agent.log_tail(agent.sibling(prefix, ".jsonl"))))
    return RuntimeError(message) if code is None else agent.AgentError(message, None, code)


def _structured(event, prefix, usage):
    """The structured result of a successful ``result`` event, parsed from its text if need be."""
    structured = event.get("structured_output")
    if structured is not None:
        return structured
    result = event.get("result")
    if not isinstance(result, str):
        raise agent.AgentError(f"agent result has no structured output; see {prefix}.jsonl", usage)
    try:
        return agent.parse_json(result)
    except ValueError:
        raise agent.AgentError(f"agent result is not valid JSON; see {prefix}.jsonl", usage) from None


def _stop_group(process):
    """SIGTERM, five seconds of grace, then SIGKILL; the whole group is always killed last."""
    try:
        agent.signal_group(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        agent.signal_group(process.pid)
        process.wait()
    finally:
        # The parent may exit promptly while a descendant ignores TERM.
        # Always kill the remaining session group before forgetting its PID.
        agent.signal_group(process.pid)


def _await(process, spec, on_start):
    """Hand the prompt to the running worker and wait; kill its group on any failure."""
    try:
        if on_start:
            on_start(AgentHandle(pid=process.pid))
        process.communicate(spec.prompt.encode(), timeout=spec.timeout)
    except BaseException as caught:
        _stop_group(process)
        if isinstance(caught, subprocess.TimeoutExpired):
            raise agent.AgentTimeout(spec.timeout) from None
        raise


def _identity_kwargs(spec):
    if not spec.separate_identity:
        return {}
    return {
        "user": spec.user,
        "group": spec.group,
        # No supplementary groups: the worker is exactly (user, group).
        # umask 002 keeps files it creates writable by the supervisor's group.
        "extra_groups": [],
        "umask": permissions.WORKER_UMASK,
    }


class LocalBackend:
    """Runs the session as a child of this process, in its own process group."""

    def _require_identity(self, request):
        """Refuse a call without a worker identity when the image requires one (the pod backend has its own)."""
        if (
            request.user is None
            and request.group is None
            and os.environ.get(agent.REQUIRE_WORKER_IDENTITY) == "1"
        ):
            raise agent.AgentError(
                "this deployment requires a separate worker identity: set [worker] uid and gid "
                "in the repository config (the image defines uid 10002, gid 10010)"
            )

    def prepare(self, request):
        self._require_identity(request)
        memory_limit_mb = request.memory_limit_mb
        if memory_limit_mb is None:
            memory_limit_mb = capacity.worker_memory_limit_mb()
        options = request.options or AgentOptions()
        root = phase_name(request.phase)
        policy = policy_for(root, options, request.choice)
        domains = domains_for(policy, options, request.target)
        cwd = Path(request.cwd)
        # The child resolves --settings and --append-system-prompt-file against its
        # own cwd, so every path handed to it must be absolute.
        prefix = Path(request.prefix).resolve()
        prefix.parent.mkdir(parents=True, exist_ok=True)
        prompt_file = agent.sibling(prefix, ".prompt.txt")
        prompt_file.write_text(request.prompt)

        # The shared git dir is writable from a linked worktree unless denied.
        common = agent.git_common_dir(cwd)
        settings = agent.sibling(prefix, ".settings.json")
        document = settings_for(
            policy,
            cwd,
            domains,
            request.read_paths,
            (common,) if common else (),
            options.hidden_paths() if options.hidden_paths else (),
        )
        settings_text = f"{json.dumps(document, indent=2)}\n"
        settings.write_text(settings_text)
        system_prompt = agent.sibling(prefix, ".system.md")
        guidance = "\n\n".join(
            part.strip()
            for part in (request.guidance, options.addendum, agent.FACTORY_ADDENDUM)
            if part and part.strip()
        )
        system_text = f"{guidance}\n"
        system_prompt.write_text(system_text)
        argv = agent.build_argv(
            os.environ.get("FACTORY_AGENT_CMD", "claude"),
            policy,
            settings,
            system_prompt,
            schema_for(root),
        )

        # A throwaway HOME and config dir per invocation: no state, credential or
        # transcript survives the call, and invocations cannot read each other's.
        home = prefix.parent / f".home-{prefix.name}"
        shutil.rmtree(home, ignore_errors=True)
        config_dir = home / ".claude"
        config_dir.mkdir(parents=True, mode=0o700)
        separate_identity = request.user is not None or request.group is not None
        if separate_identity:
            agent.share_with_worker(
                prefix,
                home,
                config_dir,
                (settings, system_prompt, *map(Path, request.read_paths)),
                request.group,
            )
        shared_mode = 0o640 if separate_identity else 0o600
        files = (
            SessionFile(prompt_file, request.prompt.encode(), 0o600),
            SessionFile(settings, settings_text.encode(), shared_mode),
            SessionFile(system_prompt, system_text.encode(), shared_mode),
        )
        return LaunchSpec(
            phase=root,
            model=policy.model,
            argv=tuple(argv),
            env=agent.worker_env(home, config_dir),
            cwd=cwd,
            prefix=prefix,
            files=files,
            read_paths=tuple(request.read_paths),
            prompt=request.prompt,
            timeout=request.timeout,
            user=request.user,
            group=request.group,
            write_scope=policy.write_scope,
            resource_class=podcontract.DEFAULT_CLASS,
            memory_limit_mb=memory_limit_mb,
            home=home,
            config_dir=config_dir,
        )

    def launch(self, spec, on_start=None):
        command = list(spec.argv)
        if spec.separate_identity:
            # Popen(user=) keeps the supervisor's ambient capabilities (see execnocaps):
            # the worker must shed them before it runs anything.
            command = agent.drop_capabilities_first(command)
        process = None
        watch = None
        try:
            with (
                agent.sibling(spec.prefix, ".jsonl").open("w") as log,
                agent.sibling(spec.prefix, ".stderr").open("w") as err,
            ):
                process = subprocess.Popen(
                    command,
                    cwd=spec.cwd,
                    stdin=subprocess.PIPE,
                    stdout=log,
                    stderr=err,
                    start_new_session=True,
                    env=agent.child_env(spec.home, spec.config_dir),
                    **_identity_kwargs(spec),
                )
                agent.register_live(process.pid)
                pgid = process.pid
                watch = memwatch.start(
                    pgid, spec.memory_limit_mb, lambda sig: agent.signal_group(pgid, sig)
                )
                _await(process, spec, on_start)
            # Do not leave agent-spawned background processes running after completion.
            agent.signal_group(process.pid)
        finally:
            if watch is not None:
                watch.stop()
            if process is not None:
                agent.forget_live(process.pid)
            shutil.rmtree(spec.home, ignore_errors=True)
        exceeded = None
        if watch is not None and watch.exceeded is not None:
            exceeded = watch.exceeded_message()
        footprint = watch.footprint() if watch is not None else None
        return Outcome(process.returncode, footprint, exceeded)

    def collect(self, spec, outcome):
        prefix = spec.prefix
        event = agent.last_result(agent.sibling(prefix, ".jsonl"))
        if outcome.exceeded is not None:
            # Killed by the guard: the spend and the footprint stay accountable, the message
            # carries numbers only (never agent text).
            record = agent.usage_record(
                event or {"subtype": "memory_limit"}, spec.phase, spec.model, outcome.footprint
            )
            raise agent.AgentError(f"{outcome.exceeded}; see {prefix}.stderr", record)
        if event is None:
            if outcome.returncode:
                raise _no_result(f"agent exited {outcome.returncode}; see {prefix}.stderr", prefix)
            raise _no_result(f"agent produced no result event; see {prefix}.jsonl", prefix)
        usage = agent.usage_record(event, spec.phase, spec.model, outcome.footprint)
        # is_error can be true even when subtype is "success" (for example a
        # refusal or an authentication failure), so check both.
        if event.get("is_error") or event.get("subtype") != "success":
            raise _failure(event, prefix, usage)
        if outcome.returncode:
            raise agent.AgentError(
                f"agent exited {outcome.returncode} after a success result; see {prefix}.stderr",
                usage,
            )
        return _structured(event, prefix, usage), usage

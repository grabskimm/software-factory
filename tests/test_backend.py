"""The execution backend seam: golden captures of ``run_agent``, and the contract of its three steps.

Golden captures of what ``run_agent`` hands to a worker and what it returns.

The files under ``tests/golden/agent/`` were captured from ``run_agent`` as it was before the
execution backend seam existed (one function that prepared, launched and collected). The seam only
moves that code, so these captures must keep matching byte for byte: the argv, the environment, the
prompt, settings and system-prompt files and their modes, the permission calls for a separate worker
identity, the order of the launch steps (memory guard, ``on_start``, the TERM then KILL sequence,
the throwaway home removal) and the text of every error.

Nothing runs a real process: ``subprocess.Popen`` is a recording fake that writes the stream-json
lines of the scenario into the log the supervisor gave it, so a capture is the same on every host.
``python -m tests.test_backend --write-golden`` rewrites the files; a change to them is a change of
behaviour and belongs in a pull request of its own.

The classes after ``GoldenCaptureTests`` pin the seam itself: what ``prepare`` describes (a
``LaunchSpec`` that holds no secret), what ``launch`` releases on every path, what ``collect`` reads,
and the order and the arguments with which ``run_agent`` drives any ``AgentBackend`` (a fake one
that runs no process, and a subclass that reuses ``prepare`` and ``collect``). The last class shows
that the API-error code of slice 1 is attached in ``collect``, so such a subclass inherits it.
"""

import dataclasses
import inspect
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from collections.abc import MutableMapping
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from factory import agent, backend, podcontract
from factory.backend import (
    AgentBackend,
    AgentHandle,
    AgentRequest,
    LaunchSpec,
    LocalBackend,
    Outcome,
    SessionFile,
)
from factory.phases import AgentOptions, phase_name
from factory.schemas import schema_for

from tests import agentstub

GOLDEN = Path(__file__).with_name("golden") / "agent"

PROMPT = "Fix the bug.\nKeep the change small.\n"
GUIDANCE = "Repo CLAUDE.md text."

# The only variables a capture sees: the allow-list inputs, decoys that must never reach the
# worker, and one synthetic secret that must (the key every worker holds today).
BASE_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "SHELL": "/bin/sh",
    "TMPDIR": "/tmp",
    "LANG": "C.UTF-8",
    "TERM": "xterm",
    "NO_COLOR": "1",
    "FACTORY_AGENT_CMD": "/opt/fake/claude",
    "ANTHROPIC_API_KEY": "synthetic-anthropic-value",
    "GH_TOKEN": "synthetic-gh-value",
    "AZURE_CLIENT_SECRET": "synthetic",
    "CLAUDE_CODE_EFFORT_LEVEL": "max",
    "HOME": "/home/supervisor",
}


def result_event(**overrides):
    event = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "",
        "session_id": "stub",
        "num_turns": 2,
        "duration_ms": 25,
        "total_cost_usd": 0.0125,
        "usage": {"input_tokens": 12, "output_tokens": 5},
        "modelUsage": {"stub-model": {"inputTokens": 12, "outputTokens": 5, "costUSD": 0.0125}},
        "structured_output": {"ok": True},
    }
    event.update(overrides)
    return event


INIT = {"type": "system", "subtype": "init", "session_id": "stub", "tools": []}
SUCCESS_LOG = [INIT, result_event()]


def scenario(phase="build-0", **fields):
    return {"phase": phase, **fields}


def standard_options(root):
    (root / "other-repo").mkdir()
    return AgentOptions(
        verify_domains=("registry.npmjs.org", "PyPI.org"),
        addendum="Operator addendum.",
        hidden_paths=lambda: (root / "other-repo",),
    )


def standard(root, phase="build-0", **fields):
    """A scenario with the options, guidance and memory limit every phase capture uses."""
    args = {"options": standard_options(root), "guidance": GUIDANCE, "memory_limit_mb": 2048}
    args.update(fields.pop("args", {}))
    fields.setdefault("log", SUCCESS_LOG)
    return scenario(phase, args=args, **fields)


def plan_scenario(root):
    evidence = root / "evidence.json"
    evidence.write_text("[]")
    return standard(root, "plan", args={"read_paths": (evidence,)})


def preflight_scenario(target):
    return lambda root: standard(root, "preflight", args={"target": target})


def with_common_dir(root):
    found = standard(root, "build-0")
    found["common_dir"] = root / "main" / ".git"
    return found


def with_choice(root):
    return standard(root, "build-0", args={"choice": {"model": "claude-test-model", "effort": "low"}})


def with_limit_from_environment(root):
    found = standard(root, "review-0")
    found["args"]["memory_limit_mb"] = None
    found["limit_from_capacity"] = 4096
    return found


def identity_scenario(root):
    evidence = root / "evidence.json"
    evidence.write_text("[]")
    return standard(root, "plan", args={"user": 20002, "group": 20010, "read_paths": (evidence,)})


def identity_build(root):
    return standard(root, "build-0", args={"user": 20002, "group": 20010})


def group_only(root):
    return standard(root, "verify", args={"group": 20010})


def failing(**fields):
    return lambda root: standard(root, "build-0", log=[INIT, result_event(**fields)])


def plain(**fields):
    return lambda root: standard(root, "build-0", **fields)


def required_identity(root):
    return standard(root, "build-0", env={"FACTORY_REQUIRE_WORKER_IDENTITY": "1"})


def bad_budget(root):
    return standard(root, "build-0", args={"options": AgentOptions(budgets={"build": 0})})


def unknown_phase(root):
    return standard(root, "deploy")


def bad_domain(root):
    return standard(root, "verify", args={"options": AgentOptions(verify_domains=("exa mple.com",))})


CASES = {
    "phases": {
        "plan": plan_scenario,
        "build-0": lambda root: standard(root, "build-0"),
        "review-1": lambda root: standard(root, "review-1"),
        "verify": lambda root: standard(root, "verify"),
        "preflight-build": preflight_scenario("build"),
        "preflight-verify": preflight_scenario("verify"),
        "linked-worktree-git-dir": with_common_dir,
        "model-and-effort-choice": with_choice,
        "memory-limit-from-environment": with_limit_from_environment,
    },
    "identity": {
        "plan-as-worker": identity_scenario,
        "build-as-worker": identity_build,
        "group-only": group_only,
    },
    "outcomes": {
        "structured-output": plain(log=SUCCESS_LOG),
        "fenced-text-result": plain(
            log=[INIT, result_event(structured_output=None, result='```json\n{"pass": false}\n```')]
        ),
        "snake-case-model-usage": plain(
            log=[INIT, result_event(modelUsage=None, model_usage={"other": {}})]
        ),
        "last-result-wins-and-garbage-is-skipped": plain(
            log=[
                "not json",
                result_event(structured_output={"v": "first"}),
                "{broken",
                result_event(structured_output={"v": "last"}),
            ]
        ),
        "is-error-with-a-success-subtype": failing(
            is_error=True, result="SECRET-AGENT-TEXT", terminal_reason="api_error", api_error_status=429
        ),
        "unsafe-terminal-reason-is-not-copied": failing(
            is_error=True, terminal_reason="has a space and SECRET", api_error_status="x" * 70
        ),
        "budget-exhausted": failing(subtype="error_max_budget_usd", is_error=True, total_cost_usd=8.2),
        "non-success-subtype": failing(subtype="error_max_turns", is_error=False),
        "no-result-and-a-nonzero-exit": plain(log=[INIT], returncode=3),
        "no-result-and-a-clean-exit": plain(log=[INIT]),
        "nonzero-exit-after-a-success-result": plain(log=SUCCESS_LOG, returncode=2),
        "result-without-structured-output": plain(
            log=[INIT, result_event(structured_output=None, result=5)]
        ),
        "result-that-is-not-json": plain(
            log=[INIT, result_event(structured_output=None, result="definitely not json PRIVATE")]
        ),
        "unclosed-fence": plain(log=[INIT, result_event(structured_output=None, result="```")]),
        "memory-guard-footprint": plain(log=SUCCESS_LOG, watch="footprint"),
        "memory-guard-unavailable": plain(log=SUCCESS_LOG, watch="unavailable"),
        "memory-limit-exceeded": plain(log=[INIT], watch="exceeded"),
        "memory-limit-exceeded-with-a-result": plain(
            log=[INIT, result_event(subtype="error_max_turns", total_cost_usd=3.5)], watch="exceeded"
        ),
        "worker-identity-required": required_identity,
        "unknown-phase": unknown_phase,
        "invalid-budget": bad_budget,
        "invalid-domain": bad_domain,
    },
    "process": {
        "normal-run": plain(log=SUCCESS_LOG),
        "timeout": plain(log=[INIT], behaviour="timeout", timeout=7),
        "timeout-of-a-worker-that-ignores-term": plain(
            log=[INIT], behaviour="timeout-ignores-term", timeout=7
        ),
        "interrupt-during-the-wait": plain(log=[INIT], behaviour="interrupt"),
        "on-start-raises": plain(log=[INIT], on_start="raises"),
        "on-start-not-given": plain(log=SUCCESS_LOG, on_start=None),
    },
}


def mode_of(path):
    return oct(stat.S_IMODE(Path(path).lstat().st_mode))


def snapshot(run_dir):
    """Every entry of the run directory with its mode and, for a file, its text split in lines."""
    entries = {".": {"mode": mode_of(run_dir)}}
    for path in sorted(run_dir.rglob("*")):
        entry = {"mode": mode_of(path)}
        if path.is_file():
            entry["lines"] = path.read_text().split("\n")
        entries[path.relative_to(run_dir).as_posix()] = entry
    return entries


class Recorder:
    def __init__(self):
        self.events = []
        self.popen = None
        self.stdin = None
        self.files = None
        self.chown = []
        self.on_exceed = None
        self.pids = []
        self.watch = None


class FakeWatch:
    """What ``memwatch.start`` returns: fixed numbers, no thread, nothing read from /proc."""

    def __init__(self, recorder, kind):
        self.recorder = recorder
        self.exceeded = 500 * 1024 * 1024 if kind == "exceeded" else None
        self._footprint = {"peak_rss_mb": 50.0, "cpu_seconds": 1.5, "samples": 3, "limit_mb": 2048}

    def stop(self):
        self.recorder.events.append("watch.stop")

    def footprint(self):
        return self._footprint

    def exceeded_message(self):
        return "worker exceeded its memory limit: peak 500 MiB over 100 MiB"


def make_popen(recorder, found, run_dir):
    behaviour = found.get("behaviour", "ok")
    log_lines = found.get("log", [])

    class FakePopen:
        pid = 4242
        returncode = found.get("returncode", 0)

        def __init__(self, args, **kwargs):
            recorder.events.append("popen")
            recorded = dict(kwargs)
            recorded["stdin"] = "PIPE" if kwargs["stdin"] == subprocess.PIPE else repr(kwargs["stdin"])
            self._log = kwargs["stdout"]
            for name in ("stdout", "stderr"):
                handle = kwargs[name]
                recorded[name] = f"{handle.name} ({handle.mode})"
            recorded["cwd"] = str(kwargs["cwd"])
            recorder.popen = {"argv": list(args), "kwargs": recorded}
            recorder.files = snapshot(run_dir)

        def communicate(self, data, timeout=None):
            recorder.events.append(f"communicate timeout={timeout!r}")
            recorder.stdin = data.decode()
            if behaviour in ("timeout", "timeout-ignores-term"):
                raise subprocess.TimeoutExpired("claude", timeout)
            if behaviour == "interrupt":
                raise KeyboardInterrupt
            if found.get("watch") == "exceeded":
                recorder.on_exceed(signal.SIGKILL)
            body = "\n".join(json.dumps(line) if isinstance(line, dict) else line for line in log_lines)
            self._log.write(f"{body}\n" if body else "")
            return (b"", b"")

        def wait(self, timeout=None):
            recorder.events.append(f"wait timeout={timeout!r}")
            if behaviour == "timeout-ignores-term" and timeout is not None:
                raise subprocess.TimeoutExpired("claude", timeout)
            return self.returncode

    return FakePopen


# Groups whose captures keep the whole command line and the files present when the worker starts;
# the others (many scenarios that differ only in the log) keep the steps and the outcome.
FULL_GROUPS = ("phases", "identity")


def schema_placeholder(argv, phase):
    """The JSON schema is a policy table of its own (test_agent pins it): compare it, do not store it."""
    if "--json-schema" not in argv:
        return argv
    index = argv.index("--json-schema") + 1
    expected = json.dumps(schema_for(phase_name(phase)), sort_keys=True, separators=(",", ":"))
    if argv[index] != expected:
        return argv
    return [*argv[:index], "<schema_for the phase>", *argv[index + 1 :]]


def normaliser(root):
    replacements = (
        (str(root), "<root>"),
        (str(Path(agent.__file__).parent), "<factory>"),
        (sys.executable, "<python>"),
    )

    def norm(value):
        if isinstance(value, str):
            for old, new in replacements:
                value = value.replace(old, new)
            return value
        if isinstance(value, dict):
            return {norm(key): norm(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [norm(item) for item in value]
        return value

    return norm


def capture(base, group, name, build):
    """Run one scenario under the recording fakes and return everything observable about it."""
    root = (base / name).resolve()
    (root / "work").mkdir(parents=True)
    found = build(root)
    recorder = Recorder()
    run_dir = root / "run"

    def killpg(group, sig):
        recorder.events.append(f"killpg {group} {signal.Signals(sig).name}")

    def chown(path, uid, gid):
        recorder.chown.append([str(path), uid, gid])

    def fake_start(pid, limit_mb, on_exceed, **kwargs):
        recorder.events.append(f"memwatch.start pid={pid} limit={limit_mb!r}")
        recorder.on_exceed = on_exceed
        if found.get("watch") == "unavailable":
            return None
        recorder.watch = FakeWatch(recorder, found.get("watch"))
        return recorder.watch

    def on_start(pid):
        recorder.events.append(f"on_start {pid}")
        if found.get("on_start") == "raises":
            raise RuntimeError("on_start failed")

    env = {**BASE_ENV, **found.get("env", {})}
    previous = os.umask(0o077)
    try:
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, env, clear=True))
            stack.enter_context(patch("factory.agent.git_common_dir", return_value=found.get("common_dir")))
            stack.enter_context(patch("factory.agent.subprocess.Popen", make_popen(recorder, found, run_dir)))
            stack.enter_context(patch("factory.agent.os.killpg", side_effect=killpg))
            stack.enter_context(patch("factory.agent.permissions.os.chown", side_effect=chown))
            stack.enter_context(patch("factory.memwatch.start", side_effect=fake_start))
            if "limit_from_capacity" in found:
                stack.enter_context(
                    patch("factory.capacity.worker_memory_limit_mb", return_value=found["limit_from_capacity"])
                )
            callback = None if found.get("on_start", "record") is None else on_start
            outcome = {}
            try:
                result, usage = agent.run_agent(
                    PROMPT,
                    root / "work",
                    run_dir / f"0-{found['phase']}",
                    found["phase"],
                    found.get("timeout", 1800),
                    callback,
                    **found.get("args", {}),
                )
                outcome["returned"] = {"result": result, "usage": usage}
            except (Exception, KeyboardInterrupt) as error:
                outcome["raised"] = {
                    "type": type(error).__name__,
                    "message": str(error),
                    "usage": getattr(error, "usage", None),
                }
    finally:
        os.umask(previous)
    captured = {
        "events": recorder.events,
        "chown": recorder.chown,
        "outcome": outcome,
        "run_directory_afterwards": sorted(path.name for path in run_dir.glob("*")) if run_dir.exists() else None,
    }
    if group in FULL_GROUPS:
        popen = {**recorder.popen, "argv": schema_placeholder(recorder.popen["argv"], found["phase"])}
        captured.update(popen=popen, stdin=recorder.stdin, files_when_the_worker_starts=recorder.files)
    return normaliser(root)(captured)


def capture_group(group):
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        return {name: capture(base, group, name, build) for name, build in CASES[group].items()}


def write_golden():
    GOLDEN.mkdir(parents=True, exist_ok=True)
    for group in CASES:
        text = json.dumps(capture_group(group), indent=2, sort_keys=True)
        (GOLDEN / f"{group}.json").write_text(f"{text}\n")


class GoldenCaptureTests(unittest.TestCase):
    maxDiff = None

    def check(self, group):
        golden = json.loads((GOLDEN / f"{group}.json").read_text())
        # A JSON round trip so tuples and Paths compare the way the stored file does.
        captured = json.loads(json.dumps(capture_group(group), sort_keys=True))
        self.assertEqual(sorted(captured), sorted(golden))
        for name in golden:
            with self.subTest(case=name):
                self.assertEqual(captured[name], golden[name])

    def test_the_golden_directory_holds_exactly_the_captured_groups(self):
        self.assertEqual(sorted(path.name for path in GOLDEN.glob("*")), [f"{group}.json" for group in sorted(CASES)])

    def test_argv_env_and_files_of_every_phase(self):
        self.check("phases")

    def test_a_separate_worker_identity(self):
        self.check("identity")

    def test_results_and_error_texts(self):
        self.check("outcomes")

    def test_the_launch_sequence_and_the_kill_sequence(self):
        self.check("process")


SECRET_LIKE = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH", re.IGNORECASE)
SECRET_VALUE = "synthetic-anthropic-value"
WRITE_SCOPES = {"plan": "none", "build-0": "cwd", "review-0": "none", "verify": "cwd", "preflight": "cwd"}


class SeamTestCase(unittest.TestCase):
    """A temporary deployment: a working tree, a run directory and a fake agent executable."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.cwd = self.root / "work"
        self.cwd.mkdir()
        self.run_dir = self.root / "run"
        self.binary = self.root / "bin"
        self.binary.mkdir()
        for patcher in (
            patch.dict(
                os.environ,
                {"FACTORY_AGENT_CMD": str(self.binary / "fake-agent"), "ANTHROPIC_API_KEY": SECRET_VALUE},
            ),
            patch("factory.agent.git_common_dir", return_value=None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def stub(self, body):
        path = self.binary / "fake-agent"
        path.write_text(f"#!{sys.executable}\n{agentstub.source(body)}")
        path.chmod(0o755)

    def request(self, phase="build-0", **kwargs):
        kwargs.setdefault("memory_limit_mb", 2048)
        return AgentRequest("do the thing", self.cwd, self.run_dir / f"0-{phase}", phase, **kwargs)

    def prefix(self, phase="build-0"):
        return self.run_dir / f"0-{phase}"


class LaunchSpecTests(SeamTestCase):
    def test_prepare_describes_the_session_and_starts_nothing(self):
        spec = LocalBackend().prepare(self.request("build-0"))
        self.assertIsInstance(spec, LaunchSpec)
        self.assertEqual((spec.phase, spec.model), ("build", "claude-sonnet-5-5"))
        self.assertEqual((spec.cwd, spec.prefix, spec.timeout), (self.cwd, self.prefix(), 1800))
        self.assertEqual(spec.argv[0], str(self.binary / "fake-agent"))
        self.assertEqual((spec.user, spec.group, spec.memory_limit_mb), (None, None, 2048))
        # The run artifacts and the throwaway home exist; no transcript, so nothing was started.
        self.assertEqual(
            sorted(path.name for path in self.run_dir.iterdir()),
            [".home-0-build-0", "0-build-0.prompt.txt", "0-build-0.settings.json", "0-build-0.system.md"],
        )
        self.assertEqual(spec.home, self.run_dir / ".home-0-build-0")
        self.assertEqual(spec.config_dir, spec.home / ".claude")

    def test_the_files_are_absolute_paths_with_the_bytes_that_were_written(self):
        spec = LocalBackend().prepare(self.request("build-0"))
        self.assertEqual(
            [file.path.name for file in spec.files],
            ["0-build-0.prompt.txt", "0-build-0.settings.json", "0-build-0.system.md"],
        )
        for file in spec.files:
            with self.subTest(file=file.path.name):
                self.assertIsInstance(file, SessionFile)
                self.assertTrue(file.path.is_absolute())
                self.assertEqual(file.path.read_bytes(), file.data)
                self.assertEqual(file.mode, 0o600)
        argv = list(spec.argv)
        self.assertEqual(argv[argv.index("--settings") + 1], str(spec.files[1].path))
        self.assertEqual(argv[argv.index("--append-system-prompt-file") + 1], str(spec.files[2].path))
        self.assertEqual(spec.files[0].data, b"do the thing")

    def test_a_separate_worker_identity_makes_settings_and_system_prompt_group_readable(self):
        evidence = self.root / "evidence.json"
        evidence.write_text("[]")
        with patch("factory.agent.permissions.os.chown"):
            spec = LocalBackend().prepare(self.request("plan", user=20002, group=20010, read_paths=(evidence,)))
        self.assertEqual([file.mode for file in spec.files], [0o600, 0o640, 0o640])
        self.assertEqual(stat.S_IMODE(spec.files[1].path.stat().st_mode), 0o640)
        self.assertEqual((spec.user, spec.group, spec.read_paths), (20002, 20010, (evidence,)))
        self.assertTrue(spec.separate_identity)
        # The capability wrapper is the local backend's launch detail, not part of the description.
        self.assertEqual(spec.argv[0], str(self.binary / "fake-agent"))

    def test_the_write_scope_and_resource_class_follow_the_policy_and_the_contract(self):
        for phase, scope in WRITE_SCOPES.items():
            with self.subTest(phase=phase):
                spec = LocalBackend().prepare(self.request(phase))
                self.assertEqual(spec.write_scope, scope)
                self.assertIn(spec.phase, podcontract.PHASES)
                self.assertEqual(spec.phase, phase_name(phase))
                self.assertEqual(spec.resource_class, podcontract.DEFAULT_CLASS)
                self.assertIn(spec.resource_class, podcontract.CLASSES)

    def test_the_description_holds_no_secret(self):
        spec = LocalBackend().prepare(self.request("verify"))
        self.assertNotIn(SECRET_VALUE, repr(spec))
        self.assertEqual([name for name in spec.env if SECRET_LIKE.search(name)], [])
        for file in spec.files:
            self.assertNotIn(SECRET_VALUE.encode(), file.data)
        # What it does hold is exactly what reaches the child besides the allow-list and the key.
        child = agent.child_env(spec.home, spec.config_dir)
        self.assertEqual({name: child[name] for name in spec.env}, spec.env)
        self.assertEqual(child["ANTHROPIC_API_KEY"], SECRET_VALUE)

    def test_a_launch_spec_cannot_be_changed_after_prepare(self):
        spec = LocalBackend().prepare(self.request())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            spec.phase = "plan"

    def test_the_environment_of_a_launch_spec_is_read_only(self):
        spec = LocalBackend().prepare(self.request())
        before = dict(spec.env)
        self.assertNotIsInstance(spec.env, MutableMapping)
        with self.assertRaises(TypeError):
            spec.env["ANTHROPIC_API_KEY"] = "x"
        with self.assertRaises(TypeError):
            del spec.env["HOME"]
        self.assertEqual(dict(spec.env), before)
        self.assertNotIn("ANTHROPIC_API_KEY", spec.env)

    def test_a_launch_spec_keeps_its_own_copy_of_what_it_was_given(self):
        # A second backend builds a spec itself (or a test with dataclasses.replace): the lists
        # and the dict it passes in must not stay reachable through the description.
        spec = LocalBackend().prepare(self.request())
        env = {"HOME": "/h", "CLAUDE_CONFIG_DIR": "/h/.claude"}
        argv = ["claude", "-p"]
        files = [SessionFile(spec.prefix, b"x", 0o600)]
        read_paths = [self.root]
        other = dataclasses.replace(spec, env=env, argv=argv, files=files, read_paths=read_paths)
        env["ANTHROPIC_API_KEY"] = "x"
        argv.append("--extra")
        files.clear()
        read_paths.clear()
        self.assertEqual(dict(other.env), {"HOME": "/h", "CLAUDE_CONFIG_DIR": "/h/.claude"})
        self.assertEqual(other.argv, ("claude", "-p"))
        self.assertEqual(other.files, (SessionFile(spec.prefix, b"x", 0o600),))
        self.assertEqual(other.read_paths, (self.root,))
        # What prepare produced is already in that form, so a copy of the spec equals it.
        for name in ("argv", "files", "read_paths"):
            with self.subTest(name=name):
                self.assertIs(type(getattr(spec, name)), tuple)
        self.assertEqual(dataclasses.replace(spec), spec)

    def test_the_memory_limit_is_decided_in_prepare(self):
        with patch("factory.capacity.worker_memory_limit_mb", return_value=123) as limit:
            self.assertEqual(LocalBackend().prepare(self.request("plan", memory_limit_mb=None)).memory_limit_mb, 123)
            limit.assert_called_once_with()
            # 0 means "no limit" and is not replaced by the environment's.
            self.assertEqual(LocalBackend().prepare(self.request("plan", memory_limit_mb=0)).memory_limit_mb, 0)
            limit.assert_called_once_with()

    def test_prepare_refuses_before_it_writes_anything(self):
        with (
            patch.dict(os.environ, {"FACTORY_REQUIRE_WORKER_IDENTITY": "1"}),
            self.assertRaisesRegex(agent.AgentError, "separate worker identity"),
        ):
            LocalBackend().prepare(self.request())
        with self.assertRaises(ValueError):
            LocalBackend().prepare(self.request("deploy"))
        self.assertFalse(self.run_dir.exists())


class CollectTests(SeamTestCase):
    def collect(self, lines, outcome):
        backend = LocalBackend()
        spec = backend.prepare(self.request("review-0"))
        body = "\n".join(json.dumps(line) for line in lines)
        agent.sibling(spec.prefix, ".jsonl").write_text(f"{body}\n")
        return spec, backend.collect(spec, outcome)

    def test_collect_reads_only_the_transcript_and_the_outcome(self):
        spec, (result, usage) = self.collect([result_event(structured_output={"pass": True})], Outcome(0))
        self.assertEqual(result, {"pass": True})
        self.assertEqual((usage["phase"], usage["model"], usage["total_cost_usd"]), ("review", spec.model, 0.0125))
        self.assertNotIn("footprint", usage)

    def test_the_footprint_of_the_outcome_goes_into_the_usage_record(self):
        footprint = {"peak_rss_mb": 7.0, "cpu_seconds": 0.5, "samples": 2, "limit_mb": 2048}
        _, (_, usage) = self.collect([result_event()], Outcome(0, footprint))
        self.assertEqual(usage["footprint"], footprint)

    def test_an_outcome_that_exceeded_the_memory_limit_fails_with_its_message_and_keeps_the_spend(self):
        outcome = Outcome(-9, {"peak_rss_mb": 5.0}, "worker exceeded its memory limit: peak 5 MiB over 1 MiB")
        with self.assertRaises(agent.AgentError) as caught:
            self.collect([result_event(total_cost_usd=2.5)], outcome)
        self.assertEqual(
            str(caught.exception),
            f"worker exceeded its memory limit: peak 5 MiB over 1 MiB; see {self.prefix('review-0')}.stderr",
        )
        self.assertEqual(caught.exception.usage["total_cost_usd"], 2.5)
        self.assertEqual(caught.exception.usage["footprint"], {"peak_rss_mb": 5.0})

    def test_the_exit_code_of_the_outcome_decides_the_missing_result_and_the_late_failure(self):
        with self.assertRaisesRegex(RuntimeError, r"agent exited 3; see .*0-review-0\.stderr"):
            self.collect([], Outcome(3))
        with self.assertRaisesRegex(RuntimeError, "no result event"):
            self.collect([], Outcome(0))
        with self.assertRaisesRegex(agent.AgentError, "agent exited 2 after a success result"):
            self.collect([result_event()], Outcome(2))


class LocalLaunchTests(SeamTestCase):
    def prepared(self, body, phase="build-0", **kwargs):
        self.stub(body)
        backend = LocalBackend()
        return backend, backend.prepare(self.request(phase, **kwargs))

    def test_on_start_receives_a_handle_whose_pid_is_the_live_process_group(self):
        backend, spec = self.prepared("sys.stdin.read()\nemit({'v': 1})")
        seen = {}

        def on_start(handle):
            seen["handle"] = handle
            seen["group"] = os.getpgid(handle.pid)
            seen["live"] = handle.pid in agent._LIVE
            seen["home"] = spec.home.is_dir()

        outcome = backend.launch(spec, on_start)
        handle = seen["handle"]
        self.assertIsInstance(handle, AgentHandle)
        self.assertEqual(seen["group"], handle.pid)
        self.assertTrue(seen["live"] and seen["home"])
        # Released on the way out: not live, no throwaway home, the artifacts stay.
        self.assertNotIn(handle.pid, agent._LIVE)
        self.assertFalse(spec.home.exists())
        self.assertTrue(all(file.path.exists() for file in spec.files))
        self.assertEqual((outcome.returncode, outcome.exceeded), (0, None))
        self.assertEqual(backend.collect(spec, outcome)[0], {"v": 1})

    def test_the_exit_code_is_the_outcome_and_collect_reports_it(self):
        backend, spec = self.prepared("sys.stdin.read()\nsys.exit(3)")
        outcome = backend.launch(spec)
        self.assertEqual(outcome.returncode, 3)
        with self.assertRaisesRegex(RuntimeError, "agent exited 3"):
            backend.collect(spec, outcome)

    def test_the_time_limit_of_the_spec_kills_the_session_and_releases_everything(self):
        backend, spec = self.prepared("sys.stdin.read()\nimport time\ntime.sleep(30)")
        with self.assertRaises(agent.AgentTimeout) as caught:
            backend.launch(dataclasses.replace(spec, timeout=0.5))
        self.assertEqual(caught.exception.timeout, 0.5)
        self.assertEqual(agent._LIVE, set())
        self.assertFalse(spec.home.exists())

    def test_the_memory_guard_signals_the_worker_group_with_the_signal_it_is_given(self):
        # memwatch sends SIGSTOP first (to freeze the group before it sweeps the tree) and SIGKILL
        # last; a callback that dropped the signal would kill the group before the sweep.
        backend, spec = self.prepared("sys.stdin.read()\nemit({'v': 1})")
        recorder = Recorder()
        sent = []
        seen = {}

        def start(pid, limit_mb, on_exceed, **kwargs):
            seen["start"] = (pid, limit_mb)
            seen["on_exceed"] = on_exceed
            return FakeWatch(recorder, None)

        def on_start(handle):
            seen["pid"] = handle.pid
            seen["on_exceed"](signal.SIGSTOP)
            seen["on_exceed"](signal.SIGKILL)
            seen["sent"] = list(sent)

        with (
            patch("factory.memwatch.start", side_effect=start),
            patch("factory.agent.os.killpg", side_effect=lambda group, sig: sent.append((group, sig))),
        ):
            backend.launch(spec, on_start)
        # The guard watches the worker's own process group, and signals exactly that group.
        self.assertEqual(seen["start"], (seen["pid"], 2048))
        self.assertEqual(seen["sent"], [(seen["pid"], signal.SIGSTOP), (seen["pid"], signal.SIGKILL)])

    def test_an_error_from_on_start_ends_the_session(self):
        backend, spec = self.prepared("sys.stdin.read()\nimport time\ntime.sleep(30)")
        pids = []

        def on_start(handle):
            pids.append(handle.pid)
            raise RuntimeError("could not record the pid")

        with self.assertRaisesRegex(RuntimeError, "could not record"):
            backend.launch(spec, on_start)
        self.assertEqual(agent._LIVE, set())
        with self.assertRaises(ProcessLookupError):
            os.killpg(pids[0], 0)


class FakeBackend:
    """A backend that records its calls and runs no process, to show the seam needs none."""

    def __init__(self, handle=None, launch_error=None):
        self.calls = []
        self.handle = AgentHandle(pid=77) if handle is None else handle
        self.launch_error = launch_error

    def prepare(self, request):
        self.calls.append(("prepare", request))
        return "spec"

    def launch(self, spec, on_start=None):
        self.calls.append(("launch", spec, on_start))
        if on_start is not None:
            on_start(self.handle)
        if self.launch_error:
            raise self.launch_error
        return "outcome"

    def collect(self, spec, outcome):
        self.calls.append(("collect", spec, outcome))
        return {"v": 1}, {"phase": "build"}


class RunAgentSeamTests(SeamTestCase):
    def run_with(self, backend, **kwargs):
        return agent.run_agent("p", self.cwd, self.prefix(), "build-0", backend=backend, **kwargs)

    def test_run_agent_prepares_then_launches_then_collects_and_returns_what_collect_returns(self):
        backend = FakeBackend()
        self.assertEqual(self.run_with(backend), ({"v": 1}, {"phase": "build"}))
        self.assertEqual([call[0] for call in backend.calls], ["prepare", "launch", "collect"])
        self.assertEqual(backend.calls[1][1], "spec")
        self.assertEqual(backend.calls[2][1:], ("spec", "outcome"))

    def test_every_argument_reaches_the_request(self):
        options = AgentOptions(addendum="x")
        backend = FakeBackend()
        agent.run_agent(
            "the prompt",
            self.cwd,
            self.prefix(),
            "review-2",
            45,
            None,
            options,
            "build",
            "guidance",
            (self.root / "a",),
            10,
            11,
            512,
            {"model": "m"},
            backend,
        )
        request = backend.calls[0][1]
        self.assertEqual(
            request,
            AgentRequest(
                prompt="the prompt",
                cwd=self.cwd,
                prefix=self.prefix(),
                phase="review-2",
                timeout=45,
                options=options,
                target="build",
                guidance="guidance",
                read_paths=(self.root / "a",),
                user=10,
                group=11,
                memory_limit_mb=512,
                choice={"model": "m"},
            ),
        )

    def test_the_callers_on_start_gets_the_pid_of_the_handle_not_the_handle(self):
        pids = []
        self.run_with(FakeBackend(), on_start=pids.append)
        self.assertEqual(pids, [77])

    def test_a_handle_without_a_process_group_records_no_pid(self):
        pids = []
        self.run_with(FakeBackend(handle=AgentHandle()), on_start=pids.append)
        self.assertEqual(pids, [])

    def test_without_an_on_start_the_backend_gets_none(self):
        backend = FakeBackend()
        self.run_with(backend)
        self.assertIsNone(backend.calls[1][2])

    def test_a_failed_prepare_or_launch_skips_the_later_steps_and_propagates_unchanged(self):
        error = RuntimeError("boom")
        backend = FakeBackend(launch_error=error)
        with self.assertRaises(RuntimeError) as caught:
            self.run_with(backend)
        self.assertIs(caught.exception, error)
        self.assertEqual([call[0] for call in backend.calls], ["prepare", "launch"])

        class Refusing(FakeBackend):
            def prepare(self, request):
                self.calls.append(("prepare", request))
                raise ValueError("no")

        refusing = Refusing()
        with self.assertRaises(ValueError):
            self.run_with(refusing)
        self.assertEqual([call[0] for call in refusing.calls], ["prepare"])

    def test_without_a_backend_the_local_backend_runs_the_session(self):
        with patch("factory.backend.LocalBackend", return_value=FakeBackend()) as local:
            agent.run_agent("p", self.cwd, self.prefix(), "build-0")
        local.assert_called_once_with()

    def test_a_second_backend_can_reuse_prepare_and_collect_and_run_no_process(self):
        class InMemory(LocalBackend):
            def launch(self, spec, on_start=None):
                if on_start is not None:
                    on_start(AgentHandle())
                body = json.dumps(result_event(structured_output={"ran": "elsewhere"}))
                agent.sibling(spec.prefix, ".jsonl").write_text(f"{body}\n")
                agent.sibling(spec.prefix, ".stderr").write_text("")
                return Outcome(0)

        result, usage = self.run_with(InMemory())
        self.assertEqual(result, {"ran": "elsewhere"})
        self.assertEqual(usage["phase"], "build")
        self.assertEqual(agent._LIVE, set())

    def test_the_protocol_is_satisfied_by_the_local_backend_and_not_by_half_a_backend(self):
        self.assertIsInstance(LocalBackend(), AgentBackend)
        self.assertIsInstance(FakeBackend(), AgentBackend)
        self.assertNotIsInstance(object(), AgentBackend)

        class NoCollect:
            def prepare(self, request):
                return None

            def launch(self, spec, on_start=None):
                return None

        self.assertNotIsInstance(NoCollect(), AgentBackend)

    def test_the_signature_the_three_callers_use_is_unchanged(self):
        names = list(inspect.signature(agent.run_agent).parameters)
        self.assertEqual(
            names,
            [
                "prompt", "cwd", "prefix", "phase", "timeout", "on_start", "options", "target",
                "guidance", "read_paths", "user", "group", "memory_limit_mb", "choice", "backend",
            ],
        )
        self.assertIsNone(inspect.signature(agent.run_agent).parameters["backend"].default)


class TranscriptBackend(LocalBackend):
    """A second backend that runs no process: ``launch`` leaves the given stream-json lines and exit code.

    It inherits ``prepare`` and ``collect``, so whatever it raises for a failed session is what the
    seam's ``collect`` makes of the transcript: the classification of an API error is part of it.
    """

    def __init__(self, lines, returncode=0):
        self.lines = lines
        self.returncode = returncode

    def launch(self, spec, on_start=None):
        body = "".join(f"{json.dumps(line)}\n" for line in self.lines)
        agent.sibling(spec.prefix, ".jsonl").write_text(body)
        agent.sibling(spec.prefix, ".stderr").write_text("")
        return Outcome(self.returncode)


SPEND_LIMIT_TEXT = "API Error: 429 enforced_spend_limit_reached POISON-agent-text"
SPEND_LIMIT_RESULT = result_event(
    subtype="success",
    is_error=True,
    api_error_status=429,
    terminal_reason="api_error",
    result=SPEND_LIMIT_TEXT,
)
SPEND_LIMIT_RETRY = {
    "type": "system",
    "subtype": "api_retry",
    "error_status": 429,
    "error": {"type": "enforced_spend_limit_reached"},
}


class FailureClassificationThroughTheSeamTests(SeamTestCase):
    """``run_agent`` on a backend that reuses ``collect`` classifies the failure: slice 1 lives in the seam."""

    def run_with(self, lines, returncode=0):
        backend = TranscriptBackend(lines, returncode)
        return agent.run_agent("p", self.cwd, self.prefix(), "build-0", backend=backend)

    def failure_message(self, tail):
        prefix = self.prefix()
        return f"agent failed ({tail}); see {prefix}.jsonl and {prefix}.stderr"

    def test_a_failed_result_event_with_a_spend_limit_carries_the_code_and_the_message_is_unchanged(self):
        with self.assertRaises(agent.AgentError) as caught:
            self.run_with([INIT, SPEND_LIMIT_RESULT], returncode=1)
        error = caught.exception
        self.assertEqual(error.api_error, "spend_limit")
        self.assertEqual(
            str(error), self.failure_message("subtype=success, is_error=true, terminal_reason=api_error, api_error_status=429")
        )
        self.assertNotIn("POISON", str(error))
        self.assertEqual(error.usage["total_cost_usd"], 0.0125)  # the spend stays accountable

    def test_the_failed_result_event_alone_decides_even_when_the_log_says_something_else(self):
        other = result_event(subtype="error_max_turns", is_error=True)
        with self.assertRaises(agent.AgentError) as caught:
            self.run_with([SPEND_LIMIT_RETRY, other], returncode=1)
        self.assertIsNone(caught.exception.api_error)
        self.assertEqual(str(caught.exception), self.failure_message("subtype=error_max_turns, is_error=true"))

    def test_collect_of_the_local_backend_classifies_the_same_transcript(self):
        backend = TranscriptBackend([INIT, SPEND_LIMIT_RESULT], returncode=1)
        spec = backend.prepare(self.request("build-0"))
        outcome = backend.launch(spec)
        with self.assertRaises(agent.AgentError) as caught:
            backend.collect(spec, outcome)
        self.assertEqual(caught.exception.api_error, "spend_limit")

    def test_no_result_event_with_an_api_error_in_the_log_tail_is_an_agent_error_with_the_code(self):
        with self.assertRaises(agent.AgentError) as caught:
            self.run_with([INIT, SPEND_LIMIT_RETRY], returncode=1)
        self.assertEqual(caught.exception.api_error, "spend_limit")
        self.assertEqual(str(caught.exception), f"agent exited 1; see {self.prefix()}.stderr")
        self.assertIsNone(caught.exception.usage)  # there is no result event to take the spend from

    def test_no_result_event_and_a_clean_exit_with_an_api_error_in_the_log_tail_names_the_log(self):
        with self.assertRaises(agent.AgentError) as caught:
            self.run_with([INIT, SPEND_LIMIT_RETRY], returncode=0)
        self.assertEqual(caught.exception.api_error, "spend_limit")
        self.assertEqual(str(caught.exception), f"agent produced no result event; see {self.prefix()}.jsonl")

    def test_no_result_event_and_nothing_in_the_log_tail_stays_the_plain_runtime_error(self):
        for returncode, text in (
            (3, f"agent exited 3; see {self.prefix()}.stderr"),
            (0, f"agent produced no result event; see {self.prefix()}.jsonl"),
        ):
            with self.subTest(returncode=returncode), self.assertRaises(RuntimeError) as caught:
                self.run_with([INIT], returncode=returncode)
            self.assertIs(type(caught.exception), RuntimeError)
            self.assertEqual(str(caught.exception), text)


class LiveSetAndEnvTests(unittest.TestCase):
    def test_register_and_forget_keep_the_set_kill_live_agents_reads(self):
        self.addCleanup(agent._LIVE.discard, 3131)
        agent.register_live(3131)
        self.assertIn(3131, agent._LIVE)
        killed = []
        with patch("factory.agent.kill_group", side_effect=killed.append):
            agent.kill_live_agents()
        self.assertIn(3131, killed)
        agent.forget_live(3131)
        self.assertNotIn(3131, agent._LIVE)
        agent.forget_live(3131)

    def test_worker_env_is_the_five_non_secret_names(self):
        env = agent.worker_env("/h", "/h/.claude")
        self.assertEqual(
            env,
            {
                "HOME": "/h",
                "CLAUDE_CONFIG_DIR": "/h/.claude",
                "DISABLE_AUTOUPDATER": "1",
                "DISABLE_TELEMETRY": "1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            },
        )


class DocsTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parent.parent

    def test_the_design_record_names_the_three_steps_and_every_public_class_of_the_seam(self):
        text = (self.ROOT / "docs" / "pod-backend.md").read_text(encoding="utf-8")
        self.assertIn("## The backend seam", text)
        public = [
            name
            for name, found in inspect.getmembers(backend, inspect.isclass)
            if found.__module__ == backend.__name__ and not name.startswith("_")
        ]
        self.assertGreaterEqual(len(public), 7)
        for name in [*public, "prepare", "launch", "collect", "kill_live_agents"]:
            with self.subTest(name=name):
                self.assertIn(f"`{name}", text)
        self.assertIn("no behaviour change", text)

    def test_the_architecture_layout_lists_the_module(self):
        self.assertIn("`backend.py`", (self.ROOT / "docs" / "architecture.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    if sys.argv[1:] == ["--write-golden"]:
        write_golden()
    else:
        unittest.main()

"""The pod backend (src/factory/podbackend.py): one worker session as a pod (slice 12b of the pod backend).

CONTRACT-TESTED against in-memory fakes, never against a cluster: the launcher is a fake with the surface of
``LauncherClient`` (and, in the last classes, the real launcher over a real unix socket on the in-memory fake of the
Kubernetes API); the relay is the real ``relay.Relay`` called in process (no socket); the pod is a script that makes the
relay calls a pod makes, one step per poll of the backend. The clock is a fake that only the backend's ``sleep`` moves,
so no test waits. Every test pins one protection and fails when that protection is removed:

* the launch request is exactly the closed request and the relay token is in no file, no request, no message and
  no log (a poisoned environment), only its hash is in the record;
* the delta is validated and applied ONLY through ``bundle.apply_delta`` (a hostile delta changes nothing), a read-only
  phase may not return one, and the size the report declares must match;
* every end of a session releases: revoke (unless the result was delivered), pod deleted, outputs copied, session
  released, home removed, handle unregistered only once the pod is confirmed gone;
* failures map to the outcomes ``LocalBackend`` gives (timeout, exit code, memory limit, no result) and a spend
  limit in the transcript reaches the breaker as ``AgentError.api_error``;
* an isolation failure trips the breaker and denies the launcher; a tripped breaker starts nothing.
"""

import ast
import base64
import contextlib
import io
import itertools
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch

from factory import agent, apierror, backend, bundle, gitsafe, launcher_client, podbackend, podexecution, podhandle, podliveness, podpool, podreport, podstatus, podsweep, relay, stopworld
from factory import podcontract as pc
from factory import sessionrecord as sr
from factory.agent import AgentError, AgentTimeout, run_agent
from factory.backend import AgentRequest
from factory.state import State
from tests import fake_kube
from tests.fake_blob import FakeClock
from tests.test_launcher import ServerHarness, launch_line

ROOT = Path(__file__).resolve().parent.parent
POD_UID = "11111111-2222-3333-4444-555555555555"
INSTANCE = "abcdef012345"
REPOSITORY = "octo/widgets"
RUN_ID = "run-20260101-0001"
EPOCH = 1_800_000_000
CANARIES = ("CANARY-ANTHROPIC-KEY-podbackend-31ad", "CANARY-GITHUB-TOKEN-podbackend-77c0", "CANARY-BROKER-podbackend-9e12")
POISON = "POISON-agent-text-do-not-leak-4b1e"


# --- fakes -------------------------------------------------------------------------------------------------------


class Moment:
    """The relay's clock, on the same fake time as everything else."""

    def __init__(self, clock):
        self._clock = clock

    def time(self):
        return EPOCH + self._clock()

    def monotonic(self):
        return self._clock()


EMPTY_DELTA = bundle.HEADER  # the empty delta: the header and no record


def status(state="running", *, final=False, exit_code=None, present=True, terminating=False, detail="x"):
    """The answer of ``LauncherClient.pod_state``."""
    return {
        "state": state,
        "detail": detail,
        "final": final,
        "exit_code": exit_code,
        "present": present,
        "terminating": terminating,
    }


RUNNING = status()
SUCCEEDED = status("succeeded", final=True, exit_code=0)


class FakeLauncher:
    """The surface of ``LauncherClient`` over scripted answers; it records every call and never touches a network."""

    def __init__(self, case):
        self.case = case
        self.requests = []
        self.tokens = []
        self.failures = deque()  # exceptions the next launches raise, then a pod is created
        self.state = RUNNING
        self.state_error = None
        self.on_poll = None  # runs inside ``pod_state``, i.e. between the backend's two reads
        self.terminated = []
        self.deleted = []
        self.denied = []
        self.terminate_error = None
        self.polls = 0

    def launch(self, request, token):
        self.requests.append(dict(request))
        self.tokens.append(token)
        if self.failures:
            raise self.failures.popleft()
        session = request["session_id"]
        self.case.on_launch(session, token)
        return launcher_client.Launched(pc.pod_name(session), POD_UID, request["timeout_seconds"], False)

    def pod_state(self, name, uid=None):
        self.polls += 1
        if self.on_poll is not None:
            self.on_poll()
        if self.state_error is not None:
            raise self.state_error
        return dict(self.state)

    def delete_pod(self, name, uid=None):
        self.deleted.append(name)
        return True

    def terminate(self, handle):
        self.terminated.append(handle)
        if self.terminate_error is not None:
            raise self.terminate_error

    def deny(self, reason):
        self.denied.append(reason)

    def reader(self, handle):
        return "alive"


class PodClient:
    """One session's pod: the calls a pod makes to the relay, in process, with the credentials of the launch."""

    def __init__(self, case, session, token, uid=POD_UID):
        self.case, self.session, self.token, self.uid = case, session, token, uid
        self.sent = {"transcript": 0, "stderr": 0}  # the bytes the relay accepted, as the wrapper's report says

    def call(self, method, path, data=b"", length="auto"):
        headers = {
            "Authorization": f"Bearer {self.token}",
            relay.HEADER_POD_NAME: pc.pod_name(self.session),
            relay.HEADER_POD_UID: self.uid,
        }
        declared = (len(data) if method == "POST" else None) if length == "auto" else length
        reply = self.case.relay.handle(method, path, headers, declared, relay.Body(io.BytesIO(data), declared or 0))
        if reply.download is not None:
            chunks = []
            self.case.relay.pump(reply.download, chunks.append)
            reply.download.close()
            return reply.status, b"".join(chunks)
        return reply.status, json.loads(reply.body)

    def preflight(self, result="ok", code=None):
        document = {"result": result}
        if code is not None:
            document["code"] = code
        return self.call("POST", "/v1/preflight", json.dumps(document).encode("ascii"))[0]

    def fetch(self):
        status_code, body = self.call("GET", "/v1/bundle")
        assert status_code == 200, status_code
        return body

    def chunk(self, stream, data):
        code = self.call("POST", f"/v1/stream/{stream}", data)[0]
        if code == 200:
            self.sent[stream] += len(data)
        return code

    def heartbeat(self):
        return self.call("POST", "/v1/heartbeat")[0]

    def finish(self, report, delta=EMPTY_DELTA):
        if isinstance(report, dict) and report.get("streamed") is None and "streamed" in report:
            report = {**report, "streamed": dict(self.sent)}  # what this pod really sent
        raw = json.dumps(report).encode("ascii") if isinstance(report, dict) else report
        return self.call("POST", "/v1/result", struct.pack(">I", len(raw)) + raw + delta)[0]


def delta_of(*ops):
    """A delta stream: each op is ``("file", path, data[, exec])``, ``("delete", path)`` or ``("symlink", path, target)``."""
    sink = io.BytesIO()
    writer = bundle.Writer(sink, bundle.DELTA)
    for op in ops:
        if op[0] == "file":
            writer.put(bundle.FILE, bundle.AREA_TREE, op[1], op[2], len(op) > 3 and op[3])
        elif op[0] == "delete":
            writer.put(bundle.DELETE, bundle.AREA_TREE, op[1])
        else:
            writer.put(bundle.SYMLINK, bundle.AREA_TREE, op[1], op[2].encode())
    return sink.getvalue(), len(ops)


def result_line(**changes):
    event = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "structured_output": {"ok": True},
        "total_cost_usd": 0.5,
        "num_turns": 3,
        "duration_ms": 1000,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    event.update(changes)
    return json.dumps(event).encode() + b"\n"


def report_of(delta_count=0, delta_bytes=None, data=EMPTY_DELTA, exit_code=0, outcome="exited", status="ok", **extra):
    """The report of ``podreport`` (``streamed`` is filled in by ``PodClient.finish`` with what the pod really sent)."""
    document = {
        "version": 1,
        "outcome": outcome,
        "exit_code": exit_code,
        "footprint": {"peak_rss_mb": 123.4, "cpu_seconds": 5.5, "samples": 7, "limit_mb": 4096},
        "exceeded_mb": None,
        "delta": {
            "status": status,
            "records": delta_count if status == "ok" else 0,
            "bytes": (len(data) if delta_bytes is None else delta_bytes) if status == "ok" else 0,
        },
        "streamed": None,
        "dropped": {"transcript": False, "stderr": False},
    }
    document.update(extra)
    return document


class Fixture:
    """A data directory with a worktree, a spool with a real relay and a script for the pod (a mixin: ``build`` sets it up)."""

    phase = "build"
    timeout = 600
    patch_git = True

    def build(self, client, clock):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(os.path.realpath(self.temp.name))
        self.data = self.base / "data"
        self.spool = self.base / "spool"
        self.rundir = self.data / "repos" / "octo" / "widgets" / "runs" / RUN_ID
        self.tree = self.data / "worktrees" / "job"
        self.prefix = self.rundir / "job-a" / "1-build"
        for directory in (self.spool, self.tree / "src", self.prefix.parent):
            directory.mkdir(parents=True)
        (self.tree / "README").write_text("hello\n")
        (self.tree / "src" / "a.py").write_text("print(1)\n")
        os.chmod(self.spool, relay.SPOOL_MODE)
        self.clock = clock
        self.pool = podpool.PodPool(
            self.base / "pool", 8, clock=lambda: EPOCH + self.clock(), breaker_dir=self.data, sleep=self.sleep
        )
        self.pool.prepare()
        self.relay = relay.Relay(self.spool, clock=Moment(self.clock), owner=(os.getuid(), os.getgid()))
        self.addCleanup(self.relay.close)
        if self.patch_git:
            fakegit = self.base / "fakegit" / ".git"
            fakegit.mkdir(parents=True)
            (fakegit / "HEAD").write_text("ref: refs/heads/job\n")
            for target, value in (
                (bundle, ("build_git_dir", {"return_value": os.fspath(fakegit)})),
                (gitsafe, ("checkout_gitdir", {"return_value": os.fspath(self.base / "unused")})),
                (agent, ("git_common_dir", {"return_value": None})),
            ):
                patcher = patch.object(target, value[0], **value[1])
                patcher.start()
                self.addCleanup(patcher.stop)
        self.launcher = client
        self.script = {}
        self.early = []
        self.ticks = 0
        self.pod = None
        self.fetched = None
        self.started = []
        self.sleeps = []
        self.base_commit = None
        self.backend = self.make_backend()


    # -- fixtures -----------------------------------------------------------------------------------------------

    def make_backend(self, **overrides):
        options = dict(
            client=self.launcher,
            pool=self.pool,
            spool=self.spool,
            data_root=self.data,
            scope=podbackend.Scope(REPOSITORY, RUN_ID),
            job_id="job-a",
            attempt=1,
            manager_instance=INSTANCE,
            base=self.base_commit,
            breaker_dir=self.data,
            clock=self.clock,
            wall=lambda: EPOCH + self.clock(),
            sleep=self.sleep,
            poll_seconds=2.0,
        )
        options.update(overrides)
        return podbackend.PodBackend(**options)

    def on_launch(self, session, token):
        self.pod = PodClient(self, session, token)
        self.session = session

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.clock.advance(seconds)
        if self.pod is None:  # still staging or waiting for the launcher: the pod's script has not begun
            for step in self.early:
                step()
            return
        self.ticks += 1
        for step in self.script.pop(self.ticks, ()):
            step()

    def request(self, **changes):
        values = dict(prompt="PROMPT TEXT", cwd=self.tree, prefix=self.prefix, phase=self.phase, timeout=self.timeout)
        values.update(changes)
        return AgentRequest(**values)

    def run_session(self, backend_=None, **changes):
        """``run_agent`` end to end: ``(result, usage)`` or the error it raises. ``on_start`` records what it is given."""
        request = self.request(**changes)
        return run_agent(
            request.prompt,
            request.cwd,
            request.prefix,
            request.phase,
            request.timeout,
            lambda started: self.started.append(started),
            backend=backend_ or self.backend,
        )

    # -- the pod's side, one step per poll ----------------------------------------------------------------------------

    def at(self, tick, step):
        self.script.setdefault(tick, []).append(step)

    def steps_to_streaming(self, transcript=None, stderr=b""):
        """Ticks 1 and 2: the pod passes its canary, fetches its bundle and starts streaming."""

        def start():
            self.assertEqual(self.pod.preflight(), 200)
            self.fetched = self.pod.fetch()

        def stream():
            self.assertEqual(self.pod.chunk("transcript", result_line() if transcript is None else transcript), 200)
            if stderr:
                self.assertEqual(self.pod.chunk("stderr", stderr), 200)
            self.assertEqual(self.pod.heartbeat(), 200)

        self.at(1, start)
        self.at(2, stream)

    def happy(self, delta=EMPTY_DELTA, count=0, transcript=None, stderr=b"", report=None):
        """The whole session: a result with ``delta`` is posted at tick 3, the pod has succeeded at tick 4."""
        self.steps_to_streaming(transcript, stderr)
        document = report_of(count, data=delta) if report is None else report

        def finish():
            self.assertEqual(self.pod.finish(document, delta), 200)
            self.launcher.state = SUCCEEDED

        self.at(3, finish)

    def record(self):
        return sr.read(self.spool, self.session)

    def marked(self, name):
        return sr.is_marked(self.spool, self.session, name)

    def jsonl(self):
        return agent.sibling(self.prefix, ".jsonl")

    def assertReleased(self, delivered=True):
        """What every end of a session leaves: no live pod, a released session, no home, the registry empty."""
        self.assertTrue(self.marked(sr.MARKER_RELEASED))
        self.assertEqual(self.marked(sr.MARKER_REVOKE), not delivered)
        self.assertEqual(podhandle.live(), [])
        self.assertEqual(list(self.prefix.parent.glob(".home-*")), [])
        self.assertEqual(len(self.launcher.terminated), 1)

class Case(Fixture, unittest.TestCase):
    """The fixture over the fake launcher."""

    def setUp(self):
        podhandle.reset()
        self.addCleanup(podhandle.reset)
        self.build(FakeLauncher(self), FakeClock(1000.0))


Fresh = Case


# --- the happy path ---------------------------------------------------------------------------------------------------


class SessionTests(Fresh):
    def test_a_session_runs_as_a_pod_and_returns_what_a_local_one_does(self):
        data, count = delta_of(("file", "src/new.py", b"print(2)\n"), ("file", "README", b"changed\n"), ("delete", "src/a.py"))
        self.happy(data, count, stderr=b"warning\n")
        result, usage = self.run_session()
        self.assertEqual(result, {"ok": True})
        self.assertEqual((usage["phase"], usage["total_cost_usd"]), ("build", 0.5))
        self.assertEqual(usage["footprint"], {"peak_rss_mb": 123.4, "cpu_seconds": 5.5, "samples": 7, "limit_mb": 4096})
        self.assertEqual(self.jsonl().read_bytes(), result_line())
        self.assertEqual(agent.sibling(self.prefix, ".stderr").read_bytes(), b"warning\n")
        self.assertEqual((self.tree / "src" / "new.py").read_text(), "print(2)\n")
        self.assertEqual((self.tree / "README").read_text(), "changed\n")
        self.assertFalse((self.tree / "src" / "a.py").exists())
        self.assertReleased(delivered=True)

    def test_the_pod_handle_reaches_on_start_as_a_pod_and_not_a_pid(self):
        self.happy()
        self.run_session()
        (started,) = self.started
        self.assertIsInstance(started, podhandle.PodHandle)
        self.assertEqual(started.name, pc.pod_name(self.session))
        self.assertEqual((started.uid, started.session, started.launcher), (POD_UID, self.session, "launcher"))

    def test_the_handle_is_recorded_in_job_state_beside_the_pid_field(self):
        directory = self.base / "state"
        directory.mkdir()
        state = State(directory, {"jobs": [{"id": "job-a"}]})
        self.happy()
        run_agent("p", self.tree, self.prefix, "build", 600, lambda started: state.record_started("job-a", started), backend=self.backend)
        self.assertEqual(state.data["jobs"]["job-a"]["pod"]["session"], self.session)
        self.assertIsNone(state.data["jobs"]["job-a"].get("pid"))

    def test_the_handle_is_registered_while_the_session_runs_and_gone_after_it(self):
        seen = []
        self.happy()
        self.at(2, lambda: seen.extend(podhandle.live()))
        self.run_session()
        self.assertEqual([handle.session for handle in seen], [self.session])
        self.assertEqual(podhandle.live(), [])

    def test_the_launch_request_is_exactly_the_closed_request(self):
        self.happy()
        self.run_session()
        (request,) = self.launcher.requests
        self.assertEqual(set(request), set(launcher_client.REQUEST_FIELDS))
        self.assertEqual(
            request,
            {
                "session_id": self.session,
                "repository": REPOSITORY,
                "run_id": RUN_ID,
                "job_id": "job-a",
                "attempt": 1,
                "phase": "build",
                "resource_class": pc.DEFAULT_CLASS,
                "timeout_seconds": 600,
                "manager_instance": INSTANCE,
            },
        )

    def test_a_short_time_limit_is_raised_to_the_launchers_minimum_and_a_fractional_one_rounded_up(self):
        for timeout, expected in ((5, 60), (90.2, 91)):
            with self.subTest(timeout=timeout):
                self.launcher.requests.clear()
                self.happy()
                self.run_session(timeout=timeout)
                self.assertEqual(self.launcher.requests[0]["timeout_seconds"], expected)
                self.setUp()
                podhandle.reset()

    def test_the_record_holds_the_hash_of_the_token_the_pod_got_and_the_pod_it_is_bound_to(self):
        self.happy()
        self.run_session()
        record = self.record()
        token = self.launcher.tokens[0]
        self.assertEqual(record["token_sha256"], sr.token_hash(token))
        self.assertEqual((record["pod_name"], record["pod_uid"]), (pc.pod_name(self.session), POD_UID))
        self.assertEqual((record["repo"], record["manager"], record["write_scope"]), (pc.repo_key(REPOSITORY), INSTANCE, "cwd"))
        self.assertEqual(record["caps"], sr.default_caps())
        # The token outlives the pod's possible start by the scheduling time and the session's own time.
        self.assertEqual(record["deadline"], EPOCH + 1000 + pc.SCHEDULING_TIMEOUT_SECONDS + 600 + pc.DEADLINE_MARGIN_SECONDS)

    def test_the_deadline_clamp_bounds_the_token_lifetime(self):
        self.happy()
        self.run_session(timeout=10 * pc.DEADLINE_CLAMP_SECONDS)
        record = self.record()
        self.assertEqual(
            record["deadline"] - (EPOCH + 1000),
            pc.SCHEDULING_TIMEOUT_SECONDS + pc.DEADLINE_CLAMP_SECONDS + pc.DEADLINE_MARGIN_SECONDS,
        )

    def test_a_read_only_phase_records_the_none_write_scope(self):
        self.phase = "plan"
        self.happy()
        self.run_session(phase="plan")
        self.assertEqual(self.record()["write_scope"], "none")
        self.assertEqual(self.launcher.requests[0]["phase"], "plan")

    def test_an_attempt_phase_name_is_launched_as_its_phase_word(self):
        self.happy()
        self.run_session(phase="build-2")
        self.assertEqual(self.launcher.requests[0]["phase"], "build")

    def test_the_pod_fetches_a_bundle_with_the_tree_and_the_manifest(self):
        self.happy()
        self.run_session()
        records = {
            (r.area, r.path): r.read(1 << 20)
            for r in bundle.Reader(io.BytesIO(self.fetched), bundle.BUNDLE)
            if r.kind == bundle.FILE
        }
        manifest = json.loads(records[(bundle.AREA_META, bundle.MANIFEST_PATH)])
        self.assertEqual(records[(bundle.AREA_TREE, "README")], b"hello\n")
        self.assertEqual(records[(bundle.AREA_TREE, "src/a.py")], b"print(1)\n")
        self.assertEqual(manifest["worktree"], "worktrees/job")
        self.assertEqual(manifest["version"], 1)

    def test_the_manifest_carries_what_part_a_lays_out_and_part_b_runs(self):
        self.happy()
        self.run_session()
        manifest = next(
            json.loads(r.read(1 << 20)) for r in bundle.Reader(io.BytesIO(self.fetched), bundle.BUNDLE) if r.area == bundle.AREA_META
        )
        home = self.prefix.parent / f".home-{self.prefix.name}"
        self.assertEqual(manifest["dirs"], [str(home), str(home / ".claude")])
        modes = {entry["path"]: entry["mode"] for entry in manifest["files"]}
        self.assertEqual(
            modes,
            {
                str(agent.sibling(self.prefix, ".prompt.txt")): 0o600,
                str(agent.sibling(self.prefix, ".settings.json")): 0o640,
                str(agent.sibling(self.prefix, ".system.md")): 0o640,
                str(home / ".gitconfig"): 0o640,
            },
        )
        prompt = next(e for e in manifest["files"] if e["path"].endswith(".prompt.txt"))
        self.assertEqual(base64.b64decode(prompt["data"]), b"PROMPT TEXT")
        run = manifest["run"]
        self.assertEqual(set(run), set(podbackend.RUN_KEYS))  # exactly what podworker.read_run accepts
        self.assertEqual(run["cwd"], str(self.tree))
        self.assertEqual(run["prompt"], str(agent.sibling(self.prefix, ".prompt.txt")))
        self.assertEqual((run["timeout"], run["memory_limit_mb"]), (600, None))
        self.assertEqual(run["argv"][0], "claude")
        self.assertEqual(run["env"]["HOME"], str(home))
        self.assertEqual(run["env"]["CLAUDE_CONFIG_DIR"], str(home / ".claude"))

    def test_the_base_commit_is_staged_when_the_job_has_one(self):
        self.base_commit = "ab" * 20
        self.backend = self.make_backend(base=self.base_commit)
        self.happy()
        with patch.object(bundle, "stage", wraps=bundle.stage) as staged:
            self.run_session()
        self.assertEqual(staged.call_args.kwargs["base"], self.base_commit)
        self.assertEqual(staged.call_args.kwargs["worktree"], self.tree)
        self.assertEqual(staged.call_args.kwargs["data_root"], os.fspath(self.data))

    def test_the_session_waits_for_a_pod_that_is_slow_to_start_and_counts_the_time_limit_from_the_fetch(self):
        self.timeout = 20
        self.steps_to_streaming()
        # Nothing happens for 14 polls (28 s) before the pod fetches: more than the time limit, which has not started.
        self.script = {}

        def late():
            self.assertEqual(self.pod.preflight(), 200)
            self.fetched = self.pod.fetch()
            self.pod.chunk("transcript", result_line())
            self.pod.heartbeat()

        self.at(14, late)
        self.at(16, lambda: (self.pod.finish(report_of()), setattr(self.launcher, "state", SUCCEEDED)))
        result, _ = self.run_session(timeout=20)
        self.assertEqual(result, {"ok": True})

    def test_nothing_secret_reaches_the_spool_the_files_the_request_or_the_output(self):
        canaries = dict(zip(("ANTHROPIC_API_KEY", "GH_TOKEN", "FACTORY_TOKEN_BROKER"), CANARIES))
        self.happy()
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, canaries), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.run_session()
        haystack = [out.getvalue(), err.getvalue(), json.dumps(self.launcher.requests), self.fetched.decode("latin-1")]
        for base in (self.spool, self.rundir):
            for path in base.rglob("*"):
                if path.is_file():
                    haystack.append(path.read_bytes().decode("latin-1"))
        for canary in CANARIES:
            for text in haystack:
                self.assertNotIn(canary, text)
        self.assertNotIn(self.launcher.tokens[0], "".join(haystack))

    def test_the_throwaway_home_exists_after_prepare_and_is_gone_after_launch(self):
        spec = self.backend.prepare(self.request())
        self.assertTrue(spec.home.is_dir())
        self.happy()
        outcome = self.backend.launch(spec, None)
        self.assertFalse(spec.home.exists())
        self.assertEqual(outcome.returncode, 0)
        result, usage = self.backend.collect(spec, outcome)
        self.assertEqual(result, {"ok": True})

    def test_the_two_output_files_exist_and_are_empty_even_when_nothing_came_back(self):
        self.launcher.failures.append(launcher_client.Unavailable())
        with self.assertRaises(AgentError):
            self.run_session()
        self.assertEqual((self.jsonl().read_bytes(), agent.sibling(self.prefix, ".stderr").read_bytes()), (b"", b""))


# --- what the worker identity and the worktree look like to prepare ----------------------------------------------------------------


class PrepareTests(Fresh):
    def test_the_worker_identity_of_the_repository_is_not_needed_and_not_applied(self):
        with patch.dict(os.environ, {agent.REQUIRE_WORKER_IDENTITY: "1"}):
            spec = self.backend.prepare(self.request())
        self.assertEqual((spec.user, spec.group), (None, None))

    def test_the_local_backend_still_requires_it(self):
        with patch.dict(os.environ, {agent.REQUIRE_WORKER_IDENTITY: "1"}):
            with self.assertRaises(AgentError):
                backend.LocalBackend().prepare(self.request())

    def test_the_repository_worker_ids_are_dropped_before_anything_is_shared(self):
        with patch("factory.agent.share_with_worker") as shared:
            spec = self.backend.prepare(self.request(user=10002, group=10010))
        shared.assert_not_called()
        self.assertEqual((spec.user, spec.group), (None, None))

    def test_a_phase_outside_the_contract_is_refused_before_a_file_is_written(self):
        with self.assertRaises(ValueError):
            self.backend.prepare(self.request(phase="deploy"))
        self.assertEqual(list(self.prefix.parent.iterdir()), [])

    def test_the_phases_of_the_contract_are_the_phases_the_policy_table_knows(self):
        from factory import phases

        self.assertEqual(set(pc.PHASES), set(phases.PHASES))

    def test_a_phase_the_contract_does_not_name_is_refused_even_if_the_policy_table_knows_it(self):
        with patch.object(pc, "PHASES", tuple(p for p in pc.PHASES if p != "build")):
            with self.assertRaises(ValueError):
                self.backend.prepare(self.request())
        self.assertEqual(list(self.prefix.parent.iterdir()), [])

    def test_a_directory_outside_the_data_directory_is_refused_before_a_file_is_written(self):
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        cases = {
            "cwd": dict(cwd=elsewhere),
            "prefix": dict(prefix=elsewhere / "1-build"),
            "read path": dict(read_paths=(elsewhere / "evidence.json",)),
            "a link out of the data directory": dict(cwd=self.link_to(elsewhere)),
        }
        for name, change in cases.items():
            with self.subTest(name):
                with self.assertRaises(AgentError) as caught:
                    self.backend.prepare(self.request(**change))
                self.assertEqual(str(caught.exception), podbackend.OUTSIDE)
        self.assertEqual(list(self.prefix.parent.iterdir()), [])
        self.assertEqual(list(elsewhere.iterdir()), [])

    def link_to(self, target):
        link = self.data / "link"
        if not link.exists():
            link.symlink_to(target)
        return link

    def test_a_tripped_breaker_refuses_before_a_file_is_written(self):
        stopworld.Breaker(self.data).trip(apierror.SPEND_LIMIT)
        with self.assertRaises(AgentError) as caught:
            self.backend.prepare(self.request())
        self.assertEqual(str(caught.exception), podbackend.TRIPPED)
        self.assertEqual(list(self.prefix.parent.iterdir()), [])

    def test_an_unreadable_breaker_marker_still_refuses(self):
        (self.data / stopworld.MARKER).write_bytes(b"\xff\xfe not json")
        with self.assertRaises(AgentError):
            self.backend.prepare(self.request())

    def test_the_contract_resource_class_is_the_default_one(self):
        self.assertEqual(self.backend.prepare(self.request()).resource_class, pc.DEFAULT_CLASS)


# --- failures ------------------------------------------------------------------------------------------------------------------------


class FailureTests(Fresh):
    def prefix_text(self):
        return f"see {self.prefix}.jsonl"

    def test_a_pod_that_exits_non_zero_without_a_result_is_the_local_exit_error(self):
        self.steps_to_streaming(transcript=b"")
        self.at(3, lambda: setattr(self.launcher, "state", status("failed_exit", final=True, exit_code=3)))
        with self.assertRaises(RuntimeError) as caught:
            self.run_session()
        self.assertEqual(str(caught.exception), f"agent exited 3; see {self.prefix}.stderr")
        self.assertReleased(delivered=False)

    def test_a_wrapper_stage_exit_is_named_by_its_stage_and_never_by_what_the_pod_said(self):
        for stage, code in podbackend.WRAPPER_STAGES.items():
            if stage == "canary":
                continue  # an isolation failure of its own (IsolationTests)
            with self.subTest(stage=stage):
                self.setUp()
                self.at(1, lambda: setattr(self.launcher, "state", status("failed_exit", final=True, exit_code=code)))
                with self.assertRaises(RuntimeError) as caught:
                    self.run_session()
                self.assertEqual(
                    str(caught.exception),
                    f"the pod worker failed at its {stage} stage; see {self.prefix}.jsonl and {self.prefix}.stderr",
                )
                self.assertReleased(delivered=False)

    def test_an_exit_that_is_no_stage_of_the_wrapper_is_reported_by_number_only(self):
        self.at(1, lambda: setattr(self.launcher, "state", status("failed_exit", final=True, exit_code=126)))
        with self.assertRaises(RuntimeError) as caught:
            self.run_session()
        self.assertEqual(str(caught.exception), f"agent exited 126; see {self.prefix}.stderr")

    def test_a_failed_pod_with_no_exit_code_is_still_a_failure(self):
        self.at(1, lambda: setattr(self.launcher, "state", status("failed_exit", final=True)))
        with self.assertRaises(RuntimeError) as caught:
            self.run_session()
        self.assertIn("agent exited 1", str(caught.exception))

    def test_a_non_zero_exit_after_a_success_result_is_the_local_message(self):
        self.happy(report=report_of(exit_code=2))
        with self.assertRaises(AgentError) as caught:
            self.run_session()
        self.assertEqual(str(caught.exception), f"agent exited 2 after a success result; see {self.prefix}.stderr")
        self.assertEqual(caught.exception.usage["total_cost_usd"], 0.5)

    def test_a_memory_kill_is_the_memory_limit_error_with_the_spend_kept(self):
        self.steps_to_streaming()
        self.at(3, lambda: setattr(self.launcher, "state", status("oom", final=True, exit_code=137)))
        with self.assertRaises(AgentError) as caught:
            self.run_session()
        self.assertEqual(
            str(caught.exception),
            f"worker exceeded its memory limit: the pod was killed at its standard class limit of 4Gi; see {self.prefix}.stderr",
        )
        self.assertEqual(caught.exception.usage["subtype"], "success")

    def test_a_memory_kill_without_a_transcript_still_records_a_usage_entry(self):
        self.at(1, lambda: setattr(self.launcher, "state", status("oom", final=True, exit_code=137)))
        with self.assertRaises(AgentError) as caught:
            self.run_session()
        self.assertEqual(caught.exception.usage["subtype"], "memory_limit")

    def test_the_ends_that_return_no_result_each_have_their_own_fixed_text(self):
        cases = {
            "evicted": (status("evicted", final=True), "evicted"),
            "gone": (status("gone", final=True, present=False), "gone"),
            "succeeded without a result": (SUCCEEDED, "ended"),
            "not scheduled": (status("unschedulable", final=True, present=False), "not_scheduled"),
            "start error": (status("starting_error", final=True, present=False), "not_scheduled"),
        }
        for name, (answer, key) in cases.items():
            with self.subTest(name):
                self.setUp()
                podhandle.reset()
                self.at(1, lambda answer=answer: setattr(self.launcher, "state", answer))
                with self.assertRaises(RuntimeError) as caught:
                    self.run_session()
                self.assertEqual(str(caught.exception), f"{podbackend.FAILURES[key]}; see {self.prefix}.jsonl and {self.prefix}.stderr")
                self.assertReleased(delivered=False)

    def test_a_spend_limit_in_the_transcript_reaches_the_breaker_as_the_api_error_code(self):
        line = result_line(subtype="error_during_execution", is_error=True, api_error_status=429, terminal_reason="enforced_spend_limit_reached")
        self.steps_to_streaming(transcript=line)
        self.at(3, lambda: setattr(self.launcher, "state", status("failed_exit", final=True, exit_code=1)))
        with self.assertRaises(AgentError) as caught:
            self.run_session()
        self.assertEqual(caught.exception.api_error, apierror.SPEND_LIMIT)

    def test_a_spend_limit_that_ends_a_pod_with_no_result_event_is_found_in_the_log_tail(self):
        line = json.dumps({"type": "system", "api_error_status": 429, "error": {"type": "enforced_spend_limit_reached"}}).encode() + b"\n"
        self.steps_to_streaming(transcript=line)
        self.at(3, lambda: setattr(self.launcher, "state", status("evicted", final=True)))
        with self.assertRaises(AgentError) as caught:
            self.run_session()
        self.assertEqual(caught.exception.api_error, apierror.SPEND_LIMIT)

    def test_a_spend_limit_result_event_of_a_pod_that_was_then_evicted_still_reaches_the_breaker_with_its_usage(self):
        line = result_line(subtype="error_during_execution", is_error=True, api_error_status=429, terminal_reason="enforced_spend_limit_reached")
        self.steps_to_streaming(transcript=line)
        self.at(3, lambda: setattr(self.launcher, "state", status("evicted", final=True)))
        with self.assertRaises(AgentError) as caught:
            self.run_session()
        self.assertEqual(caught.exception.api_error, apierror.SPEND_LIMIT)
        self.assertEqual(caught.exception.usage["total_cost_usd"], 0.5)
        self.assertIn(podbackend.FAILURES["evicted"], str(caught.exception))

    def test_an_auth_failure_in_the_transcript_is_classified_the_same_way(self):
        line = result_line(subtype="error_during_execution", is_error=True, api_error_status=401)
        self.happy(transcript=line, report=report_of(exit_code=1))
        with self.assertRaises(AgentError) as caught:
            self.run_session()
        self.assertEqual(caught.exception.api_error, apierror.AUTH)

    def test_a_failure_with_no_api_evidence_carries_no_code(self):
        self.at(1, lambda: setattr(self.launcher, "state", status("gone", final=True, present=False)))
        with self.assertRaises(RuntimeError) as caught:
            self.run_session()
        self.assertNotIsInstance(caught.exception, AgentError)

    def test_agent_text_from_the_pod_never_reaches_an_error_message(self):
        line = result_line(subtype="error_during_execution", is_error=True, result=POISON)
        self.happy(transcript=line, report=report_of(exit_code=1), stderr=POISON.encode())
        with self.assertRaises(AgentError) as caught:
            self.run_session()
        self.assertNotIn(POISON, str(caught.exception))

    def test_a_pod_that_hit_its_deadline_is_the_time_limit_error(self):
        self.steps_to_streaming()
        self.at(3, lambda: setattr(self.launcher, "state", status("deadline", final=True)))
        with self.assertRaises(AgentTimeout):
            self.run_session()
        self.assertReleased(delivered=False)

    def test_the_time_limit_stops_the_session_deletes_the_pod_and_keeps_the_transcript(self):
        self.timeout = 30
        self.steps_to_streaming()
        with self.assertRaises(AgentTimeout) as caught:
            self.run_session(timeout=30)
        self.assertEqual(caught.exception.args, AgentTimeout(30).args)
        self.assertEqual(self.jsonl().read_bytes(), result_line())
        self.assertReleased(delivered=False)
        # The relay no longer answers the pod: the session was revoked and then released.
        self.assertEqual(self.pod.heartbeat(), 410)

    def test_a_tick_before_the_time_limit_does_not_stop_the_session(self):
        self.timeout = 30
        self.steps_to_streaming()
        self.at(3, lambda: (self.pod.finish(report_of()), setattr(self.launcher, "state", SUCCEEDED)))
        # ticks 1 and 2 run at 2 s each: the session started at the second poll, and the finish comes at the third.
        result, _ = self.run_session(timeout=30)
        self.assertEqual(result, {"ok": True})

    def test_a_pod_that_goes_silent_is_stopped_after_the_orphan_silence(self):
        self.steps_to_streaming()
        with self.assertRaises(RuntimeError) as caught:
            self.run_session()
        self.assertIn(podbackend.FAILURES["silent"], str(caught.exception))
        silent = sum(self.sleeps) - 2 * 2
        self.assertGreaterEqual(silent, pc.ORPHAN_SILENCE_SECONDS)  # ``podliveness``: silent at the window, to the second
        self.assertLess(silent, pc.ORPHAN_SILENCE_SECONDS + 10)
        self.assertReleased(delivered=False)

    def test_a_heartbeat_resets_the_silence(self):
        self.steps_to_streaming()
        for tick in range(3, 900, 100):  # every 200 s: inside the 300 s of silence the orphan rule allows
            self.at(tick, lambda: self.pod.heartbeat())
        self.at(900, lambda: (self.pod.finish(report_of(data=EMPTY_DELTA), EMPTY_DELTA), setattr(self.launcher, "state", SUCCEEDED)))
        self.timeout = 5000
        result, _ = self.run_session(timeout=5000)
        self.assertEqual(result, {"ok": True})

    def test_a_pod_that_never_fetches_its_bundle_is_stopped_at_the_start_bound(self):
        self.timeout = 7200  # the relay's own deadline (the session's limit and 900 s) is then later than the bound
        with self.assertRaises(RuntimeError) as caught:
            self.run_session(timeout=7200)
        self.assertIn(podbackend.FAILURES["not_started"], str(caught.exception))
        self.assertGreater(sum(self.sleeps), podbackend.START_BOUND_SECONDS)
        self.assertLess(sum(self.sleeps), podbackend.START_BOUND_SECONDS + 10)
        self.assertReleased(delivered=False)

    def test_the_start_bound_covers_scheduling_the_grace_and_a_full_bundle_transfer(self):
        self.assertEqual(
            podbackend.START_BOUND_SECONDS,
            pc.SCHEDULING_TIMEOUT_SECONDS + pc.LAUNCH_GRACE_SECONDS + pc.RELAY_TRANSFER_TOTAL_SECONDS,
        )

    def test_a_launcher_that_stops_answering_ends_the_session_after_the_bound_even_while_the_pod_beats(self):
        self.steps_to_streaming()
        self.at(3, lambda: setattr(self.launcher, "state_error", launcher_client.Unavailable()))
        for tick in range(4, 400, 50):  # the pod keeps beating, so only the lost launcher can end the session
            self.at(tick, lambda: self.pod.heartbeat())
        self.timeout = 5000
        with self.assertRaises(RuntimeError) as caught:
            self.run_session(timeout=5000)
        self.assertIn(podbackend.FAILURES["launcher_lost"], str(caught.exception))
        lost = sum(self.sleeps) - 3 * 2
        self.assertGreater(lost, podbackend.LAUNCHER_LOST_SECONDS)
        self.assertLess(lost, podbackend.LAUNCHER_LOST_SECONDS + 10)
        self.assertReleased(delivered=False)

    def test_a_launcher_outage_alone_does_not_end_a_session_that_returns_its_result(self):
        self.happy()
        self.at(1, lambda: setattr(self.launcher, "state_error", launcher_client.Unavailable()))
        self.at(4, lambda: setattr(self.launcher, "state_error", None))
        result, _ = self.run_session()
        self.assertEqual(result, {"ok": True})

    def test_a_pod_that_finishes_between_the_two_reads_is_a_success_and_not_a_missing_result(self):
        """The pod is read first and the relay after it: a pod seen as succeeded has posted its result before that."""
        self.steps_to_streaming()

        def finish_while_the_pod_is_read():
            self.launcher.on_poll = None
            self.assertEqual(self.pod.finish(report_of()), 200)
            self.launcher.state = SUCCEEDED

        self.at(3, lambda: setattr(self.launcher, "on_poll", finish_while_the_pod_is_read))
        result, _ = self.run_session()
        self.assertEqual(result, {"ok": True})

    def test_a_pod_that_is_terminating_gets_its_grace_to_post_a_result_and_no_more(self):
        self.steps_to_streaming()
        terminating = status("evicted", final=True, terminating=True)
        self.at(3, lambda: setattr(self.launcher, "state", terminating))
        self.at(5, lambda: (self.pod.finish(report_of()), setattr(self.launcher, "state", SUCCEEDED)))
        result, _ = self.run_session()
        self.assertEqual(result, {"ok": True})

    def test_a_pod_that_stays_terminating_past_its_grace_is_over(self):
        self.steps_to_streaming()
        self.at(3, lambda: setattr(self.launcher, "state", status("evicted", final=True, terminating=True)))
        with self.assertRaises(RuntimeError) as caught:
            self.run_session()
        self.assertIn(podbackend.FAILURES["evicted"], str(caught.exception))
        self.assertGreater(sum(self.sleeps), podbackend.TERMINATING_WAIT_SECONDS)

    def test_a_session_the_relay_cut_off_names_only_the_closed_reject_code(self):
        for code in sr.REJECT_CODES:
            if code == "preflight_failed":
                continue
            with self.subTest(code):
                self.setUp()
                podhandle.reset()
                self.steps_to_streaming()
                self.at(3, lambda code=code: self.force_state("finished", reject=code))
                with self.assertRaises(RuntimeError) as caught:
                    self.run_session()
                self.assertEqual(
                    str(caught.exception),
                    f"{podbackend.CUT_OFF.format(code=code)}; see {self.prefix}.jsonl and {self.prefix}.stderr",
                )
                self.assertReleased(delivered=False)

    def test_an_expired_or_revoked_session_is_a_failure(self):
        for kind, key in (("expired", "expired"), ("revoked", "revoked")):
            with self.subTest(kind):
                self.setUp()
                podhandle.reset()
                self.steps_to_streaming()
                self.at(3, lambda kind=kind: self.force_state(kind))
                with self.assertRaises(RuntimeError) as caught:
                    self.run_session()
                self.assertIn(podbackend.FAILURES[key], str(caught.exception))

    def force_state(self, kind, reject=None):
        with sr.session_dir(self.spool, self.session) as fd:
            document = sr.read_state_at(fd)
            document["state"] = kind
            document["reject"] = reject
            sr.write_state_at(fd, document)

    def test_a_relay_state_that_cannot_be_read_ends_by_the_bounds_and_not_by_a_crash(self):
        self.steps_to_streaming()

        def corrupt():
            with sr.session_dir(self.spool, self.session) as fd:
                handle = os.open(sr.STATE_FILE, os.O_WRONLY | os.O_TRUNC | os.O_CREAT, 0o660, dir_fd=fd)
                os.write(handle, b"{not json")
                os.close(handle)

        self.at(3, corrupt)
        with self.assertRaises(RuntimeError) as caught:  # ``podliveness``: unreadable, waited on, then not endless
            self.run_session()
        self.assertIn(podbackend.FAILURES["relay_lost"], str(caught.exception))
        self.assertGreater(sum(self.sleeps), podbackend.LAUNCHER_LOST_SECONDS)
        self.assertReleased(delivered=False)


# --- launching -------------------------------------------------------------------------------------------------------------------------


class LaunchTests(Fresh):
    def test_a_launcher_at_capacity_is_waited_on_and_asked_again(self):
        self.launcher.failures.extend([launcher_client.Capacity(), launcher_client.Capacity()])
        self.happy()
        self.script = {}
        self.steps_to_streaming()
        self.at(5, lambda: (self.pod.finish(report_of()), setattr(self.launcher, "state", SUCCEEDED)))
        result, _ = self.run_session()
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(self.launcher.requests), 3)
        self.assertEqual(self.sleeps[:2], [podbackend.CAPACITY_POLL_SECONDS] * 2)
        # Every ask carries the same session and the same token.
        self.assertEqual(len({r["session_id"] for r in self.launcher.requests}), 1)
        self.assertEqual(len(set(self.launcher.tokens)), 1)

    def test_a_launcher_that_stays_at_capacity_fails_the_session_and_starts_nothing(self):
        self.launcher.failures.extend([launcher_client.Capacity()] * 1000)
        with self.assertRaises(AgentError) as caught:
            self.run_session()
        self.assertEqual(str(caught.exception), podbackend.CAPACITY)
        self.assertGreaterEqual(sum(self.sleeps), podbackend.CAPACITY_WAIT_SECONDS)
        self.assertEqual(podhandle.live(), [])
        self.assertEqual(self.launcher.terminated, [])

    def test_the_launcher_failures_are_fixed_texts_and_a_lost_answer_asks_for_the_pod_to_be_deleted(self):
        cases = (
            (launcher_client.Rejected("denied", ("host-path",)), "the pod launcher refused the launch (denied)", False),
            (launcher_client.ServerRefused(), podbackend.LAUNCHER_UNTRUSTED, False),
            (launcher_client.Unavailable(), podbackend.LAUNCHER_DOWN, True),
            (launcher_client.BadReply(), podbackend.LAUNCHER_DOWN, True),
        )
        for error, text, deletes in cases:
            with self.subTest(type(error).__name__):
                self.setUp()
                podhandle.reset()
                self.launcher.failures.append(error)
                with self.assertRaises(AgentError) as caught:
                    self.run_session()
                self.assertEqual(str(caught.exception), text)
                session = self.launcher.requests[0]["session_id"]
                self.assertEqual(self.launcher.deleted, [pc.pod_name(session)] if deletes else [])
                self.assertEqual(self.started, [])
                self.assertEqual(podhandle.live(), [])

    def test_a_failed_launch_leaves_the_session_revoked_and_released(self):
        self.launcher.failures.append(launcher_client.Rejected("denied"))
        with self.assertRaises(AgentError):
            self.run_session()
        session = self.launcher.requests[0]["session_id"]
        self.assertTrue(sr.is_marked(self.spool, session, sr.MARKER_REVOKE))
        self.assertTrue(sr.is_marked(self.spool, session, sr.MARKER_RELEASED))

    def test_a_tree_too_large_to_ship_fails_before_any_pod_is_asked_for(self):
        with patch.object(bundle, "stage", side_effect=bundle.BundleTooLarge("bytes")):
            with self.assertRaises(AgentError) as caught:
                self.run_session()
        self.assertIn("too large to ship", str(caught.exception))
        self.assertEqual(self.launcher.requests, [])

    def test_a_worktree_that_is_not_the_one_the_supervisor_made_fails_the_session_before_any_pod(self):
        refusal = gitsafe.GitSafetyError("worktree .git pointer was changed")
        with patch.object(gitsafe, "checkout_gitdir", side_effect=refusal):
            with self.assertRaises(gitsafe.GitSafetyError):
                self.run_session()
        self.assertEqual(self.launcher.requests, [])
        self.assertEqual(podhandle.live(), [])

    def test_a_tree_error_names_only_its_reason_word(self):
        with patch.object(bundle, "stage", side_effect=bundle.TreeError("link")):
            with self.assertRaises(AgentError) as caught:
                self.run_session()
        self.assertEqual(str(caught.exception), "the session's tree could not be shipped to a pod (link)")
        self.assertEqual(self.launcher.requests, [])

    def test_a_full_spool_is_waited_on_like_capacity_and_then_fails(self):
        with patch.object(bundle, "stage", side_effect=bundle.SpoolFull(10, 0)):
            with self.assertRaises(AgentError) as caught:
                self.run_session()
        self.assertEqual(str(caught.exception), podbackend.SPOOL)
        self.assertGreaterEqual(sum(self.sleeps), podbackend.SPOOL_WAIT_SECONDS)
        self.assertEqual(self.launcher.requests, [])

    def test_a_spool_that_frees_up_lets_the_session_through(self):
        real = bundle.stage
        calls = []

        def stage(*args, **kwargs):
            calls.append(1)
            if len(calls) < 3:
                raise bundle.SpoolFull(10, 0)
            return real(*args, **kwargs)

        self.happy()
        with patch.object(bundle, "stage", stage):
            result, _ = self.run_session()
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(calls), 3)

    def test_a_spool_that_cannot_be_written_fails_with_a_fixed_text(self):
        with patch.object(sr, "write", side_effect=OSError("disk says POISON")):
            with self.assertRaises(AgentError) as caught:
                self.run_session()
        self.assertEqual(str(caught.exception), podbackend.RECORD)
        self.assertEqual(self.launcher.requests, [])

    def test_a_pod_that_cannot_be_bound_is_deleted_and_the_session_fails(self):
        with patch.object(sr, "bind", side_effect=sr.RecordError("x")):
            with self.assertRaises(AgentError) as caught:
                self.run_session()
        self.assertEqual(str(caught.exception), podbackend.BOUND)
        self.assertEqual(len(self.launcher.terminated), 1)
        self.assertEqual(podhandle.live(), [])

    def test_a_failing_on_start_ends_the_session_and_deletes_the_pod(self):
        def refuse(started):
            raise RuntimeError("another pod session is already recorded")

        with self.assertRaises(RuntimeError):
            run_agent("p", self.tree, self.prefix, "build", 600, refuse, backend=self.backend)
        self.assertReleased(delivered=False)

    def test_an_interrupt_while_waiting_still_releases_everything(self):
        self.steps_to_streaming()

        def interrupt():
            raise KeyboardInterrupt

        self.at(3, interrupt)
        with self.assertRaises(KeyboardInterrupt):
            self.run_session()
        self.assertReleased(delivered=False)
        self.assertEqual(self.jsonl().read_bytes(), result_line())

    def test_a_tripped_breaker_stops_a_session_that_is_waiting_for_capacity(self):
        self.launcher.failures.extend([launcher_client.Capacity()] * 3)
        self.early.append(lambda: stopworld.Breaker(self.data).trip(apierror.SPEND_LIMIT))
        with self.assertRaises(AgentError) as caught:
            self.run_session()
        self.assertEqual(str(caught.exception), podbackend.TRIPPED)
        self.assertEqual(len(self.launcher.requests), 1)  # asked once, at capacity; the breaker stopped the second ask

    def test_the_token_is_never_kept_on_the_backend_or_the_run(self):
        self.happy()
        self.run_session()
        token = self.launcher.tokens[0]
        self.assertNotIn(token, repr(vars(self.backend)))
        self.assertNotIn(token, repr(self.started))


# --- releasing ---------------------------------------------------------------------------------------------------------------------------


class ReleaseTests(Fresh):
    def test_a_termination_that_fails_keeps_the_handle_registered_and_does_not_hide_the_result(self):
        self.happy()
        self.launcher.terminate_error = launcher_client.Unavailable()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result, _ = self.run_session()
        self.assertEqual(result, {"ok": True})
        self.assertEqual([h.session for h in podhandle.live()], [self.session])
        self.assertIn("could not terminate pod session", err.getvalue())
        self.assertNotIn(self.launcher.tokens[0], err.getvalue())

    def test_a_termination_that_fails_does_not_replace_the_error_of_the_session(self):
        self.at(1, lambda: setattr(self.launcher, "state", status("failed_exit", final=True, exit_code=3)))
        self.launcher.terminate_error = launcher_client.Unavailable()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(RuntimeError) as caught:
                self.run_session()
        self.assertIn("agent exited 3", str(caught.exception))

    def test_the_pod_is_terminated_through_the_client_with_its_handle(self):
        self.happy()
        self.run_session()
        (terminated,) = self.launcher.terminated
        self.assertEqual(terminated.session, self.session)

    def test_a_released_session_answers_the_pod_with_gone(self):
        self.happy()
        self.run_session()
        self.assertEqual(self.pod.heartbeat(), 410)

    def test_a_result_delivered_session_is_not_revoked_but_every_other_end_is(self):
        self.happy()
        self.run_session()
        self.assertFalse(self.marked(sr.MARKER_REVOKE))

    def test_the_outputs_are_copied_when_the_session_is_killed_so_the_classifier_can_read_them(self):
        self.steps_to_streaming(transcript=b'{"type":"system","note":"before the kill"}\n')
        self.at(3, lambda: setattr(self.launcher, "state", status("evicted", final=True)))
        with self.assertRaises(RuntimeError):
            self.run_session()
        self.assertIn(b"before the kill", self.jsonl().read_bytes())


# --- the delta -----------------------------------------------------------------------------------------------------------------------------


class DeltaTests(Fresh):
    def snapshot(self):
        return {str(p.relative_to(self.tree)): p.read_bytes() for p in sorted(self.tree.rglob("*")) if p.is_file()}

    def assertRejected(self, stream, count, text="delta_rejected", **report):
        before = self.snapshot()
        self.happy(stream, count, report=report_of(count, data=stream, **report) if report else None)
        with self.assertRaises(AgentError) as caught:
            self.run_session()
        self.assertEqual(str(caught.exception), f"{podbackend.FAILURES[text]}; see {self.prefix}.jsonl and {self.prefix}.stderr")
        self.assertEqual(self.snapshot(), before)
        # The spend is still accounted: the transcript had a result.
        self.assertEqual(caught.exception.usage["total_cost_usd"], 0.5)
        self.assertReleased(delivered=True)
        return caught.exception

    def test_a_delta_that_names_a_git_directory_changes_nothing(self):
        data, count = delta_of(("file", "ok.txt", b"fine\n"))
        # The writer refuses a git name, so the hostile record is written by hand.
        record = struct.pack(">BBH", bundle.FILE, 0, len(b"tree/.git/config")) + b"tree/.git/config" + struct.pack(">Q", 3) + b"x=y"
        hostile = data + record
        self.assertRejected(hostile, 2)

    def test_a_delta_that_walks_out_of_the_tree_changes_nothing(self):
        data, _ = delta_of(("file", "ok.txt", b"fine\n"))
        path = b"tree/../escape.txt"
        record = struct.pack(">BBH", bundle.FILE, 0, len(path)) + path + struct.pack(">Q", 1) + b"x"
        self.assertRejected(data + record, 2)
        self.assertFalse((self.tree.parent / "escape.txt").exists())

    def test_a_link_that_leads_out_of_the_tree_is_refused(self):
        sink = io.BytesIO()
        writer = bundle.Writer(sink, bundle.DELTA)
        writer.put(bundle.SYMLINK, bundle.AREA_TREE, "l", b"../../etc")
        self.assertRejected(sink.getvalue(), 1)

    def test_a_delta_whose_size_differs_from_the_declared_one_is_refused(self):
        data, count = delta_of(("file", "a.txt", b"a\n"), ("file", "b.txt", b"b\n"))
        self.assertRejected(data, count, delta={"status": "ok", "records": count + 1, "bytes": len(data)})

    def test_a_delta_cut_at_a_record_boundary_is_refused_by_the_declared_numbers(self):
        data, count = delta_of(("file", "a.txt", b"a\n"), ("file", "b.txt", b"b\n"))
        first, _ = delta_of(("file", "a.txt", b"a\n"))
        self.happy(first, 1, report=report_of(count, delta_bytes=len(data)))
        with self.assertRaises(AgentError) as caught:
            self.run_session()
        self.assertIn(podbackend.FAILURES["delta_rejected"], str(caught.exception))
        self.assertFalse((self.tree / "a.txt").exists())

    def test_a_read_only_phase_may_not_return_a_delta(self):
        self.phase = "plan"
        data, count = delta_of(("file", "a.txt", b"a\n"))
        self.happy(data, count)
        with self.assertRaises(AgentError) as caught:
            self.run_session(phase="plan")
        self.assertIn(podbackend.FAILURES["delta_rejected"], str(caught.exception))
        self.assertFalse((self.tree / "a.txt").exists())

    def test_a_read_only_phase_with_an_empty_delta_succeeds(self):
        self.phase = "review"
        self.happy()
        result, _ = self.run_session(phase="review")
        self.assertEqual(result, {"ok": True})

    def test_an_oversized_delta_is_a_size_failure_and_nothing_is_applied(self):
        data, count = delta_of(("file", "a.txt", b"a\n"))
        with patch.object(bundle, "apply_delta", side_effect=bundle.DeltaTooLarge("bytes")):
            self.happy(data, count)
            with self.assertRaises(AgentError) as caught:
                self.run_session()
        self.assertIn(podbackend.FAILURES["delta_too_large"], str(caught.exception))

    def test_a_delta_the_system_refused_to_apply_is_its_own_failure(self):
        data, count = delta_of(("file", "a.txt", b"a\n"))
        with patch.object(bundle, "apply_delta", side_effect=bundle.DeltaApplyFailed("write")):
            self.happy(data, count)
            with self.assertRaises(AgentError) as caught:
                self.run_session()
        self.assertIn(podbackend.FAILURES["delta_failed"], str(caught.exception))

    def test_the_delta_is_applied_only_through_the_validating_function_with_the_session_scope_and_numbers(self):
        data, count = delta_of(("file", "a.txt", b"a\n"))
        self.happy(data, count)
        with patch.object(bundle, "apply_delta", wraps=bundle.apply_delta) as applied:
            self.run_session()
        kwargs = applied.call_args.kwargs
        self.assertEqual(applied.call_args.args[1], self.tree)
        self.assertEqual((kwargs["write_scope"], kwargs["declared_records"], kwargs["declared_bytes"]), ("cwd", count, len(data)))
        self.assertEqual(kwargs["scratch"], self.prefix.parent)

    def test_a_report_file_that_is_not_a_json_object_is_a_failure(self):
        # The relay itself refuses a non-object report (400); this is a file that changed after it was committed.
        self.happy()
        self.at(3, lambda: (self.spool / self.session / sr.OUT_DIR / sr.OUT_REPORT).write_bytes(b"[1, 2]"))
        with self.assertRaises(RuntimeError) as caught:
            self.run_session()
        self.assertIn(podbackend.FAILURES["report"], str(caught.exception))

    def test_a_relay_that_gives_a_result_with_no_delta_file_is_a_failure(self):
        self.happy()
        self.at(3, lambda: (self.spool / self.session / sr.OUT_DIR / sr.OUT_DELTA).unlink())
        with self.assertRaises(RuntimeError) as caught:
            self.run_session()
        self.assertIn(podbackend.FAILURES["report"], str(caught.exception))


class ReportTests(Fresh):
    """The report is the closed schema of ``podreport`` and only ``podreport.loads`` reads it."""

    def fails_with(self, report, text="report"):
        self.happy(report=report)
        with self.assertRaises(RuntimeError) as caught:
            self.run_session()
        self.assertIn(podbackend.FAILURES[text], str(caught.exception))

    def test_a_report_is_read_only_by_podreport_and_the_module_has_no_parser_of_its_own(self):
        self.assertFalse(hasattr(podbackend, "parse_report"))
        self.happy()
        with patch.object(podreport, "loads", wraps=podreport.loads) as loaded:
            self.run_session()
        loaded.assert_called_once()

    def test_every_report_that_is_not_the_closed_schema_fails_the_session_and_applies_nothing(self):
        data, count = delta_of(("file", "a.txt", b"a\n"))
        good = report_of(count, data=data)
        broken = {
            "the old flat schema": {"exit_code": 0, "delta_records": count, "delta_bytes": len(data)},
            "a missing key": {k: v for k, v in good.items() if k != "outcome"},
            "an extra key": {**good, "note": "POISON"},
            "an unknown outcome": {**good, "outcome": "crashed"},
            "a bool exit code": {**good, "exit_code": True},
            "an exit code over 255": {**good, "exit_code": 256},
            "text for a number": {**good, "exit_code": "0"},
            "a negative size": {**good, "delta": {"status": "ok", "records": -1, "bytes": len(data)}},
            "a delta that is not ok and declares bytes": {**good, "delta": {"status": "failed", "records": 1, "bytes": 5}},
            "an unknown delta status": {**good, "delta": {"status": "partial", "records": 0, "bytes": 0}},
            "a malformed footprint": {**good, "footprint": {"peak_rss_mb": 1}},
            "a version that is not 1": {**good, "version": 2},
        }
        for name, report in broken.items():
            with self.subTest(name):
                self.setUp()
                before = {str(p) for p in self.tree.rglob("*")}
                self.fails_with(report)
                self.assertEqual({str(p) for p in self.tree.rglob("*")}, before)

    def test_a_negative_exit_code_is_the_signal_that_ended_the_agent_as_it_is_locally(self):
        self.happy(report=report_of(exit_code=-9))
        with self.assertRaises(RuntimeError) as caught:
            self.run_session()
        self.assertEqual(str(caught.exception), f"agent exited -9 after a success result; see {self.prefix}.stderr")

    def test_a_report_over_the_size_the_contract_allows_is_a_failure_whatever_it_says(self):
        self.happy()
        padding = b" " * (podreport.MAX_BYTES + 1)
        self.at(3, lambda: (self.spool / self.session / sr.OUT_DIR / sr.OUT_REPORT).write_bytes(json.dumps(report_of(streamed={"transcript": 0, "stderr": 0})).encode() + padding))
        with self.assertRaises(RuntimeError) as caught:
            self.run_session()
        self.assertIn(podbackend.FAILURES["report"], str(caught.exception))

    def test_bytes_that_are_not_json_are_a_failure_with_no_text_of_theirs(self):
        for raw in (b"\xff\xfe", b"", b"null", b"[]", b"{" * 100000):
            with self.subTest(raw=raw[:10]):
                self.setUp()
                self.happy()
                self.at(3, lambda raw=raw: (self.spool / self.session / sr.OUT_DIR / sr.OUT_REPORT).write_bytes(raw))
                with self.assertRaises(RuntimeError) as caught:
                    self.run_session()
                self.assertIn(podbackend.FAILURES["report"], str(caught.exception))


# --- the relay's output files are data ---------------------------------------------------------------------------------------------------


class OutputTests(Fresh):
    def out_dir(self):
        return self.spool / self.session / sr.OUT_DIR

    def test_a_link_in_place_of_the_transcript_is_refused_and_never_followed(self):
        secret = self.base / "secret.txt"
        secret.write_text("TOP SECRET\n")
        self.happy()

        def swap():
            target = self.out_dir() / sr.OUT_TRANSCRIPT
            target.unlink()
            target.symlink_to(secret)

        self.at(3, swap)
        with self.assertRaises(RuntimeError):
            self.run_session()
        self.assertNotIn(b"TOP SECRET", self.jsonl().read_bytes())

    def test_a_fifo_in_place_of_the_report_is_refused_and_never_waited_on(self):
        self.happy()

        def swap():
            target = self.out_dir() / sr.OUT_REPORT
            target.unlink()
            os.mkfifo(target)

        self.at(3, swap)
        with self.assertRaises(RuntimeError) as caught:
            self.run_session()
        self.assertIn(podbackend.FAILURES["report"], str(caught.exception))

    def test_an_output_over_its_cap_is_refused_and_nothing_is_truncated(self):
        self.happy(transcript=b"x" * 100)
        with patch.object(pc, "TRANSCRIPT_MAX_BYTES", 10):
            with self.assertRaises(RuntimeError) as caught:
                self.run_session()
        self.assertIn(podbackend.FAILURES["output"], str(caught.exception))

    def test_the_copy_is_bounded_chunks_and_not_one_read(self):
        big = b'{"type":"system"}\n' * (4 * podbackend.COPY_BYTES // 17) + result_line()
        self.assertGreater(len(big), 3 * podbackend.COPY_BYTES)
        self.happy(transcript=big)
        sizes = []
        real = os.read

        def spy(descriptor, count):
            sizes.append(count)
            return real(descriptor, count)

        with patch("factory.podbackend.os.read", spy):
            self.run_session()
        self.assertEqual(self.jsonl().read_bytes(), big)
        self.assertLessEqual(max(sizes), podbackend.COPY_BYTES)


# --- the isolation failure and the breaker ------------------------------------------------------------------------------------------------


class IsolationTests(Fresh):
    def fail_canary(self, code="reach_imds"):
        self.at(1, lambda: self.assertEqual(self.pod.preflight("failed", code), 200))

    def test_a_failed_canary_trips_the_breaker_denies_the_launcher_and_fails_the_session(self):
        self.fail_canary("reach_imds")
        with self.assertRaises(AgentError) as caught:
            self.run_session()
        self.assertEqual(str(caught.exception), podbackend.ISOLATION.format(code="reach_imds"))
        self.assertEqual(self.launcher.denied, ["isolation_failed"])
        self.assertEqual(stopworld.read_state(self.data)["code"], stopworld.ISOLATION_FAILED)
        self.assertReleased(delivered=False)

    def test_a_code_the_pod_made_up_is_never_put_in_the_message(self):
        self.fail_canary("send_help_to_eve")
        with self.assertRaises(AgentError) as caught:
            self.run_session()
        self.assertEqual(str(caught.exception), podbackend.ISOLATION.format(code="unknown"))
        self.assertNotIn("eve", str(caught.exception))

    def test_every_code_the_pod_worker_can_post_is_a_known_isolation_code(self):
        from factory import podworker

        posted = set(podworker._FORBIDDEN_CODES.values()) | set(podworker._API_CODES.values())
        posted |= {podworker.CODE_OTHER_FORBIDDEN, podworker.CODE_OTHER_API, podworker.CODE_CONFIG, podworker.CODE_ERROR, podworker.CODE_CONTROL}
        self.assertEqual(posted - set(podbackend.ISOLATION_CODES), set())
        for code in podbackend.ISOLATION_CODES:
            self.assertRegex(code, sr.CODE_PATTERN.pattern)

    def test_a_tripped_isolation_breaker_starts_no_further_session(self):
        self.fail_canary()
        with self.assertRaises(AgentError):
            self.run_session()
        self.launcher.requests.clear()
        with self.assertRaises(AgentError) as caught:
            self.backend.prepare(self.request())
        self.assertEqual(str(caught.exception), podbackend.TRIPPED)
        self.assertEqual(self.launcher.requests, [])

    def test_a_breaker_that_cannot_be_written_does_not_replace_the_failure(self):
        self.fail_canary()
        with patch.object(stopworld.Breaker, "trip", side_effect=OSError("read-only")):
            with self.assertRaises(AgentError) as caught:
                self.run_session()
        self.assertIn("network isolation check failed", str(caught.exception))
        self.assertEqual(self.launcher.denied, ["isolation_failed"])

    def test_a_launcher_that_cannot_deny_does_not_replace_the_failure_and_the_breaker_still_trips(self):
        self.fail_canary()
        self.launcher.deny = lambda reason: (_ for _ in ()).throw(launcher_client.Unavailable())
        with self.assertRaises(AgentError):
            self.run_session()
        self.assertIsNotNone(stopworld.read_state(self.data))

    def test_the_isolation_kind_is_one_the_breaker_trips_on(self):
        self.assertIn(podbackend.ISOLATION_REASON, stopworld.TRIPPING)
        self.assertRegex(podbackend.ISOLATION_REASON, r"[a-z][a-z_]{0,39}")  # the launcher's word pattern for a deny reason


# --- the real launcher over a real socket, on the in-memory Kubernetes API ----------------------------------------------------------------------


class Tap:
    """The real client, told when a launch happens (so the pod gets its credentials) and what pod it got."""

    def __init__(self, case, client):
        self._case, self._client = case, client
        self.terminated = []

    def terminate(self, handle):
        self.terminated.append(handle)
        return self._client.terminate(handle)

    def launch(self, request, token):
        launched = self._client.launch(request, token)
        self._case.on_launch(request["session_id"], token)
        self._case.pod.uid = launched.uid
        return launched

    def __getattr__(self, name):
        return getattr(self._client, name)


class Real(Fixture, ServerHarness):
    """The backend with the real ``LauncherClient`` and ``Launcher`` (admission mirror on), and the fake API's pod lifecycle."""

    poll = 2.0

    def setUp(self):
        ServerHarness.setUp(self)
        self.server_launcher = self.launcher  # the real ``Launcher``; the fixture's ``launcher`` is the client the backend uses
        podhandle.reset()
        self.addCleanup(podhandle.reset)
        self.start_server()
        client = launcher_client.LauncherClient(
            self.config.socket_path, launcher_uid=os.getuid(), sleep=self.clock.advance, clock=self.clock
        )
        self.client = client
        self.build(Tap(self, client), self.clock)
        self.backend = self.make_backend(poll_seconds=self.poll)

    def pod_phase(self):
        pods = self.fake.pods()
        return None if not pods else next(iter(pods.values()))

    def stream_early(self, transcript=None):
        """The pod passes its canary, fetches and streams as soon as it can (ticks 1 and 2)."""
        self.steps_to_streaming(transcript)

    def finish_at(self, tick):
        self.at(tick, lambda: self.pod.finish(report_of(data=EMPTY_DELTA), EMPTY_DELTA))


class RealSuccess(Real):
    scenario = "succeeds"

    def test_a_session_through_the_real_launcher_succeeds_and_leaves_no_pod_or_secret(self):
        self.stream_early()
        # The fake API runs the pod from 105 s; the pod posts its result at 120 s and the API says Succeeded at 200 s.
        self.at(60, lambda: self.pod.finish(report_of(data=EMPTY_DELTA), EMPTY_DELTA))
        result, usage = self.run_session()
        self.assertEqual(result, {"ok": True})
        (started,) = self.started
        self.assertEqual(started.name, pc.pod_name(self.session))
        self.assertEqual(self.fake.pods(), {})
        self.assertEqual(self.fake.secrets(), {})
        self.assertEqual(podhandle.pod_status(started, self.client.reader), "dead")
        self.assertEqual(podhandle.live(), [])
        self.assertReleased(delivered=True)

    def test_the_pod_the_real_launcher_made_carries_the_session_the_backend_asked_for(self):
        self.stream_early()
        seen = []
        self.at(1, lambda: seen.extend(self.fake.pods().values()))
        self.at(60, lambda: self.pod.finish(report_of(data=EMPTY_DELTA), EMPTY_DELTA))
        self.run_session()
        (pod,) = seen
        labels = pod["metadata"]["labels"]
        self.assertEqual(labels[pc.label_key("session")], self.session)
        self.assertEqual(labels[pc.label_key("manager")], INSTANCE)
        self.assertEqual(labels[pc.label_key("repo")], pc.repo_key(REPOSITORY))
        self.assertEqual(labels[pc.label_key("phase")], "build")
        self.assertEqual(labels[pc.label_key("attempt")], "1")

    def test_a_stop_that_deletes_the_pod_mid_session_is_a_failure_and_not_a_hang(self):
        self.stream_early()
        self.at(5, lambda: self.client.terminate(self.started[0]))
        with self.assertRaises(RuntimeError) as caught:
            self.run_session()
        self.assertIn(podbackend.FAILURES["gone"], str(caught.exception))
        self.assertEqual(self.fake.pods(), {})


class RealOom(Real):
    scenario = "oom"

    def test_an_oom_killed_pod_is_the_memory_limit_error(self):
        self.stream_early()
        with self.assertRaises(AgentError) as caught:
            self.run_session()
        self.assertIn("worker exceeded its memory limit", str(caught.exception))
        self.assertEqual(self.fake.pods(), {})


class RealExit(Real):
    scenario = "exit_nonzero"

    def test_a_pod_that_exits_two_is_the_exit_error(self):
        self.stream_early(b"")
        with self.assertRaises(RuntimeError) as caught:
            self.run_session()
        self.assertEqual(str(caught.exception), f"agent exited 2; see {self.prefix}.stderr")

    def test_a_spend_limit_that_ends_a_real_pod_trips_the_error_code_the_breaker_reads(self):
        line = json.dumps({"type": "system", "api_error_status": 429, "error": {"type": "enforced_spend_limit_reached"}}).encode() + b"\n"
        self.stream_early(line)
        with self.assertRaises(AgentError) as caught:
            self.run_session()
        self.assertEqual(caught.exception.api_error, apierror.SPEND_LIMIT)


class RealEvicted(Real):
    scenario = "evicted"

    def test_an_evicted_pod_is_a_failure_with_its_fixed_text(self):
        self.stream_early()
        with self.assertRaises(RuntimeError) as caught:
            self.run_session()
        self.assertIn(podbackend.FAILURES["evicted"], str(caught.exception))


class RealDeadline(Real):
    scenario = "deadline"

    def test_a_pod_the_kubelet_stopped_at_its_deadline_is_the_time_limit_error(self):
        self.stream_early()
        with self.assertRaises(AgentTimeout):
            self.run_session()


class RealNodeDeleted(Real):
    scenario = "node_deleted"

    def test_a_pod_that_vanishes_with_its_node_is_gone_not_succeeded(self):
        self.stream_early()
        with self.assertRaises(RuntimeError) as caught:
            self.run_session()
        self.assertIn(podbackend.FAILURES["gone"], str(caught.exception))


class RealKarpenter(Real):
    scenario = "karpenter_eviction"

    def test_a_pod_that_is_terminating_and_then_gone_is_a_failure_after_its_grace(self):
        self.stream_early()
        with self.assertRaises(RuntimeError) as caught:
            self.run_session()
        self.assertIn(podbackend.FAILURES["gone"], str(caught.exception))
        self.assertGreater(sum(self.sleeps), 55)  # it waited out the pod's termination, which ended at 60 s


class RealUnschedulable(Real):
    scenario = "unschedulable"
    poll = 30.0

    def test_a_pod_that_never_gets_a_node_is_given_up_on_at_the_start_bound(self):
        with self.assertRaises(RuntimeError) as caught:
            self.run_session()
        self.assertIn(podbackend.FAILURES["not_started"], str(caught.exception))
        self.assertEqual(self.fake.pods(), {})
        self.assertReleased(delivered=False)


class RealPullError(Real):
    scenario = "image_pull_backoff"
    poll = 30.0

    def test_a_pod_that_cannot_pull_its_image_is_given_up_on_and_deleted(self):
        with self.assertRaises(RuntimeError):
            self.run_session()
        self.assertEqual(self.fake.pods(), {})


class RealLauncherSweep(Real):
    scenario = "unschedulable"
    poll = 30.0

    def test_a_pod_the_launcher_itself_reaped_is_reported_as_not_scheduled(self):
        def sweep():
            self.server_launcher.sweep()

        self.early.append(sweep)
        self.at(1, sweep)
        for tick in range(2, 40):
            self.at(tick, sweep)
        with self.assertRaises(RuntimeError) as caught:
            self.run_session()
        self.assertIn(podbackend.FAILURES["not_scheduled"], str(caught.exception))


class RealIsolation(Real):
    scenario = "succeeds"

    def test_a_failed_canary_denies_the_real_launcher_and_trips_the_breaker(self):
        self.at(1, lambda: self.pod.preflight("failed", "reach_kubelet"))
        with self.assertRaises(AgentError) as caught:
            self.run_session()
        self.assertEqual(str(caught.exception), podbackend.ISOLATION.format(code="reach_kubelet"))
        self.assertEqual(self.client.info()["deny_reason"], "isolation_failed")
        self.assertEqual(stopworld.read_state(self.data)["code"], stopworld.ISOLATION_FAILED)
        self.assertEqual(self.fake.pods(), {})
        reply = self.server_launcher.handle_line(launch_line(9))
        self.assertEqual((reply["ok"], reply["error"]), (False, "denied"))


class RealCapacity(Real):
    scenario = "succeeds"
    options = {"max_pods": 1}

    def test_a_launcher_at_its_cap_is_waited_on_until_a_slot_frees(self):
        # One pod is already running; the launcher answers `capacity` until it is deleted.
        occupant = self.server_launcher.handle_line(launch_line(77))
        self.assertTrue(occupant["ok"])
        self.early.append(lambda: self.client.delete_pod(occupant["name"]) if self.sleeps.__len__() >= 3 else None)
        self.stream_early()
        self.at(60, lambda: self.pod.finish(report_of(data=EMPTY_DELTA), EMPTY_DELTA))
        result, _ = self.run_session()
        self.assertEqual(result, {"ok": True})
        self.assertGreaterEqual(self.sleeps.count(podbackend.CAPACITY_POLL_SECONDS), 3)


class RealGit(Fixture, unittest.TestCase):
    """The whole ship-in and ship-back with REAL git: a repository, a linked worktree, a pod that extracts the bundle."""

    patch_git = False

    def setUp(self):
        podhandle.reset()
        self.addCleanup(podhandle.reset)
        self.build(FakeLauncher(self), FakeClock(1000.0))
        shutil_rmtree(self.tree)
        self.repo = self.data / "clone"
        self.repo.mkdir()
        git = lambda *args, cwd=self.repo: subprocess.run(
            ["git", "-c", "user.name=T", "-c", "user.email=t@example.invalid", *args], cwd=cwd, text=True, capture_output=True, check=True
        )
        git("init", "-q", "-b", "main")
        (self.repo / "f.txt").write_text("one\ntwo\n")
        (self.repo / "CLAUDE.md").write_text("rules\n")
        git("add", ".")
        git("commit", "-q", "-m", "base")
        self.base_commit = git("rev-parse", "HEAD").stdout.strip()
        git("worktree", "add", "-q", "-b", "job", os.fspath(self.tree))
        (self.tree / "f.txt").write_text("one\ntwo\nthree\n")
        git("add", "--all", cwd=self.tree)
        git("commit", "-q", "-m", "work", cwd=self.tree)
        gitsafe.adopt(self.repo, self.tree)
        self.git = git
        self.backend = self.make_backend(base=self.base_commit)

    def pod_extract(self):
        root = self.base / "pod"
        root.mkdir(exist_ok=True)
        with io.BytesIO(self.fetched) as source:
            extracted = bundle.extract(source, root)
        return root / extracted.worktree

    def test_the_pod_gets_a_git_directory_that_answers_like_the_supervisors_and_its_edit_comes_back(self):
        answers = {}

        def work():
            self.pod.preflight()
            self.fetched = self.pod.fetch()
            tree = self.pod_extract()
            marker = os.path.realpath(tree / ".git")
            for name, args in {"log": ("log", "--format=%H %s", f"{self.base_commit}..HEAD"), "show": ("show", "HEAD:CLAUDE.md"), "head": ("rev-parse", "HEAD")}.items():
                answers[name] = gitsafe.run(list(args), tree, marker, check=False).stdout
            self.pod_tree = tree

        def edit():
            self.pod.chunk("transcript", result_line())
            data, count = delta_of(("file", "f.txt", b"one\ntwo\nthree\nfour\n"), ("file", "new.txt", b"new\n"))
            self.pod.finish(report_of(count, data=data), data)
            self.launcher.state = SUCCEEDED
            self.delta = data

        self.at(1, work)
        self.at(2, edit)
        result, _ = self.run_session()
        self.assertEqual(result, {"ok": True})
        here = lambda *args: self.git(*args, cwd=self.tree).stdout
        self.assertEqual(answers["log"], here("log", "--format=%H %s", f"{self.base_commit}..HEAD"))
        self.assertEqual(answers["show"], here("show", "HEAD:CLAUDE.md"))
        self.assertEqual(answers["head"], here("rev-parse", "HEAD"))
        self.assertEqual((self.tree / "f.txt").read_text(), "one\ntwo\nthree\nfour\n")
        self.assertEqual((self.tree / "new.txt").read_text(), "new\n")
        self.assertEqual(here("status", "--porcelain"), " M f.txt\n?? new.txt\n")
        # The supervisor's own git directory was not touched by the delta (it names no git path at all).
        self.assertEqual(here("rev-parse", "HEAD"), answers["head"])
        self.assertEqual(os.path.islink(self.tree / ".git"), False)
        self.assertTrue((self.tree / ".git").is_file())

    def test_a_delta_that_replaces_the_git_pointer_is_refused_and_the_pointer_is_unchanged(self):
        pointer = (self.tree / ".git").read_bytes()
        record = struct.pack(">BBH", bundle.FILE, 0, len(b"tree/.git")) + b"tree/.git" + struct.pack(">Q", 9) + b"gitdir: x"
        data = bundle.HEADER + record
        self.steps_to_streaming()
        self.at(3, lambda: self.pod.finish(report_of(1, data=data), data))
        with self.assertRaises(AgentError) as caught:
            self.run_session()
        self.assertIn(podbackend.FAILURES["delta_rejected"], str(caught.exception))
        self.assertEqual((self.tree / ".git").read_bytes(), pointer)


def shutil_rmtree(path):
    import shutil

    shutil.rmtree(path)


# --- selecting and wiring the backend --------------------------------------------------------------------------------------------------------


SWITCH = pc.ENV_MANAGER["pod_backend"]


class SelectionTests(unittest.TestCase):
    def setUp(self):
        podbackend.clear_scope()
        self.addCleanup(podbackend.clear_scope)
        self.addCleanup(podbackend.clear_pool)
        podbackend.set_pool(object())  # the pool ``activate`` built; any object stands for it here
        self.env = {
            SWITCH: "on",
            pc.MANAGER_INSTANCE_ENV: INSTANCE,
            pc.ENV_MANAGER["launcher_socket"]: "/run/factory/launcher.sock",
            pc.ENV_MANAGER["spool_dir"]: "/spool",
        }

    def choose(self, env=None, **session):
        values = dict(job_id="job-a", attempt=1, base=None)
        values.update(session)
        return podexecution.backend_for("pod", self.env if env is None else env, **values)

    def test_the_switch_on_a_scope_and_a_complete_environment_select_the_pod_backend(self):
        podbackend.set_scope(REPOSITORY, RUN_ID)
        chosen = self.choose()
        self.assertIsInstance(chosen, podbackend.PodBackend)
        kwargs = podexecution.agent_kwargs("pod", self.env, job_id="job-a", attempt=1)
        self.assertEqual(set(kwargs), {"backend"})
        self.assertIsInstance(kwargs["backend"], podbackend.PodBackend)

    def test_the_switch_off_is_still_a_refusal_whatever_else_is_set(self):
        podbackend.set_scope(REPOSITORY, RUN_ID)
        for value in (None, "", "off", "ON", "true", " on"):
            with self.subTest(value=value):
                env = {k: v for k, v in self.env.items() if k != SWITCH}
                if value is not None:
                    env[SWITCH] = value
                chosen = self.choose(env)
                self.assertIsInstance(chosen, podexecution.RefusedBackend)
                self.assertEqual(chosen.reason, podexecution.REFUSED_SWITCH_OFF)

    def test_inprocess_is_the_local_backend_whatever_the_session_describes(self):
        podbackend.set_scope(REPOSITORY, RUN_ID)
        self.assertIsNone(podexecution.backend_for("inprocess", self.env, job_id="job-a", attempt=1, base="x"))
        self.assertEqual(podexecution.agent_kwargs("inprocess", self.env, job_id="job-a", attempt=1), {})

    def test_every_missing_or_invalid_part_is_the_unconfigured_refusal_never_in_process(self):
        podbackend.set_scope(REPOSITORY, RUN_ID)
        broken = {
            "no manager instance": lambda e: e.pop(pc.MANAGER_INSTANCE_ENV),
            "a bad manager instance": lambda e: e.update({pc.MANAGER_INSTANCE_ENV: "XYZ"}),
            "a relative socket": lambda e: e.update({pc.ENV_MANAGER["launcher_socket"]: "launcher.sock"}),
            "a data directory that is not the pod's": lambda e: e.update({"FACTORY_DATA": "/mnt/other"}),
        }
        for name, change in broken.items():
            with self.subTest(name):
                env = dict(self.env)
                change(env)
                chosen = self.choose(env)
                self.assertIsInstance(chosen, podexecution.RefusedBackend)
                self.assertEqual(chosen.reason, podexecution.REFUSED_UNCONFIGURED)
        for name, session in {
            "no job": dict(job_id=None),
            "a bad job": dict(job_id="../x"),
            "no attempt": dict(attempt=None),
            "a bool attempt": dict(attempt=True),
            "a base that is not a commit": dict(base="main"),
        }.items():
            with self.subTest(name):
                chosen = self.choose(**session)
                self.assertEqual(getattr(chosen, "reason", None), podexecution.REFUSED_UNCONFIGURED)

    def test_without_a_pool_the_session_is_refused_never_run_unguarded(self):
        podbackend.set_scope(REPOSITORY, RUN_ID)
        podbackend.clear_pool()
        chosen = self.choose()
        self.assertEqual(chosen.reason, podexecution.REFUSED_UNCONFIGURED)

    def test_without_a_scope_the_session_is_refused(self):
        chosen = self.choose()
        self.assertEqual(chosen.reason, podexecution.REFUSED_UNCONFIGURED)

    def test_a_scope_that_is_not_a_valid_id_is_refused_when_it_is_set(self):
        for repository, run in (("not a repo", RUN_ID), (REPOSITORY, "../run"), (None, RUN_ID), (REPOSITORY, None), ("", "")):
            with self.subTest(repository=repository, run=run):
                with self.assertRaises(podbackend.PodConfigError):
                    podbackend.set_scope(repository, run)
        self.assertIsNone(podbackend.current_scope())

    def test_the_refusal_still_reaches_the_caller_on_the_error_path_with_nothing_started(self):
        chosen = self.choose()
        with self.assertRaises(AgentError):
            chosen.prepare(None)

    def test_nothing_of_the_pod_backend_is_imported_while_the_switch_is_off(self):
        code = (
            "import sys\n"
            "from factory import cli, runner, planner, preflight, podexecution, backend, agent\n"
            "assert podexecution.agent_kwargs('inprocess', {}, job_id='a', attempt=1) == {}\n"
            "assert podexecution.backend_for('pod', {}).reason == podexecution.REFUSED_SWITCH_OFF\n"
            "assert podexecution.activate('o/r', 'run-1', {})() is None\n"
            "assert 'factory.podbackend' not in sys.modules, 'podbackend was imported'\n"
            "assert 'factory.launcher_client' not in sys.modules\n"
        )
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        env.pop(SWITCH, None)
        done = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(done.returncode, 0, done.stderr)


class ActivationTests(unittest.TestCase):
    def setUp(self):
        podhandle.reset()
        podbackend.clear_scope()
        self.addCleanup(podhandle.reset)
        self.addCleanup(podbackend.clear_scope)
        self.hooks = []
        patcher = patch("factory.locks.register_kill_hook", side_effect=lambda hook: self.hooks.append(hook) or hook)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("factory.locks.unregister_kill_hook", side_effect=lambda hook: self.hooks.remove(hook))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_activate_installs_the_reader_and_terminator_the_kill_hook_and_the_scope(self):
        client = FakeLauncher(Case())
        undo = podbackend.activate(REPOSITORY, RUN_ID, {}, client=client)
        handle = podhandle.PodHandle(pc.pod_name("1" * 32), POD_UID, "1" * 32, "launcher", "2026-01-01T00:00:00Z")
        self.assertEqual(podhandle.pod_status(handle.record()), "alive")  # the installed reader is the client's
        self.assertTrue(podhandle.terminate_pod(handle))
        self.assertEqual(client.terminated, [handle])
        self.assertEqual(len(self.hooks), 1)
        self.assertEqual(podbackend.current_scope(), podbackend.Scope(REPOSITORY, RUN_ID))
        undo()
        self.assertEqual(self.hooks, [])
        self.assertIsNone(podbackend.current_scope())
        self.assertEqual(podhandle.pod_status(handle.record()), "unknown")

    def test_the_kill_hook_terminates_every_registered_pod_and_returns_after_it_has(self):
        client = FakeLauncher(Case())
        podbackend.activate(REPOSITORY, RUN_ID, {}, client=client)
        handles = [podhandle.PodHandle(pc.pod_name(str(n) * 32), POD_UID, str(n) * 32, "launcher", "2026-01-01T00:00:00Z") for n in (1, 2)]
        for handle in handles:
            podhandle.register(handle)
        self.assertEqual(self.hooks[0](), 0)
        self.assertEqual(sorted(h.session for h in client.terminated), sorted(h.session for h in handles))

    def test_an_invalid_repository_leaves_the_scope_unset_but_still_installs_the_seam(self):
        client = FakeLauncher(Case())
        podbackend.activate("", "", {}, client=client)
        self.assertIsNone(podbackend.current_scope())
        self.assertEqual(len(self.hooks), 1)

    def test_an_unusable_socket_path_wires_nothing_and_says_so_in_one_fixed_line(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            undo = podbackend.activate(REPOSITORY, RUN_ID, {pc.ENV_MANAGER["launcher_socket"]: "relative.sock"})
        self.assertEqual(err.getvalue(), "factory: pod backend not wired in: the launcher socket path is not valid\n")
        self.assertEqual((self.hooks, podbackend.current_scope()), ([], None))
        self.assertIsNone(undo())

    def test_the_switch_on_wires_it_through_podexecution_and_the_switch_off_does_not(self):
        with patch.object(podbackend, "activate", return_value=lambda: None) as wired:
            podexecution.activate(REPOSITORY, RUN_ID, {SWITCH: "off"})
            wired.assert_not_called()
            podexecution.activate(REPOSITORY, RUN_ID, {SWITCH: "on"})
            wired.assert_called_once()

    def test_the_command_runner_wires_it_in_before_its_try_and_undoes_it_in_the_finally(self):
        tree = ast.parse((ROOT / "src" / "factory" / "cli.py").read_text())
        run = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run")
        calls = [n for n in ast.walk(run) if isinstance(n, ast.Call) and ast.unparse(n.func) == "podexecution.activate"]
        self.assertEqual(len(calls), 1)
        trys = [n for n in run.body if isinstance(n, ast.Try)]
        self.assertTrue(any("deactivate_pods()" in ast.unparse(final) for t in trys for final in t.finalbody))
        self.assertLess(calls[0].lineno, trys[-1].lineno)


class DocumentationTests(Case):
    """The design record says what the code does: the keys it writes and reads, the texts, the row of the program table."""

    doc = (ROOT / "docs" / "pod-backend.md").read_text(encoding="utf-8")

    def section(self):
        start = self.doc.index("## The pod backend (slice 12b)")
        return self.doc[start : self.doc.index("\n## ", start + 5)]

    def test_the_section_names_every_key_of_the_manifest_run_section_and_of_the_report(self):
        spec = self.backend.prepare(self.request())
        text = self.section()
        for key in self.backend.manifest(spec)["run"]:
            with self.subTest(key=key):
                self.assertIn(f"`{key}`", text)
        for key in (*podreport.KEYS, *podreport.DELTA_KEYS, *podreport.FOOTPRINT_KEYS, *podreport.OUTCOMES, *podreport.DELTA_STATUSES):
            with self.subTest(key=key):
                self.assertIn(f"`{key}`", text)

    def test_the_section_names_every_fixed_failure_in_its_table_of_verdicts(self):
        text = self.section()
        for word in ("evicted", "gone", "succeeded", "unschedulable", "starting_error", "terminating", "preflight_failed", "deadline", "oom", "failed_exit"):
            with self.subTest(word=word):
                self.assertIn(word, text)
        for stage in ("key", "spawn", "result", "gone", "run", "canary"):
            with self.subTest(stage=stage):
                self.assertIn(f"`{stage}`", text)
        for state in podliveness.STATES:
            with self.subTest(state=state):
                self.assertIn(f"`{state}`", text)
        for reason in podpool.REFUSALS:
            with self.subTest(refusal=reason):
                self.assertIn(f"`{reason}`", text)

    def test_the_numbers_the_section_quotes_are_the_ones_in_the_code(self):
        text = self.section()
        self.assertIn(f"({podbackend.START_BOUND_SECONDS} s", text)
        self.assertIn(f"`ORPHAN_SILENCE_SECONDS` ({pc.ORPHAN_SILENCE_SECONDS} s)", text)
        self.assertIn(f"{podbackend.TERMINATING_WAIT_SECONDS} s", text)
        self.assertIn(f"every {int(podbackend.POLL_SECONDS)} s", text)
        self.assertIn(f"every {int(podbackend.CAPACITY_POLL_SECONDS)} s", text)
        self.assertIn(f"`TIMEOUT_MARGIN_SECONDS` ({podbackend.TIMEOUT_MARGIN_SECONDS} s", text)

    def test_the_program_table_and_the_architecture_reference_and_the_unverified_list_carry_the_slice(self):
        row = next(line for line in self.doc.splitlines() if line.startswith("| 12a, 12b |"))
        self.assertIn("12b, delivered", row)
        self.assertIn("both off by default", row)
        self.assertIn("* Slice 12b: see", self.doc)
        self.assertIn("`podbackend.py`", (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8"))

    def test_the_section_says_what_the_other_slices_still_have_to_do(self):
        text = self.section()
        for word in ("manager.pods", "spent_usd", "budget_usd", "podsweep.protect", "slice 17", "UNVERIFIED", "contract-tested", "podworker.RUN_KEYS", "podreport.loads", "podpool.hold", "podliveness.enforce"):
            with self.subTest(word=word):
                self.assertIn(word, text)

    def test_every_module_the_section_calls_by_name_exists(self):
        for name in re.findall(r"`([a-z_]+\.py)`", self.section()):
            with self.subTest(name=name):
                self.assertTrue((ROOT / "src" / "factory" / name).exists() or (ROOT / "tests" / name).exists(), name)


class ShapeTests(unittest.TestCase):
    """Properties of the source that the behaviour tests cannot see."""

    source = (ROOT / "src" / "factory" / "podbackend.py").read_text()

    def test_it_is_standard_library_only_and_runs_no_process_and_opens_no_socket_of_its_own(self):
        tree = ast.parse(self.source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                imported.add((node.module or "").split(".")[0])
        self.assertEqual(imported & {"subprocess", "socket", "http", "urllib", "requests", "ssl", "shlex"}, set())
        self.assertEqual(imported - {"base64", "contextlib", "dataclasses", "functools", "json", "math", "os", "re", "shutil", "stat", "sys", "threading", "time", "pathlib"}, set())

    def test_it_never_prints_the_token_a_path_or_agent_text(self):
        tree = ast.parse(self.source)
        prints = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "print"]
        self.assertEqual(len(prints), 2)  # the two fixed lines of activate
        for call in prints:
            self.assertIsInstance(call.args[0], ast.Constant)  # a literal: no value can be in it

    def test_the_token_is_a_local_variable_of_one_function_and_is_deleted_after_the_launch(self):
        tree = ast.parse(self.source)
        holders = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and "token" in n.attr.lower() and isinstance(n.ctx, ast.Store)]
        self.assertEqual(holders, [])
        self.assertIn("del token", self.source)

    def test_no_reader_indexes_the_pod_key_of_a_job(self):
        self.assertNotRegex(self.source, r"\[['\"]pod['\"]\]")

    def test_the_fixed_texts_hold_no_placeholder_but_the_two_closed_codes(self):
        for name in ("TRIPPED", "OUTSIDE", "CAPACITY", "SPOOL", "LAUNCHER_UNTRUSTED", "LAUNCHER_DOWN", "RECORD", "BOUND", *podbackend.FAILURES):
            text = getattr(podbackend, name, None) or podbackend.FAILURES[name]
            self.assertNotIn("{", text)
        self.assertEqual(re.findall(r"\{(\w+)\}", podbackend.ISOLATION), ["code"])
        self.assertEqual(re.findall(r"\{(\w+)\}", podbackend.CUT_OFF), ["code"])

    def test_the_cut_off_codes_are_the_relays_closed_set(self):
        for code in sr.REJECT_CODES:
            self.assertRegex(podbackend.CUT_OFF.format(code=code), r"^[a-z ()_]+$")


if __name__ == "__main__":
    unittest.main()

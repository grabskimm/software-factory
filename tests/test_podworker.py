"""The pod worker wrapper, part A (src/factory/podworker.py): fixed output, network canary, fetch and extract.

Slice 10a of the pod backend (docs/pod-backend.md). CONTRACT-TESTED: the wrapper runs against the in-process relay
of slice 8 (a real `RelayServer` on 127.0.0.1), against a scripted HTTP server where the relay's own answers are
not enough (a 425 that never ends, a poisoned answer, a truncated body), against a fake network for the canary and
against real temporary directories. No container ran, no cluster was consulted, and `chown` is patched (the tests
run unprivileged). Every thread has a bounded join, no test sleeps, and the retry backoff runs on an injected clock.

The text POISON stands for what must never reach a log: it is put into every place an error text or a response
could carry it (the bundle, the relay's answer, an exception raised at each stage, the environment, the token
file), and every run is checked on BOTH standard output and standard error against a whitelist of fixed lines,
and for never having opened the container's termination message file.
"""

import ast
import base64
import builtins
import contextlib
import faulthandler
import hashlib
import http.client
import http.server
import io
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import ssl
import tempfile
import threading
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock

from factory import bundle
from factory import podcontract as pc
from factory import podspec, podworker, relay
from factory import sessionrecord as sr

POISON = "POISON-do-not-leak-91c4e7"
UID = "11111111-2222-3333-4444-555555555555"
INSTANCE = "abcdef012345"
REPO = pc.repo_key("octo/widgets")
NODE_IP = "10.240.0.7"
MANAGER = "10.244.0.9"
API_HOST = "api.example.invalid"
SERVICE_HOST = "10.0.0.1"
WORKTREE = "work/tree"
IDENTITY = (4242, 4343)
TERMINATION = "/dev/termination-log"
ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "src" / "factory" / "podworker.py"
LINE = re.compile(r"podworker: (started|preflight ok|bundle fetched|exit [0-9]{1,3}|result posted|failed ([a-z]+))")


def sha(data):
    return hashlib.sha256(data).digest()


def raw_record(kind, path, size, payload=b"", flags=0):
    """One record written by hand, so that a test can break it on purpose."""
    return b"".join((struct.pack(">BBH", kind, flags, len(path)), path, struct.pack(">Q", size), payload))


def raw_bundle(manifest, *records):
    data = json.dumps(manifest).encode("ascii")
    return b"".join((bundle.HEADER, raw_record(bundle.FILE, b"meta/manifest.json", len(data), data), *records))


def make_bundle(manifest=None, tree=None, reads=None, executable=()):
    """A well-formed bundle: manifest, read-only files, a git file, the tree and one baseline record per tree path."""
    sink = io.BytesIO()
    writer = bundle.Writer(sink)
    writer.put_manifest({"version": 1, "worktree": WORKTREE, **(manifest or {})})
    for path, data in (reads or {}).items():
        writer.put(bundle.FILE, bundle.AREA_READ, path, data)
    writer.put(bundle.FILE, bundle.AREA_GIT, "HEAD", b"ref: refs/heads/main\n")
    files = {"README": b"hello\n", "src/a.py": b"print(1)\n"} if tree is None else tree
    for path, data in files.items():
        writer.put(bundle.FILE, bundle.AREA_TREE, path, data, path in executable)
    for path, data in files.items():
        writer.put_baseline(path, bundle.FILE, sha(data), path in executable)
    return sink.getvalue()


class RealClock:
    """The relay's clock: real time (the tests use no relay deadline)."""

    @staticmethod
    def time():
        return time.time()

    @staticmethod
    def monotonic():
        return time.monotonic()


class Clock:
    """The wrapper's clock: `sleep` only moves a number (and may run a hook), so no test waits."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []
        self.on_sleep = None

    def monotonic(self):
        return self.now

    @staticmethod
    def time():
        return time.time()

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep()


class FakeNet:
    """The canary's network: which destinations accept, what the API servers answer, and a record of every call."""

    def __init__(self, relay_address, events=None):
        self.accepts = {relay_address}
        self.api = {(SERVICE_HOST, 443): 401, (API_HOST, 443): 403}
        self.calls = []
        self.timeouts = set()
        self.barrier = None
        self.paths = []
        self.raises = None
        self.hold = None
        self.hold_host = None
        self.events = events if events is not None else []

    def _enter(self, host, port, timeout):
        self.calls.append((host, port))
        self.events.append(("net", host, port))
        self.timeouts.add(timeout)
        if self.barrier is not None:
            self.barrier.wait()
        if self.raises is not None:
            raise self.raises
        if self.hold is not None and host == self.hold_host:
            self.hold.wait(10)

    def connect(self, host, port, timeout):
        self._enter(host, port, timeout)
        return (host, port) in self.accepts

    def status(self, host, port, path, timeout):
        self._enter(host, port, timeout)
        self.paths.append(path)
        return self.api.get((host, port))


class Reply:
    """One scripted answer of ScriptedRelay."""

    def __init__(self, status=200, body=b"", reason=None, length=None, omit_length=False):
        self.status = status
        self.body = body
        self.reason = reason
        self.length = length
        self.omit_length = omit_length


class _ScriptedHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        return None

    def _answer(self):
        size = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(size) if size else b""
        self.server.log.append((self.command, self.path, dict(self.headers), body))
        self.server.events.append(("relay", self.command, self.path))
        queue = self.server.script.get((self.command, self.path), [])
        reply = queue.pop(0) if len(queue) > 1 else (queue[0] if queue else Reply(404))
        self.send_response(reply.status, reply.reason)
        if not reply.omit_length:
            self.send_header("Content-Length", str(len(reply.body) if reply.length is None else reply.length))
        self.end_headers()
        self.wfile.write(reply.body)

    do_GET = do_POST = _answer


class ScriptedRelay:
    """A tiny HTTP server that answers from a script: for each (method, path) a list of replies, the last one repeating."""

    def __init__(self, case, script, events=None):
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _ScriptedHandler)
        self.server.script = script
        self.server.log = []
        self.server.events = events if events is not None else []
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True)
        thread.start()

        def stop():
            self.server.shutdown()
            self.server.server_close()
            thread.join(10)

        case.addCleanup(stop)

    @property
    def log(self):
        return self.server.log

    def requests(self, method, path):
        return [entry for entry in self.log if entry[:2] == (method, path)]


class Result:
    def __init__(self, code, out, err, opened, prepared):
        self.code = code
        self.out = out
        self.err = err
        self.opened = opened
        self.prepared = prepared

    @property
    def lines(self):
        return [*self.out.splitlines(), *self.err.splitlines()]

    @property
    def text(self):
        return f"{self.out}{self.err}"


@contextlib.contextmanager
def recording_opens():
    """Every path handed to `open` or `os.open` while the block runs (the real call still happens)."""
    opened = []
    real_open, real_os_open = builtins.open, os.open

    def spy_open(file, *args, **keywords):
        opened.append(file if isinstance(file, int) else os.fsdecode(file))
        return real_open(file, *args, **keywords)

    def spy_os_open(path, *args, **keywords):
        opened.append(os.fsdecode(path))
        return real_os_open(path, *args, **keywords)

    with mock.patch("builtins.open", spy_open), mock.patch("os.open", spy_os_open):
        yield opened


class Base(unittest.TestCase):
    """A spool with a real relay, a pod data directory, a Secret volume directory and the wrapper's seams."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(os.path.realpath(self.temp.name))
        self.spool = self.base / "spool"
        self.pod = self.base / "pod"
        self.secrets = self.base / "secrets"
        self.outside = self.base / "outside"
        for directory in (self.spool, self.pod, self.secrets, self.outside):
            directory.mkdir()
        os.chown(self.spool, -1, os.getgid())
        os.chmod(self.spool, relay.SPOOL_MODE)
        self.relay = relay.Relay(self.spool, clock=RealClock(), owner=(os.getuid(), os.getgid()))
        self.addCleanup(self.relay.close)
        self.clock = Clock()
        self.events = []
        self.chowns = []
        self.server = None
        self.port = None
        self.net = None

    # -- the relay ---------------------------------------------------------------------------------------------

    def start_relay(self):
        server = relay.RelayServer(("127.0.0.1", 0), self.relay)
        thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(10)

        self.addCleanup(stop)
        self.server = server
        self.port = server.server_address[1]
        return self.port

    def make_session(self, bound=True, data=None):
        session = pc.new_session_id()
        token = sr.new_token()
        sr.write(self.spool, sr.build(session, token, int(time.time()) + 3600, REPO, "cwd", INSTANCE))
        with sr.session_dir(self.spool, session) as fd:
            in_fd = sr.ensure_dir_at(fd, sr.IN_DIR)
            handle = os.open(sr.BUNDLE_FILE, os.O_WRONLY | os.O_CREAT, sr.FILE_MODE, dir_fd=in_fd)
            os.write(handle, make_bundle() if data is None else data)
            os.close(handle)
            os.close(in_fd)
        if bound:
            self.bind(session)
        return session, token

    def bind(self, session):
        sr.bind(self.spool, session, pc.pod_name(session), UID)

    def state(self, session):
        return sr.read_state(self.spool, session)

    # -- the pod ------------------------------------------------------------------------------------------------

    def write_token(self, token, mode=0o400):
        path = self.secrets / "token"
        path.unlink(missing_ok=True)
        path.write_text(token)
        os.chmod(path, mode)

    def environment(self, session, port=None, **changes):
        relay_port = self.port if port is None else port
        env = {
            "FACTORY_SESSION_ID": session,
            "FACTORY_RELAY_URL": f"http://127.0.0.1:{relay_port}",
            "FACTORY_SESSION_DEADLINE": str(int(time.time()) + 3600),
            "FACTORY_POD_NAME": pc.pod_name(session),
            "FACTORY_POD_UID": UID,
            "FACTORY_NODE_IP": NODE_IP,
            "FACTORY_MANAGER_ADDRESS": MANAGER,
            "FACTORY_MANAGER_METRICS_ADDRESS": MANAGER,
            "FACTORY_API_PUBLIC_HOST": API_HOST,
            "KUBERNETES_SERVICE_HOST": SERVICE_HOST,
            "KUBERNETES_SERVICE_PORT": "443",
            "AZMON_COLLECT_ENV": "False",
        }
        env.update(changes)
        return {key: value for key, value in env.items() if value is not None}

    def lchown(self, path, uid, gid):
        self.chowns.append((os.fspath(path), uid, gid))

    def clean_pod(self):
        shutil.rmtree(self.pod)
        self.pod.mkdir()

    def run_worker(self, session, token, port=None, env=None, net=None, runner="stub", argv=("podworker",), fresh=True, **seams):
        """`podworker.main` with the seams of a test; the Result carries both streams and every path opened.

        Each run starts with an empty data directory and an empty record of chown calls unless `fresh` is False.
        """
        relay_port = self.port if port is None else port
        if fresh:
            self.clean_pod()
            self.chowns = []
        if token is not None:
            self.write_token(token)
        network = FakeNet(("127.0.0.1", relay_port), self.events) if net is None else net
        self.net = network
        prepared = []

        def stub(found):
            prepared.append(found)
            return 0

        seams.setdefault("root", self.pod)
        seams.setdefault("secret_dir", self.secrets)
        seams.setdefault("clock", self.clock)
        seams.setdefault("lchown", self.lchown)
        seams.setdefault("identity", IDENTITY)
        if runner is not None:
            seams["runner"] = stub if runner == "stub" else runner
        out, err = io.StringIO(), io.StringIO()
        with recording_opens() as opened, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = podworker.main(list(argv), self.environment(session, relay_port) if env is None else env, net=network, **seams)
        return Result(code, out.getvalue(), err.getvalue(), opened, prepared[0] if prepared else None)

    def assert_fixed(self, result, *, code=None, stage=None):
        """Only whitelisted lines on both streams, no poison, no termination file; optionally the code and stage."""
        for line in result.lines:
            with self.subTest(line=line):
                self.assertRegex(line, LINE)
        self.assertNotIn(POISON, result.text)
        self.assertEqual([path for path in result.opened if isinstance(path, str) and "termination" in path], [])
        self.assertNotIn(TERMINATION, result.opened)
        if code is not None:
            self.assertEqual(result.code, code)
        if stage is not None:
            self.assertEqual(result.code, podworker.STAGES[stage])
            self.assertEqual(result.err, f"podworker: failed {stage}\n")
            self.assertEqual(result.err.count("\n"), 1)

    def assert_nothing_extracted(self):
        self.assertEqual(os.listdir(self.pod), [])
        self.assertEqual(os.listdir(self.outside), [])


class Success(Base):
    def test_a_session_runs_the_canary_fetches_once_and_lays_out_the_tree(self):
        self.start_relay()
        session, token = self.make_session()
        result = self.run_worker(session, token)
        self.assert_fixed(result, code=0)
        self.assertEqual(result.out.splitlines(), ["podworker: started", "podworker: preflight ok", "podworker: bundle fetched"])
        self.assertEqual(result.err, "")
        worktree = self.pod / WORKTREE
        self.assertEqual((worktree / "README").read_bytes(), b"hello\n")
        self.assertEqual((worktree / "src" / "a.py").read_bytes(), b"print(1)\n")
        self.assertTrue((worktree / ".git" / "HEAD").is_file())
        self.assertTrue((worktree / ".git" / "objects").is_dir())
        self.assertEqual(self.state(session)["state"], "fetched")
        self.assertEqual(self.state(session)["fetch_attempts"], 1)

    def test_part_b_receives_the_session_the_client_and_the_baseline_and_no_token_leaks_through_them(self):
        self.start_relay()
        session, token = self.make_session()
        result = self.run_worker(session, token)
        prepared = result.prepared
        self.assertEqual(prepared.worktree, f"{self.pod}/{WORKTREE}")
        self.assertEqual(prepared.root, os.fspath(self.pod))
        self.assertEqual(sorted(prepared.baseline), ["README", "src/a.py"])
        self.assertEqual(prepared.config.session, session)
        self.assertEqual(prepared.config.pod_name, pc.pod_name(session))
        self.assertEqual(prepared.manifest["version"], 1)
        self.assertEqual(prepared.identity, IDENTITY)
        self.assertNotIn(token, repr(prepared))
        self.assertNotIn(token, repr(prepared.client))
        self.assertNotIn(token, result.text)
        self.assertNotIn(token, " ".join(os.environ.values()))

    def test_the_default_execute_of_part_a_fails_closed(self):
        self.start_relay()
        session, token = self.make_session()
        result = self.run_worker(session, token, runner=None)
        self.assert_fixed(result, stage="run")
        self.assertEqual(result.out.splitlines()[-1], "podworker: bundle fetched")

    def test_the_canary_finishes_before_anything_is_asked_of_the_relay_and_the_bundle_is_fetched_once(self):
        script = {
            ("POST", "/v1/preflight"): [Reply(200, b'{"state":"preflighted"}')],
            ("GET", "/v1/bundle"): [Reply(200, make_bundle())],
        }
        scripted = ScriptedRelay(self, script, self.events)
        session = pc.new_session_id()
        net = FakeNet(("127.0.0.1", scripted.port), self.events)
        result = self.run_worker(session, sr.new_token(), port=scripted.port, net=net)
        self.assert_fixed(result, code=0)
        kinds = [event[0] for event in self.events]
        self.assertEqual(kinds, [*(["net"] * 9), "relay", "relay"])
        self.assertEqual([event[1:] for event in self.events[9:]], [("POST", "/v1/preflight"), ("GET", "/v1/bundle")])
        self.assertEqual(len(scripted.requests("GET", "/v1/bundle")), 1)

    def test_the_relay_credentials_travel_in_headers_only(self):
        script = {("POST", "/v1/preflight"): [Reply(200)], ("GET", "/v1/bundle"): [Reply(200, make_bundle())]}
        scripted = ScriptedRelay(self, script)
        session, token = pc.new_session_id(), sr.new_token()
        result = self.run_worker(session, token, port=scripted.port)
        self.assert_fixed(result, code=0)
        for _method, path, headers, body in scripted.log:
            with self.subTest(path=path):
                self.assertEqual(headers["Authorization"], f"Bearer {token}")
                self.assertEqual(headers[podworker.HEADER_POD_NAME], pc.pod_name(session))
                self.assertEqual(headers[podworker.HEADER_POD_UID], UID)
                self.assertNotIn(token, path)
                self.assertNotIn(token.encode("ascii"), body)
        self.assertEqual(json.loads(scripted.requests("POST", "/v1/preflight")[0][3]), {"result": "ok"})

    def test_a_second_run_of_the_same_session_is_refused_and_fetches_nothing(self):
        self.start_relay()
        session, token = self.make_session()
        self.assertEqual(self.run_worker(session, token).code, 0)
        again = self.run_worker(session, token)
        self.assert_fixed(again, stage="preflight")
        self.assertEqual(self.state(session)["fetch_attempts"], 1)
        self.assertEqual(os.listdir(self.pod), [])

    def test_the_real_chown_works_with_the_current_identity(self):
        self.start_relay()
        session, token = self.make_session()
        data = make_bundle(tree={"a.txt": b"a"})
        with sr.session_dir(self.spool, session) as fd:
            in_fd = sr.ensure_dir_at(fd, sr.IN_DIR)
            os.unlink(sr.BUNDLE_FILE, dir_fd=in_fd)
            handle = os.open(sr.BUNDLE_FILE, os.O_WRONLY | os.O_CREAT, sr.FILE_MODE, dir_fd=in_fd)
            os.write(handle, data)
            os.close(handle)
            os.close(in_fd)
        result = self.run_worker(session, token, lchown=os.lchown, identity=(os.getuid(), os.getgid()))
        self.assert_fixed(result, code=0)
        self.assertEqual(os.lstat(self.pod / WORKTREE / "a.txt").st_uid, os.getuid())


class OutputDiscipline(Base):
    """Exceptions carrying poisoned text at every stage give one fixed line and nothing else."""

    def setUp(self):
        super().setUp()
        self.start_relay()
        self.session, self.token = self.make_session()

    def run_poisoned(self, **kwargs):
        return self.run_worker(self.session, self.token, **kwargs)

    def test_an_import_that_fails_with_poisoned_text(self):
        with mock.patch.object(podworker, "_import", side_effect=ImportError(POISON)):
            result = self.run_poisoned()
        self.assert_fixed(result, stage="import")
        self.assertEqual(result.out, "podworker: started\n")

    def test_a_poisoned_environment(self):
        for name in ("FACTORY_SESSION_ID", "FACTORY_RELAY_URL", "FACTORY_SESSION_DEADLINE", "FACTORY_POD_NAME", "FACTORY_POD_UID"):
            with self.subTest(name=name):
                env = self.environment(self.session, **{name: POISON})
                result = self.run_poisoned(env=env)
                self.assert_fixed(result, stage="environment")
        self.assertEqual(self.state(self.session)["state"], "issued")

    def test_a_poisoned_token_file(self):
        for content in (POISON, f"{POISON}!{POISON}!{POISON}", "x" * 5000):
            with self.subTest(length=len(content)):
                self.write_token(content)
                self.assert_fixed(self.run_worker(self.session, None), stage="token")
        self.assertEqual(self.state(self.session)["state"], "issued")

    def test_a_poisoned_canary(self):
        net = FakeNet(("127.0.0.1", self.port), self.events)
        net.raises = RuntimeError(POISON)
        result = self.run_poisoned(net=net)
        self.assert_fixed(result, stage="canary")
        self.assertEqual(self.state(self.session)["preflight_code"], podworker.CODE_ERROR)

    def test_a_relay_that_raises_poisoned_text_at_the_preflight(self):
        for error in (OSError(13, POISON, f"/{POISON}"), http.client.BadStatusLine(POISON), RuntimeError(POISON), ValueError(POISON)):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(podworker.RelayClient, "_send", side_effect=error):
                    result = self.run_poisoned()
                self.assert_fixed(result, stage="preflight")

    def test_a_relay_that_answers_with_poisoned_text(self):
        replies = (
            Reply(500, POISON.encode(), reason=POISON),
            Reply(401, POISON.encode()),
            Reply(200, POISON.encode(), reason=POISON, length=1),
        )
        for reply in replies:
            with self.subTest(status=reply.status):
                scripted = ScriptedRelay(self, {("POST", "/v1/preflight"): [reply]})
                result = self.run_poisoned(port=scripted.port, net=FakeNet(("127.0.0.1", scripted.port)))
                if reply.status == 200:
                    continue  # a 200 with a poisoned body is a good preflight; the next stage asserts below
                self.assert_fixed(result, stage="preflight")

    def test_a_relay_that_answers_the_bundle_with_poisoned_text(self):
        replies = (
            (Reply(500, POISON.encode(), reason=POISON), "bundle"),
            (Reply(200, POISON.encode(), omit_length=True), "bundle"),
            (Reply(200, POISON.encode(), length=9999), "extract"),  # the first bytes are not a bundle: said before the cut is noticed
            (Reply(200, POISON.encode()), "extract"),
        )
        for reply, stage in replies:
            with self.subTest(status=reply.status, length=reply.length):
                script = {("POST", "/v1/preflight"): [Reply(200)], ("GET", "/v1/bundle"): [reply]}
                scripted = ScriptedRelay(self, script)
                result = self.run_poisoned(port=scripted.port, net=FakeNet(("127.0.0.1", scripted.port)))
                self.assert_fixed(result, stage=stage)

    def test_a_bundle_whose_parse_raises_poisoned_text(self):
        for error in (RuntimeError(POISON), OSError(13, POISON, f"/{POISON}"), bundle.WireError(POISON), SystemExit(POISON), KeyboardInterrupt(POISON)):
            with self.subTest(error=type(error).__name__):
                self.pod_reset()
                with mock.patch.object(bundle, "extract", side_effect=error):
                    result = self.run_poisoned()
                self.assert_fixed(result, stage="extract")

    def pod_reset(self):
        self.session, self.token = self.make_session()

    def test_a_filesystem_that_fails_with_poisoned_text_during_extraction(self):
        result = self.run_poisoned(root=self.base / "gone" / POISON)  # the data directory does not exist
        self.assert_fixed(result, stage="extract")

    def test_a_filesystem_that_fails_with_poisoned_text_when_files_are_written(self):
        real = bundle.TreeWriter.create_file

        def failing(writer, parts, chunks, mode):
            if parts[0] == "runs":  # the files of the manifest; the tree itself was extracted before
                raise OSError(13, POISON, f"/{POISON}")
            return real(writer, parts, chunks, mode)

        data = make_bundle({"files": [{"path": f"{self.pod}/runs/run.prompt", "data": "eA==", "mode": 0o600}]})
        session, token = self.make_session(data=data)
        with mock.patch.object(bundle.TreeWriter, "create_file", failing):
            result = self.run_worker(session, token)
        self.assert_fixed(result, stage="files")

    def test_a_chown_that_fails_with_poisoned_text(self):
        def failing(path, uid, gid):
            raise PermissionError(1, POISON, f"/{POISON}")

        result = self.run_poisoned(lchown=failing)
        self.assert_fixed(result, stage="chown")

    def test_a_session_runner_that_raises_or_exits_with_poisoned_text(self):
        for error in (RuntimeError(POISON), SystemExit(POISON), KeyboardInterrupt(), OSError(2, POISON, POISON)):
            with self.subTest(error=type(error).__name__):
                self.pod_reset()

                def runner(prepared, error=error):
                    raise error

                result = self.run_poisoned(runner=runner)
                self.assert_fixed(result, stage="run")

    def test_a_session_runner_that_returns_anything_but_an_exit_code_is_a_run_failure(self):
        for value in (POISON, None, True, -1, 256, 1.5, [POISON]):
            with self.subTest(value=repr(value)[:20]):
                self.pod_reset()
                result = self.run_poisoned(runner=lambda prepared, value=value: value)
                self.assert_fixed(result, stage="run")
        for value in (0, 1, 76, 255):
            with self.subTest(value=value):
                self.pod_reset()
                self.assertEqual(self.run_poisoned(runner=lambda prepared, value=value: value).code, value)

    def test_the_arguments_must_be_exactly_the_program_name(self):
        for argv in (("podworker", "--help"), ("podworker", POISON), (), ("a", "b", "c")):
            with self.subTest(argv=argv):
                result = self.run_poisoned(argv=argv)
                self.assert_fixed(result, stage="argv")
                self.assertEqual(result.out, "")
        self.assertEqual(self.state(self.session)["state"], "issued")

    def test_the_relay_being_down_prints_only_a_fixed_line(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        dead = sock.getsockname()[1]
        sock.close()
        result = self.run_poisoned(port=dead, net=FakeNet(("127.0.0.1", dead)))
        self.assert_fixed(result, stage="preflight")
        self.assertEqual(result.out, "podworker: started\n")

    def test_the_relay_going_away_after_the_preflight_is_a_bundle_failure(self):
        script = {("POST", "/v1/preflight"): [Reply(200)]}
        scripted = ScriptedRelay(self, script)
        original = podworker.RelayClient.fetch

        def gone(client, backoff):
            raise ConnectionResetError(104, POISON)

        with mock.patch.object(podworker.RelayClient, "fetch", gone):
            result = self.run_poisoned(port=scripted.port, net=FakeNet(("127.0.0.1", scripted.port)))
        self.assertIsNotNone(original)
        self.assert_fixed(result, stage="bundle")

    def test_an_unknown_stage_is_reported_as_internal(self):
        for stage in ("nonsense", POISON, "", None):
            with self.subTest(stage=stage):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = podworker.fail(stage)
                self.assertEqual(code, podworker.STAGES["internal"])
                self.assertEqual(err.getvalue(), "podworker: failed internal\n")

    def test_the_recorder_would_see_the_termination_file_opened(self):
        with recording_opens() as opened, contextlib.suppress(OSError):
            open(TERMINATION, "w", encoding="utf-8").close()
        self.assertIn(TERMINATION, opened)


class Hooks(Base):
    def test_the_interpreter_hooks_are_replaced_while_the_wrapper_runs_and_restored_after(self):
        self.start_relay()
        session, token = self.make_session()
        before = (sys.excepthook, threading.excepthook, sys.unraisablehook)
        seen = {}

        def runner(prepared):
            seen["hooks"] = (sys.excepthook, threading.excepthook, sys.unraisablehook)
            seen["faulthandler"] = faulthandler.is_enabled()
            seen["ignoring"] = any(entry[0] == "ignore" and entry[2] is Warning for entry in warnings.filters)
            return 0

        self.assertEqual(self.run_worker(session, token, runner=runner).code, 0)
        self.assertEqual(seen["hooks"], (podworker._excepthook, podworker._thread_excepthook, podworker._unraisable))
        self.assertFalse(seen["faulthandler"])
        self.assertTrue(seen["ignoring"])
        self.assertEqual((sys.excepthook, threading.excepthook, sys.unraisablehook), before)

    def test_faulthandler_is_switched_off_even_when_it_was_on(self):
        was = faulthandler.is_enabled()
        faulthandler.enable()
        self.addCleanup(lambda: faulthandler.enable() if was else faulthandler.disable())
        with podworker._quiet():
            self.assertFalse(faulthandler.is_enabled())
        self.assertTrue(faulthandler.is_enabled())

    def test_an_exception_that_escapes_everything_is_one_fixed_line_and_a_fixed_exit(self):
        err = io.StringIO()
        with mock.patch.object(os, "_exit") as leave, contextlib.redirect_stderr(err):
            podworker._excepthook(RuntimeError, RuntimeError(POISON), None)
        leave.assert_called_once_with(podworker.STAGES["internal"])
        self.assertEqual(err.getvalue(), "podworker: failed internal\n")

    def test_a_thread_that_dies_inside_the_wrapper_is_one_fixed_line_and_a_fixed_exit(self):
        self.start_relay()
        session, token = self.make_session()

        def boom():
            raise RuntimeError(POISON)

        def runner(prepared):
            thread = threading.Thread(target=boom)
            thread.start()
            thread.join(10)
            self.assertFalse(thread.is_alive())
            return 0

        with mock.patch.object(os, "_exit") as leave:
            result = self.run_worker(session, token, runner=runner)
        leave.assert_called_once_with(podworker.STAGES["internal"])
        self.assertEqual(result.err, "podworker: failed internal\n")
        self.assertNotIn(POISON, result.text)

    def test_an_exception_ignored_in_a_destructor_prints_nothing(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertIsNone(podworker._unraisable(None))
        self.assertEqual(err.getvalue(), "")

    def test_a_warning_raised_inside_the_wrapper_is_not_printed(self):
        self.start_relay()
        session, token = self.make_session()

        def runner(prepared):
            warnings.warn(POISON, RuntimeWarning, stacklevel=1)
            return 0

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result = self.run_worker(session, token, runner=runner)
        self.assertEqual(result.code, 0)
        self.assertEqual(f"{err.getvalue()}{result.err}", "")


class Process(unittest.TestCase):
    """The real command line, in a real process: `python -m factory.podworker`."""

    def run_module(self, *args, env=None, path=None):
        environ = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": path or os.fspath(ROOT / "src")}
        environ.update(env or {})
        return subprocess.run(
            [sys.executable, "-m", "factory.podworker", *args],
            capture_output=True,
            text=True,
            env=environ,
            timeout=120,
            check=False,
        )

    def test_a_missing_environment_is_one_fixed_line_on_each_stream_and_a_fixed_exit(self):
        found = self.run_module(env={"PYTHONWARNINGS": "error", "PYTHONFAULTHANDLER": "1", "FACTORY_SESSION_ID": POISON})
        self.assertEqual(found.returncode, podworker.STAGES["environment"])
        self.assertEqual(found.stdout, "podworker: started\n")
        self.assertEqual(found.stderr, "podworker: failed environment\n")

    def test_an_argument_is_a_fixed_exit_and_never_a_usage_text(self):
        for args in (("--help",), ("-h", POISON), ("x",)):
            with self.subTest(args=args):
                found = self.run_module(*args)
                self.assertEqual(found.returncode, podworker.STAGES["argv"])
                self.assertEqual(found.stdout, "")
                self.assertEqual(found.stderr, "podworker: failed argv\n")

    def test_a_package_that_cannot_be_imported_is_one_fixed_line(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "factory"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            shutil.copy(SOURCE, package / "podworker.py")
            found = self.run_module(path=directory)
        self.assertEqual(found.returncode, podworker.STAGES["import"])
        self.assertEqual(found.stdout, "podworker: started\n")
        self.assertEqual(found.stderr, "podworker: failed import\n")

    def test_a_module_that_fails_while_it_is_imported_by_the_package_is_still_one_fixed_line(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "factory"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            shutil.copy(SOURCE, package / "podworker.py")
            (package / "podcontract.py").write_text(f"raise RuntimeError({POISON!r})\n", encoding="utf-8")
            found = self.run_module(path=directory)
        self.assertEqual(found.returncode, podworker.STAGES["import"])
        self.assertNotIn(POISON, f"{found.stdout}{found.stderr}")
        self.assertEqual(found.stderr, "podworker: failed import\n")

    def test_the_pod_command_runs_this_module(self):
        self.assertEqual(podspec.WORKER_COMMAND[-2:], ("-m", "factory.podworker"))


class TokenFile(Base):
    def setUp(self):
        super().setUp()
        self.support = podworker.load_support()

    def read(self):
        return podworker.read_token(os.fspath(self.secrets), self.support)

    def test_a_root_only_file_is_read_and_one_trailing_newline_is_dropped(self):
        token = sr.new_token()
        for text in (token, f"{token}\n"):
            with self.subTest(newline=text.endswith("\n")):
                self.write_token(text, 0o400)
                self.assertEqual(self.read(), token)

    def test_a_file_with_any_group_or_other_permission_is_refused(self):
        token = sr.new_token()
        for mode in (0o440, 0o640, 0o644, 0o604, 0o660, 0o666, 0o404, 0o410):
            with self.subTest(mode=oct(mode)):
                self.write_token(token, mode)
                with self.assertRaises(podworker.Failure) as caught:
                    self.read()
                self.assertEqual(caught.exception.stage, "token")

    def test_the_kubelet_layout_of_links_is_followed(self):
        token = sr.new_token()
        version = self.secrets / "..2024_01_01"
        version.mkdir()
        (version / "token").write_text(token)
        os.chmod(version / "token", 0o400)
        os.symlink("..2024_01_01", self.secrets / "..data")
        os.symlink("..data/token", self.secrets / "token")
        self.assertEqual(self.read(), token)

    def test_a_missing_file_a_directory_and_a_fifo_are_refused(self):
        with self.assertRaises(podworker.Failure):
            self.read()
        (self.secrets / "token").mkdir()
        with self.assertRaises(podworker.Failure):
            self.read()
        os.rmdir(self.secrets / "token")
        os.mkfifo(self.secrets / "token", 0o600)
        with self.assertRaises(podworker.Failure):
            self.read()

    def test_a_fifo_that_holds_a_valid_token_is_still_refused(self):
        token = sr.new_token()
        path = self.secrets / "token"
        os.mkfifo(path, 0o600)
        reader = os.open(path, os.O_RDONLY | os.O_NONBLOCK)  # a writer can only open a fifo that has a reader
        self.addCleanup(os.close, reader)
        writer = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        self.addCleanup(os.close, writer)
        os.write(writer, token.encode("ascii"))
        with self.assertRaises(podworker.Failure) as caught:
            self.read()
        self.assertEqual(caught.exception.stage, "token")

    def test_a_value_that_is_not_a_token_is_refused_without_echoing_it(self):
        for text in ("", "short", POISON, f"{sr.new_token()}\n\n", f"{sr.new_token()} ", "é" * 50):
            with self.subTest(text=text[:10]):
                self.write_token(text, 0o400)
                with self.assertRaises(podworker.Failure) as caught:
                    self.read()
                self.assertNotIn(POISON, f"{caught.exception}{caught.exception!r}")

    def test_the_token_is_never_copied_or_written_anywhere(self):
        self.start_relay()
        session, token = self.make_session()
        result = self.run_worker(session, token)
        self.assertEqual(result.code, 0)
        for directory, _, names in os.walk(self.pod):
            for name in names:
                with self.subTest(file=name):
                    self.assertNotIn(token.encode("ascii"), (Path(directory) / name).read_bytes())
        self.assertNotIn(token, json.dumps(result.prepared.manifest))
        self.assertEqual([entry for entry in self.chowns if token in entry[0]], [])


class Canary(Base):
    def setUp(self):
        super().setUp()
        self.start_relay()
        self.support = podworker.load_support()

    def probes(self, session):
        return podworker.build_probes(self.support, self.environment(session), ("127.0.0.1", self.port))

    def failed(self, session, token, net, code):
        """Run with `net`, assert the canary failed with `code`, the relay was told and nothing was fetched."""
        result = self.run_worker(session, token, net=net)
        self.assert_fixed(result, stage="canary")
        self.assertEqual(result.out, "podworker: started\n")
        state = self.state(session)
        self.assertEqual((state["state"], state["reject"], state["preflight_code"]), ("finished", "preflight_failed", code))
        self.assertEqual(state["fetch_attempts"], 0)
        self.assert_nothing_extracted()
        self.assertEqual(self.chowns, [])
        return result

    def test_every_forbidden_destination_that_accepts_a_connection_fails_the_canary_before_the_fetch(self):
        session, token = self.make_session()
        forbidden = [probe for probe in self.probes(session) if probe.kind == "forbidden"]
        self.assertEqual(len(forbidden), len(pc.CANARY_FORBIDDEN))
        for probe in forbidden:
            with self.subTest(destination=probe.code):
                session, token = self.make_session()
                net = FakeNet(("127.0.0.1", self.port))
                net.accepts.add((probe.host, probe.port))
                self.failed(session, token, net, probe.code)

    def test_an_enforcer_that_starts_late_does_not_fail_the_canary(self):
        for probe in [probe for probe in self.probes(self.make_session()[0]) if probe.kind == "forbidden"]:
            for settled_after in range(1, len(podworker.CANARY_SETTLE_WAITS) + 1):
                with self.subTest(destination=probe.code, settled_after=settled_after):
                    session, token = self.make_session()
                    net = FakeNet(("127.0.0.1", self.port))
                    net.accepts.add((probe.host, probe.port))
                    self.clock.sleeps.clear()
                    self.clock.on_sleep = lambda net=net, probe=probe, n=settled_after: (
                        net.accepts.discard((probe.host, probe.port)) if len(self.clock.sleeps) == n else None
                    )
                    self.assertEqual(self.run_worker(session, token, net=net).code, 0)
                    self.assertEqual(self.clock.sleeps, list(podworker.CANARY_SETTLE_WAITS[:settled_after]))
                    self.clock.on_sleep = None

    def test_an_enforcer_that_never_starts_still_fails_closed_after_the_bounded_settle(self):
        session, token = self.make_session()
        net = FakeNet(("127.0.0.1", self.port))
        net.accepts.add((MANAGER, 9464))
        self.clock.sleeps.clear()
        self.failed(session, token, net, "reach_manager_metrics")
        self.assertEqual(self.clock.sleeps, list(podworker.CANARY_SETTLE_WAITS))

    def test_a_failure_that_is_not_a_reachable_address_is_not_retried(self):
        session, token = self.make_session()
        net = FakeNet(("127.0.0.1", self.port))
        net.accepts.clear()
        self.clock.sleeps.clear()
        self.failed(session, token, net, "control_failed")
        self.assertEqual(self.clock.sleeps, [])

    def test_the_contract_list_is_probed_with_its_literals_and_the_values_of_the_pod_environment(self):
        session, _token = self.make_session()
        found = {(probe.host, probe.port) for probe in self.probes(session) if probe.kind == "forbidden"}
        self.assertEqual(
            found,
            {
                ("169.254.169.254", 80),
                ("168.63.129.16", 80),
                ("168.63.129.16", 32526),
                (NODE_IP, 10250),
                (MANAGER, 8080),
                (MANAGER, 9464),
            },
        )
        self.assertEqual(
            [(probe.host, probe.port) for probe in self.probes(session) if probe.kind == "api"],
            [(SERVICE_HOST, 443), (API_HOST, 443)],
        )

    def test_all_destinations_refused_and_the_api_denying_passes(self):
        session, token = self.make_session()
        result = self.run_worker(session, token)
        self.assert_fixed(result, code=0)
        self.assertEqual(len(self.net.calls), len(pc.CANARY_FORBIDDEN) + len(pc.CANARY_API_PROBES) + 1)
        self.assertEqual(self.net.timeouts, {3})
        self.assertEqual(self.net.paths, [pc.CANARY_API_PROBE_PATH] * 2)

    def test_an_api_server_that_answers_an_unauthenticated_request_with_anything_but_401_or_403_fails(self):
        for address, code in (((SERVICE_HOST, 443), "api_service_open"), ((API_HOST, 443), "api_public_open")):
            for status in (200, 201, 204, 206, 301, 302, 404, 500, 503):
                with self.subTest(address=address[0], status=status):
                    session, token = self.make_session()
                    net = FakeNet(("127.0.0.1", self.port))
                    net.api[address] = status
                    self.failed(session, token, net, code)

    def test_an_api_server_that_denies_or_does_not_connect_passes_at_each_address(self):
        for status in (401, 403, None):
            for address in ((SERVICE_HOST, 443), (API_HOST, 443)):
                with self.subTest(address=address[0], status=status):
                    session, token = self.make_session()
                    net = FakeNet(("127.0.0.1", self.port))
                    net.api[address] = status
                    self.assertEqual(self.run_worker(session, token, net=net).code, 0)

    def test_a_failing_control_fails_the_canary_so_a_dead_network_cannot_pass_as_an_isolated_one(self):
        session, token = self.make_session()
        net = FakeNet(("127.0.0.1", self.port))
        net.accepts.clear()
        self.failed(session, token, net, "control_failed")

    def test_a_probe_that_raises_fails_the_canary_with_a_fixed_code(self):
        session, token = self.make_session()
        net = FakeNet(("127.0.0.1", self.port))
        net.raises = ValueError(POISON)
        result = self.failed(session, token, net, "probe_error")
        self.assertNotIn(POISON, result.text)

    def test_the_probes_run_in_parallel_with_the_three_second_timeout(self):
        session, token = self.make_session()
        net = FakeNet(("127.0.0.1", self.port))
        net.barrier = threading.Barrier(len(pc.CANARY_FORBIDDEN) + len(pc.CANARY_API_PROBES) + 1, timeout=20)
        result = self.run_worker(session, token, net=net)
        self.assert_fixed(result, code=0)  # run one after the other, the barrier would break and the canary would fail
        self.assertEqual(net.timeouts, {3})
        self.assertFalse(net.barrier.broken)

    def test_a_missing_or_malformed_address_fails_closed_with_a_fixed_code(self):
        for name, value in (
            ("KUBERNETES_SERVICE_HOST", None),
            ("KUBERNETES_SERVICE_PORT", None),
            ("KUBERNETES_SERVICE_PORT", "0"),
            ("KUBERNETES_SERVICE_PORT", POISON),
            ("FACTORY_NODE_IP", None),
            ("FACTORY_NODE_IP", f"{POISON}/x"),
            ("FACTORY_MANAGER_ADDRESS", None),
            ("FACTORY_MANAGER_METRICS_ADDRESS", None),
            ("FACTORY_API_PUBLIC_HOST", None),
            ("FACTORY_API_PUBLIC_HOST", "a b"),
        ):
            with self.subTest(name=name, value=value):
                session, token = self.make_session()
                result = self.run_worker(session, token, env=self.environment(session, **{name: value}))
                self.assert_fixed(result, stage="canary")
                self.assertEqual(self.state(session)["preflight_code"], "canary_config")
                self.assertEqual(self.state(session)["fetch_attempts"], 0)

    def test_the_failure_is_posted_as_a_fixed_code_and_never_a_destination(self):
        scripted = ScriptedRelay(self, {("POST", "/v1/preflight"): [Reply(200)]})
        net = FakeNet(("127.0.0.1", scripted.port))
        net.accepts.add((NODE_IP, 10250))
        session, token = self.make_session()
        result = self.run_worker(session, token, port=scripted.port, net=net)
        self.assert_fixed(result, stage="canary")
        body = scripted.requests("POST", "/v1/preflight")[0][3]
        self.assertEqual(json.loads(body), {"result": "failed", "code": "reach_kubelet"})
        self.assertNotIn(NODE_IP.encode(), body)
        self.assertEqual(scripted.requests("GET", "/v1/bundle"), [])

    def test_a_failure_that_cannot_be_posted_still_ends_with_the_canary_line_and_exit(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        dead = sock.getsockname()[1]
        sock.close()
        session, token = self.make_session()
        result = self.run_worker(session, token, port=dead, net=FakeNet(("127.0.0.1", self.port)))
        self.assert_fixed(result, stage="canary")
        self.assertEqual(result.out, "podworker: started\n")

    def test_a_hung_probe_is_bounded_and_fails_closed(self):
        session, _token = self.make_session()
        probes = self.probes(session)
        net = FakeNet(("127.0.0.1", self.port))
        net.hold = threading.Event()
        net.hold_host = NODE_IP
        self.addCleanup(net.hold.set)
        code = podworker.run_canary(probes, net, pc.CANARY_API_PROBE_PATH, (401, 403), join_seconds=0.2)
        self.assertEqual(code, podworker.CODE_ERROR)  # a forbidden destination that never answered proved nothing
        net.hold_host = SERVICE_HOST  # an API probe that never answered is no better
        code = podworker.run_canary(probes, net, pc.CANARY_API_PROBE_PATH, (401, 403), join_seconds=0.2)
        self.assertEqual(code, podworker.CODE_ERROR)
        net.hold_host = "127.0.0.1"  # now the control itself hangs: that is a failure
        code = podworker.run_canary(probes, net, pc.CANARY_API_PROBE_PATH, (401, 403), join_seconds=0.2)
        self.assertEqual(code, "control_failed")
        net.hold.set()
        for thread in threading.enumerate():
            if thread.name.startswith("podworker-canary-"):
                thread.join(10)
                self.assertFalse(thread.is_alive())

    def test_the_first_failure_in_probe_order_is_the_one_reported(self):
        session, token = self.make_session()
        net = FakeNet(("127.0.0.1", self.port))
        net.accepts.update({("169.254.169.254", 80), (MANAGER, 9464)})
        net.api[(SERVICE_HOST, 443)] = 200
        self.failed(session, token, net, "reach_imds")

    def test_every_contract_destination_has_a_fixed_code_that_the_relay_accepts(self):
        codes = []
        for entry in pc.CANARY_FORBIDDEN:
            key = (entry.get("host") or entry["host_env"], entry["port"])
            self.assertIn(key, podworker._FORBIDDEN_CODES)
            codes.append(podworker._FORBIDDEN_CODES[key])
        for entry in pc.CANARY_API_PROBES:
            self.assertIn(entry["name"], podworker._API_CODES)
            codes.append(podworker._API_CODES[entry["name"]])
        codes.extend([podworker.CODE_CONFIG, podworker.CODE_ERROR, podworker.CODE_CONTROL, podworker.CODE_OTHER_API, podworker.CODE_OTHER_FORBIDDEN])
        self.assertEqual(len(codes), len(set(codes)))
        for code in codes:
            self.assertRegex(code, sr.CODE_PATTERN)
        self.assertEqual(set(podworker._FORBIDDEN_CODES), {(entry.get("host") or entry["host_env"], entry["port"]) for entry in pc.CANARY_FORBIDDEN})
        self.assertEqual(set(podworker._API_CODES), {entry["name"] for entry in pc.CANARY_API_PROBES})

    def test_a_pending_or_odd_outcome_never_passes_a_check_it_has_not_clearly_passed(self):
        forbidden = podworker.Probe("forbidden", "h", 1, "x")
        api = podworker.Probe("api", "h", 1, "x")
        control = podworker.Probe("control", "h", 1, "x")
        denied = (401, 403)
        self.assertIsNone(podworker._failure_code(forbidden, False, denied))
        self.assertEqual(podworker._failure_code(forbidden, podworker._PENDING, denied), podworker.CODE_ERROR)
        self.assertEqual(podworker._failure_code(api, podworker._PENDING, denied), podworker.CODE_ERROR)
        for odd in (True, 1, "yes", None, "no"):
            self.assertEqual(podworker._failure_code(forbidden, odd, denied), "x", odd)
        self.assertIsNone(podworker._failure_code(api, None, denied))
        for odd in (True, "401", 0, 200, 1.5):
            self.assertEqual(podworker._failure_code(api, odd, denied), "x", odd)
        self.assertIsNone(podworker._failure_code(control, True, denied))
        for odd in (False, None, 1, "yes", podworker._PENDING):
            self.assertEqual(podworker._failure_code(control, odd, denied), "x", odd)


class RealNet(unittest.TestCase):
    """`podworker.Net` against sockets on the loopback address."""

    def listener(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(5)
        self.addCleanup(sock.close)
        return sock

    def test_a_connect_sends_nothing(self):
        sock = self.listener()
        received = []

        def serve():
            connection, _ = sock.accept()
            connection.settimeout(10)
            received.append(connection.recv(4096))
            connection.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.assertTrue(podworker.Net.connect("127.0.0.1", sock.getsockname()[1], 3))
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(received, [b""])

    def test_a_refused_port_is_not_a_connection(self):
        sock = self.listener()
        port = sock.getsockname()[1]
        sock.close()
        self.assertFalse(podworker.Net.connect("127.0.0.1", port, 3))

    def test_an_unauthenticated_request_is_sent_and_the_status_returned(self):
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                return None

            def do_GET(self):
                seen.append((self.path, dict(self.headers)))
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(10)

        self.addCleanup(stop)
        status = podworker.Net(tls=False).status("127.0.0.1", server.server_address[1], pc.CANARY_API_PROBE_PATH, 3)
        self.assertEqual(status, 403)
        path, headers = seen[0]
        self.assertEqual(path, pc.CANARY_API_PROBE_PATH)
        self.assertEqual({name.lower() for name in headers} & {"authorization", "cookie", "proxy-authorization"}, set())

    def test_nothing_that_answers_as_http_is_none(self):
        sock = self.listener()

        def serve():
            for _ in range(2):
                connection, _ = sock.accept()
                connection.sendall(b"\x16\x03\x01 not http at all")
                connection.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        port = sock.getsockname()[1]
        self.assertIsNone(podworker.Net(tls=False).status("127.0.0.1", port, "/x", 3))
        self.assertIsNone(podworker.Net(tls=True).status("127.0.0.1", port, "/x", 3))
        thread.join(10)
        self.assertFalse(thread.is_alive())
        closed = self.listener()
        dead = closed.getsockname()[1]
        closed.close()
        self.assertIsNone(podworker.Net(tls=True).status("127.0.0.1", dead, "/x", 3))

    def test_the_tls_probe_asks_for_no_verification_and_sends_no_client_certificate(self):
        context = podworker._unverified_context()
        self.assertFalse(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_NONE)


class Backoffs(unittest.TestCase):
    def test_twelve_attempts_over_less_than_sixty_seconds_and_then_no_more(self):
        clock = Clock()
        backoff = podworker.Backoff(clock)
        answers = [backoff.again() for _ in range(podworker.RETRY_ATTEMPTS + 3)]
        self.assertEqual(answers, [*([True] * (podworker.RETRY_ATTEMPTS - 1)), *([False] * 4)])
        self.assertEqual(clock.sleeps, list(podworker.RETRY_WAITS))
        self.assertLessEqual(sum(clock.sleeps), podworker.RETRY_WINDOW_SECONDS)
        self.assertEqual(len(podworker.RETRY_WAITS), podworker.RETRY_ATTEMPTS - 1)

    def test_the_window_ends_the_retries_even_when_attempts_are_left(self):
        clock = Clock()
        backoff = podworker.Backoff(clock)
        self.assertTrue(backoff.again())
        clock.now += 58  # the requests themselves were slow
        self.assertFalse(backoff.again())
        self.assertEqual(clock.sleeps, [1])

    def test_the_window_starts_at_the_first_refusal_not_at_the_start_of_the_program(self):
        clock = Clock()
        backoff = podworker.Backoff(clock)
        clock.now += 500  # a long canary before the first request
        self.assertTrue(backoff.again())

    def test_the_numbers_are_the_specified_ones(self):
        self.assertEqual(podworker.RETRY_ATTEMPTS, 12)
        self.assertEqual(podworker.RETRY_WINDOW_SECONDS, 60)
        self.assertEqual(podworker.TOO_EARLY, 425)
        self.assertEqual(podworker.CANARY_TIMEOUT_SECONDS, 3)


class Fetching(Base):
    def test_a_session_that_is_not_bound_yet_is_retried_with_the_backoff_until_it_is(self):
        self.start_relay()
        session, token = self.make_session(bound=False)
        self.clock.on_sleep = lambda: self.bind(session) if len(self.clock.sleeps) == 3 else None
        result = self.run_worker(session, token)
        self.assert_fixed(result, code=0)
        self.assertEqual(self.clock.sleeps, [1, 2, 4])
        self.assertEqual(self.state(session)["fetch_attempts"], 1)

    def test_a_bundle_answer_of_425_is_retried_and_the_bundle_is_fetched_once(self):
        script = {
            ("POST", "/v1/preflight"): [Reply(200)],
            ("GET", "/v1/bundle"): [Reply(425), Reply(425), Reply(200, make_bundle())],
        }
        scripted = ScriptedRelay(self, script)
        result = self.run_worker(pc.new_session_id(), sr.new_token(), port=scripted.port)
        self.assert_fixed(result, code=0)
        self.assertEqual(self.clock.sleeps, [1, 2])
        self.assertEqual(len(scripted.requests("GET", "/v1/bundle")), 3)
        self.assertEqual(len(scripted.requests("POST", "/v1/preflight")), 1)

    def test_425_that_never_ends_gives_up_after_twelve_attempts_with_the_fixed_exit(self):
        script = {("POST", "/v1/preflight"): [Reply(200)], ("GET", "/v1/bundle"): [Reply(425)]}
        scripted = ScriptedRelay(self, script)
        result = self.run_worker(pc.new_session_id(), sr.new_token(), port=scripted.port)
        self.assert_fixed(result, stage="wait")
        self.assertEqual(len(scripted.requests("GET", "/v1/bundle")), 12)
        self.assertEqual(self.clock.sleeps, list(podworker.RETRY_WAITS))
        self.assertLessEqual(sum(self.clock.sleeps), 60)
        self.assertEqual(result.out.splitlines(), ["podworker: started", "podworker: preflight ok"])
        self.assert_nothing_extracted()

    def test_the_preflight_and_the_bundle_share_one_budget(self):
        script = {("POST", "/v1/preflight"): [*([Reply(425)] * 6), Reply(200)], ("GET", "/v1/bundle"): [Reply(425)]}
        scripted = ScriptedRelay(self, script)
        result = self.run_worker(pc.new_session_id(), sr.new_token(), port=scripted.port)
        self.assert_fixed(result, stage="wait")
        total = len(scripted.requests("POST", "/v1/preflight")) + len(scripted.requests("GET", "/v1/bundle"))
        self.assertEqual(total, podworker.RETRY_ATTEMPTS + 1)  # twelve refusals, and the preflight that finally passed
        self.assertEqual(len(scripted.requests("POST", "/v1/preflight")), 7)

    def test_a_preflight_that_stays_425_gives_up_too(self):
        scripted = ScriptedRelay(self, {("POST", "/v1/preflight"): [Reply(425)]})
        result = self.run_worker(pc.new_session_id(), sr.new_token(), port=scripted.port)
        self.assert_fixed(result, stage="wait")
        self.assertEqual(len(scripted.requests("POST", "/v1/preflight")), 12)
        self.assertEqual(scripted.requests("GET", "/v1/bundle"), [])

    def test_only_425_is_retried(self):
        for status in (400, 401, 403, 409, 410, 500, 503):
            with self.subTest(status=status):
                scripted = ScriptedRelay(self, {("POST", "/v1/preflight"): [Reply(status)]})
                result = self.run_worker(pc.new_session_id(), sr.new_token(), port=scripted.port)
                self.assert_fixed(result, stage="preflight")
                self.assertEqual(len(scripted.requests("POST", "/v1/preflight")), 1)
        for status in (200, 204, 400, 404, 410, 500):
            with self.subTest(bundle=status):
                script = {("POST", "/v1/preflight"): [Reply(200)], ("GET", "/v1/bundle"): [Reply(status, make_bundle())]}
                scripted = ScriptedRelay(self, script)
                result = self.run_worker(pc.new_session_id(), sr.new_token(), port=scripted.port)
                self.assertEqual(len(scripted.requests("GET", "/v1/bundle")), 1)
                if status != 200:
                    self.assert_fixed(result, stage="bundle")

    def test_a_body_shorter_than_its_content_length_is_a_bundle_failure_not_a_tree_error(self):
        data = make_bundle()
        for cut in (len(data) - 1, len(data) // 2, 20):
            with self.subTest(cut=cut):
                script = {("POST", "/v1/preflight"): [Reply(200)], ("GET", "/v1/bundle"): [Reply(200, data[:cut], length=len(data))]}
                scripted = ScriptedRelay(self, script)
                result = self.run_worker(pc.new_session_id(), sr.new_token(), port=scripted.port)
                self.assert_fixed(result, stage="bundle")

    def test_a_missing_or_oversized_content_length_is_refused_before_a_byte_is_read(self):
        for reply in (Reply(200, make_bundle(), omit_length=True), Reply(200, b"x", length=pc.BUNDLE_MAX_BYTES + 1)):
            with self.subTest(omit=reply.omit_length):
                script = {("POST", "/v1/preflight"): [Reply(200)], ("GET", "/v1/bundle"): [reply]}
                scripted = ScriptedRelay(self, script)
                result = self.run_worker(pc.new_session_id(), sr.new_token(), port=scripted.port)
                self.assert_fixed(result, stage="bundle")
                self.assert_nothing_extracted()

    def test_the_relay_header_names_are_the_relays(self):
        self.assertEqual(podworker.HEADER_POD_NAME, relay.HEADER_POD_NAME)
        self.assertEqual(podworker.HEADER_POD_UID, relay.HEADER_POD_UID)


class FakeResponse:
    def __init__(self, status, body=b""):
        self.status = status
        self._body = io.BytesIO(body)
        self.closed = False

    def getheader(self, name):
        return str(len(self._body.getvalue())) if name == "Content-Length" else None

    def read(self, count):
        return self._body.read(count)

    def close(self):
        self.closed = True


def scripted_connection(answers):
    """(a stand-in for http.client.HTTPConnection that answers with `answers` in turn, the list of connections it made)."""
    queue = list(answers)
    made = []

    class Connection:
        def __init__(self, host, port, timeout):
            self.closed = False
            self.requests = []
            self.response = None
            made.append(self)

        def request(self, method, path, body=None, headers=None):
            self.requests.append((method, path, body, dict(headers or {})))

        def getresponse(self):
            self.response = FakeResponse(queue.pop(0))
            return self.response

        def close(self):
            self.closed = True

    return Connection, made


class Clients(unittest.TestCase):
    def client(self, *answers):
        connection, made = scripted_connection(answers)
        client = podworker.RelayClient("127.0.0.1", 8090, "T" * 43, pc.pod_name("0" * 32), UID, connection=connection)
        return client, made

    def test_every_answer_and_every_connection_is_closed_also_after_a_425(self):
        client, made = self.client(425, 425, 200)
        self.assertEqual(client.preflight({"result": "ok"}, podworker.Backoff(Clock())), 200)
        self.assertEqual(len(made), 3)
        for connection in made:
            self.assertTrue(connection.closed)
            self.assertTrue(connection.response.closed)

    def test_the_fetch_closes_the_answers_it_retried_and_returns_the_last_one_open(self):
        client, made = self.client(425, 200)
        connection, response = client.fetch(podworker.Backoff(Clock()))
        first, second = made
        self.assertTrue(first.closed and first.response.closed)
        self.assertFalse(second.closed or second.response.closed)
        self.assertEqual((connection, response), (second, second.response))

    def test_the_credentials_are_in_the_headers_of_every_request_and_nowhere_else(self):
        client, made = self.client(200)
        client.preflight({"result": "ok"}, podworker.Backoff(Clock()))
        method, path, body, headers = made[0].requests[0]
        self.assertEqual((method, path), ("POST", "/v1/preflight"))
        self.assertEqual(headers["Authorization"], f"Bearer {'T' * 43}")
        self.assertNotIn(b"T" * 43, body)
        self.assertNotIn("T" * 43, f"{client!r}{path}")

    def test_a_content_length_over_the_limit_is_the_bundle_stage_and_no_body_byte_is_read(self):
        support = podworker.load_support()
        limit = pc.BUNDLE_MAX_BYTES

        class Sized(FakeResponse):
            def __init__(self, length):
                super().__init__(200, b"x")
                self.length = length
                self.reads = 0

            def getheader(self, name):
                return self.length if name == "Content-Length" else None

            def read(self, count):
                self.reads += 1
                return super().read(count)

        for length in (str(limit + 1), str(limit * 2), "-1", "1e3", " 5", ""):
            with self.subTest(length=length):
                progress = podworker.Progress()
                worker = podworker.Worker({}, support, progress, net=None, clock=Clock(), lchown=None, root="/nonexistent", identity=IDENTITY, secret_dir="/nonexistent")
                connection_class, _ = scripted_connection([])
                connection = connection_class("h", 1, 1)
                response = Sized(length)
                client = mock.Mock()
                client.fetch.return_value = (connection, response)
                with mock.patch.object(support.bundle, "extract") as extract, self.assertRaises(podworker.Failure) as caught:
                    worker._fetch(client, podworker.Backoff(Clock()))
                self.assertEqual(caught.exception.stage, "bundle")
                self.assertNotEqual(progress.stage, "extract", "the extraction was never entered")
                self.assertEqual((response.reads, extract.call_count), (0, 0))
                self.assertTrue(response.closed and connection.closed)
        self.assertEqual(podworker._content_length(Sized(str(limit)), limit), limit)  # the limit itself is allowed

    def test_the_worker_closes_the_bundle_answer_when_the_extraction_fails(self):
        support = podworker.load_support()
        progress = podworker.Progress()
        worker = podworker.Worker({}, support, progress, net=None, clock=Clock(), lchown=None, root="/nonexistent", identity=IDENTITY, secret_dir="/nonexistent")
        connection_class, _ = scripted_connection([])
        connection = connection_class("h", 1, 1)
        response = FakeResponse(200, b"not a bundle")
        client = mock.Mock()
        client.fetch.return_value = (connection, response)
        with self.assertRaises(bundle.WireError):
            worker._fetch(client, podworker.Backoff(Clock()))
        self.assertEqual(progress.stage, "extract")  # what main() reports it as
        self.assertTrue(connection.closed)
        self.assertTrue(response.closed)


class HostileBundles(Base):
    """A hostile or corrupt bundle fails with the `extract` stage; nothing is written outside the data directory."""

    def run_data(self, data, **seams):
        if self.server is None:
            self.start_relay()
        session, token = self.make_session(data=data)
        result = self.run_worker(session, token, **seams)
        self.assert_fixed(result, stage="extract")
        self.assertEqual(os.listdir(self.outside), [])
        self.assertEqual(self.chowns, [])
        return result

    def test_unclean_bundles_of_every_kind_are_refused_with_the_fixed_stage(self):
        manifest = {"version": 1, "worktree": WORKTREE}
        file_record = raw_record(bundle.FILE, b"tree/a", 1, b"a")
        cases = {
            "bad magic": POISON.encode(),
            "bad version": b"".join((bundle.MAGIC, struct.pack(">H", 9))),
            "unknown kind": raw_bundle(manifest, raw_record(9, b"tree/a", 0)),
            "the kind byte of a hard link": raw_bundle(manifest, raw_record(5, b"tree/a", 0)),
            "a flag that is not the exec bit": raw_bundle(manifest, raw_record(bundle.FILE, b"tree/a", 1, b"a", flags=2)),
            "a path with ..": raw_bundle(manifest, raw_record(bundle.FILE, b"tree/../x", 1, b"a")),
            "an absolute path": raw_bundle(manifest, raw_record(bundle.FILE, b"/etc/passwd", 1, b"a")),
            "a .git component": raw_bundle(manifest, raw_record(bundle.FILE, b"tree/.git/config", 1, b"a")),
            "a .GIT component": raw_bundle(manifest, raw_record(bundle.FILE, b"tree/.GIT/config", 1, b"a")),
            "a backslash": raw_bundle(manifest, raw_record(bundle.FILE, b"tree/a\\b", 1, b"a")),
            "a NUL": raw_bundle(manifest, raw_record(bundle.FILE, b"tree/a\0b", 1, b"a")),
            "an unknown area": raw_bundle(manifest, raw_record(bundle.FILE, b"etc/passwd", 1, b"a")),
            "a record cut off inside its payload": raw_bundle(manifest, raw_record(bundle.FILE, b"tree/a", 10, b"a")),
            "a record header cut off": raw_bundle(manifest, file_record[:5]),
            "a duplicate path": raw_bundle(manifest, file_record, file_record),
            "a file with no baseline": raw_bundle(manifest, file_record),
            "a baseline for no file": raw_bundle(manifest, raw_record(bundle.BASELINE, b"tree/a", 33, b"".join((b"\x01", sha(b"a"))))),
            "a baseline with the wrong digest": raw_bundle(
                manifest, file_record, raw_record(bundle.BASELINE, b"tree/a", 33, b"".join((b"\x01", sha(b"b"))))
            ),
            "a delete record": raw_bundle(manifest, raw_record(bundle.DELETE, b"tree/a", 0)),
            "a hostile length": raw_bundle(manifest, raw_record(bundle.FILE, b"tree/a", 2**62, b"a")),
            "a write through a planted link": raw_bundle(
                manifest,
                raw_record(bundle.SYMLINK, b"tree/link", len(os.fspath(self.outside)), os.fsencode(self.outside)),
                raw_record(bundle.FILE, b"tree/link/pwn", 1, b"a"),
            ),
            "a manifest that is not first": b"".join((bundle.HEADER, file_record)),
            "a manifest that is not json": b"".join(
                (bundle.HEADER, raw_record(bundle.FILE, b"meta/manifest.json", 4, POISON[:4].encode()))
            ),
            "a manifest with a worktree that escapes": raw_bundle({"version": 1, "worktree": "../../etc"}),
            "a manifest with no worktree": raw_bundle({"version": 1}),
            "a manifest with an absolute worktree": raw_bundle({"version": 1, "worktree": "/etc"}),
            "no bundle at all": b"",
        }
        for name, data in cases.items():
            with self.subTest(case=name):
                self.run_data(data)

    def test_a_pod_data_directory_that_is_missing_is_an_extract_failure(self):
        self.start_relay()
        session, token = self.make_session()
        result = self.run_worker(session, token, root=self.base / "missing" / POISON)
        self.assert_fixed(result, stage="extract")


class Manifest(Base):
    """The manifest's directories and files, and the hostile manifests that must fail with the `manifest` stage."""

    def setUp(self):
        super().setUp()
        self.start_relay()
        self.runs = f"{self.pod}/runs"

    def entry(self, name, data=b"x", mode=0o640, path=None):
        return {"path": path or f"{self.runs}/{name}", "data": base64.b64encode(data).decode("ascii"), "mode": mode}

    def run_manifest(self, **manifest):
        session, token = self.make_session(data=make_bundle(manifest, reads={"ref/guide.md": b"read me\n"}))
        return self.run_worker(session, token)

    def test_files_and_directories_are_created_with_the_modes_and_owners_the_manifest_asks_for(self):
        home = f"{self.runs}/.home-x"
        manifest = {
            "dirs": [home, f"{home}/.claude"],
            "files": [
                self.entry("job.prompt.txt", "the prompt\n".encode(), 0o600),
                self.entry("job.settings.json", b"{}\n", 0o640),
                self.entry("job.system.md", b"system\n", 0o640),
                self.entry(".home-x/.gitconfig", b"[safe]\n\tdirectory = *\n", 0o640),
            ],
        }
        result = self.run_manifest(**manifest)
        self.assert_fixed(result, code=0)
        runs = self.pod / "runs"
        for name, mode, data in (
            ("job.prompt.txt", 0o600, b"the prompt\n"),
            ("job.settings.json", 0o640, b"{}\n"),
            ("job.system.md", 0o640, b"system\n"),
        ):
            with self.subTest(file=name):
                info = os.lstat(runs / name)
                self.assertEqual(info.st_mode & 0o777, mode)
                self.assertEqual((runs / name).read_bytes(), data)
        self.assertEqual(os.lstat(runs / ".home-x").st_mode & 0o777, 0o700)
        self.assertEqual(os.lstat(runs / ".home-x" / ".claude").st_mode & 0o777, 0o700)
        self.assertEqual((runs / ".home-x" / ".gitconfig").read_bytes(), b"[safe]\n\tdirectory = *\n")
        uid, gid = IDENTITY
        owned = {path: (u, g) for path, u, g in self.chowns}
        self.assertEqual(owned[f"{runs}/.home-x"], (uid, gid))
        self.assertEqual(owned[f"{runs}/.home-x/.claude"], (uid, gid))
        for name in ("job.settings.json", "job.system.md", ".home-x/.gitconfig"):
            self.assertEqual(owned[f"{runs}/{name}"], (-1, gid), name)  # the worker's group reads it, root keeps it
        self.assertNotIn(f"{runs}/job.prompt.txt", owned)  # the prompt stays root's and 0600: only the wrapper reads it
        self.assertEqual(result.prepared.manifest["files"][0]["mode"], 0o600)

    def test_the_worktree_and_everything_in_it_goes_to_the_worker_and_nothing_else_does(self):
        data = make_bundle(tree={"a.txt": b"a", "d/b.txt": b"b", "d/e/c.txt": b"c"}, reads={"ref/guide.md": b"r"})
        session, token = self.make_session(data=data)
        worktree = self.pod / WORKTREE
        result = self.run_worker(session, token)
        self.assert_fixed(result, code=0)
        owned = {path for path, uid, gid in self.chowns if (uid, gid) == IDENTITY}
        expected = {
            os.fspath(worktree),
            *(os.fspath(worktree / name) for name in ("a.txt", "d", "d/b.txt", "d/e", "d/e/c.txt", ".git", ".git/HEAD", ".git/objects", ".git/refs", ".git/refs/heads", ".git/refs/tags")),
        }
        self.assertEqual(owned, expected)
        self.assertEqual({gid for _, _, gid in self.chowns}, {IDENTITY[1]})
        self.assertEqual(os.lstat(self.pod / "ref" / "guide.md").st_mode & 0o777, 0o444)  # read-only files are root's and world-readable

    def test_a_link_in_the_tree_is_given_to_the_worker_itself_and_never_followed(self):
        sink = io.BytesIO()
        writer = bundle.Writer(sink)
        writer.put_manifest({"version": 1, "worktree": WORKTREE})
        target = os.fsencode(self.outside)
        writer.put(bundle.SYMLINK, bundle.AREA_TREE, "out", target)
        writer.put_baseline("out", bundle.SYMLINK, sha(target))
        session, token = self.make_session(data=sink.getvalue())
        result = self.run_worker(session, token, lchown=os.lchown, identity=(os.getuid(), os.getgid()))
        self.assert_fixed(result, code=0)
        self.assertTrue(os.path.islink(self.pod / WORKTREE / "out"))
        self.assertEqual(os.listdir(self.outside), [])

    def test_a_chown_that_would_follow_a_link_is_not_made(self):
        sink = io.BytesIO()
        writer = bundle.Writer(sink)
        writer.put_manifest({"version": 1, "worktree": WORKTREE})
        target = os.fsencode(self.outside)
        writer.put(bundle.SYMLINK, bundle.AREA_TREE, "out", target)
        writer.put_baseline("out", bundle.SYMLINK, sha(target))
        session, token = self.make_session(data=sink.getvalue())
        result = self.run_worker(session, token)
        self.assert_fixed(result, code=0)
        paths = [path for path, _, _ in self.chowns]
        self.assertIn(f"{self.pod}/{WORKTREE}/out", paths)
        self.assertEqual([path for path in paths if path.startswith(os.fspath(self.outside))], [])

    def test_chown_tree_does_not_descend_into_a_link_to_a_directory_outside(self):
        top = self.base / "worktree"
        (top / "sub").mkdir(parents=True)
        (top / "sub" / "kept").write_text("x")
        (top / "a").write_text("x")
        (self.outside / "victim").write_text("x")
        (self.outside / "deep").mkdir()
        (self.outside / "deep" / "victim").write_text("x")
        os.symlink(self.outside, top / "link")  # a link to a directory outside the tree
        os.symlink(self.outside, top / "sub" / "link2")
        os.symlink(top / "a", top / "filelink")
        calls = []
        podworker.chown_tree(os.fspath(top), 7, 8, lambda path, uid, gid: calls.append((path, uid, gid)))
        top = os.fspath(top)
        self.assertEqual(
            sorted(path for path, _, _ in calls),
            sorted([top, *(f"{top}/{name}" for name in ("sub", "sub/kept", "sub/link2", "a", "link", "filelink"))]),
            "exactly the tree's own entries: a link is changed itself and never entered",
        )
        self.assertEqual({(uid, gid) for _, uid, gid in calls}, {(7, 8)})
        self.assertEqual(sorted(os.listdir(self.outside)), ["deep", "victim"])

    def test_hostile_manifests_fail_with_the_manifest_stage_before_anything_is_written_or_chowned(self):
        runs = self.runs
        good = self.entry("a")
        cases = {
            "no version": {"version": None},
            "version 2": {"version": 2},
            "version true": {"version": True},
            "version a string": {"version": "1"},
            "files not a list": {"files": {"a": 1}},
            "dirs not a list": {"dirs": "x"},
            "an entry that is not a dict": {"files": ["x"]},
            "an entry with an extra key": {"files": [{**good, "owner": "root"}]},
            "an entry without a mode": {"files": [{"path": good["path"], "data": good["data"]}]},
            "mode 0644": {"files": [self.entry("a", mode=0o644)]},
            "mode 0777": {"files": [self.entry("a", mode=0o777)]},
            "mode setuid": {"files": [self.entry("a", mode=0o4755)]},
            "mode true": {"files": [{**good, "mode": True}]},
            "mode a string": {"files": [{**good, "mode": "384"}]},
            "mode a float": {"files": [{**good, "mode": 416.0}]},
            "data not base64": {"files": [{**good, "data": "!!!"}]},
            "data not ascii": {"files": [{**good, "data": "é"}]},
            "data not a string": {"files": [{**good, "data": 5}]},
            "a relative path": {"files": [self.entry("a", path="runs/a")]},
            "a path outside the data directory": {"files": [self.entry("a", path="/etc/cron.d/x")]},
            "a path that is a sibling prefix of the data directory": {"files": [self.entry("a", path=f"{self.pod}x/a")]},
            "a path with ..": {"files": [self.entry("a", path=f"{runs}/../../outside/a")]},
            "a .git path": {"files": [self.entry("a", path=f"{runs}/.git/config")]},
            "a .GIT path": {"files": [self.entry("a", path=f"{runs}/.GIT/hooks/post-commit")]},
            "an empty component": {"files": [self.entry("a", path=f"{runs}//a")]},
            "the data directory itself": {"dirs": [f"{self.pod}/"]},
            "a path in the worktree": {"files": [self.entry("a", path=f"{self.pod}/{WORKTREE}/a")]},
            "a directory in the worktree": {"dirs": [f"{self.pod}/{WORKTREE}/sub"]},
            "the worktree itself": {"dirs": [f"{self.pod}/{WORKTREE}"]},
            "a path listed twice": {"files": [good, good]},
            "a file and a directory with one path": {"files": [good], "dirs": [good["path"]]},
            "too many files": {"files": [self.entry(f"f{index}") for index in range(podworker.MAX_MANIFEST_FILES + 1)]},
            "too many directories": {"dirs": [f"{runs}/d{index}" for index in range(podworker.MAX_MANIFEST_DIRS + 1)]},
            "a directory that is not a string": {"dirs": [5]},
        }
        for name, manifest in cases.items():
            with self.subTest(case=name):
                if manifest.get("version", 1) is None:
                    session, token = self.make_session(data=make_bundle_without_version())
                else:
                    session, token = self.make_session(data=make_bundle(manifest))
                result = self.run_worker(session, token)
                self.assert_fixed(result, stage="manifest")
                self.assertEqual(self.chowns, [])
                self.assertFalse((self.pod / "runs").exists())
                self.assertEqual(os.listdir(self.outside), [])

    def test_a_file_that_already_exists_is_not_overwritten(self):
        session, token = self.make_session(data=make_bundle({"files": [self.entry("a"), self.entry("a", b"other", path=f"{self.runs}/./b")]}))
        (self.pod / "runs").mkdir(parents=True)
        (self.pod / "runs" / "a").write_bytes(b"mine")
        result = self.run_worker(session, token, fresh=False)
        self.assert_fixed(result, stage="manifest")  # the `.` component is refused before anything is written
        self.assertEqual((self.pod / "runs" / "a").read_bytes(), b"mine")

    def test_a_manifest_file_that_collides_with_a_read_only_file_is_a_files_failure_and_overwrites_nothing(self):
        session, token = self.make_session(data=make_bundle({"files": [self.entry("x", path=f"{self.pod}/ref/guide.md")]}, reads={"ref/guide.md": b"read me\n"}))
        result = self.run_worker(session, token)
        self.assert_fixed(result, stage="files")
        self.assertEqual((self.pod / "ref" / "guide.md").read_bytes(), b"read me\n")

    def test_a_manifest_with_no_files_or_directories_is_fine(self):
        session, token = self.make_session()
        self.assertEqual(self.run_worker(session, token).code, 0)


def make_bundle_without_version():
    sink = io.BytesIO()
    writer = bundle.Writer(sink)
    writer.put_manifest({"worktree": WORKTREE})
    return sink.getvalue()


class Module(unittest.TestCase):
    def test_the_stage_table_is_closed_fixed_and_distinct(self):
        codes = list(podworker.STAGES.values())
        self.assertEqual(len(codes), len(set(codes)))
        self.assertTrue(all(64 <= code <= 81 for code in codes))  # nothing near 126, 127 or 128 plus a signal
        for name in podworker.STAGES:
            self.assertRegex(name, "[a-z]+")
        self.assertEqual(
            sorted(podworker.STAGES),
            sorted(["argv", "environment", "token", "import", "canary", "preflight", "wait", "bundle", "extract", "manifest", "files", "chown", "run", "internal", "key", "spawn", "result", "gone"]),
        )

    def test_the_whitelist_is_the_fixed_lines_and_the_numbered_exit(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            for kind in ("started", "preflight_ok", "fetched", "posted"):
                podworker.say(kind)
            podworker.say("exit", 3)
            podworker.say("exit", True)
            podworker.say("exit", 1000)
            podworker.say("exit", POISON)
        self.assertEqual(
            out.getvalue().splitlines(),
            [
                "podworker: started",
                "podworker: preflight ok",
                "podworker: bundle fetched",
                "podworker: result posted",
                "podworker: exit 3",
                "podworker: exit 255",
                "podworker: exit 255",
                "podworker: exit 255",
            ],
        )
        with self.assertRaises(KeyError):
            podworker.say(POISON)

    def test_every_stage_named_in_the_source_is_in_the_table(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        named = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "Failure" and node.args:
                    named.add(node.args[0])
                if node.func.id == "_require" and len(node.args) > 1:
                    named.add(node.args[1])
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Attribute) and t.attr == "stage" for t in node.targets):
                named.add(node.value)
        literals = {node.value for node in named if isinstance(node, ast.Constant)}
        self.assertGreaterEqual(len(literals), 12)
        self.assertEqual(literals - set(podworker.STAGES), set())
        self.assertEqual(set(podworker.STAGES) - literals - {"argv", "internal"}, set())

    def test_the_module_imports_only_the_standard_library_at_the_top_and_has_no_argument_parser(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, "a relative import runs before the handler is in place")
                imported.add((node.module or "").split(".")[0])
        self.assertTrue(imported <= set(sys.stdlib_module_names), imported)
        self.assertFalse(imported & {"argparse", "optparse", "getopt", "click", "traceback", "logging", "pdb"})

    def test_the_environment_names_it_reads_are_the_pods_allow_list(self):
        names = {podworker.ENV_SESSION, podworker.ENV_RELAY_URL, podworker.ENV_DEADLINE, podworker.ENV_POD_NAME, podworker.ENV_POD_UID}
        self.assertTrue(names <= set(pc.ENV_POD))
        for entry in (*pc.CANARY_FORBIDDEN, *pc.CANARY_API_PROBES):
            for key in ("host_env", "port_env"):
                if key in entry:
                    self.assertTrue(entry[key] in pc.ENV_POD or entry[key].startswith("KUBERNETES_SERVICE_"), entry)

    def test_the_source_never_names_the_termination_file_in_code(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for argument in node.args:
                    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                        self.assertNotIn("termination", argument.value)

    def test_no_code_prints_to_a_stream_but_through_the_two_fixed_writers(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        self.assertNotIn("print", names)
        writes = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr in ("write", "writelines") and isinstance(node.value, ast.Name) and node.value.id == "stream"
        ]
        self.assertEqual(len(writes), 1)

    def test_the_timeouts_are_the_specified_ones(self):
        self.assertEqual(podworker.CANARY_TIMEOUT_SECONDS, 3)
        self.assertLessEqual(podworker.RETRY_WINDOW_SECONDS, 60)


if __name__ == "__main__":
    unittest.main()

"""The remote control surface: auth, validation, argv mapping, children, shutdown, audit.

Most tests run a real ``ManagerServer`` on 127.0.0.1 port 0 in a thread and talk
to it over HTTP, with a fake runner/spawner injected so the exact argv of every
endpoint can be asserted. A few use a real child process (a small fake CLI), the
real CLI (``GET status``) and a real manager subprocess (SIGTERM).
"""

import base64
import contextlib
import hashlib
import hmac
import http.client
import io
import json
import os
import select
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import tomllib
import unittest
import unittest.mock
from pathlib import Path

import factory
from factory import manager as mgr
from factory.cli import build_parser
from factory import entra_jwt
from factory.entra_jwt import EntraJwtVerifier
from factory.locks import BlobLeaseLock, LocalFlockLock, LockBusy, LockUnavailable, RepoLock
from factory.manager import (
    ACTIONS,
    ApiError,
    CommandResult,
    Manager,
    PrincipalError,
    RunnerClosed,
    Settings,
    make_server,
    parse_principal,
)

from tests import test_entra_jwt as jwt_helpers  # key/token/JWKS helpers; the module's tests are not re-run here

SRC = str(Path(factory.__file__).resolve().parent.parent)
OID = "11111111-2222-3333-4444-555555555555"
ROLE_URI = "http://schemas.microsoft.com/ws/2008/06/identity/claims/role"


def principal_header(roles=(), oid=OID, name=None, role_typ=ROLE_URI, name_typ=None, claims=None, role_claim_typ=None):
    """A base64 Easy Auth principal, the way the platform builds it."""
    items = list(claims or [])
    if oid:
        items.append({"typ": "http://schemas.microsoft.com/identity/claims/objectidentifier", "val": oid})
    if name:
        items.append({"typ": "name", "val": name})
    items += [{"typ": role_claim_typ or role_typ or ROLE_URI, "val": role} for role in roles]
    document = {"auth_typ": "aad", "claims": items}
    if role_typ is not None:
        document["role_typ"] = role_typ
    if name_typ is not None:
        document["name_typ"] = name_typ
    return base64.b64encode(json.dumps(document).encode()).decode()


OPERATOR = principal_header([mgr.ROLE_OPERATOR])
VIEWER = principal_header([mgr.ROLE_VIEWER])
NO_ROLE = principal_header([])

FAKE_CLI = textwrap.dedent(
    r"""
    import json, os, signal, sys, time
    from pathlib import Path

    args = sys.argv[1:]
    repo = run_dir = None
    while args and args[0] in ("--repo", "--run-dir"):
        if args[0] == "--repo":
            repo = args[1]
        else:
            run_dir = args[1]
        args = args[2:]
    command = args[0]
    run = Path(run_dir)

    def mark(name, text="1"):
        (run / name).write_text(text)

    if command == os.environ.get("FAKE_SLEEP_COMMAND"):
        mark("sleeper.pid", str(os.getpid()))
        time.sleep(60)
    elif command in ("plan", "run"):
        if command == "plan" and os.environ.get("FAKE_PLAN_QUICK"):
            print("PLAN-CHILD-OUTPUT", flush=True)
            sys.exit(3)
        if os.environ.get("FAKE_IGNORE_SIGINT"):
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        else:
            def on_interrupt(signum, frame):
                mark("sigint-" + command)
                sys.exit(0)
            signal.signal(signal.SIGINT, on_interrupt)
        print("SUPERVISOR-CHILD-OUTPUT", flush=True)
        mark("env-" + command, os.environ.get("ANTHROPIC_API_KEY", ""))
        mark("ready-" + command, str(os.getpid()))
        for _ in range(600):
            time.sleep(0.1)
    elif command == "status":
        print(json.dumps({"fake": True, "repo": repo, "key": os.environ.get("ANTHROPIC_API_KEY")}))
    else:
        print("SECRET-CLI-OUTPUT")
        print("SECRET-CLI-ERROR", file=sys.stderr)
        sys.exit(int(os.environ.get("FAKE_RC", "0")))
    """
)


def wait_for(predicate, timeout=15.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


def pid_gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


class FakeRunner:
    """Records the argv of every short command and returns a scripted result."""

    def __init__(self):
        self.calls = []
        self.respond = lambda argv: CommandResult(0, b"fake-output\n", b"")

    def __call__(self, argv, timeout):
        if self.closed:
            raise RunnerClosed()
        self.calls.append((list(argv), timeout))
        return self.respond(list(argv))

    closed = False
    interrupted = 0
    killed = 0
    live = 0

    def close(self):
        self.closed = True

    def interrupt_all(self):
        self.interrupted += self.live
        return self.live

    def live_count(self):
        return self.live

    def kill_all(self):
        self.killed += self.live
        return self.live

    @property
    def argvs(self):
        return [argv for argv, _ in self.calls]


class FakeProcess:
    def __init__(self, pid, exit_on_sigint=True):
        self.pid = pid
        self.exit_on_sigint = exit_on_sigint
        self.returncode = None
        self.signals = []

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def signal_group(self, sig):
        if self.returncode is not None:
            return False
        self.signals.append(sig)
        if sig == signal.SIGKILL:
            self.returncode = -9
        elif sig == signal.SIGINT and self.exit_on_sigint:
            self.returncode = 130
        return True


class FakeSpawner:
    def __init__(self, exit_on_sigint=True):
        self.calls = []
        self.processes = []
        self.exit_on_sigint = exit_on_sigint

    def __call__(self, argv, log_path):
        self.calls.append((list(argv), log_path))
        process = FakeProcess(7000 + len(self.processes), self.exit_on_sigint)
        self.processes.append(process)
        return process

    @property
    def argvs(self):
        return [argv for argv, _ in self.calls]


class FakeLock(RepoLock):
    def __init__(self, error=None, events=None):
        self.error = error
        self.events = events if events is not None else []

    def acquire(self):
        self.events.append("acquire")
        if self.error is not None:
            raise self.error
        return self

    def renew(self):
        self.events.append("renew")

    def assert_held(self):
        self.events.append("assert_held")

    def release(self):
        self.events.append("release")


class ServerCase(unittest.TestCase):
    """A real server per test, with temp config/data directories for org/name and org/other."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        self.config_dir, self.data_dir = root / "config", root / "data"
        (self.config_dir / "repos").mkdir(parents=True)
        for name in ("org__name", "org__other"):
            (self.config_dir / "repos" / f"{name}.toml").write_text(f'repo = "{name.replace("__", "/")}"\n')
        self.data_dir.mkdir()
        self.out, self.err = io.StringIO(), io.StringIO()
        self.runner, self.spawner = FakeRunner(), FakeSpawner()
        self.cli = (sys.executable, "-m", "factory")
        self.serve()

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("FACTORY_", "FAKE_"))}
        env.update(FACTORY_CONFIG=str(self.config_dir), FACTORY_DATA=str(self.data_dir), **extra)
        return env

    def serve(self, *, runner="fake", spawner="fake", lock_factory=None, env=None, cli=None, verifier=None, **overrides):
        """(Re)start the server. ``runner``/``spawner`` of None selects the real implementations."""
        if getattr(self, "server", None) is not None:
            self.stop()
        self.cli = tuple(cli) if cli else self.cli
        settings = Settings(
            config_dir=self.config_dir,
            data_dir=self.data_dir,
            host="127.0.0.1",
            port=0,
            cli=self.cli,
            shutdown_seconds=overrides.pop("shutdown_seconds", 1.0),
            **overrides,
        )
        chosen = self.runner if runner == "fake" else runner
        if isinstance(chosen, FakeRunner):
            chosen.closed = False  # a fake outlives the manager that closed it at shutdown
        self.manager = Manager(
            settings,
            env=env if env is not None else self.env(),
            runner=chosen,
            spawner=self.spawner if spawner == "fake" else spawner,
            lock_factory=lock_factory,
            stdout=self.out,
            stderr=self.err,
            verifier=verifier,
        )
        self.server = make_server(self.manager)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        server, self.server = getattr(self, "server", None), None
        if server is None:
            return
        server.shutdown()
        self.manager.terminate_children(grace=1)
        server.close_connections()
        server.server_close()
        self.thread.join(10)

    # -- helpers --------------------------------------------------------------------

    def run_dir(self, run="r1", repo="org/name"):
        return self.data_dir / "repos" / repo / "runs" / run

    def make_run(self, run="r1", repo="org/name", plan=True):
        directory = self.run_dir(run, repo)
        directory.mkdir(parents=True)
        if plan:
            (directory / "plan.json").write_text("{}")
        return directory

    def base_argv(self, run="r1", repo="org/name"):
        return [*self.cli, "--repo", repo, "--run-dir", str(self.run_dir(run, repo))]

    def call(self, method, path, body=None, principal=OPERATOR, headers=None, raw=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            sent = dict(headers or {})
            if principal is not None:
                sent["X-MS-CLIENT-PRINCIPAL"] = principal
            data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
            connection.request(method, path, body=data, headers=sent)
            response = connection.getresponse()
            payload = response.read()
            return response.status, (json.loads(payload) if payload else None), dict(response.getheaders())
        finally:
            connection.close()

    def raw_exchange(self, payload, wait=10.0):
        """Send raw bytes and return everything the server answers until it closes."""
        with socket.create_connection(("127.0.0.1", self.port), timeout=wait) as connection:
            connection.sendall(payload)
            connection.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        return b"".join(chunks)

    def audit_lines(self):
        return [line for line in self.out.getvalue().splitlines() if line.startswith("audit ")]

    def path(self, action, run="r1", repo="org/name"):
        return f"/repos/{repo}/runs/{run}/{action}"


# --- identity ----------------------------------------------------------------------


class PrincipalTests(unittest.TestCase):
    def test_roles_from_every_accepted_claim_type(self):
        for label, header in {
            "long role URI": principal_header([mgr.ROLE_OPERATOR]),
            "roles": principal_header([mgr.ROLE_OPERATOR], role_typ=None, role_claim_typ="roles"),
            "custom role_typ": principal_header([mgr.ROLE_OPERATOR], role_typ="custom-role", role_claim_typ="custom-role"),
        }.items():
            with self.subTest(label):
                principal = parse_principal(header)
                self.assertTrue(principal.can_mutate())
                self.assertTrue(principal.can_read())

    def test_viewer_reads_but_cannot_mutate_and_no_role_cannot_read(self):
        viewer, nobody = parse_principal(VIEWER), parse_principal(NO_ROLE)
        self.assertTrue(viewer.can_read() and not viewer.can_mutate())
        self.assertFalse(nobody.can_read() or nobody.can_mutate())

    def test_role_values_match_exactly(self):
        for value in ("factory.operator", "Factory.Operator ", "Factory.Operators", "Operator"):
            with self.subTest(value):
                self.assertFalse(parse_principal(principal_header([value])).can_read())

    def test_a_role_type_equal_to_the_name_type_is_never_trusted(self):
        # A user could choose a display name equal to a role; the "role" type must not be the name type.
        header = principal_header([mgr.ROLE_OPERATOR], role_typ="name", name_typ="name", role_claim_typ="name")
        self.assertFalse(parse_principal(header).can_read())
        header = principal_header([mgr.ROLE_OPERATOR], role_typ="oid", role_claim_typ="oid", oid=None)
        self.assertFalse(parse_principal(header).can_read())

    def test_identifier_prefers_the_object_id_then_the_name_then_unknown(self):
        self.assertEqual(parse_principal(principal_header([mgr.ROLE_VIEWER], name="Jane")).ident, OID)
        self.assertEqual(parse_principal(principal_header([mgr.ROLE_VIEWER], oid=None, name="Jane")).ident, "Jane")
        self.assertEqual(parse_principal(principal_header([mgr.ROLE_VIEWER], oid=None)).ident, "unknown")
        short = {"typ": "oid", "val": "abc"}
        self.assertEqual(parse_principal(principal_header([], oid=None, claims=[short])).ident, "abc")

    def test_malformed_principals_are_rejected(self):
        def encoded(document):
            return base64.b64encode(json.dumps(document).encode()).decode()

        bad = {
            "none": None,
            "empty": "",
            "not base64": "not-base64!!",
            "not json": base64.b64encode(b"hello").decode(),
            "json list": encoded([1]),
            "no claims": encoded({"role_typ": "roles"}),
            "claims not a list": encoded({"claims": "x"}),
            "claim not an object": encoded({"claims": ["x"]}),
            "typ not a string": encoded({"claims": [{"typ": 1, "val": "x"}]}),
            "val not a string": encoded({"claims": [{"typ": "roles", "val": ["Factory.Operator"]}]}),
            "oversize": "A" * (mgr.MAX_PRINCIPAL_CHARS + 4),
            "bad utf8": base64.b64encode(b"\xff\xfe").decode(),
        }
        for label, header in bad.items():
            with self.subTest(label), self.assertRaises(PrincipalError):
                parse_principal(header)


class HelperTests(unittest.TestCase):
    def test_escape_field_leaves_one_safe_token(self):
        self.assertEqual(mgr.escape_field("a\nb c=d\\e\x00é€\U0001f600"), "a\\x0ab\\x20c\\x3dd\\x5ce\\x00\\xe9\\u20ac\\U0001f600")
        self.assertEqual(mgr.escape_field("x" * 10, limit=4), "xxxx...")
        self.assertEqual(mgr.escape_field("/repos/org/name%0A"), "/repos/org/name%0A")

    def test_redact_argv_keeps_only_the_length_of_free_text(self):
        argv = ["x", "--reason=secret text", "--limit", "5", "--reason="]
        self.assertEqual(mgr.redact_argv(argv), ["x", "--reason=<11 chars>", "--limit", "5", "--reason=<0 chars>"])
        self.assertEqual(argv[1], "--reason=secret text")  # the original is untouched

    def test_split_target_decodes_each_segment_once(self):
        self.assertEqual(mgr.split_target("/repos/org/na%6De/runs/r1/status?x=1#f"), ["repos", "org", "name", "runs", "r1", "status"])
        self.assertEqual(mgr.split_target("/a%252Fb"), ["a%2Fb"])  # decoded once, not twice
        for target in ("repos", "//repos", "/repos/", "/", "/a//b", "/%ff", "http://h/repos"):
            with self.subTest(target), self.assertRaises(ApiError) as caught:
                mgr.split_target(target)
            self.assertEqual(caught.exception.status, 404)

    def test_settings_from_env(self):
        env = {"FACTORY_CONFIG": "/etc/factory", "FACTORY_DATA": "/var/lib/factory"}
        defaults = Settings.from_env(env)
        self.assertEqual((defaults.host, defaults.port), ("127.0.0.1", 8080))
        self.assertEqual((defaults.command_timeout, defaults.shutdown_seconds), (300.0, 25.0))
        self.assertEqual(defaults.cli, (sys.executable, "-m", "factory"))
        self.assertEqual((defaults.max_body, defaults.max_inbox), (64 * 1024, 1024 * 1024))
        self.assertEqual((defaults.history_max_bytes, defaults.history_retention_days), (64 * 1024 * 1024, 365))
        custom = Settings.from_env(
            {
                **env,
                "FACTORY_MANAGER_HOST": "0.0.0.0",
                "FACTORY_MANAGER_PORT": "9000",
                "FACTORY_MANAGER_COMMAND_TIMEOUT": "12.5",
                "FACTORY_MANAGER_SHUTDOWN_SECONDS": "3",
                "FACTORY_MANAGER_CLI": "/opt/bin/python  /opt/fake.py",
                "FACTORY_HISTORY_MAX_BYTES": "2097152",
                "FACTORY_HISTORY_RETENTION_DAYS": "30",
            }
        )
        self.assertEqual((custom.host, custom.port, custom.command_timeout, custom.shutdown_seconds), ("0.0.0.0", 9000, 12.5, 3.0))
        self.assertEqual((custom.history_max_bytes, custom.history_retention_days), (2097152, 30))
        self.assertEqual(custom.cli, ("/opt/bin/python", "/opt/fake.py"))

    def test_settings_reject_bad_values_without_echoing_them(self):
        env = {"FACTORY_CONFIG": "/etc/factory", "FACTORY_DATA": "/var/lib/factory"}
        bad = [
            {"FACTORY_MANAGER_PORT": "70000"},
            {"FACTORY_MANAGER_PORT": "80.5"},
            {"FACTORY_MANAGER_PORT": "abc"},
            {"FACTORY_MANAGER_COMMAND_TIMEOUT": "0"},
            {"FACTORY_MANAGER_COMMAND_TIMEOUT": "nan"},
            {"FACTORY_MANAGER_SHUTDOWN_SECONDS": "-1"},
            {"FACTORY_MANAGER_HOST": "a b"},
            {"FACTORY_CONFIG": ""},
            {"FACTORY_DATA": "/a/../b"},
            {"FACTORY_HISTORY_MAX_BYTES": "1000"},
            {"FACTORY_HISTORY_MAX_BYTES": "2097152.5"},
            {"FACTORY_HISTORY_MAX_BYTES": "abc"},
            {"FACTORY_HISTORY_RETENTION_DAYS": "0"},
            {"FACTORY_HISTORY_RETENTION_DAYS": "3651"},
            {"FACTORY_HISTORY_RETENTION_DAYS": "1.5"},
        ]
        for change in bad:
            with self.subTest(change), self.assertRaises(ValueError):
                Settings.from_env({**env, **change})
        with self.assertRaises(ValueError) as caught:
            Settings.from_env({**env, "FACTORY_MANAGER_PORT": "SECRET-NOT-A-PORT"})
        self.assertNotIn("SECRET", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            Settings.from_env({**env, "FACTORY_HISTORY_MAX_BYTES": "SECRET-NOT-A-SIZE"})
        self.assertIn("FACTORY_HISTORY_MAX_BYTES", str(caught.exception))
        self.assertNotIn("SECRET", str(caught.exception))

    def test_every_action_has_a_known_method_and_inbox_kind(self):
        self.assertEqual(
            set(ACTIONS),
            {"plan", "run", "status", "drain", "pause", "resume", "retry", "stop", "preflight", "record-capabilities", "record-blocker", "import-evidence", "purge"},
        )
        for action in ACTIONS.values():
            self.assertIn(action.method, {"GET", "POST"})
            self.assertIn(action.inbox, {None, "object", "array"})


class PackagingTests(unittest.TestCase):
    def test_console_script_is_declared(self):
        pyproject = Path(factory.__file__).resolve().parents[2] / "pyproject.toml"
        scripts = tomllib.loads(pyproject.read_text())["project"]["scripts"]
        self.assertEqual(scripts["factory-manager"], "factory.manager:main")
        self.assertEqual(scripts["factory"], "factory.cli:main")

    def test_importing_the_module_has_no_side_effects(self):
        code = "import threading, sys; import factory.manager; assert threading.active_count() == 1"
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={"PYTHONPATH": SRC})
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""), done.stderr)

    def test_module_entry_point_prints_help(self):
        done = subprocess.run([sys.executable, "-m", "factory.manager", "--help"], capture_output=True, text=True, env={**os.environ, "PYTHONPATH": SRC})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("FACTORY_MANAGER_", done.stdout)

    def test_missing_configuration_exits_2_with_a_message_naming_the_variable(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("FACTORY_")}
        done = subprocess.run([sys.executable, "-m", "factory.manager"], capture_output=True, text=True, env={**env, "PYTHONPATH": SRC})
        self.assertEqual(done.returncode, 2)
        self.assertIn("FACTORY_CONFIG", done.stderr)
        self.assertEqual(done.stdout, "")


# --- authentication and authorisation ------------------------------------------------


class AuthTests(ServerCase):
    def test_healthz_needs_no_auth_and_returns_json(self):
        status, payload, headers = self.call("GET", "/healthz", principal=None)
        self.assertEqual((status, payload), (200, {"status": "ok"}))
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(self.audit_lines(), [])  # health probes would flood the audit trail

    def test_healthz_rejects_other_methods(self):
        status, _, headers = self.call("POST", "/healthz", principal=None)
        self.assertEqual((status, headers["Allow"]), (405, "GET"))

    def test_no_principal_header_is_401_everywhere_but_healthz(self):
        for method, path in (
            ("GET", self.path("status")),
            ("POST", self.path("run")),
            ("GET", "/repos"),
            ("GET", "/does/not/exist"),
            ("POST", self.path("plan", repo="nobody/nothing")),
        ):
            with self.subTest(method=method, path=path):
                status, payload, _ = self.call(method, path, principal=None)
                self.assertEqual((status, payload), (401, {"error": "authentication required"}))
        self.assertEqual((self.runner.calls, self.spawner.calls), ([], []))

    def test_garbage_principal_headers_are_401(self):
        for label, header in {
            "not base64": "!!!not base64!!!",
            "base64 of text": base64.b64encode(b"hello").decode(),
            "json without claims": base64.b64encode(b'{"auth_typ":"aad"}').decode(),
            "json list": base64.b64encode(b"[]").decode(),
            "empty": "",
        }.items():
            with self.subTest(label):
                status, payload, _ = self.call("GET", self.path("status"), principal=header)
                self.assertEqual((status, payload["error"]), (401, "authentication required"))
        self.assertEqual(self.runner.calls, [])

    def test_a_valid_principal_without_a_factory_role_is_403(self):
        for header in (NO_ROLE, principal_header(["Some.Other.Role"])):
            for method, path in (("GET", self.path("status")), ("POST", self.path("drain")), ("GET", "/repos"), ("GET", "/nope")):
                with self.subTest(method=method, path=path):
                    status, payload, _ = self.call(method, path, principal=header)
                    self.assertEqual((status, payload), (403, {"error": "forbidden"}))
        self.assertEqual(self.runner.calls, [])

    def test_viewer_may_read_but_every_post_is_403(self):
        self.make_run()
        self.assertEqual(self.call("GET", self.path("status"), principal=VIEWER)[0], 200)
        self.assertEqual(self.call("GET", "/repos", principal=VIEWER)[0], 200)
        executed = len(self.runner.calls)
        for action, action_spec in ACTIONS.items():
            if action_spec.method != "POST":
                continue
            with self.subTest(action):
                status, payload, _ = self.call("POST", self.path(action), body={}, principal=VIEWER)
                self.assertEqual((status, payload), (403, {"error": "forbidden"}))
        self.assertEqual(len(self.runner.calls), executed)
        self.assertEqual(self.spawner.calls, [])

    def test_operator_may_read_and_mutate(self):
        self.make_run()
        self.assertEqual(self.call("GET", self.path("status"), principal=OPERATOR)[0], 200)
        self.assertEqual(self.call("GET", "/repos", principal=OPERATOR)[0], 200)
        self.assertEqual(self.call("POST", self.path("drain"), principal=OPERATOR)[0], 200)

    def test_a_role_that_is_only_a_display_name_does_not_authorise(self):
        header = principal_header([mgr.ROLE_OPERATOR], role_typ="name", name_typ="name", role_claim_typ="name")
        self.assertEqual(self.call("POST", self.path("drain"), principal=header)[0], 403)


PAUSE = "/trigger-pause"


class TriggerPauseTests(ServerCase):
    """The trigger pause: an Operator-only, audited, persistent switch shown by GET /repos (docs/manager.md)."""

    def paused(self, principal=VIEWER):
        status, payload, _ = self.call("GET", "/repos", principal=principal)
        self.assertEqual(status, 200)
        return payload["trigger_paused"]

    def test_the_operator_sets_and_clears_the_pause_and_repos_shows_it(self):
        self.assertIs(self.paused(), False)
        self.assertEqual(self.call("POST", PAUSE, {"paused": True})[:2], (200, {"trigger_paused": True}))
        self.assertIs(self.paused(), True)
        self.assertTrue((self.data_dir / mgr.TRIGGER_PAUSE_FILE).is_file())
        self.assertEqual(self.call("POST", PAUSE, {"paused": True})[:2], (200, {"trigger_paused": True}))  # idempotent
        self.assertEqual(self.call("POST", PAUSE, {"paused": False})[:2], (200, {"trigger_paused": False}))
        self.assertIs(self.paused(), False)
        self.assertFalse((self.data_dir / mgr.TRIGGER_PAUSE_FILE).exists())
        self.assertEqual(self.call("POST", PAUSE, {"paused": False})[0], 200)  # clearing an unset pause is fine

    def test_the_marker_survives_a_manager_restart(self):
        self.call("POST", PAUSE, {"paused": True})
        self.serve()
        self.assertTrue(self.manager.trigger_paused())
        self.assertIs(self.paused(), True)

    def test_the_pause_touches_no_process_and_runs_no_command(self):
        self.make_run()
        self.assertEqual(self.call("POST", self.path("run"))[0], 202)
        before = list(self.runner.calls)
        self.call("POST", PAUSE, {"paused": True})
        self.assertEqual(self.runner.calls, before)
        self.assertEqual(self.spawner.processes[0].signals, [])
        _, payload, _ = self.call("GET", "/repos")
        self.assertEqual(payload["repos"][0]["supervisor"]["running"], True)  # a run in progress keeps going

    def test_an_operator_request_to_start_a_run_is_not_blocked_by_the_pause(self):
        self.make_run()
        self.call("POST", PAUSE, {"paused": True})
        self.assertEqual(self.call("POST", self.path("run"))[0], 202)

    def test_in_process_starts_are_refused_while_paused(self):
        self.make_run()
        self.manager.set_trigger_pause(True)
        target = self.manager.target_for("org/name", "r1")
        for start in (self.manager.start_run, lambda t: self.manager.start_plan(t, {"issues": [1]})):
            with self.assertRaises(ApiError) as caught:
                start(target)
            self.assertEqual((caught.exception.status, caught.exception.message), (503, mgr.TRIGGER_PAUSED))
        self.assertEqual(self.spawner.calls, [])

    def test_only_an_operator_may_change_it(self):
        for principal in (VIEWER, NO_ROLE, principal_header(["Some.Other.Role"])):
            for paused in (True, False):
                with self.subTest(principal=principal[:8], paused=paused):
                    self.assertEqual(self.call("POST", PAUSE, {"paused": paused}, principal=principal)[:2], (403, {"error": "forbidden"}))
        self.assertEqual(self.call("POST", PAUSE, {"paused": True}, principal=None)[:2], (401, {"error": "authentication required"}))
        self.assertFalse(self.manager.trigger_paused())

    def test_no_role_may_read_the_state_and_an_anonymous_caller_learns_nothing(self):
        self.assertEqual(self.call("GET", "/repos", principal=NO_ROLE)[0], 403)
        self.assertEqual(self.call("GET", "/repos", principal=None)[0], 401)
        self.assertEqual(self.call("GET", PAUSE, principal=OPERATOR)[0], 405)
        self.assertEqual(self.call("GET", PAUSE, principal=None)[0], 401)

    def test_the_body_is_validated(self):
        for body in ({}, {"paused": "yes"}, {"paused": 1}, {"paused": None}, {"paused": True, "x": 1}, []):
            with self.subTest(body=body):
                self.assertEqual(self.call("POST", PAUSE, body)[0], 400)
        self.assertEqual(self.call("POST", PAUSE, raw=b"{not json")[0], 400)
        self.assertFalse(self.manager.trigger_paused())

    def test_every_change_is_audited_like_any_other_action(self):
        self.call("POST", PAUSE, {"paused": True})
        self.call("POST", PAUSE, {"paused": False})
        self.call("POST", PAUSE, {"paused": True}, principal=VIEWER)
        lines = self.audit_lines()
        self.assertEqual(
            lines[0], f'audit principal={OID} method=POST path=/trigger-pause status=200 argv=["trigger-pause","on"]'
        )
        self.assertEqual(
            lines[1], f'audit principal={OID} method=POST path=/trigger-pause status=200 argv=["trigger-pause","off"]'
        )
        self.assertEqual(lines[2], f"audit principal={OID} method=POST path=/trigger-pause status=403 argv=[]")

    def test_a_marker_that_cannot_be_written_is_a_500_without_details(self):
        with unittest.mock.patch.object(Manager, "set_trigger_pause", side_effect=PermissionError("secret-path")):
            status, payload, _ = self.call("POST", PAUSE, {"paused": True})
        self.assertEqual((status, payload), (500, {"error": "could not change the trigger pause"}))
        self.assertNotIn("secret-path", self.out.getvalue() + self.err.getvalue())


# --- Entra bearer-token authentication (FACTORY_MANAGER_AUTH=entra-jwt) -------------------


class Headers(dict):
    """Case-insensitive ``get``, like the headers http.server hands over."""

    def get(self, name, default=None):
        for key, value in self.items():
            if key.lower() == name.lower():
                return value
        return default


class JwtCase(ServerCase):
    """A server in entra-jwt mode with an injected verifier: a scripted JWKS and a scripted clock."""

    def setUp(self):
        super().setUp()
        self.fetch = jwt_helpers.FakeJwks(jwt_helpers.key())
        self.keys_clock = jwt_helpers.Clock(1000.0)
        self.verifier, self.cache = jwt_helpers.make_verifier(self.fetch, self.keys_clock)
        self.serve(verifier=self.verifier, auth=mgr.AUTH_JWT, entra_tenant_id=jwt_helpers.TENANT, entra_audiences=(jwt_helpers.CLIENT_ID,))

    @staticmethod
    def token(roles=("Factory.Operator",), signer=None, **overrides):
        return jwt_helpers.make_token(signer, payload=jwt_helpers.claims(roles=list(roles), **overrides))

    def jcall(self, method, path, token=None, scheme="Bearer", headers=None, body=None, principal=None):
        sent = dict(headers or {})
        if token is not None:
            sent["Authorization"] = f"{scheme} {token}" if scheme else token
        return self.call(method, path, body=body, principal=principal, headers=sent)

    def direct(self, method, path, peer, headers=None):
        """One request through ``Manager.handle`` from a chosen peer address (the test server only sees 127.0.0.1)."""
        request = mgr.Request(method, path, Headers(headers or {}), lambda limit: b"", peer)
        return self.manager.handle(request)


UNAUTHENTICATED = (401, {"error": "authentication required"})


class JwtAuthTests(JwtCase):
    def test_an_operator_token_may_read_and_mutate(self):
        self.make_run()
        token = self.token()
        self.assertEqual(self.jcall("GET", "/repos", token)[0], 200)
        self.assertEqual(self.jcall("GET", self.path("status"), token)[0], 200)
        self.assertEqual(self.jcall("POST", self.path("drain"), token)[0], 200)

    def test_a_viewer_token_may_read_but_not_mutate(self):
        self.make_run()
        token = self.token(roles=["Factory.Viewer"])
        self.assertEqual(self.jcall("GET", "/repos", token)[0], 200)
        self.assertEqual(self.jcall("GET", self.path("status"), token)[0], 200)
        self.assertEqual(self.jcall("POST", self.path("drain"), token)[:2], (403, {"error": "forbidden"}))
        self.assertEqual(self.jcall("POST", self.path("plan"), token, body={})[0], 403)
        self.assertEqual(self.spawner.calls, [])

    def test_the_trigger_pause_needs_the_operator_role_in_jwt_mode_too(self):
        body = {"paused": True}
        self.assertEqual(self.jcall("POST", "/trigger-pause", self.token(roles=["Factory.Viewer"]), body=body)[0], 403)
        self.assertEqual(self.jcall("POST", "/trigger-pause", self.token(roles=[]), body=body)[0], 403)
        self.assertEqual(self.jcall("POST", "/trigger-pause", None, body=body)[0], 401)
        self.assertEqual(self.jcall("POST", "/trigger-pause", None, body=body, principal=OPERATOR)[0], 401)  # Easy Auth header ignored
        self.assertFalse(self.manager.trigger_paused())
        self.assertEqual(self.jcall("POST", "/trigger-pause", self.token(), body=body)[:2], (200, {"trigger_paused": True}))
        self.assertTrue(self.manager.trigger_paused())

    def test_a_valid_token_without_a_factory_role_is_403(self):
        for roles in ([], ["Some.Other.Role"], ["factory.operator"], ["Factory.Operators"]):
            token = self.token(roles=roles)
            for method, path in (("GET", "/repos"), ("POST", self.path("drain")), ("GET", "/nope")):
                with self.subTest(roles=roles, method=method, path=path):
                    self.assertEqual(self.jcall(method, path, token)[:2], (403, {"error": "forbidden"}))
        token = jwt_helpers.make_token(payload=jwt_helpers.claims(roles=None))  # no roles claim at all
        self.assertEqual(self.jcall("GET", "/repos", token)[0], 403)
        self.assertEqual(self.runner.calls, [])

    def test_a_token_is_required_everywhere_and_the_answer_does_not_depend_on_the_path(self):
        for method, path in (
            ("GET", self.path("status")),
            ("POST", self.path("run")),
            ("GET", "/repos"),
            ("GET", "/does/not/exist"),
            ("POST", self.path("plan", repo="nobody/nothing")),
        ):
            with self.subTest(method=method, path=path):
                status, payload, headers = self.jcall(method, path)
                self.assertEqual((status, payload), UNAUTHENTICATED)
                self.assertEqual(headers["WWW-Authenticate"], "Bearer")
        self.assertEqual((self.runner.calls, self.spawner.calls), ([], []))

    def test_authorization_headers_that_are_not_bearer_tokens_are_401(self):
        token = self.token()
        for label, header in {
            "no scheme": token,
            "basic": f"Basic {token}",
            "empty token": "Bearer ",
            "scheme only": "Bearer",
            "no space": f"Bearer{token}",
            "two spaces": f"Bearer  {token}",
            "two tokens": f"Bearer {token} {token}",
            "tab": f"Bearer\t{token}",
            "trailing space": f"Bearer {token} ",
            "token in quotes": f'Bearer "{token}"',
        }.items():
            with self.subTest(label):
                status, payload, _ = self.call("GET", "/repos", principal=None, headers={"Authorization": header})
                self.assertEqual((status, payload), UNAUTHENTICATED)

    def test_the_scheme_is_case_insensitive(self):
        for scheme in ("bearer", "BEARER", "Bearer"):
            with self.subTest(scheme):
                self.assertEqual(self.jcall("GET", "/repos", self.token(), scheme=scheme)[0], 200)

    def test_every_kind_of_bad_token_gets_the_same_401(self):
        good = jwt_helpers.make_token()
        header, payload, signature = good.split(".")
        attacker = jwt_helpers.key("attacker")
        public_secret = jwt_helpers._int_bytes(jwt_helpers.key().n)
        hs_input = f"{jwt_helpers.b64json({'alg': 'HS256', 'kid': jwt_helpers.KID})}.{payload}"
        hs_sig = jwt_helpers.b64(hmac.new(public_secret, hs_input.encode(), hashlib.sha256).digest())
        bad = {
            "garbage": "not-a-token",
            "alg none": f"{jwt_helpers.b64json({'alg': 'none', 'kid': jwt_helpers.KID})}.{payload}.",
            "alg none, junk signature": f"{jwt_helpers.b64json({'alg': 'none', 'kid': jwt_helpers.KID})}.{payload}.{signature}",
            "HS256 with the public key as secret": f"{hs_input}.{hs_sig}",
            "tampered payload": f"{header}.{jwt_helpers.b64json(jwt_helpers.claims(roles=['Factory.Operator'], preferred_username='root'))}.{signature}",
            "other key": jwt_helpers.make_token(attacker, kid=jwt_helpers.KID),
            "wrong issuer": self.token(iss="https://sts.windows.net/x/"),
            "wrong audience": self.token(aud="someone-else"),
            "wrong tenant": self.token(tid=jwt_helpers.OTHER_TENANT),
            "expired": self.token(exp=jwt_helpers.NOW - 3600),
            "not yet valid": self.token(nbf=jwt_helpers.NOW + 3600),
        }
        seen = set()
        for label, token in bad.items():
            with self.subTest(label):
                status, payload_, headers = self.jcall("GET", "/repos", token)
                self.assertEqual((status, payload_), UNAUTHENTICATED)
                seen.add((status, json.dumps(payload_), headers["WWW-Authenticate"], headers["Content-Type"]))
        self.assertEqual(len(seen), 1)  # nothing in the response tells the cases apart
        self.assertEqual(self.runner.calls, [])

    def test_a_forged_easy_auth_header_is_ignored_completely(self):
        self.make_run()
        # An operator principal without a token is not authentication ...
        status, payload, _ = self.call("POST", self.path("drain"), principal=OPERATOR)
        self.assertEqual((status, payload), UNAUTHENTICATED)
        # ... and next to a weaker token it does not raise the caller's rights ...
        viewer = self.token(roles=["Factory.Viewer"])
        self.assertEqual(self.jcall("POST", self.path("drain"), viewer, principal=OPERATOR)[0], 403)
        none = self.token(roles=[])
        self.assertEqual(self.jcall("GET", "/repos", none, principal=OPERATOR)[0], 403)
        # ... and next to an invalid token it does not rescue it.
        self.assertEqual(self.jcall("GET", "/repos", "junk", principal=OPERATOR)[:2], UNAUTHENTICATED)
        self.assertEqual(self.runner.calls, [])
        # A garbage principal header next to a good token is not even looked at.
        self.assertEqual(self.jcall("GET", "/repos", self.token(), principal="!!!not base64!!!")[0], 200)

    def test_the_audit_line_names_the_token_principal_not_the_forged_header(self):
        self.make_run()
        self.jcall("POST", self.path("drain"), self.token(), principal=principal_header([mgr.ROLE_OPERATOR], oid="99999999-0000-0000-0000-000000000000"))
        self.jcall("GET", "/repos")
        first, second = self.audit_lines()
        self.assertIn("audit principal=operator@example.invalid method=POST ", first)
        self.assertNotIn("99999999", first)
        self.assertEqual(second, "audit principal=anonymous method=GET path=/repos status=401 argv=[]")

    def test_the_audit_principal_falls_back_to_the_application_and_object_ids(self):
        app = self.token(preferred_username=None, azp="app-client-id")
        self.jcall("GET", "/repos", app)
        self.jcall("GET", "/repos", self.token(preferred_username=None))
        lines = self.audit_lines()
        self.assertIn("principal=app-client-id ", lines[0])
        self.assertIn(f"principal={OID} ", lines[1])

    def test_a_hostile_principal_name_cannot_forge_audit_lines(self):
        evil = "evil\nname\r audit principal=root status=200 argv=[]"
        self.jcall("GET", "/repos", self.token(preferred_username=evil))
        (line,) = self.out.getvalue().splitlines()
        self.assertRegex(line, AUDIT)
        self.assertIn("principal=evil\\x0aname\\x0d", line)

    def test_a_long_principal_name_is_truncated_in_the_audit_line(self):
        self.jcall("GET", "/repos", self.token(preferred_username="A" * 500))
        (line,) = self.audit_lines()
        self.assertIn(f"principal={'A' * 128} ", line)
        self.assertNotIn("A" * 129, line)

    def test_neither_tokens_nor_claim_values_are_ever_written_out(self):
        self.make_run()
        good = self.token(name="SECRET-NAME-CLAIM", upn="SECRET-UPN-CLAIM")
        wrong = self.token(name="SECRET-NAME-CLAIM", aud="SECRET-AUDIENCE-CLAIM")
        unknown_kid = jwt_helpers.make_token(jwt_helpers.key("rotated"), payload=jwt_helpers.claims(name="SECRET-NAME-CLAIM"))
        self.jcall("GET", "/repos", good)
        self.jcall("POST", self.path("drain"), good)
        self.jcall("GET", "/repos", wrong)
        self.jcall("GET", "/repos", unknown_kid)
        self.fetch.fail(OSError("SECRET-HOST.invalid"))
        self.keys_clock.advance(100_000)
        self.jcall("GET", "/repos", unknown_kid)
        everything = self.out.getvalue() + self.err.getvalue()
        for secret in ("SECRET-", good, wrong, unknown_kid, good.split(".")[2], good.split(".")[1], "Bearer"):
            self.assertNotIn(secret, everything)
        self.assertEqual(len(self.audit_lines()), 5)

    def test_the_manager_does_not_echo_the_token_in_responses(self):
        token = self.token(roles=[])
        reply = self.raw_exchange(f"GET /repos HTTP/1.0\r\nAuthorization: Bearer {token}\r\n\r\n".encode())
        self.assertNotIn(token.encode(), reply)
        self.assertIn(b" 403 ", reply.split(b"\r\n", 1)[0])


class JwtKeyHandlingTests(JwtCase):
    def test_an_unknown_kid_triggers_a_refetch_and_then_succeeds(self):
        self.assertEqual(self.jcall("GET", "/repos", self.token())[0], 200)
        rotated = self.token(signer=jwt_helpers.key("rotated"))
        self.fetch.serve(jwt_helpers.key(), jwt_helpers.key("rotated"))
        self.keys_clock.advance(entra_jwt.KEYS_MIN_INTERVAL_SECONDS + 1)
        self.assertEqual(self.jcall("GET", "/repos", rotated)[0], 200)
        self.assertEqual(len(self.fetch.urls), 2)

    def test_an_unknown_kid_inside_the_minimum_interval_is_401_without_a_fetch(self):
        self.assertEqual(self.jcall("GET", "/repos", self.token())[0], 200)
        rotated = self.token(signer=jwt_helpers.key("rotated"))
        self.fetch.serve(jwt_helpers.key(), jwt_helpers.key("rotated"))
        for _ in range(3):
            self.assertEqual(self.jcall("GET", "/repos", rotated)[:2], UNAUTHENTICATED)
        self.assertEqual(len(self.fetch.urls), 1)

    def test_a_jwks_outage_is_503_for_a_token_that_cannot_be_checked(self):
        self.fetch.fail()
        status, payload, headers = self.jcall("GET", "/repos", self.token())
        self.assertEqual((status, payload), (503, {"error": "authentication unavailable"}))
        self.assertEqual(headers["Retry-After"], "5")
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.audit_lines()[-1], "audit principal=anonymous method=GET path=/repos status=503 argv=[]")
        # Recovery needs no restart.
        self.fetch.serve(jwt_helpers.key())
        self.keys_clock.advance(entra_jwt.KEYS_MIN_INTERVAL_SECONDS + 1)
        self.assertEqual(self.jcall("GET", "/repos", self.token())[0], 200)

    def test_during_an_outage_wrong_tokens_stay_401_and_missing_tokens_stay_401(self):
        self.fetch.fail()
        hs = f"{jwt_helpers.b64json({'alg': 'HS256', 'kid': jwt_helpers.KID})}.{jwt_helpers.b64json(jwt_helpers.claims())}.{jwt_helpers.b64(b'x' * 32)}"
        for token in (None, "junk", hs):
            with self.subTest(token):
                self.assertEqual(self.jcall("GET", "/repos", token)[:2], UNAUTHENTICATED)

    def test_cached_keys_keep_serving_through_an_outage(self):
        self.assertEqual(self.jcall("GET", "/repos", self.token())[0], 200)
        self.fetch.fail()
        self.keys_clock.advance(7200)  # past the ttl, within the stale limit
        self.assertEqual(self.jcall("GET", "/repos", self.token())[0], 200)
        self.assertEqual(self.jcall("GET", "/repos", jwt_helpers.make_token(jwt_helpers.key("attacker"), kid=jwt_helpers.KID))[:2], UNAUTHENTICATED)
        self.keys_clock.advance(100_000)  # too stale: fail closed
        self.assertEqual(self.jcall("GET", "/repos", self.token())[0], 503)


class JwtHealthTests(JwtCase):
    def test_loopback_peers_may_probe_health_without_a_token(self):
        status, payload, _ = self.call("GET", "/healthz", principal=None)  # a real connection from 127.0.0.1
        self.assertEqual((status, payload), (200, {"status": "ok"}))
        for peer in ("127.0.0.1", "127.1.2.3", "::1", "::ffff:127.0.0.1", "::1%lo"):
            with self.subTest(peer):
                reply = self.direct("GET", "/healthz", peer)
                self.assertEqual((reply.status, reply.payload), (200, {"status": "ok"}))
        self.assertEqual(self.audit_lines(), [])  # probes would flood the audit trail
        self.assertEqual(self.fetch.urls, [])  # and need no keys

    def test_every_other_peer_needs_a_valid_token_even_for_health(self):
        for peer in ("10.244.1.7", "192.0.2.10", "2001:db8::1", "::ffff:10.0.0.1", "", "not-an-address", "localhost"):
            with self.subTest(peer):
                reply = self.direct("GET", "/healthz", peer)
                self.assertEqual((reply.status, reply.payload), UNAUTHENTICATED)
                self.assertEqual(reply.headers["WWW-Authenticate"], "Bearer")
        self.assertEqual(self.direct("GET", "/healthz", "10.244.1.7", {"Authorization": "Bearer junk"}).status, 401)
        self.assertEqual(self.direct("GET", "/healthz", "10.244.1.7", {"X-MS-CLIENT-PRINCIPAL": OPERATOR}).status, 401)

    def test_a_valid_token_lets_a_remote_peer_read_health(self):
        reply = self.direct("GET", "/healthz", "10.244.1.7", {"Authorization": f"Bearer {self.token()}"})
        self.assertEqual((reply.status, reply.payload), (200, {"status": "ok"}))
        reply = self.direct("GET", "/healthz", "10.244.1.7", {"authorization": f"Bearer {self.token(roles=[])}"})
        self.assertEqual(reply.status, 200)  # health needs a valid token, not a role

    def test_forwarding_headers_cannot_make_a_remote_peer_look_local(self):
        forged = {"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1", "Forwarded": "for=127.0.0.1", "Host": "127.0.0.1"}
        reply = self.direct("GET", "/healthz", "203.0.113.9", forged)
        self.assertEqual(reply.status, 401)

    def test_the_public_url_is_401_for_a_remote_peer_without_a_token_in_the_audit_trail(self):
        self.direct("GET", "/healthz", "203.0.113.9")
        self.assertEqual(self.audit_lines(), ["audit principal=anonymous method=GET path=/healthz status=401 argv=[]"])

    def test_health_from_a_remote_peer_is_503_when_the_token_cannot_be_checked(self):
        self.fetch.fail()
        reply = self.direct("GET", "/healthz", "10.244.1.7", {"Authorization": f"Bearer {self.token()}"})
        self.assertEqual(reply.status, 503)
        self.assertEqual(self.call("GET", "/healthz", principal=None)[0], 200)  # the pod's own probe is unaffected

    def test_health_still_rejects_other_methods(self):
        self.assertEqual(self.call("POST", "/healthz", principal=None)[0], 405)  # loopback
        self.assertEqual(self.direct("POST", "/healthz", "10.244.1.7").status, 401)  # remote, no token
        reply = self.direct("POST", "/healthz", "10.244.1.7", {"Authorization": f"Bearer {self.token()}"})
        self.assertEqual((reply.status, reply.headers["Allow"]), (405, "GET"))

    def test_is_loopback(self):
        for peer, expected in {
            "127.0.0.1": True, "127.255.255.254": True, "::1": True, "::ffff:127.0.0.1": True,
            "0.0.0.0": False, "10.0.0.1": False, "169.254.1.1": False, "::": False, "::2": False,
            "": False, "x": False, "127.0.0.1.evil": False, "1.2.3.4%lo": False,
        }.items():
            with self.subTest(peer):
                self.assertEqual(mgr.is_loopback(peer), expected)


class EasyAuthIsUnchangedTests(ServerCase):
    def test_the_default_mode_is_easy_auth_and_still_ignores_the_authorization_header(self):
        self.assertEqual(self.manager.settings.auth, mgr.AUTH_EASY)
        self.assertIsNone(self.manager.verifier)
        token = jwt_helpers.make_token()
        status, payload, headers = self.call("GET", "/repos", principal=None, headers={"Authorization": f"Bearer {token}"})
        self.assertEqual((status, payload), UNAUTHENTICATED)
        self.assertNotIn("WWW-Authenticate", headers)
        self.assertEqual(self.call("GET", "/repos", principal=VIEWER, headers={"Authorization": "Bearer junk"})[0], 200)

    def test_health_stays_open_to_every_peer(self):
        request = mgr.Request("GET", "/healthz", Headers(), lambda limit: b"", "203.0.113.9")
        self.assertEqual(self.manager.handle(request).status, 200)

    def test_the_startup_line_is_unchanged(self):
        self.assertNotIn("auth=", mgr.startup_line(self.server, self.manager))


class JwtSettingsTests(unittest.TestCase):
    BASE = {"FACTORY_CONFIG": "/etc/factory", "FACTORY_DATA": "/var/lib/factory"}

    def settings(self, **extra):
        return Settings.from_env({**self.BASE, **extra})

    def test_the_default_is_easy_auth_and_ignores_the_entra_variables(self):
        for extra in ({}, {"FACTORY_MANAGER_AUTH": ""}, {"FACTORY_MANAGER_AUTH": "easy-auth", "FACTORY_ENTRA_TENANT_ID": "garbage"}):
            with self.subTest(extra):
                settings = self.settings(**extra)
                self.assertEqual((settings.auth, settings.entra_tenant_id, settings.entra_audiences), ("easy-auth", "", ()))

    def test_entra_jwt_reads_the_tenant_and_the_audience_list(self):
        settings = self.settings(
            FACTORY_MANAGER_AUTH="entra-jwt",
            FACTORY_ENTRA_TENANT_ID=jwt_helpers.TENANT.upper(),
            FACTORY_ENTRA_AUDIENCES=f"{jwt_helpers.CLIENT_ID}, {jwt_helpers.AUDIENCE_URI}",
        )
        self.assertEqual(settings.auth, "entra-jwt")
        self.assertEqual(settings.entra_tenant_id, jwt_helpers.TENANT)
        self.assertEqual(settings.entra_audiences, (jwt_helpers.CLIENT_ID, jwt_helpers.AUDIENCE_URI))

    def test_bad_values_name_the_variable_and_never_echo_it(self):
        good = {"FACTORY_MANAGER_AUTH": "entra-jwt", "FACTORY_ENTRA_TENANT_ID": jwt_helpers.TENANT, "FACTORY_ENTRA_AUDIENCES": jwt_helpers.CLIENT_ID}
        cases = {
            "FACTORY_MANAGER_AUTH": ("SECRET-MODE", "Entra-JWT", "none", "off"),
            "FACTORY_ENTRA_TENANT_ID": ("", "SECRET-TENANT", "<<TENANT_ID>>", "contoso.onmicrosoft.com"),
            "FACTORY_ENTRA_AUDIENCES": ("", " , ", "SECRET AUDIENCE", "<<CLIENT_ID>>"),
        }
        for variable, values in cases.items():
            for value in values:
                with self.subTest(variable=variable, value=value), self.assertRaises(ValueError) as raised:
                    self.settings(**{**good, variable: value})
                self.assertIn(variable, str(raised.exception))
                self.assertNotIn("SECRET", str(raised.exception))

    def test_a_misconfigured_manager_exits_2_with_a_message_and_no_output(self):
        import contextlib

        err, out = io.StringIO(), io.StringIO()
        env = {**self.BASE, "FACTORY_MANAGER_AUTH": "entra-jwt", "FACTORY_ENTRA_TENANT_ID": "SECRET-TENANT"}
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            code = mgr.main([], env)
        self.assertEqual(code, 2)
        self.assertIn("FACTORY_ENTRA_TENANT_ID", err.getvalue())
        self.assertNotIn("SECRET", err.getvalue())
        self.assertEqual(out.getvalue(), "")


class JwtWiringTests(ServerCase):
    """The manager builds its own verifier (default fetcher) when none is injected."""

    def serve_jwt(self):
        with unittest.mock.patch("factory.entra_jwt.fetch_https", side_effect=OSError("SECRET-HOST.invalid")) as fetcher:
            self.serve(auth=mgr.AUTH_JWT, entra_tenant_id=jwt_helpers.TENANT, entra_audiences=(jwt_helpers.CLIENT_ID,))
        return fetcher

    def test_the_default_verifier_uses_the_tenant_jwks_and_fails_closed(self):
        fetcher = self.serve_jwt()
        self.assertIsInstance(self.manager.verifier, EntraJwtVerifier)
        status, payload, _ = self.call("GET", "/repos", principal=None, headers={"Authorization": f"Bearer {jwt_helpers.make_token()}"})
        self.assertEqual((status, payload), (503, {"error": "authentication unavailable"}))
        fetcher.assert_called_once_with(jwt_helpers.JWKS_URL)
        self.assertEqual(self.err.getvalue(), "factory-manager: signing key refresh failed (OSError)\n")
        self.assertNotIn("SECRET", self.out.getvalue() + self.err.getvalue())

    def test_the_startup_line_names_the_mode(self):
        self.serve_jwt()
        self.assertEqual(
            mgr.startup_line(self.server, self.manager),
            f"factory-manager listening host=127.0.0.1 port={self.port} repos=2 auth=entra-jwt",
        )


# --- routing and path validation -----------------------------------------------------


class RoutingTests(ServerCase):
    def test_repo_not_in_config_is_404_with_the_same_body_as_a_malformed_name(self):
        bodies = set()
        for repo in ("org/missing", "nobody/nothing", "..%2F..%2Fetc/passwd", "org/na%2Fme", "a__b/c", "-x/y", "org/.", "org/%00", "org/n%C3%A9"):
            with self.subTest(repo=repo):
                status, payload, _ = self.call("GET", f"/repos/{repo}/runs/r1/status")
                self.assertEqual(status, 404)
                bodies.add(json.dumps(payload))
        self.assertEqual(bodies, {'{"error": "not found"}'})
        self.assertEqual(self.runner.calls, [])

    def test_run_slug_must_match_the_cli_rule(self):
        for run in ("bad%20slug", "-x", "_x", "a" * 51, "a%2Fb", "a.b", "%00", "..", "r1%0A", "%C3%A9"):
            with self.subTest(run=run):
                status, payload, _ = self.call("GET", f"/repos/org/name/runs/{run}/status")
                self.assertEqual((status, payload), (400, {"error": "invalid run name"}))
        for run in ("r1", "a" * 50, "Run_2-x"):
            with self.subTest(run=run):
                self.assertEqual(self.call("GET", f"/repos/org/name/runs/{run}/status")[0], 200)

    def test_unknown_repo_is_reported_before_a_bad_slug(self):
        self.assertEqual(self.call("GET", "/repos/org/missing/runs/bad%20slug/status")[0], 404)

    def test_unknown_paths_are_404(self):
        for path in (
            "/",
            "/nope",
            "/repos/org",
            "/repos/org/name",
            "/repos/org/name/runs",
            "/repos/org/name/runs/r1",
            "/repos/org/name/runs/r1/unknown",
            "/repos/org/name/runs/r1/status/extra",
            "/repos/org/name/tasks/r1/status",
            "/repos/org/name/runs/r1/status/",
            "/repos/",
            "/healthz/",
        ):
            with self.subTest(path=path):
                status, payload, _ = self.call("GET", path)
                self.assertEqual((status, payload), (404, {"error": "not found"}))

    def test_wrong_method_is_405_with_an_allow_header(self):
        cases = [
            ("POST", "/repos", "GET"),
            ("DELETE", "/repos", "GET"),
            ("GET", self.path("plan"), "POST"),
            ("POST", self.path("status"), "GET"),
            ("PUT", self.path("run"), "POST"),
            ("DELETE", self.path("purge"), "POST"),
            ("PATCH", self.path("drain"), "POST"),
        ]
        for method, path, allow in cases:
            with self.subTest(method=method, path=path):
                status, payload, headers = self.call(method, path)
                self.assertEqual((status, payload["error"], headers["Allow"]), (405, "method not allowed", allow))
        self.assertEqual((self.runner.calls, self.spawner.calls), ([], []))

    def test_head_on_a_post_route_has_no_body(self):
        status, payload, _ = self.call("HEAD", self.path("plan"))
        self.assertEqual((status, payload), (405, None))

    def test_a_query_string_is_ignored(self):
        self.assertEqual(self.call("GET", f"{self.path('status')}?run=other&x=%00")[0], 200)
        self.assertEqual(self.runner.argvs[-1], [*self.base_argv(), "status", "--json"])

    def test_percent_encoding_is_decoded_exactly_once(self):
        self.assertEqual(self.call("GET", "/repos/org/na%6De/runs/r%31/status")[0], 200)
        self.assertEqual(self.runner.argvs[-1], [*self.base_argv(), "status", "--json"])
        # %2570 decodes once to "%70", which is not a valid run name (it is not decoded again to "p")
        self.assertEqual(self.call("GET", "/repos/org/name/runs/%2570/status")[0], 400)

    def test_repos_lists_allowlisted_repos_runs_and_children(self):
        self.make_run("r1")
        (self.run_dir("r1") / "state.json").write_text("{}")
        self.make_run("r2", plan=False)
        (self.data_dir / "repos" / "org" / "name" / "runs" / "not a run").mkdir()
        (self.data_dir / "repos" / "org" / "name" / "runs" / "stray-file").write_text("x")
        (self.config_dir / "repos" / "README.toml").write_text("x")
        (self.config_dir / "repos" / "not-a-repo.txt").write_text("x")
        status, payload, _ = self.call("GET", "/repos", principal=VIEWER)
        self.assertEqual(status, 200)
        repos = {entry["repo"]: entry for entry in payload["repos"]}
        self.assertEqual(sorted(repos), ["org/name", "org/other"])
        self.assertEqual(repos["org/name"]["base_branch"], "main")
        self.assertEqual(repos["org/name"]["runs"], [{"run": "r1", "planned": True, "started": True}, {"run": "r2", "planned": False, "started": False}])
        self.assertEqual(repos["org/other"]["runs"], [])
        self.assertIsNone(repos["org/name"]["supervisor"])
        status, started, _ = self.call("POST", self.path("run"))
        self.assertEqual(status, 202)
        _, payload, _ = self.call("GET", "/repos")
        supervisor = {entry["repo"]: entry for entry in payload["repos"]}["org/name"]["supervisor"]
        self.assertEqual((supervisor["kind"], supervisor["run"], supervisor["pid"], supervisor["running"]), ("run", "r1", started["pid"], True))

    def test_repos_flags_a_config_that_does_not_load(self):
        (self.config_dir / "repos" / "org__other.toml").write_text('repo = "org/other"\nbogus_key = 1\n')
        _, payload, _ = self.call("GET", "/repos")
        self.assertIn({"repo": "org/other", "error": "invalid configuration"}, payload["repos"])
        status, body, _ = self.call("GET", self.path("status", repo="org/other"))
        self.assertEqual((status, body), (500, {"error": "repository configuration is invalid"}))
        self.assertNotIn("bogus_key", self.err.getvalue())


# --- request bodies ------------------------------------------------------------------


class BodyTests(ServerCase):
    def setUp(self):
        super().setUp()
        self.make_run()

    def assert_rejected(self, action, body=None, raw=None, status=400):
        got, payload, _ = self.call("POST", self.path(action), body=body, raw=raw)
        self.assertEqual(got, status, payload)
        self.assertIn("error", payload)
        self.assertEqual((self.runner.calls, self.spawner.calls), ([], []))
        return payload

    def test_strict_schemas_reject_bad_bodies_before_anything_runs(self):
        cases = [
            ("plan", {"limit": 0}),
            ("plan", {"limit": 501}),
            ("plan", {"limit": -3}),
            ("plan", {"limit": True}),
            ("plan", {"limit": "5"}),
            ("plan", {"limit": 5.0}),
            ("plan", {"limit": None}),
            ("plan", {"issues": []}),
            ("plan", {"issues": [0]}),
            ("plan", {"issues": [-4]}),
            ("plan", {"issues": [True]}),
            ("plan", {"issues": [1.0]}),
            ("plan", {"issues": ["1"]}),
            ("plan", {"issues": "1,2"}),
            ("plan", {"issues": 7}),
            ("plan", {"issues": None}),
            ("plan", {"issues": [[1]]}),
            ("plan", {"issues": [2**31]}),
            ("plan", {"issues": list(range(1, 102))}),
            ("plan", {"issues": [1], "limit": 5}),  # the two ways of choosing issues exclude each other
            ("plan", {"timeout": 0}),
            ("plan", {"timeout": mgr.MAX_TIMEOUT_SECONDS + 1}),
            ("plan", {"extra": 1}),
            ("plan", {"budgets": {"build": 1}}),
            ("plan", {"budgets": {"plan": 0}}),
            ("plan", {"budgets": {"plan": -1}}),
            ("plan", {"budgets": {"plan": True}}),
            ("plan", {"budgets": {"plan": "5"}}),
            ("plan", {"budgets": {}}),
            ("plan", {"budgets": []}),
            ("run", {"concurrency": 0}),
            ("run", {"concurrency": 65}),
            ("run", {"verification_concurrency": "1"}),
            ("run", {"review_concurrency": 1.5}),
            ("run", {"max_active_agents": 0}),
            ("run", {"review_rounds": 4}),
            ("run", {"review_rounds": -1}),
            ("run", {"timeout": 0}),
            ("run", {"budgets": {"plan": 1}}),
            ("run", {"budgets": {"build": float("inf")}}),
            ("run", {"verify_domain": ["x.example"]}),
            ("run", {"plan_file": "/etc/passwd"}),
            ("drain", {"x": 1}),
            ("pause", {"x": 1}),
            ("purge", {"force": True}),
            ("resume", {"job": "j"}),
            ("resume", {"phase": "review"}),
            ("resume", {"job": "j", "phase": "build"}),
            ("resume", {"job": "j", "phase": "REVIEW"}),
            ("resume", {"job": "-j", "phase": "review"}),
            ("resume", {"job": "a/b", "phase": "review"}),
            ("resume", {"job": "a b", "phase": "review"}),
            ("resume", {"job": "", "phase": "review"}),
            ("resume", {"job": "j" * 65, "phase": "review"}),
            ("resume", {"job": 5, "phase": "review"}),
            ("resume", {"job": "j", "phase": "review", "reason": 5}),
            ("resume", {"job": "j", "phase": "review", "reason": "a\u0000b"}),
            ("resume", {"job": "j", "phase": "review", "reason": "x" * 2001}),
            ("resume", {"job": "j", "phase": "review", "extra": 1}),
            ("retry", {"reason": "x"}),
            ("retry", {"job": "j", "extra": 1}),
            ("retry", {"job": "j", "reason": None}),
            ("preflight", {"job": "j"}),
            ("preflight", {"job": "j", "phase": "publish"}),
            ("preflight", {"job": "j", "phase": "review"}),
            ("preflight", {"job": "j", "phase": "build", "timeout": 5}),
        ]
        for action, body in cases:
            with self.subTest(action=action, body=body):
                raw = json.dumps(body).encode()
                self.assert_rejected(action, raw=raw)

    def test_malformed_json_is_400(self):
        for label, raw in {
            "not json": b"{not json",
            "bare string": b'"x"',
            "list": b"[]",
            "number": b"5",
            "null": b"null",
            "NaN": b'{"limit": NaN}',
            "Infinity": b'{"budgets": {"plan": Infinity}}',
            "duplicate key": b'{"limit": 1, "limit": 2}',
            "bad utf8": b'{"reason": "\xff"}',
            "lone surrogate": b'{"job": "j", "phase": "review", "reason": "\\ud800"}',
            "huge integer": b'{"limit": %s}' % (b"9" * 5000),
        }.items():
            with self.subTest(label):
                self.assert_rejected("resume" if "surrogate" in label else "plan", raw=raw)

    def test_oversize_bodies_are_413_and_the_boundary_is_exact(self):
        limit = mgr.DEFAULT_MAX_BODY
        self.assert_rejected("plan", raw=b"{}%s" % (b" " * (limit - 1)), status=413)
        self.assert_rejected("resume", raw=b"x" * (limit * 4), status=413)
        status, _, _ = self.call("POST", self.path("drain"), raw=b"{}%s" % (b" " * (limit - 2)))
        self.assertEqual(status, 200)

    def test_invalid_content_length_and_chunked_bodies_are_rejected(self):
        prefix = f"POST {self.path('drain')} HTTP/1.0\r\nX-MS-CLIENT-PRINCIPAL: {OPERATOR}\r\n".encode()
        for header, expected in ((b"Content-Length: abc\r\n", 400), (b"Content-Length: -5\r\n", 400), (b"Transfer-Encoding: chunked\r\n", 411)):
            with self.subTest(header):
                reply = self.raw_exchange(prefix + header + b"\r\n0\r\n\r\n")
                self.assertIn(f" {expected} ".encode(), reply.split(b"\r\n", 1)[0])
        self.assertEqual(self.runner.calls, [])

    def test_valid_edge_values_are_accepted_and_reach_argv_unchanged(self):
        cases = [
            ({"limit": 1}, ["plan", "--limit", "1"]),
            ({"limit": 500, "timeout": mgr.MAX_TIMEOUT_SECONDS}, ["plan", "--limit", "500", "--timeout", "86400"]),
            ({"budgets": {"plan": 0.5}}, ["plan", "--budget-plan", "0.5"]),
            ({"budgets": {"plan": 3}}, ["plan", "--budget-plan", "3"]),
            ({"issues": [3, 5, 3]}, ["plan", "--issues=3,5"]),
            ({"issues": [2**31 - 1]}, ["plan", "--issues=2147483647"]),
            ({"issues": list(range(100, 0, -1))}, ["plan", "--issues=" + ",".join(map(str, range(100, 0, -1)))]),
            ({"issues": [9], "timeout": 60, "budgets": {"plan": 2}}, ["plan", "--timeout", "60", "--issues=9", "--budget-plan", "2"]),
        ]
        for index, (body, tail) in enumerate(cases):
            with self.subTest(body=body):
                status, _, _ = self.call("POST", self.path("plan", run=f"p{index}"), body=body)
                self.assertEqual(status, 202)
                self.assertEqual(self.spawner.argvs[-1], [*self.base_argv(f"p{index}"), *tail])
                self.spawner.processes[-1].returncode = 0  # let the next plan start

    def test_free_text_goes_to_argv_as_one_flag_value_element(self):
        for reason in ("--help", "-x --evil", "has spaces and 'quotes' and $(subshell)", "", "x" * 2000, "line\nbreak", "tab\there", "é€\U0001f600"):
            with self.subTest(reason=reason[:20]):
                status, _, _ = self.call("POST", self.path("retry"), body={"job": "fix-a", "reason": reason})
                self.assertEqual(status, 200)
                self.assertEqual(self.runner.argvs[-1], [*self.base_argv(), "retry", "fix-a", f"--reason={reason}"])

    def test_error_details_never_echo_the_request(self):
        payload = self.assert_rejected("resume", body={"job": "j", "phase": "review", "SECRET-KEY-NAME": "SECRET-VALUE"})
        self.assertNotIn("SECRET", json.dumps(payload))
        payload = self.assert_rejected("resume", body={"job": "j", "phase": "SECRET-PHASE"})
        self.assertNotIn("SECRET", json.dumps(payload))

    def test_slow_body_is_408_and_a_stalled_client_is_dropped(self):
        self.serve(request_timeout=0.5)
        prefix = f"POST {self.path('drain')} HTTP/1.0\r\nX-MS-CLIENT-PRINCIPAL: {OPERATOR}\r\nContent-Length: 100\r\n\r\n".encode()
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as connection:
            connection.sendall(prefix + b"{}")  # 2 of 100 bytes, then silence
            reply = connection.recv(65536)
        self.assertIn(b" 408 ", reply.split(b"\r\n", 1)[0])
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as connection:
            connection.sendall(b"GET /heal")  # never finishes the request line
            started = time.monotonic()
            self.assertEqual(connection.recv(65536), b"")  # the server gave up and closed
            self.assertLess(time.monotonic() - started, 5)


    def test_a_body_dripped_a_byte_at_a_time_hits_the_total_deadline(self):
        self.serve(request_timeout=1)
        prefix = f"POST {self.path('drain')} HTTP/1.0\r\nX-MS-CLIENT-PRINCIPAL: {OPERATOR}\r\nContent-Length: 600\r\n\r\n".encode()
        with unittest.mock.patch.object(mgr, "BODY_READ_SECONDS", 1.5):
            with socket.create_connection(("127.0.0.1", self.port), timeout=15) as connection:
                connection.sendall(prefix)
                started = time.monotonic()
                reply = b""
                while time.monotonic() - started < 10 and not reply:
                    try:
                        connection.sendall(b" ")
                    except OSError:
                        break
                    ready, _, _ = select.select([connection], [], [], 0.4)  # each byte is well inside the socket timeout
                    if ready:
                        reply = connection.recv(65536)
                elapsed = time.monotonic() - started
        self.assertIn(b" 408 ", reply.split(b"\r\n", 1)[0], reply)
        self.assertLess(elapsed, 6, "the total body deadline must cut a slow drip off")

    def test_request_line_and_headers_dripped_slowly_are_dropped(self):
        self.serve(request_timeout=2)
        with unittest.mock.patch.object(mgr, "HEADER_READ_SECONDS", 0.8):
            with socket.create_connection(("127.0.0.1", self.port), timeout=15) as connection:
                started = time.monotonic()
                closed = False
                for byte in b"GET /healthz HTTP/1.1\r\nX-A: b" * 3:
                    try:
                        connection.sendall(bytes([byte]))
                    except OSError:
                        closed = True
                        break
                    ready, _, _ = select.select([connection], [], [], 0.25)  # always inside the socket timeout
                    if ready and connection.recv(65536) == b"":
                        closed = True
                        break
                    if time.monotonic() - started > 8:
                        break
                elapsed = time.monotonic() - started
        self.assertTrue(closed, "the connection must be cut after the header deadline")
        self.assertLess(elapsed, 6)

    def test_a_normal_request_is_not_affected_by_the_header_timer(self):
        self.assertEqual(self.call("GET", "/healthz", principal=None)[0], 200)
        self.assertEqual(self.call("POST", self.path("drain"), body={})[0], 200)


class ConcurrencyLimitTests(ServerCase):
    def setUp(self):
        super().setUp()
        self.make_run("r1")
        self.serve(max_status_commands=1, max_other_commands=1)
        self.gate = threading.Event()
        self.entered = threading.Event()

        def respond(argv):
            if "status" in argv:
                self.entered.set()
                self.gate.wait(10)
            return CommandResult(0, b"{}\n", b"")

        self.runner.respond = respond
        patcher = unittest.mock.patch.object(mgr, "SLOT_WAIT_SECONDS", 0.2)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.gate.set)

    def test_status_requests_beyond_the_cap_get_503_with_retry_after(self):
        held = []
        thread = threading.Thread(target=lambda: held.append(self.call("GET", self.path("status"))))
        thread.start()
        self.assertTrue(self.entered.wait(5))
        status, payload, headers = self.call("GET", self.path("status"))
        self.assertEqual(status, 503, payload)
        self.assertEqual(headers.get("Retry-After"), "5")
        self.gate.set()
        thread.join(10)
        self.assertEqual(held[0][0], 200)
        self.assertEqual(self.call("GET", self.path("status"))[0], 200)  # the slot is free again

    def test_a_busy_status_lane_never_blocks_operator_commands(self):
        thread = threading.Thread(target=lambda: self.call("GET", self.path("status")))
        thread.start()
        self.assertTrue(self.entered.wait(5))
        self.assertEqual(self.call("POST", self.path("drain"), body={})[0], 200)
        self.gate.set()
        thread.join(10)

    def test_a_failed_command_releases_its_slot(self):
        self.runner.respond = lambda argv: (_ for _ in ()).throw(OSError("boom"))
        for _ in range(3):
            self.assertEqual(self.call("GET", self.path("status"))[0], 500)


# --- argv snapshot -------------------------------------------------------------------


class ArgvTests(ServerCase):
    """The exact argv each endpoint hands to the runner or spawner."""

    def setUp(self):
        super().setUp()
        self.make_run("r1")
        self.make_run("r2", plan=False)

    def inbox_path(self, raw, run="r1"):
        return str(self.run_dir(run) / "inbox" / f"{hashlib.sha256(raw).hexdigest()}.json")

    def test_every_endpoint_executes_exactly_this_argv(self):
        blocker = json.dumps({"id": "b1", "cause": "c", "remedy": "r", "owner": "o"}).encode()
        capabilities = json.dumps([{"id": "c1", "environment": "e", "status": "unconfirmed"}]).encode()
        evidence = json.dumps({"job_id": "fix-a", "commit": "0" * 40}).encode()
        run_all = {
            "concurrency": 3,
            "verification_concurrency": 2,
            "review_concurrency": 4,
            "max_active_agents": 6,
            "timeout": 900,
            "review_rounds": 2,
            "budgets": {"verify": 1.5, "build": 8, "review": 4.25},
        }
        run_flags = [
            "--concurrency", "3", "--verification-concurrency", "2", "--review-concurrency", "4", "--max-active-agents", "6",
            "--timeout", "900", "--review-rounds", "2", "--budget-build", "8", "--budget-review", "4.25", "--budget-verify", "1.5",
        ]  # fmt: skip
        spawned = [
            ("plan", "r2", {"limit": 7}, ["plan", "--limit", "7"]),
            ("plan", "r2", {}, ["plan"]),
            ("plan", "r2", {"issues": [12, 15]}, ["plan", "--issues=12,15"]),
            ("plan", "r2", {"limit": 10, "timeout": 600, "budgets": {"plan": 2.5}}, ["plan", "--limit", "10", "--timeout", "600", "--budget-plan", "2.5"]),
            ("run", "r1", None, ["run"]),
            ("run", "r1", run_all, ["run", *run_flags]),
        ]
        for action, run, body, tail in spawned:
            with self.subTest(action=action, body=body):
                status, payload, _ = self.call("POST", self.path(action, run), body=body)
                self.assertEqual(status, 202, payload)
                self.assertEqual(self.spawner.argvs[-1], [*self.base_argv(run), *tail])
                self.spawner.processes[-1].returncode = 0  # the child "exits", so the next one may start
        synchronous = [
            ("GET", "status", None, None, ["status", "--json"]),
            ("POST", "drain", None, None, ["drain"]),
            ("POST", "drain", {}, None, ["drain"]),
            ("POST", "resume", {"job": "fix-a", "phase": "review", "reason": "tests fixed"}, None, ["resume", "fix-a", "--phase", "review", "--reason=tests fixed"]),
            ("POST", "resume", {"job": "fix-a", "phase": "publish"}, None, ["resume", "fix-a", "--phase", "publish"]),
            ("POST", "resume", {"job": "fix-a", "phase": "verify", "reason": "-dash"}, None, ["resume", "fix-a", "--phase", "verify", "--reason=-dash"]),
            ("POST", "retry", {"job": "fix-a", "reason": "new approach"}, None, ["retry", "fix-a", "--reason=new approach"]),
            ("POST", "retry", {"job": "fix-a"}, None, ["retry", "fix-a"]),
            ("POST", "preflight", {"job": "fix-a", "phase": "build"}, None, ["preflight", "fix-a", "--phase", "build"]),
            ("POST", "preflight", {"job": "fix-a", "phase": "verify"}, None, ["preflight", "fix-a", "--phase", "verify"]),
            ("POST", "record-capabilities", None, capabilities, ["record-capabilities", "--file", self.inbox_path(capabilities)]),
            ("POST", "record-blocker", None, blocker, ["record-blocker", "--file", self.inbox_path(blocker)]),
            ("POST", "import-evidence", None, evidence, ["import-evidence", "fix-a", "--file", self.inbox_path(evidence)]),
            ("POST", "purge", None, None, ["purge"]),
        ]
        for method, action, body, raw, tail in synchronous:
            with self.subTest(action=action, body=body or raw):
                before = len(self.runner.calls)
                status, payload, _ = self.call(method, self.path(action), body=body, raw=raw)
                self.assertEqual(status, 200, payload)
                self.assertEqual(len(self.runner.calls), before + 1)
                self.assertEqual(self.runner.argvs[-1], [*self.base_argv(), *tail])
                self.assertEqual(self.runner.calls[-1][1], self.manager.settings.command_timeout)

    def test_pause_signals_the_child_then_reports_status(self):
        self.assertEqual(self.call("POST", self.path("run"))[0], 202)
        process = self.spawner.processes[0]
        status, payload, _ = self.call("POST", self.path("pause"))
        self.assertEqual(status, 200, payload)
        self.assertEqual(process.signals, [signal.SIGINT])
        self.assertEqual(self.runner.argvs, [[*self.base_argv(), "status", "--json"]])
        self.assertEqual(payload["pause"], {"pid": process.pid, "kind": "run", "exited": True})
        self.assertEqual(payload["returncode"], 0)

    def test_pause_without_a_child_started_here_is_409(self):
        status, payload, _ = self.call("POST", self.path("pause"))
        self.assertEqual((status, payload["error"]), (409, "this manager has no running process for this run"))
        self.assertEqual(self.runner.calls, [])

    def test_pause_that_times_out_is_504_and_does_not_signal_twice(self):
        self.serve(pause_seconds=0.2, spawner=FakeSpawner(exit_on_sigint=False))
        self.spawner = self.manager.spawner
        self.assertEqual(self.call("POST", self.path("run"))[0], 202)
        for _ in range(2):
            status, payload, _ = self.call("POST", self.path("pause"))
            self.assertEqual((status, payload["exited"]), (504, False))
        self.assertEqual(self.spawner.processes[0].signals, [signal.SIGINT])  # one SIGINT only
        self.assertEqual(self.runner.calls, [])

    def test_every_argv_is_accepted_by_the_real_cli_parser(self):
        """The flags and choices the manager emits exist in cli.build_parser (no drift)."""
        self.test_every_endpoint_executes_exactly_this_argv()
        argvs = [*self.spawner.argvs, *self.runner.argvs]
        self.assertGreater(len(argvs), 15)
        for argv in argvs:
            with self.subTest(argv=argv[len(self.cli) :]):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    try:
                        args = build_parser().parse_args(argv[len(self.cli) :])
                    except SystemExit:
                        self.fail(f"the CLI rejects this argv: {stderr.getvalue()}")
                self.assertEqual(args.repo, "org/name")
                self.assertTrue(args.command)

    def test_status_returns_the_parsed_json_and_the_manager_view(self):
        self.runner.respond = lambda argv: CommandResult(0, b'{"jobs": [{"id": "a"}]}\n', b"warn\n")
        status, payload, _ = self.call("GET", self.path("status"))
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], {"jobs": [{"id": "a"}]})
        self.assertEqual((payload["returncode"], payload["output"], payload["error"]), (0, '{"jobs": [{"id": "a"}]}\n', "warn\n"))
        self.assertEqual(payload["manager"], {"child": None, "last_exit": None})

    def test_status_with_plain_text_output_has_no_parsed_form(self):
        self.runner.respond = lambda argv: CommandResult(0, b"Planned 3 issues in 2 jobs\n", b"")
        _, payload, _ = self.call("GET", self.path("status"))
        self.assertNotIn("status", payload)
        self.assertEqual(payload["output"], "Planned 3 issues in 2 jobs\n")

    def test_status_shows_the_child_this_manager_started(self):
        self.assertEqual(self.call("POST", self.path("run"))[0], 202)
        _, payload, _ = self.call("GET", self.path("status"))
        child = payload["manager"]["child"]
        self.assertEqual((child["kind"], child["run"], child["running"]), ("run", "r1", True))
        self.spawner.processes[0].returncode = 7
        _, payload, _ = self.call("GET", self.path("status"))
        self.assertIsNone(payload["manager"]["child"])
        self.assertEqual(payload["manager"]["last_exit"]["returncode"], 7)

    def test_a_failing_command_is_422_with_the_cli_streams(self):
        self.runner.respond = lambda argv: CommandResult(1, b"partial\n", b"factory: unknown job\n")
        status, payload, _ = self.call("POST", self.path("retry"), body={"job": "nope"})
        self.assertEqual(status, 422)
        self.assertEqual((payload["returncode"], payload["output"], payload["error"]), (1, "partial\n", "factory: unknown job\n"))

    def test_a_timed_out_command_is_504(self):
        self.runner.respond = lambda argv: CommandResult(None, timed_out=True)
        status, payload, _ = self.call("POST", self.path("drain"))
        self.assertEqual(status, 504)
        self.assertEqual(payload["error"], "command timed out")

    def test_purge_forgets_the_finished_child(self):
        self.assertEqual(self.call("POST", self.path("run"))[0], 202)
        self.spawner.processes[0].returncode = 0
        self.assertEqual(self.call("GET", "/repos")[0], 200)
        self.assertEqual(len(self.manager.children.last_exit("org/name")), 1)
        self.assertEqual(self.call("POST", self.path("purge"))[0], 200)
        self.assertEqual(self.manager.children.last_exit("org/name"), [])

    def test_output_is_truncated_not_unbounded(self):
        big = b"x" * (mgr.MAX_OUTPUT_CHARS + 10)
        self.runner.respond = lambda argv: CommandResult(0, big, b"")
        _, payload, _ = self.call("POST", self.path("drain"))
        self.assertEqual((len(payload["output"]), payload["truncated"]), (mgr.MAX_OUTPUT_CHARS, True))

    def test_runner_failures_are_500_without_leaking_detail(self):
        def explode(argv):
            raise FileNotFoundError(2, "No such file", "/secret/path/factory")

        self.runner.respond = explode
        status, payload, _ = self.call("POST", self.path("drain"))
        self.assertEqual((status, payload), (500, {"error": "could not start the factory CLI"}))
        self.assertNotIn("/secret/path", self.err.getvalue() + self.out.getvalue())
        self.runner.respond = lambda argv: (_ for _ in ()).throw(RuntimeError("SECRET-DETAIL"))
        status, payload, _ = self.call("POST", self.path("drain"))
        self.assertEqual((status, payload), (500, {"error": "internal error"}))
        self.assertIn("RuntimeError", self.err.getvalue())
        self.assertNotIn("SECRET-DETAIL", self.err.getvalue() + self.out.getvalue())


# --- inbox ---------------------------------------------------------------------------


class InboxTests(ServerCase):
    def setUp(self):
        super().setUp()
        self.make_run()

    def test_body_is_stored_content_addressed_and_private(self):
        body = json.dumps({"id": "b1", "cause": "c"}, indent=2).encode()
        status, payload, _ = self.call("POST", self.path("record-blocker"), raw=body)
        self.assertEqual(status, 200, payload)
        inbox = self.run_dir() / "inbox"
        stored = inbox / f"{hashlib.sha256(body).hexdigest()}.json"
        self.assertEqual(stored.read_bytes(), body)
        self.assertEqual(stat.S_IMODE(stored.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(inbox.stat().st_mode), 0o700)
        self.assertEqual([entry.name for entry in inbox.iterdir()], [stored.name])  # no temp file left
        self.assertEqual(self.runner.argvs[-1], [*self.base_argv(), "record-blocker", "--file", str(stored)])

    def test_replaying_the_same_body_reuses_the_same_file(self):
        body = b'{"id": "b1"}'
        for _ in range(2):
            self.assertEqual(self.call("POST", self.path("record-blocker"), raw=body)[0], 200)
        self.assertEqual(len(list((self.run_dir() / "inbox").iterdir())), 1)

    def test_each_endpoint_demands_the_json_type_its_cli_loader_expects(self):
        listing = b'[{"id": "c1"}]'
        document = b'{"id": "b1", "job_id": "fix-a"}'
        self.assertEqual(self.call("POST", self.path("record-capabilities"), raw=listing)[0], 200)
        self.assertEqual(self.call("POST", self.path("record-blocker"), raw=document)[0], 200)
        self.assertEqual(self.call("POST", self.path("import-evidence"), raw=document)[0], 200)
        executed = len(self.runner.calls)
        for action, raw in (
            ("record-capabilities", document),
            ("record-capabilities", b'["not an object"]'),
            ("record-blocker", listing),
            ("import-evidence", listing),
            ("record-blocker", b""),
            ("record-blocker", b"   "),
            ("record-blocker", b"{not json"),
            ("record-blocker", b'{"a": NaN}'),
            ("import-evidence", b'{"id": "x"}'),  # no job_id
            ("import-evidence", b'{"job_id": "-x"}'),
            ("import-evidence", b'{"job_id": 5}'),
            ("import-evidence", b'{"job_id": "a/b"}'),
        ):
            with self.subTest(action=action, raw=raw[:20]):
                status, _, _ = self.call("POST", self.path(action), raw=raw)
                self.assertEqual(status, 400)
        self.assertEqual(len(self.runner.calls), executed)

    def test_the_cap_is_one_mebibyte(self):
        pad = mgr.DEFAULT_MAX_INBOX - len(b'{"id":"b1","pad":""}')
        body = b'{"id":"b1","pad":"%s"}' % (b"a" * pad)
        self.assertEqual(len(body), mgr.DEFAULT_MAX_INBOX)
        self.assertEqual(self.call("POST", self.path("record-blocker"), raw=body)[0], 200)
        before = list((self.run_dir() / "inbox").iterdir())
        status, _, _ = self.call("POST", self.path("record-blocker"), raw=body + b" ")
        self.assertEqual(status, 413)
        self.assertEqual(list((self.run_dir() / "inbox").iterdir()), before)

    def test_unknown_run_directory_is_404_and_creates_nothing(self):
        status, payload, _ = self.call("POST", self.path("record-blocker", run="nope"), raw=b"{}")
        self.assertEqual((status, payload), (404, {"error": "run not found"}))
        self.assertFalse(self.run_dir("nope").exists())

    def test_an_inbox_that_is_a_symlink_is_refused(self):
        target = self.run_dir() / "elsewhere"
        target.mkdir()
        (self.run_dir() / "inbox").symlink_to(target)
        status, payload, _ = self.call("POST", self.path("record-blocker"), raw=b'{"id": "b"}')
        self.assertEqual((status, payload["error"]), (500, "could not store the request body"))
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual(self.runner.calls, [])


# --- children ------------------------------------------------------------------------


class ChildProcessTests(ServerCase):
    """Real processes: the fake CLI script stands in for the factory CLI."""

    def setUp(self):
        super().setUp()
        script = self.data_dir.parent / "fake_cli.py"
        script.write_text(FAKE_CLI)
        self.cli = (sys.executable, str(script))

    def serve_real(self, **extra_env):
        self.serve(runner=None, spawner=None, env=self.env(**extra_env))

    def test_run_starts_a_detached_child_with_a_private_log(self):
        self.make_run()
        self.serve_real()
        status, payload, _ = self.call("POST", self.path("run"), body={"concurrency": 2})
        self.assertEqual(status, 202, payload)
        self.assertEqual(set(payload), {"pid", "started", "kind"})
        pid = payload["pid"]
        log = self.run_dir() / "supervisor.log"
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
        self.assertTrue(wait_for((self.run_dir() / "ready-run").exists))
        self.assertEqual(os.getpgid(pid), pid)  # its own session and group leader
        self.assertTrue(wait_for(lambda: "SUPERVISOR-CHILD-OUTPUT" in log.read_text()))  # output went to the file
        self.assertNotIn("SUPERVISOR-CHILD-OUTPUT", self.out.getvalue() + self.err.getvalue())
        self.assertEqual(self.manager.children.running()[0].pid, pid)

    def test_a_second_run_is_409_while_the_first_child_lives_and_allowed_after(self):
        self.make_run("r1")
        self.make_run("r2")
        self.serve_real()
        first = self.call("POST", self.path("run", "r1"))
        self.assertEqual(first[0], 202)
        self.assertTrue(wait_for((self.run_dir("r1") / "ready-run").exists))
        for action, run in (("run", "r1"), ("run", "r2"), ("plan", "r3")):
            with self.subTest(action=action, run=run):
                status, payload, _ = self.call("POST", self.path(action, run))
                self.assertEqual(status, 409)
                self.assertEqual((payload["kind"], payload["run"]), ("run", "r1"))
        self.assertFalse(self.run_dir("r3").exists())
        # another repository is independent
        self.make_run("r1", repo="org/other")
        self.assertEqual(self.call("POST", self.path("run", "r1", repo="org/other"))[0], 202)
        # the first child ends -> the manager reaps it and the repo is free again
        os.kill(first[1]["pid"], signal.SIGINT)
        self.assertTrue(wait_for(lambda: self.call("POST", self.path("run", "r2"))[0] == 202))

    def test_plan_logs_to_plan_log_and_a_finished_child_is_reaped_with_its_exit_code(self):
        self.serve_real(FAKE_PLAN_QUICK="1")
        status, payload, _ = self.call("POST", self.path("plan", "r9"), body={"limit": 3})
        self.assertEqual(status, 202, payload)
        log = self.run_dir("r9") / "plan.log"
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
        self.assertTrue(wait_for(lambda: "PLAN-CHILD-OUTPUT" in log.read_text()))

        def finished():
            _, listing, _ = self.call("GET", "/repos")
            entry = {row["repo"]: row for row in listing["repos"]}["org/name"]
            return entry["recent"] if entry["supervisor"] is None else None

        recent = wait_for(finished)
        self.assertEqual([(row["kind"], row["run"], row["returncode"]) for row in recent], [("plan", "r9", 3)])
        self.assertEqual(self.call("POST", self.path("plan", "r9"))[0], 202)  # same dir, no plan.json yet
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)

    def test_plan_for_an_already_planned_run_and_run_without_a_plan_are_refused(self):
        self.make_run("r1")
        self.make_run("r2", plan=False)
        self.serve_real()
        status, payload, _ = self.call("POST", self.path("plan", "r1"))
        self.assertEqual((status, payload), (409, {"error": "run already planned"}))
        status, payload, _ = self.call("POST", self.path("run", "r2"))
        self.assertEqual((status, payload), (404, {"error": "run has no plan"}))
        status, payload, _ = self.call("POST", self.path("run", "r7"))
        self.assertEqual(status, 404)
        self.assertFalse(self.run_dir("r7").exists())
        self.assertEqual(self.manager.children.running(), [])

    def test_log_file_permissions_are_tightened_and_symlinks_are_not_followed(self):
        directory = self.make_run()
        loose = directory / "supervisor.log"
        loose.write_text("old\n")
        loose.chmod(0o644)
        self.serve_real()
        self.assertEqual(self.call("POST", self.path("run"))[0], 202)
        self.assertEqual(stat.S_IMODE(loose.stat().st_mode), 0o600)
        self.assertEqual(loose.read_text().splitlines()[0], "old")  # appended, not truncated
        plan_target = directory.parent / "victim.txt"
        (directory / "plan.log").symlink_to(plan_target)
        self.stop()
        self.serve_real()
        (directory / "plan.json").unlink()
        status, _, _ = self.call("POST", self.path("plan"))
        self.assertEqual(status, 500)
        self.assertFalse(plan_target.exists())

    def test_real_pause_interrupts_the_child_and_then_reports_status(self):
        self.make_run()
        self.serve(runner=self.runner, spawner=None, env=self.env())
        self.assertEqual(self.call("POST", self.path("run"))[0], 202)
        self.assertTrue(wait_for((self.run_dir() / "ready-run").exists))
        status, payload, _ = self.call("POST", self.path("pause"))
        self.assertEqual(status, 200, payload)
        self.assertTrue((self.run_dir() / "sigint-run").exists())  # the child saw SIGINT, not SIGKILL
        self.assertTrue(pid_gone(payload["pause"]["pid"]))
        self.assertEqual(self.runner.argvs, [[*self.base_argv(), "status", "--json"]])
        self.assertEqual(self.manager.children.running(), [])

    def test_a_short_command_that_times_out_is_504_and_its_group_is_killed(self):
        self.make_run()
        self.serve(runner=None, spawner=self.spawner, env=self.env(FAKE_SLEEP_COMMAND="drain"), command_timeout=1)
        started = time.monotonic()
        status, payload, _ = self.call("POST", self.path("drain"))
        self.assertEqual((status, payload["error"]), (504, "command timed out"))
        self.assertLess(time.monotonic() - started, 20)
        pid = int((self.run_dir() / "sleeper.pid").read_text())
        self.assertTrue(wait_for(lambda: pid_gone(pid)))

    def test_real_runner_returns_the_cli_streams_and_exit_code(self):
        self.make_run()
        self.serve(runner=None, spawner=self.spawner, env=self.env(FAKE_RC="3"))
        status, payload, _ = self.call("POST", self.path("drain"))
        self.assertEqual(status, 422)
        self.assertEqual((payload["returncode"], payload["output"], payload["error"]), (3, "SECRET-CLI-OUTPUT\n", "SECRET-CLI-ERROR\n"))

    def test_children_inherit_the_managers_environment(self):
        self.make_run()
        self.serve_real(ANTHROPIC_API_KEY="sk-test-placeholder")
        _, payload, _ = self.call("GET", self.path("status"))
        self.assertEqual(payload["status"]["key"], "sk-test-placeholder")  # short commands
        self.assertEqual(self.call("POST", self.path("run"))[0], 202)
        self.assertTrue(wait_for((self.run_dir() / "ready-run").exists))
        self.assertEqual((self.run_dir() / "env-run").read_text(), "sk-test-placeholder")  # long-lived children
        self.assertNotIn("sk-test-placeholder", self.out.getvalue() + self.err.getvalue())


class ShutdownTests(ServerCase):
    def test_terminate_children_interrupts_every_group_then_kills_the_stubborn(self):
        self.make_run("r1")
        self.make_run("r1", repo="org/other")
        stubborn = FakeSpawner(exit_on_sigint=False)
        self.serve(spawner=stubborn)
        self.assertEqual(self.call("POST", self.path("run", "r1"))[0], 202)
        self.assertEqual(self.call("POST", self.path("run", "r1", repo="org/other"))[0], 202)
        summary = self.manager.terminate_children(grace=0.2)
        self.assertEqual(summary, {"interrupted": 2, "killed": 2, "killed_commands": 0})
        for process in stubborn.processes:
            self.assertEqual(process.signals, [signal.SIGINT, signal.SIGKILL])
        self.assertIn("2 process group(s)", self.err.getvalue())
        status, payload, _ = self.call("POST", self.path("run", "r1"))
        self.assertEqual((status, payload["error"]), (503, "manager is shutting down"))

    def test_terminate_children_lets_cooperative_children_exit_without_a_kill(self):
        self.make_run("r1")
        self.assertEqual(self.call("POST", self.path("run", "r1"))[0], 202)
        summary = self.manager.terminate_children(grace=5)
        self.assertEqual(summary, {"interrupted": 1, "killed": 0, "killed_commands": 0})
        self.assertEqual(self.spawner.processes[0].signals, [signal.SIGINT])
        self.assertEqual(self.err.getvalue(), "")

    def test_a_failed_signal_is_reported_and_does_not_stop_the_others(self):
        self.make_run("r1")
        self.make_run("r1", repo="org/other")
        self.assertEqual(self.call("POST", self.path("run", "r1"))[0], 202)
        self.assertEqual(self.call("POST", self.path("run", "r1", repo="org/other"))[0], 202)
        first, second = self.spawner.processes

        def refuse(sig):
            raise PermissionError(1, "Operation not permitted")

        first.signal_group = refuse
        self.manager.terminate_children(grace=0.2)
        self.assertEqual(second.signals, [signal.SIGINT])
        self.assertIn("cannot signal process group", self.err.getvalue())
        self.assertIn("PermissionError", self.err.getvalue())


    def test_a_child_that_pause_already_interrupted_gets_no_second_sigint_at_shutdown(self):
        self.make_run("r1")
        self.serve(pause_seconds=0.2, spawner=FakeSpawner(exit_on_sigint=False))
        self.assertEqual(self.call("POST", self.path("run", "r1"))[0], 202)
        self.assertEqual(self.call("POST", self.path("pause", "r1"))[0], 504)
        summary = self.manager.terminate_children(grace=0.2)
        process = self.manager.spawner.processes[0]
        self.assertEqual(process.signals, [signal.SIGINT, signal.SIGKILL])
        self.assertEqual(summary["killed"], 1)

    def test_a_signal_that_did_not_arrive_does_not_count_as_an_interrupt(self):
        self.make_run("r1")
        self.serve(pause_seconds=0.1, spawner=FakeSpawner(exit_on_sigint=False))
        self.assertEqual(self.call("POST", self.path("run", "r1"))[0], 202)
        process = self.manager.spawner.processes[0]
        real, attempts = process.signal_group, []

        def flaky(sig):
            attempts.append(sig)
            if len(attempts) == 1:
                raise PermissionError(1, "Operation not permitted")
            return real(sig)

        process.signal_group = flaky
        self.assertEqual(self.call("POST", self.path("pause", "r1"))[0], 504)  # EPERM: not delivered
        self.manager.terminate_children(grace=0.1)
        self.assertEqual(attempts[:2], [signal.SIGINT, signal.SIGINT])  # shutdown tried again
        self.assertEqual(process.signals[0], signal.SIGINT)

    def test_in_flight_short_commands_get_sigint_and_the_grace_then_sigkill(self):
        self.runner.live = 1
        summary = self.manager.terminate_children(grace=0.3)
        self.assertEqual(self.runner.interrupted, 1)
        self.assertEqual(self.runner.killed, 1)
        self.assertEqual(summary["killed_commands"], 1)

    def test_a_short_command_that_finishes_within_the_grace_is_not_killed(self):
        self.runner.live = 1
        threading.Timer(0.2, lambda: setattr(self.runner, "live", 0)).start()
        summary = self.manager.terminate_children(grace=5)
        self.assertEqual(self.runner.interrupted, 1)
        self.assertEqual(summary["killed_commands"], 0)

    def test_commands_after_shutdown_began_are_refused_with_503(self):
        self.manager.terminate_children(grace=0.1)
        status, payload, _ = self.call("GET", self.path("status"))
        self.assertEqual((status, payload["error"]), (503, "manager is shutting down"))
        self.assertEqual(self.runner.calls, [])


class SubprocessRunnerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.script = self.root / "slow.py"
        self.script.write_text(
            "import pathlib, signal, sys, time\n"
            "def stop(*_):\n"
            "    pathlib.Path(sys.argv[1]).write_text('sigint')\n"
            "    sys.exit(0)\n"
            "signal.signal(signal.SIGINT, stop)\n"
            "pathlib.Path(sys.argv[1] + '.ready').write_text('x')\n"
            "time.sleep(30)\n"
        )
        self.runner = mgr.SubprocessRunner(dict(os.environ), self.root)

    def test_interrupt_all_delivers_sigint_to_the_running_command(self):
        marker = self.root / "marker"
        results = []
        thread = threading.Thread(target=lambda: results.append(self.runner([sys.executable, str(self.script), str(marker)], 30)))
        thread.start()
        self.assertTrue(wait_for(lambda: (self.root / "marker.ready").exists()))
        self.assertEqual(self.runner.live_count(), 1)
        self.assertEqual(self.runner.interrupt_all(), 1)
        thread.join(10)
        self.assertEqual(marker.read_text(), "sigint")
        self.assertEqual(results[0].returncode, 0)
        self.assertEqual(self.runner.live_count(), 0)

    def test_a_closed_runner_starts_nothing(self):
        self.runner.close()
        with self.assertRaises(mgr.RunnerClosed):
            self.runner([sys.executable, "-c", "pass"], 5)
        self.assertEqual(self.runner.kill_all(), 0)


class SubprocessShutdownTests(unittest.TestCase):
    """A real manager process receives a real SIGTERM while a real child runs."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        (self.root / "config" / "repos").mkdir(parents=True)
        (self.root / "config" / "repos" / "org__name.toml").write_text('repo = "org/name"\n')
        self.run_dir = self.root / "data" / "repos" / "org" / "name" / "runs" / "r1"
        self.run_dir.mkdir(parents=True)
        (self.run_dir / "plan.json").write_text("{}")
        (self.root / "fake_cli.py").write_text(FAKE_CLI)
        self.process = None

    def start(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("FACTORY_", "FAKE_"))}
        env.update(
            PYTHONPATH=SRC,
            FACTORY_CONFIG=str(self.root / "config"),
            FACTORY_DATA=str(self.root / "data"),
            FACTORY_MANAGER_PORT="0",
            FACTORY_MANAGER_CLI=f"{sys.executable} {self.root / 'fake_cli.py'}",
            **extra,
        )
        self.process = subprocess.Popen(
            [sys.executable, "-m", "factory.manager"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self.cleanup_process)
        line = self.read_line()
        self.assertRegex(line, r"^factory-manager listening host=127\.0\.0\.1 port=\d+ repos=1$")
        self.port = int(line.split("port=")[1].split()[0])

    def cleanup_process(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.communicate()

    def read_line(self, timeout=15):
        ready, _, _ = select.select([self.process.stdout], [], [], timeout)
        self.assertTrue(ready, "the manager printed nothing")
        return self.process.stdout.readline().rstrip("\n")

    def post_run(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        connection.request("POST", "/repos/org/name/runs/r1/run", headers={"X-MS-CLIENT-PRINCIPAL": OPERATOR})
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 202, payload)
        self.assertTrue(wait_for((self.run_dir / "ready-run").exists))
        return payload["pid"]

    def test_sigterm_forwards_sigint_waits_and_exits_zero(self):
        self.start(FACTORY_MANAGER_SHUTDOWN_SECONDS="10")
        pid = self.post_run()
        self.process.send_signal(signal.SIGTERM)
        self.assertEqual(self.process.wait(timeout=30), 0)
        self.assertTrue((self.run_dir / "sigint-run").exists(), "the child never saw SIGINT")
        self.assertTrue(pid_gone(pid))
        stdout, stderr = self.process.communicate()
        self.assertEqual(stderr, "")  # nothing was killed, nothing to report
        for line in stdout.splitlines():
            self.assertRegex(line, r"^audit principal=\S+ method=POST path=/repos/org/name/runs/r1/run status=202 argv=\[.*\]$")
        self.assertNotIn("SUPERVISOR-CHILD-OUTPUT", stdout + stderr)
        self.assertNotIn("Traceback", stdout + stderr)

    def test_a_child_that_ignores_sigint_is_killed_after_the_grace_period(self):
        self.start(FACTORY_MANAGER_SHUTDOWN_SECONDS="1", FAKE_IGNORE_SIGINT="1")
        pid = self.post_run()
        started = time.monotonic()
        self.process.send_signal(signal.SIGTERM)
        self.assertEqual(self.process.wait(timeout=30), 0)
        self.assertLess(time.monotonic() - started, 20)
        self.assertTrue(wait_for(lambda: pid_gone(pid)))
        self.assertFalse((self.run_dir / "sigint-run").exists())
        _, stderr = self.process.communicate()
        self.assertIn("1 process group(s) and 0 command(s) were killed at shutdown", stderr)

    def test_sigint_stops_the_manager_the_same_way(self):
        self.start(FACTORY_MANAGER_SHUTDOWN_SECONDS="10")
        self.post_run()
        self.process.send_signal(signal.SIGINT)
        self.assertEqual(self.process.wait(timeout=30), 0)
        self.assertTrue((self.run_dir / "sigint-run").exists())

    def test_the_listener_closes_on_sigterm(self):
        self.start()
        self.process.send_signal(signal.SIGTERM)
        self.assertEqual(self.process.wait(timeout=30), 0)
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", self.port), timeout=2).close()


# --- lease probe ---------------------------------------------------------------------


class LeaseTests(ServerCase):
    def setUp(self):
        super().setUp()
        self.make_run()
        self.events = []

    def serve_with(self, lock):
        calls = []

        def factory_(cfg, paths):
            calls.append((cfg.repo, cfg.slug, str(paths.root)))
            return lock

        self.serve(lock_factory=factory_)
        return calls

    def test_free_lease_is_acquired_and_released_before_the_spawn(self):
        lock = FakeLock(events=self.events)
        calls = self.serve_with(lock)
        original = self.manager.spawner

        def spawn(argv, log):
            self.events.append("spawn")
            return original(argv, log)

        self.manager.spawner = spawn
        self.assertEqual(self.call("POST", self.path("run"))[0], 202)
        self.assertEqual(self.events, ["acquire", "release", "spawn"])
        self.assertEqual(calls, [("org/name", "org__name", str(self.data_dir / "repos" / "org" / "name"))])

    def test_busy_lease_is_409_and_nothing_is_spawned(self):
        self.serve_with(FakeLock(LockBusy("held"), self.events))
        for action in ("run", "plan"):
            with self.subTest(action):
                run = "r1" if action == "run" else "r5"
                status, payload, _ = self.call("POST", self.path(action, run))
                self.assertEqual((status, payload), (409, {"error": "lease held elsewhere"}))
        self.assertEqual(self.spawner.calls, [])
        self.assertEqual(self.events, ["acquire", "acquire"])  # never released: it was never held

    def test_unavailable_backend_is_503(self):
        for error in (LockUnavailable("storage down"), OSError(5, "I/O error")):
            with self.subTest(type(error).__name__):
                self.serve_with(FakeLock(error))
                status, payload, _ = self.call("POST", self.path("run"))
                self.assertEqual((status, payload), (503, {"error": "lease backend unavailable"}))
                self.assertNotIn("storage down", json.dumps(payload) + self.err.getvalue())
        self.assertEqual(self.spawner.calls, [])

    def test_the_default_probe_uses_the_same_lock_file_as_the_cli(self):
        self.serve()
        held = LocalFlockLock(self.data_dir / "repos" / "org" / "name" / "factory.lock").acquire()
        try:
            status, payload, _ = self.call("POST", self.path("run"))
            self.assertEqual((status, payload), (409, {"error": "lease held elsewhere"}))
            self.assertEqual(self.spawner.calls, [])
        finally:
            held.release()
        self.assertEqual(self.call("POST", self.path("run"))[0], 202)

    def test_lock_name_and_backend_follow_the_environment_like_the_cli(self):
        from factory.config import data_paths, load_repo_config

        cfg = load_repo_config("org/name", config_dir=self.config_dir)
        paths = data_paths(cfg, data_dir=self.data_dir)
        local = mgr.env_lock_factory({})(cfg, paths)
        self.assertIsInstance(local, LocalFlockLock)
        self.assertEqual(local.path, paths.root / "factory.lock")
        blob = mgr.env_lock_factory({"FACTORY_LOCK_ACCOUNT_URL": "https://example.blob.core.windows.net", "FACTORY_LOCK_CLIENT_ID": "x"})(cfg, paths)
        self.assertIsInstance(blob, BlobLeaseLock)
        self.assertIn("/locks/org__name", repr(blob))
        # losing the probe's lease must never take the manager down
        blob._on_lost()

    def test_active_child_conflict_is_decided_before_the_lease_is_probed(self):
        lock = FakeLock(events=self.events)
        self.serve_with(lock)
        self.assertEqual(self.call("POST", self.path("run"))[0], 202)
        self.events.clear()
        self.assertEqual(self.call("POST", self.path("run"))[0], 409)
        self.assertEqual(self.events, [])


# --- audit ---------------------------------------------------------------------------

AUDIT = r"^audit principal=\S+ method=[A-Z]+ path=\S+ status=\d{3} argv=\[.*\]$"


class AuditTests(ServerCase):
    def setUp(self):
        super().setUp()
        self.make_run()

    def test_audit_line_format_and_argv(self):
        self.call("POST", self.path("resume"), body={"job": "fix-a", "phase": "review", "reason": "TOPSECRET-REASON-TEXT"})
        self.call("GET", self.path("status"), principal=VIEWER)
        self.call("POST", self.path("drain"), principal=VIEWER)
        self.call("GET", self.path("status"), principal=None)
        lines = self.audit_lines()
        self.assertEqual(len(lines), 4)
        for line in lines:
            self.assertRegex(line, AUDIT)
        argv = json.dumps([*self.base_argv(), "resume", "fix-a", "--phase", "review", "--reason=<21 chars>"], separators=(",", ":"))
        self.assertEqual(lines[0], f"audit principal={OID} method=POST path=/repos/org/name/runs/r1/resume status=200 argv={argv}")
        self.assertIn(f"principal={OID} method=GET path=/repos/org/name/runs/r1/status status=200 argv=", lines[1])
        self.assertTrue(lines[2].endswith("status=403 argv=[]"))  # refused: nothing executed
        self.assertEqual(lines[3], "audit principal=anonymous method=GET path=/repos/org/name/runs/r1/status status=401 argv=[]")

    def test_nothing_from_the_request_or_the_cli_reaches_stdout_or_stderr(self):
        self.runner.respond = lambda argv: CommandResult(0, b"SECRET-CLI-OUTPUT\n", b"SECRET-CLI-ERROR\n")
        self.call("POST", self.path("retry"), body={"job": "fix-a", "reason": "TOPSECRET-REASON-TEXT"})
        self.call("POST", self.path("record-blocker"), raw=b'{"id": "TOPSECRET-BODY-TEXT"}')
        self.call("POST", self.path("plan", run="r8"), body={"limit": 3, "budgets": {"plan": 1}})
        self.call("POST", self.path("resume"), body={"job": "j", "phase": "nope", "reason": "TOPSECRET-REASON-TEXT"})
        self.call("POST", self.path("resume"), raw=b"TOPSECRET-BODY-TEXT not json")
        self.call("GET", self.path("status"))
        everything = self.out.getvalue() + self.err.getvalue()
        for secret in ("TOPSECRET", "SECRET-CLI", "fake-output"):
            self.assertNotIn(secret, everything)
        self.assertIn("--reason=<21 chars>", self.out.getvalue())
        self.assertEqual(len(self.audit_lines()), 6)

    def test_control_characters_in_logged_fields_are_escaped(self):
        name = "evil\nname\r\x1b[31m audit principal=root status=200 argv=[]"
        header = principal_header([mgr.ROLE_VIEWER], oid=None, name=name)
        self.call("GET", "/repos", principal=header)
        reply = self.raw_exchange(f"GET /x\x01y\x7fz HTTP/1.0\r\nX-MS-CLIENT-PRINCIPAL: {header}\r\n\r\n".encode("latin-1"))
        self.assertIn(b" 404 ", reply.split(b"\r\n", 1)[0])
        lines = self.out.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertRegex(line, AUDIT)
            self.assertNotRegex(line, r"[\x00-\x08\x0a-\x1f\x7f\x1b]")
        self.assertIn("principal=evil\\x0aname\\x0d\\x1b", lines[0])
        self.assertIn("path=/x\\x01y\\x7fz", lines[1])
        self.assertNotIn("principal=root", lines[0].split(" method=")[0])

    def test_long_fields_are_truncated(self):
        header = principal_header([mgr.ROLE_VIEWER], oid="A" * 500)
        self.call("GET", "/repos", principal=header)
        self.call("GET", f"/{'a' * 1000}", principal=header)
        first, second = self.audit_lines()
        self.assertIn(f"principal={'A' * 128} ", first)
        self.assertNotIn("A" * 129, first)
        self.assertLess(len(second), 400)
        self.assertIn("aaa... status=404", second)

    def test_unauthenticated_and_failed_requests_are_audited_too(self):
        self.call("GET", "/nope", principal=None)
        self.call("GET", "/nope")
        self.call("DELETE", "/repos")
        self.call("POST", "/healthz", principal=None)
        statuses = [line.split(" status=")[1].split()[0] for line in self.audit_lines()]
        self.assertEqual(statuses, ["401", "404", "405", "405"])

    def test_the_startup_line_reports_host_port_and_repo_count(self):
        line = mgr.startup_line(self.server, self.manager)
        self.assertEqual(line, f"factory-manager listening host=127.0.0.1 port={self.port} repos=2")


class PrivateOutputTests(unittest.TestCase):
    def test_the_module_never_prints(self):
        # Output goes through Sink only, so nothing can reach stdout by accident.
        text = Path(mgr.__file__).read_text()
        self.assertNotIn("print(", text)


# --- the real CLI --------------------------------------------------------------------


class RealCliTests(ServerCase):
    """GET status goes through the manager into the real ``python -m factory`` CLI."""

    PLAN = {
        "version": 1,
        "issues": [{"number": 1, "title": "Fix A", "body": "A"}],
        "jobs": [
            {
                "id": "fix-a",
                "issues": [1],
                "context_issues": [],
                "objective": "Fix A",
                "likely_paths": ["src"],
                "acceptance": ["A works"],
                "depends_on": [],
                "feature": False,
            }
        ],
        "dispositions": [{"issue": 1, "status": "job", "job": "fix-a", "reason": "Concrete fix"}],
    }

    def setUp(self):
        super().setUp()
        from factory.state import State

        directory = self.make_run("r1", plan=False)
        (directory / "plan.json").write_text(json.dumps(self.PLAN))
        State(directory, self.PLAN)
        self.serve(runner=None, spawner=None, cli=(sys.executable, "-m", "factory"), env=self.env(PYTHONPATH=SRC), command_timeout=120)

    def test_status_returns_the_cli_json_through_the_manager(self):
        status, payload, _ = self.call("GET", self.path("status"), principal=VIEWER)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["returncode"], 0)
        report = payload["status"]
        self.assertEqual([job["id"] for job in report["jobs"]], ["fix-a"])
        self.assertEqual(report["jobs"][0]["status"], "queued")
        self.assertEqual(report["cost_usd"], 0.0)
        self.assertEqual(json.loads(payload["output"]), report)  # the raw stdout is the same document
        self.assertEqual(payload["manager"], {"child": None, "last_exit": None})
        [line] = self.audit_lines()
        self.assertIn(f"argv={json.dumps(self.base_argv() + ['status', '--json'], separators=(',', ':'))}", line)

    def test_cli_errors_come_back_as_422_with_the_cli_message(self):
        status, payload, _ = self.call("GET", self.path("status", run="r9"))
        self.assertEqual(status, 422)
        self.assertEqual(payload["returncode"], 1)
        self.assertTrue(payload["error"].startswith("factory: "), payload["error"])
        self.assertNotIn("status", payload)

    def test_the_cli_refuses_a_repo_that_is_not_configured_but_the_manager_never_asks(self):
        self.assertEqual(self.call("GET", self.path("status", repo="org/zzz"))[0], 404)
        self.assertEqual(self.call("POST", self.path("drain", repo="org/zzz"))[0], 404)

    def test_repos_lists_the_run_created_for_the_test(self):
        _, payload, _ = self.call("GET", "/repos", principal=VIEWER)
        entry = {row["repo"]: row for row in payload["repos"]}["org/name"]
        self.assertEqual(entry["runs"], [{"run": "r1", "planned": True, "started": True}])


class InProcessStartTests(ServerCase):
    """The functions the label trigger calls are the ones behind the HTTP actions: same checks, same registry."""

    def test_start_plan_spawns_the_same_argv_as_the_http_action_and_registers_the_child(self):
        reply = self.manager.start_plan(self.manager.target_for("org/name", "auto-a"), {"issues": [4, 2, 4]})
        self.assertEqual((reply.status, reply.payload["kind"]), (202, "plan"))
        self.assertEqual(self.spawner.argvs[-1], [*self.base_argv("auto-a"), "plan", "--issues=4,2"])
        child = self.manager.children.active_for_repo("org/name")
        self.assertEqual((child.kind, child.run), ("plan", "auto-a"))

    def test_one_process_per_repository_applies(self):
        self.manager.start_plan(self.manager.target_for("org/name", "auto-a"), {"issues": [1]})
        with self.assertRaises(mgr.ApiError) as caught:
            self.manager.start_plan(self.manager.target_for("org/name", "auto-b"), {"issues": [2]})
        self.assertEqual(caught.exception.status, 409)
        self.make_run("r1")
        with self.assertRaises(mgr.ApiError) as caught:
            self.manager.start_run(self.manager.target_for("org/name", "r1"))
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(len(self.spawner.calls), 1)
        self.manager.start_plan(self.manager.target_for("org/other", "auto-c"), {"issues": [3]})  # another repository is free
        self.assertEqual(len(self.spawner.calls), 2)

    def test_the_lease_is_probed_and_a_busy_one_refuses(self):
        self.serve(lock_factory=lambda cfg, paths: FakeLock(LockBusy("held")))
        with self.assertRaises(mgr.ApiError) as caught:
            self.manager.start_plan(self.manager.target_for("org/name", "auto-a"), {"issues": [1]})
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.spawner.calls, [])

    def test_bodies_are_validated_like_requests(self):
        target = self.manager.target_for("org/name", "auto-a")
        for body in ({"issues": []}, {"issues": [0]}, {"issues": [True]}, {"issues": [1], "limit": 3}, {"extra": 1}):
            with self.subTest(body), self.assertRaises(mgr.ApiError) as caught:
                self.manager.start_plan(target, body)
            self.assertEqual(caught.exception.status, 400)
        self.assertEqual(self.spawner.calls, [])

    def test_a_planned_run_is_not_planned_again_and_a_run_needs_a_plan(self):
        self.make_run("auto-a")
        with self.assertRaises(mgr.ApiError) as caught:
            self.manager.start_plan(self.manager.target_for("org/name", "auto-a"), {"issues": [1]})
        self.assertEqual(caught.exception.status, 409)
        with self.assertRaises(mgr.ApiError) as caught:
            self.manager.start_run(self.manager.target_for("org/name", "auto-none"))
        self.assertEqual(caught.exception.status, 404)

    def test_nothing_starts_during_shutdown(self):
        self.manager.closing.set()
        with self.assertRaises(mgr.ApiError) as caught:
            self.manager.start_plan(self.manager.target_for("org/name", "auto-a"), {"issues": [1]})
        self.assertEqual(caught.exception.status, 503)

    def test_retry_job_runs_the_same_short_command_as_the_http_action(self):
        target = self.manager.target_for("org/name", "r1")
        reply = self.manager.retry_job(target, "fix-a", "label-triggered retry")
        self.assertEqual(reply.status, 200)
        self.assertEqual(self.runner.argvs[-1], [*self.base_argv("r1"), "retry", "fix-a", "--reason=label-triggered retry"])
        self.runner.respond = lambda argv: CommandResult(1, b"", b"refused")
        self.assertEqual(self.manager.retry_job(target, "fix-a", "again").status, 422)
        with self.assertRaises(mgr.ApiError) as caught:
            self.manager.retry_job(target, "../etc", "x")
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(len(self.runner.calls), 2)

    def test_target_for_validates_the_repository_and_the_run(self):
        target = self.manager.target_for("org/name", "auto-a")
        self.assertEqual((target.repo, target.run, target.run_dir), ("org/name", "auto-a", self.run_dir("auto-a")))
        for repo, run in (("org/zzz", "r1"), ("not a repo", "r1"), ("org/name", "../x"), ("org/name", "")):
            with self.subTest(repo=repo, run=run), self.assertRaises(ValueError):
                self.manager.target_for(repo, run)


if __name__ == "__main__":
    unittest.main()

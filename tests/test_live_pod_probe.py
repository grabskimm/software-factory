"""tools/live_pod_probe.py: the probes that run inside a pod of the mirror (slice 21 of the pod backend).

The probe cannot be proven here: the question each mode asks (who may talk to the launcher's socket, what the Secret volume looks like
to uid 10002, what the API server answers) has an answer only in a real pod. What these tests prove is the PROBE: that its constants
are the contract's, that each attempt has the outcome word the runbook expects for a directory it can or cannot use, that nothing it
tries leaves a file behind, that it never prints a value, and that a refusal is told from a missing path.
"""

import contextlib
import io
import errno
import json
import os
import re
import socket
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from factory import podcontract as pc
from tests.live_tools import calls, imported_modules, load

probe = load("live_pod_probe")
ROOT_USER = os.geteuid() == 0
ROOT = Path(__file__).resolve().parent.parent


def run(*argv):
    lines = []
    code = probe.main(list(argv), emit=lines.append)
    return code, lines


class Constants(unittest.TestCase):
    def test_the_paths_and_names_are_the_contracts(self):
        self.assertEqual(probe.SOCKET, pc.LAUNCHER_SOCKET)
        self.assertEqual(probe.SOCKET_DIR, pc.LAUNCHER_SOCKET_DIR)
        self.assertEqual(probe.SPOOL, pc.SPOOL_DIR)
        self.assertEqual(probe.SECRET_DIR, pc.SESSION_SECRET_DIR)
        self.assertEqual(set(probe.SECRET_FILES), {pc.SECRET_KEY_TOKEN, pc.SECRET_KEY_ANTHROPIC})
        self.assertEqual(probe.WORKER_NS, pc.WORKER_NAMESPACE)

    def test_the_probe_is_self_contained_because_it_is_fed_to_an_interpreter_on_stdin(self):
        self.assertFalse({m for m in imported_modules("live_pod_probe") if m in ("factory", "live_recording", "live_checks")})


class Words(unittest.TestCase):
    def test_a_refusal_a_missing_path_and_a_dead_listener_are_different_words(self):
        for number, expected in ((errno.EACCES, "refused"), (errno.EPERM, "refused"), (errno.EROFS, "refused"), (errno.ENOENT, "absent"), (errno.ECONNREFUSED, "no listener"), (errno.EIO, "error")):
            self.assertEqual(probe.word(OSError(number, "x")), expected)

    def test_attempt_says_allowed_only_for_a_call_that_returned(self):
        self.assertEqual(probe.attempt(lambda: None), "allowed")
        self.assertEqual(probe.attempt(lambda: (_ for _ in ()).throw(PermissionError(errno.EACCES, "x"))), "refused")


@unittest.skipIf(ROOT_USER, "root passes every permission check, so the refusals cannot be shown")
class Directories(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)

    def locked(self):
        os.chmod(self.path, 0o555)
        self.addCleanup(os.chmod, self.path, 0o755)

    def test_a_writable_directory_allows_create_bind_and_write_access_and_leaves_nothing_behind(self):
        self.assertEqual(probe.create_in(str(self.path)), "allowed")
        self.assertEqual(probe.bind_in(str(self.path)), "allowed")
        self.assertEqual(probe.write_access(str(self.path)), "allowed")
        self.assertEqual(list(self.path.iterdir()), [])

    def test_a_directory_without_write_permission_refuses_each(self):
        self.locked()
        self.assertEqual(probe.create_in(str(self.path)), "refused")
        self.assertEqual(probe.bind_in(str(self.path)), "refused")
        self.assertEqual(probe.write_access(str(self.path)), "refused")
        self.assertEqual(list(self.path.iterdir()), [])

    def test_a_missing_directory_is_absent_and_never_allowed(self):
        gone = str(self.path / "nope")
        self.assertEqual((probe.create_in(gone), probe.bind_in(gone), probe.write_access(gone)), ("absent", "absent", "absent"))

    def test_rename_and_unlink_are_not_tried_on_a_missing_file_because_that_proves_nothing(self):
        text = Path(probe.__file__).read_text()
        self.assertNotIn("os.rename", text)
        self.assertNotIn("os.unlink(os.path.join(directory", text)
        self.locked()
        with self.assertRaises(OSError) as caught:
            os.rename(self.path / "missing", self.path / "missing2")
        self.assertEqual(caught.exception.errno, errno.ENOENT)  # the reason: a refusal and a missing file look alike
        self.assertEqual(probe.write_access(str(self.path)), "refused")

    def listening(self):
        path = self.path / "launcher.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        server.listen(1)
        self.addCleanup(server.close)
        return str(path)

    def test_connect_to_a_listener_is_allowed_and_to_nothing_is_absent(self):
        path = self.listening()
        self.assertEqual(probe.connect(path), "allowed")
        self.assertEqual(probe.connect(str(self.path / "other.sock")), "absent")

    def test_connect_to_a_socket_the_user_may_not_write_is_refused(self):
        path = self.listening()
        os.chmod(path, 0o000)
        self.assertEqual(probe.connect(path), "refused")

    def patched(self, **names):
        stack = contextlib.ExitStack()
        for name, value in names.items():
            stack.enter_context(mock.patch.object(probe, name, value))
        self.addCleanup(stack.close)

    def test_socket_mode_for_a_worker_wants_every_answer_refused(self):
        sock = self.listening()
        os.chmod(sock, 0o000)
        self.locked()
        self.patched(SOCKET=sock, SOCKET_DIR=str(self.path))
        code, lines = run("socket", "--expect", "worker")
        self.assertEqual(code, 0, lines)
        self.assertEqual(len(lines), 4)
        self.assertTrue(all("(expected refused)" in line and not line.startswith("FAIL") for line in lines))

    def test_socket_mode_for_a_worker_fails_when_the_socket_can_be_connected(self):
        sock = self.listening()
        self.locked()
        self.patched(SOCKET=sock, SOCKET_DIR=str(self.path))
        code, lines = run("socket", "--expect", "worker")
        self.assertEqual(code, 1)
        self.assertIn("FAIL connect to the launcher socket: allowed (expected refused)", lines)

    def test_socket_mode_for_the_supervisor_wants_connect_allowed_and_the_directory_closed(self):
        sock = self.listening()
        self.locked()
        self.patched(SOCKET=sock, SOCKET_DIR=str(self.path))
        code, lines = run("socket", "--expect", "supervisor")
        self.assertEqual(code, 0, lines)
        self.assertIn("connect to the launcher socket: allowed (expected allowed)", lines)
        os.chmod(self.path, 0o755)  # a supervisor that can write the directory is a failure
        code, lines = run("socket", "--expect", "supervisor")
        self.assertEqual(code, 1)
        self.assertIn("FAIL create a file in the socket directory: allowed (expected refused)", lines)

    def test_spool_mode(self):
        self.patched(SPOOL=str(self.path))
        self.assertEqual(run("spool", "--expect", "supervisor")[0], 0)
        self.assertEqual(run("spool", "--expect", "worker")[0], 1)
        self.locked()
        self.assertEqual(run("spool", "--expect", "supervisor")[0], 1)
        os.chmod(self.path, 0o000)
        self.addCleanup(os.chmod, self.path, 0o755)
        code, lines = run("spool", "--expect", "worker")
        self.assertEqual(code, 0, lines)
        self.assertEqual(lines, ["list the spool: refused (expected refused)", "create a file in the spool: refused (expected refused)"])

    def test_secret_volume_reports_mode_and_owner_without_reading_and_fails_on_a_loose_mode(self):
        for name in probe.SECRET_FILES:
            file = self.path / name
            file.write_text("TOPSECRETVALUE")
            os.chmod(file, 0o400)
        self.patched(SECRET_DIR=str(self.path))
        code, lines = run("secret-volume")
        self.assertEqual(code, 0, lines)
        uid, gid = os.getuid(), os.getgid()
        self.assertEqual(lines[0], f"secret file token: regular yes, mode 0400, owner {uid}, group {gid}")
        self.assertNotIn("TOPSECRETVALUE", "\n".join(lines))
        os.chmod(self.path / "token", 0o440)
        code, lines = run("secret-volume")
        self.assertEqual(code, 1)
        self.assertIn("FAIL secret file token: it must be a regular file with no group or other permission", lines)

    def test_secret_volume_through_a_link_is_judged_by_its_target_as_a_kubelet_volume_is(self):
        target = self.path / "target"
        target.write_text("x")
        os.chmod(target, 0o400)
        for name in probe.SECRET_FILES:
            os.symlink(target, self.path / name)
        self.patched(SECRET_DIR=str(self.path))
        self.assertEqual(run("secret-volume")[0], 0)

    def test_secret_volume_missing_files_fail(self):
        self.patched(SECRET_DIR=str(self.path))
        code, lines = run("secret-volume")
        self.assertEqual(code, 1)
        self.assertEqual(lines, ["FAIL secret file token: absent", "FAIL secret file anthropic-api-key: absent"])


class SecretVolumeAsTheWorker(unittest.TestCase):
    """The branch of `secret-volume` that needs root (`--as-uid`) is run here with the identity switch stubbed, so that it is judged and not skipped."""

    SENTINEL = "TOPSECRETVALUE-sentinel"

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        for name in probe.SECRET_FILES:
            (self.path / name).write_text(self.SENTINEL)
            os.chmod(self.path / name, 0o400)
        for patch in (
            mock.patch.object(probe, "SECRET_DIR", str(self.path)),
            mock.patch.object(probe, "drop_identity", lambda uid, gid: self.switched.append((uid, gid))),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.switched = []

    def opened_files(self, refuse):
        """Run the mode while recording every os.open of a secret file; `refuse` makes the kernel say EACCES to the switched identity."""
        opened = []
        real = os.open

        def fake_open(path, flags, *rest, **named):
            if str(path).startswith(str(self.path)) and self.switched:
                opened.append((os.path.basename(path), flags))
                if refuse:
                    raise PermissionError(errno.EACCES, "refused")
            return real(path, flags, *rest, **named)

        with mock.patch.object(probe.os, "open", fake_open):
            code, lines = run("secret-volume", "--as-uid", "10002", "--as-gid", "10010")
        return code, lines, opened

    def test_the_identity_is_switched_before_the_files_are_opened_and_a_refusal_passes(self):
        code, lines, opened = self.opened_files(refuse=True)
        self.assertEqual(self.switched, [(10002, 10010)])
        self.assertEqual(code, 0, lines)
        self.assertEqual([name for name, _ in opened], list(probe.SECRET_FILES))
        self.assertIn("open secret file token as uid 10002: refused (expected refused)", lines)

    def test_a_file_the_worker_can_open_is_a_failure(self):
        code, lines, _ = self.opened_files(refuse=False)
        self.assertEqual(code, 1)
        self.assertIn("FAIL open secret file anthropic-api-key as uid 10002: allowed (expected refused)", lines)

    def test_the_files_are_opened_read_only_and_closed_and_their_content_is_never_read_or_printed(self):
        for refuse in (False, True):
            reads = []
            real_read = os.read
            with mock.patch.object(probe.os, "read", lambda fd, n: reads.append(n) or real_read(fd, n)):
                code, lines, opened = self.opened_files(refuse)
            self.assertEqual(reads, [])
            self.assertTrue(all(flags == os.O_RDONLY for _, flags in opened), opened)
            self.assertNotIn(self.SENTINEL, "\n".join(lines))
            self.assertNotIn("TOPSECRET", "\n".join(lines))

    def test_no_code_of_the_mode_reads_anything_not_even_in_a_nested_function(self):
        def names(code):
            found = set(code.co_names)
            for constant in code.co_consts:
                if hasattr(constant, "co_names"):
                    found |= names(constant)
            return found

        found = names(probe.mode_secret_volume.__code__)
        for forbidden in ("read", "readline", "readlines", "fdopen", "print", "pread", "sendfile", "mmap", "copyfile"):
            self.assertNotIn(forbidden, found)
        self.assertEqual(sorted(n for n in found if n in ("open", "close")), ["close", "open"])


class Switching(unittest.TestCase):
    def test_a_switch_without_privilege_is_an_environment_error_not_a_pass(self):
        if ROOT_USER:
            self.skipTest("root can switch")
        code, lines = run("socket", "--expect", "worker", "--as-uid", "10002", "--as-gid", "10010")
        self.assertEqual(code, 2)
        self.assertEqual(lines, ["FAIL cannot switch to the requested identity (this needs root or CAP_SETUID)"])

    def test_the_identity_is_dropped_in_the_order_that_works(self):
        order = []
        with mock.patch.object(probe.os, "setgroups", lambda g: order.append(("groups", g))), mock.patch.object(probe.os, "setgid", lambda g: order.append(("gid", g))), mock.patch.object(
            probe.os, "setuid", lambda u: order.append(("uid", u))
        ), mock.patch.object(probe.os, "getuid", lambda: 10002), mock.patch.object(probe.os, "geteuid", lambda: 10002):
            probe.drop_identity(10002, 10010)
        self.assertEqual(order, [("groups", []), ("gid", 10010), ("uid", 10002)])  # the uid last: afterwards nothing can be changed

    def test_a_drop_that_did_not_take_effect_is_an_error(self):
        with mock.patch.object(probe.os, "setgroups"), mock.patch.object(probe.os, "setgid"), mock.patch.object(probe.os, "setuid"), mock.patch.object(probe.os, "getuid", lambda: 0), mock.patch.object(
            probe.os, "geteuid", lambda: 0
        ):
            with self.assertRaises(PermissionError):
                probe.drop_identity(10002, 10010)

    def test_uid_and_gid_go_together(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            run("socket", "--expect", "worker", "--as-uid", "1")

    def test_socket_and_spool_need_an_expectation(self):
        for mode in ("socket", "spool"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                run(mode)


class Environment(unittest.TestCase):
    def run_env(self, env, *argv):
        with mock.patch.dict(os.environ, env, clear=True):
            return run("environment", *argv)

    def test_it_prints_names_and_counts_never_a_value(self):
        env = {"KUBERNETES_SERVICE_HOST": "10.0.0.1", "KUBERNETES_SERVICE_PORT": "443", "FACTORY_SESSION_ID": "abcSECRETVALUE"}
        code, lines = self.run_env(env)
        self.assertEqual(code, 0)
        self.assertEqual(lines[0], "environment: 3 names, KUBERNETES_SERVICE_HOST present, KUBERNETES_SERVICE_PORT present")
        self.assertEqual(lines[1], "environment: 0 names start with AZURE_, 0 names contain KEY, TOKEN, SECRET or PASSWORD")
        self.assertNotIn("10.0.0.1", "\n".join(lines))
        self.assertNotIn("SECRETVALUE", "\n".join(lines))

    def test_a_worker_pod_may_have_no_azure_name_and_no_secret_like_name_and_needs_the_api_address(self):
        good = {"KUBERNETES_SERVICE_HOST": "h", "KUBERNETES_SERVICE_PORT": "443"}
        self.assertEqual(self.run_env(good, "--expect", "worker-pod")[0], 0)
        for extra, message in (
            ({"AZURE_CLIENT_ID": "x"}, "FAIL environment: an AZURE_ name is present"),
            ({"ANTHROPIC_API_KEY": "x"}, "FAIL environment: a secret-like name is present"),
            ({"MY_TOKEN": "x"}, "FAIL environment: a secret-like name is present"),
            ({"MY_PASSWORD": "x"}, "FAIL environment: a secret-like name is present"),
        ):
            code, lines = self.run_env({**good, **extra}, "--expect", "worker-pod")
            with self.subTest(extra):
                self.assertEqual(code, 1)
                self.assertIn(message, lines)
        code, lines = self.run_env({}, "--expect", "worker-pod")
        self.assertIn("FAIL environment: the API server address is not in the environment", lines)

    def test_the_images_own_secret_like_name_is_not_counted_but_every_other_one_is(self):
        # Found by the first live run (docs/pod-backend-local-e2e.md): the Dockerfile sets FACTORY_TOKEN_BROKER, so every pod of the image has it.
        good = {"KUBERNETES_SERVICE_HOST": "h", "KUBERNETES_SERVICE_PORT": "443", "FACTORY_TOKEN_BROKER": "/opt/token-broker/bin/python x"}
        code, lines = self.run_env(good, "--expect", "worker-pod")
        self.assertEqual(code, 0)
        self.assertIn("environment: 0 names start with AZURE_, 0 names contain KEY, TOKEN, SECRET or PASSWORD", lines)
        self.assertTrue(any("image-defined name not counted (FACTORY_TOKEN_BROKER" in line for line in lines))
        self.assertNotIn("/opt/token-broker", "\n".join(lines))
        code, lines = self.run_env({**good, "OTHER_TOKEN": "x"}, "--expect", "worker-pod")
        self.assertEqual(code, 1)
        self.assertIn("FAIL environment: a secret-like name is present", lines)

    def test_the_dockerfile_sets_no_secret_like_name_that_the_probe_does_not_know(self):
        text = (ROOT / "Dockerfile").read_text()
        names = set(re.findall(r"^(?:ENV\s+|\s+)([A-Z][A-Z0-9_]*)=", text, flags=re.MULTILINE))
        secret_like = {name for name in names if any(part in name for part in probe.SECRET_LIKE)}
        self.assertTrue(secret_like <= set(probe.IMAGE_ENV_NAMES), secret_like - set(probe.IMAGE_ENV_NAMES))
        self.assertIn("FACTORY_TOKEN_BROKER", names)

    def test_the_allow_list_of_the_contract_has_no_secret_like_name(self):
        for name in pc.SHARED["env"]["pod"]:
            self.assertFalse(any(part in name for part in probe.SECRET_LIKE), name)
            self.assertFalse(name.startswith("AZURE_"), name)


class Network(unittest.TestCase):
    def setUp(self):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(4)
        self.addCleanup(self.listener.close)
        self.port = self.listener.getsockname()[1]
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        self.closed_port = closed.getsockname()[1]
        closed.close()

    def patched(self, status=None, forbidden=None, dns=True):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(probe, "FORBIDDEN", forbidden if forbidden is not None else (("a forbidden place", "127.0.0.1", self.closed_port),)))
        stack.enter_context(mock.patch.object(probe, "api_answer", lambda host, port: status))
        stack.enter_context(mock.patch.object(probe.socket, "getaddrinfo", (lambda *a, **k: [1]) if dns else mock.Mock(side_effect=OSError)))
        stack.enter_context(mock.patch.object(probe, "reachable", lambda host, port: "connected" if port == 443 or port == self.port else "blocked"))
        stack.enter_context(mock.patch.dict(os.environ, {"KUBERNETES_SERVICE_HOST": "10.0.0.1", "KUBERNETES_SERVICE_PORT": "443", "FACTORY_NODE_IP": "10.0.0.9"}))

    def test_the_forbidden_list_is_the_contracts(self):
        contract = pc.SHARED["canary"]["forbidden"]
        ours = [(h[1:] if h.startswith("$") else h, p) for _, h, p in probe.FORBIDDEN]
        expected = [(entry.get("host_env") or entry["host"], entry["port"]) for entry in contract]
        self.assertEqual(ours, expected)
        self.assertEqual(probe.API_PATH, pc.SHARED["canary"]["api_probe_path"])
        self.assertEqual(probe.DENIED_STATUSES, tuple(pc.SHARED["canary"]["api_probe_denied_statuses"]))

    def test_a_fence_that_holds_passes(self):
        self.patched(status=403)
        code, lines = run("network", "--control-host", "control.invalid", "--api-public-host", "api.invalid")
        self.assertEqual(code, 0, lines)
        self.assertIn("connect to a forbidden place, port %d: blocked (expected blocked)" % self.closed_port, lines)
        self.assertIn("unauthenticated request to the API server, public address: http 403 (expected no connection, 401 or 403)", lines)
        self.assertIn("connect to the control host, port 443: connected (expected connected)", lines)

    def test_a_forbidden_destination_that_connects_fails(self):
        self.patched(status=401, forbidden=(("a forbidden place", "127.0.0.1", self.port),))
        code, lines = run("network", "--control-host", "c", "--api-public-host", "a")
        self.assertEqual(code, 1)
        self.assertIn("FAIL connect to a forbidden place, port %d: connected (expected blocked)" % self.port, lines)

    def test_an_api_server_that_answers_anything_but_401_or_403_fails(self):
        for status in (200, 404, 500):
            self.patched(status=status)
            code, lines = run("network", "--control-host", "c", "--api-public-host", "a")
            with self.subTest(status):
                self.assertEqual(code, 1)
                self.assertTrue(any(line.startswith("FAIL unauthenticated request") for line in lines))

    def test_no_connection_to_the_api_server_passes_and_a_dead_network_is_caught_by_the_control(self):
        self.patched(status=None)
        self.assertEqual(run("network", "--control-host", "c", "--api-public-host", "a")[0], 0)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(probe, "reachable", lambda host, port: "blocked"))
        code, lines = run("network", "--control-host", "c", "--api-public-host", "a")
        self.assertEqual(code, 1)
        self.assertIn("FAIL connect to the control host, port 443: blocked (expected connected)", lines)

    def test_failed_dns_and_a_missing_address_are_failures_and_no_address_is_printed(self):
        self.patched(status=403, dns=False)
        code, lines = run("network", "--control-host", "c", "--api-public-host", "a")
        self.assertEqual(code, 1)
        self.assertIn("FAIL resolve kubernetes.default.svc: failed (expected resolved)", lines)
        self.patched(status=403, forbidden=(("the node's kubelet port", "$NOT_SET_ANYWHERE", 10250),))
        code, lines = run("network", "--control-host", "c", "--api-public-host", "a")
        self.assertIn("FAIL the node's kubelet port: the pod has no address for it (NOT_SET_ANYWHERE is not set)", lines)
        for line in lines:
            self.assertNotIn("10.0.0", line)

    def test_the_real_reachable_tells_a_listener_from_a_closed_port(self):
        self.assertEqual(probe.reachable("127.0.0.1", self.port), "connected")
        self.assertEqual(probe.reachable("127.0.0.1", self.closed_port), "blocked")

    def test_both_hosts_are_required(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            run("network", "--control-host", "c")


class Statuses(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        token = Path(self.directory.name, "token")
        token.write_text("REALTOKENVALUE\n")
        Path(self.directory.name, "ca.crt").write_text("x")
        self.requests = []
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.dict(os.environ, {"KUBERNETES_SERVICE_HOST": "10.0.0.1", "KUBERNETES_SERVICE_PORT": "443"}))
        stack.enter_context(mock.patch.object(probe, "SERVICE_DIR", self.directory.name))
        stack.enter_context(mock.patch.object(probe.ssl, "create_default_context", lambda cafile=None: object()))
        stack.enter_context(mock.patch.object(probe, "call", self.fake_call))

    def fake_call(self, context, host, port, method, path, token, payload=None):
        self.requests.append((method, path, token, payload))
        answers = {"/pods/factory-w-probe-missing": 404, "/secrets": 403, "/pods": 401, "/pods?dryRun=All": 422}
        status = answers[path.split(f"/namespaces/{pc.WORKER_NAMESPACE}")[1]]
        return status, {"kind": "Status", "code": status, "message": "a message"}

    def test_the_four_cases_are_asked_and_only_the_codes_are_printed(self):
        code, lines = run("statuses")
        self.assertEqual(code, 0)
        self.assertEqual(lines, ["case not_found: http 404", "case forbidden: http 403", "case unauthorized: http 401", "case denied: http 422"])
        self.assertNotIn("REALTOKENVALUE", "\n".join(lines))

    def test_only_reads_and_one_dry_run_are_made_and_the_unauthorized_case_never_sends_the_real_token(self):
        run("statuses")
        self.assertEqual([r[0] for r in self.requests], ["GET", "GET", "GET", "POST"])
        post = self.requests[3]
        self.assertTrue(post[1].endswith("?dryRun=All"))
        self.assertEqual(self.requests[2][2], "invalid")
        self.assertEqual([r[2] for r in self.requests if r[2] != "invalid"], ["REALTOKENVALUE"] * 3)
        self.assertEqual(post[3]["metadata"]["namespace"], pc.WORKER_NAMESPACE)
        self.assertEqual(post[3]["kind"], "Pod")

    def test_the_bodies_are_printed_only_when_asked_and_then_only_as_one_json_document(self):
        code, lines = run("statuses", "--bodies")
        self.assertEqual(code, 0)
        self.assertEqual(len(lines), 1)
        document = json.loads(lines[0])
        self.assertEqual([c["http"] for c in document["cases"]], [404, 403, 401, 422])
        self.assertNotIn("REALTOKENVALUE", lines[0])

    def test_outside_a_pod_or_without_the_token_it_is_an_environment_error(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(run("statuses"), (2, ["FAIL statuses: this is not a pod with an API server address"]))
        os.remove(Path(self.directory.name, "token"))
        self.assertEqual(run("statuses"), (2, ["FAIL statuses: the service account token or the CA file is not readable"]))

    def test_a_connection_failure_is_a_failure_not_a_pass(self):
        with mock.patch.object(probe, "call", side_effect=OSError):
            self.assertEqual(run("statuses"), (1, ["FAIL case not_found: no answer"]))

    def test_the_token_file_must_be_a_regular_file_and_is_bounded(self):
        fifo = Path(self.directory.name, "fifo")
        os.mkfifo(fifo)
        with self.assertRaises(OSError):
            probe.read_small(str(fifo))
        big = Path(self.directory.name, "big")
        big.write_text("a" * (probe.MAX_TOKEN_BYTES * 2))
        self.assertEqual(len(probe.read_small(str(big))), probe.MAX_TOKEN_BYTES)


class Safety(unittest.TestCase):
    def test_it_runs_no_program_and_uses_no_shell(self):
        self.assertFalse(imported_modules("live_pod_probe") & {"subprocess", "pty", "shlex", "requests", "urllib"})
        self.assertFalse(calls("live_pod_probe") & {"os.system", "os.popen", "os.exec", "os.execv", "os.execvp", "eval", "exec", "__import__", "os.chmod", "os.chown", "shutil.rmtree"})

    def test_it_never_reads_the_content_of_a_secret_file(self):
        text = Path(probe.__file__).read_text()
        self.assertEqual(text.count("os.read("), 1)  # the token of `statuses` only
        self.assertIn("def read_small", text)
        mode = probe.mode_secret_volume
        self.assertNotIn("os.read", mode.__code__.co_names)
        self.assertNotIn("read", mode.__code__.co_names)

    def test_it_writes_nothing_but_a_probe_file_it_removes_and_a_dry_run(self):
        text = Path(probe.__file__).read_text()
        self.assertEqual(text.count("os.O_CREAT"), 1)
        self.assertNotIn('"w"', text)
        self.assertIn("dryRun=All", text)
        self.assertEqual(sum(1 for line in text.splitlines() if '"POST"' in line), 1)


if __name__ == "__main__":
    unittest.main()

"""End-to-end tests for tools/token_broker.py against a fake GitHub API on 127.0.0.1.

The broker runs as a real subprocess, exactly as the supervisor will run it. The
tests need ``cryptography`` (only the broker may use it) and skip cleanly
where it is missing. Every key and token here is a throwaway fake.
"""

import base64
import contextlib
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

BROKER = Path(__file__).resolve().parents[1] / "tools" / "token_broker.py"
HAVE_CRYPTO = importlib.util.find_spec("cryptography") is not None
FAKE_TOKEN = "ghs_FAKE_installation_token_0123456789"
PERMISSIONS = {
    "actions": "read",
    "contents": "write",
    "pull_requests": "write",
    "issues": "write",
    "statuses": "write",
    "checks": "read",
    "metadata": "read",
}


# Access levels, weakest first, as GitHub orders them: a "write" grant covers a "read" request.
LEVELS = {"read": 1, "write": 2, "admin": 3}
# GitHub's answer to a token request that asks for more than the installation has accepted.
NOT_GRANTED = json.dumps(
    {
        "message": "The permissions requested are not granted to this installation.",
        "documentation_url": "https://docs.github.com/rest/apps/apps#create-an-installation-access-token-for-an-app",
    }
).encode()

_BROKER_MODULE = []


def load_broker():
    """Import the script as a module (it only needs the stdlib at import time)."""
    if not _BROKER_MODULE:
        spec = importlib.util.spec_from_file_location("token_broker_under_test", BROKER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _BROKER_MODULE.append(module)
    return _BROKER_MODULE[0]


def b64url_decode(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class FakeGitHub(ThreadingHTTPServer):
    """Records every request and answers with whatever the test configured."""

    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), self.Handler)
        self.requests = []
        self.status = 201
        self.body = json.dumps({"token": FAKE_TOKEN, "expires_at": "2030-01-01T00:00:00Z"}).encode()
        self.location = None
        self.on_request = None
        self.drop = False
        # What the installation has accepted, as GET /app/installations/<id> reports it. While it
        # is not None, a token request that asks for more is answered 422 like the real API does.
        self.granted = None
        self.installation_status = 200
        self.installation_body = None  # raw bytes that replace the generated installation document
        self.installation_location = None
        self.installation_drop = False
        threading.Thread(target=self.serve_forever, args=(0.01,), daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self.server.requests.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": self.rfile.read(length),
                }
            )
            if self.server.on_request:
                self.server.on_request()
            if self.server.drop:
                self.close_connection = True
                return  # hang up without answering
            status, body = self.server.status, self.server.body
            granted = self.server.granted
            if granted is not None:
                asked = json.loads(self.server.requests[-1]["body"])["permissions"]
                if any(LEVELS.get(granted.get(name), 0) < LEVELS[level] for name, level in asked.items()):
                    status, body = 422, NOT_GRANTED
            self.reply(status, body, self.server.location)

        def do_GET(self):
            self.server.requests.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": b"",
                }
            )
            if self.server.installation_drop:
                self.close_connection = True
                return
            body = self.server.installation_body
            if body is None:
                document = {"id": 678, "app_slug": "factory"}
                if self.server.granted is not None:
                    document["permissions"] = self.server.granted
                body = json.dumps(document).encode()
            self.reply(self.server.installation_status, body, self.server.installation_location)

        def reply(self, status, body, location=None):
            self.send_response(status)
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


@unittest.skipUnless(HAVE_CRYPTO, "cryptography is not installed (broker venv only)")
class BrokerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.pem = cls.private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ).decode()
        cls.public_key = cls.private_key.public_key()
        # Pieces of the key that must never be echoed anywhere.
        cls.key_lines = [line for line in cls.pem.splitlines() if len(line) > 20]

    def setUp(self):
        self.server = FakeGitHub()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.workdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.workdir.cleanup)

    def run_broker(self, *extra, key="default", base=None, env=None, args=None):
        """Run the broker as a subprocess with a minimal environment."""
        cmd = [sys.executable, str(BROKER)]
        if args is None:
            args = [
                "--app-id", "12345",
                "--installation-id", "678",
                "--repo", "acme/widgets",
                "--api-url", base or self.server.url,
            ]  # fmt: skip
        environment = {"PATH": os.environ.get("PATH", ""), "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
        if key == "default":
            key = self.pem
        if key is not None:
            environment["GITHUB_APP_PRIVATE_KEY"] = key
        environment.update(env or {})
        return subprocess.run(
            [*cmd, *args, *extra],
            env=environment,
            cwd=self.workdir.name,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def call(self, *extra, key="default", base=None, args=None):
        """Same as run_broker but in-process: failure matrices need not pay a startup each."""
        if args is None:
            args = ["--app-id", "12345", "--installation-id", "678", "--repo", "acme/widgets",
                    "--api-url", base or self.server.url]  # fmt: skip
        key = self.pem if key == "default" else key
        out, err = io.StringIO(), io.StringIO()
        broker = load_broker()
        with mock.patch.dict(os.environ, {"NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}):
            os.environ.pop("GITHUB_APP_PRIVATE_KEY", None)
            if key is not None:
                os.environ["GITHUB_APP_PRIVATE_KEY"] = key
            with mock.patch.object(broker, "_no_core_dumps"):
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = broker.main([*args, *extra])
        return SimpleNamespace(returncode=code, stdout=out.getvalue(), stderr=err.getvalue())

    def assert_clean_failure(self, done, *forbidden):
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertEqual(done.stdout, "")
        self.assertTrue(done.stderr.startswith("token_broker: "), done.stderr)
        self.assertEqual(done.stderr.count("\n"), 1, "one short line expected")
        for secret in (FAKE_TOKEN, self.pem, "PRIVATE KEY", *self.key_lines, *forbidden):
            self.assertNotIn(secret, done.stderr)
        self.assertNotIn("Traceback", done.stderr)

    # -- happy path ---------------------------------------------------------

    def test_permissions_are_least_privilege(self):
        """issues is write only for the factory:* status labels; never workflows or admin."""
        self.assertEqual(load_broker().PERMISSIONS, PERMISSIONS)
        self.assertEqual(PERMISSIONS["issues"], "write")
        for forbidden in ("workflows", "administration", "actions_write", "secrets", "members"):
            self.assertNotIn(forbidden, PERMISSIONS)
        self.assertEqual(PERMISSIONS["actions"], "read")
        self.assertEqual(PERMISSIONS["checks"], "read")

    def test_success_prints_one_json_line_and_sends_the_exact_request(self):
        before = int(time.time())
        done = self.run_broker()
        after = int(time.time())
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stderr, "")
        self.assertEqual(done.stdout.count("\n"), 1)
        self.assertEqual(json.loads(done.stdout), {"token": FAKE_TOKEN, "expires_at": "2030-01-01T00:00:00Z"})

        (request,) = self.server.requests
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["path"], "/app/installations/678/access_tokens")
        self.assertEqual(json.loads(request["body"]), {"repositories": ["widgets"], "permissions": PERMISSIONS})
        headers = request["headers"]
        self.assertEqual(headers["accept"], "application/vnd.github+json")
        self.assertEqual(headers["x-github-api-version"], "2022-11-28")
        self.assertEqual(headers["user-agent"], "factory-token-broker")
        self.assertEqual(headers["content-type"], "application/json")
        scheme, _, jwt = headers["authorization"].partition(" ")
        self.assertEqual(scheme, "Bearer")

        header_part, payload_part, signature_part = jwt.split(".")
        for part in (header_part, payload_part, signature_part):
            self.assertNotIn("=", part, "base64url must be unpadded")
        self.assertEqual(json.loads(b64url_decode(header_part)), {"alg": "RS256", "typ": "JWT"})
        claims = json.loads(b64url_decode(payload_part))
        self.assertEqual(sorted(claims), ["exp", "iat", "iss"])
        self.assertEqual(str(claims["iss"]), "12345")
        self.assertTrue(before - 60 <= claims["iat"] <= after - 60, claims)
        self.assertEqual(claims["exp"] - claims["iat"], 600)

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        # Raises InvalidSignature if the broker signed anything else or used another key.
        self.public_key.verify(
            b64url_decode(signature_part),
            f"{header_part}.{payload_part}".encode(),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )

    def test_only_token_and_expiry_are_relayed_from_the_response(self):
        self.server.body = json.dumps(
            {
                "token": FAKE_TOKEN,
                "expires_at": "2030-01-01T00:00:00Z",
                "permissions": PERMISSIONS,
                "repositories": [{"name": "widgets"}],
                "something_else": "ignored",
            }
        ).encode()
        done = self.call()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(sorted(json.loads(done.stdout)), ["expires_at", "token"])

    def test_repo_names_with_dots_and_underscores_and_a_trailing_slash_on_the_api_url(self):
        done = self.call(args=[
            "--app-id", "1", "--installation-id", "2", "--repo", "My-Org/my.repo_name-2",
            "--api-url", self.server.url + "/",
        ])  # fmt: skip
        self.assertEqual(done.returncode, 0, done.stderr)
        (request,) = self.server.requests
        self.assertEqual(request["path"], "/app/installations/2/access_tokens")
        self.assertEqual(json.loads(request["body"])["repositories"], ["my.repo_name-2"])

    def test_a_pem_flattened_onto_one_line_with_literal_backslash_n_still_works(self):
        done = self.call(key=self.pem.replace("\n", "\\n"))
        self.assertEqual(done.returncode, 0, done.stderr)

    # -- secrets stay out of argv, files and output --------------------------

    @unittest.skipUnless(os.path.isdir("/proc/self"), "needs /proc")
    def test_the_key_is_in_the_environment_only_never_in_argv(self):
        seen = {}
        pid_ready = threading.Event()
        holder = {}

        def peek():
            pid_ready.wait(10)
            base = Path("/proc") / str(holder["pid"])
            seen["cmdline"] = (base / "cmdline").read_bytes().decode(errors="replace")
            seen["environ"] = (base / "environ").read_bytes().decode(errors="replace")

        self.server.on_request = peek
        process = subprocess.Popen(
            [sys.executable, str(BROKER), "--app-id", "1", "--installation-id", "2",
             "--repo", "acme/widgets", "--api-url", self.server.url],
            env={"PATH": os.environ.get("PATH", ""), "NO_PROXY": "127.0.0.1", "GITHUB_APP_PRIVATE_KEY": self.pem},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=self.workdir.name,
        )  # fmt: skip
        holder["pid"] = process.pid
        pid_ready.set()
        process.communicate(timeout=60)
        self.assertEqual(process.returncode, 0)
        self.assertIn("acme/widgets", seen["cmdline"])
        for line in self.key_lines:
            self.assertNotIn(line, seen["cmdline"])
        self.assertNotIn("PRIVATE KEY", seen["cmdline"])
        self.assertIn("GITHUB_APP_PRIVATE_KEY=", seen["environ"])  # the one intended channel

    def test_a_key_passed_on_the_command_line_is_rejected_and_never_echoed(self):
        done = self.run_broker("--private-key", self.pem)
        self.assert_clean_failure(done)
        done = self.run_broker(self.pem.replace("\n", " "))
        self.assert_clean_failure(done)
        self.assertEqual(self.server.requests, [])

    def test_nothing_is_written_to_disk(self):
        env = {"TMPDIR": self.workdir.name, "HOME": self.workdir.name}
        done = self.run_broker(env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(os.listdir(self.workdir.name), [])

    def test_help_documents_no_key_option(self):
        done = self.run_broker("--help", key=None, args=[])
        self.assertEqual(done.returncode, 0)
        self.assertNotIn("--private-key", done.stdout)
        self.assertIn("GITHUB_APP_PRIVATE_KEY", done.stdout)

    # -- failures: nothing sensitive in stderr, nothing on stdout -----------

    def test_http_errors_report_the_status_but_never_the_body(self):
        # A hostile or buggy server that echoes the token and the JWT back in an error body.
        for status in (401, 403, 404, 422, 500, 502):
            with self.subTest(status=status):
                self.server.status = status
                self.server.body = json.dumps(
                    {"message": "boom " + FAKE_TOKEN, "token": FAKE_TOKEN, "pem": self.pem}
                ).encode()
                done = self.call()
                self.assert_clean_failure(done, "boom")
                self.assertIn(f"HTTP {status}", done.stderr)

    def test_a_failure_through_a_real_subprocess_prints_one_clean_line(self):
        self.server.status = 401
        self.server.body = json.dumps({"message": FAKE_TOKEN, "pem": self.pem}).encode()
        done = self.run_broker()
        self.assert_clean_failure(done)
        self.assertIn("HTTP 401", done.stderr)

    def test_unusable_success_bodies_fail_without_echoing_them(self):
        bodies = {
            "invalid json": b'{"token": "%s", "expires_at": ' % FAKE_TOKEN.encode(),
            "not an object": json.dumps([FAKE_TOKEN]).encode(),
            "missing token": json.dumps({"tokenn": FAKE_TOKEN, "expires_at": "2030-01-01T00:00:00Z"}).encode(),
            "empty token": json.dumps({"token": "", "expires_at": "2030-01-01T00:00:00Z"}).encode(),
            "non-string token": json.dumps({"token": 5, "expires_at": "2030-01-01T00:00:00Z"}).encode(),
            "missing expiry": json.dumps({"token": FAKE_TOKEN}).encode(),
            "binary": b"\xff\xfe%s" % FAKE_TOKEN.encode(),
            "empty": b"",
            "too large": b'{"token": "%s"}' % (b"a" * (1 << 20)),
        }
        for label, body in bodies.items():
            with self.subTest(label):
                self.server.body = body
                done = self.call()
                self.assert_clean_failure(done)

    def test_redirects_are_not_followed_so_the_jwt_cannot_be_forwarded(self):
        elsewhere = FakeGitHub()
        self.addCleanup(elsewhere.server_close)
        self.addCleanup(elsewhere.shutdown)
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                self.server.status = status
                self.server.location = elsewhere.url + "/app/installations/678/access_tokens"
                done = self.call()
                self.assert_clean_failure(done)
                self.assertIn(f"HTTP {status}", done.stderr)
        self.assertEqual(elsewhere.requests, [], "the Authorization header must never leave the api url")

    def test_connection_refused_fails_cleanly(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        done = self.call(base=f"http://127.0.0.1:{port}")
        self.assert_clean_failure(done)
        self.assertIn("cannot reach", done.stderr)

    def test_a_server_that_drops_the_connection_fails_cleanly(self):
        self.server.drop = True
        done = self.call()
        self.assert_clean_failure(done)

    # -- an installation that has not granted a requested permission --------

    def grant(self, **changes):
        """Make the fake installation grant PERMISSIONS, except as ``changes`` say (a level, or None to drop it)."""
        granted = dict(PERMISSIONS)
        for name, level in changes.items():
            if level is None:
                granted.pop(name)
            else:
                granted[name] = level
        self.server.granted = granted

    def sent(self, method):
        return [request for request in self.server.requests if request["method"] == method]

    def test_a_missing_permission_is_named_in_a_fixed_token_free_line(self):
        self.grant(actions=None)
        done = self.call()
        self.assert_clean_failure(done, "not granted to this installation")
        self.assertEqual(
            done.stderr,
            "token_broker: permission missing: actions (HTTP 422: the installation has not granted them)\n",
        )

    def test_several_missing_permissions_are_listed_in_sorted_order(self):
        # issues, actions and checks are absent; contents is granted below the level requested.
        self.grant(issues=None, actions=None, contents="read", checks=None)
        done = self.call()
        self.assert_clean_failure(done)
        self.assertEqual(
            done.stderr,
            "token_broker: permission missing: actions, checks, contents, issues "
            "(HTTP 422: the installation has not granted them)\n",
        )

    def test_a_stronger_grant_covers_a_weaker_request(self):
        self.grant(actions="write", checks="admin", metadata="admin", issues=None)
        done = self.call()
        self.assert_clean_failure(done)
        self.assertIn("permission missing: issues (", done.stderr)
        broker = load_broker()
        self.assertEqual(broker.missing_permissions({**PERMISSIONS, "actions": "admin", "metadata": "write"}), [])
        self.assertEqual(broker.missing_permissions({**PERMISSIONS, "contents": "read"}), ["contents"])
        self.assertEqual(broker.missing_permissions({**PERMISSIONS, "contents": "none"}), ["contents"])
        self.assertEqual(broker.missing_permissions({**PERMISSIONS, "contents": None}), ["contents"])

    def test_a_422_the_installation_does_not_explain_keeps_the_generic_message(self):
        # Every requested permission is granted, so the cause is something else (here: the repository).
        self.grant()
        self.server.status = 422
        self.server.body = json.dumps({"message": "There is at least one repository that does not exist."}).encode()
        done = self.call()
        self.assert_clean_failure(done, "does not exist")
        self.assertIn("HTTP 422 (GitHub rejected the requested repository or permissions)", done.stderr)
        self.assertNotIn("permission missing", done.stderr)
        self.assertEqual(len(self.sent("GET")), 1)

    def test_a_follow_up_that_cannot_be_used_keeps_the_generic_message(self):
        cases = {
            "server error": {"installation_status": 500},
            "not found": {"installation_status": 404},
            "unauthorised": {"installation_status": 401},
            "dropped": {"installation_drop": True},
            "invalid json": {"installation_body": b"{not json"},
            "binary": {"installation_body": b"\xff\xfe"},
            "not an object": {"installation_body": b"[1, 2]"},
            "no permissions": {"installation_body": b'{"id": 678}'},
            "permissions null": {"installation_body": b'{"permissions": null}'},
            "permissions a list": {"installation_body": b'{"permissions": ["actions"]}'},
            "permissions a string": {"installation_body": b'{"permissions": "actions"}'},
            "permissions empty": {"installation_body": b'{"permissions": {}}'},
            "nested too deep": {"installation_body": b"".join((b"[" * 200_000, b"]" * 200_000))},
            "too large": {"installation_body": b"".join((b'{"permissions": {"a": "', b"a" * (1 << 20), b'"}}'))},
        }
        defaults = {"installation_status": 200, "installation_body": None, "installation_drop": False}
        for label, changes in cases.items():
            with self.subTest(label):
                for name, value in {**defaults, **changes}.items():
                    setattr(self.server, name, value)
                self.grant(actions=None)  # the POST is refused with 422 in every case
                done = self.call()
                self.assert_clean_failure(done)
                self.assertIn("HTTP 422 (GitHub rejected the requested repository or permissions)", done.stderr)
                self.assertNotIn("permission missing", done.stderr)

    def test_a_hostile_installation_document_cannot_put_text_into_the_message(self):
        self.server.status = 422
        self.server.installation_body = json.dumps(
            {
                "permissions": {
                    "actions": FAKE_TOKEN,  # not a level
                    "contents": "write",
                    "pull_requests": ["write"],  # not a string
                    "issues": "write",
                    "statuses": "write",
                    "checks": "read",
                    "metadata": "read",
                    "evil\nname " + self.pem: "write",
                    FAKE_TOKEN: "admin",
                    "rm -rf /; token_broker: forged": "write",
                }
            }
        ).encode()
        done = self.call()
        self.assert_clean_failure(done, "rm -rf", "forged")
        self.assertEqual(
            done.stderr,
            "token_broker: permission missing: actions, pull_requests (HTTP 422: the installation has not granted them)\n",
        )

    def test_there_is_exactly_one_token_request_and_never_a_narrower_one(self):
        self.grant(actions=None, issues=None)
        done = self.call()
        self.assert_clean_failure(done)
        (post,) = self.sent("POST")
        self.assertEqual(json.loads(post["body"]), {"repositories": ["widgets"], "permissions": PERMISSIONS})
        (get,) = self.sent("GET")
        self.assertEqual((get["path"], get["body"]), ("/app/installations/678", b""))
        self.assertEqual([request["method"] for request in self.server.requests], ["POST", "GET"])

    def test_the_follow_up_read_sends_the_same_jwt_to_the_same_host_and_follows_no_redirect(self):
        elsewhere = FakeGitHub()
        self.addCleanup(elsewhere.server_close)
        self.addCleanup(elsewhere.shutdown)
        self.grant(actions=None)
        self.server.installation_status = 302
        self.server.installation_location = elsewhere.url + "/app/installations/678"
        done = self.call()
        self.assert_clean_failure(done)
        self.assertIn("HTTP 422 (GitHub rejected", done.stderr)
        self.assertEqual(elsewhere.requests, [], "the Authorization header must never leave the api url")
        (post,) = self.sent("POST")
        (get,) = self.sent("GET")
        self.assertEqual(get["headers"]["authorization"], post["headers"]["authorization"])
        self.assertTrue(get["headers"]["authorization"].startswith("Bearer "))
        self.assertEqual(get["headers"]["accept"], "application/vnd.github+json")
        self.assertEqual(get["headers"]["x-github-api-version"], "2022-11-28")
        self.assertEqual(get["headers"]["user-agent"], "factory-token-broker")

    def test_only_a_422_leads_to_the_follow_up_read(self):
        # The installation lacks a permission, but the token request failed for another reason.
        self.server.installation_body = json.dumps({"permissions": {"metadata": "read"}}).encode()
        for status in (400, 401, 403, 404, 409, 500, 502):
            with self.subTest(status=status):
                self.server.status = status
                done = self.call()
                self.assert_clean_failure(done)
                self.assertIn(f"HTTP {status}", done.stderr)
                self.assertNotIn("permission missing", done.stderr)
        self.assertEqual(self.sent("GET"), [])

    def test_a_missing_permission_through_a_real_subprocess_prints_one_clean_line(self):
        self.grant(issues=None)
        done = self.run_broker()
        self.assert_clean_failure(done)
        self.assertEqual(
            done.stderr,
            "token_broker: permission missing: issues (HTTP 422: the installation has not granted them)\n",
        )
        self.assertEqual(len(self.sent("POST")), 1)

    def test_the_supervisor_reads_the_message_back_even_with_every_permission_missing(self):
        from factory.github_auth import AppAuth, PermissionMissingError

        def relay(stderr):
            auth = AppAuth(
                1, 2, "acme/widgets", "factory", 3, ["broker"],
                run=lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, stdout="", stderr=stderr),
                environ={"GITHUB_APP_PRIVATE_KEY": "fake"},
            )  # fmt: skip
            with self.assertRaises(PermissionMissingError) as caught:
                auth.probe()
            return caught.exception

        self.grant(actions=None)
        error = relay(self.call().stderr)
        self.assertEqual(error.permissions, ("actions",))
        self.assertIn("permission missing: actions", str(error))

        self.server.granted = {"unrelated": "read"}  # the largest message the broker can print
        line = self.call().stderr
        self.assertTrue(line.isascii() and line.strip().isprintable(), line)
        self.assertLessEqual(len(line.strip()), len("token_broker: ") + 200)
        error = relay(line)
        self.assertEqual(error.permissions, tuple(sorted(PERMISSIONS)))
        self.assertIn(line.strip(), str(error), "the whole line is relayed, not cut")

    # -- input validation: no key use, no network ---------------------------

    def test_invalid_inputs_exit_non_zero_without_any_network_call(self):
        good = {"--app-id": "1", "--installation-id": "2", "--repo": "acme/widgets", "--api-url": self.server.url}
        bad = {
            "--app-id": ["0", "-1", "abc", "1.5", "", " 7", "+7", "007", "١٢٣", "9" * 25],
            "--installation-id": ["0", "-3", "x", "1e3", "", "2 ", "0x10"],
            "--repo": [
                "widgets", "a/b/c", "a/", "/b", "a b/c", "a/b c", "a/..", "a/.", "-a/b", "a/b\n",
                "a/b;rm", "a/b/", "../b", f"{'a' * 40}/b", f"a/{'b' * 101}", "",
            ],
            "--api-url": [
                "ftp://example.com", "http://example.com", "not a url", "https://", "//x",
                "https://user:pw@api.github.com", "https://api.github.com?x=1", "https://api.github.com#f",
                "http://127.0.0.1:99999",
            ],
        }  # fmt: skip
        for option, values in bad.items():
            for value in values:
                with self.subTest(option=option, value=value):
                    args = []
                    for name, ok in good.items():
                        args += [name, value if name == option else ok]
                    done = self.call(args=args)
                    self.assertEqual(done.returncode, 1, done.stderr)
                    self.assertEqual(done.stdout, "")
                    self.assertTrue(done.stderr.startswith("token_broker: "))
        self.assertEqual(self.server.requests, [])

    def test_missing_arguments_exit_non_zero_without_any_network_call(self):
        for args in ([], ["--app-id", "1"], ["--app-id", "1", "--installation-id", "2"]):
            with self.subTest(args=args):
                done = self.call(args=args)
                self.assertEqual(done.returncode, 1)
                self.assertEqual(done.stdout, "")
        self.assertEqual(self.server.requests, [])

    def test_https_is_required_except_for_loopback(self):
        done = self.call(args=["--app-id", "1", "--installation-id", "2", "--repo", "a/b",
                                     "--api-url", "http://api.github.com"])  # fmt: skip
        self.assertEqual(done.returncode, 1)
        self.assertIn("https", done.stderr)

    def test_missing_or_bad_key_exits_non_zero_without_any_network_call(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        ec_pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        encrypted = self.private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b"pw"),
        ).decode()
        truncated = self.pem[: len(self.pem) // 2]
        cases = {"missing": None, "empty": "", "blank": "  \n", "garbage": "not a pem", "ec key": ec_pem,
                 "encrypted": encrypted, "truncated": truncated}  # fmt: skip
        for label, key in cases.items():
            with self.subTest(label):
                done = self.call(key=key)
                self.assertEqual(done.returncode, 1, done.stderr)
                self.assertEqual(done.stdout, "")
                self.assertTrue(done.stderr.startswith("token_broker: "))
                for secret in (ec_pem, encrypted, truncated, *self.key_lines):
                    self.assertNotIn(secret.strip(), done.stderr)
                self.assertIn("GITHUB_APP_PRIVATE_KEY", done.stderr)
        self.assertEqual(self.server.requests, [])


class BrokerSourceTests(unittest.TestCase):
    def test_the_broker_is_executable_and_names_the_key_variable_only_via_the_environment(self):
        source = BROKER.read_text()
        self.assertTrue(os.access(BROKER, os.X_OK))
        self.assertIn('os.environ.get(KEY_ENV', source)
        self.assertNotIn("--private-key", source)

    def test_requirements_pin_cryptography_with_a_lower_bound(self):
        lines = [
            line.strip()
            for line in (BROKER.parent / "requirements-token-broker.txt").read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r"^cryptography>=\d+(\.\d+)*$")

    def test_validation_works_without_the_cryptography_package(self):
        """Bad input must be rejected before the import that needs the dependency."""
        done = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.modules['cryptography'] = None; sys.argv = ['x']; "
             f"sys.path.insert(0, {str(BROKER.parent)!r}); import token_broker; "
             "sys.exit(token_broker.main(['--app-id','0','--installation-id','1','--repo','a/b']))"],
            env={"PATH": os.environ.get("PATH", "")},
            capture_output=True,
            text=True,
            timeout=60,
        )  # fmt: skip
        self.assertEqual(done.returncode, 1)
        self.assertIn("--app-id", done.stderr)

    def test_a_missing_cryptography_package_is_a_clean_failure(self):
        done = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.modules['cryptography'] = None; "
             f"sys.path.insert(0, {str(BROKER.parent)!r}); import token_broker; "
             "sys.exit(token_broker.main(['--app-id','1','--installation-id','1','--repo','a/b']))"],
            env={"PATH": os.environ.get("PATH", ""), "GITHUB_APP_PRIVATE_KEY": "fake-key-value-for-tests"},
            capture_output=True,
            text=True,
            timeout=60,
        )  # fmt: skip
        self.assertEqual(done.returncode, 1)
        self.assertIn("cryptography", done.stderr)
        self.assertNotIn("fake-key-value-for-tests", done.stderr)
        self.assertNotIn("Traceback", done.stderr)


class CoreDumpTests(unittest.TestCase):
    def test_failure_to_disable_core_dumps_is_reported_without_secrets(self):
        broker = load_broker()
        err = io.StringIO()
        with (
            mock.patch("resource.setrlimit", side_effect=OSError("boom")),
            contextlib.redirect_stderr(err),
        ):
            broker._no_core_dumps()
        self.assertEqual(err.getvalue(), "token_broker: could not disable core dumps: OSError\n")

    def test_success_is_silent(self):
        broker = load_broker()
        err = io.StringIO()
        with mock.patch("resource.setrlimit") as limit, contextlib.redirect_stderr(err):
            broker._no_core_dumps()
        limit.assert_called_once()
        self.assertEqual(err.getvalue(), "")


if __name__ == "__main__":
    unittest.main()

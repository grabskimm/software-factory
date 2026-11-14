"""The stdlib Kubernetes client (src/factory/kube.py) against the in-memory fake (tests/fake_kube.py).

Slice 7 of the pod backend (docs/pod-backend.md). Everything here is CONTRACT-TESTED AGAINST ASSUMED
SHAPES: the client talks to a fake API server over a local HTTP server on 127.0.0.1 with a fake clock
and an injected sleep, so nothing sleeps and no cluster was consulted. The tests pin, one by one:

* the request: paths, headers, the DeleteOptions body, and that the client only ever asks for the six
  operations of the launcher (no watch, no pods/log, no exec);
* the token: re-read on every request and on every retry, read as a bounded regular file (a FIFO does
  not hang it), and never echoed;
* the error mapping: 409, 404, 401 and 403 (fatal, never retried), 422 (rule ids only), other 4xx;
* the retry: 429 and 5xx only, bounded, with the contract's backoff; a POST that may have been
  applied is not repeated after a network failure;
* the timeouts: connect and read on every call, and a deadline for the whole answer of one attempt
  (the read timeout is per socket operation, so a server that trickles bytes must not hold a call);
* the HTTPS path: certificates and host names are verified against the CA file that was given, and a
  verification failure is a fatal configuration error that is never retried;
* no leak: canaries planted in response bodies, pod specs and tokens never reach an exception, a
  log line or a returned value;
* the owner reference garbage collection of the fake, and the replay of the golden policy mutations
  of slice 6 through client and fake (the fake's admission hook runs the mirror of the policy).
"""

import ast
import contextlib
import http.client
import inspect
import io
import json
import logging
import os
import socket
import ssl
import sys
import tempfile
import threading
import time
import traceback
import unittest
from pathlib import Path
from unittest import mock

from factory import kube
from factory import podcontract as pc
from factory import podspec
from tests import fake_kube
from tests.fake_kube import HANG, FakeClock, FakeKube, match_selector

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = Path(__file__).resolve().parent / "golden"

TOKEN_A = "eyJhbGciOiJSUzI1NiJ9.CANARYTOKENAAAA.sigA"
TOKEN_B = "eyJhbGciOiJSUzI1NiJ9.CANARYTOKENBBBB.sigB"
TOKEN_C = "eyJhbGciOiJSUzI1NiJ9.CANARYTOKENCCCC.sigC"
CANARY_BODY = "CANARY-RESPONSE-BODY-7d1f"
CANARY_SPEC = "CANARY-POD-SPEC-93ab"
SECRET_KEY = "CANARY-ANTHROPIC-KEY-55e0"
SECRET_TOKEN = "CANARY-RELAY-TOKEN-11c4"
# A self-signed CA certificate made for these tests only (CN factory-kube-test-ca, EC P-256, valid for 100 years).
# Its private key was thrown away when it was made: it can vouch for nothing, it is only a CA file to load.
TEST_CA_PEM = """\
-----BEGIN CERTIFICATE-----
MIIBpjCCAUugAwIBAgIUeEV0zrlMQM+oGt3aZ+DlqVPZ6okwCgYIKoZIzj0EAwIw
HzEdMBsGA1UEAwwUZmFjdG9yeS1rdWJlLXRlc3QtY2EwIBcNMjYxMTA5MTcxNzQx
WhgPMjEyNjEwMTYxNzE3NDFaMB8xHTAbBgNVBAMMFGZhY3Rvcnkta3ViZS10ZXN0
LWNhMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEwnxbUiPiwRejxy4g4up1AOfd
Ne3Pd7PDXlrL//AoAcC5k3+NzBo6Vg/925aLg0J5ODdqG/UXPkojooCq70PcaaNj
MGEwHQYDVR0OBBYEFNzycCitM3phDKK4FkVg9mjaun6kMB8GA1UdIwQYMBaAFNzy
cCitM3phDKK4FkVg9mjaun6kMA8GA1UdEwEB/wQFMAMBAf8wDgYDVR0PAQH/BAQD
AgIEMAoGCCqGSM49BAMCA0kAMEYCIQCncCQfEuM5e3K+1j5Pj8SYLkihPZnbKcS6
q4sI6e5z1gIhAKjOKR+V4EBNEfVNfSv+A6m5B64vddpgNlIEk4UP2Rg4
-----END CERTIFICATE-----
"""


class Captured(logging.Handler):
    """Every log record of the process while a test runs (message and exception text)."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())
        if record.exc_info and record.exc_info[1] is not None:
            self.lines.append(repr(record.exc_info[1]))


class KubeCase(unittest.TestCase):
    """A fake API server on a fake clock, a token file, a client with an injected sleep, and a log capture."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.token_path = self.dir / "token"
        self.write_token(TOKEN_A)
        self.clock = FakeClock()
        self.fake = FakeKube(clock=self.clock, tokens={TOKEN_A}).start()
        self.addCleanup(self.fake.stop)
        self.sleeps = []
        self.logs = Captured()
        root = logging.getLogger()
        previous = root.level
        root.addHandler(self.logs)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, previous)
        self.addCleanup(root.removeHandler, self.logs)
        self.client = self.make_client()

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.clock.advance(seconds)

    def write_token(self, value):
        self.token_path.write_text(f"{value}\n", encoding="ascii")

    def make_client(self, **options):
        options.setdefault("sleep", self.sleep)
        options.setdefault("clock", self.clock)
        options.setdefault("token_path", self.token_path)
        return kube.Client(self.fake.url, **options)

    def last(self):
        return self.fake.calls()[-1]

    def assertNoLeak(self, error, *canaries):
        """None of the canaries is in the exception (text, args, attributes, traceback) or in a log line."""
        rendered = [
            str(error),
            repr(error),
            repr(error.args),
            repr(vars(error)),
            "".join(traceback.format_exception(error)),
            *self.logs.lines,
        ]
        for canary in canaries:
            for text in rendered:
                self.assertNotIn(canary, text)


# --- Configuration -----------------------------------------------------------------------------------


class ConfigurationTest(unittest.TestCase):
    def test_the_in_pod_environment_names_come_from_the_contract(self):
        self.assertEqual(kube.SERVICE_HOST_ENV, "KUBERNETES_SERVICE_HOST")
        self.assertEqual(kube.SERVICE_PORT_ENV, "KUBERNETES_SERVICE_PORT")
        self.assertEqual(kube.SERVICE_HOST_ENV, pc.CANARY_API_PROBES[0]["host_env"])
        self.assertEqual(kube.SERVICE_PORT_ENV, pc.CANARY_API_PROBES[0]["port_env"])

    def test_the_defaults_are_the_contract_numbers(self):
        parameters = inspect.signature(kube.Client.__init__).parameters
        self.assertEqual(parameters["connect_timeout"].default, pc.KUBE_CONNECT_TIMEOUT_SECONDS)
        self.assertEqual(parameters["read_timeout"].default, pc.KUBE_READ_TIMEOUT_SECONDS)
        self.assertEqual(parameters["retries"].default, pc.KUBE_RETRIES)
        self.assertEqual(parameters["backoff"].default, pc.KUBE_RETRY_BACKOFF_SECONDS)
        self.assertEqual(parameters["namespace"].default, pc.WORKER_NAMESPACE)
        self.assertEqual((pc.KUBE_CONNECT_TIMEOUT_SECONDS, pc.KUBE_READ_TIMEOUT_SECONDS, pc.KUBE_RETRIES), (5, 30, 3))

    def test_the_service_account_files_are_the_standard_in_pod_paths(self):
        self.assertEqual(kube.TOKEN_PATH, "/var/run/secrets/kubernetes.io/serviceaccount/token")
        self.assertEqual(kube.CA_PATH, "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")

    def test_in_cluster_reads_the_address_from_the_environment(self):
        context = ssl.create_default_context()
        client = kube.Client.in_cluster(
            {kube.SERVICE_HOST_ENV: "10.0.0.1", kube.SERVICE_PORT_ENV: "443"}, ssl_context=context
        )
        self.assertEqual((client._scheme, client._host, client._port), ("https", "10.0.0.1", 443))
        v6 = kube.Client.in_cluster({kube.SERVICE_HOST_ENV: "fd00::1", kube.SERVICE_PORT_ENV: "443"}, ssl_context=context)
        self.assertEqual((v6._scheme, v6._host, v6._port), ("https", "fd00::1", 443))
        named = kube.Client.in_cluster(
            {kube.SERVICE_HOST_ENV: "kubernetes.default.svc", kube.SERVICE_PORT_ENV: "6443"}, ssl_context=context
        )
        self.assertEqual((named._host, named._port), ("kubernetes.default.svc", 6443))

    def test_in_cluster_refuses_a_missing_or_malformed_address_without_echoing_it(self):
        context = ssl.create_default_context()
        bad = (
            {},
            {kube.SERVICE_HOST_ENV: "10.0.0.1"},
            {kube.SERVICE_PORT_ENV: "443"},
            {kube.SERVICE_HOST_ENV: "10.0.0.1", kube.SERVICE_PORT_ENV: "0"},
            {kube.SERVICE_HOST_ENV: "10.0.0.1", kube.SERVICE_PORT_ENV: "70000"},
            {kube.SERVICE_HOST_ENV: "10.0.0.1", kube.SERVICE_PORT_ENV: "44a"},
            {kube.SERVICE_HOST_ENV: "evil.example/x@y CANARY", kube.SERVICE_PORT_ENV: "443"},
            {kube.SERVICE_HOST_ENV: "", kube.SERVICE_PORT_ENV: "443"},
        )
        for environ in bad:
            with self.subTest(environ=environ), self.assertRaises(kube.ConfigError) as caught:
                kube.Client.in_cluster(environ, ssl_context=context)
            self.assertNotIn("CANARY", str(caught.exception))

    def test_plain_http_is_refused_for_every_host_but_loopback(self):
        for url in ("http://10.0.0.1:8080", "http://kubernetes.default.svc", "http://[fd00::1]:80"):
            with self.subTest(url=url), self.assertRaises(kube.ConfigError):
                kube.Client(url)
        for url in ("http://127.0.0.1:1", "http://localhost:2", "http://[::1]:3"):
            with self.subTest(url=url):
                kube.Client(url)

    def test_a_base_url_with_credentials_a_path_or_a_query_is_refused(self):
        for url in (
            "https://user:pw@10.0.0.1",
            "https://10.0.0.1/api",
            "https://10.0.0.1?x=1",
            "ftp://10.0.0.1",
            "",
            None,
            "10.0.0.1:443",
        ):
            with self.subTest(url=url), self.assertRaises(kube.ConfigError):
                kube.Client(url, ssl_context=ssl.create_default_context())

    def test_an_unusable_ca_file_is_a_configuration_error(self):
        with tempfile.TemporaryDirectory() as directory:
            garbage = Path(directory) / "ca.crt"
            garbage.write_text("not a certificate", encoding="ascii")
            for path in (str(garbage), str(Path(directory) / "missing.crt")):
                with self.subTest(path=path), self.assertRaises(kube.ConfigError):
                    kube.Client("https://10.0.0.1:443", ca_path=path)

    def test_bad_options_are_refused(self):
        context = ssl.create_default_context()
        for options in (
            {"namespace": "Not Valid"},
            {"namespace": ""},
            {"retries": -1},
            {"retries": True},
            {"retries": 11},
            {"backoff": ()},
            {"backoff": (1, -1)},
            {"connect_timeout": 0},
            {"read_timeout": True},
            {"read_timeout": "30"},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                kube.Client("https://10.0.0.1:443", ssl_context=context, **options)

    def test_the_tls_context_and_both_timeouts_reach_the_connection(self):
        context = ssl.create_default_context()
        seen = []

        class Refusing:
            def __init__(self):
                self.sock = None

            def connect(self):
                raise ConnectionRefusedError

            def close(self):
                pass

        def factory(scheme, host, port, timeout, ctx):
            seen.append((scheme, host, port, timeout, ctx))
            return Refusing()

        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "token"
            token.write_text(TOKEN_A, encoding="ascii")
            client = kube.Client(
                "https://10.0.0.1:6443",
                ssl_context=context,
                token_path=token,
                sleep=lambda seconds: None,
                connection_factory=factory,
            )
            with self.assertLogs("factory.kube", "WARNING"), self.assertRaises(kube.Unavailable):
                client.get_pod(fake_kube.worker_pod(1)["metadata"]["name"])
        self.assertEqual(len(seen), 1 + pc.KUBE_RETRIES)
        for scheme, host, port, timeout, ctx in seen:
            self.assertEqual((scheme, host, port, timeout), ("https", "10.0.0.1", 6443, pc.KUBE_CONNECT_TIMEOUT_SECONDS))
            self.assertIs(ctx, context)


# --- The HTTPS path --------------------------------------------------------------------------------------


class UnverifiablePeer:
    """Stands for `http.client.HTTPSConnection`: records how it was built and fails as a peer that cannot be verified."""

    built = []

    def __init__(self, host, port=None, timeout=None, context=None):
        self.args = (host, port, timeout, context)
        self.requests = []
        self.built.append(self)

    def connect(self):
        raise ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] CANARY-VERIFY-DETAIL")

    def request(self, *arguments, **keywords):
        self.requests.append(arguments)

    def close(self):
        return None


class HttpsPathTest(unittest.TestCase):
    """The path that production takes: https, the CA read from a file, certificate and host name verified.

    No network and no certificate authority: the connection class is a stand-in that records how it was built.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.ca = self.dir / "ca.crt"
        self.ca.write_text(TEST_CA_PEM, encoding="ascii")
        self.token = self.dir / "token"
        self.token.write_text(TOKEN_A, encoding="ascii")
        self.sleeps = []
        UnverifiablePeer.built = []

    def client(self, **options):
        return kube.Client("https://10.0.0.1:6443", ca_path=self.ca, token_path=self.token, sleep=self.sleeps.append, **options)

    def test_the_default_connection_of_https_is_an_https_connection_with_the_context_it_was_given(self):
        context = ssl.create_default_context()
        with mock.patch.object(http.client, "HTTPSConnection") as made:
            connection = kube._default_connection("https", "10.0.0.1", 6443, 5, context)
        made.assert_called_once_with("10.0.0.1", 6443, timeout=5, context=context)
        self.assertIs(connection, made.return_value)
        real = kube._default_connection("https", "10.0.0.1", 6443, 5, context)
        self.assertIs(type(real), http.client.HTTPSConnection)
        self.assertEqual((real.host, real.port, real.timeout), ("10.0.0.1", 6443, 5))
        plain = kube._default_connection("http", "127.0.0.1", 8080, 5, None)
        self.assertIs(type(plain), http.client.HTTPConnection)

    def test_the_connection_gets_a_context_that_verifies_certificate_and_host_against_the_ca_file_it_was_given(self):
        with mock.patch.object(http.client, "HTTPSConnection", UnverifiablePeer), self.assertRaises(kube.ConfigError):
            self.client().get_pod(fake_kube.worker_pod(1)["metadata"]["name"])
        (peer,) = UnverifiablePeer.built
        host, port, timeout, context = peer.args
        self.assertEqual((host, port, timeout), ("10.0.0.1", 6443, pc.KUBE_CONNECT_TIMEOUT_SECONDS))
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        # Only the CA of the file that was named: the system store is not trusted besides it.
        (certificate,) = context.get_ca_certs()
        self.assertEqual(certificate["subject"], ((("commonName", "factory-kube-test-ca"),),))

    def test_a_certificate_that_does_not_verify_is_a_fatal_configuration_error_and_is_tried_once(self):
        with mock.patch.object(http.client, "HTTPSConnection", UnverifiablePeer), self.assertRaises(kube.ConfigError) as caught:
            self.client().create_pod(fake_kube.worker_pod(1))
        self.assertEqual(str(caught.exception), "kubernetes TLS verification failed")
        self.assertIsNone(caught.exception.status)
        self.assertEqual(len(UnverifiablePeer.built), 1)  # not retried
        self.assertEqual(self.sleeps, [])
        self.assertEqual(UnverifiablePeer.built[0].requests, [])  # nothing, the token included, went to that peer
        for text in (str(caught.exception), repr(caught.exception), "".join(traceback.format_exception(caught.exception))):
            self.assertNotIn("CANARY-VERIFY-DETAIL", text)

    def test_a_tls_failure_that_is_not_a_verification_failure_is_a_transport_failure_that_is_retried(self):
        class Handshake(UnverifiablePeer):
            def connect(self):
                raise ssl.SSLError(1, "[SSL: HANDSHAKE_FAILURE]")

        with mock.patch.object(http.client, "HTTPSConnection", Handshake):
            with self.assertLogs("factory.kube", "WARNING"), self.assertRaises(kube.Unavailable):
                self.client().get_pod(fake_kube.worker_pod(1)["metadata"]["name"])
        self.assertEqual(len(UnverifiablePeer.built), 1 + pc.KUBE_RETRIES)
        self.assertEqual(self.sleeps, list(pc.KUBE_RETRY_BACKOFF_SECONDS))


# --- The token -------------------------------------------------------------------------------------------


class TokenTest(KubeCase):
    def test_the_token_is_sent_as_a_bearer_header(self):
        self.client.list_pods({pc.label_key("role"): "worker"})
        self.assertEqual(self.last().headers["authorization"], f"Bearer {TOKEN_A}")

    def test_the_token_is_re_read_on_every_request(self):
        for token in (TOKEN_A, TOKEN_B, TOKEN_C, TOKEN_A):
            self.write_token(token)
            self.fake.tokens = {token}
            self.client.list_pods({pc.label_key("role"): "worker"})
            self.assertEqual(self.last().headers["authorization"], f"Bearer {token}")
        self.assertEqual(len(self.fake.calls()), 4)

    def test_the_token_is_re_read_on_every_retry(self):
        self.fake.inject("list_pods", times=2, status=503)

        def rotate(seconds):
            self.sleeps.append(seconds)
            token = TOKEN_B if len(self.sleeps) == 1 else TOKEN_C
            self.write_token(token)
            self.fake.tokens = {token}

        client = self.make_client(sleep=rotate)
        client.list_pods({pc.label_key("role"): "worker"})
        seen = [record.headers["authorization"] for record in self.fake.calls()]
        self.assertEqual(seen, [f"Bearer {TOKEN_A}", f"Bearer {TOKEN_B}", f"Bearer {TOKEN_C}"])

    def test_a_projected_token_is_a_symlink_that_the_kubelet_swaps_and_it_is_followed(self):
        first, second = self.dir / "first", self.dir / "second"
        for directory, token in ((first, TOKEN_A), (second, TOKEN_B)):
            directory.mkdir()
            (directory / "token").write_text(token, encoding="ascii")
        data, link = self.dir / "data", self.dir / "link"
        data.symlink_to(first)
        link.symlink_to(Path("data") / "token")
        client = self.make_client(token_path=link)
        client.list_pods({pc.label_key("role"): "worker"})
        self.assertEqual(self.last().headers["authorization"], f"Bearer {TOKEN_A}")
        swapped = self.dir / "swapped"
        swapped.symlink_to(second)
        os.replace(swapped, data)  # the atomic swap of the kubelet's `..data` link
        self.fake.tokens = {TOKEN_B}
        client.list_pods({pc.label_key("role"): "worker"})
        self.assertEqual(self.last().headers["authorization"], f"Bearer {TOKEN_B}")

    def test_a_fifo_in_place_of_the_token_is_refused_and_does_not_hang(self):
        fifo = self.dir / "fifo"
        os.mkfifo(fifo)
        client = self.make_client(token_path=fifo)
        with self.assertRaises(kube.ConfigError):
            client.list_pods({pc.label_key("role"): "worker"})
        self.assertEqual(self.fake.calls(), [])

    def test_a_directory_a_missing_file_and_an_oversized_file_are_refused(self):
        big = self.dir / "big"
        big.write_text("a" * (kube.TOKEN_MAX_BYTES + 1), encoding="ascii")
        for path in (self.dir, self.dir / "missing", big):
            with self.subTest(path=path.name), self.assertRaises(kube.ConfigError):
                self.make_client(token_path=path).list_pods({pc.label_key("role"): "worker"})
        self.assertEqual(self.fake.calls(), [])

    def test_a_fifo_that_holds_a_valid_looking_token_is_refused_all_the_same(self):
        fifo = self.dir / "fifo"
        os.mkfifo(fifo)
        writer = os.open(fifo, os.O_RDWR)  # keeps the FIFO open and fed: a read of it WOULD return the token
        self.addCleanup(os.close, writer)
        os.write(writer, TOKEN_A.encode("ascii"))
        with self.assertRaises(kube.ConfigError):
            self.make_client(token_path=fifo).list_pods({pc.label_key("role"): "worker"})
        self.assertEqual(self.fake.calls(), [])

    def test_a_file_over_the_size_bound_is_refused_although_its_first_bytes_are_a_valid_token(self):
        big = self.dir / "big"
        big.write_bytes(b"".join((TOKEN_A.encode("ascii"), b"\n" * kube.TOKEN_MAX_BYTES)))
        self.assertGreater(big.stat().st_size, kube.TOKEN_MAX_BYTES)
        with self.assertRaises(kube.ConfigError):
            self.make_client(token_path=big).list_pods({pc.label_key("role"): "worker"})
        self.assertEqual(self.fake.calls(), [])

    def test_a_file_of_exactly_the_size_bound_is_a_token(self):
        edge = self.dir / "edge"
        token = "a" * kube.TOKEN_MAX_BYTES
        edge.write_text(token, encoding="ascii")
        self.fake.tokens = {token}
        self.make_client(token_path=edge).list_pods({pc.label_key("role"): "worker"})
        self.assertEqual(self.last().headers["authorization"], f"Bearer {token}")

    def test_the_token_is_read_with_a_bound_of_one_byte_more_than_the_largest_token(self):
        asked = []
        real_read = os.read

        def recording(fd, size):
            asked.append(size)
            return real_read(fd, size)

        with mock.patch.object(kube.os, "read", recording):
            self.client.list_pods({pc.label_key("role"): "worker"})
        self.assertEqual(asked, [kube.TOKEN_MAX_BYTES + 1])

    def test_a_token_that_is_not_bearer_text_is_refused_before_anything_is_sent(self):
        for text in ("", "   \n", "abc def", "abc\ndef", "abc\r\nX-Evil: 1", "tokén", "a\x00b", "tok;x", "a" * 20000):
            self.token_path.write_bytes(text.encode("utf-8"))
            with self.subTest(text=text[:12]), self.assertRaises(kube.ConfigError) as caught:
                self.client.list_pods({pc.label_key("role"): "worker"})
            self.assertNoLeak(caught.exception, "X-Evil")
        self.assertEqual(self.fake.calls(), [])

    def test_the_trailing_newline_of_the_file_is_not_part_of_the_token(self):
        self.token_path.write_text(f"{TOKEN_A}\n\n", encoding="ascii")
        self.client.list_pods({pc.label_key("role"): "worker"})
        self.assertEqual(self.last().headers["authorization"], f"Bearer {TOKEN_A}")

    def test_a_refused_token_is_fatal_and_never_retried_or_echoed(self):
        self.fake.tokens = {TOKEN_B}
        with self.assertRaises(kube.ConfigError) as caught:
            self.client.list_pods({pc.label_key("role"): "worker"})
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(len(self.fake.calls()), 1)
        self.assertEqual(self.sleeps, [])
        self.assertNoLeak(caught.exception, TOKEN_A, TOKEN_B, "CANARYTOKEN")


# --- The six operations -------------------------------------------------------------------------------


class OperationsTest(KubeCase):
    def test_create_pod_posts_the_manifest_and_returns_the_pod_with_its_uid(self):
        pod = fake_kube.worker_pod(1)
        created = self.client.create_pod(pod)
        request = self.last()
        self.assertEqual((request.method, request.path), ("POST", "/api/v1/namespaces/factory-workers/pods"))
        self.assertEqual(request.body, pod)
        self.assertEqual(request.headers["content-type"], "application/json")
        self.assertEqual(request.headers["accept"], "application/json")
        self.assertEqual(request.headers["user-agent"], "factory-kube/1")
        self.assertEqual(created["metadata"]["name"], pod["metadata"]["name"])
        self.assertRegex(created["metadata"]["uid"], r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
        podspec.build_secret(created, SECRET_TOKEN, SECRET_KEY)  # the answer is what slice 6 needs

    def test_create_pod_twice_is_already_exists(self):
        self.client.create_pod(fake_kube.worker_pod(1))
        with self.assertRaises(kube.AlreadyExists) as caught:
            self.client.create_pod(fake_kube.worker_pod(1))
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(len(self.fake.calls("create_pod")), 2)
        self.assertEqual(self.sleeps, [])

    def test_get_pod_returns_it_and_a_missing_one_is_not_found(self):
        pod = fake_kube.worker_pod(1)
        self.client.create_pod(pod)
        got = self.client.get_pod(pod["metadata"]["name"])
        self.assertEqual(got["metadata"]["labels"], pod["metadata"]["labels"])
        self.assertEqual((self.last().method, self.last().path), ("GET", f"/api/v1/namespaces/factory-workers/pods/{pod['metadata']['name']}"))
        with self.assertRaises(kube.NotFound) as caught:
            self.client.get_pod(fake_kube.worker_pod(2)["metadata"]["name"])
        self.assertEqual(caught.exception.status, 404)

    def test_list_pods_returns_exactly_the_pods_whose_labels_equal_the_selector(self):
        mine = fake_kube.worker_pod(1)
        same_repo = fake_kube.worker_pod(2)
        other_repo = fake_kube.worker_pod(3, repository="Example-Org/another-repo")
        other_manager = fake_kube.worker_pod(4, instance="123456abcdef")
        unlabelled = fake_kube.worker_pod(5)
        del unlabelled["metadata"]["labels"][pc.label_key("repo")]
        for pod in (mine, same_repo, other_repo, other_manager, unlabelled):
            self.client.create_pod(pod)
        names = lambda pods: sorted(p["metadata"]["name"] for p in pods)  # noqa: E731
        repo = pc.label_key("repo")
        by_repo = self.client.list_pods({repo: pc.repo_key(fake_kube.REPOSITORY)})
        self.assertEqual(names(by_repo), names([mine, same_repo, other_manager]))
        both = self.client.list_pods({repo: pc.repo_key(fake_kube.REPOSITORY), pc.label_key("manager"): fake_kube.INSTANCE})
        self.assertEqual(names(both), names([mine, same_repo]))
        everything = self.client.list_pods({pc.label_key("role"): "worker"})
        self.assertEqual(len(everything), 5)
        self.assertEqual(self.client.list_pods({repo: pc.repo_key("Nobody/nothing")}), [])
        selector = self.last().query["labelSelector"]
        self.assertEqual(selector, f"{repo}={pc.repo_key('Nobody/nothing')}")

    def test_the_selector_of_several_labels_is_sorted_and_comma_joined(self):
        self.client.list_pods({pc.label_key("repo"): "ab12", pc.label_key("manager"): "cd34"})
        self.assertEqual(self.last().query["labelSelector"], f"{pc.label_key('manager')}=cd34,{pc.label_key('repo')}=ab12")
        self.assertEqual(self.last().query["limit"], str(kube.LIST_PAGE_SIZE))

    def test_a_bad_selector_is_refused_before_anything_is_sent(self):
        for selector in (None, {}, "a=b", [("a", "b")], {"a b": "c"}, {pc.label_key("role"): "Worker"}, {pc.label_key("role"): 1},
                         {pc.label_key("role"): "a,b=c"}, {1: "x"}, {pc.label_key("role"): None}):
            with self.subTest(selector=repr(selector)[:30]), self.assertRaises(ValueError):
                self.client.list_pods(selector)
        self.assertEqual(self.fake.calls(), [])

    def test_list_follows_the_pages_of_the_server(self):
        self.fake.page_size = 2
        for n in range(1, 6):
            self.client.create_pod(fake_kube.worker_pod(n))
        found = self.client.list_pods({pc.label_key("role"): "worker"})
        self.assertEqual(len(found), 5)
        self.assertEqual(len({p["metadata"]["name"] for p in found}), 5)
        pages = self.fake.calls("list_pods")
        self.assertEqual(len(pages), 3)
        self.assertEqual([p.query.get("continue") for p in pages], [None, "2", "4"])

    def test_a_server_that_never_stops_paging_is_bounded(self):
        endless = {"items": [], "metadata": {"continue": "x"}}
        self.fake.inject("list_pods", times=100, status=200, body=endless)
        with self.assertRaises(kube.BadResponse):
            self.client.list_pods({pc.label_key("role"): "worker"})
        self.assertEqual(len(self.fake.calls("list_pods")), kube.MAX_LIST_PAGES)

    def test_a_malformed_list_answer_is_a_bad_response(self):
        for body in ({"items": "no"}, {"items": [1]}, {"items": [], "metadata": {"continue": "a b"}}, {"items": [], "metadata": {"continue": 5}}):
            self.fake.inject("list_pods", status=200, body=body)
            with self.subTest(body=body), self.assertRaises(kube.BadResponse):
                self.client.list_pods({pc.label_key("role"): "worker"})

    def test_delete_pod_sends_delete_options_with_the_grace_period(self):
        pod = fake_kube.worker_pod(1)
        name = pod["metadata"]["name"]
        for grace, expected in ((None, pc.TERMINATION_GRACE_SECONDS), (5, 5), (0, 0)):
            self.client.create_pod(pod)
            if grace is None:
                self.client.delete_pod(name)
            else:
                self.client.delete_pod(name, grace)
            request = self.last()
            self.assertEqual((request.method, request.path), ("DELETE", f"/api/v1/namespaces/factory-workers/pods/{name}"))
            self.assertEqual(
                request.body,
                {"apiVersion": "v1", "kind": "DeleteOptions", "gracePeriodSeconds": expected, "propagationPolicy": "Background"},
            )
            self.assertEqual(request.headers["content-type"], "application/json")
            self.clock.advance(100)
            self.fake.collect_garbage()
            self.fake.pods()  # tick: a pod that was only terminating is gone after its grace

    def test_delete_pod_that_does_not_exist_is_not_found(self):
        with self.assertRaises(kube.NotFound):
            self.client.delete_pod(fake_kube.worker_pod(1)["metadata"]["name"])

    def test_an_unreasonable_grace_period_is_refused(self):
        name = fake_kube.worker_pod(1)["metadata"]["name"]
        for grace in (-1, True, 1.5, "30", None, kube.MAX_GRACE_SECONDS + 1):
            with self.subTest(grace=grace), self.assertRaises(ValueError):
                self.client.delete_pod(name, grace)
        self.assertEqual(self.fake.calls(), [])

    def test_create_secret_returns_the_name_and_uid_and_never_the_data(self):
        created_pod = self.client.create_pod(fake_kube.worker_pod(1))
        secret = podspec.build_secret(created_pod, SECRET_TOKEN, SECRET_KEY)
        returned = self.client.create_secret(secret)
        self.assertEqual(set(returned), {"name", "uid"})
        self.assertEqual(returned["name"], secret["metadata"]["name"])
        self.assertEqual((self.last().method, self.last().path), ("POST", "/api/v1/namespaces/factory-workers/secrets"))
        self.assertIn("data", self.fake.secrets()[returned["name"]])  # the server did store (and echo) the data
        self.assertNotIn(SECRET_KEY, repr(returned))
        self.assertEqual(self.last().body, secret)

    def test_create_secret_twice_is_already_exists_and_a_missing_uid_is_a_bad_response(self):
        created_pod = self.client.create_pod(fake_kube.worker_pod(1))
        secret = podspec.build_secret(created_pod, SECRET_TOKEN, SECRET_KEY)
        self.client.create_secret(secret)
        with self.assertRaises(kube.AlreadyExists):
            self.client.create_secret(secret)
        self.fake.inject("create_secret", status=201, body={"metadata": {}})
        with self.assertRaises(kube.BadResponse):
            self.client.create_secret(secret)

    def test_delete_secret_and_a_missing_one(self):
        created_pod = self.client.create_pod(fake_kube.worker_pod(1))
        secret = podspec.build_secret(created_pod, SECRET_TOKEN, SECRET_KEY)
        name = self.client.create_secret(secret)["name"]
        self.client.delete_secret(name)
        self.assertEqual((self.last().method, self.last().path), ("DELETE", f"/api/v1/namespaces/factory-workers/secrets/{name}"))
        self.assertEqual(self.fake.secrets(), {})
        with self.assertRaises(kube.NotFound):
            self.client.delete_secret(name)

    def test_the_client_only_ever_asks_for_the_six_operations(self):
        pod = fake_kube.worker_pod(1)
        created = self.client.create_pod(pod)
        name = self.client.create_secret(podspec.build_secret(created, SECRET_TOKEN, SECRET_KEY))["name"]
        self.client.get_pod(pod["metadata"]["name"])
        self.client.list_pods({pc.label_key("role"): "worker"})
        self.client.delete_secret(name)
        self.client.delete_pod(pod["metadata"]["name"])
        ops = {record.op for record in self.fake.calls()}
        self.assertEqual(ops, set(fake_kube.OPS))
        for record in self.fake.calls():
            self.assertNotIn("watch", record.query)
            self.assertNotIn("log", record.path.split("/")[-1])
            self.assertNotIn("exec", record.path)

    def test_names_that_are_not_worker_names_are_refused_before_anything_is_sent(self):
        for name in ("", "../secrets", "factory-w-zz", "factory-w-0123456789abcdef0123456789abcdef/log", None, 5, "other-pod", "factory-w-0123456789abcdef0123456789abcdef-s"):
            with self.subTest(name=name), self.assertRaises(ValueError) as caught:
                self.client.get_pod(name)
            self.assertNotIn("zz", str(caught.exception))
            with self.assertRaises(ValueError):
                self.client.delete_pod(name)
        for name in ("", "factory-w-0123456789abcdef0123456789abcdef", "../x-s", "factory-w-zz-s", None, "x/y-s"):
            with self.subTest(secret=name), self.assertRaises(ValueError):
                self.client.delete_secret(name)
        self.assertEqual(self.fake.calls(), [])

    def test_a_manifest_that_is_not_a_worker_pod_or_secret_of_the_namespace_is_refused(self):
        pod = fake_kube.worker_pod(1)
        created = self.client.create_pod(pod)
        secret = podspec.build_secret(created, SECRET_TOKEN, SECRET_KEY)
        before = len(self.fake.calls())

        def changed(manifest, *path, value):
            copy = json.loads(json.dumps(manifest))
            target = copy
            for step in path[:-1]:
                target = target[step]
            if value is fake_kube.Step:  # the marker for "delete the key"
                del target[path[-1]]
            else:
                target[path[-1]] = value
            return copy

        for bad in (
            changed(pod, "kind", value="Secret"),
            changed(pod, "apiVersion", value="v2"),
            changed(pod, "metadata", "namespace", value="factory"),
            changed(pod, "metadata", "name", value="evil"),
            changed(pod, "metadata", "name", value=5),
            changed(pod, "metadata", "namespace", value=fake_kube.Step),
            changed(pod, "metadata", value="no"),
            {"kind": "Pod"},
            [],
            None,
        ):
            with self.subTest(bad=repr(bad)[:40]), self.assertRaises(ValueError):
                self.client.create_pod(bad)
        for bad in (
            changed(secret, "kind", value="Pod"),
            changed(secret, "metadata", "namespace", value="factory"),
            changed(secret, "metadata", "name", value="factory-w-0123456789abcdef0123456789abcdef"),
            changed(secret, "metadata", "name", value="plain-s"),
            None,
        ):
            with self.subTest(bad=repr(bad)[:40]), self.assertRaises(ValueError):
                self.client.create_secret(bad)
        too_big = json.loads(json.dumps(pod))
        too_big["metadata"]["annotations"]["x"] = "a" * (kube.MAX_REQUEST_BYTES + 1)
        with self.assertRaises(ValueError):
            self.client.create_pod(too_big)
        unserialisable = json.loads(json.dumps(pod))
        unserialisable["metadata"]["annotations"]["x"] = {1, 2}
        with self.assertRaises(ValueError):
            self.client.create_pod(unserialisable)
        self.assertEqual(len(self.fake.calls()), before)

    def test_a_client_of_another_namespace_uses_that_namespace_in_every_path(self):
        client = self.make_client(namespace="elsewhere")
        with self.assertRaises(ValueError):
            client.create_pod(fake_kube.worker_pod(1))  # a manifest of factory-workers
        client.list_pods({pc.label_key("role"): "worker"})
        self.assertEqual(self.last().path, "/api/v1/namespaces/elsewhere/pods")


# --- Garbage collection by owner reference -----------------------------------------------------------------


class GarbageCollectionTest(KubeCase):
    def launch(self, n):
        created = self.client.create_pod(fake_kube.worker_pod(n))
        secret = self.client.create_secret(podspec.build_secret(created, SECRET_TOKEN, SECRET_KEY))
        return created["metadata"]["name"], secret["name"]

    def test_deleting_a_pod_deletes_its_secret(self):
        name, secret = self.launch(1)
        other_pod, other_secret = self.launch(2)
        self.assertEqual(set(self.fake.secrets()), {secret, other_secret})
        self.client.delete_pod(name, 0)
        self.assertEqual(set(self.fake.secrets()), {other_secret})
        self.assertEqual(set(self.fake.pods()), {other_pod})

    def test_a_running_pod_keeps_its_secret_until_the_grace_has_passed(self):
        name, _secret = self.launch(1)
        self.fake.script(name, ())
        self.client.delete_pod(name)  # a pod without a scenario stays Pending: removed at once
        self.assertEqual(self.fake.secrets(), {})
        self.fake.script(fake_kube.worker_pod(3)["metadata"]["name"], "karpenter_eviction")
        running = self.client.create_pod(fake_kube.worker_pod(3))["metadata"]["name"]
        self.client.create_secret(podspec.build_secret(self.client.get_pod(running), SECRET_TOKEN, SECRET_KEY))
        self.clock.advance(1)
        self.assertEqual(self.client.get_pod(running)["status"]["phase"], "Running")
        self.client.delete_pod(running, 30)
        self.assertIn("deletionTimestamp", self.client.get_pod(running)["metadata"])
        self.assertIn(pc.secret_name(running), self.fake.secrets())
        self.clock.advance(30)
        with self.assertRaises(kube.NotFound):
            self.client.get_pod(running)
        self.assertNotIn(pc.secret_name(running), self.fake.secrets())

    def test_a_node_that_is_deleted_takes_the_pod_and_its_secret_with_it(self):
        name, secret = self.launch(1)
        self.fake.delete_node(name)
        with self.assertRaises(kube.NotFound):
            self.client.get_pod(name)
        self.assertNotIn(secret, self.fake.secrets())

    def test_a_secret_whose_owner_is_absent_is_collected_and_one_with_no_owner_is_not(self):
        created = self.client.create_pod(fake_kube.worker_pod(1))
        wrong_uid = podspec.build_secret(created, SECRET_TOKEN, SECRET_KEY)
        wrong_uid["metadata"]["ownerReferences"][0]["uid"] = "99999999-9999-4999-8999-999999999999"
        self.client.create_secret(wrong_uid)
        self.assertEqual(self.fake.secrets(), {})
        unowned = podspec.build_secret(created, SECRET_TOKEN, SECRET_KEY)
        del unowned["metadata"]["ownerReferences"]
        self.client.create_secret(unowned)
        self.assertEqual(set(self.fake.secrets()), {unowned["metadata"]["name"]})

    def test_a_secret_created_before_its_pod_exists_is_collected(self):
        created = self.client.create_pod(fake_kube.worker_pod(1))
        secret = podspec.build_secret(created, SECRET_TOKEN, SECRET_KEY)
        self.client.delete_pod(created["metadata"]["name"], 0)
        self.client.create_secret(secret)  # the owner is already gone
        self.assertEqual(self.fake.secrets(), {})


# --- Error mapping and retry ------------------------------------------------------------------------------


class ErrorMappingTest(KubeCase):
    def call(self):
        return self.client.get_pod(fake_kube.worker_pod(1)["metadata"]["name"])

    def test_each_status_maps_to_one_error_and_none_is_retried(self):
        expected = {
            400: kube.Rejected,
            401: kube.ConfigError,
            403: kube.ConfigError,
            404: kube.NotFound,
            405: kube.Rejected,
            409: kube.AlreadyExists,
            413: kube.Rejected,
            422: kube.Denied,
            301: kube.Rejected,
            302: kube.Rejected,
        }
        for status, error in expected.items():
            self.fake.inject("get_pod", status=status, headers={"Location": "http://127.0.0.1:1/elsewhere"} if status in (301, 302) else None)
            before = len(self.fake.calls())
            with self.subTest(status=status), self.assertRaises(error) as caught:
                self.call()
            self.assertEqual(caught.exception.status, status)
            self.assertEqual(type(caught.exception), error)
            self.assertEqual(len(self.fake.calls()), before + 1, "a final status is answered once, never retried")
        self.assertEqual(self.sleeps, [])

    def test_a_redirect_is_not_followed(self):
        self.fake.inject("get_pod", status=307, headers={"Location": f"{self.fake.url}/api/v1/namespaces/factory-workers/pods"})
        with self.assertRaises(kube.Rejected):
            self.call()
        self.assertEqual(len(self.fake.calls()), 1)

    def test_an_authentication_or_authorisation_failure_is_fatal_not_retryable(self):
        for status in (401, 403):
            self.fake.inject("get_pod", times=5, status=status)
            with self.subTest(status=status), self.assertRaises(kube.ConfigError):
                self.call()
        self.assertEqual(len(self.fake.calls()), 2)
        self.assertEqual(self.sleeps, [])

    def test_error_messages_are_fixed_texts(self):
        texts = {
            kube.AlreadyExists(): "kubernetes object already exists",
            kube.NotFound(): "kubernetes object not found",
            kube.Denied(("image",)): "kubernetes admission denied the object",
            kube.Rejected(400): "kubernetes rejected the request",
            kube.Unavailable(503): "kubernetes unavailable",
            kube.BadResponse(): "unexpected kubernetes response",
        }
        for error, text in texts.items():
            self.assertEqual(str(error), text)
        self.assertEqual({error.kind for error in texts}, {"already_exists", "not_found", "denied", "rejected", "unavailable", "bad_response"})
        self.assertTrue(all(isinstance(error, kube.KubeError) for error in texts))

    def test_a_denial_names_rule_ids_and_only_known_ones_in_contract_order(self):
        message = f"denied request: [privileged] {CANARY_BODY} [not-a-rule] [host-path] [Image] [image] [x]"
        self.fake.inject("create_pod", status=422, body={"message": message, "details": {"causes": [{"message": "[run-as] more"}, "junk", {"message": 5}]}})
        with self.assertRaises(kube.Denied) as caught:
            self.client.create_pod(fake_kube.worker_pod(1))
        self.assertEqual(caught.exception.rules, tuple(r for r in podspec.POLICY_RULES if r in {"privileged", "host-path", "image", "run-as"}))
        self.assertNoLeak(caught.exception, CANARY_BODY, "not-a-rule")

    def test_the_real_servers_policy_denial_is_a_403_that_is_denied_not_credentials(self):
        # Recorded from kind v1.36.1: a ValidatingAdmissionPolicy whose reason is Forbidden answers 403
        # with `[rule:<id>]`, and an authorisation failure is ALSO a 403 (it stays a ConfigError).
        message = (
            'pods "factory-w-x" is forbidden: ValidatingAdmissionPolicy \'factory-worker-pods\' with binding '
            "'factory-worker-pods' denied request: [rule:image] every container image must be the pinned digest reference"
        )
        self.fake.inject("create_pod", status=403, body={"kind": "Status", "message": message, "reason": "Forbidden", "code": 403})
        with self.assertRaises(kube.Denied) as caught:
            self.client.create_pod(fake_kube.worker_pod(1))
        self.assertEqual((caught.exception.status, caught.exception.rules), (403, ("image",)))
        self.assertEqual(len(self.fake.calls("create_pod")), 1, "a denial is not retried")
        authorisation = 'pods is forbidden: User "system:serviceaccount:factory:factory" cannot create resource "pods" in the namespace "x"'
        for body in ({"kind": "Status", "message": authorisation, "code": 403}, b"not json", {"message": "[rule:image] but no policy named"}, {"message": 5}):
            self.fake.inject("create_pod", status=403, body=body)
            with self.subTest(body=str(body)[:30]), self.assertRaises(kube.ConfigError) as refused:
                self.client.create_pod(fake_kube.worker_pod(1))
            self.assertEqual(refused.exception.status, 403)

    def test_a_denial_without_ids_or_with_an_unreadable_body_has_no_rules(self):
        for body in (b"not json", b"[]", {"message": 5}, {"message": "no brackets here image privileged"}, {"details": []}):
            self.fake.inject("create_pod", status=422, body=body)
            with self.subTest(body=body), self.assertRaises(kube.Denied) as caught:
                self.client.create_pod(fake_kube.worker_pod(1))
            self.assertEqual(caught.exception.rules, ())

    def test_a_success_body_that_is_not_a_json_object_is_a_bad_response(self):
        bodies = (b"not json", b"[]", b'"text"', b"", b"null", b"".join([b"[" * 100000, b"]" * 100000]))
        for body in bodies:
            self.fake.inject("get_pod", status=200, body=body)
            with self.subTest(body=body[:10]), self.assertRaises(kube.BadResponse):
                self.call()

    def test_a_response_over_the_size_bound_is_a_bad_response(self):
        with mock.patch.object(kube, "MAX_RESPONSE_BYTES", 1000):
            self.fake.inject("get_pod", status=200, body={"filler": "a" * 2000})
            with self.assertRaises(kube.BadResponse):
                self.call()


class RawWireCase(KubeCase):
    """A raw socket server that answers however a test wants, and a client for it."""

    CHUNKED = b"Transfer-Encoding: chunked\r\n\r\n"

    def setUp(self):
        super().setUp()
        self.accepted = 0  # connections the raw server took: one per attempt

    @staticmethod
    def read_request(connection):
        """Everything the client sends of one request: the head and the body its Content-Length announces."""
        request = b""
        while b"\r\n\r\n" not in request:
            chunk = connection.recv(65536)
            if not chunk:
                return
            request = b"".join((request, chunk))
        head, _, body = request.partition(b"\r\n\r\n")
        length = 0
        for line in head.lower().split(b"\r\n"):
            if line.startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1])
        while len(body) < length:
            chunk = connection.recv(65536)
            if not chunk:
                return
            body = b"".join((body, chunk))

    def serve(self, answer):
        """A raw socket server that reads one request per connection, sends `answer` and hangs up. Its URL.

        `answer` is the bytes to send, or a function of the connection that sends them (and may take its time).
        """
        server = socket.socket()
        self.addCleanup(server.close)
        server.bind(("127.0.0.1", 0))
        server.listen(8)

        def loop():
            while True:
                try:
                    connection, _ = server.accept()
                except OSError:
                    return
                self.accepted += 1
                with connection, contextlib.suppress(OSError):  # the client may hang up at any moment
                    connection.settimeout(2)
                    connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    self.read_request(connection)
                    if callable(answer):
                        answer(connection)
                    else:
                        connection.sendall(answer)

        threading.Thread(target=loop, daemon=True).start()
        return f"http://127.0.0.1:{server.getsockname()[1]}"

    def client_for(self, answer, clock=None, **options):
        sleep = options.pop("sleep", self.sleep)
        return kube.Client(self.serve(answer), sleep=sleep, clock=clock or self.clock, token_path=self.token_path, **options)


class MalformedWireTest(RawWireCase):
    """A server that puts broken HTTP on the wire: nothing but a KubeError ever leaves the client."""

    PARTIAL = b'{"kind":"Pod","metadata":{"na'

    def test_a_negative_chunk_size_in_a_200_or_a_422_is_unavailable_not_a_value_error(self):
        name = fake_kube.worker_pod(1)["metadata"]["name"]
        for status in (b"200 OK", b"422 Unprocessable"):
            answer = b"".join((b"HTTP/1.1 ", status, b"\r\n", self.CHUNKED, b"-5\r\nabc\r\n0\r\n\r\n"))
            with self.subTest(status=status, verb="GET"):
                with self.assertRaises(kube.Unavailable) as caught:
                    self.client_for(answer).get_pod(name)
                self.assertFalse(caught.exception.outcome_unknown)
            with self.subTest(status=status, verb="POST"):
                with self.assertRaises(kube.Unavailable) as caught:
                    self.client_for(answer).create_pod(fake_kube.worker_pod(1))
                self.assertTrue(caught.exception.outcome_unknown)  # the request went out and was maybe applied

    def test_a_negative_chunk_size_is_retried_for_a_get_like_any_other_transport_failure(self):
        client = self.client_for(b"".join((b"HTTP/1.1 200 OK\r\n", self.CHUNKED, b"-5\r\nabc\r\n0\r\n\r\n")))
        with self.assertRaises(kube.Unavailable):
            client.get_pod(fake_kube.worker_pod(1)["metadata"]["name"])
        self.assertEqual(self.sleeps, list(pc.KUBE_RETRY_BACKOFF_SECONDS))
        self.assertTrue(any("(network)" in line for line in self.logs.lines))

    def test_no_malformed_answer_raises_anything_but_a_kube_error(self):
        name = fake_kube.worker_pod(1)["metadata"]["name"]
        answers = {
            "bad chunk size": (b"".join((b"HTTP/1.1 200 OK\r\n", self.CHUNKED, b"ZZ\r\n")), kube.Unavailable),
            "huge chunk size": (b"".join((b"HTTP/1.1 200 OK\r\n", self.CHUNKED, b"F" * 40, b"\r\nabc\r\n0\r\n\r\n")), kube.Unavailable),
            "bad status line": (b"HTTP/1.1 abc\r\n\r\n", kube.Unavailable),
            "bad content length": (b"HTTP/1.1 200 OK\r\nContent-Length: -3\r\n\r\nabc", kube.BadResponse),
            "short body": (b"HTTP/1.1 200 OK\r\nContent-Length: 50\r\n\r\nabc", kube.Unavailable),
            "not http": (b"\x00\xff garbage", kube.Unavailable),
        }
        for label, (answer, expected) in answers.items():
            with self.subTest(label), self.assertRaises(expected):
                self.client_for(answer).get_pod(name)

    # -- a 2xx answer that ends before the bytes it announced ---------------------------------------------

    def cut_short(self, status=b"200 OK", length=300):
        return b"".join((b"HTTP/1.1 ", status, b"\r\nContent-Length: ", str(length).encode(), b"\r\n\r\n", self.PARTIAL))

    def test_a_get_whose_body_is_cut_short_by_a_clean_close_is_unavailable_and_retried(self):
        client = self.client_for(self.cut_short())
        with self.assertRaises(kube.Unavailable) as caught:
            client.get_pod(fake_kube.worker_pod(1)["metadata"]["name"])
        self.assertFalse(caught.exception.outcome_unknown)
        self.assertEqual(self.accepted, 1 + pc.KUBE_RETRIES)
        self.assertEqual(self.sleeps, list(pc.KUBE_RETRY_BACKOFF_SECONDS))
        self.assertTrue(any("(network)" in line for line in self.logs.lines))

    def test_a_list_and_a_delete_cut_short_are_retried_like_a_get(self):
        for label, call in (
            ("list", lambda client: client.list_pods({pc.label_key("role"): "worker"})),
            ("delete", lambda client: client.delete_pod(fake_kube.worker_pod(1)["metadata"]["name"], 0)),
        ):
            before = self.accepted
            with self.subTest(label), self.assertRaises(kube.Unavailable) as caught:
                call(self.client_for(self.cut_short()))
            self.assertFalse(caught.exception.outcome_unknown)
            self.assertEqual(self.accepted - before, 1 + pc.KUBE_RETRIES)

    def test_a_create_answered_201_and_cut_short_is_unavailable_with_an_unknown_outcome_and_not_repeated(self):
        secret = podspec.build_secret(self.client.create_pod(fake_kube.worker_pod(1)), SECRET_TOKEN, SECRET_KEY)
        for label, call in (
            ("pod", lambda client: client.create_pod(fake_kube.worker_pod(2))),
            ("secret", lambda client: client.create_secret(secret)),
        ):
            before = self.accepted
            with self.subTest(label), self.assertRaises(kube.Unavailable) as caught:
                call(self.client_for(self.cut_short(b"201 Created")))
            self.assertTrue(caught.exception.outcome_unknown)  # a 2xx was on its way: the object may exist
            self.assertEqual(self.accepted - before, 1)
            self.assertNoLeak(caught.exception, "metadata", SECRET_KEY, SECRET_TOKEN)

    def test_a_chunked_body_that_stops_before_its_last_chunk_is_unavailable(self):
        for label, tail in (
            ("between chunks", b"".join((b"1e\r\n", self.PARTIAL, b"\r\n"))),
            ("inside a chunk", b"".join((b"64\r\n", self.PARTIAL))),
        ):
            answer = b"".join((b"HTTP/1.1 200 OK\r\n", self.CHUNKED, tail))
            with self.subTest(label), self.assertRaises(kube.Unavailable):
                self.client_for(answer).get_pod(fake_kube.worker_pod(1)["metadata"]["name"])

    def test_a_body_that_is_complete_is_not_mistaken_for_a_cut_short_one(self):
        name = fake_kube.worker_pod(1)["metadata"]["name"]
        body = json.dumps({"kind": "Pod", "metadata": {"name": name}}).encode("ascii")
        whole = b"".join((b"HTTP/1.1 200 OK\r\nContent-Length: ", str(len(body)).encode(), b"\r\n\r\n", body))
        chunked = b"".join((b"HTTP/1.1 200 OK\r\n", self.CHUNKED, f"{len(body):x}".encode(), b"\r\n", body, b"\r\n0\r\n\r\n"))
        for label, answer in (("content-length", whole), ("chunked", chunked)):
            with self.subTest(label):
                self.assertEqual(self.client_for(answer).get_pod(name)["metadata"]["name"], name)
        self.assertEqual(self.sleeps, [])

    def test_a_body_that_ends_with_the_connection_has_no_length_to_check_and_is_judged_as_it_came(self):
        name = fake_kube.worker_pod(1)["metadata"]["name"]
        head = b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n"
        whole = self.client_for(b"".join((head, b'{"kind":"Pod"}'))).get_pod(name)
        self.assertEqual(whole, {"kind": "Pod"})
        with self.assertRaises(kube.BadResponse):  # nothing says that it ended too early: only the JSON does
            self.client_for(b"".join((head, self.PARTIAL))).get_pod(name)
        self.assertEqual(self.sleeps, [])


class TrickleSocket:
    """A connected socket that hands over one piece of an answer per receive, each receive costing `step` fake seconds."""

    def __init__(self, clock, pieces, step):
        self.clock = clock
        self.pieces = list(pieces)
        self.step = step
        self.timeouts = []  # every settimeout, in order
        self.received = 0  # pieces handed over

    def settimeout(self, value):
        self.timeouts.append(value)

    def gettimeout(self):
        return self.timeouts[-1] if self.timeouts else None

    def sendall(self, data):
        return None

    def close(self):
        return None

    def makefile(self, mode="rb", buffering=None):
        return TrickleReader(self)


class TrickleReader(io.RawIOBase):
    def __init__(self, sock):
        super().__init__()
        self.sock = sock

    def readable(self):
        return True

    def readinto(self, buffer):
        if not self.sock.pieces:
            return 0
        piece = self.sock.pieces.pop(0)
        self.sock.clock.advance(self.sock.step)
        self.sock.received += 1
        buffer[: len(piece)] = piece
        return len(piece)


class TrickleConnection(http.client.HTTPConnection):
    """An `http.client` connection to nowhere: `connect` hands out a TrickleSocket (the attempts are kept)."""

    def __init__(self, make_socket):
        super().__init__("127.0.0.1")
        self.make_socket = make_socket

    def connect(self):
        self.sock = self.make_socket()


class DeadlineTest(RawWireCase):
    """The read timeout is per socket operation: one attempt must still end by its deadline, whatever trickles in."""

    HEAD = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 200\r\n\r\n"
    BODY = b"".join((b"{", b" " * 198, b"}"))  # a JSON object that takes 200 bytes to arrive
    STEP = 25.0  # fake seconds one receive costs

    @staticmethod
    def singles(data):
        return [bytes([byte]) for byte in data]

    def trickle(self, pieces, step=None):
        """A client whose every connection is a TrickleSocket on its own fake clock; (client, clock, sockets)."""
        clock = FakeClock()
        sockets = []

        def make_socket():
            sockets.append(TrickleSocket(clock, pieces, step or self.STEP))
            return sockets[-1]

        def factory(scheme, host, port, timeout, context):
            return TrickleConnection(make_socket)

        client = kube.Client(
            self.fake.url,
            sleep=clock.advance,
            clock=clock,
            token_path=self.token_path,
            connection_factory=factory,
        )
        return client, clock, sockets

    def test_the_documented_numbers(self):
        self.assertEqual((kube.MAX_ATTEMPT_SECONDS, kube.MAX_CALL_SECONDS), (60.0, 120.0))

    def test_one_attempt_waits_at_most_its_deadline_for_the_body_and_the_time_left_bounds_every_receive(self):
        client, clock, sockets = self.trickle([self.HEAD, *self.singles(self.BODY)])
        started = clock()
        with self.assertRaises(kube.Unavailable) as caught:
            client.get_pod(fake_kube.worker_pod(1)["metadata"]["name"])
        self.assertFalse(caught.exception.outcome_unknown)
        # Attempt 1: receives at 0, 25 and 50 s (the head, then two body bytes); at 75 s the 60 s are over.
        # The socket timeout of a receive is the read timeout or, when less is left, what is left.
        self.assertEqual(sockets[0].received, 3)
        self.assertEqual(sockets[0].timeouts, [30, 30, 30, 10])
        # Attempt 2 starts at 76 s (75 s plus the 1 s backoff) and may not outlast the call (120 s): 2 receives.
        self.assertEqual(sockets[1].received, 2)
        self.assertEqual(sockets[1].timeouts, [30, 30, 19])
        self.assertEqual(len(sockets), 2)  # 126 s into the call + 2 s of backoff is past the budget: no third
        self.assertEqual(clock() - started, 126)
        self.assertTrue(any("(timeout)" in line for line in self.logs.lines))

    def test_the_status_line_and_the_headers_are_under_the_deadline_too(self):
        client, clock, sockets = self.trickle([*self.singles(self.HEAD), self.BODY])
        started = clock()
        with self.assertRaises(kube.Unavailable):
            client.get_pod(fake_kube.worker_pod(1)["metadata"]["name"])
        self.assertEqual([sock.received for sock in sockets], [3, 2])
        self.assertEqual(clock() - started, 126)

    def test_a_post_that_trickles_is_not_repeated_and_its_outcome_is_unknown(self):
        client, _clock, sockets = self.trickle([self.HEAD, *self.singles(self.BODY)])
        with self.assertRaises(kube.Unavailable) as caught:
            client.create_pod(fake_kube.worker_pod(1))
        self.assertTrue(caught.exception.outcome_unknown)
        self.assertEqual(len(sockets), 1)

    def test_an_answer_that_is_in_time_is_read_whole(self):
        client, _clock, sockets = self.trickle([self.HEAD, *self.singles(self.BODY)], step=0.01)
        self.assertEqual(client.get_pod(fake_kube.worker_pod(1)["metadata"]["name"]), {})
        self.assertEqual(sockets[0].received, 1 + len(self.BODY))

    def test_a_server_that_trickles_over_a_real_socket_does_not_hold_the_call(self):
        # No single receive ever waits long (a byte every few milliseconds), but the fake clock moves 20 s per
        # byte: without a deadline the client would read all 200 bytes and succeed.
        for label, preface, pieces in (
            ("body", self.HEAD, self.singles(self.BODY)),
            ("status line and headers", b"", self.singles(b"".join((self.HEAD, self.BODY)))),
        ):
            clock = FakeClock()

            def answer(connection, preface=preface, pieces=pieces, clock=clock):
                connection.sendall(preface)
                for piece in pieces:
                    connection.sendall(piece)
                    clock.advance(20)
                    time.sleep(0.002)

            with self.subTest(label), self.assertRaises(kube.Unavailable):
                self.client_for(answer, clock=clock, sleep=clock.advance).get_pod(fake_kube.worker_pod(1)["metadata"]["name"])
            self.assertTrue(any("(timeout)" in line for line in self.logs.lines))


class RetryTest(KubeCase):
    def call(self, client=None):
        return (client or self.client).list_pods({pc.label_key("role"): "worker"})

    def test_503_twice_then_success_sleeps_the_backoff(self):
        self.fake.inject("list_pods", times=2, status=503)
        self.assertEqual(self.call(), [])
        self.assertEqual(len(self.fake.calls()), 3)
        self.assertEqual(self.sleeps, [1, 2])

    def test_429_and_every_5xx_are_retried(self):
        for status in (429, 500, 502, 503, 504, 599):
            self.sleeps.clear()
            self.fake.inject("list_pods", status=status)
            with self.subTest(status=status):
                self.assertEqual(self.call(), [])
                self.assertEqual(self.sleeps, [1])

    def test_the_default_bound_is_the_contract_and_the_backoff_is_the_contract(self):
        self.fake.inject("list_pods", times=50, status=503)
        with self.assertRaises(kube.Unavailable) as caught:
            self.call()
        self.assertEqual(len(self.fake.calls()), 1 + pc.KUBE_RETRIES)
        self.assertEqual(self.sleeps, list(pc.KUBE_RETRY_BACKOFF_SECONDS))
        self.assertEqual(caught.exception.status, 503)
        self.assertFalse(caught.exception.outcome_unknown)

    def test_retries_zero_means_one_attempt(self):
        self.fake.inject("list_pods", times=5, status=503)
        with self.assertRaises(kube.Unavailable):
            self.call(self.make_client(retries=0))
        self.assertEqual((len(self.fake.calls()), self.sleeps), (1, []))

    def test_the_backoff_repeats_its_last_value_when_there_are_more_retries_than_steps(self):
        self.fake.inject("list_pods", times=50, status=503)
        with self.assertRaises(kube.Unavailable):
            self.call(self.make_client(retries=5, backoff=(1, 2)))
        self.assertEqual(self.sleeps, [1, 2, 2, 2, 2])

    def test_retry_after_raises_the_delay_and_is_capped(self):
        for header, expected in (("2", 2), ("0", 1), ("999", kube.MAX_RETRY_AFTER_SECONDS), ("abc", 1), ("-5", 1), ("1.5", 1), ("", 1)):
            self.sleeps.clear()
            self.fake.inject("list_pods", status=429, headers={"Retry-After": header})
            with self.subTest(header=header):
                self.call()
                self.assertEqual(self.sleeps, [expected])

    def test_a_call_starts_no_attempt_past_its_time_budget(self):
        self.fake.inject("list_pods", times=50, status=503)

        def slow(seconds):
            self.sleeps.append(seconds)
            self.clock.advance(kube.MAX_CALL_SECONDS - 1)  # the first sleep eats nearly the whole budget

        with self.assertRaises(kube.Unavailable):
            self.call(self.make_client(sleep=slow))
        self.assertEqual(len(self.fake.calls()), 2)
        self.assertEqual(len(self.sleeps), 1)

    def test_every_retry_is_logged_with_words_and_numbers_only(self):
        self.fake.inject("list_pods", times=2, status=503, body={"message": CANARY_BODY})
        self.call()
        retries = [line for line in self.logs.lines if "retry" in line]
        self.assertEqual(retries, ["kubernetes GET request failed (503), retry 1 of 3", "kubernetes GET request failed (503), retry 2 of 3"])

    def test_a_dropped_connection_is_retried_for_get_and_delete(self):
        name = fake_kube.worker_pod(1)["metadata"]["name"]
        self.client.create_pod(fake_kube.worker_pod(1))
        self.fake.inject("get_pod", status=None)
        self.assertEqual(self.client.get_pod(name)["metadata"]["name"], name)
        self.assertEqual(self.sleeps, [1])
        self.sleeps.clear()
        self.fake.inject("delete_pod", status=None)
        self.client.delete_pod(name, 0)
        self.assertEqual(self.sleeps, [1])
        self.assertEqual(len(self.fake.calls("delete_pod")), 2)

    def test_a_post_that_was_sent_and_dropped_is_not_repeated_and_says_the_outcome_is_unknown(self):
        self.fake.inject("create_pod", times=5, status=None)
        with self.assertRaises(kube.Unavailable) as caught:
            self.client.create_pod(fake_kube.worker_pod(1))
        self.assertTrue(caught.exception.outcome_unknown)
        self.assertEqual(len(self.fake.calls("create_pod")), 1)
        self.assertEqual(self.sleeps, [])

    def test_a_post_answered_500_is_retried_and_its_outcome_stays_unknown_when_the_retries_run_out(self):
        self.fake.inject("create_pod", times=1 + pc.KUBE_RETRIES, status=500)  # exactly the attempts of one call
        with self.assertRaises(kube.Unavailable) as caught:
            self.client.create_pod(fake_kube.worker_pod(1))
        self.assertEqual(len(self.fake.calls("create_pod")), 1 + pc.KUBE_RETRIES)
        self.assertTrue(caught.exception.outcome_unknown)
        self.fake.inject("create_pod", times=1 + pc.KUBE_RETRIES, status=429)
        with self.assertRaises(kube.Unavailable) as caught:
            self.client.create_pod(fake_kube.worker_pod(1))
        self.assertFalse(caught.exception.outcome_unknown, "a 429 was not applied")

    def test_a_post_that_may_have_landed_stays_unknown_when_a_later_attempt_is_a_429_or_503(self):
        for later in (429, 503):
            with self.subTest(later=later):
                self.fake.requests.clear()
                self.fake.inject("create_pod", times=1, status=500)
                self.fake.inject("create_pod", times=5, status=later)
                with self.assertRaises(kube.Unavailable) as caught:
                    self.client.create_pod(fake_kube.worker_pod(1))
                self.assertEqual(len(self.fake.calls("create_pod")), 1 + pc.KUBE_RETRIES)
                self.assertEqual(caught.exception.status, later)
                self.assertTrue(caught.exception.outcome_unknown, "the 500 may have been applied")

    def test_a_post_answered_502_then_429_stays_unknown(self):
        self.fake.inject("create_pod", times=1, status=502)
        self.fake.inject("create_pod", times=5, status=429)
        with self.assertRaises(kube.Unavailable) as caught:
            self.client.create_pod(fake_kube.worker_pod(1))
        self.assertEqual(caught.exception.status, 429)
        self.assertTrue(caught.exception.outcome_unknown)

    def test_a_post_answered_500_then_a_refused_connection_stays_unknown(self):
        self.fake.inject("create_pod", times=1, status=500)

        def stop_after_the_first_wait(seconds):
            self.sleeps.append(seconds)
            self.fake.stop()  # the next attempts find nothing listening: nothing is sent

        client = self.make_client(sleep=stop_after_the_first_wait)
        with self.assertRaises(kube.Unavailable) as caught:
            client.create_pod(fake_kube.worker_pod(1))
        self.assertIsNone(caught.exception.status)
        self.assertEqual(len(self.fake.calls("create_pod")), 1)
        self.assertEqual(len(self.sleeps), pc.KUBE_RETRIES)
        self.assertTrue(caught.exception.outcome_unknown, "the 500 may have been applied")

    def test_a_post_that_was_never_applied_is_not_unknown_whatever_the_mix_of_failures(self):
        self.fake.inject("create_pod", times=1, status=429)
        self.fake.inject("create_pod", times=5, status=503)
        with self.assertRaises(kube.Unavailable) as caught:
            self.client.create_pod(fake_kube.worker_pod(1))
        self.assertFalse(caught.exception.outcome_unknown)

    def test_a_get_never_reports_an_unknown_outcome(self):
        self.fake.inject("list_pods", times=1, status=500)
        self.fake.inject("list_pods", times=5, status=503)
        with self.assertRaises(kube.Unavailable) as caught:
            self.call()
        self.assertFalse(caught.exception.outcome_unknown)

    def test_a_retried_create_that_landed_the_first_time_is_already_exists(self):
        pod = fake_kube.worker_pod(1)
        self.client.create_pod(pod)
        self.fake.inject("create_pod", status=503)
        with self.assertRaises(kube.AlreadyExists):
            self.client.create_pod(pod)  # the 503 is retried and the retry finds the pod
        self.assertEqual(len(self.fake.calls("create_pod")), 3)

    def test_a_409_after_a_post_that_may_have_been_applied_is_marked_possibly_ours(self):
        for status in (500, 502, 504):
            with self.subTest(status=status):
                self.fake.requests.clear()
                self.fake.inject("create_pod", status=status, apply=True)  # landed, answered 5xx
                with self.assertRaises(kube.AlreadyExists) as caught:
                    self.client.create_pod(fake_kube.worker_pod(1))
                self.assertTrue(caught.exception.outcome_unknown)
                self.assertEqual([call.status for call in self.fake.calls("create_pod")], [status, 409])
                self.client.delete_pod(fake_kube.worker_pod(1)["metadata"]["name"])

    def test_a_409_on_the_first_attempt_or_after_attempts_that_were_not_applied_is_not_marked(self):
        pod = fake_kube.worker_pod(1)
        self.client.create_pod(pod)
        with self.assertRaises(kube.AlreadyExists) as caught:
            self.client.create_pod(pod)
        self.assertFalse(caught.exception.outcome_unknown)
        for status in (429, 503):
            with self.subTest(status=status):
                self.fake.inject("create_pod", status=status)
                with self.assertRaises(kube.AlreadyExists) as caught:
                    self.client.create_pod(pod)
                self.assertFalse(caught.exception.outcome_unknown)

    def test_a_refused_connection_is_retried_even_for_a_post_because_nothing_was_sent(self):
        self.fake.stop()
        with self.assertRaises(kube.Unavailable) as caught:
            self.client.create_pod(fake_kube.worker_pod(1))
        self.assertFalse(caught.exception.outcome_unknown)
        self.assertEqual(self.sleeps, list(pc.KUBE_RETRY_BACKOFF_SECONDS))
        self.assertTrue(any("(network)" in line for line in self.logs.lines))


class TimeoutTest(KubeCase):
    def recording_client(self, **options):
        seen = {"connect": [], "read": []}

        def factory(scheme, host, port, timeout, context):
            connection = kube._default_connection(scheme, host, port, timeout, context)
            seen["connect"].append(timeout)
            request = connection.request

            def request_and_record(*arguments, **keywords):
                seen["read"].append(connection.sock.gettimeout())  # the socket timeout once the request goes out
                return request(*arguments, **keywords)

            connection.request = request_and_record
            return connection

        return self.make_client(connection_factory=factory, **options), seen

    def test_every_call_has_a_connect_timeout_and_a_read_timeout(self):
        client, seen = self.recording_client()
        pod = fake_kube.worker_pod(1)
        client.create_pod(pod)
        client.get_pod(pod["metadata"]["name"])
        client.list_pods({pc.label_key("role"): "worker"})
        client.delete_pod(pod["metadata"]["name"], 0)
        self.assertEqual(seen["connect"], [pc.KUBE_CONNECT_TIMEOUT_SECONDS] * 4)
        self.assertEqual(seen["read"], [pc.KUBE_READ_TIMEOUT_SECONDS] * 4)

    def test_every_retry_has_the_timeouts_too(self):
        client, seen = self.recording_client(connect_timeout=2, read_timeout=7)
        self.fake.inject("list_pods", times=2, status=503)
        client.list_pods({pc.label_key("role"): "worker"})
        self.assertEqual((seen["connect"], seen["read"]), ([2] * 3, [7] * 3))

    def test_a_server_that_never_answers_ends_in_a_timeout_and_is_retried_for_a_get(self):
        name = fake_kube.worker_pod(1)["metadata"]["name"]
        self.client.create_pod(fake_kube.worker_pod(1))
        self.fake.inject("get_pod", status=HANG)
        client = self.make_client(read_timeout=0.3)
        self.assertEqual(client.get_pod(name)["metadata"]["name"], name)
        self.assertEqual(self.sleeps, [1])
        self.assertTrue(any("(timeout)" in line for line in self.logs.lines))

    def test_a_post_that_times_out_after_it_was_sent_is_not_repeated(self):
        self.fake.inject("create_pod", status=HANG)
        client = self.make_client(read_timeout=0.3)
        with self.assertRaises(kube.Unavailable) as caught:
            client.create_pod(fake_kube.worker_pod(1))
        self.assertTrue(caught.exception.outcome_unknown)
        self.assertEqual(len(self.fake.calls("create_pod")), 1)


# --- Nothing leaks -------------------------------------------------------------------------------------------


class NoLeakTest(KubeCase):
    def poisoned_pod(self):
        pod = fake_kube.worker_pod(1)
        pod["metadata"]["annotations"]["note"] = CANARY_SPEC
        return pod

    def test_no_failure_carries_a_response_body_a_pod_spec_or_a_token(self):
        failing = (None, 400, 401, 403, 404, 405, 409, 413, 422, 429, 500, 502, 503)
        for status in failing:
            self.logs.lines.clear()
            pod = self.poisoned_pod()
            body = {"message": f"{CANARY_BODY} {CANARY_SPEC} {TOKEN_A}", "details": {"causes": [{"message": CANARY_BODY}]}}
            self.fake.inject("create_pod", times=10, status=status, body=body)
            with self.subTest(status=status), self.assertRaises(kube.KubeError) as caught:
                self.client.create_pod(pod)
            self.assertNoLeak(caught.exception, CANARY_BODY, CANARY_SPEC, TOKEN_A, "CANARYTOKEN")
            self.fake._rules.clear()

    def test_no_read_failure_carries_a_body_either(self):
        for status in (None, 404, 422, 429, 503):
            self.logs.lines.clear()
            self.fake.inject("get_pod", times=10, status=status, body={"message": CANARY_BODY})
            with self.subTest(status=status), self.assertRaises(kube.KubeError) as caught:
                self.client.get_pod(fake_kube.worker_pod(1)["metadata"]["name"])
            self.assertNoLeak(caught.exception, CANARY_BODY, TOKEN_A)
            self.fake._rules.clear()

    def test_a_success_logs_nothing_and_returns_no_secret_value(self):
        created = self.client.create_pod(self.poisoned_pod())
        returned = self.client.create_secret(podspec.build_secret(created, SECRET_TOKEN, SECRET_KEY))
        self.client.get_pod(created["metadata"]["name"])
        self.client.list_pods({pc.label_key("role"): "worker"})
        self.assertEqual(self.logs.lines, [])
        self.assertNotIn(SECRET_KEY, repr(returned))
        self.assertNotIn(SECRET_TOKEN, repr(returned))

    def test_a_bad_manifest_error_does_not_echo_the_manifest(self):
        pod = self.poisoned_pod()
        pod["metadata"]["namespace"] = CANARY_SPEC
        with self.assertRaises(ValueError) as caught:
            self.client.create_pod(pod)
        self.assertNoLeak(caught.exception, CANARY_SPEC)

    def test_a_secret_that_cannot_be_encoded_does_not_echo_its_value(self):
        with self.assertRaises(ValueError) as caught:
            podspec.build_secret({"metadata": {"name": fake_kube.worker_pod(1)["metadata"]["name"], "uid": "00000000-0000-4000-8000-000000000001"}}, f"{SECRET_TOKEN} spaced", SECRET_KEY)
        self.assertNoLeak(caught.exception, SECRET_TOKEN)

    def test_the_module_imports_only_the_standard_library_and_its_contract_modules(self):
        tree = ast.parse((ROOT / "src" / "factory" / "kube.py").read_text(encoding="utf-8"))
        names, relative = set(), set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names.add((node.module or "").split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                relative.update(alias.name for alias in node.names)
        self.assertEqual(relative, {"podcontract", "podspec"})
        self.assertLessEqual(names, set(sys.stdlib_module_names))
        self.assertNotIn("random", names)
        self.assertNotIn("urllib.request", names)
        calls = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        self.assertNotIn("print", calls)


# --- Replay of the golden policy mutations through client and fake --------------------------------------------


class AdmissionReplayTest(KubeCase):
    def setUp(self):
        super().setUp()
        self.fake.stop()
        self.fake = FakeKube(clock=self.clock, tokens={TOKEN_A}, admit_image=fake_kube.IMAGE).start()
        self.addCleanup(self.fake.stop)
        self.client = self.make_client()

    def load(self, name):
        return json.loads((GOLDEN / name).read_text(encoding="utf-8"))

    def test_the_golden_pod_and_secret_are_admitted(self):
        created = self.client.create_pod(self.load("pod_standard_build.json"))
        self.assertEqual(created["metadata"]["name"], pc.pod_name("0123456789abcdef0123456789abcdef"))
        self.client.create_secret(self.load("secret_standard_build.json"))
        self.assertEqual(self.fake.calls("create_pod")[0].status, 201)

    def test_the_built_pod_is_admitted_for_every_phase_and_the_hook_is_optional(self):
        for n, phase in enumerate(pc.PHASES, start=1):
            self.client.create_pod(fake_kube.worker_pod(n, phase=phase))
        self.assertEqual(len(self.fake.pods()), len(pc.PHASES))

    def test_every_pod_mutation_is_refused_and_named(self):
        data = self.load("policy_mutations.json")
        golden = self.load(data["golden"])
        denied = refused = 0
        for mutation in data["mutations"]:
            self.assertTrue(mutation["must_deny"])
            pod = podspec.apply_patch(golden, mutation["patch"])
            with self.subTest(mutation=mutation["id"]):
                try:
                    self.client.create_pod(pod)
                except ValueError:
                    refused += 1  # the client refuses it before sending (a name or a namespace that is not the contract's)
                except kube.Denied as caught:
                    denied += 1
                    self.assertIn(mutation["rule"], caught.rules)
                else:
                    self.fail("the mutation was admitted")
        self.assertGreater(denied, len(data["mutations"]) // 2)
        self.assertEqual(denied + refused, len(data["mutations"]))
        self.assertEqual(self.fake.pods(), {})

    def test_every_secret_mutation_is_refused_and_named(self):
        data = self.load("policy_secret_mutations.json")
        golden = self.load(data["golden"])
        denied = refused = 0
        for mutation in data["mutations"]:
            secret = podspec.apply_patch(golden, mutation["patch"])
            with self.subTest(mutation=mutation["id"]):
                try:
                    self.client.create_secret(secret)
                except ValueError:
                    refused += 1  # refused before sending, like the pods
                except kube.Denied as caught:
                    denied += 1
                    # The fake takes the pod name from the owner reference (all a policy can compare the
                    # Secret with), so a change of the name or of the owner surfaces as either rule.
                    related = {"secret-name", "secret-owner"} if mutation["rule"] in ("secret-name", "secret-owner") else set()
                    self.assertTrue({mutation["rule"], *related} & set(caught.rules), caught.rules)
                else:
                    self.fail("the mutation was admitted")
        self.assertGreater(denied, 0)
        self.assertEqual(denied + refused, len(data["mutations"]))
        self.assertEqual(self.fake.secrets(), {})

    def test_a_pod_the_policy_refuses_is_admitted_when_the_hook_is_off(self):
        data = self.load("policy_mutations.json")
        host_path = next(m for m in data["mutations"] if m["rule"] == "host-path")
        pod = podspec.apply_patch(self.load(data["golden"]), host_path["patch"])
        with self.assertRaises(kube.Denied):
            self.client.create_pod(pod)
        self.fake.admit_image = None
        self.client.create_pod(pod)

    def test_a_denial_is_the_403_of_the_policy_with_the_rule_ids_as_rule_tags(self):
        data = self.load("policy_mutations.json")
        privileged = next(m for m in data["mutations"] if m["rule"] == "privileged")
        pod = podspec.apply_patch(self.load(data["golden"]), privileged["patch"])
        with self.assertRaises(kube.Denied) as caught:
            self.client.create_pod(pod)
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(self.fake.calls("create_pod")[-1].status, 403)
        self.assertEqual(len(self.fake.calls("create_pod")), 1, "a denial is not retried")


# --- The fake itself ------------------------------------------------------------------------------------------


class FakeServerTest(KubeCase):
    def raw(self, method, path, token=TOKEN_A, body=None):
        host, port = self.fake.url.removeprefix("http://").split(":")
        connection = http.client.HTTPConnection(host, int(port), timeout=5)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=headers)
        status = connection.getresponse().status
        connection.close()
        return status

    def test_the_fake_refuses_what_a_role_without_the_verb_would(self):
        base = "/api/v1/namespaces/factory-workers"
        name = fake_kube.worker_pod(1)["metadata"]["name"]
        for method, path in (
            ("GET", f"{base}/pods?watch=true"),
            ("GET", f"{base}/pods/{name}/log"),
            ("POST", f"{base}/pods/{name}/exec"),
            ("GET", f"{base}/secrets/{name}-s"),
            ("GET", f"{base}/secrets"),
            ("PATCH", f"{base}/pods/{name}"),
            ("GET", "/api/v1/nodes"),
            ("GET", "/healthz"),
        ):
            with self.subTest(method=method, path=path):
                self.assertEqual(self.raw(method, path), 403)
        self.assertEqual({record.op for record in self.fake.calls()}, {"unsupported"})

    def test_the_fake_authenticates_and_wants_json(self):
        base = "/api/v1/namespaces/factory-workers/pods"
        self.assertEqual(self.raw("GET", base, token=None), 401)
        self.assertEqual(self.raw("GET", base, token=TOKEN_B), 401)
        self.assertEqual(self.raw("GET", base), 200)
        self.assertEqual(self.raw("POST", base, body=b"{}"), 400)
        host, port = self.fake.url.removeprefix("http://").split(":")
        connection = http.client.HTTPConnection(host, int(port), timeout=5)
        connection.request("POST", base, body=b"{}", headers={"Authorization": f"Bearer {TOKEN_A}", "Content-Type": "text/plain"})
        self.assertEqual(connection.getresponse().status, 415)
        connection.close()

    def test_selector_matching(self):
        labels = {"a": "1", "b": "2"}
        for selector, expected in (
            ("", True),
            ("a=1", True),
            ("a==1", True),
            ("a=2", False),
            ("c=1", False),
            ("a!=2", True),
            ("c!=1", True),
            ("a!=1", False),
            ("a in (1,3)", True),
            ("a in (2,3)", False),
            ("c in (1)", False),
            ("a notin (2)", True),
            ("a notin (1)", False),
            ("c notin (1)", True),
            ("a", True),
            ("c", False),
            ("!c", True),
            ("!a", False),
            ("a=1,b=2", True),
            ("a=1,b=3", False),
            ("a in (1,2),b notin (3)", True),
        ):
            with self.subTest(selector=selector):
                self.assertEqual(match_selector(selector, labels), expected)
        for bad in ("a=", "a b", "a in 1", "=1", "a=1=2"):
            if bad == "a=":
                continue  # an empty value is allowed by the grammar
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                match_selector(bad, labels)

    def test_a_malformed_selector_is_a_400_of_the_server(self):
        self.assertEqual(self.raw("GET", "/api/v1/namespaces/factory-workers/pods?labelSelector=a%20b"), 400)


if __name__ == "__main__":
    unittest.main()

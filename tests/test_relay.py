"""The relay of the pod backend (src/factory/relay.py), contract-tested with an in-process client.

Most tests call `Relay.handle` directly with an in-memory body: no socket, no clock, no sleeps. A fake clock
drives expiry, retention and the total-time cap. A few tests put a real `RelayServer` on 127.0.0.1 to prove the
transport (thread bound, slow clients, fixed error bodies); they synchronise on real I/O, never on time.
Nothing here talks to a cluster; every token below is an obviously fake value.
"""

import ast
import contextlib
import dataclasses
import hashlib
import hmac
import http.client
import io
import json
import os
import shlex
import socket
import stat
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from factory import podcontract as pc
from factory import relay
from factory import sessionrecord as sr

UID = "11111111-2222-3333-4444-555555555555"
OTHER_UID = "99999999-2222-3333-4444-555555555555"
INSTANCE = "abcdef012345"
REPO = pc.repo_key("octo/widgets")
POISON = "poison-token-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
SECRET_TEXT = "SECRET-AGENT-TEXT-do-not-leak"
SLICE = 64 * 1024
BUNDLE = bytes(range(256)) * 700  # 179,200 bytes: three slices of the relay's copy size
REPORT = b'{"outcome":"exited","exit_code":0}'


def result_body(report=REPORT, delta=b""):
    return b"".join([struct.pack(">I", len(report)), report, delta])


class FakeClock:
    def __init__(self):
        self.now = time.time()

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class HookReader:
    """A request body that runs `hook` before its first read (a request made while this one is in flight)."""

    def __init__(self, data, hook):
        self._data = io.BytesIO(data)
        self._hook = hook
        self._called = False

    def read(self, size):
        if not self._called:
            self._called = True
            self._hook()
        return self._data.read(size)


class ForbiddenReader:
    """A body that must never be read: the request is refused before its body matters."""

    def read(self, size):
        raise AssertionError("the body was read")


class SpyReader:
    def __init__(self, data):
        self._data = io.BytesIO(data)
        self.largest = 0

    def read(self, size):
        self.largest = max(self.largest, size)
        return self._data.read(size)


def snapshot(base):
    """Every path under `base` with its size and modification time: equal snapshots mean nothing was touched."""
    found = []
    for directory, _, names in os.walk(base):
        found.append((directory, os.lstat(directory).st_mtime_ns))
        for name in names:
            info = os.lstat(os.path.join(directory, name))
            found.append((os.path.join(directory, name), info.st_size, info.st_mtime_ns))
    return sorted(found)


class Pod:
    """A client with one session's credentials, calling `Relay.handle` with an in-memory body."""

    def __init__(self, case, session, token, uid=UID):
        self.case = case
        self.session = session
        self.token = token
        self.uid = uid

    @property
    def relay(self):
        return self.case.relay

    def headers(self):
        return {
            "Authorization": f"Bearer {self.token}",
            relay.HEADER_POD_NAME: pc.pod_name(self.session),
            relay.HEADER_POD_UID: self.uid,
        }

    def send(self, method, path, data=b"", headers=None, drop=(), length="auto", reader=None):
        merged = {key: value for key, value in {**self.headers(), **(headers or {})}.items() if key not in drop}
        declared = (len(data) if method == "POST" else None) if length == "auto" else length
        source = io.BytesIO(data) if reader is None else reader
        return self.relay.handle(method, path, merged, declared, relay.Body(source, declared or 0))

    def call(self, method, path, data=b"", **keywords):
        reply = self.send(method, path, data, **keywords)
        if reply.download is not None:
            reply.download.close()
            return reply.status, None
        return reply.status, json.loads(reply.body)

    def heartbeat(self):
        return self.call("POST", "/v1/heartbeat")

    def preflight(self, result="ok", code=None):
        document = {"result": result}
        if code is not None:
            document["code"] = code
        return self.call("POST", "/v1/preflight", json.dumps(document).encode("ascii"))

    def fetch(self, drop_after=None):
        """(status, bytes, completed): download the bundle, optionally dropping the connection after some bytes."""
        reply = self.send("GET", "/v1/bundle")
        if reply.download is None:
            return reply.status, None, False
        chunks = []

        def write(data):
            if drop_after is not None and sum(len(chunk) for chunk in chunks) + len(data) > drop_after:
                raise BrokenPipeError("the client dropped")
            chunks.append(data)

        try:
            self.relay.pump(reply.download, write)
        except BrokenPipeError:
            return 200, b"".join(chunks), False
        return 200, b"".join(chunks), True

    def chunk(self, stream, data):
        return self.call("POST", f"/v1/stream/{stream}", data)

    def finish(self, report=REPORT, delta=b""):
        return self.call("POST", "/v1/result", result_body(report, delta))

    def stored(self):
        return sr.read_state(self.case.root, self.session)["state"]

    def reject(self):
        return sr.read_state(self.case.root, self.session)["reject"]

    def directory(self):
        return self.case.root / self.session

    def out(self, name):
        return (self.directory() / sr.OUT_DIR / name).read_bytes()

    def to_fetched(self):
        self.case.assertEqual(self.preflight()[0], 200)
        self.case.assertTrue(self.fetch()[2])
        return self

    def to_streaming(self):
        self.to_fetched()
        self.case.assertEqual(self.chunk("transcript", b"x")[0], 200)
        return self


class SpoolCase(unittest.TestCase):
    limits = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "spool"
        self.root.mkdir()
        os.chown(self.root, -1, os.getgid())
        os.chmod(self.root, relay.SPOOL_MODE)
        self.clock = FakeClock()
        self.relay = relay.Relay(self.root, clock=self.clock, limits=self.limits, owner=(os.getuid(), os.getgid()))
        self.addCleanup(self.relay.close)

    def make_session(self, bound=True, deadline_in=3600, bundle=BUNDLE, caps=None, uid=UID):
        session = pc.new_session_id()
        token = sr.new_token()
        document = sr.build(session, token, int(self.clock.time()) + deadline_in, REPO, "cwd", INSTANCE, caps=caps)
        sr.write(self.root, document)
        if bundle is not None:
            with sr.session_dir(self.root, session) as fd:
                in_fd = sr.ensure_dir_at(fd, sr.IN_DIR)
                handle = os.open(sr.BUNDLE_FILE, os.O_WRONLY | os.O_CREAT, sr.FILE_MODE, dir_fd=in_fd)
                os.write(handle, bundle)
                os.close(handle)
                os.close(in_fd)
        if bound:
            sr.bind(self.root, session, pc.pod_name(session), uid)
        return Pod(self, session, token, uid)

    def bind(self, pod):
        sr.bind(self.root, pod.session, pc.pod_name(pod.session), pod.uid)

    def set_limits(self, **changes):
        self.relay.limits = dataclasses.replace(self.relay.limits, **changes)

    def usage(self):
        return self.relay._spool_usage()


def caps(**changes):
    return dict(sr.default_caps(), **changes)


class Authentication(SpoolCase):
    def test_a_request_missing_any_credential_is_unauthorized(self):
        pod = self.make_session()
        for name in ("Authorization", relay.HEADER_POD_NAME, relay.HEADER_POD_UID):
            with self.subTest(missing=name):
                self.assertEqual(pod.call("POST", "/v1/heartbeat", drop=(name,))[0], 401)

    def test_malformed_credentials_are_unauthorized(self):
        pod = self.make_session()
        authorizations = (
            "",
            "Bearer",
            "Bearer ",
            "Bearer short",
            f"Basic {pod.token}",
            f"Bearer {'x' * 129}",
            f"Bearer {pod.token} extra",
            f"Bearer {pod.token}\n",
            f"Bearer é{pod.token}",
            pod.token,
        )
        for value in authorizations:
            with self.subTest(authorization=value[:30]):
                self.assertEqual(pod.call("POST", "/v1/heartbeat", headers={"Authorization": value})[0], 401)
        for uid in ("", "UPPER-CASE-UID-1234", "short", "../../etc/passwd", f"{UID}\n"):
            with self.subTest(uid=uid):
                self.assertEqual(pod.call("POST", "/v1/heartbeat", headers={relay.HEADER_POD_UID: uid})[0], 401)

    def test_the_scheme_is_case_insensitive_and_header_names_are_too(self):
        pod = self.make_session()
        headers = {key.lower(): value for key, value in pod.headers().items()}
        headers["authorization"] = f"bearer {pod.token}"
        reply = self.relay.handle("POST", "/v1/heartbeat", headers, 0, relay.Body(io.BytesIO(b""), 0))
        self.assertEqual(reply.status, 200)

    def test_a_pod_name_that_is_not_a_session_never_reaches_the_file_system(self):
        pod = self.make_session()
        decoy = self.base / "decoy"
        (decoy / pod.session).mkdir(parents=True)
        before = snapshot(self.base)
        session = pod.session
        names = (
            "factory-w-../../../etc/passwd",
            f"factory-w-{session.upper()}",
            f"factory-w-{session[:-1]}",
            f"factory-w-{session}0",
            f"factory-w-{session}/../x",
            f"factory-w-{session}\x00",
            "factory-w-",
            f"../{pc.pod_name(session)}",
            f" {pc.pod_name(session)}",
            f"x{pc.pod_name(session)}",
            session,
        )
        with mock.patch.object(sr, "open_session_at", side_effect=AssertionError("opened")):
            for name in names:
                with self.subTest(name=name):
                    status, _ = pod.call("POST", "/v1/heartbeat", headers={relay.HEADER_POD_NAME: name})
                    self.assertEqual(status, 401)
        self.assertEqual(snapshot(self.base), before)

    def test_a_wrong_token_is_unauthorized_and_does_not_use_up_the_real_one(self):
        pod = self.make_session()
        before = sr.read_state(self.root, pod.session)
        wrong = sr.new_token()
        for path, method in (("/v1/heartbeat", "POST"), ("/v1/preflight", "POST"), ("/v1/bundle", "GET")):
            with self.subTest(path=path):
                status, _ = pod.call(method, path, headers={"Authorization": f"Bearer {wrong}"})
                self.assertEqual(status, 401)
        self.assertEqual(sr.read_state(self.root, pod.session), before)
        self.assertEqual(pod.preflight()[0], 200)

    def test_a_wrong_pod_uid_is_forbidden_and_the_right_one_still_works(self):
        pod = self.make_session()
        impostor = Pod(self, pod.session, pod.token, OTHER_UID)
        self.assertEqual(impostor.heartbeat(), (403, {"error": "identity"}))
        self.assertEqual(impostor.preflight()[0], 403)
        self.assertEqual(pod.heartbeat()[0], 200)

    def test_tokens_and_uids_are_compared_in_constant_time_and_only_as_hashes(self):
        pod = self.make_session()
        seen = []
        real = hmac.compare_digest

        def spy(first, second):
            seen.append((first, second))
            return real(first, second)

        with mock.patch.object(relay.hmac, "compare_digest", spy):
            self.assertEqual(pod.heartbeat()[0], 200)
        flat = b" ".join(item for pair in seen for item in pair)
        self.assertGreaterEqual(len(seen), 2)
        self.assertIn(hashlib.sha256(pod.token.encode("ascii")).hexdigest().encode("ascii"), flat)
        self.assertIn(UID.encode("ascii"), flat)
        self.assertNotIn(pod.token.encode("ascii"), flat)

    def test_one_sessions_credentials_never_open_another_session(self):
        first, second = self.make_session(), self.make_session()
        first.to_fetched()
        second.to_fetched()
        crossed = {relay.HEADER_POD_NAME: pc.pod_name(second.session)}
        self.assertEqual(first.chunk("transcript", b"a")[0], 200)
        self.assertEqual(first.call("POST", "/v1/stream/transcript", b"a", headers=crossed)[0], 401)
        wrong_uid = Pod(self, first.session, first.token, OTHER_UID)
        self.assertEqual(wrong_uid.chunk("transcript", b"a")[0], 403)
        self.assertEqual(second.chunk("transcript", b"b")[0], 200)
        self.assertEqual((first.out(sr.OUT_TRANSCRIPT), second.out(sr.OUT_TRANSCRIPT)), (b"a", b"b"))

    def test_two_sessions_served_at_the_same_time_cannot_read_each_other(self):
        pods = [self.make_session(bundle=bytes([index]) * 200_000) for index in range(2)]
        outcomes = {}

        def run(index):
            pod = pods[index]
            pod.preflight()
            outcomes[index] = (pod.fetch(), [pod.chunk("transcript", bytes([65 + index]) * 1000)[0] for _ in range(5)])

        threads = [threading.Thread(target=run, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        for index, pod in enumerate(pods):
            with self.subTest(index=index):
                (status, data, complete), statuses = outcomes[index]
                self.assertEqual((status, complete, set(data), statuses), (200, True, {index}, [200] * 5))
                self.assertEqual(set(pod.out(sr.OUT_TRANSCRIPT)), {65 + index})

    def test_a_session_without_a_directory_answers_gone_before_the_token_matters(self):
        ghost = Pod(self, pc.new_session_id(), sr.new_token())
        for method, path in (
            ("GET", "/v1/bundle"),
            ("POST", "/v1/preflight"),
            ("POST", "/v1/heartbeat"),
            ("POST", "/v1/result"),
            ("POST", "/v1/stream/transcript"),
            ("POST", "/v1/stream/stderr"),
        ):
            with self.subTest(path=path):
                self.assertEqual(ghost.call(method, path, reader=ForbiddenReader()), (410, {"error": "gone"}))

    def test_a_known_session_that_is_not_bound_yet_answers_too_early_with_the_right_token_only(self):
        pod = self.make_session(bound=False)
        for method, path in (
            ("GET", "/v1/bundle"),
            ("POST", "/v1/preflight"),
            ("POST", "/v1/heartbeat"),
            ("POST", "/v1/result"),
            ("POST", "/v1/stream/transcript"),
        ):
            with self.subTest(path=path):
                self.assertEqual(pod.call(method, path, b'{"result":"ok"}')[0], 425)
        wrong = pod.call("POST", "/v1/heartbeat", headers={"Authorization": f"Bearer {sr.new_token()}"})
        self.assertEqual(wrong[0], 401)
        self.bind(pod)
        self.assertEqual(pod.heartbeat()[0], 200)

    def test_a_record_that_is_not_valid_answers_gone(self):
        pod = self.make_session()
        (pod.directory() / sr.RECORD_FILE).write_text('{"version": 1}', encoding="utf-8")
        self.assertEqual(pod.heartbeat()[0], 410)
        (pod.directory() / sr.RECORD_FILE).write_bytes(b"not json at all")
        self.assertEqual(pod.heartbeat()[0], 410)

    def test_a_state_file_that_is_not_valid_fails_closed(self):
        pod = self.make_session()
        (pod.directory() / sr.STATE_FILE).write_text('{"state": "streaming"}', encoding="utf-8")
        self.assertEqual(pod.heartbeat()[0], 410)
        self.assertEqual(pod.preflight()[0], 410)


class StateMachine(SpoolCase):
    def test_a_session_walks_the_states_in_order_and_each_is_stored(self):
        pod = self.make_session(bound=False)
        self.assertEqual(pod.stored(), "issued")
        self.assertEqual(pod.heartbeat()[0], 425)
        self.bind(pod)
        self.assertEqual(pod.heartbeat(), (200, {"state": "bound"}))
        self.assertEqual(pod.preflight(), (200, {"state": "preflighted"}))
        self.assertEqual(pod.stored(), "preflighted")
        status, data, complete = pod.fetch()
        self.assertEqual((status, data, complete), (200, BUNDLE, True))
        self.assertEqual(pod.stored(), "fetched")
        self.assertEqual(pod.chunk("transcript", b"line one\n"), (200, {"state": "streaming"}))
        self.assertEqual(pod.stored(), "streaming")
        self.assertEqual(pod.finish(), (200, {"state": "finished"}))
        state = sr.read_state(self.root, pod.session)
        self.assertEqual((state["state"], state["reject"], state["ended_at"]), ("finished", None, int(self.clock.time())))

    def test_calls_out_of_order_are_refused_and_change_nothing(self):
        pod = self.make_session()
        for method, path, data in (
            ("GET", "/v1/bundle", b""),
            ("POST", "/v1/stream/transcript", b"x"),
            ("POST", "/v1/result", result_body()),
        ):
            with self.subTest(early=path):
                self.assertEqual(pod.call(method, path, data), (409, {"error": "order"}))
        self.assertEqual(pod.stored(), "issued")
        pod.preflight()
        self.assertEqual(pod.chunk("transcript", b"x")[0], 409)
        self.assertEqual(pod.finish()[0], 409)
        pod.fetch()
        self.assertEqual(pod.preflight()[0], 409)
        self.assertEqual(pod.stored(), "fetched")

    def test_a_repeated_good_preflight_is_accepted_before_the_fetch_only(self):
        pod = self.make_session()
        self.assertEqual(pod.preflight()[0], 200)
        self.assertEqual(pod.preflight()[0], 200)
        self.assertEqual(pod.preflight("failed", "canary")[0], 409)
        self.assertEqual(pod.stored(), "preflighted")

    def test_a_failed_preflight_ends_the_session_and_the_bundle_is_never_served(self):
        pod = self.make_session()
        self.assertEqual(pod.preflight("failed", "api_reachable"), (200, {"state": "finished"}))
        state = sr.read_state(self.root, pod.session)
        self.assertEqual((state["state"], state["reject"], state["preflight_code"]), ("finished", "preflight_failed", "api_reachable"))
        self.assertEqual(pod.call("GET", "/v1/bundle")[0], 410)
        self.assertEqual(pod.fetch()[0], 410)
        self.assertEqual(pod.heartbeat()[0], 410)
        self.assertEqual(pod.preflight()[0], 410)

    def test_a_preflight_body_that_is_not_the_fixed_shape_is_refused(self):
        pod = self.make_session()
        for body in (
            b"",
            b"not json",
            b"[]",
            b'"ok"',
            b'{"result": "maybe"}',
            b'{"result": "failed"}',
            b'{"result": "failed", "code": "UPPER CASE"}',
            b'{"result": "failed", "code": 5}',
            b"".join([b'{"result": "failed", "code": "', b"x" * 40, b'"}']),
            b'{"result": "ok", "code": "x"}',
            b'{"result": "ok", "extra": 1}',
            b"[" * 3000,
            b"\xff\xfe",
        ):
            with self.subTest(body=body[:30]):
                self.assertEqual(pod.call("POST", "/v1/preflight", body)[0], 400)
        self.assertEqual(pod.stored(), "issued")
        self.assertEqual(pod.call("POST", "/v1/preflight", b"x" * 5000)[0], 413)
        self.assertEqual(pod.call("POST", "/v1/preflight", length=None)[0], 411)

    def test_the_bundle_is_fetched_once_a_second_complete_fetch_is_gone(self):
        pod = self.make_session()
        pod.preflight()
        self.assertEqual(pod.fetch(), (200, BUNDLE, True))
        self.assertEqual(pod.fetch()[0], 410)
        pod.chunk("transcript", b"x")
        self.assertEqual(pod.fetch()[0], 410)

    def test_a_download_dropped_half_way_may_be_retried_and_counts_as_used_only_when_complete(self):
        pod = self.make_session()
        pod.preflight()
        status, data, complete = pod.fetch(drop_after=SLICE)
        self.assertEqual((status, len(data), complete), (200, SLICE, False))
        self.assertEqual(pod.stored(), "preflighted")
        self.assertEqual(sr.read_state(self.root, pod.session)["fetch_attempts"], 1)
        self.assertEqual(pod.fetch(), (200, BUNDLE, True))
        self.assertEqual(pod.stored(), "fetched")
        self.assertEqual(pod.fetch()[0], 410)

    def test_the_last_byte_decides_whether_a_fetch_is_complete(self):
        pod = self.make_session()
        pod.preflight()
        status, _, complete = pod.fetch(drop_after=len(BUNDLE) - 1)
        self.assertEqual((status, complete, pod.stored()), (200, False, "preflighted"))
        self.assertEqual(pod.fetch(drop_after=len(BUNDLE))[2], True)

    def test_the_pods_next_call_may_arrive_the_moment_it_has_the_last_byte(self):
        pod = self.make_session()
        pod.preflight()
        nested = []

        def write(data):
            if len(data) < SLICE:  # the last slice of this bundle is the short one
                nested.append(pod.chunk("transcript", b"first line\n")[0])

        reply = pod.send("GET", "/v1/bundle")
        self.relay.pump(reply.download, write)
        self.assertEqual(nested, [200])
        self.assertEqual(pod.stored(), "streaming")

    def test_a_last_slice_that_fails_puts_the_session_back_so_the_fetch_can_be_retried(self):
        pod = self.make_session()
        pod.preflight()
        seen = []

        def write(data):
            if len(data) < SLICE:
                seen.append(pod.stored())  # recorded before the last slice is written
                raise BrokenPipeError("the client dropped")

        reply = pod.send("GET", "/v1/bundle")
        with self.assertRaises(BrokenPipeError):
            self.relay.pump(reply.download, write)
        self.assertEqual((seen, pod.stored()), (["fetched"], "preflighted"))
        self.assertEqual(pod.fetch(), (200, BUNDLE, True))

    def test_a_client_that_drops_before_the_first_byte_does_not_leave_the_download_claimed(self):
        pod = self.make_session()
        pod.preflight()
        reply = pod.send("GET", "/v1/bundle")
        relay_ = self.relay

        class Dropped:
            """Just enough of a request handler for `_send`: the headers cannot be written."""

            request_version = "HTTP/1.0"
            server = type("Server", (), {"relay": relay_})()

            def send_response_only(self, status):
                raise BrokenPipeError("the client dropped")

        with self.assertRaises(BrokenPipeError):
            relay._Handler._send(Dropped(), reply)
        self.assertEqual(self.relay._busy, set())
        self.assertEqual(pod.stored(), "preflighted")
        self.assertEqual(pod.fetch(), (200, BUNDLE, True))

    def test_a_session_removed_during_its_download_does_not_break_the_transfer(self):
        pod = self.make_session()
        pod.preflight()
        sent = []

        def write(data):
            if not sent:
                sr.mark(self.root, pod.session, sr.MARKER_RELEASED)
                self.assertEqual(self.relay.sweep(), 1)
            sent.append(len(data))

        reply = pod.send("GET", "/v1/bundle")
        self.relay.pump(reply.download, write)
        self.assertEqual(sum(sent), len(BUNDLE))
        self.assertEqual(self.relay._busy, set())

    def test_the_fourth_attempt_is_refused_and_the_session_is_marked_rejected(self):
        pod = self.make_session()
        pod.preflight()
        for attempt in range(sr.MAX_FETCH_ATTEMPTS):
            with self.subTest(attempt=attempt):
                self.assertEqual(pod.fetch(drop_after=10)[:3:2], (200, False))
        self.assertEqual(pod.fetch()[0], 410)
        state = sr.read_state(self.root, pod.session)
        self.assertEqual((state["state"], state["reject"]), ("finished", "bundle_attempts"))

    def test_two_downloads_at_the_same_time_are_refused_the_second_one(self):
        pod = self.make_session()
        pod.preflight()
        first = pod.send("GET", "/v1/bundle")
        self.assertEqual(first.status, 200)
        self.assertEqual(pod.call("GET", "/v1/bundle"), (409, {"error": "busy"}))
        first.download.close()
        self.assertEqual(pod.fetch()[2], True)

    def test_a_bundle_that_is_missing_is_a_server_error_and_does_not_use_an_attempt(self):
        pod = self.make_session(bundle=None)
        pod.preflight()
        self.assertEqual(pod.call("GET", "/v1/bundle"), (500, {"error": "internal"}))
        self.assertEqual(sr.read_state(self.root, pod.session)["fetch_attempts"], 0)

    def test_the_result_is_accepted_once(self):
        pod = self.make_session().to_streaming()
        self.assertEqual(pod.finish(delta=b"delta-bytes")[0], 200)
        self.assertEqual(pod.finish()[0], 410)
        self.assertEqual(pod.out(sr.OUT_DELTA), b"delta-bytes")
        self.assertEqual(pod.out(sr.OUT_REPORT), REPORT)

    def test_a_result_may_follow_the_fetch_directly(self):
        pod = self.make_session().to_fetched()
        self.assertEqual(pod.finish()[0], 200)
        self.assertEqual(pod.out(sr.OUT_DELTA), b"")

    def test_after_the_end_every_call_answers_gone(self):
        pod = self.make_session().to_streaming()
        pod.finish()
        for method, path in (
            ("GET", "/v1/bundle"),
            ("POST", "/v1/preflight"),
            ("POST", "/v1/heartbeat"),
            ("POST", "/v1/result"),
            ("POST", "/v1/stream/transcript"),
            ("POST", "/v1/stream/stderr"),
        ):
            with self.subTest(path=path):
                self.assertEqual(pod.call(method, path, reader=ForbiddenReader()), (410, {"error": "gone"}))

    def test_a_session_that_is_over_answers_gone_before_any_complaint_about_the_request_itself(self):
        pod = self.make_session().to_streaming()
        pod.finish()
        for path in ("/v1/stream/transcript", "/v1/stream/stderr", "/v1/result", "/v1/preflight", "/v1/heartbeat"):
            with self.subTest(path=path):
                self.assertEqual(pod.call("POST", path, length=None, reader=ForbiddenReader())[0], 410)
                self.assertEqual(pod.call("POST", path, length=10**9, reader=ForbiddenReader())[0], 410)

    def test_the_deadline_is_the_expiry_and_it_is_stored(self):
        deadline = int(self.clock.time()) + 100
        pod = self.make_session(deadline_in=100).to_streaming()
        self.clock.advance(99)
        self.assertEqual(pod.heartbeat()[0], 200)
        self.clock.advance(2)
        self.assertEqual(pod.heartbeat()[0], 410)
        state = sr.read_state(self.root, pod.session)
        self.assertEqual((state["state"], state["ended_at"]), ("expired", deadline))
        self.assertEqual(pod.chunk("transcript", b"late")[0], 410)
        self.assertEqual(pod.finish()[0], 410)
        self.assertEqual(pod.out(sr.OUT_TRANSCRIPT), b"x")

    def test_a_session_that_expired_unbound_answers_gone_too(self):
        pod = self.make_session(bound=False, deadline_in=10)
        self.clock.advance(10)
        self.assertEqual(pod.heartbeat()[0], 410)

    def test_the_revoke_marker_ends_the_session_for_every_call_from_then_on(self):
        pod = self.make_session().to_streaming()
        sr.mark(self.root, pod.session, sr.MARKER_REVOKE)
        for call in (pod.heartbeat, pod.preflight, lambda: pod.chunk("transcript", b"x"), pod.finish):
            self.assertEqual(call()[0], 410)
        self.assertEqual(pod.stored(), "revoked")
        self.assertEqual(pod.out(sr.OUT_TRANSCRIPT), b"x")

    def test_a_session_revoked_before_the_pod_was_bound_answers_gone_not_too_early(self):
        pod = self.make_session(bound=False)
        sr.mark(self.root, pod.session, sr.MARKER_REVOKE)
        self.assertEqual(pod.heartbeat()[0], 410)

    def test_the_released_marker_answers_gone(self):
        pod = self.make_session()
        sr.mark(self.root, pod.session, sr.MARKER_RELEASED)
        self.assertEqual(pod.heartbeat()[0], 410)
        self.assertEqual(pod.preflight()[0], 410)

    def test_an_end_state_is_not_overwritten_by_a_later_revoke_or_expiry(self):
        pod = self.make_session(deadline_in=50).to_streaming()
        pod.finish()
        ended = sr.read_state(self.root, pod.session)["ended_at"]
        sr.mark(self.root, pod.session, sr.MARKER_REVOKE)
        self.clock.advance(500)
        self.assertEqual(pod.heartbeat()[0], 410)
        state = sr.read_state(self.root, pod.session)
        self.assertEqual((state["state"], state["ended_at"]), ("finished", ended))

    def test_the_state_survives_a_restart_and_a_replay_is_still_refused(self):
        pod = self.make_session()
        pod.preflight()
        pod.fetch()
        self.relay = relay.Relay(self.root, clock=self.clock, owner=(os.getuid(), os.getgid()))
        self.addCleanup(self.relay.close)
        self.assertEqual(pod.stored(), "fetched")
        self.assertEqual(pod.fetch()[0], 410)
        self.assertEqual(pod.chunk("transcript", b"after the restart")[0], 200)

    def test_a_relay_started_on_an_empty_spool_answers_gone_so_the_pod_ends(self):
        pod = self.make_session().to_streaming()
        fresh = SpoolCase("setUp")
        fresh.setUp()
        self.addCleanup(fresh.tmp.cleanup)
        self.addCleanup(fresh.relay.close)
        reply = fresh.relay.handle("POST", "/v1/heartbeat", pod.headers(), 0, relay.Body(io.BytesIO(b""), 0))
        self.assertEqual(reply.status, 410)

    def test_the_heartbeat_file_is_an_integer_and_a_stream_chunk_counts_as_one(self):
        pod = self.make_session().to_fetched()
        self.assertIsNone(sr.read_heartbeat(self.root, pod.session))
        pod.heartbeat()
        self.assertEqual(sr.read_heartbeat(self.root, pod.session), int(self.clock.time()))
        self.assertEqual((pod.directory() / sr.HEARTBEAT_FILE).read_text(encoding="ascii"), f"{int(self.clock.time())}\n")
        self.clock.advance(31)
        pod.chunk("stderr", b"warning")
        self.assertEqual(sr.read_heartbeat(self.root, pod.session), int(self.clock.time()))
        self.clock.advance(31)
        pod.finish()
        self.assertEqual(sr.read_heartbeat(self.root, pod.session), int(self.clock.time()))

    def test_a_refused_call_writes_no_heartbeat(self):
        pod = self.make_session(bound=False)
        pod.heartbeat()
        impostor = Pod(self, pod.session, pod.token, OTHER_UID)
        self.bind(pod)
        impostor.heartbeat()
        self.assertIsNone(sr.read_heartbeat(self.root, pod.session))

    def test_a_failed_state_write_keeps_the_old_state_valid_and_leaves_no_temporary_file(self):
        pod = self.make_session()
        before = (pod.directory() / sr.STATE_FILE).read_bytes() if (pod.directory() / sr.STATE_FILE).exists() else None
        with mock.patch.object(sr.os, "replace", side_effect=OSError("disk full")), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(pod.preflight(), (500, {"error": "internal"}))
        self.assertEqual(
            (pod.directory() / sr.STATE_FILE).read_bytes() if (pod.directory() / sr.STATE_FILE).exists() else None, before
        )
        self.assertFalse([name for name in os.listdir(pod.directory()) if name.endswith(".tmp")])
        self.assertEqual(pod.preflight(), (200, {"state": "preflighted"}))
        self.assertEqual(pod.stored(), "preflighted")

    def test_concurrent_calls_on_one_session_keep_a_consistent_state(self):
        pod = self.make_session().to_fetched()
        statuses = []

        def stream(index):
            statuses.append(pod.chunk("transcript", bytes([65 + index]) * 100)[0])

        threads = [threading.Thread(target=stream, args=(index,)) for index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(statuses.count(200) + statuses.count(409), 6)
        data = pod.out(sr.OUT_TRANSCRIPT)
        self.assertEqual(len(data), 100 * statuses.count(200))
        self.assertEqual(pod.stored(), "streaming")


class Caps(SpoolCase):
    def test_the_transcript_cap_cuts_a_chunk_off_whole_and_ends_the_session(self):
        pod = self.make_session(caps=caps(transcript_bytes=10)).to_fetched()
        self.assertEqual(pod.chunk("transcript", b"123456")[0], 200)
        self.assertEqual(pod.chunk("transcript", b"abcdef"), (413, {"error": "transcript_cap"}))
        self.assertEqual(pod.out(sr.OUT_TRANSCRIPT), b"123456")
        state = sr.read_state(self.root, pod.session)
        self.assertEqual((state["state"], state["reject"]), ("finished", "transcript_cap"))
        self.assertEqual(pod.chunk("transcript", b"a")[0], 410)

    def test_a_chunk_that_fills_the_cap_exactly_is_accepted(self):
        pod = self.make_session(caps=caps(transcript_bytes=10, stderr_bytes=4)).to_fetched()
        self.assertEqual(pod.chunk("transcript", b"0123456789")[0], 200)
        self.assertEqual(pod.chunk("stderr", b"abcd")[0], 200)
        self.assertEqual(pod.chunk("transcript", b"x")[0], 413)

    def test_the_stderr_cap_is_its_own(self):
        pod = self.make_session(caps=caps(stderr_bytes=3)).to_fetched()
        self.assertEqual(pod.chunk("transcript", b"plenty of room here")[0], 200)
        self.assertEqual(pod.chunk("stderr", b"abcd"), (413, {"error": "stderr_cap"}))
        self.assertEqual(pod.reject(), "stderr_cap")

    def test_an_over_cap_chunk_is_refused_without_reading_its_body(self):
        pod = self.make_session(caps=caps(transcript_bytes=5)).to_fetched()
        self.assertEqual(pod.call("POST", "/v1/stream/transcript", b"x" * 6, reader=ForbiddenReader())[0], 413)

    def test_a_chunk_over_the_request_limit_is_refused_but_the_session_goes_on(self):
        self.set_limits(chunk_max_bytes=8)
        pod = self.make_session().to_fetched()
        self.assertEqual(pod.chunk("transcript", b"123456789"), (413, {"error": "chunk_too_large"}))
        self.assertIsNone(pod.reject())
        self.assertEqual(pod.chunk("transcript", b"12345678")[0], 200)

    def test_a_stream_chunk_needs_a_length(self):
        pod = self.make_session().to_fetched()
        self.assertEqual(pod.call("POST", "/v1/stream/transcript", b"x", length=None)[0], 411)
        self.assertEqual(pod.call("POST", "/v1/result", b"x", length=None)[0], 411)

    def test_a_chunk_is_all_or_nothing_when_the_client_goes_away_half_way(self):
        pod = self.make_session().to_fetched()
        pod.chunk("transcript", b"first chunk\n")
        with self.assertRaises(relay.ClientGone):
            pod.send("POST", "/v1/stream/transcript", b"half a chunk", length=100)
        self.assertEqual(pod.out(sr.OUT_TRANSCRIPT), b"first chunk\n")
        self.assertEqual(pod.chunk("transcript", b"second\n")[0], 200)
        self.assertEqual(pod.out(sr.OUT_TRANSCRIPT), b"first chunk\nsecond\n")

    def test_a_chunk_is_copied_in_bounded_slices(self):
        pod = self.make_session().to_fetched()
        spy = SpyReader(b"y" * (3 * 1024 * 1024))
        self.assertEqual(pod.call("POST", "/v1/stream/transcript", reader=spy, length=3 * 1024 * 1024)[0], 200)
        self.assertLessEqual(spy.largest, SLICE)
        self.assertEqual(len(pod.out(sr.OUT_TRANSCRIPT)), 3 * 1024 * 1024)

    def test_a_bundle_is_served_in_bounded_slices(self):
        pod = self.make_session()
        pod.preflight()
        reply = pod.send("GET", "/v1/bundle")
        largest = []
        real = reply.download.read

        def read(count):
            largest.append(count)
            return real(count)

        reply.download.read = read
        self.relay.pump(reply.download, lambda data: None)
        self.assertLessEqual(max(largest), SLICE)

    def test_the_bundle_cap_stops_a_bundle_that_is_too_large_from_being_served(self):
        pod = self.make_session(caps=caps(bundle_bytes=len(BUNDLE) - 1))
        pod.preflight()
        self.assertEqual(pod.call("GET", "/v1/bundle"), (413, {"error": "bundle_cap"}))
        self.assertEqual(pod.reject(), "bundle_cap")
        self.assertEqual(sr.read_state(self.root, pod.session)["fetch_attempts"], 0)
        exact = self.make_session(caps=caps(bundle_bytes=len(BUNDLE)))
        exact.preflight()
        self.assertEqual(exact.fetch()[2], True)

    def test_the_delta_cap_ends_the_session_and_no_delta_is_kept(self):
        pod = self.make_session(caps=caps(delta_bytes=5)).to_streaming()
        self.assertEqual(pod.finish(delta=b"123456"), (413, {"error": "delta_cap"}))
        self.assertEqual(pod.reject(), "delta_cap")
        self.assertFalse((pod.directory() / sr.OUT_DIR / sr.OUT_DELTA).exists())
        self.assertFalse(any(name.endswith(".part") for name in os.listdir(pod.directory() / sr.OUT_DIR)))
        ok = self.make_session(caps=caps(delta_bytes=5)).to_streaming()
        self.assertEqual(ok.finish(delta=b"12345")[0], 200)

    def test_an_oversized_declared_result_is_refused_before_any_of_it_is_read(self):
        pod = self.make_session(caps=caps(delta_bytes=5)).to_streaming()
        declared = 4 + relay.REPORT_MAX_BYTES + 5 + 1
        self.assertEqual(pod.call("POST", "/v1/result", reader=ForbiddenReader(), length=declared)[0], 413)
        self.assertEqual(pod.reject(), "delta_cap")

    def test_a_report_over_its_cap_ends_the_session(self):
        pod = self.make_session().to_streaming()
        prefix = struct.pack(">I", relay.REPORT_MAX_BYTES + 1)
        body = b"".join([prefix, b"x" * 10])
        self.assertEqual(pod.call("POST", "/v1/result", body), (413, {"error": "report_cap"}))
        self.assertEqual(pod.reject(), "report_cap")

    def test_a_report_that_is_not_a_json_object_is_refused_and_a_good_one_still_works(self):
        pod = self.make_session().to_streaming()
        for report in (b"not json", b"[1]", b'"text"', b"\xff\xfe", b"{"):
            with self.subTest(report=report):
                self.assertEqual(pod.call("POST", "/v1/result", result_body(report, b"d"))[0], 400)
        self.assertEqual(pod.call("POST", "/v1/result", struct.pack(">I", 500))[0], 400)
        self.assertEqual(pod.call("POST", "/v1/result", b"abc")[0], 400)
        self.assertEqual(pod.stored(), "streaming")
        self.assertFalse((pod.directory() / sr.OUT_DIR / sr.OUT_DELTA).exists())
        self.assertEqual(pod.finish(delta=b"d")[0], 200)

    def test_an_upload_cut_short_leaves_no_file_and_no_state_change(self):
        pod = self.make_session().to_streaming()
        with self.assertRaises(relay.ClientGone):
            pod.send("POST", "/v1/result", result_body(delta=b"0123456789"), length=len(result_body(delta=b"0123456789")) + 50)
        self.assertEqual(pod.stored(), "streaming")
        self.assertEqual(sorted(os.listdir(pod.directory() / sr.OUT_DIR)), [sr.OUT_TRANSCRIPT])
        self.assertEqual(pod.finish(delta=b"again")[0], 200)

    def test_a_result_for_a_session_revoked_during_the_upload_is_discarded(self):
        pod = self.make_session().to_streaming()
        hook = lambda: sr.mark(self.root, pod.session, sr.MARKER_REVOKE)
        reader = HookReader(result_body(delta=b"late delta"), hook)
        status, _ = pod.call("POST", "/v1/result", reader=reader, length=len(result_body(delta=b"late delta")))
        self.assertEqual(status, 410)
        self.assertEqual(pod.stored(), "revoked")
        self.assertEqual(sorted(os.listdir(pod.directory() / sr.OUT_DIR)), [sr.OUT_TRANSCRIPT])

    def test_the_spool_refusal_answers_507_and_leaves_the_session_alone(self):
        pod = self.make_session().to_fetched()
        self.set_limits(spool_refuse_bytes=self.usage() + 100)
        self.assertEqual(pod.chunk("transcript", b"a" * 60)[0], 200)
        self.assertEqual(pod.chunk("transcript", b"b" * 60), (507, {"error": "spool_full"}))
        self.assertEqual(pod.out(sr.OUT_TRANSCRIPT), b"a" * 60)
        self.assertIsNone(pod.reject())
        self.assertEqual(pod.heartbeat()[0], 200)
        self.assertEqual(pod.finish(delta=b"c" * 500)[0], 507)

    def test_the_spool_refusal_is_exactly_at_the_limit_a_chunk_that_fills_it_is_accepted_one_byte_more_is_not(self):
        fits, too_big = self.make_session().to_fetched(), self.make_session().to_fetched()
        limit = self.usage() + 60
        self.set_limits(spool_refuse_bytes=limit)
        self.assertEqual(too_big.chunk("transcript", b"b" * 61), (507, {"error": "spool_full"}))
        self.assertEqual(self.usage() + 60, limit)  # the refusal wrote nothing, so the room is still exactly 60 bytes
        self.assertEqual(fits.chunk("transcript", b"a" * 60)[0], 200)
        self.assertEqual(fits.out(sr.OUT_TRANSCRIPT), b"a" * 60)
        self.assertFalse((too_big.directory() / sr.OUT_DIR / sr.OUT_TRANSCRIPT).exists())

    def test_bytes_of_uploads_in_flight_count_against_the_spool_limit(self):
        first, second = self.make_session(bundle=b"b").to_fetched(), self.make_session(bundle=b"b").to_fetched()
        self.set_limits(spool_refuse_bytes=self.usage() + 100)
        nested = []
        hook = lambda: nested.append(second.chunk("transcript", b"n" * 70)[0])
        status, _ = first.call("POST", "/v1/stream/transcript", reader=HookReader(b"f" * 70, hook), length=70)
        self.assertEqual((status, nested), (200, [507]))

    def test_a_second_upload_on_the_same_stream_at_the_same_time_is_refused(self):
        pod = self.make_session().to_fetched()
        nested = []
        hook = lambda: nested.append(pod.chunk("transcript", b"n"))
        pod.call("POST", "/v1/stream/transcript", reader=HookReader(b"f", hook), length=1)
        self.assertEqual(nested, [(409, {"error": "busy"})])
        self.assertEqual(pod.out(sr.OUT_TRANSCRIPT), b"f")

    def test_the_defaults_are_the_contract_numbers_and_the_refusal_is_below_the_size_limit(self):
        limits = relay.Limits()
        self.assertEqual(limits.max_threads, pc.RELAY_MAX_THREADS)
        self.assertEqual(limits.idle_timeout, pc.RELAY_IDLE_TIMEOUT_SECONDS)
        self.assertEqual((limits.max_per_peer, limits.header_timeout), (relay.PEER_MAX_CONNECTIONS, relay.HEADER_TIMEOUT_SECONDS))
        self.assertLess(limits.max_per_peer, limits.max_threads)  # one peer can never take every thread
        self.assertLess(limits.header_timeout, limits.idle_timeout)  # an unauthenticated connection is cut off first
        self.assertEqual(limits.stream_total, pc.RELAY_STREAM_TOTAL_SECONDS)
        self.assertEqual(limits.transfer_total, pc.RELAY_TRANSFER_TOTAL_SECONDS)
        self.assertEqual(limits.retention, pc.SPOOL_RETENTION_SECONDS)
        self.assertEqual(limits.spool_refuse_bytes, pc.SPOOL_REFUSE_BYTES)
        self.assertLessEqual(limits.spool_refuse_bytes * 4, pc.parse_quantity(pc.SPOOL_SIZE_LIMIT) * 3)
        self.assertEqual(limits.total_for("/v1/bundle"), pc.RELAY_TRANSFER_TOTAL_SECONDS)
        self.assertEqual(limits.total_for("/v1/result"), pc.RELAY_TRANSFER_TOTAL_SECONDS)
        for path in ("/v1/heartbeat", "/v1/preflight", "/v1/stream/transcript", "/v1/stream/stderr", "/v1/nothing"):
            self.assertEqual(limits.total_for(path), pc.RELAY_STREAM_TOTAL_SECONDS)
        self.assertEqual(self.relay.limits, relay.Limits())

    def test_the_module_does_not_retype_a_number_the_contract_owns(self):
        tree = ast.parse(Path(relay.__file__).read_text(encoding="utf-8"))
        numbers = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and type(node.value) is int}
        owned = {pc.RELAY_PORT, pc.RELAY_UID, pc.SHARED_GID, pc.SUPERVISOR_UID, pc.WORKER_UID, pc.WORKER_GID}
        owned |= {pc.SPOOL_RETENTION_SECONDS, pc.RELAY_TRANSFER_TOTAL_SECONDS, pc.RELAY_STREAM_TOTAL_SECONDS}
        owned |= {pc.SPOOL_REFUSE_BYTES, pc.DEADLINE_CLAMP_SECONDS, pc.ORPHAN_SILENCE_SECONDS}
        self.assertEqual(numbers & owned, set())


class SpoolRoot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "spool"
        self.root.mkdir()
        os.chown(self.root, -1, os.getgid())
        os.chmod(self.root, relay.SPOOL_MODE)
        self.owner = (os.getuid(), os.getgid())

    def refused(self, owner=None):
        with self.assertRaises(relay.SpoolRefused) as caught:
            relay.Relay(self.root, owner=owner or self.owner)
        return str(caught.exception)

    def test_the_exact_root_is_served(self):
        relay.Relay(self.root, owner=self.owner).close()

    def test_a_root_that_is_not_exactly_the_documented_one_is_refused(self):
        for mode in (0o777, 0o2777, 0o770, 0o2750, 0o2700, 0o2771, 0o2772, 0o2774, 0o3770, 0o4770, 0o755):
            with self.subTest(mode=oct(mode)):
                os.chmod(self.root, mode)
                self.assertEqual(self.refused(), "mode")

    def test_a_root_its_owner_cannot_read_is_refused_whoever_runs_the_test(self):
        # Root reads any directory (CAP_DAC_OVERRIDE) and reaches the mode check; an owner without the read
        # bit cannot even open it. Either way the relay refuses, and it never serves such a root.
        os.chmod(self.root, 0o2070)
        self.assertIn(self.refused(), ("mode", "unreadable"))

    def test_another_owner_or_another_group_is_refused(self):
        self.assertEqual(self.refused(owner=(self.owner[0] + 1, self.owner[1])), "owner")
        self.assertEqual(self.refused(owner=(self.owner[0], self.owner[1] + 1)), "group")

    def test_the_default_owner_is_the_relay_uid_and_the_shared_group(self):
        if (os.getuid(), os.getgid()) == (pc.RELAY_UID, pc.SHARED_GID):
            self.skipTest("the test user is the relay user")
        with self.assertRaises(relay.SpoolRefused):
            relay.Relay(self.root)

    def test_a_link_a_file_or_a_missing_root_is_refused(self):
        link = Path(self.tmp.name) / "link"
        link.symlink_to(self.root)
        with self.assertRaises(relay.SpoolRefused):
            relay.Relay(link, owner=self.owner)
        file = Path(self.tmp.name) / "file"
        file.write_text("x", encoding="utf-8")
        with self.assertRaises(relay.SpoolRefused):
            relay.Relay(file, owner=self.owner)
        with self.assertRaises(relay.SpoolRefused):
            relay.Relay(Path(self.tmp.name) / "missing", owner=self.owner)

    def test_a_root_that_stops_being_right_while_running_makes_every_call_unavailable(self):
        case = SpoolCase("setUp")
        case.setUp()
        self.addCleanup(case.tmp.cleanup)
        self.addCleanup(case.relay.close)
        pod = case.make_session()
        self.assertEqual(pod.heartbeat()[0], 200)
        os.chmod(case.root, 0o2777)
        for method, path in (("POST", "/v1/heartbeat"), ("GET", "/v1/bundle"), ("POST", "/v1/stream/transcript")):
            self.assertEqual(pod.call(method, path), (503, {"error": "unavailable"}))
        os.chmod(case.root, relay.SPOOL_MODE)
        self.assertEqual(pod.heartbeat()[0], 200)


class Retention(SpoolCase):
    def test_a_finished_session_keeps_its_output_until_released_or_the_retention_passes(self):
        pod = self.make_session().to_streaming()
        pod.finish(delta=b"the delta")
        self.assertEqual(self.relay.sweep(), 0)
        self.clock.advance(pc.SPOOL_RETENTION_SECONDS - 5)
        self.assertEqual(self.relay.sweep(), 0)
        self.assertEqual(pod.out(sr.OUT_DELTA), b"the delta")
        self.clock.advance(6)
        self.assertEqual(self.relay.sweep(), 1)
        self.assertFalse(pod.directory().exists())

    def test_the_released_marker_lets_the_relay_remove_a_session_at_once(self):
        pod = self.make_session().to_streaming()
        pod.finish()
        sr.mark(self.root, pod.session, sr.MARKER_RELEASED)
        self.assertEqual(self.relay.sweep(), 1)
        self.assertFalse(pod.directory().exists())

    def test_a_released_session_goes_even_when_it_never_finished(self):
        pod = self.make_session().to_streaming()
        sr.mark(self.root, pod.session, sr.MARKER_RELEASED)
        self.assertEqual(self.relay.sweep(), 1)

    def test_output_survives_expiry_until_the_retention_after_the_deadline(self):
        pod = self.make_session(deadline_in=100).to_streaming()
        self.clock.advance(101)
        self.assertEqual(self.relay.sweep(), 0)
        self.assertEqual(pod.stored(), "expired")
        self.assertEqual(pod.out(sr.OUT_TRANSCRIPT), b"x")
        self.clock.advance(pc.SPOOL_RETENTION_SECONDS - 2)
        self.assertEqual(self.relay.sweep(), 0)
        self.assertEqual(pod.out(sr.OUT_TRANSCRIPT), b"x")
        self.clock.advance(3)
        self.assertEqual(self.relay.sweep(), 1)

    def test_a_revoked_session_is_kept_for_the_retention_after_the_marker(self):
        pod = self.make_session().to_streaming()
        sr.mark(self.root, pod.session, sr.MARKER_REVOKE)
        self.assertEqual(self.relay.sweep(), 0)
        self.assertEqual(pod.stored(), "revoked")
        self.clock.advance(pc.SPOOL_RETENTION_SECONDS + 10)
        self.assertEqual(self.relay.sweep(), 1)

    def test_a_running_session_is_never_removed(self):
        pod = self.make_session(deadline_in=10 * pc.SPOOL_RETENTION_SECONDS).to_streaming()
        self.clock.advance(5 * pc.SPOOL_RETENTION_SECONDS)
        self.assertEqual(self.relay.sweep(), 0)
        self.assertEqual(pod.heartbeat()[0], 200)

    def test_a_session_that_never_started_is_removed_after_its_deadline_and_the_retention(self):
        pod = self.make_session(bound=False, deadline_in=100)
        self.clock.advance(100 + pc.SPOOL_RETENTION_SECONDS - 1)
        self.assertEqual(self.relay.sweep(), 0)
        self.clock.advance(2)
        self.assertEqual(self.relay.sweep(), 1)
        self.assertFalse(pod.directory().exists())

    def test_leftovers_without_a_record_wait_the_retention_out_by_their_age(self):
        leftover = self.root / pc.new_session_id()
        leftover.mkdir()
        (leftover / "junk").write_text("x", encoding="utf-8")
        self.assertEqual(self.relay.sweep(), 0)
        self.clock.now = leftover.stat().st_mtime + pc.SPOOL_RETENTION_SECONDS + 1
        self.assertEqual(self.relay.sweep(), 1)
        self.assertFalse(leftover.exists())

    def test_names_that_are_not_sessions_and_links_are_left_alone(self):
        decoy = self.base / "decoy"
        decoy.mkdir()
        (decoy / "keep.txt").write_text("keep", encoding="utf-8")
        (self.root / pc.new_session_id()).symlink_to(decoy)
        (self.root / "README").write_text("x", encoding="utf-8")
        (self.root / ".hidden").mkdir()
        (self.root / "ABCDEF0123456789ABCDEF0123456789").mkdir()
        self.clock.advance(10 * pc.SPOOL_RETENTION_SECONDS)
        self.assertEqual(self.relay.sweep(), 0)
        self.assertEqual((decoy / "keep.txt").read_text(encoding="utf-8"), "keep")
        self.assertEqual(len(os.listdir(self.root)), 4)

    def test_only_the_due_sessions_go(self):
        old = self.make_session().to_streaming()
        old.finish()
        self.clock.advance(pc.SPOOL_RETENTION_SECONDS + 1)
        young = self.make_session().to_streaming()
        young.finish()
        self.assertEqual(self.relay.sweep(), 1)
        self.assertFalse(old.directory().exists())
        self.assertTrue(young.directory().exists())

    def test_startup_removes_half_written_temporary_files_and_nothing_else(self):
        pod = self.make_session().to_streaming()
        out = pod.directory() / sr.OUT_DIR
        (out / ".delta.abcd1234.part").write_bytes(b"half a delta")
        (pod.directory() / ".state.json.abcd1234.tmp").write_bytes(b"{")
        self.relay.startup()
        self.assertEqual(sorted(os.listdir(out)), [sr.OUT_TRANSCRIPT])
        self.assertEqual(sorted(os.listdir(pod.directory())), ["in", "last_heartbeat", "out", "record.json", "state.json"])


class PathSafety(SpoolCase):
    def setUp(self):
        super().setUp()
        self.planted = []  # fifos planted by a test: `ask` lets a call that waits on one go

    def test_a_whole_session_writes_only_the_fixed_set_of_names(self):
        pod = self.make_session().to_streaming()
        pod.chunk("stderr", b"e")
        pod.finish(delta=b"d")
        found = set()
        for directory, _, names in os.walk(pod.directory()):
            found.update(os.path.relpath(os.path.join(directory, name), pod.directory()) for name in names)
        self.assertEqual(
            found,
            {"record.json", "state.json", "last_heartbeat", "in/bundle", "out/transcript", "out/stderr", "out/delta", "out/report.json"},
        )
        self.assertEqual(os.listdir(self.root), [pod.session])

    def test_the_route_table_is_closed_nothing_lists_or_reads_another_session(self):
        pod = self.make_session().to_fetched()
        before = snapshot(self.base)
        for method, path in (
            ("GET", "/"),
            ("GET", "/v1"),
            ("GET", "/v1/"),
            ("GET", "/v1/sessions"),
            ("GET", f"/v1/sessions/{pod.session}"),
            ("GET", "/v1/stream"),
            ("POST", "/v1/stream/other"),
            ("POST", "/v1/stream/../bundle"),
            ("POST", "/v1/stream/transcript/"),
            ("GET", "/v1/bundle?x=1"),
            ("GET", "/v1/bundle/"),
            ("GET", "//v1/bundle"),
            ("GET", "http://relay/v1/bundle"),
            ("GET", "/V1/bundle"),
        ):
            with self.subTest(path=path):
                self.assertEqual(pod.call(method, path), (404, {"error": "not_found"}))
        self.assertEqual(snapshot(self.base), before)

    def test_a_known_path_with_the_wrong_method_is_not_allowed(self):
        pod = self.make_session().to_fetched()
        for method, path in (
            ("POST", "/v1/bundle"),
            ("GET", "/v1/stream/transcript"),
            ("GET", "/v1/stream/stderr"),
            ("GET", "/v1/result"),
            ("GET", "/v1/preflight"),
            ("GET", "/v1/heartbeat"),
            ("PUT", "/v1/result"),
            ("DELETE", "/v1/bundle"),
            ("HEAD", "/v1/bundle"),
            ("OPTIONS", "/v1/heartbeat"),
        ):
            with self.subTest(method=method, path=path):
                self.assertEqual(pod.call(method, path), (405, {"error": "method"}))

    def test_a_link_planted_where_the_relay_writes_is_refused_and_never_followed(self):
        pod = self.make_session().to_fetched()
        decoy = self.base / "decoy.txt"
        decoy.write_text("keep", encoding="utf-8")
        out = pod.directory() / sr.OUT_DIR
        out.mkdir(exist_ok=True)
        (out / sr.OUT_TRANSCRIPT).symlink_to(decoy)
        self.assertEqual(pod.chunk("transcript", b"overwritten"), (500, {"error": "internal"}))
        self.assertEqual(decoy.read_text(encoding="utf-8"), "keep")
        (out / sr.OUT_TRANSCRIPT).unlink()
        out.rmdir()
        out.symlink_to(self.base)
        self.assertEqual(pod.chunk("transcript", b"x")[0], 500)
        self.assertEqual(pod.finish()[0], 500)
        self.assertEqual(sorted(path.name for path in self.base.iterdir()), ["decoy.txt", "spool"])

    def test_a_link_in_place_of_the_bundle_or_its_directory_is_not_served(self):
        decoy = self.base / "secret.txt"
        decoy.write_text("not for pods", encoding="utf-8")
        pod = self.make_session(bundle=None)
        pod.preflight()
        in_dir = pod.directory() / sr.IN_DIR
        in_dir.mkdir()
        (in_dir / sr.BUNDLE_FILE).symlink_to(decoy)
        self.assertEqual(pod.call("GET", "/v1/bundle")[0], 500)
        (in_dir / sr.BUNDLE_FILE).unlink()
        in_dir.rmdir()
        in_dir.symlink_to(self.base)
        self.assertEqual(pod.call("GET", "/v1/bundle")[0], 500)
        self.assertEqual(sr.read_state(self.root, pod.session)["fetch_attempts"], 0)

    # -- a fifo in the place of a file: refused at once, never waited on --------------------------------

    def plant_fifo(self, path):
        os.mkfifo(path, sr.FILE_MODE)
        self.planted.append(path)

    def ask(self, pod, *request, seconds=5):
        """`pod.call(*request)` made in a thread that is waited on for `seconds` only: the answer, None if none came in time.

        A call that waits on a fifo is let go by opening the fifo itself (a read-write open never waits and
        counts as both ends), so a regression fails the test by its own bound and leaves no thread behind.
        """
        outcome = []
        thread = threading.Thread(target=lambda: outcome.append(pod.call(*request)), daemon=True)
        thread.start()
        thread.join(seconds)
        in_time = not thread.is_alive()
        if not in_time:
            for path in self.planted:
                with contextlib.suppress(OSError):
                    os.close(os.open(path, os.O_RDWR | os.O_NONBLOCK))
            thread.join(seconds)
        return outcome[0] if in_time and outcome else None

    def test_a_fifo_in_place_of_the_bundle_is_refused_without_waiting_and_the_relay_goes_on(self):
        pod = self.make_session(bundle=None)
        pod.preflight()
        in_dir = pod.directory() / sr.IN_DIR
        in_dir.mkdir()
        self.plant_fifo(in_dir / sr.BUNDLE_FILE)
        self.assertEqual(self.ask(pod, "GET", "/v1/bundle"), (500, {"error": "internal"}))
        self.assertEqual(sr.read_state(self.root, pod.session)["fetch_attempts"], 0)
        other = self.make_session()
        self.assertEqual(self.ask(other, "POST", "/v1/heartbeat"), (200, {"state": "bound"}))
        self.assertEqual(self.ask(other, "POST", "/v1/preflight", b'{"result":"ok"}'), (200, {"state": "preflighted"}))
        # nothing was left claimed or counted: with a real bundle in its place the same session is served
        (in_dir / sr.BUNDLE_FILE).unlink()
        (in_dir / sr.BUNDLE_FILE).write_bytes(b"real")
        self.assertEqual(pod.fetch(), (200, b"real", True))

    def test_a_fifo_in_place_of_a_stream_file_is_refused_and_nothing_is_pushed_into_it(self):
        for stream, name in (("transcript", sr.OUT_TRANSCRIPT), ("stderr", sr.OUT_STDERR)):
            for with_reader in (False, True):
                with self.subTest(stream=stream, with_reader=with_reader):
                    self.append_to_a_fifo(stream, name, with_reader)

    def append_to_a_fifo(self, stream, name, with_reader):
        """One chunk to a fifo planted at `name`: refused (500) twice (so the claim was let go), nothing written to it."""
        pod = self.make_session().to_fetched()
        out = pod.directory() / sr.OUT_DIR
        out.mkdir(exist_ok=True)
        fifo = out / name
        self.plant_fifo(fifo)
        # with a reader on the fifo an open for writing succeeds, so the regular-file check is what refuses it
        reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK) if with_reader else None
        try:
            for _ in range(2):
                self.assertEqual(self.ask(pod, "POST", f"/v1/stream/{stream}", b"chunk"), (500, {"error": "internal"}))
            if reader is not None:
                self.assertEqual(os.read(reader, 100), b"")  # end of file: no writer put a byte into it
        finally:
            if reader is not None:
                os.close(reader)
        other = self.make_session()
        self.assertEqual(self.ask(other, "POST", "/v1/heartbeat"), (200, {"state": "bound"}))
        self.assertEqual(sr.read_state(self.root, pod.session)["state"], "fetched")
        fifo.unlink()
        self.assertEqual(pod.chunk(stream, b"after")[0], 200)
        self.assertEqual(pod.out(name), b"after")

    def test_a_link_for_the_session_directory_is_gone_not_followed(self):
        pod = self.make_session()
        moved = self.base / "moved"
        os.rename(pod.directory(), moved)
        pod.directory().symlink_to(moved)
        self.assertEqual(pod.heartbeat()[0], 410)

    def test_files_and_directories_have_no_access_for_others_whatever_the_umask(self):
        for umask in (0o077, 0o000):
            previous = os.umask(umask)
            try:
                pod = self.make_session().to_streaming()
                pod.finish(delta=b"d")
            finally:
                os.umask(previous)
            with self.subTest(umask=oct(umask)):
                self.assertEqual(stat.S_IMODE((pod.directory() / sr.OUT_DIR).stat().st_mode), sr.DIR_MODE)
                for name in (sr.OUT_TRANSCRIPT, sr.OUT_DELTA, sr.OUT_REPORT):
                    self.assertEqual(stat.S_IMODE((pod.directory() / sr.OUT_DIR / name).stat().st_mode), sr.FILE_MODE)
                for name in (sr.STATE_FILE, sr.HEARTBEAT_FILE):
                    self.assertEqual(stat.S_IMODE((pod.directory() / name).stat().st_mode), sr.FILE_MODE)


class SecretsAndLogs(SpoolCase):
    def captured(self, action):
        stream = io.StringIO()
        with contextlib.redirect_stderr(stream), contextlib.redirect_stdout(stream):
            result = action()
        return result, stream.getvalue()

    def test_no_response_or_log_line_carries_a_token_a_header_a_body_or_agent_text(self):
        pod = self.make_session()
        poisoned = {"Authorization": f"Bearer {POISON}", relay.HEADER_POD_UID: f"x-{POISON}"}
        replies = []

        def run():
            replies.append(pod.send("POST", "/v1/preflight", POISON.encode("ascii"), headers=poisoned))
            replies.append(pod.send("POST", "/v1/preflight", SECRET_TEXT.encode("ascii")))
            replies.append(pod.send("POST", "/v1/preflight", json.dumps({"result": "failed", "code": SECRET_TEXT}).encode("ascii")))
            replies.append(pod.send("POST", f"/v1/{SECRET_TEXT}", SECRET_TEXT.encode("ascii")))
            replies.append(pod.send("GET", f"/v1/bundle?token={POISON}"))
            replies.append(pod.send("POST", "/v1/heartbeat", headers={relay.HEADER_POD_NAME: f"factory-w-{POISON}"}))
            replies.append(pod.send("POST", "/v1/result", SECRET_TEXT.encode("ascii")))

        _, log = self.captured(run)
        everything = b"".join(reply.body for reply in replies).decode("ascii") + log
        for secret in (POISON, SECRET_TEXT, pod.token):
            self.assertNotIn(secret, everything)
        self.assertEqual([reply.status for reply in replies], [401, 400, 400, 404, 404, 401, 409])

    def test_the_token_is_nowhere_in_the_spool_only_its_hash(self):
        pod = self.make_session().to_streaming()
        pod.finish(delta=b"d")
        needle = pod.token.encode("ascii")
        for directory, _, names in os.walk(self.root):
            for name in names:
                with self.subTest(file=name):
                    self.assertNotIn(needle, (Path(directory) / name).read_bytes())
        record = (pod.directory() / sr.RECORD_FILE).read_text(encoding="utf-8")
        self.assertIn(hashlib.sha256(needle).hexdigest(), record)

    def test_an_unexpected_error_answers_a_fixed_text_and_logs_only_the_class_name(self):
        pod = self.make_session()
        with mock.patch.object(sr, "write_state_at", side_effect=RuntimeError(SECRET_TEXT)):
            (status, body), log = self.captured(lambda: pod.preflight())
        self.assertEqual((status, body), (500, {"error": "internal"}))
        self.assertEqual(log, "factory-relay: internal error RuntimeError\n")
        self.assertNotIn(SECRET_TEXT, log)

    def test_the_relay_refuses_to_start_with_a_secret_in_its_environment(self):
        for name in (
            "ANTHROPIC_API_KEY",
            "GITHUB_APP_PRIVATE_KEY",
            "GH_TOKEN",
            "METRICS_TOKEN",
            "AZURE_CLIENT_ID",
            "AZURE_FEDERATED_TOKEN_FILE",
            "DB_PASSWORD",
            "MY_SECRET",
            "aws_credentials",
            "APIKEY",
        ):
            with self.subTest(name=name):
                self.assertTrue(relay.looks_secret(name))
                self.assertEqual(relay.check_environment({name: POISON}, token_path="/nonexistent"), "secret_in_environment")

    def test_names_the_relay_legitimately_has_are_not_secrets(self):
        environment = {
            "PATH": "/usr/bin",
            "HOME": "/nonexistent",
            "PYTHONUNBUFFERED": "1",
            "KUBERNETES_SERVICE_HOST": "10.0.0.1",
            "KUBERNETES_SERVICE_PORT": "443",
            pc.ENV_RELAY["port"]: "8090",
            pc.ENV_RELAY["spool_dir"]: "/spool",
            "HOSTNAME": "manager-0",
        }
        self.assertIsNone(relay.check_environment(environment, token_path="/nonexistent"))

    def test_the_names_the_image_itself_sets_are_accepted(self):
        dockerfile = Path(__file__).resolve().parent.parent / "Dockerfile"
        lines = dockerfile.read_text(encoding="utf-8").splitlines()
        runtime = lines[max(index for index, line in enumerate(lines) if line.startswith("FROM ")) :]
        names = []
        index = 0
        while index < len(runtime):
            if runtime[index].startswith("ENV "):
                instruction = [runtime[index][len("ENV ") :]]
                while instruction[-1].rstrip().endswith("\\"):
                    index += 1
                    instruction.append(runtime[index])
                joined = " ".join(part.rstrip().removesuffix("\\") for part in instruction)
                names.extend(token.partition("=")[0] for token in shlex.split(joined))
            index += 1
        self.assertIn("FACTORY_TOKEN_BROKER", names)  # the parse found the block
        self.assertIn("GH_PROMPT_DISABLED", names)
        for value in ("x", ""):
            with self.subTest(value=value):
                environment = dict.fromkeys(names, value)
                self.assertIsNone(relay.check_environment(environment, token_path="/nonexistent"), sorted(environment))

    def test_the_image_names_are_exempt_by_exact_name_only(self):
        for name in ("FACTORY_TOKEN_BROKER", "GH_NO_UPDATE_NOTIFIER", "GH_PROMPT_DISABLED"):
            with self.subTest(name=name):
                self.assertFalse(relay.looks_secret(name))
                for other in (f"{name}_TOKEN", f"{name}_KEY", name.lower()):
                    self.assertTrue(relay.looks_secret(other), other)
        for name in ("GH_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_TOKEN", "GH_HOST", "FACTORY_GITHUB_APP_KEY", "FACTORY_TOKEN"):
            with self.subTest(name=name):
                self.assertTrue(relay.looks_secret(name))

    def test_a_mounted_kubernetes_token_makes_the_relay_refuse_to_start(self):
        token = self.base / "token"
        token.write_text("fake", encoding="utf-8")
        self.assertEqual(relay.check_environment({}, token_path=str(token)), "kubernetes_token_present")

    def test_main_refuses_without_serving_and_without_printing_the_value(self):
        cases = {
            "secret_in_environment": {"ANTHROPIC_API_KEY": POISON},
            "invalid_port": {pc.ENV_RELAY["port"]: "tcp://10.0.0.5:8090"},
        }
        for code, environment in cases.items():
            with self.subTest(code=code):
                status, log = self.captured(lambda: relay.main(environment, token_path="/nonexistent"))
                self.assertEqual((status, log), (2, f"factory-relay: refusing to start: {code}\n"))

    def test_main_refuses_an_invalid_port_and_a_root_user(self):
        for port in ("0", "70000", "-1", "80a", "", "٣"):
            with self.subTest(port=port):
                status, _ = self.captured(lambda: relay.main({pc.ENV_RELAY["port"]: port}, token_path="/nonexistent"))
                self.assertEqual(status, 2)
        with mock.patch.object(relay.os, "geteuid", return_value=0):
            status, log = self.captured(lambda: relay.main({}, token_path="/nonexistent"))
        self.assertEqual((status, log), (2, "factory-relay: refusing to start: running_as_root\n"))

    def test_main_refuses_an_unsafe_spool_root(self):
        os.chmod(self.root, 0o2777)
        environment = {pc.ENV_RELAY["spool_dir"]: str(self.root), pc.ENV_RELAY["port"]: "18090"}
        with mock.patch.object(relay.os, "geteuid", return_value=pc.RELAY_UID):
            status, log = self.captured(lambda: relay.main(environment, token_path="/nonexistent"))
        self.assertEqual(status, 2)
        self.assertTrue(log.startswith("factory-relay: refusing to start: spool root "), log)

    def test_the_relay_makes_no_outbound_call_and_reads_no_secret_or_data_directory(self):
        source = Path(relay.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            if isinstance(node, ast.ImportFrom):
                modules.add("." * node.level + (node.module or ""))
        self.assertEqual(
            modules,
            {
                "hashlib", "hmac", "http.server", "json", "os", "re", "secrets", "shutil", "socketserver", "stat", "struct",
                "sys", "threading", "time", ".", "collections", "contextlib", "dataclasses",
            },
        )
        names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        names |= {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        for forbidden in ("connect", "create_connection", "urlopen", "getaddrinfo", "gethostbyname", "getfqdn", "Popen", "system"):
            self.assertNotIn(forbidden, names)
        self.assertNotIn("/data", source)
        self.assertNotIn("SCRATCH_DIR", source)

    def test_the_relay_has_no_path_to_the_environment_beyond_its_two_settings(self):
        tree = ast.parse(Path(relay.__file__).read_text(encoding="utf-8"))
        reads = [node for node in ast.walk(tree) if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv")]
        self.assertEqual(len(reads), 1)


class Transport(SpoolCase):
    def start(self):
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
        return server

    def connect(self):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        self.addCleanup(sock.close)
        return sock

    @staticmethod
    def read_all(sock):
        parts = []
        while True:
            try:
                data = sock.recv(65536)
            except (ConnectionResetError, BrokenPipeError):
                break
            if not data:
                break
            parts.append(data)
        return b"".join(parts)

    @staticmethod
    def split(raw):
        head, _, body = raw.partition(b"\r\n\r\n")
        return (int(head.split()[1]) if head else 0), head, body

    def exchange(self, payload):
        sock = self.connect()
        with contextlib.suppress(OSError):
            sock.sendall(payload)
        return self.split(self.read_all(sock))

    def request(self, pod, method, path, data=b"", headers=None):
        merged = {**pod.headers(), **(headers or {})}
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(connection.close)
        connection.request(method, path, body=data if method == "POST" else None, headers=merged)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()

    def test_a_whole_session_over_http_ends_in_the_same_states(self):
        self.start()
        pod = self.make_session()
        self.assertEqual(self.request(pod, "POST", "/v1/preflight", b'{"result": "ok"}')[0], 200)
        status, headers, body = self.request(pod, "GET", "/v1/bundle")
        self.assertEqual((status, body, headers["Content-Length"]), (200, BUNDLE, str(len(BUNDLE))))
        self.assertTrue(self.server.wait_idle(0))
        self.assertEqual(pod.stored(), "fetched")
        self.assertEqual(self.request(pod, "POST", "/v1/stream/transcript", b"hello\n")[0], 200)
        self.assertEqual(self.request(pod, "POST", "/v1/heartbeat")[0], 200)
        self.assertEqual(self.request(pod, "POST", "/v1/result", result_body(delta=b"the delta"))[0], 200)
        self.assertEqual((pod.stored(), pod.out(sr.OUT_DELTA), pod.out(sr.OUT_TRANSCRIPT)), ("finished", b"the delta", b"hello\n"))
        self.assertEqual(self.request(pod, "POST", "/v1/heartbeat")[:1], (410,))

    def test_no_header_names_the_server_software_or_the_date(self):
        self.start()
        pod = self.make_session()
        _, headers, body = self.request(pod, "POST", "/v1/heartbeat")
        self.assertEqual(sorted(headers), ["Connection", "Content-Length", "Content-Type"])
        self.assertEqual(json.loads(body), {"state": "bound"})

    def test_errors_over_http_are_fixed_documents_that_echo_nothing_and_log_nothing(self):
        self.start()
        stream = io.StringIO()
        requests = (
            f"GET /v1/bundle?token={POISON} HTTP/1.0\r\n\r\n".encode("ascii"),
            f"{POISON}\r\n\r\n".encode("ascii"),
            f"BREW /v1/{POISON} HTTP/1.0\r\n\r\n".encode("ascii"),
            f"POST /v1/heartbeat HTTP/1.0\r\nContent-Length: {POISON}\r\n\r\n".encode("ascii"),
            b"POST /v1/heartbeat HTTP/1.0\r\nContent-Length: -1\r\n\r\n",
            b"POST /v1/heartbeat HTTP/1.0\r\nContent-Length: 99999999999999\r\n\r\n",
        )
        statuses = []
        with contextlib.redirect_stderr(stream):
            for payload in requests:
                status, _, body = self.exchange(payload)
                statuses.append(status)
                self.assertNotIn(POISON.encode("ascii"), body)
                self.assertIn(b"error", body)
        self.assertEqual(statuses, [404, 400, 501, 400, 400, 400])
        self.assertEqual(stream.getvalue(), "")

    def test_a_header_section_over_the_limit_is_cut_off_without_an_answer(self):
        self.start()
        big = "".join(f"X-Filler-{index}: {'v' * 200}\r\n" for index in range(100))
        status, _, body = self.exchange(f"GET /v1/bundle HTTP/1.0\r\n{big}\r\n".encode("ascii"))
        self.assertEqual((status, body), (0, b""))
        status, _, body = self.exchange(f"GET /{'a' * 20000} HTTP/1.0\r\n\r\n".encode("ascii"))
        self.assertEqual((status, body), (0, b""))
        pod = self.make_session()
        self.assertEqual(self.request(pod, "POST", "/v1/heartbeat")[0], 200)

    def test_a_request_beyond_the_thread_bound_is_answered_503_at_once(self):
        self.set_limits(max_threads=2)
        server = self.start()
        idle = [self.connect(), self.connect()]
        self.assertTrue(server.wait_active(2))
        status, _, body = self.exchange(b"GET /v1/bundle HTTP/1.0\r\n\r\n")
        self.assertEqual((status, json.loads(body)), (503, {"error": "busy"}))
        for sock in idle:
            sock.close()
        self.assertTrue(server.wait_idle(0))
        pod = self.make_session()
        self.assertEqual(self.request(pod, "POST", "/v1/heartbeat")[0], 200)

    def source_request(self, pod, source):
        """A heartbeat from another peer address (127.0.0.0/8 is all loopback on Linux)."""
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10, source_address=(source, 0))
        self.addCleanup(connection.close)
        try:
            connection.request("POST", "/v1/heartbeat", headers=pod.headers())
        except OSError:
            self.skipTest("this host cannot bind a second loopback address")
        return connection.getresponse().status

    def test_one_peer_cannot_take_every_thread(self):
        self.start()
        pod = self.make_session()
        held = [self.connect() for _ in range(relay.Limits().max_threads)]  # silent sockets, none sends a byte
        self.assertTrue(self.server.wait_active(relay.PEER_MAX_CONNECTIONS))
        for sock in held:  # the rest are answered 503 at once, so none of them lingers
            sock.settimeout(10)
        refused = [self.split(self.read_all(sock))[0] for sock in held[relay.PEER_MAX_CONNECTIONS :]]
        self.assertEqual(set(refused), {503})
        self.assertEqual(self.server.active, relay.PEER_MAX_CONNECTIONS)
        self.assertEqual(self.source_request(pod, "127.0.0.2"), 200)  # another peer is still served
        for sock in held:
            sock.close()
        self.assertTrue(self.server.wait_idle(0))
        self.assertEqual(self.server._peers, {})
        self.assertEqual(self.request(pod, "POST", "/v1/heartbeat")[0], 200)  # and the first peer is served again

    def test_a_peer_over_its_own_bound_is_answered_503_while_the_thread_bound_has_room(self):
        self.set_limits(max_per_peer=2)
        server = self.start()
        pod = self.make_session()
        idle = [self.connect(), self.connect()]
        self.assertTrue(server.wait_active(2))
        status, _, body = self.exchange(b"GET /v1/bundle HTTP/1.0\r\n\r\n")
        self.assertEqual((status, json.loads(body)), (503, {"error": "busy"}))
        self.assertEqual(self.source_request(pod, "127.0.0.2"), 200)
        idle[0].close()
        self.assertTrue(server.wait_active(1))
        self.assertTrue(server.wait_idle(1))
        self.assertEqual(self.request(pod, "POST", "/v1/heartbeat")[0], 200)

    def test_a_connection_that_does_not_finish_its_headers_is_cut_off_at_the_header_time(self):
        self.start()
        pod = self.make_session()
        before = sr.read_heartbeat(self.root, pod.session)
        slow = self.connect()
        self.assertTrue(self.server.wait_active(1))  # accepted: the header time has started
        slow.sendall(b"POST /v1/heartbeat HTTP/1.0\r\n")
        self.clock.advance(relay.HEADER_TIMEOUT_SECONDS + 1)  # far below the total time of a stream call
        self.assertLess(relay.HEADER_TIMEOUT_SECONDS + 1, pc.RELAY_STREAM_TOTAL_SECONDS)
        with contextlib.suppress(OSError):
            slow.sendall(f"Authorization: Bearer {pod.token}\r\n".encode("ascii"))  # a drip does not extend it
        self.assertEqual(self.read_all(slow), b"")
        self.assertTrue(self.server.wait_idle(0))
        self.assertEqual(sr.read_heartbeat(self.root, pod.session), before)

    def test_a_connection_that_finishes_its_headers_in_time_gets_the_total_time_of_its_route(self):
        self.start()
        pod = self.make_session().to_streaming()
        first, rest = self.result_request(pod, result_body(delta=b"d" * 100))
        with self.claims(pod.session) as started:
            sock = self.connect()
            sock.sendall(first)
            self.assertTrue(started.wait(10))
            self.clock.advance(relay.HEADER_TIMEOUT_SECONDS + 1)  # past the header time, inside the transfer time
            sock.sendall(rest)
            self.assertEqual(self.split(self.read_all(sock))[0], 200)
        self.assertEqual(pod.stored(), "finished")

    def test_a_slow_client_is_cut_off_at_the_total_time_even_if_it_keeps_sending(self):
        self.start()
        pod = self.make_session()
        payload = b"".join(
            [
                b"POST /v1/heartbeat HTTP/1.0\r\n",
                f"Authorization: Bearer {pod.token}\r\n".encode("ascii"),
                f"{relay.HEADER_POD_NAME}: {pc.pod_name(pod.session)}\r\n".encode("ascii"),
                f"{relay.HEADER_POD_UID}: {UID}\r\n".encode("ascii"),
                b"Content-Length: 0\r\n\r\n",
            ]
        )
        quick = self.connect()
        quick.sendall(payload)
        self.assertEqual(self.split(self.read_all(quick))[0], 200)
        before = sr.read_heartbeat(self.root, pod.session)
        self.assertTrue(self.server.wait_idle(0))
        slow = self.connect()
        self.assertTrue(self.server.wait_active(1))  # accepted: its total time has started
        slow.sendall(payload[:20])
        self.clock.advance(pc.RELAY_STREAM_TOTAL_SECONDS + 1)
        with contextlib.suppress(OSError):
            slow.sendall(payload[20:])
        self.assertEqual(self.read_all(slow), b"")
        self.assertEqual(sr.read_heartbeat(self.root, pod.session), before)

    def result_request(self, pod, body):
        head = b"".join(
            [
                b"POST /v1/result HTTP/1.0\r\n",
                f"Authorization: Bearer {pod.token}\r\n".encode("ascii"),
                f"{relay.HEADER_POD_NAME}: {pc.pod_name(pod.session)}\r\n".encode("ascii"),
                f"{relay.HEADER_POD_UID}: {UID}\r\n".encode("ascii"),
                f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"),
            ]
        )
        return b"".join([head, body[:10]]), body[10:]

    @contextlib.contextmanager
    def claims(self, session):
        """Yields an Event that is set when the relay starts to receive the result of `session` (the headers are parsed)."""
        started = threading.Event()
        real = relay.Relay._claim

        def claim(relay_self, key, reserve):
            real(relay_self, key, reserve)
            if key == (session, "result"):
                started.set()

        with mock.patch.object(relay.Relay, "_claim", claim):
            yield started

    def test_a_transfer_may_take_longer_than_a_stream_call_but_not_longer_than_its_own_cap(self):
        self.start()
        body = result_body(delta=b"d" * 100)
        pod = self.make_session().to_streaming()
        first, rest = self.result_request(pod, body)
        with self.claims(pod.session) as started:
            late = self.connect()
            late.sendall(first)
            self.assertTrue(started.wait(10))
            self.clock.advance(pc.RELAY_TRANSFER_TOTAL_SECONDS - 1)
            late.sendall(rest)
            self.assertEqual(self.split(self.read_all(late))[0], 200)
        self.assertEqual(pod.stored(), "finished")
        other = self.make_session().to_streaming()
        first, rest = self.result_request(other, body)
        with self.claims(other.session) as started:
            too_late = self.connect()
            too_late.sendall(first)
            self.assertTrue(started.wait(10))
            self.clock.advance(pc.RELAY_TRANSFER_TOTAL_SECONDS + 1)
            with contextlib.suppress(OSError):
                too_late.sendall(rest)
            self.assertEqual(self.read_all(too_late), b"")
        self.assertTrue(self.server.wait_idle(0))
        self.assertEqual(other.stored(), "streaming")
        self.assertFalse(any(name.endswith(".part") for name in os.listdir(other.directory() / sr.OUT_DIR)))
        self.assertEqual(self.relay._busy, set())

    def test_a_bundle_download_that_drops_is_an_attempt_and_the_retry_completes(self):
        self.start()
        pod = self.make_session(bundle=None)
        with sr.session_dir(self.root, pod.session) as fd:
            in_fd = sr.ensure_dir_at(fd, sr.IN_DIR)
            handle = os.open(sr.BUNDLE_FILE, os.O_WRONLY | os.O_CREAT, sr.FILE_MODE, dir_fd=in_fd)
            os.ftruncate(handle, 48 * 1024 * 1024)
            os.close(handle)
            os.close(in_fd)
        pod.preflight()
        payload = b"".join(
            [
                b"GET /v1/bundle HTTP/1.0\r\n",
                f"Authorization: Bearer {pod.token}\r\n".encode("ascii"),
                f"{relay.HEADER_POD_NAME}: {pc.pod_name(pod.session)}\r\n".encode("ascii"),
                f"{relay.HEADER_POD_UID}: {UID}\r\n\r\n".encode("ascii"),
            ]
        )
        sock = self.connect()
        sock.sendall(payload)
        self.assertGreater(len(sock.recv(1024)), 0)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        sock.close()
        self.assertTrue(self.server.wait_idle(0))
        self.assertEqual((pod.stored(), sr.read_state(self.root, pod.session)["fetch_attempts"]), ("preflighted", 1))
        status, _, body = self.exchange(payload)
        self.assertEqual((status, len(body)), (200, 48 * 1024 * 1024))
        self.assertTrue(self.server.wait_idle(0))
        self.assertEqual(pod.stored(), "fetched")

    def test_the_deadline_gives_the_idle_timeout_until_the_total_time_is_used_up(self):
        clock = FakeClock()
        deadline = relay.Deadline(clock, 60, 120)
        self.assertEqual(deadline.timeout(), 60)
        clock.advance(100)
        self.assertEqual(deadline.timeout(), 20)
        deadline.limit_to(900)
        self.assertEqual(deadline.timeout(), 60)
        clock.advance(800)
        with self.assertRaises(relay.DeadlineExceeded):
            deadline.timeout()

    def test_a_read_that_returns_after_the_total_time_ran_out_is_refused(self):
        clock = FakeClock()

        class Sock:
            def __init__(self):
                self.timeouts = []

            def settimeout(self, value):
                self.timeouts.append(value)

            def recv(self, size):
                clock.advance(1000)
                return b"late data"

        sock = Sock()
        reader = relay._Reader(sock, relay.Deadline(clock, 60, 120))
        with self.assertRaises(relay.DeadlineExceeded):
            reader.read(10)
        self.assertEqual(sock.timeouts, [60])


if __name__ == "__main__":
    unittest.main()

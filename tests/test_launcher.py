"""The pod launcher (src/factory/launcher.py) and its client (src/factory/launcher_client.py).

Slice 11 of the pod backend (docs/pod-backend.md). CONTRACT-TESTED AGAINST ASSUMED SHAPES: the launcher talks to
the in-memory fake of the Kubernetes API (tests/fake_kube.py) through the real client of src/factory/kube.py over a
local HTTP server, with a fake clock and an injected sleep; no cluster was consulted. Sockets are REAL unix sockets
and ``SO_PEERCRED`` is real (Linux). One process can meet a foreign peer only by configuring another allowed uid,
so that is how the refusals are tested. Every test pins a rule that fails when the rule is removed:

* who may talk: the server drops a foreign uid without reading a byte; the client authenticates the server (lstat
  of the directory and of the socket, ``SO_PEERCRED`` of the connected socket) BEFORE it sends a byte, and a planted
  listener reads ZERO bytes (no request, no relay token);
* the launcher refuses to start on a socket directory that is not its own, is writable by group or others or is a
  symbolic link;
* the closed schema and its bounds: unknown and missing fields, wrong types, duplicate keys, a huge line, a slow
  loris (on a fake clock), the connection bound and thread joins;
* the caps (live pods, per repository, deadline clamp and minimum, class and phase enums) with the fixed
  ``capacity`` answer, and the in-process in-flight count under concurrency;
* compensation after a failure between the pod and its Secret, and that no secret value reaches a reply, a log
  line or an error text (a poisoned environment);
* the scheduling timeout, the deadline backstop and the pull-error window on a fake clock;
* deny, status, terminate, list, info, and the reader and terminate seam of slice 5.
"""

import ast
import base64
import contextlib
import io
import json
import logging
import os
import shutil
import socket
import stat
import struct
import sys
import tempfile
import threading
import traceback
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from factory import kube, launcher, launcher_client, podhandle, podspec
from factory import podcontract as pc
from tests import fake_kube
from tests.fake_kube import FakeClock, FakeKube, Step

ROOT = Path(__file__).resolve().parents[1]
KEY = "CANARY-ANTHROPIC-KEY-launcher-8c1d"
TOKEN = "CANARY-RELAY-TOKEN-launcher-5e2a"
KUBE_TOKEN = "CANARY-KUBE-BEARER-launcher-77f0"
CANARIES = (KEY, TOKEN, KUBE_TOKEN)
RELAY_URL = "http://10.244.1.7:8090"
API_HOST = "example-cluster.hcp.example.invalid"
JOIN = 10.0
FOREIGN = os.getuid() + 1  # a uid that is not ours: the only way to meet a foreign peer in one process


def short_dir(test):
    """A short private directory (AF_UNIX paths are limited to about 108 bytes, whatever TMPDIR is)."""
    base = "/tmp" if os.access("/tmp", os.W_OK) else "/dev/shm"
    path = tempfile.mkdtemp(prefix="fl", dir=base)
    test.addCleanup(shutil.rmtree, path, True)
    return path


def make_config(directory, **overrides):
    values = {
        "image": fake_kube.IMAGE,
        "namespace": pc.WORKER_NAMESPACE,
        "max_pods": 8,
        "repo_cap": 4,
        "deadline_clamp": pc.DEADLINE_CLAMP_SECONDS,
        "allowed_uid": os.getuid(),
        "relay_url": RELAY_URL,
        "classes": ("standard", "small"),
        "api_public_host": API_HOST,
        "manager_address": "10.244.1.7",
        "manager_metrics_address": "10.96.0.9",
        "socket_path": str(Path(directory) / "launcher.sock"),
        "anthropic_key": KEY,
    }
    values.update(overrides)
    return launcher.Config(**values)


def launch_request(n=1, repository=fake_kube.REPOSITORY, **overrides):
    request = {
        "session_id": fake_kube.session_id(n),
        "repository": repository,
        "run_id": "run-20260101-0001",
        "job_id": "build-1",
        "attempt": 1,
        "phase": "build",
        "resource_class": "standard",
        "timeout_seconds": 1800,
        "manager_instance": fake_kube.INSTANCE,
    }
    request.update(overrides)
    return request


def line(document):
    return json.dumps(document).encode("utf-8")


def launch_line(n=1, token=TOKEN, **overrides):
    return line({"op": "launch", "token": token, **launch_request(n, **overrides)})


def handle(session, document):
    return session.handle_line(line(document))


def pod_name(n):
    return pc.pod_name(fake_kube.session_id(n))


def noop(_seconds):
    return None


class Harness(unittest.TestCase):
    """A launcher over the real kube client and the fake API server, all on one fake clock."""

    scenario = "succeeds"
    options = {}

    def setUp(self):
        self.directory = short_dir(self)
        self.clock = FakeClock()
        self.fake = FakeKube(clock=self.clock, admit_image=fake_kube.IMAGE, scenario=self.scenario)
        self.fake.start()
        self.addCleanup(self.fake.stop)
        token_file = Path(self.directory) / "kube-token"
        token_file.write_text(KUBE_TOKEN)
        self.api = kube.Client(self.fake.url, token_path=str(token_file), sleep=noop)
        self.logs = []
        self.config = make_config(self.directory, **self.options)
        self.launcher = launcher.Launcher(self.api, self.config, clock=self.clock, log=self.logs.append)

    def launch(self, n=1, **overrides):
        return self.launcher.handle_line(launch_line(n, **overrides))

    def assertLaunched(self, reply, n=1):
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["name"], pod_name(n))
        return reply

    def assertNoSecrets(self, *texts):
        for text in texts:
            for canary in CANARIES:
                self.assertNotIn(canary, str(text))


# --- Configuration ------------------------------------------------------------------------------------------------


def full_environment(directory):
    names = pc.ENV_LAUNCHER
    return {
        names["image"]: fake_kube.IMAGE,
        names["namespace"]: pc.WORKER_NAMESPACE,
        names["max_pods"]: "8",
        names["repo_cap"]: "3",
        names["deadline_clamp"]: "7200",
        names["allowed_uid"]: str(os.getuid()),
        names["relay_url"]: RELAY_URL,
        names["classes"]: "standard",
        names["api_public_host"]: API_HOST,
        names["manager_address"]: "10.244.1.7",
        names["manager_metrics_address"]: "10.96.0.9",
        names["socket"]: str(Path(directory) / "launcher.sock"),
        launcher.KEY_ENV: KEY,
    }


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = short_dir(self)

    def test_every_contract_name_is_read_from_the_environment(self):
        env = full_environment(self.directory)
        config = launcher.Config.from_environment(env)
        self.assertEqual(
            (config.image, config.max_pods, config.repo_cap, config.deadline_clamp, config.classes, config.allowed_uid),
            (fake_kube.IMAGE, 8, 3, 7200, ("standard",), os.getuid()),
        )
        self.assertEqual(config.socket_path, env[pc.ENV_LAUNCHER["socket"]])
        self.assertEqual(set(env), {*pc.ENV_LAUNCHER.values(), launcher.KEY_ENV})

    def test_defaults_are_the_contracts_and_the_restrictive_ones(self):
        names = pc.ENV_LAUNCHER
        env = full_environment(self.directory)
        for name in (names["namespace"], names["repo_cap"], names["deadline_clamp"], names["allowed_uid"], names["classes"], names["socket"]):
            del env[name]
        config = launcher.Config.from_environment(env)
        self.assertEqual(config.namespace, pc.WORKER_NAMESPACE)
        self.assertEqual(config.repo_cap, pc.PER_REPO_CAP)
        self.assertEqual(config.deadline_clamp, pc.DEADLINE_CLAMP_SECONDS)
        self.assertEqual(config.allowed_uid, pc.SUPERVISOR_UID)
        self.assertEqual(config.classes, tuple(pc.CLASSES))
        self.assertEqual(config.socket_path, pc.LAUNCHER_SOCKET)

    def test_the_caps_have_no_default_and_a_missing_secret_refuses(self):
        names = pc.ENV_LAUNCHER
        for name, code in (
            (names["max_pods"], "max_pods"),
            (names["image"], "image"),
            (names["relay_url"], "relay_url"),
            (names["manager_address"], "manager_address"),
            (names["manager_metrics_address"], "manager_metrics_address"),
            (names["api_public_host"], "api_public_host"),
            (launcher.KEY_ENV, "anthropic_key"),
        ):
            env = full_environment(self.directory)
            del env[name]
            with self.assertRaises(launcher.ConfigRefused) as caught:
                launcher.Config.from_environment(env)
            self.assertEqual(caught.exception.code, code)

    def test_a_bad_value_is_refused_by_field_and_never_echoed(self):
        names = pc.ENV_LAUNCHER
        bad = {
            names["max_pods"]: ("0", "max_pods"),
            names["repo_cap"]: ("-1", "repo_cap"),
            names["deadline_clamp"]: (str(pc.DEADLINE_CLAMP_SECONDS + 1), "deadline_clamp"),
            names["allowed_uid"]: ("ten", "allowed_uid"),
            names["namespace"]: ("kube-system", "namespace"),
            names["classes"]: ("standard,huge", "classes"),
            names["image"]: ("registry.example.invalid/factory:latest", "image"),
            names["relay_url"]: ("https://relay.example.invalid", "relay_url"),
            names["socket"]: ("relative.sock", "socket"),
            launcher.KEY_ENV: ("has space", "anthropic_key"),
        }
        for name, (value, code) in bad.items():
            env = full_environment(self.directory)
            env[name] = value
            with self.assertRaises(launcher.ConfigRefused) as caught:
                launcher.Config.from_environment(env)
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn(value, str(caught.exception))

    def test_the_deadline_clamp_below_the_minimum_is_refused(self):
        with self.assertRaises(launcher.ConfigRefused):
            make_config(self.directory, deadline_clamp=launcher.MIN_TIMEOUT_SECONDS - 1)

    def test_a_socket_path_too_long_for_af_unix_is_refused(self):
        with self.assertRaises(launcher.ConfigRefused):
            make_config(self.directory, socket_path=f"/{'a' * 120}/launcher.sock")

    def test_the_key_is_not_in_the_repr(self):
        self.assertNotIn(KEY, repr(make_config(self.directory)))


# --- The closed schema -----------------------------------------------------------------------------------------------


class SchemaTests(Harness):
    def assertInvalid(self, raw):
        self.assertEqual(self.launcher.handle_line(raw), {"ok": False, "error": "invalid"})
        self.assertEqual(self.fake.calls(), [], "an invalid request must not reach the API")

    def test_lines_that_are_not_a_request(self):
        deep = f"{'[' * 100000}{']' * 100000}".encode()
        for raw in (b"", b"\n", b"not json", b"[]", b"1", b'"launch"', b"null", b"{}", b'{"op": 1}', b'{"op": []}', b'{"op": {}}', b'{"op": "nope"}', b"\xff\xfe", b'{"op":"info"', deep):
            with self.subTest(raw=raw[:20]):
                self.assertInvalid(raw)

    def test_a_duplicate_key_is_invalid(self):
        self.assertInvalid(b'{"op": "info", "op": "info"}')
        self.assertInvalid(b'{"op": "deny", "reason": "a", "reason": "b"}')

    def test_unknown_fields_are_rejected_in_every_operation(self):
        extras = {
            "launch": {"token": TOKEN, **launch_request()},
            "status": {"name": pod_name(1)},
            "terminate": {"name": pod_name(1)},
            "list": {},
            "info": {},
            "deny": {"reason": "isolation_failed"},
        }
        self.assertEqual(set(extras), set(launcher.OPS))
        for op, fields in extras.items():
            with self.subTest(op=op):
                self.assertInvalid(line({"op": op, **fields, "surprise": 1}))

    def test_the_image_namespace_and_caps_cannot_come_from_a_request(self):
        for name, value in (
            ("image", fake_kube.IMAGE),
            ("namespace", "kube-system"),
            ("max_pods", 1000),
            ("repo_cap", 1000),
            ("deadline_clamp", 99999),
            ("classes", ["standard"]),
            ("relay_url", "http://attacker.example.invalid:8090"),
            ("allowed_uid", 0),
            ("anthropic_key", "x"),
        ):
            with self.subTest(field=name):
                self.assertInvalid(line({"op": "launch", "token": TOKEN, **launch_request(), name: value}))

    def test_missing_fields_are_invalid(self):
        full = {"op": "launch", "token": TOKEN, **launch_request()}
        for name in full:
            with self.subTest(missing=name):
                self.assertInvalid(line({key: value for key, value in full.items() if key != name}))
        self.assertInvalid(line({"op": "status"}))
        self.assertInvalid(line({"op": "terminate"}))
        self.assertInvalid(line({"op": "deny"}))

    def test_wrong_types_and_values_in_every_launch_field_are_invalid(self):
        for name in (*podspec.REQUEST_FIELDS, "token"):
            for value in (None, True, 5, 1.5, [], {}, "", "bad value", "x" * 5000):
                if (name, value) == ("attempt", 5):
                    continue  # a valid attempt
                with self.subTest(field=name, value=repr(value)[:12]):
                    document = {"op": "launch", "token": TOKEN, **launch_request()}
                    document[name] = value
                    reply = self.launcher.handle_line(line(document))
                    self.assertEqual(reply, {"ok": False, "error": "invalid"})
        self.assertEqual(self.fake.calls(), [])

    def test_a_malformed_pod_name_or_uid_is_invalid(self):
        for name in ("", "factory-w-zz", "other-pod", pod_name(1).upper(), f"{pod_name(1)}-s", 5, None):
            self.assertInvalid(line({"op": "status", "name": name}))
            self.assertInvalid(line({"op": "terminate", "name": name}))
        for uid in ("", "not-a-uid", 5, None):
            self.assertInvalid(line({"op": "status", "name": pod_name(1), "uid": uid}))

    def test_the_error_codes_of_both_sides_are_the_same_closed_set(self):
        self.assertEqual(launcher.ERROR_CODES, launcher_client.ERROR_CODES)
        self.assertEqual(podspec.REQUEST_FIELDS, launcher_client.REQUEST_FIELDS)
        self.assertEqual(launcher.MAX_REPLY_BYTES, launcher_client.MAX_REPLY_BYTES)


# --- Launch ---------------------------------------------------------------------------------------------------------------


class LaunchTests(Harness):
    def test_a_launch_creates_the_pod_then_the_secret_with_an_owner_reference(self):
        reply = self.assertLaunched(self.launch())
        pod = self.fake.pods()[pod_name(1)]
        secret = self.fake.secrets()[pc.secret_name(pod_name(1))]
        self.assertEqual(reply["uid"], pod["metadata"]["uid"])
        self.assertEqual(
            secret["metadata"]["ownerReferences"], [{"apiVersion": "v1", "kind": "Pod", "name": pod_name(1), "uid": pod["metadata"]["uid"]}]
        )
        self.assertEqual([call.op for call in self.fake.calls() if call.op.startswith("create")], ["create_pod", "create_secret"])

    def test_the_secret_holds_the_relay_token_and_the_launchers_own_key(self):
        self.launch()
        data = self.fake.secrets()[pc.secret_name(pod_name(1))]["data"]
        decoded = {name: base64.b64decode(value).decode() for name, value in data.items()}
        self.assertEqual(decoded, {pc.SECRET_KEY_TOKEN: TOKEN, pc.SECRET_KEY_ANTHROPIC: KEY})

    def test_the_pod_is_the_template_built_from_the_launchers_environment(self):
        self.launch()
        pod = self.fake.pods()[pod_name(1)]
        container = pod["spec"]["containers"][0]
        self.assertEqual(container["image"], self.config.image)
        self.assertEqual(pod["metadata"]["namespace"], pc.WORKER_NAMESPACE)
        self.assertEqual(podspec.check_policy(self.fake.calls("create_pod")[0].body, self.config.image), [])
        env = {entry["name"]: entry.get("value") for entry in container["env"]}
        self.assertEqual(env["FACTORY_RELAY_URL"], RELAY_URL)
        self.assertEqual(env["FACTORY_API_PUBLIC_HOST"], API_HOST)

    def test_neither_secret_value_is_in_the_reply_the_pod_or_the_log(self):
        reply = self.launch()
        self.assertNoSecrets(json.dumps(reply), json.dumps(self.fake.pods()), self.logs)
        self.assertEqual(self.logs, ["launched"])

    def test_a_pod_name_that_exists_is_a_conflict_and_the_other_pod_is_left_alone(self):
        self.assertLaunched(self.launch())
        uid = self.fake.pods()[pod_name(1)]["metadata"]["uid"]
        reply = self.launch()
        self.assertEqual(reply, {"ok": False, "error": "conflict"})
        self.assertEqual(self.fake.pods()[pod_name(1)]["metadata"]["uid"], uid)
        self.assertEqual(self.fake.calls("delete_pod"), [])

    def test_an_admission_denial_is_rejected_with_the_rule_ids_and_nothing_is_left(self):
        self.fake.admit_image = f"registry.example.invalid/factory@sha256:{'f' * 64}"
        reply = self.launch()
        self.assertEqual((reply["ok"], reply["error"]), (False, "rejected"))
        self.assertEqual(reply["rules"], ["image"])
        self.assertEqual((self.fake.pods(), self.fake.secrets()), ({}, {}))
        self.assertEqual(self.fake.calls("delete_pod"), [], "a pod the API refused is not ours to delete")

    def test_an_api_server_that_cannot_list_is_unavailable_and_creates_nothing(self):
        self.fake.inject("list_pods", times=10, status=500)
        self.assertEqual(self.launch(), {"ok": False, "error": "unavailable"})
        self.assertEqual(self.fake.calls("create_pod"), [])


# --- Compensation --------------------------------------------------------------------------------------------------------------


class CompensationTests(Harness):
    def assertNothingLeft(self):
        self.assertEqual((self.fake.pods(), self.fake.secrets()), ({}, {}))

    def test_a_secret_refused_after_the_pod_exists_deletes_the_pod_and_tries_the_secret_by_name(self):
        self.fake.inject("create_secret", times=1, status=403)
        self.assertEqual(self.launch(), {"ok": False, "error": "failed"})
        self.assertNothingLeft()
        deletes = self.fake.calls("delete_pod") + self.fake.calls("delete_secret")
        self.assertEqual(
            [(call.op, call.path.rsplit("/", 1)[1], call.status) for call in deletes],
            [("delete_pod", pod_name(1), 200), ("delete_secret", pc.secret_name(pod_name(1)), 404)],
        )

    def test_a_secret_that_is_unavailable_after_the_retries_deletes_the_pod(self):
        self.fake.inject("create_secret", times=10, status=503)
        self.assertEqual(self.launch(), {"ok": False, "error": "unavailable"})
        self.assertNothingLeft()

    def test_a_secret_denied_by_admission_deletes_the_pod_and_reports_the_rule(self):
        body = {"kind": "Status", "status": "Failure", "code": 422, "message": "denied [secret-owner]"}
        self.fake.inject("create_secret", times=1, status=422, body=body)
        reply = self.launch()
        self.assertEqual((reply["error"], reply["rules"]), ("rejected", ["secret-owner"]))
        self.assertNothingLeft()

    def test_a_create_that_was_sent_and_dropped_may_have_landed_so_the_pod_is_deleted_by_its_derived_name(self):
        self.fake.inject("create_pod", times=1, status=None)  # a POST that was sent is not retried
        self.assertEqual(self.launch(), {"ok": False, "error": "unavailable"})
        self.assertEqual([(call.path.rsplit("/", 1)[1], call.status) for call in self.fake.calls("delete_pod")], [(pod_name(1), 404)])

    def test_a_create_answered_500_may_have_landed_so_the_pod_is_deleted_by_its_derived_name(self):
        self.fake.inject("create_pod", times=10, status=500)
        self.assertEqual(self.launch(), {"ok": False, "error": "unavailable"})
        self.assertEqual([call.status for call in self.fake.calls("delete_pod")], [404])

    def test_a_5xx_that_landed_then_a_409_on_the_retry_deletes_the_pod_and_the_secret(self):
        for status in (500, 502, 504):
            with self.subTest(status=status):
                self.fake.requests.clear()
                self.fake.inject("create_pod", times=1, status=status, apply=True)  # persisted, answered 5xx
                self.assertEqual(self.launch(), {"ok": False, "error": "unavailable"})
                self.assertEqual([call.status for call in self.fake.calls("create_pod")], [status, 409])
                self.assertNothingLeft()
                self.assertEqual([call.path.rsplit("/", 1)[1] for call in self.fake.calls("delete_pod")], [pod_name(1)])
                self.assertEqual(
                    [call.path.rsplit("/", 1)[1] for call in self.fake.calls("delete_secret")], [pc.secret_name(pod_name(1))]
                )

    def test_a_dropped_connection_that_landed_is_not_retried_and_the_pod_is_deleted(self):
        self.fake.inject("create_pod", times=1, status=None, apply=True)
        self.assertEqual(self.launch(), {"ok": False, "error": "unavailable"})
        self.assertNothingLeft()

    def test_a_409_on_the_first_attempt_is_a_conflict_and_deletes_nothing(self):
        self.assertLaunched(self.launch())
        before = (dict(self.fake.pods()), dict(self.fake.secrets()))
        self.fake.requests.clear()
        self.assertEqual(self.launch(), {"ok": False, "error": "conflict"})
        self.assertEqual([call.status for call in self.fake.calls("create_pod")], [409])
        self.assertEqual(self.fake.calls("delete_pod") + self.fake.calls("delete_secret"), [])
        self.assertEqual((dict(self.fake.pods()), dict(self.fake.secrets())), before)

    def test_a_409_after_a_503_is_a_conflict_because_the_503_was_not_applied(self):
        self.assertLaunched(self.launch())
        self.fake.requests.clear()
        self.fake.inject("create_pod", times=1, status=503)
        self.assertEqual(self.launch(), {"ok": False, "error": "conflict"})
        self.assertEqual([call.status for call in self.fake.calls("create_pod")], [503, 409])
        self.assertEqual(self.fake.calls("delete_pod") + self.fake.calls("delete_secret"), [])
        self.assertEqual(len(self.fake.pods()), 1)

    def test_a_create_the_api_certainly_did_not_apply_deletes_nothing(self):
        self.fake.inject("create_pod", times=10, status=503)
        self.assertEqual(self.launch(), {"ok": False, "error": "unavailable"})
        self.assertEqual(self.fake.calls("delete_pod"), [])

    def test_a_slot_is_released_after_a_failed_launch(self):
        self.fake.inject("create_secret", times=1, status=403)
        self.launch()
        self.assertEqual(self.launcher.handle_line(line({"op": "info"}))["inflight"], 0)
        self.assertLaunched(self.launch(2), 2)

    def test_a_failed_compensation_is_logged_with_fixed_words_and_the_failure_still_answers(self):
        self.fake.inject("create_secret", times=1, status=403)
        self.fake.inject("delete_pod", times=4, status=500)
        reply = self.launch()
        self.assertEqual(reply, {"ok": False, "error": "failed"})
        self.assertIn("compensation failed to delete pod", self.logs)
        self.assertNoSecrets(self.logs, reply)

    def test_an_unexpected_bug_is_a_fixed_failure_that_names_only_the_type(self):
        with mock.patch.object(self.api, "create_secret", side_effect=RuntimeError(f"boom {KEY} {TOKEN}")):
            reply = self.launch()
        self.assertEqual(reply, {"ok": False, "error": "failed"})
        self.assertNothingLeft()
        self.assertEqual(self.logs[-1], "internal error RuntimeError")
        self.assertNoSecrets(self.logs, reply)


# --- Caps ----------------------------------------------------------------------------------------------------------------------


class CapTests(Harness):
    options = {"max_pods": 2, "repo_cap": 1}

    def test_the_live_pod_cap_answers_the_fixed_capacity_code_not_a_failure(self):
        self.assertLaunched(self.launch(1, repository="Org/one"), 1)
        self.assertLaunched(self.launch(2, repository="Org/two"), 2)
        self.assertEqual(self.launch(3, repository="Org/three"), {"ok": False, "error": "capacity"})
        self.assertEqual(len(self.fake.pods()), 2)
        self.assertEqual(self.logs.count("launch refused: capacity"), 1)

    def test_the_per_repository_cap(self):
        self.assertLaunched(self.launch(1, repository="Org/one"))
        self.assertEqual(self.launch(2, repository="Org/one"), {"ok": False, "error": "capacity"})
        self.assertEqual(self.launch(3, repository="org/ONE"), {"ok": False, "error": "capacity"}, "the key is case-folded")
        self.assertLaunched(self.launch(4, repository="Org/two"), 4)

    def test_a_deleted_pod_frees_its_slot(self):
        self.assertLaunched(self.launch(1, repository="Org/one"), 1)
        self.assertLaunched(self.launch(2, repository="Org/two"), 2)
        self.assertEqual(handle(self.launcher, {"op": "terminate", "name": pod_name(1)}), {"ok": True, "deleted": True})
        self.assertLaunched(self.launch(3, repository="Org/three"), 3)

    def test_the_cap_counts_pods_listed_in_the_namespace_whoever_launched_them(self):
        # Pods that an earlier launcher process created are in the API, not in this process: the listing counts them.
        for n in (1, 2):
            other = launcher.Launcher(self.api, self.config, clock=self.clock, log=noop)
            self.assertLaunched(other.handle_line(launch_line(n, repository=f"Org/r{n}")), n)
        self.assertEqual(self.launch(3, repository="Org/three"), {"ok": False, "error": "capacity"})

    def test_two_launches_at_the_same_moment_cannot_both_take_the_last_slot(self):
        gate = GatedKube(self.api)
        session = launcher.Launcher(gate, make_config(self.directory, max_pods=1, repo_cap=4), clock=self.clock, log=noop)
        first = []
        thread = threading.Thread(target=lambda: first.append(session.handle_line(launch_line(1, repository="Org/one"))))
        thread.start()
        self.assertTrue(gate.reached.wait(JOIN), "the first launch must be inside create_pod")
        # The pod of the first launch is not in the listing yet: only the in-flight count can refuse the second.
        self.assertEqual(self.fake.pods(), {})
        self.assertEqual(session.handle_line(launch_line(2, repository="Org/two")), {"ok": False, "error": "capacity"})
        gate.release.set()
        thread.join(JOIN)
        self.assertFalse(thread.is_alive())
        self.assertTrue(first[0]["ok"])
        self.assertEqual(session.handle_line(launch_line(3, repository="Org/three")), {"ok": False, "error": "capacity"})
        self.assertEqual(session.handle_line(line({"op": "info"}))["inflight"], 0)

    def test_a_launch_that_finishes_while_another_lists_still_counts_against_the_cap(self):
        # B's listing is taken while A is in flight and before A's pod exists; A then finishes, and only then does B
        # decide. The listing does not show A and A is no longer in flight: B must still be refused.
        wrapped = InterleavedKube(self.api)
        session = launcher.Launcher(wrapped, make_config(self.directory, max_pods=1, repo_cap=4), clock=self.clock, log=noop)
        replies = {}

        def launch(key, n):
            replies[key] = session.handle_line(launch_line(n, repository=f"Org/r{n}"))
            if key == "first":
                wrapped.first_done.set()

        first = threading.Thread(target=launch, args=("first", 1))
        first.start()
        self.assertTrue(wrapped.first_in_create.wait(JOIN), "the first launch must be admitted and about to create its pod")
        second = threading.Thread(target=launch, args=("second", 2))
        second.start()
        for thread in (first, second):
            thread.join(JOIN * 2)
            self.assertFalse(thread.is_alive())
        self.assertTrue(replies["first"]["ok"], replies)
        self.assertEqual(replies["second"], {"ok": False, "error": "capacity"})
        self.assertEqual(list(self.fake.pods()), [pod_name(1)], "the cap of one holds")
        self.assertEqual(session.handle_line(line({"op": "info"}))["inflight"], 0)

    def test_the_repository_cap_holds_for_launches_in_flight_too(self):
        gate = GatedKube(self.api)
        session = launcher.Launcher(gate, make_config(self.directory, max_pods=8, repo_cap=1), clock=self.clock, log=noop)
        first = []
        thread = threading.Thread(target=lambda: first.append(session.handle_line(launch_line(1, repository="Org/one"))))
        thread.start()
        self.assertTrue(gate.reached.wait(JOIN))
        self.assertEqual(session.handle_line(launch_line(2, repository="Org/one")), {"ok": False, "error": "capacity"})
        gate.release.set()
        thread.join(JOIN)
        self.assertFalse(thread.is_alive())

    def test_the_admission_counter_in_isolation(self):
        counter = launcher.Admission(2, 1)
        self.assertTrue(counter.try_admit("a", 0, 0))
        self.assertFalse(counter.try_admit("a", 0, 0), "same repository, cap 1")
        self.assertTrue(counter.try_admit("b", 0, 0))
        self.assertFalse(counter.try_admit("c", 0, 0), "two in flight, max 2")
        counter.release("a")
        counter.release("b")
        self.assertEqual(counter.inflight, 0)
        self.assertFalse(counter.try_admit("d", 2, 0), "two listed, max 2")

    def test_the_admission_snapshot_counts_a_launch_that_finished_after_it(self):
        counter = launcher.Admission(1, 4)
        self.assertTrue(counter.try_admit("a", 0, 0))
        before = counter.snapshot()  # taken before the listing, which does not show the pod of "a" yet
        counter.release("a")  # "a" finishes between that listing and the decision
        self.assertTrue(counter.try_admit("b", 0, 0), "without the snapshot the slot looks free (the race)")
        counter.release("b")
        self.assertFalse(counter.try_admit("b", 0, 0, before), "with it, the launch that finished is still counted")
        self.assertEqual(counter.inflight, 0)
        per_repo = launcher.Admission(8, 1)
        self.assertTrue(per_repo.try_admit("a", 0, 0))
        before = per_repo.snapshot()
        per_repo.release("a")
        self.assertFalse(per_repo.try_admit("a", 0, 0, before), "and per repository")
        self.assertTrue(per_repo.try_admit("other", 0, 0, before))


class GatedKube:
    """Delegates to a real client; the first create_pod waits until the test lets it go."""

    def __init__(self, client):
        self._client = client
        self.reached = threading.Event()
        self.release = threading.Event()

    def __getattr__(self, name):
        return getattr(self._client, name)

    def create_pod(self, pod):
        if not self.reached.is_set():
            self.reached.set()
            if not self.release.wait(JOIN):
                raise AssertionError("the test never released the gate")
        return self._client.create_pod(pod)


class InterleavedKube:
    """Delegates to a real client and orders two launches: the first launch's create_pod waits until the second
    launch has taken its listing, and that listing is not used until the first launch has finished."""

    def __init__(self, client):
        self._client = client
        self._mutex = threading.Lock()
        self._listings = 0
        self.first_in_create = threading.Event()
        self.second_listed = threading.Event()
        self.first_done = threading.Event()

    def __getattr__(self, name):
        return getattr(self._client, name)

    def list_pods(self, selector):
        with self._mutex:
            self._listings += 1
            second = self._listings == 2
        items = self._client.list_pods(selector)
        if second:
            self.second_listed.set()
            if not self.first_done.wait(JOIN):
                raise AssertionError("the first launch never finished")
        return items

    def create_pod(self, pod):
        self.first_in_create.set()
        if not self.second_listed.wait(JOIN):
            raise AssertionError("the second launch never listed")
        return self._client.create_pod(pod)


class DeadlineAndEnumTests(Harness):
    def active_deadline(self, n=1):
        return self.fake.pods()[pod_name(n)]["spec"]["activeDeadlineSeconds"]

    def test_a_timeout_above_the_clamp_is_reduced_and_the_answer_says_so(self):
        reply = self.assertLaunched(self.launch(timeout_seconds=86400))
        self.assertEqual((reply["timeout_seconds"], reply["clamped"]), (pc.DEADLINE_CLAMP_SECONDS, True))
        self.assertEqual(self.active_deadline(), pc.DEADLINE_CLAMP_SECONDS + pc.DEADLINE_MARGIN_SECONDS)

    def test_a_timeout_within_the_clamp_is_kept(self):
        reply = self.assertLaunched(self.launch(timeout_seconds=1800))
        self.assertEqual((reply["timeout_seconds"], reply["clamped"]), (1800, False))
        self.assertEqual(self.active_deadline(), 1800 + pc.DEADLINE_MARGIN_SECONDS)

    def test_a_timeout_below_the_minimum_is_invalid(self):
        for seconds in (0, 1, launcher.MIN_TIMEOUT_SECONDS - 1, -5, True):
            self.assertEqual(self.launch(timeout_seconds=seconds), {"ok": False, "error": "invalid"})
        self.assertLaunched(self.launch(timeout_seconds=launcher.MIN_TIMEOUT_SECONDS))

    def test_the_launchers_own_clamp_is_the_one_that_applies(self):
        session = launcher.Launcher(self.api, make_config(self.directory, deadline_clamp=3600), clock=self.clock, log=noop)
        reply = session.handle_line(launch_line(timeout_seconds=7200))
        self.assertEqual((reply["timeout_seconds"], reply["clamped"]), (3600, True))
        self.assertEqual(self.active_deadline(), 3600 + pc.DEADLINE_MARGIN_SECONDS)

    def test_the_class_enum(self):
        self.assertEqual(self.launch(resource_class="huge"), {"ok": False, "error": "invalid"})
        self.assertLaunched(self.launch(1, resource_class="small"))
        only_standard = launcher.Launcher(self.api, make_config(self.directory, classes=("standard",)), clock=self.clock, log=noop)
        self.assertEqual(only_standard.handle_line(launch_line(2, resource_class="small")), {"ok": False, "error": "invalid"})

    def test_the_phase_enum(self):
        for phase in pc.PHASES:
            self.assertLaunched(self.launch(1 + pc.PHASES.index(phase), repository=f"Org/{phase}", phase=phase), 1 + pc.PHASES.index(phase))
        self.assertEqual(self.launch(9, phase="deploy"), {"ok": False, "error": "invalid"})

    def test_a_bad_request_never_reaches_the_api(self):
        self.launch(resource_class="huge")
        self.launch(timeout_seconds=5)
        self.launch(phase="deploy")
        self.assertEqual(self.fake.calls(), [])


class DenyTests(Harness):
    def test_deny_refuses_every_launch_until_restart_and_nothing_is_created(self):
        self.assertLaunched(self.launch(1))
        self.assertEqual(handle(self.launcher, {"op": "deny", "reason": "isolation_failed"}), {"ok": True})
        before = len(self.fake.calls())
        self.assertEqual(self.launch(2), {"ok": False, "error": "denied"})
        self.assertEqual(len(self.fake.calls()), before, "a denied launch makes no API call")
        self.assertEqual(self.logs[-1], "launches denied reason=isolation_failed")

    def test_the_other_operations_keep_working_when_denied(self):
        self.assertLaunched(self.launch(1))
        handle(self.launcher, {"op": "deny", "reason": "isolation_failed"})
        self.assertEqual(handle(self.launcher, {"op": "status", "name": pod_name(1)})["ok"], True)
        self.assertEqual(handle(self.launcher, {"op": "list"})["ok"], True)
        self.assertEqual(handle(self.launcher, {"op": "terminate", "name": pod_name(1)})["deleted"], True)

    def test_a_reason_that_is_not_a_fixed_word_is_not_logged_but_still_denies(self):
        handle(self.launcher, {"op": "deny", "reason": f"free text {KEY}\nwith a newline"})
        self.assertEqual(self.launch(1), {"ok": False, "error": "denied"})
        info = handle(self.launcher, {"op": "info"})
        self.assertEqual((info["denied"], info["deny_reason"]), (True, "unspecified"))
        self.assertNoSecrets(self.logs, info)

    def test_deny_is_sticky_and_the_first_reason_stays(self):
        handle(self.launcher, {"op": "deny", "reason": "first_reason"})
        handle(self.launcher, {"op": "deny", "reason": "second_reason"})
        self.assertEqual(handle(self.launcher, {"op": "info"})["deny_reason"], "first_reason")
        self.assertEqual(self.logs, ["launches denied reason=first_reason"])


# --- Status, terminate, list, info ----------------------------------------------------------------------------------------------


class StatusTests(Harness):
    def test_status_classifies_with_the_slice_7_table(self):
        for n, scenario, state, final in (
            (1, "succeeds", "unschedulable", False),
            (2, "exit_nonzero", "running", False),
            (3, "oom", "running", False),
            (4, "evicted", "running", False),
        ):
            self.fake.script(pod_name(n), scenario)
            self.assertLaunched(self.launch(n, repository=f"Org/r{n}"), n)
            reply = handle(self.launcher, {"op": "status", "name": pod_name(n)})
            self.assertEqual((reply["state"], reply["final"]), (state, final))
        self.clock.advance(100000)
        states = {n: handle(self.launcher, {"op": "status", "name": pod_name(n)})["state"] for n in (2, 3, 4)}
        self.assertEqual(states, {2: "failed_exit", 3: "oom", 4: "evicted"})
        self.assertEqual(handle(self.launcher, {"op": "status", "name": pod_name(2)})["exit_code"], 2)

    def test_a_pod_that_vanished_is_gone_never_succeeded(self):
        self.fake.script(pod_name(1), "node_deleted")
        self.assertLaunched(self.launch(1))
        self.clock.advance(60)
        reply = handle(self.launcher, {"op": "status", "name": pod_name(1)})
        self.assertEqual((reply["state"], reply["final"]), ("gone", True))

    def test_status_says_whether_the_pod_is_still_there_and_whether_it_is_terminating(self):
        self.fake.script(pod_name(1), (Step(0, fake_kube.running()),))
        reply = self.assertLaunched(self.launch(1))
        status = {"op": "status", "name": pod_name(1), "uid": reply["uid"]}
        running = handle(self.launcher, status)
        self.assertEqual((running["state"], running["final"], running["present"], running["terminating"]), ("running", False, True, False))
        self.assertEqual(handle(self.launcher, {"op": "terminate", "name": pod_name(1)}), {"ok": True, "deleted": True})
        # The delete is accepted; the pod is still in the API for its grace period, its containers running.
        self.assertIn(pod_name(1), self.fake.pods())
        going = handle(self.launcher, status)
        self.assertEqual((going["state"], going["detail"], going["final"], going["present"], going["terminating"]), ("evicted", "terminating", True, True, True))
        self.clock.advance(pc.TERMINATION_GRACE_SECONDS)
        gone = handle(self.launcher, status)
        self.assertEqual((gone["state"], gone["final"], gone["present"], gone["terminating"]), ("gone", True, False, False))

    def test_a_pod_that_is_over_is_present_and_not_terminating(self):
        self.assertLaunched(self.launch(1))
        self.clock.advance(250)  # the scenario "succeeds" is over at 200 s
        reply = handle(self.launcher, {"op": "status", "name": pod_name(1)})
        self.assertEqual((reply["state"], reply["final"], reply["present"], reply["terminating"]), ("succeeded", True, True, False))

    def test_status_with_another_uid_is_gone(self):
        reply = self.assertLaunched(self.launch(1))
        same = handle(self.launcher, {"op": "status", "name": pod_name(1), "uid": reply["uid"]})
        other = handle(self.launcher, {"op": "status", "name": pod_name(1), "uid": "11111111-1111-4111-8111-111111111111"})
        self.assertEqual((same["state"], other["state"], other["detail"]), ("unschedulable", "gone", "uid_mismatch"))
        self.assertEqual((same["present"], other["present"], other["terminating"]), (True, False, False))

    def test_status_when_the_api_is_down_is_unavailable_not_a_guess(self):
        self.assertLaunched(self.launch(1))
        self.fake.inject("get_pod", times=10, status=500)
        self.assertEqual(handle(self.launcher, {"op": "status", "name": pod_name(1)}), {"ok": False, "error": "unavailable"})

    def test_terminate_deletes_the_pod_and_the_secret_and_a_missing_pod_is_fine(self):
        self.assertLaunched(self.launch(1))
        self.assertEqual(handle(self.launcher, {"op": "terminate", "name": pod_name(1)}), {"ok": True, "deleted": True})
        self.assertEqual((self.fake.pods(), self.fake.secrets()), ({}, {}))
        self.assertEqual(handle(self.launcher, {"op": "terminate", "name": pod_name(1)}), {"ok": True, "deleted": False})

    def test_terminate_with_another_uid_leaves_the_pod_alone(self):
        self.assertLaunched(self.launch(1))
        reply = handle(self.launcher, {"op": "terminate", "name": pod_name(1), "uid": "11111111-1111-4111-8111-111111111111"})
        self.assertEqual(reply, {"ok": True, "deleted": False})
        self.assertIn(pod_name(1), self.fake.pods())

    def test_terminate_when_the_api_is_down_is_unavailable(self):
        self.assertLaunched(self.launch(1))
        self.fake.inject("delete_pod", times=10, status=500)
        self.assertEqual(handle(self.launcher, {"op": "terminate", "name": pod_name(1)}), {"ok": False, "error": "unavailable"})

    def test_list_reports_validated_fields_only(self):
        reply = self.assertLaunched(self.launch(1))
        listing = handle(self.launcher, {"op": "list"})
        self.assertEqual(listing["truncated"], False)
        (pod,) = listing["pods"]
        self.assertEqual(
            pod,
            {
                "name": pod_name(1),
                "uid": reply["uid"],
                "state": "unschedulable",
                "final": False,
                "terminating": False,
                "manager": fake_kube.INSTANCE,
                "created": int(self.clock()),
                "deadline": int(self.clock()) + 1800 + pc.DEADLINE_MARGIN_SECONDS,
            },
        )

    def test_list_ignores_a_pod_it_cannot_manage(self):
        foreign = fake_kube.worker_pod(1)
        foreign["metadata"] = {**foreign["metadata"], "name": "not-a-worker-pod", "uid": "11111111-1111-4111-8111-111111111111"}
        self.fake._pods[(pc.WORKER_NAMESPACE, "not-a-worker-pod")] = fake_kube._Pod(foreign, 0.0, ())
        self.assertEqual(handle(self.launcher, {"op": "list"})["pods"], [])
        self.assertEqual(handle(self.launcher, {"op": "info"})["live"], 1, "the cap still counts it")

    def test_info_reports_caps_live_count_and_deny_state(self):
        self.assertLaunched(self.launch(1))
        info = handle(self.launcher, {"op": "info"})
        self.assertEqual(
            info,
            {
                "ok": True,
                "max_pods": 8,
                "repo_cap": 4,
                "deadline_clamp": pc.DEADLINE_CLAMP_SECONDS,
                "min_timeout": launcher.MIN_TIMEOUT_SECONDS,
                "classes": ["standard", "small"],
                "live": 1,
                "inflight": 0,
                "denied": False,
                "deny_reason": None,
                "reaped": {},
            },
        )

    def test_info_still_answers_when_the_api_cannot_list(self):
        self.fake.inject("list_pods", times=10, status=500)
        self.assertIsNone(handle(self.launcher, {"op": "info"})["live"])


# --- The scheduling timeout, on a fake clock ---------------------------------------------------------------------------------------


class SweepTests(Harness):
    def gone(self, n=1):
        return pod_name(n) not in self.fake.pods()

    def reaped_state(self, n=1):
        return handle(self.launcher, {"op": "status", "name": pod_name(n)})

    def test_a_pod_pending_for_600_seconds_is_deleted_and_reported_unschedulable(self):
        self.fake.script(pod_name(1), "unschedulable")
        self.assertLaunched(self.launch(1))
        self.clock.advance(pc.SCHEDULING_TIMEOUT_SECONDS - 1)
        self.assertEqual(self.launcher.sweep(), 0)
        self.assertFalse(self.gone())
        self.clock.advance(1)
        self.assertEqual(self.launcher.sweep(), 1)
        self.assertTrue(self.gone())
        self.assertEqual(self.fake.secrets(), {})
        reply = self.reaped_state()
        self.assertEqual((reply["state"], reply["detail"], reply["final"]), ("unschedulable", "scheduling_timeout", True))
        self.assertEqual(self.logs[-1], "reaped reason=scheduling_timeout")

    def test_a_pod_waiting_for_its_secret_or_image_counts_as_pending(self):
        self.fake.script(pod_name(1), (Step(0, fake_kube.pending_waiting("ContainerCreating")),))
        self.assertLaunched(self.launch(1))
        self.clock.advance(pc.SCHEDULING_TIMEOUT_SECONDS)
        self.assertEqual(self.launcher.sweep(), 1)

    def test_a_running_pod_is_not_subject_to_the_scheduling_timeout(self):
        self.fake.script(pod_name(1), (Step(0, fake_kube.running()),))
        self.assertLaunched(self.launch(1))
        self.clock.advance(pc.SCHEDULING_TIMEOUT_SECONDS * 2)
        self.assertEqual(self.launcher.sweep(), 0)
        self.assertFalse(self.gone())

    def test_a_pod_that_outlives_its_deadline_plus_the_grace_is_deleted(self):
        self.fake.script(pod_name(1), (Step(0, fake_kube.running()),))
        self.assertLaunched(self.launch(1, timeout_seconds=60))
        limit = 60 + pc.DEADLINE_MARGIN_SECONDS + pc.TERMINATION_GRACE_SECONDS
        self.clock.advance(limit - 1)
        self.assertEqual(self.launcher.sweep(), 0)
        self.clock.advance(1)
        self.assertEqual(self.launcher.sweep(), 1)
        self.assertIn(pod_name(1), self.fake.pods(), "a running pod is terminating for its grace period, not gone yet")
        reply = self.reaped_state()
        self.assertEqual((reply["state"], reply["detail"], reply["final"]), ("deadline", "launcher_backstop", True))
        self.assertEqual((reply["present"], reply["terminating"]), (True, True), "still there, and its containers may run")
        self.clock.advance(pc.TERMINATION_GRACE_SECONDS)
        reply = self.reaped_state()
        self.assertEqual((reply["state"], reply["present"], reply["terminating"]), ("deadline", False, False), "the reason survives the pod vanishing")

    def test_the_deadline_backstop_also_removes_a_finished_pod_nobody_collected(self):
        self.assertLaunched(self.launch(1, timeout_seconds=60))
        self.clock.advance(250)  # the scenario "succeeds" is over at 200 s
        self.assertEqual(handle(self.launcher, {"op": "status", "name": pod_name(1)})["state"], "succeeded")
        self.assertEqual(self.launcher.sweep(), 0, "a finished pod stays until the supervisor reads it")
        self.clock.advance(60 + pc.DEADLINE_MARGIN_SECONDS + pc.TERMINATION_GRACE_SECONDS)
        self.assertEqual(self.launcher.sweep(), 1)

    def test_an_image_pull_error_for_120_seconds_is_deleted_and_reported_starting_error(self):
        self.fake.script(pod_name(1), "image_pull_backoff")
        self.assertLaunched(self.launch(1))
        self.clock.advance(10)  # ImagePullBackOff from 10 s on
        self.assertEqual(self.launcher.sweep(), 0, "the window starts when the error is first seen")
        self.clock.advance(pc.PULL_ERROR_SECONDS - 1)
        self.assertEqual(self.launcher.sweep(), 0)
        self.clock.advance(1)
        self.assertEqual(self.launcher.sweep(), 1)
        self.assertTrue(self.gone())
        reply = self.reaped_state()
        self.assertEqual((reply["state"], reply["detail"], reply["final"]), ("starting_error", "pull_or_config_error", True))

    def test_a_config_error_is_treated_like_a_pull_error(self):
        self.fake.script(pod_name(1), "config_error")
        self.assertLaunched(self.launch(1))
        self.clock.advance(3)
        self.launcher.sweep()
        self.clock.advance(pc.PULL_ERROR_SECONDS)
        self.assertEqual(self.launcher.sweep(), 1)

    def test_an_error_that_clears_restarts_its_window(self):
        steps = (
            Step(0, fake_kube.pending_waiting("ImagePullBackOff")),
            Step(60, fake_kube.running()),
            Step(100, fake_kube.pending_waiting("ErrImagePull")),
        )
        self.fake.script(pod_name(1), steps)
        self.assertLaunched(self.launch(1))
        self.clock.advance(1)
        self.launcher.sweep()  # error seen at +1
        self.clock.advance(70)
        self.assertEqual(self.launcher.sweep(), 0)  # running at +71: the window is closed
        self.clock.advance(35)
        self.assertEqual(self.launcher.sweep(), 0)  # the error is back at +106, first seen again
        self.clock.advance(pc.PULL_ERROR_SECONDS - 1)
        self.assertEqual(self.launcher.sweep(), 0)
        self.clock.advance(1)
        self.assertEqual(self.launcher.sweep(), 1)

    def test_a_sweep_that_cannot_list_deletes_nothing(self):
        self.fake.script(pod_name(1), "unschedulable")
        self.assertLaunched(self.launch(1))
        self.clock.advance(100000)
        self.fake.inject("list_pods", times=10, status=500)
        with self.assertRaises(kube.Unavailable):
            self.launcher.sweep()
        self.assertEqual(self.fake.calls("delete_pod"), [])
        self.assertFalse(self.gone())

    def test_a_failed_deletion_is_retried_by_the_next_pass_and_not_remembered(self):
        self.fake.script(pod_name(1), "unschedulable")
        self.assertLaunched(self.launch(1))
        self.clock.advance(pc.SCHEDULING_TIMEOUT_SECONDS)
        self.fake.inject("delete_pod", times=4, status=500)
        self.assertEqual(self.launcher.sweep(), 0)
        self.assertEqual(self.logs[-1], "reaping failed reason=scheduling_timeout")
        self.assertEqual(self.reaped_state()["state"], "unschedulable")  # the pod is still there, still pending
        self.assertEqual(self.launcher.sweep(), 1)

    def test_a_pod_that_is_already_terminating_is_left_to_the_kubelet(self):
        self.fake.script(pod_name(1), "karpenter_eviction")
        self.assertLaunched(self.launch(1))
        self.clock.advance(45)  # deletionTimestamp is set at 30 s, the pod vanishes at 60 s
        self.assertEqual(self.launcher.sweep(), 0)
        self.assertEqual(self.fake.calls("delete_pod"), [])

    def test_info_counts_what_the_launcher_reaped(self):
        self.fake.script(pod_name(1), "unschedulable")
        self.assertLaunched(self.launch(1))
        self.clock.advance(pc.SCHEDULING_TIMEOUT_SECONDS)
        self.launcher.sweep()
        self.assertEqual(handle(self.launcher, {"op": "info"})["reaped"], {"scheduling_timeout": 1})

    def test_the_memory_of_reaped_pods_is_bounded_and_forgets_the_oldest_first(self):
        self.fake.scenario = "unschedulable"
        for n in range(1, 6):
            self.assertLaunched(self.launch(n, repository=f"Org/r{n}"), n)
        self.clock.advance(pc.SCHEDULING_TIMEOUT_SECONDS)
        with mock.patch.object(launcher, "MAX_REMEMBERED", 3):
            self.assertEqual(self.launcher.sweep(), 5)
        states = {n: self.reaped_state(n)["state"] for n in range(1, 6)}
        self.assertEqual(states, {1: "gone", 2: "gone", 3: "unschedulable", 4: "unschedulable", 5: "unschedulable"})

    def test_the_sweeper_loop_survives_every_kind_of_failure(self):
        stop = threading.Event()
        lines = []
        fake = ScriptedSweeps(stop, [kube.Unavailable(), RuntimeError(f"bug {KEY}"), 3])
        worker = threading.Thread(target=launcher._sweeper, args=(fake, stop, lines.append, 0))
        worker.start()
        worker.join(JOIN)
        self.assertFalse(worker.is_alive())
        self.assertEqual(fake.calls, 3)
        self.assertEqual(lines, ["sweep failed unavailable", "sweep failed RuntimeError", "sweep deleted=3"])
        self.assertNoSecrets(lines)


class ScriptedSweeps:
    """A launcher stand-in whose sweep() returns or raises the scripted outcomes, then stops the loop."""

    def __init__(self, stop, outcomes):
        self.stop = stop
        self.outcomes = outcomes
        self.calls = 0

    def sweep(self):
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if self.calls == len(self.outcomes):
            self.stop.set()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


# --- Transport: the unit of one connection --------------------------------------------------------------------------------------------


class FakeConn:
    """A connected socket stand-in: a peer uid, the chunks it delivers, and what was asked of it."""

    def __init__(self, uid=None, chunks=(), clock=None, tick=0.0):
        self.uid = os.getuid() if uid is None else uid
        self.chunks = iter(chunks)
        self.clock = clock
        self.tick = tick
        self.reads = 0
        self.sent = []
        self.closed = False

    def getsockopt(self, _level, _option, _size):
        return struct.pack("3i", 1, self.uid, 1)

    def settimeout(self, _timeout):
        return None

    def recv(self, size):
        self.reads += 1
        if self.clock is not None:
            self.clock.advance(self.tick)
        data = next(self.chunks, b"")
        return data[:size]

    def send(self, data):
        self.sent.append(bytes(data))
        return len(data)

    def close(self):
        self.closed = True

    def reply(self):
        return json.loads(b"".join(self.sent))


def spy_handler(replies=None):
    calls = []

    def handler(raw):
        calls.append(raw)
        return replies or {"ok": True}

    handler.calls = calls
    return handler


class ConnectionTests(unittest.TestCase):
    limits = launcher.Limits(request_timeout=5.0, max_request_bytes=1024)

    def serve(self, conn, handler=None, allowed=None, clock=None):
        used = handler or spy_handler()
        uid = os.getuid() if allowed is None else allowed
        return launcher.serve_connection(conn, used, uid, self.limits, clock or FakeClock()), used

    def test_a_foreign_peer_is_refused_without_reading_a_byte(self):
        conn = FakeConn(uid=FOREIGN, chunks=[b'{"op":"info"}\n'])
        outcome, handler = self.serve(conn)
        self.assertEqual(outcome, "refused")
        self.assertEqual((conn.reads, conn.sent, handler.calls), (0, [], []))

    def test_a_peer_whose_credentials_cannot_be_read_is_refused(self):
        conn = FakeConn(chunks=[b'{"op":"info"}\n'])
        with mock.patch.object(conn, "getsockopt", side_effect=OSError("no credentials")):
            outcome, handler = self.serve(conn)
        self.assertEqual((outcome, conn.reads, handler.calls), ("refused", 0, []))

    def test_a_short_credential_answer_is_refused(self):
        conn = FakeConn(chunks=[b'{"op":"info"}\n'])
        with mock.patch.object(conn, "getsockopt", return_value=b"\x01"):
            outcome, _handler = self.serve(conn)
        self.assertEqual(outcome, "refused")

    def test_the_allowed_peer_is_served_one_line_and_gets_one_line_back(self):
        conn = FakeConn(chunks=[b'{"op":"in', b'fo"}\nIGNORED'])
        outcome, handler = self.serve(conn, spy_handler({"ok": True, "x": 1}))
        self.assertEqual(outcome, "served")
        self.assertEqual(handler.calls, [b'{"op":"info"}'])
        self.assertEqual(b"".join(conn.sent), b'{"ok":true,"x":1}\n')

    def test_a_huge_line_is_cut_off_at_the_bound_and_does_not_grow_memory(self):
        conn = FakeConn(chunks=iter(lambda: b"a" * 4096, None))
        outcome, handler = self.serve(conn)
        self.assertEqual(outcome, "too_large")
        self.assertEqual(conn.reply(), {"ok": False, "error": "too_large"})
        self.assertEqual(handler.calls, [])
        self.assertLessEqual(conn.reads, self.limits.max_request_bytes // 1000 + 2)

    def test_a_line_of_exactly_the_bound_is_served_and_one_byte_more_is_not(self):
        exact = FakeConn(chunks=[b"a" * self.limits.max_request_bytes, b"\n"])
        self.assertEqual(self.serve(exact)[0], "served")
        over = FakeConn(chunks=[b"a" * (self.limits.max_request_bytes + 1), b"\n"])
        self.assertEqual(self.serve(over)[0], "too_large")

    def test_a_slow_loris_is_cut_off_at_the_total_deadline_however_slowly_it_trickles(self):
        clock = FakeClock()
        conn = FakeConn(chunks=iter(lambda: b"x", None), clock=clock, tick=1.0)  # one byte per second, forever
        outcome, handler = self.serve(conn, clock=clock)
        self.assertEqual(outcome, "timeout")
        self.assertEqual(conn.reply(), {"ok": False, "error": "timeout"})
        self.assertEqual(handler.calls, [])
        self.assertLessEqual(conn.reads, 6)

    def test_a_peer_that_closes_before_the_newline_is_not_served_or_answered(self):
        conn = FakeConn(chunks=[b'{"op":"info"}'])
        outcome, handler = self.serve(conn)
        self.assertEqual((outcome, conn.sent, handler.calls), ("incomplete", [], []))


# --- Transport: real sockets ----------------------------------------------------------------------------------------------------------


class ServerHarness(Harness):
    def start_server(self, session=None, **kwargs):
        limits = kwargs.pop("limits", None)
        server = launcher.Server(
            session or self.launcher,
            self.config.socket_path,
            kwargs.pop("allowed_uid", os.getuid()),
            limits=limits,
            log=self.logs.append,
            **kwargs,
        ).start()
        self.addCleanup(self.close_server, server)
        return server

    def close_server(self, server):
        server.close()
        self.assertEqual([t.name for t in threading.enumerate() if t.name.startswith("launcher-")], [], "a launcher thread outlived close()")

    def raw(self, payload, timeout=JOIN):
        """Send `payload` on a fresh connection and return everything the server answers until it closes."""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(self.config.socket_path)
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):  # a server that drops us first
                sock.sendall(payload)
            chunks = []
            with contextlib.suppress(ConnectionResetError):
                data = sock.recv(4096)
                while data:
                    chunks.append(data)
                    data = sock.recv(4096)
            return b"".join(chunks)

    def client(self, **options):
        return launcher_client.LauncherClient(self.config.socket_path, launcher_uid=os.getuid(), sleep=noop, **options)


class ServerTests(ServerHarness):
    def test_the_socket_is_created_0660_and_is_a_socket_of_ours(self):
        self.start_server()
        info = os.lstat(self.config.socket_path)
        self.assertTrue(stat.S_ISSOCK(info.st_mode))
        self.assertEqual((stat.S_IMODE(info.st_mode), info.st_uid), (0o660, os.getuid()))

    def test_close_removes_the_socket(self):
        server = self.start_server()
        server.close()
        self.assertFalse(os.path.lexists(self.config.socket_path))

    def test_the_allowed_uid_is_served_over_a_real_socket_with_real_peercred(self):
        self.start_server()
        reply = json.loads(self.raw(b'{"op":"info"}\n'))
        self.assertEqual((reply["ok"], reply["max_pods"]), (True, 8))

    def test_a_foreign_uid_is_dropped_and_the_launcher_is_never_asked(self):
        session = mock.Mock(wraps=self.launcher)
        self.start_server(session, allowed_uid=FOREIGN)
        self.assertEqual(self.raw(b'{"op":"info"}\n'), b"", "nothing is answered to a foreign peer")
        self.assertEqual(session.handle_line.call_count, 0)
        self.assertEqual(self.fake.calls(), [])

    def test_a_foreign_uid_cannot_launch(self):
        self.start_server(allowed_uid=FOREIGN)
        self.assertEqual(self.raw(b"".join((launch_line(1), b"\n"))), b"")
        self.assertEqual((self.fake.pods(), self.fake.secrets()), ({}, {}))

    def test_a_malformed_request_over_the_socket_is_invalid_and_a_huge_one_is_cut_off(self):
        self.start_server()
        self.assertEqual(json.loads(self.raw(b'{"op":"nope"}\n')), {"ok": False, "error": "invalid"})
        self.assertEqual(json.loads(self.raw(b"x" * (launcher.MAX_REQUEST_BYTES + 100))), {"ok": False, "error": "too_large"})

    def test_a_slow_client_is_cut_off_by_the_request_deadline(self):
        self.start_server(limits=launcher.Limits(request_timeout=0.3))
        self.assertEqual(json.loads(self.raw(b'{"op":')), {"ok": False, "error": "timeout"})

    def test_connections_beyond_the_bound_are_answered_busy_at_once_and_hold_no_thread(self):
        entered = threading.Semaphore(0)
        release = threading.Event()

        class Blocking:
            def handle_line(self, _raw):
                entered.release()
                release.wait(JOIN)
                return {"ok": True}

        self.start_server(Blocking(), limits=launcher.Limits(max_connections=2))
        clients = []
        for _ in range(2):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(JOIN)
            sock.connect(self.config.socket_path)
            sock.sendall(b'{"op":"info"}\n')
            clients.append(sock)
        for _ in range(2):
            self.assertTrue(entered.acquire(timeout=JOIN), "both handlers must be running")
        self.assertEqual(json.loads(self.raw(b'{"op":"info"}\n')), {"ok": False, "error": "busy"})
        release.set()
        for sock in clients:
            self.assertEqual(json.loads(sock.recv(4096)), {"ok": True})
            sock.close()

    def test_a_foreign_peer_at_max_connections_takes_no_thread_and_is_not_told_busy(self):
        server = launcher.Server(self.launcher, self.config.socket_path, os.getuid(), limits=launcher.Limits(max_connections=2), log=self.logs.append)
        server._connections = {FakeConn(), FakeConn()}  # the bound is reached
        with mock.patch.object(launcher.threading, "Thread") as thread:
            allowed = FakeConn()
            server._dispatch(allowed)
            self.assertEqual(json.loads(b"".join(allowed.sent)), {"ok": False, "error": "busy"})  # the control: our uid is told
            foreign = FakeConn(uid=FOREIGN)
            server._dispatch(foreign)
        self.assertEqual(foreign.sent, [], "a foreign peer is never told busy")
        self.assertEqual(foreign.reads, 0)
        self.assertTrue(foreign.closed)
        self.assertEqual(thread.return_value.start.call_count, 0, "no worker thread starts for either")
        self.assertEqual(server._workers, set())
        self.assertEqual(len(server._connections), 2)

    def test_an_accept_error_that_is_not_a_closed_listener_is_logged_and_the_server_keeps_serving(self):
        server = launcher.Server(self.launcher, self.config.socket_path, os.getuid(), log=self.logs.append)
        served = []
        server._dispatch = served.append
        later = FakeConn()

        class Listener:
            def __init__(self):
                self.results = [OSError(24, "too many open files"), later, OSError(24, "too many open files")]

            def fileno(self):
                return 7  # still open

            def accept(self):
                result = self.results.pop(0)
                if not self.results:
                    server._stop.set()  # end the loop after the last answer
                if isinstance(result, OSError):
                    raise result
                return result, ""

        with mock.patch.object(launcher, "ACCEPT_RETRY_SECONDS", 0):
            server._accept_loop(Listener())
        self.assertEqual(served, [later], "a connection after the error is still served")
        self.assertEqual(self.logs.count("accept failed"), 1)  # the last error came with stop set: silent end

    def test_an_accept_error_on_a_closed_listener_ends_the_loop_without_a_log_line(self):
        server = launcher.Server(self.launcher, self.config.socket_path, os.getuid(), log=self.logs.append)

        class Closed:
            def fileno(self):
                return -1

            def accept(self):
                raise OSError(9, "bad file descriptor")

        server._accept_loop(Closed())
        self.assertEqual(self.logs, [])

    def test_a_peer_that_connects_and_hangs_up_does_not_leave_a_thread(self):
        server = self.start_server()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(self.config.socket_path)
        sock.close()
        server.close()
        self.assertEqual([t for t in threading.enumerate() if t.name.startswith("launcher-")], [])


class StartRefusalTests(ServerHarness):
    """The launcher refuses to start when the socket directory is not what the init container makes it."""

    def refused(self, code, path=None, **kwargs):
        server = launcher.Server(self.launcher, path or self.config.socket_path, os.getuid(), log=noop, **kwargs)
        with self.assertRaises(launcher.SocketRefused) as caught:
            server.start()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual([t for t in threading.enumerate() if t.name.startswith("launcher-")], [])

    def test_a_directory_that_the_group_can_write_is_refused(self):
        os.chmod(self.directory, 0o770)
        self.refused("writable")

    def test_a_directory_that_others_can_write_is_refused(self):
        os.chmod(self.directory, 0o707)
        self.refused("writable")

    def test_a_directory_owned_by_another_uid_is_refused(self):
        self.refused("owner", own_uid=FOREIGN)

    def test_a_symbolic_link_for_the_directory_is_refused(self):
        link = Path(self.directory) / "link"
        link.symlink_to(self.directory)
        self.refused("not_a_directory", path=str(link / "launcher.sock"))

    def test_a_missing_directory_is_refused(self):
        self.refused("unreadable", path=str(Path(self.directory) / "missing" / "launcher.sock"))

    def test_something_that_is_not_our_socket_at_the_path_is_refused_and_left_alone(self):
        Path(self.config.socket_path).write_text("not a socket")
        self.refused("occupied")
        self.assertEqual(Path(self.config.socket_path).read_text(), "not a socket")

    def test_a_symbolic_link_at_the_socket_path_is_refused_and_left_alone(self):
        target = Path(self.directory) / "elsewhere"
        target.write_text("target")
        Path(self.config.socket_path).symlink_to(target)
        self.refused("occupied")
        self.assertEqual(target.read_text(), "target")

    def test_a_stale_socket_of_our_own_is_replaced(self):
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(self.config.socket_path)
        stale.close()
        self.start_server()
        self.assertEqual(json.loads(self.raw(b'{"op":"info"}\n'))["ok"], True)

    def test_the_directory_checks_in_isolation(self):
        ok = SimpleNamespace(st_mode=stat.S_IFDIR | 0o750, st_uid=10003)
        launcher.check_directory(ok, 10003)
        for mode, uid, code in (
            (stat.S_IFDIR | 0o770, 10003, "writable"),
            (stat.S_IFDIR | 0o752, 10003, "writable"),
            (stat.S_IFDIR | 0o750, 10001, "owner"),
            (stat.S_IFLNK | 0o777, 10003, "not_a_directory"),
            (stat.S_IFREG | 0o600, 10003, "not_a_directory"),
        ):
            with self.subTest(mode=oct(mode), uid=uid), self.assertRaises(launcher.SocketRefused) as caught:
                launcher.check_directory(SimpleNamespace(st_mode=mode, st_uid=uid), 10003)
            self.assertEqual(caught.exception.code, code)


# --- main ---------------------------------------------------------------------------------------------------------------------------------


class MainTests(ServerHarness):
    def run_main(self, env, **kwargs):
        """main() in a thread with a bounded join: a launcher that should have refused but serves is stopped and fails."""
        errors = io.StringIO()
        handlers = []
        outcome = []
        with (
            mock.patch.object(launcher.signal, "signal", side_effect=lambda signum, handler: handlers.append((signum, handler))),
            contextlib.redirect_stderr(errors),
        ):
            worker = threading.Thread(target=lambda: outcome.append(launcher.main(env, **kwargs)))
            worker.start()
            worker.join(JOIN)
            if worker.is_alive():
                for signum, handler in handlers:
                    handler(signum, None)
                worker.join(JOIN)
                self.fail("main kept serving where it had to refuse to start")
        return outcome[0], errors.getvalue()

    def test_a_missing_environment_refuses_to_start_and_serves_nothing(self):
        code, errors = self.run_main({})
        self.assertEqual((code, errors), (2, "factory-launcher: refusing to start: anthropic_key\n"))

    def test_the_launcher_refuses_to_run_as_root(self):
        code, errors = self.run_main(full_environment(self.directory), euid=0)
        self.assertEqual((code, errors), (2, "factory-launcher: refusing to start: running_as_root\n"))
        self.assertFalse(os.path.lexists(self.config.socket_path))

    def test_without_the_in_pod_kubernetes_environment_it_refuses_to_start(self):
        code, errors = self.run_main(full_environment(self.directory), euid=1)
        self.assertEqual((code, errors), (2, "factory-launcher: refusing to start: kubernetes configuration\n"))

    def test_a_group_writable_directory_refuses_to_start(self):
        os.chmod(self.directory, 0o770)
        with mock.patch.object(kube.Client, "in_cluster", return_value=self.api):
            code, errors = self.run_main(full_environment(self.directory), euid=1)
        # euid only selects the root refusal; the directory is judged against the real owner (us)
        self.assertEqual((code, errors), (2, "factory-launcher: refusing to start: socket writable\n"))

    def test_main_serves_until_sigterm_and_removes_the_socket(self):
        registered = threading.Event()
        handlers = []

        def capture(signum, handler):
            handlers.append((signum, handler))
            registered.set()

        outcome = []
        errors = io.StringIO()
        with (
            mock.patch.object(kube.Client, "in_cluster", return_value=self.api),
            mock.patch.object(launcher.signal, "signal", side_effect=capture),
            contextlib.redirect_stderr(errors),
        ):
            worker = threading.Thread(target=lambda: outcome.append(launcher.main(full_environment(self.directory), euid=1)))
            worker.start()
            self.assertTrue(registered.wait(JOIN), "main must reach its signal handler")
            reply = json.loads(self.raw(b'{"op":"info"}\n'))
            self.assertEqual(reply["ok"], True)
            signum, handler = handlers[0]
            handler(signum, None)
            worker.join(JOIN)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome, [0])
        self.assertFalse(os.path.lexists(self.config.socket_path))
        self.assertNoSecrets(errors.getvalue())


# --- The client ------------------------------------------------------------------------------------------------------------------------------


class PlantedListener:
    """A raw listener at `path` that records every byte it is sent and optionally answers with a canned reply."""

    def __init__(self, path, reply=b""):
        self.path = path
        self.reply = reply
        self.accepted = 0
        self.received = []
        self.finished = threading.Semaphore(0)  # released when one connection has been read to its end
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(8)
        self.thread = threading.Thread(target=self._run, name="planted-listener", daemon=True)
        self.thread.start()

    def _run(self):
        while True:
            try:
                conn, _address = self.sock.accept()
            except OSError:
                return
            self.accepted += 1
            self._handle(conn)
            self.finished.release()

    def _handle(self, conn):
        with conn, contextlib.suppress(OSError):
            conn.settimeout(2.0)
            if self.reply:
                conn.sendall(self.reply)
            data = conn.recv(4096)
            while data:
                self.received.append(data)
                data = conn.recv(4096)

    def wait_for_connection(self):
        if not self.finished.acquire(timeout=JOIN):
            raise AssertionError("the listener never finished a connection")

    def close(self):
        with contextlib.suppress(OSError):
            self.sock.shutdown(socket.SHUT_RDWR)
        self.sock.close()
        self.thread.join(JOIN)

    def bytes_received(self):
        return b"".join(self.received)


class ClientTests(ServerHarness):
    def test_launch_status_list_info_terminate_over_the_real_socket(self):
        self.start_server()
        client = self.client()
        launched = client.launch(launch_request(1), TOKEN)
        self.assertEqual((launched.name, launched.timeout_seconds, launched.clamped), (pod_name(1), 1800, False))
        self.assertEqual(launched.uid, self.fake.pods()[pod_name(1)]["metadata"]["uid"])
        self.assertEqual(client.pod_state(launched.name, launched.uid)["state"], "unschedulable")
        self.assertEqual([pod["name"] for pod in client.list_pods()], [pod_name(1)])
        self.assertEqual(client.info()["live"], 1)
        self.assertTrue(client.delete_pod(launched.name, launched.uid))
        self.assertFalse(client.delete_pod(launched.name, launched.uid))

    def test_a_clamped_launch_is_reported_to_the_caller(self):
        self.start_server()
        launched = self.client().launch(launch_request(1, timeout_seconds=86400), TOKEN)
        self.assertEqual((launched.timeout_seconds, launched.clamped), (pc.DEADLINE_CLAMP_SECONDS, True))

    def test_capacity_is_its_own_error_that_is_not_a_failure_of_the_launch(self):
        self.start_server(launcher.Launcher(self.api, make_config(self.directory, max_pods=1), clock=self.clock, log=noop))
        client = self.client()
        client.launch(launch_request(1), TOKEN)
        with self.assertRaises(launcher_client.Capacity) as caught:
            client.launch(launch_request(2, repository="Org/two"), TOKEN)
        self.assertEqual(str(caught.exception), "launcher at capacity")

    def test_a_denied_launcher_is_a_rejected_launch(self):
        self.start_server()
        client = self.client()
        client.deny("isolation_failed")
        with self.assertRaises(launcher_client.Rejected) as caught:
            client.launch(launch_request(1), TOKEN)
        self.assertEqual(caught.exception.code, "denied")
        self.assertEqual(self.fake.calls("create_pod"), [])
        self.assertEqual(client.info()["denied"], True)

    def test_an_admission_denial_comes_through_with_its_rule_ids(self):
        self.start_server()
        self.fake.admit_image = f"registry.example.invalid/factory@sha256:{'f' * 64}"
        with self.assertRaises(launcher_client.Rejected) as caught:
            self.client().launch(launch_request(1), TOKEN)
        self.assertEqual((caught.exception.code, caught.exception.rules), ("rejected", ("image",)))

    def test_the_launch_request_is_closed_on_the_client_side_too(self):
        client = self.client()
        for bad in ({**launch_request(), "image": "x"}, {key: value for key, value in launch_request().items() if key != "phase"}, [], None):
            with self.assertRaises(ValueError):
                client.launch(bad, TOKEN)
        with self.assertRaises(ValueError):
            client.launch(launch_request(), 5)

    # -- the seam of slice 5 -------------------------------------------------------------------------------------------

    def handle_of(self, launched):
        return SimpleNamespace(name=launched.name, uid=launched.uid)

    def test_reader_maps_the_launcher_answer_to_alive_dead_or_unknown(self):
        self.start_server()
        client = self.client()
        expectations = (
            (1, "unschedulable", "alive"),
            (2, "succeeds", "dead"),
            (3, "exit_nonzero", "dead"),
            (4, "oom", "dead"),
            (5, "evicted", "dead"),
            (6, "node_deleted", "dead"),
            (7, "image_pull_backoff", "alive"),
        )
        launched = {}
        for n, scenario, _expected in expectations:
            self.fake.script(pod_name(n), scenario)
            launched[n] = client.launch(launch_request(n, repository=f"Org/r{n}"), TOKEN)
        self.clock.advance(250)
        for n, scenario, expected in expectations:
            with self.subTest(scenario=scenario):
                self.assertEqual(client.reader(self.handle_of(launched[n])), expected)

    def test_reader_is_dead_for_another_uid_and_unknown_when_it_cannot_tell(self):
        self.start_server()
        client = self.client()
        launched = client.launch(launch_request(1), TOKEN)
        other = SimpleNamespace(name=launched.name, uid="11111111-1111-4111-8111-111111111111")
        self.assertEqual(client.reader(other), "dead")
        for bad in (None, SimpleNamespace(name="x", uid="y"), {"name": launched.name}, SimpleNamespace(name=launched.name, uid=5)):
            self.assertEqual(client.reader(bad), "unknown")
        self.fake.inject("get_pod", times=10, status=500)
        self.assertEqual(client.reader(self.handle_of(launched)), "unknown")

    def test_reader_is_unknown_when_the_launcher_is_not_there_and_never_raises(self):
        client = launcher_client.LauncherClient(
            str(Path(self.directory) / "absent.sock"), launcher_uid=os.getuid(), sleep=noop, retries=0
        )
        handle_ = SimpleNamespace(name=pod_name(1), uid="11111111-1111-4111-8111-111111111111")
        self.assertEqual(client.reader(handle_), "unknown")

    def test_a_pod_that_is_alive_is_not_dead_even_when_pending(self):
        self.start_server()
        client = self.client()
        self.fake.script(pod_name(1), "unschedulable")
        launched = client.launch(launch_request(1), TOKEN)
        self.assertEqual(client.reader(self.handle_of(launched)), "alive")

    def test_terminate_deletes_and_a_missing_pod_is_success(self):
        self.start_server()
        client = self.client()
        launched = client.launch(launch_request(1), TOKEN)
        client.terminate(self.handle_of(launched))
        self.assertEqual(self.fake.pods(), {})
        client.terminate(self.handle_of(launched))

    def test_terminate_refuses_a_handle_that_does_not_name_a_worker_pod_and_raises_when_the_launcher_is_down(self):
        client = launcher_client.LauncherClient(
            str(Path(self.directory) / "absent.sock"), launcher_uid=os.getuid(), sleep=noop, retries=0
        )
        with self.assertRaises(ValueError):
            client.terminate(SimpleNamespace(name="kube-system-pod", uid="x"))
        with self.assertRaises(launcher_client.Unavailable):
            client.terminate(SimpleNamespace(name=pod_name(1), uid="11111111-1111-4111-8111-111111111111"))


class PodHandleSeamTests(ServerHarness):
    """The client's reader and terminator against the real pod handle of slice 5 (src/factory/podhandle.py)."""

    def record_of(self, n, launched):
        return podhandle.PodHandle(
            name=launched.name,
            uid=launched.uid,
            session=fake_kube.session_id(n),
            launcher="launcher",
            created_at="2026-01-01T00:00:00Z",
        )

    def absent_client(self):
        return launcher_client.LauncherClient(
            str(Path(self.directory) / "absent.sock"), launcher_uid=os.getuid(), sleep=noop, retries=0
        )

    def ticking_client(self, **options):
        """A client whose sleep moves the clock of the fake API: waiting for a terminating pod takes no real time."""
        return launcher_client.LauncherClient(
            self.config.socket_path, launcher_uid=os.getuid(), sleep=self.clock.advance, clock=self.clock, **options
        )

    def running(self, client, n=1, **overrides):
        """A launched pod that stays Running: deleting it is a grace period, not an instant removal."""
        self.fake.script(pod_name(n), (Step(0, fake_kube.running()),))
        return self.record_of(n, client.launch(launch_request(n, **overrides), TOKEN))

    def test_pod_status_reads_a_recorded_pod_through_the_client_reader(self):
        self.start_server()
        client = self.client()
        record = self.record_of(1, client.launch(launch_request(1), TOKEN))
        self.assertEqual(podhandle.pod_status(record, client.reader), "alive")
        self.assertEqual(podhandle.pod_status(record.record(), client.reader), "alive")
        client.terminate(record)
        self.assertEqual(podhandle.pod_status(record, client.reader), "dead")

    def test_confirm_gone_terminates_a_live_pod_through_the_client_and_forgets_a_dead_one(self):
        self.start_server()
        client = self.client()
        record = self.record_of(1, client.launch(launch_request(1), TOKEN))
        self.assertTrue(podhandle.confirm_gone(record, client.reader, client.terminate))
        self.assertEqual(self.fake.pods(), {})
        self.assertTrue(podhandle.confirm_gone(record, client.reader, client.terminate))

    def test_the_installed_callables_are_the_clients(self):
        self.start_server()
        client = self.client()
        record = self.record_of(1, client.launch(launch_request(1), TOKEN))
        podhandle.install(reader=client.reader, terminate=client.terminate)
        self.addCleanup(podhandle.reset)
        self.assertEqual(podhandle.pod_status(record), "alive")
        self.assertTrue(podhandle.terminate_pod(record))
        self.assertEqual(self.fake.pods(), {})

    def test_terminate_returns_only_once_a_running_pod_is_gone(self):
        self.start_server()
        client = self.ticking_client()
        record = self.running(client)
        started = self.clock()
        client.terminate(record)
        self.assertEqual(self.fake.pods(), {}, "the pod is gone when terminate returns, not merely asked to go")
        self.assertGreaterEqual(self.clock() - started, pc.TERMINATION_GRACE_SECONDS, "it waited for the grace period")
        self.assertEqual(podhandle.pod_status(record, client.reader), "dead")
        client.terminate(record)  # a pod that is already gone is success

    def test_the_reader_does_not_call_a_terminating_pod_dead(self):
        self.start_server()
        client = self.ticking_client()
        record = self.running(client)
        self.assertEqual(podhandle.pod_status(record, client.reader), "alive")
        self.assertTrue(client.delete_pod(record.name, record.uid), "the delete is accepted")
        self.assertIn(record.name, self.fake.pods(), "and the pod is still in the API, running for its grace period")
        state = client.pod_state(record.name, record.uid)
        self.assertEqual((state["state"], state["final"], state["present"], state["terminating"]), ("evicted", True, True, True))
        self.assertEqual(podhandle.pod_status(record, client.reader), "alive")
        self.clock.advance(pc.TERMINATION_GRACE_SECONDS)
        self.assertEqual(podhandle.pod_status(record, client.reader), "dead")

    def test_confirm_gone_does_not_forget_a_running_pod_until_it_is_gone(self):
        self.start_server()
        client = self.ticking_client(terminate_wait=5.0)
        record = self.running(client)
        with self.assertRaises(launcher_client.Unavailable):
            client.terminate(record)
        self.assertIn(record.name, self.fake.pods(), "the wait was bounded and the pod is still there")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(podhandle.confirm_gone(record, client.reader, client.terminate), "so the record stays")
        self.clock.advance(pc.TERMINATION_GRACE_SECONDS)

        def refuse(_record):
            self.fail("the pod is gone: nothing is asked of the launcher")

        self.assertTrue(podhandle.confirm_gone(record, client.reader, refuse))

    def test_confirm_gone_waits_for_a_running_pod_to_go_and_then_forgets_it(self):
        self.start_server()
        client = self.ticking_client()
        record = self.running(client)
        self.assertTrue(podhandle.confirm_gone(record, client.reader, client.terminate))
        self.assertEqual(self.fake.pods(), {})

    def test_the_deadline_backstop_reap_of_a_running_pod_reads_alive_until_it_is_gone(self):
        self.start_server()
        client = self.ticking_client()
        record = self.running(client, timeout_seconds=60)
        self.clock.advance(60 + pc.DEADLINE_MARGIN_SECONDS + pc.TERMINATION_GRACE_SECONDS)
        self.assertEqual(self.launcher.sweep(), 1)
        self.assertIn(record.name, self.fake.pods())
        self.assertEqual(podhandle.pod_status(record, client.reader), "alive", "the launcher deleted it; it still runs")
        client.terminate(record)
        self.assertEqual(self.fake.pods(), {})
        self.assertEqual(podhandle.pod_status(record, client.reader), "dead")

    def test_a_launcher_that_is_not_there_is_unknown_and_confirms_nothing(self):
        client = self.absent_client()
        record = podhandle.PodHandle(
            name=pod_name(1),
            uid="11111111-1111-4111-8111-111111111111",
            session=fake_kube.session_id(1),
            launcher="launcher",
            created_at="2026-01-01T00:00:00Z",
        )
        self.assertEqual(podhandle.pod_status(record, client.reader), "unknown")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(podhandle.confirm_gone(record, client.reader, client.terminate))


class ServerAuthenticationTests(unittest.TestCase):
    """The client authenticates the server before it sends a byte: a planted listener receives NOTHING."""

    def setUp(self):
        self.directory = short_dir(self)
        self.path = str(Path(self.directory) / "launcher.sock")
        self.listeners = []

    def listen(self, path=None, reply=b""):
        listener = PlantedListener(path or self.path, reply)
        self.addCleanup(listener.close)
        self.listeners.append(listener)
        return listener

    def client(self, path=None, **options):
        options.setdefault("launcher_uid", os.getuid())
        return launcher_client.LauncherClient(path or self.path, sleep=noop, retries=0, **options)

    def assertRefusedWithNothingSent(self, listener, client, accepted=0):
        with self.assertRaises(launcher_client.ServerRefused) as caught:
            client.launch(launch_request(1), TOKEN)
        self.assertEqual(str(caught.exception), "launcher identity not trusted")
        if accepted:
            listener.wait_for_connection()
        listener.close()
        self.assertEqual(listener.bytes_received(), b"", "the listener must have read zero bytes")
        self.assertEqual(listener.accepted, accepted)
        self.assertNotIn(TOKEN, "".join(traceback.format_exception(caught.exception)))

    def test_a_listener_run_by_another_uid_receives_nothing(self):
        listener = self.listen()
        # The files look right (we own them); only SO_PEERCRED of the connected socket can tell the impostor.
        with mock.patch.object(launcher_client, "check_paths"):
            self.assertRefusedWithNothingSent(listener, self.client(launcher_uid=FOREIGN), accepted=1)

    def test_the_peer_check_is_what_refuses_and_the_same_listener_is_trusted_by_its_own_uid(self):
        listener = self.listen(reply=b'{"ok": true, "max_pods": 3}\n')
        with mock.patch.object(launcher_client, "check_paths"):
            self.assertEqual(self.client().info()["max_pods"], 3)
        listener.wait_for_connection()
        listener.close()
        self.assertEqual(listener.bytes_received(), b'{"op":"info"}\n')

    def test_a_socket_directory_the_group_can_write_is_refused_before_connecting(self):
        listener = self.listen()
        os.chmod(self.directory, 0o770)
        self.assertRefusedWithNothingSent(listener, self.client())

    def test_a_socket_directory_others_can_write_is_refused_before_connecting(self):
        listener = self.listen()
        os.chmod(self.directory, 0o707)
        self.assertRefusedWithNothingSent(listener, self.client())

    def test_a_socket_directory_owned_by_another_uid_is_refused_before_connecting(self):
        listener = self.listen()
        self.assertRefusedWithNothingSent(listener, self.client(launcher_uid=FOREIGN))

    def test_a_symlinked_socket_path_is_refused_before_connecting(self):
        real = str(Path(self.directory) / "real.sock")
        listener = self.listen(real)
        os.symlink(real, self.path)
        self.assertRefusedWithNothingSent(listener, self.client())

    def test_a_symlinked_socket_directory_is_refused_before_connecting(self):
        real_dir = Path(self.directory) / "real"
        real_dir.mkdir()
        listener = self.listen(str(real_dir / "launcher.sock"))
        link = Path(self.directory) / "link"
        link.symlink_to(real_dir)
        self.assertRefusedWithNothingSent(listener, self.client(str(link / "launcher.sock")))

    def test_a_regular_file_at_the_socket_path_is_refused(self):
        listener = self.listen(str(Path(self.directory) / "other.sock"))
        Path(self.path).write_text("not a socket")
        self.assertRefusedWithNothingSent(listener, self.client())

    def test_the_file_checks_in_isolation(self):
        launcher_client.check_directory(SimpleNamespace(st_mode=stat.S_IFDIR | 0o750, st_uid=10003), 10003)
        launcher_client.check_socket(SimpleNamespace(st_mode=stat.S_IFSOCK | 0o660, st_uid=10003), 10003)
        for info in (
            SimpleNamespace(st_mode=stat.S_IFDIR | 0o770, st_uid=10003),
            SimpleNamespace(st_mode=stat.S_IFDIR | 0o702, st_uid=10003),
            SimpleNamespace(st_mode=stat.S_IFDIR | 0o750, st_uid=10001),
            SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_uid=10003),
        ):
            with self.subTest(mode=oct(info.st_mode)), self.assertRaises(launcher_client.ServerRefused):
                launcher_client.check_directory(info, 10003)
        for info in (SimpleNamespace(st_mode=stat.S_IFSOCK | 0o660, st_uid=10001), SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_uid=10003), SimpleNamespace(st_mode=stat.S_IFREG | 0o660, st_uid=10003)):
            with self.subTest(mode=oct(info.st_mode)), self.assertRaises(launcher_client.ServerRefused):
                launcher_client.check_socket(info, 10003)


class ClientBoundsTests(unittest.TestCase):
    def setUp(self):
        self.directory = short_dir(self)
        self.path = str(Path(self.directory) / "launcher.sock")

    def listen(self, reply=b""):
        listener = PlantedListener(self.path, reply)
        self.addCleanup(listener.close)
        return listener

    def client(self, **options):
        options.setdefault("retries", 0)
        return launcher_client.LauncherClient(self.path, launcher_uid=os.getuid(), sleep=noop, **options)

    def test_a_launcher_that_is_starting_is_retried_with_the_contract_backoff_then_unavailable(self):
        sleeps = []
        client = launcher_client.LauncherClient(self.path, launcher_uid=os.getuid(), sleep=sleeps.append)
        with self.assertRaises(launcher_client.Unavailable) as caught:
            client.info()
        self.assertEqual(str(caught.exception), "launcher unavailable")
        self.assertEqual(sleeps, [1, 2, 4])

    def test_a_launcher_that_appears_during_the_retry_is_used(self):
        started = []

        def start_then_sleep(seconds):
            started.append(seconds)
            if len(started) == 1:
                self.listen(b'{"ok": true, "max_pods": 3}\n')

        client = launcher_client.LauncherClient(self.path, launcher_uid=os.getuid(), sleep=start_then_sleep)
        self.assertEqual(client.info()["max_pods"], 3)
        self.assertEqual(started, [1])

    def test_an_impostor_is_never_retried(self):
        sleeps = []
        Path(self.path).write_text("not a socket")
        client = launcher_client.LauncherClient(self.path, launcher_uid=os.getuid(), sleep=sleeps.append)
        with self.assertRaises(launcher_client.ServerRefused):
            client.info()
        self.assertEqual(sleeps, [])

    def test_a_launcher_that_never_answers_is_unavailable_after_the_read_timeout(self):
        self.listen()
        with self.assertRaises(launcher_client.Unavailable):
            self.client(read_timeout=0.3).info()

    def test_a_request_is_never_repeated_after_it_was_sent(self):
        listener = self.listen()
        with self.assertRaises(launcher_client.Unavailable):
            self.client(read_timeout=0.3, retries=3).info()
        listener.wait_for_connection()
        listener.close()
        self.assertEqual((listener.accepted, listener.bytes_received()), (1, b'{"op":"info"}\n'))

    def test_a_reply_over_the_bound_is_cut_off(self):
        self.listen(b"a" * (launcher_client.MAX_REPLY_BYTES + 5000))
        with self.assertRaises(launcher_client.BadReply):
            self.client().info()

    def test_replies_that_are_not_the_closed_shape_are_a_bad_reply(self):
        replies = (
            b"not json\n",
            b"[1]\n",
            b"null\n",
            b'{"ok": "yes"}\n',
            b'{"ok": false}\n',
            b'{"ok": false, "error": "weird"}\n',
            b'{"ok": false, "error": 5}\n',
            b"\xff\xfe\n",
        )
        for reply in replies:
            with self.subTest(reply=reply[:20]):
                listener = PlantedListener(self.path, reply)
                try:
                    with self.assertRaises(launcher_client.BadReply):
                        self.client().info()
                finally:
                    listener.close()
                    os.unlink(self.path)

    def test_a_launch_or_status_reply_with_the_wrong_fields_is_a_bad_reply(self):
        cases = (
            (b'{"ok": true}\n', lambda c: c.launch(launch_request(1), TOKEN)),
            (b'{"ok": true, "name": "x", "uid": "y", "timeout_seconds": 1, "clamped": false}\n', lambda c: c.launch(launch_request(1), TOKEN)),
            (b'{"ok": true, "state": "bogus", "detail": "x", "final": true, "exit_code": null, "present": true, "terminating": false}\n', lambda c: c.pod_state(pod_name(1))),
            (b'{"ok": true, "state": "running", "detail": "x y", "final": false, "exit_code": null, "present": true, "terminating": false}\n', lambda c: c.pod_state(pod_name(1))),
            (b'{"ok": true, "state": "running", "detail": "x", "final": "no", "exit_code": null, "present": true, "terminating": false}\n', lambda c: c.pod_state(pod_name(1))),
            (b'{"ok": true, "state": "running", "detail": "x", "final": false, "exit_code": true, "present": true, "terminating": false}\n', lambda c: c.pod_state(pod_name(1))),
            # whether the pod is still there is part of the closed shape: without it the reader and terminate cannot decide
            (b'{"ok": true, "state": "running", "detail": "x", "final": false, "exit_code": null}\n', lambda c: c.pod_state(pod_name(1))),
            (b'{"ok": true, "state": "gone", "detail": "x", "final": true, "exit_code": null, "present": false}\n', lambda c: c.pod_state(pod_name(1))),
            (b'{"ok": true, "state": "gone", "detail": "x", "final": true, "exit_code": null, "present": "no", "terminating": false}\n', lambda c: c.pod_state(pod_name(1))),
            (b'{"ok": true, "state": "gone", "detail": "x", "final": true, "exit_code": null, "present": false, "terminating": true}\n', lambda c: c.pod_state(pod_name(1))),
            (b'{"ok": true, "pods": "x"}\n', lambda c: c.list_pods()),
        )
        for reply, call in cases:
            with self.subTest(reply=reply[:40]):
                listener = PlantedListener(self.path, reply)
                try:
                    with self.assertRaises(launcher_client.BadReply):
                        call(self.client())
                finally:
                    listener.close()
                    os.unlink(self.path)

    def test_a_status_reply_of_the_closed_shape_is_read(self):
        reply = b'{"ok": true, "state": "evicted", "detail": "terminating", "final": true, "exit_code": null, "present": true, "terminating": true}\n'
        self.listen(reply)
        state = self.client().pod_state(pod_name(1))
        self.assertEqual(state, {"state": "evicted", "detail": "terminating", "final": True, "exit_code": None, "present": True, "terminating": True})

    def test_the_fixed_codes_map_to_fixed_errors(self):
        for code, error, text in (
            ("capacity", launcher_client.Capacity, "launcher at capacity"),
            ("unavailable", launcher_client.Unavailable, "launcher unavailable"),
            ("busy", launcher_client.Unavailable, "launcher unavailable"),
            ("timeout", launcher_client.Unavailable, "launcher unavailable"),
            ("invalid", launcher_client.Rejected, "launcher refused the request (invalid)"),
            ("failed", launcher_client.Rejected, "launcher refused the request (failed)"),
        ):
            with self.subTest(code=code):
                listener = PlantedListener(self.path, f'{{"ok": false, "error": "{code}"}}\n'.encode())
                try:
                    with self.assertRaises(error) as caught:
                        self.client().info()
                finally:
                    listener.close()
                    os.unlink(self.path)
                self.assertEqual(str(caught.exception), text)

    def test_rule_ids_in_a_rejection_are_filtered_to_words(self):
        listener = PlantedListener(self.path, b'{"ok": false, "error": "rejected", "rules": ["image", "BAD RULE", 5, "host-users"]}\n')
        self.addCleanup(listener.close)
        with self.assertRaises(launcher_client.Rejected) as caught:
            self.client().info()
        self.assertEqual(caught.exception.rules, ("image", "host-users"))

    def test_from_environment_uses_the_contract_names(self):
        client = launcher_client.LauncherClient.from_environment({pc.ENV_MANAGER["launcher_socket"]: self.path}, launcher_uid=os.getuid())
        self.assertEqual(client._path, self.path)
        self.assertEqual(launcher_client.LauncherClient.from_environment({})._path, pc.LAUNCHER_SOCKET)

    def test_a_bad_constructor_argument_is_refused(self):
        for path, uid in (("relative", 1), ("/a\0b", 1), ("/a", -1), ("/a", True), (5, 1)):
            with self.assertRaises(ValueError):
                launcher_client.LauncherClient(path, launcher_uid=uid)


# --- No secret anywhere ---------------------------------------------------------------------------------------------------------------------


class Captured(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.text = []

    def emit(self, record):
        self.text.append(record.getMessage())
        if record.exc_info:
            self.text.append("".join(traceback.format_exception(*record.exc_info)))


class PoisonedEnvironmentTests(ServerHarness):
    """The key, the relay token and the Kubernetes bearer are written to the API and nowhere else."""

    def test_no_canary_reaches_a_reply_a_log_line_stderr_or_an_error(self):
        captured = Captured()
        logging.getLogger().addHandler(captured)
        self.addCleanup(logging.getLogger().removeHandler, captured)
        errors = io.StringIO()
        replies, failures = [], []
        session = launcher.Launcher(self.api, self.config, clock=self.clock)  # the default log goes to stderr
        with contextlib.redirect_stderr(errors):
            self.start_server(session)
            client = self.client()
            self.fake.inject("create_secret", times=1, status=403, body={"message": f"leak {KEY} {TOKEN}"})
            self.fake.inject("create_pod", times=1, status=422, body={"message": f"denied [image] leak {KEY} {TOKEN}"})
            for n in (1, 2, 3):
                try:
                    launched = client.launch(launch_request(n, repository=f"Org/r{n}"), TOKEN)
                    replies.append(launched)
                except launcher_client.LauncherError as failure:
                    failures.append(failure)
            replies.extend([client.info(), client.list_pods(), client.pod_state(pod_name(3))])
            client.deny("isolation_failed")
            self.clock.advance(100000)
            session.sweep()
        self.assertEqual(len(failures), 2)
        self.assertNoSecrets(errors.getvalue(), repr(replies), [str(f) for f in failures], captured.text, repr(self.config))
        for failure in failures:
            self.assertNoSecrets("".join(traceback.format_exception(failure)))
        self.assertIn("reaped reason=deadline", errors.getvalue())

    def test_the_log_lines_are_a_closed_set_of_fixed_words(self):
        allowed = (
            "launched",
            "launch refused: capacity",
            "launch refused: listing failed",
            "launch failed",
            "compensation failed to delete",
            "launches denied reason=",
            "reaped reason=",
            "reaping failed reason=",
            "terminate failed",
            "internal error",
            "sweep",
        )
        self.fake.inject("create_secret", times=1, status=403)
        self.launch(1)
        self.launch(2)
        handle(self.launcher, {"op": "deny", "reason": "isolation_failed"})
        for entry in self.logs:
            self.assertTrue(entry.startswith(allowed), entry)
            self.assertRegex(entry, r"^[a-z0-9 :=_]+$")


# --- The program does only what it says -----------------------------------------------------------------------------------------------------------


def parse(name):
    return ast.parse((ROOT / "src" / "factory" / name).read_text(encoding="utf-8"))


class ProgramShapeTests(unittest.TestCase):
    modules = ("launcher.py", "launcher_client.py")

    def imports(self, tree):
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                found.add(node.module.split(".")[0] if node.module else "." * node.level)
        return found

    def test_the_launcher_runs_no_child_process_and_makes_no_outbound_call_of_its_own(self):
        forbidden = {"subprocess", "multiprocessing", "ctypes", "urllib", "http", "requests", "shlex", "pty", "asyncio", "concurrent"}
        for name in self.modules:
            self.assertEqual(self.imports(parse(name)) & forbidden, set(), name)
        banned = {"system", "popen", "fork", "forkpty", "execv", "execve", "execvp", "execl", "execlp", "spawnl", "spawnv", "posix_spawn"}
        for node in ast.walk(parse("launcher.py")):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os":
                self.assertNotIn(node.attr, banned)

    def test_the_launcher_only_listens_on_and_connects_to_a_unix_socket(self):
        for name in self.modules:
            for node in ast.walk(parse(name)):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "socket":
                    self.assertNotIn(node.attr, {"AF_INET", "AF_INET6", "create_connection", "gethostbyname", "getaddrinfo", "create_server"}, name)

    def test_standard_library_and_the_package_only(self):
        for name in self.modules:
            for module in self.imports(parse(name)):
                self.assertTrue(module in sys.stdlib_module_names or module == ".", f"{name}: {module}")

    def test_no_string_or_bytes_is_built_with_plus(self):
        def is_text(node):
            return isinstance(node, ast.JoinedStr) or (isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)))

        trees = {name: parse(name) for name in self.modules}
        trees["tests/test_launcher.py"] = ast.parse(Path(__file__).read_text(encoding="utf-8"))  # this file too
        for name, tree in trees.items():
            for node in ast.walk(tree):
                if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                    self.assertFalse(is_text(node.left) or is_text(node.right), f"{name}:{node.lineno}")
                if isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Add):
                    self.assertFalse(is_text(node.value), f"{name}:{node.lineno}")

    def test_pods_are_built_only_through_podspec(self):
        tree = parse("launcher.py")
        calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "podspec"
        }
        self.assertLessEqual({"build_pod", "build_secret"}, calls)
        source = (ROOT / "src" / "factory" / "launcher.py").read_text(encoding="utf-8")
        self.assertNotIn('"kind": "Pod"', source)
        self.assertNotIn('"kind": "Secret"', source)

    def test_the_launcher_reads_the_key_only_from_its_own_environment_name(self):
        self.assertEqual(launcher.KEY_ENV, "ANTHROPIC_API_KEY")


if __name__ == "__main__":
    unittest.main()

"""In-memory fake of the slice of the Kubernetes API that factory.kube uses (slice 7 of the pod backend).

Like tests/fake_blob.py it binds 127.0.0.1 only and keeps time on an injectable fake clock, so a
test moves a pod through its lifecycle by advancing time instead of sleeping.

EVERY SHAPE HERE IS ASSUMED. No cluster was consulted: the paths, the Status bodies, the pod
statuses of each lifecycle and the way a denial reaches the client are the documented Kubernetes API
as the author knows it. A recording from a real cluster replaces them (slice 22). Passing against
this fake proves that the client and the classifier agree with these assumptions, nothing more.

Supported (exactly the launcher's six operations): create, get, list (by label selector, with
pages) and delete of pods in a namespace; create and delete of Secrets. Anything else, a `watch`,
`pods/log` and `exec` included, is answered 403 as an RBAC Role without that verb would, and a test
asserts the client never asks. Also supported:

* owner reference garbage collection: removing a pod removes the Secrets it owns, and a Secret whose
  owners are all absent (no pod of that name and uid) is collected at the next request;
* a scriptable pod lifecycle (`Step`, `SCENARIOS`) driven by the clock: Pending with an
  Unschedulable condition, a container waiting (ContainerCreating, ErrImagePull, ImagePullBackOff,
  CreateContainerConfigError), Running, Succeeded, Failed with a terminated container (OOMKilled,
  an exit code), Evicted, DeadlineExceeded, a kubelet admission failure, a DisruptionTarget
  condition, a deletionTimestamp, and a pod that vanishes when its node is deleted;
* an optional admission hook (`admit_image`) that runs `podspec.check_policy` for pods and
  `podspec.check_secret_policy` for Secrets and answers 403 (a ValidatingAdmissionPolicy's `Forbidden`) with the broken rule ids as `[rule:<id>]`;
* fault injection (`inject`): a status with headers and a body, a dropped connection, a hang.
"""

import contextlib
import copy
import http.server
import json
import re
import socket
import threading
import urllib.parse
from dataclasses import dataclass

from factory import podspec
from tests.fake_blob import FakeClock

__all__ = ["FakeClock", "FakeKube", "SCENARIOS", "Step", "match_selector"]

HANG = "hang"
FIXED_TIME = "2026-01-01T00:00:00Z"
OPS = ("create_pod", "get_pod", "list_pods", "delete_pod", "create_secret", "delete_secret")
_STATUS_REASONS = {
    400: "BadRequest",
    401: "Unauthorized",
    403: "Forbidden",
    404: "NotFound",
    409: "AlreadyExists",
    415: "UnsupportedMediaType",
    422: "Invalid",
    429: "TooManyRequests",
    500: "InternalError",
    503: "ServiceUnavailable",
}

# --- Lifecycle statuses (ASSUMED shapes of `pod.status`) ------------------------------------------


def _condition(kind, status, reason=None):
    found = {"type": kind, "status": status}
    if reason:
        found["reason"] = reason
    return found


def _container(state):
    return {"name": "worker", "ready": False, "restartCount": 0, "state": state}


def pending_unschedulable():
    return {"phase": "Pending", "conditions": [_condition("PodScheduled", "False", "Unschedulable")]}


def pending_waiting(reason):
    return {
        "phase": "Pending",
        "conditions": [_condition("PodScheduled", "True")],
        "containerStatuses": [_container({"waiting": {"reason": reason}})],
    }


def running():
    return {
        "phase": "Running",
        "conditions": [_condition("PodScheduled", "True"), _condition("Ready", "True")],
        "containerStatuses": [_container({"running": {"startedAt": FIXED_TIME}})],
    }


def terminated(phase, reason, exit_code, pod_reason=None, pod_conditions=()):
    status = {
        "phase": phase,
        "conditions": [_condition("PodScheduled", "True"), *pod_conditions],
        "containerStatuses": [
            _container({"terminated": {"reason": reason, "exitCode": exit_code, "finishedAt": FIXED_TIME}})
        ],
    }
    if pod_reason:
        status["reason"] = pod_reason
    return status


def succeeded():
    return terminated("Succeeded", "Completed", 0)


def failed_exit(code=1):
    return terminated("Failed", "Error", code)


def oom_killed():
    return terminated("Failed", "OOMKilled", 137)


def evicted():
    return terminated("Failed", "Error", 137, pod_reason="Evicted")


def deadline_exceeded():
    return terminated("Failed", "Error", 137, pod_reason="DeadlineExceeded")


def kubelet_rejected():
    return {"phase": "Failed", "reason": "OutOfcpu", "conditions": []}


def running_with_disruption():
    status = running()
    status["conditions"].append(_condition("DisruptionTarget", "True", "TerminationByKubelet"))
    return status


def running_but_oom_killed():
    """The window in which the container is already dead and the pod phase has not followed yet."""
    status = running()
    status["containerStatuses"] = [_container({"terminated": {"reason": "OOMKilled", "exitCode": 137}})]
    return status


@dataclass(frozen=True)
class Step:
    """One change of a pod `at` seconds after it was created: a new status, a deletionTimestamp, or removal."""

    at: float
    status: dict | None = None
    deletion: bool = False
    vanish: bool = False


SCENARIOS = {
    "succeeds": (
        Step(0, pending_unschedulable()),
        Step(100, pending_waiting("ContainerCreating")),
        Step(105, running()),
        Step(200, succeeded()),
    ),
    "unschedulable": (Step(0, pending_unschedulable()),),
    "image_pull_backoff": (
        Step(0, pending_waiting("ContainerCreating")),
        Step(5, pending_waiting("ErrImagePull")),
        Step(10, pending_waiting("ImagePullBackOff")),
    ),
    "config_error": (Step(0, pending_waiting("ContainerCreating")), Step(3, pending_waiting("CreateContainerConfigError"))),
    "exit_nonzero": (Step(0, running()), Step(60, failed_exit(2))),
    "oom": (Step(0, running()), Step(60, oom_killed())),
    "oom_while_running": (Step(0, running()), Step(60, running_but_oom_killed())),
    "evicted": (Step(0, running()), Step(30, evicted())),
    "deadline": (Step(0, running()), Step(30, deadline_exceeded())),
    "kubelet_rejected": (Step(0, kubelet_rejected()),),
    "disruption_target": (Step(0, running()), Step(30, running_with_disruption())),
    "node_deleted": (Step(0, running()), Step(30, vanish=True)),
    "karpenter_eviction": (Step(0, running()), Step(30, deletion=True), Step(60, vanish=True)),
}

# --- Label selectors ------------------------------------------------------------------------------

_REQUIREMENT = re.compile(
    r"\s*(!?)([A-Za-z0-9._/-]+)\s*(?:(==|=|!=)\s*([A-Za-z0-9._-]*)|\s+(in|notin)\s*\(([^)]*)\))?\s*"
)


def _split_requirements(text):
    parts, depth, current = [], 0, []
    for char in text:
        depth += (char == "(") - (char == ")")
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return [part for part in parts if part.strip()]


def _holds(found, labels):
    negated, key, operator, value, set_operator, members = found.groups()
    if set_operator:
        wanted = {member.strip() for member in members.split(",")}
        present = key in labels and labels[key] in wanted
        return present if set_operator == "in" else not present
    if operator in ("=", "=="):
        return key in labels and labels[key] == value
    if operator == "!=":
        return labels.get(key) != value
    return (key not in labels) if negated else (key in labels)


def parse_selector(selector):
    """The requirements of a label selector (`k=v`, `k==v`, `k!=v`, `k in (a,b)`, `k notin (a)`, `k`, `!k`)."""
    found = [_REQUIREMENT.fullmatch(part) for part in _split_requirements(selector)]
    if any(requirement is None for requirement in found):
        raise ValueError("bad selector")
    return found


def match_selector(selector, labels):
    """Whether `labels` satisfy a label selector; ValueError for one that does not parse."""
    return all(_holds(requirement, labels) for requirement in parse_selector(selector))


# --- The server ---------------------------------------------------------------------------------------


@dataclass
class Record:
    """One request as the server saw it (headers lower-cased; `body` is the parsed JSON or None)."""

    method: str
    path: str
    query: dict
    op: str
    headers: dict
    body: object = None
    status: int | None = None  # None: the connection was dropped without a reply
    injected: bool = False


@dataclass
class _Pod:
    manifest: dict
    created: float
    steps: tuple
    applied: int = 0
    remove_at: float | None = None


class FakeKube:
    """The API server. `admit_image` turns the admission hook on (the pinned image of the policy parameter)."""

    def __init__(self, clock=None, tokens=None, admit_image=None, page_size=None, scenario="succeeds"):
        self.clock = clock or FakeClock()
        self.tokens = set(tokens) if tokens else None  # None: any non-empty bearer token
        self.admit_image = admit_image
        self.page_size = page_size
        self.scenario = scenario
        self.requests = []
        self._pods = {}
        self._secrets = {}
        self._scripts = {}
        self._rules = []
        self._uids = 0
        self._mutex = threading.RLock()
        self._release = threading.Event()
        self._httpd = None
        self._thread = None
        self._port = None

    # -- lifecycle of the server -----------------------------------------------------------------

    def start(self):
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence the default access log
                pass

            def do_GET(self):
                server._handle(self)

            do_POST = do_DELETE = do_PUT = do_PATCH = do_GET

        class Httpd(http.server.ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        self._httpd = Httpd(("127.0.0.1", 0), Handler)
        self._port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._release.set()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._thread.join(timeout=5)
            self._httpd = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc_info):
        self.stop()

    @property
    def url(self):
        return f"http://127.0.0.1:{self._port}"

    # -- test controls ---------------------------------------------------------------------------------

    def script(self, pod_name, scenario):
        """Give the pod that will be created under `pod_name` a lifecycle: a SCENARIOS key or a tuple of Steps."""
        self._scripts[pod_name] = scenario

    def inject(self, op, times=1, status=503, headers=None, body=None, apply=False):
        """Fail the next `times` requests of `op` ('any' matches all). `status` None drops the connection,
        HANG never answers (until stop()); `body` (bytes or a JSON-able value) replaces the Status body.
        `apply` makes the server carry the request out first and then fail the answer (a write that landed
        and whose reply was a 5xx or lost)."""
        with self._mutex:
            self._rules.append(
                {"op": op, "times": times, "status": status, "headers": headers or {}, "body": body, "apply": apply}
            )

    def calls(self, op=None):
        with self._mutex:
            return [record for record in self.requests if op is None or record.op == op]

    def pods(self, namespace=None):
        with self._mutex:
            self._tick()
            return {name: copy.deepcopy(p.manifest) for (ns, name), p in self._pods.items() if namespace in (None, ns)}

    def secrets(self, namespace=None):
        with self._mutex:
            self._tick()
            return {name: copy.deepcopy(s) for (ns, name), s in self._secrets.items() if namespace in (None, ns)}

    def delete_node(self, pod_name, namespace="factory-workers"):
        """The node of a pod is deleted: the pod object vanishes at once (and its Secrets with it)."""
        with self._mutex:
            self._pods.pop((namespace, pod_name), None)
            self._collect_garbage()

    def collect_garbage(self):
        with self._mutex:
            self._collect_garbage()

    # -- time -----------------------------------------------------------------------------------------

    def _tick(self):
        now = self.clock()
        for key, pod in list(self._pods.items()):
            self._advance(key, pod, now)
        self._collect_garbage()

    def _advance(self, key, pod, now):
        elapsed = now - pod.created
        while pod.applied < len(pod.steps) and pod.steps[pod.applied].at <= elapsed:
            step = pod.steps[pod.applied]
            pod.applied += 1
            if step.vanish:
                self._pods.pop(key, None)
                return
            if step.status is not None:
                pod.manifest["status"] = copy.deepcopy(step.status)
            if step.deletion:
                pod.manifest["metadata"]["deletionTimestamp"] = FIXED_TIME
        if pod.remove_at is not None and now >= pod.remove_at:
            self._pods.pop(key, None)

    def _collect_garbage(self):
        live = {(ns, p.manifest["metadata"]["name"], p.manifest["metadata"]["uid"]) for (ns, _n), p in self._pods.items()}
        for key, secret in list(self._secrets.items()):
            owners = secret["metadata"].get("ownerReferences") or []
            if owners and not any((key[0], o.get("name"), o.get("uid")) in live for o in owners if o.get("kind") == "Pod"):
                del self._secrets[key]

    # -- request handling ------------------------------------------------------------------------------

    def _handle(self, handler):
        length = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(length) if length else b""
        parts = urllib.parse.urlsplit(handler.path)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
        headers = {k.lower(): v for k, v in handler.headers.items()}
        namespace, resource, name, tail = self._route(parts.path)
        op = self._op(handler.command, resource, name, tail, query)
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = None
        record = Record(handler.command, parts.path, query, op, headers, body)
        with self._mutex:
            self.requests.append(record)
            fault = self._fault(op)
            if fault is None:
                self._tick()
                status, payload = self._dispatch(op, namespace, name, headers, body, query)
                self._send(handler, record, status, payload)
                return
            record.injected = True
            if fault.get("apply"):
                self._tick()
                self._dispatch(op, namespace, name, headers, body, query)  # applied; the answer below is the fault
        # Outside the lock: a hanging request must not block the retry that follows it.
        self._send_fault(handler, record, fault)

    @staticmethod
    def _route(path):
        segments = [urllib.parse.unquote(s) for s in path.strip("/").split("/")]
        if len(segments) >= 5 and segments[:2] == ["api", "v1"] and segments[2] == "namespaces":
            name = segments[5] if len(segments) > 5 else None
            tail = segments[6:] if len(segments) > 6 else []
            return segments[3], segments[4], name, tail
        return None, None, None, segments

    @staticmethod
    def _op(method, resource, name, tail, query):
        if resource not in ("pods", "secrets") or tail or "watch" in query:
            return "unsupported"
        singular = "pod" if resource == "pods" else "secret"
        if method == "POST" and name is None:
            return f"create_{singular}"
        if method == "DELETE" and name is not None:
            return f"delete_{singular}"
        if method == "GET" and resource == "pods":
            return "get_pod" if name is not None else "list_pods"
        return "unsupported"

    def _fault(self, op):
        for rule in self._rules:
            if rule["op"] in (op, "any") and rule["times"] > 0:
                rule["times"] -= 1
                return rule
        return None

    def _send_fault(self, handler, record, fault):
        status = fault["status"]
        if status == HANG:
            self._release.wait(timeout=10)
            handler.close_connection = True
            return
        if status is None:
            record.status = None
            handler.close_connection = True
            # The client may already have gone; a dropped connection is the point.
            with contextlib.suppress(OSError):
                handler.connection.shutdown(socket.SHUT_RDWR)
            return
        payload = fault["body"] if fault["body"] is not None else self._failure(status, "injected")
        self._send(handler, record, status, payload, fault["headers"])

    @staticmethod
    def _failure(code, message, **extra):
        return {
            "kind": "Status",
            "apiVersion": "v1",
            "status": "Failure",
            "message": message,
            "reason": _STATUS_REASONS.get(code, "Unknown"),
            "code": code,
            **extra,
        }

    @staticmethod
    def _send(handler, record, status, payload, headers=None):
        record.status = status
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(data)))
        for key, value in (headers or {}).items():
            handler.send_header(key, value)
        handler.end_headers()
        handler.wfile.write(data)

    # -- the six operations -------------------------------------------------------------------------------

    def _dispatch(self, op, namespace, name, headers, body, query):
        auth = headers.get("authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if not token or (self.tokens is not None and token not in self.tokens):
            return 401, self._failure(401, "Unauthorized")
        if op == "unsupported":
            return 403, self._failure(403, "forbidden: the role has no such verb")
        if op.startswith("create_") and (headers.get("content-type") or "").split(";")[0] != "application/json":
            return 415, self._failure(415, "unsupported media type")
        return getattr(self, f"_{op}")(namespace, name, body, query)

    def _next_uid(self):
        self._uids += 1
        return f"00000000-0000-4000-8000-{self._uids:012x}"

    def _create_pod(self, namespace, _name, body, _query):
        if not isinstance(body, dict) or body.get("kind") != "Pod" or not isinstance(body.get("metadata"), dict):
            return 400, self._failure(400, "bad pod")
        meta = body["metadata"]
        name = meta.get("name")
        if not isinstance(name, str) or meta.get("namespace", namespace) != namespace:
            return 400, self._failure(400, "bad pod metadata")
        if self.admit_image is not None:
            broken = podspec.check_policy(body, self.admit_image)
            if broken:
                return 403, self._denied(name, broken)
        if (namespace, name) in self._pods:
            return 409, self._failure(409, f'pods "{name}" already exists')
        manifest = copy.deepcopy(body)
        manifest["metadata"].update(
            {"uid": self._next_uid(), "namespace": namespace, "resourceVersion": "1", "creationTimestamp": FIXED_TIME}
        )
        manifest["status"] = {"phase": "Pending"}
        scenario = self._scripts.get(name, self.scenario)
        steps = SCENARIOS[scenario] if isinstance(scenario, str) else tuple(scenario)
        self._pods[(namespace, name)] = _Pod(manifest, self.clock(), steps)
        return 201, copy.deepcopy(manifest)

    def _denied(self, name, broken):
        ids = " ".join(f"[{rule}]" for rule in broken)
        # The shape a real API server answered (kind v1.36.1, docs/pod-backend-local-e2e.md): the policy's reason is Forbidden, so the
        # status is 403, and each message carries `[rule:<id>]` (infra/policy/pod-policy.json).
        tags = " ".join(f"[rule:{rule}] a rule of the policy was broken" for rule in broken)
        message = (
            f'pods "{name}" is forbidden: ValidatingAdmissionPolicy \'factory-worker-pods\' with binding '
            f"'factory-worker-pods' denied request: {tags}"
        )
        return self._failure(403, message, details={"name": name, "kind": "pods"})

    def _get_pod(self, namespace, name, _body, _query):
        pod = self._pods.get((namespace, name))
        if pod is None:
            return 404, self._failure(404, f'pods "{name}" not found')
        return 200, copy.deepcopy(pod.manifest)

    def _list_pods(self, namespace, _name, _body, query):
        try:
            parse_selector(query.get("labelSelector", ""))  # a malformed selector is a 400 even when nothing matches
            matching = [
                copy.deepcopy(p.manifest)
                for (ns, _n), p in sorted(self._pods.items())
                if ns == namespace and match_selector(query.get("labelSelector", ""), p.manifest["metadata"].get("labels") or {})
            ]
        except ValueError:
            return 400, self._failure(400, "unable to parse the label selector")
        start = int(query["continue"]) if query.get("continue", "").isdigit() else 0
        size = min(int(query.get("limit") or len(matching) or 1), self.page_size or len(matching) or 1)
        page = matching[start : start + size]
        meta = {"resourceVersion": "1"}
        if start + size < len(matching):
            meta["continue"] = str(start + size)
        return 200, {"kind": "PodList", "apiVersion": "v1", "metadata": meta, "items": page}

    def _delete_pod(self, namespace, name, body, _query):
        pod = self._pods.get((namespace, name))
        if pod is None:
            return 404, self._failure(404, f'pods "{name}" not found')
        grace = (body or {}).get("gracePeriodSeconds", 30)
        phase = (pod.manifest.get("status") or {}).get("phase")
        if phase == "Running" and grace > 0:
            pod.manifest["metadata"]["deletionTimestamp"] = FIXED_TIME
            pod.remove_at = self.clock() + grace
            return 200, copy.deepcopy(pod.manifest)
        del self._pods[(namespace, name)]
        self._collect_garbage()
        return 200, {"kind": "Status", "apiVersion": "v1", "status": "Success"}

    def _create_secret(self, namespace, _name, body, _query):
        if not isinstance(body, dict) or body.get("kind") != "Secret" or not isinstance(body.get("metadata"), dict):
            return 400, self._failure(400, "bad secret")
        meta = body["metadata"]
        name = meta.get("name")
        if not isinstance(name, str) or meta.get("namespace", namespace) != namespace:
            return 400, self._failure(400, "bad secret metadata")
        if self.admit_image is not None:
            owners = meta.get("ownerReferences")
            owner = owners[0].get("name") if isinstance(owners, list) and owners and isinstance(owners[0], dict) else ""
            broken = podspec.check_secret_policy(body, owner if isinstance(owner, str) else "")
            if broken:
                return 403, self._denied(name, broken)
        if (namespace, name) in self._secrets:
            return 409, self._failure(409, f'secrets "{name}" already exists')
        manifest = copy.deepcopy(body)
        manifest["metadata"].update({"uid": self._next_uid(), "namespace": namespace, "resourceVersion": "1"})
        self._secrets[(namespace, name)] = manifest
        return 201, copy.deepcopy(manifest)  # the real API echoes the data back; the client must drop it

    def _delete_secret(self, namespace, name, _body, _query):
        if (namespace, name) not in self._secrets:
            return 404, self._failure(404, f'secrets "{name}" not found')
        del self._secrets[(namespace, name)]
        return 200, {"kind": "Status", "apiVersion": "v1", "status": "Success"}


# --- Fixtures shared by the tests -------------------------------------------------------------------

IMAGE = f"registry.example.invalid/factory@sha256:{'0123456789abcdef' * 4}"
INSTANCE = "abcdef012345"
REPOSITORY = "Example-Org/example-repo"
NAMESPACE = "factory-workers"


def session_id(n):
    """The n-th session id (32 lowercase hex characters)."""
    return f"{n:032x}"


def worker_pod(n=1, repository=REPOSITORY, phase="build", instance=INSTANCE):
    """The manifest `podspec.build_pod` makes for session `n` (a fixed clock, so it is reproducible)."""
    config = podspec.PodConfig(
        IMAGE, "http://10.244.1.7:8090", "10.244.1.7", "10.96.0.9", "example-cluster.hcp.example.invalid", clock=lambda: 1_800_000_000
    )
    request = {
        "session_id": session_id(n),
        "repository": repository,
        "run_id": "run-20260101-0001",
        "job_id": "build-1",
        "attempt": 1,
        "phase": phase,
        "resource_class": "standard",
        "timeout_seconds": 1800,
        "manager_instance": instance,
    }
    return podspec.build_pod(request, config)

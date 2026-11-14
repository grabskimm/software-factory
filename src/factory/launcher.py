"""The pod launcher, as its own program: ``python -m factory.launcher``.

Slice 11 of the pod backend (``docs/pod-backend.md``). The launcher is a container of the manager pod
(wired by a later Terraform slice) and the ONLY holder of a Kubernetes token and, besides the worker pod
itself, of the Anthropic key. The supervisor in the main container asks it, over a unix socket, to start a
worker pod, to read its state and to delete it. Nothing in the package imports this module yet, nothing
starts it, and the backend is off by default.

Who may talk to it. The socket is ``/run/factory/launcher.sock`` in the root of an ``emptyDir`` shared with
the main container. That directory is NOT this program's to set up (it runs non-root with every capability
dropped and cannot ``chown``): an init container makes it owned by the launcher uid, group 10001, mode 0750,
and ``main`` REFUSES TO START when the directory is not owned by its own uid, is writable by group or others,
is a symbolic link, or when something that is not its own stale socket sits at the socket path. The socket
itself is created 0660. Authentication is ``SO_PEERCRED``: the uid of the connecting process must equal the
configured allowed uid (10001), and a connection from anyone else is closed WITHOUT reading a byte. There is no
bearer token anywhere (nothing for an environment deny-list to miss).

The wire protocol is one request per connection: a single line of JSON, a single line of JSON back. The schema
is CLOSED: ``launch``, ``status``, ``terminate``, ``list``, ``info`` and ``deny``, each with a fixed set of
fields, and an unknown or missing field, a duplicate key or a wrong type is ``invalid``. A request is bounded
in size (``MAX_REQUEST_BYTES``) and in time (``REQUEST_TIMEOUT_SECONDS`` for the whole line, however slowly it
trickles in), at most ``MAX_CONNECTIONS`` connections are served at once (one beyond that is answered ``busy``
at once and holds no thread), and every thread is joined with a bound when the server closes. The image digest,
the namespace and every cap come ONLY from this program's own environment (set by Terraform), never from a
request: a launch carries only the closed request of ``podspec`` plus the one-time relay token.

What it enforces (``Config``): a maximum number of live pods (counted by LISTING the labelled pods, plus an
in-process in-flight counter so that two launches at the same moment cannot both take the last slot), a
per-repository cap (default 4), a deadline clamp (default and at most 14400 s, a longer request is reduced and
the answer says so) and a minimum (below ``MIN_TIMEOUT_SECONDS`` is ``invalid``), the class enum and the phase
enum. A launch over a cap answers the fixed code ``capacity``: the supervisor waits, it is not a failure.

Launch order: build the pod (``podspec.build_pod``, the ONLY way a pod is built here), create it, create the
Secret that holds the relay token and the Anthropic key with an ownerReference to the pod (``podspec.build_secret``),
return name and uid. Any failure after the pod may exist deletes the pod and the Secret by their derived names
(a 404 is fine); the pod is not deleted when the API certainly refused to create it (a pod of that name is then
someone else's). The two secret values are written to the API and nowhere else: never logged, never returned,
never in an error text.

The scheduling timeout (``Launcher.sweep``, run every ``SWEEP_INTERVAL_SECONDS``, on an injectable clock): a pod
that stays Pending (waiting for a node, its Secret or its image) past the contract's 600 s, a pod that outlives
its deadline plus the grace (the kubelet enforces ``activeDeadlineSeconds`` only once it has accepted the pod, so
a pod stuck Pending has no deadline), and a pod stuck in ``ImagePullBackOff``, ``ErrImagePull`` or
``CreateContainerConfigError`` for 120 s are deleted by the launcher itself. The pod is then gone from the API,
so the launcher remembers (in memory, bounded) why, and ``status`` answers ``unschedulable``,
``starting_error`` or ``deadline`` instead of ``gone`` for it. A launcher that restarts forgets and answers
``gone``.

``deny(reason)`` makes the launcher refuse every launch until it restarts (fail closed after an isolation
failure); the other operations keep working so that pods can still be read and deleted. The launcher runs no
child process and makes no outbound call except to the API server (a test reads this file's syntax tree), and
logs fixed lines only: no request text, no pod name, no manifest, no token and no key.

CONTRACT-TESTED AGAINST ASSUMED SHAPES. Every Kubernetes answer the tests use comes from the in-memory fake of
``tests/fake_kube.py``; no cluster was consulted. ``SO_PEERCRED`` was exercised only inside one container (the
only way to meet a foreign peer in one process is to configure another allowed uid); what it reports across
containers that share an ``emptyDir`` under ``hostUsers: false`` is UNVERIFIED.

Standard library only.
"""

import collections
import contextlib
import json
import os
import re
import signal
import socket
import stat
import struct
import sys
import threading
import time
from dataclasses import dataclass, field

from . import kube
from . import podcontract as pc
from . import podspec
from . import podstatus

# --- Numbers of the program (the caps of a session are the contract's or the environment's) ------------------

MAX_REQUEST_BYTES = 16 * 1024
REQUEST_TIMEOUT_SECONDS = 5.0
REPLY_TIMEOUT_SECONDS = 10.0
MAX_REPLY_BYTES = 1024 * 1024
MAX_CONNECTIONS = 16
MIN_TIMEOUT_SECONDS = 60
SWEEP_INTERVAL_SECONDS = 15
MAX_LIST_PODS = 1000
MAX_REMEMBERED = 1024
MAX_CAP = 1000
SOCKET_MODE = 0o660
JOIN_SECONDS = 5.0
ACCEPT_RETRY_SECONDS = 0.5  # the pause after an accept() error that is not a closed listener
_RECV_BYTES = 4096
_CREDENTIALS = struct.Struct("3i")  # struct ucred: pid, uid, gid
_MAX_SOCKET_PATH = 100  # AF_UNIX limits a path to about 108 bytes

# The fixed answer codes. The client repeats the set (tests/test_launcher.py compares the two).
ERROR_CODES = (
    "invalid",
    "too_large",
    "timeout",
    "busy",
    "denied",
    "capacity",
    "unavailable",
    "rejected",
    "conflict",
    "failed",
)

OPS = ("launch", "status", "terminate", "list", "info", "deny")
# op -> (every field it may carry, the fields it must carry)
_OP_FIELDS = {
    "launch": ({"op", "token", *podspec.REQUEST_FIELDS}, {"op", "token", *podspec.REQUEST_FIELDS}),
    "status": ({"op", "name", "uid"}, {"op", "name"}),
    "terminate": ({"op", "name", "uid"}, {"op", "name"}),
    "list": ({"op"}, {"op"}),
    "info": ({"op"}, {"op"}),
    "deny": ({"op", "reason"}, {"op", "reason"}),
}

_UID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_REASON = re.compile(r"[a-z][a-z_]{0,39}")
_SELECTOR = {pc.label_key("role"): pc.LABEL_ROLE_VALUE}
_TERMINATING_ROW = "terminating"  # the row of ``podstatus`` for a pod with a deletionTimestamp that is not over

# Why the launcher deleted a pod itself, and what `status` then says of the vanished pod.
REAP_REASONS = {
    "scheduling_timeout": (podstatus.UNSCHEDULABLE, "scheduling_timeout"),
    "starting_error": (podstatus.STARTING_ERROR, "pull_or_config_error"),
    "deadline": (podstatus.DEADLINE, "launcher_backstop"),
}

# The Kubernetes environment names the launcher reads besides its own configuration.
KEY_ENV = "ANTHROPIC_API_KEY"


def _log(message):
    """One fixed, secret-free line on stderr. Callers pass only literal words and numbers."""
    sys.stderr.write(f"factory-launcher: {message}\n")


# --- Configuration ------------------------------------------------------------------------------------------


class ConfigRefused(Exception):
    """The environment of the launcher is unusable. `code` is a fixed word; the message never carries a value."""

    def __init__(self, code):
        super().__init__(f"refusing to start: {code}")
        self.code = code


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)  # a bool is not a number here


@dataclass(frozen=True)
class Config:
    """The immutable configuration of one launcher: image, namespace, caps and the one secret it holds.

    Every field is validated on construction (a refusal names the field, never its value). The caps and the
    image never come from a request. The key is not part of ``repr``.
    """

    image: str
    namespace: str
    max_pods: int
    repo_cap: int
    deadline_clamp: int
    allowed_uid: int
    relay_url: str
    classes: tuple
    api_public_host: str
    manager_address: str
    manager_metrics_address: str
    socket_path: str
    anthropic_key: str = field(repr=False)

    def __post_init__(self):
        self._check_numbers()
        self._check_names()
        try:
            self.pod_config()
        except ValueError as problem:
            raise ConfigRefused(_refused_field(str(problem))) from None
        try:
            podspec.build_secret(_dry_pod(), "x", self.anthropic_key)
        except ValueError:
            raise ConfigRefused("anthropic_key") from None

    def _check_numbers(self):
        if not (_is_int(self.max_pods) and 1 <= self.max_pods <= MAX_CAP):
            raise ConfigRefused("max_pods")
        if not (_is_int(self.repo_cap) and 1 <= self.repo_cap <= MAX_CAP):
            raise ConfigRefused("repo_cap")
        if not (_is_int(self.deadline_clamp) and MIN_TIMEOUT_SECONDS <= self.deadline_clamp <= pc.DEADLINE_CLAMP_SECONDS):
            raise ConfigRefused("deadline_clamp")
        if not (_is_int(self.allowed_uid) and 0 <= self.allowed_uid < 2**31):
            raise ConfigRefused("allowed_uid")

    def _check_names(self):
        # podspec builds pods only in the contract's namespace, so the environment must repeat it.
        if self.namespace != pc.WORKER_NAMESPACE:
            raise ConfigRefused("namespace")
        if not (isinstance(self.classes, tuple) and self.classes and all(name in pc.CLASSES for name in self.classes)):
            raise ConfigRefused("classes")
        path = self.socket_path
        if not (isinstance(path, str) and path.startswith("/") and "\0" not in path and len(path.encode("utf-8")) <= _MAX_SOCKET_PATH):
            raise ConfigRefused("socket")

    def pod_config(self):
        """The `podspec.PodConfig` of this launcher (ValueError for an unusable image, address or host)."""
        return podspec.PodConfig(self.image, self.relay_url, self.manager_address, self.manager_metrics_address, self.api_public_host)

    @classmethod
    def from_environment(cls, environ):
        """The configuration of the process environment `environ`, or ConfigRefused with the name of the field."""
        names = pc.ENV_LAUNCHER
        key = environ.get(KEY_ENV)
        if not isinstance(key, str) or not key:
            raise ConfigRefused("anthropic_key")
        return cls(
            image=_required(environ, names["image"], "image"),
            namespace=environ.get(names["namespace"], pc.WORKER_NAMESPACE),
            max_pods=_integer(environ, names["max_pods"], "max_pods", None),
            repo_cap=_integer(environ, names["repo_cap"], "repo_cap", pc.PER_REPO_CAP),
            deadline_clamp=_integer(environ, names["deadline_clamp"], "deadline_clamp", pc.DEADLINE_CLAMP_SECONDS),
            allowed_uid=_integer(environ, names["allowed_uid"], "allowed_uid", pc.SUPERVISOR_UID),
            relay_url=_required(environ, names["relay_url"], "relay_url"),
            classes=_classes(environ.get(names["classes"])),
            api_public_host=_required(environ, names["api_public_host"], "api_public_host"),
            manager_address=_required(environ, names["manager_address"], "manager_address"),
            manager_metrics_address=_required(environ, names["manager_metrics_address"], "manager_metrics_address"),
            socket_path=environ.get(names["socket"], pc.LAUNCHER_SOCKET),
            anthropic_key=key,
        )


def _refused_field(problem):
    """The field a `PodConfig` ValueError is about (its texts are fixed), as a word for ConfigRefused."""
    for word, code in (("relay", "relay_url"), ("manager metrics", "manager_metrics_address"), ("manager", "manager_address"), ("api", "api_public_host")):
        if word in problem:
            return code
    return "image"


def _dry_pod():
    """A stand-in pod answer, so that the key is checked with the real validator when the launcher starts."""
    name = pc.pod_name("0" * pc.LABEL_HEX_LENGTHS["session"])
    return {"metadata": {"name": name, "uid": "00000000-0000-4000-8000-000000000000", "namespace": pc.WORKER_NAMESPACE}}


def _required(environ, name, code):
    value = environ.get(name)
    if not isinstance(value, str) or not value:
        raise ConfigRefused(code)
    return value


def _integer(environ, name, code, default):
    text = environ.get(name)
    if text is None:
        if default is None:
            raise ConfigRefused(code)
        return default
    if not (isinstance(text, str) and text.isascii() and text.isdigit() and len(text) <= 10):
        raise ConfigRefused(code)
    return int(text)


def _classes(text):
    if text is None:
        return tuple(pc.CLASSES)
    return tuple(part.strip() for part in text.split(",") if part.strip())


# --- Requests ---------------------------------------------------------------------------------------------------


class Invalid(Exception):
    """A request that is not the closed schema. Carries no text: the reply is the fixed code `invalid`."""


def _no_duplicates(pairs):
    found = {}
    for key, value in pairs:
        if key in found:
            raise Invalid()
        found[key] = value
    return found


def parse_request(line):
    """The request dict of one line of bytes: closed schema, no duplicate keys, bounded; Invalid otherwise."""
    try:
        request = json.loads(line.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except (ValueError, RecursionError):  # includes UnicodeDecodeError and json.JSONDecodeError
        raise Invalid() from None
    if not isinstance(request, dict):
        raise Invalid()
    op = request.get("op")
    if not isinstance(op, str) or op not in _OP_FIELDS:
        raise Invalid()
    allowed, required = _OP_FIELDS[op]
    keys = set(request)
    if not required <= keys <= allowed:
        raise Invalid()
    return request


def _text_field(request, name, pattern):
    value = request.get(name)
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise Invalid()
    return value


def _pod_name(request):
    name = request.get("name")
    try:
        pc.secret_name(name)  # ValueError for anything but a worker pod name
    except ValueError:
        raise Invalid() from None
    return name


def _pod_uid(request):
    return _text_field(request, "uid", _UID) if "uid" in request else None


def _error(code, **extra):
    return {"ok": False, "error": code, **extra}


# --- Admission counter ----------------------------------------------------------------------------------------------


class Admission:
    """The in-process half of the live-pod cap: launches that have been admitted and are not yet visible in a listing.

    A listing alone would let two launches that run at the same moment both see one free slot. ``try_admit`` is
    called with the number of live pods (and of this repository's) that the listing showed, and counts the launches
    still in flight on top of them.

    The in-flight count must be read BEFORE the listing it is combined with (``snapshot``, passed as ``before``):
    a launch that finishes between the listing and the admission has created its pod after the listing was taken,
    so the listing does not show it, and the count read afterwards no longer holds it either. Every admission is
    made under the launcher's admission lock, so nothing can be admitted between the snapshot and ``try_admit``:
    the snapshot is never smaller than the launches in flight at that moment. A launch counted in both the listing
    and the snapshot is counted twice, which can only refuse a launch early, never admit one too many.
    """

    def __init__(self, max_pods, repo_cap):
        self._max = max_pods
        self._repo_cap = repo_cap
        self._mutex = threading.Lock()
        self._inflight = 0
        self._by_repo = collections.Counter()

    def snapshot(self):
        """(launches in flight, the same per repository) now: to be taken before the listing it goes with."""
        with self._mutex:
            return self._inflight, collections.Counter(self._by_repo)

    def try_admit(self, repo, live, live_in_repo, before=None):
        with self._mutex:
            inflight, by_repo = self._inflight, self._by_repo[repo]
            if before is not None:
                inflight, by_repo = max(inflight, before[0]), max(by_repo, before[1][repo])
            if live + inflight >= self._max or live_in_repo + by_repo >= self._repo_cap:
                return False
            self._inflight += 1
            self._by_repo[repo] += 1
            return True

    def release(self, repo):
        with self._mutex:
            self._inflight -= 1
            self._by_repo[repo] -= 1
            if self._by_repo[repo] <= 0:
                del self._by_repo[repo]

    @property
    def inflight(self):
        with self._mutex:
            return self._inflight


# --- What the launcher reads of a listed pod -----------------------------------------------------------------------


@dataclass(frozen=True)
class PodView:
    """The few validated fields of a listed pod the launcher acts on (no free text of the pod is kept)."""

    name: str
    uid: str | None
    repo: str | None
    manager: str | None
    outcome: object
    created: int | None
    deadline: int | None
    terminating: bool


def _at(document, *path):
    found = document
    for step in path:
        if not isinstance(found, dict):
            return None
        found = found.get(step)
    return found


def _label(item, key):
    value = _at(item, "metadata", "labels", pc.label_key(key))
    return value if isinstance(value, str) else None


def _epoch(value):
    """An integer epoch from an annotation text, else None."""
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 12:
        return int(value)
    return None


def inspect_pod(item):
    """A `PodView` of a listed pod, or None when it is not a pod this launcher can manage (a foreign name)."""
    name = _at(item, "metadata", "name")
    try:
        pc.secret_name(name)
    except ValueError:
        return None
    uid = _at(item, "metadata", "uid")
    deadline = _epoch(_at(item, "metadata", "annotations", pc.annotation_key("deadline")))
    seconds = _at(item, "spec", "activeDeadlineSeconds")
    # The creation time is derived from the launcher's own clock (the deadline annotation is that clock plus the
    # deadline seconds), not from the API server's timestamp, so no clock skew can age a pod early or late.
    created = deadline - seconds if deadline is not None and _is_int(seconds) and 0 < seconds <= deadline else None
    return PodView(
        name=name,
        uid=uid if isinstance(uid, str) and _UID.fullmatch(uid) else None,
        repo=_label(item, "repo"),
        manager=_label(item, "manager"),
        outcome=podstatus.classify(item),
        created=created,
        deadline=deadline,
        terminating=bool(_at(item, "metadata", "deletionTimestamp")),
    )


def _outcome_reply(outcome, terminating):
    """The status answer for a pod that the API returned (so `present`). `terminating` is True while the pod is being
    deleted and is not over: its containers may still run, although the table of ``podstatus`` reads a deletion as a
    final state."""
    return {
        "ok": True,
        "state": outcome.state,
        "detail": outcome.detail,
        "final": outcome.final,
        "exit_code": outcome.exit_code,
        "present": True,
        "terminating": terminating,
    }


def _may_exist(failure):
    """Whether a pod may exist after `create_pod` raised `failure` (a certain refusal means it does not)."""
    if isinstance(failure, kube.BadResponse):
        return True  # a 2xx whose body was unusable: the pod was created
    # A 409 after an earlier attempt that may have been applied (a 5xx whose retry then found the pod) is possibly
    # our own pod; a 409 on a first attempt, or after attempts that certainly were not applied, is someone else's.
    return isinstance(failure, (kube.Unavailable, kube.AlreadyExists)) and failure.outcome_unknown


def _kind(failure):
    """A fixed word for an error of the Kubernetes client (its own `kind`) or of a client-side validation."""
    return failure.kind if isinstance(failure, kube.KubeError) else "value"


def _failure_reply(failure):
    """The fixed reply for a Kubernetes error of a launch (a ValueError of the client is `failed`)."""
    if isinstance(failure, kube.Unavailable):
        return _error("unavailable")
    if isinstance(failure, kube.AlreadyExists):
        # A 409 that follows a possibly-applied attempt is probably our own retry finding our own pod, which the
        # launcher has then deleted: that is a failed create to try again, not a pod of someone else.
        return _error("unavailable" if failure.outcome_unknown else "conflict")
    if isinstance(failure, kube.Denied):
        return _error("rejected", rules=list(failure.rules))
    return _error("failed")


# --- The launcher ----------------------------------------------------------------------------------------------------


class Launcher:
    """The operations of the launcher over a Kubernetes client, with no socket in it (``Server`` carries those).

    `client` has the six operations of ``kube.Client`` (a test passes the real client against the fake API server).
    `clock` returns epoch seconds and also dates the deadline of every pod it builds. `log` receives fixed lines.
    """

    def __init__(self, client, config, clock=time.time, log=_log):
        self._client = client
        self._config = config
        self._clock = clock
        self._log = log
        self._pod_config = podspec.PodConfig(config.image, config.relay_url, config.manager_address, config.manager_metrics_address, config.api_public_host, clock=clock)
        self._admission = Admission(config.max_pods, config.repo_cap)
        self._admit_lock = threading.Lock()
        self._mutex = threading.Lock()
        self._denied = None
        self._first_seen = {}
        self._error_since = {}
        self._reaped = collections.OrderedDict()
        self._reap_counts = collections.Counter()

    # -- entry point -----------------------------------------------------------------------------------------

    def handle_line(self, line):
        """The reply (a dict) to one request line of bytes. Never raises: a bug is the fixed code `failed`."""
        try:
            request = parse_request(line)
            return getattr(self, f"_op_{request['op']}")(request)
        except Invalid:
            return _error("invalid")
        except Exception as bug:  # one request must not take the launcher down; only the type name is logged
            self._log(f"internal error {type(bug).__name__}")
            return _error("failed")

    # -- deny ----------------------------------------------------------------------------------------------

    @property
    def denied(self):
        with self._mutex:
            return self._denied

    def _op_deny(self, request):
        reason = request["reason"]
        word = reason if isinstance(reason, str) and _REASON.fullmatch(reason) else "unspecified"
        with self._mutex:
            first = self._denied is None
            if first:
                self._denied = word
        if first:
            self._log(f"launches denied reason={word}")
        return {"ok": True}

    # -- launch --------------------------------------------------------------------------------------------

    def _plan(self, request):
        """(manifest, repository label, effective timeout, clamped) of a launch; ValueError for any invalid field."""
        fields = podspec.validate_request({name: request[name] for name in podspec.REQUEST_FIELDS})
        token = request["token"]
        if fields["resource_class"] not in self._config.classes:
            raise ValueError("invalid resource class")
        seconds = fields["timeout_seconds"]
        if not _is_int(seconds) or seconds < MIN_TIMEOUT_SECONDS:
            raise ValueError("invalid timeout")
        effective = min(seconds, self._config.deadline_clamp)
        fields["timeout_seconds"] = effective
        manifest = podspec.build_pod(fields, self._pod_config)
        # The same validator the Secret goes through later, before anything is created: a bad token fails here.
        podspec.build_secret(_stand_in(manifest), token, self._config.anthropic_key)
        return manifest, manifest["metadata"]["labels"][pc.label_key("repo")], effective, effective != seconds

    def _admit(self, repo):
        """True when the launch fits the caps and its slot is taken.

        The in-flight count is snapshotted before the listing, under the admission lock, which every admission
        takes: a launch that finishes while this one lists is then counted by one of the two (see ``Admission``).
        """
        with self._admit_lock:
            before = self._admission.snapshot()
            items = self._client.list_pods(_SELECTOR)
            in_repo = sum(1 for item in items if _label(item, "repo") == repo)
            return self._admission.try_admit(repo, len(items), in_repo, before)

    def _op_launch(self, request):
        try:
            manifest, repo, effective, clamped = self._plan(request)
        except ValueError:
            return _error("invalid")
        if self.denied is not None:
            return _error("denied")
        try:
            admitted = self._admit(repo)
        except (kube.KubeError, ValueError) as failure:
            self._log(f"launch refused: listing failed {_kind(failure)}")
            return _error("unavailable")
        if not admitted:
            self._log("launch refused: capacity")
            return _error("capacity")
        try:
            pod = self._create(manifest, request["token"])
        except (kube.KubeError, ValueError) as failure:
            self._log(f"launch failed {_kind(failure)}")
            return _failure_reply(failure)
        finally:
            self._admission.release(repo)
        self._log("launched")
        return {"ok": True, "name": pod["metadata"]["name"], "uid": pod["metadata"]["uid"], "timeout_seconds": effective, "clamped": clamped}

    def _create(self, manifest, token):
        """Create the pod, then its Secret; on any failure after the pod may exist, delete both. Returns the pod."""
        name = manifest["metadata"]["name"]
        exists = [True]  # may the pod exist? a list so that the helper can answer
        done = False
        try:
            pod = self._create_pod(manifest, exists)
            self._client.create_secret(podspec.build_secret(pod, token, self._config.anthropic_key))
            done = True
            return pod
        finally:
            if not done and exists[0]:
                self._compensate(name)

    def _create_pod(self, manifest, exists):
        try:
            return self._client.create_pod(manifest)
        except kube.KubeError as failure:
            exists[0] = _may_exist(failure)
            raise
        except ValueError:  # refused by the client before anything was sent
            exists[0] = False
            raise

    def _compensate(self, name):
        """Delete the pod and then its Secret by their derived names (a 404 is fine); never raises."""
        self._delete_quietly("pod", self._client.delete_pod, name)
        self._delete_quietly("secret", self._client.delete_secret, pc.secret_name(name))

    def _delete_quietly(self, what, action, target):
        try:
            with contextlib.suppress(kube.NotFound):
                action(target)
        except (kube.KubeError, ValueError):
            self._log(f"compensation failed to delete {what}")

    # -- status, terminate, list, info ------------------------------------------------------------------------------

    def _op_status(self, request):
        name, uid = _pod_name(request), _pod_uid(request)
        try:
            pod = self._client.get_pod(name)
        except kube.NotFound:
            return self._gone(name, "not_found")
        except (kube.KubeError, ValueError):
            return _error("unavailable")
        if uid is not None and _at(pod, "metadata", "uid") != uid:
            return self._gone(name, "uid_mismatch")  # another pod under this name is not this session's pod
        outcome = podstatus.classify(pod)
        # A pod the launcher deleted may still be there for its grace period, terminating: say why it is going, and
        # that it is still there (`present`, `terminating`): its containers may run until it is gone.
        terminating = outcome.row == _TERMINATING_ROW
        # The remembered reason is for a pod that is going or gone; a pod of that name that is not over is not it.
        remembered = self._remembered(name, present=True, terminating=terminating) if outcome.final else None
        return remembered or _outcome_reply(outcome, terminating)

    def _remembered(self, name, present, terminating):
        """The reply for a pod the launcher itself deleted (the reason it gave), or None."""
        with self._mutex:
            reaped = self._reaped.get(name)
        if reaped is None:
            return None
        state, why = reaped
        return {"ok": True, "state": state, "detail": why, "final": True, "exit_code": None, "present": present, "terminating": terminating}

    def _gone(self, name, detail):
        """The state of a pod the API no longer has: the reason the launcher deleted it, else `gone`."""
        gone = {"ok": True, "state": podstatus.GONE, "detail": detail, "final": True, "exit_code": None, "present": False, "terminating": False}
        return self._remembered(name, present=False, terminating=False) or gone

    def _op_terminate(self, request):
        name, uid = _pod_name(request), _pod_uid(request)
        try:
            if uid is not None and not self._same_pod(name, uid):
                return {"ok": True, "deleted": False}
            deleted = True
            try:
                self._client.delete_pod(name)
            except kube.NotFound:
                deleted = False
            with contextlib.suppress(kube.NotFound):
                self._client.delete_secret(pc.secret_name(name))
        except (kube.KubeError, ValueError):
            self._log("terminate failed")
            return _error("unavailable")
        return {"ok": True, "deleted": deleted}

    def _same_pod(self, name, uid):
        try:
            return _at(self._client.get_pod(name), "metadata", "uid") == uid
        except kube.NotFound:
            return False

    def _op_list(self, _request):
        try:
            items = self._client.list_pods(_SELECTOR)
        except (kube.KubeError, ValueError):
            return _error("unavailable")
        views = [view for view in (inspect_pod(item) for item in items) if view is not None]
        pods = [_listed(view) for view in views[:MAX_LIST_PODS]]
        return {"ok": True, "pods": pods, "truncated": len(views) > MAX_LIST_PODS}

    def _op_info(self, _request):
        live = None
        with contextlib.suppress(kube.KubeError, ValueError):
            live = len(self._client.list_pods(_SELECTOR))
        with self._mutex:
            denied, counts = self._denied, dict(self._reap_counts)
        return {
            "ok": True,
            "max_pods": self._config.max_pods,
            "repo_cap": self._config.repo_cap,
            "deadline_clamp": self._config.deadline_clamp,
            "min_timeout": MIN_TIMEOUT_SECONDS,
            "classes": list(self._config.classes),
            "live": live,
            "inflight": self._admission.inflight,
            "denied": denied is not None,
            "deny_reason": denied,
            "reaped": counts,
        }

    # -- the scheduling timeout -------------------------------------------------------------------------------------

    def sweep(self):
        """One pass over the labelled pods: delete what is overdue. Returns the number of pods deleted.

        Raises a ``kube.KubeError`` when the pods cannot be listed: nothing is deleted on a guess.
        """
        items = self._client.list_pods(_SELECTOR)
        now = self._clock()
        views = [view for view in (inspect_pod(item) for item in items) if view is not None]
        deleted = 0
        for view in views:
            reason = self._overdue(view, now)
            if reason is not None and self._reap(view, reason):
                deleted += 1
        self._forget({view.name for view in views})
        return deleted

    def _overdue(self, view, now):
        """The reason a pod is to be deleted (a key of REAP_REASONS), or None."""
        if view.terminating:
            return None
        if view.deadline is not None and now >= view.deadline + pc.TERMINATION_GRACE_SECONDS:
            return "deadline"
        state = view.outcome.state
        if state in (podstatus.PENDING, podstatus.UNSCHEDULABLE):
            since = view.created if view.created is not None else self._seen(self._first_seen, view.name, now)
            return "scheduling_timeout" if now - since >= pc.SCHEDULING_TIMEOUT_SECONDS else None
        if state == podstatus.STARTING_ERROR and not view.outcome.final:
            since = self._seen(self._error_since, view.name, now)
            return "starting_error" if now - since >= pc.PULL_ERROR_SECONDS else None
        with self._mutex:
            self._error_since.pop(view.name, None)  # the error cleared: its window starts again if it comes back
        return None

    def _seen(self, table, name, now):
        with self._mutex:
            return table.setdefault(name, now)

    def _reap(self, view, reason):
        try:
            with contextlib.suppress(kube.NotFound):
                self._client.delete_pod(view.name)
            with contextlib.suppress(kube.NotFound):
                self._client.delete_secret(pc.secret_name(view.name))
        except (kube.KubeError, ValueError):
            self._log(f"reaping failed reason={reason}")
            return False
        with self._mutex:
            self._reaped[view.name] = REAP_REASONS[reason]
            while len(self._reaped) > MAX_REMEMBERED:
                self._reaped.popitem(last=False)
            self._reap_counts[reason] += 1
        self._log(f"reaped reason={reason}")
        return True

    def _forget(self, listed):
        with self._mutex:
            for table in (self._first_seen, self._error_since):
                for name in [name for name in table if name not in listed]:
                    del table[name]


def _stand_in(manifest):
    """A pod answer for the dry validation of the Secret: the manifest's own name and a fixed uid."""
    return {"metadata": {"name": manifest["metadata"]["name"], "uid": "00000000-0000-4000-8000-000000000000", "namespace": pc.WORKER_NAMESPACE}}


def _listed(view):
    return {
        "name": view.name,
        "uid": view.uid,
        "state": view.outcome.state,
        "final": view.outcome.final,
        "terminating": view.terminating,
        "manager": view.manager,
        "created": view.created,
        "deadline": view.deadline,
    }


# --- Transport: one request per connection ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Limits:
    """The bounds of the socket server; a test may lower them."""

    max_connections: int = MAX_CONNECTIONS
    request_timeout: float = REQUEST_TIMEOUT_SECONDS
    reply_timeout: float = REPLY_TIMEOUT_SECONDS
    max_request_bytes: int = MAX_REQUEST_BYTES


class RequestTooLarge(Exception):
    """The line is longer than the bound."""


def peer_uid(conn):
    """The uid of the process at the other end of a unix socket (``SO_PEERCRED``); OSError when it cannot be read."""
    option = getattr(socket, "SO_PEERCRED", None)
    if option is None:
        raise OSError("SO_PEERCRED is not available")
    return _CREDENTIALS.unpack(conn.getsockopt(socket.SOL_SOCKET, option, _CREDENTIALS.size))[1]


def peer_allowed(conn, allowed_uid):
    """True only when the peer's uid is read and equals `allowed_uid`: anything else, an error included, is False."""
    try:
        return peer_uid(conn) == allowed_uid
    except (OSError, struct.error):
        return False


def read_line(conn, limit, deadline, clock):
    """One line (without the newline) from `conn`, at most `limit` bytes and complete by `deadline`.

    A peer that trickles bytes does not extend the deadline. Raises RequestTooLarge when the line is over the
    bound, TimeoutError (an OSError) when the deadline passes, EOFError when the peer closes before the newline.
    """
    chunks = []
    size = 0
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError("request took too long")
        conn.settimeout(remaining)
        data = conn.recv(min(_RECV_BYTES, limit + 1 - size))
        if not data:
            raise EOFError("closed before the end of the line")
        end = data.find(b"\n")
        chunks.append(data if end < 0 else data[:end])
        size += len(chunks[-1])
        if size > limit:
            raise RequestTooLarge()
        if end >= 0:
            return b"".join(chunks)


def send_reply(conn, document, timeout, clock):
    """Write one reply line within `timeout` seconds in total; a peer that is gone or slow is dropped silently."""
    data = f"{json.dumps(document, separators=(',', ':'))}\n".encode("utf-8")
    deadline = clock() + timeout
    with contextlib.suppress(OSError):
        while data:
            remaining = deadline - clock()
            if remaining <= 0:
                return
            conn.settimeout(remaining)
            sent = conn.send(data[:65536])
            data = data[sent:]


def serve_connection(conn, handler, allowed_uid, limits, clock=time.monotonic):
    """Serve ONE request on a connected socket and return a fixed word for what happened.

    ``refused``: the peer is not the allowed uid, so not a byte was read and nothing was answered.
    ``timeout``, ``too_large``: the request did not arrive in time or was over the bound (answered with the code
    when the peer still listens). ``incomplete``: the peer closed before the end of its line. ``served``:
    `handler(line)` answered.
    """
    if not peer_allowed(conn, allowed_uid):
        return "refused"
    deadline = clock() + limits.request_timeout
    try:
        line = read_line(conn, limits.max_request_bytes, deadline, clock)
    except RequestTooLarge:
        send_reply(conn, _error("too_large"), limits.reply_timeout, clock)
        return "too_large"
    except EOFError:
        return "incomplete"
    except OSError:  # a TimeoutError or a broken connection; nobody may be left to answer
        send_reply(conn, _error("timeout"), limits.reply_timeout, clock)
        return "timeout"
    send_reply(conn, handler(line), limits.reply_timeout, clock)
    return "served"


# --- The socket directory and the server ----------------------------------------------------------------------------------


class SocketRefused(Exception):
    """The socket directory or path is not what the init container makes it. `code` is a fixed word."""

    def __init__(self, code):
        super().__init__(f"socket directory refused: {code}")
        self.code = code


def check_directory(info, own_uid):
    """Raise SocketRefused unless `info` (an ``os.lstat`` result) is a real directory owned by `own_uid` that
    neither group nor others can write to. A symbolic link shows as a link in an lstat, so it is refused."""
    if not stat.S_ISDIR(info.st_mode):
        raise SocketRefused("not_a_directory")
    if info.st_uid != own_uid:
        raise SocketRefused("owner")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise SocketRefused("writable")


def prepare_socket_path(path, own_uid):
    """Check the directory of `path` and clear a stale socket of our own; SocketRefused for anything else."""
    directory = os.path.dirname(path)
    try:
        check_directory(os.lstat(directory), own_uid)
    except OSError:
        raise SocketRefused("unreadable") from None
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError:
        raise SocketRefused("unreadable") from None
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != own_uid:
        raise SocketRefused("occupied")
    os.unlink(path)


class Server:
    """A bounded unix-socket server for one ``Launcher``: ``start()`` binds and serves, ``close()`` joins everything."""

    def __init__(self, launcher, path, allowed_uid, own_uid=None, limits=None, clock=time.monotonic, log=_log):
        self._launcher = launcher
        self._path = path
        self._allowed = allowed_uid
        self._own = os.geteuid() if own_uid is None else own_uid
        self._limits = limits or Limits()
        self._clock = clock
        self._log = log
        self._listener = None
        self._thread = None
        self._mutex = threading.Lock()
        self._connections = set()  # connections being served: the bound counts these
        self._workers = set()  # every started worker not yet joined: close() joins all of them
        self._stop = threading.Event()

    @property
    def path(self):
        return self._path

    def start(self):
        """Check the directory, bind the socket at mode 0660 and serve in a thread. SocketRefused refuses to start."""
        prepare_socket_path(self._path, self._own)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        previous = os.umask(0o117)
        try:
            listener.bind(self._path)
        except OSError:
            listener.close()
            raise SocketRefused("cannot_bind") from None
        finally:
            os.umask(previous)
        try:
            os.chmod(self._path, SOCKET_MODE)
            info = os.lstat(self._path)
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != self._own:
                raise SocketRefused("occupied")
            listener.listen(self._limits.max_connections)
        except (OSError, SocketRefused):
            listener.close()
            with contextlib.suppress(OSError):
                os.unlink(self._path)
            raise
        self._listener = listener
        self._thread = threading.Thread(target=self._accept_loop, args=(listener,), name="launcher-accept", daemon=True)
        self._thread.start()
        return self

    def _accept_loop(self, listener):
        while not self._stop.is_set():
            try:
                conn, _address = listener.accept()
            except OSError:
                if self._stop.is_set() or listener.fileno() == -1:
                    return  # `close()` closed the listener: the server is over
                # Any other error (a descriptor limit, an aborted connection ...) leaves the listener open: log a
                # fixed line and keep serving, rather than leave a launcher that is up and answers nobody. A
                # persistent error is paced so that it does not spin.
                self._log("accept failed")
                self._stop.wait(ACCEPT_RETRY_SECONDS)
                continue
            self._dispatch(conn)

    def _dispatch(self, conn):
        # A foreign peer is dropped here, before it can take a thread or even be told `busy`.
        if not peer_allowed(conn, self._allowed):
            conn.close()
            return
        worker = threading.Thread(target=self._serve, args=(conn,), name="launcher-conn", daemon=True)
        with self._mutex:
            busy = len(self._connections) >= self._limits.max_connections
            if not busy:
                self._connections.add(conn)
                self._workers = {thread for thread in self._workers if thread.is_alive()}
                self._workers.add(worker)
        if busy:
            send_reply(conn, _error("busy"), self._limits.reply_timeout, self._clock)
            conn.close()
            return
        worker.start()

    def _serve(self, conn):
        try:
            serve_connection(conn, self._launcher.handle_line, self._allowed, self._limits, self._clock)
        finally:
            with self._mutex:
                self._connections.discard(conn)
            conn.close()

    def close(self):
        """Stop accepting, wake and join every thread with a bound, remove the socket file if it is still ours."""
        self._stop.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            with contextlib.suppress(OSError):
                listener.shutdown(socket.SHUT_RDWR)  # wakes a blocked accept
            listener.close()
        if self._thread is not None:
            self._thread.join(JOIN_SECONDS)
        with self._mutex:
            connections, workers = list(self._connections), list(self._workers)
        for conn in connections:
            with contextlib.suppress(OSError):
                conn.shutdown(socket.SHUT_RDWR)  # wakes a blocked read
        for worker in workers:
            worker.join(JOIN_SECONDS)
        with contextlib.suppress(OSError):
            info = os.lstat(self._path)
            if stat.S_ISSOCK(info.st_mode) and info.st_uid == self._own:
                os.unlink(self._path)


# --- Program --------------------------------------------------------------------------------------------------------------


def _sweeper(launcher, stop, log, interval=SWEEP_INTERVAL_SECONDS):
    while not stop.wait(interval):
        try:
            removed = launcher.sweep()
        except kube.KubeError as failure:
            log(f"sweep failed {failure.kind}")
            continue
        except Exception as bug:  # the sweep must keep running; only the type name is logged
            log(f"sweep failed {type(bug).__name__}")
            continue
        if removed:
            log(f"sweep deleted={removed}")


def main(environment=None, euid=None):
    """Run the launcher until it is stopped. Returns 2 when it refuses to start (nothing is served).

    `euid` is the effective uid to judge (the process's own by default); the launcher is non-root by design
    and refuses to run as root.
    """
    env = os.environ if environment is None else environment
    try:
        config = Config.from_environment(env)
        if (os.geteuid() if euid is None else euid) == 0:
            raise ConfigRefused("running_as_root")
        client = kube.Client.in_cluster(env, namespace=config.namespace)
        launcher = Launcher(client, config)
        server = Server(launcher, config.socket_path, config.allowed_uid).start()
    except ConfigRefused as refusal:
        _log(f"refusing to start: {refusal.code}")
        return 2
    except kube.KubeError:
        _log("refusing to start: kubernetes configuration")
        return 2
    except SocketRefused as refusal:
        _log(f"refusing to start: socket {refusal.code}")
        return 2
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda _signum, _frame: stop.set())
    sweeper = threading.Thread(target=_sweeper, args=(launcher, stop, _log), daemon=True)
    sweeper.start()
    _log(f"listening max_pods={config.max_pods} repo_cap={config.repo_cap}")
    try:
        stop.wait()
    except KeyboardInterrupt:
        stop.set()
    finally:
        server.close()
        sweeper.join(JOIN_SECONDS)
    return 0


if __name__ == "__main__":
    sys.exit(main())

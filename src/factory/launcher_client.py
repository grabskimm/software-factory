"""The manager-side client of the pod launcher (slice 11 of the pod backend, issue #145).

The supervisor in the manager's main container uses this client to ask the launcher (``launcher.py``) to start a
worker pod, to read its state and to delete it. Nothing imports this module yet and the backend is off by default.

THE CLIENT AUTHENTICATES THE SERVER BEFORE IT SENDS A BYTE. The launch request carries the one-time relay token, so
a listener planted at the socket path would receive a live token; the socket directory is an ``emptyDir`` shared
with a container that runs other code (the worker uid 10002 runs in the same pod). Before anything is written the
client checks, with ``lstat`` (which never follows a symbolic link), that the socket directory is a real directory
owned by the configured launcher uid that neither group nor others can write to, and that the socket is a socket
owned by that uid; then it connects and reads ``SO_PEERCRED`` of the CONNECTED socket, which must report the same
uid. Any mismatch is ``ServerRefused`` (a fixed text) and NOTHING was sent: no request, no token. A missing
directory or socket (a launcher that is still starting) is retried a bounded number of times with the contract's
backoff, never anything that looks like an impostor.

Bounds: every connect, send and read has a timeout, the reply is read against a TOTAL deadline (a server that
trickles bytes cannot hold the caller) and is at most ``MAX_REPLY_BYTES``. Only the connect is retried: a request
that was sent is never repeated (a launch may have been applied). Every error text is fixed: nothing from a reply, a
request or a token is ever in an exception or a log line.

The seam of slice 5 (``podhandle``): ``LauncherClient.reader(handle)`` answers ``"alive"``, ``"dead"`` or
``"unknown"`` (``dead`` when the launcher says the pod is gone or in a final state that is not a deletion in
progress, ``alive`` for a pod that is terminating (its containers may run for the termination grace), ``unknown``
when the launcher cannot be reached or answers anything unexpected, never a guess), and
``LauncherClient.terminate(handle)`` deletes the pod and RETURNS ONLY ONCE THE POD IS GONE: the supervisor forgets
the pod's record when it returns, so a delete that was merely accepted is not enough. It waits, within a bound (the
termination grace and a margin), polling the launcher; a pod that is still there at the bound raises ``Unavailable``
(a pod already gone is success; any other failure raises). A handle is any object (or mapping) with ``name`` and
``uid``.

CONTRACT-TESTED AGAINST ASSUMED SHAPES: the tests run the real launcher over a real unix socket with the in-memory
fake of the Kubernetes API; no cluster was consulted. What ``SO_PEERCRED`` reports across containers that share an
``emptyDir`` under ``hostUsers: false`` is UNVERIFIED.

Standard library only.
"""

import json
import os
import re
import socket
import stat
import struct
import time
from dataclasses import dataclass

from . import podcontract as pc
from . import podstatus

MAX_REPLY_BYTES = 1024 * 1024
CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 30.0
# A launch may wait on two API calls, each bounded by the Kubernetes client at about two minutes.
LAUNCH_TIMEOUT_SECONDS = 300.0
# The seam's terminate returns once the pod is gone: it waits for the grace period of the deletion and a margin.
TERMINATE_WAIT_SECONDS = pc.TERMINATION_GRACE_SECONDS + 15.0
TERMINATE_POLL_SECONDS = 1.0
CONNECT_RETRIES = 3
CONNECT_BACKOFF_SECONDS = (1, 2, 4)
_RECV_BYTES = 65536
_CREDENTIALS = struct.Struct("3i")  # struct ucred: pid, uid, gid
_UID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# The fixed codes of an answer that is not ok. The launcher repeats the set (tests compare the two).
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
REQUEST_FIELDS = (
    "session_id",
    "repository",
    "run_id",
    "job_id",
    "attempt",
    "phase",
    "resource_class",
    "timeout_seconds",
    "manager_instance",
)


# --- Errors (fixed texts) -------------------------------------------------------------------------------------


class LauncherError(Exception):
    """Base of every error of this client. The text is fixed."""


class Unavailable(LauncherError):
    """The launcher cannot be reached, did not answer in time, or answered `unavailable` or `busy`."""

    def __init__(self):
        super().__init__("launcher unavailable")


class ServerRefused(LauncherError):
    """The thing at the socket path is not the launcher: NOTHING was sent to it."""

    def __init__(self):
        super().__init__("launcher identity not trusted")


class Capacity(LauncherError):
    """The launcher is at one of its caps. The supervisor waits and asks again: this is not a failure of the launch."""

    def __init__(self):
        super().__init__("launcher at capacity")


class Rejected(LauncherError):
    """The launcher refused the request with one of the fixed codes. `rules` holds admission rule ids, if any."""

    def __init__(self, code, rules=()):
        super().__init__(f"launcher refused the request ({code})")
        self.code = code
        self.rules = tuple(rules)


class BadReply(LauncherError):
    """The answer is not the closed shape the launcher sends."""

    def __init__(self):
        super().__init__("unexpected launcher reply")


class _NotReady(Exception):
    """Internal: the socket is not there (yet) or does not accept: the launcher may still be starting."""


@dataclass(frozen=True)
class Launched:
    """A launched pod: its name and uid, the timeout the launcher applied and whether it reduced the request's."""

    name: str
    uid: str
    timeout_seconds: int
    clamped: bool


# --- Checking the server before a byte is sent ------------------------------------------------------------------


def check_directory(info, launcher_uid):
    """Raise ServerRefused unless `info` (an ``os.lstat`` result) is a real directory owned by `launcher_uid` that
    neither group nor others can write to. A symbolic link shows as a link in an lstat, so it is refused."""
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != launcher_uid or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ServerRefused()


def check_socket(info, launcher_uid):
    """Raise ServerRefused unless `info` (an ``os.lstat`` result) is a socket owned by `launcher_uid` (not a link)."""
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != launcher_uid:
        raise ServerRefused()


def check_paths(socket_path, launcher_uid, lstat=os.lstat):
    """The checks of the directory and of the socket, with no connection made.

    Raises `_NotReady` when either is missing (a launcher that is starting) and ServerRefused for anything that is
    there but is not what the launcher's init container and the launcher make.
    """
    directory = os.path.dirname(socket_path)
    try:
        check_directory(lstat(directory), launcher_uid)
        check_socket(lstat(socket_path), launcher_uid)
    except (FileNotFoundError, NotADirectoryError):
        raise _NotReady() from None
    except OSError:
        raise ServerRefused() from None


def peer_uid(sock):
    """The uid of the process that listens at the other end of a connected unix socket (``SO_PEERCRED``)."""
    option = getattr(socket, "SO_PEERCRED", None)
    if option is None:
        raise OSError("SO_PEERCRED is not available")
    return _CREDENTIALS.unpack(sock.getsockopt(socket.SOL_SOCKET, option, _CREDENTIALS.size))[1]


# --- The client -----------------------------------------------------------------------------------------------------------


def _handle_fields(handle):
    """(name, uid) of a handle object or mapping; ValueError (fixed text) when it does not name a worker pod."""
    if isinstance(handle, dict):
        name, uid = handle.get("name"), handle.get("uid")
    else:
        name, uid = getattr(handle, "name", None), getattr(handle, "uid", None)
    pc.secret_name(name)  # ValueError unless it is a worker pod name
    if not isinstance(uid, str) or not _UID.fullmatch(uid):
        raise ValueError("invalid pod handle")
    return name, uid


class LauncherClient:
    """One launcher, reached over a unix socket. Every call opens its own connection (one request per connection).

    `launcher_uid` is the uid the launcher runs as (the contract's by default). `sleep` and `clock` are injectable,
    so a test never really sleeps.
    """

    def __init__(
        self,
        socket_path=pc.LAUNCHER_SOCKET,
        *,
        launcher_uid=pc.LAUNCHER_UID,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        read_timeout=READ_TIMEOUT_SECONDS,
        launch_timeout=LAUNCH_TIMEOUT_SECONDS,
        terminate_wait=TERMINATE_WAIT_SECONDS,
        retries=CONNECT_RETRIES,
        backoff=CONNECT_BACKOFF_SECONDS,
        sleep=time.sleep,
        clock=time.monotonic,
    ):
        if not isinstance(socket_path, str) or not socket_path.startswith("/") or "\0" in socket_path:
            raise ValueError("invalid socket path")
        if isinstance(launcher_uid, bool) or not isinstance(launcher_uid, int) or launcher_uid < 0:
            raise ValueError("invalid launcher uid")
        self._path = socket_path
        self._uid = launcher_uid
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._launch_timeout = launch_timeout
        self._terminate_wait = terminate_wait
        self._retries = retries
        self._backoff = tuple(backoff)
        self._sleep = sleep
        self._clock = clock

    @classmethod
    def from_environment(cls, environ=None, **options):
        """A client for the socket path of the manager's environment (the contract's path when it is not set)."""
        env = os.environ if environ is None else environ
        return cls(env.get(pc.ENV_MANAGER["launcher_socket"], pc.LAUNCHER_SOCKET), **options)

    # -- connecting ---------------------------------------------------------------------------------------------

    def _connect_once(self):
        check_paths(self._path, self._uid)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(self._connect_timeout)
            sock.connect(self._path)
            trusted = peer_uid(sock) == self._uid
        except (FileNotFoundError, ConnectionRefusedError):
            sock.close()
            raise _NotReady() from None
        except (OSError, struct.error):
            sock.close()
            raise Unavailable() from None
        if not trusted:
            sock.close()
            raise ServerRefused()
        return sock

    def _connect(self):
        """A connected socket whose peer was checked, or an error. Only a launcher that is not there yet is retried."""
        for attempt in range(self._retries + 1):
            try:
                return self._connect_once()
            except _NotReady:
                if attempt < self._retries:
                    self._sleep(self._backoff[min(attempt, len(self._backoff) - 1)])
        raise Unavailable()

    # -- one exchange ---------------------------------------------------------------------------------------------

    def _request(self, document, timeout):
        """Send one request line and return the parsed ok reply; raise the error of any other answer."""
        data = f"{json.dumps(document, separators=(',', ':'))}\n".encode("utf-8")
        sock = self._connect()
        try:
            deadline = self._clock() + timeout
            self._send(sock, data, deadline)
            reply = self._receive(sock, deadline)
        except OSError:  # a timeout or a reset (a reply that is not the closed shape is a BadReply)
            raise Unavailable() from None
        finally:
            sock.close()
        return self._interpret(reply)

    def _send(self, sock, data, deadline):
        offset = 0
        while offset < len(data):
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise TimeoutError("send took too long")
            sock.settimeout(remaining)
            offset += sock.send(data[offset : offset + _RECV_BYTES])

    def _receive(self, sock, deadline):
        chunks = []
        size = 0
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise TimeoutError("reply took too long")
            sock.settimeout(remaining)
            data = sock.recv(min(_RECV_BYTES, MAX_REPLY_BYTES + 1 - size))
            if not data:
                raise ConnectionError("closed before the end of the reply")
            end = data.find(b"\n")
            chunks.append(data if end < 0 else data[:end])
            size += len(chunks[-1])
            if size > MAX_REPLY_BYTES:
                raise BadReply()
            if end >= 0:
                return self._parse(b"".join(chunks))

    @staticmethod
    def _parse(line):
        try:
            return json.loads(line.decode("utf-8"))
        except (ValueError, RecursionError):  # includes UnicodeDecodeError
            raise BadReply() from None

    @staticmethod
    def _interpret(reply):
        if not isinstance(reply, dict) or not isinstance(reply.get("ok"), bool):
            raise BadReply()
        if reply["ok"]:
            return reply
        code = reply.get("error")
        if code not in ERROR_CODES:
            raise BadReply()
        if code == "capacity":
            raise Capacity()
        if code in ("unavailable", "busy", "timeout"):
            raise Unavailable()
        rules = reply.get("rules")
        known = [rule for rule in rules if isinstance(rule, str) and re.fullmatch(r"[a-z][a-z-]{0,40}", rule)] if isinstance(rules, list) else []
        raise Rejected(code, known[:16])

    # -- the operations ------------------------------------------------------------------------------------------------

    def launch(self, request, token):
        """Ask the launcher to start a worker pod; returns `Launched`.

        `request` is exactly the closed request of ``podspec`` (nothing else is sent); `token` is the one-time relay
        token. Raises Capacity when a cap is reached (wait and ask again), Unavailable, ServerRefused, Rejected.
        """
        if not isinstance(request, dict) or set(request) != set(REQUEST_FIELDS) or not isinstance(token, str):
            raise ValueError("invalid launch request")
        document = {"op": "launch", "token": token, **{name: request[name] for name in REQUEST_FIELDS}}
        reply = self._request(document, self._launch_timeout)
        name, uid = reply.get("name"), reply.get("uid")
        seconds, clamped = reply.get("timeout_seconds"), reply.get("clamped")
        try:
            pc.secret_name(name)
        except ValueError:
            raise BadReply() from None
        if not (isinstance(uid, str) and _UID.fullmatch(uid) and isinstance(seconds, int) and not isinstance(seconds, bool) and isinstance(clamped, bool)):
            raise BadReply()
        return Launched(name, uid, seconds, clamped)

    def pod_state(self, name, uid=None):
        """The state of a pod: a dict with `state` (one of ``podstatus.STATES``), `detail`, `final`, `exit_code`,
        `present` (the pod is still in the API) and `terminating` (it is being deleted and is not over: it may still
        run, although `final` is True for it).

        When `uid` is given and the pod under that name has another uid, the answer is `gone` and not `present`.
        """
        return self._pod_state(name, uid, self._read_timeout)

    def _pod_state(self, name, uid, timeout):
        pc.secret_name(name)
        document = {"op": "status", "name": name}
        if uid is not None:
            document["uid"] = uid
        reply = self._request(document, timeout)
        if reply.get("state") not in podstatus.STATES or not isinstance(reply.get("final"), bool):
            raise BadReply()
        present, terminating = reply.get("present"), reply.get("terminating")
        if not isinstance(present, bool) or not isinstance(terminating, bool) or (terminating and not present):
            raise BadReply()
        detail, code = reply.get("detail"), reply.get("exit_code")
        if not isinstance(detail, str) or not re.fullmatch(r"[A-Za-z_]{1,64}", detail):
            raise BadReply()
        if code is not None and (isinstance(code, bool) or not isinstance(code, int)):
            raise BadReply()
        return {
            "state": reply["state"],
            "detail": detail,
            "final": reply["final"],
            "exit_code": code,
            "present": present,
            "terminating": terminating,
        }

    def delete_pod(self, name, uid=None):
        """Ask the launcher to delete a pod and its Secret; True when a pod was deleted, False when there was none
        (success either way). It returns when the deletion is ACCEPTED: the pod may be terminating for its grace
        period afterwards (``terminate`` waits for it to be gone).

        With `uid`, a pod under that name with another uid is left alone.
        """
        return self._delete_pod(name, uid, self._read_timeout)

    def _delete_pod(self, name, uid, timeout):
        pc.secret_name(name)
        document = {"op": "terminate", "name": name}
        if uid is not None:
            document["uid"] = uid
        return self._request(document, timeout).get("deleted") is True

    def list_pods(self):
        """The labelled pods: a list of dicts (`name`, `uid`, `state`, `final`, `terminating`, `manager`, `created`, `deadline`)."""
        reply = self._request({"op": "list"}, self._read_timeout)
        pods = reply.get("pods")
        if not isinstance(pods, list) or not all(isinstance(pod, dict) for pod in pods):
            raise BadReply()
        return pods

    def info(self):
        """The launcher's caps, live count and deny state (a dict)."""
        return self._request({"op": "info"}, self._read_timeout)

    def deny(self, reason):
        """Make the launcher refuse every launch until it restarts. `reason` is a fixed lowercase word."""
        if not isinstance(reason, str):
            raise ValueError("invalid reason")
        self._request({"op": "deny", "reason": reason}, self._read_timeout)

    # -- the seam of slice 5 ----------------------------------------------------------------------------------------------

    def reader(self, handle):
        """``"alive"``, ``"dead"`` or ``"unknown"`` for the pod of `handle`: never raises, never guesses.

        A pod that is terminating is ``alive`` (it may run for its grace period), whatever ``final`` says of it.
        """
        try:
            name, uid = _handle_fields(handle)
            state = self.pod_state(name, uid)
            return "dead" if state["final"] and not state["terminating"] else "alive"
        except (LauncherError, ValueError):
            return "unknown"

    def terminate(self, handle):
        """Delete the pod of `handle` and return only once the pod is gone (already gone is success).

        The delete is accepted at once, but the pod then runs for up to its termination grace: this polls the
        launcher until the API no longer has the pod, within one bound for the whole call (``terminate_wait``: the
        grace and a margin), and raises ``Unavailable`` when the pod is still there at the bound. Raises ValueError
        for a handle that does not name a worker pod and LauncherError when the launcher cannot do it.
        """
        name, uid = _handle_fields(handle)
        deadline = self._clock() + self._terminate_wait
        self._delete_pod(name, uid, self._within(deadline))
        while self._pod_state(name, uid, self._within(deadline))["present"]:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise Unavailable()
            self._sleep(min(TERMINATE_POLL_SECONDS, remaining))

    def _within(self, deadline):
        """The timeout of one request that must also end by `deadline` (never more than a read, never below 1 s)."""
        return max(1.0, min(self._read_timeout, deadline - self._clock()))

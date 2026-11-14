"""The relay of the pod backend, as its own program: ``python -m factory.relay``.

Slice 8 of the pod backend (``docs/pod-backend.md``). A network-reachable parser fed by untrusted
pods must not live in the manager container, so the relay is a separate container with no secret: no
Anthropic key, no GitHub or cloud credential, no Kubernetes token (``main`` refuses to start when the
environment or the file system says otherwise), no outbound call. Pods reach it over plain HTTP inside
the cluster; the supervisor reaches it through the spool, a directory both share
(``sessionrecord.py`` defines the layout and the schemas).

What a pod can do, and nothing else (no endpoint lists, reads another session or answers without a
valid token; the route table below is closed)::

    GET  /v1/bundle               the tree, once (after a good preflight)
    POST /v1/preflight            {"result": "ok"} or {"result": "failed", "code": "<fixed code>"}
    POST /v1/stream/transcript    append a chunk (all of it or none of it)
    POST /v1/stream/stderr        append a chunk
    POST /v1/heartbeat            liveness; answers with the token state
    POST /v1/result               <4-byte big-endian report length><report JSON><delta bytes>, once

Every request carries ``Authorization: Bearer <token>``, ``X-Factory-Pod-Name`` and
``X-Factory-Pod-Uid``. The pod name carries the session id (validated before it touches a path). The
token is compared in constant time with the SHA-256 the supervisor wrote in the session record; the pod
uid with the one the supervisor bound after the launch. The token is single use in the sense that
matters: the bundle counts as fetched only when its last byte was written without error (a download that
drops may be retried, at most three attempts), the result is accepted once, and a session in a terminal
state answers 410 to everything.

Properties kept by construction (each has a test that fails when it is removed):

* the one-way state machine ``issued, bound, preflighted, fetched, streaming, finished, expired,
  revoked`` is stored in ``state.json`` and survives a restart; a session the relay has no record of
  (an empty spool after a manager rollout) answers 410, which ends the pod;
* output is never deleted before the supervisor had its chance to collect it: a session directory goes
  away only after the supervisor's ``released`` marker, or after the retention that follows
  ``finished``, ``expired`` or ``revoked``;
* hard caps: bundle, transcript, stderr, delta, chunk and report sizes, a per-request idle timeout and a
  total time per request, a thread bound (503 at once beyond it), and a spool refusal threshold (507)
  that is always below the spool ``sizeLimit`` (a tripped ``sizeLimit`` evicts the manager pod);
* one peer cannot take every thread: a connection gets a short time to deliver a complete header
  section (nothing is read after it before the credentials are checked, so an unauthenticated
  connection never holds a thread longer), and one peer address holds at most a few threads at once;
* the spool root must be exactly what the init container makes it, or the relay refuses to serve;
* uploads are written only under ``<session>/out/`` with a fixed set of file names; nothing from a
  request is ever a path; every file is opened relative to a directory descriptor with ``O_NOFOLLOW``
  and ``O_NONBLOCK`` and must be a regular file (a fifo or a device in its place is refused, never waited on);
* a log line or a response body is a fixed text: never a token, a header, a body, a path or agent text.

Plain HTTP inside the cluster (the token plus the network policy) is by design; TLS is out of scope.
Standard library only. Nothing here was run against a cluster: contract-tested with an in-process client.
"""

import hashlib
import hmac
import http.server
import json
import os
import re
import secrets
import shutil
import socketserver
import stat
import struct
import sys
import threading
import time
from collections import namedtuple
from contextlib import contextmanager, suppress
from dataclasses import dataclass

from . import podcontract as pc
from . import sessionrecord as sr

HEADER_POD_NAME = "X-Factory-Pod-Name"
HEADER_POD_UID = "X-Factory-Pod-Uid"

_MIB = 1024 * 1024
# Relay-local sizes (the contract owns the caps of a session; these bound one request).
STREAM_CHUNK_MAX_BYTES = 8 * _MIB
REPORT_MAX_BYTES = 64 * 1024
SMALL_BODY_MAX_BYTES = 4096
HEADER_MAX_BYTES = 16 * 1024
SWEEP_INTERVAL_SECONDS = 60
_COPY_BYTES = 64 * 1024
_RECV_BYTES = 64 * 1024
# A connection must deliver a complete header section within this time of being accepted; only then does
# the total time of its route apply. A drip of bytes does not extend it.
HEADER_TIMEOUT_SECONDS = 10
# Concurrent connections one peer address may hold (a pod needs a stream, a heartbeat and a transfer).
PEER_MAX_CONNECTIONS = 4
_PREFIX = struct.Struct(">I")
_TRANSFER_PATHS = ("/v1/bundle", "/v1/result")

# The spool root as the init container makes it: owned by the relay uid, group 10001, mode 2770.
SPOOL_MODE = sr.DIR_MODE

# An environment name that looks like a credential makes the relay refuse to start, except the few names
# the image itself sets (the Dockerfile runtime ENV): they hold a command line or a switch, never a
# credential, and a pod template cannot remove an image ENV name. A test reads the Dockerfile and fails
# when the image gains a name the guard would refuse.
_IMAGE_NAMES = frozenset({"FACTORY_TOKEN_BROKER", "GH_NO_UPDATE_NOTIFIER", "GH_PROMPT_DISABLED"})
_SECRET_WORDS = frozenset({"KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "CREDENTIALS", "APIKEY"})
_SECRET_PREFIXES = ("AZURE_", "ANTHROPIC_", "GITHUB_", "GH_")
_KUBERNETES_TOKEN = "/var/run/secrets/kubernetes.io/serviceaccount/token"

_STREAM_FILES = {"transcript": sr.OUT_TRANSCRIPT, "stderr": sr.OUT_STDERR}
_STREAM_CAPS = {"transcript": "transcript_bytes", "stderr": "stderr_bytes"}
_STREAM_REJECTS = {"transcript": "transcript_cap", "stderr": "stderr_cap"}
_ACTIVE_STATES = ("fetched", "streaming")

_JSON = "application/json"
_OCTETS = "application/octet-stream"


class SpoolRefused(Exception):
    """The spool root is not what the init container makes it. The text is a fixed code."""


class Refused(Exception):
    """Unwinds to the reply that answers the request."""

    def __init__(self, reply):
        super().__init__("refused")
        self.reply = reply


class ClientGone(Exception):
    """The client went away or ran out of time while the body was read; nothing is answered."""


class DeadlineExceeded(TimeoutError):
    """A request used more than its total time."""


class HeaderTooLarge(OSError):
    """The header section of a request is larger than the relay accepts."""


@dataclass
class Reply:
    status: int
    body: bytes = b""
    content_type: str = _JSON
    download: object = None


def _document(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


def _error(status, code):
    return Reply(status, _document({"error": code}))


def _ok(state):
    return Reply(200, _document({"state": state}))


def _log(message):
    """One fixed, token-free line on stderr. Callers pass only literal text and numbers."""
    sys.stderr.write(f"factory-relay: {message}\n")


class SystemClock:
    def time(self):
        return time.time()

    def monotonic(self):
        return time.monotonic()


@dataclass(frozen=True)
class Limits:
    """The numbers of one relay. Each default is the contract's, or a relay-local one below; a test may lower them."""

    max_threads: int = pc.RELAY_MAX_THREADS
    max_per_peer: int = PEER_MAX_CONNECTIONS
    header_timeout: float = HEADER_TIMEOUT_SECONDS
    idle_timeout: float = pc.RELAY_IDLE_TIMEOUT_SECONDS
    stream_total: float = pc.RELAY_STREAM_TOTAL_SECONDS
    transfer_total: float = pc.RELAY_TRANSFER_TOTAL_SECONDS
    spool_refuse_bytes: int = pc.SPOOL_REFUSE_BYTES
    retention: float = pc.SPOOL_RETENTION_SECONDS
    chunk_max_bytes: int = STREAM_CHUNK_MAX_BYTES

    def total_for(self, path):
        """The total time one request may take: longer for a bundle or result transfer."""
        return self.transfer_total if path in _TRANSFER_PATHS else self.stream_total


# --- Spool root and environment checks ------------------------------------------------------------


def check_root(info, uid, gid):
    """Raise SpoolRefused unless `info` (an os.stat_result) is a directory owned by uid:gid with mode 2770."""
    if not stat.S_ISDIR(info.st_mode):
        raise SpoolRefused("not_a_directory")
    if info.st_uid != uid:
        raise SpoolRefused("owner")
    if info.st_gid != gid:
        raise SpoolRefused("group")
    if stat.S_IMODE(info.st_mode) != SPOOL_MODE:
        raise SpoolRefused("mode")


def open_spool_root(path, uid, gid):
    """A descriptor for the spool root, which must be exactly the directory the init container makes."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        raise SpoolRefused("unreadable") from None
    try:
        check_root(os.fstat(fd), uid, gid)
    except SpoolRefused:
        os.close(fd)
        raise
    return fd


def looks_secret(name):
    """Whether an environment variable name looks like a credential (KEY, TOKEN, SECRET, ... or AZURE_...)."""
    if name in _IMAGE_NAMES:
        return False
    upper = name.upper()
    return upper.startswith(_SECRET_PREFIXES) or any(word in _SECRET_WORDS for word in upper.split("_"))


def check_environment(environment, token_path=_KUBERNETES_TOKEN):
    """A fixed refusal code when the relay would hold a secret, else None.

    The relay has no secret by design. If an environment variable that looks like a credential, an
    injected cloud identity or a mounted Kubernetes token shows up anyway (a mistake in the pod
    template), refusing to serve is safer than serving with it. Only the names the image sets itself
    are exempt, by exact name.
    """
    if any(looks_secret(name) for name in environment):
        return "secret_in_environment"
    if os.path.exists(token_path):
        return "kubernetes_token_present"
    return None


# --- Transport: deadlines, a bounded reader and writer, one request per connection ---------------


class Deadline:
    """The idle timeout and the total time of one request, on an injectable monotonic clock."""

    def __init__(self, clock, idle, total):
        self._clock = clock
        self._idle = idle
        self._total = total
        self._start = clock.monotonic()

    def limit_to(self, total):
        self._total = total

    def timeout(self):
        """Seconds the next blocking call may take; DeadlineExceeded when the total time is used up."""
        remaining = self._total - (self._clock.monotonic() - self._start)
        if remaining <= 0:
            raise DeadlineExceeded("request took too long")
        return min(self._idle, remaining)


class _Reader:
    """The read side of a connection: bounded header section, deadline checked before and after every read."""

    closed = False

    def __init__(self, sock, deadline):
        self._sock = sock
        self._deadline = deadline
        self._buffer = bytearray()
        self._header_left = HEADER_MAX_BYTES

    def begin_body(self):
        self._header_left = None

    def _fill(self):
        self._sock.settimeout(self._deadline.timeout())
        data = self._sock.recv(_RECV_BYTES)
        self._deadline.timeout()
        if not data:
            return False
        self._buffer.extend(data)
        return True

    def _take(self, count):
        data = bytes(self._buffer[:count])
        del self._buffer[:count]
        if self._header_left is not None:
            self._header_left -= len(data)
            if self._header_left < 0:
                raise HeaderTooLarge("header section too large")
        return data

    def readline(self, limit):
        while True:
            end = self._buffer.find(b"\n", 0, limit)
            if end >= 0:
                return self._take(end + 1)
            if len(self._buffer) >= limit or not self._fill():
                return self._take(min(len(self._buffer), limit))

    def read(self, size):
        if size <= 0:
            return b""
        if not self._buffer and not self._fill():
            return b""
        return self._take(min(size, len(self._buffer)))

    def close(self):
        return None


class _Writer:
    closed = False

    def __init__(self, sock, deadline):
        self._sock = sock
        self._deadline = deadline

    def write(self, data):
        view = memoryview(data)
        while view:
            self._sock.settimeout(self._deadline.timeout())
            view = view[self._sock.send(view) :]
        return len(data)

    def flush(self):
        return None

    def close(self):
        self.closed = True


class Body:
    """The request body: exactly `length` bytes. A short or dead connection raises ClientGone."""

    def __init__(self, reader, length):
        self._reader = reader
        self._left = length

    def read(self, size):
        want = min(size, self._left)
        if want <= 0:
            return b""
        try:
            data = self._reader.read(want)
        except OSError:
            raise ClientGone("client went away") from None
        if not data:
            raise ClientGone("client went away")
        self._left -= len(data)
        return data


def _content_length(headers):
    value = headers.get("Content-Length")
    if value is None:
        return None
    if re.fullmatch(r"[0-9]{1,12}", value) is None:
        raise ValueError("invalid content length")
    return int(value)


class _Handler(http.server.BaseHTTPRequestHandler):
    """One request per connection; every answer is a fixed document; nothing is logged per request."""

    protocol_version = "HTTP/1.0"
    server_version = "factory-relay"
    sys_version = ""

    def __init__(self, request, client_address, server, deadline):
        self._deadline = deadline  # started when the connection was accepted, not when its thread got going
        super().__init__(request, client_address, server)

    def version_string(self):
        return self.server_version

    def setup(self):
        self.connection = self.request
        self.rfile = _Reader(self.connection, self._deadline)
        self.wfile = _Writer(self.connection, self._deadline)

    def log_message(self, *args):
        return None

    def send_error(self, code, message=None, explain=None):
        # The standard implementation echoes the request line into an HTML body and a log line.
        self.close_connection = True
        self._send(_error(code if isinstance(code, int) else 400, "bad_request"))

    def _send(self, reply):
        if self.request_version == "HTTP/0.9":  # a bare request line still gets a status line and headers
            self.request_version = "HTTP/1.0"
        download = reply.download
        size = download.size if download is not None else len(reply.body)
        try:
            self.send_response_only(reply.status)
            self.send_header("Content-Type", _OCTETS if download is not None else reply.content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Connection", "close")
            self.end_headers()
            if download is not None:
                self.server.relay.pump(download, self.wfile.write)
            else:
                self.wfile.write(reply.body)
        finally:
            if download is not None:
                download.close()  # also when the client dropped before the first byte: no leaked descriptor or claim

    def _serve(self):
        relay = self.server.relay
        # The header section is complete: the route's total time applies now. Nothing was read past the
        # headers and nothing blocks before the credentials are checked (Relay.handle reads a body only
        # for an authenticated caller), so a connection that cannot authenticate never needs more than
        # the header time.
        self._deadline.limit_to(relay.limits.total_for(self.path))
        self.rfile.begin_body()
        try:
            length = _content_length(self.headers)
        except ValueError:
            self._send(_error(400, "bad_request"))
            return
        body = Body(self.rfile, length or 0)
        try:
            reply = relay.handle(self.command, self.path, self.headers, length, body)
        except ClientGone:
            self.close_connection = True
            return
        self._send(reply)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _serve


class RelayServer(http.server.HTTPServer):
    """An HTTP server with a hard bound on concurrent requests: a request beyond it answers 503 at once."""

    request_queue_size = 128

    def __init__(self, address, relay):
        self.relay = relay
        self._active = 0
        self._peers = {}
        self._changed = threading.Condition()
        super().__init__(address, _Handler)

    def server_bind(self):
        # http.server.HTTPServer.server_bind resolves the host name; the relay makes no lookups.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port

    @property
    def active(self):
        with self._changed:
            return self._active

    def wait_idle(self, count=0, timeout=10.0):
        """Wait until at most `count` requests are being served (tests and orderly shutdown)."""
        with self._changed:
            return self._changed.wait_for(lambda: self._active <= count, timeout)

    def wait_active(self, count, timeout=10.0):
        """Wait until at least `count` requests are being served (tests)."""
        with self._changed:
            return self._changed.wait_for(lambda: self._active >= count, timeout)

    def _release(self, peer):
        with self._changed:
            self._active -= 1
            left = self._peers.get(peer, 0) - 1
            if left > 0:
                self._peers[peer] = left
            else:
                self._peers.pop(peer, None)
            self._changed.notify_all()

    def process_request(self, request, client_address):
        limits = self.relay.limits
        # Until the header section is complete only the short header time applies (see _Handler._serve).
        deadline = Deadline(self.relay.clock, limits.idle_timeout, limits.header_timeout)
        peer = client_address[0] if client_address else ""
        with self._changed:
            full = self._active >= limits.max_threads or self._peers.get(peer, 0) >= limits.max_per_peer
            if not full:
                self._active += 1
                self._peers[peer] = self._peers.get(peer, 0) + 1
                self._changed.notify_all()
        if full:
            self._refuse(request)
            return
        try:
            threading.Thread(target=self._serve, args=(request, client_address, deadline, peer), daemon=True).start()
        except RuntimeError:
            self._release(peer)
            self._refuse(request)

    def _serve(self, request, client_address, deadline, peer):
        try:
            _Handler(request, client_address, self, deadline)
        except Exception as error:  # a bug must not leak a traceback; the connection just ends
            if not isinstance(error, OSError):
                _log(f"request failed {type(error).__name__}")
        finally:
            self.shutdown_request(request)
            self._release(peer)

    def _refuse(self, request):
        reply = _error(503, "busy")
        head = f"HTTP/1.0 503 Service Unavailable\r\nContent-Type: {_JSON}\r\nContent-Length: {len(reply.body)}\r\nConnection: close\r\n\r\n"
        with suppress(OSError):
            request.settimeout(1.0)
            request.sendall(b"".join((head.encode("ascii"), reply.body)))
        self.shutdown_request(request)

    def handle_error(self, request, client_address):
        return None


# --- The relay ------------------------------------------------------------------------------------

Auth = namedtuple("Auth", "fd record uid")


def _header(headers, name):
    value = headers.get(name)
    if value is not None:
        return value
    lowered = name.lower()
    for key, candidate in headers.items():
        if key.lower() == lowered:
            return candidate
    return None


def _session_of(pod_name):
    """The session id a pod name carries, None when the name is not the contract's pod name."""
    prefix = pc.POD_NAME_PREFIX
    if not pod_name.startswith(prefix):
        return None
    try:
        return pc.validate_session_id(pod_name[len(prefix) :])
    except ValueError:
        return None


def _size_at(fd, name):
    try:
        return os.stat(name, dir_fd=fd, follow_symlinks=False).st_size
    except FileNotFoundError:
        return 0


def _copy(body, size, handle):
    left = size
    while left > 0:
        piece = body.read(min(_COPY_BYTES, left))
        sr.write_all(handle, piece)
        left -= len(piece)


def _read_exact(body, size):
    parts = []
    left = size
    while left > 0:
        piece = body.read(left)
        if not piece:
            raise ClientGone("client went away")
        parts.append(piece)
        left -= len(piece)
    return b"".join(parts)


def _stage(out_fd, final, body, size):
    """Receive `size` bytes into a temporary file next to `final`; the temporary name, or the error."""
    temp = f".{final}.{secrets.token_hex(4)}.part"
    handle = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, sr.FILE_MODE, dir_fd=out_fd)
    staged = False
    try:
        os.fchmod(handle, sr.FILE_MODE)
        _copy(body, size, handle)
        staged = True
    finally:
        os.close(handle)
        if not staged:
            with suppress(OSError):
                os.unlink(temp, dir_fd=out_fd)
    return temp


def _open_regular(name, flags, dir_fd):
    """(descriptor, fstat) of the regular file `name` of the directory `dir_fd`; OSError for anything else.

    Every spool file the relay reads or appends to is opened here. ``O_NONBLOCK`` keeps the open from
    waiting for the other end of a fifo (the relay-wide lock is held around some of these opens, and a
    request thread would never come back), ``O_NOFOLLOW`` refuses a link, and the ``fstat`` of the open
    descriptor refuses a fifo, a device or a socket before a byte is read or written. A refusal closes
    the descriptor.
    """
    handle = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, sr.FILE_MODE, dir_fd=dir_fd)
    try:
        info = os.fstat(handle)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("not a regular file")
    except OSError:
        os.close(handle)
        raise
    return handle, info


def _append(out_fd, name, body, size):
    """Append `size` bytes from the body to `name`, all or nothing: a failure cuts the file back."""
    handle, info = _open_regular(name, os.O_WRONLY | os.O_APPEND | os.O_CREAT, out_fd)
    start = info.st_size
    try:
        os.fchmod(handle, sr.FILE_MODE)
        _copy(body, size, handle)
    except (ClientGone, OSError):
        with suppress(OSError):
            os.ftruncate(handle, start)
        raise
    finally:
        os.close(handle)


class Download:
    """A bundle being served. The relay marks the fetch complete only when `pump` wrote the last byte."""

    def __init__(self, relay, session, handle, size):
        self.size = size
        self._relay = relay
        self.session = session
        self._handle = handle
        self._open = True

    def read(self, count):
        return os.read(self._handle, count)

    def close(self):
        if self._open:
            self._open = False
            os.close(self._handle)
            self._relay.unclaim((self.session, "bundle"), 0)


class Relay:
    """The relay's logic, independent of sockets: `handle` answers one request, `sweep` keeps the spool tidy."""

    def __init__(self, root, clock=None, limits=None, owner=None):
        self.clock = SystemClock() if clock is None else clock
        self.limits = Limits() if limits is None else limits
        self._owner = (pc.RELAY_UID, pc.SHARED_GID) if owner is None else tuple(owner)
        self._root_path = os.fspath(root)
        self._root_fd = open_spool_root(self._root_path, *self._owner)
        self._lock = threading.Lock()
        self._busy = set()
        self._reserved = 0
        self._routes = {
            ("GET", "/v1/bundle"): self._bundle,
            ("POST", "/v1/preflight"): self._preflight,
            ("POST", "/v1/heartbeat"): self._heartbeat,
            ("POST", "/v1/result"): self._result,
            ("POST", "/v1/stream/transcript"): self._stream_of("transcript"),
            ("POST", "/v1/stream/stderr"): self._stream_of("stderr"),
        }
        self._paths = {path for _, path in self._routes}

    def close(self):
        os.close(self._root_fd)

    # -- entry point ---------------------------------------------------------------------------

    def handle(self, method, path, headers, length, body):
        """Answer one request. `body` is read only after the caller is authenticated and the call is allowed."""
        route = self._routes.get((method, path))
        if route is None:
            return _error(405, "method") if path in self._paths else _error(404, "not_found")
        try:
            self._check_root()
            return route(headers, length, body)
        except Refused as refusal:
            return refusal.reply
        except ClientGone:
            raise
        except Exception as error:  # fixed answer; the text of an unexpected error is never sent or logged
            _log(f"internal error {type(error).__name__}")
            return _error(500, "internal")

    def _check_root(self):
        try:
            check_root(os.fstat(self._root_fd), *self._owner)
        except (SpoolRefused, OSError):
            raise Refused(_error(503, "unavailable")) from None

    # -- authentication and the state gate ----------------------------------------------------------

    def _credentials(self, headers):
        authorization = _header(headers, "Authorization") or ""
        scheme, _, token = authorization.partition(" ")
        session = _session_of(_header(headers, HEADER_POD_NAME) or "")
        uid = _header(headers, HEADER_POD_UID) or ""
        valid = (
            scheme.lower() == "bearer"
            and sr.TOKEN_PATTERN.fullmatch(token) is not None
            and session is not None
            and sr.POD_UID_PATTERN.fullmatch(uid) is not None
        )
        if not valid:
            raise Refused(_error(401, "unauthorized"))
        return session, token, uid

    def _open_session(self, session):
        try:
            return sr.open_session_at(self._root_fd, session)
        except OSError:  # no directory, or a link or file in its place: the same as no record
            raise Refused(_error(410, "gone")) from None

    @contextmanager
    def _authenticated(self, headers):
        """Yield Auth(fd, record, uid) for a request whose token matches the record; Refused otherwise."""
        session, token, uid = self._credentials(headers)
        fd = self._open_session(session)
        try:
            try:
                record = sr.read_at(fd)
            except sr.RecordError:
                raise Refused(_error(410, "gone")) from None
            digest = hashlib.sha256(token.encode("ascii")).hexdigest().encode("ascii")
            if not hmac.compare_digest(digest, record["token_sha256"].encode("ascii")):
                raise Refused(_error(401, "unauthorized"))
            yield Auth(fd, record, uid)
        finally:
            os.close(fd)

    def _end(self, fd, state, name, at):
        state["state"] = name
        state["ended_at"] = at
        sr.write_state_at(fd, state)

    def _reject(self, fd, state, code):
        state["reject"] = code
        self._end(fd, state, "finished", int(self.clock.time()))

    def _effective(self, fd, record, state):
        """The state a call sees now; a newly seen terminal state is stored. The caller holds the lock."""
        if sr.marked_at(fd, sr.MARKER_RELEASED):
            return sr.RELEASED
        current = state["state"]
        if current in sr.TERMINAL_STATES:
            return current
        revoked_at = sr.marker_time_at(fd, sr.MARKER_REVOKE)
        if revoked_at is not None:
            self._end(fd, state, "revoked", revoked_at)
            return "revoked"
        if int(self.clock.time()) >= record["deadline"]:
            self._end(fd, state, "expired", record["deadline"])
            return "expired"
        if current == "issued" and record["pod_uid"] is not None:
            return "bound"
        return current

    def _gate(self, auth):
        """(state document, effective state) for a live, bound session of this pod; Refused otherwise.

        Order: a finished, expired, revoked or released session answers 410; a record without a pod yet
        answers 425; a token presented by another pod uid answers 403. The caller holds the lock.
        """
        try:
            state = sr.read_state_at(auth.fd)
        except sr.RecordError:  # a state file that is not valid fails closed
            raise Refused(_error(410, "gone")) from None
        effective = self._effective(auth.fd, auth.record, state)
        if effective == sr.RELEASED or effective in sr.TERMINAL_STATES:
            raise Refused(_error(410, "gone"))
        if auth.record["pod_uid"] is None:
            raise Refused(_error(425, "too_early"))
        presented = auth.uid.encode("ascii")
        if not hmac.compare_digest(presented, auth.record["pod_uid"].encode("ascii")):
            raise Refused(_error(403, "identity"))
        return state, effective

    @staticmethod
    def _require(effective, allowed):
        if effective not in allowed:
            raise Refused(_error(409, "order"))

    # -- spool accounting ---------------------------------------------------------------------------

    def _spool_usage(self):
        """Bytes held by regular files under the spool, three levels deep, links not followed."""
        total = 0
        stack = [(self._root_path, 0)]
        while stack:
            path, depth = stack.pop()
            try:
                with os.scandir(path) as entries:
                    listing = list(entries)
            except OSError:
                continue
            for entry in listing:
                total += self._entry_bytes(entry, depth, stack)
        return total

    @staticmethod
    def _entry_bytes(entry, depth, stack):
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            return 0
        if stat.S_ISDIR(info.st_mode):
            if depth < 2:
                stack.append((entry.path, depth + 1))
            return 0
        return info.st_size if stat.S_ISREG(info.st_mode) else 0

    def _claim(self, key, reserve):
        """Mark one transfer of a session as running, and reserve its bytes. The caller holds the lock."""
        if key in self._busy:
            raise Refused(_error(409, "busy"))
        if reserve and self._spool_usage() + self._reserved + reserve > self.limits.spool_refuse_bytes:
            raise Refused(_error(507, "spool_full"))
        self._busy.add(key)
        self._reserved += reserve

    def unclaim(self, key, reserve):
        with self._lock:
            self._busy.discard(key)
            self._reserved -= reserve

    def _touch(self, fd):
        now = int(self.clock.time())
        if sr.read_heartbeat_at(fd) != now:
            sr.write_heartbeat_at(fd, now)

    @contextmanager
    def _out_dir(self, fd):
        try:
            out_fd = sr.ensure_dir_at(fd, sr.OUT_DIR)
        except OSError:
            raise Refused(_error(500, "internal")) from None
        try:
            yield out_fd
        finally:
            os.close(out_fd)

    # -- small bodies ---------------------------------------------------------------------------------

    @staticmethod
    def _small_body(length, body):
        if length is None:
            raise Refused(_error(411, "length_required"))
        if length > SMALL_BODY_MAX_BYTES:
            raise Refused(_error(413, "body_too_large"))
        return _read_exact(body, length)

    # -- endpoints ----------------------------------------------------------------------------------

    def _preflight(self, headers, length, body):
        with self._authenticated(headers) as auth:
            with self._lock:
                self._gate(auth)  # a session that is over or not bound yet is refused before its body is read
            raw = self._small_body(length, body)
            try:
                document = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, RecursionError):
                raise Refused(_error(400, "bad_request")) from None
            outcome, code = self._preflight_fields(document)
            with self._lock:
                state, effective = self._gate(auth)
                if effective == "preflighted" and outcome == "ok":
                    return _ok("preflighted")
                self._require(effective, ("bound",))
                if outcome == "ok":
                    state["state"] = "preflighted"
                    sr.write_state_at(auth.fd, state)
                    return _ok("preflighted")
                state["preflight_code"] = code
                self._reject(auth.fd, state, "preflight_failed")
                return _ok("finished")

    @staticmethod
    def _preflight_fields(document):
        """(outcome, code) of a preflight body: {"result": "ok"} or {"result": "failed", "code": "<fixed code>"}."""
        if isinstance(document, dict) and set(document) <= {"result", "code"}:
            outcome = document.get("result")
            code = document.get("code")
            if outcome == "ok" and "code" not in document:
                return "ok", None
            if outcome == "failed" and isinstance(code, str) and sr.CODE_PATTERN.fullmatch(code):
                return "failed", code
        raise Refused(_error(400, "bad_request"))

    def _heartbeat(self, headers, length, body):
        with self._authenticated(headers) as auth:
            with self._lock:
                _, effective = self._gate(auth)
                self._touch(auth.fd)
            if length:
                self._small_body(length, body)
            return _ok(effective)

    def _bundle(self, headers, length, body):
        with self._authenticated(headers) as auth:
            with self._lock:
                state, effective = self._gate(auth)
                if effective in _ACTIVE_STATES:
                    raise Refused(_error(410, "gone"))
                self._require(effective, ("preflighted",))
                if state["fetch_attempts"] >= sr.MAX_FETCH_ATTEMPTS:
                    self._reject(auth.fd, state, "bundle_attempts")
                    raise Refused(_error(410, "gone"))
                download = self._start_download(auth, state)
            return Reply(200, download=download)

    def _start_download(self, auth, state):
        """Count the attempt (stored before the first byte) and open the bundle. The caller holds the lock."""
        key = (auth.record["session"], "bundle")
        self._claim(key, 0)
        handle = None
        download = None
        try:
            handle, size = self._open_bundle(auth.fd)
            if size > auth.record["caps"]["bundle_bytes"]:
                self._reject(auth.fd, state, "bundle_cap")
                raise Refused(_error(413, "bundle_cap"))
            state["fetch_attempts"] += 1
            sr.write_state_at(auth.fd, state)
            download = Download(self, auth.record["session"], handle, size)
        finally:
            if download is None:
                self._busy.discard(key)
                if handle is not None:
                    os.close(handle)
        return download

    @staticmethod
    def _open_bundle(fd):
        try:
            in_fd = os.open(sr.IN_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
        except OSError:
            raise Refused(_error(500, "internal")) from None
        try:
            handle, info = _open_regular(sr.BUNDLE_FILE, os.O_RDONLY, in_fd)
        except OSError:  # missing, a link, or not a regular file (a fifo is refused, never waited on)
            raise Refused(_error(500, "internal")) from None
        finally:
            os.close(in_fd)
        return handle, info.st_size

    def pump(self, download, write):
        """Send a bundle through `write`. The fetch counts as used only if the last byte was written.

        A failing `write` (the client dropped) propagates and leaves the attempt counted and the state
        unchanged, so the pod may retry within its attempts; a second complete fetch answers 410.

        The state becomes `fetched` just before the last slice is written and goes back to `preflighted`
        if that write fails. Recording it after the write would let the pod's next call, which can arrive
        the moment it has the last byte, find the old state and be refused.
        """
        sent = 0
        marked = False
        try:
            while sent < download.size:
                piece = download.read(min(_COPY_BYTES, download.size - sent))
                if not piece:
                    raise OSError("bundle shorter than announced")
                if sent + len(piece) == download.size:
                    marked = self._set_fetched(download.session, "preflighted", "fetched")
                write(piece)
                sent += len(piece)
        finally:
            if marked and sent < download.size:
                self._set_fetched(download.session, "fetched", "preflighted")
            download.close()

    def _set_fetched(self, session, before, after):
        """Move the stored state from `before` to `after`; whether it moved (False when the session is gone)."""
        try:
            fd = self._open_session(session)
        except Refused:
            return False
        try:
            with self._lock:
                record = sr.read_at(fd)
                state = sr.read_state_at(fd)
                if state["state"] != before or self._effective(fd, record, state) != before:
                    return False
                state["state"] = after
                sr.write_state_at(fd, state)
                return True
        finally:
            os.close(fd)

    def _stream_of(self, name):
        def stream(headers, length, body):
            return self._stream(name, headers, length, body)

        return stream

    def _stream(self, name, headers, length, body):
        with self._authenticated(headers) as auth:
            key = (auth.record["session"], name)
            with self._lock:
                state, effective = self._gate(auth)
                self._require(effective, _ACTIVE_STATES)
                if length is None:
                    raise Refused(_error(411, "length_required"))
                if length > self.limits.chunk_max_bytes:
                    raise Refused(_error(413, "chunk_too_large"))
                with self._out_dir(auth.fd) as out_fd:
                    if _size_at(out_fd, _STREAM_FILES[name]) + length > auth.record["caps"][_STREAM_CAPS[name]]:
                        self._reject(auth.fd, state, _STREAM_REJECTS[name])
                        raise Refused(_error(413, _STREAM_REJECTS[name]))
                self._claim(key, length)
            try:
                self._store_chunk(auth, name, body, length)
            finally:
                self.unclaim(key, length)
        return _ok("streaming")

    def _store_chunk(self, auth, name, body, length):
        try:
            with self._out_dir(auth.fd) as out_fd:
                _append(out_fd, _STREAM_FILES[name], body, length)
        except OSError:
            raise Refused(_error(500, "internal")) from None
        with self._lock:
            state, effective = self._gate(auth)
            if effective == "fetched":
                state["state"] = "streaming"
                sr.write_state_at(auth.fd, state)
            self._touch(auth.fd)

    def _result(self, headers, length, body):
        with self._authenticated(headers) as auth:
            caps = auth.record["caps"]
            key = (auth.record["session"], "result")
            with self._lock:
                state, effective = self._gate(auth)
                self._require(effective, _ACTIVE_STATES)
                if length is None:
                    raise Refused(_error(411, "length_required"))
                if length < _PREFIX.size:
                    raise Refused(_error(400, "bad_request"))
                if length > _PREFIX.size + REPORT_MAX_BYTES + caps["delta_bytes"]:
                    self._reject(auth.fd, state, "delta_cap")
                    raise Refused(_error(413, "delta_cap"))
                self._claim(key, length)
            try:
                self._receive_result(auth, length, body)
            finally:
                self.unclaim(key, length)
        return _ok("finished")

    def _result_sizes(self, auth, length, body):
        """(report bytes, delta size) from the length prefix; an over-cap part ends the session."""
        (report_size,) = _PREFIX.unpack(_read_exact(body, _PREFIX.size))
        delta_size = length - _PREFIX.size - report_size
        code = None
        if report_size > REPORT_MAX_BYTES:
            code = "report_cap"
        elif delta_size > auth.record["caps"]["delta_bytes"]:
            code = "delta_cap"
        if code is not None:
            with self._lock:
                state, _ = self._gate(auth)
                self._reject(auth.fd, state, code)
            raise Refused(_error(413, code))
        if delta_size < 0:
            raise Refused(_error(400, "bad_request"))
        return report_size, delta_size

    def _receive_result(self, auth, length, body):
        report_size, delta_size = self._result_sizes(auth, length, body)
        report = _read_exact(body, report_size)
        try:
            valid = isinstance(json.loads(report.decode("utf-8")), dict)
        except (UnicodeDecodeError, ValueError, RecursionError):
            valid = False
        if not valid:
            raise Refused(_error(400, "bad_request"))
        try:
            with self._out_dir(auth.fd) as out_fd:
                temp = _stage(out_fd, sr.OUT_DELTA, body, delta_size)
                with self._lock:
                    self._commit_result(auth, out_fd, temp, report)
        except OSError:
            raise Refused(_error(500, "internal")) from None

    def _commit_result(self, auth, out_fd, temp, report):
        """Make the staged delta and the report the session's result, then finish it. Holds the lock.

        The state is re-checked here: a session revoked or expired while the upload ran gets no result.
        The state is written last, so a crash in between leaves a session that may post again.
        """
        try:
            state, effective = self._gate(auth)
            self._require(effective, _ACTIVE_STATES)
            os.replace(temp, sr.OUT_DELTA, src_dir_fd=out_fd, dst_dir_fd=out_fd)
            sr.atomic_write_at(out_fd, sr.OUT_REPORT, report)
        except (Refused, OSError):
            with suppress(OSError):
                os.unlink(temp, dir_fd=out_fd)
            raise
        self._end(auth.fd, state, "finished", int(self.clock.time()))
        self._touch(auth.fd)

    # -- startup and the sweep ----------------------------------------------------------------------

    def _session_names(self):
        return [name for name in os.listdir(self._root_fd) if _session_of(f"{pc.POD_NAME_PREFIX}{name}") is not None]

    def startup(self):
        """Remove half-written temporary files left by a relay that was stopped mid-write."""
        for name in self._session_names():
            with suppress(OSError):
                self._clean_session(name)

    def _clean_session(self, name):
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self._root_fd)
        try:
            self._unlink_temporaries(fd, ".tmp")
            out_fd = os.open(sr.OUT_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            try:
                self._unlink_temporaries(out_fd, ".part")
            finally:
                os.close(out_fd)
        finally:
            os.close(fd)

    @staticmethod
    def _unlink_temporaries(fd, suffix):
        for entry in os.listdir(fd):
            if entry.startswith(".") and entry.endswith(suffix):
                with suppress(OSError):
                    os.unlink(entry, dir_fd=fd)

    def sweep(self):
        """Mark sessions that expired or were revoked, and remove the directories whose time has come.

        A directory goes away after the supervisor's `released` marker, or once the retention has passed
        since the session ended (finished, expired or revoked). Output is never deleted before that.
        Returns how many directories were removed.
        """
        removed = 0
        for name in self._session_names():
            with self._lock:
                removed += self._sweep_one(name)
        return removed

    def _sweep_one(self, name):
        try:
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self._root_fd)
        except OSError:
            return 0
        try:
            due = self._removable(fd)
        except OSError:
            due = False
        finally:
            os.close(fd)
        if not due:
            return 0
        try:
            shutil.rmtree(name, dir_fd=self._root_fd)
        except OSError:
            _log("sweep could not remove a session")
            return 0
        return 1

    def _removable(self, fd):
        now = self.clock.time()
        try:
            record = sr.read_at(fd)
            state = sr.read_state_at(fd)
        except sr.RecordError:
            # No usable record: leftovers of a session that never started; wait the retention out.
            return now - os.fstat(fd).st_mtime >= self.limits.retention
        effective = self._effective(fd, record, state)
        if effective == sr.RELEASED:
            return True
        ended = state["ended_at"] if state["ended_at"] is not None else now
        return effective in sr.TERMINAL_STATES and now >= ended + self.limits.retention


# --- Program ------------------------------------------------------------------------------------


def _port(environment):
    text = environment.get(pc.ENV_RELAY["port"], str(pc.RELAY_PORT))
    if not (text.isascii() and text.isdigit() and 1 <= int(text) <= 65535):
        return None
    return int(text)


def _sweeper(relay, stop):
    while not stop.wait(SWEEP_INTERVAL_SECONDS):
        try:
            removed = relay.sweep()
        except Exception as error:  # the sweep must keep running; the text of the error is not logged
            _log(f"sweep failed {type(error).__name__}")
            continue
        if removed:
            _log(f"sweep removed={removed}")


def main(environment=None, token_path=_KUBERNETES_TOKEN):
    """Run the relay until it is stopped. Returns 2 when it refuses to start (nothing is served)."""
    env = os.environ if environment is None else environment
    problem = check_environment(env, token_path)
    port = _port(env)
    if problem is None and port is None:
        problem = "invalid_port"
    if problem is None and os.geteuid() == 0:
        problem = "running_as_root"
    if problem is not None:
        _log(f"refusing to start: {problem}")
        return 2
    try:
        relay = Relay(env.get(pc.ENV_RELAY["spool_dir"], pc.SPOOL_DIR))
    except SpoolRefused as refusal:
        _log(f"refusing to start: spool root {refusal}")
        return 2
    os.umask(stat.S_IRWXO)  # nothing the relay creates is open to others, whatever the image's umask
    relay.startup()
    try:
        server = RelayServer(("", port), relay)
    except OSError:
        _log("refusing to start: cannot listen")
        return 2
    stop = threading.Event()
    threading.Thread(target=_sweeper, args=(relay, stop), daemon=True).start()
    _log(f"listening port={port}")
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()
        relay.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

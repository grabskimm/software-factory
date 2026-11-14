"""The pod worker wrapper: ``python -m factory.podworker``.

Slices 10a and 10b of the pod backend (``docs/pod-backend.md``). This is the program a worker pod runs instead
of the image entrypoint (the command of ``podspec.WORKER_COMMAND``): a small wrapper that runs as the
container's root, reads the one-time relay token from a root-only file of the Secret volume, proves
that the pod cannot reach what it must not reach, fetches its tree from the relay and lays it out under
the data directory (part A, slice 10a). Part B (slice 10b, the section "The run" below) runs ``claude``
as the worker identity, streams its transcript and its standard error to the relay, heartbeats, posts the
file delta, the footprint and the outcome once, and handles SIGTERM by stopping the session and draining.

Output discipline (the point of this file). Log Analytics collects the standard output and the standard
error of every container, so the wrapper's own streams carry the lines of ``_LINES`` and one failure
line, ``podworker: failed <stage>``, and nothing else: no traceback, no path, no environment variable,
no response text, no token and no model text. A single handler (``main``) turns EVERY exception (a
parse error in the bundle, the relay's answer, the file system, an import, a ``SystemExit`` carrying
text, a keyboard interrupt) into that one line, whose stage is the closed set ``STAGES`` with one fixed
exit code each. There is no argument parser (the arguments must be exactly the program name; anything
else is a fixed exit), ``sys.excepthook``, ``threading.excepthook`` and ``sys.unraisablehook`` are
replaced, ``faulthandler`` is off, warnings are off, and the container's termination message file
(``/dev/termination-log``) is never opened, so no free text reaches the pod status or its events.
Nothing from the repository package is imported before the handler is in place: an import that fails
is a ``podworker: failed import`` line like any other.

The order of a session, each step a stage of ``STAGES``:

1. ``argv``, ``environment`` (the pod's allow-listed variables, all validated), ``token`` (the token
   file must be a regular file with no group or other permission; its content is held in memory only,
   is never copied, printed or put in an argument or in a child's environment).
2. ``canary``: before any tree is fetched, in parallel, three seconds each, a TCP connect that sends
   nothing to every FORBIDDEN destination of ``podcontract.CANARY_FORBIDDEN`` (the instance metadata
   address, the Azure host channel on 80 and 32526, the node's kubelet port, the manager's API and
   metrics ports); a successful connect is a failure. The API server is probed at BOTH of its addresses
   (the kubelet-injected service address, and the public address the launcher passes) with an
   UNAUTHENTICATED request to a namespaced path: only 401 or 403 or no connection passes, anything else
   fails. A CONTROL connect to the relay must succeed, so a dead network cannot pass as an isolated one.
   Any failure posts ``{"result": "failed", "code": <fixed code>}`` to the relay (which then refuses to
   serve the bundle) and ends with the fixed canary exit code; the bundle is never fetched.
3. ``preflight`` posts ``{"result": "ok"}``; ``bundle`` fetches the tree ONCE; ``extract`` writes it with
   the primitives of ``bundle.py`` (nothing is followed, nothing is overwritten) under the data directory
   at the identical absolute paths; ``manifest`` validates the manifest (below); ``chown`` hands the
   worktree to the worker identity; ``files`` writes the manifest's directories and files with the
   modes it says. A relay answer of 425 (the session is not bound to its pod yet) is retried with a
   bounded backoff, at most 12 attempts over 60 seconds in all, then ``wait``.
4. Part B: ``run`` (the manifest's ``run`` section), ``key`` (the Anthropic key file), ``spawn`` (the
   agent), then the session itself, then ``result`` (the report and the delta are posted once) or
   ``gone`` (the relay says the session is over: nothing is posted).

The manifest (``meta/manifest.json`` of the bundle, written by the supervisor, at most 256 KiB, no
secret) is read here for the keys of this part; other keys are the business of part B::

    {"version": 1,
     "worktree": "<path under the data directory>",             completed by bundle.stage
     "dirs":  ["<absolute path under the data directory>", ...],  the worker's throwaway home and config dir
     "files": [{"path": "<absolute path>", "data": "<base64>", "mode": 384 | 416}, ...],
     "run":   {...}}                                            part B's section, below

Every path must be absolute, under the data directory, clean (the rules of ``bundle.split_relative``:
no ``..``, no empty component, no ``.git`` in any spelling) and outside the worktree. A directory is
created 0700 and owned by the worker identity (the home is the worker's to use). A file is created
with ``O_EXCL`` (nothing is overwritten) with mode 0600 (the prompt, which only the wrapper reads and
hands to ``claude`` on its standard input) or 0640 (settings and system prompt: the group of the worker
may read them, the worker may not change them). No other mode is accepted.

The ``run`` section (part B; ``PodBackend`` writes it from the ``LaunchSpec``)::

    {"argv": ["<the agent's command line, the binary first>", ...],   1 to 256 strings
     "env":  {"HOME": "...", "CLAUDE_CONFIG_DIR": "...", ...},        exactly the names of WORKER_ENV_NAMES
     "cwd":  "<the worktree>",
     "prompt": "<the path of the manifest file (mode 0600) that holds the prompt>",
     "timeout": <seconds>,
     "memory_limit_mb": <integer> | null}

Standard library only. Nothing here ran in a pod: contract-tested against the in-process relay of
``relay.py``, a fake network for the canary, real temporary directories and real child processes.
"""

import base64
import collections
import contextlib
import faulthandler
import http.client
import importlib
import json
import os
import re
import select
import signal
import socket
import ssl
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import warnings
from dataclasses import dataclass
from typing import NamedTuple

# --- Stages, exit codes and the only lines this program prints ----------------------------------

# Stage -> its fixed exit code. The set is closed; a failure line names one of these and nothing else.
STAGES = {
    "argv": 64,
    "environment": 65,
    "token": 66,
    "import": 67,
    "canary": 68,
    "preflight": 69,
    "wait": 70,
    "bundle": 71,
    "extract": 72,
    "manifest": 73,
    "files": 74,
    "chown": 75,
    "run": 76,
    "internal": 77,
    "key": 78,
    "spawn": 79,
    "result": 80,
    "gone": 81,
}

# The lines of the wrapper itself. ``%d`` is the exit code of the session (part B).
_LINES = {
    "started": "podworker: started",
    "preflight_ok": "podworker: preflight ok",
    "fetched": "podworker: bundle fetched",
    "exit": "podworker: exit %d",
    "posted": "podworker: result posted",
}
_FAILED = "podworker: failed %s"


def _write(stream, text):
    # A closed or full stream cannot be reported anywhere else, so a failed write is dropped on purpose.
    with contextlib.suppress(OSError, ValueError):
        stream.write(f"{text}\n")
        stream.flush()


def say(kind, number=None):
    """Print one of the fixed lines on standard output; ``number`` (0 to 255) fills ``%d``."""
    text = _LINES[kind]
    if "%d" in text:
        valid = isinstance(number, int) and not isinstance(number, bool) and 0 <= number <= 255
        text = text % (number if valid else 255)
    _write(sys.stdout, text)


def fail(stage):
    """Print the one failure line for ``stage`` on standard error and return its fixed exit code.

    A stage outside ``STAGES`` is reported as ``internal``, so no text can reach the line through it.
    """
    name = stage if stage in STAGES else "internal"
    _write(sys.stderr, _FAILED % name)
    return STAGES[name]


class Failure(Exception):
    """A stage failed. It carries the stage name and nothing else, whatever caused it."""

    def __init__(self, stage):
        super().__init__(stage)
        self.stage = stage


class Progress:
    """The stage the program is in, so that an exception nobody classified is reported against it."""

    def __init__(self):
        self.stage = "internal"


# --- The interpreter's own ways of printing ------------------------------------------------------


def _die(stage):
    os._exit(fail(stage))


def _excepthook(kind, value, trace):
    """Replaces ``sys.excepthook``: an exception that escaped everything is one fixed line and a fixed exit."""
    _die("internal")


def _thread_excepthook(args):
    """Replaces ``threading.excepthook``: a thread that died is one fixed line and a fixed exit."""
    _die("internal")


def _unraisable(unraisable):
    """Replaces ``sys.unraisablehook``: an exception ignored in a destructor prints nothing."""
    return None


@contextlib.contextmanager
def _quiet():
    """Replace every hook through which the interpreter could print by itself; restore them afterwards."""
    hooks = (sys.excepthook, threading.excepthook, sys.unraisablehook)
    was_enabled = faulthandler.is_enabled()
    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    sys.unraisablehook = _unraisable
    faulthandler.disable()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            yield
    finally:
        sys.excepthook, threading.excepthook, sys.unraisablehook = hooks
        if was_enabled:
            # Best effort, for a caller that ran this in its own process (a test): a debugging aid that cannot
            # be switched back on (standard error without a file descriptor) must not become an exception here.
            with contextlib.suppress(Exception):
                faulthandler.enable()


# --- The package, imported inside the handler -----------------------------------------------------


class Support(NamedTuple):
    """The modules of the package this program uses: the contract, the session schema, the bundle, the memory guard and the report."""

    pc: object
    sr: object
    bundle: object
    memwatch: object
    report: object


def _import(name):
    return importlib.import_module(f".{name}", __package__)


def load_support():
    """Import the package modules; any failure is the ``import`` stage, never the text of the error."""
    try:
        return Support(_import("podcontract"), _import("sessionrecord"), _import("bundle"), _import("memwatch"), _import("podreport"))
    except Exception:  # an import can fail with any text; the caller reports one fixed line
        raise Failure("import") from None


# --- Configuration -------------------------------------------------------------------------------

ENV_SESSION = "FACTORY_SESSION_ID"
ENV_RELAY_URL = "FACTORY_RELAY_URL"
ENV_DEADLINE = "FACTORY_SESSION_DEADLINE"
ENV_POD_NAME = "FACTORY_POD_NAME"
ENV_POD_UID = "FACTORY_POD_UID"
# The relay's header names (``relay.HEADER_POD_NAME`` and ``HEADER_POD_UID``; a test pins that they agree).
HEADER_POD_NAME = "X-Factory-Pod-Name"
HEADER_POD_UID = "X-Factory-Pod-Uid"

_URL = re.compile(r"http://([A-Za-z0-9][A-Za-z0-9.-]{0,252}):([0-9]{1,5})")
_ADDRESS = re.compile(r"[A-Za-z0-9.:-]{1,253}")
_DEADLINE = re.compile(r"[0-9]{1,12}")
SECRET_MAX_BYTES = 4096


def _require(condition, stage):
    if not condition:
        raise Failure(stage)


@dataclass(frozen=True)
class Config:
    """What the pod's environment says about this session (all of it validated, none of it secret)."""

    session: str
    pod_name: str
    pod_uid: str
    relay_host: str
    relay_port: int
    deadline: int


def read_config(environ, support):
    """The session's configuration from the pod's allow-listed variables; the ``environment`` stage on any problem."""
    pc, sr = support.pc, support.sr
    try:
        session = pc.validate_session_id(environ.get(ENV_SESSION, ""))
        expected_name = pc.pod_name(session)
    except ValueError:
        raise Failure("environment") from None
    pod_name = environ.get(ENV_POD_NAME, "")
    pod_uid = environ.get(ENV_POD_UID, "")
    relay = _URL.fullmatch(environ.get(ENV_RELAY_URL, ""))
    deadline = environ.get(ENV_DEADLINE, "")
    _require(pod_name == expected_name, "environment")
    _require(sr.POD_UID_PATTERN.fullmatch(pod_uid) is not None, "environment")
    _require(relay is not None and 1 <= int(relay.group(2)) <= 65535, "environment")
    _require(_DEADLINE.fullmatch(deadline) is not None, "environment")
    return Config(session, pod_name, pod_uid, relay.group(1), int(relay.group(2)), int(deadline))


def _read_private(path, stage):
    """The text of a root-only file of the Secret volume, one trailing newline dropped; ``stage`` on any problem.

    The file must be a regular file without a single group or other permission bit. The kubelet builds a
    Secret volume out of links (the key points into a versioned directory it manages and nobody else can
    write), so the final component is followed; the open descriptor is what is checked.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        raise Failure(stage) from None
    try:
        info = os.fstat(fd)
        _require(stat.S_ISREG(info.st_mode) and not info.st_mode & 0o077 and info.st_size <= SECRET_MAX_BYTES, stage)
        data = os.read(fd, SECRET_MAX_BYTES + 1)
    except OSError:
        raise Failure(stage) from None
    finally:
        os.close(fd)
    text = data.decode("ascii", errors="replace")
    return text[:-1] if text.endswith("\n") else text


def read_token(directory, support):
    """The relay token, read from the root-only file of the Secret volume; the ``token`` stage on any problem.

    The value is held in memory only: never printed, never copied, never in an argument or an environment.
    """
    token = _read_private(f"{directory}/{support.pc.SECRET_KEY_TOKEN}", "token")
    _require(support.sr.TOKEN_PATTERN.fullmatch(token) is not None, "token")
    return token


_KEY = re.compile(r"[\x21-\x7e]{8,4096}")


def read_key(directory, support):
    """The Anthropic key, read like the token (part B); the ``key`` stage on any problem.

    It goes into the environment of the agent and nowhere else: not into an argument, a file, a log line or
    the report.
    """
    key = _read_private(f"{directory}/{support.pc.SECRET_KEY_ANTHROPIC}", "key")
    _require(_KEY.fullmatch(key) is not None, "key")
    return key


# --- Time ------------------------------------------------------------------------------------------


class SystemClock:
    """The real clock. Tests inject one whose ``sleep`` only moves a number."""

    @staticmethod
    def monotonic():
        return time.monotonic()

    @staticmethod
    def time():
        """Wall-clock epoch seconds (the session deadline is one)."""
        return time.time()

    @staticmethod
    def sleep(seconds):
        time.sleep(seconds)


# A relay answer of 425 means "this session is not bound to its pod yet". At most 12 requests in all (the
# preflight and the bundle share the budget) over at most 60 seconds, waiting between them; then ``wait``.
RETRY_ATTEMPTS = 12
RETRY_WAITS = (1, 2, 4, 5, 5, 5, 5, 5, 5, 5, 5)  # seconds before attempt 2 ... 12; 47 in all
RETRY_WINDOW_SECONDS = 60
TOO_EARLY = 425


class Backoff:
    """The bounded retry of a relay answer of 425, on an injectable clock."""

    def __init__(self, clock):
        self._clock = clock
        self._started = None
        self._refusals = 0

    def again(self):
        """Wait for the next attempt and return True, or False when the attempts or the window are used up."""
        if self._started is None:
            self._started = self._clock.monotonic()
        self._refusals += 1
        if self._refusals >= RETRY_ATTEMPTS:
            return False
        wait = RETRY_WAITS[self._refusals - 1]
        if self._clock.monotonic() - self._started + wait > RETRY_WINDOW_SECONDS:
            return False
        self._clock.sleep(wait)
        return True


# --- The relay, from the pod's side ------------------------------------------------------------------

RELAY_TIMEOUT_SECONDS = 60  # the relay's own idle timeout; one socket operation never waits longer


def _close(connection, response):
    """Close an answer and its connection (a response to a ``Connection: close`` request owns the socket)."""
    response.close()
    connection.close()


class RelayClient:
    """One request per connection; the credentials travel in headers only and are in no other text."""

    def __init__(self, host, port, token, pod_name, pod_uid, *, connection=http.client.HTTPConnection):
        self._address = (host, port)
        self._connection = connection
        self._headers = {
            "Authorization": f"Bearer {token}",
            HEADER_POD_NAME: pod_name,
            HEADER_POD_UID: pod_uid,
        }

    def __repr__(self):
        return "RelayClient()"

    def _send(self, method, path, body=None, length=None, content_type="application/json"):
        """(connection, response) for one request; the caller closes the connection.

        ``length`` is the ``Content-Length`` of a body that is an iterable of bytes (it is never sent chunked).
        """
        connection = self._connection(*self._address, timeout=RELAY_TIMEOUT_SECONDS)
        try:
            headers = dict(self._headers)
            if body is not None:
                headers["Content-Type"] = content_type
            if length is not None:
                headers["Content-Length"] = str(length)
            connection.request(method, path, body=body, headers=headers)
            return connection, connection.getresponse()
        except BaseException:
            connection.close()
            raise

    def preflight(self, document, backoff):
        """Post a preflight document; the status of the answer. A 425 is retried within the budget."""
        body = json.dumps(document, separators=(",", ":")).encode("ascii")
        while True:
            connection, response = self._send("POST", "/v1/preflight", body)
            status = response.status
            _close(connection, response)
            if status != TOO_EARLY:
                return status
            if not backoff.again():
                raise Failure("wait")

    def _status(self, method, path, body=None, length=None, content_type="application/json"):
        """The status of one request, or None when nothing answered as HTTP (the relay is unreachable); never raises on I/O."""
        try:
            connection, response = self._send(method, path, body, length, content_type)
        except (OSError, http.client.HTTPException, ValueError):
            return None
        try:
            return response.status
        finally:
            _close(connection, response)

    def stream(self, name, data):
        """Append a chunk to the transcript or the standard error stream of the session (part B); the status or None."""
        return self._status("POST", f"/v1/stream/{name}", data, content_type="application/octet-stream")

    def heartbeat(self):
        """Tell the relay the pod is alive (part B); the status or None."""
        return self._status("POST", "/v1/heartbeat")

    def result(self, report, delta, size):
        """Post the report and the delta once (part B); the status or None.

        The body is the relay's: a 4-byte big-endian length, that many bytes of report, then ``size`` bytes of
        ``delta`` (a binary file, read from its start in 64 KiB slices; ``None`` when ``size`` is 0).
        """

        def body():
            yield struct.pack(">I", len(report))
            yield report
            if size:
                delta.seek(0)
                while piece := delta.read(STREAM_SLICE_BYTES):
                    yield piece

        return self._status("POST", "/v1/result", body(), struct.calcsize(">I") + len(report) + size, "application/octet-stream")

    def fetch(self, backoff):
        """(connection, response) of ``GET /v1/bundle``, the first answer that is not a 425; the caller closes it."""
        while True:
            connection, response = self._send("GET", "/v1/bundle")
            if response.status != TOO_EARLY:
                return connection, response
            _close(connection, response)
            if not backoff.again():
                raise Failure("wait")


class _Source:
    """The body of the bundle answer as the binary source ``bundle.extract`` reads.

    Trouble on the connection is the ``bundle`` stage, not a tree error: the exception raised here is
    neither an ``OSError`` nor a ``ValueError``, so the extraction code does not mistake it for a write
    failure. A body shorter than its ``Content-Length`` is that trouble too (``http.client`` returns
    an empty read for a connection that closed early).
    """

    def __init__(self, response, size):
        self._response = response
        self._left = size

    def read(self, count):
        if not self._left or count <= 0:
            return b""
        try:
            data = self._response.read(min(count, self._left))
        except (OSError, http.client.HTTPException, ValueError):
            raise Failure("bundle") from None
        _require(bool(data), "bundle")
        self._left -= len(data)
        return data


def _content_length(response, limit):
    text = response.getheader("Content-Length")
    _require(text is not None and text.isascii() and text.isdigit() and int(text) <= limit, "bundle")
    return int(text)


# --- The network canary ---------------------------------------------------------------------------------

CANARY_TIMEOUT_SECONDS = 3
CANARY_JOIN_SECONDS = 10  # the whole canary: every probe has its own timeouts, the join only bounds a hung one
# A network policy enforcer may start enforcing a moment after the container starts (about 1 s on kind, not measured
# on AKS), so a check that found a destination reachable is repeated after these waits (seconds, 6 in all) before it
# is a failure. An enforcer that never arrives fails the last run, so the canary still fails closed.
CANARY_SETTLE_WAITS = (1, 2, 3)

# The fixed codes a failed canary posts (``sessionrecord.CODE_PATTERN``). Never a host, never an address.
_FORBIDDEN_CODES = {
    ("169.254.169.254", 80): "reach_imds",
    ("168.63.129.16", 80): "reach_wireserver",
    ("168.63.129.16", 32526): "reach_hostga",
    ("FACTORY_NODE_IP", 10250): "reach_kubelet",
    ("FACTORY_MANAGER_ADDRESS", 8080): "reach_manager_api",
    ("FACTORY_MANAGER_METRICS_ADDRESS", 9464): "reach_manager_metrics",
}
_API_CODES = {"service": "api_service_open", "public": "api_public_open"}
CODE_OTHER_FORBIDDEN = "reach_forbidden"
CODE_OTHER_API = "api_open"
CODE_CONFIG = "canary_config"
CODE_ERROR = "probe_error"
CODE_CONTROL = "control_failed"

_ISOLATION_CODES = frozenset(_FORBIDDEN_CODES.values()) | frozenset(_API_CODES.values()) | {CODE_OTHER_FORBIDDEN, CODE_OTHER_API}

_PENDING = object()  # the probe had not finished when the canary stopped waiting
_ERROR = object()  # the probe raised: the text is dropped, only this fact is kept


def _unverified_context():
    """A TLS context that does not verify the server certificate, for the API-server probe only.

    The probe sends no credential and no body and reads only the status line, and the pod has no copy of the
    cluster's CA (the service account volume is not mounted), so there is nothing to verify against and
    nothing to protect: what is asked is whether an anonymous request is answered.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


class Net:
    """The real network of the canary: a TCP connect that sends nothing, and one unauthenticated request."""

    def __init__(self, tls=True):
        self._tls = tls

    @staticmethod
    def connect(host, port, timeout):
        """True when a TCP connection to host:port was made. Nothing is sent; it is closed at once."""
        try:
            connection = socket.create_connection((host, port), timeout=timeout)
        except OSError:
            return False
        connection.close()
        return True

    def status(self, host, port, path, timeout):
        """The HTTP status of an unauthenticated GET of ``path``, or None when nothing answered as HTTP."""
        connection = (
            http.client.HTTPSConnection(host, port, timeout=timeout, context=_unverified_context())
            if self._tls
            else http.client.HTTPConnection(host, port, timeout=timeout)
        )
        response = None
        try:
            connection.request("GET", path, headers={"Accept": "application/json", "Connection": "close"})
            response = connection.getresponse()
            return response.status
        except (OSError, http.client.HTTPException, ValueError):
            return None
        finally:
            if response is not None:
                response.close()
            connection.close()


class Probe(NamedTuple):
    """One check of the canary: ``forbidden`` (must not connect), ``api`` (must not be answered) or ``control``."""

    kind: str
    host: str
    port: int
    code: str


def _endpoint(entry, environ):
    """(host, port) of a contract entry: its literal, or the value of the environment variable it names."""
    host = entry["host"] if "host" in entry else environ.get(entry["host_env"], "")
    if "port" in entry:
        port = entry["port"]
    else:
        text = environ.get(entry["port_env"], "")
        port = int(text) if text.isascii() and text.isdigit() else 0
    if not _ADDRESS.fullmatch(host) or not 1 <= port <= 65535:
        raise ValueError("canary endpoint")
    return host, port


def build_probes(support, environ, relay):
    """The checks of the canary, in a fixed order: the contract's forbidden list, its API probes, the control.

    ``relay`` is the (host, port) of the relay. ``ValueError`` when a destination cannot be resolved from the
    environment (a missing or malformed address): the canary then fails closed with ``canary_config``.
    """
    pc = support.pc
    probes = []
    for entry in pc.CANARY_FORBIDDEN:
        host, port = _endpoint(entry, environ)
        key = (entry.get("host") or entry["host_env"], entry["port"])
        probes.append(Probe("forbidden", host, port, _FORBIDDEN_CODES.get(key, CODE_OTHER_FORBIDDEN)))
    for entry in pc.CANARY_API_PROBES:
        host, port = _endpoint(entry, environ)
        probes.append(Probe("api", host, port, _API_CODES.get(entry["name"], CODE_OTHER_API)))
    probes.append(Probe("control", relay[0], relay[1], CODE_CONTROL))
    return probes


def _probe(probe, net, path, timeout, outcomes, index):
    try:
        if probe.kind == "api":
            outcomes[index] = net.status(probe.host, probe.port, path, timeout)
        else:
            outcomes[index] = net.connect(probe.host, probe.port, timeout)
    except Exception:  # whatever went wrong is one fixed result; its text is never kept
        outcomes[index] = _ERROR


def _is_isolation_code(code):
    """True for a code that says an address was reachable (the only one a late enforcer can explain)."""
    return code in _ISOLATION_CODES


def _failure_code(probe, outcome, denied):
    """The code of a failed check, None for a passed one. Anything a check did not clearly pass is a failure."""
    if outcome is _ERROR:
        return CODE_ERROR
    if outcome is _PENDING and probe.kind != "control":
        # Fail closed: a probe that never finished (a hostname blocked in getaddrinfo, say) proved nothing, so it
        # must not pass a destination that is forbidden or an API server that must refuse. The control already fails.
        return CODE_ERROR
    if probe.kind == "forbidden":
        return None if outcome is False else probe.code
    if probe.kind == "api":
        return None if outcome is None or outcome in denied else probe.code
    return None if outcome is True else probe.code


def run_canary(probes, net, path, denied, timeout=CANARY_TIMEOUT_SECONDS, join_seconds=CANARY_JOIN_SECONDS):
    """Run every probe in parallel and return the code of the first failed check (in probe order), or None.

    A forbidden destination that connected fails; an API server that answered anything but one of
    ``denied`` fails; the control that did not connect fails. A probe that has not finished when the
    join time is over fails closed: the control and every other check count it as failed (`probe_error` for
    a forbidden or API probe). The threads are daemons and every join is bounded.
    """
    outcomes = [_PENDING] * len(probes)
    threads = [
        threading.Thread(target=_probe, args=(probe, net, path, timeout, outcomes, index), name=f"podworker-canary-{index}", daemon=True)
        for index, probe in enumerate(probes)
    ]
    for thread in threads:
        thread.start()
    limit = time.monotonic() + join_seconds
    for thread in threads:
        thread.join(max(0.0, limit - time.monotonic()))
    for probe, outcome in zip(probes, outcomes, strict=True):
        code = _failure_code(probe, outcome, denied)
        if code is not None:
            return code
    return None


# --- The manifest ------------------------------------------------------------------------------------------

MANIFEST_VERSION = 1
FILE_MODES = (0o600, 0o640)
DIR_MODE = 0o700
MAX_MANIFEST_FILES = 64
MAX_MANIFEST_DIRS = 16


@dataclass(frozen=True)
class Plan:
    """What the manifest asks for, validated: directories (components under the data directory) and files."""

    dirs: tuple
    files: tuple


class FileSpec(NamedTuple):
    parts: tuple
    data: bytes
    mode: int


def _parts_of(path, prefix, support):
    _require(isinstance(path, str) and path.startswith(prefix), "manifest")
    try:
        return support.bundle.split_relative(path[len(prefix) :])
    except ValueError:  # a WireError: not a clean relative path
        raise Failure("manifest") from None


def _file_spec(entry, prefix, support):
    _require(isinstance(entry, dict) and set(entry) == {"path", "data", "mode"}, "manifest")
    mode = entry["mode"]
    _require(isinstance(mode, int) and not isinstance(mode, bool) and mode in FILE_MODES, "manifest")
    _require(isinstance(entry["data"], str), "manifest")
    try:
        data = base64.b64decode(entry["data"].encode("ascii"), validate=True)
    except ValueError:
        raise Failure("manifest") from None
    return FileSpec(_parts_of(entry["path"], prefix, support), data, mode)


def plan_files(manifest, root, worktree, support):
    """The validated ``Plan`` of a manifest; the ``manifest`` stage when anything about it is not as documented.

    ``root`` is the data directory (a real path), ``worktree`` the components of the worktree under it. Nothing
    is written here.
    """
    _require(manifest.get("version") == MANIFEST_VERSION and not isinstance(manifest.get("version"), bool), "manifest")
    prefix = f"{root}/"
    dirs = manifest.get("dirs", [])
    files = manifest.get("files", [])
    _require(isinstance(dirs, list) and len(dirs) <= MAX_MANIFEST_DIRS, "manifest")
    _require(isinstance(files, list) and len(files) <= MAX_MANIFEST_FILES, "manifest")
    dir_parts = tuple(_parts_of(path, prefix, support) for path in dirs)
    specs = tuple(_file_spec(entry, prefix, support) for entry in files)
    claimed = [*dir_parts, *(spec.parts for spec in specs)]
    _require(len(set(claimed)) == len(claimed), "manifest")
    _require(not any(parts[: len(worktree)] == tuple(worktree) for parts in claimed), "manifest")
    return Plan(dir_parts, specs)


def chown_tree(top, uid, gid, lchown):
    """Give ``top`` and everything under it to ``uid:gid``; links are not followed (the link itself is changed).

    Nothing else runs in the pod while this happens, so the paths ``os.walk`` lists are the paths that are changed;
    a directory it cannot list is an error, not a skipped subtree.
    """

    def refuse(error):
        raise error

    lchown(top, uid, gid)
    for directory, names, files in os.walk(top, followlinks=False, onerror=refuse):
        for name in (*names, *files):
            lchown(os.path.join(directory, name), uid, gid)


def write_files(plan, root, identity, lchown, support):
    """Create the manifest's directories and files under ``root`` through ``bundle.TreeWriter``.

    Directories are 0700 and the worker's; a file is created exclusively with its mode (the prompt, 0600, stays
    root's and secret from the worker; a 0640 file gets the worker's group so it can be read and not changed).
    """
    uid, gid = identity
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        writer = support.bundle.TreeWriter(root_fd)
        for parts in plan.dirs:
            fd = writer.open_dir(parts)
            try:
                os.fchmod(fd, DIR_MODE)
            finally:
                os.close(fd)
            lchown(os.path.join(root, *parts), uid, gid)
        for spec in plan.files:
            writer.create_file(spec.parts, [spec.data], spec.mode)
            if spec.mode & stat.S_IRGRP:
                lchown(os.path.join(root, *spec.parts), -1, gid)
    finally:
        os.close(root_fd)


# --- The run (part B) -----------------------------------------------------------------------------------
#
# ``Worker.execute`` runs the agent as the worker identity, streams what it writes to the relay, heartbeats,
# and posts the delta, the footprint and the outcome once. Nothing the agent wrote is ever printed by this
# program: its standard output and standard error go to pipes that are read here and sent to the relay, never
# to this program's own streams, which stay the fixed lines of ``_LINES``.

# The names that ``agent.worker_env`` sets and ``agent.CHILD_ENV_KEYS`` copies, and the umask of
# ``permissions.WORKER_UMASK`` (a test pins that the copies agree: this module imports nothing of the package
# before its handler is in place).
WORKER_ENV_NAMES = (
    "HOME",
    "CLAUDE_CONFIG_DIR",
    "DISABLE_AUTOUPDATER",
    "DISABLE_TELEMETRY",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
)
CHILD_ENV_KEYS = ("PATH", "SHELL", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "NO_COLOR")
WORKER_UMASK = 0o002
ENV_KEY = "ANTHROPIC_API_KEY"

RUN_KEYS = ("argv", "env", "cwd", "prompt", "timeout", "memory_limit_mb")
MAX_ARGV = 256
MAX_ARG_CHARS = 64 * 1024
MAX_ENV_VALUE_CHARS = 4096
MIN_TIMEOUT_SECONDS = 0.1
MAX_MEMORY_LIMIT_MB = 1 << 20

# All PROVISIONAL (nothing ran in a pod): the time kept for the delta and the result before the token deadline,
# the grace the agent gets between SIGTERM and SIGKILL (as ``backend._stop_group``), the time the pipes get to
# drain after the agent's group is gone and the time the senders get to flush what is buffered.
RESULT_RESERVE_SECONDS = 60
STOP_GRACE_SECONDS = 5
DRAIN_SECONDS = 5
FLUSH_SECONDS = 60
# After SIGTERM the pod has ``podcontract.TERMINATION_GRACE_SECONDS`` (30) in all: the stop grace, then these two, then the delta.
TERMINATED_DRAIN_SECONDS = 2
TERMINATED_FLUSH_SECONDS = 8
# After a deadline the threads that did not end get this long, once for all the pumps, to see the flag that tells them to stop.
ABANDON_JOIN_SECONDS = 1
# The worst case between SIGTERM and the start of the delta read, with every wait running out at once: SIGTERM to the
# group and the stop grace, the wait after SIGKILL, ONE drain deadline and ONE flush deadline shared by both pumps, and the
# join of the threads abandoned after each. A test pins that this leaves time of the 30 s grace for the delta.
TERMINATED_WORST_SECONDS = (
    2 * STOP_GRACE_SECONDS + TERMINATED_DRAIN_SECONDS + TERMINATED_FLUSH_SECONDS + 2 * ABANDON_JOIN_SECONDS
)

STREAM_SLICE_BYTES = 64 * 1024
STREAM_BUFFER_BYTES = 8 * 1024 * 1024  # what one stream may hold before the agent's pipe is not read (it blocks: nothing is dropped)
STREAM_SEND_BYTES = 1024 * 1024  # one post carries at most this much (the relay accepts 8 MiB)
READ_POLL_SECONDS = 0.25
FEED_JOIN_SECONDS = 1  # the thread that writes the prompt looks at its stop flag at least every READ_POLL_SECONDS
POST_WAITS = (1, 2)  # a chunk is tried 3 times
RESULT_WAITS = (1, 2)  # the result is tried 3 times
CLIENT_ERROR = range(400, 500)


def exit_number(returncode):
    """The number of the ``podworker: exit <n>`` line: the agent's exit status, or 128 plus the signal that ended it."""
    return min(returncode, 255) if returncode >= 0 else min(128 - returncode, 255)


@dataclass(frozen=True)
class RunSpec:
    """The validated ``run`` section of the manifest."""

    argv: tuple
    env: dict
    cwd: str
    prompt: bytes
    timeout: float
    memory_limit_mb: int | None


def _text(value, limit):
    """True for a string that can be an argument or an environment value: no NUL, at most ``limit`` characters, encodable."""
    if not isinstance(value, str) or len(value) > limit or "\0" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def read_run(prepared, support):
    """The ``RunSpec`` of the manifest's ``run`` section; the ``run`` stage when anything about it is not as documented.

    Everything is checked before the agent is started. ``cwd`` must be the worktree itself; ``HOME`` and
    ``CLAUDE_CONFIG_DIR`` must be directories the manifest asks to be created (so they exist and are the
    worker's); ``prompt`` must name a manifest file of mode 0600 (the prompt is never readable by the worker).
    """
    manifest = prepared.manifest
    run = manifest.get("run")
    _require(isinstance(run, dict) and set(run) == set(RUN_KEYS), "run")
    argv, env = run["argv"], run["env"]
    _require(isinstance(argv, list) and 1 <= len(argv) <= MAX_ARGV, "run")
    _require(all(_text(item, MAX_ARG_CHARS) for item in argv) and argv[0] != "", "run")
    _require(isinstance(env, dict) and {"HOME", "CLAUDE_CONFIG_DIR"} <= set(env) <= set(WORKER_ENV_NAMES), "run")
    _require(all(_text(value, MAX_ENV_VALUE_CHARS) for value in env.values()), "run")
    dirs = manifest.get("dirs", [])
    _require(env["HOME"] in dirs and env["CLAUDE_CONFIG_DIR"] in dirs, "run")
    _require(run["cwd"] == prepared.worktree, "run")
    prompts = [
        entry
        for entry in manifest.get("files", [])
        if isinstance(entry, dict) and entry.get("path") == run["prompt"] and entry.get("mode") == 0o600
    ]
    _require(len(prompts) == 1, "run")
    timeout, limit = run["timeout"], run["memory_limit_mb"]
    _require(isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout == timeout, "run")
    _require(MIN_TIMEOUT_SECONDS <= timeout <= support.pc.DEADLINE_CLAMP_SECONDS, "run")
    _require(limit is None or (isinstance(limit, int) and not isinstance(limit, bool) and 1 <= limit <= MAX_MEMORY_LIMIT_MB), "run")
    try:
        prompt = base64.b64decode(prompts[0]["data"].encode("ascii"), validate=True)
    except ValueError:
        raise Failure("run") from None
    return RunSpec(tuple(argv), dict(env), run["cwd"], prompt, float(timeout), limit)


def child_env(environ, spec, key):
    """The environment of the agent: the few inherited names, the manifest's non-secret names and the key. Nothing else."""
    env = {name: environ[name] for name in CHILD_ENV_KEYS if name in environ}
    env.update(spec.env)
    env[ENV_KEY] = key
    return env


def identity_kwargs(identity):
    """What ``subprocess.Popen`` needs to start the agent as the worker and nobody else, as ``backend._identity_kwargs`` does."""
    uid, gid = identity
    return {"user": uid, "group": gid, "extra_groups": [], "umask": WORKER_UMASK}


def _execnocaps():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "execnocaps.py")


def wrapped(argv):
    """The command line that clears every ambient capability before the agent runs (``agent.drop_capabilities_first``)."""
    return [sys.executable, "-I", "-S", _execnocaps(), "--", *argv]


@dataclass(frozen=True)
class Tuning:
    """The seams and numbers of the run; the program itself passes none. ``None`` is the production default."""

    popen: object = subprocess.Popen
    identity_kwargs: object = None  # a callable (uid, gid) -> the Popen keywords; None is ``identity_kwargs``
    wrap: bool = True  # False: no capability-dropping wrapper (a test that runs the agent as itself)
    tmp_dir: str | None = None  # None is the contract's /tmp
    heartbeat_seconds: float | None = None  # None is the contract's 30
    stop_grace: float = STOP_GRACE_SECONDS
    drain: float = DRAIN_SECONDS
    flush: float = FLUSH_SECONDS
    terminated_drain: float = TERMINATED_DRAIN_SECONDS
    terminated_flush: float = TERMINATED_FLUSH_SECONDS
    post_waits: tuple = POST_WAITS
    result_waits: tuple = RESULT_WAITS
    signals: bool = True  # install the SIGTERM handler (the main thread only)
    reserve: float = RESULT_RESERVE_SECONDS


class Pump:
    """One stream of the agent (its transcript or its standard error) on its way to the relay.

    A reader thread drains the agent's pipe into a buffer of at most ``STREAM_BUFFER_BYTES`` and a sender thread posts
    what is buffered, coalesced into posts of at most ``STREAM_SEND_BYTES``, in order. A full buffer stops the reader
    (the agent then blocks on its pipe): while the relay answers nothing is dropped, because the transcript carries
    the agent's result event. A post is tried three times; the relay refusing it (a 4xx answer: the session is over, a
    cap was reached, the pod is not the bound one) calls ``on_gone`` and ends the stream; the relay not answering (or
    answering 5xx) three times in a row ends the stream too, and from then on everything the agent writes is read
    and DROPPED (``dropped``), so a dead relay never blocks the agent. Nothing is ever printed.
    """

    def __init__(self, name, fd, post, on_gone, waits=POST_WAITS):
        self.name = name
        self._fd = fd
        self._post = post
        self._on_gone = on_gone
        self._waits = waits
        self._cond = threading.Condition()
        self._chunks = collections.deque()
        self._held = 0
        self._eof = False
        self._abandon = threading.Event()
        self._broken = False
        self.sent = 0
        self.dropped = False
        self._reader = threading.Thread(target=self._read, name=f"podworker-read-{name}", daemon=True)
        self._sender = threading.Thread(target=self._send, name=f"podworker-send-{name}", daemon=True)

    def start(self):
        self._reader.start()
        self._sender.start()

    def _read(self):
        try:
            while not self._abandon.is_set():
                ready, _, _ = select.select([self._fd], [], [], READ_POLL_SECONDS)
                if not ready:
                    continue
                data = os.read(self._fd, STREAM_SLICE_BYTES)
                if not data:
                    break
                self._put(data)
        except (OSError, ValueError):  # the pipe was closed under us: the stream is over
            pass
        finally:
            with self._cond:
                self._eof = True
                self._cond.notify_all()

    def _put(self, data):
        with self._cond:
            while self._held >= STREAM_BUFFER_BYTES and not self._broken and not self._abandon.is_set():
                self._cond.wait(READ_POLL_SECONDS)
            if self._broken or self._abandon.is_set():
                self.dropped = True
                return
            self._chunks.append(data)
            self._held += len(data)
            self._cond.notify_all()

    def _take(self):
        """The next batch to send (at most ``STREAM_SEND_BYTES``), or None when the reader is done and nothing is left."""
        with self._cond:
            while not self._chunks and not self._eof:
                self._cond.wait()
            if not self._chunks:
                return None
            batch = bytearray()
            while self._chunks and (not batch or len(batch) + len(self._chunks[0]) <= STREAM_SEND_BYTES):
                batch += self._chunks.popleft()
            self._held -= len(batch)
            self._cond.notify_all()
            return bytes(batch)

    def _send(self):
        while (batch := self._take()) is not None:
            if self._broken:
                self.dropped = True
                continue
            self._deliver(batch)

    def _deliver(self, batch):
        waits = self._waits
        for attempt in range(len(waits) + 1):
            status = self._post(self.name, batch)
            if status == 200:
                self.sent += len(batch)
                return
            if status is not None and status in CLIENT_ERROR:
                self._on_gone()
                break
            if attempt < len(waits) and self._abandon.wait(waits[attempt]):
                break
        with self._cond:  # the relay refused or did not answer: this stream is over, the rest is dropped
            self._broken = True
            self.dropped = True
            self._cond.notify_all()

    def finish(self, drain_seconds, flush_seconds):
        """Wait for the pipe to end (``drain_seconds``), then for the buffer to be sent (``flush_seconds``); one pump alone."""
        finish_pumps((self,), drain_seconds, flush_seconds)

    def _drain(self, deadline):
        self._reader.join(max(0.0, deadline - time.monotonic()))
        if self._reader.is_alive():  # a process that left the agent's group still holds the pipe: stop reading
            self._abandon.set()

    def _flush(self, deadline):
        self._sender.join(max(0.0, deadline - time.monotonic()))
        if self._sender.is_alive():
            self._abandon.set()
            with self._cond:
                self._broken = True
                self.dropped = True
                self._cond.notify_all()

    def reader_done(self):
        return not self._reader.is_alive()


def finish_pumps(pumps, drain_seconds, flush_seconds):
    """Finish all the pumps against ONE drain deadline and then ONE flush deadline (not one after the other).

    Both readers drain for at most ``drain_seconds`` in all, the readers still running are told to stop and given
    ``ABANDON_JOIN_SECONDS`` (once, together); then the senders flush for at most ``flush_seconds`` in all and the ones
    still running are abandoned the same way. The whole is at most ``drain + flush + 2 * ABANDON_JOIN_SECONDS``, however
    many pumps there are.
    """
    pumps = tuple(pumps)
    deadline = time.monotonic() + drain_seconds
    for pump in pumps:
        pump._drain(deadline)
    deadline = time.monotonic() + ABANDON_JOIN_SECONDS
    for pump in pumps:
        pump._reader.join(max(0.0, deadline - time.monotonic()))
    deadline = time.monotonic() + flush_seconds
    for pump in pumps:
        pump._flush(deadline)
    deadline = time.monotonic() + ABANDON_JOIN_SECONDS
    for pump in pumps:
        pump._sender.join(max(0.0, deadline - time.monotonic()))


def _no_notice(*_):
    """The memory guard's own fixed lines are not on this program's whitelist: they are dropped here."""
    return None


@contextlib.contextmanager
def _muted(module):
    saved = module.notice
    module.notice = _no_notice
    try:
        yield
    finally:
        module.notice = saved


class Session:
    """Part B of one session: run the agent, stream, heartbeat, stop it when it must stop, and post the result once.

    The order, every step bounded: the agent starts in its own process group as the worker identity, with the
    prompt on its standard input; its two pipes are pumped to the relay; a heartbeat goes out every
    ``HEARTBEAT_SECONDS``; the memory guard samples its tree. The session ends when the agent exits, when its time
    limit (the manifest's ``timeout``, at most the token deadline less ``RESULT_RESERVE_SECONDS``) is reached,
    when SIGTERM arrives (the pod is being deleted, or its deadline passed) or when the relay says the session is
    over (any 4xx answer to a heartbeat or a stream post, 410 above all: a manager that was replaced, a revoked or
    expired token). Ending it by force is SIGTERM to the agent's group, ``STOP_GRACE_SECONDS`` and SIGKILL; the group
    gets SIGKILL last in every case, as ``LocalBackend`` does. Then the pipes drain, the delta of the tree against
    the baseline is produced, and the report and the delta are posted once. The heartbeat goes on through all of that
    and stops only when the result is posted (or the session failed). A session the relay ended gets no
    result (the post could only be refused): ``gone``. SIGTERM again while draining changes nothing: the final
    delta and result are still attempted, and the kubelet's SIGKILL at the end of the grace is the only limit.
    """

    def __init__(self, prepared, run, key, environ, support, clock, tuning):
        self._prepared = prepared
        self._run = run
        self._support = support
        self._clock = clock
        self._tuning = tuning
        self._env = child_env(environ, run, key)
        self._client = prepared.client
        self._wake = threading.Event()
        self._exited = threading.Event()
        self._terminate = threading.Event()
        self._gone = threading.Event()
        self._stop_beat = threading.Event()
        self._stop_feed = threading.Event()
        self._feeder = None
        self._beater = None
        self._pumps = {}
        self.returncode = None

    # -- signals and groups ---------------------------------------------------------------------------------------

    def _on_term(self, number, frame):
        self._terminate.set()
        self._wake.set()

    @contextlib.contextmanager
    def _signals(self):
        if not self._tuning.signals or threading.current_thread() is not threading.main_thread():
            yield
            return
        previous = signal.signal(signal.SIGTERM, self._on_term)
        try:
            yield
        finally:
            signal.signal(signal.SIGTERM, previous if previous is not None else signal.SIG_DFL)

    def _gone_now(self):
        self._gone.set()
        self._wake.set()

    @staticmethod
    def _signal_group(pgid, number):
        # The group may already be gone (or hold nothing the wrapper may signal): never an error here.
        with contextlib.suppress(OSError):
            os.killpg(pgid, number)

    # -- the agent ------------------------------------------------------------------------------------------------

    def _spawn(self):
        tuning = self._tuning
        command = wrapped(self._run.argv) if tuning.wrap else list(self._run.argv)
        keywords = (tuning.identity_kwargs or identity_kwargs)(self._prepared.identity)
        try:
            return tuning.popen(
                command,
                cwd=self._run.cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._env,
                close_fds=True,
                start_new_session=True,
                **keywords,
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            raise Failure("spawn") from None

    def _feed(self, process):
        """The prompt to the agent's standard input (in a thread: a prompt larger than the pipe must not block the wrapper).

        It is written to the raw descriptor, never through the buffered writer of ``Popen``, and the descriptor is
        not blocking: the thread waits for room with a timeout and looks at ``_stop_feed`` each time. So a process
        outside the agent's group that holds the read end and never reads cannot keep the thread, or a lock of the
        buffered writer (which ``close`` takes), blocked: ``_close_pipes`` stops the thread and then closes.
        """
        stream = process.stdin
        try:
            fd = stream.fileno()
            os.set_blocking(fd, False)
            data = memoryview(self._run.prompt)
            while data and not self._stop_feed.is_set():
                _, ready, _ = select.select([], [fd], [], READ_POLL_SECONDS)
                if ready:
                    with contextlib.suppress(BlockingIOError):
                        data = data[os.write(fd, data[:STREAM_SLICE_BYTES]) :]
        except (OSError, ValueError):  # an agent that exits without reading it is not an error here
            pass
        finally:
            with contextlib.suppress(OSError, ValueError):  # the end of the prompt: the agent sees the end of its input
                stream.close()

    def _reap(self, process):
        self.returncode = process.wait()
        self._exited.set()
        self._wake.set()

    def _beat(self):
        interval = self._tuning.heartbeat_seconds or self._support.pc.HEARTBEAT_SECONDS
        while True:
            status = self._client.heartbeat()
            if status is not None and status in CLIENT_ERROR:
                self._gone_now()
                return
            if self._stop_beat.wait(interval):
                return

    def _stop(self, process):
        """SIGTERM to the agent's group, the grace, then SIGKILL to the group."""
        self._signal_group(process.pid, signal.SIGTERM)
        if not self._exited.wait(self._tuning.stop_grace):
            self._signal_group(process.pid, signal.SIGKILL)
            self._exited.wait(STOP_GRACE_SECONDS)

    def _limit(self):
        """Seconds the agent may run: its timeout, at most what the token deadline leaves less the reserve."""
        left = self._prepared.config.deadline - self._clock.time() - self._tuning.reserve
        limit = min(self._run.timeout, left)
        _require(limit >= MIN_TIMEOUT_SECONDS, "run")
        return limit

    def _await(self, process, limit):
        """Wait for the end of the session; the reason it ended: ``exited``, ``gone``, ``terminated`` or ``timeout``."""
        self._wake.wait(limit)
        if self._exited.is_set():
            return "exited"
        if self._gone.is_set():
            reason = "gone"
        elif self._terminate.is_set():
            reason = "terminated"
        else:
            reason = "timeout"
        self._stop(process)
        return reason

    # -- the delta ------------------------------------------------------------------------------------------------

    def _delta(self):
        """(status, records, bytes, file): the delta in an unnamed temporary file, or an empty one with a status that says why not."""
        bundle = self._support.bundle
        try:
            copy = tempfile.TemporaryFile(dir=self._tuning.tmp_dir or self._support.pc.TMP_DIR)
        except OSError:
            return "failed", 0, 0, None
        status = "failed"
        try:
            root_fd = os.open(self._prepared.worktree, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                produced = bundle.produce_delta(root_fd, self._prepared.baseline, copy)
            finally:
                os.close(root_fd)
            copy.flush()
            return "ok", produced.records, produced.size, copy
        except bundle.BundleTooLarge:
            status = "too_large"
        except (bundle.BundleError, OSError, ValueError):
            status = "failed"
        copy.close()
        return status, 0, 0, None

    def _post(self, report, copy, size):
        """Post the result once: a lost answer is retried (a session already finished answers 410: ``gone``)."""
        waits = self._tuning.result_waits
        for attempt in range(len(waits) + 1):
            status = self._client.result(report, copy, size)
            if status == 200:
                return
            if status == 410:
                raise Failure("gone")
            if status is not None and status in CLIENT_ERROR:
                break
            if attempt < len(waits):
                self._clock.sleep(waits[attempt])
        raise Failure("result")

    # -- the session ----------------------------------------------------------------------------------------------

    def run(self):
        """Run the session; the exit code of the wrapper (0 when the result was posted)."""
        limit = self._limit()
        with self._signals():
            process = self._spawn()
            try:
                return self._supervise(process, limit)
            finally:
                self._stop_beat.set()  # the result is posted (or the session failed): the heartbeat has nothing left to say
                self._signal_group(process.pid, signal.SIGKILL)
                self._close_pipes(process)
                if self._beater is not None:
                    self._beater.join(ABANDON_JOIN_SECONDS)

    def _close_pipes(self, process):
        """Close the agent's three pipes; a read end only once its reader is done (its descriptor number must not be reused under it).

        The prompt's thread is stopped first (it sees the flag within ``READ_POLL_SECONDS``) and the write end is closed
        only once it is gone, so no thread is inside a write on a descriptor that is closed (and its number reused).
        """
        self._stop_feed.set()
        if self._feeder is not None:
            self._feeder.join(FEED_JOIN_SECONDS)
        streams = {"transcript": process.stdout, "stderr": process.stderr}
        if self._feeder is None or not self._feeder.is_alive():
            with contextlib.suppress(OSError, ValueError):
                if process.stdin is not None:
                    process.stdin.close()
        for name, stream in streams.items():
            pump = self._pumps.get(name)
            if stream is not None and (pump is None or pump.reader_done()):
                with contextlib.suppress(OSError, ValueError):
                    stream.close()

    def _supervise(self, process, limit):
        support, tuning = self._support, self._tuning
        pumps = self._pumps
        for name, stream in (("transcript", process.stdout), ("stderr", process.stderr)):
            pumps[name] = Pump(name, stream.fileno(), self._client.stream, self._gone_now, tuning.post_waits)
        for pump in pumps.values():
            pump.start()
        self._feeder = threading.Thread(target=self._feed, args=(process,), name="podworker-feed", daemon=True)
        self._feeder.start()
        threading.Thread(target=self._reap, args=(process,), name="podworker-reap", daemon=True).start()
        beat = self._beater = threading.Thread(target=self._beat, name="podworker-beat", daemon=True)
        with _muted(support.memwatch):
            watch = support.memwatch.Watchdog(
                process.pid, self._run.memory_limit_mb, lambda number: self._signal_group(process.pid, number)
            ).start()
            beat.start()
            try:
                reason = self._await(process, limit)
            finally:
                # The heartbeat is NOT stopped here: it goes on through the drain, the delta read and the result upload
                # (``run`` stops it once the result is posted), so a long delta never looks like a silent pod.
                self._signal_group(process.pid, signal.SIGKILL)  # whatever the agent left running in its group
                watch.stop()
        terminated = reason == "terminated"
        finish_pumps(
            pumps.values(),
            tuning.terminated_drain if terminated else tuning.drain,
            tuning.terminated_flush if terminated else tuning.flush,
        )
        if self._gone.is_set():
            raise Failure("gone")
        return self._finish(process, reason, watch, pumps)

    def _finish(self, process, reason, watch, pumps):
        support = self._support
        returncode = self.returncode if self.returncode is not None else process.returncode
        _require(isinstance(returncode, int), "run")
        exceeded = watch.exceeded
        outcome = "memory_limit" if exceeded is not None else reason
        say("exit", exit_number(returncode))
        status, records, size, copy = self._delta()
        try:
            try:
                report = support.report.dumps(
                    support.report.build(
                        outcome,
                        returncode,
                        watch.footprint(),
                        None if exceeded is None else int(exceeded // support.memwatch.MIB),
                        (status, records, size),
                        {name: pump.sent for name, pump in pumps.items()},
                        {name: pump.dropped for name, pump in pumps.items()},
                    )
                )
            except ValueError:
                raise Failure("result") from None
            self._post(report, copy, size)
        finally:
            if copy is not None:
                copy.close()
        say("posted")
        return 0


# --- Part A of the session, and the program ---------------------------------------------------------------------


@dataclass(frozen=True)
class Prepared:
    """What part A hands to part B: the session, the relay client, the extracted tree and the baseline."""

    config: Config
    client: RelayClient
    root: str
    worktree: str
    manifest: dict
    baseline: dict
    identity: tuple


class Worker:
    """Part A of one session. ``prepare`` does everything up to a laid-out tree; ``execute`` is part B's."""

    def __init__(self, environ, support, progress, *, net, clock, lchown, root, identity, secret_dir, tuning=None):
        self._tuning = tuning or Tuning()
        self._environ = environ
        self._support = support
        self._progress = progress
        self._net = net
        self._clock = clock
        self._lchown = lchown
        self._root = root
        self._identity = identity
        self._secret_dir = secret_dir

    def prepare(self):
        """Environment, token, canary, preflight, fetch, extract, manifest, ownership and files; ``Prepared``."""
        progress = self._progress
        progress.stage = "environment"
        config = read_config(self._environ, self._support)
        progress.stage = "token"
        token = read_token(self._secret_dir, self._support)
        client = RelayClient(config.relay_host, config.relay_port, token, config.pod_name, config.pod_uid)
        backoff = Backoff(self._clock)
        progress.stage = "canary"
        self._canary(config, client, backoff)
        progress.stage = "preflight"
        _require(client.preflight({"result": "ok"}, backoff) == 200, "preflight")
        say("preflight_ok")
        progress.stage = "bundle"
        extracted = self._fetch(client, backoff)
        say("fetched")
        return self._lay_out(config, client, extracted)

    def _canary(self, config, client, backoff):
        support = self._support
        try:
            probes = build_probes(support, self._environ, (config.relay_host, config.relay_port))
            denied = tuple(support.pc.CANARY_API_DENIED_STATUSES)
            code = run_canary(probes, self._net, support.pc.CANARY_API_PROBE_PATH, denied)
            for wait in CANARY_SETTLE_WAITS:
                if code is None or not _is_isolation_code(code):
                    break
                self._clock.sleep(wait)
                code = run_canary(probes, self._net, support.pc.CANARY_API_PROBE_PATH, denied)
        except ValueError:
            code = CODE_CONFIG
        if code is None:
            return
        # What the wrapper reports is the failure itself; a post that cannot be delivered changes nothing
        # (the exit code and the pod phase tell the supervisor), so its own failure is dropped.
        with contextlib.suppress(Exception):
            client.preflight({"result": "failed", "code": code}, backoff)
        raise Failure("canary")

    def _fetch(self, client, backoff):
        connection, response = client.fetch(backoff)
        try:
            _require(response.status == 200, "bundle")
            source = _Source(response, _content_length(response, self._support.pc.BUNDLE_MAX_BYTES))
            self._progress.stage = "extract"
            extracted = self._support.bundle.extract(source, self._root)
        finally:
            _close(connection, response)
        return extracted

    def _lay_out(self, config, client, extracted):
        support, progress = self._support, self._progress
        root = os.path.realpath(self._root)
        worktree = f"{root}/{extracted.worktree}"
        progress.stage = "manifest"
        plan = plan_files(extracted.manifest, root, tuple(extracted.worktree.split("/")), support)
        progress.stage = "chown"
        uid, gid = self._identity
        chown_tree(worktree, uid, gid, self._lchown)
        progress.stage = "files"
        write_files(plan, root, self._identity, self._lchown, support)
        return Prepared(config, client, root, worktree, extracted.manifest, extracted.baseline, self._identity)

    def execute(self, prepared):
        """Run the session (part B) and return the exit code of the wrapper: 0 when the result was posted.

        Fails closed: a manifest with no valid ``run`` section is the ``run`` stage, a missing key file the ``key``
        stage, so a pod that fetched its tree and cannot run anything never looks like a session that ran.
        """
        run = read_run(prepared, self._support)
        key = read_key(self._secret_dir, self._support)
        return Session(prepared, run, key, self._environ, self._support, self._clock, self._tuning).run()


def _main(argv, environ, progress, seams):
    _require(len(argv) == 1, "argv")
    say("started")
    progress.stage = "import"
    support = load_support()
    pc = support.pc
    worker = Worker(
        environ,
        support,
        progress,
        net=seams.get("net") or Net(),
        clock=seams.get("clock") or SystemClock(),
        lchown=seams.get("lchown") or os.lchown,
        root=seams.get("root") or pc.SCRATCH_DIR,
        identity=seams.get("identity") or (pc.WORKER_UID, pc.WORKER_GID),
        secret_dir=seams.get("secret_dir") or pc.SESSION_SECRET_DIR,
        tuning=seams.get("tuning") or Tuning(),
    )
    prepared = worker.prepare()
    progress.stage = "run"
    code = (seams.get("runner") or worker.execute)(prepared)
    # The exit code is a number; anything else (text a library would print at exit) is a failure of this stage.
    _require(isinstance(code, int) and not isinstance(code, bool) and 0 <= code <= 255, "run")
    return code


def main(argv=None, environ=None, **seams):
    """Run part A of a session and return the exit code; every exception is one fixed line and a fixed code.

    The keyword arguments are test seams (``net``, ``clock``, ``lchown``, ``root``, ``identity``,
    ``secret_dir``, ``tuning`` and ``runner``); the program itself passes none.
    """
    progress = Progress()
    with _quiet():
        try:
            return _main(sys.argv if argv is None else argv, os.environ if environ is None else environ, progress, seams)
        except Failure as failure:
            return fail(failure.stage)
        except BaseException:  # every other exception, SystemExit and KeyboardInterrupt included: one fixed line
            return fail(progress.stage)


if __name__ == "__main__":
    sys.exit(main())

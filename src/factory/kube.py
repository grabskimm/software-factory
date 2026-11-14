"""A standard-library Kubernetes client for the pod launcher (slice 7 of the pod backend, issue #145).

What the launcher needs from the API and nothing more: create a pod, get a pod, list pods by label
selector, delete a pod (with a grace period), create a Secret and delete a Secret, in ONE namespace
(the worker namespace of ``podcontract``). There is no ``watch`` (callers poll), no ``pods/log``,
no ``exec`` and no Secret read: the Role of the launcher has none of those verbs either
(``docs/pod-backend.md``). Nothing imports this module yet and no pod is ever launched by it; the
backend is off by default.

CONTRACT-TESTED AGAINST ASSUMED SHAPES. No cluster was consulted. The request paths, the
``DeleteOptions`` body, the Status shapes of the error answers and the way a denial of the
admission policy reaches the client (a 422 whose message carries the rule ids in square brackets) are
the documented Kubernetes API as the author knows it, checked only against the in-memory fake of
``tests/fake_kube.py`` over a local HTTP server. A recording from a real cluster replaces them
(slice 22); until then each one is UNVERIFIED.

Rules of the client:

* ``http.client`` and ``ssl`` only (the layer beneath ``urllib``, because urllib has ONE timeout and
  the contract has two: connect and read; it also never follows a redirect, so the token cannot
  travel to another host). The API host comes from the in-pod environment
  (``KUBERNETES_SERVICE_HOST`` and ``KUBERNETES_SERVICE_PORT``, the names of the contract's canary),
  the CA from the standard in-pod file, and the service account token is RE-READ ON EVERY REQUEST
  (projected tokens rotate). Plain ``http`` is refused for any host but loopback, so a token is never
  sent in the clear (the loopback case exists for the tests only).
* The token file is read as a regular file of bounded size: opened without blocking (a FIFO cannot
  hang the read), checked with ``fstat``, size-capped, and the text must be bearer-token characters.
  Symbolic links are FOLLOWED on purpose: a projected token is a symlink into a ``..data`` directory
  that the kubelet swaps atomically, so refusing links would refuse every real token. The directory is
  a read-only mount of the container, not a path another process can plant.
* Timeouts on every call: 5 s to connect and 30 s for a socket read (the contract's numbers). The read
  timeout is PER SOCKET OPERATION, so alone it would not stop a server that trickles bytes. Each attempt
  therefore also has a DEADLINE for receiving the whole answer (status line, headers and body): at most
  ``MAX_ATTEMPT_SECONDS`` after the connection was made and never later than ``MAX_CALL_SECONDS`` after the
  call began, checked on the injected clock before every receive (a receive waits at most what is left,
  and past the deadline the attempt ends as a timeout). Connecting and sending the request (at most
  1 MiB) are NOT interrupted by it: they have only the per-operation socket timeouts, so a call can run
  past 120 s by what those two phases of its last attempt take (and a host NAME is resolved without any
  timeout: the in-pod address is an IP).
* A bounded retry with backoff on 429, on 5xx and on a network failure that left the request possibly
  unsent; a ``POST`` that failed AFTER it was sent is not retried on a network error (it may have been
  applied), and ``Unavailable.outcome_unknown`` says so. A retry that answers 409 means an earlier
  attempt may have landed: ``AlreadyExists.outcome_unknown`` is then True (the object may be the caller's own). A malformed answer on the wire (a bad status
  line, a negative chunk size, non-HTTP bytes) and a 2xx body that ends before its ``Content-Length`` are
  network failures too: ``Unavailable``, retried like any other for a ``GET`` or ``DELETE``. A body
  that is delimited only by the close of the connection (no ``Content-Length``, not chunked) cannot be
  checked this way and is parsed as it came (a cut-off JSON object is a ``BadResponse``).
* 409 is ``AlreadyExists``, 404 is ``NotFound``, 401 and 403 are ``ConfigError`` (fatal, never
  retried), 422 is ``Denied`` and carries the rule ids of the admission policy and nothing else.
* Response bodies are NEVER logged, copied into an error or returned in part: every message is a fixed
  text, the log lines carry words and numbers only, and a denial yields only the rule ids that
  ``podspec.POLICY_RULES`` knows. ``create_secret`` returns the name and uid, never the data the server
  echoes back. No pod spec and no token value reaches a log or an exception.
"""

import http.client
import io
import ipaddress
import json
import logging
import os
import re
import ssl
import stat
import time
import urllib.parse
from collections.abc import Mapping

from . import podcontract as pc
from . import podspec

log = logging.getLogger("factory.kube")

# --- Where things are ---------------------------------------------------------------------------

_SERVICE_PROBE = next(probe for probe in pc.CANARY_API_PROBES if probe["name"] == "service")
SERVICE_HOST_ENV = _SERVICE_PROBE["host_env"]
SERVICE_PORT_ENV = _SERVICE_PROBE["port_env"]
# The standard in-pod location of the service account files (a Kubernetes constant, not set by
# Terraform and not in the contract). ASSUMED to be what the launcher container sees.
SERVICE_ACCOUNT_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
TOKEN_PATH = f"{SERVICE_ACCOUNT_DIR}/token"
CA_PATH = f"{SERVICE_ACCOUNT_DIR}/ca.crt"

TOKEN_MAX_BYTES = 16384
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_DENIAL_BYTES = 65536
MAX_REQUEST_BYTES = 1024 * 1024
LIST_PAGE_SIZE = 500
MAX_LIST_PAGES = 10
# One call (its retries included) starts no further attempt past this many seconds, and no attempt waits for
# an answer past them either (the read timeout is per socket operation, a deadline is not).
MAX_CALL_SECONDS = 120.0
# One attempt takes at most this long, from the connection being made, to receive the whole answer.
MAX_ATTEMPT_SECONDS = 60.0
MAX_RETRY_AFTER_SECONDS = 30
MAX_GRACE_SECONDS = 3600

_TOKEN_TEXT = re.compile(r"[A-Za-z0-9._~+/=-]{1,%d}" % TOKEN_MAX_BYTES)
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")
_NAMESPACE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
_LABEL_KEY = re.compile(r"[A-Za-z0-9]([A-Za-z0-9._/-]{0,251}[A-Za-z0-9])?")
_RETRY_AFTER = re.compile(r"[0-9]{1,4}")
# The rule id of a denial: the real policy writes `[rule:<id>]` (infra/policy/pod-policy.json); a bare `[<id>]` is the form the
# offline mirror of the policy uses. Both name an id of ``podspec.POLICY_RULES``.
_DENIED_ID = re.compile(r"\[(?:rule:)?([a-z][a-z-]{0,40})\]")
# How a ValidatingAdmissionPolicy denial reads in the Status message. It is what tells a policy's 403 from an authorisation 403.
_POLICY_DENIAL = re.compile(r"ValidatingAdmissionPolicy '[A-Za-z0-9._-]{1,253}' with binding '[A-Za-z0-9._-]{1,253}' denied request:")
_CONTINUE = re.compile(r"[\x21-\x7e]{1,4096}")
_LOOPBACK_NAMES = ("localhost",)


# --- Errors (fixed texts; nothing from a request or a response is ever in a message) -------------


class KubeError(Exception):
    """Base of every error of this client. `status` is the HTTP status when there was one."""

    kind = "error"

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class AlreadyExists(KubeError):
    """409: an object of that name exists.

    `outcome_unknown` is True for a POST of which an EARLIER attempt may have been applied (sent and dropped, or
    answered 5xx other than 503): the 409 may then be the object that attempt created, so it is possibly ours. A 409
    on the first attempt, or after attempts that were certainly not applied (429, 503, never sent), is someone else's.
    """

    kind = "already_exists"

    def __init__(self, outcome_unknown=False):
        super().__init__("kubernetes object already exists", 409)
        self.outcome_unknown = outcome_unknown


class NotFound(KubeError):
    """404: no such object (a pod that was created and is now NotFound is gone, never succeeded)."""

    kind = "not_found"

    def __init__(self):
        super().__init__("kubernetes object not found", 404)


class ConfigError(KubeError):
    """A fatal configuration error: credentials refused (401, 403), an unusable token, CA or address."""

    kind = "config"

    def __init__(self, message="kubernetes credentials or address unusable", status=None):
        super().__init__(message, status)


class Denied(KubeError):
    """The API refused the object. `rules` holds the admission rule ids and nothing else.

    The status is 422 for what the API server's own validation refuses, and 403 for a ValidatingAdmissionPolicy that denies
    (the policy's ``reason`` is ``Forbidden``): the first live run, against a real API server on kind, showed the 403, which this client had
    read as bad credentials.
    """

    kind = "denied"

    def __init__(self, rules=(), status=422):
        super().__init__("kubernetes admission denied the object", status)
        self.rules = tuple(rules)


class Rejected(KubeError):
    """Any other status that is not a success and not retried (a 400, a 405, a redirect ...)."""

    kind = "rejected"

    def __init__(self, status):
        super().__init__("kubernetes rejected the request", status)


class Unavailable(KubeError):
    """The retries are used up (429, 5xx or the network). `outcome_unknown` is True for a POST of which ANY attempt
    may have landed (sent and dropped, or answered 5xx other than 503), whatever the last attempt was."""

    kind = "unavailable"

    def __init__(self, status=None, outcome_unknown=False):
        super().__init__("kubernetes unavailable", status)
        self.outcome_unknown = outcome_unknown


class BadResponse(KubeError):
    """A success status whose body is not what the client expects (not JSON, too large, wrong shape).

    The status was 2xx, so for a create the object exists: the caller gets it by name."""

    kind = "bad_response"

    def __init__(self):
        super().__init__("unexpected kubernetes response")


class _Transport(Exception):
    """Internal: the connection failed. `sent` is True once the request may have reached the server."""

    def __init__(self, sent, word):
        super().__init__(word)
        self.sent = sent
        self.word = word


# --- Validation helpers ----------------------------------------------------------------------------


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)  # a bool is not a number here


def _pod_name(name):
    pc.secret_name(name)  # ValueError("invalid pod name") for anything but a worker pod name
    return name


def _secret_name(name):
    suffix = pc.SECRET_NAME_SUFFIX
    if not isinstance(name, str) or not name.endswith(suffix) or pc.secret_name(name[: -len(suffix)]) != name:
        raise ValueError("invalid secret name")
    return name


def _selector(selector):
    """`key=value,...` from a mapping of label keys to label values (equality only), else ValueError."""
    if not isinstance(selector, Mapping) or not selector:
        raise ValueError("invalid selector")
    parts = []
    for key in sorted(selector):
        value = selector[key]
        if not isinstance(key, str) or not _LABEL_KEY.fullmatch(key):
            raise ValueError("invalid selector")
        pc.validate_label_value(value)
        parts.append(f"{key}={value}")
    return ",".join(parts)


def _manifest(obj, kind, namespace, name_of):
    """The name of a manifest that is a `kind` object of this client's namespace, else ValueError."""
    if not isinstance(obj, Mapping) or obj.get("kind") != kind or obj.get("apiVersion") != "v1":
        raise ValueError(f"invalid {kind.lower()}")
    meta = obj.get("metadata")
    if not isinstance(meta, Mapping) or meta.get("namespace") != namespace:
        raise ValueError(f"invalid {kind.lower()}")
    return name_of(meta.get("name"))


def _body(obj):
    try:
        payload = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise ValueError("invalid manifest") from None
    if len(payload) > MAX_REQUEST_BYTES:
        raise ValueError("manifest too large")
    return payload


def _retry_after(value):
    if isinstance(value, str) and _RETRY_AFTER.fullmatch(value):
        return min(int(value), MAX_RETRY_AFTER_SECONDS)
    return 0


def _is_policy_denial(data):
    """Whether a 403 body is the Status of a ValidatingAdmissionPolicy denial (and not an authorisation failure)."""
    try:
        status = json.loads(data or b"")
    except (ValueError, RecursionError):
        return False
    message = status.get("message") if isinstance(status, dict) else None
    return isinstance(message, str) and _POLICY_DENIAL.search(message[:4096]) is not None


def _denied_rules(data):
    """The admission rule ids a 422 Status names, in contract order: only ids of POLICY_RULES, only as `[id]`."""
    try:
        status = json.loads(data)
    except (ValueError, RecursionError):
        return ()
    if not isinstance(status, dict):
        return ()
    texts = [status.get("message")]
    details = status.get("details")
    causes = details.get("causes") if isinstance(details, dict) else None
    if isinstance(causes, list):
        texts.extend(cause.get("message") for cause in causes[:16] if isinstance(cause, dict))
    found = set()
    for text in texts:
        if isinstance(text, str):
            found.update(_DENIED_ID.findall(text[:4096]))
    return tuple(rule for rule in podspec.POLICY_RULES if rule in found)


def _base_url(host, port):
    if not isinstance(host, str) or not isinstance(port, str) or not re.fullmatch(r"[0-9]{1,5}", port):
        raise ConfigError("kubernetes address missing or malformed")
    if not 1 <= int(port) <= 65535:
        raise ConfigError("kubernetes address missing or malformed")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if not _HOST.fullmatch(host):
            raise ConfigError("kubernetes address missing or malformed") from None
        return f"https://{host}:{port}"
    return f"https://[{address}]:{port}" if address.version == 6 else f"https://{address}:{port}"


def _is_loopback(host):
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _default_connection(scheme, host, port, timeout, context):
    if scheme == "https":
        return http.client.HTTPSConnection(host, port, timeout=timeout, context=context)
    return http.client.HTTPConnection(host, port, timeout=timeout)


class _DeadlineReader(io.RawIOBase):
    """The read side of one connection: every receive waits at most the read timeout and never past the deadline."""

    def __init__(self, raw, sock, clock, limit, read_timeout):
        super().__init__()
        self._raw = raw
        self._sock = sock
        self._clock = clock
        self._limit = limit
        self._read_timeout = read_timeout

    def readable(self):
        return True

    def readinto(self, buffer):
        left = self._limit - self._clock()
        if left <= 0:
            raise TimeoutError("attempt deadline passed")
        self._sock.settimeout(min(self._read_timeout, left))
        return self._raw.readinto(buffer)

    def close(self):
        try:
            self._raw.close()
        finally:
            super().close()


class _DeadlineSocket:
    """A connected socket whose reads keep a deadline: the status line, the headers and the body of an answer.

    `http.client` reads an answer only through `makefile("rb")`; everything else is the socket's own. The reader is
    made by the socket itself, so closing the connection while the answer is still being read stays safe.
    """

    def __init__(self, sock, clock, limit, read_timeout):
        self._sock = sock
        self._clock = clock
        self._limit = limit
        self._read_timeout = read_timeout

    def makefile(self, mode="rb", buffering=None):
        if mode != "rb":
            raise ValueError("the read side only")
        raw = self._sock.makefile("rb", buffering=0)
        return io.BufferedReader(_DeadlineReader(raw, self._sock, self._clock, self._limit, self._read_timeout))

    def __getattr__(self, name):
        return getattr(self._sock, name)


# --- The client ---------------------------------------------------------------------------------------


class Client:
    """The six operations of the launcher against the API of one namespace.

    `base_url` is `https://host:port` (plain `http` only for a loopback host, for tests). `sleep` and
    `clock` are injectable (a test never really sleeps). `connection_factory(scheme, host, port,
    connect_timeout, context)` returns an `http.client` connection (a test records the timeouts).
    """

    def __init__(
        self,
        base_url,
        *,
        namespace=pc.WORKER_NAMESPACE,
        token_path=TOKEN_PATH,
        ca_path=CA_PATH,
        ssl_context=None,
        connect_timeout=pc.KUBE_CONNECT_TIMEOUT_SECONDS,
        read_timeout=pc.KUBE_READ_TIMEOUT_SECONDS,
        retries=pc.KUBE_RETRIES,
        backoff=pc.KUBE_RETRY_BACKOFF_SECONDS,
        sleep=time.sleep,
        clock=time.monotonic,
        connection_factory=_default_connection,
    ):
        if not isinstance(namespace, str) or not _NAMESPACE.fullmatch(namespace):
            raise ValueError("invalid namespace")
        if not (_is_int(retries) and 0 <= retries <= 10) or not backoff or not all(_is_int(b) and b >= 0 for b in backoff):
            raise ValueError("invalid retry bounds")
        if not all(isinstance(t, (int, float)) and not isinstance(t, bool) and t > 0 for t in (connect_timeout, read_timeout)):
            raise ValueError("invalid timeout")
        parts = urllib.parse.urlsplit(base_url if isinstance(base_url, str) else "")
        try:
            port = parts.port
        except ValueError:
            port = None
        host = parts.hostname
        if parts.scheme not in ("https", "http") or not host or parts.path not in ("", "/") or parts.query or parts.fragment or parts.username:
            raise ConfigError("kubernetes address missing or malformed")
        if parts.scheme == "http" and not _is_loopback(host):
            raise ConfigError("plain http to a non-loopback host refused")
        self._scheme = parts.scheme
        self._host = host
        self._port = port or (443 if parts.scheme == "https" else 80)
        self._namespace = namespace
        self._token_path = token_path
        self._context = None
        if parts.scheme == "https":
            self._context = ssl_context or self._load_context(ca_path)
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._retries = retries
        self._backoff = tuple(backoff)
        self._sleep = sleep
        self._clock = clock
        self._factory = connection_factory

    @classmethod
    def in_cluster(cls, environ=None, **options):
        """A client for the API server named by the in-pod environment (the CA and token at the standard paths)."""
        env = os.environ if environ is None else environ
        base = _base_url(env.get(SERVICE_HOST_ENV), env.get(SERVICE_PORT_ENV))
        return cls(base, **options)

    @staticmethod
    def _load_context(ca_path):
        try:
            return ssl.create_default_context(cafile=ca_path)
        except (OSError, ssl.SSLError):
            raise ConfigError("kubernetes CA file unusable") from None

    # -- paths ---------------------------------------------------------------------------------------

    def _collection(self, resource):
        return f"/api/v1/namespaces/{urllib.parse.quote(self._namespace, safe='')}/{resource}"

    def _item(self, resource, name):
        return f"{self._collection(resource)}/{urllib.parse.quote(name, safe='')}"

    # -- operations ------------------------------------------------------------------------------------

    def create_pod(self, pod):
        """Create a pod from a manifest of this namespace; returns the pod as the API answered (it has the uid)."""
        _manifest(pod, "Pod", self._namespace, _pod_name)
        return self._call("POST", self._collection("pods"), body=_body(pod))

    def get_pod(self, name):
        """The pod by name; `NotFound` when it does not exist (after creation that means gone)."""
        return self._call("GET", self._item("pods", _pod_name(name)))

    def list_pods(self, selector):
        """The pods whose labels equal every pair of the `selector` mapping (a bounded number of pages)."""
        query = {"labelSelector": _selector(selector), "limit": str(LIST_PAGE_SIZE)}
        items = []
        for _page in range(MAX_LIST_PAGES):
            page = self._call("GET", self._collection("pods"), query=query)
            found = page.get("items")
            if not isinstance(found, list) or not all(isinstance(item, dict) for item in found):
                raise BadResponse()
            items.extend(found)
            meta = page.get("metadata")
            more = meta.get("continue") if isinstance(meta, dict) else None
            if not more:
                return items
            if not isinstance(more, str) or not _CONTINUE.fullmatch(more):
                raise BadResponse()
            query = {**query, "continue": more}
        raise BadResponse()

    def delete_pod(self, name, grace_seconds=pc.TERMINATION_GRACE_SECONDS):
        """Delete a pod with a grace period (the contract's by default); the Secret goes with it (owner reference)."""
        _pod_name(name)
        if not _is_int(grace_seconds) or not 0 <= grace_seconds <= MAX_GRACE_SECONDS:
            raise ValueError("invalid grace period")
        options = {
            "apiVersion": "v1",
            "kind": "DeleteOptions",
            "gracePeriodSeconds": grace_seconds,
            "propagationPolicy": "Background",
        }
        return self._call("DELETE", self._item("pods", name), body=_body(options))

    def create_secret(self, secret):
        """Create a Secret; returns `{"name", "uid"}` only (the API echoes the data back: it is dropped here)."""
        name = _manifest(secret, "Secret", self._namespace, _secret_name)
        created = self._call("POST", self._collection("secrets"), body=_body(secret))
        meta = created.get("metadata")
        uid = meta.get("uid") if isinstance(meta, dict) else None
        if not isinstance(uid, str):
            raise BadResponse()
        return {"name": name, "uid": uid}

    def delete_secret(self, name):
        """Delete a Secret by name (it is normally garbage-collected with its pod)."""
        return self._call("DELETE", self._item("secrets", _secret_name(name)))

    # -- transport ---------------------------------------------------------------------------------------

    def _token(self):
        """The service account token, read afresh: a regular file of bounded size holding bearer-token text."""
        try:
            fd = os.open(self._token_path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError:
            raise ConfigError("kubernetes token unreadable") from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > TOKEN_MAX_BYTES:
                raise ConfigError("kubernetes token unreadable")
            raw = os.read(fd, TOKEN_MAX_BYTES + 1)
        except OSError:
            raise ConfigError("kubernetes token unreadable") from None
        finally:
            os.close(fd)
        try:
            text = raw.decode("ascii").strip()
        except UnicodeDecodeError:
            raise ConfigError("kubernetes token malformed") from None
        if not _TOKEN_TEXT.fullmatch(text):
            raise ConfigError("kubernetes token malformed")
        return text

    def _attempt(self, method, target, payload, deadline):
        """One request: (status, Retry-After text, body bytes or None). The token is read for THIS request.

        The answer (status line, headers, body) must be complete by `deadline` or `MAX_ATTEMPT_SECONDS` from now."""
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "Accept": "application/json",
            "User-Agent": "factory-kube/1",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        connection = self._factory(self._scheme, self._host, self._port, self._connect_timeout, self._context)
        sent = False
        try:
            connection.connect()
            connection.sock.settimeout(self._read_timeout)
            limit = min(deadline, self._clock() + MAX_ATTEMPT_SECONDS)
            connection.sock = _DeadlineSocket(connection.sock, self._clock, limit, self._read_timeout)
            sent = True
            connection.request(method, target, body=payload, headers=headers)
            response = connection.getresponse()
            status = response.status
            return status, response.getheader("Retry-After"), self._read_body(response, status)
        except ssl.SSLCertVerificationError:
            raise ConfigError("kubernetes TLS verification failed") from None
        except TimeoutError:
            raise _Transport(sent, "timeout") from None
        except (OSError, http.client.HTTPException, ValueError):
            # ValueError: a chunked answer with a negative chunk size makes http.client raise one while the
            # body is read. IncompleteRead: a 2xx body shorter than its Content-Length (see _read_body).
            # Whatever a hostile or broken server puts on the wire ends here, as a transport failure.
            raise _Transport(sent, "network") from None
        finally:
            connection.close()

    @staticmethod
    def _read_body(response, status):
        """The body of a 2xx (bounded), the start of the body of a 422 or a 403, else None.

        `read(n)` does not raise when the server closes the connection before `Content-Length` bytes came: it
        returns what there is. `response.length` is what was announced and is still to come: a 2xx object cut
        short is a failed transport, not a malformed object (and for a POST it may have been applied).
        """
        if 200 <= status < 300:
            data = response.read(MAX_RESPONSE_BYTES + 1)
            if len(data) <= MAX_RESPONSE_BYTES and response.length:
                raise http.client.IncompleteRead(data, response.length)
            return data
        if status in (403, 422):
            return response.read(MAX_DENIAL_BYTES)
        return None

    def _call(self, method, path, query=None, body=None):
        target = path if not query else f"{path}?{urllib.parse.urlencode(query)}"
        started = self._clock()
        deadline = started + MAX_CALL_SECONDS
        attempt = 0
        # Sticky across the attempts: one POST that may have been applied taints the whole call, even when
        # a later attempt ends on a 429, a 503 or a connection that never sent anything.
        unknown = False
        while True:
            try:
                status, retry_after, data = self._attempt(method, target, body, deadline)
            except _Transport as failure:
                # An unsent request is always safe to repeat; a POST that was sent may have been applied.
                word, status, retry_after = failure.word, None, None
                retryable = not failure.sent or method != "POST"
                unknown = unknown or (failure.sent and method == "POST")
            else:
                if 200 <= status < 300:
                    return self._parse(data)
                if status != 429 and status < 500:
                    self._raise_final(status, data, unknown)
                word, retryable = status, True
                unknown = unknown or (method == "POST" and status not in (429, 503))
            delay = max(self._backoff[min(attempt, len(self._backoff) - 1)], _retry_after(retry_after))
            if not retryable or attempt >= self._retries or self._clock() + delay > deadline:
                raise Unavailable(status, outcome_unknown=unknown)
            attempt += 1
            log.warning("kubernetes %s request failed (%s), retry %d of %d", method, word, attempt, self._retries)
            self._sleep(delay)

    @staticmethod
    def _parse(data):
        if data is None or len(data) > MAX_RESPONSE_BYTES:
            raise BadResponse()
        try:
            parsed = json.loads(data)
        except (ValueError, RecursionError):
            raise BadResponse() from None
        if not isinstance(parsed, dict):
            raise BadResponse()
        return parsed

    @staticmethod
    def _raise_final(status, data, unknown=False):
        """Raise the error of a status that is neither a success nor retryable (always raises).

        `unknown` is the sticky flag of `_call`: a 409 that follows a possibly-applied POST keeps it."""
        if status == 403 and _is_policy_denial(data):
            raise Denied(_denied_rules(data), 403)
        if status in (401, 403):
            raise ConfigError("kubernetes credentials refused", status)
        if status == 404:
            raise NotFound()
        if status == 409:
            raise AlreadyExists(outcome_unknown=unknown)
        if status == 422:
            raise Denied(_denied_rules(data or b""))
        raise Rejected(status)

"""In-process fake of the slice of the Azure Blob REST API that factory.locks uses.

It binds 127.0.0.1 only and keeps lease time on an injectable fake clock, so tests
can expire, renew and break leases by advancing time instead of sleeping. The status
codes follow the documented service: lease operations that conflict return 409,
blob operations with a wrong or missing lease id return 412.

Supported: Put Blob (If-None-Match: *), Lease Blob (acquire/renew/release/break),
Set Blob Metadata, Get Blob Properties (HEAD/GET). Anything else is a 400.
"""

import contextlib
import http.server
import socket
import threading
import urllib.parse
import uuid
from dataclasses import dataclass, field


class FakeClock:
    """Monotonic-seconds stand-in shared by the fake server and the lock under test."""

    def __init__(self, start=1000.0):
        self._now = float(start)
        self._mutex = threading.Lock()

    def __call__(self):
        with self._mutex:
            return self._now

    def advance(self, seconds):
        with self._mutex:
            self._now += seconds
            return self._now


@dataclass
class Blob:
    metadata: dict = field(default_factory=dict)
    state: str = "available"  # available | leased | expired | breaking | broken
    lease_id: str | None = None
    duration: int = 0
    expiry: float = 0.0
    break_deadline: float = 0.0


@dataclass
class Record:
    """One request as the server saw it."""

    method: str
    path: str
    op: str
    headers: dict  # lower-cased names
    status: int | None = None  # None: connection dropped without a reply
    injected: bool = False
    error_code: str = ""

    @property
    def lease_id(self):
        return self.headers.get("x-ms-lease-id")


class FakeBlobServer:
    VERSION = "2023-11-03"

    def __init__(self, clock=None, tokens=None, containers=("locks",), exists_status=409):
        self.clock = clock or FakeClock()
        self.tokens = set(tokens) if tokens else None  # None: any non-empty bearer token
        self.containers = set(containers)
        self.exists_status = exists_status  # what Put Blob with If-None-Match: * answers
        self.blobs: dict[tuple[str, str], Blob] = {}
        self.requests: list[Record] = []
        self.partitioned = False
        self._rules: list[dict] = []
        self._mutex = threading.RLock()
        self._httpd = None
        self._thread = None

    # -- lifecycle ----------------------------------------------------------------

    def start(self):
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence the default stderr access log
                pass

            def do_PUT(self):
                server._handle(self)

            do_HEAD = do_GET = do_DELETE = do_PUT

        class Httpd(http.server.ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        self._httpd = Httpd(("127.0.0.1", 0), Handler)
        self._port = self._httpd.server_address[1]  # stays valid after stop(): a dead endpoint
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self._thread.start()
        return self

    def stop(self):
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

    # -- test controls ------------------------------------------------------------

    def blob(self, container, name):
        return self.blobs.get((container, name))

    def inject(self, op, times=1, status=503):
        """Fail the next `times` requests of `op` ('any' matches all). status=None drops
        the connection, which clients see as a network error."""
        with self._mutex:
            self._rules.append({"op": op, "times": times, "status": status})

    def break_lease(self, container, name):
        """Server-side break (period 0): the lease is gone and anyone may acquire."""
        with self._mutex:
            self.blobs[(container, name)].state = "broken"

    def delete(self, container, name):
        with self._mutex:
            self.blobs.pop((container, name), None)

    def calls(self, op=None):
        with self._mutex:
            return [r for r in self.requests if op is None or r.op == op]

    def ops(self):
        return [r.op for r in self.calls()]

    # -- request handling ---------------------------------------------------------

    def _handle(self, handler):
        length = int(handler.headers.get("Content-Length") or 0)
        if length:
            handler.rfile.read(length)  # drain; the lock never sends bodies
        parts = urllib.parse.urlsplit(handler.path)
        query = urllib.parse.parse_qs(parts.query)
        headers = {k.lower(): v for k, v in handler.headers.items()}
        comp = (query.get("comp") or [None])[0]
        op = self._op(handler.command, comp, headers)
        record = Record(handler.command, parts.path, op, headers)
        with self._mutex:
            self.requests.append(record)
            fault = self._fault(op)
            if self.partitioned:
                fault = {"status": None}
            if fault is not None:
                record.injected = True
                self._reply(handler, record, fault["status"])
                return
            status, extra = self._dispatch(handler.command, parts.path, op, headers)
            self._reply(handler, record, status, extra)

    @staticmethod
    def _op(method, comp, headers):
        if method == "PUT" and comp is None:
            return "put_blob"
        if method == "PUT" and comp == "lease":
            return headers.get("x-ms-lease-action", "lease")
        if method == "PUT" and comp == "metadata":
            return "metadata"
        if method in ("HEAD", "GET") and comp is None:
            return "properties"
        return "unsupported"

    def _fault(self, op):
        for rule in self._rules:
            if rule["op"] in (op, "any") and rule["times"] > 0:
                rule["times"] -= 1
                return rule
        return None

    def _reply(self, handler, record, status, extra=None):
        record.status = status
        if status is None:
            handler.close_connection = True
            # The client may already have gone; a dropped connection is the point.
            with contextlib.suppress(OSError):
                handler.connection.shutdown(socket.SHUT_RDWR)
            return
        error = (extra or {}).pop("error", "")
        record.error_code = error
        handler.send_response(status)
        handler.send_header("Content-Length", "0")
        handler.send_header("x-ms-request-id", str(uuid.uuid4()))
        if error:
            handler.send_header("x-ms-error-code", error)
        for name, value in (extra or {}).items():
            handler.send_header(name, value)
        handler.end_headers()

    def _fail(self, status, code):
        return status, {"error": code}

    def _dispatch(self, method, path, op, headers):
        auth = headers.get("authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if not token or (self.tokens is not None and token not in self.tokens):
            return self._fail(401, "AuthenticationFailed")
        if headers.get("x-ms-version") != self.VERSION:
            return self._fail(400, "InvalidHeaderValue")
        segments = [urllib.parse.unquote(s) for s in path.strip("/").split("/")]
        if len(segments) != 2 or op == "unsupported":
            return self._fail(400, "UnsupportedHeader")
        container, name = segments
        if container not in self.containers:
            return self._fail(404, "ContainerNotFound")
        now = self.clock()
        blob = self.blobs.get((container, name))
        if blob is not None:
            self._age(blob, now)
        if op == "put_blob":
            return self._put_blob(container, name, blob, headers)
        if blob is None:
            return self._fail(404, "BlobNotFound")
        if op == "properties":
            return self._properties(blob, headers)
        if op == "metadata":
            return self._metadata(blob, headers)
        return self._lease(op, blob, headers, now)

    @staticmethod
    def _age(blob, now):
        if blob.state == "leased" and blob.duration != -1 and now >= blob.expiry:
            blob.state = "expired"
        if blob.state == "breaking" and now >= blob.break_deadline:
            blob.state = "broken"

    @staticmethod
    def _active(blob):
        return blob.state in ("leased", "breaking")

    def _put_blob(self, container, name, blob, headers):
        if headers.get("x-ms-blob-type") != "BlockBlob":
            return self._fail(400, "MissingRequiredHeader")
        if blob is None:
            self.blobs[(container, name)] = Blob()
            return 201, {}
        if headers.get("if-none-match") == "*":
            code = "BlobAlreadyExists" if self.exists_status == 409 else "ConditionNotMet"
            return self._fail(self.exists_status, code)
        refusal = self._blob_lease_check(blob, headers)
        if refusal:
            return refusal
        blob.metadata = {}  # a real overwrite resets metadata (that is why locks never overwrite)
        return 201, {}

    def _blob_lease_check(self, blob, headers):
        """Lease rules for blob operations (write): wrong or missing id is a 412."""
        given = headers.get("x-ms-lease-id")
        if self._active(blob):
            if given is None:
                return self._fail(412, "LeaseIdMissing")
            if given != blob.lease_id:
                return self._fail(412, "LeaseIdMismatchWithBlobOperation")
            return None
        if given is not None:
            code = "LeaseLost" if blob.state == "expired" else "LeaseNotPresentWithBlobOperation"
            return self._fail(412, code)
        return None

    def _properties(self, blob, headers):
        given = headers.get("x-ms-lease-id")
        if given is not None and (not self._active(blob) or given != blob.lease_id):
            return self._fail(412, "LeaseIdMismatchWithBlobOperation")
        extra = {f"x-ms-meta-{k}": v for k, v in blob.metadata.items()}
        extra["x-ms-lease-state"] = blob.state
        return 200, extra

    def _metadata(self, blob, headers):
        refusal = self._blob_lease_check(blob, headers)
        if refusal:
            return refusal
        blob.metadata = {
            k[len("x-ms-meta-"):]: v for k, v in headers.items() if k.startswith("x-ms-meta-")
        }
        return 200, {}

    def _lease(self, action, blob, headers, now):
        given = headers.get("x-ms-lease-id")
        if action == "acquire":
            duration = int(headers.get("x-ms-lease-duration", "0"))
            if duration != -1 and not 15 <= duration <= 60:
                return self._fail(400, "InvalidHeaderValue")
            proposed = headers.get("x-ms-proposed-lease-id") or str(uuid.uuid4())
            if self._active(blob) and proposed != blob.lease_id:
                code = (
                    "LeaseAlreadyPresent"
                    if blob.state == "leased"
                    else "LeaseIsBreakingAndCannotBeAcquired"
                )
                return self._fail(409, code)
            blob.state, blob.lease_id = "leased", proposed
            blob.duration, blob.expiry = duration, now + duration
            return 201, {"x-ms-lease-id": proposed}
        if action == "break":
            if not self._active(blob):
                return self._fail(409, "LeaseNotPresentWithLeaseOperation")
            blob.state = "broken"
            return 202, {"x-ms-lease-time": "0"}
        if action not in ("renew", "release"):
            return self._fail(400, "UnsupportedHeader")
        if given is None:
            return self._fail(400, "MissingRequiredHeader")
        if blob.state == "available":
            return self._fail(409, "LeaseNotPresentWithLeaseOperation")
        if given != blob.lease_id:
            return self._fail(409, "LeaseIdMismatchWithLeaseOperation")
        if action == "release":
            blob.state, blob.lease_id = "available", None
            return 200, {}
        if blob.state == "breaking":
            return self._fail(409, "LeaseIsBreakingAndCannotBeRenewed")
        if blob.state == "broken":
            return self._fail(409, "LeaseIsBrokenAndCannotBeRenewed")
        # Leased, or expired but not re-leased by anyone else: the owner may renew.
        blob.state, blob.expiry = "leased", now + blob.duration
        return 200, {"x-ms-lease-id": blob.lease_id}

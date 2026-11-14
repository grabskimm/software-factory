"""Repository locks: exactly one supervisor per repo, on one host or across replicas.

`LocalFlockLock` is the single-host lock (a non-blocking `fcntl.flock`).
`BlobLeaseLock` extends the guarantee across replicas with an Azure Blob lease:
a daemon thread renews it, and a fencing counter in the blob metadata orders
holders. A holder that is paused or partitioned past its lease must never act
afterwards, so the supervisor calls `assert_held()` right before every external
side effect (git push, PR create/edit, commit status) and the process exits (after
killing recorded children) once the lease is lost.

Stdlib only. Tokens are never written to disk, URLs, argv, logs or exception text.

Identity for the blob requests comes from one of two token sources, chosen by
`lock_from_env`: `WorkloadIdentityToken` (AKS: a projected service-account token
exchanged at the Microsoft Entra v2 token endpoint) or `ManagedIdentityToken` (Azure
Container Apps identity endpoint, or IMDS on a VM).
"""

from __future__ import annotations

import abc
import contextlib
import fcntl
import http.client
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, NamedTuple

log = logging.getLogger("factory.locks")

API_VERSION = "2023-11-03"
EXIT_LOST = 75  # EX_TEMPFAIL: tells the platform to restart and re-acquire later
BUSY_MESSAGE = "another factory supervisor holds the repository lock"

# assert_held() refuses once the last proven renewal is this close to expiry: the
# action that follows (a push) takes time and clocks are imperfect.
SAFETY_MARGIN = 10.0
# Younger proof than this is trusted without a round trip, keeping assert_held cheap.
SYNC_RENEW_AFTER = 5.0
REQUEST_TIMEOUT = 5.0  # seconds per HTTP call, so detecting loss stays well inside a lease
KILL_HOOK_TIMEOUT = 10.0  # a hung hook must never keep a stale holder alive

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_CONTAINER = re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]")
_BLOB = re.compile(r"[A-Za-z0-9._-]{1,200}")
_TOKEN = re.compile(r"[A-Za-z0-9._~+/=-]{1,8192}")  # base64url/JWT; rejects header injection
_ERROR_CODE = re.compile(r"[A-Za-z0-9]{1,64}")
_DIGITS = re.compile(r"[0-9]{1,15}")


class LockError(RuntimeError):
    """Base class so callers can catch every lock failure with one clause."""


class LockBusy(LockError):
    """Another holder has the lock."""


class LockLost(LockError):
    """This process can no longer prove that it holds the lock."""


class LockUnavailable(LockError):
    """The lock service could not be used (network, auth, 5xx). Nothing is held."""


class LockConfigError(LockError, ValueError):
    """The lock environment is unusable or ambiguous. The text names variables, never values."""


# --- kill hooks and the default on_lost -----------------------------------------

_kill_hooks: list[Callable[[], None]] = []
_hooks_mutex = threading.Lock()


def register_kill_hook(fn: Callable[[], None]) -> Callable[[], None]:
    """Run `fn` (kill recorded child process groups) when a lease is lost."""
    with _hooks_mutex:
        if fn not in _kill_hooks:
            _kill_hooks.append(fn)
    return fn


def unregister_kill_hook(fn: Callable[[], None]) -> None:
    with _hooks_mutex:
        if fn in _kill_hooks:
            _kill_hooks.remove(fn)


def run_kill_hooks(timeout: float | None = None) -> None:
    """Run every hook in its own thread; a failing or hung hook blocks no other."""
    timeout = KILL_HOOK_TIMEOUT if timeout is None else timeout
    with _hooks_mutex:
        hooks = list(_kill_hooks)
    threads = []
    for hook in hooks:
        thread = threading.Thread(
            target=_run_hook, args=(hook,), name="factory-kill-hook", daemon=True
        )
        thread.start()
        threads.append(thread)
    deadline = time.monotonic() + timeout
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))


def _run_hook(hook: Callable[[], None]) -> None:
    try:
        hook()
    except Exception as error:  # the process is about to die; never skip the exit
        log.error("kill hook failed (%s)", type(error).__name__)


def exit_process_on_lost() -> None:
    """Default on_lost: kill children, then exit so a stale holder cannot act."""
    try:
        run_kill_hooks()
    finally:  # even if a hook thread cannot start, a stale holder must not survive
        os._exit(EXIT_LOST)


# --- interface ---------------------------------------------------------------------


class RepoLock(abc.ABC):
    """acquire/renew/assert_held/release plus a monotonically increasing fencing token."""

    fencing_token: int = 0

    @abc.abstractmethod
    def acquire(self) -> "RepoLock":
        """Take the lock without blocking; raise LockBusy if someone else holds it."""

    @abc.abstractmethod
    def renew(self) -> None:
        """Prove (and extend) the hold now; raise LockLost if it is gone."""

    @abc.abstractmethod
    def assert_held(self) -> None:
        """Cheap, honest check before an external side effect; raise LockLost."""

    @abc.abstractmethod
    def release(self) -> None:
        """Give the lock up. Idempotent and never raises."""

    def __enter__(self) -> "RepoLock":
        return self.acquire()

    def __exit__(self, *exc_info: object) -> None:
        self.release()


class LocalFlockLock(RepoLock):
    """The original behaviour of state.lock(): flock on a file, fencing token 0."""

    fencing_token = 0

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._handle = None
        self._mutex = threading.Lock()

    def acquire(self) -> "LocalFlockLock":
        with self._mutex:
            if self._handle is not None:
                return self
            handle = self.path.open("a")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                raise LockBusy(BUSY_MESSAGE) from None
            except BaseException:
                handle.close()
                raise
            if not self._is_path(handle):
                # The file was unlinked between open and flock; this inode is not the lock.
                handle.close()
                raise LockBusy("repository lock file was replaced while locking") from None
            self._handle = handle
        return self

    def _is_path(self, handle) -> bool:
        try:
            held, named = os.fstat(handle.fileno()), os.stat(self.path)
        except OSError:
            return False
        return (held.st_dev, held.st_ino) == (named.st_dev, named.st_ino)

    def renew(self) -> None:
        self.assert_held()  # flock never expires; renewing is just re-checking

    def assert_held(self) -> None:
        with self._mutex:
            if self._handle is None:
                raise LockLost("repository lock is not held")
            if not self._is_path(self._handle):
                # Someone removed or replaced the file: a second supervisor could lock a new one.
                raise LockLost("repository lock file was removed or replaced")

    def release(self) -> None:
        with self._mutex:
            handle, self._handle = self._handle, None
        if handle is None:
            return
        # Closing the descriptor drops the flock even if the explicit unlock fails.
        with contextlib.suppress(OSError):
            fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


# --- Azure Blob lease --------------------------------------------------------------


class _Reply(NamedTuple):
    status: int
    headers: http.client.HTTPMessage | None

    def error_code(self) -> str:
        code = self.headers.get("x-ms-error-code", "") if self.headers is not None else ""
        return code if _ERROR_CODE.fullmatch(code or "") else ""

    def describe(self) -> str:
        code = self.error_code()
        return f"HTTP {self.status}" + (f" {code}" if code else "")


class _Transport(Exception):
    """Network/auth plumbing failure. The reason never contains a token."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib replays the Authorization header on redirects; never follow them."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _default_opener() -> urllib.request.OpenerDirector:
    # No proxies: IMDS and the ACA identity endpoint are link-local, and the blob
    # account is reached through the NAT gateway. No redirects: see _NoRedirect.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def _send(
    opener: urllib.request.OpenerDirector, request: urllib.request.Request, timeout: float
) -> _Reply:
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        try:
            return _Reply(error.code, error.headers)
        finally:
            error.close()
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as error:
        raise _Transport(f"request failed ({type(error).__name__})") from None
    with response:
        return _Reply(response.status, response.headers)


def _ok(reply: _Reply) -> bool:
    return 200 <= reply.status < 300


class BlobLeaseLock(RepoLock):
    """Cross-replica lock: an Azure Blob lease on `<container>/<blob_name>`.

    The fencing token is a counter in the blob metadata, incremented while holding
    the lease. Metadata writes require the lease id, so a stale holder's write is
    rejected (412) once anyone else has acquired or the lease has lapsed.
    """

    def __init__(
        self,
        account_url: str,
        container: str,
        blob_name: str,
        token_provider: Callable[[], str],
        lease_seconds: int = 60,
        renew_every: float = 15,
        max_failed_renewals: int = 2,
        on_lost: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] | None = None,
        opener: urllib.request.OpenerDirector | None = None,
        request_timeout: float = REQUEST_TIMEOUT,
    ):
        self._blob_url = _blob_url(account_url, container, blob_name)
        if not 15 <= lease_seconds <= 60:
            raise ValueError("lease_seconds must be between 15 and 60 (Azure fixed-duration lease)")
        if not renew_every > 0 or renew_every + request_timeout >= lease_seconds - SAFETY_MARGIN:
            raise ValueError("renew_every plus one request timeout must fit well inside the lease")
        if max_failed_renewals < 1:
            raise ValueError("max_failed_renewals must be at least 1")
        self._token_provider = token_provider
        self._lease_seconds = lease_seconds
        self._renew_every = renew_every
        self._max_failed = max_failed_renewals
        self._on_lost = on_lost or exit_process_on_lost
        self._clock = clock
        self._sleep = sleep  # None: an interruptible wait on the thread's stop event
        self._opener = opener or _default_opener()
        self._request_timeout = request_timeout
        # _mu guards the fields below with short critical sections. _io serialises lease
        # round trips (renew thread, assert_held, release). Order is always _io then _mu.
        self._mu = threading.Lock()
        self._io = threading.Lock()
        self._state = "idle"  # idle | held | lost | released
        self._lease_id: str | None = None
        self._last_ok = 0.0  # clock() just before the last request the server accepted
        self._failures = 0
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._lost_reason = ""
        self.fencing_token = 0

    def __repr__(self) -> str:
        return f"BlobLeaseLock({self._blob_url!r}, state={self._state!r})"

    # -- acquire -----------------------------------------------------------------

    def acquire(self) -> "BlobLeaseLock":
        with self._io:
            with self._mu:
                if self._state == "held":
                    return self
            lease_id = str(uuid.uuid4())
            sent = self._clock()  # the lease can only start after this instant
            self._acquire_lease(lease_id)
            try:
                fence = self._bump_fence(lease_id)
            except BaseException:
                self._send_release(lease_id)  # do not make the next replica wait out the lease
                raise
            stop = threading.Event()
            with self._mu:
                self._state, self._lease_id, self._last_ok = "held", lease_id, sent
                self._failures, self._lost_reason = 0, ""
                self._stop, self.fencing_token = stop, fence
            self._thread = threading.Thread(
                target=self._renew_loop,
                args=(stop,),
                name="factory-lease-renew",
                daemon=True,
            )
            self._thread.start()
        return self

    def _acquire_lease(self, lease_id: str) -> None:
        for attempt in (1, 2):
            self._create_if_missing()
            reply = self._call(
                "acquire lease",
                "PUT",
                "lease",
                {
                    "x-ms-lease-action": "acquire",
                    "x-ms-lease-duration": str(self._lease_seconds),
                    "x-ms-proposed-lease-id": lease_id,
                },
            )
            if _ok(reply):
                return
            if reply.status == 409:  # LeaseAlreadyPresent
                raise LockBusy(BUSY_MESSAGE)
            if reply.status == 404 and attempt == 1:
                continue  # the blob vanished between create and lease (purge); recreate once
            raise LockUnavailable(f"acquire lease: {reply.describe()}")

    def _create_if_missing(self) -> None:
        reply = self._call(
            "create lock blob",
            "PUT",
            None,
            {"x-ms-blob-type": "BlockBlob", "If-None-Match": "*"},
        )
        # Never overwrite: Put Blob on an existing blob would wipe its metadata (the fence).
        if not _ok(reply) and reply.status not in (409, 412):
            raise LockUnavailable(f"create lock blob: {reply.describe()}")

    def _bump_fence(self, lease_id: str) -> int:
        reply = self._call("read fencing token", "HEAD", None, {})
        if reply.status != 200:
            raise LockUnavailable(f"read fencing token: {reply.describe()}")
        raw = reply.headers.get("x-ms-meta-fence")
        if raw is None:
            current = 0
        elif _DIGITS.fullmatch(raw):
            current = int(raw)
        else:
            # Proceeding would break monotonicity; an operator must repair the blob.
            raise LockUnavailable("fencing counter in the lock blob metadata is corrupt")
        reply = self._call(
            "write fencing token",
            "PUT",
            "metadata",
            {"x-ms-lease-id": lease_id, "x-ms-meta-fence": str(current + 1)},
        )
        if reply.status in (409, 412):
            raise LockLost(f"lease lost while recording the fencing token ({reply.describe()})")
        if not _ok(reply):
            raise LockUnavailable(f"write fencing token: {reply.describe()}")
        return current + 1

    # -- renew -------------------------------------------------------------------

    def renew(self) -> None:
        """One renewal round trip. LockLost if gone; LockUnavailable if it failed but
        the failure budget (max_failed_renewals) is not yet spent."""
        self._renew_round()

    def _renew_loop(self, stop: threading.Event) -> None:
        wait = self._renew_every
        while True:
            if self._sleep is None:
                stop.wait(wait)
            else:
                self._sleep(wait)
            if stop.is_set():
                return
            started = self._clock()
            try:
                self._renew_round()
            except LockLost:
                return  # lost or released; on_lost has already fired where it applies
            except LockUnavailable:
                pass  # counted in _failures; try again next tick
            except Exception as error:
                # A bug here would silently stop renewals; fail closed instead.
                self._lose(stop, f"renewal thread failed ({type(error).__name__})")
                return
            wait = max(0.0, self._renew_every - (self._clock() - started))

    def _lose(self, generation: threading.Event, reason: str) -> None:
        with self._mu:
            if self._state != "held" or self._stop is not generation:
                return
            self._state, self._lost_reason = "lost", reason
            generation.set()
        self._notify_lost(reason)

    def _renew_round(self, only_if_stale: bool = False) -> None:
        lost: LockLost | None = None
        transient: LockUnavailable | None = None
        with self._io:
            with self._mu:
                self._require_held()
                if only_if_stale and self._clock() - self._last_ok <= SYNC_RENEW_AFTER:
                    return  # another caller proved it while we waited for _io
                lease_id, generation = self._lease_id, self._stop
            sent = self._clock()
            ok, definite, detail = self._renew_request(lease_id)
            with self._mu:
                if self._state != "held" or self._stop is not generation:
                    raise LockLost(self._lost_reason or "repository lock was released")
                if ok:
                    self._last_ok, self._failures = sent, 0
                    return
                self._failures += 1
                if definite or self._failures >= self._max_failed:
                    reason = (
                        detail
                        if definite
                        else f"{self._failures} consecutive lease renewals failed ({detail})"
                    )
                    self._state, self._lost_reason = "lost", reason
                    generation.set()
                    lost = LockLost(reason)
                else:
                    transient = LockUnavailable(
                        f"lease renewal failed ({detail}); "
                        f"{self._failures} of {self._max_failed} failures tolerated"
                    )
        if lost is not None:
            self._notify_lost(str(lost))
            raise lost
        log.warning("%s", transient)
        raise transient  # type: ignore[misc]

    def _renew_request(self, lease_id: str | None) -> tuple[bool, bool, str]:
        """Returns (renewed, definitely_lost, detail). A bug counts as a failed renewal."""
        try:
            reply = self._call_raw(
                "PUT",
                "lease",
                {"x-ms-lease-action": "renew", "x-ms-lease-id": lease_id or ""},
            )
        except _Transport as error:
            return False, False, error.reason
        except Exception as error:
            return False, False, f"unexpected {type(error).__name__}"
        if _ok(reply):
            return True, False, ""
        if reply.status in (404, 409, 412):
            # The lease is gone for good: broken, expired and re-leased, or blob deleted.
            return False, True, f"lease lost ({reply.describe()})"
        return False, False, reply.describe()

    def _notify_lost(self, reason: str) -> None:
        """Called by whoever moved the lock held -> lost, which happens at most once."""
        log.error("repository lock lost: %s", reason)
        try:
            self._on_lost()
        except Exception as error:
            log.error("on_lost failed (%s)", type(error).__name__)

    # -- assert_held -------------------------------------------------------------

    def assert_held(self) -> None:
        """Raise LockLost unless the lease is provably held with margin to spare."""
        age = self._check_local()
        if age <= SYNC_RENEW_AFTER:
            return
        try:
            self._renew_round(only_if_stale=True)
        except LockLost:
            raise
        except Exception as error:  # "fails for any reason" means it cannot be proven
            detail = error if isinstance(error, LockUnavailable) else type(error).__name__
            raise LockLost(f"cannot prove the lease is still held: {detail}") from None
        self._check_local()

    def _check_local(self) -> float:
        with self._mu:
            self._require_held()
            age = self._clock() - self._last_ok
        if age >= self._lease_seconds - SAFETY_MARGIN:
            raise LockLost(f"lease renewal is {age:.0f}s old; the lease may have expired")
        return age

    def _require_held(self) -> None:
        """Callers hold _mu."""
        if self._state == "held":
            return
        if self._state == "lost":
            raise LockLost(self._lost_reason or "repository lock was lost")
        raise LockLost(
            "repository lock was released" if self._state == "released"
            else "repository lock is not held"
        )

    # -- release -----------------------------------------------------------------

    def release(self) -> None:
        try:
            self._release()
        except Exception as error:  # release is best effort; the lease expires anyway
            log.warning("lease release failed (%s)", type(error).__name__)

    def _release(self) -> None:
        with self._mu:
            if self._state != "held":
                return  # idle/released: nothing to do; lost: the lease is not ours to release
            lease_id, stop = self._lease_id, self._stop
            self._state = "released"
            if stop is not None:
                stop.set()
        # Wait (bounded) for a renewal already in flight so its result cannot race the release.
        if self._io.acquire(timeout=self._request_timeout * 2):
            try:
                self._send_release(lease_id)
            finally:
                self._io.release()

    def _send_release(self, lease_id: str | None) -> None:
        try:
            self._call_raw(
                "PUT",
                "lease",
                {"x-ms-lease-action": "release", "x-ms-lease-id": lease_id or ""},
            )
        except Exception:
            pass  # best effort

    # -- HTTP --------------------------------------------------------------------

    def _call(self, op: str, method: str, comp: str | None, headers: Mapping[str, str]) -> _Reply:
        """Acquire-path call: a transport failure means nothing is held, so say so."""
        try:
            return self._call_raw(method, comp, headers)
        except _Transport as error:
            raise LockUnavailable(f"{op}: {error.reason}") from None

    def _call_raw(self, method: str, comp: str | None, headers: Mapping[str, str]) -> _Reply:
        failure = None
        try:
            token = self._token_provider()
        except Exception as error:
            failure = f"token provider failed ({type(error).__name__})"
        if failure is None and not (isinstance(token, str) and _TOKEN.fullmatch(token)):
            failure = "token provider returned an unusable token"
        if failure is not None:
            raise _Transport(failure)  # raised outside the except block: no chained context
        url = self._blob_url + (f"?comp={comp}" if comp else "")
        request = urllib.request.Request(url, method=method)
        request.add_header("Authorization", f"Bearer {token}")
        request.add_header("x-ms-version", API_VERSION)
        if method == "PUT":
            request.add_header("Content-Length", "0")
        for name, value in headers.items():
            request.add_header(name, value)
        return _send(self._opener, request, self._request_timeout)


def _blob_url(account_url: str, container: str, blob_name: str) -> str:
    parts = urllib.parse.urlsplit(account_url)
    loopback = parts.scheme == "http" and parts.hostname in _LOOPBACK
    if parts.scheme != "https" and not loopback:
        raise ValueError("account_url must be https (plain http only for loopback tests)")
    if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("account_url must be a bare URL with no credentials, query or fragment")
    parts.port  # noqa: B018 - raises ValueError for a malformed port
    if not _CONTAINER.fullmatch(container):
        raise ValueError("invalid blob container name")
    if not _BLOB.fullmatch(blob_name) or blob_name in {".", ".."}:
        raise ValueError("invalid lock blob name; expected <org>__<repo>")
    base = f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"
    return f"{base}/{container}/{urllib.parse.quote(blob_name, safe='')}"


# --- token sources -----------------------------------------------------------------


class TokenError(RuntimeError):
    """An identity token could not be fetched. Never contains a token or an assertion."""


class _CachedToken:
    """Shared plumbing of the token sources: one cached bearer token, one fetch at a time.

    A token is reused until 5 minutes before it expires, so renewals every 15 s do not
    hammer the identity endpoint. If a refresh fails while the cached token is still
    valid for at least STALE_FLOOR seconds, that token is served instead of failing:
    a brief identity outage then costs nothing. Call the instance to get a token string.
    """

    REFRESH_MARGIN = 300.0
    STALE_FLOOR = 30.0
    _MAX_BODY = 64 * 1024
    _SUBJECT = "identity"  # the word used in error messages
    _REQUIRE_BEARER = False

    def __init__(
        self,
        opener: urllib.request.OpenerDirector | None,
        now: Callable[[], float],
        timeout: float,
    ):
        self._opener = opener or _default_opener()
        self._now = now  # wall clock: expires_on is an absolute epoch time
        self._timeout = timeout
        self._mutex = threading.Lock()
        self._token: str | None = None
        self._expires = 0.0

    def __call__(self) -> str:
        with self._mutex:
            if self._token is not None and self._now() < self._expires - self.REFRESH_MARGIN:
                return self._token
            try:
                token, expires = self._fetch()
            except TokenError as error:
                if self._token is not None and self._now() < self._expires - self.STALE_FLOOR:
                    log.warning("%s token refresh failed, reusing the cached token: %s", self._SUBJECT, error)
                    return self._token
                raise
            self._token, self._expires = token, expires
            return token

    def _fetch(self) -> tuple[str, float]:
        raise NotImplementedError

    def _error_detail(self, error: urllib.error.HTTPError) -> str:
        """Extra, secret-free text for an HTTP error. The default adds nothing."""
        return ""

    def _exchange(self, request: urllib.request.Request) -> tuple[str, float]:
        """Send `request`; return (access_token, absolute expiry). Raises TokenError."""
        subject = self._SUBJECT
        failure = None
        body = b""
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                body = response.read(self._MAX_BODY + 1)
        except urllib.error.HTTPError as error:
            failure = f"{subject} endpoint returned HTTP {error.code}{self._error_detail(error)}"
            error.close()
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as error:
            failure = f"{subject} endpoint unreachable ({type(error).__name__})"
        if failure is not None:
            raise TokenError(failure)
        if len(body) > self._MAX_BODY:
            raise TokenError(f"{subject} response is too large")
        try:
            document = json.loads(body)
            token = document["access_token"]
        except (ValueError, KeyError, TypeError):
            raise TokenError(f"{subject} response is not a token document") from None
        if not (isinstance(token, str) and _TOKEN.fullmatch(token)):
            raise TokenError(f"{subject} returned a malformed token")
        if self._REQUIRE_BEARER:
            kind = document.get("token_type")
            if kind is not None and not (isinstance(kind, str) and kind.lower() == "bearer"):
                raise TokenError(f"{subject} returned a token that is not a bearer token")
        return token, self._expiry(document)

    def _expiry(self, document: Mapping[str, Any]) -> float:
        now = self._now()
        for key, relative in (("expires_on", False), ("expires_in", True)):
            value = str(document.get(key, ""))
            if _DIGITS.fullmatch(value):
                return now + int(value) if relative else float(value)
        return now  # no expiry information: treat as already due, so it is never reused


# --- managed identity --------------------------------------------------------------


class ManagedIdentityToken(_CachedToken):
    """Bearer tokens from the Container Apps identity endpoint or IMDS, cached."""

    _SUBJECT = "managed identity"
    _IMDS = "http://169.254.169.254/metadata/identity/oauth2/token"

    def __init__(
        self,
        resource: str = "https://storage.azure.com/",
        client_id: str | None = None,
        opener: urllib.request.OpenerDirector | None = None,
        env: Mapping[str, str] | None = None,
        now: Callable[[], float] = time.time,
        timeout: float = 5.0,
    ):
        super().__init__(opener, now, timeout)
        self._resource, self._client_id = resource, client_id
        self._env = env

    def __repr__(self) -> str:
        return "ManagedIdentityToken(<redacted>)"

    def _build_request(self) -> urllib.request.Request:
        env = os.environ if self._env is None else self._env
        endpoint, secret = env.get("IDENTITY_ENDPOINT"), env.get("IDENTITY_HEADER")
        extra = {"client_id": self._client_id} if self._client_id else {}
        if endpoint and secret:  # Azure Container Apps / App Service
            if urllib.parse.urlsplit(endpoint).scheme not in ("http", "https"):
                raise TokenError("IDENTITY_ENDPOINT is not an http(s) URL")
            query = urllib.parse.urlencode(
                {"resource": self._resource, "api-version": "2019-08-01", **extra}
            )
            url = endpoint + ("&" if "?" in endpoint else "?") + query
            request = urllib.request.Request(url, method="GET")
            request.add_header("X-IDENTITY-HEADER", secret)
            return request
        query = urllib.parse.urlencode(
            {"api-version": "2018-02-01", "resource": self._resource, **extra}
        )
        request = urllib.request.Request(f"{self._IMDS}?{query}", method="GET")
        request.add_header("Metadata", "true")
        return request

    def _fetch(self) -> tuple[str, float]:
        return self._exchange(self._build_request())


# --- workload identity (AKS) -------------------------------------------------------

STORAGE_SCOPE = "https://storage.azure.com/.default"
DEFAULT_AUTHORITY_HOST = "https://login.microsoftonline.com/"
# Hosts a client assertion may be sent to. The assertion is a credential for the
# application, so the authority is an allowlist, not a free URL.
AUTHORITY_HOSTS = frozenset(
    {
        "login.microsoftonline.com",
        "login.microsoftonline.us",
        "login.microsoftonline.de",
        "login.partner.microsoftonline.cn",
        "login.chinacloudapi.cn",
    }
)
# Tests only: also accept a loopback authority (any port, http or https). Nothing else.
ALLOW_LOOPBACK_AUTHORITY = "FACTORY_LOCK_ALLOW_LOOPBACK_AUTHORITY"
_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
_GUID = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")
_TENANT = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")  # GUID or domain name
_JWT = re.compile(r"[A-Za-z0-9_-]{1,16384}\.[A-Za-z0-9_-]{1,16384}\.[A-Za-z0-9_-]{1,16384}")
_AAD_ERROR = re.compile(r"[a-z_]{1,64}")
_AAD_CODE = re.compile(r"[0-9]{1,9}")


def validate_authority_host(value: str, allow_loopback: bool = False) -> str:
    """The normalised authority (`https://<host>/`) or LockConfigError. Never echoes `value`."""
    problem = "AZURE_AUTHORITY_HOST must be https://<a Microsoft Entra login host>/"
    try:
        parts = urllib.parse.urlsplit(value.strip())
        host, _ = parts.hostname, parts.port  # .port raises ValueError for a malformed port
    except ValueError:
        raise LockConfigError(problem) from None
    if (
        parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or parts.path not in ("", "/")
        or "@" in parts.netloc
        or "\\" in value
    ):
        raise LockConfigError(problem)
    if host in AUTHORITY_HOSTS:
        if parts.scheme != "https" or parts.port not in (None, 443):
            raise LockConfigError(problem)
        return f"https://{host}/"
    if allow_loopback and host in _LOOPBACK and parts.scheme in ("http", "https"):
        netloc = f"[{host}]" if ":" in host else host
        return f"{parts.scheme}://{netloc}{':' + str(parts.port) if parts.port else ''}/"
    raise LockConfigError(problem)


class WorkloadIdentityToken(_CachedToken):
    """Bearer tokens for Azure Storage from a Kubernetes projected service-account token.

    Microsoft Entra workload identity: the pod's projected token (a JWT whose subject is
    the service account) is sent as a client assertion to
    `<authority><tenant>/oauth2/v2.0/token` for `client_id`, which has a federated
    credential for that subject. The token file rotates, so it is read again on every
    refresh and never kept. Nothing secret is logged, persisted or put in a URL or an
    exception; the instance prints as `<redacted>`.
    """

    _SUBJECT = "workload identity"
    _REQUIRE_BEARER = True
    _MAX_ASSERTION = 16 * 1024

    def __init__(
        self,
        client_id: str,
        tenant_id: str,
        token_file: str | os.PathLike[str],
        authority_host: str = DEFAULT_AUTHORITY_HOST,
        scope: str = STORAGE_SCOPE,
        opener: urllib.request.OpenerDirector | None = None,
        now: Callable[[], float] = time.time,
        timeout: float = 5.0,
        allow_loopback_authority: bool = False,
    ):
        super().__init__(opener, now, timeout)
        if not (isinstance(client_id, str) and _GUID.fullmatch(client_id)):
            raise LockConfigError("the workload identity client id must be a GUID")
        if not (isinstance(tenant_id, str) and _TENANT.fullmatch(tenant_id)):
            raise LockConfigError("AZURE_TENANT_ID must be a tenant GUID or domain name")
        path = os.fspath(token_file)
        if not (isinstance(path, str) and path and "\0" not in path and os.path.isabs(path)):
            raise LockConfigError("AZURE_FEDERATED_TOKEN_FILE must be an absolute file path")
        if not (isinstance(scope, str) and re.fullmatch(r"[\x21-\x7e]{1,200}", scope)):
            raise LockConfigError("invalid token scope")
        authority = validate_authority_host(authority_host, allow_loopback_authority)
        self._client_id, self._token_file, self._scope = client_id, path, scope
        self._url = f"{authority}{tenant_id}/oauth2/v2.0/token"

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        client_id: str | None = None,
        **options: Any,
    ) -> "WorkloadIdentityToken":
        """Build from AZURE_CLIENT_ID, AZURE_TENANT_ID, AZURE_FEDERATED_TOKEN_FILE and
        (optional) AZURE_AUTHORITY_HOST, which the AKS workload identity webhook injects.
        `client_id` overrides AZURE_CLIENT_ID. LockConfigError names what is missing."""
        env = os.environ if env is None else env
        values = {
            "AZURE_CLIENT_ID": client_id or _text(env, "AZURE_CLIENT_ID"),
            "AZURE_TENANT_ID": _text(env, "AZURE_TENANT_ID"),
            "AZURE_FEDERATED_TOKEN_FILE": _text(env, "AZURE_FEDERATED_TOKEN_FILE"),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise LockConfigError(
                "workload identity needs " + ", ".join(missing) + " (set by the AKS workload "
                "identity webhook: pod label azure.workload.identity/use=true and a service "
                "account annotated with azure.workload.identity/client-id)"
            )
        return cls(
            values["AZURE_CLIENT_ID"],
            values["AZURE_TENANT_ID"],
            values["AZURE_FEDERATED_TOKEN_FILE"],
            authority_host=_text(env, "AZURE_AUTHORITY_HOST") or DEFAULT_AUTHORITY_HOST,
            allow_loopback_authority=_text(env, ALLOW_LOOPBACK_AUTHORITY) == "1",
            **options,
        )

    def __repr__(self) -> str:
        return "WorkloadIdentityToken(<redacted>)"

    def _read_assertion(self) -> str:
        failure = None
        data = b""
        try:
            with open(self._token_file, "rb") as handle:
                data = handle.read(self._MAX_ASSERTION + 1)
        except OSError as error:  # the path is not echoed: strerror would carry it
            failure = f"the federated token file is unreadable ({type(error).__name__})"
        if failure is None and len(data) > self._MAX_ASSERTION:
            failure = "the federated token file is too large"
        if failure is None:
            try:
                assertion = data.decode("ascii").strip()
            except UnicodeDecodeError:
                assertion = ""
            if not assertion:
                failure = "the federated token file is empty or not text"
            elif not _JWT.fullmatch(assertion):
                failure = "the federated token file does not hold a JWT"
        if failure is not None:
            raise TokenError(failure)
        return assertion

    def _build_request(self) -> urllib.request.Request:
        form = urllib.parse.urlencode(
            {
                "client_id": self._client_id,
                "scope": self._scope,
                "grant_type": "client_credentials",
                "client_assertion_type": _ASSERTION_TYPE,
                "client_assertion": self._read_assertion(),
            }
        )
        request = urllib.request.Request(self._url, data=form.encode("ascii"), method="POST")
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        request.add_header("Accept", "application/json")
        return request

    def _fetch(self) -> tuple[str, float]:
        try:
            return self._exchange(self._build_request())
        except TokenError as error:
            log.warning("workload identity token request failed: %s", error)
            raise

    def _error_detail(self, error: urllib.error.HTTPError) -> str:
        """Entra's short error code and AADSTS numbers, e.g. ` (invalid_client, AADSTS700016)`.
        Never the free-text description, which can carry ids and echoes."""
        try:
            document = json.loads(error.read(self._MAX_BODY))
            code = document.get("error")
            numbers = document.get("error_codes")
        except (ValueError, AttributeError, OSError, http.client.HTTPException):
            return ""
        parts = []
        if isinstance(code, str) and _AAD_ERROR.fullmatch(code):
            parts.append(code)
        if isinstance(numbers, list):
            valid = [
                n
                for n in numbers[:20]
                if isinstance(n, int) and not isinstance(n, bool) and _AAD_CODE.fullmatch(str(n))
            ]
            parts.extend(f"AADSTS{n}" for n in valid[:3])
        return f" ({', '.join(parts)})" if parts else ""


def _text(env: Mapping[str, str], name: str) -> str:
    return (env.get(name) or "").strip()


# --- factory -----------------------------------------------------------------------

_AUTH_MODES = ("auto", "workload-identity", "managed-identity")


def token_source_from_env(env: Mapping[str, str]) -> Callable[[], str]:
    """The identity used for the blob lease.

    FACTORY_LOCK_AUTH is `workload-identity`, `managed-identity` or `auto` (default).
    Auto picks workload identity when AZURE_FEDERATED_TOKEN_FILE is set (the AKS webhook
    injects it with AZURE_CLIENT_ID, AZURE_TENANT_ID and AZURE_AUTHORITY_HOST) and the
    managed identity endpoint otherwise. It refuses to guess when both are present
    (a federated token file plus IDENTITY_ENDPOINT and IDENTITY_HEADER), and when the
    workload identity variables are only partly there. Raises LockConfigError.
    """
    mode = _text(env, "FACTORY_LOCK_AUTH").lower() or "auto"
    if mode not in _AUTH_MODES:
        raise LockConfigError("FACTORY_LOCK_AUTH must be one of: " + ", ".join(_AUTH_MODES))
    client_id = _text(env, "FACTORY_LOCK_CLIENT_ID") or _text(env, "AZURE_CLIENT_ID") or None
    if mode == "managed-identity":
        return ManagedIdentityToken(client_id=client_id)
    if mode == "workload-identity":
        return WorkloadIdentityToken.from_env(env, client_id)
    if not _text(env, "AZURE_FEDERATED_TOKEN_FILE"):
        return ManagedIdentityToken(client_id=client_id)
    if _text(env, "IDENTITY_ENDPOINT") and _text(env, "IDENTITY_HEADER"):
        raise LockConfigError(
            "both workload identity (AZURE_FEDERATED_TOKEN_FILE) and managed identity "
            "(IDENTITY_ENDPOINT, IDENTITY_HEADER) are configured; set FACTORY_LOCK_AUTH to "
            "workload-identity or managed-identity"
        )
    return WorkloadIdentityToken.from_env(env, client_id)


def lock_from_env(
    slug: str,
    local_path: str | Path,
    env: Mapping[str, str] = os.environ,
    **blob_options: Any,
) -> RepoLock:
    """BlobLeaseLock when FACTORY_LOCK_ACCOUNT_URL is set, else LocalFlockLock.

    `slug` is the blob name (`<org>__<repo>`). `blob_options` are passed to
    BlobLeaseLock (for example `on_lost=` or `token_provider=`); the local lock ignores them.
    Without a `token_provider` the identity comes from `token_source_from_env`: workload
    identity on AKS, the managed identity endpoint on Container Apps. The identity's client
    id is FACTORY_LOCK_CLIENT_ID, else AZURE_CLIENT_ID. Misconfiguration raises
    LockConfigError (a LockError and a ValueError).
    """
    account_url = (env.get("FACTORY_LOCK_ACCOUNT_URL") or "").strip()
    if not account_url:
        return LocalFlockLock(local_path)
    if "token_provider" not in blob_options:
        blob_options["token_provider"] = token_source_from_env(env)
    return BlobLeaseLock(
        account_url,
        env.get("FACTORY_LOCK_CONTAINER") or "locks",
        slug,
        **blob_options,
    )

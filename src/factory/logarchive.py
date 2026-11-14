"""A retained, redacted, compressed copy of a run's transcripts and logs, kept for a number of days.

A finished run is purged by the janitor within about 15 minutes of its last job merging (``purge.py``), and
its logs go with the run directory. So that what a worker did stays inspectable after that, ``purge`` first
copies the allow-listed logs (the same names ``logs.py`` serves) of the run it is about to delete into an
archive, and a daily sweep of the manager removes copies older than ``FACTORY_LOGS_ARCHIVE_RETENTION_DAYS``
(default 30).

* **Where.** The default is a directory on the data volume, ``${FACTORY_DATA}/logs-archive/<org>__<name>/<run>/``,
  outside ``repos/`` (so neither ``purge`` nor ``purge-clone`` removes it, and a worker's sandbox hides it
  like everything else at the top of the data directory). With ``FACTORY_LOGS_ARCHIVE_ACCOUNT_URL`` set, the
  same keys go to a container of the existing Azure storage account (``FACTORY_LOGS_ARCHIVE_CONTAINER``,
  default ``logs-archive``), through the Blob REST API with the pod's workload identity (``locks.py`` supplies
  the token, ``urllib`` the HTTP; the transport is injectable, so tests use a fake).
* **What.** One gzip per file, named ``<job>/<file>.gz`` (or ``<file>.gz`` for a run-level file), redacted line
  by line before it is compressed (``redact.py``): the archive never holds more than the endpoint would have
  shown, and a transcript is never copied raw. Settings, prompt and result files are not copied. A file over
  ``MAX_FILE_BYTES`` keeps its tail, a line over ``MAX_LINE`` is cut, and a run stops at ``MAX_RUN_BYTES``. The
  whole copy has a wall-clock budget (``MAX_SECONDS``): past it the file in progress is kept as far as it got,
  with a line that says so, and the copy fails like any other (below).
* **Best effort.** A failed copy never stops or slows a purge beyond the attempt: the CLI prints one fixed
  line (``FAILED_MARKER``) and the purge goes on, because keeping a finished run's directory forever is the
  worse failure (the disk fills, the repository's clone is never released). The manager counts those lines.
  The purge runs as a CLI child that the manager kills at its command timeout (300 s), so a copy that did not
  return would keep the run for ever: that is what ``MAX_SECONDS`` and the linear redaction are for.
* **Retention** is by modification time (local) or ``Last-Modified`` (blob) of each copy, so a run that is archived
  again (purged after a retry) starts its clock over. Nothing here is read back by the API: an operator reads a
  copy from the volume or the container (docs/operations.md, "Logs").

Stdlib only. Importing this module has no side effects.
"""

from __future__ import annotations

import contextlib
import gzip
import http.client
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, Mapping, NamedTuple

from . import logs
from .redact import Redactor

ENV_MODE = "FACTORY_LOGS_ARCHIVE"
ENV_ACCOUNT_URL = "FACTORY_LOGS_ARCHIVE_ACCOUNT_URL"
ENV_CONTAINER = "FACTORY_LOGS_ARCHIVE_CONTAINER"
ENV_RETENTION_DAYS = "FACTORY_LOGS_ARCHIVE_RETENTION_DAYS"
DEFAULT_CONTAINER = "logs-archive"
DEFAULT_RETENTION_DAYS = 30
MIN_RETENTION_DAYS = 1
MAX_RETENTION_DAYS = 3650
ARCHIVE_DIR = "logs-archive"
FAILED_MARKER = "factory: log archive failed"
SWEEP_SECONDS = 24 * 3600.0  # the janitor sweeps the archive at most this often

MAX_FILE_BYTES = 128 * 1024 * 1024  # more than this of one file: its tail is kept
MAX_RUN_BYTES = 512 * 1024 * 1024  # raw bytes of all the files of one run
MAX_LINE = 1024 * 1024
MAX_SECONDS = 150.0  # wall clock for one run's copy: the purge CLI is killed at the manager's 300 s command timeout
RUN_LOGS = re.compile(r"(?:plan\.log|supervisor\.log|append\.log|planner\.jsonl|planner\.stderr)")
JOB_DIR = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
RUN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,49}")
REPO_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9._-]{1,100}")
_CONTAINER = re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]")
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_TOKEN = re.compile(r"[A-Za-z0-9._~+/=-]{1,8192}")

API_VERSION = "2023-11-03"
REQUEST_TIMEOUT = 60.0
MAX_LIST_BODY = 8 * 1024 * 1024
LIST_PAGE = 1000
MAX_LIST_PAGES = 20  # one sweep looks at most at this many pages
MAX_DELETES = 2000  # and removes at most this many blobs


class ArchiveError(RuntimeError):
    """A copy or a sweep failed. The message is fixed text and a status code: never a token, a URL or a body."""


class ArchiveConfigError(ValueError):
    """A FACTORY_LOGS_ARCHIVE* setting is invalid."""


@dataclass(frozen=True)
class Config:
    enabled: bool = True
    account_url: str = ""
    container: str = DEFAULT_CONTAINER
    retention_days: int = DEFAULT_RETENTION_DAYS

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Config":
        mode = (env.get(ENV_MODE) or "on").strip().lower()
        if mode not in ("on", "off"):
            raise ArchiveConfigError(f"{ENV_MODE} must be on or off")
        text = (env.get(ENV_RETENTION_DAYS) or "").strip()
        days = DEFAULT_RETENTION_DAYS
        if text:
            if not (text.isascii() and text.isdigit()) or not MIN_RETENTION_DAYS <= int(text) <= MAX_RETENTION_DAYS:
                raise ArchiveConfigError(f"{ENV_RETENTION_DAYS} must be a whole number of days from {MIN_RETENTION_DAYS} to {MAX_RETENTION_DAYS}")
            days = int(text)
        account = (env.get(ENV_ACCOUNT_URL) or "").strip()
        container = (env.get(ENV_CONTAINER) or "").strip() or DEFAULT_CONTAINER
        if account:
            _base_url(account, container)  # validates both
        return cls(mode == "on", account, container, days)


# --- backends -------------------------------------------------------------------------------


class LocalBackend:
    """Copies below a directory of the data volume (``logs-archive``); never through a link."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def put(self, key: str, source: Path) -> None:
        destination = self.root.joinpath(*key.split("/"))
        self._directory(destination.parent)
        staged = destination.with_name(f".{destination.name}.{os.getpid()}.part")
        try:
            descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            with os.fdopen(descriptor, "wb") as out, open(source, "rb") as handle:
                shutil.copyfileobj(handle, out, 1024 * 1024)
            os.replace(staged, destination)
        except OSError as error:
            raise ArchiveError(f"cannot write the archive ({type(error).__name__})") from None
        finally:
            with contextlib.suppress(OSError):
                staged.unlink()

    def _directory(self, path: Path) -> None:
        """Create ``path`` below the root one level at a time, refusing a link on the way."""
        try:
            current = self.root.parent
            for part in path.relative_to(current).parts:
                current = current / part
                if current.is_symlink():
                    raise ArchiveError("the archive path goes through a link")
                try:
                    os.mkdir(current, 0o700)
                except FileExistsError:
                    if not current.is_dir() or current.is_symlink():
                        raise ArchiveError("the archive path is not a directory") from None
        except OSError as error:
            raise ArchiveError(f"cannot create the archive directory ({type(error).__name__})") from None

    def sweep(self, cutoff: float) -> int:
        """Remove copies last written before ``cutoff`` (seconds since the epoch), then the directories left empty."""
        removed = 0
        if not self.root.is_dir() or self.root.is_symlink():
            return 0
        for directory, names, files in os.walk(self.root, topdown=False, followlinks=False):
            for name in files:
                path = Path(directory) / name
                try:
                    info = path.lstat()
                    if info.st_mtime < cutoff and not path.is_symlink():
                        path.unlink()
                        removed += 1
                except OSError:
                    continue
            if Path(directory) != self.root:
                with contextlib.suppress(OSError):
                    os.rmdir(directory)  # only an empty one goes
        return removed


class Reply(NamedTuple):
    status: int
    headers: Mapping[str, str]
    body: bytes


# (method, url, headers, file to send or None) -> Reply. The default sends over HTTPS; tests inject a fake.
Transport = Callable[[str, str, Mapping[str, str], "Path | None"], Reply]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib would replay the Authorization header on a redirect; never follow one."""

    def redirect_request(self, *args, **kwargs):
        return None


def urllib_transport(timeout: float = REQUEST_TIMEOUT) -> Transport:
    """The real transport: no proxy (the account is reached directly), no redirect, a bounded reply body."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)

    def send(method: str, url: str, headers: Mapping[str, str], source: Path | None) -> Reply:
        handle = open(source, "rb") if source is not None else None
        try:
            request = urllib.request.Request(url, data=handle, method=method)
            for name, value in headers.items():
                request.add_header(name, value)
            if handle is not None:
                request.add_header("Content-Length", str(os.fstat(handle.fileno()).st_size))
            elif method in ("PUT", "POST"):
                request.add_header("Content-Length", "0")
            try:
                response = opener.open(request, timeout=timeout)
            except urllib.error.HTTPError as error:
                try:
                    return Reply(error.code, dict(error.headers), error.read(MAX_LIST_BODY))
                finally:
                    error.close()
            except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as error:
                raise ArchiveError(f"blob request failed ({type(error).__name__})") from None
            with response:
                return Reply(response.status, dict(response.headers), response.read(MAX_LIST_BODY + 1))
        finally:
            if handle is not None:
                handle.close()

    return send


def _base_url(account_url: str, container: str) -> str:
    parts = urllib.parse.urlsplit(account_url)
    loopback = parts.scheme == "http" and parts.hostname in _LOOPBACK
    if parts.scheme != "https" and not loopback:
        raise ArchiveConfigError(f"{ENV_ACCOUNT_URL} must be https")
    if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ArchiveConfigError(f"{ENV_ACCOUNT_URL} must be a bare URL with no credentials, query or fragment")
    try:
        parts.port  # noqa: B018 - raises ValueError for a malformed port
    except ValueError:
        raise ArchiveConfigError(f"{ENV_ACCOUNT_URL} has an invalid port") from None
    if not _CONTAINER.fullmatch(container):
        raise ArchiveConfigError(f"{ENV_CONTAINER} is not a valid container name")
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}/{container}"


class BlobBackend:
    """Copies into a container of an Azure storage account over the Blob REST API (Put Blob, List Blobs, Delete Blob)."""

    def __init__(self, account_url: str, container: str, token_provider: Callable[[], str], transport: Transport | None = None):
        self._base = _base_url(account_url, container)
        self._token = token_provider
        self._send = transport or urllib_transport()

    def _call(self, method: str, url: str, extra: Mapping[str, str] | None = None, source: Path | None = None) -> Reply:
        try:
            token = self._token()
        except Exception as error:
            raise ArchiveError(f"token provider failed ({type(error).__name__})") from None
        if not (isinstance(token, str) and _TOKEN.fullmatch(token)):
            raise ArchiveError("token provider returned an unusable token")
        headers = {"Authorization": f"Bearer {token}", "x-ms-version": API_VERSION, **(extra or {})}
        return self._send(method, url, headers, source)

    def put(self, key: str, source: Path) -> None:
        url = f"{self._base}/{urllib.parse.quote(key, safe='/')}"
        reply = self._call(
            "PUT", url, {"x-ms-blob-type": "BlockBlob", "Content-Type": "application/gzip"}, source
        )
        if not 200 <= reply.status < 300:
            raise ArchiveError(f"blob upload failed (HTTP {reply.status})")

    def sweep(self, cutoff: float) -> int:
        """Delete blobs last modified before ``cutoff``: a bounded number of list pages and deletes per call."""
        removed, marker = 0, ""
        for _ in range(MAX_LIST_PAGES):
            names, marker = self._list(marker)
            for name, modified in names:
                if modified < cutoff:
                    if removed >= MAX_DELETES:
                        return removed
                    reply = self._call("DELETE", f"{self._base}/{urllib.parse.quote(name, safe='/')}", {})
                    if reply.status == 202:
                        removed += 1
                    elif reply.status != 404:  # a blob that is already gone is fine
                        raise ArchiveError(f"blob delete failed (HTTP {reply.status})")
            if not marker:
                break
        return removed

    def _list(self, marker: str) -> tuple:
        query = {"restype": "container", "comp": "list", "maxresults": str(LIST_PAGE)}
        if marker:
            query["marker"] = marker
        reply = self._call("GET", f"{self._base}?{urllib.parse.urlencode(query)}")
        if reply.status != 200 or len(reply.body) > MAX_LIST_BODY:
            raise ArchiveError(f"blob list failed (HTTP {reply.status})")
        if b"<!DOCTYPE" in reply.body or b"<!ENTITY" in reply.body:
            raise ArchiveError("blob list answer refused")
        try:
            document = ElementTree.fromstring(reply.body)
            names = []
            for blob in document.iter("Blob"):
                name = blob.findtext("Name") or ""
                stamp = blob.findtext("Properties/Last-Modified") or ""
                names.append((name, parsedate_to_datetime(stamp).timestamp()))
            following = document.findtext("NextMarker") or ""
        except (ElementTree.ParseError, TypeError, ValueError):
            raise ArchiveError("blob list answer unreadable") from None
        return names, following


# --- copying --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Result:
    files: int
    skipped: int
    bytes: int  # raw bytes read from the logs


def backend_for(config: Config, data_dir: Path, env: Mapping[str, str], transport: Transport | None = None):
    """The backend the settings choose: the container with the pod's identity, else the directory of the data volume."""
    if config.account_url:
        from .locks import token_source_from_env

        return BlobBackend(config.account_url, config.container, token_source_from_env(env), transport)
    return LocalBackend(Path(data_dir) / ARCHIVE_DIR)


def _sources(base: Path, parts: tuple) -> list:
    """``[(relative name, descriptor)]`` of the files of a run worth keeping; the caller closes the descriptors."""
    found = []
    top = logs.open_directory(base, parts)
    try:
        try:
            entries = sorted(entry.name for entry in os.scandir(top) if not entry.name.startswith("."))
        except OSError:
            return found
        for name in entries:
            if RUN_LOGS.fullmatch(name):
                _add(found, top, name, name)
            elif JOB_DIR.fullmatch(name):
                try:
                    inner = logs.enter_directory(top, name)
                except logs.LogNotFound:
                    continue
                try:
                    with contextlib.suppress(OSError):  # an unreadable job directory is skipped, like an unreadable file
                        for file in sorted(os.listdir(inner)):
                            if logs.JOB_FILE.fullmatch(file):
                                _add(found, inner, file, f"{name}/{file}")
                finally:
                    os.close(inner)
    finally:
        os.close(top)
    return found


def _add(found: list, directory: int, name: str, relative: str) -> None:
    with contextlib.suppress(logs.LogNotFound):  # a file that is gone or not a regular file is simply not archived
        found.append((relative, logs.open_file(directory, name)))


_OUT_OF_TIME = "the copy ran out of time"


def _drop_rest_of_line(handle) -> int:
    """Read and drop the rest of an over-long line; return how many bytes that was."""
    dropped = 0
    while True:
        rest = handle.readline(1024 * 1024)
        dropped += len(rest)
        if not rest or rest.endswith(b"\n"):
            return dropped


def _compress(descriptor: int, destination: Path, budget: int, jsonl: bool, expired: Callable[[], bool]) -> tuple:
    """Write the redacted lines of an open log to ``destination`` as gzip.

    Returns ``(raw bytes of the file that covers, whether the whole of it was copied)``: the copy stops, with a
    line that says so, when ``expired()`` is true (checked before every line, and a line is bounded by ``MAX_LINE``).
    """
    size = os.fstat(descriptor).st_size
    start = max(0, size - min(MAX_FILE_BYTES, budget))
    redactor = Redactor(spans_lines=not jsonl)
    with os.fdopen(os.dup(descriptor), "rb") as handle, gzip.open(destination, "wb", compresslevel=6) as out:
        if start:
            handle.seek(start - 1)
            if handle.read(1) != b"\n":
                handle.readline(MAX_LINE)  # the partial first line of the tail
            out.write(f"[the first {start} bytes of this file are not kept]\n".encode())
        while True:
            if expired():
                out.write(b"[the rest of this file is not kept: the copy ran out of time]\n")
                return size - start, False
            raw = handle.readline(MAX_LINE + 1)
            if not raw:
                break
            length = len(raw)
            if len(raw) > MAX_LINE and not raw.endswith(b"\n"):
                length += _drop_rest_of_line(handle)
            text = logs.render_line(redactor, raw.rstrip(b"\n"), length - (1 if raw.endswith(b"\n") else 0), MAX_LINE)
            if text is not None:
                out.write(text.encode("utf-8"))
                out.write(b"\n")
        marker = redactor.finish()  # the file ended inside a key block: say how many lines that withheld
        if marker is not None:
            out.write(f"{marker}\n".encode())
    return size - start, True


def archive_run(run_dir: Path, repo: str, backend, *, stage: Path | None = None, seconds: float | None = None,
                clock: Callable[[], float] = time.monotonic) -> Result:
    """Copy the logs of ``run_dir`` (``<data>/repos/<org>/<name>/runs/<run>``) to ``backend``, redacted and compressed.

    A file that cannot be read is skipped and counted; a backend that fails raises ``ArchiveError`` (the files
    before it stay copied), and so does a copy that is still going after ``seconds`` of ``clock`` (``MAX_SECONDS``
    by default; the file it was in stays copied as far as it got). ``repo`` is ``<org>/<name>``.
    """
    run = Path(run_dir)
    if not REPO_NAME.fullmatch(repo) or not RUN_NAME.fullmatch(run.name) or len(run.parts) < 6:
        raise ArchiveError("not a run directory of a managed repository")
    parts = run.parts[-5:]
    if parts[0] != "repos" or parts[3] != "runs":
        raise ArchiveError("not a run directory of a managed repository")
    prefix = f"{repo.replace('/', '__')}/{run.name}"
    sources = _sources(run.parents[4], parts)
    deadline = clock() + (MAX_SECONDS if seconds is None else seconds)

    def expired() -> bool:
        return clock() >= deadline

    files = skipped = taken = 0
    try:
        with tempfile.TemporaryDirectory(dir=stage) as scratch:
            staged = Path(scratch) / "copy.gz"
            while sources:
                relative, descriptor = sources.pop(0)
                try:
                    if expired():
                        raise ArchiveError(_OUT_OF_TIME)
                    if taken >= MAX_RUN_BYTES:
                        skipped += 1
                        continue
                    try:
                        covered, complete = _compress(descriptor, staged, MAX_RUN_BYTES - taken, relative.endswith(".jsonl"), expired)
                    except OSError:
                        skipped += 1
                        continue
                    taken += covered
                    backend.put(f"{prefix}/{relative}.gz", staged)
                    files += 1
                    if not complete:
                        raise ArchiveError(_OUT_OF_TIME)
                finally:
                    os.close(descriptor)
    finally:
        for _, descriptor in sources:  # left over when the backend failed or the time ran out
            os.close(descriptor)
    return Result(files, skipped, taken)


def sweep(backend, retention_days: int, now: Callable[[], float] = time.time) -> int:
    """Remove copies older than ``retention_days``; how many went."""
    return backend.sweep(now() - retention_days * 86400.0)

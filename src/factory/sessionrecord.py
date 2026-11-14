"""The session record of the pod backend: the files the supervisor and the relay share in the spool.

Slice 8 of the pod backend (``docs/pod-backend.md``). The relay (``relay.py``) is a separate program
that pods reach over the network; the supervisor (``PodBackend``, a later slice) lives in the manager
container. They meet only in the spool, a directory that is the root of an ``emptyDir``::

    <spool>/<session id>/record.json      written by the supervisor before the launch, rewritten once
                                          to bind the pod name and uid after it (atomic)
    <spool>/<session id>/state.json       written by the relay only: the one-way token state machine
    <spool>/<session id>/last_heartbeat   written by the relay: an integer, epoch seconds
    <spool>/<session id>/revoke           marker, written by the supervisor
    <spool>/<session id>/released         marker, written by the supervisor after collect
    <spool>/<session id>/in/bundle        the tree for the pod, staged by the supervisor
    <spool>/<session id>/out/             transcript, stderr, delta and report, written by the relay

This module is the one definition of that layout and of the record and state schemas. The relay
imports it and ``PodBackend`` imports the same module, so a round trip test (``tests/test_sessionrecord.py``)
writes with the supervisor-side functions and reads with the relay's.

Rules every function keeps: the session id is the contract's 32 lowercase hex digits, validated before it
is used as a path component (no traversal); every file is opened relative to a directory descriptor with
``O_NOFOLLOW`` and ``O_NONBLOCK`` and must be a regular file; reads are bounded; writes are atomic (a
temporary name in the same directory, ``fsync``, ``rename``); documents have closed field sets and bounded
sizes. Errors raise ``RecordError`` with a fixed text that never echoes the rejected value, which may come
from a pod.

Standard library only. Nothing here reads the environment or the network.
"""

import hashlib
import json
import os
import re
import secrets
import stat
from contextlib import contextmanager, suppress

from . import podcontract as pc

VERSION = 1

# --- Layout ------------------------------------------------------------------------------------

RECORD_FILE = "record.json"
STATE_FILE = "state.json"
HEARTBEAT_FILE = "last_heartbeat"
IN_DIR = "in"
OUT_DIR = "out"
BUNDLE_FILE = "bundle"
OUT_TRANSCRIPT = "transcript"
OUT_STDERR = "stderr"
OUT_DELTA = "delta"
OUT_REPORT = "report.json"
# The closed set of names the relay writes under out/. Nothing from a request ever becomes a path.
OUT_FILES = (OUT_TRANSCRIPT, OUT_STDERR, OUT_DELTA, OUT_REPORT)

# The marker names are part of the schema (fixed here, not a field of the record).
MARKER_REVOKE = "revoke"
MARKER_RELEASED = "released"
MARKERS = (MARKER_REVOKE, MARKER_RELEASED)

# The spool root is owned by the relay uid with the shared group, mode 2770 (set once by the init
# container). Directories below it are group-writable and setgid so that the supervisor (same group) and
# the relay can both work in them; nothing is open to others. The numbers are built from the stat flags.
DIR_MODE = stat.S_ISGID | stat.S_IRWXU | stat.S_IRWXG
FILE_MODE = stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IWGRP

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC

MAX_RECORD_BYTES = 4096
MAX_STATE_BYTES = 1024
_MAX_HEARTBEAT_BYTES = 32
_MAX_DEADLINE = 2**40

# --- Schema ------------------------------------------------------------------------------------

RECORD_FIELDS = (
    "version",
    "session",
    "token_sha256",
    "pod_name",
    "pod_uid",
    "deadline",
    "caps",
    "repo",
    "write_scope",
    "manager",
)

# The per-session caps. A record may lower a cap but never raise it above the contract.
CAP_LIMITS = {
    "bundle_bytes": pc.BUNDLE_MAX_BYTES,
    "delta_bytes": pc.DELTA_MAX_BYTES,
    "delta_files": pc.DELTA_MAX_FILES,
    "file_bytes": pc.FILE_MAX_BYTES,
    "transcript_bytes": pc.TRANSCRIPT_MAX_BYTES,
    "stderr_bytes": pc.STDERR_MAX_BYTES,
}

# The write scopes of phases.py ("none": read-only phase, the delta must be empty; "cwd": it may write).
WRITE_SCOPES = ("none", "cwd")

# The one-way state machine of a session token. "released" is supervisor-driven (a marker), never stored.
STATES = ("issued", "bound", "preflighted", "fetched", "streaming", "finished", "expired", "revoked")
TERMINAL_STATES = ("finished", "expired", "revoked")
RELEASED = "released"
STATE_FIELDS = ("version", "state", "fetch_attempts", "preflight_code", "reject", "ended_at")
# A bundle fetch that drops half way may be retried; the relay counts every attempt.
MAX_FETCH_ATTEMPTS = 3
# The fixed codes of a session the relay cut off. A rejected session is finished: every call answers 410
# and the supervisor reads the code before it trusts anything under out/.
REJECT_CODES = (
    "preflight_failed",
    "bundle_cap",
    "bundle_attempts",
    "transcript_cap",
    "stderr_cap",
    "delta_cap",
    "report_cap",
)

_SESSION_HEX = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_HEX12 = re.compile(r"[0-9a-f]{12}")
# 43 URL-safe characters are the 256 bits `new_token` makes; a token with less entropy is refused, not accepted.
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43,128}")
POD_UID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{6,62}[a-z0-9]")
CODE_PATTERN = re.compile(r"[a-z0-9_]{1,32}")


class RecordError(ValueError):
    """A record, state document, marker or file that is not valid. The text is fixed; it never echoes the input."""


class MissingRecord(RecordError):
    """The session has no record (or no session directory)."""


# --- Small validators --------------------------------------------------------------------------


def _is_int(value, low, high):
    # type(value) is int on purpose: a bool is an int in Python and must not count as a number here.
    return type(value) is int and low <= value <= high


def _text(value, pattern):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _require(condition, field):
    if not condition:
        raise RecordError(f"invalid session record field {field}")


def new_token():
    """A fresh one-time token (43 URL-safe characters, 256 bits). Only its hash is ever stored."""
    return secrets.token_urlsafe(32)


def token_hash(token):
    """The lowercase hex SHA-256 of a token; ValueError (fixed text) when it is not a token."""
    if not _text(token, TOKEN_PATTERN):
        raise ValueError("invalid token")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def default_caps():
    """The contract maxima as a caps dict (a fresh copy)."""
    return dict(CAP_LIMITS)


def _validate_caps(caps):
    _require(isinstance(caps, dict) and set(caps) == set(CAP_LIMITS), "caps")
    for key, limit in CAP_LIMITS.items():
        _require(_is_int(caps[key], 1, limit), "caps")
    return {key: caps[key] for key in CAP_LIMITS}


def validate(record):
    """The record as a normalised copy when it is valid, else RecordError (closed fields, bounded values)."""
    _require(isinstance(record, dict) and set(record) == set(RECORD_FIELDS), "fields")
    _require(_is_int(record["version"], VERSION, VERSION), "version")
    _require(_text(record["session"], _SESSION_HEX), "session")
    _require(_text(record["token_sha256"], _HEX64), "token_sha256")
    bound = record["pod_name"] is not None or record["pod_uid"] is not None
    if bound:
        _require(record["pod_name"] == f"{pc.POD_NAME_PREFIX}{record['session']}", "pod_name")
        _require(_text(record["pod_uid"], POD_UID_PATTERN), "pod_uid")
    _require(_is_int(record["deadline"], 1, _MAX_DEADLINE), "deadline")
    _require(_text(record["repo"], _HEX12), "repo")
    _require(record["write_scope"] in WRITE_SCOPES, "write_scope")
    _require(_text(record["manager"], _HEX12), "manager")
    result = {field: record[field] for field in RECORD_FIELDS}
    result["caps"] = _validate_caps(record["caps"])
    return result


def build(session, token, deadline, repo, write_scope, manager, caps=None):
    """A new, unbound record for a session: what the supervisor writes before it launches.

    `repo` is the repository label value (`podcontract.repo_key`), `manager` the manager instance id.
    """
    return validate(
        {
            "version": VERSION,
            "session": session,
            "token_sha256": token_hash(token),
            "pod_name": None,
            "pod_uid": None,
            "deadline": deadline,
            "caps": default_caps() if caps is None else dict(caps),
            "repo": repo,
            "write_scope": write_scope,
            "manager": manager,
        }
    )


def new_state():
    """The state of a session the relay has not touched yet."""
    return {
        "version": VERSION,
        "state": "issued",
        "fetch_attempts": 0,
        "preflight_code": None,
        "reject": None,
        "ended_at": None,
    }


def validate_state(document):
    """The state document as a normalised copy when it is valid, else RecordError."""
    _require_state(isinstance(document, dict) and set(document) == set(STATE_FIELDS))
    _require_state(_is_int(document["version"], VERSION, VERSION))
    _require_state(document["state"] in STATES)
    _require_state(_is_int(document["fetch_attempts"], 0, MAX_FETCH_ATTEMPTS))
    code = document["preflight_code"]
    _require_state(code is None or _text(code, CODE_PATTERN))
    _require_state(document["reject"] is None or document["reject"] in REJECT_CODES)
    ended = document["ended_at"]
    _require_state(ended is None or _is_int(ended, 0, _MAX_DEADLINE))
    return {field: document[field] for field in STATE_FIELDS}


def _require_state(condition):
    if not condition:
        raise RecordError("invalid session state")


# --- Files relative to a directory descriptor ---------------------------------------------------


def _encode(document):
    return f"{json.dumps(document, sort_keys=True, separators=(',', ':'))}\n".encode("ascii")


def _decode(raw):
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise RecordError("unreadable session document") from None


def _read_small_at(fd, name, limit):
    """The bytes of a small regular file under the directory `fd`; None when it does not exist."""
    try:
        handle = os.open(name, _READ_FLAGS, dir_fd=fd)
    except FileNotFoundError:
        return None
    except OSError:
        raise RecordError("unreadable session file") from None
    try:
        if not stat.S_ISREG(os.fstat(handle).st_mode):
            raise RecordError("unreadable session file")
        data = os.read(handle, limit + 1)
    finally:
        os.close(handle)
    if len(data) > limit:
        raise RecordError("session file too large")
    return data


def write_all(handle, data):
    """Write all of `data` to the descriptor `handle` (os.write may write less than asked)."""
    view = memoryview(data)
    while view:
        view = view[os.write(handle, view) :]


def _create_temp(fd, name):
    temp = f".{name}.{secrets.token_hex(4)}.tmp"
    handle = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, FILE_MODE, dir_fd=fd)
    return temp, handle


def atomic_write_at(fd, name, data, durable=True):
    """Write `data` as the file `name` of the directory `fd`: a temporary name, then one rename.

    A reader sees the old file or the new one, never a partial one; a failure leaves the old file and
    removes the temporary one. The mode is FILE_MODE whatever the umask.
    """
    temp, handle = _create_temp(fd, name)
    try:
        try:
            os.fchmod(handle, FILE_MODE)
            write_all(handle, data)
            if durable:
                os.fsync(handle)
        finally:
            os.close(handle)
        os.replace(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
    except OSError:
        with suppress(OSError):
            os.unlink(temp, dir_fd=fd)
        raise


def ensure_dir_at(parent_fd, name):
    """A descriptor for the subdirectory `name` of `parent_fd`, created with DIR_MODE when it is missing.

    A symbolic link or a file in its place is refused (OSError).
    """
    created = True
    try:
        os.mkdir(name, DIR_MODE, dir_fd=parent_fd)
    except FileExistsError:
        created = False
    fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    if created:
        os.fchmod(fd, DIR_MODE)
    return fd


# --- Record, state, heartbeat and markers (directory descriptor of one session) -------------------


def read_at(fd):
    """The validated record of the session directory `fd`; MissingRecord when there is none."""
    raw = _read_small_at(fd, RECORD_FILE, MAX_RECORD_BYTES)
    if raw is None:
        raise MissingRecord("no session record")
    return validate(_decode(raw))


def write_at(fd, record):
    """Validate and atomically write the record of the session directory `fd`."""
    atomic_write_at(fd, RECORD_FILE, _encode(validate(record)))


def read_state_at(fd):
    """The validated state of the session directory `fd`; a fresh state when the relay has not written one."""
    raw = _read_small_at(fd, STATE_FILE, MAX_STATE_BYTES)
    if raw is None:
        return new_state()
    return validate_state(_decode(raw))


def write_state_at(fd, document):
    """Validate and atomically write the state of the session directory `fd`.

    Not synced: the spool is an emptyDir that does not outlive the pod, the rename is what makes the
    write atomic, and a pod's next call must not wait for a disk flush to see the state it just caused.
    """
    atomic_write_at(fd, STATE_FILE, _encode(validate_state(document)), durable=False)


def read_heartbeat_at(fd):
    """The `last_heartbeat` of the session directory `fd` (epoch seconds), None when absent or not an integer."""
    try:
        raw = _read_small_at(fd, HEARTBEAT_FILE, _MAX_HEARTBEAT_BYTES)
    except RecordError:
        return None
    text = None if raw is None else raw.decode("ascii", "replace").strip()
    if text is None or not text.isascii() or not text.isdigit():
        return None
    return int(text)


def write_heartbeat_at(fd, seconds):
    """Atomically write `last_heartbeat` (an integer of epoch seconds). Not synced: it is rewritten constantly."""
    if not _is_int(seconds, 0, _MAX_DEADLINE):
        raise RecordError("invalid heartbeat")
    atomic_write_at(fd, HEARTBEAT_FILE, f"{seconds}\n".encode("ascii"), durable=False)


def _check_marker(name):
    if name not in MARKERS:
        raise RecordError("unknown marker")


def marked_at(fd, name):
    """Whether the marker `name` exists in the session directory `fd` (a link counts as present, it is never followed)."""
    _check_marker(name)
    try:
        os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def marker_time_at(fd, name):
    """The modification time of the marker `name` as integer seconds, None when it does not exist."""
    _check_marker(name)
    try:
        return int(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mtime)
    except FileNotFoundError:
        return None


def mark_at(fd, name):
    """Create the marker `name` (idempotent). It has no content."""
    _check_marker(name)
    handle = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, FILE_MODE, dir_fd=fd)
    try:
        os.fchmod(handle, FILE_MODE)
    finally:
        os.close(handle)


# --- Whole-spool entry points ---------------------------------------------------------------------


@contextmanager
def _root_fd(root):
    if isinstance(root, int):
        yield root
        return
    fd = os.open(root, _DIR_FLAGS)
    try:
        yield fd
    finally:
        os.close(fd)


def open_session_at(root_fd, session):
    """A descriptor for the session directory under the spool root descriptor; the caller closes it.

    FileNotFoundError when the session has no directory; the session id is validated first, so it can
    never carry a path separator or `..`.
    """
    if not _text(session, _SESSION_HEX):
        raise RecordError("invalid session id")
    return os.open(session, _DIR_FLAGS, dir_fd=root_fd)


@contextmanager
def session_dir(root, session, create=False):
    """The directory descriptor of one session. `root` is the spool path or its directory descriptor."""
    if not _text(session, _SESSION_HEX):
        raise RecordError("invalid session id")
    with _root_fd(root) as root_fd:
        try:
            fd = ensure_dir_at(root_fd, session) if create else open_session_at(root_fd, session)
        except FileNotFoundError:
            raise MissingRecord("no session record") from None
    try:
        yield fd
    finally:
        os.close(fd)


def write(root, record):
    """Supervisor side: validate and atomically write a session's record, creating its directory."""
    checked = validate(record)
    with session_dir(root, checked["session"], create=True) as fd:
        write_at(fd, checked)


def read(root, session):
    """The validated record of a session."""
    with session_dir(root, session) as fd:
        return read_at(fd)


def bind(root, session, pod_name, pod_uid):
    """Supervisor side: bind the pod name and uid the launcher returned to the record (atomic rewrite).

    Binding again with the same values is allowed; another pod for the same session is refused.
    """
    if not isinstance(pod_name, str) or not isinstance(pod_uid, str):
        raise RecordError("invalid session record field pod_name")
    with session_dir(root, session) as fd:
        record = read_at(fd)
        if record["pod_name"] is not None:
            if (record["pod_name"], record["pod_uid"]) == (pod_name, pod_uid):
                return record
            raise RecordError("session already bound")
        record["pod_name"] = pod_name
        record["pod_uid"] = pod_uid
        write_at(fd, record)
        return record


def read_state(root, session):
    """What the relay recorded for a session (the supervisor polls this)."""
    with session_dir(root, session) as fd:
        return read_state_at(fd)


def read_heartbeat(root, session):
    """`last_heartbeat` of a session in epoch seconds, None when the relay has seen none."""
    with session_dir(root, session) as fd:
        return read_heartbeat_at(fd)


def mark(root, session, name):
    """Supervisor side: write the marker `revoke` or `released`."""
    with session_dir(root, session) as fd:
        mark_at(fd, name)


def is_marked(root, session, name):
    with session_dir(root, session) as fd:
        return marked_at(fd, name)

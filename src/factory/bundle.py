"""The bundle of the pod backend: the wire format of what a pod gets and gives back, and ship-in staging.

Slice 9a of the pod backend (``docs/pod-backend.md``). The supervisor's worktree stays authoritative.
A pod gets a SELF-CONTAINED copy of it at the identical path under its data directory, and (slice 9b)
sends back a file delta that the supervisor validates and applies to its own worktree. One module
defines the format for both directions, so the supervisor side (here and in 9b) and the pod side
(slices 10a and 10b) cannot diverge.

The wire format (version 1) is a magic, a version and length-prefixed records, nothing else::

    stream  := "FCTYBNDL" <version: u16 big endian> record*
    record  := <kind: u8> <flags: u8> <path length: u16> <path> <size: u64> <payload: size bytes>

The set of kinds is CLOSED: ``file`` (1, the bytes of a regular file), ``symlink`` (2, the link target
as text), ``delete`` (3, no payload; the delta only) and ``baseline`` (4, a path, the kind of the shipped
path and the SHA-256 of a file's bytes or of a symlink's target; the bundle only). Any other kind byte
is a protocol error, so a hard link, a device, a fifo or a directory cannot even be expressed
(directories are implied by paths). The only flag is bit 0, the exec bit; any other bit is an error.

A path is ``<area>/<relative path>``. The areas say where a record goes and are a closed set:
``tree`` (the worktree), ``git`` (the files of a staged, self-contained git directory, which the
extractor puts at ``<worktree>/.git``), ``read`` (read-only files outside the worktree, at their
path under the data directory) and ``meta`` (``manifest.json``, the one JSON object that describes the
session and names the worktree). A record path never carries an absolute path, ``..``, an empty or
``.`` component, a NUL, a backslash or a component that is ``.git`` in any case or Unicode variant
(``is_git_name``); the git directory travels under the area ``git`` precisely so that no record
ever spells that name. Everything is checked by ONE function for the writer and the reader.

Reading the worktree uses ``openat`` with ``O_NOFOLLOW`` and never follows a link (the previous worker
may have planted one), never waits on a fifo (``O_NONBLOCK`` and a regular-file check on the open
descriptor, as ``relay.py`` does), and enforces every cap from ``podcontract`` (file size, count,
bytes, path length, depth) from ``lstat`` data BEFORE a byte is read. Staging fails with
``BundleTooLarge`` ("bundle too large"), never truncates. A hostile length prefix is rejected before
anything is allocated: nothing is read in one piece larger than 64 KiB except a manifest of at most
256 KiB.

The pod gets: every regular file and symlink of the worktree in whatever ignore state; a minimal git
directory built supervisor-side through ``gitsafe`` (an empty repository, a shallow fetch of the head
commit and of the base commit, the index of the head; never a copy of the object store, never
alternates, no remote and no path into the supervisor's directories; plus the supervisor's local
``info/exclude`` and ``info/attributes``, which git reads from a git directory); the ``read_paths`` files; the
manifest; and a ``baseline`` record per shipped tree path, which is what the pod compares against to
compute its delta. The supervisor's own pointer and any nested ``.git`` are never shipped; fifos,
sockets, devices and names that cannot be expressed are left out and counted.

Every failure of this module is a ``BundleError`` with a fixed text: a record that cannot be decoded
(``WireError``, a link target holding a NUL byte included), a cap (``BundleTooLarge``), a full spool
(``SpoolFull``) and a tree that cannot be read or written safely (``TreeError``, which also wraps what
the operating system refuses when a tree is written).

Slice 9b adds the other direction on the supervisor's side: ``apply_delta`` validates a whole delta first
(``DeltaRejected`` with a fixed code, ``DeltaTooLarge``: nothing is written) and only then applies it to the
supervisor's own worktree with ``openat`` and ``O_NOFOLLOW`` per component, a temporary name and a rename, and
``unlinkat`` for deletions (``DeltaApplyFailed`` when the operating system refuses a step part-way). The delta
is hostile input from a process that a possibly prompt-injected model ran: see the section "The delta" below.

Nothing here reads the environment, the network or Kubernetes. Standard library only (``gitsafe``
runs git). Contract-tested with real local git repositories; nothing was run in a pod.
"""

import collections
import contextlib
import errno
import functools
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import unicodedata
from dataclasses import dataclass, field
from typing import NamedTuple

from . import gitsafe
from . import podcontract as pc
from . import sessionrecord as sr

# --- The format -------------------------------------------------------------------------------

MAGIC = b"FCTYBNDL"
VERSION = 1
_VERSION = struct.Struct(">H")
_HEAD = struct.Struct(">BBH")  # kind, flags, path length
_SIZE = struct.Struct(">Q")
HEADER = b"".join((MAGIC, _VERSION.pack(VERSION)))
RECORD_FIXED_BYTES = _HEAD.size + _SIZE.size

FILE = 1
SYMLINK = 2
DELETE = 3
BASELINE = 4
KINDS = {FILE: "file", SYMLINK: "symlink", DELETE: "delete", BASELINE: "baseline"}
FLAG_EXEC = 0x01

BUNDLE = "bundle"  # supervisor to pod
DELTA = "delta"  # pod to supervisor (slice 9b validates and applies it)

AREA_TREE = "tree"
AREA_GIT = "git"
AREA_READ = "read"
AREA_META = "meta"
AREAS = (AREA_TREE, AREA_GIT, AREA_READ, AREA_META)
MANIFEST_PATH = "manifest.json"

# Which kinds each direction accepts in each area. A direction an area does not list is refused there.
_ALLOWED = {
    AREA_TREE: {BUNDLE: (FILE, SYMLINK, BASELINE), DELTA: (FILE, SYMLINK, DELETE)},
    AREA_GIT: {BUNDLE: (FILE,)},
    AREA_READ: {BUNDLE: (FILE,)},
    AREA_META: {BUNDLE: (FILE,)},
}

# Bundle-local numbers (the contract has no bundle file count and no depth: its caps are the byte caps,
# the per-file cap, the delta file count and the path length). PROVISIONAL, like every cap of the program.
MAX_FILES = 200_000
MAX_DEPTH = 100  # components below an area, also the number of directory descriptors a walk holds open
MAX_HISTORY = 10_000  # commits of history a staged git directory may need (see ``build_git_dir``)
# The manifest carries the session's small files (prompt, settings, system prompt, .gitconfig) as base64, so a file of N bytes
# costs 4N/3. It was 256 KiB until 2026-11-11, when a repository with a large skills library (issue #260) made a system prompt of
# 568 KB and every plan failed with "bundle too large" (seen on production). 2 MiB holds a system prompt of about 1.4 MiB
# (the largest `repo_skills_budget_kib`, 1024 KiB, plus the repository's CLAUDE.md and the operator addendum) with room for the rest.
MANIFEST_MAX_BYTES = 2 * 1024 * 1024
INFO_MAX_BYTES = 256 * 1024  # the supervisor's local info/exclude or info/attributes (each), copied into the git directory
CHUNK_BYTES = 64 * 1024
BASELINE_PAYLOAD_BYTES = 33  # one kind byte and a SHA-256


@dataclass(frozen=True)
class Limits:
    """The byte and file caps of one direction."""

    max_bytes: int
    max_files: int


LIMITS = {
    BUNDLE: Limits(pc.BUNDLE_MAX_BYTES, MAX_FILES),
    DELTA: Limits(pc.DELTA_MAX_BYTES, pc.DELTA_MAX_FILES),
}


# --- Errors (the text is fixed and never echoes a path, a name or a byte of a tree) -----------------


class BundleError(ValueError):
    """Base of every failure here. ``reason`` is a short fixed word for logs and tests."""

    def __init__(self, message, reason):
        super().__init__(message)
        self.reason = reason


class WireError(BundleError):
    """The stream is not a valid bundle or delta."""

    def __init__(self, reason):
        super().__init__("bundle protocol error", reason)


class BundleTooLarge(BundleError):
    """A cap was exceeded: the session fails, nothing is truncated."""

    def __init__(self, reason):
        super().__init__("bundle too large", reason)


class SpoolFull(BundleError):
    """The spool cannot take the bundle now: the caller waits, like capacity, and stages again later."""

    def __init__(self, needed=0, available=0):
        super().__init__("spool is full", "spool")
        self.needed = needed
        self.available = available


class TreeError(BundleError):
    """A tree could not be read or written safely (it changed, a link is in the way, a path clashes)."""

    def __init__(self, reason):
        super().__init__("tree could not be read or written safely", reason)


# --- Paths -------------------------------------------------------------------------------------

_EXTRA_IGNORABLE = frozenset((0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x3164, 0xFFA0))


def _ignorable(char):
    """A code point that renders as nothing (default ignorable): such a character cannot hide a name."""
    code = ord(char)
    return (
        unicodedata.category(char) == "Cf"
        or code in _EXTRA_IGNORABLE
        or 0x180B <= code <= 0x180F
        or 0xFE00 <= code <= 0xFE0F
        or 0xE0100 <= code <= 0xE01EF
    )


@functools.cache
def _ignorable_table():
    """A ``str.translate`` table that deletes every default-ignorable code point (one scan of the code space, on first use).

    A table keeps ``folded`` in C code however long a hostile name is: a Python loop over every character of a
    4096-byte path was the slowest thing a delta could make the supervisor do.
    """
    return {code: None for code in range(sys.maxunicode + 1) if _ignorable(chr(code))}


def folded(name):
    """``name`` as a file system that ignores case, Unicode form and invisible characters would see it.

    Default-ignorable code points are removed, the text is NFKC-casefolded, and trailing dots and
    spaces are dropped (Windows ignores them).
    """
    if name.isascii():  # no default-ignorable code point is ASCII and NFKC leaves ASCII alone: only the case and the tail are left
        return name.casefold().rstrip(". ")
    table = _ignorable_table()
    text = unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", name.translate(table)).casefold())
    return text.translate(table).rstrip(". ")


def is_git_name(name):
    """Whether a path component would be taken for a git directory: ``.git`` in any case or Unicode variant, or ``git~1``."""
    return folded(name) in (".git", "git~1")


def split_wire_path(raw):
    """``(area, parts)`` of the path bytes of a record; ``WireError`` for anything but a clean relative path."""
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= pc.PATH_MAX_BYTES or b"\0" in raw or b"\\" in raw:
        raise WireError("path")
    pieces = os.fsdecode(raw).split("/")
    area, parts = pieces[0], tuple(pieces[1:])
    if area not in AREAS or not parts or len(parts) > MAX_DEPTH:
        raise WireError("path")
    if any(part in ("", ".", "..") or is_git_name(part) for part in parts):
        raise WireError("path")
    return area, parts


def wire_path(area, path):
    """The path bytes of a record for the relative ``path`` (text) in ``area``; checked like a read one."""
    raw = os.fsencode(f"{area}/{path}")
    split_wire_path(raw)
    return raw


def split_relative(text):
    """The components of a relative path given as text (the manifest's worktree); ``WireError`` otherwise."""
    return split_wire_path(os.fsencode(f"{AREA_TREE}/{text}"))[1]


# --- The checks every record passes, whichever side it is on --------------------------------------


def _payload_cap(kind, area):
    if kind == FILE:
        if area == AREA_META:
            return MANIFEST_MAX_BYTES
        # The staged git directory is bounded by the bundle cap alone (a pack is one file); the rest by the contract.
        return pc.BUNDLE_MAX_BYTES if area == AREA_GIT else pc.FILE_MAX_BYTES
    if kind == SYMLINK:
        return pc.PATH_MAX_BYTES
    return 0 if kind == DELETE else BASELINE_PAYLOAD_BYTES


def check_record(direction, kind, area, size, executable):
    """Raise unless a record of this kind, area, size and exec bit is allowed in ``direction``."""
    if kind not in KINDS or kind not in _ALLOWED.get(area, {}).get(direction, ()):
        raise WireError("kind")
    if executable and kind not in (FILE, BASELINE):
        raise WireError("flags")
    if kind == DELETE and size != 0:  # before the cap: a delete's cap is 0, so this is a protocol error and not a large file
        raise WireError("size")
    if size > _payload_cap(kind, area):
        raise BundleTooLarge("size")
    if kind == SYMLINK and size < 1:
        raise WireError("size")
    if kind == BASELINE and size != BASELINE_PAYLOAD_BYTES:
        raise WireError("size")


def check_target(target):
    """Raise unless ``target`` can be the text of a link: no NUL byte (no operating system can hold one).

    The length is bounded by ``check_record`` (1 to ``PATH_MAX_BYTES``). Used by the writer, and by the reader
    when it decodes a ``symlink`` record, so the same text is refused in both directions.
    """
    if b"\0" in target:
        raise WireError("target")


def baseline_payload(kind, digest):
    """The payload of a baseline record: the kind byte of the shipped path and its 32-byte digest."""
    if kind not in (FILE, SYMLINK) or not isinstance(digest, bytes) or len(digest) != 32:
        raise WireError("baseline")
    return b"".join((bytes((kind,)), digest))


# --- Writer ------------------------------------------------------------------------------------


class Writer:
    """Writes a stream to a binary sink (a file object): the header, then records.

    Every record is checked before a byte of it is written (kind, area, path, size, exec bit and the caps
    of ``direction``), so the writer cannot produce what the reader refuses.
    """

    def __init__(self, sink, direction=BUNDLE, limits=None):
        self._sink = sink
        self.direction = direction
        self._limits = LIMITS[direction] if limits is None else limits
        self.written = 0
        self.records = 0
        self.baselines = 0
        self._emit(HEADER)

    def _emit(self, data):
        self._sink.write(data)
        self.written += len(data)

    def begin(self, kind, area, path, size, executable=False):
        """Check a record and write its header; the caller then writes exactly ``size`` payload bytes."""
        raw = wire_path(area, path)
        check_record(self.direction, kind, area, size, executable)
        if self.written + RECORD_FIXED_BYTES + len(raw) + size > self._limits.max_bytes:
            raise BundleTooLarge("bytes")
        if kind == BASELINE:
            self.baselines += 1
        else:
            self.records += 1
        if self.records > self._limits.max_files or self.baselines > self._limits.max_files:
            raise BundleTooLarge("files")
        flags = FLAG_EXEC if executable else 0
        self._emit(b"".join((_HEAD.pack(kind, flags, len(raw)), raw, _SIZE.pack(size))))

    def put(self, kind, area, path, payload=b"", executable=False):
        """A record whose payload is already in memory (a symlink target, a delete, a baseline, the manifest)."""
        if kind == SYMLINK:
            check_target(payload)
        self.begin(kind, area, path, len(payload), executable)
        self._emit(payload)

    def put_file(self, area, path, fd, size, executable=False):
        """A file record whose ``size`` bytes are read from the descriptor ``fd``; returns their SHA-256 digest."""
        self.begin(FILE, area, path, size, executable)
        digest = hashlib.sha256()
        left = size
        while left:
            chunk = os.read(fd, min(CHUNK_BYTES, left))
            if not chunk:
                raise TreeError("changed")  # the file shrank while it was being read
            digest.update(chunk)
            self._emit(chunk)
            left -= len(chunk)
        if not _at_end(fd):
            raise TreeError("changed")  # the file grew while it was being read: never a silently truncated copy
        return digest.digest()

    def put_baseline(self, path, kind, digest, executable=False):
        self.put(BASELINE, AREA_TREE, path, baseline_payload(kind, digest), executable)

    def put_manifest(self, document):
        self.put(FILE, AREA_META, MANIFEST_PATH, manifest_bytes(document))


def manifest_bytes(document):
    """The manifest as bytes: one JSON object of at most ``MANIFEST_MAX_BYTES`` (a session description, never a secret)."""
    if not isinstance(document, dict):
        raise WireError("manifest")
    data = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("ascii")
    if len(data) > MANIFEST_MAX_BYTES:
        raise BundleTooLarge("size")
    return data


# --- Reader ------------------------------------------------------------------------------------


class Record:
    """One record of a stream. Its payload is read from the stream on demand, never all at once by default."""

    def __init__(self, reader, kind, executable, area, parts, size):
        self._reader = reader
        self.kind = kind
        self.executable = executable
        self.area = area
        self.parts = parts
        self.size = size
        self.left = size
        self._held = None  # a link target: read and checked when the record is decoded

    def hold(self, data):
        """Keep ``data``, already read from the stream, as the whole payload."""
        self._held = data
        self.left = 0

    @property
    def path(self):
        return "/".join(self.parts)

    def chunks(self):
        """The payload in pieces of at most 64 KiB."""
        if self._held is not None:
            yield self._held
            return
        while self.left:
            yield self._reader.take_payload(self, min(CHUNK_BYTES, self.left))

    def read(self, limit=MANIFEST_MAX_BYTES):
        """The whole payload as bytes; ``BundleTooLarge`` when it is longer than ``limit`` (nothing is read then)."""
        if self.size > limit:
            raise BundleTooLarge("size")
        return b"".join(self.chunks())


class Reader:
    """Reads a stream from a binary source (a file object): checks the header, then yields ``Record`` objects.

    The reader enforces the caps of ``direction`` while it reads. Every length is checked before it is
    used to read: a path length against the contract's path cap, a size against the cap of its kind and
    area and against what is left of the byte cap. An unknown kind byte, an unknown flag, a record the
    direction does not allow, a path that is not clean and a stream cut off inside a record are
    ``WireError``; too many bytes or records are ``BundleTooLarge``.
    """

    def __init__(self, source, direction=BUNDLE, limits=None):
        self._source = source
        self.direction = direction
        self._limits = LIMITS[direction] if limits is None else limits
        self.consumed = 0
        self.records = 0
        self.baselines = 0
        self._current = None
        head = self._read(len(HEADER))
        if head[: len(MAGIC)] != MAGIC:
            raise WireError("magic")
        if head != HEADER:
            raise WireError("version")

    def _read(self, count):
        pieces = []
        left = count
        while left:
            data = self._source.read(left)
            if not data:
                break
            pieces.append(data)
            left -= len(data)
        data = b"".join(pieces)
        self.consumed += len(data)
        if len(data) != count:
            raise WireError("truncated")
        return data

    def take_payload(self, record, count):
        data = self._read(count)
        record.left -= count
        return data

    def _drain(self):
        record = self._current
        while record is not None and record.left:
            self.take_payload(record, min(CHUNK_BYTES, record.left))

    def _next_head(self):
        first = self._source.read(1)
        if not first:
            return None
        self.consumed += 1
        return b"".join((first, self._read(_HEAD.size - 1)))

    def __iter__(self):
        while True:
            self._drain()
            head = self._next_head()
            if head is None:
                return
            self._current = self._record(head)
            yield self._current

    def _record(self, head):
        kind, flags, length = _HEAD.unpack(head)
        if flags & ~FLAG_EXEC:
            raise WireError("flags")
        if kind not in KINDS:
            raise WireError("kind")
        if not 1 <= length <= pc.PATH_MAX_BYTES:
            raise WireError("path")
        area, parts = split_wire_path(self._read(length))
        (size,) = _SIZE.unpack(self._read(_SIZE.size))
        check_record(self.direction, kind, area, size, bool(flags & FLAG_EXEC))
        if self.consumed + size > self._limits.max_bytes:
            raise BundleTooLarge("bytes")
        self._count(kind)
        record = Record(self, kind, bool(flags & FLAG_EXEC), area, parts, size)
        if kind == SYMLINK:  # at most PATH_MAX_BYTES (``check_record``): read and checked here, so no consumer sees a bad target
            target = self._read(size)
            check_target(target)
            record.hold(target)
        return record

    def _count(self, kind):
        if kind == BASELINE:
            self.baselines += 1
        else:
            self.records += 1
        if self.records > self._limits.max_files or self.baselines > self._limits.max_files:
            raise BundleTooLarge("files")


def read_manifest(record):
    """The manifest object of a ``meta/manifest.json`` record; ``WireError`` when it is anything else."""
    if record.area != AREA_META or record.parts != (MANIFEST_PATH,) or record.kind != FILE:
        raise WireError("manifest")
    try:
        document = json.loads(record.read().decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise WireError("manifest") from None
    if not isinstance(document, dict):
        raise WireError("manifest")
    return document


def read_baseline(record):
    """``(path, kind, executable, digest)`` of a baseline record."""
    data = record.read(BASELINE_PAYLOAD_BYTES)
    kind = data[0]
    if kind not in (FILE, SYMLINK):
        raise WireError("baseline")
    return record.path, kind, record.executable, data[1:]


# --- Reading a tree safely ---------------------------------------------------------------------

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


class Entry(NamedTuple):
    """A regular file or symlink found by ``walk``. ``dir_fd`` stays valid only until the walk moves on."""

    path: str
    kind: int
    executable: bool
    size: int
    dir_fd: int
    name: str
    target: bytes | None


@dataclass
class Totals:
    """What a staging run has counted so far, across all its walks (record bytes include the headers)."""

    area_bytes: int = 0
    files: int = 0
    skipped: int = 0  # fifos, sockets, devices and names that cannot be expressed
    git_names: int = 0  # entries named like a git directory (the supervisor's pointer, a nested repository)
    limits: Limits = field(default_factory=lambda: LIMITS[BUNDLE])

    def add(self, wire_length, size, counts=True):
        """Count one record of ``size`` payload bytes; a baseline record (``counts`` False) is not a file."""
        self.area_bytes += RECORD_FIXED_BYTES + wire_length + size
        if counts:
            self.files += 1
        if self.files > self.limits.max_files or self.area_bytes > self.limits.max_bytes:
            raise BundleTooLarge("bytes")


def _expressible(parts):
    return not any("\\" in part for part in parts)


def _classify(fd, prefix, name, area, totals, file_cap, baseline):
    """The next step of a walk for one directory entry: an ``Entry``, a ``(descriptor, parts)`` to descend into, or None."""
    if is_git_name(name):
        totals.git_names += 1
        return None
    parts = (*prefix, name)
    path = "/".join(parts)
    try:
        info = os.lstat(name, dir_fd=fd)
    except OSError:
        raise TreeError("changed") from None
    mode = info.st_mode
    if len(parts) > MAX_DEPTH or len(os.fsencode(f"{area}/{path}")) > pc.PATH_MAX_BYTES:
        raise BundleTooLarge("path")
    if not _expressible(parts) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
        totals.skipped += 1
        return None
    if stat.S_ISDIR(mode):
        try:
            return os.open(name, _DIR_FLAGS, dir_fd=fd), parts
        except OSError:
            raise TreeError("changed") from None
    return _entry(fd, name, path, info, area, totals, file_cap, baseline)


def _entry(fd, name, path, info, area, totals, file_cap, baseline):
    length = len(os.fsencode(f"{area}/{path}"))
    if stat.S_ISLNK(info.st_mode):
        try:
            target = os.fsencode(os.readlink(name, dir_fd=fd))
        except OSError:
            raise TreeError("changed") from None
        if not 1 <= len(target) <= pc.PATH_MAX_BYTES:
            raise BundleTooLarge("path")
        totals.add(length, len(target))
        if baseline:
            totals.add(length, BASELINE_PAYLOAD_BYTES, counts=False)
        return Entry(path, SYMLINK, False, len(target), fd, name, target)
    if file_cap is not None and info.st_size > file_cap:
        raise BundleTooLarge("size")
    totals.add(length, info.st_size)
    if baseline:
        totals.add(length, BASELINE_PAYLOAD_BYTES, counts=False)
    return Entry(path, FILE, bool(info.st_mode & stat.S_IXUSR), info.st_size, fd, name, None)


def walk(root_fd, area, totals, file_cap=pc.FILE_MAX_BYTES, baseline=False):
    """Yield every regular file and symlink under the directory ``root_fd``, in sorted order, as ``Entry`` objects.

    Iterative, with at most ``MAX_DEPTH`` descriptors open. Nothing is followed: a symlink is an entry
    (its target text), never a directory to enter, and every directory is opened with ``O_NOFOLLOW``.
    ``totals`` counts record bytes and files as it goes and raises ``BundleTooLarge`` from ``lstat``
    data, before any file is opened. Close the generator (``contextlib.closing``) to release descriptors.
    """
    stack = [(root_fd, (), iter(sorted(os.listdir(root_fd))), False)]
    try:
        while stack:
            fd, prefix, names, owned = stack[-1]
            name = next(names, None)
            if name is None:
                stack.pop()
                if owned:
                    os.close(fd)
                continue
            found = _classify(fd, prefix, name, area, totals, file_cap, baseline)
            if isinstance(found, Entry):
                yield found
            elif found is not None:
                child, parts = found
                stack.append((child, parts, iter(sorted(os.listdir(child))), True))
    finally:
        for fd, _prefix, _names, owned in stack:
            if owned:
                os.close(fd)


def _at_end(fd):
    """True when the descriptor has no byte left to read (a regular file read to exactly its recorded size)."""
    try:
        return not os.read(fd, 1)
    except OSError:
        raise TreeError("changed") from None


def open_file(entry):
    """A read-only descriptor for the file ``entry`` names: no link followed, no wait on a fifo.

    ``O_NOFOLLOW`` refuses a link, ``O_NONBLOCK`` keeps the open from waiting for the other end of a
    fifo, and the ``fstat`` of the open descriptor refuses anything that is not a regular file before a
    byte is read (the entry may have been swapped since ``lstat``). A size that is not the one ``lstat``
    recorded, smaller or larger, is a file that changed: nothing is ever truncated to the recorded size.
    """
    try:
        fd = os.open(entry.name, _READ_FLAGS, dir_fd=entry.dir_fd)
    except OSError:
        raise TreeError("changed") from None
    try:
        info = os.fstat(fd)
    except OSError:
        os.close(fd)
        raise TreeError("changed") from None
    if not stat.S_ISREG(info.st_mode) or info.st_size != entry.size:
        os.close(fd)
        raise TreeError("changed")
    return fd


def digest_of(entry):
    """The baseline digest of an entry: SHA-256 of a file's bytes or of a symlink's target."""
    if entry.kind == SYMLINK:
        return hashlib.sha256(entry.target).digest()
    fd = open_file(entry)
    try:
        digest = hashlib.sha256()
        left = entry.size
        while left:
            chunk = os.read(fd, min(CHUNK_BYTES, left))
            if not chunk:
                raise TreeError("changed")
            digest.update(chunk)
            left -= len(chunk)
        if not _at_end(fd):
            raise TreeError("changed")
    finally:
        os.close(fd)
    return digest.digest()


def open_under(root_fd, parts):
    """A new descriptor for the directory ``parts`` below ``root_fd``; no link is followed and nothing is created."""
    fd = os.dup(root_fd)
    try:
        for name in parts:
            child = os.open(name, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
    except OSError:
        os.close(fd)
        raise TreeError("path") from None
    return fd


# --- Writing a tree safely (extraction; slice 9b adds the delta's replace and delete) -----------------


class TreeWriter:
    """Creates files and symlinks under a root directory one component at a time, never through a link.

    Every component is opened with ``O_NOFOLLOW``; a missing directory is created. A file is created with
    ``O_EXCL``, so a path that is written twice, or that is already something else, is an error and an
    earlier record can never be overwritten by a later one.
    """

    def __init__(self, root_fd):
        self._root = root_fd

    def open_dir(self, parts):
        """A new descriptor for the directory ``parts`` under the root, created on the way."""
        fd = os.dup(self._root)
        try:
            for name in parts:
                fd = _step(fd, name)
        except (OSError, ValueError):  # ValueError: the operating system refused the text of a name
            os.close(fd)
            raise TreeError("path") from None
        return fd

    def create_file(self, parts, chunks, mode):
        """Write a new file ``parts`` from an iterable of byte chunks; returns the SHA-256 digest of what it wrote."""
        dir_fd = self.open_dir(parts[:-1])
        try:
            fd = os.open(parts[-1], _CREATE_FLAGS, mode, dir_fd=dir_fd)
            try:
                os.fchmod(fd, mode)
                digest = hashlib.sha256()
                for chunk in chunks:
                    sr.write_all(fd, chunk)
                    digest.update(chunk)
            finally:
                os.close(fd)
        except BundleError:  # the stream failed while it was read (it is a ValueError too): keep its type
            raise
        except (OSError, ValueError):
            raise TreeError("write") from None
        finally:
            os.close(dir_fd)
        return digest.digest()

    def create_symlink(self, parts, target):
        """Create the symlink ``parts`` with the target text; returns the SHA-256 digest of the target."""
        dir_fd = self.open_dir(parts[:-1])
        try:
            os.symlink(os.fsdecode(target), parts[-1], dir_fd=dir_fd)
        except (OSError, ValueError):  # ValueError: a NUL in the target or in a name, which no operating system accepts
            raise TreeError("write") from None
        finally:
            os.close(dir_fd)
        return hashlib.sha256(target).digest()

    # The delta's operations (slice 9b). A path that exists is replaced by a rename of a new object that was created
    # under a temporary name in the SAME directory (``O_EXCL`` or ``symlink``, so nothing is followed and nothing is
    # overwritten before the rename), and a rename replaces the directory entry itself, never what a link points at.

    def open_existing(self, parts):
        """A new descriptor for the directory ``parts`` under the root, or None when it does not exist.

        Nothing is created and no link is followed: a component that is a link or a file is ``TreeError``.
        """
        fd = os.dup(self._root)
        try:
            for name in parts:
                child = os.open(name, _DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = child
        except FileNotFoundError:
            os.close(fd)
            return None
        except (OSError, ValueError):
            os.close(fd)
            raise TreeError("path") from None
        return fd

    def replace_file(self, parts, chunks, mode):
        """Write ``parts`` from an iterable of byte chunks and rename it over what is there; returns the SHA-256 digest."""
        dir_fd = self.open_dir(parts[:-1])
        try:
            return _replace_with(dir_fd, parts[-1], lambda temp: _write_temp(dir_fd, temp, chunks, mode))
        finally:
            os.close(dir_fd)

    def replace_symlink(self, parts, target):
        """Create the symlink ``parts`` (renamed over what is there); returns the SHA-256 digest of the target."""
        dir_fd = self.open_dir(parts[:-1])
        try:
            return _replace_with(dir_fd, parts[-1], lambda temp: _link_temp(dir_fd, temp, target))
        finally:
            os.close(dir_fd)

    def unlink(self, parts):
        """Remove the file or link ``parts``, never a directory; True when something was removed, False when it was gone."""
        dir_fd = self.open_existing(parts[:-1])
        if dir_fd is None:
            return False
        try:
            os.unlink(parts[-1], dir_fd=dir_fd)
        except FileNotFoundError:
            return False
        except (OSError, ValueError):  # a directory (``unlinkat`` without ``AT_REMOVEDIR``) or a permission
            raise TreeError("write") from None
        finally:
            os.close(dir_fd)
        return True

    def prune(self, parts):
        """Remove the directory ``parts`` when it is empty (``unlinkat`` with ``AT_REMOVEDIR``); True when it was removed."""
        dir_fd = self.open_existing(parts[:-1])
        if dir_fd is None:
            return False
        try:
            os.rmdir(parts[-1], dir_fd=dir_fd)
        except OSError as error:
            if error.errno in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT):
                return False
            raise TreeError("write") from None
        finally:
            os.close(dir_fd)
        return True


def _write_temp(dir_fd, temp, chunks, mode):
    fd = os.open(temp, _CREATE_FLAGS, mode, dir_fd=dir_fd)
    try:
        os.fchmod(fd, mode)
        digest = hashlib.sha256()
        for chunk in chunks:
            sr.write_all(fd, chunk)
            digest.update(chunk)
    finally:
        os.close(fd)
    return digest.digest()


def _link_temp(dir_fd, temp, target):
    os.symlink(os.fsdecode(target), temp, dir_fd=dir_fd)
    return hashlib.sha256(target).digest()


def _replace_with(dir_fd, name, make):
    """``make(temp)`` creates a new object under a temporary name in ``dir_fd``; it is renamed to ``name`` or removed again."""
    temp = f".{secrets.token_hex(8)}.factory-tmp"
    try:
        digest = make(temp)
    except FileExistsError:  # the name is taken by something that is not ours: neither follow it nor remove it
        raise TreeError("write") from None
    except BundleError:  # the stream failed while it was read: keep its type
        _discard(dir_fd, temp)
        raise
    except (OSError, ValueError):
        _discard(dir_fd, temp)
        raise TreeError("write") from None
    try:
        os.replace(temp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except OSError:  # a directory has taken the place of the file, or the directory is gone
        _discard(dir_fd, temp)
        raise TreeError("write") from None
    return digest


def _discard(dir_fd, name):
    with contextlib.suppress(OSError):
        os.unlink(name, dir_fd=dir_fd)


def _step(fd, name):
    """The subdirectory ``name`` of ``fd`` (created when missing) as a new descriptor; ``fd`` is closed on success only."""
    with contextlib.suppress(FileExistsError):
        os.mkdir(name, 0o755, dir_fd=fd)
    child = os.open(name, _DIR_FLAGS, dir_fd=fd)
    os.close(fd)
    return child


@dataclass
class Extracted:
    """What ``extract`` found: the worktree's path under the root, the manifest, the baseline and a count."""

    worktree: str
    manifest: dict
    baseline: dict
    files: int


# The directories every git directory needs. A detached head with no base ships no ref at all, so
# ``refs`` would be implied by no path and git would refuse the copy ("not a git repository").
GIT_SKELETON = (("objects",), ("refs", "heads"), ("refs", "tags"))


def _mode(executable, read_only):
    if read_only:
        return 0o555 if executable else 0o444
    return 0o755 if executable else 0o644


class _Extraction:
    """The state of one ``extract`` run: where each area goes, what was written, what the baseline says."""

    def __init__(self, writer, worktree):
        self.writer = writer
        self.worktree = worktree
        self.written = {}
        self.baseline = {}
        self.files = 0
        self.git = False

    def take(self, record):
        area = record.area
        self.git = self.git or area == AREA_GIT
        if area == AREA_TREE and record.kind == BASELINE:
            path, kind, executable, digest = read_baseline(record)
            if path in self.baseline:
                raise WireError("duplicate")
            self.baseline[path] = (kind, executable, digest)
            return
        if area == AREA_META:
            raise WireError("manifest")
        self.files += 1
        if record.kind == SYMLINK:
            digest = self.writer.create_symlink((*self.worktree, *record.parts), record.read(pc.PATH_MAX_BYTES))
            self._note(record, SYMLINK, False, digest)
            return
        if area == AREA_GIT:
            self.writer.create_file((*self.worktree, ".git", *record.parts), record.chunks(), _mode(record.executable, False))
        elif area == AREA_READ:
            self.writer.create_file(record.parts, record.chunks(), _mode(record.executable, True))
        else:
            digest = self.writer.create_file((*self.worktree, *record.parts), record.chunks(), _mode(record.executable, False))
            self._note(record, FILE, record.executable, digest)

    def _note(self, record, kind, executable, digest):
        if record.path in self.written:
            raise WireError("duplicate")
        self.written[record.path] = (kind, executable, digest)

    def finish(self):
        if self.written != self.baseline:
            raise WireError("baseline")
        if self.git:  # directories are implied by paths, so an empty one that git insists on is made here
            for parts in GIT_SKELETON:
                os.close(self.writer.open_dir((*self.worktree, ".git", *parts)))


def extract(source, root, direction=BUNDLE):
    """Extract a bundle from the binary ``source`` into the directory ``root``; the pod's side of the ship-in.

    The first record must be the manifest, which names the worktree (a path under ``root``). The tree
    goes to the worktree, the git files to ``<worktree>/.git`` (the one place that name is spelled, by this
    function and not by data), the read-only files to their path under ``root``. Every path is written
    through ``TreeWriter``, so nothing is followed and nothing is overwritten. At the end every shipped
    tree path must have exactly one baseline record that matches what was written, and the other way
    round; then the empty directories a git directory needs (``GIT_SKELETON``) are made, because the
    wire format carries paths and so cannot carry an empty directory. Returns ``Extracted``.
    """
    reader = Reader(source, direction)
    records = iter(reader)
    manifest = read_manifest(next(records, None) or _missing())
    worktree = manifest.get("worktree")
    if not isinstance(worktree, str):
        raise WireError("manifest")
    parts = split_relative(worktree)
    if not parts:
        raise WireError("manifest")
    root_fd = os.open(os.path.realpath(root), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        state = _Extraction(TreeWriter(root_fd), parts)
        os.close(state.writer.open_dir(parts))
        for record in records:
            state.take(record)
        state.finish()
    finally:
        os.close(root_fd)
    return Extracted("/".join(parts), manifest, state.baseline, state.files)


def _missing():
    raise WireError("manifest")


# --- Staging the bundle (supervisor side) ----------------------------------------------------------

_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_BRANCH = re.compile(r"refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]{0,200}")
GIT_TIMEOUT_SECONDS = 600
_PRIVATE_NAMES = ("FETCH_HEAD", "ORIG_HEAD", "logs")
INFO_NAMES = ("exclude", "attributes")


@dataclass(frozen=True)
class Staged:
    """What ``stage`` did: the size of the bundle, the number of records and what it left out."""

    size: int
    records: int
    git_bytes: int
    skipped: int
    git_names: int
    worktree: str


def spool_usage(root):
    """Bytes held by regular files under the spool, three levels deep, links not followed (as the relay counts them)."""
    total = 0
    stack = [(os.fspath(root), 0)]
    while stack:
        path, depth = stack.pop()
        try:
            with os.scandir(path) as listing:
                entries = list(listing)
        except OSError:
            continue
        for entry in entries:
            total += _spool_entry(entry, depth, stack)
    return total


def _spool_entry(entry, depth, stack):
    try:
        info = entry.stat(follow_symlinks=False)
    except OSError:
        return 0
    if stat.S_ISDIR(info.st_mode):
        if depth < 2:
            stack.append((entry.path, depth + 1))
        return 0
    return info.st_size if stat.S_ISREG(info.st_mode) else 0


def _git(work, args, auth, check=True):
    """``git <args>`` in the repository ``work`` that this module just created, through ``gitsafe``."""
    return gitsafe.repo_run(
        ["-C", os.fspath(work), "-c", "core.logAllRefUpdates=false", *args],
        work,
        auth=auth,
        timeout=GIT_TIMEOUT_SECONDS,
        check=check,
    )


def _sha(text):
    if not _SHA.fullmatch(text):
        raise TreeError("git")
    return text


def _count(tree, gitdir, spec, auth):
    done = gitsafe.run(["rev-list", "--count", spec], tree, gitdir, auth=auth)
    try:
        return int(done.stdout.strip())
    except ValueError:
        raise TreeError("git") from None


def _depth(tree, gitdir, head, base, auth):
    """How many commits of history the staged repository needs for ``git log base..HEAD`` to read as it does here.

    The commits that ``base..head`` lists, plus the commit they fork from, and enough of ``base`` for that
    fork point to be reachable from it (otherwise the fork point would be listed too). Exact for the
    shapes tested (a linear branch, a merge of the base into it, a merge in progress); bounded by
    ``MAX_HISTORY`` and then by the byte cap.
    """
    if base is None:
        return 1
    ahead = _count(tree, gitdir, f"{base}..{head}", auth)
    fork = gitsafe.run(["merge-base", base, head], tree, gitdir, auth=auth, check=False)
    since = _sha(fork.stdout.strip()) if fork.returncode == 0 else None
    behind = _count(tree, gitdir, base if since is None else f"{since}..{base}", auth)
    depth = max(ahead, behind) + 1
    if depth > MAX_HISTORY:
        raise BundleTooLarge("history")
    return depth


def _branch(tree, gitdir, auth):
    """The branch the worktree is on (a full ref name) or None for a detached head or an unusual name."""
    done = gitsafe.run(["symbolic-ref", "-q", "HEAD"], tree, gitdir, auth=auth, check=False)
    name = done.stdout.strip()
    return name if done.returncode == 0 and _BRANCH.fullmatch(name) and ".." not in name else None


def build_git_dir(work, tree, gitdir, base=None, *, auth=gitsafe.PROCESS_AUTH):
    """Build a minimal, self-contained git directory for the worktree ``tree`` inside the empty directory ``work``.

    Through ``gitsafe`` only. It is an empty repository (no template: no hooks, no description) that
    fetches, shallow and with no remote, the head commit of ``tree`` and the commit ``base`` from the
    supervisor's common git directory over ``file://`` (so the objects are copied, never linked and never
    shared as alternates), points ``HEAD`` at the same branch (or detaches it), and gets the index of the
    head commit. It has no merge state: ``MERGE_HEAD`` and the index of a merge in progress stay in the
    supervisor's own git directory. Returns the path of the new git directory.
    """
    head = _sha(gitsafe.git(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], tree, gitdir, auth=auth))
    base_sha = None if base is None else _sha(gitsafe.git(["rev-parse", "--verify", "--quiet", f"{_sha(base)}^{{commit}}"], tree, gitdir, auth=auth))
    common = gitsafe.git(["rev-parse", "--path-format=absolute", "--git-common-dir"], tree, gitdir, auth=auth)
    depth = _depth(tree, gitdir, head, base_sha, auth)
    branch = _branch(tree, gitdir, auth)
    _git(work, ["init", "--quiet", "--template="], auth)
    wants = [f"{head}:refs/factory/head"]
    if base_sha is not None:
        wants.append(f"{base_sha}:refs/factory/base")
    _git(
        work,
        [
            "-c",
            "uploadpack.allowAnySHA1InWant=true",
            "-c",
            "transfer.unpackLimit=1",
            "fetch",
            "--quiet",
            "--no-tags",
            f"--depth={depth}",
            f"file://{common}",
            *wants,
        ],
        auth,
    )
    if branch is None:
        _git(work, ["update-ref", "--no-deref", "HEAD", head], auth)
    else:
        _git(work, ["update-ref", branch, head], auth)
        _git(work, ["symbolic-ref", "HEAD", branch], auth)
    _git(work, ["update-ref", "-d", "refs/factory/head"], auth)
    _git(work, ["read-tree", "HEAD"], auth)
    directory = os.path.join(work, ".git")
    for name in _PRIVATE_NAMES:
        path = os.path.join(directory, name)
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
    _copy_info(common, directory)
    return directory


def _read_info(info_fd, name):
    """The bytes of ``info/<name>`` below the descriptor ``info_fd``, or None when there is no such file.

    Nothing is followed and nothing is waited on (the file sits in a directory a worker may be able to
    write): a link, a fifo, a directory or anything else that is not a regular file is ``TreeError``, a
    file over ``INFO_MAX_BYTES`` is ``BundleTooLarge``.
    """
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=info_fd)
    except FileNotFoundError:
        return None
    except OSError:
        raise TreeError("git") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise TreeError("git")
        with os.fdopen(fd, "rb", closefd=False) as source:
            data = source.read(INFO_MAX_BYTES + 1)
    except OSError:
        raise TreeError("git") from None
    finally:
        os.close(fd)
    if len(data) > INFO_MAX_BYTES:
        raise BundleTooLarge("size")
    return data


def _copy_info(common, directory):
    """Copy the supervisor's local ``info/exclude`` and ``info/attributes`` into the staged git directory.

    Git reads ignore rules and attributes that are local to a repository from these two files, so
    without them the pod's ``git status`` would list a file the supervisor's hides. The bytes are copied
    (no link, no hard link); a missing file or directory is simply not copied.
    """
    try:
        common_fd = os.open(os.path.realpath(common), _DIR_FLAGS)
        try:
            info_fd = os.open("info", _DIR_FLAGS, dir_fd=common_fd)
        finally:
            os.close(common_fd)
    except FileNotFoundError:
        return
    except OSError:
        raise TreeError("git") from None
    try:
        found = {name: _read_info(info_fd, name) for name in INFO_NAMES}
    finally:
        os.close(info_fd)
    present = {name: data for name, data in found.items() if data is not None}
    if present:
        target = os.path.join(directory, "info")
        os.mkdir(target)
        for name, data in present.items():
            with open(os.path.join(target, name), "xb") as sink:
                sink.write(data)


def _walk_dir(fd, area, totals, file_cap, baseline):
    """Entries of the directory ``fd`` for a measuring pass: nothing is opened, the totals do the counting."""
    with contextlib.closing(walk(fd, area, totals, file_cap, baseline)) as entries:
        collections.deque(entries, maxlen=0)  # consume the generator: the totals count as it walks


def _relative(root, path):
    """The components of ``path`` below ``root`` (both real paths); ``TreeError`` when it is not below it."""
    try:
        relative = os.path.relpath(path, root)
    except ValueError:
        raise TreeError("outside") from None
    parts = tuple(relative.split(os.sep))
    if relative in (".", "") or parts[0] == "..":
        raise TreeError("outside")
    return parts


class _Plan:
    """What one staging run ships, found by measuring (no file is opened yet): the three sources and their totals."""

    def __init__(self, root_fd, root, tree_parts, read_paths):
        self.root_fd = root_fd
        self.tree_parts = tree_parts
        self.reads = [_relative(root, os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))) for path in read_paths]
        self._refuse_clashing_reads()
        self.totals = Totals()

    def _refuse_clashing_reads(self):
        """A read path under the worktree, or one listed twice, names a pod path that another record writes too.

        The pod creates every file with ``O_EXCL``, so such a bundle could never be extracted: refuse it here,
        where the caller can report it, not in the pod.
        """
        depth = len(self.tree_parts)
        if len(set(self.reads)) != len(self.reads) or any(parts[:depth] == self.tree_parts for parts in self.reads):
            raise TreeError("read path")

    def measure(self, manifest):
        self.totals.area_bytes = len(HEADER) + RECORD_FIXED_BYTES + len(wire_path(AREA_META, MANIFEST_PATH)) + len(manifest)
        for parts in self.reads:
            with self.read_entry(parts, self.totals):
                pass
        tree_fd = open_under(self.root_fd, self.tree_parts)
        try:
            _walk_dir(tree_fd, AREA_TREE, self.totals, pc.FILE_MAX_BYTES, True)
        finally:
            os.close(tree_fd)

    @contextlib.contextmanager
    def read_entry(self, parts, totals):
        """The ``Entry`` of one read path while its directory is open; a link, a directory or a missing file is refused."""
        parent = open_under(self.root_fd, parts[:-1])
        try:
            found = _classify(parent, parts[:-1], parts[-1], AREA_READ, totals, pc.FILE_MAX_BYTES, False)
            if not isinstance(found, Entry) or found.kind != FILE:
                if found is not None and not isinstance(found, Entry):
                    os.close(found[0])
                raise TreeError("read path")
            yield found
        finally:
            os.close(parent)


_CLAIM_LOCK = threading.Lock()
_CLAIMED = {}  # real path of a spool -> bytes that running stagings have claimed in it and not yet written


class _Claim:
    """The bytes one ``stage`` run holds in a spool until its bundle is renamed into place (or the run fails).

    The manager runs sessions in threads, so a check of the spool followed, much later, by a write would let
    several runs pass the same check and together go past the refusal limit (and the contract evicts the
    pod for a spool over its size). Each run therefore claims what it is about to write under one lock, and
    every check counts the claims of the other runs next to the bytes already on disk. A claim made at the
    first check is replaced at the second by the real size. The bytes of a bundle that is being written are
    briefly counted twice (on disk and in the claim): that can only make another run wait a little longer.
    """

    def __init__(self, spool, refuse_bytes):
        self._key = os.path.realpath(spool)
        self._refuse = refuse_bytes
        self._held = 0

    def resize(self, needed):
        """Hold ``needed`` bytes (instead of what this run held); ``SpoolFull`` when they would pass the limit."""
        with _CLAIM_LOCK:
            others = _CLAIMED.get(self._key, 0) - self._held
            used = spool_usage(self._key)
            if used + others + needed > self._refuse:
                raise SpoolFull(needed, max(0, self._refuse - used - others))
            _CLAIMED[self._key] = others + needed
            self._held = needed

    def release(self):
        with _CLAIM_LOCK:
            left = _CLAIMED.get(self._key, 0) - self._held
            self._held = 0
            if left > 0:
                _CLAIMED[self._key] = left
            else:
                _CLAIMED.pop(self._key, None)


def stage(
    spool,
    session,
    *,
    data_root,
    worktree,
    manifest,
    scratch,
    base=None,
    read_paths=(),
    gitdir=None,
    auth=gitsafe.PROCESS_AUTH,
    refuse_bytes=pc.SPOOL_REFUSE_BYTES,
):
    """Stage the bundle of one session as ``<spool>/<session>/in/bundle`` (atomically) and return ``Staged``.

    ``data_root`` is the supervisor's data directory; ``worktree`` and every path of ``read_paths`` must be
    under it, and they keep their path under it in the pod. ``manifest`` is a JSON-serialisable dict that
    this function completes with the key ``worktree``; it must hold no secret. ``scratch`` is a directory
    the supervisor owns, where the staged git directory is built and removed again. ``base`` is the base
    commit (a hex id) or None.

    Order, so that nothing is read before it must be: measure the read paths and the worktree from
    ``lstat`` data (``BundleTooLarge`` on a cap), check the spool (``SpoolFull`` is a wait, not a
    failure), build the git directory and measure it, check again, and only then write. A check counts
    the bytes on disk and what other running stagings of the same spool have claimed (``_Claim``), and
    keeps its own claim until the bundle is renamed into place. A failure leaves no ``bundle`` file and
    no scratch directory.
    """
    claim = _Claim(spool, refuse_bytes)
    try:
        return _stage(spool, session, data_root, worktree, manifest, scratch, base, read_paths, gitdir, auth, claim)
    except subprocess.SubprocessError:
        raise TreeError("git") from None
    finally:
        claim.release()


def _stage(spool, session, data_root, worktree, manifest, scratch, base, read_paths, gitdir, auth, claim):
    document = dict(manifest)
    root = os.path.realpath(data_root)
    tree_path = os.path.realpath(worktree)
    tree_parts = _relative(root, tree_path)
    if "worktree" in document:
        raise WireError("manifest")
    document["worktree"] = "/".join(tree_parts)
    manifest_data = manifest_bytes(document)
    trusted = gitsafe.checkout_gitdir(tree_path) if gitdir is None else gitdir
    if trusted is None:
        raise TreeError("git")
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        plan = _Plan(root_fd, root, tree_parts, read_paths)
        plan.measure(manifest_data)
        claim.resize(plan.totals.area_bytes)
        return _stage_with_git(spool, session, plan, document, scratch, tree_path, trusted, base, auth, claim)
    finally:
        os.close(root_fd)


def _stage_with_git(spool, session, plan, document, scratch, tree_path, gitdir, base, auth, claim):
    work = tempfile.mkdtemp(prefix=".stage-", dir=os.fspath(scratch))
    try:
        directory = build_git_dir(work, tree_path, gitdir, base, auth=auth)
        git_fd = os.open(directory, _DIR_FLAGS)
        try:
            before = plan.totals.area_bytes
            _walk_dir(git_fd, AREA_GIT, plan.totals, None, False)
            git_bytes = plan.totals.area_bytes - before
            claim.resize(plan.totals.area_bytes)
            return _write(spool, session, plan, document, git_fd, git_bytes)
        finally:
            os.close(git_fd)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _write(spool, session, plan, document, git_fd, git_bytes):
    with sr.session_dir(spool, session, create=True) as session_fd:
        in_fd = sr.ensure_dir_at(session_fd, sr.IN_DIR)
        try:
            writer = _write_atomic(in_fd, plan, document, git_fd)
        finally:
            os.close(in_fd)
    totals = plan.totals
    return Staged(writer.written, writer.records, git_bytes, totals.skipped, totals.git_names, document["worktree"])


def _write_atomic(in_fd, plan, document, git_fd):
    """Write the bundle under a temporary name and rename it into place; a failure leaves no partial file."""
    temp = f".{sr.BUNDLE_FILE}.{secrets.token_hex(4)}.tmp"
    fd = os.open(temp, _CREATE_FLAGS, sr.FILE_MODE, dir_fd=in_fd)
    committed = False
    try:
        os.fchmod(fd, sr.FILE_MODE)
        with os.fdopen(fd, "wb", closefd=False) as sink:
            writer = _emit_bundle(sink, plan, document, git_fd)
        os.replace(temp, sr.BUNDLE_FILE, src_dir_fd=in_fd, dst_dir_fd=in_fd)
        committed = True
    finally:
        if not committed:  # a partial bundle is never left under a name the relay could serve
            with contextlib.suppress(OSError):
                os.unlink(temp, dir_fd=in_fd)
        os.close(fd)
    return writer


def _emit_bundle(sink, plan, document, git_fd):
    """Write header, manifest, read-only files, git directory, tree and baseline; returns the writer."""
    writer = Writer(sink, BUNDLE)
    writer.put_manifest(document)
    for parts in plan.reads:
        with plan.read_entry(parts, Totals()) as entry:
            _emit_entries(writer, AREA_READ, [entry], None)
    with contextlib.closing(walk(git_fd, AREA_GIT, Totals(), None, False)) as entries:
        _emit_entries(writer, AREA_GIT, entries, None)
    baseline = []
    tree_fd = open_under(plan.root_fd, plan.tree_parts)
    try:
        with contextlib.closing(walk(tree_fd, AREA_TREE, Totals(), pc.FILE_MAX_BYTES, True)) as entries:
            _emit_entries(writer, AREA_TREE, entries, baseline)
    finally:
        os.close(tree_fd)
    for path, kind, executable, digest in baseline:
        writer.put_baseline(path, kind, digest, executable)
    return writer


def _emit_entries(writer, area, entries, baseline):
    """Write one record per entry; a ``baseline`` list receives ``(path, kind, executable, digest)`` for each."""
    for entry in entries:
        if entry.kind == SYMLINK:
            writer.put(SYMLINK, area, entry.path, entry.target)
            digest = hashlib.sha256(entry.target).digest()
        else:
            fd = open_file(entry)
            try:
                digest = writer.put_file(area, entry.path, fd, entry.size, entry.executable)
            finally:
                os.close(fd)
        if baseline is not None:
            baseline.append((entry.path, entry.kind, entry.executable, digest))


# --- The delta: validation and safe apply (slice 9b, supervisor side) ---------------------------------
#
# The delta is written by a process that a possibly prompt-injected model ran, and the supervisor applies it
# to ITS OWN worktree with ITS privileges. The whole stream is validated first, with no tree in sight; the
# tree is then checked read-only; only then is anything written. A violation at either of those two steps
# rejects ALL of the delta with a fixed code and writes nothing.

NAME_MAX = 255  # bytes in one path component: a longer name would only fail part-way through the apply
LINK_MAX = pc.PATH_MAX_BYTES - 1  # bytes of a link target: the operating system refuses ``PATH_MAX`` (4096) and more, which would fail part-way
DELTA_MAX_DIRECTORIES = 2 * pc.DELTA_MAX_FILES  # distinct directories the paths of a delta name (the apply creates every missing one)
SPOOL_MEMORY_BYTES = 4 * 1024 * 1024  # a delta up to this size is held in memory, a larger one in an unnamed temporary file
WRITE_SCOPE_CWD = "cwd"  # the one write scope that may carry records (``sessionrecord.WRITE_SCOPES``); anything else must be empty


class DeltaRejected(BundleError):
    """The delta is not acceptable: nothing was written. ``reason`` is the fixed code."""

    def __init__(self, reason):
        super().__init__("delta rejected", reason)


class DeltaTooLarge(BundleError):
    """A cap of the delta was exceeded (bytes, records, one file, a path): the session fails, nothing is truncated."""

    def __init__(self, reason):
        super().__init__("delta too large", reason)


class DeltaApplyFailed(BundleError):
    """The operating system refused a step while the delta was applied: the worktree is left as it is, for inspection."""

    def __init__(self, reason):
        super().__init__("delta could not be applied", reason)


class DeltaOp(NamedTuple):
    """One validated record: what it does, where, and how many payload bytes it carries."""

    kind: int
    parts: tuple
    executable: bool
    size: int


@dataclass(frozen=True)
class Applied:
    """What ``apply_delta`` did: records, files written, links written, paths deleted and the bytes of the delta."""

    records: int
    files: int
    symlinks: int
    deleted: int
    size: int


class _Tee:
    """Passes reads through and keeps a private copy of every byte that was read (so what is applied is what was checked)."""

    def __init__(self, source, copy):
        self._source = source
        self._copy = copy

    def read(self, count):
        data = bytes(self._source.read(count))
        try:
            self._copy.write(data)
        except OSError:  # the scratch directory is full or gone: not the delta's fault, and nothing has been written to a tree
            raise DeltaApplyFailed("spool") from None
        return data


def _check_link(parts, target):
    """Raise unless the link ``parts`` may point at ``target``: relative, inside the tree, no git name, no backslash.

    The target is a run of ``..`` (at most as many as the link has directories above it) followed by names
    only: a ``..`` after a name is refused, and so is a target of ``PATH_MAX`` bytes or more (``symlinkat``
    would fail on it part-way through the apply). That is what makes the lexical check sound. A link that is
    created sits in a directory of the tree that is real (``check_tree``), so a leading ``..`` leaves that
    directory for a real ancestor, and the names that follow only go down; by induction a link, or a chain
    of links, can never lead out of the tree. Without the rule ``l1 -> .`` and ``l2 -> l1/..`` would make
    ``l2`` the parent of the tree, and the supervisor follows links when it reads the files it applied.
    """
    if len(target) > LINK_MAX:
        raise WireError("link")
    text = os.fsdecode(target)
    if text.startswith("/") or "\\" in text:
        raise WireError("link")
    room = len(parts) - 1  # directories between the root of the tree and the link: how many ``..`` it may start with
    descending = False
    for name in text.split("/"):
        if name in ("", "."):
            continue
        if name == "..":
            room -= 1
            if descending or room < 0:
                raise WireError("link")
            continue
        if is_git_name(name):
            raise WireError("link")
        descending = True


def _op(record):
    """The ``DeltaOp`` of a record the reader accepted; the checks the reader does not make for the delta are made here."""
    if record.area != AREA_TREE or record.kind not in (FILE, SYMLINK, DELETE):
        raise WireError("kind")
    if any(len(os.fsencode(part)) > NAME_MAX for part in record.parts):
        raise WireError("path")
    if record.kind == SYMLINK:
        _check_link(record.parts, record.read(pc.PATH_MAX_BYTES))
    return DeltaOp(record.kind, record.parts, record.executable, record.size)


def _common(left, right):
    """How many leading components two paths share."""
    count = 0
    for one, two in zip(left, right):
        if one != two:
            break
        count += 1
    return count


def _is_prefix(shorter, longer):
    return len(shorter) < len(longer) and longer[: len(shorter)] == shorter


def _by_key(pair):
    return pair[0]


def _check_neighbours(before, current):
    """Two paths next to each other in folded order: the same path twice, or two spellings of one name; returns how many components they share."""
    (key_a, op_a), (key_b, op_b) = before, current
    shared = _common(key_a, key_b)
    if op_a.parts[:shared] != op_b.parts[:shared]:
        raise WireError("collision")  # equal after folding, different as written: one name on a folding file system
    if shared == len(key_a) == len(key_b):
        raise WireError("duplicate")
    return shared


def _new_directories(before, key, shared):
    """The directories above the path ``key`` that no path before it (in folded order) has above it: it names them first."""
    above = len(key) - 1
    if before is None:
        return above
    return max(0, above - min(shared, len(before[0]) - 1))


def _check_names(ops):
    """Refuse two paths that are one name after folding, a path written twice, and a path below another one.

    A path below another record's path is allowed only when exactly one of the two is a ``delete``: a file
    that becomes a directory (``delete a``, ``file a/b``) or a directory that becomes a file. Everything else
    (a file and a directory of one name, a link followed by a file written through it) is a ``clash``.
    Sorting by the folded components puts every path below ``p`` right after ``p``, so one pass with a
    chain of open ancestors finds all of it in ``O(n log n)`` and a hostile delta cannot make it quadratic.

    The same pass counts the distinct directories the paths name (a directory is new when the path before
    it in this order is not below it) and refuses more than ``DELTA_MAX_DIRECTORIES`` as ``BundleTooLarge``:
    the apply creates every directory a path needs, so without this cap 20,000 empty files with 99 new
    directories each would make about two million of them from a stream of a few megabytes.
    """
    keyed = sorted(((tuple(folded(part) for part in op.parts), op) for op in ops), key=_by_key)
    chain = []  # the records whose path is a proper prefix of the one being looked at
    before = None
    directories = 0
    for key, op in keyed:
        shared = 0 if before is None else _check_neighbours(before, (key, op))
        directories += _new_directories(before, key, shared)
        if directories > DELTA_MAX_DIRECTORIES:
            raise BundleTooLarge("directories")
        while chain and not _is_prefix(chain[-1][0], key):
            chain.pop()
        for _, ancestor in chain:
            if (ancestor.kind == DELETE) == (op.kind == DELETE):
                raise WireError("clash")
        chain.append((key, op))
        before = (key, op)


def _scan(source, limits, allow_writes):
    """``(ops, bytes read)`` of the stream; every record has passed the reader's checks and ``_op``."""
    reader = Reader(source, DELTA, limits)
    if not allow_writes:
        if source.read(1):  # a read-only phase writes nothing: any byte after the header is too much
            raise WireError("scope")
        return [], reader.consumed
    ops = [_op(record) for record in reader]
    _check_names(ops)
    return ops, reader.consumed


def _check_declared(ops, size, declared_records, declared_bytes):
    if (declared_records is not None and declared_records != len(ops)) or (declared_bytes is not None and declared_bytes != size):
        raise WireError("declared")


def validate_delta(source, *, write_scope, scratch=None, limits=None, declared_records=None, declared_bytes=None):
    """Read and check the whole delta in the binary ``source``; returns a ``ValidDelta`` that holds a private copy of its bytes.

    Nothing is written anywhere in the supervisor's trees. Raises ``DeltaTooLarge`` (a cap: the bytes, the
    records, one file, a path) or ``DeltaRejected`` with a fixed ``reason``: the stream (``magic``, ``version``,
    ``kind``, ``flags``, ``path``, ``size``, ``target``, ``truncated``), a link (``link``), two paths that clash
    (``duplicate``, ``collision``, ``clash``), a read-only phase that returned something (``scope``: only the
    write scope ``cwd`` may carry records) and numbers the pod's report declared that the stream does not
    have (``declared``: the format has no trailer, so a stream cut at a record boundary would otherwise read
    as a shorter delta; the caller passes the record count and the byte length the report names).
    ``scratch`` is the directory for the private copy of a large delta (the default is the system's).
    """
    copy = tempfile.SpooledTemporaryFile(max_size=SPOOL_MEMORY_BYTES, dir=None if scratch is None else os.fspath(scratch))
    effective = LIMITS[DELTA] if limits is None else limits
    handed_over = False
    try:
        ops, size = _scan(_Tee(source, copy), effective, write_scope == WRITE_SCOPE_CWD)
        _check_declared(ops, size, declared_records, declared_bytes)
        copy.seek(0)
        valid = ValidDelta(copy, ops, size, effective)
        handed_over = True
        return valid
    except BundleTooLarge as error:
        raise DeltaTooLarge(error.reason) from None
    except DeltaApplyFailed:
        raise
    except BundleError as error:
        raise DeltaRejected(error.reason) from None
    except OSError:  # the source could not be read
        raise DeltaRejected("read") from None
    finally:
        if not handed_over:
            copy.close()


# What the tree has to look like for a delta, checked READ-ONLY before the first write.


def _prune_order(deletes):
    """The directories the apply tries to remove after the deletions, deepest first (``_vacated`` predicts the same list).

    Every directory above a deleted path, the root excluded. A walk up from a path stops at the first directory
    that is already known (everything above it is known too), so a directory is made once however many deleted
    paths lie below it: the work and the memory follow the directories the delta names (at most
    ``DELTA_MAX_DIRECTORIES``, see ``_check_names``) and not the paths times their depth.
    """
    seen = set()
    for parts in deletes:
        directory = parts[:-1]
        while directory and directory not in seen:
            seen.add(directory)
            directory = directory[:-1]
    return sorted(seen, key=len, reverse=True)


def _lstat(fd, name):
    try:
        return os.lstat(name, dir_fd=fd)
    except FileNotFoundError:
        return None


def _next_dir(fd, name, parts, count, deletes):
    """The directory ``name`` of ``fd`` (the ``count``-th component of ``parts``) as a new descriptor; None when it is missing or the delta deletes it first."""
    info = _lstat(fd, name)
    if info is None:
        return None
    if stat.S_ISDIR(info.st_mode):
        return os.open(name, _DIR_FLAGS, dir_fd=fd)
    if parts[:count] in deletes:  # only asked for a file or a link, so the usual path copies nothing
        return None  # a file or link that the delta deletes before it writes below it
    raise DeltaRejected("path")  # a link or a file where a directory has to be: nothing is written through it


def _parent_fd(root_fd, parts, deletes):
    """A descriptor for the directory that holds ``parts[-1]``, or None when it does not exist yet (or is deleted first)."""
    fd = os.dup(root_fd)
    try:
        for count, name in enumerate(parts[:-1], start=1):
            child = _next_dir(fd, name, parts, count, deletes)
            os.close(fd)
            fd = child
            if fd is None:
                return None
        opened, fd = fd, None  # handed to the caller, who closes it
        return opened
    finally:
        if fd is not None:
            os.close(fd)


def _vacated(writer, deletes, order):
    """The directories the deletions will leave empty, so that the apply removes them (``order`` is ``_prune_order``: bottom up)."""
    gone = set()
    for directory in order:
        dir_fd = writer.open_existing(directory)
        if dir_fd is None:
            continue
        try:
            names = os.listdir(dir_fd)
        finally:
            os.close(dir_fd)
        if all((*directory, name) in deletes or (*directory, name) in gone for name in names):
            gone.add(directory)
    return gone


def _check_op(root_fd, op, deletes, gone):
    """One record against the tree: no link or file in a directory's place, no directory in a file's place."""
    delete = op.kind == DELETE
    parent = _parent_fd(root_fd, op.parts, frozenset() if delete else deletes)
    if parent is None:
        return
    try:
        info = _lstat(parent, op.parts[-1])
    finally:
        os.close(parent)
    if info is not None and stat.S_ISDIR(info.st_mode) and (delete or op.parts not in gone):
        raise DeltaRejected("clash")


class ValidDelta:
    """A delta that passed ``validate_delta``. It holds a private copy of the bytes it checked, so nothing can change them.

    ``check_tree`` is read-only; ``apply`` writes. Use it as a context manager (or call ``close``): the copy is released.
    """

    def __init__(self, copy, ops, size, limits):
        self._copy = copy
        self.ops = tuple(ops)
        self.size = size
        self._limits = limits
        self.deletes = tuple(op.parts for op in self.ops if op.kind == DELETE)

    @functools.cached_property
    def prune_order(self):
        """The directories the apply tries to remove, deepest first: worked out once for ``check_tree`` and ``apply``."""
        return tuple(_prune_order(self.deletes))

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self._copy.close()

    @property
    def records(self):
        return len(self.ops)

    def check_tree(self, root_fd):
        """Raise ``DeltaRejected`` unless the tree under the directory ``root_fd`` is as the delta needs; writes nothing.

        Every directory above a path must be a real directory (or a file the delta deletes first), a path that
        is a directory may not be replaced by a file or a link (unless the deletions empty it), and a deletion
        may not name a directory. A deletion of a path that is already gone is fine (so a delta applies twice).
        """
        try:
            deletes = frozenset(self.deletes)
            gone = _vacated(TreeWriter(root_fd), deletes, self.prune_order)
            for op in self.ops:
                _check_op(root_fd, op, deletes, gone)
        except DeltaRejected:
            raise
        except BundleError as error:  # the tree could not be read (a link where a directory was expected)
            raise DeltaRejected(error.reason) from None
        except OSError:
            raise DeltaRejected("changed") from None

    def apply(self, root_fd):
        """Apply the delta to the tree under the directory ``root_fd``; returns ``Applied``. May stop part-way (``DeltaApplyFailed``).

        Deletions first (``unlinkat``), then the directories they emptied, then the files and links in stream
        order, each through ``TreeWriter`` (one component at a time with ``O_NOFOLLOW``, a temporary name
        created with ``O_EXCL``, a rename). A failure leaves the worktree as it is: nothing is rolled back.
        """
        try:
            return _apply(root_fd, self)
        except BundleError as error:
            raise DeltaApplyFailed(error.reason) from None
        except (OSError, ValueError):
            raise DeltaApplyFailed("write") from None

    def stream(self):
        """The checked bytes again, from the start."""
        self._copy.seek(0)
        return self._copy

    @property
    def limits(self):
        return self._limits


def _put(writer, record):
    if record.kind == SYMLINK:
        writer.replace_symlink(record.parts, record.read(pc.PATH_MAX_BYTES))
    else:
        writer.replace_file(record.parts, record.chunks(), _mode(record.executable, False))


def _apply(root_fd, delta):
    writer = TreeWriter(root_fd)
    for parts in delta.deletes:
        writer.unlink(parts)
    for directory in delta.prune_order:
        writer.prune(directory)
    pending = iter([op for op in delta.ops if op.kind != DELETE])
    counts = collections.Counter()
    for record in Reader(delta.stream(), DELTA, delta.limits):
        if record.kind == DELETE:
            continue
        op = next(pending, None)
        if op is None or (op.kind, op.parts) != (record.kind, record.parts):
            raise WireError("changed")  # cannot happen: the copy is private. Fail closed rather than write something unplanned.
        _put(writer, record)
        counts[record.kind] += 1
    return Applied(len(delta.ops), counts[FILE], counts[SYMLINK], len(delta.deletes), delta.size)


def _notify(checkpoint, stage):
    if checkpoint is not None:
        checkpoint(stage)


def apply_delta(
    source,
    worktree,
    *,
    write_scope,
    scratch=None,
    limits=None,
    declared_records=None,
    declared_bytes=None,
    checkpoint=None,
):
    """Validate the delta in the binary ``source`` and apply it to the worktree directory ``worktree``; returns ``Applied``.

    Three steps, each of which can only fail the whole delta: ``validate_delta`` (the stream), ``check_tree``
    (the tree, read-only) and ``apply``. The first two write nothing. The worktree directory is opened once
    (``O_NOFOLLOW``) and every path below it is resolved from that descriptor, one component at a time.
    ``write_scope`` is the session's (``cwd`` or ``none``): a read-only phase must return an empty delta.
    ``checkpoint`` is an optional callable that is called with ``"validated"`` and ``"checked"`` between the
    steps; the malicious-payload tests use it to change the tree in the gap and prove that nothing is written
    through what they planted. Raises ``DeltaRejected``, ``DeltaTooLarge`` or ``DeltaApplyFailed``, whose texts are fixed.
    """
    with validate_delta(
        source,
        write_scope=write_scope,
        scratch=scratch,
        limits=limits,
        declared_records=declared_records,
        declared_bytes=declared_bytes,
    ) as delta:
        _notify(checkpoint, "validated")
        if not delta.ops:
            return Applied(0, 0, 0, 0, delta.size)
        try:
            root_fd = os.open(os.fspath(worktree), _DIR_FLAGS)
        except OSError:
            raise DeltaRejected("worktree") from None
        try:
            delta.check_tree(root_fd)
            _notify(checkpoint, "checked")
            return delta.apply(root_fd)
        finally:
            os.close(root_fd)


# --- Producing the delta (pod side; the wrapper of slice 10b) ----------------------------------------


@dataclass(frozen=True)
class Produced:
    """What ``produce_delta`` wrote: the records (``declared_records`` of the report), the bytes (``declared_bytes``) and their kinds."""

    records: int
    files: int
    symlinks: int
    deleted: int
    size: int


def produce_delta(root_fd, baseline, sink):
    """Write the file delta of the tree below the directory ``root_fd`` against ``baseline`` to the binary ``sink``.

    ``baseline`` is what ``extract`` returned: tree path to ``(kind, exec bit, SHA-256)`` of what was shipped. A path
    whose kind, exec bit or hash differs is a ``file`` or ``symlink`` record, a baseline path that is gone is a
    ``delete``; ALL changed paths are in it, ignored and untracked ones too. It is the same module that the
    supervisor reads the delta with (``Writer`` and ``Reader`` with the direction ``delta``), so what is produced here is
    never refused for its form, only for what it says. The tree is read exactly as the supervisor reads a worktree
    (``walk``: descriptors, ``O_NOFOLLOW``, a fifo is never waited on, a git directory of any spelling is never
    entered, nothing is followed), because the worker owned every file in it.

    Nothing is truncated: a cap (the delta's bytes and records, one file, a path, the depth) is ``BundleTooLarge`` and a
    tree that changed while it was read is ``TreeError``; the sink then holds a partial stream that the caller must not
    send. The tree itself may be at most as large as a bundle can be (``LIMITS[BUNDLE]``), because only a tree that
    small could have been shipped in. Returns ``Produced``.
    """
    writer = Writer(sink, DELTA)
    seen = set()
    counts = collections.Counter()
    with contextlib.closing(walk(root_fd, AREA_TREE, Totals())) as entries:
        for entry in entries:
            seen.add(entry.path)
            if baseline.get(entry.path) == (entry.kind, entry.executable, digest_of(entry)):
                continue
            counts[entry.kind] += 1
            if entry.kind == SYMLINK:
                writer.put(SYMLINK, AREA_TREE, entry.path, entry.target)
                continue
            fd = open_file(entry)
            try:
                writer.put_file(AREA_TREE, entry.path, fd, entry.size, entry.executable)
            finally:
                os.close(fd)
    gone = sorted(set(baseline) - seen)
    for path in gone:
        writer.put(DELETE, AREA_TREE, path)
    return Produced(writer.records, counts[FILE], counts[SYMLINK], len(gone), writer.written)

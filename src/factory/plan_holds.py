"""A per-repository hold on an issue set whose ``plan`` keeps failing the same way.

``${FACTORY_DATA}/repos/<org>/<name>/plan-holds.json`` maps each sorted issue set the trigger loop planned to the last
``plan_failure`` code, how many plans in a row ended with that code, and a fingerprint of the issues' labels and
``updated_at`` as they were when the last of those plans started. Once the same code has occurred ``THRESHOLD`` times the
set is *held*: the loop does not plan it again while its fingerprint stays the same. A change of the fingerprint (a label
or an edit) lets exactly one more plan through; if that fails with the same code the set is held again. There is no timed
retry. A plan that ends well, or the deletion of the entry (or of the file), releases the hold.

The file holds issue numbers, codes from ``plan_failure.CODES``, counts and fingerprints (a hash): never issue text.
A file that is missing, unreadable, oversized, not JSON or not of this shape is an empty table and ``problem`` says why:
the loop keeps working from memory (so a file that cannot be written does not mean an endless replan either).
"""

import contextlib
import hashlib
import json
import os
import stat
import secrets
from pathlib import Path

from . import plan_failure

FILE = "plan-holds.json"
VERSION = 1
THRESHOLD = 2  # the same code this many times in a row holds the set
MAX_BYTES = 256 * 1024  # a larger file is not ours
MAX_ENTRIES = 256  # the oldest entries go first
MAX_COUNT = 1_000_000
MAX_SET = 64  # issue numbers in one key
FINGERPRINT_LENGTH = 32

OVERSIZED = "oversized"
UNREADABLE = "unreadable"
CORRUPT = "corrupt"


def key_of(numbers):
    """The entry key of an issue set: its numbers, sorted and distinct, comma separated."""
    return ",".join(str(number) for number in sorted(set(numbers)))


def _numbers_of(key):
    """The sorted numbers a key names, or None when it is not exactly what ``key_of`` makes."""
    if not isinstance(key, str) or not key or len(key) > 1024:
        return None
    try:
        numbers = [int(part) for part in key.split(",")]
    except ValueError:
        return None
    return numbers if key == key_of(numbers) and len(numbers) <= MAX_SET and min(numbers) >= 1 else None


def fingerprint_of(facts):
    """A hash of ``(number, labels, updated_at)`` for each issue; ``facts`` is a list of ``trigger.IssueFacts`` or ``(number, None)``.

    A label change, an edit (``updated_at``) or an issue that is no longer listed changes it. The labels are folded and
    sorted, so their order does not matter.
    """
    rows = []
    for item in facts:
        if isinstance(item, tuple):
            rows.append([item[0], None, None])
            continue
        labels = sorted({name.strip().casefold() for name in item.labels if isinstance(name, str)})
        rows.append([item.number, labels, item.updated])
    document = json.dumps(sorted(rows, key=lambda row: row[0]), separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(document.encode()).hexdigest()[:FINGERPRINT_LENGTH]


def _entry(value):
    """``(code, count, fingerprint)`` from one parsed entry, or None."""
    if not isinstance(value, dict):
        return None
    code, count, print_ = value.get("code"), value.get("count"), value.get("fingerprint")
    if code not in plan_failure.CODES or not isinstance(print_, str):
        return None
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= MAX_COUNT:
        return None
    if len(print_) > FINGERPRINT_LENGTH or not print_.isalnum() or not print_.isascii():
        return None
    return (code, count, print_)


def parse(raw):
    """``(entries, problem)`` from the bytes of a hold file; an entry that is not valid is dropped and reported as ``corrupt``."""
    try:
        document = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return {}, CORRUPT
    if not isinstance(document, dict) or document.get("version") != VERSION or not isinstance(document.get("sets"), dict):
        return {}, CORRUPT
    entries, problem = {}, None
    for key, value in document["sets"].items():
        entry = _entry(value)
        if _numbers_of(key) is None or entry is None or len(entries) >= MAX_ENTRIES:
            problem = CORRUPT
            continue
        entries[key] = entry
    return entries, problem


def _signature(path):
    """What identifies the file as it is now: None when it is missing, else its inode, size and modification time."""
    try:
        status = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        return "unreadable"
    return (status.st_ino, status.st_size, status.st_mtime_ns)


def _read(path):
    """``(entries, problem)`` of the file at ``path``."""
    try:
        # O_NONBLOCK: a FIFO at the path must not stop the trigger loop until a writer shows up.
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                return {}, UNREADABLE
            raw = os.read(descriptor, MAX_BYTES + 1)
        finally:
            os.close(descriptor)
    except FileNotFoundError:
        return {}, None
    except OSError:
        return {}, UNREADABLE
    if len(raw) > MAX_BYTES:
        return {}, OVERSIZED
    return parse(raw)


class PlanHolds:
    """The holds of one repository: the file's table, kept in memory and re-read whenever the file changes.

    Single-threaded: only the trigger pass touches it. ``problem`` is the reason the file could not be used on its last
    read (``oversized``, ``unreadable``, ``corrupt``), None otherwise; ``write_failed`` is True while the last write failed.
    The memory stays authoritative until the file changes under us (an operator edit, a restart), so a file that cannot be
    read or written never means the same failing plan is started again and again.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._entries = {}  # key -> (code, count, fingerprint), oldest first
        self._seen = None  # signature of the file as we last read or wrote it
        self.problem = None
        self.write_failed = False

    def refresh(self):
        """Take the file's table when it changed since we last read or wrote it; True when it was read and had a problem."""
        signature = _signature(self.path)
        if signature == self._seen:
            return False
        self._seen = signature
        if signature is None:
            self._entries = {}  # the file was removed: the operator released every hold
            self.problem = None
            return False
        self._entries, self.problem = _read(self.path)
        return self.problem is not None

    def held(self, numbers, print_):
        """The ``(code, count)`` that holds this set, or None: held while the same code occurred enough times and nothing changed."""
        entry = self._entries.get(key_of(numbers))
        if entry is None or entry[1] < THRESHOLD or entry[2] != print_:
            return None
        return entry[0], entry[1]

    def held_sets(self, print_of):
        """Every set held now, as ``{number: [sorted numbers of each held set that has it]}``; ``print_of(numbers)`` is the current fingerprint.

        Reads nothing but the table: it lets a caller skip the members of a held set before it spends anything on them.
        """
        members = {}
        for key, (_, count, stamp) in self._entries.items():
            if count < THRESHOLD:
                continue
            numbers = _numbers_of(key)
            if numbers is not None and print_of(numbers) == stamp:
                for number in numbers:
                    members.setdefault(number, []).append(numbers)
        return members

    def failed(self, numbers, code, print_):
        """Record one more failed plan; returns the count of the same code in a row (1 after a different code)."""
        code = code if code in plan_failure.CODES else plan_failure.OTHER
        key = key_of(numbers)
        before = self._entries.pop(key, None)
        count = before[1] + 1 if before is not None and before[0] == code else 1
        self._entries[key] = (code, min(count, MAX_COUNT), print_)
        while len(self._entries) > MAX_ENTRIES:
            del self._entries[next(iter(self._entries))]
        self._save()
        return count

    def rebase(self, numbers, before, after):
        """Move a held set's fingerprint from ``before`` to ``after`` (only while it is still ``before`` and the set is held)."""
        entry = self._entries.get(key_of(numbers))
        if entry is None or entry[1] < THRESHOLD or entry[2] != before or before == after:
            return
        self._entries[key_of(numbers)] = (entry[0], entry[1], after)
        self._save()

    def succeeded(self, numbers):
        """A plan for the set ended well: drop its entry."""
        if self._entries.pop(key_of(numbers), None) is not None:
            self._save()

    def _save(self):
        document = {
            "version": VERSION,
            "sets": {
                key: {"code": code, "count": count, "fingerprint": print_}
                for key, (code, count, print_) in self._entries.items()
            },
        }
        temp = self.path.with_name(f".{self.path.name}.{secrets.token_hex(4)}.tmp")
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
            try:
                with os.fdopen(descriptor, "w") as handle:
                    json.dump(document, handle, sort_keys=True)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, self.path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(temp)
                raise
        except OSError:
            self.write_failed = True
            return
        self.write_failed = False
        self.problem = None
        self._seen = _signature(self.path)

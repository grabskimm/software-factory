"""Read a run's logs for an operator: an allow-list of files, opened without following a link, shown as plain text.

``plan.log`` and ``supervisor.log`` of a run, the planner's transcript and, per job, the transcripts and stderr
of every worker invocation (``<job>/<attempt>-<phase>.jsonl|.stderr``) can be read through the manager
(docs/manager.md, "Log endpoints"). They hold agent-authored text and private repository content, so every
measure below exists because of that, and the endpoint is for the Operator role only (``manager.py``).

* **Allow-list.** Only the names matched by ``RUN_FILES`` and ``JOB_FILE`` are ever opened; the settings,
  prompt and result files that sit next to a transcript are not served, and no directory is ever listed
  beyond those names. A name that does not match is "not found", exactly like a file that does not exist.
* **No traversal, no links.** The run directory is reached from the data directory one component at a time
  with ``O_NOFOLLOW`` and a directory descriptor (``openat``), so a symlink at any depth is refused; the file
  is opened with ``O_NOFOLLOW`` and ``O_NONBLOCK`` (a FIFO cannot block the server), must be a regular file and
  must have exactly one link (a hard link to a file elsewhere is refused). Names and the run and job
  identifiers are matched by regular expression before any path is built.
* **Plain text.** The answer is ``text/plain; charset=utf-8`` with ``nosniff`` and a policy that allows
  nothing; no markup is ever produced. Text is decoded with replacement, ANSI sequences and other control
  characters are removed and secret-shaped strings are replaced (``redact.py``) before a byte leaves the
  process, on every line, JSONL transcripts included.
* **Bounded.** A response holds at most ``MAX_BODY`` bytes; a request examines at most ``SCAN_BUDGET`` bytes of
  the file and never reads it whole (windows of at most ``MAX_LIMIT`` raw bytes by ``pread``); a line longer
  than ``MAX_LINE`` is cut (back to a word boundary, so a token is not left half-cut) and says so. Windows
  start and end on line boundaries, so a secret inside a line is seen whole by the redaction.

Reads are by range: ``offset`` and ``limit`` forward, or ``tail`` (and ``before``) backward from the end. The
answer says, in headers, which bytes of the file it covers and whether more exists on either side.

Stdlib only. Importing this module has no side effects.
"""

from __future__ import annotations

import os
import re
import stat
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .redact import Redactor

RUN_FILES = re.compile(r"(?:plan\.log|supervisor\.log|planner\.jsonl|planner\.stderr)")
JOB_FILE = re.compile(
    r"(?:[0-9]{1,4}-(?:build|review|verify)(?:-(?:[0-9]{1,4}|baseline))?"
    r"|preflight-(?:build|verify)-[0-9]{8}T[0-9]{6,12})\.(?:jsonl|stderr)"
)
MAX_LISTED = 200  # files named in one listing

DEFAULT_LIMIT = 64 * 1024
MIN_LIMIT = 1024
MAX_LIMIT = 256 * 1024  # raw bytes examined for one window
MAX_BODY = 512 * 1024  # bytes of text in one answer (decoding can widen a raw window)
MAX_LINE = 32 * 1024  # a longer line is shown cut, with its length
SCAN_BUDGET = 2 * 1024 * 1024  # bytes a request may look at to find a line boundary
CHUNK = 64 * 1024
MAX_OFFSET = 2**53
QUIET_SECONDS = 3.0  # a file not written for this long has no line in progress: its unfinished last line is shown

_BOUNDARY = " \t\"',;"
_BACKOFF = 256


class LogNotFound(Exception):
    """No such log: a name outside the allow-list, a missing file, or anything that is not a plain file."""


@dataclass(frozen=True)
class Window:
    """One answer: the text and the raw bytes of the file it covers (``start`` up to ``end``)."""

    text: str
    start: int
    end: int
    size: int
    modified: float
    partial: bool = False  # an unfinished last line was left out: the writer is mid-line, ``end`` is that line's start

    @property
    def more_before(self) -> bool:
        return self.start > 0

    @property
    def more_after(self) -> bool:
        return self.end < self.size

    def body(self) -> bytes:
        return self.text.encode("utf-8")


# --- opening --------------------------------------------------------------------------------

_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def enter_directory(parent: int, name: str) -> int:
    try:
        return os.open(name, _DIR, dir_fd=parent)
    except OSError:
        raise LogNotFound from None


def open_directory(base: Path, parts: tuple) -> int:
    """A descriptor of ``base/<parts>``, each part entered with ``O_NOFOLLOW`` from the one before; the caller closes it.

    ``base`` is the operator's data directory (it may itself be a mount or a link); everything below it must be
    a plain directory. The parts are single names the caller has already matched against a pattern.
    """
    try:
        current = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        raise LogNotFound from None
    try:
        for part in parts:
            if not part or part in (".", "..") or "/" in part or "\0" in part:
                raise LogNotFound
            child = enter_directory(current, part)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def open_file(directory: int, name: str) -> int:
    """A read-only descriptor of a regular file with one link, without following a link or blocking."""
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
    except OSError:
        raise LogNotFound from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise LogNotFound
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def list_files(directory: int, shape: re.Pattern) -> list:
    """``[{"name", "size", "modified"}]`` of the allow-listed regular files of an open directory, by name."""
    try:
        names = sorted(entry.name for entry in os.scandir(directory) if shape.fullmatch(entry.name))
    except OSError:
        raise LogNotFound from None
    files = []
    for name in names[:MAX_LISTED]:
        try:
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            files.append({"name": name, "size": info.st_size, "modified": _iso(info.st_mtime)})
    return files


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- reading --------------------------------------------------------------------------------


def _pread(descriptor: int, count: int, at: int) -> bytes:
    try:
        return os.pread(descriptor, count, at)
    except OSError:
        raise LogNotFound from None


def _next_line_start(descriptor: int, position: int, size: int) -> int:
    """The offset after the next newline at or after ``position`` (``size`` at the end; a long run is cut at the budget)."""
    scanned = 0
    while position < size and scanned < SCAN_BUDGET:
        chunk = _pread(descriptor, min(CHUNK, size - position), position)
        if not chunk:
            return size
        index = chunk.find(b"\n")
        if index >= 0:
            return position + index + 1
        position += len(chunk)
        scanned += len(chunk)
    return min(position, size)


def _line_start_before(descriptor: int, position: int) -> int | None:
    """The offset after the last newline before ``position``, looking back at most ``SCAN_BUDGET`` bytes."""
    floor = max(0, position - SCAN_BUDGET)
    while position > floor:
        low = max(floor, position - CHUNK)
        chunk = _pread(descriptor, position - low, low)
        index = chunk.rfind(b"\n")
        if index >= 0:
            return low + index + 1
        position = low
    return 0 if floor == 0 else None


def _is_line_start(descriptor: int, position: int) -> bool:
    return position == 0 or _pread(descriptor, 1, position - 1) == b"\n"


def _cut_back(text: str) -> str:
    """A line cut at ``MAX_LINE``: back to the last separator of its final stretch, so no token is left half-cut."""
    floor = max(0, len(text) - _BACKOFF)
    index = max(text.rfind(mark, floor) for mark in _BOUNDARY)
    return text[: index if index > 0 else floor]


def render_line(redactor: Redactor, raw: bytes, length: int, cap: int = MAX_LINE) -> str | None:
    """One line, cleaned and redacted, cut at ``cap`` bytes (``length`` is its real length); None for a dropped line."""
    cut = length > cap
    text = raw[:cap].decode("utf-8", errors="replace")
    if cut:
        text = _cut_back(text)
    out = redactor.process(text)
    if out is None:
        return None
    return f"{out} [line cut: {length} bytes]" if cut else out


def is_jsonl(name: str) -> bool:
    """Whether a log is a JSONL transcript: one event per line, so a private key block never spans lines."""
    return name.endswith(".jsonl")


def read_window(descriptor: int, *, offset: int | None = None, limit: int = DEFAULT_LIMIT, tail: int | None = None,
                before: int | None = None, jsonl: bool = False, now: float | None = None) -> Window:
    """A window of the file: forward from ``offset`` for ``limit`` bytes, or the last ``tail`` bytes before ``before``.

    The window is aligned to lines: a start inside a line moves to the next line start, an end inside a line
    moves back to the previous line end. The unfinished last line of the whole file is the one exception, and only for a
    file that has been quiet for ``QUIET_SECONDS``: while a writer may still be mid-line it is left out and the window ends
    at its start (``Window.partial``), so that the next forward read, which the server aligns to a line start, begins exactly
    there and the rest of the line is never lost. ``now`` is the clock (a test passes a fixed one).
    ``start``, ``end`` and ``size`` of the result are raw byte offsets of the file. ``jsonl`` says the file is a
    JSONL transcript (``is_jsonl``): no redaction state is carried from one line to the next there.
    """
    info = os.fstat(descriptor)
    size, modified = info.st_size, info.st_mtime
    redactor = Redactor(spans_lines=not jsonl)
    quiet = ((time.time() if now is None else now) - modified) >= QUIET_SECONDS
    if tail is None:
        position = offset or 0
        if position >= size:
            return Window("", size, size, size, modified)
        if not _is_line_start(descriptor, position):
            position = _next_line_start(descriptor, position, size)
            if position >= size:
                return Window("", size, size, size, modified)
        count = max(min(limit, MAX_LIMIT), MAX_LINE + 1)
        data = _pread(descriptor, min(count, size - position), position)
        last = position + len(data) >= size
        if b"\n" not in data and not last:
            # a single line longer than the window: show its head, then continue after it
            following = _next_line_start(descriptor, position + len(data), size)
            text = render_line(redactor, data, following - position - 1) or ""
            return Window(text, position, following, size, modified)
        return _finish(data, position, last, size, modified, True, min(limit, MAX_LIMIT), jsonl, quiet)
    end = size if before is None else min(before, size)
    low = max(0, end - min(tail, MAX_LIMIT))
    if end == 0:
        return Window("", 0, 0, size, modified)
    lead = 1 if low > 0 else 0
    data = _pread(descriptor, end - low + lead, low - lead)
    aligned = lead == 0 or data[:1] == b"\n"
    data = data[lead:]
    if end < size and data[-1:] != b"\n":
        cut = data.rfind(b"\n") + 1
        data = data[:cut]
        end = low + cut
    if not aligned:
        index = data.find(b"\n")
        if index < 0 or index + 1 >= len(data):  # no whole line in the window: its last line is longer than it
            return _long_line(descriptor, end, size, modified, data.endswith(b"\n"), jsonl)
        low += index + 1
        data = data[index + 1:]
    return _finish(data, low, end >= size, size, modified, False, MAX_LIMIT, jsonl, quiet)


def _long_line(descriptor: int, end: int, size: int, modified: float, newline: bool, jsonl: bool) -> Window:
    """The window held no whole line: the last line is longer than it. Show the head of that line, cut."""
    first = _line_start_before(descriptor, end - 1 if newline else end)
    if first is None:  # longer than the scan budget: nothing is shown, and the range says where it ends
        return Window("", end, end, size, modified)
    head = _pread(descriptor, min(MAX_LINE + 1, end - first), first)
    text = render_line(Redactor(spans_lines=not jsonl), head, end - first - (1 if newline else 0)) or ""
    return Window(text, first, end, size, modified)


def _finish(data: bytes, start: int, last: bool, size: int, modified: float, forward: bool, limit: int,
            jsonl: bool, quiet: bool = True) -> Window:
    """Render the lines of ``data`` (which begins at file offset ``start``) into a window.

    Only complete lines are used, except that a final line without a newline is used when ``last`` (the data
    runs to the end of the file) and ``quiet`` (nobody wrote to the file lately: otherwise it is left out and the
    window ends where that line starts, so a later read from ``end`` returns the whole line). Forward, lines are used up to ``limit`` raw bytes (at least one); either way
    whole lines are dropped from the far side until the text fits ``MAX_BODY`` (decoding can widen a window). A
    window that ends inside a private key block says how many lines it withheld.
    """
    redactor = Redactor(spans_lines=not jsonl)
    parts = data.split(b"\n")
    final = parts.pop()  # what follows the last newline: empty when the data ends with one
    open_end = bool(final) and last and quiet
    withheld = bool(final) and last and not quiet
    if open_end:
        parts.append(final)
    pieces = []  # (text or None for a dropped key line, raw bytes of the line with its newline)
    used = 0
    for index, part in enumerate(parts):
        total = len(part) + (0 if open_end and index == len(parts) - 1 else 1)
        if forward and pieces and used + total > limit:
            break
        pieces.append((render_line(redactor, part, len(part)), total))
        used += total

    sizes = [0 if text is None else len(text.encode("utf-8")) + 1 for text, _ in pieces]
    total = sum(sizes)
    low, high = 0, len(pieces)
    while total - 1 > MAX_BODY and high - low > 1:
        if forward:
            high -= 1
            total -= sizes[high]
            used -= pieces[high][1]
        else:
            total -= sizes[low]
            used -= pieces[low][1]
            start += pieces[low][1]
            low += 1
    shown = [piece[0] for piece in pieces[low:high] if piece[0] is not None]
    marker = redactor.finish()
    if marker is not None and high == len(pieces):  # a window cut short at the far side does not know its own end
        shown.append(marker)
    return Window("\n".join(shown), start, start + used, size, modified, withheld and high == len(pieces))


def view(descriptor: int, params: dict, jsonl: bool = False) -> Window:
    """A window for the query of a request: ``offset`` and ``limit``, or ``tail`` and ``before``; the default is the tail."""
    if "tail" in params or "before" in params or "offset" not in params:
        return read_window(descriptor, tail=params.get("tail", DEFAULT_LIMIT), before=params.get("before"), jsonl=jsonl)
    return read_window(descriptor, offset=params["offset"], limit=params.get("limit", DEFAULT_LIMIT), jsonl=jsonl)


def _offset(value: str) -> int:
    if not value.isascii() or not value.isdigit() or int(value) > MAX_OFFSET:
        raise ValueError(value)
    return int(value)


def _count(top: int):
    def check(value: str) -> int:
        if not value.isascii() or not value.isdigit() or not 1 <= int(value) <= top:
            raise ValueError(value)
        return int(value)

    return check


def parse_view(parse_query, target: str) -> dict:
    """The validated range query of a log request (``parse_query`` is ``dashboard.parse_query``).

    ``offset`` with an optional ``limit`` reads forward; ``tail`` (the last bytes) with an optional ``before``
    (an offset to read up to) reads backward; the two forms cannot be mixed. No parameter reads the tail.
    """
    from .dashboard import QueryError

    params = parse_query(
        target, {"offset": _offset, "limit": _count(MAX_LIMIT), "tail": _count(MAX_LIMIT), "before": _offset}
    )
    forward = "offset" in params or "limit" in params
    backward = "tail" in params or "before" in params
    if (forward and backward) or ("limit" in params and "offset" not in params):
        raise QueryError("query")
    return params


def headers_for(window: Window) -> dict:
    """The headers of a log answer: plain text that nothing can render, never cached, and the range it covers."""
    return {
        "Content-Type": "text/plain; charset=utf-8",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; sandbox",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Content-Disposition": 'inline; filename="log.txt"',
        "X-Log-Size": str(window.size),
        "X-Log-Start": str(window.start),
        "X-Log-End": str(window.end),
        "X-Log-More-Before": "true" if window.more_before else "false",
        "X-Log-More-After": "true" if window.more_after else "false",
        "X-Log-Partial": "true" if window.partial else "false",
    }


def open_log(base: Path, parts: tuple, name: str, shape: re.Pattern) -> int:
    """A descriptor of one allow-listed log file under ``base/<parts>`` (the caller closes it), or ``LogNotFound``."""
    if not shape.fullmatch(name):
        raise LogNotFound
    directory = open_directory(base, parts)
    try:
        return open_file(directory, name)
    finally:
        os.close(directory)

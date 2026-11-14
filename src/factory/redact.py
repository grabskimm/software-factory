"""Make a log line safe to show or to keep: control sequences removed, secret-shaped strings replaced.

Worker transcripts, stderr and the supervisor and plan logs can hold agent-authored text, private repository
content and, by accident, a credential an agent printed or a tool echoed. Everything that leaves a run's
logs (the log endpoints of ``logs.py`` and the retained copies of ``logarchive.py``) goes through here first.

Two steps, in this order, on every line:

1. ``clean``: ANSI escape sequences (and their escaped spelling inside a JSON string), every other control
   character, the line and paragraph separators and the bidirectional overrides are removed, so a log can
   neither move a terminal cursor nor reorder the text a person reads. Cleaning comes first so a token cannot
   be split by an escape sequence to slip past the patterns.
2. ``Redactor.process``: strings with the shape of a credential become ``[REDACTED]``: GitHub tokens
   (``ghp_``, ``gho_``, ``ghu_``, ``ghs_``, ``ghr_``, ``github_pat_``), Anthropic keys (``sk-ant-``), private key
   blocks (``Redactor``: in a plain text log a block that spans lines is tracked from line to line, within a bound
   and with a visible marker; a JSONL event is one line, so there a block never spans lines), ``Authorization``
   and API key header values, ``Bearer`` values, values of names that end in token, secret, password or key, Azure shared access
   signatures (``sig=``), JWT-shaped strings and a line that is nothing but a long base64 run (the body of a
   key block seen without its first line).

This is **defence in depth, not a guarantee**: it matches shapes, so a secret with an unusual shape, one
split across lines, or one a model re-encoded is not caught (docs/manager.md, "Log endpoints"). Every step is
linear in the length of the line (no nested repetition; a private key block is found with one search for its
start and one for its end, never a pattern that scans on to the end of the line for every start), so a hostile
line cannot make it slow.

Stdlib only. Importing this module has no side effects.
"""

from __future__ import annotations

import re

MARK = "[REDACTED]"

# --- cleaning -------------------------------------------------------------------------------

_ANSI = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_]|[ -/]+[0-~])"
    r"|\x9b[0-?]*[ -/]*[@-~]"
)
# The same sequences as they appear inside a JSON string or a quoted repr (backslash, not a real ESC byte).
_ESCAPED_ANSI = re.compile(r"\\(?:u001[bB]|x1[bB]|033|e)\[[0-9;?]*[ -/]*[@-~]")
# C0 except tab (a line never holds a newline), DEL, C1, separators, zero-width and bidirectional controls.
_CONTROL = re.compile(
    "[\x00-\x08\x0a-\x1f\x7f-\x9f\u2028\u2029\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]"
)


def clean(text: str) -> str:
    """``text`` without ANSI sequences, control characters and bidirectional overrides."""
    stripped = _ESCAPED_ANSI.sub("", _ANSI.sub("", text))
    return _CONTROL.sub("", stripped)


# --- redaction ------------------------------------------------------------------------------

_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----")
_PEM_END = re.compile(r"-----END [A-Z0-9 ]{0,40}PRIVATE KEY-----")
_BASE64_LINE = re.compile(r"\s*[A-Za-z0-9+/]{60,}={0,2}\s*")
_HEX_LINE = re.compile(r"\s*[0-9a-fA-F]+\s*")

# A key block that is still open after this many lines or bytes was not a key (even an 8192-bit RSA key is about
# 110 lines and 7 KiB): the lines after that are shown again, and the dropped ones are counted in a visible marker.
MAX_KEY_LINES = 200
MAX_KEY_BYTES = 16 * 1024

# Replaced whole.
_SHAPES = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{16,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}"),
)
# Keep the name and the separator, replace the value: ``\1`` is the part to keep.
_VALUES = (
    re.compile(
        r"(?i)((?:proxy-)?authorization[\\\"']{0,3}\s*[:=]\s*[\\\"']{0,3})"
        r"(?:(?:bearer|basic|token|negotiate|digest)\s+)?[^\s\"'\\,;]{4,}"
    ),
    re.compile(
        r"(?i)((?:x-api-key|x-ms-client-principal|api-key|apikey)[\\\"']{0,3}\s*[:=]\s*[\\\"']{0,3})[^\s\"'\\,;]{4,}"
    ),
    re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(
        r"(?i)((?:token|secret|password|passwd|api[_-]?key|private[_-]?key|access[_-]?key|account[_-]?key"
        r"|credentials?)[\\\"']{0,3}\s*[:=]\s*[\\\"']{0,3})[A-Za-z0-9._~+/=%-]{8,}"
    ),
    re.compile(r"(?i)(\bsig=)[A-Za-z0-9%+/=_-]{16,}"),
)


def _string_end(text: str, start: int) -> int:
    """Where the JSON string that holds ``start`` ends: its first quote that no backslash escapes, else the end of ``text``."""
    position = start
    while True:
        quote = text.find('"', position)
        if quote < 0:
            return len(text)
        slashes = quote
        while slashes > start and text[slashes - 1] == "\\":
            slashes -= 1
        if (quote - slashes) % 2 == 0:
            return quote
        position = quote + 1


def _redact_blocks(text: str, json_line: bool) -> tuple[str, bool]:
    """``text`` with its private key blocks replaced by ``MARK``, and whether it ends inside a block with no end.

    Linear in the length of the line: one search for each start marker and, until one fails, one for its end (a
    start with no end after it means no later start has one either, so the search is never repeated). A block
    that is not finished runs to the end of the line; in a JSON line (``json_line``) only to the end of the
    string that holds it, so the line stays valid JSON and what follows it is still checked.
    """
    out, position, ends_left = [], 0, True
    while True:
        begin = _PEM_BEGIN.search(text, position)
        if begin is None:
            out.append(text[position:])
            return "".join(out), False
        out.append(text[position:begin.start()])
        out.append(MARK)
        end = _PEM_END.search(text, begin.end()) if ends_left else None
        if end is not None:
            position = end.end()
            continue
        ends_left = False
        if not json_line:
            return "".join(out), True
        position = _string_end(text, begin.end())


def _withheld(count: int) -> str:
    return f"[{count} {'line' if count == 1 else 'lines'} withheld]"


class Redactor:
    """Redacts line by line and, in a plain text log, remembers whether a private key block is open across lines.

    One instance per text (a file, a response window). ``process`` returns the cleaned and redacted line, or
    None for a line that belongs to a key block and is dropped. The open block is bounded (``MAX_KEY_LINES``
    lines, ``MAX_KEY_BYTES`` bytes): a start marker with no end, such as a line that only mentions the marker,
    must not swallow the rest of a log. When the bound is passed, or the text ends inside a block (``finish``),
    the number of dropped lines is shown. ``spans_lines=False`` is for JSONL, where an event is one line and a
    key can only be inside one: nothing is carried from line to line there.
    """

    def __init__(self, *, spans_lines: bool = True) -> None:
        self.spans_lines = spans_lines
        self.in_key = False
        self._held = 0  # lines dropped in the open block
        self._held_bytes = 0

    def process(self, line: str) -> str | None:
        text = clean(line)
        if not self.in_key:
            return self._line(text)
        end = _PEM_END.search(text)
        if end is None:
            self._held += 1
            self._held_bytes += len(text)
            if self._held <= MAX_KEY_LINES and self._held_bytes <= MAX_KEY_BYTES:
                return None
            dropped = self._held - 1  # this line is not dropped: it is shown, redacted like any other
            self.in_key = False
            shown = self._line(text)
            return f"{_withheld(dropped)} {shown}" if dropped else shown
        self.in_key = False
        rest = self._line(text[end.end():])
        return None if not rest else f"{MARK}{rest}"

    def finish(self) -> str | None:
        """The marker line for dropped lines of a block still open at the end of the text, else None."""
        dropped, self._held, self._held_bytes = self._held, 0, 0
        if not self.in_key:
            return None
        self.in_key = False
        return _withheld(dropped) if dropped else None

    def _line(self, text: str) -> str:
        """A line that is outside a key block: redacted, and it may open one."""
        text, unfinished = _redact_blocks(text, not self.spans_lines)
        if unfinished and self.spans_lines:
            self.in_key, self._held, self._held_bytes = True, 0, 0
        elif not unfinished and _BASE64_LINE.fullmatch(text) and not _HEX_LINE.fullmatch(text):
            return MARK
        for shape in _SHAPES:
            text = shape.sub(MARK, text)
        for value in _VALUES:
            text = value.sub(rf"\g<1>{MARK}", text)
        return text


def redact_text(text: str, *, spans_lines: bool = True) -> str:
    """One string, possibly several lines, cleaned and redacted (a dropped key body line leaves no line)."""
    redactor = Redactor(spans_lines=spans_lines)
    kept = []
    for line in text.split("\n"):
        out = redactor.process(line)
        if out is not None:
            kept.append(out)
    marker = redactor.finish()
    if marker is not None:
        kept.append(marker)
    return "\n".join(kept)

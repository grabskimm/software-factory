"""Classify the API error behind a failed ``claude`` session into one closed code (issue #146).

A spend-limit 429 carries no ``retry-after`` and retrying it cannot succeed until a person raises the
limit; a rate-limit 429 carries one and clears by itself. The two must not be told apart by guessing,
so the rules are DATA (``SIGNATURES``), each row naming the fields it reads, and anything that no row
recognises is ``other``, never ``spend_limit``.

The codes (a closed set of fixed words, also the values of the metric label ``code``):

* ``spend_limit``: a 429 whose error type is the documented spend-cap one.
* ``rate_limit``: a 429 whose class is a rate limit (its error type, or a ``retry-after`` value).
* ``overloaded``: the API says it is overloaded (529, or an ``overloaded_error`` type).
* ``auth``: the key was refused (401).
* ``rate_limit_unknown``: a 429 that no row recognises.
* ``other``: an API error (a status or an error type is present) that no row recognises.
* ``none``: the event carries no API-error evidence at all (an ordinary failed task, a success).

What is trusted. The fields read are the ones the CLI itself sets (``api_error_status``,
``terminal_reason``, ``subtype``, an ``error`` type). The free-form ``result`` text can hold model
output, so it is searched for the fixed type words ONLY when an integer status is present (the model
cannot set that), is bounded, and is never copied: the classifier returns a code and nothing else, so
no agent text reaches the state, a log line or a metric.

The real ``claude`` error shape for a spend limit, a 401 and a 429 is UNVERIFIED (the shapes below
are the ones the design lists); the recordings of a live run replace them (docs/capacity.md).
"""

from __future__ import annotations

import json
import re
from collections import namedtuple

SPEND_LIMIT = "spend_limit"
RATE_LIMIT = "rate_limit"
OVERLOADED = "overloaded"
AUTH = "auth"
RATE_LIMIT_UNKNOWN = "rate_limit_unknown"
OTHER = "other"
NONE = "none"
CODES = (SPEND_LIMIT, RATE_LIMIT, OVERLOADED, AUTH, RATE_LIMIT_UNKNOWN, OTHER, NONE)

# The fields a signature may read. ``result`` is free text: see the module docstring.
STATUS = "api_error_status"
TERMINAL = "terminal_reason"
SUBTYPE = "subtype"
ERROR = "error"
RESULT = "result"
# A second spelling of the status that a stream event (not the final result) may use.
STATUS_FIELDS = (STATUS, "error_status")
RETRY_AFTER_FIELDS = ("retry_after", "retry-after", "retry_after_seconds", "retry_after_ms")

MAX_TEXT = 4096  # characters of ``result`` that are searched
MAX_TAIL_LINES = 200  # lines of a log tail that are looked at, newest first
MAX_DEPTH = 3  # nesting of an ``error`` object that is read
MAX_NUMBER = 64  # characters of a ``retry-after`` string that is parsed
# A plain decimal in ASCII digits. ``str.isdigit`` is not used: it accepts superscripts and circled digits that
# ``float`` refuses.
_DECIMAL = re.compile(r"(?:[0-9]+\.?[0-9]*|\.[0-9]+)")

# ``code``: what the row says; ``statuses``: the HTTP statuses it applies to; ``markers``: fixed words, any of
# which in a field the row reads makes it match; ``retry_after``: a ``retry-after`` value alone makes it match;
# ``reads``: the fields it looks at. A row with no markers and no ``retry_after`` matches on the status alone.
# The first row that matches wins, so the order is part of the table.
Signature = namedtuple("Signature", ("code", "statuses", "markers", "retry_after", "reads"))
SIGNATURES = (
    Signature(SPEND_LIMIT, (429,), ("enforced_spend_limit_reached",), False, (STATUS, TERMINAL, SUBTYPE, ERROR, RESULT)),
    Signature(AUTH, (401,), (), False, (STATUS,)),
    Signature(OVERLOADED, (529,), (), False, (STATUS,)),
    Signature(OVERLOADED, (500, 503), ("overloaded_error",), False, (STATUS, ERROR, RESULT)),
    Signature(RATE_LIMIT, (429,), ("rate_limit_error",), True, (STATUS, TERMINAL, SUBTYPE, ERROR, RESULT)),
)


def _status(event):
    """The HTTP status the event carries (an int, or three digits), else None. A bool is not a status."""
    for name in STATUS_FIELDS:
        value = event.get(name)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and 100 <= value <= 599:
            return value
        if isinstance(value, str) and len(value) == 3 and value.isascii() and value.isdigit():
            return int(value)
    return None


def _walk_error(value, depth=0):
    """The ``type`` and ``code`` strings of an ``error`` value (a string, or a mapping that may nest one)."""
    if isinstance(value, str):
        return [value[:200]]
    found = []
    if isinstance(value, dict) and depth < MAX_DEPTH:
        for name in ("type", "code"):
            if isinstance(value.get(name), str):
                found.append(value[name][:200])
        found.extend(_walk_error(value.get("error"), depth + 1))
    return found


def _retry_after(event):
    """Whether a ``retry-after`` value is present on the event or on its ``error`` object."""
    for holder in (event, event.get(ERROR)):
        if not isinstance(holder, dict):
            continue
        for name in RETRY_AFTER_FIELDS:
            value = holder.get(name)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)) and value > 0:
                return True
            if isinstance(value, str) and _positive_decimal(value):
                return True
    return False


def _positive_decimal(text):
    """Whether ``text`` is a positive decimal written in ASCII digits (no sign, no exponent, no other script)."""
    stripped = text.strip()
    if len(stripped) > MAX_NUMBER or not _DECIMAL.fullmatch(stripped):
        return False
    return float(stripped) > 0


def _words(event, reads, status):
    """The lowercase strings of the fields ``reads`` names. Free text only under an integer status."""
    words = []
    for name in reads:
        if name == TERMINAL and isinstance(event.get(TERMINAL), str):
            words.append(event[TERMINAL][:200])
        elif name == SUBTYPE and isinstance(event.get(SUBTYPE), str):
            words.append(event[SUBTYPE][:200])
        elif name == ERROR:
            words.extend(_walk_error(event.get(ERROR)))
        elif name == RESULT and status is not None and isinstance(event.get(RESULT), str):
            words.append(event[RESULT][:MAX_TEXT])
    return [word.lower() for word in words]


def _evidence(event, status):
    """Whether the event shows an API error at all: a status, or an ``error`` type string."""
    return status is not None or bool(_walk_error(event.get(ERROR)))


def _classify_event(event):
    status = _status(event)
    if not _evidence(event, status):
        return NONE
    retry_after = _retry_after(event)
    for row in SIGNATURES:
        if status not in row.statuses:
            continue
        if not row.markers and not row.retry_after:
            return row.code
        words = _words(event, row.reads, status)
        if any(marker in word for marker in row.markers for word in words):
            return row.code
        if row.retry_after and retry_after:
            return row.code
    return RATE_LIMIT_UNKNOWN if status == 429 else OTHER


def _tail_events(log_tail):
    """The JSON objects of a log tail, newest first; text that is not a JSON object line is skipped."""
    text = log_tail
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8", errors="replace")
    if not isinstance(text, str):
        return
    for raw in reversed(text.splitlines()[-MAX_TAIL_LINES:]):
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(event, dict):
            yield event


def _safe_classify_event(event):
    """``_classify_event``, but a fault in it is ``none``: it must never replace the failure being reported."""
    try:
        return _classify_event(event)
    except Exception:  # whatever a hostile value does inside the classifier, the caller still gets a code
        return NONE


def classify(event, log_tail=None):
    """The code of the API error behind a failed session: ``event`` is the final ``result`` event.

    When there is no result event (``None``) and a bounded ``log_tail`` of the stream is given, the newest
    object of the tail that shows an API error decides. Never raises for a malformed value: it is ``none``.
    """
    if isinstance(event, dict):
        return _safe_classify_event(event)
    for candidate in _tail_events(log_tail):
        code = _safe_classify_event(candidate)
        if code != NONE:
            return code
    return NONE

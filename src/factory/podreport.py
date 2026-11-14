"""The report a pod worker posts with its file delta, and the one function that checks it.

Slice 10b of the pod backend (``docs/pod-backend.md``). ``POST /v1/result`` carries a report (one JSON
object of at most 64 KiB, ``relay.REPORT_MAX_BYTES``) and the delta. The relay only checks that the report
is a JSON object; what it says is the business of the supervisor, which reads ``out/report.json`` with
``validate`` before it trusts a word of it (slice 12b). The wrapper (``podworker.py``) builds it with ``build``
and ``dumps``. One module for both sides, so they cannot diverge.

The report is data from a pod, so it is hostile input to the supervisor: the key set is closed, every value
is a bool, a number, a null or a word of a fixed list, a bool is never a number, and nothing in it is free
text, a path, a token or a word of the agent. ``validate`` raises ``ValueError("report")`` (a fixed text, the
input is never echoed) and returns a new dict of exactly the documented shape.

======================  =========================================================================================
``version``             1
``outcome``             ``exited`` (the agent ended by itself, whatever its code), ``timeout`` (the session's
                        time limit, or the token deadline less the reserve, was reached and the group was killed),
                        ``terminated`` (SIGTERM: the pod is being deleted or its deadline passed; the session was
                        stopped and drained) or ``memory_limit`` (the memory guard killed it)
``exit_code``           the agent's exit status; a negative number is the signal that ended it
``footprint``           ``memwatch.Watchdog.footprint()`` (``peak_rss_mb``, ``cpu_seconds``, ``samples``,
                        ``limit_mb``) or null when nothing was sampled
``exceeded_mb``         resident MiB of the sample over the limit, or null
``delta``               ``status`` (``ok``; ``too_large`` a cap was reached; ``failed`` the tree could not be read
                        safely), ``records`` and ``bytes`` (the numbers the supervisor passes to ``apply_delta`` as
                        ``declared_records`` and ``declared_bytes``: the stream has no trailer, so a stream cut at a
                        record boundary would otherwise read as a shorter delta). Unless ``ok`` the delta part of the
                        request is empty (zero bytes, not even a header) and both numbers are 0
``streamed``            the bytes of the transcript and of the standard error the relay accepted (a check against the
                        size of ``out/transcript`` and ``out/stderr``)
``dropped``             true for a stream whose data the wrapper had to discard because the relay did not answer
======================  =========================================================================================

Standard library only.
"""

import json

VERSION = 1
MAX_BYTES = 64 * 1024  # the relay's REPORT_MAX_BYTES (a test pins that they agree)

OUTCOMES = ("exited", "timeout", "terminated", "memory_limit")
DELTA_STATUSES = ("ok", "too_large", "failed")
STREAMS = ("transcript", "stderr")

KEYS = ("version", "outcome", "exit_code", "footprint", "exceeded_mb", "delta", "streamed", "dropped")
DELTA_KEYS = ("status", "records", "bytes")
FOOTPRINT_KEYS = ("peak_rss_mb", "cpu_seconds", "samples", "limit_mb")

_INTEGER_MAX = 1 << 48
_EXIT_MIN, _EXIT_MAX = -128, 255


def _fail():
    raise ValueError("report")


def _integer(value, low=0, high=_INTEGER_MAX):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        _fail()
    return value


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= _INTEGER_MAX:
        _fail()
    return value


def _closed(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        _fail()
    return value


def _footprint(value):
    if value is None:
        return None
    _closed(value, FOOTPRINT_KEYS)
    limit = value["limit_mb"]
    return {
        "peak_rss_mb": _number(value["peak_rss_mb"]),
        "cpu_seconds": _number(value["cpu_seconds"]),
        "samples": _integer(value["samples"]),
        "limit_mb": None if limit is None else _integer(limit, 1),
    }


def _delta(value):
    _closed(value, DELTA_KEYS)
    if value["status"] not in DELTA_STATUSES:
        _fail()
    records, size = _integer(value["records"]), _integer(value["bytes"])
    if value["status"] != "ok" and (records or size):
        _fail()
    return {"status": value["status"], "records": records, "bytes": size}


def _flags(value, check):
    _closed(value, STREAMS)
    return {name: check(value[name]) for name in STREAMS}


def _boolean(value):
    if not isinstance(value, bool):
        _fail()
    return value


def validate(document):
    """The report as a new dict of the documented shape; ``ValueError("report")`` when it is anything else."""
    _closed(document, KEYS)
    if document["version"] != VERSION or isinstance(document["version"], bool):
        _fail()
    if document["outcome"] not in OUTCOMES:
        _fail()
    exceeded = document["exceeded_mb"]
    return {
        "version": VERSION,
        "outcome": document["outcome"],
        "exit_code": _integer(document["exit_code"], _EXIT_MIN, _EXIT_MAX),
        "footprint": _footprint(document["footprint"]),
        "exceeded_mb": None if exceeded is None else _integer(exceeded),
        "delta": _delta(document["delta"]),
        "streamed": _flags(document["streamed"], _integer),
        "dropped": _flags(document["dropped"], _boolean),
    }


def build(outcome, exit_code, footprint, exceeded_mb, delta, streamed, dropped):
    """A validated report; ``delta`` is ``(status, records, bytes)``, ``streamed`` and ``dropped`` map a stream name to a value."""
    status, records, size = delta
    return validate(
        {
            "version": VERSION,
            "outcome": outcome,
            "exit_code": exit_code,
            "footprint": footprint,
            "exceeded_mb": exceeded_mb,
            "delta": {"status": status, "records": records, "bytes": size},
            "streamed": {name: streamed[name] for name in STREAMS},
            "dropped": {name: dropped[name] for name in STREAMS},
        }
    )


def dumps(document):
    """The bytes the wrapper posts: the validated report as compact ASCII JSON, at most ``MAX_BYTES``."""
    data = json.dumps(validate(document), sort_keys=True, separators=(",", ":")).encode("ascii")
    if len(data) > MAX_BYTES:
        _fail()
    return data


def loads(data):
    """The validated report of the bytes the relay stored (``out/report.json``); ``ValueError("report")`` on anything else."""
    if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_BYTES:
        _fail()
    try:
        document = json.loads(bytes(data).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ValueError("report") from None
    return validate(document)

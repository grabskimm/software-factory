#!/usr/bin/env python3
"""Prompt-cache report over run directories: a per-phase table of cache hit rate, cost and cold starts.

    cache_report.py RUN_OR_RUNS_DIR [...]     print the table (text)
    cache_report.py --json RUN_OR_RUNS_DIR    the same numbers as one JSON document
    cache_report.py --check RUN_OR_RUNS_DIR   also say how many sessions' per-turn sums disagree with
                                              their result record (a self-check of the reading)

Each argument is a run directory (``<data>/repos/<org>/<repo>/runs/<run>``), the ``runs`` directory
above them, or one ``*.jsonl`` session log. The logs are the Claude Code ``stream-json`` files Factory
keeps next to a job (``<attempt>-<phase>.jsonl``, ``planner.jsonl``). What is read, per log:

* the last ``result`` record: ``usage`` (input, cache write, cache read, output and thinking tokens),
  ``total_cost_usd``, ``duration_ms``, ``num_turns``, and the model (``modelUsage`` key);
* the ``assistant`` records' per-turn ``usage`` and ``timestamp``, **once per message id**: Claude Code
  writes one ``assistant`` record per content block, all carrying the same message id and the same
  usage, so summing every record counts a turn several times. Counted once per id, the cache and input
  sums equal the result record's (``--check`` verifies it). The per-turn ``output_tokens`` are streaming
  placeholders and are not used; output comes from the result record.

Definitions:

* hit rate = cache reads / (cache reads + cache writes + uncached input);
* cold start = the cache write of a session's first turn; the session is *warm* when that turn read
  from the cache;
* mid-session expiry = a later turn that follows a gap of at least ``EXPIRY_GAP_SECONDS`` (the 5 minute
  cache lifetime) and read less than ``EXPIRY_READ_SHARE`` of the previous turn's context (its reads plus
  writes): the prefix was not found in the cache and had to be written again. A heuristic; a long wait
  that merely appends a lot of new output (a test run) is not counted, because the prefix was still read.

A session with no ``result`` record (killed, or still running) is counted under ``no_result`` and is in
no figure. Cost is the CLI's client-side estimate, not billing data.

Read-only: nothing is written, no network, no GitHub, no run-directory change. Only regular files are read
(never a symlink; dot directories such as ``.home-*`` are skipped), each line is bounded, and each file is read
as a stream, so memory does not grow with the log. The output has numbers and a fixed vocabulary
(phase names from the file name, model names matching ``claude-...``): never an issue, pull request,
comment, prompt or tool text. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from datetime import datetime

MAX_LINE_BYTES = 4 * 1024 * 1024  # a longer line is skipped (and counted), never parsed
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_FILES = 20000
MAX_DEPTH = 4
EXPIRY_GAP_SECONDS = 300.0  # the documented 5 minute cache lifetime
EXPIRY_READ_SHARE = 0.5  # a turn after a gap that reads less than this share of the previous context lost its cache

KNOWN_PHASES = ("planner", "build", "review", "verify", "verify-baseline", "preflight", "followup")
_PHASE = re.compile(r"^(?:\d{1,3}-)?([a-z][a-z-]*?)(?:-\d{1,3})?$")
_MODEL = re.compile(r"^claude-[a-z0-9][a-z0-9.\-]{0,40}$")
_TOKEN_FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def phase_of(file_name: str) -> str:
    """The phase named by a log's file name (``1-build-0.jsonl`` is build), or ``other``."""
    stem = file_name[: -len(".jsonl")] if file_name.endswith(".jsonl") else file_name
    if stem == "verify-baseline" or re.fullmatch(r"\d{1,3}-verify-baseline(?:-\d{1,3})?", stem):
        return "verify-baseline"
    found = _PHASE.match(stem)
    name = found.group(1) if found else ""
    return name if name in KNOWN_PHASES else "other"


def _count(value: object) -> int:
    return value if type(value) is int and 0 <= value < 10**12 else 0


def _number(value: object) -> float:
    return float(value) if type(value) in (int, float) and 0 <= value < 10**9 else 0.0


def _seconds(text: object) -> float | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def is_regular_log(path: str) -> bool:
    """True for a regular file (never a symlink) within the size bound: the only kind of log that is read."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_size <= MAX_FILE_BYTES


def iter_records(path: str, counts: dict):
    """Stream the JSON object records of one log, one line at a time, so memory does not grow with the file.

    A line longer than ``MAX_LINE_BYTES`` is dropped unparsed and counted in ``counts["long_lines"]``; a line that
    starts like an object but does not parse is counted in ``counts["bad_lines"]``; other lines are ignored.
    Raises ``OSError`` when the file cannot be read. Shared with tools/cache_write_sources.py."""
    with open(path, "rb") as handle:
        while True:
            line = handle.readline(MAX_LINE_BYTES + 1)
            if not line:
                break
            if len(line) > MAX_LINE_BYTES:
                counts["long_lines"] += 1
                while line and not line.endswith(b"\n"):  # drop the rest of the oversized line
                    line = handle.readline(MAX_LINE_BYTES)
                continue
            if not line.lstrip().startswith(b"{"):
                continue
            try:
                record = json.loads(line)
            except (ValueError, RecursionError):  # not JSON, or nested so deeply that the parser gives up
                counts["bad_lines"] += 1
                continue
            if isinstance(record, dict):
                yield record


def read_session(path: str) -> dict | None:
    """The numbers of one session log, or None when it is not a regular readable file."""
    if not is_regular_log(path):
        return None
    result = None
    turns: dict = {}  # message id -> (first timestamp, usage fields)
    counts = {"long_lines": 0, "bad_lines": 0}
    try:
        for record in iter_records(path, counts):
            kind = record.get("type")
            if kind == "result":
                result = record
            elif kind == "assistant":
                message = record.get("message")
                usage = message.get("usage") if isinstance(message, dict) else None
                if isinstance(usage, dict) and isinstance(message.get("id"), str) and message["id"] not in turns:
                    turns[message["id"]] = (_seconds(record.get("timestamp")), usage)
    except OSError:
        return None
    return summarize(result, list(turns.values()), counts["long_lines"], counts["bad_lines"])


def summarize(result: dict | None, turns: list, long_lines: int = 0, bad_lines: int = 0) -> dict:
    """The per-session numbers from the last result record and the per-turn (id-deduplicated) usage."""
    session = {"has_result": isinstance(result, dict), "long_lines": long_lines, "bad_lines": bad_lines, "turns": len(turns)}
    first_write = first_read = 0
    expiry_events = expiry_tokens = 0
    previous = None
    previous_context = 0
    sums = dict.fromkeys(_TOKEN_FIELDS, 0)
    for index, (stamp, usage) in enumerate(turns):
        write, read = _count(usage.get("cache_creation_input_tokens")), _count(usage.get("cache_read_input_tokens"))
        for field in _TOKEN_FIELDS:
            sums[field] += _count(usage.get(field))
        if index == 0:
            first_write, first_read = write, read
        elif (
            stamp is not None and previous is not None and stamp - previous >= EXPIRY_GAP_SECONDS
            and read < EXPIRY_READ_SHARE * previous_context
        ):
            expiry_events += 1
            expiry_tokens += write
        previous_context = read + write
        if stamp is not None:
            previous = stamp
    session.update(turn_sums=sums, cold_write=first_write, warm_start=first_read > 0 if turns else False,
                   expiry_events=expiry_events, expiry_tokens=expiry_tokens)
    if isinstance(result, dict):
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        details = usage.get("output_tokens_details")
        details = details if isinstance(details, dict) else {}
        models = result.get("modelUsage")
        model = next((name for name in (models if isinstance(models, dict) else {}) if isinstance(name, str) and _MODEL.match(name)), "other")
        session.update(
            input=_count(usage.get("input_tokens")), write=_count(usage.get("cache_creation_input_tokens")),
            read=_count(usage.get("cache_read_input_tokens")), output=_count(usage.get("output_tokens")),
            thinking=_count(details.get("thinking_tokens")), cost=_number(result.get("total_cost_usd")),
            duration_ms=_number(result.get("duration_ms")), model=model,
        )
    return session


def find_logs(paths: list) -> list:
    """The ``*.jsonl`` session logs under ``paths``: regular files only, no symlink followed, no dot directory."""
    found: list = []
    for root in paths:
        root = os.path.abspath(root)
        if os.path.islink(root):
            continue
        if os.path.isfile(root):
            if root.endswith(".jsonl"):
                found.append(root)
            continue
        base_depth = root.rstrip(os.sep).count(os.sep)
        for directory, names, files in os.walk(root, followlinks=False):
            names[:] = sorted(n for n in names if not n.startswith(".") and not os.path.islink(os.path.join(directory, n)))
            if directory.rstrip(os.sep).count(os.sep) - base_depth >= MAX_DEPTH:
                names[:] = []
            found.extend(os.path.join(directory, f) for f in sorted(files) if f.endswith(".jsonl"))
            if len(found) >= MAX_FILES:
                return found[:MAX_FILES]
    return found


def _rate(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def aggregate(paths: list) -> dict:
    """The report over every log under ``paths``: rows by (phase, model), a total row and the self-check."""
    rows: dict = {}
    skipped = no_result = mismatched = checked = long_lines = bad_lines = 0
    for path in find_logs(paths):
        session = read_session(path)
        if session is None:
            skipped += 1
            continue
        long_lines += session["long_lines"]
        bad_lines += session["bad_lines"]
        if not session["has_result"]:
            no_result += 1
            continue
        checked += 1
        sums = session["turn_sums"]
        if session["turns"] and (sums["cache_creation_input_tokens"], sums["cache_read_input_tokens"], sums["input_tokens"]) != (
            session["write"], session["read"], session["input"]
        ):
            mismatched += 1
        for key in ((phase_of(os.path.basename(path)), session["model"]), ("all", "")):
            row = rows.setdefault(key, dict.fromkeys(
                ("sessions", "turns", "input", "write", "read", "output", "thinking", "cost", "duration_ms",
                 "cold_write", "warm_starts", "expiry_events", "expiry_tokens"), 0))
            row["sessions"] += 1
            row["turns"] += session["turns"]
            for field in ("input", "write", "read", "output", "thinking", "cost", "duration_ms", "cold_write", "expiry_events", "expiry_tokens"):
                row[field] += session[field] if field in session else 0
            row["warm_starts"] += 1 if session["warm_start"] else 0
    table = []
    for (phase, model), row in sorted(rows.items(), key=lambda item: (item[0][0] == "all", item[0])):
        total_input = row["input"] + row["write"] + row["read"]
        table.append({
            "phase": phase, "model": model, "sessions": row["sessions"], "turns": row["turns"],
            "hit_rate": _rate(row["read"], total_input), "cost_usd": round(row["cost"], 2),
            "cache_write_tokens": row["write"], "cache_read_tokens": row["read"], "uncached_input_tokens": row["input"],
            "output_tokens": row["output"], "thinking_share": _rate(row["thinking"], row["output"]),
            "cold_start_tokens_per_session": round(row["cold_write"] / row["sessions"]) if row["sessions"] else 0,
            "cold_start_share_of_writes": _rate(row["cold_write"], row["write"]),
            "warm_start_share": _rate(row["warm_starts"], row["sessions"]),
            "expiry_events": row["expiry_events"], "expiry_share_of_writes": _rate(row["expiry_tokens"], row["write"]),
            "seconds_per_session": round(row["duration_ms"] / 1000 / row["sessions"]) if row["sessions"] else 0,
        })
    return {"rows": table, "sessions_read": checked, "no_result": no_result, "skipped": skipped,
            "long_lines": long_lines, "bad_lines": bad_lines, "per_turn_sum_mismatches": mismatched}


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f} %"


def render(report: dict, check: bool = False) -> str:
    head = ("phase", "model", "sessions", "turns", "hit rate", "cost $", "writes", "reads", "output", "thinking",
            "cold/sess", "warm", "expiry")
    lines = [head]
    for row in report["rows"]:
        lines.append((
            row["phase"], row["model"] or "-", str(row["sessions"]), str(row["turns"]), _pct(row["hit_rate"]),
            f"{row['cost_usd']:.2f}", f"{row['cache_write_tokens']:,}", f"{row['cache_read_tokens']:,}",
            f"{row['output_tokens']:,}", _pct(row["thinking_share"]), f"{row['cold_start_tokens_per_session']:,}",
            _pct(row["warm_start_share"]), f"{row['expiry_events']} ({_pct(row['expiry_share_of_writes'])})",
        ))
    widths = [max(len(line[i]) for line in lines) for i in range(len(head))]
    out = ["  ".join(cell.ljust(widths[i]) if i < 2 else cell.rjust(widths[i]) for i, cell in enumerate(line)) for line in lines]
    out.append("")
    out.append(f"sessions with a result record: {report['sessions_read']}; without: {report['no_result']}; "
               f"unreadable or not regular: {report['skipped']}; oversized lines skipped: {report['long_lines']}; bad lines: {report['bad_lines']}")
    if check:
        out.append(f"per-turn sums that disagree with the result record: {report['per_turn_sum_mismatches']} of {report['sessions_read']}")
    out.append("cost is the CLI's client-side estimate, not billing data")
    return "\n".join(out)


# Names the sibling tool tools/cache_write_sources.py shares, so it reads the logs by the same rules.
count_value, rate, percent, seconds = _count, _rate, _pct, _seconds


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prompt-cache report over Factory run directories (read-only).")
    parser.add_argument("paths", nargs="+", help="a run directory, a runs directory, or a *.jsonl session log")
    parser.add_argument("--json", action="store_true", help="print one JSON document instead of the table")
    parser.add_argument("--check", action="store_true", help="also report sessions whose per-turn sums disagree with the result record")
    args = parser.parse_args(argv)
    report = aggregate(args.paths)
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else render(report, args.check))
    return 0


if __name__ == "__main__":
    sys.exit(main())

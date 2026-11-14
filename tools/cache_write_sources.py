#!/usr/bin/env python3
"""What fills the context: an ESTIMATE of the cache-write tokens each tool's results cost, per phase.

    cache_write_sources.py RUN_OR_RUNS_DIR [...]     print the table (text)
    cache_write_sources.py --json RUN_OR_RUNS_DIR    the same numbers as one JSON document
    cache_write_sources.py --check RUN_OR_RUNS_DIR   also say how many sessions' sums disagree (a self-check)

A companion of tools/cache_report.py. The arguments, the logs and the safety rules are those of
that tool, whose streaming reader and file rules this tool imports rather than copies: each argument is a run
directory, the ``runs`` directory above them, or one ``*.jsonl`` session log in Claude Code's ``stream-json`` shape; only
regular files are read (never a symlink, no dot directory); each line is bounded and each file is read as a stream; a
session with no ``result`` record is counted apart and is in no figure. Read-only: nothing is written, no network, no GitHub.

Method (an ESTIMATE, not a measurement of the model's tokenizer). The API reports one ``cache_creation_input_tokens``
figure per turn, the context newly written since the previous turn. The log does not say which part of it came from which
tool, so it is attributed:

1. Turns are the ``assistant`` records counted once per message id (Claude Code writes one record per content block, all
   with the same id and usage), as in cache_report. In a session that runs sub-agents (records carrying a
   ``parent_tool_use_id``) each conversation is a sequence of turns of its own.
2. What was written at turn N is what entered between turn N-1 and turn N: the content blocks of turn N-1's own records
   (text, thinking, tool calls) and every record after them, up to turn N: the ``tool_result`` blocks and any user text.
   Before the first turn only what was logged before it (the prompt, when it is logged); the system prompt and tool
   definitions are not in the log. Blocks after the last turn were never sent to the model and are left out.
3. Each block is sized in UTF-8 bytes of its text (a tool call by the bytes of its JSON input); ``BYTES_PER_TOKEN`` (4)
   turns bytes into estimated tokens. Images and other non-text content are not sized.
4. Each ``tool_result`` is resolved to a tool through its ``tool_use_id`` and the tool call with that id, and takes its
   estimated tokens. Everything else that entered (the prompt, assistant text, tool inputs, user text) and everything the
   proxy cannot see (the system prompt, harness reminders, a result whose size the proxy under-counts) is the remainder:
   it goes to the fixed bucket ``other (prompt, assistant text, system)``, so the per-turn sum equals the reported
   ``cache_creation_input_tokens`` exactly. When the proxy's total is larger than what was written, every block (the
   bucket's own text included) is scaled down in proportion to its size instead.

So a tool's figure never exceeds what its results would cost at 4 bytes a token unless that scaling applies, and the bucket
``other`` is where unexplained tokens land. Source code and JSON tokenize at fewer bytes per token than prose; the figures
are an order-of-magnitude guide to which source dominates, not a bill. Cost and the CLI's own usage are not used here.

A turn that lost the cache re-writes the whole context, not only what entered since the last turn. Such a turn is detected by
cache_report's mid-session expiry rule (a gap of at least ``EXPIRY_GAP_SECONDS`` since the conversation's previous stamped
turn and cache reads below ``EXPIRY_READ_SHARE`` of the previous turn's reads plus writes; it needs the records'
``timestamp``). What it wrote beyond the estimate of what entered goes to the fixed bucket ``REWRITE`` instead of ``other``,
so the earlier results it wrote again are not hidden in ``other``. A tool's figure is the cost of writing its results once;
the second write is in ``REWRITE``, which cannot say which tools' results were written again. A log without timestamps
shows no such turn and its re-writes stay in ``other``.

Labels come from a closed vocabulary and never from content. A tool name is looked up, not filtered: it is its own label only
if it is one of the ``BUILTIN_TOOLS`` (Claude Code's built-in tools, ``Read``, ``Bash``, ``Grep``, ...); any name starting with
``mcp__`` is the one label ``mcp-tool`` (the server and tool segments are never printed); every other name, whatever its
characters, is ``other-tool``. Tool names are text the model chose, so none of them can reach the output other than as one of
those labels. ``unknown-tool`` is for a result whose tool call id is missing or never seen; ``Bash:test`` for a ``Bash`` call
whose command (read, never printed) starts a known test runner (python -m unittest or pytest, tox, npm test, go test,
cargo test, ...). The output has numbers and these labels only: never an issue, comment, prompt, file path, command, id or
any tool input or output text.

``--check`` reports three counts that should all be 0: sessions where the attributed tokens do not add up to the per-turn
cache writes (a bug in the attribution), sessions where the per-turn cache writes do not add up to the ``result`` record's
(a log that is not what this reading assumes, e.g. a truncated one) and ``turns_untracked`` (below).

Memory is bounded whatever the log holds: a line is at most ``MAX_LINE_BYTES`` and a file ``MAX_FILE_BYTES`` (cache_report's),
at most ``MAX_TRACKED_TOOL_CALLS`` tool calls wait for their result (a result whose call was not tracked is ``unknown-tool``),
at most ``MAX_CONVERSATIONS`` sub-agent conversations are told apart (later ones share one), an id is cut or not tracked
past ``MAX_ID_CHARS`` characters, and at most ``MAX_TURNS`` distinct turns are remembered. Past that last cap a new
assistant message id is not a turn: it is not attributed and its record is counted in ``turns_untracked`` (one message can be
several records), while a record of an id that was remembered is still counted once. So the per-turn sum stays exact for the
turns that were tracked, the figures leave the rest out, ``turns_untracked`` says so (and the ``result`` record's total then
disagrees with the per-turn sum). Real sessions have hundreds to a few thousand turns, far below the cap.

The per-row median is over at most ``MAX_SIZES`` results (the first ones read); the count and the largest are exact.
Standard library only.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from array import array

_HERE = os.path.dirname(os.path.abspath(__file__))
_SPEC = importlib.util.spec_from_file_location("cache_report", os.path.join(_HERE, "cache_report.py"))
cache_report = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cache_report)

BYTES_PER_TOKEN = 4  # the size proxy: one estimated token per 4 bytes of text
OTHER = "other (prompt, assistant text, system)"
UNKNOWN_TOOL = "unknown-tool"
OTHER_TOOL = "other-tool"
MCP_TOOL = "mcp-tool"  # every tool whose name starts with mcp__; the server and tool segments are never kept
BASH_TEST = "Bash:test"
REWRITE = "re-written after cache loss"
# The only tool names that are their own label. A tool name is text the model chose, so it is looked up here, never
# filtered by its characters: a name that is not in this set is ``other-tool`` however harmless it looks.
BUILTIN_TOOLS = frozenset((
    "Agent", "AskUserQuestion", "Bash", "BashOutput", "CronCreate", "CronDelete", "CronList", "Edit", "EnterPlanMode",
    "EnterWorktree", "ExitPlanMode", "ExitWorktree", "Glob", "Grep", "KillBash", "KillShell", "LS", "ListMcpResourcesTool",
    "Monitor", "MultiEdit", "NotebookEdit", "NotebookRead", "Read", "ReadMcpResourceTool", "SendMessage", "Skill",
    "SlashCommand", "StructuredOutput", "Task", "TaskCreate", "TaskGet", "TaskList", "TaskOutput", "TaskStop", "TaskUpdate",
    "TodoWrite", "ToolSearch", "WebFetch", "WebSearch", "Write",
))
VOCABULARY = BUILTIN_TOOLS | {OTHER, UNKNOWN_TOOL, OTHER_TOOL, MCP_TOOL, BASH_TEST, REWRITE}  # every label a row can carry
MAX_TRACKED_TOOL_CALLS = 50000  # tool calls waiting for their result
MAX_CONVERSATIONS = 64  # sub-agent conversations told apart; later ones share one
MAX_TURNS = 100000  # distinct assistant message ids remembered to count each turn once; later new ids are not turns (turns_untracked)
MAX_SIZES = 200000  # result sizes kept per row for the median
COMMAND_HEAD = 2000  # the part of a Bash command looked at
MAX_ID_CHARS = 256  # ids are only ever dictionary keys; a longer one is cut (a conversation key) or not tracked (a tool call id)

_TEST_COMMAND = re.compile(
    r"(?:^|[\s;&|(/])(?:python[0-9.]*\s+-m\s+(?:unittest|pytest)|pytest|py\.test|tox|nox|npm\s+(?:run\s+)?test"
    r"|yarn\s+test|pnpm\s+test|go\s+test|cargo\s+test|make\s+test|mvn\s+test|gradle\s+test|dotnet\s+test|rspec|jest|vitest)(?:\s|$|;|&|\|)"
)
_TEXT_FIELD = {"text": "text", "thinking": "thinking", "redacted_thinking": "data"}


def text_bytes(value: object) -> int:
    """UTF-8 size of a string; 0 for anything else. ASCII text is sized without making a copy."""
    if not isinstance(value, str):
        return 0
    return len(value) if value.isascii() else len(value.encode("utf-8", "replace"))


def content_bytes(content: object) -> int:
    """Size of a tool result's content: a string, or the text blocks of a list. Images and the like are not sized."""
    if isinstance(content, str):
        return text_bytes(content)
    if isinstance(content, list):
        return sum(text_bytes(block.get("text")) for block in content if isinstance(block, dict) and block.get("type") == "text")
    return 0


def tool_label(name: object, tool_input: object) -> str:
    """The label of a tool call, always a member of ``VOCABULARY``: a built-in tool's own name (``Bash:test`` for a ``Bash``
    call that starts a test runner), ``mcp-tool`` for any ``mcp__`` name, else ``other-tool``. The name itself is never kept."""
    if not isinstance(name, str):
        return OTHER_TOOL
    if name.startswith("mcp__"):
        return MCP_TOOL
    if name not in BUILTIN_TOOLS:
        return OTHER_TOOL
    if name == "Bash" and isinstance(tool_input, dict):
        command = tool_input.get("command")
        if isinstance(command, str) and _TEST_COMMAND.search(command[:COMMAND_HEAD]):
            return BASH_TEST
    return name


def allocate(written: int, weights: dict, rewrite: bool = False) -> dict:
    """Split ``written`` cache-write tokens over labels from their sizes in bytes; the parts add up to ``written``.

    When the sizes, at ``BYTES_PER_TOKEN``, explain no more than was written, each tool takes its size over
    ``BYTES_PER_TOKEN``; otherwise every label (``OTHER`` included) takes its proportional share. What is left over is
    ``OTHER``'s, except in a turn that ``rewrite`` the context after a cache loss: there ``OTHER`` keeps only the size of
    its own text and what is left over (the earlier context written again) is ``REWRITE``'s."""
    total = sum(weights.values())
    tokens: dict = {}
    if total <= BYTES_PER_TOKEN * written:
        for label, size in weights.items():
            if label != OTHER or rewrite:
                tokens[label] = size // BYTES_PER_TOKEN
        if rewrite:
            tokens.setdefault(OTHER, 0)
            left = written - sum(tokens.values())
            if left > 0:
                tokens[REWRITE] = left
            return tokens
    else:
        for label, size in weights.items():
            tokens[label] = written * size // total
    tokens[OTHER] = tokens.get(OTHER, 0) + written - sum(tokens.values())
    return tokens


class Stat:
    """The figures of one label: estimated tokens, results seen, the largest and the sizes kept for a median."""

    __slots__ = ("weight", "tokens", "results", "largest", "sizes")

    def __init__(self) -> None:
        self.weight = 0
        self.tokens = 0
        self.results = 0
        self.largest = 0
        self.sizes = array("Q")

    def add_result(self, size: int) -> None:
        self.weight += size
        self.results += 1
        self.largest = max(self.largest, size)
        if len(self.sizes) < MAX_SIZES:
            self.sizes.append(size)

    def merge(self, other: Stat) -> None:
        self.tokens += other.tokens
        self.results += other.results
        self.largest = max(self.largest, other.largest)
        room = MAX_SIZES - len(self.sizes)
        if room > 0:
            self.sizes.extend(other.sizes[:room])


def lost_cache(stamp: float | None, previous_stamp: float | None, read: int, previous_context: int) -> bool:
    """cache_report's mid-session expiry rule: a turn at least ``EXPIRY_GAP_SECONDS`` after the previous stamped one that read
    less than ``EXPIRY_READ_SHARE`` of that turn's context (its reads plus writes) found no cache and wrote the context again."""
    return (stamp is not None and previous_stamp is not None and stamp - previous_stamp >= cache_report.EXPIRY_GAP_SECONDS
            and read < cache_report.EXPIRY_READ_SHARE * previous_context)


class Conversation:
    """What has entered one conversation since its last turn, by label (sizes in bytes) and results, and its last turn's context."""

    def __init__(self) -> None:
        self.pending: dict = {}
        self.stamp: float | None = None  # the time of its latest stamped turn
        self.context = 0  # the cache reads plus writes of its previous turn

    def stat(self, label: str) -> Stat:
        found = self.pending.get(label)
        if found is None:
            found = self.pending[label] = Stat()
        return found

    def close(self, written: int, read: int, stamp: float | None, into: dict) -> bool:
        """A turn that wrote ``written`` tokens and read ``read`` arrived: attribute the writes to what entered, into ``into``.

        Returns True when the turn lost the cache and wrote the earlier context again (see ``lost_cache``)."""
        rewrite = lost_cache(stamp, self.stamp, read, self.context)
        shares = allocate(written, {label: stat.weight for label, stat in self.pending.items()}, rewrite)
        for label, tokens in shares.items():
            into.setdefault(label, Stat()).tokens += tokens
        for label, stat in self.pending.items():
            into.setdefault(label, Stat()).merge(stat)
        self.pending = {}
        self.context = read + written
        if stamp is not None:
            self.stamp = stamp
        return rewrite


class Walker:
    """Walks one session's records in order and attributes each turn's cache writes.

    Every structure that grows with the log is capped (``MAX_TRACKED_TOOL_CALLS``, ``MAX_CONVERSATIONS``, ``MAX_ID_CHARS``,
    ``MAX_SIZES``, ``MAX_TURNS``). Past ``MAX_TURNS`` distinct message ids a record with a new id is no turn: it closes nothing,
    adds nothing to ``turns`` or ``written`` and is counted in ``turns_untracked``; a record of an id already remembered is
    still counted once. The per-turn sum is therefore exact for the tracked turns and ``turns_untracked`` is the visible loss."""

    def __init__(self) -> None:
        self.calls: dict = {}  # tool call id -> label, until its result is seen
        self.turn_ids: set = set()  # at most MAX_TURNS message ids (each cut to MAX_ID_CHARS)
        self.conversations: dict = {}
        self.stats: dict = {}  # label -> Stat for the whole session
        self.turns = 0
        self.turns_untracked = 0  # assistant records of a turn that arrived after MAX_TURNS ids were remembered
        self.written = 0
        self.rewrites = 0  # turns that wrote the context again after a cache loss

    def conversation(self, record: dict) -> Conversation:
        key = record.get("parent_tool_use_id")
        key = key[:MAX_ID_CHARS] if isinstance(key, str) else None
        if key not in self.conversations and len(self.conversations) >= MAX_CONVERSATIONS:
            key = ""
        return self.conversations.setdefault(key, Conversation())

    def feed(self, record: dict) -> None:
        message = record.get("message")
        if record.get("type") not in ("assistant", "user") or not isinstance(message, dict):
            return
        conversation = self.conversation(record)
        usage, message_id = message.get("usage"), message.get("id")
        if record.get("type") == "assistant" and isinstance(usage, dict) and isinstance(message_id, str):
            turn_id = message_id[:MAX_ID_CHARS]
            if turn_id not in self.turn_ids:
                if len(self.turn_ids) >= MAX_TURNS:
                    self.turns_untracked += 1
                else:
                    self.turn_ids.add(turn_id)
                    self.turn(conversation, usage, record)
        content = message.get("content")
        if isinstance(content, str):
            conversation.stat(OTHER).weight += text_bytes(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    self.block(conversation, block)

    def turn(self, conversation: Conversation, usage: dict, record: dict) -> None:
        """A turn that was not seen before: attribute its cache writes to what entered the conversation since its last turn."""
        written = cache_report.count_value(usage.get("cache_creation_input_tokens"))
        self.turns += 1
        self.written += written
        if conversation.close(written, cache_report.count_value(usage.get("cache_read_input_tokens")),
                              cache_report.seconds(record.get("timestamp")), self.stats):
            self.rewrites += 1

    def block(self, conversation: Conversation, block: dict) -> None:
        kind = block.get("type")
        if kind == "tool_result":
            call_id = block.get("tool_use_id")
            label = self.calls.pop(call_id, UNKNOWN_TOOL) if isinstance(call_id, str) and len(call_id) <= MAX_ID_CHARS else UNKNOWN_TOOL
            conversation.stat(label).add_result(content_bytes(block.get("content")))
            return
        if kind == "tool_use":
            call_id, tool_input = block.get("id"), block.get("input")
            if isinstance(call_id, str) and len(call_id) <= MAX_ID_CHARS and (call_id in self.calls or len(self.calls) < MAX_TRACKED_TOOL_CALLS):
                self.calls[call_id] = tool_label(block.get("name"), tool_input)
            size = text_bytes(block.get("name")) + (text_bytes(json.dumps(tool_input, separators=(",", ":"), ensure_ascii=False)) if tool_input else 0)
        else:
            field = _TEXT_FIELD.get(kind) if isinstance(kind, str) else None  # a type that is a list or dict is unhashable
            size = text_bytes(block.get(field)) if field else 0
        conversation.stat(OTHER).weight += size


def read_session(path: str) -> dict | None:
    """The attribution of one session log, or None when it is not a regular readable file."""
    if not cache_report.is_regular_log(path):
        return None
    counts = {"long_lines": 0, "bad_lines": 0}
    walker = Walker()
    result = None
    try:
        for record in cache_report.iter_records(path, counts):
            if record.get("type") == "result":
                result = record
            else:
                walker.feed(record)
    except OSError:
        return None
    usage = result.get("usage") if isinstance(result, dict) and isinstance(result.get("usage"), dict) else {}
    return {
        "has_result": isinstance(result, dict), "long_lines": counts["long_lines"], "bad_lines": counts["bad_lines"],
        "turns": walker.turns, "turns_untracked": walker.turns_untracked, "rewrites": walker.rewrites, "written": walker.written, "attributed": sum(s.tokens for s in walker.stats.values()),
        "result_write": cache_report.count_value(usage.get("cache_creation_input_tokens")), "stats": walker.stats,
    }


def _median(sizes: array) -> int | None:
    if not sizes:
        return None
    ordered = sorted(sizes)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) // 2


def aggregate(paths: list) -> dict:
    """The report over every log under ``paths``: rows by (phase, tool), per-phase totals and the self-checks."""
    phases: dict = {}  # phase -> {"sessions", "turns", "written", "tools": {label: Stat}}
    skipped = no_result = checked = long_lines = bad_lines = attribution_bad = result_bad = rewrites = untracked = 0
    for path in cache_report.find_logs(paths):
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
        rewrites += session["rewrites"]
        untracked += session["turns_untracked"]
        attribution_bad += 1 if session["attributed"] != session["written"] else 0
        result_bad += 1 if session["turns"] and session["written"] != session["result_write"] else 0
        for key in (cache_report.phase_of(os.path.basename(path)), "all"):
            phase = phases.setdefault(key, {"sessions": 0, "turns": 0, "written": 0, "tools": {}})
            phase["sessions"] += 1
            phase["turns"] += session["turns"]
            phase["written"] += session["written"]
            for label, stat in session["stats"].items():
                phase["tools"].setdefault(label, Stat()).merge(stat)
    rows, totals = [], []
    for key in sorted(phases, key=lambda name: (name == "all", name)):
        phase = phases[key]
        totals.append({"phase": key, "sessions": phase["sessions"], "turns": phase["turns"], "cache_write_tokens": phase["written"]})
        for label, stat in sorted(phase["tools"].items(), key=lambda item: (-item[1].tokens, item[0])):
            rows.append({
                "phase": key, "tool": label, "estimated_write_tokens": stat.tokens,
                "share_of_writes": cache_report.rate(stat.tokens, phase["written"]), "results": stat.results,
                "median_result_bytes": _median(stat.sizes), "largest_result_bytes": stat.largest if stat.results else None,
            })
    return {"method": f"estimate: tokens attributed by size at {BYTES_PER_TOKEN} bytes per token; rewrite_turns is detected by the "
                      f"expiry rule, needs timestamps (0 can also mean there are none)", "rows": rows, "phases": totals,
            "sessions_read": checked, "rewrite_turns": rewrites, "turns_untracked": untracked, "no_result": no_result, "skipped": skipped, "long_lines": long_lines, "bad_lines": bad_lines,
            "attribution_sum_mismatches": attribution_bad, "result_sum_mismatches": result_bad}


def _bytes(value: int | None) -> str:
    return "-" if value is None else f"{value:,}"


def render(report: dict, check: bool = False) -> str:
    head = ("phase", "tool", "est. writes", "share", "results", "median B", "largest B")
    lines = [head]
    for row in report["rows"]:
        lines.append((
            row["phase"], row["tool"], f"{row['estimated_write_tokens']:,}", cache_report.percent(row["share_of_writes"]),
            f"{row['results']:,}", _bytes(row["median_result_bytes"]), _bytes(row["largest_result_bytes"]),
        ))
    widths = [max(len(line[i]) for line in lines) for i in range(len(head))]
    out = ["  ".join(cell.ljust(widths[i]) if i < 2 else cell.rjust(widths[i]) for i, cell in enumerate(line)) for line in lines]
    out.append("")
    for total in report["phases"]:
        out.append(f"{total['phase']}: {total['sessions']} sessions, {total['turns']} turns, {total['cache_write_tokens']:,} cache-write tokens")
    out.append(f"turns that wrote the context again after a cache loss (the row {REWRITE!r}; detected by the expiry rule, "
               f"needs timestamps): {report['rewrite_turns']}")
    out.append(f"sessions with a result record: {report['sessions_read']}; without: {report['no_result']}; "
               f"unreadable or not regular: {report['skipped']}; oversized lines skipped: {report['long_lines']}; bad lines: {report['bad_lines']}; "
               f"records of turns past the tracking cap, in no figure: {report['turns_untracked']}")
    if check:
        out.append(f"sessions whose attributed tokens do not add up to the per-turn cache writes: {report['attribution_sum_mismatches']} of {report['sessions_read']}")
        out.append(f"sessions whose per-turn cache writes disagree with the result record: {report['result_sum_mismatches']} of {report['sessions_read']}")
        out.append(f"assistant records of turns past the tracking cap (MAX_TURNS), left out of every figure: {report['turns_untracked']}")
    out.append(f"ESTIMATE: tokens are attributed by result size at {BYTES_PER_TOKEN} bytes per token; the rest is in the bucket other, "
               f"except a context written again after a cache loss, which is its own row")
    return "\n".join(out)


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="Estimate which tools' results fill the prompt cache, per phase (read-only).")
    parser.add_argument("paths", nargs="+", help="a run directory, a runs directory, or a *.jsonl session log")
    parser.add_argument("--json", action="store_true", help="print one JSON document instead of the table")
    parser.add_argument("--check", action="store_true", help="also report sessions whose sums disagree (self-check of the reading)")
    args = parser.parse_args(argv)
    report = aggregate(args.paths)
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else render(report, args.check))
    return 0


if __name__ == "__main__":
    sys.exit(main())

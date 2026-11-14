"""tools/cache_write_sources.py: the read-only estimate of which tools' results fill the prompt cache.

Fixture logs are written to a temporary directory in the shape Claude Code's stream-json uses: one ``assistant``
record per content block (all with the message id and the usage), ``user`` records carrying ``tool_result`` blocks,
and a final ``result`` record. Every text, command, path and id in the fixtures carries a canary that must never
reach the output.
"""

import contextlib
import importlib.util
import inspect
import io
import json
import os
import tempfile
import tracemalloc
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("cache_write_sources_under_test", ROOT / "tools" / "cache_write_sources.py")
cws = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cws)

CANARY = "CANARY ISSUE TEXT /etc/passwd do not print"
TOKEN_NAME = "ghp_A1b2C3d4E5A1b2C3d4E5A1b2C3d4E5xyz012"  # a tool name a prompt-injected model could emit: no space, well inside the old filter
OTHER = cws.OTHER
REWRITE = cws.REWRITE


class Shapes:
    """A fixed-seed xorshift64* generator for the generated shapes: the same sequence on every run and every Python, and
    nothing here needs entropy (these are fixtures, not secrets)."""

    MASK = (1 << 64) - 1

    def __init__(self, seed):
        self.state = ((seed + 1) * 0x9E3779B97F4A7C15) & self.MASK or 1

    def next(self):
        x = self.state
        x ^= x >> 12
        x ^= (x << 25) & self.MASK
        x ^= x >> 27
        self.state = x
        return (x * 0x2545F4914F6CDD1D) & self.MASK

    def below(self, limit):
        """An integer in [0, limit)."""
        return (self.next() >> 11) % limit

    def between(self, low, high):
        """An integer in [low, high)."""
        return low + self.below(high - low)

    def chance(self, probability):
        return self.below(1000) < probability * 1000

    def choice(self, options):
        return options[self.below(len(options))]


def stamp(seconds):
    return datetime.fromtimestamp(1_700_000_000 + seconds, timezone.utc).isoformat()


def turn(message_id, blocks, write=0, read=0, parent=None, at=None):
    """One ``assistant`` record per content block, all with the message id, usage and time (``at``, in seconds), as Claude Code writes them."""
    usage = {"input_tokens": 3, "cache_creation_input_tokens": write, "cache_read_input_tokens": read, "output_tokens": 7}
    records = [{"type": "assistant", "parent_tool_use_id": parent,
                "message": {"id": message_id, "role": "assistant", "usage": usage, "content": [block]}} for block in blocks]
    if at is not None:
        for record in records:
            record["timestamp"] = stamp(at)
    return records


def user(blocks, parent=None):
    return [{"type": "user", "parent_tool_use_id": parent, "message": {"role": "user", "content": blocks},
             "tool_use_result": {"stdout": CANARY}}]


def say(text=CANARY):
    return {"type": "text", "text": text}


def call(call_id, name, **tool_input):
    return {"type": "tool_use", "id": call_id, "name": name, "input": tool_input}


def output(call_id, content):
    return {"type": "tool_result", "tool_use_id": call_id, "content": content}


def finish(write, read=0):
    return [{"type": "result", "subtype": "success", "total_cost_usd": 1.0, "duration_ms": 1000, "result": CANARY,
             "modelUsage": {"claude-sonnet-5-5": {}},
             "usage": {"input_tokens": 0, "cache_creation_input_tokens": write, "cache_read_input_tokens": read, "output_tokens": 5}}]


def write_log(path, records, raw_extra=b""):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{json.dumps(record)}\n".encode() for record in records]
    path.write_bytes(b"".join([*lines, raw_extra]))


def row(report, phase, tool):
    found = [r for r in report["rows"] if r["phase"] == phase and r["tool"] == tool]
    return found[0] if found else None


def tokens(report, phase="all"):
    return {r["tool"]: r["estimated_write_tokens"] for r in report["rows"] if r["phase"] == phase}


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def log(self, records, name="1-build-0.jsonl", raw_extra=b""):
        path = self.root / "runs" / "run-a" / "job" / name
        write_log(path, records, raw_extra)
        return path

    def report(self):
        return cws.aggregate([str(self.root / "runs")])


class AllocateTests(unittest.TestCase):
    def test_a_tool_takes_its_size_over_four_and_the_rest_is_other(self):
        self.assertEqual(cws.allocate(1000, {"Read": 2000, "Bash": 400, OTHER: 600}), {"Read": 500, "Bash": 100, OTHER: 400})

    def test_sizes_that_explain_more_than_was_written_are_scaled_down_in_proportion(self):
        out = cws.allocate(100, {"Read": 600, "Grep": 200, OTHER: 200})
        self.assertEqual(out, {"Read": 60, "Grep": 20, OTHER: 20})

    def test_the_parts_always_add_up_to_what_was_written(self):
        rng = Shapes(7)
        for _ in range(500):
            weights = {name: rng.between(0, 50000) for name in ("Read", "Bash", "Grep", "mcp__a__b", OTHER) if rng.chance(0.8)}
            written = rng.choice((0, 1, 7, rng.between(0, 20000)))
            out = cws.allocate(written, weights)
            self.assertEqual(sum(out.values()), written, (written, weights))
            self.assertTrue(all(value >= 0 for value in out.values()))

    def test_a_rewrite_turn_gives_the_part_the_new_bytes_do_not_explain_to_its_own_bucket(self):
        out = cws.allocate(1000, {"Read": 2000, "Bash": 400, OTHER: 600}, rewrite=True)
        self.assertEqual(out, {"Read": 500, "Bash": 100, OTHER: 150, REWRITE: 250})
        self.assertEqual(cws.allocate(1000, {"Read": 2000, "Bash": 400, OTHER: 600}), {"Read": 500, "Bash": 100, OTHER: 400})

    def test_a_rewrite_turn_that_the_sizes_explain_has_no_rewrite_share_and_is_scaled_as_any_other(self):
        self.assertEqual(cws.allocate(100, {"Read": 600, "Grep": 200, OTHER: 200}, rewrite=True), {"Read": 60, "Grep": 20, OTHER: 20})
        self.assertEqual(cws.allocate(100, {"Read": 400}, rewrite=True), {"Read": 100, OTHER: 0})
        self.assertEqual(cws.allocate(900, {}, rewrite=True), {OTHER: 0, REWRITE: 900})

    def test_the_parts_add_up_to_what_was_written_for_rewrite_turns_too(self):
        rng = Shapes(9)
        for _ in range(500):
            weights = {name: rng.between(0, 50000) for name in ("Read", "Bash", "mcp-tool", OTHER) if rng.chance(0.8)}
            written = rng.choice((0, 1, 7, rng.between(0, 20000)))
            out = cws.allocate(written, weights, rewrite=True)
            self.assertEqual(sum(out.values()), written, (written, weights))
            self.assertTrue(all(value >= 0 for value in out.values()))

    def test_nothing_entered_means_everything_written_is_other(self):
        self.assertEqual(cws.allocate(900, {}), {OTHER: 900})
        self.assertEqual(cws.allocate(0, {"Read": 5000}), {"Read": 0, OTHER: 0})


class SizingTests(unittest.TestCase):
    def test_text_is_sized_in_utf8_bytes_and_a_lone_surrogate_does_not_raise(self):
        self.assertEqual(cws.text_bytes("abc"), 3)
        self.assertEqual(cws.text_bytes("é"), 2)
        self.assertEqual(cws.text_bytes("€"), 3)
        self.assertGreaterEqual(cws.text_bytes("\ud800"), 1)
        self.assertEqual(cws.text_bytes(None), 0)
        self.assertEqual(cws.text_bytes(12), 0)

    def test_string_and_list_content_are_sized_and_other_content_is_not(self):
        self.assertEqual(cws.content_bytes("x" * 40), 40)
        listed = [{"type": "text", "text": "a" * 10}, {"type": "image", "source": {"data": "A" * 9999}}, {"type": "text", "text": "b" * 5}, "junk"]
        self.assertEqual(cws.content_bytes(listed), 15)
        self.assertEqual(cws.content_bytes(None), 0)
        self.assertEqual(cws.content_bytes({"type": "text", "text": "x"}), 0)

    def test_a_tool_name_is_looked_up_in_the_built_in_names_not_filtered_by_its_characters(self):
        for name, expected in (("Read", "Read"), ("Grep", "Grep"), ("WebFetch", "WebFetch"), ("Task", "Task"),
                               (TOKEN_NAME, "other-tool"), ("a" * 64, "other-tool"), ("Tool0", "other-tool"), ("read", "other-tool"),
                               ("Read ", "other-tool"), ("Read\n", "other-tool"), ("has space", "other-tool"), ("line\nbreak", "other-tool"),
                               ("", "other-tool"), (None, "other-tool"), (7, "other-tool"), (["Read"], "other-tool"), ({"Read": 1}, "other-tool"),
                               ("unknown-tool", "other-tool"), ("other-tool", "other-tool"), ("mcp-tool", "other-tool"),
                               ("Bash:test", "other-tool"), (REWRITE, "other-tool"), (OTHER, "other-tool")):
            with self.subTest(name=name):
                self.assertEqual(cws.tool_label(name, {}), expected)

    def test_every_mcp_tool_is_one_label_and_neither_segment_is_kept(self):
        for name in ("mcp__github__issue_read", "mcp__", f"mcp__{TOKEN_NAME}__{TOKEN_NAME}", f"mcp__{CANARY}", "mcp__a\nb"):
            with self.subTest(name=name):
                self.assertEqual(cws.tool_label(name, {}), "mcp-tool")
        self.assertEqual(cws.tool_label("mcp_github", {}), "other-tool")
        self.assertEqual(cws.tool_label("MCP__x", {}), "other-tool")

    def test_no_built_in_name_is_one_of_the_fixed_labels_and_each_is_a_plain_word(self):
        fixed = {OTHER, cws.UNKNOWN_TOOL, cws.OTHER_TOOL, cws.MCP_TOOL, cws.BASH_TEST, REWRITE}
        self.assertEqual(cws.BUILTIN_TOOLS & fixed, set())
        self.assertEqual(cws.VOCABULARY, cws.BUILTIN_TOOLS | fixed)
        for name in cws.BUILTIN_TOOLS:
            self.assertRegex(name, r"^[A-Za-z]{2,24}$")
        self.assertLess(len(cws.BUILTIN_TOOLS), 100)
        for name in ("Read", "Write", "Edit", "Bash", "Grep", "Glob", "Task", "WebFetch", "WebSearch", "TodoWrite", "NotebookEdit"):
            self.assertIn(name, cws.BUILTIN_TOOLS)

    def test_a_bash_call_that_starts_a_test_runner_is_labelled_apart(self):
        for command, expected in (("python3 -m unittest discover -s tests", "Bash:test"), ("PYTHONPATH=src python -m pytest -x", "Bash:test"),
                                  ("cd x && npm test", "Bash:test"), (".venv/bin/pytest", "Bash:test"), ("go test ./...", "Bash:test"),
                                  ("ls -la", "Bash"), ("git log --oneline", "Bash"), ("echo pytestfoo", "Bash"), ("", "Bash")):
            with self.subTest(command=command):
                self.assertEqual(cws.tool_label("Bash", {"command": command}), expected)
        self.assertEqual(cws.tool_label("Bash", None), "Bash")
        self.assertEqual(cws.tool_label("Bash", {"command": 7}), "Bash")
        self.assertEqual(cws.tool_label("Read", {"command": "pytest"}), "Read")


class AttributionTests(Base):
    def test_a_single_turn_wrote_the_system_prompt_so_it_is_all_other(self):
        self.log([*turn("m1", [say()], write=1000), *finish(1000)])
        report = self.report()
        self.assertEqual(tokens(report), {OTHER: 1000})
        only = row(report, "build", OTHER)
        self.assertEqual((only["share_of_writes"], only["results"], only["median_result_bytes"], only["largest_result_bytes"]), (1.0, 0, None, None))
        self.assertEqual(report["phases"], [{"phase": "build", "sessions": 1, "turns": 1, "cache_write_tokens": 1000},
                                            {"phase": "all", "sessions": 1, "turns": 1, "cache_write_tokens": 1000}])

    def test_the_results_that_entered_before_a_turn_take_that_turns_writes(self):
        self.log([*turn("m1", [say(), call("t1", "Read", file_path="f")], write=500),
                  *user([output("t1", "x" * 4000)]),
                  *turn("m2", [say()], write=1500), *finish(2000)])
        report = self.report()
        self.assertEqual(tokens(report), {"Read": 1000, OTHER: 1000})
        read = row(report, "build", "Read")
        self.assertEqual((read["share_of_writes"], read["results"], read["median_result_bytes"], read["largest_result_bytes"]), (0.5, 1, 4000, 4000))

    def test_several_tools_in_one_turn_each_get_their_own_row(self):
        self.log([*turn("m1", [call("a", "Read", file_path="f"), call("b", "Bash", command="ls"), call("c", "Grep", pattern="p"),
                               call("d", "Bash", command="python3 -m unittest")], write=100),
                  *user([output("a", "x" * 8000), output("b", "y" * 4000)]),
                  *user([output("c", "z" * 2000), output("d", "t" * 400)]),
                  *turn("m2", [say()], write=5000), *finish(5100)])
        report = self.report()
        self.assertEqual(tokens(report), {"Read": 2000, "Bash": 1000, "Grep": 500, "Bash:test": 100, OTHER: 100 + 1400})
        self.assertEqual(sum(tokens(report).values()), 5100)

    def test_a_turn_that_wrote_less_than_the_results_would_cost_scales_them_down(self):
        self.log([*turn("m1", [call("a", "Read", file_path="f")], write=0), *user([output("a", "x" * 8000)]),
                  *turn("m2", [say()], write=1000), *finish(1000)])
        weight = len(b"Read") + len(json.dumps({"file_path": "f"}, separators=(",", ":")))  # the tool call is the other bucket's
        report = self.report()
        self.assertEqual(tokens(report)["Read"], 1000 * 8000 // (8000 + weight))
        self.assertEqual(sum(tokens(report).values()), 1000)

    def test_string_content_and_list_content_are_both_sized(self):
        listed = [{"type": "text", "text": "a" * 800}, {"type": "image", "source": {"data": "A" * 5000}}, {"type": "text", "text": "b" * 800}]
        self.log([*turn("m1", [call("s", "Read", p="1"), call("l", "Grep", p="2")], write=0),
                  *user([output("s", "x" * 1600), output("l", listed)]), *turn("m2", [say()], write=10000), *finish(10000)])
        report = self.report()
        self.assertEqual((tokens(report)["Read"], tokens(report)["Grep"]), (400, 400))
        self.assertEqual(row(report, "build", "Grep")["largest_result_bytes"], 1600)

    def test_a_result_whose_call_is_missing_or_unknown_is_unknown_tool(self):
        self.log([*turn("m1", [call("known", "Read", p="1")], write=0),
                  *user([output("never-seen", "x" * 400), {"type": "tool_result", "content": "y" * 400}, output("known", "z" * 400),
                         output(["not", "a", "string"], "w" * 400)]),
                  *turn("m2", [say()], write=1000), *finish(1000)])
        report = self.report()
        self.assertEqual(tokens(report), {"unknown-tool": 300, "Read": 100, OTHER: 600})
        self.assertEqual(row(report, "build", "unknown-tool")["results"], 3)

    def test_a_call_id_is_used_once(self):
        self.log([*turn("m1", [call("t1", "Read", p="1")], write=0), *user([output("t1", "x" * 400), output("t1", "x" * 400)]),
                  *turn("m2", [say()], write=1000), *finish(1000)])
        self.assertEqual((tokens(self.report())["Read"], tokens(self.report())["unknown-tool"]), (100, 100))

    def test_a_turn_split_over_several_records_is_one_turn_and_writes_are_counted_once(self):
        records = [*turn("m1", [say("a"), say("b"), call("t1", "Read", p="1")], write=300), *user([output("t1", "x" * 400)]),
                   *turn("m2", [say("c"), say("d"), say("e")], write=900), *finish(1200)]
        self.log(records)
        report = self.report()
        self.assertEqual(report["phases"][-1]["turns"], 2)
        self.assertEqual(sum(tokens(report).values()), 1200)  # a record-by-record sum would have been 3 * 300 + 3 * 900
        self.assertEqual((report["attribution_sum_mismatches"], report["result_sum_mismatches"]), (0, 0))

    def test_a_turn_without_a_usage_record_is_not_a_boundary_but_its_blocks_still_count(self):
        no_usage = [{"type": "assistant", "message": {"id": "ghost", "content": [say("x" * 4000)]}}]
        self.log([*turn("m1", [call("t1", "Read", p="1")], write=0), *no_usage, *user([output("t1", "x" * 400)]),
                  *turn("m2", [say()], write=1000), *finish(1000)])
        # the ghost's 4000 bytes are in the segment: 400 + 4000 + the tool call's 13 bytes explain more than the 1000 written
        self.assertEqual(tokens(self.report())["Read"], 1000 * 400 // (400 + 4000 + 13))

    def test_results_after_the_last_turn_were_never_written_and_are_not_counted(self):
        self.log([*turn("m1", [call("t1", "Read", p="1")], write=100), *user([output("t1", "x" * 400)]), *finish(100)])
        report = self.report()
        self.assertIsNone(row(report, "build", "Read"))
        self.assertEqual(tokens(report), {OTHER: 100})

    def test_user_text_and_a_logged_prompt_before_the_first_turn_are_other(self):
        prompt = [{"type": "user", "message": {"role": "user", "content": "p" * 4000}}]
        self.log([*prompt, *turn("m1", [say()], write=2000), *finish(2000)])
        self.assertEqual(tokens(self.report()), {OTHER: 2000})

    def test_a_tool_name_outside_the_vocabulary_is_other_tool_and_the_name_is_never_printed(self):
        names = (TOKEN_NAME, f"{TOKEN_NAME}x", "x" * 65, f"{CANARY} tool", "SecretTool_9f8e7d")
        self.log([*turn("m1", [call(f"t{i}", name, p=str(i)) for i, name in enumerate(names)], write=0),
                  *user([output(f"t{i}", "x" * 400) for i in range(len(names))]), *turn("m2", [say()], write=1000), *finish(1000)])
        report = self.report()
        self.assertEqual(tokens(report)["other-tool"], 500)
        self.assertEqual(row(report, "all", "other-tool")["results"], len(names))
        text = f"{cws.render(report, check=True)}{json.dumps(report)}"
        for forbidden in ("ghp_", "A1b2C3", "xyz012", "CANARY", "SecretTool", "xxxxxxxx"):
            self.assertNotIn(forbidden, text)

    def test_many_distinct_non_built_in_names_make_one_row_and_many_mcp_names_make_one_row(self):
        count = 200
        calls = [call(f"t{i}", f"Tool{i}" if i % 2 else f"mcp__server{i}__tool{i}", p=str(i)) for i in range(count)]
        self.log([*turn("m1", calls, write=0), *user([output(f"t{i}", "x" * 400) for i in range(count)]),
                  *turn("m2", [say()], write=10**6), *finish(10**6)])
        report = self.report()
        self.assertEqual(sorted(t for t in tokens(report) if t != OTHER), ["mcp-tool", "other-tool"])
        self.assertEqual((row(report, "build", "mcp-tool")["results"], row(report, "build", "other-tool")["results"]), (100, 100))

    def test_sub_agent_conversations_do_not_take_each_others_writes(self):
        sub = "toolu_task"
        self.log([
            *turn("main1", [call(sub, "Task", prompt="p")], write=100),
            *turn("sub1", [call("r1", "Read", p="1")], write=5000, parent=sub),
            *user([output("r1", "x" * 4000)], parent=sub),
            *turn("sub2", [say()], write=1200, parent=sub),
            *user([output(sub, "t" * 400)]),
            *turn("main2", [say()], write=300),
            *finish(6600),
        ])
        report = self.report()
        self.assertEqual((tokens(report)["Read"], tokens(report)["Task"]), (1000, 100))
        self.assertEqual(report["phases"][-1]["turns"], 4)
        self.assertEqual(sum(tokens(report).values()), 6600)

    def test_interleaved_sub_agents_each_keep_their_own_pending_results(self):
        self.log([
            *turn("a1", [call("ra", "Read", p="1")], write=0, parent="toolu_a"),
            *turn("b1", [call("rb", "Grep", p="2")], write=0, parent="toolu_b"),
            *user([output("ra", "x" * 4000)], parent="toolu_a"),
            *user([output("rb", "y" * 400)], parent="toolu_b"),
            *turn("a2", [say()], write=1100, parent="toolu_a"),
            *turn("b2", [say()], write=300, parent="toolu_b"),
            *finish(1400),
        ])
        report = self.report()
        self.assertEqual((tokens(report)["Read"], tokens(report)["Grep"]), (1000, 100))
        self.assertEqual(sum(tokens(report).values()), 1400)

    def test_a_flood_of_sub_agent_ids_still_adds_up_beyond_the_conversation_bound(self):
        records = []
        for index in range(5):
            records.extend(turn(f"m{index}", [say()], write=10, parent=f"sub{index}"))
        self.log([*records, *finish(50)])
        with mock.patch.object(cws, "MAX_CONVERSATIONS", 2):
            report = self.report()
        self.assertEqual(sum(tokens(report).values()), 50)
        self.assertEqual((report["attribution_sum_mismatches"], report["result_sum_mismatches"]), (0, 0))


class BoundTests(Base):
    """Each memory bound is pinned by patching its constant small: the test fails when the bound is taken out."""

    @staticmethod
    def walk(records, walker=None):
        walker = walker or cws.Walker()
        for record in records:
            walker.feed(record)
        return walker

    def test_sub_agent_conversations_beyond_the_bound_share_one(self):
        parents = [f"sub{index}" for index in range(5)]
        records = [record for index, parent in enumerate(parents) for record in turn(f"m{index}", [call(f"c{index}", "Read", p="1")], write=10, parent=parent)]
        for index, parent in enumerate(parents):  # every conversation ends with a tool result that is still pending
            records.extend(user([output(f"c{index}", "x" * 400)], parent=parent))
        with mock.patch.object(cws, "MAX_CONVERSATIONS", 2):
            walker = self.walk(records)
        self.assertEqual(set(walker.conversations), {"sub0", "sub1", ""})
        self.assertEqual([walker.conversations[key].pending["Read"].results for key in ("sub0", "sub1", "")], [1, 1, 3])
        self.assertEqual((walker.turns, walker.written), (5, 50))

    def test_a_conversation_already_told_apart_keeps_its_own_after_the_bound_is_reached(self):
        with mock.patch.object(cws, "MAX_CONVERSATIONS", 2):
            walker = self.walk([*turn("a1", [say()], write=1, parent="a"), *turn("b1", [say()], write=1, parent="b"),
                                *turn("c1", [say()], write=1, parent="c"), *user([output("x", "y" * 400)], parent="a")])
        self.assertEqual(set(walker.conversations), {"a", "b", ""})
        self.assertEqual(walker.conversations["a"].pending["unknown-tool"].results, 1)

    def test_a_parent_id_is_cut_to_the_id_bound(self):
        stem = "p" * cws.MAX_ID_CHARS
        walker = self.walk([*turn("m1", [say()], write=1, parent=f"{stem}a"), *turn("m2", [say()], write=1, parent=f"{stem}b")])
        self.assertEqual(list(walker.conversations), [stem])
        self.assertEqual(walker.turns, 2)

    def test_tool_calls_beyond_the_bound_are_not_tracked_and_their_results_are_unknown_tool(self):
        ids = [f"c{index}" for index in range(5)]
        with mock.patch.object(cws, "MAX_TRACKED_TOOL_CALLS", 2):
            walker = self.walk(turn("m1", [call(i, "Read", p=i) for i in ids], write=0))
            self.assertEqual(set(walker.calls), {"c0", "c1"})
            self.walk(user([output(i, "x" * 400) for i in ids]), walker)
            pending = walker.conversations[None].pending
            self.assertEqual((pending["Read"].results, pending["unknown-tool"].results), (2, 3))
            self.assertEqual(walker.calls, {})  # a call is forgotten once its result is seen, so there is room again
            self.walk(turn("m2", [call("c9", "Grep", p="9")], write=0), walker)
            self.assertEqual(walker.calls, {"c9": "Grep"})

    def test_through_a_log_the_untracked_results_show_as_unknown_tool_and_the_sums_still_add_up(self):
        ids = [f"c{index}" for index in range(5)]
        self.log([*turn("m1", [call(i, "Read", p=i) for i in ids], write=0), *user([output(i, "x" * 400) for i in ids]),
                  *turn("m2", [say()], write=1000), *finish(1000)])
        with mock.patch.object(cws, "MAX_TRACKED_TOOL_CALLS", 2):
            report = self.report()
        self.assertEqual((row(report, "all", "Read")["results"], row(report, "all", "unknown-tool")["results"]), (2, 3))
        self.assertEqual(sum(tokens(report).values()), 1000)
        self.assertEqual((report["attribution_sum_mismatches"], report["result_sum_mismatches"]), (0, 0))

    def test_a_tool_call_id_longer_than_the_bound_is_not_tracked(self):
        longest, too_long = "t" * cws.MAX_ID_CHARS, "t" * (cws.MAX_ID_CHARS + 1)
        walker = self.walk(turn("m1", [call(longest, "Read", p="1"), call(too_long, "Grep", p="2")], write=0))
        self.assertEqual(list(walker.calls), [longest])
        self.walk(user([output(longest, "x" * 400), output(too_long, "x" * 400)]), walker)
        pending = walker.conversations[None].pending
        self.assertEqual((pending["Read"].results, pending["unknown-tool"].results), (1, 1))
        self.assertNotIn("Grep", pending)

    def test_message_ids_are_cut_to_the_id_bound(self):
        stem = "m" * cws.MAX_ID_CHARS
        walker = self.walk([*turn(f"{stem}a", [say()], write=5), *turn(f"{stem}b", [say()], write=7)])
        self.assertEqual((walker.turns, walker.written), (1, 5))  # the two ids are one id once cut
        self.assertEqual({len(turn_id) for turn_id in walker.turn_ids}, {cws.MAX_ID_CHARS})

    def test_turns_beyond_the_bound_are_counted_apart_and_the_tracked_sum_stays_exact(self):
        records = [record for index in range(3) for record in turn(f"m{index}", [say("t" * 40)], write=100 * (index + 1))]
        records += turn("m3", [say()], write=400) + turn("m4", [say()], write=500)
        records += turn("m5", [say(), say()], write=600)  # one message in two records: both are counted, nothing can tell them apart
        records += turn("m0", [say("again")], write=100)  # a message that was remembered is still one turn
        with mock.patch.object(cws, "MAX_TURNS", 3):
            walker = self.walk(records)
        self.assertEqual(len(walker.turn_ids), 3)
        self.assertEqual((walker.turns, walker.written, walker.turns_untracked), (3, 600, 4))
        self.assertEqual(sum(stat.tokens for stat in walker.stats.values()), 600)  # exact for the turns that are tracked
        self.assertEqual(self.walk(records).turns_untracked, 0)  # and the cap is the only reason for the count

    def test_the_report_says_how_many_records_of_turns_past_the_cap_it_left_out(self):
        records = [record for index in range(6) for record in turn(f"m{index}", [say("t" * 40)], write=100 * (index + 1))]
        self.log([*records, *finish(2100)])
        with mock.patch.object(cws, "MAX_TURNS", 3):
            report = self.report()
        self.assertEqual((report["turns_untracked"], report["phases"][-1]["turns"], report["phases"][-1]["cache_write_tokens"]), (3, 3, 600))
        self.assertEqual(sum(tokens(report).values()), 600)
        self.assertEqual((report["attribution_sum_mismatches"], report["result_sum_mismatches"]), (0, 1))  # the result record has all 2100
        text, checked = cws.render(report), cws.render(report, check=True)
        self.assertIn("records of turns past the tracking cap, in no figure: 3", text)
        self.assertIn("left out of every figure: 3", checked)
        self.assertNotIn("left out of every figure", text)
        whole = self.report()  # the same log under the real cap loses nothing
        self.assertEqual((whole["turns_untracked"], whole["phases"][-1]["turns"], whole["result_sum_mismatches"]), (0, 6, 0))
        self.assertIn("left out of every figure: 0", cws.render(whole, check=True))


class RewriteTests(Base):
    """A turn that lost the cache writes the whole context again; that is not credited to the tools or hidden in ``other``."""

    def expired(self, stamps=True, read_after=0):
        """Turn 1 writes 5000, turn 2 (a 80000 byte Read result entered) reads 5000 and writes 21000, turn 3 comes 400 s later
        and reads ``read_after`` of the 26000 token context: with 0 it wrote the context again plus 100 new tokens."""
        at = (lambda n: n) if stamps else (lambda n: None)
        self.log([*turn("m1", [call("t1", "Read", file_path="f")], write=5000, at=at(0)),
                  *user([output("t1", "x" * 80000)]),
                  *turn("m2", [say("a" * 400)], write=21000, read=5000, at=at(10)),
                  *turn("m3", [say("b")], write=26100 if not read_after else 100, read=read_after, at=at(410)),
                  *finish(52100 if not read_after else 26100, 5000 + read_after)])
        return self.report()

    def test_a_turn_after_a_cache_loss_puts_the_context_it_wrote_again_in_its_own_row(self):
        report = self.expired()
        self.assertEqual(tokens(report), {"Read": 20000, OTHER: 5000 + 1000 + 100, REWRITE: 26000})
        self.assertEqual(sum(tokens(report).values()), 52100)
        self.assertEqual(report["rewrite_turns"], 1)
        self.assertEqual((report["attribution_sum_mismatches"], report["result_sum_mismatches"]), (0, 0))
        rewritten = row(report, "build", REWRITE)
        self.assertEqual((rewritten["results"], rewritten["median_result_bytes"], rewritten["largest_result_bytes"]), (0, None, None))
        self.assertEqual(rewritten["share_of_writes"], round(26000 / 52100, 4))

    def test_a_row_for_rewrites_is_there_only_when_one_happened(self):
        self.assertNotIn(REWRITE, tokens(self.expired(read_after=26000)))
        self.assertEqual(self.expired(read_after=26000)["rewrite_turns"], 0)

    def test_a_turn_after_a_long_wait_that_still_read_its_prefix_is_not_a_rewrite(self):
        report = self.expired(read_after=25000)  # a long test run: the 400 s gap is there but the context was read
        self.assertNotIn(REWRITE, tokens(report))
        self.assertEqual(sum(tokens(report).values()), 26100)

    def test_without_timestamps_a_rewrite_cannot_be_seen_and_stays_in_other(self):
        report = self.expired(stamps=False)
        self.assertEqual(tokens(report), {"Read": 20000, OTHER: 5000 + 1000 + 26100})
        self.assertEqual(report["rewrite_turns"], 0)

    def test_a_fast_turn_that_read_little_is_not_counted_as_an_expiry(self):
        self.log([*turn("m1", [say()], write=5000, at=0), *turn("m2", [say()], write=5000, read=0, at=299), *finish(10000)])
        self.assertNotIn(REWRITE, tokens(self.report()))

    def test_the_first_turn_of_a_conversation_is_never_a_rewrite_and_each_conversation_has_its_own_context(self):
        sub = "toolu_task"
        self.log([*turn("main1", [call(sub, "Task", prompt="p")], write=90000, at=0),
                  *turn("sub1", [say()], write=5000, parent=sub, at=1000),  # a long gap and no reads, but the sub-agent's first turn
                  *turn("sub2", [say()], write=100, read=5000, parent=sub, at=1010),
                  *turn("main2", [say()], write=100, read=90000, at=1020), *finish(95200, 95000)])
        report = self.report()
        self.assertEqual(report["rewrite_turns"], 0)
        self.assertNotIn(REWRITE, tokens(report))

    def test_it_counts_the_same_turns_as_cache_reports_expiry_rule(self):
        shapes = ((5000, 0, 0), (300, 5000, 20), (4000, 0, 500), (200, 4300, 520), (3000, 100, 900), (50, 3000, 905))  # write, read, seconds
        records = []
        for index, (write, read, at) in enumerate(shapes):
            records.extend(turn(f"m{index}", [say("t" * 100)], write=write, read=read, at=at))
        records.extend(finish(sum(write for write, _, _ in shapes)))
        path = self.log(records)
        theirs = cws.cache_report.read_session(str(path))["expiry_events"]
        self.assertEqual(theirs, 2)
        self.assertEqual(self.report()["rewrite_turns"], theirs)

    def test_the_text_report_says_how_many_turns_wrote_the_context_again(self):
        text = cws.render(self.expired(), check=True)
        self.assertIn("turns that wrote the context again after a cache loss", text)
        self.assertIn("detected by the expiry rule, needs timestamps): 1\n", text)  # the qualifier is next to the number
        self.assertIn(REWRITE, text)
        self.assertIn("do not add up to the per-turn cache writes: 0 of 1", text)


class StatisticsTests(Base):
    def build(self, sizes, name="1-build-0.jsonl"):
        calls = [call(f"t{i}", "Read", p=str(i)) for i in range(len(sizes))]
        self.log([*turn("m1", calls, write=0), *user([output(f"t{i}", "x" * size) for i, size in enumerate(sizes)]),
                  *turn("m2", [say()], write=10**6), *finish(10**6)], name=name)

    def test_the_median_and_the_largest_of_an_odd_and_an_even_number_of_results(self):
        self.build([100, 300, 200])
        self.assertEqual((row(self.report(), "all", "Read")["median_result_bytes"], row(self.report(), "all", "Read")["largest_result_bytes"]), (200, 300))
        self.build([100, 300, 200, 1000])
        read = row(self.report(), "all", "Read")
        self.assertEqual((read["median_result_bytes"], read["largest_result_bytes"], read["results"]), (250, 1000, 4))

    def test_the_count_and_the_largest_stay_exact_when_the_median_is_bounded(self):
        self.build([5, 9, 1, 700, 3])
        with mock.patch.object(cws, "MAX_SIZES", 2):
            read = row(self.report(), "all", "Read")
        self.assertEqual((read["results"], read["largest_result_bytes"], read["median_result_bytes"]), (5, 700, 7))

    def test_rows_are_per_phase_with_an_all_row_and_shares_that_add_up(self):
        self.build([4000], name="1-build-0.jsonl")
        write_log(self.root / "runs" / "run-a" / "job" / "1-review-0.jsonl",
                  [*turn("m1", [call("t1", "Read", p="1")], write=0), *user([output("t1", "x" * 4000)]),
                   *turn("m2", [say()], write=4000), *finish(4000)])
        report = self.report()
        self.assertEqual([p["phase"] for p in report["phases"]], ["build", "review", "all"])
        self.assertEqual(row(report, "review", "Read")["share_of_writes"], 0.25)
        for phase in ("build", "review", "all"):
            shares = [r["share_of_writes"] for r in report["rows"] if r["phase"] == phase]
            self.assertAlmostEqual(sum(shares), 1.0, places=3)
        self.assertEqual(row(report, "all", "Read")["results"], 2)
        self.assertEqual(row(report, "all", "Read")["estimated_write_tokens"], 1000 + 1000)
        self.assertEqual(report["phases"][-1]["cache_write_tokens"], 10**6 + 4000)

    def test_rows_are_ordered_by_estimated_tokens_then_name(self):
        self.log([*turn("m1", [call("a", "Grep", p="1"), call("b", "Read", p="2"), call("c", "Bash", command="ls")], write=0),
                  *user([output("a", "x" * 400), output("b", "x" * 4000), output("c", "x" * 400)]), *turn("m2", [say()], write=2000), *finish(2000)])
        self.assertEqual([r["tool"] for r in self.report()["rows"] if r["phase"] == "build"], ["Read", OTHER, "Bash", "Grep"])


class CheckTests(Base):
    def test_a_session_that_adds_up_is_not_flagged(self):
        self.log([*turn("m1", [say()], write=500), *turn("m2", [say()], write=700), *finish(1200)])
        report = self.report()
        self.assertEqual((report["attribution_sum_mismatches"], report["result_sum_mismatches"], report["sessions_read"]), (0, 0, 1))
        self.assertIn("do not add up to the per-turn cache writes: 0 of 1", cws.render(report, check=True))

    def test_per_turn_writes_that_disagree_with_the_result_record_are_counted(self):
        self.log([*turn("m1", [say()], write=500), *finish(9999)])
        report = self.report()
        self.assertEqual(report["result_sum_mismatches"], 1)
        self.assertIn("disagree with the result record: 1 of 1", cws.render(report, check=True))
        self.assertNotIn("disagree with the result record", cws.render(report))

    def test_a_broken_attribution_would_be_counted(self):
        self.log([*turn("m1", [say()], write=500), *finish(500)])
        with mock.patch.object(cws, "allocate", lambda written, weights, rewrite=False: {OTHER: written - 1}):
            report = self.report()
        self.assertEqual(report["attribution_sum_mismatches"], 1)
        self.assertIn("do not add up to the per-turn cache writes: 1 of 1", cws.render(report, check=True))

    def test_a_session_without_a_result_record_is_counted_apart_and_is_in_no_figure(self):
        self.log([*turn("m1", [say()], write=500)])
        report = self.report()
        self.assertEqual((report["sessions_read"], report["no_result"], report["rows"], report["phases"]), (0, 1, [], []))

    def test_the_last_result_record_wins(self):
        self.log([*turn("m1", [say()], write=500), *finish(1), *finish(500)])
        self.assertEqual(self.report()["result_sum_mismatches"], 0)

    def test_empty_input_has_no_rows_and_no_division(self):
        report = cws.aggregate([str(self.root)])
        self.assertEqual((report["rows"], report["sessions_read"]), ([], 0))
        self.assertIn("sessions with a result record: 0", cws.render(report))

    def test_a_session_with_zero_cache_writes_has_no_share_not_a_zero_division(self):
        self.log([*turn("m1", [say()], write=0), *finish(0)])
        self.assertIsNone(row(self.report(), "build", OTHER)["share_of_writes"])
        self.assertIn("-", cws.render(self.report()))


class ReadingTests(Base):
    def test_bad_oversized_deeply_nested_and_non_json_lines_are_skipped_and_counted(self):
        big = b"".join((b'{"type":"user","x":"', b"a" * (cws.cache_report.MAX_LINE_BYTES + 10), b'"}\n'))
        deep = b"".join((b'{"type":"user","x":', b"[" * 200000, b"]" * 200000, b"}\n"))
        records = [*turn("m1", [call("t1", "Read", p="1")], write=0), *user([output("t1", "x" * 400)]), *turn("m2", [say()], write=1000), *finish(1000)]
        self.log(records, raw_extra=b"".join((b"{not json\n", b"plain text line\n", big, deep, b'{"type":"user"}\n')))
        report = self.report()
        self.assertEqual((report["sessions_read"], report["bad_lines"], report["long_lines"]), (1, 2, 1))
        self.assertEqual(tokens(report)["Read"], 100)
        self.assertEqual((report["attribution_sum_mismatches"], report["result_sum_mismatches"]), (0, 0))

    def test_hostile_shapes_are_ignored_not_trusted(self):
        odd = [
            {"type": "assistant", "message": "not a dict"}, {"type": "user", "message": {"content": 5}},
            {"type": "assistant", "message": {"id": 5, "usage": {"cache_creation_input_tokens": 9}, "content": [None, 3, {"type": 7}]}},
            {"type": "assistant", "message": {"id": "h1", "usage": {"cache_creation_input_tokens": 0}, "content": [
                {"type": ["x"], "text": "t" * 50}, {"type": {}, "thinking": "t" * 50}, {"type": [["text"]]}, {"type": {"a": [1]}, "data": "d"}]}},
            {"type": "assistant", "parent_tool_use_id": ["p"], "message": {"id": ["m"], "usage": {"cache_creation_input_tokens": 9}, "content": [
                {"type": "tool_use", "id": ["a"], "name": {"b": 1}, "input": {}}, {"type": "tool_use", "id": {"a": 1}, "name": ["Read"], "input": []}]}},
            {"type": "user", "parent_tool_use_id": {"p": 1}, "message": {"content": [
                {"type": "tool_result", "tool_use_id": {"a": 1}, "content": [{"type": ["text"], "text": "x"}]}, {"type": {}}, {"type": ["tool_result"]}]}},
            {"type": "assistant", "message": {"id": "m0", "usage": {"cache_creation_input_tokens": -9, "cache_read_input_tokens": "9"},
                                              "content": [{"type": "tool_use", "id": 5, "name": ["x"], "input": 3}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "x" * 5000, "content": {"a": 1}}]}},
            {"type": "assistant", "message": {"id": "m9", "usage": {"cache_creation_input_tokens": True}, "content": "text"}},
        ]
        self.log([*odd, *finish(0)])
        report = self.report()
        self.assertEqual(report["sessions_read"], 1)
        self.assertEqual(sum(tokens(report).values()), 0)

    def test_odd_and_unhashable_values_in_every_position_never_raise(self):
        rng = Shapes(5)
        pool = (None, 0, 1, -1, True, 1.5, "", "x", "Read", "Bash", "text", "thinking", "tool_use", "tool_result", "mcp__a", "x" * 300,
                [], ["x"], [["x"]], {}, {"a": 1}, {"a": [1]}, "2020-01-01T00:00:00Z", "\ud800")
        keys = ("type", "id", "name", "input", "tool_use_id", "content", "text", "thinking", "data")

        def odd():
            return rng.choice(pool)

        def block():
            made = {key: odd() for key in keys if rng.chance(0.5)}
            made.setdefault("type", rng.choice(("tool_use", "tool_result", "text", "thinking", "redacted_thinking")) if rng.chance(0.5) else odd())
            return made

        walker = cws.Walker()
        for _ in range(4000):
            content = rng.choice(("a string", odd(), [block() for _ in range(rng.between(0, 4))]))
            usage = {"cache_creation_input_tokens": odd(), "cache_read_input_tokens": odd()} if rng.chance(0.7) else odd()
            message = {"id": odd(), "usage": usage, "content": content}
            walker.feed({"type": rng.choice(("assistant", "user", odd())), "parent_tool_use_id": odd(), "timestamp": odd(), "message": message})
        self.assertEqual(sum(stat.tokens for stat in walker.stats.values()), walker.written)

    def test_only_regular_files_are_read_and_no_symlink_is_followed(self):
        real = self.log([*turn("m1", [say()], write=10), *finish(10)])
        outside = self.root / "elsewhere"
        write_log(outside / "1-build-0.jsonl", [*turn("m1", [say()], write=777), *finish(777)])
        run = self.root / "runs" / "run-a"
        os.symlink(outside, run / "linked-dir")
        os.symlink(outside / "1-build-0.jsonl", run / "job" / "1-review-0.jsonl")
        write_log(run / ".home-1-build-0" / "1-build-0.jsonl", [*turn("m1", [say()], write=888), *finish(888)])
        self.assertIsNone(cws.read_session(str(run / "job" / "1-review-0.jsonl")))
        self.assertTrue(real.is_file())
        report = self.report()
        self.assertEqual((report["sessions_read"], report["skipped"]), (1, 1))
        self.assertEqual(report["phases"][-1]["cache_write_tokens"], 10)
        self.assertEqual(cws.aggregate([str(run / "linked-dir")])["sessions_read"], 0)

    def test_a_missing_path_is_just_empty(self):
        self.assertEqual(cws.aggregate([str(self.root / "nope")])["rows"], [])

    def test_a_huge_log_is_streamed_not_held_in_memory(self):
        calls = [call(f"t{i}", "Read", p=str(i)) for i in range(16)]
        records = [*turn("m1", calls, write=0)]
        for index in range(16):  # about 48 MB in sixteen lines of 3 MB, each under the line bound
            records.extend(user([output(f"t{index}", "x" * 3_000_000)]))
        records.extend([*turn("m2", [say()], write=10**6), *finish(10**6)])
        path = self.log(records)
        size = path.stat().st_size
        self.assertGreater(size, 45_000_000)
        tracemalloc.start()
        try:
            session = cws.read_session(str(path))
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(session["stats"]["Read"].results, 16)
        self.assertLess(peak, size // 3)


class OutputTests(Base):
    def populate(self):
        records = [
            *turn("m1", [say(), call(f"{CANARY}-id", "Bash", command=f"python3 -m unittest {CANARY}"),
                         call("t2", "Read", file_path=f"/home/user/{CANARY}/secret.py"), call("t3", f"mcp__{CANARY}", q=CANARY),
                         call("t4", TOKEN_NAME, q=CANARY), call("t5", f"mcp__{TOKEN_NAME}__{TOKEN_NAME}", q=CANARY)], write=400),
            *user([output(f"{CANARY}-id", CANARY * 50), output("t2", [{"type": "text", "text": CANARY * 30}]), output("t3", CANARY),
                   output("t4", CANARY), output("t5", CANARY)]),
            *turn("m2", [say(CANARY)], write=3000), *finish(3400),
        ]
        self.log(records)
        write_log(self.root / "runs" / "run-a" / "planner.jsonl", records)

    def test_no_text_command_path_or_id_reaches_the_output(self):
        self.populate()
        report = self.report()
        text = f"{cws.render(report, check=True)}{json.dumps(report)}"
        for forbidden in ("CANARY", "passwd", "secret.py", "run-a", "/home", str(self.root), "unittest", "toolu", "ghp_", "A1b2C3", "xyz012"):
            self.assertNotIn(forbidden, text)
        self.assertIn("Bash:test", text)
        self.assertIn("other-tool", text)
        self.assertIn("mcp-tool", text)

    def test_every_label_is_from_the_fixed_vocabulary(self):
        self.populate()
        labels = {r["tool"] for r in self.report()["rows"]}
        self.assertEqual(labels, {"Bash:test", "Read", "mcp-tool", "other-tool", OTHER})  # written out, not derived from the module
        self.assertLessEqual(labels, cws.VOCABULARY)

    def test_the_directory_is_unchanged_after_a_report(self):
        self.populate()
        before = sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in self.root.rglob("*"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cws.main([str(self.root / "runs"), "--check"]), 0)
        self.assertEqual(before, sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in self.root.rglob("*")))
        self.assertIn("ESTIMATE", out.getvalue())
        self.assertIn("Read", out.getvalue())

    def test_json_output_is_one_document_and_the_text_table_has_the_columns(self):
        self.populate()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cws.main([str(self.root / "runs"), "--json"])
        document = json.loads(out.getvalue())
        self.assertEqual(set(document["rows"][0]), {"phase", "tool", "estimated_write_tokens", "share_of_writes", "results",
                                                    "median_result_bytes", "largest_result_bytes"})
        self.assertEqual(document["phases"][-1]["phase"], "all")
        self.assertIn("estimate", document["method"].lower())
        self.assertIn("rewrite_turns is detected by the expiry rule, needs timestamps", document["method"])
        self.assertEqual((document["rewrite_turns"], document["turns_untracked"]), (0, 0))
        text = cws.render(document)
        self.assertIn("est. writes", text.splitlines()[0])
        self.assertIn("median B", text.splitlines()[0])

    def test_the_per_turn_sums_equal_the_reported_creation_tokens_over_many_shapes(self):
        rng = Shapes(11)
        for session in range(20):
            records, total = [], 0
            for index in range(rng.between(1, 8)):
                write = rng.choice((0, 3, rng.between(0, 30000)))
                total += write
                ids = [f"s{session}-{index}-{n}" for n in range(rng.between(0, 4))]
                records.extend(turn(f"m{session}-{index}", [say("t" * rng.between(0, 500)), *(call(i, rng.choice(("Read", "Bash", "Grep")), p=i) for i in ids)], write=write))
                records.extend(user([output(i, "r" * rng.between(0, 40000)) for i in ids]))
            records.extend(finish(total))
            write_log(self.root / "runs" / f"run-{session}" / "job" / "1-build-0.jsonl", records)
        report = self.report()
        self.assertEqual((report["sessions_read"], report["attribution_sum_mismatches"], report["result_sum_mismatches"]), (20, 0, 0))
        self.assertEqual(sum(tokens(report).values()), report["phases"][-1]["cache_write_tokens"])


class SharedReaderTests(Base):
    """The tool reads logs through cache_report's reader and file rules; a copy of them would pass every other test here
    but not these, which put a canned answer in cache_report's place."""

    def canned(self, written):
        return [*turn("c1", [say()], write=written), *finish(written)]

    def test_it_defines_none_of_the_reader_or_the_file_rules_itself(self):
        for name in ("iter_records", "is_regular_log", "find_logs", "phase_of", "MAX_LINE_BYTES", "MAX_FILE_BYTES"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(cws, name))
                self.assertTrue(hasattr(cws.cache_report, name))
        source = inspect.getsource(cws)
        for reader in ("readline", "json.loads", ".readlines", "os.walk", "os.scandir", "os.listdir", "islink", "lstat"):
            with self.subTest(reader=reader):
                self.assertNotIn(reader, source)

    def test_the_records_of_a_session_come_from_cache_reports_iter_records(self):
        path = self.log([*turn("m1", [say()], write=777), *finish(777)])  # what a reader of its own would find in the file
        asked = []

        def canned(argument, counts):
            asked.append(argument)
            counts["bad_lines"] = 4
            yield from self.canned(123)

        with mock.patch.object(cws.cache_report, "iter_records", canned):
            session = cws.read_session(str(path))
        self.assertEqual(asked, [str(path)])
        self.assertEqual((session["written"], session["result_write"], session["turns"], session["bad_lines"]), (123, 123, 1, 4))

    def test_whether_a_path_is_a_log_to_read_is_cache_reports_call(self):
        path = self.log(self.canned(5))
        with mock.patch.object(cws.cache_report, "is_regular_log", return_value=False) as refused:
            self.assertIsNone(cws.read_session(str(path)))
        refused.assert_called_once_with(str(path))
        with mock.patch.object(cws.cache_report, "iter_records", lambda argument, counts: iter(self.canned(9))), \
                mock.patch.object(cws.cache_report, "is_regular_log", return_value=True):
            self.assertEqual(cws.read_session(str(self.root / "no-such-file.jsonl"))["written"], 9)

    def test_which_logs_are_read_and_their_phase_are_cache_reports_call(self):
        outside = self.root / "elsewhere" / "9-build-0.jsonl"
        write_log(outside, self.canned(40))
        self.log(self.canned(1000))  # under the argument, not offered by the patched finder
        with mock.patch.object(cws.cache_report, "find_logs", return_value=[str(outside)]) as finder:
            report = cws.aggregate([str(self.root / "runs")])
        finder.assert_called_once_with([str(self.root / "runs")])
        self.assertEqual((report["sessions_read"], report["phases"][-1]["cache_write_tokens"]), (1, 40))
        with mock.patch.object(cws.cache_report, "phase_of", return_value="zz-phase"):
            self.assertEqual([phase["phase"] for phase in self.report()["phases"]], ["zz-phase", "all"])


if __name__ == "__main__":
    unittest.main()

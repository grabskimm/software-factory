"""tools/cache_report.py: the read-only prompt-cache report over session logs.

Fixture logs are written to a temporary directory in the shape Claude Code's stream-json uses (one
``assistant`` record per content block, all with the same message id and usage; a final ``result``
record). Message text in the fixtures carries a canary that must never reach the output.
"""

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("cache_report_under_test", ROOT / "tools" / "cache_report.py")
cr = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cr)

CANARY = "CANARY-ISSUE-TEXT-do-not-print"


def assistant(message_id, stamp, write=0, read=0, fresh=0, blocks=1):
    """``blocks`` records with the same message id and usage, like a thinking, text and tool_use block."""
    usage = {"input_tokens": fresh, "cache_creation_input_tokens": write, "cache_read_input_tokens": read, "output_tokens": 7}
    return [
        {"type": "assistant", "timestamp": stamp, "message": {"id": message_id, "usage": usage,
                                                              "content": [{"type": "text", "text": CANARY}]}}
        for _ in range(blocks)
    ]


def result(fresh, write, read, output=100, thinking=40, cost=1.5, model="claude-sonnet-5-5", duration=60000):
    return {"type": "result", "subtype": "success", "total_cost_usd": cost, "duration_ms": duration,
            "result": CANARY, "modelUsage": {model: {"costUSD": cost}},
            "usage": {"input_tokens": fresh, "cache_creation_input_tokens": write, "cache_read_input_tokens": read,
                      "output_tokens": output, "output_tokens_details": {"thinking_tokens": thinking}}}


def write_log(path, records, raw_extra=b""):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        for record in records:
            handle.write(json.dumps(record).encode() + b"\n")
        handle.write(raw_extra)


def warm_session(stamps=("2026-11-08T10:00:00Z", "2026-11-08T10:00:30Z")):
    """Two turns; the first reads from the cache (warm). Per-turn sums: fresh 4, write 1500, read 11000."""
    return (
        assistant("m1", stamps[0], write=500, read=5000, fresh=2, blocks=3)
        + assistant("m2", stamps[1], write=1000, read=6000, fresh=2, blocks=2)
        + [result(4, 1500, 11000)]
    )


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_dir(self, name="run-a"):
        return self.root / "runs" / name


class ReadingTests(Base):
    def test_a_turn_is_counted_once_however_many_records_carry_its_message_id(self):
        log = self.run_dir() / "job" / "1-build-0.jsonl"
        write_log(log, warm_session())
        session = cr.read_session(str(log))
        self.assertEqual(session["turns"], 2)
        self.assertEqual(session["turn_sums"], {"input_tokens": 4, "cache_creation_input_tokens": 1500, "cache_read_input_tokens": 11000})
        self.assertEqual((session["input"], session["write"], session["read"]), (4, 1500, 11000))
        self.assertEqual((session["output"], session["thinking"], session["model"]), (100, 40, "claude-sonnet-5-5"))
        self.assertTrue(session["warm_start"])
        self.assertEqual(session["cold_write"], 500)

    def test_summing_every_record_would_have_overcounted_the_way_the_dedup_prevents(self):
        records = warm_session()
        naive = sum(r["message"]["usage"]["cache_read_input_tokens"] for r in records if r["type"] == "assistant")
        self.assertGreater(naive, 11000)  # the trap: 3 + 2 records for 2 turns

    def test_a_session_without_a_result_is_counted_apart_and_in_no_figure(self):
        write_log(self.run_dir() / "job" / "1-build-0.jsonl", assistant("m1", "2026-11-08T10:00:00Z", write=9, read=9))
        report = cr.aggregate([str(self.run_dir())])
        self.assertEqual((report["sessions_read"], report["no_result"], report["rows"]), (0, 1, []))

    def test_bad_oversized_and_non_json_lines_are_skipped_and_counted(self):
        log = self.run_dir() / "job" / "1-review-0.jsonl"
        big = b'{"type":"user","x":"' + b"a" * (cr.MAX_LINE_BYTES + 10) + b'"}\n'
        write_log(log, warm_session(), raw_extra=b"{not json\nplain text line\n" + big + b'{"type":"user"}\n')
        report = cr.aggregate([str(log)])
        self.assertEqual((report["sessions_read"], report["bad_lines"], report["long_lines"]), (1, 1, 1))
        self.assertEqual(report["per_turn_sum_mismatches"], 0)

    def test_a_line_nested_too_deeply_to_parse_is_a_bad_line_not_a_crash(self):
        log = self.run_dir() / "job" / "1-build-0.jsonl"
        deep = b"".join((b'{"type":"user","x":', b"[" * 200000, b"]" * 200000, b"}\n"))
        write_log(log, warm_session(), raw_extra=deep)
        report = cr.aggregate([str(log)])
        self.assertEqual((report["sessions_read"], report["bad_lines"]), (1, 1))

    def test_the_last_result_record_wins(self):
        log = self.run_dir() / "job" / "1-build-0.jsonl"
        write_log(log, warm_session()[:-1] + [result(1, 1, 1, cost=0.1), result(4, 1500, 11000, cost=2.0)])
        self.assertEqual(cr.read_session(str(log))["cost"], 2.0)

    def test_hostile_values_are_ignored_not_trusted(self):
        log = self.run_dir() / "job" / "1-build-0.jsonl"
        odd = result(True, -5, "9", output=10**15)
        odd["total_cost_usd"] = "1e9"
        odd["modelUsage"] = {"not-a-claude-model; rm -rf": {}}
        write_log(log, [odd])
        session = cr.read_session(str(log))
        self.assertEqual((session["input"], session["write"], session["read"], session["output"], session["cost"]), (0, 0, 0, 0, 0.0))
        self.assertEqual(session["model"], "other")


class ExpiryAndColdStartTests(Base):
    def test_the_first_record_of_a_turn_gives_its_time(self):
        # the content blocks of one turn are written as the model streams them, seconds apart; the gap is measured
        # from when the turn started, not from its last block
        late_block = assistant("m2", "2026-11-08T10:04:00Z", write=5200, read=100)
        records = (
            assistant("m1", "2026-11-08T10:00:00Z", write=5000, read=0)
            + assistant("m2", "2026-11-08T10:06:00Z", write=5200, read=100)[:1]
            + late_block
            + [result(0, 10200, 100)]
        )
        log = self.run_dir() / "job" / "1-build-0.jsonl"
        write_log(log, records)
        session = cr.read_session(str(log))
        self.assertEqual((session["turns"], session["expiry_events"]), (2, 1))

    def test_a_gap_of_five_minutes_with_a_rewrite_is_a_mid_session_expiry(self):
        records = (
            assistant("m1", "2026-11-08T10:00:00Z", write=5000, read=0)
            + assistant("m2", "2026-11-08T10:00:10Z", write=100, read=5000)
            + assistant("m3", "2026-11-08T10:06:00Z", write=5200, read=100)  # 350 s later, written again
            + [result(0, 10300, 5100)]
        )
        log = self.run_dir() / "job" / "1-verify.jsonl"
        write_log(log, records)
        session = cr.read_session(str(log))
        self.assertEqual((session["expiry_events"], session["expiry_tokens"]), (1, 5200))
        self.assertFalse(session["warm_start"])
        self.assertEqual(session["cold_write"], 5000)

    def test_a_long_gap_that_still_reads_from_the_cache_is_not_an_expiry(self):
        records = (
            assistant("m1", "2026-11-08T10:00:00Z", write=5000, read=0)
            + assistant("m2", "2026-11-08T10:20:00Z", write=100, read=5000)
            + [result(0, 5100, 5000)]
        )
        log = self.run_dir() / "job" / "1-build-0.jsonl"
        write_log(log, records)
        self.assertEqual(cr.read_session(str(log))["expiry_events"], 0)

    def test_a_long_wait_that_appends_a_lot_but_still_reads_the_prefix_is_not_an_expiry(self):
        # a long test run: the context grows by a big tool result, the old prefix was still found in the cache
        records = (
            assistant("m1", "2026-11-08T10:00:00Z", write=5000, read=0)
            + assistant("m2", "2026-11-08T10:12:00Z", write=9000, read=5000)
            + [result(0, 14000, 5000)]
        )
        log = self.run_dir() / "job" / "1-build-0.jsonl"
        write_log(log, records)
        self.assertEqual(cr.read_session(str(log))["expiry_events"], 0)

    def test_a_quick_turn_with_a_big_write_is_not_an_expiry(self):
        records = assistant("m1", "2026-11-08T10:00:00Z", write=100, read=5000) + assistant("m2", "2026-11-08T10:00:20Z", write=9000, read=100) + [result(0, 9100, 5100)]
        log = self.run_dir() / "job" / "1-build-0.jsonl"
        write_log(log, records)
        self.assertEqual(cr.read_session(str(log))["expiry_events"], 0)


class ReportTests(Base):
    def build(self):
        write_log(self.run_dir() / "job-a" / "1-build-0.jsonl", warm_session())
        write_log(self.run_dir() / "job-a" / "1-build-1.jsonl", warm_session())
        write_log(self.run_dir() / "job-a" / "1-review-0.jsonl", warm_session()[:-1] + [result(4, 1500, 11000, model="claude-opus-5-5", cost=3.0)])
        write_log(self.run_dir() / "planner.jsonl", warm_session())
        write_log(self.run_dir() / "job-a" / "1-verify-baseline.jsonl", warm_session())

    def test_phases_come_from_the_file_names(self):
        for name, phase in (("1-build-0.jsonl", "build"), ("2-review-1.jsonl", "review"), ("1-verify.jsonl", "verify"),
                            ("1-verify-baseline.jsonl", "verify-baseline"), ("planner.jsonl", "planner"),
                            ("odd-name.jsonl", "other"), ("1-secret-phase-0.jsonl", "other")):
            with self.subTest(name=name):
                self.assertEqual(cr.phase_of(name), phase)

    def test_the_table_has_a_row_per_phase_and_model_and_a_total(self):
        self.build()
        report = cr.aggregate([str(self.run_dir())])
        keys = [(r["phase"], r["model"]) for r in report["rows"]]
        self.assertEqual(keys, [("build", "claude-sonnet-5-5"), ("planner", "claude-sonnet-5-5"), ("review", "claude-opus-5-5"),
                                ("verify-baseline", "claude-sonnet-5-5"), ("all", "")])
        build = report["rows"][0]
        self.assertEqual((build["sessions"], build["turns"]), (2, 4))
        self.assertEqual(build["hit_rate"], round(22000 / (22000 + 3000 + 8), 4))  # reads / (reads + writes + fresh input)
        self.assertEqual(build["cost_usd"], 3.0)
        self.assertEqual(build["thinking_share"], 0.4)
        total = report["rows"][-1]
        self.assertEqual((total["sessions"], total["cost_usd"]), (5, 1.5 * 4 + 3.0))
        self.assertEqual(report["per_turn_sum_mismatches"], 0)

    def test_a_mismatch_between_the_turn_sums_and_the_result_is_reported_by_the_check(self):
        write_log(self.run_dir() / "job" / "1-build-0.jsonl", warm_session()[:-1] + [result(4, 1500, 99999)])
        report = cr.aggregate([str(self.run_dir())])
        self.assertEqual(report["per_turn_sum_mismatches"], 1)
        self.assertIn("disagree with the result record: 1 of 1", cr.render(report, check=True))

    def test_empty_input_has_no_rows_and_no_division(self):
        report = cr.aggregate([str(self.root)])
        self.assertEqual((report["rows"], report["sessions_read"]), ([], 0))
        self.assertIn("sessions with a result record: 0", cr.render(report))

    def test_a_row_without_cache_traffic_has_no_rate_not_a_zero_division(self):
        write_log(self.run_dir() / "job" / "1-build-0.jsonl", [result(0, 0, 0, output=0)])
        row = cr.aggregate([str(self.run_dir())])["rows"][0]
        self.assertEqual((row["hit_rate"], row["thinking_share"], row["cold_start_share_of_writes"]), (None, None, None))

    def test_no_message_or_result_text_reaches_the_output(self):
        self.build()
        report = cr.aggregate([str(self.run_dir())])
        text = cr.render(report, check=True) + json.dumps(report)
        self.assertNotIn("CANARY", text)
        self.assertNotIn("run-a", text)  # no run, job or path name either
        self.assertNotIn(str(self.root), text)


class ReadOnlyTests(Base):
    def test_only_regular_files_are_read_and_no_symlink_is_followed(self):
        real = self.run_dir() / "job" / "1-build-0.jsonl"
        write_log(real, warm_session())
        outside = self.root / "elsewhere"
        write_log(outside / "1-build-0.jsonl", warm_session())
        os.symlink(outside, self.run_dir() / "linked-dir")
        os.symlink(outside / "1-build-0.jsonl", self.run_dir() / "job" / "1-review-0.jsonl")
        write_log(self.run_dir() / ".home-1-build-0" / "1-build-0.jsonl", warm_session())
        found = cr.find_logs([str(self.run_dir())])
        # the symlinked directory and the dot directory are not entered; a symlinked file is listed but never read
        self.assertEqual([os.path.basename(p) for p in found], ["1-build-0.jsonl", "1-review-0.jsonl"])
        self.assertIsNone(cr.read_session(str(self.run_dir() / "job" / "1-review-0.jsonl")))
        report = cr.aggregate([str(self.run_dir())])
        self.assertEqual((report["sessions_read"], report["skipped"]), (1, 1))
        self.assertEqual(cr.find_logs([str(self.run_dir() / "linked-dir")]), [])

    def test_the_directory_is_unchanged_after_a_report(self):
        write_log(self.run_dir() / "job" / "1-build-0.jsonl", warm_session())
        before = sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in self.root.rglob("*"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cr.main([str(self.run_dir()), "--check"]), 0)
        self.assertEqual(before, sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in self.root.rglob("*")))
        self.assertIn("build", out.getvalue())

    def test_json_output_is_one_document(self):
        write_log(self.run_dir() / "job" / "1-build-0.jsonl", warm_session())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cr.main([str(self.run_dir()), "--json"])
        self.assertEqual(json.loads(out.getvalue())["rows"][-1]["phase"], "all")

    def test_a_missing_path_is_just_empty(self):
        report = cr.aggregate([str(self.root / "nope")])
        self.assertEqual((report["rows"], report["sessions_read"]), ([], 0))


if __name__ == "__main__":
    unittest.main()

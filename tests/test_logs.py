"""Serving logs to an operator: redaction, plain text, an allow-list, no links, bounded ranges (docs/manager.md, "Log endpoints").

``redact`` and ``logs`` are tested on their own; the endpoints run on a real ``ManagerServer`` as in
tests/test_dashboard_api.py. The two things the issue insists on are checked at both levels: a log with
``<script>``, ANSI escapes and a token-shaped string comes out inert and redacted, and a Viewer cannot read it.
"""

import http.client
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from factory import logs, redact
from factory.dashboard import QueryError
from tests import test_manager as tm

TOKEN = f"ghs_{'A' * 36}"
PAT = f"github_pat_{'B' * 40}"
ANTHROPIC = f"sk-ant-api03-{'C' * 30}"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r"
SAS = f"https://acct.blob.core.windows.net/c/b?sv=2023-11-03&sig={'D' * 40}%3D"
KEY_BODY = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7VJTUt9Us8cKj"


class RedactTests(unittest.TestCase):
    def test_every_token_shape_is_replaced(self):
        for label, secret in {
            "installation token": TOKEN,
            "personal token": f"ghp_{'E' * 36}",
            "fine-grained token": PAT,
            "anthropic key": ANTHROPIC,
            "jwt": JWT,
        }.items():
            with self.subTest(label):
                out = redact.redact_text(f"before {secret} after")
                self.assertEqual(out, f"before {redact.MARK} after")

    def test_header_values_and_named_secrets_keep_their_name_and_lose_their_value(self):
        secret = "s3cretvalue1234"
        for text, kept in (
            (f"Authorization: Bearer {secret}", "Authorization: "),
            (f'"Authorization": "Basic {secret}"', '"Authorization": "'),
            (f"x-api-key: {secret}", "x-api-key: "),
            (f"curl -H 'Authorization: token {secret}' url", "curl -H 'Authorization: "),
            (f"got Bearer {secret} back", "got Bearer "),
            (f"ANTHROPIC_API_KEY={secret}", "ANTHROPIC_API_KEY="),
            (f'{{"client_secret":"{secret}"}}', '{"client_secret":"'),
            (f"password = {secret}", "password = "),
        ):
            with self.subTest(text):
                out = redact.redact_text(text)
                self.assertNotIn(secret, out)
                self.assertIn(kept, out)
                self.assertIn(redact.MARK, out)

    def test_a_shared_access_signature_loses_its_signature_only(self):
        out = redact.redact_text(SAS)
        self.assertNotIn("DDDD", out)
        self.assertIn("sv=2023-11-03&sig=", out)

    def test_ordinary_numbers_and_hashes_are_left_alone(self):
        for text in (
            '{"usage":{"input_tokens":1234,"output_tokens":56,"cache_read_input_tokens":7}}',
            "commit 0123456789abcdef0123456789abcdef01234567 merged",
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "max_tokens: 4096",
            "the token budget is large",
        ):
            with self.subTest(text):
                self.assertEqual(redact.redact_text(text), text)

    def test_a_private_key_block_is_dropped_across_lines_and_inside_one_json_line(self):
        block = f"-----BEGIN RSA PRIVATE KEY-----\n{KEY_BODY}\n{KEY_BODY}\n-----END RSA PRIVATE KEY-----"
        out = redact.redact_text(f"before\n{block}\nafter")
        self.assertEqual(out, f"before\n{redact.MARK}\nafter")
        inline = f'{{"text":"-----BEGIN PRIVATE KEY-----\\n{KEY_BODY}\\n-----END PRIVATE KEY-----","n":1}}'
        self.assertEqual(redact.redact_text(inline), f'{{"text":"{redact.MARK}","n":1}}')

    def test_the_body_of_a_key_seen_without_its_first_line_is_still_caught(self):
        self.assertEqual(redact.redact_text(f"{KEY_BODY}"), redact.MARK)

    def test_ansi_sequences_and_control_characters_are_removed_and_cannot_hide_a_token(self):
        out = redact.redact_text(f"\x1b[31mred\x1b[0m \x1b]0;title\x07ok\x9b2K\r\x00end gh\x1bs_{'A' * 36}")
        self.assertEqual(out, f"red okend {redact.MARK}")
        split = redact.redact_text(f"ghs_\x1b[0m{'A' * 36}")
        self.assertEqual(split, redact.MARK)

    def test_an_escaped_ansi_sequence_inside_json_text_is_removed(self):
        self.assertEqual(redact.redact_text('{"text":"\\u001b[31mred\\u001b[0m"}'), '{"text":"red"}')

    def test_bidirectional_overrides_and_separators_are_removed(self):
        self.assertEqual(redact.redact_text("a‮b⁦c d​e"), "abcde")

    def test_markup_is_not_touched_it_is_plain_text_that_nothing_renders(self):
        self.assertEqual(redact.redact_text("<script>alert(1)</script>"), "<script>alert(1)</script>")

    def test_a_hostile_long_line_finishes(self):
        for line in ("a" * 300_000, "Bearer " * 40_000, "token=" * 40_000, f"-----BEGIN {'A' * 30} " * 5000):
            redact.redact_text(line)

    def test_a_line_of_repeated_key_markers_is_linear_not_quadratic(self):
        # 37000 markers are a 999 KB line, the longest the retained copy reads; the quadratic pattern took 200 s.
        begin, end = "-----BEGIN PRIVATE KEY-----", "-----END PRIVATE KEY-----"
        for label, text in {
            "no end": begin * 37_000,
            "ends between lines": "\n".join([begin * 20_000, end, begin * 20_000]),
            "start and end pairs": f"{begin} {end}" * 18_000,
            "json strings": f'{{"a":"{begin}",' * 30_000,
            "json escaped quotes": f'"{begin}\\"' * 30_000,
        }.items():
            for spans_lines in (True, False):
                with self.subTest(label, spans_lines=spans_lines):
                    started = time.monotonic()
                    redact.redact_text(text, spans_lines=spans_lines)
                    self.assertLess(time.monotonic() - started, 10.0)

    def test_a_lone_start_marker_in_a_json_line_ends_with_its_string_and_the_line_stays_json(self):
        marker = "-----BEGIN PRIVATE KEY-----"
        line = json.dumps({"content": f"if text.startswith('{marker}'): flag()", "n": 1, "t": TOKEN})
        out = redact.redact_text(line, spans_lines=False)
        self.assertEqual(json.loads(out), {"content": f"if text.startswith('{redact.MARK}", "n": 1, "t": redact.MARK})
        body = json.dumps({"content": f"{marker}\n{KEY_BODY}\n{KEY_BODY}", "n": 2})  # a block cut off before its end
        self.assertEqual(json.loads(redact.redact_text(body, spans_lines=False)), {"content": redact.MARK, "n": 2})
        two = json.dumps({"a": marker, "b": f"x {marker} y"})
        self.assertEqual(json.loads(redact.redact_text(two, spans_lines=False)), {"a": redact.MARK, "b": f"x {redact.MARK}"})

    def test_a_json_line_never_opens_a_block_for_the_next_line(self):
        redactor = redact.Redactor(spans_lines=False)
        first = redactor.process('{"text":"grep -- -----BEGIN PRIVATE KEY----- src"}')
        self.assertEqual(first, f'{{"text":"grep -- {redact.MARK}"}}')
        self.assertFalse(redactor.in_key)
        self.assertEqual(redactor.process('{"type":"result"}'), '{"type":"result"}')
        self.assertIsNone(redactor.finish())

    def test_a_lone_start_marker_does_not_swallow_a_text_log_it_is_bounded_and_says_so(self):
        marker = "-----BEGIN PRIVATE KEY-----"
        body = [f"ordinary line {index}" for index in range(30)]
        with patch.object(redact, "MAX_KEY_LINES", 10):
            out = redact.redact_text("\n".join([f"saw {marker} in a file", *body])).split("\n")
        self.assertEqual(out[0], f"saw {redact.MARK}")
        self.assertEqual(out[1], "[10 lines withheld] ordinary line 10")  # the eleventh line is shown again
        self.assertEqual(out[2:], body[11:])
        self.assertEqual(len(out), 21)

    def test_a_block_over_the_byte_bound_is_given_up_as_well(self):
        marker = "-----BEGIN PRIVATE KEY-----"
        wide = "w " * 2500
        out = redact.redact_text("\n".join([marker, wide, wide, wide, wide, "after"])).split("\n")
        self.assertEqual(out, [redact.MARK, f"[3 lines withheld] {wide}", "after"])

    def test_text_that_ends_inside_a_block_says_how_many_lines_it_withheld(self):
        marker = "-----BEGIN RSA PRIVATE KEY-----"
        self.assertEqual(redact.redact_text(f"a\n{marker}\n{KEY_BODY}\n{KEY_BODY}"), f"a\n{redact.MARK}\n[2 lines withheld]")
        self.assertEqual(redact.redact_text(f"a\n{marker}\n{KEY_BODY}"), f"a\n{redact.MARK}\n[1 line withheld]")
        self.assertEqual(redact.redact_text(f"a\n{marker}"), f"a\n{redact.MARK}")  # nothing was withheld

    def test_a_key_that_ends_inside_the_bound_is_dropped_without_a_marker(self):
        body = [KEY_BODY] * 150  # a real key is under 110 lines; the bound is above it
        block = "\n".join(["-----BEGIN PRIVATE KEY-----", *body, "-----END PRIVATE KEY-----"])
        self.assertEqual(redact.redact_text(f"x\n{block}\ny"), f"x\n{redact.MARK}\ny")


def lines(count, width=50):
    return [f"line {index:05d} {'x' * width}" for index in range(count)]


class WindowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)

    def open(self, data):
        path = self.dir / "f.log"
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
        descriptor = os.open(path, os.O_RDONLY)
        self.addCleanup(os.close, descriptor)
        return descriptor

    def test_the_default_is_the_tail_and_earlier_text_is_reached_with_before(self):
        text = "\n".join([*lines(5000), ""])
        descriptor = self.open(text)
        tail = logs.view(descriptor, {})
        self.assertTrue(tail.more_before)
        self.assertFalse(tail.more_after)
        self.assertEqual(tail.end, len(text))
        self.assertTrue(tail.text.endswith(f"line 04999 {'x' * 50}"))
        self.assertLessEqual(len(tail.body()), logs.DEFAULT_LIMIT)
        earlier = logs.view(descriptor, {"before": tail.start})
        self.assertEqual(earlier.end, tail.start)
        self.assertEqual(f"{earlier.text}\n{tail.text}", text[earlier.start:tail.end].rstrip("\n"))

    def test_forward_ranges_cover_a_large_file_exactly_once_in_parts(self):
        text = "\n".join([*lines(20_000), ""])
        descriptor = self.open(text)
        seen, offset, parts = [], 0, 0
        while True:
            window = logs.view(descriptor, {"offset": offset, "limit": 100_000})
            self.assertLessEqual(len(window.body()), logs.MAX_BODY)
            parts += 1
            seen.append(window.text)
            self.assertEqual(window.start, offset)
            if not window.more_after:
                break
            self.assertGreater(window.end, offset)
            offset = window.end
        self.assertGreater(parts, 5)
        self.assertEqual("\n".join(seen), text.rstrip("\n"))

    def test_an_offset_inside_a_line_moves_to_the_next_line_start(self):
        descriptor = self.open("aaaa\nbbbb\ncccc\n")
        window = logs.view(descriptor, {"offset": 2})
        self.assertEqual((window.start, window.text), (5, "bbbb\ncccc"))
        self.assertEqual(logs.view(descriptor, {"offset": 99}).text, "")

    def test_a_secret_is_never_cut_by_a_window_boundary(self):
        secret_line = f"x token {TOKEN} y"
        text = "\n".join([*lines(3000, 20), secret_line, *lines(3000, 20), ""])
        descriptor = self.open(text)
        offset = 0
        shown = []
        while True:
            window = logs.view(descriptor, {"offset": offset, "limit": 4096})
            shown.append(window.text)
            if not window.more_after:
                break
            offset = window.end
        joined = "\n".join(shown)
        self.assertNotIn("AAAA", joined)
        self.assertIn(f"x token {redact.MARK} y", joined)

    def test_a_very_long_line_is_cut_with_its_length_and_never_half_a_token(self):
        line = f"{'word ' * 9000}{TOKEN}{' tail' * 2000}"
        descriptor = self.open(f"first\n{line}\nlast\n")
        window = logs.view(descriptor, {"offset": 0, "limit": logs.MAX_LIMIT})
        self.assertIn("first", window.text)
        self.assertIn(f"[line cut: {len(line)} bytes]", window.text)
        self.assertIn("last", window.text)
        self.assertLessEqual(len(window.body()), logs.MAX_BODY)

    def test_a_line_longer_than_a_window_shows_its_head_and_continues_after_it(self):
        line = "w" * 400_000
        descriptor = self.open(f"{line}\nnext\n")
        window = logs.view(descriptor, {"offset": 0, "limit": 2048})
        self.assertIn("[line cut: 400000 bytes]", window.text)
        self.assertEqual(window.end, 400_001)
        self.assertEqual(logs.view(descriptor, {"offset": window.end}).text, "next")
        self.assertEqual(logs.view(descriptor, {"tail": 2048}).text, "next")

    def test_invalid_utf8_is_replaced_and_the_answer_stays_inside_the_hard_cap(self):
        descriptor = self.open(b"\xff\xfe\n" * 200_000)
        window = logs.view(descriptor, {"tail": logs.MAX_LIMIT})
        self.assertLessEqual(len(window.body()), logs.MAX_BODY)
        self.assertIn("�", window.text)

    def test_a_lone_start_marker_in_a_transcript_does_not_hide_the_rest_of_it(self):
        events = [{"n": index, "content": "text"} for index in range(5)]
        events[1] = {"n": 1, "content": "if text.startswith('-----BEGIN PRIVATE KEY-----'): flag()"}
        events[4] = {"type": "result", "total_cost_usd": 0.5}
        text = "".join(f"{json.dumps(event)}\n" for event in events)
        for params in ({}, {"offset": 0}, {"tail": 4096}):
            with self.subTest(params):
                window = logs.view(self.open(text), params, jsonl=True)
                parsed = [json.loads(line) for line in window.text.splitlines()]
                self.assertEqual(len(parsed), 5)
                self.assertEqual(parsed[1]["content"], f"if text.startswith('{redact.MARK}")
                self.assertEqual(parsed[4], {"type": "result", "total_cost_usd": 0.5})

    def test_a_lone_start_marker_in_a_text_log_withholds_a_bounded_number_of_lines_and_says_so(self):
        text = "\n".join(["start", "grep -- -----BEGIN PRIVATE KEY----- src", "one", "two", "three", ""])
        window = logs.view(self.open(text), {})
        self.assertEqual(window.text.split("\n"), ["start", f"grep -- {redact.MARK}", "[3 lines withheld]"])
        many = "\n".join(["-----BEGIN PRIVATE KEY-----", *lines(redact.MAX_KEY_LINES + 20), ""])
        shown = logs.view(self.open(many), {}).text.split("\n")
        self.assertEqual(shown[0], redact.MARK)
        self.assertTrue(shown[1].startswith(f"[{redact.MAX_KEY_LINES} lines withheld] line {redact.MAX_KEY_LINES:05d} "))
        self.assertEqual(len(shown), 20 + 1)
        self.assertTrue(shown[-1].startswith(f"line {redact.MAX_KEY_LINES + 19:05d} "))

    def aged(self, path, seconds):
        old = os.stat(path).st_mtime - seconds
        os.utime(path, (old, old))

    def test_a_final_line_without_a_newline_is_shown_once_the_file_is_quiet(self):
        descriptor = self.open("one\ntwo")
        self.aged(self.dir / "f.log", logs.QUIET_SECONDS + 1)
        window = logs.view(descriptor, {})
        self.assertEqual((window.text, window.end, window.partial), ("one\ntwo", 7, False))

    def test_an_unfinished_last_line_is_left_out_while_the_file_is_being_written_and_never_lost(self):
        """Issue 226: the window ends at the start of the unfinished line, so the next forward read returns all of it."""
        path = self.dir / "f.log"
        descriptor = self.open("one\ntwo\npart")
        first = logs.view(descriptor, {})
        self.assertEqual((first.text, first.start, first.end, first.size, first.partial), ("one\ntwo", 0, 8, 12, True))
        self.assertTrue(first.more_after)
        with open(path, "ab") as handle:
            handle.write(b"ial line\nnext\n")
        later = logs.read_window(descriptor, offset=first.end)
        self.assertEqual((later.text, later.start, later.partial), ("partial line\nnext", 8, False))
        self.assertEqual(later.end, later.size)

    def test_the_same_holds_for_a_tail_window_and_for_a_file_that_is_one_unfinished_line(self):
        descriptor = self.open("one\ntwo\npart")
        tail = logs.read_window(descriptor, tail=4096)
        self.assertEqual((tail.text, tail.end, tail.partial), ("one\ntwo", 8, True))
        only = self.dir / "g.log"
        only.write_bytes(b"no newline yet")
        other = os.open(only, os.O_RDONLY)
        self.addCleanup(os.close, other)
        window = logs.read_window(other, offset=0)
        self.assertEqual((window.text, window.start, window.end, window.partial), ("", 0, 0, True))
        self.assertTrue(window.more_after)

    def test_the_quiet_time_is_measured_from_the_last_write_with_an_injected_clock(self):
        descriptor = self.open("one\ntwo")
        modified = os.fstat(descriptor).st_mtime
        self.assertEqual(logs.read_window(descriptor, now=modified + logs.QUIET_SECONDS - 0.5).text, "one")
        self.assertEqual(logs.read_window(descriptor, now=modified + logs.QUIET_SECONDS).text, "one\ntwo")

    def test_a_complete_file_is_unaffected_and_the_partial_header_says_so(self):
        text = "\n".join([*lines(50), ""])
        window = logs.view(self.open(text), {})
        self.assertEqual((window.end, window.partial), (len(text), False))
        self.assertEqual(logs.headers_for(window)["X-Log-Partial"], "false")
        unfinished = logs.view(self.open(text + "tail"), {})
        self.assertEqual(logs.headers_for(unfinished)["X-Log-Partial"], "true")

    def test_a_huge_file_is_never_read_whole(self):
        path = self.dir / "sparse.log"
        huge = 3 * 1024 * 1024 * 1024
        with open(path, "wb") as handle:
            handle.truncate(huge)
            handle.seek(huge - 32)
            handle.write(b"\nlast but one\nthe end of it all\n")
        descriptor = os.open(path, os.O_RDONLY)
        self.addCleanup(os.close, descriptor)
        sizes = []
        real = os.pread

        def spy(fd, count, offset):
            sizes.append(count)
            return real(fd, count, offset)

        with patch("factory.logs.os.pread", spy):
            tail = logs.view(descriptor, {"tail": 4096})
            head = logs.view(descriptor, {"offset": 0, "limit": 4096})
            middle = logs.view(descriptor, {"offset": 1_000_000_000})
        self.assertEqual(tail.text, "last but one\nthe end of it all")
        self.assertEqual(head.size, huge)
        self.assertLessEqual(max(sizes), max(logs.MAX_LIMIT, logs.CHUNK, logs.MAX_LINE + 1))
        self.assertLessEqual(len(sizes) * logs.CHUNK, 4 * logs.SCAN_BUDGET + 8 * logs.MAX_LIMIT)
        self.assertLessEqual(len(middle.body()), logs.MAX_BODY)


class OpenTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.run = self.base / "repos" / "org" / "name" / "runs" / "r1"
        self.run.mkdir(parents=True)
        self.parts = ("repos", "org", "name", "runs", "r1")

    def test_a_plain_file_opens_and_every_other_shape_does_not(self):
        (self.run / "plan.log").write_text("ok\n")
        descriptor = logs.open_log(self.base, self.parts, "plan.log", logs.RUN_FILES)
        os.close(descriptor)
        for name in ("append.log", "plan.log.bak", "../plan.log", "x.settings.json", "state.json", "", "plan.log/"):
            with self.subTest(name), self.assertRaises(logs.LogNotFound):
                logs.open_log(self.base, self.parts, name, logs.RUN_FILES)

    def test_the_job_shapes(self):
        for name in ("1-build-0.jsonl", "2-review-1.stderr", "1-verify.jsonl", "1-verify-baseline.stderr", "preflight-build-20261109T101010123456.jsonl"):
            with self.subTest(name):
                self.assertTrue(logs.JOB_FILE.fullmatch(name))
        for name in ("1-build-0.settings.json", "1-build-0.prompt.txt", "1-build-0.system.md", "1-build-0.json", "1-build-0.jsonl.bak", "pr-body.md", "x-build-0.jsonl", "1-deploy-0.jsonl", "1-build-0.JSONL", "../1-build-0.jsonl"):
            with self.subTest(name):
                self.assertIsNone(logs.JOB_FILE.fullmatch(name))

    def test_a_symlinked_file_is_refused(self):
        secret = self.base / "secret.txt"
        secret.write_text("TOP SECRET\n")
        os.symlink(secret, self.run / "plan.log")
        with self.assertRaises(logs.LogNotFound):
            logs.open_log(self.base, self.parts, "plan.log", logs.RUN_FILES)

    def test_a_symlink_at_any_depth_is_refused(self):
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "plan.log").write_text("TOP SECRET\n")
        for link, parts in (
            (self.run.parent / "r2", (*self.parts[:-1], "r2")),
            (self.base / "repos" / "org" / "alias", ("repos", "org", "alias", "runs", "r1")),
            (self.base / "repos" / "alias", ("repos", "alias", "name", "runs", "r1")),
        ):
            os.symlink(elsewhere, link)
            with self.subTest(parts=parts), self.assertRaises(logs.LogNotFound):
                logs.open_log(self.base, parts, "plan.log", logs.RUN_FILES)

    def test_a_hard_link_a_fifo_and_a_directory_are_refused(self):
        outside = self.base / "outside.txt"
        outside.write_text("TOP SECRET\n")
        os.link(outside, self.run / "plan.log")
        os.mkfifo(self.run / "supervisor.log")
        (self.run / "planner.jsonl").mkdir()
        for name in ("plan.log", "supervisor.log", "planner.jsonl"):
            with self.subTest(name), self.assertRaises(logs.LogNotFound):
                logs.open_log(self.base, self.parts, name, logs.RUN_FILES)

    def test_a_part_that_is_not_one_plain_name_is_refused(self):
        for part in ("..", ".", "a/b", "", "x\0y"):
            with self.subTest(part), self.assertRaises(logs.LogNotFound):
                logs.open_directory(self.base, ("repos", part))

    def test_a_listing_names_only_allow_listed_regular_files(self):
        job = self.run / "fix-a"
        job.mkdir()
        for name in ("1-build-0.jsonl", "1-build-0.stderr", "1-build-0.settings.json", "1-build-0.prompt.txt", "pr-body.md"):
            (job / name).write_text("x\n")
        os.symlink(self.base, job / "2-build-0.jsonl")
        descriptor = logs.open_directory(self.base, (*self.parts, "fix-a"))
        try:
            names = [item["name"] for item in logs.list_files(descriptor, logs.JOB_FILE)]
        finally:
            os.close(descriptor)
        self.assertEqual(names, ["1-build-0.jsonl", "1-build-0.stderr"])


class ParseViewTests(unittest.TestCase):
    def parse(self, query):
        from factory.dashboard import parse_query

        return logs.parse_view(parse_query, f"/x{query}")

    def test_the_two_forms_and_their_limits(self):
        self.assertEqual(self.parse(""), {})
        self.assertEqual(self.parse("?offset=10&limit=500"), {"offset": 10, "limit": 500})
        self.assertEqual(self.parse("?tail=500&before=9"), {"tail": 500, "before": 9})
        for bad in ("?offset=-1", "?offset=x", f"?limit={logs.MAX_LIMIT + 1}&offset=0", "?limit=5", "?offset=1&tail=1", "?tail=0",
                    "?before=1&offset=1", "?offset=1&offset=2", "?unknown=1", f"?offset={logs.MAX_OFFSET + 1}"):
            with self.subTest(bad), self.assertRaises(QueryError):
                self.parse(bad)


class LogCase(tm.ServerCase):
    def logs_dir(self, run="r1", repo="org/name"):
        directory = self.run_dir(run, repo)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def fetch(self, path, principal=tm.OPERATOR, method="GET"):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            headers = {} if principal is None else {"X-MS-CLIENT-PRINCIPAL": principal}
            connection.request(method, path, headers=headers)
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            connection.close()

    def text(self, path, principal=tm.OPERATOR):
        status, body, headers = self.fetch(path, principal)
        self.assertEqual(status, 200, body)
        return body.decode("utf-8"), headers

    base = "/repos/org/name/runs/r1"


class EndpointTests(LogCase):
    def setUp(self):
        super().setUp()
        self.run = self.logs_dir()
        self.job = self.run / "fix-a"
        self.job.mkdir()

    def test_the_issue_scenario_script_ansi_and_a_token_render_inert_and_redacted(self):
        hostile = f"<script>alert(1)</script> \x1b[31mred\x1b[0m {TOKEN} \x1b]0;pwned\x07done\n"
        (self.run / "supervisor.log").write_text(f"start\n{hostile}end\n")
        text, headers = self.text(f"{self.base}/logs/supervisor.log")
        self.assertEqual(text, f"start\n<script>alert(1)</script> red {redact.MARK} done\nend")
        self.assertEqual(headers["Content-Type"], "text/plain; charset=utf-8")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertIn("sandbox", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertNotIn(TOKEN, text)

    def test_every_line_of_a_transcript_is_redacted(self):
        events = [
            {"type": "assistant", "text": f"token {TOKEN}"},
            {"type": "tool", "input": f"curl -H 'Authorization: Bearer {PAT}' https://x"},
            {"type": "result", "text": ANTHROPIC, "jwt": JWT, "usage": {"input_tokens": 12}},
        ]
        (self.job / "1-build-0.jsonl").write_text("\n".join([*(json.dumps(event) for event in events), ""]))
        text, _ = self.text(f"{self.base}/jobs/fix-a/logs/1-build-0.jsonl")
        for secret in (TOKEN, PAT, ANTHROPIC, JWT):
            self.assertNotIn(secret, text)
        self.assertEqual(text.count(redact.MARK), 4)
        self.assertIn('"input_tokens": 12', text)
        parsed = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(len(parsed), 3)

    def test_a_lone_start_marker_does_not_hide_the_rest_of_a_transcript_or_a_log(self):
        events = [{"n": index} for index in range(5)]
        events[1] = {"n": 1, "content": "assert text.startswith('-----BEGIN PRIVATE KEY-----')"}
        events[4] = {"type": "result", "total_cost_usd": 0.5}
        (self.job / "1-build-0.jsonl").write_text("".join(f"{json.dumps(event)}\n" for event in events))
        text, headers = self.text(f"{self.base}/jobs/fix-a/logs/1-build-0.jsonl")
        parsed = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(len(parsed), 5)
        self.assertEqual(parsed[4]["total_cost_usd"], 0.5)
        self.assertEqual(headers["X-Log-End"], headers["X-Log-Size"])
        (self.run / "plan.log").write_text("start\ngrep -- -----BEGIN PRIVATE KEY----- src\none\ntwo\n")
        text, _ = self.text(f"{self.base}/logs/plan.log")
        self.assertEqual(text, f"start\ngrep -- {redact.MARK}\n[2 lines withheld]")

    def test_stderr_and_the_planner_transcript_are_served_too(self):
        (self.job / "1-review-0.stderr").write_text("warning: x\n")
        (self.run / "planner.jsonl").write_text('{"a":1}\n')
        (self.run / "plan.log").write_text("planning\n")
        self.assertEqual(self.text(f"{self.base}/jobs/fix-a/logs/1-review-0.stderr")[0], "warning: x")
        self.assertEqual(self.text(f"{self.base}/logs/planner.jsonl")[0], '{"a":1}')
        self.assertEqual(self.text(f"{self.base}/logs/plan.log")[0], "planning")

    def test_a_viewer_gets_403_everywhere_and_an_anonymous_caller_401(self):
        (self.run / "plan.log").write_text("x\n")
        (self.job / "1-build-0.jsonl").write_text("x\n")
        paths = (f"{self.base}/logs", f"{self.base}/logs/plan.log", f"{self.base}/jobs/fix-a/logs", f"{self.base}/jobs/fix-a/logs/1-build-0.jsonl")
        for path in paths:
            with self.subTest(path):
                status, body, _ = self.fetch(path, tm.VIEWER)
                self.assertEqual((status, json.loads(body)), (403, {"error": "forbidden"}))
                status, body, _ = self.fetch(path, None)
                self.assertEqual((status, json.loads(body)), (401, {"error": "authentication required"}))
                self.assertEqual(self.fetch(path, tm.principal_header([]))[0], 403)
        self.assertEqual((self.runner.calls, self.spawner.calls), ([], []))

    def test_only_get_is_allowed(self):
        (self.run / "plan.log").write_text("x\n")
        status, body, headers = self.fetch(f"{self.base}/logs/plan.log", method="POST")
        self.assertEqual((status, headers["Allow"]), (405, "GET"))

    def test_names_outside_the_allow_list_are_not_found(self):
        (self.run / "plan.log").write_text("x\n")
        (self.run / "state.json").write_text("{}")
        (self.run / "append.log").write_text("x\n")
        (self.job / "1-build-0.settings.json").write_text('{"env":{"K":"V"}}')
        (self.job / "1-build-0.prompt.txt").write_text("secret prompt")
        for path in (
            f"{self.base}/logs/state.json",
            f"{self.base}/logs/append.log",
            f"{self.base}/logs/..%2fstate.json",
            f"{self.base}/logs/%2e%2e",
            f"{self.base}/logs/plan.log%00",
            f"{self.base}/jobs/fix-a/logs/1-build-0.settings.json",
            f"{self.base}/jobs/fix-a/logs/1-build-0.prompt.txt",
            f"{self.base}/jobs/fix-a/logs/..%2fplan.log",
            f"{self.base}/jobs/fix-a/logs/missing.jsonl",
            f"{self.base}/jobs/fix-a/logs/2-build-0.jsonl",
            "/repos/org/other/runs/r1/logs/plan.log",
            "/repos/no/body/runs/r1/logs/plan.log",
        ):
            with self.subTest(path):
                status, body, _ = self.fetch(path)
                self.assertEqual((status, json.loads(body)), (404, {"error": "not found"}))

    def test_a_job_and_a_run_name_are_validated_first(self):
        self.assertEqual(self.fetch("/repos/org/name/runs/..%2f/logs")[0], 400)
        self.assertEqual(self.fetch(f"{self.base}/jobs/..%2f..%2fx/logs")[0], 400)
        self.assertEqual(self.fetch(f"{self.base}/jobs/{'a' * 70}/logs/1-build-0.jsonl")[0], 400)

    def test_a_symlinked_log_run_or_job_directory_is_refused(self):
        secret = Path(self.data_dir) / "secret.txt"
        secret.write_text("TOP SECRET\n")
        os.symlink(secret, self.run / "plan.log")
        os.symlink(secret, self.job / "1-build-0.jsonl")
        os.link(secret, self.run / "supervisor.log")
        elsewhere = Path(self.data_dir) / "elsewhere"
        (elsewhere / "fix-b").mkdir(parents=True)
        (elsewhere / "fix-b" / "1-build-0.jsonl").write_text("TOP SECRET\n")
        (elsewhere / "plan.log").write_text("TOP SECRET\n")
        os.symlink(elsewhere / "fix-b", self.run / "fix-b")
        os.symlink(elsewhere, self.run.parent / "r9")
        for path in (
            f"{self.base}/logs/plan.log",
            f"{self.base}/jobs/fix-a/logs/1-build-0.jsonl",
            f"{self.base}/logs/supervisor.log",
            f"{self.base}/jobs/fix-b/logs/1-build-0.jsonl",
            "/repos/org/name/runs/r9/logs/plan.log",
        ):
            with self.subTest(path):
                status, body, _ = self.fetch(path)
                self.assertEqual(status, 404)
                self.assertNotIn(b"TOP SECRET", body)
        self.assertEqual(self.fetch(f"{self.base}/jobs/fix-b/logs")[1], b'{"repo":"org/name","run":"r1","job":"fix-b","files":[]}')

    def test_the_listings_name_only_allow_listed_files_with_their_sizes(self):
        (self.run / "plan.log").write_text("abc\n")
        (self.run / "state.json").write_text("{}")
        (self.run / "planner.stderr").write_text("")
        (self.job / "1-build-0.jsonl").write_text("12345\n")
        (self.job / "1-build-0.settings.json").write_text("{}")
        status, body, headers = self.fetch(f"{self.base}/logs")
        listing = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual([(f["name"], f["size"]) for f in listing["files"]], [("plan.log", 4), ("planner.stderr", 0)])
        listing = json.loads(self.fetch(f"{self.base}/jobs/fix-a/logs")[1])
        self.assertEqual([(f["name"], f["size"]) for f in listing["files"]], [("1-build-0.jsonl", 6)])
        self.assertEqual(json.loads(self.fetch(f"{self.base}/jobs/none-yet/logs")[1])["files"], [])
        self.assertEqual(self.fetch("/repos/org/name/runs/gone/logs")[0], 404)

    def test_a_large_log_is_served_in_parts_with_the_range_in_headers(self):
        body = "\n".join([*lines(30_000), ""])
        (self.job / "1-build-0.stderr").write_text(body)
        path = f"{self.base}/jobs/fix-a/logs/1-build-0.stderr"
        text, headers = self.text(path)
        self.assertLessEqual(len(text.encode()), logs.DEFAULT_LIMIT)
        self.assertEqual(headers["X-Log-Size"], str(len(body)))
        self.assertEqual((headers["X-Log-More-Before"], headers["X-Log-More-After"]), ("true", "false"))
        self.assertEqual(int(headers["X-Log-End"]), len(body))
        offset, seen, parts = 0, [], 0
        while True:
            text, headers = self.text(f"{path}?offset={offset}&limit=200000")
            parts += 1
            seen.append(text)
            self.assertLessEqual(len(text.encode()), logs.MAX_BODY)
            self.assertEqual(int(headers["X-Log-Start"]), offset)
            if headers["X-Log-More-After"] == "false":
                break
            offset = int(headers["X-Log-End"])
        self.assertGreater(parts, 5)
        self.assertEqual("\n".join(seen), body.rstrip("\n"))
        text, headers = self.text(f"{path}?tail=1024&before={int(headers['X-Log-Start'])}")
        self.assertEqual(headers["X-Log-More-After"], "true")

    def test_bad_query_parameters_are_400_and_never_echoed(self):
        (self.run / "plan.log").write_text("x\n")
        for query in ("?offset=-5", "?limit=1", "?tail=0", "?offset=1&tail=1", "?x=1", f"?tail={logs.MAX_LIMIT + 1}", "?offset=1&offset=2", "?tail=%3Cscript%3E"):
            with self.subTest(query):
                status, body, _ = self.fetch(f"{self.base}/logs/plan.log{query}")
                self.assertEqual(status, 400)
                self.assertNotIn(b"script", body)
        self.assertEqual(self.fetch(f"{self.base}/logs?offset=1")[0], 400)

    def test_the_audit_line_names_the_principal_path_and_bytes_and_never_content(self):
        (self.run / "plan.log").write_text(f"CANARY-{TOKEN}-CONTENT\n")
        text, _ = self.text(f"{self.base}/logs/plan.log")
        lines_ = self.audit_lines()
        self.assertEqual(len(lines_), 1)
        self.assertIn(f"principal={tm.OID} method=GET path={self.base}/logs/plan.log status=200", lines_[0])
        self.assertTrue(lines_[0].endswith(f" bytes={len(text.encode())}"), lines_[0])
        for output in (self.out.getvalue(), self.err.getvalue()):
            self.assertNotIn("CANARY", output)
            self.assertNotIn(TOKEN, output)
        self.out.truncate(0)
        self.out.seek(0)
        self.fetch(f"{self.base}/logs/plan.log", tm.VIEWER)
        self.assertNotIn("bytes=", self.audit_lines()[0])

    def test_no_process_is_started_to_read_a_log(self):
        (self.run / "plan.log").write_text("x\n")
        self.text(f"{self.base}/logs/plan.log")
        self.assertEqual((self.runner.calls, self.spawner.calls), ([], []))


class JwtLogTests(tm.JwtCase):
    def setUp(self):
        super().setUp()
        directory = self.run_dir()
        directory.mkdir(parents=True)
        (directory / "plan.log").write_text("x\n")

    def test_the_operator_role_reads_and_the_viewer_role_does_not(self):
        path = "/repos/org/name/runs/r1/logs"
        self.assertEqual(self.jcall("GET", path, None)[0], 401)
        self.assertEqual(self.jcall("GET", path, self.token(roles=["Factory.Viewer"]))[:2], (403, {"error": "forbidden"}))
        self.assertEqual(self.jcall("GET", path, self.token(roles=["Some.Other"]))[0], 403)
        status, payload, _ = self.jcall("GET", path, self.token())
        self.assertEqual((status, [f["name"] for f in payload["files"]]), (200, ["plan.log"]))


if __name__ == "__main__":
    unittest.main()

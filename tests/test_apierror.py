"""The API-error classifier (src/factory/apierror.py) and where the agent failure path uses it.

Every event here is an INJECTED, ASSUMED shape: the real ``claude`` error for a spend limit, a 401 and a
429 is not recorded yet (docs/capacity.md, "Stop-the-world breaker"), so these tests pin the rules, not
the real shapes. Anything the table does not recognise must be ``other`` or ``rate_limit_unknown``, never
``spend_limit``, and no agent text may leave the classifier.
"""

import json
import unittest
from unittest import mock

from factory import agent, apierror
from factory.agent import AgentError
from tests.test_agent import AgentTestCase

POISON = "POISON-agent-authored-text-9f3c"


def result(**fields):
    return {"type": "result", "subtype": "success", "is_error": True, **fields}


def lines(*events):
    return "\n".join(json.dumps(event) for event in events)


class ClassifyTests(unittest.TestCase):
    def test_a_429_with_the_spend_cap_type_is_a_spend_limit(self):
        for event in (
            result(api_error_status=429, result='API Error: 429 {"error":{"type":"enforced_spend_limit_reached"}}'),
            result(api_error_status=429, error={"type": "enforced_spend_limit_reached"}),
            result(api_error_status=429, error={"error": {"code": "ENFORCED_SPEND_LIMIT_REACHED"}}),
            result(api_error_status=429, terminal_reason="enforced_spend_limit_reached"),
            result(api_error_status="429", subtype="enforced_spend_limit_reached"),
        ):
            with self.subTest(event=event):
                self.assertEqual(apierror.classify(event), "spend_limit")

    def test_a_429_with_a_rate_limit_class_or_a_retry_after_is_a_rate_limit(self):
        for event in (
            result(api_error_status=429, error={"type": "rate_limit_error"}),
            result(api_error_status=429, result='API Error: 429 {"error":{"type":"rate_limit_error"}}'),
            result(api_error_status=429, retry_after=30),
            result(api_error_status=429, error={"retry-after": "12"}),
            result(api_error_status=429, retry_after_ms=1500),
        ):
            with self.subTest(event=event):
                self.assertEqual(apierror.classify(event), "rate_limit")

    def test_a_spend_limit_wins_over_a_rate_limit_hint_in_the_same_event(self):
        event = result(api_error_status=429, retry_after=5, result="enforced_spend_limit_reached rate_limit_error")
        self.assertEqual(apierror.classify(event), "spend_limit")

    def test_a_429_nobody_recognises_is_rate_limit_unknown_never_a_spend_limit(self):
        for event in (
            result(api_error_status=429),
            result(api_error_status=429, result="quota exceeded for the month"),
            result(api_error_status=429, retry_after=0),
            result(api_error_status=429, retry_after=True),
            result(api_error_status=429, error={"type": "something_new"}),
        ):
            with self.subTest(event=event):
                self.assertEqual(apierror.classify(event), "rate_limit_unknown")

    def test_overloaded_and_auth(self):
        self.assertEqual(apierror.classify(result(api_error_status=529)), "overloaded")
        self.assertEqual(apierror.classify(result(api_error_status=503, error={"type": "overloaded_error"})), "overloaded")
        self.assertEqual(apierror.classify(result(api_error_status=401)), "auth")
        self.assertEqual(apierror.classify(result(api_error_status="401")), "auth")

    def test_other_api_errors_are_other(self):
        for event in (
            result(api_error_status=400, result="enforced_spend_limit_reached"),  # the marker is for a 429 only
            result(api_error_status=403),
            result(api_error_status=500),
            result(api_error_status=503),  # without the overloaded type
            result(error="something_unrecognised"),
            result(error={"type": "invalid_request_error"}),
        ):
            with self.subTest(event=event):
                self.assertEqual(apierror.classify(event), "other")

    def test_an_event_without_api_error_evidence_is_none(self):
        for event in (
            {"type": "result", "subtype": "success", "is_error": False},
            result(subtype="error_max_turns"),
            result(subtype="error_max_budget_usd", terminal_reason="api_error"),
            result(result="enforced_spend_limit_reached 429"),  # text alone is never evidence
            result(api_error_status=True),
            result(api_error_status=42),
            result(api_error_status="4290"),
            result(error={}),
            {},
            None,
            "not an event",
            [],
        ):
            with self.subTest(event=event):
                self.assertEqual(apierror.classify(event), "none")

    def test_free_text_is_searched_only_under_a_status_and_never_copied(self):
        """A model can write any text; only an event the CLI marked with a status is read for type words."""
        planted = result(result=f"{POISON} enforced_spend_limit_reached")
        self.assertEqual(apierror.classify(planted), "none")
        planted = result(api_error_status=500, result=f"{POISON} enforced_spend_limit_reached")
        self.assertEqual(apierror.classify(planted), "other")
        for event in (planted, result(api_error_status=429, result=POISON, terminal_reason=POISON)):
            code = apierror.classify(event)
            self.assertIn(code, apierror.CODES)
            self.assertNotIn(POISON, code)

    def test_the_text_reader_itself_refuses_free_text_without_a_status(self):
        """Defence in depth: the table only reaches a row under an integer status, and the reader checks it again."""
        event = {"result": "enforced_spend_limit_reached"}
        self.assertEqual(apierror._words(event, (apierror.RESULT,), None), [])
        self.assertEqual(apierror._words(event, (apierror.RESULT,), 429), ["enforced_spend_limit_reached"])

    def test_a_huge_text_is_bounded(self):
        late = result(api_error_status=429, result=f"{'x' * apierror.MAX_TEXT}enforced_spend_limit_reached")
        self.assertEqual(apierror.classify(late), "rate_limit_unknown")  # the marker lies beyond what is read

    def test_a_deeply_nested_error_object_does_not_recurse_without_bound(self):
        nested = {"type": "x"}
        for _ in range(500):
            nested = {"error": nested}
        self.assertEqual(apierror.classify(result(api_error_status=429, error=nested)), "rate_limit_unknown")

    def test_a_retry_after_is_a_positive_decimal_in_ascii_digits_and_nothing_else(self):
        for value in ("30", " 30 ", "1.5", ".5", "5.", "0.001", "9" * 40):
            with self.subTest(value=value):
                self.assertEqual(apierror.classify(result(api_error_status=429, retry_after=value)), "rate_limit")
        for value in ("0", "0.0", "", "  ", ".", "-1", "+1", "1e5", "nan", "inf", "1.2.3", "Wed, 21 Oct 2026 07:28:00 GMT"):
            with self.subTest(value=value):
                self.assertEqual(apierror.classify(result(api_error_status=429, retry_after=value)), "rate_limit_unknown")

    def test_a_malformed_retry_after_cannot_raise_and_is_no_retry_after(self):
        """``str.isdigit`` is true for digits that ``float`` refuses: they must neither raise nor count."""
        odd = ("\u00b2", "\u2461", "1\u00b3", "\u0661\u0662\u0663", "\uff11\uff12", "5\x00", "9" * 1000, "\u00bd")
        for value in odd:
            for event in (
                result(api_error_status=429, retry_after=value),
                result(api_error_status=429, error={"type": "x", "retry-after": value}),
                result(api_error_status=429, retry_after_seconds=value),
                result(error={"type": "x", "retry_after": value}),
            ):
                with self.subTest(value=value, event=event):
                    expected = "rate_limit_unknown" if "api_error_status" in event else "other"
                    self.assertEqual(apierror.classify(event), expected)

    def test_hostile_values_in_every_field_never_raise_and_stay_in_the_closed_set(self):
        odd = (None, True, 0, -1, 2**80, 1.5, float("nan"), float("inf"), "", "\u00b2", "\x00", "x" * 10000, [], {}, [1], {"a": 1})
        for name in (*apierror.STATUS_FIELDS, *apierror.RETRY_AFTER_FIELDS, "error", "result", "subtype", "terminal_reason"):
            for value in odd:
                with self.subTest(field=name, value=repr(value)[:20]):
                    code = apierror.classify({**result(api_error_status=429), name: value})
                    self.assertIn(code, apierror.CODES)

    def test_a_fault_inside_the_classifier_is_none_and_never_propagates(self):
        with mock.patch.object(apierror, "_classify_event", side_effect=ValueError(POISON)):
            self.assertEqual(apierror.classify(result(api_error_status=429)), "none")
            self.assertEqual(apierror.classify(None, lines({"api_error_status": 429})), "none")

    def test_a_fault_on_one_line_of_the_tail_does_not_hide_an_older_api_error(self):
        tail = lines({"api_error_status": 401}, {"api_error_status": 429})
        with mock.patch.object(apierror, "_classify_event", side_effect=[ValueError("newest"), "auth"]):
            self.assertEqual(apierror.classify(None, tail), "auth")

    def test_the_codes_are_the_closed_set(self):
        self.assertEqual(
            apierror.CODES,
            ("spend_limit", "rate_limit", "overloaded", "auth", "rate_limit_unknown", "other", "none"),
        )
        for row in apierror.SIGNATURES:
            self.assertIn(row.code, apierror.CODES)
            self.assertNotIn(row.code, ("other", "none", "rate_limit_unknown"))

    def test_the_signature_table_names_the_fields_it_reads_and_puts_the_spend_cap_first(self):
        known = {apierror.STATUS, apierror.TERMINAL, apierror.SUBTYPE, apierror.ERROR, apierror.RESULT}
        for row in apierror.SIGNATURES:
            with self.subTest(row=row.code, statuses=row.statuses):
                self.assertTrue(set(row.reads) <= known)
                self.assertIn(apierror.STATUS, row.reads)
                self.assertTrue(row.statuses and all(isinstance(status, int) for status in row.statuses))
        codes = [row.code for row in apierror.SIGNATURES]
        self.assertEqual(codes[0], "spend_limit")
        self.assertLess(codes.index("spend_limit"), codes.index("rate_limit"))
        spend = apierror.SIGNATURES[0]
        self.assertEqual(spend.statuses, (429,))
        self.assertFalse(spend.retry_after)  # a retry-after never makes a spend limit

    def test_a_marker_in_a_field_the_row_does_not_read_is_ignored(self):
        """The overloaded row reads the error and the result, not the terminal reason or the subtype."""
        event = result(api_error_status=503, terminal_reason="overloaded_error", subtype="overloaded_error")
        self.assertEqual(apierror.classify(event), "other")


class TailTests(unittest.TestCase):
    def test_with_no_result_event_the_newest_api_error_of_the_tail_decides(self):
        tail = lines(
            {"type": "system", "subtype": "init"},
            {"type": "system", "subtype": "api_retry", "error_status": 429, "error": "rate_limit_error"},
            {"type": "assistant", "message": {"content": []}},
            {"type": "system", "subtype": "api_retry", "error_status": 429, "error": {"type": "enforced_spend_limit_reached"}},
            {"type": "assistant", "message": {"content": []}},
        )
        self.assertEqual(apierror.classify(None, tail), "spend_limit")

    def test_a_result_event_is_used_and_the_tail_is_not(self):
        tail = lines({"api_error_status": 429, "error": "enforced_spend_limit_reached"})
        self.assertEqual(apierror.classify(result(api_error_status=401), tail), "auth")
        self.assertEqual(apierror.classify({"type": "result", "is_error": False}, tail), "none")

    def test_garbage_and_non_objects_are_skipped(self):
        tail = "\n".join(["not json", "[1, 2]", "{broken", '"text"', "", "   ", lines({"api_error_status": 401})])
        self.assertEqual(apierror.classify(None, tail), "auth")
        self.assertEqual(apierror.classify(None, tail.encode()), "auth")
        for empty in (None, "", b"", "no events here", 17):
            self.assertEqual(apierror.classify(None, empty), "none")

    def test_only_a_bounded_number_of_lines_is_read(self):
        old = lines({"api_error_status": 401})
        tail = "\n".join([old, *['{"type":"assistant"}'] * apierror.MAX_TAIL_LINES])
        self.assertEqual(apierror.classify(None, tail), "none")

    def test_a_malformed_retry_after_in_the_tail_cannot_raise(self):
        tail = lines({"error_status": 429, "retry_after": "\u00b2"})
        self.assertEqual(apierror.classify(None, tail), "rate_limit_unknown")

    def test_a_tail_event_without_a_status_or_error_is_not_evidence(self):
        tail = lines({"type": "assistant", "message": {"content": [{"type": "text", "text": "enforced_spend_limit_reached"}]}})
        self.assertEqual(apierror.classify(None, tail), "none")


class AgentFailureTests(AgentTestCase):
    """The code reaches ``AgentError`` and nothing else: the message and the usage are what they were."""

    def test_a_failed_session_carries_the_code_and_the_message_is_unchanged(self):
        self.stub(
            "emit(None, is_error=True, api_error_status=429, terminal_reason='api_error', "
            "result='API Error: 429 enforced_spend_limit_reached POISON-text')\nsys.exit(1)"
        )
        with self.assertRaises(AgentError) as caught:
            self.run_phase("build-0")
        self.assertEqual(caught.exception.api_error, "spend_limit")
        message = str(caught.exception)
        self.assertEqual(
            message,
            f"agent failed (subtype=success, is_error=true, terminal_reason=api_error, api_error_status=429); "
            f"see {self.run_dir}/0-build-0.jsonl and {self.run_dir}/0-build-0.stderr",
        )
        self.assertNotIn("POISON", message)
        self.assertEqual(caught.exception.usage["total_cost_usd"], 0.0125)  # the spend stays accountable

    def test_a_malformed_retry_after_still_raises_the_agent_error_with_the_usage_and_no_agent_text(self):
        self.stub(
            "emit(None, is_error=True, api_error_status=429, retry_after='\\u00b2', result='POISON-text')\nsys.exit(1)"
        )
        with self.assertRaises(AgentError) as caught:
            self.run_phase("build-0")
        self.assertEqual(caught.exception.api_error, "rate_limit_unknown")
        self.assertEqual(caught.exception.usage["total_cost_usd"], 0.0125)  # the spend stays accountable
        self.assertNotIn("POISON", str(caught.exception))
        self.assertNotIn("\u00b2", str(caught.exception))

    def test_a_malformed_retry_after_in_the_log_still_raises_the_agent_error_not_a_value_error(self):
        self.stub(
            "print(json.dumps({'type': 'system', 'subtype': 'api_retry', 'error_status': 429, "
            "'retry_after': '\\u00b2'}), flush=True)\nsys.exit(1)"
        )
        with self.assertRaises(AgentError) as caught:
            self.run_phase("build-0")
        self.assertEqual(caught.exception.api_error, "rate_limit_unknown")

    def test_a_failure_that_is_no_api_error_carries_no_code(self):
        self.stub("emit(None, subtype='error_max_turns', is_error=True)\nsys.exit(1)")
        with self.assertRaises(AgentError) as caught:
            self.run_phase("build-0")
        self.assertIsNone(caught.exception.api_error)

    def test_an_error_made_without_a_code_has_none(self):
        self.assertIsNone(AgentError("x").api_error)
        self.assertIsNone(AgentError("x", {"total_cost_usd": 1}).api_error)

    def test_no_result_event_but_an_api_error_in_the_log_is_an_agent_error_with_the_code(self):
        self.stub(
            "print(json.dumps({'type': 'system', 'subtype': 'api_retry', 'error_status': 429, "
            "'error': {'type': 'enforced_spend_limit_reached'}}), flush=True)\nsys.exit(1)"
        )
        with self.assertRaises(AgentError) as caught:
            self.run_phase("build-0")
        self.assertEqual(caught.exception.api_error, "spend_limit")
        self.assertEqual(str(caught.exception), f"agent exited 1; see {self.run_dir}/0-build-0.stderr")

    def test_no_result_event_and_nothing_in_the_log_stays_a_plain_runtime_error(self):
        self.stub("print(json.dumps({'type': 'system', 'subtype': 'init'}))\nsys.exit(3)")
        with self.assertRaises(RuntimeError) as caught:
            self.run_phase("build-0")
        self.assertIs(type(caught.exception), RuntimeError)
        self.assertEqual(str(caught.exception), f"agent exited 3; see {self.run_dir}/0-build-0.stderr")

    def test_the_log_tail_is_bounded_and_drops_a_partial_first_line(self):
        path = self.root / "big.jsonl"
        path.write_text("".join(f'{{"n": {n}}}\n' for n in range(10000)))
        tail = agent.log_tail(path, limit=100)
        self.assertLessEqual(len(tail), 100)
        for line in tail.splitlines():
            self.assertTrue(json.loads(line)["n"] >= 0)  # every line that is left parses
        self.assertEqual(agent.log_tail(self.root / "missing.jsonl"), "")
        self.assertEqual(agent.log_tail(path, limit=10**9).count("\n"), 10000)


if __name__ == "__main__":
    unittest.main()

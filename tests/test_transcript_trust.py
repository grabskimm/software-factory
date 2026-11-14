"""A transcript may come from a pod: nothing in it is trusted when it is read, recorded or summed."""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from factory import agent, backend, podcontract, podpool, status
from factory.runner import Runner


def result(**fields):
    return {"type": "result", "subtype": "success", "is_error": False, **fields}


class SubtypeTests(unittest.TestCase):
    HOSTILE = "x\nfactory: job gh-1: status=merged\n" + "A" * 5000

    def test_a_hostile_subtype_never_reaches_the_failure_message(self):
        error = backend._failure({"type": "result", "subtype": self.HOSTILE, "is_error": True}, "/data/p", {})
        self.assertEqual(str(error), "agent failed (subtype=unknown, is_error=true); see /data/p.jsonl and /data/p.stderr")

    def test_a_token_subtype_and_a_missing_one_are_kept_as_before(self):
        self.assertEqual(agent.subtype_of("error_max_turns"), "error_max_turns")
        self.assertIsNone(agent.subtype_of(None))
        for value in ("a b", "", "a" * 65, 7, ["success"], {"a": 1}, "ok\n"):
            self.assertEqual(agent.subtype_of(value), "unknown", repr(value))

    def test_the_usage_record_stores_the_subtype_as_a_token(self):
        self.assertEqual(agent.usage_record(result(subtype=self.HOSTILE), "build", "m")["subtype"], "unknown")
        self.assertEqual(agent.usage_record(result(), "build", "m")["subtype"], "success")


class UsageShapeTests(unittest.TestCase):
    def record(self, **fields):
        return agent.usage_record(result(**fields), "build", "m")

    def test_a_normal_usage_object_is_kept(self):
        usage = {
            "input_tokens": 3,
            "output_tokens": 4,
            "cache_creation_input_tokens": 50,
            "cache_read_input_tokens": 500,
            "cache_creation": {"ephemeral_5m_input_tokens": 45, "ephemeral_1h_input_tokens": 5},
            "service_tier": "standard",
        }
        model_usage = {"claude-opus-5-5[1m]": {"inputTokens": 3, "costUSD": 0.5, "contextWindow": 1000000}, "other": {}}
        record = self.record(usage=usage, modelUsage=model_usage)
        self.assertEqual(record["usage"], usage)
        self.assertEqual(record["model_usage"], model_usage)

    def test_values_that_are_not_finite_non_negative_numbers_are_dropped(self):
        usage = {
            "ok": 1,
            "neg": -1,
            "inf": float("inf"),
            "nan": float("nan"),
            "huge": 2**70,
            "flag": True,
            "text": "a\nb",
            "long": "a" * 65,
            "list": [1, 2],
            "bad key\n": 1,
            "k" * 65: 1,
        }
        self.assertEqual(self.record(usage=usage)["usage"], {"ok": 1})

    def test_the_sizes_are_bounded(self):
        usage = {f"k{i}": i for i in range(1000)}
        self.assertEqual(len(self.record(usage=usage)["usage"]), 32)
        models = {f"m{i}": {f"k{i}": 1} for i in range(1000)}
        record = self.record(modelUsage=models)
        self.assertEqual(len(record["model_usage"]), 16)
        nested = {"a": {"b": {"c": {"d": 1}}}}
        self.assertEqual(self.record(usage=nested)["usage"], {"a": {"b": {}}})
        self.assertLess(len(json.dumps(self.record(usage=usage, modelUsage=models))), 20_000)

    def test_a_model_usage_that_is_not_an_object_of_objects_is_dropped(self):
        record = self.record(modelUsage={"m": 5, "n\nx": {"a": 1}, "ok": {"a": 1, "b": {"c": 1}}})
        self.assertEqual(record["model_usage"], {"ok": {"a": 1}})
        self.assertEqual(self.record(modelUsage=["x"])["model_usage"], {})

    def test_the_counts_are_never_negative_or_not_finite(self):
        record = self.record(num_turns=-5, duration_ms=float("inf"))
        self.assertEqual((record["num_turns"], record["duration_ms"]), (0, 0))


class CostTests(unittest.TestCase):
    def test_a_negative_or_not_finite_cost_is_never_recorded(self):
        for value in (-1e6, -0.0001, float("nan"), float("inf"), float("-inf"), True, "5", None, [1]):
            record = agent.usage_record(result(total_cost_usd=value), "build", "m")
            self.assertEqual(record["total_cost_usd"], 0, repr(value))

    def test_a_huge_cost_is_clamped_to_the_pool_bound(self):
        self.assertEqual(agent.MAX_COST_USD, podpool.MAX_USD)
        self.assertEqual(agent.usage_record(result(total_cost_usd=1e12), "build", "m")["total_cost_usd"], podpool.MAX_USD)
        self.assertEqual(agent.usage_record(result(total_cost_usd=0.0125), "build", "m")["total_cost_usd"], 0.0125)

    def test_cost_usd_ignores_negative_and_not_finite_values_in_stored_records(self):
        records = [{"total_cost_usd": -1e6}, {"total_cost_usd": float("nan")}, {"total_cost_usd": float("inf")}, {"total_cost_usd": 0.5}]
        self.assertEqual(status.cost_usd(records), 0.5)

    def test_cost_usd_stays_inside_the_bound_the_pool_accepts(self):
        self.assertEqual(status.cost_usd([{"total_cost_usd": 1e12}, {"total_cost_usd": 1e12}]), podpool.MAX_USD)

    def test_a_hostile_session_neither_deflates_the_run_cost_nor_locks_the_pool_out(self):
        hostile = agent.usage_record(result(total_cost_usd=-1e6), "build", "m")
        spent = status.cost_usd([hostile, {"total_cost_usd": 2.0}])
        self.assertEqual(spent, 2.0)
        launch = dict(
            session_id=podcontract.new_session_id(), repository="o/r", run_id="run-1", job_id="j", attempt=0, phase="build",
            resource_class="standard", timeout_seconds=600, manager_instance="a" * 8,
        )
        podpool.Request.for_launch(launch, estimate_usd=1.0, spent_usd=spent, budget_usd=10)
        with self.assertRaises(ValueError):  # what the unfixed value did
            podpool.Request.for_launch(launch, estimate_usd=1.0, spent_usd=-1e6, budget_usd=10)

    def test_run_cost_ignores_hostile_values_and_stays_within_the_bound(self):
        with tempfile.TemporaryDirectory() as temp:
            data = {"jobs": {
                "a": {"usage": [{"total_cost_usd": -1e6}, {"total_cost_usd": 1.5}]},
                "b": {"usage": [{"total_cost_usd": float("nan")}]},
            }}
            fake = SimpleNamespace(directory=Path(temp), state=SimpleNamespace(mutex=threading.RLock(), data=data))
            self.assertEqual(Runner.run_cost(fake), 1.5)
            data["jobs"]["c"] = {"usage": [{"total_cost_usd": 1e12}]}
            data["jobs"]["d"] = {"usage": [{"total_cost_usd": 1e12}]}
            self.assertEqual(Runner.run_cost(fake), podpool.MAX_USD)


class LastResultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = Path(self.temp.name) / "t.jsonl"

    def write(self, *chunks):
        self.log.write_bytes(b"".join(chunks))

    def line(self, **fields):
        return json.dumps(result(**fields)).encode() + b"\n"

    def test_a_line_over_the_limit_is_skipped_and_the_ones_around_it_still_count(self):
        huge = b'{"type":"result","subtype":"success","pad":"' + b"a" * (agent.MAX_LINE_BYTES + 10) + b'"}\n'
        self.write(self.line(n=1), huge, b"garbage\n")
        self.assertEqual(agent.last_result(self.log)["n"], 1)
        self.write(huge, huge)
        self.assertIsNone(agent.last_result(self.log))
        self.write(self.line(n=1), huge, self.line(n=2))
        self.assertEqual(agent.last_result(self.log)["n"], 2)

    def test_an_over_limit_line_without_a_newline_at_the_end_of_the_file_is_skipped(self):
        self.write(self.line(n=1), b'{"type":"result","pad":"' + b"a" * (agent.MAX_LINE_BYTES * 3))
        self.assertEqual(agent.last_result(self.log)["n"], 1)

    def test_a_line_at_the_limit_is_read(self):
        base = len(json.dumps(result(n=3, pad="")))
        text = json.dumps(result(n=3, pad="a" * (agent.MAX_LINE_BYTES - base))).encode()
        self.assertEqual(len(text), agent.MAX_LINE_BYTES)
        self.write(text + b"\n")
        self.assertEqual(agent.last_result(self.log)["n"], 3)
        self.write(text + b"A\n")
        self.assertIsNone(agent.last_result(self.log))

    def test_a_file_without_a_final_newline_crlf_and_invalid_bytes_read_as_before(self):
        self.write(self.line(n=1).replace(b"\n", b"\r\n"), b"\xff\xfe not json\n", self.line(n=2).rstrip(b"\n"))
        self.assertEqual(agent.last_result(self.log)["n"], 2)
        self.write(b"")
        self.assertIsNone(agent.last_result(self.log))

    def test_the_memory_it_holds_is_bounded_by_the_limit(self):
        # Reads never ask for more than the limit plus one byte, whatever the line length.
        sizes = []

        class Spy:
            def __init__(self, handle):
                self.handle = handle

            def readline(self, size=-1):
                sizes.append(size)
                return self.handle.readline(size)

        log = self.log
        log.write_bytes(b"a" * (agent.MAX_LINE_BYTES * 4) + b"\n" + self.line(n=1))
        with open(log, "rb") as handle:
            lines = list(agent._lines(Spy(handle)))
        self.assertEqual(len(lines), 1)
        self.assertTrue(sizes and all(0 < size <= agent.MAX_LINE_BYTES + 1 for size in sizes))


if __name__ == "__main__":
    unittest.main()

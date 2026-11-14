"""The report a pod worker posts with its delta (src/factory/podreport.py): a closed shape that the supervisor validates.

Slice 10b of the pod backend (docs/pod-backend.md). The report is data from a pod, so every test here is about what
`validate` refuses: a key that is not in the closed set, a value that is not a bool, a number or a fixed word, and a
number that is a bool in disguise.
"""

import copy
import json
import unittest

from factory import podreport, relay

GOOD = {
    "version": 1,
    "outcome": "exited",
    "exit_code": 0,
    "footprint": {"peak_rss_mb": 812.4, "cpu_seconds": 63.1, "samples": 41, "limit_mb": 21504},
    "exceeded_mb": None,
    "delta": {"status": "ok", "records": 3, "bytes": 1234},
    "streamed": {"transcript": 100, "stderr": 5},
    "dropped": {"transcript": False, "stderr": False},
}


def changed(path, value):
    document = copy.deepcopy(GOOD)
    target = document
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return document


class Validation(unittest.TestCase):
    def test_a_good_report_is_returned_as_a_new_dict_of_the_same_shape(self):
        result = podreport.validate(GOOD)
        self.assertEqual(result, GOOD)
        self.assertIsNot(result, GOOD)
        self.assertIsNot(result["delta"], GOOD["delta"])

    def test_every_outcome_and_delta_status_is_accepted_and_nothing_else(self):
        for outcome in podreport.OUTCOMES:
            podreport.validate(changed(["outcome"], outcome))
        for status in podreport.DELTA_STATUSES:
            document = changed(["delta"], {"status": status, "records": 0, "bytes": 0})
            podreport.validate(document)
        for bad in ("done", "", None, 1, ["exited"], "EXITED", "gone"):
            with self.subTest(outcome=bad), self.assertRaises(ValueError):
                podreport.validate(changed(["outcome"], bad))
        for bad in ("fine", "", None, 1):
            with self.subTest(status=bad), self.assertRaises(ValueError):
                podreport.validate(changed(["delta", "status"], bad))

    def test_the_key_sets_are_closed_at_every_level_missing_and_extra(self):
        for path in ([], ["delta"], ["streamed"], ["dropped"], ["footprint"]):
            node = GOOD
            for key in path:
                node = node[key]
            for key in node:
                missing = copy.deepcopy(GOOD)
                target = missing
                for step in path:
                    target = target[step]
                del target[key]
                with self.subTest(path=path, missing=key), self.assertRaises(ValueError):
                    podreport.validate(missing)
            extra = copy.deepcopy(GOOD)
            target = extra
            for step in path:
                target = target[step]
            target["extra"] = 1
            with self.subTest(path=path, extra=True), self.assertRaises(ValueError):
                podreport.validate(extra)

    def test_a_bool_is_never_a_number_and_a_number_is_never_a_bool(self):
        for path in (["version"], ["exit_code"], ["exceeded_mb"], ["delta", "records"], ["delta", "bytes"], ["streamed", "stderr"], ["footprint", "samples"], ["footprint", "peak_rss_mb"], ["footprint", "limit_mb"]):
            with self.subTest(path=path), self.assertRaises(ValueError):
                podreport.validate(changed(path, True))
        for path in (["dropped", "transcript"], ["dropped", "stderr"]):
            for bad in (0, 1, "false", None):
                with self.subTest(path=path, bad=bad), self.assertRaises(ValueError):
                    podreport.validate(changed(path, bad))

    def test_numbers_are_bounded_and_text_is_refused(self):
        bad = {
            ("exit_code",): (-129, 256, "0", 1.5, None),
            ("delta", "records"): (-1, 1 << 49, "3", 2.5),
            ("delta", "bytes"): (-1, 1 << 49, "3"),
            ("streamed", "transcript"): (-1, "x", None, 0.5),
            ("exceeded_mb",): (-1, "1", 1.5),
            ("footprint", "peak_rss_mb"): (-1, "812", None, float("nan"), float("inf")),
            ("footprint", "cpu_seconds"): (-0.1, "1", None),
            ("footprint", "samples"): (-1, 1.5, "1"),
            ("footprint", "limit_mb"): (0, -1, "20", 1.5),
            ("version",): (0, 2, "1", None),
        }
        for path, values in bad.items():
            for value in values:
                with self.subTest(path=path, value=repr(value)), self.assertRaises(ValueError):
                    podreport.validate(changed(list(path), value))
        for exit_code in (-128, -9, 0, 3, 255):
            podreport.validate(changed(["exit_code"], exit_code))

    def test_a_footprint_may_be_null_and_a_limit_may_be_null(self):
        podreport.validate(changed(["footprint"], None))
        podreport.validate(changed(["footprint", "limit_mb"], None))

    def test_a_delta_that_is_not_ok_declares_nothing(self):
        for status in ("too_large", "failed"):
            with self.subTest(status=status), self.assertRaises(ValueError):
                podreport.validate(changed(["delta"], {"status": status, "records": 1, "bytes": 0}))
            with self.subTest(status=status), self.assertRaises(ValueError):
                podreport.validate(changed(["delta"], {"status": status, "records": 0, "bytes": 10}))

    def test_anything_that_is_not_an_object_is_refused_and_the_text_never_echoes_the_input(self):
        for bad in (None, [], "report", 1, [GOOD]):
            with self.subTest(bad=repr(bad)), self.assertRaises(ValueError) as caught:
                podreport.validate(bad)
            self.assertEqual(str(caught.exception), "report")
        with self.assertRaises(ValueError) as caught:
            podreport.validate(changed(["outcome"], "POISON-text"))
        self.assertEqual(str(caught.exception), "report")


class Wire(unittest.TestCase):
    def test_dumps_is_compact_ascii_json_that_loads_back_to_the_same_report(self):
        data = podreport.dumps(GOOD)
        self.assertEqual(json.loads(data), GOOD)
        self.assertNotIn(b" ", data)
        self.assertEqual(podreport.loads(data), GOOD)

    def test_loads_refuses_what_is_not_a_report_in_every_way(self):
        for data in (b"", b"[]", b"not json", b"\xff\xfe", b"{}", json.dumps({**GOOD, "extra": 1}).encode(), b"[" * 100000, b"NaN"):
            with self.subTest(data=data[:20]), self.assertRaises(ValueError) as caught:
                podreport.loads(data)
            self.assertEqual(str(caught.exception), "report")
        for data in (None, "text", 5):
            with self.subTest(data=repr(data)), self.assertRaises(ValueError):
                podreport.loads(data)

    def test_loads_refuses_more_than_the_relays_cap(self):
        self.assertEqual(podreport.MAX_BYTES, relay.REPORT_MAX_BYTES)
        with self.assertRaises(ValueError):
            podreport.loads(b" " * (podreport.MAX_BYTES + 1))

    def test_dumps_validates_what_it_is_given(self):
        with self.assertRaises(ValueError):
            podreport.dumps(changed(["outcome"], "nonsense"))

    def test_build_makes_a_validated_report(self):
        report = podreport.build(
            "timeout", -15, None, None, ("ok", 2, 99), {"transcript": 1, "stderr": 2}, {"transcript": False, "stderr": True}
        )
        self.assertEqual(report["outcome"], "timeout")
        self.assertEqual(report["delta"], {"status": "ok", "records": 2, "bytes": 99})
        self.assertEqual(report["dropped"], {"transcript": False, "stderr": True})
        with self.assertRaises(ValueError):
            podreport.build("exited", 0, None, None, ("failed", 1, 1), {"transcript": 0, "stderr": 0}, {"transcript": False, "stderr": False})

    def test_the_worst_case_report_is_far_below_the_relays_cap(self):
        biggest = changed(["streamed"], {"transcript": (1 << 48), "stderr": (1 << 48)})
        biggest["footprint"] = {"peak_rss_mb": float(1 << 48), "cpu_seconds": float(1 << 48), "samples": 1 << 48, "limit_mb": 1 << 48}
        self.assertLess(len(podreport.dumps(biggest)), 1024)


class Module(unittest.TestCase):
    def test_the_module_is_standard_library_only(self):
        import ast
        from pathlib import Path

        source = Path(podreport.__file__).read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.level or (node.module or "").split(".")[0])
        self.assertEqual(imported, {"json"})


if __name__ == "__main__":
    unittest.main()

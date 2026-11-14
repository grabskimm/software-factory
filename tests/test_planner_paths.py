"""planner.normalize_likely_paths: spelling is repaired, anything else is left for the validator to reject."""

import unittest

from factory.planner import normalize_likely_paths


def result_with(*paths):
    return {"jobs": [{"id": "a", "likely_paths": list(paths)}]}


class NormalizeLikelyPathsTests(unittest.TestCase):
    def paths(self, *paths):
        return normalize_likely_paths(result_with(*paths))["jobs"][0]["likely_paths"]

    def test_spelling_only_problems_are_repaired(self):
        self.assertEqual(self.paths("docs/decisions/"), ["docs/decisions"])
        self.assertEqual(self.paths("./src"), ["src"])
        self.assertEqual(self.paths("a//b"), ["a/b"])
        self.assertEqual(self.paths("a/./b"), ["a/b"])
        self.assertEqual(self.paths("./"), ["."])  # the whole repository stays the whole repository

    def test_canonical_paths_and_their_order_are_untouched_and_duplicates_collapse(self):
        self.assertEqual(self.paths("src", "docs/x.md", "tests"), ["src", "docs/x.md", "tests"])
        self.assertEqual(self.paths("src/", "src", "./src"), ["src"])

    def test_anything_else_is_returned_unchanged_so_that_validation_rejects_it(self):
        for bad in ("../x", "/abs", "src/*.py", "a?b", "a[b]", "a\\b", "", "src/../x", "../x/", "/abs/", "src/../x/", "./../x"):
            with self.subTest(path=bad):
                self.assertEqual(self.paths(bad), [bad])

    def test_odd_shapes_do_not_raise(self):
        for odd in ({}, {"jobs": None}, {"jobs": "x"}, {"jobs": [None, 3, {"likely_paths": "src/"}, {"likely_paths": [3, None, ["a"]]}]}, None, []):
            with self.subTest(shape=str(odd)[:30]):
                normalize_likely_paths(odd)  # must not raise: validate_plan reports what is wrong

    def test_the_result_is_changed_in_place_and_returned(self):
        result = result_with("docs/")
        self.assertIs(normalize_likely_paths(result), result)
        self.assertEqual(result["jobs"][0]["likely_paths"], ["docs"])


if __name__ == "__main__":
    unittest.main()

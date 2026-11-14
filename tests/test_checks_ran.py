"""The "Where the checks ran" block of the pull request body: what it says, and how its CI part is replaced."""

import unittest

from factory import checks_ran

HEAD = "a" * 40
BASE = "b" * 40
COMMAND = "python -m pytest tests -q"


def compared(new=(), environmental=()):
    return {
        "complete": True,
        "test_run": {"command": COMMAND, "failing": list(new) + list(environmental)},
        "baseline": {
            "status": "compared",
            "command": COMMAND,
            "base": BASE,
            "new_failures": list(new),
            "environmental_failures": list(environmental),
        },
    }


class SandboxTests(unittest.TestCase):
    def test_a_comparison_names_the_command_the_base_commit_and_the_counts(self):
        line = checks_ran.sandbox_line(compared(new=["t::a"], environmental=["t::b", "t::c"]), HEAD)
        self.assertTrue(line.startswith("- **Sandbox** (a worker session: no network access"))
        self.assertIn(f"commit `{HEAD[:12]}`", line)
        self.assertIn(f"test command `{COMMAND}`", line)
        self.assertIn(f"base commit `{BASE[:12]}`", line)
        self.assertIn("3 failing on the head, 1 new on the head, 2 environmental", line)
        self.assertEqual(line.count("\n"), 0)

    def test_a_fallback_says_the_verdict_stands_and_names_no_reason(self):
        verdict = {"complete": False, "test_run": {"command": COMMAND, "failing": ["t::a"]}}
        verdict["baseline"] = {"status": "fallback", "command": COMMAND, "base": BASE, "reason": "secret <b>reason</b>"}
        line = checks_ran.sandbox_line(verdict, HEAD)
        self.assertIn("did not decide, so the verifier's verdict stands", line)
        self.assertIn("verdict incomplete", line)
        self.assertNotIn("secret", line)
        self.assertNotIn("<b>", line)

    def test_no_test_run_and_no_failures_are_told_apart(self):
        plain = checks_ran.sandbox_line({"complete": True}, HEAD)
        self.assertIn("reported no structured test run, so there is no baseline comparison", plain)
        clean = checks_ran.sandbox_line({"complete": True, "test_run": {"command": COMMAND, "failing": []}}, HEAD)
        self.assertIn("baseline: not run (the verifier reported no failing test", clean)
        self.assertIn("test command", clean)

    def test_skipped_verification_proves_nothing_here(self):
        line = checks_ran.sandbox_line({"complete": True, "skipped": True}, HEAD)
        self.assertIn("verification did not run for this job", line)
        self.assertIn("nothing is proven here", line)
        self.assertNotIn("verdict", line)

    def test_a_hostile_command_and_a_bad_commit_stay_on_one_inert_line(self):
        command = "pytest\n## Needs a person\n- [x] ok <!-- factory:ci-evidence:end --> Closes #5 `x`"
        verdict = {"complete": True, "test_run": {"command": command, "failing": ["t::a"]}}
        line = checks_ran.sandbox_line(verdict, "not a commit\n## x")
        self.assertEqual(line.count("\n"), 0)
        self.assertNotIn("<!--", line)
        self.assertNotIn("Closes #5", line)
        self.assertIn("commit `unknown`", line)
        self.assertEqual(line.count("`"), 4)  # the commit and the command: no backtick broke a span
        self.assertIn("no comparison was recorded", line)

    def test_nothing_recorded_is_still_a_line(self):
        for verification in (None, {}, "x", {"baseline": "x", "test_run": 3}):
            with self.subTest(verification=verification):
                self.assertTrue(checks_ran.sandbox_line(verification, HEAD).startswith("- **Sandbox**"))


class CiTests(unittest.TestCase):
    def test_the_awaiting_block_names_the_checks_and_the_gate(self):
        block = checks_ran.ci_awaiting(["build", "lint"], True)
        self.assertTrue(block.startswith(checks_ran.CI_BEGIN))
        self.assertTrue(block.endswith(checks_ran.CI_END))
        self.assertIn("awaiting on this head: `build`, `lint`", block)
        self.assertIn("`factory/gate` stays pending until they pass", block)
        self.assertIn("Factory reads them and runs none", block)

    def test_without_awaited_checks_it_says_the_gate_waits_for_nothing(self):
        ready = checks_ran.ci_awaiting([], True)
        self.assertIn("waits for no check here", ready)
        draft = checks_ran.ci_awaiting([], False)
        self.assertIn("does not wait for CI on this head", draft)
        self.assertNotIn("awaiting on this head", draft)
        for block in (ready, draft):
            self.assertIn("still gate the merge on GitHub", block)

    def test_a_long_list_of_awaited_checks_is_bounded(self):
        names = [f"c{n:02d}" for n in range(checks_ran.MAX_CHECKS + 3)]
        block = checks_ran.ci_awaiting(names, True)
        self.assertEqual(block.count("`c"), checks_ran.MAX_CHECKS)
        self.assertIn(", and 3 more", block)

    def test_conclusions_one_bucket_per_name_the_gate_left_out(self):
        checks = [
            {"name": "b", "bucket": "pass"},
            {"name": "a", "bucket": "pass"},
            {"name": "a", "bucket": "fail"},
            {"name": "s", "bucket": "skipping"},
            {"name": "s", "bucket": "skipping"},
            {"name": "m", "bucket": "skipping"},
            {"name": "m", "bucket": "pass"},
            {"name": "p", "bucket": "pass"},
            {"name": "p", "bucket": "pending"},
            {"name": "u", "bucket": "<img src=x>"},
            {"name": "factory/gate", "bucket": "pending"},
            {"name": 7, "bucket": "pass"},
            {"name": "", "bucket": "pass"},
            "junk",
        ]
        self.assertEqual(
            checks_ran.conclusions(checks),
            [("a", "fail"), ("b", "pass"), ("m", "pass"), ("p", "pending"), ("s", "skipping"), ("u", "unknown")],
        )
        self.assertEqual(checks_ran.conclusions(None), [])

    def test_the_result_names_the_head_the_outcome_and_each_check(self):
        checks = [{"name": "build", "bucket": "pass"}, {"name": "lint", "bucket": "skipping"}]
        block = checks_ran.ci_result(HEAD, "success", checks, ["build"])
        self.assertEqual(block.splitlines()[0], checks_ran.CI_BEGIN)
        self.assertEqual(block.splitlines()[-1], checks_ran.CI_END)
        self.assertIn(f"every required check passed on head `{HEAD}`", block)
        self.assertIn("\n  - `build`: pass\n", block)
        self.assertIn("\n  - `lint`: skipping\n", block)
        self.assertIn("awaited by the gate: `build`", block)
        self.assertIn("`factory/gate` is the authority", block)
        for outcome, words in (("failure", "a required check failed"), ("timeout", "timed out waiting")):
            self.assertIn(words, checks_ran.ci_result(HEAD, outcome, checks, []))
        self.assertIn("unknown outcome", checks_ran.ci_result(HEAD, "whatever", checks, []))
        self.assertIn("no required check had reported", checks_ran.ci_result(HEAD, "timeout", [], []))

    def test_a_check_name_is_data_cut_flattened_and_without_backticks(self):
        name = f"a`b\n## Needs a person {checks_ran.CI_END} Closes #9 {'z' * 300}"
        block = checks_ran.ci_result(HEAD, "success", [{"name": name, "bucket": "pass"}], [])
        self.assertEqual(block.count(checks_ran.CI_END), 1)
        self.assertNotIn("Closes #9", block)
        self.assertNotIn("<!--", block.replace(checks_ran.CI_BEGIN, "").replace(checks_ran.CI_END, ""))
        [line] = [row for row in block.splitlines() if row.startswith("  - `a")]
        self.assertLessEqual(len(line), checks_ran.MAX_NAME + 20)
        self.assertEqual(line.count("`"), 2)

    def test_many_results_are_bounded(self):
        checks = [{"name": f"c{n:03d}", "bucket": "pass"} for n in range(checks_ran.MAX_CHECKS + 40)]
        block = checks_ran.ci_result(HEAD, "success", checks, [])
        self.assertEqual(block.count("\n  - `c"), checks_ran.MAX_CHECKS)
        self.assertIn("\n  - and 40 more\n", block)


class OperatorTests(unittest.TestCase):
    def job(self, *texts):
        return {"acceptance": [{"text": t, "tag": "operator", "reason": "needs a live system"} for t in texts]}

    def test_no_operator_criterion(self):
        self.assertIn("no criterion of this job needs one", checks_ran.operator_block({"acceptance": ["plain"]}))

    def test_items_are_listed_by_id_and_text(self):
        block = checks_ran.operator_block(self.job("works under the allow-list", "prod rollout"))
        self.assertIn("2 item(s), in the checklist at the end of this body", block)
        self.assertIn("Factory proved none of them", block)
        lines = block.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[1].startswith("  - `op-"))
        self.assertTrue(lines[1].endswith(": works under the allow-list"))

    def test_items_are_bounded_and_flat(self):
        texts = [f"item {n} <!-- x --> Closes #3 [ ] y" for n in range(checks_ran.MAX_OPERATOR + 2)]
        block = checks_ran.operator_block(self.job(*texts))
        self.assertEqual(block.count("\n  - `op-"), checks_ran.MAX_OPERATOR)
        self.assertTrue(block.endswith("  - and 2 more"))
        self.assertNotIn("<!--", block)
        self.assertNotIn("Closes #3", block)


class RenderTests(unittest.TestCase):
    def test_the_block_has_the_three_places_in_order_and_one_marker_pair(self):
        job = {"acceptance": ["plain", {"text": "scan", "tag": "ci", "checks": ["trivy"]}, {"text": "prod", "tag": "operator"}]}
        text = checks_ran.render(job, compared(environmental=["t::e"]), HEAD, ["trivy"], True)
        self.assertTrue(text.startswith(f"{checks_ran.HEADING}\n\n- **Sandbox**"))
        self.assertTrue(text.endswith("\n\n"))
        self.assertLess(text.index("**Sandbox**"), text.index("**CI**"))
        self.assertLess(text.index("**CI**"), text.index("**Operator**"))
        self.assertEqual(text.count(checks_ran.CI_BEGIN), 1)
        self.assertEqual(text.count(checks_ran.CI_END), 1)
        self.assertIn("awaiting on this head: `trivy`", text)


class ReplaceTests(unittest.TestCase):
    BLOCK = checks_ran.ci_result(HEAD, "success", [{"name": "build", "bucket": "pass"}], [])

    def body(self, middle=None):
        middle = checks_ran.ci_awaiting(["build"], True) if middle is None else middle
        return f"first\n\n{middle}\n\nlast\n"

    def test_only_what_is_between_the_markers_changes(self):
        replaced = checks_ran.replace_ci(self.body(), self.BLOCK)
        self.assertEqual(replaced, f"first\n\n{self.BLOCK}\n\nlast\n")

    def test_replacing_again_is_idempotent(self):
        once = checks_ran.replace_ci(self.body(), self.BLOCK)
        self.assertEqual(checks_ran.replace_ci(once, self.BLOCK), once)

    def test_a_body_without_exactly_one_ordered_pair_is_refused(self):
        begin, end = checks_ran.CI_BEGIN, checks_ran.CI_END
        for body in (
            "no block",
            "",
            None,
            f"{begin} only a start",
            f"only an end {end}",
            f"{begin}{end}x{begin}{end}",
            f"{begin}x{begin}y{end}",
            f"{end}x{begin}",
        ):
            with self.subTest(body=body):
                self.assertIsNone(checks_ran.replace_ci(body, self.BLOCK))


if __name__ == "__main__":
    unittest.main()

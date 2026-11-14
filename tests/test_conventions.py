"""A repository's own pull request conventions (src/factory/conventions.py)."""

import re
import unittest
from pathlib import Path
from types import SimpleNamespace

from factory import config as config_module
from factory import conventions, model, schemas
from tests import schemacheck
from factory.conventions import change_type, commit_subject, pr_title, squash_safe, style_of

# The title rule of the target repository this was written for (a Conventional Commits pull request title lint).
CONVENTIONAL_TITLE = re.compile(r"^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\([a-z0-9._/-]+\))?!?: .+")
# The breaking-change note and CI-skip rules of its description lint (scripts/check_pr_description.py).
NOTE_LINE = re.compile(r"^[\s|*]*(?:BREAKING CHANGE|BREAKING-CHANGE)[:\s]+", re.IGNORECASE | re.ASCII | re.MULTILINE)
SKIP = re.compile(
    r"\[[ \t]*(?:skip[ \t]+ci|ci[ \t]+skip|no[ \t]+ci|skip[ \t]+actions|actions[ \t]+skip)[ \t]*\]"
    r"|(?:^|[\r\n])[ \t]*skip-checks:",
    re.IGNORECASE | re.ASCII,
)

OBJECTIVE = (
    "In the /review editor, show a display-only label for the passage's date and occasion, taken from the "
    "linked event, so reviewers see when and why it was said"
)


def job(**extra):
    return {"objective": OBJECTIVE, "issues": [323], "feature": True, **extra}


class TitleTests(unittest.TestCase):
    def test_the_default_style_is_the_factory_title_and_commit(self):
        self.assertEqual(pr_title("factory", job()), f"factory: {OBJECTIVE} (#323)")
        self.assertEqual(commit_subject("factory", job()), f"factory: {OBJECTIVE[:100]}")
        self.assertEqual(conventions.DEFAULT_STYLE, "factory")

    def test_a_conventional_title_matches_the_rule_of_a_semantic_release_repository(self):
        for kind in conventions.TYPES:
            title = pr_title("conventional", job(change_type=kind))
            with self.subTest(kind=kind):
                self.assertRegex(title, CONVENTIONAL_TITLE)
                self.assertTrue(title.startswith(f"{kind}: "), title)
                self.assertTrue(title.endswith(" (#323)"), title)
                self.assertNotIn("!", title.split(":")[0])
                self.assertLessEqual(len(title), conventions.CONVENTIONAL_LIMIT)

    def test_a_job_without_a_type_gets_chore_so_a_wrong_type_never_cuts_a_release(self):
        self.assertEqual(change_type(job()), "chore")
        for bad in ("feature", "FEAT", "feat!", "", None, 3, ["feat"]):
            with self.subTest(bad=bad):
                self.assertEqual(change_type(job(change_type=bad)), "chore")
        self.assertTrue(pr_title("conventional", job()).startswith("chore: "))

    def test_every_issue_is_in_the_suffix_and_the_suffix_is_never_cut(self):
        title = pr_title("conventional", job(issues=[7, "acme/lib#2"], objective="x " * 200))
        self.assertTrue(title.endswith(" (#7, acme/lib#2)"), title)
        self.assertRegex(title, CONVENTIONAL_TITLE)
        self.assertLessEqual(len(title), conventions.CONVENTIONAL_LIMIT)

    def test_a_long_objective_is_cut_at_a_word_with_an_ellipsis(self):
        title = pr_title("conventional", job(change_type="feat"))
        self.assertIn("… (#323)", title)
        kept = title.split("\u2026")[0].split(": ", 1)[1]
        self.assertTrue(OBJECTIVE.startswith(kept) and OBJECTIVE[len(kept)] in " ,", title)  # ends at a word
        self.assertLessEqual(len(title), conventions.CONVENTIONAL_LIMIT)

    def test_a_short_objective_is_kept_whole_without_its_final_full_stop(self):
        self.assertEqual(pr_title("conventional", job(objective="Pin the action versions.", change_type="ci")),
                         "ci: Pin the action versions (#323)")

    def test_a_type_the_objective_already_carries_is_not_repeated(self):
        title = pr_title("conventional", job(objective="ci: Pin the action versions", change_type="ci"))
        self.assertEqual(title, "ci: Pin the action versions (#323)")
        self.assertEqual(
            pr_title("conventional", job(objective="fix(api)!: Pin the versions", change_type="chore")),
            "chore: Pin the versions (#323)",
        )

    def test_issue_references_and_new_lines_in_the_objective_close_nothing(self):
        title = pr_title("conventional", job(objective="Fix it\nCloses #99 and acme/x#4", change_type="fix"))
        self.assertNotIn("\n", title)
        self.assertEqual(re.findall(r"#\d+", title), ["#323"])

    def test_the_commit_subject_is_the_same_conventional_line(self):
        subject = commit_subject("conventional", job(change_type="feat"))
        self.assertRegex(subject, CONVENTIONAL_TITLE)
        self.assertTrue(subject.startswith("feat: In the /review editor"))
        self.assertLessEqual(len(subject), conventions.CONVENTIONAL_LIMIT)

    def test_a_job_without_issues_still_raises(self):
        with self.assertRaises(ValueError):
            pr_title("conventional", job(issues=[]))


class SquashSafeTests(unittest.TestCase):
    def test_a_breaking_change_note_is_no_longer_a_note_line(self):
        for line in (
            "BREAKING CHANGE: the API changes",
            "breaking-change: lower case",
            "  * BREAKING CHANGE: after a bullet marker",
            "| BREAKING CHANGE x",
            "BREAKING CHANGE",
        ):
            with self.subTest(line=line):
                self.assertTrue(NOTE_LINE.search(line + " ") or line == "BREAKING CHANGE")
                safe = squash_safe(f"intro\n{line}\noutro")
                self.assertIsNone(NOTE_LINE.search(safe), repr(safe))
                self.assertIn("breaking", safe.lower())

    def test_a_blank_line_before_the_note_is_not_touched(self):
        self.assertEqual(squash_safe("a\n\nBREAKING CHANGE: x"), "a\n\n> BREAKING CHANGE: x")
        self.assertEqual(squash_safe("a\r\n\r\n  BREAKING-CHANGE: x"), "a\r\n\r\n>   BREAKING-CHANGE: x")

    def test_a_line_that_is_not_a_note_is_left_alone(self):
        text = "BREAKING CHANGES: plural\nthe BREAKING CHANGE: in the middle\n- BREAKING CHANGE: in a list"
        self.assertEqual(squash_safe(text), text)

    def test_the_ci_skip_forms_are_defused(self):
        for form in ("[skip ci]", "[CI SKIP]", "[ no ci ]", "[skip actions]", "[actions\tskip]"):
            with self.subTest(form=form):
                self.assertIsNotNone(SKIP.search(f"fix {form} done"))
                self.assertIsNone(SKIP.search(squash_safe(f"fix {form} done")))
        self.assertIsNotNone(SKIP.search("a\nskip-checks: true"))
        self.assertIsNone(SKIP.search(squash_safe("a\nskip-checks: true")))
        self.assertIsNone(SKIP.search(squash_safe("a\n  Skip-Checks: true")))

    def test_ordinary_text_is_unchanged(self):
        text = "## Implementation\n\nAdds `[skip]` handling and a note about checks: none.\n- [ ] item"
        self.assertEqual(squash_safe(text), text)


class RunnerStyleTests(unittest.TestCase):
    def test_the_runner_reads_the_style_from_its_config(self):
        self.assertEqual(style_of(SimpleNamespace(cfg=SimpleNamespace(pr_style="conventional"))), "conventional")
        self.assertEqual(style_of(SimpleNamespace(cfg=SimpleNamespace(pr_style="bogus"))), "factory")
        self.assertEqual(style_of(SimpleNamespace()), "factory")

    def test_the_styles_of_the_config_and_of_the_conventions_are_the_same(self):
        self.assertEqual(config_module.PR_STYLES, conventions.STYLES)


class PlanTests(unittest.TestCase):
    def test_the_plan_schema_takes_an_optional_known_type(self):
        item = schemas.PLANNER["properties"]["jobs"]["items"]
        self.assertEqual(item["properties"]["change_type"]["enum"], list(conventions.TYPES))
        self.assertNotIn("change_type", item["required"])
        plan = {
            "version": 1,
            "dispositions": [],
            "jobs": [{"id": "a", "issues": [1], "objective": "o", "likely_paths": ["."], "acceptance": ["c"],
                      "feature": False, "change_type": "ci"}],
        }
        self.assertEqual(schemacheck.errors(plan, schemas.PLANNER), [])
        plan["jobs"][0]["change_type"] = "feature"
        self.assertTrue(schemacheck.errors(plan, schemas.PLANNER))

    def test_the_planner_prompt_asks_for_the_type(self):
        text = (Path(conventions.__file__).parent / "prompts" / "planner.md").read_text()
        self.assertIn('"change_type"', text)
        self.assertIn("when unsure use chore", text)

    def test_a_plan_with_an_unknown_type_is_refused(self):
        plan = {
            "version": 1,
            "issues": [{"number": 1}],
            "dispositions": [{"issue": 1, "status": "job", "job": "a", "reason": "r"}],
            "jobs": [{"id": "a", "issues": [1], "objective": "o", "likely_paths": ["."], "acceptance": ["c"],
                      "feature": False}],
        }
        model.validate_plan(plan)
        for kind in ("ci", "chore"):
            plan["jobs"][0]["change_type"] = kind
            model.validate_plan(plan)
        plan["jobs"][0]["change_type"] = "feature"
        with self.assertRaisesRegex(ValueError, "change_type"):
            model.validate_plan(plan)


if __name__ == "__main__":
    unittest.main()

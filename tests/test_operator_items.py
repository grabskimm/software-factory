"""The "Needs a person" checklist: ids, flattening, strict tick reading and rendering."""

import json
import unittest

from factory.model import MAX_REASON, outstanding_criteria
from factory.operator_items import (
    bounded,
    DEFAULT_REASON,
    HEADING,
    MARKER,
    agent_text,
    checklist_for,
    defuse,
    flatten,
    operator_items,
    render_checklist,
    ticked_ids,
    without_operator,
)


def job(*acceptance):
    return {"id": "fix-a", "issues": [1], "acceptance": list(acceptance)}


def operator(text, **extra):
    return {"text": text, "tag": "operator", **extra}


class IdTests(unittest.TestCase):
    def test_ids_come_from_the_text_not_from_the_position_or_the_other_criteria(self):
        first = operator_items(job("plain", operator("prod login works"), operator("paid step")))
        reordered = operator_items(
            job(operator("paid step"), {"text": "other", "tag": "ci", "checks": ["x"]}, operator("prod login works"))
        )
        by_text = {item["text"]: item["id"] for item in first}
        self.assertEqual({item["text"]: item["id"] for item in reordered}, by_text)
        self.assertEqual(len(set(by_text.values())), 2)
        for item_id in by_text.values():
            self.assertRegex(item_id, r"^op-[0-9a-f]{10}$")

    def test_whitespace_does_not_change_an_id_but_the_text_does(self):
        base = operator_items(job(operator("prod login works")))[0]["id"]
        self.assertEqual(operator_items(job(operator("  prod   login\nworks ")))[0]["id"], base)
        self.assertNotEqual(operator_items(job(operator("prod login breaks")))[0]["id"], base)

    def test_a_repeated_text_gets_a_numbered_id(self):
        ids = [item["id"] for item in operator_items(job(operator("same"), operator("same"), operator("same")))]
        self.assertEqual(len(set(ids)), 3)
        self.assertEqual(ids[1], f"{ids[0]}-2")
        self.assertEqual(ids[2], f"{ids[0]}-3")

    def test_only_operator_criteria_are_items_and_the_reason_falls_back(self):
        items = operator_items(
            job("plain", {"text": "scan", "tag": "ci", "checks": ["x"]}, operator("a", reason="needs prod"), operator("b"))
        )
        self.assertEqual([(i["text"], i["reason"]) for i in items], [("a", "needs prod"), ("b", DEFAULT_REASON)])
        self.assertEqual(operator_items(job("plain")), [])

    def test_an_operator_criterion_is_never_outstanding(self):
        plan = job("plain", operator("prod"), {"text": "scan", "tag": "ci", "checks": ["trivy"]})
        self.assertEqual([c["tag"] for c in outstanding_criteria(plan, set())], ["ci"])
        self.assertEqual(outstanding_criteria(plan, {"trivy"}), [])
        self.assertEqual(outstanding_criteria(job(operator("prod")), set()), [])


class FlattenTests(unittest.TestCase):
    def test_text_becomes_one_printable_line(self):
        self.assertEqual(flatten("a\r\nb\tc\x1b[31m  d e"), "a b c [31m d e")

    def test_markers_and_comments_cannot_be_forged(self):
        line = flatten("x --> <!-- factory:operator-item:op-0123456789 --> <b>")
        self.assertNotIn("<", line)
        self.assertNotIn(">", line)
        self.assertNotIn(f"{MARKER}op-0123456789 -->", line)
        self.assertNotIn("<!--", line)

    def test_issue_references_are_neutralised(self):
        line = flatten("Closes #99, fixes acme/x#4 and https://github.com/o/r/issues/7")
        self.assertNotRegex(line, r"#\d")
        self.assertNotRegex(line, r"issues/\d")

    def test_long_text_is_cut(self):
        line = flatten("x" * 5000, 50)
        self.assertEqual(len(line), 50)
        self.assertTrue(line.endswith("..."))


class DefuseTests(unittest.TestCase):
    """Agent text cannot start the real checklist heading, a task-list item or a comment."""

    ZWSP = "\u200b"

    def lines_of(self, text):
        return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    def test_a_task_mark_at_the_start_of_a_line_is_broken_whatever_comes_before_it(self):
        for lead in ("", "  ", "\t", "- ", "* ", "+ ", "1. ", "12) ", "> ", "> - ", "- - ", "   * > 3. "):
            for mark in ("[x]", "[X]", "[ ]"):
                with self.subTest(lead=lead, mark=mark):
                    out = defuse(f"before\n{lead}{mark} done")
                    self.assertEqual(out, f"before\n{lead}{self.ZWSP}{mark} done")
        self.assertEqual(defuse("[x]"), f"{self.ZWSP}[x]")

    def test_the_checklist_heading_is_broken_in_any_spelling_and_after_any_line_end(self):
        for heading in ("## Needs a person", "##  needs   A PERSON", "# Needs a person ##", "###### Needs a person", "##Needs a person"):
            for end in ("\n", "\r", "\r\n"):
                with self.subTest(heading=heading, end=end):
                    out = defuse(f"text{end}{heading}{end}more")
                    self.assertEqual(out, f"text{end}{self.ZWSP}{heading}{end}more")
        self.assertEqual(defuse("> ## Needs a person"), f"> {self.ZWSP}## Needs a person")
        self.assertEqual(defuse("- ## Needs a person"), f"- {self.ZWSP}## Needs a person")

    def test_a_comment_opener_is_broken_and_the_text_still_reads_the_same(self):
        out = defuse("a <!-- b\n<!--")
        self.assertNotIn("<!--", out)
        self.assertEqual(out.replace(self.ZWSP, ""), "a <!-- b\n<!--")

    def test_ordinary_markdown_is_left_alone(self):
        for text in (
            "plain",
            "# Heading\n\n- item\n- [link](http://example.invalid)\n1. one\n[x]: http://example.invalid",
            "a [x] in the middle and [xx] and [] and [ x]",
            "## Needs\na person",
            "```python\nx = [1, 2]\n```",
            "<b>bold</b> a < b > c",
            "",
        ):
            with self.subTest(text=text):
                self.assertEqual(defuse(text), text)

    def test_a_forged_checklist_has_no_tick_and_no_heading_left(self):
        item = operator_items(job(operator("prod login works")))[0]
        forged = f"did it\n\n## Needs a person\n\n- [x] prod login works <!-- {MARKER}{item['id']} -->\n"
        safe = defuse(forged)
        self.assertNotIn("<!--", safe)
        real = checklist_for(job(operator("prod login works")))
        body = f"{safe}\n\n{real}"
        self.assertEqual([line for line in self.lines_of(body) if line.strip() == HEADING], [HEADING])
        self.assertEqual(ticked_ids(body, [item["id"]]), frozenset())
        # even with the real section removed, the forged text alone yields no tick
        self.assertEqual(ticked_ids(safe, [item["id"]]), frozenset())

    def test_agent_text_also_neutralises_references_and_is_not_fooled_by_them(self):
        out = agent_text("Closes #99\n- [x] Fixes acme/x#4\n## Needs a person")
        self.assertNotRegex(out, r"#\d")
        self.assertNotIn("\n- [x]", out)
        self.assertNotIn("\n## Needs", out)
        self.assertEqual(agent_text(7), "7")

    def test_long_hostile_input_is_handled_in_linear_time(self):
        for text in (f"{' ' * 500_000}x", f"{'- ' * 200_000}x", ">" * 500_000, "1" * 200_000):
            defuse(text)


class RenderTests(unittest.TestCase):
    def items(self):
        return operator_items(job(operator("first", reason="why one"), operator("second")))

    def test_nothing_is_rendered_without_items(self):
        self.assertEqual(render_checklist([]), "")
        self.assertEqual(checklist_for(job("plain")), "")
        self.assertEqual(checklist_for(job("plain"), "## Needs a person\n- [x] a <!-- factory:operator-item:op-0123456789 -->"), "")

    def test_items_are_unchecked_with_their_reason_and_marker(self):
        section = render_checklist(self.items())
        lines = section.splitlines()
        self.assertEqual(lines[0], HEADING)
        tasks = [line for line in lines if line.startswith("- [")]
        self.assertEqual(len(tasks), 2)
        self.assertTrue(all(line.startswith("- [ ] ") for line in tasks))
        self.assertIn("first (why: why one)", tasks[0])
        self.assertIn(f"second (why: {DEFAULT_REASON})", tasks[1])
        for task, item in zip(tasks, self.items()):
            self.assertTrue(task.endswith(f" <!-- {MARKER}{item['id']} -->"))
        self.assertNotIn("[x]", section)

    def test_only_a_ticked_id_is_checked(self):
        items = self.items()
        section = render_checklist(items, {items[1]["id"], "op-ffffffffff"})
        tasks = [line for line in section.splitlines() if line.startswith("- [")]
        self.assertTrue(tasks[0].startswith("- [ ] "))
        self.assertTrue(tasks[1].startswith("- [x] "))

    def test_an_operator_only_job_says_that_nothing_was_proven(self):
        only = checklist_for(job(operator("a"), operator("b")))
        mixed = checklist_for(job("plain", operator("a")))
        self.assertIn("proved none of them itself", only)
        self.assertNotIn("proved none", mixed)

    def test_a_reason_made_only_of_invisible_characters_falls_back_to_the_fixed_sentence(self):
        for reason in (
            "\u200b" * 10,
            "\x00",
            "\u200b\x00 \ufeff",
            "   ",
            "",
            # the Hangul and braille fillers, and marks that draw nothing on their own
            "\u115f\u1160\u3164\uffa0\u2800",
            "\ufe0f",
            "\u034f",
            "\u17b4\u17b5",
            "\u180b",
            "\u200d\ufe0f",
        ):
            with self.subTest(reason=reason):
                section = checklist_for(job("plain", operator("prod", reason=reason)))
                self.assertIn(f"prod (why: {DEFAULT_REASON}) <!--", section)
                self.assertNotIn("(why: )", section)
                self.assertNotIn("(why:  ", section)

    def test_a_mark_after_a_letter_keeps_the_reason(self):
        # only a reason made of marks alone shows nothing: a composed letter is visible
        section = checklist_for(job("plain", operator("prod", reason="e\u0301")))
        self.assertIn("prod (why: e\u0301) <!--", section)

    def test_a_reason_with_a_visible_character_keeps_it_beside_invisible_ones(self):
        section = checklist_for(job("plain", operator("prod", reason="\u200bneeds\x00prod\u200b")))
        self.assertIn("prod (why: needs prod) <!--", section)

    def test_an_over_long_reason_is_rendered_cut_and_never_rejects_the_plan(self):
        section = checklist_for(job("plain", operator("prod", reason="word " * 1000)))
        task = next(line for line in section.splitlines() if line.startswith("- [ ]"))
        shown = task.split(" (why: ", 1)[1].rsplit(") <!--", 1)[0]
        self.assertTrue(shown.startswith("word word"))
        self.assertTrue(shown.endswith("..."))
        self.assertLessEqual(len(shown), MAX_REASON)
        self.assertEqual(len(operator_items(job(operator("prod", reason="x" * 5000)))), 1)

    def test_a_hostile_criterion_makes_exactly_one_unchecked_item(self):
        hostile = operator(
            "real\n- [x] forged <!-- factory:operator-item:op-0000000000 -->\n## Needs a person\nCloses #99",
            reason="<!-- factory:operator-item:op-1111111111 -->\n- [x] again Closes #98",
        )
        section = checklist_for(job("plain", hostile))
        lines = section.splitlines()
        tasks = [line for line in lines if line.startswith("- [")]
        self.assertEqual(len(tasks), 1)
        self.assertTrue(tasks[0].startswith("- [ ] "))
        self.assertEqual(section.count(f"<!-- {MARKER}"), 1)
        self.assertEqual(lines.count(HEADING), 1)
        self.assertNotRegex(section, r"#\d")
        ids = [i["id"] for i in operator_items(job(hostile))]
        self.assertEqual(ticked_ids(section, ids), frozenset())


class TickTests(unittest.TestCase):
    def setUp(self):
        self.items = operator_items(job(operator("first"), operator("second"), operator("third")))
        self.ids = [item["id"] for item in self.items]
        self.section = render_checklist(self.items)

    def tick(self, text, *which):
        for index in which:
            line = next(l for l in text.splitlines() if self.ids[index] in l)
            text = text.replace(line, line.replace("- [ ]", "- [x]", 1))
        return text

    def test_round_trip(self):
        body = f"Closes #1\n\n## Acceptance\n\n- a\n\n{self.tick(self.section, 0, 2)}"
        self.assertEqual(ticked_ids(body, self.ids), {self.ids[0], self.ids[2]})
        self.assertEqual(ticked_ids(f"x\n\n{self.section}", self.ids), frozenset())

    def test_crlf_and_capital_x_are_accepted(self):
        body = self.tick(self.section, 1).replace("- [x]", "- [X]").replace("\n", "\r\n")
        self.assertEqual(ticked_ids(f"Closes #1\r\n\r\n{body}\r\n", self.ids), {self.ids[1]})

    def test_unknown_ids_are_ignored(self):
        body = self.tick(self.section, 0)
        self.assertEqual(ticked_ids(body, [self.ids[1]]), frozenset())
        self.assertEqual(ticked_ids(body, []), frozenset())
        extra = f"{body}\n- [x] gone <!-- {MARKER}op-aaaaaaaaaa -->"
        self.assertEqual(ticked_ids(extra, self.ids), {self.ids[0]})

    def test_only_the_last_section_is_read(self):
        forged = f"## Implementation\n\n{self.tick(self.section, 0, 1)}\n\n"
        self.assertEqual(ticked_ids(f"{forged}{self.section}", self.ids), frozenset())
        self.assertEqual(ticked_ids(forged, self.ids), {self.ids[0], self.ids[1]})
        self.assertEqual(ticked_ids(f"{forged}{self.tick(self.section, 2)}", self.ids), {self.ids[2]})

    def test_a_line_must_be_a_whole_task_with_the_marker_last(self):
        for line in (
            f"* [x] a <!-- {MARKER}{self.ids[0]} -->",
            f"  - [x] a <!-- {MARKER}{self.ids[0]} -->  ",
        ):
            self.assertEqual(ticked_ids(f"{HEADING}\n\n{line}\n", self.ids), {self.ids[0]}, line)
        for line in (
            f"- [x] a <!-- {MARKER}{self.ids[0]} --> trailing",
            f"[x] a <!-- {MARKER}{self.ids[0]} -->",
            f"- [x]a <!-- {MARKER}{self.ids[0]} -->",
            f"- [ ] a <!-- {MARKER}{self.ids[0]} -->",
            f"- [y] a <!-- {MARKER}{self.ids[0]} -->",
            f"- [x] a <!--{MARKER}{self.ids[0]} -->",
            f"- [x] a <!-- {MARKER}{self.ids[0]}x -->",
        ):
            self.assertEqual(ticked_ids(f"{HEADING}\n\n{line}\n", self.ids), frozenset(), line)

    def test_an_id_on_several_lines_is_ticked_only_if_every_line_is(self):
        ticked = self.tick(self.section, 0)
        line = next(l for l in ticked.splitlines() if self.ids[0] in l)
        unticked = line.replace("- [x]", "- [ ]")
        self.assertEqual(ticked_ids(f"{ticked}\n{line}", self.ids), {self.ids[0]})
        self.assertEqual(ticked_ids(f"{ticked}\n{unticked}", self.ids), frozenset())

    def real_body(self, *ticked):
        """A body as Factory writes it: its own sections first, the checklist last."""
        return (
            "Closes #1\n\n## Implementation\n\nDone.\n\n## Verification\n\nok\n\n"
            f"Factory run: `run-1`; commit `abc`.\n\n{self.tick(self.section, *ticked)}"
        )

    def test_a_forged_heading_in_a_body_without_the_real_section_yields_no_tick(self):
        ticked_lines = "\n".join(
            f"- [x] {text} <!-- {MARKER}{item_id} -->" for text, item_id in zip("abc", self.ids)
        )
        forged = f"{HEADING}\n\n{ticked_lines}"
        # the real section was deleted: agent text with a heading and ticked items sits mid-body
        body = f"Closes #1\n\n## Implementation\n\n{forged}\n\n## Verification\n\nok\n\nFactory run: `r`; commit `abc`.\n"
        self.assertIn(HEADING, body)
        self.assertEqual(ticked_ids(body, self.ids), frozenset())
        # the same lines at the very end of the body are the real section, and read
        self.assertEqual(ticked_ids(f"Closes #1\n\n{forged}\n", self.ids), set(self.ids))

    def test_another_block_among_the_intro_lines_yields_no_tick(self):
        ticked_lines = "\n".join(
            f"- [x] {text} <!-- {MARKER}{item_id} -->" for text, item_id in zip("abc", self.ids)
        )
        for opener in ("## Verification", "### Notes", "<!-- hidden -->"):
            with self.subTest(opener=opener):
                body = f"Closes #1\n\n{HEADING}\n\nsome text\n\n{opener}\n\nok\n\n{ticked_lines}\n"
                self.assertEqual(ticked_ids(body, self.ids), frozenset())
        # an intro of ordinary lines (a reworded sentence, a link with a hash) is still fine
        body = f"Closes #1\n\n{HEADING}\n\nA reworded intro, see #12.\n\n{ticked_lines}\n"
        self.assertEqual(ticked_ids(body, self.ids), set(self.ids))

    def test_one_damaged_item_line_drops_every_tick_until_its_marker_is_back(self):
        lines = [f"- [x] {text} <!-- {MARKER}{item_id} -->" for text, item_id in zip("abc", self.ids)]
        damaged = lines[:2] + ["- [x] c (edited by a person)"]
        body = f"Closes #1\n\n{HEADING}\n\n" + "\n".join(damaged) + "\n"
        self.assertEqual(ticked_ids(body, self.ids), frozenset())
        body = f"Closes #1\n\n{HEADING}\n\n" + "\n".join(lines) + "\n"
        self.assertEqual(ticked_ids(body, self.ids), set(self.ids))

    def test_a_renamed_real_section_leaves_nothing_to_read_but_a_forged_heading_earlier(self):
        renamed = self.real_body(0).replace(HEADING, "## Reviewer checklist")
        self.assertEqual(ticked_ids(renamed, self.ids), frozenset())
        early = f"Closes #1\n\n{self.tick(self.section, 1)}\n\n{renamed}"
        self.assertEqual(ticked_ids(early, self.ids), frozenset())

    def test_text_after_the_list_loses_every_tick(self):
        body = self.real_body(0, 1)
        self.assertEqual(ticked_ids(body, self.ids), {self.ids[0], self.ids[1]})
        for tail in ("Thanks!", "## Notes\n\nlooks fine", "- [x] a plain task", "> quoted", "<!-- note -->"):
            with self.subTest(tail=tail):
                self.assertEqual(ticked_ids(f"{body}\n\n{tail}", self.ids), frozenset())
        # an item line with an unknown id, blank lines and a trailing newline are not "text"
        extra = f"{body}\n\n- [x] gone <!-- {MARKER}op-aaaaaaaaaa -->\n\n\n"
        self.assertEqual(ticked_ids(extra, self.ids), {self.ids[0], self.ids[1]})

    def test_text_between_the_items_loses_every_tick(self):
        lines = self.tick(self.section, 0).split("\n")
        index = next(i for i, line in enumerate(lines) if self.ids[1] in line)
        lines.insert(index, "an aside")
        self.assertEqual(ticked_ids("\n".join(lines), self.ids), frozenset())

    def test_the_intro_lines_before_the_first_item_do_not_matter(self):
        body = self.real_body(2)
        self.assertEqual(ticked_ids(body.replace("Factory could not prove", "Reworded intro, not proving"), self.ids), {self.ids[2]})
        crlf = body.replace("\n", "\r\n")
        self.assertEqual(ticked_ids(f"{crlf}\r\n", self.ids), {self.ids[2]})
        self.assertEqual(ticked_ids(f"{crlf}\r\nappended", self.ids), frozenset())

    def test_a_body_that_is_not_text_gives_no_tick(self):
        for body in (None, 7, b"x", [], {}):
            self.assertEqual(ticked_ids(body, self.ids), frozenset())


class BoundedPromptTests(unittest.TestCase):
    """A reason is cut or dropped instead of refusing the plan, so a prompt must not carry a raw one."""

    def test_a_job_with_no_reason_to_change_comes_back_as_the_same_object(self):
        plan = job("plain", {"text": "scan", "tag": "ci", "checks": ["x"]}, operator("prod", reason="short"))
        self.assertIs(bounded(plan), plan)

    def test_an_over_long_or_misplaced_reason_does_not_reach_a_prompt(self):
        huge = "X" * 100000
        plan = job(
            {"text": "a", "tag": "sandbox", "reason": huge},
            {"text": "b", "tag": "ci", "checks": ["x"], "reason": huge},
            operator("c", reason=huge),
            operator("d", reason="\u200b"),
        )
        shown = bounded(plan)["acceptance"]
        self.assertEqual(shown[0], {"text": "a", "tag": "sandbox"})
        self.assertEqual(shown[1], {"text": "b", "tag": "ci", "checks": ["x"]})
        self.assertLessEqual(len(shown[2]["reason"]), MAX_REASON)
        self.assertTrue(shown[2]["reason"].endswith("..."))
        self.assertNotIn("reason", shown[3])
        self.assertLess(len(json.dumps(shown)), 2000)
        self.assertEqual(len(plan["acceptance"][0]["reason"]), 100000)  # the plan itself is not rewritten
        # the review and verification prompts get the same bounded form for the criteria they keep
        kept, texts = without_operator(plan)
        self.assertEqual(kept["acceptance"], [{"text": "a", "tag": "sandbox"}, {"text": "b", "tag": "ci", "checks": ["x"]}])
        self.assertEqual(texts, ["c", "d"])


class WithoutOperatorTests(unittest.TestCase):
    def test_a_job_without_operator_criteria_is_returned_as_is(self):
        plan = job("plain", {"text": "scan", "tag": "ci", "checks": ["x"]})
        self.assertIs(without_operator(plan)[0], plan)
        self.assertEqual(without_operator(plan)[1], [])

    def test_operator_criteria_are_removed_and_the_others_keep_their_form(self):
        ci = {"text": "scan", "tag": "ci", "checks": ["x"]}
        plan = job("plain", operator("prod", reason="r"), ci, {"text": "untagged"})
        stripped, texts = without_operator(plan)
        self.assertEqual(stripped["acceptance"], ["plain", ci, {"text": "untagged"}])
        self.assertEqual(texts, ["prod"])
        self.assertEqual(len(plan["acceptance"]), 4)
        self.assertEqual(without_operator(job(operator("a")))[0]["acceptance"], [])


if __name__ == "__main__":
    unittest.main()

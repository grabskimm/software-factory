"""Epic phases, slice 2 of #30: the links between a phase's pull request and its issues, and the nested-epic block.

A phase issue's pull request says ``Closes #child, refs #parent``. Only the last phase may say ``closes`` for the parent, and
only when that is proven live (parent open, not ignored, the approval stands, the phase issue is Factory's valid child of the
CURRENT plan, every earlier phase merged), at publish and at every completion; anything unread says ``refs``. One pure helper
(``epic.first_line``) builds the line everywhere. Whether an issue is a phase is decided at plan time from the issue itself
(origin label or marker, and Factory's own author id); a planner's ``epic`` for a phase is blocked with a fixed reason.
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from factory import append, completion, epic, epic_gate, epic_phases, fragments, issue_labels, planner
from factory.config import RepoConfig
from factory.epic import PhaseLink
from factory.epic_gate import AWAITING_APPROVAL, NEEDS_INFO, PLAN_APPROVED, EpicSettings
from factory.issue_comments import DEFAULT_TRIGGER_LABEL
from factory.model import validate_plan
from factory.phases import AgentOptions
from factory.runner import Runner
from factory.trigger import Actor, ClosingPulls, IssueFacts, PhaseIssue, PullFacts, settings_of
from tests.test_epic import GateReader, disposition
from tests.test_epic_phases import CANARY, MALLORY, REPO, PhaseCase, PhaseReader, phases_of
from tests.test_factory import assignment
from tests.test_issue_comments import FakeGhCase, plan_with

DIGEST = "ab" * 32
OTHER_DIGEST = "cd" * 32


def stamp_for(parent=7, child=202, phase=3, count=3, digest=DIGEST):
    return epic.phase_stamp(parent, child, phase, count, digest)


def job_for(child=202, phase=3, count=3, issues=None, digest=DIGEST, parent=7):
    return {"id": "j", "issues": issues or [child], "epic_phase": stamp_for(parent, child, phase, count, digest)}


class LinkReader(PhaseReader):
    """The phase reader plus the two pull request calls the demotion makes."""

    def __init__(self):
        super().__init__()
        self.bodies = {}  # pull request -> body
        self.body_writes = []

    def open_issues(self, label):
        if label != epic.LABEL_PHASE:
            return super().open_issues(label)
        self.calls.append("open_issues")  # the open phase issues, found by their origin label like a repository lists them
        return [
            IssueFacts(number, tuple(child["labels"]), "1")
            for number, child in sorted(self.children.items())
            if child["state"] == "open" and label in child["labels"]
        ]

    def pull_facts(self, number):
        self.calls.append("pull_facts")
        return PullFacts(self.pulls.get(number, "open"), "c" * 40, self.bodies[number])

    def set_pull_body(self, number, body):
        self.calls.append("set_pull_body")
        self.body_writes.append(number)
        self.bodies[number] = body


# --- the one helper that builds the first line -------------------------------------------------------------------------


class FirstLineTests(unittest.TestCase):
    def test_a_job_without_a_link_gets_the_plain_line(self):
        self.assertEqual(epic.first_line([1, 2], lambda n: n == 1), "Closes #1, refs #2")
        self.assertEqual(epic.first_line([1], lambda n: False), "Refs #1")

    def test_a_phase_issue_references_its_parent_and_only_the_last_phase_may_close_it(self):
        for child_closes, parent_closes, expected in (
            (True, False, "Closes #5, refs #3"),
            (True, True, "Closes #5, closes #3"),
            (False, True, "Refs #5, refs #3"),  # a pull request that is not complete closes neither
            (False, False, "Refs #5, refs #3"),
        ):
            with self.subTest(child=child_closes, parent=parent_closes):
                self.assertEqual(epic.first_line([5], lambda n: child_closes, PhaseLink(3, 5, parent_closes)), expected)

    def test_the_link_applies_only_when_the_jobs_issues_are_exactly_the_phase_issue(self):
        link = PhaseLink(3, 5, True)
        for issues in ([5, 6], [6, 5], [6], [5, 5], []):
            with self.subTest(issues=issues):
                if issues:
                    self.assertEqual(epic.first_line(issues, lambda n: True, link), epic.first_line(issues, lambda n: True))
        self.assertEqual(epic.first_line([5, 6], lambda n: True, link), "Closes #5, closes #6")  # an appended or grouped issue: plain
        self.assertEqual(epic.linked_forms([5, 6], link), ())

    def test_the_forms_a_rewrite_recognises_are_the_three_lines_factory_writes(self):
        self.assertEqual(
            epic.linked_forms([5], PhaseLink(3, 5)), ("Refs #5, refs #3", "Closes #5, refs #3", "Closes #5, closes #3")
        )

    def test_a_stale_last_line_is_demoted_and_nothing_else_is_touched(self):
        link = PhaseLink(3, 5)
        self.assertEqual(epic.demoted_first_line("Closes #5, closes #3", link), "Closes #5, refs #3")
        from factory.github import closing_notice, refresh_notice

        # the notice under a demoted first line follows it: the parent is no longer closed
        before = f"\n\n{closing_notice('Closes #5, closes #3')}\n\nrest"
        after = refresh_notice("Closes #5, refs #3", before)
        self.assertIn("closes these issues: #5.", after)
        self.assertIn("It only mentions #3", after)
        self.assertNotIn("#3, #5", after)
        for other in ("Closes #5, refs #3", "Refs #5, refs #3", "Closes #5, closes #4", "Closes #6, closes #3", "closes #3", ""):
            with self.subTest(other=other):
                self.assertIsNone(epic.demoted_first_line(other, link))


class StampTests(unittest.TestCase):
    def test_a_valid_stamp_is_kept_and_anything_else_is_refused(self):
        good = stamp_for()
        self.assertEqual(epic.check_phase_stamp(good), good)
        bad = {
            "a bool is no number": {**good, "parent": True},
            "a bool child": {**good, "child": True},
            "a string number": {**good, "phase": "3"},
            "phase beyond the plan": {**good, "phase": 4},
            "more phases than allowed": {**good, "of": 9, "phase": 9},
            "a child that is its own parent": {**good, "child": 7},
            "a digest that is no sha256": {**good, "digest": "xyz"},
            "an upper case digest": {**good, "digest": DIGEST.upper()},
            "a missing key": {k: v for k, v in good.items() if k != "of"},
            "an extra key": {**good, "extra": 1},
            "not a mapping": [1, 2, 3],
        }
        for name, value in bad.items():
            with self.subTest(name), self.assertRaises(ValueError):
                epic.check_phase_stamp(value)

    def test_recorded_stamp_reads_a_job_and_never_raises(self):
        self.assertEqual(epic.recorded_stamp(job_for()), stamp_for())
        self.assertIsNone(epic.recorded_stamp({"id": "j", "issues": [1]}))
        self.assertIsNone(epic.recorded_stamp({"epic_phase": {"parent": True}}))
        self.assertIsNone(epic.recorded_stamp(None))


# --- the live proof of ``closes parent`` ----------------------------------------------------------------------------------


class LiveLinkCase(unittest.TestCase):
    def setUp(self):
        self.reader = LinkReader()
        self.settings = EpicSettings(True, "write")
        self.reader.scene(7, PLAN_APPROVED, epic.LABEL_APPROVE)

    def merged_phases(self, count):
        for phase in range(1, count + 1):
            self.reader.add_child(199 + phase, phase)
            self.reader.close_child(199 + phase, merged_by=900 + phase)

    def last_child(self, number=202, **kw):
        self.reader.add_child(number, 3, **kw)

    def job(self, **kw):
        kw.setdefault("digest", self.reader.digest())
        return job_for(**kw)

    def link(self, job, active=True, reader=True):
        return epic_phases.phase_link(job, self.reader if reader else None, self.settings, active)

    def line(self, job, **kw):
        return epic.first_line(job["issues"], lambda n: True, self.link(job, **kw))


class LiveLinkTests(LiveLinkCase):
    def test_the_last_phase_closes_the_parent_when_everything_holds(self):
        self.merged_phases(2)
        self.last_child()
        self.assertEqual(self.line(self.job()), "Closes #202, closes #7")

    def test_a_single_phase_plan_closes_its_parent_with_its_only_phase(self):
        self.reader.scene(7, PLAN_APPROVED, epic.LABEL_APPROVE, phases=phases_of(1))
        self.reader.add_child(200, 1, count=1)
        self.assertEqual(self.line(self.job(child=200, phase=1, count=1)), "Closes #200, closes #7")

    def test_a_phase_that_is_not_the_last_only_references_the_parent_and_reads_nothing(self):
        self.reader.add_child(200, 1)
        self.assertEqual(self.line(self.job(child=200, phase=1)), "Closes #200, refs #7")
        self.assertEqual(self.reader.calls, [])

    def test_an_ignored_parent_is_only_referenced(self):
        self.merged_phases(2)
        self.last_child()
        self.reader.issues[7]["labels"].append("factory-ignore")
        self.assertEqual(self.line(self.job()), "Closes #202, refs #7")

    def test_a_parent_that_carries_the_origin_label_is_only_referenced(self):
        self.merged_phases(2)
        self.last_child()
        self.reader.issues[7]["labels"].append(epic.LABEL_PHASE)  # forged: the pass never proves such a parent, so neither may this line
        self.assertEqual(self.line(self.job()), "Closes #202, refs #7")

    def test_a_parent_whose_label_cannot_be_read_is_only_referenced(self):
        self.merged_phases(2)
        self.last_child()
        self.reader.errors["issue_state"] = OSError("503")
        self.assertEqual(self.line(self.job()), "Closes #202, refs #7")

    def test_a_closed_parent_is_only_referenced(self):
        self.merged_phases(2)
        self.last_child()
        self.reader.issues[7]["state"] = "closed"
        self.assertEqual(self.line(self.job()), "Closes #202, refs #7")

    def test_a_parent_that_lost_a_label_is_only_referenced(self):
        self.merged_phases(2)
        self.last_child()
        for label in (PLAN_APPROVED, epic.LABEL_APPROVE):
            with self.subTest(label):
                self.reader.issues[7]["labels"] = [name for name in (PLAN_APPROVED, epic.LABEL_APPROVE) if name != label]
                self.assertEqual(self.line(self.job()), "Closes #202, refs #7")

    def test_an_approval_that_no_longer_stands_is_only_referenced(self):
        self.merged_phases(2)
        self.last_child()
        self.reader.actors[7] = Actor("mallory", "User", "9", "2027-01-01T00:00:30Z")  # a reader of the repository approved it
        self.assertEqual(self.line(self.job()), "Closes #202, refs #7")

    def test_a_plan_edited_after_the_approval_is_only_referenced(self):
        self.merged_phases(2)
        self.last_child()
        self.reader.actors[7] = Actor("carol", "User", "5", "2027-01-01T00:00:01Z")  # applied before the plan comment's last edit
        self.assertEqual(self.line(self.job()), "Closes #202, refs #7")

    def test_a_digest_that_is_not_the_current_plans_is_only_referenced(self):
        self.merged_phases(2)
        self.last_child()
        self.assertEqual(self.line(self.job(digest=OTHER_DIGEST)), "Closes #202, refs #7")  # the stamp is from another plan

    def test_a_phase_issue_of_another_plan_is_only_referenced(self):
        self.merged_phases(2)
        self.last_child(digest=OTHER_DIGEST)  # its marker is from another plan: not a child of the current one
        self.assertEqual(self.line(self.job()), "Closes #202, refs #7")

    def test_a_plan_that_changed_since_planning_is_only_referenced(self):
        self.merged_phases(2)
        self.last_child()
        job = self.job()
        self.reader.scene(7, PLAN_APPROVED, epic.LABEL_APPROVE, phases=phases_of(3, title="Another"))
        self.assertEqual(self.line(job), "Closes #202, refs #7")

    def test_an_earlier_phase_that_is_not_merged_is_only_referenced(self):
        self.reader.add_child(200, 1)
        self.reader.close_child(200, merged_by=901)
        self.reader.add_child(201, 2)  # phase 2 is still open
        self.last_child()
        self.assertEqual(self.line(self.job()), "Closes #202, refs #7")

    def test_an_earlier_phase_closed_without_a_merged_pull_request_is_only_referenced(self):
        self.merged_phases(1)
        self.reader.add_child(201, 2)
        self.reader.close_child(201)  # closed, no closing pull request
        self.last_child()
        self.assertEqual(self.line(self.job()), "Closes #202, refs #7")

    def test_evidence_that_cannot_be_read_is_only_referenced(self):
        self.merged_phases(2)
        self.last_child()
        self.reader.closing[200] = OSError("graphql 502")
        self.assertEqual(self.line(self.job()), "Closes #202, refs #7")

    def test_a_pull_request_of_another_repository_is_no_merge(self):
        self.merged_phases(2)
        self.reader.closing[200] = ClosingPulls(((901, "elsewhere/other", "MERGED"),), True)
        self.last_child()
        self.assertEqual(self.line(self.job()), "Closes #202, refs #7")

    def test_a_phase_issue_that_is_not_the_valid_child_is_only_referenced(self):
        self.merged_phases(2)
        self.last_child(number=203, user=MALLORY)  # a person's issue with the marker: not Factory's
        self.assertEqual(self.line(self.job(child=203)), "Closes #203, refs #7")
        self.last_child()
        self.assertEqual(self.line(self.job(child=203)), "Closes #203, refs #7")  # the valid child is another number
        self.assertEqual(self.line(self.job()), "Closes #202, closes #7")

    def test_two_children_for_the_last_phase_are_only_referenced(self):
        self.merged_phases(2)
        self.last_child()
        self.last_child(number=203)
        self.assertEqual(self.line(self.job()), "Closes #202, refs #7")

    def test_a_closed_last_phase_issue_is_only_referenced(self):
        self.merged_phases(2)
        self.last_child()
        self.reader.close_child(202)
        self.assertEqual(self.line(self.job()), "Closes #202, refs #7")

    def test_the_kill_switches_and_a_missing_client_are_only_referenced_without_a_github_call(self):
        self.merged_phases(2)
        self.last_child()
        self.reader.calls.clear()
        self.assertEqual(self.line(self.job(), active=False), "Closes #202, refs #7")
        self.assertEqual(self.line(self.job(), reader=False), "Closes #202, refs #7")
        self.assertEqual(self.reader.calls, [])

    def test_an_appended_or_grouped_issue_gives_a_plain_line_and_no_read(self):
        self.merged_phases(2)
        self.last_child()
        for issues in ([202, 300], [300, 202]):
            with self.subTest(issues=issues):
                self.assertIsNone(self.link(self.job(issues=issues)))
                self.assertEqual(epic.first_line(issues, lambda n: True, self.link(self.job(issues=issues))), f"Closes #{issues[0]}, closes #{issues[1]}")
        self.assertEqual(self.reader.calls, [])

    def test_a_job_without_a_stamp_has_no_link_and_no_call(self):
        self.assertIsNone(self.link({"id": "j", "issues": [202]}))
        self.assertEqual(self.reader.calls, [])


# --- the runner's publish and the completion of an incomplete pull request say the same line ---------------------------


def publish_body(job, ready, link):
    runner = Runner.__new__(Runner)
    runner.directory = Path("run")
    runner.options = AgentOptions()
    return runner.pr_body(
        job, {"commit": "a" * 40}, {"summary": "Done", "tests": ["t"]}, {"pass": True, "findings": []}, {"complete": True}, ready, link=link
    )


class PublishAndCompletionTests(unittest.TestCase):
    def job(self, issues=(202,)):
        job = assignment()["jobs"][0]
        return {**job, "issues": list(issues)}

    def test_publish_says_the_link_and_a_pull_request_that_is_not_ready_closes_nothing(self):
        for closes in (False, True):
            link = PhaseLink(7, 202, closes)
            ready = publish_body(self.job(), True, link).splitlines()[0]
            self.assertEqual(ready, "Closes #202, closes #7" if closes else "Closes #202, refs #7")
            self.assertEqual(publish_body(self.job(), False, link).splitlines()[0], "Refs #202, refs #7")

    def test_a_job_that_is_not_a_phase_keeps_its_line(self):
        self.assertEqual(publish_body(self.job((1,)), True, None).splitlines()[0], "Closes #1")

    def test_a_grouped_job_gets_the_plain_line_even_with_a_link(self):
        self.assertEqual(publish_body(self.job((202, 300)), True, PhaseLink(7, 202, True)).splitlines()[0], "Closes #202, closes #300")

    def test_the_completion_writes_exactly_the_line_the_publication_would_have(self):
        for closes in (False, True):
            link = PhaseLink(7, 202, closes)
            incomplete = publish_body(self.job(), False, link)
            wanted = publish_body(self.job(), True, link)
            done = completion.rewritten_body(incomplete, [202], lambda n: True, link)
            self.assertEqual(done.splitlines()[0], wanted.splitlines()[0])
            # Only the first line and the notice under it change (the notice follows the line); the rest is as published.
            self.assertEqual(done.splitlines()[2], wanted.splitlines()[2])  # the notice is the one the publication would have written
            self.assertEqual(done.split("\n", 4)[4], incomplete.split("\n", 4)[4])
            self.assertNotEqual(done.splitlines()[2], incomplete.splitlines()[2])

    def test_a_completion_recognises_both_forms_of_the_parent_and_writes_the_live_one(self):
        body = "Closes #202, closes #7\n\nrest\n"  # a last phase that was published when the proof held
        self.assertEqual(completion.rewritten_body(body, [202], lambda n: True, PhaseLink(7, 202, False)), "Closes #202, refs #7\n\nrest\n")
        self.assertEqual(completion.rewritten_body("Closes #202, refs #7\n\nrest\n", [202], lambda n: True, PhaseLink(7, 202, True)), body)
        self.assertEqual(completion.rewritten_body("Refs #202, refs #7\r\n\nrest", [202], lambda n: True, PhaseLink(7, 202, False)), "Closes #202, refs #7\r\n\nrest")

    def test_a_completion_refuses_a_first_line_that_is_not_the_published_one(self):
        link = PhaseLink(7, 202, True)
        for first in ("Closes #202, closes #8", "Closes #202", "Closes #9, closes #7", "Refs #202, closes #7"):
            with self.subTest(first=first):
                self.assertIsNone(completion.rewritten_body(f"{first}\n\nrest", [202], lambda n: True, link))
        # the plain rules are unchanged for a job that is not a phase
        self.assertIsNone(completion.rewritten_body("Closes #9\n\nrest", [1], lambda n: True))
        self.assertEqual(completion.rewritten_body("Refs #1\n\nrest", [1], lambda n: True), "Closes #1\n\nrest")


class CompletionReader:
    """The calls ``complete_verified`` makes, over one incomplete pull request."""

    def __init__(self, body):
        self.repo = "o/r"
        self.bodies = {10: body}
        self.writes = []

    def repo_facts(self):
        return SimpleNamespace(archived=False, private=False)

    def open_pulls(self, label):
        return {10}

    def pull_facts(self, number):
        return PullFacts("open", "c" * 40, self.bodies[number])

    def label_actor(self, number, label):
        return Actor("alice", "User", "5", "2027-01-01T00:00:10Z")

    def permission(self, login):
        return "write"

    def status_of(self, sha, context):
        return None

    def set_pull_body(self, number, body):
        self.writes.append(number)
        self.bodies[number] = body


class CompleteVerifiedTests(unittest.TestCase):
    def run_completion(self, body, links, job=None):
        reader = CompletionReader(body)
        job = job or job_for()
        states = {"j": {"status": "pr_incomplete", "commit": "c" * 40, "pr": "https://github.com/o/r/pull/10"}}
        labels = SimpleNamespace(ignored_among=lambda numbers, with_unread=False: (set(), set()))
        with patch("factory.completion.post_gate_status", lambda *args, **kwargs: None):
            completion.complete_verified(
                "o/r", Path("."), {"jobs": [job]}, states, reader, settings_of(RepoConfig(repo="o/r")), labels, links=links
            )
        return reader

    def test_the_completion_uses_the_live_link_of_the_job(self):
        seen = []

        def links(job):
            seen.append(job["id"])
            return PhaseLink(7, 202, True)

        reader = self.run_completion("Refs #202, refs #7\n\nrest", links)
        self.assertEqual((seen, reader.bodies[10]), (["j"], "Closes #202, closes #7\n\nrest"))

    def test_a_live_check_that_no_longer_holds_completes_the_child_only(self):
        reader = self.run_completion("Closes #202, closes #7\n\nrest", lambda job: PhaseLink(7, 202, False))
        self.assertEqual(reader.bodies[10], "Closes #202, refs #7\n\nrest")

    def test_without_a_link_the_phase_line_is_not_the_published_one_and_nothing_is_written(self):
        reader = self.run_completion("Refs #202, refs #7\n\nrest", None)
        self.assertEqual(reader.writes, [])


# --- plan time: is this issue a phase? ---------------------------------------------------------------------------------


def captured(number, label=True, body="text"):
    return {"number": number, "title": "T", "body": body, "labels": [{"name": epic.LABEL_PHASE}] if label else [], "comments": []}


class PlanStampsTests(PhaseCase):
    def setUp(self):
        super().setUp()
        self.reader.add_child(200, 1)
        self.marker = epic.phase_marker(7, 1, 3, self.reader.digest())

    def stamps(self, *issues):
        return epic_phases.plan_stamps(self.reader, issues)

    def test_a_phase_issue_of_factory_is_stamped_from_its_marker(self):
        self.assertEqual(self.stamps(captured(200)), {200: epic.phase_stamp(7, 200, 1, 3, self.reader.digest())})

    def test_the_marker_alone_is_enough_and_so_is_the_label_alone(self):
        self.reader.children[200]["labels"] = []  # a person took the label off
        found = self.stamps(captured(200, label=False, body=self.marker))
        self.assertEqual(found, {200: epic.phase_stamp(7, 200, 1, 3, self.reader.digest())})
        self.reader.children[200]["body"] = "no marker any more"  # and edited the marker away, the label is back
        self.reader.children[200]["labels"] = [epic.LABEL_PHASE]
        self.assertEqual(self.stamps(captured(200)), {200: None})  # still a phase (no nesting), but no parent link

    def test_a_forged_label_or_marker_on_a_persons_issue_makes_nothing_a_phase(self):
        self.reader.add_child(300, 1, user=MALLORY)  # carries the label and the marker, written by a person
        self.assertEqual(self.stamps(captured(300), captured(300, label=False, body=self.marker)), {})

    def test_the_author_is_checked_here_too_whatever_the_reader_says_about_the_text(self):
        self.reader.phase_issue = lambda number: PhaseIssue(number, "open", "", False, self.marker, "", (epic.LABEL_PHASE,))
        self.assertEqual(self.stamps(captured(300)), {})

    def test_an_issue_that_is_gone_or_not_an_issue_is_no_phase(self):
        self.assertEqual(self.stamps(captured(999)), {})

    def test_an_issue_that_looks_like_nothing_is_not_even_read(self):
        self.reader.calls.clear()
        self.assertEqual(self.stamps(captured(5, label=False, body="a normal issue")), {})
        self.assertEqual(self.reader.calls, [])

    def test_a_boolean_is_no_issue_number(self):
        self.reader.calls.clear()
        self.assertEqual(self.stamps({**captured(1), "number": True}), {})  # True is 1 in Python, and issue 1 would be read
        self.assertEqual(self.reader.calls, [])

    def test_an_issue_that_cannot_be_read_is_neither_stamped_nor_cleared(self):
        original = self.reader.phase_issue

        def broken(number):
            raise OSError("503")

        self.reader.phase_issue = broken
        self.assertEqual(self.stamps(captured(200)), {200: None})
        self.reader.phase_issue = original

    def test_a_marker_that_names_the_issue_as_its_own_parent_gives_no_link(self):
        self.reader.children[200]["body"] = f"{epic.phase_marker(200, 1, 3, self.reader.digest())}\ntext"
        self.assertEqual(self.stamps(captured(200)), {200: None})

    def test_a_human_planned_phase_issue_still_gets_the_link(self):
        # the trigger label was applied by a person and the plan is made the usual way: the stamp comes from the issue itself
        self.reader.add_child(201, 2)
        stamps = self.stamps(captured(200), captured(201))
        plan = {
            "jobs": [{"id": "p", "issues": [201], "depends_on": []}],
            "dispositions": [{"issue": 201, "status": "job", "job": "p", "reason": "r"}],
        }
        epic.bind_phases(plan, stamps)
        self.assertEqual(plan["jobs"][0]["epic_phase"], epic.phase_stamp(7, 201, 2, 3, self.reader.digest()))
        self.assertEqual(epic_phases.phase_link(plan["jobs"][0], self.reader, self.settings).closes_parent, False)  # phase 2 of 3: refs
        self.assertEqual(epic.first_line([201], lambda n: True, epic_phases.phase_link(plan["jobs"][0], self.reader, self.settings)), "Closes #201, refs #7")


# --- a phase is not an epic -----------------------------------------------------------------------------------------------


class NestedEpicTests(unittest.TestCase):
    def plan(self, reason=CANARY):
        return {
            "jobs": [],
            "dispositions": [disposition(1, reason=reason), disposition(2)],
        }

    def test_an_epic_for_a_phase_is_blocked_with_the_fixed_reason(self):
        plan = epic.downgrade(self.plan(), only_invalid=True, nested={1: None})
        first, second = plan["dispositions"]
        self.assertEqual((first["status"], first["reason"]), ("blocked", epic.NESTED_REASON))
        self.assertNotIn("epic", first)
        self.assertNotIn("#", epic.NESTED_REASON)
        self.assertNotIn(CANARY, json.dumps(plan))  # no model text
        self.assertEqual(second["status"], "epic")  # another issue is untouched

    def test_nested_wins_even_when_the_payload_is_valid_and_epic_mode_works(self):
        for only_invalid in (False, True):
            with self.subTest(only_invalid=only_invalid):
                plan = epic.downgrade(self.plan(), only_invalid=only_invalid, nested={1})
                self.assertEqual(plan["dispositions"][0]["reason"], epic.NESTED_REASON)

    def test_without_nested_the_behaviour_is_what_it_was(self):
        plan = epic.downgrade(self.plan(), only_invalid=True)
        self.assertEqual([item["status"] for item in plan["dispositions"]], ["epic", "epic"])
        plan = epic.downgrade(self.plan())
        self.assertEqual([(item["status"], item["reason"]) for item in plan["dispositions"]][0], ("blocked", CANARY))


def job(name, issues, depends=()):
    return {"id": name, "issues": list(issues), "depends_on": list(depends), "objective": "o", "likely_paths": ["src"], "acceptance": ["a"], "feature": False}


def dispositions_for(jobs):
    return [{"issue": n, "status": "job", "job": item["id"], "reason": "r"} for item in jobs for n in item["issues"]]


class BindTests(unittest.TestCase):
    def test_a_single_issue_job_gets_the_stamp_and_a_planner_written_stamp_is_dropped(self):
        jobs = [job("a", [1]), job("b", [2])]
        jobs[1]["epic_phase"] = stamp_for(child=2)  # whatever the planner wrote is not Factory's
        plan = epic.bind_phases({"jobs": jobs, "dispositions": dispositions_for(jobs)}, {1: stamp_for(child=1, phase=1)})
        self.assertEqual(plan["jobs"][0]["epic_phase"], stamp_for(child=1, phase=1))
        self.assertNotIn("epic_phase", plan["jobs"][1])

    def test_a_phase_without_a_readable_marker_is_planned_without_a_link(self):
        jobs = [job("a", [1])]
        plan = epic.bind_phases({"jobs": jobs, "dispositions": dispositions_for(jobs)}, {1: None})
        self.assertEqual(len(plan["jobs"]), 1)
        self.assertNotIn("epic_phase", plan["jobs"][0])

    def test_a_phase_grouped_with_other_issues_is_blocked_and_so_are_its_dependents(self):
        jobs = [job("a", [1, 2]), job("b", [3], ["a"]), job("c", [4], ["b"]), job("d", [5])]
        plan = epic.bind_phases({"jobs": jobs, "dispositions": dispositions_for(jobs)}, {1: stamp_for(child=1, phase=1)})
        self.assertEqual([item["id"] for item in plan["jobs"]], ["d"])
        reasons = {item["issue"]: (item["status"], item["reason"]) for item in plan["dispositions"]}
        self.assertEqual(reasons[1], ("blocked", epic.GROUPED_REASON))
        self.assertEqual(reasons[2], ("blocked", epic.GROUPED_REASON))
        self.assertEqual(reasons[3], ("blocked", epic.DEPENDENCY_REASON))
        self.assertEqual(reasons[4], ("blocked", epic.DEPENDENCY_REASON))
        self.assertEqual(reasons[5][0], "job")
        for reason in (epic.GROUPED_REASON, epic.DEPENDENCY_REASON):
            self.assertNotIn("#", reason)

    def test_a_doubtful_phase_in_a_group_is_blocked_too(self):
        jobs = [job("a", [1, 2])]
        plan = epic.bind_phases({"jobs": jobs, "dispositions": dispositions_for(jobs)}, {2: None})
        self.assertEqual(plan["jobs"], [])


class PlannedCase(unittest.TestCase):
    """``planner.plan`` end to end with a stub agent: what ``plan.json`` records for phase issues."""

    def planned(self, answer, stamps, numbers=(1, 2, 3)):
        work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        dest = work / "run"
        dest.mkdir()
        label = {"name": DEFAULT_TRIGGER_LABEL}
        listed = [{"number": n, "title": "T", "body": "b", "comments": [], "labels": [label]} for n in numbers]
        (dest / "issues.json").write_text(json.dumps(listed))
        (dest / "open-prs.json").write_text("[]")
        asked = []

        def phases(issues):
            asked.append([issue["number"] for issue in issues])
            return stamps

        with patch("factory.planner.run_agent", lambda *a, **k: (answer, {"phase": "plan"})), patch("factory.planner.repo_guidance", return_value=""):
            planner.plan(work, dest, use_snapshot=True, epic_on=True, phases=phases)
        return json.loads((dest / "plan.json").read_text()), asked

    def test_a_phase_issue_planned_as_an_epic_is_blocked_and_the_others_are_planned(self):
        a = job("a", [2])
        answer = {"version": 1, "jobs": [a], "dispositions": [disposition(1), *dispositions_for([a]), {"issue": 3, "status": "covered", "reason": "r"}]}
        written, asked = self.planned(answer, {1: stamp_for(child=1, phase=1)})
        self.assertEqual(asked, [[1, 2, 3]])
        self.assertEqual({d["issue"]: (d["status"], d["reason"]) for d in written["dispositions"]}[1], ("blocked", epic.NESTED_REASON))
        self.assertEqual([item["id"] for item in written["jobs"]], ["a"])
        self.assertNotIn("epic_phase", written["jobs"][0])

    def test_a_phase_issue_planned_as_a_job_records_its_stamp_and_the_plan_validates(self):
        a, b = job("a", [1]), job("b", [2])
        a["epic_phase"] = stamp_for(child=9)  # a planner that tries to write its own is ignored
        answer = {"version": 1, "jobs": [a, b], "dispositions": [*dispositions_for([a, b]), {"issue": 3, "status": "covered", "reason": "r"}]}
        written, _ = self.planned(answer, {1: stamp_for(child=1, phase=1)})
        self.assertEqual(written["jobs"][0]["epic_phase"], stamp_for(child=1, phase=1))
        self.assertNotIn("epic_phase", written["jobs"][1])
        validate_plan(written)

    def test_a_phase_grouped_with_another_issue_is_not_planned(self):
        g = job("g", [1, 2])
        answer = {"version": 1, "jobs": [g], "dispositions": [*dispositions_for([g]), {"issue": 3, "status": "covered", "reason": "r"}]}
        written, _ = self.planned(answer, {1: stamp_for(child=1, phase=1)})
        self.assertEqual(written["jobs"], [])
        self.assertEqual({d["issue"]: d["reason"] for d in written["dispositions"]}[2], epic.GROUPED_REASON)


class PlanFileCase(unittest.TestCase):
    def test_a_stamp_is_validated_and_must_belong_to_a_job_that_is_exactly_that_issue(self):
        plan = assignment()
        plan["issues"] = [{"number": 202, "title": "T", "body": "b"}]
        plan["jobs"][0]["issues"] = [202]
        plan["dispositions"] = [{"issue": 202, "status": "job", "job": "fix-a", "reason": "r"}]
        plan["jobs"][0]["epic_phase"] = stamp_for()
        validate_plan(plan)
        for name, change in {
            "another child": lambda p: p["jobs"][0].update(epic_phase=stamp_for(child=203)),
            "a bool": lambda p: p["jobs"][0].update(epic_phase={**stamp_for(), "parent": True}),
            "not a mapping": lambda p: p["jobs"][0].update(epic_phase="x"),
        }.items():
            broken = json.loads(json.dumps(plan))
            change(broken)
            with self.subTest(name), self.assertRaises(ValueError):
                validate_plan(broken)

    def test_a_fragment_carries_the_stamp_and_refuses_a_bad_one(self):
        result = {
            "jobs": [{**job("a", [202]), "epic_phase": stamp_for()}],
            "dispositions": [{"issue": 202, "status": "job", "job": "a", "reason": "r"}],
        }
        document = append.build_fragment(result, [{"number": 202, "title": "T", "body": "b"}])
        self.assertEqual(document["jobs"][0]["epic_phase"], stamp_for())
        raw = json.dumps(document).encode()
        self.assertEqual(fragments.parse(raw, 10)[1][0]["epic_phase"], stamp_for())
        document["jobs"][0]["epic_phase"] = stamp_for(child=203)
        with self.assertRaises(fragments.FragmentError):
            fragments.parse(json.dumps(document).encode(), 10)


# --- the blocked comment says why, and nothing else is touched ----------------------------------------------------------


class NestedCommentTests(FakeGhCase):
    def test_a_nested_epic_is_blocked_with_a_comment_and_the_parent_is_untouched(self):
        plan = plan_with({"issue": 1, "status": "epic", "reason": CANARY, "epic": disposition(1)["epic"]})
        epic.downgrade(plan, only_invalid=True, nested={1: None})
        labels, _ = self.labeller(plan)
        labels.sync_plan()
        mine = self.comments(1)
        self.assertEqual(len(mine), 1)
        self.assertIn("one phase of an epic plan", mine[0]["body"])
        self.assertNotIn(CANARY, mine[0]["body"])
        calls = self.calls()
        self.assertTrue(any(issue_labels.LABEL_BLOCKED in " ".join(call) for call in calls))
        self.assertFalse(any(issue_labels.LABEL_NEEDS_INFO in " ".join(call) for call in calls))  # no gate label
        self.assertFalse(any("issues/50" in arg for call in calls for arg in call))  # the parent (#50) is never addressed


class GateRefusalTests(unittest.TestCase):
    def test_a_phase_issue_never_has_a_gate_state(self):
        for gate in (NEEDS_INFO, AWAITING_APPROVAL):
            with self.subTest(gate):
                self.assertEqual(epic_gate.state_of([gate]), gate)
                self.assertIsNone(epic_gate.state_of([gate, epic.LABEL_PHASE]))

    def test_the_gate_pass_writes_nothing_for_a_phase_issue_that_carries_a_gate_label(self):
        reader = GateReader()
        reader.scene(7, AWAITING_APPROVAL, epic.LABEL_APPROVE, epic.LABEL_PHASE, questions=0, actor=Actor("carol", "User", "5", "2027-01-01T00:00:30Z"))
        moves = []
        epic_gate.run_pass(reader, EpicSettings(True, "write"), lambda n, label: moves.append((n, label)), lambda n, e: moves.append((n, e)), {}, 0.0)
        self.assertEqual((moves, reader.writes()), ([], []))

    def test_a_plan_never_gives_an_epic_disposition_of_a_phase_a_gate_label(self):
        item = disposition(1)
        self.assertEqual(issue_labels.disposition_label(item, fresh=True), issue_labels.LABEL_NEEDS_INFO)
        self.assertIsNone(issue_labels.disposition_label(item, fresh=True, phase=True))


class PhasePassTests(PhaseCase):
    def test_a_phase_issue_that_carries_the_approval_labels_is_never_a_parent(self):
        self.reader.scene(7, PLAN_APPROVED, epic.LABEL_APPROVE, epic.LABEL_PHASE)
        self.run_pass()
        self.assert_nothing_written()
        self.assertNotIn("plan_comment", self.reader.calls)


# --- a stale last phase pull request stops closing the parent -----------------------------------------------------------


class DemotionCase(PhaseCase):
    OURS = REPO.casefold()

    def setUp(self):
        super().setUp()
        self.reader = LinkReader()
        self.status = epic_phases.EpicStatus(self.reader, REPO, lambda: 0.0, env={})
        self.reader.scene(7, PLAN_APPROVED, epic.LABEL_APPROVE)
        self.reader.next_issue = 300  # the phase issues added by hand below are 200 to 202
        self.stale = "Closes #202, closes #7\r\n\n## Assignment\n\nbody\n"

    def last_phase_open(self):
        """Phases 1 and 2 merged, phase 3 open with an open pull request that says it closes the parent."""
        for phase in (1, 2):
            self.reader.add_child(199 + phase, phase)
            self.reader.close_child(199 + phase, merged_by=900 + phase)
        self.reader.add_child(202, 3)
        self.reader.closing[202] = ClosingPulls(((500, self.OURS, "OPEN"),), True)
        self.reader.bodies[500] = self.stale
        self.run_pass()
        self.assertEqual(self.reader.body_writes, [])  # a healthy epic: nothing is rewritten


class DemotionTests(DemotionCase):
    def halt(self):
        self.reader.close_child(202, ago=5000.0)
        self.run_pass(epic_phases.REREAD_SECONDS)  # first sighting
        self.run_pass(epic_phases.GRACE_SECONDS)  # second, after the window: halted

    def test_a_halted_epic_takes_closes_back_from_the_open_pull_request_and_keeps_the_rest_of_the_body(self):
        self.last_phase_open()
        self.halt()
        self.assertEqual(self.noted, [(7, f"state=halted code={epic_phases.CODE_CLOSED}")])
        self.assertEqual(self.reader.bodies[500], "Closes #202, refs #7\r\n\n## Assignment\n\nbody\n")
        self.assertIn((7, "pull=500 parent link demoted"), self.said)

    def test_a_changed_plan_digest_demotes_the_pull_request_of_the_old_plan(self):
        self.last_phase_open()
        old = self.reader.digest()
        self.reader.scene(7, PLAN_APPROVED, epic.LABEL_APPROVE, phases=phases_of(3, title="Rewritten"))
        self.assertNotEqual(old, self.reader.digest())
        self.again()
        self.assertEqual(self.reader.bodies[500], "Closes #202, refs #7\r\n\n## Assignment\n\nbody\n")
        self.assertEqual(self.reader.created, [300])  # the new plan starts over with its own phase issue
        self.assertEqual(self.ledger()["digest"], self.reader.digest())

    def test_a_failed_demotion_keeps_the_old_ledger_and_is_tried_again(self):
        self.last_phase_open()
        old = self.reader.digest()
        self.reader.scene(7, PLAN_APPROVED, epic.LABEL_APPROVE, phases=phases_of(3, title="Rewritten"))
        self.reader.closing[202] = OSError("graphql 502")
        self.again()
        self.assertEqual((self.reader.created, self.ledger()["digest"], self.failures[-1]), ([], old, (7, "OSError")))
        self.reader.closing[202] = ClosingPulls(((500, self.OURS, "OPEN"),), True)
        self.again()
        self.assertEqual(self.reader.bodies[500], "Closes #202, refs #7\r\n\n## Assignment\n\nbody\n")
        self.assertEqual(self.ledger()["digest"], self.reader.digest())

    def test_a_deleted_phase_issue_of_the_old_plan_does_not_keep_the_new_plan_from_starting(self):
        self.last_phase_open()
        old = self.reader.digest()
        self.reader.scene(7, PLAN_APPROVED, epic.LABEL_APPROVE, phases=phases_of(3, title="Rewritten"))
        del self.reader.children[202]  # the person deleted the open phase issue, then rewrote the plan
        self.reader.closing[202] = RuntimeError("issue not found")  # what GitHub's answer for a missing issue comes to: the call raises
        asked = self.reader.calls.count("closing_pulls")
        self.again()
        self.assertEqual(self.reader.created, [300])  # the new plan starts over with its own phase 1
        self.assertEqual(self.ledger()["digest"], self.reader.digest())
        self.assertNotEqual(self.ledger()["digest"], old)
        self.assertEqual(self.failures, [])
        self.assertEqual(self.reader.body_writes, [])
        self.assertEqual(self.reader.calls.count("closing_pulls"), asked)  # nothing is asked about an issue that does not exist

    def test_a_phase_issue_of_the_old_plan_that_is_not_factorys_is_not_asked_about(self):
        self.last_phase_open()
        self.reader.scene(7, PLAN_APPROVED, epic.LABEL_APPROVE, phases=phases_of(3, title="Rewritten"))
        self.reader.children[202]["user"] = "someone-else"  # the issue was replaced by one of another author
        self.reader.closing[202] = RuntimeError("must not be asked")
        asked = self.reader.calls.count("closing_pulls")
        self.again()
        self.assertEqual((self.reader.created, self.failures), ([300], []))
        self.assertEqual(self.reader.calls.count("closing_pulls"), asked)

    def test_an_unreadable_phase_issue_of_the_old_plan_keeps_the_old_ledger_and_is_tried_again(self):
        self.last_phase_open()
        old = self.reader.digest()
        self.reader.scene(7, PLAN_APPROVED, epic.LABEL_APPROVE, phases=phases_of(3, title="Rewritten"))
        real = self.reader.phase_issue

        def broken(number):
            raise OSError("graphql 502")

        self.reader.phase_issue = broken
        self.again()
        self.assertEqual((self.reader.created, self.ledger()["digest"], self.failures[-1]), ([], old, (7, "OSError")))
        self.reader.phase_issue = real
        self.again()
        self.assertEqual(self.reader.bodies[500], "Closes #202, refs #7\r\n\n## Assignment\n\nbody\n")  # still demoted, then the new plan starts
        self.assertEqual((self.reader.created, self.ledger()["digest"]), ([300], self.reader.digest()))

    def test_only_the_exact_published_line_of_an_open_pull_request_of_this_repository_is_touched(self):
        self.last_phase_open()
        self.reader.bodies[500] = "Closes #202, closes #7 and more\n\nrest"  # a person edited the line
        self.halt()
        self.assertEqual(self.reader.body_writes, [])
        for other in (
            ClosingPulls(((500, "elsewhere/other", "OPEN"),), True),  # a pull request of another repository
            ClosingPulls(((500, self.OURS, "MERGED"),), True),
            ClosingPulls(((500, self.OURS, "CLOSED"),), True),
            ClosingPulls(((500, self.OURS, "UNKNOWN"),), True),
        ):
            with self.subTest(other=other):
                self.reader.bodies[500] = self.stale
                self.reader.closing[202] = other
                self.run_pass(epic_gate.RESCAN_SECONDS)  # a halted parent is read again hourly
                self.assertEqual(self.reader.body_writes, [])
        self.reader.closing[202] = ClosingPulls(((500, self.OURS, "OPEN"),), True)
        self.reader.pulls[500] = "closed"  # listed as open but closed by now
        self.run_pass(epic_gate.RESCAN_SECONDS)
        self.assertEqual(self.reader.body_writes, [])
        del self.reader.pulls[500]  # the control: the same pass does rewrite an open pull request of this repository
        self.run_pass(epic_gate.RESCAN_SECONDS)
        self.assertEqual(self.reader.body_writes, [500])

    def test_a_healthy_epic_rewrites_nothing_and_asks_nothing_about_pull_requests(self):
        self.last_phase_open()
        self.reader.calls.clear()
        self.again()
        self.assertEqual(self.reader.body_writes, [])
        self.assertNotIn("closing_pulls", self.reader.calls)

    def test_a_deleted_phase_issue_is_not_asked_about_pull_requests(self):
        self.reader.add_child(200, 1)
        self.run_pass()
        del self.reader.children[200]
        self.reader.calls.clear()
        self.again()
        self.assertEqual(self.noted[-1], (7, f"state=halted code={epic_phases.CODE_DELETED}"))
        self.assertNotIn("closing_pulls", self.reader.calls)


class WithdrawalTests(DemotionCase):
    """The parent cannot be proven any more (a kill switch, a withdrawn approval, a plan nobody approved): the pull request that was
    published with ``Closes #parent`` must not keep it, and the pass does not have to settle the parent to take it back."""

    DEMOTED = "Closes #202, refs #7\r\n\n## Assignment\n\nbody\n"

    def parent(self, *labels):
        self.reader.issues[7].update(labels=list(labels), updated="2")

    def assert_demoted(self):
        self.assertEqual(self.reader.bodies[500], self.DEMOTED)
        self.assertEqual(self.reader.body_writes, [500])
        self.assertIn((7, "pull=500 parent link demoted"), self.said)
        self.assertEqual(self.failures, [])
        self.run_pass(epic_gate.RESCAN_SECONDS)
        self.assertEqual(self.reader.body_writes, [500])  # not the published line any more: not touched again

    def test_an_ignored_parent_takes_closes_back(self):
        self.last_phase_open()
        self.parent(PLAN_APPROVED, epic.LABEL_APPROVE, "factory-ignore")
        self.again()
        self.assert_demoted()

    def test_a_parent_that_lost_the_approval_label_takes_closes_back(self):
        self.last_phase_open()
        self.parent(PLAN_APPROVED)
        self.again()
        self.assert_demoted()

    def test_an_approval_that_no_longer_stands_takes_closes_back(self):
        self.last_phase_open()
        self.reader.actors[7] = Actor("carol", "User", "5", "2027-01-01T00:00:01Z")  # applied before the plan's last edit
        self.parent(PLAN_APPROVED, epic.LABEL_APPROVE)
        reading = epic_phases.read_parent(self.reader, IssueFacts(7, (PLAN_APPROVED, epic.LABEL_APPROVE), "2"), self.settings, self.state)
        self.assertEqual(reading.verdict, "off")  # the case is the one the pass cannot settle
        self.again()
        self.assert_demoted()

    def test_a_plan_comment_that_is_gone_takes_closes_back(self):
        self.last_phase_open()
        self.reader.plans[7] = None
        self.parent(PLAN_APPROVED, epic.LABEL_APPROVE)
        self.again()
        self.assert_demoted()

    def test_a_parent_that_lost_the_plan_approved_label_is_never_listed_and_takes_closes_back_through_the_phase_issue(self):
        self.last_phase_open()
        self.parent()
        self.again()
        self.assert_demoted()

    def unsettled(self, *labels, change=None):
        """The same parent twice, as it is and with the origin label forged on it: both must take ``Closes`` back."""
        for forged in (False, True):
            with self.subTest(forged_origin_label=forged):
                self.setUp()
                self.last_phase_open()
                if change is not None:
                    change()
                self.parent(*labels, *((epic.LABEL_PHASE,) if forged else ()))
                self.again()
                self.assert_demoted()

    def approval_before_the_plan_edit(self):
        self.reader.actors[7] = Actor("carol", "User", "5", "2027-01-01T00:00:01Z")  # applied before the plan's last edit

    def lose_the_plan(self):
        self.reader.plans[7] = None

    def test_a_forged_origin_label_does_not_keep_closes_alive_on_an_ignored_parent(self):
        self.unsettled(PLAN_APPROVED, epic.LABEL_APPROVE, "factory-ignore")

    def test_a_forged_origin_label_does_not_keep_closes_alive_on_a_parent_without_the_approval_label(self):
        self.unsettled(PLAN_APPROVED)

    def test_a_forged_origin_label_does_not_keep_closes_alive_when_the_approval_does_not_stand(self):
        self.unsettled(PLAN_APPROVED, epic.LABEL_APPROVE, change=self.approval_before_the_plan_edit)

    def test_a_forged_origin_label_does_not_keep_closes_alive_when_the_plan_comment_is_gone(self):
        self.unsettled(PLAN_APPROVED, epic.LABEL_APPROVE, change=self.lose_the_plan)

    def test_a_parent_with_a_forged_origin_label_is_never_read_or_settled_and_takes_closes_back(self):
        self.last_phase_open()
        self.parent(PLAN_APPROVED, epic.LABEL_APPROVE, epic.LABEL_PHASE)  # approved and not ignored: only the label is wrong
        self.reader.calls.clear()
        self.again()
        self.assert_demoted()
        self.assertNotIn("plan_comment", self.reader.calls)  # the label still makes it no parent: nothing is read ...
        self.assertEqual((self.reader.created, self.reader.comment_writes), ([], ["POST"]))  # ... opened or written (the one POST is the first pass's)

    def test_a_closed_parent_needs_nothing_and_an_approved_one_is_left_alone(self):
        self.last_phase_open()
        self.reader.issues[7].update(labels=[], updated="2", state="closed")
        self.again()
        self.assertEqual(self.reader.body_writes, [])
        self.reader.issues[7].update(labels=[PLAN_APPROVED, epic.LABEL_APPROVE], updated="3", state="open")
        self.again()
        self.assertEqual(self.reader.body_writes, [])  # the control: an approved parent whose approval stands keeps its line

    def test_only_the_published_line_of_an_open_pull_request_of_this_repository_is_touched(self):
        for name, closing, body in (
            ("a line a person edited", ClosingPulls(((500, self.OURS, "OPEN"),), True), "Closes #202, closes #7 and more\n\nrest"),
            ("another repository", ClosingPulls(((500, "elsewhere/other", "OPEN"),), True), self.stale),
            ("a merged pull request", ClosingPulls(((500, self.OURS, "MERGED"),), True), self.stale),
        ):
            with self.subTest(name):
                self.last_phase_open()
                self.reader.closing[202], self.reader.bodies[500] = closing, body
                self.parent(PLAN_APPROVED, epic.LABEL_APPROVE, "factory-ignore")
                self.run_pass(epic_gate.RESCAN_SECONDS)
                self.assertEqual(self.reader.body_writes, [])
                self.setUp()

    def test_a_parent_without_a_ledger_costs_one_read_and_writes_nothing(self):
        self.parent(PLAN_APPROVED, epic.LABEL_APPROVE, "factory-ignore")  # never settled: no status comment exists
        self.reader.calls.clear()
        self.run_pass()
        self.assertEqual([call for call in self.reader.calls if call != "open_issues"], ["epic_status"])
        self.assertEqual((self.reader.body_writes, self.failures), ([], []))

    def test_a_failed_call_is_reported_and_tried_again_after_two_minutes(self):
        self.last_phase_open()
        self.parent(PLAN_APPROVED, epic.LABEL_APPROVE, "factory-ignore")
        self.reader.closing[202] = OSError("graphql 502")
        self.again()
        self.assertEqual((self.reader.body_writes, self.failures), ([], [(7, "OSError")]))
        self.reader.closing[202] = ClosingPulls(((500, self.OURS, "OPEN"),), True)
        self.run_pass(epic_phases.REREAD_SECONDS - 1)
        self.assertEqual(self.reader.body_writes, [])  # not before the two minutes
        self.run_pass(2)
        self.assertEqual(self.reader.body_writes, [500])
        self.assertEqual(self.reader.bodies[500], self.DEMOTED)

    def test_a_failed_call_on_the_phase_issue_of_a_parent_without_the_label_is_tried_again(self):
        self.last_phase_open()
        self.parent()
        self.reader.closing[202] = OSError("graphql 502")
        self.again()
        self.assertEqual((self.reader.body_writes, self.failures), ([], [(202, "OSError")]))
        self.reader.closing[202] = ClosingPulls(((500, self.OURS, "OPEN"),), True)
        self.run_pass(epic_phases.REREAD_SECONDS + 1)
        self.assertEqual(self.reader.bodies[500], self.DEMOTED)

    def test_a_deleted_phase_issue_of_the_ledger_is_not_asked_about_pull_requests(self):
        self.last_phase_open()
        del self.reader.children[202]
        self.parent(PLAN_APPROVED, epic.LABEL_APPROVE, "factory-ignore")
        self.reader.calls.clear()
        self.again()
        self.assertNotIn("closing_pulls", self.reader.calls)
        self.assertEqual((self.reader.body_writes, self.failures), ([], []))

    def test_the_sweep_is_bounded_and_looks_at_a_phase_issue_again_only_when_it_or_the_hour_changes(self):
        self.reader.issues.pop(7)
        for index in range(12):  # twelve epics whose parents carry no approval label at all
            parent = 20 + index
            self.reader.scene(parent)
            self.reader.add_child(300 + index, 1, parent=parent)
        self.reader.calls.clear()
        self.run_pass()
        self.assertEqual(self.reader.calls.count("phase_issue"), epic_phases.MAX_SWEPT_PER_PASS)
        self.run_pass(1)
        self.assertEqual(self.reader.calls.count("phase_issue"), 12)  # the two left, the ten done are not read again
        self.reader.calls.clear()
        self.run_pass(1)
        self.assertEqual(self.reader.calls, ["open_issues", "open_issues"])
        self.reader.calls.clear()
        self.run_pass(epic_gate.RESCAN_SECONDS)
        self.assertNotIn("phase_issue", self.reader.calls)  # the parent a marker names does not change
        self.assertEqual(self.reader.calls.count("issue_state"), epic_phases.MAX_SWEPT_PER_PASS)

    def test_a_label_on_someone_elses_issue_is_one_read_and_nothing_more(self):
        self.reader.issues.pop(7)
        self.reader.add_child(300, 1, user=MALLORY)
        self.reader.calls.clear()
        self.run_pass()
        self.assertEqual([call for call in self.reader.calls if call != "open_issues"], ["phase_issue"])  # not ours: no parent, no pull request asked
        self.run_pass(1)
        self.assertEqual([call for call in self.reader.calls if call != "open_issues"], ["phase_issue"])  # and not read again
        self.assertEqual((self.reader.body_writes, self.failures), ([], []))


class DeletedPhaseIssueTests(DemotionCase):
    """What is NOT covered: the open pull request of a DELETED phase issue keeps ``Closes #<deleted>, closes #<parent>``.

    GitHub cannot list the pull requests of an issue that no longer exists, so the pass does not ask, finds no such pull request
    and rewrites nothing, and merging it closes the parent early. ``docs/operations.md`` lists this under "Not covered" (a person who
    deletes a phase issue looks at its pull request). These tests pin what really happens, so that no comment or document claims
    more: the same three paths that rewrite the line while the phase issue exists leave it alone once the issue is deleted, and
    nothing the pass writes or says is about that pull request.
    """

    DEMOTED = "Closes #202, refs #7\r\n\n## Assignment\n\nbody\n"

    def delete_the_last_phase_issue(self):
        del self.reader.children[202]
        self.reader.closing[202] = RuntimeError("issue not found")  # what GitHub's answer for a missing issue comes to: the call raises

    def halted(self, deleted):
        if deleted:
            self.delete_the_last_phase_issue()
        else:
            self.reader.close_child(202, ago=5000.0)
        self.again()  # a deleted phase issue halts at once, a closed one after the first sighting and the grace window
        self.run_pass(epic_phases.GRACE_SECONDS)
        code = epic_phases.CODE_DELETED if deleted else epic_phases.CODE_CLOSED
        self.assertEqual(self.noted[-1], (7, f"state=halted code={code}"))

    def replaced(self, deleted):
        self.reader.scene(7, PLAN_APPROVED, epic.LABEL_APPROVE, phases=phases_of(3, title="Rewritten"))
        if deleted:
            self.delete_the_last_phase_issue()
        self.again()
        self.assertEqual(self.reader.created, [300])  # the new plan starts over either way

    def ignored(self, deleted):
        self.reader.issues[7].update(labels=[PLAN_APPROVED, epic.LABEL_APPROVE, "factory-ignore"], updated="2")
        if deleted:
            self.delete_the_last_phase_issue()
        self.again()

    def test_the_pull_request_of_a_deleted_phase_issue_is_left_as_it_is_and_nothing_is_said_about_it(self):
        paths = (("a halted epic", self.halted), ("a replaced plan", self.replaced), ("a parent that cannot be proven", self.ignored))
        for name, path in paths:
            for deleted in (False, True):
                with self.subTest(path=name, deleted=deleted):
                    self.setUp()
                    self.last_phase_open()
                    path(deleted)
                    self.assertEqual(self.failures, [])  # asking about the deleted issue would have raised, and would have been reported
                    if not deleted:  # the control: the phase issue is there, so its pull request is found and loses ``closes #7``
                        self.assertEqual((self.reader.body_writes, self.reader.bodies[500]), ([500], self.DEMOTED))
                        continue
                    self.assertEqual((self.reader.body_writes, self.reader.bodies[500]), ([], self.stale))  # the line, CRLF and all
                    self.assertEqual([text for _, text in (*self.said, *self.noted) if "pull=" in text], [])
                    self.assertNotIn("#500", self.body())  # not the status comment either: nothing there claims it was looked at

    def test_the_docs_list_it_as_not_covered_and_tell_a_person_to_look_at_the_pull_request(self):
        root = Path(__file__).resolve().parents[1]
        operations = " ".join((root / "docs/operations.md").read_text().split())
        manager = " ".join((root / "docs/manager.md").read_text().split())
        self.assertIn("**Not covered**", operations)
        covered = operations[operations.index("**Not covered**") :]
        covered = covered[: covered.index("* **What a phase issue is.**")]
        for phrase in ("a phase issue that was **deleted**", "is not found, is not rewritten", "an early close", "must also look at its open pull request"):
            self.assertIn(phrase, covered)
        self.assertIn("Before deleting any phase issue, look at its open pull request", operations)
        self.assertIn("so its open pull request is not found and keeps `closes #<parent>`", manager)
        for body in (operations, manager):
            self.assertNotIn("nothing could close the parent", body)  # the claim this class exists to keep out


class RunnerLinkTests(LiveLinkCase):
    def runner(self, **kw):
        runner = Runner.__new__(Runner)
        runner.cfg = RepoConfig(repo=REPO, **kw)
        runner.slug, runner.repo = REPO, Path(".")
        return runner

    def test_the_runner_proves_the_link_live_with_the_repositorys_own_client(self):
        self.merged_phases(2)
        self.last_child()
        with patch("factory.trigger.GithubReader", return_value=self.reader), patch("factory.shell.get_auth", return_value=None):
            link = self.runner().phase_link(self.job())
        self.assertEqual(link, PhaseLink(7, 202, True))

    def test_the_kill_switches_say_refs_and_make_no_call(self):
        self.merged_phases(2)
        self.last_child()
        self.reader.calls.clear()
        for name, runner in {
            "epic mode off": self.runner(epic_mode=False),
            "label triggers off": self.runner(auto_build=False),
        }.items():
            with self.subTest(name), patch("factory.trigger.GithubReader", return_value=self.reader), patch("factory.shell.get_auth", return_value=None):
                self.assertEqual(runner.phase_link(self.job()), PhaseLink(7, 202, False))
        self.assertEqual(self.reader.calls, [])

    def test_no_client_says_refs_and_an_ordinary_job_makes_no_client_at_all(self):
        with patch("factory.trigger.GithubReader", side_effect=OSError("no token")), patch("factory.shell.get_auth", return_value=None):
            self.assertEqual(self.runner().phase_link(self.job()), PhaseLink(7, 202, False))
        with patch("factory.trigger.GithubReader") as client:
            self.assertIsNone(self.runner().phase_link({"id": "j", "issues": [1]}))
        client.assert_not_called()


if __name__ == "__main__":
    unittest.main()

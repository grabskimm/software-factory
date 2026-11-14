"""Epic phases (src/factory/epic_phases.py, the phase reads of trigger.py, the manager wiring): slice 1 of #30.

After an authorised approval Factory opens the phase issues one at a time and keeps one status comment on the parent. These
cases pin the authority (the approval is re-derived on every pass, the label is never enough), the idempotence (a crash after the
write, a removed label and a second pass never leave two children), the unlock rule (only a merged closing pull request of this
repository), the grace window before a halt, the bounds, and that no text of a person or a model reaches a place it can act.
"""

import dataclasses
import json
import re
import shutil
import subprocess
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from factory import epic, epic_gate, epic_phases, issue_comments, issue_labels, trigger
from factory.epic_gate import PLAN_APPROVED, EpicSettings
from factory.epic_phases import Child, View, decide
from factory.trigger import Actor, ClosingPulls, GithubReader, IssueFacts, PhaseIssue, PlanComment, StatusComment
from tests.test_epic import T0, T1, T2, T3, EpicPassCase, GateReader, GateTriggerReader, StubGh, rows
from tests.test_trigger_loop import LABEL, REPO

NOW = 1_800_000_000.0
BOT = {"type": "Bot", "id": 4242, "login": "factory-app[bot]"}
MALLORY = {"type": "User", "id": 9, "login": "mallory"}
CANARY = "CANARY-6f1c"  # a model-written string that must never appear outside a fenced block


def phases_of(count, title="Phase title"):
    return [
        {
            "title": f"{title} {number}",
            "objective": f"Objective {number}",
            "paths": [f"src/p{number}.py"],
            "acceptance": [f"works {number}"],
            "depends_on": [number - 1] if number > 1 else [],
            "cost": "S",
        }
        for number in range(1, count + 1)
    ]


def stamp(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


class PhaseMixin:
    """The reads and writes the phase pass makes, over the same small in-memory GitHub as the gate (``GateReader``)."""

    repo = REPO
    app_user_id = 4242

    def init_phases(self):
        self.comments = {}  # parent -> [{"id", "body", "user"}]
        self.children = {}  # number -> {"state", "closed_at", "user", "title", "body", "labels"}
        self.closing = {}  # child -> ClosingPulls or an exception
        self.pulls = {}  # pull request -> open | closed | merged
        self.creator = BOT["login"]
        self.child_actors = {}  # (child, label) -> the Actor that applied it
        self.open_pulls = ([], False)  # Factory's open pull requests, and whether the listing was cut off (or an exception)
        self.truncated = False
        self.crash_after_create = False
        self.comment_error = None
        self.created = []
        self.comment_writes = []
        self.next_issue, self.next_comment = 200, 7000

    def scene(self, number, *labels, phases=None, questions=0, plan=True, **kw):
        kw.setdefault("actor", Actor("carol", "User", "5", T2))
        super().scene(number, *labels, plan=False, questions=questions, **kw)
        if not plan:
            return
        parsed = {"issue": number, **epic.validate({"phases": phases or phases_of(3), "questions": ["q"] * questions})}
        self.plans[number] = PlanComment(100, "factory[bot]", T3, T0, questions, parsed, epic.digest(parsed))

    def digest(self, number=7):
        return self.plans[number].digest

    # -- phase issues ---------------------------------------------------------------------

    def add_child(self, number, phase, parent=7, count=3, digest=None, user=BOT, state="open", closed_at=None, body=None):
        marker = epic.phase_marker(parent, phase, count, digest or self.plans.get(parent, self.plans[7]).digest)
        self.children[number] = {
            "state": state, "closed_at": closed_at, "user": user, "title": f"Phase {phase}/{count}: x",
            "body": body or f"{marker}\ntext", "labels": [epic.LABEL_PHASE],
        }

    def close_child(self, number, ago=5.0, merged_by=None):
        self.children[number].update(state="closed", closed_at=NOW - ago)
        if merged_by is not None:
            self.closing[number] = ClosingPulls(((merged_by, REPO.casefold(), "MERGED"),), True)
            self.pulls[merged_by] = "merged"

    def _row(self, number, child):
        ours = child["user"] == BOT
        closed = stamp(child["closed_at"]) if child["closed_at"] else ""
        line = child["body"].split("\n", 1)[0]
        content = epic.content_hash(child["title"], child["body"])
        labels = tuple(child["labels"]) if ours else ()
        return PhaseIssue(number, child["state"], closed, ours, line if ours else "", content if ours else "", labels)

    def phase_issues(self, parent, since="", max_pages=5):
        self.calls.append("phase_issues")
        if not self.creator:
            raise LookupError("unknown identity")
        found = [
            self._row(n, c)
            for n, c in sorted(self.children.items())
            if c["body"].startswith(f"{epic.PHASE_PREFIX}parent={parent} ")
        ]
        return [row for row in found if row.ours], self.truncated

    def phase_issue(self, number, text=False):
        self.calls.append("phase_issue")
        if number not in self.children:
            return None
        row = self._row(number, self.children[number])
        if text and row.ours:
            row = dataclasses.replace(row, text=(self.children[number]["title"], self.children[number]["body"]))
        return row

    def issue_state(self, number):
        if number in self.children:
            self.calls.append("issue_state")
            child = self.children[number]
            return child["state"], tuple(child["labels"])
        return super().issue_state(number)

    def remove_label(self, number, label):
        if number in self.children:  # a phase issue carries no trigger label: nothing to take off
            self.calls.append("remove_label")
            return None
        return super().remove_label(number, label)

    def creator_login(self):
        return self.creator

    def label_actors(self, number, labels):
        self.calls.append("label_actors")
        return {label: self.child_actors.get((number, label)) for label in labels}

    def open_pulls_of(self, max_pages=5):
        self.calls.append("open_pulls_of")
        if isinstance(self.open_pulls, Exception):
            raise self.open_pulls
        return self.open_pulls

    def closing_pulls(self, number):
        self.calls.append("closing_pulls")
        found = self.closing.get(number, ClosingPulls((), True))
        if isinstance(found, Exception):
            raise found
        return found

    def pull_state(self, number):
        self.calls.append("pull_state")
        return self.pulls.get(number, "open")

    def create_issue(self, title, body, labels):
        self.calls.append("create_issue")
        number, self.next_issue = self.next_issue, self.next_issue + 1
        self.children[number] = {"state": "open", "closed_at": None, "user": BOT, "title": title, "body": body, "labels": list(labels)}
        self.created.append(number)
        if self.crash_after_create:
            raise OSError("connection reset after the write")
        return number

    # -- the status comment ------------------------------------------------------------------

    def put_comment(self, parent, body, user=BOT):
        self.next_comment += 1
        self.comments.setdefault(parent, []).append({"id": self.next_comment, "body": body, "user": user, "updated": T0})
        return self.next_comment

    def ours(self, parent=7):
        wanted = issue_comments.marker(parent, "epic-status")
        return [c for c in self.comments.get(parent, []) if c["user"] == BOT and c["body"].startswith(wanted)]

    def epic_status(self, number):
        self.calls.append("epic_status")
        mine = self.ours(number)
        if not mine:
            return None
        item = min(mine, key=lambda c: c["id"])
        ledger = next((line for line in item["body"].splitlines()[:4] if line.startswith(epic_phases.LEDGER_PREFIX)), "")
        return StatusComment(item["id"], item["updated"], ledger, item["body"])

    def call(self, args):
        self.calls.append("comment_call")
        if self.comment_error is not None:
            raise self.comment_error
        path = next(arg for arg in args if arg.startswith("repos/"))
        listing = re.fullmatch(rf"repos/{REPO}/issues/(\d+)/comments", path)
        editing = re.fullmatch(rf"repos/{REPO}/issues/comments/(\d+)", path)
        body = next((arg[5:] for arg in args if arg.startswith("body=")), None)
        if listing and "--paginate" in args:
            jq = args[args.index("--jq") + 1]
            wanted = re.search(r'startswith\("(.*?)"\)', jq)[1]
            mine = [c for c in self.comments.get(int(listing[1]), []) if c["user"] == BOT and c["body"].startswith(wanted)]
            return "\n".join(json.dumps({"id": c["id"], "body": c["body"]}) for c in mine)
        if listing and args[1:3] == ["-X", "POST"]:
            self.comment_writes.append("POST")
            return str(self.put_comment(int(listing[1]), body))
        if editing and args[1:3] == ["-X", "PATCH"]:
            self.comment_writes.append("PATCH")
            target = next((item for items in self.comments.values() for item in items if item["id"] == int(editing[1])), None)
            if target is not None:
                target["body"] = body
                return ""
        raise AssertionError(f"fake gh: unhandled {args}")


class PhaseReader(PhaseMixin, GateReader):
    def __init__(self):
        GateReader.__init__(self)
        self.init_phases()


class PhaseOpsReader(PhaseMixin, GateTriggerReader):
    def __init__(self, repo=REPO):
        GateTriggerReader.__init__(self, repo)
        self.init_phases()


class PhaseCase(unittest.TestCase):
    def setUp(self):
        self.reader = PhaseReader()
        self.status = epic_phases.EpicStatus(self.reader, REPO, lambda: 0.0, env={})
        self.state = epic_phases.PhaseState()
        self.settings = EpicSettings(True, "write")
        self.now = NOW
        self.said, self.noted, self.failures = [], [], []
        self.reader.scene(7, PLAN_APPROVED, epic.LABEL_APPROVE)

    def run_pass(self, advance=0.0, allowed=lambda: True):
        self.now += advance
        self.status.new_cycle()
        return epic_phases.run_pass(
            self.reader, self.settings, self.status, self.state, self.now,
            lambda number, text: self.said.append((number, text)),
            lambda number, text: self.noted.append((number, text)),
            lambda number, error: self.failures.append((number, type(error).__name__)),
            allowed,
        )

    def again(self):
        return self.run_pass(epic_phases.REREAD_SECONDS)

    def ledger(self, parent=7):
        return epic_phases.parse_ledger(self.reader.epic_status(parent).ledger)

    def body(self, parent=7):
        return self.reader.epic_status(parent).body

    def writes(self):
        return [call for call in self.reader.calls if call in ("create_issue", "comment_call", "add_label", "remove_label")]

    def assert_nothing_written(self):
        self.assertEqual((self.reader.created, self.reader.comment_writes, self.writes()), ([], [], []))


# --- creation, idempotence, the status comment ------------------------------------------------------------


class CreationTests(PhaseCase):
    def test_the_first_pass_opens_phase_one_with_the_origin_label_and_a_status_comment(self):
        self.assertEqual(self.run_pass(), 1)
        self.assertEqual(self.reader.created, [200])
        child = self.reader.children[200]
        self.assertEqual(child["labels"], [epic.LABEL_PHASE])  # the origin label only, applied with the issue: never the trigger label
        self.assertEqual(child["title"], "Phase 1/3: Phase title 1")
        self.assertEqual(child["body"].splitlines()[0], epic.phase_marker(7, 1, 3, self.reader.digest()))
        self.assertEqual(self.said, [(7, "phase=1 child=200 created")])
        ledger = self.ledger()
        self.assertEqual(ledger["digest"], self.reader.digest())
        self.assertEqual(ledger["phases"][1]["child"], 200)
        self.assertEqual(ledger["phases"][1]["hash"], epic.content_hash(child["title"], child["body"]))
        self.assertIn("- [ ] Phase 1: #200 open", self.body())
        self.assertIn("- [ ] Phase 2: not opened yet", self.body())
        self.assertEqual(self.failures, [])

    def test_a_second_pass_writes_nothing(self):
        self.run_pass()
        self.reader.calls.clear()
        self.again()
        self.assertEqual(self.writes(), [])
        self.assertEqual((self.reader.created, self.reader.comment_writes), ([200], ["POST"]))  # one child, one comment

    def test_a_crash_after_the_write_leaves_one_child_because_the_next_pass_finds_it_by_search(self):
        self.reader.crash_after_create = True
        self.run_pass()
        self.assertEqual((self.reader.created, self.failures), ([200], [(7, "OSError")]))
        self.assertIsNone(self.reader.epic_status(7))  # the ledger was never written
        self.reader.crash_after_create = False
        self.again()
        self.again()
        self.assertEqual(self.reader.created, [200])  # found, adopted, never a second one
        self.assertEqual(self.ledger()["phases"][1]["child"], 200)

    def test_a_failed_ledger_update_after_the_write_is_healed_by_the_search(self):
        self.reader.comment_error = OSError("403")
        self.run_pass()
        self.assertEqual((self.reader.created, self.failures), ([200], [(7, "StatusFailed")]))
        self.reader.comment_error = None
        self.again()
        self.assertEqual(self.reader.created, [200])
        self.assertEqual(self.ledger()["phases"][1]["child"], 200)

    def test_removing_the_origin_label_makes_no_child_invisible_and_causes_no_second_one(self):
        self.run_pass()
        self.reader.children[200]["labels"] = []
        self.again()
        self.assertEqual(self.reader.created, [200])
        # even with the ledger gone (the status comment deleted) the search finds the child by its author and marker
        self.reader.comments.clear()
        self.again()
        self.assertEqual(self.reader.created, [200])
        self.assertEqual(self.ledger()["phases"][1]["child"], 200)

    def test_one_child_per_parent_per_pass_and_never_more_than_the_plan(self):
        self.reader.scene(7, PLAN_APPROVED, epic.LABEL_APPROVE, phases=phases_of(8))
        for phase in range(1, 9):
            self.again()
            self.assertEqual(len(self.reader.created), phase)  # exactly one more per pass
            child = self.reader.created[-1]
            self.reader.close_child(child, merged_by=900 + phase)
        for _ in range(3):
            self.again()
        self.assertEqual(len(self.reader.created), 8)  # a parent with 8 phases never gets a 9th
        self.assertIn(f"State: `{epic_phases.STATE_DONE}`", self.body())
        self.assertEqual(self.noted, [])

    def test_a_pass_that_finds_phase_one_merged_opens_phase_two_only(self):
        self.run_pass()
        self.reader.close_child(200, merged_by=910)
        self.again()
        self.assertEqual(self.reader.created, [200, 201])
        self.assertIn("- [x] Phase 1: #200, pull request #910 merged", self.body())
        self.assertIn("- [ ] Phase 2: #201 open", self.body())
        self.assertIn("- [ ] Phase 3: not opened yet", self.body())
        self.assertEqual(
            self.ledger()["phases"][1],
            {"child": 200, "hash": self.ledger()["phases"][1]["hash"], "pr": 910, "merged": True, "neg": None, "started": True, "plans": 0, "stall": None},
        )
        self.assertEqual(self.reader.comment_writes, ["POST", "PATCH"])  # the one status comment is edited in place


class AuthorityTests(PhaseCase):
    def test_a_label_applied_by_a_triager_creates_nothing(self):
        self.reader.scene(7, PLAN_APPROVED, epic.LABEL_APPROVE, actor=Actor("bob", "User", "5", T2))  # triage access
        self.run_pass()
        self.assert_nothing_written()

    def test_an_approval_applied_by_a_bot_or_before_the_plans_last_edit_creates_nothing(self):
        for actor in (Actor("app[bot]", "Bot", "5", T2), Actor("carol", "User", "5", T3), None):
            with self.subTest(actor=actor):
                self.state = epic_phases.PhaseState()
                self.reader.scene(7, PLAN_APPROVED, epic.LABEL_APPROVE, actor=actor)
                self.run_pass()
                self.assert_nothing_written()

    def test_an_early_approval_of_a_plan_with_unanswered_questions_creates_nothing(self):
        self.reader.scene(7, PLAN_APPROVED, epic.LABEL_APPROVE, questions=2)  # nobody authorised answered
        self.run_pass()
        self.assert_nothing_written()

    def test_removing_the_approval_label_or_the_state_label_or_ignoring_the_parent_creates_nothing(self):
        for labels in ((PLAN_APPROVED,), (epic.LABEL_APPROVE,), (PLAN_APPROVED, epic.LABEL_APPROVE, "factory-ignore")):
            with self.subTest(labels=labels):
                self.state = epic_phases.PhaseState()
                self.reader.scene(7, *labels)
                self.run_pass()
                self.assert_nothing_written()

    def test_a_plan_that_changed_after_the_children_were_made_starts_a_new_generation(self):
        self.run_pass()
        self.reader.scene(7, PLAN_APPROVED, epic.LABEL_APPROVE, phases=phases_of(2, title="Other"))  # a different plan, a new digest
        self.again()
        self.assertEqual(self.reader.created, [200, 201])  # the old child is old-generation: ignored, a new first phase opens
        self.assertEqual(self.ledger()["digest"], self.reader.digest())
        self.assertEqual(self.ledger()["phases"][1]["child"], 201)

    def test_an_edit_of_the_plan_comment_after_the_approval_creates_no_new_child(self):
        self.run_pass()
        self.reader.close_child(200, merged_by=910)  # phase 2 would be due
        plan = self.reader.plans[7]
        self.reader.plans[7] = dataclasses.replace(plan, updated="2027-01-01T00:01:00Z")  # edited after the approval (T2)
        before = self.body()
        self.again()
        self.assertEqual((self.reader.created, self.body(), self.reader.comment_writes), ([200], before, ["POST"]))

    def test_the_pass_is_off_when_the_repository_refuses(self):
        self.assertEqual(self.run_pass(allowed=lambda: False), 0)
        self.assert_nothing_written()

    def test_a_parent_without_a_valid_plan_comment_is_dropped_cheaply_and_not_read_again(self):
        for plan in (False, True):
            self.reader.scene(8, PLAN_APPROVED, epic.LABEL_APPROVE, plan=False)
            if plan:  # a plan comment whose marker could not be read: no plan
                self.reader.plans[8] = PlanComment(100, "factory[bot]", T3, T0, None)
            self.state = epic_phases.PhaseState()
            self.reader.calls.clear()
            self.reader.issues.pop(7, None)
            self.run_pass()
            # the plan comment, then the ledger (a withdrawn plan must not keep ``Closes``): reads only, nothing is written
            self.assertEqual([c for c in self.reader.calls if c not in ("open_issues", "plan_comment")], ["epic_status"])
            self.reader.calls.clear()
            self.again()
            self.assertEqual(self.reader.calls, ["open_issues", "open_issues"])  # the parents and the phase issues listed, found unchanged, skipped
        self.assert_nothing_written()


# --- the children: forged, duplicated, deleted, unreadable ------------------------------------------------------


class DiscoveryTests(PhaseCase):
    def test_a_forged_phase_issue_by_another_author_neither_counts_nor_unlocks_a_phase(self):
        self.reader.add_child(300, 1, user=MALLORY, state="closed", closed_at=NOW - 5000)
        self.reader.closing[300] = ClosingPulls(((901, REPO.casefold(), "MERGED"),), True)
        self.reader.pulls[901] = "merged"
        self.run_pass()
        self.assertEqual(self.reader.created, [200])  # Factory still opens its own phase 1: the forgery unlocked nothing
        self.assertEqual(self.ledger()["phases"][1]["child"], 200)

    def test_a_marker_for_another_parent_or_plan_or_size_is_not_a_child(self):
        self.reader.add_child(300, 1, parent=8)
        self.reader.add_child(301, 1, digest="0" * 64)
        self.reader.add_child(302, 1, count=5)
        self.run_pass()
        self.assertEqual(self.reader.created, [200])

    def test_valid_children_ignores_a_row_that_is_not_ours_whatever_its_first_line_says(self):
        line = epic.phase_marker(7, 1, 3, "a" * 64)
        mine = PhaseIssue(1, "open", "", True, line, "h")
        theirs = PhaseIssue(2, "open", "", False, line, "h")
        found = epic_phases._valid_children([mine, theirs], 7, 3, "a" * 64)
        self.assertEqual(found, {1: {1: mine}})

    def test_two_valid_children_for_one_phase_stop_and_say_and_create_nothing(self):
        self.reader.add_child(300, 1)
        self.reader.add_child(301, 1)
        self.run_pass()
        self.assertEqual(self.reader.created, [])
        self.assertIn("more than one issue claims it (#300, #301)", self.body())
        self.assertIn(f"Code: `{epic_phases.CODE_DUPLICATE}`", self.body())
        self.assertEqual(self.noted, [(7, f"state=stopped code={epic_phases.CODE_DUPLICATE}")])

    def test_a_deleted_child_halts_the_epic_and_is_never_created_again(self):
        self.run_pass()
        del self.reader.children[200]
        self.again()
        self.again()
        self.assertEqual(self.reader.created, [200])
        self.assertIn("- [ ] Phase 1: #200 no longer exists", self.body())
        self.assertIn(f"Code: `{epic_phases.CODE_DELETED}`", self.body())
        self.assertEqual(self.noted[0], (7, f"state=halted code={epic_phases.CODE_DELETED}"))
        self.assertEqual(self.ledger()["phases"][1]["child"], 200)  # the record is kept

    def test_a_truncated_search_creates_nothing_and_says_so_in_the_status_comment_with_a_fixed_code(self):
        self.reader.truncated = True
        self.run_pass()
        self.assertEqual((self.reader.created, self.reader.comment_writes), ([], ["POST"]))  # the status comment, never a phase issue
        self.assertNotIn("create_issue", self.reader.calls)
        self.assertEqual(self.failures, [(7, "Truncated")])
        self.assertIn(f"State: `{epic_phases.STATE_UNKNOWN}` · Code: `{epic_phases.CODE_TRUNCATED}`", self.body())
        self.assertIn("- [ ] Phase 1: not opened yet", self.body())
        self.assertIn(epic_phases._NEXT[epic_phases.CODE_TRUNCATED].format(label=LABEL), self.body())
        self.assertEqual((self.ledger()["phases"], self.noted), ({}, []))
        self.run_pass(epic_gate.RESCAN_SECONDS)  # a read that stopped at a bound is repeated hourly, or when the parent changes
        self.assertEqual((self.reader.created, self.reader.comment_writes), ([], ["POST"]))  # the same words: nothing is written again
        self.assertEqual(self.failures, [(7, "Truncated"), (7, "Truncated")])
        self.reader.truncated = False  # a complete search opens the phase and edits the same comment
        self.run_pass(epic_gate.RESCAN_SECONDS)
        self.assertEqual((self.reader.created, self.reader.comment_writes), ([200], ["POST", "PATCH"]))
        self.assertIn(f"Code: `{epic_phases.CODE_OPEN}`", self.body())

    def test_a_truncated_search_after_a_merged_phase_keeps_the_ledger_and_opens_nothing(self):
        self.run_pass()
        self.reader.close_child(200, merged_by=910)
        self.reader.truncated = True
        self.again()
        self.assertEqual(self.reader.created, [200])
        self.assertIn("- [x] Phase 1: #200, pull request #910 merged", self.body())
        self.assertIn("- [ ] Phase 2: not opened yet", self.body())
        self.assertIn(f"Code: `{epic_phases.CODE_TRUNCATED}`", self.body())
        self.assertEqual((self.ledger()["phases"][1]["merged"], self.ledger()["phases"][1]["pr"]), (True, 910))  # monotone

    def test_an_unknown_identity_creates_nothing_and_writes_nothing(self):
        self.reader.creator = ""
        self.run_pass()
        self.assert_nothing_written()
        self.assertEqual(self.failures, [(7, "Truncated")])
        self.run_pass(epic_gate.RESCAN_SECONDS)
        self.assert_nothing_written()
        self.assertEqual(self.failures, [(7, "Truncated"), (7, "Truncated")])

    def test_the_search_runs_from_the_plans_last_edit_so_a_stale_status_write_cannot_make_a_second_child(self):
        """The status comment's time is not a safe floor for the search.

        A second manager's older view, or a person's edit, writes a ledger that lacks a child that exists, and leaves the comment
        newer than that child: a search that started at the comment's time would miss it and open a second one.
        """
        sinces, original = [], self.reader.phase_issues

        def recording(parent, since="", max_pages=5):
            sinces.append(since)
            return original(parent, since, max_pages)

        self.reader.phase_issues = recording
        self.run_pass()
        stale = epic_phases.render_status(7, self.reader.digest(), decide(3, {}, {}, True, NOW), LABEL)  # phase 1 "not opened yet"
        self.reader.ours()[0].update(body=stale, updated="2027-06-01T00:00:00Z")
        self.again()
        self.assertEqual(self.reader.created, [200])  # found by the search and adopted, never a second one
        self.assertEqual(self.ledger()["phases"][1]["child"], 200)
        self.assertEqual(set(sinces), {self.reader.plans[7].updated})

    def test_a_search_that_finds_a_child_right_before_the_write_stops_the_write(self):
        original = self.reader.phase_issues
        calls = []

        def racing(parent, since="", max_pages=5):
            calls.append(parent)
            if len(calls) == 2:  # the second search is the one right before the write: another manager just wrote
                self.reader.add_child(300, 1)
            return original(parent, since, max_pages)

        self.reader.phase_issues = racing
        self.run_pass()
        self.assertEqual(self.reader.created, [])
        self.reader.phase_issues = original
        self.again()
        self.assertEqual(self.reader.created, [])  # adopted next pass: the one that was found
        self.assertEqual(self.ledger()["phases"][1]["child"], 300)

    def test_a_forged_status_comment_is_ignored_and_a_ledger_claim_of_a_merge_is_checked_again(self):
        forged = epic_phases.encode_ledger(7, self.reader.digest(), {1: {"child": 400, "hash": "", "pr": 999, "merged": True, "neg": None}})
        self.reader.put_comment(7, f"{issue_comments.marker(7, 'epic-status')}\n{forged}\nforged", user=MALLORY)
        self.run_pass()
        self.assertEqual(self.reader.created, [200])  # someone else's comment is not the ledger: phase 1 opens, nothing is unlocked
        # Factory's own ledger that claims a merge that is not true falls back to the evidence
        self.reader.comments.clear()
        claim = epic_phases.encode_ledger(7, self.reader.digest(), {1: {"child": 200, "hash": "", "pr": 998, "merged": True, "neg": None}})
        self.reader.put_comment(7, f"{issue_comments.marker(7, 'epic-status')}\n{claim}\nclaim")
        self.reader.pulls[998] = "open"
        self.again()
        self.assertEqual(self.reader.created, [200])
        self.assertIn("- [ ] Phase 1: #200 open", self.body())
        self.reader.pulls[998] = "merged"  # now it is true
        self.state.verified.clear()
        self.reader.comments.clear()
        self.reader.put_comment(7, f"{issue_comments.marker(7, 'epic-status')}\n{claim}\nclaim")
        self.again()
        self.assertEqual(self.reader.created, [200, 201])


# --- the unlock rule and the grace window ----------------------------------------------------------------------


class UnlockTests(PhaseCase):
    def closed_with(self, *pulls, complete=True, states=None):
        self.run_pass()
        self.reader.close_child(200)
        self.reader.closing[200] = ClosingPulls(tuple(pulls), complete)
        for number, state in (states or {}).items():
            self.reader.pulls[number] = state
        self.again()

    def test_only_a_merged_closing_pull_request_of_this_repository_unlocks_the_next_phase(self):
        cases = {
            "open": ((901, REPO.casefold(), "OPEN"),),
            "closed unmerged": ((901, REPO.casefold(), "CLOSED"),),
            "unknown state": ((901, REPO.casefold(), "UNKNOWN"),),
            "another repository": ((901, "org/other", "MERGED"),),
        }
        for name, pulls in cases.items():
            with self.subTest(name):
                self.setUp()
                self.closed_with(*pulls, states={901: "merged"})
                self.assertEqual(self.reader.created, [200], name)

    def test_a_truncated_read_or_a_failed_read_never_unlocks_and_never_halts(self):
        self.closed_with((901, REPO.casefold(), "OPEN"), complete=False)
        self.assertEqual(self.reader.created, [200])
        for advance in (50.0, epic_phases.GRACE_SECONDS * 3, epic_phases.GRACE_SECONDS * 3):
            self.run_pass(advance + epic_phases.REREAD_SECONDS)
        self.assertEqual(self.noted, [])  # unknown is never a halt
        self.assertIn(f"Code: `{epic_phases.CODE_CHECKING}`", self.body())
        self.reader.closing[200] = RuntimeError(CANARY)
        before = self.body()
        self.again()
        self.assertEqual((self.failures[-1], self.body(), self.reader.created), ((7, "RuntimeError"), before, [200]))

    def test_the_graphql_merged_state_must_be_confirmed_by_the_pull_request_itself(self):
        self.closed_with((901, REPO.casefold(), "MERGED"), states={901: "closed"})
        self.assertEqual(self.reader.created, [200])

    def test_a_merged_closing_pull_request_unlocks_it(self):
        self.closed_with((901, REPO.casefold(), "MERGED"), states={901: "merged"})
        self.assertEqual(self.reader.created, [200, 201])

    def test_an_open_child_is_not_merged_whatever_its_pull_request_says(self):
        self.run_pass()
        self.reader.closing[200] = ClosingPulls(((901, REPO.casefold(), "MERGED"),), True)
        self.reader.pulls[901] = "merged"
        self.again()
        self.assertEqual(self.reader.created, [200])

    def test_a_closed_child_without_a_merged_pull_request_is_a_halt_only_after_the_grace_window_and_two_reads(self):
        self.run_pass()
        self.reader.close_child(200, ago=5.0)
        self.again()  # first sighting: remembered in the ledger, not a halt
        self.assertEqual(self.ledger()["phases"][1]["neg"], int(self.now))
        self.assertIn(f"Code: `{epic_phases.CODE_CHECKING}`", self.body())
        self.again()  # a second read, still inside the grace window
        self.assertEqual(self.noted, [])
        self.run_pass(epic_phases.GRACE_SECONDS)  # a second read after the window: the halt
        self.assertIn(f"Code: `{epic_phases.CODE_CLOSED}`", self.body())
        self.assertIn("- [ ] Phase 1: #200 closed without a merged pull request", self.body())
        self.assertEqual(self.noted, [(7, f"state=halted code={epic_phases.CODE_CLOSED}")])
        self.assertEqual(self.reader.created, [200])  # the next phase is not opened, and nothing is relabelled
        self.assertNotIn("add_label", self.reader.calls)
        # a merge that shows up late is not held against it
        self.reader.close_child(200, ago=5.0, merged_by=920)
        self.run_pass(epic_gate.RESCAN_SECONDS)  # a halted parent is looked at again hourly, or when it changes
        self.assertEqual(self.reader.created, [200, 201])

    def test_a_first_sighting_long_after_the_close_is_still_not_a_halt(self):
        self.run_pass()
        self.reader.close_child(200, ago=epic_phases.GRACE_SECONDS * 5)
        self.again()
        self.assertIn(f"Code: `{epic_phases.CODE_CHECKING}`", self.body())
        self.assertEqual(self.noted, [])

    def test_an_edit_of_a_child_is_noticed_by_its_hash_and_a_template_change_is_not_an_edit(self):
        self.run_pass()
        with patch.object(epic_phases, "FOOTER", "A different footer.") as _, patch.dict(epic_phases._NEXT, {epic_phases.CODE_OPEN: "x"}):
            with patch.object(epic_phases, "render_child", side_effect=AssertionError("a child is never rendered again to compare")):
                self.again()
        self.assertNotIn("edited", self.body())
        self.reader.children[200]["body"] = f"{self.reader.children[200]['body']}\nsomebody added a line"
        self.again()
        self.assertIn("- [ ] Phase 1: #200 open, edited since Factory opened it", self.body())
        self.assertEqual(self.reader.created, [200])


class RemedyTests(PhaseCase):
    """What a person can really do about a stopped or halted epic. The fixed texts and the operator documentation name only these.

    A stopped, halted or unknown epic is read again hourly: an edit of a phase issue does not touch the parent.
    """

    def remove_claim(self, how, number):
        """The remedy: the issue loses its marker line (a person edits it) or goes away (an administrator deletes it)."""
        if how == "deleted":
            del self.reader.children[number]
        else:
            self.reader.children[number]["body"] = "Not a phase issue any more.\nthe rest of the text"

    def test_two_children_of_one_phase_resolve_when_one_loses_its_marker_line_or_is_deleted_and_the_epic_goes_on(self):
        # #200 is Factory's own and the ledger names it; #300 is a second valid child (a second manager's)
        cases = [(extra, kept, how) for extra, kept in ((300, 200), (200, 300)) for how in ("first line edited", "deleted")]
        for extra, kept, how in cases:
            with self.subTest(extra=extra, how=how):
                self.setUp()
                self.run_pass()
                self.reader.add_child(300, 1)
                self.run_pass(epic_gate.RESCAN_SECONDS)
                self.assertIn(f"State: `{epic_phases.STATE_STOPPED}` · Code: `{epic_phases.CODE_DUPLICATE}`", self.body())
                self.assertEqual(self.reader.created, [200])
                self.remove_claim(how, extra)
                self.run_pass(epic_gate.RESCAN_SECONDS)
                self.assertIn(f"State: `{epic_phases.STATE_PROGRESS}` · Code: `{epic_phases.CODE_OPEN}`", self.body())
                self.assertIn(f"- [ ] Phase 1: #{kept} open", self.body())
                self.assertEqual(self.ledger()["phases"][1]["child"], kept)
                self.reader.close_child(kept, merged_by=910)
                self.again()
                self.assertEqual(self.reader.created, [200, 201])  # the next phase is opened
                self.assertEqual((self.ledger()["phases"][1]["merged"], self.ledger()["phases"][1]["pr"]), (True, 910))

    def test_a_duplicate_of_a_later_phase_keeps_the_merged_phases_in_the_ledger_through_the_remedy(self):
        self.run_pass()
        self.reader.close_child(200, merged_by=910)
        self.again()
        self.assertEqual(self.reader.created, [200, 201])
        merged = self.ledger()["phases"][1]
        self.reader.add_child(300, 2)
        self.run_pass(epic_gate.RESCAN_SECONDS)
        self.assertIn("more than one issue claims it (#201, #300)", self.body())
        self.assertIn("- [x] Phase 1: #200, pull request #910 merged", self.body())
        self.assertEqual(self.ledger()["phases"][1], merged)  # monotone while stopped
        self.reader.children[300]["body"] = "Not a phase issue any more."
        self.run_pass(epic_gate.RESCAN_SECONDS)
        self.assertIn("- [ ] Phase 2: #201 open", self.body())
        self.assertEqual(self.ledger()["phases"][1], merged)  # and after the remedy
        self.reader.close_child(201, merged_by=911)
        self.again()
        self.assertEqual(self.reader.created, [200, 201, 202])
        self.assertEqual([self.ledger()["phases"][n]["pr"] for n in (1, 2)], [910, 911])

    def test_closing_the_extra_issue_does_not_resolve_a_duplicate(self):
        """Factory counts closed phase issues too, so the old advice to close the other one never worked."""
        self.run_pass()
        self.reader.add_child(300, 1)
        self.run_pass(epic_gate.RESCAN_SECONDS)
        self.reader.close_child(300, ago=epic_phases.GRACE_SECONDS * 3)
        for _ in range(3):
            self.run_pass(epic_gate.RESCAN_SECONDS)
        self.assertIn(f"Code: `{epic_phases.CODE_DUPLICATE}`", self.body())
        self.assertIn("more than one issue claims it (#200, #300)", self.body())
        self.assertEqual(self.reader.created, [200])
        self.assertEqual(self.noted, [(7, f"state=stopped code={epic_phases.CODE_DUPLICATE}")] * 4)  # every read still found the stop

    def test_a_halted_epic_goes_on_when_a_person_reopens_the_phase_issue(self):
        self.run_pass()
        self.reader.close_child(200, ago=5.0)
        self.again()
        self.again()
        self.run_pass(epic_phases.GRACE_SECONDS)
        self.assertIn(f"State: `{epic_phases.STATE_HALTED}` · Code: `{epic_phases.CODE_CLOSED}`", self.body())
        self.reader.children[200].update(state="open", closed_at=None)  # reopened
        self.run_pass(epic_gate.RESCAN_SECONDS)
        self.assertIn(f"State: `{epic_phases.STATE_PROGRESS}` · Code: `{epic_phases.CODE_OPEN}`", self.body())
        self.assertIsNone(self.ledger()["phases"][1]["neg"])  # the first sighting of the closed state is forgotten
        self.assertEqual(self.reader.created, [200])
        self.reader.close_child(200, merged_by=920)  # and built and merged like any phase
        self.again()
        self.assertEqual(self.reader.created, [200, 201])
        self.assertTrue(self.ledger()["phases"][1]["merged"])

    def test_deleting_the_status_comment_is_not_a_remedy_because_it_is_the_ledger(self):
        """Without the ledger a merged phase whose issue is gone is opened again, so no text or document advises it."""
        self.run_pass()
        self.reader.close_child(200, merged_by=910)
        self.again()
        self.assertEqual(self.reader.created, [200, 201])
        del self.reader.children[200]
        self.reader.comments.clear()
        self.again()
        self.assertEqual(len(self.reader.created), 3)
        self.assertEqual(self.reader.children[202]["body"].splitlines()[0], epic.phase_marker(7, 1, 3, self.reader.digest()))

    def test_the_fixed_texts_name_the_remedies_above_and_never_tell_anyone_to_close_a_phase_issue(self):
        golden = {
            epic_phases.CODE_CLOSED: "A phase issue was closed without a merged pull request, so Factory opens nothing more. A person "
            "reopens the phase issue to go on with it; Factory looks again within an hour. If it was closed on "
            "purpose, a person applies the `{label}` label to the parent for a new plan, or opens and builds the "
            "remaining phases by hand.",
            epic_phases.CODE_DELETED: "A phase issue no longer exists. Factory never opens it again and opens nothing more. GitHub no "
            "longer lists its open pull request, so Factory cannot rewrite that pull request's first line: a "
            "person looks at it. To resume, a person applies the `{label}` label to the parent for a new plan, or "
            "opens and builds the remaining phases by hand.",
            epic_phases.CODE_DUPLICATE: "More than one issue claims the same phase, so Factory opens nothing more. A person keeps one of them and "
            "deletes the first line (the hidden marker line) of the other, so that it no longer reads as a phase issue of this plan; an "
            "administrator can delete the other issue instead. Closing it does not help, because Factory counts closed phase issues too. "
            "Factory looks again within an hour.",
            epic_phases.CODE_TRUNCATED: "Factory could not prove that the next phase issue does not exist yet: it reads a limited number of "
            "the issues and pull requests it opened itself since the plan was last edited, and there are more "
            "than that. So it opens nothing more. To resume, a person applies the `{label}` label to the parent "
            "for a new plan, or opens and builds the remaining phases by hand.",
        }
        for code, text in golden.items():
            self.assertEqual(epic_phases._NEXT[code], text, code)
        for code, text in epic_phases._NEXT.items():
            if code != epic_phases.CODE_DONE:  # only the parent is closed by a person, and only once every phase is merged
                self.assertIsNone(re.search(r"(?i)\bcloses?\b", text), code)  # "closed" describes a state, "close" would be advice
        self.assertEqual(
            sorted(epic_phases._NEXT),
            sorted(
                [
                    epic_phases.CODE_OPEN, epic_phases.CODE_PENDING, epic_phases.CODE_CHECKING, epic_phases.CODE_DONE,
                    epic_phases.CODE_CLOSED, epic_phases.CODE_DELETED, epic_phases.CODE_DUPLICATE, epic_phases.CODE_TRUNCATED,
                    epic_phases.CODE_RUNNING, epic_phases.CODE_HELD, epic_phases.CODE_STALLED, epic_phases.CODE_ATTEMPTS,
                ]
            ),
        )  # every code the status comment can show has a text (the incomplete one is never written)


# --- the texts --------------------------------------------------------------------------------------------------


class TextTests(PhaseCase):
    CLOSING = re.compile(r"(?i)\b(close[sd]?|fix(e[sd])?|resolve[sd]?)\b\W{0,3}#\d")

    def test_the_child_has_a_fixed_template_and_model_text_only_inside_one_fenced_block(self):
        phases = phases_of(3)
        phases[0].update(title=f"Add {CANARY} @octocat https://evil.example/x", objective=f"Do {CANARY} now closes #5")
        self.reader.scene(7, PLAN_APPROVED, epic.LABEL_APPROVE, phases=phases)
        self.run_pass()
        child = self.reader.children[200]
        body = child["body"]
        outside = re.sub(r"```text\n.*?\n```", "", body, flags=re.S)
        self.assertNotIn(CANARY, outside)
        self.assertEqual(body.count("```text"), 1)
        self.assertIn(CANARY, body)  # the text itself is there, in the block
        for text in (child["title"], body):
            self.assertNotIn("@octocat", text)
            self.assertNotIn("evil.example", text)
        self.assertIsNone(self.CLOSING.search(outside))
        self.assertIn("(part of #7)", outside)
        self.assertEqual(len(re.findall(rf"apply the `{LABEL}` label", outside)), 1)
        self.assertNotIn("@", outside)

    def test_the_status_comment_has_no_model_text_no_mention_and_no_link(self):
        phases = phases_of(3)
        phases[0].update(title=f"Add {CANARY}", objective=f"Do {CANARY}")
        self.reader.scene(7, PLAN_APPROVED, epic.LABEL_APPROVE, phases=phases)
        self.run_pass()
        self.reader.close_child(200, merged_by=910)
        self.again()
        text = self.body()
        self.assertNotIn(CANARY, text)
        self.assertEqual(self.reader.ours()[0]["body"], text)
        visible = text.split("\n", 2)[2]
        self.assertNotRegex(visible, r"@|https?:|www\.|\]\(")

    def test_every_state_renders_a_body_that_obeys_the_rules(self):
        digest = "b" * 64
        entry = {"child": 5, "hash": "", "pr": None, "merged": False, "neg": None}
        for outcome in (
            decide(3, {}, {}, True, NOW),
            decide(3, {}, {1: View((Child(5, "open", None, "c"),))}, True, NOW),
            decide(3, {1: entry}, {1: View((Child(5, "closed", int(NOW) - 5000, "c"),))}, True, NOW),
            decide(3, {1: {**entry, "neg": 1}}, {1: View((Child(5, "closed", int(NOW) - 5000, "c"),))}, True, NOW),
            decide(3, {1: entry}, {1: View(gone=True)}, True, NOW),
            decide(3, {}, {1: View((Child(5, "open", None, "c"), Child(6, "open", None, "c")))}, True, NOW),
            decide(1, {}, {1: View((Child(5, "closed", None, "c"),), evidence="merged", pr=77)}, True, NOW),
            decide(3, {}, {}, False, NOW, truncated=True),
        ):
            with self.subTest(code=outcome.code):
                body = epic_phases.render_status(7, digest, outcome, LABEL)
                self.assertTrue(body.startswith(f"{issue_comments.marker(7, 'epic-status')}\n"))
                self.assertIn(f"Code: `{outcome.code}`", body)
        golden = epic_phases.render_status(7, digest, decide(2, {}, {1: View((Child(5, "open", None, "c"),))}, True, NOW, made={1: (5, "c")}), LABEL)
        self.assertEqual(
            golden.split("\n", 2)[2],
            "\n".join(
                [
                    "**Factory: phase delivery**",
                    "",
                    "State: `in-progress` · Code: `phase-open`",
                    "",
                    "Phases (2), delivered one at a time in this order:",
                    "",
                    "- [ ] Phase 1: #5 open",
                    "- [ ] Phase 2: not opened yet",
                    "",
                    f"**What happens next:** {epic_phases._NEXT[epic_phases.CODE_OPEN].format(label=LABEL)}",
                    "",
                    epic_phases.FOOTER,
                ]
            ),
        )

    def test_the_status_template_refuses_a_mention_or_a_link(self):
        outcome = decide(1, {}, {}, True, NOW)
        for bad in ("@someone", "https://x.example", "www.x.example"):
            with self.subTest(bad), patch.object(epic_phases, "FOOTER", bad), self.assertRaises(ValueError):
                epic_phases.render_status(7, "b" * 64, outcome, LABEL)

    def test_a_label_name_that_is_not_plain_never_reaches_a_text(self):
        self.assertEqual(epic_phases._label("ship `it`\n@x"), issue_comments.DEFAULT_TRIGGER_LABEL)
        self.assertEqual(epic_phases._label("ship it"), "ship it")


class LedgerTests(unittest.TestCase):
    def test_the_ledger_round_trips_and_rejects_tampering(self):
        more = {"started": False, "plans": 0, "stall": None}
        entries = {
            1: {"child": 5, "hash": "a" * 64, "pr": 9, "merged": True, "neg": None, **more},
            2: {"child": 6, "hash": "", "pr": None, "merged": False, "neg": 12, "started": True, "plans": 2, "stall": 99},
        }
        line = epic_phases.encode_ledger(7, "d" * 64, entries)
        # a ledger written before the automatic start has none of the three new fields: they read as their defaults
        old = {"child": 6, "hash": "", "pr": None, "merged": False, "neg": 12}
        self.assertEqual(epic_phases.parse_ledger(epic_phases.encode_ledger(7, "d" * 64, {2: old}))["phases"][2], {**old, **more})
        self.assertEqual(epic_phases.parse_ledger(line), {"parent": 7, "digest": "d" * 64, "phases": entries})
        self.assertIsNone(epic_phases.parse_ledger(f"{line}x"))
        self.assertIsNone(epic_phases.parse_ledger(None))
        self.assertIsNone(epic_phases.parse_ledger(f"{epic_phases.LEDGER_PREFIX}AAAA -->"))
        bad = (
            {"child": True, "hash": "", "pr": None, "merged": False, "neg": None},
            {"child": 5, "hash": "zz", "pr": None, "merged": False, "neg": None},
            {"child": 5, "hash": "", "pr": True, "merged": False, "neg": None},
            {"child": 5, "hash": "", "pr": None, "merged": True, "neg": None},  # merged without a pull request
            {"child": 5, "hash": "", "pr": None, "merged": False, "neg": -1},
            {"child": 5, "hash": "", "pr": None, "merged": False, "neg": True},  # a bool is not a time
            {"child": 5, "hash": "", "pr": None, "merged": "yes", "neg": None},
            {"child": 5, "hash": "", "pr": None, "merged": False, "neg": None, "started": "yes"},
            {"child": 5, "hash": "", "pr": None, "merged": False, "neg": None, "plans": True},  # a bool is not a count
            {"child": 5, "hash": "", "pr": None, "merged": False, "neg": None, "plans": epic_phases.MAX_PLAN_ATTEMPTS + 1},
            {"child": 5, "hash": "", "pr": None, "merged": False, "neg": None, "stall": -1},
            {"child": 5, "hash": "", "pr": None, "merged": False, "neg": None, "stall": True},  # a bool is not a time
        )
        for entry in bad:
            with self.subTest(entry):
                self.assertIsNone(epic_phases.parse_ledger(epic_phases.encode_ledger(7, "d" * 64, {1: entry})))
        self.assertIsNone(epic_phases.parse_ledger(epic_phases.encode_ledger(7, "d" * 64, {epic.MAX_PHASES + 1: entries[2]})))
        self.assertIsNone(epic_phases.parse_ledger(epic_phases.encode_ledger(7, "d" * 64, {True: entries[2]})))  # a bool is not a phase number
        self.assertIsNone(epic_phases.parse_ledger(epic_phases.encode_ledger(True, "d" * 64, {1: entries[2]})))  # nor an issue number


class DecideTests(unittest.TestCase):
    def test_the_walk_stops_at_the_first_phase_that_is_not_merged(self):
        merged = View((Child(5, "closed", 1, "c"),), evidence="merged", pr=50)
        out = decide(3, {}, {1: merged, 2: View((Child(6, "open", None, "c"),)), 3: merged}, True, NOW)
        self.assertEqual([row.kind for row in out.rows], ["merged", "open", "none"])
        self.assertEqual(sorted(out.entries), [1, 2])
        self.assertEqual((out.state, out.code, out.create), ("in-progress", "phase-open", None))

    def test_a_phase_is_created_only_when_the_search_was_complete_and_only_the_first_unmerged_one(self):
        merged = View((Child(5, "closed", 1, "c"),), evidence="merged", pr=50)
        self.assertEqual(decide(3, {}, {1: merged, 2: View()}, True, NOW).create, 2)
        out = decide(3, {}, {1: merged, 2: View()}, False, NOW)
        self.assertEqual((out.create, out.state, out.code), (None, "unknown", "incomplete"))
        self.assertEqual(decide(1, {}, {1: merged}, True, NOW).state, "done")

    def test_a_search_that_ran_out_of_pages_has_its_own_code_and_still_creates_nothing(self):
        merged = View((Child(5, "closed", 1, "c"),), evidence="merged", pr=50)
        out = decide(3, {}, {1: merged, 2: View()}, False, NOW, truncated=True)
        self.assertEqual((out.create, out.state, out.code), (None, "unknown", epic_phases.CODE_TRUNCATED))
        self.assertEqual(sorted(out.entries), [1])  # the merged phase is kept
        out = decide(3, {}, {1: merged, 2: View()}, False, NOW)  # an unknown identity: not reported in the comment
        self.assertEqual((out.create, out.state, out.code), (None, "unknown", epic_phases.CODE_INCOMPLETE))
        self.assertEqual(decide(3, {}, {1: merged, 2: View()}, True, NOW, truncated=True).create, 2)  # a complete search is complete

    def test_a_merged_phase_stays_merged_when_its_claim_cannot_be_checked_but_nothing_unlocks(self):
        prev = {1: {"child": 5, "hash": "", "pr": 50, "merged": True, "neg": None}}
        out = decide(2, prev, {1: View(evidence="unknown")}, True, NOW)
        self.assertEqual((out.create, out.entries[1]["merged"], out.code), (None, True, "checking"))


# --- the reader --------------------------------------------------------------------------------------------------


class SlugApp:
    app_user_id = 4242
    app_slug = "factory-app"


def phase_row(number, state="open", login="factory-app[bot]", kind="Bot", uid=4242, title="Phase 1/3: x", body=None, match=True, closed=""):
    first = epic.phase_marker(7, 1, 3, "c" * 64)
    return {
        "number": number, "state": state, "closed": closed, "login": login, "type": kind, "uid": uid,
        "title": title if match else "", "body": (body or f"{first}\ntext") if match else "", "match": match,
    }


class ReaderTests(unittest.TestCase):
    def reader(self, *answers, auth=SlugApp()):
        stub = StubGh(*answers)
        return GithubReader(REPO, auth, Path("."), run=stub), stub

    def test_phase_issues_lists_only_factorys_own_rows_and_keeps_no_text(self):
        good = phase_row(201, "closed", closed=T1)
        listing = rows(
            good, phase_row(202, login="mallory", kind="User", uid=9), phase_row(203, uid=9999),
            phase_row(204, match=False), phase_row(205, kind="User"),
        )
        reader, stub = self.reader(listing)
        found, truncated = reader.phase_issues(7, since=T0)
        marker = epic.phase_marker(7, 1, 3, "c" * 64)
        self.assertEqual(
            found, [PhaseIssue(201, "closed", T1, True, marker, epic.content_hash(good["title"], good["body"]))]
        )
        self.assertFalse(truncated)
        argv = " ".join(stub.calls[0])
        self.assertIn("creator=factory-app%5Bbot%5D", argv)
        self.assertIn("state=all", argv)
        self.assertIn("since=2027-01-01T00%3A00%3A10Z", argv)
        jq = stub.calls[0][-1]
        self.assertNotIn("select(", jq)  # nothing is dropped inside gh: the end of the listing is decided on the raw page
        self.assertIn("pr: $p", jq)
        self.assertIn(json.dumps(f"{epic.PHASE_PREFIX}parent=7 "), jq)
        self.assertIn("title: (if $m", jq)  # a title or a body only leaves gh for a row that starts with the marker

    def test_phase_issues_says_when_it_ran_out_of_pages_and_refuses_an_unknown_identity(self):
        full = rows(*(phase_row(1000 + n, match=False) for n in range(trigger.PAGE_SIZE)))
        reader, stub = self.reader(*[full] * 3)
        self.assertEqual(reader.phase_issues(7, max_pages=3), ([], True))
        self.assertEqual(len(stub.calls), 3)
        for auth in (type("NoSlug", (), {"app_user_id": 4242})(), type("BadSlug", (), {"app_user_id": 4242, "app_slug": "a b"})()):
            reader, stub = self.reader(auth=auth)
            with self.assertRaises(LookupError):
                reader.phase_issues(7)
            self.assertEqual(stub.calls, [])  # nothing is listed blind

    def test_a_full_page_of_mostly_pull_requests_is_still_a_full_page(self):
        """Factory's own pull requests are listed with its issues: they are printed (``pr`` true), so they count towards the page."""
        pulls = [{**phase_row(1000 + n, match=False), "pr": True} for n in range(trigger.PAGE_SIZE - 1)]
        first = rows(*pulls, phase_row(300, match=False))  # 100 rows, 99 of them pull requests: nothing here is a phase issue
        second = rows(phase_row(201), phase_row(202, match=False))
        reader, stub = self.reader(first, second)
        found, truncated = reader.phase_issues(7)
        self.assertEqual(([row.number for row in found], truncated), ([201], False))
        self.assertEqual(len(stub.calls), 2)  # page 2 was read although page 1 held almost no issue
        full = rows(*pulls, phase_row(300, match=False))
        reader, stub = self.reader(*[full] * 2)
        self.assertEqual(reader.phase_issues(7, max_pages=2), ([], True))  # the page bound was reached on full pages: not complete
        self.assertEqual(len(stub.calls), 2)

    def test_a_boolean_is_not_an_issue_number(self):
        reader, _ = self.reader(rows(phase_row(True)))
        self.assertEqual(reader.phase_issues(7), ([], False))
        reader, _ = self.reader(json.dumps({k: v for k, v in phase_row(True).items() if k != "match"}))
        with self.assertRaises(ValueError):  # an answer that is not understood is an error, never "no such issue"
            reader.phase_issue(1)

    def test_a_row_flagged_as_a_pull_request_is_never_a_phase_issue(self):
        reader, _ = self.reader(rows({**phase_row(201), "pr": True}))
        self.assertEqual(reader.phase_issues(7), ([], False))

    @unittest.skipUnless(shutil.which("jq"), "needs jq")
    def test_the_real_filter_keeps_counting_pull_requests_and_finds_the_phase_issue_on_a_later_page(self):
        """The listing is read through the real jq over GitHub-shaped pages: the filter, not a stub, decides what a page holds."""
        marker = epic.phase_marker(7, 1, 3, "c" * 64)

        def item(number, pull=False, body="plain text"):
            row = {
                "number": number, "state": "open", "closed_at": None, "title": f"item {number}", "body": body,
                "user": {"login": "factory-app[bot]", "type": "Bot", "id": 4242},
            }
            if pull:
                row["pull_request"] = {"url": f"https://api.github.com/repos/o/r/pulls/{number}"}
            return row

        # Newest first, as GitHub lists them; every second item is a pull request; the phase issue is the 131st item (page 2).
        items = [item(500 - n, pull=n % 2 == 0) for n in range(130)]
        items.append(item(200, body=f"{marker}\ntext"))
        items.insert(3, item(499, pull=True, body=f"{marker}\nSECRET-PR-TEXT"))  # a pull request that copies the marker
        served = []

        def run(argv, cwd, timeout=None, auth=None):
            path = argv[2]
            page = int(re.search(r"[?&]page=(\d+)", path).group(1))
            served.append(page)
            size = trigger.PAGE_SIZE
            done = subprocess.run(
                ["jq", "-c", argv[argv.index("--jq") + 1]],
                input=json.dumps(items[(page - 1) * size : page * size]), capture_output=True, text=True, check=True,
            )
            self.assertNotIn("SECRET-PR-TEXT", done.stdout)  # a pull request's text never leaves gh
            return done.stdout

        reader = GithubReader(REPO, SlugApp(), Path("."), run=run)
        found, truncated = reader.phase_issues(7)
        self.assertEqual(([row.number for row in found], truncated), ([200], False))
        self.assertEqual(served, [1, 2])  # page 1 had 100 items of which about half are pull requests: it was still a full page
        served.clear()
        found, truncated = reader.phase_issues(7, max_pages=1)
        self.assertEqual((found, truncated, served), ([], True, [1]))  # the child is past the bound: say so, find nothing

    def test_the_ambient_login_is_the_creator_when_there_is_no_app_identity(self):
        reader, stub = self.reader("factory-ci\n", "factory-ci\n", rows({**phase_row(201), "login": "factory-ci", "type": "User", "uid": 5}), auth=None)
        found, _ = reader.phase_issues(7)
        self.assertEqual([row.number for row in found], [201])
        self.assertIn("creator=factory-ci", " ".join(stub.calls[2]))

    def test_one_issue_by_number_is_none_when_it_does_not_exist_and_an_error_is_not_none(self):
        reader, _ = self.reader(json.dumps({k: v for k, v in phase_row(201).items() if k != "match"}))
        self.assertEqual(reader.phase_issue(201).number, 201)
        for status in (404, 410):
            error = subprocess.CalledProcessError(1, ["gh"], stderr=f"gh: Gone (HTTP {status})")
            reader = GithubReader(REPO, SlugApp(), Path("."), run=lambda *a, error=error, **k: (_ for _ in ()).throw(error))
            self.assertIsNone(reader.phase_issue(201))
        error = subprocess.CalledProcessError(1, ["gh"], stderr="gh: Server Error (HTTP 500)")
        reader = GithubReader(REPO, SlugApp(), Path("."), run=lambda *a, **k: (_ for _ in ()).throw(error))
        with self.assertRaises(subprocess.CalledProcessError):
            reader.phase_issue(201)
        reader, _ = self.reader(json.dumps({**{k: v for k, v in phase_row(201).items() if k != "match"}, "pr": True}))
        self.assertFalse(reader.phase_issue(201).ours)  # a pull request is never a phase issue

    def test_closing_pulls_reads_numbers_states_and_repositories_only(self):
        answer = json.dumps({"total": 3, "prs": [{"n": 110, "s": "MERGED", "r": "Org/Name"}, {"n": 111, "s": "OPEN", "r": "org/name"}, {"n": 112, "s": "WEIRD", "r": "x/y"}]})
        reader, stub = self.reader(answer)
        found = reader.closing_pulls(101)
        self.assertEqual(found, ClosingPulls(((110, "org/name", "MERGED"), (111, "org/name", "OPEN"), (112, "x/y", "UNKNOWN")), True))
        argv = stub.calls[0]
        self.assertEqual(argv[:3], ["gh", "api", "graphql"])
        self.assertIn("number=101", argv)
        self.assertIn("owner=org", argv)
        query = next(arg for arg in argv if arg.startswith("query="))
        self.assertNotIn("title", f"{query}{argv[-1]}")
        self.assertNotIn("body", f"{query}{argv[-1]}")
        self.assertIn("closedByPullRequestsReferences", query)

    def test_the_closing_query_asks_for_closed_and_merged_pull_requests(self):
        """Without ``includeClosedPrs`` GitHub leaves out every merged pull request, so a merged phase would read as none."""
        connection = re.search(r"closedByPullRequestsReferences\(([^)]*)\)", trigger.CLOSING_QUERY)
        self.assertIsNotNone(connection)
        arguments = [part.strip() for part in connection[1].split(",")]
        self.assertIn("includeClosedPrs:true", arguments)
        self.assertIn("orderByState:true", arguments)  # open ones first: the read of 20 is less likely to cut a merged one
        self.assertIn(f"first:{trigger.CLOSING_FIRST}", arguments)
        self.assertEqual(trigger.CLOSING_QUERY.count("{"), trigger.CLOSING_QUERY.count("}"))
        self.assertEqual(trigger.CLOSING_QUERY.count("("), trigger.CLOSING_QUERY.count(")"))

    @unittest.skipUnless(shutil.which("jq"), "needs jq")
    def test_a_merged_closing_pull_request_is_read_through_the_real_filter_only_when_the_query_asks_for_it(self):
        """A stand-in for GitHub that, like GitHub, lists a merged pull request only when ``includeClosedPrs:true`` is asked.

        The answer goes through the real jq and the real ``_CLOSING_JQ``: neither the query's arguments nor the filter is stubbed.
        """
        nodes = [
            {"number": 140, "state": "MERGED", "title": "SECRET-PR-TITLE", "repository": {"nameWithOwner": "Org/Name"}},
            {"number": 141, "state": "CLOSED", "repository": {"nameWithOwner": "org/name"}},
        ]

        def github(query):
            wanted = "includeClosedPrs:true" in query
            listed = nodes if wanted else []
            return {"data": {"repository": {"issue": {"closedByPullRequestsReferences": {"totalCount": len(listed), "nodes": listed}}}}}

        def run(argv, cwd, timeout=None, auth=None):
            query = next(arg for arg in argv if arg.startswith("query="))
            done = subprocess.run(
                ["jq", "-c", argv[argv.index("--jq") + 1]], input=json.dumps(github(query)), capture_output=True, text=True, check=True,
            )
            self.assertNotIn("SECRET-PR-TITLE", done.stdout)  # a title never leaves gh
            return done.stdout

        found = GithubReader(REPO, SlugApp(), Path("."), run=run).closing_pulls(101)
        self.assertEqual(found, ClosingPulls(((140, "org/name", "MERGED"), (141, "org/name", "CLOSED")), True))
        self.assertEqual(github(trigger.CLOSING_QUERY)["data"]["repository"]["issue"]["closedByPullRequestsReferences"]["totalCount"], 2)
        # The failure this pins: the same issue read WITHOUT the argument is complete and empty, which reads as "no merged pull request".
        self.assertEqual(github("closedByPullRequestsReferences(first:20)")["data"]["repository"]["issue"]["closedByPullRequestsReferences"]["totalCount"], 0)

    def test_closing_pulls_is_incomplete_when_truncated_or_not_understood_and_a_bad_answer_raises(self):
        cases = {
            "more references than read": {"total": 25, "prs": [{"n": 1, "s": "MERGED", "r": "org/name"}]},
            "a boolean is not a number": {"total": 1, "prs": [{"n": True, "s": "MERGED", "r": "org/name"}]},
            "a node without a repository": {"total": 1, "prs": [{"n": 5, "s": "MERGED", "r": None}]},
        }
        for name, document in cases.items():
            with self.subTest(name):
                reader, _ = self.reader(json.dumps(document))
                self.assertFalse(reader.closing_pulls(101).complete)
        for bad in ('{"total": true, "prs": []}', '{"total": 1}', "[]", '{"total": -1, "prs": []}'):
            with self.subTest(bad):
                reader, _ = self.reader(bad)
                with self.assertRaises(ValueError):
                    reader.closing_pulls(101)

    def test_create_issue_sends_the_labels_with_the_issue_and_returns_the_number(self):
        reader, stub = self.reader("201\n")
        self.assertEqual(reader.create_issue("T", "B", (epic.LABEL_PHASE,)), 201)
        argv = stub.calls[0]
        self.assertEqual(argv[:4], ["gh", "api", "-X", "POST"])
        self.assertIn("labels[]=factory:epic-phase", argv)
        self.assertNotIn(LABEL, " ".join(argv))
        reader, _ = self.reader("0\n")
        with self.assertRaises(ValueError):
            reader.create_issue("T", "B", ())

    def test_create_issue_passes_every_value_as_a_raw_field_in_one_argv_element(self):
        """The arguments are a list for gh (no shell) and every value goes in with ``-f``, never ``-F``: ``@file`` is text, ``-x`` no option."""
        for text in ("@/etc/passwd", "-X DELETE", "--jq .x", "$(id); `id` | rm -rf /", "one\n@two", "-F body=@/etc/passwd"):
            with self.subTest(text):
                reader, stub = self.reader("201\n")
                self.assertEqual(reader.create_issue(text, f"@{text}", ("a label", "@label")), 201)
                argv = stub.calls[0]
                self.assertIsInstance(argv, list)
                for field in (f"title={text}", f"body=@{text}", "labels[]=a label", "labels[]=@label"):
                    self.assertEqual(argv.count(field), 1)  # one element: a space or a newline never splits it
                    self.assertEqual(argv[argv.index(field) - 1], "-f")  # a raw field: gh never reads a file for it
                self.assertEqual([arg for arg in argv if arg.startswith("-")], ["-X", "--jq", "-f", "-f", "-f", "-f"])  # no -F, no other option
                self.assertEqual(len(argv), 15)

    def test_the_status_comment_is_found_by_marker_and_author_id(self):
        line = epic_phases.encode_ledger(7, "d" * 64, {})
        body = f"{issue_comments.marker(7, 'epic-status')}\n{line}\ntext"

        def row(ident, uid, kind, **extra):
            return {"id": ident, "login": "x", "type": kind, "uid": uid, "created": T3, "updated": T0, "match": True, "plan": line, "body": body, **extra}

        reader, stub = self.reader(rows(row(30, 9, "User"), row(20, 4242, "Bot"), row(10, 4243, "Bot")))
        found = reader.epic_status(7)
        self.assertEqual(found, StatusComment(20, T0, line, body))
        self.assertIn(json.dumps(issue_comments.marker(7, "epic-status")), stub.calls[0][-1])
        self.assertIn("body: (if $m", stub.calls[0][-1])  # the whole body only of a comment that starts with the marker

    def test_a_plan_comment_whose_marker_names_another_issue_has_no_plan(self):
        other = epic.hidden(8, epic.validate({"phases": phases_of(2), "questions": []}))
        mine = epic.hidden(7, epic.validate({"phases": phases_of(2), "questions": ["q"]}))
        reader, _ = self.reader(rows({"id": 5, "login": "x", "type": "Bot", "uid": 4242, "created": T3, "updated": T0, "match": True, "plan": other}))
        found = reader.plan_comment(7)
        self.assertEqual((found.questions, found.plan, found.digest), (None, None, ""))
        reader, _ = self.reader(rows({"id": 5, "login": "x", "type": "Bot", "uid": 4242, "created": T3, "updated": T0, "match": True, "plan": mine}))
        found = reader.plan_comment(7)
        self.assertEqual((found.questions, found.plan["issue"], len(found.plan["phases"])), (1, 7, 2))
        self.assertEqual(found.digest, epic.digest(found.plan))

    def test_the_digest_follows_the_hidden_payload_only(self):
        one = {"issue": 7, **epic.validate({"phases": phases_of(2), "questions": []})}
        two = {"issue": 7, **epic.validate({"phases": phases_of(2), "questions": []})}
        self.assertEqual(epic.digest(one), epic.digest(two))
        changed = {"issue": 7, **epic.validate({"phases": phases_of(3), "questions": []})}
        self.assertNotEqual(epic.digest(one), epic.digest(changed))
        self.assertNotEqual(epic.digest(one), epic.digest({**one, "issue": 8}))
        marker = epic.phase_marker(7, 2, 3, epic.digest(one))
        self.assertEqual(epic.parse_phase_marker(marker), (7, 2, 3, epic.digest(one)))
        for bad in ("", "x", marker.replace("phase=2", "phase=4"), marker.replace("of=3", "of=9"), marker.replace("parent=7", "parent=0"), None):
            self.assertIsNone(epic.parse_phase_marker(bad))


# --- bounds -------------------------------------------------------------------------------------------------------


class BoundTests(PhaseCase):
    def test_at_most_ten_parents_are_read_per_pass_round_robin_and_each_is_reached(self):
        for number in range(10, 35):
            self.reader.scene(number, PLAN_APPROVED, epic.LABEL_APPROVE)
        self.reader.issues.pop(7)
        self.status.cap = 100  # the comment cap has its own test below
        reads = [self.run_pass() for _ in range(3)]
        self.assertEqual(reads, [10, 10, 5])
        self.assertEqual(sorted(self.reader.created), list(range(200, 225)))
        self.assertEqual(len({child["body"].split("\n", 1)[0] for child in self.reader.children.values()}), 25)  # one phase 1 per parent

    def test_parents_whose_reads_raise_stop_at_the_bound_and_the_cursor_moves_past_them(self):
        for number in range(10, 35):  # 25 parents whose plan comment cannot be read (a rate limit window, say)
            self.reader.scene(number, PLAN_APPROVED, epic.LABEL_APPROVE)
            self.reader.plans[number] = OSError("rate limited")
        self.reader.issues.pop(7)
        wanted = [list(range(10, 20)), list(range(20, 30)), [*range(30, 35), *range(10, 15)]]  # round robin, wrapping round
        for tried in wanted:
            self.reader.calls.clear()
            del self.failures[:]
            self.assertEqual(self.run_pass(), epic_phases.MAX_READS_PER_PASS)
            self.assertEqual(self.reader.calls.count("plan_comment"), epic_phases.MAX_READS_PER_PASS)  # never 25
            self.assertEqual([number for number, _ in self.failures], tried)
        self.assert_nothing_written()

    def test_parents_whose_approval_does_not_stand_are_full_reads_and_bounded_too(self):
        for number in range(10, 35):  # a triager applied both labels to 25 real plans: each is a full read that ends "off"
            self.reader.scene(number, PLAN_APPROVED, epic.LABEL_APPROVE, actor=Actor("bob", "User", "5", T2))
        self.reader.issues.pop(7)
        spent = []
        for _ in range(4):
            self.reader.calls.clear()
            reads = self.run_pass()
            spent.append((reads, self.reader.calls.count("label_actor")))
        self.assertEqual(spent, [(10, 10), (10, 10), (5, 5), (0, 0)])  # then each is skipped until it changes
        self.assert_nothing_written()

    def test_a_parent_whose_read_keeps_failing_does_not_block_the_parents_after_it_across_passes(self):
        for number in range(10, 24):  # the first ten always fail: they use the whole bound of a pass
            self.reader.scene(number, PLAN_APPROVED, epic.LABEL_APPROVE)
        for number in range(10, 20):
            self.reader.plans[number] = OSError("always")
        self.reader.issues.pop(7)
        self.run_pass()
        self.assertEqual(self.reader.created, [])
        self.run_pass()  # the cursor stands after the last parent tried: the four that were not reached are read now
        self.assertEqual(len(self.reader.created), 4)
        self.assertEqual(sorted(self.ledger(number)["phases"][1]["child"] for number in range(20, 24)), self.reader.created)

    def test_a_failing_read_does_not_cost_a_cheap_drop_and_junk_does_not_cost_a_full_read(self):
        for number in range(10, 14):  # four parents whose read raises: four full reads
            self.reader.scene(number, PLAN_APPROVED, epic.LABEL_APPROVE)
            self.reader.plans[number] = OSError("down")
        for number in range(20, 30 + epic_phases.MAX_DROPS_PER_PASS):  # junk: no plan comment at all
            self.reader.scene(number, PLAN_APPROVED, epic.LABEL_APPROVE, plan=False)
        self.reader.issues.pop(7)
        self.assertEqual(self.run_pass(), 4)  # the junk dropped in the same pass is not counted as a read
        self.assertEqual(self.reader.calls.count("plan_comment"), 4 + epic_phases.MAX_DROPS_PER_PASS)

    def test_the_status_writers_per_pass_cap_ends_the_pass_and_the_parents_not_reached_come_first_next_time(self):
        for number in range(10, 22):
            self.reader.scene(number, PLAN_APPROVED, epic.LABEL_APPROVE)
        self.reader.issues.pop(7)
        reads = [self.run_pass() for _ in range(3)]
        self.assertEqual(reads, [5, 5, 2])  # the default cap is five comments per pass
        self.assertEqual(len(self.reader.created), 12)
        self.assertEqual(len(self.reader.comment_writes), 12)

    def test_nothing_is_read_while_the_status_writer_may_not_write(self):
        self.status.configure(False)
        self.assertEqual(self.run_pass(), 0)
        self.assertEqual(self.reader.calls, [])

    def test_junk_labels_cannot_starve_a_real_epic_and_cost_nothing_once_seen(self):
        for number in range(10, 75):  # 65 issues somebody labelled by hand: no Factory plan comment at all
            self.reader.scene(number, PLAN_APPROVED, epic.LABEL_APPROVE, plan=False)
        self.reader.scene(100, PLAN_APPROVED, epic.LABEL_APPROVE)
        self.reader.issues.pop(7)
        passes = 0
        while not self.reader.created and passes < 5:
            self.run_pass()
            passes += 1
        self.assertEqual((self.reader.created, passes <= 3), ([200], True))
        self.reader.calls.clear()
        self.run_pass()
        self.assertEqual([c for c in self.reader.calls if c == "plan_comment"], [])  # the junk is not read again
        self.assertEqual(self.reader.comment_writes, ["POST"])

    def test_a_parent_with_an_open_phase_is_read_again_after_two_minutes_and_a_quiet_one_is_not(self):
        self.run_pass()
        self.reader.calls.clear()
        self.run_pass(epic_phases.REREAD_SECONDS - 1)
        self.assertEqual(self.reader.calls, ["open_issues", "open_issues"])  # the approved parents, then the open phase issues
        self.run_pass(2)
        self.assertIn("plan_comment", self.reader.calls)
        # all phases merged: nothing to watch, only the hourly rescan
        for _ in range(3):
            self.reader.close_child(self.reader.created[-1], merged_by=900 + len(self.reader.created))
            self.again()
        self.reader.calls.clear()
        self.again()
        self.assertEqual(self.reader.calls, ["open_issues", "open_issues"])


class RecheckTests(PhaseCase):
    """A read takes several calls and the write comes after them: the parent is looked at once more right before the write."""

    def pass_with_a_change_after_the_read(self, change):
        original = epic_phases.read_parent

        def read_then_change(*args, **kwargs):
            reading = original(*args, **kwargs)
            change()
            return reading

        with patch.object(epic_phases, "read_parent", read_then_change):
            return self.run_pass()

    def test_a_parent_that_changed_between_the_read_and_the_write_gets_nothing_and_loses_nothing(self):
        cases = {
            "closed": (lambda item: item.update(state="closed"), lambda item: item.pop("state")),
            "factory-ignore": (lambda item: item["labels"].append("factory-ignore"), lambda item: item["labels"].remove("factory-ignore")),
            "no plan-approved label": (lambda item: item["labels"].remove(PLAN_APPROVED), lambda item: item["labels"].append(PLAN_APPROVED)),
            "no approval label": (lambda item: item["labels"].remove(epic.LABEL_APPROVE), lambda item: item["labels"].append(epic.LABEL_APPROVE)),
        }
        for name, (change, restore) in cases.items():
            with self.subTest(name):
                self.setUp()
                item = self.reader.issues[7]
                self.pass_with_a_change_after_the_read(lambda: change(item))
                self.assert_nothing_written()  # no phase issue and nothing in the status comment
                self.assertEqual(self.reader.calls.count("phase_issues"), 1)  # the read's search only: no second search, no write
                self.assertEqual((self.said, self.failures, self.reader.comments), ([], [], {}))
                restore(item)
                self.again()  # a guard says "not now", not "never": with the parent as it was the phase opens
                self.assertEqual(self.reader.created, [200])

    def test_an_unchanged_parent_is_written_to(self):
        self.pass_with_a_change_after_the_read(lambda: None)
        self.assertEqual((self.reader.created, self.reader.comment_writes), ([200], ["POST"]))
        self.assertEqual(self.reader.calls.count("phase_issues"), 2)  # the read's search and the one right before the write


# --- the manager's pass ----------------------------------------------------------------------------------------------


class OpsCase(EpicPassCase):
    def setUp(self):
        super().setUp()
        self.readers[REPO] = self.reader = PhaseOpsReader(REPO)
        self.ops = self.new_ops(self.manager, github=lambda cfg: self.readers[cfg.repo])
        self.reader.scene(7, PLAN_APPROVED, epic.LABEL_APPROVE)

    def nothing_happened(self):
        self.assertEqual((self.reader.created, self.reader.comment_writes), ([], []))
        self.assertEqual(self.epic_lines(), [])


class ManagerTests(OpsCase):
    def test_the_pass_opens_phase_one_logs_one_fixed_line_and_starts_nothing(self):
        self.ops.trigger_pass()
        self.assertEqual(self.reader.created, [200])
        self.assertEqual(self.epic_lines(), [f"epic repo={REPO} issue=7 phase=1 child=200 created"])
        self.assertEqual(self.reader.children[200]["labels"], [epic.LABEL_PHASE])  # never the trigger label
        self.assertEqual((self.spawned("plan"), self.spawned("run")), ([], []))  # no run is started
        self.assertEqual(self.reader.issues[7]["labels"], [PLAN_APPROVED, epic.LABEL_APPROVE])  # the parent's labels are untouched
        self.ops.trigger_pass()
        self.assertEqual(self.reader.created, [200])

    def test_the_kill_switches_create_nothing_and_write_nothing(self):
        for extra, env in (("epic_mode = false\n", None), ("", "off"), ("auto_build = false\n", None), ("comment_on_blocked = false\n", None)):
            with self.subTest(extra=extra, env=env):
                self.reader.calls.clear()
                self.configure(extra)
                self.manager.env.pop(epic.ENV_SWITCH, None)
                if env:
                    self.manager.env[epic.ENV_SWITCH] = env
                self.ops.trigger_pass()
                self.nothing_happened()
                if "auto_build" in extra:
                    self.assertEqual(self.reader.calls, [])  # not a single GitHub call
        self.configure("")
        self.manager.env.pop(epic.ENV_SWITCH, None)
        self.manager.env["FACTORY_ISSUE_COMMENTS"] = "off"
        self.ops.trigger_pass()
        self.nothing_happened()
        self.manager.env.pop("FACTORY_ISSUE_COMMENTS")
        self.ops.trigger_pass()
        self.assertEqual(self.reader.created, [200])  # and on again

    def test_a_paused_trigger_and_an_ignored_parent_write_nothing(self):
        with patch.object(self.manager, "trigger_paused", return_value=True):
            self.ops.trigger_pass()
        self.nothing_happened()
        self.reader.scene(7, PLAN_APPROVED, epic.LABEL_APPROVE, "factory-ignore")
        self.ops.trigger_pass()
        self.nothing_happened()

    def test_a_failing_read_is_one_rate_limited_line_with_the_type_and_no_text(self):
        self.ops.trigger_pass()  # opens phase one
        self.reader.close_child(200)
        self.reader.closing[200] = RuntimeError(CANARY)
        self.time.now += epic_phases.REREAD_SECONDS + 1
        self.ops.trigger_pass()
        self.assertEqual(self.epic_lines()[-1], f"epic repo={REPO} issue=7 failed error=RuntimeError")
        # the line for the opened phase, the manager's start that read the same failure first, then the pass's own
        self.assertEqual(self.epic_lines()[1], f"epic repo={REPO} issue=7 start failed error=RuntimeError")
        self.assertEqual(len(self.epic_lines()), 3)
        self.assertTrue(all(CANARY not in line for line in self.lines()))

    def test_a_halt_is_said_once_in_fixed_words(self):
        self.ops.trigger_pass()
        self.reader.close_child(200)
        self.reader.children[200]["closed_at"] = self.time.now - epic_phases.GRACE_SECONDS * 2
        for _ in range(3):
            self.time.now += epic_phases.REREAD_SECONDS + 1
            self.ops.trigger_pass()
        halted = [line for line in self.epic_lines() if "halted" in line]
        self.assertEqual(halted, [f"epic repo={REPO} issue=7 state=halted code={epic_phases.CODE_CLOSED}"])
        self.assertEqual(self.reader.created, [200])  # and nothing is relabelled: the parent keeps its labels
        self.assertEqual(self.reader.issues[7]["labels"], [PLAN_APPROVED, epic.LABEL_APPROVE])

    def test_a_phase_issue_is_an_origin_labelled_issue_and_not_a_status_label(self):
        self.assertNotIn(issue_labels.LABEL_EPIC_PHASE, issue_labels.STATUS_LABELS)
        listing = [{"labels": [{"name": issue_labels.LABEL_EPIC_PHASE}]}, {"labels": [{"name": issue_labels.LABEL_EPIC_PHASE}, {"name": LABEL}]}, {"labels": []}]
        self.assertEqual(
            [issue_labels.is_unadopted(item, LABEL) for item in listing], [True, False, False]
        )
        self.assertEqual(len(issue_labels.drop_unadopted(listing, LABEL)), 2)
        # a person applying the trigger label to a phase issue is a request to plan it, not an issue in flight
        self.assertEqual(trigger.classify(IssueFacts(9, (LABEL, issue_labels.LABEL_EPIC_PHASE)), LABEL), "retry")
        self.assertEqual(trigger.classify(IssueFacts(9, (issue_labels.LABEL_EPIC_PHASE,)), LABEL), None)


if __name__ == "__main__":
    unittest.main()

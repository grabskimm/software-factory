"""Epic mode (src/factory/epic.py, epic_gate.py, the epic parts of issue_comments.py, issue_labels.py, trigger.py).

The plan side: the ``epic`` disposition, its bounds, the one plan comment (fenced, sanitised, idempotent) and
the labels. The gate side: the pure state machine, the authority rules that fail closed, and the manager pass.
"""

import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from factory import append, epic, epic_gate, fragments, issue_comments, issue_labels, planner, trigger
from factory.config import RepoConfig
from factory.epic_gate import AWAITING_APPROVAL, NEEDS_INFO, PLAN_APPROVED, EpicSettings, Facts, decide
from factory.issue_comments import marker, without_factory_comments
from factory.model import validate_plan
from factory.schemas import PLANNER
from factory.trigger import Actor, GithubReader, IssueFacts, PlanComment, Remark
from tests import schemacheck
from tests.test_issue_comments import SECRET, FakeGhCase
from tests.test_plan_append import AppendCase
from tests.test_operations import CANARY
from tests.test_trigger_loop import LABEL, REPO, FakeReader, TriggerCase

PHASES = [
    {
        "title": "Add the ledger",
        "objective": "Persist job events append-only",
        "paths": ["src/factory/history.py"],
        "acceptance": ["events survive a restart"],
        "depends_on": [],
        "cost": "M",
    },
    {
        "title": "Expose read-only endpoints",
        "objective": "Serve the ledger over HTTP",
        "paths": ["src/factory/dashboard.py", "docs/manager.md"],
        "acceptance": ["GET returns the recorded events", "no endpoint writes"],
        "depends_on": [1],
        "cost": "S",
    },
]
QUESTIONS = ["Should the endpoints require authentication?", "Is a database allowed?"]
REASON = "Five coordinated layers; each can be reviewed and merged alone."


def payload(questions=QUESTIONS, phases=PHASES):
    return {"phases": copy.deepcopy(phases), "questions": list(questions)}


def disposition(number=1, questions=QUESTIONS, reason=REASON):
    return {"issue": number, "status": "epic", "reason": reason, "epic": payload(questions)}


def epic_plan(questions=QUESTIONS, number=1, reason=REASON):
    return {
        "version": 1,
        "artifact_roots": [],
        "issues": [{"number": number, "title": "T", "body": ""}],
        "jobs": [],
        "dispositions": [disposition(number, questions, reason)],
    }


# --- the payload -----------------------------------------------------------------------


class PayloadTests(unittest.TestCase):
    def test_a_valid_payload_is_returned_canonical(self):
        clean = epic.validate(payload())
        self.assertEqual([phase["depends_on"] for phase in clean["phases"]], [[], [1]])
        self.assertEqual(clean["questions"], QUESTIONS)
        no_dependencies = copy.deepcopy(PHASES)
        del no_dependencies[0]["depends_on"]
        self.assertEqual(epic.validate({"phases": no_dependencies})["phases"][0]["depends_on"], [])

    def test_the_bounds_hold(self):
        def broken(change):
            document = payload()
            change(document)
            return document

        cases = {
            "no phases": broken(lambda d: d.update(phases=[])),
            "too many phases": broken(lambda d: d.update(phases=[PHASES[0]] * (epic.MAX_PHASES + 1))),
            "too many questions": broken(lambda d: d.update(questions=["q"] * (epic.MAX_QUESTIONS + 1))),
            "long title": broken(lambda d: d["phases"][0].update(title="t" * (epic.MAX_TITLE + 1))),
            "long objective": broken(lambda d: d["phases"][0].update(objective="o" * (epic.MAX_OBJECTIVE + 1))),
            "no paths": broken(lambda d: d["phases"][0].update(paths=[])),
            "too many paths": broken(lambda d: d["phases"][0].update(paths=["p"] * (epic.MAX_PATHS + 1))),
            "long path": broken(lambda d: d["phases"][0].update(paths=["p" * (epic.MAX_PATH + 1)])),
            "no acceptance": broken(lambda d: d["phases"][0].update(acceptance=[])),
            "long criterion": broken(lambda d: d["phases"][0].update(acceptance=["a" * (epic.MAX_CRITERION + 1)])),
            "long question": broken(lambda d: d.update(questions=["q" * (epic.MAX_QUESTION + 1)])),
            "blank question": broken(lambda d: d.update(questions=["   "])),
            "bad cost": broken(lambda d: d["phases"][0].update(cost="XL")),
            "unknown phase key": broken(lambda d: d["phases"][0].update(extra="x")),
            "unknown payload key": broken(lambda d: d.update(extra=1)),
            "missing field": broken(lambda d: d["phases"][0].pop("objective")),
            "forward dependency": broken(lambda d: d["phases"][0].update(depends_on=[2])),
            "self dependency": broken(lambda d: d["phases"][1].update(depends_on=[2])),
            "duplicate dependency": broken(lambda d: d["phases"][1].update(depends_on=[1, 1])),
            "boolean dependency": broken(lambda d: d["phases"][1].update(depends_on=[True])),
            "zero dependency": broken(lambda d: d["phases"][1].update(depends_on=[0])),
            "not a dict": [],
        }
        for name, document in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                epic.validate(document)
        limit = broken(lambda d: d.update(phases=[PHASES[0]] * epic.MAX_PHASES, questions=["q"] * epic.MAX_QUESTIONS))
        epic.validate(limit)  # exactly at the caps is fine

    def test_an_error_never_carries_the_payload(self):
        document = payload()
        document["phases"][0]["title"] = CANARY * 40
        with self.assertRaises(ValueError) as caught:
            epic.validate(document)
        self.assertNotIn(CANARY, str(caught.exception))

    def test_the_schema_bounds_match_the_validator(self):
        item = {"issue": 3, "status": "epic", "reason": REASON, "epic": payload()}
        plan = {"version": 1, "jobs": [], "dispositions": [item]}
        self.assertEqual(schemacheck.errors(plan, PLANNER), [])
        for change in (
            lambda d: d["epic"]["phases"].append(PHASES[0]) or d["epic"]["phases"].extend([PHASES[0]] * 8),
            lambda d: d["epic"].update(questions=["q"] * (epic.MAX_QUESTIONS + 1)),
            lambda d: d["epic"]["phases"][0].update(cost="XL"),
            lambda d: d["epic"]["phases"][0].update(title="t" * (epic.MAX_TITLE + 1)),
            lambda d: d["epic"]["phases"][0].pop("acceptance"),
        ):
            broken = copy.deepcopy(plan)
            change(broken["dispositions"][0])
            self.assertNotEqual(schemacheck.errors(broken, PLANNER), [])

    def test_the_hidden_marker_round_trips_and_rejects_tampering(self):
        clean = epic.validate(payload())
        line = epic.hidden(7, clean)
        self.assertTrue(line.startswith("<!-- factory-epic-plan:v1 ") and line.endswith(" -->"))
        self.assertNotIn("\n", line)
        self.assertEqual(epic.parse_hidden(f"{marker(7, 'epic')}\n{line}\ntext"), {"issue": 7, **clean})
        self.assertIsNone(epic.parse_hidden("no marker here"))
        self.assertIsNone(epic.parse_hidden(None))
        self.assertIsNone(epic.parse_hidden(line.replace("v1 ", "v1 AAAA", 1)))
        self.assertIsNone(epic.parse_hidden("<!-- factory-epic-plan:v1 !!!! -->"))
        self.assertIsNone(epic.parse_hidden(line.replace("-->", "--> ", 1).replace("<!--", "<!-- x", 1)))
        self.assertEqual(epic.question_count({"epic": payload()}), 2)
        self.assertEqual(epic.question_count({"epic": payload(questions=[])}), 0)
        self.assertEqual(epic.question_count({}), 0)

    def test_the_planner_prompt_keeps_or_cuts_the_epic_block(self):
        raw = (Path(planner.__file__).parent / "prompts" / "planner.md").read_text()
        on, off = epic.planner_prompt(raw, True), epic.planner_prompt(raw, False)
        self.assertIn("`epic`", on)
        for text in (on, off):
            self.assertNotIn(epic.BEGIN, text)
            self.assertNotIn(epic.END, text)
        self.assertNotIn("epic", off.lower())  # an opted-out repository's planner is told nothing about epics
        self.assertTrue(off.endswith("an investigation.\n"))  # and gets the prompt it always had
        self.assertTrue(on.startswith(off.rstrip("\n")))
        for phrase in ("blocked", "investigation", "untrusted data"):
            self.assertIn(phrase, on)
        self.assertEqual(epic.planner_prompt("no block", True), "no block")

    def test_the_prompt_example_is_a_valid_epic_disposition(self):
        raw = (Path(planner.__file__).parent / "prompts" / "planner.md").read_text()
        example = json.loads(next(line for line in raw.splitlines() if line.startswith('{"issue":123,"status":"epic"')))
        plan = {"version": 1, "jobs": [], "dispositions": [example]}
        self.assertEqual(schemacheck.errors(plan, PLANNER), [])
        validate_plan({**plan, "issues": [{"number": 123, "title": "t", "body": "b"}]})

    def test_downgrade_makes_blocked_and_keeps_the_reason(self):
        plan = epic_plan()
        plan["dispositions"].append({"issue": 2, "status": "covered", "reason": "r"})
        epic.downgrade(plan)
        self.assertEqual(
            plan["dispositions"],
            [{"issue": 1, "status": "blocked", "reason": REASON}, {"issue": 2, "status": "covered", "reason": "r"}],
        )

    def test_epic_mode_needs_the_switch_the_repository_and_a_way_to_comment(self):
        on = RepoConfig(REPO)
        self.assertTrue(on.epic_mode)  # on by default
        self.assertTrue(epic.enabled(on, {}) and epic.can_plan(on, {}))
        self.assertFalse(epic.enabled(RepoConfig(REPO, epic_mode=False), {}))
        self.assertFalse(epic.can_plan(RepoConfig(REPO, epic_mode=False), {}))
        for off in ("off", "OFF", "0", "false", "no", "disabled"):
            self.assertFalse(epic.enabled(on, {epic.ENV_SWITCH: off}), off)
        self.assertTrue(epic.enabled(on, {epic.ENV_SWITCH: "on"}))
        # without a comment channel the planner's epic cannot be seen, so it is downgraded, but the gate may still run
        quiet = RepoConfig(REPO, comment_on_blocked=False)
        self.assertTrue(epic.enabled(quiet, {}))
        self.assertFalse(epic.can_plan(quiet, {}))
        self.assertFalse(epic.can_plan(on, {issue_comments.ENV_SWITCH: "off"}))
        # no label-trigger pass means no gate pass: nothing would ever move the labels, so the epic is downgraded
        unwatched = RepoConfig(REPO, auto_build=False)
        self.assertTrue(epic.enabled(unwatched, {}))
        self.assertFalse(epic.can_plan(unwatched, {}))
        self.assertFalse(epic.can_plan(SimpleNamespaceConfig(auto_build="yes"), {}))  # anything but True is off
        self.assertTrue(epic.can_plan(SimpleNamespaceConfig(), {}))
        self.assertFalse(epic.enabled(SimpleNamespaceConfig(epic_mode="yes"), {}))  # anything but True is off


class SimpleNamespaceConfig:
    def __init__(self, **values):
        self.__dict__.update(values)


# --- the plan: model, fragments, append, planner -----------------------------------------


class PlanTests(unittest.TestCase):
    def test_validate_plan_accepts_an_epic_disposition_that_owns_no_job(self):
        validate_plan(epic_plan())
        validate_plan(epic_plan(questions=[]))

    def test_validate_plan_rejects_a_malformed_epic(self):
        for name, change in {
            "no payload": lambda d: d.pop("epic"),
            "empty phases": lambda d: d["epic"].update(phases=[]),
            "no reason": lambda d: d.pop("reason"),
            "unknown key": lambda d: d["epic"].update(extra=[]),
        }.items():
            with self.subTest(name), self.assertRaises((ValueError, KeyError)):
                plan = epic_plan()
                change(plan["dispositions"][0])
                validate_plan(plan)
        for status in ("blocked", "covered", "investigation"):
            plan = epic_plan()
            plan["dispositions"][0]["status"] = status  # an epic payload on another status is not allowed
            with self.subTest(status), self.assertRaises(ValueError):
                validate_plan(plan)

    def test_an_epic_disposition_cannot_own_a_job(self):
        plan = epic_plan()
        plan["jobs"] = [
            {"id": "a", "issues": [1], "objective": "o", "likely_paths": ["src"], "acceptance": ["x"], "feature": False}
        ]
        with self.assertRaises(ValueError):
            validate_plan(plan)

    def test_a_fragment_carries_an_epic_disposition_and_it_round_trips(self):
        base = {
            "version": 1, "artifact_roots": [], "issues": [{"number": 5, "title": "t", "body": ""}],
            "jobs": [{"id": "x", "issues": [5], "objective": "o", "likely_paths": ["src"], "acceptance": ["a"],
                      "feature": False}],
            "dispositions": [{"issue": 5, "status": "job", "job": "x", "reason": "r"}],
        }
        result = {"jobs": [], "dispositions": [disposition(8)]}
        document = append.build_fragment(result, [{"number": 8, "title": "t", "body": "b"}])
        self.assertEqual(document["dispositions"][0]["epic"], payload())
        raw = append.validated(document, "append-x", base)
        issues, jobs, dispositions = fragments.parse(raw, 10)
        merged = fragments.merge(base, "fragment-append-x.json", "d" * 64, issues, jobs, 10, dispositions)
        self.assertEqual(merged["dispositions"][-1]["status"], "epic")
        self.assertEqual(merged["dispositions"][-1]["epic"], payload())

    def test_a_fragment_with_a_bad_epic_is_refused(self):
        document = append.build_fragment(
            {"jobs": [], "dispositions": [disposition(8)]}, [{"number": 8, "title": "t", "body": "b"}]
        )
        for change in (
            lambda d: d["epic"].update(phases=[]),
            lambda d: d.pop("epic"),
            lambda d: d.update(status="blocked"),  # an epic payload on a blocked disposition
        ):
            broken = copy.deepcopy(document)
            change(broken["dispositions"][0])
            with self.subTest(), self.assertRaises(fragments.FragmentError):
                fragments.parse(json.dumps(broken).encode(), 10)

    def planned(self, epic_on, answer=None, numbers=(1,)):
        work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        dest = work / "run"
        dest.mkdir()
        listed = [{"number": n, "title": "T", "body": "b", "comments": [], "labels": []} for n in numbers]
        (dest / "issues.json").write_text(json.dumps(listed))
        (dest / "open-prs.json").write_text("[]")
        seen = {}

        def fake_agent(prompt, *args, **kwargs):
            seen["prompt"] = prompt
            return copy.deepcopy(answer or {"version": 1, "jobs": [], "dispositions": [disposition(1)]}), {"phase": "plan"}

        with patch("factory.planner.run_agent", fake_agent), patch("factory.planner.repo_guidance", return_value=""):
            result = planner.plan(work, dest, use_snapshot=True, epic_on=epic_on)
        return result, seen["prompt"], json.loads((dest / "plan.json").read_text())

    def test_the_planner_keeps_an_epic_only_when_epic_mode_can_work(self):
        result, prompt, written = self.planned(True)
        self.assertEqual(written["dispositions"][0]["status"], "epic")
        self.assertIn("Epic mode is on", prompt)
        result, prompt, written = self.planned(False)
        self.assertEqual(written["dispositions"][0], {"issue": 1, "status": "blocked", "reason": REASON})
        self.assertNotIn("Epic mode", prompt)
        self.assertNotIn("epic", prompt.lower())

    def test_an_epic_the_schema_lets_through_but_the_validator_refuses_is_blocked_not_a_failed_plan(self):
        job = {"id": "a", "issues": [2], "objective": "o", "likely_paths": ["src"], "acceptance": ["x"], "feature": False}
        for name, change in {
            "forward dependency": lambda d: d["phases"][0].update(depends_on=[2]),
            "self dependency": lambda d: d["phases"][1].update(depends_on=[2]),
            "duplicate dependency": lambda d: d["phases"][1].update(depends_on=[1, 1]),
            "blank title": lambda d: d["phases"][0].update(title="   "),
            "blank question": lambda d: d.update(questions=["  "]),
            "blank path": lambda d: d["phases"][0].update(paths=[" "]),
            "not a list of numbers": lambda d: d["phases"][1].update(depends_on=[[1]]),
        }.items():
            with self.subTest(name):
                bad = disposition(1)
                change(bad["epic"])
                answer = {
                    "version": 1,
                    "jobs": [job],
                    "dispositions": [bad, {"issue": 2, "status": "job", "job": "a", "reason": "r"}],
                }
                if name != "not a list of numbers":  # (that one the schema does refuse)
                    self.assertEqual(schemacheck.errors(answer, PLANNER), [])
                _, _, written = self.planned(True, answer, numbers=(1, 2))
                self.assertEqual(written["dispositions"][0], {"issue": 1, "status": "blocked", "reason": REASON})
                self.assertEqual(written["dispositions"][1]["status"], "job")  # the rest of the batch is planned
                self.assertEqual([item["id"] for item in written["jobs"]], ["a"])

    def test_a_valid_epic_is_kept_next_to_an_invalid_one(self):
        bad = disposition(1)
        bad["epic"]["phases"][1]["depends_on"] = [2]
        answer = {"version": 1, "jobs": [], "dispositions": [bad, disposition(2)]}
        _, _, written = self.planned(True, answer, numbers=(1, 2))
        self.assertEqual([item["status"] for item in written["dispositions"]], ["blocked", "epic"])
        self.assertNotIn("epic", written["dispositions"][0])

    def test_only_invalid_leaves_a_valid_epic_and_every_other_status_alone(self):
        plan = epic_plan()
        plan["dispositions"].extend([{"issue": 2, "status": "covered", "reason": "r"}, {"issue": 3, "status": "epic", "reason": "r"}])
        epic.downgrade(plan, only_invalid=True)
        self.assertEqual([item["status"] for item in plan["dispositions"]], ["epic", "covered", "blocked"])
        self.assertEqual(plan["dispositions"][0]["epic"], payload())


class AppendEpicTests(AppendCase):
    """``plan --append-to``: an epic fragment is kept only where epic mode can work, else it is today's ``blocked``."""

    def setUp(self):
        super().setUp()
        # the switches are read from the environment: the suite must not depend on the shell that runs it
        environ = patch.dict("os.environ")
        environ.start()
        self.addCleanup(environ.stop)
        for name in (epic.ENV_SWITCH, "FACTORY_ISSUE_COMMENTS"):
            os.environ.pop(name, None)

    def appended(self, cfg=None):
        self.result = {"version": 1, "jobs": [], "dispositions": [disposition(2)]}
        self.append(cfg=cfg)
        (path,) = self.fragments()
        return json.loads(path.read_text())["dispositions"][0], self.prompts[-1]

    def test_an_epic_fragment_carries_the_phase_plan_when_epic_mode_is_on(self):
        item, prompt = self.appended()
        self.assertEqual((item["status"], item["epic"]), ("epic", payload()))
        self.assertIn("Epic mode is on", prompt)

    def test_an_invalid_epic_becomes_blocked_instead_of_refusing_the_fragment(self):
        self.result = {"version": 1, "jobs": [], "dispositions": [disposition(2)]}
        self.result["dispositions"][0]["epic"]["phases"][0]["depends_on"] = [2]
        self.append()
        (path,) = self.fragments()
        self.assertEqual(json.loads(path.read_text())["dispositions"][0], {"issue": 2, "status": "blocked", "reason": REASON})

    def test_it_becomes_blocked_when_epic_mode_is_off_or_there_is_no_way_to_move_it(self):
        for cfg in (
            RepoConfig(repo="org/name", epic_mode=False),
            RepoConfig(repo="org/name", comment_on_blocked=False),
            RepoConfig(repo="org/name", auto_build=False),  # no label-trigger pass: no gate pass either
        ):
            with self.subTest(cfg):
                for path in self.fragments():
                    path.unlink()  # one fragment per issue: start each case from an empty inbox
                item, prompt = self.appended(cfg)
                self.assertEqual(item, {"issue": 2, "status": "blocked", "reason": REASON})
                self.assertNotIn("Epic mode", prompt)


# --- the plan comment --------------------------------------------------------------------


class CommentTests(FakeGhCase):
    def post(self, plan=None, **comment_args):
        labels, commenter = self.labeller(plan or epic_plan(), **comment_args)
        labels.sync_plan()
        return labels, commenter

    def test_one_fixed_template_comment_with_a_hidden_marker_and_fenced_text(self):
        self.post()
        (comment,) = self.comments(1)
        body = comment["body"]
        lines = body.splitlines()
        self.assertEqual(lines[0], marker(1, "epic"))
        self.assertTrue(lines[0].startswith(issue_comments.MARKER_PREFIX))
        self.assertEqual(epic.parse_hidden(body)["questions"], QUESTIONS)
        self.assertIn("**Factory: needs a phased plan**", body)
        self.assertIn("1. [M] Add the ledger", body)
        self.assertIn("2. [S] Expose read-only endpoints", body)
        self.assertIn("Depends on phase: 1", body)
        self.assertIn("1. Should the endpoints require authentication?", body)
        self.assertIn("2. Is a database allowed?", body)
        self.assertIn("`factory:needs-info`", body)
        self.assertIn("`factory-approve-plan`", body)
        self.assertIn("re-apply the `factory-build` label", body)
        self.assertIn("Code: `epic`", body)
        self.assertIn("Run: `run-1`", body)
        # the model's words sit inside fenced blocks only
        outside = []
        inside = False
        for line in lines[2:]:
            if line.startswith("```"):
                inside = not inside
            elif not inside:
                outside.append(line)
        self.assertNotIn("ledger", "\n".join(outside))
        self.assertNotIn("authentication", "\n".join(outside))
        self.assertNotIn("Five coordinated", "\n".join(outside))

    def test_a_plan_without_questions_says_how_to_approve_it(self):
        self.post(epic_plan(questions=[]))
        (comment,) = self.comments(1)
        self.assertNotIn("Open questions", comment["body"])
        self.assertNotIn("factory:needs-info", comment["body"])
        self.assertIn("applies the `factory-approve-plan` label", comment["body"])

    def test_the_comment_is_posted_once_edited_in_place_and_never_duplicated(self):
        labels, commenter = self.post()
        before = len(self.comment_writes())
        labels.reconcile({})  # the next run's reconcile: the same plan
        labels.announce_dispositions(epic_plan()["dispositions"])
        self.assertEqual(len(self.comment_writes()), before)
        self.assertEqual(len(self.comments(1)), 1)
        # a new process (restart) with the same persisted plan finds its own comment: no second one
        again, _ = self.labeller(epic_plan())
        again.reconcile({})
        self.assertEqual(len(self.comments(1)), 1)
        # a re-plan with other questions edits the same comment
        replanned, _ = self.labeller(epic_plan(questions=["Which database?"]), run="run-2")
        replanned.sync_plan()
        (comment,) = self.comments(1)
        self.assertIn("1. Which database?", comment["body"])
        self.assertIn("Run: `run-2`", comment["body"])
        self.assertNotIn("authentication", comment["body"])
        patches = [c for c in self.comment_writes() if "PATCH" in c]
        self.assertEqual(len(patches), 1)

    def test_a_persons_comment_quoting_the_marker_is_never_edited(self):
        store = self.root / "comments.json"
        store.write_text(json.dumps({"1": [{"id": 5, "body": f"{marker(1, 'epic')}\nforged", "user": {"type": "User", "login": "mallory"}}]}))
        self.post()
        comments = self.comments(1)
        self.assertEqual(len(comments), 2)
        self.assertEqual(comments[0]["body"].endswith("forged"), True)
        self.assertEqual([c for c in self.comment_writes() if "PATCH" in c], [])

    def test_hostile_text_is_sanitised_and_cannot_close_the_fence_or_ping(self):
        hostile = copy.deepcopy(epic_plan())
        item = hostile["dispositions"][0]
        item["reason"] = f"```\n@everyone ping https://evil.example/x?token=abc <script>alert(1)</script> {SECRET}"
        item["epic"]["phases"][0]["title"] = "Ship ``` @admin ![x](http://evil.example/p.png)"
        item["epic"]["phases"][0]["objective"] = "ghp_abcdefghijklmnopqrstuvwxyz0123456789 and /etc/passwd"
        item["epic"]["phases"][0]["paths"] = ["/home/user/.ssh/id_rsa", "src/ok.py"]
        item["epic"]["questions"] = ["Ok? [click](javascript:alert(1)) #999 and @team/leads"]
        self.post(hostile)
        (comment,) = self.comments(1)
        body = comment["body"]
        for needle in ("@everyone", "@admin", "evil.example", "<script", "ghp_abcdefghijkl", "/etc/passwd", ".ssh", "javascript:", "@team"):
            self.assertNotIn(needle, body, needle)
        self.assertIn("src/ok.py", body)
        self.assertEqual(body.count("```"), body.count("```text") * 2)  # every fence closes; nothing closed it early
        parsed = epic.parse_hidden(body)
        self.assertIsNotNone(parsed)
        self.assertNotIn("evil.example", json.dumps(parsed))  # the hidden marker carries the sanitised plan only

    def test_the_comment_stays_under_githubs_limit_at_the_caps(self):
        biggest = {
            "title": "t" * epic.MAX_TITLE,
            "objective": "o" * epic.MAX_OBJECTIVE,
            "paths": ["src/%s.py" % ("p" * (epic.MAX_PATH - 8))] * epic.MAX_PATHS,
            "acceptance": ["a" * epic.MAX_CRITERION] * epic.MAX_ACCEPTANCE,
            "depends_on": [],
            "cost": "L",
        }
        # words, not one long token: a long unbroken token is (rightly) redacted as a secret
        words = " ".join(["word"] * 90)
        biggest.update(title=words[: epic.MAX_TITLE], objective=words[: epic.MAX_OBJECTIVE], acceptance=[words[: epic.MAX_CRITERION]] * epic.MAX_ACCEPTANCE)
        big = {"phases": [dict(biggest, depends_on=list(range(1, n))) for n in range(1, epic.MAX_PHASES + 1)],
               "questions": [words[: epic.MAX_QUESTION]] * epic.MAX_QUESTIONS}
        clean = issue_comments.sanitize_epic(big, (), None)
        body = issue_comments.render(1, "epic", "epic", "w " * 400, "run-1", payload=clean)
        self.assertLess(len(body), 65_000)
        self.assertIsNotNone(epic.parse_hidden(body))

    def test_a_run_name_or_code_that_is_not_safe_is_replaced_never_echoed(self):
        clean = issue_comments.sanitize_epic(payload(), (), None)
        body = issue_comments.render(1, "epic", "bad`code\nx", None, "run`1\nignore this", payload=clean)
        self.assertIn("Run: `unknown` \u00b7 Code: `epic`", body)
        self.assertNotIn("ignore this", body)
        self.assertNotIn("bad`code", body)
        body = issue_comments.render(1, "epic", "epic", None, "run-7", payload=clean)
        self.assertIn("Run: `run-7` \u00b7 Code: `epic`", body)

    def test_downgrade_with_only_invalid_never_raises_on_a_payload_it_cannot_read(self):
        plan = {"dispositions": [{"issue": 1, "status": "epic", "reason": "r", "epic": "not a payload"},
                                 {"issue": 2, "status": "epic", "reason": "r"}]}
        epic.downgrade(plan, only_invalid=True)
        self.assertEqual([item["status"] for item in plan["dispositions"]], ["blocked", "blocked"])
        self.assertTrue(all("epic" not in item for item in plan["dispositions"]))

    def test_a_payload_that_cannot_be_read_gives_the_template_only(self):
        broken = epic_plan()
        broken["dispositions"][0]["epic"] = {"phases": []}
        self.post(broken)
        (comment,) = self.comments(1)
        self.assertIn("could not safely quote its plan", comment["body"])
        self.assertIsNone(epic.parse_hidden(comment["body"]))

    def test_short_fields_survive_the_sanitiser(self):
        short = epic_plan()
        short["dispositions"][0]["epic"]["phases"][0].update(title="API", paths=["src"], acceptance=["ok"])
        self.post(short)
        body = self.comments(1)[0]["body"]
        self.assertIn("1. [M] API", body)
        self.assertIn("Touches: src", body)

    def test_the_planner_never_sees_the_epic_comment(self):
        self.post()
        (comment,) = self.comments(1)
        issue = {"number": 1, "comments": [{"body": comment["body"]}, {"body": "a person's answer"}]}
        self.assertEqual(without_factory_comments(issue)["comments"], [{"body": "a person's answer"}])

    def test_no_comment_without_a_comment_channel(self):
        self.post(enabled=False)
        self.assertEqual(self.comments(1), [])
        with patch.dict("os.environ", {issue_comments.ENV_SWITCH: "off"}):
            self.post()
        self.assertEqual(self.comments(1), [])

    def test_no_comment_on_an_ignored_issue(self):
        self.set_labels(1, "factory-ignore")
        self.post()
        self.assertEqual(self.comments(1), [])


# --- the labels --------------------------------------------------------------------------


class LabelTests(FakeGhCase):
    def label_calls(self, number):
        return [
            c for c in self.calls()
            if "-X" in c and any(f"/issues/{number}/labels" in a for a in c)
        ]

    def test_epic_is_not_in_the_static_disposition_table(self):
        self.assertNotIn("epic", issue_labels.DISPOSITION_LABELS)

    def test_a_fresh_epic_starts_needing_info_or_awaiting_approval(self):
        self.labeller(epic_plan())[0].sync_plan()
        added = [a for c in self.label_calls(1) for a in c if a.startswith("labels[]=")]
        self.assertEqual(added, ["labels[]=factory:needs-info"])
        self.assertIn(["label", "create", "factory-approve-plan"], [c[:3] for c in self.calls()])
        self.assertTrue(any(c[:3] == ["label", "create", "factory:needs-info"] for c in self.calls()))
        # no questions: it starts awaiting approval
        (self.root / "calls.jsonl").unlink()
        self.labeller(epic_plan(questions=[]))[0].sync_plan()
        added = [a for c in self.label_calls(1) for a in c if a.startswith("labels[]=")]
        self.assertEqual(added, ["labels[]=factory:awaiting-approval"])

    def test_the_first_gate_label_goes_on_only_after_the_plan_comment_is_current(self):
        # a gate pass that ran between the label and the comment edit would read the OLD plan under the NEW label
        self.labeller(epic_plan())[0].sync_plan()
        calls = self.calls()
        label_at = next(i for i, c in enumerate(calls) if any(a == "labels[]=factory:needs-info" for a in c))
        comment_at = next(i for i, c in enumerate(calls) if any("/issues/1/comments" in a for a in c) and ("POST" in c or "PATCH" in c))
        self.assertLess(comment_at, label_at)

    def test_a_fresh_plan_takes_a_stale_approval_off(self):
        self.set_labels(1, "factory-approve-plan")
        self.labeller(epic_plan())[0].sync_plan()
        self.assertTrue(any(c[2] == "DELETE" and c[3].endswith("/labels/factory-approve-plan") for c in self.label_calls(1)))

    def test_reconcile_never_overwrites_the_gate_label_or_removes_an_approval(self):
        self.set_labels(1, "factory:awaiting-approval", "factory-approve-plan")
        labels, _ = self.labeller(epic_plan())
        labels.reconcile({})
        self.assertEqual(self.label_calls(1), [])  # no write at all: the gate pass owns the label now
        self.assertEqual(len(self.comments(1)), 1)  # the comment self-heals

    def test_purge_keeps_the_gate_label_where_a_blocked_issue_loses_its_label(self):
        self.set_labels(1, "factory:plan-approved")
        self.set_labels(2, "factory:blocked")
        plan = epic_plan()
        plan["issues"].append({"number": 2, "title": "B", "body": ""})
        plan["dispositions"].append({"issue": 2, "status": "blocked", "reason": "r"})
        labels, _ = self.labeller(plan)
        labels.settle_after_purge({})
        self.assertEqual(self.label_calls(1), [])
        self.assertTrue(any(c[2] == "DELETE" for c in self.label_calls(2)))

    def test_a_job_label_replaces_the_gate_labels(self):
        self.set_labels(1, "factory:needs-info")
        labels, _ = self.labeller(epic_plan())
        labels.sync({1: issue_labels.LABEL_PLANNED})
        removed = [c[3].rsplit("/", 1)[1] for c in self.label_calls(1) if c[2] == "DELETE"]
        self.assertEqual(removed, ["factory%3Aneeds-info"])

    def test_the_three_labels_are_status_labels(self):
        for label in issue_labels.EPIC_LABELS:
            self.assertIn(label, issue_labels.STATUS_LABELS)
        self.assertEqual(set(issue_labels.EPIC_LABELS), set(epic_gate.EPIC_STATUS_LABELS))


# --- the gate: the pure state machine ------------------------------------------------------


class DecideTests(unittest.TestCase):
    def test_the_table(self):
        full = dict(complete=True, plan_found=True)
        cases = [
            (Facts(state=NEEDS_INFO, **full, authorised_reply=True), AWAITING_APPROVAL),
            (Facts(state=NEEDS_INFO, **full), None),
            # an approval never skips the questions
            (Facts(state=NEEDS_INFO, **full, approval_present=True, approval_authorised=True, approval_current=True), None),
            (Facts(state=AWAITING_APPROVAL, **full, approval_present=True, approval_authorised=True, approval_current=True), PLAN_APPROVED),
            # a reply never approves
            (Facts(state=AWAITING_APPROVAL, **full, authorised_reply=True), None),
            (Facts(state=AWAITING_APPROVAL, **full, approval_present=True, approval_authorised=False, approval_current=True), None),
            (Facts(state=AWAITING_APPROVAL, **full, approval_present=True, approval_authorised=True, approval_current=False), None),
            (Facts(state=AWAITING_APPROVAL, **full, approval_authorised=True, approval_current=True), None),
            # whatever is unknown keeps the lower state
            (Facts(state=NEEDS_INFO, complete=False, plan_found=True, authorised_reply=True), None),
            (Facts(state=NEEDS_INFO, complete=True, plan_found=False, authorised_reply=True), None),
            (Facts(state=None, **full, authorised_reply=True), None),
            (Facts(state=PLAN_APPROVED, **full, authorised_reply=True), None),
            # ignore and a closed issue win over everything
            (Facts(state=NEEDS_INFO, **full, authorised_reply=True, ignored=True), None),
            (Facts(state=NEEDS_INFO, **full, authorised_reply=True, open=False), None),
            (Facts(state=AWAITING_APPROVAL, **full, approval_present=True, approval_authorised=True, approval_current=True, ignored=True), None),
        ]
        for facts, expected in cases:
            with self.subTest(facts):
                self.assertEqual(decide(facts), expected)
        self.assertIsNone(decide(Facts()))

    def test_the_labels_decide_the_state(self):
        self.assertEqual(epic_gate.state_of(["bug", "Factory:Needs-Info"]), NEEDS_INFO)
        self.assertEqual(epic_gate.state_of([AWAITING_APPROVAL]), AWAITING_APPROVAL)
        self.assertEqual(epic_gate.state_of([AWAITING_APPROVAL, NEEDS_INFO]), NEEDS_INFO)  # the lower one
        self.assertIsNone(epic_gate.state_of([NEEDS_INFO, PLAN_APPROVED]))  # approved is terminal
        self.assertIsNone(epic_gate.state_of([NEEDS_INFO, "factory-ignore"]))
        self.assertIsNone(epic_gate.state_of(["bug"]))

    def test_authorised_needs_a_person_at_the_minimum(self):
        user = Actor("alice", "User")
        self.assertTrue(epic_gate.authorised(user, "write", "write"))
        self.assertTrue(epic_gate.authorised(user, "admin", "write"))
        for level in ("read", "triage", "none", "custom-role", None, ""):
            self.assertFalse(epic_gate.authorised(user, level, "write"), level)
        self.assertFalse(epic_gate.authorised(Actor("bot[bot]", "Bot"), "admin", "write"))
        self.assertFalse(epic_gate.authorised(Actor("org", "Organization"), "admin", "write"))
        self.assertFalse(epic_gate.authorised(None, "admin", "write"))
        self.assertFalse(epic_gate.authorised(user, "write", "bogus"))


# --- the gate against an in-memory GitHub ----------------------------------------------------

T0, T1, T2, T3 = "2027-01-01T00:00:10Z", "2027-01-01T00:00:20Z", "2027-01-01T00:00:30Z", "2027-01-01T00:00:05Z"


class GateReader:
    """The reader methods the gate uses, over a small in-memory GitHub; every call is recorded."""

    def __init__(self):
        self.permissions = {"alice": "write", "carol": "admin", "bob": "triage", "mallory": "read"}
        self.issues = {}  # number -> {"labels": [...], "updated": str}
        self.plans = {}  # number -> PlanComment | None
        self.replies = {}  # number -> ([Remark], truncated)
        self.actors = {}  # number -> Actor | None
        self.errors = {}  # method name -> exception
        self.calls = []

    def scene(self, number, *labels, plan=True, questions=2, updated="1", replies=(), actor=None, truncated=False):
        self.issues[number] = {"labels": list(labels), "updated": updated}
        self.plans[number] = PlanComment(100, "factory[bot]", T3, T0, questions) if plan else None
        self.replies[number] = (list(replies), truncated)
        self.actors[number] = actor

    def _enter(self, name):
        self.calls.append(name)
        if name in self.errors:
            raise self.errors[name]

    def open_issues(self, label):
        self._enter("open_issues")
        return [
            IssueFacts(n, tuple(item["labels"]), item["updated"])
            for n, item in sorted(self.issues.items())
            if label in item["labels"]
        ]

    def plan_comment(self, number):
        self._enter("plan_comment")
        if isinstance(self.plans[number], Exception):
            raise self.plans[number]
        return self.plans[number]

    def issue_state(self, number):
        self._enter("issue_state")
        item = self.issues[number]
        return item.get("state", "open"), tuple(item["labels"])

    def issue_replies(self, number, after_id, since="", max_pages=10):
        self._enter("issue_replies")
        return self.replies[number]

    def label_actor(self, number, label):
        self._enter("label_actor")
        return self.actors[number]

    def permission(self, login):
        self._enter("permission")
        return self.permissions.get(login, "none")

    def ensure_label(self, label, color, description):
        self._enter("ensure_label")

    def add_label(self, number, label):
        self._enter("add_label")
        self.issues[number]["labels"].append(label)
        self.issues[number]["updated"] = f"{self.issues[number]['updated']}+"

    def remove_label(self, number, label):
        self._enter("remove_label")
        self.issues[number]["labels"].remove(label)

    def writes(self):
        return [c for c in self.calls if c in ("ensure_label", "add_label", "remove_label")]


def reply(ident, login, kind="User", created=T1, association=""):
    return Remark("issue", ident, login, kind, created, False, created, association)


class GateCase(unittest.TestCase):
    def setUp(self):
        self.reader = GateReader()
        self.settings = EpicSettings(True, "write")
        self.moves, self.failures, self.seen = [], [], {}

    def run_pass(self, now=0.0, settings=None):
        return epic_gate.run_pass(
            self.reader, settings or self.settings,
            lambda number, label: self.moves.append((number, label)),
            lambda number, error: self.failures.append((number, type(error).__name__)),
            self.seen, now,
        )

    def labels(self, number):
        return sorted(self.reader.issues[number]["labels"])


class QuestionRoundTripTests(GateCase):
    def test_an_authorised_answer_then_an_approval_move_the_issue_step_by_step(self):
        self.reader.scene(7, NEEDS_INFO, replies=[reply(101, "alice")])
        self.run_pass()
        self.assertEqual(self.moves, [(7, AWAITING_APPROVAL)])
        self.assertEqual(self.labels(7), [AWAITING_APPROVAL])
        # the new label goes on before the old one comes off
        self.assertLess(self.reader.calls.index("add_label"), self.reader.calls.index("remove_label"))
        # an authorised person applies the approval label after the answer
        self.reader.issues[7]["labels"].append(epic.LABEL_APPROVE)
        self.reader.actors[7] = Actor("carol", "User", "9", T2)
        self.run_pass()
        self.assertEqual(self.moves[-1], (7, PLAN_APPROVED))
        self.assertEqual(self.labels(7), sorted([PLAN_APPROVED, epic.LABEL_APPROVE]))

    def test_nothing_is_approved_while_the_questions_are_unanswered(self):
        self.reader.scene(7, NEEDS_INFO, epic.LABEL_APPROVE, actor=Actor("carol", "User", "9", T2))
        self.run_pass()
        self.assertEqual(self.moves, [])
        self.assertEqual(self.reader.writes(), [])

    def test_a_reply_alone_never_approves(self):
        self.reader.scene(7, AWAITING_APPROVAL, replies=[reply(101, "carol")])
        self.run_pass()
        self.assertEqual(self.reader.writes(), [])
        self.assertNotIn("issue_replies", self.reader.calls)  # no approval label: not even a read


class UnauthorisedTests(GateCase):
    def test_replies_that_do_not_count_are_ignored(self):
        self.reader.scene(
            7, NEEDS_INFO,
            replies=[
                reply(101, "mallory"),  # read access
                reply(102, "bob"),  # triage
                reply(103, "stranger"),  # no access at all
                reply(104, "dependabot[bot]", "Bot"),  # a bot
                reply(105, "factory[bot]", "Bot"),  # Factory's own comment
                reply(106, "alice", created=T0),  # not after the plan comment's last edit
                reply(107, "carol", "Organization"),
            ],
        )
        self.run_pass()
        self.assertEqual(self.moves, [])
        self.assertEqual(self.reader.writes(), [])

    def test_factorys_own_login_is_never_an_answer_even_when_it_is_a_user_account(self):
        self.reader.scene(7, NEEDS_INFO, replies=[reply(101, "factory[bot]", "User")])
        self.reader.permissions["factory[bot]"] = "admin"
        self.run_pass()
        self.assertEqual(self.moves, [])

    def test_an_answer_that_pre_dates_a_re_plan_does_not_count(self):
        # the plan comment was edited at T0; an answer from before that belongs to the previous plan
        self.reader.scene(7, NEEDS_INFO, replies=[reply(101, "alice", created=T3)])
        self.run_pass()
        self.assertEqual(self.moves, [])

    def test_the_approval_of_someone_who_cannot_approve_changes_nothing(self):
        for actor in (Actor("bob", "User", "9", T2), Actor("mallory", "User", "9", T2), Actor("x[bot]", "Bot", "9", T2), None):
            with self.subTest(actor):
                self.seen.clear()
                self.reader.scene(8, AWAITING_APPROVAL, epic.LABEL_APPROVE, questions=0, actor=actor)
                self.run_pass()
                self.assertEqual(self.moves, [])
                self.assertEqual(self.reader.writes(), [])

    def test_a_later_unauthorised_reapply_fails_closed(self):
        # carol approved earlier, then a triager removed and re-applied the label: the newest applier counts
        self.reader.scene(8, AWAITING_APPROVAL, epic.LABEL_APPROVE, questions=0, actor=Actor("bob", "User", "11", T2))
        self.run_pass()
        self.assertEqual(self.moves, [])

    def test_a_removed_approval_does_not_count(self):
        # the actor is the newest applier ever, but the label is not on the issue now
        self.reader.scene(8, AWAITING_APPROVAL, questions=0, actor=Actor("carol", "User", "9", T2))
        self.run_pass()
        self.assertEqual(self.moves, [])
        self.assertNotIn("label_actor", self.reader.calls)

    def test_an_approval_older_than_the_plan_or_the_answers_is_not_current(self):
        before_plan = Actor("carol", "User", "9", T3)
        self.reader.scene(8, AWAITING_APPROVAL, epic.LABEL_APPROVE, questions=0, actor=before_plan)
        self.run_pass()
        self.assertEqual(self.moves, [])
        # a plan with questions: the approval must also follow the first authorised answer
        self.seen.clear()
        self.reader.scene(9, AWAITING_APPROVAL, epic.LABEL_APPROVE, replies=[reply(101, "alice", created=T2)],
                          actor=Actor("carol", "User", "9", T1))
        self.run_pass()
        self.assertEqual(self.moves, [])
        self.seen.clear()
        self.reader.scene(9, AWAITING_APPROVAL, epic.LABEL_APPROVE, replies=[reply(101, "alice", created=T1)],
                          actor=Actor("carol", "User", "9", T2))
        self.run_pass()
        self.assertEqual(self.moves, [(9, PLAN_APPROVED)])

    def test_a_plan_with_questions_that_nobody_answered_cannot_be_approved(self):
        self.reader.scene(9, AWAITING_APPROVAL, epic.LABEL_APPROVE, actor=Actor("carol", "User", "9", T2))
        self.run_pass()
        self.assertEqual(self.moves, [])

    def test_a_plan_whose_marker_cannot_be_read_still_needs_an_answer(self):
        self.reader.scene(9, AWAITING_APPROVAL, epic.LABEL_APPROVE, questions=None, actor=Actor("carol", "User", "9", T2))
        self.run_pass()
        self.assertEqual(self.moves, [])

    def test_a_plan_without_questions_is_approved_by_a_label_after_the_plan(self):
        self.reader.scene(8, AWAITING_APPROVAL, epic.LABEL_APPROVE, questions=0, actor=Actor("alice", "User", "9", T2))
        self.run_pass()
        self.assertEqual(self.moves, [(8, PLAN_APPROVED)])

    def test_an_undatable_event_is_not_current(self):
        for created in ("", "yesterday", "2027-01-01 00:00:30"):
            with self.subTest(created):
                self.seen.clear()
                self.reader.scene(8, AWAITING_APPROVAL, epic.LABEL_APPROVE, questions=0, actor=Actor("alice", "User", "9", created))
                self.run_pass()
                self.assertEqual(self.moves, [])


class FailClosedTests(GateCase):
    def test_a_failing_lookup_decides_nothing_and_writes_nothing(self):
        for method in ("plan_comment", "issue_replies", "permission"):
            with self.subTest(method):
                self.seen.clear()
                self.failures.clear()
                self.reader.errors = {method: subprocess.CalledProcessError(1, ["gh"], stderr=CANARY)}
                self.reader.scene(7, NEEDS_INFO, replies=[reply(101, "alice")])
                self.run_pass()
                self.assertEqual(self.failures, [(7, "CalledProcessError")])
                self.assertEqual(self.reader.writes(), [])
                self.assertEqual(self.moves, [])

    def test_a_failing_label_read_for_an_approval_writes_nothing(self):
        self.reader.errors = {"label_actor": RuntimeError(CANARY)}
        self.reader.scene(8, AWAITING_APPROVAL, epic.LABEL_APPROVE, questions=0)
        self.run_pass()
        self.assertEqual(self.failures, [(8, "RuntimeError")])
        self.assertEqual(self.reader.writes(), [])

    def test_a_failed_add_leaves_the_old_label_in_place(self):
        self.reader.errors = {"add_label": RuntimeError("x")}
        self.reader.scene(7, NEEDS_INFO, replies=[reply(101, "alice")])
        self.run_pass()
        self.assertEqual(self.labels(7), [NEEDS_INFO])
        self.assertNotIn("remove_label", self.reader.calls)

    def test_a_failed_issue_does_not_stop_the_others_and_is_read_again(self):
        self.reader.scene(7, NEEDS_INFO, replies=[reply(101, "alice")])
        self.reader.scene(8, NEEDS_INFO, replies=[reply(102, "alice")])
        self.reader.errors = {"plan_comment": RuntimeError("boom")}
        self.run_pass()
        self.assertEqual([n for n, _ in self.failures], [7, 8])
        self.reader.errors = {}
        self.run_pass()  # a failed issue was not marked as seen
        self.assertEqual([n for n, _ in self.moves], [7, 8])

    def test_a_missing_plan_comment_changes_nothing(self):
        self.reader.scene(7, NEEDS_INFO, plan=False, replies=[reply(101, "alice")])
        self.reader.scene(8, AWAITING_APPROVAL, epic.LABEL_APPROVE, plan=False, questions=0, actor=Actor("carol", "User", "9", T2))
        self.run_pass()
        self.assertEqual(self.moves, [])
        self.assertEqual(self.reader.writes(), [])

    def test_too_many_comments_or_commenters_without_an_authorised_one_is_unknown(self):
        self.reader.scene(7, NEEDS_INFO, replies=[reply(101, "mallory")], truncated=True)
        self.reader.scene(8, NEEDS_INFO, replies=[reply(200 + n, "user%d" % n) for n in range(trigger.MAX_REPLY_LOGINS + 1)])
        self.run_pass()
        self.assertEqual(self.moves, [])
        # an authorised answer among the first ones is enough, even when the list is truncated
        self.reader.scene(9, NEEDS_INFO, replies=[reply(300, "alice")], truncated=True)
        self.run_pass()
        self.assertEqual(self.moves, [(9, AWAITING_APPROVAL)])

    def test_drive_by_commenters_cannot_freeze_the_gate_before_an_authorised_answer(self):
        socks = [reply(200 + n, "sock%d" % n, association="NONE") for n in range(3 * trigger.MAX_REPLY_LOGINS)]
        self.reader.scene(7, NEEDS_INFO, replies=[*socks, reply(900, "alice", association="COLLABORATOR")])
        self.run_pass()
        self.assertEqual(self.moves, [(7, AWAITING_APPROVAL)])
        self.assertEqual(self.failures, [])
        lookups = self.reader.calls.count("permission")
        self.assertEqual(lookups, trigger.MAX_REPLY_LOGINS + 1)  # the sock accounts used up only their own bound
        for number, association in ((8, "OWNER"), (9, "MEMBER")):
            self.reader.scene(number, NEEDS_INFO, replies=[*socks, reply(901, "carol", association=association)])
            self.run_pass()
        self.assertEqual([n for n, _ in self.moves], [7, 8, 9])

    def test_an_authorised_answer_the_bound_hid_is_reported_not_silent(self):
        socks = [reply(200 + n, "sock%d" % n, association="NONE") for n in range(trigger.MAX_REPLY_LOGINS)]
        # GitHub did not call alice a member or collaborator (a private membership), and that reply comes after the bound
        self.reader.scene(7, NEEDS_INFO, replies=[*socks, reply(900, "alice", association="CONTRIBUTOR")])
        self.run_pass()
        self.assertEqual(self.moves, [])  # fails closed
        self.assertEqual(self.reader.writes(), [])
        self.assertEqual(self.failures, [(7, "Truncated")])
        self.run_pass()
        self.assertEqual(self.failures, [(7, "Truncated")])  # an unchanged issue is not read (and not reported) again
        # a truncated list is reported the same way
        self.reader.scene(8, NEEDS_INFO, replies=[reply(101, "mallory")], truncated=True)
        self.run_pass()
        self.assertEqual(self.failures, [(7, "Truncated"), (8, "Truncated")])

    def test_the_commenters_that_get_a_lookup_are_bounded_even_when_all_look_trusted(self):
        crowd = [reply(200 + n, "user%d" % n, association="MEMBER") for n in range(2 * trigger.MAX_TRUSTED_REPLY_LOGINS)]
        self.reader.scene(7, NEEDS_INFO, replies=crowd)
        self.run_pass()
        self.assertEqual(self.reader.calls.count("permission"), trigger.MAX_TRUSTED_REPLY_LOGINS)
        self.assertEqual(self.moves, [])
        self.assertEqual(self.failures, [(7, "Truncated")])

    def test_the_association_never_authorises_anyone(self):
        self.reader.scene(7, NEEDS_INFO, replies=[reply(101, "mallory", association="OWNER")])
        self.run_pass()
        self.assertEqual(self.moves, [])  # mallory only has read access, whatever GitHub calls the account
        self.assertEqual(self.failures, [])

    def test_the_ignore_label_and_a_missing_gate_label_are_left_alone(self):
        self.reader.scene(7, NEEDS_INFO, "factory-ignore", replies=[reply(101, "alice")])
        self.run_pass()
        self.assertEqual(self.moves, [])
        self.assertNotIn("plan_comment", self.reader.calls)

    def test_a_listing_failure_is_the_callers_to_report(self):
        self.reader.errors = {"open_issues": RuntimeError("down")}
        with self.assertRaises(RuntimeError):
            self.run_pass()


class RecheckBeforeWriteTests(GateCase):
    """The decision is made on a read that can take many calls: the write looks at the issue once more."""

    def approvable(self, number=7):
        self.reader.scene(number, AWAITING_APPROVAL, epic.LABEL_APPROVE, actor=Actor("carol", "User", "9", T2), replies=[reply(101, "alice")])

    def test_a_factory_ignore_added_during_the_read_stops_the_write(self):
        self.approvable()
        plain = self.reader.plan_comment

        def plan_comment(number):
            result = plain(number)
            self.reader.issues[number]["labels"].append("factory-ignore")  # an operator opts the issue out meanwhile
            return result

        self.reader.plan_comment = plan_comment
        self.run_pass()
        self.assertEqual((self.moves, self.reader.writes()), ([], []))
        self.assertIn("issue_state", self.reader.calls)

    def test_an_issue_closed_during_the_read_is_left_alone(self):
        self.approvable()
        plain = self.reader.plan_comment

        def plan_comment(number):
            result = plain(number)
            self.reader.issues[number]["state"] = "closed"
            return result

        self.reader.plan_comment = plan_comment
        self.run_pass()
        self.assertEqual((self.moves, self.reader.writes()), ([], []))

    def test_a_gate_label_that_changed_during_the_read_is_not_overwritten_from_a_stale_decision(self):
        self.reader.scene(7, NEEDS_INFO, replies=[reply(101, "alice")])
        plain = self.reader.issue_replies

        def issue_replies(number, after_id, since="", max_pages=10):
            result = plain(number, after_id, since, max_pages)
            self.reader.issues[number]["labels"] = [PLAN_APPROVED]  # somebody moved it already
            return result

        self.reader.issue_replies = issue_replies
        self.run_pass()
        self.assertEqual((self.moves, self.reader.writes()), ([], []))

    def test_the_stale_labels_taken_off_come_from_the_fresh_read(self):
        self.reader.scene(7, NEEDS_INFO, replies=[reply(101, "alice")])
        self.run_pass()
        self.assertEqual(self.moves, [(7, AWAITING_APPROVAL)])
        self.assertEqual(self.labels(7), [AWAITING_APPROVAL])

    def test_a_plan_comment_past_the_page_bound_is_reported_not_silently_ignored(self):
        self.reader.scene(7, NEEDS_INFO, replies=[reply(101, "alice")])
        self.reader.plans[7] = epic.PlanSearchTruncated()
        self.run_pass()
        self.assertEqual((self.moves, self.reader.writes()), ([], []))
        self.assertEqual(self.failures, [(7, "Truncated")])


class PacingTests(GateCase):
    def test_an_unchanged_issue_costs_nothing_until_it_changes_or_the_hour_passes(self):
        self.reader.scene(7, NEEDS_INFO)
        self.run_pass()
        first = len(self.reader.calls)
        self.run_pass()
        self.assertEqual(len(self.reader.calls), first + 2)  # only the two listings
        self.reader.issues[7]["updated"] = "2"
        self.run_pass()
        self.assertGreater(len(self.reader.calls), first + 4)
        count = len(self.reader.calls)
        self.run_pass(now=epic_gate.RESCAN_SECONDS * 2)  # an hour later: read again anyway
        self.assertGreater(len(self.reader.calls), count + 2)

    def test_at_most_a_bounded_number_of_issues_are_read_per_pass(self):
        for number in range(1, epic_gate.MAX_PER_PASS + 6):
            self.reader.scene(number, NEEDS_INFO)
        self.assertEqual(self.run_pass(), epic_gate.MAX_PER_PASS)
        self.assertEqual(self.run_pass(), 5)  # the rest on the next pass; the first ones are free
        self.assertEqual(self.run_pass(), 0)

    def test_the_allowed_check_is_asked_once_and_only_when_something_must_be_read(self):
        asked = []
        self.reader.scene(7, NEEDS_INFO)
        self.reader.scene(8, NEEDS_INFO)
        epic_gate.run_pass(self.reader, self.settings, print, print, self.seen, 0.0, allowed=lambda: asked.append(1) or False)
        self.assertEqual(asked, [1])
        self.assertNotIn("plan_comment", self.reader.calls)
        asked.clear()
        epic_gate.run_pass(self.reader, self.settings, print, print, {7: ("1", 0), 8: ("1", 0)}, 0.0, allowed=lambda: asked.append(1) or True)
        self.assertEqual(asked, [])

    def test_the_minimum_permission_comes_from_the_settings(self):
        self.reader.scene(7, NEEDS_INFO, replies=[reply(101, "alice")])
        self.run_pass(settings=EpicSettings(True, "admin"))
        self.assertEqual(self.moves, [])
        self.run_pass(now=epic_gate.RESCAN_SECONDS * 3, settings=EpicSettings(True, "write"))
        self.assertEqual(self.moves, [(7, AWAITING_APPROVAL)])

    def test_settings_follow_the_config_and_fail_to_the_safe_side(self):
        self.assertEqual(epic_gate.settings_of(RepoConfig(REPO), {}), EpicSettings(True, "write"))
        self.assertFalse(epic_gate.settings_of(RepoConfig(REPO, epic_mode=False), {}).enabled)
        self.assertFalse(epic_gate.settings_of(RepoConfig(REPO), {epic.ENV_SWITCH: "off"}).enabled)
        self.assertEqual(epic_gate.settings_of(SimpleNamespaceConfig(auto_build_min_permission="bogus"), {}).min_permission, "admin")


# --- the reader (the real methods over a stubbed gh) -------------------------------------------


class StubGh:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, argv, cwd, timeout=None, auth=None):
        self.calls.append(argv)
        return self.answers.pop(0) if self.answers else ""


class App:
    app_user_id = 4242


def rows(*items):
    return "\n".join(json.dumps(item) for item in items)


def plan_row(ident, uid, login, kind, plan="", match=True):
    return {"id": ident, "login": login, "type": kind, "uid": uid, "created": T3, "updated": T0, "match": match, "plan": plan}


def other_row(ident):
    """A comment of someone else: gh still prints one row for it, with ``match`` false (the filter is in Python)."""
    return plan_row(ident, 9, "someone", "User", match=False)


class ReaderTests(unittest.TestCase):
    def reader(self, *answers, auth=None):
        stub = StubGh(*answers)
        return GithubReader(REPO, auth, Path("."), run=stub), stub

    def test_plan_comment_is_the_lowest_id_of_factorys_own_with_its_question_count(self):
        line = epic.hidden(7, epic.validate(payload()))
        reader, stub = self.reader(
            rows(plan_row(500, 4242, "x", "Bot", line), plan_row(300, 4242, "x", "Bot", line), plan_row(200, 9, "p", "User", line)),
            auth=App(),
        )
        found = reader.plan_comment(7)
        self.assertEqual(found, PlanComment(300, "x", T3, T0, 2))
        argv = stub.calls[0]
        self.assertIn("issues/7/comments", " ".join(argv))
        self.assertIn(json.dumps(marker(7, "epic")), argv[argv.index("--jq") + 1])

    def test_the_plan_comment_is_found_on_a_later_page_of_comments_that_are_not_factorys(self):
        line = epic.hidden(7, epic.validate(payload()))
        first = rows(*(other_row(1000 + n) for n in range(trigger.PAGE_SIZE)))  # a page of other people's comments
        second = rows(plan_row(1100, 4242, "x", "Bot", line))
        reader, stub = self.reader(first, second, auth=App())
        found = reader.plan_comment(7)
        self.assertEqual((found.id, found.questions), (1100, 2))
        self.assertEqual(len(stub.calls), 2)  # a page that shows nothing of ours did not end the walk
        self.assertIn("page=2", " ".join(stub.calls[1]))

    def test_the_search_says_so_when_it_runs_out_of_pages_without_finding_the_plan_comment(self):
        full = rows(*(other_row(1000 + n) for n in range(trigger.PAGE_SIZE)))
        reader, stub = self.reader(*[full] * trigger.MAX_REMARK_PAGES, auth=App())
        with self.assertRaises(epic.PlanSearchTruncated):
            reader.plan_comment(7)
        self.assertEqual(len(stub.calls), trigger.MAX_REMARK_PAGES)
        # a plan comment found before the bound is reached is an answer, whatever follows
        line = epic.hidden(7, epic.validate(payload()))
        mixed = rows(plan_row(5, 4242, "x", "Bot", line), *(other_row(1000 + n) for n in range(trigger.PAGE_SIZE - 1)))
        reader, _ = self.reader(*[mixed] * trigger.MAX_REMARK_PAGES, auth=App())
        self.assertEqual(reader.plan_comment(7).id, 5)

    def test_issue_state_is_one_fresh_read_of_the_state_and_the_label_names(self):
        reader, stub = self.reader(json.dumps({"state": "OPEN", "labels": ["factory:awaiting-approval", "x"]}))
        self.assertEqual(reader.issue_state(7), ("open", ("factory:awaiting-approval", "x")))
        self.assertIn("issues/7", " ".join(stub.calls[0]))
        for bad in ('{"state": 3, "labels": []}', '{"state": "open", "labels": [1]}', "[]"):
            reader, _ = self.reader(bad)
            with self.assertRaises(ValueError):
                reader.issue_state(7)

    def test_a_comment_by_another_author_is_not_factorys(self):
        line = epic.hidden(7, epic.validate(payload()))
        forged = rows(plan_row(10, 777, "mallory", "User", line), plan_row(11, 4243, "other-app", "Bot", line))
        reader, _ = self.reader(forged, auth=App())
        self.assertIsNone(reader.plan_comment(7))

    def test_without_an_app_identity_the_ambient_login_decides(self):
        reader, stub = self.reader("factory-ci\n", rows(plan_row(10, 1, "Factory-CI", "User"), plan_row(9, 2, "mallory", "User")))
        found = reader.plan_comment(7)
        self.assertEqual((found.id, found.login, found.questions), (10, "Factory-CI", None))  # no readable marker: unknown
        reader, _ = self.reader("", rows(plan_row(10, 1, "Factory-CI", "User")))
        self.assertIsNone(reader.plan_comment(7))  # no identity at all: nobody is found

    def test_a_marker_that_does_not_validate_is_an_unknown_question_count(self):
        reader, _ = self.reader(rows(plan_row(10, 4242, "x", "Bot", "<!-- factory-epic-plan:v1 AAAA -->")), auth=App())
        self.assertIsNone(reader.plan_comment(7).questions)

    def test_issue_replies_keeps_ids_above_the_plan_comment_and_reports_truncation(self):
        listing = rows(
            {"id": 90, "login": "a", "type": "User", "created": T1, "updated": T1},
            {"id": 120, "login": "alice", "type": "User", "created": T1, "updated": T1},
            {"id": "x", "login": "bad", "type": "User", "created": T1, "updated": T1},
        )
        reader, stub = self.reader(listing)
        found, truncated = reader.issue_replies(7, 100, since=T0)
        self.assertEqual([(r.id, r.login, r.is_bot) for r in found], [(120, "alice", False)])
        self.assertFalse(truncated)
        self.assertIn("since=2027-01-01T00%3A00%3A10Z", " ".join(stub.calls[0]))
        self.assertIn("author_association", stub.calls[0][-1])  # only the association label, never a text
        full = rows(*({"id": 200 + n, "login": "a", "type": "User", "created": T1, "updated": T1} for n in range(trigger.PAGE_SIZE)))
        reader, _ = self.reader(*[full] * 3)
        self.assertEqual(reader.issue_replies(7, 100, max_pages=3)[1], True)

    def test_the_association_is_kept_as_a_string_and_is_empty_otherwise(self):
        row = {"id": 120, "login": "alice", "type": "User", "created": T1, "updated": T1}
        listing = rows(
            {**row, "assoc": "COLLABORATOR"},
            {**row, "id": 121, "assoc": 7},
            {**row, "id": 122},
            {**row, "id": 123, "assoc": "NONE"},
        )
        reader, _ = self.reader(listing)
        found, _ = reader.issue_replies(7, 100)
        self.assertEqual([(r.association, r.trusted) for r in found], [("COLLABORATOR", True), ("", False), ("", False), ("NONE", False)])

    def test_no_text_is_ever_projected(self):
        reader, stub = self.reader("", "")
        reader.issue_replies(7, 1)
        reader, plan_stub = self.reader("x", "")
        reader.plan_comment(7)
        self.assertNotIn(".body", stub.calls[0][-1])  # replies: ids, logins, types and times only
        # the plan comment's own marker line leaves gh, nothing else of the body does: the body is bound once
        # to a variable, the filter and the marker line are derived from it, and no output key carries it
        jq = plan_stub.calls[-1][-1]
        self.assertEqual(jq.count(".body"), 1)
        self.assertNotIn("body:", jq)
        self.assertIn("startswith(\"<!-- factory-epic-plan:v1 \")", jq)

    def test_writes_go_through_the_labels_api_and_the_cli(self):
        reader, stub = self.reader()
        reader.add_label(7, "factory:awaiting-approval")
        reader.ensure_label("factory:awaiting-approval", "c2e0c6", "d")
        self.assertEqual(stub.calls[0][:4], ["gh", "api", "-X", "POST"])
        self.assertIn("labels[]=factory:awaiting-approval", stub.calls[0])
        self.assertEqual(stub.calls[1][:5], ["gh", "label", "create", "factory:awaiting-approval", "--force"])
        self.assertEqual(stub.calls[1][-2:], ["-R", REPO])

    def test_label_actor_reports_when_the_label_was_applied(self):
        event = {"id": 5, "event": "labeled", "label": epic.LABEL_APPROVE, "login": "carol", "type": "User", "created": T2}
        reader, stub = self.reader(rows(event))
        self.assertEqual(reader.label_actor(7, epic.LABEL_APPROVE), Actor("carol", "User", "5", T2))
        self.assertIn("created_at", stub.calls[0][-1])

    def test_the_actors_of_several_labels_carry_when_each_was_applied(self):
        first = {"id": 5, "event": "labeled", "label": epic.LABEL_APPROVE, "login": "carol", "type": "User", "created": T2}
        second = {"id": 6, "event": "labeled", "label": "bug", "login": "dave", "type": "User", "created": T3}
        reader, stub = self.reader(rows(first, second))
        found = reader.label_actors(7, [epic.LABEL_APPROVE, "bug"])
        self.assertEqual(found, {epic.LABEL_APPROVE: Actor("carol", "User", "5", T2), "bug": Actor("dave", "User", "6", T3)})
        self.assertEqual(len(stub.calls), 1)
        self.assertIn("created_at", stub.calls[0][-1])


# --- the manager pass and the trigger ------------------------------------------------------------


class GateTriggerReader(GateReader, FakeReader):
    """The label trigger's FakeReader and the gate's in-memory reads on one repository (one issue store)."""

    def __init__(self, repo=REPO):
        FakeReader.__init__(self, repo)
        GateReader.__init__(self)

    def scene(self, number, *labels, **kw):
        GateReader.scene(self, number, *labels, **kw)
        self.issues[number].update(actor=("alice", "User"), event="1")

    def open_issues(self, label):
        return FakeReader.open_issues(self, label)  # records "issues", honours ``fail``

    def label_actor(self, number, label):
        if label == epic.LABEL_APPROVE:
            self.calls.append("label_actor")
            return self.actors.get(number)
        return FakeReader.label_actor(self, number, label)

    def permission(self, login):
        return FakeReader.permission(self, login)

    def remove_label(self, number, label):
        FakeReader.remove_label(self, number, label)
        self.calls.append("remove_label")


class EpicPassCase(TriggerCase):
    def setUp(self):
        super().setUp()
        self.manager.env.pop(epic.ENV_SWITCH, None)
        self.readers[REPO] = self.reader = GateTriggerReader(REPO)
        self.ops = self.new_ops(self.manager, github=lambda cfg: self.readers[cfg.repo])

    def epic_lines(self):
        return [line for line in self.lines() if line.startswith("epic ")]

    def gate_calls(self):
        return [c for c in self.reader.calls if c in ("plan_comment", "issue_replies", "ensure_label", "add_label", "remove_label")]


class OperationsTests(EpicPassCase):
    def test_an_authorised_answer_moves_the_label_and_logs_one_fixed_line(self):
        self.reader.scene(7, NEEDS_INFO, replies=[reply(101, "alice")])
        self.ops.trigger_pass()
        self.assertEqual(self.epic_lines(), [f"epic repo={REPO} issue=7 state=awaiting-approval"])
        self.assertEqual(self.reader.issues[7]["labels"], [AWAITING_APPROVAL])

    def test_the_pass_is_on_by_default_and_off_by_the_repository_the_switch_or_the_opt_out(self):
        for extra, env in (("epic_mode = false\n", None), ("", "off"), ("auto_build = false\n", None)):
            with self.subTest(extra=extra, env=env):
                self.reader.calls.clear()
                self.configure(extra)
                self.manager.env.pop(epic.ENV_SWITCH, None)
                if env:
                    self.manager.env[epic.ENV_SWITCH] = env
                self.reader.scene(7, NEEDS_INFO, replies=[reply(101, "alice")])
                self.ops.trigger_pass()
                if "auto_build" in extra:
                    self.assertEqual(self.reader.calls, [])  # an opted-out repository is not read at all
                self.assertEqual(self.gate_calls(), [])
                self.assertEqual(self.epic_lines(), [])
                self.assertEqual(self.reader.issues[7]["labels"], [NEEDS_INFO])
        self.configure("")
        self.manager.env.pop(epic.ENV_SWITCH, None)
        self.ops.trigger_pass()
        self.assertEqual(self.reader.issues[7]["labels"], [AWAITING_APPROVAL])  # and on again by default

    def test_a_repository_with_no_gate_label_needs_no_further_read(self):
        self.reader.label(9, "bug")
        self.ops.trigger_pass()
        self.assertEqual(self.gate_calls(), [])
        self.assertEqual(self.epic_lines(), [])

    def test_a_failing_pass_is_one_line_with_the_type_then_a_backoff(self):
        self.reader.fail = RuntimeError(CANARY)
        self.reader.scene(7, NEEDS_INFO)
        self.ops.trigger_pass()
        self.assertEqual(self.epic_lines(), [f"epic repo={REPO} pass failed error=RuntimeError backoff=60s"])
        self.ops.trigger_pass()
        self.assertEqual(len(self.epic_lines()), 1)  # backing off: no second attempt this cycle

    def test_a_failing_issue_is_one_rate_limited_line_without_text(self):
        self.reader.errors = {"plan_comment": RuntimeError(CANARY)}
        self.reader.scene(7, NEEDS_INFO)
        self.ops.trigger_pass()
        self.assertEqual(self.epic_lines(), [f"epic repo={REPO} issue=7 failed error=RuntimeError"])

    def test_a_read_that_stopped_at_a_bound_is_one_fixed_line_without_a_login(self):
        socks = [reply(200 + n, "sock%d" % n) for n in range(trigger.MAX_REPLY_LOGINS + 1)]
        self.reader.scene(7, NEEDS_INFO, replies=socks)
        self.ops.trigger_pass()
        self.assertEqual(self.epic_lines(), [f"epic repo={REPO} issue=7 truncated"])
        self.assertEqual(self.reader.issues[7]["labels"], [NEEDS_INFO])

    def test_an_archived_repository_is_not_written_to(self):
        self.reader.facts = trigger.RepoFacts(archived=True, private=True)
        self.reader.scene(7, NEEDS_INFO, replies=[reply(101, "alice")])
        self.ops.trigger_pass()
        self.assertEqual(self.reader.issues[7]["labels"], [NEEDS_INFO])
        self.assertEqual(self.gate_calls(), [])

    def test_a_paused_trigger_runs_no_gate_pass(self):
        self.reader.scene(7, NEEDS_INFO, replies=[reply(101, "alice")])
        with patch.object(self.manager, "trigger_paused", return_value=True):
            self.ops.trigger_pass()
        self.assertEqual(self.reader.issues[7]["labels"], [NEEDS_INFO])

    def test_the_pass_reads_no_run_directory(self):
        # the gate works from GitHub alone: no run directory exists here at all
        self.assertFalse((self.data_dir / "repos" / REPO / "runs").exists() and any((self.data_dir / "repos" / REPO / "runs").iterdir()))
        self.reader.scene(7, NEEDS_INFO, replies=[reply(101, "alice")])
        self.ops.trigger_pass()
        self.assertEqual(self.reader.issues[7]["labels"], [AWAITING_APPROVAL])


class ReplanTests(EpicPassCase):
    def write_epic_run(self, run, number):
        directory = self.run_dir(run)
        directory.mkdir(parents=True, exist_ok=True)
        plan = {
            "version": 1,
            "issues": [{"number": number, "title": CANARY, "body": CANARY}],
            "jobs": [],
            "dispositions": [{"issue": number, "status": "epic", "reason": CANARY}],
        }
        (directory / "plan.json").write_text(json.dumps(plan))
        (directory / "state.json").write_text("{}")

    def test_the_waiting_labels_and_the_approved_one_are_retryable_and_a_building_one_is_in_flight(self):
        facts = lambda *labels: IssueFacts(12, (LABEL, *labels))  # noqa: E731
        self.assertEqual(trigger.classify(facts(NEEDS_INFO), LABEL), "retry")
        self.assertEqual(trigger.classify(facts(AWAITING_APPROVAL), LABEL), "retry")
        self.assertEqual(trigger.classify(facts(NEEDS_INFO, AWAITING_APPROVAL), LABEL), "retry")
        # an approved epic is a request for a new plan only while no phase is in flight (operations._epic_in_flight asks, see test_epic_start)
        self.assertEqual(trigger.classify(facts(PLAN_APPROVED), LABEL), "retry")
        self.assertEqual(trigger.classify(facts(NEEDS_INFO, "factory:building"), LABEL), "in-flight")

    def test_re_applying_the_label_on_an_epic_plans_it_afresh_without_a_retry(self):
        self.write_epic_run("auto-20270301T000000Z", 12)
        self.reader.label(12, LABEL, NEEDS_INFO)
        self.ops.trigger_pass()
        self.assertEqual(self.retries(), [])
        self.assertEqual(self.spawned("run"), [])
        self.assertEqual(self.spawned("plan")[-1][-1], "--issues=12")

    def test_an_approved_epic_with_the_trigger_label_is_left_alone(self):
        self.write_epic_run("auto-20270301T000000Z", 12)
        self.reader.label(12, LABEL, PLAN_APPROVED)
        self.ops.trigger_pass()
        self.assertEqual(self.spawned("plan"), [])
        self.assertEqual(self.retries(), [])

    def retries(self):
        return [argv for argv in self.runner.argvs if "retry" in argv]


if __name__ == "__main__":
    unittest.main()

"""The label-trigger loop of the manager's operations (docs/manager.md, "Label-triggered builds").

Same fakes as tests/test_operations.py (a scripted CLI runner, a recording spawner, scripted
leases, a clock and a wait that never sleep) plus a fake GitHub reader that keeps issues, their
labels, who labelled them and what each user may do. Every test drives single passes
(``trigger_pass``) directly; the thread tests use real events with long intervals.

The base class asserts after every test that the canary, planted in plan files and in the
streams of failing commands, never reached the manager's stdout or stderr.
"""

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from factory import epic, hold_comments, issue_comments, metrics, observed_issues, plan_failure, plan_holds, planner
from factory import manager as mgr
from factory import operations as ops
from factory import trigger
from factory.github_auth import AmbientAuth, AppAuth
from factory.issue_labels import DISPOSITION_LABELS, IGNORE_LABEL, LABEL_PLANNED
from factory.manager import Manager, Settings
from factory.operations import Operations, github_reader
from factory.trigger import Actor, IssueFacts, RepoFacts
from tests.test_operations import CANARY, NOW, OpsCase, failure
from tests.test_operations import text as ops_text
from tests.test_operations import report as ops_report
from tests.test_trigger import FAKE_GH

REPO = "org/name"
LABEL = "factory-build"
# Ids, counts, run names and fixed words: no quote, no bracket, nothing a title or a body could be made of.
SAFE_LINE = re.compile(r"^trigger repo=[A-Za-z0-9._/-]+( [a-z-]+=[A-Za-z0-9._/:-]+| [a-z-]+)+$")


class FakeReader:
    """One repository's GitHub: issues (labels, who labelled, last update) and user permissions."""

    def __init__(self, repo=REPO):
        self.repo = repo
        self.facts = RepoFacts(archived=False, private=True)
        self.issues = {}
        self.permissions = {"alice": "write", "carol": "admin", "bob": "triage", "mallory": "read"}
        self.calls = []
        self.removed = []
        self.fail = None
        self.remove_error = None
        self.gate = None  # (entered, release) events: open_issues blocks on them
        self.pulls = {}  # pull request number -> "open" | "closed" | "merged"
        self.app_user_id = 4242  # Factory's own bot user: the author of the comments below
        self.comments = {}  # issue -> [{"id", "body", "user"}]: the conversations ``call`` reads and writes
        self.gh_calls = []  # the comment calls (``call`` and ``issue_state``), apart from ``calls``
        self.comment_error = None  # raised by every comment write
        self.next_comment = 9000

    def label(self, number, *labels, actor=("alice", "User"), updated="1", event=None):
        """Issue ``number`` as it is now; ``event`` is the id of the ``labeled`` event (a new label has a new one)."""
        event = updated if event is None else event
        self.issues[number] = {"labels": list(labels), "actor": actor, "updated": updated, "event": event}

    def labels_of(self, number):
        return self.issues[number]["labels"]

    def kinds(self):
        return [call if isinstance(call, str) else call[0] for call in self.calls]

    def repo_facts(self):
        self.calls.append("repo")
        return self.facts

    def open_issues(self, label):
        self.calls.append("issues")
        if self.gate is not None:
            entered, release = self.gate
            entered.set()
            release.wait(30)
        if self.fail is not None:
            raise self.fail
        return [
            IssueFacts(number, tuple(item["labels"]), item["updated"])
            for number, item in sorted(self.issues.items())
            if label.casefold() in {name.casefold() for name in item["labels"]}
        ]

    def label_actor(self, number, label):
        self.calls.append(("actor", number))
        actor = self.issues[number]["actor"]
        return Actor(*actor, event=self.issues[number]["event"]) if actor else None

    def permission(self, login):
        self.calls.append(("permission", login))
        return self.permissions.get(login, "none")

    def issue_state(self, number):
        self.gh_calls.append(("state", number))
        item = self.issues[number]
        return item.get("state", "open"), tuple(item["labels"])

    def issue_facts(self, number):
        self.gh_calls.append(("facts", number))
        item = self.issues[number]
        return IssueFacts(number, tuple(item["labels"]), item["updated"])

    def put_comment(self, number, body, user=None):
        self.next_comment += 1
        mine = {"type": "Bot", "id": self.app_user_id, "login": "factory[bot]"}
        self.comments.setdefault(number, []).append({"id": self.next_comment, "body": body, "user": user or mine})

    def mine(self, number):
        return [c["body"] for c in self.comments.get(number, []) if c["user"].get("id") == self.app_user_id]

    def writes(self):
        return [call for call in self.gh_calls if call[0] in ("POST", "PATCH")]

    def call(self, args):
        """The ``gh api`` calls of ``hold_comments``: list an issue's comments by marker and author, post, edit."""
        self.gh_calls.append(("gh", *args))
        path = next(arg for arg in args if arg.startswith("repos/"))
        listing = re.fullmatch(rf"repos/{self.repo}/issues/(\d+)/comments", path)
        editing = re.fullmatch(rf"repos/{self.repo}/issues/comments/(\d+)", path)
        body = next((arg[5:] for arg in args if arg.startswith("body=")), None)
        if listing and "--paginate" in args:
            wanted = re.search(r'startswith\("(.*?)"\)', args[args.index("--jq") + 1])[1]
            bot = int(re.search(r"\.user\.id == (\d+)", args[args.index("--jq") + 1])[1])
            return "\n".join(
                json.dumps({"id": c["id"], "body": c["body"]})
                for c in self.comments.get(int(listing[1]), [])
                if c["user"].get("id") == bot and c["body"].startswith(wanted)
            )
        if self.comment_error is not None:
            raise self.comment_error
        if listing and args[1:3] == ["-X", "POST"]:
            self.gh_calls.append(("POST", int(listing[1])))
            self.put_comment(int(listing[1]), body)
            if int(listing[1]) in self.issues:  # a comment is an update of the issue, as on GitHub
                self.issues[int(listing[1])]["updated"] += "c"
            return str(self.next_comment)
        if editing and args[1:3] == ["-X", "PATCH"]:
            self.gh_calls.append(("PATCH", int(editing[1])))
            for rows in self.comments.values():
                for item in rows:
                    if item["id"] == int(editing[1]):
                        item["body"] = body
            return ""
        raise AssertionError(f"fake gh: unhandled {args}")

    def pull_state(self, number):
        self.calls.append(("pull", number))
        if self.fail is not None:
            raise self.fail
        return self.pulls[number]

    def remove_label(self, number, label):
        self.calls.append(("remove", number))
        if self.remove_error is not None:
            raise self.remove_error
        self.removed.append(number)
        names = self.issues[number]["labels"]
        self.issues[number]["labels"] = [name for name in names if name.casefold() != label.casefold()]


class TriggerCase(OpsCase):
    def setUp(self):
        super().setUp()
        self.readers = {REPO: FakeReader(REPO), "org/other": FakeReader("org/other")}
        self.reader = self.readers[REPO]
        # These cases pin what the label trigger itself reads; the epic gate pass (default on) has its own
        # cases in EpicGateTests, which switch it back on.
        self.manager.env[epic.ENV_SWITCH] = "off"
        self.ops = self.new_ops(self.manager, github=lambda cfg: self.readers[cfg.repo])

    # -- configuration ---------------------------------------------------------------

    def configure(self, extra="", repo=REPO):
        path = self.config_dir / "repos" / f"{repo.replace('/', '__')}.toml"
        path.write_text(f'repo = "{repo}"\n{extra}')

    # -- driving the fake processes ----------------------------------------------------

    def trigger_lines(self):
        return [line for line in self.lines() if line.startswith("trigger ")]

    def spawned(self, kind=None):
        """argv of the plan/run children started so far, filtered by the subcommand."""
        argvs = self.spawner.argvs
        return [argv for argv in argvs if kind is None or kind in argv]

    def run_of(self, argv):
        return Path(argv[argv.index("--run-dir") + 1]).name

    def write_plan(self, run, numbers, repo=REPO, state=False, mark=False):
        """A run directory with a plan; ``mark`` is the trigger loop's own marker (what ``_plan`` leaves)."""
        directory = self.run_dir(run, repo)
        directory.mkdir(parents=True, exist_ok=True)
        if mark:
            (directory / ops.TRIGGER_MARKER).write_text("2027-01-01T00:00:00Z")
        plan = {
            "version": 1,
            "issues": [{"number": n, "title": CANARY, "body": CANARY} for n in numbers],
            "jobs": [{"id": f"job-{n}", "issues": [n], "objective": CANARY} for n in numbers],
            # a real plan gives every issue it selects a disposition, a job-owned one included
            "dispositions": [{"issue": n, "status": "job", "job": f"job-{n}", "reason": CANARY} for n in numbers],
        }
        (directory / "plan.json").write_text(json.dumps(plan))
        if state:
            (directory / "state.json").write_text("{}")
        return directory

    def finish(self, argv, code=0, numbers=None):
        """The child started with ``argv`` exits with ``code``; a successful plan has written its plan.json."""
        process = self.spawner.processes[self.spawner.argvs.index(argv)]
        if code == 0 and "plan" in argv and numbers is not None:
            self.write_plan(self.run_of(argv), numbers)
        process.returncode = code

    def plan_and_finish(self, numbers):
        self.ops.trigger_pass()
        argv = self.spawned("plan")[-1]
        self.finish(argv, 0, numbers)
        return argv

    def start_build(self, numbers):
        """Plan ``numbers`` (already labelled), let the next pass start the run, and let the run begin its state."""
        plan = self.plan_and_finish(numbers)
        self.ops.trigger_pass()
        run = self.spawned("run")[-1]
        (self.run_dir(self.run_of(run)) / "state.json").write_text("{}")  # what `run` writes first
        return plan, run


# --- the happy path ------------------------------------------------------------------


class LifecycleTests(TriggerCase):
    def test_a_label_from_an_authorised_user_plans_then_runs_exactly_once(self):
        self.reader.label(7, LABEL, "bug")
        self.ops.trigger_pass()
        plans = self.spawned("plan")
        self.assertEqual(len(plans), 1)
        expected_run = trigger.run_name(NOW, lambda name: False)
        self.assertEqual(
            plans[0],
            ["fake-cli", "--repo", REPO, "--run-dir", str(self.run_dir(expected_run)), "plan", "--timeout", "1800", "--issues=7"],
        )
        self.assertEqual(self.trigger_lines(), [f"trigger repo={REPO} issues=1 run={expected_run} planning"])
        self.assertEqual(self.reader.removed, [])  # consumed only after the plan succeeded
        self.assertEqual(self.spawned("run"), [])

        self.ops.trigger_pass()  # the plan is still running: nothing else starts
        self.assertEqual((len(self.spawner.calls), self.reader.removed), (1, []))

        self.finish(plans[0], 0, [7])
        self.ops.trigger_pass()
        self.assertEqual(self.reader.removed, [7])
        self.assertEqual(self.reader.labels_of(7), ["bug"])
        runs = self.spawned("run")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0][:5], ["fake-cli", "--repo", REPO, "--run-dir", str(self.run_dir(expected_run))])
        self.assertEqual(runs[0][5:], ["run", "--concurrency", "1", "--timeout", "1800"])
        self.assertEqual(
            self.trigger_lines()[-1], f"trigger repo={REPO} issues=1 run={expected_run} started"
        )

        self.ops.trigger_pass()  # the run is going
        self.assertEqual(len(self.spawner.calls), 2)

    def test_after_the_run_the_status_label_keeps_the_issue_from_being_built_again(self):
        self.reader.label(7, LABEL)
        plan, run = self.start_build([7])
        self.reader.label(7, "factory:planned")  # what the plan and run put on the issue
        for process in self.spawner.processes:
            process.returncode = 0
        self.ops.trigger_pass()
        self.assertEqual(len(self.spawner.calls), 2)
        # Someone adds the trigger label again while the issue is in flight: ignored, one line.
        self.reader.label(7, LABEL, "factory:building")
        self.ops.trigger_pass()
        self.ops.trigger_pass()
        self.assertEqual(len(self.spawner.calls), 2)
        self.assertEqual(
            [line for line in self.trigger_lines() if "skipped" in line],
            [f"trigger repo={REPO} issue=7 skipped reason=in-flight"],
        )

    def test_to_rebuild_the_label_is_added_again_once_the_issue_has_no_status_label(self):
        self.reader.label(7, LABEL)
        self.start_build([7])
        for process in self.spawner.processes:
            process.returncode = 0
        self.reader.label(7, LABEL, updated="2")  # status labels removed by hand, label added again
        self.ops.trigger_pass()
        self.assertEqual(len(self.spawned("plan")), 2)
        self.assertEqual(len({self.run_of(argv) for argv in self.spawned("plan")}), 2)

    def test_the_repository_configured_concurrency_is_used_for_the_run(self):
        self.configure("auto_build_concurrency = 4\n")
        self.reader.label(7, LABEL)
        _, run = self.start_build([7])
        self.assertEqual(run[-4:], ["--concurrency", "4", "--timeout", "1800"])

    def test_the_repository_configured_session_timeout_is_used_for_the_plan_and_the_run(self):
        self.configure("auto_build_timeout_seconds = 5400\n")
        self.reader.label(7, LABEL)
        plan, run = self.start_build([7])
        self.assertEqual(plan[plan.index("--timeout") + 1], "5400")
        self.assertEqual(run[run.index("--timeout") + 1], "5400")

    def test_an_out_of_range_session_timeout_means_the_default_and_the_config_still_loads(self):
        self.configure("auto_build_timeout_seconds = 86401\n")
        self.reader.label(7, LABEL)
        plan, run = self.start_build([7])
        self.assertEqual(plan[plan.index("--timeout") + 1], "1800")
        self.assertEqual(run[run.index("--timeout") + 1], "1800")

    def test_a_custom_label_name_is_the_trigger(self):
        self.configure('auto_build_label = "ship it"\n')
        self.reader.label(7, LABEL)
        self.reader.label(8, "Ship It")
        self.ops.trigger_pass()
        self.assertEqual(self.spawned("plan")[-1][-1], "--issues=8")

    def test_other_repositories_are_independent_and_each_gets_its_own_plan(self):
        self.reader.label(7, LABEL)
        self.readers["org/other"].label(3, LABEL)
        self.ops.trigger_pass()
        plans = self.spawned("plan")
        self.assertEqual(sorted(argv[argv.index("--repo") + 1] for argv in plans), ["org/name", "org/other"])
        self.assertEqual(len(self.trigger_lines()), 2)

    def test_the_run_goes_through_the_managers_own_start_so_the_registry_knows_it(self):
        self.reader.label(7, LABEL)
        self.ops.trigger_pass()
        child = self.manager.children.active_for_repo(REPO)
        self.assertEqual((child.kind, child.repo), ("plan", REPO))
        self.assertEqual(child.run, self.run_of(self.spawned("plan")[-1]))
        self.finish(self.spawned("plan")[-1], 0, [7])
        self.ops.trigger_pass()
        child = self.manager.children.active_for_repo(REPO)
        self.assertEqual(child.kind, "run")
        # the cost guardrail sees it like any other run
        self.assertIn(child, self.manager.children.running())


# --- which issues, and who may ask -----------------------------------------------------


class EligibilityTests(TriggerCase):
    def assertNothingStarted(self):
        self.assertEqual(self.spawner.calls, [])
        self.assertEqual(self.reader.removed, [])

    def test_an_unlabelled_issue_is_never_touched(self):
        self.reader.label(7, "bug")
        self.ops.trigger_pass()
        self.assertNothingStarted()
        self.assertEqual(self.trigger_lines(), [])

    def test_a_user_below_the_minimum_is_ignored_and_the_label_stays(self):
        for number, actor in ((1, "bob"), (2, "mallory"), (3, "stranger")):
            self.reader.label(number, LABEL, actor=(actor, "User"))
        self.ops.trigger_pass()
        self.assertNothingStarted()
        self.assertEqual(
            self.trigger_lines(),
            [f"trigger repo={REPO} issue={n} skipped reason=unauthorised" for n in (1, 2, 3)],
        )
        for number in (1, 2, 3):
            self.assertIn(LABEL, self.reader.labels_of(number))

    def test_a_refusal_is_logged_once_and_not_looked_up_again_until_the_issue_changes(self):
        self.reader.label(1, LABEL, actor=("bob", "User"), updated="a")
        self.ops.trigger_pass()
        before = list(self.reader.calls)
        for _ in range(3):
            self.ops.trigger_pass()
        self.assertEqual(len(self.trigger_lines()), 1)
        self.assertEqual([c for c in self.reader.calls[len(before):] if c != "repo" and c != "issues"], [])
        # a maintainer re-applies the label: the issue is updated, so it is looked at again
        self.reader.label(1, LABEL, actor=("carol", "User"), updated="b")
        self.ops.trigger_pass()
        self.assertEqual(self.spawned("plan")[-1][-1], "--issues=1")

    def test_a_bot_is_ignored_even_with_admin_rights(self):
        self.reader.permissions["dependabot[bot]"] = "admin"
        self.reader.permissions["a-bot"] = "admin"
        self.reader.label(1, LABEL, actor=("dependabot[bot]", "Bot"))
        self.reader.label(2, LABEL, actor=("a-bot", "Bot"))
        self.reader.label(3, LABEL, actor=("an-org", "Organization"))
        self.ops.trigger_pass()
        self.assertNothingStarted()
        self.assertEqual(len(self.trigger_lines()), 3)
        self.assertNotIn(("permission", "dependabot[bot]"), self.reader.calls)  # no permission lookup for a bot

    def test_a_label_with_no_event_is_unauthorised(self):
        self.reader.label(1, LABEL, actor=None)
        self.ops.trigger_pass()
        self.assertNothingStarted()
        self.assertEqual(self.trigger_lines(), [f"trigger repo={REPO} issue=1 skipped reason=unauthorised"])

    def test_the_minimum_is_configurable_upwards_and_for_private_repositories_downwards(self):
        self.reader.label(1, LABEL, actor=("alice", "User"))  # write
        self.configure('auto_build_min_permission = "maintain"\n')
        self.ops.trigger_pass()
        self.assertNothingStarted()
        self.configure('auto_build_min_permission = "triage"\n')  # private repository: allowed
        self.reader.label(1, LABEL, actor=("alice", "User"), updated="2")  # (a refusal is remembered until the issue changes)
        self.reader.label(2, LABEL, actor=("bob", "User"), updated="2")
        self.ops.trigger_pass()
        self.assertEqual(self.spawned("plan")[-1][-1], "--issues=1,2")

    def test_a_lowered_minimum_is_refused_for_a_repository_that_is_not_private(self):
        self.reader.facts = RepoFacts(archived=False, private=False)
        self.configure('auto_build_min_permission = "triage"\n')
        self.reader.label(1, LABEL, actor=("bob", "User"))
        self.ops.trigger_pass()
        self.assertNothingStarted()
        self.assertEqual(self.trigger_lines(), [f"trigger repo={REPO} skipped reason=config"])
        self.assertEqual(self.reader.kinds(), ["repo"])  # nothing was read about issues or users

    def test_the_visibility_is_checked_at_every_poll_not_only_when_the_config_loads(self):
        self.configure('auto_build_min_permission = "triage"\n')
        self.reader.label(1, LABEL, actor=("bob", "User"))
        self.ops.trigger_pass()  # private: allowed
        plan = self.spawned("plan")[-1]
        self.assertEqual(plan[-1], "--issues=1")
        self.finish(plan, 1)  # that plan fails, so the label stays
        self.ops.trigger_pass()
        self.time.now += 100
        self.reader.facts = RepoFacts(archived=False, private=False)  # the repository was made public meanwhile
        self.reader.label(2, LABEL, actor=("bob", "User"), updated="2")
        self.ops.trigger_pass()
        self.assertEqual(len(self.spawned("plan")), 1)
        self.assertEqual(self.trigger_lines()[-1], f"trigger repo={REPO} skipped reason=config")

    def test_write_and_above_work_on_a_public_repository(self):
        self.reader.facts = RepoFacts(archived=False, private=False)
        self.reader.label(1, LABEL, actor=("alice", "User"))
        self.ops.trigger_pass()
        self.assertEqual(self.spawned("plan")[-1][-1], "--issues=1")

    def test_factory_ignore_always_wins(self):
        self.reader.label(1, LABEL, "factory-ignore")
        self.reader.label(2, LABEL, "Factory-Ignore", "factory:blocked")
        self.ops.trigger_pass()
        self.assertNothingStarted()
        self.assertEqual(
            self.trigger_lines(),
            [f"trigger repo={REPO} issue={n} skipped reason=ignored" for n in (1, 2)],
        )
        self.assertNotIn(("actor", 1), self.reader.calls)  # no lookup for an issue that is ignored

    def test_any_status_label_means_in_flight(self):
        for number, status in enumerate(("planned", "building", "in-review", "pr-open", "merged"), start=1):
            self.reader.label(number, LABEL, f"factory:{status}")
        self.ops.trigger_pass()
        self.assertNothingStarted()
        self.assertEqual(
            [line for line in self.trigger_lines()],
            [f"trigger repo={REPO} issue={n} skipped reason=in-flight" for n in range(1, 6)],
        )

    def test_a_repeated_skip_is_one_line_per_hour(self):
        self.reader.label(1, LABEL, "factory-ignore")
        for _ in range(5):
            self.ops.trigger_pass()
        self.assertEqual(len(self.trigger_lines()), 1)
        self.time.now += 3600
        self.ops.trigger_pass()
        self.assertEqual(len(self.trigger_lines()), 2)

    def test_the_issue_cap_takes_the_lowest_numbers_and_the_rest_wait(self):
        self.configure("auto_build_max_issues = 3\n")
        for number in (9, 4, 12, 1, 7):
            self.reader.label(number, LABEL)
        plan, run = self.start_build([1, 4, 7])
        self.assertEqual(plan[-1], "--issues=1,4,7")
        self.assertEqual(self.reader.removed, [1, 4, 7])
        self.assertEqual(sorted(n for n in self.reader.issues if LABEL in self.reader.labels_of(n)), [9, 12])
        # the next batch starts after the run ended, once the first issues carry their status labels
        for process in self.spawner.processes:
            process.returncode = 0
        for number in (1, 4, 7):
            self.reader.label(number, "factory:pr-open")
        self.ops.trigger_pass()
        self.assertEqual(self.spawned("plan")[-1][-1], "--issues=9,12")

    def test_archived_repositories_are_skipped(self):
        self.reader.facts = RepoFacts(archived=True, private=True)
        self.reader.label(1, LABEL)
        self.ops.trigger_pass()
        self.assertNothingStarted()
        self.assertEqual(self.trigger_lines(), [f"trigger repo={REPO} skipped reason=archived"])
        self.assertEqual(self.reader.kinds(), ["repo"])

    def test_auto_build_false_opts_out_without_a_single_github_call(self):
        self.configure("auto_build = false\n")
        self.reader.label(1, LABEL)
        for _ in range(3):
            self.ops.trigger_pass()
        self.assertEqual(self.reader.calls, [])
        self.assertEqual(self.spawner.calls, [])
        self.assertEqual(self.trigger_lines(), [f"trigger repo={REPO} skipped reason=disabled"])  # said once, ever
        self.time.now += 10**7
        self.ops.trigger_pass()
        self.assertEqual(len(self.trigger_lines()), 1)
        self.configure("")  # the config is read again on every pass: back to watching
        self.ops.trigger_pass()
        self.assertEqual(self.spawned("plan")[-1][-1], "--issues=1")

    def test_a_repository_whose_config_does_not_load_is_skipped_with_one_warning(self):
        self.configure('auto_build = "yes"\n')
        self.reader.label(1, LABEL)
        self.ops.trigger_pass()
        self.ops.trigger_pass()
        self.assertEqual(self.spawner.calls, [])
        self.assertEqual(len([w for w in self.warnings() if "does not load" in w]), 1)


# --- busy repositories -------------------------------------------------------------------


class BusyTests(TriggerCase):
    def test_a_registered_process_keeps_the_trigger_from_reading_anything(self):
        for kind in ("plan", "run"):
            self.add_child(REPO, "x", kind)
            self.reader.label(1, LABEL)
            self.ops.trigger_pass()
            self.assertEqual((self.reader.calls, self.spawner.calls), ([], []))
            self.manager.children.active_for_repo(REPO).process.returncode = 0
        self.assertEqual(self.trigger_lines(), [f"trigger repo={REPO} skipped reason=busy"])  # said once

    def test_a_busy_lease_waits_for_the_next_cycle_without_reading_issues(self):
        self.script_lease(REPO, "busy", "busy", then="free")
        self.reader.label(1, LABEL)
        self.ops.trigger_pass()
        self.ops.trigger_pass()
        self.assertEqual((self.reader.calls, self.spawner.calls), ([], []))
        self.assertEqual(self.trigger_lines(), [f"trigger repo={REPO} skipped reason=busy"])
        self.ops.trigger_pass()
        self.assertEqual(self.spawned("plan")[-1][-1], "--issues=1")

    def test_a_lease_that_becomes_busy_between_the_probe_and_the_start_is_just_busy(self):
        self.script_lease(REPO, "free", "busy", then="free")  # our probe, then the manager's own
        self.reader.label(1, LABEL)
        self.ops.trigger_pass()
        self.assertEqual(self.spawner.calls, [])
        self.assertEqual(self.trigger_lines(), [f"trigger repo={REPO} skipped reason=busy"])
        self.ops.trigger_pass()
        self.assertEqual(len(self.spawned("plan")), 1)

    def test_an_unavailable_lease_backend_is_not_a_start(self):
        self.script_lease(REPO, "unavailable", then="free")
        self.reader.label(1, LABEL)
        self.ops.trigger_pass()
        self.assertEqual((self.reader.calls, self.spawner.calls), ([], []))

    def test_a_planned_run_is_not_run_while_a_supervisor_is_registered_or_the_lease_is_busy(self):
        self.write_plan("auto-20270101T000000Z", [5], mark=True)
        self.reader.label(5, LABEL, "factory:planned")
        self.add_child(REPO, "somebody-elses", "run")
        self.ops.trigger_pass()
        self.assertEqual(self.spawner.calls, [])
        self.manager.children.active_for_repo(REPO).process.returncode = 0
        self.script_lease(REPO, "busy", then="free")
        self.ops.trigger_pass()
        self.assertEqual(self.spawner.calls, [])
        self.assertEqual(self.trigger_lines()[-1], f"trigger repo={REPO} skipped reason=busy")
        self.ops.trigger_pass()
        self.assertEqual(len(self.spawned("run")), 1)

    def test_concurrent_cycles_start_one_plan(self):
        entered, release = threading.Event(), threading.Event()
        self.reader.gate = (entered, release)
        self.reader.label(1, LABEL)
        first = threading.Thread(target=self.ops.trigger_pass)
        first.start()
        self.assertTrue(entered.wait(10))
        second = threading.Thread(target=self.ops.trigger_pass)  # a pass is at work: this one returns at once
        second.start()
        second.join(10)
        self.assertFalse(second.is_alive())
        self.assertEqual(self.spawner.calls, [])
        release.set()
        first.join(10)
        self.assertEqual(len(self.spawned("plan")), 1)
        self.ops.trigger_pass()
        self.assertEqual(len(self.spawner.calls), 1)  # the plan is running

    def test_two_operations_objects_still_start_one_plan_because_the_manager_refuses_the_second(self):
        other = self.new_ops(self.manager, github=lambda cfg: self.readers[cfg.repo])
        self.reader.label(1, LABEL)
        self.ops.trigger_pass()
        other.trigger_pass()
        self.assertEqual(len(self.spawned("plan")), 1)
        self.assertEqual(self.trigger_lines()[-1], f"trigger repo={REPO} skipped reason=busy")


# --- failure, backoff and restarts ---------------------------------------------------------


class FailureTests(TriggerCase):
    def fail_plan(self, code=1):
        self.ops.trigger_pass()
        argv = self.spawned("plan")[-1]
        self.finish(argv, code)
        return argv

    def test_a_failed_plan_keeps_the_label_logs_one_line_and_backs_off(self):
        self.reader.label(7, LABEL)
        argv = self.fail_plan(3)
        self.ops.trigger_pass()
        run = self.run_of(argv)
        self.assertEqual(self.trigger_lines()[-1], f"trigger repo={REPO} run={run} plan failed rc=3 code=other backoff=60s")
        self.assertEqual(self.reader.removed, [])
        self.assertIn(LABEL, self.reader.labels_of(7))
        self.assertEqual(len(self.spawned("plan")), 1)
        self.ops.trigger_pass()
        self.assertEqual(self.trigger_lines()[-1], f"trigger repo={REPO} skipped reason=backoff")
        self.assertEqual(len(self.trigger_lines()), 3)

    def test_the_line_carries_the_code_the_plan_recorded(self):
        for code in plan_failure.CODES:
            with self.subTest(code=code):
                self.time.now += 4000  # past any backoff
                self.reader.label(7, LABEL)
                self.ops.trigger_pass()
                argv = self.spawned("plan")[-1]
                plan_failure.write(self.run_dir(self.run_of(argv)), code)
                self.finish(argv, 3)
                self.ops.trigger_pass()
                self.assertRegex(
                    self.trigger_lines()[-1],
                    rf"^trigger repo={REPO} run={self.run_of(argv)} plan failed rc=3 code={code} backoff=\d+s$",
                )

    def test_a_missing_unreadable_or_unknown_code_file_reads_as_other(self):
        hostile = f"schema_invalid {CANARY}"
        cases = {
            "missing": None,
            "unknown": "made_up_code",
            "hostile text": hostile,
            "case differs": "TIMEOUT",
            "empty": "",
            "not text": b"\xff\xfe\x00",
            "too long": "timeout" + " " * 200 + CANARY,
            "unreadable": "directory",
            "dangling link": "symlink",
        }
        for name, content in cases.items():
            with self.subTest(case=name):
                self.time.now += 4000
                self.reader.label(7, LABEL, updated=name)  # an edit each time: the same code twice would hold the set
                self.ops.trigger_pass()
                argv = self.spawned("plan")[-1]
                directory = self.run_dir(self.run_of(argv))
                directory.mkdir(parents=True, exist_ok=True)
                path = directory / plan_failure.FILE
                if content == "directory":
                    path.mkdir()  # reading it fails
                elif content == "symlink":
                    path.symlink_to(directory / "nowhere")
                elif isinstance(content, bytes):
                    path.write_bytes(content)
                elif content is not None:
                    path.write_text(content)
                self.finish(argv, 1)
                self.ops.trigger_pass()
                line = self.trigger_lines()[-1]
                self.assertRegex(line, r" plan failed rc=1 code=other backoff=\d+s$")
                self.assertNotIn(CANARY, line)

    def test_hostile_issue_text_in_a_failing_plan_never_reaches_the_code_file_or_the_line(self):
        self.reader.label(7, LABEL)
        self.ops.trigger_pass()
        argv = self.spawned("plan")[-1]
        directory = self.run_dir(self.run_of(argv))
        directory.mkdir(parents=True, exist_ok=True)
        issues = [{"number": 7, "title": CANARY, "body": CANARY, "comments": [], "labels": [], "url": ""}]
        (directory / "issues.json").write_text(json.dumps(issues))
        (directory / "open-prs.json").write_text("[]")
        document = {
            "version": 1,
            "jobs": [{"id": "fix-a", "issues": [7], "context_issues": [], "objective": CANARY, "likely_paths": [f"../{CANARY}"],
                      "acceptance": [CANARY], "depends_on": [], "feature": False}],
            "dispositions": [{"issue": 7, "status": "job", "reason": CANARY, "job": "fix-a"}],
        }
        with patch("factory.planner.run_agent", return_value=(document, {"phase": "plan"})), patch(
            "factory.planner.repo_guidance", return_value=""
        ), self.assertRaises(ValueError):
            planner.plan(directory, directory, use_snapshot=True)
        self.assertEqual(plan_failure.read(directory), plan_failure.INVALID_PLAN_PATHS)
        self.assertEqual((directory / plan_failure.FILE).read_text(), "invalid_plan_paths\n")
        with patch("factory.planner.run_agent", side_effect=RuntimeError(CANARY)), patch(
            "factory.planner.repo_guidance", return_value=""
        ), self.assertRaises(RuntimeError):
            planner.plan(directory, directory, use_snapshot=True)
        self.assertEqual((directory / plan_failure.FILE).read_text(), "planner_error\n")
        self.finish(argv, 1)
        self.ops.trigger_pass()
        self.assertEqual(
            self.trigger_lines()[-1], f"trigger repo={REPO} run={self.run_of(argv)} plan failed rc=1 code=planner_error backoff=60s"
        )
        self.assertNotIn(CANARY, "\n".join(self.trigger_lines()))

    def test_the_backoff_doubles_up_to_an_hour_and_the_label_is_tried_again_each_time(self):
        self.reader.label(7, LABEL)
        delays = []
        for attempt in range(9):
            self.reader.label(7, LABEL, updated=str(attempt))  # an edit each time: the same code twice would hold the set
            self.fail_plan(1)
            self.ops.trigger_pass()
            delays.append(int(re.search(r"backoff=(\d+)s", self.trigger_lines()[-1]).group(1)))
            self.time.now += delays[-1] - 1
            self.ops.trigger_pass()
            self.assertEqual(len(self.spawned("plan")), attempt + 1, "still backing off")
            self.time.now += 1
        self.assertEqual(delays, [60, 120, 240, 480, 960, 1920, 3600, 3600, 3600])
        self.reader.label(7, LABEL, updated="last")
        self.ops.trigger_pass()
        self.assertEqual(len(self.spawned("plan")), 10)
        self.assertEqual(len({self.run_of(argv) for argv in self.spawned("plan")}), 10)  # a new run name each time

    def test_a_success_resets_the_backoff(self):
        self.reader.label(7, LABEL)
        self.fail_plan(1)
        self.ops.trigger_pass()
        self.time.now += 61
        plan, _ = self.start_build([7])
        self.assertEqual(self.reader.removed, [7])
        for process in self.spawner.processes:
            process.returncode = 0
        self.reader.label(8, LABEL, updated="2")
        self.fail_plan(1)
        self.ops.trigger_pass()
        self.assertTrue(self.trigger_lines()[-1].endswith("plan failed rc=1 code=other backoff=60s"))  # not 120

    def test_a_plan_that_exits_zero_without_a_plan_file_is_a_failure(self):
        self.reader.label(7, LABEL)
        self.fail_plan(0)
        self.ops.trigger_pass()
        self.assertIn("plan failed rc=0 code=other backoff=60s", self.trigger_lines()[-1])
        self.assertEqual(self.reader.removed, [])

    def test_a_plan_killed_by_a_signal_is_a_failure_too(self):
        self.reader.label(7, LABEL)
        self.fail_plan(-9)
        self.ops.trigger_pass()
        self.assertIn("plan failed rc=-9", self.trigger_lines()[-1])

    def test_a_plan_that_cannot_be_started_backs_off(self):
        def refuse(argv, log_path):
            raise OSError(5, CANARY)

        self.manager.spawner = refuse
        self.reader.label(7, LABEL)
        self.ops.trigger_pass()
        self.assertEqual(
            self.trigger_lines(), [f"trigger repo={REPO} run={self.expected_run()} plan start refused status=500 backoff=60s"]
        )

    def expected_run(self):
        return trigger.run_name(self.time.now, lambda name: False)

    def test_a_github_failure_is_one_line_with_the_exception_type_and_a_backoff(self):
        self.reader.label(7, LABEL)
        self.reader.fail = subprocess.CalledProcessError(1, ["gh"], stderr=CANARY)
        self.ops.trigger_pass()
        self.assertEqual(self.trigger_lines(), [f"trigger repo={REPO} pass failed error=CalledProcessError backoff=60s"])
        self.ops.trigger_pass()
        self.assertEqual(self.trigger_lines()[-1], f"trigger repo={REPO} skipped reason=backoff")
        self.time.now += 60
        self.reader.fail = None
        self.ops.trigger_pass()
        self.assertEqual(len(self.spawned("plan")), 1)

    def test_one_repository_failing_does_not_stop_the_others(self):
        self.reader.fail = RuntimeError(CANARY)
        self.readers["org/other"].label(3, LABEL)
        self.ops.trigger_pass()
        self.assertEqual([argv[argv.index("--repo") + 1] for argv in self.spawned("plan")], ["org/other"])

    def test_a_label_that_cannot_be_removed_is_one_line_and_the_run_still_starts(self):
        self.reader.label(7, LABEL)
        self.reader.remove_error = subprocess.CalledProcessError(1, ["gh"], stderr=CANARY)
        _, run = self.start_build([7])
        self.assertIn(f"trigger repo={REPO} label-remove failed error=CalledProcessError", self.trigger_lines())
        self.assertEqual(len(self.spawned("run")), 1)
        self.assertEqual(self.reader.removed, [])
        # the issue is in flight now, so the leftover label is ignored
        self.reader.label(7, LABEL, "factory:planned")
        for process in self.spawner.processes:
            process.returncode = 0
        self.ops.trigger_pass()
        self.assertEqual(len(self.spawned("plan")), 1)

    def test_a_run_that_is_refused_is_tried_again_and_the_labels_are_not_removed_twice(self):
        self.reader.label(7, LABEL)
        plan = self.plan_and_finish([7])
        self.script_lease(REPO, "busy", then="free")  # the manager's own probe at the start of the run
        self.ops.trigger_pass()
        self.assertEqual((self.reader.removed, self.spawned("run")), ([7], []))
        self.assertEqual(self.trigger_lines()[-1], f"trigger repo={REPO} skipped reason=busy")
        self.ops.trigger_pass()
        self.assertEqual(len(self.spawned("run")), 1)
        self.assertEqual(self.reader.removed, [7])

    def test_a_run_that_ends_without_ever_writing_its_state_backs_off_and_is_started_again(self):
        self.reader.label(7, LABEL)
        self.plan_and_finish([7])
        self.ops.trigger_pass()
        first = self.spawned("run")[-1]
        self.finish(first, 1)  # no state.json: it never got going
        self.ops.trigger_pass()
        self.assertEqual(
            self.trigger_lines()[-1], f"trigger repo={REPO} run={self.run_of(first)} run failed rc=1 backoff=60s"
        )
        self.ops.trigger_pass()
        self.assertEqual(self.trigger_lines()[-1], f"trigger repo={REPO} skipped reason=backoff")
        self.assertEqual(len(self.spawned("run")), 1)
        self.time.now += 60
        self.ops.trigger_pass()
        self.assertEqual(len(self.spawned("run")), 2)
        self.assertEqual(self.reader.removed, [7])  # the label was consumed once

    def test_a_run_that_ends_after_writing_its_state_is_no_failure_whatever_its_exit_code(self):
        self.reader.label(7, LABEL)
        _, run = self.start_build([7])
        self.finish(run, 2)  # jobs that need a human make `run` exit 2
        self.reader.label(7, "factory:blocked")
        self.ops.trigger_pass()
        self.assertFalse([line for line in self.trigger_lines() if "failed" in line])
        self.assertEqual(len(self.spawner.calls), 2)

    def test_a_pending_run_is_finished_after_a_restart(self):
        self.reader.label(7, LABEL, "factory:planned")  # the plan labelled it before the manager stopped
        self.write_plan("auto-20270101T000000Z", [7], mark=True)
        manager = self.new_manager()
        fresh = self.new_ops(manager, github=lambda cfg: self.readers[cfg.repo])
        fresh.trigger_pass()
        self.assertEqual(self.reader.removed, [7])
        runs = [argv for argv in self.spawner.argvs if "run" in argv]
        self.assertEqual(len(runs), 1)
        self.assertEqual(self.run_of(runs[0]), "auto-20270101T000000Z")
        self.assertEqual(self.trigger_lines()[-1], f"trigger repo={REPO} issues=1 run=auto-20270101T000000Z started")

    def test_a_plan_the_restart_cut_short_is_planned_again_under_a_new_name(self):
        self.reader.label(7, LABEL)
        self.ops.trigger_pass()
        cut = self.spawned("plan")[-1]
        self.assertFalse((self.run_dir(self.run_of(cut)) / "plan.json").exists())
        manager = self.new_manager()  # a restart: nothing is remembered
        fresh = self.new_ops(manager, github=lambda cfg: self.readers[cfg.repo])
        self.time.now += 5
        fresh.trigger_pass()
        plans = self.spawned("plan")
        self.assertEqual(len(plans), 2)
        self.assertNotEqual(self.run_of(plans[0]), self.run_of(plans[1]))
        self.assertEqual(plans[1][-1], "--issues=7")

    def test_a_restart_after_the_run_started_does_nothing_more(self):
        self.reader.label(7, LABEL)
        self.start_build([7])
        self.reader.label(7, "factory:building")
        run_dir = self.run_dir(self.run_of(self.spawned("run")[-1]))
        (run_dir / "state.json").write_text("{}")  # what the run's first write does
        manager = self.new_manager()
        fresh = self.new_ops(manager, github=lambda cfg: self.readers[cfg.repo])
        before = len(self.spawner.calls)
        fresh.trigger_pass()
        self.assertEqual(len(self.spawner.calls), before)

    def test_only_runs_the_trigger_planned_are_finished_by_the_trigger(self):
        self.write_plan("r1", [7])  # planned by hand, never run: not the trigger's business
        self.write_plan("auto-hold", [8])  # a hand-made run that borrows the prefix: no marker, not ours
        self.ops.trigger_pass()
        self.assertEqual(self.spawner.calls, [])
        self.assertEqual(self.reader.removed, [])

    def test_the_run_a_plan_start_leaves_is_marked(self):
        self.reader.label(7, LABEL)
        self.ops.trigger_pass()
        run = self.run_of(self.spawned("plan")[-1])
        self.assertTrue((self.run_dir(run) / ops.TRIGGER_MARKER).is_file())

    def test_a_plan_whose_start_is_refused_leaves_no_run_directory_behind(self):
        self.reader.label(7, LABEL)
        self.script_lease(REPO, "free", "busy", then="free")
        self.ops.trigger_pass()
        self.assertEqual(self.spawner.calls, [])
        self.assertFalse(self.run_dir(self.expected_run()).exists())

    def test_a_pending_run_is_checked_against_the_repository_like_a_new_plan(self):
        self.write_plan("auto-20270101T000000Z", [5], mark=True)
        self.reader.facts = RepoFacts(archived=True, private=False)
        self.ops.trigger_pass()
        self.assertEqual(self.spawner.calls, [])
        self.assertEqual(self.trigger_lines(), [f"trigger repo={REPO} skipped reason=archived"])
        self.reader.facts = RepoFacts(archived=False, private=False)
        self.configure('auto_build_min_permission = "triage"\n')  # not allowed on a public repository
        self.ops.trigger_pass()
        self.assertEqual(self.spawner.calls, [])
        self.assertEqual(self.trigger_lines()[-1], f"trigger repo={REPO} skipped reason=config")
        self.reader.facts = RepoFacts(archived=False, private=True)
        self.ops.trigger_pass()
        self.assertEqual(len(self.spawned("run")), 1)


# --- retries -----------------------------------------------------------------------------


class PlanHoldTests(TriggerCase):
    """An issue set whose plan fails twice with the same code is held until a label or an edit changes (plan_holds.py)."""

    HOLD_LINE = re.compile(rf"^trigger repo={REPO} issues=[0-9,]+ held code=[a-z_]+ failures=[0-9]+$")

    def hold_file(self):
        return self.data_dir / "repos" / REPO / "plan-holds.json"

    def attempt(self, code=plan_failure.SCHEMA_INVALID):
        """One pass that may start a plan; a plan it started fails with ``code``, and the pass after it sees that."""
        before = len(self.spawned("plan"))
        self.time.now += 4000  # past any backoff
        self.ops.trigger_pass()
        started = len(self.spawned("plan")) - before
        if started:
            argv = self.spawned("plan")[-1]
            plan_failure.write(self.run_dir(self.run_of(argv)), code)
            self.finish(argv, 3)
            self.ops.trigger_pass()
        return started

    def hold_lines(self):
        return [line for line in self.trigger_lines() if " held " in line]

    def test_a_plan_that_fails_twice_the_same_way_is_planned_twice_then_held_with_one_line(self):
        self.reader.label(7, LABEL)
        self.reader.label(8, LABEL)
        self.assertEqual([self.attempt(), self.attempt()], [1, 1])
        self.assertEqual(self.hold_lines(), [])
        for _ in range(5):
            self.assertEqual(self.attempt(), 0)
        self.assertEqual(len(self.spawned("plan")), 2)
        self.assertEqual(self.hold_lines(), [f"trigger repo={REPO} issues=7,8 held code=schema_invalid failures=2"])
        self.assertRegex(self.hold_lines()[0], self.HOLD_LINE)
        self.assertEqual(json.loads(self.hold_file().read_text())["sets"]["7,8"]["count"], 2)

    def test_the_hold_line_shows_once_even_after_other_work_started(self):
        self.reader.label(7, LABEL)
        self.attempt(), self.attempt(), self.attempt()
        self.reader.label(9, LABEL, "factory-ignore")
        self.attempt()
        self.assertEqual(len(self.hold_lines()), 1)

    def test_a_held_set_does_not_keep_other_issues_from_being_planned(self):
        self.configure("auto_build_max_issues = 1\n")
        self.reader.label(7, LABEL)
        self.attempt(), self.attempt()
        self.reader.label(9, LABEL)
        self.assertEqual(self.attempt(), 1)
        self.assertEqual(self.spawned("plan")[-1][-1], "--issues=9")

    def test_a_label_change_allows_exactly_one_more_attempt_and_the_set_is_held_again(self):
        self.reader.label(7, LABEL)
        self.attempt(), self.attempt()
        self.assertEqual(self.attempt(), 0)
        self.reader.label(7, LABEL, "bug", updated="2")
        self.assertEqual(self.attempt(), 1)
        self.assertEqual([self.attempt() for _ in range(3)], [0, 0, 0])
        self.assertEqual(len(self.spawned("plan")), 3)
        self.assertEqual(len(self.hold_lines()), 2)
        self.assertIn("failures=3", self.hold_lines()[-1])

    def test_an_edit_that_changes_only_updated_at_allows_one_more_attempt(self):
        self.reader.label(7, LABEL)
        self.attempt(), self.attempt()
        self.reader.label(7, LABEL, updated="2", event="1")
        self.assertEqual([self.attempt(), self.attempt()], [1, 0])

    def test_the_label_order_is_not_a_change(self):
        self.reader.label(7, LABEL, "bug", "docs")
        self.attempt(), self.attempt()
        self.reader.label(7, "docs", "bug", LABEL, updated=self.reader.issues[7]["updated"])  # the hold's own comment moved it
        self.assertEqual(self.attempt(), 0)

    def test_a_new_manager_on_the_same_data_directory_keeps_the_hold(self):
        self.reader.label(7, LABEL)
        self.attempt(), self.attempt()
        self.ops = self.new_ops(self.manager, github=lambda cfg: self.readers[cfg.repo])
        self.assertEqual(self.attempt(), 0)
        self.assertEqual(len(self.hold_lines()), 1)
        self.reader.label(7, LABEL, updated="2")
        self.assertEqual(self.attempt(), 1)

    def test_the_file_holds_numbers_codes_counts_and_fingerprints_only(self):
        self.reader.label(7, LABEL, f"secret-{CANARY}")
        self.attempt(), self.attempt()
        raw = self.hold_file().read_text()
        self.assertNotIn(CANARY, raw)
        entry = json.loads(raw)["sets"]["7"]
        self.assertEqual(sorted(entry), ["code", "count", "fingerprint"])
        self.assertIn(entry["code"], plan_failure.CODES)
        self.assertEqual([p.name for p in self.hold_file().parent.iterdir() if p.name.startswith(".")], [])

    def test_a_different_code_resets_the_count(self):
        self.reader.label(7, LABEL)
        self.assertEqual(self.attempt(plan_failure.TIMEOUT), 1)
        self.assertEqual(self.attempt(plan_failure.SCHEMA_INVALID), 1)
        self.assertEqual(self.attempt(plan_failure.TIMEOUT), 1)
        self.assertEqual(self.hold_lines(), [])
        self.assertEqual(self.attempt(plan_failure.TIMEOUT), 1)
        self.assertEqual(self.attempt(), 0)
        self.assertEqual(len(self.hold_lines()), 1)

    def test_a_successful_plan_clears_the_entry(self):
        self.reader.label(7, LABEL)
        self.attempt()
        self.assertIn("7", json.loads(self.hold_file().read_text())["sets"])
        self.time.now += 4000
        self.plan_and_finish([7])
        self.ops.trigger_pass()
        self.assertEqual(json.loads(self.hold_file().read_text())["sets"], {})
        self.assertEqual(self.attempt(), 0)  # the run is in progress now; nothing was held
        self.assertEqual(self.hold_lines(), [])

    def hold_everything(self, count):
        """``count`` labelled issues, planned (and failed) until every set is held."""
        for number in range(1, count + 1):
            self.reader.label(number, LABEL)
        while self.attempt():
            pass

    def idle_reads(self):
        """The reader calls of one more idle pass, as ``(label_actor + permission calls, all calls)``."""
        self.reader.calls.clear()
        self.time.now += 4000
        self.ops.trigger_pass()
        reads = [call for call in self.reader.calls if isinstance(call, tuple) and call[0] in ("actor", "permission")]
        return len(reads), len(self.reader.calls)

    def test_an_idle_pass_over_held_issues_costs_no_authorisation_reads_however_many_there_are(self):
        self.hold_everything(30)
        before = len(self.spawned("plan"))
        self.assertGreater(before, 0)
        self.assertEqual(len(self.hold_lines()), 10)  # 30 issues, the default 3 to a set
        self.assertEqual(self.idle_reads()[0], 0)
        self.assertEqual(len(self.spawned("plan")), before)
        self.reader.label(31, LABEL)  # a new issue is the only one that is read
        reads, total = self.idle_reads()
        self.assertEqual(reads, 2)
        self.assertEqual(len(self.spawned("plan")), before + 1)
        self.assertLessEqual(total, 4)

    def test_a_held_issue_is_not_authorised_again_but_a_change_releases_its_set(self):
        self.reader.label(7, LABEL)
        self.reader.label(8, LABEL)
        self.attempt(), self.attempt()
        self.assertEqual(self.idle_reads()[0], 0)
        self.reader.label(8, LABEL, updated="2")
        self.assertEqual(self.attempt(), 1)
        self.assertEqual(self.spawned("plan")[-1][-1], "--issues=7,8")

    def test_deleting_the_file_releases_the_holds(self):
        self.reader.label(7, LABEL)
        self.attempt(), self.attempt()
        self.assertEqual(self.attempt(), 0)
        self.hold_file().unlink()
        self.assertEqual(self.attempt(), 1)

    def test_a_bad_file_is_one_line_and_not_a_crash_or_a_loop(self):
        cases = {
            "not json": b"this is not json",
            "truncated": b'{"version": 1, "sets": {"7": {"code": "sche',
            "binary": b"\xff\xfe\x00\x01",
            "empty": b"",
            "wrong shape": b"[1, 2, 3]",
            "bad code": json.dumps({"version": 1, "sets": {"7": {"code": CANARY, "count": 5, "fingerprint": "ab"}}}).encode(),
            "oversized": b" " * (plan_holds.MAX_BYTES + 1),
        }
        for name, content in cases.items():
            with self.subTest(name=name):
                self.setUp()
                self.reader.label(7, LABEL)
                self.hold_file().parent.mkdir(parents=True, exist_ok=True)
                self.hold_file().write_bytes(content)
                self.assertEqual([self.attempt(), self.attempt(), self.attempt()], [1, 1, 0])
                unusable = [line for line in self.trigger_lines() if "plan holds unusable" in line]
                self.assertEqual(len(unusable), 1)
                self.assertNotIn(CANARY, self.out.getvalue() + self.err.getvalue())

    def test_a_directory_where_the_file_should_be_is_one_line_and_the_hold_still_works_from_memory(self):
        self.reader.label(7, LABEL)
        self.hold_file().mkdir(parents=True)
        self.assertEqual([self.attempt(), self.attempt(), self.attempt(), self.attempt()], [1, 1, 0, 0])
        self.assertEqual(len([line for line in self.trigger_lines() if "plan holds unusable" in line]), 1)
        self.assertEqual(len(self.hold_lines()), 1)

    def test_a_fifo_where_the_file_should_be_neither_blocks_the_loop_nor_holds_anything(self):
        self.reader.label(7, LABEL)
        self.hold_file().parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(self.hold_file())
        entries, problem = plan_holds._read(self.hold_file())  # returns at once: no writer is ever needed
        self.assertEqual((entries, problem), ({}, plan_holds.UNREADABLE))

    def test_a_file_with_one_good_and_one_bad_entry_keeps_the_good_one_and_says_corrupt(self):
        good = {"code": "other", "count": 2, "fingerprint": "a" * 32}
        raw = json.dumps({"version": 1, "sets": {"7": good, "8": {"code": "not-a-code", "count": 2, "fingerprint": "b"}, "x": good}})
        entries, problem = plan_holds.parse(raw.encode())
        self.assertEqual(list(entries), ["7"])
        self.assertEqual(problem, plan_holds.CORRUPT)

    def test_a_symlink_where_the_file_should_be_is_refused(self):
        self.hold_file().parent.mkdir(parents=True, exist_ok=True)
        target = self.hold_file().parent / "elsewhere.json"
        target.write_text(json.dumps({"version": 1, "sets": {"7": {"code": "other", "count": 2, "fingerprint": "a" * 32}}}))
        self.hold_file().symlink_to(target)
        self.assertEqual(plan_holds._read(self.hold_file()), ({}, plan_holds.UNREADABLE))

    def test_more_entries_than_the_cap_are_not_kept(self):
        entry = {"code": "other", "count": 2, "fingerprint": "a" * 32}
        raw = json.dumps({"version": 1, "sets": {str(n): entry for n in range(1, plan_holds.MAX_ENTRIES + 6)}})
        entries, problem = plan_holds.parse(raw.encode())
        self.assertEqual(len(entries), plan_holds.MAX_ENTRIES)
        self.assertEqual(problem, plan_holds.CORRUPT)

    def test_a_file_that_cannot_be_written_is_said_and_the_hold_still_works_from_memory(self):
        self.reader.label(7, LABEL)
        real = os.open

        def refuse(path, *args, **kwargs):
            if Path(path).name.startswith(".plan-holds"):
                raise PermissionError("read-only")
            return real(path, *args, **kwargs)

        with patch("factory.plan_holds.os.open", side_effect=refuse):
            self.assertEqual([self.attempt(), self.attempt(), self.attempt()], [1, 1, 0])
        self.assertTrue([line for line in self.trigger_lines() if "plan holds not saved" in line])
        self.assertFalse(self.hold_file().exists())


class HoldCommentTests(TriggerCase):
    """When a hold starts each issue of the set gets ONE fixed-text comment (hold_comments.py), best effort."""

    attempt = PlanHoldTests.attempt
    hold_lines = PlanHoldTests.hold_lines

    def two_issues(self):
        self.reader.label(7, LABEL)
        self.reader.label(8, LABEL)

    def test_the_comment_comes_when_the_hold_starts_one_per_issue_and_never_again(self):
        self.two_issues()
        self.assertEqual(self.attempt(), 1)
        self.assertEqual(self.reader.writes(), [])  # one failure is not a hold
        self.assertEqual(self.attempt(), 1)
        self.assertEqual(self.reader.writes(), [("POST", 7), ("POST", 8)])
        for number in (7, 8):
            self.assertEqual(self.reader.mine(number), [issue_comments.render_held(number, plan_failure.SCHEMA_INVALID)])
        for _ in range(4):  # the comments moved updated_at, yet the set stays held
            self.assertEqual(self.attempt(), 0)
        self.assertEqual(len(self.spawned("plan")), 2)
        self.assertEqual(len(self.hold_lines()), 1)
        self.assertEqual(len(self.reader.writes()), 2)

    def test_a_second_failing_pass_after_an_edit_finds_the_marker_and_posts_nothing(self):
        self.two_issues()
        self.attempt(), self.attempt()
        self.reader.label(7, LABEL, "bug", updated="9")  # a person's change: one more plan, which fails the same way
        self.assertEqual(self.attempt(), 1)
        self.assertEqual(self.reader.writes(), [("POST", 7), ("POST", 8)])
        self.assertEqual([len(self.reader.mine(n)) for n in (7, 8)], [1, 1])
        self.assertEqual(self.attempt(), 0)

    def test_a_fresh_operations_instance_finds_the_marker_and_posts_nothing_new(self):
        self.two_issues()
        self.attempt(), self.attempt()
        self.ops = self.new_ops(self.manager, github=lambda cfg: self.readers[cfg.repo])  # a restart
        self.reader.label(8, LABEL, "bug", updated="9")
        self.assertEqual(self.attempt(), 1)
        self.assertEqual(self.reader.writes(), [("POST", 7), ("POST", 8)])
        searched = [call for call in self.reader.gh_calls if call[0] == "gh" and "--paginate" in call]
        self.assertEqual(len(searched), 4)  # each issue was looked up again, by marker
        self.assertEqual([len(self.reader.mine(n)) for n in (7, 8)], [1, 1])

    def test_a_different_code_edits_the_comment_in_place(self):
        self.reader.label(7, LABEL)
        self.attempt(), self.attempt()
        self.reader.label(7, LABEL, updated="9")
        self.attempt(plan_failure.TIMEOUT), self.attempt(plan_failure.TIMEOUT)
        self.assertEqual(self.reader.writes(), [("POST", 7), ("PATCH", 9001)])
        self.assertEqual(self.reader.mine(7), [issue_comments.render_held(7, plan_failure.TIMEOUT)])

    def test_a_label_change_or_an_edit_after_the_comment_still_releases_the_hold(self):
        self.reader.label(7, LABEL)
        self.attempt(), self.attempt()
        self.assertEqual(self.attempt(), 0)
        self.reader.label(7, LABEL, "bug", updated="9")
        self.assertEqual(self.attempt(), 1)
        self.assertEqual(self.attempt(), 0)
        self.reader.label(7, LABEL, "bug", updated="10")
        self.assertEqual(self.attempt(), 1)

    def test_a_label_changed_between_the_failure_and_the_comment_keeps_its_release(self):
        self.reader.label(7, LABEL)
        self.attempt()
        original = self.reader.issue_facts

        def moved(number):  # a person relabels the issue just before the loop reads it again
            self.reader.label(7, LABEL, "bug", updated="9")
            return original(number)

        with patch.object(self.reader, "issue_facts", moved):
            self.assertEqual(self.attempt(), 1)
        self.assertEqual(self.attempt(), 1)

    def test_an_edit_made_while_the_plan_ran_still_releases_the_hold_after_the_comment(self):
        self.reader.label(7, LABEL)
        self.attempt()
        original = self.reader.issue_facts

        def edited(number):  # a person edits the body (no label change) before the loop reads the issue
            self.reader.label(7, LABEL, updated="9")
            return original(number)

        with patch.object(self.reader, "issue_facts", edited):
            self.assertEqual(self.attempt(), 1)
        self.assertEqual(len(self.reader.mine(7)), 1)  # the hold started and the comment is there
        self.assertEqual(self.attempt(), 1)  # the edit released the hold: planned again
        self.assertEqual(self.attempt(), 0)  # it failed the same way: held again, no second comment
        self.assertEqual(len(self.reader.mine(7)), 1)

    def test_a_set_larger_than_the_cycle_cap_is_commented_issue_by_issue_in_full(self):
        self.manager.env[issue_comments.ENV_CAP] = "2"
        self.configure("auto_build_max_issues = 5\n")
        numbers = (7, 8, 9, 10, 11)
        for number in numbers:
            self.reader.label(number, LABEL)
        self.attempt(), self.attempt()
        self.assertEqual([len(self.reader.mine(n)) for n in numbers], [1] * 5)
        self.assertEqual(self.attempt(), 0)
        self.assertEqual([len(self.reader.mine(n)) for n in numbers], [1] * 5)

    def test_a_cap_of_zero_posts_no_hold_comment_and_the_hold_stays(self):
        self.manager.env[issue_comments.ENV_CAP] = "0"
        self.two_issues()
        self.assertEqual(self.attempt(), 1)
        self.assertEqual(self.attempt(), 1)
        self.assertEqual(self.reader.writes(), [])
        skipped = [line for line in self.trigger_lines() if "hold comment skipped" in line]
        self.assertEqual(skipped, [f"trigger repo={REPO} hold comment skipped reason=cap"])
        for _ in range(3):
            self.assertEqual(self.attempt(), 0)
        self.assertEqual(len(self.spawned("plan")), 2)
        self.assertEqual(len(self.hold_lines()), 1)
        self.assertEqual(self.reader.writes(), [])

    def test_a_positive_cap_smaller_than_the_set_still_comments_on_each_issue(self):
        self.manager.env[issue_comments.ENV_CAP] = "1"
        self.two_issues()
        self.attempt(), self.attempt()
        self.assertEqual(self.reader.writes(), [("POST", 7), ("POST", 8)])
        self.assertEqual([len(self.reader.mine(n)) for n in (7, 8)], [1, 1])
        self.assertEqual(self.attempt(), 0)

    def test_nothing_is_posted_when_the_repository_turns_comment_on_blocked_off(self):
        self.configure("comment_on_blocked = false\n")
        self.two_issues()
        self.attempt(), self.attempt()
        self.assertEqual(self.attempt(), 0)
        self.assertEqual(len(self.hold_lines()), 1)
        self.assertEqual(self.reader.comments, {})
        self.assertEqual(self.reader.gh_calls, [])
        self.assertEqual(self.attempt(), 0)  # the hold itself still works

    def test_nothing_is_posted_when_the_deployment_switch_is_off(self):
        self.manager.env[issue_comments.ENV_SWITCH] = "off"
        self.two_issues()
        self.attempt(), self.attempt()
        self.assertEqual(self.reader.comments, {})
        self.assertEqual(self.reader.gh_calls, [])
        self.assertEqual(self.attempt(), 0)
        self.assertEqual(len(self.hold_lines()), 1)

    def test_a_closed_issue_gets_no_comment(self):
        self.two_issues()
        self.reader.issues[8]["state"] = "closed"
        self.attempt(), self.attempt()
        self.assertEqual(self.reader.writes(), [("POST", 7)])

    def test_an_issue_labelled_factory_ignore_after_the_listing_gets_no_comment(self):
        # The label arrives after the listing and its pre-read, so only the guard in the writer can stop the comment:
        # the reader answers the writer's own read (``issue_state``) with the label and the pre-read (``issue_facts``) without it.
        self.two_issues()
        real = self.reader.issue_state

        def state(number):
            found, names = real(number)
            return found, (*names, "Factory-Ignore ") if number == 8 else names  # as GitHub spells it, with stray case and space

        with patch.object(self.reader, "issue_state", side_effect=state):
            self.attempt(), self.attempt()
        self.assertEqual(self.reader.writes(), [("POST", 7)])
        self.assertEqual(self.reader.mine(8), [])
        self.assertEqual(len(self.reader.mine(7)), 1)

    def test_the_ignore_label_of_the_hold_comment_is_the_one_issue_labels_uses(self):
        self.assertEqual(hold_comments.IGNORE_LABEL, IGNORE_LABEL)

    def test_a_failing_comment_is_one_line_and_never_stops_the_loop_or_the_hold(self):
        self.two_issues()
        self.reader.comment_error = subprocess.CalledProcessError(1, ["gh"], stderr=CANARY)
        self.attempt()
        self.assertEqual(self.attempt(), 1)
        lines = [line for line in self.trigger_lines() if "hold comment failed" in line]
        self.assertEqual(lines, [f"trigger repo={REPO} hold comment failed error=CalledProcessError"])
        self.assertRegex(lines[0], SAFE_LINE)
        self.assertEqual(self.attempt(), 0)  # still held
        self.assertEqual(len(self.hold_lines()), 1)

    def test_a_reader_that_raises_for_the_comment_never_stops_the_loop(self):
        self.two_issues()
        self.attempt()
        with patch.object(self.reader, "issue_state", side_effect=RuntimeError(CANARY)):
            self.assertEqual(self.attempt(), 1)
        lines = [line for line in self.trigger_lines() if "hold comment failed" in line]
        self.assertEqual(lines, [f"trigger repo={REPO} hold comment failed error=RuntimeError"])
        self.assertEqual(self.attempt(), 0)

    def test_a_top_up_that_fails_twice_comments_too(self):
        self.reader.label(7, LABEL)
        cfg = self.ops._load(REPO)[0]
        self.ops._hold_failed(REPO, cfg, self.ops._paths(cfg), (7,), plan_failure.OTHER, "x")
        self.assertEqual(self.reader.writes(), [])
        self.ops._hold_failed(REPO, cfg, self.ops._paths(cfg), (7,), plan_failure.OTHER, "x")
        self.assertEqual(self.reader.mine(7), [issue_comments.render_held(7, plan_failure.OTHER)])


class RetryTests(TriggerCase):
    def setUp(self):
        super().setUp()
        self.write_plan("auto-20270101T000000Z", [7, 8], state=True)
        self.write_plan("auto-20260101T000000Z", [3], state=True)

    def retries(self):
        return [argv for argv in self.runner.argvs if "retry" in argv]

    def test_the_label_on_a_blocked_issue_retries_its_job_and_runs_the_run_again(self):
        self.reader.label(7, LABEL, "factory:blocked")
        self.ops.trigger_pass()
        self.assertEqual(
            self.retries(),
            [["fake-cli", "--repo", REPO, "--run-dir", str(self.run_dir("auto-20270101T000000Z")), "retry", "job-7",
              "--reason=label-triggered retry"]],
        )
        self.assertEqual(self.reader.removed, [7])
        runs = self.spawned("run")
        self.assertEqual(self.run_of(runs[0]), "auto-20270101T000000Z")
        self.assertEqual(self.spawned("plan"), [])
        self.assertEqual(self.trigger_lines(), [f"trigger repo={REPO} issues=1 run=auto-20270101T000000Z started retry=yes"])

    def test_ci_failed_is_retried_the_same_way(self):
        self.reader.label(8, LABEL, "factory:ci-failed")
        self.ops.trigger_pass()
        self.assertEqual([argv[-2] for argv in self.retries()], ["job-8"])
        self.assertEqual(len(self.spawned("run")), 1)

    def test_the_retry_is_authorised_like_a_first_build(self):
        self.reader.label(7, LABEL, "factory:blocked", actor=("bob", "User"))
        self.ops.trigger_pass()
        self.assertEqual((self.retries(), self.spawner.calls), ([], []))

    def test_an_issue_in_two_runs_is_retried_in_the_newest_one(self):
        self.write_plan("auto-20270201T000000Z", [7], state=True)
        self.reader.label(7, LABEL, "factory:blocked")
        self.ops.trigger_pass()
        self.assertEqual(self.run_of(self.spawned("run")[-1]), "auto-20270201T000000Z")

    def test_without_a_run_the_issue_is_planned_afresh(self):
        self.reader.label(99, LABEL, "factory:blocked")
        self.ops.trigger_pass()
        self.assertEqual(self.retries(), [])
        self.assertEqual(self.spawned("plan")[-1][-1], "--issues=99")

    def write_investigated_run(self, run, number, status="investigation"):
        """A started run whose plan only gave ``number`` a disposition (an investigation by default) and no job."""
        directory = self.run_dir(run)
        directory.mkdir(parents=True, exist_ok=True)
        plan = {
            "version": 1,
            "issues": [{"number": number, "title": CANARY, "body": CANARY}],
            "jobs": [],
            "dispositions": [{"issue": number, "status": status, "reason": CANARY}],
        }
        (directory / "plan.json").write_text(json.dumps(plan))
        (directory / "state.json").write_text("{}")

    def test_the_label_an_investigation_gets_is_a_request_to_plan_again_not_work_in_flight(self):
        investigated = DISPOSITION_LABELS["investigation"]
        facts = lambda *labels: IssueFacts(12, (LABEL, *labels))  # noqa: E731
        self.assertEqual(trigger.classify(facts(investigated), LABEL), "retry")
        # what an investigation used to get: the watcher took it for queued work and ignored the label
        self.assertEqual(trigger.classify(facts(LABEL_PLANNED), LABEL), "in-flight")

    def test_re_applying_the_label_on_an_investigated_issue_plans_it_afresh_without_a_retry(self):
        self.write_investigated_run("auto-20270301T000000Z", 12)
        self.reader.label(12, LABEL, DISPOSITION_LABELS["investigation"])
        self.ops.trigger_pass()
        self.assertEqual(self.retries(), [])  # no job owns it: there is nothing to retry
        self.assertEqual(self.spawned("run"), [])
        self.assertEqual(self.spawned("plan")[-1][-1], "--issues=12")

    def test_a_newer_investigation_is_not_overruled_by_an_older_runs_job_for_the_same_issue(self):
        # job-7 sits in the older run auto-20270101T000000Z (setUp); the newer plan only investigated #7,
        # so re-applying the label must plan it afresh, not retry a job whose plan never saw the answers
        self.write_investigated_run("auto-20270301T000000Z", 7)
        self.reader.label(7, LABEL, DISPOSITION_LABELS["investigation"])
        self.ops.trigger_pass()
        self.assertEqual(self.retries(), [])
        self.assertEqual(self.spawned("run"), [])
        self.assertEqual(self.spawned("plan")[-1][-1], "--issues=7")

    def test_a_newer_blocked_disposition_without_a_job_hands_the_issue_to_a_fresh_plan(self):
        self.write_investigated_run("auto-20270301T000000Z", 7, status="blocked")
        self.reader.label(7, LABEL, "factory:blocked")
        self.ops.trigger_pass()
        self.assertEqual(self.retries(), [])
        self.assertEqual(self.spawned("plan")[-1][-1], "--issues=7")

    def test_an_older_investigation_does_not_hide_a_newer_runs_job(self):
        self.write_investigated_run("auto-20260601T000000Z", 7)  # older than job-7's run: it does not matter
        self.write_plan("auto-20270201T000000Z", [7], state=True)
        self.reader.label(7, LABEL, "factory:blocked")
        self.ops.trigger_pass()
        self.assertEqual(self.run_of(self.spawned("run")[-1]), "auto-20270201T000000Z")
        self.assertEqual(self.spawned("plan"), [])

    def test_a_newer_covered_disposition_does_not_hide_an_older_runs_job(self):
        # "covered" only says another job's work resolves the issue; it gives the issue no label, so it does
        # not mean the issue was set aside, and the job a person asked to retry is still the one to retry
        self.write_investigated_run("auto-20270301T000000Z", 7, status="covered")
        self.reader.label(7, LABEL, "factory:blocked")
        self.ops.trigger_pass()
        self.assertEqual(self.run_of(self.spawned("run")[-1]), "auto-20270101T000000Z")
        self.assertEqual(self.spawned("plan"), [])

    def block_on_closed_dependency(self, run, job, reason="dependency job-1 was closed without merging"):
        """The state ``block`` leaves for a job whose dependency was closed without merging."""
        state = {"jobs": {job: {"status": "blocked", "error": reason}}}
        (self.run_dir(run) / "state.json").write_text(json.dumps(state))

    def test_a_job_blocked_by_a_closed_dependency_is_planned_afresh_not_retried(self):
        self.block_on_closed_dependency("auto-20270101T000000Z", "job-7")
        self.reader.label(7, LABEL, "factory:blocked")
        self.ops.trigger_pass()
        self.assertEqual(self.retries(), [])  # the old job is neither retried nor blocked a second time
        self.assertEqual(self.runner.commands("block"), [])
        self.assertEqual(self.spawned("run"), [])
        plans = self.spawned("plan")
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[-1][-1], "--issues=7")  # a new plan, in a new run
        self.assertNotEqual(self.run_of(plans[-1]), "auto-20270101T000000Z")
        self.assertIn(
            f"trigger repo={REPO} issue=7 retry replanned reason=dependency-closed", self.trigger_lines()
        )

    def test_a_stale_blocked_job_in_a_run_that_sorts_later_does_not_hide_a_live_job_of_another_run(self):
        # runs are ordered by name, not by time: the manual run "zz-manual" is looked at before "auto-..."
        self.write_plan("zz-manual", [7], state=True)
        self.block_on_closed_dependency("zz-manual", "job-7")
        (self.run_dir("auto-20270101T000000Z") / "state.json").write_text(
            json.dumps({"jobs": {"job-7": {"status": "failed", "error": "tests failed"}}})
        )
        self.reader.label(7, LABEL, "factory:blocked")
        self.ops.trigger_pass()
        self.assertEqual([(argv[-2], self.run_of(argv)) for argv in self.retries()], [("job-7", "auto-20270101T000000Z")])
        self.assertEqual(self.spawned("plan"), [])

    def test_a_job_blocked_for_another_reason_still_goes_through_the_retry(self):
        self.block_on_closed_dependency("auto-20270101T000000Z", "job-7", reason="builder blocked: no network")
        self.reader.label(7, LABEL, "factory:blocked")
        self.ops.trigger_pass()
        self.assertEqual([argv[-2] for argv in self.retries()], ["job-7"])
        self.assertEqual(self.spawned("plan"), [])
        self.assertEqual(self.trigger_lines(), [f"trigger repo={REPO} issues=1 run=auto-20270101T000000Z started retry=yes"])

    def test_a_job_that_failed_is_still_retried_even_if_its_error_names_a_dependency(self):
        state = {"jobs": {"job-8": {"status": "failed", "error": "dependency job-1 was closed without merging"}}}
        (self.run_dir("auto-20270101T000000Z") / "state.json").write_text(json.dumps(state))
        self.reader.label(8, LABEL, "factory:ci-failed")
        self.ops.trigger_pass()
        self.assertEqual([argv[-2] for argv in self.retries()], ["job-8"])

    def refuse_retries(self):
        self.runner.override[("retry",)] = lambda repo, run, tail: mgr.CommandResult(1, f"o {CANARY}".encode(), f"e {CANARY}".encode())

    def test_a_retry_the_cli_refuses_keeps_the_label_and_pauses_that_issue(self):
        self.refuse_retries()
        self.reader.label(7, LABEL, "factory:blocked")
        self.ops.trigger_pass()
        self.assertEqual(self.spawned("run"), [])
        self.assertEqual(self.reader.removed, [])
        self.assertEqual(
            self.trigger_lines(), [f"trigger repo={REPO} run=auto-20270101T000000Z retry refused issues=1 backoff=60s reason=cli-refused"]
        )
        self.assertFalse((self.run_dir("auto-20270101T000000Z") / ops.OWED_MARKER).exists())
        self.assertEqual(len(self.retries()), 1)
        self.ops.trigger_pass()  # paused: the CLI is not asked again, and the line is not repeated
        self.assertEqual(len(self.retries()), 1)
        self.time.now += 61
        self.ops.trigger_pass()
        self.assertEqual(len(self.retries()), 2)
        self.assertTrue(self.trigger_lines()[-1].endswith("retry refused issues=1 backoff=120s reason=cli-refused"))

    def test_a_stale_retry_request_does_not_starve_the_other_issues_of_the_repository(self):
        self.refuse_retries()
        self.reader.label(7, LABEL, "factory:blocked")
        self.reader.label(50, LABEL)
        self.ops.trigger_pass()
        self.assertEqual(self.spawned("plan")[-1][-1], "--issues=50")  # planned in the very same pass
        self.assertEqual(len(self.retries()), 1)
        self.assertNotIn("backoff=", self.trigger_lines()[-1])  # no repository-wide backoff: the plan started
        self.assertTrue(self.trigger_lines()[-1].endswith("planning"))

    def test_one_refused_job_does_not_stop_the_retry_of_another(self):
        self.runner.override[("retry",)] = lambda repo, run, tail: (
            mgr.CommandResult(1, b"", b"") if "job-7" in tail else mgr.CommandResult(0, b"ok", b"")
        )
        self.reader.label(7, LABEL, "factory:blocked")
        self.reader.label(8, LABEL, "factory:blocked")
        self.ops.trigger_pass()
        self.assertEqual(self.reader.removed, [8])
        self.assertIn(LABEL, self.reader.labels_of(7))
        self.assertEqual(len(self.spawned("run")), 1)
        self.assertEqual(self.trigger_lines()[0].split(" ", 3)[3][:20], "retry refused issues")
        self.assertTrue(self.trigger_lines()[-1].startswith(f"trigger repo={REPO} issues=1 run="))

    def test_a_run_that_does_not_start_after_the_retry_is_started_by_the_next_pass(self):
        self.reader.label(7, LABEL, "factory:blocked")
        self.script_lease(REPO, "free", "busy", then="free")
        self.ops.trigger_pass()
        self.assertEqual((len(self.retries()), self.spawned("run")), (1, []))
        self.reader.label(7, "factory:planned")  # what the retry did to the issue
        self.ops.trigger_pass()
        self.assertEqual(len(self.spawned("run")), 1)
        self.assertEqual(len(self.retries()), 1)  # not retried again
        self.assertEqual(self.trigger_lines()[-1], f"trigger repo={REPO} issues=1 run=auto-20270101T000000Z started retry=yes")

    def test_two_issues_of_one_job_retry_it_once(self):
        self.write_plan("auto-20270301T000000Z", [20, 21], state=True)
        path = self.run_dir("auto-20270301T000000Z") / "plan.json"
        document = json.loads(path.read_text())
        document["jobs"] = [{"id": "shared", "issues": [20, 21]}]
        path.write_text(json.dumps(document))
        self.reader.label(20, LABEL, "factory:blocked")
        self.reader.label(21, LABEL, "factory:blocked")
        self.ops.trigger_pass()
        self.assertEqual([argv[-2] for argv in self.retries()], ["shared"])
        self.assertEqual(sorted(self.reader.removed), [20, 21])
        self.assertTrue(self.trigger_lines()[-1].startswith(f"trigger repo={REPO} issues=2 "))

    def test_new_work_waits_while_a_retry_takes_the_cycle(self):
        self.reader.label(7, LABEL, "factory:blocked")
        self.reader.label(50, LABEL)
        self.ops.trigger_pass()
        self.assertEqual(self.spawned("plan"), [])
        self.assertEqual(len(self.spawned("run")), 1)


    def test_a_retry_whose_run_could_not_start_is_started_after_a_restart(self):
        self.reader.label(7, LABEL, "factory:blocked")
        self.script_lease(REPO, "free", "busy", then="free")
        self.ops.trigger_pass()  # retried, label taken off, the run refused as busy
        run_dir = self.run_dir("auto-20270101T000000Z")
        self.assertEqual((len(self.retries()), self.spawned("run")), (1, []))
        self.assertEqual((run_dir / ops.OWED_MARKER).read_text(), "1")
        manager = self.new_manager()  # a restart: nothing is remembered but the disk
        fresh = self.new_ops(manager, github=lambda cfg: self.readers[cfg.repo])
        fresh.trigger_pass()
        self.assertEqual(self.run_of(self.spawned("run")[-1]), "auto-20270101T000000Z")
        self.assertEqual(self.trigger_lines()[-1], f"trigger repo={REPO} issues=1 run=auto-20270101T000000Z started retry=yes")
        self.assertFalse((run_dir / ops.OWED_MARKER).exists())
        self.assertEqual(len(self.retries()), 1)  # not retried again

    def test_an_owed_run_is_checked_against_the_repository_too(self):
        (self.run_dir("auto-20270101T000000Z") / ops.OWED_MARKER).write_text("1")
        self.reader.facts = RepoFacts(archived=True, private=True)
        self.ops.trigger_pass()
        self.assertEqual(self.spawned("run"), [])
        self.assertEqual(self.trigger_lines(), [f"trigger repo={REPO} skipped reason=archived"])

    def test_a_label_that_cannot_be_removed_does_not_retry_the_same_issue_over_and_over(self):
        self.reader.remove_error = subprocess.CalledProcessError(1, ["gh"], stderr=CANARY)
        self.reader.label(7, LABEL, "factory:blocked")
        for _ in range(4):
            self.ops.trigger_pass()
            for process in self.spawner.processes:
                process.returncode = 0
        self.assertEqual((len(self.retries()), len(self.spawned("run"))), (1, 1))
        self.assertEqual(
            len([line for line in self.trigger_lines() if line.endswith("skipped reason=unremoved")]), 1
        )  # said once
        # the person labels it again after fixing something: a new label event is a new request
        self.reader.label(7, LABEL, "factory:blocked", updated="2")
        self.ops.trigger_pass()
        self.assertEqual((len(self.retries()), len(self.spawned("run"))), (2, 2))

# --- the trigger pause ------------------------------------------------------------------------


class PauseTests(TriggerCase):
    """While the pause marker is set the manager starts no plan or run by itself (docs/manager.md, "Trigger pause")."""

    def make(self, *args, **kwargs):  # a started run whose job-2 waits for job-1's pull request
        return DependencyResumeTests.make(self, *args, **kwargs)

    def test_a_paused_trigger_reads_nothing_and_starts_no_plan(self):
        self.reader.label(7, LABEL)
        self.manager.set_trigger_pause(True)
        for _ in range(3):
            self.ops.trigger_pass()
        self.assertEqual((self.spawner.calls, self.reader.calls), ([], []))
        self.assertEqual(self.trigger_lines(), ["trigger skipped reason=paused"])  # one line, not one per pass
        self.manager.set_trigger_pause(False)
        self.ops.trigger_pass()
        self.assertEqual(len(self.spawned("plan")), 1)  # the label was never consumed: it is built after the resume

    def test_a_planned_run_is_not_run_while_paused(self):
        self.reader.label(7, LABEL)
        self.ops.trigger_pass()
        self.finish(self.spawned("plan")[-1], 0, [7])
        self.manager.set_trigger_pause(True)
        self.ops.trigger_pass()
        self.assertEqual((self.spawned("run"), self.reader.removed), ([], []))
        self.manager.set_trigger_pause(False)
        self.ops.trigger_pass()
        self.assertEqual(len(self.spawned("run")), 1)

    def test_dependency_resume_waits_for_the_pause_to_end(self):
        self.make()
        self.reader.pulls = {11: "merged"}
        self.manager.set_trigger_pause(True)
        self.ops.trigger_pass()
        self.assertEqual((self.spawned("run"), self.reader.calls), ([], []))
        self.manager.set_trigger_pause(False)
        self.ops.trigger_pass()
        self.assertEqual(len(self.spawned("run")), 1)

    def test_a_pause_set_in_the_middle_of_a_pass_still_stops_the_start(self):
        """The manager's own in-process starts refuse too, so a pass that began before the pause cannot slip one in."""
        self.reader.label(7, LABEL)
        original = self.reader.open_issues

        def pause_then_list(label):
            self.manager.set_trigger_pause(True)
            return original(label)

        self.reader.open_issues = pause_then_list
        self.ops.trigger_pass()
        self.assertEqual(self.spawner.calls, [])
        self.assertEqual(list((self.data_dir / "repos" / REPO / "runs").glob("*")), [])  # no half-made run left behind
        self.assertEqual(self.warnings(), [])

    def test_a_run_in_progress_is_not_touched_by_the_pause(self):
        self.reader.label(7, LABEL)
        _, run = self.start_build([7])
        child = self.manager.children.active_for_repo(REPO)
        self.manager.set_trigger_pause(True)
        self.ops.trigger_pass()
        self.assertTrue(child.running())
        self.assertEqual(self.manager.children.active_for_repo(REPO), child)
        self.assertEqual(self.runner.commands("drain"), [])  # the pause itself drains nothing
        self.assertEqual(self.spawner.processes[-1].signals, [])  # nothing was signalled

    def test_the_marker_survives_a_manager_restart(self):
        self.reader.label(7, LABEL)
        self.manager.set_trigger_pause(True)
        restarted = self.new_manager()
        self.assertTrue(restarted.trigger_paused())
        self.ops = self.new_ops(restarted, github=lambda cfg: self.readers[cfg.repo])
        self.ops.trigger_pass()
        self.assertEqual(self.spawner.calls, [])
        restarted.set_trigger_pause(False)
        self.assertFalse(self.new_manager().trigger_paused())

    def test_setting_and_clearing_are_idempotent(self):
        for wanted in (True, True, False, False):
            self.manager.set_trigger_pause(wanted)
            self.assertEqual(self.manager.trigger_paused(), wanted)
        self.assertEqual([p.name for p in self.data_dir.iterdir() if p.name.startswith(".")], [])  # no temp file left


# --- runs whose queued jobs wait for a dependency merge ---------------------------------------

RUN_A = "auto-20270101T000000Z"
PULL = "https://github.com/org/name/pull/{}"


class DependencyResumeTests(TriggerCase):
    """A started, idle run with a queued job behind another job's pull request (docs/manager.md)."""

    def setUp(self):
        super().setUp()
        self.labelled = []  # (issue, label): what the fake CLI's `block` puts on an issue
        self.runner.override[("block",)] = self.block

    def block(self, repo, run, tail):
        """The CLI's ``block``: the job turns blocked with the reason, and its issue is labelled."""
        directory = self.run_dir(run, repo)
        data = json.loads((directory / "state.json").read_text())
        reason = next(item.split("=", 1)[1] for item in tail if item.startswith("--reason="))
        data["jobs"][tail[0]].update(status="blocked", error=reason)
        (directory / "state.json").write_text(json.dumps(data))
        self.labelled.append((int(tail[0].split("-")[1]), "factory:blocked"))
        return ops.CommandResult(0, b"blocked\n")

    def make(self, run=RUN_A, jobs=None):
        """A started run. ``jobs``: id -> (status, depends_on, pull number or None); default job-2 waits for job-1."""
        jobs = jobs or {"job-1": ("pr_ready", [], 11), "job-2": ("queued", ["job-1"], None)}
        directory = self.run_dir(run)
        directory.mkdir(parents=True, exist_ok=True)
        specs, states = [], {}
        for ident, (status, needs, pull) in jobs.items():
            number = int(ident.split("-")[1])
            specs.append({"id": ident, "issues": [number], "depends_on": needs, "objective": CANARY})
            states[ident] = {"status": status, "attempt": 1}
            if pull is not None:
                states[ident]["pr"] = PULL.format(pull)
        (directory / "plan.json").write_text(json.dumps({"version": 1, "issues": [], "jobs": specs}))
        (directory / "state.json").write_text(json.dumps({"version": 1, "jobs": states}))
        return directory

    def set_status(self, job, status, run=RUN_A):
        path = self.run_dir(run) / "state.json"
        data = json.loads(path.read_text())
        data["jobs"][job]["status"] = status
        path.write_text(json.dumps(data))

    def runs(self):
        return self.spawned("run")

    def skips(self):
        return [line for line in self.trigger_lines() if "skipped" in line]

    def restart(self):
        """A new manager process: no memory, but the same data directory, GitHub and (here) registry."""
        self.ops = self.new_ops(self.manager, github=lambda cfg: self.readers[cfg.repo])

    def test_the_configured_session_timeout_is_used_by_the_dependency_resume(self):
        self.configure("auto_build_timeout_seconds = 7200\n")
        self.make()
        self.reader.pulls = {11: "merged"}
        self.ops.trigger_pass()
        [argv] = self.runs()
        self.assertEqual(argv[argv.index("--timeout") + 1], "7200")

    def test_the_dependency_merging_resumes_the_idle_run_exactly_once(self):
        self.make()
        self.reader.pulls = {11: "open"}
        self.ops.trigger_pass()
        self.assertEqual(self.runs(), [])
        self.reader.pulls[11] = "merged"
        self.ops.trigger_pass()
        [argv] = self.runs()
        self.assertEqual(
            argv, ["fake-cli", "--repo", REPO, "--run-dir", str(self.run_dir(RUN_A)), "run", "--concurrency", "1", "--timeout", "1800"]
        )
        self.assertEqual(self.trigger_lines()[-1], f"trigger repo={REPO} run={RUN_A} jobs=1 started resume=dependencies")
        child = self.manager.children.active_for_repo(REPO)  # the manager's own start: the registry knows it
        self.assertEqual((child.kind, child.repo, child.run), ("run", REPO, RUN_A))
        for _ in range(3):  # the job runs: nothing starts, no pull request is read (labels may be: a top-up looks)
            before = self.reader.kinds().count("pull")
            self.ops.trigger_pass()
            self.assertEqual((len(self.runs()), self.reader.kinds().count("pull")), (1, before))
            self.assertEqual(len(self.spawner.calls), 1)
        # The run reconciles job-1 as merged, builds job-2 and exits.
        self.set_status("job-1", "merged")
        self.set_status("job-2", "pr_ready")
        self.spawner.processes[-1].returncode = 0
        for _ in range(3):
            self.ops.trigger_pass()
        self.assertEqual(len(self.runs()), 1)
        self.assertEqual(self.runner.commands("block"), [])

    def test_a_run_that_ends_without_moving_the_job_is_not_started_over_and_over(self):
        self.make()
        self.reader.pulls = {11: "merged"}
        self.ops.trigger_pass()
        self.assertEqual(len(self.runs()), 1)
        self.spawner.processes[-1].returncode = 0  # drained or crashed: job-2 is still queued, job-1 still unreconciled
        calls = len(self.reader.calls)
        for _ in range(3):
            self.ops.trigger_pass()
        self.assertEqual(len(self.runs()), 1)
        self.assertEqual(self.reader.kinds().count("pull"), 1)
        marker = ops.read_resumed(self.run_dir(RUN_A) / ops.DEPENDENCY_MARKER)
        self.assertEqual(marker["job-2"][0], ops.MAX_RESUME_ATTEMPTS)  # a clean exit: its starts are used up
        self.time.now += 100000  # and a person is needed, however long it takes
        self.ops.trigger_pass()
        self.assertEqual(len(self.runs()), 1)
        self.assertGreater(len(self.reader.calls), calls)  # (the repository facts are still read)

    def test_a_resumed_run_that_fails_is_logged_and_started_again_after_the_backoff(self):
        self.make()
        self.reader.pulls = {11: "merged"}
        self.ops.trigger_pass()
        self.assertEqual(len(self.runs()), 1)
        self.spawner.processes[-1].returncode = 1  # e.g. the lease was taken between the probe and the CLI's own lock
        self.ops.trigger_pass()
        self.assertEqual(self.trigger_lines()[-1], f"trigger repo={REPO} run={RUN_A} run failed rc=1 backoff=60s")
        self.assertFalse((self.run_dir(RUN_A) / ops.DEPENDENCY_MARKER).exists())  # the job is not lost
        self.ops.trigger_pass()  # the repository backoff holds
        self.assertEqual(len(self.runs()), 1)
        self.time.now += 61
        self.ops.trigger_pass()
        self.assertEqual(len(self.runs()), 2)
        self.spawner.processes[-1].returncode = 1
        self.ops.trigger_pass()
        self.assertEqual(self.trigger_lines()[-1], f"trigger repo={REPO} run={RUN_A} run failed rc=1 backoff=120s")
        self.time.now += 121
        self.ops.trigger_pass()
        self.assertEqual(len(self.runs()), 3)
        self.spawner.processes[-1].returncode = 0  # this time it ran (and moved the job)
        self.set_status("job-1", "merged")
        self.set_status("job-2", "pr_ready")
        for _ in range(3):
            self.ops.trigger_pass()
        self.assertEqual(len(self.runs()), 3)

    def test_after_a_restart_a_resumed_job_that_stays_queued_is_retried_a_bounded_number_of_times(self):
        self.make()
        self.reader.pulls = {11: "merged"}
        self.ops.trigger_pass()
        self.spawner.processes[-1].returncode = 1  # the exit code dies with this manager
        self.restart()
        self.ops.trigger_pass()
        self.assertEqual(len(self.runs()), 1)
        self.assertEqual(
            self.skips(), [f"trigger repo={REPO} run={RUN_A} job=job-2 skipped reason=resumed"]
        )  # a fixed line, shown once
        for _ in range(3):
            self.ops.trigger_pass()
        self.assertEqual(len(self.skips()), 1)
        starts = 1
        for _ in range(5):
            self.time.now += 5000  # longer than the longest retry wait
            self.restart()
            self.ops.trigger_pass()
            if len(self.runs()) > starts:
                starts += 1
                self.spawner.processes[-1].returncode = 1
        self.assertEqual(starts, ops.MAX_RESUME_ATTEMPTS)  # then only a person can move it on
        self.assertEqual(len(self.runs()), ops.MAX_RESUME_ATTEMPTS)

    def test_while_the_dependency_is_open_nothing_starts_and_one_skip_line_is_logged(self):
        self.make()
        self.reader.pulls = {11: "open"}
        for _ in range(5):
            self.ops.trigger_pass()
            self.time.now += 60
        self.assertEqual(self.spawner.calls, [])
        self.assertEqual(self.skips(), [f"trigger repo={REPO} run={RUN_A} skipped reason=dependency"])
        self.assertEqual(self.runner.commands("block"), [])
        self.time.now += 3600  # an hour later the line may show again, once
        self.ops.trigger_pass()
        self.assertEqual(len(self.skips()), 2)

    def test_a_dependency_without_a_pull_request_yet_just_waits_without_asking_github(self):
        self.make(jobs={"job-1": ("running", [], None), "job-2": ("queued", ["job-1"], None)})
        self.ops.trigger_pass()
        self.assertEqual(self.reader.kinds().count("pull"), 0)
        self.assertEqual(self.spawner.calls, [])
        self.assertEqual(len(self.skips()), 1)

    def test_a_pull_request_of_another_repository_is_not_trusted(self):
        directory = self.make()
        data = json.loads((directory / "state.json").read_text())
        data["jobs"]["job-1"]["pr"] = "https://github.com/evil/other/pull/11"
        (directory / "state.json").write_text(json.dumps(data))
        self.reader.pulls = {11: "merged"}
        self.ops.trigger_pass()
        self.assertEqual((self.spawner.calls, self.reader.kinds().count("pull")), ([], 0))

    def test_a_dependency_closed_without_merging_blocks_the_job_with_a_reason_and_is_not_retried(self):
        self.make()
        self.reader.pulls = {11: "closed"}
        self.ops.trigger_pass()
        [argv] = [argv for argv in self.runner.argvs if "block" in argv]
        self.assertEqual(argv[-3:], ["block", "job-2", "--reason=dependency job-1 was closed without merging"])
        state = json.loads((self.run_dir(RUN_A) / "state.json").read_text())["jobs"]["job-2"]
        self.assertEqual(state["status"], "blocked")
        self.assertIn("job-1", state["error"])
        self.assertEqual(self.labelled, [(2, "factory:blocked")])
        self.assertEqual(self.trigger_lines(), [f"trigger repo={REPO} run={RUN_A} job=job-2 blocked dependency=job-1 reason=dependency-closed"])
        self.assertEqual(self.spawner.calls, [])
        for _ in range(4):  # a permanently blocked job is never looked at again
            self.ops.trigger_pass()
            self.time.now += 7200
        self.assertEqual(len(self.runner.commands("block")), 1)
        self.assertEqual((self.reader.kinds().count("pull"), self.spawner.calls), (1, []))
        self.assertEqual(len(self.trigger_lines()), 1)

    def test_a_dependency_already_closed_in_the_state_blocks_without_asking_github(self):
        self.make(jobs={"job-1": ("closed", [], 11), "job-2": ("queued", ["job-1"], None)})
        self.ops.trigger_pass()
        self.assertEqual(len(self.runner.commands("block")), 1)
        self.assertEqual(self.reader.kinds().count("pull"), 0)

    def test_a_refused_block_pauses_that_job_with_a_backoff_and_never_starts_a_run(self):
        self.make()
        self.reader.pulls = {11: "closed"}
        self.runner.override[("block",)] = failure()
        self.ops.trigger_pass()
        self.assertEqual(
            self.trigger_lines(), [f"trigger repo={REPO} run={RUN_A} job=job-2 block failed rc=1 backoff=60s"]
        )
        self.ops.trigger_pass()  # paused: the CLI is not asked again
        self.assertEqual(len(self.runner.commands("block")), 1)
        self.time.now += 61
        self.runner.override[("block",)] = self.block
        self.ops.trigger_pass()
        self.assertEqual(len(self.runner.commands("block")), 2)
        self.assertEqual(self.spawner.calls, [])

    def test_a_manager_restart_between_passes_changes_nothing(self):
        self.make()
        self.reader.pulls = {11: "open"}
        self.ops.trigger_pass()
        self.restart()
        self.reader.pulls[11] = "merged"
        self.ops.trigger_pass()
        self.assertEqual(len(self.runs()), 1)
        self.restart()  # restarted while the run is going, and again after it ended with the job still queued
        self.ops.trigger_pass()
        self.spawner.processes[-1].returncode = 0
        self.restart()
        self.ops.trigger_pass()
        self.assertEqual(len(self.runs()), 1)

    def test_a_restart_before_the_merge_still_blocks_a_closed_dependency_once(self):
        self.make()
        self.reader.pulls = {11: "open"}
        self.ops.trigger_pass()
        self.restart()
        self.reader.pulls[11] = "closed"
        self.ops.trigger_pass()
        self.restart()
        self.ops.trigger_pass()
        self.assertEqual(len(self.runner.commands("block")), 1)

    def test_a_job_with_two_dependencies_resumes_only_when_both_are_merged(self):
        self.make(jobs={
            "job-1": ("pr_ready", [], 11), "job-2": ("pr_ready", [], 12),
            "job-3": ("queued", ["job-1", "job-2"], None),
        })
        self.reader.pulls = {11: "merged", 12: "open"}
        self.ops.trigger_pass()
        self.assertEqual(self.spawner.calls, [])
        self.reader.pulls[12] = "merged"
        self.ops.trigger_pass()
        self.assertEqual(len(self.runs()), 1)
        self.assertTrue(self.trigger_lines()[-1].endswith("jobs=1 started resume=dependencies"))

    def test_one_dependency_closed_blocks_even_when_the_other_is_still_open(self):
        self.make(jobs={
            "job-1": ("pr_ready", [], 11), "job-2": ("pr_ready", [], 12),
            "job-3": ("queued", ["job-1", "job-2"], None),
        })
        self.reader.pulls = {11: "open", 12: "closed"}
        self.ops.trigger_pass()
        self.assertEqual(self.runner.commands("block"), [(REPO, RUN_A)])
        self.assertIn("job-2", json.loads((self.run_dir(RUN_A) / "state.json").read_text())["jobs"]["job-3"]["error"])

    def test_dependencies_already_merged_in_the_state_are_not_asked_about(self):
        self.make(jobs={
            "job-1": ("merged", [], 11), "job-2": ("pr_ready", [], 12),
            "job-3": ("queued", ["job-1", "job-2"], None),
        })
        self.reader.pulls = {12: "merged"}
        self.ops.trigger_pass()
        self.assertEqual([call for call in self.reader.calls if call[0] == "pull"], [("pull", 12)])
        self.assertEqual(len(self.runs()), 1)

    def test_only_the_jobs_that_wait_for_a_merge_count(self):
        self.make(jobs={"job-1": ("pr_ready", [], 11), "job-2": ("failed", ["job-1"], None), "job-3": ("running", ["job-1"], None)})
        self.reader.pulls = {11: "merged"}
        self.ops.trigger_pass()
        self.assertEqual((self.spawner.calls, self.reader.kinds().count("pull")), ([], 0))

    def test_a_busy_lease_is_a_busy_skip_not_a_start(self):
        self.make()
        self.reader.pulls = {11: "merged"}
        self.script_lease(REPO, "busy", "busy", then="free")
        self.ops.trigger_pass()
        self.ops.trigger_pass()
        self.assertEqual((self.spawner.calls, self.reader.calls), ([], []))
        self.assertEqual(self.trigger_lines(), [f"trigger repo={REPO} skipped reason=busy"])
        self.ops.trigger_pass()
        self.assertEqual(len(self.runs()), 1)

    def test_a_lease_taken_between_the_probe_and_the_start_leaves_the_run_for_the_next_pass(self):
        self.make()
        self.reader.pulls = {11: "merged"}
        self.script_lease(REPO, "free", "busy", then="free")  # our probe, then the manager's own
        self.ops.trigger_pass()
        self.assertEqual(self.spawner.calls, [])
        self.assertEqual(self.trigger_lines(), [f"trigger repo={REPO} skipped reason=busy"])
        self.assertFalse((self.run_dir(RUN_A) / ops.DEPENDENCY_MARKER).exists())  # the refused start left no mark
        self.ops.trigger_pass()
        self.assertEqual(len(self.runs()), 1)

    def test_a_running_process_is_a_busy_skip_and_nothing_is_read(self):
        self.make()
        self.reader.pulls = {11: "merged"}
        self.add_child(REPO, "elsewhere", "run")
        self.ops.trigger_pass()
        self.assertEqual((self.spawner.calls, self.reader.calls), ([], []))
        self.assertEqual(self.trigger_lines(), [f"trigger repo={REPO} skipped reason=busy"])

    def test_a_run_over_its_budget_is_not_resumed(self):
        self.policies[REPO] = ops.Policy(604800, 5.0, "never")
        self.make()
        self.reader.pulls = {11: "merged"}
        self.runner.reports[(REPO, RUN_A)] = ops_report(cost=6.0)
        self.ops.trigger_pass()
        self.assertEqual(self.spawner.calls, [])
        self.assertEqual(self.skips(), [f"trigger repo={REPO} run={RUN_A} skipped reason=over-budget"])
        self.runner.reports[(REPO, RUN_A)] = ops_report(cost=1.0)
        self.ops.trigger_pass()
        self.assertEqual(len(self.runs()), 1)

    def test_a_pass_reads_at_most_a_bounded_number_of_pull_requests(self):
        jobs = {f"job-{n}": ("pr_ready", [], 100 + n) for n in range(1, 6)}
        jobs.update({f"job-{n}": ("queued", [f"job-{n - 5}"], None) for n in range(6, 11)})
        self.make(jobs=jobs)
        self.reader.pulls = {100 + n: "open" for n in range(1, 6)}
        with patch.object(ops, "MAX_DEPENDENCY_LOOKUPS", 3):
            self.ops.trigger_pass()
        self.assertEqual(self.reader.kinds().count("pull"), 3)

    def test_a_github_failure_is_the_usual_one_line_and_backoff_and_starts_nothing(self):
        self.make()
        self.reader.pulls = {11: "merged"}
        self.reader.fail = subprocess.CalledProcessError(1, ["gh"], stderr=CANARY, output=CANARY)
        self.ops.trigger_pass()
        self.assertEqual(self.trigger_lines(), [f"trigger repo={REPO} pass failed error=CalledProcessError backoff=60s"])
        self.assertEqual(self.spawner.calls, [])

    def test_an_owed_or_pending_run_comes_first_and_every_line_is_made_of_ids_and_fixed_words(self):
        self.make()
        self.reader.pulls = {11: "merged"}
        self.write_plan("auto-20260101T000000Z", [5], mark=True)
        self.ops.trigger_pass()
        self.assertEqual([self.run_of(argv) for argv in self.runs()], ["auto-20260101T000000Z"])
        self.spawner.processes[-1].returncode = 0
        (self.run_dir("auto-20260101T000000Z") / "state.json").write_text("{}")
        self.ops.trigger_pass()
        self.assertEqual([self.run_of(argv) for argv in self.runs()], ["auto-20260101T000000Z", RUN_A])
        for line in self.trigger_lines():
            self.assertRegex(line, SAFE_LINE)

    def test_runs_without_a_dependency_wait_cost_no_github_call(self):
        self.write_plan(RUN_A, [7, 8], state=True)  # a plan whose jobs have no depends_on, and an empty state
        self.ops.trigger_pass()
        self.assertEqual(self.reader.kinds(), ["repo", "issues"])


class UnremovedLabelTests(TriggerCase):
    def fail_removal(self):
        self.reader.remove_error = subprocess.CalledProcessError(1, ["gh"], stderr=CANARY)

    def ended(self):
        for process in self.spawner.processes:
            process.returncode = 0

    def test_an_issue_whose_label_cannot_be_removed_is_not_built_again_even_with_no_status_label(self):
        # The status labels use the same credentials, so they may be missing too: nothing marks the issue.
        self.reader.label(7, LABEL)
        self.fail_removal()
        self.start_build([7])
        self.ended()
        for _ in range(3):
            self.ops.trigger_pass()
        self.assertEqual((len(self.spawned("plan")), len(self.spawned("run"))), (1, 1))
        self.assertEqual(
            [line for line in self.trigger_lines() if "unremoved" in line],
            [f"trigger repo={REPO} issue=7 skipped reason=unremoved"],
        )
        self.assertEqual(self.reader.kinds().count("remove"), 4)  # the removal is tried again every cycle

    def test_once_the_label_can_be_removed_it_is_and_nothing_is_built(self):
        self.reader.label(7, LABEL)
        self.fail_removal()
        self.start_build([7])
        self.ended()
        self.ops.trigger_pass()
        self.reader.remove_error = None
        self.ops.trigger_pass()
        self.assertEqual(self.reader.removed, [7])
        self.assertNotIn(LABEL, self.reader.labels_of(7))
        self.ops.trigger_pass()
        self.assertEqual(len(self.spawned("plan")), 1)

    def test_a_label_applied_anew_is_a_new_request_and_goes_through_the_authorisation(self):
        self.reader.label(7, LABEL)
        self.fail_removal()
        self.start_build([7])
        self.ended()
        self.ops.trigger_pass()
        self.reader.label(7, LABEL, actor=("bob", "User"), updated="2")  # a triager, not enough
        self.ops.trigger_pass()
        self.assertEqual(len(self.spawned("plan")), 1)
        self.assertIn(f"trigger repo={REPO} issue=7 skipped reason=unauthorised", self.trigger_lines())
        self.reader.label(7, LABEL, actor=("alice", "User"), updated="3")
        self.ops.trigger_pass()
        self.assertEqual(len(self.spawned("plan")), 2)

    def test_the_memory_is_dropped_when_the_issue_no_longer_carries_the_label(self):
        self.reader.label(7, LABEL)
        self.fail_removal()
        self.start_build([7])
        self.ended()
        self.reader.label(7, "bug", updated="2")  # someone removed the label by hand
        self.ops.trigger_pass()
        self.assertEqual(self.ops._unremoved, {})

    def test_a_label_event_that_could_not_be_read_is_learned_at_the_first_sighting(self):
        self.reader.label(7, LABEL)
        self.fail_removal()
        self.start_build([7])
        self.ended()
        self.ops._unremoved[(REPO, 7)] = None  # as if the read at the removal had failed
        self.ops.trigger_pass()
        self.assertEqual(self.ops._unremoved[(REPO, 7)], "1")
        self.ops.trigger_pass()
        self.assertEqual(len(self.spawned("plan")), 1)

    def test_the_removal_of_several_labels_goes_on_after_the_first_failure(self):
        self.reader.label(7, LABEL)
        self.reader.label(8, LABEL)
        original = self.reader.remove_label

        def flaky(number, label):
            if number == 7:
                raise subprocess.CalledProcessError(1, ["gh"], stderr=CANARY)
            return original(number, label)

        self.reader.remove_label = flaky
        self.start_build([7, 8])
        self.assertEqual(self.reader.removed, [8])
        self.assertEqual(sorted(self.ops._unremoved), [(REPO, 7)])
        self.assertEqual(len([line for line in self.trigger_lines() if "label-remove failed" in line]), 1)


class CostTests(TriggerCase):
    def actors(self):
        return [call for call in self.reader.calls if isinstance(call, tuple) and call[0] == "actor"]

    def test_an_idle_cycle_authorises_only_as_many_issues_as_it_can_start(self):
        for number in range(1, 301):
            self.reader.label(number, LABEL)
        self.ops.trigger_pass()
        self.assertEqual(self.spawned("plan")[-1][-1], "--issues=1,2,3")
        self.assertEqual(len(self.actors()), 3)
        self.assertEqual(len(self.reader.calls), 2 + 3 * 2)  # repo, issues, then actor + permission for three

    def test_unauthorised_issues_ahead_of_the_cap_are_looked_up_once_not_every_cycle(self):
        for number in range(1, 301):
            self.reader.label(number, LABEL, actor=("mallory", "User"))
        self.reader.label(400, LABEL)
        self.ops.trigger_pass()
        self.assertEqual(self.spawned("plan")[-1][-1], "--issues=400")
        self.assertEqual(len(self.actors()), 301)  # one lookup per refusal, once
        self.manager.children.active_for_repo(REPO).process.returncode = 0
        before = len(self.actors())
        self.reader.label(401, LABEL)
        self.write_plan(self.run_of(self.spawned("plan")[-1]), [400], mark=True)
        self.time.now += 1
        self.ops.trigger_pass()  # the plan is finished: the run starts, nothing is looked up
        self.assertEqual(len(self.actors()), before)

    def test_retries_and_new_issues_are_capped_separately_and_retries_still_come_first(self):
        self.write_plan("auto-20270101T000000Z", list(range(100, 110)), state=True)
        for number in range(1, 11):
            self.reader.label(number, LABEL)
        for number in range(100, 110):
            self.reader.label(number, LABEL, "factory:blocked")
        self.ops.trigger_pass()
        self.assertEqual(len(self.actors()), 6)  # three of each kind
        self.assertEqual(self.spawned("plan"), [])
        self.assertEqual(len(self.spawned("run")), 1)


class BackoffResetTests(TriggerCase):
    def test_a_pass_that_reads_everything_and_finds_nothing_to_start_forgets_earlier_failures(self):
        self.reader.fail = RuntimeError(CANARY)
        self.ops.trigger_pass()
        self.time.now += 60
        self.ops.trigger_pass()
        self.assertTrue(self.trigger_lines()[-1].endswith("backoff=120s"))
        self.time.now += 120
        self.reader.fail = None
        self.ops.trigger_pass()  # no issue carries the label: a clean pass
        self.reader.fail = RuntimeError(CANARY)
        self.ops.trigger_pass()
        self.assertTrue(self.trigger_lines()[-1].endswith("backoff=60s"))  # not 240


# --- no text in any output --------------------------------------------------------------


class PrivacyTests(TriggerCase):
    def test_no_issue_text_reaches_the_output_in_any_path(self):
        self.reader.fail = subprocess.CalledProcessError(1, ["gh"], stderr=CANARY, output=CANARY)
        self.reader.label(1, LABEL)
        self.ops.trigger_pass()
        self.reader.fail = None
        self.reader.remove_error = RuntimeError(CANARY)
        self.time.now += 100
        self.start_build([1])
        self.runner.override[("retry",)] = mgr.CommandResult(1, CANARY.encode(), CANARY.encode())
        self.assertNotIn(CANARY, self.out.getvalue() + self.err.getvalue())
        self.assertTrue(self.trigger_lines())
        for line in self.trigger_lines():
            self.assertRegex(line, SAFE_LINE)

    def test_every_line_is_made_of_ids_and_fixed_words(self):
        self.reader.label(1, LABEL, "factory-ignore")
        self.reader.label(2, LABEL, actor=("bob", "User"))
        self.reader.label(3, LABEL, "factory:building")
        self.reader.label(4, LABEL)
        self.ops.trigger_pass()
        self.assertEqual(len(self.trigger_lines()), 4)
        for line in self.trigger_lines():
            self.assertRegex(line, SAFE_LINE)
            self.assertNotIn("bob", line)  # not even a login


# --- the wiring: settings, threads, the default reader ------------------------------------


class WiringTests(TriggerCase):
    def test_the_trigger_wants_a_thread_whenever_the_interval_is_positive(self):
        self.assertTrue(self.ops.trigger_wanted())
        self.assertFalse(self.new_ops(self.new_manager(trigger_seconds=0.0)).trigger_wanted())
        # Registering a repository (or turning auto_build on) after the manager booted needs no restart:
        # the thread exists whatever is registered now, and each pass re-reads the configuration.
        self.configure("auto_build = false\n", REPO)
        self.configure("auto_build = false\n", "org/other")
        self.assertTrue(self.ops.trigger_wanted())
        for name in ("org__name", "org__other"):
            (self.config_dir / "repos" / f"{name}.toml").unlink()
        self.assertTrue(self.ops.trigger_wanted())  # nothing registered yet

    def test_a_repository_registered_after_the_start_is_watched_by_the_next_pass(self):
        for name in ("org__name", "org__other"):
            (self.config_dir / "repos" / f"{name}.toml").unlink()
        self.ops.trigger_pass()
        self.assertEqual((self.spawner.calls, self.reader.calls), ([], []))
        self.configure("")
        self.reader.label(7, LABEL)
        self.ops.trigger_pass()
        self.assertEqual(self.spawned("plan")[-1][-1], "--issues=7")

    def test_the_interval_setting(self):
        env = {"FACTORY_CONFIG": "/etc/factory", "FACTORY_DATA": "/var/lib/factory"}
        self.assertEqual(Settings.from_env(env).trigger_seconds, 60.0)
        self.assertEqual(Settings.from_env({**env, "FACTORY_MANAGER_TRIGGER_SECONDS": "0"}).trigger_seconds, 0.0)
        self.assertEqual(Settings.from_env({**env, "FACTORY_MANAGER_TRIGGER_SECONDS": " 15 "}).trigger_seconds, 15.0)
        for bad in ("-1", "x", "nan", "inf", "86401"):
            with self.assertRaises(ValueError) as caught:
                Settings.from_env({**env, "FACTORY_MANAGER_TRIGGER_SECONDS": bad})
            self.assertIn("FACTORY_MANAGER_TRIGGER_SECONDS", str(caught.exception))
            self.assertNotIn(bad, str(caught.exception))

    def test_the_default_reader_uses_the_ambient_login_without_an_app(self):
        cfg = mgr.load_repo_config(REPO, config_dir=self.config_dir)
        reader = github_reader(self.manager, cfg)
        self.assertIsInstance(reader, trigger.GithubReader)
        self.assertIsInstance(reader._auth, AmbientAuth)
        self.assertEqual(reader.repo, REPO)

    def test_the_default_reader_uses_the_apps_installation_token_when_one_is_configured(self):
        self.configure("installation_id = 4242\n")
        env = {
            "FACTORY_GITHUB_APP_ID": "1", "FACTORY_GITHUB_APP_SLUG": "factory-bot", "FACTORY_GITHUB_APP_USER_ID": "2",
            "FACTORY_TOKEN_BROKER": "broker --x",
        }
        manager = Manager(self.manager.settings, env=env, runner=self.runner, spawner=self.spawner, stdout=self.out, stderr=self.err)
        cfg = mgr.load_repo_config(REPO, config_dir=self.config_dir)
        reader = github_reader(manager, cfg)
        self.assertIsInstance(reader._auth, AppAuth)
        self.assertEqual(reader._auth.installation_id, 4242)
        self.assertEqual(reader._auth.repo, REPO)

    def test_a_half_configured_app_fails_the_pass_with_one_line_and_never_falls_back_to_the_ambient_login(self):
        manager = Manager(
            self.manager.settings, env={"FACTORY_GITHUB_APP_ID": "1"}, runner=self.runner, spawner=self.spawner,
            lock_factory=self.lease, stdout=self.out, stderr=self.err,
        )
        ops_ = Operations(manager, clock=self.time.clock, wait=self.time.wait)
        ops_.trigger_pass()
        self.assertEqual(
            [line for line in self.lines() if line.startswith("trigger")][:1],
            [f"trigger repo={REPO} pass failed error=ValueError backoff=60s"],
        )
        self.assertEqual(self.spawner.calls, [])

    def test_a_reader_is_made_once_per_repository(self):
        made = []
        ops_ = self.new_ops(self.manager, github=lambda cfg: made.append(cfg.repo) or self.readers[cfg.repo])
        for _ in range(3):
            ops_.trigger_pass()
        self.assertEqual(sorted(made), ["org/name", "org/other"])


class FakeGhEndToEndTests(OpsCase):
    """The real reader against a fake ``gh`` binary, the real manager start functions, a fake CLI: a whole cycle."""

    def setUp(self):
        super().setUp()
        (self.config_dir / "repos" / "org__other.toml").unlink()  # the fake gh serves one repository
        self.manager.env[epic.ENV_SWITCH] = "off"  # the fake gh does not serve the epic gate's reads

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.gh_root = Path(temp.name)
        bin_dir = self.gh_root / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text(f"#!{sys.executable}\n" + FAKE_GH)
        gh.chmod(0o755)
        patcher = patch.dict(
            os.environ,
            {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_DIR": str(self.gh_root), "FAKE_REPO": REPO},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.github = {
            "repo": {"archived": False, "private": True, "visibility": "private"},
            "issues": [{"number": 7, "pr": False, "labels": [LABEL, "bug"], "updated": "2027-01-01T00:00:00Z"}],
            "events": {"7": [
                {"event": "labeled", "label": "bug", "login": "mallory", "type": "User"},
                {"event": "labeled", "label": LABEL, "login": "alice", "type": "User"},
            ]},
            "permissions": {"alice": "write", "mallory": "read"},
        }
        self.save()
        self.ops = self.new_ops(self.manager)  # the default factory: github_reader, no App configured

    def save(self):
        (self.gh_root / "github.json").write_text(json.dumps(self.github))

    def test_a_label_from_a_writer_is_planned_consumed_and_run_through_the_real_reader(self):
        self.ops.trigger_pass()
        [plan] = [argv for argv in self.spawner.argvs if "plan" in argv]
        self.assertEqual(plan[-1], "--issues=7")
        run_name = Path(plan[plan.index("--run-dir") + 1]).name
        directory = self.run_dir(run_name)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "plan.json").write_text(json.dumps({"issues": [{"number": 7, "title": CANARY}], "jobs": []}))
        self.spawner.processes[-1].returncode = 0
        self.ops.trigger_pass()
        self.assertEqual(json.loads((self.gh_root / "github.json").read_text())["removed"], [["7", LABEL]])
        self.assertEqual(len([argv for argv in self.spawner.argvs if "run" in argv]), 1)
        self.assertEqual(self.warnings(), [])

    def started_run_waiting(self, dependency_pull=11):
        """A started run: job-1 published pull request 11, job-2 waits for it (the supervisor has exited)."""
        directory = self.run_dir("auto-20270101T000000Z")
        directory.mkdir(parents=True)
        jobs = [
            {"id": "job-1", "issues": [1], "depends_on": []},
            {"id": "job-2", "issues": [2], "depends_on": ["job-1"]},
        ]
        (directory / "plan.json").write_text(json.dumps({"issues": [], "jobs": jobs}))
        states = {
            "job-1": {"status": "pr_ready", "pr": f"https://github.com/{REPO}/pull/{dependency_pull}"},
            "job-2": {"status": "queued"},
        }
        (directory / "state.json").write_text(json.dumps({"jobs": states}))
        self.github["issues"] = []
        self.github["pulls"] = {"11": {"state": "open", "merged": False}}
        self.save()

    def test_the_real_reader_resumes_a_waiting_run_once_its_dependency_merges(self):
        self.started_run_waiting()
        self.ops.trigger_pass()
        self.ops.trigger_pass()
        self.assertEqual([argv for argv in self.spawner.argvs if "run" in argv], [])
        self.assertEqual(len([line for line in self.lines() if "skipped" in line]), 1)
        self.github["pulls"]["11"] = {"state": "closed", "merged": True}
        self.save()
        self.ops.trigger_pass()
        self.ops.trigger_pass()
        [argv] = [argv for argv in self.spawner.argvs if "run" in argv]
        self.assertEqual(Path(argv[argv.index("--run-dir") + 1]).name, "auto-20270101T000000Z")
        self.assertEqual(self.warnings(), [])

    def test_the_real_reader_blocks_a_job_whose_dependency_was_closed_unmerged(self):
        self.started_run_waiting()
        self.github["pulls"]["11"] = {"state": "closed", "merged": False}
        self.save()
        self.runner.override[("block",)] = ops_text("blocked\n")
        self.ops.trigger_pass()
        self.assertEqual(self.spawner.calls, [])
        self.assertEqual(
            self.lines(), [f"trigger repo={REPO} run=auto-20270101T000000Z job=job-2 blocked dependency=job-1 reason=dependency-closed"]
        )
        [argv] = [argv for argv in self.runner.argvs if "block" in argv]
        self.assertEqual(argv[-3:], ["block", "job-2", "--reason=dependency job-1 was closed without merging"])

    def test_a_label_from_a_reader_is_left_alone(self):
        self.github["events"]["7"][-1]["login"] = "mallory"
        self.save()
        self.ops.trigger_pass()
        self.assertEqual(self.spawner.calls, [])
        self.assertEqual(self.lines(), [f"trigger repo={REPO} issue=7 skipped reason=unauthorised"])

    def test_the_gh_failure_is_one_line_with_the_exception_type_only(self):
        self.github["fail_all"] = CANARY
        self.save()
        self.ops.trigger_pass()
        self.assertEqual(self.lines(), [f"trigger repo={REPO} pass failed error=CalledProcessError backoff=60s"])


class TriggerThreadTests(OpsCase):
    """Real threads and events, intervals far longer than any test; cleanup always stops them."""

    def setUp(self):
        super().setUp()
        self.manager = self.new_manager(lease_wait_seconds=3600.0, poll_seconds=3600.0, janitor_seconds=86400.0,
                                        trigger_seconds=3600.0)
        self.addCleanup(self.stop_threads)

    def stop_threads(self):
        self.manager.closing.set()
        if self.manager.operations is not None:
            self.assertEqual(self.manager.operations.join(30), 0)

    def names(self, operations):
        return sorted(thread.name for thread in operations.threads)

    def test_four_threads_with_the_trigger_and_it_is_a_thread_like_the_others(self):
        self.manager.start_operations()
        operations = self.manager.operations
        self.assertEqual(
            self.names(operations),
            ["factory-manager-guardrail", "factory-manager-janitor", "factory-manager-recovery", "factory-manager-trigger"],
        )
        self.assertFalse(any(thread.daemon for thread in operations.threads))
        began = time.monotonic()
        self.manager.terminate_children(grace=0.1)
        self.assertLess(time.monotonic() - began, 4.0)
        self.assertEqual([t for t in operations.threads if t.is_alive()], [])
        self.assertEqual(self.warnings(), [])

    def test_interval_zero_creates_no_trigger_thread_and_no_poll(self):
        manager = self.new_manager(trigger_seconds=0.0)
        manager.start_operations()
        self.addCleanup(manager.terminate_children, 0.1)
        self.assertEqual(
            self.names(manager.operations),
            ["factory-manager-guardrail", "factory-manager-janitor", "factory-manager-recovery"],
        )

    def test_the_trigger_thread_exists_even_when_no_repository_is_registered_or_watching(self):
        for name in ("org__name", "org__other"):
            (self.config_dir / "repos" / f"{name}.toml").write_text(f'repo = "{name.replace("__", "/")}"\nauto_build = false\n')
        self.manager.start_operations()
        self.assertIn("factory-manager-trigger", self.names(self.manager.operations))
        manager = self.new_manager()
        for name in ("org__name", "org__other"):
            (self.config_dir / "repos" / f"{name}.toml").unlink()
        manager.start_operations()
        self.addCleanup(manager.terminate_children, 0.1)
        self.assertIn("factory-manager-trigger", self.names(manager.operations))

    def test_the_operations_switch_off_starts_no_trigger_either(self):
        manager = self.new_manager(operations=False)
        manager.start_operations()
        self.assertIsNone(manager.operations)

    def test_the_trigger_thread_passes_once_per_interval_and_only_after_the_restart_pass(self):
        order, waits = [], []

        def wait(seconds):
            if threading.current_thread().name != "factory-manager-trigger":
                return True  # the other loops end at once
            waits.append(seconds)
            return len(waits) > 3  # three passes, then the loop is told to stop

        operations = Operations(self.manager, wait=wait, policy=lambda cfg: ops.Policy())
        operations.recover_all = lambda: order.append("recovered")
        operations.check_budgets = lambda: None
        operations.sweep = lambda: None
        operations.trigger_pass = lambda: order.append("trigger")
        self.manager.operations = operations
        operations.start()
        for thread in operations.threads:
            thread.join(10)
        self.assertEqual(order[0], "recovered")
        self.assertEqual(order.count("trigger"), 3)
        self.assertEqual(waits[:3], [3600.0] * 3)


class SharedPathResumeTests(TriggerCase):
    """The same loop resumes a run whose only waits are ``shared paths: <job>`` waits."""

    def setUp(self):
        super().setUp()
        self.labelled = []
        self.runner.override[("block",)] = self.block

    block = DependencyResumeTests.block
    make = DependencyResumeTests.make
    runs = DependencyResumeTests.runs
    skips = DependencyResumeTests.skips
    restart = DependencyResumeTests.restart

    def shared(self, jobs=None, run=RUN_A):
        """job-1 holds a pull request; every other (queued) job waits behind it for shared paths."""
        jobs = jobs or {"job-1": ("pr_ready", [], 11), "job-2": ("queued", [], None)}
        directory = self.make(run, jobs)
        data = json.loads((directory / "state.json").read_text())
        for ident, (status, _, pull) in jobs.items():
            if status == "queued":
                data["jobs"][ident]["waiting_for"] = "shared paths: job-1"
            if pull is not None:
                data["jobs"][ident]["branch"] = f"factory/{run}/{ident}"
        (directory / "state.json").write_text(json.dumps(data))
        return directory

    def test_the_sibling_merging_resumes_the_idle_run_exactly_once(self):
        self.shared()
        self.reader.pulls = {11: "open"}
        self.ops.trigger_pass()
        self.assertEqual(self.runs(), [])
        self.assertEqual(self.skips(), [f"trigger repo={REPO} run={RUN_A} skipped reason=dependency"])
        self.reader.pulls[11] = "merged"
        self.ops.trigger_pass()
        [argv] = self.runs()
        self.assertEqual(argv[-5:], ["run", "--concurrency", "1", "--timeout", "1800"])
        self.assertEqual(self.trigger_lines()[-1], f"trigger repo={REPO} run={RUN_A} jobs=1 started resume=dependencies")
        for _ in range(3):
            self.ops.trigger_pass()
        self.assertEqual(len(self.runs()), 1)
        self.assertEqual(self.runner.commands("block"), [])

    def test_a_sibling_closed_unmerged_releases_the_wait_instead_of_blocking(self):
        self.shared()
        self.reader.pulls = {11: "closed"}
        self.ops.trigger_pass()
        self.assertEqual(len(self.runs()), 1)
        self.assertEqual(self.runner.commands("block"), [])

    def test_a_sibling_that_failed_without_a_branch_releases_the_wait_without_asking_github(self):
        directory = self.shared()
        data = json.loads((directory / "state.json").read_text())
        data["jobs"]["job-1"] = {"status": "failed", "attempt": 1, "error": "boom"}
        (directory / "state.json").write_text(json.dumps(data))
        self.ops.trigger_pass()
        self.assertEqual(len(self.runs()), 1)
        self.assertEqual(self.reader.kinds().count("pull"), 0)

    def test_a_sibling_that_failed_with_a_branch_and_no_pull_request_keeps_the_wait(self):
        directory = self.shared()
        data = json.loads((directory / "state.json").read_text())
        data["jobs"]["job-1"] = {"status": "failed", "attempt": 1, "branch": "factory/x/job-1"}
        (directory / "state.json").write_text(json.dumps(data))
        self.ops.trigger_pass()
        self.assertEqual(self.spawner.calls, [])

    def test_a_job_with_two_siblings_to_wait_for_resumes_per_sibling_not_per_job(self):
        self.shared({"job-1": ("pr_ready", [], 11), "job-2": ("pr_ready", [], 12), "job-3": ("queued", [], None)})
        self.reader.pulls = {11: "merged", 12: "open"}
        self.ops.trigger_pass()
        self.assertEqual(len(self.runs()), 1)  # job-3 waits for job-1 only; job-2 is not its blocker yet
        # The run moved the wait on to job-2 and ended cleanly.
        path = self.run_dir(RUN_A) / "state.json"
        data = json.loads(path.read_text())
        data["jobs"]["job-1"]["status"] = "merged"
        data["jobs"]["job-3"]["waiting_for"] = "shared paths: job-2"
        path.write_text(json.dumps(data))
        self.spawner.processes[-1].returncode = 0
        self.ops.trigger_pass()
        self.assertEqual(len(self.runs()), 1)
        self.reader.pulls[12] = "merged"
        self.ops.trigger_pass()
        self.assertEqual(len(self.runs()), 2)  # the second sibling is answered at once, not after the retry delay

    def test_a_restart_in_between_changes_nothing(self):
        self.shared()
        self.reader.pulls = {11: "merged"}
        self.ops.trigger_pass()
        self.assertEqual(len(self.runs()), 1)
        self.restart()
        self.ops.trigger_pass()
        self.assertEqual(len(self.runs()), 1)


if __name__ == "__main__":
    unittest.main()

# --- idle reconcile of published pull requests ----------------------------------------


class IdleReconcileTests(TriggerCase):
    """The janitor and the trigger pass ask the CLI's ``reconcile`` for runs with a published pull request."""

    make = DependencyResumeTests.make

    def published(self, run=RUN_A):
        directory = self.make(run, {"job-1": ("pr_ready", [], 11)})
        for name in ("plan.json", "state.json"):  # fresh, so that the janitor does not call the run abandoned
            os.utime(directory / name, (NOW, NOW))
        os.utime(directory, (NOW, NOW))
        return directory

    def reconciles(self):
        return self.runner.commands("reconcile")

    def test_the_janitor_reconciles_a_run_it_keeps_and_the_trigger_pass_shares_the_pacing(self):
        self.published()
        self.runner.reports[(REPO, RUN_A)] = ops_report(("job-1", "pr_ready"))
        self.ops.sweep()
        self.assertEqual(self.reconciles(), [(REPO, RUN_A)])
        self.assertTrue(self.run_dir(RUN_A).is_dir())  # reconciled, not purged
        self.ops.trigger_pass()
        self.ops.sweep()
        self.assertEqual(len(self.reconciles()), 1)  # not again within RECONCILE_SECONDS
        self.time.now += ops.RECONCILE_SECONDS + 1
        self.ops.trigger_pass()
        self.assertEqual(len(self.reconciles()), 2)

    def test_the_trigger_pass_reconciles_without_a_supervisor_and_never_beside_one(self):
        self.published()
        self.add_child(REPO, RUN_A, "run")
        self.ops.trigger_pass()
        self.assertEqual(self.reconciles(), [])
        self.manager.children._active.clear()
        self.ops.trigger_pass()
        self.assertEqual(self.reconciles(), [(REPO, RUN_A)])

    def test_a_run_without_a_published_pull_request_is_never_reconciled(self):
        self.make(RUN_A, {"job-1": ("running", [], None), "job-2": ("pr_ready", [], None)})
        self.ops.trigger_pass()
        self.ops.sweep()
        self.assertEqual(self.reconciles(), [])

    def test_a_refusal_is_one_fixed_rate_limited_line_and_the_loop_goes_on(self):
        self.published()
        self.runner.override[("reconcile",)] = failure(1)
        self.ops.trigger_pass()
        self.time.now += ops.RECONCILE_SECONDS + 1
        self.ops.trigger_pass()
        self.assertEqual(len(self.reconciles()), 2)
        self.assertEqual(
            [line for line in self.lines() if line.startswith("reconcile-skipped")],
            [f"reconcile-skipped repo={REPO} run={RUN_A} rc=1 cause=other"],
        )
        self.assertEqual(self.warnings(), [])

    def test_a_reconcile_refusal_carries_the_fixed_cause_and_none_of_the_cli_text(self):
        self.published()
        stderr = f"factory: another factory supervisor holds the repository lock {CANARY}\nfactory: {CANARY}\n".encode()
        self.runner.override[("reconcile",)] = ops.CommandResult(1, b"", stderr)
        self.ops.trigger_pass()
        self.runner.override[("reconcile",)] = ops.CommandResult(
            1, b"", b"factory: another factory supervisor holds the repository lock\n"
        )
        self.time.now += ops.RECONCILE_SECONDS + 1
        self.ops.trigger_pass()
        self.runner.override[("reconcile",)] = ops.CommandResult(1, b"", f"factory: git pull failed: {CANARY}\n".encode())
        self.time.now += ops.RECONCILE_SECONDS + 1
        self.ops.trigger_pass()
        lines = [line for line in self.lines() if line.startswith("reconcile-skipped")]
        self.assertEqual(
            lines,
            [
                f"reconcile-skipped repo={REPO} run={RUN_A} rc=1 cause=other",
                f"reconcile-skipped repo={REPO} run={RUN_A} rc=1 cause=lease_busy",
                f"reconcile-skipped repo={REPO} run={RUN_A} rc=1 cause=git_failed",
            ],
        )
        self.assertNotIn(CANARY, "\n".join(self.lines() + self.warnings()))


class UnflushedObservationTests(TriggerCase):
    """A job that ended blocked, failed or waiting for a person has no pull request to bring its run back to the
    idle reconcile; the observations a refused flush left behind do (``observed_issues.pending``)."""

    make = DependencyResumeTests.make

    def reconciles(self):
        return self.runner.commands("reconcile")

    def observed(self, status="blocked", run=RUN_A, **extra):
        """A started run whose one job ended with a stored observation and no pull request."""
        directory = self.make(run, {"job-1": (status, [], None)})
        for name in ("plan.json", "state.json"):
            os.utime(directory / name, (NOW, NOW))
        os.utime(directory, (NOW, NOW))
        with contextlib.redirect_stderr(io.StringIO()):
            kept = observed_issues.parse_observations(
                [{"kind": "bug_found", "title": "The sandbox has no jq binary", "detail": "Tests need jq."}],
                "job-1",
                "build",
            )
        path = directory / "state.json"
        data = json.loads(path.read_text())
        data["jobs"]["job-1"].update(observed=kept, **extra)
        path.write_text(json.dumps(data))
        return directory, kept

    def test_each_job_that_ended_without_a_pull_request_brings_its_run_back(self):
        for number, status in enumerate(("blocked", "failed", "needs_human_publish")):
            with self.subTest(status=status):
                self.time.now += ops.RECONCILE_SECONDS + 1
                self.observed(status)
                self.ops.trigger_pass()
                self.assertEqual(self.reconciles(), [(REPO, RUN_A)] * (number + 1))

    def test_it_is_paced_like_every_other_idle_reconcile(self):
        self.observed()
        self.ops.trigger_pass()
        self.ops.sweep()
        self.assertEqual(len(self.reconciles()), 1)
        self.time.now += ops.RECONCILE_SECONDS + 1
        self.ops.trigger_pass()
        self.assertEqual(len(self.reconciles()), 2)

    def test_a_run_that_is_still_working_is_not_asked_for_them(self):
        self.observed("running")
        self.ops.trigger_pass()
        self.assertEqual(self.reconciles(), [])

    def test_nothing_is_asked_once_every_observation_is_recorded_as_settled(self):
        directory, kept = self.observed()
        (directory / observed_issues.DONE_FILE).write_text(
            json.dumps({"version": 1, "done": {"job-1": [item["fp"] for item in kept]}})
        )
        self.ops.trigger_pass()
        self.assertEqual(self.reconciles(), [])

    def test_a_record_of_another_job_or_garbage_settles_nothing(self):
        directory, kept = self.observed()
        done = directory / observed_issues.DONE_FILE
        done.write_text(json.dumps({"version": 1, "done": {"job-2": [item["fp"] for item in kept]}}))
        self.ops.trigger_pass()
        self.assertEqual(len(self.reconciles()), 1)
        self.time.now += ops.RECONCILE_SECONDS + 1
        done.write_text("[not json")
        self.ops.trigger_pass()
        self.assertEqual(len(self.reconciles()), 2)

    def test_nothing_is_asked_when_issues_for_observations_are_off(self):
        self.observed()
        self.configure("observed_issues = false\n")
        self.ops.trigger_pass()
        self.assertEqual(self.reconciles(), [])
        self.configure()
        self.manager.env[observed_issues.ENV_SWITCH] = "off"
        self.ops.trigger_pass()
        self.assertEqual(self.reconciles(), [])
        del self.manager.env[observed_issues.ENV_SWITCH]
        self.ops.trigger_pass()
        self.assertEqual(len(self.reconciles()), 1)


# --- the planner spend cap ------------------------------------------------------------


class PlannerSpendCapTests(TriggerCase):
    """``planner_max_usd_per_hour``: no planner starts once the usage recorded in the last hour reaches the cap."""

    REASON = f"trigger repo={REPO} skipped reason={ops.PLANNER_SPEND_REASON}"

    def usage(self, run, cost, age, name="planner-usage.json"):
        """A planner usage record in ``run`` whose file was written ``age`` seconds before the fake clock."""
        directory = self.run_dir(run)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(json.dumps({"total_cost_usd": cost}))
        moment = self.time.now - age
        os.utime(directory / name, (moment, moment))

    def skips(self):
        return [line for line in self.trigger_lines() if "skipped" in line]

    def test_spend_at_the_cap_starts_no_plan_and_logs_one_line(self):
        self.usage("auto-20270101T000000Z", 1.5, age=600)
        self.usage("auto-20270101T010000Z", 0.5, age=60, name="planner-usage-extra.json")  # 2.0 in all: at the cap
        self.reader.label(7, LABEL)
        for _ in range(3):
            self.ops.trigger_pass()
        self.assertEqual(self.spawner.calls, [])
        self.assertEqual(self.skips(), [self.REASON])
        self.assertEqual(self.reader.labels_of(7), [LABEL])
        counter = self.manager.metrics["factory_watcher_skips_total"]
        self.assertEqual(counter.value(repo=REPO, skip_reason=ops.PLANNER_SPEND_REASON), 3)  # counted every pass
        self.assertIn(ops.PLANNER_SPEND_REASON, metrics.SKIP_REASONS)  # not folded into "other"

    def test_spend_below_the_cap_still_plans(self):
        self.usage("auto-20270101T000000Z", 1.99, age=60)
        self.reader.label(7, LABEL)
        self.ops.trigger_pass()
        self.assertEqual(len(self.spawned("plan")), 1)

    def test_the_cap_is_per_repository(self):
        self.usage("auto-20270101T000000Z", 5.0, age=60)
        self.readers["org/other"].label(7, LABEL)
        self.ops.trigger_pass()
        self.assertEqual(len(self.spawned("plan")), 1)
        self.assertIn("org/other", self.spawned("plan")[0])

    def test_planning_resumes_once_the_records_leave_the_one_hour_window(self):
        self.usage("auto-20270101T000000Z", 2.5, age=1800)
        self.reader.label(7, LABEL)
        self.ops.trigger_pass()
        self.assertEqual(self.spawned("plan"), [])
        self.time.now += 1799  # the record is 3599 s old: still inside the hour
        self.ops.trigger_pass()
        self.assertEqual(self.spawned("plan"), [])
        self.time.now += 2  # now 3601 s old
        self.ops.trigger_pass()
        self.assertEqual(len(self.spawned("plan")), 1)

    def disabled(self, value):
        self.configure(f"planner_max_usd_per_hour = {value}\n")
        self.usage("auto-20270101T000000Z", 100.0, age=60)
        self.reader.label(7, LABEL)
        self.ops.trigger_pass()
        self.assertEqual(len(self.spawned("plan")), 1)
        self.assertEqual(self.skips(), [])

    def test_zero_disables_the_cap(self):
        self.disabled("0")

    def test_off_disables_the_cap(self):
        self.disabled('"off"')

    def test_a_configured_cap_replaces_the_default(self):
        self.configure("planner_max_usd_per_hour = 10\n")
        self.usage("auto-20270101T000000Z", 5.0, age=60)
        self.reader.label(7, LABEL)
        self.ops.trigger_pass()
        self.assertEqual(len(self.spawned("plan")), 1)

    def test_a_failed_session_without_a_record_and_unreadable_records_count_for_nothing(self):
        directory = self.run_dir("auto-20270101T000000Z")
        directory.mkdir(parents=True)
        (directory / "planner-usage.json").write_text("not json")
        (directory / "planner-usage-x.json").write_text(json.dumps({"total_cost_usd": "9"}))
        self.reader.label(7, LABEL)
        self.ops.trigger_pass()
        self.assertEqual(len(self.spawned("plan")), 1)

"""tools/effort_ab.py: the effort A/B harness (issues 132 and 154), against a fake client and a fake data directory.

Nothing here calls GitHub or reads a real run: ``FakeWorld`` plays the sandbox repository and Factory's label trigger (when the trigger
label is added it writes a run directory with a job, like the manager's plan and run would) and the harness reads those files with the
real readers. Every free-text position (task title, body, acceptance, expected paths, session subtypes) carries a canary that must never
be printed, and the token environment variable carries one that must never reach the output either.
"""

import importlib.util
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("effort_ab_under_test", ROOT / "tools" / "effort_ab.py")
ab = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ab)

CANARY = "CANARY-FREE-TEXT-do-not-print"
TOKEN = "ghp_CANARYTOKENdoNotPrint"
REPO = "org/sandbox"
COSTS = {"low": 1.0, "medium": 2.0, "high": 4.0}  # cost of the build session per effort in the fake world


def write_tasks(directory, count=2):
    tasks = [
        {"id": f"task-{n}", "title": f"{CANARY} title {n}", "body": f"{CANARY} body {n}", "expected_paths": [f"src/{CANARY}-{n}.py", "README.md"],
         "acceptance": [f"{CANARY} acceptance"]}
        for n in range(1, count + 1)
    ]
    path = Path(directory) / "tasks.json"
    path.write_text(json.dumps(tasks))
    return str(path)


class FakeWorld:
    """The sandbox repository, Factory's trigger and the data directory, in memory and on disk. It has NO merge method on purpose:
    any call the harness makes that is not one of the methods below raises, so a merge (or any other new call) fails the test."""

    def __init__(self, data, outcome=None, auto_merge=False, merged=False, branch="factory/job"):
        self.data, self.issues, self.prs, self.calls, self.deleted = Path(data), {}, {}, [], []
        self.outcome = outcome or (lambda issue, effort: "pr_ready")
        self.auto_merge, self.merged, self.branch = auto_merge, merged, branch
        self.fail_after_create = False
        self.fail_add_label = False
        self.number = 0

    def __getattr__(self, name):
        raise AssertionError(f"the harness called {name}, which the client does not offer")

    # -- the client
    def refresh(self):
        self.calls.append("refresh")

    def ensure_label(self, name, color, description):
        self.calls.append(("ensure_label", name))

    def create_issue(self, title, body, labels):
        self.number += 1
        self.issues[self.number] = {"title": title, "body": body, "labels": list(labels), "state": "OPEN"}
        self.calls.append(("create_issue", self.number, tuple(labels)))
        if self.fail_after_create:
            self.fail_after_create = False
            raise ab.ClientError("create_issue")  # created, but the answer was lost
        return self.number

    def add_label(self, number, label):
        self.calls.append(("add_label", number, label))
        if self.fail_add_label:
            raise ab.ClientError("add_label")
        self.issues[number]["labels"].append(label)
        self.on_trigger(number)

    def issue(self, number):
        issue = self.issues[number]
        return {"state": issue["state"], "labels": list(issue["labels"])}

    def find_issues(self, label):
        return [{"number": n, "title": i["title"], "labels": list(i["labels"])} for n, i in self.issues.items() if label in i["labels"]]

    def pr(self, number):
        self.calls.append(("pr", number))
        pr = self.prs[number]
        return {**pr, "state": "MERGED" if self.merged else pr["state"], "autoMergeRequest": {"enabledBy": "x"} if self.auto_merge else None}

    def close_pr(self, number):
        self.calls.append(("close_pr", number))
        self.prs[number]["state"] = "CLOSED"

    def close_issue(self, number):
        self.calls.append(("close_issue", number))
        self.issues[number]["state"] = "CLOSED"

    def delete_branch(self, name):
        self.calls.append(("delete_branch", name))
        self.deleted.append(name)

    # -- Factory
    def effort_of(self, number):
        return next(label.split(":")[1] for label in self.issues[number]["labels"] if label.startswith("factory-effort:"))

    def on_trigger(self, number):
        effort = self.effort_of(number)
        status = self.outcome(number, effort)
        run = self.data / "repos" / "org" / "sandbox" / "runs" / f"auto-{number:04d}"
        run.mkdir(parents=True)
        entry = {"model": "claude-sonnet-5-5", "effort": effort, "model_source": "issue", "effort_source": "issue"}
        plan = {"version": 1, "jobs": [{"id": f"job-{number}", "issues": [number], "title": CANARY, "choice": {"build": entry, "verify": entry}}]}
        sessions = [{
            "phase": "build", "model": "claude-sonnet-5-5", "subtype": CANARY,
            "usage": {"input_tokens": 10, "output_tokens": 500, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000,
                      "output_tokens_details": {"thinking_tokens": 200}},
            "model_usage": {"claude-sonnet-5-5": {"costUSD": COSTS[effort]}, CANARY: {"costUSD": 1}},
            "total_cost_usd": COSTS[effort], "num_turns": 7, "duration_ms": 12000}]
        job = {"status": status, "attempt": 1, "usage": sessions, "commit": "abc", "error": CANARY,
               "verification": {"complete": True, "commit": "abc"}, "review": {"pass": False, "findings": [CANARY, CANARY]}}
        if status in ab.PUBLISHED:
            job["pr"] = f"https://github.com/{REPO}/pull/{100 + number}"
            self.prs[100 + number] = {
                "state": "OPEN", "headRefName": self.branch, "baseRefName": "main", "isCrossRepository": False,
                "statusCheckRollup": [{"__typename": "StatusContext", "context": "factory/gate", "state": "SUCCESS"},
                                      {"__typename": "CheckRun", "name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}],
                "files": [{"path": "README.md"}, {"path": "other.py"}]}
        (run / "plan.json").write_text(json.dumps(plan))
        (run / "state.json").write_text(json.dumps({"version": 1, "jobs": {f"job-{number}": job}}))
        (run / "planner-usage.json").write_text(json.dumps({"total_cost_usd": 0.5}))


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.tasks = write_tasks(self.root)
        self.state = str(self.root / "state.json")
        self.clock = [1_000_000.0]
        self.lines = []
        env = mock.patch.dict(os.environ, {"GH_TOKEN": TOKEN})
        env.start()
        self.addCleanup(env.stop)

    def now(self):
        return self.clock[0]

    def sleep(self, seconds):
        self.clock[0] += seconds

    def argv(self, command="run", *extra):
        return [command, "--tasks", self.tasks, "--state", self.state, "--repo", REPO, "--data", str(self.data), "--confirm-sandbox",
                "--seed", "7", "--poll-seconds", "1", "--efforts", "low,medium,high", *extra]

    def go(self, world, *argv, command="run"):
        self.lines.clear()
        code = ab.main(self.argv(command, *argv), client=world, say=self.lines.append, now=self.now, sleep=self.sleep)
        return code

    def output(self):
        return "\n".join(self.lines)

    def state_doc(self):
        return json.loads(Path(self.state).read_text())

    def world(self, **kwargs):
        return FakeWorld(self.data, **kwargs)


class PlanTests(Base):
    def tasks_of(self, count=3):
        return ab.load_tasks(write_tasks(self.root, count))

    def test_the_same_seed_gives_the_same_order_whatever_the_order_of_the_tasks_and_another_seed_differs(self):
        tasks = self.tasks_of(4)
        first = ab.make_arms(tasks, ["low", "medium", "high"], 2, 7)
        self.assertEqual(first, ab.make_arms(tasks, ["low", "medium", "high"], 2, 7))
        self.assertEqual(first, ab.make_arms(list(reversed(tasks)), ["high", "low", "medium"], 2, 7))
        self.assertNotEqual([a["key"] for a in first], [a["key"] for a in ab.make_arms(tasks, ["low", "medium", "high"], 2, 8)])

    def test_every_task_effort_and_repeat_is_one_arm_and_a_block_holds_all_the_efforts_together(self):
        arms = ab.make_arms(self.tasks_of(3), ["low", "medium", "high"], 2, 3)
        self.assertEqual(len(arms), 18)
        self.assertEqual(len({a["key"] for a in arms}), 18)
        for start in range(0, 18, 3):
            block = arms[start:start + 3]
            self.assertEqual(len({(a["task"], a["repeat"]) for a in block}), 1)
            self.assertEqual({a["effort"] for a in block}, {"low", "medium", "high"})

    def test_the_order_is_not_the_task_file_order_for_every_seed(self):
        tasks = self.tasks_of(6)
        orders = {tuple(a["task"] for a in ab.make_arms(tasks, ["low", "high"], 1, seed)[::2]) for seed in range(20)}
        self.assertGreater(len(orders), 5)

    def test_the_issue_text_is_the_same_for_every_effort(self):
        task = self.tasks_of(1)[0]
        self.assertEqual(ab.issue_text(task), ab.issue_text(dict(task)))
        self.assertIn("Acceptance criteria", ab.issue_text(task)[1])

    def test_a_dry_run_prints_the_plan_makes_no_call_and_writes_nothing(self):
        self.lines.clear()
        with mock.patch.object(ab, "Client", side_effect=AssertionError("no client in a dry run")):
            code = ab.main(["run", "--dry-run", "--tasks", self.tasks, "--seed", "7", "--repeats", "2"], say=self.lines.append)
        self.assertEqual(code, 0)
        text = self.output()
        self.assertIn("arms=12", text)
        self.assertIn("seed=7", text)
        self.assertEqual(sum(1 for line in self.lines if line.startswith("effort_ab: arm ")), 12)
        self.assertEqual(sorted(os.listdir(self.root)), ["data", "tasks.json"])
        self.assertNotIn(CANARY, text)

    def test_bad_task_files_are_refused_with_a_fixed_sentence(self):
        for document in ([], [{"id": "A b", "title": "t", "body": "b"}], [{"id": "a", "title": "t", "body": "b", "extra": 1}],
                         [{"id": "a", "title": "t", "body": "b", "expected_paths": ["../x"]}],
                         [{"id": "a", "title": "t", "body": "b"}, {"id": "a", "title": "t", "body": "b"}], "x"):
            path = self.root / "bad.json"
            path.write_text(json.dumps(document))
            with self.assertRaises(ab.Refused):
                ab.load_tasks(str(path))


class RunTests(Base):
    def test_a_run_makes_one_identical_labelled_issue_per_arm_and_the_trigger_label_comes_last(self):
        world = self.world()
        self.assertEqual(self.go(world, "--repeats", "1", "--budget-usd", "1000"), 0)
        self.assertEqual(len(world.issues), 6)
        by_task = {}
        for number, issue in world.issues.items():
            by_task.setdefault(issue["title"], set()).add(issue["body"])
            name = self.state_doc()["name"]
            self.assertEqual(sorted(l for l in issue["labels"] if not l.startswith("factory-effort:")),
                             sorted(["factory-model:sonnet", f"effort-ab:{name}", "factory-build"]))
        self.assertTrue(all(len(bodies) == 1 for bodies in by_task.values()))
        for number in world.issues:
            created = next(c for c in world.calls if c[0] == "create_issue" and c[1] == number)
            self.assertNotIn("factory-build", created[2])
            self.assertLess(world.calls.index(created), world.calls.index(("add_label", number, "factory-build")))
        self.assertEqual({world.effort_of(n) for n in world.issues}, {"low", "medium", "high"})

    def test_arms_run_one_at_a_time_in_the_planned_order(self):
        world = self.world()
        self.go(world, "--repeats", "1", "--budget-usd", "1000")
        planned = [a["key"] for a in self.state_doc()["arms"]]
        made = [world.issues[n] for n in sorted(world.issues)]
        self.assertEqual([(i["title"], next(l for l in i["labels"] if l.startswith("factory-effort:"))[len("factory-effort:"):]) for i in made],
                         [(next(t for t in ab.load_tasks(self.tasks) if t["id"] == k.split(".")[0])["title"], k.split(".")[1]) for k in planned])

    def test_it_never_merges_and_never_makes_a_call_the_client_does_not_offer(self):
        world = self.world()
        self.assertEqual(self.go(world, "--repeats", "1", "--budget-usd", "1000"), 0)  # FakeWorld raises on any other call, a merge included
        self.assertFalse(any("merge" in str(call).lower() for call in world.calls))
        self.assertTrue(all(pr["state"] == "CLOSED" for pr in world.prs.values()))
        for name in dir(ab.Client):
            self.assertNotIn("merge", name.lower())
            self.assertNotIn("push", name.lower())

    def test_the_real_client_runs_only_the_allowed_gh_verbs_and_refuses_a_merge(self):
        runs = []

        def runner(argv, **kwargs):
            runs.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout='{"state": "OPEN", "labels": []}' if "view" in argv else "https://github.com/org/sandbox/issues/5\n", stderr="")

        client = ab.Client(REPO, runner=runner)
        client.ensure_label("l", "fff", "d")
        self.assertEqual(client.create_issue("t", "b", ["x"]), 5)
        client.add_label(5, "factory-build")
        client.issue(5)
        client.close_pr(7)
        client.close_issue(5)
        client.delete_branch("factory/x")
        for argv in runs:
            self.assertEqual(argv[0], "gh")
            self.assertIn((argv[1], argv[2]), ab.Client.ALLOWED)
            self.assertNotIn("merge", argv)
            self.assertNotIn("--force-with-lease", argv)
        before = len(runs)
        for bad in (("pr", "merge", "7"), ("pr", "ready", "7"), ("repo", "delete", REPO), ("api", "-X", "PUT", f"repos/{REPO}/git/refs/heads/x"),
                    ("api", "-X", "DELETE", "repos/other/repo/git/refs/heads/x"), ("api", "-X", "DELETE", f"repos/{REPO}/pulls/1/merge")):
            with self.assertRaises(ab.ClientError):
                client._gh("x", *bad)
        self.assertEqual(len(runs), before)
        with self.assertRaises(ab.ClientError):
            client.delete_branch("../x")

    def test_each_session_ends_with_the_pull_request_closed_unmerged_and_the_issue_closed_and_the_branch_left(self):
        world = self.world()
        self.go(world, "--repeats", "1", "--budget-usd", "1000")
        self.assertEqual(sorted(c[1] for c in world.calls if c[0] == "close_pr"), [101, 102, 103, 104, 105, 106])
        self.assertTrue(all(i["state"] == "CLOSED" for i in world.issues.values()))
        self.assertEqual(world.deleted, [])
        self.assertTrue(all(a["status"] == "done" for a in self.state_doc()["arms"]))

    def test_the_branch_is_deleted_only_on_request_and_never_the_base_or_a_fork_branch(self):
        world = self.world()
        self.go(world, "--repeats", "1", "--budget-usd", "1000", "--delete-branches")
        self.assertEqual(world.deleted, ["factory/job"] * 6)
        for kwargs in ({"branch": "main"},):
            other = tempfile.TemporaryDirectory()
            self.addCleanup(other.cleanup)
            self.state = str(Path(other.name) / "state.json")
            world = FakeWorld(other.name, **kwargs)
            self.data = Path(other.name)
            self.go(world, "--repeats", "1", "--budget-usd", "1000", "--delete-branches")
            self.assertEqual(world.deleted, [])

    def test_a_merged_pull_request_stops_the_experiment_for_good(self):
        world = self.world(merged=True)
        self.assertEqual(self.go(world, "--repeats", "1", "--budget-usd", "1000"), 4)
        self.assertEqual(len(world.issues), 1)
        self.assertIn("merged or set to auto-merge", self.output())
        self.assertEqual(self.go(world, "--repeats", "1", "--budget-usd", "1000"), 4)  # sticky: it does not go on
        self.assertEqual(len(world.issues), 1)
        self.assertEqual([c for c in world.calls if c[0] == "close_pr"], [])

    def test_an_auto_merge_request_closes_the_pull_request_and_stops_once(self):
        world = self.world(auto_merge=True)
        self.assertEqual(self.go(world, "--repeats", "1", "--budget-usd", "1000"), 4)
        self.assertEqual(len([c for c in world.calls if c[0] == "close_pr"]), 1)
        self.assertEqual(len(world.issues), 1)
        world.auto_merge = False
        self.assertEqual(self.go(world, "--repeats", "1", "--budget-usd", "1000"), 0)

    def test_a_pull_request_outside_the_sandbox_is_never_touched(self):
        world = self.world()
        original = world.on_trigger

        def elsewhere(number):
            original(number)
            run = next(self.data.rglob(f"auto-{number:04d}")) / "state.json"
            document = json.loads(run.read_text())
            document["jobs"][f"job-{number}"]["pr"] = "https://github.com/someone/else/pull/9"
            run.write_text(json.dumps(document))

        world.on_trigger = elsewhere
        self.assertEqual(self.go(world, "--repeats", "1", "--budget-usd", "1000"), 4)
        self.assertEqual([c for c in world.calls if c[0] in ("close_pr", "pr")], [])

    def test_a_session_that_never_ends_times_out_closes_up_and_stops_the_experiment(self):
        world = self.world(outcome=lambda issue, effort: "running")
        self.assertEqual(self.go(world, "--repeats", "1", "--budget-usd", "1000", "--arm-timeout-minutes", "5"), 4)
        self.assertEqual(self.state_doc()["arms"][0]["outcome"], "timeout")
        self.assertEqual(len(world.issues), 1)
        self.assertEqual(world.issues[1]["state"], "CLOSED")
        self.assertIn("a session may still be running", self.output())

    def test_the_overall_time_limit_pauses_an_arm_that_is_still_waiting_and_keeps_the_state(self):
        world = self.world(outcome=lambda issue, effort: "running")
        self.assertEqual(self.go(world, "--repeats", "1", "--budget-usd", "1000", "--max-hours", "1", "--poll-seconds", "3600"), 5)
        self.assertEqual(self.state_doc()["arms"][0]["status"], "waiting")
        self.assertEqual(len(world.issues), 1)
        self.assertEqual([c for c in world.calls if c[0] in ("close_pr", "close_issue")], [])

    def test_the_time_limit_is_checked_between_arms_too(self):
        world = self.world()
        world.refresh = lambda: self.sleep(3600 * 2)  # each poll costs two hours of the clock
        self.assertEqual(self.go(world, "--repeats", "1", "--budget-usd", "1000", "--max-hours", "1"), 5)
        self.assertEqual(len(world.issues), 1)
        self.assertEqual(self.state_doc()["arms"][0]["status"], "done")

    def test_a_pull_request_with_checks_still_pending_is_waited_for_a_bounded_time_and_recorded_as_pending(self):
        world = self.world()
        original = world.on_trigger

        def pending(number):
            original(number)
            world.prs[100 + number]["statusCheckRollup"].append({"__typename": "CheckRun", "name": "slow", "status": "IN_PROGRESS"})

        world.on_trigger = pending
        self.assertEqual(self.go(world, "--repeats", "1", "--budget-usd", "1000", "--ci-wait-minutes", "3", "--poll-seconds", "60"), 0)
        self.assertEqual(self.state_doc()["arms"][0]["record"]["pr"]["ci"], "pending")
        self.assertGreaterEqual(len([c for c in world.calls if c[0] == "pr"]), 4)

    def test_a_job_that_failed_is_recorded_without_a_pull_request(self):
        world = self.world(outcome=lambda issue, effort: "failed")
        self.assertEqual(self.go(world, "--repeats", "1", "--budget-usd", "1000"), 0)
        arm = self.state_doc()["arms"][0]
        self.assertEqual((arm["outcome"], arm["pr"]), ("failed", None))
        self.assertNotIn("pr", arm["record"])
        self.assertEqual([c for c in world.calls if c[0] == "close_pr"], [])

    def test_an_issue_blocked_before_a_job_exists_is_an_outcome_too(self):
        world = self.world()
        world.on_trigger = lambda number: world.issues[number]["labels"].append("factory:blocked")
        self.assertEqual(self.go(world, "--repeats", "1", "--budget-usd", "1000"), 0)
        self.assertEqual(self.state_doc()["arms"][0]["outcome"], "blocked_before_job")

    def test_a_failing_call_three_times_in_a_row_stops_cleanly_with_the_state_kept(self):
        world = self.world(outcome=lambda issue, effort: "running")
        calls = {"n": 0}

        def broken():
            calls["n"] += 1
            raise ab.ClientError("refresh")

        world.refresh = broken
        self.assertEqual(self.go(world, "--repeats", "1", "--budget-usd", "1000"), 4)
        self.assertEqual(calls["n"], 3)
        self.assertIn("op=refresh", self.output())
        self.assertEqual(self.state_doc()["arms"][0]["status"], "waiting")


class BudgetTests(Base):
    def test_it_refuses_to_start_an_arm_that_would_pass_the_budget_and_stops_cleanly(self):
        world = self.world()
        # the first arm has no measure yet: the default estimate (3.00) fits a budget of 5; after it (cost 1.5 to 4.5) the next arm does not fit
        code = self.go(world, "--repeats", "1", "--budget-usd", "5", "--arm-estimate-usd", "3")
        self.assertEqual(code, 3)
        self.assertLess(len(world.issues), 6)
        self.assertGreaterEqual(len(world.issues), 1)
        self.assertIn("budget stop", self.output())
        spent = sum(a["cost_usd"] for a in self.state_doc()["arms"] if a["cost_usd"])
        for arm in self.state_doc()["arms"]:
            self.assertIn(arm["status"], ("done", "pending"))
        self.assertGreater(spent, 0)

    def test_a_budget_below_the_first_estimate_starts_nothing(self):
        world = self.world()
        self.assertEqual(self.go(world, "--repeats", "1", "--budget-usd", "2", "--arm-estimate-usd", "3"), 3)
        self.assertEqual(world.issues, {})
        self.assertEqual([c for c in world.calls if c[0] in ("create_issue", "add_label")], [])

    def test_the_cost_so_far_counts_the_planner_and_every_session(self):
        world = self.world()
        self.go(world, "--repeats", "1", "--budget-usd", "1000")
        costs = {a["effort"]: a["cost_usd"] for a in self.state_doc()["arms"]}
        self.assertEqual(costs, {"low": 1.5, "medium": 2.5, "high": 4.5})

    def test_a_later_run_with_a_larger_budget_continues_without_a_second_issue_for_any_arm(self):
        world = self.world()
        self.assertEqual(self.go(world, "--repeats", "1", "--budget-usd", "5", "--arm-estimate-usd", "3"), 3)
        made = len(world.issues)
        self.assertEqual(self.go(world, "--repeats", "1", "--budget-usd", "1000"), 0)
        self.assertEqual(len(world.issues), 6)
        titles = [(i["title"], world.effort_of(n)) for n, i in world.issues.items()]
        self.assertEqual(len(set(titles)), 6)
        self.assertGreater(6, made)
        self.assertEqual(self.go(world, "--repeats", "1", "--budget-usd", "1000"), 0)  # a finished experiment runs nothing more
        self.assertEqual(len(world.issues), 6)

    def test_a_cost_file_replaces_the_readers(self):
        world = self.world()
        cost = self.root / "cost.json"
        cost.write_text(json.dumps({"cost_usd": 9.5}))
        self.assertEqual(self.go(world, "--repeats", "1", "--budget-usd", "10", "--arm-estimate-usd", "1", "--cost-file", str(cost)), 3)
        self.assertEqual(world.issues, {})
        cost.write_text(json.dumps({"cost_usd": 0.5}))
        self.assertEqual(self.go(world, "--repeats", "1", "--budget-usd", "10", "--arm-estimate-usd", "1", "--cost-file", str(cost)), 0)
        cost.write_text("{}")
        with self.assertRaises(ab.Refused):
            ab.read_cost_file(str(cost))
        cost.write_text(json.dumps({"cost_usd": -1}))
        with self.assertRaises(ab.Refused):
            ab.read_cost_file(str(cost))

    def test_an_arm_whose_cost_cannot_be_read_counts_as_the_estimate(self):
        state = {"arms": [{"status": "done", "issue": 1, "effort": "low", "cost_usd": None},
                          {"status": "done", "issue": 2, "effort": "low", "cost_usd": 2.0},
                          {"status": "pending", "issue": None, "effort": "low", "cost_usd": None}]}
        self.assertEqual(ab.spent_so_far(state, None, 3.0), 4.0)  # 2.0 measured, the unknown one at the dearest measured arm of its effort; a pending arm costs nothing yet

    def test_the_estimate_is_the_dearest_finished_arm_of_the_effort_else_of_any_else_the_default(self):
        state = {"arms": [{"effort": "low", "cost_usd": 1.0}, {"effort": "high", "cost_usd": 4.0}, {"effort": "high", "cost_usd": 5.0}]}
        self.assertEqual(ab.estimate_for(state, "high", 3.0), 5.0)
        self.assertEqual(ab.estimate_for(state, "medium", 3.0), 5.0)
        self.assertEqual(ab.estimate_for({"arms": []}, "medium", 3.0), 3.0)


class ResumeTests(Base):
    def test_an_issue_created_before_a_failed_trigger_is_reused_and_the_trigger_is_added_once(self):
        world = self.world()
        world.fail_add_label = True
        self.assertEqual(self.go(world, "--repeats", "1", "--budget-usd", "1000"), 4)
        self.assertEqual(len(world.issues), 1)
        world.fail_add_label = False
        self.assertEqual(self.go(world, "--repeats", "1", "--budget-usd", "1000"), 0)
        self.assertEqual(len(world.issues), 6)
        self.assertEqual([i["labels"].count("factory-build") for i in world.issues.values()], [1] * 6)  # none was triggered twice

    def test_an_issue_created_but_not_recorded_is_found_again_by_its_experiment_label(self):
        world = self.world()
        world.fail_after_create = True
        self.assertEqual(self.go(world, "--repeats", "1", "--budget-usd", "1000"), 4)
        self.assertEqual(self.state_doc()["arms"][0]["issue"], None)
        self.assertEqual(self.go(world, "--repeats", "1", "--budget-usd", "1000"), 0)
        self.assertEqual(len(world.issues), 6)

    def test_two_unrecorded_issues_for_one_arm_are_refused_not_guessed(self):
        world = self.world()
        world.fail_after_create = True
        self.go(world, "--repeats", "1", "--budget-usd", "1000")
        first = self.state_doc()["arms"][0]
        title = next(t for t in ab.load_tasks(self.tasks) if t["id"] == first["task"])["title"]
        world.issues[99] = {"title": title, "body": "", "labels": list(world.issues[1]["labels"]), "state": "OPEN"}
        self.assertEqual(self.go(world, "--repeats", "1", "--budget-usd", "1000"), 2)
        self.assertEqual(len(world.issues), 2)

    def test_the_repeats_may_grow_and_the_finished_arms_stay_where_they_are(self):
        world = self.world()
        self.go(world, "--repeats", "1", "--budget-usd", "1000")
        before = [a["key"] for a in self.state_doc()["arms"]]
        self.assertEqual(self.go(world, "--repeats", "2", "--budget-usd", "1000"), 0)
        after = [a["key"] for a in self.state_doc()["arms"]]
        self.assertEqual(after[:6], before)
        self.assertEqual(len(after), 12)
        self.assertEqual(len(world.issues), 12)

    def test_a_state_of_another_experiment_is_refused_and_left_alone(self):
        world = self.world()
        self.go(world, "--repeats", "1", "--budget-usd", "1000")
        before = Path(self.state).read_text()
        self.assertEqual(self.go(world, "--repeats", "1", "--budget-usd", "1000", "--model", "opus"), 2)
        self.assertEqual(self.go(world, "--repeats", "1", "--budget-usd", "1000", "--seed", "8"), 2)
        self.assertEqual(self.go(world, "--repeats", "1", "--budget-usd", "1000", "--efforts", "low,high"), 2)
        self.assertEqual(Path(self.state).read_text(), before)

    def test_a_file_that_is_not_a_state_is_never_overwritten(self):
        Path(self.state).write_text("not json")
        self.assertEqual(self.go(self.world(), "--repeats", "1"), 2)
        self.assertEqual(Path(self.state).read_text(), "not json")

    def test_the_state_file_is_private_and_the_records_file_is_written_after_every_arm(self):
        self.go(self.world(), "--repeats", "1", "--budget-usd", "1000")
        self.assertEqual(stat.S_IMODE(os.stat(self.state).st_mode), 0o600)
        records = json.loads(Path(self.state + ".records.json").read_text())
        self.assertEqual(len(records["arms"]), 6)
        self.assertNotIn("title", json.dumps(records))


class RecordTests(Base):
    def record(self, outcome=None):
        world = self.world(outcome=outcome)
        self.go(world, "--repeats", "1", "--budget-usd", "1000")
        return self.state_doc()["arms"][0]["record"], self.state_doc()["arms"][0]

    def test_a_session_carries_the_figures_the_comparison_needs(self):
        record, arm = self.record()
        session = record["sessions"][0]
        self.assertEqual((session["phase"], session["model"]), ("build", "claude-sonnet-5-5"))
        self.assertEqual((session["output_tokens"], session["thinking_tokens"], session["cache_creation_input_tokens"],
                          session["cache_read_input_tokens"], session["input_tokens"]), (500, 200, 100, 1000, 10))
        self.assertEqual((session["num_turns"], session["duration_ms"], session["total_cost_usd"]), (7, 12000, COSTS[arm["effort"]]))
        self.assertEqual(session["model_usage"]["claude-sonnet-5-5"]["costUSD"], COSTS[arm["effort"]])
        self.assertEqual(session["subtype"], "unknown")  # the canary is not a token
        self.assertIn("other", session["model_usage"])
        self.assertEqual(record["totals"]["output_tokens"], 500)
        self.assertEqual(record["planner_cost_usd"], 0.5)

    def test_the_outcome_that_matters_is_recorded(self):
        record, _arm = self.record()
        self.assertEqual(record["job_status"], "pr_ready")
        self.assertEqual(record["verification"], {"complete": True, "matches_head": True})
        self.assertEqual(record["review"], {"passed": False, "findings": 2})
        self.assertIs(record["incomplete"], False)
        self.assertEqual(record["pr"]["gate"], "success")
        self.assertEqual(record["pr"]["ci"], "passing")
        self.assertEqual((record["pr"]["files_changed"], record["pr"]["expected_paths_touched"], record["pr"]["expected_paths_total"],
                          record["pr"]["unexpected_paths"]), (2, 1, 2, 1))
        self.assertIs(record["effort_confirmed"], True)

    def test_an_incomplete_pull_request_is_marked_so(self):
        record, _arm = self.record(lambda issue, effort: "pr_incomplete")
        self.assertIs(record["incomplete"], True)

    def test_an_effort_the_plan_did_not_record_as_the_label_is_not_confirmed(self):
        world = self.world()
        original = world.on_trigger

        def wrong(number):
            original(number)
            path = next(self.data.rglob(f"auto-{number:04d}")) / "plan.json"
            plan = json.loads(path.read_text())
            plan["jobs"][0]["choice"]["build"]["effort"] = "low" if world.effort_of(number) != "low" else "high"
            path.write_text(json.dumps(plan))

        world.on_trigger = wrong
        self.go(world, "--repeats", "1", "--budget-usd", "1000")
        self.assertIs(self.state_doc()["arms"][0]["record"]["effort_confirmed"], False)
        self.assertEqual(ab.summary_rows(self.state_doc())[0]["effort_confirmed"], 0)

    def test_the_table_is_effort_reports_and_says_the_outcome_is_closed_by_design(self):
        self.go(self.world(), "--repeats", "1", "--budget-usd", "1000")
        text = self.output()
        self.assertIn("Jobs by effort", text)
        self.assertIn("by design", text)
        self.assertIn("effort_confirmed=2", text)


class PilotTests(Base):
    def test_the_pilot_runs_one_repeat_and_prints_the_spread_and_a_number_of_repeats(self):
        world = self.world()
        self.assertEqual(self.go(world, "--budget-usd", "1000", command="pilot"), 0)
        self.assertEqual(len(world.issues), 6)
        text = self.output()
        self.assertIn("pilot spread", text)
        self.assertIn("low -> medium", text)
        self.assertIn("repeats per arm =", text)
        self.assertIn("effort_ab: results", text)

    def test_repeats_needed_follow_the_paired_formula(self):
        arms = []
        for task, (low, high) in enumerate([(1.0, 1.2), (1.0, 1.5), (1.0, 1.1), (1.0, 1.4)], 1):
            for effort, cost in (("low", low), ("high", high)):
                arms.append({"task": f"t{task}", "effort": effort, "record": {"cost_usd": cost}})
        state = {"arms": arms, "efforts": ["low", "high"], "tasks": ["t1", "t2", "t3", "t4"]}
        repeats, pairs = ab.suggested_repeats(state, 0.1)
        diffs = [0.2, 0.5, 0.1, 0.4]
        import statistics
        sd = statistics.stdev(diffs)
        self.assertEqual(pairs[0][2], 4)
        self.assertAlmostEqual(pairs[0][4], sd)
        import math
        self.assertEqual(repeats, math.ceil(math.ceil(7.85 * (sd / 0.1) ** 2) / 4))
        self.assertEqual(ab.suggested_repeats({**state, "arms": arms[:2]}, 0.1)[1], [])

    def test_the_report_command_reads_a_state_file_and_calls_no_client(self):
        self.go(self.world(), "--budget-usd", "1000", command="pilot")
        self.lines.clear()
        with mock.patch.object(ab, "Client", side_effect=AssertionError("no client")):
            self.assertEqual(ab.main(["report", "--state", self.state, "--data", str(self.data)], say=self.lines.append), 0)
        self.assertIn("effort_ab: results", self.output())


class SafetyTests(Base):
    def test_nothing_of_a_task_a_path_a_token_or_a_gh_message_is_printed(self):
        world = self.world()
        self.go(world, "--repeats", "1", "--budget-usd", "5", "--arm-estimate-usd", "3")
        self.go(world, "--repeats", "1", "--budget-usd", "1000")
        for secret in (CANARY, TOKEN, "other.py", "README.md", "src/"):
            self.assertNotIn(secret, self.output(), secret)
        self.assertNotIn(CANARY, Path(self.state).read_text())
        self.assertNotIn(TOKEN, Path(self.state + ".records.json").read_text())

    def test_a_gh_failure_shows_a_fixed_verb_and_no_message_or_token(self):
        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, stdout=TOKEN, stderr=f"{TOKEN} {CANARY}")

        self.lines.clear()
        code = ab.main(self.argv("run"), client=ab.Client(REPO, runner=runner), say=self.lines.append, now=self.now, sleep=self.sleep)
        self.assertEqual(code, 4)
        self.assertIn("op=label", self.output())
        for secret in (TOKEN, CANARY):
            self.assertNotIn(secret, self.output())
        with self.assertRaises(ab.ClientError) as caught:
            ab.Client(REPO, runner=runner).issue(1)
        self.assertEqual(str(caught.exception), "issue")

    def test_a_timeout_or_a_missing_gh_is_a_fixed_error(self):
        for error in (subprocess.TimeoutExpired("gh", 1), FileNotFoundError(TOKEN)):
            def runner(argv, **kwargs):
                raise error

            with self.assertRaises(ab.ClientError) as caught:
                ab.Client(REPO, runner=runner).issue(1)
            self.assertEqual(str(caught.exception), "issue")

    def test_every_gh_call_is_bounded(self):
        seen = []

        def runner(argv, **kwargs):
            seen.append(kwargs)
            return subprocess.CompletedProcess(argv, 0, stdout='{"state":"OPEN","labels":[]}', stderr="")

        ab.Client(REPO, runner=runner).issue(1)
        self.assertEqual(seen[0]["timeout"], ab.CALL_TIMEOUT)
        self.assertNotIn("shell", seen[0])

    def test_a_run_needs_the_sandbox_confirmation_a_repository_and_a_cost_source(self):
        base = ["run", "--tasks", self.tasks, "--state", self.state, "--repo", REPO, "--data", str(self.data)]
        self.assertEqual(ab.main(base, client=self.world(), say=self.lines.append), 2)
        self.assertIn("--confirm-sandbox", self.output())
        for argv in ([a for a in base if a not in ("--repo", REPO)] + ["--confirm-sandbox"],
                     [a for a in base if a not in ("--data", str(self.data))] + ["--confirm-sandbox"],
                     base + ["--confirm-sandbox", "--trigger-label", "factory-ignore"],
                     ["run", "--tasks", self.tasks, "--state", self.state, "--repo", "notarepo", "--data", str(self.data), "--confirm-sandbox"]):
            self.lines.clear()
            self.assertEqual(ab.main(argv, client=self.world(), say=self.lines.append), 2, argv)
        self.assertFalse(os.path.exists(self.state))

    def test_the_whole_tool_runs_no_shell_and_has_no_merge_verb(self):
        source = (ROOT / "tools" / "effort_ab.py").read_text()
        self.assertNotIn("shell=True", source)
        self.assertNotIn("os.system", source)
        self.assertNotIn('"merge"', source)
        self.assertEqual(sorted(v for _g, v in ab.Client.ALLOWED if v != "-X"), sorted(["create", "create", "edit", "view", "list", "close", "view", "close"]))

    def test_the_output_of_a_real_script_run_is_fixed_text(self):
        done = subprocess.run(["python3", "-I", str(ROOT / "tools" / "effort_ab.py"), "run", "--dry-run", "--tasks", self.tasks, "--seed", "1"],
                              capture_output=True, text=True, env={"PYTHONPATH": str(ROOT / "src"), "GH_TOKEN": TOKEN}, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn(CANARY, done.stdout + done.stderr)
        self.assertNotIn(TOKEN, done.stdout + done.stderr)
        self.assertTrue(all(line.startswith("effort_ab: ") for line in done.stdout.splitlines()))


if __name__ == "__main__":
    unittest.main()

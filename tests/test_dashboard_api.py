"""The dashboard's read endpoints and the operator entries of the ledger (docs/manager.md, "Dashboard endpoints").

A real ``ManagerServer`` on a fake runner and spawner, as in tests/test_manager.py. Every endpoint is
checked in both authentication modes, for the order of the refusals (401, 403, 404, 405), and for what
the issue requires of a read: no process is ever started (the fake runner and spawner stay empty and
``subprocess`` is not reachable), the ledger is the source of the past, and nothing a run wrote as free text
leaves it.
"""

import copy
import json
import os
import subprocess
import sys
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from factory import dashboard, history, manager as mgr, memwatch
from factory.history import Ledger
from factory.manager import Child, CommandResult
from factory.state import State, atomic_json
from tests import test_manager as tm
from tests.test_factory import assignment

CANARY = "CANARY: 9c2d agent-authored text."
JAN = 1_767_225_600.0  # 2026-01-01T00:00:00Z
DAY = 86400.0
HOUR = 3600.0
PR = "https://github.com/org/name/pull/7"
ENDPOINTS = ("/dashboard", "/history/runs", "/history/jobs", "/repos/org/name/runs/r1/jobs/fix-a")


def stamp_ago(seconds):
    """The stamp ``state.timestamp`` would write ``seconds`` ago (a negative number is in the future)."""
    return datetime.fromtimestamp(time.time() - seconds, timezone.utc).isoformat().replace("+00:00", "Z")


def two_job_plan():
    plan = assignment()
    other = copy.deepcopy(plan["jobs"][0])
    plan["jobs"][0]["likely_paths"] = ["src/a"]
    other.update(id="fix-b", issues=[2], likely_paths=["src/b"])
    plan["jobs"].append(other)
    plan["issues"].append({"number": 2, "title": "B", "body": "B"})
    plan["dispositions"].append({"issue": 2, "status": "job", "job": "fix-b", "reason": "Fix"})
    return plan


class DashboardCase(tm.ServerCase):
    def plan_run(self, run="r1", repo="org/name", plan=None, started=True, **fix_a):
        """A run directory with a valid plan and, when ``started``, a state in which fix-a has ``fix_a``."""
        directory = self.run_dir(run, repo)
        directory.mkdir(parents=True, exist_ok=True)
        plan = plan or assignment()
        atomic_json(directory / "plan.json", plan)
        if started:
            state = State(directory, plan)
            if fix_a:
                state.update("fix-a", **fix_a)
        return directory

    def write_history(self, *entries):
        """Ledger entries ``(day offset, repo, run, kind, fields)`` written on a clock that follows the offsets."""
        now = [JAN]
        ledger = Ledger(self.data_dir, clock=lambda: now[0])
        for step, (offset, repo, run, kind, fields) in enumerate(entries):
            now[0] = JAN + offset * DAY + step
            ledger.append(kind, repo, run, **fields)

    def get(self, path, principal=tm.VIEWER):
        return self.call("GET", path, principal=principal)

    def body(self, path, principal=tm.VIEWER):
        status, payload, _ = self.get(path, principal)
        self.assertEqual(status, 200, payload)
        return payload

    def history_scenario(self):
        """Two purged runs of org/name (one merged, one abandoned) and a blocked run of org/other that still exists."""
        merged_jobs = [{"id": "fix-a", "status": "merged", "pr": PR, "handoff": "merged"}]
        self.write_history(
            (0, "org/name", "r-merged", "planned", {"issues": [1], "jobs": [{"id": "fix-a", "issues": [1]}], "planner_cost_usd": 0.5}),
            (0, "org/name", "r-merged", "run_started", {}),
            (0, "org/name", "r-merged", "job", {"job": "fix-a", "status": "running", "phase": "build", "attempt": 1, "cost_usd": 1.0, "issues": [1]}),
            (0, "org/name", "r-merged", "job", {"job": "fix-a", "status": "merged", "phase": "publish", "attempt": 1, "cost_usd": 2.0, "pr": PR}),
            (0, "org/name", "r-merged", "run_finished", {"result": "stopped"}),
            (0, "org/name", "r-merged", "purged", {"reason": "terminal", "jobs": merged_jobs}),
            (31, "org/name", "r-aband", "job", {"job": "fix-b", "status": "running", "phase": "build", "attempt": 1, "issues": [2]}),
            (31, "org/name", "r-aband", "purged", {"reason": "abandoned", "jobs": [{"id": "fix-b", "status": "running"}]}),
            (45, "org/other", "r-blocked", "job", {"job": "fix-c", "status": "failed", "phase": "build", "attempt": 2, "issues": [3]}),
        )


class AuthTests(DashboardCase):
    """401 before 403 before 404 before 405, for every new endpoint, with the Easy Auth header."""

    def setUp(self):
        super().setUp()
        self.plan_run(status="running", worker_phase="build", phase="build", attempt=1)

    def test_an_anonymous_caller_gets_401_everywhere_whatever_the_path(self):
        for path in (*ENDPOINTS, "/history", "/history/other", "/repos/no/body/runs/r1/jobs/x", "/dashboard?junk=1"):
            for method in ("GET", "POST", "PUT", "DELETE"):
                with self.subTest(path=path, method=method):
                    status, payload, _ = self.call(method, path, principal=None)
                    self.assertEqual((status, payload), (401, {"error": "authentication required"}))

    def test_a_principal_without_a_factory_role_gets_403_before_anything_else(self):
        for path in (*ENDPOINTS, "/history/other", "/repos/no/body/runs/r1/jobs/x"):
            for method in ("GET", "POST"):
                with self.subTest(path=path, method=method):
                    status, payload, _ = self.call(method, path, principal=tm.NO_ROLE)
                    self.assertEqual((status, payload), (403, {"error": "forbidden"}))

    def test_a_viewer_and_an_operator_may_read(self):
        for principal in (tm.VIEWER, tm.OPERATOR):
            for path in ENDPOINTS:
                with self.subTest(path=path):
                    status, payload, headers = self.get(path, principal)
                    self.assertEqual(status, 200, payload)
                    self.assertEqual(headers["Content-Type"], "application/json")
                    self.assertEqual(headers["Cache-Control"], "no-store")
                    self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_only_get_is_allowed_for_anyone_who_may_read(self):
        for principal in (tm.VIEWER, tm.OPERATOR):
            for path in ENDPOINTS:
                for method in ("POST", "PUT", "PATCH", "DELETE"):
                    with self.subTest(path=path, method=method, operator=principal == tm.OPERATOR):
                        status, payload, headers = self.call(method, path, body={}, principal=principal)
                        self.assertEqual((status, payload, headers["Allow"]), (405, {"error": "method not allowed"}, "GET"))

    def test_a_path_that_names_nothing_is_404_once_authenticated(self):
        for path in (
            "/history",
            "/history/other",
            "/history/runs/extra",
            "/dashboard/extra",
            "/repos/org/name/runs/r1/jobs",
            "/repos/org/name/runs/r1/jobs/fix-a/extra",
            "/repos/org/name/runs/r1/jobs/nope",
            "/repos/org/nope/runs/r1/jobs/fix-a",
            "/repos/org/name/runs/r2/jobs/fix-a",
        ):
            with self.subTest(path):
                status, payload, _ = self.get(path)
                self.assertEqual((status, payload), (404, {"error": "not found"}))

    def test_a_bad_run_or_job_name_is_400(self):
        for path in ("/repos/org/name/runs/-x/jobs/fix-a", "/repos/org/name/runs/r1/jobs/a%20b", "/repos/org/name/runs/r1/jobs/_a"):
            with self.subTest(path):
                self.assertEqual(self.get(path)[0], 400)

    def test_no_new_endpoint_takes_part_in_the_action_table(self):
        self.assertTrue(set(mgr.READS).isdisjoint(mgr.ACTIONS))


class NoProcessTests(DashboardCase):
    def test_opening_every_endpoint_starts_no_cli_and_no_gh(self):
        self.plan_run(status="running", worker_phase="build", phase="build", attempt=1)
        self.history_scenario()

        def refuse(*args, **kwargs):
            raise AssertionError("a read started a process")

        with patch.object(subprocess, "Popen", refuse), patch.object(subprocess, "run", refuse), patch("os.system", refuse):
            for path in (*ENDPOINTS, "/history/runs?outcome=merged", "/history/jobs?run=r-merged"):
                for _ in range(2):
                    self.assertEqual(self.get(path)[0], 200)
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.spawner.calls, [])

    def test_the_audit_line_of_a_read_names_the_principal_and_has_no_argv(self):
        self.get("/dashboard")
        self.get("/history/runs?outcome=merged&limit=3")
        lines = self.audit_lines()
        self.assertEqual(
            lines,
            [
                f"audit principal={tm.OID} method=GET path=/dashboard status=200 argv=[]",
                f"audit principal={tm.OID} method=GET path=/history/runs status=200 argv=[]",  # the query string is not logged
            ],
        )


class OverviewTests(DashboardCase):
    def test_a_job_in_flight_shows_phase_age_cost_and_the_budget(self):
        (self.config_dir / "repos" / "org__name.toml").write_text('repo = "org/name"\nrun_budget_usd = 5\n')
        directory = self.plan_run(
            status="running", worker_phase="build", phase="build", attempt=1, usage=[{"total_cost_usd": 0.5}, {"total_cost_usd": 0.25}]
        )
        atomic_json(directory / "planner-usage.json", {"total_cost_usd": 0.25})
        payload = self.body("/dashboard")
        self.assertEqual([repo["repo"] for repo in payload["repos"]], ["org/name", "org/other"])
        run = payload["repos"][0]["runs"][0]
        self.assertEqual((run["run"], run["started"], run["budget_usd"]), ("r1", True, 5.0))
        self.assertEqual((run["cost_usd"], run["planner_cost_usd"]), (1.0, 0.25))
        job = run["jobs"][0]
        self.assertEqual((job["id"], job["status"], job["phase"], job["attempt"], job["issues"]), ("fix-a", "running", "build", 1, [1]))
        self.assertEqual(job["cost_usd"], 0.75)
        self.assertIsInstance(job["phase_age_seconds"], float)
        self.assertEqual(payload["repos"][0]["jobs_by_status"], {"running": 1})
        self.assertEqual(payload["pr_state"], "recorded")
        self.assertEqual(payload["repos"][1]["runs"], [])

    def test_a_published_job_shows_its_pull_request_and_the_reason_code(self):
        self.plan_run(status="pr_incomplete", pr=PR, attempt=1, reason_code="ci_pending", commit="a" * 40)
        job = self.body("/dashboard")["repos"][0]["runs"][0]["jobs"][0]
        self.assertEqual((job["pr"], job["reason_code"], job["handoff_status"]), (PR, "ci_pending", "published"))

    def test_the_pull_request_state_the_ledger_last_saw_is_shown_with_how_old_it_is(self):
        self.plan_run(status="pr_ready", pr=PR, attempt=1)
        self.write_history((0, "org/name", "r1", "pr_observed", {"job": "fix-a", "pr": PR, "handoff_status": "merged", "observed_at": "2026-01-01T00:00:00Z"}))
        observed = self.body("/dashboard")["repos"][0]["runs"][0]["jobs"][0]["pr_observed"]
        self.assertEqual((observed["handoff_status"], observed["observed_at"]), ("merged", "2026-01-01T00:00:00Z"))
        self.assertGreater(observed["age_seconds"], 0)
        self.assertIsNone(observed["fresh"])  # an entry that did not say

    def test_a_failed_lookup_neither_replaces_what_github_confirmed_nor_hides_that_it_failed(self):
        self.plan_run(status="pr_ready", pr=PR, attempt=1)
        merged = {"job": "fix-a", "pr": PR, "handoff_status": "merged", "observed_at": "2026-01-01T00:00:00Z", "fresh": True}
        guess = {"job": "fix-a", "pr": PR, "handoff_status": "published", "observed_at": "2026-01-01T00:20:00Z", "fresh": False}
        self.write_history((0, "org/name", "r1", "pr_observed", merged), (0, "org/name", "r1", "pr_observed", guess))
        observed = self.body("/dashboard")["repos"][0]["runs"][0]["jobs"][0]["pr_observed"]
        self.assertEqual(
            (observed["handoff_status"], observed["observed_at"], observed["fresh"]), ("merged", "2026-01-01T00:00:00Z", True)
        )
        detail = self.body("/repos/org/name/runs/r1/jobs/fix-a")
        self.assertEqual(
            detail["pr_observed"], {"handoff_status": "merged", "observed_at": "2026-01-01T00:00:00Z", "fresh": True}
        )
        self.assertEqual((detail["history"]["outcome"], detail["history"]["observed_fresh"]), ("merged", True))

    def test_a_word_only_state_json_gave_is_marked_as_not_confirmed(self):
        self.plan_run(status="pr_ready", pr=PR, attempt=1)
        guess = {"job": "fix-a", "pr": PR, "handoff_status": "published", "observed_at": "2026-01-01T00:20:00Z", "fresh": False}
        self.write_history((0, "org/name", "r1", "pr_observed", guess))
        observed = self.body("/dashboard")["repos"][0]["runs"][0]["jobs"][0]["pr_observed"]
        self.assertEqual((observed["handoff_status"], observed["fresh"]), ("published", False))
        self.assertEqual(self.body("/repos/org/name/runs/r1/jobs/fix-a")["pr_observed"]["fresh"], False)
        listed = self.body("/history/jobs")["jobs"][0]
        self.assertEqual((listed["handoff_status"], listed["observed_fresh"]), ("published", False))

    def test_the_supervisor_is_running_draining_stale_or_idle(self):
        directory = self.plan_run(status="running", worker_phase="build", phase="build", attempt=1)
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()

        def state_with(status, pid=None, **extra):
            atomic_json(
                directory / "supervisor-control.json",
                {
                    "version": 1,
                    "history": [],
                    "current": {"status": status, "pid": os.getpid() if pid is None else pid, "started_at": stamp_ago(0), **extra},
                },
            )
            self.manager.dashboard._overview = None
            return self.body("/dashboard")["repos"][0]["runs"][0]["supervisor"]

        self.assertEqual(self.body("/dashboard")["repos"][0]["runs"][0]["supervisor"]["state"], "idle")  # no record at all
        self.assertEqual(state_with("running")["state"], "running")
        self.assertEqual(state_with("draining", drain_requested_at="2026-01-01T00:01:00Z")["drain_requested_at"], "2026-01-01T00:01:00Z")
        self.assertEqual(state_with("draining")["state"], "draining")
        self.assertEqual(state_with("running", pid=dead.pid)["state"], "stale")
        # a live process that started after the record says the supervisor did got a reused id: the supervisor is gone
        self.assertEqual(state_with("running", started_at="2000-01-01T00:00:00Z")["state"], "stale")
        self.assertEqual(state_with("running", started_at=None)["state"], "stale")
        finished = state_with("drained", finished_at="2026-01-01T00:02:00Z")
        self.assertEqual((finished["state"], finished["result"], finished["finished_at"]), ("idle", "drained", "2026-01-01T00:02:00Z"))

    def test_a_supervisor_the_manager_started_is_running_or_planning(self):
        self.plan_run(status="running", worker_phase="build", phase="build", attempt=1)
        self.manager.children.register(Child("run", "org/name", "r1", tm.FakeProcess(7001), "2026-01-01T00:00:00Z"))
        repo = self.body("/dashboard")["repos"][0]
        self.assertEqual((repo["runs"][0]["supervisor"]["state"], repo["runs"][0]["supervisor"]["managed"]), ("running", True))
        self.assertEqual((repo["supervisor"]["kind"], repo["supervisor"]["run"]), ("run", "r1"))
        self.assertNotIn("pid", repo["supervisor"])

    def test_a_run_that_cannot_be_read_is_a_row_with_an_error_not_a_500(self):
        broken_plan = self.plan_run("broken-plan", started=False)
        (broken_plan / "plan.json").write_text("{not json")
        broken_state = self.plan_run("broken-state", status="running", attempt=1)
        (broken_state / "state.json").write_text("[1, 2")
        linked = self.plan_run("linked", status="running", attempt=1)
        (linked / "state.json").rename(self.root_file("real-state.json"))
        os.symlink(self.root_file("real-state.json"), linked / "state.json")
        garbled = self.plan_run("garbled", status="running", attempt=1)
        (garbled / "state.json").write_text(json.dumps({"version": 1, "jobs": {"fix-a": {"status": ["running"], "usage": "x"}}}))
        empty = self.run_dir("empty")
        empty.mkdir(parents=True)
        runs = {run["run"]: run for run in self.body("/dashboard")["repos"][0]["runs"]}
        self.assertEqual(runs["garbled"]["error"], "state unreadable")  # valid JSON the report cannot use
        self.assertEqual(runs["broken-plan"]["error"], "plan unreadable")
        self.assertEqual(runs["broken-state"]["error"], "state unreadable")
        self.assertEqual(runs["linked"]["error"], "state unreadable")  # never read through a symlink
        self.assertEqual(runs["empty"]["error"], "not planned")
        self.assertTrue(all(run["jobs"] == [] for run in runs.values()))

    def root_file(self, name):
        return self.data_dir.parent / name

    def test_a_planned_run_that_never_started_lists_its_jobs_as_not_started(self):
        self.plan_run(started=False)
        run = self.body("/dashboard")["repos"][0]["runs"][0]
        self.assertEqual((run["planned"], run["started"]), (True, False))
        self.assertEqual([(job["id"], job["status"]) for job in run["jobs"]], [("fix-a", "not_started")])

    def test_what_a_run_wrote_as_free_text_is_never_in_the_overview(self):
        self.plan_run(
            status="failed",
            error=CANARY,
            retry_reason=CANARY,
            retry_history=[{"reason": CANARY, "previous_error": CANARY}],
            review={"pass": False, "findings": [CANARY]},
            verification={"complete": False, "blocker": CANARY},
        )
        text = json.dumps(self.body("/dashboard"))
        self.assertNotIn("CANARY", text)

    def test_the_number_of_jobs_listed_is_capped_and_says_so(self):
        self.plan_run(plan=two_job_plan(), status="running", worker_phase="build", phase="build", attempt=1)
        with patch.object(dashboard, "MAX_ACTIVE_ROWS", 1):
            payload = self.body("/dashboard")
        self.assertEqual(len(payload["repos"][0]["runs"][0]["jobs"]), 1)
        self.assertEqual(payload["repos"][0]["runs"][0]["jobs_total"], 2)
        self.assertTrue(payload["truncated"])
        self.manager.dashboard._overview = None
        self.assertFalse(self.body("/dashboard")["truncated"])  # under every cap: nothing was left out

    def test_the_number_of_runs_listed_is_capped_and_says_so(self):
        for run in ("r1", "r2", "r3"):
            self.plan_run(run, started=False)
        with patch.object(dashboard, "MAX_ACTIVE_RUNS", 2):
            payload = self.body("/dashboard")
        repo = payload["repos"][0]
        self.assertEqual((len(repo["runs"]), repo["runs_total"], payload["truncated"]), (2, 3, True))

    def test_the_overview_is_reused_for_a_few_seconds_and_rebuilt_after(self):
        directory = self.plan_run(status="running", worker_phase="build", phase="build", attempt=1)
        now = [100.0]
        self.manager.dashboard._clock = lambda: now[0]
        first = self.body("/dashboard")
        State(directory, assignment()).update("fix-a", status="failed", worker_phase=None)
        now[0] += dashboard.OVERVIEW_TTL - 1
        self.assertEqual(self.body("/dashboard"), first)
        now[0] += 2
        self.assertEqual(self.body("/dashboard")["repos"][0]["runs"][0]["jobs"][0]["status"], "failed")

    def test_the_overview_takes_no_parameters(self):
        for query in ("?x=1", "?", "?limit=5", "?repo=org/name"):
            with self.subTest(query):
                status, payload, _ = self.get(f"/dashboard{query}")
                self.assertEqual(status, 400 if query != "?" else 200, payload)

    def test_the_trigger_pause_and_the_ledger_size_are_shown(self):
        self.manager.set_trigger_pause(True)
        payload = self.body("/dashboard")
        self.assertTrue(payload["trigger_paused"])
        self.assertEqual(payload["history"]["max_bytes"], history.DEFAULT_MAX_BYTES)


class ActivityWindowTests(DashboardCase):
    """Only active runs are on the overview: a live supervisor, or activity in the last 12 hours (docs/manager.md)."""

    def quiet_run(self, run="r1", hours=13, control=None, **fix_a):
        """A run whose stamps, files and directory are all ``hours`` old; ``control`` is its supervisor's current record."""
        with patch("factory.state.timestamp", return_value=stamp_ago(hours * HOUR)):
            directory = self.plan_run(run, **fix_a)
        if control is not None:
            self.write_control(directory, **control)
        self.age(directory, hours)
        return directory

    @staticmethod
    def write_control(directory, **current):
        atomic_json(directory / "supervisor-control.json", {"version": 1, "history": [], "current": current})

    @staticmethod
    def age(directory, hours):
        """Set the modification time of everything in a run directory, then of the directory, ``hours`` back.

        A symlink gets the time itself (``follow_symlinks=False``), not the file it points to.
        """
        moment = time.time() - hours * HOUR
        for path in (*directory.iterdir(), directory):
            os.utime(path, (moment, moment), follow_symlinks=False)

    def board(self):
        """A freshly built overview (the cached one is dropped)."""
        self.manager.dashboard._overview = None
        return self.body("/dashboard")

    @staticmethod
    def listed(payload, index=0):
        return [run["run"] for run in payload["repos"][index]["runs"]]

    def test_a_run_quiet_for_longer_than_the_window_is_left_out_counted_and_in_the_history(self):
        self.quiet_run("r1", status="pr_ready", pr=PR, attempt=1)
        self.plan_run("r2", status="running", worker_phase="build", phase="build", attempt=1)
        # A status read of the guardrail or the janitor writes pr_observed entries for old runs: that is not activity.
        Ledger(self.data_dir).append(
            "pr_observed", "org/name", "r1", job="fix-a", pr=PR, handoff_status="published", observed_at=stamp_ago(0)
        )
        payload = self.board()
        repo = payload["repos"][0]
        self.assertEqual(self.listed(payload), ["r2"])
        self.assertEqual((repo["runs_total"], repo["runs_inactive"]), (2, 1))
        self.assertEqual(repo["jobs_by_status"], {"running": 1})  # the quiet run's jobs are not counted
        self.assertFalse(payload["truncated"])  # left out because it is quiet, which is not a cut
        self.assertEqual((payload["repos"][1]["runs_total"], payload["repos"][1]["runs_inactive"]), (0, 0))
        self.assertIn("r1", [run["run"] for run in self.body("/history/runs")["runs"]])

    def test_a_quiet_run_with_a_live_supervisor_stays_on_the_board(self):
        old = stamp_ago(13 * HOUR)
        controls = {
            "r-managed": None,
            "r-planning": None,
            "r-running": {"status": "running", "pid": os.getpid(), "started_at": old},
            "r-draining": {"status": "draining", "pid": os.getpid(), "started_at": old, "drain_requested_at": old},
        }
        for run, control in controls.items():
            self.quiet_run(run, control=control, status="running", worker_phase="build", phase="build", attempt=1)
        self.manager.children.register(Child("run", "org/name", "r-managed", tm.FakeProcess(7001), old))
        self.manager.children.register(Child("plan", "org/name", "r-planning", tm.FakeProcess(7002), old))
        # the supervisors of r-running and r-draining (this process, as far as the probe is told) started 14 hours ago,
        # before their records say: they are the processes that wrote them
        with patch.object(dashboard, "_process_start", return_value=time.time() - 14 * HOUR):
            payload = self.board()
        repo = payload["repos"][0]
        runs = {run["run"]: run for run in repo["runs"]}
        self.assertEqual(
            {run: view["supervisor"]["state"] for run, view in runs.items()},
            {"r-managed": "running", "r-planning": "planning", "r-running": "running", "r-draining": "draining"},
        )
        self.assertEqual(repo["runs_inactive"], 0)
        for run, view in runs.items():
            with self.subTest(run):
                recorded = json.loads((self.run_dir(run) / "state.json").read_text())["jobs"]["fix-a"]["updated_at"]
                self.assertAlmostEqual(history.epoch_of(view["last_activity_at"]), history.epoch_of(recorded), places=5)
                self.assertLess(history.epoch_of(view["last_activity_at"]), time.time() - 12 * HOUR)
                self.assertEqual(len(view["jobs"]), 1)

    def test_a_stale_supervisor_does_not_keep_a_quiet_run_on_the_board(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        control = {"status": "running", "pid": dead.pid, "started_at": stamp_ago(13 * HOUR)}
        self.quiet_run(control=control, status="running", worker_phase="build", phase="build", attempt=1)
        payload = self.board()
        self.assertEqual(self.listed(payload), [])
        self.assertEqual(payload["repos"][0]["runs_inactive"], 1)

    @unittest.skipUnless(memwatch.supported(), "the start time of a process is read from /proc")
    def test_a_reused_pid_does_not_keep_a_quiet_run_on_the_board(self):
        # A supervisor killed in an earlier container left its record saying running; an unrelated process got its id.
        other = subprocess.Popen(["sleep", "60"])
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        control = {"status": "running", "pid": other.pid, "started_at": stamp_ago(200 * HOUR)}
        self.quiet_run(hours=200, control=control, status="running", worker_phase="build", phase="build", attempt=1)
        payload = self.board()
        self.assertEqual(self.listed(payload), [])
        self.assertEqual(payload["repos"][0]["runs_inactive"], 1)
        # without /proc only the id can be probed (docs/manager.md): the record then reads as running
        with patch.object(memwatch, "supported", return_value=False):
            payload = self.board()
        self.assertEqual([(run["run"], run["supervisor"]["state"]) for run in payload["repos"][0]["runs"]], [("r1", "running")])

    def test_a_started_run_with_unreadable_state_falls_back_to_its_files(self):
        # Its job stamps cannot be read, so the time of its files stands in for them: it stays listed, with its error.
        old = {"status": "stopped", "started_at": stamp_ago(14 * HOUR), "finished_at": stamp_ago(13 * HOUR)}
        directory = self.quiet_run(control=old, status="pr_ready", pr=PR, attempt=1)
        (directory / "state.json").write_text("{not json", encoding="utf-8")
        payload = self.board()
        runs = payload["repos"][0]["runs"]
        self.assertEqual([(run["run"], run.get("error")) for run in runs], [("r1", "state unreadable")])
        self.assertEqual(payload["repos"][0]["runs_inactive"], 0)
        newest = os.lstat(directory / "state.json").st_mtime
        self.assertAlmostEqual(history.epoch_of(runs[0]["last_activity_at"]), newest, places=5)
        self.age(directory, 13)
        payload = self.board()
        self.assertEqual(self.listed(payload), [])
        self.assertEqual(payload["repos"][0]["runs_inactive"], 1)

    def test_a_fresh_run_is_listed_with_its_last_activity(self):
        directory = self.plan_run(status="running", worker_phase="build", phase="build", attempt=1)
        recorded = json.loads((directory / "state.json").read_text())["jobs"]["fix-a"]["updated_at"]
        run = self.board()["repos"][0]["runs"][0]
        self.assertAlmostEqual(history.epoch_of(run["last_activity_at"]), history.epoch_of(recorded), places=5)
        self.assertRegex(run["last_activity_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")

    def test_the_supervisor_record_is_activity_too(self):
        directory = self.quiet_run(status="pr_ready", pr=PR, attempt=1)
        finished = stamp_ago(HOUR)
        self.write_control(directory, status="drained", started_at=stamp_ago(3 * HOUR), finished_at=finished)
        run = self.board()["repos"][0]["runs"][0]
        self.assertEqual((run["run"], run["supervisor"]["state"]), ("r1", "idle"))
        self.assertAlmostEqual(history.epoch_of(run["last_activity_at"]), history.epoch_of(finished), places=5)

    def test_the_recorded_stamps_decide_and_a_file_written_later_does_not_bring_a_run_back(self):
        # Old stamps in files that were written just now (a copy that did not keep the times, a rewrite that changed
        # nothing): the stamps are the activity; the times of the files only stand in for a run that recorded none.
        with patch("factory.state.timestamp", return_value=stamp_ago(13 * HOUR)):
            self.plan_run(status="pr_ready", pr=PR, attempt=1)
        payload = self.board()
        self.assertEqual(self.listed(payload), [])
        self.assertEqual(payload["repos"][0]["runs_inactive"], 1)

    def test_a_run_that_recorded_no_stamp_is_as_active_as_its_files(self):
        fresh = self.plan_run("r1", started=False)
        self.age(self.plan_run("r2", started=False), 13)
        payload = self.board()
        self.assertEqual(self.listed(payload), ["r1"])
        self.assertEqual(payload["repos"][0]["runs_inactive"], 1)
        newest = max(os.lstat(path).st_mtime for path in (fresh, fresh / "plan.json"))
        self.assertAlmostEqual(history.epoch_of(payload["repos"][0]["runs"][0]["last_activity_at"]), newest, places=5)
        self.assertEqual([job["status"] for job in payload["repos"][0]["runs"][0]["jobs"]], ["not_started"])

    def test_the_time_of_a_file_is_never_read_through_a_symlink(self):
        directory = self.plan_run(started=False)
        target = self.data_dir.parent / "fresh-plan.json"
        (directory / "plan.json").rename(target)
        os.symlink(target, directory / "plan.json")
        self.age(directory, 13)  # the link and the directory are old; the file the link points to was written now
        payload = self.board()
        self.assertEqual(self.listed(payload), [])
        self.assertEqual(payload["repos"][0]["runs_inactive"], 1)

    def test_quiet_runs_take_neither_the_run_cap_nor_the_job_rows(self):
        for run in ("r1", "r2"):
            self.plan_run(run, status="running", worker_phase="build", phase="build", attempt=1)
        for run in ("r3", "r4", "r5"):  # newer names, so they are scanned first
            self.quiet_run(run)
        with patch.object(dashboard, "MAX_ACTIVE_RUNS", 2), patch.object(dashboard, "MAX_ACTIVE_ROWS", 2):
            payload = self.board()
        repo = payload["repos"][0]
        self.assertEqual(self.listed(payload), ["r1", "r2"])
        self.assertEqual([len(run["jobs"]) for run in repo["runs"]], [1, 1])
        self.assertEqual((repo["runs_total"], repo["runs_inactive"], payload["truncated"]), (5, 3, False))

    def test_the_cap_keeps_the_newest_active_runs_and_they_get_the_job_rows_first(self):
        for run in ("r1", "r2", "r3"):
            self.plan_run(run, status="running", worker_phase="build", phase="build", attempt=1)
        with patch.object(dashboard, "MAX_ACTIVE_RUNS", 2), patch.object(dashboard, "MAX_ACTIVE_ROWS", 1):
            payload = self.board()
        repo = payload["repos"][0]
        self.assertEqual(self.listed(payload), ["r2", "r3"])  # still listed oldest first
        self.assertEqual([(len(run["jobs"]), run["jobs_total"]) for run in repo["runs"]], [(0, 1), (1, 1)])
        self.assertEqual((repo["runs_total"], repo["runs_inactive"], payload["truncated"]), (3, 0, True))

    def test_a_cut_scan_reads_the_newest_run_directories_and_says_so(self):
        for run in ("r1", "r2", "r3"):
            self.plan_run(run, started=False)
        with patch.object(mgr, "MAX_LISTED_RUNS", 2):
            payload = self.board()
        self.assertEqual(self.listed(payload), ["r2", "r3"])
        self.assertTrue(payload["truncated"])

    def test_the_window_is_twelve_hours_before_the_time_of_the_answer(self):
        self.plan_run(status="running", worker_phase="build", phase="build", attempt=1)
        self.assertEqual(dashboard.ACTIVE_WINDOW_SECONDS, 43200)
        for hours, listed in ((0, ["r1"]), (11.9, ["r1"]), (12.1, [])):
            with self.subTest(hours=hours), patch.object(dashboard, "timestamp", return_value=stamp_ago(-hours * HOUR)):
                payload = self.board()
                self.assertEqual(payload["active_window_seconds"], 43200)
                self.assertEqual(self.listed(payload), listed)
                self.assertEqual(payload["repos"][0]["runs_inactive"], 1 - len(listed))

    def test_a_quiet_run_is_told_by_the_time_of_its_files_without_reading_its_state(self):
        old = self.quiet_run("r1", status="pr_ready", pr=PR, attempt=1)
        fresh = self.plan_run("r2", status="running", worker_phase="build", phase="build", attempt=1)
        reads = []
        real = dashboard.read_json

        def spy(path, *args, **kwargs):
            reads.append(path)
            return real(path, *args, **kwargs)

        with patch.object(dashboard, "read_json", spy):
            payload = self.board()
        self.assertEqual(self.listed(payload), ["r2"])
        self.assertNotIn(old / "state.json", reads)
        self.assertNotIn(old / "plan.json", reads)
        self.assertIn(old / "supervisor-control.json", reads)  # the one small read a quiet run costs: is its supervisor live?
        self.assertEqual(reads.count(fresh / "state.json"), 1)  # read once, for its activity and for its rows


class HistoryTests(DashboardCase):
    def setUp(self):
        super().setUp()
        self.history_scenario()

    def runs(self, query=""):
        return self.body(f"/history/runs{query}")

    def names(self, payload):
        return [run["run"] for run in payload["runs"]]

    def test_purged_runs_are_listed_although_no_run_directory_exists(self):
        self.assertFalse(self.run_dir("r-merged").exists())
        payload = self.runs()
        self.assertEqual(self.names(payload), ["r-blocked", "r-aband", "r-merged"])  # newest first
        merged = payload["runs"][2]
        self.assertEqual((merged["repo"], merged["issues"], merged["outcome"], merged["purge_reason"]), ("org/name", [1], "merged", "terminal"))
        self.assertEqual((merged["cost_usd"], merged["planner_cost_usd"], merged["last_result"]), (2.5, 0.5, "stopped"))
        self.assertEqual(merged["jobs"][0]["pr"], PR)
        self.assertEqual(merged["jobs"][0]["outcome"], "merged")
        self.assertTrue(merged["purged_at"])
        aband = payload["runs"][1]
        self.assertEqual((aband["outcome"], aband["purge_reason"]), ("abandoned", "abandoned"))
        blocked = payload["runs"][0]
        self.assertEqual((blocked["repo"], blocked["outcome"], blocked["purged_at"]), ("org/other", "blocked", None))

    def test_the_outcome_repository_and_date_filters(self):
        self.assertEqual(self.names(self.runs("?outcome=merged")), ["r-merged"])
        self.assertEqual(self.names(self.runs("?outcome=abandoned")), ["r-aband"])
        self.assertEqual(self.names(self.runs("?outcome=blocked")), ["r-blocked"])
        self.assertEqual(self.names(self.runs("?outcome=closed")), [])
        self.assertEqual(self.names(self.runs("?repo=org/name")), ["r-aband", "r-merged"])
        self.assertEqual(self.names(self.runs("?repo=org/other&outcome=merged")), [])
        self.assertEqual(self.names(self.runs("?since=2026-02-01")), ["r-blocked", "r-aband"])
        self.assertEqual(self.names(self.runs("?until=2026-01-31")), ["r-merged"])  # a date covers its whole day
        self.assertEqual(self.names(self.runs("?since=2026-02-01&until=2026-02-01")), ["r-aband"])
        self.assertEqual(self.names(self.runs("?since=2026-02-01T00:00:00Z&until=2026-03-01T00:00:00Z&repo=org/name")), ["r-aband"])

    def test_pages_follow_a_cursor_and_nothing_repeats_or_is_missed(self):
        seen, cursor, pages = [], None, 0
        while True:
            payload = self.runs(f"?limit=1{f'&cursor={cursor}' if cursor else ''}")
            seen.extend(self.names(payload))
            pages += 1
            cursor = payload["next_cursor"]
            if cursor is None:
                break
        self.assertEqual((seen, pages), (["r-blocked", "r-aband", "r-merged"], 3))
        self.assertEqual(self.runs("?limit=3")["next_cursor"], None)
        self.assertIsNotNone(self.runs("?limit=2")["next_cursor"])

    def test_the_job_list_has_the_same_filters_and_pages(self):
        payload = self.body("/history/jobs")
        self.assertEqual([(job["run"], job["job"], job["outcome"]) for job in payload["jobs"]], [("r-blocked", "fix-c", "blocked"), ("r-aband", "fix-b", "abandoned"), ("r-merged", "fix-a", "merged")])
        merged = payload["jobs"][2]
        self.assertEqual((merged["repo"], merged["pr"], merged["cost_usd"], merged["purged"], merged["purge_reason"]), ("org/name", PR, 2.0, True, "terminal"))
        self.assertEqual([job["job"] for job in self.body("/history/jobs?outcome=merged")["jobs"]], ["fix-a"])
        self.assertEqual([job["job"] for job in self.body("/history/jobs?run=r-aband")["jobs"]], ["fix-b"])
        self.assertEqual([job["job"] for job in self.body("/history/jobs?repo=org/other")["jobs"]], ["fix-c"])
        first = self.body("/history/jobs?limit=1")
        second = self.body(f"/history/jobs?limit=1&cursor={first['next_cursor']}")
        self.assertEqual((first["jobs"][0]["job"], second["jobs"][0]["job"]), ("fix-c", "fix-b"))

    def test_a_malformed_query_is_400_and_names_only_a_known_parameter(self):
        marker = "MARKER-NOT-A-VALUE"
        for path, parameter in {
            "/history/runs?outcome=bogus": "outcome",
            f"/history/runs?outcome={marker}": "outcome",
            "/history/runs?repo=not-a-repo": "repo",
            "/history/runs?limit=0": "limit",
            "/history/runs?limit=101": "limit",
            "/history/runs?limit=-1": "limit",
            "/history/runs?limit=abc": "limit",
            "/history/runs?limit=": "limit",
            "/history/runs?since=yesterday": "since",
            "/history/runs?until=2026-13-45": "until",
            "/history/runs?until=9999-12-31": "until",  # a day after it is out of range
            "/history/runs?cursor=junk": "cursor",
            "/history/runs?cursor=1.000000|org/name": "cursor",
            "/history/jobs?limit=201": "limit",
            "/history/jobs?run=-bad": "run",
            f"/history/runs?{marker}=1": "query",
            "/history/runs?run=r1": "query",  # a jobs-only filter
            "/history/runs?limit=1&limit=2": "query",
            "/history/runs?outcome": "query",
            f"/history/runs?{'a=1&' * 30}": "query",
            f"/history/runs?outcome={'x' * 2000}": "query",
        }.items():
            with self.subTest(path[:60]):
                status, payload, _ = self.get(path)
                self.assertEqual((status, payload), (400, {"error": "invalid query parameter", "parameter": parameter}))
                self.assertNotIn(marker, json.dumps(payload))

    def test_a_cursor_of_the_other_list_is_refused(self):
        run_cursor = self.runs("?limit=1")["next_cursor"]
        job_cursor = self.body("/history/jobs?limit=1")["next_cursor"]
        self.assertEqual(self.get(f"/history/jobs?cursor={run_cursor}")[:2], (400, {"error": "invalid query parameter", "parameter": "cursor"}))
        self.assertEqual(self.get(f"/history/runs?cursor={job_cursor}")[:2], (400, {"error": "invalid query parameter", "parameter": "cursor"}))

    def test_the_response_is_bounded_however_large_the_ledger(self):
        ledger = Ledger(self.data_dir)
        for number in range(400):
            for job in range(3):
                ledger.append("job", "org/name", f"bulk-{number}", job=f"j{job}", status="merged", attempt=1, cost_usd=1.5)
        for path, key, cap in (("/history/runs?limit=100", "runs", 100), ("/history/jobs?limit=200", "jobs", 200), ("/history/runs", "runs", 25)):
            payload = self.body(path)
            self.assertEqual(len(payload[key]), cap)
            self.assertIsNotNone(payload["next_cursor"])
        self.assertLess(len(json.dumps(self.body("/history/runs?limit=100"))), 300_000)

    def test_the_bytes_read_are_capped_and_the_answer_says_older_entries_were_left(self):
        with patch.object(history, "DEFAULT_READ_BYTES", 400):
            payload = self.runs()
        self.assertTrue(payload["ledger"]["truncated"])
        self.assertEqual(self.names(payload), ["r-blocked", "r-aband"][: len(payload["runs"])])
        self.assertLess(len(payload["runs"]), 3)
        self.manager.dashboard._folded = None  # the cache is keyed on the files, not on the cap
        self.assertFalse(self.runs()["ledger"]["truncated"])

    def test_the_ledger_is_read_again_only_when_it_changed(self):
        self.runs()
        with patch.object(Ledger, "read", autospec=True, side_effect=Ledger.read) as read:
            self.runs()
            self.runs("?outcome=merged")
            self.assertEqual(read.call_count, 0)
            Ledger(self.data_dir).append("job", "org/name", "r-new", job="a", status="running")
            self.assertIn("r-new", self.names(self.runs()))
            self.assertEqual(read.call_count, 1)

    def test_history_that_does_not_exist_yet_is_an_empty_list(self):
        for path in self.history_dir().iterdir():
            path.unlink()
        self.assertEqual(self.runs()["runs"], [])
        self.assertEqual(self.runs()["ledger"]["segments"], 0)

    def history_dir(self):
        return self.data_dir / "history"

    def test_the_ledger_info_is_part_of_every_history_answer(self):
        info = self.runs()["ledger"]
        self.assertEqual((info["max_bytes"], info["retention_days"], info["truncated"]), (history.DEFAULT_MAX_BYTES, 365, False))
        self.assertGreater(info["bytes"], 0)


class JobDetailTests(DashboardCase):
    def test_a_live_job_is_read_from_the_run_with_its_timeline_and_review(self):
        self.plan_run(
            status="running",
            worker_phase="review",
            phase="review",
            attempt=2,
            commit="b" * 40,
            usage=[{"total_cost_usd": 1.25}],
            review={"pass": False, "findings": ["first finding", f"second\nfinding {CANARY}"]},
            error=CANARY,
            retry_history=[{"reason": CANARY}],
        )
        payload = self.body("/repos/org/name/runs/r1/jobs/fix-a")
        self.assertEqual(payload["source"], "state")
        self.assertEqual((payload["repo"], payload["run"], payload["job"], payload["issues"]), ("org/name", "r1", "fix-a", [1]))
        self.assertEqual((payload["status"], payload["phase"], payload["attempt"], payload["commit"], payload["cost_usd"]), ("running", "review", 2, "b" * 40, 1.25))
        self.assertEqual(payload["review"]["passed"], False)
        self.assertEqual(payload["review"]["findings_total"], 2)
        self.assertIn("first finding", payload["review"]["findings"])
        self.assertTrue(all("\n" not in finding for finding in payload["review"]["findings"]))  # one printable line each
        events = payload["phase_events"]
        self.assertEqual(events[-1]["phase"], "review")
        self.assertEqual({"at", "status", "phase", "attempt"} <= set(events[-1]), True)
        text = json.dumps(payload)
        self.assertNotIn("previous_error", text)
        self.assertNotIn('"error"', text)
        self.assertNotIn("reason", [key for key in payload])  # the retry reasons are not served

    def test_findings_are_clipped(self):
        self.plan_run(status="failed", review={"pass": False, "findings": ["x" * 5000] * 50})
        review = self.body("/repos/org/name/runs/r1/jobs/fix-a")["review"]
        self.assertEqual((len(review["findings"]), review["findings_total"]), (dashboard.MAX_FINDINGS, 50))
        self.assertTrue(all(len(finding) <= 500 for finding in review["findings"]))

    def test_a_purged_job_is_read_from_the_ledger(self):
        self.history_scenario()
        payload = self.body("/repos/org/name/runs/r-merged/jobs/fix-a")
        self.assertEqual(payload["source"], "ledger")
        self.assertEqual((payload["status"], payload["outcome"], payload["pr"], payload["purged"], payload["purge_reason"]), ("merged", "merged", PR, True, "terminal"))
        self.assertEqual([(step["status"], step.get("phase")) for step in payload["timeline"]], [("running", "build"), ("merged", "publish")])
        self.assertEqual(payload["history"]["outcome"], "merged")
        self.assertNotIn("review", payload)

    def test_a_live_job_also_says_what_the_ledger_knows(self):
        self.plan_run(status="pr_ready", pr=PR, attempt=1)
        self.write_history((0, "org/name", "r1", "pr_observed", {"job": "fix-a", "pr": PR, "handoff_status": "merged", "observed_at": "2026-01-01T00:00:00Z"}))
        payload = self.body("/repos/org/name/runs/r1/jobs/fix-a")
        self.assertEqual(payload["source"], "state")
        self.assertEqual(payload["pr_observed"], {"handoff_status": "merged", "observed_at": "2026-01-01T00:00:00Z", "fresh": None})
        self.assertEqual(payload["history"]["outcome"], "merged")

    def test_a_run_whose_directory_went_but_whose_job_the_ledger_knows_is_not_a_404(self):
        self.plan_run(status="running", attempt=1)
        self.history_scenario()
        self.assertEqual(self.get("/repos/org/name/runs/r-aband/jobs/fix-b")[0], 200)
        self.assertEqual(self.get("/repos/org/name/runs/r-aband/jobs/fix-a")[0], 404)  # another job of another run

    def test_an_unreadable_state_falls_back_to_the_ledger_or_a_404(self):
        directory = self.plan_run(status="running", attempt=1)
        (directory / "state.json").write_text("{broken")
        self.assertEqual(self.get("/repos/org/name/runs/r1/jobs/fix-a")[0], 404)
        self.write_history((0, "org/name", "r1", "job", {"job": "fix-a", "status": "running", "attempt": 1}))
        self.assertEqual(self.body("/repos/org/name/runs/r1/jobs/fix-a")["source"], "ledger")

    def test_a_job_the_plan_does_not_have_is_a_404(self):
        self.plan_run(status="running", attempt=1)
        self.assertEqual(self.get("/repos/org/name/runs/r1/jobs/fix-z")[0], 404)

    def test_no_parameter_is_accepted(self):
        self.plan_run(status="running", attempt=1)
        self.assertEqual(self.get("/repos/org/name/runs/r1/jobs/fix-a?x=1")[0], 400)

    def test_no_log_is_served_by_any_endpoint(self):
        directory = self.plan_run(status="running", attempt=1)
        (directory / "fix-a").mkdir()
        (directory / "fix-a" / "1-build-0.jsonl").write_text(f"{CANARY}\n")
        (directory / "supervisor.log").write_text(f"{CANARY}\n")
        for path in (*ENDPOINTS, "/repos/org/name/runs/r1/jobs/fix-a/log", "/repos/org/name/runs/r1/logs", "/repos/org/name/runs/r1/log"):
            status, payload, _ = self.get(path)
            self.assertNotIn("CANARY", json.dumps(payload), path)


class OperatorEntryTests(DashboardCase):
    def ledger_events(self, kind="operator"):
        return [event for event in Ledger(self.data_dir).read().events if event["kind"] == kind]

    def test_a_successful_action_is_recorded_with_the_principal_and_the_audit_line_agrees(self):
        self.make_run()
        status, _, _ = self.call("POST", self.path("drain"), body={})
        self.assertEqual(status, 200)
        status, _, _ = self.call("POST", self.path("retry"), body={"job": "fix-a", "reason": f"because {CANARY}"})
        self.assertEqual(status, 200)
        events = self.ledger_events()
        self.assertEqual([(e["action"], e["principal"], e["http"], e["repo"], e["run"]) for e in events], [("drain", tm.OID, 200, "org/name", "r1"), ("retry", tm.OID, 200, "org/name", "r1")])
        self.assertEqual(events[1]["job"], "fix-a")
        self.assertNotIn("job", events[0])
        self.assertNotIn(CANARY, json.dumps(events))  # a reason is free text: it is not in the ledger
        self.assertIn(f"audit principal={tm.OID} method=POST path=/repos/org/name/runs/r1/drain status=200", self.audit_lines()[0])

    def test_the_entry_is_part_of_the_run_in_the_history(self):
        self.make_run()
        self.call("POST", self.path("drain"), body={})
        run = self.body("/history/runs")["runs"][0]
        self.assertEqual([(op["action"], op["principal"]) for op in run["operators"]], [("drain", tm.OID)])

    def test_a_refused_or_failed_action_is_not_recorded(self):
        self.make_run()
        self.assertEqual(self.call("POST", self.path("drain"), body={}, principal=tm.VIEWER)[0], 403)
        self.assertEqual(self.call("POST", self.path("drain"), body={}, principal=None)[0], 401)
        self.assertEqual(self.call("POST", self.path("retry"), body={})[0], 400)  # a required field is missing
        self.runner.respond = lambda argv: CommandResult(1, b"", b"refused")
        self.assertEqual(self.call("POST", self.path("drain"), body={})[0], 422)
        self.assertEqual(self.ledger_events(), [])

    def test_a_read_is_not_recorded(self):
        self.make_run()
        self.get("/dashboard")
        self.call("GET", self.path("status"))
        self.assertEqual(self.ledger_events(), [])
        self.assertFalse((self.data_dir / "history").exists())


class JwtDashboardTests(tm.JwtCase):
    def setUp(self):
        super().setUp()
        directory = self.run_dir()
        directory.mkdir(parents=True)
        plan = assignment()
        atomic_json(directory / "plan.json", plan)
        State(directory, plan).update("fix-a", status="running", worker_phase="build", phase="build", attempt=1)

    def test_a_token_is_required_for_every_new_endpoint(self):
        for path in (*ENDPOINTS, "/history", "/dashboard?x=1", "/repos/no/body/runs/r1/jobs/x"):
            for method in ("GET", "POST"):
                with self.subTest(path=path, method=method):
                    status, payload, headers = self.jcall(method, path, None)
                    self.assertEqual((status, payload), tm.UNAUTHENTICATED)
                    self.assertEqual(headers["WWW-Authenticate"], "Bearer")
                    self.assertEqual(self.jcall(method, path, None, principal=tm.OPERATOR)[0], 401)  # Easy Auth header ignored
        self.assertEqual((self.runner.calls, self.spawner.calls), ([], []))

    def test_a_token_without_a_factory_role_is_403(self):
        for roles in ([], ["Some.Other.Role"], ["factory.viewer"]):
            for path in ENDPOINTS:
                with self.subTest(roles=roles, path=path):
                    self.assertEqual(self.jcall("GET", path, self.token(roles=roles))[:2], (403, {"error": "forbidden"}))

    def test_a_viewer_and_an_operator_may_read_and_neither_may_write(self):
        for roles in (["Factory.Viewer"], ["Factory.Operator"]):
            token = self.token(roles=roles)
            for path in ENDPOINTS:
                with self.subTest(roles=roles, path=path):
                    self.assertEqual(self.jcall("GET", path, token)[0], 200)
                    status, payload, headers = self.jcall("POST", path, token, body={})
                    self.assertEqual((status, headers["Allow"]), (405, "GET"))
        self.assertEqual((self.runner.calls, self.spawner.calls), ([], []))

    def test_an_unknown_path_is_404_only_after_the_token_was_checked(self):
        self.assertEqual(self.jcall("GET", "/history/other", self.token(roles=["Factory.Viewer"]))[:2], (404, {"error": "not found"}))

    def test_the_operator_entry_carries_the_verified_identity(self):
        status, _, _ = self.jcall("POST", self.path("drain"), self.token(), body={})
        self.assertEqual(status, 200)
        events = [event for event in Ledger(self.data_dir).read().events if event["kind"] == "operator"]
        self.assertEqual([(e["action"], e["principal"]) for e in events], [("drain", "operator@example.invalid")])

    def test_a_forged_easy_auth_header_cannot_write_an_entry(self):
        self.assertEqual(self.jcall("POST", self.path("drain"), None, principal=tm.OPERATOR, body={})[0], 401)
        self.assertEqual([event for event in Ledger(self.data_dir).read().events if event["kind"] == "operator"], [])


if __name__ == "__main__":
    unittest.main()

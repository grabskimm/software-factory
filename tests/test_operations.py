"""The operations loop: restart recovery, cost guardrail and auto-purge (docs/manager.md).

Everything runs against fakes in the style of tests/test_manager.py: a runner that
answers the CLI commands from a script (and removes the run directory when it is
"purged", as the real CLI does), a spawner that records the argv of started children, a
lease whose answers are scripted, and a clock and a wait that never sleep. Each test
drives one pass (``recover_all``, ``check_budgets``, ``sweep``) directly; the thread and
shutdown tests use real events with long intervals.

Every status payload the fake CLI returns carries a canary string in the fields a
real report fills with agent-authored text (job errors, blockers, next actions); the
base class asserts after every test that the canary never reached the manager's stdout
or stderr.
"""

import io
import itertools
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import unittest.mock
from dataclasses import replace
from pathlib import Path

import factory
from factory import history
from factory import manager as mgr
from factory import locks
from factory import operations as ops
from factory import podhandle, purge, recovery
from factory.config import RepoConfig
from factory.locks import LockBusy, LockUnavailable
from factory.manager import ApiError, Child, CommandResult, Manager, Settings
from factory.operations import Operations, Policy
from factory.state import timestamp
from tests.test_manager import FakeLock, FakeProcess, FakeRunner, FakeSpawner

SRC = str(Path(factory.__file__).resolve().parent.parent)
CANARY = "CANARY-8d41-agent-authored-text"
NOW = 1_800_000_000.0
DAY = 24 * 3600
TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z")


def report(*jobs, cost=0.0, workers=0, supervisor=None):
    """A ``status --json`` document. A job is (id, status) or (id, status, handoff_status)."""
    rows = []
    for job in jobs:
        ident, status, handoff = (*job, None)[:3]
        rows.append(
            {
                "id": ident,
                "status": status,
                "handoff_status": handoff,
                "error": CANARY,
                "next_action": CANARY,
                "blockers": [{"id": "b1", "cause": CANARY, "remedy": CANARY}],
            }
        )
    return {
        "jobs": rows,
        "cost_usd": cost,
        "active_worker_count": workers,
        "supervisor": supervisor,
        "blockers": {"b1": {"cause": CANARY}},
    }


def text(output):
    return CommandResult(0, output.encode())


def failure(code=1):
    """A CLI refusal whose streams carry the canary: they must never be logged."""
    return CommandResult(code, f"out {CANARY}".encode(), f"err {CANARY}".encode())


def split_argv(argv):
    """(repo, run or None, command, tail) of a fake-CLI argv: ``fake-cli --repo R [--run-dir D] cmd ...``."""
    rest, repo, run = list(argv[1:]), None, None
    while rest and rest[0] in ("--repo", "--run-dir"):
        option, value, rest = rest[0], rest[1], rest[2:]
        if option == "--repo":
            repo = value
        else:
            run = Path(value).name
    return repo, run, rest[0], rest[1:]


class OpsRunner(FakeRunner):
    """The CLI as the operations loop sees it, scripted per command.

    ``override[(command, repo, run)]``, ``[(command, repo)]`` or ``[(command,)]`` is a
    ``CommandResult``, an exception to raise, or a callable ``(repo, run, tail)``.
    """

    def __init__(self, data_dir):
        super().__init__()
        self.data_dir = data_dir
        self.reports = {}
        self.recovered = {}
        self.override = {}
        self.respond = self.answer

    @property
    def seen(self):
        return [split_argv(argv)[:3] for argv in self.argvs]

    def commands(self, name):
        return [(repo, run) for repo, run, command in self.seen if command == name]

    def answer(self, argv):
        repo, run, command, tail = split_argv(argv)
        for key in ((command, repo, run), (command, repo), (command,)):
            if key in self.override:
                chosen = self.override[key]
                if isinstance(chosen, BaseException):
                    raise chosen
                return chosen(repo, run, tail) if callable(chosen) else chosen
        return getattr(self, f"default_{command.replace('-', '_')}")(repo, run, tail)

    def default_recover(self, repo, run, tail):
        return text(f"Recovered {self.recovered.get((repo, run), 0)} interrupted job(s)\n")

    def default_status(self, repo, run, tail):
        return text(json.dumps(self.reports.get((repo, run), report())))

    def default_reconcile(self, repo, run, tail):
        return text("Reconciled\n")

    def default_retry(self, repo, run, tail):
        return text("Retried\n")

    def default_drain(self, repo, run, tail):
        return text(f"Drain requested for supervisor PID 1. {CANARY}\n")

    def default_purge(self, repo, run, tail):
        shutil.rmtree(self.data_dir / "repos" / repo / "runs" / run)  # what the real purge does
        return text("Purged\n")

    def default_purge_clone(self, repo, run, tail):
        return text("Removed clone\n")


class FakeTime:
    """A clock that only moves when something waits; ``stop_after`` makes the n-th wait report shutdown."""

    def __init__(self, now=NOW):
        self.now = now
        self.waits = []
        self.stop_after = None
        self.on_wait = None

    def clock(self):
        return self.now

    def wait(self, seconds):
        if len(self.waits) >= 200:  # a loop that never gives up fails the test instead of hanging it
            raise AssertionError("runaway wait loop")
        self.waits.append(seconds)
        self.now += seconds
        if self.on_wait is not None:
            self.on_wait(len(self.waits))
        return self.stop_after is not None and len(self.waits) >= self.stop_after


class OpsCase(unittest.TestCase):
    """Config for org/name and org/other, a manager on fakes, and an ``Operations`` ready to drive."""

    cli = ("fake-cli",)

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        self.config_dir, self.data_dir = root / "config", root / "data"
        (self.config_dir / "repos").mkdir(parents=True)
        for name in ("org__name", "org__other"):
            (self.config_dir / "repos" / f"{name}.toml").write_text(f'repo = "{name.replace("__", "/")}"\n')
        self.data_dir.mkdir()
        self.out, self.err = io.StringIO(), io.StringIO()
        self.runner, self.spawner = OpsRunner(self.data_dir), FakeSpawner()
        self.time = FakeTime()
        self.policies = {}
        self.leases = {}  # repo -> iterator of "free" | "busy" | "unavailable"
        self.probes = []
        self.pids = itertools.count(8000)
        self.manager = self.new_manager()
        self.ops = self.new_ops(self.manager)

    def tearDown(self):
        self.assertNotIn(CANARY, self.out.getvalue() + self.err.getvalue(), "agent-authored text reached the logs")

    def script_lease(self, repo, *states, then="free"):
        """The next probes of ``repo`` answer ``states`` in order, every later one ``then``."""
        self.leases[repo] = itertools.chain(states, itertools.repeat(then))

    def lease(self, cfg, paths):
        self.probes.append(cfg.repo)
        state = next(self.leases.get(cfg.repo, itertools.repeat("free")))
        error = {"free": None, "busy": LockBusy("held"), "unavailable": LockUnavailable(CANARY)}[state]
        return FakeLock(error)

    def new_manager(self, **overrides):
        settings = Settings(
            config_dir=self.config_dir,
            data_dir=self.data_dir,
            cli=self.cli,
            shutdown_seconds=1.0,
            **overrides,
        )
        self.runner.closed = False
        return Manager(
            settings,
            env={},
            runner=self.runner,
            spawner=self.spawner,
            lock_factory=self.lease,
            stdout=self.out,
            stderr=self.err,
        )

    def new_ops(self, manager, **extra):
        return Operations(
            manager,
            clock=self.time.clock,
            wait=self.time.wait,
            policy=lambda cfg: self.policies.get(cfg.repo, Policy()),
            **extra,
        )

    # -- fixtures -------------------------------------------------------------------

    def run_dir(self, run="r1", repo="org/name"):
        return self.data_dir / "repos" / repo / "runs" / run

    def make_run(self, run="r1", repo="org/name", *, state=True, plan=True, age=0, supervisor_age=None):
        """A run directory whose files were last modified ``age`` seconds before the fake clock."""
        directory = self.run_dir(run, repo)
        directory.mkdir(parents=True)
        files = {"plan.json": plan, "state.json": state, "supervisor-control.json": supervisor_age is not None}
        for name, wanted in files.items():
            if wanted:
                (directory / name).write_text("{}")
                moment = NOW - (supervisor_age if name == "supervisor-control.json" else age)
                os.utime(directory / name, (moment, moment))
        os.utime(directory, (NOW - age, NOW - age))
        return directory

    def add_child(self, repo="org/name", run="r1", kind="run", started=None):
        child = Child(kind, repo, run, FakeProcess(next(self.pids)), started or timestamp())
        self.manager.children.register(child)
        return child

    # -- observations ---------------------------------------------------------------

    def lines(self):
        return self.out.getvalue().splitlines()

    def warnings(self):
        return self.err.getvalue().splitlines()

    def names(self, command):
        return self.runner.commands(command)


# --- settings and the policy adapter -------------------------------------------------


class SettingsTests(unittest.TestCase):
    ENV = {"FACTORY_CONFIG": "/etc/factory", "FACTORY_DATA": "/var/lib/factory"}

    def settings(self, **extra):
        return Settings.from_env({**self.ENV, **extra})

    def test_defaults(self):
        settings = self.settings()
        self.assertEqual(
            (settings.operations, settings.lease_wait_seconds, settings.poll_seconds, settings.janitor_seconds),
            (True, 120.0, 60.0, 900.0),
        )

    def test_values_are_read_and_the_switch_is_on_or_off(self):
        settings = self.settings(
            FACTORY_MANAGER_OPERATIONS=" OFF ",
            FACTORY_MANAGER_LEASE_WAIT_SECONDS="0",
            FACTORY_MANAGER_POLL_SECONDS="1",
            FACTORY_MANAGER_JANITOR_SECONDS="3600.5",
        )
        self.assertEqual(
            (settings.operations, settings.lease_wait_seconds, settings.poll_seconds, settings.janitor_seconds),
            (False, 0.0, 1.0, 3600.5),
        )
        self.assertTrue(self.settings(FACTORY_MANAGER_OPERATIONS="on").operations)

    def test_bad_values_are_refused_naming_the_variable_but_not_the_value(self):
        for name, value in (
            ("FACTORY_MANAGER_OPERATIONS", "maybe"),
            ("FACTORY_MANAGER_LEASE_WAIT_SECONDS", "-1"),
            ("FACTORY_MANAGER_LEASE_WAIT_SECONDS", "99999"),
            ("FACTORY_MANAGER_POLL_SECONDS", "0.5"),
            ("FACTORY_MANAGER_POLL_SECONDS", "x7"),
            ("FACTORY_MANAGER_JANITOR_SECONDS", "0.2"),
            ("FACTORY_MANAGER_JANITOR_SECONDS", "nan"),
        ):
            with self.subTest(name, value=value), self.assertRaises(ValueError) as caught:
                self.settings(**{name: value})
            self.assertIn(name, str(caught.exception))
            self.assertNotIn(value, str(caught.exception))

    def test_every_manager_variable_is_documented(self):
        source = (Path(mgr.__file__)).read_text()
        documented = (Path(mgr.__file__).resolve().parents[2] / "docs" / "manager.md").read_text()
        for name in sorted(set(re.findall(r'"(FACTORY_MANAGER_[A-Z_]+)"', source))):
            with self.subTest(name):
                self.assertIn(f"`{name}`", documented)


class PolicyTests(unittest.TestCase):
    def test_a_config_without_the_settings_gets_the_contract_defaults(self):
        self.assertEqual(ops.policy_of(object()), Policy(604800, None, "never"))

    def test_values_are_taken_from_the_config(self):
        class Config:
            abandon_after = 3600
            run_budget_usd = 25
            auto_resume_after_restart = "once"

        self.assertEqual(ops.policy_of(Config()), Policy(3600, 25.0, "once"))
        self.assertIsInstance(ops.policy_of(Config()).run_budget_usd, float)

    def test_values_outside_the_contract_fall_back_to_the_safe_default(self):
        def policy(**values):
            return ops.policy_of(type("Config", (), values)())

        self.assertEqual(policy(abandon_after=60).abandon_after, ops.DEFAULT_ABANDON_AFTER)
        self.assertEqual(policy(abandon_after=True).abandon_after, ops.DEFAULT_ABANDON_AFTER)
        self.assertEqual(policy(abandon_after="7d").abandon_after, ops.DEFAULT_ABANDON_AFTER)
        for bad in (0, -1.0, True, "5", float("nan"), float("inf"), None):
            with self.subTest(bad):
                self.assertIsNone(policy(run_budget_usd=bad).run_budget_usd)
        for bad in ("always", "ONCE", True, None):
            with self.subTest(bad):
                self.assertEqual(policy(auto_resume_after_restart=bad).auto_resume_after_restart, "never")

    @unittest.skipUnless(
        {"abandon_after", "run_budget_usd", "auto_resume_after_restart"} <= set(RepoConfig.__dataclass_fields__),
        "the repository config does not carry the operations settings yet",
    )
    def test_a_real_repository_config_file_carries_the_settings(self):
        from factory.config import load_repo_config

        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "repos").mkdir()
            (Path(directory) / "repos" / "org__name.toml").write_text(
                'repo = "org/name"\nabandon_after = "2d"\nrun_budget_usd = 12.5\nauto_resume_after_restart = "once"\n'
            )
            cfg = load_repo_config("org/name", config_dir=directory)
        self.assertEqual(ops.policy_of(cfg), Policy(2 * DAY, 12.5, "once"))


class MarkerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)

    def test_claim_is_exclusive_and_keeps_the_first_content(self):
        path = self.directory / "auto-resumed"
        self.assertTrue(ops.claim_marker(path, "first"))
        self.assertFalse(ops.claim_marker(path, "second"))
        self.assertEqual(path.read_text(), "first")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_claim_does_not_follow_a_planted_symlink(self):
        target = self.directory / "elsewhere"
        (self.directory / "auto-resumed").symlink_to(target)
        self.assertFalse(ops.claim_marker(self.directory / "auto-resumed", "x"))
        self.assertFalse(target.exists())

    def test_write_replaces_atomically_and_leaves_no_temporary_file(self):
        path = self.directory / "budget-drained"
        ops.write_marker(path, "one")
        ops.write_marker(path, "two")
        self.assertEqual(path.read_text(), "two")
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()), ["budget-drained"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_a_failed_write_leaves_no_temporary_file(self):
        path = self.directory / "budget-drained"
        with unittest.mock.patch.object(os, "replace", side_effect=OSError(28, "no space")):
            with self.assertRaises(OSError):
                ops.write_marker(path, "x")
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_read_never_follows_a_symlink_and_a_missing_marker_is_absent(self):
        self.assertEqual(ops.read_marker(self.directory / "nope"), (False, None))
        (self.directory / "link").symlink_to(self.directory / "real")
        self.assertEqual(ops.read_marker(self.directory / "link"), (True, None))

    def test_a_marker_covers_a_process_unless_it_is_provably_older(self):
        path = self.directory / "budget-drained"
        child = lambda started: Child("run", "org/name", "r1", FakeProcess(1), started)  # noqa: E731
        self.assertFalse(ops.drain_covers(path, child("2026-01-01T00:00:00Z")))
        path.write_text("2026-06-01T00:00:00.5Z")
        self.assertTrue(ops.drain_covers(path, child("2026-01-01T00:00:00Z")))
        self.assertTrue(ops.drain_covers(path, child("2026-06-01T00:00:00.5Z")))
        self.assertFalse(ops.drain_covers(path, child("2026-06-01T00:00:01Z")))
        path.write_text("garbage")  # unreadable content still means a drain happened
        self.assertTrue(ops.drain_covers(path, child("2999-01-01T00:00:00Z")))
        path.write_text("")
        self.assertTrue(ops.drain_covers(path, child("2999-01-01T00:00:00Z")))


class DocumentTests(unittest.TestCase):
    def test_counts_know_the_fixed_statuses_and_put_the_rest_in_other(self):
        counts = ops.count_jobs(report(("a", "queued"), ("b", "queued"), ("c", "waiting_build"), ("d", "interrupted")))
        self.assertEqual((counts["queued"], counts["interrupted"], counts["other"]), (2, 1, 1))
        self.assertEqual(sum(counts.values()), 4)
        self.assertEqual(sum(ops.count_jobs({}).values()), 0)
        self.assertEqual(sum(ops.count_jobs({"jobs": "x"}).values()), 0)
        odd = ops.count_jobs({"jobs": [1, None, {"status": ["x"]}, {"status": "other"}, {}]})
        self.assertEqual((odd["other"], sum(odd.values())), (5, 5))  # a list status is unhashable but harmless

    def test_interrupted_job_ids_that_are_not_plain_ids_never_reach_argv(self):
        document = report(("good-1", "interrupted"), ("../x", "interrupted"), ("--rf", "interrupted"), ("ok", "queued"))
        document["jobs"].append({"id": 5, "status": "interrupted"})
        self.assertEqual(ops.interrupted_jobs(document), ["good-1"])

    def test_terminal_needs_a_job_and_every_job_done(self):
        self.assertTrue(ops.is_terminal(report(("a", "merged"), ("b", "closed"))))
        self.assertTrue(ops.is_terminal(report(("a", "pr_ready", "merged"), ("b", "pr_incomplete", "closed"))))
        self.assertFalse(ops.is_terminal(report(("a", "merged"), ("b", "pr_ready", "published"))))
        self.assertFalse(ops.is_terminal(report(("a", "merged"), ("b", "queued"))))
        self.assertFalse(ops.is_terminal(report()))
        self.assertFalse(ops.is_terminal({}))
        self.assertFalse(ops.is_terminal({"jobs": ["merged"]}))

    def test_a_live_worker_is_any_worker_a_running_supervisor_or_anything_unreadable(self):
        self.assertFalse(ops.has_live_worker(report()))
        self.assertTrue(ops.has_live_worker(report(workers=1)))
        for status in ("running", "draining"):
            self.assertTrue(ops.has_live_worker(report(supervisor={"status": status})))
        for status in ("stale", "drained", "failed"):
            self.assertFalse(ops.has_live_worker(report(supervisor={"status": status})))
        self.assertTrue(ops.has_live_worker({"supervisor": None}))  # no worker count
        self.assertTrue(ops.has_live_worker({"active_worker_count": "0", "supervisor": None}))
        self.assertTrue(ops.has_live_worker({"active_worker_count": 0, "supervisor": "x"}))

    def test_cost_must_be_a_finite_number(self):
        self.assertEqual(ops.cost_of({"cost_usd": 3}), 3.0)
        for bad in ({}, {"cost_usd": "3"}, {"cost_usd": True}, {"cost_usd": float("nan")}, {"cost_usd": float("inf")}):
            with self.subTest(bad):
                self.assertIsNone(ops.cost_of(bad))


# --- restart recovery ----------------------------------------------------------------


class RestartRecoveryTests(OpsCase):
    def test_it_waits_for_a_busy_lease_then_recovers_and_does_not_run(self):
        self.make_run()
        self.script_lease("org/name", "busy", "unavailable")
        self.runner.recovered[("org/name", "r1")] = 2
        self.runner.reports[("org/name", "r1")] = report(("a", "interrupted"), ("b", "interrupted"), ("c", "queued"))
        self.ops.recover_all()
        self.assertEqual(self.time.waits, [ops.LEASE_POLL_SECONDS, ops.LEASE_POLL_SECONDS])
        self.assertEqual(self.runner.seen, [("org/name", "r1", "recover"), ("org/name", "r1", "status")])
        self.assertEqual(self.runner.argvs[0], ["fake-cli", "--repo", "org/name", "--run-dir", str(self.run_dir()), "recover"])
        self.assertEqual(self.runner.argvs[1][-2:], ["status", "--json"])
        self.assertEqual(
            self.lines(),
            [
                "restart repo=org/name run=r1 interrupted=2 queued=1 pr_ready=0 running=0 pr_incomplete=0 "
                "failed=0 blocked=0 needs_human_publish=0 stopped=0 merged=0 closed=0 not_started=0 other=0 recovered=2"
            ],
        )
        self.assertEqual(self.spawner.calls, [])  # never starts a run by itself
        self.assertEqual(self.err.getvalue(), "")

    def test_it_gives_up_after_the_lease_wait_with_one_warning(self):
        self.manager = self.new_manager(lease_wait_seconds=10.0)
        self.ops = self.new_ops(self.manager)
        self.make_run("r1")
        self.make_run("r2")
        self.script_lease("org/name", then="busy")
        self.ops.recover_all()
        self.assertEqual(self.time.waits, [3.0, 3.0, 3.0, 1.0])  # the last step is cut to the budget
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.warnings(), ["factory-manager: restart repo=org/name lease busy after 10s; recovery skipped"])
        self.assertEqual(self.lines(), [])

    def test_a_lease_backend_that_stays_down_is_reported_as_such(self):
        self.manager = self.new_manager(lease_wait_seconds=3.0)
        self.ops = self.new_ops(self.manager)
        self.make_run()
        self.script_lease("org/name", then="unavailable")
        self.ops.recover_all()
        self.assertEqual(self.warnings(), ["factory-manager: restart repo=org/name lease unavailable after 3s; recovery skipped"])

    def test_a_zero_wait_probes_once(self):
        self.manager = self.new_manager(lease_wait_seconds=0.0)
        self.ops = self.new_ops(self.manager)
        self.make_run()
        self.script_lease("org/name", then="busy")
        self.ops.recover_all()
        self.assertEqual((self.time.waits, self.probes), ([], ["org/name"]))

    def test_shutdown_during_the_lease_wait_ends_it_without_a_warning(self):
        self.make_run()
        self.script_lease("org/name", then="busy")
        self.time.stop_after = 2
        self.ops.recover_all()
        self.assertEqual(len(self.time.waits), 2)
        self.assertEqual((self.err.getvalue(), self.runner.calls), ("", []))

    def test_only_runs_with_a_state_file_are_recovered_and_idle_repositories_are_not_touched(self):
        self.make_run("planned", state=False)
        self.make_run("started")
        self.ops.recover_all()
        self.assertEqual(self.names("recover"), [("org/name", "started")])
        self.assertFalse((self.data_dir / "repos" / "org" / "other").exists())  # no probe created its directory
        self.assertEqual(self.probes, ["org/name"])

    def test_every_run_of_every_repository_is_recovered(self):
        for repo in ("org/name", "org/other"):
            self.make_run("r1", repo)
            self.make_run("r2", repo)
        self.ops.recover_all()
        self.assertEqual(
            self.names("recover"),
            [("org/name", "r1"), ("org/name", "r2"), ("org/other", "r1"), ("org/other", "r2")],
        )
        self.assertEqual(len([line for line in self.lines() if line.startswith("restart ")]), 4)

    def test_a_failed_recover_is_one_line_with_the_exit_code_and_the_next_run_goes_on(self):
        self.make_run("r1")
        self.make_run("r2")
        self.runner.override[("recover", "org/name", "r1")] = failure(1)
        self.ops.recover_all()
        self.assertEqual(self.lines()[0], "restart repo=org/name run=r1 failed=recover rc=1")
        self.assertTrue(self.lines()[1].startswith("restart repo=org/name run=r2 interrupted=0"))
        self.assertEqual(self.names("status"), [("org/name", "r2")])  # no status after a failed recover

    def test_a_timed_out_recover_says_so(self):
        self.make_run()
        self.runner.override[("recover",)] = CommandResult(None, timed_out=True)
        self.ops.recover_all()
        self.assertEqual(self.lines(), ["restart repo=org/name run=r1 failed=recover rc=timeout"])

    def test_a_status_that_cannot_be_read_is_reported_with_the_recovered_count(self):
        self.make_run()
        self.runner.recovered[("org/name", "r1")] = 1
        for answer in (failure(1), text("Planned 2 issues in 1 jobs\n"), text("[1]"), CommandResult(0, b"\xff\xfe")):
            with self.subTest(answer.stdout[:12]):
                self.out.truncate(0), self.out.seek(0)
                self.runner.override[("status",)] = answer
                self.ops.recover_all()
                self.assertEqual(self.lines(), ["restart repo=org/name run=r1 failed=status recovered=1"])

    def test_the_recovered_count_comes_from_the_cli_line(self):
        for output, expected in {
            "Recovered 3 interrupted job(s)\n": 3,
            "Nothing to recover\n": 0,
            "Recovered many interrupted job(s)\n": "unknown",
            "": "unknown",
        }.items():
            with self.subTest(output):
                self.assertEqual(ops.recovered_count(text(output)), expected)

    def test_a_process_this_manager_started_in_the_meantime_is_left_alone(self):
        self.make_run()
        self.add_child()
        self.ops.recover_all()
        self.assertEqual(self.lines(), ["restart repo=org/name run=r1 skipped=process-running"])
        self.assertEqual(self.runner.calls, [])

    def test_an_exception_in_one_run_is_a_fixed_line_and_the_others_still_recover(self):
        self.make_run("r1")
        self.make_run("r2")
        self.runner.override[("recover", "org/name", "r1")] = ValueError(CANARY)
        self.ops.recover_all()
        self.assertEqual(self.warnings(), ["factory-manager: operations recovery failed (ValueError)"])
        self.assertEqual(self.names("recover"), [("org/name", "r1"), ("org/name", "r2")])
        self.assertEqual(len(self.lines()), 1)

    def test_a_repository_whose_config_does_not_load_is_skipped_with_one_warning(self):
        (self.config_dir / "repos" / "org__other.toml").write_text("not = [valid")
        self.make_run()
        self.ops.recover_all()
        self.ops.recover_all()
        self.assertEqual(self.warnings(), ["factory-manager: operations: the configuration of org/other does not load"])
        self.assertEqual(self.names("recover"), [("org/name", "r1")] * 2)

    def test_a_full_command_lane_is_retried_before_recovery_gives_up_on_a_run(self):
        self.manager = self.new_manager(max_other_commands=1)
        self.ops = self.new_ops(self.manager)
        self.make_run()
        lane = self.manager._lanes["other"]
        lane.acquire()
        self.time.on_wait = lambda count: lane.release()  # the lane frees up while recovery waits
        with unittest.mock.patch.object(mgr, "SLOT_WAIT_SECONDS", 0.01):
            self.ops.recover_all()
        self.assertEqual(self.time.waits, [ops.SLOT_RETRY_SECONDS])
        self.assertEqual(self.names("recover"), [("org/name", "r1")])
        self.assertEqual(self.err.getvalue(), "")

    def test_a_lane_that_stays_full_is_one_warning_and_no_command(self):
        self.manager = self.new_manager(max_other_commands=1)
        self.ops = self.new_ops(self.manager)
        self.make_run()
        self.manager._lanes["other"].acquire()
        with unittest.mock.patch.object(mgr, "SLOT_WAIT_SECONDS", 0.01):
            self.ops.recover_all()
        self.assertEqual(self.time.waits, [1.0, 1.0])
        self.assertEqual(self.warnings(), ["factory-manager: operations skipped a command: no free command slot"])
        self.assertEqual(self.runner.calls, [])

    def test_a_closed_runner_ends_the_pass_quietly(self):
        self.make_run()
        self.runner.close()
        self.ops.recover_all()
        self.assertEqual((self.out.getvalue(), self.err.getvalue()), ("", ""))

    def test_a_cli_that_cannot_start_is_one_fixed_line(self):
        self.make_run()
        self.runner.override[("recover",)] = FileNotFoundError(2, CANARY)
        self.ops.recover_all()
        self.assertEqual(self.warnings(), ["factory-manager: cannot start the factory CLI (FileNotFoundError)"])


class SlowLeaseProbeTests(OpsCase):
    def test_the_wait_is_a_deadline_on_the_clock_so_slow_probes_use_it_up(self):
        """A blob probe that answers after its request timeout must not multiply the wait."""
        self.make_run()
        self.manager.settings = replace(self.manager.settings, lease_wait_seconds=10.0)

        def slow_busy_probe(cfg, paths):
            self.time.now += 4.0  # the probe itself takes 4 s
            return FakeLock(LockBusy("held"))

        self.manager.lock_factory = slow_busy_probe
        self.ops.recover_all()
        self.assertLessEqual(self.time.now - NOW, 10.0 + 4.0 + 3.0)  # limit plus at most one probe and one poll
        self.assertIn("lease busy after 10s; recovery skipped", self.warnings()[-1])


class AutoResumeTests(OpsCase):
    JOBS = (("a", "interrupted"), ("b", "queued"), ("c", "interrupted"))

    def setUp(self):
        super().setUp()
        self.policies["org/name"] = Policy(auto_resume_after_restart="once")
        self.make_run()
        self.runner.recovered[("org/name", "r1")] = 2
        self.runner.reports[("org/name", "r1")] = report(*self.JOBS)
        self.marker = self.run_dir() / "auto-resumed"

    def retries(self):
        return [argv[argv.index("retry"):] for argv in self.runner.argvs if "retry" in argv]

    def test_each_interrupted_job_is_retried_and_exactly_one_run_is_started(self):
        marker_when_retried = []
        self.runner.override[("retry",)] = lambda repo, run, tail: (
            marker_when_retried.append(self.marker.exists()) or text("Retried\n")
        )
        self.ops.recover_all()
        reason = "--reason=automatic resume after manager restart"
        self.assertEqual(self.retries(), [["retry", "a", reason], ["retry", "c", reason]])
        self.assertEqual(marker_when_retried, [True, True])  # the marker exists before the first retry
        self.assertEqual(self.spawner.argvs, [["fake-cli", "--repo", "org/name", "--run-dir", str(self.run_dir()), "run", "--timeout", "1800"]])
        self.assertEqual(self.spawner.calls[0][1], self.run_dir() / "supervisor.log")
        child = self.manager.children.get("org/name", "r1")
        self.assertEqual((child.kind, child.running()), ("run", True))  # pause, drain and shutdown can see it
        self.assertEqual(self.lines()[-1], f"resume repo=org/name run=r1 retried=2 action=run pid={child.pid}")
        # the order that matters: recover, status, then the retries, and the run last
        self.assertEqual([c for _, _, c in self.runner.seen], ["recover", "status", "retry", "retry"])

    def test_the_resumed_run_gets_the_repository_session_timeout(self):
        (self.config_dir / "repos" / "org__name.toml").write_text('repo = "org/name"\nauto_build_timeout_seconds = 3600\n')
        self.ops.recover_all()
        [argv] = self.spawner.argvs
        self.assertEqual(argv[argv.index("--timeout") + 1], "3600")

    def test_the_resumed_run_keeps_the_session_timeout_when_the_config_cannot_be_read_again(self):
        (self.config_dir / "repos" / "org__name.toml").write_text('repo = "org/name"\nauto_build_timeout_seconds = 7200\n')
        real, calls = self.ops._load, []

        def flaky(repo):
            calls.append(repo)
            return real(repo) if len(calls) == 1 else None  # unreadable from the second read on

        with unittest.mock.patch.object(self.ops, "_load", flaky):
            self.ops.recover_all()
        [argv] = self.spawner.argvs
        self.assertEqual(argv[argv.index("--timeout") + 1], "7200")

    def test_a_run_that_already_spent_its_budget_is_not_resumed_and_keeps_the_marker_free(self):
        """cost_usd is cumulative: a run started over its limit is drained again after paying for a phase."""
        self.policies["org/name"] = Policy(auto_resume_after_restart="once", run_budget_usd=5.0)
        self.runner.reports[("org/name", "r1")] = report(*self.JOBS, cost=5.0)
        self.ops.recover_all()
        self.assertEqual((self.retries(), self.spawner.calls, self.marker.exists()), ([], [], False))
        self.assertEqual(self.lines()[-1], "resume repo=org/name run=r1 skipped=over-budget")

    def test_a_paused_trigger_resumes_nothing_and_keeps_the_marker_free(self):
        self.manager.set_trigger_pause(True)
        self.ops.recover_all()
        self.assertEqual((self.retries(), self.spawner.calls, self.marker.exists()), ([], [], False))
        self.assertEqual(self.lines()[-1], "resume repo=org/name run=r1 skipped=trigger-paused")
        self.assertEqual([c for _, _, c in self.runner.seen], ["recover", "status"])  # recovery itself still runs

    def test_a_resume_held_back_by_the_pause_runs_once_when_the_pause_is_cleared(self):
        """A safe deploy restarts the pod paused: recovery sees the pause, the resume must still happen after it."""
        self.manager.set_trigger_pause(True)
        self.ops.recover_all()
        self.ops.resume_deferred()  # still paused: nothing
        self.assertEqual((self.retries(), self.spawner.calls, self.marker.exists()), ([], [], False))
        self.manager.set_trigger_pause(False)
        self.ops.resume_deferred()
        self.assertEqual(len(self.retries()), 2)
        self.assertEqual(len(self.spawner.calls), 1)
        self.assertTrue(self.marker.exists())  # claimed only now, when it really ran
        self.assertRegex(self.lines()[-1], r"^resume repo=org/name run=r1 retried=2 action=run pid=")
        self.ops.resume_deferred()  # nothing is left to run twice
        self.assertEqual(len(self.spawner.calls), 1)

    def test_a_held_back_resume_is_kept_once_however_often_recovery_sees_the_pause(self):
        self.manager.set_trigger_pause(True)
        self.ops.recover_all()
        self.ops.recover_all()
        self.manager.set_trigger_pause(False)
        self.ops.resume_deferred()
        self.assertEqual(len(self.spawner.calls), 1)

    def test_the_recovery_thread_waits_for_the_pause_to_clear_and_then_resumes(self):
        self.manager.set_trigger_pause(True)
        waits = []

        def wait(seconds):
            waits.append(seconds)
            if len(waits) == 2:
                self.manager.set_trigger_pause(False)  # the deploy's last step
            return False

        self.ops._wait = wait
        self.ops._recover_then_signal()
        self.assertEqual(len(waits), 2)
        self.assertTrue(self.ops._recovered.is_set())
        self.assertEqual(len(self.spawner.calls), 1)

    def test_the_recovery_thread_stops_waiting_at_shutdown_and_resumes_nothing(self):
        self.manager.set_trigger_pause(True)
        self.ops._wait = lambda seconds: True
        self.ops._recover_then_signal()
        self.assertEqual((self.spawner.calls, self.marker.exists()), ([], False))

    def test_a_pause_does_not_stop_a_run_that_is_already_going(self):
        child = self.add_child("org/other", "r9")
        self.manager.set_trigger_pause(True)
        self.ops.recover_all()
        self.assertTrue(child.running())

    def test_a_run_under_its_budget_is_still_resumed(self):
        self.policies["org/name"] = Policy(auto_resume_after_restart="once", run_budget_usd=5.0)
        self.runner.reports[("org/name", "r1")] = report(*self.JOBS, cost=4.99)
        self.ops.recover_all()
        self.assertEqual(len(self.retries()), 2)
        self.assertEqual(len(self.spawner.calls), 1)

    def test_an_unreadable_cost_after_a_budget_drain_counts_as_over_budget(self):
        self.policies["org/name"] = Policy(auto_resume_after_restart="once", run_budget_usd=5.0)
        document = report(*self.JOBS)
        del document["cost_usd"]
        self.runner.reports[("org/name", "r1")] = document
        (self.run_dir() / "budget-drained").write_text(timestamp())
        self.ops.recover_all()
        self.assertEqual(self.spawner.calls, [])
        self.assertEqual(self.lines()[-1], "resume repo=org/name run=r1 skipped=over-budget")

    def test_the_marker_holds_only_a_timestamp(self):
        self.ops.recover_all()
        self.assertRegex(self.marker.read_text(), f"^{TIMESTAMP.pattern}$")
        self.assertEqual(self.marker.stat().st_mode & 0o777, 0o600)

    def test_a_second_manager_start_does_not_resume_again(self):
        self.ops.recover_all()
        self.assertEqual(len(self.spawner.calls), 1)
        second = self.new_manager()
        self.out.truncate(0), self.out.seek(0)
        self.new_ops(second).recover_all()
        self.assertEqual(len(self.retries()), 2)  # still only the first start's retries
        self.assertEqual(len(self.spawner.calls), 1)
        self.assertEqual(self.lines()[-1], "resume repo=org/name run=r1 skipped=already-resumed")
        self.assertIsNone(second.children.get("org/name", "r1"))

    def test_without_the_setting_nothing_is_retried_or_started(self):
        self.policies.clear()
        self.ops.recover_all()
        self.assertEqual((self.retries(), self.spawner.calls, self.marker.exists()), ([], [], False))
        self.assertEqual(len(self.lines()), 1)

    def test_a_run_without_interrupted_jobs_is_not_resumed_and_keeps_its_marker_free(self):
        self.runner.reports[("org/name", "r1")] = report(("a", "queued"), ("b", "pr_ready"))
        self.ops.recover_all()
        self.assertEqual((self.retries(), self.spawner.calls, self.marker.exists()), ([], [], False))

    def test_a_process_started_by_an_operator_in_the_meantime_blocks_the_resume_and_keeps_the_marker_free(self):
        def operator_starts_a_run(repo, run, tail):
            self.add_child("org/name", "other-run")
            return text(json.dumps(self.runner.reports[(repo, run)]))

        self.runner.override[("status",)] = operator_starts_a_run
        self.ops.recover_all()
        self.assertEqual(self.lines()[-1], "resume repo=org/name run=r1 skipped=process-running")
        self.assertEqual((self.retries(), self.spawner.calls, self.marker.exists()), ([], [], False))

    def test_every_run_is_recovered_before_the_first_one_is_resumed_and_only_one_is_started(self):
        self.make_run("r2")
        self.runner.reports[("org/name", "r2")] = report(("x", "interrupted"))
        self.ops.recover_all()
        self.assertEqual([c for _, _, c in self.runner.seen][:4], ["recover", "status", "recover", "status"])
        self.assertEqual(len(self.spawner.calls), 1)
        self.assertEqual(self.lines()[-1], "resume repo=org/name run=r2 skipped=process-running")
        self.assertTrue(self.marker.exists())
        self.assertFalse((self.run_dir("r2") / "auto-resumed").exists())

    def test_a_failing_retry_is_reported_and_the_other_jobs_still_go(self):
        self.runner.override[("retry",)] = lambda repo, run, tail: failure(1) if tail[0] == "a" else text("ok\n")
        self.ops.recover_all()
        self.assertIn("resume repo=org/name run=r1 job=a retry=failed rc=1", self.lines())
        self.assertEqual(len(self.retries()), 2)
        self.assertEqual(len(self.spawner.calls), 1)
        self.assertIn("retried=1 action=run", self.lines()[-1])

    def test_if_every_retry_fails_no_run_is_started_and_the_marker_stays(self):
        self.runner.override[("retry",)] = failure(1)
        self.ops.recover_all()
        self.assertEqual(self.spawner.calls, [])
        self.assertEqual(self.lines()[-1], "resume repo=org/name run=r1 retried=0 action=none")
        self.assertTrue(self.marker.exists())

    def test_a_refused_start_is_one_line_with_the_status_and_the_marker_stays(self):
        self.script_lease("org/name", "free", "busy")  # the first probe is recovery's wait, the second the start's
        self.ops.recover_all()
        self.assertEqual(self.spawner.calls, [])
        self.assertEqual(self.lines()[-1], "resume repo=org/name run=r1 retried=2 action=run-refused status=409")
        self.assertTrue(self.marker.exists())

    def test_a_run_without_a_plan_cannot_be_started(self):
        (self.run_dir() / "plan.json").unlink()
        self.ops.recover_all()
        self.assertEqual(self.lines()[-1], "resume repo=org/name run=r1 retried=2 action=run-refused status=404")

    def test_job_ids_from_the_status_document_are_validated_before_they_reach_argv(self):
        self.runner.reports[("org/name", "r1")] = report(("--bad", "interrupted"), ("ok", "interrupted"))
        self.ops.recover_all()
        self.assertEqual(self.retries(), [["retry", "ok", "--reason=automatic resume after manager restart"]])

    def test_the_resumed_child_is_registered_like_one_started_over_http(self):
        self.ops.recover_all()
        [child] = self.manager.children.running()
        self.assertEqual((child.repo, child.run, child.kind), ("org/name", "r1", "run"))
        with self.assertRaises(ApiError) as caught:  # the repository now has a process: another run is refused
            self.manager.start_run(mgr.Target("org/name", "r1", None, None, self.run_dir()))
        self.assertEqual(caught.exception.status, 409)

    def test_shutdown_during_the_retries_stops_before_the_run_is_started(self):
        def stop_after_first(repo, run, tail):
            self.manager.closing.set()
            return text("ok\n")

        self.runner.override[("retry",)] = stop_after_first
        self.ops.recover_all()
        self.assertEqual((len(self.retries()), self.spawner.calls), (1, []))


# --- cost guardrail ------------------------------------------------------------------


class GuardrailTests(OpsCase):
    def setUp(self):
        super().setUp()
        self.policies["org/name"] = Policy(run_budget_usd=25.0)
        self.make_run()
        self.child = self.add_child(started="2026-01-01T00:00:00Z")
        self.marker = self.run_dir() / "budget-drained"

    def cost(self, value, repo="org/name", run="r1"):
        self.runner.reports[(repo, run)] = report(("a", "running"), cost=value)

    def test_under_the_budget_nothing_happens(self):
        self.cost(24.99)
        self.ops.check_budgets()
        self.ops.check_budgets()
        self.assertEqual(self.names("drain"), [])
        self.assertEqual(self.names("status"), [("org/name", "r1")] * 2)
        self.assertEqual((self.lines(), self.marker.exists()), ([], False))

    def test_at_the_budget_exactly_one_drain_even_across_two_ticks(self):
        self.cost(25.0)
        self.ops.check_budgets()
        self.ops.check_budgets()
        self.assertEqual(self.names("drain"), [("org/name", "r1")])
        self.assertEqual(self.names("status"), [("org/name", "r1")])  # the second tick does not even ask
        self.assertEqual(self.runner.argvs[-1], ["fake-cli", "--repo", "org/name", "--run-dir", str(self.run_dir()), "drain"])
        self.assertEqual(self.lines(), ["budget repo=org/name run=r1 cost=25.00 limit=25.00 action=drain"])
        self.assertRegex(self.marker.read_text(), f"^{TIMESTAMP.pattern}$")

    def test_the_lines_carry_two_decimals(self):
        self.policies["org/name"] = Policy(run_budget_usd=0.5)
        self.cost(30.456)
        self.ops.check_budgets()
        self.assertEqual(self.lines(), ["budget repo=org/name run=r1 cost=30.46 limit=0.50 action=drain"])

    def test_a_restarted_manager_does_not_drain_the_same_process_again(self):
        self.cost(30.0)
        self.ops.check_budgets()
        self.assertEqual(len(self.names("drain")), 1)
        second = self.new_manager()
        second.children.register(Child("run", "org/name", "r1", FakeProcess(7100), "2026-01-01T00:00:00Z"))
        self.runner.calls.clear()
        again = self.new_ops(second)
        again.check_budgets()
        again.check_budgets()
        self.assertEqual(self.runner.calls, [])  # the marker answered, no status call either

    def test_a_run_the_operator_started_after_the_drain_is_guarded_afresh(self):
        self.cost(30.0)
        self.ops.check_budgets()
        second = self.new_manager()
        second.children.register(Child("run", "org/name", "r1", FakeProcess(7100), "2999-01-01T00:00:00Z"))
        self.new_ops(second).check_budgets()
        self.assertEqual(len(self.names("drain")), 2)

    def test_a_failed_drain_is_not_remembered_and_the_next_tick_tries_again(self):
        self.cost(40.0)
        self.runner.override[("drain",)] = failure(1)
        self.ops.check_budgets()
        self.assertEqual(self.lines(), ["budget repo=org/name run=r1 cost=40.00 limit=25.00 action=drain-failed rc=1"])
        self.assertFalse(self.marker.exists())
        del self.runner.override[("drain",)]
        self.ops.check_budgets()
        self.assertEqual(len(self.names("drain")), 2)
        self.assertTrue(self.lines()[-1].endswith("action=drain"))
        self.ops.check_budgets()
        self.assertEqual(len(self.names("drain")), 2)

    def test_a_marker_that_cannot_be_written_still_stops_repeats_in_this_process(self):
        self.cost(40.0)
        with unittest.mock.patch.object(ops, "write_marker", side_effect=OSError(30, CANARY)):
            self.ops.check_budgets()
            self.ops.check_budgets()
        self.assertEqual(len(self.names("drain")), 1)
        self.assertEqual(self.warnings(), ["factory-manager: cannot write the budget marker (OSError)"])
        self.assertTrue(self.lines()[-1].endswith("action=drain"))

    def test_without_a_limit_the_repository_is_not_even_asked(self):
        self.policies.clear()
        self.cost(1e6)
        self.ops.check_budgets()
        self.assertEqual(self.runner.calls, [])

    def test_the_limit_is_read_again_on_every_tick(self):
        self.cost(30.0)
        self.policies["org/name"] = Policy(run_budget_usd=100.0)
        self.ops.check_budgets()
        self.assertEqual(self.names("drain"), [])
        self.policies["org/name"] = Policy(run_budget_usd=10.0)
        self.ops.check_budgets()
        self.assertEqual(len(self.names("drain")), 1)

    def test_only_running_children_of_kind_run_are_watched(self):
        self.manager.children.register(Child("plan", "org/other", "p1", FakeProcess(7200), timestamp()))
        self.policies["org/other"] = Policy(run_budget_usd=1.0)
        self.child.process.returncode = 0  # the run exited
        self.cost(99.0)
        self.ops.check_budgets()
        self.assertEqual(self.runner.calls, [])

    def test_each_watched_run_has_its_own_budget_and_marker(self):
        self.add_child("org/other", "r9")
        self.make_run("r9", "org/other")
        self.policies["org/other"] = Policy(run_budget_usd=5.0)
        self.cost(30.0)
        self.runner.reports[("org/other", "r9")] = report(("a", "running"), cost=4.0)
        self.ops.check_budgets()
        self.assertEqual(self.names("drain"), [("org/name", "r1")])
        self.assertFalse((self.run_dir("r9", "org/other") / "budget-drained").exists())

    def test_a_status_that_gives_no_usable_cost_never_drains(self):
        for answer in (
            failure(1),
            text("not json"),
            text("[]"),
            text("{}"),
            text('{"cost_usd": "99"}'),
            text('{"cost_usd": NaN}'),
            CommandResult(None, timed_out=True),
        ):
            with self.subTest(answer.stdout[:12]):
                self.runner.override[("status",)] = answer
                self.ops.check_budgets()
        self.assertEqual(self.names("drain"), [])

    def test_status_uses_the_status_lane_and_drain_the_other_lane(self):
        self.manager = self.new_manager(max_status_commands=1, max_other_commands=1)
        self.ops = self.new_ops(self.manager)
        self.add_child(started="2026-01-01T00:00:00Z")
        self.cost(30.0)
        with unittest.mock.patch.object(mgr, "SLOT_WAIT_SECONDS", 0.01):
            self.manager._lanes["status"].acquire()  # polling is stuck behind its own lane ...
            self.ops.check_budgets()
            self.assertEqual(self.runner.calls, [])
            self.manager._lanes["status"].release()
            self.manager._lanes["other"].acquire()  # ... and an operator-class lane that is full blocks only the drain
            self.ops.check_budgets()
            self.assertEqual(self.names("status"), [("org/name", "r1")])
            self.assertEqual(self.names("drain"), [])
            self.manager._lanes["other"].release()
            self.ops.check_budgets()
        self.assertEqual(self.names("drain"), [("org/name", "r1")])

    def test_the_slot_is_released_after_every_command(self):
        self.manager = self.new_manager(max_status_commands=1, max_other_commands=1)
        self.ops = self.new_ops(self.manager)
        self.add_child(started="2026-01-01T00:00:00Z")
        self.cost(30.0)
        self.runner.override[("status",)] = ValueError(CANARY)
        self.ops.check_budgets()
        self.assertEqual(self.warnings(), ["factory-manager: operations guardrail failed (ValueError)"])
        self.assertTrue(self.manager._lanes["status"].acquire(blocking=False))

    def test_shutdown_stops_the_pass_between_runs(self):
        self.add_child("org/other", "r9")
        self.policies["org/other"] = Policy(run_budget_usd=1.0)
        self.runner.override[("status",)] = lambda repo, run, tail: (
            self.manager.closing.set() or text(json.dumps(report(cost=0.0)))
        )
        self.ops.check_budgets()
        self.assertEqual(len(self.names("status")), 1)


# --- auto-purge ----------------------------------------------------------------------


class AutoPurgeTests(OpsCase):
    def verdict(self, *jobs, repo="org/name", run="r1", **extra):
        self.runner.reports[(repo, run)] = report(*jobs, **extra)

    def test_a_terminal_run_is_purged_and_the_clone_goes_with_the_last_run(self):
        self.make_run()
        self.verdict(("a", "merged"), ("b", "closed"))
        self.manager.children._finished[("org/name", "r1")] = {"returncode": 0}
        self.ops.sweep()
        self.assertEqual([c for _, _, c in self.runner.seen], ["status", "purge", "purge-clone"])
        self.assertEqual(self.runner.argvs[-1], ["fake-cli", "--repo", "org/name", "purge-clone"])  # no --run-dir
        self.assertEqual(
            self.lines(),
            ["purge repo=org/name run=r1 reason=terminal", "purge-clone repo=org/name removed=yes"],
        )
        self.assertFalse(self.run_dir().exists())
        self.assertEqual(self.manager.children.last_exit("org/name", "r1"), [])  # forgotten

    def test_the_purge_carries_what_the_janitor_saw_so_a_revived_run_is_refused_under_the_lease(self):
        self.make_run(age=10)
        self.verdict(("a", "merged"))
        seen = os.stat(self.run_dir() / "state.json").st_mtime_ns
        self.ops.sweep()
        purge = [argv for argv in self.runner.argvs if "purge" in argv][0]
        self.assertEqual(purge[purge.index("purge"):], ["purge", f"--unchanged-since={seen}"])

    def test_a_purge_the_cli_refuses_is_one_skipped_line_and_leaves_the_run(self):
        self.make_run(age=10)
        self.verdict(("a", "merged"))
        self.runner.override[("purge",)] = CommandResult(1, b"", b"the run was used after it was judged finished\n")
        self.ops.sweep()
        self.assertEqual(self.lines(), ["purge-skipped repo=org/name run=r1 reason=terminal rc=1 cause=other"])
        self.assertTrue(self.run_dir().exists())
        self.assertEqual(self.names("purge-clone"), [])  # a run remains

    def test_a_purge_refused_for_unchanged_since_says_so_with_a_fixed_cause(self):
        self.make_run(age=10)
        self.verdict(("a", "merged"))
        stderr = f"factory: the run was used after it was judged finished; not purged\n{CANARY}\n".encode()
        self.runner.override[("purge",)] = CommandResult(1, f"out {CANARY}".encode(), stderr)
        self.ops.sweep()
        self.assertEqual(
            self.lines(), ["purge-skipped repo=org/name run=r1 reason=terminal rc=1 cause=unchanged_since"]
        )
        self.assertNotIn(CANARY, "\n".join(self.lines() + self.warnings()))
        self.assertTrue(self.run_dir().exists())

    @staticmethod
    def real_git_error(error):
        """The message the real ``purge._git_in`` raises when ``gitsafe.repo_git`` fails with ``error``."""
        with unittest.mock.patch.object(purge.gitsafe, "repo_git", side_effect=error):
            try:
                purge._git_in(Path("/nonexistent"))("worktree", "list", "--porcelain")
            except RuntimeError as raised:
                return str(raised)
        raise AssertionError("_git_in did not raise")

    def real_state_unreadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "state.json").write_text("{not json")
            try:
                purge._children(Path(tmp))
            except purge.PurgeRefused as raised:
                return str(raised)
        raise AssertionError("_children did not refuse")

    @staticmethod
    def pod_record():
        session = "ab" * 16
        return podhandle.PodHandle(
            name=f"factory-w-{session}", uid="11111111-1111-4111-8111-111111111111", session=session,
            launcher="launcher", created_at="2026-01-01T00:00:00Z",
        ).record()

    def real_refusal_messages(self):
        """cause -> messages taken from the real source: constants, templates of purge.py and raised exceptions."""
        called = subprocess.CalledProcessError(128, ["git"], stderr="fatal: not a git repository")
        return {
            "alive": [
                purge.MSG_CHILD_ALIVE.format(key="job-1", pid=4242),
                purge.MSG_SUPERVISOR_LIVE.format(pid=77, status="running"),
                purge.MSG_SUPERVISOR_LIVE.format(pid=77, status="draining"),
            ],
            "pod_live": [
                str(podhandle.still_live("job-1", self.pod_record(), status, action))
                for status in (podhandle.ALIVE, podhandle.UNKNOWN)
                for action in ("resuming", "purging", "recovering")
            ]
            + [
                str(podhandle.still_live("job-1", {"name": "bad"}, podhandle.UNKNOWN, "resuming")),
                recovery.MSG_POD_LIVE.format(key="job-1"),
            ],
            "foreign_pid": [purge.MSG_FOREIGN_PID.format(key="job-1", pid=4242)],
            "branch_checked_out": [
                purge.MSG_CHECKED_OUT.format(name="factory/r1/job-1", where="the main checkout"),
                purge.MSG_CHECKED_OUT.format(name="factory/r1/job-1", where="/data/elsewhere"),
            ],
            "other_repo": [
                purge.MSG_OTHER_REPO,
                purge.MSG_WORKTREE_OTHER_REPO.format(name="job-1"),
            ],
            "state_unreadable": [purge.MSG_STATE_UNREADABLE, self.real_state_unreadable()],
            "lease_busy": [locks.BUSY_MESSAGE, str(LockBusy(locks.BUSY_MESSAGE))],
            "git_failed": [
                self.real_git_error(called),
                self.real_git_error(subprocess.CalledProcessError(128, ["git"], stderr="")),
                self.real_git_error(FileNotFoundError()),
                self.real_git_error(subprocess.TimeoutExpired(["git"], 1)),
            ],
            "unchanged_since": [purge.MSG_UNCHANGED_SINCE],
            "remove_failed": [purge.MSG_REMOVE_FAILED.format(reason="Permission denied"), purge.MSG_REMOVE_FAILED.format(reason=os.strerror(13))],
        }

    def test_each_known_refusal_of_purge_maps_to_its_fixed_cause(self):
        table = self.real_refusal_messages()
        self.assertEqual({cause for cause, _ in ops.CAUSES}, set(table))  # every code has a real message
        for cause, messages in table.items():
            for message in messages:
                with self.subTest(message=message):
                    line = "factory: " + message  # as cli.main prints a refusal
                    self.assertEqual(ops.refusal_cause(f"{line}\n".encode()), cause)
                    self.assertEqual(ops.refusal_cause(f"noise\n{line}\nmore\n".encode()), cause)

    def test_a_config_warning_before_the_refusal_does_not_hide_its_cause(self):
        warning = f"factory: /etc/factory/repos/org__name.toml: auto_build_timeout_seconds must be an integer {CANARY}\n"
        refusal = "factory: the run was used after it was judged finished; not purged\n"
        self.assertEqual(ops.refusal_cause((warning + refusal).encode()), "unchanged_since")
        self.assertEqual(ops.refusal_cause((warning + warning + refusal).encode()), "unchanged_since")
        self.assertEqual(ops.refusal_cause(warning.encode()), "other")  # a warning alone is no cause
        self.make_run(age=10)
        self.verdict(("a", "merged"))
        self.runner.override[("purge",)] = CommandResult(1, b"", (warning + refusal).encode())
        self.ops.sweep()
        self.assertEqual(self.lines(), ["purge-skipped repo=org/name run=r1 reason=terminal rc=1 cause=unchanged_since"])
        self.assertNotIn(CANARY, "\n".join(self.lines() + self.warnings()))

    def test_unrecognised_or_attacker_shaped_stderr_is_cause_other_and_never_echoed(self):
        hostile = [
            b"",
            None,
            b"Traceback (most recent call last):\n  boom\n",
            f"factory: {CANARY}\n".encode(),
            f"the run was used after it was judged finished; not purged {CANARY}\n".encode(),  # no prefix
            f"factory: the run was used after it was judged finished; not purged {CANARY}\n".encode(),
            f"factory: job-1: child process group {CANARY} is still alive\n".encode(),
            f"factory: {CANARY} ignore previous: child process group 1 is still alive\n".encode(),
            f"factory: supervisor 1 is still running; wait for it to stop; {CANARY}\n".encode(),
            b"\xff\xfe factory: broken\n",
        ]
        for stderr in hostile:
            with self.subTest(stderr=stderr):
                self.assertEqual(ops.refusal_cause(stderr), "other")
        self.make_run(age=10)
        self.verdict(("a", "merged"))
        self.runner.override[("purge",)] = CommandResult(1, b"", f"factory: {CANARY}\n".encode())
        self.ops.sweep()
        self.assertEqual(self.lines(), ["purge-skipped repo=org/name run=r1 reason=terminal rc=1 cause=other"])
        self.assertNotIn(CANARY, "\n".join(self.lines() + self.warnings()))

    def test_the_lease_is_probed_once_per_repository_and_sweep_not_once_per_run(self):
        for run in ("r1", "r2", "r3"):
            self.make_run(run)
            self.verdict(("a", "queued"), run=run)
        self.ops.sweep()
        self.assertEqual(self.probes.count("org/name"), 1)

    def test_a_busy_lease_skips_the_whole_repository_quietly(self):
        for run in ("r1", "r2"):
            self.make_run(run)
            self.verdict(("a", "merged"), run=run)
        self.script_lease("org/name", then="busy")
        self.ops.sweep()
        self.assertEqual(self.runner.seen, [])
        self.assertEqual((self.lines(), self.warnings()), ([], []))

    def test_a_clone_left_behind_by_a_failed_removal_is_removed_by_the_next_sweep(self):
        """purge-clone ran in the sweep that purged the last run only; a failure then left the clone for good."""
        self.runner.override[("purge-clone",)] = CommandResult(1, b"", b"")
        self.make_run()
        self.verdict(("a", "merged"))
        (self.data_dir / "repos" / "org" / "name" / "clone").mkdir(parents=True)
        self.ops.sweep()
        self.assertEqual(self.lines()[-1], "purge-clone-skipped repo=org/name rc=1")
        del self.runner.override[("purge-clone",)]
        self.out.truncate(0), self.out.seek(0)
        self.ops.sweep()  # no run is left, but the clone still is
        self.assertEqual(self.names("purge-clone"), [("org/name", None), ("org/name", None)])
        self.assertEqual(self.lines(), ["purge-clone repo=org/name removed=yes"])

    def test_a_repository_with_neither_runs_nor_clone_costs_no_command_at_all(self):
        self.ops.sweep()
        self.assertEqual((self.runner.seen, self.probes), ([], []))

    def test_the_clone_stays_while_another_run_remains(self):
        self.make_run("r1")
        self.make_run("r2")
        self.verdict(("a", "merged"), run="r1")
        self.verdict(("a", "queued"), run="r2")
        self.ops.sweep()
        self.assertEqual(self.names("purge"), [("org/name", "r1")])
        self.assertEqual(self.names("purge-clone"), [])
        self.assertEqual(self.lines(), ["purge repo=org/name run=r1 reason=terminal"])

    def test_the_clone_goes_when_the_last_of_several_runs_is_purged_in_one_sweep(self):
        self.make_run("r1")
        self.make_run("r2")
        for run in ("r1", "r2"):
            self.verdict(("a", "pr_ready", "merged"), run=run)
        self.ops.sweep()
        self.assertEqual(self.names("purge"), [("org/name", "r1"), ("org/name", "r2")])
        self.assertEqual(self.names("purge-clone"), [("org/name", None)])

    def test_a_repository_that_purged_nothing_is_never_asked_to_remove_its_clone(self):
        self.make_run("r1")
        self.verdict(("a", "queued"))
        self.ops.sweep()
        self.assertEqual(self.names("purge-clone"), [])
        self.assertEqual(self.lines(), [])

    def test_terminal_means_every_job_merged_or_closed(self):
        self.make_run("r1")
        self.make_run("r2")
        self.make_run("r3")
        self.verdict(("a", "merged"), ("b", "pr_ready", "ci_failed"), run="r1")
        self.verdict(("a", "merged"), ("b", "failed"), run="r2")
        self.verdict(run="r3")  # no jobs at all
        self.ops.sweep()
        self.assertEqual(self.names("purge"), [])

    def test_a_run_is_abandoned_only_after_abandon_after(self):
        self.policies["org/name"] = Policy(abandon_after=3600)
        self.make_run(age=3599)
        self.verdict(("a", "queued"))
        self.ops.sweep()
        self.assertEqual(self.names("purge"), [])
        self.time.now += 2  # now 3601 s idle
        self.ops.sweep()
        self.assertEqual(self.names("purge"), [("org/name", "r1")])
        self.assertEqual(self.lines()[0], "purge repo=org/name run=r1 reason=abandoned")

    def test_exactly_abandon_after_is_not_yet_abandoned(self):
        self.policies["org/name"] = Policy(abandon_after=3600)
        self.make_run(age=3600)
        self.verdict(("a", "queued"))
        self.ops.sweep()
        self.assertEqual(self.names("purge"), [])

    def test_the_default_is_seven_days_and_each_repository_has_its_own(self):
        self.policies["org/other"] = Policy(abandon_after=3600)
        for repo in ("org/name", "org/other"):
            self.make_run("r1", repo, age=2 * DAY)
            self.verdict(("a", "queued"), repo=repo)
        self.ops.sweep()
        self.assertEqual(self.names("purge"), [("org/other", "r1")])
        self.make_run("r2", "org/name", age=7 * DAY + 5)
        self.verdict(("a", "queued"), repo="org/name", run="r2")
        self.ops.sweep()
        self.assertEqual(self.names("purge"), [("org/other", "r1"), ("org/name", "r2")])

    def test_the_newest_of_state_plan_and_supervisor_control_decides(self):
        self.policies["org/name"] = Policy(abandon_after=3600)
        self.make_run(age=10 * 3600, supervisor_age=60)  # a drain was requested a minute ago
        self.verdict(("a", "queued"))
        self.ops.sweep()
        self.assertEqual(self.names("purge"), [])
        os.utime(self.run_dir() / "supervisor-control.json", (NOW - 7200, NOW - 7200))
        self.ops.sweep()
        self.assertEqual(self.names("purge"), [("org/name", "r1")])

    def test_a_live_worker_or_supervisor_protects_an_old_run(self):
        self.policies["org/name"] = Policy(abandon_after=3600)
        cases = {
            "worker": {"workers": 2},
            "running": {"supervisor": {"status": "running", "pid": 1}},
            "draining": {"supervisor": {"status": "draining", "pid": 1}},
        }
        cases["stale"] = {"supervisor": {"status": "stale", "pid": 1}}  # its owner is gone
        for label, extra in cases.items():
            self.make_run(label, age=DAY)
            self.verdict(("a", "queued"), run=label, **extra)
        self.ops.sweep()
        self.assertEqual(self.names("purge"), [("org/name", "stale")])

    def test_a_status_document_that_cannot_be_read_protects_the_run(self):
        self.policies["org/name"] = Policy(abandon_after=3600)
        self.make_run(age=DAY)
        for answer in (failure(1), text("Planned 1 issues in 1 jobs\n"), text("{")):
            with self.subTest(answer.stdout[:10]):
                self.runner.override[("status",)] = answer
                self.ops.sweep()
        self.assertEqual(self.names("purge"), [])

    def test_a_run_holding_finished_work_for_a_person_is_never_called_abandoned(self):
        # The commit of a needs_human_publish job exists only in its run: idle is not abandoned.
        self.policies["org/name"] = Policy(abandon_after=3600)
        self.make_run("parked", age=30 * DAY)
        self.verdict(("a", "needs_human_publish"), ("b", "merged"), run="parked")
        self.make_run("idle", age=30 * DAY)
        self.verdict(("a", "queued"), run="idle")
        self.ops.sweep()
        self.ops.sweep()
        self.assertEqual(self.names("purge"), [("org/name", "idle")])
        self.assertTrue(self.run_dir("parked").exists())
        kept = [line for line in self.warnings() if "purge kept" in line]
        self.assertEqual(kept, ["factory-manager: operations: purge kept repo=org/name run=parked reason=awaiting_person"], "one warning")
        # once the person published it (or it was retried), the ordinary rules apply again
        self.verdict(("a", "pr_ready"), ("b", "merged"), run="parked")
        self.ops.sweep()
        self.assertEqual(self.names("purge"), [("org/name", "idle"), ("org/name", "parked")])
        self.assertTrue(ops.awaits_person({"jobs": [{"status": "needs_human_publish"}]}))
        for document in ({}, {"jobs": None}, {"jobs": ["x"]}, {"jobs": [{"status": "failed"}]}):
            self.assertFalse(ops.awaits_person(document), document)

    def test_a_planned_run_that_never_started_is_judged_by_its_files_alone(self):
        self.policies["org/name"] = Policy(abandon_after=3600)
        self.make_run("old", state=False, age=DAY)
        self.make_run("new", state=False, age=60)
        self.ops.sweep()
        self.assertEqual(self.names("purge"), [("org/name", "old")])
        self.assertEqual(self.names("status"), [])  # a run without state has no status document to ask for

    def test_a_directory_with_no_state_files_falls_back_to_its_own_age(self):
        self.policies["org/name"] = Policy(abandon_after=3600)
        self.make_run("failed-plan", plan=False, state=False, age=DAY)
        self.make_run("fresh", plan=False, state=False, age=5)
        self.ops.sweep()
        self.assertEqual(self.names("purge"), [("org/name", "failed-plan")])

    def test_a_busy_lease_skips_the_run_without_asking_for_its_status(self):
        self.make_run(age=30 * DAY)
        self.verdict(("a", "merged"))
        self.script_lease("org/name", then="busy")
        self.ops.sweep()
        self.assertEqual((self.runner.calls, self.lines(), self.err.getvalue()), ([], [], ""))

    def test_an_unavailable_lease_backend_skips_with_one_warning(self):
        self.make_run("r1", age=30 * DAY)
        self.make_run("r2", age=30 * DAY)
        self.script_lease("org/name", then="unavailable")
        self.ops.sweep()
        self.ops.sweep()
        self.assertEqual(self.warnings(), ["factory-manager: operations: lease backend unavailable for org/name"])
        self.assertEqual(self.runner.calls, [])

    def test_a_repository_with_a_process_of_this_manager_is_left_alone(self):
        self.make_run("r1")
        self.make_run("r2")
        self.verdict(("a", "merged"), run="r1")
        self.add_child("org/name", "r2")
        self.ops.sweep()
        self.assertEqual((self.runner.calls, self.lines()), ([], []))

    def test_a_refused_purge_is_one_skipped_line_and_nothing_else_happens(self):
        self.make_run()
        self.verdict(("a", "merged"))
        self.runner.override[("purge",)] = failure(1)
        self.manager.children._finished[("org/name", "r1")] = {"returncode": 0}
        self.ops.sweep()
        self.assertEqual(self.lines(), ["purge-skipped repo=org/name run=r1 reason=terminal rc=1 cause=other"])
        self.assertEqual(self.names("purge-clone"), [])
        self.assertTrue(self.run_dir().exists())
        self.assertEqual(len(self.manager.children.last_exit("org/name", "r1")), 1)  # not forgotten
        self.assertEqual(self.err.getvalue(), "")

    def test_a_timed_out_purge_is_skipped_with_rc_timeout(self):
        self.make_run()
        self.verdict(("a", "merged"))
        self.runner.override[("purge",)] = CommandResult(None, timed_out=True)
        self.ops.sweep()
        self.assertEqual(self.lines(), ["purge-skipped repo=org/name run=r1 reason=terminal rc=timeout cause=other"])

    def test_a_clone_that_cannot_be_removed_is_one_skipped_line(self):
        self.make_run()
        self.verdict(("a", "merged"))
        self.runner.override[("purge-clone",)] = failure(1)
        self.ops.sweep()
        self.assertEqual(self.lines()[-1], "purge-clone-skipped repo=org/name rc=1")

    def test_nothing_to_remove_is_reported_as_such(self):
        self.make_run()
        self.verdict(("a", "merged"))
        self.runner.override[("purge-clone",)] = text("Nothing to remove\n")
        self.ops.sweep()
        self.assertEqual(self.lines()[-1], "purge-clone repo=org/name removed=no")

    def test_an_exception_in_one_repository_does_not_stop_the_sweep(self):
        self.make_run("r1", "org/name")
        self.make_run("r1", "org/other")
        self.verdict(("a", "merged"), repo="org/other")
        self.runner.override[("status", "org/name")] = RuntimeError(CANARY)
        self.ops.sweep()
        self.assertEqual(self.warnings(), ["factory-manager: operations janitor failed (RuntimeError)"])
        self.assertEqual(self.names("purge"), [("org/other", "r1")])

    def test_a_repository_whose_config_does_not_load_is_skipped(self):
        (self.config_dir / "repos" / "org__name.toml").write_text("repo = 5")
        self.make_run("r1", "org/other")
        self.verdict(("a", "merged"), repo="org/other")
        self.ops.sweep()
        self.assertEqual(self.names("purge"), [("org/other", "r1")])
        self.assertEqual(len(self.warnings()), 1)

    def test_shutdown_stops_the_sweep_between_runs(self):
        for run in ("r1", "r2", "r3"):
            self.make_run(run)
            self.verdict(("a", "merged"), run=run)
        self.runner.override[("purge", "org/name", "r1")] = lambda repo, run, tail: (
            self.manager.closing.set() or text("Purged\n")
        )
        self.ops.sweep()
        self.assertEqual(self.names("purge"), [("org/name", "r1")])
        self.assertEqual(self.names("purge-clone"), [])

    def test_status_goes_through_the_status_lane_and_the_other_commands_follow(self):
        self.manager = self.new_manager(max_status_commands=1, max_other_commands=1)
        self.ops = self.new_ops(self.manager)
        self.make_run()
        self.verdict(("a", "merged"))
        with unittest.mock.patch.object(mgr, "SLOT_WAIT_SECONDS", 0.01):
            self.manager._lanes["status"].acquire()
            self.ops.sweep()
            self.assertEqual(self.runner.calls, [])
            self.manager._lanes["status"].release()
            self.ops.sweep()
        self.assertEqual([c for _, _, c in self.runner.seen], ["status", "purge", "purge-clone"])


# --- the history ledger ----------------------------------------------------------------


PR_URL = "https://github.com/org/name/pull/7"


class HistoryFeedTests(OpsCase):
    """The guardrail's and the janitor's status reads feed the ledger; a purge leaves its account behind."""

    def setUp(self):
        super().setUp()
        self.ledger = self.manager.ledger

    def verdict(self, *jobs, repo="org/name", run="r1", live_ok=True, failed=(), **extra):
        document = report(*jobs, **extra)
        for row in document["jobs"]:
            row["pr"] = PR_URL
        # What status_report builds: ``ok`` is false for the whole read as soon as one lookup failed, and
        # ``failed`` names the jobs whose lookup it was.
        document["live_refresh"] = {
            "ok": live_ok and not failed,
            "failed": [{"job": job, "error": "TimeoutExpired", "reason": "timeout"} for job in failed],
        }
        document["observed_at"] = "2026-01-02T03:04:05.000000Z"
        self.runner.reports[(repo, run)] = document

    def events(self, kind=None):
        return [event for event in self.ledger.read().events if kind in (None, event["kind"])]

    def test_a_purge_records_why_and_what_github_said_about_each_pull_request(self):
        self.make_run()
        self.verdict(("a", "pr_ready", "merged"), ("b", "closed"))
        self.ops.sweep()
        purged = self.events("purged")
        self.assertEqual(len(purged), 1)
        self.assertEqual((purged[0]["repo"], purged[0]["run"], purged[0]["reason"]), ("org/name", "r1", "terminal"))
        self.assertEqual(
            purged[0]["jobs"],
            [
                {"id": "a", "status": "pr_ready", "handoff": "merged", "pr": PR_URL, "fresh": True},
                {"id": "b", "status": "closed", "pr": PR_URL, "fresh": True},
            ],
        )
        self.assertFalse(self.run_dir().exists())  # the run is gone, the history is not
        self.assertEqual(self.names("purge"), [("org/name", "r1")])

    def test_an_abandoned_run_is_recorded_as_abandoned(self):
        self.policies["org/name"] = Policy(abandon_after=3600)
        self.make_run(age=4000)
        self.verdict(("a", "queued"))
        self.ops.sweep()
        self.assertEqual([event["reason"] for event in self.events("purged")], ["abandoned"])

    def test_a_purge_the_cli_refused_records_nothing(self):
        self.make_run(age=10)
        self.verdict(("a", "merged"))
        self.runner.override[("purge",)] = CommandResult(1, b"", b"refused\n")
        self.ops.sweep()
        self.assertEqual(self.events("purged"), [])
        self.assertTrue(self.run_dir().exists())

    def test_a_run_that_stays_still_has_its_pull_request_state_recorded_once_per_change(self):
        self.make_run()
        self.verdict(("a", "pr_ready", "published"))
        self.ops.sweep()
        self.ops.sweep()  # nothing changed: nothing new
        observed = self.events("pr_observed")
        self.assertEqual(len(observed), 1)
        self.assertEqual(
            (observed[0]["job"], observed[0]["pr"], observed[0]["handoff_status"], observed[0]["observed_at"], observed[0]["fresh"]),
            ("a", PR_URL, "published", "2026-01-02T03:04:05.000000Z", True),
        )
        self.verdict(("a", "pr_ready", "ci_failed"))
        self.ops.sweep()
        self.assertEqual([event["handoff_status"] for event in self.events("pr_observed")], ["published", "ci_failed"])

    def test_a_lookup_that_failed_is_recorded_as_not_fresh(self):
        self.make_run()
        self.verdict(("a", "pr_ready", "published"), live_ok=False)
        self.ops.sweep()
        self.assertFalse(self.events("pr_observed")[0]["fresh"])

    def test_a_failed_lookup_after_a_confirmed_state_does_not_hide_it_in_what_the_ledger_shows(self):
        self.make_run()
        self.verdict(("a", "pr_ready", "ci_failed"))
        self.ops.sweep()
        self.verdict(("a", "pr_ready", "published"), live_ok=False)  # the lookup failed: state.json says published
        self.ops.sweep()
        self.assertEqual([event["fresh"] for event in self.events("pr_observed")], [True, False])
        job = history.fold(self.events())[("org/name", "r1")]["jobs"]["a"]
        self.assertEqual((job["handoff_status"], job["observed_fresh"]), ("ci_failed", True))

    def test_one_failed_lookup_does_not_turn_the_words_of_the_other_jobs_into_guesses(self):
        self.make_run()
        # Read 1: job a's lookup succeeded (merged), job b's failed (state.json says published).
        self.verdict(("a", "pr_ready", "merged"), ("b", "pr_ready", "published"), failed=("b",))
        self.ops.sweep()
        observed = {event["job"]: (event["handoff_status"], event["fresh"]) for event in self.events("pr_observed")}
        self.assertEqual(observed, {"a": ("merged", True), "b": ("published", False)})
        # Read 2: GitHub is flaky and both lookups fail; state.json says published for both.
        self.verdict(("a", "pr_ready", "published"), ("b", "pr_ready", "published"), failed=("a", "b"))
        self.ops.sweep()
        jobs = history.fold(self.events())[("org/name", "r1")]["jobs"]
        self.assertEqual((jobs["a"]["handoff_status"], jobs["a"]["observed_fresh"]), ("merged", True))
        self.assertEqual((jobs["b"]["handoff_status"], jobs["b"]["observed_fresh"]), ("published", False))

    def test_a_status_read_that_is_not_ok_and_names_no_failed_job_confirms_nothing(self):
        self.make_run()
        self.verdict(("a", "pr_ready", "ci_failed"))
        self.ops.sweep()
        self.verdict(("a", "pr_ready", "published"), live_ok=False)  # not ok, and no job is named
        self.ops.sweep()
        self.assertEqual([event["fresh"] for event in self.events("pr_observed")], [True, False])
        job = history.fold(self.events())[("org/name", "r1")]["jobs"]["a"]
        self.assertEqual((job["handoff_status"], job["observed_fresh"]), ("ci_failed", True))

    def test_a_purge_records_freshness_per_job_when_only_some_lookups_failed(self):
        self.policies["org/name"] = Policy(abandon_after=3600)
        self.make_run(age=4000)
        self.verdict(("a", "pr_ready", "merged"), ("b", "pr_ready", "published"), failed=("b",))
        self.ops.sweep()
        purged = self.events("purged")
        self.assertEqual(len(purged), 1)
        self.assertEqual({entry["id"]: entry["fresh"] for entry in purged[0]["jobs"]}, {"a": True, "b": False})
        self.assertIs(purged[0]["fresh"], False)  # the event's own flag is the whole read's: one lookup failed
        jobs = history.fold(self.events())[("org/name", "r1")]["jobs"]
        self.assertEqual((jobs["a"]["handoff_status"], jobs["a"]["observed_fresh"]), ("merged", True))
        self.assertEqual((jobs["b"]["handoff_status"], jobs["b"]["observed_fresh"]), ("published", False))

    def test_a_purge_records_whether_the_status_read_it_carries_was_confirmed_by_github(self):
        for live_ok in (True, False):
            with self.subTest(live_ok=live_ok):
                self.make_run()
                self.verdict(("a", "pr_ready", "merged"), live_ok=live_ok)
                self.ops.sweep()
                self.assertIs(self.events("purged")[-1]["fresh"], live_ok)

    def test_the_guardrail_feeds_the_ledger_too(self):
        self.policies["org/name"] = Policy(run_budget_usd=25.0)
        self.make_run()
        self.add_child(started="2026-01-01T00:00:00Z")
        self.verdict(("a", "pr_ready", "published"), cost=1.0)
        self.ops.check_budgets()
        self.assertEqual([event["handoff_status"] for event in self.events("pr_observed")], ["published"])

    def test_a_job_without_a_pull_request_has_nothing_to_observe(self):
        self.make_run()
        self.runner.reports[("org/name", "r1")] = report(("a", "running"))
        self.ops.sweep()
        self.assertEqual(self.events("pr_observed"), [])

    def test_a_purge_forgets_the_throttle_so_a_new_run_of_the_same_name_is_observed_again(self):
        self.make_run()
        self.verdict(("a", "pr_ready", "published"))
        self.ops.sweep()
        self.verdict(("a", "pr_ready", "merged"))
        self.ops.sweep()  # terminal: purged
        self.assertEqual(self.ops._observed, {})

    def test_no_agent_authored_text_reaches_the_ledger(self):
        self.make_run()
        self.verdict(("a", "pr_ready", "merged"))
        self.ops.sweep()
        raw = b"".join(path.read_bytes() for path in (self.data_dir / "history").iterdir())
        self.assertIn(b"purged", raw)
        self.assertNotIn(CANARY.encode(), raw)

    def test_the_sweep_applies_the_ledger_bounds(self):
        with unittest.mock.patch.object(self.manager.ledger, "prune") as prune:
            self.ops.sweep()
        prune.assert_called_once_with()

    def test_a_ledger_that_cannot_be_written_never_stops_a_purge(self):
        (self.data_dir / "history").write_text("a file where the directory should be\n")
        self.make_run()
        self.verdict(("a", "merged"))
        self.ops.sweep()
        self.assertFalse(self.run_dir().exists())
        self.assertEqual(self.lines()[0], "purge repo=org/name run=r1 reason=terminal")


# --- threads and shutdown ------------------------------------------------------------


class LoopTests(OpsCase):
    def test_a_loop_waits_then_passes_until_the_wait_says_stop(self):
        ticks = []
        self.time.stop_after = 3
        self.ops._loop("guardrail", 60.0, lambda: ticks.append(self.time.now))
        self.assertEqual(self.time.waits, [60.0, 60.0, 60.0])
        self.assertEqual(len(ticks), 2)  # a pass only after a full interval, none after the stop

    def test_a_failing_pass_does_not_end_the_loop(self):
        self.time.stop_after = 3

        def tick():
            raise OSError(5, CANARY)

        self.ops._loop("janitor", 900.0, tick)
        self.assertEqual(self.warnings(), ["factory-manager: operations janitor failed (OSError)"] * 2)


class ThreadTests(OpsCase):
    """Real threads and real events; the intervals are far longer than any test.

    Whatever a test does, its cleanup sets the manager's shutdown event and joins the
    threads: a non-daemon thread left behind in a wait of an hour would keep the whole
    test process from exiting.
    """

    def setUp(self):
        super().setUp()
        # trigger_seconds=0: these tests are about the three original threads; the trigger thread
        # has its own tests (tests/test_trigger_loop.py).
        self.manager = self.new_manager(
            lease_wait_seconds=3600.0, poll_seconds=3600.0, janitor_seconds=86400.0, trigger_seconds=0.0
        )
        self.addCleanup(self.stop_threads)

    def stop_threads(self):
        self.manager.closing.set()
        if self.manager.operations is not None:
            self.assertEqual(self.manager.operations.join(30), 0)

    def attach(self, **kwargs):
        """An ``Operations`` on this test's manager, registered where shutdown looks for it."""
        kwargs.setdefault("policy", lambda cfg: Policy())
        operations = Operations(self.manager, **kwargs)
        self.manager.operations = operations
        return operations

    def test_building_a_manager_starts_no_thread(self):
        before = set(threading.enumerate())
        manager = self.new_manager()
        self.assertLessEqual(set(threading.enumerate()), before)  # at most threads of earlier tests ending
        self.assertIsNone(manager.operations)

    def test_the_switch_off_starts_nothing(self):
        before = set(threading.enumerate())
        manager = self.new_manager(operations=False)
        manager.start_operations()
        self.assertIsNone(manager.operations)
        self.assertLessEqual(set(threading.enumerate()), before)

    def test_start_runs_three_threads_that_are_not_daemons_and_shutdown_joins_them(self):
        self.make_run()
        self.script_lease("org/name", then="busy")  # recovery sits in its lease wait for an hour
        waiting, in_wait = threading.Event(), set()

        def wait(seconds):
            in_wait.add(threading.current_thread().name)
            if len(in_wait) == 2:
                waiting.set()
            return self.manager.closing.wait(seconds)

        operations = self.attach(wait=wait)
        operations.start()
        # Recovery sits in its lease wait and the guardrail in its first interval; the janitor
        # holds back until the restart pass is over, so it never reaches a wait of its own.
        self.assertTrue(waiting.wait(10))
        threads = operations.threads
        self.assertEqual(
            sorted(t.name for t in threads),
            ["factory-manager-guardrail", "factory-manager-janitor", "factory-manager-recovery"],
        )
        self.assertFalse(any(t.daemon for t in threads))
        started = time.monotonic()
        summary = self.manager.terminate_children(grace=0.1)
        self.assertLess(time.monotonic() - started, 4.0)  # no wait runs out its hour
        self.assertEqual(summary["killed"], 0)
        self.assertEqual([t for t in threads if t.is_alive()], [])
        self.assertEqual(self.warnings(), [])

    def test_start_operations_wires_the_real_class_and_is_idempotent(self):
        self.manager.start_operations()
        first = self.manager.operations
        self.assertIsInstance(first, Operations)
        self.manager.start_operations()
        self.assertIs(self.manager.operations, first)
        self.assertEqual(len(first.threads), 3)
        self.manager.terminate_children(grace=0.1)
        self.assertEqual([t for t in first.threads if t.is_alive()], [])
        self.manager.start_operations()  # nothing new starts once shutdown began
        self.assertIs(self.manager.operations, first)
        self.assertEqual(len(first.threads), 3)

    def test_a_thread_that_does_not_stop_is_reported_at_shutdown(self):
        release = threading.Event()
        operations = self.attach(wait=lambda seconds: release.wait(30))
        self.addCleanup(release.set)  # runs before stop_threads joins
        operations.start()
        operations.threads[0].join(10)  # recovery has nothing to do and ends; the two loops stay in their wait
        with unittest.mock.patch.object(mgr, "OPERATIONS_JOIN_SECONDS", 0.05):
            self.manager.terminate_children(grace=0.05)
        self.assertEqual(self.warnings(), ["factory-manager: 2 operations thread(s) did not stop in time"])

    def test_an_in_flight_command_does_not_hold_up_shutdown(self):
        self.make_run()
        started, finish = threading.Event(), threading.Event()
        self.addCleanup(finish.set)

        def slow_recover(repo, run, tail):
            started.set()
            finish.wait(30)
            return text("Recovered 0 interrupted job(s)\n")

        self.runner.override[("recover",)] = slow_recover
        operations = self.attach(wait=self.manager.closing.wait)
        operations.start()
        self.assertTrue(started.wait(10))
        self.runner.live = 1  # as the real runner would report the command in flight

        def cli_exits():  # the CLI leaves after the manager's SIGINT
            self.runner.live = 0
            finish.set()

        threading.Timer(0.2, cli_exits).start()
        began = time.monotonic()
        self.manager.terminate_children(grace=5)
        self.assertLess(time.monotonic() - began, 4.0)
        self.assertEqual(operations.join(0), 0)
        self.assertEqual((self.runner.interrupted, self.lines(), self.warnings()), (1, [], []))  # a command cut short logs nothing


# --- a real manager process ----------------------------------------------------------

FAKE_CLI = textwrap.dedent(
    r"""
    import json, os, sys

    args = sys.argv[1:]
    while args and args[0] in ("--repo", "--run-dir"):
        args = args[2:]
    command = args[0]
    with open(os.environ["OPS_LOG"], "a") as log:
        log.write(" ".join(args) + "\n")
    canary = os.environ["OPS_CANARY"]
    if command == "recover":
        print("Recovered 1 interrupted job(s)")
    elif command == "status":
        job = {"id": "j1", "status": "interrupted", "handoff_status": None, "error": canary, "next_action": canary}
        print(json.dumps({"jobs": [job], "cost_usd": 1.0, "active_worker_count": 0, "supervisor": None,
                          "blockers": {"b": {"cause": canary}}}))
    else:
        print(canary)
        print(canary, file=sys.stderr)
    """
)


class RealManagerTests(unittest.TestCase):
    """``factory-manager`` as a process: the loops start after the listening line and stop on SIGTERM."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        (self.root / "config" / "repos").mkdir(parents=True)
        (self.root / "config" / "repos" / "org__name.toml").write_text('repo = "org/name"\n')
        run = self.root / "data" / "repos" / "org" / "name" / "runs" / "r1"
        run.mkdir(parents=True)
        (run / "plan.json").write_text("{}")
        (run / "state.json").write_text("{}")
        (self.root / "fake_cli.py").write_text(FAKE_CLI)
        self.log = self.root / "calls.log"
        self.process = None
        self.lines, self.seen = queue.Queue(), []

    def start(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("FACTORY_", "FAKE_"))}
        env.update(
            PYTHONPATH=SRC,
            FACTORY_CONFIG=str(self.root / "config"),
            FACTORY_DATA=str(self.root / "data"),
            FACTORY_MANAGER_PORT="0",
            FACTORY_MANAGER_CLI=f"{sys.executable} {self.root / 'fake_cli.py'}",
            OPS_LOG=str(self.log),
            OPS_CANARY=CANARY,
            **extra,
        )
        self.process = subprocess.Popen(
            [sys.executable, "-m", "factory.manager"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self.cleanup_process)
        self.pump = threading.Thread(target=self.pump_stdout, daemon=True)
        self.pump.start()

    def pump_stdout(self):
        for line in self.process.stdout:
            self.lines.put(line.rstrip("\n"))

    def finish(self):
        """(stdout, stderr) after the process exited."""
        self.pump.join(10)
        rest = []
        while not self.lines.empty():
            rest.append(self.lines.get())
        return "\n".join(self.seen + rest), self.process.stderr.read()

    def cleanup_process(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()
        self.process.stdout.close()
        self.process.stderr.close()

    def read_line(self, timeout=30):
        try:
            line = self.lines.get(timeout=timeout)
        except queue.Empty:
            self.fail("the manager printed nothing")
        self.seen.append(line)
        return line

    def test_recovery_runs_after_the_listening_line_and_sigterm_stops_everything(self):
        self.start()
        self.assertRegex(self.read_line(), r"^factory-manager listening host=127\.0\.0\.1 port=\d+ repos=1$")
        self.assertEqual(
            self.read_line(),
            "restart repo=org/name run=r1 interrupted=1 queued=0 pr_ready=0 running=0 pr_incomplete=0 "
            "failed=0 blocked=0 needs_human_publish=0 stopped=0 merged=0 closed=0 not_started=0 other=0 recovered=1",
        )
        self.process.send_signal(signal.SIGTERM)
        started = time.monotonic()
        self.assertEqual(self.process.wait(timeout=30), 0)
        self.assertLess(time.monotonic() - started, 20)  # the hour-long waits did not hold it up
        stdout, stderr = self.finish()
        self.assertEqual(self.log.read_text().splitlines(), ["recover", "status --json"])  # no run, no retry
        self.assertEqual(stderr, "")
        self.assertNotIn(CANARY, stdout + stderr)

    def test_a_bad_switch_value_is_refused_at_start(self):
        self.start(FACTORY_MANAGER_OPERATIONS="maybe")
        self.assertEqual(self.process.wait(timeout=30), 2)
        self.assertEqual(self.finish(), ("", "factory-manager: FACTORY_MANAGER_OPERATIONS must be 'on' or 'off'\n"))


class PrivateOutputTests(unittest.TestCase):
    def test_the_module_never_prints(self):
        self.assertNotIn("print(", Path(ops.__file__).read_text())


if __name__ == "__main__":
    unittest.main()

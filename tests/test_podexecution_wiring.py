"""What the three callers of ``run_agent`` and the manager hand to the pod backend (fixes of the second review).

The pod backend itself is tested in tests/test_podbackend.py; here a recording factory stands in for
``PodBackend.from_environment`` and the arguments of the call are the subject: a caller that drops ``spent_usd``,
``budget_usd``, ``base``, ``attempt`` or ``job_id`` leaves the pod pool without the run's budget or the session
without its identity, and nothing else would notice. Also: the manager's sweeper is a ``Sweeper`` with the switch on
and nothing (and no pod module imported) with it off.
"""

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from factory import planner, podbackend, podcontract, podexecution, podsweep
from factory.config import RepoConfig
from factory.manager import Manager, Settings
from factory.phases import AgentOptions
from factory.preflight import CAPABILITIES, probe_worker
from factory.runner import Runner
from tests.test_agent import AgentTestCase
from tests.test_factory import assignment

ROOT = Path(__file__).resolve().parent.parent
SWITCH = podcontract.ENV_MANAGER["pod_backend"]
FOUND = {key: {"status": "available", "evidence": "synthetic"} for key in CAPABILITIES}


class Wired(AgentTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(patch.dict(os.environ, {SWITCH: "on"}))
        self.factory = self.enterContext(patch.object(podbackend.PodBackend, "from_environment", return_value="THE-BACKEND"))

    def runner(self, **config):
        directory = Path(tempfile.mkdtemp(dir=self.root))
        runner = Runner(directory, directory, assignment(), cfg=RepoConfig(repo="org/name", execution="pod", **config))
        runner.issue_labels.sync = lambda wanted: None
        runner.state.update("fix-a", base="0123abc", attempt=2)
        runner.state.save()
        (directory / "fix-a").mkdir()
        return runner

    def made(self):
        self.factory.assert_called_once()
        return self.factory.call_args.kwargs


class RunnerTests(Wired):
    def invoke(self, runner):
        with (
            patch("factory.runner.run_agent", return_value=({"ok": True}, {})) as called,
            patch.object(Runner, "guidance", return_value=""),
        ):
            runner.invoke("fix-a", "build-0", "prompt", self.cwd)
        self.assertEqual(called.call_args.kwargs["backend"], "THE-BACKEND")

    def test_a_session_is_described_to_the_backend_with_its_job_attempt_base_and_the_run_budget(self):
        runner = self.runner(run_budget_usd=7.5)
        self.invoke(runner)
        made = self.made()
        self.assertEqual((made["job_id"], made["attempt"], made["base"], made["budget_usd"]), ("fix-a", 2, "0123abc", 7.5))

    def test_the_spend_it_is_given_is_the_runs_cost_read_when_the_pool_asks_not_when_the_session_started(self):
        runner = self.runner(run_budget_usd=7.5)
        self.invoke(runner)
        spent = self.made()["spent_usd"]
        self.assertTrue(callable(spent))
        self.assertEqual(spent(), 0)
        runner.state.update("fix-a", usage=[{"total_cost_usd": 1.25}])
        self.assertEqual(spent(), runner.run_cost())
        self.assertEqual(spent(), 1.25)

    def test_a_repository_without_a_run_budget_hands_the_backend_none(self):
        self.invoke(self.runner())
        self.assertIsNone(self.made()["budget_usd"])


class PreflightTests(Wired):
    def test_the_probe_is_described_to_the_backend_with_its_job_attempt_base_and_the_run_budget(self):
        runner = self.runner(run_budget_usd=7.5)
        runner.state.update("fix-a", attempt=3)
        with (
            patch("factory.preflight.run_agent", return_value=(FOUND, {})) as called,
            patch.object(Runner, "guidance", return_value=""),
        ):
            probe_worker(runner, "fix-a", "build", self.cwd)
        self.assertEqual(called.call_args.kwargs["backend"], "THE-BACKEND")
        made = self.made()
        self.assertEqual((made["job_id"], made["attempt"], made["base"], made["budget_usd"]), ("fix-a", 3, "0123abc", 7.5))
        spent = made["spent_usd"]
        runner.state.update("fix-a", usage=[{"total_cost_usd": 2.0}])
        self.assertEqual(spent(), 2.0)


class PlannerTests(Wired):
    def test_the_plan_session_is_the_job_planner_at_attempt_zero(self):
        destination = self.root / "dest"
        destination.mkdir()
        issues = [{"number": 1, "title": "t", "body": "b", "comments": [], "labels": [], "url": "u"}]
        result = ({"version": 1, "jobs": [], "dispositions": []}, {})
        with patch("factory.planner.run_agent", return_value=result) as called, patch.object(planner, "repo_guidance", return_value=""):
            planner.run_planner(self.cwd, destination, issues, [], 60, AgentOptions(), None, None, execution="pod")
        self.assertEqual(called.call_args.kwargs["backend"], "THE-BACKEND")
        made = self.made()
        self.assertEqual((made["job_id"], made["attempt"]), ("planner", 0))


class ManagerSweeperTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        for name in ("config", "data", "spool"):
            (self.root / name).mkdir()
        self.base = {"FACTORY_CONFIG": str(self.root / "config"), "FACTORY_DATA": str(self.root / "data")}

    def manager(self, **extra):
        env = {**self.base, **extra}
        manager = Manager(Settings.from_env(env), env=env, stdout=io.StringIO(), stderr=io.StringIO())
        self.addCleanup(manager.runner.close)
        return manager

    def on(self):
        return {
            SWITCH: "on",
            podcontract.ENV_MANAGER["launcher_socket"]: str(self.root / "launcher.sock"),
            podcontract.ENV_MANAGER["spool_dir"]: str(self.root / "spool"),
        }

    def test_the_switch_on_gives_the_manager_a_sweeper_over_its_spool(self):
        manager = self.manager(**self.on())
        self.assertIsInstance(manager.pods, podsweep.Sweeper)
        self.assertEqual(manager.pods._spool, str(self.root / "spool"))

    def test_the_switch_off_or_unset_or_misspelled_gives_it_none(self):
        for value in (None, "off", "ON", "true", ""):
            with self.subTest(value=value):
                env = self.on()
                env.pop(SWITCH)
                if value is not None:
                    env[SWITCH] = value
                self.assertIsNone(self.manager(**env).pods)

    def test_the_sweeper_is_the_one_podexecution_builds_from_the_managers_environment(self):
        with patch.object(podexecution, "sweeper", return_value="SWEEPER") as built:
            manager = self.manager(**self.on())
        self.assertEqual(manager.pods, "SWEEPER")
        self.assertEqual(built.call_args.args[0][SWITCH], "on")

    def test_a_manager_with_the_switch_off_imports_no_pod_backend(self):
        code = (
            "import io, sys\n"
            "from factory.manager import Manager, Settings\n"
            "env = {'FACTORY_CONFIG': sys.argv[1], 'FACTORY_DATA': sys.argv[2]}\n"
            "m = Manager(Settings.from_env(env), env=env, stdout=io.StringIO(), stderr=io.StringIO())\n"
            "print(m.pods is None, 'factory.podbackend' in sys.modules, 'factory.podpool' in sys.modules, 'factory.launcher_client' in sys.modules)\n"
            "m.runner.close()\n"
        )
        environ = {key: value for key, value in os.environ.items() if key != SWITCH}
        environ["PYTHONPATH"] = str(ROOT / "src")
        done = subprocess.run(
            [sys.executable, "-c", code, self.base["FACTORY_CONFIG"], self.base["FACTORY_DATA"]],
            env=environ, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.split(), ["True", "False", "False", "False"])


if __name__ == "__main__":
    unittest.main()

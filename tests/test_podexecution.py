"""The ``execution`` setting and the manager's pod switch (slice 12a of the pod backend, docs/pod-backend.md).

Pod mode is REFUSED here (the pod backend itself is tested in tests/test_podbackend.py): nothing in these
tests starts a pod, calls Kubernetes or needs a cluster. The in-process path is proven unchanged by what the three callers of ``run_agent`` pass (nothing
extra) and by the golden captures of ``tests/test_backend.py``, which this slice does not touch.
"""

import contextlib
import dataclasses
import io
import itertools
import json
import os
import re
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from factory import cli, config, planner, podcontract, podexecution
from factory.agent import AgentError, run_agent
from factory.backend import AgentBackend
from factory.config import RepoConfig
from factory.manager import Manager, Settings
from factory.phases import AgentOptions
from factory.preflight import CAPABILITIES, probe_worker
from factory.runner import Runner

from tests.test_agent import AgentTestCase
from tests.test_config import ALL_KEYS, REPO, SAMPLE, ConfigTestCase, flatten
from tests.test_factory import assignment
from tests.test_plan_append import AppendCase

ROOT = Path(__file__).resolve().parent.parent
SWITCH = podcontract.ENV_MANAGER["pod_backend"]
INSTANCE = podcontract.MANAGER_INSTANCE_ENV
# Every value of the switch a deployment could carry, with the refusal each one gets for ``pod``.
SWITCH_CASES = (
    (None, podexecution.REFUSED_SWITCH_OFF),
    ("", podexecution.REFUSED_SWITCH_OFF),
    ("off", podexecution.REFUSED_SWITCH_OFF),
    ("ON", podexecution.REFUSED_SWITCH_OFF),
    (" on", podexecution.REFUSED_SWITCH_OFF),
    ("true", podexecution.REFUSED_SWITCH_OFF),
    ("on", podexecution.REFUSED_UNCONFIGURED),
)


@contextlib.contextmanager
def switch(value):
    """The process environment with the pod switch unset (None) or set to ``value``; restored on exit."""
    with patch.dict(os.environ):
        os.environ.pop(SWITCH, None)
        if value is not None:
            os.environ[SWITCH] = value
        yield


class ExecutionSettingTests(ConfigTestCase):
    def test_the_default_is_inprocess(self):
        self.assertEqual(self.load(self.body()).execution, "inprocess")
        self.assertEqual(RepoConfig().execution, "inprocess")
        self.assertEqual(config.EXECUTION_MODES, ("inprocess", "pod"))
        self.assertEqual((config.EXECUTION_INPROCESS, config.EXECUTION_POD), ("inprocess", "pod"))

    def test_both_values_load(self):
        for value in ("inprocess", "pod"):
            with self.subTest(value):
                self.assertEqual(self.load(self.body(f'execution = "{value}"\n')).execution, value)

    def test_the_sample_sets_it_to_the_non_default_value_and_it_round_trips(self):
        self.assertIn("execution", ALL_KEYS)
        self.assertIn("execution", flatten(tomllib.loads(SAMPLE)))
        self.assertEqual(self.load(SAMPLE).execution, "pod")

    def test_everything_else_is_rejected_naming_the_key(self):
        for literal in ('"Pod"', '""', '"none"', '"in-process"', '"pod "', "true", "1", '["pod"]', '"inprocess,pod"'):
            with self.subTest(literal):
                message = self.assertRejected(self.body(f"execution = {literal}\n"), "execution must be one of inprocess, pod")
                self.assertLessEqual(len(message), 400)
        self.assertRejected(self.body('[execution]\nmode = "pod"\n'), "execution must be one of inprocess, pod")

    def test_a_config_built_in_code_is_validated_too(self):
        self.assertEqual(RepoConfig(repo=REPO, execution="pod").execution, "pod")
        with self.assertRaises(ValueError):
            RepoConfig(repo=REPO, execution="elsewhere")
        with self.assertRaises(ValueError):
            dataclasses.replace(RepoConfig(repo=REPO), execution="elsewhere")

    def test_a_long_bad_value_is_cut_in_the_message(self):
        message = self.assertRejected(self.body(f'execution = "{"x" * 500}"\n'), "execution must be one of")
        self.assertLess(len(message), 300)

    def test_the_documented_example_shows_the_key_with_its_default(self):
        block = re.search(r"```toml\n(.*?)```", (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8"), re.S)
        self.assertIsNotNone(block)
        document = tomllib.loads(block.group(1))
        self.assertEqual(document["execution"], "inprocess")


class SwitchTests(unittest.TestCase):
    def test_only_the_exact_word_on_turns_it_on(self):
        for value, _ in SWITCH_CASES:
            with self.subTest(value=value):
                env = {} if value is None else {SWITCH: value}
                self.assertIs(podexecution.switch_is_on(env), value == "on")
                self.assertEqual(podexecution.switch_value(env), "on" if value == "on" else "off")

    def test_the_words_are_the_contracts(self):
        self.assertEqual((podcontract.POD_BACKEND_ON, podcontract.POD_BACKEND_OFF), ("on", "off"))
        self.assertEqual(SWITCH, "FACTORY_POD_BACKEND")

    def test_the_process_environment_is_the_default(self):
        with switch("on"):
            self.assertTrue(podexecution.switch_is_on())
            self.assertEqual(podexecution.switch_value(), "on")
        with switch(None):
            self.assertFalse(podexecution.switch_is_on())
            self.assertEqual(podexecution.switch_value(), "off")


class BackendSelectionTests(unittest.TestCase):
    def test_inprocess_selects_the_local_backend_and_adds_no_argument(self):
        for value, _ in SWITCH_CASES:
            with self.subTest(value=value):
                env = {} if value is None else {SWITCH: value}
                self.assertIsNone(podexecution.backend_for("inprocess", env))
                self.assertEqual(podexecution.agent_kwargs("inprocess", env), {})

    def test_pod_is_refused_for_every_value_of_the_switch_with_its_own_text(self):
        for value, expected in SWITCH_CASES:
            with self.subTest(value=value):
                env = {} if value is None else {SWITCH: value}
                chosen = podexecution.backend_for("pod", env)
                self.assertIsInstance(chosen, podexecution.RefusedBackend)
                self.assertEqual(chosen.reason, expected)
                self.assertEqual(podexecution.agent_kwargs("pod", env), {"backend": chosen})

    def test_the_two_refusals_differ_and_both_say_nothing_was_started_and_the_way_back(self):
        off, unavailable = podexecution.REFUSED_SWITCH_OFF, podexecution.REFUSED_UNCONFIGURED
        self.assertNotEqual(off, unavailable)
        for text in (off, unavailable):
            self.assertIn("nothing was started", text)
            self.assertIn('Remove execution = "pod" from the repository config', text)
        self.assertIn("FACTORY_POD_BACKEND is not on", off)
        self.assertIn("not configured for this session", unavailable)
        self.assertNotIn("FACTORY_POD_BACKEND", unavailable)

    def test_an_unknown_mode_is_refused_and_never_runs_in_process(self):
        for value in ("Pod", "", None, "elsewhere", 1, True, ["pod"]):
            with self.subTest(value=value):
                chosen = podexecution.backend_for(value, {SWITCH: "on"})
                self.assertIsInstance(chosen, podexecution.RefusedBackend)
                self.assertEqual(chosen.reason, podexecution.REFUSED_UNKNOWN)
                self.assertIn("backend", podexecution.agent_kwargs(value, {SWITCH: "on"}))

    def test_the_texts_never_carry_a_value_from_the_environment_or_the_config(self):
        poisoned = "SECRET-FROM-THE-ENVIRONMENT"
        for execution in ("pod", poisoned):
            chosen = podexecution.backend_for(execution, {SWITCH: poisoned})
            self.assertNotIn(poisoned, chosen.reason)
        for text in (podexecution.REFUSED_SWITCH_OFF, podexecution.REFUSED_UNCONFIGURED, podexecution.REFUSED_UNKNOWN):
            self.assertTrue(text.isascii())
            self.assertNotIn("\n", text)

    def test_the_refusal_is_an_agent_backend_and_every_step_refuses(self):
        refused = podexecution.RefusedBackend("fixed reason")
        self.assertIsInstance(refused, AgentBackend)
        for call in (
            lambda: refused.prepare(object()),
            lambda: refused.launch(object(), lambda handle: None),
            lambda: refused.collect(object(), object()),
        ):
            with self.assertRaises(AgentError) as caught:
                call()
            self.assertEqual(str(caught.exception), "fixed reason")
            self.assertIsNone(caught.exception.usage)

    def test_the_refusal_is_immutable(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            podexecution.RefusedBackend("x").reason = "y"


class RefusalLaunchesNothingTests(AgentTestCase):
    """With ``execution = "pod"`` no worker starts and no run artifact is written, whatever the switch says."""

    STARTED = "pathlib.Path(__file__).with_name('started').write_text('yes')\nsys.stdin.read()\nemit({'ok': True})"

    def setUp(self):
        super().setUp()
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))  # Runner.invoke announces each phase
        self.stub(self.STARTED)
        popen = patch("factory.backend.subprocess.Popen", side_effect=AssertionError("a process was started"))
        popen.start()
        self.addCleanup(popen.stop)
        self.run_dir.mkdir()

    def assertNothingStarted(self):
        self.assertFalse((self.binary / "started").exists())
        self.assertEqual(sorted(path.name for path in self.run_dir.rglob("*")), [])

    def runner(self, execution):
        directory = Path(tempfile.mkdtemp(dir=self.root))
        runner = Runner(directory, directory, assignment(), cfg=RepoConfig(repo="org/name", execution=execution))
        runner.issue_labels.sync = lambda wanted: None
        runner.state.save()
        return runner, directory

    def test_run_agent_refuses_before_anything_is_written_or_started(self):
        for value, expected in SWITCH_CASES:
            with self.subTest(value=value), switch(value):
                backend = podexecution.backend_for("pod")
                with self.assertRaises(AgentError) as caught:
                    run_agent("p", self.cwd, self.run_dir / "0-build-0", "build-0", backend=backend)
                self.assertEqual(str(caught.exception), expected)
                self.assertNothingStarted()

    def test_every_session_of_the_runner_is_refused_with_the_fixed_text(self):
        for (value, expected), phase in itertools.product(SWITCH_CASES, ("build-0", "review-0", "verify")):
            with self.subTest(value=value, phase=phase), switch(value):
                runner, directory = self.runner("pod")
                with patch.object(Runner, "guidance", return_value=""), self.assertRaises(AgentError) as caught:
                    runner.invoke("fix-a", phase, "prompt", self.cwd)
                self.assertEqual(str(caught.exception), expected)
                self.assertFalse((self.binary / "started").exists())
                state = runner.state.data["jobs"]["fix-a"]
                self.assertFalse(state.get("usage"))
                self.assertFalse(state.get("pid"))
                self.assertEqual(sorted(directory.rglob("*.prompt.txt")), [])

    def test_the_planner_is_refused_with_the_fixed_text(self):
        destination = self.root / "dest"
        destination.mkdir()
        issues = [{"number": 1, "title": "t", "body": "b", "comments": [], "labels": [], "url": "u"}]
        for value, expected in SWITCH_CASES:
            with self.subTest(value=value), switch(value), patch.object(planner, "repo_guidance", return_value=""):
                with self.assertRaises(AgentError) as caught:
                    planner.run_planner(self.cwd, destination, issues, [], 60, AgentOptions(), None, None, execution="pod")
                self.assertEqual(str(caught.exception), expected)
                self.assertFalse((self.binary / "started").exists())
                self.assertFalse((destination / "planner.prompt.txt").exists())

    def test_the_preflight_probe_is_refused_with_the_fixed_text(self):
        for value, expected in SWITCH_CASES:
            with self.subTest(value=value), switch(value):
                runner, directory = self.runner("pod")
                with patch.object(Runner, "guidance", return_value=""), self.assertRaises(AgentError) as caught:
                    probe_worker(runner, "fix-a", "build", self.cwd)
                self.assertEqual(str(caught.exception), expected)
                self.assertFalse((self.binary / "started").exists())
                self.assertEqual(sorted(directory.rglob("*.prompt.txt")), [])


class InProcessIsUnchangedTests(AgentTestCase):
    """``execution = "inprocess"`` (every repository today): the three callers make the call they always made."""

    def setUp(self):
        super().setUp()
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))  # Runner.invoke announces each phase

    def runner(self, cfg):
        directory = Path(tempfile.mkdtemp(dir=self.root))
        runner = Runner(directory, directory, assignment(), cfg=cfg)
        runner.issue_labels.sync = lambda wanted: None
        runner.state.save()
        (directory / "fix-a").mkdir()  # where a session's files go; the real run_agent makes it, a fake does not
        return runner

    def test_the_default_repository_still_runs_its_session_as_a_child_process(self):
        self.stub("pathlib.Path(__file__).with_name('started').write_text('yes')\nsys.stdin.read()\nemit({'ok': True})")
        for value in (None, "on"):
            with self.subTest(switch=value), switch(value):
                (self.binary / "started").unlink(missing_ok=True)
                runner = self.runner(RepoConfig(repo="org/name"))
                with patch.object(Runner, "guidance", return_value=""):
                    result = runner.invoke("fix-a", "build-0", "prompt", self.cwd)
                self.assertEqual(result, {"ok": True})
                self.assertTrue((self.binary / "started").exists())

    def test_the_runner_passes_no_backend(self):
        runner = self.runner(RepoConfig(repo="org/name"))
        for value in (None, "on"):
            with self.subTest(switch=value), switch(value):
                with (
                    patch("factory.runner.run_agent", return_value=({"ok": True}, {})) as called,
                    patch.object(Runner, "guidance", return_value=""),
                ):
                    runner.invoke("fix-a", "build-0", "prompt", self.cwd)
                self.assertEqual(
                    sorted(called.call_args.kwargs), ["choice", "group", "guidance", "options", "user"]
                )

    def test_the_runner_hands_the_backend_of_a_pod_repository_to_every_session(self):
        runner = self.runner(RepoConfig(repo="org/name", execution="pod"))
        with (
            patch("factory.runner.run_agent", return_value=({"ok": True}, {})) as called,
            patch.object(Runner, "guidance", return_value=""),
            switch("on"),
        ):
            runner.invoke("fix-a", "build-0", "prompt", self.cwd)
        chosen = called.call_args.kwargs["backend"]
        self.assertIsInstance(chosen, podexecution.RefusedBackend)
        self.assertEqual(chosen.reason, podexecution.REFUSED_UNCONFIGURED)

    def test_the_planner_passes_no_backend_and_hands_a_pod_repository_its_backend(self):
        destination = self.root / "dest"
        destination.mkdir()
        issues = [{"number": 1, "title": "t", "body": "b", "comments": [], "labels": [], "url": "u"}]
        result = ({"version": 1, "jobs": [], "dispositions": []}, {})
        for execution, expected in (("inprocess", None), ("pod", podexecution.REFUSED_SWITCH_OFF)):
            with (
                self.subTest(execution=execution),
                switch(None),
                patch("factory.planner.run_agent", return_value=result) as called,
                patch.object(planner, "repo_guidance", return_value=""),
            ):
                planner.run_planner(self.cwd, destination, issues, [], 60, AgentOptions(), None, None, execution=execution)
            if expected is None:
                self.assertNotIn("backend", called.call_args.kwargs)
            else:
                self.assertEqual(called.call_args.kwargs["backend"].reason, expected)
        with patch("factory.planner.run_agent", return_value=result) as called, patch.object(planner, "repo_guidance", return_value=""):
            planner.run_planner(self.cwd, destination, issues, [], 60, AgentOptions(), None, None)
        self.assertNotIn("backend", called.call_args.kwargs)

    def test_the_preflight_probe_passes_no_backend_and_hands_a_pod_repository_its_backend(self):
        found = {key: {"status": "available", "evidence": "synthetic"} for key in CAPABILITIES}
        for execution, expected in (("inprocess", None), ("pod", podexecution.REFUSED_SWITCH_OFF)):
            with self.subTest(execution=execution), switch(None):
                runner = self.runner(RepoConfig(repo="org/name", execution=execution))
                with (
                    patch("factory.preflight.run_agent", return_value=(found, {})) as called,
                    patch.object(Runner, "guidance", return_value=""),
                ):
                    probe_worker(runner, "fix-a", "build", self.cwd)
                if expected is None:
                    self.assertNotIn("backend", called.call_args.kwargs)
                else:
                    self.assertEqual(called.call_args.kwargs["backend"].reason, expected)

    def test_the_plan_command_hands_the_repositorys_execution_to_the_planner(self):
        class Stop(Exception):
            pass

        args = SimpleNamespace(issues=[1], limit=None, timeout=10, use_snapshot=False)
        for execution in ("inprocess", "pod"):
            directory = Path(tempfile.mkdtemp(dir=self.root))
            cfg = RepoConfig(repo="org/name", execution=execution)
            with (
                self.subTest(execution=execution),
                patch.dict(os.environ, {"FACTORY_MAX_TOTAL_WORKERS": "off"}),
                patch.object(cli, "planner_worktree", return_value=self.cwd),
                patch.object(cli, "share_planner_tree"),
                patch.object(cli, "label_authority", return_value=None),
                patch.object(cli, "plan", side_effect=Stop) as plan,
                self.assertRaises(Stop),
            ):
                cli.plan_command(args, cfg, self.cwd, directory, None)
            self.assertEqual(plan.call_args.kwargs["execution"], execution)


class PlannerPlumbingTests(unittest.TestCase):
    """``plan`` and ``run_screened`` hand the repository's setting down to the session that runs the planner."""

    class Stop(Exception):
        pass

    def test_the_setting_travels_from_plan_to_run_screened_to_run_planner(self):
        issue = {"number": 1, "title": "t", "body": "b", "comments": [], "labels": [], "url": "u"}
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            (destination / "issues.json").write_text(json.dumps([issue]))
            (destination / "open-prs.json").write_text("[]")
            for execution in (None, "inprocess", "pod"):
                chosen = {} if execution is None else {"execution": execution}
                with self.subTest(execution=execution):
                    with patch.object(planner, "run_screened", side_effect=self.Stop) as screened, self.assertRaises(self.Stop):
                        planner.plan(destination, destination, use_snapshot=True, **chosen)
                    self.assertEqual(screened.call_args.kwargs["execution"], execution or "inprocess")
                    with patch.object(planner, "run_planner", side_effect=self.Stop) as session, self.assertRaises(self.Stop):
                        planner.run_screened(destination, destination, [issue], [], 60, AgentOptions(), None, None, **chosen)
                    self.assertEqual(session.call_args.kwargs["execution"], execution or "inprocess")


class AppendHandsTheExecutionOn(AppendCase):
    def setUp(self):
        super().setUp()
        self.seen_kwargs = []

    def fake_agent(self, prompt, cwd, prefix, phase, *args, **kwargs):
        self.seen_kwargs.append(kwargs)
        return super().fake_agent(prompt, cwd, prefix, phase, *args, **kwargs)

    def test_a_default_repository_is_planned_by_an_append_with_no_backend(self):
        with switch(None):
            self.append((2,))
        self.assertEqual(len(self.seen_kwargs), 1)
        self.assertNotIn("backend", self.seen_kwargs[0])

    def test_a_pod_repository_is_planned_by_an_append_with_the_refusing_backend(self):
        with switch(None):
            self.append((2,), cfg=RepoConfig(repo="org/name", execution="pod"))
        self.assertEqual(len(self.seen_kwargs), 1)
        self.assertEqual(self.seen_kwargs[0]["backend"].reason, podexecution.REFUSED_SWITCH_OFF)


class ManagerInstanceTests(unittest.TestCase):
    PRINT = f"import os; print(os.environ.get({INSTANCE!r})); print(os.environ.get({SWITCH!r}))"

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        for name in ("config", "data"):
            (self.root / name).mkdir()
        self.base = {"FACTORY_CONFIG": str(self.root / "config"), "FACTORY_DATA": str(self.root / "data")}

    def manager(self, **extra):
        env = {**self.base, **extra}
        manager = Manager(Settings.from_env(env), env=env, stdout=io.StringIO(), stderr=io.StringIO())
        self.addCleanup(manager.runner.close)
        return manager

    def seen_by_a_short_command(self, manager):
        result = manager.runner([sys.executable, "-I", "-c", self.PRINT], 30)
        self.assertEqual(result.returncode, 0)
        return result.stdout.decode().split()

    def seen_by_a_supervisor(self, manager):
        log = Path(tempfile.mkdtemp(dir=self.root)) / "supervisor.log"  # the spawner appends: one log per child
        child = manager.spawner([sys.executable, "-I", "-c", self.PRINT], log)
        self.assertEqual(child.wait(30), 0)
        return log.read_text().split()

    def test_the_instance_id_is_twelve_lowercase_hex_characters(self):
        manager = self.manager()
        self.assertEqual(podcontract.validate_instance_id(manager.instance_id), manager.instance_id)
        self.assertRegex(manager.instance_id, r"^[0-9a-f]{12}$")

    def test_every_start_draws_a_new_one(self):
        with patch.object(podcontract, "new_instance_id", side_effect=["aaaaaaaaaaaa", "bbbbbbbbbbbb"]) as draw:
            first, second = self.manager(), self.manager()
        self.assertEqual((first.instance_id, second.instance_id), ("aaaaaaaaaaaa", "bbbbbbbbbbbb"))
        self.assertEqual(draw.call_count, 2)
        self.assertNotEqual(self.manager().instance_id, self.manager().instance_id)

    def test_it_reaches_the_supervisors_and_the_short_commands_it_starts(self):
        manager = self.manager()
        self.assertEqual(manager.env[INSTANCE], manager.instance_id)
        for seen in (self.seen_by_a_supervisor(manager), self.seen_by_a_short_command(manager)):
            self.assertEqual(seen[0], manager.instance_id)

    def test_an_older_value_in_the_inherited_environment_is_replaced(self):
        manager = self.manager(**{INSTANCE: "0123456789ab"})
        self.assertNotEqual(manager.instance_id, "0123456789ab")
        self.assertEqual(self.seen_by_a_supervisor(manager)[0], manager.instance_id)

    def test_the_switch_reaches_the_supervisors_in_its_canonical_form(self):
        for value, expected in (("on", "on"), ("off", "off"), ("ON", "off"), ("yes", "off"), ("", "off")):
            with self.subTest(value=value):
                manager = self.manager(**{SWITCH: value})
                self.assertEqual(self.seen_by_a_supervisor(manager)[1], expected)
        self.assertEqual(self.seen_by_a_supervisor(self.manager())[1], "off")

    def test_the_metrics_token_still_stays_in_the_manager(self):
        manager = self.manager(FACTORY_MANAGER_METRICS_TOKEN="x" * 40)
        self.assertNotIn("FACTORY_MANAGER_METRICS_TOKEN", manager.env)


class DocumentationTests(unittest.TestCase):
    def read(self, name):
        return (ROOT / name).read_text(encoding="utf-8")

    def test_the_manager_documents_its_two_new_names(self):
        text = self.read("docs/manager.md")
        for name in (INSTANCE, SWITCH):
            with self.subTest(name):
                self.assertIn(f"| `{name}` |", text)

    def test_the_repository_key_is_documented_where_the_settings_are(self):
        for name in ("docs/manager.md", "docs/operations.md", "docs/enrolling-repositories.md", "docs/pod-backend.md", "docs/configuration.md"):
            with self.subTest(name):
                self.assertIn("`execution`", self.read(name))

    def test_the_design_record_quotes_both_refusals_and_says_which_one_the_backend_replaced(self):
        text = self.read("docs/pod-backend.md")
        for reason in (podexecution.REFUSED_SWITCH_OFF, podexecution.REFUSED_UNCONFIGURED):
            with self.subTest(reason=reason[:40]):
                self.assertIn(reason.split(" Remove ")[0], text)
        self.assertNotIn("**still refused**", text)
        self.assertNotIn("until slice 12b exists", text)
        self.assertIn("no pod backend yet", text)  # the text slice 12b replaced is named, so nobody searches for it
        self.assertIn("No silent fallback", text)

    def test_the_architecture_reference_names_the_module(self):
        self.assertIn("`podexecution.py`", self.read("docs/architecture.md"))


if __name__ == "__main__":
    unittest.main()

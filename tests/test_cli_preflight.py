"""CLI and worker-capability regressions without network or live agents."""

import argparse
import contextlib
import copy
import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from factory import cli
from factory.blockers import record_capabilities
from factory.phases import AgentOptions
from factory.preflight import CAPABILITIES, probe_worker
from factory.runner import Runner
from tests import test_factory as fixtures
from tests.test_factory import assignment


class CommandTests(unittest.TestCase):
    setUp = fixtures.IntegrationTests.setUp
    git = fixtures.IntegrationTests.git
    script = fixtures.IntegrationTests.script
    install_agent = fixtures.IntegrationTests.install_agent

    def invoke_cli(self, *args):
        output = io.StringIO()
        with (
            contextlib.chdir(self.repo),
            contextlib.redirect_stdout(output),
            patch("sys.argv", ["factory", "--run-dir", str(self.run_dir), *args]),
        ):
            cli.main()
        return output.getvalue()

    def test_status_json_is_read_only_and_missing_run_is_not_created(self):
        plan = assignment()
        (self.run_dir / "plan.json").write_text(json.dumps(plan))
        runner = Runner(self.repo, self.run_dir, plan)
        before = runner.state.path.read_bytes()
        report = json.loads(self.invoke_cli("status", "--json"))
        self.assertEqual(report["active_worker_count"], 0)
        self.assertEqual(runner.state.path.read_bytes(), before)
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing"
            with (
                contextlib.chdir(self.repo),
                contextlib.redirect_stderr(io.StringIO()),
                patch("sys.argv", ["factory", "--run-dir", str(missing), "status"]),
                self.assertRaises(SystemExit),
            ):
                cli.main()
            self.assertFalse(missing.exists())

    def test_merge_and_close_commands_do_not_exist(self):
        # The factory opens pull requests and never merges them or closes issues (docs/handoff.md): no subcommand of the
        # parser may start that way, whatever it is called, and no `run` option may ask for a merge.
        parser = cli.build_parser()
        (commands,) = [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]
        names = sorted(commands.choices)
        self.assertLessEqual({"plan", "run", "status", "purge", "retry", "stop"}, set(names))  # the parser was read, not an empty list
        self.assertEqual([name for name in names if name.startswith(("merge", "close"))], [])
        for argv in (["run", "--merge"], ["run", "--merge-timeout", "5"]):
            with (
                self.subTest(argv=argv),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as caught,
            ):
                self.invoke_cli(*argv)
            self.assertEqual(caught.exception.code, 2)
        helptext = parser.format_help()
        self.assertNotIn("merge", helptext)
        self.assertIn("purge", helptext)
        self.assertIn("--repo", helptext)

    def test_capability_probe_uses_actual_phase_sandbox_without_promoting_job(self):
        plan = assignment()
        runner = Runner(self.repo, self.run_dir, plan)
        before = copy.deepcopy(runner.state.data["jobs"]["fix-a"])
        result = {
            key: {"status": "unavailable", "evidence": "synthetic denied probe"}
            for key in CAPABILITIES
        }
        for phase in ("build", "verify"):
            runner.options = AgentOptions(verify_domains=("registry.npmjs.org",))
            with patch(
                "factory.preflight.run_agent", return_value=(result, {})
            ) as agent:
                report = probe_worker(runner, "fix-a", phase, self.repo)
            self.assertEqual(agent.call_args.args[3], "preflight")
            self.assertEqual(agent.call_args.kwargs["target"], phase)
            self.assertEqual(len(report["capabilities"]), 4)
        state = runner.state.data["jobs"]["fix-a"]
        self.assertEqual(state["status"], before["status"])
        self.assertEqual(state["attempt"], before["attempt"])
        self.assertNotIn("verification", state)
        self.assertEqual(len(runner.state.data["capabilities"]), 8)

    def test_verify_probe_requires_its_url_host_on_the_verify_allowlist(self):
        runner = Runner(self.repo, self.run_dir, assignment())
        result = {
            key: {"status": "available", "evidence": "synthetic"} for key in CAPABILITIES
        }
        for domains in ((), ("pypi.org",)):
            runner.options = AgentOptions(verify_domains=domains)
            with (
                patch("factory.preflight.run_agent", return_value=(result, {})) as agent,
                self.assertRaisesRegex(ValueError, "not on the verify allowlist"),
            ):
                probe_worker(runner, "fix-a", "verify", self.repo)
            agent.assert_not_called()
        # The build probe measures a network-off sandbox, so no allowlist is needed.
        runner.options = AgentOptions()
        with patch("factory.preflight.run_agent", return_value=(result, {})) as agent:
            probe_worker(runner, "fix-a", "build", self.repo)
        self.assertEqual(agent.call_args.kwargs["target"], "build")

    def test_probe_script_and_prompt_use_the_configured_url_and_bash_tool(self):
        runner = Runner(self.repo, self.run_dir, assignment())
        runner.options = AgentOptions(
            verify_domains=("example.invalid",), probe_url="https://example.invalid/ping"
        )
        result = {
            key: {"status": "unavailable", "evidence": "synthetic"} for key in CAPABILITIES
        }
        with patch("factory.preflight.run_agent", return_value=(result, {})) as agent:
            probe_worker(runner, "fix-a", "verify", self.repo)
        prompt = agent.call_args.args[0]
        self.assertIn("'https://example.invalid/ping'", prompt)
        self.assertIn("Bash tool", prompt)
        self.assertNotIn("shell tool", prompt)
        self.assertNotIn("fonts.googleapis.com", prompt)

    def test_the_probe_script_runs_and_prints_exactly_the_capability_json(self):
        runner = Runner(self.repo, self.run_dir, assignment())
        # Nothing listens on port 9, so the download fails at once and without any network.
        runner.options = AgentOptions(probe_url="https://127.0.0.1:9/ping")
        result = {key: {"status": "unavailable", "evidence": "synthetic"} for key in CAPABILITIES}
        with patch("factory.preflight.run_agent", return_value=(result, {})) as agent:
            probe_worker(runner, "fix-a", "build", self.repo)
        prompt = agent.call_args.args[0]
        script = prompt[prompt.index("import json, pathlib") :]
        done = subprocess.run(
            [sys.executable, "-c", script], cwd=self.repo, capture_output=True, text=True, timeout=60
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        observed = json.loads(done.stdout)
        self.assertEqual(set(observed), CAPABILITIES)
        self.assertEqual(
            {name: item["status"] for name, item in observed.items()},
            {
                "workspace_write": "available",
                "shared_git_write": "available",
                "loopback_listener": "available",
                "network_download": "unavailable",
            },
        )

    def test_nothing_in_src_prints_an_agent_result_or_a_prompt(self):
        # The Phase 5 acceptance grep: grep -rn "print(.*result\|print(.*prompt" src
        found = [
            f"{path.name}:{number}"
            for path in sorted(Path(cli.__file__).resolve().parent.rglob("*.py"))
            for number, line in enumerate(path.read_text().splitlines(), 1)
            if re.search(r"print\(.*(result|prompt)", line)
        ]
        self.assertEqual(found, [])

    def test_budget_flags_are_validated_and_reach_agent_options(self):
        for bad in ("0", "-1", "nan", "inf", "abc"):
            with (
                self.subTest(value=bad),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as caught,
            ):
                self.invoke_cli("run", "--budget-build", bad)
            self.assertEqual(caught.exception.code, 2)
        with (
            contextlib.chdir(self.repo),
            patch(
                "sys.argv",
                ["factory", "--run-dir", str(self.run_dir), "run", "--budget-build", "2.5",
                 "--budget-review", "1", "--verify-domain", "pypi.org", "--verify-domain", "files.pythonhosted.org"],
            ),
            patch("factory.cli.Runner") as runner_class,
            patch("factory.cli.validate_plan", side_effect=lambda value: value),
        ):
            (self.run_dir / "plan.json").write_text(json.dumps(assignment()))
            runner_class.return_value.state.data = {"jobs": {}}
            runner_class.return_value.drained = False
            cli.main()
        options = runner_class.call_args.kwargs["options"]
        self.assertEqual(options.budgets, {"build": 2.5, "review": 1.0})
        self.assertEqual(options.verify_domains, ("pypi.org", "files.pythonhosted.org"))

    def test_planner_is_read_only_phase_with_evidence_files_readable(self):
        from factory import planner

        destination = self.run_dir / "planned"
        destination.mkdir()
        snapshot = ([{"number": 1, "title": "Fix A", "body": "A"}], [])
        output = {k: v for k, v in assignment().items() if k != "issues"}
        with (
            patch.object(planner, "snapshot", return_value=snapshot),
            patch.object(planner, "run_agent", return_value=(output, {"total_cost_usd": 0.5})) as agent,
        ):
            planner.plan(self.repo, destination, options=AgentOptions())
        self.assertEqual(agent.call_args.args[3], "plan")
        self.assertEqual(
            agent.call_args.kwargs["read_paths"],
            (destination / "issues.json", destination / "open-prs.json"),
        )
        self.assertEqual(json.loads((destination / "planner-usage.json").read_text()), {"total_cost_usd": 0.5})

    def test_malformed_probe_does_not_record_confirmed_capabilities(self):
        runner = Runner(self.repo, self.run_dir, assignment())
        with (
            patch(
                "factory.preflight.run_agent", return_value=({"available": True}, {})
            ),
            self.assertRaises(ValueError),
        ):
            probe_worker(runner, "fix-a", "verify", self.repo)
        self.assertNotIn("capabilities", runner.state.data)
        self.assertIsNone(runner.state.data["jobs"]["fix-a"].get("pid"))

    def test_prompts_include_external_prerequisites_without_mixing_worker_phases(self):
        plan = assignment()
        runner = Runner(self.repo, self.run_dir, plan)
        record_capabilities(
            runner.state,
            [
                {
                    "id": key,
                    "environment": environment,
                    "status": "unconfirmed",
                    "evidence": [],
                }
                for key, environment in (
                    ("external-windows-host", "designated test host"),
                    ("build-only-probe", "fix-a:build:/tmp/worktree"),
                    ("verify-only-probe", "fix-a:verify:/tmp/worktree"),
                )
            ],
        )
        for phase in ("build", "verify"):
            prompt = runner.prompt(
                "builder" if phase == "build" else phase, plan["jobs"][0]
            )
            self.assertIn("external-windows-host", prompt)
            self.assertIn(f"{phase}-only-probe", prompt)
            other = "verify" if phase == "build" else "build"
            self.assertNotIn(f"{other}-only-probe", prompt)

    def test_retry_requires_changed_premise_and_clears_resume_request(self):
        plan = assignment()
        (self.run_dir / "plan.json").write_text(json.dumps(plan))
        runner = Runner(self.repo, self.run_dir, plan)
        runner.state.update(
            "fix-a",
            status="failed",
            attempt=1,
            resume_phase="verify",
            error="old error",
        )
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.invoke_cli("retry", "fix-a")
        self.invoke_cli(
            "retry",
            "fix-a",
            "--reason",
            "New concrete review finding requires code changes",
        )
        state = json.loads(runner.state.path.read_text())["jobs"]["fix-a"]
        self.assertEqual(state["status"], "queued")
        self.assertIsNone(state["resume_phase"])
        self.assertEqual(len(state["retry_history"]), 1)


if __name__ == "__main__":
    unittest.main()

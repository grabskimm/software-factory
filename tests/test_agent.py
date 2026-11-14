"""Claude Code adapter: argv, env, result parsing, failure modes, process cleanup."""

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from factory import agent, capacity, memwatch
from factory import planner as planner_module
from factory import runner as runner_module
from factory import gitsafe
from factory.agent import AgentError, AgentTimeout, child_env, guidance_with_skills, repo_guidance, repo_skills, run_agent
from factory.phases import AgentOptions
from factory.schemas import SCHEMAS
from factory.status import token_summary, usage_tokens

from tests import agentstub

DUMP_ARGV = """
pathlib.Path(__file__).with_name('seen.json').write_text(json.dumps({
    'argv': ARGS,
    'env': dict(os.environ),
    'cwd': os.getcwd(),
    'home_exists': pathlib.Path(os.environ['HOME']).is_dir(),
    'config_exists': pathlib.Path(os.environ['CLAUDE_CONFIG_DIR']).is_dir(),
    'settings': json.loads(pathlib.Path(arg('--settings')).read_text()),
    'system_prompt': pathlib.Path(arg('--append-system-prompt-file')).read_text(),
    'stdin': sys.stdin.read(),
}))
emit({'ok': True})
"""

SECRET_LIKE = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH", re.IGNORECASE)


class AgentTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cwd = self.root / "work"
        self.cwd.mkdir()
        self.run_dir = self.root / "run"
        self.binary = self.root / "bin"
        self.binary.mkdir()
        patcher = patch.dict(
            os.environ, {"FACTORY_AGENT_CMD": str(self.binary / "fake-agent")}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def stub(self, body, raw=False):
        path = self.binary / "fake-agent"
        path.write_text(f"#!{sys.executable}\n" + (body if raw else agentstub.source(body)))
        path.chmod(0o755)

    def seen(self):
        return json.loads((self.binary / "seen.json").read_text())

    def run_phase(self, phase, **kwargs):
        return run_agent("do the thing", self.cwd, self.run_dir / f"0-{phase}", phase, **kwargs)


class ResultParsingTests(AgentTestCase):
    def test_success_returns_structured_output_and_usage_record(self):
        self.stub("sys.stdin.read()\nemit({'pass': True, 'findings': []})")
        result, usage = self.run_phase("review-0")
        self.assertEqual(result, {"pass": True, "findings": []})
        self.assertEqual(usage["phase"], "review")
        self.assertEqual(usage["model"], "claude-opus-5-5")
        self.assertEqual(usage["subtype"], "success")
        self.assertEqual(usage["total_cost_usd"], 0.0125)
        self.assertEqual(usage["num_turns"], 2)
        self.assertEqual(usage["duration_ms"], 25)
        self.assertEqual(usage["usage"]["output_tokens"], 5)
        # model_usage (includes subagents) is read from the CLI's camelCase key.
        self.assertIn("stub-model", usage["model_usage"])
        # "footprint" (memwatch) is optional: present only when the guard sampled the worker.
        self.assertEqual(set(usage) - {"footprint"}, {"phase", "model", "usage", "model_usage", "total_cost_usd", "num_turns", "duration_ms", "subtype"})

    def test_the_usage_record_keeps_the_cache_counts_the_token_reader_names(self):
        usage_object = (
            "{'input_tokens': 3, 'output_tokens': 4, 'cache_creation_input_tokens': 50, 'cache_read_input_tokens': 500, "
            "'cache_creation': {'ephemeral_5m_input_tokens': 45, 'ephemeral_1h_input_tokens': 5}}"
        )
        self.stub(f"emit({{'a': 1}}, usage={usage_object})")
        _, usage = self.run_phase("build-0")
        counts = usage_tokens(usage["usage"])
        self.assertEqual(
            counts,
            {
                "input_tokens": 3,
                "output_tokens": 4,
                "cache_creation_input_tokens": 50,
                "cache_read_input_tokens": 500,
                "ephemeral_5m_input_tokens": 45,
                "ephemeral_1h_input_tokens": 5,
            },
        )
        row = token_summary([{"usage": [usage]}])["build"]
        self.assertEqual(row["cache_hit_rate"], round(500 / 553, 6))

    def test_text_result_is_parsed_when_no_structured_output(self):
        self.stub("emit(text='```json\\n{\"pass\": false, \"findings\": [\"x\"]}\\n```')")
        result, _ = self.run_phase("review-0")
        self.assertEqual(result, {"pass": False, "findings": ["x"]})

    def test_snake_case_model_usage_is_accepted(self):
        self.stub("emit({'a': 1}, modelUsage=None, model_usage={'other': {}})")
        _, usage = self.run_phase("build-0")
        self.assertEqual(usage["model_usage"], {"other": {}})

    def test_last_result_event_wins_and_garbage_lines_are_ignored(self):
        self.stub(
            "print('not json')\n"
            "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'structured_output': {'v': 'first'}}))\n"
            "print('{broken')\n"
            "emit({'v': 'last'})\n"
        )
        result, _ = self.run_phase("build-0")
        self.assertEqual(result, {"v": "last"})

    def test_is_error_true_fails_even_when_subtype_is_success(self):
        self.stub(
            "emit({'v': 1}, is_error=True, result='SECRET-AGENT-TEXT', terminal_reason='api_error')"
        )
        with self.assertRaises(AgentError) as caught:
            self.run_phase("build-0")
        message = str(caught.exception)
        self.assertIn("is_error=true", message)
        self.assertIn("subtype=success", message)
        self.assertIn("terminal_reason=api_error", message)
        # Agent text must not leak into errors, which reach stdout and state.
        self.assertNotIn("SECRET-AGENT-TEXT", message)
        self.assertEqual(caught.exception.usage["total_cost_usd"], 0.0125)

    def test_budget_exhaustion_subtype_is_reported_and_spend_kept(self):
        self.stub(
            "emit(None, subtype='error_max_budget_usd', is_error=True, total_cost_usd=8.2)\n"
            "sys.exit(1)"
        )
        with self.assertRaises(AgentError) as caught:
            self.run_phase("build-0")
        self.assertIn("error_max_budget_usd", str(caught.exception))
        self.assertEqual(caught.exception.usage["total_cost_usd"], 8.2)

    def test_non_success_subtype_fails_even_without_is_error(self):
        self.stub("emit({'v': 1}, subtype='error_max_turns', is_error=False)")
        with self.assertRaisesRegex(AgentError, "error_max_turns"):
            self.run_phase("build-0")

    def test_missing_result_event_fails(self):
        self.stub(
            "print(json.dumps({'type': 'system', 'subtype': 'init'}))\nsys.stdin.read()",
            raw=False,
        )
        with self.assertRaisesRegex(RuntimeError, "no result event"):
            self.run_phase("build-0")

    def test_nonzero_exit_without_result_fails_with_exit_code(self):
        self.stub("sys.stderr.write('boom')\nsys.exit(3)")
        with self.assertRaisesRegex(RuntimeError, "agent exited 3"):
            self.run_phase("build-0")
        self.assertEqual((self.run_dir / "0-build-0.stderr").read_text(), "boom")

    def test_nonzero_exit_after_success_result_fails(self):
        self.stub("emit({'v': 1})\nsys.exit(2)")
        with self.assertRaisesRegex(AgentError, "exited 2"):
            self.run_phase("build-0")

    def test_unparseable_text_result_fails_without_echoing_it(self):
        self.stub("emit(text='definitely not json PRIVATE')")
        with self.assertRaises(AgentError) as caught:
            self.run_phase("build-0")
        self.assertNotIn("PRIVATE", str(caught.exception))

    def test_prompt_and_logs_stay_in_the_run_directory_not_stdout(self):
        self.stub("emit({'v': 1})")
        with patch("sys.stdout") as stdout, patch("sys.stderr") as stderr:
            self.run_phase("build-0")
        stdout.write.assert_not_called()
        stderr.write.assert_not_called()
        self.assertEqual((self.run_dir / "0-build-0.prompt.txt").read_text(), "do the thing")
        self.assertIn('"type": "result"', (self.run_dir / "0-build-0.jsonl").read_text())


class ProcessTests(AgentTestCase):
    def test_timeout_kills_the_whole_process_group_and_retains_logs(self):
        self.stub(
            "import os, pathlib, signal, time\n"
            "child = os.fork()\n"
            "if child == 0:\n"
            "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "    pathlib.Path('child.pid').write_text(str(os.getpid()))\n"
            "    time.sleep(30)\n"
            "else:\n"
            "    time.sleep(30)\n",
            raw=True,
        )
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_phase("build-0", timeout=0.5)
        child = (self.cwd / "child.pid").read_text()
        status = subprocess.run(
            ["ps", "-o", "stat=", "-p", child], capture_output=True, text=True, check=False
        ).stdout.strip()
        self.assertTrue(not status or status.startswith("Z"), status)
        self.assertTrue((self.run_dir / "0-build-0.stderr").exists())
        # The throwaway HOME is removed even when the call is killed.
        self.assertEqual([p.name for p in self.run_dir.glob(".home-*")], [])

    def test_a_timeout_raises_the_fixed_reason_not_the_command_line(self):
        self.stub("sys.stdin.read()\nimport time\ntime.sleep(30)")
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            self.run_phase("build-0", timeout=1)
        self.assertIsInstance(caught.exception, AgentTimeout)
        self.assertEqual(str(caught.exception), "timed out after 1 s")
        self.assertEqual(caught.exception.timeout, 1)

    def test_required_worker_identity_is_enforced_before_anything_is_spawned(self):
        self.stub("sys.stdin.read()\nemit({'v': 1})")
        with patch.dict(os.environ, {"FACTORY_REQUIRE_WORKER_IDENTITY": "1"}):
            with self.assertRaises(AgentError) as caught:
                self.run_phase("build-0")
        self.assertIn("[worker] uid and gid", str(caught.exception))
        self.assertFalse(self.run_dir.exists(), "nothing may be written or started")

    def test_worker_identity_is_not_required_outside_the_image(self):
        self.stub("sys.stdin.read()\nemit({'v': 1})")
        with patch.dict(os.environ, {"FACTORY_REQUIRE_WORKER_IDENTITY": "0"}):
            result, _usage = self.run_phase("build-0")
        self.assertEqual(result, {"v": 1})

    def test_on_start_receives_the_process_group_leader_pid(self):
        self.stub("sys.stdin.read()\nemit({'v': 1})")
        pids = []
        self.run_phase("build-0", on_start=pids.append)
        self.assertEqual(len(pids), 1)
        self.assertGreater(pids[0], 1)


class ArgvAndEnvTests(AgentTestCase):
    maxDiff = None

    def argv(self, phase, **kwargs):
        self.stub(DUMP_ARGV)
        self.run_phase(phase, **kwargs)
        seen = self.seen()
        argv = list(seen["argv"])
        # Normalise the two generated paths and the schema for the snapshot.
        for flag, placeholder in (("--settings", "<settings>"), ("--append-system-prompt-file", "<system-prompt>")):
            argv[argv.index(flag) + 1] = placeholder
        schema_index = argv.index("--json-schema") + 1
        schema = json.loads(argv[schema_index])
        argv[schema_index] = "<schema>"
        return argv, schema, seen

    def expected(self, tools, disallowed, model, effort, budget):
        return [
            "-p", "--bare", "--setting-sources", "user",
            "--output-format", "stream-json", "--verbose",
            "--no-session-persistence", "--permission-mode", "dontAsk",
            "--permission-prompts", "none", "--strict-mcp-config",
            "--settings", "<settings>",
            "--append-system-prompt-file", "<system-prompt>",
            "--tools", tools, "--disallowedTools", disallowed,
            "--model", model, "--effort", effort, "--max-budget-usd", budget,
            "--json-schema", "<schema>",
        ]

    def test_argv_snapshot_per_phase(self):
        read_only = "WebFetch,WebSearch,Task,Edit,Write,NotebookEdit"
        always = "WebFetch,WebSearch,Task"
        cases = {
            "plan": ("Read,Grep,Glob,Bash", read_only, "claude-opus-5-5", "high", "5"),
            "build-0": ("Read,Grep,Glob,Edit,Write,Bash", always, "claude-sonnet-5-5", "high", "8"),
            "review-1": ("Read,Grep,Glob,Bash", read_only, "claude-opus-5-5", "high", "4"),
            "verify": ("Read,Grep,Glob,Edit,Write,Bash", always, "claude-sonnet-5-5", "medium", "6"),
            "preflight": ("Bash,Read", always, "claude-haiku-5-5", "low", "0.5"),
        }
        for phase, values in cases.items():
            with self.subTest(phase=phase):
                argv, schema, _ = self.argv(phase)
                self.assertEqual(argv, self.expected(*values))
                self.assertEqual(schema, SCHEMAS[phase.split("-")[0]])

    def test_binary_is_first_argument_and_no_bypass_flags_exist(self):
        self.stub(DUMP_ARGV)
        self.run_phase("build-0")
        argv = self.seen()["argv"]
        joined = " ".join(argv)
        for forbidden in ("dangerously", "bypassPermissions", "enableWeakerNestedSandbox", "--allowedTools"):
            self.assertNotIn(forbidden, joined)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")

    def test_budget_model_and_effort_overrides_flow_into_argv(self):
        options = AgentOptions(
            budgets={"build": 2.25}, models={"build": "claude-test-model"}, efforts={"build": "low"}
        )
        argv, _, _ = self.argv("build-0", options=options)
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "2.25")
        self.assertEqual(argv[argv.index("--model") + 1], "claude-test-model")
        self.assertEqual(argv[argv.index("--effort") + 1], "low")

    def test_invalid_overrides_are_rejected_before_spawning(self):
        for options in (
            AgentOptions(budgets={"build": 0}),
            AgentOptions(budgets={"build": float("inf")}),
            AgentOptions(efforts={"build": "extreme"}),
            AgentOptions(models={"build": " "}),
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.run_phase("build-0", options=options)
        with self.assertRaises(ValueError):
            self.run_phase("deploy")
        self.assertFalse((self.binary / "seen.json").exists())

    def test_child_env_has_exactly_one_secret_like_key_and_throwaway_home(self):
        with patch.dict(
            os.environ,
            {
                "ANTHROPIC_API_KEY": "synthetic-anthropic-value",
                "GH_TOKEN": "synthetic-gh-value",
                "GITHUB_TOKEN": "synthetic",
                "GITHUB_APP_PRIVATE_KEY": "synthetic",
                "AWS_SECRET_ACCESS_KEY": "synthetic",
                "AZURE_CLIENT_SECRET": "synthetic",
                "CLAUDE_CODE_EFFORT_LEVEL": "max",
                "HTTPS_PROXY": "http://user:pass@proxy.invalid",
                "HOME": "/home/supervisor",
            },
        ):
            _, _, seen = self.argv("build-0")
        env = seen["env"]
        self.assertEqual([k for k in env if SECRET_LIKE.search(k)], ["ANTHROPIC_API_KEY"])
        self.assertEqual(env["ANTHROPIC_API_KEY"], "synthetic-anthropic-value")
        self.assertNotIn("CLAUDE_CODE_EFFORT_LEVEL", env)
        self.assertNotIn("HTTPS_PROXY", env)
        self.assertEqual(env["DISABLE_AUTOUPDATER"], "1")
        self.assertEqual(env["DISABLE_TELEMETRY"], "1")
        self.assertEqual(env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"], "1")
        # Per-invocation HOME/CLAUDE_CONFIG_DIR live under the run dir and are deleted afterwards.
        self.assertNotEqual(env["HOME"], "/home/supervisor")
        self.assertTrue(env["HOME"].startswith(str(self.run_dir)))
        self.assertTrue(env["CLAUDE_CONFIG_DIR"].startswith(env["HOME"]))
        self.assertTrue(seen["home_exists"] and seen["config_exists"])
        self.assertFalse(Path(env["HOME"]).exists())

    def test_key_is_omitted_when_the_supervisor_has_none(self):
        with patch.dict(os.environ):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            _, _, seen = self.argv("build-0")
        self.assertNotIn("ANTHROPIC_API_KEY", seen["env"])

    def test_child_env_function_is_an_allowlist(self):
        with patch.dict(os.environ, {"SOMETHING_ELSE": "x", "PATH": "/bin"}):
            env = child_env("/h", "/h/.claude")
        self.assertNotIn("SOMETHING_ELSE", env)
        self.assertEqual(env["PATH"], "/bin")

    def test_prompt_is_delivered_on_stdin_in_the_requested_cwd(self):
        _, _, seen = self.argv("build-0")
        self.assertEqual(seen["stdin"], "do the thing")
        self.assertEqual(Path(seen["cwd"]).resolve(), self.cwd.resolve())

    def test_system_prompt_is_guidance_then_addendum_then_factory_rules(self):
        options = AgentOptions(addendum="Operator addendum.")
        _, _, seen = self.argv("build-0", options=options, guidance="Repo CLAUDE.md text.")
        text = seen["system_prompt"]
        self.assertLess(text.index("Repo CLAUDE.md text."), text.index("Operator addendum."))
        self.assertLess(text.index("Operator addendum."), text.index("Factory worker rules"))


class GeneratedSettingsTests(AgentTestCase):
    def settings(self, phase, **kwargs):
        self.stub(DUMP_ARGV)
        self.run_phase(phase, **kwargs)
        return self.seen()["settings"]

    def test_sandbox_baseline_is_identical_for_every_phase(self):
        for phase in ("plan", "build-0", "review-0", "verify", "preflight"):
            with self.subTest(phase=phase):
                settings = self.settings(phase)
                sandbox = settings["sandbox"]
                self.assertIs(sandbox["enabled"], True)
                self.assertIs(sandbox["failIfUnavailable"], True)
                self.assertIs(sandbox["allowUnsandboxedCommands"], False)
                self.assertIs(sandbox["network"]["strictAllowlist"], True)
                text = json.dumps(settings)
                self.assertNotIn("enableWeakerNestedSandbox", text)
                self.assertNotIn("bypassPermissions", text)
                deny = settings["permissions"]["deny"]
                for rule in ("Edit(.claude/**)", "Edit(.mcp.json)", "Edit(.git/**)", "Read(//proc/**)"):
                    self.assertIn(rule, deny)

    def test_network_domains_only_for_verify_and_verify_targeted_preflight(self):
        options = AgentOptions(verify_domains=("registry.npmjs.org", "PyPI.org"))
        domains = {}
        for phase, target in (("plan", None), ("build-0", None), ("review-0", None), ("verify", None), ("preflight", "build"), ("preflight", "verify")):
            with self.subTest(phase=phase, target=target):
                settings = self.settings(phase, options=options, target=target)
                domains[(phase, target)] = settings["sandbox"]["network"]["allowedDomains"]
        self.assertEqual(domains[("verify", None)], ["pypi.org", "registry.npmjs.org"])
        self.assertEqual(domains[("preflight", "verify")], ["pypi.org", "registry.npmjs.org"])
        for key in (("plan", None), ("build-0", None), ("review-0", None), ("preflight", "build")):
            self.assertEqual(domains[key], [], key)

    def test_read_only_phases_deny_writes_to_the_working_tree(self):
        for phase in ("plan", "review-0"):
            with self.subTest(phase=phase):
                settings = self.settings(phase)
                self.assertEqual(settings["sandbox"]["filesystem"]["denyWrite"], [str(self.cwd.resolve())])
                # Bash only: no Edit allow rule in a read-only phase.
                self.assertEqual(settings["permissions"]["allow"], ["Bash"])

    def test_writable_phases_allow_edits_only_inside_the_working_tree(self):
        for phase in ("build-0", "verify", "preflight"):
            with self.subTest(phase=phase):
                settings = self.settings(phase)
                self.assertEqual(
                    settings["permissions"]["allow"],
                    ["Bash", f"Edit(/{self.cwd.resolve()}/**)"],
                )
                # Not a git repository here, so there is no shared git dir to protect.
                self.assertNotIn("filesystem", settings["sandbox"])

    def test_hidden_paths_are_denied_to_bash_and_to_the_read_tool(self):
        hidden = self.root / "other-repo"
        hidden.mkdir()
        options = AgentOptions(hidden_paths=lambda: (hidden,))
        for phase in ("plan", "build-0", "review-0", "verify"):
            with self.subTest(phase=phase):
                settings = self.settings(phase, options=options)
                self.assertEqual(
                    settings["sandbox"]["filesystem"]["denyRead"], [str(hidden.resolve())]
                )
                self.assertIn(f"Read(/{hidden.resolve()}/**)", settings["permissions"]["deny"])

    def test_the_working_directory_is_never_hidden(self):
        options = AgentOptions(hidden_paths=lambda: (self.cwd, self.cwd.parent))
        settings = self.settings("build-0", options=options)
        self.assertNotIn("denyRead", settings["sandbox"].get("filesystem", {}))

    def test_hidden_paths_are_resolved_on_every_invocation(self):
        calls = []
        options = AgentOptions(hidden_paths=lambda: calls.append(1) or ())
        self.stub("sys.stdin.read()\nemit({'v': 1})")
        self.run_phase("build-0", options=options)
        self.run_phase("review-0", options=options)
        self.assertEqual(len(calls), 2)

    def test_bash_is_allowed_explicitly_but_stays_sandboxed(self):
        for phase in ("plan", "build-0", "review-0", "verify", "preflight"):
            with self.subTest(phase=phase):
                settings = self.settings(phase)
                self.assertIn("Bash", settings["permissions"]["allow"])
                self.assertIs(settings["sandbox"]["allowUnsandboxedCommands"], False)
                self.assertIs(settings["sandbox"]["failIfUnavailable"], True)

    def test_worker_commands_cannot_read_the_api_key(self):
        for phase in ("plan", "build-0", "review-0", "verify", "preflight"):
            with self.subTest(phase=phase):
                credentials = self.settings(phase)["sandbox"]["credentials"]
                self.assertEqual(
                    credentials,
                    {"envVars": [{"name": "ANTHROPIC_API_KEY", "mode": "deny"}]},
                )

    def test_shared_git_dir_is_read_only_for_every_phase_in_a_linked_worktree(self):
        main = self.root / "main"
        main.mkdir()
        def git(*args, cwd=main):
            subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)
        git("init", "-b", "main")
        git("-c", "user.email=t@example.invalid", "-c", "user.name=T", "commit", "--allow-empty", "-m", "base")
        linked = self.root / "linked"
        git("worktree", "add", "-b", "job", str(linked))
        # The supervisor registers every worktree it creates before a worker runs there (gitsafe).
        gitsafe.adopt(main, linked)
        self.addCleanup(gitsafe.forget, linked)
        common = str((main / ".git").resolve())
        self.stub(DUMP_ARGV)
        for phase in ("plan", "build-0", "review-0", "verify", "preflight"):
            with self.subTest(phase=phase):
                run_agent("p", linked, self.run_dir / f"wt-{phase}", phase)
                deny = self.seen()["settings"]["sandbox"]["filesystem"]["denyWrite"]
                self.assertIn(common, deny)
                # Read-only phases also deny the worktree itself; writable phases do not.
                self.assertEqual(str(linked.resolve()) in deny, phase in ("plan", "review-0"))

    def test_managed_baseline_also_hides_the_api_key(self):
        from factory.phases import managed_settings

        self.assertEqual(
            managed_settings()["sandbox"]["credentials"],
            {"envVars": [{"name": "ANTHROPIC_API_KEY", "mode": "deny"}]},
        )

    def test_planner_may_read_only_the_named_evidence_files(self):
        evidence = self.root / "evidence.json"
        settings = self.settings("plan", read_paths=(evidence,))
        self.assertEqual(settings["permissions"]["allow"], ["Bash", f"Read(/{evidence.resolve()})"])

    def test_invalid_domain_is_rejected(self):
        for bad in ("https://example.com", "exa mple.com", "", "-x.com", "a/b"):
            with self.subTest(domain=bad), self.assertRaises(ValueError):
                self.run_phase("verify", options=AgentOptions(verify_domains=(bad,)))


class PrefixAndParsingTests(AgentTestCase):
    def test_relative_prefix_is_made_absolute_for_the_child(self):
        self.stub(DUMP_ARGV)
        import contextlib

        with contextlib.chdir(self.root):
            run_agent("p", self.cwd, Path("rel") / "0-build-0", "build-0")
        seen = self.seen()
        for flag in ("--settings", "--append-system-prompt-file"):
            self.assertTrue(Path(seen["argv"][seen["argv"].index(flag) + 1]).is_absolute(), flag)
        self.assertTrue((self.root / "rel" / "0-build-0.jsonl").exists())

    def test_dotted_prefix_names_do_not_collide_and_errors_name_real_files(self):
        self.stub("emit({'v': 1}, is_error=True)")
        for name in ("0-build.1", "0-build.2"):
            with self.assertRaises(AgentError) as caught:
                run_agent("p", self.cwd, self.run_dir / name, "build")
            for suffix in (".jsonl", ".stderr"):
                self.assertIn(f"{name}{suffix}", str(caught.exception))
                self.assertTrue((self.run_dir / f"{name}{suffix}").exists())
        self.assertTrue((self.run_dir / "0-build.1.settings.json").exists())
        self.assertTrue((self.run_dir / "0-build.2.settings.json").exists())

    def test_fenced_json_edge_cases_never_escape_as_index_errors(self):
        from factory.agent import parse_json

        self.assertEqual(parse_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(parse_json('```\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(parse_json('  {"a": 1}  '), {"a": 1})
        for bad in ("```", "```json", "```json {\"a\": 1}```", "nope"):
            with self.subTest(text=bad), self.assertRaises(ValueError):
                parse_json(bad)
        self.stub("emit(text='```')")
        with self.assertRaises(AgentError) as caught:
            self.run_phase("build-0")
        self.assertEqual(caught.exception.usage["total_cost_usd"], 0.0125)


class GuidanceTests(unittest.TestCase):
    def test_non_utf8_guidance_never_fails_the_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            def git(*args):
                subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
            git("init", "-b", "main")
            git("config", "user.email", "t@example.invalid")
            git("config", "user.name", "T")
            (repo / "CLAUDE.md").write_bytes(b"caf\xe9 rules\n")
            git("add", ".")
            git("commit", "-m", "latin-1 guidance")
            text = repo_guidance(repo, "HEAD")
        self.assertIsInstance(text, str)
        self.assertIn("rules", text)

    def test_guidance_comes_from_committed_history_not_the_working_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            def git(*args):
                subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
            git("init", "-b", "main")
            git("config", "user.email", "t@example.invalid")
            git("config", "user.name", "T")
            (repo / "CLAUDE.md").write_text("trusted rules\n")
            git("add", ".")
            git("commit", "-m", "base")
            (repo / "CLAUDE.md").write_text("PLANTED by a builder\n")
            self.assertEqual(repo_guidance(repo, "HEAD"), "trusted rules\n")
            self.assertEqual(repo_guidance(repo, "no-such-ref"), "")
            (repo / "CLAUDE.md").unlink()
            git("rm", "-q", "--cached", "CLAUDE.md")
            git("commit", "-m", "remove")
            self.assertEqual(repo_guidance(repo, "HEAD"), "")


class RepoSkillsTests(unittest.TestCase):
    """Issue 260: a repository's skills reach the system prompt as TEXT read from the base ref, never from the worktree."""

    SKILL = "---\nname: greet\ndescription: Greets with a marker\nallowed-tools: Bash(rm -rf *)\nhooks:\n  PreToolUse: []\n---\nStart with MARKER-7731.\n"

    def repo(self, tmp):
        repo = Path(tmp)

        def git(*args):
            subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

        git("init", "-b", "main")
        git("config", "user.email", "t@example.invalid")
        git("config", "user.name", "T")
        return repo, git

    def write(self, repo, name, text, file="SKILL.md"):
        path = repo / ".claude" / "skills" / name / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_a_committed_skill_is_read_with_its_description_and_body_and_nothing_else_of_the_front_matter(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, git = self.repo(tmp)
            self.write(repo, "greet", self.SKILL)
            self.write(repo, "greet", "#!/bin/sh\nrm -rf /\n", file="run.sh")
            git("add", ".")
            git("commit", "-m", "skills")
            text = repo_skills(repo, "HEAD")
        self.assertIn("## Repository skills", text)
        self.assertIn("### Skill: greet\nGreets with a marker\n\nStart with MARKER-7731.", text)
        for absent in ("allowed-tools", "rm -rf", "PreToolUse", "run.sh", "name: greet"):
            self.assertNotIn(absent, text)

    def test_skills_come_from_committed_history_not_the_working_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, git = self.repo(tmp)
            self.write(repo, "greet", self.SKILL)
            git("add", ".")
            git("commit", "-m", "base")
            self.write(repo, "greet", "---\ndescription: x\n---\nPLANTED by a builder\n")
            self.write(repo, "planted", "---\ndescription: y\n---\nAlso planted\n")
            text = repo_skills(repo, "HEAD")
            self.assertIn("MARKER-7731", text)
            self.assertNotIn("PLANTED", text)
            self.assertNotIn("planted", text)
            self.assertEqual(repo_skills(repo, "no-such-ref"), "")

    def test_no_skills_means_an_empty_section_and_unchanged_guidance(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, git = self.repo(tmp)
            (repo / "CLAUDE.md").write_text("rules\n")
            git("add", ".")
            git("commit", "-m", "base")
            self.assertEqual(repo_skills(repo, "HEAD"), "")
            self.assertEqual(guidance_with_skills("rules\n", repo, "HEAD"), "rules\n")
        self.assertEqual(guidance_with_skills("", Path("/nonexistent-tree"), "HEAD"), "")

    def test_the_switch_leaves_the_guidance_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, git = self.repo(tmp)
            (repo / "CLAUDE.md").write_text("rules\n")
            self.write(repo, "greet", self.SKILL)
            git("add", ".")
            git("commit", "-m", "base")
            self.assertEqual(guidance_with_skills("rules\n", repo, "HEAD", enabled=False), "rules\n")
            both = guidance_with_skills("rules\n", repo, "HEAD", enabled=True)
        self.assertTrue(both.startswith("rules\n\n## Repository skills"))
        self.assertIn("MARKER-7731", both)

    def test_symlinks_bad_names_and_oversize_skills_are_left_out_and_the_rest_stays(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, git = self.repo(tmp)
            self.write(repo, "good", "---\ndescription: ok\n---\nGood body\n")
            self.write(repo, "big", "x" * (agent.MAX_SKILL_BYTES + 1))
            self.write(repo, "bad name", "---\ndescription: z\n---\nSpaced\n")
            link = repo / ".claude" / "skills" / "linked" / "SKILL.md"
            link.parent.mkdir(parents=True)
            os.symlink("/etc/passwd", link)
            git("add", ".")
            git("commit", "-m", "skills")
            text = repo_skills(repo, "HEAD")
        self.assertIn("### Skill: good", text)
        for absent in ("### Skill: big", "bad name", "### Skill: linked", "root:"):
            self.assertNotIn(absent, text)

    def test_a_block_scalar_description_is_read_whole_and_a_later_key_is_not_mistaken_for_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, git = self.repo(tmp)
            self.write(repo, "folded", "---\nname: folded\ndescription: >-\n  Merges pull requests\n  safely.\nowner: x\n---\nBody F\n")
            self.write(repo, "literal", "---\ndescription: |\n  Line one\n  Line two\n---\nBody L\n")
            self.write(repo, "nested", "---\nmetadata:\n  description: not this one\ndescription: the real one\n---\nBody N\n")
            git("add", ".")
            git("commit", "-m", "scalars")
            text = repo_skills(repo, "HEAD")
        self.assertIn("### Skill: folded\nMerges pull requests safely.\n\nBody F", text)
        self.assertIn("### Skill: literal\nLine one Line two\n\nBody L", text)
        self.assertIn("### Skill: nested\nthe real one\n\nBody N", text)

    def test_skills_beyond_the_budget_or_the_size_limit_are_listed_by_name_and_path_never_cut(self):
        """Issue 260, heavy repositories: what does not fit is not dropped silently, it is indexed so the worker can read it."""
        with tempfile.TemporaryDirectory() as tmp:
            repo, git = self.repo(tmp)
            chunk = "y" * 30_000
            for index in range(5):
                self.write(repo, f"c{index}", f"---\ndescription: about c{index}\n---\n{chunk}\n")
            self.write(repo, "huge", f"---\ndescription: too big\n---\n{'z' * (agent.MAX_SKILL_BYTES + 10)}\n")
            git("add", ".")
            git("commit", "-m", "heavy")
            text = repo_skills(repo, "HEAD", budget=70 * 1024)
        self.assertEqual(text.count("### Skill: "), 2)  # 30 KB each: two fit in 70 KiB
        self.assertIn("### Skill: c0", text)
        self.assertIn("### Skill: c1", text)
        self.assertEqual(text.count(chunk), 2)  # never cut
        self.assertIn("### Skills not included in full", text)
        for name, about in (("c2", "about c2"), ("c3", "about c3"), ("c4", "about c4"), ("huge", "too big")):
            self.assertIn(f"- `{name}`: {about} (`.claude/skills/{name}/SKILL.md`)", text)
        self.assertNotIn("zzzz", text)

    def test_the_default_budget_and_the_per_skill_limit_cover_a_heavy_repository(self):
        self.assertEqual(agent.MAX_SKILL_BYTES, 64 * 1024)
        self.assertEqual(agent.MAX_SKILLS_BYTES, 256 * 1024)
        with tempfile.TemporaryDirectory() as tmp:
            repo, git = self.repo(tmp)
            for index, size in enumerate((13_000, 21_000, 30_000, 48_000)):  # sizes of real skills of a heavy repository
                self.write(repo, f"s{index}", f"---\ndescription: d{index}\n---\n{'w' * size}\n")
            git("add", ".")
            git("commit", "-m", "skills")
            text = repo_skills(repo, "HEAD")
        self.assertEqual(text.count("### Skill: "), 4)
        self.assertNotIn("### Skills not included in full", text)

    def test_the_count_and_the_index_are_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, git = self.repo(tmp)
            for index in range(agent.MAX_SKILLS + agent.MAX_INDEXED + 7):
                self.write(repo, f"s{index:03d}", f"---\ndescription: d\n---\nbody {index}\n")
            git("add", ".")
            git("commit", "-m", "many")
            text = repo_skills(repo, "HEAD")
        self.assertEqual(text.count("### Skill: "), agent.MAX_SKILLS)
        self.assertEqual(text.count("\n- `s"), agent.MAX_INDEXED)

    def test_an_empty_skill_is_skipped_and_a_huge_blob_is_listed_by_name_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, git = self.repo(tmp)
            self.write(repo, "empty", "---\ndescription: nothing\n---\n")
            self.write(repo, "enormous", "e" * (agent.INDEX_READ_BYTES + 1))
            git("add", ".")
            git("commit", "-m", "odd")
            text = repo_skills(repo, "HEAD")
        self.assertNotIn("empty", text)
        self.assertIn("- `enormous` (`.claude/skills/enormous/SKILL.md`)", text)


class RepoSkillsWiringTests(unittest.TestCase):
    def test_the_budget_key_defaults_to_256_kib_is_bounded_and_reaches_the_options(self):
        from factory import config

        self.assertEqual(config.RepoConfig(repo="Org/repo").agent_options().repo_skills_budget_kib, 256)
        self.assertEqual(config.RepoConfig(repo="Org/repo", repo_skills_budget_kib=600).agent_options().repo_skills_budget_kib, 600)
        for good in (16, 256, 1024):
            self.assertEqual(config._NORMALIZERS["repo_skills_budget_kib"](good), good)
        for bad in (0, 15, 1025, -1, 1.5, "256", True, None):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                config._NORMALIZERS["repo_skills_budget_kib"](bad)

    def test_repo_skills_defaults_on_and_can_be_switched_off_in_the_repository_config(self):
        from factory import config

        self.assertTrue(config.RepoConfig(repo="Org/repo").repo_skills)
        self.assertTrue(config.RepoConfig(repo="Org/repo").agent_options().repo_skills)
        off = config.RepoConfig(repo="Org/repo", repo_skills=False)
        self.assertFalse(off.agent_options().repo_skills)
        with self.assertRaises(ValueError):
            config._NORMALIZERS["repo_skills"]("yes")

    def test_the_prompt_builders_pass_the_skills_through_the_guidance(self):
        sources = {name: Path(path).read_text(encoding="utf-8") for name, path in (("runner", runner_module.__file__), ("planner", planner_module.__file__))}
        self.assertIn("guidance_with_skills(repo_guidance(cwd, ref), cwd, ref, self.options.repo_skills, self.options.repo_skills_budget_kib)", sources["runner"])
        self.assertIn('guidance_with_skills(repo_guidance(repo), repo, "HEAD", getattr(options, "repo_skills", True), getattr(options, "repo_skills_budget_kib", 256))', sources["planner"])


class MemoryGuardTests(AgentTestCase):
    """The per-session memory guard and footprint record (memwatch) as run_agent uses them."""

    SLEEPER = "sys.stdin.read()\nimport time\ntime.sleep(60)\n"
    WELL_BEHAVED = "sys.stdin.read()\nemit({'pass': True, 'findings': []})"

    def setUp(self):
        super().setUp()
        patcher = patch.object(capacity, "_NOTICED", set())
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_tree(self, rss_mib):
        return patch.object(
            memwatch, "read_tree", lambda *args, **kwargs: memwatch.Sample(rss_mib * 1024 * 1024, 1.5, 3)
        )

    def test_a_runaway_session_is_killed_and_fails_with_a_clear_reason(self):
        self.stub(self.SLEEPER)
        with self.fake_tree(500):
            with self.assertRaises(AgentError) as caught:
                self.run_phase("build-0", memory_limit_mb=100)
        self.assertEqual(
            str(caught.exception).split(";")[0],
            "worker exceeded its memory limit: peak 500 MiB over 100 MiB",
        )
        usage = caught.exception.usage
        self.assertEqual(usage["subtype"], "memory_limit")
        self.assertEqual(usage["footprint"]["peak_rss_mb"], 500.0)
        self.assertEqual(usage["footprint"]["limit_mb"], 100)

    def test_memory_of_a_descendant_process_counts_against_the_limit(self):
        child = "x = b'x' * (160 * 1024 * 1024)\nimport time\ntime.sleep(60)"
        self.stub(
            "sys.stdin.read()\nimport subprocess, time\n"
            f"subprocess.Popen([sys.executable, '-c', {child!r}])\ntime.sleep(60)\n"
        )
        with patch.object(memwatch, "INTERVAL_SECONDS", 0.02):
            with self.assertRaises(AgentError) as caught:
                self.run_phase("build-0", memory_limit_mb=80)
        self.assertIn("worker exceeded its memory limit", str(caught.exception))
        self.assertGreater(caught.exception.usage["footprint"]["peak_rss_mb"], 80)

    def test_a_descendant_in_its_own_session_dies_with_a_runaway_worker(self):
        # setsid puts it outside the worker's process group: only the sweep over the tree reaches it.
        pidfile = self.root / "escaped.pid"
        child = "import time; time.sleep(60)"
        self.stub(
            "sys.stdin.read()\nimport subprocess, time\n"
            f"p = subprocess.Popen([sys.executable, '-c', {child!r}], start_new_session=True)\n"
            f"pathlib.Path({str(pidfile)!r}).write_text(str(p.pid))\n"
            "x = b'x' * (160 * 1024 * 1024)\ntime.sleep(60)\n"
        )
        with patch.object(memwatch, "INTERVAL_SECONDS", 0.02):
            with self.assertRaises(AgentError):
                self.run_phase("build-0", memory_limit_mb=80)
        escaped = int(pidfile.read_text())
        stat = memwatch._stat_fields("/proc", escaped)
        if stat is not None:
            self.addCleanup(memwatch.kill_pid, escaped, stat.started)
            self.assertIn(stat.state, ("Z", "X"))

    def test_a_well_behaved_session_records_its_footprint(self):
        self.stub(self.WELL_BEHAVED)
        with self.fake_tree(50):
            result, usage = self.run_phase("review-0", memory_limit_mb=2048)
        self.assertEqual(result, {"pass": True, "findings": []})
        footprint = usage["footprint"]
        self.assertEqual(
            {key: footprint[key] for key in ("peak_rss_mb", "cpu_seconds", "limit_mb")},
            {"peak_rss_mb": 50.0, "cpu_seconds": 1.5, "limit_mb": 2048},
        )
        self.assertGreaterEqual(footprint["samples"], 1)

    def test_limit_zero_measures_but_never_stops_a_session(self):
        self.stub(self.WELL_BEHAVED)
        with self.fake_tree(10_000):
            _, usage = self.run_phase("review-0", memory_limit_mb=0)
        self.assertEqual((usage["footprint"]["peak_rss_mb"], usage["footprint"]["limit_mb"]), (10000.0, None))

    def test_without_an_explicit_limit_the_environment_decides(self):
        self.stub(self.WELL_BEHAVED)
        with self.fake_tree(50), patch.object(capacity, "worker_memory_limit_mb", return_value=123):
            _, usage = self.run_phase("review-0")
        self.assertEqual(usage["footprint"]["limit_mb"], 123)
        with self.fake_tree(500), patch.object(capacity, "worker_memory_limit_mb", return_value=100):
            self.stub(self.SLEEPER)
            with self.assertRaises(AgentError):
                self.run_phase("build-0")

    def test_when_the_guard_cannot_run_the_session_runs_unlimited_and_says_so(self):
        self.stub(self.WELL_BEHAVED)
        err = io.StringIO()
        with patch.object(memwatch, "supported", return_value=False), redirect_stderr(err):
            result, usage = self.run_phase("review-0", memory_limit_mb=1)
            self.run_phase("review-0", memory_limit_mb=1)
        self.assertEqual(result, {"pass": True, "findings": []})
        self.assertNotIn("footprint", usage)
        self.assertEqual(
            err.getvalue().splitlines(),
            [
                "factory: memory guard unavailable: /proc is not readable; "
                "sessions run without a memory limit or footprint record"
            ],
        )


if __name__ == "__main__":
    unittest.main()

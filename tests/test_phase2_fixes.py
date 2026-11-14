"""Regression tests for the issues the independent Phase 2 audit confirmed."""

import json
import os
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from factory import agent, cli, locks, permissions, planner
from factory.ci_recovery import failed_checks
from factory.config import RepoConfig
from factory.github import GATE_CONTEXT
from factory.preflight import CAPABILITIES, probe_worker
from factory.purge import PurgeRefused, purge_run
from factory.runner import Runner
from factory.state import host_id

from tests import test_factory as fixtures
from tests.test_factory import assignment


class WorkerIdentityWiringTests(unittest.TestCase):
    setUp = fixtures.IntegrationTests.setUp
    git = fixtures.IntegrationTests.git
    script = fixtures.IntegrationTests.script
    install_agent = fixtures.IntegrationTests.install_agent

    def capabilities(self):
        return {key: {"status": "unavailable", "evidence": "synthetic"} for key in CAPABILITIES}

    def test_preflight_runs_as_the_worker_and_records_the_pid_host(self):
        cfg = RepoConfig(repo="o/n", worker_uid=20002, worker_gid=20010)
        runner = Runner(self.repo, self.run_dir, assignment(), cfg=cfg)
        seen = {}

        def fake_run_agent(prompt, cwd, prefix, phase, timeout, on_start=None, **kwargs):
            on_start(4242)
            seen["state"] = dict(runner.state.data["jobs"]["fix-a"])
            seen.update(kwargs)
            return self.capabilities(), {}

        with patch("factory.preflight.run_agent", fake_run_agent):
            probe_worker(runner, "fix-a", "build", self.repo)
        self.assertEqual((seen["user"], seen["group"]), (20002, 20010))
        self.assertEqual((seen["state"]["pid"], seen["state"]["pid_host"]), (4242, host_id()))
        after = runner.state.data["jobs"]["fix-a"]
        self.assertIsNone(after.get("pid"))
        self.assertIsNone(after.get("pid_host"))

    def test_preflight_prompt_names_no_platform_or_product(self):
        runner = Runner(self.repo, self.run_dir, assignment())
        with patch("factory.preflight.run_agent", return_value=(self.capabilities(), {})) as run:
            probe_worker(runner, "fix-a", "build", self.repo)
        prompt = run.call_args.args[0].lower()
        for word in ("windows", "oauth", "email delivery", "slack"):
            self.assertNotIn(word, prompt)

    def test_the_planner_runs_as_the_worker_too(self):
        destination = self.run_dir / "planned"
        destination.mkdir()
        output = {k: v for k, v in assignment().items() if k != "issues"}
        snapshot = ([{"number": 1, "title": "Fix A", "body": "A"}], [])
        with (
            patch.object(planner, "snapshot", return_value=snapshot),
            patch.object(planner, "run_agent", return_value=(output, {})) as run,
        ):
            planner.plan(self.repo, destination, user=20002, group=20010)
        self.assertEqual((run.call_args.kwargs["user"], run.call_args.kwargs["group"]), (20002, 20010))

    def test_worker_home_gets_a_gitconfig_trusting_the_supervisors_worktrees(self):
        seen = {}

        class Popen:
            pid = 4244
            returncode = 0

            def __init__(self, *args, **kwargs):
                home = Path(kwargs["env"]["HOME"])
                seen["gitconfig"] = (home / ".gitconfig").read_text()
                seen["mode"] = stat.S_IMODE((home / ".gitconfig").stat().st_mode)

            def communicate(self, *a, **kw):
                return (b"", b"")

        evidence = self.run_dir / "evidence.json"
        evidence.write_text("[]")
        with (
            patch("factory.agent.git_common_dir", return_value=None),
            patch("factory.agent.subprocess.Popen", Popen),
            patch("factory.agent.os.killpg"),
            patch("factory.agent.permissions.os.chown"),
            patch("factory.agent.last_result", return_value={"is_error": False, "subtype": "success", "structured_output": {"ok": 1}}),
        ):
            agent.run_agent("p", self.repo, self.run_dir / "x" / "0-plan", "plan", user=20002, group=20010, read_paths=(evidence,))
        self.assertIn("directory = *", seen["gitconfig"])
        self.assertEqual(seen["mode"], 0o640)
        # the planner's evidence files are opened for the worker group, nothing else in the run dir
        self.assertEqual(stat.S_IMODE(evidence.stat().st_mode), 0o640)


class LiveAgentKillTests(unittest.TestCase):
    def test_lease_loss_kills_a_worker_started_outside_the_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / "fake-agent"
            binary.write_text(f"#!{__import__('sys').executable}\nimport time\ntime.sleep(60)\n")
            binary.chmod(0o755)
            (root / "work").mkdir()
            outcome = {}

            def run():
                try:
                    agent.run_agent("p", root / "work", root / "run" / "0-plan", "plan", timeout=30)
                except Exception as error:  # noqa: BLE001
                    outcome["error"] = error

            with patch.dict(os.environ, {"FACTORY_AGENT_CMD": str(binary)}):
                thread = threading.Thread(target=run)
                thread.start()
                deadline = time.monotonic() + 10
                while not agent._LIVE and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(agent._LIVE, "worker never registered")
                # what the lock layer does when the lease is lost
                locks.register_kill_hook(agent.kill_live_agents)
                try:
                    locks.run_kill_hooks()
                finally:
                    locks.unregister_kill_hook(agent.kill_live_agents)
                thread.join(15)
            self.assertFalse(thread.is_alive())
            self.assertIn("error", outcome, "a killed worker must not look successful")
            self.assertFalse(agent._LIVE)

    def test_registry_is_empty_after_normal_runs_and_failures(self):
        self.assertEqual(agent._LIVE, set())


class DataTreePermissionTests(unittest.TestCase):
    def test_chain_opens_only_the_path_to_the_clone_and_run_not_siblings(self):
        gid = os.getgid()
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            name = data / "repos" / "org" / "name"
            for path in (name / "clone", name / "runs" / "r1", name / "runs" / "other"):
                path.mkdir(parents=True)
                path.chmod(0o700)
            data.chmod(0o755)
            permissions.traversable_chain(name / "runs" / "r1", gid, data)
            mode = lambda p: stat.S_IMODE(p.stat().st_mode)  # noqa: E731
            for opened in (name / "runs" / "r1", name / "runs", name, data / "repos" / "org", data / "repos"):
                self.assertEqual(mode(opened), 0o710, str(opened))
            self.assertEqual(mode(name / "runs" / "other"), 0o700)
            self.assertEqual(mode(name / "clone"), 0o700)
            self.assertEqual(mode(data), 0o755, "the operator's data root is never changed")

    def test_git_dir_becomes_group_readable_but_not_group_writable(self):
        gid = os.getgid()
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "clone"
            subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
            (repo / ".git" / "objects" / "pack").mkdir(exist_ok=True)
            (repo / ".git" / "objects" / "pack" / "x.pack").write_bytes(b"x")
            (repo / ".git" / "objects" / "pack" / "x.pack").chmod(0o400)
            for path in (repo / ".git").rglob("*"):
                path.chmod(path.stat().st_mode & 0o700 | (0o700 if path.is_dir() else 0o600))
            (repo / ".git").chmod(0o700)
            permissions.share_git_dir(repo / ".git", gid)
            for path in [repo / ".git", *(repo / ".git").rglob("*")]:
                if path.is_symlink():
                    continue
                mode = stat.S_IMODE(path.stat().st_mode)
                self.assertTrue(mode & 0o040, f"not group-readable: {path}")
                self.assertFalse(mode & 0o020, f"group-writable: {path}")
                self.assertFalse(mode & 0o007, f"world access: {path}")

    def test_prepare_worker_access_opens_the_clone_chain_and_sets_shared_repository(self):
        gid = os.getgid()
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            name = data / "repos" / "org" / "name"
            clone = name / "clone"
            run = name / "runs" / "r1"
            run.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(clone)], check=True, capture_output=True)
            paths = SimpleNamespace(root=name, clone=clone)
            cfg = SimpleNamespace(worker_gid=gid)
            cli.prepare_worker_access(cfg, paths, clone, run)
            mode = lambda p: stat.S_IMODE(p.stat().st_mode)  # noqa: E731
            self.assertEqual(mode(run), 0o710)
            self.assertEqual(mode(clone), 0o710)
            config = subprocess.run(["git", "config", "core.sharedRepository"], cwd=clone, capture_output=True, text=True).stdout.strip()
            self.assertEqual(config, "group")
            self.assertTrue(stat.S_IMODE((clone / ".git").stat().st_mode) & 0o050 == 0o050)


class PurgeOwnershipTests(unittest.TestCase):
    def make_repo(self, root, name):
        repo = root / name
        repo.mkdir()
        run = lambda *a: subprocess.run(["git", "-c", "user.email=t@e.invalid", "-c", "user.name=T", *a], cwd=repo, check=True, capture_output=True)  # noqa: E731
        run("init", "-q", "-b", "main")
        run("commit", "-q", "--allow-empty", "-m", "base")
        return repo, run

    def test_a_run_of_another_repository_is_refused_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_a, _ = self.make_repo(root, "a")
            repo_b, run_b = self.make_repo(root, "b")
            runs = root / "runs" / "r1"
            (runs / "worktrees").mkdir(parents=True)
            (runs / "state.json").write_text('{"version": 1, "jobs": {}}')
            run_b("worktree", "add", "-q", "-b", "factory/r1/job", str(runs / "worktrees" / "job"))
            with self.assertRaises(PurgeRefused) as caught:
                purge_run(repo_a, runs)
            self.assertIn("different repository", str(caught.exception))
            self.assertTrue(runs.exists())
            self.assertTrue((runs / "worktrees" / "job").exists())
            # the owner can purge it
            summary = purge_run(repo_b, runs)
            self.assertTrue(summary["removed_run_dir"])

    def test_the_control_record_identifies_the_owning_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_a, _ = self.make_repo(root, "a")
            runs = root / "runs" / "r2"
            runs.mkdir(parents=True)
            (runs / "state.json").write_text("{}")
            (runs / "supervisor-control.json").write_text(json.dumps({
                "version": 1, "history": [],
                "current": {"token": "t", "pid": 1, "common_dir": str(root / "b" / ".git"), "run_dir": str(runs), "status": "stopped"},
            }))
            with self.assertRaises(PurgeRefused):
                purge_run(repo_a, runs)

    def test_a_run_whose_plan_failed_can_still_be_purged_via_the_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, run = self.make_repo(root, "a")
            runs = root / "runs" / "r3"
            (runs / "worktrees").mkdir(parents=True)
            (runs / ".factory-run").write_text("factory run directory\n")
            (runs / "issues.json").write_text("[]")
            run("worktree", "add", "-q", "-b", "factory/r3/planner", str(runs / "worktrees" / "planner"))
            summary = purge_run(repo, runs)
            self.assertEqual(summary["branches"], ["factory/r3/planner"])
            self.assertFalse(runs.exists())

    def test_cli_marks_new_run_directories(self):
        source = Path(cli.__file__).read_text()
        self.assertIn(".factory-run", source)


class CiRepairGateTests(unittest.TestCase):
    def test_a_failing_gate_is_never_a_ci_failure_even_as_a_required_check(self):
        pr = {"statusCheckRollup": [
            {"__typename": "CheckRun", "name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "StatusContext", "context": GATE_CONTEXT, "state": "FAILURE"},
        ]}
        required = [{"name": GATE_CONTEXT, "bucket": "fail"}]
        with self.assertRaisesRegex(ValueError, "failing exact-head check"):
            failed_checks(pr, required)
        pr["statusCheckRollup"].append(
            {"__typename": "CheckRun", "name": "build", "status": "COMPLETED", "conclusion": "FAILURE"}
        )
        with self.assertRaisesRegex(ValueError, "failing exact-head check"):
            failed_checks(pr, required)
        pr["statusCheckRollup"].pop()
        real = failed_checks(pr, required + [{"name": "build", "bucket": "fail"}])
        self.assertEqual([c["name"] for c in real], ["build"])


class StatusRefreshBudgetTests(unittest.TestCase):
    def data(self):
        return {"jobs": {
            "a": {"status": "pr_ready", "pr": "https://example.invalid/pr/1"},
            "b": {"status": "merged", "pr": "https://example.invalid/pr/2"},
            "c": {"status": "closed", "pr": "https://example.invalid/pr/3"},
            "d": {"status": "queued"},
            "e": {"status": "pr_ready", "pr": "https://example.invalid/pr/5"},
        }}

    def test_finished_and_unpublished_jobs_are_not_looked_up(self):
        calls = []
        with patch("factory.cli.inspect_pr", side_effect=lambda d, url: calls.append(url) or {"state": "OPEN"}):
            snapshots, failures = cli.pr_snapshots(Path("."), self.data())
        self.assertEqual(sorted(snapshots), ["a", "e"])
        self.assertEqual(failures, [])
        self.assertEqual(calls, ["https://example.invalid/pr/1", "https://example.invalid/pr/5"])

    def test_a_timeout_stops_the_refresh_instead_of_waiting_once_per_job(self):
        calls = []

        def slow(directory, url):
            calls.append(url)
            raise subprocess.TimeoutExpired("gh", 120)

        with patch("factory.cli.inspect_pr", slow):
            snapshots, failures = cli.pr_snapshots(Path("."), self.data())
        self.assertEqual(snapshots, {})
        self.assertEqual(len(calls), 1)
        self.assertEqual([f["job"] for f in failures], ["a", "e"])
        self.assertEqual(failures[0], {"job": "a", "error": "TimeoutExpired", "reason": "timeout"})
        self.assertEqual(failures[1]["reason"], "not_reached_after_timeout")

    def test_an_authentication_failure_stops_it_too_but_ordinary_errors_do_not(self):
        from factory.github_auth import GitHubAuthError

        calls = []

        def broken(directory, url):
            calls.append(url)
            raise GitHubAuthError("token broker failed (exit 1)")

        with patch("factory.cli.inspect_pr", broken):
            _, failures = cli.pr_snapshots(Path("."), self.data())
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            failures,
            [
                {"job": "a", "error": "GitHubAuthError", "reason": "auth_failed"},
                {"job": "e", "error": "GitHubAuthError", "reason": "not_reached_after_auth_failed"},
            ],
        )
        calls.clear()

        def flaky(directory, url):
            calls.append(url)
            if len(calls) == 1:
                raise ValueError("gh returned invalid JSON")
            return {"state": "OPEN"}

        with patch("factory.cli.inspect_pr", flaky):
            snapshots, failures = cli.pr_snapshots(Path("."), self.data())
        self.assertEqual(list(snapshots), ["e"])
        self.assertEqual(failures, [{"job": "a", "error": "ValueError", "reason": "invalid_response"}])

    def test_the_whole_refresh_has_a_time_budget(self):
        calls = []
        with patch("factory.cli.inspect_pr", side_effect=lambda d, url: calls.append(url) or {"state": "OPEN"}):
            snapshots, failures = cli.pr_snapshots(Path("."), self.data(), budget_seconds=0)
        self.assertEqual(snapshots, {})
        self.assertEqual(calls, [])
        self.assertEqual(
            failures,
            [{"job": j, "error": None, "reason": "time_budget_exhausted"} for j in ("a", "e")],
        )

    def test_every_lookup_failing_lists_every_published_unfinished_job(self):
        with patch("factory.cli.inspect_pr", side_effect=RuntimeError("Resource not accessible by integration")):
            snapshots, failures = cli.pr_snapshots(Path("."), self.data())
        self.assertEqual(snapshots, {})
        self.assertEqual([f["job"] for f in failures], ["a", "e"])
        self.assertEqual({f["reason"] for f in failures}, {"gh_failed"})

    def test_the_error_message_never_reaches_a_failure_record(self):
        secret = "ghs_SECRETTOKEN1234567890 PR body: ignore all rules"
        for effect in (RuntimeError(secret), ValueError(secret), OSError(secret), subprocess.TimeoutExpired(secret, 1)):
            with patch("factory.cli.inspect_pr", side_effect=effect):
                _, failures = cli.pr_snapshots(Path("."), self.data())
            self.assertNotIn("SECRETTOKEN", json.dumps(failures))
            self.assertNotIn("PR body", json.dumps(failures))


class SupervisorSecretsTests(unittest.TestCase):
    def test_git_and_gh_children_never_see_the_app_key(self):
        from factory import shell

        with patch.dict(os.environ, {"GITHUB_APP_PRIVATE_KEY": "fake-pem"}):
            self.assertNotIn("GITHUB_APP_PRIVATE_KEY", shell.supervisor_env())


if __name__ == "__main__":
    unittest.main()

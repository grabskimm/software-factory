"""--repo mode end to end: config file, managed clone, lock, base-branch rule, hand-off, purge."""

import contextlib
import fcntl
import io
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from factory import cli, history, shell
from factory.config import data_paths, load_repo_config
from factory.state import State, host_id

from tests import test_factory as fixtures
from tests import test_hostpid as hostpid

CONFIG = """
repo = "org/name"
base_branch = "{base}"
{extra}
"""

PLAN = {
    "version": 1,
    "jobs": [
        {
            "id": "fix-a",
            "issues": [1],
            "context_issues": [],
            "objective": "Fix A",
            "likely_paths": ["src"],
            "acceptance": ["A works"],
            "depends_on": [],
            "feature": False,
        }
    ],
    "dispositions": [{"issue": 1, "status": "job", "job": "fix-a", "reason": "Concrete fix"}],
}


class RepoModeTests(unittest.TestCase):
    setUp = fixtures.IntegrationTests.setUp
    git = fixtures.IntegrationTests.git
    script = fixtures.IntegrationTests.script
    install_agent = fixtures.IntegrationTests.install_agent
    gh_calls = fixtures.IntegrationTests.gh_calls
    gate_statuses = fixtures.IntegrationTests.gate_statuses

    def prepare(self, base="main", extra=""):
        """FACTORY_CONFIG/FACTORY_DATA temp dirs with a config file and a managed clone."""
        self.config_dir = self.root / "config"
        (self.config_dir / "repos").mkdir(parents=True)
        (self.config_dir / "repos" / "org__name.toml").write_text(CONFIG.format(base=base, extra=extra))
        self.data_dir = self.root / "data"
        patcher = patch.dict(
            os.environ,
            {
                "FACTORY_CONFIG": str(self.config_dir),
                "FACTORY_DATA": str(self.data_dir),
                "FAKE_ISSUES": str(self.root / "issues.json"),
                "FAKE_DEFAULT_BRANCH": "main",
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("FACTORY_GITHUB_APP_ID", "FACTORY_GITHUB_APP_SLUG", "FACTORY_GITHUB_APP_USER_ID", "FACTORY_TOKEN_BROKER", "FACTORY_LOCK_ACCOUNT_URL"):
            os.environ.pop(name, None)
        (self.root / "issues.json").write_text(json.dumps([{"number": 1, "title": "Fix A", "body": "A", "comments": [], "labels": [], "url": "u"}]))
        cfg = load_repo_config("org/name")
        self.paths = data_paths(cfg)
        # The managed clone is a clone of the origin; ensure_clone() finds it and does nothing.
        Path(self.paths.clone).parent.mkdir(parents=True)
        subprocess.run(["git", "clone", "-q", str(self.root / "origin.git"), str(self.paths.clone)], check=True, capture_output=True)
        for key, value in (("user.email", "factory@example.invalid"), ("user.name", "Factory Test")):
            subprocess.run(["git", "config", key, value], cwd=self.paths.clone, check=True)
        self.run_dir = self.paths.run_dir("r1")
        self.addCleanup(shell.set_auth, None)

    def planner_and_workers(self):
        self.install_agent(
            f"""
prompt = sys.stdin.read()
if 'planning a small GitHub issue factory' in prompt:
    emit(json.loads({json.dumps(json.dumps(PLAN))}))
elif 'independent reviewer' in prompt:
    emit({{'pass': True, 'findings': []}})
else:
    pathlib.Path('src/a.txt').write_text('fixed\\n')
    emit({{'summary': 'Fix A', 'blocked': False, 'blocker': '', 'tests': ['t'], 'feature': False}})
"""
        )

    def cli(self, *args, expect=0):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), patch("sys.argv", ["factory", *args]):
            try:
                cli.main()
                code = 0
            except SystemExit as exit_:
                code = exit_.code
        self.assertEqual(code, expect, err.getvalue())
        return out.getvalue(), err.getvalue()

    def test_plan_run_status_and_purge_through_the_managed_clone(self):
        self.prepare()
        self.planner_and_workers()
        base = ["--repo", "org/name", "--run-dir", str(self.run_dir)]
        out, _ = self.cli(*base, "plan", "--limit", "5")
        self.assertIn("Planned 1 issues in 1 jobs", out)
        plan = json.loads((self.run_dir / "plan.json").read_text())
        self.assertEqual(plan["artifact_roots"], [])
        self.assertEqual(plan["jobs"][0]["artifact_paths"], [])
        # `gh issue list` ran with -R for the named repository
        listing = [c for c in self.gh_calls() if c["args"][:2] == ["issue", "list"]][0]["args"]
        self.assertEqual(listing[listing.index("-R") + 1], "org/name")
        out, _ = self.cli(*base, "run", "--timeout", "30")
        self.assertIn("fix-a: pr_ready https://example.invalid/pr/1", out)
        # run output is status and PR only
        self.assertNotIn("Fix A", out.replace("fix-a", ""))
        create = [c for c in self.gh_calls() if c["args"][:2] == ["pr", "create"]][0]
        self.assertIn("Closes #1", create["body"])
        self.assertEqual(self.gate_statuses()[-1][0], "success")
        status, _ = self.cli(*base, "status", "--json", "--offline")
        report = json.loads(status)
        self.assertEqual(report["jobs"][0]["handoff_status"], "published")
        self.assertGreater(report["cost_usd"], 0)
        # purge removes the run, its worktrees and local branches; the remote branch stays
        branches = self.git("-C", str(self.paths.clone), "branch", "--list", "factory/r1/*")
        self.assertIn("factory/r1/fix-a", branches)
        out, _ = self.cli(*base, "purge")
        self.assertIn("Purged run r1", out)
        self.assertFalse(self.run_dir.exists())
        self.assertEqual(self.git("-C", str(self.paths.clone), "branch", "--list", "factory/r1/*"), "")
        remote = subprocess.run(["git", "branch", "--list", "factory/*"], cwd=self.root / "origin.git", capture_output=True, text=True).stdout
        self.assertIn("factory/r1/fix-a", remote)

    def test_the_history_ledger_records_the_run_and_survives_purge_and_purge_clone(self):
        self.prepare()
        self.planner_and_workers()
        base = ["--repo", "org/name", "--run-dir", str(self.run_dir)]
        self.cli(*base, "plan", "--limit", "5")
        self.cli(*base, "run", "--timeout", "30")
        ledger = history.Ledger(self.data_dir)
        events = ledger.read().events
        kinds = [event["kind"] for event in events]
        self.assertEqual((kinds[0], kinds[1], kinds[-1]), ("planned", "run_started", "run_finished"))
        self.assertEqual(events[-1]["result"], "stopped")
        planned = events[0]
        self.assertEqual((planned["issues"], planned["jobs"]), ([1], [{"id": "fix-a", "issues": [1]}]))
        self.assertGreater(planned["planner_cost_usd"], 0)
        jobs = [event for event in events if event["kind"] == "job"]
        self.assertEqual([event["status"] for event in jobs][0], "queued")  # attempt counted before the first phase
        self.assertEqual(jobs[-1]["status"], "pr_ready")
        self.assertEqual((jobs[-1]["issues"], jobs[-1]["attempt"]), ([1], 1))
        self.assertGreater(jobs[-1]["cost_usd"], 0)
        self.assertEqual({event["repo"] for event in events}, {"org/name"})
        self.assertEqual({event["run"] for event in events}, {"r1"})
        # purge removes the run directory; the history stays, with what each job last was
        self.cli(*base, "purge")
        self.assertFalse(self.run_dir.exists())
        events = ledger.read().events
        self.assertEqual(events[-1]["kind"], "purged")
        self.assertEqual((events[-1]["reason"], events[-1]["jobs"]), ("manual", [{"id": "fix-a", "status": "pr_ready"}]))  # the fake gh's PR URL is no github pull URL
        # purge-clone removes the clone and the repository's directory content, not the history
        out, _ = self.cli("--repo", "org/name", "purge-clone")
        self.assertEqual(out, "Removed clone\n")
        self.assertFalse(Path(self.paths.clone).exists())
        self.assertEqual(history.Ledger(self.data_dir).read().events, events)  # a restart reads the same
        folded = history.fold(events)[("org/name", "r1")]
        self.assertEqual((folded["outcome"], folded["purge_reason"], folded["issues"]), ("abandoned", "manual", [1]))
        # what the issue and the agents wrote never got in
        raw = b"".join(path.read_bytes() for path in (self.data_dir / "history").iterdir())
        for text in (b"Fix A", b"A works", b"Concrete fix", b"fixed"):
            self.assertNotIn(text, raw)
        self.assertEqual(sorted(p.name for p in (self.data_dir / "history").iterdir() if not p.name.startswith("events-")), ["history.lock"])

    def test_base_branch_must_be_the_default_branch_unless_allowed(self):
        self.prepare(base="release")
        self.planner_and_workers()
        base = ["--repo", "org/name", "--run-dir", str(self.run_dir)]
        _, err = self.cli(*base, "plan", expect=1)
        self.assertIn("default branch", err)
        self.assertFalse((self.run_dir / "plan.json").exists())

    def test_non_default_base_is_allowed_by_flag_and_says_issues_stay_open(self):
        self.prepare(base="release", extra="allow_non_default_base = true")
        self.git("branch", "release", "main")
        self.git("push", "origin", "release")
        subprocess.run(["git", "fetch", "-q", "origin"], cwd=self.paths.clone, check=True)
        self.planner_and_workers()
        base = ["--repo", "org/name", "--run-dir", str(self.run_dir)]
        _, err = self.cli(*base, "plan")
        self.assertIn("stay open", err)
        self.assertTrue((self.run_dir / "plan.json").exists())

    def test_run_dir_is_required_with_repo_and_unknown_repo_is_a_clear_error(self):
        self.prepare()
        _, err = self.cli("--repo", "org/name", "status", expect=1)
        self.assertIn("--run-dir is required", err)
        _, err = self.cli("--repo", "org/other", "--run-dir", str(self.run_dir), "status", expect=1)
        self.assertIn("org__other.toml", err)

    def test_a_second_supervisor_for_the_same_repo_is_refused(self):
        self.prepare()
        self.planner_and_workers()
        base = ["--repo", "org/name", "--run-dir", str(self.run_dir)]
        self.cli(*base, "plan")
        lock_file = Path(self.paths.root) / "factory.lock"
        with lock_file.open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _, err = self.cli(*base, "run", expect=1)
        self.assertIn("another factory supervisor holds the repository lock", err)
        self.assertEqual([c for c in self.gh_calls() if c["args"][:2] == ["pr", "create"]], [])

    def test_unexecuted_setup_commands_are_not_silent(self):
        self.prepare(extra='setup_commands = ["pnpm install --frozen-lockfile"]')
        self.planner_and_workers()
        base = ["--repo", "org/name", "--run-dir", str(self.run_dir)]
        _, err = self.cli(*base, "plan")
        self.assertIn("setup_commands is configured but this version does not run it", err)

    def test_half_configured_github_app_fails_instead_of_falling_back(self):
        self.prepare()
        os.environ["FACTORY_GITHUB_APP_ID"] = "12345"
        _, err = self.cli("--repo", "org/name", "--run-dir", str(self.run_dir), "plan", expect=1)
        self.assertIn("FACTORY_", err)
        self.assertEqual(self.gh_calls(), [])

    def test_purge_refuses_while_a_child_may_be_alive(self):
        self.prepare()
        self.planner_and_workers()
        base = ["--repo", "org/name", "--run-dir", str(self.run_dir)]
        self.cli(*base, "plan")
        self.cli(*base, "run", "--timeout", "30")
        sleeper = subprocess.Popen(["sleep", "30"], start_new_session=True)
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)
        state_path = self.run_dir / "state.json"
        data = json.loads(state_path.read_text())
        data["jobs"]["fix-a"]["pid"] = sleeper.pid
        state_path.write_text(json.dumps(data))
        _, err = self.cli(*base, "purge", expect=1)
        self.assertTrue(self.run_dir.exists(), err)

    def ran(self):
        """A planned and run repository: one pr_ready job in a state.json. Returns the common argv."""
        self.prepare()
        self.planner_and_workers()
        base = ["--repo", "org/name", "--run-dir", str(self.run_dir)]
        self.cli(*base, "plan")
        self.cli(*base, "run", "--timeout", "30")
        return base

    def job(self):
        return json.loads((self.run_dir / "state.json").read_text())["jobs"]["fix-a"]

    def set_job(self, **values):
        path = self.run_dir / "state.json"
        data = json.loads(path.read_text())
        data["jobs"]["fix-a"].update(values)
        path.write_text(json.dumps(data))

    def data_tree(self):
        """Every path below the data directory with the bytes of its files, for 'nothing was written'."""
        found = {}
        for current, names, files in os.walk(self.data_dir):
            for name in names:
                found[str(Path(current, name).relative_to(self.data_dir))] = None
            for name in files:
                path = Path(current, name)
                found[str(path.relative_to(self.data_dir))] = path.read_bytes() if ".git" not in path.parts else None
        return found

    def test_recover_marks_a_dead_supervisors_running_job_interrupted(self):
        base = self.ran()
        self.set_job(status="running", pid=hostpid.dead_pid(), pid_host=host_id(), error="agent said: PRIVATE-MARKER")
        out, err = self.cli(*base, "recover")
        self.assertEqual(out, "Recovered 1 interrupted job(s)\n")
        self.assertNotIn("PRIVATE-MARKER", out + err)
        self.assertEqual((self.job()["status"], self.job()["pid"], self.job()["pid_host"]), ("interrupted", None, None))
        report = json.loads(self.cli(*base, "status", "--json", "--offline")[0])
        self.assertEqual(report["jobs"][0]["status"], "interrupted")
        # Nothing is running any more, so a second call has nothing to turn.
        out, _ = self.cli(*base, "recover")
        self.assertEqual(out, "Recovered 0 interrupted job(s)\n")
        # `retry` starts from the state recover left; it needs nothing more than the lease.
        out, _ = self.cli(*base, "retry", "fix-a", "--reason", "the supervisor died")
        self.assertIn("fix-a queued", out)

    def test_status_json_shows_the_tokens_and_the_ledger_keeps_them_after_a_transition(self):
        base = self.ran()
        usage = [
            {
                "phase": "build",
                "total_cost_usd": 0.5,
                "usage": {"input_tokens": 10, "output_tokens": 40, "cache_creation_input_tokens": 90, "cache_read_input_tokens": 900},
            }
        ]
        self.set_job(status="running", pid=hostpid.dead_pid(), pid_host=host_id(), usage=usage)
        report = json.loads(self.cli(*base, "status", "--json", "--offline")[0])
        self.assertEqual(report["tokens"]["build"]["cache_read_input_tokens"], 900)
        self.assertEqual(report["tokens"]["build"]["cache_hit_rate"], 0.9)
        self.cli(*base, "recover")
        entries = [event for event in history.Ledger(self.data_dir).read().events if event["kind"] == "job"]
        self.assertEqual(entries[-1]["tokens"]["build"]["cache_read_input_tokens"], 900)
        self.assertEqual(entries[-1]["status"], "interrupted")

    def test_recover_retry_and_block_record_their_transitions_in_the_ledger_without_the_text(self):
        base = self.ran()
        self.set_job(status="running", pid=hostpid.dead_pid(), pid_host=host_id(), error="agent said: PRIVATE-MARKER")
        before = len(history.Ledger(self.data_dir).read().events)
        self.cli(*base, "recover")
        self.cli(*base, "retry", "fix-a", "--reason", "PRIVATE-REASON")
        self.cli(*base, "block", "fix-a", "--reason=PRIVATE-BLOCK-REASON")
        events = history.Ledger(self.data_dir).read().events[before:]
        self.assertEqual([(e["kind"], e["job"], e["status"]) for e in events], [("job", "fix-a", status) for status in ("interrupted", "queued", "blocked")])
        raw = b"".join(path.read_bytes() for path in (self.data_dir / "history").iterdir())
        for text in (b"PRIVATE-MARKER", b"PRIVATE-REASON", b"PRIVATE-BLOCK-REASON"):
            self.assertNotIn(text, raw)

    def issue_label_writes(self):
        return [
            c["args"] for c in self.gh_calls()
            if c["args"][:1] == ["api"] and "-X" in c["args"] and "/issues/" in " ".join(c["args"])
        ]

    def test_recover_moves_the_issue_to_blocked_even_if_the_run_is_never_resumed(self):
        base = self.ran()
        self.set_job(status="running", pid=hostpid.dead_pid(), pid_host=host_id())
        before = len(self.gh_calls())
        self.cli(*base, "recover")
        added = [a for a in self.issue_label_writes() if "labels[]=factory:blocked" in a]
        self.assertTrue(added, self.gh_calls()[before:])
        self.assertEqual(self.job()["status"], "interrupted")

    def test_block_marks_a_queued_job_blocked_with_a_reason_and_labels_its_issue(self):
        base = self.ran()
        self.set_job(status="queued", error=None, waiting_for="dependency merge: other")
        before = len(self.gh_calls())
        reason = "dependency other was closed without merging"
        out, _ = self.cli(*base, "block", "fix-a", f"--reason={reason}")
        self.assertEqual(out, "fix-a blocked\n")
        self.assertEqual((self.job()["status"], self.job()["error"], self.job()["waiting_for"]), ("blocked", reason, None))
        added = [a for a in self.issue_label_writes() if "labels[]=factory:blocked" in a]
        self.assertTrue(added, self.gh_calls()[before:])
        # Only a queued job: a blocked one (or a running or merged one) is refused and left alone.
        _, err = self.cli(*base, "block", "fix-a", "--reason=again", expect=1)
        self.assertIn("only a queued job may be blocked", err)
        self.assertEqual(self.job()["error"], reason)
        _, err = self.cli(*base, "block", "nope", "--reason=x", expect=1)
        self.assertIn("unknown job", err)
        # `retry` takes it back to the queue.
        self.cli(*base, "retry", "fix-a", "--reason", "the dependency was replaced")
        self.assertEqual(self.job()["status"], "queued")

    def test_block_needs_a_started_run_and_a_reason(self):
        self.prepare()
        self.planner_and_workers()
        base = ["--repo", "org/name", "--run-dir", str(self.run_dir)]
        self.cli(*base, "plan")
        _, err = self.cli(*base, "block", "fix-a", "--reason=x", expect=1)
        self.assertIn("run has not started", err)
        self.assertFalse((self.run_dir / "state.json").exists())
        self.cli(*base, "block", "fix-a", expect=2)

    def test_status_only_reads_and_never_writes_issue_labels(self):
        base = self.ran()
        self.set_job(status="pr_ready", pr="https://example.invalid/pr/1")
        before = len(self.issue_label_writes())
        calls = len(self.gh_calls())

        def forbidden(*args, **kwargs):
            raise AssertionError("status must not touch issue labels")

        snapshot = {"headRefOid": "fake", "baseRefName": "main", "isDraft": False, "state": "MERGED",
                    "statusCheckRollup": [], "mergeCommit": {"oid": "abc"}}
        with patch.object(cli, "IssueLabels", forbidden), patch.object(cli, "pr_snapshots", lambda *a, **k: ({"fix-a": snapshot}, [])) as live:
            for extra in (["--offline"], []):
                self.cli(*base, "status", "--json", *extra)
        self.assertEqual(len(self.issue_label_writes()), before)
        self.assertFalse([c for c in self.gh_calls()[calls:] if "/issues/" in " ".join(c["args"]) or c["args"][:2] == ["label", "create"]])

    def test_status_survives_failing_live_lookups_and_reports_them(self):
        base = self.ran()
        self.set_job(status="pr_ready", pr="https://example.invalid/pr/1")
        secret = "ghs_SECRETTOKEN1234567890"
        with patch.object(cli, "inspect_pr", side_effect=RuntimeError(f"{secret} PR body: x")):
            out, err = self.cli(*base, "status", "--json")
            text, text_err = self.cli(*base, "status")
        report = json.loads(out)
        self.assertEqual(report["live_refresh"], {"ok": False, "failed": [
            {"job": "fix-a", "error": "RuntimeError", "reason": "gh_failed"}]})
        self.assertIn("may be stale", report["jobs"][0]["next_action"])
        self.assertEqual(len([l for l in text.splitlines() if l.startswith("Warning")]), 1)
        for output in (out, err, text, text_err):
            self.assertNotIn(secret, output)
            self.assertNotIn("PR body", output)
        offline, _ = self.cli(*base, "status", "--json", "--offline")
        self.assertEqual(json.loads(offline)["live_refresh"], {"ok": True, "failed": []})

    def test_recover_with_nothing_running_recovers_zero(self):
        base = self.ran()
        out, _ = self.cli(*base, "recover")
        self.assertEqual(out, "Recovered 0 interrupted job(s)\n")
        self.assertEqual(self.job()["status"], "pr_ready")

    def test_recover_refuses_while_a_child_is_alive_and_changes_nothing(self):
        base = self.ran()
        sleeper = hostpid.sleeper(self)
        self.set_job(status="running", pid=sleeper.pid, pid_host=host_id())
        before = json.loads((self.run_dir / "state.json").read_text())
        _, err = self.cli(*base, "recover", expect=1)
        self.assertIn("still alive", err)
        self.assertEqual(json.loads((self.run_dir / "state.json").read_text()), before)
        # Once the child is gone the same command succeeds.
        hostpid.kill_group(sleeper.pid)
        sleeper.wait()
        out, _ = self.cli(*base, "recover")
        self.assertEqual(out, "Recovered 1 interrupted job(s)\n")

    def test_recover_needs_the_lease_for_a_pid_from_another_host(self):
        base = self.ran()
        self.set_job(status="running", pid=hostpid.dead_pid(), pid_host=hostpid.OTHER_HOST)
        before = json.loads((self.run_dir / "state.json").read_text())
        # A local flock is no lease, so the foreign pid cannot be called dead.
        _, err = self.cli(*base, "recover", expect=1)
        self.assertIn("repository lease", err)
        self.assertEqual(json.loads((self.run_dir / "state.json").read_text()), before)
        # With a real lease the previous holder's children died with it.
        with patch.object(cli, "holds_lease_for", lambda lock: lambda: True):
            out, _ = self.cli(*base, "recover")
        self.assertEqual(out, "Recovered 1 interrupted job(s)\n")
        self.assertEqual((self.job()["status"], self.job()["pid"]), ("interrupted", None))

    def test_recover_takes_the_repository_lease(self):
        base = self.ran()
        self.set_job(status="running", pid=hostpid.dead_pid(), pid_host=host_id())
        with (Path(self.paths.root) / "factory.lock").open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _, err = self.cli(*base, "recover", expect=1)
        self.assertIn("another factory supervisor holds the repository lock", err)
        self.assertEqual(self.job()["status"], "running")

    def test_recover_without_a_state_file_says_so_and_writes_nothing(self):
        self.prepare()
        self.planner_and_workers()
        base = ["--repo", "org/name", "--run-dir", str(self.run_dir)]
        before = self.data_tree()
        # The run directory does not exist at all ...
        out, _ = self.cli(*base, "recover")
        self.assertEqual(out, "Nothing to recover\n")
        self.assertFalse(self.run_dir.exists())
        self.assertEqual(self.data_tree(), before)
        # ... or is planned but was never run.
        self.cli(*base, "plan")
        self.assertFalse((self.run_dir / "state.json").exists())
        before = self.data_tree()
        out, _ = self.cli(*base, "recover")
        self.assertEqual(out, "Nothing to recover\n")
        self.assertEqual(self.data_tree(), before)

    def test_recover_checks_for_the_state_again_under_the_lease(self):
        # A purge may finish between the check before the lease and the lease itself.
        from argparse import Namespace

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.recover_command(Namespace(command="recover"), self.root / "gone", lambda: False)
        self.assertEqual(out.getvalue(), "Nothing to recover\n")

    def test_recover_needs_the_plan_once_there_is_a_state(self):
        base = self.ran()
        (self.run_dir / "plan.json").unlink()
        self.cli(*base, "recover", expect=1)

    def test_recover_does_not_need_the_clone_and_never_creates_it(self):
        base = self.ran()
        self.set_job(status="running", pid=hostpid.dead_pid(), pid_host=host_id())
        shutil.rmtree(self.paths.clone)
        with patch.object(cli, "ensure_clone", side_effect=AssertionError("must not clone")):
            out, _ = self.cli(*base, "recover")
        self.assertEqual(out, "Recovered 1 interrupted job(s)\n")
        self.assertFalse(Path(self.paths.clone).exists())

    def test_recover_and_purge_clone_need_no_worker_access_in_a_container_with_a_worker_identity(self):
        # The managed container always sets worker.uid/gid; the access preparation walks
        # a run directory, which purge-clone has not got and recover may not have either.
        self.addCleanup(os.umask, os.umask(0o022))  # a worker identity makes the umask private
        self.prepare(extra="[worker]\nuid = 10002\ngid = 10010")
        base = ["--repo", "org/name", "--run-dir", str(self.run_dir)]
        refuse = patch.object(cli, "prepare_worker_access", side_effect=AssertionError("not needed here"))
        with refuse:
            self.assertEqual(self.cli(*base, "recover")[0], "Nothing to recover\n")
        self.run_dir.mkdir(parents=True)
        (self.run_dir / "plan.json").write_text(json.dumps(fixtures.assignment()))
        State(self.run_dir, fixtures.assignment()).update(
            "fix-a", status="running", pid=hostpid.dead_pid(), pid_host=host_id()
        )
        with refuse:
            self.assertEqual(self.cli(*base, "recover")[0], "Recovered 1 interrupted job(s)\n")
        # The commands that do start work for a worker still prepare its access.
        with patch.object(cli, "prepare_worker_access") as prepared:
            self.cli(*base, "retry", "fix-a", "--reason", "again")
        prepared.assert_called_once()
        shutil.rmtree(self.run_dir)
        with refuse:
            self.assertEqual(self.cli("--repo", "org/name", "purge-clone")[0], "Removed clone\n")

    def test_purge_clone_removes_the_clone_only_once_nothing_is_left(self):
        base = self.ran()
        other = Path(self.data_dir) / "repos" / "org" / "other"
        (other / "clone").mkdir(parents=True)
        (other / "clone" / ".git").mkdir()
        repo = ["--repo", "org/name"]
        # A run directory remains: refused, and the clone is untouched.
        _, err = self.cli(*repo, "purge-clone", expect=1)
        self.assertIn("run directory remains", err)
        self.assertTrue((Path(self.paths.clone) / ".git").is_dir())
        self.cli(*base, "purge")
        with patch.object(cli, "ensure_clone", side_effect=AssertionError("must not clone")):
            out, err = self.cli(*repo, "purge-clone")
            self.assertEqual((out, err), ("Removed clone\n", ""))
            self.assertFalse(Path(self.paths.clone).exists())
            self.assertEqual(self.cli(*repo, "purge-clone")[0], "Nothing to remove\n")
        self.assertTrue((other / "clone" / ".git").is_dir())

    def test_purge_clone_for_a_repository_that_was_never_used_creates_nothing(self):
        self.prepare()
        shutil.rmtree(self.paths.root)
        before = self.data_tree()
        out, _ = self.cli("--repo", "org/name", "purge-clone")
        self.assertEqual(out, "Nothing to remove\n")
        self.assertEqual(self.data_tree(), before)
        self.assertFalse(Path(self.paths.root).exists())

    def test_purge_clone_refuses_a_linked_worktree_and_a_symlinked_clone(self):
        self.prepare()
        repo = ["--repo", "org/name"]
        elsewhere = self.root / "elsewhere"
        subprocess.run(["git", "worktree", "add", "-q", "-b", "side/x", str(elsewhere), "origin/main"], cwd=self.paths.clone, check=True)
        _, err = self.cli(*repo, "purge-clone", expect=1)
        self.assertIn("linked worktree", err)
        self.assertTrue((Path(self.paths.clone) / ".git").is_dir())
        subprocess.run(["git", "worktree", "remove", "--force", str(elsewhere)], cwd=self.paths.clone, check=True)
        real = self.root / "real-clone"
        Path(self.paths.clone).rename(real)
        Path(self.paths.clone).symlink_to(real, target_is_directory=True)
        _, err = self.cli(*repo, "purge-clone", expect=1)
        self.assertIn("symlink", err)
        self.assertTrue((real / ".git").is_dir())

    def test_purge_refuses_a_run_that_was_used_after_the_janitor_looked(self):
        from factory.purge import newest_activity_ns

        self.prepare()
        self.planner_and_workers()
        base = ["--repo", "org/name", "--run-dir", str(self.run_dir)]
        self.cli(*base, "plan", "--limit", "5")
        seen = newest_activity_ns(self.run_dir)
        _, err = self.cli(*base, "purge", f"--unchanged-since={seen - 1}", expect=1)
        self.assertIn("used after it was judged finished", err)
        self.assertTrue(self.run_dir.exists())
        out, _ = self.cli(*base, "purge", f"--unchanged-since={seen}")
        self.assertIn("Purged run r1", out)
        self.assertFalse(self.run_dir.exists())

    def test_purge_works_for_a_run_whose_clone_step_failed(self):
        """plan creates the run directory first; a failed clone left it without a clone to purge through."""
        self.prepare()
        shutil.rmtree(self.paths.clone)
        self.run_dir.mkdir(parents=True)
        (self.run_dir / ".factory-run").write_text("factory run directory\n")
        out, _ = self.cli("--repo", "org/name", "--run-dir", str(self.run_dir), "purge")
        self.assertIn("Purged run r1", out)
        self.assertFalse(self.run_dir.exists())
        self.assertFalse(Path(self.paths.clone).exists())  # purge must not clone it back

    # ---- status labels at purge
    def seed_run(self, name="r1", status="queued", plan=None, **job):
        """A run directory with a plan and (unless status is None) a state, made by hand."""
        run_dir = self.paths.run_dir(name)
        run_dir.mkdir(parents=True)
        (run_dir / ".factory-run").write_text("factory run directory\n")
        selected = plan or fixtures.assignment()
        (run_dir / "plan.json").write_text(json.dumps(selected))
        if status is not None:
            jobs = {item["id"]: {"status": status, **job} for item in selected["jobs"]}
            (run_dir / "state.json").write_text(json.dumps({"jobs": jobs}))
        return run_dir

    def put_labels(self, **by_number):
        store = {k.removeprefix("n"): v for k, v in by_number.items()}
        (self.root / "issue-labels.json").write_text(json.dumps(store))

    def labels_of(self, number=1):
        path = self.root / "issue-labels.json"
        return json.loads(path.read_text()).get(str(number), []) if path.exists() else []

    def purge(self, name="r1", **kwargs):
        run_dir = self.paths.run_dir(name)
        return self.cli("--repo", "org/name", "--run-dir", str(run_dir), "purge", **kwargs)

    def pull_requests(self, **by_url):
        """gh pr view answers from this table: url -> state (the shared fake has no pr view)."""
        return patch(
            "factory.issue_labels.inspect_pr",
            side_effect=lambda repo, url: {"state": by_url[url]},
        )

    def test_purge_releases_the_status_label_of_every_job_without_a_pull_request(self):
        self.prepare()
        for status in ("queued", "running", "waiting_review", "failed", "blocked", "interrupted", "closed", None):
            with self.subTest(status=status):
                self.seed_run(status=status)
                self.put_labels(n1=["bug", "factory:planned", "factory:blocked"])
                out, err = self.purge()
                self.assertEqual(out, "Purged run r1: 0 worktrees, 0 branches, run dir removed=True\n")
                self.assertEqual(err, "")
                self.assertEqual(self.labels_of(), ["bug"])
                self.assertFalse(self.paths.run_dir("r1").exists())

    def test_purge_releases_what_the_plan_only_investigated_or_blocked(self):
        self.prepare()
        plan = fixtures.assignment()
        for number, status in ((2, "investigation"), (3, "blocked"), (4, "covered"), (5, "investigation")):
            plan["issues"].append({"number": number, "title": f"T{number}", "body": "b"})
            plan["dispositions"].append({"issue": number, "status": status, "job": None, "reason": "r"})
        self.seed_run(plan=plan, status="pr_ready", pr="https://example.invalid/pr/1")
        # An investigated issue is labelled blocked; issue 5 still carries the planned label an
        # older version gave it, and the purge releases that one as well.
        self.put_labels(
            n1=["factory:pr-open"],
            n2=["factory:blocked"],
            n3=["factory:blocked"],
            n4=["factory:planned"],
            n5=["factory:planned"],
        )
        with self.pull_requests(**{"https://example.invalid/pr/1": "OPEN"}):
            self.purge()
        # The open pull request keeps its label; the issues that never had a job lose theirs.
        # A covered issue never got a label from Factory, so whatever it carries is not touched.
        self.assertEqual(
            [self.labels_of(n) for n in (1, 2, 3, 4, 5)],
            [["factory:pr-open"], [], [], ["factory:planned"], []],
        )

    def test_purge_follows_the_live_pull_request_of_a_published_job(self):
        self.prepare()
        url = "https://example.invalid/pr/1"
        for status in ("pr_ready", "pr_incomplete"):
            for live, expected in (
                ("MERGED", ["factory:merged"]),
                ("CLOSED", []),
                ("OPEN", ["factory:pr-open"]),
            ):
                with self.subTest(status=status, live=live):
                    self.seed_run(status=status, pr=url)
                    self.put_labels(n1=["factory:pr-open"])
                    with self.pull_requests(**{url: live}):
                        out, err = self.purge()
                    self.assertEqual(self.labels_of(), expected)
                    self.assertEqual((out.startswith("Purged run r1: "), err), (True, ""))

    def test_purge_keeps_the_label_of_a_published_job_it_cannot_look_up(self):
        self.prepare()
        self.seed_run(status="pr_ready", pr="https://example.invalid/pr/1")
        self.put_labels(n1=["factory:ci-failed"])
        # The shared fake gh has no `pr view`, like a gh that cannot reach GitHub.
        out, _ = self.purge()
        self.assertTrue(out.startswith("Purged run r1: "))
        self.assertEqual(self.labels_of(), ["factory:ci-failed"])

    def test_purge_of_a_merged_job_sets_merged_without_asking_github(self):
        self.prepare()
        self.seed_run(status="merged", pr="https://example.invalid/pr/1")
        self.put_labels(n1=["factory:pr-open"])
        with patch("factory.issue_labels.inspect_pr", side_effect=AssertionError("no lookup")):
            self.purge()
        self.assertEqual(self.labels_of(), ["factory:merged"])

    def test_purge_leaves_the_label_of_an_issue_that_another_run_lists(self):
        self.prepare()
        other = fixtures.assignment()
        other["issues"].append({"number": 2, "title": "T2", "body": "b"})
        other["jobs"][0]["issues"] = [1, 2]
        other["dispositions"].append({"issue": 2, "status": "job", "job": "fix-a", "reason": "r"})
        self.seed_run("r1", status="failed")
        self.seed_run("r2", status="running", plan=other)
        self.put_labels(n1=["factory:building"], n2=["factory:building"])
        self.purge("r1")
        self.assertEqual(self.labels_of(1), ["factory:building"])
        self.assertEqual(self.issue_label_writes(), [])
        # Once nothing else lists them, purging the last run releases both.
        self.purge("r2")
        self.assertEqual((self.labels_of(1), self.labels_of(2)), ([], []))

    def test_a_refused_purge_changes_no_label(self):
        base = self.ran()
        self.assertEqual(self.labels_of(), ["factory:pr-open"])
        writes = len(self.issue_label_writes())
        calls = len(self.gh_calls())
        sleeper = subprocess.Popen(["sleep", "30"], start_new_session=True)
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)
        self.set_job(status="failed", pid=sleeper.pid)
        self.cli(*base, "purge", expect=1)
        self.set_job(status="failed", pid=None)
        from factory.purge import newest_activity_ns

        stale = newest_activity_ns(self.run_dir) - 1
        _, err = self.cli(*base, "purge", f"--unchanged-since={stale}", expect=1)
        self.assertIn("used after it was judged finished", err)
        self.assertTrue(self.run_dir.exists())
        self.assertEqual(self.labels_of(), ["factory:pr-open"])
        self.assertEqual(len(self.issue_label_writes()), writes)
        self.assertEqual(len(self.gh_calls()), calls)

    def test_purge_of_a_run_that_only_has_a_marker_or_only_a_plan(self):
        self.prepare()
        run_dir = self.paths.run_dir("r1")
        run_dir.mkdir(parents=True)
        (run_dir / ".factory-run").write_text("factory run directory\n")
        self.put_labels(n1=["factory:planned"])
        out, err = self.purge()
        self.assertEqual((out.startswith("Purged run r1: "), err), (True, ""))
        self.assertEqual(self.gh_calls(), [])
        self.assertEqual(self.labels_of(), ["factory:planned"])  # nothing says which issues it had
        # A plan with no state: no job ever started, so every issue is released.
        self.seed_run(status=None)
        out, err = self.purge()
        self.assertEqual((out.startswith("Purged run r1: "), err), (True, ""))
        self.assertEqual(self.labels_of(), [])

    def test_purge_releases_the_label_of_a_run_whose_clone_step_failed(self):
        self.prepare()
        shutil.rmtree(self.paths.clone)
        self.seed_run(status="queued")
        self.put_labels(n1=["factory:planned"])
        out, _ = self.purge()
        self.assertTrue(out.startswith("Purged run r1: "))
        self.assertEqual(self.labels_of(), [])
        self.assertFalse(Path(self.paths.clone).exists())  # purge must not clone it back

    def test_a_failing_label_release_never_fails_the_purge_and_prints_one_line(self):
        from factory import issue_labels

        issue_labels.reset_report()
        self.addCleanup(issue_labels.reset_report)
        self.prepare()
        self.seed_run(status="queued")
        self.put_labels(n1=["factory:planned"])
        os.environ["FAKE_ISSUE_API_FAIL"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_ISSUE_API_FAIL", None)
        out, err = self.purge()
        self.assertEqual(out, "Purged run r1: 0 worktrees, 0 branches, run dir removed=True\n")
        self.assertEqual(err, "factory: issue label sync failed (CalledProcessError)\n")
        self.assertFalse(self.paths.run_dir("r1").exists())

    def test_purge_clone_takes_the_repository_lease(self):
        self.prepare()
        with (Path(self.paths.root) / "factory.lock").open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _, err = self.cli("--repo", "org/name", "purge-clone", expect=1)
        self.assertIn("another factory supervisor holds the repository lock", err)
        self.assertTrue((Path(self.paths.clone) / ".git").is_dir())

    def test_purge_clone_takes_no_run_dir_and_every_other_command_still_needs_one(self):
        self.prepare()
        _, err = self.cli("--repo", "org/name", "--run-dir", str(self.run_dir), "purge-clone", expect=1)
        self.assertIn("takes no --run-dir", err)
        self.assertTrue((Path(self.paths.clone) / ".git").is_dir())
        _, err = self.cli("purge-clone", expect=1)
        self.assertIn("needs --repo", err)
        for name in ("status", "drain", "plan", "run", "recover", "purge", "retry"):
            with self.subTest(name):
                tail = [name, "fix-a", "--reason", "x"] if name == "retry" else [name]
                _, err = self.cli("--repo", "org/name", *tail, expect=1)
                self.assertIn("--run-dir is required with --repo", err)

    def test_the_new_commands_are_in_the_help_text(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch("sys.argv", ["factory", "--help"]), self.assertRaises(SystemExit):
            cli.main()
        text = " ".join(out.getvalue().split())  # argparse wraps lines
        for expected in ("recover", "purge-clone", "interrupted", "no --run-dir"):
            self.assertIn(expected, text)


if __name__ == "__main__":
    unittest.main()


class HiddenPathsWiringTests(unittest.TestCase):
    def options(self, **config):
        from argparse import Namespace

        from factory.config import RepoConfig

        return cli.agent_options(Namespace(), RepoConfig(**config))

    def test_workers_of_a_managed_repo_with_a_separate_identity_get_the_hiding_callback(self):
        options = self.options(repo="org/name", worker_uid=10002, worker_gid=10010)
        self.assertTrue(callable(options.hidden_paths))

    def test_no_hiding_without_a_separate_identity_or_in_local_mode(self):
        self.assertIsNone(self.options(repo="org/name").hidden_paths)
        self.assertIsNone(self.options().hidden_paths)


class GitCommonDirFailureTests(unittest.TestCase):
    """A git failure while finding the common directory is worded the way purge words it."""

    def stderr_of(self, failure):
        """What ``main`` prints when ``git_common_dir``'s git call raises ``failure``."""
        from argparse import Namespace

        out = io.StringIO()
        with (
            patch("factory.cli.command", side_effect=failure),
            patch("factory.cli.build_parser") as parser,
            patch("factory.cli.run", side_effect=lambda args: cli.git_common_dir(Path("/nonexistent"))),
            contextlib.redirect_stderr(out),
            self.assertRaises(SystemExit),
        ):
            parser.return_value.parse_args.return_value = Namespace()
            cli.main()
        return out.getvalue()

    def test_a_nonzero_exit_is_a_git_failed_line_with_a_capped_one_line_detail(self):
        from factory import operations

        noisy = "fatal: not a git repository\n" + "x" * 1000
        error = subprocess.CalledProcessError(128, ["git", "rev-parse", "SECRET-ARG"], stderr=noisy)
        stderr = self.stderr_of(error)
        line = stderr.splitlines()[0]
        self.assertTrue(line.startswith("factory: git rev-parse --git-common-dir failed: fatal: not a git repository x"))
        self.assertLessEqual(len(line), len("factory: git rev-parse --git-common-dir failed: ") + 300)
        self.assertNotIn("Command '", stderr)
        self.assertNotIn("SECRET-ARG", stderr)
        self.assertEqual(operations.refusal_cause(stderr.encode()), "git_failed")

    def test_a_nonzero_exit_without_stderr_names_the_status(self):
        from factory import operations

        stderr = self.stderr_of(subprocess.CalledProcessError(128, ["git"], stderr=""))
        self.assertEqual(stderr, "factory: git rev-parse --git-common-dir failed: 128\n")
        self.assertEqual(operations.refusal_cause(stderr.encode()), "git_failed")

    def test_a_hang_or_a_missing_git_is_a_could_not_run_line(self):
        from factory import operations

        for failure, name in (
            (subprocess.TimeoutExpired(["git", "SECRET-ARG"], 120), "TimeoutExpired"),
            (FileNotFoundError(2, "No such file", "git"), "FileNotFoundError"),
            (PermissionError(13, "denied", "/x"), "PermissionError"),
        ):
            with self.subTest(failure=name):
                stderr = self.stderr_of(failure)
                self.assertEqual(stderr, f"factory: git rev-parse could not run: {name}\n")
                self.assertEqual(operations.refusal_cause(stderr.encode()), "git_failed")

    def test_success_still_returns_the_absolute_common_directory(self):
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            self.assertEqual(cli.git_common_dir(root), root / ".git")
            sub = root / "sub"
            sub.mkdir()
            self.assertEqual(cli.git_common_dir(sub), root / ".git")

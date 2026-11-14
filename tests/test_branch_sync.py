"""The supervisor brings an existing job branch up to the current base before a builder session."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from factory.locks import LockLost
from factory.runner import Runner

from tests import agentstub, test_cli_preflight
from tests.test_factory import FAKE_GH, assignment
from tests.test_github import passing_pr

JOB = "fix-a"

# Builder: records whether the sibling's file and conflict markers were present when it started,
# then edits src/a.txt (resolving a conflict when the prompt reports one).
AGENT = """
prompt = sys.stdin.read()
here = pathlib.Path(__file__).parent
if 'independent reviewer' in prompt:
    emit({'pass': True, 'findings': []})
else:
    text = pathlib.Path('src/a.txt').read_text()
    (here / 'builder-saw').write_text(json.dumps({
        'sibling': pathlib.Path('src/b.txt').exists(),
        'markers': '<<<<<<<' in text,
        'prompt_mentions_conflict': 'conflict' in prompt and 'src/a.txt' in prompt,
        'prompt_forbids_git': 'do not fetch, rebase' in prompt,
    }))
    if (here / 'leave-markers').exists() or (here / 'no-change').exists():
        pass
    elif '<<<<<<<' in text:
        pathlib.Path('src/a.txt').write_text('resolved\\n')
    else:
        pathlib.Path('src/a.txt').write_text('fixed\\n')
    emit({'summary': 'Fix A', 'blocked': False, 'blocker': '', 'tests': ['ok'], 'feature': False})
"""


class HeldLock:
    """A repository lock that can be lost on demand."""

    def __init__(self):
        self.lost = False

    def assert_held(self):
        if self.lost:
            raise LockLost("lock lost")


class BranchSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.run_dir = self.root / "pilot"
        self.run_dir.mkdir()
        self.binary = self.root / "bin"
        self.binary.mkdir()
        self.origin = self.root / "origin.git"
        self.git("init", "-b", "main")
        self.git("config", "user.email", "factory@example.invalid")
        self.git("config", "user.name", "Factory Test")
        (self.repo / "src").mkdir()
        (self.repo / "src/a.txt").write_text("before\n")
        self.git("add", ".")
        self.git("commit", "-m", "base")
        subprocess.run(["git", "init", "--bare", str(self.origin)], capture_output=True, check=True)
        self.git("remote", "add", "origin", str(self.origin))
        self.git("push", "-u", "origin", "main")
        self.script("fake-agent", agentstub.source(AGENT))
        self.script("gh", FAKE_GH)
        env = patch.dict(
            os.environ,
            {
                "PATH": str(self.binary) + os.pathsep + os.environ["PATH"],
                "FACTORY_AGENT_CMD": str(self.binary / "fake-agent"),
                "FAKE_PR": str(self.root / "pr.json"),
                "FAKE_GH_LOG": str(self.root / "gh-calls.jsonl"),
            },
        )
        env.start()
        self.addCleanup(env.stop)

    invoke_cli = test_cli_preflight.CommandTests.invoke_cli

    def script(self, name, source):
        path = self.binary / name
        path.write_text(f"#!{sys.executable}\n" + source)
        path.chmod(0o755)

    def git(self, *args, cwd=None):
        return subprocess.run(
            ["git", *args], cwd=cwd or self.repo, text=True, capture_output=True, check=True
        ).stdout.strip()

    def gates(self):
        """(state, sha) of every factory/gate status posted so far."""
        log = self.root / "gh-calls.jsonl"
        found = []
        for line in log.read_text().splitlines() if log.exists() else []:
            args = json.loads(line)["args"]
            if args[:1] == ["api"] and "context=factory/gate" in args:
                found.append((args[args.index("-f") + 1].split("=", 1)[1], args[3].rsplit("/", 1)[1]))
        return found

    def runner(self, lock=None):
        return Runner(self.repo, self.run_dir, assignment(), timeout=10, lock=lock)

    def publish(self):
        """A first, ordinary run: the job branch is pushed and its PR published."""
        (self.run_dir / "plan.json").write_text(json.dumps(assignment()))
        runner = self.runner()
        runner.run()
        state = runner.state.data["jobs"][JOB]
        self.assertEqual(state["status"], "pr_ready", state.get("error"))
        return state

    def advance_base(self, files):
        """A sibling PR merges: ``files`` land on origin/main."""
        other = self.root / "sibling"
        subprocess.run(["git", "clone", "-q", "-b", "main", str(self.origin), str(other)], check=True, capture_output=True)
        self.git("config", "user.email", "sibling@example.invalid", cwd=other)
        self.git("config", "user.name", "Sibling", cwd=other)
        for name, text in files.items():
            (other / name).write_text(text)
        self.git("add", ".", cwd=other)
        self.git("commit", "-m", "sibling merged", cwd=other)
        self.git("push", "origin", "main", cwd=other)
        return self.git("rev-parse", "HEAD", cwd=other)

    def remote_head(self, branch):
        return self.git("rev-parse", f"refs/heads/{branch}", cwd=self.origin)

    def prepared(self, files):
        """Published job, then the base moves; returns (runner, worktree, state, old origin head)."""
        state = self.publish()
        old = self.remote_head(state["branch"])
        self.advance_base(files)
        runner = self.runner()
        worktree = runner.prepare_worktree(assignment()["jobs"][0])
        return runner, worktree, runner.state.data["jobs"][JOB], old

    def retry(self):
        runner = self.runner()
        runner.state.update(JOB, status="queued", resume_phase=None, retry_reason="base moved")
        runner.run()
        return runner.state.data["jobs"][JOB]

    def pushes(self, runner):
        calls = []
        real = runner.git

        def spy(*args, **kwargs):
            calls.append(args)
            return real(*args, **kwargs)

        runner.git = spy
        return calls

    def test_clean_update_merges_supervisor_side_and_builder_runs_on_it(self):
        first = self.publish()
        self.advance_base({"src/b.txt": "sibling\n"})
        self.git("fetch", "origin")
        new_base = self.git("rev-parse", "origin/main")
        state = self.retry()
        self.assertEqual(state["status"], "pr_ready", state.get("error"))
        worktree = state["worktree"]
        head = self.git("rev-parse", "HEAD", cwd=worktree)
        self.assertEqual(state["base"], new_base)
        self.assertEqual(state["commit"], head)
        self.assertNotEqual(head, first["commit"])
        # commit()'s check against the new base holds, and the PR head is the merged branch.
        self.git("merge-base", "--is-ancestor", new_base, head, cwd=worktree)
        self.assertEqual(self.remote_head(state["branch"]), head)
        seen = json.loads((self.binary / "builder-saw").read_text())
        self.assertEqual(seen, {"sibling": True, "markers": False, "prompt_mentions_conflict": False, "prompt_forbids_git": False})
        self.assertEqual(self.gates()[-1], ("success", head))

    def test_clean_update_pushes_with_lease_only_to_the_job_branch(self):
        runner, worktree, state, old = self.prepared({"src/b.txt": "sibling\n"})
        calls = self.pushes(runner)
        self.assertEqual(runner.sync_branch(assignment()["jobs"][0], worktree), [])
        pushes = [c for c in calls if c[0] == "push"]
        self.assertEqual(len(pushes), 1)
        branch = state["branch"]
        self.assertEqual(
            pushes[0],
            ("push", f"--force-with-lease=refs/heads/{branch}:{old}", "-u", "origin", f"refs/heads/{branch}:refs/heads/{branch}"),
        )
        head = self.git("rev-parse", "HEAD", cwd=worktree)
        self.assertEqual(self.remote_head(branch), head)
        self.assertEqual(self.gates()[-1], ("pending", head))
        self.assertEqual(self.git("rev-parse", "refs/heads/main", cwd=self.origin), state["base"])

    def test_conflict_is_left_in_the_worktree_for_the_builder(self):
        runner, worktree, state, old = self.prepared({"src/a.txt": "sibling\n"})
        gates = len(self.gates())
        calls = self.pushes(runner)
        conflicts = runner.sync_branch(assignment()["jobs"][0], worktree)
        self.assertEqual(conflicts, ["src/a.txt"])
        self.assertIn("<<<<<<<", (Path(worktree) / "src/a.txt").read_text())
        self.git("rev-parse", "--verify", "MERGE_HEAD", cwd=worktree)
        self.assertEqual([c for c in calls if c[0] == "push"], [])
        self.assertEqual(self.remote_head(state["branch"]), old)
        self.assertEqual(len(self.gates()), gates)
        self.assertEqual(runner.state.data["jobs"][JOB]["base"], self.git("rev-parse", "origin/main"))

    def test_builder_resolves_the_conflict_and_the_job_reaches_a_mergeable_head(self):
        first = self.publish()
        self.advance_base({"src/a.txt": "sibling\n"})
        self.git("fetch", "origin")
        new_base = self.git("rev-parse", "origin/main")
        state = self.retry()
        self.assertEqual(state["status"], "pr_ready", state.get("error"))
        worktree = state["worktree"]
        seen = json.loads((self.binary / "builder-saw").read_text())
        self.assertTrue(seen["markers"] and seen["prompt_mentions_conflict"] and seen["prompt_forbids_git"])
        head = self.git("rev-parse", "HEAD", cwd=worktree)
        self.assertEqual((Path(worktree) / "src/a.txt").read_text(), "resolved\n")
        self.assertEqual(len(self.git("rev-list", "--parents", "-n1", "HEAD", cwd=worktree).split()), 3)
        self.git("merge-base", "--is-ancestor", new_base, head, cwd=worktree)
        self.git("merge-base", "--is-ancestor", first["commit"], head, cwd=worktree)
        self.assertEqual(self.remote_head(state["branch"]), head)
        self.assertEqual(self.gates()[-1], ("success", head))
        # Only the job's own file is in scope, not what the base merged in.
        self.assertEqual(state["actual_paths"], ["src/a.txt"])

    def test_unresolved_markers_are_not_committed(self):
        first = self.publish()
        self.advance_base({"src/a.txt": "sibling\n"})
        (self.binary / "leave-markers").touch()
        state = self.retry()
        self.assertEqual(state["status"], "failed")
        self.assertIn("unresolved merge conflict markers", state["error"])
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=state["worktree"]), first["commit"])
        self.assertEqual(self.remote_head(state["branch"]), first["commit"])

    def test_lock_lost_after_the_merge_pushes_and_posts_nothing(self):
        lock = HeldLock()
        state = self.publish()
        old = self.remote_head(state["branch"])
        self.advance_base({"src/b.txt": "sibling\n"})
        runner = self.runner(lock)
        worktree = runner.prepare_worktree(assignment()["jobs"][0])
        gates = len(self.gates())
        real = runner.git

        def lose_after_merge(*args, **kwargs):
            result = real(*args, **kwargs)
            if "merge" in args:
                lock.lost = True
            return result

        runner.git = lose_after_merge
        with self.assertRaises(LockLost):
            runner.sync_branch(assignment()["jobs"][0], worktree)
        self.assertEqual(self.remote_head(state["branch"]), old)
        self.assertEqual(len(self.gates()), gates)

    def test_lock_lost_after_the_push_posts_no_gate(self):
        lock = HeldLock()
        state = self.publish()
        self.advance_base({"src/b.txt": "sibling\n"})
        runner = self.runner(lock)
        worktree = runner.prepare_worktree(assignment()["jobs"][0])
        gates = len(self.gates())
        real = runner.git

        def lose_after_push(*args, **kwargs):
            result = real(*args, **kwargs)
            if args[0] == "push":
                lock.lost = True
            return result

        runner.git = lose_after_push
        with self.assertRaises(LockLost):
            runner.sync_branch(assignment()["jobs"][0], worktree)
        self.assertEqual(len(self.gates()), gates)

    def test_lock_lost_before_the_fetch_does_nothing(self):
        lock = HeldLock()
        state = self.publish()
        old = self.remote_head(state["branch"])
        self.advance_base({"src/b.txt": "sibling\n"})
        runner = self.runner(lock)
        worktree = runner.prepare_worktree(assignment()["jobs"][0])
        before = self.git("rev-parse", "origin/main")
        lock.lost = True
        calls = self.pushes(runner)
        with self.assertRaises(LockLost):
            runner.sync_branch(assignment()["jobs"][0], worktree)
        self.assertEqual(calls, [])
        self.assertEqual(self.git("rev-parse", "origin/main"), before)
        self.assertEqual(self.remote_head(state["branch"]), old)

    def test_unchanged_base_changes_nothing(self):
        state = self.publish()
        runner = self.runner()
        worktree = runner.prepare_worktree(assignment()["jobs"][0])
        head = self.git("rev-parse", "HEAD", cwd=worktree)
        count = self.git("rev-list", "--count", "HEAD", cwd=worktree)
        gates = len(self.gates())
        calls = self.pushes(runner)
        self.assertEqual(runner.sync_branch(assignment()["jobs"][0], worktree), [])
        self.assertEqual([c for c in calls if c[0] in {"push", "merge", "commit"}], [])
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=worktree), head)
        self.assertEqual(self.git("rev-list", "--count", "HEAD", cwd=worktree), count)
        self.assertEqual(self.remote_head(state["branch"]), head)
        self.assertEqual(len(self.gates()), gates)

    def test_unchanged_base_retry_makes_no_merge_commit(self):
        first = self.publish()
        state = self.retry()
        self.assertEqual(state["status"], "pr_ready", state.get("error"))
        self.assertEqual(self.git("rev-list", "--merges", "--count", "HEAD", cwd=state["worktree"]), "0")
        self.assertEqual(state["base"], first["base"])

    def test_remote_commits_the_branch_lacks_are_never_overwritten(self):
        runner, worktree, state, old = self.prepared({"src/b.txt": "sibling\n"})
        other = self.root / "human"
        subprocess.run(["git", "clone", "-q", "-b", "main", str(self.origin), str(other)], check=True, capture_output=True)
        self.git("config", "user.email", "h@example.invalid", cwd=other)
        self.git("config", "user.name", "Human", cwd=other)
        self.git("checkout", "-q", state["branch"], cwd=other)
        (other / "human.txt").write_text("x")
        self.git("add", ".", cwd=other)
        self.git("commit", "-m", "human", cwd=other)
        self.git("push", "origin", state["branch"], cwd=other)
        human = self.remote_head(state["branch"])
        with self.assertRaises(RuntimeError):
            runner.sync_branch(assignment()["jobs"][0], worktree)
        self.assertEqual(self.remote_head(state["branch"]), human)

    def test_retry_admits_a_green_pr_that_conflicts_and_syncs_it_to_a_mergeable_head(self):
        first = self.publish()
        self.advance_base({"src/a.txt": "sibling\n"})
        pr = passing_pr(headRefOid=first["commit"], mergeable="CONFLICTING", mergeStateStatus="DIRTY")
        with patch("factory.ci_recovery.inspect_pr", return_value=pr), patch(
            "factory.ci_recovery.required_checks", return_value=[{"name": "tests", "bucket": "pass"}]
        ):
            self.invoke_cli("retry", JOB, "--reason", "base moved")
        queued = self.runner().state.data["jobs"][JOB]
        self.assertEqual(queued["status"], "queued")
        self.assertIsNone(queued["ci_repair"])
        self.assertEqual(queued["retry_reason"], "base moved")
        runner = self.runner()
        runner.run()
        state = runner.state.data["jobs"][JOB]
        self.assertEqual(state["status"], "pr_ready", state.get("error"))
        head = self.git("rev-parse", "HEAD", cwd=state["worktree"])
        self.git("merge-base", "--is-ancestor", self.git("rev-parse", "origin/main"), head, cwd=state["worktree"])
        self.assertEqual(self.remote_head(state["branch"]), head)
        self.assertEqual(self.gates()[-1], ("success", head))

    def test_retry_still_rejects_a_clean_pr_with_passing_checks(self):
        first = self.publish()
        pr = passing_pr(headRefOid=first["commit"], mergeable="MERGEABLE", mergeStateStatus="CLEAN")
        with patch("factory.ci_recovery.inspect_pr", return_value=pr), patch(
            "factory.ci_recovery.required_checks", return_value=[{"name": "tests", "bucket": "pass"}]
        ), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.invoke_cli("retry", JOB, "--reason", "nothing changed")
        self.assertEqual(self.runner().state.data["jobs"][JOB]["status"], "pr_ready")

    def test_ci_repair_cannot_be_completed_by_the_supervisors_merge_commit(self):
        first = self.publish()
        self.advance_base({"src/b.txt": "sibling\n"})
        (self.binary / "no-change").touch()
        runner = self.runner()
        runner.state.update(
            JOB,
            status="queued",
            resume_phase=None,
            retry_reason="repair",
            ci_repair={"commit": first["commit"], "diagnosis": "repair", "failed_checks": []},
        )
        runner.run()
        state = runner.state.data["jobs"][JOB]
        self.assertEqual(state["status"], "failed")
        self.assertIn("CI repair made no code change", state["error"])
        self.assertEqual(state["synced_head"], self.git("rev-parse", "HEAD", cwd=state["worktree"]))
        self.assertNotEqual(state["synced_head"], first["commit"])


if __name__ == "__main__":
    unittest.main()

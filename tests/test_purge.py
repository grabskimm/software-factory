"""Purge uses real git repos and linked worktrees; it must never delete what is not the run's."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import factory
from factory import purge as purge_module
from factory.control import RunControl, request_drain
from factory.purge import PurgeRefused, purge_run, worker_remover
from factory.state import State, atomic_json, host_id, timestamp

from tests.test_hostpid import OTHER_HOST, dead_pid, kill_group, sleeper

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Factory Test",
    "GIT_AUTHOR_EMAIL": "factory@example.invalid",
    "GIT_COMMITTER_NAME": "Factory Test",
    "GIT_COMMITTER_EMAIL": "factory@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}
NOTHING = {"worktrees": [], "branches": [], "removed_run_dir": False}


def git(*args, cwd):
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=GIT_ENV,
        text=True,
        capture_output=True,
        check=True,
        timeout=60,
    )
    return result.stdout.rstrip("\n")


class PurgeCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.repo = self.root / "repo"
        self.origin = self.root / "origin.git"
        self.runs = self.root / "runs"
        self.repo.mkdir()
        self.runs.mkdir()
        self.g("init", "-b", "main")
        (self.repo / "README.md").write_text("main\n")
        self.g("add", ".")
        self.g("commit", "-m", "initial")
        git("init", "--bare", str(self.origin), cwd=self.root)
        self.g("remote", "add", "origin", str(self.origin))
        self.g("push", "origin", "main")

    def g(self, *args, cwd=None):
        return git(*args, cwd=cwd or self.repo)

    def make_run(self, name, jobs=("job",), reviews=("job",), planner=True):
        """A run dir like the real one: plan, state, planner/job/review worktrees + branches."""
        directory = self.runs / name
        directory.mkdir()
        plan = {"jobs": [{"id": job} for job in jobs]}
        atomic_json(directory / "plan.json", plan)
        state = State(directory, plan)
        (directory / "issues.json").write_text("[]\n")
        trees = []
        if planner:
            trees.append(("planner", f"factory/{name}/planner"))
        for job in jobs:
            trees.append((job, f"factory/{name}/{job}"))
        for job in reviews:
            trees.append((f"{job}-review-1-0", f"factory/{name}/{job}-review-1-0"))
        for folder, branch in trees:
            path = directory / "worktrees" / folder
            self.g("worktree", "add", "-b", branch, str(path), "main")
            (path / "scratch.txt").write_text("untracked agent output\n")
            (path / "README.md").write_text("modified and uncommitted\n")
        for job in jobs:
            state.update(
                job,
                branch=f"factory/{name}/{job}",
                worktree=str(directory / "worktrees" / job),
            )
        (directory / jobs[0]).mkdir()
        (directory / jobs[0] / "0-build-0.jsonl").write_text("transcript\n")
        return directory, state

    def branches(self):
        out = self.g("for-each-ref", "--format=%(refname:short)", "refs/heads")
        return sorted(out.splitlines())

    def worktrees(self):
        out = self.g("worktree", "list", "--porcelain")
        return [
            Path(line.split(" ", 1)[1])
            for line in out.splitlines()
            if line.startswith("worktree ")
        ]

    def admin_entries(self):
        """Git's per-worktree admin directories (what `worktree prune` cleans up)."""
        root = self.repo / ".git" / "worktrees"
        return sorted(p.name for p in root.iterdir()) if root.exists() else []

    def snapshot(self):
        """Everything purge could possibly change, for 'refused means nothing changed'."""
        return (
            self.branches(),
            self.worktrees(),
            sorted(
                str(p.relative_to(self.root))
                for p in self.runs.rglob("*")
                if p.name != ".git"
            ),
            self.g("for-each-ref", "--format=%(refname) %(objectname)"),
        )

    def assert_refused(self, directory, message, **kwargs):
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, message):
            purge_run(self.repo, directory, **kwargs)
        self.assertEqual(self.snapshot(), before)

    def purge(self, directory, **kwargs):
        return purge_run(self.repo, directory, **kwargs)


class PurgeTests(PurgeCase):
    def test_full_purge_removes_the_run_and_nothing_else(self):
        run1, _ = self.make_run("r1", jobs=("job", "other"), reviews=("job", "other"))
        run2, _ = self.make_run("r2")
        run10, _ = self.make_run("r10")  # a name that merely starts like r1
        elsewhere = self.root / "elsewhere"
        self.g("worktree", "add", "-b", "side/feature", str(elsewhere), "main")
        self.g("branch", "feature/x", "main")
        self.g("branch", "factory-r1/job", "main")
        self.g("branch", "factory/other-run/job", "main")
        before = self.branches()
        self.assertIn("factory/r1/job-review-1-0", before)

        summary = self.purge(run1)

        self.assertEqual(
            sorted(Path(p).name for p in summary["worktrees"]),
            sorted(
                [
                    "planner",
                    "job",
                    "other",
                    "job-review-1-0",
                    "other-review-1-0",
                ]
            ),
        )
        self.assertEqual(
            summary["branches"],
            sorted(b for b in before if b.startswith("factory/r1/")),
        )
        self.assertEqual(len(summary["branches"]), 5)
        self.assertTrue(summary["removed_run_dir"])
        self.assertFalse(run1.exists())
        self.assertEqual(
            self.branches(), sorted(set(before) - set(summary["branches"]))
        )
        remaining = self.worktrees()
        self.assertFalse([p for p in remaining if "r1/worktrees" in str(p)])
        for survivor in (run2, run10):
            self.assertTrue((survivor / "plan.json").is_file())
            self.assertTrue((survivor / "worktrees" / "job").is_dir())
            self.assertIn(survivor / "worktrees" / "job", remaining)
            self.assertEqual(
                git("branch", "--show-current", cwd=survivor / "worktrees" / "job"),
                f"factory/{survivor.name}/job",
            )
        self.assertIn(elsewhere, remaining)
        self.assertIn(self.repo, remaining)
        # Git's own admin entries for the purged trees are gone, not just the directories.
        self.assertEqual(len(self.admin_entries()), len(remaining) - 1)
        self.assertEqual(self.g("status", "--porcelain"), "")
        self.assertEqual(self.g("branch", "--show-current"), "main")
        self.assertEqual((self.repo / "README.md").read_text(), "main\n")

    def test_purge_removes_dirty_worktrees_and_every_file_in_the_run_dir(self):
        run, _ = self.make_run("r1")
        (run / "pr-body.md").write_text("Closes #1\n")
        (run / "worktrees" / "job" / "node_modules").mkdir()
        (run / "worktrees" / "job" / "node_modules" / "x.js").write_text("1")
        summary = self.purge(run)
        self.assertTrue(summary["removed_run_dir"])
        self.assertFalse(run.exists())

    def test_second_call_is_idempotent_and_reports_nothing_left(self):
        run, _ = self.make_run("r1")
        first = self.purge(run)
        self.assertTrue(first["removed_run_dir"])
        self.assertEqual(self.purge(run), NOTHING)
        self.assertEqual(self.branches(), ["main"])
        self.assertEqual(self.worktrees(), [self.repo])

    def test_resumes_after_an_interrupted_purge(self):
        run, _ = self.make_run("r1")
        # Crash window: one worktree directory deleted by hand (stale git entry),
        # one branch already deleted, run dir still present.
        shutil.rmtree(run / "worktrees" / "job")
        self.g("worktree", "remove", "--force", str(run / "worktrees" / "planner"))
        self.g("branch", "-D", "factory/r1/planner")
        summary = self.purge(run)
        self.assertTrue(summary["removed_run_dir"])
        self.assertEqual(self.branches(), ["main"])
        self.assertEqual(self.worktrees(), [self.repo])
        self.assertEqual(self.admin_entries(), [])

    def test_run_with_only_a_plan_and_no_state_can_be_purged(self):
        directory = self.runs / "planned"
        directory.mkdir()
        atomic_json(directory / "plan.json", {"jobs": []})
        self.assertTrue(self.purge(directory)["removed_run_dir"])
        self.assertFalse(directory.exists())

    def test_run_with_only_state_and_no_plan_can_be_purged(self):
        directory = self.runs / "stateful"
        directory.mkdir()
        atomic_json(directory / "state.json", {"version": 1, "jobs": {}})
        self.assertTrue(self.purge(directory)["removed_run_dir"])

    def test_a_run_with_no_worktrees_or_branches_only_loses_its_directory(self):
        directory = self.runs / "plain"
        directory.mkdir()
        atomic_json(directory / "plan.json", {"jobs": []})
        self.assertEqual(
            self.purge(directory),
            {"worktrees": [], "branches": [], "removed_run_dir": True},
        )

    def test_review_branches_without_worktrees_are_deleted_too(self):
        run, _ = self.make_run("r1")
        self.g("branch", "factory/r1/job-review-2-1", "main")
        summary = self.purge(run)
        self.assertIn("factory/r1/job-review-2-1", summary["branches"])
        self.assertEqual(self.branches(), ["main"])

    def test_run_name_comes_from_the_directory_not_from_state_contents(self):
        run, state = self.make_run("r1")
        other, _ = self.make_run("r2")
        # Hostile/stale state pointing at another run's worktree and branch.
        state.update(
            "job",
            branch="factory/r2/job",
            worktree=str(other / "worktrees" / "job"),
        )
        self.purge(run)
        self.assertTrue((other / "worktrees" / "job").is_dir())
        self.assertIn("factory/r2/job", self.branches())

    def test_unrelated_registered_worktree_is_not_removed_even_inside_the_run_dir(self):
        run, _ = self.make_run("r1")
        stray = run / "stray-tree"
        self.g("worktree", "add", "-b", "side/stray", str(stray), "main")
        summary = self.purge(run)
        self.assertNotIn(str(stray), summary["worktrees"])
        self.assertIn("side/stray", self.branches())  # the branch is not factory/r1/*
        self.assertFalse(stray.exists())  # the directory went with the run dir ...
        self.assertNotIn(stray, self.worktrees())  # ... and git no longer lists it


class RemoteSafetyTests(PurgeCase):
    def test_remote_branches_and_tracking_refs_are_untouched(self):
        run, _ = self.make_run("r1")
        self.g("push", "-u", "origin", "factory/r1/job")
        self.g("push", "origin", "factory/r1/planner")
        remote_before = git("for-each-ref", "--format=%(refname) %(objectname)", cwd=self.origin)
        self.assertIn("refs/heads/factory/r1/job", remote_before)
        tracking_before = self.g("for-each-ref", "--format=%(refname) %(objectname)", "refs/remotes")

        summary = self.purge(run)

        self.assertIn("factory/r1/job", summary["branches"])
        self.assertNotIn("factory/r1/job", self.branches())
        self.assertEqual(
            git("for-each-ref", "--format=%(refname) %(objectname)", cwd=self.origin),
            remote_before,
        )
        self.assertEqual(
            self.g("for-each-ref", "--format=%(refname) %(objectname)", "refs/remotes"),
            tracking_before,
        )

    def test_only_local_branch_commands_are_ever_issued(self):
        run, _ = self.make_run("r1")
        calls = []
        real = purge_module._git_in(self.repo)

        def recording(*args):
            calls.append(args)
            return real(*args)

        self.purge(run, git=recording)
        verbs = {args[0] for args in calls}
        self.assertEqual(verbs, {"worktree", "for-each-ref", "branch"})
        for args in calls:
            self.assertNotIn("-r", args)
            self.assertNotIn("--remotes", args)
            self.assertFalse(any(a == "origin" or "refs/remotes" in a for a in args))
            if args[0] == "branch":
                self.assertEqual(args[:2], ("branch", "-D"))
                self.assertTrue(args[2].startswith("factory/r1/"))
            if args[0] == "worktree":
                self.assertIn(args[1], {"list", "remove", "prune"})

    def test_fake_git_hostile_output_cannot_widen_what_is_deleted(self):
        run, _ = self.make_run("r1", jobs=("job",), reviews=(), planner=False)
        outside = self.root / "outside-tree"
        outside.mkdir()
        inside_run_not_worktrees = run / "other-place"
        inside_run_not_worktrees.mkdir()
        listing = "\n\n".join(
            [
                f"worktree {self.repo}\nHEAD abc\nbranch refs/heads/main",
                f"worktree {run / 'worktrees' / 'job'}\nHEAD abc\nbranch refs/heads/factory/r1/job",
                f"worktree {outside}\nHEAD abc\nbranch refs/heads/elsewhere",
                f"worktree {inside_run_not_worktrees}\nHEAD abc\ndetached",
                f"worktree {run / 'worktrees' / '..' / '..' / '..' / 'outside-tree'}\nHEAD abc\ndetached",
            ]
        )
        refs = "\n".join(
            [
                "refs/heads/factory/r1/job",
                "refs/heads/factory/r10/job",
                "refs/heads/factory/r1/",
                "refs/heads/feature/x",
                "refs/remotes/origin/factory/r1/job",
                "refs/tags/factory/r1/v1",
            ]
        )
        calls = []

        def fake(*args):
            calls.append(args)
            if args[:2] == ("worktree", "list"):
                return listing
            if args[0] == "for-each-ref":
                return refs
            return ""

        summary = self.purge(run, git=fake)
        prefix = "refs/heads/factory/r1/"
        self.assertEqual(
            calls,
            [
                ("worktree", "list", "--porcelain"),
                ("for-each-ref", "--format=%(refname)", prefix),
                ("worktree", "remove", "--force", str(run / "worktrees" / "job")),
                ("worktree", "prune"),
                ("branch", "-D", "factory/r1/job"),
                ("worktree", "prune"),
            ],
        )
        self.assertEqual(summary["branches"], ["factory/r1/job"])
        self.assertTrue(outside.is_dir())


class RefusalTests(PurgeCase):
    def test_refuses_while_a_child_process_group_is_alive(self):
        run, state = self.make_run("r1")
        child = sleeper(self)
        state.update("job", status="running")
        state.record_pid("job", child.pid)
        self.assert_refused(run, rf"job: child process group {child.pid} is still alive")
        # A lease never makes a live local child safe to delete under.
        self.assert_refused(
            run, "still alive", holds_lease=lambda: True
        )
        kill_group(child.pid)
        child.wait()
        self.assertTrue(self.purge(run)["removed_run_dir"])

    def test_refuses_while_only_a_grandchild_survives_in_the_group(self):
        run, state = self.make_run("r1")
        leader = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import subprocess, sys; "
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])",
            ],
            start_new_session=True,
        )
        self.addCleanup(kill_group, leader.pid)
        leader.wait()
        state.record_pid("job", leader.pid)
        self.assert_refused(run, "still alive")

    def test_refuses_foreign_host_pid_without_the_lease(self):
        run, state = self.make_run("r1")
        state.update("job", status="running", pid=4242, pid_host=OTHER_HOST)
        self.assert_refused(run, "another host.*repository lease")
        for lease in (lambda: False, lambda: 1, None):
            self.assert_refused(run, "repository lease", holds_lease=lease)

    def test_foreign_host_pid_proceeds_when_the_lease_is_held(self):
        run, state = self.make_run("r1")
        child = sleeper(self)  # the same number is live here, but it is not "our" process
        state.update("job", status="running", pid=child.pid, pid_host=OTHER_HOST)
        summary = self.purge(run, holds_lease=lambda: True)
        self.assertTrue(summary["removed_run_dir"])
        self.assertEqual(self.branches(), ["main"])
        self.assertIsNone(child.poll())  # purge never signals any pid

    def test_lease_is_only_consulted_when_a_foreign_pid_exists(self):
        run, state = self.make_run("r1")
        state.update("job", pid=dead_pid(), pid_host=host_id())
        calls = []
        self.purge(run, holds_lease=lambda: calls.append(1) or True)
        self.assertEqual(calls, [])

    def test_unreadable_or_odd_state_json_refuses(self):
        for index, text in enumerate(
            ("{not json", "[]", '{"jobs": []}', '{"jobs": {"a": 5}}', "{}")
        ):
            with self.subTest(text=text):
                run, _ = self.make_run(f"s{index}")
                (run / "state.json").write_text(text)
                self.assert_refused(run, "state.json is unreadable")

    def test_refuses_while_the_supervisor_is_running_or_draining(self):
        run, _ = self.make_run("r1")
        common = self.repo / ".git"
        with RunControl(run, common) as owner:
            self.assert_refused(run, "supervisor .* is still running")
            request_drain(run, common)
            self.assertTrue(owner.draining())
            self.assert_refused(run, "supervisor .* is still draining")
        # After the supervisor has stopped the very same run can be purged.
        self.assertTrue(self.purge(run)["removed_run_dir"])

    def test_refuses_while_a_supervisor_in_another_process_holds_the_run(self):
        run, _ = self.make_run("r1")
        common = self.repo / ".git"
        script = (
            "import sys, pathlib\n"
            "from factory.control import RunControl\n"
            "with RunControl(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])):\n"
            "    print('ready', flush=True)\n"
            "    sys.stdin.readline()\n"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", script, str(run), str(common)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env={**os.environ, "PYTHONPATH": str(Path(factory.__file__).parent.parent)},
        )
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.wait)
        self.addCleanup(child.stdin.close)
        self.assertEqual(child.stdout.readline().strip(), "ready")
        self.assert_refused(run, "supervisor .* is still running")
        child.stdin.write("\n")
        child.stdin.flush()
        child.wait(timeout=30)
        self.assertTrue(self.purge(run)["removed_run_dir"])

    def test_stale_control_record_of_a_dead_supervisor_does_not_block(self):
        run, _ = self.make_run("r1")
        atomic_json(
            run / "supervisor-control.json",
            {
                "version": 1,
                "history": [],
                "current": {
                    "token": "t",
                    "pid": dead_pid(),
                    "common_dir": str(self.repo / ".git"),
                    "run_dir": str(run),
                    "started_at": timestamp(),
                    "status": "running",
                    "drain_requested_at": None,
                },
            },
        )
        self.assertTrue(self.purge(run)["removed_run_dir"])

    def test_corrupt_control_record_refuses(self):
        run, _ = self.make_run("r1")
        (run / "supervisor-control.json").write_text("{corrupt")
        self.assert_refused(run, "supervisor control record unusable")

    def test_refuses_malformed_run_names_and_changes_nothing(self):
        self.make_run("r1")
        for name in ("bad name", ".hidden", "-lead", "_lead", "a" * 51, "semi;colon", "dot.dot"):
            with self.subTest(name=name):
                directory = self.runs / name
                directory.mkdir()
                atomic_json(directory / "plan.json", {"jobs": []})
                self.assert_refused(directory, "short alphanumeric slug")
                self.assertTrue((directory / "plan.json").is_file())
        for odd in (self.runs / "r1" / "..", Path("/")):
            with self.subTest(path=str(odd)):
                self.assert_refused(odd, "short alphanumeric slug")
        self.assertTrue((self.runs / "r1" / "plan.json").is_file())

    def test_a_malformed_name_is_refused_even_if_it_does_not_exist(self):
        with self.assertRaises(PurgeRefused):
            self.purge(self.runs / "no such run")

    def test_refuses_a_symlinked_run_directory(self):
        run, _ = self.make_run("r1")
        link = self.runs / "link"
        link.symlink_to(run, target_is_directory=True)
        self.assert_refused(link, "symlink")
        self.assertTrue(link.is_symlink())
        self.assertTrue((run / "plan.json").is_file())
        # Also when it points at an unrelated directory that looks like a run.
        decoy = self.root / "decoy"
        decoy.mkdir()
        atomic_json(decoy / "plan.json", {"jobs": []})
        dangling = self.runs / "dangling"
        dangling.symlink_to(self.root / "nowhere", target_is_directory=True)
        evil = self.runs / "evil"
        evil.symlink_to(decoy, target_is_directory=True)
        self.assert_refused(evil, "symlink")
        self.assert_refused(dangling, "symlink")
        self.assertTrue((decoy / "plan.json").is_file())

    def test_refuses_a_directory_that_is_not_a_run(self):
        precious = self.runs / "precious"
        precious.mkdir()
        (precious / "thesis.tex").write_text("irreplaceable\n")
        self.assert_refused(precious, "no plan.json or state.json")
        self.assertEqual((precious / "thesis.tex").read_text(), "irreplaceable\n")
        # Markers must be real files, not directories or symlinks to somewhere else.
        fake = self.runs / "fake"
        fake.mkdir()
        (fake / "plan.json").mkdir()
        self.assert_refused(fake, "no plan.json or state.json")
        notadir = self.runs / "notadir"
        notadir.write_text("a file\n")
        self.assert_refused(notadir, "not a plain run directory")
        self.assertTrue(notadir.is_file())

    def test_refuses_when_the_main_checkout_is_inside_the_run_directory(self):
        directory = self.runs / "host"
        directory.mkdir()
        atomic_json(directory / "plan.json", {"jobs": []})
        nested = directory / "checkout"
        nested.mkdir()
        git("init", "-b", "main", cwd=nested)
        before = sorted(p.name for p in directory.iterdir())
        for repo in (nested, directory):
            with self.assertRaisesRegex(PurgeRefused, "inside the run directory"):
                purge_run(repo, directory)
        self.assertEqual(sorted(p.name for p in directory.iterdir()), before)

    def test_refuses_when_a_run_branch_is_checked_out_in_the_main_checkout(self):
        run, _ = self.make_run("r1")
        self.g("branch", "factory/r1/extra", "main")
        self.g("checkout", "factory/r1/extra")
        self.assert_refused(run, "factory/r1/extra is checked out in the main checkout")
        self.g("checkout", "main")
        self.assertTrue(self.purge(run)["removed_run_dir"])
        self.assertEqual(self.branches(), ["main"])

    def test_refuses_when_a_run_branch_is_checked_out_in_a_foreign_worktree(self):
        run, _ = self.make_run("r1")
        self.g("branch", "factory/r1/extra", "main")
        foreign = self.root / "foreign"
        self.g("worktree", "add", str(foreign), "factory/r1/extra")
        self.assert_refused(run, "factory/r1/extra is checked out in .*foreign")
        self.assertTrue(foreign.is_dir())

    def test_a_git_failure_leaves_a_resumable_run(self):
        run, _ = self.make_run("r1")
        job_tree = run / "worktrees" / "job"
        self.g("worktree", "lock", str(job_tree))
        with self.assertRaisesRegex(RuntimeError, "locked"):
            self.purge(run)
        self.assertTrue(run.is_dir())  # the run dir goes last, so a retry is possible
        self.g("worktree", "unlock", str(job_tree))
        self.assertTrue(self.purge(run)["removed_run_dir"])
        self.assertEqual(self.branches(), ["main"])
        self.assertEqual(self.worktrees(), [self.repo])

    def test_a_failed_final_prune_is_reported_not_swallowed(self):
        run, _ = self.make_run("r1")

        def flaky(*args):
            if args[:2] == ("worktree", "prune") and not run.exists():
                raise RuntimeError("git worktree prune failed: boom")
            return self.g(*args)

        summary = self.purge(run, git=flaky)
        self.assertTrue(summary["removed_run_dir"])
        self.assertEqual(
            summary["warnings"],
            ["worktree prune after removal failed: git worktree prune failed: boom"],
        )
        self.assertEqual(self.branches(), ["main"])

    def test_not_a_git_repository_refuses_and_changes_nothing(self):
        run, _ = self.make_run("r1")
        not_git = self.root / "plain-dir"
        not_git.mkdir()
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, "git (worktree list|rev-parse) .*failed"):
            purge_run(not_git, run)
        self.assertEqual(self.snapshot(), before)

    def test_ambient_git_environment_cannot_redirect_the_deletion(self):
        run, _ = self.make_run("r1")
        decoy = self.root / "decoy-repo"
        decoy.mkdir()
        git("init", "-b", "main", cwd=decoy)
        git("commit", "--allow-empty", "-m", "x", cwd=decoy)
        git("branch", "factory/r1/job", cwd=decoy)
        with patch.dict(os.environ, {"GIT_DIR": str(decoy / ".git"), "GIT_WORK_TREE": str(decoy)}):
            summary = self.purge(run)
        self.assertTrue(summary["removed_run_dir"])
        self.assertEqual(self.branches(), ["main"])
        self.assertEqual(git("branch", "--list", "factory/r1/job", cwd=decoy).strip(), "factory/r1/job")


class RunDirectoryContentTests(PurgeCase):
    def test_rmtree_does_not_follow_symlinks_inside_the_run_dir(self):
        run, _ = self.make_run("r1")
        keep = self.root / "keep"
        keep.mkdir()
        (keep / "important.txt").write_text("keep me\n")
        (run / "link-to-keep").symlink_to(keep, target_is_directory=True)
        (run / "worktrees" / "job" / "link-out").symlink_to(keep, target_is_directory=True)
        self.assertTrue(self.purge(run)["removed_run_dir"])
        self.assertEqual((keep / "important.txt").read_text(), "keep me\n")

    def test_summary_is_json_serialisable(self):
        run, _ = self.make_run("r1")
        text = json.dumps(self.purge(run))
        self.assertIn("removed_run_dir", text)


class PurgeCloneCase(unittest.TestCase):
    """<data>/repos/org/name with a real clone of a bare origin, next to a second repository."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.data = self.base / "data"
        self.root = self.data / "repos" / "org" / "name"
        self.clone = self.root / "clone"
        self.runs = self.root / "runs"
        self.origin = self.base / "origin.git"
        seed = self.base / "seed"
        seed.mkdir()
        git("init", "-b", "main", cwd=seed)
        (seed / "README.md").write_text("main\n")
        git("add", ".", cwd=seed)
        git("commit", "-m", "initial", cwd=seed)
        git("clone", "--bare", str(seed), str(self.origin), cwd=self.base)
        self.make_clone(self.root)
        self.other_root = self.data / "repos" / "org" / "other"
        self.make_clone(self.other_root)
        (self.other_root / "runs" / "r1").mkdir(parents=True)

    def make_clone(self, root):
        root.mkdir(parents=True, exist_ok=True)
        git("clone", str(self.origin), str(root / "clone"), cwd=self.base)
        (root / "factory.lock").write_text("")

    def tree(self, top=None):
        """Every path below ``top`` (default: the data directory), links not followed."""
        top = top or self.data
        found = []
        for current, names, files in os.walk(top):
            found += [str(Path(current, name).relative_to(top)) for name in names + files]
        return sorted(found)

    def purge(self, **kwargs):
        return purge_module.purge_clone(self.root, self.data, **kwargs)

    def assert_refused(self, message, root=None, data=None, **kwargs):
        before = self.tree(self.base)
        with self.assertRaisesRegex(PurgeRefused, message) as caught:
            purge_module.purge_clone(root or self.root, data or self.data, **kwargs)
        self.assertEqual(self.tree(self.base), before, "a refusal must change nothing")
        # Refusals name no path, so they cannot leak where another repository lives.
        self.assertNotIn(str(self.base), str(caught.exception))
        self.assertNotIn("other", str(caught.exception))


class PurgeCloneTests(PurgeCloneCase):
    def test_removes_the_clone_and_only_the_clone(self):
        others = [p for p in self.tree() if p.startswith("repos/org/other")]
        self.assertEqual(self.purge(), {"removed": True})
        self.assertFalse(self.clone.exists())
        self.assertFalse((self.root / purge_module.CLONE_LEFTOVER).exists())
        self.assertTrue((self.root / "factory.lock").exists())
        self.assertEqual([p for p in self.tree() if p.startswith("repos/org/other")], others)
        self.assertTrue((self.other_root / "clone" / ".git").is_dir())
        self.assertIn("refs/heads/main", git("for-each-ref", "--format=%(refname)", cwd=self.origin))

    def test_an_empty_runs_directory_does_not_keep_the_clone(self):
        self.runs.mkdir()
        self.assertEqual(self.purge(), {"removed": True})
        self.assertFalse(self.clone.exists())
        self.assertTrue(self.runs.is_dir())

    def test_a_second_call_and_a_repository_that_never_existed_remove_nothing(self):
        self.purge()
        self.assertEqual(self.purge(), {"removed": False})
        never = self.data / "repos" / "org" / "never-used"
        self.assertEqual(purge_module.purge_clone(never, self.data), {"removed": False})
        self.assertFalse(never.exists())

    def test_removal_keeps_working_when_the_data_directory_itself_is_a_symlink(self):
        link = self.base / "mounted"
        link.symlink_to(self.data, target_is_directory=True)
        root = link / "repos" / "org" / "name"
        self.assertEqual(purge_module.purge_clone(root, link), {"removed": True})
        self.assertFalse(self.clone.exists())

    def test_a_run_directory_refuses(self):
        for name, make in (
            ("directory", lambda: (self.runs / "r1").mkdir(parents=True)),
            ("stray file", lambda: (self.runs.mkdir(), (self.runs / ".keep").write_text(""))),
        ):
            with self.subTest(name):
                make()
                self.assert_refused("run directory remains")
                self.assertTrue((self.clone / ".git").is_dir())
                shutil.rmtree(self.runs)

    def test_a_runs_path_that_is_not_a_plain_directory_refuses(self):
        self.runs.write_text("not a directory")
        self.assert_refused("runs directory is not a directory")
        self.runs.unlink()
        self.runs.symlink_to(self.base, target_is_directory=True)
        self.assert_refused("runs directory is a symlink")

    def test_a_linked_worktree_refuses_and_survives(self):
        elsewhere = self.base / "elsewhere"
        git("worktree", "add", "-b", "side/feature", str(elsewhere), "main", cwd=self.clone)
        (elsewhere / "work.txt").write_text("unsaved work\n")
        self.assert_refused(r"1 linked worktree\(s\)")
        self.assertEqual((elsewhere / "work.txt").read_text(), "unsaved work\n")
        self.assertIn("side/feature", git("branch", "--list", "side/feature", cwd=self.clone))

    def test_a_worktree_whose_directory_is_gone_still_refuses_until_pruned(self):
        elsewhere = self.base / "elsewhere"
        git("worktree", "add", "-b", "side/feature", str(elsewhere), "main", cwd=self.clone)
        shutil.rmtree(elsewhere)
        self.assert_refused(r"1 linked worktree\(s\)")
        git("worktree", "prune", cwd=self.clone)
        self.assertEqual(self.purge(), {"removed": True})

    def test_the_clone_being_a_symlink_refuses_even_when_it_points_at_a_clone(self):
        real = self.base / "real-clone"
        self.clone.rename(real)
        self.clone.symlink_to(real, target_is_directory=True)
        self.assert_refused("the clone is a symlink")
        self.assertTrue((real / ".git").is_dir())
        self.clone.unlink()
        self.clone.symlink_to(self.base / "nowhere")  # dangling
        self.assert_refused("the clone is a symlink")

    def test_a_symlink_anywhere_on_the_way_refuses(self):
        # level -> (directory that becomes a symlink, where the clone then lives inside the moved one)
        levels = {
            "repos": ("repos", "org/name/clone"),
            "org": ("repos/org", "name/clone"),
            "name": ("repos/org/name", "clone"),
        }
        for level, (relative, inside) in levels.items():
            with self.subTest(level):
                data = self.base / f"data-{level}"
                shutil.copytree(self.data, data, symlinks=True)
                moved = self.base / f"moved-{level}"
                (data / relative).rename(moved)
                (data / relative).symlink_to(moved, target_is_directory=True)
                self.assert_refused("goes through a symlink", root=data / "repos" / "org" / "name", data=data)
                self.assertTrue((moved / inside / ".git").is_dir())

    def test_a_root_outside_the_data_directory_or_of_the_wrong_shape_refuses(self):
        outside = self.base / "outside" / "repos" / "org" / "name"
        self.make_clone(outside)
        self.assert_refused("not inside the data root", root=outside)
        for shape in (self.data, self.data / "repos", self.data / "repos" / "org", self.root / "clone", self.data / "other" / "org" / "name"):
            with self.subTest(shape.relative_to(self.base)):
                self.assert_refused("not <data>/repos/<org>/<name>", root=shape)
        # '..' cannot be used to walk out of the tree and back in under another name.
        self.assert_refused("not <data>/repos/<org>/<name>", root=self.root / ".." / "other" / "x")

    def test_a_clone_that_resolves_somewhere_else_than_its_path_says_refuses(self):
        # The symlink walk should make this unreachable; the resolved path is the last word.
        real_resolve = Path.resolve

        def lying(path, strict=False):
            if path == self.clone:
                return self.base / "elsewhere"
            return real_resolve(path, strict=strict)

        with patch.object(Path, "resolve", lying):
            self.assert_refused("does not resolve inside the data root")
        self.assertEqual(self.purge(), {"removed": True})

    def test_a_clone_that_is_not_a_plain_clone_refuses(self):
        shutil.rmtree(self.clone)
        self.clone.write_text("a file")
        self.assert_refused("the clone is not a directory")
        self.clone.unlink()
        self.clone.mkdir()
        (self.clone / "notes.txt").write_text("not a clone")
        self.assert_refused("no .git directory")
        (self.clone / ".git").write_text("gitdir: /somewhere/else\n")  # a linked worktree, not a clone
        self.assert_refused("no .git directory")
        (self.clone / ".git").unlink()
        (self.clone / ".git").symlink_to(self.other_root / "clone" / ".git", target_is_directory=True)
        self.assert_refused("no .git directory")
        self.assertTrue((self.other_root / "clone" / ".git" / "HEAD").is_file())

    def test_symlinks_inside_the_clone_are_removed_not_followed(self):
        keep = self.base / "keep"
        keep.mkdir()
        (keep / "important.txt").write_text("keep me\n")
        (self.clone / "link-out").symlink_to(keep, target_is_directory=True)
        (self.clone / ".git" / "link-out").symlink_to(keep, target_is_directory=True)
        self.assertEqual(self.purge(), {"removed": True})
        self.assertEqual((keep / "important.txt").read_text(), "keep me\n")

    def test_the_clone_has_left_its_name_before_a_failed_deletion_and_the_next_call_finishes(self):
        with patch.object(purge_module.shutil, "rmtree", side_effect=OSError(13, "Permission denied")):
            with self.assertRaisesRegex(RuntimeError, "could not remove the clone: Permission denied"):
                self.purge()
        leftover = self.root / purge_module.CLONE_LEFTOVER
        self.assertFalse(self.clone.exists())
        self.assertTrue((leftover / ".git").is_dir())
        self.assertEqual(self.purge(), {"removed": True})
        self.assertFalse(leftover.exists())
        self.assertEqual(self.purge(), {"removed": False})

    def test_a_leftover_is_removed_even_when_a_new_clone_exists_again(self):
        leftover = self.root / purge_module.CLONE_LEFTOVER
        leftover.mkdir()
        (leftover / "half.txt").write_text("x")
        self.assertEqual(self.purge(), {"removed": True})
        self.assertFalse(leftover.exists())
        self.assertFalse(self.clone.exists())

    def test_a_leftover_that_is_a_symlink_refuses_and_its_target_survives(self):
        keep = self.base / "keep"
        keep.mkdir()
        (keep / "important.txt").write_text("keep me\n")
        (self.root / purge_module.CLONE_LEFTOVER).symlink_to(keep, target_is_directory=True)
        self.assert_refused("symlink")
        self.assertTrue((self.clone / ".git").is_dir(), "the clone must not be touched first")
        self.assertEqual((keep / "important.txt").read_text(), "keep me\n")

    def test_a_clone_that_cannot_be_renamed_is_reported(self):
        with patch.object(purge_module.os, "rename", side_effect=OSError(1, "Operation not permitted")):
            with self.assertRaisesRegex(RuntimeError, "could not set the clone aside: Operation not permitted"):
                self.purge()
        self.assertTrue((self.clone / ".git").is_dir())

    def test_ambient_git_environment_cannot_redirect_the_worktree_check(self):
        decoy = self.base / "decoy"
        git("clone", str(self.origin), str(decoy), cwd=self.base)
        git("worktree", "add", "-b", "decoy/side", str(self.base / "decoy-tree"), "main", cwd=decoy)
        with patch.dict(os.environ, {"GIT_DIR": str(decoy / ".git"), "GIT_WORK_TREE": str(decoy)}):
            self.assertEqual(self.purge(), {"removed": True})
        self.assertTrue((self.base / "decoy-tree").is_dir())


class PurgeCloneGitTests(PurgeCloneCase):
    """The only thing asked of git is the worktree list; its answer decides, and nothing else is trusted."""

    LISTING = "worktree {path}\nHEAD abc\nbranch refs/heads/main"

    def listing(self, *extra):
        return "\n\n".join([self.LISTING.format(path=self.clone), *extra])

    def test_only_a_read_only_worktree_listing_is_ever_issued(self):
        calls = []

        def fake(*args):
            calls.append(args)
            return self.listing()

        self.assertEqual(self.purge(git=fake), {"removed": True})
        self.assertEqual(calls, [("worktree", "list", "--porcelain")])

    def test_a_second_listed_worktree_refuses_even_if_the_directory_looks_unrelated(self):
        extra = "worktree /nowhere/at/all\nHEAD abc\ndetached\nprunable gitdir file points to non-existent location"
        self.assert_refused(r"1 linked worktree\(s\)", git=lambda *args: self.listing(extra))

    def test_an_unusable_listing_refuses_instead_of_guessing(self):
        def failing(*args):
            raise RuntimeError(f"git worktree failed: secret detail at {self.base}")

        self.assert_refused("could not be listed", git=failing)
        self.assert_refused("could not be listed", git=lambda *args: "")
        self.assert_refused("could not be listed", git=lambda *args: "garbage\nwithout a worktree line")

    def test_a_listing_that_is_not_about_the_clone_itself_refuses(self):
        other = self.LISTING.format(path=self.other_root / "clone")
        self.assert_refused("could not be listed", git=lambda *args: other)

    def test_an_empty_dot_git_folder_refuses_even_when_an_enclosing_repository_exists(self):
        # git would walk up out of a broken clone and answer for the repository around it.
        shutil.rmtree(self.clone / ".git")
        (self.clone / ".git").mkdir()
        git("init", "-b", "main", cwd=self.root)
        self.assert_refused("could not be listed")


if __name__ == "__main__":
    unittest.main()


class ResumablePurgeTests(PurgeCase):
    """A purge that fails or is killed halfway must be finishable by the next call."""

    def test_a_failed_removal_leaves_the_markers_so_the_next_purge_finishes_the_job(self):
        run, _ = self.make_run("r1")
        (run / "stuck").mkdir()
        (run / "stuck" / "file").write_text("x")
        real = shutil.rmtree

        def fail_on_stuck(path, *args, **kwargs):
            if Path(path).name == "stuck":
                raise OSError(1, "Operation not permitted")
            return real(path, *args, **kwargs)

        with patch("factory.purge.shutil.rmtree", side_effect=fail_on_stuck):
            with self.assertRaisesRegex(RuntimeError, "could not remove run directory"):
                self.purge(run)
        # whatever was removed, the directory still identifies itself as a Factory run
        self.assertTrue((run / "plan.json").is_file() and (run / "state.json").is_file())
        self.assertTrue((run / "stuck" / "file").exists())  # what could not go is still there
        summary = self.purge(run)  # the cause is gone: finishes
        self.assertTrue(summary["removed_run_dir"])
        self.assertFalse(run.exists())

    def test_the_markers_are_removed_last(self):
        run, _ = self.make_run("r1")
        order = []
        real = shutil.rmtree
        real_unlink = Path.unlink

        def record_tree(path, *args, **kwargs):
            order.append(Path(path).name)
            return real(path, *args, **kwargs)

        def record_unlink(self_, *args, **kwargs):
            order.append(self_.name)
            return real_unlink(self_, *args, **kwargs)

        with patch("factory.purge.shutil.rmtree", side_effect=record_tree), patch.object(Path, "unlink", record_unlink):
            self.purge(run)
        markers = ("plan.json", "state.json", ".factory-run")
        removed = [name for name in order if name in (*markers, "issues.json", "job", "worktrees")]
        first_marker = min(index for index, name in enumerate(removed) if name in markers)
        self.assertTrue(removed[:first_marker], "something else was removed first")
        self.assertTrue(all(name in markers for name in removed[first_marker:]), removed)  # from there on, markers only

    def test_a_worktree_removal_killed_after_its_git_pointer_went_is_finished(self):
        run, _ = self.make_run("r1")
        half = run / "worktrees" / "job"
        (half / ".git").unlink()  # what a killed `git worktree remove` leaves
        with self.assertRaises(subprocess.CalledProcessError):
            self.g("worktree", "remove", "--force", str(half))  # git refuses it for good
        summary = self.purge(run)
        self.assertTrue(summary["removed_run_dir"])
        self.assertFalse(run.exists())
        self.assertEqual(self.worktrees(), [self.repo])
        self.assertEqual(self.branches(), ["main"])

    def test_a_worktree_git_refuses_for_another_reason_is_still_refused(self):
        run, _ = self.make_run("r1")
        self.g("worktree", "lock", str(run / "worktrees" / "job"))
        before = self.snapshot()
        with self.assertRaises(RuntimeError):
            self.purge(run)
        self.assertTrue(run.exists())
        self.assertEqual(self.snapshot()[0], before[0])


class UnchangedSinceTests(PurgeCase):
    def test_a_run_used_after_the_decision_is_not_purged(self):
        run, _ = self.make_run("r1")
        seen = purge_module.newest_activity_ns(run)
        os.utime(run / "state.json", ns=(seen + 1, seen + 1))  # an operator command rewrote it
        self.assert_refused(run, "used after it was judged finished", unchanged_since=seen)

    def test_an_unchanged_run_is_purged(self):
        run, _ = self.make_run("r1")
        summary = self.purge(run, unchanged_since=purge_module.newest_activity_ns(run))
        self.assertTrue(summary["removed_run_dir"])

    def test_without_the_guard_nothing_changes(self):
        run, _ = self.make_run("r1")
        self.assertTrue(self.purge(run)["removed_run_dir"])

    def test_the_supervisor_control_file_counts_as_activity(self):
        run, _ = self.make_run("r1")
        seen = purge_module.newest_activity_ns(run)
        (run / "supervisor-control.json").write_text("{}")
        os.utime(run / "supervisor-control.json", ns=(seen + 5, seen + 5))
        self.assert_refused(run, "used after it was judged finished", unchanged_since=seen)

    def test_the_activity_of_a_directory_without_any_file_is_its_own_time(self):
        bare = self.runs / "r9"
        bare.mkdir()
        self.assertEqual(purge_module.newest_activity_ns(bare), bare.stat().st_mtime_ns)
        self.assertIsNone(purge_module.newest_activity_ns(self.runs / "gone"))


@unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions, so a closed directory cannot be shown")
class WorkerOwnedFilesTests(PurgeCase):
    """A worker session leaves directories only the worker uid may enter (Claude Code makes ``.claude/...`` 0700)."""

    def worker_home(self, run):
        """``readme-usage/.home-1-build-0/.claude/sessions`` with a file, and the sessions directory closed (mode 0)."""
        sessions = run / "readme-usage" / ".home-1-build-0" / ".claude" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "s.json").write_text("{}")
        os.chmod(sessions, 0)  # neither listable nor deletable by this uid: what a 0700 directory of another uid is
        self.addCleanup(lambda: os.path.exists(sessions) and os.chmod(sessions, 0o700))
        return sessions

    def test_a_run_with_a_directory_the_supervisor_may_not_enter_is_refused_without_a_worker_identity(self):
        run, _ = self.make_run("r1")
        self.worker_home(run)
        with self.assertRaisesRegex(RuntimeError, "could not remove run directory: Permission denied"):
            purge_run(self.repo, run)
        self.assertTrue(run.exists())

    def test_the_worker_identity_removes_what_the_supervisor_may_not_and_the_run_goes(self):
        run, _ = self.make_run("r1")
        sessions = self.worker_home(run)
        calls = []

        def as_worker(path):
            calls.append(path)
            os.chmod(sessions, 0o700)  # the worker may enter and delete its own directory

        summary = purge_run(self.repo, run, as_worker=as_worker)
        self.assertTrue(summary["removed_run_dir"])
        self.assertFalse(run.exists())
        # Asked once, for the directory the supervisor cannot list, never for the top-level entry: the worker may traverse the run's
        # own directories (group --x) but not list them, so a removal that starts at the entry sees nothing (the first live purge).
        self.assertEqual([Path(path).name for path in calls], ["sessions"])

    def test_each_closed_directory_is_handed_over_once_topmost_first_and_never_below_it(self):
        run, _ = self.make_run("r1")
        deep = run / "appends" / "append-1" / ".home-planner" / ".claude"
        for name in ("sessions", "ssh"):
            (deep / name / "inner" / "closed").mkdir(parents=True)
            (deep / name / "inner" / "closed" / "f").write_text("x")
        other = run / "job" / ".home-1-build-0" / ".config"
        other.mkdir(parents=True)
        for closed in (deep / "sessions", deep / "ssh", other):
            os.chmod(closed, 0)  # topmost closed directories; the ones below them cannot even be seen
            self.addCleanup(lambda closed=closed: os.path.exists(closed) and os.chmod(closed, 0o700))
        handed = []

        def as_worker(path):
            handed.append(Path(path))
            os.chmod(path, 0o700)
            shutil.rmtree(path)

        self.assertTrue(purge_run(self.repo, run, as_worker=as_worker)["removed_run_dir"])
        self.assertEqual(sorted(handed), sorted([deep / "sessions", deep / "ssh", other]))
        self.assertFalse(run.exists())

    def test_a_closed_directory_below_a_directory_the_supervisor_can_list_is_found_through_it(self):
        run, _ = self.make_run("r1")
        sessions = self.worker_home(run)
        entry = run / "readme-usage"
        os.chmod(entry, 0o710)  # like the job directories of a real run: the group may traverse, the owner lists
        handed = []

        def as_worker(path):
            handed.append(Path(path))
            os.chmod(path, 0o700)

        purge_run(self.repo, run, as_worker=as_worker)
        self.assertEqual(handed, [sessions])

    def test_the_worker_identity_is_not_asked_when_the_supervisor_can_delete_everything(self):
        run, _ = self.make_run("r1")
        calls = []
        purge_run(self.repo, run, as_worker=calls.append)
        self.assertEqual(calls, [])
        self.assertFalse(run.exists())

    def test_what_the_worker_could_not_remove_either_still_refuses_the_purge(self):
        run, _ = self.make_run("r1")
        self.worker_home(run)
        with self.assertRaisesRegex(RuntimeError, "Permission denied"):
            purge_run(self.repo, run, as_worker=lambda path: None)
        self.assertTrue(run.exists())

    def test_the_remover_deletes_a_tree_with_a_link_and_never_follows_it(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        tree = self.root / "tree"
        (tree / "a" / "b").mkdir(parents=True)
        (tree / "a" / "b" / "f").write_text("x")
        (tree / "link").symlink_to(outside)
        worker_remover(None, None)(tree)  # no uid to switch to in a test: the child runs as this user
        self.assertFalse(tree.exists())
        self.assertEqual((outside / "keep.txt").read_text(), "keep")

    def test_the_cli_hands_purge_the_worker_identity_of_the_repository_config_and_only_then(self):
        from factory import cli
        from factory.config import RepoConfig

        self.assertIsNone(cli.worker_remover_for(None))
        self.assertIsNone(cli.worker_remover_for(RepoConfig(repo="org/name")))
        self.assertTrue(callable(cli.worker_remover_for(RepoConfig(repo="org/name", worker_uid=10002, worker_gid=10010))))

    def test_the_child_is_started_as_the_worker_without_environment_groups_or_capabilities(self):
        seen = {}

        def fake_run(argv, **kwargs):
            seen.update(argv=argv, kwargs=kwargs)

        with patch("factory.purge.subprocess.run", side_effect=fake_run):
            worker_remover(10002, 10010)(self.root / "tree")
        kwargs = seen["kwargs"]
        self.assertEqual((kwargs["user"], kwargs["group"], kwargs["extra_groups"]), (10002, 10010, []))
        self.assertEqual((kwargs["env"], kwargs["cwd"], kwargs["check"]), ({}, "/", False))
        self.assertEqual(kwargs["timeout"], purge_module.REMOVE_TIMEOUT)
        argv = seen["argv"]
        self.assertEqual(Path(argv[1]).name, "execnocaps.py")  # the capability-dropping wrapper, as for workers
        self.assertEqual(argv[2], "--")
        self.assertEqual((argv[3], argv[4]), (sys.executable, "-I"))
        self.assertEqual(argv[-1], str(self.root / "tree"))

    def test_a_worker_that_removed_the_whole_entry_is_success(self):
        run, _ = self.make_run("r1")
        self.worker_home(run)

        def as_worker(path):
            os.chmod(path, 0o700)
            shutil.rmtree(path)  # the worker identity could delete the closed directory itself

        self.assertTrue(purge_run(self.repo, run, as_worker=as_worker)["removed_run_dir"])
        self.assertFalse(run.exists())

    def test_a_run_without_a_clone_words_an_undeletable_directory_like_any_other(self):
        run = self.runs / "r1"
        (run / "readme-usage" / "closed").mkdir(parents=True)
        (run / ".factory-run").write_text("x")
        os.chmod(run / "readme-usage" / "closed", 0)
        self.addCleanup(lambda: os.chmod(run / "readme-usage" / "closed", 0o700))
        with self.assertRaisesRegex(RuntimeError, "^could not remove run directory: Permission denied$"):
            purge_run(self.root / "no-such-clone", run)

    def test_the_remover_never_raises(self):
        worker_remover(None, None)(self.root / "does-not-exist")
        with patch("factory.purge.subprocess.run", side_effect=PermissionError("no setuid")):
            worker_remover(1, 1)(self.root)


class NoCloneTests(PurgeCase):
    def test_a_run_whose_clone_step_failed_is_purged_without_git(self):
        """plan makes the run directory first, then clones; a failed clone leaves only the marker."""
        run = self.runs / "r1"
        run.mkdir()
        (run / ".factory-run").write_text("factory run directory\n")
        (run / "plan.log").write_text("x")
        missing = self.root / "no-such-clone"
        summary = purge_run(missing, run)
        self.assertEqual(summary, {"worktrees": [], "branches": [], "removed_run_dir": True})
        self.assertFalse(run.exists())

    def test_a_missing_clone_does_not_excuse_worktrees_that_are_there(self):
        run, _ = self.make_run("r1")
        with self.assertRaises(RuntimeError):
            purge_run(self.root / "no-such-clone", run)  # git cannot vouch for the worktrees
        self.assertTrue(run.exists())

    def test_a_missing_clone_still_needs_the_run_to_be_dead(self):
        run = self.runs / "r1"
        run.mkdir()
        (run / ".factory-run").write_text("x")
        child = sleeper(self)
        atomic_json(run / "state.json", {"jobs": {"job": {"pid": child.pid, "pid_host": host_id()}}})
        with self.assertRaisesRegex(RuntimeError, "still alive"):
            purge_run(self.root / "no-such-clone", run)
        self.assertTrue(run.exists())

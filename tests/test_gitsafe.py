"""Supervisor git on worker-touched trees: nothing a worker plants may run code in the supervisor.

Every test uses REAL local git repositories. A planted ``core.fsmonitor`` program or hook writes a
marker file when git runs it, so "runs nothing" is asserted as "the marker does not exist", and
each plant is first shown to be live with a plain git call (the control).
"""

import itertools
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from factory import gitsafe, publish_block
from factory.agent import git_common_dir, repo_guidance
from factory.recovery import validate_resume
from factory.runner import Runner
from factory.state import State

from tests.test_factory import assignment

IDENTITY = ("-c", "user.name=T", "-c", "user.email=t@example.invalid")
STAMPS = itertools.count(1)  # distinct mtimes for ``NestedRepositoryTests.touch``
REFUSAL = "refusing to allow a GitHub App to create or update workflow `.github/workflows/ci.yml` without `workflows` permission"
SECRETS = ("ANTHROPIC_API_KEY", "GITHUB_APP_PRIVATE_KEY", "FACTORY_MANAGER_METRICS_TOKEN")


def plain(*args, cwd, check=True):
    """A plain git call as a person would run it (no gitsafe): the control of every plant."""
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=check)


class Fixture(unittest.TestCase):
    """A main clone with a bare origin and one linked worktree ``tree`` on branch ``job``."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(os.path.realpath(self.temp.name))
        self.marker = self.root / "MARKER"
        self.repo = self.root / "repo"
        self.origin = self.root / "origin.git"
        self.repo.mkdir()
        plain("init", "-q", "-b", "main", cwd=self.repo)
        plain("config", "user.email", "t@example.invalid", cwd=self.repo)
        plain("config", "user.name", "T", cwd=self.repo)
        (self.repo / "f.txt").write_text("one\n")
        (self.repo / "CLAUDE.md").write_text("trusted rules\n")
        plain("add", ".", cwd=self.repo)
        plain("commit", "-q", "-m", "base", cwd=self.repo)
        plain("init", "-q", "--bare", str(self.origin), cwd=self.root)
        plain("remote", "add", "origin", str(self.origin), cwd=self.repo)
        plain("push", "-q", "-u", "origin", "main", cwd=self.repo)
        self.tree = self.root / "tree"
        plain("worktree", "add", "-q", "-b", "job", str(self.tree), cwd=self.repo)
        self.gitdir = gitsafe.resolve_gitdir(self.repo, self.tree)
        gitsafe.trust(self.tree, self.gitdir)
        self.addCleanup(gitsafe.forget, self.tree)

    def program(self, name, body):
        """An executable that records that it ran, then does ``body`` (a shell line)."""
        path = self.root / name
        path.write_text("\n".join(["#!/bin/sh", f"touch {self.marker}", body, ""]))
        path.chmod(0o755)
        return path

    def hook(self, directory, name):
        Path(directory).mkdir(parents=True, exist_ok=True)
        self.program(f"hook-{name}", "exit 0")
        target = Path(directory) / name
        shutil.copy(self.root / f"hook-{name}", target)
        target.chmod(0o755)

    def plant_fsmonitor(self, *config):
        program = self.program("fsmonitor.sh", "printf '\\0'")
        plain("config", *config, "core.fsmonitor", str(program), cwd=self.tree)

    def dirty(self):
        (self.tree / "f.txt").write_text("two\n")

    def assert_live(self, *command):
        """The control: a plain git call runs the planted program (so the plant works), then reset."""
        self.dirty()
        plain(*command, cwd=self.tree, check=False)
        self.assertTrue(self.marker.exists(), "the plant did not fire: the test would prove nothing")
        self.marker.unlink()

    def assert_silent(self, *command):
        self.dirty()
        gitsafe.git(command, self.tree, self.gitdir)
        self.assertFalse(self.marker.exists(), f"git {command[0]} ran a planted program")


class ResolutionTests(Fixture):
    def test_the_git_directory_comes_from_the_main_clone(self):
        self.assertEqual(self.gitdir, str(self.repo / ".git" / "worktrees" / "tree"))
        pointer = (self.tree / ".git").read_text().strip()
        self.assertEqual(pointer, f"gitdir: {self.gitdir}")

    def test_a_rewritten_pointer_does_not_change_the_resolution(self):
        (self.tree / ".git").write_text("gitdir: /nonexistent/elsewhere\n")
        self.assertEqual(gitsafe.resolve_gitdir(self.repo, self.tree), self.gitdir)

    def test_the_main_checkout_is_its_own_git_directory(self):
        self.assertEqual(gitsafe.resolve_gitdir(self.repo, self.repo), str(self.repo / ".git"))

    def test_a_tree_the_clone_does_not_know_is_refused(self):
        stranger = self.root / "stranger"
        stranger.mkdir()
        (stranger / ".git").write_text(f"gitdir: {self.gitdir}\n")  # a copied pointer is not a registration
        for path in (stranger, self.root / "missing"):
            with self.assertRaises(gitsafe.GitSafetyError) as caught:
                gitsafe.resolve_gitdir(self.repo, path)
            self.assertEqual(str(caught.exception), gitsafe.NOT_REGISTERED)

    def test_a_recorded_directory_must_agree_with_the_clone(self):
        gitsafe.forget(self.tree)
        with self.assertRaises(gitsafe.GitSafetyError) as caught:
            gitsafe.gitdir_of(self.repo, self.tree, recorded=str(self.root / "elsewhere"))
        self.assertEqual(str(caught.exception), gitsafe.RECORDED_DIFFERS)
        self.assertIsNone(gitsafe.trusted(self.tree))
        self.assertEqual(gitsafe.gitdir_of(self.repo, self.tree, recorded=self.gitdir), self.gitdir)
        self.assertEqual(gitsafe.trusted(self.tree), self.gitdir)

    def test_agent_code_that_has_only_a_path_needs_a_registered_worktree(self):
        gitsafe.forget(self.tree)
        with self.assertRaises(gitsafe.GitSafetyError):
            gitsafe.checkout_gitdir(self.tree)  # a linked worktree nobody registered
        self.assertEqual(gitsafe.checkout_gitdir(self.repo), str(self.repo / ".git"))
        self.assertIsNone(gitsafe.checkout_gitdir(self.root))  # not a repository at all
        gitsafe.adopt(self.repo, self.tree)
        self.assertEqual(gitsafe.checkout_gitdir(self.tree), self.gitdir)


class PlantedConfigTests(Fixture):
    def test_fsmonitor_in_the_worktree_local_config_runs_nothing(self):
        plain("config", "extensions.worktreeConfig", "true", cwd=self.repo)
        self.plant_fsmonitor("--worktree")
        self.assertTrue((Path(self.gitdir) / "config.worktree").exists())
        for command in (("status",), ("diff",), ("add", "--all")):
            with self.subTest(command=command):
                self.assert_live(*command)
                self.assert_silent(*command)

    def test_fsmonitor_in_the_shared_config_runs_nothing(self):
        self.plant_fsmonitor()
        for command in (("status",), ("diff",), ("add", "--all")):
            with self.subTest(command=command):
                self.assert_live(*command)
                self.assert_silent(*command)

    def test_read_only_calls_run_nothing_either(self):
        self.plant_fsmonitor()
        for command in (("rev-parse", "HEAD"), ("merge-base", "HEAD", "main"), ("branch", "--show-current")):
            with self.subTest(command=command):
                gitsafe.git(command, self.tree, self.gitdir)
                self.assertFalse(self.marker.exists())

    def test_a_replaced_git_directory_is_refused_and_runs_nothing(self):
        (self.tree / ".git").unlink()
        plain("init", "-q", cwd=self.tree)
        self.plant_fsmonitor()
        self.dirty()
        plain("status", cwd=self.tree, check=False)
        self.assertTrue(self.marker.exists(), "the plant did not fire")
        self.marker.unlink()
        with self.assertRaises(gitsafe.GitSafetyError) as caught:
            gitsafe.git(["status"], self.tree, self.gitdir)
        self.assertEqual(str(caught.exception), "worktree .git pointer was changed")
        self.assertFalse(self.marker.exists())

    def test_an_explicit_git_directory_alone_ignores_a_replaced_git_directory(self):
        # Defence in depth: even without the pointer check git never looks at the planted directory.
        (self.tree / ".git").unlink()
        plain("init", "-q", cwd=self.tree)
        self.plant_fsmonitor()
        self.dirty()
        env = gitsafe._environment(gitsafe.PROCESS_AUTH, self.tree, self.gitdir)
        done = gitsafe._execute(["status", "--porcelain"], self.tree, env, 30, False, None, None)
        self.assertFalse(self.marker.exists())
        self.assertIn("f.txt", done.stdout)  # it read the real index, not the empty planted repository

    def test_hooks_do_not_run_on_status_diff_add_or_commit(self):
        hooks = self.repo / ".git" / "hooks"
        for name in ("post-index-change", "pre-commit", "post-commit", "reference-transaction"):
            self.hook(hooks, name)
        self.dirty()
        plain("status", cwd=self.tree)
        self.assertTrue(self.marker.exists(), "the hook did not fire: the test would prove nothing")
        self.marker.unlink()
        self.dirty()
        for command in (("status",), ("diff",), ("add", "--all"), (*IDENTITY, "commit", "-m", "x")):
            with self.subTest(command=command):
                gitsafe.git(command, self.tree, self.gitdir)
                self.assertFalse(self.marker.exists())
                (self.tree / "f.txt").write_text(f"three {command[0]}\n")

    def test_hooks_named_by_a_planted_hooks_path_do_not_run(self):
        planted = self.root / "planted-hooks"
        self.hook(planted, "post-index-change")
        plain("config", "core.hooksPath", str(planted), cwd=self.repo)
        self.assert_live("status")
        self.assert_silent("status")


class NestedRepositoryTests(Fixture):
    """A repository nested in a worktree has its own configuration: git starts a child git in it.

    ``status``, ``diff`` and ``add`` start ``git status`` inside every gitlink of the index, and that
    child reads the nested repository's ``.git/config`` and the worktree's ``.gitattributes``: a
    ``filter.<name>.clean`` program runs as the supervisor. The sandbox only denies writes to the
    worktree's own ``.git``, so a worker can write ``vendor/lib/.git``, and the supervisor's own
    ``add --all`` records the nested repository as a gitlink.
    """

    @staticmethod
    def touch(path):
        """Give a file a mtime it never had: git compares stat data, so it must hash the content again (and filter it)."""
        stamp = 1_000_000_000 + next(STAMPS)
        os.utime(path, (stamp, stamp))

    def arm(self, directory, name="f.txt"):
        """Make ``directory`` a repository whose clean filter writes the marker, with a file whose stat changed."""
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        plain("init", "-q", "-b", "main", cwd=path)
        (path / ".gitattributes").write_text("* filter=evil\n")
        program = self.program("clean-filter.sh", "cat")
        plain("config", "filter.evil.clean", str(program), cwd=path)
        (path / name).write_text("one\n")
        plain("add", "--all", cwd=path)
        plain(*IDENTITY, "commit", "-q", "-m", "armed", cwd=path)
        self.touch(path / name)
        self.marker.unlink(missing_ok=True)

    def assert_live(self, *command):
        """The control, then the nested file is made stale again (the control's child git refreshed the nested index)."""
        self.touch(self.tree / "vendor" / "lib" / "f.txt")
        super().assert_live(*command)
        self.touch(self.tree / "vendor" / "lib" / "f.txt")

    def record(self):
        """The nested repository as a gitlink of HEAD, as the supervisor's own ``add --all`` leaves it."""
        plain("add", "vendor/lib", cwd=self.tree)
        plain(*IDENTITY, "commit", "-q", "-m", "gitlink", cwd=self.tree)
        self.marker.unlink(missing_ok=True)

    def recorded(self):
        self.arm(self.tree / "vendor" / "lib")
        self.record()

    def submodule(self):
        """A real submodule that the supervisor initialised in a linked worktree (the one legitimate nested repository)."""
        library = self.root / "library"
        library.mkdir()
        plain("init", "-q", "-b", "main", cwd=library)
        (library / "lib.txt").write_text("lib\n")
        plain("add", ".", cwd=library)
        plain(*IDENTITY, "commit", "-q", "-m", "lib", cwd=library)
        self.allow = ("-c", "protocol.file.allow=always")
        plain(*self.allow, "submodule", "add", "-q", str(library), "vendor", cwd=self.repo)
        plain(*IDENTITY, "commit", "-q", "-m", "submodule", cwd=self.repo)
        extra = self.root / "extra"
        plain("worktree", "add", "-q", "-b", "with-sub", str(extra), cwd=self.repo)
        gitdir = gitsafe.adopt(self.repo, extra)
        self.addCleanup(gitsafe.forget, extra)
        gitsafe.git([*self.allow, "submodule", "update", "--init", "--recursive"], extra, gitdir)
        return extra, gitdir

    def assert_refused(self, command, tree=None, gitdir=None):
        with self.assertRaises(gitsafe.GitSafetyError) as caught:
            gitsafe.git(command, tree or self.tree, gitdir or self.gitdir)
        self.assertEqual(str(caught.exception), gitsafe.NESTED_REPOSITORY)
        self.assertFalse(self.marker.exists(), f"git {command[0]} ran the nested clean filter")

    def test_status_and_diff_start_no_child_git_in_a_recorded_nested_repository(self):
        self.recorded()
        for command in (("status", "--porcelain"), ("diff",)):
            with self.subTest(command=command):
                self.assert_live(*command)
                gitsafe.git(command, self.tree, self.gitdir)
                self.assertFalse(self.marker.exists(), f"git {command[0]} ran the nested clean filter")

    def test_the_status_of_a_moved_gitlink_is_still_reported(self):
        self.recorded()
        nested = self.tree / "vendor" / "lib"
        plain(*IDENTITY, "commit", "-q", "--allow-empty", "-m", "moved", cwd=nested)
        listed = gitsafe.git(["status", "--porcelain"], self.tree, self.gitdir)
        self.assertIn("vendor/lib", listed)

    def test_a_gitmodules_ignore_setting_of_the_worktree_does_not_override_the_command_line(self):
        self.recorded()
        (self.tree / ".gitmodules").write_text('[submodule "lib"]\n\tpath = vendor/lib\n\turl = ./lib\n\tignore = none\n')
        # The configuration alone is not enough: the worker's setting wins over it.
        plain("-c", "diff.ignoreSubmodules=dirty", "status", cwd=self.tree)
        self.assertTrue(self.marker.exists(), "the override did not fire: the test would prove nothing")
        self.marker.unlink()
        self.touch(self.tree / "vendor" / "lib" / "f.txt")
        gitsafe.git(["status", "--porcelain"], self.tree, self.gitdir)
        gitsafe.git(["diff"], self.tree, self.gitdir)
        self.assertFalse(self.marker.exists())

    def test_add_and_commit_refuse_a_recorded_nested_repository_and_run_nothing(self):
        self.recorded()
        self.assert_live("add", "--all")
        self.assert_refused(["add", "--all"])
        self.assert_refused([*IDENTITY, "commit", "-q", "-m", "x"])
        self.assert_refused([*IDENTITY, "merge", "--no-edit", "main"])

    def test_an_untracked_nested_repository_is_refused_before_the_supervisor_stages_it(self):
        self.arm(self.tree / "vendor" / "lib")
        gitsafe.git(["status", "--porcelain", "--untracked-files=all"], self.tree, self.gitdir)
        self.assertEqual(gitsafe.git(["rev-parse", "--abbrev-ref", "HEAD"], self.tree, self.gitdir), "job")
        self.assert_refused(["add", "--all"])
        staged = plain("ls-files", "--stage", cwd=self.tree).stdout
        self.assertNotIn("160000", staged)

    def test_an_ignored_nested_repository_is_never_staged_or_visited(self):
        self.arm(self.tree / "vendor" / "lib")
        (self.tree / ".gitignore").write_text("vendor/\n")
        gitsafe.git(["add", "--all"], self.tree, self.gitdir)
        self.assertNotIn("vendor", plain("ls-files", "--stage", cwd=self.tree).stdout)
        self.assertFalse(self.marker.exists())

    def test_a_symlink_to_a_nested_repository_is_a_plain_file_for_git(self):
        self.arm(self.root / "elsewhere")
        (self.tree / "vendor").mkdir()
        (self.tree / "vendor" / "lib").symlink_to(self.root / "elsewhere", target_is_directory=True)
        plain("add", "--force", "vendor/lib", cwd=self.tree)  # a tracked symlink, which is not a gitlink
        gitsafe.git(["add", "--all"], self.tree, self.gitdir)
        self.assertFalse(self.marker.exists())

    def test_commands_that_never_read_the_worktree_still_run_next_to_a_nested_repository(self):
        self.arm(self.tree / "vendor" / "lib")
        self.assertEqual(gitsafe.git(["branch", "--show-current"], self.tree, self.gitdir), "job")
        gitsafe.git(["rev-parse", "HEAD"], self.tree, self.gitdir)
        gitsafe.git(["merge-base", "HEAD", "main"], self.tree, self.gitdir)

    def test_a_submodule_the_supervisor_initialised_still_works(self):
        extra, gitdir = self.submodule()
        self.assertTrue((extra / "vendor" / ".git").is_file())
        (extra / "f.txt").write_text("changed\n")
        (extra / "vendor" / "lib.txt").write_text("changed in the submodule\n")
        listed = gitsafe.git(["status", "--porcelain"], extra, gitdir)
        self.assertIn("f.txt", listed)
        gitsafe.git(["add", "--all"], extra, gitdir)
        gitsafe.git([*IDENTITY, "commit", "-q", "-m", "x"], extra, gitdir)
        gitsafe.git([*self.allow, "submodule", "update", "--init", "--recursive"], extra, gitdir)

    def test_a_submodule_whose_git_directory_a_worker_replaced_is_refused(self):
        extra, gitdir = self.submodule()
        gitfile = extra / "vendor" / ".git"
        gitfile.unlink()
        self.arm(extra / "vendor")
        plain("status", cwd=extra, check=False)  # the control: plain git enters the planted directory
        self.assertTrue(self.marker.exists(), "the plant did not fire: the test would prove nothing")
        self.marker.unlink()
        self.touch(extra / "vendor" / "f.txt")
        self.assert_refused(["add", "--all"], extra, gitdir)
        self.assert_refused([*IDENTITY, "commit", "-q", "-m", "x"], extra, gitdir)

    def test_a_submodule_pointer_aimed_outside_the_modules_directory_is_refused(self):
        extra, gitdir = self.submodule()
        planted = self.root / "planted"
        self.arm(planted, "lib.txt")
        (extra / "vendor" / ".gitattributes").write_text("* filter=evil\n")
        (extra / "vendor" / ".git").write_text(f"gitdir: {planted / '.git'}\n")
        plain("status", cwd=extra, check=False)  # the control
        self.assertTrue(self.marker.exists(), "the plant did not fire: the test would prove nothing")
        self.marker.unlink()
        self.touch(extra / "vendor" / "lib.txt")
        self.assert_refused(["add", "--all"], extra, gitdir)

    def test_a_submodule_of_a_submodule_is_checked_too(self):
        inner = self.root / "inner"
        inner.mkdir()
        plain("init", "-q", "-b", "main", cwd=inner)
        (inner / "in.txt").write_text("inner\n")
        plain("add", ".", cwd=inner)
        plain(*IDENTITY, "commit", "-q", "-m", "inner", cwd=inner)
        library = self.root / "library"
        library.mkdir()
        plain("init", "-q", "-b", "main", cwd=library)
        allow = ("-c", "protocol.file.allow=always")
        plain(*allow, "submodule", "add", "-q", str(inner), "deps/inner", cwd=library)
        plain(*IDENTITY, "commit", "-q", "-m", "library", cwd=library)
        plain(*allow, "submodule", "add", "-q", str(library), "vendor", cwd=self.repo)
        plain(*IDENTITY, "commit", "-q", "-m", "submodule", cwd=self.repo)
        extra = self.root / "extra"
        plain("worktree", "add", "-q", "-b", "with-sub", str(extra), cwd=self.repo)
        gitdir = gitsafe.adopt(self.repo, extra)
        self.addCleanup(gitsafe.forget, extra)
        gitsafe.git([*allow, "submodule", "update", "--init", "--recursive"], extra, gitdir)
        gitsafe.git(["add", "--all"], extra, gitdir)  # both levels are genuine: nothing is refused
        deep = extra / "vendor" / "deps" / "inner"
        (deep / ".git").unlink()
        self.arm(deep, "in.txt")
        plain("status", cwd=extra, check=False)  # the control: plain git reaches the grandchild
        self.assertTrue(self.marker.exists(), "the plant did not fire: the test would prove nothing")
        self.marker.unlink()
        self.touch(deep / "in.txt")
        self.assert_refused(["add", "--all"], extra, gitdir)
        gitsafe.git(["status", "--porcelain"], extra, gitdir)
        self.assertFalse(self.marker.exists())

    def test_status_and_diff_get_the_command_line_option_unless_the_caller_chose_one(self):
        option = gitsafe.IGNORE_OPTION
        cases = (
            (["status", "--porcelain"], ["status", option, "--porcelain"]),
            (["diff", "--name-only", "a", "b"], ["diff", option, "--name-only", "a", "b"]),
            ([*IDENTITY, "status"], [*IDENTITY, "status", option]),
            (["diff", "--", "--ignore-submodules=all"], ["diff", option, "--", "--ignore-submodules=all"]),
            (["status", "--ignore-submodules=all"], ["status", "--ignore-submodules=all"]),
            (["rev-parse", "HEAD"], ["rev-parse", "HEAD"]),
            (["--version"], ["--version"]),
        )
        for given, expected in cases:
            with self.subTest(given=given):
                self.assertEqual(gitsafe._ignoring_submodules(given), expected)

    def test_the_error_of_a_status_call_carries_the_callers_argv(self):
        done = gitsafe.run(["status", "--no-such-option"], self.tree, self.gitdir, check=False)
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(done.args, ["git", "status", "--no-such-option"])


class EnvironmentTests(Fixture):
    def other(self):
        other = self.root / "other"
        other.mkdir()
        plain("init", "-q", cwd=other)
        return other

    def test_git_dir_and_work_tree_of_the_caller_are_ignored(self):
        other = self.other()
        caller = {
            "GIT_DIR": str(other / ".git"),
            "GIT_WORK_TREE": str(other),
            "GIT_INDEX_FILE": str(other / "index-elsewhere"),
        }
        with patch.dict(os.environ, caller):
            self.assertEqual(gitsafe.git(["rev-parse", "--git-dir"], self.tree, self.gitdir), self.gitdir)
            self.assertEqual(gitsafe.git(["rev-parse", "--show-toplevel"], self.tree, self.gitdir), str(self.tree))
            self.assertEqual(gitsafe.repo_git(["rev-parse", "--git-dir"], self.repo), ".git")
            self.assertEqual(gitsafe.repo_git(["rev-parse", "--show-toplevel"], self.repo), str(self.repo))
            self.assertEqual(gitsafe.git(["status", "--porcelain"], self.tree, self.gitdir), "")

    def test_the_child_environment_is_the_supervisors_without_its_secrets(self):
        real = shutil.which("git")
        dump = self.root / "env-dump"
        wrapper = self.root / "bin" / "git"
        wrapper.parent.mkdir()
        wrapper.write_text("\n".join(["#!/bin/sh", f"env > {dump}", f'exec {real} "$@"', ""]))
        wrapper.chmod(0o755)
        env = {name: "sentinel-value" for name in SECRETS}
        env["PATH"] = os.pathsep.join([str(wrapper.parent), os.environ["PATH"]])
        calls = {
            "tree": lambda: gitsafe.git(["rev-parse", "HEAD"], self.tree, self.gitdir),
            "clone": lambda: gitsafe.repo_git(["rev-parse", "HEAD"], self.repo),
            "guidance": lambda: repo_guidance(self.tree, "HEAD"),
            "common": lambda: git_common_dir(self.tree),
        }
        with patch.dict(os.environ, env):
            for name, call in calls.items():
                with self.subTest(call=name):
                    dump.unlink(missing_ok=True)
                    call()
                    seen = dump.read_text().splitlines()
                    names = {line.split("=", 1)[0] for line in seen}
                    self.assertFalse(names & set(SECRETS))
                    self.assertNotIn("sentinel-value", dump.read_text())
                    self.assertIn("GIT_CONFIG_NOSYSTEM=1", seen)
                    self.assertNotIn("GIT_WORK_TREE", names if name == "clone" else set())
                    if name != "clone":
                        self.assertIn(f"GIT_DIR={self.gitdir}", seen)
                        self.assertIn(f"GIT_WORK_TREE={self.tree}", seen)

    def test_every_call_carries_the_hardening_pairs_first(self):
        seen = []
        real = subprocess.run

        def record(argv, **kwargs):
            seen.append(list(argv))
            return real(argv, **kwargs)

        pairs = [
            "-c", "core.fsmonitor=false",
            "-c", "core.hooksPath=/dev/null",
            "-c", "diff.ignoreSubmodules=dirty",
            "-c", "fetch.recurseSubmodules=false",
            "-c", "submodule.recurse=false",
        ]  # fmt: skip
        with patch("factory.gitsafe.subprocess.run", record):
            for command in (["status"], ["rev-parse", "HEAD"], ["diff"], ["merge-base", "HEAD", "main"]):
                gitsafe.git(command, self.tree, self.gitdir)
            gitsafe.repo_git(["rev-parse", "HEAD"], self.repo)
        self.assertEqual(len(seen), 5)
        for argv in seen:
            self.assertEqual(argv[: len(pairs) + 1], ["git", *pairs])


class PointerTests(Fixture):
    def test_the_unchanged_pointer_passes_with_or_without_the_final_newline(self):
        text = f"gitdir: {self.gitdir}"
        for content in (f"{text}\n", text):
            with self.subTest(content=content):
                (self.tree / ".git").write_text(content)
                self.assertEqual(gitsafe.git(["branch", "--show-current"], self.tree, self.gitdir), "job")

    def test_a_rewritten_pointer_is_refused_before_git_starts(self):
        plain("worktree", "add", "-q", "-b", "other", str(self.root / "second"), cwd=self.repo)
        second = gitsafe.resolve_gitdir(self.repo, self.root / "second")
        for content in (f"gitdir: {second}\n", "gitdir: /nonexistent\n", "", f"gitdir: {self.gitdir}/../x\n"):
            with self.subTest(content=content):
                (self.tree / ".git").write_text(content)
                with patch("factory.gitsafe.subprocess.run") as started:
                    with self.assertRaises(gitsafe.GitSafetyError) as caught:
                        gitsafe.git(["status"], self.tree, self.gitdir)
                self.assertEqual(str(caught.exception), gitsafe.POINTER_CHANGED)
                started.assert_not_called()

    def test_a_pointer_that_became_a_symlink_or_vanished_is_refused(self):
        real = self.root / "pointer-copy"
        shutil.copy(self.tree / ".git", real)
        (self.tree / ".git").unlink()
        os.symlink(real, self.tree / ".git")
        with self.assertRaises(gitsafe.GitSafetyError):
            gitsafe.git(["status"], self.tree, self.gitdir)
        (self.tree / ".git").unlink()
        with self.assertRaises(gitsafe.GitSafetyError):
            gitsafe.git(["status"], self.tree, self.gitdir)

    def test_a_plain_checkout_is_checked_as_a_directory(self):
        main = str(self.repo / ".git")
        self.assertEqual(gitsafe.git(["branch", "--show-current"], self.repo, main), "main")
        with self.assertRaises(gitsafe.GitSafetyError):
            gitsafe.git(["branch", "--show-current"], self.repo, str(self.root / "other.git"))


class UnchangedFlowTests(Fixture):
    def test_commit_merge_base_and_conflicted_merge_still_work(self):
        base = gitsafe.git(["rev-parse", "HEAD"], self.tree, self.gitdir)
        (self.tree / "f.txt").write_text("job side\n")
        gitsafe.git(["add", "--all"], self.tree, self.gitdir)
        gitsafe.git([*IDENTITY, "commit", "-m", "job"], self.tree, self.gitdir)
        self.assertEqual(gitsafe.run(["merge-base", "--is-ancestor", base, "HEAD"], self.tree, self.gitdir, check=False).returncode, 0)
        self.assertEqual(gitsafe.run(["merge-base", "--is-ancestor", "HEAD", base], self.tree, self.gitdir, check=False).returncode, 1)
        plain("checkout", "-q", "-b", "side", base, cwd=self.repo)
        (self.repo / "f.txt").write_text("main side\n")
        plain("commit", "-q", "-am", "side", cwd=self.repo)
        plain("checkout", "-q", "main", cwd=self.repo)
        with self.assertRaises(subprocess.CalledProcessError):
            gitsafe.git([*IDENTITY, "merge", "--no-edit", "side"], self.tree, self.gitdir)
        conflicted = gitsafe.git(["diff", "--name-only", "--diff-filter=U", "-z"], self.tree, self.gitdir)
        self.assertEqual(conflicted.split("\0")[0], "f.txt")
        merging = gitsafe.run(["rev-parse", "-q", "--verify", "MERGE_HEAD"], self.tree, self.gitdir, check=False)
        self.assertEqual(merging.returncode, 0)

    def test_push_fetch_and_bundle_work_under_the_explicit_git_directory(self):
        (self.tree / "g.txt").write_text("new\n")
        gitsafe.git(["add", "--all"], self.tree, self.gitdir)
        gitsafe.git([*IDENTITY, "commit", "-m", "job"], self.tree, self.gitdir)
        gitsafe.git(["push", "-u", "origin", "refs/heads/job:refs/heads/job"], self.tree, self.gitdir)
        head = gitsafe.git(["rev-parse", "HEAD"], self.tree, self.gitdir)
        self.assertEqual(plain("rev-parse", "refs/heads/job", cwd=self.origin).stdout.strip(), head)
        gitsafe.git(["fetch", "origin", "+refs/heads/job:refs/remotes/origin/job"], self.tree, self.gitdir)
        self.assertEqual(gitsafe.git(["rev-parse", "refs/remotes/origin/job"], self.tree, self.gitdir), head)
        bundle = self.root / "job.bundle"
        gitsafe.git(["bundle", "create", str(bundle), "^main", "refs/heads/job"], self.tree, self.gitdir)
        plain("bundle", "verify", str(bundle), cwd=self.repo)

    def test_submodule_update_works_under_the_explicit_git_directory(self):
        library = self.root / "library"
        library.mkdir()
        plain("init", "-q", "-b", "main", cwd=library)
        (library / "lib.txt").write_text("lib\n")
        plain("add", ".", cwd=library)
        plain(*IDENTITY, "commit", "-q", "-m", "lib", cwd=library)
        allow = ("-c", "protocol.file.allow=always")
        plain(*allow, "submodule", "add", "-q", str(library), "vendor", cwd=self.repo)
        plain(*IDENTITY, "commit", "-q", "-m", "submodule", cwd=self.repo)
        extra = self.root / "extra"
        plain("worktree", "add", "-q", "-b", "with-sub", str(extra), cwd=self.repo)
        gitdir = gitsafe.adopt(self.repo, extra)
        self.addCleanup(gitsafe.forget, extra)
        gitsafe.git([*allow, "submodule", "update", "--init", "--recursive"], extra, gitdir)
        self.assertEqual((extra / "vendor" / "lib.txt").read_text(), "lib\n")

    def test_the_error_carries_the_callers_argv_without_the_injected_pairs(self):
        command = ["rev-parse", "--verify", "refs/heads/none"]
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            gitsafe.git(command, self.tree, self.gitdir)
        self.assertEqual(caught.exception.cmd, ["git", *command])
        self.assertTrue(caught.exception.stderr)  # git's own message is kept for the caller to classify
        done = gitsafe.run(command, self.tree, self.gitdir, check=False)
        self.assertEqual(done.args, ["git", *command])
        with self.assertRaises(subprocess.CalledProcessError) as repo_error:
            gitsafe.repo_git(command, self.repo)
        self.assertEqual(repo_error.exception.cmd, ["git", *command])


class RunnerTests(Fixture):
    """The supervisor's own use: ``Runner`` through gitsafe, a real push to a bare origin included."""

    def setUp(self):
        super().setUp()
        self.job = assignment()["jobs"][0]
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        self.runner = Runner(self.repo, self.run_dir, assignment(), timeout=10)
        self.runner.state.observer = None  # no issue-label sync (it would call gh)
        self.key = self.job["id"]
        self.worktree = self.runner.prepare_worktree(self.job)
        self.addCleanup(gitsafe.forget, self.worktree)

    def state(self):
        return self.runner.state.data["jobs"][self.key]

    def refuse_pushes(self):
        hook = self.origin / "hooks" / "pre-receive"
        hook.write_text("\n".join(["#!/bin/sh", "cat >&2 <<'MESSAGE'", REFUSAL, "MESSAGE", "exit 1", ""]))
        hook.chmod(0o755)

    def commit_workflow(self):
        path = self.worktree / ".github" / "workflows" / "ci.yml"
        path.parent.mkdir(parents=True)
        path.write_text("name: ci\n")
        self.runner.stage_and_commit(self.job, self.worktree)
        return self.runner.git("rev-parse", "HEAD", cwd=self.worktree)

    def test_the_worktree_git_directory_is_recorded_in_the_job_state(self):
        self.assertEqual(self.state()["gitdir"], gitsafe.resolve_gitdir(self.repo, self.worktree))
        self.assertTrue(self.state()["gitdir"].startswith(str(self.repo / ".git" / "worktrees")))

    def test_a_rewritten_pointer_fails_the_next_call_but_not_the_clone(self):
        (self.worktree / ".git").write_text("gitdir: /nonexistent\n")
        with self.assertRaises(gitsafe.GitSafetyError):
            self.runner.git("status", cwd=self.worktree)
        with self.assertRaises(gitsafe.GitSafetyError):
            self.runner.is_ancestor("HEAD", "HEAD", self.worktree)
        self.assertEqual(self.runner.git("branch", "--show-current"), "main")

    def test_a_restarted_supervisor_checks_the_recorded_directory(self):
        gitsafe.forget(self.worktree)
        self.runner.state.update(self.key, gitdir=str(self.root / "elsewhere"))
        with self.assertRaises(gitsafe.GitSafetyError) as caught:
            self.runner.git("status", cwd=self.worktree)
        self.assertEqual(str(caught.exception), gitsafe.RECORDED_DIFFERS)
        self.runner.state.update(self.key, gitdir=gitsafe.resolve_gitdir(self.repo, self.worktree))
        self.assertEqual(self.runner.git("branch", "--show-current", cwd=self.worktree), self.state()["branch"])

    def test_a_planted_fsmonitor_runs_nothing_through_the_runner(self):
        program = self.program("fsmonitor.sh", "printf '\\0'")
        plain("config", "core.fsmonitor", str(program), cwd=self.worktree)
        (self.worktree / "f.txt").write_text("changed\n")
        plain("status", cwd=self.worktree, check=False)
        self.assertTrue(self.marker.exists(), "the plant did not fire")
        self.marker.unlink()
        self.runner.git("status", "--porcelain", cwd=self.worktree)
        self.runner.stage_and_commit(self.job, self.worktree)
        self.runner.conflicted_paths(self.worktree)
        self.runner.head_changes(self.worktree, "HEAD")
        self.assertFalse(self.marker.exists())

    def test_a_nested_repository_a_worker_planted_runs_nothing_through_the_runner(self):
        nested = self.worktree / "vendor" / "lib"
        nested.mkdir(parents=True)
        plain("init", "-q", "-b", "main", cwd=nested)
        (nested / ".gitattributes").write_text("* filter=evil\n")
        plain("config", "filter.evil.clean", str(self.program("clean-filter.sh", "cat")), cwd=nested)
        (nested / "f.txt").write_text("one\n")
        plain("add", "--all", cwd=nested)
        plain(*IDENTITY, "commit", "-q", "-m", "armed", cwd=nested)
        os.utime(nested / "f.txt", (1_000_000_000, 1_000_000_000))  # git must hash the file again, through its filter
        self.marker.unlink(missing_ok=True)
        self.runner.git("status", "--porcelain", "--untracked-files=all", cwd=self.worktree)
        with self.assertRaises(gitsafe.GitSafetyError):
            self.runner.stage_and_commit(self.job, self.worktree)
        self.assertFalse(self.marker.exists())
        self.assertNotIn("160000", plain("ls-files", "--stage", cwd=self.worktree).stdout)
        self.assertEqual(self.runner.git("branch", "--show-current", cwd=self.worktree), self.state()["branch"])

    def test_a_workflow_rejection_still_parks_the_job_for_a_person(self):
        self.refuse_pushes()
        sha = self.commit_workflow()
        branch = self.state()["branch"]
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            self.runner.push_branch(self.worktree, branch, None)
        self.assertEqual(list(caught.exception.cmd)[:2], ["git", "push"])
        self.assertNotIn("core.fsmonitor=false", caught.exception.cmd)
        self.runner.state.update(self.key, commit=sha, status="running")
        parked = self.runner.push_or_park(
            self.job, self.worktree, self.state(), "", {"pass": True}, {"complete": True}
        )
        self.assertFalse(parked)
        self.assertEqual(self.state()["status"], "needs_human_publish")
        self.assertEqual(self.state()["push_rejection"], publish_block.REJECTION_WORKFLOWS)
        self.assertTrue(Path(self.state()["human_publish"]["bundle"]).is_file())

    def test_another_rejection_is_not_parked(self):
        hook = self.origin / "hooks" / "pre-receive"
        hook.write_text("\n".join(["#!/bin/sh", "echo 'remote: protected branch' >&2", "exit 1", ""]))
        hook.chmod(0o755)
        self.commit_workflow()
        self.runner.state.update(self.key, commit=self.runner.git("rev-parse", "HEAD", cwd=self.worktree))
        with self.assertRaisesRegex(RuntimeError, "git push failed"):
            self.runner.push_or_park(self.job, self.worktree, self.state(), "", {"pass": True}, {"complete": True})
        self.assertEqual(self.state()["push_rejection"], publish_block.REJECTION_OTHER)

    def test_a_successful_push_goes_through(self):
        (self.worktree / "g.txt").write_text("new\n")
        self.runner.stage_and_commit(self.job, self.worktree)
        branch = self.state()["branch"]
        self.runner.push_branch(self.worktree, branch, None)
        head = self.runner.git("rev-parse", "HEAD", cwd=self.worktree)
        self.assertEqual(plain("rev-parse", f"refs/heads/{branch}", cwd=self.origin).stdout.strip(), head)


class ResumePathTests(Fixture):
    """``recovery`` revalidates a retained worktree after a restart: the same guarantees apply."""

    def setUp(self):
        super().setUp()
        self.head = gitsafe.git(["rev-parse", "HEAD"], self.tree, self.gitdir)
        self.plan = {"artifact_roots": ["plans/archive"], "jobs": [{"id": "job", "feature": True}]}
        self.state = State(self.root, self.plan)
        self.state.update(
            "job",
            status="pr_incomplete",
            branch="job",
            worktree=str(self.tree),
            commit=self.head,
            reviewed_commit=self.head,
            attempt=1,
            review={"pass": True, "findings": []},
            build={"blocked": False, "summary": "built", "tests": [], "feature": True},
        )
        gitsafe.forget(self.tree)  # a new process: nothing is registered yet

    def resume(self, phase="review"):
        return validate_resume(self.repo, self.plan, self.state, "job", phase)

    def test_a_clean_retained_worktree_still_validates(self):
        _, _, worktree, head = self.resume()
        self.assertEqual((worktree, head), (self.tree, self.head))
        self.assertEqual(gitsafe.trusted(self.tree), self.gitdir)

    def test_a_planted_fsmonitor_runs_nothing_on_the_resume_path(self):
        self.plant_fsmonitor()
        self.dirty()
        plain("status", cwd=self.tree, check=False)
        self.assertTrue(self.marker.exists(), "the plant did not fire")
        self.marker.unlink()
        plain("checkout", "--", "f.txt", cwd=self.tree, check=False)
        self.marker.unlink(missing_ok=True)  # the plain call above ran the plant too
        for phase in ("review", "verify", "publish"):
            with self.subTest(phase=phase):
                try:
                    self.resume(phase)
                except ValueError:
                    pass  # publish lacks verification evidence: only what ran matters here
                self.assertFalse(self.marker.exists())

    def test_a_replaced_git_directory_is_refused_on_the_resume_path(self):
        (self.tree / ".git").unlink()
        plain("init", "-q", cwd=self.tree)
        self.plant_fsmonitor()
        with self.assertRaisesRegex(ValueError, "pointer was changed"):
            self.resume()
        self.assertFalse(self.marker.exists())

    def test_a_rewritten_pointer_is_refused_on_the_resume_path(self):
        (self.tree / ".git").write_text("gitdir: /nonexistent\n")
        with self.assertRaisesRegex(ValueError, "pointer was changed"):
            self.resume()

    def test_a_worktree_the_clone_does_not_know_is_refused(self):
        stranger = self.root / "stranger"
        shutil.copytree(self.tree, stranger, symlinks=True)
        self.state.update("job", worktree=str(stranger))
        with self.assertRaisesRegex(ValueError, "not registered"):
            self.resume()

    def test_the_recorded_directory_must_match(self):
        self.state.update("job", gitdir=str(self.root / "elsewhere"))
        with self.assertRaisesRegex(ValueError, "differs from the recorded"):
            self.resume()


class AgentReadTests(Fixture):
    """``agent.repo_guidance`` and ``agent.git_common_dir`` read worker-touched trees."""

    def test_guidance_and_common_dir_of_a_registered_worktree(self):
        self.assertEqual(repo_guidance(self.tree, "HEAD"), "trusted rules\n")
        self.assertEqual(repo_guidance(self.tree, "no-such-ref"), "")
        self.assertEqual(git_common_dir(self.tree), self.repo / ".git")

    def test_a_planted_fsmonitor_runs_nothing(self):
        self.plant_fsmonitor()
        self.assertEqual(repo_guidance(self.tree, "HEAD"), "trusted rules\n")
        self.assertEqual(git_common_dir(self.tree), self.repo / ".git")
        self.assertFalse(self.marker.exists())

    def test_a_replaced_git_directory_cannot_supply_guidance_or_a_sandbox_path(self):
        (self.tree / ".git").unlink()
        plain("init", "-q", cwd=self.tree)
        (self.tree / "CLAUDE.md").write_text("planted instructions\n")
        plain("add", "CLAUDE.md", cwd=self.tree)
        plain(*IDENTITY, "commit", "-q", "-m", "planted", cwd=self.tree)
        self.plant_fsmonitor()
        for read in (lambda: repo_guidance(self.tree, "HEAD"), lambda: git_common_dir(self.tree)):
            with self.assertRaises(gitsafe.GitSafetyError):
                read()
        self.assertFalse(self.marker.exists())

    def test_an_unregistered_linked_worktree_is_refused(self):
        gitsafe.forget(self.tree)
        with self.assertRaises(gitsafe.GitSafetyError):
            repo_guidance(self.tree, "HEAD")
        with self.assertRaises(gitsafe.GitSafetyError):
            git_common_dir(self.tree)

    def test_a_directory_that_is_no_repository_still_yields_nothing(self):
        self.assertEqual(repo_guidance(self.root, "HEAD"), "")
        self.assertIsNone(git_common_dir(self.root))


if __name__ == "__main__":
    unittest.main()

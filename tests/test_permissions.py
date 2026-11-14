"""share_tree / share_git_dir must cope with entries the worker created.

After a build, the worktree holds files and directories owned by the worker uid. The
supervisor (not their owner, not root) cannot chown or chmod them: EPERM. Those entries
already carry the worker group and group write (setgid directory, worker umask 002), so
re-sharing must leave them alone instead of failing the review phase (observed on a real
cluster: "[Errno 1] Operation not permitted: .../worktrees/<job>/.claude").
"""

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from factory import permissions


def make_tree(root: Path, gid: int) -> None:
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.json").write_text("{}")
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("x = 1\n")
    (root / "README.md").write_text("hi\n")
    for path in (root, root / ".claude", root / "src"):
        os.chmod(path, 0o2775)
    for path in (root / ".claude" / "settings.json", root / "src" / "a.py", root / "README.md"):
        os.chmod(path, 0o664)


class ShareTreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.gid = os.getgid()
        make_tree(self.root, self.gid)

    def test_already_shared_entries_are_not_touched(self):
        with mock.patch("os.chown", side_effect=PermissionError(1, "Operation not permitted")) as chown, \
                mock.patch("os.chmod", side_effect=PermissionError(1, "Operation not permitted")) as chmod, \
                mock.patch("os.geteuid", return_value=os.geteuid() + 1):
            permissions.share_tree(self.root, self.gid)
        chown.assert_not_called()
        chmod.assert_not_called()

    def test_entries_owned_by_another_uid_that_need_a_change_are_skipped(self):
        os.chmod(self.root / ".claude" / "settings.json", 0o600)
        with mock.patch("os.chmod", side_effect=PermissionError(1, "Operation not permitted")), \
                mock.patch("os.geteuid", return_value=os.geteuid() + 1):
            permissions.share_tree(self.root, self.gid)  # must not raise

    def test_a_failure_on_an_entry_the_supervisor_owns_is_still_raised(self):
        os.chmod(self.root / "README.md", 0o600)
        with mock.patch("os.chmod", side_effect=PermissionError(1, "Operation not permitted")):
            with self.assertRaises(PermissionError):
                permissions.share_tree(self.root, self.gid)

    def test_missing_group_write_is_added(self):
        os.chmod(self.root / "README.md", 0o600)
        os.chmod(self.root / "src", 0o755)
        permissions.share_tree(self.root, self.gid)
        self.assertEqual(stat.S_IMODE((self.root / "README.md").stat().st_mode) & 0o660, 0o660)
        self.assertEqual(stat.S_IMODE((self.root / "src").stat().st_mode) & 0o2770, 0o2770)

    def test_symlinks_are_not_followed(self):
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: outside.rmdir())
        os.chmod(outside, 0o700)
        (self.root / "link").symlink_to(outside)
        permissions.share_tree(self.root, self.gid)
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o700)


class ShareGitDirTests(unittest.TestCase):
    def test_entries_that_are_already_readable_are_not_touched(self):
        with tempfile.TemporaryDirectory() as tmp:
            git = Path(tmp) / ".git"
            git.mkdir()
            (git / "HEAD").write_text("ref: refs/heads/main\n")
            os.chmod(git, 0o2750)
            os.chmod(git / "HEAD", 0o640)
            with mock.patch("os.chown", side_effect=PermissionError(1, "x")) as chown, \
                    mock.patch("os.chmod", side_effect=PermissionError(1, "x")) as chmod:
                permissions.share_git_dir(git, os.getgid())
            chown.assert_not_called()
            chmod.assert_not_called()


if __name__ == "__main__":
    unittest.main()

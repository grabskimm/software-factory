"""The files that say the cluster has no network policy engine: only the tests that assert the wording is gone may still carry it.

Issue #150 left the refresh of the stale NetworkPolicy comment to the change that makes an engine real: the worker namespace
policy of the pod backend (slice 15a, issues #144 and #145), which rewrote the comments of infra/k8s.tf, infra/monitoring.tf,
infra/variables.tf and the documents that carried the old wording. Two tests (infra/test/test_monitoring_design.py and
infra/test/test_worker_ns_design.py) assert that the old strings are GONE, so they are the files that still carry them and
the only ones that may (ALLOWED_CARRIERS below). A file that carries the old wording again is found here first.

It reads every file under infra/ and docs/ (whatever its suffix: .tf, .tftpl, .tfvars, .json, .hcl and the rest) except
hidden ones, symlinks, __pycache__ and files over 2 MiB. The docs are read too: none of them says it.

The test must not pass without finding anything: KNOWN_CARRIERS are files that carry the wording today, and the test
fails when one of them stops matching. That is the signal that the comments were refreshed (update the two tuples) or
that WORDING no longer matches (fix the pattern).
"""

import os
import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCANNED = ("infra", "docs")
MAX_BYTES = 2 * 1024 * 1024  # a larger file is not a comment or a document
# Files that carry the old wording (as strings that a test asserts are absent). At least these must be found, or the test would
# pass by finding nothing. Before slice 15a the carriers were infra/k8s.tf, infra/monitoring.tf and infra/variables.tf.
KNOWN_CARRIERS = ("infra/test/test_monitoring_design.py",)
# The only files that may carry it: the tests that assert it is gone.
ALLOWED_CARRIERS = (*KNOWN_CARRIERS, "infra/test/test_worker_ns_design.py")
# The files whose comments were rewritten in slice 15a: none of them may say again that no engine can exist.
REFRESHED = (
    "infra/k8s.tf",
    "infra/monitoring.tf",
    "infra/variables.tf",
    "infra/terraform.tfvars.example",
    "infra/README.md",
    "docs/metrics.md",
)
# "policy engine", also when a comment wraps it onto the next line, or the superseded issue number.
WORDING = re.compile(r"policy\s+(?:#\s*)?engine|issue #?122(?!\d)", re.IGNORECASE)


def skipped_name(name):
    return name.startswith(".") or name == "__pycache__"


def scanned_files(root=ROOT):
    """The files to read: every regular file of the scanned trees that is not hidden, not a symlink and not large.
    Symlinked directories are not entered."""
    found = []
    for name in SCANNED:
        for directory, subdirectories, filenames in os.walk(root / name, followlinks=False):
            subdirectories[:] = sorted(sub for sub in subdirectories if not skipped_name(sub))
            for filename in sorted(filenames):
                path = Path(directory) / filename
                relative = path.relative_to(root)
                if skipped_name(filename) or path.is_symlink() or not path.is_file():
                    continue
                if path.stat().st_size > MAX_BYTES:
                    continue
                found.append(path)
    return found


def carriers(root=ROOT):
    found = []
    for path in scanned_files(root):
        if WORDING.search(path.read_text(encoding="utf-8", errors="replace")):
            found.append(path.relative_to(root).as_posix())
    return found


class NetworkPolicyWordingIsAllowedOnlyInItsTests(unittest.TestCase):
    def test_every_file_with_the_old_wording_is_an_allowed_carrier(self):
        found = carriers()
        missing = [relative for relative in KNOWN_CARRIERS if relative not in found]
        self.assertEqual(
            missing,
            [],
            "these files no longer match WORDING. If their comments were refreshed on purpose, update KNOWN_CARRIERS "
            "and ALLOWED_CARRIERS in this test together. If they were not, WORDING stopped matching and this test "
            "would pass without checking anything",
        )
        for relative in found:
            with self.subTest(file=relative):
                self.assertIn(relative, ALLOWED_CARRIERS)

    def test_the_files_rewritten_for_the_worker_namespace_policy_no_longer_carry_the_old_wording(self):
        found = carriers()
        for relative in REFRESHED:
            with self.subTest(file=relative):
                self.assertTrue((ROOT / relative).is_file(), relative)
                self.assertNotIn(relative, found)
        # the comment of infra/k8s.tf no longer calls a policy decoration and says what enforces one
        header = (ROOT / "infra" / "k8s.tf").read_text(encoding="utf-8").split("\nlocals {", 1)[0]
        self.assertNotRegex(header, r"(?i)decoration")
        self.assertRegex(header, r'network_data_plane\s*=\s*(?:#\s*)?"cilium"')
        self.assertIn("worker_ns.tf", header)

    def test_the_wording_matches_a_comment_that_wraps_and_the_issue_number(self):
        for text in (
            "the cluster has no network policy engine",
            "# the cluster runs no network policy\n# engine, so one would be decoration",
            "NO NETWORK POLICY   ENGINE",
            "(Cilium is a separate piece of work, issue 122)",
            "once an engine exists (issue #122)",
        ):
            with self.subTest(text=text):
                self.assertRegex(text, WORDING)
        for text in ("a policy decision", "issue 1220 is open", "network policies and engines"):
            with self.subTest(text=text):
                self.assertNotRegex(text, WORDING)

    def test_the_scan_reads_every_kind_of_file_but_not_hidden_linked_cached_or_large_ones(self):
        wording = "# no network policy\n# engine\n"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            readable = ("infra/a.tf", "infra/zz.tfvars", "infra/sub/b.tftpl", "infra/c.json", "infra/d.hcl", "docs/e.md", "docs/f.txt", "docs/g", "docs/operations.md")
            ignored = ("infra/.hidden.tf", "infra/.dir/h.tf", "infra/__pycache__/i.py")
            for relative in (*readable, *ignored):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(wording, encoding="utf-8")
            (root / "infra" / "big.tf").write_text(wording + "x" * MAX_BYTES, encoding="utf-8")
            (root / "infra" / "binary.bin").write_bytes(b"\xff\xfe\x00no network policy\n# engine\x00")
            outside = root / "outside"
            outside.mkdir()
            (outside / "j.tf").write_text(wording, encoding="utf-8")
            try:
                (root / "infra" / "link.tf").symlink_to(outside / "j.tf")
                (root / "infra" / "linked").symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are not available here")
            self.assertEqual(set(carriers(root)), {*readable, "infra/binary.bin"})

    def test_the_scan_reads_the_infra_and_docs_trees(self):
        scanned = set(scanned_files())
        self.assertTrue(scanned, "no file is scanned")
        self.assertIn(ROOT / "infra" / "k8s.tf", scanned)
        self.assertIn(ROOT / "docs" / "pod-backend.md", scanned)

    def test_the_allowed_carriers_are_tests_that_assert_the_wording_is_gone(self):
        for relative in ALLOWED_CARRIERS:
            with self.subTest(file=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertRegex(text, r"assertNot(?:In|Regex)\(")  # it holds the wording only to assert that it is absent


if __name__ == "__main__":
    unittest.main()

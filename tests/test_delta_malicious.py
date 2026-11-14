"""The file delta a pod returns is hostile input (slice 9b of the pod backend): every malicious payload is rejected, nothing is written.

A pod ran ``claude``, which a prompt-injected model may have steered, and the supervisor applies what the pod sends
back to ITS OWN worktree with ITS privileges. The decisive property is checked here on real directories: for every
payload below the delta is rejected with a fixed code and the whole tree (the worktree, the directory a link points
at, the scratch directory; every file, link and directory, with its mode, inode and modification time) is
byte-identical afterwards. The payloads are written byte by byte (``rec``), not with ``bundle.Writer``, because the
writer refuses most of them. The round trips with real git repositories are in ``tests/test_bundle.py``.

This suite proves the supervisor's side only. It says nothing about what a pod does.
"""

import io
import os
import stat
import struct
import tempfile
import unicodedata
import unittest
from itertools import chain, product
from pathlib import Path
from unittest.mock import patch

from factory import bundle
from factory import podcontract as pc
from factory import sessionrecord as sr

FILE, SYMLINK, DELETE, BASELINE = bundle.FILE, bundle.SYMLINK, bundle.DELETE, bundle.BASELINE
REJECTED = "delta rejected"
TOO_LARGE = "delta too large"
FAILED = "delta could not be applied"
TAR_HARD_LINK = ord("1")  # the type byte a tar archive uses for a hard link
POISON = "SECRET-TOKEN-0123"


def rec(kind, path, payload=b"", flags=0, size=None):
    """One record as bytes. ``path`` is the whole wire path (``tree/...``) as text or bytes; nothing is checked."""
    raw = path if isinstance(path, bytes) else os.fsencode(path)
    head = struct.pack(">BBH", kind, flags, len(raw))
    return b"".join((head, raw, struct.pack(">Q", len(payload) if size is None else size), payload))


def delta(*records):
    return b"".join((bundle.HEADER, *records))


def snapshot(root):
    """Everything under ``root``, empty directories included, links not followed: type, mode, inode, mtime, bytes or target."""
    found = {}
    for directory, names, files in os.walk(root):
        for name in [*names, *files]:
            path = os.path.join(directory, name)
            info = os.lstat(path)
            key = os.path.relpath(path, root)
            if stat.S_ISLNK(info.st_mode):
                found[key] = ("link", os.readlink(path))
            elif stat.S_ISREG(info.st_mode):
                found[key] = ("file", info.st_mode & 0o7777, info.st_ino, info.st_mtime_ns, Path(path).read_bytes())
            else:
                found[key] = ("dir", info.st_mode & 0o7777, info.st_ino)
    return found


class Counting:
    """A source that remembers the largest read it was asked for (a hostile length must not make a huge one)."""

    def __init__(self, data):
        self.inner = io.BytesIO(data)
        self.largest = 0

    def read(self, count=-1):
        self.largest = max(self.largest, count)
        return self.inner.read(count)


class Fixture(unittest.TestCase):
    """A worktree with every kind of entry, a directory outside it that links could point at, and a scratch directory."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(os.path.realpath(temp.name))
        self.tree = self.root / "tree"
        self.outside = self.root / "outside"
        self.scratch = self.root / "scratch"
        for directory in (self.tree / "src" / "deep", self.tree / "empty", self.outside, self.scratch):
            directory.mkdir(parents=True)
        files = {
            "README.md": "readme\n",
            "src/app.py": "print(1)\n",
            "src/util.py": "x = 1\n",
            "src/deep/mod.py": "y = 2\n",
            ".git": "gitdir: /elsewhere/supervisor.git/worktrees/job\n",  # the supervisor's own pointer
            "run.sh": "#!/bin/sh\n",
        }
        for name, text in files.items():
            (self.tree / name).write_text(text)
        os.chmod(self.tree / "run.sh", 0o755)
        os.symlink("README.md", self.tree / "link")
        (self.outside / "canary.txt").write_text("canary\n")
        self.before = snapshot(self.root)

    def apply(self, data, scope="cwd", scratch_override=None, **extra):
        scratch = self.scratch if scratch_override is None else scratch_override
        return bundle.apply_delta(io.BytesIO(data), self.tree, write_scope=scope, scratch=scratch, **extra)

    def assert_untouched(self):
        self.assertEqual(snapshot(self.root), self.before)

    def reject(self, data, reason, error=bundle.DeltaRejected, text=REJECTED, **extra):
        """The delta is refused with the fixed code and text and not one byte of any tree has changed."""
        with self.assertRaises(error) as caught:
            self.apply(data, **extra)
        self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(str(caught.exception), text)
        self.assert_untouched()

    def leftovers(self):
        return [key for key in snapshot(self.root) if key.endswith(".factory-tmp")]


class PathRuleTests(Fixture):
    GIT_NAMES = [
        ".git",
        ".git/config",
        ".git/hooks/pre-commit",
        "sub/.git",
        ".GIT",
        ".GIT/x",
        ".Git/x",
        "src/.GiT/HEAD",
        "git~1",
        "GIT~1/x",
        ".git.",
        ".git ",
        ".git. .",
        ".g\u200bit",
        ".git\u200b",
        ".\u2060git",
        ".G\u200dIT",
        ".git\u00ad",
        "\uff0egit",
        ".git\u3164",
    ]

    def test_a_git_name_in_any_case_or_unicode_variant_is_rejected_for_every_kind_of_record(self):
        for name in self.GIT_NAMES:
            for kind, payload in ((FILE, b"x"), (DELETE, b""), (SYMLINK, b"README.md")):
                with self.subTest(name=name, kind=kind):
                    self.reject(delta(rec(kind, f"tree/{name}", payload)), "path")

    def test_the_fast_folding_gives_what_the_character_loop_gave(self):
        def loop(name):
            text = "".join(char for char in name if not bundle._ignorable(char))
            text = unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", text).casefold())
            return "".join(char for char in text if not bundle._ignorable(char)).rstrip(". ")

        for code in chain(range(0x30000), range(0xE0000, 0xE0200)):
            character = chr(code)
            if bundle.folded(character) != loop(character):
                self.fail(f"U+{code:04X} is folded differently")
        words = ["", ".", " . ", "README.MD", "a.", ".g\u200bit", "\ufb01le", "caf\u00e9", "cafe\u0301", "\u00df", "Stra\u00dfe.", "\udcff"]
        words.extend((".G\u200dIT. ", "\uff0egit", "a\u3164b", "x" * 4096, "\u00e9" * 4096))
        for word in words:
            self.assertEqual(bundle.folded(word), loop(word))
        self.assertEqual(bundle.folded("ABC"), "abc")
        self.assertTrue(bundle.is_git_name(".G\u200dIT"))
        self.assertFalse(bundle.is_git_name(".gitx"))

    def test_a_config_file_that_would_run_a_program_cannot_be_written_as_git_config(self):
        evil = b"[core]\n\tfsmonitor = touch pwned\n"
        for name in (".git/config", ".GIT/config", ".git/hooks/post-commit", "git~1/config"):
            with self.subTest(name=name):
                self.reject(delta(rec(FILE, f"tree/{name}", evil)), "path")
        self.reject(delta(rec(FILE, "git/config", evil)), "kind")  # the area of a staged git directory: not in a delta

    def test_names_that_only_look_like_a_git_directory_are_not_refused(self):
        names = [".github/workflows/ci.yml", ".gitignore", ".gitattributes", ".gitmodules", "git", "gitx", "digit", "a.git/b", ".git-x"]
        data = delta(*(rec(FILE, f"tree/{name}", b"ok") for name in names))
        self.assertEqual(self.apply(data).files, len(names))
        for name in names:
            self.assertEqual((self.tree / name).read_bytes(), b"ok")

    def test_traversal_and_unclean_paths_are_rejected(self):
        for path in ("..", "../x", "a/../b", "a/..", "./x", "a/./b", "a//b", "a/", "", "/abs", "x/../../outside/f"):
            with self.subTest(path=path):
                self.reject(delta(rec(FILE, f"tree/{path}", b"x")), "path")
                self.reject(delta(rec(DELETE, f"tree/{path}")), "path")
        for wire in ("/etc/passwd", "etc/passwd", "../outside/f", "tree", "treex/a", "TREE/a", ""):
            with self.subTest(wire=wire):
                self.reject(delta(rec(FILE, wire, b"x")), "path")

    def test_a_nul_a_backslash_an_overlong_path_and_an_overlong_name_are_rejected(self):
        self.reject(delta(rec(FILE, b"tree/a\x00b", b"x")), "path")
        self.reject(delta(rec(FILE, "tree/a\\b", b"x")), "path")
        self.reject(delta(rec(FILE, f"tree/{'p' * 5000}", b"x")), "path")
        self.reject(delta(rec(FILE, f"tree/{'n' * (bundle.NAME_MAX + 1)}", b"x")), "path")
        self.reject(delta(rec(FILE, "tree/{}".format("/".join(["d"] * (bundle.MAX_DEPTH + 1))), b"x")), "path")
        self.assertEqual(self.apply(delta(rec(FILE, f"tree/{'n' * bundle.NAME_MAX}", b"x"))).files, 1)

    def test_a_delete_outside_the_tree_or_of_git_is_rejected(self):
        for wire in ("tree/../outside/canary.txt", "tree/.git", "tree/.GIT", "git/HEAD", "read/src/app.py", "meta/manifest.json", "/etc/passwd"):
            with self.subTest(wire=wire):
                self.reject(delta(rec(DELETE, wire)), "kind" if wire.split("/")[0] in ("git", "read", "meta") else "path")
        self.assertTrue((self.outside / "canary.txt").exists())

    def test_the_other_areas_of_the_wire_format_are_not_accepted_in_a_delta(self):
        for area in ("git", "read", "meta"):
            for kind, payload in ((FILE, b"x"), (SYMLINK, b"a"), (DELETE, b"")):
                with self.subTest(area=area, kind=kind):
                    self.reject(delta(rec(kind, f"{area}/a", payload)), "kind")
        self.reject(delta(rec(BASELINE, "tree/a", bytes(33))), "kind")

    def test_no_error_carries_a_path_or_a_byte_of_the_delta(self):
        hostile = delta(rec(FILE, f"tree/{POISON}/.git", POISON.encode()))
        with self.assertRaises(bundle.DeltaRejected) as caught:
            self.apply(hostile)
        shown = " ".join((str(caught.exception), repr(caught.exception), repr(caught.exception.args), caught.exception.reason))
        self.assertNotIn(POISON, shown)
        self.assertEqual(caught.exception.args, (REJECTED,))


class KindAndFlagTests(Fixture):
    def test_every_kind_byte_outside_the_closed_set_is_rejected(self):
        for kind in range(256):
            if kind in (FILE, SYMLINK, DELETE):
                continue
            with self.subTest(kind=kind):
                self.reject(delta(rec(kind, "tree/a", b"x")), "kind")

    def test_the_byte_a_tar_hard_link_uses_is_rejected_with_a_payload_and_without_one(self):
        self.reject(delta(rec(TAR_HARD_LINK, "tree/hard", b"README.md")), "kind")
        self.reject(delta(rec(TAR_HARD_LINK, "tree/hard", b"")), "kind")
        self.reject(delta(rec(TAR_HARD_LINK, "tree/hard", b"../outside/canary.txt")), "kind")
        for typeflag in "0234567":  # a device, a fifo, a directory, a contiguous file, a link: every other tar type byte
            self.reject(delta(rec(ord(typeflag), "tree/x", b"")), "kind")

    def test_a_record_after_good_ones_with_an_unknown_kind_rejects_the_whole_delta(self):
        good = rec(FILE, "tree/README.md", b"changed\n")
        self.reject(delta(good, rec(DELETE, "tree/src/util.py"), rec(TAR_HARD_LINK, "tree/hard", b"x")), "kind")

    def test_only_the_exec_bit_exists_and_only_on_a_file(self):
        for flags in (0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0xFF, 0x03):  # setuid, setgid and sticky have no bit
            with self.subTest(flags=flags):
                self.reject(delta(rec(FILE, "tree/f", b"x", flags=flags)), "flags")
        self.reject(delta(rec(SYMLINK, "tree/l", b"README.md", flags=bundle.FLAG_EXEC)), "flags")
        self.reject(delta(rec(DELETE, "tree/README.md", flags=bundle.FLAG_EXEC)), "flags")

    def test_a_delete_with_a_payload_and_an_empty_link_are_rejected(self):
        self.reject(delta(rec(DELETE, "tree/README.md", b"x")), "size")
        self.reject(delta(rec(DELETE, "tree/README.md", b"", size=1)), "size")
        self.reject(delta(rec(DELETE, "tree/README.md", b"", size=2**40)), "size")
        self.reject(delta(rec(SYMLINK, "tree/l", b"")), "size")


class LinkTests(Fixture):
    def test_a_link_must_be_relative_and_stay_inside_the_tree_lexically(self):
        cases = {
            "/etc/passwd": "link",
            "/": "link",
            "../outside": "link",
            "..": "link",
            "../../x": "link",
            "x/../..": "link",
            "a/b/../../..": "link",
            "src/..": "link",  # a name and then a ..: refused even where it stays inside (see the chain tests)
            "src/../README.md": "link",
            "./src/../README.md": "link",
            ".git": "link",
            ".git/config": "link",
            "a/.GIT/b": "link",
            "a\\b": "link",
            "..\\x": "link",
            "a\x00b": "target",
        }
        for target, reason in cases.items():
            with self.subTest(target=target):
                self.reject(delta(rec(SYMLINK, "tree/l", target.encode())), reason)
        for target in ("../../x", "../.."):  # one directory down is one level of room, not two
            self.reject(delta(rec(SYMLINK, "tree/src/l", target.encode())), "link")

    def test_a_link_that_stays_inside_is_accepted_and_is_created_as_written(self):
        data = delta(
            rec(SYMLINK, "tree/l1", b"README.md"),
            rec(SYMLINK, "tree/l2", b"src/app.py"),
            rec(SYMLINK, "tree/src/l3", b"../README.md"),
            rec(SYMLINK, "tree/src/deep/l4", b"../.."),
            rec(SYMLINK, "tree/l5", b"."),
            rec(SYMLINK, "tree/l6", b"src/"),
        )
        self.assertEqual(self.apply(data).symlinks, 6)
        self.assertEqual(os.readlink(self.tree / "src" / "l3"), "../README.md")
        self.assertEqual(os.readlink(self.tree / "src" / "deep" / "l4"), "../..")
        self.assertTrue((self.outside / "canary.txt").exists())

    def test_a_dot_dot_after_a_name_is_refused_so_that_no_chain_of_links_can_climb_out_of_the_tree(self):
        # Each pair is accepted by a check that only counts depth: the first link is harmless, the second one is lexically fine,
        # and followed through the first it is the parent of the tree (the supervisor follows links when it reads what it applied).
        chains = {
            "a link to its own directory, then a way up through it": (("tree/b", b"."), ("tree/p", b"b/b/b/b/../../../../canary.txt")),
            "one hop up per link": (("tree/l1", b"."), ("tree/l2", b"l1/..")),
            "a link below the root": (("tree/a/l1", b".."), ("tree/b/l2", b"../a/l1/..")),
            "a long way down to a link, then a way up through it": (
                ("tree/x0/x1/x2/x3/x4/up", b"../../../../.."),
                ("tree/src/a.txt", b"../x0/x1/x2/x3/x4/up/../../..//dev/zero"),
            ),
        }
        for label, (first, second) in chains.items():
            with self.subTest(label):
                self.reject(delta(rec(SYMLINK, first[0], first[1]), rec(SYMLINK, second[0], second[1])), "link")
                self.reject(delta(rec(SYMLINK, second[0], second[1]), rec(SYMLINK, first[0], first[1])), "link")
        self.assertEqual(sorted(os.listdir(self.outside)), ["canary.txt"])

    def test_no_chain_of_accepted_links_leads_out_of_the_tree(self):
        """Every short target over a few names, in three directories: those the check accepts are made for real and all resolve inside the tree."""
        names = ("..", "k0", "k1", "src", "deep")  # k0 and k1 are the first two links made, so later targets go through them
        records, links = [], {}
        for size in (1, 2, 3):
            for combination in product(names, repeat=size):
                target = "/".join(combination)
                for directory in ("", "src/", "src/deep/"):
                    path = f"{directory}k{len(links)}"
                    try:
                        bundle._check_link(tuple(path.split("/")), target.encode())
                    except bundle.WireError:
                        continue
                    records.append(rec(SYMLINK, f"tree/{path}", target.encode()))
                    links[path] = target
        self.assertEqual((links["src/k0"], links["src/deep/k1"]), ("..", ".."))
        self.assertGreater(len(links), 150)  # not degenerate: many climb, many descend, many go through another link
        self.apply(delta(*records))
        root = os.path.realpath(self.tree)
        for path, target in links.items():
            resolved = os.path.realpath(self.tree / path)
            if not (resolved == root or resolved.startswith(f"{root}{os.sep}")):
                self.fail(f"{path} -> {target} resolves to {resolved}")

    def test_a_link_of_the_longest_target_the_operating_system_holds_applies_and_one_byte_more_is_refused_before_anything_is_written(self):
        longest = ("a/" * 2048)[: pc.PATH_MAX_BYTES - 1]
        self.assertEqual(len(longest), 4095)
        self.assertEqual(self.apply(delta(rec(DELETE, "tree/src/util.py"), rec(SYMLINK, "tree/l", longest.encode()))).symlinks, 1)
        self.assertEqual(os.readlink(self.tree / "l"), longest)
        self.before = snapshot(self.root)
        too_long = f"{longest}z".encode()
        self.assertEqual(len(too_long), pc.PATH_MAX_BYTES)  # inside the wire format's cap, which symlinkat does not take
        # the deletion comes first in the stream: it must not have run when the link is refused
        self.reject(delta(rec(DELETE, "tree/src/app.py"), rec(SYMLINK, "tree/l2", too_long)), "link")
        self.assertTrue((self.tree / "src" / "app.py").exists())

    def test_every_boundary_value_the_check_accepts_can_be_applied(self):
        deepest = "/".join(["d"] * (bundle.MAX_DEPTH - 1))  # 99 directories and a file: 100 components
        data = delta(
            rec(FILE, f"tree/{'n' * bundle.NAME_MAX}", b"longest name"),
            rec(FILE, f"tree/{deepest}/f", b"deepest path"),
            rec(SYMLINK, "tree/l", ("a/" * 2048)[: pc.PATH_MAX_BYTES - 1].encode()),
            rec(SYMLINK, f"tree/{deepest}/up", "/".join([".."] * (bundle.MAX_DEPTH - 1)).encode()),
        )
        self.assertEqual(self.apply(data).records, 4)
        self.assertEqual((self.tree / deepest / "f").read_bytes(), b"deepest path")
        self.assertEqual(os.path.realpath(self.tree / deepest / "up"), os.path.realpath(self.tree))

    def test_a_link_replaces_a_file_and_a_file_replaces_a_link_without_following_it(self):
        self.apply(delta(rec(SYMLINK, "tree/README.md", b"run.sh"), rec(FILE, "tree/link", b"now a file\n")))
        self.assertEqual(os.readlink(self.tree / "README.md"), "run.sh")
        self.assertFalse((self.tree / "link").is_symlink())
        self.assertEqual((self.tree / "link").read_bytes(), b"now a file\n")
        self.assertEqual((self.tree / "run.sh").read_text(), "#!/bin/sh\n")  # the target of the old link was never written


class ClashTests(Fixture):
    def test_the_same_path_twice_is_rejected(self):
        for first, second in ((FILE, FILE), (FILE, DELETE), (DELETE, DELETE), (FILE, SYMLINK), (SYMLINK, DELETE)):
            payload = {FILE: b"x", SYMLINK: b"README.md", DELETE: b""}
            with self.subTest(first=first, second=second):
                data = delta(rec(first, "tree/src/app.py", payload[first]), rec(second, "tree/src/app.py", payload[second]))
                self.reject(data, "duplicate")

    def test_two_paths_that_are_one_name_after_folding_are_rejected(self):
        pairs = [
            ("Readme", "README"),
            ("ReadMe.TXT", "readme.txt"),
            ("\ufb01le", "file"),  # the ligature fi, NFKC
            ("caf\u00e9", "cafe\u0301"),  # precomposed and decomposed
            ("a.", "a"),
            ("a ", "a"),
            ("a\u200bb", "ab"),
            ("\u00df", "ss"),  # casefold
            ("A/x", "a/y"),  # the directories collide, the files do not
            ("src/App.py", "src/app.py"),
            ("dir/A/x", "dir/a/y"),
        ]
        for left, right in pairs:
            for order in ((left, right), (right, left)):
                with self.subTest(order=order):
                    data = delta(rec(FILE, f"tree/{order[0]}", b"1"), rec(FILE, f"tree/{order[1]}", b"2"))
                    self.reject(data, "collision")
        self.reject(delta(rec(DELETE, "tree/src/app.py"), rec(FILE, "tree/src/APP.PY", b"x")), "collision")  # a case-only rename: refused

    def test_a_file_and_a_directory_of_one_name_are_rejected_in_either_order(self):
        for left, right in (("a", "a/b"), ("a/b", "a"), ("a/b", "a/b/c"), ("a", "a/b/c/d")):
            for order in ((left, right), (right, left)):
                with self.subTest(order=order):
                    self.reject(delta(rec(FILE, f"tree/{order[0]}", b"1"), rec(FILE, f"tree/{order[1]}", b"2")), "clash")
        self.reject(delta(rec(FILE, "tree/a", b"1"), rec(SYMLINK, "tree/a/b", b"x")), "clash")
        self.reject(delta(rec(DELETE, "tree/a"), rec(DELETE, "tree/a/b")), "clash")
        self.reject(delta(rec(DELETE, "tree/a/b"), rec(DELETE, "tree/a")), "clash")
        self.reject(delta(rec(FILE, "tree/a/b", b"1"), rec(FILE, "tree/x", b"x"), rec(FILE, "tree/a", b"2")), "clash")

    def test_a_file_written_through_a_symlinked_directory_is_rejected(self):
        # the link points at a directory inside the tree and, in the second case, at one outside it
        for target in (b"src", b"../outside", b"/etc"):
            with self.subTest(target=target):
                data = delta(rec(SYMLINK, "tree/d", target), rec(FILE, "tree/d/pwned", b"x"))
                self.reject(data, "link" if target != b"src" else "clash")
        self.reject(delta(rec(FILE, "tree/d/pwned", b"x"), rec(SYMLINK, "tree/d", b"src")), "clash")  # in the other order
        self.reject(delta(rec(SYMLINK, "tree/d", b"src"), rec(FILE, "tree/d/deep/er/pwned", b"x")), "clash")
        self.assertFalse((self.tree / "src" / "pwned").exists())

    def test_a_path_through_a_link_the_same_delta_creates_earlier_is_rejected_whatever_comes_between(self):
        data = delta(
            rec(SYMLINK, "tree/hop", b"src"),
            rec(FILE, "tree/unrelated", b"1"),
            rec(DELETE, "tree/README.md"),
            rec(FILE, "tree/hop/x", b"pwned"),
        )
        self.reject(data, "clash")

    def test_the_order_of_the_records_does_not_matter(self):
        records = [rec(FILE, "tree/a", b"1"), rec(FILE, "tree/a/b", b"2"), rec(FILE, "tree/c", b"3"), rec(FILE, "tree/c.", b"4")]
        count = len(records)
        for shift in range(count):
            with self.subTest(shift=shift):
                self.reject(delta(*records[shift:], *records[:shift]), "clash")
        for shift in range(2):  # only the collision left: it is found wherever the records are
            with self.subTest(only_collision=shift):
                self.reject(delta(*records[2 + shift :], *records[2 : 2 + shift]), "collision")

    def test_a_delete_below_a_file_the_delta_writes_and_the_other_way_round_is_a_transition_not_a_clash(self):
        # a file that becomes a directory, a directory that becomes a file: exactly one of the two paths is a delete
        data = delta(rec(DELETE, "tree/src/util.py"), rec(FILE, "tree/src/util.py/inner.txt", b"i"))
        self.assertEqual(self.apply(data).files, 1)
        self.assertEqual((self.tree / "src" / "util.py" / "inner.txt").read_bytes(), b"i")
        data = delta(rec(DELETE, "tree/src/deep/mod.py"), rec(FILE, "tree/src/deep", b"now a file"))
        self.assertEqual(self.apply(data).files, 1)
        self.assertEqual((self.tree / "src" / "deep").read_bytes(), b"now a file")


class ScopeTests(Fixture):
    def test_a_read_only_phase_must_return_an_empty_delta(self):
        for scope in ("none", "other", ""):
            applied = self.apply(delta(), scope=scope)
            self.assertEqual((applied.records, applied.files, applied.deleted), (0, 0, 0))
            self.assert_untouched()
        records = {
            "file": rec(FILE, "tree/new.txt", b"x"),
            "link": rec(SYMLINK, "tree/l", b"README.md"),
            "delete": rec(DELETE, "tree/README.md"),
            "empty file": rec(FILE, "tree/empty.txt", b""),
            "malformed": b"\x07",
            "unknown kind": rec(TAR_HARD_LINK, "tree/h", b"x"),
            "git name": rec(FILE, "tree/.git/config", b"x"),
        }
        for scope in ("none", "bogus", ""):
            for label, record in records.items():
                with self.subTest(scope=scope, record=label):
                    self.reject(delta(record), "scope", scope=scope)

    def test_the_write_scope_that_may_write_is_one_the_session_record_knows(self):
        self.assertIn(bundle.WRITE_SCOPE_CWD, sr.WRITE_SCOPES)
        self.assertEqual(self.apply(delta(rec(FILE, "tree/new.txt", b"x")), scope=bundle.WRITE_SCOPE_CWD).files, 1)

    def test_a_read_only_delta_with_a_broken_header_is_still_a_protocol_error(self):
        self.reject(b"", "truncated", scope="none")
        self.reject(b"NOTMAGIC\x00\x01", "magic", scope="none")
        self.reject(b"".join((bundle.MAGIC, b"\x00\x02")), "version", scope="none")


class CapTests(Fixture):
    def test_an_oversize_entry_fails_the_session_and_reads_nothing_big(self):
        lie = pc.FILE_MAX_BYTES + 1
        source = Counting(delta(rec(FILE, "tree/big", b"", size=lie)))
        with self.assertRaises(bundle.DeltaTooLarge) as caught:
            bundle.apply_delta(source, self.tree, write_scope="cwd", scratch=self.scratch)
        self.assertEqual((caught.exception.reason, str(caught.exception)), ("size", TOO_LARGE))
        self.assertLessEqual(source.largest, bundle.CHUNK_BYTES)  # the declared size allocated nothing
        self.assert_untouched()
        self.reject(delta(rec(FILE, "tree/big", b"", size=2**64 - 1)), "size", bundle.DeltaTooLarge, TOO_LARGE)
        self.reject(delta(rec(SYMLINK, "tree/l", b"", size=pc.PATH_MAX_BYTES + 1)), "size", bundle.DeltaTooLarge, TOO_LARGE)

    def test_the_byte_cap_is_enforced_before_the_payload_is_read(self):
        small = bundle.Limits(max_bytes=len(bundle.HEADER) + 100, max_files=100)
        self.reject(delta(rec(FILE, "tree/a", b"x" * 200)), "bytes", bundle.DeltaTooLarge, TOO_LARGE, limits=small)
        data = delta(rec(FILE, "tree/a", b"1"), rec(FILE, "tree/b", b"2"), rec(FILE, "tree/c", b"x" * 60))
        self.reject(data, "bytes", bundle.DeltaTooLarge, TOO_LARGE, limits=small)

    def test_the_record_cap_is_exact_and_a_breach_is_not_a_truncation(self):
        limits = bundle.Limits(max_bytes=10_000, max_files=3)
        three = [rec(FILE, f"tree/n{number}", b"x") for number in range(3)]
        self.assertEqual(self.apply(delta(*three), limits=limits).files, 3)
        self.before = snapshot(self.root)  # three files were written: the breach below is measured from here
        self.reject(delta(*three, rec(FILE, "tree/n3", b"x")), "files", bundle.DeltaTooLarge, TOO_LARGE, limits=limits)
        self.assertFalse((self.tree / "n3").exists())  # not even the first three were written

    def test_the_contract_cap_of_twenty_thousand_records_is_exact(self):
        records = [rec(FILE, f"tree/f{number}", b"") for number in range(pc.DELTA_MAX_FILES)]
        with bundle.validate_delta(io.BytesIO(delta(*records)), write_scope="cwd", scratch=self.scratch) as valid:
            self.assertEqual(valid.records, pc.DELTA_MAX_FILES)
        records.append(rec(FILE, "tree/one-more", b""))
        with self.assertRaises(bundle.DeltaTooLarge) as caught:
            bundle.validate_delta(io.BytesIO(delta(*records)), write_scope="cwd", scratch=self.scratch)
        self.assertEqual(caught.exception.reason, "files")

    def test_the_delta_byte_cap_of_the_contract_is_the_default(self):
        self.assertEqual(bundle.LIMITS[bundle.DELTA], bundle.Limits(pc.DELTA_MAX_BYTES, pc.DELTA_MAX_FILES))
        self.reject(delta(rec(FILE, "tree/f", b"", size=pc.DELTA_MAX_BYTES)), "size", bundle.DeltaTooLarge, TOO_LARGE)  # over one file's cap


    def test_the_directories_a_delta_names_are_capped_and_counted_exactly(self):
        shapes = {  # paths -> the distinct directories they name
            ("a/f",): 1,
            ("a/b/c/d/e/f",): 5,
            ("a/b/f1", "a/b/f2", "a/c/f3"): 3,  # a, a/b and a/c
            ("a/f", "b/f", "c/d/f"): 4,
            ("src/new/f", "src/deep/x/y", "src/f"): 4,  # src, src/new, src/deep, src/deep/x
        }
        for paths, count in shapes.items():
            data = delta(*(rec(FILE, f"tree/{path}", b"") for path in paths))
            with self.subTest(paths=paths):
                with patch.object(bundle, "DELTA_MAX_DIRECTORIES", count):
                    with bundle.validate_delta(io.BytesIO(data), write_scope="cwd", scratch=self.scratch):
                        pass
                with patch.object(bundle, "DELTA_MAX_DIRECTORIES", count - 1):
                    self.reject(data, "directories", bundle.DeltaTooLarge, TOO_LARGE)
        data = delta(rec(DELETE, "tree/src/app.py"), rec(DELETE, "tree/src/util.py"), rec(FILE, "tree/src/deep/new", b"x"))
        with patch.object(bundle, "DELTA_MAX_DIRECTORIES", 2):  # src and src/deep, whatever the kind of the records
            self.assertEqual(self.apply(data).records, 3)
        self.assertEqual(bundle.DELTA_MAX_DIRECTORIES, 2 * pc.DELTA_MAX_FILES)

    def test_a_small_delta_that_would_make_millions_of_directories_is_too_large_and_writes_nothing(self):
        """The records are within every cap of the contract; each one names 98 directories of its own."""
        deep = "/".join(["a"] * 98)
        records = [rec(FILE, f"tree/d{number}/{deep}/f", b"") for number in range(bundle.DELTA_MAX_DIRECTORIES // 98 + 2)]
        data = delta(*records)
        self.assertLess(len(records), pc.DELTA_MAX_FILES)
        self.assertLess(len(data), 1024 * 1024)
        self.reject(data, "directories", bundle.DeltaTooLarge, TOO_LARGE)  # and every tree is byte-identical afterwards

    def test_deletions_below_one_deep_directory_cost_the_directories_and_not_the_paths_times_their_depth(self):
        chain = "/".join(f"d{level}" for level in range(98))
        os.makedirs(self.tree / chain)
        names = [f"f{number}" for number in range(300)]
        for name in names:
            (self.tree / chain / name).write_bytes(b"x")
        data = delta(*(rec(DELETE, f"tree/{chain}/{name}") for name in names))
        with patch.object(bundle, "_prune_order", wraps=bundle._prune_order) as order:
            applied = self.apply(data)
        self.assertEqual((applied.deleted, order.call_count), (300, 1))  # once, for the tree check and for the apply together
        self.assertFalse((self.tree / "d0").exists())  # the whole chain was emptied and went
        with bundle.validate_delta(io.BytesIO(data), write_scope="cwd", scratch=self.scratch) as valid:
            self.assertEqual(len(valid.prune_order), 98)  # the directories above the files, once each: d0 and every one below it
            self.assertIs(valid.prune_order, valid.prune_order)
            self.assertEqual(valid.prune_order[0], tuple(f"d{level}" for level in range(98)))  # deepest first
            self.assertEqual(valid.prune_order[-1], ("d0",))


class SpoolTests(Fixture):
    def test_a_scratch_directory_that_cannot_hold_the_private_copy_fails_with_a_fixed_text_and_writes_nothing(self):
        data = delta(rec(FILE, "tree/README.md", b"x" * 100))
        with patch.object(bundle, "SPOOL_MEMORY_BYTES", 10), self.assertRaises(bundle.DeltaApplyFailed) as caught:
            self.apply(data, scratch_override=self.root / "no" / "such" / "directory")
        self.assertEqual((caught.exception.reason, str(caught.exception)), ("spool", FAILED))
        self.assert_untouched()

    def test_a_source_that_cannot_be_read_is_rejected(self):
        class Broken:
            def read(self, count):
                raise OSError(5, "input error", POISON)

        with self.assertRaises(bundle.DeltaRejected) as caught:
            bundle.apply_delta(Broken(), self.tree, write_scope="cwd", scratch=self.scratch)
        self.assertEqual((caught.exception.reason, POISON in str(caught.exception)), ("read", False))
        self.assert_untouched()


class StreamTests(Fixture):
    GOOD = (rec(FILE, "tree/README.md", b"changed\n"), rec(DELETE, "tree/src/util.py"), rec(SYMLINK, "tree/l", b"src/app.py"))

    def test_a_size_that_lies_is_rejected_both_ways(self):
        self.reject(delta(rec(FILE, "tree/a", b"abc", size=10)), "truncated")  # more than the stream holds
        self.reject(delta(rec(FILE, "tree/a", b"abc", size=pc.FILE_MAX_BYTES)), "truncated")
        # fewer than the stream holds: the rest is read as the next record, and it is not one
        for rest in (b"abcdef", b"".join((rec(FILE, "tree/b", b"x"), b"\x00")), b"\x00" * 40):
            with self.subTest(rest=rest):
                self.reject_stream(delta(rec(FILE, "tree/a", rest, size=2)))

    def test_a_stream_cut_anywhere_is_rejected_unless_the_cut_is_a_record_boundary_and_then_the_declared_numbers_catch_it(self):
        full = delta(*self.GOOD)
        total = len(full)
        boundaries, offset = set(), len(bundle.HEADER)
        boundaries.add(offset)
        for record in self.GOOD:
            offset += len(record)
            boundaries.add(offset)
        for cut in range(total):
            with self.subTest(cut=cut):
                if cut in boundaries:
                    self.reject(full[:cut], "declared", declared_records=len(self.GOOD), declared_bytes=total)
                else:
                    self.reject(full[:cut], "truncated", declared_records=len(self.GOOD), declared_bytes=total)
                    self.reject(full[:cut], "truncated")

    def test_a_cut_at_a_record_boundary_is_a_valid_shorter_delta_without_the_declared_numbers(self):
        # The format has no trailer (stated in docs/pod-backend.md): this pins the limitation, and the caller closes it.
        cut = len(bundle.HEADER) + len(self.GOOD[0])
        with bundle.validate_delta(io.BytesIO(delta(*self.GOOD)[:cut]), write_scope="cwd", scratch=self.scratch) as valid:
            self.assertEqual(valid.records, 1)

    def test_declared_numbers_that_do_not_match_reject_the_delta(self):
        full = delta(*self.GOOD)
        self.reject(full, "declared", declared_records=2)
        self.reject(full, "declared", declared_records=4)
        self.reject(full, "declared", declared_bytes=len(full) - 1)
        self.reject(full, "declared", declared_bytes=len(full) + 1)
        applied = self.apply(full, declared_records=3, declared_bytes=len(full))
        self.assertEqual((applied.records, applied.size), (3, len(full)))

    def reject_stream(self, data):
        """The stream is refused as a protocol error (which one depends on where the garbage lands) and nothing changed."""
        with self.assertRaises(bundle.DeltaRejected) as caught:
            self.apply(data)
        self.assertIn(caught.exception.reason, ("truncated", "flags", "kind", "path", "size", "duplicate", "clash", "collision"))
        self.assert_untouched()

    def test_trailing_garbage_is_rejected(self):
        full = delta(*self.GOOD)
        for junk in (b"\x00", b"\x01", b"junk", b"\xff" * 40, bundle.HEADER, b"\x01\x00", b"".join((b"\x01\x00\x00\x03", b"abc"))):
            with self.subTest(junk=junk):
                self.reject_stream(b"".join((full, junk)))

    def test_a_broken_header_is_rejected(self):
        self.reject(b"", "truncated")
        self.reject(b"FCTYBNDL", "truncated")
        self.reject(b"FCTYBNDX\x00\x01", "magic")
        self.reject(b"".join((bundle.MAGIC, b"\x00\x00")), "version")
        self.reject(b"".join((bundle.MAGIC, b"\x01\x00")), "version")
        self.reject(b"".join((bundle.MAGIC, b"\x00\x02")), "version")

    def test_a_record_count_that_lies_cannot_pass_the_reader(self):
        # there is no count in the format: the reader counts what it reads against the cap, so a delta cannot understate it
        limits = bundle.Limits(max_bytes=1_000_000, max_files=2)
        self.reject(delta(*self.GOOD), "files", bundle.DeltaTooLarge, TOO_LARGE, limits=limits)


class WholeDeltaTests(Fixture):
    BAD = {
        "stream": rec(TAR_HARD_LINK, "tree/hard", b"x"),
        "git name": rec(FILE, "tree/.git/config", b"[core]\nfsmonitor = x\n"),
        "link": rec(SYMLINK, "tree/l", b"/etc/passwd"),
        "clash": rec(FILE, "tree/README.md/inside", b"x"),
        "through a link": rec(FILE, "tree/link/inside", b"x"),
        "a directory deleted": rec(DELETE, "tree/src"),
        "a directory replaced": rec(FILE, "tree/src", b"x"),
    }
    GOOD = (
        rec(FILE, "tree/README.md", b"changed\n"),
        rec(FILE, "tree/brand/new/file.txt", b"new\n", flags=bundle.FLAG_EXEC),
        rec(DELETE, "tree/src/util.py"),
        rec(SYMLINK, "tree/newlink", b"README.md"),
        rec(DELETE, "tree/run.sh"),
    )

    def test_one_bad_record_anywhere_rejects_all_of_it_and_nothing_is_written(self):
        for label, bad in self.BAD.items():
            slots = len(self.GOOD) + 1
            for position in range(slots):
                with self.subTest(bad=label, position=position):
                    records = [*self.GOOD[:position], bad, *self.GOOD[position:]]
                    with self.assertRaises(bundle.DeltaRejected):
                        self.apply(delta(*records))
                    self.assert_untouched()

    def test_a_delta_that_applies_cleanly_changes_exactly_what_it_names_and_never_the_git_pointer(self):
        pointer = (self.tree / ".git").read_bytes()
        applied = self.apply(delta(*self.GOOD))
        self.assertEqual((applied.records, applied.files, applied.symlinks, applied.deleted), (5, 2, 1, 2))
        self.assertEqual((self.tree / "README.md").read_bytes(), b"changed\n")
        self.assertEqual((self.tree / "brand" / "new" / "file.txt").read_bytes(), b"new\n")
        self.assertFalse((self.tree / "src" / "util.py").exists())
        self.assertFalse((self.tree / "run.sh").exists())
        self.assertEqual(os.readlink(self.tree / "newlink"), "README.md")
        self.assertEqual((self.tree / ".git").read_bytes(), pointer)
        self.assertEqual(snapshot(self.outside), {key.split("/", 1)[1]: value for key, value in self.before.items() if key.startswith("outside/")})
        self.assertEqual(self.leftovers(), [])


class TreeStateTests(Fixture):
    """What the tree looks like BEFORE the delta: a link planted by an earlier worker, a file where a directory goes."""

    def plant(self):
        os.symlink("../outside", self.tree / "linkdir")
        os.symlink("../outside/canary.txt", self.tree / "linkfile")
        self.before = snapshot(self.root)

    def test_nothing_is_written_or_deleted_through_a_directory_link_that_is_already_there(self):
        self.plant()
        self.reject(delta(rec(FILE, "tree/linkdir/pwned", b"x")), "path")
        self.reject(delta(rec(DELETE, "tree/linkdir/canary.txt")), "path")
        self.reject(delta(rec(SYMLINK, "tree/linkdir/l", b"README.md")), "path")
        self.reject(delta(rec(FILE, "tree/linkdir/deep/pwned", b"x")), "path")
        self.reject(delta(rec(FILE, "tree/README.md", b"ok"), rec(FILE, "tree/linkdir/pwned", b"x")), "path")
        self.assertEqual(sorted(os.listdir(self.outside)), ["canary.txt"])

    def test_a_link_that_the_delta_deletes_first_is_not_in_the_way(self):
        self.plant()
        data = delta(rec(DELETE, "tree/linkdir"), rec(FILE, "tree/linkdir/real.txt", b"in the tree"))
        self.assertEqual(self.apply(data).files, 1)
        self.assertEqual((self.tree / "linkdir" / "real.txt").read_bytes(), b"in the tree")
        self.assertFalse((self.tree / "linkdir").is_symlink())
        self.assertEqual(sorted(os.listdir(self.outside)), ["canary.txt"])

    def test_a_file_or_a_link_in_the_place_of_a_directory_is_in_the_way(self):
        self.plant()
        self.reject(delta(rec(FILE, "tree/README.md/x", b"1")), "path")
        self.reject(delta(rec(DELETE, "tree/README.md/x")), "path")
        self.reject(delta(rec(FILE, "tree/link/x", b"1")), "path")
        self.reject(delta(rec(FILE, "tree/src/app.py/deep/x", b"1")), "path")

    def test_a_directory_cannot_be_deleted_or_replaced_by_a_file_or_a_link(self):
        self.reject(delta(rec(DELETE, "tree/src")), "clash")
        self.reject(delta(rec(DELETE, "tree/empty")), "clash")
        self.reject(delta(rec(FILE, "tree/src", b"x")), "clash")
        self.reject(delta(rec(SYMLINK, "tree/src", b"README.md")), "clash")
        self.reject(delta(rec(FILE, "tree/src/deep", b"x")), "clash")
        self.reject(delta(rec(DELETE, "tree/src/app.py"), rec(FILE, "tree/src", b"x")), "clash")  # src still has other files

    def test_a_directory_that_the_deletions_empty_can_become_a_file(self):
        data = delta(rec(DELETE, "tree/src/deep/mod.py"), rec(FILE, "tree/src/deep", b"file now"))
        self.apply(data)
        self.assertEqual((self.tree / "src" / "deep").read_bytes(), b"file now")

    def test_a_deletion_of_something_that_is_gone_is_fine_so_that_a_delta_applies_twice(self):
        data = delta(rec(DELETE, "tree/src/util.py"), rec(DELETE, "tree/no/such/file"), rec(FILE, "tree/new.txt", b"n"))
        first = self.apply(data)
        once = stable(self.tree)
        self.assertEqual(self.apply(data), first)
        self.assertEqual(stable(self.tree), once)
        self.assertEqual(first.deleted, 2)

    def test_deletions_remove_the_directories_they_emptied_and_only_those(self):
        self.apply(delta(rec(DELETE, "tree/src/deep/mod.py")))
        self.assertFalse((self.tree / "src" / "deep").exists())
        self.assertTrue((self.tree / "src").is_dir())
        self.assertTrue((self.tree / "empty").is_dir())  # an empty directory the delta does not touch stays
        self.apply(delta(rec(DELETE, "tree/src/app.py"), rec(DELETE, "tree/src/util.py")))
        self.assertFalse((self.tree / "src").exists())


def stable(root):
    """``snapshot`` without the inode and the modification time, which a second application of a delta renews."""
    return {key: (value[:2], value[4:]) if value[0] == "file" else value for key, value in snapshot(root).items()}


class RaceTests(Fixture):
    """The tree is changed between the steps by an injected hook: nothing is written through what the hook planted."""

    def swap_for_link(self, name):
        directory = self.tree / name
        directory.rename(self.root / f"{name}.moved")
        os.symlink(self.outside, directory)

    def run_with(self, data, stage, hook):
        def checkpoint(seen):
            if seen == stage:
                hook()

        return bundle.apply_delta(io.BytesIO(data), self.tree, write_scope="cwd", scratch=self.scratch, checkpoint=checkpoint)

    def test_a_directory_swapped_for_a_link_after_validation_is_caught_by_the_tree_check(self):
        data = delta(rec(FILE, "tree/src/new.txt", b"pwned"))
        with self.assertRaises(bundle.DeltaRejected) as caught:
            self.run_with(data, "validated", lambda: self.swap_for_link("src"))
        self.assertEqual((caught.exception.reason, str(caught.exception)), ("path", REJECTED))
        self.assertEqual(sorted(os.listdir(self.outside)), ["canary.txt"])
        self.assertEqual(self.leftovers(), [])

    def test_a_directory_swapped_for_a_link_after_the_tree_check_is_caught_by_the_no_follow_open(self):
        (self.outside / "app.py").write_text("not yours\n")  # what a deletion that followed the link would remove
        data = delta(rec(FILE, "tree/README.md", b"first\n"), rec(FILE, "tree/src/new.txt", b"pwned"), rec(DELETE, "tree/src/app.py"))
        with self.assertRaises(bundle.DeltaApplyFailed) as caught:
            self.run_with(data, "checked", lambda: self.swap_for_link("src"))
        self.assertEqual((caught.exception.reason, str(caught.exception)), ("path", FAILED))
        self.assertEqual(sorted(os.listdir(self.outside)), ["app.py", "canary.txt"])  # nothing went through the link, the deletion included
        self.assertEqual((self.outside / "app.py").read_text(), "not yours\n")
        self.assertEqual(self.leftovers(), [])
        self.assertTrue((self.root / "src.moved" / "app.py").exists())

    def test_a_failure_part_way_leaves_the_worktree_as_it_is_with_no_temporary_file(self):
        data = delta(rec(FILE, "tree/README.md", b"applied\n"), rec(FILE, "tree/src/app.py", b"never\n"))

        def swap_file_for_directory():
            os.unlink(self.tree / "src" / "app.py")
            os.mkdir(self.tree / "src" / "app.py")

        with self.assertRaises(bundle.DeltaApplyFailed) as caught:
            self.run_with(data, "checked", swap_file_for_directory)
        self.assertEqual((caught.exception.reason, str(caught.exception)), ("write", FAILED))
        self.assertEqual((self.tree / "README.md").read_bytes(), b"applied\n")  # the first record was applied: nothing is rolled back
        self.assertTrue((self.tree / "src" / "app.py").is_dir())
        self.assertEqual(self.leftovers(), [])

    def test_a_deletion_never_removes_a_directory_that_took_the_place_of_the_file(self):
        def swap():
            os.unlink(self.tree / "src" / "util.py")
            os.mkdir(self.tree / "src" / "util.py")
            (self.tree / "src" / "util.py" / "precious.txt").write_text("keep\n")

        with self.assertRaises(bundle.DeltaApplyFailed):
            self.run_with(delta(rec(DELETE, "tree/src/util.py")), "checked", swap)
        self.assertEqual((self.tree / "src" / "util.py" / "precious.txt").read_text(), "keep\n")

    def test_the_worktree_path_swapped_for_a_link_before_the_tree_is_opened_is_refused(self):
        def swap():
            self.tree.rename(self.root / "tree.moved")
            os.symlink(self.outside, self.tree)

        with self.assertRaises(bundle.DeltaRejected) as caught:
            self.run_with(delta(rec(FILE, "tree/new.txt", b"x")), "validated", swap)
        self.assertEqual(caught.exception.reason, "worktree")
        self.assertEqual(sorted(os.listdir(self.outside)), ["canary.txt"])

    def test_the_worktree_is_pinned_by_its_descriptor_once_it_is_open(self):
        def swap():
            self.tree.rename(self.root / "tree.moved")
            os.symlink(self.outside, self.tree)

        applied = self.run_with(delta(rec(FILE, "tree/new.txt", b"x")), "checked", swap)
        self.assertEqual(applied.files, 1)
        self.assertEqual(sorted(os.listdir(self.outside)), ["canary.txt"])  # it went into the directory that was opened
        self.assertEqual((self.root / "tree.moved" / "new.txt").read_bytes(), b"x")

    def test_a_temporary_name_that_already_exists_as_a_link_is_not_followed_or_removed(self):
        planted = self.tree / ".abababababababab.factory-tmp"
        os.symlink(self.outside / "canary.txt", planted)
        for record in (rec(FILE, "tree/new.txt", b"pwned"), rec(SYMLINK, "tree/newlink", b"README.md")):
            with patch.object(bundle.secrets, "token_hex", return_value="ab" * 8), self.assertRaises(bundle.DeltaApplyFailed) as caught:
                self.apply(delta(record))
            self.assertEqual((caught.exception.reason, str(caught.exception)), ("write", FAILED))
            self.assertEqual((self.outside / "canary.txt").read_text(), "canary\n")
            self.assertTrue(planted.is_symlink())  # it is not ours: left where it is
            self.assertFalse((self.tree / "new.txt").exists())

    def test_what_is_applied_is_what_was_validated_even_if_the_source_changes_afterwards(self):
        source = io.BytesIO(delta(rec(FILE, "tree/README.md", b"validated\n")))
        evil = delta(rec(FILE, "tree/.git/config", b"[core]\nfsmonitor = x\n"), rec(FILE, "tree/pwned", b"x"))
        with bundle.validate_delta(source, write_scope="cwd", scratch=self.scratch) as valid:
            source.seek(0)
            source.truncate()
            source.write(evil)
            root_fd = os.open(self.tree, os.O_RDONLY | os.O_DIRECTORY)
            try:
                valid.check_tree(root_fd)
                valid.apply(root_fd)
            finally:
                os.close(root_fd)
        self.assertEqual((self.tree / "README.md").read_bytes(), b"validated\n")
        self.assertFalse((self.tree / "pwned").exists())

    def test_a_delta_larger_than_the_memory_threshold_is_applied_and_leaves_no_file_in_the_scratch_directory(self):
        payload = bytes(bundle.SPOOL_MEMORY_BYTES + 1024)
        applied = self.apply(delta(rec(FILE, "tree/big.bin", payload), rec(FILE, "tree/small", b"s")))
        self.assertEqual(applied.files, 2)
        self.assertEqual((self.tree / "big.bin").read_bytes(), payload)
        self.assertEqual(os.listdir(self.scratch), [])


class ModeTests(Fixture):
    def test_only_the_exec_bit_is_honoured_whatever_the_umask(self):
        for mask in (0o000, 0o077, 0o022):
            records = delta(
                rec(FILE, f"tree/plain{mask}", b"p"),
                rec(FILE, f"tree/exec{mask}", b"e", flags=bundle.FLAG_EXEC),
                rec(FILE, "tree/README.md", b"replaced"),
            )
            previous = os.umask(mask)
            try:
                self.apply(records)
            finally:
                os.umask(previous)
            with self.subTest(umask=oct(mask)):
                self.assertEqual(stat.S_IMODE(os.lstat(self.tree / f"plain{mask}").st_mode), 0o644)
                self.assertEqual(stat.S_IMODE(os.lstat(self.tree / f"exec{mask}").st_mode), 0o755)
                self.assertEqual(stat.S_IMODE(os.lstat(self.tree / "README.md").st_mode), 0o644)

    def test_a_file_replaced_without_the_exec_bit_loses_it_and_one_with_it_gains_it(self):
        self.apply(delta(rec(FILE, "tree/run.sh", b"#!/bin/sh\n"), rec(FILE, "tree/README.md", b"r", flags=bundle.FLAG_EXEC)))
        self.assertEqual(stat.S_IMODE(os.lstat(self.tree / "run.sh").st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(os.lstat(self.tree / "README.md").st_mode), 0o755)

    def test_an_existing_file_is_replaced_by_a_new_one_and_a_hard_link_to_it_is_not_written(self):
        os.link(self.tree / "README.md", self.outside / "hardlink.txt")
        self.apply(delta(rec(FILE, "tree/README.md", b"replaced\n")))
        self.assertEqual((self.outside / "hardlink.txt").read_text(), "readme\n")  # the rename replaced the entry, not the inode


if __name__ == "__main__":
    unittest.main()

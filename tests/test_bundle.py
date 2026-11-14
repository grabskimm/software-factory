"""The bundle (slice 9a of the pod backend): the wire format, the safe reading of a worktree and ship-in staging.

Three groups. The wire format is tested on bytes only (round trips, every unknown kind byte, hostile lengths, every
cut-off point). Staging and extraction are tested on real directories, and the git answers on REAL local git
repositories: the worker's ``diff``, ``log base..HEAD``, ``status`` and ``show HEAD:CLAUDE.md`` in the extracted
copy must equal the ones in the supervisor's worktree (in-process mode), also for a merged and a conflicted tree.
Nothing here runs a pod; the pod side is ``extract`` against a temporary directory.
"""

import contextlib
import hashlib
import io
import itertools
import json
import os
import shutil
import socket
import stat
import struct
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from factory import bundle, gitsafe
from factory import podcontract as pc
from factory import sessionrecord as sr

from tests import test_branch_sync
from tests.test_factory import assignment

IDENTITY = ("-c", "user.name=T", "-c", "user.email=t@example.invalid")
SMALL = bundle.Limits(max_bytes=10_000_000, max_files=1000)
STAMPS = itertools.count(1)


def plain(*args, cwd, check=True):
    """A plain git call as a person would run it (the fixture's own setup, not the code under test)."""
    return subprocess.run(["git", *IDENTITY, *args], cwd=cwd, text=True, capture_output=True, check=check)


def raw_record(kind, path, size, payload=b"", flags=0, length=None):
    """One record as bytes, written by hand so that a test can break it on purpose."""
    head = struct.pack(">BBH", kind, flags, len(path) if length is None else length)
    return b"".join((head, path, struct.pack(">Q", size), payload))


def stream(*records):
    return io.BytesIO(b"".join((bundle.HEADER, *records)))


def drain(source, direction=bundle.BUNDLE):
    """Every record of a stream with its payload (one at a time, as a decoder uses it)."""
    return [(r.kind, r.executable, r.area, r.path, r.read(pc.FILE_MAX_BYTES)) for r in bundle.Reader(source, direction)]


def without_git(files):
    """``files`` minus the worktree's own ``.git`` (a pointer in the supervisor's tree, a directory in the pod's)."""
    return {key: value for key, value in files.items() if key != ".git" and not key.startswith(".git/")}


class Counting:
    """A stream that remembers the largest read it was asked for (a hostile length must not make a huge one)."""

    def __init__(self, data):
        self.inner = io.BytesIO(data)
        self.largest = 0

    def read(self, count=-1):
        self.largest = max(self.largest, count)
        return self.inner.read(count)


class WireFormatTests(unittest.TestCase):
    def test_every_kind_round_trips_in_the_direction_that_has_it(self):
        sink = io.BytesIO()
        writer = bundle.Writer(sink, bundle.BUNDLE)
        writer.put_manifest({"worktree": "w", "n": 1})
        writer.put(bundle.FILE, bundle.AREA_TREE, "a/b.txt", b"hello", executable=True)
        writer.put(bundle.SYMLINK, bundle.AREA_TREE, "l", b"a/b.txt")
        writer.put_baseline("a/b.txt", bundle.FILE, b"\x01" * 32, executable=True)
        writer.put(bundle.FILE, bundle.AREA_GIT, "HEAD", b"ref")
        writer.put(bundle.FILE, bundle.AREA_READ, "x/evidence.json", b"{}")
        got = drain(io.BytesIO(sink.getvalue()))
        self.assertEqual([item[0] for item in got], [1, 1, 2, 4, 1, 1])
        self.assertEqual(got[1][1:], (True, "tree", "a/b.txt", b"hello"))
        self.assertEqual(got[2][4], b"a/b.txt")
        self.assertEqual(got[3][4], b"".join((b"\x01", b"\x01" * 32)))
        self.assertEqual(writer.written, len(sink.getvalue()))
        delta = io.BytesIO()
        out = bundle.Writer(delta, bundle.DELTA)
        out.put(bundle.FILE, bundle.AREA_TREE, "new.txt", b"x")
        out.put(bundle.DELETE, bundle.AREA_TREE, "gone.txt")
        out.put(bundle.SYMLINK, bundle.AREA_TREE, "s", b"new.txt")
        self.assertEqual([item[0] for item in drain(io.BytesIO(delta.getvalue()), bundle.DELTA)], [1, 3, 2])

    def test_a_kind_byte_outside_the_closed_set_is_rejected_whatever_follows(self):
        for kind in range(256):
            if kind in bundle.KINDS:
                continue
            for direction in (bundle.BUNDLE, bundle.DELTA):
                with self.assertRaises(bundle.WireError, msg=kind) as caught:
                    list(bundle.Reader(stream(raw_record(kind, b"tree/f", 0)), direction))
                self.assertEqual(caught.exception.reason, "kind")

    def test_a_kind_the_direction_does_not_have_is_rejected(self):
        with self.assertRaises(bundle.WireError):
            list(bundle.Reader(stream(raw_record(bundle.BASELINE, b"tree/f", 33, bytes(33))), bundle.DELTA))
        with self.assertRaises(bundle.WireError):
            list(bundle.Reader(stream(raw_record(bundle.DELETE, b"tree/f", 0)), bundle.BUNDLE))
        with self.assertRaises(bundle.WireError):  # a delta carries tree paths only: never git files
            list(bundle.Reader(stream(raw_record(bundle.FILE, b"git/config", 0)), bundle.DELTA))
        with self.assertRaises(bundle.WireError):  # a symlink in the git area cannot be expressed
            list(bundle.Reader(stream(raw_record(bundle.SYMLINK, b"git/x", 1, b"y")), bundle.BUNDLE))

    def test_flags_other_than_the_exec_bit_and_a_stray_exec_bit_are_rejected(self):
        with self.assertRaises(bundle.WireError) as caught:
            list(bundle.Reader(stream(raw_record(bundle.FILE, b"tree/f", 0, flags=0x02))))
        self.assertEqual(caught.exception.reason, "flags")
        with self.assertRaises(bundle.WireError):
            list(bundle.Reader(stream(raw_record(bundle.SYMLINK, b"tree/f", 1, b"t", flags=bundle.FLAG_EXEC))))

    def test_unclean_paths_are_refused_by_the_reader_and_by_the_writer(self):
        bad = [
            b"/etc/passwd",
            b"tree//a",
            b"tree/a/",
            b"tree/./a",
            b"tree/../a",
            b"tree/a/../b",
            b"tree/a\\b",
            b"tree/a\0b",
            b"tree",
            b"elsewhere/a",
            b"",
            b"tree/.git",
            b"tree/.git/config",
            b"tree/x/.git/hooks/post-commit",
            b"tree/.GIT",
            b"tree/.Git/config",
            b"tree/git~1/config",
            b"tree/GIT~1",
            b"tree/.git.",
            b"tree/.git ",
            b"tree/.git. .",
            "tree/.g​it".encode(),
            "tree/⁠.git".encode(),
            "tree/.gi­t".encode(),
            "tree/．git".encode(),
            "tree/.͇git".replace("͇", "͏").encode(),
            "tree/.git️".encode(),
            b"git/objects/.git/x",
        ]
        for path in bad:
            with self.subTest(path=path):
                with self.assertRaises(bundle.WireError):
                    list(bundle.Reader(stream(raw_record(bundle.FILE, path, 0))))
                if path and b"\0" not in path:
                    area, _, rest = path.decode().partition("/")
                    with self.assertRaises(bundle.WireError):
                        bundle.Writer(io.BytesIO()).put(bundle.FILE, area, rest)
        with self.assertRaises(bundle.WireError):
            list(bundle.Reader(stream(raw_record(bundle.FILE, b"x" * (pc.PATH_MAX_BYTES + 1), 0))))

    def test_a_name_that_is_not_a_git_directory_is_fine(self):
        for name in (".gitignore", ".gitmodules", "gitfoo", "my.git", "git", ".github", "dig~1", "a.git.txt"):
            self.assertFalse(bundle.is_git_name(name), name)

    def test_a_hostile_length_never_makes_a_large_read_or_allocation(self):
        for size in (2**63, 2**64 - 1, pc.FILE_MAX_BYTES + 1):
            source = Counting(b"".join((bundle.HEADER, raw_record(bundle.FILE, b"tree/f", size))))
            with self.assertRaises(bundle.BundleTooLarge):
                list(bundle.Reader(source))
            self.assertLessEqual(source.largest, bundle.CHUNK_BYTES)
        source = Counting(b"".join((bundle.HEADER, raw_record(bundle.FILE, b"tree/f", 0, length=65535))))
        with self.assertRaises(bundle.WireError):
            list(bundle.Reader(source))
        self.assertLessEqual(source.largest, bundle.CHUNK_BYTES)
        manifest = raw_record(bundle.FILE, b"meta/manifest.json", bundle.MANIFEST_MAX_BYTES + 1)
        with self.assertRaises(bundle.BundleTooLarge):
            list(bundle.Reader(Counting(b"".join((bundle.HEADER, manifest)))))
        with self.assertRaises(bundle.BundleTooLarge):  # a size that does not fit what is left of the byte cap
            list(bundle.Reader(stream(raw_record(bundle.FILE, b"tree/f", 60)), limits=bundle.Limits(60, 10)))

    def test_a_stream_cut_off_inside_a_record_is_an_error_and_at_a_boundary_is_a_shorter_stream(self):
        sink = io.BytesIO()
        writer = bundle.Writer(sink)
        boundaries = {writer.written}
        for index in range(3):
            writer.put(bundle.FILE, bundle.AREA_TREE, f"f{index}", b"x" * index)
            boundaries.add(writer.written)
        data = sink.getvalue()
        first_cut = len(bundle.HEADER)
        last_cut = len(data)
        for cut in range(first_cut, last_cut):
            if cut in boundaries:
                list(bundle.Reader(io.BytesIO(data[:cut])))
                continue
            with self.assertRaises(bundle.WireError, msg=cut) as caught:
                drain(io.BytesIO(data[:cut]))
            self.assertEqual(caught.exception.reason, "truncated")
        header_size = len(bundle.HEADER)
        for cut in range(header_size):
            with self.assertRaises(bundle.WireError):
                list(bundle.Reader(io.BytesIO(data[:cut])))

    def test_the_magic_and_the_version_are_checked(self):
        with self.assertRaises(bundle.WireError) as caught:
            list(bundle.Reader(io.BytesIO(b"NOTABUNDLE!")))
        self.assertEqual(caught.exception.reason, "magic")
        with self.assertRaises(bundle.WireError) as caught:
            list(bundle.Reader(io.BytesIO(b"".join((bundle.MAGIC, struct.pack(">H", 2))))))
        self.assertEqual(caught.exception.reason, "version")

    def test_the_record_and_byte_caps_apply_to_both_sides(self):
        small = bundle.Limits(max_bytes=200, max_files=2)
        writer = bundle.Writer(io.BytesIO(), limits=small)
        writer.put(bundle.FILE, bundle.AREA_TREE, "a", b"1")
        writer.put(bundle.FILE, bundle.AREA_TREE, "b", b"1")
        with self.assertRaises(bundle.BundleTooLarge):
            writer.put(bundle.FILE, bundle.AREA_TREE, "c", b"1")
        with self.assertRaises(bundle.BundleTooLarge):
            bundle.Writer(io.BytesIO(), limits=bundle.Limits(max_bytes=40, max_files=9)).put(
                bundle.FILE, bundle.AREA_TREE, "a", b"x" * 100
            )
        records = (raw_record(bundle.FILE, b"tree/a", 0), raw_record(bundle.FILE, b"tree/b", 0), raw_record(bundle.FILE, b"tree/c", 0))
        with self.assertRaises(bundle.BundleTooLarge):
            list(bundle.Reader(stream(*records), limits=small))
        self.assertEqual(bundle.LIMITS[bundle.DELTA].max_files, pc.DELTA_MAX_FILES)
        self.assertEqual(bundle.LIMITS[bundle.DELTA].max_bytes, pc.DELTA_MAX_BYTES)
        self.assertEqual(bundle.LIMITS[bundle.BUNDLE].max_bytes, pc.BUNDLE_MAX_BYTES)

    def test_the_manifest_holds_a_system_prompt_built_from_the_largest_skills_budget(self):
        """Production, 2026-11-11: a 568 KB system prompt (a repository's skills) failed every plan at the old 256 KiB limit."""
        import base64

        biggest = (1024 + 64) * 1024  # repo_skills_budget_kib at its maximum, plus a CLAUDE.md and the addendum
        files = [
            {"path": "/data/x/session.system.md", "data": base64.b64encode(b"s" * biggest).decode(), "mode": 416},
            {"path": "/data/x/session.prompt.txt", "data": base64.b64encode(b"p" * 120_000).decode(), "mode": 384},
            {"path": "/data/x/settings.json", "data": base64.b64encode(b"{}" * 2000).decode(), "mode": 416},
        ]
        data = bundle.manifest_bytes({"dirs": [], "files": files})
        self.assertLessEqual(len(data), bundle.MANIFEST_MAX_BYTES)
        self.assertGreater(len(data), 256 * 1024)  # the old limit would have refused it
        self.assertEqual(bundle.MANIFEST_MAX_BYTES, 2 * 1024 * 1024)

    def test_a_manifest_record_of_a_large_system_prompt_is_read_and_one_byte_over_the_limit_is_refused(self):
        payload = b'{"files":"' + b"a" * 900_000 + b'"}'
        read = [len(record.read(bundle.MANIFEST_MAX_BYTES)) for record in bundle.Reader(stream(raw_record(bundle.FILE, b"meta/manifest.json", len(payload), payload)))]
        self.assertEqual(read, [len(payload)])
        over = raw_record(bundle.FILE, b"meta/manifest.json", bundle.MANIFEST_MAX_BYTES + 1)
        with self.assertRaises(bundle.BundleTooLarge):
            list(bundle.Reader(Counting(b"".join((bundle.HEADER, over)))))

    def test_a_manifest_must_be_one_small_json_object(self):
        with self.assertRaises(bundle.WireError):
            bundle.manifest_bytes(["not", "an", "object"])
        with self.assertRaises(bundle.BundleTooLarge):
            bundle.manifest_bytes({"x": "y" * bundle.MANIFEST_MAX_BYTES})
        record = next(iter(bundle.Reader(stream(raw_record(bundle.FILE, b"meta/manifest.json", 2, b"[]")))))
        with self.assertRaises(bundle.WireError):
            bundle.read_manifest(record)

    def test_a_symlink_target_with_a_nul_byte_is_refused_by_the_writer_and_the_reader_in_both_directions(self):
        for direction in (bundle.BUNDLE, bundle.DELTA):
            with self.subTest(direction=direction):
                sink = io.BytesIO()
                writer = bundle.Writer(sink, direction)
                before = sink.getvalue()
                with self.assertRaises(bundle.WireError) as caught:
                    writer.put(bundle.SYMLINK, bundle.AREA_TREE, "ln", b"a\0b")
                self.assertEqual((caught.exception.reason, str(caught.exception)), ("target", "bundle protocol error"))
                self.assertEqual(sink.getvalue(), before)  # nothing of the refused record was written
                hostile = stream(raw_record(bundle.SYMLINK, b"tree/ln", 3, b"a\0b"))
                with self.assertRaises(bundle.WireError) as caught:
                    list(bundle.Reader(hostile, direction))  # refused when the record is decoded, whatever the consumer does
                self.assertEqual(caught.exception.reason, "target")
                clean = drain(stream(raw_record(bundle.SYMLINK, b"tree/ln", 3, b"a/b")), direction)
                self.assertEqual(clean, [(bundle.SYMLINK, False, bundle.AREA_TREE, "ln", b"a/b")])


class Tree(unittest.TestCase):
    """A supervisor data directory, a spool, a scratch directory and an empty 'pod' data directory."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(os.path.realpath(self.temp.name))
        self.data = self.root / "data"
        self.spool = self.root / "spool"
        self.scratch = self.root / "scratch"
        self.pod = self.root / "pod"
        for directory in (self.data, self.spool, self.scratch, self.pod):
            directory.mkdir()
        self.session = pc.new_session_id()

    def pod_files(self, base):
        """{relative path: (kind, exec, content or link target)} of everything under ``base``, links not followed."""
        found = {}
        for directory, names, files in os.walk(base):
            for name in [*names, *files]:
                path = Path(directory) / name
                info = os.lstat(path)
                key = os.path.relpath(path, base)
                if stat.S_ISLNK(info.st_mode):
                    found[key] = ("link", False, os.readlink(path))
                elif stat.S_ISREG(info.st_mode):
                    found[key] = ("file", bool(info.st_mode & stat.S_IXUSR), path.read_bytes())
        return found

    def bundle_path(self, session=None):
        return self.spool / (session or self.session) / sr.IN_DIR / sr.BUNDLE_FILE


class DirectoryStagingTests(Tree):
    """Reading a tree that is not a git repository: the safety of the walk, the caps, the spool."""

    def setUp(self):
        super().setUp()
        self.tree = self.data / "work" / "tree"
        self.tree.mkdir(parents=True)
        (self.tree / "src").mkdir()
        (self.tree / "src" / "a.py").write_text("print(1)\n")
        (self.tree / "run.sh").write_text("#!/bin/sh\n")
        os.chmod(self.tree / "run.sh", 0o755)
        (self.tree / "README").write_text("r\n")
        self.fat = 0  # bytes of the one large file that the fake git directory holds besides HEAD

    def stage(self, **extra):
        with patch.object(bundle, "build_git_dir", return_value=self.fake_git()):
            return self.stage_as(self.session, **extra)

    def stage_as(self, session, **extra):
        """``bundle.stage`` of the fixture's tree for ``session``; the caller patches ``build_git_dir``."""
        return bundle.stage(
            self.spool,
            session,
            data_root=self.data,
            worktree=self.tree,
            manifest={"argv": ["claude"]},
            scratch=self.scratch,
            gitdir=self.root / "unused",
            **extra,
        )

    def fake_git(self):
        directory = self.root / "fakegit" / str(self.fat) / ".git"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "HEAD").write_text("ref: refs/heads/x\n")
        if self.fat:
            (directory / "objects").mkdir(exist_ok=True)
            (directory / "objects" / "big.pack").write_bytes(b"x" * self.fat)
        return os.fspath(directory)

    def extract(self):
        with open(self.bundle_path(), "rb") as source:
            return bundle.extract(source, self.pod)

    def test_a_tree_round_trips_with_exec_bits_and_a_complete_baseline(self):
        staged = self.stage()
        self.assertGreater(staged.git_bytes, 0)
        self.assertLess(staged.git_bytes, staged.size)
        result = self.extract()
        self.assertEqual(result.worktree, "work/tree")
        self.assertEqual(result.manifest["argv"], ["claude"])
        self.assertEqual(self.pod_files(self.pod / "work" / "tree")[".git/HEAD"][2], b"ref: refs/heads/x\n")
        shipped = {k: v for k, v in self.pod_files(self.pod / "work" / "tree").items() if not k.startswith(".git")}
        self.assertEqual(shipped, {k: v for k, v in self.pod_files(self.tree).items()})
        self.assertTrue(shipped["run.sh"][1])
        self.assertFalse(shipped["README"][1])
        self.assertEqual(set(result.baseline), {"src/a.py", "run.sh", "README"})
        self.assertEqual(result.baseline["run.sh"][:2], (bundle.FILE, True))
        self.assertEqual(staged.size, self.bundle_path().stat().st_size)
        self.assertEqual(stat.S_IMODE(self.bundle_path().stat().st_mode), sr.FILE_MODE)

    def test_the_baseline_covers_every_staged_path_and_nothing_else(self):
        (self.tree / "ignored").mkdir()
        (self.tree / "ignored" / "x.bin").write_bytes(b"\0\1")
        os.symlink("src/a.py", self.tree / "alias")
        self.stage()
        with open(self.bundle_path(), "rb") as source:
            tree_paths = set()
            baseline_paths = set()
            for record in bundle.Reader(source):  # a payload is read before the reader moves on
                if record.area == bundle.AREA_TREE and record.kind == bundle.BASELINE:
                    baseline_paths.add(bundle.read_baseline(record)[0])
                elif record.area == bundle.AREA_TREE:
                    tree_paths.add(record.path)
                    record.read(pc.FILE_MAX_BYTES)
        self.assertEqual(tree_paths, baseline_paths)
        self.assertEqual(tree_paths, {"src/a.py", "run.sh", "README", "ignored/x.bin", "alias"})

    def test_a_baseline_that_does_not_match_what_was_shipped_is_refused(self):
        sink = io.BytesIO()
        writer = bundle.Writer(sink)
        writer.put_manifest({"worktree": "w"})
        writer.put(bundle.FILE, bundle.AREA_TREE, "f", b"data")
        writer.put_baseline("f", bundle.FILE, b"\0" * 32)
        with self.assertRaises(bundle.WireError) as caught:
            bundle.extract(io.BytesIO(sink.getvalue()), self.pod)
        self.assertEqual(caught.exception.reason, "baseline")
        sink = io.BytesIO()
        writer = bundle.Writer(sink)
        writer.put_manifest({"worktree": "second"})
        writer.put(bundle.FILE, bundle.AREA_TREE, "f", b"data")
        with self.assertRaises(bundle.WireError):  # a shipped path with no baseline record
            bundle.extract(io.BytesIO(sink.getvalue()), self.pod)

    def test_a_symlink_planted_by_a_previous_worker_is_shipped_as_text_and_never_followed(self):
        outside = self.data / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("TOPSECRETCONTENT")
        os.symlink("../../outside", self.tree / "evil")
        os.symlink("/etc", self.tree / "abs")
        os.symlink("/proc/self/fd/0", self.tree / "fd")
        os.symlink("missing", self.tree / "dangling")
        os.symlink(".", self.tree / "loop")
        self.stage()
        data = self.bundle_path().read_bytes()
        self.assertNotIn(b"TOPSECRETCONTENT", data)
        self.assertNotIn(b"root:x:", data)
        result = self.extract()
        shipped = self.pod_files(self.pod / "work" / "tree")
        self.assertEqual(shipped["evil"], ("link", False, "../../outside"))
        self.assertEqual(shipped["abs"], ("link", False, "/etc"))
        self.assertEqual(shipped["loop"], ("link", False, "."))
        self.assertEqual(result.baseline["evil"][0], bundle.SYMLINK)
        self.assertFalse((self.pod / "outside").exists())

    def test_a_symlink_as_the_worktree_itself_or_a_read_path_is_not_followed(self):
        real = self.data / "elsewhere"
        real.mkdir()
        (real / "f").write_text("x")
        link = self.data / "linked"
        os.symlink(real, link)
        evidence = self.data / "evidence.json"
        evidence.write_text("{}")
        os.symlink(evidence, self.data / "alias.json")
        with self.assertRaises(bundle.TreeError):
            self.stage(read_paths=[self.data / "alias.json"])
        with self.assertRaises(bundle.TreeError):  # a directory is not a read-only file
            self.stage(read_paths=[self.tree / "src"])
        with self.assertRaises(bundle.TreeError):
            self.stage(read_paths=[self.data / "missing.json"])
        with self.assertRaises(bundle.TreeError):
            self.stage(read_paths=[self.root / "outside-the-data-root.json"])
        self.assertFalse(self.bundle_path().exists())

    def test_read_paths_are_staged_read_only_at_their_path_under_the_data_root(self):
        evidence = self.data / "runs" / "evidence.json"
        evidence.parent.mkdir()
        evidence.write_text('{"issues": []}')
        self.stage(read_paths=[evidence])
        self.extract()
        copy = self.pod / "runs" / "evidence.json"
        self.assertEqual(copy.read_text(), '{"issues": []}')
        self.assertEqual(stat.S_IMODE(copy.stat().st_mode), 0o444)

    def test_a_read_path_inside_the_worktree_or_listed_twice_is_refused_at_staging(self):
        evidence = self.data / "runs" / "evidence.json"
        evidence.parent.mkdir()
        evidence.write_text("{}")
        (self.data / "alias").symlink_to(evidence.parent)
        clashing = {
            "inside the worktree": [self.tree / "README"],
            "inside a subdirectory of the worktree": [self.tree / "src" / "a.py"],
            "listed twice": [evidence, evidence],
            "twice, once through a link": [evidence, self.data / "alias" / "evidence.json"],
        }
        for name, paths in clashing.items():
            with self.subTest(name), self.assertRaises(bundle.TreeError) as caught:
                self.stage(read_paths=paths)
            self.assertEqual((caught.exception.reason, str(caught.exception)), ("read path", "tree could not be read or written safely"))
        self.assertFalse(self.bundle_path().exists())
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.stage(read_paths=[evidence])  # a clean one next to the same tree still stages, and extracts
        self.extract()
        self.assertEqual((self.pod / "runs" / "evidence.json").read_text(), "{}")

    def test_the_digest_of_an_entry_is_the_baseline_digest_of_its_bytes_or_its_target(self):
        os.symlink("README", self.tree / "alias")
        fd = os.open(self.tree, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        entries = {}
        with contextlib.closing(bundle.walk(fd, bundle.AREA_TREE, bundle.Totals())) as walking:
            for entry in walking:
                entries[entry.path] = bundle.digest_of(entry)
        self.assertEqual(entries["README"], hashlib.sha256(b"r\n").digest())
        self.assertEqual(entries["alias"], hashlib.sha256(b"README").digest())
        self.assertEqual(entries["src/a.py"], hashlib.sha256(b"print(1)\n").digest())

    def walked_entry(self, path):
        """Run ``check(entry)`` on the entry of ``path`` while the walk still holds its directory open."""
        fd = os.open(self.tree, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        with contextlib.closing(bundle.walk(fd, bundle.AREA_TREE, bundle.Totals())) as walking:
            for entry in walking:
                if entry.path == path:
                    yield entry
                    return

    def test_a_file_that_grew_between_the_walk_and_the_read_is_a_change_never_a_truncated_copy(self):
        # lstat said 2 bytes (README is "r\n"); 50 more arrive before the file is opened: open_file refuses it
        for entry in self.walked_entry("README"):
            with open(self.tree / "README", "ab") as handle:
                handle.write(b"x" * 50)
            with self.assertRaises(bundle.TreeError) as caught:
                bundle.open_file(entry)
            self.assertEqual(caught.exception.reason, "changed")
            with self.assertRaises(bundle.TreeError) as caught:
                bundle.digest_of(entry)
            self.assertEqual(caught.exception.reason, "changed")

    def test_a_file_that_grows_after_it_was_opened_fails_the_record_and_the_baseline_digest(self):
        for entry in self.walked_entry("README"):
            fd = bundle.open_file(entry)
            self.addCleanup(os.close, fd)
            with open(self.tree / "README", "ab") as handle:
                handle.write(b"x" * 50)  # after the size check, before the read of the last byte
            written = io.BytesIO()
            with self.assertRaises(bundle.TreeError) as caught:
                bundle.Writer(written).put_file(bundle.AREA_TREE, "README", fd, entry.size)
            self.assertEqual(caught.exception.reason, "changed")
            with self.assertRaises(bundle.TreeError):
                bundle.digest_of(entry)  # it opens the file anew: the size no longer is the one lstat saw

    def test_the_digest_of_a_file_that_grows_while_it_is_read_is_a_change(self):
        for entry in self.walked_entry("README"):
            real_read = os.read

            def growing(fd, count):
                data = real_read(fd, count)
                if data:  # the file grows right after the bytes lstat counted were read
                    with open(self.tree / "README", "ab") as handle:
                        handle.write(b"x")
                return data

            with patch.object(os, "read", growing), self.assertRaises(bundle.TreeError) as caught:
                bundle.digest_of(entry)
            self.assertEqual(caught.exception.reason, "changed")

    def test_a_fifo_a_socket_and_a_hard_link_are_handled_without_blocking(self):
        os.mkfifo(self.tree / "pipe")
        listener = socket.socket(socket.AF_UNIX)
        self.addCleanup(listener.close)
        with contextlib.chdir(self.tree):  # a short relative name: an absolute path under a long TMPDIR passes the 108 bytes of AF_UNIX
            listener.bind("sock")
        os.link(self.tree / "README", self.tree / "linked")
        outcome = {}

        def run():
            outcome["staged"] = self.stage()

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(60)
        self.assertFalse(worker.is_alive(), "staging waited on a fifo")
        self.assertEqual(outcome["staged"].skipped, 2)
        self.extract()
        shipped = self.pod_files(self.pod / "work" / "tree")
        self.assertNotIn("pipe", shipped)
        self.assertNotIn("sock", shipped)
        self.assertEqual(shipped["linked"], ("file", False, b"r\n"))

    def test_opening_an_entry_that_became_a_fifo_does_not_wait_and_is_refused(self):
        os.mkfifo(self.tree / "swapped")
        outcome = {}

        def run():
            fd = os.open(self.tree, os.O_RDONLY | os.O_DIRECTORY)
            try:
                entry = bundle.Entry("swapped", bundle.FILE, False, 0, fd, "swapped", None)
                try:
                    bundle.open_file(entry)
                except bundle.TreeError as error:
                    outcome["error"] = error
            finally:
                os.close(fd)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(60)
        self.assertFalse(worker.is_alive(), "open waited on a fifo")
        self.assertIsInstance(outcome.get("error"), bundle.TreeError)

    def test_an_entry_swapped_for_a_symlink_is_refused(self):
        os.symlink("README", self.tree / "swapped")
        fd = os.open(self.tree, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        with self.assertRaises(bundle.TreeError):
            bundle.open_file(bundle.Entry("swapped", bundle.FILE, False, 2, fd, "swapped", None))

    def test_names_that_spell_a_git_directory_are_never_shipped(self):
        names = [".git", ".GIT", ".Git", ".git ", ".git.", "git~1", "GIT~1", ".g​it", "⁠.git"]
        (self.tree / "nested").mkdir()
        for name in names:
            (self.tree / name).mkdir()
            (self.tree / name / "config").write_text("[core]\n\tfsmonitor = touch /tmp/pwned\n")
            (self.tree / "nested" / name).write_text("pointer")
        self.stage()
        data = self.bundle_path().read_bytes()
        self.assertNotIn(b"fsmonitor", data)
        for name in names:
            self.assertNotIn(os.fsencode(name), data, name)
        self.extract()
        shipped = self.pod_files(self.pod / "work" / "tree")
        self.assertEqual({k for k in shipped if not k.startswith(".git/")}, {"src/a.py", "run.sh", "README"})
        self.assertEqual(self.stage().git_names, 2 * len(names))

    def test_every_cap_fails_with_bundle_too_large_and_leaves_nothing_behind(self):
        (self.tree / "src" / "deep" / "er" / "est").mkdir(parents=True)
        (self.tree / "src" / "deep" / "er" / "est" / "f").write_text("x")
        cases = {
            "files": ({bundle.BUNDLE: bundle.Limits(10_000_000, 2)}, {}),
            "bytes": ({bundle.BUNDLE: bundle.Limits(300, 100)}, {}),
        }
        for name, (limits, _) in cases.items():
            with self.subTest(cap=name), patch.dict(bundle.LIMITS, limits):
                with self.assertRaises(bundle.BundleTooLarge):
                    self.stage()
        for what, attribute, value in (("depth", "MAX_DEPTH", 3), ("path", "PATH_MAX_BYTES", 20)):
            with self.subTest(cap=what):
                target = bundle if what == "depth" else pc
                with patch.object(target, attribute, value), self.assertRaises(bundle.BundleTooLarge):
                    self.stage()
        with patch.object(pc, "FILE_MAX_BYTES", 5), self.assertRaises(bundle.BundleTooLarge):
            self.stage()
        self.assertFalse(self.bundle_path().exists())
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.assertEqual([p for p in (self.spool / self.session).rglob("*") if p.is_file()], [])

    def test_a_failure_while_writing_leaves_no_partial_bundle_and_keeps_the_previous_one(self):
        self.stage()
        before = self.bundle_path().read_bytes()
        calls = []

        def shrinking(entry):
            calls.append(entry.path)
            if len(calls) == 2:
                raise bundle.TreeError("changed")
            return real_open(entry)

        real_open = bundle.open_file
        with patch.object(bundle, "open_file", shrinking), self.assertRaises(bundle.TreeError):
            self.stage()
        self.assertEqual(sorted(p.name for p in self.bundle_path().parent.iterdir()), [sr.BUNDLE_FILE])
        self.assertEqual(self.bundle_path().read_bytes(), before)
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_a_file_that_shrinks_after_it_was_opened_fails_the_staging_and_leaves_no_partial_bundle(self):
        written = io.BytesIO()
        with self.assertRaises(bundle.TreeError) as caught:  # the record declares 9 bytes and the file has only 3 left
            short = self.tree / "short.bin"
            short.write_bytes(b"abc")
            fd = os.open(short, os.O_RDONLY)
            try:
                bundle.Writer(written).put_file(bundle.AREA_TREE, "short.bin", fd, 9)
            finally:
                os.close(fd)
        self.assertEqual(caught.exception.reason, "changed")
        short.unlink()
        self.stage()
        before = self.bundle_path().read_bytes()
        real_open = bundle.open_file

        def truncating(entry):
            fd = real_open(entry)
            if entry.path == "src/a.py":
                os.truncate(self.tree / "src" / "a.py", 2)  # the file shrinks between the open and the read
            return fd

        with patch.object(bundle, "open_file", truncating), self.assertRaises(bundle.TreeError) as caught:
            self.stage()
        self.assertEqual(caught.exception.reason, "changed")
        self.assertEqual(self.bundle_path().read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.bundle_path().parent.iterdir()), [sr.BUNDLE_FILE])
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_a_directory_swapped_for_a_symlink_after_it_was_listed_is_not_followed(self):
        (self.tree / "sub").mkdir()
        (self.tree / "sub" / "inner.txt").write_text("inner\n")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("TOP-SECRET-BYTES\n")
        real_lstat = os.lstat
        seen = []

        def swapping(path, *, dir_fd=None):
            info = real_lstat(path, dir_fd=dir_fd)
            if path == "sub" and dir_fd is not None:
                seen.append(path)
                if len(seen) == 2:  # the writing pass: the measuring pass saw a directory, then it becomes a link to elsewhere
                    os.rename(self.tree / "sub", self.root / "moved")
                    os.symlink(outside, self.tree / "sub")
            return info

        with patch.object(os, "lstat", swapping), self.assertRaises(bundle.TreeError) as caught:
            self.stage()
        self.assertEqual(len(seen), 2)
        self.assertEqual(caught.exception.reason, "changed")
        self.assertFalse(self.bundle_path().exists())  # and so no bundle can hold the bytes of the directory it points to
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.assertEqual([p for p in (self.spool / self.session).rglob("*") if p.is_file()], [])

    def test_the_git_area_has_no_per_file_cap_and_the_other_areas_do(self):
        with patch.object(pc, "FILE_MAX_BYTES", 100):
            git_file = raw_record(bundle.FILE, b"git/objects/pack/p.pack", 5000, b"x" * 5000)
            sizes = [len(b"".join(record.chunks())) for record in bundle.Reader(stream(git_file))]
            self.assertEqual(sizes, [5000])  # a pack is one file: only the bundle cap bounds it
            for area in (b"tree", b"read"):
                with self.subTest(area=area), self.assertRaises(bundle.BundleTooLarge):
                    list(bundle.Reader(stream(raw_record(bundle.FILE, b"/".join((area, b"p.bin")), 5000, b"x" * 5000))))
            writer = bundle.Writer(io.BytesIO())
            writer.put(bundle.FILE, bundle.AREA_GIT, "objects/pack/p.pack", b"x" * 5000)
            with self.assertRaises(bundle.BundleTooLarge):
                writer.put(bundle.FILE, bundle.AREA_TREE, "p.bin", b"x" * 5000)
            self.fat = 5000  # and a whole staging: the git directory's big file is accepted, a tree file of that size is not
            (self.tree / "src" / "a.py").write_text("x" * 50)
            self.assertGreater(self.stage().git_bytes, 5000)
            self.extract()
            self.assertEqual(len((self.pod / "work" / "tree" / ".git" / "objects" / "big.pack").read_bytes()), 5000)
            self.bundle_path().unlink()
            (self.tree / "src" / "a.py").write_text("x" * 5000)
            with self.assertRaises(bundle.BundleTooLarge):
                self.stage()

    def test_a_file_over_the_contract_cap_is_refused_before_a_byte_of_it_is_read(self):
        with open(self.tree / "huge.bin", "wb") as handle:
            handle.truncate(pc.FILE_MAX_BYTES + 1)  # sparse: no disk is used
        with patch.object(bundle.os, "read", side_effect=AssertionError("read before the cap was checked")):
            with self.assertRaises(bundle.BundleTooLarge) as caught:
                self.stage()
        self.assertEqual(caught.exception.reason, "size")
        self.assertEqual(str(caught.exception), "bundle too large")

    def test_failures_never_echo_a_name_or_a_path_of_the_tree(self):
        (self.tree / "PLANTED-NAME-1234").write_text("x")
        with patch.object(pc, "PATH_MAX_BYTES", 20), self.assertRaises(bundle.BundleError) as caught:
            self.stage()
        self.assertNotIn("PLANTED", str(caught.exception))
        self.assertNotIn(os.fspath(self.root), str(caught.exception))

    def test_the_spool_waits_instead_of_failing_and_counts_what_is_already_in_it(self):
        with self.assertRaises(bundle.SpoolFull) as caught:
            self.stage(refuse_bytes=100)
        self.assertGreater(caught.exception.needed, 100)
        self.assertEqual(caught.exception.available, 100)
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.assertFalse(self.bundle_path().exists())
        staged = self.stage(refuse_bytes=10 * 1024 * 1024)
        self.assertEqual(bundle.spool_usage(self.spool), staged.size)
        second = pc.new_session_id()
        with patch.object(bundle, "build_git_dir", return_value=self.fake_git()), self.assertRaises(bundle.SpoolFull):
            self.stage_as(second, refuse_bytes=staged.size + 100)
        self.assertFalse(self.bundle_path(second).exists())

    def test_a_claim_is_held_in_this_module_only_and_the_bytes_on_disk_are_what_the_relay_counts(self):
        claim = bundle._Claim(self.spool, 10_000)
        self.addCleanup(claim.release)
        claim.resize(9_000)
        self.assertEqual(bundle.spool_usage(self.spool), 0)  # a staging in flight is not on disk: a disk count (the relay's) cannot see it
        other = bundle._Claim(self.spool, 10_000)
        self.addCleanup(other.release)
        with self.assertRaises(bundle.SpoolFull):  # another staging of this process does
            other.resize(2_000)
        claim.release()
        other.resize(2_000)

    def test_the_git_directory_counts_against_the_spool_limit_and_waits(self):
        small = self.stage()
        self.bundle_path().unlink()
        self.fat = 5000
        with self.assertRaises(bundle.SpoolFull) as caught:  # the tree alone fits, the tree and the git directory do not
            self.stage(refuse_bytes=small.size + 1000)
        self.assertGreaterEqual(caught.exception.needed, small.size + 5000)
        self.assertEqual(caught.exception.available, small.size + 1000)
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.assertFalse(self.bundle_path().exists())
        self.fat = 0
        self.assertEqual(self.stage(refuse_bytes=small.size + 10).size, small.size)  # the failed run holds nothing back
        self.bundle_path().unlink()
        self.fat = 5000
        big = self.stage(refuse_bytes=10 * 1024 * 1024)
        self.assertGreaterEqual(big.git_bytes, 5000)
        self.assertEqual(big.size - big.git_bytes, small.size - small.git_bytes)
        self.assertEqual(bundle.spool_usage(self.spool), big.size)

    def test_the_git_directory_counts_against_the_bundle_cap(self):
        self.fat = 5000
        big = self.stage()
        self.bundle_path().unlink()
        self.assertGreater(big.git_bytes, 5000)
        with patch.dict(bundle.LIMITS, {bundle.BUNDLE: bundle.Limits(big.size, 1000)}):
            self.assertEqual(self.stage().size, big.size)  # the cap is exactly the tree and the git directory together
            self.bundle_path().unlink()
        smaller = {bundle.BUNDLE: bundle.Limits(big.size - 1, 1000)}  # the git directory alone is far below it
        with patch.dict(bundle.LIMITS, smaller), self.assertRaises(bundle.BundleTooLarge):
            self.stage()
        self.assertFalse(self.bundle_path().exists())
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.assertEqual([p for p in (self.spool / self.session).rglob("*") if p.is_file()], [])

    def test_stagings_running_together_share_the_spool_limit(self):
        size = self.stage().size
        self.bundle_path().unlink()
        limit = size + size // 2  # one bundle fits under the refusal limit, two do not
        first, second = pc.new_session_id(), pc.new_session_id()
        inside, go, outcome = threading.Event(), threading.Event(), []
        self.addCleanup(go.set)
        fake = self.fake_git()

        def building(*_args, **_kwargs):
            if not inside.is_set():  # only the first run waits here, after its claim and before its second check
                inside.set()
                if not go.wait(30):
                    raise AssertionError("never released")
            return fake

        def hold():
            try:
                outcome.append(self.stage_as(first, refuse_bytes=limit))
            except (bundle.BundleError, AssertionError) as error:  # reported by the assertions below
                outcome.append(error)

        with patch.object(bundle, "build_git_dir", building):
            holder = threading.Thread(target=hold, daemon=True)
            holder.start()
            self.assertTrue(inside.wait(30))
            with self.assertRaises(bundle.SpoolFull):  # nothing is on disk yet, but the first run has claimed its bytes
                self.stage_as(second, refuse_bytes=limit)
            self.assertFalse(self.bundle_path(second).exists())
            go.set()
            holder.join(30)
            self.assertFalse(holder.is_alive())
            self.assertIsInstance(outcome[0], bundle.Staged)
            self.assertLessEqual(bundle.spool_usage(self.spool), limit)
            with self.assertRaises(bundle.SpoolFull):  # and now its bundle is on disk
                self.stage_as(second, refuse_bytes=limit)
            shutil.rmtree(self.spool / first)  # a finished run holds no claim any more
            self.assertEqual(self.stage_as(second, refuse_bytes=limit).size, size)
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_the_worktree_must_be_under_the_data_root(self):
        with self.assertRaises(bundle.TreeError):
            bundle.stage(
                self.spool,
                self.session,
                data_root=self.data / "work",
                worktree=self.data,
                manifest={},
                scratch=self.scratch,
                gitdir=self.root / "unused",
            )

    def test_a_manifest_cannot_name_its_own_worktree(self):
        with self.assertRaises(bundle.WireError):
            bundle.stage(
                self.spool,
                self.session,
                data_root=self.data,
                worktree=self.tree,
                manifest={"worktree": "elsewhere"},
                scratch=self.scratch,
                gitdir=self.root / "unused",
            )


class ExtractionSafetyTests(Tree):
    """The pod side writes a hostile bundle: no link is followed and no earlier record is overwritten."""

    def bundle_of(self, *records, worktree="w"):
        sink = io.BytesIO()
        writer = bundle.Writer(sink)
        writer.put_manifest({"worktree": worktree})
        for kind, area, path, payload in records:
            writer.put(kind, area, path, payload)
        return io.BytesIO(sink.getvalue())

    def test_a_file_cannot_be_written_through_a_symlinked_directory(self):
        outside = self.root / "outside"
        outside.mkdir()
        source = self.bundle_of(
            (bundle.SYMLINK, bundle.AREA_TREE, "d", os.fsencode(outside)),
            (bundle.FILE, bundle.AREA_TREE, "d/pwned", b"x"),
        )
        with self.assertRaises(bundle.TreeError):
            bundle.extract(source, self.pod)
        self.assertEqual(list(outside.iterdir()), [])

    def test_a_symlink_as_the_worktree_directory_is_not_followed(self):
        outside = self.root / "outside"
        outside.mkdir()
        os.symlink(outside, self.pod / "w")
        with self.assertRaises(bundle.TreeError):
            bundle.extract(self.bundle_of((bundle.FILE, bundle.AREA_TREE, "f", b"x")), self.pod)
        self.assertEqual(list(outside.iterdir()), [])

    def test_a_path_written_twice_or_over_a_directory_is_an_error(self):
        with self.assertRaises(bundle.TreeError):
            bundle.extract(
                self.bundle_of((bundle.FILE, bundle.AREA_TREE, "f", b"1"), (bundle.FILE, bundle.AREA_TREE, "f", b"2")), self.pod
            )
        (self.pod / "other").mkdir()
        with self.assertRaises(bundle.TreeError):
            bundle.extract(
                self.bundle_of((bundle.FILE, bundle.AREA_TREE, "a", b"1"), (bundle.FILE, bundle.AREA_TREE, "a/b", b"2"), worktree="other"),
                self.pod,
            )

    def test_the_manifest_comes_first_and_names_a_clean_worktree(self):
        for worktree in ("", "/abs", "../up", "a/../b", ".git", "x/.GIT/y", 5):
            sink = io.BytesIO()
            writer = bundle.Writer(sink)
            document = {"worktree": worktree}
            writer.put(bundle.FILE, bundle.AREA_META, bundle.MANIFEST_PATH, json.dumps(document).encode())
            with self.subTest(worktree=worktree), self.assertRaises(bundle.WireError):
                bundle.extract(io.BytesIO(sink.getvalue()), self.pod)
        sink = io.BytesIO()
        bundle.Writer(sink).put(bundle.FILE, bundle.AREA_TREE, "f", b"x")
        with self.assertRaises(bundle.WireError):
            bundle.extract(io.BytesIO(sink.getvalue()), self.pod)
        with self.assertRaises(bundle.WireError):
            bundle.extract(io.BytesIO(bundle.HEADER), self.pod)
        sink = io.BytesIO()
        writer = bundle.Writer(sink)
        writer.put_manifest({"worktree": "w"})
        writer.put_manifest({"worktree": "w"})
        with self.assertRaises(bundle.WireError):
            bundle.extract(io.BytesIO(sink.getvalue()), self.pod)

    def test_the_git_files_go_under_dot_git_of_the_worktree_and_nowhere_else(self):
        source = self.bundle_of((bundle.FILE, bundle.AREA_GIT, "objects/pack/p.pack", b"PACK"), (bundle.FILE, bundle.AREA_GIT, "HEAD", b"h"))
        result = bundle.extract(source, self.pod)
        self.assertEqual((self.pod / "w" / ".git" / "HEAD").read_bytes(), b"h")
        self.assertEqual((self.pod / "w" / ".git" / "objects" / "pack" / "p.pack").read_bytes(), b"PACK")
        self.assertEqual(result.baseline, {})


    def test_the_directories_git_insists_on_are_made_even_when_no_path_implies_them(self):
        source = self.bundle_of((bundle.FILE, bundle.AREA_GIT, "HEAD", b"h"))
        bundle.extract(source, self.pod)
        for parts in bundle.GIT_SKELETON:
            self.assertTrue(os.path.isdir(self.pod.joinpath("w", ".git", *parts)), parts)
        bundle.extract(self.bundle_of(worktree="plain"), self.pod)
        self.assertTrue((self.pod / "plain").is_dir())
        self.assertFalse((self.pod / "plain" / ".git").exists())  # a bundle without a git area gets no git directory

    def test_a_symlink_target_with_a_nul_byte_is_a_wire_error_and_creates_nothing(self):
        sink = io.BytesIO()
        bundle.Writer(sink).put_manifest({"worktree": "w"})
        hostile = b"".join((sink.getvalue(), raw_record(bundle.SYMLINK, b"tree/ln", 3, b"a\0b")))
        with self.assertRaises(bundle.WireError):
            bundle.extract(io.BytesIO(hostile), self.pod)
        self.assertFalse(os.path.lexists(self.pod / "w" / "ln"))

    def test_what_the_operating_system_refuses_is_a_tree_error_not_a_bare_value_error(self):
        fd = os.open(self.pod, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        writer = bundle.TreeWriter(fd)
        with self.assertRaises(bundle.TreeError) as caught:  # a target with a NUL: the writer is used by the delta too
            writer.create_symlink(("ln",), b"a\0b")
        self.assertEqual((caught.exception.reason, str(caught.exception)), ("write", "tree could not be read or written safely"))
        with self.assertRaises(bundle.TreeError) as caught:
            writer.create_file(("a\0b",), [b"x"], 0o644)
        self.assertEqual(caught.exception.reason, "write")
        with self.assertRaises(bundle.TreeError) as caught:
            writer.open_dir(("d\0e",))
        self.assertEqual(caught.exception.reason, "path")
        self.assertEqual(os.listdir(self.pod), [])

    def test_a_stream_cut_off_inside_a_file_stays_a_wire_error_and_is_not_turned_into_a_write_error(self):
        sink = io.BytesIO()
        writer = bundle.Writer(sink)
        writer.put_manifest({"worktree": "w"})
        writer.put(bundle.FILE, bundle.AREA_TREE, "f", b"0123456789")
        for cut in (3, 7):  # inside the payload: the reader's error (a ValueError too) must keep its type
            target = self.root / f"pod{cut}"
            target.mkdir()
            with self.subTest(cut=cut):
                with self.assertRaises(bundle.WireError) as caught:
                    bundle.extract(io.BytesIO(sink.getvalue()[:-cut]), target)
                self.assertEqual(caught.exception.reason, "truncated")

    def test_a_git_file_where_the_skeleton_needs_a_directory_is_an_error(self):
        source = self.bundle_of((bundle.FILE, bundle.AREA_GIT, "HEAD", b"h"), (bundle.FILE, bundle.AREA_GIT, "refs/heads", b"x"))
        with self.assertRaises(bundle.TreeError):
            bundle.extract(source, self.pod)


class Repositories(Tree):
    """A main clone, a bare origin and a linked worktree on the branch ``job``, all real."""

    def setUp(self):
        super().setUp()
        self.repo = self.data / "clone"
        self.origin = self.root / "origin.git"
        self.repo.mkdir()
        plain("init", "-q", "-b", "main", cwd=self.repo)
        (self.repo / "f.txt").write_text("one\ntwo\nthree\n")
        (self.repo / "CLAUDE.md").write_text("trusted rules\n")
        (self.repo / "dir").mkdir()
        (self.repo / "dir" / "a.txt").write_text("a\n")
        (self.repo / "run.sh").write_text("#!/bin/sh\n")
        os.chmod(self.repo / "run.sh", 0o755)
        (self.repo / ".gitignore").write_text("build/\n")
        plain("add", ".", cwd=self.repo)
        plain("commit", "-q", "-m", "base", cwd=self.repo)
        for number in range(4):  # some history below the base, so that a shallow copy really is shallow
            (self.repo / "dir" / f"h{number}.txt").write_text(f"{number}\n")
            plain("add", "--all", cwd=self.repo)
            plain("commit", "-q", "-m", f"history {number}", cwd=self.repo)
        plain("init", "-q", "--bare", os.fspath(self.origin), cwd=self.root)
        plain("remote", "add", "origin", os.fspath(self.origin), cwd=self.repo)
        plain("push", "-q", "-u", "origin", "main", cwd=self.repo)
        self.base = self.rev("main")
        self.tree = self.data / "worktrees" / "job"
        plain("worktree", "add", "-q", "-b", "job", os.fspath(self.tree), cwd=self.repo)
        self.gitdir = gitsafe.resolve_gitdir(self.repo, self.tree)
        gitsafe.trust(self.tree, self.gitdir)

    def rev(self, ref, cwd=None):
        return plain("rev-parse", ref, cwd=cwd or self.repo).stdout.strip()

    def commit(self, message, cwd=None):
        plain("add", "--all", cwd=cwd or self.tree)
        plain("commit", "-q", "-m", message, cwd=cwd or self.tree)

    def stage_and_extract(self, base=None, **extra):
        gitdir = extra.pop("gitdir", self.gitdir)
        staged = bundle.stage(
            self.spool,
            self.session,
            data_root=self.data,
            worktree=self.tree,
            manifest={"argv": ["claude"], "timeout": 60},
            scratch=self.scratch,
            base=base,
            gitdir=gitdir,
            **extra,
        )
        with open(self.bundle_path(), "rb") as source:
            extracted = bundle.extract(source, self.pod)
        self.pod_tree = self.pod / extracted.worktree
        return staged, extracted

    def local(self, *args):
        return gitsafe.run(list(args), self.tree, self.gitdir, check=False)

    def pod_git(self, *args):
        marker = os.path.realpath(self.pod_tree / ".git")
        return gitsafe.run(list(args), self.pod_tree, marker, check=False)

    def assert_same_answers(self, base, *, worktree_state=True):
        """The worker's git answers in the pod equal the ones in-process."""
        questions = [
            ("log", "--format=%H %P %s", f"{base}..HEAD"),
            ("show", "HEAD:CLAUDE.md"),
            ("diff", "--no-color", base, "HEAD"),
            ("rev-parse", "HEAD"),
            ("rev-parse", "--abbrev-ref", "HEAD"),
            ("ls-tree", "-r", "HEAD"),
            ("cat-file", "-t", base),
        ]
        if worktree_state:
            questions += [("status", "--porcelain=v1", "--untracked-files=all"), ("diff", "--no-color"), ("diff", "--no-color", "--stat", base)]
        for question in questions:
            with self.subTest(question=question):
                here, there = self.local(*question), self.pod_git(*question)
                self.assertEqual(there.returncode, here.returncode, there.stderr)
                self.assertEqual(there.stdout, here.stdout)


class GitAnswerTests(Repositories):
    def work(self):
        (self.tree / "f.txt").write_text("one\ntwo changed\nthree\n")
        self.commit("first change")
        (self.tree / "dir" / "b.txt").write_text("b\n")
        self.commit("second change")

    def test_a_branch_with_commits_and_uncommitted_work_answers_like_in_process(self):
        self.work()
        (self.tree / "f.txt").write_text("one\ntwo changed again\nthree\n")
        (self.tree / "new.txt").write_text("untracked\n")
        os.unlink(self.tree / "dir" / "a.txt")
        (self.tree / "build").mkdir()
        (self.tree / "build" / "out.bin").write_bytes(b"\0ignored but shipped")
        os.symlink("f.txt", self.tree / "alias")
        os.chmod(self.tree / "run.sh", 0o644)
        staged, extracted = self.stage_and_extract(base=self.base)
        self.assert_same_answers(self.base)
        here = self.pod_files(self.tree)
        there = self.pod_files(self.pod_tree)
        self.assertEqual(without_git(there), without_git(here))
        self.assertIn("build/out.bin", extracted.baseline)  # every ignore state ships, and is in the baseline
        self.assertEqual(set(extracted.baseline), set(without_git(here)))
        self.assertNotIn("MERGE_HEAD", {k.split("/")[-1] for k in there})
        self.assertEqual(staged.worktree, "worktrees/job")

    def test_the_staged_git_directory_is_self_contained_and_points_nowhere(self):
        self.work()
        self.stage_and_extract(base=self.base)
        data = self.bundle_path().read_bytes()
        for forbidden in (os.fsencode(self.repo), os.fsencode(self.origin), b"alternates", b"[remote", b"[submodule"):
            self.assertNotIn(forbidden, data)
        gitdir = self.pod_tree / ".git"
        self.assertTrue((gitdir / "shallow").exists())
        self.assertFalse((gitdir / "objects" / "info" / "alternates").exists())
        self.assertEqual([p.name for p in (gitdir / "hooks").iterdir()] if (gitdir / "hooks").exists() else [], [])
        self.assertFalse((gitdir / "logs").exists())
        self.assertFalse((gitdir / "FETCH_HEAD").exists())
        self.assertEqual(self.pod_git("config", "--local", "--list").stdout.count("remote."), 0)
        self.assertEqual(self.pod_git("fsck", "--connectivity-only").returncode, 0)
        self.assertEqual(self.pod_git("rev-parse", "refs/factory/base").stdout.strip(), self.base)
        self.assertEqual(self.pod_git("rev-parse", "--is-shallow-repository").stdout.strip(), "true")

    PORCELAIN = ("status", "--porcelain=v1", "--untracked-files=all")

    def test_staged_but_uncommitted_changes_read_differently_in_the_pod_because_its_index_is_the_head_commits(self):
        # A documented difference (docs/pod-backend.md): the supervisor's index is not shipped (slice 9b item 4 keeps it in the
        # supervisor's own git directory, and its blobs are not in the shallow copy), so staged work reads as unstaged work.
        (self.tree / "f.txt").write_text("one\ntwo staged\nthree\n")
        (self.tree / "new.txt").write_text("staged new file\n")
        plain("add", "f.txt", "new.txt", cwd=self.tree)
        plain("rm", "-q", "dir/a.txt", cwd=self.tree)
        self.stage_and_extract(base=self.base)
        self.assertEqual(self.local(*self.PORCELAIN).stdout, "D  dir/a.txt\nM  f.txt\nA  new.txt\n")
        self.assertEqual(self.pod_git(*self.PORCELAIN).stdout, " D dir/a.txt\n M f.txt\n?? new.txt\n")
        self.assertEqual(self.local("diff", "--no-color").stdout, "")
        self.assertIn("two staged", self.pod_git("diff", "--no-color").stdout)
        self.assertIn("two staged", self.local("diff", "--no-color", "--cached").stdout)
        self.assertEqual(self.pod_git("diff", "--no-color", "--cached").stdout, "")
        self.assert_same_answers(self.base, worktree_state=False)  # what does not read the index is equal
        self.assertEqual(without_git(self.pod_files(self.pod_tree)), without_git(self.pod_files(self.tree)))  # and so are the files

    def test_the_local_excludes_and_attributes_of_the_supervisor_travel_with_the_git_directory(self):
        info = self.repo / ".git" / "info"
        info.mkdir(exist_ok=True)
        (info / "exclude").write_text("localonly.txt\n")
        (info / "attributes").write_text("*.dat -diff\n")
        (self.tree / "localonly.txt").write_text("hidden from status here\n")
        (self.tree / "new.txt").write_text("visible\n")
        (self.tree / "x.dat").write_text("data\n")
        _staged, extracted = self.stage_and_extract(base=self.base)
        here = self.local(*self.PORCELAIN).stdout
        self.assertEqual(here, "?? new.txt\n?? x.dat\n")
        self.assertEqual(self.pod_git(*self.PORCELAIN).stdout, here)
        self.assert_same_answers(self.base)
        attributes = ("check-attr", "diff", "--", "x.dat", "f.txt")
        self.assertEqual(self.local(*attributes).stdout, "x.dat: diff: unset\nf.txt: diff: unspecified\n")
        self.assertEqual(self.pod_git(*attributes).stdout, self.local(*attributes).stdout)
        self.assertIn("localonly.txt", extracted.baseline)  # the file itself ships, whatever its ignore state
        for name, content in (("exclude", b"localonly.txt\n"), ("attributes", b"*.dat -diff\n")):
            copy = os.lstat(self.pod_tree / ".git" / "info" / name)  # a plain file of its own, never a link
            self.assertTrue(stat.S_ISREG(copy.st_mode) and copy.st_nlink == 1, name)
            self.assertEqual((self.pod_tree / ".git" / "info" / name).read_bytes(), content)

    def staging_error(self):
        """The ``BundleError`` that staging this repository raises, or None. It runs in a thread: a fifo must not hang the test."""
        outcome = []

        def run():
            try:
                self.stage_and_extract(base=self.base)
            except bundle.BundleError as caught:
                outcome.append(caught)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(60)
        self.assertFalse(worker.is_alive(), "staging waited on a fifo")
        return outcome[0] if outcome else None

    def test_a_local_exclude_that_is_not_a_small_regular_file_is_refused_and_nothing_is_left_behind(self):
        info = self.repo / ".git" / "info"
        info.mkdir(exist_ok=True)
        secret = self.root / "secret.txt"
        secret.write_text("TOP-SECRET-BYTES\n")
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "exclude").write_text("TOP-SECRET-BYTES\n")

        def make_link():
            (info / "exclude").symlink_to(secret)

        def make_fifo():
            os.mkfifo(info / "exclude")

        def make_big():
            (info / "exclude").write_bytes(b"#" * (bundle.INFO_MAX_BYTES + 1))

        def make_linked_directory():
            shutil.move(info, self.root / "real-info")
            info.symlink_to(elsewhere)

        cases = (("link", make_link, bundle.TreeError), ("fifo", make_fifo, bundle.TreeError), ("big", make_big, bundle.BundleTooLarge))
        for name, make, error in (*cases, ("linked directory", make_linked_directory, bundle.TreeError)):
            with self.subTest(name):
                shutil.rmtree(info, ignore_errors=True)
                info.unlink(missing_ok=True)
                info.mkdir()
                make()
                self.assertIsInstance(self.staging_error(), error)
                self.assertFalse(self.bundle_path().exists())
                self.assertEqual(list(self.scratch.iterdir()), [])
        small = bundle.INFO_MAX_BYTES  # the limit itself is fine
        shutil.rmtree(info, ignore_errors=True)
        info.unlink(missing_ok=True)
        info.mkdir()
        (info / "exclude").write_bytes(b"#" * small)
        self.stage_and_extract(base=self.base)
        self.assertEqual(len((self.pod_tree / ".git" / "info" / "exclude").read_bytes()), small)

    def test_the_repository_configuration_of_the_supervisor_is_not_carried(self):
        plain("config", "status.showUntrackedFiles", "no", cwd=self.repo)  # the common configuration, read by the worktree
        (self.tree / "new.txt").write_text("untracked\n")
        self.stage_and_extract(base=self.base)
        self.assertEqual(self.local("status", "--porcelain=v1").stdout, "")
        self.assertEqual(self.pod_git("status", "--porcelain=v1").stdout, "?? new.txt\n")
        self.assertEqual(self.pod_git("config", "--local", "--get", "status.showuntrackedfiles").returncode, 1)

    def test_the_pod_holds_the_job_branch_and_the_base_and_no_other_ref(self):
        self.work()
        plain("tag", "v1", cwd=self.repo)
        plain("branch", "other", cwd=self.repo)
        self.stage_and_extract(base=self.base)
        names = ("for-each-ref", "--format=%(refname)")
        here = set(self.local(*names).stdout.split())
        self.assertLessEqual({"refs/tags/v1", "refs/heads/other", "refs/heads/main", "refs/remotes/origin/main"}, here)
        self.assertEqual(self.pod_git(*names).stdout.split(), ["refs/factory/base", "refs/heads/job"])

    def test_a_merged_branch_answers_like_in_process(self):
        self.work()
        (self.repo / "other.txt").write_text("from main\n")
        plain("add", "other.txt", cwd=self.repo)
        plain("commit", "-q", "-m", "main moved", cwd=self.repo)
        new_base = self.rev("main")
        plain("merge", "-q", "--no-edit", "-m", "merge main", new_base, cwd=self.tree)
        (self.tree / "after.txt").write_text("after the merge\n")
        self.stage_and_extract(base=new_base)
        self.assertEqual(len(self.local("log", "--merges", "--format=%H", f"{new_base}..HEAD").stdout.split()), 1)
        self.assert_same_answers(new_base)

    def test_a_merge_in_progress_with_conflicts_answers_like_in_process_where_the_spec_says_so(self):
        (self.tree / "f.txt").write_text("one\nbranch side\nthree\n")
        self.commit("branch edit")
        (self.repo / "f.txt").write_text("one\nmain side\nthree\n")
        (self.repo / "other.txt").write_text("clean from main\n")
        plain("add", "--all", cwd=self.repo)
        plain("commit", "-q", "-m", "main edit", cwd=self.repo)
        for number in range(30):  # main has moved far past the fork point: the fork point must stay reachable from the base
            (self.repo / "dir" / f"m{number}.txt").write_text(f"{number}\n")
            plain("add", "--all", cwd=self.repo)
            plain("commit", "-q", "-m", f"main {number}", cwd=self.repo)
        new_base = self.rev("main")
        merged = plain("merge", "--no-edit", new_base, cwd=self.tree, check=False)
        self.assertNotEqual(merged.returncode, 0)
        self.assertEqual(self.local("rev-parse", "-q", "--verify", "MERGE_HEAD").returncode, 0)
        self.stage_and_extract(base=new_base)
        self.assertIn("<<<<<<<", (self.pod_tree / "f.txt").read_text())
        self.assertEqual((self.pod_tree / "f.txt").read_bytes(), (self.tree / "f.txt").read_bytes())
        self.assert_same_answers(new_base, worktree_state=False)
        # The merge state lives only in the supervisor's git directory (slice 9b item 4): the pod sees the
        # conflict as ordinary edits of the head commit, so ITS status and worktree diff differ by design.
        self.assertEqual(self.pod_git("rev-parse", "-q", "--verify", "MERGE_HEAD").returncode, 1)
        self.assertIn("UU f.txt", self.local("status", "--porcelain=v1").stdout)
        self.assertIn(" M f.txt", self.pod_git("status", "--porcelain=v1").stdout)
        self.assertEqual(
            self.pod_git("log", "--format=%s", f"{new_base}..HEAD").stdout.split("\n")[:-1], ["branch edit"]
        )

    def test_a_detached_worktree_at_the_base_commit_is_staged_too(self):
        detached = self.data / "worktrees" / "baseline"
        plain("worktree", "add", "-q", "--detach", os.fspath(detached), self.base, cwd=self.repo)
        gitdir = gitsafe.resolve_gitdir(self.repo, detached)
        gitsafe.trust(detached, gitdir)
        self.tree = detached
        self.gitdir = gitdir
        self.stage_and_extract(base=self.base)
        self.assertEqual(self.pod_git("rev-parse", "HEAD").stdout.strip(), self.base)
        self.assertEqual(self.pod_git("symbolic-ref", "-q", "HEAD").returncode, 1)
        self.assert_same_answers(self.base)

    def test_a_detached_head_without_a_base_is_still_a_git_repository(self):
        detached = self.data / "worktrees" / "bare-head"
        plain("worktree", "add", "-q", "--detach", os.fspath(detached), self.base, cwd=self.repo)
        gitdir = gitsafe.resolve_gitdir(self.repo, detached)
        gitsafe.trust(detached, gitdir)
        self.tree = detached
        self.gitdir = gitdir
        self.stage_and_extract(base=None)
        self.assertFalse(any(name.startswith("refs/") for name in self.refs()))  # no ref at all travels: nothing implies refs/
        for question in (("rev-parse", "HEAD"), ("status", "--porcelain=v1"), ("log", "--oneline"), ("rev-parse", "--git-dir")):
            with self.subTest(question=question):
                done = self.pod_git(*question)
                self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.pod_git("rev-parse", "HEAD").stdout.strip(), self.base)
        self.assertEqual(self.pod_git("status", "--porcelain=v1").stdout, "")

    def refs(self):
        """The ref files of the staged bundle, as the bundle names them."""
        with open(self.bundle_path(), "rb") as source:
            return [path for _, _, area, path, _ in drain(source) if area == bundle.AREA_GIT and path.startswith("refs/")]

    def test_without_a_base_only_the_head_is_staged(self):
        self.work()
        self.stage_and_extract(base=None)
        self.assertEqual(self.pod_git("rev-list", "--count", "HEAD").stdout.strip(), "1")
        self.assertEqual(self.pod_git("show", "HEAD:CLAUDE.md").stdout, "trusted rules\n")

    def test_submodule_files_are_staged_without_gitlink_metadata(self):
        sub = self.root / "sub"
        sub.mkdir()
        plain("init", "-q", "-b", "main", cwd=sub)
        (sub / "s.txt").write_text("submodule file\n")
        plain("add", ".", cwd=sub)
        plain("commit", "-q", "-m", "sub", cwd=sub)
        plain("-c", "protocol.file.allow=always", "submodule", "add", "-q", os.fspath(sub), "vendor/sub", cwd=self.tree)
        self.commit("add submodule")
        shutil_tree = self.tree / "vendor" / "sub"
        self.assertTrue((shutil_tree / ".git").is_file())  # the pointer git wrote into the supervisor's modules directory
        staged, extracted = self.stage_and_extract(base=self.base)
        self.assertEqual((self.pod_tree / "vendor" / "sub" / "s.txt").read_text(), "submodule file\n")
        self.assertIn("vendor/sub/s.txt", extracted.baseline)
        self.assertFalse((self.pod_tree / "vendor" / "sub" / ".git").exists())
        self.assertFalse((self.pod_tree / ".git" / "modules").exists())
        self.assertEqual(self.pod_git("config", "--local", "--list").stdout.count("submodule"), 0)
        self.assertGreaterEqual(staged.git_names, 2)  # the worktree's own pointer and the submodule's
        self.assertEqual(self.pod_git("ls-tree", "HEAD", "vendor/sub").stdout, self.local("ls-tree", "HEAD", "vendor/sub").stdout)

    def test_a_rewritten_git_pointer_stops_staging_before_any_git_runs_on_the_tree(self):
        marker = self.root / "MARKER"
        (self.tree / ".git").write_text(f"gitdir: {self.root / 'evil'}\n")
        (self.root / "evil").mkdir()
        with self.assertRaises(gitsafe.GitSafetyError):
            self.stage_and_extract(base=self.base)
        self.assertFalse(marker.exists())
        self.assertFalse(self.bundle_path().exists())
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_a_planted_fsmonitor_in_a_nested_git_directory_is_not_shipped_and_never_runs(self):
        marker = self.root / "MARKER"
        planted = self.tree / "vendor" / "lib" / ".git"
        planted.mkdir(parents=True)
        (planted / "config").write_text(f"[core]\n\tfsmonitor = touch {marker}\n")
        self.stage_and_extract(base=self.base)
        self.assertFalse(marker.exists())
        self.assertFalse((self.pod_tree / "vendor" / "lib" / ".git").exists())
        self.assertNotIn(b"fsmonitor", self.bundle_path().read_bytes())

    def test_the_history_depth_follows_the_commits_the_base_range_needs(self):
        self.work()
        head = self.rev("HEAD", self.tree)
        self.assertEqual(bundle._depth(self.tree, self.gitdir, head, self.base, gitsafe.PROCESS_AUTH), 3)
        self.assertEqual(bundle._depth(self.tree, self.gitdir, head, None, gitsafe.PROCESS_AUTH), 1)
        with patch.object(bundle, "MAX_HISTORY", 2), self.assertRaises(bundle.BundleTooLarge):
            bundle._depth(self.tree, self.gitdir, head, self.base, gitsafe.PROCESS_AUTH)

    def test_a_base_that_is_not_a_commit_id_is_refused_before_it_reaches_git(self):
        for base in ("--upload-pack=touch x", "main", "HEAD~1", self.base[:10], f"{self.base}; ls"):
            with self.subTest(base=base), self.assertRaises(bundle.TreeError):
                self.stage_and_extract(base=base)


def pod_delta(pod_tree, baseline):
    """The delta a pod would send: what differs from the baseline it was given, written with the same module the supervisor reads it with.

    A test stand-in for the pod worker (slice 10b): ``walk`` never follows a link and never enters a git directory, every
    path whose kind, exec bit or hash differs from ``baseline`` is a ``file`` or ``symlink`` record, and a baseline path
    that is gone is a ``delete``.
    """
    sink = io.BytesIO()
    writer = bundle.Writer(sink, bundle.DELTA)
    seen = set()
    root_fd = os.open(pod_tree, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with contextlib.closing(bundle.walk(root_fd, bundle.AREA_TREE, bundle.Totals())) as entries:
            for entry in entries:
                seen.add(entry.path)
                if baseline.get(entry.path) == (entry.kind, entry.executable, bundle.digest_of(entry)):
                    continue
                if entry.kind == bundle.SYMLINK:
                    writer.put(bundle.SYMLINK, bundle.AREA_TREE, entry.path, entry.target)
                    continue
                fd = bundle.open_file(entry)
                try:
                    writer.put_file(bundle.AREA_TREE, entry.path, fd, entry.size, entry.executable)
                finally:
                    os.close(fd)
    finally:
        os.close(root_fd)
    for path in sorted(set(baseline) - seen):
        writer.put(bundle.DELETE, bundle.AREA_TREE, path)
    return sink.getvalue()


class DeltaRoundTripTests(Repositories):
    """Stage a real worktree, let a 'pod' change its copy, apply the delta it returns, and compare with in-process mode."""

    def setUp(self):
        super().setUp()
        self.staged, self.extracted = self.stage_and_extract(base=self.base)

    def delta(self):
        return pod_delta(self.pod_tree, self.extracted.baseline)

    def apply(self, data, scope="cwd", **extra):
        return bundle.apply_delta(io.BytesIO(data), self.tree, write_scope=scope, scratch=self.scratch, **extra)

    def assert_same_tree(self):
        self.assertEqual(without_git(self.pod_files(self.tree)), without_git(self.pod_files(self.pod_tree)))

    def status(self):
        return self.local("status", "--porcelain=v1", "--untracked-files=all").stdout.splitlines()

    def test_an_unchanged_pod_tree_returns_an_empty_delta_that_changes_nothing(self):
        data = self.delta()
        self.assertEqual(data, bundle.HEADER)
        before = self.pod_files(self.tree)
        self.assertEqual(self.apply(data), bundle.Applied(0, 0, 0, 0, len(bundle.HEADER)))
        self.assertEqual(self.pod_files(self.tree), before)
        self.assertEqual(self.status(), [])

    def test_an_edit_an_add_a_delete_a_rename_an_exec_change_and_symlinks_round_trip(self):
        (self.pod_tree / "f.txt").write_text("one\ntwo changed\nthree\n")  # an edit
        (self.pod_tree / "added").mkdir()
        (self.pod_tree / "added" / "new.txt").write_text("new\n")  # an add, in a new directory
        os.unlink(self.pod_tree / "dir" / "a.txt")  # a delete
        os.rename(self.pod_tree / "dir" / "h0.txt", self.pod_tree / "dir" / "renamed.txt")  # a rename: a delete and an add
        os.chmod(self.pod_tree / "run.sh", 0o644)  # the exec bit goes
        os.chmod(self.pod_tree / "CLAUDE.md", 0o755)  # and comes
        os.symlink("f.txt", self.pod_tree / "alias")  # a new link
        os.unlink(self.pod_tree / "dir" / "h1.txt")
        os.symlink("../f.txt", self.pod_tree / "dir" / "h1.txt")  # a file becomes a link
        (self.pod_tree / "build").mkdir()
        (self.pod_tree / "build" / "out.bin").write_bytes(b"\0ignored")  # ignored files come back too
        (self.pod_tree / "untracked.txt").write_text("untracked\n")
        data = self.delta()
        applied = self.apply(data)
        self.assertEqual((applied.files, applied.symlinks, applied.deleted), (7, 2, 2))
        self.assert_same_tree()
        self.assertEqual(sorted(self.status()), sorted([
            " M f.txt", " M CLAUDE.md", " M run.sh", " D dir/a.txt", " D dir/h0.txt", "?? dir/renamed.txt", " T dir/h1.txt",
            "?? added/new.txt", "?? alias", "?? untracked.txt",
        ]))
        self.assertIn("old mode 100755\nnew mode 100644", self.local("diff", "--no-color", "run.sh").stdout)
        self.assertEqual(stat.S_IMODE(os.lstat(self.tree / "CLAUDE.md").st_mode), 0o755)
        self.assertEqual((self.tree / "build" / "out.bin").read_bytes(), b"\0ignored")
        self.assertTrue((self.tree / ".git").is_file())  # the supervisor's own pointer was never in the way
        self.assertEqual({item[2] for item in drain(io.BytesIO(data), bundle.DELTA)}, {bundle.AREA_TREE})  # tree paths only, no git file

    def test_a_file_becomes_a_directory_and_a_directory_becomes_a_file(self):
        os.unlink(self.pod_tree / "CLAUDE.md")
        os.mkdir(self.pod_tree / "CLAUDE.md")
        (self.pod_tree / "CLAUDE.md" / "inner.md").write_text("inner\n")
        shutil.rmtree(self.pod_tree / "dir")
        (self.pod_tree / "dir").write_text("a file now\n")
        self.apply(self.delta())
        self.assert_same_tree()
        self.assertTrue((self.tree / "CLAUDE.md").is_dir())
        self.assertEqual((self.tree / "dir").read_text(), "a file now\n")

    def test_the_pods_git_directory_never_reaches_the_supervisor(self):
        marker = self.root / "MARKER"
        pod_git = self.pod_tree / ".git"
        (pod_git / "config").write_text(f"[core]\n\tfsmonitor = touch {marker}\n")
        (pod_git / "hooks").mkdir()
        (pod_git / "hooks" / "pre-commit").write_text(f"#!/bin/sh\ntouch {marker}\n")
        (pod_git / "MERGE_HEAD").write_text(f"{self.base}\n")
        (self.pod_tree / "legit.txt").write_text("legit\n")
        before = self.pod_files(self.gitdir)
        pointer = (self.tree / ".git").read_bytes()
        data = self.delta()
        self.assertEqual([item[3] for item in drain(io.BytesIO(data), bundle.DELTA)], ["legit.txt"])
        self.apply(data)
        self.assertEqual(self.pod_files(self.gitdir), before)
        self.assertEqual((self.tree / ".git").read_bytes(), pointer)
        self.assertEqual(self.local("status", "--porcelain=v1").returncode, 0)
        self.assertFalse(marker.exists())
        before = self.pod_files(self.gitdir)  # git status above may have refreshed its own index: measure again
        forged = b"".join((bundle.HEADER, raw_record(bundle.FILE, b"tree/.git/config", 4, b"evil")))
        with self.assertRaises(bundle.DeltaRejected):
            self.apply(forged)
        self.assertEqual(self.pod_files(self.gitdir), before)

    def test_a_read_only_phase_must_return_an_empty_delta(self):
        self.assertEqual(self.apply(self.delta(), scope="none").records, 0)
        (self.pod_tree / "f.txt").write_text("a read-only phase wrote this\n")
        before = self.pod_files(self.tree)
        with self.assertRaises(bundle.DeltaRejected) as caught:
            self.apply(self.delta(), scope="none")
        self.assertEqual((caught.exception.reason, str(caught.exception)), ("scope", "delta rejected"))
        self.assertEqual(self.pod_files(self.tree), before)

    def test_a_cap_breach_fails_with_delta_too_large_and_writes_nothing(self):
        (self.pod_tree / "f.txt").write_text("x" * 500)
        (self.pod_tree / "other.txt").write_text("y\n")
        data = self.delta()
        before = self.pod_files(self.tree)
        with patch.object(pc, "FILE_MAX_BYTES", 100), self.assertRaises(bundle.DeltaTooLarge) as caught:
            self.apply(data)
        self.assertEqual((caught.exception.reason, str(caught.exception)), ("size", "delta too large"))
        with self.assertRaises(bundle.DeltaTooLarge) as caught:
            self.apply(data, limits=bundle.Limits(max_bytes=len(data) - 1, max_files=10))
        self.assertEqual(caught.exception.reason, "bytes")
        with self.assertRaises(bundle.DeltaTooLarge) as caught:
            self.apply(data, limits=bundle.Limits(max_bytes=10_000, max_files=1))
        self.assertEqual(caught.exception.reason, "files")
        self.assertEqual(self.pod_files(self.tree), before)
        self.assertEqual(self.apply(data, limits=bundle.Limits(max_bytes=len(data), max_files=2)).files, 2)  # exactly at the caps

    def test_a_symlink_planted_in_the_supervisors_tree_is_not_written_through(self):
        outside = self.root / "outside"
        outside.mkdir()
        os.symlink(outside, self.tree / "planted")  # by an earlier worker, in a tree the pod never saw as a directory
        (self.pod_tree / "planted").mkdir()
        (self.pod_tree / "planted" / "pwned.txt").write_text("x\n")
        with self.assertRaises(bundle.DeltaRejected):
            self.apply(self.delta())
        self.assertEqual(list(outside.iterdir()), [])

    def test_the_delta_applies_twice_with_the_same_result(self):
        (self.pod_tree / "f.txt").write_text("changed\n")
        os.unlink(self.pod_tree / "dir" / "a.txt")
        data = self.delta()
        self.apply(data)
        once = self.pod_files(self.tree)
        self.apply(data)
        self.assertEqual(self.pod_files(self.tree), once)

    def test_a_submodule_file_edited_by_the_pod_lands_in_the_submodule_and_its_pointer_is_untouched(self):
        sub = self.root / "sub"
        sub.mkdir()
        plain("init", "-q", "-b", "main", cwd=sub)
        (sub / "s.txt").write_text("submodule file\n")
        plain("add", ".", cwd=sub)
        plain("commit", "-q", "-m", "sub", cwd=sub)
        plain("-c", "protocol.file.allow=always", "submodule", "add", "-q", os.fspath(sub), "vendor/sub", cwd=self.tree)
        self.commit("add submodule")
        self.pod = self.root / "pod-with-submodule"  # a second staging: a new pod directory and a new session
        self.pod.mkdir()
        self.session = pc.new_session_id()
        self.staged, self.extracted = self.stage_and_extract(base=self.base)
        pointer = (self.tree / "vendor" / "sub" / ".git").read_bytes()
        (self.pod_tree / "vendor" / "sub" / "s.txt").write_text("edited in the pod\n")
        (self.pod_tree / "vendor" / "sub" / "added.txt").write_text("added\n")
        data = self.delta()
        self.assertEqual(sorted(item[3] for item in drain(io.BytesIO(data), bundle.DELTA)), ["vendor/sub/added.txt", "vendor/sub/s.txt"])
        self.apply(data)
        self.assertEqual((self.tree / "vendor" / "sub" / "s.txt").read_text(), "edited in the pod\n")
        self.assertEqual((self.tree / "vendor" / "sub" / ".git").read_bytes(), pointer)
        inner = plain("status", "--porcelain=v1", "--untracked-files=all", cwd=self.tree / "vendor" / "sub").stdout.splitlines()
        self.assertEqual(sorted(inner), [" M s.txt", "?? added.txt"])
        forged = b"".join((bundle.HEADER, raw_record(bundle.FILE, b"tree/vendor/sub/.git", 4, b"evil")))
        with self.assertRaises(bundle.DeltaRejected):
            self.apply(forged)
        self.assertEqual((self.tree / "vendor" / "sub" / ".git").read_bytes(), pointer)


class ConflictedMergeCommitTests(unittest.TestCase):
    """The merge state lives in the supervisor's git directory: the delta returns the resolved files and the UNCHANGED ``commit()`` concludes it."""

    def setUp(self):
        self.harness = test_branch_sync.BranchSyncTests("test_conflict_is_left_in_the_worktree_for_the_builder")
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.spool = Path(self.temp.name) / "spool"
        self.scratch = Path(self.temp.name) / "scratch"
        self.pod = Path(self.temp.name) / "pod"
        for directory in (self.spool, self.scratch, self.pod):
            directory.mkdir()

    def conflicted(self):
        """The supervisor's worktree with a base merge in progress, staged and extracted; returns what the test needs."""
        harness = self.harness
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):  # the fixture runs a whole job first
            runner, worktree, state, _old = harness.prepared({"src/a.txt": "sibling\n"})
            job = assignment()["jobs"][0]
            self.assertEqual(runner.sync_branch(job, worktree), ["src/a.txt"])
        harness.git("rev-parse", "-q", "--verify", "MERGE_HEAD", cwd=worktree)  # the merge is in progress in the supervisor's git directory
        gitdir = gitsafe.resolve_gitdir(harness.repo, worktree)
        gitsafe.trust(Path(worktree), gitdir)
        base = runner.state.data["jobs"][test_branch_sync.JOB]["base"]
        session = pc.new_session_id()
        bundle.stage(
            self.spool, session, data_root=harness.root, worktree=worktree, manifest={}, scratch=self.scratch, base=base, gitdir=gitdir
        )
        with open(self.spool / session / sr.IN_DIR / sr.BUNDLE_FILE, "rb") as source:
            extracted = bundle.extract(source, self.pod)
        return runner, job, Path(worktree), self.pod / extracted.worktree, extracted

    def deliver(self, worktree, pod_tree, extracted):
        data = pod_delta(pod_tree, extracted.baseline)
        return bundle.apply_delta(io.BytesIO(data), worktree, write_scope="cwd", scratch=self.scratch)

    def test_the_pod_resolves_the_conflict_and_the_unchanged_commit_concludes_the_merge(self):
        runner, job, worktree, pod_tree, extracted = self.conflicted()
        self.assertIn("<<<<<<<", (pod_tree / "src" / "a.txt").read_text())  # the staged copy carries the conflict-marker file
        (pod_tree / "src" / "a.txt").write_text("resolved\n")
        pod_git = pod_tree / ".git"
        (pod_git / "MERGE_HEAD").write_text(f"{'0' * 40}\n")  # whatever the pod writes in ITS git directory goes nowhere
        applied = self.deliver(worktree, pod_tree, extracted)
        self.assertEqual((applied.files, applied.deleted), (1, 0))
        harness = self.harness
        harness.git("rev-parse", "--verify", "MERGE_HEAD", cwd=worktree)  # still merging: only commit() concludes it
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            head = runner.commit(job, worktree)
        self.assertEqual(head, harness.git("rev-parse", "HEAD", cwd=worktree))
        self.assertEqual(len(harness.git("rev-list", "--parents", "-n1", "HEAD", cwd=worktree).split()), 3)  # a merge commit
        self.assertEqual((worktree / "src" / "a.txt").read_text(), "resolved\n")
        base = runner.state.data["jobs"][test_branch_sync.JOB]["base"]
        harness.git("merge-base", "--is-ancestor", base, head, cwd=worktree)

    def test_markers_the_pod_left_in_the_file_are_refused_by_the_unchanged_commit(self):
        runner, job, worktree, pod_tree, extracted = self.conflicted()
        with open(pod_tree / "src" / "a.txt", "a") as conflicted:
            conflicted.write("still unresolved\n")  # the conflict markers stay
        self.deliver(worktree, pod_tree, extracted)
        head = self.harness.git("rev-parse", "HEAD", cwd=worktree)
        with self.assertRaises(RuntimeError) as caught:
            runner.commit(job, worktree)
        self.assertIn("unresolved merge conflict markers", str(caught.exception))
        self.assertEqual(self.harness.git("rev-parse", "HEAD", cwd=worktree), head)
        self.harness.git("rev-parse", "--verify", "MERGE_HEAD", cwd=worktree)

    def test_a_chain_of_links_at_the_conflicted_path_is_rejected_so_that_commit_never_follows_it_out_of_the_worktree(self):
        runner, job, worktree, pod_tree, extracted = self.conflicted()
        chains = {  # each is lexically inside the tree and, followed through the first link, ends above it
            "a link to its own directory": (("tree/b", b"."), ("tree/src/a.txt", b"../b/b/b/../../../x")),
            "one hop per link": (("tree/b", b"."), ("tree/c", b"b/.."), ("tree/src/a.txt", b"../c/../x")),
        }
        before = (worktree / "src" / "a.txt").read_bytes()
        for label, links in chains.items():
            data = b"".join((bundle.HEADER, *(raw_record(bundle.SYMLINK, path.encode(), len(target), target) for path, target in links)))
            with self.subTest(label), self.assertRaises(bundle.DeltaRejected) as caught:
                bundle.apply_delta(io.BytesIO(data), worktree, write_scope="cwd", scratch=self.scratch)
            self.assertEqual(caught.exception.reason, "link")
            self.assertFalse((worktree / "src" / "a.txt").is_symlink())
            self.assertEqual((worktree / "src" / "a.txt").read_bytes(), before)
            self.assertFalse((worktree / "b").exists())
        with self.assertRaises(RuntimeError) as markers:  # the file in the worktree is the one with the markers, read as itself
            runner.commit(job, worktree)
        self.assertIn("unresolved merge conflict markers remain in src/a.txt", str(markers.exception))


if __name__ == "__main__":
    unittest.main()

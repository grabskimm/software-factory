"""The retained copy of a run's logs: redacted, compressed, swept after N days (docs/manager.md, "Log retention").

The local directory backend is the default and is what most tests exercise; the Azure Blob backend runs on an
injected fake transport (no network). Purge, the CLI hook and the janitor are tested where they use it.
"""

import contextlib
import gzip
import io
import json
import os
import shutil
import stat
import tempfile
import time
import unittest
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest import mock

from factory import cli, logarchive, redact
from factory.logarchive import ArchiveConfigError, ArchiveError, BlobBackend, Config, LocalBackend, Reply
from factory.manager import CommandResult
from factory.purge import purge_run
from tests import test_operations as ops
from tests.test_purge import PurgeCase
from tests.test_logs import ANTHROPIC, JWT, PAT, TOKEN

KEY = f"-----BEGIN PRIVATE KEY-----\n{'M' * 64}\n-----END PRIVATE KEY-----"
SECRETS = (TOKEN, PAT, ANTHROPIC, JWT, "M" * 64)


class ArchiveCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name).resolve()
        self.run = self.data / "repos" / "org" / "name" / "runs" / "r1"
        self.job = self.run / "fix-a"
        self.job.mkdir(parents=True)
        self.archive = self.data / "logs-archive"
        self.write_logs()

    def write_logs(self):
        (self.run / "plan.log").write_text(f"planning {TOKEN}\n")
        (self.run / "supervisor.log").write_text(f"started\n{KEY}\ndone\n")
        (self.run / "planner.jsonl").write_text(f'{json.dumps({"text": ANTHROPIC})}\n')
        events = [{"text": f"token {PAT}"}, {"text": "fine", "jwt": JWT}]
        (self.job / "1-build-0.jsonl").write_text("".join(f"{json.dumps(event)}\n" for event in events))
        (self.job / "1-build-0.stderr").write_text("warning\n")
        for name in ("1-build-0.settings.json", "1-build-0.prompt.txt", "1-build-0.system.md", "1-build-0.json", "pr-body.md"):
            (self.job / name).write_text(f"NOT A LOG {TOKEN}\n")
        (self.run / "state.json").write_text('{"jobs": {}}')
        (self.run / "worktrees" / "fix-a").mkdir(parents=True)
        (self.run / "worktrees" / "fix-a" / "1-build-0.jsonl").write_text(f"{TOKEN}\n")

    def files(self):
        return sorted(str(path.relative_to(self.archive)) for path in self.archive.rglob("*") if path.is_file())

    def read(self, relative):
        return gzip.decompress((self.archive / relative).read_bytes()).decode()

    def archive_run(self, backend=None):
        return logarchive.archive_run(self.run, "org/name", backend or LocalBackend(self.archive))


class ConfigTests(unittest.TestCase):
    def test_the_defaults_are_a_local_copy_kept_thirty_days(self):
        self.assertEqual(Config.from_env({}), Config(True, "", "logs-archive", 30))

    def test_every_setting(self):
        config = Config.from_env(
            {
                "FACTORY_LOGS_ARCHIVE": "OFF",
                "FACTORY_LOGS_ARCHIVE_ACCOUNT_URL": "https://acct.blob.core.windows.net/",
                "FACTORY_LOGS_ARCHIVE_CONTAINER": "my-logs",
                "FACTORY_LOGS_ARCHIVE_RETENTION_DAYS": "7",
            }
        )
        self.assertEqual(config, Config(False, "https://acct.blob.core.windows.net/", "my-logs", 7))

    def test_invalid_settings_are_refused(self):
        for name, value in (
            ("FACTORY_LOGS_ARCHIVE", "maybe"),
            ("FACTORY_LOGS_ARCHIVE_RETENTION_DAYS", "0"),
            ("FACTORY_LOGS_ARCHIVE_RETENTION_DAYS", "3651"),
            ("FACTORY_LOGS_ARCHIVE_RETENTION_DAYS", "1.5"),
            ("FACTORY_LOGS_ARCHIVE_RETENTION_DAYS", "-3"),
            ("FACTORY_LOGS_ARCHIVE_ACCOUNT_URL", "http://acct.blob.core.windows.net"),
            ("FACTORY_LOGS_ARCHIVE_ACCOUNT_URL", "https://user:pass@acct.blob.core.windows.net"),
            ("FACTORY_LOGS_ARCHIVE_ACCOUNT_URL", "https://acct.blob.core.windows.net/?sig=x"),
        ):
            with self.subTest(name=name, value=value), self.assertRaises(ArchiveConfigError):
                Config.from_env({name: value})
        with self.assertRaises(ArchiveConfigError):
            Config.from_env({"FACTORY_LOGS_ARCHIVE_ACCOUNT_URL": "https://a.example", "FACTORY_LOGS_ARCHIVE_CONTAINER": "Bad_Name"})

    def test_the_manager_refuses_to_start_with_an_invalid_setting(self):
        from factory.manager import Settings

        env = {"FACTORY_CONFIG": tempfile.gettempdir(), "FACTORY_DATA": tempfile.gettempdir(), "FACTORY_LOGS_ARCHIVE_RETENTION_DAYS": "0"}
        with self.assertRaises(ValueError):
            Settings.from_env(env)


class LocalArchiveTests(ArchiveCase):
    def test_the_allow_listed_logs_are_copied_compressed_under_the_run(self):
        result = self.archive_run()
        self.assertEqual(
            self.files(),
            [
                "org__name/r1/fix-a/1-build-0.jsonl.gz",
                "org__name/r1/fix-a/1-build-0.stderr.gz",
                "org__name/r1/plan.log.gz",
                "org__name/r1/planner.jsonl.gz",
                "org__name/r1/supervisor.log.gz",
            ],
        )
        self.assertEqual((result.files, result.skipped), (5, 0))
        self.assertEqual(self.read("org__name/r1/fix-a/1-build-0.stderr.gz"), "warning\n")

    def test_what_is_copied_is_redacted_and_nothing_else_is(self):
        self.archive_run()
        for path in self.archive.rglob("*"):
            if path.is_file():
                text = gzip.decompress(path.read_bytes()).decode()
                for secret in SECRETS:
                    self.assertNotIn(secret, text, path.name)
                self.assertNotIn("NOT A LOG", text)
        self.assertEqual(self.read("org__name/r1/plan.log.gz"), "planning [REDACTED]\n")
        self.assertEqual(self.read("org__name/r1/supervisor.log.gz"), "started\n[REDACTED]\ndone\n")
        self.assertEqual(len(self.read("org__name/r1/fix-a/1-build-0.jsonl.gz").splitlines()), 2)

    def test_files_are_private_and_a_second_copy_replaces_the_first(self):
        self.archive_run()
        path = self.archive / "org__name" / "r1" / "plan.log.gz"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.archive / "org__name").stat().st_mode), 0o700)
        (self.run / "plan.log").write_text("later\n")
        self.archive_run()
        self.assertEqual(self.read("org__name/r1/plan.log.gz"), "later\n")
        self.assertEqual([name for name in self.files() if ".part" in name], [])

    def test_links_and_hard_links_in_the_run_are_not_followed(self):
        secret = self.data / "secret.txt"
        secret.write_text("TOP SECRET\n")
        (self.job / "2-build-0.jsonl").symlink_to(secret)
        os.link(secret, self.job / "3-build-0.jsonl")
        (self.run / "plan.log").unlink()
        (self.run / "plan.log").symlink_to(secret)
        self.archive_run()
        for path in self.archive.rglob("*"):
            if path.is_file():
                self.assertNotIn("TOP SECRET", gzip.decompress(path.read_bytes()).decode())
        self.assertNotIn("org__name/r1/plan.log.gz", self.files())
        self.assertNotIn("org__name/r1/fix-a/2-build-0.jsonl.gz", self.files())

    def test_a_job_directory_that_is_a_link_is_not_entered(self):
        elsewhere = self.data / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "1-build-0.jsonl").write_text("TOP SECRET\n")
        (self.run / "fix-b").symlink_to(elsewhere)
        self.archive_run()
        self.assertFalse([name for name in self.files() if "fix-b" in name])

    def test_a_large_file_keeps_its_tail_and_an_over_long_line_is_cut(self):
        body = "".join(f"line {index} {'x' * 100}\n" for index in range(2000))
        (self.run / "supervisor.log").write_text(f"{body}{'y' * 5000}\n")
        with mock.patch.object(logarchive, "MAX_FILE_BYTES", 50_000):
            self.archive_run()
        text = self.read("org__name/r1/supervisor.log.gz")
        self.assertTrue(text.startswith("[the first "))
        self.assertIn("line 1999", text)
        self.assertNotIn("line 0 ", text)
        (self.run / "plan.log").write_text(f"{'w ' * 200}\n")
        with mock.patch.object(logarchive, "MAX_LINE", 100):
            self.archive_run()
        self.assertIn("[line cut:", self.read("org__name/r1/plan.log.gz"))

    def test_a_run_stops_at_its_byte_budget(self):
        with mock.patch.object(logarchive, "MAX_RUN_BYTES", 10):
            result = self.archive_run()
        self.assertGreater(result.skipped, 0)

    def test_a_lone_start_marker_does_not_truncate_a_transcript_or_a_log(self):
        marker = "-----BEGIN PRIVATE KEY-----"
        events = [{"n": index, "content": "x"} for index in range(50)]
        events[1] = {"n": 1, "content": f"if text.startswith('{marker}'): flag()"}
        events[49] = {"type": "result", "total_cost_usd": 1.5}
        (self.job / "1-build-0.jsonl").write_text("".join(f"{json.dumps(event)}\n" for event in events))
        (self.run / "plan.log").write_text("".join(f"line {index}\n" if index != 1 else f"saw {marker}\n" for index in range(300)))
        self.archive_run()
        parsed = [json.loads(line) for line in self.read("org__name/r1/fix-a/1-build-0.jsonl.gz").splitlines()]
        self.assertEqual(len(parsed), 50)
        self.assertEqual(parsed[1]["content"], f"if text.startswith('{redact.MARK}")
        self.assertEqual(parsed[49], {"type": "result", "total_cost_usd": 1.5})
        plan = self.read("org__name/r1/plan.log.gz").splitlines()
        self.assertEqual(plan[1], f"saw {redact.MARK}")
        self.assertEqual(plan[2], f"[{redact.MAX_KEY_LINES} lines withheld] line {redact.MAX_KEY_LINES + 2}")
        self.assertEqual(plan[-1], "line 299")
        (self.run / "plan.log").write_text(f"start\n{marker}\none\ntwo\n")  # the file ends inside the block
        self.archive_run()
        self.assertEqual(self.read("org__name/r1/plan.log.gz"), f"start\n{redact.MARK}\n[2 lines withheld]\n")

    def test_a_hostile_line_of_key_markers_is_copied_in_seconds_not_minutes(self):
        hostile = "-----BEGIN PRIVATE KEY-----" * 37_000  # 999 KB: the quadratic pattern needed 200 s for this
        (self.run / "supervisor.log").write_text(f"before\n{hostile}\nafter\n")
        (self.job / "1-build-0.jsonl").write_text(f'{json.dumps({"text": hostile})}\n{{"type":"result"}}\n')
        started = time.monotonic()
        result = self.archive_run()
        self.assertLess(time.monotonic() - started, 20.0)
        self.assertEqual(self.read("org__name/r1/supervisor.log.gz"), f"before\n{redact.MARK}\n[1 line withheld]\n")
        self.assertEqual(self.read("org__name/r1/fix-a/1-build-0.jsonl.gz").splitlines()[1], '{"type":"result"}')
        self.assertGreater(result.files, 0)

    def test_a_copy_past_its_time_budget_fails_before_it_starts_a_file(self):
        with self.assertRaises(ArchiveError) as caught:
            logarchive.archive_run(self.run, "org/name", LocalBackend(self.archive), seconds=0.0)
        self.assertEqual(str(caught.exception), "the copy ran out of time")
        self.assertEqual(self.files(), [])

    def test_a_copy_that_runs_out_of_time_keeps_the_file_as_far_as_it_got_and_stops(self):
        for name in ("supervisor.log", "planner.jsonl", "fix-a"):
            target = self.run / name
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        (self.run / "plan.log").write_text("".join(f"line {index}\n" for index in range(50)))

        class Clock:
            def __init__(self, calls):
                self.left = calls

            def __call__(self):
                self.left -= 1
                return 0.0 if self.left >= 0 else 1000.0

        # one call for the start, one before the file, then one before each line: the fourth line is the first refused
        with self.assertRaises(ArchiveError):
            logarchive.archive_run(self.run, "org/name", LocalBackend(self.archive), seconds=10.0, clock=Clock(5))
        text = self.read("org__name/r1/plan.log.gz")
        self.assertEqual(text.splitlines()[:3], ["line 0", "line 1", "line 2"])
        self.assertEqual(text.splitlines()[3:], ["[the rest of this file is not kept: the copy ran out of time]"])
        self.assertEqual(self.files(), ["org__name/r1/plan.log.gz"])

    def test_only_a_run_of_a_managed_repository_is_archived(self):
        for repo, run in (("org", self.run), ("org/name", self.data / "runs" / "r1"), ("../x/y", self.run)):
            with self.subTest(repo=repo), self.assertRaises(ArchiveError):
                logarchive.archive_run(run, repo, LocalBackend(self.archive))

    def test_a_link_in_the_archive_path_is_refused(self):
        elsewhere = self.data / "elsewhere"
        elsewhere.mkdir()
        self.archive.mkdir()
        (self.archive / "org__name").symlink_to(elsewhere)
        with self.assertRaises(ArchiveError):
            self.archive_run()
        self.assertEqual(list(elsewhere.iterdir()), [])


class SweepTests(ArchiveCase):
    def test_old_copies_go_new_ones_stay_and_empty_directories_are_removed(self):
        self.archive_run()
        old = self.archive / "org__name" / "r1" / "fix-a" / "1-build-0.jsonl.gz"
        for path in (old, old.with_name("1-build-0.stderr.gz"), self.archive / "org__name" / "r1" / "plan.log.gz"):
            os.utime(path, (1_000_000, 1_000_000))
        keep = self.archive / "org__name" / "r1" / "supervisor.log.gz"
        removed = logarchive.sweep(LocalBackend(self.archive), 30, now=lambda: 1_000_000 + 31 * 86400.0 + 5)
        self.assertEqual(removed, 3)
        self.assertFalse(old.exists())
        self.assertFalse(old.parent.exists())  # the job directory went with its last copy
        self.assertTrue(keep.exists())

    def test_the_retention_is_counted_in_days_and_a_copy_inside_it_stays(self):
        self.archive_run()
        now = max(path.stat().st_mtime for path in self.archive.rglob("*") if path.is_file())
        self.assertEqual(logarchive.sweep(LocalBackend(self.archive), 30, now=lambda: now + 29 * 86400.0), 0)
        self.assertEqual(logarchive.sweep(LocalBackend(self.archive), 30, now=lambda: now + 31 * 86400.0), 5)
        self.assertEqual(self.files(), [])
        self.assertTrue(self.archive.is_dir())

    def test_a_link_is_never_followed_or_removed_by_the_sweep(self):
        self.archive_run()
        outside = self.data / "outside.txt"
        outside.write_text("keep me")
        os.utime(outside, (1, 1))
        (self.archive / "org__name" / "link.gz").symlink_to(outside)
        logarchive.sweep(LocalBackend(self.archive), 1, now=lambda: 10 * 86400.0 * 365 * 100)
        self.assertEqual(outside.read_text(), "keep me")

    def test_a_missing_archive_is_nothing_to_sweep(self):
        self.assertEqual(LocalBackend(self.data / "nowhere").sweep(10**12), 0)


class FakeBlobs:
    """The slice of the Blob REST API the backend uses, on the injected transport."""

    def __init__(self):
        self.blobs = {}  # name -> (bytes, last modified seconds)
        self.requests = []
        self.fail = {}  # method -> status
        self.page = 1000
        self.body = None  # replaces the list answer

    def __call__(self, method, url, headers, source):
        from urllib.parse import parse_qs, unquote, urlsplit

        parts = urlsplit(url)
        data = source.read_bytes() if source is not None else b""
        self.requests.append((method, url, dict(headers), data))
        if method in self.fail:
            return Reply(self.fail[method], {"x-ms-error-code": "Nope"}, b"")
        name = unquote(parts.path.removeprefix("/logs-archive/"))
        if method == "PUT":
            self.blobs[name] = (data, 1_800_000_000.0)
            return Reply(201, {}, b"")
        if method == "DELETE":
            return Reply(202, {}, b"") if self.blobs.pop(name, None) is not None else Reply(404, {}, b"")
        query = parse_qs(parts.query)
        if self.body is not None:
            return Reply(200, {}, self.body)
        names = [item for item in sorted(self.blobs) if item > query.get("marker", [""])[0]]
        chunk = names[: self.page]
        following = chunk[-1] if len(names) > self.page else ""
        items = "".join(
            f"<Blob><Name>{item}</Name><Properties><Last-Modified>{format_datetime(datetime.fromtimestamp(self.blobs[item][1], timezone.utc), usegmt=True)}</Last-Modified></Properties></Blob>"
            for item in chunk
        )
        xml = f"<?xml version=\"1.0\"?><EnumerationResults><Blobs>{items}</Blobs><NextMarker>{following}</NextMarker></EnumerationResults>"
        return Reply(200, {}, xml.encode())


class BlobArchiveTests(ArchiveCase):
    URL = "https://acct.blob.core.windows.net"

    def setUp(self):
        super().setUp()
        self.fake = FakeBlobs()
        self.backend = BlobBackend(self.URL, "logs-archive", lambda: "tok-123", self.fake)

    def test_a_run_is_uploaded_with_the_identity_token_and_redacted(self):
        result = self.archive_run(self.backend)
        self.assertEqual(result.files, 5)
        self.assertEqual(
            sorted(self.fake.blobs),
            [
                "org__name/r1/fix-a/1-build-0.jsonl.gz",
                "org__name/r1/fix-a/1-build-0.stderr.gz",
                "org__name/r1/plan.log.gz",
                "org__name/r1/planner.jsonl.gz",
                "org__name/r1/supervisor.log.gz",
            ],
        )
        method, url, headers, body = next(item for item in self.fake.requests if item[1].endswith("plan.log.gz"))
        self.assertEqual((method, url), ("PUT", f"{self.URL}/logs-archive/org__name/r1/plan.log.gz"))
        self.assertEqual(headers["Authorization"], "Bearer tok-123")
        self.assertEqual(headers["x-ms-blob-type"], "BlockBlob")
        self.assertEqual(headers["x-ms-version"], logarchive.API_VERSION)
        self.assertEqual(gzip.decompress(body).decode(), "planning [REDACTED]\n")
        for data, _ in self.fake.blobs.values():
            for secret in SECRETS:
                self.assertNotIn(secret, gzip.decompress(data).decode())

    def test_a_failed_upload_raises_a_fixed_message_without_a_token_or_a_url(self):
        self.fake.fail["PUT"] = 403
        with self.assertRaises(ArchiveError) as caught:
            self.archive_run(self.backend)
        self.assertEqual(str(caught.exception), "blob upload failed (HTTP 403)")
        self.assertNotIn("tok-123", str(caught.exception))

    def test_a_token_that_cannot_be_had_is_one_fixed_message(self):
        def broken():
            raise RuntimeError("secret detail tok-999")

        for provider in (broken, lambda: "has space", lambda: "", lambda: None):
            backend = BlobBackend(self.URL, "logs-archive", provider, self.fake)
            with self.subTest(provider=provider), self.assertRaises(ArchiveError) as caught:
                self.archive_run(backend)
            self.assertNotIn("tok-999", str(caught.exception))
            self.assertNotIn("secret detail", str(caught.exception))
        self.assertEqual(self.fake.requests, [])

    def test_a_blob_url_must_be_https_and_bare(self):
        for url in ("http://acct.example", "https://u:p@acct.example", "https://acct.example?x=1", "ftp://acct.example"):
            with self.subTest(url), self.assertRaises(ArchiveConfigError):
                BlobBackend(url, "logs-archive", lambda: "t", self.fake)

    def test_the_sweep_lists_in_pages_and_deletes_only_the_old(self):
        self.fake.page = 2
        for index, age in enumerate((40, 31, 5, 45, 1)):
            self.fake.blobs[f"org__name/r{index}/plan.log.gz"] = (b"x", 1_800_000_000.0 - age * 86400.0)
        removed = logarchive.sweep(self.backend, 30, now=lambda: 1_800_000_000.0)
        self.assertEqual(removed, 3)
        self.assertEqual(sorted(self.fake.blobs), ["org__name/r2/plan.log.gz", "org__name/r4/plan.log.gz"])
        lists = [item for item in self.fake.requests if item[0] == "GET"]
        self.assertEqual(len(lists), 3)
        self.assertIn("marker=org__name%2Fr1%2Fplan.log.gz", lists[1][1])
        self.assertTrue(all(item[2]["Authorization"] == "Bearer tok-123" for item in self.fake.requests))

    def test_a_sweep_failure_and_a_hostile_list_answer_are_refused(self):
        self.fake.fail["GET"] = 500
        with self.assertRaises(ArchiveError):
            logarchive.sweep(self.backend, 30)
        self.fake.fail.clear()
        for body in (b"<!DOCTYPE x [<!ENTITY a 'b'>]><EnumerationResults/>", b"not xml", b"<EnumerationResults><Blobs><Blob><Name>a</Name><Properties><Last-Modified>nonsense</Last-Modified></Properties></Blob></Blobs></EnumerationResults>"):
            self.fake.body = body
            with self.subTest(body=body[:20]), self.assertRaises(ArchiveError):
                logarchive.sweep(self.backend, 30)

    def test_a_delete_that_is_refused_stops_the_sweep(self):
        self.fake.blobs["org__name/r0/plan.log.gz"] = (b"x", 1.0)
        self.fake.fail["DELETE"] = 403
        with self.assertRaises(ArchiveError):
            logarchive.sweep(self.backend, 30, now=lambda: 10**10)

    def test_the_backend_choice_follows_the_settings(self):
        local = logarchive.backend_for(Config(), self.data, {})
        self.assertIsInstance(local, LocalBackend)
        self.assertEqual(local.root, self.data / "logs-archive")
        with mock.patch("factory.locks.token_source_from_env", return_value=lambda: "t") as source:
            blob = logarchive.backend_for(Config(True, self.URL, "logs-archive", 30), self.data, {"X": "1"}, self.fake)
        self.assertIsInstance(blob, BlobBackend)
        source.assert_called_once_with({"X": "1"})


class PurgeHookTests(ArchiveCase):
    def purge(self, hook):
        shutil.rmtree(self.run / "worktrees")  # a run whose clone step failed: purged after the liveness check alone
        return purge_run(self.data / "no-clone", self.run, before_remove=hook)

    def test_the_hook_runs_after_the_checks_and_before_the_directory_goes(self):
        seen = []

        def hook(run_dir):
            seen.append((run_dir, sorted(path.name for path in run_dir.iterdir())))

        summary = self.purge(hook)
        self.assertTrue(summary["removed_run_dir"])
        self.assertFalse(self.run.exists())
        self.assertEqual(seen[0][0], self.run)
        self.assertIn("plan.log", seen[0][1])

    def test_a_failing_hook_never_fails_or_stops_the_purge(self):
        def hook(run_dir):
            raise RuntimeError("archive exploded")

        self.assertTrue(self.purge(hook)["removed_run_dir"])
        self.assertFalse(self.run.exists())

    def test_a_copy_that_runs_out_of_time_still_lets_the_purge_go(self):
        class Config:
            repo = "org/name"

        err = io.StringIO()
        with mock.patch.object(logarchive, "MAX_SECONDS", 0.0), contextlib.redirect_stderr(err):
            summary = self.purge(cli.archive_before_purge(Config()))
        self.assertTrue(summary["removed_run_dir"])
        self.assertFalse(self.run.exists())
        self.assertIn(logarchive.FAILED_MARKER, err.getvalue())

    def test_the_hook_is_not_called_when_the_purge_is_refused(self):
        from factory.purge import PurgeRefused

        (self.run / "state.json").write_text(json.dumps({"jobs": {"fix-a": {"pid": os.getpid(), "pid_host": None}}}))
        called = []
        with mock.patch("factory.purge.pid_status", return_value="alive"), self.assertRaises(PurgeRefused):
            self.purge(called.append)
        self.assertEqual(called, [])
        self.assertTrue((self.run / "plan.log").exists())


class FullPurgeHookTests(PurgeCase):
    """The hook on the path with a clone and linked worktrees: it sees the transcript after the checks and before the run goes."""

    def test_the_transcript_is_still_there_when_the_hook_runs_and_gone_after(self):
        directory, _ = self.make_run("r1")
        seen = []

        def hook(run_dir):
            seen.append((run_dir.name, (run_dir / "job" / "0-build-0.jsonl").read_text()))

        summary = purge_run(self.repo, directory, before_remove=hook)
        self.assertEqual(seen, [("r1", "transcript\n")])
        self.assertTrue(summary["removed_run_dir"])
        self.assertFalse(directory.exists())

    def test_a_purge_the_checks_refuse_does_not_call_the_hook(self):
        from factory.purge import PurgeRefused

        directory, state = self.make_run("r1")
        state.update("job", pid=os.getpid(), pid_host=None)
        called = []
        with mock.patch("factory.purge.pid_status", return_value="alive"), self.assertRaises(PurgeRefused):
            purge_run(self.repo, directory, before_remove=called.append)
        self.assertEqual(called, [])
        self.assertTrue(directory.exists())


class CliHookTests(ArchiveCase):
    class Config:
        repo = "org/name"

    def run_hook(self, env, cfg=None):
        out, err = io.StringIO(), io.StringIO()
        hook = cli.archive_before_purge(cfg or self.Config())
        with mock.patch.dict(os.environ, env, clear=False), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            hook(self.run.resolve())
        return out.getvalue(), err.getvalue()

    def test_the_default_copies_to_the_data_volume_and_prints_nothing(self):
        out, err = self.run_hook({})
        self.assertEqual((out, err), ("", ""))
        self.assertIn("org__name/r1/plan.log.gz", self.files())

    def test_it_can_be_turned_off(self):
        out, err = self.run_hook({"FACTORY_LOGS_ARCHIVE": "off"})
        self.assertEqual((out, err), ("", ""))
        self.assertFalse(self.archive.exists())

    def test_a_failure_is_one_fixed_line_without_the_error_text(self):
        class Broken:
            def put(self, key, source):
                raise ArchiveError("detail that must not be repeated tok-123")

        with mock.patch.object(logarchive, "backend_for", return_value=Broken()):
            out, err = self.run_hook({})
        self.assertEqual(out, "")
        self.assertEqual(err, f"{logarchive.FAILED_MARKER} (ArchiveError); the logs go with the run\n")
        self.assertNotIn("tok-123", err)

    def test_a_copy_that_runs_out_of_time_is_the_same_one_line(self):
        with mock.patch.object(logarchive, "MAX_SECONDS", 0.0):
            out, err = self.run_hook({})
        self.assertEqual(out, "")
        self.assertEqual(err, f"{logarchive.FAILED_MARKER} (ArchiveError); the logs go with the run\n")

    def test_an_invalid_setting_is_the_same_one_line(self):
        out, err = self.run_hook({"FACTORY_LOGS_ARCHIVE_RETENTION_DAYS": "x"})
        self.assertEqual(err, f"{logarchive.FAILED_MARKER} (ArchiveConfigError); the logs go with the run\n")

    def test_local_mode_has_no_hook(self):
        class Local:
            repo = ""

        self.assertIsNone(cli.archive_before_purge(None))
        self.assertIsNone(cli.archive_before_purge(Local()))


class JanitorTests(ops.OpsCase):
    def archive_dir(self):
        return self.data_dir / "logs-archive"

    def old_copy(self, age_days):
        path = self.archive_dir() / "org__name" / "r0" / "plan.log.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        stamp = ops.NOW - age_days * 86400.0
        os.utime(path, (stamp, stamp))
        return path

    def test_the_sweep_removes_expired_copies_at_most_once_a_day(self):
        old = self.old_copy(31)
        self.ops.sweep()
        self.assertFalse(old.exists())
        self.assertIn("log-archive swept removed=1 retention_days=30", self.lines())
        count = len(self.lines())
        again = self.old_copy(40)
        self.ops.sweep()
        self.assertTrue(again.exists())  # not yet: the next sweep is a day away
        self.assertEqual(len(self.lines()), count)
        self.time.now += 86400.0 + 1
        self.ops.sweep()
        self.assertFalse(again.exists())

    def test_the_retention_comes_from_the_environment(self):
        self.manager.env["FACTORY_LOGS_ARCHIVE_RETENTION_DAYS"] = "3"
        recent, expired = self.old_copy(2), self.archive_dir() / "org__name" / "r0" / "supervisor.log.gz"
        expired.write_bytes(b"x")
        os.utime(expired, (ops.NOW - 4 * 86400.0, ops.NOW - 4 * 86400.0))
        self.ops.sweep()
        self.assertTrue(recent.exists())
        self.assertFalse(expired.exists())

    def test_off_means_no_sweep(self):
        self.manager.env["FACTORY_LOGS_ARCHIVE"] = "off"
        old = self.old_copy(400)
        self.ops.sweep()
        self.assertTrue(old.exists())

    def test_a_failed_sweep_is_one_fixed_line_and_a_count_and_is_tried_again_the_next_day(self):
        class Broken:
            def sweep(self, cutoff):
                raise ArchiveError("detail tok-123")

        with mock.patch.object(logarchive, "backend_for", return_value=Broken()):
            self.ops.sweep()
        self.assertIn("log-archive sweep failed error=ArchiveError", self.lines())
        self.assertNotIn("tok-123", "".join([self.out.getvalue(), self.err.getvalue()]))
        self.assertEqual(self.manager.metrics["factory_log_archive_failures_total"].value(), 1)

    def test_a_purge_whose_copy_failed_is_counted_and_is_still_a_purge(self):
        self.make_run()
        self.runner.reports[("org/name", "r1")] = ops.report(("a", "merged"))

        def purge(repo, run, tail):
            self.runner.default_purge(repo, run, tail)
            return CommandResult(0, b"Purged\n", f"{logarchive.FAILED_MARKER} (ArchiveError); the logs go with the run\n".encode())

        self.runner.override[("purge",)] = purge
        self.ops.sweep()
        self.assertFalse(self.run_dir().exists())
        self.assertIn("purge repo=org/name run=r1 reason=terminal", self.lines())
        self.assertIn("log-archive failed repo=org/name run=r1 the logs went with the run", self.lines())
        self.assertEqual(self.manager.metrics["factory_log_archive_failures_total"].value(), 1)

    def test_a_purge_that_copied_is_not_counted(self):
        self.make_run()
        self.runner.reports[("org/name", "r1")] = ops.report(("a", "merged"))
        self.ops.sweep()
        self.assertEqual(self.manager.metrics["factory_log_archive_failures_total"].value(), 0)


if __name__ == "__main__":
    unittest.main()

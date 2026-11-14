"""Graceful drain preserves real worker checkpoints without releasing ownership early."""

import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from factory import control
from factory.control import RunControl, request_drain, supervisor_status
from factory.state import State, atomic_json, lock
from tests import test_factory
from tests.test_factory import assignment


class ControlTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.common = self.directory / "git"

    def test_live_ownership_identity_and_idempotent_request(self):
        with RunControl(self.directory, self.common) as owner:
            with self.assertRaisesRegex(ValueError, "another repository"):
                request_drain(self.directory, self.directory / "other-git")
            with self.assertRaisesRegex(RuntimeError, "another supervisor"):
                with RunControl(self.directory, self.common):
                    self.fail("second control owner entered")
            first = request_drain(self.directory, self.common)
            second = request_drain(self.directory, self.common)
            self.assertEqual(first, second)
            self.assertEqual(first["token"], owner.token)
            self.assertEqual(first["pid"], os.getpid())
            self.assertTrue(owner.draining())
            self.assertTrue(supervisor_status(self.directory)["active"])
        self.assertEqual(supervisor_status(self.directory)["status"], "drained")
        self.assertFalse(supervisor_status(self.directory)["active"])

    def test_explicit_restart_archives_drain_intent_and_rejects_stale_request(self):
        with RunControl(self.directory, self.common) as previous:
            request_drain(self.directory, self.common)
        prior_document = control._read(self.directory)
        with RunControl(self.directory, self.common) as restarted:
            self.assertFalse(restarted.draining())
            self.assertNotEqual(previous.token, restarted.token)
            document = control._read(self.directory)
            self.assertEqual(document["history"][-1]["status"], "drained")
            self.assertIsNotNone(document["history"][-1]["drain_requested_at"])
            self.assertIsNone(document["current"]["drain_requested_at"])
            # A caller read the old invocation, then waited while explicit run restarted.
            with patch("factory.control._read", side_effect=[prior_document, document]):
                with self.assertRaisesRegex(ValueError, "invocation changed"):
                    request_drain(self.directory, self.common)
            self.assertFalse(restarted.draining())

    def test_stale_pid_and_old_supervisor_cannot_be_targeted(self):
        before = list(self.directory.iterdir())
        with self.assertRaisesRegex(ValueError, "no drain-capable"):
            request_drain(self.directory, self.common)
        self.assertEqual(list(self.directory.iterdir()), before)
        with RunControl(self.directory, self.common):
            pass
        document = control._read(self.directory)
        document["current"]["status"] = "running"
        # PID is still live (this test), but its invocation ownership lock is gone.
        atomic_json(self.directory / "supervisor-control.json", document)
        with self.assertRaisesRegex(ValueError, "not active"):
            request_drain(self.directory, self.common)
        self.assertEqual(supervisor_status(self.directory)["status"], "stale")

    def test_operator_supervisor_record_is_ignored_and_preserved(self):
        legacy = self.directory / "supervisor.json"
        contents = (
            b'{"pid": 123, "command": "operator-owned", "log": "supervisor.log"}\n'
        )
        legacy.write_bytes(contents)
        self.assertIsNone(supervisor_status(self.directory))
        with self.assertRaisesRegex(ValueError, "no drain-capable"):
            request_drain(self.directory, self.common)
        with RunControl(self.directory, self.common) as owner:
            request_drain(self.directory, self.common)
            self.assertTrue(owner.draining())
        self.assertEqual(legacy.read_bytes(), contents)

    def test_drain_and_phase_admission_are_serialized(self):
        admitted = threading.Event()
        release = threading.Event()
        requested = threading.Event()
        failures = []
        with RunControl(self.directory, self.common) as owner:

            def submit():
                with owner.admission() as allowed:
                    self.assertTrue(allowed)
                    admitted.set()
                    release.wait(5)

            def drain():
                try:
                    request_drain(self.directory, self.common)
                    requested.set()
                except Exception as error:
                    failures.append(error)

            submitter = threading.Thread(target=submit)
            submitter.start()
            self.assertTrue(admitted.wait(5))
            requester = threading.Thread(target=drain)
            requester.start()
            try:
                self.assertFalse(requested.wait(0.05))
            finally:
                release.set()
                submitter.join(5)
                requester.join(5)
            self.assertFalse(failures)
            self.assertTrue(requested.is_set())
            with owner.admission() as allowed:
                self.assertFalse(allowed)

    def test_interrupt_remains_distinct_from_drained_completion(self):
        with self.assertRaises(KeyboardInterrupt):
            with RunControl(self.directory, self.common):
                request_drain(self.directory, self.common)
                raise KeyboardInterrupt
        self.assertEqual(supervisor_status(self.directory)["status"], "interrupted")


class DrainIntegrationTests(unittest.TestCase):
    setUp = test_factory.IntegrationTests.setUp
    git = test_factory.IntegrationTests.git
    script = test_factory.IntegrationTests.script
    install_agent = test_factory.IntegrationTests.install_agent

    def command(self, *args, timeout=10):
        return subprocess.run(
            [sys.executable, "-m", "factory", "--run-dir", str(self.run_dir), *args],
            cwd=self.repo,
            env=self.cli_env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def wait_file(self, path, process):
        deadline = time.monotonic() + 10
        while not path.exists():
            if process.poll() is not None:
                self.fail(
                    f"supervisor exited before worker start: {process.communicate()}"
                )
            if time.monotonic() >= deadline:
                self.fail("worker did not start")
            time.sleep(0.02)

    def exercise_drain(self, held_phase):
        plan = assignment()
        other = copy.deepcopy(plan["jobs"][0])
        # Disjoint from fix-a: overlapping jobs would wait for fix-a's merge, not run on resume.
        plan["jobs"][0]["likely_paths"] = ["src/a"]
        other.update(id="fix-b", issues=[2], likely_paths=["src/b"])
        plan["jobs"].append(other)
        plan["issues"].append({"number": 2, "title": "B", "body": "B"})
        plan["dispositions"].append(
            {"issue": 2, "status": "job", "job": "fix-b", "reason": "Fix"}
        )
        (self.run_dir / "plan.json").write_text(json.dumps(plan))
        legacy = self.run_dir / "supervisor.json"
        legacy_contents = b'{"pid": 123, "started_at": "operator metadata", "command": "old supervisor"}\n'
        legacy.write_bytes(legacy_contents)
        self.cli_env = dict(os.environ)
        State(self.run_dir, plan)
        old_status = self.command("status", "--json")
        self.assertEqual(old_status.returncode, 0, old_status.stderr)
        self.assertIsNone(json.loads(old_status.stdout)["supervisor"])
        old_drain = self.command("drain")
        self.assertEqual(old_drain.returncode, 1)
        self.assertIn("no drain-capable supervisor", old_drain.stderr)
        self.assertEqual(legacy.read_bytes(), legacy_contents)
        self.install_agent(
            """
import time
prompt = sys.stdin.read()
root = pathlib.Path(__file__).parent
phase = 'review' if 'independent reviewer' in prompt else 'build'
with (root / 'phases').open('a') as log:
    log.write(phase + '\\n')
if phase == (root / 'held-phase').read_text():
    (root / 'started').touch()
    deadline = time.monotonic() + 20
    while not (root / 'release').exists() and time.monotonic() < deadline:
        time.sleep(0.01)
if phase == 'build':
    pathlib.Path('src/a.txt').write_text('fixed\\n')
    result = {'summary': 'Fix A', 'blocked': False, 'blocker': '', 'tests': ['synthetic check passed'], 'feature': False}
else:
    result = {'pass': True, 'findings': []}
emit(result)
"""
        )
        (self.binary / "held-phase").write_text(held_phase)
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "factory",
                "--run-dir",
                str(self.run_dir),
                "run",
                "--concurrency",
                "1",
                "--max-active-agents",
                "1",
                "--timeout",
                "25",
            ],
            cwd=self.repo,
            env=self.cli_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            self.wait_file(self.binary / "started", process)
            status = self.command("status", "--json")
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertTrue(json.loads(status.stdout)["supervisor"]["active"])
            self.assertEqual(legacy.read_bytes(), legacy_contents)
            common = Path(
                self.git("rev-parse", "--path-format=absolute", "--git-common-dir")
            )
            with self.assertRaises(RuntimeError), lock(common / "factory.lock"):
                self.fail("active supervisor did not hold repository lock")
            result = self.command("drain")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("does not mark the work complete", result.stdout)
            self.assertIsNone(process.poll(), "drain killed a worker before release")
            (self.binary / "release").touch()
            output, _ = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, output)
            self.assertIn("Drain does not mark work complete", output)
            data = json.loads((self.run_dir / "state.json").read_text())
            first, second = data["jobs"]["fix-a"], data["jobs"]["fix-b"]
            self.assertEqual(
                first["status"],
                "waiting_review" if held_phase == "build" else "waiting_publish",
            )
            self.assertEqual(first["attempt"], 1)
            self.assertIsNone(first.get("pid"))
            self.assertTrue(first["commit"])
            self.assertEqual(second["status"], "queued")
            self.assertEqual(second["attempt"], 0)
            phases = (self.binary / "phases").read_text().splitlines()
            self.assertEqual(
                phases, ["build"] if held_phase == "build" else ["build", "review"]
            )
            self.assertEqual(supervisor_status(self.run_dir)["status"], "drained")
            with lock(common / "factory.lock"):
                pass
            # Explicit restart resumes saved review/publication, never repeats first build.
            restarted = self.command(
                "run", "--concurrency", "1", "--max-active-agents", "1", timeout=15
            )
            self.assertEqual(
                restarted.returncode, 0, restarted.stdout + restarted.stderr
            )
            phases = (self.binary / "phases").read_text().splitlines()
            self.assertEqual(phases.count("build"), 2)
            self.assertEqual(phases.count("review"), 2)
            record = control._read(self.run_dir)
            self.assertEqual(record["history"][-1]["status"], "drained")
            self.assertIsNone(record["current"]["drain_requested_at"])
            self.assertEqual(legacy.read_bytes(), legacy_contents)
        finally:
            (self.binary / "release").touch()
            if process.poll() is None:
                process.communicate(timeout=30)

    def test_drain_build_preserves_commit_without_starting_review_or_next_job(self):
        self.exercise_drain("build")

    def test_drain_review_preserves_review_without_starting_publication_or_next_job(
        self,
    ):
        self.exercise_drain("review")


if __name__ == "__main__":
    unittest.main()

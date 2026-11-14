"""Stopping a single job (docs/manager.md, "Stopping a job"): the request, the supervisor that carries it out, the CLI.

The supervisor tests run a real ``Runner`` with real worker processes (the fake agent of tests/agentstub.py in its
own process group), so a worker that outlasts the grace period is really signalled and one that finishes in time is
really left alone. Nothing here sleeps for a fixed time: the grace period and the SIGTERM wait are set to a few
tenths of a second on the runner, and the tests poll files the workers write.
"""

import copy
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from factory import control, history
from factory import runner as runner_module
from factory.control import RunControl, clean_text, request_stop, stop_grace_seconds
from factory.issue_labels import LABEL_BLOCKED, label_for_job
from factory.runner import PendingStop, Runner
from factory.state import State
from factory.status import status_report
from tests import test_factory
from tests.test_factory import assignment

REASON = "CANARY-REASON waiting for the migration"

# The worker of the supervisor tests. Only the build session of fix-a holds (until ``release``), in the mode the
# test wrote to ``mode``: ``hang`` ends on SIGTERM like any process, ``ignore-term`` notes the SIGTERM and carries
# on (and leaves a background child in its process group), ``blocked`` reports that it is blocked once released.
WORKER = r"""
import signal, subprocess, time
root = pathlib.Path(__file__).parent
prompt = sys.stdin.read()
phase = 'review' if 'independent reviewer' in prompt else 'build'
job = 'fix-a' if 'fix-a' in str(pathlib.Path.cwd()) else 'fix-b'
with (root / 'phases').open('a') as log:
    log.write(f'{job}:{phase}\n')
mode = (root / 'mode').read_text() if (root / 'mode').exists() else 'fast'
holding = phase == 'build' and job == 'fix-a' and mode != 'fast'
if holding:
    (root / 'pid').write_text(str(os.getpid()))
    if mode == 'ignore-term':
        def noted(signum, frame):
            (root / 'termed').touch()
        signal.signal(signal.SIGTERM, noted)
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
        (root / 'child').write_text(str(child.pid))
    (root / 'started').touch()
    deadline = time.monotonic() + 60
    while not (root / 'release').exists() and time.monotonic() < deadline:
        time.sleep(0.01)
if phase == 'build':
    if holding and mode == 'blocked':
        emit({'summary': 'cannot', 'blocked': True, 'blocker': 'cannot go on', 'tests': [], 'feature': False})
        sys.exit(0)
    name = 'a' if job == 'fix-a' else 'b'
    pathlib.Path(f'src/{name}.txt').write_text('fixed\n')
    result = {'summary': 'Fix', 'blocked': False, 'blocker': '', 'tests': ['synthetic check passed'], 'feature': False}
else:
    result = {'pass': True, 'findings': []}
emit(result)
"""


def two_jobs():
    plan = assignment()
    other = copy.deepcopy(plan["jobs"][0])
    plan["jobs"][0]["likely_paths"] = ["src/a"]
    other.update(id="fix-b", issues=[2], likely_paths=["src/b"])
    plan["jobs"].append(other)
    plan["issues"].append({"number": 2, "title": "B", "body": "B"})
    plan["dispositions"].append({"issue": 2, "status": "job", "job": "fix-b", "reason": "Fix"})
    return plan


def gone(pid):
    """Whether a process no longer runs (a zombie nobody reaped yet does not run either)."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return True
    return text.rsplit(")", 1)[1].split()[0] == "Z"


class TextTests(unittest.TestCase):
    def test_the_reason_is_data_without_control_characters_and_bounded(self):
        hostile = "line one\nline two\x1b[31m red\x07 \u202eevil\u2028tail\x00 end"
        text = clean_text(hostile, 200)
        self.assertTrue(text.isprintable(), text)
        self.assertNotIn("\n", text)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\u202e", text)
        self.assertEqual(clean_text("  a \t\n b  ", 200), "a b")
        self.assertEqual(len(clean_text("x" * 500, 200)), 200)
        self.assertIsNone(clean_text("  \n\t ", 200))
        self.assertIsNone(clean_text(None, 200))
        self.assertIsNone(clean_text(b"bytes", 200))

    def test_the_grace_period_comes_from_the_environment_within_its_bounds(self):
        self.assertEqual(stop_grace_seconds({}), control.DEFAULT_STOP_GRACE)
        self.assertEqual(stop_grace_seconds({"FACTORY_STOP_GRACE_SECONDS": "30"}), 30.0)
        self.assertEqual(stop_grace_seconds({"FACTORY_STOP_GRACE_SECONDS": " 1 "}), 1.0)
        for bad in ("0", "0.5", "3601", "-5", "soon", "nan", "inf", ""):
            with self.subTest(bad):
                self.assertEqual(stop_grace_seconds({"FACTORY_STOP_GRACE_SECONDS": bad}), control.DEFAULT_STOP_GRACE)


class RequestTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.common = self.directory / "git"

    def test_a_request_is_recorded_for_the_live_supervisor_and_asking_again_gets_the_first_answer(self):
        with RunControl(self.directory, self.common) as owner:
            first = request_stop(self.directory, self.common, "fix-a", "alice", REASON)
            second = request_stop(self.directory, self.common, "fix-a", "mallory", "another reason")
            self.assertTrue(first["created"])
            self.assertFalse(second["created"])
            self.assertEqual({k: v for k, v in first.items() if k != "created"}, {k: v for k, v in second.items() if k != "created"})
            self.assertEqual((first["by"], first["reason"]), ("alice", REASON))
            # Taken by the supervisor, and only once.
            self.assertEqual([job for job, _ in owner.stop_requests()], ["fix-a"])
            owner.record_stop("fix-a", "stopping")
            self.assertEqual(owner.stop_requests(), [])
            record = control._read(self.directory)["current"]["stop_requests"]["fix-a"]
            self.assertEqual(record["result"], "stopping")
            self.assertTrue(record["seen_at"])
            # A request of another job is a request of its own, and a drain request is not one.
            request_stop(self.directory, self.common, "fix-b")
            self.assertEqual([job for job, _ in owner.stop_requests()], ["fix-b"])
            self.assertFalse(owner.draining())

    def test_a_request_is_data_only_nothing_is_signalled_and_no_job_state_is_edited(self):
        state_file = self.directory / "state.json"
        state_file.write_text('{"jobs": {}}\n')
        before = state_file.read_bytes()
        with RunControl(self.directory, self.common), patch("os.kill") as kill, patch("os.killpg") as killpg:
            request_stop(self.directory, self.common, "fix-a", "alice")
        # Probing a pid (signal 0) is how the owner's liveness is checked; delivering a signal never happens.
        self.assertEqual([call for call in kill.call_args_list if call.args[1] != 0], [])
        self.assertEqual(killpg.call_count, 0)
        self.assertEqual(state_file.read_bytes(), before)

    def test_the_text_is_sanitised_and_bounded_before_it_is_stored(self):
        with RunControl(self.directory, self.common):
            request = request_stop(self.directory, self.common, "fix-a", "al\nice\x1b[0m", f"a\nb\x1b[31m{'x' * 500}")
        self.assertNotIn("\n", request["by"])
        self.assertNotIn("\x1b", request["by"])
        self.assertTrue(request["reason"].isprintable())
        self.assertEqual(len(request["reason"]), control.MAX_STOP_REASON)

    def test_a_stale_foreign_or_missing_supervisor_cannot_be_asked(self):
        with self.assertRaisesRegex(ValueError, "no drain-capable"):
            request_stop(self.directory, self.common, "fix-a")
        with RunControl(self.directory, self.common):
            with self.assertRaisesRegex(ValueError, "another repository"):
                request_stop(self.directory, self.directory / "other-git", "fix-a")
            for bad in ("", "../x", "a b", "x" * 65, None, 7):
                with self.subTest(bad), self.assertRaisesRegex(ValueError, "invalid job id"):
                    request_stop(self.directory, self.common, bad)
        with self.assertRaisesRegex(ValueError, "not active"):
            document = control._read(self.directory)
            document["current"]["status"] = "running"
            control.atomic_json(self.directory / "supervisor-control.json", document)
            request_stop(self.directory, self.common, "fix-a")

    def test_a_restart_does_not_inherit_the_requests_of_the_earlier_invocation(self):
        with RunControl(self.directory, self.common):
            request_stop(self.directory, self.common, "fix-a", "alice")
        with RunControl(self.directory, self.common) as restarted:
            self.assertEqual(restarted.stop_requests(), [])
            self.assertIn("fix-a", control._read(self.directory)["history"][-1]["stop_requests"])


class SupervisorCase(unittest.TestCase):
    """A real Runner in a thread, real worker processes, and the control record the CLI would write."""

    setUp = test_factory.IntegrationTests.setUp
    git = test_factory.IntegrationTests.git
    script = test_factory.IntegrationTests.script
    install_agent = test_factory.IntegrationTests.install_agent
    gh_calls = test_factory.IntegrationTests.gh_calls

    def prepare(self, mode, plan=None, concurrency=1, grace=10.0):
        self.install_agent(WORKER)
        (self.binary / "mode").write_text(mode)
        self.common = self.git("rev-parse", "--path-format=absolute", "--git-common-dir")
        self.runner = Runner(self.repo, self.run_dir, plan or assignment(), concurrency=concurrency, timeout=60)
        # A long grace period for the tests that release the worker themselves, a short one for the others.
        self.runner.stop_grace = grace
        self.runner.stop_term_wait = 0.3
        self.labels, self.ledger_dir = [], Path(self.root) / "data"
        self.ledger_dir.mkdir()
        self.runner.state.observer = lambda key, previous, current: self.labels.append((key, current.get("status")))
        self.ledger = history.Ledger(self.ledger_dir)
        recorder = history.Recorder(self.ledger, "org/name", "r1", self.runner.plan)
        self.runner.state.ledger = recorder.on_job
        self.errors = []

        def work():
            try:
                self.runner.run()
            except BaseException as error:  # noqa: BLE001 (reported by the test thread)
                self.errors.append(error)

        self.thread = threading.Thread(target=work)
        self.thread.start()
        self.addCleanup(self.finish)

    def finish(self):
        (self.binary / "release").touch()
        self.thread.join(60)

    def wait_until(self, check, what, seconds=30):
        deadline = time.monotonic() + seconds
        while not check():
            if time.monotonic() > deadline:
                self.fail(f"timed out waiting for {what}")
            time.sleep(0.02)

    def ask(self, job="fix-a", principal="alice", reason=REASON):
        return request_stop(self.run_dir, self.common, job, principal, reason)

    def result_of(self, job="fix-a"):
        document = control._read(self.run_dir)["current"].get("stop_requests", {})
        return document.get(job, {}).get("result")

    def join(self):
        self.thread.join(60)
        self.assertFalse(self.thread.is_alive(), "the supervisor did not finish")
        self.assertEqual(self.errors, [])

    def job(self, key="fix-a"):
        return json.loads((self.run_dir / "state.json").read_text())["jobs"][key]

    def phases(self):
        return (self.binary / "phases").read_text().split()


class SupervisorStopTests(SupervisorCase):
    def test_a_step_that_finishes_within_the_grace_period_is_not_killed_and_no_new_session_starts(self):
        self.prepare("hang")
        self.wait_until((self.binary / "started").exists, "the worker")
        self.ask()
        self.wait_until(lambda: self.result_of() == "stopping", "the supervisor to take the request")
        self.assertIsNone(self.runner.state.data["jobs"]["fix-a"].get("error"))
        self.assertEqual(self.job()["status"], "running")
        self.assertEqual(self.job()["stop"]["state"], "stopping")
        (self.binary / "release").touch()  # the step finishes by itself, inside the grace period
        self.join()
        job = self.job()
        self.assertEqual(job["status"], "stopped")
        self.assertIs(job["stop"]["terminated"], False)
        self.assertEqual((job["stop"]["requested_by"], job["stop"]["reason"]), ("alice", REASON))
        self.assertEqual(job["stop"]["phase"], "build")
        # The step ran to its end (its usage and checkpoint are kept), then the job went no further.
        self.assertEqual(len(job["usage"]), 1)
        self.assertEqual(job["pending_phase"], "review")
        self.assertTrue(job["commit"])
        self.assertEqual(self.phases(), ["fix-a:build"])
        self.assertNotIn("termed", [p.name for p in self.binary.iterdir()])
        self.assertEqual(self.result_of(), "stopped")
        # The branch and worktree are kept, no pull request was made, none was closed.
        self.assertTrue(self.git("branch", "--list", job["branch"]))
        self.assertTrue(Path(job["worktree"]).is_dir())
        self.assertFalse([call for call in self.gh_calls() if call["args"][:2] in (["pr", "create"], ["pr", "close"])])
        self.assertIn(("fix-a", "stopped"), self.labels)
        self.assertEqual(label_for_job(job), LABEL_BLOCKED)

    def test_a_worker_that_outlasts_the_grace_period_is_terminated_with_its_whole_process_group(self):
        self.prepare("ignore-term", grace=0.3)
        self.wait_until((self.binary / "started").exists, "the worker")
        worker, child = int((self.binary / "pid").read_text()), int((self.binary / "child").read_text())
        self.assertFalse(gone(worker))
        self.assertFalse(gone(child))
        self.ask()
        self.join()  # grace, then SIGTERM (this worker notes it and carries on), then SIGKILL
        self.assertTrue((self.binary / "termed").exists(), "the worker was killed without the SIGTERM first")
        self.assertTrue(gone(worker))
        self.assertTrue(gone(child), "a process of the worker's group outlived the stop")
        job = self.job()
        self.assertEqual(job["status"], "stopped", job.get("error"))
        self.assertIs(job["stop"]["terminated"], True)
        self.assertIsNone(job["error"])
        self.assertIsNone(job["pid"])
        self.assertEqual(self.phases(), ["fix-a:build"])  # no review, no second attempt
        self.assertEqual(self.result_of(), "terminated")
        self.assertEqual(job["attempt"], 1)

    def test_a_worker_that_ends_on_the_sigterm_is_not_sent_the_sigkill(self):
        self.prepare("hang", grace=0.3)
        self.wait_until((self.binary / "started").exists, "the worker")
        signals, real = [], runner_module.signal_group

        def spy(pid, sig=signal.SIGKILL):
            signals.append(sig)
            return real(pid, sig)

        with patch("factory.runner.signal_group", spy):
            self.ask()
            self.join()
        self.assertEqual(signals, [signal.SIGTERM])
        self.assertEqual(self.job()["status"], "stopped")
        self.assertIs(self.job()["stop"]["terminated"], True)

    def test_a_job_that_finishes_while_the_stop_is_pending_is_left_as_it_finished(self):
        self.prepare("blocked")
        self.wait_until((self.binary / "started").exists, "the worker")
        self.ask()
        self.wait_until(lambda: self.result_of() == "stopping", "the supervisor to take the request")
        (self.binary / "release").touch()  # the job ends (blocked) before the grace period does
        self.join()
        job = self.job()
        self.assertEqual(job["status"], "blocked")
        self.assertEqual(job["error"], "cannot go on")
        self.assertIsNone(job.get("stop"))
        self.assertEqual(self.result_of(), "finished")
        self.assertNotIn(("fix-a", "stopped"), self.labels)
        self.assertEqual(label_for_job(job), LABEL_BLOCKED)
        self.assertFalse((self.binary / "termed").exists())

    def test_a_request_first_seen_after_the_worker_thread_is_over_is_settled_when_the_pool_harvests_it(self):
        # The opposite order of 'a phase that has just queued the next one': the worker thread has finished (its own
        # settle_stop ran with nothing pending) but the pool has not harvested the future yet, so the request is still
        # accepted for a job that "has a worker". Nothing but the harvest can settle it then.
        settled = threading.Event()
        original_settle, original_requests = Runner.settle_stop, RunControl.stop_requests

        def settle(runner, key):
            original_settle(runner, key)
            settled.set()

        def requests(owner):
            found = original_requests(owner)
            if found and not settled.is_set():
                (self.binary / "release").touch()  # the build session ends now ...
                self.assertTrue(settled.wait(20), "the worker thread to be over with its phase")  # ... and so does the thread
            return found

        for target, name, replacement in ((Runner, "settle_stop", settle), (RunControl, "stop_requests", requests)):
            patcher = patch.object(target, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.prepare("hang", grace=30)
        self.wait_until((self.binary / "started").exists, "the worker")
        self.ask()
        self.wait_until(lambda: self.job()["status"] == "stopped", "the job to be stopped", seconds=15)
        self.join()  # a supervisor with a stop nobody settles would poll for ever
        job = self.job()
        self.assertIs(job["stop"]["terminated"], False)
        self.assertEqual(job["pending_phase"], "review")
        self.assertEqual(self.phases(), ["fix-a:build"])  # no review started
        self.assertEqual(self.result_of(), "stopped")
        self.assertEqual(self.runner.stopping, {})

    def test_the_other_jobs_of_the_run_go_on_and_a_stopped_job_keeps_its_branch(self):
        self.prepare("hang", plan=two_jobs(), concurrency=2, grace=0.3)
        self.wait_until((self.binary / "started").exists, "the worker of fix-a")
        self.ask()
        self.join()
        self.assertEqual(self.job("fix-b")["status"], "pr_ready")
        stopped = self.job("fix-a")
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual([p for p in self.phases() if p.startswith("fix-a")], ["fix-a:build"])
        self.assertTrue(self.git("branch", "--list", stopped["branch"]))
        self.assertFalse([call for call in self.gh_calls() if call["args"][:2] == ["pr", "close"]])

    def test_the_ledger_names_who_stopped_the_job_and_never_the_reason(self):
        self.prepare("hang")
        self.wait_until((self.binary / "started").exists, "the worker")
        self.ask(principal="alice@example.invalid")
        self.wait_until(lambda: self.result_of() == "stopping", "the supervisor to take the request")
        (self.binary / "release").touch()
        self.join()
        snapshot = self.ledger.read(history.DEFAULT_READ_BYTES)
        stopped = [event for event in snapshot.events if event["kind"] == "job" and event.get("status") == "stopped"]
        self.assertEqual(len(stopped), 1)
        self.assertEqual(stopped[0]["stopped_by"], "alice@example.invalid")
        run = history.fold(snapshot.events)[("org/name", "r1")]
        job = history.public_job(run, run["jobs"]["fix-a"])
        self.assertEqual((job["status"], job["outcome"]), ("stopped", "stopped"))
        self.assertEqual(job["stopped_by"], "alice@example.invalid")
        self.assertTrue(job["stopped_at"])
        self.assertEqual(run["outcome"], "stopped")
        segments = b"".join(path.read_bytes() for path in (self.ledger_dir / "history").glob("events-*.jsonl"))
        self.assertNotIn(b"CANARY-REASON", segments)

    def test_the_supervisor_prints_the_job_and_never_the_reason_or_the_principal(self):
        self.prepare("hang")
        self.wait_until((self.binary / "started").exists, "the worker")
        with patch("builtins.print") as printed:
            self.ask(principal="alice@example.invalid")
            self.wait_until(lambda: self.result_of() == "stopping", "the supervisor to take the request")
            (self.binary / "release").touch()
            self.join()
        lines = " ".join(str(arg) for call in printed.call_args_list for arg in call.args)
        self.assertIn("fix-a: stop requested", lines)
        self.assertIn("fix-a: stopped", lines)
        self.assertNotIn("CANARY-REASON", lines)
        self.assertNotIn("alice", lines)


class BetweenPhasesTests(unittest.TestCase):
    """``accept_stop`` for a job with no worker: at once for one still going, not at all for one that is over."""

    setUp = test_factory.IntegrationTests.setUp
    git = test_factory.IntegrationTests.git
    script = test_factory.IntegrationTests.script
    install_agent = test_factory.IntegrationTests.install_agent

    def runner_with(self, **values):
        (self.run_dir / "state.json").unlink(missing_ok=True)  # every case starts from a fresh state
        runner = Runner(self.repo, self.run_dir, assignment(), timeout=10)
        runner.state.update("fix-a", **values)
        return runner

    def accept(self, runner, job="fix-a"):
        common = self.git("rev-parse", "--path-format=absolute", "--git-common-dir")
        with RunControl(self.run_dir, common) as owner:
            request_stop(self.run_dir, common, job, "alice", REASON)
            runner.service_stops(owner, {})
            return control._read(self.run_dir)["current"]["stop_requests"][job]["result"]

    def test_a_job_waiting_for_its_next_phase_stops_at_once_and_keeps_that_phase_for_resume(self):
        for status, extra in (("waiting_review", {"pending_phase": "review"}), ("queued", {})):
            with self.subTest(status):
                runner = self.runner_with(status=status, attempt=1, waiting_for="a worker slot", **extra)
                self.assertEqual(self.accept(runner), "stopped")
                job = runner.state.data["jobs"]["fix-a"]
                self.assertEqual(job["status"], "stopped")
                self.assertIs(job["stop"]["terminated"], False)
                self.assertEqual(job.get("pending_phase"), extra.get("pending_phase"))
                self.assertIsNone(job["waiting_for"])
                self.assertEqual(runner.stopping, {})

    def test_a_phase_that_has_just_queued_the_next_one_is_still_stopped_before_it_starts(self):
        # The worker thread has moved the job to waiting_review but has not returned yet: the pool still lists its
        # future, so the request is kept for the thread to settle (never dropped, never a review that starts).
        runner = self.runner_with(status="waiting_review", pending_phase="review", attempt=1)
        common = self.git("rev-parse", "--path-format=absolute", "--git-common-dir")
        with RunControl(self.run_dir, common) as owner:
            request_stop(self.run_dir, common, "fix-a", "alice", REASON)
            runner.service_stops(owner, {"future": ("fix-a", "build")})
            self.assertTrue(runner.stop_pending("fix-a"))
            self.assertEqual(runner.state.data["jobs"]["fix-a"]["status"], "waiting_review")
            runner.control = owner
            runner.settle_stop("fix-a")
            self.assertEqual(runner.state.data["jobs"]["fix-a"]["status"], "stopped")
            self.assertFalse(runner.stop_pending("fix-a"))
            self.assertEqual(control._read(self.run_dir)["current"]["stop_requests"]["fix-a"]["result"], "stopped")

    def test_a_job_that_is_over_is_left_exactly_as_it_is(self):
        for status in ("pr_ready", "blocked", "failed", "merged", "closed", "pr_incomplete", "needs_human_publish", "stopped"):
            with self.subTest(status):
                runner = self.runner_with(status=status, attempt=1, pr="https://github.com/org/name/pull/7")
                before = json.dumps(runner.state.data, sort_keys=True)
                self.assertEqual(self.accept(runner), "not_running")
                self.assertEqual(json.dumps(runner.state.data, sort_keys=True), before)

    def test_a_pending_stop_without_a_worker_is_no_reason_to_poll_on(self):
        # The pool has no future for the job, so nothing would ever settle the request: it must end the run (as
        # it does for any job that will not start), not turn the loop into a poll without a wait.
        runner = self.runner_with(status="waiting_review", pending_phase="review", attempt=1)
        runner.stopping["fix-a"] = PendingStop({"requested_by": "alice"}, time.monotonic() + 60)
        ticks = []

        def counted(owner, active):
            ticks.append(1)
            if len(ticks) > 20:
                raise AssertionError("the pool polls on for a job that will not start")

        common = self.git("rev-parse", "--path-format=absolute", "--git-common-dir")
        with patch.object(runner, "service_stops", counted), RunControl(self.run_dir, common) as owner:
            runner.run_pool(owner)
        self.assertLessEqual(len(ticks), 2)
        self.assertEqual(runner.state.data["jobs"]["fix-a"]["status"], "waiting_review")

    def test_a_job_the_run_does_not_have_is_ignored(self):
        runner = self.runner_with(status="queued")
        before = json.dumps(runner.state.data, sort_keys=True)
        self.assertEqual(self.accept(runner, job="nope"), "unknown_job")
        self.assertEqual(json.dumps(runner.state.data, sort_keys=True), before)

    def test_a_stopped_job_is_never_scheduled_again_by_the_supervisor(self):
        runner = self.runner_with(status="waiting_review", pending_phase="review", attempt=1)
        self.assertEqual(self.accept(runner), "stopped")
        self.assertIsNone(runner.pending_phase(runner.state.data["jobs"]["fix-a"]))
        self.assertEqual(runner.waiting_jobs("review"), [])
        self.assertEqual(runner.waiting_jobs("build"), [])


class StaleRecordTests(unittest.TestCase):
    def test_recovery_clears_the_stop_of_a_job_it_turns_interrupted_and_leaves_the_others(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = two_jobs()
            state = State(Path(directory), plan)
            pending = {"state": "stopping", "requested_by": "alice", "requested_at": "2026-11-09T00:00:00Z"}
            done = {"state": "stopped", "requested_by": "alice", "requested_at": "2026-11-09T00:00:00Z"}
            state.update("fix-a", status="running", attempt=1, stop=pending)
            state.update("fix-b", status="stopped", attempt=1, stop=done)
            self.assertEqual(state.recover(), ["fix-a"])
            self.assertEqual(state.data["jobs"]["fix-a"]["status"], "interrupted")
            self.assertIsNone(state.data["jobs"]["fix-a"].get("stop"))
            self.assertEqual(state.data["jobs"]["fix-b"]["stop"], done)  # a stopped job keeps its record


class StatusTests(unittest.TestCase):
    def test_status_shows_a_stopped_job_with_what_to_do_next(self):
        plan = assignment()
        with tempfile.TemporaryDirectory() as temp:
            state = State(Path(temp), plan)
            state.update("fix-a", status="stopped", attempt=1, stop={"state": "stopped", "requested_by": "alice"})
            report = status_report(plan, state.data)
        row = report["jobs"][0]
        self.assertEqual(row["status"], "stopped")
        self.assertEqual(row["owner"], "operator")
        self.assertIn("retry fix-a --reason", row["next_action"])
        self.assertIn("left as they are", row["next_action"])
        self.assertFalse(row["active_worker"])


class CliTests(unittest.TestCase):
    """``factory stop`` and ``retry`` as processes, against a real supervisor process."""

    setUp = test_factory.IntegrationTests.setUp
    git = test_factory.IntegrationTests.git
    script = test_factory.IntegrationTests.script
    install_agent = test_factory.IntegrationTests.install_agent

    def command(self, *args, timeout=60):
        return subprocess.run(
            [sys.executable, "-m", "factory", "--run-dir", str(self.run_dir), *args],
            cwd=self.repo,
            env=dict(os.environ, FACTORY_STOP_GRACE_SECONDS="1"),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def wait_file(self, path, process):
        deadline = time.monotonic() + 30
        while not path.exists():
            if process.poll() is not None:
                self.fail(f"supervisor exited early: {process.communicate()}")
            if time.monotonic() > deadline:
                self.fail("worker did not start")
            time.sleep(0.02)

    def start_supervisor(self, mode):
        self.install_agent(WORKER)
        (self.binary / "mode").write_text(mode)
        (self.run_dir / "plan.json").write_text(json.dumps(assignment()))
        env = dict(os.environ, FACTORY_STOP_GRACE_SECONDS="1")
        process = subprocess.Popen(
            [sys.executable, "-m", "factory", "--run-dir", str(self.run_dir), "run", "--timeout", "60"],
            cwd=self.repo,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.addCleanup(lambda: (self.binary / "release").touch())
        self.addCleanup(lambda: process.poll() is None and process.kill())
        self.wait_file(self.binary / "started", process)
        return process

    def test_stop_refuses_what_is_not_running_and_changes_nothing(self):
        State(self.run_dir, assignment())  # a started run whose only job is queued
        before = (self.run_dir / "state.json").read_bytes()
        queued = self.command("stop", "fix-a")
        self.assertEqual(queued.returncode, 1)
        self.assertIn("job is not running (status queued)", queued.stderr)
        unknown = self.command("stop", "nope")
        self.assertEqual((unknown.returncode, "unknown job" in unknown.stderr), (1, True))
        self.assertEqual((self.run_dir / "state.json").read_bytes(), before)
        State(self.run_dir, assignment()).update("fix-a", status="running")
        ownerless = self.command("stop", "fix-a")
        self.assertEqual(ownerless.returncode, 1)
        self.assertIn("no drain-capable supervisor", ownerless.stderr)
        self.assertFalse((self.run_dir / "supervisor-control.json").exists())
        fresh = self.run_dir.parent / "fresh"
        fresh.mkdir()
        unstarted = subprocess.run(
            [sys.executable, "-m", "factory", "--run-dir", str(fresh), "stop", "fix-a"],
            cwd=self.repo, env=dict(os.environ), capture_output=True, text=True, timeout=60,
        )
        self.assertIn("has not started", unstarted.stderr)

    def test_stop_then_retry_resumes_the_job_and_a_second_stop_is_a_no_op_with_the_same_answer(self):
        process = self.start_supervisor("hang")
        first = self.command("stop", "fix-a", "--reason", REASON, "--principal", "alice", "--json")
        self.assertEqual(first.returncode, 0, first.stderr)
        answer = json.loads(first.stdout)
        self.assertEqual((answer["stop"], answer["requested_by"], answer["grace_seconds"]), ("requested", "alice", 1.0))
        second = self.command("stop", "fix-a", "--reason", "another", "--principal", "mallory", "--json")
        again = json.loads(second.stdout)
        self.assertEqual((again["stop"], again["requested_by"], again["requested_at"]), ("already_requested", "alice", answer["requested_at"]))
        output, _ = process.communicate(timeout=60)  # the worker ignores the grace period: it is terminated
        self.assertEqual(process.returncode, 2, output)  # a job that is not published ends the run non-zero, as for failed
        self.assertIn("fix-a: stopped", output)
        self.assertNotIn("CANARY-REASON", output)
        job = json.loads((self.run_dir / "state.json").read_text())["jobs"]["fix-a"]
        self.assertEqual((job["status"], job["stop"]["terminated"], job["stop"]["requested_by"]), ("stopped", True, "alice"))
        status = self.command("status", "--json", "--offline")
        row = json.loads(status.stdout)["jobs"][0]
        self.assertEqual(row["status"], "stopped")
        # Once it is stopped it is no longer running: 'not running', and nothing changes.
        after = self.command("stop", "fix-a")
        self.assertIn("job is not running (status stopped)", after.stderr)
        # retry needs a reason like for any later attempt, then continues the job to a published pull request.
        refused = self.command("retry", "fix-a")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("retry requires --reason", refused.stderr)
        (self.binary / "mode").write_text("fast")
        retried = self.command("retry", "fix-a", "--reason", "the migration is done")
        self.assertEqual(retried.returncode, 0, retried.stderr)
        job = json.loads((self.run_dir / "state.json").read_text())["jobs"]["fix-a"]
        self.assertEqual(job["status"], "queued")
        self.assertIsNone(job.get("stop"))
        finished = self.command("run", "--timeout", "60", timeout=120)
        self.assertEqual(finished.returncode, 0, f"{finished.stdout}{finished.stderr}")
        job = json.loads((self.run_dir / "state.json").read_text())["jobs"]["fix-a"]
        self.assertEqual(job["status"], "pr_ready")
        self.assertEqual(job["attempt"], 2)


if __name__ == "__main__":
    unittest.main()

"""tools/infra_deploy_gate.py: the pause, drain, wait and resume calls an apply pipeline makes.

Most tests use fakes for the manager, the token and the clock. The end-to-end class drives the
real script code over HTTP against a real manager in entra-jwt mode (tests/test_manager.py's
JwtCase), so the paths, methods, bodies and the role check are the manager's own.
"""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from tests import test_manager as tm

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("infra_deploy_gate_under_test", ROOT / "tools" / "infra_deploy_gate.py")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)

SECRET = "eyJ-SECRET-BEARER-TOKEN"


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeManager:
    """Scripted ``snapshot`` answers (the last one repeats) and recorded calls."""

    def __init__(self, *snapshots, pause_errors=0):
        self.snapshots = list(snapshots)
        self.calls = []
        self.drain_status = 200
        self.start_statuses = []  # scripted answers of start_run; 202 once they run out
        self.pause_errors = pause_errors

    def snapshot(self):
        self.calls.append(("snapshot",))
        item = self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]
        if isinstance(item, Exception):
            raise item
        return item

    def drain(self, repo, run):
        self.calls.append(("drain", repo, run))
        return self.drain_status

    def start_run(self, repo, run):
        self.calls.append(("run", repo, run))
        return self.start_statuses.pop(0) if self.start_statuses else 202

    def set_pause(self, paused):
        self.calls.append(("pause", paused))
        if self.pause_errors:
            self.pause_errors -= 1
            raise gate.GateError("POST /trigger-pause paused=false answered 503")


BUSY = (True, [("org/name", "run", "r1")])
IDLE = (True, [])


class Capture(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.summary = Path(temp.name) / "summary.md"
        patcher = unittest.mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(self.summary)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def summary_text(self):
        return self.summary.read_text() if self.summary.exists() else ""

    def run_quiet(self, function, *args, **kwargs):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                function(*args, **kwargs)
            finally:
                self.output = out.getvalue()


class WaitTests(Capture):
    def wait(self, manager, timeout=1800.0, poll=15.0):
        self.run_quiet(gate.wait, manager, timeout=timeout, poll=poll, clock=self.clock.clock, sleep=self.clock.sleep)

    def test_returns_as_soon_as_no_supervisor_is_active(self):
        manager = FakeManager(BUSY, BUSY, IDLE)
        self.wait(manager)
        self.assertEqual(manager.calls.count(("snapshot",)), 3)
        self.assertEqual(self.clock.sleeps, [15.0, 15.0])
        self.assertEqual(self.summary_text(), "")

    def test_a_run_that_is_listed_is_drained_once_however_long_it_takes(self):
        manager = FakeManager(BUSY, BUSY, BUSY, IDLE)
        self.wait(manager)
        self.assertEqual([call for call in manager.calls if call[0] == "drain"], [("drain", "org/name", "r1")])

    def test_a_supervisor_that_appears_during_the_wait_is_drained_too(self):
        manager = FakeManager(BUSY, (True, [("org/other", "run", "r2")]), IDLE)
        self.wait(manager)
        self.assertEqual(
            [call for call in manager.calls if call[0] == "drain"],
            [("drain", "org/name", "r1"), ("drain", "org/other", "r2")],
        )

    def test_a_plan_is_waited_for_but_never_drained(self):
        manager = FakeManager((True, [("org/name", "plan", "auto-1")]), IDLE)
        self.wait(manager)
        self.assertEqual([call for call in manager.calls if call[0] == "drain"], [])

    def test_the_bound_fails_and_names_every_active_run_in_the_summary(self):
        active = [("org/name", "run", "r1"), ("org/other", "plan", "auto-7")]
        manager = FakeManager((True, active))
        with self.assertRaises(gate.GateError) as caught:
            self.wait(manager, timeout=1800.0)
        self.assertGreaterEqual(self.clock.now, 1800.0)
        self.assertLess(self.clock.now, 1800.0 + 1)  # the last sleep is cut to the deadline
        text = self.summary_text()
        self.assertIn("nothing was applied", text)
        self.assertIn("`org/name run r1 (run)`", text)
        self.assertIn("`org/other run auto-7 (plan)`", text)
        self.assertIn("org/name run r1", str(caught.exception))
        self.assertIn("nothing was applied", str(caught.exception))

    def test_names_in_the_summary_cannot_inject_markup(self):
        manager = FakeManager((True, [("org/na`me", "run", "r1\n## x")]))
        with self.assertRaises(gate.GateError):
            self.wait(manager, timeout=30.0)
        self.assertNotIn("`me", self.summary_text().replace("org/na?me", ""))
        self.assertNotIn("## x", self.summary_text())

    def test_a_cleared_pause_stops_the_wait_at_once(self):
        manager = FakeManager((False, []))
        with self.assertRaises(gate.Refused):
            self.wait(manager)
        self.assertEqual(self.clock.sleeps, [])

    def test_a_manager_that_cannot_be_read_for_a_while_does_not_end_the_wait(self):
        manager = FakeManager(gate.GateError("GET /repos failed (URLError)"), gate.GateError("x"), IDLE)
        self.wait(manager)
        self.assertEqual(len(self.clock.sleeps), 2)

    def test_a_manager_that_stays_unreadable_fails_at_the_deadline(self):
        manager = FakeManager(gate.GateError("GET /repos failed (URLError)"))
        with self.assertRaises(gate.GateError):
            self.wait(manager, timeout=60.0)
        self.assertIn("could not be read", self.summary_text())

    def test_a_token_that_cannot_be_issued_fails_at_once(self):
        manager = FakeManager(gate.Refused("az could not issue a token"))
        with self.assertRaises(gate.Refused):
            self.wait(manager)
        self.assertEqual(self.clock.sleeps, [])


PODS = ("worker-pods", "pods", "2-running")


class PodAwareGateTests(Capture):
    """The wait also waits for worker pods (docs/pod-backend.md, slice 20): a rollout would cut a running session off."""

    def wait(self, manager, timeout=1800.0, poll=15.0):
        self.run_quiet(gate.wait, manager, timeout=timeout, poll=poll, clock=self.clock.clock, sleep=self.clock.sleep)

    def test_a_running_pod_holds_the_wait_after_the_last_supervisor_is_gone(self):
        manager = FakeManager((True, [PODS]), (True, [PODS]), IDLE)
        self.wait(manager)
        self.assertEqual(self.clock.sleeps, [15.0, 15.0])
        self.assertEqual([call for call in manager.calls if call[0] == "drain"], [], "a pod is waited for, never drained")

    def test_a_pod_that_outlives_the_bound_fails_the_wait_and_is_named_in_the_summary(self):
        manager = FakeManager(BUSY[:1] + ([PODS],))
        with self.assertRaises(gate.GateError) as caught:
            self.wait(manager, timeout=60.0)
        self.assertIn("worker pods (2-running)", self.summary_text())
        self.assertIn("nothing was applied", self.summary_text())
        self.assertIn("worker pods (2-running)", str(caught.exception))

    def test_a_supervisor_and_a_pod_are_both_waited_for(self):
        manager = FakeManager((True, [("org/name", "run", "r1"), PODS]), (True, [PODS]), IDLE)
        self.wait(manager)
        self.assertEqual([call for call in manager.calls if call[0] == "drain"], [("drain", "org/name", "r1")])

    def test_the_census_the_manager_reports_decides_what_is_busy(self):
        idle = {"live": 0, "final": 3, "unknown": False}
        self.assertIsNone(gate.pods_entry(idle))
        self.assertEqual(gate.pods_entry({"live": 2, "final": 0, "unknown": False}), PODS)

    def test_a_census_that_cannot_be_read_is_busy_never_idle(self):
        for census in (
            {"live": 0, "final": 0, "unknown": True},  # the launcher did not answer
            {"live": 2, "final": 0, "unknown": True},
            None, "idle", [], 0, {},
            {"live": 0},  # no "unknown": a malformed answer is not an idle one
            {"unknown": False},
            {"live": True, "unknown": False},  # a boolean is not a count
            {"live": -1, "unknown": False},
            {"live": "0", "unknown": False},
            {"live": 0, "unknown": 0},  # a number is not False
        ):
            with self.subTest(census=census):
                self.assertEqual(gate.pods_entry(census)[:2], ("worker-pods", "pods"))

    def test_the_label_of_a_pod_entry_cannot_inject_markup(self):
        text = gate.label(("worker-pods", "pods", "x`\n## y"))
        self.assertEqual(text, "worker pods (x?????y)")
        self.assertFalse(any(ch in text for ch in "`#\n"))

    def test_a_pod_entry_is_not_a_supervisor_and_is_never_asked_to_drain(self):
        manager = FakeManager((True, [PODS]))
        asked = set()
        gate.ask_drain(manager, [PODS], asked)
        self.assertEqual((manager.calls, asked), ([], set()))


class SnapshotTests(unittest.TestCase):
    """``Manager.snapshot`` over a scripted HTTP answer."""

    def snapshot(self, payload):
        class Response:
            status = 200

            def __init__(self, body):
                self.body = body

            def read(self):
                return self.body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        manager = gate.Manager("https://manager.example", "api://aud", token=lambda audience: "t", opener=lambda request, timeout: Response(json.dumps(payload).encode()))
        return manager.snapshot()

    BASE = {"trigger_paused": True, "repos": [{"repo": "org/name", "supervisor": None}]}

    def test_an_older_manager_without_the_key_has_no_pods_to_wait_for(self):
        self.assertEqual(self.snapshot(self.BASE), (True, []))

    def test_a_census_with_running_pods_is_one_active_entry(self):
        payload = {**self.BASE, "worker_pods": {"live": 2, "final": 1, "unknown": False}}
        self.assertEqual(self.snapshot(payload), (True, [PODS]))

    def test_a_census_that_says_nothing_runs_is_idle(self):
        payload = {**self.BASE, "worker_pods": {"live": 0, "final": 1, "unknown": False}}
        self.assertEqual(self.snapshot(payload), (True, []))

    def test_a_census_that_is_present_but_wrong_is_busy(self):
        for census in (None, "x", {"live": 0}, {"unknown": True}):
            with self.subTest(census=census):
                self.assertEqual(self.snapshot({**self.BASE, "worker_pods": census})[1], [("worker-pods", "pods", "unreadable")])

    def test_the_pod_entry_follows_the_supervisors(self):
        payload = {
            "trigger_paused": True,
            "repos": [{"repo": "org/name", "supervisor": {"kind": "run", "run": "r1"}}],
            "worker_pods": {"live": 1, "final": 0, "unknown": False},
        }
        self.assertEqual(self.snapshot(payload)[1], [("org/name", "run", "r1"), ("worker-pods", "pods", "1-running")])


class ResumeTests(Capture):
    def resume(self, manager, timeout=300.0):
        self.run_quiet(gate.resume, manager, timeout=timeout, poll=10.0, clock=self.clock.clock, sleep=self.clock.sleep)

    def test_clears_the_pause(self):
        manager = FakeManager(IDLE)
        self.resume(manager)
        self.assertEqual(manager.calls, [("pause", False)])

    def test_retries_while_the_manager_restarts(self):
        manager = FakeManager(IDLE, pause_errors=3)
        self.resume(manager)
        self.assertEqual(manager.calls, [("pause", False)] * 4)

    def test_a_pause_that_cannot_be_cleared_is_reported_loudly_and_fails(self):
        manager = FakeManager(IDLE, pause_errors=10**6)
        with self.assertRaises(gate.GateError) as caught:
            self.resume(manager, timeout=60.0)
        self.assertIn("STILL SET", str(caught.exception))
        self.assertIn("STILL SET", self.summary_text())
        self.assertIn("paused", self.summary_text())


class DrainedRunsTests(Capture):
    """The runs the gate drained continue after the pause is cleared (nothing else resumes a drained run)."""

    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = str(Path(temp.name) / "drained.json")

    def resume(self, manager, timeout=300.0):
        self.run_quiet(
            gate.resume, manager, timeout=timeout, poll=10.0, clock=self.clock.clock, sleep=self.clock.sleep, state=self.state
        )

    def test_drain_and_wait_record_the_runs_that_accepted_the_drain_once(self):
        self.run_quiet(gate.drain, FakeManager((True, [("org/a", "run", "r1"), ("org/b", "plan", "p1")])), state=self.state)
        self.run_quiet(
            gate.wait, FakeManager(BUSY, IDLE), poll=1.0, clock=self.clock.clock, sleep=self.clock.sleep, state=self.state
        )
        self.assertEqual(gate.load_drained(self.state), [("org/a", "r1"), ("org/name", "r1")])

    def test_a_drain_the_manager_refused_is_not_recorded(self):
        manager = FakeManager((True, [("org/a", "run", "r1")]))
        manager.drain_status = 422
        self.run_quiet(gate.drain, manager, state=self.state)
        self.assertEqual(gate.load_drained(self.state), [])

    def test_resume_clears_the_pause_first_then_starts_each_drained_run(self):
        gate.record_drained(self.state, "org/a", "r1")
        gate.record_drained(self.state, "org/b", "r2")
        manager = FakeManager(IDLE)
        self.resume(manager)
        self.assertEqual(manager.calls, [("pause", False), ("run", "org/a", "r1"), ("run", "org/b", "r2")])
        self.assertFalse(os.path.exists(self.state))
        self.assertIn("run started again", self.output)

    def test_no_drained_run_means_no_run_call(self):
        manager = FakeManager(IDLE)
        self.resume(manager)
        self.assertEqual(manager.calls, [("pause", False)])

    def test_a_run_that_cannot_be_started_is_named_and_does_not_fail_the_job(self):
        gate.record_drained(self.state, "org/a", "r1")
        manager = FakeManager(IDLE)
        manager.start_statuses = [409]
        self.resume(manager)
        self.assertIn("org/a run r1", self.summary_text())
        self.assertIn("::warning::", self.output)

    def test_a_manager_that_is_still_coming_back_is_retried(self):
        gate.record_drained(self.state, "org/a", "r1")
        manager = FakeManager(IDLE)
        manager.start_statuses = [0, 503]
        self.resume(manager)
        self.assertEqual(manager.calls.count(("run", "org/a", "r1")), 3)
        self.assertEqual(self.summary_text(), "")

    def test_runs_are_not_started_while_the_pause_cannot_be_cleared(self):
        gate.record_drained(self.state, "org/a", "r1")
        manager = FakeManager(IDLE, pause_errors=10**6)
        with self.assertRaises(gate.GateError):
            self.resume(manager, timeout=30.0)
        self.assertNotIn(("run", "org/a", "r1"), manager.calls)
        self.assertEqual(gate.load_drained(self.state), [("org/a", "r1")])

    def test_the_state_file_defaults_to_runner_temp(self):
        self.assertEqual(gate.state_path({"RUNNER_TEMP": "/t"}), os.path.join("/t", "factory-drained.json"))
        self.assertEqual(gate.state_path({"GATE_STATE_FILE": "/x.json", "RUNNER_TEMP": "/t"}), "/x.json")


class DrainTests(Capture):
    def test_drains_each_active_run_and_skips_a_plan(self):
        manager = FakeManager(
            (True, [("org/name", "run", "r1"), ("org/other", "plan", "auto-1"), ("org/third", "run", "r3")])
        )
        self.run_quiet(gate.drain, manager)
        self.assertEqual(
            manager.calls, [("snapshot",), ("drain", "org/name", "r1"), ("drain", "org/third", "r3")]
        )

    def test_nothing_active_is_fine(self):
        manager = FakeManager(IDLE)
        self.run_quiet(gate.drain, manager)
        self.assertEqual(manager.calls, [("snapshot",)])
        self.assertIn("nothing to drain", self.output)


class TokenTests(Capture):
    def test_the_token_comes_from_the_az_session_and_failures_do_not_echo_az_output(self):
        ok = subprocess.CompletedProcess([], 0, stdout=SECRET + "\n", stderr="")
        with unittest.mock.patch.object(subprocess, "run", return_value=ok) as run:
            self.assertEqual(gate.fetch_token("api://aud"), SECRET)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], ["az", "account", "get-access-token"])
        self.assertEqual(argv[argv.index("--resource") + 1], "api://aud")
        bad = subprocess.CompletedProcess([], 1, stdout="", stderr="AADSTS-DETAIL " + SECRET)
        with unittest.mock.patch.object(subprocess, "run", return_value=bad):
            with self.assertRaises(gate.Refused) as caught:
                gate.fetch_token("api://aud")
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertNotIn("AADSTS", str(caught.exception))
        self.assertIn("Factory.Operator", str(caught.exception))

    def test_no_secret_is_written_to_a_file_or_the_environment(self):
        source = (ROOT / "tools" / "infra_deploy_gate.py").read_text()
        for forbidden in ("add-mask", "GITHUB_ENV", "GITHUB_OUTPUT", "os.environ[", "os.putenv", "print(token", "say(token"):
            self.assertNotIn(forbidden, source)

    def test_http_errors_are_reported_by_status_only(self):
        error = gate.urllib.error.HTTPError("https://m/repos", 500, "x", {}, io.BytesIO(b"BODY-" + SECRET.encode()))

        def opener(request, timeout):
            raise error

        manager = gate.Manager("https://m", "aud", token=lambda audience: SECRET, opener=opener)
        with self.assertRaises(gate.GateError) as caught:
            manager.snapshot()
        self.assertEqual(str(caught.exception), "GET /repos answered 500")

    def test_transport_errors_name_only_the_exception_type(self):
        def opener(request, timeout):
            raise gate.urllib.error.URLError(f"connection to {SECRET} refused")

        manager = gate.Manager("https://m", "aud", token=lambda audience: SECRET, opener=opener)
        with self.assertRaises(gate.GateError) as caught:
            manager.snapshot()
        self.assertEqual(str(caught.exception), "GET /repos failed (URLError)")

    def test_the_url_must_be_http_or_https(self):
        for bad in ("", "ftp://x", "https://", "https://x/?q=1", "file:///etc"):
            with self.subTest(bad=bad), self.assertRaises(gate.GateError):
                gate.Manager(bad, "aud")


class MainTests(Capture):
    def test_usage_and_missing_configuration(self):
        codes = []
        self.run_quiet(lambda: codes.extend([gate.main([], {}), gate.main(["nope"], {})]))
        self.assertEqual(codes, [2, 2])
        code = []
        self.run_quiet(lambda: code.append(gate.main(["pause"], {})))
        self.assertEqual(code, [1])
        self.assertIn("FACTORY_MANAGER_URL and FACTORY_MANAGER_AUDIENCE", self.output)

    def test_a_failure_is_an_error_annotation_and_exit_1(self):
        class Failing(FakeManager):
            def set_pause(self, paused):
                raise gate.GateError("POST /trigger-pause paused=true answered 403")

        code = []
        self.run_quiet(lambda: code.append(gate.main(["pause"], {}, manager=Failing(IDLE))))
        self.assertEqual(code, [1])
        self.assertIn("::error::POST /trigger-pause paused=true answered 403", self.output)

    def test_numbers_are_validated(self):
        with unittest.mock.patch.dict(os.environ, {"GATE_WAIT_SECONDS": "soon"}):
            code = []
            self.run_quiet(lambda: code.append(gate.main(["wait"], {"FACTORY_MANAGER_URL": "https://m", "FACTORY_MANAGER_AUDIENCE": "a"}, manager=FakeManager(IDLE))))
            self.assertEqual(code, [1])


class EndToEndTests(tm.JwtCase):
    """The script's own HTTP code against a real manager that checks a real (test-signed) bearer token."""

    def client(self, token):
        return gate.Manager(f"http://127.0.0.1:{self.port}", "api://aud", token=lambda audience: token)

    def run_gate(self, command, token=None, **options):
        manager = self.client(token or self.token())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            getattr(gate, command)(manager, **options)
        return out.getvalue()

    def test_pause_then_resume_through_the_real_endpoint(self):
        self.run_gate("pause")
        self.assertTrue(self.manager.trigger_paused())
        self.run_gate("resume", timeout=5.0, poll=0.01)
        self.assertFalse(self.manager.trigger_paused())

    def test_a_tripped_breaker_survives_the_gates_pause_then_resume_and_the_gate_still_succeeds(self):
        """The gate resumes on every automatic promotion; that must not clear a stop-the-world breaker (#146)."""
        self.manager.breaker.trip("spend_limit")
        self.run_gate("pause")
        self.assertTrue(self.manager.trigger_pause_set())
        out = self.run_gate("resume", timeout=5.0, poll=0.01)
        self.assertIn("trigger resumed", out)
        self.assertFalse(self.manager.trigger_pause_set())  # the manual marker is cleared, as before
        self.assertTrue(self.manager.breaker.tripped())  # the breaker is not
        self.assertTrue(self.manager.trigger_paused())

    def test_a_viewer_token_cannot_pause_and_nothing_is_set(self):
        with self.assertRaises(gate.GateError) as caught:
            self.run_gate("pause", token=self.token(roles=["Factory.Viewer"]))
        self.assertEqual(str(caught.exception), "POST /trigger-pause paused=true answered 403")
        self.assertFalse(self.manager.trigger_paused())

    def test_drain_and_wait_see_a_real_supervisor_then_the_wait_ends_when_it_exits(self):
        self.make_run()
        self.manager.set_trigger_pause(True)
        self.assertEqual(self.call("POST", self.path("run"), principal=None, headers={"Authorization": f"Bearer {self.token()}"})[0], 202)
        out = self.run_gate("drain")
        self.assertIn("drain requested for org/name run r1 (run): HTTP 200", out)
        self.assertIn(self.base_argv() + ["drain"], self.runner.argvs)
        process = self.spawner.processes[0]
        process.returncode = 0  # the drained supervisor exits
        out = self.run_gate("wait", timeout=5.0, poll=0.01)
        self.assertIn("safe to apply", out)
        self.assertNotIn(SECRET, out)

    def test_wait_fails_while_a_supervisor_stays_and_the_summary_names_it(self):
        self.make_run()
        self.manager.set_trigger_pause(True)
        self.call("POST", self.path("run"), principal=None, headers={"Authorization": f"Bearer {self.token()}"})
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "summary.md"
            with unittest.mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(path)}):
                with self.assertRaises(gate.GateError), contextlib.redirect_stdout(io.StringIO()):
                    gate.wait(self.client(self.token()), timeout=0.3, poll=0.05)
            self.assertIn("`org/name run r1 (run)`", path.read_text())

    def test_a_drained_run_is_started_again_through_the_real_endpoint_after_the_pause_is_cleared(self):
        self.make_run()
        self.manager.set_trigger_pause(True)
        self.call("POST", self.path("run"), principal=None, headers={"Authorization": f"Bearer {self.token()}"})
        with tempfile.TemporaryDirectory() as temp:
            state = str(Path(temp) / "drained.json")
            self.run_gate("drain", state=state)
            self.assertEqual(gate.load_drained(state), [("org/name", "r1")])
            self.spawner.processes[0].returncode = 0
            self.run_gate("wait", timeout=5.0, poll=0.01, state=state)
            started = len(self.spawner.processes)
            out = self.run_gate("resume", timeout=5.0, poll=0.01, state=state)
        self.assertFalse(self.manager.trigger_paused())
        self.assertIn("run started again for org/name run r1 (run): HTTP 202", out)
        self.assertEqual(len(self.spawner.processes), started + 1)

    def test_a_manager_that_reports_pods_holds_the_wait_until_they_are_gone_and_one_without_the_key_does_not(self):
        self.manager.set_trigger_pause(True)
        out = self.run_gate("wait", timeout=5.0, poll=0.01)
        self.assertIn("safe to apply", out)  # no pod sweeper wired in: the answer has no worker_pods key
        self.assertNotIn("worker_pods", self.call("GET", "/repos", principal=None, headers={"Authorization": f"Bearer {self.token()}"})[1])
        census = {"live": 1, "final": 0, "unknown": False}
        self.manager.pods = type("Pods", (), {"census": lambda self_: dict(census)})()
        with self.assertRaises(gate.GateError), contextlib.redirect_stdout(io.StringIO()):
            gate.wait(self.client(self.token()), timeout=0.3, poll=0.05)
        census["live"] = 0
        self.assertIn("safe to apply", self.run_gate("wait", timeout=5.0, poll=0.01))
        census["unknown"] = True
        with self.assertRaises(gate.GateError), contextlib.redirect_stdout(io.StringIO()):
            gate.wait(self.client(self.token()), timeout=0.3, poll=0.05)

    def test_wait_refuses_when_the_pause_was_never_set(self):
        with self.assertRaises(gate.Refused), contextlib.redirect_stdout(io.StringIO()):
            gate.wait(self.client(self.token()), timeout=1.0, poll=0.01)


if __name__ == "__main__":
    unittest.main()

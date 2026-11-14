"""``POST /repos/{org}/{name}/runs/{run}/stop`` (docs/manager.md, "Stopping a job"): who may, what is refused, what is audited.

A real ``ManagerServer`` on a fake runner and spawner, as in tests/test_manager.py. The CLI is the fake runner: the
handler's own checks (404, 409, the content type and origin rules) must refuse before any process is started, and the
argv it builds must carry the sanitised reason and the principal as data after ``--flag=value``.
"""

import json
import os
import unittest

from factory import dashboard, history, manager as mgr
from factory.manager import CommandResult
from factory.state import atomic_json, timestamp
from tests import test_manager as tm
from tests.test_dashboard_api import DashboardCase

JSON = {"Content-Type": "application/json"}
CANARY = "CANARY-REASON agent-style text"
ANSWER = {"job": "fix-a", "stop": "requested", "requested_at": "2026-11-09T00:00:00Z", "requested_by": "x", "grace_seconds": 120.0}


class StopCase(DashboardCase):
    def setUp(self):
        super().setUp()
        self.directory = self.plan_run(status="running", worker_phase="build", phase="build", attempt=1)
        self.supervise()
        self.runner.respond = lambda argv: CommandResult(0, f"{json.dumps(ANSWER)}\n".encode(), b"")

    def supervise(self, status="running", pid=None, started_at=None):
        """A control record of this test process as the run's supervisor, started now (as a supervisor writes it)."""
        atomic_json(
            self.directory / "supervisor-control.json",
            {
                "version": 1,
                "history": [],
                "current": {"token": "t", "pid": os.getpid() if pid is None else pid, "status": status, "started_at": started_at or timestamp()},
            },
        )

    def stop(self, body=None, principal=tm.OPERATOR, headers=JSON, run="r1", repo="org/name"):
        sent = {"job": "fix-a"} if body is None else body
        return self.call("POST", self.path("stop", run, repo), body=sent, principal=principal, headers=headers)

    def state_bytes(self):
        return (self.directory / "state.json").read_bytes()


class WhoMayTests(StopCase):
    def test_anonymous_is_401_a_role_less_caller_and_a_viewer_are_403_and_nothing_runs(self):
        before = self.state_bytes()
        for principal, expected in ((None, 401), (tm.NO_ROLE, 403), (tm.VIEWER, 403)):
            with self.subTest(principal=principal):
                status, payload, _ = self.stop(principal=principal)
                self.assertEqual(status, expected, payload)
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.state_bytes(), before)

    def test_the_refusals_keep_their_documented_order(self):
        self.assertEqual(self.stop(principal=None, headers={})[0], 401)  # before the content type is looked at
        self.assertEqual(self.stop(principal=tm.VIEWER, headers={})[0], 403)
        self.assertEqual(self.stop(principal=tm.OPERATOR, run="nope")[0], 404)  # a run that is not there
        self.assertEqual(self.stop(repo="org/missing")[0], 404)
        self.assertEqual(self.call("GET", self.path("stop"))[0], 405)
        self.assertEqual(self.stop(headers={})[0], 415)  # then the content type, before the body is read
        self.assertEqual(self.stop(body={"job": "../x"})[0], 400)
        self.assertEqual(self.runner.calls, [])

    def test_an_operator_stops_a_running_job_and_the_argv_carries_the_data_as_flags(self):
        status, payload, _ = self.stop({"job": "fix-a", "reason": CANARY})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["stop"], ANSWER)
        self.assertEqual(
            self.runner.argvs,
            [[*self.base_argv(), "stop", "fix-a", "--json", f"--reason={CANARY}", f"--principal={tm.OID}"]],
        )

    def test_the_audit_line_and_the_ledger_name_the_principal_and_never_the_reason(self):
        self.stop({"job": "fix-a", "reason": CANARY})
        audit = self.audit_lines()[-1]
        self.assertIn(f"principal={tm.OID} method=POST path=/repos/org/name/runs/r1/stop status=200", audit)
        self.assertIn(f"--reason=<{len(CANARY)} chars>", audit)
        self.assertIn(f"--principal=<{len(tm.OID)} chars>", audit)
        self.assertNotIn("CANARY", "".join((self.out.getvalue(), self.err.getvalue())))
        events = self.manager.ledger.read(history.DEFAULT_READ_BYTES).events
        operator = [event for event in events if event["kind"] == "operator"]
        self.assertEqual(len(operator), 1)
        self.assertEqual((operator[0]["action"], operator[0]["job"], operator[0]["principal"], operator[0]["http"]), ("stop", "fix-a", tm.OID, 200))
        run = history.fold(events)[("org/name", "r1")]
        self.assertEqual(run["operators"][0]["action"], "stop")
        segments = b"".join(path.read_bytes() for path in (self.data_dir / "history").glob("events-*.jsonl"))
        self.assertNotIn(b"CANARY", segments)

    def test_a_refused_stop_is_not_written_to_the_ledger(self):
        self.stop({"job": "nope"})
        self.assertEqual([e for e in self.manager.ledger.read(history.DEFAULT_READ_BYTES).events if e["kind"] == "operator"], [])


class WhatIsRefusedTests(StopCase):
    def test_a_job_that_is_not_running_is_409_and_nothing_is_touched(self):
        for status in ("queued", "waiting_review", "pr_ready", "blocked", "failed", "stopped", "merged"):
            with self.subTest(status):
                self.plan_run(status=status, attempt=1)
                before = self.state_bytes()
                code, payload, _ = self.stop()
                self.assertEqual((code, payload["error"], payload["job_status"]), (409, "job is not running", status))
                self.assertEqual(self.state_bytes(), before)
        self.assertEqual(self.runner.calls, [])

    def test_an_unknown_job_or_a_run_that_has_not_started_is_404(self):
        self.assertEqual(self.stop({"job": "nope"})[0], 404)
        self.plan_run(run="r2", started=False)
        self.assertEqual(self.stop(run="r2")[0], 404)
        self.assertEqual(self.runner.calls, [])

    def test_a_running_job_without_a_live_supervisor_is_409(self):
        for label, prepare in (
            ("no record", lambda: (self.directory / "supervisor-control.json").unlink()),
            ("dead pid", lambda: self.supervise(pid=2**22 + 12345)),
            # a live process that started after the record did is another one that got the same id
            ("reused pid", lambda: self.supervise(started_at="2000-01-01T00:00:00Z")),
            ("finished", lambda: self.supervise(status="drained")),
        ):
            with self.subTest(label):
                self.supervise()
                prepare()
                code, payload, _ = self.stop()
                self.assertEqual((code, payload["error"]), (409, "no supervisor is running this run"))
        self.assertEqual(self.runner.calls, [])

    def test_a_draining_supervisor_may_still_be_asked(self):
        self.supervise(status="draining")
        self.assertEqual(self.stop()[0], 200)

    def test_the_body_is_validated_like_every_other_path_parameter(self):
        bad = (
            {},
            {"job": ""},
            {"job": "a b"},
            {"job": "x" * 65},
            {"job": 7},
            {"job": "fix-a", "reason": 7},
            {"job": "fix-a", "reason": "x" * 201},
            {"job": "fix-a", "unknown": 1},
            [],
        )
        for body in bad:
            with self.subTest(body=body):
                self.assertEqual(self.stop(body)[0], 400)
        self.assertEqual(self.call("POST", self.path("stop"), raw=b"{not json", headers=JSON)[0], 400)
        self.assertEqual(self.call("POST", self.path("stop"), raw=b'{"job": "fix-a", "job": "fix-a"}', headers=JSON)[0], 400)
        self.assertEqual(self.runner.calls, [])

    def test_the_reason_is_sanitised_bounded_and_optional(self):
        hostile = "first\nsecond\x1b[31m red\u202e flipped\x00"
        self.assertEqual(self.stop({"job": "fix-a", "reason": hostile})[0], 200)
        reason = [item for item in self.runner.argvs[-1] if item.startswith("--reason=")][0]
        self.assertTrue(reason.isprintable(), reason)
        self.assertNotIn("\x1b", reason)
        self.assertNotIn("\n", reason)
        self.assertEqual(self.stop({"job": "fix-a", "reason": "x" * 200})[0], 200)
        self.assertEqual(len(self.runner.argvs[-1][-2]) - len("--reason="), 200)
        for empty in ("", "  \n\t "):
            self.assertEqual(self.stop({"job": "fix-a", "reason": empty})[0], 200)
            self.assertFalse([item for item in self.runner.argvs[-1] if item.startswith("--reason=")], empty)
        self.assertEqual(self.stop({"job": "fix-a"})[0], 200)
        self.assertFalse([item for item in self.runner.argvs[-1] if item.startswith("--reason=")])

    def test_a_refusal_of_the_cli_is_reported_not_hidden(self):
        self.runner.respond = lambda argv: CommandResult(1, b"", b"factory: job is not running (status pr_ready); only a running job can be stopped\n")
        code, payload, _ = self.stop()
        self.assertEqual(code, 422)
        self.assertIn("not running", payload["error"])
        self.assertNotIn("stop", payload)


class CrossOriginTests(StopCase):
    """The page sends the bearer token itself; these rules matter where a cookie session is in front (easy-auth mode)."""

    def test_a_json_content_type_is_required(self):
        for headers in ({}, {"Content-Type": "text/plain"}, {"Content-Type": "application/x-www-form-urlencoded"}, {"Content-Type": "multipart/form-data"}):
            with self.subTest(headers):
                self.assertEqual(self.stop(headers=headers)[0], 415)
        self.assertEqual(self.stop(headers={"Content-Type": "application/json; charset=utf-8"})[0], 200)
        self.assertEqual(self.stop(headers={"Content-Type": "Application/JSON"})[0], 200)

    def test_a_request_a_page_of_another_origin_sent_is_refused(self):
        host = f"127.0.0.1:{self.port}"
        for label, extra in (
            ("cross-site fetch metadata", {"Sec-Fetch-Site": "cross-site"}),
            ("same-site fetch metadata", {"Sec-Fetch-Site": "same-site"}),
            ("another origin", {"Origin": "https://evil.example"}),
            ("the opaque origin", {"Origin": "null"}),
            ("the same host on another port", {"Origin": "http://127.0.0.1:1"}),
            ("a lookalike host", {"Origin": f"http://{host}.evil.example"}),
        ):
            with self.subTest(label):
                code, payload, _ = self.stop(headers={**JSON, **extra})
                self.assertEqual((code, payload), (403, {"error": "cross-origin request refused"}))
        self.assertEqual(self.runner.calls, [])

    def test_the_pages_own_origin_and_clients_that_are_no_browser_pass(self):
        host = f"127.0.0.1:{self.port}"
        for label, extra in (
            ("no browser headers (curl, the CLI)", {}),
            ("same-origin fetch metadata", {"Sec-Fetch-Site": "same-origin", "Origin": f"http://{host}"}),
            ("typed address", {"Sec-Fetch-Site": "none"}),
            ("origin only", {"Origin": f"http://{host}"}),
        ):
            with self.subTest(label):
                self.assertEqual(self.stop(headers={**JSON, **extra})[0], 200)

    def test_the_other_actions_keep_their_behaviour(self):
        # Only the stop endpoint asks for the stricter rules.
        self.assertEqual(self.call("POST", self.path("drain"), principal=tm.OPERATOR)[0], 200)
        self.assertTrue(mgr.ACTIONS["stop"].same_origin_json)
        self.assertEqual([name for name, action in mgr.ACTIONS.items() if action.same_origin_json], ["stop"])


class DashboardShowsTheStopTests(StopCase):
    def test_the_job_detail_and_the_active_list_show_the_stop_with_the_reason_as_clipped_text(self):
        self.plan_run(
            status="running",
            worker_phase="build",
            phase="build",
            attempt=1,
            stop={"state": "stopping", "requested_at": "2026-11-09T00:00:00Z", "requested_by": "alice", "reason": CANARY, "phase": "build"},
        )
        detail = self.body("/repos/org/name/runs/r1/jobs/fix-a")
        self.assertEqual(detail["stop"]["state"], "stopping")
        self.assertEqual((detail["stop"]["requested_by"], detail["stop"]["reason"], detail["stop"]["phase"]), ("alice", CANARY, "build"))
        active = self.body("/dashboard")
        rows = [job for repo in active["repos"] for run in repo["runs"] for job in run["jobs"]]
        self.assertEqual([row["stop"]["state"] for row in rows], ["stopping"])

    def test_a_stale_record_is_not_shown_for_a_job_that_went_on(self):
        stale = {"state": "stopped", "requested_at": "2026-11-09T00:00:00Z", "requested_by": "alice", "reason": CANARY, "phase": "build"}
        for status in ("queued", "running", "interrupted"):
            with self.subTest(status):
                self.plan_run(status=status, attempt=1, worker_phase="build" if status == "running" else None, stop=stale)
                self.assertIsNone(self.body("/repos/org/name/runs/r1/jobs/fix-a")["stop"])
                active = self.body("/dashboard")
                rows = [job for repo in active["repos"] for run in repo["runs"] for job in run["jobs"]]
                self.assertEqual([row["stop"] for row in rows], [None])

    def test_a_job_without_a_stop_has_none_and_a_hostile_record_is_made_safe(self):
        self.assertIsNone(self.body("/repos/org/name/runs/r1/jobs/fix-a")["stop"])
        hostile = {"state": "stopped", "requested_at": "not a time", "requested_by": "a\nb" * 100, "reason": "r\x1b[0m" * 400, "phase": "../..", "terminated": "yes"}
        view = dashboard.stop_view(hostile, "stopped")
        self.assertEqual((view["state"], view["requested_at"], view["phase"], view["terminated"]), ("stopped", None, None, None))
        self.assertLessEqual(len(view["reason"]), 200)
        self.assertLessEqual(len(view["requested_by"]), 128)
        self.assertTrue(view["reason"].isprintable())
        self.assertIsNone(dashboard.stop_view("nope", "stopped"))
        self.assertIsNone(dashboard.stop_view({**hostile, "state": "x y"}, "stopped"))  # a state of no known kind is no stop

    def test_a_stop_is_shown_only_while_the_job_is_stopped_or_has_the_request_pending(self):
        stopped = {"state": "stopped", "requested_by": "alice", "reason": CANARY}
        stopping = {"state": "stopping", "requested_by": "alice", "reason": CANARY}
        for status in ("stopped", "running"):
            record = stopped if status == "stopped" else stopping
            self.assertEqual(dashboard.stop_view(record, status)["state"], record["state"])
        # A record that outlived its job's stop (resume, recovery) is not shown, whatever it says.
        for status in ("queued", "running", "interrupted", "pr_ready", "waiting_review", "failed", None):
            self.assertIsNone(dashboard.stop_view(stopped, status), status)
        for status in ("queued", "stopped", "interrupted", "waiting_review", None):
            self.assertIsNone(dashboard.stop_view(stopping, status), status)


class JwtStopTests(tm.JwtCase):
    def setUp(self):
        super().setUp()
        self.make_run()
        self.runner.respond = lambda argv: CommandResult(0, f"{json.dumps(ANSWER)}\n".encode(), b"")

    def test_a_token_is_needed_a_role_is_needed_and_a_viewer_cannot_stop(self):
        path = self.path("stop")
        self.assertEqual(self.jcall("POST", path, None, headers=JSON, body={"job": "fix-a"})[0], 401)
        self.assertEqual(self.jcall("POST", path, self.token(roles=[]), headers=JSON, body={"job": "fix-a"})[0], 403)
        self.assertEqual(self.jcall("POST", path, self.token(roles=["Factory.Viewer"]), headers=JSON, body={"job": "fix-a"})[0], 403)
        self.assertEqual(self.runner.calls, [])
        # An operator passes the role check; this run has no plan, so the job is not found (404), not a server error.
        self.assertEqual(self.jcall("POST", path, self.token(), headers=JSON, body={"job": "fix-a"})[0], 404)

    def test_an_operator_token_with_a_foreign_origin_is_refused(self):
        headers = {**JSON, "Origin": "https://evil.example"}
        self.assertEqual(self.jcall("POST", self.path("stop"), self.token(), headers=headers, body={"job": "fix-a"})[0], 403)


if __name__ == "__main__":
    unittest.main()

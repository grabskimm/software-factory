"""The orphan sweep must not delete the pod of a session that another process is running (fix of the second review).

The manager sweeps; the sessions run in other processes (the ``factory plan`` child records no job at all, a
``factory preflight`` probe records a pod under a job that is not "running", a supervisor that restarted has lost its
in-memory hold). ``podsweep.protect`` is process-local and cannot help there: the relay record in the spool, which the
supervisor writes before the launch and releases after it, can. Every test here runs the sweeper in a SEPARATE Python
process (a fresh ``podsweep`` with an empty ``protected()``) against a spool that a real ``PodBackend`` session is using
in this one, and the unit tests drive ``plan`` with a spool of records in every state.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from factory import podcontract as pc
from factory import podsweep as ps
from factory.agent import AgentError
from factory import sessionrecord as sr
from tests.test_podbackend import EPOCH, POD_UID, SUCCEEDED, Fresh, report_of
from tests.test_podsweep import GRACE, NOW, UID, Stub, SweepCase, alive, entry, listing, recorded, session

ROOT = Path(__file__).resolve().parent.parent

CHILD = r"""
import json, sys
from factory import podcontract as pc, podhandle, podsweep as ps

spool, session, now, owner, final = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4], sys.argv[5] == "final"
name = pc.pod_name(session)
pod = {"name": name, "uid": sys.argv[6], "state": "succeeded" if final else "running", "final": final,
       "terminating": False, "manager": None, "created": now - 10 * pc.LAUNCH_GRACE_SECONDS, "deadline": now + 100000}
entries = {}
if owner != "none":
    handle = podhandle.PodHandle(name, sys.argv[6], session, "launcher", "2026-01-01T00:00:00Z")
    entries[session] = ps.Entry("octo/widgets", "run-1", "job-a", owner, handle)
deleted = []
sweeper = ps.Sweeper(lambda: [pod], lambda n, u=None: deleted.append(n), spool, clock=lambda: now)
report = sweeper.run_once(lambda: ps.Recorded(entries, True), lambda repo, run: True)
print(json.dumps({"deleted": report.deleted, "released": report.released, "calls": deleted,
                  "protected": sorted(ps.protected()), "skipped": report.skipped}))
"""


def sweep_in_another_process(spool, found, now, owner="none", final=False):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    done = subprocess.run(
        [sys.executable, "-c", CHILD, os.fspath(spool), found, str(int(now)), owner, "final" if final else "live", POD_UID],
        env=env, capture_output=True, text=True, timeout=60,
    )
    if done.returncode:
        raise AssertionError(done.stderr)
    return json.loads(done.stdout)


class LiveSessionTests(Fresh):
    """A session that a ``PodBackend`` is running here, swept from a process that knows nothing of it."""

    def sweep(self, **options):
        return sweep_in_another_process(self.spool, self.session, EPOCH + self.clock(), **options)

    def run_with_sweeps(self, step, **backend_options):
        """Run a session; ``step`` sweeps at tick 2 (streaming). Returns whatever ``step`` collected."""
        seen = []
        self.happy()
        self.at(2, lambda: seen.append(step()))
        backend = self.make_backend(**backend_options) if backend_options else self.backend
        self.run_session(backend)
        return seen

    def assertHeld(self, report):
        self.assertEqual((report["deleted"], report["calls"], report["released"]), ({}, [], 0))
        self.assertEqual(report["protected"], [], "the sweeping process holds nothing of its own: the record alone protects the pod")
        self.assertFalse(sr.is_marked(self.spool, self.session, sr.MARKER_REVOKE), "a live session is not even revoked")

    def test_a_run_session_not_yet_recorded_in_any_state_is_not_swept(self):
        (report,) = self.run_with_sweeps(lambda: self.sweep(owner="none"))
        self.assertHeld(report)
        self.assertTrue(sr.is_marked(self.spool, self.session, sr.MARKER_RELEASED), "and the session still ended normally")

    def test_a_plan_session_which_no_job_records_is_not_swept(self):
        # The planner's job id is not a job in any state.json: the state lists no pod at all (the reviewer's scratch case).
        (report,) = self.run_with_sweeps(lambda: self.sweep(owner="none"), job_id="planner", attempt=0)
        self.assertHeld(report)

    def test_a_preflight_session_whose_job_is_not_running_is_not_swept_as_a_stale_record(self):
        for status in ("failed", "queued", "pending"):
            with self.subTest(status=status):
                self.setUp()
                (report,) = self.run_with_sweeps(lambda: self.sweep(owner=status))
                self.assertHeld(report)

    def test_a_pod_that_is_already_over_but_whose_session_is_live_is_not_swept_as_finished(self):
        (report,) = self.run_with_sweeps(lambda: self.sweep(owner="none", final=True))
        self.assertHeld(report)

    def test_a_supervisor_that_restarted_holds_no_protection_and_the_record_still_protects_the_pod(self):
        ps.reset()  # what a restart does to the in-memory hold; the sweep below is in another process anyway
        (report,) = self.run_with_sweeps(lambda: self.sweep(owner="none"))
        self.assertHeld(report)

    def test_once_the_session_is_released_the_same_pod_is_swept_and_a_leftover_is_not_protected(self):
        self.happy()
        self.run_session()
        self.assertTrue(sr.is_marked(self.spool, self.session, sr.MARKER_RELEASED))
        report = self.sweep(owner="none")
        self.assertEqual(report["deleted"], {"unrecorded": 1})
        self.assertEqual(report["calls"], [pc.pod_name(self.session)])
        report = self.sweep(owner="failed")
        self.assertEqual(report["deleted"], {"stale_record": 1})

    def test_a_session_whose_deadline_has_passed_and_whose_owner_is_gone_is_swept(self):
        seen = []
        self.happy()
        self.at(2, lambda: seen.append(sweep_in_another_process(self.spool, self.session, EPOCH + self.clock() + 10**5)))
        with self.assertRaises(AgentError):  # the sweep revoked the session, as it is meant to for an orphan
            self.run_session()
        (report,) = seen
        self.assertEqual(report["deleted"], {"unrecorded": 1}, "an expired record holds nothing")
        self.assertTrue(sr.is_marked(self.spool, self.session, sr.MARKER_REVOKE))

    def test_a_session_revoked_by_its_own_supervisor_is_swept(self):
        seen = []

        def revoke_and_sweep():
            sr.mark(self.spool, self.session, sr.MARKER_REVOKE)
            seen.append(self.sweep(owner="none"))

        self.happy()
        self.at(2, revoke_and_sweep)
        with self.assertRaises(AgentError):
            self.run_session()
        self.assertEqual(seen[0]["deleted"], {"unrecorded": 1})


class PlanTests(SweepCase):
    """``plan`` over a spool whose records are in every state (the sweeper reads them with ``read_sessions``)."""

    def swept(self, owner=None, *, pod=None, **session_options):
        self.make_session(1, **session_options)
        stub = Stub([pod or listing(1)], spool=self.spool)
        owners = recorded(*([owner] if owner else []))
        return self.sweep(stub, owners), stub

    def test_a_live_record_holds_an_unrecorded_pod(self):
        report, stub = self.swept(state="streaming")
        self.assertEqual((report.deleted, stub.deleted, report.skipped), ({}, [], 1))

    def test_a_live_record_holds_a_stale_record_pod_and_a_finished_one(self):
        report, stub = self.swept(entry(1, status="failed"), state="fetched")
        self.assertEqual((report.deleted, stub.deleted), ({}, []))
        report, stub = self.swept(pod=listing(1, state="succeeded", final=True), state="issued")
        self.assertEqual((report.deleted, stub.deleted), ({}, []))

    def test_every_way_a_record_ends_releases_the_hold(self):
        cases = {
            "finished": dict(state="finished"),
            "expired": dict(state="expired"),
            "revoked_state": dict(state="revoked"),
            "revoke_marker": dict(state="streaming", revoke=True),
            "released": dict(state="streaming", released=True),
            "deadline": dict(state="streaming", deadline=NOW),
        }
        for name, options in cases.items():
            with self.subTest(name):
                self.setUp()
                report, stub = self.swept(**options)
                self.assertEqual(report.deleted, {"unrecorded": 1})

    def test_a_record_that_cannot_be_read_holds_the_pod_but_a_missing_one_does_not(self):
        found = self.make_session(1)
        (self.spool / found / "record.json").write_text("{not json")
        stub = Stub([listing(1)], spool=self.spool)
        self.assertEqual((self.sweep(stub, recorded()).deleted, stub.deleted), ({}, []))
        (self.spool / found / "record.json").unlink()  # the spool was replaced: no call of the pod can succeed
        stub = Stub([listing(1)], spool=self.spool)
        self.assertEqual(self.sweep(stub, recorded()).deleted, {"unrecorded": 1})

    def test_an_overdue_pod_is_deleted_whatever_its_record_says(self):
        report, stub = self.swept(pod=listing(1, deadline=NOW - 10 * GRACE), state="streaming")
        self.assertEqual(report.deleted, {"overdue": 1})

    def test_a_running_job_whose_supervisor_is_gone_is_still_swept_by_the_old_rule(self):
        self.make_session(1, state="streaming")
        stub = Stub([listing(1)], spool=self.spool)
        sweeper = self.sweeper(stub)
        owners = recorded(entry(1, status="running"))
        sweeper.run_once(lambda: owners, lambda repo, run: False)
        self.now += pc.ORPHAN_SILENCE_SECONDS
        report = sweeper.run_once(lambda: owners, lambda repo, run: False)
        self.assertEqual(report.deleted, {"supervisor_gone": 1})

    def test_a_young_pod_is_still_left_alone_whatever_its_record_says(self):
        report, stub = self.swept(pod=listing(1, created=NOW - 5), state="finished")
        self.assertEqual((report.deleted, stub.deleted), ({}, []))

    def test_without_a_spool_nothing_changes(self):
        stub = Stub([listing(1)])
        self.assertEqual(self.sweep(stub, recorded(), spool=None).deleted, {"unrecorded": 1})

    def test_the_session_listing_says_which_records_are_live(self):
        self.make_session(1, state="streaming")
        self.make_session(2, state="finished")
        self.make_session(3, state="streaming", deadline=NOW)
        live = {item.session: item.live for item in ps.read_sessions(self.spool, NOW)}
        self.assertEqual(live, {session(1): True, session(2): False, session(3): False})


if __name__ == "__main__":
    unittest.main()

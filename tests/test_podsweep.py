"""The orphan sweep and the pod census (src/factory/podsweep.py, slice 20 of the pod backend, docs/pod-backend.md).

CONTRACT-TESTED AGAINST ASSUMED SHAPES: the decision is driven with hand-built launcher listings, state files in
temporary directories and a real spool directory; one class runs the real launcher over a real unix socket against the
in-memory fake of the Kubernetes API (tests/fake_kube.py) on ONE fake clock. No cluster, no network beyond 127.0.0.1,
no sleeping. Every rule is pinned by a test that fails when the rule is removed:

* a pod is deleted only when nobody owns it: an unreadable owner (a state file, a pod record, a cut run listing, a
  failing provider) is "do not delete", except for a pod past its own deadline;
* the launch grace, a launch in flight (``protect``) and an age that cannot be read each keep an unrecorded pod;
* a live supervisor keeps a running job's pod; a gone one does so only after ``ORPHAN_SILENCE_SECONDS`` of THIS
  sweeper's own observation, and a supervisor that comes back resets the count;
* a pod that is terminating is left alone; a finished pod a running job may still collect is left alone;
* the pods are listed BEFORE the owners are read; the relay record is revoked BEFORE the pod is deleted;
* a relay record is released only when it is over, quiet, unowned, not listed and the listing is complete;
* the census never reports "nothing running" for a launcher that could not be asked.
"""

import ast
import contextlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from factory import launcher, launcher_client, podhandle, podsweep as ps
from factory import podcontract as pc
from factory import sessionrecord as sr
from tests import fake_kube
from tests.fake_kube import Step
from tests.test_launcher import ServerHarness, TOKEN, launch_request, pod_name

NOW = 1_800_000_000
GRACE = pc.LAUNCH_GRACE_SECONDS
SILENCE = pc.ORPHAN_SILENCE_SECONDS
UID = "11111111-1111-4111-8111-111111111111"


def session(n):
    return fake_kube.session_id(n)


def listing(n=1, *, state="running", final=False, terminating=False, created=NOW - 10 * GRACE, deadline=NOW + 3600, manager="abcdef012345", uid=UID):
    """One entry of the launcher's ``list`` answer."""
    return {
        "name": pc.pod_name(session(n)),
        "uid": uid,
        "state": state,
        "final": final,
        "terminating": terminating,
        "manager": manager,
        "created": created,
        "deadline": deadline,
    }


def handle(n=1):
    return podhandle.PodHandle(pc.pod_name(session(n)), UID, session(n), "launcher", "2026-01-01T00:00:00Z")


def entry(n=1, status="running", repo="org/name", run="r1"):
    return ps.Entry(repo, run, f"job-{n}", status, handle(n))


def recorded(*entries, complete=True):
    return ps.Recorded({item.handle.session: item for item in entries}, complete)


def alive(_repo, _run):
    return True


def gone(_repo, _run):
    return False


class Stub:
    """A launcher client: the listing it answers, and the deletes it was asked for (in order, with the spool's marker at the time)."""

    def __init__(self, pods=(), spool=None, fail=()):
        self.pods = list(pods)
        self.spool = spool
        self.fail = set(fail)
        self.listed = 0
        self.deleted = []
        self.error = None

    def list_pods(self):
        self.listed += 1
        if self.error is not None:
            raise self.error
        return self.pods

    def delete_pod(self, name, uid=None):
        revoked = None
        if self.spool is not None:
            found = name[len(pc.POD_NAME_PREFIX) :]
            with contextlib.suppress(sr.RecordError):
                revoked = sr.is_marked(self.spool, found, sr.MARKER_REVOKE)
        self.deleted.append((name, uid, revoked))
        if name in self.fail:
            raise launcher_client.Unavailable()
        return True


class SweepCase(unittest.TestCase):
    def setUp(self):
        ps.reset()
        self.addCleanup(ps.reset)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.spool = self.dir / "spool"
        self.spool.mkdir()
        self.now = NOW

    def clock(self):
        return self.now

    def sweeper(self, stub, **options):
        return ps.Sweeper(stub.list_pods, stub.delete_pod, options.pop("spool", self.spool), clock=self.clock, **options)

    def sweep(self, stub, owners, supervisor_live=alive, **options):
        return self.sweeper(stub, **options).run_once(lambda: owners, supervisor_live)

    def make_session(self, n=1, *, state="finished", deadline=NOW + 3600, age=10 * GRACE, released=False, revoke=False):
        found = session(n)
        sr.write(self.spool, sr.build(found, "t" * 43, deadline, pc.repo_key("octo/widgets"), "cwd", "abcdef012345"))
        with sr.session_dir(self.spool, found) as fd:
            document = sr.new_state()
            document.update(state=state)
            sr.write_state_at(fd, document)
            if released:
                sr.mark_at(fd, sr.MARKER_RELEASED)
            if revoke:
                sr.mark_at(fd, sr.MARKER_REVOKE)
        moment = NOW - age
        os.utime(self.spool / found, (moment, moment))
        return found

    def names(self, stub):
        return [name for name, _uid, _revoked in stub.deleted]


class PodDecisionTests(SweepCase):
    def test_an_unrecorded_pod_is_deleted_once_it_is_older_than_the_launch_grace(self):
        young = Stub([listing(1, created=NOW - GRACE + 1)])
        old = Stub([listing(1, created=NOW - GRACE)])
        self.assertEqual(self.sweep(young, recorded()).deleted, {})
        report = self.sweep(old, recorded())
        self.assertEqual(report.deleted, {"unrecorded": 1})
        self.assertEqual(old.deleted, [(pc.pod_name(session(1)), UID, None)], "the delete names the pod and its uid")

    def test_a_pod_whose_age_cannot_be_read_is_never_judged_by_age(self):
        stub = Stub([listing(1, created=None), listing(2, created=0), listing(3, created="yesterday"), listing(4, created=True)])
        report = self.sweep(stub, recorded())
        self.assertEqual((report.deleted, report.skipped, stub.deleted), ({}, 4, []))

    def test_a_launch_in_flight_is_held_out_of_the_sweep_however_old_the_pod_is(self):
        stub = Stub([listing(1)])
        with ps.protect(session(1)):
            self.assertEqual(self.sweep(stub, recorded()).deleted, {})
            self.assertEqual(ps.protected(), frozenset({session(1)}))
        self.assertEqual(ps.protected(), frozenset())
        self.assertEqual(self.sweep(stub, recorded()).deleted, {"unrecorded": 1})

    def test_protect_releases_on_an_exception_and_refuses_a_bad_id(self):
        with self.assertRaises(RuntimeError), ps.protect(session(1)):
            raise RuntimeError("x")
        self.assertEqual(ps.protected(), frozenset())
        with self.assertRaises(ValueError):
            ps.protect("not-a-session").__enter__()

    def test_a_running_job_with_a_live_supervisor_keeps_its_pod_however_old(self):
        stub = Stub([listing(1, created=NOW - 100_000)])
        report = self.sweep(stub, recorded(entry(1)), alive)
        self.assertEqual((report.deleted, stub.deleted), ({}, []))

    def test_a_pod_recorded_by_a_job_that_is_not_running_is_a_stale_record_once_past_the_launch_grace(self):
        for status in ("failed", "interrupted", "queued", "merged"):
            with self.subTest(status=status):
                stub = Stub([listing(1, created=NOW - GRACE)])
                report = self.sweep(stub, recorded(entry(1, status=status)), alive)
                self.assertEqual(report.deleted, {"stale_record": 1})

    def test_a_stale_record_gets_the_launch_grace_so_a_session_no_supervisor_runs_is_not_swept_mid_probe(self):
        # ``factory preflight`` records a pod under a job that is not "running", with no supervisor and no hold.
        for status in ("failed", "queued", "blocked"):
            with self.subTest(status=status):
                stub = Stub([listing(1, created=NOW - GRACE + 1)])
                report = self.sweep(stub, recorded(entry(1, status=status)), alive)
                self.assertEqual((report.deleted, stub.deleted, report.skipped), ({}, [], 1))

    def test_a_stale_record_whose_pod_age_cannot_be_read_is_never_judged_by_age(self):
        stub = Stub([listing(1, created=None), listing(2, created=0)])
        report = self.sweep(stub, recorded(entry(1, status="failed"), entry(2, status="failed")), alive)
        self.assertEqual((report.deleted, report.skipped, stub.deleted), ({}, 2, []))

    def test_a_stale_record_is_held_out_of_the_sweep_by_a_launch_in_flight_however_old_the_pod_is(self):
        stub = Stub([listing(1)])
        with ps.protect(session(1)):
            self.assertEqual(self.sweep(stub, recorded(entry(1, status="failed")), alive).deleted, {})
        self.assertEqual(self.sweep(stub, recorded(entry(1, status="failed")), alive).deleted, {"stale_record": 1})

    def test_a_gone_supervisor_costs_the_pod_only_after_the_silence_window_of_the_sweepers_own_clock(self):
        stub = Stub([listing(1)])
        sweeper = self.sweeper(stub)
        owners = recorded(entry(1))
        self.assertEqual(sweeper.run_once(lambda: owners, gone).deleted, {})
        self.now += SILENCE - 1
        self.assertEqual(sweeper.run_once(lambda: owners, gone).deleted, {})
        self.now += 1
        report = sweeper.run_once(lambda: owners, gone)
        self.assertEqual((report.deleted, self.names(stub)), ({"supervisor_gone": 1}, [pc.pod_name(session(1))]))

    def test_a_supervisor_that_comes_back_starts_the_count_again(self):
        stub = Stub([listing(1)])
        sweeper = self.sweeper(stub)
        owners = recorded(entry(1))
        sweeper.run_once(lambda: owners, gone)
        self.now += SILENCE - 1
        sweeper.run_once(lambda: owners, alive)  # back
        self.now += 10
        self.assertEqual(sweeper.run_once(lambda: owners, gone).deleted, {})  # gone again: counted from here
        self.now += SILENCE - 1
        self.assertEqual(sweeper.run_once(lambda: owners, gone).deleted, {})
        self.now += 1
        self.assertEqual(sweeper.run_once(lambda: owners, gone).deleted, {"supervisor_gone": 1})

    def test_a_supervisor_check_that_raises_counts_as_live(self):
        def broken(_repo, _run):
            raise OSError("cannot read")

        sweeper = self.sweeper(Stub([listing(1)]))
        owners = recorded(entry(1))
        for _ in range(3):
            self.assertEqual(sweeper.run_once(lambda: owners, broken).deleted, {})
            self.now += SILENCE
        self.assertEqual(sweeper._gone_since, {})

    def test_the_count_is_per_run_not_per_sweeper(self):
        stub = Stub([listing(1), listing(2)])
        sweeper = self.sweeper(stub)
        owners = recorded(entry(1, run="r1"), entry(2, run="r2"))
        sweeper.run_once(lambda: owners, lambda repo, run: run == "r2")
        self.now += SILENCE
        report = sweeper.run_once(lambda: owners, lambda repo, run: run == "r2")
        self.assertEqual(self.names(stub), [pc.pod_name(session(1))], "only the run without a supervisor")
        self.assertEqual(report.deleted, {"supervisor_gone": 1})

    def test_a_pod_that_is_over_and_unowned_is_finished_garbage(self):
        stub = Stub([listing(1, state="succeeded", final=True)])
        self.assertEqual(self.sweep(stub, recorded()).deleted, {"finished": 1})

    def test_a_pod_that_is_over_but_belongs_to_a_running_job_is_left_for_its_collector(self):
        stub = Stub([listing(1, state="succeeded", final=True)])
        self.assertEqual(self.sweep(stub, recorded(entry(1)), alive).deleted, {})

    def test_a_pod_that_is_terminating_is_left_alone_whatever_else_is_true(self):
        stub = Stub([listing(1, state="evicted", final=True, terminating=True, deadline=NOW - 10 * GRACE)])
        report = self.sweep(stub, recorded(entry(1, status="failed")))
        self.assertEqual((report.deleted, report.terminating, stub.deleted), ({}, 1, []))

    def test_a_listing_that_does_not_say_whether_a_final_pod_is_terminating_is_read_as_terminating(self):
        old = listing(1, state="evicted", final=True)
        del old["terminating"]
        self.assertTrue(ps.listed(old).terminating)
        self.assertFalse(ps.listed(old).over)
        live = listing(2)
        del live["terminating"]
        self.assertFalse(ps.listed(live).terminating)

    def test_a_pod_past_its_deadline_and_the_graces_is_deleted_even_when_a_live_job_records_it(self):
        limit = pc.TERMINATION_GRACE_SECONDS + GRACE
        inside = Stub([listing(1, deadline=NOW - limit + 1)])
        past = Stub([listing(1, deadline=NOW - limit)])
        self.assertEqual(self.sweep(inside, recorded(entry(1)), alive).deleted, {})
        self.assertEqual(self.sweep(past, recorded(entry(1)), alive).deleted, {"overdue": 1})

    def test_a_pod_past_its_deadline_is_deleted_even_when_the_owners_could_not_be_read(self):
        stub = Stub([listing(1, deadline=NOW - 10 * GRACE), listing(2)])
        report = self.sweep(stub, recorded(complete=False))
        self.assertEqual((report.deleted, self.names(stub)), ({"overdue": 1}, [pc.pod_name(session(1))]))

    def test_entries_that_are_not_worker_pods_are_ignored(self):
        junk = [None, "x", {}, {**listing(1), "name": "kube-system-pod"}, {**listing(2), "state": "dancing"}, {**listing(3), "final": "no"}]
        stub = Stub(junk)
        self.assertEqual((self.sweep(stub, recorded()).deleted, stub.deleted), ({}, []))

    def test_the_manager_label_alone_decides_nothing(self):
        a = Stub([listing(1, manager="000000000000")])
        b = Stub([listing(1, manager=None)])
        self.assertEqual(self.sweep(a, recorded(entry(1)), alive).deleted, {})
        self.assertEqual(self.sweep(b, recorded(entry(1)), alive).deleted, {})


class UnreadableOwnerTests(SweepCase):
    def test_an_incomplete_answer_deletes_no_unowned_pod_and_releases_no_record(self):
        self.make_session(2)
        stub = Stub([listing(1), listing(3, state="succeeded", final=True)])
        report = self.sweep(stub, recorded(complete=False))
        self.assertEqual((report.deleted, report.released, report.incomplete, stub.deleted), ({}, 0, True, []))
        self.assertEqual(report.skipped, 2)

    def test_an_incomplete_answer_still_leaves_a_pod_a_job_stopped_recording_alone(self):
        stub = Stub([listing(1)])
        self.assertEqual(self.sweep(stub, recorded(entry(1, status="failed"), complete=False)).deleted, {})

    def test_a_provider_that_raises_is_an_incomplete_answer(self):
        stub = Stub([listing(1)])

        def boom():
            raise RuntimeError("cannot scan")

        report = self.sweeper(stub).run_once(boom, alive)
        self.assertEqual((report.incomplete, report.deleted, stub.deleted), (True, {}, []))

    def test_a_launcher_that_cannot_be_asked_does_nothing_and_says_so(self):
        stub = Stub([listing(1)])
        stub.error = launcher_client.Unavailable()
        called = []
        report = self.sweeper(stub).run_once(lambda: called.append(1) or recorded(), alive)
        self.assertEqual((report.unreadable, report.deleted, stub.deleted, called), (True, {}, [], []))
        self.assertEqual(report.line(), "pod sweep deleted=0 released=0 failed=0 incomplete=0 unreadable=1")

    def test_a_listing_that_is_not_a_list_is_no_pods_not_a_crash(self):
        stub = Stub()
        stub.pods = "nonsense"
        report = self.sweep(stub, recorded())
        self.assertEqual((report.deleted, report.unreadable), ({}, False))


class OrderTests(SweepCase):
    def test_the_pods_are_listed_before_the_owners_are_read(self):
        order = []
        stub = Stub([listing(1)])
        original = stub.list_pods
        stub.list_pods = lambda: order.append("list") or original()
        self.sweeper(stub).run_once(lambda: order.append("recorded") or recorded(), alive)
        self.assertEqual(order, ["list", "recorded"])

    def test_the_relay_record_is_revoked_before_the_pod_is_deleted(self):
        self.make_session(1, state="streaming", deadline=NOW - 1)  # a record whose deadline has passed holds nothing
        stub = Stub([listing(1)], spool=self.spool)
        self.sweep(stub, recorded())
        self.assertEqual(stub.deleted, [(pc.pod_name(session(1)), UID, True)])
        self.assertTrue(sr.is_marked(self.spool, session(1), sr.MARKER_REVOKE))

    def test_a_pod_whose_record_is_gone_is_still_deleted(self):
        stub = Stub([listing(1)], spool=self.spool)
        self.assertEqual(self.sweep(stub, recorded()).deleted, {"unrecorded": 1})
        self.assertEqual(stub.deleted[0][2], None)

    def test_a_failed_delete_is_counted_not_reported_as_done_and_the_others_go_on(self):
        stub = Stub([listing(1), listing(2)], fail={pc.pod_name(session(1))})
        report = self.sweep(stub, recorded())
        self.assertEqual((report.deleted, report.failed), ({"unrecorded": 1}, 1))
        self.assertEqual(len(stub.deleted), 2)

    def test_without_a_spool_pods_are_still_swept_and_no_record_is_touched(self):
        stub = Stub([listing(1)])
        report = self.sweep(stub, recorded(), spool=None)
        self.assertEqual(report.deleted, {"unrecorded": 1})


class SessionSweepTests(SweepCase):
    def test_an_unowned_ended_quiet_record_with_no_pod_is_revoked_released_and_its_secret_asked_for(self):
        found = self.make_session(1, state="finished")
        stub = Stub(spool=self.spool)
        report = self.sweep(stub, recorded())
        self.assertEqual(report.released, 1)
        self.assertEqual(stub.deleted, [(pc.pod_name(found), None, True)], "the derived name, no uid: the launcher deletes the Secret too")
        self.assertTrue(sr.is_marked(self.spool, found, sr.MARKER_RELEASED))
        self.assertTrue(sr.is_marked(self.spool, found, sr.MARKER_REVOKE))

    def test_each_condition_keeps_a_record(self):
        def owned(stub):
            return recorded(entry(1))

        def listed_pod(stub):
            stub.pods = [listing(1)]
            return recorded(entry(1, status="running"))

        def not_over(stub):
            self.make_session(1, state="streaming", deadline=NOW + 3600)
            return recorded()

        def not_quiet(stub):
            self.make_session(1, state="finished", age=GRACE - 1)
            return recorded()

        def already_released(stub):
            self.make_session(1, state="finished", released=True)
            return recorded()

        def held(stub):
            return recorded()

        def owners_unreadable(stub):
            return recorded(complete=False)

        cases = {
            "a job records it": (owned, "finished"),
            "its pod is listed": (listed_pod, "finished"),
            "its relay session is not over": (not_over, None),
            "it changed less than the launch grace ago": (not_quiet, None),
            "it is released already": (already_released, None),
            "a launch of this process holds it": (held, "finished"),
            "its owners could not be read": (owners_unreadable, "finished"),
        }
        for label, (setup, state) in cases.items():
            with self.subTest(label):
                for leftover in os.listdir(self.spool):
                    shutil.rmtree(self.spool / leftover)
                if state is not None:
                    self.make_session(1, state=state)
                stub = Stub(spool=self.spool)
                owners = setup(stub)
                with contextlib.ExitStack() as stack:
                    if label.startswith("a launch"):
                        stack.enter_context(ps.protect(session(1)))
                    report = self.sweep(stub, owners, alive)
                self.assertEqual(report.released, 0)
                self.assertEqual([item for item in stub.deleted if item[1] is None], [], "no derived-name delete either")
                self.assertFalse(
                    sr.is_marked(self.spool, session(1), sr.MARKER_RELEASED) and label != "it is released already", label
                )

    def test_a_listed_pod_of_the_session_keeps_the_record_even_when_the_pod_is_not_judged_at_all(self):
        # A terminating pod is skipped by the pod pass, so only the "no pod of that name is listed" condition holds
        # the record of its session: the pod may still be using it for its termination grace.
        for pod in (listing(1, terminating=True, final=True, state="failed_exit"), listing(1, terminating=True)):
            with self.subTest(pod=pod):
                for leftover in os.listdir(self.spool):
                    shutil.rmtree(self.spool / leftover)
                self.make_session(1, state="finished")
                stub = Stub([pod], spool=self.spool)
                report = self.sweep(stub, recorded(), alive)
                self.assertEqual((report.released, report.terminating), (0, 1))
                self.assertEqual(stub.deleted, [])
                self.assertFalse(sr.is_marked(self.spool, session(1), sr.MARKER_RELEASED))
        stub = Stub([], spool=self.spool)
        self.assertEqual(self.sweep(stub, recorded(), alive).released, 1, "and once the pod is gone the record goes")

    def test_a_record_past_its_deadline_is_over_even_if_the_relay_never_stored_expired(self):
        found = self.make_session(1, state="streaming", deadline=NOW - 1)
        stub = Stub(spool=self.spool)
        self.assertEqual(self.sweep(stub, recorded()).released, 1)
        self.assertTrue(sr.is_marked(self.spool, found, sr.MARKER_RELEASED))

    def test_a_directory_with_no_record_is_left_to_the_relays_retention(self):
        found = session(1)
        (self.spool / found).mkdir()
        os.utime(self.spool / found, (NOW - 10 * GRACE, NOW - 10 * GRACE))
        stub = Stub(spool=self.spool)
        report = self.sweep(stub, recorded())
        self.assertEqual((report.released, report.failed, stub.deleted), (0, 0, []), "the relay removes it itself once its retention has passed")

    def test_a_record_that_cannot_be_read_is_never_released(self):
        found = self.make_session(1)
        (self.spool / found / "record.json").write_text("{not json")
        stub = Stub(spool=self.spool)
        self.assertEqual((self.sweep(stub, recorded()).released, stub.deleted), (0, []))

    def test_a_listing_as_long_as_the_launchers_limit_may_be_cut_so_absence_proves_nothing(self):
        self.make_session(1)
        full = [listing(100 + n) for n in range(ps.LIST_LIMIT)]
        stub = Stub(full, spool=self.spool)
        report = self.sweep(stub, recorded(*[entry(100 + n) for n in range(ps.LIST_LIMIT)]), alive)
        self.assertEqual((report.released, os.path.exists(self.spool / session(1) / sr.MARKER_RELEASED)), (0, False))

    def test_a_spool_that_cannot_be_read_releases_nothing_and_holds_every_unowned_pod_but_an_overdue_one(self):
        # Any record may be live: nothing without a running owner is deleted on a guess (a pod past its own deadline
        # needs no owner and is still deleted).
        stub = Stub([listing(1), listing(2, final=True, terminating=False), listing(3, deadline=NOW - 10 * GRACE)])
        report = self.sweep(stub, recorded(), spool=self.dir / "absent")
        self.assertEqual((report.released, report.deleted), (0, {"overdue": 1}))
        self.assertEqual([name for name, _, _ in stub.deleted], [pc.pod_name(session(3))])

    def test_names_in_the_spool_that_are_not_session_directories_are_ignored(self):
        (self.spool / "not-a-session").mkdir()
        (self.spool / session(2)).write_text("a file")
        (self.spool / session(3)).symlink_to(self.dir)
        self.assertEqual([s.session for s in ps.read_sessions(self.spool, NOW)], [])

    def test_a_failed_derived_delete_keeps_the_record_unreleased(self):
        found = self.make_session(1)
        stub = Stub(spool=self.spool, fail={pc.pod_name(found)})
        report = self.sweep(stub, recorded())
        self.assertEqual((report.released, report.failed), (0, 1))
        self.assertFalse(sr.is_marked(self.spool, found, sr.MARKER_RELEASED))


class ReadRecordedTests(SweepCase):
    def state(self, name, document, raw=None):
        path = self.dir / f"{name}.json"
        path.write_text(raw if raw is not None else json.dumps(document))
        return path

    def jobs(self, **jobs):
        return {"version": 1, "jobs": jobs}

    def test_the_jobs_that_record_a_pod_are_found_with_their_run_and_status(self):
        path = self.state("a", self.jobs(**{"job-1": {"status": "running", "pod": handle(1).record()}, "job-2": {"status": "queued"}}))
        found = ps.read_recorded([("org/name", "r1", path)])
        self.assertTrue(found.complete)
        self.assertEqual(found.entries, {session(1): ps.Entry("org/name", "r1", "job-1", "running", handle(1))})

    def test_a_run_without_a_state_file_has_no_jobs_and_is_fine(self):
        found = ps.read_recorded([("org/name", "r1", self.dir / "absent.json")])
        self.assertEqual((found.entries, found.complete), ({}, True))

    def test_anything_unreadable_makes_the_answer_incomplete(self):
        bad_record = self.jobs(**{"job-1": {"status": "running", "pod": {"name": "x"}}})
        cases = {
            "not json": self.state("a", None, raw="{nope"),
            "no jobs": self.state("b", {"version": 1}),
            "jobs not a mapping": self.state("c", {"jobs": []}),
            "a pod record that does not validate": self.state("d", bad_record),
            "a document that is not an object": self.state("e", [1]),
            "a directory": self.dir,
        }
        for label, path in cases.items():
            with self.subTest(label):
                self.assertFalse(ps.read_recorded([("org/name", "r1", path)]).complete)

    def test_a_link_in_the_place_of_the_state_file_is_unreadable_not_followed(self):
        target = self.state("target", self.jobs())
        link = self.dir / "link.json"
        link.symlink_to(target)
        self.assertFalse(ps.read_recorded([("org/name", "r1", link)]).complete)

    def test_a_file_over_the_bound_is_unreadable(self):
        path = self.state("big", None, raw=" " * 10)
        original = ps.MAX_STATE_BYTES
        ps.MAX_STATE_BYTES = 5
        self.addCleanup(setattr, ps, "MAX_STATE_BYTES", original)
        self.assertFalse(ps.read_recorded([("org/name", "r1", path)]).complete)

    def test_one_unreadable_file_makes_the_whole_answer_incomplete_but_keeps_the_rest(self):
        good = self.state("g", self.jobs(**{"job-1": {"status": "running", "pod": handle(1).record()}}))
        bad = self.state("b", None, raw="{")
        found = ps.read_recorded([("org/name", "r1", good), ("org/name", "r2", bad)])
        self.assertEqual((found.complete, list(found.entries)), (False, [session(1)]))

    def test_a_cut_run_listing_stays_incomplete(self):
        self.assertFalse(ps.read_recorded([], complete=False).complete)

    def test_a_running_job_wins_when_two_jobs_record_the_same_session(self):
        record = handle(1).record()
        first = self.state("a", self.jobs(**{"job-1": {"status": "failed", "pod": record}}))
        second = self.state("b", self.jobs(**{"job-9": {"status": "running", "pod": record}}))
        for order in ((first, second), (second, first)):
            found = ps.read_recorded([("org/name", f"r{i}", path) for i, path in enumerate(order)])
            self.assertEqual(found.entries[session(1)].status, "running")

    def test_a_status_that_is_not_a_plain_word_is_unknown_so_the_answer_is_incomplete_and_nothing_is_deleted(self):
        for status in ("running; DROP", "needs-human", "Running", "x" * 33, "", None, 5, ["running"]):
            with self.subTest(status=status):
                path = self.state("a", self.jobs(**{"job-1": {"status": status, "pod": handle(1).record()}}))
                found = ps.read_recorded([("org/name", "r1", path)])
                self.assertFalse(found.complete)
                stub = Stub([listing(1)])
                report = self.sweep(stub, found, alive)
                self.assertEqual((report.deleted, stub.deleted, report.incomplete), ({}, [], True))

    def test_a_job_without_a_status_is_unknown_not_stopped(self):
        path = self.state("a", self.jobs(**{"job-1": {"pod": handle(1).record()}}))
        self.assertFalse(ps.read_recorded([("org/name", "r1", path)]).complete)

    def test_a_plain_word_status_of_the_longest_length_is_read(self):
        path = self.state("a", self.jobs(**{"job-1": {"status": "a_" * 16, "pod": handle(1).record()}}))
        found = ps.read_recorded([("org/name", "r1", path)])
        self.assertEqual((found.complete, found.entries[session(1)].status), (True, "a_" * 16))


class CensusTests(unittest.TestCase):
    def census(self, answer):
        return ps.census(lambda: answer)

    def test_live_and_final_are_counted_and_a_terminating_pod_still_counts_as_live(self):
        pods = [listing(1), listing(2, state="pending"), listing(3, state="succeeded", final=True), listing(4, state="evicted", final=True, terminating=True)]
        self.assertEqual(self.census(pods), {"live": 3, "final": 1, "unknown": False})

    def test_nothing_listed_is_a_known_zero(self):
        self.assertEqual(self.census([]), {"live": 0, "final": 0, "unknown": False})

    def test_a_launcher_that_cannot_be_asked_is_unknown_never_zero(self):
        def refuse():
            raise launcher_client.Unavailable()

        self.assertEqual(ps.census(refuse), {"live": 0, "final": 0, "unknown": True})
        for answer in (None, "x", {"pods": []}, 3):
            with self.subTest(answer=answer):
                self.assertTrue(self.census(answer)["unknown"])

    def test_an_entry_that_is_not_a_worker_pod_makes_the_answer_unknown(self):
        self.assertTrue(self.census([listing(1), {"name": "kube-system-pod"}])["unknown"])

    def test_a_listing_as_long_as_the_launchers_limit_is_unknown(self):
        full = [listing(n) for n in range(1, ps.LIST_LIMIT + 1)]
        self.assertTrue(self.census(full)["unknown"])
        self.assertFalse(self.census(full[:-1])["unknown"])

    def test_the_list_limit_is_the_launchers(self):
        self.assertEqual(ps.LIST_LIMIT, launcher.MAX_LIST_PODS)


class ReportTests(unittest.TestCase):
    def test_an_idle_pass_says_nothing_and_a_busy_one_says_counts_only(self):
        self.assertIsNone(ps.Report().line())
        self.assertIsNone(ps.Report(skipped=3, terminating=2).line())
        report = ps.Report(deleted={"unrecorded": 2, "finished": 1}, released=1, failed=1, incomplete=True)
        self.assertEqual(report.line(), "pod sweep deleted=3 released=1 failed=1 incomplete=1 unreadable=0")
        self.assertEqual(report.total(), 4)


class RealLauncherTests(ServerHarness):
    """The sweep and the census against the real launcher over a real socket and the fake API, on ONE fake clock."""

    def setUp(self):
        super().setUp()
        ps.reset()
        self.addCleanup(ps.reset)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.spool = Path(tmp.name)

    def ticking(self):
        return launcher_client.LauncherClient(
            self.config.socket_path, launcher_uid=os.getuid(), sleep=self.clock.advance, clock=self.clock
        )

    def running(self, client, n=1):
        self.fake.script(pod_name(n), (Step(0, fake_kube.running()),))
        reply = client.launch(launch_request(n), TOKEN)
        found = session(n)
        sr.write(self.spool, sr.build(found, "t" * 43, int(self.clock()) + GRACE, pc.repo_key(fake_kube.REPOSITORY), "cwd", fake_kube.INSTANCE))
        return reply

    def test_an_unowned_running_pod_is_deleted_after_the_grace_and_is_then_terminating_and_then_gone(self):
        self.start_server()
        client = self.ticking()
        self.running(client)
        sweeper = ps.Sweeper(client.list_pods, client.delete_pod, self.spool, clock=self.clock)
        census = sweeper.census()
        self.assertEqual(census, {"live": 1, "final": 0, "unknown": False})
        self.assertEqual(sweeper.run_once(recorded, alive).deleted, {}, "inside the launch grace")
        self.clock.advance(GRACE)
        self.assertEqual(sweeper.run_once(recorded, alive).deleted, {"unrecorded": 1})
        self.assertTrue(sr.is_marked(self.spool, session(1), sr.MARKER_REVOKE), "the pod is cut off at the relay first")
        self.assertIn(pod_name(1), self.fake.pods(), "the delete is accepted: the pod runs for its termination grace")
        self.assertEqual(sweeper.census()["live"], 1, "a terminating pod still counts as running for the deploy gate")
        again = sweeper.run_once(recorded, alive)
        self.assertEqual((again.deleted, again.terminating), ({}, 1), "and is left alone, not asked again")
        self.clock.advance(pc.TERMINATION_GRACE_SECONDS)
        self.assertEqual(self.fake.pods(), {})
        self.assertEqual(sweeper.census(), {"live": 0, "final": 0, "unknown": False})

    def test_a_pod_a_job_records_survives_the_sweep_and_one_it_stopped_recording_does_not(self):
        self.start_server()
        client = self.ticking()
        self.running(client)
        self.clock.advance(10 * GRACE)
        sweeper = ps.Sweeper(client.list_pods, client.delete_pod, self.spool, clock=self.clock)
        owned = lambda: ps.Recorded({session(1): ps.Entry("org/name", "r1", "job-1", "running", handle(1))}, True)  # noqa: E731
        self.assertEqual(sweeper.run_once(owned, alive).deleted, {})
        released = lambda: ps.Recorded({session(1): ps.Entry("org/name", "r1", "job-1", "failed", handle(1))}, True)  # noqa: E731
        self.assertEqual(sweeper.run_once(released, alive).deleted, {"stale_record": 1})

    def test_a_launcher_that_is_down_is_unknown_to_the_census_and_idle_to_the_sweep(self):
        client = launcher_client.LauncherClient(
            str(self.spool / "absent.sock"), launcher_uid=os.getuid(), sleep=lambda _s: None, retries=0
        )
        sweeper = ps.Sweeper(client.list_pods, client.delete_pod, self.spool, clock=self.clock)
        self.assertEqual(sweeper.census(), {"live": 0, "final": 0, "unknown": True})
        report = sweeper.run_once(recorded, alive)
        self.assertEqual((report.unreadable, report.deleted), (True, {}))

    def test_the_real_listing_has_the_fields_the_sweep_reads(self):
        self.start_server()
        client = self.ticking()
        self.running(client)
        (item,) = client.list_pods()
        found = ps.listed(item)
        self.assertEqual((found.session, found.state, found.final, found.terminating), (session(1), "running", False, False))
        self.assertEqual((found.created, found.manager), (int(self.clock()), fake_kube.INSTANCE))


class ShapeTests(unittest.TestCase):
    def source(self):
        return Path(ps.__file__).read_text(encoding="utf-8")

    def test_the_module_is_standard_library_and_the_package_only(self):
        modules = set()
        for node in ast.walk(ast.parse(self.source())):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
            if isinstance(node, ast.ImportFrom):
                modules.add("." * node.level + (node.module or ""))
        self.assertTrue(modules <= set(sys.stdlib_module_names) | {"."}, modules)

    def test_it_never_prints_reads_the_environment_or_opens_a_socket_or_a_process(self):
        tree = ast.parse(self.source())
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        self.assertFalse({"print", "exec", "eval"} & names)
        self.assertFalse({"environ", "getenv", "socket", "connect", "system", "Popen", "urlopen"} & attributes)

    def test_the_windows_are_the_contracts(self):
        numbers = {
            node.value
            for node in ast.walk(ast.parse(self.source()))
            if isinstance(node, ast.Constant) and type(node.value) is int and node.value > 1
        }
        self.assertLessEqual(numbers, {1000, 1024, 64, 63, 2, 40, 32}, numbers)
        self.assertIn("pc.LAUNCH_GRACE_SECONDS", self.source())
        self.assertIn("pc.ORPHAN_SILENCE_SECONDS", self.source())
        self.assertIn("pc.TERMINATION_GRACE_SECONDS", self.source())

    def test_a_log_line_has_no_name_in_it(self):
        report = ps.Report(deleted={"unrecorded": 1}, released=1)
        self.assertNotIn(pc.POD_NAME_PREFIX, report.line())

    def test_the_reasons_are_documented_in_the_design_record(self):
        text = (Path(__file__).resolve().parents[1] / "docs" / "pod-backend.md").read_text(encoding="utf-8")
        for word in ps.REASONS:
            self.assertTrue(f"`{word}`" in text, word)
        self.assertTrue("podsweep.py" in text)


if __name__ == "__main__":
    unittest.main()

"""The pod status classifier (src/factory/podstatus.py) over every lifecycle the fake can produce.

Slice 7 of the pod backend (docs/pod-backend.md). CONTRACT-TESTED AGAINST ASSUMED SHAPES: the pod
statuses come from tests/fake_kube.py, written from the documented Kubernetes behaviour; no cluster
was consulted, and every row of the table is marked ASSUMED until slice 22 replaces it with a
recording. The tests pin:

* the table: the closed set of states, a row per state, the `reads` column (it is checked against the
  fields the matchers really read, in both directions), the ASSUMED marker and the order of the rows;
* every lifecycle of the fake, driven through the real client over HTTP with the fake clock;
* a NotFound after creation is `gone`, never `succeeded`;
* the classifier fails closed (garbage is `pending`, never `succeeded` or `running`), never raises, never
  copies free text and never mutates the pod.
"""

import ast
import copy
import dataclasses
import sys
import tempfile
import unittest
from pathlib import Path

from factory import kube
from factory import podspec
from factory import podstatus as ps
from tests import fake_kube
from tests.fake_kube import FakeClock, FakeKube

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "eyJhbGciOiJSUzI1NiJ9.token.sig"
CANARY = "CANARY-POD-TEXT-4c2e"

SPEC_STATES = {
    "pending",
    "unschedulable",
    "starting_error",
    "running",
    "succeeded",
    "failed_exit",
    "oom",
    "evicted",
    "deadline",
    "gone",
}

# Per scenario of the fake: (seconds after creation, state, detail, exit code, final) as the client sees it.
TIMELINES = {
    "succeeds": [
        (0, "unschedulable", "Unschedulable", None, False),
        (99, "unschedulable", "Unschedulable", None, False),
        (100, "pending", "waiting", None, False),
        (105, "running", "running", None, False),
        (199, "running", "running", None, False),
        (200, "succeeded", "completed", None, True),
    ],
    "unschedulable": [(0, "unschedulable", "Unschedulable", None, False), (5000, "unschedulable", "Unschedulable", None, False)],
    "image_pull_backoff": [
        (0, "pending", "waiting", None, False),
        (5, "starting_error", "ErrImagePull", None, False),
        (10, "starting_error", "ImagePullBackOff", None, False),
    ],
    "config_error": [(0, "pending", "waiting", None, False), (3, "starting_error", "CreateContainerConfigError", None, False)],
    "exit_nonzero": [(0, "running", "running", None, False), (60, "failed_exit", "exit", 2, True)],
    "oom": [(0, "running", "running", None, False), (60, "oom", "OOMKilled", 137, True)],
    "oom_while_running": [(0, "running", "running", None, False), (60, "oom", "OOMKilled", 137, True)],
    "evicted": [(0, "running", "running", None, False), (30, "evicted", "Evicted", None, True)],
    "deadline": [(0, "running", "running", None, False), (30, "deadline", "DeadlineExceeded", None, True)],
    "kubelet_rejected": [(0, "starting_error", "never_started", None, True)],
    "disruption_target": [(0, "running", "running", None, False), (30, "evicted", "DisruptionTarget", None, True)],
    "node_deleted": [(0, "running", "running", None, False), (29, "running", "running", None, False), (30, "gone", "not_found", None, True)],
    "karpenter_eviction": [
        (0, "running", "running", None, False),
        (30, "evicted", "terminating", None, True),
        (60, "gone", "not_found", None, True),
    ],
}


def pod(phase=None, **status):
    """A minimal pod with `status.phase` and the other status fields given."""
    found = {"metadata": {"name": "p"}, "status": dict(status)}
    if phase is not None:
        found["status"]["phase"] = phase
    return found


def container(state):
    return {"name": "worker", "state": state}


def terminated(reason, code):
    return container({"terminated": {"reason": reason, "exitCode": code}})


class ClassifierCase(unittest.TestCase):
    """A fake API on a fake clock and a client, to look at pods the way the launcher will."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        token = Path(tmp.name) / "token"
        token.write_text(TOKEN, encoding="ascii")
        self.clock = FakeClock()
        self.fake = FakeKube(clock=self.clock, tokens={TOKEN}).start()
        self.addCleanup(self.fake.stop)
        self.client = kube.Client(self.fake.url, token_path=token, clock=self.clock, sleep=lambda seconds: None)

    def observe(self, name):
        """What the launcher does when it polls: a NotFound after creation is `gone`."""
        try:
            return ps.classify(self.client.get_pod(name))
        except kube.NotFound:
            return ps.classify(None)


class TableTest(unittest.TestCase):
    def test_the_closed_set_is_the_one_of_the_design(self):
        self.assertEqual(set(ps.STATES), SPEC_STATES)
        self.assertEqual(len(ps.STATES), len(SPEC_STATES))

    def test_every_state_has_a_row_and_every_row_a_known_state(self):
        self.assertEqual({row.state for row in ps.TABLE}, SPEC_STATES)

    def test_row_ids_are_unique_and_the_ends_of_the_table_are_gone_and_the_fallback(self):
        ids = [row.id for row in ps.TABLE]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual((ps.TABLE[0].id, ps.TABLE[-1].id), ("gone", "unrecognised"))
        self.assertEqual([row.id for row in ps.TABLE if row.match is None], ["gone", "unrecognised"])

    def test_every_row_names_the_field_it_reads_and_is_marked_assumed(self):
        for row in ps.TABLE:
            with self.subTest(row=row.id):
                self.assertTrue(row.reads)
                self.assertEqual(row.evidence, "ASSUMED")
                self.assertTrue(all(isinstance(path, str) and path for path in row.reads))
        # The marker is a fixed word: a recording replaces it row by row (slice 22).
        self.assertEqual(ps.ASSUMED, "ASSUMED")

    def test_the_fields_a_row_names_are_pod_fields_or_the_not_found_answer(self):
        for row in ps.TABLE:
            for path in row.reads:
                with self.subTest(row=row.id, path=path):
                    self.assertTrue(path.startswith(("status.", "metadata.")) or path == ps.NOT_FOUND)

    def corpus(self):
        pods = [{"metadata": {"name": "p"}, "status": copy.deepcopy(step.status)} for steps in fake_kube.SCENARIOS.values() for step in steps if step.status]
        pods.append({"metadata": {"name": "p", "deletionTimestamp": fake_kube.FIXED_TIME}, "status": {"phase": "Running"}})
        pods.append({"metadata": {"name": "p"}, "status": {"phase": "Pending", "conditions": [{"type": "PodScheduled", "status": "False", "reason": "SchedulingGated"}]}})
        pods.extend([{}, {"status": None}, pod("Unknown"), pod("Pending", containerStatuses=[container({"waiting": {"reason": "ContainerCreating"}})])])
        return pods

    def test_a_matcher_reads_only_the_fields_its_row_names(self):
        for found in self.corpus():
            for row in ps.ROWS:
                view = ps.View(found)
                row.match(view)
                with self.subTest(row=row.id):
                    self.assertLessEqual(view.touched, set(row.reads))

    def test_every_field_a_row_names_is_read_by_its_matcher_on_some_pod(self):
        for row in ps.ROWS:
            touched = set()
            for found in self.corpus():
                view = ps.View(found)
                row.match(view)
                touched |= view.touched
            with self.subTest(row=row.id):
                self.assertEqual(touched, set(row.reads))

    def test_the_view_records_paths_and_walks_dicts_only(self):
        view = ps.View({"status": {"phase": "Running", "containerStatuses": [{"state": {"waiting": {"reason": "x"}}}, 5, "s", None]}})
        self.assertEqual(view.value("status.phase"), "Running")
        self.assertIsNone(view.value("status.phase.deeper"))
        self.assertIsNone(view.value("nothing.here"))
        children = view.each("status.containerStatuses")
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0].value("state.waiting.reason"), "x")
        self.assertEqual(view.each("status.phase"), [])
        self.assertEqual(view.each("a.b"), [])
        self.assertEqual(view.touched, {"status.phase", "status.phase.deeper", "status.phase[]", "nothing.here", "status.containerStatuses[]", "status.containerStatuses[].state.waiting.reason", "a.b[]"})
        many = ps.View({"s": [{"k": n} for n in range(100)]})
        self.assertEqual(len(many.each("s")), ps.MAX_ITEMS)

    def test_the_order_of_the_table_is_the_precedence(self):
        ids = [row.id for row in ps.ROWS]
        order = lambda first, second: self.assertLess(ids.index(first), ids.index(second))  # noqa: E731
        order("succeeded", "eviction-reason")
        order("succeeded", "terminating")
        order("eviction-reason", "oom-killed")
        order("deadline-reason", "oom-killed")
        order("oom-killed", "failed-exit")
        order("failed-exit", "failed-never-started")
        order("failed-exit", "terminating")
        order("terminating", "start-error")
        order("start-error", "unschedulable")
        order("unschedulable", "running")
        order("running", "pending")

    def test_the_module_is_standard_library_only_and_pure(self):
        tree = ast.parse((ROOT / "src" / "factory" / "podstatus.py").read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, "no relative import: the classifier does not need the client or the contract")
                names.add((node.module or "").split(".")[0])
        self.assertLessEqual(names, set(sys.stdlib_module_names))
        self.assertTrue(names.isdisjoint({"random", "socket", "http", "urllib", "subprocess", "os"}))


class LifecycleTest(ClassifierCase):
    def test_every_scenario_of_the_fake_has_a_timeline_here(self):
        self.assertEqual(set(TIMELINES), set(fake_kube.SCENARIOS))

    def test_every_lifecycle_of_the_fake_is_classified_over_time(self):
        for number, scenario in enumerate(TIMELINES, start=1):
            pod_manifest = fake_kube.worker_pod(number)
            name = pod_manifest["metadata"]["name"]
            self.fake.script(name, scenario)
            started = self.clock()
            self.client.create_pod(pod_manifest)
            for seconds, state, detail, code, final in TIMELINES[scenario]:
                self.clock.advance(started + seconds - self.clock())
                outcome = self.observe(name)
                with self.subTest(scenario=scenario, seconds=seconds):
                    self.assertEqual((outcome.state, outcome.detail, outcome.exit_code, outcome.final), (state, detail, code, final))

    def test_every_state_of_the_closed_set_is_produced_by_some_lifecycle(self):
        produced = {state for timeline in TIMELINES.values() for _s, state, _d, _c, _f in timeline}
        self.assertEqual(produced, SPEC_STATES)

    def test_every_row_is_hit_by_some_lifecycle_of_the_fake(self):
        hit = set()
        for steps in fake_kube.SCENARIOS.values():
            for step in steps:
                if step.status is not None:
                    hit.add(ps.classify({"metadata": {"name": "p"}, "status": step.status}).row)
        for step in fake_kube.SCENARIOS["karpenter_eviction"]:
            if step.deletion:
                hit.add(ps.classify({"metadata": {"deletionTimestamp": fake_kube.FIXED_TIME}, "status": {"phase": "Running"}}).row)
        hit.update({"gone"})
        self.assertEqual(hit, {row.id for row in ps.TABLE} - {"unrecognised"})

    def test_a_pod_that_has_just_been_created_is_pending(self):
        manifest = fake_kube.worker_pod(1)
        created = self.client.create_pod(manifest)
        self.assertEqual(ps.classify(created).state, "pending")

    def test_a_finished_pod_that_was_deleted_is_gone_and_never_succeeded(self):
        manifest = fake_kube.worker_pod(1)
        name = manifest["metadata"]["name"]
        self.client.create_pod(manifest)
        self.clock.advance(300)
        self.assertEqual(self.observe(name).state, "succeeded")
        self.client.delete_pod(name, 0)
        outcome = self.observe(name)
        self.assertEqual(outcome.state, "gone")
        self.assertNotEqual(outcome.state, "succeeded")
        self.assertTrue(outcome.final)

    def test_a_pod_that_vanishes_with_its_node_is_gone_never_succeeded(self):
        manifest = fake_kube.worker_pod(1)
        name = manifest["metadata"]["name"]
        self.client.create_pod(manifest)
        self.clock.advance(150)
        self.assertEqual(self.observe(name).state, "running")
        self.fake.delete_node(name)
        self.assertEqual(self.observe(name).state, "gone")

    def test_a_not_found_answer_is_gone_whatever_the_pod_was_doing(self):
        self.assertEqual(ps.classify(None), ps.Outcome("gone", "not_found", "gone", True))
        with self.assertRaises(kube.NotFound):
            self.client.get_pod(fake_kube.worker_pod(1)["metadata"]["name"])

    def test_a_pod_that_is_being_deleted_is_terminating_until_it_is_gone(self):
        manifest = fake_kube.worker_pod(1)
        name = manifest["metadata"]["name"]
        self.client.create_pod(manifest)
        self.clock.advance(150)
        self.client.delete_pod(name, 30)
        outcome = self.observe(name)
        # The classifier cannot tell who deleted the pod: the caller knows when it asked for the delete.
        self.assertEqual((outcome.state, outcome.detail, outcome.final), ("evicted", "terminating", True))
        self.clock.advance(30)
        self.assertEqual(self.observe(name).state, "gone")

    def test_a_manifest_that_has_no_status_yet_is_pending_and_never_succeeded(self):
        found = podspec.build_pod(
            {
                "session_id": fake_kube.session_id(1),
                "repository": fake_kube.REPOSITORY,
                "run_id": "run-1",
                "job_id": "build-1",
                "attempt": 1,
                "phase": "build",
                "resource_class": "small",
                "timeout_seconds": 600,
                "manager_instance": fake_kube.INSTANCE,
            },
            podspec.PodConfig(fake_kube.IMAGE, "http://10.244.1.7:8090", "10.244.1.7", "10.96.0.9", "example.invalid", clock=lambda: 1_800_000_000),
        )
        outcome = ps.classify(found)  # a manifest has no status yet: it is not running and it did not succeed
        self.assertEqual((outcome.state, outcome.row, outcome.final), ("pending", "unrecognised", False))


class PrecedenceTest(unittest.TestCase):
    """Rows that could both match: the earlier one wins (the table order is the contract)."""

    def state(self, found):
        return ps.classify(found).state

    def test_succeeded_wins_over_a_deletion_timestamp_and_an_eviction_reason(self):
        found = pod("Succeeded", reason="Evicted", containerStatuses=[terminated("OOMKilled", 137)])
        found["metadata"]["deletionTimestamp"] = "2026-01-01T00:00:00Z"
        self.assertEqual(self.state(found), "succeeded")

    def test_an_eviction_wins_over_the_oom_and_the_exit_code_of_the_killed_container(self):
        self.assertEqual(self.state(pod("Failed", reason="Evicted", containerStatuses=[terminated("OOMKilled", 137)])), "evicted")
        self.assertEqual(self.state(pod("Failed", reason="Evicted", containerStatuses=[terminated("Error", 137)])), "evicted")

    def test_the_deadline_wins_over_the_oom_and_the_exit_code(self):
        self.assertEqual(self.state(pod("Failed", reason="DeadlineExceeded", containerStatuses=[terminated("OOMKilled", 137)])), "deadline")
        self.assertEqual(self.state(pod("Failed", reason="DeadlineExceeded", containerStatuses=[terminated("Error", 137)])), "deadline")

    def test_the_oom_wins_over_a_plain_exit_code(self):
        self.assertEqual(self.state(pod("Failed", containerStatuses=[terminated("OOMKilled", 137)])), "oom")

    def test_a_failure_after_a_start_is_not_an_eviction_just_because_the_pod_is_being_deleted(self):
        found = pod("Failed", containerStatuses=[terminated("Error", 1)])
        found["metadata"]["deletionTimestamp"] = "2026-01-01T00:00:00Z"
        outcome = ps.classify(found)
        self.assertEqual((outcome.state, outcome.exit_code), ("failed_exit", 1))

    def test_a_start_error_wins_over_unschedulable_and_pending(self):
        found = pod(
            "Pending",
            conditions=[{"type": "PodScheduled", "status": "False", "reason": "Unschedulable"}],
            containerStatuses=[container({"waiting": {"reason": "ImagePullBackOff"}})],
        )
        self.assertEqual(self.state(found), "starting_error")

    def test_a_deleted_pending_pod_is_terminating_before_it_is_unschedulable(self):
        found = pod("Pending", conditions=[{"type": "PodScheduled", "status": "False", "reason": "Unschedulable"}])
        found["metadata"]["deletionTimestamp"] = "2026-01-01T00:00:00Z"
        self.assertEqual(self.state(found), "evicted")

    def test_each_row_in_isolation(self):
        cases = {
            "succeeded": pod("Succeeded"),
            "eviction-reason": pod("Failed", reason="Terminated"),
            "deadline-reason": pod("Failed", reason="DeadlineExceeded"),
            "disruption-condition": pod("Running", conditions=[{"type": "DisruptionTarget", "status": "True"}]),
            "oom-killed": pod("Running", containerStatuses=[terminated("OOMKilled", 137)]),
            "failed-exit": pod("Failed", containerStatuses=[terminated("Error", 3)]),
            "failed-never-started": pod("Failed", reason="OutOfpods"),
            "terminating": {"metadata": {"deletionTimestamp": "t"}, "status": {"phase": "Running"}},
            "start-error": pod("Pending", containerStatuses=[container({"waiting": {"reason": "ErrImageNeverPull"}})]),
            "unschedulable": pod("Pending", conditions=[{"type": "PodScheduled", "status": "False", "reason": "Unschedulable"}]),
            "running": pod("Running"),
            "pending": pod("Pending"),
            "unrecognised": pod("Unknown"),
        }
        self.assertEqual(set(cases), {row.id for row in ps.TABLE} - {"gone"})
        for row_id, found in cases.items():
            with self.subTest(row=row_id):
                self.assertEqual(ps.classify(found).row, row_id)

    def test_a_disruption_condition_that_is_not_true_is_not_an_eviction(self):
        for status in ("False", "Unknown", True, None):
            found = pod("Running", conditions=[{"type": "DisruptionTarget", "status": status}])
            with self.subTest(status=status):
                self.assertEqual(self.state(found), "running")

    def test_every_listed_start_error_and_eviction_reason_is_recognised_and_other_words_are_not(self):
        for reason in ps.START_ERROR_REASONS:
            self.assertEqual(self.state(pod("Pending", containerStatuses=[container({"waiting": {"reason": reason}})])), "starting_error", reason)
        for reason in ("ContainerCreating", "PodInitializing", "", None, 5, CANARY):
            self.assertEqual(self.state(pod("Pending", containerStatuses=[container({"waiting": {"reason": reason}})])), "pending", reason)
        for reason in ps.EVICTION_REASONS:
            self.assertEqual(self.state(pod("Failed", reason=reason)), "evicted", reason)
        for reason in ("Preempting", "evicted", "EVICTED", CANARY, None, 5):
            self.assertEqual(self.state(pod("Failed", reason=reason)), "starting_error", reason)

    def test_a_start_error_while_running_is_still_a_start_error_but_not_after_a_terminal_phase(self):
        waiting = [container({"waiting": {"reason": "CreateContainerError"}})]
        self.assertEqual(self.state(pod("Running", containerStatuses=waiting)), "starting_error")
        self.assertEqual(self.state(pod("Succeeded", containerStatuses=waiting)), "succeeded")
        self.assertEqual(self.state(pod("Failed", containerStatuses=waiting)), "starting_error")  # never started, by the Failed row


class FailClosedTest(unittest.TestCase):
    GARBAGE = (
        [],
        "Succeeded",
        0,
        1.5,
        True,
        b"Succeeded",
        {},
        {"status": None},
        {"status": []},
        {"status": "Succeeded"},
        {"status": {}},
        {"status": {"phase": None}},
        {"status": {"phase": "succeeded"}},
        {"status": {"phase": "SUCCEEDED"}},
        {"status": {"phase": ["Succeeded"]}},
        {"status": {"phase": {"Succeeded": 1}}},
        {"status": {"phase": "Unknown"}},
        {"status": {"phase": 5}},
        {"status": {"phase": True}},
        {"phase": "Succeeded"},
        {"status": {"phase": "Succeeded "}},
        {"status": {"conditions": "x", "containerStatuses": "y"}},
    )

    def test_garbage_is_pending_unrecognised_never_succeeded_never_running(self):
        for found in self.GARBAGE:
            with self.subTest(found=repr(found)[:40]):
                outcome = ps.classify(found)
                self.assertEqual((outcome.state, outcome.detail, outcome.row, outcome.final), ("pending", "unrecognised", "unrecognised", False))

    def test_the_classifier_never_raises(self):
        deep = {}
        cursor = deep
        for _step in range(50_000):
            cursor["status"] = {}
            cursor = cursor["status"]
        hostile = [deep, {"status": {"phase": "Failed", "containerStatuses": [None, 1, "x", [], {"state": None}, {"state": {"terminated": 5}}]}},
                   {"status": {"phase": "Pending", "conditions": [None, {"type": []}, {"type": "PodScheduled", "status": "False", "reason": {}}]}},
                   {"metadata": {"deletionTimestamp": ["x"]}, "status": {"phase": "Running"}}]
        for found in hostile:
            ps.classify(found)

    def test_a_huge_list_is_bounded(self):
        many = [container({"waiting": {"reason": "ContainerCreating"}}) for _n in range(10_000)]
        many.append(container({"waiting": {"reason": "ImagePullBackOff"}}))
        self.assertEqual(ps.classify(pod("Pending", containerStatuses=many)).state, "pending")

    def test_exit_codes_are_integers_between_0_and_255_and_a_bool_is_not_one(self):
        for code, expected in ((0, 0), (1, 1), (137, 137), (255, 255), (256, None), (-1, None), (True, None), (False, None), (1.0, None), ("1", None), (None, None), ([1], None)):
            with self.subTest(code=code):
                found = pod("Failed", containerStatuses=[terminated("Error", code)])
                self.assertEqual(ps.classify(found).exit_code, expected)
        self.assertIsNone(ps.classify(pod("Failed", containerStatuses=[{"state": {"terminated": {}}}])).exit_code)

    def test_no_free_text_of_the_pod_reaches_the_outcome(self):
        poisoned = {
            "metadata": {"name": CANARY, "deletionTimestamp": None, "labels": {"x": CANARY}},
            "status": {
                "phase": "Failed",
                "reason": CANARY,
                "message": CANARY,
                "conditions": [{"type": "PodScheduled", "status": "False", "reason": CANARY, "message": CANARY}],
                "containerStatuses": [
                    {"name": CANARY, "state": {"terminated": {"reason": CANARY, "message": CANARY, "exitCode": 7, "signal": CANARY}}}
                ],
                "hostIP": CANARY,
            },
        }
        outcome = ps.classify(poisoned)
        self.assertEqual((outcome.state, outcome.exit_code), ("failed_exit", 7))
        self.assertNotIn(CANARY, repr(outcome))
        for found in (
            pod("Pending", containerStatuses=[container({"waiting": {"reason": CANARY, "message": CANARY}})]),
            pod("Failed", reason=CANARY),
            pod("Running", conditions=[{"type": "DisruptionTarget", "status": "True", "reason": CANARY}]),
            pod("Pending", conditions=[{"type": "PodScheduled", "status": "False", "reason": "Unschedulable", "message": CANARY}]),
        ):
            self.assertNotIn(CANARY, repr(ps.classify(found)))

    def test_every_detail_is_a_fixed_word_or_a_listed_reason(self):
        allowed = {
            "completed",
            "exit",
            "never_started",
            "terminating",
            "running",
            "waiting",
            "Unschedulable",
            "OOMKilled",
            "DeadlineExceeded",
            "DisruptionTarget",
            "not_found",
            "unrecognised",
            *ps.EVICTION_REASONS,
            *ps.START_ERROR_REASONS,
        }
        seen = {detail for timeline in TIMELINES.values() for _s, _st, detail, _c, _f in timeline}
        self.assertLessEqual(seen, allowed)

    def test_classify_does_not_change_the_pod(self):
        for steps in fake_kube.SCENARIOS.values():
            for step in steps:
                if step.status is not None:
                    found = {"metadata": {"name": "p"}, "status": step.status}
                    before = copy.deepcopy(found)
                    ps.classify(found)
                    self.assertEqual(found, before)

    def test_an_outcome_is_immutable_and_comparable(self):
        outcome = ps.classify(pod("Running"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            outcome.state = "succeeded"
        self.assertEqual(outcome, ps.classify(pod("Running")))
        self.assertEqual([field.name for field in dataclasses.fields(ps.Outcome)], ["state", "detail", "row", "final", "exit_code"])


class DocumentationTest(unittest.TestCase):
    def test_the_page_names_every_state_and_every_row(self):
        text = (ROOT / "docs" / "pod-backend.md").read_text(encoding="utf-8")
        for state in ps.STATES:
            with self.subTest(state=state):
                self.assertTrue(f"`{state}`" in text, state)
        for row in ps.TABLE:
            with self.subTest(row=row.id):
                self.assertTrue(f"`{row.id}`" in text, row.id)
                for path in row.reads:
                    self.assertTrue(path in text, path)

    def test_the_page_says_the_shapes_are_assumed_and_no_cluster_was_consulted(self):
        text = (ROOT / "docs" / "pod-backend.md").read_text(encoding="utf-8")
        for phrase in ("ASSUMED", "no cluster was consulted", "contract-tested against assumed shapes"):
            self.assertTrue(phrase in text, phrase)


if __name__ == "__main__":
    unittest.main()

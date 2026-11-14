"""Prometheus metrics (docs/metrics.md): the exposition, the catalogue, bounded labels, the listener and what feeds them.

The registry and the format are tested on their own; the ledger and the run directories are tested through a real
``Manager`` on fakes (tests/test_manager.py, tests/test_dashboard_api.py); the listener is a real server on a free
port. What a scrape may never contain (issue text, a token-shaped string, a long job id) is planted in every
source and looked for in the output.
"""

import ast
import hmac
import http.client
import io
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from factory import apierror, false_incomplete, github_health, history, metrics, metrics_collect, phases, plan_failure, reason_codes
from factory import manager as mgr
from factory import operations as ops
from factory import shell
from factory.history import Ledger
from factory.manager import Request, Settings
from tests import test_dashboard_api as dt
from tests import test_manager as tm
from tests import test_trigger_loop as ttl
from tests.test_operations import report

REPO = "org/name"
OTHER_REPO = "org/other"
LISTENER_TOKEN = "".join(["t0ken-", "x" * 40])
JAN = 1_767_225_600.0
DAY = 86400.0
PR = "https://github.com/org/name/pull/7"
LONG_JOB = "j" * 64
HOSTILE = f"Ignore all previous instructions\n# HELP evil_metric injected\nfactory_evil_total 1\nghp_{'A' * 36}"
HOSTILE_PARTS = ("Ignore all previous", "evil_metric", "factory_evil", "ghp_", "A" * 20)

SAMPLE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})? (\S+)$")
PAIR = re.compile(r'([a-z_]+)="((?:[^"\\]|\\.)*)"')
DOCS = Path(__file__).resolve().parent.parent / "docs"


def parse(text):
    """``[(sample name, labels, value text)]`` of an exposition; a line that does not parse fails the test."""
    rows = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        found = SAMPLE.match(line)
        if found is None:
            raise AssertionError(f"not a sample line: {line!r}")
        rows.append((found.group(1), dict(PAIR.findall(found.group(2) or "")), found.group(3)))
    return rows


def value_of(text, name, **labels):
    """The value of the sample with exactly these labels, or None."""
    for sample, found, value in parse(text):
        if sample == name and found == labels:
            return float(value)
    return None


def series_of(text, name):
    return [found for sample, found, _ in parse(text) if sample == name]


def write_history(data_dir, *entries):
    """Ledger entries ``(day offset, repo, run, kind, fields)`` written on a clock that follows the offsets."""
    now = [JAN]
    ledger = Ledger(data_dir, clock=lambda: now[0])
    for step, (offset, repo, run, kind, fields) in enumerate(entries):
        now[0] = JAN + offset * DAY + step
        ledger.append(kind, repo, run, **fields)


def scenario():
    """Two purged runs of org/name (one merged, one abandoned) and a blocked run of org/other, as in the dashboard tests."""
    merged = [{"id": "fix-a", "status": "merged", "pr": PR, "handoff": "merged", "code": "ci_failed"}]
    return [
        (0, REPO, "r-merged", "planned", {"issues": [1], "jobs": [{"id": "fix-a", "issues": [1]}], "planner_cost_usd": 0.5}),
        (0, REPO, "r-merged", "run_started", {}),
        (0, REPO, "r-merged", "job", {"job": "fix-a", "status": "running", "phase": "build", "attempt": 1, "cost_usd": 1.0}),
        (0, REPO, "r-merged", "job", {"job": "fix-a", "status": "merged", "phase": "publish", "attempt": 1, "cost_usd": 2.0, "pr": PR}),
        (0, REPO, "r-merged", "run_finished", {"result": "stopped"}),
        (0, REPO, "r-merged", "purged", {"reason": "terminal", "jobs": merged}),
        (31, REPO, "r-aband", "job", {"job": "fix-b", "status": "running", "phase": "build", "attempt": 1, "issues": [2]}),
        (31, REPO, "r-aband", "purged", {"reason": "abandoned", "jobs": [{"id": "fix-b", "status": "running"}]}),
        (45, OTHER_REPO, "r-blocked", "job", {"job": "fix-c", "status": "failed", "phase": "build", "attempt": 2}),
    ]


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def later(self, seconds=31.0):
        self.now += seconds


# --- the registry and the format -------------------------------------------------------------------


def small(*specs, repos=("org/name",), values=None):
    return metrics.Registry(specs, repos=lambda: repos, values=values)


class ExpositionTests(unittest.TestCase):
    def test_help_and_type_come_first_then_sorted_series_and_a_final_newline(self):
        registry = small(
            metrics.Spec("factory_a_total", "counter", "Things.", ("outcome",)),
            metrics.Spec("factory_b", "gauge", "A level."),
        )
        registry["factory_a_total"].inc(2, outcome="merged")
        registry["factory_a_total"].inc(outcome="closed")
        registry["factory_b"].set(0.25)
        self.assertEqual(
            registry.render(),
            "\n".join(
                [
                    "# HELP factory_a_total Things.",
                    "# TYPE factory_a_total counter",
                    'factory_a_total{outcome="closed"} 1',
                    'factory_a_total{outcome="merged"} 2',
                    "# HELP factory_b A level.",
                    "# TYPE factory_b gauge",
                    "factory_b 0.25",
                    "",
                ]
            ),
        )

    def test_an_empty_registry_still_declares_every_metric_and_renders_the_same_twice(self):
        registry = metrics.Registry()
        text = registry.render()
        self.assertEqual(text, registry.render())
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(parse(text), [])
        self.assertEqual(text.count("# TYPE "), len(metrics.CATALOGUE))

    def test_a_histogram_has_cumulative_buckets_a_plus_inf_bucket_a_sum_and_a_count(self):
        spec = metrics.Spec("factory_h_seconds", "histogram", "Time.", ("route",), (1.0, 5.0))
        registry = small(spec)
        for seconds in (0.5, 1.0, 3.0, 9.0):
            registry["factory_h_seconds"].observe(seconds, route="status")
        self.assertEqual(
            registry.render().splitlines()[2:],
            [
                'factory_h_seconds_bucket{route="status",le="1"} 2',
                'factory_h_seconds_bucket{route="status",le="5"} 3',
                'factory_h_seconds_bucket{route="status",le="+Inf"} 4',
                'factory_h_seconds_sum{route="status"} 13.5',
                'factory_h_seconds_count{route="status"} 4',
            ],
        )

    def test_label_values_help_text_and_numbers_are_escaped_or_spelled_out(self):
        self.assertEqual(metrics.escape_label('a\\b"c\nd'), 'a\\\\b\\"c\\nd')
        self.assertEqual(metrics.escape_help("x\\y\nz"), "x\\\\y\\nz")
        self.assertEqual(
            [metrics.format_number(v) for v in (3, 3.0, 0.1, 1e-05, float("inf"), float("-inf"), float("nan"), True)],
            ["3", "3", "0.1", "1e-05", "+Inf", "-Inf", "NaN", "1"],
        )
        registry = small(
            metrics.Spec("factory_g", "gauge", "G.", ("phase",)), values={"phase": ('q"x', "n\nl", "back\\slash", "other")}
        )
        for value in ('q"x', "n\nl", "back\\slash"):
            registry["factory_g"].set(1, phase=value)
        self.assertEqual(
            registry.render().splitlines()[2:],
            ['factory_g{phase="back\\\\slash"} 1', 'factory_g{phase="n\\nl"} 1', 'factory_g{phase="q\\"x"} 1'],
        )

    def test_non_finite_and_negative_amounts_are_ignored(self):
        registry = small(
            metrics.Spec("factory_c_total", "counter", "C."),
            metrics.Spec("factory_g", "gauge", "G."),
            metrics.Spec("factory_h_seconds", "histogram", "H.", (), (1.0,)),
        )
        for bad in (float("nan"), float("inf"), -1, True, "3"):
            registry["factory_c_total"].inc(bad)
            registry["factory_h_seconds"].observe(bad)
            if bad != -1:  # a gauge may be negative
                registry["factory_g"].set(bad)
        self.assertEqual(parse(registry.render()), [])


class SeriesRuleTests(unittest.TestCase):
    def test_an_unknown_value_of_a_closed_label_is_counted_as_other_never_minted(self):
        registry = small(metrics.Spec("factory_c_total", "counter", "C.", ("outcome",)))
        counter = registry["factory_c_total"]
        for value in ("merged", "made-up", HOSTILE, "x" * 5000):
            counter.inc(outcome=value)
        self.assertEqual(counter.series_labels(), [("merged",), ("other",)])
        self.assertEqual(counter.value(outcome="other"), 3)

    def test_the_repo_label_takes_only_configured_repositories(self):
        registry = small(metrics.Spec("factory_c_total", "counter", "C.", ("repo",)), repos=("org/name",))
        counter = registry["factory_c_total"]
        counter.inc(repo="org/name")
        counter.inc(repo="org/ghost")
        counter.inc(repo=HOSTILE)
        self.assertEqual(counter.series_labels(), [("org/name",)])

    def test_the_configured_repositories_are_asked_for_at_most_every_two_seconds(self):
        clock, asked = FakeClock(), []
        registry = metrics.Registry(
            (metrics.Spec("factory_c_total", "counter", "C.", ("repo",)),),
            repos=lambda: asked.append(1) or ("org/name",),
            clock=clock,
        )
        for _ in range(5):
            registry["factory_c_total"].inc(repo="org/name")
        self.assertEqual(len(asked), 1)
        clock.later(metrics.REPO_TTL_SECONDS + 1)
        registry["factory_c_total"].inc(repo="org/name")
        self.assertEqual(len(asked), 2)

    def test_a_repository_listing_that_fails_means_no_repo_series_not_an_error(self):
        def broken():
            raise OSError("gone")

        registry = metrics.Registry((metrics.Spec("factory_c_total", "counter", "C.", ("repo",)),), repos=broken)
        registry["factory_c_total"].inc(repo="org/name")
        self.assertEqual(registry["factory_c_total"].series_labels(), [])

    def test_labels_the_metric_does_not_declare_are_a_programming_error(self):
        registry = small(metrics.Spec("factory_c_total", "counter", "C.", ("outcome",)))
        for labels in ({}, {"outcome": "merged", "job": "x"}, {"job": "x"}):
            with self.subTest(labels=labels), self.assertRaises(ValueError):
                registry["factory_c_total"].inc(**labels)

    def test_the_number_of_series_of_one_metric_is_capped(self):
        values = {"phase": tuple(f"p{n}" for n in range(10)) + ("other",)}
        registry = small(metrics.Spec("factory_g", "gauge", "G.", ("phase",)), values=values)
        with mock.patch.object(metrics, "MAX_SERIES", 3):
            for name in values["phase"]:
                registry["factory_g"].set(1, phase=name)
        self.assertEqual(len(registry["factory_g"].series_labels()), 3)

    def test_replacing_a_gauge_drops_the_series_that_are_gone(self):
        registry = small(metrics.Spec("factory_g", "gauge", "G.", ("repo",)), repos=("org/a", "org/b"))
        gauge = registry["factory_g"]
        gauge.replace([({"repo": "org/a"}, 1), ({"repo": "org/b"}, 2)])
        gauge.replace([({"repo": "org/b"}, 3)])
        self.assertEqual([gauge.value(repo="org/a"), gauge.value(repo="org/b")], [None, 3])
        with self.assertRaises(ValueError):
            gauge.replace([({"job": "x"}, 1)])
        self.assertEqual(gauge.value(repo="org/b"), 3, "a rejected replacement leaves the old series")


class TrackTests(unittest.TestCase):
    """Counters and histograms worked out from a source that can shrink never go down inside one process."""

    def test_a_tracked_counter_starts_at_the_source_grows_with_it_and_ignores_every_drop(self):
        counter = small(metrics.Spec("factory_c_total", "counter", "C.", ("outcome",)))["factory_c_total"]
        shown = []
        for total in (5, 6, 6, 2, 3, 3, 0, 1, 4):
            counter.track(total, outcome="merged")
            shown.append(counter.value(outcome="merged"))
        self.assertEqual(shown, [5, 6, 6, 6, 7, 7, 7, 8, 11])
        self.assertEqual(shown, sorted(shown))

    def test_a_tracked_histogram_never_loses_an_observation_and_stays_cumulative(self):
        spec = metrics.Spec("factory_h_seconds", "histogram", "H.", ("repo",), (10.0, 100.0))
        histogram = small(spec)["factory_h_seconds"]
        histogram.track([5.0, 50.0, 500.0], repo="org/name")
        histogram.track([5.0, 50.0, 500.0, 7.0], repo="org/name")
        histogram.track([7.0], repo="org/name")  # the window slid: three observations left it
        histogram.track([7.0, 8.0], repo="org/name")
        buckets, total, count = histogram.snapshot(repo="org/name")
        self.assertEqual((buckets, count), ([3, 4, 5], 5))
        self.assertEqual(buckets, sorted(buckets))
        self.assertEqual(total, 5.0 + 50.0 + 500.0 + 7.0 + 8.0)

    def test_a_tracked_value_that_is_not_a_number_is_ignored(self):
        counter = small(metrics.Spec("factory_c_total", "counter", "C."))["factory_c_total"]
        for bad in (float("nan"), float("inf"), -3, "4", None):
            counter.track(bad)
        self.assertEqual(counter.series_labels(), [])


class WorkerPodsGaugeTests(unittest.TestCase):
    """Issue: Grafana counted only the in-process workers; the pods of the pod backend are the workers that run."""

    def rows(self, census, env=None):
        gauge = mock.Mock()
        manager = mock.Mock()
        manager.worker_pods.return_value = census
        manager.env = {} if env is None else env
        manager.metrics = {"factory_worker_pods": gauge}
        metrics_collect.Collector(manager)._pods()
        return gauge.replace.call_args.args[0]

    def test_the_census_and_the_ceiling_are_exported(self):
        rows = self.rows({"live": 7, "final": 2, "unknown": False}, {"FACTORY_MAX_POD_SESSIONS": "30"})
        self.assertEqual(rows, [({"part": "live"}, 7), ({"part": "final"}, 2), ({"part": "ceiling"}, 30)])

    def test_nothing_is_exported_without_a_sweeper_or_when_the_census_cannot_be_read(self):
        self.assertEqual(self.rows(None), [])
        self.assertEqual(self.rows({"live": 0, "final": 0, "unknown": True}), [])

    def test_a_missing_or_bad_ceiling_is_left_out_and_never_guessed(self):
        for env in ({}, {"FACTORY_MAX_POD_SESSIONS": ""}, {"FACTORY_MAX_POD_SESSIONS": "x"}, {"FACTORY_MAX_POD_SESSIONS": "0"}, {"FACTORY_MAX_POD_SESSIONS": "99999"}):
            with self.subTest(env=env):
                rows = self.rows({"live": 1, "final": 0, "unknown": False}, env)
                self.assertEqual([row[0]["part"] for row in rows], ["live", "final"])

    def test_the_dashboard_counts_workers_as_in_process_plus_pods(self):
        import json

        dashboard = json.loads((Path(__file__).resolve().parent.parent / "infra" / "monitoring" / "dashboards" / "factory-overview.json").read_text(encoding="utf-8"))
        panels = {panel["id"]: panel for panel in dashboard["panels"]}
        self.assertIn("factory_worker_pods{part=\"live\"}", panels[4]["targets"][0]["expr"])
        self.assertIn("in-process", panels[5]["title"])
        self.assertEqual([t["expr"] for t in panels[14]["targets"]], ["factory_capacity_workers", "factory_worker_pods"])


class CatalogueTests(unittest.TestCase):
    def test_the_shipped_catalogue_is_sound(self):
        self.assertEqual(metrics.catalogue_problems(metrics.CATALOGUE, metrics.LABEL_VALUES), [])

    def test_every_label_of_every_metric_is_allowed_and_has_a_small_closed_set(self):
        """The cardinality test: an unbounded label (an id, a title, a branch, a login) cannot be declared."""
        for spec in metrics.CATALOGUE:
            for label in spec.labels:
                with self.subTest(metric=spec.name, label=label):
                    self.assertIn(label, metrics.LABEL_NAMES)
                    if label == "repo":
                        continue
                    allowed = metrics.LABEL_VALUES[label]
                    self.assertLessEqual(len(allowed), 32)
                    self.assertEqual(len(set(allowed)), len(allowed))
                    self.assertEqual(allowed[-1], metrics.OTHER)
                    for value in allowed:
                        self.assertRegex(value, r"^[A-Za-z0-9_.-]{1,32}$")

    def test_no_label_name_could_carry_free_text(self):
        for forbidden in ("job", "job_id", "issue", "pr", "title", "body", "comment", "branch", "login", "user", "token", "error", "run"):
            self.assertNotIn(forbidden, metrics.LABEL_NAMES)

    def test_an_unbounded_label_is_refused_when_a_catalogue_is_built(self):
        for label in ("job", "title", "login", "branch"):
            spec = metrics.Spec("factory_bad_total", "counter", "Bad.", (label,))
            with self.subTest(label=label):
                self.assertTrue(metrics.catalogue_problems([spec], metrics.LABEL_VALUES))
                with self.assertRaises(ValueError):
                    metrics.Registry([spec])

    def test_a_label_without_a_closed_value_set_is_refused(self):
        spec = metrics.Spec("factory_bad_total", "counter", "Bad.", ("phase",))
        self.assertTrue(metrics.catalogue_problems([spec], {}))

    def test_naming_rules(self):
        problems = metrics.catalogue_problems(
            [
                metrics.Spec("other_total", "counter", "x"),
                metrics.Spec("factory_c", "counter", "x"),
                metrics.Spec("factory_g_total", "gauge", "x"),
                metrics.Spec("factory_h_total", "histogram", "x", (), (1.0,)),
                metrics.Spec("factory_h2_seconds", "histogram", "x", (), ()),
                metrics.Spec("factory_dup", "gauge", "x"),
                metrics.Spec("factory_dup", "gauge", "x"),
                metrics.Spec("factory_src", "gauge", "x", (), (), "elsewhere"),
            ],
            metrics.LABEL_VALUES,
        )
        text = "\n".join(problems)
        for expected in (
            "other_total does not start with",
            "factory_c: only counters end in _total",
            "factory_g_total: only counters end in _total",
            "factory_h_total: only counters end in _total",
            "factory_h2_seconds needs strictly increasing buckets",
            "factory_dup is declared twice",
            "factory_src has unknown source",
        ):
            self.assertIn(expected, text)

    def test_only_what_the_ledger_and_the_run_files_hold_is_a_counter_or_histogram_from_them(self):
        """A figure over retained runs falls when the janitor purges: it is a gauge, never a counter."""
        for spec in metrics.CATALOGUE:
            if spec.source == metrics.RUNS:
                self.assertEqual(spec.kind, metrics.GAUGE, spec.name)
                self.assertFalse(spec.name.endswith("_total"), spec.name)

    def test_the_vocabularies_are_the_ones_the_code_uses(self):
        values = metrics.LABEL_VALUES
        self.assertEqual(metrics.JOB_STATUSES, ops.JOB_STATUSES)
        self.assertEqual(values["status"], (*ops.JOB_STATUSES, "other"))
        self.assertEqual(values["reason"], (*reason_codes.ORDER, "other"))
        self.assertIn("workflow_hold", values["reason"])
        self.assertNotIn("operator_item", values["reason"])
        self.assertEqual(values["phase"], (*phases.PHASES, "none", "other"))
        self.assertEqual(values["outcome"], (*history.OUTCOMES, "other"))
        self.assertEqual(values["purge_reason"], (*history.PURGE_REASONS, "other"))
        # The API-error vocabulary is a label of its own: the classifier's codes, what only a trip says, then other.
        self.assertEqual(
            values["code"],
            ("spend_limit", "rate_limit", "overloaded", "auth", "rate_limit_unknown", "none", "isolation_failed", "other"),
        )
        self.assertEqual(sorted(values["code"]), sorted({*apierror.CODES, "isolation_failed"}))
        self.assertNotEqual(values["code"], values["reason"])
        # A plan's failure code is a label of its own: `code` is not widened, and the table is the one the plan writes.
        self.assertEqual(values["plan_failure_code"], plan_failure.CODES)
        self.assertEqual(values["plan_failure_code"][-1], "other")
        self.assertNotIn("invalid_plan_paths", values["code"])
        routes = ("healthz", "repos", "trigger-pause", "spend-breaker", "ui", *mgr.READS, *mgr.LOG_READS, *mgr.ACTIONS, "unknown")
        self.assertEqual(sorted(metrics.ROUTES), sorted(routes))
        self.assertEqual(metrics.FALSE_INCOMPLETE_STATES, ("ok", "stale", "checking", "unavailable"))

    def test_every_skip_reason_the_watcher_names_is_in_the_closed_set(self):
        source = (Path(ops.__file__)).read_text()
        named = set(re.findall(r'self\._skip\(repo, "([a-z_-]+)"', source))
        self.assertTrue(named)
        self.assertLessEqual(named, set(metrics.SKIP_REASONS))
        for word in ("in-flight", "ignored", "run_full", "slots_full", "budget", "draining"):
            self.assertIn(word, metrics.SKIP_REASONS)

    def test_every_catalogued_metric_is_documented_and_every_documented_metric_exists(self):
        text = (DOCS / "metrics.md").read_text()
        rows = {}
        for line in text.splitlines():
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if line.startswith("| `factory_") and len(cells) >= 5:
                rows[cells[0].strip("`")] = cells
        declared = {spec.name: spec for spec in metrics.CATALOGUE}
        self.assertEqual(sorted(rows), sorted(declared), "docs/metrics.md and the catalogue differ")
        for name, spec in declared.items():
            with self.subTest(metric=name):
                _, kind, labels, source, meaning, *_ = rows[name]
                self.assertEqual(kind, spec.kind)
                self.assertEqual(labels, ", ".join(spec.labels) if spec.labels else "-")
                self.assertEqual(source, spec.source)
                self.assertTrue(meaning)


    def test_the_closed_label_sets_and_the_links_are_documented(self):
        text = (DOCS / "metrics.md").read_text()
        rows = {}
        for line in text.splitlines():
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) == 2 and cells[0].strip("`") in metrics.LABEL_NAMES and line.startswith("| `"):
                rows[cells[0].strip("`")] = cells[1]
        self.assertEqual(sorted(rows), sorted(metrics.LABEL_NAMES))
        for name, values in metrics.LABEL_VALUES.items():
            self.assertEqual(rows[name], ", ".join(values), name)
        self.assertIn("workflow_hold", rows["reason"])
        for linked in ("operations.md", "manager.md"):
            self.assertIn("docs/metrics.md", (DOCS / linked).read_text(), linked)
        self.assertIn("FACTORY_MANAGER_METRICS_PORT", (DOCS / "manager.md").read_text())

    def test_the_proposed_factory_series_do_not_reuse_the_reason_label(self):
        """One closed set exists per label name: a proposal for a new vocabulary names a new label, never ``reason``."""
        text = (DOCS / "metrics.md").read_text()
        proposed = {
            "factory_session_interruptions_total": "interruption_cause",
            "factory_nap_disruption_blocked_total": "blocked_reason",
        }
        for series, label in proposed.items():
            rows = [line for line in text.splitlines() if line.startswith("|") and f"`{series}`" in line]
            self.assertEqual(len(rows), 1, series)
            labels_cell = [cell.strip() for cell in rows[0].strip().strip("|").split(" | ")][2]
            ours = labels_cell.split("Factory:")[-1]
            self.assertIn(f"`{label}`", ours, series)
            self.assertNotIn("`reason`", ours.replace("not `reason`", "").replace("Not `reason`", ""), series)
            if label in metrics.LABEL_NAMES:
                self.assertIn(label, metrics.LABEL_VALUES, series)
        self.assertNotIn("closed `reason` set", text)


# --- what a scrape reads ---------------------------------------------------------------------------


class AggregationTests(unittest.TestCase):
    """``aggregate_ledger`` over a folded ledger, with no manager."""

    def totals(self, *entries):
        with tempfile.TemporaryDirectory() as temp:
            write_history(temp, *entries)
            runs = history.fold(Ledger(temp).read().events)
        return metrics_collect.aggregate_ledger(runs)

    def test_outcomes_reason_codes_purges_cost_lead_time_and_phase_time(self):
        totals = self.totals(*scenario())
        self.assertEqual(totals.runs, {(REPO, "merged"): 1, (REPO, "abandoned"): 1, (OTHER_REPO, "blocked"): 1})
        self.assertEqual(totals.jobs, {(REPO, "merged"): 1, (REPO, "abandoned"): 1, (OTHER_REPO, "blocked"): 1})
        self.assertEqual(totals.reasons, {(REPO, "ci_failed"): 1})
        self.assertEqual(totals.purges, {(REPO, "terminal"): 1, (REPO, "abandoned"): 1})
        self.assertEqual(totals.cost, {REPO: 2.5, OTHER_REPO: 0.0})
        self.assertEqual(totals.lead, {REPO: [3.0]})
        self.assertEqual(totals.phase, {(REPO, "build"): [1.0]})

    def test_a_job_still_running_has_no_outcome_and_an_open_phase_is_not_counted(self):
        totals = self.totals(
            (0, REPO, "r1", "planned", {"issues": [1], "jobs": [{"id": "a", "issues": [1]}]}),
            (0, REPO, "r1", "job", {"job": "a", "status": "running", "phase": "build", "attempt": 1}),
        )
        self.assertEqual((totals.runs, totals.jobs, totals.phase, totals.lead), ({}, {}, {}, {}))

    def test_a_stretch_longer_than_a_day_is_the_manager_being_down_not_a_phase(self):
        totals = self.totals(
            (0, REPO, "r1", "job", {"job": "a", "status": "running", "phase": "build", "attempt": 1}),
            (3, REPO, "r1", "job", {"job": "a", "status": "interrupted", "attempt": 1}),
        )
        self.assertEqual(totals.phase, {})

    def test_phase_segments_split_on_a_change_of_phase_and_of_status(self):
        job = {
            "timeline": [
                {"at": "2026-01-01T00:00:00Z", "status": "running", "phase": "build"},
                {"at": "2026-01-01T00:00:10Z", "status": "running", "phase": "build"},
                {"at": "2026-01-01T00:01:00Z", "status": "running", "phase": "review"},
                {"at": "2026-01-01T00:03:00Z", "status": "pr_ready", "phase": "review"},
                {"at": "2026-01-01T00:04:00Z", "status": "running", "phase": "verify"},
            ]
        }
        self.assertEqual(metrics_collect.phase_segments(job), [("build", 60.0), ("review", 120.0)])

    def test_the_github_health_text_is_reduced_to_a_closed_state(self):
        for text, expected in (
            ("ok", "ok"),
            ("ambient", "ambient"),
            ("checking", "checking"),
            (f"permission missing: {HOSTILE}", "permission_missing"),
            (f"unavailable: {HOSTILE}", "unavailable"),
            (HOSTILE, "other"),
        ):
            self.assertEqual(metrics_collect.github_state(text), expected)


class ScrapeCase(dt.DashboardCase):
    """A manager on fakes with the metrics listener on a free port and a clock the collector can be moved on."""

    TOKEN = ""  # a subclass sets one to put the listener behind the bearer token

    def setUp(self):
        super().setUp()
        self.serve(metrics_port=0, metrics_token=self.TOKEN)
        self.clock = FakeClock()
        self.manager.collector._clock = self.clock
        self.listener = mgr.make_metrics_server(self.manager)
        self.listener_thread = threading.Thread(target=self.listener.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.listener_thread.start()
        self.addCleanup(self.close_listener)

    def close_listener(self):
        self.listener.shutdown()
        self.listener.server_close()
        self.listener_thread.join(10)

    def fetch(self, method="GET", path="/metrics", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.listener.server_address[1], timeout=30)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read().decode("utf-8"), dict(response.getheaders())
        finally:
            connection.close()

    def scrape(self):
        status, text, _ = self.fetch()
        self.assertEqual(status, 200)
        return text


class LedgerMetricsTests(ScrapeCase):
    def test_the_ledger_feeds_counters_histograms_and_cost_by_repository(self):
        self.write_history(*scenario())
        text = self.scrape()
        self.assertEqual(value_of(text, "factory_runs_finished_total", repo=REPO, outcome="merged"), 1)
        self.assertEqual(value_of(text, "factory_runs_finished_total", repo=REPO, outcome="abandoned"), 1)
        self.assertEqual(value_of(text, "factory_runs_finished_total", repo=OTHER_REPO, outcome="blocked"), 1)
        self.assertEqual(value_of(text, "factory_runs_finished_total", repo=REPO, outcome="closed"), 0)
        self.assertEqual(value_of(text, "factory_jobs_finished_total", repo=REPO, outcome="merged"), 1)
        self.assertEqual(value_of(text, "factory_job_reason_codes_total", repo=REPO, reason="ci_failed"), 1)
        self.assertEqual(value_of(text, "factory_job_reason_codes_total", repo=REPO, reason="workflow_hold"), 0)
        self.assertEqual(value_of(text, "factory_runs_purged_total", repo=REPO, purge_reason="terminal"), 1)
        self.assertEqual(value_of(text, "factory_runs_purged_total", repo=REPO, purge_reason="abandoned"), 1)
        self.assertEqual(value_of(text, "factory_cost_usd_total", repo=REPO), 2.5)
        self.assertEqual(value_of(text, "factory_plan_to_pr_seconds_count", repo=REPO), 1)
        self.assertEqual(value_of(text, "factory_plan_to_pr_seconds_sum", repo=REPO), 3)
        self.assertEqual(value_of(text, "factory_plan_to_pr_seconds_bucket", repo=REPO, le="60"), 1)
        self.assertEqual(value_of(text, "factory_phase_seconds_count", repo=REPO, phase="build"), 1)
        self.assertEqual(value_of(text, "factory_phase_seconds_bucket", repo=REPO, phase="build", le="30"), 1)
        self.assertEqual(value_of(text, "factory_history_truncated"), 0)
        self.assertGreater(value_of(text, "factory_history_bytes"), 0)

    def test_a_purged_run_leaves_the_counters_where_they_were_and_only_the_gauges_fall(self):
        """The reason the ledger is the source: purge removes the run, not the history of what finished."""
        self.write_history(*scenario())
        self.plan_run("r1", status="merged", handoff_status="merged")
        text = self.scrape()
        self.assertEqual(value_of(text, "factory_jobs", repo=REPO, status="merged", phase="none"), 1)
        self.assertEqual(value_of(text, "factory_jobs_finished_total", repo=REPO, outcome="merged"), 1)
        shutil.rmtree(self.run_dir("r1"))  # what the janitor does to a merged run
        self.clock.later()
        text = self.scrape()
        self.assertIsNone(value_of(text, "factory_jobs", repo=REPO, status="merged", phase="none"))
        self.assertEqual(value_of(text, "factory_retained_runs", repo=REPO), 0)
        self.assertEqual(value_of(text, "factory_jobs_finished_total", repo=REPO, outcome="merged"), 1)
        self.assertEqual(value_of(text, "factory_cost_usd_total", repo=REPO), 2.5)

    def test_a_ledger_that_shrinks_never_lowers_a_counter_and_new_work_still_counts(self):
        self.write_history(*scenario())
        before = self.scrape()
        shutil.rmtree(self.data_dir / history.HISTORY_DIR)  # the size cap drops every old segment
        self.clock.later()
        after = self.scrape()
        for name, labels in (
            ("factory_runs_finished_total", {"repo": REPO, "outcome": "merged"}),
            ("factory_cost_usd_total", {"repo": REPO}),
            ("factory_plan_to_pr_seconds_count", {"repo": REPO}),
        ):
            self.assertEqual(value_of(after, name, **labels), value_of(before, name, **labels), name)
        self.assertEqual(value_of(after, "factory_history_bytes"), 0)
        self.write_history((60, REPO, "r-new", "job", {"job": "fix-d", "status": "merged", "phase": "publish", "attempt": 1}))
        self.clock.later()
        self.assertEqual(value_of(self.scrape(), "factory_jobs_finished_total", repo=REPO, outcome="merged"), 2)

    def test_the_aggregate_is_cached_for_a_short_time_so_a_scrape_does_not_refold_the_ledger(self):
        self.write_history(*scenario())
        with mock.patch.object(history, "fold", wraps=history.fold) as fold:
            self.scrape()
            self.scrape()
            self.assertEqual(fold.call_count, 1)
            self.clock.later()
            self.write_history((70, REPO, "r-more", "job", {"job": "fix-e", "status": "closed", "attempt": 1}))
            self.scrape()
            self.assertEqual(fold.call_count, 2)

    def test_a_ledger_read_that_was_cut_says_so(self):
        self.write_history(*scenario())
        with mock.patch.object(history, "DEFAULT_READ_BYTES", 600):
            self.assertEqual(value_of(self.scrape(), "factory_history_truncated"), 1)

    def test_a_repository_that_is_not_configured_never_gets_a_series(self):
        self.write_history((0, "org/ghost", "r1", "job", {"job": "x", "status": "merged", "attempt": 1}))
        text = self.scrape()
        self.assertNotIn("org/ghost", text)
        self.assertEqual(value_of(text, "factory_jobs_finished_total", repo=REPO, outcome="merged"), 0)


def counts(fresh, output, write, read):
    return {
        "input_tokens": fresh,
        "output_tokens": output,
        "cache_creation_input_tokens": write,
        "cache_read_input_tokens": read,
    }


TOKEN_COUNTERS = {
    "input_tokens": "factory_input_tokens_total",
    "output_tokens": "factory_output_tokens_total",
    "cache_creation_input_tokens": "factory_cache_creation_input_tokens_total",
    "cache_read_input_tokens": "factory_cache_read_input_tokens_total",
}


def token_scenario():
    """A planner and two jobs in different phases of org/name, one job of org/other, and a job with no usage data."""
    return [
        (0, REPO, "r-a", "planned", {"issues": [1], "jobs": [{"id": "fix-a", "issues": [1]}], "planner_tokens": counts(1, 2, 3, 4)}),
        (0, REPO, "r-a", "job", {"job": "fix-a", "status": "running", "phase": "build", "attempt": 1, "tokens": {"build": counts(10, 20, 30, 40)}}),
        (
            0,
            REPO,
            "r-a",
            "job",
            {
                "job": "fix-a",
                "status": "pr_ready",
                "phase": "review",
                "attempt": 1,
                "tokens": {"build": counts(100, 200, 300, 400), "review": counts(5, 6, 7, 8)},
            },
        ),
        (0, REPO, "r-a", "job", {"job": "fix-b", "status": "running", "phase": "build", "attempt": 1}),
        (1, OTHER_REPO, "r-o", "job", {"job": "fix-c", "status": "running", "phase": "verify", "attempt": 1, "tokens": {"verify": counts(9, 9, 9, 9)}}),
    ]


class TokenMetricsTests(ScrapeCase):
    """Tokens in and out per phase: ledger counters with the labels repo and phase only (docs/metrics.md)."""

    def figure(self, text, field, phase, repo=REPO):
        return value_of(text, TOKEN_COUNTERS[field], repo=repo, phase=phase)

    def test_the_four_counts_are_counters_by_repository_and_phase(self):
        self.write_history(*token_scenario())
        text = self.scrape()
        expected = {
            ("plan", "input_tokens"): 1,
            ("plan", "cache_read_input_tokens"): 4,
            ("build", "input_tokens"): 100,  # the latest cumulative figure of the job, not the sum of its entries
            ("build", "output_tokens"): 200,
            ("build", "cache_creation_input_tokens"): 300,
            ("build", "cache_read_input_tokens"): 400,
            ("review", "cache_read_input_tokens"): 8,
            ("review", "output_tokens"): 6,
            ("verify", "input_tokens"): 0,
        }
        for (phase, field), figure in expected.items():
            with self.subTest(phase=phase, field=field):
                self.assertEqual(self.figure(text, field, phase), figure)
        self.assertEqual(self.figure(text, "input_tokens", "verify", repo=OTHER_REPO), 9)
        for name in TOKEN_COUNTERS.values():
            self.assertIn(f"# TYPE {name} counter", text)

    def test_the_labels_are_repo_and_phase_only(self):
        for name in TOKEN_COUNTERS.values():
            spec = next(spec for spec in metrics.CATALOGUE if spec.name == name)
            self.assertEqual(spec.labels, ("repo", "phase"))
            self.assertEqual(spec.source, metrics.LEDGER)
            self.assertEqual(spec.kind, metrics.COUNTER)
        self.write_history(*token_scenario())
        for sample, labels, _ in parse(self.scrape()):
            if sample in TOKEN_COUNTERS.values():
                self.assertEqual(sorted(labels), ["phase", "repo"], sample)
                self.assertIn(labels["phase"], metrics.phases.PHASES, sample)

    def test_a_purged_run_does_not_make_a_token_counter_go_backwards(self):
        """The ledger is the source: purging a run removes its state.json, not what the ledger holds."""
        self.write_history(*token_scenario())
        usage = [{"phase": "build", "usage": counts(100, 200, 300, 400)}]
        self.plan_run("r-a", status="pr_ready", worker_phase="build", attempt=1, usage=usage)
        before = self.scrape()
        shutil.rmtree(self.run_dir("r-a"))  # what the janitor does
        self.clock.later()
        after = self.scrape()
        for field in TOKEN_COUNTERS:
            for phase in ("plan", "build", "review"):
                self.assertEqual(self.figure(after, field, phase), self.figure(before, field, phase), (field, phase))
        self.assertEqual(self.figure(after, "cache_read_input_tokens", "build"), 400)

    def test_a_ledger_that_shrinks_never_lowers_them_and_new_usage_still_counts(self):
        self.write_history(*token_scenario())
        before = self.scrape()
        shutil.rmtree(self.data_dir / history.HISTORY_DIR)  # the size cap drops every old segment
        self.clock.later()
        after = self.scrape()
        for field in TOKEN_COUNTERS:
            self.assertEqual(self.figure(after, field, "build"), self.figure(before, field, "build"), field)
        self.write_history((60, REPO, "r-new", "job", {"job": "fix-d", "status": "running", "phase": "build", "attempt": 1, "tokens": {"build": counts(1, 1, 1, 1)}}))
        self.clock.later()
        self.assertEqual(self.figure(self.scrape(), "cache_read_input_tokens", "build"), 401)

    def test_a_deployment_with_no_usage_data_has_zero_counters_and_nothing_else_changes(self):
        self.write_history(*scenario())
        text = self.scrape()
        for field in TOKEN_COUNTERS:
            for phase in metrics.phases.PHASES:
                self.assertEqual(self.figure(text, field, phase), 0, (field, phase))
        self.assertEqual(value_of(text, "factory_cost_usd_total", repo=REPO), 2.5)
        self.assertEqual(value_of(text, "factory_jobs_finished_total", repo=REPO, outcome="merged"), 1)

    def test_a_phase_or_count_outside_the_vocabulary_makes_no_series_and_no_text_gets_through(self):
        hostile = {HOSTILE: counts(1, 1, 1, 1), "elsewhere": counts(1, 1, 1, 1), "build": {**counts(1, 2, 3, 4), "secret": 7, "input_tokens": True}}
        self.write_history(
            (0, REPO, "r-h", "job", {"job": "fix-a", "status": "running", "phase": "build", "attempt": 1, "tokens": hostile}),
            (0, REPO, "r-h", "planned", {"issues": [1], "jobs": [{"id": "fix-a", "issues": [1]}], "planner_tokens": {"input_tokens": HOSTILE, "output_tokens": -1}}),
        )
        text = self.scrape()
        self.assertEqual(self.figure(text, "output_tokens", "build"), 2)
        self.assertEqual(self.figure(text, "input_tokens", "build"), 0)
        self.assertEqual(self.figure(text, "output_tokens", "plan"), 0)
        phases_seen = {labels["phase"] for sample, labels, _ in parse(text) if sample in TOKEN_COUNTERS.values()}
        self.assertEqual(phases_seen, set(metrics.phases.PHASES))
        self.assertNotIn("elsewhere", text)
        self.assertNotIn("secret", text)
        for part in HOSTILE_PARTS:
            self.assertNotIn(part, text)


class RetainedRunMetricsTests(ScrapeCase):
    USAGE = [
        {"phase": "build", "total_cost_usd": 1.5, "footprint": {"peak_rss_mb": 512, "cpu_seconds": 3.0}},
        {"phase": "review", "total_cost_usd": 0.25, "footprint": {"peak_rss_mb": 256, "cpu_seconds": 1.0}},
    ]

    def test_jobs_cost_footprint_and_budget_of_the_retained_runs_are_gauges(self):
        (self.config_dir / "repos" / "org__name.toml").write_text(f'repo = "{REPO}"\nrun_budget_usd = 5\n')
        self.plan_run("r1", status="running", worker_phase="build", phase="build", attempt=1, usage=self.USAGE)
        text = self.scrape()
        self.assertEqual(value_of(text, "factory_jobs", repo=REPO, status="running", phase="build"), 1)
        self.assertEqual(value_of(text, "factory_retained_cost_usd", repo=REPO, phase="build"), 1.5)
        self.assertEqual(value_of(text, "factory_retained_cost_usd", repo=REPO, phase="review"), 0.25)
        self.assertEqual(value_of(text, "factory_run_budget_usd", repo=REPO), 5)
        self.assertEqual(value_of(text, "factory_run_spent_usd_max", repo=REPO), 1.75)
        self.assertEqual(value_of(text, "factory_phase_peak_rss_mib", repo=REPO, phase="build"), 512)
        self.assertEqual(value_of(text, "factory_phase_p95_rss_mib", repo=REPO, phase="review"), 256)
        self.assertEqual(value_of(text, "factory_retained_runs", repo=REPO), 1)
        self.assertEqual(value_of(text, "factory_retained_runs_truncated"), 0)
        self.assertIsNone(value_of(text, "factory_run_budget_usd", repo=OTHER_REPO), "no budget configured: no series")

    def test_the_run_gauges_are_not_counters_in_name_type_or_behaviour(self):
        self.plan_run("r1", status="running", worker_phase="build", attempt=1, usage=self.USAGE)
        text = self.scrape()
        for name in ("factory_jobs", "factory_retained_cost_usd", "factory_run_spent_usd_max"):
            self.assertIn(f"# TYPE {name} gauge", text)
        self.assertNotIn("# TYPE factory_jobs counter", text)

    @staticmethod
    def waiting_for_human_matcher():
        """The ``status`` regex of the FactoryJobsWaitingForHuman rule in infra/monitoring/alert-rules.json."""
        rules = json.loads((Path(__file__).resolve().parent.parent / "infra" / "monitoring" / "alert-rules.json").read_text())
        exprs = [rule["expr"] for group in rules["groups"] for rule in group["rules"] if rule.get("alert") == "FactoryJobsWaitingForHuman"]
        assert len(exprs) == 1
        found = re.search(r'factory_jobs\{status=~"([^"]*)"\}', exprs[0])
        assert found, exprs[0]
        return re.compile(found.group(1))

    def test_a_pr_ready_job_whose_pull_request_merged_is_not_waiting_for_a_person(self):
        """``reconcile`` never rewrites ``state.json``: the merged job keeps reading ``pr_ready`` until its run is purged.

        The alert counts the raw ``status`` of ``factory_jobs`` and selects ``needs_human_publish|blocked`` only, so
        such a job is a ``pr_ready`` series that the rule's matcher (anchored, as in PromQL) does not select.
        """
        self.plan_run("r1", status="pr_ready", pr=PR, handoff="merged")
        text = self.scrape()
        self.assertEqual(value_of(text, "factory_jobs", repo=REPO, status="pr_ready", phase="none"), 1)
        matcher = self.waiting_for_human_matcher()
        waiting = [
            labels for labels in series_of(text, "factory_jobs") if matcher.fullmatch(labels["status"])
        ]
        self.assertEqual(waiting, [], "a run whose only job is pr_ready with a merged pull request adds nothing to the alert")
        # The same rule does count what a person has to act on.
        for status in ("needs_human_publish", "blocked"):
            self.assertTrue(matcher.fullmatch(status), status)
        for status in ("pr_ready", "pr_incomplete", "merged"):
            self.assertFalse(matcher.fullmatch(status), status)

    def test_a_status_or_phase_that_is_not_in_the_closed_set_is_other(self):
        self.plan_run("r1", status=HOSTILE, worker_phase=HOSTILE, attempt=1, usage=[{"phase": HOSTILE, "total_cost_usd": 1}])
        text = self.scrape()
        self.assertEqual(value_of(text, "factory_jobs", repo=REPO, status="other", phase="other"), 1)
        self.assertEqual(value_of(text, "factory_retained_cost_usd", repo=REPO, phase="other"), 1)
        for part in HOSTILE_PARTS:
            self.assertNotIn(part, text)

    def test_the_number_of_runs_read_per_refresh_is_bounded_and_says_so(self):
        for number in range(3):
            self.plan_run(f"r{number}", status="running", worker_phase="build", attempt=1)
        with mock.patch.object(metrics_collect, "MAX_RUNS_PER_REPO", 2):
            text = self.scrape()
        self.assertEqual(value_of(text, "factory_retained_runs", repo=REPO), 2)
        self.assertEqual(value_of(text, "factory_retained_runs_truncated"), 1)

    def test_a_state_file_that_is_not_what_it_should_be_costs_nothing(self):
        directory = self.plan_run("r1", started=False)
        (directory / "state.json").write_text(json.dumps({"jobs": [1, 2]}))
        self.plan_run("r2", started=False)
        (self.run_dir("r2") / "state.json").write_text("not json")
        text = self.scrape()
        self.assertEqual(value_of(text, "factory_retained_runs", repo=REPO), 2)
        self.assertEqual(series_of(text, "factory_jobs"), [])
        self.assertIsNone(value_of(text, "factory_metrics_scrape_errors_total"))


class LiveMetricsTests(ScrapeCase):
    def test_requests_are_counted_by_route_method_and_status_class_with_their_duration(self):
        self.call("GET", "/healthz", principal=None)
        self.call("GET", "/repos", principal=tm.VIEWER)
        self.call("GET", "/repos", principal=None)
        self.call("POST", "/repos", principal=tm.VIEWER)
        self.call("GET", "/no/such/path", principal=tm.VIEWER)
        self.call("DELETE", "/repos", principal=tm.OPERATOR)
        text = self.scrape()
        total = "factory_manager_requests_total"
        self.assertEqual(value_of(text, total, route="healthz", method="GET", status_class="2xx"), 1)
        self.assertEqual(value_of(text, total, route="repos", method="GET", status_class="2xx"), 1)
        self.assertEqual(value_of(text, total, route="repos", method="GET", status_class="4xx"), 1)
        self.assertEqual(value_of(text, total, route="repos", method="POST", status_class="4xx"), 1)
        self.assertEqual(value_of(text, total, route="unknown", method="GET", status_class="4xx"), 1)
        self.assertEqual(value_of(text, total, route="repos", method="other", status_class="4xx"), 1)
        self.assertEqual(value_of(text, "factory_manager_request_seconds_count", route="repos"), 4)
        self.assertEqual(value_of(text, "factory_manager_request_seconds_bucket", route="repos", le="+Inf"), 4)

    def test_401_and_403_are_counted_apart(self):
        self.call("GET", "/repos", principal=None)
        self.call("GET", "/repos", principal=None)
        self.call("GET", "/repos", principal=tm.NO_ROLE)
        self.call("POST", "/trigger-pause", body={"paused": True}, principal=tm.VIEWER)
        text = self.scrape()
        self.assertEqual(value_of(text, "factory_manager_auth_failures_total", http_status="401"), 2)
        self.assertEqual(value_of(text, "factory_manager_auth_failures_total", http_status="403"), 2)

    def test_the_request_path_never_becomes_a_label(self):
        self.call("GET", f"/repos/{HOSTILE.splitlines()[0].replace(' ', '%20')}/x", principal=tm.VIEWER)
        self.call("GET", "/repos/org/name/runs/r1/status", principal=tm.VIEWER)
        text = self.scrape()
        self.assertEqual(
            sorted({found["route"] for found in series_of(text, "factory_manager_requests_total")}), ["status", "unknown"]
        )
        self.assertNotIn("Ignore", text)

    def test_children_the_pause_and_the_runner_are_read_from_memory(self):
        self.manager.runner.live = 3
        self.manager.set_trigger_pause(True)
        text = self.scrape()
        self.assertEqual(value_of(text, "factory_manager_commands_running"), 3)
        self.assertEqual(value_of(text, "factory_trigger_paused"), 1)
        self.assertEqual([value_of(text, "factory_manager_children", kind=kind) for kind in ("plan", "run", "append")], [0, 0, 0])
        self.manager.set_trigger_pause(False)
        self.clock.later()
        self.assertEqual(value_of(self.scrape(), "factory_trigger_paused"), 0)

    def test_the_children_gauge_counts_each_kind_of_live_child(self):
        for pid, (kind, repo, run) in enumerate((("run", "org/name", "r1"), ("run", "org/other", "r2"), ("plan", "org/name", "r3"))):
            self.manager.children.register(mgr.Child(kind, repo, run, tm.FakeProcess(7100 + pid), "2026-01-01T00:00:00Z"))
        text = self.scrape()
        counts = {kind: value_of(text, "factory_manager_children", kind=kind) for kind in ("plan", "run", "append")}
        self.assertEqual(counts, {"plan": 1, "run": 2, "append": 0})

    def test_the_github_health_is_one_hot_per_repository_over_a_closed_set(self):
        self.manager.github_health._states[REPO] = f"permission missing: {HOSTILE}"
        with mock.patch.object(github_health.github_auth, "app_configured", return_value=True):
            text = self.scrape()
        states = {found["state"]: v for (n, found, v) in parse(text) if n == "factory_github_app_state" and found["repo"] == REPO}
        self.assertEqual({name for name, v in states.items() if v == "1"}, {"permission_missing"})
        self.assertEqual(set(states), {"ok", "ambient", "checking", "permission_missing", "unavailable", "other"})
        self.assertEqual(value_of(text, "factory_github_app_state", repo=OTHER_REPO, state="checking"), 1)
        for part in HOSTILE_PARTS:
            self.assertNotIn(part, text)

    def test_the_false_incomplete_summary_is_gauges_with_the_rate_only_when_there_is_one(self):
        now = datetime(2026, 1, 31, tzinfo=timezone.utc)
        outcomes = [(True, "ci_failed")] * 8 + [(False, "ci_failed")] * 2 + [(True, "workflow_hold")] * 3
        self.manager.false_incomplete.set(REPO, false_incomplete.summarize(outcomes, 2, now - timedelta(days=30), now, False))
        text = self.scrape()
        self.assertEqual(value_of(text, "factory_false_incomplete_state", repo=REPO, state="ok"), 1)
        self.assertEqual(value_of(text, "factory_false_incomplete_state", repo=OTHER_REPO, state="checking"), 1)
        self.assertEqual(value_of(text, "factory_false_incomplete_prs", repo=REPO, count="counted"), 10)
        self.assertEqual(value_of(text, "factory_false_incomplete_prs", repo=REPO, count="workflow_hold"), 3)
        self.assertEqual(value_of(text, "factory_false_incomplete_prs", repo=REPO, count="closed_unmerged"), 2)
        self.assertEqual(value_of(text, "factory_false_incomplete_reason_prs", repo=REPO, reason="ci_failed"), 10)
        self.assertEqual(value_of(text, "factory_false_incomplete_reason_prs", repo=REPO, reason="workflow_hold"), 3)
        self.assertEqual(value_of(text, "factory_false_incomplete_rate", repo=REPO), 0.8)
        self.assertIsNone(value_of(text, "factory_false_incomplete_rate", repo=OTHER_REPO))
        self.assertEqual({found["repo"] for found in series_of(text, "factory_false_incomplete_prs")}, {REPO})

    def test_the_worker_budget_and_the_disk_are_read_without_writing(self):
        snapshot = {"total": 4, "in_use": 1, "free": 3, "holders": [], "waiting": [{"repo": REPO, "job": "x", "phase": "build", "since": "1"}]}
        with mock.patch.object(metrics_collect.capacity, "snapshot_from_env", return_value=snapshot):
            text = self.scrape()
        self.assertEqual([value_of(text, "factory_capacity_workers", part=part) for part in ("total", "in_use", "waiting")], [4, 1, 1])
        self.assertGreater(value_of(text, "factory_data_disk_bytes", part="total"), 0)
        used, free = (value_of(text, "factory_data_disk_bytes", part=part) for part in ("used", "free"))
        self.assertLessEqual(sum([used, free]), value_of(text, "factory_data_disk_bytes", part="total") * 1.01)
        self.clock.later()
        self.assertEqual(series_of(self.scrape(), "factory_capacity_workers"), [], "no pool: no series")

    def test_a_collector_that_fails_costs_its_own_numbers_not_the_scrape(self):
        with mock.patch.object(metrics_collect.shutil, "disk_usage", side_effect=RuntimeError(HOSTILE)):
            text = self.scrape()
        self.assertEqual(value_of(text, "factory_metrics_scrape_errors_total"), 1)
        self.assertIsNotNone(value_of(text, "factory_trigger_paused"))
        for part in HOSTILE_PARTS:
            self.assertNotIn(part, text)

    def test_a_scrape_starts_no_process_and_makes_no_github_call(self):
        self.write_history(*scenario())
        self.plan_run("r1", status="running", worker_phase="build", attempt=1)
        with mock.patch("subprocess.Popen", side_effect=AssertionError("a scrape started a process")), mock.patch(
            "socket.create_connection", side_effect=AssertionError("a scrape made a network call")
        ):
            self.assertIn("factory_jobs", self.manager.scrape_metrics())
        self.assertEqual((self.runner.calls, self.spawner.calls), ([], []))

    def test_the_key_refresh_failure_is_counted_and_still_warned_with_the_same_words(self):
        self.manager._key_refresh_failed("fetch")
        self.assertIn("signing key refresh failed (fetch)", self.err.getvalue())
        self.assertEqual(value_of(self.scrape(), "factory_manager_jwks_refresh_failures_total"), 1)


# --- the listener ------------------------------------------------------------------------------------


class ListenerTests(ScrapeCase):
    def test_get_metrics_answers_the_text_format_without_any_identity(self):
        status, text, headers = self.fetch()
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/plain; version=0.0.4; charset=utf-8")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("# TYPE factory_manager_requests_total counter", text)
        self.assertEqual(self.call("GET", "/repos", principal=None)[0], 401, "the API still wants an identity")

    def test_a_query_string_is_ignored_and_any_other_path_is_404(self):
        self.assertEqual(self.fetch(path="/metrics?x=1")[0], 200)
        for path in ("/", "/healthz", "/metrics/", "/metrics/x", "/dashboard", "/repos", "/history/runs", "/%6detrics"):
            with self.subTest(path=path):
                status, text, _ = self.fetch(path=path)
                self.assertEqual((status, text), (404, "404\n"))

    def test_every_other_method_is_405_with_allow_get(self):
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"):
            with self.subTest(method=method):
                status, _, headers = self.fetch(method)
                self.assertEqual((status, headers["Allow"]), (405, "GET"))

    def test_a_malformed_request_gets_a_plain_answer_not_a_traceback(self):
        with socket.create_connection(("127.0.0.1", self.listener.server_address[1]), timeout=10) as connection:
            connection.sendall(b"BREW /metrics HTTP/1.1\r\n\r\n")
            connection.shutdown(socket.SHUT_WR)
            answer = connection.recv(4096)
        self.assertTrue(answer.startswith(b"HTTP/1.0 501"), answer)
        self.assertIn(b"text/plain", answer)
        self.assertEqual(self.err.getvalue(), "")

    def test_the_listener_writes_nothing_to_the_audit_trail_or_the_logs(self):
        before = (self.out.getvalue(), self.err.getvalue())
        for _ in range(3):
            self.fetch()
        self.fetch(path="/nope")
        self.fetch("POST")
        self.assertEqual((self.out.getvalue(), self.err.getvalue()), before)
        self.assertEqual(self.audit_lines(), [])

    def test_the_main_port_has_no_metrics_path(self):
        self.assertEqual(self.call("GET", "/metrics", principal=tm.OPERATOR)[0], 404)
        self.assertEqual(self.call("GET", "/metrics", principal=None)[0], 401)

    def test_a_scrape_that_fails_as_a_whole_is_a_plain_500_without_its_message(self):
        self.listener.scrape = mock.Mock(side_effect=RuntimeError(HOSTILE))
        status, text, _ = self.fetch()
        self.assertEqual((status, text), (500, "500\n"))
        self.assertEqual(self.fetch("POST")[0], 405, "the listener still answers")
        taken = [self.listener.slots.acquire(blocking=False) for _ in range(metrics.MAX_CONNECTIONS)]
        for _ in taken:
            self.listener.slots.release()
        self.assertTrue(all(taken), "the failed scrape gave its slot back")

    def test_connections_beyond_the_cap_are_refused_with_503(self):
        slots = self.listener.slots
        taken = [slots.acquire(blocking=False) for _ in range(metrics.MAX_CONNECTIONS)]
        try:
            status, _, headers = self.fetch()
            self.assertEqual((status, headers["Retry-After"]), (503, "5"))
        finally:
            for _ in taken:
                slots.release()
        self.assertEqual(self.fetch()[0], 200)


class TokenListenerTests(ScrapeCase):
    """The listener behind ``FACTORY_MANAGER_METRICS_TOKEN``: a bearer token, 401 without it, and the token nowhere else."""

    TOKEN = LISTENER_TOKEN

    def bearer(self, value):
        return {"Authorization": value}

    def test_a_request_without_the_token_is_401_with_a_challenge_and_costs_no_scrape(self):
        self.manager.scrape_metrics = mock.Mock(side_effect=AssertionError("an unauthorised request scraped"))
        for headers in (
            {},
            self.bearer(""),
            self.bearer("Bearer"),
            self.bearer("Bearer "),
            self.bearer(f"Bearer {self.TOKEN}x"),
            self.bearer(f"Bearer {self.TOKEN[:-1]}"),
            self.bearer(f"Bearer {'a' * 5000}"),
            self.bearer(f"Basic {self.TOKEN}"),
            self.bearer(self.TOKEN),
        ):
            with self.subTest(headers=headers):
                status, text, answer = self.fetch(headers=headers)
                self.assertEqual((status, text), (401, "401\n"))
                self.assertEqual(answer["WWW-Authenticate"], 'Bearer realm="factory-metrics"')
                self.assertEqual(answer["Cache-Control"], "no-store")

    def test_the_right_token_gets_the_numbers_and_the_scheme_is_case_insensitive(self):
        for scheme in ("Bearer", "bearer", "BEARER"):
            with self.subTest(scheme=scheme):
                status, text, headers = self.fetch(headers=self.bearer(f"{scheme} {self.TOKEN}"))
                self.assertEqual(status, 200)
                self.assertEqual(headers["Content-Type"], metrics.CONTENT_TYPE)
                self.assertIn("# TYPE factory_manager_requests_total counter", text)

    def test_an_unknown_path_is_401_without_the_token_and_404_with_it(self):
        self.assertEqual(self.fetch(path="/healthz")[0], 401)
        self.assertEqual(self.fetch(path="/nope", headers=self.bearer(f"Bearer {self.TOKEN}"))[0], 404)

    def test_the_token_is_compared_with_a_constant_time_comparison(self):
        with mock.patch.object(metrics.hmac, "compare_digest", wraps=hmac.compare_digest) as spy:
            self.assertEqual(self.fetch(headers=self.bearer(f"Bearer {self.TOKEN[:-1]}y"))[0], 401)
        self.assertEqual(spy.call_count, 1)
        compared = spy.call_args.args
        self.assertTrue(all(isinstance(value, bytes) and len(value) == 32 for value in compared), "digests, not the token")

    def test_the_token_is_never_written_or_exposed(self):
        self.fetch(headers=self.bearer(f"Bearer {self.TOKEN}"))
        self.fetch(headers=self.bearer("Bearer wrong"))
        self.fetch()
        _, text, headers = self.fetch(headers=self.bearer(f"Bearer {self.TOKEN}"))
        for seen in (self.out.getvalue(), self.err.getvalue(), text, repr(self.manager.settings), repr(self.listener.__dict__), str(headers)):
            self.assertNotIn(self.TOKEN, seen)
        self.assertEqual(self.audit_lines(), [])
        self.assertNotIn(self.TOKEN, mgr.startup_line(self.server, self.manager, self.listener))
        self.assertTrue(mgr.startup_line(self.server, self.manager, self.listener).endswith(" metrics_auth=bearer"))

    def test_other_methods_are_still_405_and_the_api_is_unchanged(self):
        self.assertEqual(self.fetch("POST")[0], 405)
        self.assertEqual(self.call("GET", "/metrics", principal=None)[0], 401)
        self.assertEqual(self.call("GET", "/repos", principal=tm.OPERATOR)[0], 200)

    def test_without_a_token_nothing_is_asked_for_and_the_startup_line_does_not_claim_one(self):
        listener = metrics.MetricsServer(("127.0.0.1", 0), lambda: "", token="")
        self.addCleanup(listener.server_close)
        self.assertFalse(listener.requires_token)
        self.assertNotIn("metrics_auth", mgr.startup_line(self.server, self.manager, listener))


class SettingsTests(unittest.TestCase):
    ENV = {"FACTORY_CONFIG": "/etc/factory", "FACTORY_DATA": "/var/lib/factory"}

    def settings(self, **extra):
        return Settings.from_env({**self.ENV, **extra})

    def test_the_listener_is_off_unless_a_port_is_set(self):
        for value in (None, "", "  ", "off", "OFF"):
            with self.subTest(value=value):
                env = {} if value is None else {"FACTORY_MANAGER_METRICS_PORT": value}
                self.assertIsNone(self.settings(**env).metrics_port)
        self.assertEqual(self.settings().metrics_host, "127.0.0.1")

    def test_a_port_and_a_host_are_read_and_checked(self):
        found = self.settings(FACTORY_MANAGER_METRICS_PORT="9100", FACTORY_MANAGER_METRICS_HOST="0.0.0.0", FACTORY_MANAGER_METRICS_TOKEN=LISTENER_TOKEN)
        self.assertEqual((found.metrics_port, found.metrics_host, found.metrics_token), (9100, "0.0.0.0", LISTENER_TOKEN))
        self.assertEqual(self.settings(FACTORY_MANAGER_METRICS_PORT="0").metrics_port, 0)
        for bad in ({"FACTORY_MANAGER_METRICS_PORT": "abc"}, {"FACTORY_MANAGER_METRICS_PORT": "70000"}, {"FACTORY_MANAGER_METRICS_PORT": "9100.5"}, {"FACTORY_MANAGER_METRICS_PORT": "-1"}, {"FACTORY_MANAGER_METRICS_HOST": "a b"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.settings(**bad)

    def test_the_metrics_port_may_not_be_the_api_port(self):
        with self.assertRaisesRegex(ValueError, "must differ from FACTORY_MANAGER_PORT"):
            self.settings(FACTORY_MANAGER_PORT="9100", FACTORY_MANAGER_METRICS_PORT="9100")
        self.assertEqual(self.settings(FACTORY_MANAGER_PORT="9100", FACTORY_MANAGER_METRICS_PORT="9101").metrics_port, 9101)
        self.assertEqual(self.settings(FACTORY_MANAGER_PORT="0", FACTORY_MANAGER_METRICS_PORT="0").metrics_port, 0)

    def test_a_listener_off_the_loopback_needs_a_token_and_the_error_never_carries_one(self):
        for host in ("0.0.0.0", "::", "10.244.1.7", "metrics.example.test", "127.0.0.1.example.test"):
            with self.subTest(host=host):
                with self.assertRaises(ValueError) as caught:
                    self.settings(FACTORY_MANAGER_METRICS_PORT="9464", FACTORY_MANAGER_METRICS_HOST=host)
                self.assertEqual(str(caught.exception), "FACTORY_MANAGER_METRICS_TOKEN is required when FACTORY_MANAGER_METRICS_HOST is not a loopback address")
                self.assertEqual(self.settings(FACTORY_MANAGER_METRICS_PORT="9464", FACTORY_MANAGER_METRICS_HOST=host, FACTORY_MANAGER_METRICS_TOKEN=LISTENER_TOKEN).metrics_host, host)
        for host in ("127.0.0.1", "127.1.2.3", "::1", "localhost", "LOCALHOST"):
            with self.subTest(host=host):
                self.assertEqual(self.settings(FACTORY_MANAGER_METRICS_PORT="9464", FACTORY_MANAGER_METRICS_HOST=host).metrics_token, "")

    def test_with_the_listener_off_a_wide_host_needs_nothing(self):
        self.assertIsNone(self.settings(FACTORY_MANAGER_METRICS_HOST="0.0.0.0").metrics_port)

    def test_a_token_is_trimmed_checked_and_kept_out_of_repr(self):
        found = self.settings(FACTORY_MANAGER_METRICS_PORT="9464", FACTORY_MANAGER_METRICS_TOKEN=f"{LISTENER_TOKEN}\n")
        self.assertEqual(found.metrics_token, LISTENER_TOKEN)
        self.assertNotIn(LISTENER_TOKEN, repr(found))
        for bad in ("short", "x" * 31, "x" * 513, f"{LISTENER_TOKEN} {LISTENER_TOKEN}", f"{LISTENER_TOKEN}\u00e9", f"{LISTENER_TOKEN}\t{LISTENER_TOKEN}"):
            with self.subTest(bad=bad[:12]):
                with self.assertRaises(ValueError) as caught:
                    self.settings(FACTORY_MANAGER_METRICS_PORT="9464", FACTORY_MANAGER_METRICS_TOKEN=bad)
                self.assertEqual(str(caught.exception), "FACTORY_MANAGER_METRICS_TOKEN must be 32 to 512 printable characters without spaces")
                self.assertNotIn(bad, str(caught.exception))

    def test_main_refuses_a_wide_listener_without_a_token_before_listening_on_anything(self):
        stderr = io.StringIO()
        env = {**self.ENV, "FACTORY_MANAGER_PORT": "0", "FACTORY_MANAGER_METRICS_PORT": "0", "FACTORY_MANAGER_METRICS_HOST": "0.0.0.0"}
        with mock.patch.object(sys, "stderr", stderr), mock.patch.object(mgr, "make_server") as make:
            self.assertEqual(mgr.main([], env), 2)
        make.assert_not_called()
        self.assertEqual(stderr.getvalue(), "factory-manager: FACTORY_MANAGER_METRICS_TOKEN is required when FACTORY_MANAGER_METRICS_HOST is not a loopback address\n")

    def test_no_child_process_inherits_the_token(self):
        manager = mgr.Manager(self.settings(), env={**self.ENV, "FACTORY_MANAGER_METRICS_TOKEN": LISTENER_TOKEN, "KEEP": "1"})
        self.assertEqual(manager.env.get("KEEP"), "1")
        self.assertNotIn("FACTORY_MANAGER_METRICS_TOKEN", manager.env)
        self.assertNotIn("FACTORY_MANAGER_METRICS_TOKEN", manager.runner._env)
        self.assertNotIn("FACTORY_MANAGER_METRICS_TOKEN", manager.spawner._env)
        with mock.patch.dict(os.environ, {"FACTORY_MANAGER_METRICS_TOKEN": LISTENER_TOKEN}):
            self.assertNotIn("FACTORY_MANAGER_METRICS_TOKEN", shell.supervisor_env())


    def test_main_refuses_a_clash_before_listening_on_anything(self):
        stderr = io.StringIO()
        env = {**self.ENV, "FACTORY_MANAGER_PORT": "9100", "FACTORY_MANAGER_METRICS_PORT": "9100"}
        with mock.patch.object(sys, "stderr", stderr):
            self.assertEqual(mgr.main([], env), 2)
        self.assertIn("FACTORY_MANAGER_METRICS_PORT must differ from FACTORY_MANAGER_PORT", stderr.getvalue())


class LifecycleTests(tm.ServerCase):
    def test_off_by_default_nothing_listens_and_the_startup_line_says_nothing_of_it(self):
        self.assertIsNone(mgr.make_metrics_server(self.manager))
        self.assertNotIn("metrics", mgr.startup_line(self.server, self.manager))

    def test_serve_runs_the_listener_beside_the_api_and_closes_it_at_shutdown(self):
        self.serve(metrics_port=0)
        listener = mgr.make_metrics_server(self.manager)
        port = listener.server_address[1]
        server = mgr.make_server(self.manager)
        seen = {}

        def scrape_then_stop():
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            connection.request("GET", "/metrics")
            seen["status"] = connection.getresponse().status
            connection.close()
            server.shutdown()

        threading.Thread(target=scrape_then_stop, daemon=True).start()
        with mock.patch.object(mgr.signal, "signal"):
            self.assertEqual(mgr.serve(self.manager, server, listener), 0)
        self.assertEqual(seen, {"status": 200})
        self.assertEqual(listener.socket.fileno(), -1, "the metrics socket is closed")
        first = self.out.getvalue().splitlines()[0]
        self.assertRegex(first, rf"^factory-manager listening host=127\.0\.0\.1 port=\d+ repos=2 metrics_port={port}$")

    def test_the_listener_binds_the_loopback_by_default(self):
        self.serve(metrics_port=0)
        listener = mgr.make_metrics_server(self.manager)
        self.addCleanup(listener.server_close)
        self.assertEqual(listener.server_address[0], "127.0.0.1")

    def test_main_stops_with_2_when_the_metrics_port_is_taken_and_leaves_no_listener(self):
        taken = socket.socket()
        self.addCleanup(taken.close)
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        env = {
            "FACTORY_CONFIG": str(self.config_dir),
            "FACTORY_DATA": str(self.data_dir),
            "FACTORY_MANAGER_PORT": "0",
            "FACTORY_MANAGER_METRICS_PORT": str(taken.getsockname()[1]),
        }
        stderr = io.StringIO()
        with mock.patch.object(sys, "stderr", stderr):
            self.assertEqual(mgr.main([], env), 2)
        self.assertIn("cannot listen for metrics on 127.0.0.1:", stderr.getvalue())


# --- the watcher and the guardrail ----------------------------------------------------------------------


class WatcherMetricsTests(ttl.TriggerCase):
    def setUp(self):
        super().setUp()
        self.manager.operations = self.ops

    def scrape(self):
        return self.manager.scrape_metrics()

    def test_passes_skips_and_failures_are_counted_and_the_backoff_is_a_live_gauge(self):
        self.reader.label(7, ttl.LABEL, actor=("mallory", "User"))
        self.ops.trigger_pass()
        text = self.scrape()
        self.assertEqual(value_of(text, "factory_watcher_passes_total"), 1)
        self.assertEqual(value_of(text, "factory_watcher_skips_total", repo=REPO, skip_reason="unauthorised"), 1)
        self.reader.fail = RuntimeError(ttl.CANARY)
        self.ops.trigger_pass()
        text = self.scrape()
        self.assertEqual(value_of(text, "factory_watcher_failures_total", repo=REPO), 1)
        self.assertEqual(value_of(text, "factory_watcher_backoff_seconds", repo=REPO), 60)
        self.ops.trigger_pass()
        self.assertEqual(value_of(self.scrape(), "factory_watcher_skips_total", repo=REPO, skip_reason="backoff"), 1)
        self.time.now += 61
        self.assertEqual(value_of(self.scrape(), "factory_watcher_backoff_seconds", repo=REPO), 0)
        self.assertNotIn(ttl.CANARY, text)

    def failed_plan(self, code, updated="1"):
        """Drive one plan through the trigger loop to a failure; ``code`` is written to its run directory (None: no file)."""
        self.time.now += 4000  # past any backoff
        self.reader.label(7, ttl.LABEL, updated=updated)  # an edit each time: the same code twice would hold the set
        self.ops.trigger_pass()
        argv = self.spawned("plan")[-1]
        directory = self.run_dir(self.run_of(argv))
        directory.mkdir(parents=True, exist_ok=True)
        if code is not None:
            (directory / plan_failure.FILE).write_text(code)
        self.finish(argv, 1)
        self.ops.trigger_pass()

    def test_a_failed_plan_is_counted_once_under_the_code_it_recorded(self):
        for position, code in enumerate(plan_failure.CODES):
            with self.subTest(code=code):
                before = value_of(self.scrape(), "factory_plan_failures_total", repo=REPO, plan_failure_code=code) or 0
                self.failed_plan(code, updated=f"c{position}")
                text = self.scrape()
                self.assertEqual(value_of(text, "factory_plan_failures_total", repo=REPO, plan_failure_code=code), before + 1)
        total = sum(float(value) for sample, _, value in parse(self.scrape()) if sample == "factory_plan_failures_total")
        self.assertEqual(total, len(plan_failure.CODES))

    def test_a_missing_or_unknown_plan_failure_code_is_counted_as_other(self):
        for position, code in enumerate((None, "made_up_code", f"timeout {ttl.CANARY}")):
            with self.subTest(code=code):
                before = value_of(self.scrape(), "factory_plan_failures_total", repo=REPO, plan_failure_code="other") or 0
                self.failed_plan(code, updated=f"u{position}")
                text = self.scrape()
                self.assertEqual(value_of(text, "factory_plan_failures_total", repo=REPO, plan_failure_code="other"), before + 1)
                self.assertNotIn(ttl.CANARY, text)
        self.assertEqual(series_of(self.scrape(), "factory_plan_failures_total"), [{"repo": REPO, "plan_failure_code": "other"}])

    def test_a_paused_watcher_counts_the_paused_pass_and_not_a_pass(self):
        self.manager.set_trigger_pause(True)
        self.ops.trigger_pass()
        text = self.scrape()
        self.assertEqual(value_of(text, "factory_watcher_paused_passes_total"), 1)
        self.assertIsNone(value_of(text, "factory_watcher_passes_total"))

    def test_a_skip_is_counted_even_when_its_log_line_is_rate_limited(self):
        self.configure("auto_build = false\n")
        for _ in range(3):
            self.ops.trigger_pass()
        self.assertEqual(self.trigger_lines(), [f"trigger repo={REPO} skipped reason=disabled"])  # said once, ever
        self.assertEqual(value_of(self.scrape(), "factory_watcher_skips_total", repo=REPO, skip_reason="disabled"), 3)

    def test_a_skip_reason_outside_the_closed_set_is_other(self):
        self.ops._skip(REPO, HOSTILE)
        self.assertEqual(value_of(self.scrape(), "factory_watcher_skips_total", repo=REPO, skip_reason="other"), 1)

    def test_a_drain_by_the_cost_guardrail_is_counted_once(self):
        self.policies[REPO] = ops.Policy(run_budget_usd=25.0)
        self.make_run()
        self.add_child(started="2026-01-01T00:00:00Z")
        self.runner.reports[(REPO, "r1")] = report(("a", "running"), cost=25.0)
        self.ops.check_budgets()
        self.ops.check_budgets()
        self.assertEqual(value_of(self.scrape(), "factory_budget_drains_total", repo=REPO), 1)

    def test_a_full_scrape_has_every_catalogued_metric_and_only_bounded_labels(self):
        """Every metric of the catalogue has a series, and every series is made of closed words and configured repositories."""
        write_history(self.data_dir, *scenario())
        (self.config_dir / "repos" / "org__name.toml").write_text(f'repo = "{REPO}"\nrun_budget_usd = 25\n')
        run = self.write_plan("r1", [7], state=False)
        usage = [{"phase": "build", "total_cost_usd": 1.5, "footprint": {"peak_rss_mb": 512, "cpu_seconds": 3.0}}]
        document = {"jobs": {LONG_JOB: {"status": "running", "worker_phase": "build", "usage": usage, "error": HOSTILE, "branch": HOSTILE}}}
        (run / "state.json").write_text(json.dumps(document))
        now = datetime(2026, 1, 31, tzinfo=timezone.utc)
        outcomes = [(True, "ci_failed")] * 8 + [(False, "ci_failed")] * 4
        self.manager.false_incomplete.set(REPO, false_incomplete.summarize(outcomes, 1, now - timedelta(days=30), now, False))
        self.manager.github_health._states[REPO] = "ok"
        self.ops._plan_failed(REPO, "timeout")  # the failure path itself is driven in test_a_failed_plan_is_counted_once_...
        self.manager.set_trigger_pause(True)
        self.ops.trigger_pass()
        self.manager.set_trigger_pause(False)
        self.reader.label(7, ttl.LABEL, actor=("mallory", "User"))
        self.ops.trigger_pass()
        self.reader.fail = RuntimeError(HOSTILE)
        self.ops.trigger_pass()
        self.add_child(kind="run", run="r1")  # after the watcher passes: a live child of ours would make the repository busy
        self.manager.runner.live = 1
        self.policies[REPO] = ops.Policy(run_budget_usd=25.0)
        self.runner.reports[(REPO, "r1")] = report(("a", "running"), cost=25.0)
        self.ops.check_budgets()
        failed = report(("a", "failed"), ("b", "failed"), cost=25.0)
        seen = datetime.fromtimestamp(self.time.now - 5, timezone.utc).isoformat().replace("+00:00", "Z")
        failed["jobs"][0].update(api_error="rate_limit", api_error_at=seen, attempt=1)
        failed["jobs"][1].update(api_error="spend_limit", api_error_at=seen, attempt=1)
        self.runner.reports[(REPO, "r1")] = failed
        self.ops.check_api_errors()  # counts both failures and trips the breaker once
        for path, who in (("/healthz", None), ("/repos", tm.VIEWER), ("/repos", None), ("/repos", tm.NO_ROLE)):
            headers = {} if who is None else {"X-MS-CLIENT-PRINCIPAL": who}
            self.manager.handle(Request("GET", path, headers, lambda limit: b"", "127.0.0.1"))
        self.manager._key_refresh_failed("fetch")
        snapshot = {"total": 4, "in_use": 1, "free": 3, "holders": [], "waiting": []}
        census = {"live": 2, "final": 1, "unknown": False}  # the pod census of a manager with the pod backend on
        with mock.patch.object(metrics_collect.capacity, "snapshot_from_env", return_value=snapshot), mock.patch.object(
            self.manager, "worker_pods", return_value=census
        ):
            text = self.scrape()
        self.assertTrue(parse(text))
        # Every catalogued metric has at least one sample (a gauge that depends on a setting was given the setting).
        names = {sample for sample, _, _ in parse(text)}
        for spec in metrics.CATALOGUE:
            if spec.name == "factory_metrics_scrape_errors_total":
                continue  # only a failing collector makes one; it is covered in LiveMetricsTests
            if spec.name == "factory_log_archive_failures_total":
                continue  # only a failed copy or sweep makes one; it is covered in tests/test_logarchive.py
            with self.subTest(metric=spec.name):
                found = {spec.name, f"{spec.name}_bucket", f"{spec.name}_count", f"{spec.name}_sum"} & names
                self.assertTrue(found, f"{spec.name} has no series in a full scrape")
        # Every label of every sample is declared by its metric and is a closed word or a configured repository.
        declared = {spec.name: spec for spec in metrics.CATALOGUE}
        configured = set(self.manager.configured_repos())
        for sample, labels, _ in parse(text):
            base = re.sub(r"_(bucket|sum|count)$", "", sample) if sample not in declared else sample
            spec = declared[base]
            self.assertLessEqual(set(labels), {*spec.labels, "le"}, sample)
            for name, value in labels.items():
                if name == "repo":
                    self.assertIn(value, configured, sample)
                elif name != "le":
                    self.assertIn(value, metrics.LABEL_VALUES[name], f"{sample} {name}={value}")
        # Nothing the sources held as text got through, and no series was added beside the catalogue.
        for part in (*HOSTILE_PARTS, LONG_JOB, "org/ghost", ttl.CANARY):
            self.assertNotIn(part, text)
        for line in text.splitlines():
            if line.startswith("#"):
                self.assertRegex(line, r"^# (HELP|TYPE) factory_[a-z0-9_]+ ")
        self.assertEqual(sorted({line.split()[2] for line in text.splitlines() if line.startswith("# TYPE")}), sorted(declared))


class SourceHygieneTests(unittest.TestCase):
    """The static analyzer on pull requests rejects two shapes; keep them out of the metrics modules."""

    FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
    BLOCKS = (ast.For, ast.AsyncFor, ast.While, ast.If, ast.With, ast.AsyncWith, ast.Try)
    MAX_NESTED_BLOCKS = 4

    def functions_of(self, module):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        return [node for node in ast.walk(tree) if isinstance(node, self.FUNCTIONS)]

    @staticmethod
    def parameters_of(function):
        args = function.args
        found = {arg.arg for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
        found.update(arg.arg for arg in (args.vararg, args.kwarg) if arg is not None)
        return found - {"self", "cls"}

    @staticmethod
    def assigned_by(node):
        """The plain names a statement binds as its own targets (not those bound inside a value expression)."""
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign, ast.For, ast.AsyncFor)):
            targets = [node.target]
        else:
            return set()
        names = (name for target in targets for name in ast.walk(target))
        return {name.id for name in names if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Store)}

    def nesting_of(self, node, current=0):
        """The deepest chain of control blocks under ``node`` (an ``elif`` is not one deeper; inner functions apart)."""
        deepest = current
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (*self.FUNCTIONS, ast.Lambda, ast.ClassDef)):
                continue
            is_elif = isinstance(node, ast.If) and isinstance(child, ast.If) and node.orelse == [child]
            step = 1 if isinstance(child, self.BLOCKS) and not is_elif else 0
            deepest = max(deepest, self.nesting_of(child, current + step))
        return deepest

    def test_no_function_assigns_to_its_own_parameter(self):
        for module in (metrics, metrics_collect):
            for function in self.functions_of(module):
                names = self.parameters_of(function)
                for node in ast.walk(function):
                    clash = names & self.assigned_by(node)
                    if clash:
                        self.fail(f"{module.__name__}.{function.name} line {node.lineno} assigns {sorted(clash)}")

    def test_no_function_nests_blocks_too_deep(self):
        for module in (metrics, metrics_collect):
            for function in self.functions_of(module):
                self.assertLessEqual(
                    self.nesting_of(function), self.MAX_NESTED_BLOCKS, f"{module.__name__}.{function.name}"
                )


if __name__ == "__main__":
    unittest.main()

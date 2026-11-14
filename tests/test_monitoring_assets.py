"""The alert rules and the Grafana dashboards of infra/monitoring/ against the real metric catalogue (factory.metrics).

infra/test/test_monitoring_design.py checks the same files against docs/metrics.md without importing the package;
this module checks what only the package knows: the label names of each metric and the closed set of values a
label may take. A rule that selects ``factory_jobs{status="runing"}`` or groups ``by (repository)`` would never
match a series, so it fails here instead of staying silent in production.
"""

import json
import re
import unittest
from pathlib import Path

from factory import metrics

MON = Path(__file__).resolve().parent.parent / "infra" / "monitoring"
SPECS = {spec.name: spec for spec in metrics.CATALOGUE}
HISTOGRAM_SUFFIX = re.compile(r"_(bucket|sum|count)$")
SELECTOR = re.compile(r"\b(factory_[a-z0-9_]+)\{([^}]*)\}")
MATCHER = re.compile(r'([a-z_]+)\s*(=~|!~|!=|=)\s*"([^"]*)"')
GROUPING = re.compile(r"\b(?:by|without)\s*\(([^)]*)\)")
LEGEND = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
# Labels the platform adds to every series, or that a query makes (le of a histogram).
EXTRA_LABELS = {"le", "job", "instance", "namespace", "container", "pod", "node"}
# The labels of the series that are not the manager's (issue 149): kube-state-metrics series and the recording rules built on
# them. A rule or a panel that groups by, or a legend that names, a label that is not here would never match anything.
PLATFORM_LABELS = {
    "kube_pod_created": {"uid"},
    "kube_pod_status_phase": {"uid", "phase"},
    "kube_pod_status_unschedulable": {"uid"},
    "kube_pod_status_ready": {"uid", "condition"},
    "kube_pod_status_ready_time": {"uid"},
    "kube_pod_status_reason": {"uid", "reason"},
    "kube_node_labels": {"label_karpenter_sh_nodepool"},
    "kube_customresource_nodeclaim_created_timestamp_seconds": {"name", "nodepool"},
    "kube_customresource_nodeclaim_condition_last_transition_timestamp_seconds": {"name", "nodepool", "type", "status"},
    "factory:worker_pod_time_to_ready_seconds": {"uid"},
    "factory:worker_pods_pending": set(),
    "factory:worker_pods_unschedulable": set(),
    "factory:nap_nodes": {"nodepool"},
    "factory:nap_nodes_total": set(),
    "factory:nodeclaim_condition_seconds": {"nodepool", "type"},
}
PLATFORM_NAME = re.compile(r"\b(kube_[a-z0-9_]+|factory:[a-z0-9_]+)\b")


def spec_of(name):
    return SPECS[HISTOGRAM_SUFFIX.sub("", name)] if name not in SPECS else SPECS[name]


def expressions():
    found = []
    rules = json.loads((MON / "alert-rules.json").read_text(encoding="utf-8"))
    for group in rules["groups"]:
        for rule in group["rules"]:
            found.append((rule.get("alert") or rule["record"], rule["expr"], ()))
    for path in sorted((MON / "dashboards").glob("*.json")):
        board = json.loads(path.read_text(encoding="utf-8"))
        for panel in board["panels"]:
            for target in panel.get("targets", []):
                found.append((f"{path.name}: {panel['title']}", target["expr"], (target.get("legendFormat", ""),)))
    return found


def platform_labels(expr):
    """The labels of the kube-state-metrics series and recorded series that the expression names (an unknown name fails)."""
    labels = set()
    for name in PLATFORM_NAME.findall(expr):
        if name not in PLATFORM_LABELS:
            raise AssertionError(f"{name} is not in PLATFORM_LABELS")
        labels |= PLATFORM_LABELS[name]
    # label_replace(<selector>, "<new label>", ...) makes a label that the series did not have.
    labels |= set(re.findall(r'label_replace\(.*?\},\s*"([a-z_]+)"', expr))
    return labels


def metrics_in(expr):
    names = set(re.findall(r"\b(factory_[a-z0-9_]+)\b", expr))
    return {name for name in names if HISTOGRAM_SUFFIX.sub("", name) in SPECS or name in SPECS}


class RuleAndDashboardLabelTests(unittest.TestCase):
    def test_every_factory_metric_is_declared_by_the_catalogue(self):
        count = 0
        for where, expr, _ in expressions():
            for name in re.findall(r"\b(factory_[a-z0-9_]+)\b", expr):
                with self.subTest(where=where, metric=name):
                    base = name if name in SPECS else HISTOGRAM_SUFFIX.sub("", name)
                    self.assertIn(base, SPECS)
                    if base != name:
                        self.assertEqual(SPECS[base].kind, metrics.HISTOGRAM, "_bucket, _sum and _count exist for histograms only")
                    elif SPECS[base].kind == metrics.HISTOGRAM:
                        self.fail("a histogram is selected without _bucket, _sum or _count")
                    count += 1
        self.assertGreater(count, 40)

    def test_a_selector_names_labels_the_metric_has_and_values_the_label_can_take(self):
        checked = 0
        for where, expr, _ in expressions():
            for name, body in SELECTOR.findall(expr):
                spec = spec_of(name)
                for label, operator, value in MATCHER.findall(body):
                    with self.subTest(where=where, metric=name, label=label):
                        self.assertIn(label, spec.labels)
                        if value.startswith("$") or label == "repo":
                            continue
                        allowed = metrics.LABEL_VALUES[label]
                        options = value.split("|") if operator in ("=~", "!~") else [value]
                        self.assertEqual(set(options) - set(allowed), set())
                        checked += 1
        self.assertGreater(checked, 8)

    def test_a_grouping_names_labels_the_series_it_aggregates_carry(self):
        checked = 0
        for where, expr, _ in expressions():
            carried = {label for name in metrics_in(expr) for label in spec_of(name).labels} | EXTRA_LABELS | platform_labels(expr)
            for body in GROUPING.findall(expr):
                for label in [part.strip() for part in body.split(",") if part.strip()]:
                    with self.subTest(where=where, label=label):
                        self.assertIn(label, carried)
                        checked += 1
        self.assertGreater(checked, 15)

    def test_a_legend_names_a_label_that_the_query_keeps(self):
        checked = 0
        for where, expr, legends in expressions():
            kept = {part.strip() for body in GROUPING.findall(expr) for part in body.split(",")}
            if not GROUPING.search(expr):
                kept = {label for name in metrics_in(expr) for label in spec_of(name).labels} | EXTRA_LABELS | platform_labels(expr)
            for legend in legends:
                for label in LEGEND.findall(legend):
                    with self.subTest(where=where, label=label):
                        self.assertIn(label, kept)
                        checked += 1
        self.assertGreater(checked, 15)

    def test_the_repo_variable_is_a_label_of_every_metric_it_filters(self):
        for where, expr, _ in expressions():
            for name, body in SELECTOR.findall(expr):
                if 'repo=~"$repo"' in body:
                    with self.subTest(where=where, metric=name):
                        self.assertIn("repo", spec_of(name).labels)

    def test_dashboards_filter_by_a_repository_variable_the_catalogue_supports(self):
        for path in sorted((MON / "dashboards").glob("*.json")):
            board = json.loads(path.read_text(encoding="utf-8"))
            if not board["templating"]["list"]:
                # A dashboard without the variable may not select a series of the manager (the kube-state-metrics dashboard, issue 149).
                with self.subTest(dashboard=path.name):
                    for panel in board["panels"]:
                        for target in panel.get("targets", []):
                            self.assertNotIn("factory_", target["expr"].replace("factory:", ""))
                continue
            variable = board["templating"]["list"][0]
            with self.subTest(dashboard=path.name):
                self.assertEqual(variable["definition"], "label_values(factory_jobs, repo)")
                self.assertIn("repo", SPECS["factory_jobs"].labels)
                self.assertTrue(variable["includeAll"])
                self.assertEqual(variable["allValue"], ".*")


if __name__ == "__main__":
    unittest.main()

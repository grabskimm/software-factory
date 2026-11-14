"""tools/infra_cluster_guard.py: the cluster-action guard to run after `terraform plan` (issue #144, slice 13).

The guard reads the action list of a plan (`<actions> <address>` lines) and the context of the run and refuses a
change to the AKS cluster or a node pool on a path that must not make one. The tests feed it a sample list for every
event, stage and action, and assert exit status and message. A second class reads the Terraform files as text and
fails when a resource of the guarded classes exists that the guard does not list.

Nothing here runs Terraform, Azure or GitHub: the guard is exercised against sample files. That the guard behaves in a
real pipeline is UNVERIFIED.
"""

import contextlib
import importlib.util
import io
import itertools
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from typing import NamedTuple

from factory import podcontract as pc

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("infra_cluster_guard_under_test", ROOT / "tools" / "infra_cluster_guard.py")
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)

INFRA = ROOT / "infra"

CLUSTER_ADDRESSES = ("azurerm_kubernetes_cluster.this", "azurerm_kubernetes_cluster_node_pool.factory")
POOL_ADDRESSES = (
    "azurerm_kubernetes_cluster_node_pool.monitoring",
    "azurerm_kubernetes_cluster_node_pool.monitoring[0]",
    'azurerm_kubernetes_cluster_node_pool.monitoring["a b"]',
)
CHANGES = ("create", "update", "delete", "delete,create", "create,delete")
NOT_CREATE = ("update", "delete", "delete,create", "create,delete")
UNRELATED = (
    "update kubernetes_manifest.factory_deployment",
    'create azurerm_role_assignment.aks_cluster_admin["00000000"]',
    "update azurerm_key_vault.this",
    "delete,create kubernetes_persistent_volume_claim_v1.data",
)
LABEL = "infra-cluster-change"


class Result(NamedTuple):
    code: int
    out: str
    summary: str


def run(actions, event, stage="all", snapshots="", labels="[]", plan=None):
    """The exit status, the printed text and the step summary of one guard run on the text ``actions`` (None: no file).

    ``plan`` is the text of the plan JSON (the guard's optional second argument), or None for no second argument.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "actions.txt"
        summary = Path(tmp) / "summary.md"
        if actions is not None:
            path.write_text(actions, encoding="utf-8")
        args = [str(path)]
        if plan is not None:
            (Path(tmp) / "plan.json").write_text(plan, encoding="utf-8")
            args.append(str(Path(tmp) / "plan.json"))
        env = {"GUARD_EVENT": event, "GUARD_STAGE": stage, "GUARD_SNAPSHOTS_TAKEN": snapshots, "GUARD_PR_LABELS": labels}
        out = io.StringIO()
        with unittest.mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}), contextlib.redirect_stdout(out):
            code = guard.main(args, env)
        text = summary.read_text(encoding="utf-8") if summary.exists() else ""
        return Result(code, out.getvalue(), text)


def quiet_main(args, env):
    """guard.main with stdout captured and no step summary (a CI run of these tests has a real one in its environment)."""
    out = io.StringIO()
    with unittest.mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": ""}), contextlib.redirect_stdout(out):
        code = guard.main(args, env)
    return code, out.getvalue()


def listing(addresses, actions):
    return "".join(f"{change} {address}\n" for address in addresses for change in actions)


class Case(unittest.TestCase):
    def assertRefused(self, result, needle):
        self.assertEqual(result.code, 1, result.out)
        self.assertIn("::error::", result.out)
        self.assertIn(needle, result.out)
        self.assertIn("Guard result: **refused**", result.summary)

    def assertAllowed(self, result):
        self.assertEqual(result.code, 0, result.out)
        self.assertNotIn("::error::", result.out)
        self.assertIn("Guard result: **allowed**", result.summary)


class AutomaticPath(Case):
    """workflow_run: the run after an image build, behind only the production approval."""

    def test_every_action_on_the_cluster_or_the_factory_node_pool_is_refused(self):
        for address in CLUSTER_ADDRESSES:
            for change in CHANGES:
                with self.subTest(change=change, address=address):
                    result = run(f"{change} {address}\n", "workflow_run")
                    self.assertRefused(result, "refused on the automatic path")
                    self.assertIn("with stage=cluster", result.out)

    def test_every_action_on_a_node_pool_object_of_any_index_is_refused(self):
        for address in POOL_ADDRESSES:
            for change in CHANGES:
                with self.subTest(change=change, address=address):
                    self.assertRefused(run(f"{change} {address}\n", "workflow_run"), "node pool object")

    def test_the_lines_the_specification_names_are_refused_with_their_message(self):
        self.assertRefused(run("update azurerm_kubernetes_cluster.this\n", "workflow_run"), "AKS cluster or its factory node pool")
        self.assertRefused(
            run("create,delete azurerm_kubernetes_cluster_node_pool.factory\n", "workflow_run"),
            "AKS cluster or its factory node pool",
        )
        self.assertRefused(run("create azurerm_kubernetes_cluster_node_pool.monitoring[0]\n", "workflow_run"), "node pool object")

    def test_nothing_the_dispatch_inputs_or_labels_say_rescues_an_automatic_run(self):
        labels = json.dumps([LABEL])
        for text in ("update azurerm_kubernetes_cluster.this\n", "create azurerm_kubernetes_cluster_node_pool.monitoring[0]\n"):
            with self.subTest(text=text):
                self.assertEqual(run(text, "workflow_run", stage="cluster", snapshots="true", labels=labels).code, 1)

    def test_a_plan_without_a_cluster_action_is_allowed(self):
        self.assertAllowed(run("", "workflow_run"))
        self.assertAllowed(run("no-op azurerm_kubernetes_cluster.this\n", "workflow_run"))
        self.assertAllowed(run("".join(f"{line}\n" for line in UNRELATED), "workflow_run"))


class ManualDispatch(Case):
    def test_stage_all_refuses_every_action_on_the_cluster_and_the_factory_node_pool(self):
        for address in CLUSTER_ADDRESSES:
            for change in CHANGES:
                with self.subTest(change=change, address=address):
                    self.assertRefused(run(f"{change} {address}\n", "workflow_dispatch", stage="all"), "stage=all never applies")

    def test_the_attestation_does_not_rescue_stage_all(self):
        self.assertRefused(
            run("update azurerm_kubernetes_cluster.this\n", "workflow_dispatch", stage="all", snapshots="true"), "stage=all"
        )

    def test_stage_cluster_allows_a_plain_create_without_an_attestation(self):
        for address in CLUSTER_ADDRESSES:
            with self.subTest(address=address):
                result = run(f"create {address}\n", "workflow_dispatch", stage="cluster")
                self.assertAllowed(result)
                self.assertNotIn("::notice::", result.out)

    def test_stage_cluster_needs_the_attestation_for_every_other_action(self):
        for address in CLUSTER_ADDRESSES:
            for change in NOT_CREATE:
                with self.subTest(change=change, address=address):
                    result = run(f"{change} {address}\n", "workflow_dispatch", stage="cluster")
                    self.assertRefused(result, "cluster_snapshots_taken=true")
                    self.assertIn("Caddy disk", result.out)

    def test_stage_cluster_with_the_attestation_allows_every_action_and_says_it_is_not_checked(self):
        for address, change in itertools.product(CLUSTER_ADDRESSES, CHANGES):
            with self.subTest(change=change, address=address):
                result = run(f"{change} {address}\n", "workflow_dispatch", stage="cluster", snapshots="true")
                self.assertAllowed(result)
                # a plain create needed no attestation, so there is nothing to say about it
                self.assertEqual("cannot check that the snapshots exist" in result.out, change != "create")

    def test_only_the_exact_text_true_is_an_attestation(self):
        for value in ("", "false", "True", "TRUE", "1", "yes", " ", "true false"):
            with self.subTest(value=value):
                result = run("update azurerm_kubernetes_cluster.this\n", "workflow_dispatch", stage="cluster", snapshots=value)
                self.assertRefused(result, "cluster_snapshots_taken=true")

    def test_node_pool_objects_are_allowed_on_either_stage_with_the_drain_gate_reminder(self):
        for stage, address, change in itertools.product(("all", "cluster"), POOL_ADDRESSES, CHANGES):
            with self.subTest(stage=stage, change=change, address=address):
                result = run(f"{change} {address}\n", "workflow_dispatch", stage=stage)
                self.assertAllowed(result)
                self.assertIn("::notice::An apply that changes a node pool object must go through the drain gate", result.out)

    def test_the_lines_the_specification_names_are_allowed_on_a_manual_dispatch(self):
        self.assertAllowed(run("create azurerm_kubernetes_cluster_node_pool.monitoring[0]\n", "workflow_dispatch", stage="all"))
        self.assertAllowed(run("create azurerm_kubernetes_cluster_node_pool.monitoring[0]\n", "workflow_dispatch", stage="cluster"))

    def test_one_refused_line_refuses_the_whole_plan(self):
        text = "create azurerm_kubernetes_cluster_node_pool.monitoring[0]\nupdate azurerm_kubernetes_cluster.this\n"
        result = run(text, "workflow_dispatch", stage="cluster")
        self.assertRefused(result, "cluster_snapshots_taken=true")
        self.assertIn("drain gate", result.out)  # the allowed line still gets its reminder


class PullRequest(Case):
    def test_a_cluster_or_pool_action_without_the_label_is_refused(self):
        for address in (*CLUSTER_ADDRESSES, *POOL_ADDRESSES):
            for change in CHANGES:
                with self.subTest(change=change, address=address):
                    self.assertRefused(run(f"{change} {address}\n", "pull_request"), "label infra-cluster-change")

    def test_with_the_label_it_is_allowed_and_warns_that_a_manual_run_must_apply_it(self):
        for address in (*CLUSTER_ADDRESSES, *POOL_ADDRESSES):
            with self.subTest(address=address):
                result = run(f"update {address}\n", "pull_request", labels=json.dumps(["bug", LABEL]))
                self.assertAllowed(result)
                self.assertIn("::warning::", result.out)
                self.assertIn("stage=cluster", result.out)

    def test_the_stage_and_the_attestation_do_not_matter_to_a_pull_request(self):
        self.assertRefused(run("update azurerm_kubernetes_cluster.this\n", "pull_request", stage="cluster", snapshots="true"), "label")

    def test_only_an_exact_label_name_counts(self):
        near = (
            ["Infra-Cluster-Change"],
            ["infra-cluster-change "],
            ["infra-cluster-changes"],
            ["infra-cluster"],
            ["x-infra-cluster-change"],
            [1, None, {"name": LABEL}],
            [[LABEL]],
            [],
        )
        for labels in near:
            with self.subTest(labels=labels):
                self.assertRefused(run("update azurerm_kubernetes_cluster.this\n", "pull_request", labels=json.dumps(labels)), "label")

    def test_labels_that_are_not_a_json_list_of_names_count_as_no_label(self):
        for raw in ("", "not json", '"infra-cluster-change"', '{"infra-cluster-change": true}', "null", "[", "42"):
            with self.subTest(raw=raw):
                self.assertRefused(run("update azurerm_kubernetes_cluster.this\n", "pull_request", labels=raw), "label")

    def test_the_label_is_found_among_other_names_and_in_the_pretty_printed_form_of_the_workflow(self):
        pretty = json.dumps(["factory:planned", LABEL, "enhancement"], indent=2)
        self.assertAllowed(run("delete azurerm_kubernetes_cluster_node_pool.factory\n", "pull_request", labels=pretty))

    def test_a_plan_without_a_cluster_action_needs_no_label(self):
        self.assertAllowed(run("", "pull_request"))
        self.assertAllowed(run("".join(f"{line}\n" for line in UNRELATED), "pull_request"))


class WhatCounts(Case):
    def test_only_the_listed_addresses_count_and_a_no_op_never_does(self):
        text = "".join(
            [
                "no-op azurerm_kubernetes_cluster.this\n",
                "no-op azurerm_kubernetes_cluster_node_pool.monitoring[0]\n",
                "update azurerm_kubernetes_cluster.this_other\n",
                "update azurerm_kubernetes_cluster_node_pool.factory2\n",
                "update azurerm_kubernetes_cluster_node_pool.monitoring_extra\n",
                "update data.azurerm_kubernetes_cluster.this\n",
                "update module.m.azurerm_kubernetes_cluster.this\n",
                *(f"{line}\n" for line in UNRELATED),
            ]
        )
        result = run(text, "workflow_run")
        self.assertAllowed(result)
        self.assertIn("cluster-touching actions in this plan: 0", result.out)

    def test_an_index_of_any_form_belongs_to_the_listed_address(self):
        for index in ("[0]", "[7]", '["a"]', '["a b"]', '["x]y"]'):
            with self.subTest(index=index):
                self.assertEqual(run(f"update azurerm_kubernetes_cluster_node_pool.factory{index}\n", "workflow_run").code, 1)

    def test_trailing_blanks_do_not_hide_the_address(self):
        self.assertEqual(run("update azurerm_kubernetes_cluster.this  \n", "workflow_run").code, 1)
        self.assertEqual(run("update azurerm_kubernetes_cluster.this\t\n", "workflow_run").code, 1)
        self.assertEqual(run("update azurerm_kubernetes_cluster.this\r\n", "workflow_run").code, 1)

    def test_the_summary_always_shows_the_count_and_the_lines(self):
        two_lines = "update azurerm_kubernetes_cluster.this\ncreate azurerm_kubernetes_cluster_node_pool.monitoring[0]\n"
        for event, stage, labels in (
            ("workflow_run", "all", "[]"),
            ("workflow_dispatch", "cluster", "[]"),
            ("pull_request", "all", json.dumps([LABEL])),
        ):
            with self.subTest(event=event):
                empty = run("", event, stage=stage, snapshots="true", labels=labels)
                self.assertIn("cluster-touching actions in this plan: 0", empty.out)
                self.assertIn("cluster-touching actions in this plan: 0", empty.summary)
                self.assertNotIn("```", empty.summary)
                two = run(two_lines, event, stage=stage, snapshots="true", labels=labels)
                for shown in (two.out, two.summary):
                    self.assertIn("cluster-touching actions in this plan: 2", shown)
                    self.assertIn("update azurerm_kubernetes_cluster.this (CLUSTER)", shown)
                    self.assertIn("create azurerm_kubernetes_cluster_node_pool.monitoring[0] (POOL_OBJECTS)", shown)

    def test_a_refusal_is_in_the_summary_as_well_as_in_the_log(self):
        result = run("update azurerm_kubernetes_cluster.this\n", "workflow_run")
        self.assertIn("update azurerm_kubernetes_cluster.this (CLUSTER)", result.summary)
        self.assertIn("Guard result: **refused**", result.summary)

    def test_the_same_message_is_not_printed_once_per_line(self):
        result = run(listing(CLUSTER_ADDRESSES, CHANGES), "workflow_run")
        self.assertEqual(result.out.count("::error::"), 1)

    def test_the_guard_only_reads_the_action_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "actions.txt"
            original = "update azurerm_kubernetes_cluster.this\ncreate azurerm_key_vault.this\n"
            path.write_bytes(original.encode("utf-8"))
            env = {"GUARD_EVENT": "workflow_run", "GUARD_STAGE": "all"}
            self.assertEqual(quiet_main([str(path)], env)[0], 1)
            self.assertEqual(path.read_bytes(), original.encode("utf-8"))
            self.assertEqual(sorted(entry.name for entry in Path(tmp).iterdir()), ["actions.txt"])


class FailClosed(Case):
    def assertCannotDecide(self, result):
        self.assertEqual(result.code, 2, result.out)
        self.assertIn("::error::", result.out)
        self.assertIn("refuses to decide", result.out)

    def test_a_missing_file_or_a_directory_cannot_be_decided(self):
        self.assertCannotDecide(run(None, "workflow_run"))
        with tempfile.TemporaryDirectory() as tmp:
            code, out = quiet_main([tmp], {"GUARD_EVENT": "workflow_run", "GUARD_STAGE": "all"})
        self.assertEqual(code, 2)
        self.assertIn("refuses to decide", out)

    def test_a_line_that_is_not_actions_then_address_cannot_be_decided(self):
        for text in (
            "update\n",
            "azurerm_kubernetes_cluster.this\n",
            "UPDATE azurerm_kubernetes_cluster.this\n",
            "update,  azurerm_kubernetes_cluster.this\n",
            "\tupdate azurerm_kubernetes_cluster.this\n",
            "update azurerm_key_vault.this\nNOT A LINE\n",
            "update  azurerm_kubernetes_cluster.this\n",
        ):
            with self.subTest(text=text):
                self.assertCannotDecide(run(text, "workflow_run"))

    def test_a_file_that_is_not_text_or_is_too_big_cannot_be_decided(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "actions.txt"
            for data in (b"\xff\xfe\x00update x\n", b"".join([b"update azurerm_key_vault.this\n"] * 40000)):
                path.write_bytes(data)
                code, _ = quiet_main([str(path)], {"GUARD_EVENT": "workflow_run", "GUARD_STAGE": "all"})
                self.assertEqual(code, 2)

    def test_an_unknown_or_missing_event_or_stage_cannot_be_decided_even_for_an_empty_plan(self):
        for event in ("", "push", "Workflow_Run", "schedule", "workflow_run_x", "pull_request,workflow_run"):
            with self.subTest(event=event):
                self.assertCannotDecide(run("", event))
        for stage in ("", "apply", "ALL", "cluster,all"):
            with self.subTest(stage=stage):
                self.assertCannotDecide(run("", "workflow_run", stage=stage))

    def test_a_stray_newline_around_an_environment_value_is_trimmed(self):
        self.assertEqual(run("", " workflow_run\n", stage="all\n").code, 0)

    def test_the_arguments_are_one_file_and_an_optional_plan(self):
        for args in ([], ["a", "b", "c"]):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(guard.main(args, {}), 2)
                self.assertIn("usage", err.getvalue())


class OutputHygiene(Case):
    HOSTILE_LABEL = "`::error::labeljunk`\r::set-output name=x::y"

    def test_only_a_cleaned_address_of_a_listed_resource_reaches_the_log_and_the_summary(self):
        address = 'azurerm_kubernetes_cluster_node_pool.monitoring["`::warning::addrjunk`$(id)"]'
        result = run(f"update {address}\n", "workflow_run")
        self.assertEqual(result.code, 1)
        cleaned = 'azurerm_kubernetes_cluster_node_pool.monitoring["???warning??addrjunk???id?"]'
        shown = [line for line in result.out.splitlines() if line.startswith("  ")]
        self.assertEqual(shown, [f"  update {cleaned} (POOL_OBJECTS)"])
        self.assertIn(f"update {cleaned} (POOL_OBJECTS)", result.summary)
        for text in (result.out, result.summary):
            self.assertNotIn("::warning::", text)
            self.assertNotIn("$(", text)
            self.assertNotIn("`", text.replace("```", ""))

    def test_a_hostile_label_is_never_echoed(self):
        for labels in (json.dumps([self.HOSTILE_LABEL]), json.dumps([self.HOSTILE_LABEL, LABEL])):
            with self.subTest(labels=labels):
                result = run("update azurerm_kubernetes_cluster.this\n", "pull_request", labels=labels)
                for text in (result.out, result.summary):
                    self.assertNotIn("labeljunk", text)
                    self.assertNotIn("set-output", text)

    def test_a_hostile_event_or_stage_is_never_echoed(self):
        for value, field in itertools.product(("evilvalue::error::x", "evil`value"), ("event", "stage")):
            with self.subTest(value=value, field=field):
                result = run("", value) if field == "event" else run("", "workflow_run", stage=value)
                self.assertEqual(result.code, 2)
                for text in (result.out, result.summary):
                    self.assertNotIn("evil", text)

    def test_a_very_long_address_is_cut(self):
        address = f"azurerm_kubernetes_cluster_node_pool.monitoring[{'a' * 5000}]"
        result = run(f"update {address}\n", "workflow_run")
        shown = [line for line in result.out.splitlines() if line.startswith("  update ")]
        self.assertEqual(len(shown), 1)
        self.assertLess(len(shown[0]), 300)

    def test_every_message_is_a_fixed_text(self):
        for table in (guard.REFUSALS, guard.UNREADABLE):
            for code, text in table.items():
                with self.subTest(code=code):
                    self.assertNotIn("{", text)
                    self.assertTrue(text.isascii())
        for code, (level, text) in guard.ADVICE.items():
            with self.subTest(code=code):
                self.assertIn(level, ("notice", "warning"))
                self.assertNotIn("{", text)
                self.assertTrue(text.isascii())


DIGEST_A = "acr.example.invalid/factory@sha256:" + "a" * 64
DIGEST_B = "acr.example.invalid/factory@sha256:" + "b" * 64
PARAMS = "kubernetes_config_map_v1.worker_policy_params[0]"


def config_map_change(before=None, after=None, actions=("update",), address=PARAMS, **extra):
    """One resource change of ``terraform show -json`` for the worker policy parameter (an image-only update by default)."""
    base = {"metadata": [{"name": "factory-worker-policy", "namespace": "factory-workers", "labels": {"a": "b"}}], "id": "x"}
    before = {**base, "data": {"image": DIGEST_A}} if before is None else before
    after = {**base, "data": {"image": DIGEST_B}} if after is None else after
    change = {"actions": list(actions), "before": before, "after": after, "after_unknown": {}, "before_sensitive": {}, "after_sensitive": {}}
    change.update(extra)
    return {"address": address, "type": "kubernetes_config_map_v1", "change": change}


def plan_json(*changes):
    return json.dumps({"format_version": "1.2", "resource_changes": list(changes)})


class ImageOnlyUpdate(Case):
    """Issue 265: the automatic path may apply an image-only update of the worker policy parameter, nothing wider."""

    LINE = f"update {PARAMS}\n"

    def test_an_image_only_update_is_allowed_on_the_automatic_path_with_a_notice(self):
        result = run(self.LINE + "update kubernetes_manifest.factory_deployment\n", "workflow_run", plan=plan_json(config_map_change()))
        self.assertEqual(result.code, 0, result.out)
        self.assertIn("image-only update of the worker policy parameter", result.out)
        self.assertIn(f"update {PARAMS} (POOL_OBJECTS)", result.out)

    def test_every_other_change_of_that_object_stays_refused(self):
        base = {"metadata": [{"name": "factory-worker-policy", "namespace": "factory-workers", "labels": {"a": "b"}}], "id": "x"}
        cases = {
            "another key": config_map_change(after={**base, "data": {"image": DIGEST_B, "extra": "1"}}),
            "the key renamed": config_map_change(after={**base, "data": {"img": DIGEST_B}}),
            "no data": config_map_change(after={**base}),
            "a tag, not a digest": config_map_change(after={**base, "data": {"image": "acr.example.invalid/factory:latest"}}),
            "a bad digest": config_map_change(after={**base, "data": {"image": "acr.example.invalid/factory@sha256:" + "z" * 64}}),
            "a non-string image": config_map_change(after={**base, "data": {"image": 7}}),
            "a label changed too": config_map_change(after={**{**base, "metadata": [{"name": "factory-worker-policy", "namespace": "factory-workers", "labels": {"a": "c"}}]}, "data": {"image": DIGEST_B}}),
            "an unknown value": config_map_change(after_unknown={"data": {"image": True}}),
            "a sensitive value": config_map_change(after_sensitive={"data": True}),
            "a replace": config_map_change(actions=("delete", "create")),
            "a delete": config_map_change(actions=("delete",)),
            "a create": config_map_change(actions=("create",)),
            "an update with a second action": config_map_change(actions=("update", "update")),
        }
        for name, change in cases.items():
            with self.subTest(name):
                result = run(self.LINE, "workflow_run", plan=plan_json(change))
                self.assertEqual(result.code, 1, result.out)
                self.assertIn("refused on the automatic path", result.out)

    def test_another_pool_object_action_in_the_same_plan_still_refuses_the_whole_plan(self):
        for other in ("update kubernetes_manifest.worker_nodepool[0]", "create kubernetes_namespace_v1.worker[0]", "delete kubernetes_role_v1.worker_launcher[0]"):
            with self.subTest(other):
                result = run(f"{other}\n{self.LINE}", "workflow_run", plan=plan_json(config_map_change()))
                self.assertEqual(result.code, 1, result.out)

    def test_the_plan_must_prove_it_for_the_very_address_and_not_for_another_object(self):
        elsewhere = config_map_change(address="kubernetes_config_map_v1.worker_policy_params[1]")
        self.assertEqual(run(self.LINE, "workflow_run", plan=plan_json(elsewhere)).code, 1)
        self.assertEqual(run(self.LINE, "workflow_run", plan=plan_json()).code, 1)
        other_type = config_map_change(address="kubernetes_config_map_v1.insights_agent")
        self.assertEqual(run(self.LINE, "workflow_run", plan=plan_json(other_type)).code, 1)

    def test_a_missing_or_unreadable_plan_cannot_be_decided_and_never_allows(self):
        self.assertEqual(run(self.LINE, "workflow_run").code, 2)
        for text in ("", "not json", "[]", "{}", '{"resource_changes": "x"}', '{"resource_changes": [1, null, "x"]}'):
            with self.subTest(text=text):
                self.assertEqual(run(self.LINE, "workflow_run", plan=text).code, 1, text)
        with tempfile.TemporaryDirectory() as tmp:
            actions = Path(tmp) / "actions.txt"
            actions.write_text(self.LINE, encoding="utf-8")
            for args in ([str(actions), str(Path(tmp) / "missing.json")], [str(actions), tmp]):
                with self.subTest(args=args[1:]):
                    self.assertEqual(quiet_main(args, {"GUARD_EVENT": "workflow_run", "GUARD_STAGE": "all"})[0], 2)
        binary = Path(tempfile.mkdtemp()) / "plan.json"
        binary.write_bytes(b"\xff\xfe\x00")
        self.addCleanup(binary.unlink)
        with tempfile.TemporaryDirectory() as tmp:
            actions = Path(tmp) / "actions.txt"
            actions.write_text(self.LINE, encoding="utf-8")
            self.assertEqual(quiet_main([str(actions), str(binary)], {"GUARD_EVENT": "workflow_run", "GUARD_STAGE": "all"})[0], 2)

    def test_an_oversized_plan_cannot_be_decided(self):
        with unittest.mock.patch.object(guard, "MAX_PLAN_BYTES", 10):
            self.assertEqual(run(self.LINE, "workflow_run", plan=plan_json(config_map_change())).code, 2)

    def test_a_plan_without_the_candidate_never_needs_or_reads_the_plan_json(self):
        self.assertEqual(run("update kubernetes_manifest.factory_deployment\n", "workflow_run").code, 0)
        self.assertEqual(run("update kubernetes_manifest.factory_deployment\n", "workflow_run", plan="not json").code, 0)

    def test_the_other_events_are_unchanged_by_the_plan_json(self):
        good = plan_json(config_map_change())
        self.assertEqual(run(self.LINE, "pull_request", plan=good).code, 1)
        self.assertEqual(run(self.LINE, "pull_request", labels=json.dumps([LABEL]), plan=good).code, 0)
        manual = run(self.LINE, "workflow_dispatch", stage="all", plan=good)
        self.assertEqual(manual.code, 0)
        self.assertIn("drain gate", manual.out)
        self.assertEqual(run(self.LINE, "workflow_dispatch", stage="all").code, 0)

    def test_only_the_workflow_run_event_reads_the_plan_so_other_events_work_without_it(self):
        self.assertEqual(run(self.LINE, "workflow_dispatch", stage="cluster").code, 0)
        self.assertEqual(run(self.LINE, "pull_request", labels=json.dumps([LABEL])).code, 0)

    def test_the_image_reference_needs_a_repository_and_a_full_digest(self):
        for good in (DIGEST_A, "r.example.invalid/a/b_c-d.e@sha256:" + "0" * 64):
            self.assertTrue(guard.IMAGE_REFERENCE.fullmatch(good), good)
        for bad in ("", "@sha256:" + "a" * 64, "r@sha256:" + "a" * 63, "r@sha256:" + "A" * 64, "R.example@sha256:" + "a" * 64, "r@sha1:" + "a" * 64, "r x@sha256:" + "a" * 64):
            self.assertFalse(guard.IMAGE_REFERENCE.fullmatch(bad), bad)

    def test_the_automatic_path_still_refuses_the_cluster_class_whatever_the_plan_says(self):
        result = run(f"update azurerm_kubernetes_cluster.this\n{self.LINE}", "workflow_run", plan=plan_json(config_map_change()))
        self.assertEqual(result.code, 1)
        self.assertIn("AKS cluster", result.out)


class CommandLine(unittest.TestCase):
    """The script as a pipeline runs it: `python3 tools/infra_cluster_guard.py FILE` with the context in the environment."""

    def invoke(self, text, **context):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "actions.txt"
            path.write_text(text, encoding="utf-8")
            env = {"PATH": os.environ.get("PATH", ""), **context}
            return subprocess.run(
                [sys.executable, "-I", str(ROOT / "tools" / "infra_cluster_guard.py"), str(path)],
                capture_output=True,
                text=True,
                env=env,
                cwd=tmp,
                timeout=60,
                check=False,
            )

    def test_exit_status_is_zero_one_or_two(self):
        refused = self.invoke("update azurerm_kubernetes_cluster.this\n", GUARD_EVENT="workflow_run", GUARD_STAGE="all")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("::error::", refused.stdout)
        allowed = self.invoke("update azurerm_key_vault.this\n", GUARD_EVENT="workflow_run", GUARD_STAGE="all")
        self.assertEqual(allowed.returncode, 0)
        self.assertIn("cluster-touching actions in this plan: 0", allowed.stdout)
        unknown = self.invoke("", GUARD_EVENT="", GUARD_STAGE="all")
        self.assertEqual(unknown.returncode, 2)

    def test_the_labels_come_from_the_environment_never_from_the_arguments(self):
        labeled = self.invoke(
            "update azurerm_kubernetes_cluster.this\n",
            GUARD_EVENT="pull_request",
            GUARD_STAGE="all",
            GUARD_PR_LABELS=json.dumps([LABEL]),
        )
        self.assertEqual(labeled.returncode, 0)
        self.assertIn("::warning::", labeled.stdout)


# ---- the address lists are data, and they must cover every resource of the guarded classes ----------------------

RESOURCE = re.compile(r'(?ms)^resource "([a-z0-9_]+)" "([A-Za-z0-9_-]+)" \{\n(.*?)^\}$')
CLUSTER_TYPE_PREFIX = "azurerm_kubernetes_cluster"
NODE_POOL_MARKERS = ("karpenter.sh", "karpenter.azure.com")
# The Kubernetes providers whose resources the completeness rule below reads (infra/versions.tf pins `kubernetes` only; the other two
# prefixes make a later provider of the same kind subject to the rule without a change here).
KUBERNETES_PREFIXES = ("kubernetes_", "helm_", "kubectl_")
# The text by which a Kubernetes resource of infra/ is tied to the worker namespace of the pod backend (infra/worker_ns.tf). Two
# groups: a reference to an OBJECT of the namespace or to its gate (the resource goes with the variable), and a NAME of the
# namespace (a selector or a field that spells it).
WORKER_OBJECT_SPELLINGS = (
    "local.worker_ns_count",  # the count local of worker_ns.tf
    "var.pod_backend_enabled",  # the variable that gates every object of the namespace
    "kubernetes_namespace_v1.worker",  # the namespace itself, also `.worker[0].metadata[0].name`
    "kubernetes_service_account_v1.worker",  # its metadata carries the namespace
    "kubernetes_network_policy_v1.worker_default_deny",  # its metadata carries the namespace
)
WORKER_NAME_SPELLINGS = (
    "local.worker_contract.names.worker_namespace",  # the contract name (infra/locals.tf)
    pc.WORKER_NAMESPACE,  # the literal name, `factory-workers`, as podcontract and the contract local spell it
)
WORKER_NAMESPACE_SPELLINGS = (*WORKER_OBJECT_SPELLINGS, *WORKER_NAME_SPELLINGS)
# A resource that names the namespace only as a PEER, inside a conditional on the variable, and lives in another namespace: the
# prepared policy of the manager namespace admits the relay port FROM the worker pods. Turning the variable off updates its
# ingress list in place and deletes nothing, so it is not an object of the namespace. The exemption holds only while the resource
# is in another namespace, is not gated by the variable and references no object of the worker namespace (`is_only_a_peer`).
WORKER_NAMESPACE_PEERS = ("kubernetes_manifest.factory_network_policy",)
OWN_NAMESPACE = r'"?namespace"?'
SELECTED_NAMESPACE = r'"?namespace"?|"kubernetes\.io/metadata\.name"'
GATE = re.compile(r"(?m)^[ \t]*(?:count|for_each)[ \t]*=[^\n]*pod_backend_enabled")
LOCALS_BLOCK = re.compile(r"(?ms)^locals \{\n(.*?)^\}$")
LOCAL_ATTRIBUTE = re.compile(r"(?m)^  (\w+)\s*=")
WORKER_NAMESPACE_ADDRESSES = (
    "kubernetes_namespace_v1.worker",
    "kubernetes_service_account_v1.worker",
    "kubernetes_network_policy_v1.worker_default_deny",
    "kubernetes_config_map_v1.worker_policy_params",  # slice 15b: the parameter of the admission policy, in the namespace
    "kubernetes_role_v1.worker_launcher",  # slice 16: the Role of the launcher
    "kubernetes_role_binding_v1.worker_launcher",  # slice 16: its binding to the manager's service account
)
# Slice 17: the one object of the pod backend that lives in the manager namespace and is gated by the same variable (infra/pod_backend.tf).
POD_BACKEND_ADDRESSES = ("kubernetes_service_v1.factory_relay",)
WORKER_POLICY_ADDRESSES = (
    "kubernetes_manifest.worker_policy_pods",
    "kubernetes_manifest.worker_policy_pods_binding",
    "kubernetes_manifest.worker_policy_secrets",
    "kubernetes_manifest.worker_policy_secrets_binding",
)
AKS_API_TYPE = "Microsoft.ContainerService"  # an azapi resource of this type can change the cluster or a pool as well


def terraform_texts(directory):
    return {path.name: path.read_text(encoding="utf-8") for path in sorted(directory.glob("*.tf"))}


def terraform_resources(texts):
    """{`type.name`: body} of every top-level `resource` block of the texts (terraform fmt puts the closing brace in column 0)."""
    found = {}
    for text in texts.values():
        for kind, name, body in RESOURCE.findall(text):
            found[f"{kind}.{name}"] = body
    return found


def modules(texts):
    return [name for name, text in texts.items() if re.search(r'(?m)^module "', text)]


def without_comments(text):
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(("#", "//")))


def locals_map(texts):
    """{name: expression text} of every attribute of every top-level `locals` block of the texts (attributes sit at two spaces)."""
    found = {}
    for text in texts.values():
        for inner in LOCALS_BLOCK.findall(without_comments(text)):
            heads = list(LOCAL_ATTRIBUTE.finditer(inner))
            for head, following in zip(heads, [*heads[1:], None], strict=True):
                found[head.group(1)] = inner[head.end() : following.start() if following else len(inner)]
    return found


def open_brackets(text):
    return sum(text.count(char) for char in "([{") - sum(text.count(char) for char in ")]}")


def continued(expression, following):
    """The expression and, while the brackets it opened stay open, the lines that follow it."""
    joined = expression
    for more in following:
        if open_brackets(joined) <= 0:
            break
        joined = f"{joined} {more.strip()}"
    return joined


def argument_expressions(body, names):
    """The expression after every `<name> =` (or `<name>:`) of `body`, a name matching the regex `names`, also inside an inline map.

    It runs to the end of the line, and over the next lines while the brackets it opened stay open.
    """
    lines = without_comments(body).splitlines()
    pattern = re.compile(rf"(?<![\w.])(?:{names})\s*[=:]\s*")
    return [continued(line[head.end() :], lines[number + 1 :]) for number, line in enumerate(lines) for head in pattern.finditer(line)]


def local_parts(rhs, path):
    """The expression of the attribute `path` names inside the object a local is (`local.config.namespace`), else the whole text."""
    attribute = re.match(r"\.(\w+)", path)
    found = argument_expressions(rhs, re.escape(attribute.group(1))) if attribute else []
    return found or [rhs]


def resolves_to_worker(expression, aliases, seen=()):
    """Whether the expression names the worker namespace: by a spelling, or through a `local.` that does (at any depth).

    A reference into an object local reads only the attribute it names, so `local.config.namespace` is not tainted by an
    unrelated attribute of `config`. A path that cannot be found in the text is read as the whole local (the safe side).
    """
    if any(spelling in expression for spelling in WORKER_NAMESPACE_SPELLINGS):
        return True
    for name, path in re.findall(r"\blocal\.(\w+)((?:\.\w+|\[[^\]]*\])*)", expression):
        # `worker_contract` holds the name next to many others: only its exact path counts (a spelling above).
        if name in seen or name == "worker_contract" or name not in aliases:
            continue
        if any(resolves_to_worker(part, aliases, (*seen, name)) for part in local_parts(aliases[name], path)):
            return True
    return False


def namespace_arguments(kind, names):
    """`names` (a regex of argument names), and for a namespace resource also its `name`, which IS the namespace."""
    return f"{names}|name" if kind.startswith("kubernetes_namespace") else names


def is_in_worker_namespace(kind, body, aliases):
    """Whether a `namespace` argument (for a namespace resource also its `name`) of the body resolves to the worker namespace."""
    names = namespace_arguments(kind, OWN_NAMESPACE)
    return any(resolves_to_worker(expression, aliases) for expression in argument_expressions(body, names))


def names_worker_namespace(kind, body, aliases):
    """Whether a Kubernetes resource is tied to the worker namespace: a spelling in its text (comments left out), or a namespace
    argument or a namespace label selector that resolves to it through a local."""
    plain = without_comments(body)
    if any(spelling in plain for spelling in WORKER_NAMESPACE_SPELLINGS):
        return True
    names = namespace_arguments(kind, SELECTED_NAMESPACE)
    return any(resolves_to_worker(expression, aliases) for expression in argument_expressions(plain, names))


def is_only_a_peer(address, kind, body, aliases, peers):
    plain = without_comments(body)
    in_another_namespace = bool(argument_expressions(plain, OWN_NAMESPACE)) and not is_in_worker_namespace(kind, plain, aliases)
    ties = GATE.search(plain) or any(spelling in plain for spelling in WORKER_OBJECT_SPELLINGS if spelling != "var.pod_backend_enabled")
    return address in peers and in_another_namespace and not ties


def worker_objects(resources, aliases, peers):
    """The addresses of the Kubernetes resources that name the worker namespace, or name one of the others (a Role bound by a
    RoleBinding that only reads the Role's metadata)."""
    candidates = {address: body for address, body in resources.items() if address.startswith(KUBERNETES_PREFIXES)}
    found = {
        address
        for address, body in candidates.items()
        if names_worker_namespace(address.split(".", 1)[0], body, aliases) and not is_only_a_peer(address, address.split(".", 1)[0], body, aliases, peers)
    }
    grown = True
    while grown:
        before = len(found)
        for address, body in candidates.items():
            if address not in found and any(re.search(rf"\b{re.escape(other)}\b", without_comments(body)) for other in found):
                found.add(address)
        grown = len(found) > before
    return found


def list_problems(resources, listed, aliases=None, peers=WORKER_NAMESPACE_PEERS):
    """What is wrong between the resources of infra/ and the guard's lists (empty when they agree).

    `aliases` is {local name: expression} of the tree (default: the real infra/); a test that adds a `locals` block passes its own.
    """
    tree_locals = locals_map(terraform_texts(INFRA)) if aliases is None else aliases
    found = []
    for address, body in sorted(resources.items()):
        kind = address.split(".", 1)[0]
        if kind.startswith(CLUSTER_TYPE_PREFIX) and address not in listed:
            found.append(f"{address} is a cluster-class resource that tools/infra_cluster_guard.py does not list")
        if kind.startswith("azapi_") and AKS_API_TYPE in body and address not in listed:
            found.append(f"{address} is an azapi resource of the AKS API that tools/infra_cluster_guard.py does not list")
        is_pool_object = kind == "kubernetes_manifest" and any(marker in body for marker in NODE_POOL_MARKERS)
        if is_pool_object and listed.get(address) != guard.POOL_OBJECTS:
            found.append(f"{address} is a NodePool or AKSNodeClass manifest that is not listed under POOL_OBJECTS")
    for address in sorted(worker_objects(resources, tree_locals, peers)):
        if listed.get(address) != guard.POOL_OBJECTS:
            found.append(f"{address} is an object of the worker namespace that is not listed under POOL_OBJECTS")
    for address in sorted(listed):
        if address not in resources:
            found.append(f"{address} is listed but no resource of infra/ has that address")
    return found


class AddressLists(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.texts = terraform_texts(INFRA)
        cls.resources = terraform_resources(cls.texts)

    def test_the_lists_are_data_with_one_entry_per_address_and_a_class_per_entry(self):
        addresses = [address for address, _ in guard.LISTED]
        self.assertEqual(len(addresses), len(set(addresses)), "an address is listed twice")
        self.assertEqual({klass for _, klass in guard.LISTED}, {guard.CLUSTER, guard.POOL_OBJECTS})
        self.assertEqual(guard.CLASS_OF, dict(guard.LISTED))
        for address in addresses:
            with self.subTest(address=address):
                self.assertRegex(address, r"^[a-z0-9_]+\.[A-Za-z0-9_-]+$")  # type.name, no index, no module

    def test_the_specified_classes_are_what_is_listed(self):
        self.assertEqual(
            {address for address, klass in guard.LISTED if klass == guard.CLUSTER},
            {"azurerm_kubernetes_cluster.this", "azurerm_kubernetes_cluster_node_pool.factory"},
        )
        self.assertIn(("azurerm_kubernetes_cluster_node_pool.monitoring", guard.POOL_OBJECTS), guard.LISTED)

    def test_the_node_pool_and_node_class_manifests_of_the_worker_pool_are_pool_objects_and_exist(self):
        # Slice 14 (issue #144): turning worker_nodepool_enabled off in INFRA_TFVARS would DELETE the NodePool, which ignores
        # `do-not-disrupt`, so both manifests are refused on the automatic path and applied only by a drain-gated manual run.
        for address in ("kubernetes_manifest.worker_nodepool", "kubernetes_manifest.worker_nodeclass"):
            with self.subTest(address=address):
                self.assertEqual(guard.CLASS_OF.get(address), guard.POOL_OBJECTS)
                self.assertIn(address, self.resources, "the guard lists an address that no resource of infra/ has")
                for index in ("", "[0]"):
                    line = f"create {address}{index}\n"
                    self.assertEqual(run(line, "workflow_run").code, 1, line)
                    manual = run(line, "workflow_dispatch", stage="all")
                    self.assertEqual(manual.code, 0, line)
                    self.assertIn("drain gate", manual.out)
                for change in ("update", "delete", "delete,create"):
                    self.assertEqual(run(f"{change} {address}[0]\n", "workflow_run").code, 1, change)

    def test_the_admission_policy_manifests_are_pool_objects_and_exist(self):
        # Slice 15b (issue #144): turning pod_backend_enabled off would remove the fence (the policies and the parameter) while a
        # worker pod may still run, so the four manifests are refused on the automatic path like the namespace.
        self.assertEqual(
            {address for address, klass in guard.LISTED if klass == guard.POOL_OBJECTS and "worker_policy" in address and "manifest" in address},
            set(WORKER_POLICY_ADDRESSES),
        )
        for address in WORKER_POLICY_ADDRESSES:
            with self.subTest(address=address):
                self.assertEqual(guard.CLASS_OF.get(address), guard.POOL_OBJECTS)
                self.assertIn(address, self.resources, "the guard lists an address that no resource of infra/ has")
                for change in ("create", "update", "delete", "delete,create"):
                    self.assertEqual(run(f"{change} {address}[0]\n", "workflow_run").code, 1, change)
                manual = run(f"create {address}[0]\n", "workflow_dispatch", stage="all")
                self.assertEqual(manual.code, 0)
                self.assertIn("drain gate", manual.out)

    def test_the_objects_of_the_worker_namespace_are_pool_objects_and_exist(self):
        # Slice 15a (issue #144): turning pod_backend_enabled off in INFRA_TFVARS would DELETE the namespace and every worker pod
        # in it (a disruption that ignores `do-not-disrupt`) and would remove the default-deny policy, so the three objects are
        # refused on the automatic path and applied only by a drain-gated manual run.
        self.assertEqual(
            {address for address, klass in guard.LISTED if klass == guard.POOL_OBJECTS and "worker" in address and "manifest" not in address},
            set(WORKER_NAMESPACE_ADDRESSES),
        )
        for address in WORKER_NAMESPACE_ADDRESSES:
            with self.subTest(address=address):
                self.assertEqual(guard.CLASS_OF.get(address), guard.POOL_OBJECTS)
                self.assertIn(address, self.resources, "the guard lists an address that no resource of infra/ has")
                for index in ("", "[0]"):
                    line = f"create {address}{index}\n"
                    self.assertEqual(run(line, "workflow_run").code, 1, line)
                    manual = run(line, "workflow_dispatch", stage="all")
                    self.assertEqual(manual.code, 0, line)
                    self.assertIn("drain gate", manual.out)
                for change in ("update", "delete", "delete,create"):
                    # The one update that can be allowed on the automatic path needs the plan JSON to prove it: without
                    # it the guard cannot decide (2) instead of refusing (1); everything else stays refused.
                    candidate = change == "update" and address == guard.IMAGE_ONLY_ADDRESS
                    self.assertEqual(run(f"{change} {address}[0]\n", "workflow_run").code, 2 if candidate else 1, change)
                    self.assertEqual(run(f"{change} {address}[0]\n", "pull_request").code, 1, change)

    def test_the_relay_service_of_the_pod_backend_is_a_pool_object_and_exists(self):
        # Slice 17 (issues #144 and #145): the Service is gated by the variable like the objects of the namespace; turning the variable
        # off deletes the way a worker pod reaches the relay, so it is refused on the automatic path like them. The Deployment is not
        # listed: the flip and an image promotion update it in place.
        self.assertEqual(guard.CLASS_OF.get("kubernetes_manifest.factory_deployment"), None)
        for address in POD_BACKEND_ADDRESSES:
            with self.subTest(address=address):
                self.assertEqual(guard.CLASS_OF.get(address), guard.POOL_OBJECTS)
                self.assertIn(address, self.resources, "the guard lists an address that no resource of infra/ has")
                for change in ("create", "update", "delete", "delete,create"):
                    self.assertEqual(run(f"{change} {address}[0]\n", "workflow_run").code, 1, change)
                manual = run(f"create {address}[0]\n", "workflow_dispatch", stage="all")
                self.assertEqual(manual.code, 0)
                self.assertIn("drain gate", manual.out)

    def test_the_manager_node_pool_taint_is_a_cluster_change_that_stage_all_refuses(self):
        # variables.tf, manager_pool_taint_enabled: a change of node_taints touches the node pool `factory`, a CLUSTER object, so it
        # is refused on the automatic path and on stage=all, and needs the attestation on stage=cluster.
        self.assertEqual(guard.CLASS_OF["azurerm_kubernetes_cluster_node_pool.factory"], guard.CLUSTER)
        line = "update azurerm_kubernetes_cluster_node_pool.factory\n"
        self.assertEqual(run(line, "workflow_run").code, 1)
        self.assertEqual(run(line, "workflow_dispatch", stage="all").code, 1)
        self.assertEqual(run(line, "workflow_dispatch", stage="cluster").code, 1)
        self.assertEqual(run(line, "workflow_dispatch", stage="cluster", snapshots="true").code, 0)

    # The completeness rule for the worker namespace: every spelling by which a Kubernetes resource can name it.

    @staticmethod
    def role(body, kind="kubernetes_role_v1"):
        return f'resource "{kind}" "launcher" {{\n{body}}}\n'

    def problems(self, *added, listed=None, peers=WORKER_NAMESPACE_PEERS):
        """list_problems on the real tree plus the added files' text (the tree's locals and the added ones both count)."""
        texts = {**self.texts, **{f"added{number}.tf": text for number, text in enumerate(added)}}
        return list_problems(terraform_resources(texts), guard.CLASS_OF if listed is None else listed, locals_map(texts), peers)

    def assertCaughtOnce(self, found, address="kubernetes_role_v1.launcher"):
        self.assertEqual(len(found), 1, found)
        self.assertIn(address, found[0])
        self.assertIn("worker namespace", found[0])

    def assertNeedsTheListing(self, *added, address="kubernetes_role_v1.launcher"):
        """Caught when unlisted or listed under the wrong class, and quiet once listed under POOL_OBJECTS."""
        self.assertCaughtOnce(self.problems(*added), address)
        self.assertCaughtOnce(self.problems(*added, listed={**guard.CLASS_OF, address: guard.CLUSTER}), address)
        self.assertEqual(self.problems(*added, listed={**guard.CLASS_OF, address: guard.POOL_OBJECTS}), [])

    def test_a_kubernetes_resource_that_names_the_worker_namespace_by_any_spelling_is_caught(self):
        spellings = {
            "the count local": '  count = local.worker_ns_count\n  metadata {\n    name = "x"\n  }\n',
            "the variable": "  count = var.pod_backend_enabled ? 1 : 0\n",
            "the variable in a for_each": "  for_each = var.pod_backend_enabled ? toset([1]) : toset([])\n",
            "the namespace resource": "  metadata {\n    namespace = kubernetes_namespace_v1.worker[0].metadata[0].name\n  }\n",
            "the service account resource": "  metadata {\n    namespace = kubernetes_service_account_v1.worker[0].metadata[0].namespace\n  }\n",
            "the policy resource": "  depends_on = [kubernetes_network_policy_v1.worker_default_deny]\n",
            "the contract name": "  metadata {\n    namespace = local.worker_contract.names.worker_namespace\n  }\n",
            "the contract name in a label selector": '  manifest = {\n    sel = { "kubernetes.io/metadata.name" = local.worker_contract.names.worker_namespace }\n  }\n',
            "the literal of podcontract": f'  metadata {{\n    namespace = "{pc.WORKER_NAMESPACE}"\n  }}\n',
            "the literal in a manifest": f'  manifest = {{\n    metadata = {{ namespace = "{pc.WORKER_NAMESPACE}" }}\n  }}\n',
            "the literal as a quoted key": f'  manifest = {{\n    "metadata" = {{ "namespace" = "{pc.WORKER_NAMESPACE}" }}\n  }}\n',
            "the literal in a heredoc": f"  yaml = <<-EOT\n    namespace: {pc.WORKER_NAMESPACE}\n  EOT\n",
        }
        for spelling, body in spellings.items():
            with self.subTest(spelling=spelling):
                self.assertNeedsTheListing(self.role(body))

    def test_a_namespace_argument_that_resolves_to_the_worker_namespace_through_a_local_is_caught(self):
        argument = "  metadata {\n    namespace = local.launcher_ns\n  }\n"
        cases = {
            "a local of the contract name": ("locals {\n  launcher_ns = local.worker_contract.names.worker_namespace\n}\n", argument),
            "a local of the literal": (f'locals {{\n  launcher_ns = "{pc.WORKER_NAMESPACE}"\n}}\n', argument),
            "a local of a local of the literal": (f'locals {{\n  launcher_ns = local.inner\n  inner = "{pc.WORKER_NAMESPACE}"\n}}\n', argument),
            "a local of the namespace resource": ("locals {\n  launcher_ns = kubernetes_namespace_v1.worker[0].metadata[0].name\n}\n", argument),
            "an attribute of an object local": (f'locals {{\n  launcher_ns = {{\n    namespace = "{pc.WORKER_NAMESPACE}"\n    other = "x"\n  }}\n}}\n', "  metadata {\n    namespace = local.launcher_ns.namespace\n  }\n"),
            "a local whose expression the brackets carry over lines": ("locals {\n  launcher_ns = local.worker_contract.names.worker_namespace\n}\n", "  metadata {\n    namespace = (\n      local.launcher_ns\n    )\n  }\n"),
            "a local in a conditional": ("locals {\n  launcher_ns = local.worker_contract.names.worker_namespace\n}\n", '  metadata {\n    namespace = var.on ? local.launcher_ns : "factory"\n  }\n'),
            "a local in a label selector": ("locals {\n  launcher_ns = local.worker_contract.names.worker_namespace\n}\n", '  manifest = {\n    sel = { "kubernetes.io/metadata.name" = local.launcher_ns }\n  }\n'),
        }
        for case, (definition, body) in cases.items():
            with self.subTest(case=case):
                self.assertNeedsTheListing(definition, self.role(body))

    def test_a_namespace_resource_named_by_a_local_and_a_resource_that_only_reads_a_listed_one_are_caught(self):
        namespace = 'locals {\n  ns = local.worker_contract.names.worker_namespace\n}\nresource "kubernetes_namespace_v1" "second" {\n  metadata {\n    name = local.ns\n  }\n}\n'
        self.assertNeedsTheListing(namespace, address="kubernetes_namespace_v1.second")
        # A binding that reads only the Role (never the namespace) belongs to the namespace as soon as the Role does.
        binding = 'resource "kubernetes_role_binding_v1" "bound" {\n  role_ref {\n    name = kubernetes_role_v1.launcher.metadata[0].name\n  }\n}\n'
        role = self.role('  metadata {\n    namespace = "factory-workers"\n  }\n')
        found = self.problems(role, binding)
        self.assertEqual(sorted(entry.split(" ", 1)[0] for entry in found), ["kubernetes_role_binding_v1.bound", "kubernetes_role_v1.launcher"])
        both = {**guard.CLASS_OF, "kubernetes_role_binding_v1.bound": guard.POOL_OBJECTS, "kubernetes_role_v1.launcher": guard.POOL_OBJECTS}
        self.assertEqual(self.problems(role, binding, listed=both), [])

    def test_a_helm_or_kubectl_resource_is_read_like_a_kubernetes_one(self):
        for kind in ("helm_release", "kubectl_manifest"):
            with self.subTest(kind=kind):
                self.assertNeedsTheListing(self.role("  count = local.worker_ns_count\n", kind), address=f"{kind}.launcher")

    def test_a_resource_of_another_namespace_or_a_comment_or_an_unrelated_attribute_needs_no_listing(self):
        argument = "  metadata {\n    namespace = local.other_ns\n  }\n"
        self.assertEqual(self.problems('locals {\n  other_ns = "factory"\n}\n', self.role(argument)), [])
        # `local.config.namespace` reads the attribute `namespace` only, not the attribute that names the worker namespace.
        config = 'locals {\n  config = {\n    namespace = "kube-system"\n    excluded = local.worker_contract.names.worker_namespace\n  }\n}\n'
        self.assertEqual(self.problems(config, self.role("  metadata {\n    namespace = local.config.namespace\n  }\n")), [])
        self.assertEqual(self.problems(self.role(f"  # in the namespace {pc.WORKER_NAMESPACE}: nothing to list\n  metadata {{\n    name = \"x\"\n  }}\n")), [])

    def test_what_the_rule_cannot_read_is_not_detected_and_with_the_variable_gate_it_is(self):
        # tools/infra_cluster_guard.py, infra/README.md and docs/pod-backend.md say which spellings a textual rule cannot see.
        # This pins that statement: if the rule learns one of them, this test fails and the three places change with it.
        unreadable = {
            "a function of other strings": '  metadata {\n    namespace = format("%s-workers", local.k8s_namespace)\n  }\n',
            "an interpolation": '  metadata {\n    namespace = "${local.k8s_namespace}-workers"\n  }\n',
            "a variable": "  metadata {\n    namespace = var.some_namespace\n  }\n",
            "a data source": "  metadata {\n    namespace = data.kubernetes_namespace_v1.read.metadata[0].name\n  }\n",
            "a file": '  manifest = yamldecode(file("role.yaml"))\n',
        }
        for spelling, body in unreadable.items():
            with self.subTest(spelling=spelling):
                self.assertEqual(self.problems(self.role(body)), [])
                self.assertCaughtOnce(self.problems(self.role(f"  count = local.worker_ns_count\n{body}")))

    def test_the_prepared_policy_is_the_one_exempted_peer_and_only_while_it_stays_one(self):
        (address,) = WORKER_NAMESPACE_PEERS
        self.assertIn(address, self.resources)
        self.assertEqual(list_problems(self.resources, guard.CLASS_OF), [])
        # Without the exemption the real tree fails on it alone: the rule does see the contract name in its selector.
        self.assertCaughtOnce(list_problems(self.resources, guard.CLASS_OF, peers=()), address)

        def peer(*lines):
            body = "\n".join(lines)
            return f'resource "kubernetes_manifest" "factory_network_policy" {{\n  manifest = {{\n    metadata = {{\n{body}\n    }}\n  }}\n  extra = local.worker_contract.names.worker_namespace\n}}\n'

        quiet = peer("      namespace = local.k8s_namespace")
        self.assertEqual(self.problems(quiet), [])
        alias = "locals {\n  alias_x = local.worker_contract.names.worker_namespace\n}\n"
        self.assertCaughtOnce(self.problems(alias, peer("      namespace = local.alias_x")), address)
        for case, text in {
            "it moves into the worker namespace": peer("      namespace = local.worker_contract.names.worker_namespace"),
            "the variable gates it": peer("      namespace = local.k8s_namespace").replace("  extra =", "  count = var.pod_backend_enabled ? 1 : 0\n  extra =", 1),
            "the count local gates it": peer("      namespace = local.k8s_namespace").replace("  extra =", "  count = local.worker_ns_count\n  extra =", 1),
            "it depends on the namespace": peer("      namespace = local.k8s_namespace").replace("  extra =", "  depends_on = [kubernetes_namespace_v1.worker]\n  extra =", 1),
            "it names no namespace of its own": peer("      name = \"x\""),
        }.items():
            with self.subTest(case=case):
                self.assertCaughtOnce(self.problems(text), address)

    def test_a_kubernetes_resource_outside_the_worker_namespace_needs_no_listing(self):
        text = 'resource "kubernetes_config_map_v1" "other" {\n  metadata {\n    namespace = "factory"\n  }\n}\n'
        resources = terraform_resources({**self.texts, "other.tf": text})
        self.assertEqual(list_problems(resources, guard.CLASS_OF), [])

    def test_the_real_tree_has_no_cluster_class_resource_that_is_not_listed_and_no_listed_address_without_a_resource(self):
        self.assertTrue(self.resources, "the scan found no resource: the pattern no longer matches the files")
        self.assertIn("azurerm_kubernetes_cluster.this", self.resources)
        self.assertEqual(list_problems(self.resources, guard.CLASS_OF), [])

    def test_the_cluster_class_is_exactly_what_the_cluster_stage_of_the_readme_targets_reach(self):
        # infra/README.md, "First apply": `stage=cluster` is four `-target` lines, the cluster, the factory node pool and the two role
        # assignments. The class is the first two (the guard refuses a plan that touches them on an automatic path); a pool object
        # that these lines could apply would belong to it, and a stale or mistyped address would be a stage that applies nothing.
        readme = (INFRA / "README.md").read_text(encoding="utf-8")
        section = readme[readme.index("\n## First apply\n") : readme.index("\n## Call the API\n")]
        self.assertIn("four `-target` lines", " ".join(section.split()))
        targets = re.findall(r"-target='?([A-Za-z0-9_.]+)'?", section)
        self.assertEqual(len(targets), 4, targets)
        self.assertEqual(len(set(targets)), 4, targets)
        cluster_class = {address for address, klass in guard.LISTED if klass == guard.CLUSTER}
        self.assertEqual({target for target in targets if target.startswith(CLUSTER_TYPE_PREFIX)}, cluster_class)
        self.assertEqual(
            set(targets) - cluster_class,
            {"azurerm_role_assignment.aks_cluster_admin", "azurerm_role_assignment.aks_cluster_user"},
        )
        for target in targets:
            with self.subTest(target=target):
                self.assertIn(target, self.resources, "the README targets an address that no resource of infra/ has")
        for address, klass in guard.LISTED:
            if klass == guard.POOL_OBJECTS:
                with self.subTest(address=address):
                    self.assertNotIn(address, targets)  # a pool object that stage=cluster could apply would belong to CLUSTER

    def test_no_module_can_hide_a_cluster_resource_from_the_lists(self):
        self.assertEqual(modules(self.texts), [])

    # The same scan on synthetic trees: adding a resource without listing it must fail.

    def test_a_new_node_pool_cluster_or_extension_that_is_not_listed_is_caught(self):
        for kind, name in (
            ("azurerm_kubernetes_cluster_node_pool", "extra"),
            ("azurerm_kubernetes_cluster", "second"),
            ("azurerm_kubernetes_cluster_extension", "flux"),
        ):
            with self.subTest(kind=kind):
                text = f'resource "{kind}" "{name}" {{\n  name = "x"\n}}\n'
                resources = terraform_resources({**self.texts, "added.tf": text})
                found = list_problems(resources, guard.CLASS_OF)
                self.assertEqual(len(found), 1, found)
                self.assertIn(f"{kind}.{name}", found[0])

    def test_a_listed_resource_added_with_its_listing_passes(self):
        text = 'resource "azurerm_kubernetes_cluster_node_pool" "extra" {\n  name = "x"\n}\n'
        resources = terraform_resources({**self.texts, "added.tf": text})
        listed = {**guard.CLASS_OF, "azurerm_kubernetes_cluster_node_pool.extra": guard.POOL_OBJECTS}
        self.assertEqual(list_problems(resources, listed), [])

    def test_an_address_removed_from_the_lists_is_caught(self):
        for address in guard.CLASS_OF:
            with self.subTest(address=address):
                listed = {key: value for key, value in guard.CLASS_OF.items() if key != address}
                self.assertEqual(len(list_problems(self.resources, listed)), 1)

    def test_a_listed_address_without_a_resource_is_caught(self):
        listed = {**guard.CLASS_OF, "azurerm_kubernetes_cluster_node_pool.gone": guard.POOL_OBJECTS}
        found = list_problems(self.resources, listed)
        self.assertEqual(len(found), 1)
        self.assertIn("gone", found[0])

    def test_a_node_pool_or_node_class_manifest_must_be_listed_as_a_pool_object(self):
        body = 'resource "kubernetes_manifest" "worker_pool" {\n  manifest = {\n    apiVersion = "karpenter.sh/v1"\n    kind = "NodePool"\n  }\n}\n'
        resources = terraform_resources({**self.texts, "pool.tf": body})
        self.assertEqual(len(list_problems(resources, guard.CLASS_OF)), 1)
        listed = {**guard.CLASS_OF, "kubernetes_manifest.worker_pool": guard.POOL_OBJECTS}
        self.assertEqual(list_problems(resources, listed), [])
        wrong_class = {**guard.CLASS_OF, "kubernetes_manifest.worker_pool": guard.CLUSTER}
        self.assertEqual(len(list_problems(resources, wrong_class)), 1)
        node_class = 'resource "kubernetes_manifest" "node_class" {\n  manifest = {\n    apiVersion = "karpenter.azure.com/v1beta1"\n  }\n}\n'
        more = terraform_resources({**self.texts, "class.tf": node_class})
        self.assertEqual(len(list_problems(more, guard.CLASS_OF)), 1)

    def test_an_azapi_resource_of_the_aks_api_must_be_listed(self):
        text = 'resource "azapi_update_resource" "pool_patch" {\n  type = "Microsoft.ContainerService/managedClusters/agentPools@2024-09-01"\n}\n'
        resources = terraform_resources({**self.texts, "patch.tf": text})
        found = list_problems(resources, guard.CLASS_OF)
        self.assertEqual(len(found), 1, found)
        self.assertIn("azapi_update_resource.pool_patch", found[0])
        listed = {**guard.CLASS_OF, "azapi_update_resource.pool_patch": guard.CLUSTER}
        self.assertEqual(list_problems(resources, listed), [])
        other = 'resource "azapi_update_resource" "table" {\n  type = "Microsoft.OperationalInsights/workspaces/tables@2022-10-01"\n}\n'
        self.assertEqual(list_problems(terraform_resources({**self.texts, "other.tf": other}), guard.CLASS_OF), [])

    def test_a_commented_out_resource_is_not_a_resource(self):
        text = '# resource "azurerm_kubernetes_cluster_node_pool" "idea" {\n#   name = "x"\n# }\n'
        resources = terraform_resources({**self.texts, "idea.tf": text})
        self.assertEqual(list_problems(resources, guard.CLASS_OF), [])

    def test_a_module_is_detected(self):
        self.assertEqual(modules({"m.tf": 'module "pool" {\n  source = "./pool"\n}\n'}), ["m.tf"])


class TheClaimIsTrue(unittest.TestCase):
    """The completeness rule is a text rule. The three places that describe it name every spelling it reads, the one resource it
    exempts and, in the same words, what it cannot see, so that the claim is literally what the tests above do."""

    LIMIT = (
        "a namespace that is computed from other strings (`format`, an interpolation), read from a `var.` or a data source, or decoded "
        "from a file (`yamldecode`), in a resource that `pod_backend_enabled` does not gate"
    )
    PLACES = ("infra/README.md", "docs/pod-backend.md", "tools/infra_cluster_guard.py")

    @staticmethod
    def flat(path):
        text = (ROOT / path).read_text(encoding="utf-8")
        return re.sub(r"\s+", " ", re.sub(r"(?m)^\s*#\s?", "", text))

    def test_each_place_names_every_spelling_the_rule_reads(self):
        for path in self.PLACES:
            flat = self.flat(path)
            for spelling in WORKER_NAMESPACE_SPELLINGS:
                with self.subTest(path=path, spelling=spelling):
                    self.assertIn(f"`{spelling}`", flat)

    def test_each_place_names_the_exempted_peer_says_it_reads_text_and_says_what_it_cannot_see(self):
        for path in self.PLACES:
            flat = self.flat(path)
            with self.subTest(path=path):
                self.assertIn(f"`{WORKER_NAMESPACE_PEERS[0]}`", flat)
                self.assertIn("reads the .tf files as text", flat.replace("`", ""))
                self.assertIn(self.LIMIT, flat)


if __name__ == "__main__":
    unittest.main()

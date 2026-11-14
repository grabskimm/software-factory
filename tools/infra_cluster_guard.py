#!/usr/bin/env python3
"""Refuse a Terraform plan that changes the AKS cluster or a node pool on a path that must not (infra/README.md, "Safe deployments").

An optional operator script for the operator's own deployment pipeline (the vocabulary is GitHub Actions': event names,
`::error::` lines, GITHUB_STEP_SUMMARY). Run it right after each `terraform plan`, before a reviewer is asked or anything
is paused or applied:

    infra_cluster_guard.py ACTIONS_FILE [PLAN_JSON]

ACTIONS_FILE is the action list the pipeline writes from the plan (one `<actions> <address>` line per resource that
changes, for example `update azurerm_kubernetes_cluster.this` or `delete,create azurerm_kubernetes_cluster_node_pool.factory`).
The guard only READS it: it does not change the file, the fingerprint that is computed from it or the plan.

PLAN_JSON (optional) is `terraform show -json` of the same plan. It is read for ONE purpose (issue #265): the automatic
path may apply an update of the worker admission policy's parameter (`kubernetes_config_map_v1.worker_policy_params`)
when, and only when, the plan shows that the update changes nothing but `data.image`, from one sha256 digest reference to
another (the allowed image of the pod backend moves with every image release). Any other change of that object, any
other pool object action and a missing, unreadable or oversized PLAN_JSON for such an update stay refused or undecided (exit 2).

Environment (all set by the pipeline, none is a secret): GUARD_EVENT (the event name), GUARD_STAGE (`all` or
`cluster`, the dispatch input), GUARD_SNAPSHOTS_TAKEN (the dispatch input `cluster_snapshots_taken`, only the
exact text `true` counts), GUARD_PR_LABELS (the labels of the pull request as a JSON list of names),
GITHUB_STEP_SUMMARY (optional).

Two classes of address are known, as DATA (LISTED below; tests/test_infra_cluster_guard.py reads the .tf files and fails
when a resource of these classes exists that is not listed):

  CLUSTER        the cluster and the factory node pool, the addresses a `stage=cluster` run applies (the -target lines
                 of the operator's pipeline). A rule "only stage=cluster" is meaningful only for addresses that stage can apply.
  POOL_OBJECTS   node pools that no such -target line reaches (the count-gated monitoring pool, any index), the NodePool
                 and AKSNodeClass objects of the node provisioning add-on and the objects of the worker namespace of the
                 pod backend (its namespace, service account and default-deny network policy). Changing or deleting one
                 is a manual disruption that ignores `do-not-disrupt`: deleting the NodePool or the namespace ends every
                 worker session on it, and deleting the policy removes the fence around a worker pod.

Rules (a no-op line is ignored; every other action counts, create, update, delete and replace). The events are GitHub
Actions' names: workflow_run is the automatic run after an image build, workflow_dispatch a manual run, pull_request a plan
on a pull request:

  event             CLUSTER                                       POOL_OBJECTS
  workflow_run      refused                                       refused, except an image-only update of
                                                                  worker_policy_params (see PLAN_JSON above)
  workflow_dispatch refused with stage=all; with stage=cluster    allowed (either stage), with the reminder that
                    allowed, and an update, delete or replace     the apply must go through the drain gate
                    also needs cluster_snapshots_taken=true
                    (a plain create does not)
  pull_request      refused unless the label infra-cluster-change is on the pull request (then a warning)

Exit status: 0 allowed, 1 refused, 2 the guard cannot decide (unreadable list, unknown event or stage): it fails closed.

Everything printed is a fixed message, a count, the action words (letters and commas only) and the address of a listed
resource with every character outside a small safe set replaced; no label, input or other text reaches the log
or the summary. Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import NamedTuple

CLUSTER = "CLUSTER"
POOL_OBJECTS = "POOL_OBJECTS"

# One entry per address and a class per entry. An address is `<type>.<name>` without an index: any index
# (`[0]`, `["key"]`) of a listed address belongs to it. The NodePool and AKSNodeClass `kubernetes_manifest` addresses
# (infra/worker_pool.tf, count-gated by worker_nodepool_enabled) are listed in the pull request that created them: turning
# the variable off in INFRA_TFVARS would DELETE the NodePool, a disruption that ignores `do-not-disrupt`. So are the three
# Kubernetes objects of the worker namespace (infra/worker_ns.tf, count-gated by pod_backend_enabled): turning that variable
# off would DELETE the namespace and every worker pod in it, and so are the admission policy parameter and the four policy and binding
# manifests (infra/worker_policy.tf, the same gate): turning the variable off would remove the fence while pods may still run. So are
# the Role and the RoleBinding of the launcher (infra/worker_rbac.tf: turning the variable off ends the launcher's right to create
# a pod) and the relay Service of the manager namespace (infra/pod_backend.tf, the same gate: the way a worker pod reaches the relay;
# the Deployment itself is not listed, an image promotion or the backend flip updates it in place and rolls the manager). A later object of that namespace is listed in the pull request
# that creates it: tests/test_infra_cluster_guard.py reads the .tf files as text and fails for a Kubernetes resource that names the
# worker namespace and is not listed, whether it names it by `local.worker_ns_count`, by `var.pod_backend_enabled`, by a reference to
# `kubernetes_namespace_v1.worker`, `kubernetes_service_account_v1.worker` or `kubernetes_network_policy_v1.worker_default_deny`, by
# `local.worker_contract.names.worker_namespace`, by the literal `factory-workers`, through a local that holds one of these (in a
# `namespace` argument or a namespace label selector), or by naming another resource that does. One resource is exempted by name,
# `kubernetes_manifest.factory_network_policy`: it names the namespace only as the peer of its relay rule, lives in the manager
# namespace and is not gated by the variable, so turning the variable off updates it in place. The rule reads text, so the following
# is not detected: a namespace that is computed from other strings (`format`, an interpolation), read from a `var.` or a data
# source, or decoded from a file (`yamldecode`), in a resource that `pod_backend_enabled` does not gate (the same resource with the
# gate is detected).
LISTED: tuple[tuple[str, str], ...] = (
    ("azurerm_kubernetes_cluster.this", CLUSTER),
    ("azurerm_kubernetes_cluster_node_pool.factory", CLUSTER),
    ("azurerm_kubernetes_cluster_node_pool.monitoring", POOL_OBJECTS),
    ("kubernetes_manifest.worker_nodepool", POOL_OBJECTS),
    ("kubernetes_manifest.worker_nodeclass", POOL_OBJECTS),
    ("kubernetes_namespace_v1.worker", POOL_OBJECTS),
    ("kubernetes_service_account_v1.worker", POOL_OBJECTS),
    ("kubernetes_network_policy_v1.worker_default_deny", POOL_OBJECTS),
    ("kubernetes_config_map_v1.worker_policy_params", POOL_OBJECTS),
    ("kubernetes_manifest.worker_policy_pods", POOL_OBJECTS),
    ("kubernetes_manifest.worker_policy_pods_binding", POOL_OBJECTS),
    ("kubernetes_manifest.worker_policy_secrets", POOL_OBJECTS),
    ("kubernetes_manifest.worker_policy_secrets_binding", POOL_OBJECTS),
    ("kubernetes_role_v1.worker_launcher", POOL_OBJECTS),
    ("kubernetes_role_binding_v1.worker_launcher", POOL_OBJECTS),
    ("kubernetes_service_v1.factory_relay", POOL_OBJECTS),
)
CLASS_OF = dict(LISTED)

LABEL = "infra-cluster-change"

PULL_REQUEST = "pull_request"
WORKFLOW_RUN = "workflow_run"
WORKFLOW_DISPATCH = "workflow_dispatch"
EVENTS = (PULL_REQUEST, WORKFLOW_RUN, WORKFLOW_DISPATCH)
STAGES = ("all", "cluster")

MAX_LIST_BYTES = 1_048_576
MAX_PLAN_BYTES = 64 * 1024 * 1024
IMAGE_ONLY_ADDRESS = "kubernetes_config_map_v1.worker_policy_params"
IMAGE_KEY = "image"
IMAGE_REFERENCE = re.compile(r"[a-z0-9][a-z0-9./_-]{0,200}@sha256:[0-9a-f]{64}")
MAX_ADDRESS = 200
LINE = re.compile(r"^([a-z-]+(?:,[a-z-]+)*) (\S.*)$")
UNSAFE = re.compile(r'[^A-Za-z0-9_.\[\]"-]')

REFUSALS = {
    "automatic_cluster": (
        "A plan that changes the AKS cluster or its factory node pool is refused on the automatic path (the run after an image "
        "build). Nothing was applied. A cluster change goes only through a manual run with stage=cluster (infra/README.md, "
        '"Safe deployments"). If INFRA_TFVARS was changed ahead of that run, this automatic run raced it: do the manual run first.'
    ),
    "automatic_pool": (
        "A plan that changes a node pool object (the monitoring node pool, a NodePool, an AKSNodeClass or an object of the "
        "worker namespace) is refused on the automatic path. Changing or deleting one is a manual disruption that ignores "
        "do-not-disrupt. Nothing was applied. Run it by hand so that the apply goes through the drain gate."
    ),
    "stage_all": (
        "stage=all never applies a change to the AKS cluster or its factory node pool. Run it by hand with stage=cluster for "
        'such a change (infra/README.md, "Safe deployments"). Nothing was applied.'
    ),
    "snapshots": (
        "An update, delete or replace of the AKS cluster or its factory node pool needs the dispatch input "
        "cluster_snapshots_taken=true: the attestation that a snapshot of the data disk and of the Caddy disk (and of the "
        "Prometheus disk when monitoring is on) exists. Nothing was applied."
    ),
    "pr_unlabeled": (
        "This pull request changes the AKS cluster or a node pool. Add the label infra-cluster-change to show that this is "
        "intended; the plan then runs again. Nothing is applied by a pull request."
    ),
}

# The kind of an advisory (a workflow command level) and its fixed text.
ADVICE = {
    "pr_labeled": (
        "warning",
        "This pull request changes the AKS cluster or a node pool and carries the label infra-cluster-change. Merging applies "
        "nothing, and the next automatic run after an image build refuses a plan with such an action: apply the change first "
        'by a manual run with stage=cluster (infra/README.md, "Safe deployments").',
    ),
    "drain_gate": (
        "notice",
        "An apply that changes a node pool object must go through the drain gate: set FACTORY_MANAGER_URL and "
        'FACTORY_MANAGER_AUDIENCE and approve only when the pause, drain and wait steps will run (infra/README.md, "Safe deployments").',
    ),
    "image_only": (
        "notice",
        "The only node pool object action is an image-only update of the worker policy parameter (the allowed image moves "
        "with the release): allowed on the automatic path. The apply goes through the drain gate like the Deployment's.",
    ),
    "attested": (
        "notice",
        "cluster_snapshots_taken=true was set by the person who started this run. The guard cannot check that the snapshots "
        "exist: it only refuses a run that did not attest.",
    ),
}

UNREADABLE = {
    "list": "the action list could not be read: the guard refuses to decide",
    "line": "a line of the action list is not '<actions> <address>': the guard refuses to decide",
    "event": "the event is not one the guard knows: it refuses to decide",
    "stage": "the stage is neither all nor cluster: the guard refuses to decide",
    "plan": "the plan JSON could not be read for an update of the worker policy parameter: the guard refuses to decide",
}


class Unreadable(Exception):
    """The guard cannot decide; the message is a fixed text from UNREADABLE and never carries input."""


class Touch(NamedTuple):
    actions: str
    address: str
    klass: str
    image_only: bool = False  # an update that changes only data.image of the worker policy parameter (proven from the plan JSON)


class Context(NamedTuple):
    event: str
    stage: str
    snapshots_taken: bool
    labeled: bool


class Verdict(NamedTuple):
    refusals: tuple
    advice: tuple


def base_address(address: str) -> str:
    """The address without its index: `a.b[0]` and `a.b["x"]` are both `a.b`."""
    return address.split("[", 1)[0]


def parse(text: str) -> list[Touch]:
    """The lines of the action list that change a listed address (a no-op line and any other address are skipped)."""
    touches = []
    for line in text.splitlines():
        if not line.strip():
            continue
        found = LINE.match(line)
        if not found:
            raise Unreadable(UNREADABLE["line"])
        actions, address = found.group(1), found.group(2).strip()
        klass = CLASS_OF.get(base_address(address))
        if klass and actions != "no-op":
            touches.append(Touch(actions, UNSAFE.sub("?", address)[:MAX_ADDRESS], klass))
    return touches


def is_image_candidate(touch: Touch) -> bool:
    """An update (and nothing else) of the worker policy parameter: the one touch whose plan detail is looked at."""
    return touch.klass == POOL_OBJECTS and touch.actions == "update" and base_address(touch.address) == IMAGE_ONLY_ADDRESS


def _flat_true(value) -> bool:
    """Whether any leaf of a Terraform ``after_unknown`` or sensitivity structure is true (a value not known yet or hidden)."""
    if isinstance(value, dict):
        return any(_flat_true(item) for item in value.values())
    if isinstance(value, list):
        return any(_flat_true(item) for item in value)
    return value is True


def image_only_addresses(plan_text: str) -> set:
    """The addresses of the worker policy parameter whose planned update changes only ``data.image`` between two digest references.

    Read from ``terraform show -json``: the resource change must be exactly ``["update"]``; ``data`` must hold the one key
    ``image`` before and after, each a ``<repository>@sha256:<64 hex>`` reference; everything else of the object (metadata,
    labels, the id, ...) must be equal before and after; and nothing of it may be unknown or sensitive. Anything else, and
    JSON that is not a plan, yields no address (so the update stays refused): the function never raises on content.
    """
    try:
        plan = json.loads(plan_text)
    except ValueError:
        return set()
    changes = plan.get("resource_changes") if isinstance(plan, dict) else None
    found = set()
    for item in changes if isinstance(changes, list) else []:
        if not isinstance(item, dict) or base_address(str(item.get("address", ""))) != IMAGE_ONLY_ADDRESS:
            continue
        change = item.get("change")
        if not isinstance(change, dict) or change.get("actions") != ["update"]:
            continue
        before, after = change.get("before"), change.get("after")
        if not isinstance(before, dict) or not isinstance(after, dict):
            continue
        data_before, data_after = before.get("data"), after.get("data")
        if not isinstance(data_before, dict) or not isinstance(data_after, dict):
            continue
        if set(data_before) != {IMAGE_KEY} or set(data_after) != {IMAGE_KEY}:
            continue
        images = (data_before[IMAGE_KEY], data_after[IMAGE_KEY])
        if not all(isinstance(image, str) and IMAGE_REFERENCE.fullmatch(image) for image in images):
            continue
        rest_before = {key: value for key, value in before.items() if key != "data"}
        rest_after = {key: value for key, value in after.items() if key != "data"}
        if rest_before != rest_after:
            continue
        if _flat_true(change.get("after_unknown")) or _flat_true(change.get("before_sensitive")) or _flat_true(change.get("after_sensitive")):
            continue
        found.add(str(item["address"]))
    return found


def mark_image_only(touches: list, plan_path: str | None) -> list:
    """``touches`` with ``image_only`` set where the plan JSON proves it; ``Unreadable`` when a candidate has no readable plan."""
    if not any(is_image_candidate(touch) for touch in touches):
        return touches
    if plan_path is None:
        raise Unreadable(UNREADABLE["plan"])
    try:
        with open(plan_path, encoding="utf-8") as handle:
            text = handle.read(MAX_PLAN_BYTES + 1)
    except (OSError, UnicodeDecodeError):
        raise Unreadable(UNREADABLE["plan"]) from None
    if len(text) > MAX_PLAN_BYTES:
        raise Unreadable(UNREADABLE["plan"])
    proven = {UNSAFE.sub("?", address)[:MAX_ADDRESS] for address in image_only_addresses(text)}
    return [touch._replace(image_only=True) if is_image_candidate(touch) and touch.address in proven else touch for touch in touches]


def judge(touch: Touch, context: Context) -> tuple[str, str]:
    """(refusal code, advice code) for one touch; each is an empty string when there is none."""
    if context.event == PULL_REQUEST:
        return ("", "pr_labeled") if context.labeled else ("pr_unlabeled", "")
    if context.event == WORKFLOW_RUN:
        if touch.klass == POOL_OBJECTS and touch.image_only:
            return "", "image_only"
        return ("automatic_cluster" if touch.klass == CLUSTER else "automatic_pool", "")
    if touch.klass == POOL_OBJECTS:
        return "", "drain_gate"
    if context.stage != "cluster":
        return "stage_all", ""
    if touch.actions == "create":
        return "", ""
    return ("", "attested") if context.snapshots_taken else ("snapshots", "")


def decide(touches: list[Touch], context: Context) -> Verdict:
    refusals: list[str] = []
    advice: list[str] = []
    for touch in touches:
        refusal, note = judge(touch, context)
        if refusal and refusal not in refusals:
            refusals.append(refusal)
        if note and note not in advice:
            advice.append(note)
    return Verdict(tuple(refusals), tuple(advice))


def read_labels(raw: str) -> list[str]:
    """The label names from the JSON list; anything else is no label, so the guard fails closed."""
    try:
        names = json.loads(raw or "[]")
    except ValueError:
        return []
    if not isinstance(names, list):
        return []
    return [name for name in names if isinstance(name, str)]


def read_context(env) -> Context:
    event = env.get("GUARD_EVENT", "").strip()
    if event not in EVENTS:
        raise Unreadable(UNREADABLE["event"])
    stage = env.get("GUARD_STAGE", "").strip()
    if stage not in STAGES:
        raise Unreadable(UNREADABLE["stage"])
    return Context(
        event=event,
        stage=stage,
        snapshots_taken=env.get("GUARD_SNAPSHOTS_TAKEN", "").strip() == "true",
        labeled=LABEL in read_labels(env.get("GUARD_PR_LABELS", "")),
    )


def read_actions(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read(MAX_LIST_BYTES + 1)
    except (OSError, UnicodeDecodeError):
        raise Unreadable(UNREADABLE["list"]) from None
    if len(text) > MAX_LIST_BYTES:
        raise Unreadable(UNREADABLE["list"])
    return text


def say(text: str) -> None:
    print(text, flush=True)


def summary(lines: list) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("".join(f"{line}\n" for line in lines))


def report(touches: list[Touch], verdict: Verdict) -> None:
    count = f"cluster-touching actions in this plan: {len(touches)}"
    listed = [f"{touch.actions} {touch.address} ({touch.klass})" for touch in touches]
    say(count)
    for line in listed:
        say(f"  {line}")
    for code in verdict.refusals:
        say(f"::error::{REFUSALS[code]}")
    for code in verdict.advice:
        level, text = ADVICE[code]
        say(f"::{level}::{text}")
    outcome = "refused" if verdict.refusals else "allowed"
    block = ["", "```", *listed, "```"] if listed else []
    summary(["### Cluster guard", "", count, *block, "", f"Guard result: **{outcome}**"])


def main(argv: list | None = None, environ=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) not in (1, 2):
        print("usage: infra_cluster_guard.py ACTIONS_FILE [PLAN_JSON]", file=sys.stderr)
        return 2
    env = os.environ if environ is None else environ
    try:
        context = read_context(env)
        touches = parse(read_actions(args[0]))
        if context.event == WORKFLOW_RUN:  # the one path where the plan detail can change a verdict
            touches = mark_image_only(touches, args[1] if len(args) == 2 else None)
    except Unreadable as problem:
        say(f"::error::{problem}")
        summary(["### Cluster guard", "", f"Guard result: **cannot decide** ({problem})"])
        return 2
    verdict = decide(touches, context)
    report(touches, verdict)
    return 1 if verdict.refusals else 0


if __name__ == "__main__":
    sys.exit(main())

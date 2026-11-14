#!/usr/bin/env python3
"""Read-only checks of the live runbook (slice 21 of the pod backend, docs/pod-backend-runbook.md).

Every command here runs one or a few fixed `az` or `kubectl` calls as the person who holds the credentials, judges the answer
against a number or a name the repository already fixes, and prints a short verdict. Nothing is created, changed or deleted:
the only calls that are not plain reads are the server-side DRY RUN of `denial-sample` (like the matrix of tools/policy_matrix.py)
and the access review that `kubectl auth can-i` makes; neither persists anything. Safe by construction:

* argv lists only (no shell), every call bounded by a timeout, a missing binary or a timeout is a verdict, not a crash;
* only fixed text, counts and numbers are printed, plus a few words that came from the cluster or from Azure (a VM size, a
  quota name) after every character outside a small safe set was replaced; never a manifest, an answer, a name of a pod or a node,
  an environment, a token or a secret; `--record DIR --step S` writes the observation as a recording
  (tools/live_recording.py: scrubbed, no Secret ever, mode 0600) and prints only its step, kind and the number of redactions;
* a command that calls `kubectl` refuses to run without `--kubeconfig FILE` (exit status 2): with no file it would address the default
  context, which may be production;
* no command takes a command line, a path to run or a value to put into a call except through a fixed pattern.

    quota             --location L [--family F] [--worker-limit-cpu W] [--new-vcpu B] [--surge-vcpu S] [--family-new-vcpu N]
    nodes             the size and the allocatable of every node, by pool
    node-requests     what the pods request on each node (the system node's headroom)
    crds              the field names the Terraform writes against the installed NodePool and AKSNodeClass definitions
    reads             --profile plan: every kind the plan identity reads (run it with that identity's kubeconfig)
    can-i             --profile launcher|worker --as USER: what an identity may and may not do in the worker namespace
    colocation        no node holds the pods of two repositories (the pod design's "one repository per node")
    capture           --kind pod|pod-events|nodeclaim|nodepool|nodes|crd|az-aks: record one observation
    denial-sample     record the first line of a real admission denial (a dry run as the launcher identity)
    pod-fields        the fields of the pod API of THIS cluster that neither the admission policy nor a golden pod names
    golden-image      the image of the golden pod (what the policy parameter must hold while the matrix runs)

Exit status: 0 the check passed, 1 it found a problem, 2 a usage or environment error. Standard library only (the contract names
come from ``factory.podcontract``, hence PYTHONPATH=src).
"""

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import live_recording as rec
from factory import podcontract as pc

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "golden"
WORKER_NS = pc.WORKER_NAMESPACE
MANAGER_NS = pc.MANAGER_NAMESPACE
LABEL_REPO = f"{pc.LABEL_PREFIX}repo"
LAUNCHER_USER = f"system:serviceaccount:{MANAGER_NS}:factory"
WORKER_USER = f"system:serviceaccount:{WORKER_NS}:{pc.WORKER_SERVICE_ACCOUNT}"

NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,60}[a-z0-9])?")
POD_NAME = re.compile(re.escape(pc.POD_NAME_PREFIX) + r"[a-z0-9](?:[a-z0-9-]{0,40}[a-z0-9])?")
AZURE_NAME = re.compile(r"[A-Za-z0-9._()-]{1,90}")
FAMILY = re.compile(r"[A-Za-z0-9]{3,40}")
LOCATION = re.compile(r"[a-z0-9]{3,30}")
USER = re.compile(r"[A-Za-z0-9:._@-]{1,200}")
DEFAULT_TIMEOUT = 60

# The three custom resource definitions the Terraform writes (infra/worker_pool.tf) and the field paths it writes into them. A
# path part ending in [] is a list of objects. `required` must exist; `info` is reported (where the documentation disagrees with
# itself); `enums` are values the Terraform writes that the schema must allow.
CRDS = {
    "nodepool": {
        "name": "nodepools.karpenter.sh",
        "version": "v1",
        "required": (
            "spec.template.metadata.labels",
            "spec.template.spec.expireAfter",
            "spec.template.spec.nodeClassRef.group",
            "spec.template.spec.nodeClassRef.kind",
            "spec.template.spec.nodeClassRef.name",
            "spec.template.spec.taints[].key",
            "spec.template.spec.taints[].value",
            "spec.template.spec.taints[].effect",
            "spec.template.spec.startupTaints[].key",
            "spec.template.spec.requirements[].key",
            "spec.template.spec.requirements[].operator",
            "spec.template.spec.requirements[].values",
            "spec.disruption.consolidationPolicy",
            "spec.disruption.consolidateAfter",
            "spec.limits",
        ),
        "info": ("spec.disruption.expireAfter", "spec.template.spec.terminationGracePeriod", "spec.disruption.budgets"),
        "enums": {"spec.disruption.consolidationPolicy": ("WhenEmpty",)},
    },
    "nodeclass": {
        "name": "aksnodeclasses.karpenter.azure.com",
        "version": "v1beta1",
        "required": ("spec.imageFamily", "spec.kubelet.podPidsLimit"),
        "info": ("spec.osDiskSizeGB",),
        "enums": {"spec.imageFamily": ("AzureLinux",)},
    },
}

# What the read-only plan identity must be able to read (docs/pod-backend.md, "Terraform B2", and infra/README.md, "Custom role").
PLAN_READS = (
    ("namespaces", None),
    ("serviceaccounts", WORKER_NS),
    ("networkpolicies.networking.k8s.io", WORKER_NS),
    ("configmaps", WORKER_NS),
    ("roles.rbac.authorization.k8s.io", WORKER_NS),
    ("rolebindings.rbac.authorization.k8s.io", WORKER_NS),
    ("deployments.apps", MANAGER_NS),
    ("configmaps", MANAGER_NS),
    ("customresourcedefinitions.apiextensions.k8s.io", None),
    ("storageclasses.storage.k8s.io", None),
    ("clusterroles.rbac.authorization.k8s.io", None),
    ("clusterrolebindings.rbac.authorization.k8s.io", None),
    ("nodepools.karpenter.sh", None),
    ("aksnodeclasses.karpenter.azure.com", None),
    ("validatingadmissionpolicies.admissionregistration.k8s.io", None),
    ("validatingadmissionpolicybindings.admissionregistration.k8s.io", None),
)

# (verb, resource, namespace, expected). The launcher's Role: pods create, get, list and delete and secrets create and delete in
# the worker namespace; no exec, no secret get or list, no pods/log (docs/pod-backend.md, "Architecture"). The worker pod's
# service account has no binding at all.
LAUNCHER_CAN = (
    ("create", "pods", WORKER_NS, "yes"),
    ("get", "pods", WORKER_NS, "yes"),
    ("list", "pods", WORKER_NS, "yes"),
    ("delete", "pods", WORKER_NS, "yes"),
    ("create", "secrets", WORKER_NS, "yes"),
    ("delete", "secrets", WORKER_NS, "yes"),
    ("get", "secrets", WORKER_NS, "no"),
    ("list", "secrets", WORKER_NS, "no"),
    ("watch", "pods", WORKER_NS, "no"),
    ("update", "pods", WORKER_NS, "no"),
    ("patch", "pods", WORKER_NS, "no"),
    ("update", "secrets", WORKER_NS, "no"),
    ("create", "pods/exec", WORKER_NS, "no"),
    ("get", "pods/log", WORKER_NS, "no"),
    ("create", "pods", MANAGER_NS, "no"),
    ("get", "secrets", MANAGER_NS, "no"),
    ("list", "namespaces", None, "no"),
)
WORKER_CAN = tuple((verb, resource, ns, "no") for verb, resource, ns, _ in LAUNCHER_CAN if verb in ("create", "get", "list", "delete")) + (
    ("create", "pods/exec", MANAGER_NS, "no"),
)

CAPTURE_KINDS = ("pod", "pod-events", "nodeclaim", "nodepool", "nodes", "crd", "az-aks")
AKS_QUERY = (
    "{kubernetes:currentKubernetesVersion,tier:sku.tier,plugin:networkProfile.networkPlugin,mode:networkProfile.networkPluginMode,"
    "dataplane:networkProfile.networkDataplane,policy:networkProfile.networkPolicy,outbound:networkProfile.outboundType,"
    "nap:nodeProvisioningProfile.mode,pools:agentPoolProfiles[].{name:name,size:vmSize,os:osSku,count:count,mode:mode,"
    "version:orchestratorVersion}}"
)


class Usage(Exception):
    """A request the tool refuses before it calls anything (exit status 2); the message is a fixed text."""


@dataclass(frozen=True)
class Outcome:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


class Cli:
    """One `kubectl` and one `az`, bounded by a timeout. `runner` is subprocess.run (a test passes a fake)."""

    def __init__(self, kubectl="kubectl", az="az", kubeconfig=None, timeout=DEFAULT_TIMEOUT, runner=subprocess.run):
        self.kubectl_binary = kubectl
        self.az_binary = az
        self.kubeconfig = kubeconfig
        self.timeout = timeout
        self.runner = runner

    def kubectl_argv(self, *tail, user=None):
        argv = [self.kubectl_binary]
        if self.kubeconfig:
            argv.extend(["--kubeconfig", self.kubeconfig])
        if user:
            argv.extend(["--as", user])
        argv.extend(tail)
        return argv

    def run(self, argv, payload=None):
        try:
            done = self.runner(argv, input=payload, capture_output=True, text=True, timeout=self.timeout, check=False)
        except subprocess.TimeoutExpired:
            return Outcome(124, timed_out=True)
        except OSError:
            return Outcome(127, stderr="the program could not be run")
        return Outcome(done.returncode, done.stdout or "", done.stderr or "")

    def kubectl(self, *tail, user=None):
        return self.run(self.kubectl_argv(*tail, user=user))

    def az(self, *tail):
        return self.run([self.az_binary, *tail])


def load_json(outcome):
    """The JSON document of a successful call, or None."""
    if outcome.returncode != 0:
        return None
    try:
        return json.loads(outcome.stdout)
    except ValueError:
        return None


def failed_call(outcome, what, emit):
    """Print the fixed verdict for a call that gave no usable answer; always one problem."""
    if outcome.timed_out:
        emit(f"FAIL {what}: the call timed out")
    elif outcome.returncode == 127:
        emit(f"FAIL {what}: the program could not be run")
    elif "forbidden" in outcome.stderr.lower():
        emit(f"FAIL {what}: forbidden")
    else:
        emit(f"FAIL {what}: the call failed or its answer was unreadable")
    return 1


def cpu_milli(text):
    """Millicpu of a Kubernetes cpu quantity, or None."""
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(m?)", str(text))
    if not match:
        return None
    value = float(match.group(1))
    return int(round(value if match.group(2) else value * 1000))


MEMORY_UNITS = {"": 1, "k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12, "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4}


def memory_mib(text):
    """Mebibytes of a Kubernetes memory or storage quantity, or None."""
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(Ki|Mi|Gi|Ti|k|M|G|T)?", str(text))
    if not match:
        return None
    return int(float(match.group(1)) * MEMORY_UNITS[match.group(2) or ""] / 1024**2)


def number(value):
    """An int out of an int or a digit string (az prints either), or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


# --- recording --------------------------------------------------------------------------------------------------------


def record(args, emit, kind, payload, argv=None, returncode=None, clock=None):
    """Write the observation when --record was given; the number of problems (a failed write is one)."""
    if not args.record:
        return 0
    try:
        document = rec.make(args.step, kind, payload, argv, returncode, tool="live_checks.py " + args.command, clock=clock)
        rec.write(document, args.record)
    except rec.Refused as refusal:
        emit(f"FAIL not recorded: {refusal.code}")
        return 1
    except OSError:
        emit("FAIL not recorded: the file could not be written")
        return 1
    emit(f"recorded {document['step']} {document['kind']}: {document['redactions']} redactions")
    return 0


# --- quota ------------------------------------------------------------------------------------------------------------


def usage_row(rows, wanted):
    for row in rows:
        name = row.get("name") if isinstance(row, dict) else None
        if isinstance(name, dict) and str(name.get("value", "")).lower() == wanted.lower():
            used, limit = number(row.get("currentValue")), number(row.get("limit"))
            if used is not None and limit is not None:
                return used, limit
    return None


def cmd_quota(cli, args, emit, clock=None):
    """The vCPU the plan needs, against what `az vm list-usage` says is left (the regional total binds first)."""
    argv = [cli.az_binary, "vm", "list-usage", "--location", args.location, "-o", "json"]
    outcome = cli.run(argv)
    rows = load_json(outcome)
    if not isinstance(rows, list):
        return failed_call(outcome, "quota", emit)
    problems = record(args, emit, "az-quota", rows, argv, outcome.returncode, clock)
    regional = usage_row(rows, "cores")
    if regional is None:
        emit("FAIL quota: the regional total (cores) is not in the answer")
        return problems + 1
    planned = args.new_vcpu + args.worker_limit_cpu + args.surge_vcpu
    emit(f"regional: used {regional[0]} of {regional[1]}, planned {planned}, margin {regional[1] - regional[0] - planned}")
    fits = regional[0] + planned <= regional[1]
    if args.family:
        family = usage_row(rows, args.family)
        if family is None:
            emit(f"FAIL quota: the family {rec.sanitise(args.family, 40)} is not in the answer")
            return problems + 1
        wanted = args.worker_limit_cpu + args.family_new_vcpu
        emit(f"family: used {family[0]} of {family[1]}, planned {wanted}, margin {family[1] - family[0] - wanted}")
        fits = fits and family[0] + wanted <= family[1]
    emit("quota: fits" if fits else "FAIL quota: does not fit, change nothing and lower the limit or ask for more quota")
    return problems + (0 if fits else 1)


# --- nodes ------------------------------------------------------------------------------------------------------------


def node_pool(node):
    labels = (node.get("metadata") or {}).get("labels") or {}
    if labels.get(pc.WORKER_NODE_LABEL_KEY) == pc.WORKER_NODE_LABEL_VALUE:
        return "worker"
    if labels.get("kubernetes.azure.com/mode") == "system":
        return "system"
    return "other"


def node_ready(node):
    for condition in (node.get("status") or {}).get("conditions") or ():
        if condition.get("type") == "Ready":
            return condition.get("status") == "True"
    return False


def cmd_nodes(cli, args, emit, clock=None):
    argv = cli.kubectl_argv("get", "nodes", "-o", "json")
    outcome = cli.run(argv)
    document = load_json(outcome)
    if not isinstance(document, dict) or not isinstance(document.get("items"), list):
        return failed_call(outcome, "nodes", emit)
    problems = record(args, emit, "nodes", document, argv, outcome.returncode, clock)
    nodes = [n for n in document["items"] if isinstance(n, dict)]
    pools = [node_pool(n) for n in nodes]
    ready = sum(1 for n in nodes if node_ready(n))
    emit(
        f"nodes: {len(nodes)} total, {pools.count('worker')} worker, {pools.count('system')} system, "
        f"{pools.count('other')} other, {ready} ready"
    )
    for node, pool in zip(nodes, pools):
        if pool != "worker":
            continue
        status = node.get("status") or {}
        allocatable = status.get("allocatable") or {}
        size = ((node.get("metadata") or {}).get("labels") or {}).get("node.kubernetes.io/instance-type", "unknown")
        cpu, mem, disk = (
            cpu_milli(allocatable.get("cpu")),
            memory_mib(allocatable.get("memory")),
            memory_mib(allocatable.get("ephemeral-storage")),
        )
        if None in (cpu, mem, disk):
            emit("FAIL a worker node reports an allocatable quantity that is not understood")
            problems += 1
            continue
        emit(
            f"worker node: size {rec.sanitise(size, 40)}, allocatable cpu {cpu} mCPU, memory {mem} MiB, "
            f"ephemeral-storage {disk // 1024} GiB, ready {'yes' if node_ready(node) else 'no'}"
        )
    return problems


def pod_requests(pod):
    """(millicpu, MiB) a pod asks the scheduler for: the larger of its containers' sum and its biggest init container."""

    def total(containers, combine):
        cpus, mems = [], []
        for container in containers or ():
            requests = ((container or {}).get("resources") or {}).get("requests") or {}
            cpus.append(cpu_milli(requests.get("cpu", "0")))
            mems.append(memory_mib(requests.get("memory", "0")))
        if None in cpus or None in mems:
            return None
        return combine(cpus), combine(mems)

    spec = pod.get("spec") or {}
    main = total(spec.get("containers"), sum)
    init = total(spec.get("initContainers"), lambda values: max(values, default=0))
    if main is None or init is None:
        return None
    return max(main[0], init[0]), max(main[1], init[1])


def cmd_node_requests(cli, args, emit, clock=None):
    """What the pods request on each node against what it can allocate: the system node's headroom before and after a change."""
    nodes_outcome = cli.kubectl("get", "nodes", "-o", "json")
    pods_outcome = cli.kubectl("get", "pods", "--all-namespaces", "-o", "json", "--field-selector", "status.phase!=Succeeded,status.phase!=Failed")
    nodes, pods = load_json(nodes_outcome), load_json(pods_outcome)
    if not isinstance(nodes, dict) or not isinstance(pods, dict):
        return failed_call(nodes_outcome if not isinstance(nodes, dict) else pods_outcome, "node-requests", emit)
    requested, problems = {}, 0
    for pod in pods.get("items") or ():
        node = (pod.get("spec") or {}).get("nodeName")
        if not node:
            continue
        asked = pod_requests(pod)
        if asked is None:
            emit("FAIL a pod requests a quantity that is not understood")
            problems += 1
            continue
        total = requested.setdefault(node, [0, 0])
        total[0] += asked[0]
        total[1] += asked[1]
    for index, node in enumerate(sorted(nodes.get("items") or (), key=lambda n: (node_pool(n), (n.get("metadata") or {}).get("name", ""))), 1):
        allocatable = (node.get("status") or {}).get("allocatable") or {}
        cpu, mem = cpu_milli(allocatable.get("cpu")), memory_mib(allocatable.get("memory"))
        if cpu is None or mem is None or cpu <= 0 or mem <= 0:
            emit("FAIL a node reports an allocatable quantity that is not understood")
            problems += 1
            continue
        used = requested.get((node.get("metadata") or {}).get("name"), [0, 0])
        emit(
            f"node {index} ({node_pool(node)}): cpu requested {used[0]} of {cpu} mCPU ({used[0] * 100 // cpu} percent), "
            f"memory requested {used[1]} of {mem} MiB ({used[1] * 100 // mem} percent)"
        )
        if node_pool(node) == "system" and cpu - used[0] < args.min_headroom_mcpu:
            emit(f"note: the system node has less than {args.min_headroom_mcpu} mCPU of headroom (a suggested trigger, not a measured limit)")
    return problems


# --- the custom resource definitions ------------------------------------------------------------------------------------


def lookup(schema, path):
    """The schema node at `path` (dotted, `[]` for a list of objects), None when absent, "unchecked" when it is free-form."""
    node = schema
    for part in path.split("."):
        is_list = part.endswith("[]")
        name = part[:-2] if is_list else part
        if not isinstance(node, dict):
            return None
        properties = node.get("properties")
        if not isinstance(properties, dict):
            return "unchecked" if node.get("x-kubernetes-preserve-unknown-fields") else None
        node = properties.get(name)
        if is_list:
            node = node.get("items") if isinstance(node, dict) else None
        if node is None:
            return None
    return node


def check_crd(cli, key, args, emit, clock=None):
    spec = CRDS[key]
    argv = cli.kubectl_argv("get", "crd", spec["name"], "-o", "json")
    outcome = cli.run(argv)
    crd = load_json(outcome)
    if not isinstance(crd, dict):
        return failed_call(outcome, f"crd {key}", emit)
    problems = record(args, emit, "crd", crd, argv, outcome.returncode, clock) if args.crd == key else 0
    versions = (crd.get("spec") or {}).get("versions") or []
    served = {v.get("name"): v for v in versions if isinstance(v, dict) and v.get("served")}
    emit(f"crd {key}: version {spec['version']} served {'yes' if spec['version'] in served else 'no'}, {len(served)} versions served")
    if spec["version"] not in served:
        emit(f"FAIL crd {key}: the API version the Terraform writes is not served")
        return problems + 1
    schema = ((served[spec["version"]].get("schema") or {}).get("openAPIV3Schema")) or {}
    for path in spec["required"]:
        found = lookup(schema, path)
        word = "unchecked" if found == "unchecked" else "present" if found is not None else "absent"
        emit(f"field {key} {path}: {word}")
        if found is None:
            problems += 1
    for path in spec["info"]:
        emit(f"info {key} {path}: {'present' if lookup(schema, path) is not None else 'absent'}")
    for path, members in spec["enums"].items():
        node = lookup(schema, path)
        allowed = node.get("enum") if isinstance(node, dict) else None
        for member in members:
            if allowed is None:
                emit(f"enum {key} {path} {member}: not listed by the schema")
            elif member in allowed:
                emit(f"enum {key} {path} {member}: allowed")
            else:
                emit(f"FAIL enum {key} {path} {member}: not allowed")
                problems += 1
    return problems


def cmd_crds(cli, args, emit, clock=None):
    problems = sum(check_crd(cli, key, args, emit, clock) for key in CRDS)
    emit("crds: the Terraform's field names are installed" if not problems else f"FAIL crds: {problems} problems")
    return problems


# --- identities -----------------------------------------------------------------------------------------------------------


def read_verdict(outcome):
    if outcome.timed_out:
        return "timeout", 0
    text = outcome.stderr.lower()
    if outcome.returncode == 0:
        return "ok", len([line for line in outcome.stdout.splitlines() if line.strip()])
    if "forbidden" in text:
        return "forbidden", 0
    if "doesn't have a resource type" in text or "not found" in text or "no matches for kind" in text:
        return "missing", 0
    return "error", 0


def cmd_reads(cli, args, emit, clock=None):
    """Read every kind the plan identity reads, with the kubeconfig of that identity: all must be `ok`."""
    problems = 0
    for resource, namespace in PLAN_READS:
        tail = ["get", resource, "-o", "name", f"--request-timeout={min(cli.timeout, 30)}s"]
        tail += ["-n", namespace] if namespace else []
        word, count = read_verdict(cli.kubectl(*tail))
        if word == "missing" and args.allow_missing:
            word = "missing (allowed)"
        emit(f"read {resource}: {word}" + (f", {count} items" if word == "ok" else ""))
        if word not in ("ok", "missing (allowed)"):
            problems += 1
    emit("reads: the identity can read every kind" if not problems else f"FAIL reads: {problems} kinds cannot be read")
    return problems


def cmd_can_i(cli, args, emit, clock=None):
    """`kubectl auth can-i` as an identity: the answers must be exactly the expected ones."""
    expected = LAUNCHER_CAN if args.profile == "launcher" else WORKER_CAN
    user = args.user or (LAUNCHER_USER if args.profile == "launcher" else WORKER_USER)
    problems = 0
    for verb, resource, namespace, want in expected:
        tail = ["auth", "can-i", verb, resource] + (["-n", namespace] if namespace else [])
        outcome = cli.kubectl(*tail, user=user)
        answer = outcome.stdout.strip().lower().split("\n")[0] if outcome.returncode in (0, 1) and not outcome.timed_out else "error"
        answer = answer if answer in ("yes", "no") else "error"
        where = namespace or "the cluster"
        ok = answer == want
        emit(f"{'can-i' if ok else 'FAIL can-i'} {verb} {resource} in {where}: {answer} (expected {want})")
        problems += 0 if ok else 1
    emit("can-i: every answer is as expected" if not problems else f"FAIL can-i: {problems} answers differ")
    return problems


# --- one repository per node ----------------------------------------------------------------------------------------------


def colocation(pods):
    """(pods on a node, nodes, nodes with more than one repository, repositories) of a pod list; fail-closed on a missing label."""
    by_node, repos, unlabelled = {}, set(), 0
    pods = [pod for pod in pods if (pod.get("status") or {}).get("phase") not in ("Succeeded", "Failed")]
    for pod in pods:
        node = (pod.get("spec") or {}).get("nodeName")
        if not node:
            continue
        repo = ((pod.get("metadata") or {}).get("labels") or {}).get(LABEL_REPO)
        if not repo:
            unlabelled += 1
            continue
        by_node.setdefault(node, set()).add(repo)
        repos.add(repo)
    mixed = sum(1 for held in by_node.values() if len(held) > 1)
    return sum(1 for pod in pods if (pod.get("spec") or {}).get("nodeName")), len(by_node), mixed, len(repos), unlabelled


def cmd_colocation(cli, args, emit, clock=None):
    outcome = cli.kubectl("-n", WORKER_NS, "get", "pods", "-o", "json")
    document = load_json(outcome)
    if not isinstance(document, dict) or not isinstance(document.get("items"), list):
        return failed_call(outcome, "colocation", emit)
    scheduled, nodes, mixed, repos, unlabelled = colocation(document["items"])
    emit(f"pods: {scheduled} scheduled on {nodes} nodes, {repos} repositories, {unlabelled} without a repository label")
    problems = 0
    if unlabelled:
        emit("FAIL colocation: a pod has no repository label")
        problems += 1
    if mixed:
        emit(f"FAIL colocation: {mixed} nodes hold the pods of more than one repository")
        problems += 1
    if repos < 2:
        emit("FAIL colocation: fewer than two repositories were running, which proves nothing; start sessions of two repositories at once")
        problems += 1
    if not problems:
        emit("colocation: no node holds the pods of two repositories")
    return problems


# --- recordings -------------------------------------------------------------------------------------------------------------


def capture_argv(cli, args):
    """(the argv of a capture, or None and the reason)."""
    namespace = args.namespace
    if not NAME.fullmatch(namespace):
        return None, "the namespace"
    if args.kind in ("pod", "pod-events"):
        if not args.name or not (POD_NAME.fullmatch(args.name) or namespace != WORKER_NS and NAME.fullmatch(args.name)):
            return None, "the pod name"
        if args.kind == "pod":
            return cli.kubectl_argv("-n", namespace, "get", "pod", args.name, "-o", "json"), None
        selector = f"involvedObject.name={args.name}"
        return cli.kubectl_argv("-n", namespace, "get", "events", "--field-selector", selector, "-o", "json"), None
    if args.kind == "nodeclaim":
        return cli.kubectl_argv("get", "nodeclaims.karpenter.sh", "-o", "json"), None
    if args.kind == "nodepool":
        return cli.kubectl_argv("get", "nodepools.karpenter.sh", pc.NODEPOOL_NAME, "-o", "json"), None
    if args.kind == "nodes":
        return cli.kubectl_argv("get", "nodes", "-o", "json"), None
    if args.kind == "crd":
        if args.name not in {spec["name"] for spec in CRDS.values()}:
            return None, "the definition name"
        return cli.kubectl_argv("get", "crd", args.name, "-o", "json"), None
    if not (AZURE_NAME.fullmatch(args.resource_group or "") and AZURE_NAME.fullmatch(args.cluster or "")):
        return None, "the resource group or the cluster name"
    return [cli.az_binary, "aks", "show", "-g", args.resource_group, "-n", args.cluster, "--query", AKS_QUERY, "-o", "json"], None


def cmd_capture(cli, args, emit, clock=None):
    argv, why = capture_argv(cli, args)
    if argv is None:
        raise Usage(f"{why} is not acceptable")
    outcome = cli.run(argv)
    document = load_json(outcome)
    if document is None:
        return failed_call(outcome, f"capture {args.kind}", emit)
    return record(args, emit, args.kind, document, argv, outcome.returncode, clock)


def load_policy_matrix():
    import importlib.util

    spec = importlib.util.spec_from_file_location("policy_matrix_for_live_checks", Path(__file__).resolve().parent / "policy_matrix.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def cmd_denial_sample(cli, args, emit, clock=None):
    """A server-side dry run of a pod that breaks several rules, as the launcher identity: the real denial text, recorded."""
    matrix = load_policy_matrix()
    golden = json.loads((GOLDEN / matrix.GOLDEN_POD).read_text(encoding="utf-8"))
    kubectl = matrix.Kubectl(cli.kubectl_binary, args.user or LAUNCHER_USER, cli.timeout, cli.kubeconfig, cli.runner)
    outcome = kubectl.dry_run_create(matrix.probe_pod(golden))
    result = matrix.classify(outcome)
    if result.kind != "policy":
        emit(f"FAIL denial-sample: the probe pod was not denied by a policy ({result.kind})")
        return 1
    # The recording keeps the line as the server wrote it (quotes and brackets are the shape the fixture is for); the scrubber of the
    # recording, not the display filter, is what protects it.
    text = next((line for line in outcome.stderr.splitlines() if "denied request:" in line), "")[:400]
    emit(f"denial: policy {rec.sanitise(result.policy, 60)}, {len(result.rules)} rule ids named")
    problems = 0 if result.rules else 1
    if problems:
        emit("FAIL denial-sample: the message names no bracketed rule id")
    return problems + record(args, emit, "denial", text, ["kubectl", "create", "--dry-run=server"], outcome.returncode, clock)


# The scopes of `kubectl explain` that the pod policy writes deny-lists for, with the golden-object path of the same fields and the
# way the policy names a field of that scope: `has(object.spec.X)`, `has(c.X)` and `has(c.securityContext.X)`.
POD_SCOPES = (
    ("pod.spec", ("spec",), re.compile(r"has\(object\.spec\.([A-Za-z]+)\)")),
    ("pod.spec.containers", ("spec", "containers"), re.compile(r"has\(c\.([A-Za-z]+)\)")),
    ("pod.spec.containers.securityContext", ("spec", "containers", "securityContext"), re.compile(r"has\(c\.securityContext\.([A-Za-z]+)\)")),
)
EXPLAIN_FIELD = re.compile(r"^\s{2,4}([A-Za-z][A-Za-z0-9]*)\s+<", re.MULTILINE)


def explained_fields(text):
    """The field names a `kubectl explain` answer lists."""
    return set(EXPLAIN_FIELD.findall(text))


def golden_fields(path):
    """The keys the golden pods use at a scope (the spec, every container, every container's securityContext)."""
    found = set()
    for golden in sorted(GOLDEN.glob("pod_*.json")):
        nodes = [json.loads(golden.read_text(encoding="utf-8"))]
        for part in path:
            following = []
            for node in nodes:
                value = node.get(part) if isinstance(node, dict) else None
                following.extend(value if isinstance(value, list) else [value])
            nodes = following
        for node in nodes:
            if isinstance(node, dict):
                found.update(node)
    return found


def policy_fields(pattern):
    """The names the pod policy's expressions mention at a scope."""
    document = json.loads((ROOT / "infra" / "policy" / "pod-policy.json").read_text(encoding="utf-8"))
    names = set()
    for validation in document["spec"]["validations"]:
        names.update(pattern.findall(validation["expression"]))
    return names


def cmd_pod_fields(cli, args, emit, clock=None):
    """Fields of the cluster's pod API that nothing names: a field added after the policy was written is not refused (docs/pod-backend.md)."""
    problems = 0
    for scope, path, pattern in POD_SCOPES:
        outcome = cli.kubectl("explain", scope)
        server = explained_fields(outcome.stdout) if outcome.returncode == 0 else set()
        if not server:
            emit(f"FAIL pod-fields {scope}: no field could be read from the answer of `kubectl explain`")
            problems += 1
            continue
        named = policy_fields(pattern) | golden_fields(path)
        unnamed = sorted(server - named)
        emit(f"pod-fields {scope}: {len(server)} fields in the cluster, {len(server & named)} named by the policy or a golden pod, {len(unnamed)} unnamed")
        for name in unnamed:
            emit(f"FAIL unnamed field {scope}: {rec.sanitise(name, 60)}")
        problems += len(unnamed)
    emit("pod-fields: every field is named" if not problems else f"FAIL pod-fields: {problems} problems")
    return problems


def cmd_golden_image(cli, args, emit, clock=None):
    matrix = load_policy_matrix()
    golden = json.loads((GOLDEN / matrix.GOLDEN_POD).read_text(encoding="utf-8"))
    emit(golden["spec"]["containers"][0]["image"])
    return 0


# --- the command line ---------------------------------------------------------------------------------------------------------


def parse_arguments(argv):
    parser = argparse.ArgumentParser(description="Read-only checks of the live runbook.")
    parser.add_argument("--kubectl", default="kubectl")
    parser.add_argument("--az", default="az")
    parser.add_argument("--kubeconfig", default=None, help="required by every command that calls kubectl")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--record", default=None, help="a directory that exists: write the observation there as a recording")
    parser.add_argument("--step", default=None, help="the runbook step the recording belongs to (D5)")
    sub = parser.add_subparsers(dest="command", required=True)
    quota = sub.add_parser("quota")
    quota.add_argument("--location", required=True)
    quota.add_argument("--family", default=None)
    quota.add_argument("--worker-limit-cpu", type=int, default=0)
    quota.add_argument("--new-vcpu", type=int, default=0)
    quota.add_argument("--surge-vcpu", type=int, default=0)
    quota.add_argument("--family-new-vcpu", type=int, default=0)
    sub.add_parser("nodes")
    requests = sub.add_parser("node-requests")
    requests.add_argument("--min-headroom-mcpu", type=int, default=100)
    crds = sub.add_parser("crds")
    crds.add_argument("--crd", choices=sorted(CRDS), default=None, help="also record this definition")
    reads = sub.add_parser("reads")
    reads.add_argument("--profile", choices=("plan",), required=True)
    reads.add_argument("--allow-missing", action="store_true", help="a kind the cluster does not have yet is not a problem")
    can = sub.add_parser("can-i")
    can.add_argument("--profile", choices=("launcher", "worker"), required=True)
    can.add_argument("--as", dest="user", default=None)
    sub.add_parser("colocation")
    capture = sub.add_parser("capture")
    capture.add_argument("--kind", choices=CAPTURE_KINDS, required=True)
    capture.add_argument("--name", default=None)
    capture.add_argument("--namespace", default=WORKER_NS)
    capture.add_argument("--resource-group", default=None)
    capture.add_argument("--cluster", default=None)
    denial = sub.add_parser("denial-sample")
    denial.add_argument("--as", dest="user", default=None)
    sub.add_parser("pod-fields")
    sub.add_parser("golden-image")
    return parser.parse_args(argv)


COMMANDS = {
    "quota": cmd_quota,
    "nodes": cmd_nodes,
    "node-requests": cmd_node_requests,
    "crds": cmd_crds,
    "reads": cmd_reads,
    "can-i": cmd_can_i,
    "colocation": cmd_colocation,
    "capture": cmd_capture,
    "denial-sample": cmd_denial_sample,
    "pod-fields": cmd_pod_fields,
    "golden-image": cmd_golden_image,
}


def uses_kubectl(args):
    """Whether the command calls `kubectl` (everything but the two that call only `az` or nothing)."""
    if args.command in ("quota", "golden-image"):
        return False
    return not (args.command == "capture" and args.kind == "az-aks")


def main(argv=None, runner=subprocess.run, emit=print, clock=None):
    args = parse_arguments(sys.argv[1:] if argv is None else argv)
    if args.timeout < 1 or args.timeout > 600:
        emit("the timeout must be between 1 and 600 seconds")
        return 2
    if args.record is not None and (not args.step or not rec.STEP.fullmatch(args.step) or not Path(args.record).is_dir()):
        emit("--record needs an existing directory and --step, a part letter A to G and a number")
        return 2
    if args.command == "quota" and not (LOCATION.fullmatch(args.location) and (args.family is None or FAMILY.fullmatch(args.family))):
        emit("the location or the family is not acceptable")
        return 2
    if args.command == "quota" and min(args.worker_limit_cpu, args.new_vcpu, args.surge_vcpu, args.family_new_vcpu) < 0:
        emit("a vCPU number cannot be negative")
        return 2
    if args.command in ("can-i", "denial-sample") and args.user is not None and not USER.fullmatch(args.user):
        emit("the identity is not acceptable")
        return 2
    if args.command in ("capture", "denial-sample") and not args.record:
        emit(f"{args.command} needs --record DIR and --step STEP")
        return 2
    if args.kubeconfig is None and uses_kubectl(args):
        # A call without a file would address whatever the default context is, which may be production (the runbook, A4).
        emit(f"{args.command}: this command calls kubectl and needs --kubeconfig FILE (a call with no file would use the default cluster)")
        return 2
    cli = Cli(args.kubectl, args.az, args.kubeconfig, args.timeout, runner)
    try:
        problems = COMMANDS[args.command](cli, args, emit, clock)
    except Usage as refusal:
        emit(f"{args.command}: {refusal}")
        return 2
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Replay the worker admission matrix against a real API server (slice 15b of the pod backend, docs/pod-backend.md).

The ValidatingAdmissionPolicy objects of ``infra/policy/*.json`` are the hard prerequisite of the pod backend: they pin
everything a launcher may say in a worker pod and in its per-session Secret. ``src/factory/podspec.py`` is a Python MIRROR
of those rules; ``tests/test_policy_cel.py`` runs the CEL through an offline model. Neither is evidence that a real API server
enforces the CEL. This tool is: it submits the golden pods and Secrets of ``tests/golden`` (all must be ADMITTED) and every
mutation of ``policy_mutations.json`` and ``policy_secret_mutations.json`` as the launcher identity with
``kubectl create --dry-run=server`` and classifies the answer:

* the golden objects must be admitted;
* a mutation must be DENIED BY THE POLICY (the message of the API server names the policy), with the id of a rule in the
  message (``[rule:<id>]``). The server reports only the first failing validation, so the id must be the mutation's own rule,
  a rule the mirror also breaks for that object, or one named in SERVER_VIEW (what the server shows the policy after it has
  decoded the object: ``stringData`` is merged into ``data``, the owner reference is the only source of a pod name);
* a mutation the API server turns back into the golden object (it defaults the field, or drops it) is "no effect", not an
  admitted mutation: the object the dry run returns is compared with the one the golden returns;
* a mutation rejected before the policy ran (decoding, schema validation or another admission plugin) is "not exercised".
  It is not a hole, but it proves nothing about the CEL, so more than MAX_NOT_EXERCISED_PERCENT percent of the mutations in
  that class fails the run;
* anything else fails the run: an admitted mutation, a denied golden object, a denial that is not the policy's, a denial
  that names no rule, a timeout.

Safe by construction: argv lists only (no shell), every call is bounded by a timeout, the objects it submits hold the
placeholder values of the golden files, and it prints only fixed text, counts, ids from the repository's own files and, for a
rejection before the policy, a sanitised first line of the server's reason (never for a Secret). It never prints an
environment, a token, a manifest or a response.

    PYTHONPATH=src python3 tools/policy_matrix.py fixtures | kubectl apply -f -
    kubectl apply -f infra/policy/pod-policy.json -f infra/policy/pod-binding.json ...   (the four files)
    PYTHONPATH=src python3 tools/policy_matrix.py status
    PYTHONPATH=src python3 tools/policy_matrix.py run

Exit status: 0 when the matrix passed, 1 when it found a problem, 2 for a usage or environment error. Standard library only
(and ``factory.podspec`` and ``factory.podcontract`` from ``src``, hence PYTHONPATH=src).
"""

import argparse
import copy
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from factory import podcontract as pc
from factory import podspec as ps

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "golden"
POLICY_DIR = ROOT / "infra" / "policy"
POD_POLICY = "factory-worker-pods"
SECRET_POLICY = "factory-worker-secrets"
DEFAULT_USER = f"system:serviceaccount:{pc.MANAGER_NAMESPACE}:factory"
GOLDEN_POD = "pod_standard_build.json"

DENIAL = re.compile(r"ValidatingAdmissionPolicy '([A-Za-z0-9.-]+)' with binding '[A-Za-z0-9.-]+' denied request: (.*)")
RULE_TOKEN = re.compile(r"\[rule:([a-z]+(?:-[a-z]+)*)\]")
UNSAFE = re.compile(r"[^A-Za-z0-9 ._:'/,()\[\]=-]")

MAX_NOT_EXERCISED_PERCENT = 30
ATTEMPTS = 30
PAUSE_SECONDS = 2
DETAIL_LIMIT = 160

# What the API server shows the policy, as opposed to what the mirror judges: the rules that deny a mutation there.
SERVER_VIEW = {
    # stringData is merged into data before admission, so the policy sees one extra key.
    "secret-string-data-added": ("secret-keys",),
    # The policy has no pod to compare with: a Secret named after another pod is judged against its owner reference, so the
    # owner rule sees the mismatch (the mirror gets the pod name as an argument).
    "secret-other-name": ("secret-owner",),
}


@dataclass(frozen=True)
class Outcome:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


@dataclass(frozen=True)
class Result:
    kind: str  # admitted, policy, other, timeout, unreadable
    document: object = None
    policy: str = ""
    rules: tuple = ()
    detail: str = ""


class Kubectl:
    """One kubectl, as one identity, with every call bounded by a timeout. `runner` is subprocess.run (a test passes a fake)."""

    def __init__(self, binary, user, timeout, kubeconfig=None, runner=subprocess.run):
        self.binary = binary
        self.user = user
        self.timeout = timeout
        self.kubeconfig = kubeconfig
        self.runner = runner

    def argv(self, *tail):
        base = [self.binary]
        if self.kubeconfig:
            base.extend(["--kubeconfig", self.kubeconfig])
        if self.user:
            base.extend(["--as", self.user])
        base.extend(tail)
        return base

    def run(self, tail, payload=None):
        try:
            done = self.runner(
                self.argv(*tail), input=payload, capture_output=True, text=True, timeout=self.timeout, check=False
            )
        except subprocess.TimeoutExpired:
            return Outcome(124, timed_out=True)
        except OSError:
            return Outcome(127, stderr="kubectl could not be run")
        return Outcome(done.returncode, done.stdout or "", done.stderr or "")

    def dry_run_create(self, manifest):
        return self.run(["create", "--dry-run=server", "-f", "-", "-o", "json"], json.dumps(manifest))

    def get_json(self, kind, name):
        return self.run(["get", kind, name, "-o", "json"])


def sanitise(text):
    first = text.strip().splitlines()[0] if text.strip() else ""
    return UNSAFE.sub("?", first)[:DETAIL_LIMIT]


def classify(outcome, secret=False):
    """The class of one dry-run answer. The detail of a rejection is withheld for a Secret."""
    if outcome.timed_out:
        return Result("timeout")
    if outcome.returncode == 0:
        try:
            return Result("admitted", document=json.loads(outcome.stdout))
        except ValueError:
            return Result("unreadable")
    found = DENIAL.search(outcome.stderr)
    if found:
        return Result("policy", policy=found.group(1), rules=tuple(RULE_TOKEN.findall(found.group(2))))
    return Result("other", detail="details withheld" if secret else sanitise(outcome.stderr))


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def comparable(document):
    """What of a returned object tells one object from another: not the uid, the timestamps or the managed fields."""
    if not isinstance(document, dict):
        return None
    meta = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
    keep = {key: meta.get(key) for key in ("name", "namespace", "labels", "annotations", "ownerReferences")}
    return json.dumps(
        {"metadata": keep, "spec": document.get("spec"), "type": document.get("type"), "data": document.get("data")},
        sort_keys=True,
    )


class Matrix:
    """The state of one run: the counts, the failures and the lines it printed."""

    def __init__(self, kubectl, emit=print, sleep=time.sleep):
        self.kubectl = kubectl
        self.emit = emit
        self.sleep = sleep
        self.failures = 0
        self.counts = {"denied": 0, "no effect": 0, "not exercised": 0, "admitted golden": 0}

    def fail(self, text):
        self.failures += 1
        self.emit(f"FAIL {text}")

    def wait_enforced(self, golden):
        """Wait until the pod policy denies a probe pod: the policy is compiled and bound asynchronously."""
        probe = probe_pod(golden)
        for _ in range(ATTEMPTS):
            if classify(self.kubectl.dry_run_create(probe)).kind == "policy":
                return True
            self.sleep(PAUSE_SECONDS)
        return False

    def golden(self, path):
        document = load(path)
        secret = document.get("kind") == "Secret"
        result = classify(self.kubectl.dry_run_create(document), secret)
        if result.kind != "admitted":
            self.fail(f"golden {path.name} was not admitted ({result.kind}, {result.policy}, {','.join(result.rules)})")
            return None
        self.counts["admitted golden"] += 1
        self.emit(f"golden {path.name}: admitted")
        return comparable(result.document)

    def mutation(self, mutation, patched, baseline, expected, secret):
        result = classify(self.kubectl.dry_run_create(patched), secret)
        name = mutation["id"]
        if result.kind == "admitted":
            self.admitted(name, result, baseline)
        elif result.kind == "policy":
            self.denied(name, result, expected)
        elif result.kind == "other":
            self.counts["not exercised"] += 1
            self.emit(f"mutation {name}: not exercised (rejected before the policy: {result.detail})")
        else:
            self.fail(f"mutation {name}: {result.kind}")

    def admitted(self, name, result, baseline):
        if comparable(result.document) == baseline:
            self.counts["no effect"] += 1
            self.emit(f"mutation {name}: no effect (the server turned it back into the golden object)")
        else:
            self.fail(f"mutation {name} was ADMITTED")

    def denied(self, name, result, expected):
        if result.policy not in (POD_POLICY, SECRET_POLICY):
            self.fail(f"mutation {name}: denied by another policy")
        elif not result.rules:
            self.fail(f"mutation {name}: denied by the policy without a rule id")
        elif not set(result.rules) & expected:
            self.fail(f"mutation {name}: denied naming {','.join(result.rules)}, expected one of {','.join(sorted(expected))}")
        else:
            self.counts["denied"] += 1
            self.emit(f"mutation {name}: denied by {','.join(result.rules)}")


def probe_pod(golden):
    """The golden pod with several unrelated violations, so that dropping one rule does not turn the probe into an admission."""
    probe = copy.deepcopy(golden)
    # Violations the API server's own validation lets through: with `hostUsers: false` it rejects hostNetwork, hostPID and
    # hostIPC itself ("Forbidden: when hostUsers is false"), which would never reach the policy being waited for.
    probe["spec"].update({"restartPolicy": "Always", "automountServiceAccountToken": True, "shareProcessNamespace": True})
    probe["spec"]["containers"][0]["image"] = "registry.example.invalid/factory:latest"
    return probe


def expected_rules(mutation, patched, image, pod_name):
    """The rule ids that may be named for a mutation; `pod_name` is None for a pod and the pod's name for a Secret."""
    named = {mutation["rule"], *SERVER_VIEW.get(mutation["id"], ())}
    if pod_name is None:
        return named | set(ps.check_policy(patched, image))
    return named | set(ps.check_secret_policy(patched, pod_name))


def run_matrix(kubectl, emit=print, sleep=time.sleep):
    """Run the whole matrix; the number of problems found (0 is a pass)."""
    matrix = Matrix(kubectl, emit, sleep)
    pod_file = load(GOLDEN / "policy_mutations.json")
    secret_file = load(GOLDEN / "policy_secret_mutations.json")
    golden = load(GOLDEN / pod_file["golden"])
    image = golden["spec"]["containers"][0]["image"]
    if not matrix.wait_enforced(golden):
        matrix.fail("the pod policy did not start to deny within the allowed time")
        return summarise(matrix, 0)
    baselines = {path.name: matrix.golden(path) for path in sorted(GOLDEN.glob("pod_*.json"))}
    baselines.update({path.name: matrix.golden(path) for path in sorted(GOLDEN.glob("secret_*.json"))})
    pod_baseline = baselines[pod_file["golden"]]
    secret_golden = load(GOLDEN / secret_file["golden"])
    for mutation in pod_file["mutations"]:
        patched = ps.apply_patch(golden, mutation["patch"])
        matrix.mutation(mutation, patched, pod_baseline, expected_rules(mutation, patched, image, None), False)
    for mutation in secret_file["mutations"]:
        patched = ps.apply_patch(secret_golden, mutation["patch"])
        expected = expected_rules(mutation, patched, image, secret_file["pod_name"])
        matrix.mutation(mutation, patched, baselines[secret_file["golden"]], expected, True)
    return summarise(matrix, len(pod_file["mutations"]) + len(secret_file["mutations"]))


def summarise(matrix, total):
    counts = matrix.counts
    matrix.emit(
        f"matrix: {counts['admitted golden']} golden admitted, {counts['denied']} denied by a rule, "
        f"{counts['no effect']} no effect, {counts['not exercised']} not exercised, {matrix.failures} problems, {total} mutations"
    )
    if total and counts["not exercised"] * 100 > MAX_NOT_EXERCISED_PERCENT * total:
        matrix.fail(f"more than {MAX_NOT_EXERCISED_PERCENT} percent of the mutations never reached the policy")
    return matrix.failures


def fixtures(image):
    """The objects the policies need around them on a cluster that has nothing else: namespace, service account, parameter, RBAC."""
    ref = load(POLICY_DIR / "pod-binding.json")["spec"]["paramRef"]
    namespace = pc.WORKER_NAMESPACE
    role = {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "matrix-launcher"}
    return {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace, "labels": {"pod-security.kubernetes.io/enforce": "privileged"}}},
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": {"name": pc.WORKER_SERVICE_ACCOUNT, "namespace": namespace},
                "automountServiceAccountToken": False,
            },
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": ref["name"], "namespace": ref["namespace"]}, "data": {"image": image}},
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "Role",
                "metadata": {"name": role["name"], "namespace": namespace},
                "rules": [{"apiGroups": [""], "resources": ["pods", "secrets"], "verbs": ["create"]}],
            },
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "RoleBinding",
                "metadata": {"name": role["name"], "namespace": namespace},
                "roleRef": role,
                "subjects": [{"kind": "ServiceAccount", "name": "factory", "namespace": pc.MANAGER_NAMESPACE}],
            },
        ],
    }


def observed(kubectl, name, sleep):
    for _ in range(ATTEMPTS):
        outcome = kubectl.get_json("validatingadmissionpolicy", name)
        document = json.loads(outcome.stdout) if outcome.returncode == 0 and outcome.stdout.strip() else None
        status = document.get("status") if isinstance(document, dict) else None
        generation = document.get("metadata", {}).get("generation") if isinstance(document, dict) else None
        if isinstance(status, dict) and status.get("observedGeneration") == generation:
            return document
        sleep(PAUSE_SECONDS)
    return None


# What the API server's type checker (v1.36, measured on kind) reports for three rules although the real objects behave: it cannot
# resolve a field whose type is a Quantity or a ResourceList (`has(v.emptyDir.sizeLimit)`, `has(c.resources.requests)`,
# `has(object.spec.overhead)`; the `resources` rule names both `requests` and `limits` many times). The matrix replay proves those rules at run time (every golden pod is admitted and each mutation of
# the volumes, resources and spec-fields rules is denied naming its rule), so exactly these three are reported and tolerated; any
# other warning, or the same message for another field, still fails.
TOLERATED_TYPE_CHECK = re.compile(r"(?:/v1, Kind=Pod: )?ERROR: <input>:\d+:\d+: undefined field '(?:sizeLimit|requests|limits|overhead)'")
_ERROR_LINE = re.compile(r"(?:/v1, Kind=Pod: )?ERROR: <input>:")


def tolerated(text):
    """True when the warning holds at least one ERROR line and EVERY ERROR line of it is one of the three known limits.

    cel-go reports each error of an expression on its own line (the source line and the caret follow it), so judging the first
    line only would let a second, real error of the same expression pass.
    """
    errors = [line for line in text.splitlines() if _ERROR_LINE.match(line)]
    return bool(errors) and all(TOLERATED_TYPE_CHECK.fullmatch(line) for line in errors)


def check_status(kubectl, emit=print, sleep=time.sleep):
    """Wait for the API server to type-check both policies and fail on any expression warning; the number of problems."""
    problems = 0
    for name in (POD_POLICY, SECRET_POLICY):
        document = observed(kubectl, name, sleep)
        if document is None:
            emit(f"FAIL policy {name}: its status was not observed within the allowed time")
            problems += 1
            continue
        warnings = document["status"].get("typeChecking", {}).get("expressionWarnings", [])
        emit(f"policy {name}: {len(warnings)} type-check warnings")
        for warning in warnings:
            text = str(warning.get("warning", ""))
            if name == POD_POLICY and tolerated(text):
                emit(f"tolerated {sanitise(str(warning.get('fieldRef', '')))}: {sanitise(text)}")
                continue
            emit(f"FAIL {sanitise(str(warning.get('fieldRef', '')))}: {sanitise(text)}")
            problems += 1
    return problems


def parse_arguments(argv):
    parser = argparse.ArgumentParser(description="Replay the worker admission matrix against a real API server.")
    parser.add_argument("command", choices=("fixtures", "status", "run"))
    parser.add_argument("--kubectl", default="kubectl")
    parser.add_argument("--kubeconfig", default=None)
    parser.add_argument("--as", dest="user", default=DEFAULT_USER)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--image", default=None, help="the allowed image of the fixtures (default: the one of the golden pod)")
    return parser.parse_args(argv)


def main(argv=None, runner=subprocess.run, emit=print, sleep=time.sleep):
    args = parse_arguments(sys.argv[1:] if argv is None else argv)
    if args.timeout < 1:
        emit("the timeout must be at least one second")
        return 2
    if args.command == "fixtures":
        image = args.image or load(GOLDEN / GOLDEN_POD)["spec"]["containers"][0]["image"]
        emit(json.dumps(fixtures(image), indent=2))
        return 0
    # The type-check status of a policy is read as the caller (the cluster's administrator): the identity the matrix
    # impersonates may only create pods and secrets, so it could never read a policy and the wait would always time out.
    kubectl = Kubectl(args.kubectl, None if args.command == "status" else args.user, args.timeout, args.kubeconfig, runner)
    problems = check_status(kubectl, emit, sleep) if args.command == "status" else run_matrix(kubectl, emit, sleep)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

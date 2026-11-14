"""tools/live_checks.py against a fake `az` and `kubectl` (slice 21 of the pod backend, docs/pod-backend-runbook.md).

The tool's commands are the read-only checks a person runs in the live runbook. Here the "cluster" is a table of canned answers keyed
by the argv list, so what these tests prove is the TOOL: which calls it makes (argv lists, a timeout, nothing that writes), what it
decides from an answer, that a missing program, a timeout and an unreadable answer are verdicts, and that it prints no name of a
node, a pod or a secret. They do not prove any real answer has these shapes; the runbook's recordings do.
"""

import datetime
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from factory import podcontract as pc
from tests.live_tools import calls, imported_modules, keywords, load

chk = load("live_checks")
rec = load("live_recording")

SENTINEL_NODE = "aks-sentinelnode-12345678-vmss000000"
SENTINEL_POD = "factory-w-sentinelpod0001"
CLOCK = lambda: datetime.datetime(2026, 11, 12, 10, 0, 0, tzinfo=datetime.timezone.utc)  # noqa: E731
LINE = r"^(?:ok |FAIL |note|nodes: |worker node: |node \d+ \(|crd |field |info |enum |read |can-i|FAIL can-i|regional: |family: |quota: |pods: |colocation: |recorded |denial: |reads: |crds: |[a-z.]+/[a-z0-9]+ )"


TEST_KUBECONFIG = "/test/kubeconfig"


def without_test_kubeconfig(argv):
    """The argv without the kubeconfig pair `run` adds (a tool refuses a kubectl command without one): the tests that are not about it compare the rest."""
    argv = list(argv)
    for index in range(len(argv) - 1):
        if argv[index : index + 2] == ["--kubeconfig", TEST_KUBECONFIG]:
            return argv[:index] + argv[index + 2 :]
    return argv


class Fake:
    """A stand-in for subprocess.run: answers by argv (a tuple prefix match), records every call."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []

    def __call__(self, argv, input=None, capture_output=True, text=True, timeout=None, check=False, **extra):
        argv = without_test_kubeconfig(argv)
        self.calls.append({"argv": list(argv), "input": input, "timeout": timeout, "extra": extra})
        for prefix, answer in self.answers.items():
            if tuple(argv[: len(prefix)]) == prefix or all(part in argv for part in prefix):
                if isinstance(answer, Exception):
                    raise answer
                return subprocess.CompletedProcess(argv, answer[0], answer[1], answer[2] if len(answer) > 2 else "")
        return subprocess.CompletedProcess(argv, 1, "", "Error from server (NotFound): not found")


def ok(document):
    return (0, json.dumps(document))


def node(pool="worker", cpu="3860m", memory="14000000Ki", disk="100Gi", ready="True", name=SENTINEL_NODE, size="Standard_D4s_v6", boot="b1"):
    labels = {"node.kubernetes.io/instance-type": size}
    if pool == "worker":
        labels[pc.WORKER_NODE_LABEL_KEY] = pc.WORKER_NODE_LABEL_VALUE
    if pool == "system":
        labels["kubernetes.azure.com/mode"] = "system"
    return {
        "metadata": {"name": name, "labels": labels},
        "status": {"allocatable": {"cpu": cpu, "memory": memory, "ephemeral-storage": disk}, "conditions": [{"type": "Ready", "status": ready}], "nodeInfo": {"bootID": boot}},
    }


def pod(node_name=SENTINEL_NODE, repo="aaaaaaaaaaaa", cpu="100m", memory="128Mi", init=None, phase="Running", name=SENTINEL_POD):
    spec = {"nodeName": node_name, "containers": [{"resources": {"requests": {"cpu": cpu, "memory": memory}}}]}
    if init:
        spec["initContainers"] = [{"resources": {"requests": {"cpu": init[0], "memory": init[1]}}}]
    labels = {chk.LABEL_REPO: repo} if repo else {}
    return {"metadata": {"name": name, "labels": labels}, "spec": spec, "status": {"phase": phase}}


def run(argv, answers=None, **options):
    fake = Fake(answers)
    lines = []
    argv = list(argv)
    if "--kubeconfig" not in argv and not options.get("without_kubeconfig"):
        argv = ["--kubeconfig", TEST_KUBECONFIG] + argv  # every command that calls kubectl needs one: tests of the others do not care
    code = chk.main(argv, runner=fake, emit=lines.append, clock=CLOCK)
    return code, lines, fake


def usage_rows(used=12, limit=150, family=("standardDSv6Family", 0, 350)):
    rows = [{"name": {"value": "cores", "localizedValue": "Total Regional vCPUs"}, "currentValue": str(used), "limit": str(limit)}]
    if family:
        rows.append({"name": {"value": family[0], "localizedValue": "x"}, "currentValue": family[1], "limit": family[2]})
    return rows


class Quota(unittest.TestCase):
    ARGS = ("quota", "--location", "westus3", "--family", "StandardDSv6Family", "--worker-limit-cpu", "32", "--new-vcpu", "10", "--surge-vcpu", "22", "--family-new-vcpu", "0")
    CALL = ("az", "vm", "list-usage", "--location", "westus3", "-o", "json")

    def test_it_asks_az_with_an_argv_list_and_a_timeout_and_reports_the_margin(self):
        code, lines, fake = run(self.ARGS, {self.CALL: ok(usage_rows())})
        self.assertEqual(code, 0, lines)
        self.assertEqual(lines, ["regional: used 12 of 150, planned 64, margin 74", "family: used 0 of 350, planned 32, margin 318", "quota: fits"])
        self.assertEqual(fake.calls[0]["argv"], list(self.CALL))
        self.assertEqual(fake.calls[0]["timeout"], 60)

    def test_the_regional_total_binds_even_when_the_family_has_room(self):
        code, lines, _ = run(self.ARGS, {self.CALL: ok(usage_rows(used=100))})
        self.assertEqual(code, 1)
        self.assertIn("regional: used 100 of 150, planned 64, margin -14", lines)
        self.assertTrue(lines[-1].startswith("FAIL quota: does not fit"))

    def test_the_family_binds_when_the_region_has_room(self):
        code, lines, _ = run(self.ARGS, {self.CALL: ok(usage_rows(family=("standardDSv6Family", 340, 350)))})
        self.assertEqual(code, 1)
        self.assertIn("family: used 340 of 350, planned 32, margin -22", lines)

    def test_an_exact_fit_passes_and_one_over_fails(self):
        exact = run(self.ARGS, {self.CALL: ok(usage_rows(used=86))})
        over = run(self.ARGS, {self.CALL: ok(usage_rows(used=87))})
        self.assertEqual((exact[0], over[0]), (0, 1))

    def test_a_missing_row_an_unreadable_answer_a_timeout_and_a_missing_program_are_verdicts(self):
        cases = {
            "no total": ok([{"name": {"value": "other"}, "currentValue": 1, "limit": 2}]),
            "no family": ok(usage_rows(family=None)),
            "not json": (0, "text"),
            "failed": (1, "", "boom"),
            "timeout": subprocess.TimeoutExpired("az", 60),
            "no az": FileNotFoundError(),
        }
        for name, answer in cases.items():
            code, lines, _ = run(self.ARGS, {self.CALL: answer})
            with self.subTest(name):
                self.assertEqual(code, 1, lines)
                self.assertTrue(lines[-1].startswith("FAIL quota"), lines)

    def test_the_location_and_the_family_are_checked_before_anything_is_called(self):
        for argv in (("quota", "--location", "west us"), ("quota", "--location", "westus3", "--family", "a;b"), ("quota", "--location", "westus3", "--new-vcpu", "-1")):
            code, lines, fake = run(argv)
            self.assertEqual(code, 2, argv)
            self.assertEqual(fake.calls, [])


class Nodes(unittest.TestCase):
    ANSWERS = {("kubectl", "get", "nodes"): ok({"items": [node(), node("system", name="aks-system-1"), node("other", name="aks-factory-1")]})}

    def test_counts_by_pool_and_the_allocatable_of_a_worker_node(self):
        code, lines, fake = run(("nodes",), self.ANSWERS)
        self.assertEqual(code, 0)
        self.assertEqual(
            lines,
            [
                "nodes: 3 total, 1 worker, 1 system, 1 other, 3 ready",
                "worker node: size Standard_D4s_v6, allocatable cpu 3860 mCPU, memory 13671 MiB, ephemeral-storage 100 GiB, ready yes",
            ],
        )
        self.assertEqual(fake.calls[0]["argv"], ["kubectl", "get", "nodes", "-o", "json"])

    def test_no_node_name_is_printed_and_an_odd_quantity_fails_closed(self):
        _, lines, _ = run(("nodes",), self.ANSWERS)
        self.assertNotIn(SENTINEL_NODE, "\n".join(lines))
        code, lines, _ = run(("nodes",), {("kubectl", "get", "nodes"): ok({"items": [node(cpu="lots")]})})
        self.assertEqual(code, 1)
        self.assertIn("FAIL a worker node reports an allocatable quantity that is not understood", lines)

    def test_a_node_that_is_not_ready_is_counted_as_such(self):
        _, lines, _ = run(("nodes",), {("kubectl", "get", "nodes"): ok({"items": [node(ready="False")]})})
        self.assertEqual(lines[0], "nodes: 1 total, 1 worker, 0 system, 0 other, 0 ready")

    def test_the_kubeconfig_and_the_binary_are_passed_as_arguments(self):
        _, _, fake = run(("--kubectl", "/opt/kubectl", "--kubeconfig", "/x/config", "nodes"), {("/opt/kubectl",): ok({"items": []})})
        self.assertEqual(fake.calls[0]["argv"], ["/opt/kubectl", "--kubeconfig", "/x/config", "get", "nodes", "-o", "json"])


class NodeRequests(unittest.TestCase):
    def answers(self, pods, nodes=None):
        return {
            ("kubectl", "get", "nodes"): ok({"items": nodes or [node("system", cpu="1900m", memory="4000Mi", name="s1")]}),
            ("kubectl", "get", "pods"): ok({"items": pods}),
        }

    def test_the_requests_of_a_node_are_summed_and_compared_with_its_allocatable(self):
        pods = [pod("s1", cpu="1000m", memory="1000Mi"), pod("s1", cpu="676m", memory="1130Mi"), pod("elsewhere", cpu="9")]
        code, lines, fake = run(("node-requests",), self.answers(pods))
        self.assertEqual(code, 0)
        self.assertEqual(lines[0], "node 1 (system): cpu requested 1676 of 1900 mCPU (88 percent), memory requested 2130 of 4000 MiB (53 percent)")
        selector = fake.calls[1]["argv"][fake.calls[1]["argv"].index("--field-selector") + 1]
        self.assertEqual(selector, "status.phase!=Succeeded,status.phase!=Failed")  # a finished pod holds no node capacity

    def test_the_larger_of_the_containers_and_the_biggest_init_container_counts(self):
        _, lines, _ = run(("node-requests",), self.answers([pod("s1", cpu="100m", memory="100Mi", init=("500m", "50Mi"))]))
        self.assertIn("cpu requested 500 of 1900 mCPU", lines[0])
        self.assertIn("memory requested 100 of 4000 MiB", lines[0])

    def test_a_system_node_with_little_headroom_is_a_note_not_a_failure(self):
        code, lines, _ = run(("node-requests",), self.answers([pod("s1", cpu="1850m")]))
        self.assertEqual(code, 0)
        self.assertTrue(any(line.startswith("note: the system node has less than 100 mCPU") for line in lines))
        _, quiet, _ = run(("node-requests", "--min-headroom-mcpu", "10"), self.answers([pod("s1", cpu="1850m")]))
        self.assertFalse(any(line.startswith("note") for line in quiet))

    def test_a_quantity_that_is_not_understood_fails_closed_and_no_name_is_printed(self):
        code, lines, _ = run(("node-requests",), self.answers([pod("s1", cpu="lots")]))
        self.assertEqual(code, 1)
        self.assertIn("FAIL a pod requests a quantity that is not understood", lines)
        self.assertNotIn(SENTINEL_POD, "\n".join(lines))


def schema(**overrides):
    nodepool_spec = {
        "template": {
            "properties": {
                "metadata": {"properties": {"labels": {}}},
                "spec": {
                    "properties": {
                        "expireAfter": {},
                        "nodeClassRef": {"properties": {"group": {}, "kind": {}, "name": {}}},
                        "taints": {"items": {"properties": {"key": {}, "value": {}, "effect": {}}}},
                        "startupTaints": {"items": {"properties": {"key": {}, "value": {}, "effect": {}}}},
                        "requirements": {"items": {"properties": {"key": {}, "operator": {}, "values": {}}}},
                    }
                },
            }
        },
        "disruption": {"properties": {"consolidationPolicy": {"enum": ["WhenEmpty", "WhenEmptyOrUnderutilized"]}, "consolidateAfter": {}, "budgets": {}}},
        "limits": {},
    }
    nodepool_spec.update(overrides)
    return nodepool_spec


def crd(spec_properties, version="v1", served=True):
    return {"spec": {"versions": [{"name": version, "served": served, "schema": {"openAPIV3Schema": {"properties": {"spec": {"properties": spec_properties}}}}}]}}


NODECLASS = crd({"imageFamily": {"enum": ["Ubuntu", "AzureLinux"]}, "kubelet": {"properties": {"podPidsLimit": {}}}}, "v1beta1")


class Crds(unittest.TestCase):
    def answers(self, nodepool, nodeclass=NODECLASS):
        return {("kubectl", "get", "crd", "nodepools.karpenter.sh"): ok(nodepool), ("kubectl", "get", "crd", "aksnodeclasses.karpenter.azure.com"): ok(nodeclass)}

    def test_every_field_the_terraform_writes_present_passes(self):
        code, lines, fake = run(("crds",), self.answers(crd(schema())))
        self.assertEqual(code, 0, lines)
        self.assertIn("field nodepool spec.template.spec.expireAfter: present", lines)
        self.assertIn("field nodeclass spec.kubelet.podPidsLimit: present", lines)
        self.assertIn("info nodepool spec.disruption.expireAfter: absent", lines)
        self.assertIn("enum nodeclass spec.imageFamily AzureLinux: allowed", lines)
        self.assertEqual(lines[-1], "crds: the Terraform's field names are installed")
        self.assertEqual(fake.calls[0]["argv"], ["kubectl", "get", "crd", "nodepools.karpenter.sh", "-o", "json"])

    def test_expire_after_under_disruption_instead_of_the_template_is_found_and_failed(self):
        template = schema()["template"]
        del template["properties"]["spec"]["properties"]["expireAfter"]
        disruption = dict(schema()["disruption"])
        disruption["properties"] = {**disruption["properties"], "expireAfter": {}}
        code, lines, _ = run(("crds",), self.answers(crd(schema(template=template, disruption=disruption))))
        self.assertEqual(code, 1)
        self.assertIn("field nodepool spec.template.spec.expireAfter: absent", lines)
        self.assertIn("info nodepool spec.disruption.expireAfter: present", lines)

    def test_each_missing_field_is_a_problem(self):
        for field in ("podPidsLimit",):
            klass = crd({"imageFamily": {"enum": ["AzureLinux"]}, "kubelet": {"properties": {}}}, "v1beta1")
            code, lines, _ = run(("crds",), self.answers(crd(schema()), klass))
            self.assertEqual(code, 1)
            self.assertIn("field nodeclass spec.kubelet.podPidsLimit: absent", lines)
        no_startup = schema()["template"]
        del no_startup["properties"]["spec"]["properties"]["startupTaints"]
        code, lines, _ = run(("crds",), self.answers(crd(schema(template=no_startup))))
        self.assertEqual(code, 1)
        self.assertIn("field nodepool spec.template.spec.startupTaints[].key: absent", lines)

    def test_an_unserved_api_version_and_an_unlisted_enum_value_fail(self):
        code, lines, _ = run(("crds",), self.answers(crd(schema(), served=False)))
        self.assertEqual(code, 1)
        self.assertIn("FAIL crd nodepool: the API version the Terraform writes is not served", lines)
        klass = crd({"imageFamily": {"enum": ["Ubuntu"]}, "kubelet": {"properties": {"podPidsLimit": {}}}}, "v1beta1")
        code, lines, _ = run(("crds",), self.answers(crd(schema()), klass))
        self.assertEqual(code, 1)
        self.assertIn("FAIL enum nodeclass spec.imageFamily AzureLinux: not allowed", lines)

    def test_a_free_form_object_is_reported_as_unchecked_and_is_not_a_pass_by_accident(self):
        free = crd({"template": {"x-kubernetes-preserve-unknown-fields": True}, "disruption": schema()["disruption"], "limits": {}})
        code, lines, _ = run(("crds",), self.answers(free))
        self.assertIn("field nodepool spec.template.spec.expireAfter: unchecked", lines)
        self.assertEqual(code, 0)

    def test_a_missing_definition_is_a_verdict(self):
        code, lines, _ = run(("crds",), {})
        self.assertEqual(code, 1)
        self.assertTrue(all(line.startswith("FAIL") for line in lines), lines)

    def test_the_paths_are_the_ones_the_terraform_writes(self):
        text = (chk.ROOT / "infra" / "worker_pool.tf").read_text()
        for path in chk.CRDS["nodepool"]["required"] + chk.CRDS["nodeclass"]["required"]:
            leaf = path.split(".")[-1].replace("[]", "")
            self.assertIn(leaf, text, path)
        self.assertIn('apiVersion = "karpenter.sh/v1"', text)
        self.assertIn('apiVersion = "karpenter.azure.com/v1beta1"', text)
        self.assertEqual((chk.CRDS["nodepool"]["version"], chk.CRDS["nodeclass"]["version"]), ("v1", "v1beta1"))


class Reads(unittest.TestCase):
    def test_every_kind_readable_passes_with_the_identitys_kubeconfig(self):
        answers = {("kubectl",): (0, "a/b\nc/d\n")}
        code, lines, fake = run(("--kubeconfig", "/plan/config", "reads", "--profile", "plan"), answers)
        self.assertEqual(code, 0, lines)
        self.assertEqual(len(fake.calls), len(chk.PLAN_READS))
        for call in fake.calls:
            self.assertEqual(call["argv"][:3], ["kubectl", "--kubeconfig", "/plan/config"])
            self.assertIn("-o", call["argv"])
            self.assertIn("name", call["argv"])
            self.assertTrue(any(part.startswith("--request-timeout=") for part in call["argv"]))
        self.assertIn("read validatingadmissionpolicies.admissionregistration.k8s.io: ok, 2 items", lines)

    def test_the_two_admission_kinds_are_in_the_list(self):
        names = [name for name, _ in chk.PLAN_READS]
        self.assertIn("validatingadmissionpolicies.admissionregistration.k8s.io", names)
        self.assertIn("validatingadmissionpolicybindings.admissionregistration.k8s.io", names)

    def test_a_forbidden_kind_fails_the_gate_and_names_it(self):
        answers = {
            ("validatingadmissionpolicies.admissionregistration.k8s.io",): (1, "", 'Error from server (Forbidden): validatingadmissionpolicies.admissionregistration.k8s.io is forbidden: User "x" cannot list'),
            ("kubectl",): (0, "a/b\n"),
        }
        code, lines, _ = run(("reads", "--profile", "plan"), answers)
        self.assertEqual(code, 1)
        self.assertIn("read validatingadmissionpolicies.admissionregistration.k8s.io: forbidden", lines)
        self.assertEqual(lines[-1], "FAIL reads: 1 kinds cannot be read")

    def test_a_missing_kind_fails_unless_it_is_allowed_and_a_timeout_never_passes(self):
        answers = {("nodepools.karpenter.sh",): (1, "", "error: the server doesn't have a resource type \"nodepools\""), ("kubectl",): (0, "a/b\n")}
        self.assertEqual(run(("reads", "--profile", "plan"), answers)[0], 1)
        code, lines, _ = run(("reads", "--profile", "plan", "--allow-missing"), answers)
        self.assertEqual(code, 0)
        self.assertIn("read nodepools.karpenter.sh: missing (allowed)", lines)
        code, lines, _ = run(("reads", "--profile", "plan"), {("kubectl",): subprocess.TimeoutExpired("kubectl", 1)})
        self.assertEqual(code, 1)
        self.assertEqual(sum(1 for line in lines if line.endswith(": timeout")), len(chk.PLAN_READS))


class CanI(unittest.TestCase):
    @staticmethod
    def answers(table):
        """kubectl auth can-i answers by (verb, resource): the launcher of the Role of the design unless the table says otherwise."""
        def answer(argv):
            verb, resource = argv[argv.index("can-i") + 1], argv[argv.index("can-i") + 2]
            ns = argv[argv.index("-n") + 1] if "-n" in argv else None
            expected = {(v, r, n): w for v, r, n, w in chk.LAUNCHER_CAN}
            got = table.get((verb, resource, ns), expected[(verb, resource, ns)])
            return (0, "yes\n") if got == "yes" else (1, "no\n")

        class Router(Fake):
            def __call__(self, argv, **kwargs):
                argv = without_test_kubeconfig(argv)
                self.calls.append({"argv": list(argv), "timeout": kwargs.get("timeout"), "extra": {}})
                code, out = answer(argv)
                return subprocess.CompletedProcess(argv, code, out, "")

        return Router()

    def run_with(self, router, *argv):
        lines = []
        argv = argv if "--kubeconfig" in argv else ("--kubeconfig", TEST_KUBECONFIG, *argv)
        code = chk.main(list(argv), runner=router, emit=lines.append)
        return code, lines

    def test_the_launcher_may_do_what_its_role_says_and_nothing_else(self):
        router = self.answers({})
        code, lines = self.run_with(router, "can-i", "--profile", "launcher")
        self.assertEqual(code, 0, lines)
        self.assertEqual(lines[-1], "can-i: every answer is as expected")
        for call in router.calls:
            self.assertEqual(call["argv"][:4], ["kubectl", "--as", chk.LAUNCHER_USER, "auth"])

    def test_one_permission_too_many_or_too_few_is_a_failure(self):
        for change in (("get", "secrets", pc.WORKER_NAMESPACE), ("create", "pods/exec", pc.WORKER_NAMESPACE), ("create", "pods", pc.MANAGER_NAMESPACE)):
            code, lines = self.run_with(self.answers({change: "yes"}), "can-i", "--profile", "launcher")
            with self.subTest(change):
                self.assertEqual(code, 1)
                self.assertTrue(any(line.startswith("FAIL can-i") and "(expected no)" in line for line in lines))
        code, lines = self.run_with(self.answers({("create", "pods", pc.WORKER_NAMESPACE): "no"}), "can-i", "--profile", "launcher")
        self.assertEqual(code, 1)
        self.assertTrue(any("(expected yes)" in line and line.startswith("FAIL") for line in lines))

    def test_the_launcher_list_matches_the_role_of_the_design(self):
        yes = {(v, r) for v, r, n, w in chk.LAUNCHER_CAN if w == "yes"}
        self.assertEqual(yes, {("create", "pods"), ("get", "pods"), ("list", "pods"), ("delete", "pods"), ("create", "secrets"), ("delete", "secrets")})
        no = {(v, r) for v, r, n, w in chk.LAUNCHER_CAN if w == "no"}
        for forbidden in (("get", "secrets"), ("list", "secrets"), ("create", "pods/exec"), ("get", "pods/log")):
            self.assertIn(forbidden, no)

    def test_the_worker_service_account_may_do_nothing(self):
        self.assertTrue(all(want == "no" for *_, want in chk.WORKER_CAN))
        router = Fake({("kubectl",): (1, "no\n")})
        code, lines = self.run_with(router, "can-i", "--profile", "worker")
        self.assertEqual(code, 0, lines)
        self.assertEqual(router.calls[0]["argv"][:3], ["kubectl", "--as", chk.WORKER_USER])

    def test_an_unintelligible_answer_is_an_error_not_a_yes_or_a_no(self):
        code, lines = self.run_with(Fake({("kubectl",): (0, "maybe\n")}), "can-i", "--profile", "worker")
        self.assertEqual(code, 1)
        self.assertTrue(any(": error (expected no)" in line for line in lines))

    def test_the_identity_is_checked(self):
        code, _ = self.run_with(Fake(), "can-i", "--profile", "worker", "--as", "x; rm -rf /")
        self.assertEqual(code, 2)


class Colocation(unittest.TestCase):
    def pods(self, *items):
        return {("kubectl", "-n", pc.WORKER_NAMESPACE, "get", "pods"): ok({"items": list(items)})}

    def test_two_repositories_on_two_nodes_pass(self):
        code, lines, fake = run(("colocation",), self.pods(pod("n1", "aaaaaaaaaaaa"), pod("n2", "bbbbbbbbbbbb"), pod("n1", "aaaaaaaaaaaa")))
        self.assertEqual(code, 0, lines)
        self.assertEqual(lines[0], "pods: 3 scheduled on 2 nodes, 2 repositories, 0 without a repository label")
        self.assertEqual(lines[-1], "colocation: no node holds the pods of two repositories")
        self.assertEqual(fake.calls[0]["argv"], ["kubectl", "-n", pc.WORKER_NAMESPACE, "get", "pods", "-o", "json"])

    def test_two_repositories_on_one_node_fail(self):
        code, lines, _ = run(("colocation",), self.pods(pod("n1", "aaaaaaaaaaaa"), pod("n1", "bbbbbbbbbbbb")))
        self.assertEqual(code, 1)
        self.assertIn("FAIL colocation: 1 nodes hold the pods of more than one repository", lines)

    def test_it_proves_nothing_with_one_repository_and_says_so(self):
        code, lines, _ = run(("colocation",), self.pods(pod("n1"), pod("n2")))
        self.assertEqual(code, 1)
        self.assertTrue(any("fewer than two repositories" in line for line in lines))

    def test_a_pod_without_the_label_fails_closed_and_a_finished_pod_does_not_count(self):
        code, lines, _ = run(("colocation",), self.pods(pod("n1", repo=None), pod("n1", "a"), pod("n2", "b")))
        self.assertEqual(code, 1)
        self.assertIn("FAIL colocation: a pod has no repository label", lines)
        code, lines, _ = run(("colocation",), self.pods(pod("n1", "a"), pod("n1", "b", phase="Succeeded"), pod("n2", "b")))
        self.assertEqual(code, 0, lines)

    def test_a_pending_pod_has_no_node_and_is_not_counted(self):
        self.assertEqual(chk.colocation([pod(node_name=None, repo="a"), pod("n1", "a")])[:3], (1, 1, 0))

    def test_no_pod_name_is_printed(self):
        _, lines, _ = run(("colocation",), self.pods(pod("n1", "aaaaaaaaaaaa")))
        self.assertNotIn(SENTINEL_POD, "\n".join(lines))


class Capture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.options = ("--record", self.directory.name, "--step", "D5")

    def written(self):
        return [json.loads(path.read_text()) for path in sorted(Path(self.directory.name).iterdir())]

    def test_a_pod_is_captured_with_a_fixed_argv_and_recorded_scrubbed(self):
        body = {"kind": "Pod", "metadata": {"name": SENTINEL_POD}, "status": {"phase": "Pending", "hostIP": "10.0.0.4"}}
        code, lines, fake = run((*self.options, "capture", "--kind", "pod", "--name", SENTINEL_POD), {("kubectl",): ok(body)})
        self.assertEqual(code, 0, lines)
        self.assertEqual(fake.calls[0]["argv"], ["kubectl", "-n", pc.WORKER_NAMESPACE, "get", "pod", SENTINEL_POD, "-o", "json"])
        (document,) = self.written()
        self.assertEqual((document["step"], document["kind"], without_test_kubeconfig(document["argv"]), document["returncode"]), ("D5", "pod", fake.calls[0]["argv"], 0))
        self.assertEqual(document["argv"][1:3], ["--kubeconfig", TEST_KUBECONFIG])  # the recording says which cluster it came from
        self.assertEqual(document["payload"]["status"]["hostIP"], "0.0.0.0")
        self.assertEqual(lines, ["recorded D5 pod: 1 redactions"])
        self.assertEqual(rec.problems(document), [])

    def test_the_other_kinds_use_fixed_calls(self):
        wanted = {
            "pod-events": ["kubectl", "-n", pc.WORKER_NAMESPACE, "get", "events", "--field-selector", f"involvedObject.name={SENTINEL_POD}", "-o", "json"],
            "nodeclaim": ["kubectl", "get", "nodeclaims.karpenter.sh", "-o", "json"],
            "nodepool": ["kubectl", "get", "nodepools.karpenter.sh", pc.NODEPOOL_NAME, "-o", "json"],
            "nodes": ["kubectl", "get", "nodes", "-o", "json"],
        }
        for kind, argv in wanted.items():
            code, lines, fake = run((*self.options, "capture", "--kind", kind, "--name", SENTINEL_POD), {("kubectl",): ok({"items": []})})
            with self.subTest(kind):
                self.assertEqual((code, fake.calls[0]["argv"]), (0, argv), lines)
        code, _, fake = run((*self.options, "capture", "--kind", "crd", "--name", "nodepools.karpenter.sh"), {("kubectl",): ok({"spec": {}})})
        self.assertEqual((code, fake.calls[0]["argv"]), (0, ["kubectl", "get", "crd", "nodepools.karpenter.sh", "-o", "json"]))
        code, _, fake = run((*self.options, "capture", "--kind", "az-aks", "--resource-group", "rg-mirror", "--cluster", "aks-mirror"), {("az",): ok({"kubernetes": "1.36.4"})})
        self.assertEqual(fake.calls[0]["argv"][:8], ["az", "aks", "show", "-g", "rg-mirror", "-n", "aks-mirror", "--query"])
        self.assertEqual(code, 0)  # the long --query is an argument the recording accepts (it was refused as too long once)
        self.assertEqual([d["kind"] for d in self.written() if d["kind"] == "az-aks"], ["az-aks"])

    def test_nothing_that_could_inject_a_call_is_accepted(self):
        bad = (
            ("capture", "--kind", "pod", "--name", "x; rm -rf /"),
            ("capture", "--kind", "pod", "--name", "not-a-worker-pod"),
            ("capture", "--kind", "pod", "--name", "factory-w-x", "--namespace", "kube-system; x"),
            ("capture", "--kind", "pod"),
            ("capture", "--kind", "crd", "--name", "secrets"),
            ("capture", "--kind", "az-aks", "--resource-group", "rg;x", "--cluster", "c"),
        )
        for argv in bad:
            code, lines, fake = run((*self.options, *argv))
            with self.subTest(argv):
                self.assertEqual(code, 2, lines)
                self.assertEqual(fake.calls, [])
        self.assertEqual(self.written(), [])

    def test_a_scratch_namespace_may_hold_a_pod_of_any_name_but_the_worker_namespace_only_worker_pods(self):
        code, _, fake = run((*self.options, "capture", "--kind", "pod", "--name", "pause-1", "--namespace", "scratch"), {("kubectl",): ok({"kind": "Pod"})})
        self.assertEqual(code, 0)
        self.assertEqual(fake.calls[0]["argv"][:3], ["kubectl", "-n", "scratch"])

    def test_it_needs_a_recording_directory_and_refuses_a_secret(self):
        code, _, fake = run(("capture", "--kind", "nodes"))
        self.assertEqual((code, fake.calls), (2, []))
        code, lines, _ = run((*self.options, "capture", "--kind", "nodes"), {("kubectl",): ok({"kind": "Secret", "data": {"a": "Yg=="}})})
        self.assertEqual(code, 1)
        self.assertEqual(lines, ["FAIL not recorded: secret-object"])
        self.assertEqual(self.written(), [])

    def test_a_failed_call_records_nothing(self):
        code, lines, _ = run((*self.options, "capture", "--kind", "nodes"), {("kubectl",): (1, "", "forbidden")})
        self.assertEqual((code, lines), (1, ["FAIL capture nodes: forbidden"]))
        self.assertEqual(self.written(), [])

    def test_the_recording_option_is_checked_before_a_call(self):
        for options in (("--record", "/nonexistent/dir", "--step", "D5"), ("--record", self.directory.name), ("--record", self.directory.name, "--step", "Q9")):
            code, _, fake = run((*options, "nodes"))
            self.assertEqual((code, fake.calls), (2, []), options)

    def test_nodes_and_quota_and_crds_can_record_too(self):
        run((*self.options, "nodes"), {("kubectl",): ok({"items": [node()]})})
        run((*self.options, "quota", "--location", "westus3"), {("az",): ok(usage_rows())})
        run((*self.options, "crds", "--crd", "nodepool"), Crds.answers(Crds, crd(schema())))
        self.assertEqual(sorted(d["kind"] for d in self.written()), ["az-quota", "crd", "nodes"])


class DenialSample(unittest.TestCase):
    def test_it_records_the_first_line_of_a_real_denial_and_names_the_rules(self):
        message = "ValidatingAdmissionPolicy 'factory-worker-pods' with binding 'factory-worker-pods' denied request: [rule:image] the image is not the allowed one"
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        code, lines, fake = run(("--record", directory.name, "--step", "E6", "denial-sample"), {("kubectl",): (1, "", f'Error from server (Forbidden): error when creating "STDIN": {message}')})
        self.assertEqual(code, 0, lines)
        self.assertEqual(lines[0], "denial: policy factory-worker-pods, 1 rule ids named")
        self.assertEqual(fake.calls[0]["argv"][:3], ["kubectl", "--as", chk.LAUNCHER_USER])
        self.assertEqual(fake.calls[0]["argv"][3:], ["create", "--dry-run=server", "-f", "-", "-o", "json"])
        (path,) = Path(directory.name).iterdir()
        document = json.loads(path.read_text())
        self.assertEqual((document["kind"], rec.problems(document)), ("denial", []))
        self.assertIn("[rule:image]", document["payload"]["text"])

    def test_an_admitted_probe_or_a_denial_without_a_rule_id_is_a_failure(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        options = ("--record", directory.name, "--step", "E6", "denial-sample")
        code, lines, _ = run(options, {("kubectl",): ok({"kind": "Pod"})})
        self.assertEqual(code, 1)
        self.assertEqual(lines, ["FAIL denial-sample: the probe pod was not denied by a policy (admitted)"])
        bare = "ValidatingAdmissionPolicy 'factory-worker-pods' with binding 'b' denied request: no id"
        code, lines, _ = run(options, {("kubectl",): (1, "", bare)})
        self.assertEqual(code, 1)
        self.assertIn("FAIL denial-sample: the message names no bracketed rule id", lines)


class GoldenImage(unittest.TestCase):
    def test_it_prints_the_image_of_the_golden_pod_and_calls_nothing(self):
        code, lines, fake = run(("golden-image",))
        golden = json.loads((chk.GOLDEN / "pod_standard_build.json").read_text())
        self.assertEqual((code, lines, fake.calls), (0, [golden["spec"]["containers"][0]["image"]], []))


def explain(names):
    return "KIND:       Pod\nVERSION:    v1\n\nFIELD: spec <PodSpec>\n\nDESCRIPTION:\n    desc\n\nFIELDS:\n" + "".join(f"  {n}\t<string>\n    Optional text.\n\n" for n in names)


class PodFields(unittest.TestCase):
    def all_named(self):
        return {scope: sorted(chk.policy_fields(pattern) | chk.golden_fields(path)) for scope, path, pattern in chk.POD_SCOPES}

    def answers(self, extra=None):
        named = self.all_named()
        extra = extra or {}
        return {("kubectl", "explain", scope): (0, explain(names + extra.get(scope, []))) for scope, names in named.items()}

    def test_a_cluster_whose_fields_are_all_named_passes(self):
        code, lines, fake = run(("pod-fields",), self.answers())
        self.assertEqual(code, 0, lines)
        self.assertEqual(lines[-1], "pod-fields: every field is named")
        self.assertEqual([c["argv"] for c in fake.calls], [["kubectl", "explain", scope] for scope, *_ in chk.POD_SCOPES])

    def test_a_field_the_cluster_has_and_nothing_names_is_a_failure_with_its_name(self):
        code, lines, _ = run(("pod-fields",), self.answers({"pod.spec": ["hostnameOverride"], "pod.spec.containers.securityContext": ["userNamespaces"]}))
        self.assertEqual(code, 1)
        self.assertIn("FAIL unnamed field pod.spec: hostnameOverride", lines)
        self.assertIn("FAIL unnamed field pod.spec.containers.securityContext: userNamespaces", lines)
        self.assertIn("pod-fields pod.spec: %d fields in the cluster, %d named by the policy or a golden pod, 1 unnamed" % (len(self.all_named()["pod.spec"]) + 1, len(self.all_named()["pod.spec"])), lines)

    def test_a_field_the_cluster_lacks_is_not_a_problem(self):
        answers = self.answers()
        names = self.all_named()["pod.spec"][1:]
        answers[("kubectl", "explain", "pod.spec")] = (0, explain(names))
        self.assertEqual(run(("pod-fields",), answers)[0], 0)

    def test_an_answer_that_lists_nothing_fails_closed(self):
        for answer in ((0, "no fields here"), (1, "", "error"), subprocess.TimeoutExpired("kubectl", 1)):
            answers = self.answers()
            answers[("kubectl", "explain", "pod.spec")] = answer
            code, lines, _ = run(("pod-fields",), answers)
            self.assertEqual(code, 1)
            self.assertIn("FAIL pod-fields pod.spec: no field could be read from the answer of `kubectl explain`", lines)

    def test_the_scopes_read_the_deny_lists_of_the_real_policy(self):
        fields = {scope: chk.policy_fields(pattern) for scope, _, pattern in chk.POD_SCOPES}
        self.assertIn("hostNetwork", fields["pod.spec"])
        self.assertIn("lifecycle", fields["pod.spec.containers"])
        self.assertIn("privileged", fields["pod.spec.containers.securityContext"])


class Quantities(unittest.TestCase):
    def test_cpu_and_memory(self):
        self.assertEqual([chk.cpu_milli(x) for x in ("100m", "2", "1.5", "0", "3860m")], [100, 2000, 1500, 0, 3860])
        self.assertEqual([chk.memory_mib(x) for x in ("128Mi", "4Gi", "1048576Ki", "0", "1G")], [128, 4096, 1024, 0, 953])
        for bad in ("", "x", "-1", "1e3", "1Pi", None):
            self.assertIsNone(chk.cpu_milli(bad) if bad != "1Pi" else None)
            self.assertIsNone(chk.memory_mib(bad))


class Safety(unittest.TestCase):
    def test_no_call_uses_a_shell_and_the_only_subprocess_function_is_run(self):
        self.assertNotIn(("shell", True), keywords("live_checks"))
        names = calls("live_checks")
        self.assertEqual({n for n in names if n.startswith("subprocess.")}, set())  # the runner is injected: subprocess.run is only the default
        self.assertFalse(names & {"os.system", "os.popen", "eval", "exec"})
        self.assertFalse(imported_modules("live_checks") & {"socket", "ssl", "http", "urllib", "requests"})

    def test_every_call_has_a_timeout_a_list_and_no_other_option(self):
        fake = Fake({("kubectl",): ok({"items": []}), ("az",): ok(usage_rows())})
        for argv in (("nodes",), ("colocation",), ("node-requests",), ("quota", "--location", "westus3"), ("reads", "--profile", "plan"), ("can-i", "--profile", "worker")):
            run(argv, fake.answers)
        runs = []
        for argv in (("nodes",), ("quota", "--location", "westus3"), ("reads", "--profile", "plan")):
            runs.append(run(argv, fake.answers)[2])
        for single in runs:
            for call in single.calls:
                self.assertIsInstance(call["argv"], list)
                self.assertEqual(call["timeout"], 60)
                self.assertEqual(call["extra"], {})

    @staticmethod
    def subcommand(argv):
        """The kubectl or az subcommand of an argv: the first word that is not the program or an option and its value."""
        rest, skip = argv[1:], False
        for word in rest:
            if skip:
                skip = False
            elif word in ("--kubeconfig", "--as", "-n"):
                skip = True
            elif not word.startswith("-"):
                return word
        return None

    def test_no_command_calls_a_verb_that_writes(self):
        writing = {"create", "apply", "delete", "patch", "replace", "edit", "scale", "set", "annotate", "label", "drain", "cordon", "exec", "cp", "run", "expose", "rollout", "update", "put"}
        fake = Fake({("kubectl",): ok({"items": [node(), pod()]}), ("az",): ok(usage_rows())})
        for argv in (
            ("nodes",), ("node-requests",), ("colocation",), ("quota", "--location", "westus3"), ("reads", "--profile", "plan"),
            ("can-i", "--profile", "launcher"), ("crds",),
        ):
            _, _, used = run(argv, fake.answers)
            for call in used.calls:
                verb = self.subcommand(call["argv"])
                self.assertNotIn(verb, writing, (argv, call["argv"]))
                self.assertIn(verb, {"get", "auth", "vm"}, call["argv"])

    def test_the_only_dry_run_is_the_denial_sample_and_it_is_server_side_and_persists_nothing(self):
        text = (chk.ROOT / "tools" / "live_checks.py").read_text()
        self.assertEqual(text.count("dry_run_create"), 1)

    def test_a_fixed_line_shape_for_every_printed_line(self):
        fake = Fake({("kubectl",): ok({"items": [node(), pod()]}), ("az",): ok(usage_rows())})
        for argv in (("nodes",), ("node-requests",), ("colocation",), ("quota", "--location", "westus3"), ("reads", "--profile", "plan"), ("can-i", "--profile", "worker"), ("crds",)):
            _, lines, _ = run(argv, fake.answers)
            for line in lines:
                self.assertRegex(line, LINE)
                self.assertNotIn(SENTINEL_NODE, line)
                self.assertNotIn(SENTINEL_POD, line)

    def test_a_timeout_option_out_of_range_is_refused(self):
        for value in ("0", "601", "-5"):
            self.assertEqual(run(("--timeout", value, "nodes"))[0], 2)


class KubeconfigRequired(unittest.TestCase):
    """A command that calls `kubectl` with no `--kubeconfig` would address the default context, which may be production (runbook, A4)."""

    KUBECTL_COMMANDS = (
        ("nodes",), ("node-requests",), ("crds",), ("reads", "--profile", "plan"), ("can-i", "--profile", "launcher"), ("colocation",),
        ("capture", "--kind", "pod", "--name", SENTINEL_POD), ("capture", "--kind", "nodeclaim"), ("denial-sample",), ("pod-fields",),
    )

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def test_every_command_that_calls_kubectl_refuses_to_run_without_a_kubeconfig_and_calls_nothing(self):
        for argv in self.KUBECTL_COMMANDS:
            with self.subTest(argv=argv):
                code, lines, fake = run(("--record", self.directory.name, "--step", "D5", *argv), {("kubectl",): ok({"items": []})}, without_kubeconfig=True)
                self.assertEqual(code, 2, argv)
                self.assertEqual(fake.calls, [], argv)
                self.assertEqual(len(lines), 1)
                self.assertIn("needs --kubeconfig", lines[0])

    def test_the_commands_that_call_only_az_or_nothing_need_none(self):
        code, _, fake = run(("quota", "--location", "westus3"), {("az",): ok(usage_rows())}, without_kubeconfig=True)
        self.assertEqual(code, 0)
        self.assertEqual({call["argv"][0] for call in fake.calls}, {"az"})
        code, lines, _ = run(("golden-image",), without_kubeconfig=True)
        self.assertEqual(code, 0)
        code, _, fake = run(("--record", self.directory.name, "--step", "D1", "capture", "--kind", "az-aks", "--resource-group", "rg-x", "--cluster", "aks-x"), {("az",): ok({"tier": "Free"})}, without_kubeconfig=True)
        self.assertEqual(code, 0)
        self.assertEqual({call["argv"][0] for call in fake.calls}, {"az"})

    def test_every_kubectl_command_the_tool_has_is_in_the_list_of_the_ones_that_refuse(self):
        named = {argv[0] for argv in self.KUBECTL_COMMANDS}
        self.assertEqual(named, set(chk.COMMANDS) - {"quota", "golden-image"})

    def test_a_kubeconfig_is_passed_to_every_kubectl_call_of_a_command(self):
        for argv in self.KUBECTL_COMMANDS:
            if argv[0] in ("capture", "denial-sample"):
                continue
            code, lines, fake = run(("--kubeconfig", "/x/config", *argv), {("kubectl",): ok({"items": []})}, without_kubeconfig=True)
            self.assertTrue(fake.calls, argv)
            for call in fake.calls:
                self.assertEqual(call["argv"][1:3], ["--kubeconfig", "/x/config"], (argv, call["argv"]))


class DisplayFilter(unittest.TestCase):
    """What comes from the cluster or from Azure and is printed goes through `sanitise`: nothing that a terminal or a shell would act on survives."""

    HOSTILE = "Std\x1b[31m$(id)`x`;\"q\"\nnext"

    def test_a_node_size_from_the_cluster_is_printed_without_a_control_or_shell_character(self):
        document = {"items": [node(pool="worker", size=self.HOSTILE)]}
        _, lines, _ = run(("nodes",), {("kubectl",): ok(document)})
        (worker,) = [line for line in lines if line.startswith("worker node:")]
        self.assertNotRegex(worker, r"[\x00-\x1f\x7f$`;\"\\]")
        self.assertIn("size Std?[31m?(id)?x??", worker)

    def test_the_filter_itself_keeps_the_safe_set_and_cuts_the_length(self):
        for text in ("Standard_D4s_v6", "a b.c:d/e,f(g)[h]=i-j", "it's"):
            self.assertEqual(rec.sanitise(text), text)
        for char in ("\x1b", "\x00", "\n", "\r", "\t", "$", "`", ";", "\"", "|", "&", "<", ">", "\\", "{", "}", "*", "!", "~", "#", "\x7f", "\u00e9"):
            self.assertEqual(rec.sanitise(f"a{char}b"), "a?b", repr(char))
        self.assertEqual(len(rec.sanitise("x" * 500)), 120)
        self.assertEqual(rec.sanitise("x" * 500, 40), "x" * 40)


if __name__ == "__main__":
    unittest.main()

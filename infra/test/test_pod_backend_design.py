"""Design guards for the launcher's Role and the manager pod of the pod backend (slices 16 and 17, issues #144, #145 and #147).

They read the .tf files and the Python sources as text (standard library only, like the other tests of this directory), so they
run in CI as `python3 -I -m unittest discover -s infra/test` with no package installed: the contract is loaded BY PATH and the
launcher, relay and Kubernetes client sources are read with `ast` and regular expressions, never imported. A text test can check
defaults, gating, the shape of an object and a wording; it cannot prove that a plan shows no line for the Deployment, that the
kubelet starts three containers that share two directories under `hostUsers: false`, or that the webhook leaves the new
containers alone. Only a real `terraform plan` and a run on the throwaway mirror can: the pull request says so, and
docs/pod-backend.md ("Terraform C") lists every such claim under UNVERIFIED.

What is pinned here:
  * slice 16: ONE Role and ONE RoleBinding of the worker namespace, both gated by the one count local, both waiting for the
    default-deny policy AND both admission policy bindings; the Role grants exactly the verbs on pods and Secrets that the
    launcher's Kubernetes client uses (read from src/factory/kube.py) and nothing more (no wildcard, no update, no patch, no
    subresource, no Secret read); the binding names one subject, the manager's service account, the very identity the admission
    policies are bound to; no ClusterRole, no ClusterRoleBinding and no binding of the worker service account exists;
  * slice 17: every addition to the Deployment is a `concat` or `merge` of a local that is an empty list, map or null while the
    backend is off (so the Deployment of a default plan is the object it was); the launcher and the relay run the image of the
    admission policy parameter, hold the secrets and the mounts the design record assigns them and no others, carry none of the
    three relaxations, mount the Kubernetes token in the launcher ONLY; the init container is a fixed command made of the
    contract's ids and paths; every environment name of the manager, launcher and relay groups is produced exactly once from the
    contract; the ceilings read the one pool local; the relay Service cannot make Kubernetes inject a clashing variable; the
    manager taint is a separate default-off flip that needs the backend.

Run from the repository root, either as discovery or by dotted module name:
    python3 -I -m unittest discover -s infra/test -v
    python3 -m unittest infra.test.test_pod_backend_design -v
"""

import ast
import importlib.util
import json
import re
import sys
import unittest
from pathlib import Path

# Sibling modules are imported by bare name: put this directory on sys.path so the import also works when the
# module is loaded by dotted name from the repository root (python3 -m unittest infra.test.<module>).
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from test_worker_ns_design import (
    POLICY_ADDRESS,
    block,
    children,
    code,
    depends_on,
    locals_assignment,
    resource,
    role_like_resources,
    squash,
    unordered_role_like_resources,
    variable,
)

INFRA = Path(__file__).resolve().parent.parent
ROOT = INFRA.parent
SRC = ROOT / "src" / "factory"

_SPEC = importlib.util.spec_from_file_location("podcontract", SRC / "podcontract.py")
pc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(pc)

ROLES = ("azurerm_role_assignment.aks_cluster_admin", "azurerm_role_assignment.aks_cluster_user")
FENCE = (
    POLICY_ADDRESS,
    "kubernetes_manifest.worker_policy_pods_binding",
    "kubernetes_manifest.worker_policy_secrets_binding",
)
BACKEND = "pod_backend.tf"
EXPOSED = (
    "pod_backend_main_env",
    "pod_backend_main_mounts",
    "pod_backend_pod_annotations",
    "pod_backend_pod_spec_extra",
    "pod_backend_init_containers",
    "pod_backend_containers",
    "pod_backend_volumes",
)


def source(name):
    return (SRC / name).read_text(encoding="utf-8")


def tf_files():
    return {path.name: path.read_text(encoding="utf-8") for path in sorted(INFRA.glob("*.tf"))}


def backend_local(name):
    return squash(locals_assignment(BACKEND, name))


def list_of(text):
    """The strings of a `["a", "b"]` list literal."""
    return re.findall(r'"([^"]*)"', text)


def rules_of(role_text):
    """[(api_groups, resources, verbs)] of the `rule` blocks of a Role resource."""
    found = []
    for rule in children(role_text, "rule"):
        groups, resources, verbs = (list_of(re.search(rf"{name}\s*=\s*\[(.*?)\]", rule, re.DOTALL).group(1)) for name in ("api_groups", "resources", "verbs"))
        found.append((groups, resources, verbs))
    return found


def client_calls():
    """{(resource, verb)} that src/factory/kube.py performs, read from its `_call(<METHOD>, self._collection|_item(<resource>...` lines."""
    verbs = {("POST", "collection"): "create", ("GET", "item"): "get", ("GET", "collection"): "list", ("DELETE", "item"): "delete"}
    found = set()
    for method, shape, kind in re.findall(r'_call\(\s*"([A-Z]+)",\s*self\._(collection|item)\("(\w+)"', source("kube.py")):
        found.add((kind, verbs[(method, shape)]))
    return found


def render(text):
    """A string expression of the backend file with every `${local.worker_contract.a.b}` replaced by the contract's value."""

    def value(match):
        node = pc.SHARED
        for part in match.group(1).split("."):
            node = node[part]
        return str(node)

    return re.sub(r"\$\{local\.worker_contract\.([a-z_.]+)\}", value, text)


class TheRoleOfTheLauncher(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.role = resource("worker_rbac.tf", "kubernetes_role_v1", "worker_launcher")
        cls.binding = resource("worker_rbac.tf", "kubernetes_role_binding_v1", "worker_launcher")
        cls.text = code("worker_rbac.tf")

    def test_the_file_holds_exactly_one_role_and_one_binding_and_both_use_the_count_local_of_the_namespace(self):
        self.assertEqual(re.findall(r'(?m)^resource "(\w+)" "(\w+)"', self.text), [("kubernetes_role_v1", "worker_launcher"), ("kubernetes_role_binding_v1", "worker_launcher")])
        for body in (self.role, self.binding):
            self.assertRegex(body, r"(?m)^\s*count\s*=\s*local\.worker_ns_count\s*$")
            self.assertIn("namespace = kubernetes_namespace_v1.worker[0].metadata[0].name", body)

    def test_both_wait_for_the_default_deny_policy_and_both_admission_policy_bindings_and_the_cluster_admin_roles(self):
        for name, body in (("role", self.role), ("binding", self.binding)):
            listed = depends_on(body)
            for address in (*FENCE, *ROLES):
                with self.subTest(resource=name, address=address):
                    self.assertRegex(listed, rf"(?m)^\s*{re.escape(address)},?\s*$")
        self.assertIn("kubernetes_role_v1.worker_launcher", depends_on(self.binding))

    def test_the_scans_of_the_earlier_slices_see_both_resources_and_find_nothing_unordered(self):
        texts = {name: text for name, text in tf_files().items()}
        seen = {address for _, address, _ in role_like_resources(texts)}
        self.assertLessEqual({"kubernetes_role_v1.worker_launcher", "kubernetes_role_binding_v1.worker_launcher"}, seen)
        self.assertEqual(unordered_role_like_resources(texts), [])

    def test_the_role_grants_exactly_what_the_launchers_client_uses(self):
        granted = {(resources[0], verb) for _, resources, verbs in rules_of(self.role) for verb in verbs}
        self.assertEqual(granted, client_calls())
        self.assertEqual(client_calls(), {("pods", "create"), ("pods", "get"), ("pods", "list"), ("pods", "delete"), ("secrets", "create"), ("secrets", "delete")})

    def test_the_role_names_the_core_group_and_one_resource_per_rule_and_nothing_else(self):
        rules = rules_of(self.role)
        self.assertEqual([(groups, resources) for groups, resources, _ in rules], [([""], ["pods"]), ([""], ["secrets"])])
        flat = squash(self.role)
        for forbidden in ('"*"', "update", "patch", "watch", "deletecollection", "pods/", "exec", "attach", "portforward", "resource_names", "impersonate", "escalate"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, flat)
        self.assertNotRegex(flat, r'"bind"')
        secrets = [verbs for _, resources, verbs in rules if resources == ["secrets"]]
        self.assertEqual(secrets, [["create", "delete"]], "a Secret is written and deleted, never read")

    def test_the_binding_names_the_role_and_one_subject_the_manager_service_account(self):
        (reference,) = children(self.binding, "role_ref")
        self.assertRegex(reference, r'api_group\s*=\s*"rbac\.authorization\.k8s\.io"')
        self.assertRegex(reference, r'kind\s*=\s*"Role"')
        self.assertIn("name      = kubernetes_role_v1.worker_launcher[0].metadata[0].name", reference)
        (subject,) = children(self.binding, "subject")
        self.assertRegex(subject, r'kind\s*=\s*"ServiceAccount"')
        self.assertIn("name      = local.k8s_service_account", subject)
        self.assertIn("namespace = local.k8s_namespace", subject)
        self.assertNotIn("api_group", subject)

    def test_the_subject_is_the_identity_the_admission_policies_are_bound_to(self):
        locals_text = code("locals.tf")
        namespace = re.search(r'(?m)^\s*k8s_namespace\s*=\s*"([^"]+)"', locals_text).group(1)
        account = re.search(r'(?m)^\s*k8s_service_account\s*=\s*"([^"]+)"', locals_text).group(1)
        username = f"system:serviceaccount:{namespace}:{account}"
        for name in ("pod-policy.json", "secret-policy.json"):
            policy = json.loads((INFRA / "policy" / name).read_text(encoding="utf-8"))
            with self.subTest(policy=name):
                self.assertIn(username, json.dumps(policy["spec"]["matchConditions"]))
        self.assertEqual((namespace, account), (pc.MANAGER_NAMESPACE, "factory"))

    def test_there_is_no_cluster_role_and_nothing_binds_the_worker_service_account(self):
        # The ClusterRoles of Prometheus and of kube-state-metrics (issue 149: the base one and the NodeClaim one, each with its binding;
        # manifests, monitoring.tf) are the only cluster-scoped role-like resources of the tree.
        found = {address for _, address, _ in role_like_resources(tf_files())}
        self.assertEqual(
            {address for address in found if "cluster_role" in address},
            {
                "kubernetes_manifest.prometheus_cluster_role",
                "kubernetes_manifest.prometheus_cluster_role_binding",
                "kubernetes_manifest.kube_state_metrics_cluster_role",
                "kubernetes_manifest.kube_state_metrics_cluster_role_binding",
                "kubernetes_manifest.kube_state_metrics_nodeclaim_cluster_role",
                "kubernetes_manifest.kube_state_metrics_nodeclaim_cluster_role_binding",
            },
        )
        for name, text in tf_files().items():
            for _, address, body in role_like_resources({name: text}):
                if "subject" in body:
                    with self.subTest(address=address):
                        self.assertNotIn("worker_service_account", body)
                        self.assertNotIn("kubernetes_service_account_v1.worker", body)

    def test_the_plan_identity_needs_no_new_data_action(self):
        role = json.loads((INFRA / "roles" / "factory-plan-cluster-reader.json").read_text(encoding="utf-8"))
        actions = set(role["DataActions"])
        for kind in ("roles", "rolebindings"):
            self.assertIn(f"Microsoft.ContainerService/managedClusters/rbac.authorization.k8s.io/{kind}/read", actions)
        self.assertEqual(len(actions), 7)


class TheDeploymentIsOnlyExtended(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.deployment = resource("k8s.tf", "kubernetes_manifest", "factory_deployment")

    def test_every_addition_is_a_concat_or_merge_of_a_backend_local(self):
        flat = squash(self.deployment)
        for needle in (
            "annotations = merge({",
            "}, local.pod_backend_pod_annotations)",
            "spec = merge({",
            "}, local.pod_backend_pod_spec_extra)",
            "initContainers = concat([",
            "], local.pod_backend_init_containers)",
            "containers = concat([",
            "], local.pod_backend_containers)",
            "local.app_env_optional_list, local.pod_backend_main_env)",
            "], local.pod_backend_main_mounts)",
            "volumes = concat([",
            "], local.pod_backend_volumes)",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, flat)

    def test_the_literal_lists_still_hold_exactly_what_they_held_before(self):
        flat = self.deployment
        self.assertEqual(re.findall(r'(?m)^\s{14}name\s*=\s*"(\w[\w-]*)"$', flat[flat.index("initContainers = concat(") : flat.index("containers = concat([\n")]), ["keyvault-sync"])
        main = flat[flat.index("\n          containers = concat([") :]
        self.assertEqual(re.findall(r'(?m)^\s{14}name\s*=\s*"(\w[\w-]*)"$', main[: main.index("], local.pod_backend_containers)")]), ["factory"])
        self.assertEqual(re.findall(r'(?m)^\s{14}name\s*=\s*"(\w[\w-]*)"$', main[main.index("volumes = concat(") :]), ["data", "repos", "keyvault"])

    def test_the_deployment_never_spells_the_gate_or_an_object_of_the_namespace(self):
        # tests/test_infra_cluster_guard.py would then list the Deployment among the worker objects, and a flip would be refused as a pool change.
        for spelling in ("pod_backend_enabled", "worker_ns_count", "kubernetes_namespace_v1.worker", "kubernetes_role", "worker_launcher", "factory-workers", "worker_contract.names.worker_namespace"):
            with self.subTest(spelling=spelling):
                self.assertNotIn(spelling, self.deployment)

    def test_the_pod_still_has_no_kubernetes_token_and_no_service_links(self):
        self.assertRegex(self.deployment, r"automountServiceAccountToken\s*=\s*false")
        self.assertRegex(self.deployment, r"enableServiceLinks\s*=\s*false")
        self.assertNotIn("serviceAccountToken", self.deployment)


class EveryBackendLocalIsEmptyWhileTheBackendIsOff(unittest.TestCase):
    def test_the_switch_is_the_count_local_of_the_namespace(self):
        self.assertEqual(backend_local("pod_backend_on"), "local.worker_ns_count == 1")
        self.assertEqual(squash(locals_assignment("worker_ns.tf", "worker_ns_count")), "var.pod_backend_enabled ? 1 : 0")

    def test_each_exposed_local_is_a_for_expression_filtered_by_the_switch_over_its_all_local(self):
        # A conditional `cond ? [a, b] : []` needs one common type for both branches, which a list of unlike objects does not have,
        # so the value is built unconditionally (`_all`) and filtered: `[for x in all : x if switch]` is an empty tuple when off.
        for name in EXPOSED:
            with self.subTest(local=name):
                expression = backend_local(name)
                self.assertRegex(expression, rf"^[\[{{]\s?for \w+(?:, \w+)? in local\.{name}_all : .+ if local\.pod_backend_on\s?[\]}}]$")
                self.assertTrue(locals_assignment(BACKEND, f"{name}_all").strip())

    def test_only_those_locals_and_the_taint_reach_k8s_tf_and_aks_tf(self):
        used = set(re.findall(r"local\.(pod_backend_\w+|manager_pool_node_taints)", code("k8s.tf") + code("aks.tf")))
        self.assertEqual(used, set(EXPOSED) | {"manager_pool_node_taints"})

    def test_the_taint_is_null_not_an_empty_list_while_it_is_off_and_the_pool_reads_it(self):
        taint = backend_local("manager_pool_node_taints")
        self.assertRegex(taint, r"^var\.manager_pool_taint_enabled \? \[.*\] : null$")
        self.assertIn("${local.worker_contract.nodes.manager_taint_key}=${local.worker_contract.nodes.manager_taint_value}:${local.worker_contract.nodes.taint_effect}", taint)
        factory = resource("aks.tf", "azurerm_kubernetes_cluster_node_pool", "factory")
        self.assertIn("node_taints           = local.manager_pool_node_taints", factory)
        monitoring = resource("aks.tf", "azurerm_kubernetes_cluster_node_pool", "monitoring")
        self.assertIn('node_taints           = ["workload=monitoring:NoSchedule"]', monitoring)
        self.assertEqual(len(re.findall(r"(?m)^\s*node_taints\s*=", code("aks.tf"))), 2)

    def test_the_taint_flip_is_off_by_default_and_needs_the_backend(self):
        text = variable("manager_pool_taint_enabled")
        self.assertRegex(text, r"(?m)^\s*type\s*=\s*bool\s*$")
        self.assertRegex(text, r"(?m)^\s*default\s*=\s*false\s*$")
        self.assertIn("!var.manager_pool_taint_enabled || var.pod_backend_enabled", text)
        flat = squash(text)
        for needle in ("workload=manager:NoSchedule", "UNVERIFIED", "REPLACE", "cluster_snapshots_taken=true", "mirror"):
            with self.subTest(needle=needle):
                self.assertIn(needle, flat)

    def test_the_toleration_comes_with_the_backend_and_names_the_contracts_manager_taint(self):
        spec = backend_local("pod_backend_pod_spec_extra_all")
        self.assertEqual(spec.count("tolerations"), 1)
        for needle in ("key = local.worker_contract.nodes.manager_taint_key", 'operator = "Equal"', "value = local.worker_contract.nodes.manager_taint_value", "effect = local.worker_contract.nodes.taint_effect"):
            with self.subTest(needle=needle):
                self.assertIn(needle, spec)
        self.assertNotIn("worker_taint", spec)
        self.assertEqual(pc.TAINT_EFFECT, "NoSchedule")

    def test_the_gates_are_not_set_in_any_committed_file(self):
        for path in INFRA.glob("*.tf*"):
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"(?m)^\s*manager_pool_taint_enabled\s*=\s*true", path.name)


class TheContainers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.all = backend_local("pod_backend_containers_all")
        raw = locals_assignment(BACKEND, "pod_backend_containers_all")
        names = list(re.finditer(r'(?m)^    \{\n      name\s*=\s*local\.pod_backend_container_names\.(\w+)', raw))
        assert [found.group(1) for found in names] == ["launcher", "relay"], "the list does not hold the launcher and then the relay"
        cls.launcher = raw[names[0].start() : names[1].start()]
        cls.relay = raw[names[1].start() :]
        cls.init = locals_assignment(BACKEND, "pod_backend_init_containers_all")
        cls.sidecar = locals_assignment(BACKEND, "pod_backend_sidecar_security")
        cls.volumes = locals_assignment(BACKEND, "pod_backend_volumes_all")

    def test_all_three_run_the_image_of_the_admission_policy_parameter(self):
        for name, text in (("launcher", self.launcher), ("relay", self.relay), ("init", self.init)):
            with self.subTest(container=name):
                self.assertRegex(text, r"(?m)^\s*image\s*=\s*local\.image\s*$")
        self.assertRegex(code("worker_policy.tf"), r"data = \{\s*image = local\.image\s*\}")
        self.assertRegex(self.launcher, r"name = local\.worker_contract\.env\.launcher\.image, value = local\.image\s*\}")
        self.assertEqual(len(re.findall(r"(?m)^\s*image\s*=", code(BACKEND))), 3)

    def test_the_commands_are_the_images_interpreter_and_the_programs_module(self):
        self.assertEqual(backend_local("pod_backend_python"), '"/opt/factory/bin/python"')
        self.assertIn('"/opt/factory/bin/python"', code("k8s.tf"))
        self.assertIn('command         = [local.pod_backend_python, "-m", "factory.launcher"]', self.launcher)
        self.assertIn('command         = [local.pod_backend_python, "-m", "factory.relay"]', self.relay)
        for module in ("launcher.py", "relay.py"):
            self.assertIn('if __name__ == "__main__":', source(module))

    def test_the_sidecars_drop_everything_and_carry_none_of_the_three_relaxations(self):
        flat = squash(self.sidecar)
        for needle in ("runAsNonRoot = true", "allowPrivilegeEscalation = false", "readOnlyRootFilesystem = true", 'capabilities = { drop = ["ALL"] }', 'seccompProfile = { type = "RuntimeDefault" }'):
            with self.subTest(needle=needle):
                self.assertIn(needle, flat)
        for text in (self.sidecar, self.launcher, self.relay):
            for forbidden in ("procMount", "appArmorProfile", "privileged", "Unconfined", "Unmasked", '"add"', "add ", "hostPath", "hostNetwork", "hostPID", "hostIPC", "fsGroup", "SYS_ADMIN"):
                with self.subTest(forbidden=forbidden):
                    self.assertNotIn(forbidden, text)

    def test_each_sidecar_has_its_own_contract_uid_and_the_shared_group(self):
        self.assertIn("merge(local.pod_backend_sidecar_security, {\n        runAsUser  = local.worker_contract.ids.launcher_uid\n        runAsGroup = local.worker_contract.ids.shared_gid\n      })", self.launcher)
        self.assertIn("merge(local.pod_backend_sidecar_security, {\n        runAsUser  = local.worker_contract.ids.relay_uid\n        runAsGroup = local.worker_contract.ids.shared_gid\n      })", self.relay)
        self.assertNotEqual((pc.LAUNCHER_UID, pc.RELAY_UID), (pc.SUPERVISOR_UID, pc.WORKER_UID))
        self.assertEqual(pc.SHARED_GID, pc.SUPERVISOR_UID)

    def test_requests_equal_limits_and_the_sidecars_are_small(self):
        sizing = backend_local("pod_backend_sizing")
        found = {name: (cpu, memory) for name, cpu, memory in re.findall(r'(\w+) = \{ cpu = "([0-9m]+)", memory = "([0-9]+Mi)" \}', sizing)}
        self.assertEqual(set(found), {"launcher", "relay"})
        self.assertLessEqual(sum(int(cpu[:-1]) for cpu, _ in found.values()), 500)
        self.assertLessEqual(sum(int(memory[:-2]) for _, memory in found.values()), 1024)
        for name, text in (("launcher", self.launcher), ("relay", self.relay)):
            with self.subTest(container=name):
                self.assertIn(f"requests = local.pod_backend_sizing.{name}", text)
                self.assertIn(f"limits   = local.pod_backend_sizing.{name}", text)
        self.assertIn("requests = local.secrets_init_resources\n        limits   = local.secrets_init_resources", self.init)

    def test_the_kubernetes_token_is_mounted_by_the_launcher_and_by_nothing_else(self):
        token = "local.pod_backend_volume_names.token"
        self.assertEqual(code(BACKEND).count(token), 2, "the volume definition and the launcher's mount")
        self.assertIn(token, self.launcher)
        for text in (self.relay, self.init, locals_assignment(BACKEND, "pod_backend_main_mounts_all")):
            self.assertNotIn(token, text)
            self.assertNotIn("serviceaccount", text)
        for name, text in tf_files().items():
            if name != BACKEND:
                self.assertNotIn("serviceAccountToken", code(name), name)
        self.assertEqual(code(BACKEND).count("serviceAccountToken"), 1)

    def test_the_token_volume_is_the_projected_token_and_the_ca_the_client_reads_at_the_standard_path(self):
        client = source("kube.py")
        directory = re.search(r'(?m)^SERVICE_ACCOUNT_DIR = "([^"]+)"', client).group(1)
        token_name = re.search(r'(?m)^TOKEN_PATH = f"\{SERVICE_ACCOUNT_DIR\}/([\w.]+)"', client).group(1)
        ca_name = re.search(r'(?m)^CA_PATH = f"\{SERVICE_ACCOUNT_DIR\}/([\w.]+)"', client).group(1)
        self.assertEqual(backend_local("pod_backend_token_mount_path"), f'"{directory}"')
        volume = squash(self.volumes)
        self.assertIn(f'serviceAccountToken = {{ path = "{token_name}", expirationSeconds = 3600 }}', volume)
        self.assertIn(f'configMap = {{ name = "kube-root-ca.crt", items = [{{ key = "ca.crt", path = "{ca_name}" }}] }}', volume)
        self.assertRegex(self.launcher, r"name = local\.pod_backend_volume_names\.token, mountPath = local\.pod_backend_token_mount_path, readOnly = true")

    def test_the_only_secret_in_any_environment_is_the_anthropic_key_of_the_launcher(self):
        self.assertEqual(code(BACKEND).count("valueFrom"), 1)
        self.assertEqual(code(BACKEND).count("secretKeyRef"), 1)
        self.assertIn("valueFrom", self.launcher)
        self.assertIn("name = local.kv_secret_objects.anthropic_api_key.k8s_secret_key", self.launcher)
        self.assertIn("name = local.k8s_synced_secret", self.launcher)
        self.assertEqual(code(BACKEND).count("kv_secret_objects"), 2, "only the Anthropic key, only for the launcher (name and key)")
        self.assertNotIn("github", code(BACKEND).lower())
        for text in (self.relay, self.init):
            self.assertNotIn("valueFrom", text)
            self.assertNotIn("secretKeyRef", text)
        self.assertNotIn("kv_secret", backend_local("pod_backend_main_env_all"))

    def test_container_insights_is_told_not_to_copy_either_sidecars_environment(self):
        for text in (self.launcher, self.relay):
            self.assertIn('{ name = "AZMON_COLLECT_ENV", value = "False" }', text)
        self.assertIn("AZMON_COLLECT_ENV", pc.ENV_POD)

    def test_the_relay_declares_its_port_under_the_name_the_service_targets(self):
        self.assertIn('ports           = [{ name = "relay", containerPort = local.worker_contract.ports.relay, protocol = "TCP" }]', self.relay)
        service = resource(BACKEND, "kubernetes_service_v1", "factory_relay")
        self.assertIn('target_port = "relay"', service)
        self.assertNotRegex(self.launcher, r"(?m)^\s*ports\s*=")

    def test_the_volumes_are_two_emptydirs_with_the_contracts_limits_and_the_projected_token(self):
        volume = squash(self.volumes)
        self.assertIn("emptyDir = { sizeLimit = local.worker_contract.volumes.socket_dir_size_limit }", volume)
        self.assertIn("emptyDir = { sizeLimit = local.worker_contract.volumes.spool_size_limit }", volume)
        self.assertEqual(volume.count("emptyDir"), 2)
        self.assertNotIn("medium", volume)
        self.assertNotIn("hostPath", volume)
        self.assertEqual((pc.SPOOL_SIZE_LIMIT, pc.SOCKET_DIR_SIZE_LIMIT), ("16Gi", "1Mi"))

    def test_the_main_container_gets_the_socket_read_only_and_the_spool_read_write_and_the_four_names_of_its_group(self):
        mounts = squash(locals_assignment(BACKEND, "pod_backend_main_mounts_all"))
        self.assertIn("{ name = local.pod_backend_volume_names.socket, mountPath = local.worker_contract.paths.launcher_socket_dir, readOnly = true }", mounts)
        self.assertIn("{ name = local.pod_backend_volume_names.spool, mountPath = local.worker_contract.paths.spool_dir }", mounts)
        self.assertEqual(mounts.count("readOnly"), 1)
        env = squash(locals_assignment(BACKEND, "pod_backend_main_env_all"))
        for key in pc.ENV_MANAGER:
            self.assertEqual(env.count(f"local.worker_contract.env.manager.{key},"), 1, key)
        self.assertIn("value = local.worker_contract.env_values.pod_backend_on", env)

    def test_the_workload_identity_webhook_is_told_to_skip_exactly_the_three_new_containers(self):
        names = dict(re.findall(r'(?m)^    (\w+)\s*=\s*"([\w-]+)"$', locals_assignment(BACKEND, "pod_backend_container_names")))
        self.assertEqual(names, {"init": "volume-perms", "launcher": "launcher", "relay": "relay"})
        self.assertIn('"azure.workload.identity/skip-containers" = join(";", values(local.pod_backend_container_names))', backend_local("pod_backend_pod_annotations_all"))
        # The container names of the deployment's own containers must not be in it: the main container keeps its identity.
        self.assertNotIn("factory", names.values())
        self.assertNotIn("keyvault-sync", names.values())


class TheInitContainer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.init = locals_assignment(BACKEND, "pod_backend_init_containers_all")

    def test_the_command_is_two_owners_and_two_modes_from_the_contracts_ids_and_paths(self):
        commands = re.search(r'command = \[\s*"/bin/sh",\s*"-c",\s*join\(" && ", \[(.*?)\]\),\s*\]', self.init, re.DOTALL)
        rendered = [render(line) for line in re.findall(r'"([^"]+)"', commands.group(1))]
        expected = [
            f"chown {pc.LAUNCHER_UID}:{pc.SHARED_GID} {pc.LAUNCHER_SOCKET_DIR}",
            "chmod 0750 " + pc.LAUNCHER_SOCKET_DIR,
            f"chown {pc.RELAY_UID}:{pc.SHARED_GID} {pc.SPOOL_DIR}",
            "chmod 2770 " + pc.SPOOL_DIR,
        ]
        self.assertEqual(rendered, expected)
        self.assertEqual(pc.LAUNCHER_SOCKET_DIR, "/run/factory")

    def test_it_is_root_of_the_user_namespace_with_three_capabilities_no_secret_and_only_the_two_directories(self):
        flat = squash(self.init)
        self.assertIn("runAsUser = 0", flat)
        self.assertIn("runAsNonRoot = false", flat)
        self.assertIn("allowPrivilegeEscalation = false", flat)
        self.assertIn("readOnlyRootFilesystem = true", flat)
        self.assertIn('drop = ["ALL"]', flat)
        self.assertIn('add = ["CHOWN", "FOWNER", "FSETID"]', flat)
        for forbidden in ("env", "valueFrom", "privileged", "procMount", "hostPath", "token", "keyvault"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, re.sub(r"\s+", " ", self.init).replace("securityContext", "").replace("readOnlyRootFilesystem", ""))
        self.assertEqual(flat.count("mountPath"), 2)
        self.assertIn("pod_backend_volume_names.socket", flat)
        self.assertIn("pod_backend_volume_names.spool", flat)

    def test_it_comes_after_the_key_vault_init_container_so_the_literal_list_is_unchanged(self):
        self.assertIn("], local.pod_backend_init_containers)", resource("k8s.tf", "kubernetes_manifest", "factory_deployment"))


class TheEnvironmentComesFromTheContract(unittest.TestCase):
    GROUPS = ("manager", "launcher", "relay")

    def test_every_name_of_the_three_groups_is_produced_exactly_once_and_only_through_the_contract(self):
        # variables.tf is left out: a description may name the switch the manager reads.
        outside = "\n".join(code(name) for name in tf_files() if name not in ("locals.tf", "variables.tf"))
        contract_free_locals = code("locals.tf").replace(locals_assignment("locals.tf", "worker_contract"), "")
        everything = outside + contract_free_locals
        for group in self.GROUPS:
            for key, name in pc.SHARED["env"][group].items():
                with self.subTest(group=group, key=key):
                    self.assertEqual(everything.count(f"local.worker_contract.env.{group}.{key},"), 1)
                    self.assertNotIn(name, everything, "an environment name is spelled once, in the contract, never retyped")

    def test_the_pod_group_is_not_produced_by_terraform_at_all(self):
        for name in pc.SHARED["env"]["pod"]:
            if name == "AZMON_COLLECT_ENV":
                continue
            with self.subTest(name=name):
                self.assertNotIn(name, "\n".join(code(file) for file in tf_files() if file != "locals.tf"))

    def test_the_two_ceilings_read_the_one_pool_local(self):
        for key in ("max_pod_sessions",):
            line = [row for row in locals_assignment(BACKEND, "pod_backend_main_env_all").splitlines() if f"env.manager.{key}" in row]
            self.assertEqual(len(line), 1)
            self.assertIn("tostring(local.worker_pool.max_sessions)", line[0])
        line = [row for row in locals_assignment(BACKEND, "pod_backend_containers_all").splitlines() if "env.launcher.max_pods" in row]
        self.assertEqual(len(line), 1)
        self.assertIn("tostring(local.worker_pool.max_sessions)", line[0])

    def test_the_launcher_values_that_have_a_contract_source_read_it(self):
        env = squash(locals_assignment(BACKEND, "pod_backend_containers_all"))
        for key, source_expression in (
            ("namespace", "local.worker_contract.names.worker_namespace"),
            ("repo_cap", "tostring(local.worker_contract.limits.per_repo_cap)"),
            ("deadline_clamp", "tostring(local.worker_contract.timing.deadline_clamp_seconds)"),
            ("allowed_uid", "tostring(local.worker_contract.ids.supervisor_uid)"),
            ("classes", "local.worker_contract.classes.default"),
            ("socket", "local.worker_contract.paths.launcher_socket"),
        ):
            with self.subTest(key=key):
                self.assertIn(f"name = local.worker_contract.env.launcher.{key}, value = {source_expression} }}", env)
        self.assertIn("name = local.worker_contract.env.launcher.api_public_host, value = azurerm_kubernetes_cluster.this.fqdn }", env)
        self.assertIn("name = local.worker_contract.env.launcher.manager_address, value = local.k8s_app_service_fqdn }", env)
        self.assertIn('value = "http://${local.pod_backend_relay_host}:${local.worker_contract.ports.relay}"', env)

    def test_each_group_is_read_by_the_expected_python_modules_and_a_new_reader_needs_this_table_changed(self):
        # "Each name is produced by exactly one Terraform local and read by exactly one Python module" (podcontract.py). The
        # manager group has five readers by design: the switch is read by podexecution and written for the children by the
        # manager, the socket path by the launcher client, FACTORY_MAX_POD_SESSIONS by the pool (slice 18) and the spool and
        # the same names by the pod backend (slice 12b). The launcher and relay groups have exactly one reader each.
        expected = {
            "ENV_LAUNCHER": {"launcher.py"},
            "ENV_RELAY": {"relay.py"},
            "ENV_MANAGER": {"launcher_client.py", "manager.py", "metrics_collect.py", "podbackend.py", "podexecution.py", "podpool.py"},
        }
        found = {name: set() for name in expected}
        for path in sorted(SRC.glob("*.py")):
            if path.name == "podcontract.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                name = node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else None
                if name in found:
                    found[name].add(path.name)
        self.assertEqual(found, expected)


class TheRelayService(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = resource(BACKEND, "kubernetes_service_v1", "factory_relay")

    def test_it_is_gated_cluster_ip_only_and_selects_the_manager_pod_on_the_relay_port(self):
        self.assertRegex(self.service, r"(?m)^\s*count\s*=\s*local\.worker_ns_count\s*$")
        self.assertIn('type     = "ClusterIP"', self.service)
        self.assertIn("selector = local.k8s_selector_labels", self.service)
        self.assertIn("port        = local.worker_contract.ports.relay", self.service)
        self.assertNotIn("LoadBalancer", self.service)
        self.assertNotIn("NodePort", self.service)
        self.assertIn("namespace = kubernetes_namespace_v1.factory.metadata[0].name", self.service)
        for address in ROLES:
            self.assertIn(address, depends_on(self.service))

    def test_the_policy_of_the_worker_namespace_selects_the_same_pod_and_port(self):
        policy = squash(resource("worker_ns.tf", "kubernetes_network_policy_v1", "worker_default_deny"))
        self.assertIn("port = tostring(local.worker_contract.ports.relay)", policy)
        self.assertIn("match_labels = local.k8s_selector_labels", policy)
        self.assertIn('"kubernetes.io/metadata.name" = local.k8s_namespace', policy)

    def test_its_name_cannot_make_kubernetes_inject_the_variable_the_relay_refuses(self):
        # `<SERVICE>_PORT` of a Service named factory-relay is FACTORY_RELAY_PORT: the relay refuses a value of the form tcp://...
        name = re.search(r'pod_backend_relay_service_name\s*=\s*"\$\{local\.k8s_app_name\}(-[\w-]+)"', code(BACKEND)).group(1)
        service = ("factory" + name).upper().replace("-", "_")
        self.assertNotEqual(f"{service}_PORT", pc.ENV_RELAY["port"])
        self.assertNotEqual(f"{service}_PORT_{pc.RELAY_PORT}_TCP_PORT", pc.ENV_RELAY["port"])
        self.assertNotEqual("factory" + name, "factory-relay")
        # And the two layers behind it: the relay sets the name itself, and the Deployment has no service links.
        self.assertIn("env.relay.port, value = tostring(local.worker_contract.ports.relay)", squash(locals_assignment(BACKEND, "pod_backend_containers_all")))
        self.assertRegex(resource("k8s.tf", "kubernetes_manifest", "factory_deployment"), r"enableServiceLinks\s*=\s*false")

    def test_the_relay_url_and_the_manager_address_are_the_service_names_of_the_manager_namespace(self):
        host = backend_local("pod_backend_relay_host")
        self.assertEqual(host, '"${local.pod_backend_relay_service_name}.${local.k8s_namespace}.svc.cluster.local"')
        fqdn = squash(locals_assignment("k8s.tf", "k8s_app_service_fqdn"))
        self.assertEqual(fqdn, '"${local.k8s_app_name}.${local.k8s_namespace}.svc.cluster.local"')


class TheDocuments(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.record = (ROOT / "docs" / "pod-backend.md").read_text(encoding="utf-8")
        cls.readme = (INFRA / "README.md").read_text(encoding="utf-8")
        cls.section = cls.record.split("## Terraform C", 1)[1].split("\n## ", 1)[0]

    def test_the_header_comments_say_what_a_text_test_cannot_show(self):
        for name in (BACKEND, "worker_rbac.tf"):
            raw = (INFRA / name).read_text(encoding="utf-8")
            header = raw.split("\nlocals {", 1)[0]
            flat = squash(header.replace("# ", ""))
            with self.subTest(file=name):
                self.assertIn("OFF by default", flat)
                self.assertIn("UNVERIFIED", flat)
        raw = (INFRA / "worker_rbac.tf").read_text(encoding="utf-8")
        flat = squash(raw.replace("# ", ""))
        for needle in ("kubernetes_network_policy_v1.worker_default_deny", "kubernetes_manifest.worker_policy_pods_binding", "kubernetes_manifest.worker_policy_secrets_binding", "ORDER"):
            self.assertIn(needle, flat)

    def test_the_design_record_has_a_section_a_table_row_and_an_unverified_list_for_the_slices(self):
        flat = squash(self.section)
        for needle in ("worker_rbac.tf", "pod_backend.tf", "UNVERIFIED", "volume-perms", "kube-root-ca.crt", "skip-containers", "manager_pool_taint_enabled", "factory-relay-svc", "SO_PEERCRED"):
            with self.subTest(needle=needle):
                self.assertIn(needle, flat)
        (row,) = [line for line in self.record.splitlines() if line.startswith("| 16, 17 |")]
        self.assertIn("delivered, not wired in", row)
        self.assertIn("`depends_on` `kubernetes_network_policy_v1.worker_default_deny`", row)

    def test_the_infra_readme_lists_the_new_objects_and_the_flip(self):
        flat = squash(self.readme)
        for needle in ("worker_rbac.tf", "pod_backend.tf", "manager_pool_taint_enabled", "factory-relay-svc", "volume-perms"):
            with self.subTest(needle=needle):
                self.assertIn(needle, flat)


if __name__ == "__main__":
    unittest.main()

"""Design guards for the admission policy objects of the pod backend (slice 15b, issues #144 and #145).

`infra/policy/*.json` holds the ValidatingAdmissionPolicy and ValidatingAdmissionPolicyBinding objects, one object per file;
`infra/worker_policy.tf` reads them with `jsondecode(file(...))`. These tests read the files and the .tf as text (standard library
only), so they run in CI as `python3 -I -m unittest discover -s infra/test` with no package installed: the contract is loaded BY
PATH (src/factory/podcontract.py has no relative import) and the rule ids are read out of src/factory/podspec.py with `ast`
(podspec itself has a relative import and cannot be loaded by path).

What is pinned: every literal of the objects (namespace, user name, labels, taint, keys, ports, paths, caps, class maxima, the
image pattern, the environment allow-list) equals the contract; the rule ids of the validations equal `podspec.POLICY_RULES` in
its order; the objects are bound to ONE identity and ONE namespace with `failurePolicy: Fail` and the actions Deny and Audit;
the Terraform side gates every object by `pod_backend_enabled`, depends on both role assignments and takes the image from the
same local as the launcher. What a text test cannot show: that a real API server accepts and evaluates the CEL (run
tools/policy_matrix.py against a kind cluster to replay the golden objects and every mutation; tests/test_policy_cel.py runs
the expressions through an offline model) and that the provider path for the kinds works (a `terraform plan` and a live run).

Run from the repository root:  python3 -I -m unittest discover -s infra/test -v
"""

import ast
import importlib.util
import json
import re
import unittest
from pathlib import Path

INFRA = Path(__file__).resolve().parent.parent
ROOT = INFRA.parent
POLICY_DIR = INFRA / "policy"

_SPEC = importlib.util.spec_from_file_location("podcontract", ROOT / "src" / "factory" / "podcontract.py")
pc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(pc)

FILES = ("pod-policy.json", "pod-binding.json", "secret-policy.json", "secret-binding.json")
ROLES = ("azurerm_role_assignment.aks_cluster_admin", "azurerm_role_assignment.aks_cluster_user")
RULE = re.compile(r"^\[rule:([a-z]+(?:-[a-z]+)*)\] ")
POD_POLICY = "factory-worker-pods"
SECRET_POLICY = "factory-worker-secrets"
PARAM_NAME = "worker-policy-params"


def load(name):
    return json.loads((POLICY_DIR / name).read_text(encoding="utf-8"))


def tf_text():
    return (INFRA / "worker_policy.tf").read_text(encoding="utf-8")


def uncommented(text):
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def block(text, kind, name):
    start = re.search(rf'(?m)^resource "{kind}" "{name}" \{{$', text)
    if start is None:
        raise AssertionError(f"resource {kind}.{name} not found")
    end = re.search(r"(?m)^\}$", text[start.end() :])
    if end is None:
        raise AssertionError(f"resource {kind}.{name} is not closed")
    return text[start.start() : start.end() + end.end()]


def role_bodies():
    """(path, text) of every Role, RoleBinding, ClusterRole and ClusterRoleBinding resource of infra/*.tf."""
    pattern = re.compile(r'(?m)^resource "kubernetes_(?:cluster_)?role(?:_binding)?(?:_v1)?" "[a-z_]+" \{$')
    for path in sorted(INFRA.glob("*.tf")):
        text = uncommented(path.read_text(encoding="utf-8"))
        for found in pattern.finditer(text):
            end = text.find("\n}\n", found.start())  # the last resource of a file has no newline after its brace once comments are dropped
            yield path, text[found.start() : end] if end >= 0 else text[found.start() :]


def rule_ids(name):
    """The tuple POD_RULES or SECRET_RULES of src/factory/podspec.py, read with ast (the module cannot be loaded by path)."""
    tree = ast.parse((ROOT / "src" / "factory" / "podspec.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and [t.id for t in node.targets if isinstance(t, ast.Name)] == [name]:
            return list(ast.literal_eval(node.value))
    raise AssertionError(f"{name} not found in podspec.py")


def local_value(name):
    found = re.search(rf'(?m)^  {name}\s*=\s*"([^"]+)"', (INFRA / "locals.tf").read_text(encoding="utf-8"))
    if found is None:
        raise AssertionError(f"local {name} not found")
    return found.group(1)


def expressions(policy):
    return [validation["expression"] for validation in policy["spec"]["validations"]]


def class_max(key):
    return max(pc.CLASSES.values(), key=lambda row: pc.parse_quantity(row[key]))[key]


class PolicyFiles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = {name: load(name) for name in FILES}
        cls.pods = cls.docs["pod-policy.json"]
        cls.secrets = cls.docs["secret-policy.json"]

    def test_the_directory_holds_exactly_the_four_objects_one_per_file(self):
        self.assertEqual(sorted(path.name for path in POLICY_DIR.iterdir()), sorted(FILES))
        kinds = {name: (doc["apiVersion"], doc["kind"]) for name, doc in self.docs.items()}
        policy = ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicy")
        binding = ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicyBinding")
        self.assertEqual(kinds, {"pod-policy.json": policy, "pod-binding.json": binding, "secret-policy.json": policy, "secret-binding.json": binding})

    def test_the_files_are_canonical_two_space_json_with_one_final_newline(self):
        for name in FILES:
            text = (POLICY_DIR / name).read_text(encoding="utf-8")
            with self.subTest(name=name):
                self.assertEqual(text, f"{json.dumps(json.loads(text), indent=2)}\n")

    def test_the_names_pair_each_binding_with_its_policy(self):
        self.assertEqual(self.pods["metadata"]["name"], POD_POLICY)
        self.assertEqual(self.secrets["metadata"]["name"], SECRET_POLICY)
        self.assertEqual(self.docs["pod-binding.json"]["metadata"]["name"], POD_POLICY)
        self.assertEqual(self.docs["pod-binding.json"]["spec"]["policyName"], POD_POLICY)
        self.assertEqual(self.docs["secret-binding.json"]["metadata"]["name"], SECRET_POLICY)
        self.assertEqual(self.docs["secret-binding.json"]["spec"]["policyName"], SECRET_POLICY)

    def test_the_policies_fail_closed_and_are_bound_to_one_identity(self):
        user = f"system:serviceaccount:{pc.MANAGER_NAMESPACE}:{local_value('k8s_service_account')}"
        self.assertEqual(local_value("k8s_namespace"), pc.MANAGER_NAMESPACE)
        for policy in (self.pods, self.secrets):
            spec = policy["spec"]
            with self.subTest(policy=policy["metadata"]["name"]):
                self.assertEqual(spec["failurePolicy"], "Fail")
                self.assertEqual(spec["matchConditions"], [{"name": "launcher", "expression": f"request.userInfo.username == '{user}'"}])
                self.assertEqual(set(spec), {"failurePolicy", "matchConstraints", "matchConditions", "validations", *({"paramKind"} if policy is self.pods else set())})

    def test_the_policies_match_create_of_the_one_kind_each_and_nothing_else(self):
        for policy, resource in ((self.pods, "pods"), (self.secrets, "secrets")):
            rules = policy["spec"]["matchConstraints"]["resourceRules"]
            with self.subTest(resource=resource):
                self.assertEqual(rules, [{"apiGroups": [""], "apiVersions": ["v1"], "operations": ["CREATE"], "resources": [resource]}])
                self.assertNotIn("matchPolicy", policy["spec"]["matchConstraints"])
                self.assertNotIn("namespaceSelector", policy["spec"]["matchConstraints"])

    def test_the_pod_policy_takes_a_config_map_parameter_and_the_secret_policy_none(self):
        self.assertEqual(self.pods["spec"]["paramKind"], {"apiVersion": "v1", "kind": "ConfigMap"})
        self.assertNotIn("paramKind", self.secrets["spec"])
        pod_binding = self.docs["pod-binding.json"]["spec"]
        self.assertEqual(pod_binding["paramRef"], {"name": PARAM_NAME, "namespace": pc.WORKER_NAMESPACE, "parameterNotFoundAction": "Deny"})
        self.assertNotIn("paramRef", self.docs["secret-binding.json"]["spec"])

    def test_the_bindings_deny_and_audit_in_the_worker_namespace_only(self):
        selector = {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": pc.WORKER_NAMESPACE}}}
        for name in ("pod-binding.json", "secret-binding.json"):
            spec = self.docs[name]["spec"]
            with self.subTest(name=name):
                self.assertEqual(spec["validationActions"], ["Deny", "Audit"])
                self.assertEqual(spec["matchResources"], selector)

    def test_the_rule_ids_are_the_stable_ones_of_the_mirror_in_its_order(self):
        for policy, name in ((self.pods, "POD_RULES"), (self.secrets, "SECRET_RULES")):
            found = []
            for validation in policy["spec"]["validations"]:
                matched = RULE.match(validation["message"])
                self.assertIsNotNone(matched, validation["message"])
                found.append(matched.group(1))
            self.assertEqual(found, rule_ids(name))
        self.assertEqual(len(rule_ids("POD_RULES")) + len(rule_ids("SECRET_RULES")), len(set(rule_ids("POD_RULES")) | set(rule_ids("SECRET_RULES"))))

    def test_every_validation_is_a_forbidden_deny_with_nothing_else(self):
        for policy in (self.pods, self.secrets):
            for validation in policy["spec"]["validations"]:
                with self.subTest(message=validation["message"][:40]):
                    self.assertEqual(set(validation), {"expression", "message", "reason"})
                    self.assertEqual(validation["reason"], "Forbidden")
                    self.assertNotIn("\\", validation["expression"], "a CEL string escape is not used: write a literal dot as [.]")
                    self.assertTrue(validation["expression"].strip())

    def test_the_contract_literals_are_the_ones_of_the_expressions(self):
        text = "\n".join(expressions(self.pods))
        secret_text = "\n".join(expressions(self.secrets))
        prefix = pc.LABEL_PREFIX
        wanted = [
            pc.WORKER_NAMESPACE,
            pc.WORKER_SERVICE_ACCOUNT,
            pc.IMAGE_REFERENCE_PATTERN,
            pc.POD_NAME_PREFIX,
            pc.SECRET_NAME_SUFFIX,
            pc.WORKER_NODE_LABEL_KEY,
            pc.WORKER_NODE_LABEL_VALUE,
            pc.WORKER_TAINT_KEY,
            pc.WORKER_TAINT_VALUE,
            pc.TAINT_EFFECT,
            pc.ANNOTATION_DO_NOT_DISRUPT,
            pc.ANNOTATION_DO_NOT_DISRUPT_VALUE,
            pc.SCRATCH_DIR,
            pc.TMP_DIR,
            pc.SESSION_SECRET_DIR,
            f"'{prefix}",
            f":{pc.RELAY_PORT}$",
            str(pc.DEADLINE_CLAMP_SECONDS + pc.DEADLINE_MARGIN_SECONDS),
            f"terminationGracePeriodSeconds <= {pc.TERMINATION_GRACE_SECONDS}",
            *pc.ENV_POD,
            *(f"'{key}'" for key in pc.LABEL_KEYS),
            *(f"'{phase}'" for phase in pc.PHASES),
            *(f"'{prefix}{key}'" for key in pc.ANNOTATION_KEYS),
        ]
        for literal in wanted:
            with self.subTest(literal=literal):
                self.assertIn(literal, text)
        for literal in (pc.WORKER_NAMESPACE, pc.POD_NAME_PREFIX, pc.SECRET_NAME_SUFFIX, pc.SECRET_KEY_TOKEN, pc.SECRET_KEY_ANTHROPIC):
            with self.subTest(secret_literal=literal):
                self.assertIn(literal, secret_text)

    def test_the_class_maxima_are_the_largest_of_the_contract_table(self):
        text = "\n".join(expressions(self.pods))
        for key, pattern in (
            ("cpu", "'cpu': '{}'"),
            ("memory", "'memory': '{}'"),
            ("ephemeral_storage", "'ephemeral-storage': '{}'"),
        ):
            # once for each of the three container lists (containers, init containers, ephemeral containers)
            self.assertEqual(text.count(pattern.format(class_max(key))), 3, key)
        self.assertIn(f"quantity('{class_max('data_size_limit')}')", text)
        self.assertIn(f"quantity('{class_max('tmp_size_limit')}')", text)

    def test_the_hex_lengths_and_the_label_count_are_the_contract_ones(self):
        text = "\n".join(expressions(self.pods))
        for key, length in pc.LABEL_HEX_LENGTHS.items():
            with self.subTest(key=key):
                self.assertIn(f"^[0-9a-f]{{{length}}}$", text)
        self.assertIn(f"size(object.metadata.labels) == {len(pc.LABEL_KEYS)}", text)
        self.assertIn("size(object.metadata.annotations) == 5", text)

    def test_the_secret_keys_are_exactly_the_two_contract_keys(self):
        text = "\n".join(expressions(self.secrets))
        self.assertIn(f"size(object.data) == 2", text)
        self.assertIn(f"'{pc.SECRET_KEY_TOKEN}' in object.data", text)
        self.assertIn(f"'{pc.SECRET_KEY_ANTHROPIC}' in object.data", text)

    def test_no_expression_reads_a_secret_value_or_the_environment_of_the_request(self):
        for policy in (self.pods, self.secrets):
            for text in expressions(policy):
                self.assertNotIn("oldObject", text)
                self.assertNotIn("request.object", text)

    def test_the_pod_policy_refuses_what_the_design_forbids_by_name(self):
        text = "\n".join(expressions(self.pods))
        for field in ("hostPath", "hostNetwork", "hostPID", "hostIPC", "nodeName", "privileged", "capabilities", "runAsUser", "seccompProfile",
                      "ownerReferences", "initContainers", "ephemeralContainers", "secretKeyRef", "envFrom", "serviceAccountName",
                      "automountServiceAccountToken", "hostUsers", "procMount", "appArmorProfile", "podAntiAffinity", "restartPolicy",
                      "activeDeadlineSeconds", "terminationGracePeriodSeconds", "azure.workload.identity/use"):
            with self.subTest(field=field):
                self.assertIn(field, text)


class TerraformSide(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = tf_text()
        cls.text = uncommented(cls.raw)
        cls.objects = {
            "worker_policy_pods": "pod-policy.json",
            "worker_policy_pods_binding": "pod-binding.json",
            "worker_policy_secrets": "secret-policy.json",
            "worker_policy_secrets_binding": "secret-binding.json",
        }

    def test_each_file_is_read_once_by_exactly_one_manifest(self):
        reads = re.findall(r'jsondecode\(file\("\$\{path\.module\}/policy/([a-z-]+\.json)"\)\)', self.text)
        self.assertEqual(sorted(reads), sorted(FILES))
        for name, file in self.objects.items():
            with self.subTest(resource=name):
                body = block(self.text, "kubernetes_manifest", name)
                self.assertIn(f'manifest = jsondecode(file("${{path.module}}/policy/{file}"))', body)

    def test_every_object_is_gated_by_the_one_variable_and_waits_for_both_role_assignments(self):
        names = [("kubernetes_manifest", name) for name in self.objects] + [("kubernetes_config_map_v1", "worker_policy_params")]
        for kind, name in names:
            with self.subTest(resource=name):
                body = block(self.text, kind, name)
                self.assertIn("count = local.worker_ns_count", body)
                for role in ROLES:
                    self.assertIn(role, body)
        self.assertEqual(len(re.findall(r'(?m)^resource "', self.text)), len(names))

    def test_the_gate_is_the_variable_of_the_namespace_and_nothing_sets_it(self):
        ns = (INFRA / "worker_ns.tf").read_text(encoding="utf-8")
        self.assertIn("worker_ns_count = var.pod_backend_enabled ? 1 : 0", ns)
        for path in INFRA.glob("*.tf*"):
            self.assertNotRegex(path.read_text(encoding="utf-8"), r"(?m)^\s*pod_backend_enabled\s*=\s*true", path.name)

    def test_the_bindings_wait_for_their_policy_and_the_pod_binding_for_the_parameter(self):
        pods = block(self.text, "kubernetes_manifest", "worker_policy_pods_binding")
        self.assertIn("kubernetes_manifest.worker_policy_pods,", pods)
        self.assertIn("kubernetes_config_map_v1.worker_policy_params,", pods)
        secrets = block(self.text, "kubernetes_manifest", "worker_policy_secrets_binding")
        self.assertIn("kubernetes_manifest.worker_policy_secrets,", secrets)

    def test_the_parameter_carries_the_image_of_the_launcher_from_the_same_local(self):
        body = block(self.text, "kubernetes_config_map_v1", "worker_policy_params")
        self.assertRegex(body, r"data = \{\s*image = local\.image\s*\}")
        self.assertIn(f'name      = local.worker_policy_param_name', body)
        self.assertIn(f'worker_policy_param_name = "{PARAM_NAME}"', self.text)
        self.assertIn("namespace = kubernetes_namespace_v1.worker[0].metadata[0].name", body)
        locals_text = (INFRA / "locals.tf").read_text(encoding="utf-8")
        self.assertRegex(locals_text, r'(?m)^  image\s*=\s*"\$\{data\.azurerm_container_registry\.this\.login_server\}/\$\{var\.image_repository\}@\$\{var\.image_digest\}"')

    def test_the_parameter_has_the_three_preconditions_of_the_namespace(self):
        body = block(self.text, "kubernetes_config_map_v1", "worker_policy_params")
        for condition in ('var.network_data_plane == "cilium"', 'var.nap_mode == "Auto"', "var.worker_nodepool_enabled"):
            self.assertIn(condition, body)

    def test_the_header_names_the_order_the_role_binding_must_follow_and_the_unverified_parts(self):
        flat = " ".join(re.sub(r"(?m)^#\s?", "", line) for line in self.raw.splitlines() if line.startswith("#"))
        for needle in ("worker_policy_pods_binding", "worker_policy_secrets_binding", "UNVERIFIED", "failurePolicy: Fail", "request.userInfo.username"):
            self.assertIn(needle, flat)

    def test_any_role_binding_that_names_the_worker_namespace_waits_for_both_policy_bindings(self):
        # Slice 16 added the Role and RoleBinding (worker_rbac.tf); every one that names the worker namespace MUST wait for the fence.
        self.assertTrue(any("worker_launcher" in body for _, body in role_bodies()), "the Role of slice 16 is no longer scanned")
        for path, body in role_bodies():
            if pc.WORKER_NAMESPACE in body or "worker" in body:
                with self.subTest(path=path.name):
                    self.assertIn("kubernetes_manifest.worker_policy_pods_binding", body)
                    self.assertIn("kubernetes_manifest.worker_policy_secrets_binding", body)


class Documentation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.readme = " ".join((INFRA / "README.md").read_text(encoding="utf-8").split())

    def test_the_readme_describes_the_policies_and_the_human_fallback(self):
        for needle in (
            "infra/policy/",
            "kubernetes_manifest.worker_policy_pods",
            "kubernetes_config_map_v1.worker_policy_params",
            "Admission matrix against a real API server",
            "tools/policy_matrix.py",
            "Run the admission matrix on a scratch cluster",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.readme)


if __name__ == "__main__":
    unittest.main()

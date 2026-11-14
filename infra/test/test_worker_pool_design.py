"""Design guards for the default-off cluster and worker-pool variables of infra/ (slice 14 of the pod backend, issue #144).

They read the .tf files as text (standard library only, like check_refs.py), so they run in CI as
`python3 -I -m unittest discover -s infra/test` with no package installed. A text test can check defaults, gating and the
shape of a formula; it cannot prove that a plan shows no line ("update in place", "no line for the Deployment") - only the
real `terraform plan` can, and the pull request body pastes it.

What is pinned here:
  * every new variable is off or unset by default, and no committed *.auto.tfvars file sets one;
  * the cluster arguments are omitted when their variables are null (a `dynamic` block with an empty `for_each`);
  * the NodePool and AKSNodeClass manifests are count-gated, carry the preconditions and both role dependencies, set
    `expireAfter: Never`, `WhenEmpty` with 30 s, and no `terminationGracePeriod`;
  * ONE local (`worker_pool`) feeds the NodePool limit and the ceiling, and the surge-reserve formula, the multiple-of
    check and the family-only sum are what the specification says;
  * `prevent_destroy` is on the cluster and the README documents the deliberate removal;
  * the cluster guard lists both manifests under POOL_OBJECTS.

Run from the repository root, either as discovery or by dotted module name:
    python3 -I -m unittest discover -s infra/test -v
    python3 -m unittest infra.test.test_worker_pool_design -v
"""

import re
import sys
import unittest
from pathlib import Path

# Sibling modules are imported by bare name: put this directory on sys.path so the import also works when the
# module is loaded by dotted name from the repository root (python3 -m unittest infra.test.<module>).
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import check_refs

INFRA = Path(__file__).resolve().parent.parent
ROOT = INFRA.parent

NEW_VARIABLES = {
    "network_data_plane": "null",
    "network_policy": "null",
    "nap_mode": "null",
    "nap_default_node_pools": '"None"',
    "worker_nodepool_enabled": "false",
    "worker_pool_limits_cpu": "32",
    "worker_pool_sku": '"Standard_D4s_v6"',
    "worker_pool_pods_per_node": "1",
    "family_vcpu_quota": "null",
}
MANIFESTS = ("worker_nodeclass", "worker_nodepool")
ROLES = ("azurerm_role_assignment.aks_cluster_admin", "azurerm_role_assignment.aks_cluster_user")


def code(name):
    """One .tf file of infra/ without comment lines."""
    text = (INFRA / name).read_text(encoding="utf-8")
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(("#", "//")))


def block(text, header):
    """The top-level (or nested) block that starts with `header`, braces included."""
    start = text.index(header)
    view = check_refs.blank_view(text)
    return text[start : check_refs.match_brace(view, text.index("{", start))]


def variable(name):
    return block(code("variables.tf"), f'variable "{name}"')


def resource(file, kind, name):
    return block(code(file), f'resource "{kind}" "{name}"')


def local_attribute(text, name):
    """The `{...}` value of the locals attribute `name` (a multi-line object)."""
    found = re.search(rf"(?m)^  {re.escape(name)}\s*=\s*\{{", text)
    if found is None:
        raise AssertionError(f"no locals attribute {name}")
    view = check_refs.blank_view(text)
    return text[found.end() - 1 : check_refs.match_brace(view, found.end() - 1)]


def squash(text):
    return re.sub(r"\s+", " ", text)


def depends_on(text):
    """The text inside `depends_on = [ ... ]` of one resource block."""
    found = re.search(r"depends_on\s*=\s*\[(.*?)\]", text, re.DOTALL)
    if found is None:
        raise AssertionError("the block has no depends_on")
    return found.group(1)


class VariablesAreOffByDefault(unittest.TestCase):
    def test_every_new_variable_exists_with_its_off_default(self):
        for name, default in NEW_VARIABLES.items():
            with self.subTest(variable=name):
                self.assertRegex(variable(name), rf"(?m)^\s*default\s*=\s*{re.escape(default)}\s*$")

    def test_the_accepted_values_are_checked(self):
        pairs = {
            "network_data_plane": '["azure", "cilium"]',
            "nap_mode": '["Auto", "Manual"]',
            "nap_default_node_pools": '["Auto", "None"]',
        }
        for name, accepted in pairs.items():
            with self.subTest(variable=name):
                self.assertIn(f"contains({accepted}", variable(name))
        self.assertIn('== "cilium"', variable("network_policy"))
        # the provider's rule: a Cilium network policy needs the Cilium data plane
        self.assertIn('var.network_policy != "cilium" || var.network_data_plane == "cilium"', variable("network_policy"))

    def test_the_pool_needs_the_quota_and_the_quota_is_checked_when_it_is_set(self):
        self.assertIn("!var.worker_nodepool_enabled || var.family_vcpu_quota != null", variable("worker_nodepool_enabled"))
        self.assertIn("var.family_vcpu_quota == null ||", variable("family_vcpu_quota"))

    def test_the_limit_must_be_a_whole_multiple_of_the_node_vcpus(self):
        text = variable("worker_pool_limits_cpu")
        self.assertIn('var.worker_pool_limits_cpu % tonumber(regex("^Standard_[DE]([0-9]+)", var.worker_pool_sku)[0]) == 0', text)
        self.assertIn("floor(var.worker_pool_limits_cpu) == var.worker_pool_limits_cpu", text)

    def test_the_worker_size_is_a_d_or_e_series_premium_capable_size(self):
        self.assertIn('regex("^Standard_[DE][0-9]+(ds|as|ads|s)_v[0-9]+$", var.worker_pool_sku)', variable("worker_pool_sku"))

    def test_no_committed_tfvars_file_sets_any_new_variable_and_the_example_only_comments_them(self):
        for path in sorted(INFRA.glob("*.auto.tfvars")):
            text = path.read_text(encoding="utf-8")
            for name in NEW_VARIABLES:
                with self.subTest(file=path.name, variable=name):
                    self.assertNotRegex(text, rf"(?m)^\s*{name}\s*=")
        example = (INFRA / "terraform.tfvars.example").read_text(encoding="utf-8")
        for name in NEW_VARIABLES:
            with self.subTest(variable=name):
                self.assertNotRegex(example, rf"(?m)^\s*{name}\s*=")
                self.assertRegex(example, rf"(?m)^#\s*{name}\s*=")

    def test_nothing_in_the_configuration_sets_cilium_or_nap_itself(self):
        for path in sorted(INFRA.glob("*.tf")):
            text = code(path.name)
            with self.subTest(file=path.name):
                self.assertNotRegex(text, r'(?m)^\s*network_data_plane\s*=\s*"')
                self.assertNotRegex(text, r'(?m)^\s*network_policy\s*=\s*"')
                self.assertNotRegex(text, r'(?m)^\s*nap_mode\s*=\s*"')

    def test_the_readme_names_every_new_variable(self):
        readme = (INFRA / "README.md").read_text(encoding="utf-8")
        for name in NEW_VARIABLES:
            with self.subTest(variable=name):
                self.assertIn(f"`{name}`", readme)


class ClusterArgumentsAreOmittedWhenNull(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cluster = resource("aks.tf", "azurerm_kubernetes_cluster", "this")

    def test_the_network_arguments_come_from_the_variables(self):
        profile = block(self.cluster, "network_profile")
        self.assertRegex(profile, r"network_data_plane\s*=\s*var\.network_data_plane\b")
        self.assertRegex(profile, r"network_policy\s*=\s*var\.network_policy\b")

    def test_the_node_provisioning_block_is_dynamic_and_empty_while_nap_mode_is_null(self):
        found = re.findall(r'(?m)^\s*(dynamic\s+)?"?node_provisioning_profile"?\s*\{', self.cluster)
        self.assertEqual(found, ["dynamic "], "node_provisioning_profile must exist only as a dynamic block")
        dynamic = block(self.cluster, 'dynamic "node_provisioning_profile"')
        self.assertIn("for_each = var.nap_mode == null ? [] : [var.nap_mode]", squash(dynamic))
        self.assertIn("mode               = node_provisioning_profile.value", dynamic)
        self.assertIn("default_node_pools = var.nap_default_node_pools", dynamic)

    def test_the_cluster_cannot_be_destroyed_or_replaced(self):
        lifecycle = block(self.cluster, "lifecycle")
        self.assertRegex(lifecycle, r"prevent_destroy\s*=\s*true")

    def test_the_readme_documents_the_deliberate_removal_of_prevent_destroy(self):
        readme = squash((INFRA / "README.md").read_text(encoding="utf-8"))
        self.assertIn("prevent_destroy", readme)
        for phrase in ("scratch copy of `infra/`", "never commit", "test deployment", "re-creat"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, readme)

    def test_the_production_pools_are_one_node_each_because_the_surge_formula_counts_one(self):
        for kind, name in (
            ("azurerm_kubernetes_cluster_node_pool", "factory"),
            ("azurerm_kubernetes_cluster_node_pool", "monitoring"),
        ):
            with self.subTest(pool=name):
                self.assertRegex(resource("aks.tf", kind, name), r"node_count\s*=\s*1\b")
        self.assertRegex(block(self.cluster, "default_node_pool"), r"node_count\s*=\s*1\b")


class TheWorkerPoolObjects(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = code("worker_pool.tf")
        cls.nodepool = resource("worker_pool.tf", "kubernetes_manifest", "worker_nodepool")
        cls.nodeclass = resource("worker_pool.tf", "kubernetes_manifest", "worker_nodeclass")

    def test_the_file_declares_exactly_the_two_manifests_and_nothing_else_that_creates_something(self):
        declared = re.findall(r'(?m)^resource "([a-z0-9_]+)" "([A-Za-z0-9_-]+)"', self.text)
        self.assertEqual(sorted(declared), [("kubernetes_manifest", name) for name in MANIFESTS])
        self.assertNotRegex(self.text, r'(?m)^(?:data|module|ephemeral)\s+"')
        self.assertNotIn("azurerm_role_assignment ", self.text)  # no new identity or permission

    def test_both_objects_are_count_gated_by_the_one_variable(self):
        self.assertRegex(self.text, r"(?m)^  worker_pool_count\s*=\s*var\.worker_nodepool_enabled \? 1 : 0$")
        for name, text in (("nodepool", self.nodepool), ("nodeclass", self.nodeclass)):
            with self.subTest(object=name):
                self.assertRegex(text, r"(?m)^  count\s*=\s*local\.worker_pool_count$")
        self.assertEqual(len(re.findall(r"var\.worker_nodepool_enabled", self.text)), 1)

    def test_both_objects_need_nap_auto_and_the_cilium_data_plane(self):
        for name, text in (("nodepool", self.nodepool), ("nodeclass", self.nodeclass)):
            with self.subTest(object=name):
                preconditions = squash(block(text, "lifecycle"))
                self.assertIn('condition = var.nap_mode == "Auto"', preconditions)
                self.assertIn('condition = var.network_data_plane == "cilium"', preconditions)

    def test_both_objects_wait_for_both_role_assignments_and_the_pool_waits_for_the_class(self):
        for name, text in (("nodepool", self.nodepool), ("nodeclass", self.nodeclass)):
            with self.subTest(object=name):
                for role in ROLES:
                    self.assertIn(role, depends_on(text))
        self.assertIn("kubernetes_manifest.worker_nodeclass", depends_on(self.nodepool))

    def test_the_node_pool_object_has_the_contract_name_labels_taint_and_requirements(self):
        text = squash(self.nodepool)
        self.assertIn('apiVersion = "karpenter.sh/v1" kind = "NodePool"', text)
        self.assertIn("name = local.worker_contract.names.nodepool", text)
        self.assertIn("(local.worker_contract.nodes.worker_label_key) = local.worker_contract.nodes.worker_label_value", text)
        self.assertIn('"kubernetes.azure.com/ebpf-dataplane" = "cilium"', text)
        for attribute in ("worker_taint_key", "worker_taint_value", "taint_effect"):
            with self.subTest(taint=attribute):
                self.assertIn(f"local.worker_contract.nodes.{attribute}", text)
        for requirement in (
            'key = "karpenter.azure.com/sku-name" operator = "In" values = [var.worker_pool_sku]',
            'key = "karpenter.sh/capacity-type" operator = "In" values = ["on-demand"]',
            'key = "kubernetes.io/arch" operator = "In" values = ["amd64"]',
            'key = "kubernetes.io/os" operator = "In" values = ["linux"]',
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, text)
        self.assertIn('group = "karpenter.azure.com" kind = "AKSNodeClass" name = local.worker_contract.names.nodeclass', text)
        self.assertIn('key = "node.cilium.io/agent-not-ready" value = "true" effect = "NoExecute"', text)

    def test_the_node_pool_is_never_rotated_and_empty_nodes_go_after_30_seconds(self):
        text = squash(self.nodepool)
        self.assertIn('expireAfter = "Never"', text)
        self.assertIn('consolidationPolicy = "WhenEmpty" consolidateAfter = "30s"', text)
        self.assertNotIn("WhenEmptyOrUnderutilized", text)

    def test_no_termination_grace_period_and_no_spot_anywhere_in_the_pool_file(self):
        # a NodePool-level grace period bypasses do-not-disrupt; code() already dropped comments
        self.assertNotRegex(self.text, r"(?i)terminationGrace")
        self.assertNotRegex(self.text, r"(?i)\bspot\b")

    def test_the_limit_is_the_one_local_stringified(self):
        self.assertIn("limits = { cpu = tostring(local.worker_pool.limits_cpu) }", squash(self.nodepool))

    def test_the_node_class_is_azure_linux_with_a_kubelet_pids_bound(self):
        text = squash(self.nodeclass)
        self.assertIn('apiVersion = "karpenter.azure.com/v1beta1" kind = "AKSNodeClass"', text)
        self.assertIn('imageFamily = "AzureLinux"', text)
        self.assertNotIn("AzureLinux3", text)  # there is no such image family value
        self.assertRegex(text, r"kubelet = \{ podPidsLimit = [0-9]+ \}")
        self.assertIn("name = local.worker_contract.names.nodeclass", text)

    def test_the_pool_reads_the_contract_local_so_its_tflint_annotation_is_gone(self):
        self.assertIn("local.worker_contract", self.text)
        self.assertNotIn("tflint-ignore", (INFRA / "locals.tf").read_text(encoding="utf-8"))

    def test_the_cluster_guard_lists_both_manifests_as_pool_objects(self):
        guard = (ROOT / "tools" / "infra_cluster_guard.py").read_text(encoding="utf-8")
        for name in MANIFESTS:
            with self.subTest(manifest=name):
                self.assertIn(f'("kubernetes_manifest.{name}", POOL_OBJECTS),', guard)


class OneLocalFeedsTheCeiling(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = code("worker_pool.tf")
        cls.locals_block = block(cls.text, "locals")
        cls.worker_pool = local_attribute(cls.locals_block, "worker_pool")

    def test_the_local_has_exactly_the_four_documented_keys(self):
        keys = re.findall(r"(?m)^\s{4}([a-z_]+)\s*=", self.worker_pool)
        self.assertEqual(keys, ["node_vcpu", "limits_cpu", "pods_per_node", "max_sessions"])

    def test_the_ceiling_is_floor_of_limit_over_node_times_pods_per_node(self):
        text = squash(self.worker_pool)
        self.assertIn("limits_cpu = var.worker_pool_limits_cpu", text)
        self.assertIn("pods_per_node = var.worker_pool_pods_per_node", text)
        self.assertIn(
            "max_sessions = floor(var.worker_pool_limits_cpu / local.vm_size_facts[var.worker_pool_sku].vcpu) * var.worker_pool_pods_per_node",
            text,
        )

    def test_nothing_else_reads_the_limit_or_the_pods_per_node_variable(self):
        # The NodePool limit, the concurrency ceiling and the launcher cap are all derived from local.worker_pool.
        # Strip the local itself and variables.tf (the variables validate each other there); nothing else may read them.
        for path in sorted(INFRA.glob("*.tf")):
            if path.name == "variables.tf":
                continue
            text = code(path.name)
            if path.name == "worker_pool.tf":
                text = text.replace(self.worker_pool, "")
            with self.subTest(file=path.name):
                self.assertNotIn("var.worker_pool_limits_cpu", text)
                self.assertNotIn("var.worker_pool_pods_per_node", text)

    def test_the_pods_per_node_may_not_exceed_the_default_class_and_one_node_must_fit(self):
        nodepool = squash(resource("worker_pool.tf", "kubernetes_manifest", "worker_nodepool"))
        self.assertIn("local.worker_pool.pods_per_node <= local.worker_class_pods_per_node", nodepool)
        self.assertIn("local.worker_pool.max_sessions >= 1", nodepool)
        self.assertIn(
            "worker_class_pods_per_node = local.worker_contract.classes.table[local.worker_contract.classes.default].pods_per_node",
            squash(self.locals_block),
        )

    def test_a_future_ceiling_or_launcher_cap_env_must_be_fed_from_the_local(self):
        # Slice 17 adds FACTORY_MAX_POD_SESSIONS and the launcher's maximum of live pods. The check is the one function below;
        # it is exercised on synthetic text here and runs on every .tf file of infra/ except the contract local itself.
        good = "FACTORY_MAX_POD_SESSIONS = tostring(local.worker_pool.max_sessions)"
        self.assertEqual(ceiling_feeds_problems({"a.tf": good}), [])
        # Slice 17 spells the two names as references into the contract (pod_backend.tf), so the check reads that spelling too.
        contract_good = "{ name = local.worker_contract.env.manager.max_pod_sessions, value = tostring(local.worker_pool.max_sessions) },"
        self.assertEqual(ceiling_feeds_problems({"a.tf": contract_good}), [])
        for bad in (
            "FACTORY_MAX_POD_SESSIONS = tostring(var.worker_pool_limits_cpu)",
            "FACTORY_MAX_POD_SESSIONS = \"8\"",
            "FACTORY_LAUNCHER_MAX_PODS = tostring(local.something_else)",
            "{ name = local.worker_contract.env.manager.max_pod_sessions, value = \"8\" },",
            "{ name = local.worker_contract.env.launcher.max_pods, value = tostring(var.worker_pool_limits_cpu) },",
        ):
            with self.subTest(bad=bad):
                self.assertEqual(len(ceiling_feeds_problems({"a.tf": bad})), 1)
        real = {path.name: code(path.name) for path in sorted(INFRA.glob("*.tf")) if path.name != "locals.tf"}
        real["locals.tf"] = code("locals.tf").replace(local_attribute(code("locals.tf"), "worker_contract"), "")
        self.assertEqual(ceiling_feeds_problems(real), [])


def ceiling_feeds_problems(files):
    """Every assignment of the two ceiling environment names (spelled out, or as a reference into the contract) must read local.worker_pool.max_sessions."""
    found = []
    for name, text in sorted(files.items()):
        for line in text.splitlines():
            named = re.match(r"\s*(FACTORY_MAX_POD_SESSIONS|FACTORY_LAUNCHER_MAX_PODS)\s*=", line)
            referenced = re.search(r"local\.worker_contract\.env\.(manager\.max_pod_sessions|launcher\.max_pods)\b", line)
            if (named or referenced) and "local.worker_pool.max_sessions" not in line:
                found.append(f"{name}: {line.strip()}")
    return found


class TheSurgeReserve(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = code("worker_pool.tf")
        cls.locals_block = squash(block(cls.text, "locals"))
        cls.nodepool = squash(resource("worker_pool.tf", "kubernetes_manifest", "worker_nodepool"))
        found = re.search(r'(?m)^  vm_size_pattern\s*=\s*"([^"\n]+)"', cls.text)
        cls.pattern = re.compile(found.group(1))

    def facts(self, size):
        found = self.pattern.match(size)
        if found is None:
            return None
        return int(found["vcpu"]), f"{found['letters']}{found['feature']}_v{found['version']}"

    def test_the_formula_is_the_limit_plus_twice_the_production_vcpus_of_the_family_against_the_quota(self):
        self.assertIn(
            "worker_pool_vcpu_need = local.worker_pool.limits_cpu + 2 * local.worker_family_production_vcpu",
            self.locals_block,
        )
        self.assertIn(
            "condition = var.family_vcpu_quota != null ? local.worker_pool_vcpu_need <= var.family_vcpu_quota : false",
            self.nodepool,
        )

    def test_the_pool_cannot_be_enabled_without_the_quota(self):
        # a null quota must fail the precondition, never pass it
        self.assertIn("var.family_vcpu_quota != null ?", self.nodepool)
        self.assertTrue(self.nodepool.count(": false") >= 1)

    def test_only_the_production_pools_of_the_workers_family_are_counted(self):
        self.assertIn(
            'if try(local.vm_size_facts[size].family, "") == local.vm_size_facts[var.worker_pool_sku].family',
            self.locals_block,
        )
        self.assertIn('family = "${found.letters}${found.feature}_v${found.version}"', self.locals_block)
        self.assertIn("vcpu = tonumber(found.vcpu)", self.locals_block)

    def test_the_sum_adds_the_vcpus_of_each_pool_and_not_the_number_of_pools(self):
        # Review finding: only the filter and the facts were pinned, so replacing the value after the colon by 1
        # (count the pools, not their vCPUs) left every test green while the surge check stopped protecting the quota.
        self.assertIn(
            "worker_family_production_vcpu = sum(concat([0], [ for size in local.production_pool_sizes : "
            "local.vm_size_facts[size].vcpu if try(local.vm_size_facts[size].family, \"\") == "
            "local.vm_size_facts[var.worker_pool_sku].family ]))",
            self.locals_block,
        )

    def test_the_three_production_pools_are_counted_with_the_monitoring_pool_only_when_it_exists(self):
        self.assertIn("[var.aks_system_pool_vm_size, var.aks_user_pool_vm_size]", self.locals_block)
        self.assertIn("var.monitoring_enabled && var.monitoring_node_pool_vm_size != null", self.locals_block)
        aks = code("aks.tf")
        self.assertIn("count = var.monitoring_enabled && var.monitoring_node_pool_vm_size != null ? 1 : 0", aks)

    def test_a_size_that_does_not_parse_fails_closed(self):
        self.assertIn("production_pools_parse = alltrue([for size in local.production_pool_sizes : local.vm_size_facts[size] != null])", self.locals_block)
        self.assertIn("condition = local.production_pools_parse", self.nodepool)

    def test_the_size_pattern_gives_one_family_to_sizes_that_differ_only_in_their_vcpus(self):
        d4, d8 = self.facts("Standard_D4ds_v5"), self.facts("Standard_D8ds_v5")
        self.assertEqual((d4, d8), ((4, "Dds_v5"), (8, "Dds_v5")))
        self.assertEqual(self.facts("Standard_D2ds_v5"), (2, "Dds_v5"))
        for other in ("Standard_E4ds_v5", "Standard_D4as_v5", "Standard_D4ads_v5", "Standard_D4ds_v4", "Standard_D4s_v5", "Standard_DC4ds_v3"):
            with self.subTest(other=other):
                self.assertNotEqual(self.facts(other)[1], "Dds_v5")
        self.assertIsNone(self.facts("Standard_D4ds"))
        self.assertIsNone(self.facts("D4ds_v5"))

    def test_the_default_worker_size_is_a_dsv6_size_in_a_family_of_its_own(self):
        # Dsv6 or Dsv7, not Ddsv5 (family capped at 65, some zones restricted)
        default = re.search(r'default\s*=\s*"([A-Za-z0-9_]+)"', variable("worker_pool_sku")).group(1)
        self.assertEqual(default, "Standard_D4s_v6")
        self.assertEqual(self.facts(default), (4, "Ds_v6"))
        self.assertNotEqual(self.facts(default)[1], self.facts("Standard_D2ds_v5")[1])
        self.assertNotEqual(self.facts(default)[1], self.facts("Standard_D8ds_v5")[1])
        description = variable("worker_pool_sku")
        for phrase in ("NotAvailableForSubscription", "NOT measured"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, description)

    def configured_figures(self):
        """The numbers of the surge reserve, derived from the defaults of variables.tf and a configuration with the optional pool.

        That configuration runs the system, factory and `mon` pools: the first two are the defaults, the `mon` size is not a
        default (monitoring_node_pool_vm_size is null), so the test assumes the example size of terraform.tfvars.example.
        """
        defaults = {
            name: re.search(r'default\s*=\s*"?([A-Za-z0-9_]+)"?', variable(name)).group(1)
            for name in ("aks_system_pool_vm_size", "aks_user_pool_vm_size", "worker_pool_sku", "worker_pool_limits_cpu", "worker_pool_pods_per_node")
        }
        mon = "Standard_D2ds_v5"
        pools = (defaults["aks_system_pool_vm_size"], defaults["aks_user_pool_vm_size"], mon)
        without_mon = sum(self.facts(size)[0] for size in pools[:2])
        live = sum(self.facts(size)[0] for size in pools)
        limit = int(defaults["worker_pool_limits_cpu"])
        node = self.facts(defaults["worker_pool_sku"])[0]

        return {
            "defaults": defaults,
            "pools": pools,
            "without_mon": without_mon,
            "live": live,
            "limit": limit,
            "node": node,
        }

    def test_with_the_defaults_the_inputs_of_the_surge_reserve_hold(self):
        figures = self.configured_figures()
        defaults, pools, live, limit, node = (figures[key] for key in ("defaults", "pools", "live", "limit", "node"))
        worker_family = self.facts(defaults["worker_pool_sku"])[1]
        self.assertEqual(pools, ("Standard_D2ds_v5", "Standard_D8ds_v5", "Standard_D2ds_v5"))
        # the production pools run 12 vCPUs with the optional mon pool and 10 without it, all of the Dds_v5 family
        self.assertEqual((live, figures["without_mon"]), (12, 10))
        self.assertEqual({self.facts(size)[1] for size in pools}, {"Dds_v5"})
        self.assertEqual(limit // node * int(defaults["worker_pool_pods_per_node"]), 8)
        # the default worker family holds no production vCPU: the family check only needs the limit within the family quota
        self.assertEqual(sum(self.facts(size)[0] for size in pools if self.facts(size)[1] == worker_family), 0)

    def test_the_example_names_the_default_worker_size_and_not_the_capped_family(self):
        # The README no longer carries the quota arithmetic of one subscription (it tells the operator to read az vm list-usage);
        # the example still has to show the default size and the family quota input, and must not recommend the small family.
        example = (INFRA / "terraform.tfvars.example").read_text(encoding="utf-8")
        self.assertIn('# worker_pool_sku           = "Standard_D4s_v6"', example)
        self.assertIn("# family_vcpu_quota         = 350", example)
        self.assertNotIn("D4ds_v5", example, "the example must not recommend the capped family")

    def test_the_readme_says_the_regional_total_is_not_checked(self):
        readme = squash((INFRA / "README.md").read_text(encoding="utf-8"))
        self.assertIn("does **not** check the regional vCPU total (`cores`)", readme)
        self.assertIn("`family_vcpu_quota`", readme)


class TheStagedOrderIsDocumented(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.readme = squash((INFRA / "README.md").read_text(encoding="utf-8"))

    def test_the_four_steps_come_in_the_specified_order(self):
        positions = [self.readme.find(step) for step in ("(1) Cilium", "(2) node auto-provisioning", "(3) NodePool objects", "(4) pod backend")]
        self.assertTrue(all(position >= 0 for position in positions), positions)
        self.assertEqual(positions, sorted(positions))

    def test_the_readme_says_the_values_are_never_set_by_a_pull_request_and_who_applies_what(self):
        for phrase in (
            "never set by a pull request",
            "`stage=cluster`",
            "drain gate",
            "snapshots",
            "scratch deployment",
            "worker_nodepool_enabled",
            "family_vcpu_quota",
            "POOL_OBJECTS",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.readme)


if __name__ == "__main__":
    unittest.main()


class KarpenterEventsDiagnosticSetting(unittest.TestCase):
    """Issue 149: the control-plane log of node auto-provisioning reaches Log Analytics, with one category and only with NAP."""

    def test_it_exists_only_with_nap_and_names_one_category(self):
        text = (INFRA / "observability.tf").read_text(encoding="utf-8")
        block = text[text.index('resource "azurerm_monitor_diagnostic_setting" "karpenter_events"') :]
        self.assertIn('count = var.nap_mode == "Auto" ? 1 : 0', block)
        self.assertIn("target_resource_id         = azurerm_kubernetes_cluster.this.id", block)
        self.assertIn("log_analytics_workspace_id = azurerm_log_analytics_workspace.this.id", block)
        self.assertEqual(block.count("enabled_log"), 1)
        self.assertIn('category = "karpenter-events"', block)


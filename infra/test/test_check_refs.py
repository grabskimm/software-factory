"""Tests for check_refs.py: it must pass the real configuration and fail on each defect.

Run from the repository root, either as discovery or by dotted module name:
    python3 -I -m unittest discover -s infra/test -v
    python3 -m unittest infra.test.test_check_refs -v
"""

import re
import sys
import tempfile
import unittest
from pathlib import Path

# Sibling modules are imported by bare name: put this directory on sys.path so the import also works when the
# module is loaded by dotted name from the repository root (python3 -m unittest infra.test.<module>).
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import check_refs

INFRA = Path(__file__).resolve().parent.parent

CLEAN = {
    "main.tf": '''
variable "used" {
  type = string

  validation {
    condition     = length(var.used) > 0
    error_message = "x"
  }
}

locals {
  name = "n-${var.used}"
}

resource "azurerm_thing" "a" {
  name = local.name
}

output "thing" {
  value = azurerm_thing.a.id
}
'''
}


class Fixture(unittest.TestCase):
    def run_check(self, files):
        with tempfile.TemporaryDirectory() as tmp:
            for name, text in files.items():
                (Path(tmp) / name).write_text(text, encoding="utf-8")
            return check_refs.check(Path(tmp))

    def test_clean_module_passes(self):
        self.assertEqual(self.run_check(CLEAN), [])

    def test_unused_variable_fails(self):
        files = {"main.tf": CLEAN["main.tf"] + '\nvariable "forgotten" {\n  type = string\n}\n'}
        problems = self.run_check(files)
        self.assertEqual(problems, ["variable 'forgotten' is declared but never used"])

    def test_variable_read_only_by_its_own_validation_is_unused(self):
        selfish = 'variable "selfish" {\n  type = string\n  validation {\n    condition = length(var.selfish) > 0\n    error_message = "x"\n  }\n}\n'
        files = {"main.tf": "\n".join([CLEAN["main.tf"], selfish])}
        self.assertEqual(self.run_check(files), ["variable 'selfish' is declared but never used"])

    def test_comment_and_string_text_do_not_count_as_use(self):
        extra = '''
variable "mentioned" {
  type        = string
  description = "see var.mentioned and ${var.used} in a string"
}

# var.mentioned is only a comment here
'''
        # "${var.used}" inside a description string is an interpolation of `used`, not of `mentioned`.
        problems = self.run_check({"main.tf": CLEAN["main.tf"] + extra})
        self.assertEqual(problems, ["variable 'mentioned' is declared but never used"])

    def test_interpolation_counts_as_use(self):
        extra = 'variable "quoted" {\n  type = string\n}\n\nlocals {\n  other = "x-${var.quoted}"\n}\n'
        self.assertEqual(self.run_check({"main.tf": CLEAN["main.tf"] + extra}), [])

    def test_output_with_missing_resource_fails(self):
        extra = 'output "ghost" {\n  value = azurerm_thing.missing.id\n}\n'
        problems = self.run_check({"main.tf": CLEAN["main.tf"] + extra})
        self.assertEqual(len(problems), 1)
        self.assertIn("output 'ghost' refers to missing resource azurerm_thing.missing", problems[0])

    def test_missing_data_source_and_local_fail(self):
        extra = 'output "bad" {\n  value = "${data.azurerm_registry.nope.id}${local.nope}"\n}\n'
        problems = self.run_check({"main.tf": CLEAN["main.tf"] + extra})
        joined = "\n".join(problems)
        self.assertIn("missing data source data.azurerm_registry.nope", joined)
        self.assertIn("undeclared local.nope", joined)

    def test_data_reference_is_not_read_as_a_resource(self):
        extra = (
            'data "azurerm_thing" "d" {\n  name = var.used\n}\n'
            'output "d" {\n  value = data.azurerm_thing.d.id\n}\n'
        )
        self.assertEqual(self.run_check({"main.tf": CLEAN["main.tf"] + extra}), [])

    def test_guid_literal_fails_but_nil_guid_is_allowed(self):
        guid = "123e4567-e89b-12d3-a456-426614174000"
        problems = self.run_check({"main.tf": CLEAN["main.tf"] + f'\nlocals {{\n  sub = "{guid}"\n}}\n'})
        self.assertEqual(len(problems), 1)
        self.assertIn("GUID-shaped literal", problems[0])
        nil = "\nlocals {\n  nil = \"00000000-0000-0000-0000-000000000000\"\n}\n"
        self.assertEqual(self.run_check({"main.tf": CLEAN["main.tf"] + nil}), [])

    def test_guid_in_example_or_readme_fails(self):
        guid = "123e4567-e89b-12d3-a456-426614174000"
        files = dict(CLEAN)
        files["terraform.tfvars.example"] = f'used = "{guid}"\n'
        problems = self.run_check(files)
        self.assertEqual(len(problems), 1)
        self.assertIn("GUID-shaped literal", problems[0])

    def test_long_hex_literal_in_tf_fails(self):
        digest = "a" * 64
        problems = self.run_check({"main.tf": CLEAN["main.tf"] + f'\nlocals {{\n  d = "sha256:{digest}"\n}}\n'})
        self.assertEqual(len(problems), 1)
        self.assertIn("long hex literal", problems[0])

    def test_key_vault_secret_resource_fails(self):
        extra = 'resource "azurerm_key_vault_secret" "s" {\n  name = var.used\n}\n'
        problems = self.run_check({"main.tf": CLEAN["main.tf"] + extra})
        self.assertTrue(any("must not manage Key Vault secret values" in p for p in problems), problems)

    def test_duplicate_resource_fails(self):
        extra = 'resource "azurerm_thing" "a" {\n  name = var.used\n}\n'
        problems = self.run_check({"main.tf": CLEAN["main.tf"] + extra})
        self.assertIn("duplicate resource azurerm_thing.a", problems)

    def test_example_must_assign_every_required_variable(self):
        files = dict(CLEAN)
        files["terraform.tfvars.example"] = "# used = \"commented out does not count\"\n"
        problems = self.run_check(files)
        self.assertEqual(problems, ["terraform.tfvars.example / image.auto.tfvars do not assign required variable 'used'"])

    def test_example_must_not_assign_unknown_variables(self):
        files = dict(CLEAN)
        files["terraform.tfvars.example"] = 'used = "x"\nstray = "y"\n'
        problems = self.run_check(files)
        self.assertEqual(problems, ["terraform.tfvars.example / image.auto.tfvars assign unknown variable 'stray'"])

    def test_variable_with_default_need_not_be_in_the_example(self):
        extra = 'variable "sized" {\n  type    = number\n  default = 4\n}\n\nlocals {\n  s = var.sized\n}\n'
        files = {"main.tf": CLEAN["main.tf"] + extra, "terraform.tfvars.example": 'used = "x"\n'}
        self.assertEqual(self.run_check(files), [])


class KubernetesAdditions(unittest.TestCase):
    """The Kubernetes half of the configuration is held to the same rules as the Azure half."""

    run_check = Fixture.run_check

    def test_missing_kubernetes_resource_fails(self):
        extra = 'output "ghost" {\n  value = kubernetes_namespace_v1.missing.id\n}\n'
        problems = self.run_check({"main.tf": CLEAN["main.tf"] + extra})
        self.assertEqual(len(problems), 1)
        self.assertIn("refers to missing resource kubernetes_namespace_v1.missing", problems[0])

    def test_existing_kubernetes_resource_passes(self):
        extra = (
            'resource "kubernetes_namespace_v1" "ns" {\n  metadata {\n    name = var.used\n  }\n}\n'
            'output "ns" {\n  value = kubernetes_namespace_v1.ns.id\n}\n'
        )
        self.assertEqual(self.run_check({"main.tf": CLEAN["main.tf"] + extra}), [])

    def test_kubernetes_secret_resource_fails(self):
        for kind in ("kubernetes_secret", "kubernetes_secret_v1"):
            with self.subTest(kind=kind):
                extra = f'resource "{kind}" "s" {{\n  data = {{}}\n}}\n'
                problems = self.run_check({"main.tf": CLEAN["main.tf"] + extra})
                self.assertTrue(any("must not manage Kubernetes secret values" in p for p in problems), problems)

    def test_kubernetes_secret_data_source_fails(self):
        extra = 'data "kubernetes_secret_v1" "s" {\n  metadata {\n    name = var.used\n  }\n}\n'
        problems = self.run_check({"main.tf": CLEAN["main.tf"] + extra})
        self.assertTrue(any("must not manage Kubernetes secret values" in p for p in problems), problems)

    def test_ephemeral_secret_block_fails(self):
        extra = 'ephemeral "kubernetes_secret_v1" "s" {\n  metadata {\n    name = var.used\n  }\n}\n'
        problems = self.run_check({"main.tf": CLEAN["main.tf"] + extra})
        self.assertTrue(any("must not manage Kubernetes secret values" in p for p in problems), problems)

    def test_other_kubernetes_kinds_are_fine(self):
        extra = (
            'resource "kubernetes_config_map_v1" "c" {\n  data = {}\n}\n'
            'resource "kubernetes_service_account_v1" "a" {\n  metadata {\n    name = var.used\n  }\n}\n'
        )
        self.assertEqual(self.run_check({"main.tf": CLEAN["main.tf"] + extra}), [])

    def test_manifest_of_kind_secret_fails_in_hcl_and_yaml(self):
        hcl = 'resource "kubernetes_manifest" "m" {\n  manifest = {\n    kind = "Secret"\n  }\n}\n'
        yaml = 'locals {\n  y = yamldecode(<<-EOT\n    kind: Secret\n  EOT\n  )\n}\n'
        for text in (hcl, yaml):
            with self.subTest(text=text):
                problems = self.run_check({"main.tf": CLEAN["main.tf"] + text})
                self.assertTrue(any("must not manage a Kubernetes Secret" in p for p in problems), problems)

    def test_manifest_kinds_that_merely_contain_the_word_are_fine(self):
        text = (
            'resource "kubernetes_manifest" "m" {\n  manifest = {\n    kind = "SecretProviderClass"\n  }\n}\n'
            '# kind = "Secret" in a comment is not a resource\n'
        )
        self.assertEqual(self.run_check({"main.tf": CLEAN["main.tf"] + text}), [])


class PublicConstantGuid(unittest.TestCase):
    """The AKS server application ID is allowed in locals.tf only."""

    run_check = Fixture.run_check

    AKS = "6dae42f8-4368-4678-94ff-3960e28e3630"

    def locals_with_constant(self):
        return f'locals {{\n  aks = "{self.AKS}"\n}}\n'

    def test_allowed_in_locals_tf(self):
        files = {"main.tf": CLEAN["main.tf"], "locals.tf": self.locals_with_constant()}
        self.assertEqual(self.run_check(files), [])

    def test_not_allowed_in_another_tf_file(self):
        problems = self.run_check({"main.tf": CLEAN["main.tf"] + self.locals_with_constant()})
        self.assertEqual(len(problems), 1)
        self.assertIn("GUID-shaped literal", problems[0])

    def test_not_allowed_in_example_or_readme(self):
        for name in ("terraform.tfvars.example", "README.md"):
            with self.subTest(name=name):
                files = {"main.tf": CLEAN["main.tf"], "locals.tf": self.locals_with_constant(), name: f'used = "{self.AKS}"\n'}
                problems = self.run_check(files)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("GUID-shaped literal", problems[0])

    def test_any_other_guid_in_locals_tf_still_fails(self):
        other = "123e4567-e89b-12d3-a456-426614174000"
        files = {"main.tf": CLEAN["main.tf"], "locals.tf": f'locals {{\n  sub = "{other}"\n}}\n'}
        problems = self.run_check(files)
        self.assertEqual(len(problems), 1)
        self.assertIn("GUID-shaped literal", problems[0])


def code(name: str) -> str:
    """One .tf file of infra/ without comment lines."""
    text = (INFRA / name).read_text(encoding="utf-8")
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(("#", "//")))


def block_text(name: str, header: str) -> str:
    """The text of the top-level block that starts with `header` in one .tf file."""
    text = code(name)
    start = text.index(header)
    end = check_refs.match_brace(text, text.index("{", start))
    return text[start:end]


class Design(unittest.TestCase):
    """The AKS design decisions that must not regress silently (spikes/S1.md, the design record)."""

    def test_cluster_has_no_static_credentials_and_uses_entra_rbac(self):
        cluster = block_text("aks.tf", 'resource "azurerm_kubernetes_cluster" "this"')
        for expected in (
            "local_account_disabled            = true",
            "azure_rbac_enabled = true",
            "oidc_issuer_enabled       = true",
            "workload_identity_enabled = true",
            'outbound_type       = "userAssignedNATGateway"',
            'network_plugin_mode = "overlay"',
            "secret_rotation_enabled = false",
            "run_command_enabled = false",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, cluster)

    def test_both_pools_are_azure_linux_with_one_fixed_node(self):
        for header in (
            'resource "azurerm_kubernetes_cluster" "this"',
            'resource "azurerm_kubernetes_cluster_node_pool" "factory"',
        ):
            with self.subTest(header=header):
                text = block_text("aks.tf", header)
                self.assertIn('os_sku', text)
                self.assertIn('"AzureLinux3"', text)
                self.assertIn("node_count", text)
                self.assertRegex(text, r"auto_scaling_enabled\s*=\s*false")
                self.assertNotRegex(text, r"(min_count|max_count)")
        pool = block_text("aks.tf", 'resource "azurerm_kubernetes_cluster_node_pool" "factory"')
        self.assertIn("local.user_pool_labels", pool)
        self.assertIn('pool = "factory"', block_text("locals.tf", "locals"))

    def test_the_container_apps_stack_is_gone(self):
        text = "\n".join(code(path.name) for path in sorted(INFRA.glob("*.tf")))
        for forbidden in (
            "azurerm_container_app",
            "Microsoft.App/",
            "azurerm_private_endpoint",
            "azurerm_private_dns_zone",
            "azurerm_storage_share",
            "NfsAzureFile",
            "authConfigs",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, text)
        self.assertFalse((INFRA / "environment.tf").exists())
        self.assertFalse((INFRA / "app.tf").exists())

    def test_nat_gateway_and_service_endpoints_stay(self):
        network = code("network.tf")
        self.assertIn('resource "azurerm_nat_gateway" "this"', network)
        self.assertIn('resource "azurerm_subnet_nat_gateway_association" "nodes"', network)
        self.assertIn('service_endpoints    = ["Microsoft.Storage", "Microsoft.KeyVault"]', network)
        self.assertIn('output "nat_public_ip"', code("outputs.tf"))

    def test_node_nsg_admits_the_pod_network_before_it_denies_the_internet(self):
        network = code("network.tf")
        for rule in ("allow-pod-network-inbound", "allow-pod-network-outbound", "allow-ingress-inbound"):
            with self.subTest(rule=rule):
                self.assertIn(f'"{rule}"', network)
        # Overlay pod addresses are outside the VNet, so the outbound deny (priority 4000) would
        # swallow pod-to-pod traffic between nodes unless the explicit allow ranks above it.
        priorities = dict(re.findall(r'"([a-z-]+)" = \{\s*priority\s*=\s*([0-9]+)', network))
        self.assertLess(int(priorities["allow-pod-network-outbound"]), int(priorities["deny-other-internet-outbound"]))
        self.assertIn("var.aks_pod_cidr", code("network.tf"))

    def test_vault_and_lock_account_stay_closed(self):
        vault = block_text("keyvault.tf", 'resource "azurerm_key_vault" "this"')
        for expected in ("rbac_authorization_enabled    = true", "purge_protection_enabled      = true", 'default_action             = "Deny"'):
            with self.subTest(expected=expected):
                self.assertIn(expected, vault)
        account = block_text("storage.tf", 'resource "azurerm_storage_account" "locks"')
        for expected in ("shared_access_key_enabled       = false", 'default_action             = "Deny"'):
            with self.subTest(expected=expected):
                self.assertIn(expected, account)

    def test_role_assignments_are_never_broader_than_one_resource_except_the_ingress_address_list_right(self):
        # One reviewed exception: the cloud provider lists the public IPs of the resource group
        # when it binds the ingress Service (infra/ingress.tf), so that single assignment is at
        # resource-group scope, Network Contributor, for the cluster identity only.
        resource_group_exceptions = {"azurerm_role_assignment.cluster_ingress_ip"}
        module = check_refs.Module(INFRA)
        for block in module.of_kind("resource"):
            if block["labels"][0] != "azurerm_role_assignment":
                continue
            with self.subTest(resource=".".join(block["labels"])):
                text = code(block["file"])
                body = text[text.index(f'resource "azurerm_role_assignment" "{block["labels"][1]}"'):]
                body = body[: check_refs.match_brace(body, body.index("{"))]
                scope = re.search(r"scope\s*=\s*(\S+)", body).group(1)
                address = ".".join(block["labels"])
                if address in resource_group_exceptions:
                    self.assertEqual(scope, "data.azurerm_resource_group.this.id")
                    self.assertIn('role_definition_name = "Network Contributor"', body)
                    self.assertIn("azurerm_user_assigned_identity.cluster.principal_id", body)
                else:
                    self.assertNotIn("azurerm_resource_group", scope)
                self.assertNotIn("subscription", scope)
                self.assertNotIn("virtual_network", scope)

    def test_the_workload_identity_trusts_exactly_one_service_account(self):
        credential = block_text("identity.tf", 'resource "azurerm_federated_identity_credential" "workload"')
        self.assertIn("local.workload_identity_subject", credential)
        self.assertIn("azurerm_kubernetes_cluster.this.oidc_issuer_url", credential)
        subject = re.search(r'workload_identity_subject\s*=\s*"([^"]+)"', code("locals.tf")).group(1)
        self.assertEqual(subject, "system:serviceaccount:${local.k8s_namespace}:${local.k8s_service_account}")
        locals_text = code("locals.tf")
        self.assertRegex(locals_text, r'k8s_namespace\s*=\s*"factory"')
        self.assertRegex(locals_text, r'k8s_service_account\s*=\s*"factory"')

    def test_manager_env_selects_entra_jwt_and_keeps_secrets_out(self):
        env = block_text("locals.tf", "app_env = merge(")
        self.assertIn('FACTORY_MANAGER_AUTH    = "entra-jwt"', env)
        self.assertIn("FACTORY_ENTRA_TENANT_ID", env)
        self.assertIn("FACTORY_ENTRA_AUDIENCES", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("GITHUB_APP_PRIVATE_KEY", env)
        self.assertNotIn("secret_version", env)

    def test_container_insights_cannot_copy_the_secret_environment(self):
        text = code("locals.tf")
        self.assertRegex(text, r"\[log_collection_settings\.env_var\]\W+\s*enabled = false")
        self.assertIn('AZMON_COLLECT_ENV = "False"', text)

    def test_scanner_waiver_is_the_first_line_of_aks_tf_and_names_exactly_two_rules(self):
        # The IaC scanner reads `dd-iac-scan disable=` only at the beginning of a file, and a blanket `ignore`,
        # `ignore-block` or `ignore-line` would also hide findings that are not decided (README.md, "Cluster API").
        rules = ("terraform-azure-aks-private-cluster-disabled", "terraform-azure-aks-network-policy-misconfigured")
        expected = f"# dd-iac-scan disable={','.join(rules)}"
        self.assertEqual((INFRA / "aks.tf").read_text(encoding="utf-8").splitlines()[0], expected)
        for path in sorted(INFRA.glob("*.tf")):
            directives = [line for line in path.read_text(encoding="utf-8").splitlines() if "dd-iac-scan" in line]
            with self.subTest(file=path.name):
                self.assertEqual(directives, [expected] if path.name == "aks.tf" else [])

    def test_scanner_waiver_holds_only_while_the_api_server_is_public_and_the_policy_is_a_variable(self):
        # The waiver is the answer to a Medium finding. It must go the day the cluster becomes private, and the
        # network policy stays the operator's staged choice (variables.tf) rather than a literal in aks.tf.
        cluster = block_text("aks.tf", 'resource "azurerm_kubernetes_cluster" "this"')
        self.assertNotRegex(cluster, r"private_cluster_enabled\s*=\s*true")
        self.assertIn('dynamic "api_server_access_profile"', cluster)
        self.assertRegex(cluster, r"network_policy\s*=\s*var\.network_policy")
        self.assertIn("aks_api_authorized_ip_ranges", cluster)


class RealConfiguration(unittest.TestCase):
    def test_infra_is_clean(self):
        self.assertEqual(check_refs.check(INFRA), [])

    def test_the_check_actually_sees_the_configuration(self):
        module = check_refs.Module(INFRA)
        self.assertGreaterEqual(len(module.of_kind("variable")), 35)
        self.assertGreaterEqual(len(module.of_kind("resource")), 24)
        self.assertGreaterEqual(len(module.of_kind("output")), 8)
        names = {name for block in module.of_kind("locals") for name in check_refs.local_names(block["body"])}
        self.assertIn("names", names)
        self.assertIn("repo_config_files", names)
        self.assertIn("kv_secret_objects", names)
        self.assertIn("app_env", names)
        providers = {tuple(block["labels"]) for block in module.of_kind("provider")}
        self.assertEqual(providers, {("azurerm",), ("azapi",), ("kubernetes",)})


if __name__ == "__main__":
    unittest.main()

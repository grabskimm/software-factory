"""Design guards for the Kubernetes half of infra/ (k8s.tf, ingress.tf).

They read the .tf files as text (standard library only, like check_refs.py) and fail
when a decision recorded in spikes/S1.md or in the design record is changed by accident:
the three pod relaxations and nothing more, one replica with Recreate, the Key Vault files kept
out of the Factory container, no Secret objects in Terraform and Caddy's header handling.

Run from the repository root, either as discovery or by dotted module name:
    python3 -I -m unittest discover -s infra/test -v
    python3 -m unittest infra.test.test_workload_design -v
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


def attribute_block(text: str, header: str) -> str:
    """The balanced `{...}` or `[...]` that follows the first `header` in `text`."""
    start = text.index(header) + len(header)
    # A header that already ends with the opening bracket ("name = [") is matched from there.
    opener = start - 1 if header[-1] in "{[" else next(i for i in range(start, len(text)) if text[i] in "{[")
    close = {"{": "}", "[": "]"}[text[opener]]
    depth = 0
    for i in range(opener, len(text)):
        if text[i] == text[opener]:
            depth += 1
        elif text[i] == close:
            depth -= 1
            if depth == 0:
                return text[opener : i + 1]
    raise AssertionError(f"unbalanced after {header!r}")


class FactoryDeployment(unittest.TestCase):
    """spikes/S1.md: exactly three pod relaxations, applied through kubernetes_manifest."""

    @classmethod
    def setUpClass(cls):
        cls.k8s = code("k8s.tf")
        cls.deployment = block_text("k8s.tf", 'resource "kubernetes_manifest" "factory_deployment"')
        cls.containers = attribute_block(cls.deployment, "containers = concat([")
        cls.init_containers = attribute_block(cls.deployment, "initContainers = concat([")
        cls.main = cls.containers

    def test_the_deployment_is_a_manifest_because_the_typed_resource_cannot_say_it(self):
        self.assertNotIn('resource "kubernetes_deployment', self.k8s)
        self.assertNotIn('resource "kubernetes_pod', self.k8s)
        self.assertNotIn('resource "kubernetes_stateful_set', self.k8s)

    def test_the_three_relaxations_are_present(self):
        self.assertRegex(self.deployment, r"hostUsers\s*=\s*false")
        self.assertRegex(self.main, r'procMount\s*=\s*"Unmasked"')
        self.assertRegex(self.main, r'appArmorProfile\s*=\s*\{\s*type\s*=\s*"Unconfined"\s*\}')

    def test_nothing_else_is_relaxed_or_added_to_the_container(self):
        security = attribute_block(self.main, "securityContext =")
        self.assertEqual(sorted(re.findall(r"^\s*([A-Za-z]+)\s*=", security, re.M)), ["appArmorProfile", "procMount"])
        for forbidden in ("privileged", "capabilities", "allowPrivilegeEscalation", "seccompProfile", "readOnlyRootFilesystem", "runAs"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.deployment)
        # No pod-level securityContext either: fsGroup would walk /data, and the entrypoint chowns it.
        self.assertNotRegex(self.deployment, r"(?m)^\s*securityContext\s*=.*\n(?:(?!^\s*containers).)*fsGroup")
        self.assertNotIn("fsGroup", self.deployment)
        self.assertNotIn("hostNetwork", self.deployment)
        self.assertNotIn("hostPID", self.deployment)
        self.assertNotIn("hostPath", self.deployment)
        self.assertNotIn("enableWeakerNestedSandbox", self.k8s)

    def test_the_metrics_listener_is_on_and_reachable_only_with_monitoring_on_behind_a_token(self):
        # Rewritten for the monitoring phase of issue 48. The old design (loopback only, no token) is still
        # what an unconfigured deployment gets; the new one is: bind the pod address, declare the port, a
        # ClusterIP Service, a bearer token from Key Vault, and no route in the ingress.
        env = block_text("locals.tf", "app_env = merge(")
        self.assertRegex(env, r"FACTORY_MANAGER_METRICS_PORT\s*=\s*tostring\(local\.metrics_port\)")
        self.assertRegex(code("locals.tf"), r"(?m)^\s*metrics_port\s*=\s*9464\s*$")
        self.assertNotIn("FACTORY_MANAGER_METRICS_HOST", env, "the host is only set with monitoring on, from the pod address")
        host = attribute_block(code("k8s.tf"), "app_env_metrics_list = ")
        self.assertIn("var.monitoring_enabled", code("k8s.tf")[code("k8s.tf").index("app_env_metrics_list = ") :][:60])
        self.assertIn('name = "FACTORY_MANAGER_METRICS_HOST"', host)
        self.assertIn('fieldPath = "status.podIP"', host)
        self.assertNotIn("0.0.0.0", host)
        for name in ("locals.tf", "ingress.tf", "variables.tf", "monitoring.tf", "outputs.tf"):
            with self.subTest(name=name):
                self.assertNotIn("FACTORY_MANAGER_METRICS_HOST", code(name))
        # The token is a Key Vault secret, only with monitoring on, delivered like the other two (the synced
        # Secret, as an environment variable), so the manager cannot start the wide listener without one.
        locals_text = code("locals.tf")
        objects = locals_text[locals_text.index("kv_secret_objects = merge(") :]
        objects = objects[: objects.index("\n  )")]
        self.assertRegex(objects, r"var\.monitoring_enabled \? \{\s*metrics_token = \{")
        self.assertIn('k8s_secret_key = "FACTORY_MANAGER_METRICS_TOKEN"', objects)
        self.assertIn("local.kv_secret_names.metrics_token", objects)
        # The literal port is spelled once; no other file hard-codes it, and the ingress knows nothing of it.
        for name in ("k8s.tf", "ingress.tf", "variables.tf", "monitoring.tf", "outputs.tf", "network.tf"):
            with self.subTest(port=name):
                self.assertNotIn("9464", code(name))
        ingress = code("ingress.tf")
        for forbidden in ("metrics_port", "-metrics", "metrics.", "9464", "k8s_metrics_service_fqdn"):
            with self.subTest(ingress=forbidden):
                self.assertNotIn(forbidden, ingress)
        # The container declares the port only with monitoring on, and only that one extra port.
        k8s_text = code("k8s.tf")
        ports = k8s_text[k8s_text.index("app_container_ports = ") :]
        ports = ports[: ports.index("\n  )")]
        self.assertEqual(re.findall(r"containerPort\s*=\s*([^,}\s]+)", ports), ["local.app_port", "local.metrics_port"])
        self.assertRegex(ports, r"var\.monitoring_enabled \? \[\{ name = \"metrics\"")
        self.assertRegex(self.main, r"ports\s*=\s*local\.app_container_ports")
        self.assertNotIn("containerPort", self.main)
        self.assertIn("local.app_env_list, local.app_env_secret_list, local.app_env_metrics_list, local.app_env_optional_list", self.main)

    def test_the_metrics_service_is_cluster_ip_only_and_exists_only_with_monitoring_on(self):
        service = block_text("k8s.tf", 'resource "kubernetes_service_v1" "factory_metrics"')
        self.assertRegex(service, r"count\s*=\s*local\.monitoring_count")
        self.assertIn('type     = "ClusterIP"', service)
        for forbidden in ("LoadBalancer", "NodePort", "external", "annotations", "load_balancer"):
            self.assertNotIn(forbidden, service)
        self.assertIn("selector = local.k8s_selector_labels", service)
        self.assertIn("port        = local.metrics_port", service)
        self.assertIn('name      = "${local.k8s_app_name}-metrics"', service)

    def test_the_committed_ingress_input_is_one_valid_custom_host(self):
        text = (INFRA / "ingress.auto.tfvars").read_text(encoding="utf-8")
        assigned = re.findall(r"(?m)^([a-z_]+)\s*=\s*(.+)$", text)
        self.assertEqual([name for name, _ in assigned], ["ingress_custom_host"])
        host = re.fullmatch(r'"([^"]+)"', assigned[0][1].strip())
        self.assertIsNotNone(host, "a quoted host name")
        # the same rules the variable's validation enforces (variables.tf)
        self.assertRegex(host.group(1), r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?$")
        self.assertFalse(host.group(1).endswith(".cloudapp.azure.com"))
        self.assertIn("ingress_custom_host", code("variables.tf"))
        # and it is tracked on purpose: infra/.gitignore ignores every other *.tfvars
        ignore = (INFRA / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("!ingress.auto.tfvars", ignore)

    def test_one_replica_that_is_recreated_on_the_user_pool(self):
        self.assertRegex(self.deployment, r"replicas\s*=\s*1\b")
        self.assertRegex(self.deployment, r'strategy\s*=\s*\{\s*type\s*=\s*"Recreate"\s*\}')
        self.assertNotIn("RollingUpdate", self.deployment)
        self.assertIn("nodeSelector       = local.user_pool_labels", self.deployment)

    def test_requests_equal_limits_from_the_sizing_local(self):
        resources = attribute_block(self.main, "resources =")
        self.assertIn("requests = { cpu = local.k8s_workload_sizing.cpu, memory = local.k8s_workload_sizing.memory }", resources)
        self.assertIn("limits   = { cpu = local.k8s_workload_sizing.cpu, memory = local.k8s_workload_sizing.memory }", resources)
        self.assertIn("terminationGracePeriodSeconds = local.k8s_workload_sizing.termination_grace_period_seconds", self.deployment)

    def test_service_links_are_off_so_a_service_named_factory_adds_no_variables(self):
        self.assertRegex(self.deployment, r"enableServiceLinks\s*=\s*false")

    def test_probes_are_exec_on_loopback_never_http_get(self):
        self.assertNotIn("httpGet", self.k8s)
        for probe in ("startupProbe", "livenessProbe", "readinessProbe"):
            with self.subTest(probe=probe):
                self.assertRegex(attribute_block(self.main, f"{probe} ="), r"exec\s*=\s*\{\s*command\s*=\s*local\.app_health_command")
        command = attribute_block(code("k8s.tf"), "app_health_command = [")
        self.assertIn("http://127.0.0.1:${local.app_port}/healthz", command)
        self.assertIn("/opt/factory/bin/python", command)

    def test_key_vault_files_are_mounted_only_by_the_init_container(self):
        self.assertIn("local.k8s_secrets_mount_path", self.init_containers)
        self.assertNotIn("local.k8s_secrets_mount_path", self.main)
        self.assertEqual(self.deployment.count("local.k8s_secrets_mount_path"), self.init_containers.count("local.k8s_secrets_mount_path"))
        self.assertNotIn('name = "keyvault"', self.main)
        self.assertIn("secrets-store.csi.k8s.io", self.deployment)
        self.assertRegex(self.deployment, r"secretProviderClass\s*=\s*local\.k8s_secret_provider_class")

    def test_secrets_reach_the_container_only_as_environment_from_the_synced_secret(self):
        env_secret = attribute_block(code("k8s.tf"), "app_env_secret_list = [")
        self.assertIn("secretKeyRef", env_secret)
        self.assertIn("local.k8s_synced_secret", env_secret)
        self.assertIn("local.app_env_list, local.app_env_secret_list", self.main)
        self.assertNotIn("valueFrom", self.main)  # spelled out only in the local above

    def test_optional_environment_goes_at_the_end_of_the_list_never_into_the_sorted_middle(self):
        # The kubernetes_manifest provider fills the planned object from the prior state BY LIST POSITION. A variable inserted into the
        # sorted list shifts every later entry, and where an index changes from a fieldRef entry to a secretKeyRef entry the apply sends
        # an invalid Deployment (seen 2026-11-09, entra_spa_scope). So the optional variables are a separate list, concatenated last.
        locals_text = code("locals.tf")
        app_env = locals_text[locals_text.index("app_env = merge(") :]
        app_env = app_env[: app_env.index("\n  )")]
        optional = locals_text[locals_text.index("app_env_optional = merge(") :]
        optional = optional[: optional.index("\n  )")]
        for name in ("FACTORY_TOKEN_BROKER", "FACTORY_ENTRA_SPA_CLIENT_ID", "FACTORY_ENTRA_SPA_SCOPE"):
            with self.subTest(name=name):
                self.assertNotIn(name, app_env)
                self.assertIn(name, optional)
        # Slice 17 appends the environment of the pod backend AFTER the optional variables (empty while the backend is off).
        self.assertRegex(self.main, r"env\s*=\s*concat\([^)]*local\.app_env_metrics_list,\s*local\.app_env_optional_list,\s*local\.pod_backend_main_env\)")
        self.assertIn("app_env_optional_list = [for name in sort(keys(local.app_env_optional))", code("k8s.tf"))

    def test_the_rollout_inputs_are_in_the_pod_template(self):
        self.assertIn("local.deploy_inputs_sha256", self.deployment)
        self.assertIn("kubernetes_config_map_v1.repos.metadata[0].name", self.deployment)

    def test_the_provider_does_not_wait_for_the_rollout(self):
        # A `wait { rollout = true }` makes kubernetes_manifest fail with "Provider produced
        # inconsistent result after apply" on every image promotion (the Deployment controller
        # bumps deployment.kubernetes.io/revision while the provider waits); reproduced on a real
        # cluster. Check the rollout yourself after an apply (infra/README.md, "First apply").
        self.assertNotRegex(self.deployment, r"wait\s*\{")

    def test_the_deployment_waits_for_the_insights_config_and_the_secret_provider_class(self):
        depends = attribute_block(self.deployment, "depends_on =")
        for expected in (
            "kubernetes_config_map_v1.insights_agent",
            "kubernetes_manifest.secret_provider_class",
            "azurerm_kubernetes_cluster_node_pool.factory",
            "azurerm_role_assignment.key_vault_secrets_user",
            "azurerm_role_assignment.locks_blob_data_contributor",
            "azurerm_role_assignment.acr_pull",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, depends)


class FactorySupportObjects(unittest.TestCase):
    def test_secret_provider_class_pins_versions_and_uses_workload_identity(self):
        spc = block_text("k8s.tf", 'resource "kubernetes_manifest" "secret_provider_class"')
        self.assertIn('provider = "azure"', spc)
        self.assertIn('usePodIdentity = "false"', spc)
        self.assertIn("azurerm_user_assigned_identity.workload.client_id", spc)
        self.assertIn("objectVersion = local.kv_secret_objects[key].version", spc)
        self.assertNotIn('objectVersion = ""', spc)
        self.assertIn("secretObjects", spc)
        self.assertIn("local.k8s_synced_secret", spc)

    def test_service_account_carries_the_workload_identity_annotation_and_no_api_token(self):
        account = block_text("k8s.tf", 'resource "kubernetes_service_account_v1" "factory"')
        self.assertIn('"azure.workload.identity/client-id" = azurerm_user_assigned_identity.workload.client_id', account)
        self.assertIn("name      = local.k8s_service_account", account)
        self.assertRegex(account, r"automount_service_account_token\s*=\s*false")

    def test_storage_class_keeps_the_disk_and_binds_late(self):
        storage = block_text("k8s.tf", 'resource "kubernetes_storage_class_v1" "factory_disk"')
        for expected in (
            'storage_provisioner    = "disk.csi.azure.com"',
            'reclaim_policy         = "Retain"',
            'volume_binding_mode    = "WaitForFirstConsumer"',
            "allow_volume_expansion = true",
            'skuName = "Premium_LRS"',
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, storage)

    def test_the_data_claim_is_a_single_writer_disk_that_terraform_will_not_replace(self):
        claim = block_text("k8s.tf", 'resource "kubernetes_persistent_volume_claim_v1" "data"')
        self.assertIn('access_modes       = ["ReadWriteOnce"]', claim)
        self.assertRegex(claim, r"wait_until_bound\s*=\s*false")
        self.assertRegex(claim, r"prevent_destroy\s*=\s*true")
        self.assertIn("local.k8s_workload_sizing.data_disk_size", claim)

    def test_repository_config_is_an_immutable_configmap_not_a_secret(self):
        config = block_text("k8s.tf", 'resource "kubernetes_config_map_v1" "repos"')
        self.assertIn("data      = local.repo_config_data", config)
        self.assertRegex(config, r"immutable\s*=\s*true")
        self.assertRegex(config, r"create_before_destroy\s*=\s*true")
        self.assertIn('"/config/repos"', code("locals.tf"))
        self.assertIn("local.config_repos_mount_path", block_text("k8s.tf", 'resource "kubernetes_manifest" "factory_deployment"'))

    def test_insights_agent_configmap_comes_from_the_contract_local(self):
        config = block_text("k8s.tf", 'resource "kubernetes_config_map_v1" "insights_agent"')
        for expected in ("local.k8s_insights_agent_config.name", "local.k8s_insights_agent_config.namespace", "local.k8s_insights_agent_config.data"):
            with self.subTest(expected=expected):
                self.assertIn(expected, config)

    def test_the_service_is_cluster_ip_only(self):
        service = block_text("k8s.tf", 'resource "kubernetes_service_v1" "factory"')
        self.assertIn('type     = "ClusterIP"', service)
        self.assertNotIn("LoadBalancer", service)

    def test_every_kubernetes_resource_waits_for_the_cluster_admin_roles(self):
        module = check_refs.Module(INFRA)
        direct = {}
        refs = {}
        for block in module.of_kind("resource"):
            kind, name = block["labels"][:2]
            if not kind.startswith("kubernetes_"):
                continue
            body = block["body"]
            address = f"{kind}.{name}"
            depends = re.search(r"depends_on\s*=\s*\[(.*?)\]", body, re.S)
            text = depends.group(1) if depends else ""
            direct[address] = "azurerm_role_assignment.aks_cluster_admin" in text and "azurerm_role_assignment.aks_cluster_user" in text
            refs[address] = set(re.findall(r"\b(kubernetes_[a-z0-9_]+\.[A-Za-z_][\w-]*)", body)) - {address}
        self.assertGreaterEqual(len(direct), 12)

        def reaches(address, seen=()):
            if direct[address]:
                return True
            return any(reaches(other, seen + (address,)) for other in refs[address] if other in direct and other not in seen)

        for address in sorted(direct):
            with self.subTest(address=address):
                self.assertTrue(reaches(address), f"{address} does not depend, directly or through another Kubernetes object, on both role assignments")

    def test_terraform_declares_no_kubernetes_secret_values(self):
        for path in sorted(INFRA.glob("*.tf")):
            text = path.read_text(encoding="utf-8")
            with self.subTest(file=path.name):
                self.assertNotRegex(text, r'"kubernetes_secret')
                self.assertNotRegex(text, r'kind\s*=\s*"Secret"\s*$')


class Ingress(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ingress = code("ingress.tf")
        start = cls.ingress.index("caddyfile = join(")
        cls.caddyfile = cls.ingress[start : cls.ingress.index("ingress_node_selector", start)]

    def test_caddy_removes_every_easy_auth_principal_header(self):
        for header in ("X-MS-CLIENT-PRINCIPAL", "X-MS-CLIENT-PRINCIPAL-ID", "X-MS-CLIENT-PRINCIPAL-NAME", "X-MS-CLIENT-PRINCIPAL-IDP"):
            with self.subTest(header=header):
                self.assertIn(f'request_header -{header}"', self.caddyfile)

    def manager_routes(self):
        """The Caddyfile text that handles the manager: the whole file minus the Grafana handler (monitoring on)."""
        routes = attribute_block(self.ingress, "caddy_routes = var.monitoring_enabled ? [")
        off = self.ingress[self.ingress.index("] : [", self.ingress.index("caddy_routes = ")) :]
        off = off[: off.index("\n  ]") + 4]
        on_manager = routes[routes.index('"\\thandle {"') :]
        return self.caddyfile.replace(routes, "") + off + on_manager

    def test_caddy_forwards_authorization_unchanged(self):
        manager = self.manager_routes()
        self.assertNotRegex(manager, r"(?i)-authorization")
        self.assertNotRegex(manager, r"(?i)header_up")
        self.assertNotRegex(manager, r"(?i)\bbasic_?auth\b|forward_auth|\bpath\b")
        self.assertIn("reverse_proxy ${local.k8s_app_service_fqdn}:${local.app_port}", manager)
        # Monitoring off (the default): the site is exactly what it was before, one reverse_proxy and no handle.
        off = self.ingress[self.ingress.index("] : [", self.ingress.index("caddy_routes = ")) :]
        self.assertNotIn("handle", off)
        self.assertNotIn("grafana", off.lower())

    def test_caddy_uses_lets_encrypt_only_and_no_admin_api(self):
        self.assertIn("acme_ca https://acme-v02.api.letsencrypt.org/directory", self.caddyfile)
        self.assertIn("admin off", self.caddyfile)
        # one site block; its address is the host list (the Azure name first, then the optional custom name)
        self.assertIn("${local.ingress_site_address} {", self.caddyfile)
        self.assertIn("ingress_hosts        = compact([local.ingress_fqdn, local.ingress_custom_host])", code("locals.tf"))

    def test_the_image_is_pinned_by_digest_through_the_local(self):
        deployment = block_text("ingress.tf", 'resource "kubernetes_deployment_v1" "caddy"')
        self.assertIn("image             = local.ingress_image", deployment)
        self.assertEqual(re.search(r'ingress_image\s*=\s*"([^"]+)"', code("locals.tf")).group(1), "${var.caddy_image_repository}@${var.caddy_image_digest}")

    def test_the_caddy_pod_meets_the_restricted_pod_security_level(self):
        deployment = block_text("ingress.tf", 'resource "kubernetes_deployment_v1" "caddy"')
        for expected in (
            "run_as_non_root = true",
            'type = "RuntimeDefault"',
            "allow_privilege_escalation = false",
            "read_only_root_filesystem  = true",
            'drop = ["ALL"]',
            'name  = "net.ipv4.ip_unprivileged_port_start"',
            "automount_service_account_token = false",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, deployment)
        # The container-level flag is spelled out too: the typed resource would send false.
        start = deployment.index("allow_privilege_escalation")
        container = deployment[start : deployment.index("volume_mount", start)]
        self.assertIn("run_as_non_root            = true", container)
        namespace = block_text("ingress.tf", 'resource "kubernetes_namespace_v1" "ingress"')
        self.assertIn('"pod-security.kubernetes.io/enforce" = "restricted"', namespace)
        self.assertNotRegex(deployment, r"\bprivileged\s*=")
        self.assertNotIn("host_network", deployment)

    def test_the_public_ip_is_static_standard_with_the_dns_label_and_in_the_resource_group(self):
        address = block_text("ingress.tf", 'resource "azurerm_public_ip" "ingress"')
        for expected in (
            'allocation_method   = "Static"',
            'sku                 = "Standard"',
            "domain_name_label   = var.ingress_dns_label",
            "resource_group_name = data.azurerm_resource_group.this.name",
            "local.names.ingress_public_ip",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, address)

    def test_the_cluster_identity_may_list_and_use_the_address_in_its_resource_group(self):
        # The cloud provider lists the public IPs of the resource group when it reconciles the
        # Service; a role scoped to the single address would leave the Service pending.
        role = block_text("ingress.tf", 'resource "azurerm_role_assignment" "cluster_ingress_ip"')
        self.assertIn("scope                = data.azurerm_resource_group.this.id", role)
        self.assertNotIn("azurerm_public_ip.ingress.id", role)
        self.assertIn('role_definition_name = "Network Contributor"', role)
        self.assertIn("azurerm_user_assigned_identity.cluster.principal_id", role)

    def test_the_load_balancer_service_binds_the_static_address(self):
        service = block_text("ingress.tf", 'resource "kubernetes_service_v1" "caddy"')
        for expected in (
            '"service.beta.kubernetes.io/azure-load-balancer-resource-group" = data.azurerm_resource_group.this.name',
            '"service.beta.kubernetes.io/azure-pip-name"',
            "azurerm_public_ip.ingress.name",
            'type                    = "LoadBalancer"',
            'external_traffic_policy = "Local"',
            "load_balancer_ip        = azurerm_public_ip.ingress.ip_address",
            "azurerm_role_assignment.cluster_ingress_ip",
            "port        = 80",
            "port        = 443",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, service)
        # Only the two public ports; the manager's own port is never exposed here.
        self.assertNotIn("8080", service)

    def test_source_ranges_come_from_the_same_list_as_the_nsg_and_are_omitted_when_empty(self):
        service = block_text("ingress.tf", 'resource "kubernetes_service_v1" "caddy"')
        self.assertIn("length(local.ingress_lb_source_ranges) == 0 ? null : local.ingress_lb_source_ranges", service)
        self.assertIn("for prefix in local.ingress_source_prefixes : prefix if prefix != \"Internet\"", self.ingress)
        self.assertIn("local.ingress_source_prefixes", code("network.tf"))

    def test_caddy_state_survives_a_restart(self):
        deployment = block_text("ingress.tf", 'resource "kubernetes_deployment_v1" "caddy"')
        self.assertIn("kubernetes_persistent_volume_claim_v1.caddy_data", deployment)
        self.assertRegex(deployment, r'type = "Recreate"')
        self.assertIn('"checksum/caddyfile" = sha256(local.caddyfile)', deployment)


class Outputs(unittest.TestCase):
    def test_the_operator_gets_the_url_and_the_address(self):
        outputs = code("outputs.tf")
        self.assertIn('output "manager_url"', outputs)
        self.assertIn('output "ingress_public_ip"', outputs)
        self.assertIn('output "nat_public_ip"', outputs)
        self.assertIn("https://${local.ingress_fqdn}", outputs)  # manager_url stays the Azure name


def hcl_regex(text: str) -> str:
    """The pattern of an HCL string literal: `\\\\.` in the file is the regular expression `\\.`."""
    return text.replace("\\\\", "\\")


class CustomHost(unittest.TestCase):
    """Issue 26, browser sign-in: an optional host name of your own beside the Azure name, both served and certified."""

    @classmethod
    def setUpClass(cls):
        cls.variables = code("variables.tf")
        cls.locals = code("locals.tf")
        cls.outputs = code("outputs.tf")
        cls.variable = block_text("variables.tf", 'variable "ingress_custom_host"')

    def test_the_variable_defaults_to_none_so_nothing_changes_until_it_is_set(self):
        self.assertRegex(self.variable, r"default\s*=\s*null")
        self.assertIn("type        = string", self.variable)
        self.assertIn("ingress_custom_host == null ? \"\" : var.ingress_custom_host", self.locals)
        # with it unset the host list is the Azure name alone, so the Caddyfile text is what it was
        self.assertIn("compact([local.ingress_fqdn, local.ingress_custom_host])", self.locals)
        self.assertIn('join(", ", local.ingress_hosts)', self.locals)

    def test_the_validation_accepts_a_real_host_and_refuses_everything_else(self):
        pattern = re.search(r'regex\("([^"]+)", coalesce\(var\.ingress_custom_host', self.variable).group(1)
        regex = re.compile(hcl_regex(pattern))
        for good in ("factory.example.org", "a.b.example.org", "xn--bcher-kva.example.org", "f1.example.io"):
            with self.subTest(good=good):
                self.assertTrue(regex.search(good), good)
        for bad in ("Factory.Example.Org", "factory", "*.example.org", "factory.example.org:443", "factory.example.org.", "a_b.example.org", "https://x.example.org", "-a.example.org", "x.1com", "a..example.org", "", "x y.example.org", "factory.example.org/ui"):
            with self.subTest(bad=bad):
                self.assertIsNone(regex.search(bad), bad)
        azure = re.search(r'regex\("([^"]*cloudapp[^"]*)", coalesce', self.variable).group(1)
        self.assertTrue(re.search(hcl_regex(azure), "x.westeurope.cloudapp.azure.com"))
        self.assertIsNone(re.search(hcl_regex(azure), "factory.example.org"))

    def test_caddy_serves_the_one_site_under_every_host_and_obtains_each_certificate_itself(self):
        ingress = code("ingress.tf")
        self.assertEqual(ingress.count('"${local.ingress_site_address} {",'), 1)
        self.assertNotIn("ingress_custom_host", ingress)  # the Caddyfile is generated from the host list only
        self.assertNotIn("dns ", ingress.split("caddyfile = join(")[1].split("ingress_node_selector")[0])  # no DNS-challenge module: HTTP-01 only
        self.assertNotIn("tls {", ingress)
        self.assertNotIn("trusted_proxies", ingress)  # Caddy overwrites X-Forwarded-Proto itself: the manager believes it only from the cluster
        self.assertIn("local.ingress_primary_host", code("monitoring.tf"))
        self.assertNotIn("domain        = local.ingress_fqdn", code("monitoring.tf"))

    def test_the_operator_gets_every_redirect_uri_for_every_host(self):
        self.assertIn('output "dashboard_spa_redirect_uris"', self.outputs)
        self.assertIn('[for host in local.ingress_hosts : "https://${host}/ui/"]', self.outputs)
        self.assertIn('[for host in local.ingress_hosts : "https://${host}/grafana/login/generic_oauth"]', self.outputs)
        self.assertIn('output "ingress_hosts"', self.outputs)
        self.assertIn('output "custom_host_dns_target"', self.outputs)
        self.assertIn('"https://${local.ingress_primary_host}/ui/"', self.outputs)
        self.assertIn('output "manager_url"', self.outputs)
        # the host root is not a redirect URI of the page (it sets its own), so it is never listed
        spa = block_text("outputs.tf", 'output "dashboard_spa_redirect_uris"')
        self.assertNotRegex(spa, r'https://\$\{host\}"')

    def test_the_page_settings_reach_the_manager_only_when_they_are_set(self):
        for name, variable in (("FACTORY_ENTRA_SPA_CLIENT_ID", "entra_spa_client_id"), ("FACTORY_ENTRA_SPA_SCOPE", "entra_spa_scope")):
            with self.subTest(name=name):
                self.assertIn(f"var.{variable} == null ? {{}} : {{ {name} = var.{variable} }}", self.locals)
                self.assertRegex(block_text("variables.tf", f'variable "{variable}"'), r"default\s*=\s*null")
        self.assertIn("FACTORY_ENTRA_AUDIENCES = join(\",\", var.entra_allowed_audiences)", self.locals)  # the audience handling is unchanged

    def test_no_secret_is_added(self):
        for name in ("ingress_custom_host", "entra_spa_client_id", "entra_spa_scope"):
            self.assertNotIn("sensitive", block_text("variables.tf", f'variable "{name}"'))
        self.assertNotIn("secret", self.variable.lower().replace("secrets", ""))


class Providers(unittest.TestCase):
    def test_the_kubernetes_provider_authenticates_with_kubelogin_and_the_azure_cli_session(self):
        providers = code("providers.tf")
        provider = block_text("providers.tf", 'provider "kubernetes"')
        self.assertIn('command     = "kubelogin"', provider)
        self.assertIn('"azurecli"', provider)
        self.assertIn("local.aks_aad_server_app_id", provider)
        self.assertNotIn("client_certificate", providers)
        self.assertNotIn("client_key", providers)
        self.assertNotIn("token ", provider)


class ClusterAccessAndMonitoring(unittest.TestCase):
    """Plan/apply parity of the role assignments, and the Container Insights rule."""

    def test_cluster_roles_come_only_from_the_variable_never_from_the_caller(self):
        # A for_each keyed by the caller's object ID gives the read-only plan identity and the
        # apply identity different addresses, and the apply job's fingerprint gate then refuses
        # every apply.
        for name in ("aks_cluster_admin", "aks_cluster_user"):
            with self.subTest(name=name):
                block = block_text("aks.tf", f'resource "azurerm_role_assignment" "{name}"')
                self.assertIn("for_each = var.aks_cluster_admin_principal_ids", block)
        for name in ("aks.tf", "locals.tf", "network.tf", "identity.tf", "k8s.tf", "ingress.tf"):
            with self.subTest(file=name):
                self.assertNotIn("azurerm_client_config", code(name))

    def test_the_admin_principal_variable_is_required_and_non_empty(self):
        variable = block_text("variables.tf", 'variable "aks_cluster_admin_principal_ids"')
        self.assertNotRegex(variable, r"\bdefault\s*=")
        self.assertIn("length(var.aks_cluster_admin_principal_ids) > 0", variable)

    def test_the_vm_sizes_must_be_premium_storage_capable(self):
        user = block_text("variables.tf", 'variable "aks_user_pool_vm_size"')
        system = block_text("variables.tf", 'variable "aks_system_pool_vm_size"')
        user_regex = re.search(r'regex\("([^"]+)", var\.aks_user_pool_vm_size\)', user).group(1)
        system_regex = re.search(r'regex\("([^"]+)", var\.aks_system_pool_vm_size\)', system).group(1)
        for size in ("Standard_D8ds_v5", "Standard_D8s_v5", "Standard_D8as_v5", "Standard_D8ads_v5", "Standard_E8ds_v5"):
            with self.subTest(size=size):
                self.assertRegex(size, user_regex)
        for size in ("Standard_D8_v5", "Standard_D8d_v5", "Standard_D8a_v4", "Standard_D8a_v5"):
            with self.subTest(size=size):
                self.assertNotRegex(size, user_regex)
        self.assertRegex("Standard_D2ds_v5", system_regex)
        self.assertNotRegex("Standard_D2_v5", system_regex)
        self.assertNotRegex("Standard_D2d_v5", system_regex)

    def test_container_insights_has_a_data_collection_rule_and_association(self):
        rule = block_text("observability.tf", 'resource "azurerm_monitor_data_collection_rule" "insights"')
        self.assertIn("local.names.insights_dcr", rule)
        self.assertIn('extension_name = "ContainerInsights"', rule)
        self.assertIn("azurerm_log_analytics_workspace.this.id", rule)
        locals_ = code("locals.tf")
        self.assertIn('insights_dcr         = "MSCI-${var.location}-aks-${var.name_prefix}"', locals_)
        streams = attribute_block(code("observability.tf"), "insights_streams = [")
        self.assertIn("Microsoft-ContainerLogV2", streams)
        # That stream carries every container's environment variables, secrets included.
        self.assertNotIn("ContainerInventory", streams)
        association = block_text("observability.tf", 'resource "azurerm_monitor_data_collection_rule_association" "insights"')
        self.assertIn("azurerm_kubernetes_cluster.this.id", association)
        self.assertIn("azurerm_monitor_data_collection_rule.insights.id", association)


class ClusterGuardDocumentation(unittest.TestCase):
    """infra/README.md ("Safe deployments") is where an operator learns about the cluster guard: what it refuses, which label
    and attestation lift it and how the flip is done. The repository ships no workflow that runs the guard (an operator's
    own pipeline runs `tools/infra_cluster_guard.py`), so what is pinned is the text that the guard's own messages cite."""

    @classmethod
    def setUpClass(cls):
        cls.readme = (INFRA / "README.md").read_text(encoding="utf-8")

    def section(self, start: str, end: str) -> str:
        # the needles are phrases, not line breaks: the README may be re-wrapped
        return " ".join(self.readme[self.readme.index(start) : self.readme.index(end)].split())

    def test_the_readme_documents_the_guard_the_label_the_attestation_and_the_flip_procedure(self):
        safe = self.section("## Safe deployments", "## Roll back")
        for needle in (
            "**Cluster guard.**",
            "tools/infra_cluster_guard.py",
            "`CLUSTER`",
            "`POOL_OBJECTS`",
            "azurerm_kubernetes_cluster.this",
            "azurerm_kubernetes_cluster_node_pool.factory",
            "azurerm_kubernetes_cluster_node_pool.monitoring",
            "`cluster_snapshots_taken`",
            "`infra-cluster-change`",
            "label `infra-cluster-change`",
            "drain gate",
            # the flip procedure: the values live in INFRA_TFVARS, are set just before the manual run, and stay set
            "live in `INFRA_TFVARS`, not in a pull request",
            "immediately before the manual `stage=cluster`",
            "**stays set**",
            "no action on the Deployment",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, safe)
        self.assertIn("36. **Cluster guard", self.readme[self.readme.index("## UNVERIFIED") :])

    def test_the_first_apply_names_the_stages_the_guard_reads_and_the_four_target_lines(self):
        # the former needle "the four `-target` lines are as before" lived in a workflow description; the stages are now
        # defined in "First apply", which is also where the addresses of stage=cluster are listed (tests/test_infra_cluster_guard.py)
        first = self.section("## First apply", "## Call the API")
        for needle in (
            "`tools/infra_cluster_guard.py` (`GUARD_STAGE`)",
            "`cluster` is the four `-target` lines here",
            "`all` is a plain apply of everything",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, first)

    def test_the_guard_reads_the_label_the_readme_documents(self):
        tool = (ROOT / "tools" / "infra_cluster_guard.py").read_text(encoding="utf-8")
        label = re.search(r'(?m)^LABEL = "([^"]+)"$', tool).group(1)
        self.assertEqual(label, "infra-cluster-change")
        self.assertIn(f"`{label}`", self.section("## Safe deployments", "## Roll back"))


if __name__ == "__main__":
    unittest.main()

"""Design guards for the monitoring stack (monitoring.tf, monitoring/, the ingress route, the identities).

Standard library only, like the other infra tests: they read the .tf, .tftpl and .json files as text. They fail
when a decision of issue 48 is changed by accident: everything is off by default, each component reads one
secret of its own, nothing is anonymous, the ingress knows only the /grafana/ path, every image is pinned, every
metric an alert or a dashboard uses is in the catalogue (docs/metrics.md), and no webhook is ever invented.

Run from the repository root, either as discovery or by dotted module name:
    python3 -I -m unittest discover -s infra/test -v
    python3 -m unittest infra.test.test_monitoring_design -v
"""

import configparser
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

import check_refs
from test_workload_design import attribute_block, block_text, code

INFRA = Path(__file__).resolve().parent.parent
ROOT = INFRA.parent
MON = INFRA / "monitoring"

# Series that come from the platform and not from the manager; each must be kept by the cAdvisor scrape.
PLATFORM_METRICS = {
    "up",
    "container_cpu_usage_seconds_total",
    "container_memory_working_set_bytes",
    "container_spec_memory_limit_bytes",
    "container_spec_cpu_quota",
    "container_spec_cpu_period",
    "container_oom_events_total",
    "container_start_time_seconds",
    # kube-state-metrics (issue 149); each must be kept by its scrape job.
    "kube_pod_created",
    "kube_pod_status_phase",
    "kube_pod_status_unschedulable",
    "kube_pod_status_ready",
    "kube_pod_status_ready_time",
    "kube_pod_status_reason",
    "kube_node_labels",
    "kube_customresource_nodeclaim_created_timestamp_seconds",
    "kube_customresource_nodeclaim_condition_last_transition_timestamp_seconds",
}
PROMQL_WORDS = {"by", "without", "on", "ignoring", "group_left", "group_right", "bool", "and", "or", "unless"}


def call_arguments(text: str, header: str) -> str:
    """The text between the parentheses of the call that starts with `header` (which ends with its opening one)."""
    start = text.index(header) + len(header)
    depth = 1
    for position, character in enumerate(text[start:], start):
        depth += {"(": 1, ")": -1}.get(character, 0)
        if depth == 0:
            return text[start:position]
    raise AssertionError(f"unbalanced after {header!r}")


def catalogue_names() -> set:
    """Every metric name of the catalogue tables of docs/metrics.md."""
    text = (ROOT / "docs" / "metrics.md").read_text(encoding="utf-8")
    return set(re.findall(r"(?m)^\| `(factory_[a-z0-9_]+)` \|", text))


def base_name(name: str) -> str:
    return re.sub(r"_(bucket|sum|count)$", "", name) if name.startswith("factory_") else name


def identifiers(expr: str) -> set:
    """The metric names an expression selects (a name before `{`, `[`, `)` or a space, not a label or a string)."""
    stripped = re.sub(r'"[^"]*"', '""', expr)
    stripped = re.sub(r"\b(by|without)\s*\([^)]*\)", " ", stripped)
    found = set(re.findall(r"\b((?:factory|container|kube)_[a-z0-9_]+)\b", stripped))
    if re.search(r"\bup\s*\{", stripped) or re.search(r"\babsent\(up\b", stripped):
        found.add("up")
    return found


def rule_expressions() -> list:
    """The ALERTING rules (name, rule). The recording rules of issue 149 are in recording_rules()."""
    document = json.loads((MON / "alert-rules.json").read_text(encoding="utf-8"))
    return [(rule["alert"], rule) for group in document["groups"] for rule in group["rules"] if "alert" in rule]


def recording_rules() -> list:
    document = json.loads((MON / "alert-rules.json").read_text(encoding="utf-8"))
    return [(rule["record"], rule) for group in document["groups"] for rule in group["rules"] if "record" in rule]


def dashboards() -> dict:
    return {path.name: json.loads(path.read_text(encoding="utf-8")) for path in sorted((MON / "dashboards").glob("*.json"))}


def dashboard_expressions() -> list:
    found = []
    for name, board in dashboards().items():
        for panel in board["panels"]:
            for target in panel.get("targets", []):
                found.append((f"{name}: {panel['title']}", target["expr"]))
        for variable in board["templating"]["list"]:
            found.append((f"{name}: variable {variable['name']}", variable["definition"]))
    return found


class OffByDefault(unittest.TestCase):
    """The default deployment is unchanged: no monitoring object, identity, role, node or secret exists."""

    def test_the_switch_defaults_to_false_and_the_receiver_to_none(self):
        variables = code("variables.tf")
        self.assertRegex(block_text("variables.tf", 'variable "monitoring_enabled"'), r"default\s*=\s*false")
        self.assertRegex(block_text("variables.tf", 'variable "alert_receiver"'), r'default\s*=\s*"none"')
        self.assertRegex(block_text("variables.tf", 'variable "factory_network_policy_enabled"'), r"default\s*=\s*false")
        self.assertRegex(block_text("variables.tf", 'variable "monitoring_node_pool_vm_size"'), r"default\s*=\s*null")
        for name in ("metrics_token_secret_version", "grafana_oauth_client_id", "grafana_oauth_client_secret_version", "alert_receiver_secret_version"):
            with self.subTest(variable=name):
                self.assertRegex(block_text("variables.tf", f'variable "{name}"'), r"default\s*=\s*null")
        self.assertIn("monitoring_enabled needs metrics_token_secret_version, grafana_oauth_client_id and grafana_oauth_client_secret_version", variables)
        self.assertIn("alert_receiver other than none needs monitoring_enabled and alert_receiver_secret_version", variables)

    def test_every_monitoring_object_is_counted_or_keyed_by_the_switch(self):
        module = check_refs.Module(INFRA)
        names = set()
        for block in module.of_kind("resource"):
            if block["file"] not in ("monitoring.tf", "identity.tf", "aks.tf", "k8s.tf"):
                continue
            kind, name = block["labels"][:2]
            if "monitoring" not in name and name not in {"factory_metrics", "factory_network_policy"} and block["file"] != "monitoring.tf":
                continue
            names.add(f"{kind}.{name}")
            with self.subTest(resource=f"{kind}.{name}"):
                self.assertRegex(
                    block["body"],
                    r"(?m)^\s*(count\s*=\s*(local\.monitoring_count|local\.alerting_enabled \? 1 : 0|var\.monitoring_enabled && [^\n]+ \? 1 : 0)|for_each\s*=\s*local\.monitoring_components)\s*$",
                )
        self.assertGreaterEqual(len(names), 20, sorted(names))
        self.assertIn("kubernetes_namespace_v1.monitoring", names)
        self.assertIn("azurerm_kubernetes_cluster_node_pool.monitoring", names)

    def test_components_are_empty_while_monitoring_is_off_and_alertmanager_needs_a_receiver(self):
        text = code("monitoring.tf")
        self.assertIn('if var.monitoring_enabled && (name != "alertmanager" || local.alerting_enabled)', text)
        self.assertIn('alerting_enabled         = var.monitoring_enabled && var.alert_receiver != "none"', text)
        for resource in ("kubernetes_config_map_v1\" \"alertmanager_config", "kubernetes_manifest\" \"alertmanager", "kubernetes_service_v1\" \"alertmanager"):
            with self.subTest(resource=resource):
                self.assertRegex(block_text("monitoring.tf", f'resource "{resource}"'), r"count\s*=\s*local\.alerting_enabled \? 1 : 0")

    def test_switching_monitoring_off_leaves_the_factory_pod_and_the_caddyfile_alone(self):
        locals_ = code("locals.tf")
        self.assertIn("var.monitoring_enabled ? { metrics_token_version = var.metrics_token_secret_version } : {}", locals_)
        ingress = code("ingress.tf")
        self.assertRegex(ingress, r"caddy_routes = var\.monitoring_enabled \? \[")


class Secrets(unittest.TestCase):
    """Three Key Vault secrets, each read by one identity of its own; no Kubernetes Secret, no value, no webhook."""

    def test_each_component_has_its_own_identity_and_a_role_on_one_secret_only(self):
        identity = code("identity.tf")
        for name in ("azurerm_user_assigned_identity", "azurerm_federated_identity_credential", "azurerm_role_assignment"):
            self.assertRegex(identity, rf'resource "{name}" "monitoring(_secret_reader)?"')
        role = block_text("identity.tf", 'resource "azurerm_role_assignment" "monitoring_secret_reader"')
        self.assertIn('scope                = "${azurerm_key_vault.this.id}/secrets/${each.value.secret_name}"', role)
        self.assertIn('role_definition_name = "Key Vault Secrets User"', role)
        credential = block_text("identity.tf", 'resource "azurerm_federated_identity_credential" "monitoring"')
        self.assertIn('subject                   = "system:serviceaccount:${local.k8s_monitoring_namespace}:${each.key}"', credential)
        inputs = attribute_block(code("monitoring.tf"), "monitoring_secret_inputs = ")
        for component, secret in (("prometheus", "metrics_token"), ("grafana", "grafana_oauth_client_secret"), ("alertmanager", "alert_receiver_url")):
            with self.subTest(component=component):
                self.assertRegex(inputs, rf"{component}\s*=\s*\{{ secret_name = local\.kv_secret_names\.{secret},")
        self.assertEqual(len(re.findall(r"secret_name\s*=", inputs)), 3, "one secret per component")

    def test_the_secret_names_are_declared_in_one_place(self):
        names = attribute_block(code("locals.tf"), "kv_secret_names = ")
        for expected in ('metrics_token               = "metrics-bearer-token"', 'grafana_oauth_client_secret = "grafana-oauth-client-secret"', 'alert_receiver_url          = "alert-receiver-url"'):
            self.assertIn(expected, names)

    def test_secrets_are_files_in_the_pod_that_reads_them_and_never_a_kubernetes_secret(self):
        text = code("monitoring.tf")
        self.assertNotIn("secretObjects", text)
        self.assertNotRegex(text, r'"kubernetes_secret|kind\s*=\s*"Secret"')
        self.assertEqual(text.count("secrets-store.csi.k8s.io"), 3)
        self.assertNotIn("secretKeyRef", text)
        self.assertNotIn("valueFrom", text)
        self.assertIn("objectVersion = each.value.version", text)
        grafana = block_text("monitoring.tf", 'resource "kubernetes_manifest" "grafana"')
        self.assertIn('name = "GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET__FILE"', grafana)
        self.assertNotIn("CLIENT_SECRET\"", grafana.replace("CLIENT_SECRET__FILE", ""))
        prometheus_config = (MON / "prometheus.yml.tftpl").read_text(encoding="utf-8")
        self.assertIn("credentials_file: /mnt/secrets/${token_file}", prometheus_config)
        self.assertNotRegex(prometheus_config, r"(?m)^\s*(credentials|bearer_token|password):")

    def test_a_secret_version_is_in_the_rollout_checksum_of_the_pod_that_reads_it(self):
        for resource, version in (("prometheus", "metrics_token_secret_version"), ("grafana", "grafana_oauth_client_secret_version"), ("alertmanager", "alert_receiver_secret_version")):
            with self.subTest(resource=resource):
                self.assertIn(f"var.{version}", block_text("monitoring.tf", f'resource "kubernetes_manifest" "{resource}"'))

    def test_no_destination_is_invented(self):
        everything = "\n".join(path.read_text(encoding="utf-8") for path in [*(found for found in MON.rglob("*") if found.is_file()), *INFRA.glob("*.tf"), INFRA / "terraform.tfvars.example"])
        for forbidden in ("hooks.slack.com", "office.com", "webhook.site", "pagerduty.com", "opsgenie", "discord"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, everything)
        receiver = (MON / "alertmanager.yml.tftpl").read_text(encoding="utf-8")
        self.assertEqual(sorted(re.findall(r"(\w+_file): /mnt/secrets/\$\{url_file\}", receiver)), ["api_url_file", "url_file", "webhook_url_file"])
        self.assertNotRegex(receiver, r"https?://")
        self.assertNotRegex(receiver, r"(?m)^\s*(api_url|url|webhook_url):")


class Workloads(unittest.TestCase):
    """Pod Security restricted, pinned images, resources everywhere, ClusterIP only."""

    @classmethod
    def setUpClass(cls):
        cls.text = code("monitoring.tf")
        cls.deployments = {name: block_text("monitoring.tf", f'resource "kubernetes_manifest" "{name}"') for name in ("prometheus", "grafana", "alertmanager")}

    def test_the_namespace_enforces_the_restricted_level(self):
        namespace = block_text("monitoring.tf", 'resource "kubernetes_namespace_v1" "monitoring"')
        self.assertIn('"pod-security.kubernetes.io/enforce" = "restricted"', namespace)
        for expected in ("azurerm_role_assignment.aks_cluster_admin", "azurerm_role_assignment.aks_cluster_user"):
            self.assertIn(expected, namespace)
        security = attribute_block(self.text, "monitoring_container_security = ")
        for expected in ("allowPrivilegeEscalation = false", "readOnlyRootFilesystem   = true", 'capabilities             = { drop = ["ALL"] }'):
            self.assertIn(expected, security)
        pod = attribute_block(self.text, "monitoring_pod_security = ")
        self.assertIn("runAsNonRoot   = true", pod)
        self.assertIn('seccompProfile = { type = "RuntimeDefault" }', pod)

    def test_every_workload_is_non_root_with_a_numeric_user_limited_and_not_host_connected(self):
        for name, deployment in self.deployments.items():
            with self.subTest(deployment=name):
                self.assertIn("local.monitoring_pod_security", deployment)
                self.assertRegex(deployment, r"runAsUser\s*=\s*\d+")
                self.assertIn("securityContext = local.monitoring_container_security", deployment)
                self.assertRegex(deployment, r"limits\s*=\s*\{ cpu")
                self.assertRegex(deployment, r"requests\s*=\s*\{ cpu")
                self.assertRegex(deployment, r"strategy\s*=\s*\{ type = \"Recreate\" \}")
                self.assertRegex(deployment, r"enableServiceLinks\s*=\s*false")
                for forbidden in ("hostNetwork", "hostPID", "hostPath", "privileged", "hostUsers", "procMount", "Unconfined"):
                    self.assertNotIn(forbidden, deployment)
        # Only Prometheus holds the Kubernetes API token (it reads node metrics).
        self.assertRegex(self.deployments["prometheus"], r"automountServiceAccountToken\s*=\s*true")
        self.assertRegex(self.deployments["grafana"], r"automountServiceAccountToken\s*=\s*false")
        self.assertRegex(self.deployments["alertmanager"], r"automountServiceAccountToken\s*=\s*false")

    def test_images_come_from_variables_that_refuse_a_floating_tag_and_are_pinned_by_digest(self):
        for name, variable in (("prometheus", "prometheus_image"), ("grafana", "grafana_image"), ("alertmanager", "alertmanager_image")):
            with self.subTest(image=name):
                self.assertIn(f"image           = var.{variable}", self.deployments[name])
                block = block_text("variables.tf", f'variable "{variable}"')
                self.assertIn("never latest", block)
                default = re.search(r'default\s*=\s*"([^"]+)"', block).group(1)
                self.assertRegex(default, r"^docker\.io/[a-z/]+:v?[0-9]+\.[0-9]+\.[0-9]+$")
                pinned = re.search(rf'(?m)^{variable}\s*=\s*"([^"]+)"', (INFRA / "monitoring-images.auto.tfvars").read_text(encoding="utf-8")).group(1)
                self.assertRegex(pinned, rf"^{re.escape(default)}@sha256:[0-9a-f]{{64}}$", "the digest file pins the same version as the default")

    def test_the_services_are_cluster_ip_only_and_grafana_is_reached_only_through_caddy(self):
        for name in ("prometheus", "grafana", "alertmanager"):
            with self.subTest(service=name):
                service = block_text("monitoring.tf", f'resource "kubernetes_service_v1" "{name}"')
                self.assertIn('type     = "ClusterIP"', service)
                for forbidden in ("LoadBalancer", "NodePort", "external"):
                    self.assertNotIn(forbidden, service)
        ingress = code("ingress.tf")
        self.assertIn("local.k8s_grafana_service_fqdn", ingress)
        for private in ("k8s_prometheus_service_fqdn", "k8s_alertmanager_service_fqdn", "prometheus_port", "alertmanager_port"):
            self.assertNotIn(private, ingress, "only Grafana is routed")

    def test_prometheus_data_is_a_retained_disk_and_grafana_state_is_disposable(self):
        claim = block_text("monitoring.tf", 'resource "kubernetes_persistent_volume_claim_v1" "prometheus"')
        self.assertIn("kubernetes_storage_class_v1.factory_disk.metadata[0].name", claim)
        self.assertIn('access_modes       = ["ReadWriteOnce"]', claim)
        self.assertIn("${var.prometheus_storage_gib}Gi", claim)
        prometheus = self.deployments["prometheus"]
        self.assertIn("--storage.tsdb.retention.time=${var.prometheus_retention}", prometheus)
        self.assertIn("--storage.tsdb.retention.size=${floor(var.prometheus_storage_gib * 0.8)}GB", prometheus)
        self.assertNotIn("--web.enable-lifecycle", prometheus)
        self.assertNotIn("--web.enable-admin-api", prometheus)
        self.assertRegex(self.deployments["grafana"], r'name = "data", emptyDir')

    def test_prometheus_may_only_read_node_metrics(self):
        role = block_text("monitoring.tf", 'resource "kubernetes_manifest" "prometheus_cluster_role"')
        verbs = re.findall(r'verbs = \[([^\]]*)\]', role)
        self.assertEqual(verbs, ['"get", "list", "watch"', '"get"'])
        self.assertEqual(re.findall(r'resources = \[([^\]]*)\]', role), ['"nodes"', '"nodes/metrics"'])
        for forbidden in ("pods", "secrets", "nodes/proxy", "*", "create", "delete", "update", "patch"):
            self.assertNotIn(f'"{forbidden}"', role)

    def test_the_dedicated_node_pool_is_tainted_and_optional(self):
        pool = block_text("aks.tf", 'resource "azurerm_kubernetes_cluster_node_pool" "monitoring"')
        self.assertIn("count = var.monitoring_enabled && var.monitoring_node_pool_vm_size != null ? 1 : 0", pool)
        self.assertIn('node_taints           = ["workload=monitoring:NoSchedule"]', pool)
        self.assertIn("node_labels           = local.monitoring_pool_labels", pool)
        self.assertIn('{ key = "workload", operator = "Equal", value = "monitoring", effect = "NoSchedule" }', self.text)
        for name, deployment in self.deployments.items():
            with self.subTest(deployment=name):
                self.assertIn("nodeSelector                  = local.monitoring_node_selector", deployment)
                self.assertIn("tolerations                   = local.monitoring_tolerations", deployment)


class KubeStateMetrics(unittest.TestCase):
    """Issue 149: one small kube-state-metrics pod, read-only, for the NAP-side series (docs/metrics.md)."""

    @classmethod
    def setUpClass(cls):
        cls.text = code("monitoring.tf")
        cls.deployment = block_text("monitoring.tf", 'resource "kubernetes_manifest" "kube_state_metrics"')
        cls.prometheus = (MON / "prometheus.yml.tftpl").read_text(encoding="utf-8")
        cls.config = (MON / "kube-state-metrics-nodeclaim.yml.tftpl").read_text(encoding="utf-8")
        cls.job = cls.prometheus[cls.prometheus.index("job_name: kube-state-metrics") :]

    def worker_namespace(self):
        return re.search(r'(?m)^\s*worker_namespace\s*=\s*"([^"]+)"', code("locals.tf")).group(1)

    def test_everything_exists_only_with_monitoring_and_the_nodeclaim_part_only_on_request(self):
        for name in ("kube_state_metrics", "kube_state_metrics_cluster_role", "kube_state_metrics_cluster_role_binding"):
            kind = "kubernetes_manifest"
            with self.subTest(resource=name):
                self.assertRegex(block_text("monitoring.tf", f'resource "{kind}" "{name}"'), r"count\s*=\s*local\.monitoring_count")
        for header in ('resource "kubernetes_service_account_v1" "kube_state_metrics"', 'resource "kubernetes_service_v1" "kube_state_metrics"'):
            with self.subTest(resource=header):
                self.assertRegex(block_text("monitoring.tf", header), r"count\s*=\s*local\.monitoring_count")
        for header in (
            'resource "kubernetes_manifest" "kube_state_metrics_nodeclaim_cluster_role"',
            'resource "kubernetes_manifest" "kube_state_metrics_nodeclaim_cluster_role_binding"',
        ):
            with self.subTest(resource=header):
                self.assertIn("count = var.monitoring_enabled && var.monitoring_nodeclaim_metrics_enabled ? 1 : 0", block_text("monitoring.tf", header))
        self.assertRegex(block_text("variables.tf", 'variable "monitoring_nodeclaim_metrics_enabled"'), r"default\s*=\s*false")
        role = block_text("monitoring.tf", 'resource "kubernetes_manifest" "kube_state_metrics_nodeclaim_cluster_role"')
        self.assertIn('condition     = var.nap_mode == "Auto"', role)

    def test_the_roles_only_list_and_watch_what_the_series_need(self):
        base = block_text("monitoring.tf", 'resource "kubernetes_manifest" "kube_state_metrics_cluster_role"')
        self.assertEqual(re.findall(r"resources = \[([^\]]*)\]", base), ['"pods", "nodes"'])
        self.assertEqual(re.findall(r"verbs = \[([^\]]*)\]", base), ['"list", "watch"'])
        claims = block_text("monitoring.tf", 'resource "kubernetes_manifest" "kube_state_metrics_nodeclaim_cluster_role"')
        self.assertEqual(re.findall(r"apiGroups = \[([^\]]*)\]", claims), ["local.ksm_nodeclaim_api_group", '"apiextensions.k8s.io"'])
        self.assertIn('ksm_nodeclaim_api_group = "karpenter.sh"', self.text)  # a local: the cluster guard reads a literal as a NodePool object
        self.assertEqual(re.findall(r"resources = \[([^\]]*)\]", claims), ['"nodeclaims"', '"customresourcedefinitions"'])
        self.assertEqual(re.findall(r"verbs = \[([^\]]*)\]", claims), ['"list", "watch"', '"list", "watch"'])
        for name, role in (("base", base), ("nodeclaims", claims)):
            for forbidden in ("secrets", "configmaps", "*", "create", "delete", "update", "patch", "get", "proxy", "exec", "events"):
                with self.subTest(role=name, verb=forbidden):
                    self.assertNotIn(f'"{forbidden}"', role)
        for header in ("kube_state_metrics_cluster_role_binding", "kube_state_metrics_nodeclaim_cluster_role_binding"):
            binding = block_text("monitoring.tf", f'resource "kubernetes_manifest" "{header}"')
            with self.subTest(binding=header):
                self.assertEqual(re.findall(r"subjects = \[\s*\{ ([^}]*) \},", binding), ['kind = "ServiceAccount", name = "kube-state-metrics", namespace = local.k8s_monitoring_namespace'])

    def test_the_pod_is_hardened_small_unauthenticated_to_azure_and_not_host_connected(self):
        deployment = self.deployment
        self.assertIn("local.monitoring_pod_security", deployment)
        self.assertRegex(deployment, r"runAsUser\s*=\s*65534")
        self.assertIn("securityContext = local.monitoring_container_security", deployment)
        self.assertRegex(deployment, r'strategy\s*=\s*\{ type = "Recreate" \}')
        self.assertRegex(deployment, r"enableServiceLinks\s*=\s*false")
        self.assertRegex(deployment, r"automountServiceAccountToken\s*=\s*true")
        for forbidden in ("hostNetwork", "hostPID", "hostPath", "privileged", "hostUsers", "procMount", "Unconfined", "keyvault", "csi", "workload", "monitoring_pod_labels"):
            self.assertNotIn(forbidden, deployment)
        # It fits beside Prometheus and Grafana on the one node of the pool `mon` (200m and 896Mi are already requested there).
        self.assertIn('requests = { cpu = "10m", memory = "64Mi" }', deployment)
        self.assertIn('limits   = { cpu = "200m", memory = "256Mi" }', deployment)
        self.assertIn("nodeSelector                  = local.monitoring_node_selector", deployment)
        self.assertIn("tolerations                   = local.monitoring_tolerations", deployment)
        self.assertIn("serviceAccountName            = \"kube-state-metrics\"", deployment)
        # No Azure identity: it is not one of the components that read a Key Vault secret.
        self.assertNotIn("kube-state-metrics", attribute_block(self.text, "monitoring_secret_inputs = "))
        self.assertNotIn("kube_state_metrics", block_text("monitoring.tf", 'resource "kubernetes_manifest" "monitoring_secret_provider_class"'))

    def test_the_image_comes_from_a_variable_that_refuses_a_floating_tag_and_the_digest_is_pinned_when_known(self):
        self.assertIn("image           = var.kube_state_metrics_image", self.deployment)
        block = block_text("variables.tf", 'variable "kube_state_metrics_image"')
        self.assertIn("never latest", block)
        default = re.search(r'default\s*=\s*"([^"]+)"', block).group(1)
        self.assertRegex(default, r"^registry\.k8s\.io/kube-state-metrics/kube-state-metrics:v[0-9]+\.[0-9]+\.[0-9]+$")
        tfvars = (INFRA / "monitoring-images.auto.tfvars").read_text(encoding="utf-8")
        pinned = re.search(r'(?m)^kube_state_metrics_image\s*=\s*"([^"]+)"', tfvars)
        if pinned:  # the digest could not be read offline (README.md): once it is added it must be the same version as the default
            self.assertRegex(pinned.group(1), rf"^{re.escape(default)}@sha256:[0-9a-f]{{64}}$")
        else:
            self.assertIn("NO digest is pinned yet", tfvars)
            self.assertIn("never typed from memory", tfvars)

    def test_the_service_is_cluster_ip_only_and_the_ingress_does_not_know_it(self):
        service = block_text("monitoring.tf", 'resource "kubernetes_service_v1" "kube_state_metrics"')
        self.assertIn('type     = "ClusterIP"', service)
        for forbidden in ("LoadBalancer", "NodePort", "external"):
            self.assertNotIn(forbidden, service)
        self.assertNotIn("kube_state_metrics", code("ingress.tf"))
        self.assertNotIn("ksm_", code("ingress.tf"))

    def test_it_watches_pods_and_nodes_only_and_the_nodeclaim_arguments_are_last_and_optional(self):
        text = call_arguments(self.text, "ksm_args = concat(")
        self.assertIn('"--resources=pods,nodes"', text)
        self.assertIn('"--metric-labels-allowlist=nodes=[karpenter.sh/nodepool]"', text)
        for forbidden in ("secrets", "configmaps", "events", "--resources=*"):
            self.assertNotIn(forbidden, text)
        # Append-only (the kubernetes_manifest provider fills a list by position): the optional argument is the last entry.
        self.assertTrue(
            text.rstrip().rstrip(",").endswith('var.monitoring_nodeclaim_metrics_enabled ? ["--custom-resource-state-config-file=/etc/kube-state-metrics/customresourcestate.yaml"] : []'),
            text,
        )
        mounts = self.deployment[self.deployment.index("volumeMounts") :]
        # A fixed-length list, never `cond ? [x] : []` and never a filtered for expression: the kubernetes_manifest provider cannot plan
        # the first ("Failed to transform List value into Tuple of different length") and returns a list for the second ("attribute
        # volumes: tuple required"), both seen in production on 2026-11-10. The ConfigMap is therefore always created with monitoring.
        self.assertIn('{ name = "config", mountPath = "/etc/kube-state-metrics", readOnly = true }', mounts)
        self.assertNotIn("monitoring_nodeclaim_metrics_enabled ? [{", self.deployment.replace("\n", "").replace("  ", ""))
        self.assertNotIn("for item in", self.deployment)
        self.assertRegex(block_text("monitoring.tf", 'resource "kubernetes_config_map_v1" "kube_state_metrics_config"'), r"count\s*=\s*local\.monitoring_count")
        self.assertIn("configMap = { name = \"kube-state-metrics-config\" }", self.deployment)
        self.assertIn("kubernetes_config_map_v1.kube_state_metrics_config", self.deployment)  # depends_on, not an index

    def test_the_custom_resource_state_file_reads_nodeclaims_only_and_names_the_api_version_as_unverified(self):
        self.assertIn("group: karpenter.sh", self.config)
        self.assertIn("kind: NodeClaim", self.config)
        self.assertIn("version: ${api_version}", self.config)
        self.assertEqual(len(re.findall(r"(?m)^\s+- groupVersionKind:", self.config)), 1)
        self.assertEqual(
            re.findall(r"(?m)^\s+- name: (\w+)", self.config),
            ["nodeclaim_created_timestamp_seconds", "nodeclaim_condition_last_transition_timestamp_seconds"],
        )
        self.assertIn("UNVERIFIED", self.config)
        supplied = call_arguments(self.text, "ksm_nodeclaim_config_text = templatefile(")
        self.assertIn("api_version = local.ksm_nodeclaim_api_version", supplied)
        self.assertIn('ksm_nodeclaim_api_version = "v1"', self.text)
        self.assertIn("UNVERIFIED: the API version that NAP serves for NodeClaim", (INFRA / "monitoring.tf").read_text(encoding="utf-8"))
        # The unique label is the name of the object (without it two claims of a NodePool would be one series twice).
        self.assertIn("name: [metadata, name]", self.config)
        self.assertNotIn("providerID", self.config)

    def test_the_scrape_job_is_a_static_target_without_a_credential_and_keeps_only_the_series_that_are_used(self):
        self.assertIn('targets: ["${ksm_target}"]', self.job)
        self.assertNotIn("authorization", self.job)
        self.assertNotIn("credentials_file", self.job)
        supplied = call_arguments(self.text, "prometheus_config_text = templatefile(")
        self.assertIn('ksm_target          = "${local.k8s_ksm_service_fqdn}:${local.ksm_port}"', supplied)
        self.assertIn("worker_namespace    = local.worker_contract.names.worker_namespace", supplied)
        self.assertIn('k8s_ksm_service_fqdn          = "kube-state-metrics.${local.k8s_monitoring_namespace}.svc.cluster.local"', self.text)
        regex = re.search(r"regex: (kube_pod_[^\n]+)\n\s+action: keep", self.job).group(1)
        self.assertIn("${worker_namespace}", regex)
        self.assertIn("kube_node_labels;", regex)  # node and NodeClaim series have no namespace label
        pods = re.search(r"kube_pod_\(([^)]*)\)", regex).group(1).split("|")
        claims = re.search(r"kube_customresource_nodeclaim_\(([^)]*)\)", regex).group(1).split("|")
        kept = {f"kube_pod_{name}" for name in pods} | {"kube_node_labels"} | {f"kube_customresource_nodeclaim_{name}" for name in claims}
        used = set()
        for _, text in [*((name, rule["expr"]) for name, rule in rule_expressions()), *((name, rule["expr"]) for name, rule in recording_rules()), *dashboard_expressions()]:
            used |= {name for name in identifiers(text) if name.startswith("kube_")}
        self.assertEqual(used, kept, "keep the series that are used, and only those")
        self.assertEqual(kept, {name for name in PLATFORM_METRICS if name.startswith("kube_")})

    def test_the_rules_name_the_worker_namespace_of_the_pod_contract(self):
        namespace = self.worker_namespace()
        self.assertEqual(namespace, "factory-workers")
        for name, rule in [*rule_expressions(), *recording_rules()]:
            if "kube_" not in rule["expr"]:
                continue  # the container_ rules of the manager name the factory namespace
            for match in re.findall(r'namespace="([^"]+)"', rule["expr"]):
                with self.subTest(rule=name):
                    self.assertEqual(match, namespace)

    def test_the_recorded_series_and_the_alerts_of_the_nap_side_exist(self):
        recorded = {name for name, _ in recording_rules()}
        self.assertEqual(
            recorded,
            {
                "factory:worker_pod_time_to_ready_seconds",
                "factory:worker_pods_pending",
                "factory:worker_pods_unschedulable",
                "factory:nap_nodes",
                "factory:nap_nodes_total",
                "factory:nodeclaim_condition_seconds",
            },
        )
        alerts = {name: rule for name, rule in rule_expressions()}
        for expected in ("FactoryKubeStateMetricsNotScraped", "FactoryWorkerPodPendingTooLong", "FactoryWorkerPodSlowToReady", "FactoryNodeClaimNotInitialized", "FactoryNapNodesIdle", "FactoryWorkerPodEvicted"):
            with self.subTest(alert=expected):
                self.assertIn(expected, alerts)
        # Thresholds: pending older than 10 minutes, slow to Ready above 5 minutes (a cold start measured 94 to 101 s), a claim older than 15.
        self.assertIn("> 600", alerts["FactoryWorkerPodPendingTooLong"]["expr"])
        self.assertIn("> 300", alerts["FactoryWorkerPodSlowToReady"]["expr"])
        self.assertIn("> 900", alerts["FactoryNodeClaimNotInitialized"]["expr"])
        # An alert on a recorded series names one that exists.
        for name, rule in alerts.items():
            for used in re.findall(r"\b(factory:[a-z0-9_]+)\b", rule["expr"]):
                with self.subTest(alert=name, recorded=used):
                    self.assertIn(used, recorded)
        # The honest limit is in the runbook of the eviction alert: it is not Karpenter's eviction.
        self.assertIn("not available from kube-state-metrics", alerts["FactoryWorkerPodEvicted"]["annotations"]["runbook"])

    def test_every_recording_rule_is_used_and_no_recorded_name_is_a_manager_series(self):
        text = " ".join([*(rule["expr"] for _, rule in rule_expressions()), *(expr for _, expr in dashboard_expressions())])
        for name, rule in recording_rules():
            with self.subTest(record=name):
                self.assertRegex(name, r"^factory:[a-z0-9_]+$", "a colon keeps it apart from the factory_ names of the catalogue")
                self.assertEqual(sorted(rule), ["expr", "record"])
                self.assertIn(name, text)

    def test_the_dashboard_shows_the_series_and_keeps_object_names_out_of_the_legends(self):
        board = dashboards()["factory-nap.json"]
        self.assertEqual(board["uid"], "factory-nap")
        self.assertEqual(board["templating"]["list"], [])
        titles = [panel["title"] for panel in board["panels"]]
        for expected in ("Pending worker pods", "Nodes of node auto-provisioning", "Worker pod: creation to Ready", "NodeClaim: creation to each condition", "Worker pods Evicted by the kubelet"):
            self.assertIn(expected, titles)
        for panel in board["panels"]:
            for target in panel.get("targets", []):
                with self.subTest(panel=panel["title"]):
                    for forbidden in ("pod", "uid", "name"):
                        self.assertNotIn("{{" + forbidden + "}}", target["legendFormat"], "per-object labels stay out of a legend")
        used = set()
        for _, expr in dashboard_expressions():
            used |= set(re.findall(r"\b(factory:[a-z0-9_]+)\b", expr))
        self.assertLessEqual({"factory:worker_pods_pending", "factory:nap_nodes", "factory:nap_nodes_total", "factory:worker_pod_time_to_ready_seconds", "factory:nodeclaim_condition_seconds"}, used)

    def test_the_documents_say_what_is_implemented_and_what_is_not(self):
        text = (ROOT / "docs" / "metrics.md").read_text(encoding="utf-8")
        self.assertNotIn("NAP-side series (proposed, not implemented)", text)
        section = text[text.index("### NAP-side series") :]
        for name in ("kube_pod_status_phase", "kube_node_labels", "factory:nap_nodes", "factory:worker_pod_time_to_ready_seconds", "kube_customresource_nodeclaim_condition_last_transition_timestamp_seconds", "factory:nodeclaim_condition_seconds"):
            with self.subTest(name=name):
                self.assertIn(f"`{name}", section)  # a series may be followed by a selector
        for heading in ("Not available, and why", "Which source: self-hosted Prometheus or Azure Monitor", "Network isolation", "Per-session footprint"):
            self.assertIn(heading, section)
        self.assertIn("one more small pod", section)
        readme = (INFRA / "README.md").read_text(encoding="utf-8")
        self.assertIn("kube-state-metrics", readme)
        self.assertIn("one more small pod", readme)
        self.assertNotIn("No node-exporter and no kube-state-metrics", readme)


class NetworkPolicy(unittest.TestCase):
    def test_the_policy_is_prepared_and_off_and_keeps_the_manager_api_open_to_caddy(self):
        policy = block_text("monitoring.tf", 'resource "kubernetes_manifest" "factory_network_policy"')
        self.assertIn("count = var.monitoring_enabled && var.factory_network_policy_enabled ? 1 : 0", policy)
        self.assertRegex(policy, r"port = local\.app_port")
        self.assertRegex(policy, r"port = local\.metrics_port")
        self.assertIn("local.k8s_ingress_selector_labels", policy)
        self.assertIn("local.prometheus_selector_labels", policy)
        # Slice 15a (issue #144): a third rule, the relay port from the worker namespace, is appended behind pod_backend_enabled
        # (infra/test/test_worker_ns_design.py pins that it is present if and only if the variable is on).
        self.assertEqual(
            re.findall(r'"kubernetes.io/metadata.name" = (local\.[\w.]+)', policy),
            ["local.k8s_ingress_namespace", "local.k8s_monitoring_namespace", "local.worker_contract.names.worker_namespace"],
        )
        raw = (INFRA / "monitoring.tf").read_text(encoding="utf-8")
        self.assertIn("PREPARED, OFF", raw)
        # The wording changed with the Cilium data plane (slice 15a): enforcement needs it, and the old text said that no engine
        # exists and pointed at the superseded issue.
        self.assertIn("Enforcement needs the Cilium data plane", raw)
        self.assertNotIn("NO network policy engine", raw)
        self.assertNotIn("issue 122", raw)
        variable = block_text("variables.tf", 'variable "factory_network_policy_enabled"')
        self.assertIn("PREPARED, OFF", variable)
        self.assertIn("Enforcement needs the Cilium data plane", variable)
        self.assertNotIn("NO network policy engine", variable)


class GrafanaConfiguration(unittest.TestCase):
    """The rendered grafana.ini: sub path, Entra only, secure cookies, no telemetry and no runtime plugin installs."""

    @classmethod
    def setUpClass(cls):
        text = (MON / "grafana.ini.tftpl").read_text(encoding="utf-8")
        cls.template = text
        values = {"port": "3000", "domain": "api.example.test", "client_id": "client-id", "tenant_id": "tenant-id", "operator_role": "Editor"}
        rendered = re.sub(r"\$\{(\w+)\}", lambda found: values[found.group(1)], text)
        cls.config = configparser.ConfigParser(interpolation=None, strict=True, comment_prefixes=(";", "#"), inline_comment_prefixes=None)
        cls.config.read_string(rendered)

    def test_every_placeholder_is_supplied_by_monitoring_tf(self):
        supplied = call_arguments(code("monitoring.tf"), "grafana_ini_text = templatefile(")
        self.assertEqual(set(re.findall(r"\$\{(\w+)\}", self.template)), set(re.findall(r"(?m)^\s*(\w+)\s*=", supplied)))

    def test_it_is_served_from_the_sub_path_of_the_api_host(self):
        server = self.config["server"]
        self.assertEqual(server["root_url"], "https://api.example.test/grafana/")
        self.assertEqual(server["serve_from_sub_path"], "true")
        self.assertIn("domain = ${domain}", self.template)
        self.assertIn("root_url = https://${domain}/grafana/", self.template)

    def test_cookies_are_secure_and_framing_is_off(self):
        security = self.config["security"]
        self.assertEqual(security["cookie_secure"], "true")
        self.assertEqual(security["cookie_samesite"], "lax", "strict would break the return from Entra; lax is the strictest that works")
        self.assertEqual(security["allow_embedding"], "false")
        self.assertEqual(security["strict_transport_security"], "true")
        self.assertEqual(security["content_security_policy"], "true")
        self.assertNotIn("cookie_httponly", security, "Grafana's session cookie is HttpOnly and has no switch to turn that off")

    def test_nobody_is_anonymous_and_there_is_no_local_login_or_admin(self):
        self.assertEqual(self.config["auth.anonymous"]["enabled"], "false")
        self.assertEqual(self.config["auth.basic"]["enabled"], "false")
        self.assertEqual(self.config["auth.proxy"]["enabled"], "false")
        self.assertEqual(self.config["auth.jwt"]["enabled"], "false")
        self.assertEqual(self.config["auth"]["disable_login_form"], "true")
        self.assertEqual(self.config["security"]["disable_initial_admin_creation"], "true")
        self.assertEqual(self.config["users"]["allow_sign_up"], "false")
        self.assertEqual(self.config["users"]["auto_assign_org_role"], "Viewer")
        self.assertNotRegex(self.template, r"(?mi)^\s*admin_(user|password)\s*=")
        self.assertEqual(self.config["public_dashboards"]["enabled"], "false")

    def test_what_answers_without_a_sign_in_shows_no_metrics_and_no_version(self):
        # Caddy hands everything below /grafana/ to Grafana, so /grafana/metrics reaches Grafana's own Prometheus
        # endpoint (on by default, no basic auth) and /grafana/api/health reports the version unless it is hidden.
        self.assertEqual(self.config["metrics"]["enabled"], "false")
        self.assertNotIn("basic_auth_username", self.config["metrics"], "switched off, not protected by a password kept in the config")
        self.assertEqual(self.config["auth.anonymous"]["hide_version"], "true")
        grafana = block_text("monitoring.tf", 'resource "kubernetes_manifest" "grafana"')
        self.assertNotRegex(grafana, r"GF_(METRICS|AUTH_ANONYMOUS)", "an environment variable would override the file and could turn them back on")

    def test_entra_sign_in_maps_the_app_roles_and_refuses_everyone_else(self):
        oauth = self.config["auth.generic_oauth"]
        self.assertEqual(oauth["enabled"], "true")
        self.assertEqual(oauth["auto_login"], "true")
        self.assertEqual(oauth["use_pkce"], "true")
        self.assertEqual(oauth["role_attribute_strict"], "true")
        self.assertEqual(oauth["allow_assign_grafana_admin"], "false")
        self.assertEqual(oauth["skip_org_role_sync"], "false")
        for endpoint in ("auth_url", "token_url"):
            self.assertRegex(oauth[endpoint], r"^https://login\.microsoftonline\.com/tenant-id/oauth2/v2\.0/(authorize|token)$")
        mapping = oauth["role_attribute_path"]
        self.assertIn("'Factory.Operator') && 'Editor'", mapping)
        self.assertIn("'Factory.Viewer') && 'Viewer'", mapping)
        self.assertNotIn("Admin", mapping)
        self.assertIn("&& '${operator_role}'", self.template)
        self.assertEqual(self.config["auth.generic_oauth"]["login_attribute_path"], "preferred_username")
        self.assertEqual(oauth["client_id"], "client-id")
        self.assertNotIn("client_secret", oauth, "the secret is a file (GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET__FILE), never in the config")
        self.assertNotIn("api_url", oauth)
        self.assertNotIn("tls_skip_verify_insecure", oauth)
        self.assertNotIn("allowed_domains", oauth)

    def test_no_telemetry_no_update_checks_no_runtime_plugin_installs_no_external_snapshots(self):
        self.assertEqual({key: self.config["analytics"][key] for key in ("reporting_enabled", "check_for_updates", "check_for_plugin_updates", "feedback_links_enabled")}, dict.fromkeys(("reporting_enabled", "check_for_updates", "check_for_plugin_updates", "feedback_links_enabled"), "false"))
        self.assertEqual(self.config["news"]["news_feed_enabled"], "false")
        self.assertEqual(self.config["plugins"]["plugin_admin_enabled"], "false")
        self.assertEqual(self.config["plugins"]["preinstall_disabled"], "true")
        self.assertEqual(self.config["snapshots"]["external_enabled"], "false")
        self.assertNotRegex(self.template, r"(?m)^\s*(preinstall|install_plugins|allow_loading_unsigned_plugins)\b")
        grafana = block_text("monitoring.tf", 'resource "kubernetes_manifest" "grafana"')
        self.assertNotIn("GF_INSTALL_PLUGINS", grafana)

    def test_the_operator_role_is_a_validated_variable_and_a_viewer_is_never_more(self):
        block = block_text("variables.tf", 'variable "grafana_operator_role"')
        self.assertRegex(block, r'default\s*=\s*"Editor"')
        self.assertIn('contains(["Editor", "Admin"], var.grafana_operator_role)', block)
        self.assertIn("operator_role = var.grafana_operator_role", code("monitoring.tf"))

    def test_dashboards_and_the_datasource_are_provisioned_and_not_editable(self):
        text = code("monitoring.tf")
        self.assertIn("allowUiUpdates        = false", text)
        self.assertIn("disableDeletion       = true", text)
        self.assertIn("editable  = false", text)
        self.assertIn('uid       = "factory-prometheus"', text)
        for name, board in dashboards().items():
            with self.subTest(dashboard=name):
                self.assertFalse(board["editable"])
        self.assertEqual(self.config["dashboards"]["default_home_dashboard_path"], "/var/lib/grafana-dashboards/factory-overview.json")
        self.assertIn("factory-overview.json", dashboards())


class IngressRoute(unittest.TestCase):
    """Grafana is the one added route; the manager's routes are untouched and no manager token reaches Grafana."""

    @classmethod
    def setUpClass(cls):
        text = code("ingress.tf")
        start = text.index("caddy_routes = var.monitoring_enabled ? [")
        block = text[start : text.index("\n  ]\n", start)]
        cls.on, cls.off = block.split("] : [", 1)

    def test_the_grafana_path_is_handled_before_the_manager_and_only_that_path(self):
        self.assertIn('"\\tredir /grafana /grafana/ permanent"', self.on)
        self.assertIn('"\\thandle /grafana/* {"', self.on)
        self.assertLess(self.on.index("handle /grafana/*"), self.on.index('"\\thandle {"'))
        self.assertEqual(self.on.count("reverse_proxy ${"), 2)
        self.assertIn("${local.k8s_grafana_service_fqdn}:${local.grafana_port}", self.on)
        self.assertIn("${local.k8s_app_service_fqdn}:${local.app_port}", self.on)
        for other in ("/dashboard", "/ui", "/healthz", "/history", "/repos", "/metrics"):
            self.assertNotIn(other, self.on, "the manager's own paths are not named, so they are not changed")

    def test_the_manager_token_never_reaches_grafana_and_grafana_has_no_other_auth_in_front(self):
        grafana = self.on[: self.on.index('"\\thandle {"')]
        self.assertIn("header_up -Authorization", grafana)
        self.assertIn("header_up -X-Webauth-User", grafana)
        self.assertNotRegex(self.on, r"(?i)\bbasic_?auth\b|forward_auth|jwt|oauth")
        self.assertNotIn("header_up", self.on[self.on.index('"\\thandle {"') :])

    def test_the_easy_auth_headers_are_still_removed_for_every_path(self):
        text = code("ingress.tf")
        head = text[text.index("caddyfile = join(") : text.index("caddy_routes = ")]
        for header in ("X-MS-CLIENT-PRINCIPAL", "X-MS-CLIENT-PRINCIPAL-ID", "X-MS-CLIENT-PRINCIPAL-NAME", "X-MS-CLIENT-PRINCIPAL-IDP"):
            self.assertIn(f'request_header -{header}"', head)
        self.assertIn("local.caddy_routes", head)

    def test_the_public_urls_for_the_operator_are_outputs(self):
        outputs = code("outputs.tf")
        self.assertIn('https://${local.ingress_fqdn}/grafana/login/generic_oauth', outputs)
        self.assertIn('var.monitoring_enabled ? "https://${local.ingress_fqdn}/grafana/" : null', outputs)


class PrometheusConfiguration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (MON / "prometheus.yml.tftpl").read_text(encoding="utf-8")

    def test_the_manager_is_scraped_with_the_token_file_and_over_the_metrics_service(self):
        job = self.text[self.text.index("job_name: factory-manager") : self.text.index("job_name: prometheus")]
        self.assertIn("type: Bearer", job)
        self.assertIn("credentials_file: /mnt/secrets/${token_file}", job)
        self.assertIn('targets: ["${manager_target}"]', job)
        self.assertIn("scheme: http", job)
        supplied = call_arguments(code("monitoring.tf"), "prometheus_config_text = templatefile(")
        self.assertIn('manager_target      = "${local.k8s_metrics_service_fqdn}:${local.metrics_port}"', supplied)
        self.assertIn('token_file          = local.kv_secret_names.metrics_token', supplied)
        self.assertIn('k8s_metrics_service_fqdn      = "${local.k8s_app_name}-metrics.${local.k8s_namespace}.svc.cluster.local"', code("monitoring.tf"))

    def test_the_alerting_block_exists_only_with_a_receiver(self):
        self.assertIn("%{ if alerting ~}", self.text)
        block = self.text[self.text.index("%{ if alerting ~}") : self.text.index("%{ endif ~}")]
        self.assertIn("${alertmanager_target}", block)
        self.assertIn("alerting            = local.alerting_enabled", code("monitoring.tf"))

    def test_the_kubelet_scrape_keeps_exactly_the_container_series_the_rules_and_dashboards_use(self):
        keep = re.search(r"regex: container_\(([^)]*)\)\n\s+action: keep", self.text).group(1).split("|")
        kept = {f"container_{name}" for name in keep}
        used = set()
        for _, text in [*((name, rule["expr"]) for name, rule in rule_expressions()), *dashboard_expressions()]:
            used |= {name for name in identifiers(text) if name.startswith("container_")}
        self.assertEqual(used, kept, "keep the series that are used, and only those")
        self.assertTrue(kept <= PLATFORM_METRICS)

    def test_the_kubelet_scrape_uses_the_service_account_token_and_only_three_namespaces(self):
        self.assertIn("credentials_file: /var/run/secrets/kubernetes.io/serviceaccount/token", self.text)
        self.assertIn("role: node", self.text)
        supplied = call_arguments(code("monitoring.tf"), "prometheus_config_text = templatefile(")
        self.assertIn("join(\"|\", [local.k8s_namespace, local.k8s_ingress_namespace, local.k8s_monitoring_namespace])", supplied)
        self.assertIn("insecure_skip_verify: true", self.text)  # recorded as UNVERIFIED in README.md

    def test_the_rule_files_are_the_mounted_json(self):
        self.assertIn("/etc/prometheus-rules/*.json", self.text)
        self.assertIn('"alert-rules.json" = local.prometheus_rules_text', code("monitoring.tf"))
        self.assertIn('prometheus_rules_text = file("${path.module}/monitoring/alert-rules.json")', code("monitoring.tf"))


class AlertRules(unittest.TestCase):
    EXPECTED = {
        "FactoryManagerNotScraped",
        "FactoryRunBudgetBurn",
        "FactoryBudgetDrained",
        "FactoryJobsInterrupted",
        "FactoryWatcherBackoff",
        "FactoryGitHubAppUnhealthy",
        "FactoryHistoryNearCap",
        "FactoryHistoryTruncated",
        "FactoryPodMemoryNearLimit",
        "FactoryPodOOMKilled",
        "FactoryDataDiskLow",
        "FactoryTriggerPausedTooLong",
        "FactoryPlansFailing",
    }

    def test_the_first_signals_worth_waking_someone_for_are_there(self):
        self.assertLessEqual(self.EXPECTED, {name for name, _ in rule_expressions()})

    def test_every_rule_has_a_severity_a_for_and_a_runbook_sentence(self):
        seen = set()
        for name, rule in rule_expressions():
            with self.subTest(alert=name):
                self.assertNotIn(name, seen)
                seen.add(name)
                self.assertRegex(name, r"^[A-Z][A-Za-z0-9]+$")
                self.assertIn(rule["labels"]["severity"], ("critical", "warning", "info"))
                self.assertRegex(rule["for"], r"^[0-9]+[smh]$")
                self.assertEqual(sorted(rule["annotations"]), ["runbook", "summary"])
                self.assertGreaterEqual(len(rule["annotations"]["runbook"]), 80, "a runbook sentence says what to check or do")
                self.assertTrue(rule["annotations"]["runbook"].rstrip().endswith("."))
                self.assertTrue(rule["annotations"]["summary"].rstrip().endswith("."))

    def test_every_metric_an_alert_uses_is_in_the_catalogue_or_is_a_platform_series(self):
        catalogue = catalogue_names()
        self.assertGreaterEqual(len(catalogue), 30)
        for name, rule in rule_expressions():
            for metric in identifiers(rule["expr"]):
                with self.subTest(alert=name, metric=metric):
                    if metric.startswith("factory_"):
                        self.assertIn(base_name(metric), catalogue)
                    else:
                        self.assertIn(metric, PLATFORM_METRICS)

    def test_a_histogram_is_only_queried_through_its_series(self):
        histograms = {"factory_plan_to_pr_seconds", "factory_phase_seconds", "factory_manager_request_seconds"}
        for name, text in [*((name, rule["expr"]) for name, rule in rule_expressions()), *dashboard_expressions()]:
            for metric in re.findall(r"\b(factory_[a-z0-9_]+)\b", text):
                with self.subTest(where=name, metric=metric):
                    self.assertNotIn(metric, histograms, "a histogram has _bucket, _sum and _count series only")

    def test_annotations_template_only_closed_labels(self):
        for name, rule in rule_expressions():
            for text in rule["annotations"].values():
                for label in re.findall(r"\{\{ \$labels\.(\w+) \}\}", text):
                    with self.subTest(alert=name, label=label):
                        self.assertIn(label, ("repo", "state"))
                self.assertNotRegex(text, r"\{\{(?! \$labels\.\w+ \}\})")

    def test_the_rules_file_is_json_that_prometheus_reads_as_yaml(self):
        document = json.loads((MON / "alert-rules.json").read_text(encoding="utf-8"))
        self.assertEqual(list(document), ["groups"])
        for group in document["groups"]:
            self.assertEqual(sorted(group), ["name", "rules"])


class Dashboards(unittest.TestCase):
    def test_there_is_an_overview_and_every_dashboard_is_complete(self):
        boards = dashboards()
        self.assertGreaterEqual(len(boards), 2)
        uids = set()
        for name, board in boards.items():
            with self.subTest(dashboard=name):
                uids.add(board["uid"])
                self.assertEqual(name, f"{board['uid']}.json")
                self.assertTrue(board["title"])
                self.assertTrue([panel for panel in board["panels"] if panel["type"] != "row"])
                ids = [panel["id"] for panel in board["panels"]]
                self.assertEqual(len(ids), len(set(ids)))
                for panel in board["panels"]:
                    if panel["type"] == "row":
                        continue
                    self.assertTrue(panel["title"])
                    self.assertTrue(panel["targets"])
                    self.assertEqual(panel["datasource"], {"type": "prometheus", "uid": "factory-prometheus"})
        self.assertEqual(len(uids), len(boards))
        self.assertIn("factory-overview", uids)

    def test_every_metric_a_dashboard_uses_is_in_the_catalogue_or_is_a_platform_series(self):
        catalogue = catalogue_names()
        seen = set()
        for where, expr in dashboard_expressions():
            for metric in identifiers(expr):
                seen.add(metric)
                with self.subTest(where=where, metric=metric):
                    if metric.startswith("factory_"):
                        self.assertIn(base_name(metric), catalogue)
                    else:
                        self.assertIn(metric, PLATFORM_METRICS)
        self.assertGreaterEqual(len([name for name in seen if name.startswith("factory_")]), 25)

    def test_the_dashboards_cover_the_inventory_of_the_issue(self):
        text = " ".join(expr for _, expr in dashboard_expressions())
        for expected in ("factory_jobs{", "factory_jobs_finished_total", "factory_cost_usd_total", "factory_plan_to_pr_seconds_bucket", "factory_phase_seconds_bucket", "factory_job_reason_codes_total", "factory_watcher_skips_total", "factory_manager_auth_failures_total", "container_memory_working_set_bytes", "container_spec_memory_limit_bytes", "factory_capacity_workers", "factory_data_disk_bytes"):
            with self.subTest(expected=expected):
                self.assertIn(expected, text)

    def test_the_text_of_a_dashboard_is_static(self):
        for name, board in dashboards().items():
            blob = json.dumps(board)
            with self.subTest(dashboard=name):
                self.assertNotIn("http://", blob)
                self.assertNotIn("https://", blob)
                self.assertEqual(board["links"], [])


if __name__ == "__main__":
    unittest.main()

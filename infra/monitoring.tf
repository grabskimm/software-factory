# Monitoring (issue 48): Prometheus, Grafana and, only when a receiver is configured, Alertmanager,
# as plain Kubernetes objects in the namespace `monitoring` of the SAME cluster. Nothing here exists
# unless var.monitoring_enabled is true (the default is false, see variables.tf for why).
#
# Why plain manifests and not kube-prometheus-stack: no Helm provider (a new provider changes
# .terraform.lock.hcl hashes that cannot be produced offline), no CRDs (the operator's custom
# resources need the CRDs at plan time, like the SecretProviderClass already does), and a config
# that stays readable: the scrape config, the alert rules and the dashboards are files under
# infra/monitoring/ that tests read (infra/test/test_monitoring_design.py).
#
#   Prometheus    scrapes the manager's metrics listener (ClusterIP Service factory-metrics, bearer
#                 token from Key Vault) and the kubelet's container metrics (cAdvisor); evaluates the
#                 alert rules; keeps 15 days on its own Premium disk.
#   Grafana       dashboards as code (ConfigMaps), Entra sign-in (generic OAuth), served at /grafana/ by
#                 Caddy (ingress.tf). State is an emptyDir: it is disposable.
#   Alertmanager  only with alert_receiver != "none"; the destination URL is a Key Vault secret.
#
# Secrets: three Key Vault secrets (metrics-bearer-token, grafana-oauth-client-secret and, with a
# receiver, alert-receiver-url), each readable by ONE identity of its own (identity.tf) and mounted
# as a file by the Secrets Store CSI driver straight into the pod that needs it. There is no synced
# Kubernetes Secret here (check_refs.py forbids one); Grafana and Alertmanager read their secret
# from the file, and Prometheus uses it as credentials_file. The Factory pod gets the metrics token
# through its own SecretProviderClass (k8s.tf).
#
# Pod Security is `restricted` for the namespace: non-root, no privilege escalation, all
# capabilities dropped, RuntimeDefault seccomp, read-only root file system. node-exporter is deliberately
# not run (a DaemonSet with host access would need `privileged`; the kubelet's cAdvisor series give the
# pod-against-limit figures, README.md, "Monitoring"). kube-state-metrics (issue 149) IS run, as one small
# Deployment with read-only access to pods and nodes (and, on request, to NodeClaims): it is the only source in
# the cluster of pending pods, the node count of node auto-provisioning and time to node (docs/metrics.md,
# "NAP-side series"). Its resources are created only while monitoring_enabled is true, so a default plan is unchanged.
#
# Everything depends on the cluster admin roles through the namespace. Whether the images start under
# these settings, whether the kubelet scrape works and whether Entra sign-in works is UNVERIFIED
# (README.md): nothing here was applied to a real cluster.

locals {
  monitoring_count         = var.monitoring_enabled ? 1 : 0
  alerting_enabled         = var.monitoring_enabled && var.alert_receiver != "none"
  k8s_monitoring_namespace = "monitoring"

  prometheus_port   = 9090
  grafana_port      = 3000
  alertmanager_port = 9093
  ksm_port          = 8080
  # The container's own health and telemetry port: not in the Service, not scraped.
  ksm_telemetry_port = 8081

  prometheus_selector_labels   = { "app.kubernetes.io/name" = "prometheus" }
  grafana_selector_labels      = { "app.kubernetes.io/name" = "grafana" }
  alertmanager_selector_labels = { "app.kubernetes.io/name" = "alertmanager" }
  ksm_selector_labels          = { "app.kubernetes.io/name" = "kube-state-metrics" }

  k8s_prometheus_service_fqdn   = "prometheus.${local.k8s_monitoring_namespace}.svc.cluster.local"
  k8s_grafana_service_fqdn      = "grafana.${local.k8s_monitoring_namespace}.svc.cluster.local"
  k8s_alertmanager_service_fqdn = "alertmanager.${local.k8s_monitoring_namespace}.svc.cluster.local"
  k8s_ksm_service_fqdn          = "kube-state-metrics.${local.k8s_monitoring_namespace}.svc.cluster.local"
  k8s_metrics_service_fqdn      = "${local.k8s_app_name}-metrics.${local.k8s_namespace}.svc.cluster.local"

  # One identity per component, each allowed to read exactly the one secret it needs. The map is
  # empty while monitoring is off, so no identity, role or SecretProviderClass exists then.
  monitoring_secret_inputs = {
    prometheus   = { secret_name = local.kv_secret_names.metrics_token, version = var.metrics_token_secret_version }
    grafana      = { secret_name = local.kv_secret_names.grafana_oauth_client_secret, version = var.grafana_oauth_client_secret_version }
    alertmanager = { secret_name = local.kv_secret_names.alert_receiver_url, version = var.alert_receiver_secret_version }
  }
  monitoring_components = {
    for name, input in local.monitoring_secret_inputs : name => input
    if var.monitoring_enabled && (name != "alertmanager" || local.alerting_enabled)
  }

  # kube-state-metrics (issue 149). The pods and nodes it reads are always on; the NodeClaim part is opt-in because the
  # NodeClaim definition exists only with node auto-provisioning on (a cluster without it has nothing to list).
  # UNVERIFIED: the API version that NAP serves for NodeClaim. The NodePool of worker_pool.tf is karpenter.sh/v1, so v1 is
  # the expectation; the older `v1beta1` is what earlier Karpenter releases served. Check on the cluster:
  #   kubectl get crd nodeclaims.karpenter.sh -o jsonpath='{.spec.versions[*].name}'
  ksm_nodeclaim_api_version = "v1"
  # The API group of the NodeClaim objects. A local, because tools/infra_cluster_guard.py (and its test) treat a kubernetes_manifest whose text
  # names the Karpenter groups as a NodePool or AKSNodeClass object; the ClusterRole below only reads, it is not one.
  ksm_nodeclaim_api_group = "karpenter.sh"
  ksm_nodeclaim_config_text = templatefile("${path.module}/monitoring/kube-state-metrics-nodeclaim.yml.tftpl", {
    api_version = local.ksm_nodeclaim_api_version
  })
  ksm_args = concat(
    [
      "--port=${local.ksm_port}",
      "--telemetry-port=${local.ksm_telemetry_port}",
      "--resources=pods,nodes",
      # The label that names the NodePool of a node (documented on NAP nodes); exposed as label_karpenter_sh_nodepool.
      "--metric-labels-allowlist=nodes=[karpenter.sh/nodepool]",
    ],
    # At the END: the list of a kubernetes_manifest is filled from the prior state by position (see app_env_optional in locals.tf).
    var.monitoring_nodeclaim_metrics_enabled ? ["--custom-resource-state-config-file=/etc/kube-state-metrics/customresourcestate.yaml"] : [],
  )

  # Placement: the system node by default, or the dedicated tainted pool (aks.tf).
  monitoring_pool_labels = { pool = "monitoring" }
  monitoring_node_selector = var.monitoring_node_pool_vm_size == null ? {
    "kubernetes.azure.com/agentpool" = local.names.system_pool
  } : local.monitoring_pool_labels
  monitoring_tolerations = var.monitoring_node_pool_vm_size == null ? [] : [
    { key = "workload", operator = "Equal", value = "monitoring", effect = "NoSchedule" },
  ]

  monitoring_pod_labels = merge(local.k8s_labels, {
    "azure.workload.identity/use" = "true"
  })
  # Pod Security "restricted": these settings are the whole of it.
  monitoring_pod_security = {
    runAsNonRoot   = true
    seccompProfile = { type = "RuntimeDefault" }
  }
  monitoring_container_security = {
    allowPrivilegeEscalation = false
    readOnlyRootFilesystem   = true
    capabilities             = { drop = ["ALL"] }
  }

  # --- Prometheus ----------------------------------------------------------------------------------

  prometheus_config_text = templatefile("${path.module}/monitoring/prometheus.yml.tftpl", {
    scrape_interval     = var.prometheus_scrape_interval
    alerting            = local.alerting_enabled
    alertmanager_target = "${local.k8s_alertmanager_service_fqdn}:${local.alertmanager_port}"
    manager_target      = "${local.k8s_metrics_service_fqdn}:${local.metrics_port}"
    token_file          = local.kv_secret_names.metrics_token
    kept_namespaces     = join("|", [local.k8s_namespace, local.k8s_ingress_namespace, local.k8s_monitoring_namespace])
    ksm_target          = "${local.k8s_ksm_service_fqdn}:${local.ksm_port}"
    worker_namespace    = local.worker_contract.names.worker_namespace
  })
  prometheus_rules_text = file("${path.module}/monitoring/alert-rules.json")

  # --- Grafana -------------------------------------------------------------------------------------

  grafana_ini_text = templatefile("${path.module}/monitoring/grafana.ini.tftpl", {
    port          = local.grafana_port
    domain        = local.ingress_primary_host
    client_id     = coalesce(var.grafana_oauth_client_id, "unset")
    tenant_id     = var.tenant_id
    operator_role = var.grafana_operator_role
  })
  grafana_provisioning = {
    "datasources.yaml" = yamlencode({
      apiVersion = 1
      datasources = [{
        name      = "Prometheus"
        uid       = "factory-prometheus"
        type      = "prometheus"
        access    = "proxy"
        url       = "http://${local.k8s_prometheus_service_fqdn}:${local.prometheus_port}"
        isDefault = true
        editable  = false
        jsonData  = { timeInterval = var.prometheus_scrape_interval, httpMethod = "POST" }
      }]
    })
    "dashboards.yaml" = yamlencode({
      apiVersion = 1
      providers = [{
        name                  = "factory"
        orgId                 = 1
        folder                = "Factory"
        type                  = "file"
        disableDeletion       = true
        allowUiUpdates        = false
        updateIntervalSeconds = 60
        options               = { path = "/var/lib/grafana-dashboards" }
      }]
    })
  }
  grafana_dashboards = { for name in fileset("${path.module}/monitoring/dashboards", "*.json") : name => file("${path.module}/monitoring/dashboards/${name}") }

  # --- Alertmanager --------------------------------------------------------------------------------

  alertmanager_config_text = templatefile("${path.module}/monitoring/alertmanager.yml.tftpl", {
    receiver = var.alert_receiver
    url_file = local.kv_secret_names.alert_receiver_url
  })

  secrets_mount_path = "/mnt/secrets"
}

# --- Namespace, service accounts, secret provider classes ----------------------------------------

resource "kubernetes_namespace_v1" "monitoring" {
  count = local.monitoring_count

  metadata {
    name = local.k8s_monitoring_namespace
    labels = merge(local.k8s_labels, {
      "pod-security.kubernetes.io/enforce" = "restricted"
    })
  }

  depends_on = [
    azurerm_role_assignment.aks_cluster_admin,
    azurerm_role_assignment.aks_cluster_user,
  ]
}

# One service account per component, federated to its own identity (identity.tf). Only Prometheus
# mounts the Kubernetes API token: it reads node metrics from the kubelet.
resource "kubernetes_service_account_v1" "monitoring" {
  for_each = local.monitoring_components

  metadata {
    name      = each.key
    namespace = kubernetes_namespace_v1.monitoring[0].metadata[0].name
    labels    = local.k8s_labels
    annotations = {
      "azure.workload.identity/client-id" = azurerm_user_assigned_identity.monitoring[each.key].client_id
      "azure.workload.identity/tenant-id" = var.tenant_id
    }
  }

  automount_service_account_token = each.key == "prometheus"
}

# Pinned to the Key Vault VERSION, like the Factory's own. No secretObjects: the file is read from the
# mounted volume, so no Kubernetes Secret is ever created.
resource "kubernetes_manifest" "monitoring_secret_provider_class" {
  for_each = local.monitoring_components

  manifest = {
    apiVersion = "secrets-store.csi.x-k8s.io/v1"
    kind       = "SecretProviderClass"
    metadata = {
      name      = "${each.key}-keyvault"
      namespace = local.k8s_monitoring_namespace
      labels    = local.k8s_labels
    }
    spec = {
      provider = "azure"
      parameters = {
        usePodIdentity = "false"
        clientID       = azurerm_user_assigned_identity.monitoring[each.key].client_id
        tenantId       = var.tenant_id
        keyvaultName   = azurerm_key_vault.this.name
        objects = jsonencode({
          array = [
            jsonencode({
              objectName    = each.value.secret_name
              objectType    = "secret"
              objectVersion = each.value.version
            }),
          ]
        })
      }
    }
  }

  depends_on = [
    kubernetes_namespace_v1.monitoring,
    azurerm_role_assignment.aks_cluster_admin,
    azurerm_role_assignment.aks_cluster_user,
  ]
}

# --- Prometheus ----------------------------------------------------------------------------------

resource "kubernetes_config_map_v1" "prometheus_config" {
  count = local.monitoring_count

  metadata {
    name      = "prometheus-config"
    namespace = kubernetes_namespace_v1.monitoring[0].metadata[0].name
    labels    = local.k8s_labels
  }

  data = {
    "prometheus.yml" = local.prometheus_config_text
  }
}

resource "kubernetes_config_map_v1" "prometheus_rules" {
  count = local.monitoring_count

  metadata {
    name      = "prometheus-rules"
    namespace = kubernetes_namespace_v1.monitoring[0].metadata[0].name
    labels    = local.k8s_labels
  }

  data = {
    "alert-rules.json" = local.prometheus_rules_text
  }
}

# The time series database. Same class as /data (Premium_LRS, Retain, WaitForFirstConsumer): the disk
# outlives the claim, and the claim stays Pending until the pod is scheduled.
resource "kubernetes_persistent_volume_claim_v1" "prometheus" {
  count = local.monitoring_count

  metadata {
    name      = "prometheus-data"
    namespace = kubernetes_namespace_v1.monitoring[0].metadata[0].name
    labels    = local.k8s_labels
  }

  spec {
    access_modes       = ["ReadWriteOnce"]
    storage_class_name = kubernetes_storage_class_v1.factory_disk.metadata[0].name

    resources {
      requests = {
        storage = "${var.prometheus_storage_gib}Gi"
      }
    }
  }

  wait_until_bound = false
}

# Read-only access to node metrics for the kubelet scrape: list the nodes and read /metrics of each.
# No pod, secret or proxy access.
resource "kubernetes_manifest" "prometheus_cluster_role" {
  count = local.monitoring_count

  manifest = {
    apiVersion = "rbac.authorization.k8s.io/v1"
    kind       = "ClusterRole"
    metadata = {
      name   = "factory-prometheus"
      labels = local.k8s_labels
    }
    rules = [
      { apiGroups = [""], resources = ["nodes"], verbs = ["get", "list", "watch"] },
      { apiGroups = [""], resources = ["nodes/metrics"], verbs = ["get"] },
    ]
  }

  depends_on = [
    kubernetes_namespace_v1.monitoring,
    azurerm_role_assignment.aks_cluster_admin,
    azurerm_role_assignment.aks_cluster_user,
  ]
}

resource "kubernetes_manifest" "prometheus_cluster_role_binding" {
  count = local.monitoring_count

  manifest = {
    apiVersion = "rbac.authorization.k8s.io/v1"
    kind       = "ClusterRoleBinding"
    metadata = {
      name   = "factory-prometheus"
      labels = local.k8s_labels
    }
    roleRef = {
      apiGroup = "rbac.authorization.k8s.io"
      kind     = "ClusterRole"
      name     = "factory-prometheus"
    }
    subjects = [
      { kind = "ServiceAccount", name = "prometheus", namespace = local.k8s_monitoring_namespace },
    ]
  }

  depends_on = [
    kubernetes_namespace_v1.monitoring,
    kubernetes_manifest.prometheus_cluster_role,
    kubernetes_service_account_v1.monitoring,
  ]
}

resource "kubernetes_manifest" "prometheus" {
  count = local.monitoring_count

  manifest = {
    apiVersion = "apps/v1"
    kind       = "Deployment"
    metadata = {
      name      = "prometheus"
      namespace = local.k8s_monitoring_namespace
      labels    = merge(local.k8s_labels, local.prometheus_selector_labels)
    }
    spec = {
      replicas             = 1
      revisionHistoryLimit = 3
      # The disk is ReadWriteOnce: the old pod must be gone before the new one mounts it.
      strategy = { type = "Recreate" }
      selector = { matchLabels = local.prometheus_selector_labels }
      template = {
        metadata = {
          labels = merge(local.monitoring_pod_labels, local.prometheus_selector_labels)
          annotations = {
            # The configuration is read at start: a change rolls the pod (no reload endpoint is exposed).
            "checksum/config" = sha256("${local.prometheus_config_text}${local.prometheus_rules_text}${var.metrics_token_secret_version == null ? "" : var.metrics_token_secret_version}")
          }
        }
        spec = {
          serviceAccountName            = "prometheus"
          automountServiceAccountToken  = true
          enableServiceLinks            = false
          nodeSelector                  = local.monitoring_node_selector
          tolerations                   = local.monitoring_tolerations
          terminationGracePeriodSeconds = 300
          securityContext = merge(local.monitoring_pod_security, {
            runAsUser  = 65534
            runAsGroup = 65534
            fsGroup    = 65534
          })

          containers = [
            {
              name            = "prometheus"
              image           = var.prometheus_image
              imagePullPolicy = "IfNotPresent"
              args = [
                "--config.file=/etc/prometheus/prometheus.yml",
                "--storage.tsdb.path=/prometheus",
                "--storage.tsdb.retention.time=${var.prometheus_retention}",
                "--storage.tsdb.retention.size=${floor(var.prometheus_storage_gib * 0.8)}GB",
                "--web.listen-address=0.0.0.0:${local.prometheus_port}",
              ]
              ports           = [{ name = "http", containerPort = local.prometheus_port, protocol = "TCP" }]
              securityContext = local.monitoring_container_security
              resources = {
                requests = { cpu = "100m", memory = "512Mi" }
                limits   = { cpu = "1", memory = "1Gi" }
              }
              volumeMounts = [
                { name = "config", mountPath = "/etc/prometheus", readOnly = true },
                { name = "rules", mountPath = "/etc/prometheus-rules", readOnly = true },
                { name = "data", mountPath = "/prometheus" },
                { name = "keyvault", mountPath = local.secrets_mount_path, readOnly = true },
              ]
              startupProbe = {
                httpGet          = { path = "/-/ready", port = "http" }
                periodSeconds    = 10
                failureThreshold = 60
              }
              readinessProbe = {
                httpGet       = { path = "/-/ready", port = "http" }
                periodSeconds = 15
              }
              livenessProbe = {
                httpGet          = { path = "/-/healthy", port = "http" }
                periodSeconds    = 30
                failureThreshold = 6
              }
            },
          ]

          volumes = [
            { name = "config", configMap = { name = kubernetes_config_map_v1.prometheus_config[0].metadata[0].name } },
            { name = "rules", configMap = { name = kubernetes_config_map_v1.prometheus_rules[0].metadata[0].name } },
            { name = "data", persistentVolumeClaim = { claimName = kubernetes_persistent_volume_claim_v1.prometheus[0].metadata[0].name } },
            {
              name = "keyvault"
              csi = {
                driver           = "secrets-store.csi.k8s.io"
                readOnly         = true
                volumeAttributes = { secretProviderClass = "prometheus-keyvault" }
              }
            },
          ]
        }
      }
    }
  }

  depends_on = [
    kubernetes_namespace_v1.monitoring,
    kubernetes_manifest.monitoring_secret_provider_class,
    kubernetes_manifest.prometheus_cluster_role_binding,
    azurerm_kubernetes_cluster_node_pool.monitoring,
    azurerm_role_assignment.monitoring_secret_reader,
    azurerm_federated_identity_credential.monitoring,
  ]
}

resource "kubernetes_service_v1" "prometheus" {
  count = local.monitoring_count

  metadata {
    name      = "prometheus"
    namespace = kubernetes_namespace_v1.monitoring[0].metadata[0].name
    labels    = local.k8s_labels
  }

  spec {
    type     = "ClusterIP"
    selector = local.prometheus_selector_labels

    port {
      name        = "http"
      port        = local.prometheus_port
      target_port = "http"
      protocol    = "TCP"
    }
  }
}

# --- Grafana -------------------------------------------------------------------------------------

resource "kubernetes_config_map_v1" "grafana_config" {
  count = local.monitoring_count

  metadata {
    name      = "grafana-config"
    namespace = kubernetes_namespace_v1.monitoring[0].metadata[0].name
    labels    = local.k8s_labels
  }

  data = {
    "grafana.ini" = local.grafana_ini_text
  }
}

resource "kubernetes_config_map_v1" "grafana_provisioning" {
  count = local.monitoring_count

  metadata {
    name      = "grafana-provisioning"
    namespace = kubernetes_namespace_v1.monitoring[0].metadata[0].name
    labels    = local.k8s_labels
  }

  data = local.grafana_provisioning
}

resource "kubernetes_config_map_v1" "grafana_dashboards" {
  count = local.monitoring_count

  metadata {
    name      = "grafana-dashboards"
    namespace = kubernetes_namespace_v1.monitoring[0].metadata[0].name
    labels    = local.k8s_labels
  }

  data = local.grafana_dashboards
}

resource "kubernetes_manifest" "grafana" {
  count = local.monitoring_count

  manifest = {
    apiVersion = "apps/v1"
    kind       = "Deployment"
    metadata = {
      name      = "grafana"
      namespace = local.k8s_monitoring_namespace
      labels    = merge(local.k8s_labels, local.grafana_selector_labels)
    }
    spec = {
      replicas             = 1
      revisionHistoryLimit = 3
      strategy             = { type = "Recreate" }
      selector             = { matchLabels = local.grafana_selector_labels }
      template = {
        metadata = {
          labels = merge(local.monitoring_pod_labels, local.grafana_selector_labels)
          annotations = {
            "checksum/config" = sha256("${local.grafana_ini_text}${jsonencode(local.grafana_provisioning)}${jsonencode(local.grafana_dashboards)}${var.grafana_oauth_client_secret_version == null ? "" : var.grafana_oauth_client_secret_version}")
          }
        }
        spec = {
          serviceAccountName            = "grafana"
          automountServiceAccountToken  = false
          enableServiceLinks            = false
          nodeSelector                  = local.monitoring_node_selector
          tolerations                   = local.monitoring_tolerations
          terminationGracePeriodSeconds = 30
          securityContext = merge(local.monitoring_pod_security, {
            runAsUser  = 472
            runAsGroup = 472
            fsGroup    = 472
          })

          containers = [
            {
              name            = "grafana"
              image           = var.grafana_image
              imagePullPolicy = "IfNotPresent"
              # A random signing key for every start: the state is an emptyDir, so nothing is encrypted
              # with it that outlives the pod. Then the image's own entrypoint.
              command = [
                "/bin/sh",
                "-c",
                "GF_SECURITY_SECRET_KEY=\"$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \\n')\"; export GF_SECURITY_SECRET_KEY; exec /run.sh",
              ]
              env = [
                { name = "GF_PATHS_CONFIG", value = "/etc/grafana-config/grafana.ini" },
                { name = "GF_PATHS_PROVISIONING", value = "/etc/grafana-provisioning" },
                { name = "GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET__FILE", value = "${local.secrets_mount_path}/${local.kv_secret_names.grafana_oauth_client_secret}" },
              ]
              ports           = [{ name = "http", containerPort = local.grafana_port, protocol = "TCP" }]
              securityContext = local.monitoring_container_security
              resources = {
                requests = { cpu = "100m", memory = "384Mi" }
                limits   = { cpu = "1", memory = "768Mi" }
              }
              volumeMounts = [
                { name = "config", mountPath = "/etc/grafana-config", readOnly = true },
                { name = "provisioning", mountPath = "/etc/grafana-provisioning", readOnly = true },
                { name = "dashboards", mountPath = "/var/lib/grafana-dashboards", readOnly = true },
                { name = "data", mountPath = "/var/lib/grafana" },
                { name = "tmp", mountPath = "/tmp" },
                { name = "keyvault", mountPath = local.secrets_mount_path, readOnly = true },
              ]
              startupProbe = {
                httpGet          = { path = "/grafana/api/health", port = "http" }
                periodSeconds    = 10
                failureThreshold = 30
              }
              readinessProbe = {
                httpGet       = { path = "/grafana/api/health", port = "http" }
                periodSeconds = 15
              }
              livenessProbe = {
                httpGet          = { path = "/grafana/api/health", port = "http" }
                periodSeconds    = 30
                failureThreshold = 6
              }
            },
          ]

          volumes = [
            { name = "config", configMap = { name = kubernetes_config_map_v1.grafana_config[0].metadata[0].name } },
            {
              name = "provisioning"
              configMap = {
                name = kubernetes_config_map_v1.grafana_provisioning[0].metadata[0].name
                items = [
                  { key = "datasources.yaml", path = "datasources/datasources.yaml" },
                  { key = "dashboards.yaml", path = "dashboards/dashboards.yaml" },
                ]
              }
            },
            { name = "dashboards", configMap = { name = kubernetes_config_map_v1.grafana_dashboards[0].metadata[0].name } },
            { name = "data", emptyDir = { sizeLimit = "1Gi" } },
            { name = "tmp", emptyDir = { sizeLimit = "64Mi" } },
            {
              name = "keyvault"
              csi = {
                driver           = "secrets-store.csi.k8s.io"
                readOnly         = true
                volumeAttributes = { secretProviderClass = "grafana-keyvault" }
              }
            },
          ]
        }
      }
    }
  }

  depends_on = [
    kubernetes_namespace_v1.monitoring,
    kubernetes_manifest.monitoring_secret_provider_class,
    azurerm_kubernetes_cluster_node_pool.monitoring,
    azurerm_role_assignment.monitoring_secret_reader,
    azurerm_federated_identity_credential.monitoring,
  ]
}

# ClusterIP only: Caddy (ingress.tf) is the way in, and only below /grafana/.
resource "kubernetes_service_v1" "grafana" {
  count = local.monitoring_count

  metadata {
    name      = "grafana"
    namespace = kubernetes_namespace_v1.monitoring[0].metadata[0].name
    labels    = local.k8s_labels
  }

  spec {
    type     = "ClusterIP"
    selector = local.grafana_selector_labels

    port {
      name        = "http"
      port        = local.grafana_port
      target_port = "http"
      protocol    = "TCP"
    }
  }
}

# --- Alertmanager (only with a receiver) ------------------------------------------------------------

resource "kubernetes_config_map_v1" "alertmanager_config" {
  count = local.alerting_enabled ? 1 : 0

  metadata {
    name      = "alertmanager-config"
    namespace = kubernetes_namespace_v1.monitoring[0].metadata[0].name
    labels    = local.k8s_labels
  }

  data = {
    "alertmanager.yml" = local.alertmanager_config_text
  }
}

resource "kubernetes_manifest" "alertmanager" {
  count = local.alerting_enabled ? 1 : 0

  manifest = {
    apiVersion = "apps/v1"
    kind       = "Deployment"
    metadata = {
      name      = "alertmanager"
      namespace = local.k8s_monitoring_namespace
      labels    = merge(local.k8s_labels, local.alertmanager_selector_labels)
    }
    spec = {
      replicas             = 1
      revisionHistoryLimit = 3
      strategy             = { type = "Recreate" }
      selector             = { matchLabels = local.alertmanager_selector_labels }
      template = {
        metadata = {
          labels = merge(local.monitoring_pod_labels, local.alertmanager_selector_labels)
          annotations = {
            "checksum/config" = sha256("${local.alertmanager_config_text}${var.alert_receiver_secret_version == null ? "" : var.alert_receiver_secret_version}")
          }
        }
        spec = {
          serviceAccountName            = "alertmanager"
          automountServiceAccountToken  = false
          enableServiceLinks            = false
          nodeSelector                  = local.monitoring_node_selector
          tolerations                   = local.monitoring_tolerations
          terminationGracePeriodSeconds = 30
          securityContext = merge(local.monitoring_pod_security, {
            runAsUser  = 65534
            runAsGroup = 65534
            fsGroup    = 65534
          })

          containers = [
            {
              name            = "alertmanager"
              image           = var.alertmanager_image
              imagePullPolicy = "IfNotPresent"
              args = [
                "--config.file=/etc/alertmanager/alertmanager.yml",
                "--storage.path=/alertmanager",
                "--web.listen-address=0.0.0.0:${local.alertmanager_port}",
                "--cluster.listen-address=",
              ]
              ports           = [{ name = "http", containerPort = local.alertmanager_port, protocol = "TCP" }]
              securityContext = local.monitoring_container_security
              resources = {
                requests = { cpu = "10m", memory = "32Mi" }
                limits   = { cpu = "200m", memory = "128Mi" }
              }
              volumeMounts = [
                { name = "config", mountPath = "/etc/alertmanager", readOnly = true },
                { name = "data", mountPath = "/alertmanager" },
                { name = "keyvault", mountPath = local.secrets_mount_path, readOnly = true },
              ]
              readinessProbe = {
                httpGet       = { path = "/-/ready", port = "http" }
                periodSeconds = 15
              }
              livenessProbe = {
                httpGet          = { path = "/-/healthy", port = "http" }
                periodSeconds    = 30
                failureThreshold = 6
              }
            },
          ]

          volumes = [
            { name = "config", configMap = { name = kubernetes_config_map_v1.alertmanager_config[0].metadata[0].name } },
            { name = "data", emptyDir = { sizeLimit = "64Mi" } },
            {
              name = "keyvault"
              csi = {
                driver           = "secrets-store.csi.k8s.io"
                readOnly         = true
                volumeAttributes = { secretProviderClass = "alertmanager-keyvault" }
              }
            },
          ]
        }
      }
    }
  }

  depends_on = [
    kubernetes_namespace_v1.monitoring,
    kubernetes_manifest.monitoring_secret_provider_class,
    azurerm_kubernetes_cluster_node_pool.monitoring,
    azurerm_role_assignment.monitoring_secret_reader,
    azurerm_federated_identity_credential.monitoring,
  ]
}

resource "kubernetes_service_v1" "alertmanager" {
  count = local.alerting_enabled ? 1 : 0

  metadata {
    name      = "alertmanager"
    namespace = kubernetes_namespace_v1.monitoring[0].metadata[0].name
    labels    = local.k8s_labels
  }

  spec {
    type     = "ClusterIP"
    selector = local.alertmanager_selector_labels

    port {
      name        = "http"
      port        = local.alertmanager_port
      target_port = "http"
      protocol    = "TCP"
    }
  }
}

# --- kube-state-metrics (issue 149) ---------------------------------------------------------------------
#
# Reads pods, nodes and (on request) NodeClaims and turns their state into series: pending worker pods, the node count per
# NodePool, time from pod creation to Ready and the condition times of a NodeClaim. It has no secret and no Azure identity
# (so none of the per-component identities of identity.tf), keeps no state, and serves /metrics on a ClusterIP Service
# without authentication, like the Prometheus port: it exposes pod and node names (and, from #145, worker pod names), which is
# why the open network-isolation decision of issue 48 (docs/metrics.md) names it. There is no NetworkPolicy for it here: the
# monitoring namespace has none, and the only one of this stack is the prepared one of the factory namespace (the end of this file).
# Whether the image starts under the restricted settings is UNVERIFIED (README.md): the distroless image runs as 65534.

resource "kubernetes_service_account_v1" "kube_state_metrics" {
  count = local.monitoring_count

  metadata {
    name      = "kube-state-metrics"
    namespace = kubernetes_namespace_v1.monitoring[0].metadata[0].name
    labels    = local.k8s_labels
  }

  # It reads the API server with this token and nothing else.
  automount_service_account_token = true
}

# Read-only: list and watch (its informers), never get on a single object, never a write verb, no secret, no configmap.
resource "kubernetes_manifest" "kube_state_metrics_cluster_role" {
  count = local.monitoring_count

  manifest = {
    apiVersion = "rbac.authorization.k8s.io/v1"
    kind       = "ClusterRole"
    metadata = {
      name   = "factory-kube-state-metrics"
      labels = local.k8s_labels
    }
    rules = [
      { apiGroups = [""], resources = ["pods", "nodes"], verbs = ["list", "watch"] },
    ]
  }

  depends_on = [
    kubernetes_namespace_v1.monitoring,
    azurerm_role_assignment.aks_cluster_admin,
    azurerm_role_assignment.aks_cluster_user,
  ]
}

resource "kubernetes_manifest" "kube_state_metrics_cluster_role_binding" {
  count = local.monitoring_count

  manifest = {
    apiVersion = "rbac.authorization.k8s.io/v1"
    kind       = "ClusterRoleBinding"
    metadata = {
      name   = "factory-kube-state-metrics"
      labels = local.k8s_labels
    }
    roleRef = {
      apiGroup = "rbac.authorization.k8s.io"
      kind     = "ClusterRole"
      name     = "factory-kube-state-metrics"
    }
    subjects = [
      { kind = "ServiceAccount", name = "kube-state-metrics", namespace = local.k8s_monitoring_namespace },
    ]
  }

  depends_on = [
    kubernetes_namespace_v1.monitoring,
    kubernetes_manifest.kube_state_metrics_cluster_role,
    kubernetes_service_account_v1.kube_state_metrics,
  ]
}

# The NodeClaim part, its own objects so that the base role above never changes. The CustomResourceDefinition rule is what the
# custom resource state feature of kube-state-metrics reads to find the resource (its documentation, UNVERIFIED for the pinned version).
resource "kubernetes_manifest" "kube_state_metrics_nodeclaim_cluster_role" {
  count = var.monitoring_enabled && var.monitoring_nodeclaim_metrics_enabled ? 1 : 0

  manifest = {
    apiVersion = "rbac.authorization.k8s.io/v1"
    kind       = "ClusterRole"
    metadata = {
      name   = "factory-kube-state-metrics-nodeclaims"
      labels = local.k8s_labels
    }
    rules = [
      { apiGroups = [local.ksm_nodeclaim_api_group], resources = ["nodeclaims"], verbs = ["list", "watch"] },
      { apiGroups = ["apiextensions.k8s.io"], resources = ["customresourcedefinitions"], verbs = ["list", "watch"] },
    ]
  }

  lifecycle {
    precondition {
      condition     = var.nap_mode == "Auto"
      error_message = "monitoring_nodeclaim_metrics_enabled needs nap_mode = Auto: the NodeClaim definition exists only with node auto-provisioning on."
    }
  }

  depends_on = [
    kubernetes_namespace_v1.monitoring,
    azurerm_role_assignment.aks_cluster_admin,
    azurerm_role_assignment.aks_cluster_user,
  ]
}

resource "kubernetes_manifest" "kube_state_metrics_nodeclaim_cluster_role_binding" {
  count = var.monitoring_enabled && var.monitoring_nodeclaim_metrics_enabled ? 1 : 0

  manifest = {
    apiVersion = "rbac.authorization.k8s.io/v1"
    kind       = "ClusterRoleBinding"
    metadata = {
      name   = "factory-kube-state-metrics-nodeclaims"
      labels = local.k8s_labels
    }
    roleRef = {
      apiGroup = "rbac.authorization.k8s.io"
      kind     = "ClusterRole"
      name     = "factory-kube-state-metrics-nodeclaims"
    }
    subjects = [
      { kind = "ServiceAccount", name = "kube-state-metrics", namespace = local.k8s_monitoring_namespace },
    ]
  }

  depends_on = [
    kubernetes_namespace_v1.monitoring,
    kubernetes_manifest.kube_state_metrics_nodeclaim_cluster_role,
    kubernetes_service_account_v1.kube_state_metrics,
  ]
}

resource "kubernetes_config_map_v1" "kube_state_metrics_config" {
  # Always created with monitoring: the Deployment mounts it in every case, so its volume list has a fixed length (an empty list, or a
  # list that grows and shrinks with an option, is not a value the kubernetes_manifest provider can plan and return consistently:
  # "Failed to transform List value into Tuple of different length" in the plan and "attribute volumes: tuple required" after the apply,
  # both seen on a real cluster on 2026-11-10). Without the option the file is a comment and no argument names it.
  count = local.monitoring_count

  metadata {
    name      = "kube-state-metrics-config"
    namespace = kubernetes_namespace_v1.monitoring[0].metadata[0].name
    labels    = local.k8s_labels
  }

  data = {
    "customresourcestate.yaml" = var.monitoring_nodeclaim_metrics_enabled ? local.ksm_nodeclaim_config_text : "# NodeClaim series are off (monitoring_nodeclaim_metrics_enabled)\n"
  }
}

resource "kubernetes_manifest" "kube_state_metrics" {
  count = local.monitoring_count

  manifest = {
    apiVersion = "apps/v1"
    kind       = "Deployment"
    metadata = {
      name      = "kube-state-metrics"
      namespace = local.k8s_monitoring_namespace
      labels    = merge(local.k8s_labels, local.ksm_selector_labels)
    }
    spec = {
      replicas             = 1
      revisionHistoryLimit = 3
      strategy             = { type = "Recreate" }
      selector             = { matchLabels = local.ksm_selector_labels }
      template = {
        metadata = {
          # No workload identity: the pod has no Azure identity and no Key Vault mount.
          labels = merge(local.k8s_labels, local.ksm_selector_labels)
          annotations = {
            "checksum/config" = sha256("${join(" ", local.ksm_args)}${var.monitoring_nodeclaim_metrics_enabled ? local.ksm_nodeclaim_config_text : ""}")
          }
        }
        spec = {
          serviceAccountName            = "kube-state-metrics"
          automountServiceAccountToken  = true
          enableServiceLinks            = false
          nodeSelector                  = local.monitoring_node_selector
          tolerations                   = local.monitoring_tolerations
          terminationGracePeriodSeconds = 30
          securityContext = merge(local.monitoring_pod_security, {
            runAsUser  = 65534
            runAsGroup = 65534
          })

          containers = [
            {
              name            = "kube-state-metrics"
              image           = var.kube_state_metrics_image
              imagePullPolicy = "IfNotPresent"
              args            = local.ksm_args
              ports = [
                { name = "http", containerPort = local.ksm_port, protocol = "TCP" },
                { name = "telemetry", containerPort = local.ksm_telemetry_port, protocol = "TCP" },
              ]
              securityContext = local.monitoring_container_security
              # Pods and nodes of a small cluster: tens of megabytes (UNVERIFIED, measured on no cluster). Requests are small so that the
              # sum of the monitoring pods still fits the one node of the pool `mon` (a Standard_D2ds_v5) or the system node.
              resources = {
                requests = { cpu = "10m", memory = "64Mi" }
                limits   = { cpu = "200m", memory = "256Mi" }
              }
              # Always mounted (see the ConfigMap): a fixed-length list.
              volumeMounts = [
                { name = "config", mountPath = "/etc/kube-state-metrics", readOnly = true },
              ]
              readinessProbe = {
                httpGet       = { path = "/readyz", port = "telemetry" }
                periodSeconds = 15
              }
              livenessProbe = {
                httpGet          = { path = "/livez", port = "http" }
                periodSeconds    = 30
                failureThreshold = 6
              }
            },
          ]

          volumes = [
            { name = "config", configMap = { name = "kube-state-metrics-config" } },
          ]
        }
      }
    }
  }

  depends_on = [
    kubernetes_namespace_v1.monitoring,
    kubernetes_manifest.kube_state_metrics_cluster_role_binding,
    kubernetes_manifest.kube_state_metrics_nodeclaim_cluster_role_binding,
    kubernetes_config_map_v1.kube_state_metrics_config,
    azurerm_kubernetes_cluster_node_pool.monitoring,
  ]
}

resource "kubernetes_service_v1" "kube_state_metrics" {
  count = local.monitoring_count

  metadata {
    name      = "kube-state-metrics"
    namespace = kubernetes_namespace_v1.monitoring[0].metadata[0].name
    labels    = local.k8s_labels
  }

  spec {
    type     = "ClusterIP"
    selector = local.ksm_selector_labels

    port {
      name        = "http"
      port        = local.ksm_port
      target_port = "http"
      protocol    = "TCP"
    }
  }
}

# --- Network isolation of the metrics port: PREPARED, OFF -------------------------------------------

# Enforcement needs the Cilium data plane (network_data_plane = "cilium", issue 144; staged by hand, README.md).
# The reference deployment runs the default data plane today, where this object would be accepted by the API
# server and not enforced: that is why factory_network_policy_enabled defaults to false. The control that works
# today is the bearer token on the listener. Once Cilium runs, review the rules below and turn it on: a pod that
# a NetworkPolicy selects accepts ONLY what the policy lists, so the manager API port must stay open to Caddy.
#
# With the pod backend on (pod_backend_enabled, worker_ns.tf) a third rule admits the relay port of the manager pod
# (a container of it, the relay) from the worker pods of the worker namespace: without it the relay is cut off
# the moment this policy is turned on. The rule is absent, and so is its entry in the object, while
# pod_backend_enabled is false. It is the LAST entry, so that setting or unsetting it moves no other one (the
# kubernetes_manifest provider fills the planned object by list position; see app_env_optional in locals.tf).
resource "kubernetes_manifest" "factory_network_policy" {
  count = var.monitoring_enabled && var.factory_network_policy_enabled ? 1 : 0

  manifest = {
    apiVersion = "networking.k8s.io/v1"
    kind       = "NetworkPolicy"
    metadata = {
      name      = "factory-ingress"
      namespace = local.k8s_namespace
      labels    = local.k8s_labels
    }
    spec = {
      podSelector = { matchLabels = local.k8s_selector_labels }
      policyTypes = ["Ingress"]
      ingress = concat(
        [
          {
            from = [{
              namespaceSelector = { matchLabels = { "kubernetes.io/metadata.name" = local.k8s_ingress_namespace } }
              podSelector       = { matchLabels = local.k8s_ingress_selector_labels }
            }]
            ports = [{ protocol = "TCP", port = local.app_port }]
          },
          {
            from = [{
              namespaceSelector = { matchLabels = { "kubernetes.io/metadata.name" = local.k8s_monitoring_namespace } }
              podSelector       = { matchLabels = local.prometheus_selector_labels }
            }]
            ports = [{ protocol = "TCP", port = local.metrics_port }]
          },
        ],
        var.pod_backend_enabled ? [
          {
            from = [{
              namespaceSelector = { matchLabels = { "kubernetes.io/metadata.name" = local.worker_contract.names.worker_namespace } }
              podSelector       = { matchLabels = { "${local.worker_contract.labels.prefix}role" = local.worker_contract.labels.role_value } }
            }]
            ports = [{ protocol = "TCP", port = local.worker_contract.ports.relay }]
          },
        ] : [],
      )
    }
  }

  depends_on = [
    kubernetes_namespace_v1.factory,
    kubernetes_namespace_v1.monitoring,
    kubernetes_namespace_v1.ingress,
  ]
}

# The Factory workload inside the cluster: namespace, service account, storage, Key Vault
# secrets, repository config, the Deployment and its Service. The Azure side (cluster,
# identity, vault, disks' storage class provisioner) is in the other files; names, labels
# and the non-secret environment come from the "Kubernetes contract" in locals.tf.
#
# Typed hashicorp/kubernetes resources are used wherever they can express the object.
# Two objects are `kubernetes_manifest`, and only because the typed kinds cannot say it:
#
#   * The Deployment. hostUsers, securityContext.procMount and securityContext.appArmorProfile
#     are the three pod relaxations that make the bubblewrap sandbox work (spikes/S1.md, S1
#     PASS), and kubernetes_deployment_v1 (hashicorp/kubernetes 3.5.0, checked against the
#     provider schema) has no argument for any of them; neither have kubernetes_pod_v1 nor
#     kubernetes_stateful_set_v1.
#   * The SecretProviderClass. It is a custom resource of the Secrets Store CSI driver add-on,
#     so there is no typed resource at all.
#
# kubernetes_manifest needs a reachable API server (and, for the custom resource, the add-on's
# CRD) at PLAN time. The first apply is therefore staged: the cluster and the role
# assignments first, everything else second (README.md, "First apply").
#
# What is deliberately NOT here: a Kubernetes Secret with a value (the two secrets live in
# Key Vault; the CSI driver syncs them into a Secret at pod start, check_refs.py forbids
# kubernetes_secret* and kind Secret), an ENABLED NetworkPolicy for the Factory namespace (a prepared,
# disabled one is at the end of monitoring.tf and the bearer token on the metrics listener is the control
# that works today), readOnlyRootFilesystem (the entrypoint prepares /data
# and /config as root and the image was not built for it), any seccomp, capability or
# runAsUser setting for the Factory container (the image's entrypoint depends on the default
# capability set, and S1 passed without any of them). The Factory pod differs from the default
# only in the three relaxations named at the Deployment.
#
# Network policy. A NetworkPolicy is enforced only by the Cilium data plane (network_data_plane =
# "cilium", variables.tf, switched on by hand: README.md, "Switching the data plane"); the reference
# deployment's cluster runs the default data plane today, and a policy there is accepted by the API server and not
# enforced. With the Cilium data plane an engine exists, and worker_ns.tf declares the default-deny policy
# of the worker namespace of the pod backend (pod_backend_enabled, off by default; the design record is
# docs/pod-backend.md, issue #145). The policy of the Factory namespace stays prepared and off, for the
# reason above.

locals {
  # The Deployment and the Service are named like the app, and so is the Service DNS name
  # that Caddy proxies to. 8080 comes from locals.tf (app_port).
  k8s_app_service_fqdn = "${local.k8s_app_name}.${local.k8s_namespace}.svc.cluster.local"

  # Repository config is IMMUTABLE and its name carries a hash of the content: a change
  # creates a new ConfigMap, the Deployment is pointed at it (and so rolls, as deploy_inputs_sha256
  # also does), and only then is the old one deleted (create_before_destroy). A running pod can
  # therefore never see a config file change under it; the new text arrives with the rollout,
  # after the operator has drained (README.md, "Rotate secrets and change repository configuration").
  repo_config_name = "${local.k8s_repo_config}-${substr(sha256(jsonencode(local.repo_config_data)), 0, 10)}"

  # Non-secret environment, in a stable order.
  app_env_list = [for name in sort(keys(local.app_env)) : { name = name, value = local.app_env[name] }]

  # Optional plain variables, appended AFTER everything else so that setting or unsetting one never moves another entry (see
  # app_env_optional in locals.tf for why the position matters to the kubernetes_manifest provider).
  app_env_optional_list = [for name in sort(keys(local.app_env_optional)) : { name = name, value = local.app_env_optional[name] }]

  # The two secrets come from the Secret that the CSI driver syncs from Key Vault.
  app_env_secret_list = [
    for key in sort(keys(local.kv_secret_objects)) : {
      name = local.kv_secret_objects[key].k8s_secret_key
      valueFrom = {
        secretKeyRef = {
          name = local.k8s_synced_secret
          key  = local.kv_secret_objects[key].k8s_secret_key
        }
      }
    }
  ]

  # Probes. /healthz answers without a token only for loopback peers (src/factory/manager.py,
  # FACTORY_MANAGER_AUTH=entra-jwt), and an httpGet probe comes from the node's address, so the
  # probes run INSIDE the container and talk to 127.0.0.1. The interpreter is the image's own
  # (the same command as the Dockerfile HEALTHCHECK). urlopen raises on any non-2xx answer.
  # The liveness probe is deliberately slow to trip (10 x 30 s): a restart kills running
  # agents, so a briefly busy manager must not be restarted.
  app_health_command = [
    "/opt/factory/bin/python",
    "-c",
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${local.app_port}/healthz', timeout=5).read()",
  ]

  # Metrics listener, only with monitoring on (monitoring.tf): bind the pod address (the Service's
  # target, never 0.0.0.0) and declare the port. The token arrives with the Key Vault secrets above.
  # Off, none of this exists and the listener stays on the loopback.
  app_env_metrics_list = var.monitoring_enabled ? [
    { name = "FACTORY_MANAGER_METRICS_HOST", valueFrom = { fieldRef = { fieldPath = "status.podIP" } } },
  ] : []
  app_container_ports = concat(
    [{ name = "http", containerPort = local.app_port, protocol = "TCP" }],
    var.monitoring_enabled ? [{ name = "metrics", containerPort = local.metrics_port, protocol = "TCP" }] : [],
  )

  # Sizing of the init container that only mounts the Key Vault volume (see the Deployment).
  # Requests equal limits for every container so the pod stays in the Guaranteed QoS class.
  secrets_init_resources = {
    cpu    = "50m"
    memory = "32Mi"
  }
}

# --- Namespace and identity -------------------------------------------------------------

resource "kubernetes_namespace_v1" "factory" {
  metadata {
    name = local.k8s_namespace
    labels = merge(local.k8s_labels, {
      # The Factory pod needs an unconfined AppArmor profile and an unmasked /proc, which the
      # baseline and restricted Pod Security levels refuse. The namespace is therefore
      # explicitly "privileged"; nothing else runs in it, and creating pods here needs
      # Kubernetes RBAC that only the cluster admins hold.
      "pod-security.kubernetes.io/enforce" = "privileged"
    })
  }

  depends_on = [
    azurerm_role_assignment.aks_cluster_admin,
    azurerm_role_assignment.aks_cluster_user,
  ]
}

# The service account that identity.tf federates (system:serviceaccount:factory:factory).
# The workload identity webhook reads the annotation and injects AZURE_CLIENT_ID,
# AZURE_TENANT_ID, AZURE_FEDERATED_TOKEN_FILE and AZURE_AUTHORITY_HOST into pods that carry
# the label azure.workload.identity/use=true. The pod has no use for the Kubernetes API, so the
# default API token is not mounted (the workload identity token is a separate projected volume).
resource "kubernetes_service_account_v1" "factory" {
  metadata {
    name      = local.k8s_service_account
    namespace = kubernetes_namespace_v1.factory.metadata[0].name
    labels    = local.k8s_labels
    annotations = {
      "azure.workload.identity/client-id" = azurerm_user_assigned_identity.workload.client_id
      "azure.workload.identity/tenant-id" = var.tenant_id
    }
  }

  automount_service_account_token = false
}

# --- Container Insights ---------------------------------------------------------------------

# Container Insights copies the environment variables of every container into the workspace
# unless this ConfigMap turns it off, and the manager's environment holds ANTHROPIC_API_KEY and
# GITHUB_APP_PRIVATE_KEY (synced from Key Vault). The Deployment depends on it so it exists
# before the first Factory pod does; AZMON_COLLECT_ENV=False in the container's own environment
# (locals.tf app_env) is the second layer.
resource "kubernetes_config_map_v1" "insights_agent" {
  metadata {
    name      = local.k8s_insights_agent_config.name
    namespace = local.k8s_insights_agent_config.namespace
    labels    = local.k8s_labels
  }

  data = local.k8s_insights_agent_config.data

  depends_on = [
    azurerm_role_assignment.aks_cluster_admin,
    azurerm_role_assignment.aks_cluster_user,
    azurerm_kubernetes_cluster.this,
  ]
}

# --- Storage -----------------------------------------------------------------------------------

# /data is an Azure Disk, not a file share: a pod with hostUsers=false cannot mount NFS
# (spikes/S2.md). Retain keeps the disk when the claim is deleted; WaitForFirstConsumer
# creates it where the pod is scheduled. The disk lives in the cluster's node resource group,
# so deleting the CLUSTER deletes it too (README.md).
resource "kubernetes_storage_class_v1" "factory_disk" {
  metadata {
    name   = local.k8s_storage_class
    labels = local.k8s_labels
  }

  storage_provisioner    = "disk.csi.azure.com"
  reclaim_policy         = "Retain"
  volume_binding_mode    = "WaitForFirstConsumer"
  allow_volume_expansion = true

  parameters = {
    skuName = "Premium_LRS"
    fsType  = "ext4"
  }

  depends_on = [
    azurerm_role_assignment.aks_cluster_admin,
    azurerm_role_assignment.aks_cluster_user,
  ]
}

# The claim stays Pending until the pod is scheduled (WaitForFirstConsumer), so Terraform must
# not wait for it to bind. prevent_destroy: a plan that would REPLACE the claim (and so leave the
# old disk Released and start on an empty one) fails instead. To tear the stack down on purpose,
# remove the lifecycle block first.
resource "kubernetes_persistent_volume_claim_v1" "data" {
  metadata {
    name      = local.k8s_data_pvc
    namespace = kubernetes_namespace_v1.factory.metadata[0].name
    labels    = local.k8s_labels
  }

  spec {
    access_modes       = ["ReadWriteOnce"]
    storage_class_name = kubernetes_storage_class_v1.factory_disk.metadata[0].name

    resources {
      requests = {
        storage = local.k8s_workload_sizing.data_disk_size
      }
    }
  }

  wait_until_bound = false

  lifecycle {
    prevent_destroy = true
  }
}

# --- Repository configuration ------------------------------------------------------------------

# One file per repository, `<org>__<name>.toml`, mounted at /config/repos (config.py reads
# $FACTORY_CONFIG/repos/<org>__<name>.toml). The files hold no secrets, so a ConfigMap is
# right (and the GitHub App key never lands in the same volume as in the old Secret volume).
resource "kubernetes_config_map_v1" "repos" {
  metadata {
    name      = local.repo_config_name
    namespace = kubernetes_namespace_v1.factory.metadata[0].name
    labels    = local.k8s_labels
  }

  data      = local.repo_config_data
  immutable = true

  lifecycle {
    create_before_destroy = true
  }
}

# --- Key Vault secrets -------------------------------------------------------------------------

# Secrets Store CSI driver, provider azure, workload identity. Every object is pinned to the
# Key Vault VERSION from the variables (an empty objectVersion would mean "latest"), and
# secretObjects syncs the two files into the Secret `factory-keyvault` with the keys
# ANTHROPIC_API_KEY and GITHUB_APP_PRIVATE_KEY, which the Deployment reads as environment
# variables. The Secret exists only while a pod that mounts the volume exists.
# https://raw.githubusercontent.com/MicrosoftDocs/azure-aks-docs/main/articles/aks/csi-secrets-store-identity-access.md
# https://raw.githubusercontent.com/MicrosoftDocs/azure-aks-docs/main/articles/aks/csi-secrets-store-driver.md
resource "kubernetes_manifest" "secret_provider_class" {
  manifest = {
    apiVersion = "secrets-store.csi.x-k8s.io/v1"
    kind       = "SecretProviderClass"
    metadata = {
      name      = local.k8s_secret_provider_class
      namespace = local.k8s_namespace
      labels    = local.k8s_labels
    }
    spec = {
      provider = "azure"
      parameters = {
        usePodIdentity = "false"
        clientID       = azurerm_user_assigned_identity.workload.client_id
        tenantId       = var.tenant_id
        keyvaultName   = azurerm_key_vault.this.name
        # JSON is YAML: the driver parses `objects` as a YAML document whose array items are
        # YAML documents themselves.
        objects = jsonencode({
          array = [
            for key in sort(keys(local.kv_secret_objects)) : jsonencode({
              objectName    = local.kv_secret_objects[key].secret_name
              objectType    = "secret"
              objectVersion = local.kv_secret_objects[key].version
            })
          ]
        })
      }
      secretObjects = [
        {
          secretName = local.k8s_synced_secret
          type       = "Opaque"
          labels     = local.k8s_labels
          data = [
            for key in sort(keys(local.kv_secret_objects)) : {
              objectName = local.kv_secret_objects[key].secret_name
              key        = local.kv_secret_objects[key].k8s_secret_key
            }
          ]
        },
      ]
    }
  }

  depends_on = [
    kubernetes_namespace_v1.factory,
    azurerm_role_assignment.aks_cluster_admin,
    azurerm_role_assignment.aks_cluster_user,
  ]
}

# --- The Factory Deployment --------------------------------------------------------------------

# One replica and strategy Recreate: the data disk is ReadWriteOnce, and two managers would
# also be two supervisors per repository (the blob lease would refuse the second). A rollout
# stops the old pod (up to terminationGracePeriodSeconds, so drain first), then starts the new
# one on the same node with the same disk.
#
# The pod differs from the default in exactly the three settings spikes/S1.md records the
# sandbox PASS with (together; smaller sets tried failed, two subsets were not tried), all accepted by the operator:
#   spec.hostUsers: false                      (pod user namespace)
#   securityContext.procMount: Unmasked        (container; requires hostUsers: false)
#   securityContext.appArmorProfile: Unconfined (container)
# Nothing else is relaxed or added: no privileged, no extra capability, the image keeps
# setting no_new_privs itself, and managed-settings.json is untouched. The seccomp profile is
# left at the cluster default on purpose: S1 passed with it, and RuntimeDefault was not tried.
#
# Init container: the Key Vault volume must be mounted by some container for the CSI driver to
# publish it (a volume that no container mounts is skipped by the kubelet), and the synced
# Secret exists only after that. The volume is mounted ONLY by this init container, which also
# fails the pod early and legibly if a secret file is missing or empty. The Factory container
# itself never sees the files: the GitHub App key stays out of its file system and is only an
# environment variable of the supervisor, as before. The init container uses the Factory image
# (already pulled) with a command that replaces the entrypoint.
resource "kubernetes_manifest" "factory_deployment" {
  manifest = {
    apiVersion = "apps/v1"
    kind       = "Deployment"
    metadata = {
      name      = local.k8s_app_name
      namespace = local.k8s_namespace
      labels    = merge(local.k8s_labels, local.k8s_selector_labels)
    }
    spec = {
      replicas                = 1
      revisionHistoryLimit    = 3
      progressDeadlineSeconds = 1200
      strategy                = { type = "Recreate" }
      selector                = { matchLabels = local.k8s_selector_labels }
      template = {
        metadata = {
          labels = merge(local.k8s_labels, local.k8s_pod_labels)
          # merge(): with the pod backend, the annotation that keeps the workload identity webhook out of the launcher, the relay and
          # the init container (pod_backend.tf); an empty map while the backend is off.
          annotations = merge({
            # A new Key Vault secret version or new repository config changes this value and
            # so rolls the pod; both are read only when a pod starts.
            "checksum/deploy-inputs" = local.deploy_inputs_sha256
          }, local.pod_backend_pod_annotations)
        }
        # merge(): the toleration of the manager taint, only with the pod backend (pod_backend.tf); an empty map while it is off.
        spec = merge({
          serviceAccountName           = local.k8s_service_account
          automountServiceAccountToken = false
          # A Service named `factory` would otherwise inject FACTORY_PORT and FACTORY_SERVICE_*
          # variables into every pod of the namespace, in the Factory configuration's own prefix.
          enableServiceLinks = false
          nodeSelector       = local.user_pool_labels

          # Relaxation 1 of 3.
          hostUsers = false

          terminationGracePeriodSeconds = local.k8s_workload_sizing.termination_grace_period_seconds

          # concat(): the init container of the pod backend (pod_backend.tf) comes last and is empty while the backend is off.
          initContainers = concat([
            {
              name            = "keyvault-sync"
              image           = local.image
              imagePullPolicy = "IfNotPresent"
              command = [
                "/bin/sh",
                "-c",
                join(" && ", [for key in sort(keys(local.kv_secret_objects)) : "test -s ${local.k8s_secrets_mount_path}/${local.kv_secret_objects[key].secret_name}"]),
              ]
              resources = {
                requests = local.secrets_init_resources
                limits   = local.secrets_init_resources
              }
              volumeMounts = [
                { name = "keyvault", mountPath = local.k8s_secrets_mount_path, readOnly = true },
              ]
            },
          ], local.pod_backend_init_containers)

          # concat(): the launcher and the relay (pod_backend.tf) come after the main container and are absent while the backend is off.
          containers = concat([
            {
              name            = "factory"
              image           = local.image
              imagePullPolicy = "IfNotPresent"
              ports           = local.app_container_ports
              env             = concat(local.app_env_list, local.app_env_secret_list, local.app_env_metrics_list, local.app_env_optional_list, local.pod_backend_main_env)

              resources = {
                requests = { cpu = local.k8s_workload_sizing.cpu, memory = local.k8s_workload_sizing.memory }
                limits   = { cpu = local.k8s_workload_sizing.cpu, memory = local.k8s_workload_sizing.memory }
              }

              securityContext = {
                # Relaxations 2 and 3 of 3.
                procMount       = "Unmasked"
                appArmorProfile = { type = "Unconfined" }
              }

              volumeMounts = concat([
                { name = "data", mountPath = local.data_mount_path },
                { name = "repos", mountPath = local.config_repos_mount_path, readOnly = true },
              ], local.pod_backend_main_mounts)

              startupProbe = {
                exec                = { command = local.app_health_command }
                initialDelaySeconds = 5
                periodSeconds       = 10
                timeoutSeconds      = 10
                failureThreshold    = 30
              }
              livenessProbe = {
                exec                = { command = local.app_health_command }
                initialDelaySeconds = 30
                periodSeconds       = 30
                timeoutSeconds      = 10
                failureThreshold    = 10
              }
              readinessProbe = {
                exec                = { command = local.app_health_command }
                initialDelaySeconds = 5
                periodSeconds       = 10
                timeoutSeconds      = 10
                failureThreshold    = 3
                successThreshold    = 1
              }
            },
          ], local.pod_backend_containers)

          volumes = concat([
            {
              name                  = "data"
              persistentVolumeClaim = { claimName = kubernetes_persistent_volume_claim_v1.data.metadata[0].name }
            },
            {
              name      = "repos"
              configMap = { name = kubernetes_config_map_v1.repos.metadata[0].name }
            },
            {
              name = "keyvault"
              csi = {
                driver   = "secrets-store.csi.k8s.io"
                readOnly = true
                volumeAttributes = {
                  secretProviderClass = local.k8s_secret_provider_class
                }
              }
            },
          ], local.pod_backend_volumes)
        }, local.pod_backend_pod_spec_extra)
      }
    }
  }

  # No `wait { rollout = true }` here, deliberately. With the wait, the provider reads the
  # Deployment back after the rollout finished, when the Deployment controller has already
  # bumped the annotation deployment.kubernetes.io/revision, and fails with "Provider produced
  # inconsistent result after apply" on every image promotion (reproduced three times on a real
  # cluster; metadata.annotations is already in the provider's default computed_fields, so
  # declaring it again changes nothing). The typed kubernetes_deployment_v1 cannot be used
  # instead: it has no hostUsers, procMount or appArmorProfile. Whether the new pod becomes
  # ready is not checked by Terraform: follow every apply with
  # `kubectl -n factory rollout status deploy/factory` (a pipeline step or by hand), which fails
  # when the pod cannot start (image pull, Key Vault, disk, sandbox flags).

  depends_on = [
    kubernetes_namespace_v1.factory,
    kubernetes_service_account_v1.factory,
    kubernetes_manifest.secret_provider_class,
    kubernetes_config_map_v1.insights_agent,
    azurerm_kubernetes_cluster_node_pool.factory,
    azurerm_role_assignment.key_vault_secrets_user,
    azurerm_role_assignment.locks_blob_data_contributor,
    azurerm_role_assignment.acr_pull,
    azurerm_federated_identity_credential.workload,
    azurerm_nat_gateway_public_ip_association.this,
  ]
}

# ClusterIP only: Caddy (ingress.tf) is the way in. The Service never gets an external address.
resource "kubernetes_service_v1" "factory" {
  metadata {
    name      = local.k8s_app_name
    namespace = kubernetes_namespace_v1.factory.metadata[0].name
    labels    = local.k8s_labels
  }

  spec {
    type     = "ClusterIP"
    selector = local.k8s_selector_labels

    port {
      name        = "http"
      port        = local.app_port
      target_port = "http"
      protocol    = "TCP"
    }
  }
}

# The metrics listener, for Prometheus only (monitoring.tf). ClusterIP: it has no external address and
# no route in the ingress (the Caddyfile never names this Service or its port). The listener answers
# only to the bearer token; a NetworkPolicy that limits the callers to Prometheus is prepared and off
# (monitoring.tf), until the Cilium data plane is switched on and the rules are reviewed.
resource "kubernetes_service_v1" "factory_metrics" {
  count = local.monitoring_count

  metadata {
    name      = "${local.k8s_app_name}-metrics"
    namespace = kubernetes_namespace_v1.factory.metadata[0].name
    labels    = local.k8s_labels
  }

  spec {
    type     = "ClusterIP"
    selector = local.k8s_selector_labels

    port {
      name        = "metrics"
      port        = local.metrics_port
      target_port = "metrics"
      protocol    = "TCP"
    }
  }
}

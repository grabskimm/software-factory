# Names and values derived from the variables. Nothing here is an operator input.
#
# The "Kubernetes contract" section at the end is the interface between the Azure half of
# the configuration (the other *.tf files) and the Kubernetes half (k8s.tf, ingress.tf):
# namespaces, labels, object names, the Key Vault objects and the non-secret environment of
# the manager are defined once, here.

locals {
  compact_prefix = replace(var.name_prefix, "-", "")

  # Storage account names are global and limited to 24 lowercase alphanumerics,
  # so a short stable hash of the scope is appended instead of trusting the prefix
  # to be unique: "st" + prefix (<= 12) + "lk" + 6 hex = at most 20 characters.
  unique_suffix = substr(sha256("${var.subscription_id}/${var.resource_group_name}/${var.name_prefix}"), 0, 6)

  names = {
    vnet                 = "vnet-${var.name_prefix}"
    node_subnet          = "snet-nodes"
    nat_gateway          = "ngw-${var.name_prefix}"
    nat_public_ip        = "pip-ngw-${var.name_prefix}"
    node_nsg             = "nsg-nodes-${var.name_prefix}"
    ingress_public_ip    = "pip-ingress-${var.name_prefix}"
    log_analytics        = "log-${var.name_prefix}"
    cluster              = "aks-${var.name_prefix}"
    insights_dcr         = "MSCI-${var.location}-aks-${var.name_prefix}"
    node_resource_group  = "rg-${var.name_prefix}-aks-nodes"
    system_pool          = "system"
    system_pool_rotation = "systmp"
    user_pool            = "factory"
    monitoring_pool      = "mon"
    cluster_identity     = "id-aks-${var.name_prefix}"
    identity             = "id-${var.name_prefix}"
    federated_credential = "fc-${var.name_prefix}-factory"
    locks_account        = "st${local.compact_prefix}lk${local.unique_suffix}"
  }

  # Application ID of the "Azure Kubernetes Service AAD Server" first-party application. It is
  # the token audience for every Entra login to an AKS API server and the value kubelogin needs
  # as --server-id (providers.tf). It is the same in every tenant and is not a tenant secret
  # or an operator value; it is the one GUID literal check_refs.py allows, in this file only.
  # https://raw.githubusercontent.com/MicrosoftDocs/azure-aks-docs/main/articles/aks/kubelogin-authentication.md
  aks_aad_server_app_id = "6dae42f8-4368-4678-94ff-3960e28e3630"

  lock_container_name = "locks"
  # Retained, redacted copies of purged runs' logs (src/factory/logarchive.py): a second container of the lock account.
  logs_archive_container_name = "logs-archive"

  # The pods' user-pool node label. The Deployment selects it (nodeSelector pool=factory).
  user_pool_labels = {
    pool = "factory"
  }

  # With authorized IP ranges the node egress address must be in the list, otherwise the
  # nodes cannot reach the API server they belong to (a user-assigned NAT gateway is not
  # added automatically, unlike a managed load balancer's outbound IPs). Empty means
  # no restriction: the API server is then protected by Entra ID alone (aks.tf leaves the block out).
  # https://raw.githubusercontent.com/MicrosoftDocs/azure-aks-docs/main/articles/aks/api-server-authorized-ip-ranges.md
  aks_authorized_ip_ranges = length(var.aks_api_authorized_ip_ranges) == 0 ? [] : concat(var.aks_api_authorized_ip_ranges, ["${azurerm_public_ip.nat.ip_address}/32"])

  # Overlay networking: the DNS service address is the tenth address of the service range,
  # the AKS convention (and the default of `az aks create`).
  aks_dns_service_ip = cidrhost(var.aks_service_cidr, 10)

  # --- Kubernetes contract ---------------------------------------------------------------

  # Names. The workload identity federated credential (identity.tf) trusts exactly
  # system:serviceaccount:<k8s_namespace>:<k8s_service_account> on this cluster's issuer, so
  # the ServiceAccount the Deployment uses must have exactly this namespace and name.
  k8s_namespace         = "factory"
  k8s_ingress_namespace = "ingress"
  k8s_service_account   = "factory"
  k8s_app_name          = "factory"
  k8s_ingress_app_name  = "caddy"

  workload_identity_subject = "system:serviceaccount:${local.k8s_namespace}:${local.k8s_service_account}"

  # Labels. selector_labels never change (a Deployment selector is immutable). The pod label
  # azure.workload.identity/use makes the workload identity webhook inject AZURE_CLIENT_ID,
  # AZURE_TENANT_ID, AZURE_FEDERATED_TOKEN_FILE and AZURE_AUTHORITY_HOST and the projected
  # service account token.
  # https://azure.github.io/azure-workload-identity/docs/topics/service-account-labels-and-annotations.html
  k8s_labels = {
    "app.kubernetes.io/part-of"    = "factory"
    "app.kubernetes.io/managed-by" = "terraform"
  }
  k8s_selector_labels = {
    "app.kubernetes.io/name" = local.k8s_app_name
  }
  k8s_pod_labels = merge(local.k8s_selector_labels, {
    "azure.workload.identity/use" = "true"
  })
  k8s_ingress_selector_labels = {
    "app.kubernetes.io/name" = local.k8s_ingress_app_name
  }

  # Storage. The disk is Premium_LRS ext4, ReadWriteOnce, one replica (README.md). The class
  # keeps the disk when the claim is deleted (reclaimPolicy Retain): run state outlives a
  # careless `terraform destroy` of the Kubernetes objects.
  k8s_storage_class = "factory-disk"
  k8s_data_pvc      = "factory-data"

  # Key Vault secrets through the Secrets Store CSI driver (provider azure, workload identity).
  k8s_secret_provider_class = "factory-keyvault"
  k8s_synced_secret         = "factory-keyvault"
  k8s_secrets_mount_path    = "/mnt/secrets"

  # Repository configuration files: the ConfigMap `k8s_repo_config` is mounted at
  # config_repos_mount_path and every key becomes one file `<org>__<name>.toml`.
  k8s_repo_config = "factory-repos"

  # FACTORY_DATA and FACTORY_CONFIG. config.py reads $FACTORY_CONFIG/repos/<org>__<name>.toml.
  data_mount_path         = "/data"
  config_dir              = "/config"
  config_repos_mount_path = "/config/repos"

  # Sizing of the pod (variables.tf checks it against the user pool VM size): requests equal
  # limits, memory in Kubernetes quantity form, and the disk behind the PVC.
  k8s_workload_sizing = {
    cpu                              = tostring(var.container_cpu)
    memory                           = var.container_memory
    termination_grace_period_seconds = var.termination_grace_period_seconds
    data_disk_size                   = "${var.data_disk_size_gib}Gi"
  }

  # Port the manager listens on; the Service, the Caddy upstream and the exec probes use it.
  app_port = 8080

  # Port of the metrics listener (docs/metrics.md). With monitoring on it is declared on the container and
  # reached through the ClusterIP Service `factory-metrics`, which the ingress never routes to.
  metrics_port = 9464

  # Names of the two secrets the OPERATOR uploads to Key Vault out of band. Terraform
  # declares the names only, never a value. Each is pinned to the VERSION the variables give,
  # so a rotation in Key Vault changes nothing until the version variable is changed and a
  # pull request has been applied; a new version then arrives through a Deployment rollout,
  # never behind the running pod's back.
  #
  # Three more secrets exist only for the monitoring stack (monitoring.tf): the metrics bearer token
  # (also a third object of the Factory pod's own SecretProviderClass while monitoring is on), the
  # Grafana OIDC client secret and the alert receiver URL. Each is read by one identity of its own.
  kv_secret_names = {
    anthropic_api_key           = "anthropic-api-key"
    github_app_key              = "github-app-private-key"
    metrics_token               = "metrics-bearer-token"
    grafana_oauth_client_secret = "grafana-oauth-client-secret"
    alert_receiver_url          = "alert-receiver-url"
  }

  # The SecretProviderClass objects and the Kubernetes Secret keys they are synced to. The
  # key names are the environment variable names the manager reads (ANTHROPIC_API_KEY;
  # GITHUB_APP_PRIVATE_KEY is read only by the token broker, workers never see it).
  kv_secret_objects = merge(
    {
      anthropic_api_key = {
        secret_name    = local.kv_secret_names.anthropic_api_key
        version        = var.anthropic_api_key_secret_version
        k8s_secret_key = "ANTHROPIC_API_KEY"
      }
      github_app_key = {
        secret_name    = local.kv_secret_names.github_app_key
        version        = var.github_app_private_key_secret_version
        k8s_secret_key = "GITHUB_APP_PRIVATE_KEY"
      }
    },
    # Only with monitoring on: the token the metrics listener wants (src/factory/manager.py,
    # FACTORY_MANAGER_METRICS_TOKEN). Off, the listener stays on the loopback without one.
    var.monitoring_enabled ? {
      metrics_token = {
        secret_name    = local.kv_secret_names.metrics_token
        version        = coalesce(var.metrics_token_secret_version, "unset")
        k8s_secret_key = "FACTORY_MANAGER_METRICS_TOKEN"
      }
    } : {},
  )

  image = "${data.azurerm_container_registry.this.login_server}/${var.image_repository}@${var.image_digest}"

  # One file per repository config: the ConfigMap data. The file name is `<org>__<name>.toml`
  # (repo_configs keys are validated so two keys cannot collide).
  repo_config_files = {
    for repo, text in var.repo_configs : repo => {
      file_name = "${replace(repo, "/", "__")}.toml"
      text      = text
    }
  }
  repo_config_data = { for file in values(local.repo_config_files) : file.file_name => file.text }

  # Non-secret environment of the manager container. Names come from src/factory/config.py
  # (FACTORY_CONFIG, FACTORY_DATA), src/factory/locks.py lock_from_env (FACTORY_LOCK_*),
  # src/factory/github_auth.py (FACTORY_GITHUB_APP_*) and src/factory/manager.py
  # (FACTORY_MANAGER_*, FACTORY_ENTRA_*). None of these is a secret. The workload identity
  # webhook adds AZURE_CLIENT_ID, AZURE_TENANT_ID, AZURE_FEDERATED_TOKEN_FILE and
  # AZURE_AUTHORITY_HOST; the two secrets come from the synced Secret (k8s_synced_secret).
  app_env = merge(
    {
      FACTORY_CONFIG       = local.config_dir
      FACTORY_DATA         = local.data_mount_path
      FACTORY_MANAGER_HOST = "0.0.0.0"
      FACTORY_MANAGER_PORT = tostring(local.app_port)

      # The Prometheus metrics listener (docs/metrics.md): ON. Without monitoring it binds the loopback of the
      # pod only (FACTORY_MANAGER_METRICS_HOST is not set, the default is 127.0.0.1), nothing declares the port
      # and only a process in the pod (`kubectl exec`) can read it. With monitoring on (k8s.tf,
      # app_env_metrics_list) it binds the pod address, the port is declared on the container, a ClusterIP
      # Service reaches it, and it answers only to the bearer token from Key Vault (the manager refuses to
      # start on a non-loopback address without one).
      FACTORY_MANAGER_METRICS_PORT = tostring(local.metrics_port)

      # Second layer next to k8s_insights_agent_config: Container Insights skips this
      # container's environment even if the cluster-wide setting is ever turned back on.
      AZMON_COLLECT_ENV = "False"

      FACTORY_LOCK_ACCOUNT_URL = trimsuffix(azurerm_storage_account.locks.primary_blob_endpoint, "/")
      FACTORY_LOCK_CONTAINER   = local.lock_container_name
      FACTORY_LOCK_CLIENT_ID   = azurerm_user_assigned_identity.workload.client_id

      # Retained copy of a run's logs before purge deletes them (src/factory/logarchive.py): the same storage
      # account, its own container, the same workload identity. Removing the ACCOUNT_URL line falls back to a
      # directory on the data disk (`logs-archive/`).
      FACTORY_LOGS_ARCHIVE_ACCOUNT_URL    = trimsuffix(azurerm_storage_account.locks.primary_blob_endpoint, "/")
      FACTORY_LOGS_ARCHIVE_CONTAINER      = local.logs_archive_container_name
      FACTORY_LOGS_ARCHIVE_RETENTION_DAYS = tostring(var.logs_archive_retention_days)

      FACTORY_GITHUB_APP_ID      = var.github_app_id
      FACTORY_GITHUB_APP_SLUG    = var.github_app_slug
      FACTORY_GITHUB_APP_USER_ID = var.github_app_user_id

      # The manager validates Entra bearer tokens itself (no Easy Auth on AKS). Audiences are a
      # comma-separated list.
      FACTORY_MANAGER_AUTH    = "entra-jwt"
      FACTORY_ENTRA_TENANT_ID = var.tenant_id
      FACTORY_ENTRA_AUDIENCES = join(",", var.entra_allowed_audiences)
    },
  )

  # Optional plain variables, set only when the operator sets the input. They are NOT part of app_env: they go at the END of the
  # container's env list (k8s.tf, app_env_optional_list). The kubernetes_manifest provider fills the planned object from the
  # prior state BY LIST POSITION, so a variable inserted into the sorted list shifts every later entry, and where the entry at an
  # index changes shape (a fieldRef entry becoming a secretKeyRef entry) the apply sends an invalid Deployment
  # ("valueFrom: may not have more than one field specified", seen 2026-11-09 when entra_spa_scope was first set). Entries added at
  # the end, or removed from the end, leave every other index alone.
  app_env_optional = merge(
    var.token_broker_command == null ? {} : { FACTORY_TOKEN_BROKER = var.token_broker_command },
    # What the dashboard page signs in with; unset, the manager derives both from the audiences above.
    var.entra_spa_client_id == null ? {} : { FACTORY_ENTRA_SPA_CLIENT_ID = var.entra_spa_client_id },
    var.entra_spa_scope == null ? {} : { FACTORY_ENTRA_SPA_SCOPE = var.entra_spa_scope },
  )

  # Changes to a Key Vault secret version or to the repository config text must roll the
  # Deployment: the CSI mount and the synced Secret are read when a pod starts. Put this value
  # in a pod template annotation. It contains no secret: version IDs and the non-secret repo
  # config text only.
  deploy_inputs_sha256 = sha256(jsonencode(merge(
    {
      repo_configs      = var.repo_configs
      anthropic_version = var.anthropic_api_key_secret_version
      github_version    = var.github_app_private_key_secret_version
    },
    # Only with monitoring on, so switching it off again (and the default) leaves the hash, and so the pod, alone.
    var.monitoring_enabled ? { metrics_token_version = var.metrics_token_secret_version } : {},
  )))

  # Container Insights collects the environment variables of every container into the
  # workspace by default, and the manager's environment carries ANTHROPIC_API_KEY and
  # GITHUB_APP_PRIVATE_KEY (synced from Key Vault). This agent ConfigMap turns that
  # collection off; k8s.tf MUST create it (kube-system, exactly this name) before the Factory
  # pod starts. https://raw.githubusercontent.com/MicrosoftDocs/azure-monitor-docs/main/articles/azure-monitor/containers/kubernetes-data-collection-configmap.md
  k8s_insights_agent_config = {
    name      = "container-azm-ms-agentconfig"
    namespace = "kube-system"
    data = {
      "schema-version" = "v1"
      "config-version" = "ver1"
      "log-data-collection-settings" = join("\n", concat(
        [
          "[log_collection_settings]",
          "  [log_collection_settings.stdout]",
          "    enabled = true",
        ],
        local.k8s_log_exclusion_lines,
        [
          "  [log_collection_settings.stderr]",
          "    enabled = true",
        ],
        local.k8s_log_exclusion_lines,
        [
          "  [log_collection_settings.env_var]",
          "    enabled = false",
        ],
      ))
    }
  }

  # The namespaces whose stdout and stderr Container Insights does NOT collect, only with the pod backend (pod_backend_enabled,
  # worker_ns.tf): a worker pod's output must not reach the workspace (the session transcript goes to the relay, not to the pod
  # log, and a stray line of it must not be stored either). An explicit list REPLACES the agent's documented default
  # (kube-system and gatekeeper-system), so it is spelled out in full: naming only the worker namespace would newly collect the
  # output of both. The same list stands under stdout and stderr. Off (the default) the lines are empty and the data above is
  # byte-identical to what it was before this list existed. The key spelling `exclude_namespaces` and the default list are read in
  # Microsoft's data-collection ConfigMap article and are UNVERIFIED on a cluster (docs/pod-backend.md).
  # https://raw.githubusercontent.com/MicrosoftDocs/azure-monitor-docs/main/articles/azure-monitor/containers/kubernetes-data-collection-configmap.md
  k8s_log_excluded_namespaces = ["kube-system", "gatekeeper-system", local.worker_contract.names.worker_namespace]
  k8s_log_exclusion_lines     = var.pod_backend_enabled ? ["    exclude_namespaces = ${jsonencode(local.k8s_log_excluded_namespaces)}"] : []

  # --- Ingress -----------------------------------------------------------------------------

  # <label>.<region>.cloudapp.azure.com: the public IP's domain_name_label (ingress.tf) gives
  # the address this name; Caddy obtains its certificate for it.
  ingress_fqdn = "${var.ingress_dns_label}.${var.location}.cloudapp.azure.com"

  # The optional host name of your own (ingress_custom_host). Both names serve the same site: the Azure name first,
  # so it keeps working whatever happens to the custom name. Caddy obtains one certificate per name. The primary
  # name is the one Grafana's root_url (and so its sign-in redirect) uses: the custom name once it is set.
  ingress_custom_host  = var.ingress_custom_host == null ? "" : var.ingress_custom_host
  ingress_hosts        = compact([local.ingress_fqdn, local.ingress_custom_host])
  ingress_site_address = join(", ", local.ingress_hosts)
  ingress_primary_host = local.ingress_custom_host == "" ? local.ingress_fqdn : local.ingress_custom_host

  # Caddy, pinned by digest. The Let's Encrypt contact is optional ("" means none).
  ingress_image      = "${var.caddy_image_repository}@${var.caddy_image_digest}"
  ingress_acme_email = var.acme_email == null ? "" : var.acme_email

  # Source prefixes the NSG admits to ports 80 and 443; ingress.tf puts the same list in the
  # Service's loadBalancerSourceRanges (omitted when ingress_allowed_cidrs is empty).
  ingress_source_prefixes = length(var.ingress_allowed_cidrs) == 0 ? ["Internet"] : var.ingress_allowed_cidrs

  # --- Worker pod contract ---------------------------------------------------------------

  # The names and numbers that the Terraform half and the Python half of the pod backend must
  # agree on (docs/pod-backend.md, src/factory/podcontract.py, issues #144 to #147). The backend is
  # off and nothing is created from this: it is literals only (strings, numbers, lists and maps of
  # them; no var. or local. reference, so a test parses it without evaluating HCL), and
  # infra/test/test_pod_contract.py fails when it differs from podcontract.SHARED in either
  # direction. Values derived from variables (the concurrency ceiling, resource sums) live in
  # other locals. All numbers marked PROVISIONAL in podcontract.py stay estimates until measured.
  #
  # Read by worker_pool.tf (the NodePool and AKSNodeClass objects take their names, label and taint from it, and the
  # concurrency ceiling local reads the class table), by worker_ns.tf (the worker namespace, its service account, the
  # relay port of the network policy and the label of the namespace) and by pod_backend.tf (the launcher and relay containers, the
  # init container, the volumes, the toleration and the manager taint: every environment name, uid, path and port of them).
  # infra/test/test_pod_contract.py fails if a silencing annotation for tflint's unused
  # declaration rule is put back on it, or is missing while nothing reads the local.
  worker_contract = {
    names = {
      manager_namespace       = "factory"
      worker_namespace        = "factory-workers"
      worker_service_account  = "factory-worker"
      pod_name_prefix         = "factory-w-"
      secret_name_suffix      = "-s"
      secret_key_token        = "token"
      secret_key_anthropic    = "anthropic-api-key"
      nodepool                = "factory-workers"
      nodeclass               = "factory-workers"
      image_reference_pattern = "^[a-z0-9][a-z0-9.-]*(:[0-9]+)?(/[a-z0-9._-]+)+@sha256:[0-9a-f]{64}$"
    }
    labels = {
      prefix     = "factory.internal/"
      keys       = ["role", "repo", "run", "job", "attempt", "phase", "manager", "session"]
      role_value = "worker"
      phases     = ["plan", "build", "review", "verify", "preflight"]
      hex_lengths = {
        repo    = 12
        run     = 12
        job     = 12
        manager = 12
        session = 32
      }
    }
    annotations = {
      do_not_disrupt       = "karpenter.sh/do-not-disrupt"
      do_not_disrupt_value = "true"
      keys                 = ["deadline", "repo", "run-id", "job-id"]
    }
    nodes = {
      worker_label_key    = "pool"
      worker_label_value  = "worker"
      worker_taint_key    = "workload"
      worker_taint_value  = "worker"
      manager_taint_key   = "workload"
      manager_taint_value = "manager"
      taint_effect        = "NoSchedule"
    }
    ports = {
      relay   = 8090
      app     = 8080
      metrics = 9464
    }
    paths = {
      session_secret_dir  = "/run/factory-session"
      scratch_dir         = "/data"
      tmp_dir             = "/tmp"
      launcher_socket_dir = "/run/factory"
      launcher_socket     = "/run/factory/launcher.sock"
      spool_dir           = "/spool"
    }
    ids = {
      supervisor_uid = 10001
      worker_uid     = 10002
      worker_gid     = 10010
      launcher_uid   = 10003
      relay_uid      = 10004
      shared_gid     = 10001
    }
    env = {
      manager = {
        pod_backend      = "FACTORY_POD_BACKEND"
        max_pod_sessions = "FACTORY_MAX_POD_SESSIONS"
        launcher_socket  = "FACTORY_POD_LAUNCHER_SOCKET"
        spool_dir        = "FACTORY_POD_SPOOL_DIR"
      }
      launcher = {
        image                   = "FACTORY_LAUNCHER_IMAGE"
        namespace               = "FACTORY_LAUNCHER_NAMESPACE"
        max_pods                = "FACTORY_LAUNCHER_MAX_PODS"
        repo_cap                = "FACTORY_LAUNCHER_REPO_CAP"
        deadline_clamp          = "FACTORY_LAUNCHER_DEADLINE_CLAMP"
        allowed_uid             = "FACTORY_LAUNCHER_ALLOWED_UID"
        relay_url               = "FACTORY_LAUNCHER_RELAY_URL"
        classes                 = "FACTORY_LAUNCHER_CLASSES"
        api_public_host         = "FACTORY_LAUNCHER_API_PUBLIC_HOST"
        manager_address         = "FACTORY_LAUNCHER_MANAGER_ADDRESS"
        manager_metrics_address = "FACTORY_LAUNCHER_MANAGER_METRICS_ADDRESS"
        socket                  = "FACTORY_LAUNCHER_SOCKET"
      }
      relay = {
        port      = "FACTORY_RELAY_PORT"
        spool_dir = "FACTORY_RELAY_SPOOL_DIR"
      }
      pod = [
        "FACTORY_SESSION_ID",
        "FACTORY_RELAY_URL",
        "FACTORY_SESSION_DEADLINE",
        "FACTORY_POD_NAME",
        "FACTORY_POD_UID",
        "FACTORY_NODE_IP",
        "FACTORY_MANAGER_ADDRESS",
        "FACTORY_MANAGER_METRICS_ADDRESS",
        "FACTORY_API_PUBLIC_HOST",
        "AZMON_COLLECT_ENV",
      ]
    }
    env_values = {
      pod_backend_on  = "on"
      pod_backend_off = "off"
    }
    timing = {
      deadline_clamp_seconds    = 14400
      deadline_margin_seconds   = 300
      termination_grace_seconds = 30
    }
    limits = {
      per_repo_cap = 4
    }
    classes = {
      default = "standard"
      table = {
        standard = {
          cpu               = "2"
          memory            = "4Gi"
          ephemeral_storage = "20Gi"
          pods_per_node     = 1
          data_size_limit   = "14Gi"
          tmp_size_limit    = "2Gi"
        }
        small = {
          cpu               = "1"
          memory            = "2Gi"
          ephemeral_storage = "10Gi"
          pods_per_node     = 3
          data_size_limit   = "7Gi"
          tmp_size_limit    = "1Gi"
        }
      }
    }
    volumes = {
      spool_size_limit      = "16Gi"
      socket_dir_size_limit = "1Mi"
    }
    canary = {
      forbidden = [
        { host = "169.254.169.254", port = 80 },
        { host = "168.63.129.16", port = 80 },
        { host = "168.63.129.16", port = 32526 },
        { host_env = "FACTORY_NODE_IP", port = 10250 },
        { host_env = "FACTORY_MANAGER_ADDRESS", port = 8080 },
        { host_env = "FACTORY_MANAGER_METRICS_ADDRESS", port = 9464 },
      ]
      api_probes = [
        { name = "service", host_env = "KUBERNETES_SERVICE_HOST", port_env = "KUBERNETES_SERVICE_PORT" },
        { name = "public", host_env = "FACTORY_API_PUBLIC_HOST", port = 443 },
      ]
      api_probe_path            = "/api/v1/namespaces/factory/secrets"
      api_probe_denied_statuses = [401, 403]
    }
  }
}

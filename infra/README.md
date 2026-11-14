> [!WARNING]
> **Checked offline on every change; what a real apply confirms is not recorded here.** `terraform fmt`, `init -backend=false`,
> `validate`, `tflint`, `check_refs.py` and the tests run offline, and the Kubernetes objects were also applied twice (no
> drift) to a local kind cluster with Azure values stubbed. Nothing in this directory says **which** of the items under
> "UNVERIFIED" at the end were checked on a real apply, so that list stays authoritative: treat each item as open until
> you have checked it, read the list before changing what it covers, and check each item on your next real plan and apply.
>
> Spikes S1-S3 (`spikes/README.md`) are recorded PASS, S1 **with three accepted pod relaxations** (`spikes/S1.md`). Do not
> apply unless all three begin with `# S<n>: PASS`; nothing checks that for you.
>
> **This is an optional reference deployment.** The GitHub Actions pipelines that originally planned, applied and promoted
> it are not part of this repository: run `terraform plan` and `terraform apply` with your own pipeline and approval
> process. `INFRA_TFVARS` below is the name this document uses for the operator's tfvars: a repository (or CI) variable
> that holds that text and that your own pipeline passes to `terraform` with `-var-file`. The drain procedure in "Safe
> deployments" still applies, and `tools/infra_deploy_gate.py` and `tools/infra_cluster_guard.py` are optional scripts you
> can call from your pipeline. `infra/image.auto.tfvars` and `infra/ingress.auto.tfvars` hold **placeholders**: replace the
> digest (the validation rejects the placeholder) and the host name, or delete the host line, before a real deploy.

# Factory infrastructure (Terraform)

Infrastructure for the Factory manager on **Azure Kubernetes Service (AKS)**: one cluster with a small system
pool and a one-node `factory` pool that runs exactly one manager pod, a VNet with a NAT gateway for a fixed egress
address, run data on an Azure Disk, per-repository locks as blob leases, secrets in Key Vault read through the
Secrets Store CSI driver, a public HTTPS ingress (Caddy, Let's Encrypt), and Microsoft Entra authentication that
**the manager itself performs** (bearer tokens; there is no Easy Auth on AKS).

Why AKS and not Container Apps: the bubblewrap sandbox did not start on Container Apps (S1 FAIL) and passed on an
Azure Linux 3 node with `spec.hostUsers: false`, `procMount: Unmasked` and an `Unconfined` AppArmor profile on the
Factory container together (the smaller sets tried failed; two subsets were not tried, `spikes/S1.md`). A pod with `hostUsers: false` cannot mount NFS, so `/data` is an Azure Disk (ReadWriteOnce): one
replica, strategy `Recreate`. Factory's own sandbox settings are not weakened. See `spikes/S1.md` and `spikes/S2.md`.

Providers: `azurerm ~> 4.81` (checked against 4.81.0), `azapi ~> 2.13` (only for the Log Analytics retention) and
`kubernetes ~> 3.5` (checked against 3.5.0). Terraform `>= 1.9` (checked with 1.13.5). Whoever runs a plan or apply needs the
Azure CLI (logged in) and **kubelogin** on the `PATH`: the kubernetes provider gets its token from
`kubelogin get-token --login azurecli` (`providers.tf`).

## What it creates

| Resource | File | Notes |
|---|---|---|
| VNet, one node subnet (`/26` by default) with `Microsoft.Storage` and `Microsoft.KeyVault` service endpoints | `network.tf` | The resource group is looked up, not created |
| NAT gateway (Standard) + static Standard public IP on the node subnet | `network.tf` | The IP is the only egress address of the cluster: output `nat_public_ip` |
| NSG on the node subnet | `network.tf` | See "Network design" |
| Log Analytics workspace (30 days, its minimum) with 4-day retention on the Factory tables | `observability.tf` | workspace default 30 days; `azapi_update_resource` sets 4 days on `ContainerLogV2`, `KubeEvents`, `KubePodInventory`, `AZKVAuditLogs` |
| AKS cluster: Azure Linux 3, local accounts disabled, Entra ID + Azure RBAC, OIDC issuer, workload identity, CNI Overlay, `userAssignedNATGateway`, Key Vault CSI add-on (rotation off), Container Insights | `aks.tf` | System pool 1 x `Standard_D2ds_v5`; no autoscaler. `prevent_destroy` is set: a plan that replaces or destroys the cluster fails (see "Switching the data plane"). The Cilium data plane and node auto-provisioning are optional arguments, omitted by default |
| Node pool `factory`: 1 x `Standard_D8ds_v5`, label `pool=factory`, no autoscaler | `aks.tf` | The manager pod's node |
| Role assignments "Azure Kubernetes Service RBAC Cluster Admin" and "Cluster User Role" | `aks.tf` | For exactly the principals in `aks_cluster_admin_principal_ids` (required, non-empty; includes the apply identity). The caller is never added implicitly, so a plan and an apply see the same addresses |
| Lock account + `locks` container + `logs-archive` container | `storage.tf` | Default-deny firewall, node subnet through a service endpoint, no account keys; `logs-archive` holds the retained copies of purged runs' logs |
| Key Vault (RBAC, purge protection) | `keyvault.tf` | Holds secret NAMES only; no value is in Terraform |
| Identity `workload` + federated credential for `system:serviceaccount:factory:factory` + roles | `identity.tf` | Key Vault Secrets User (vault), Storage Blob Data Contributor (container `locks` only), Storage Blob Data Contributor (container `logs-archive` only) |
| Control-plane identity `cluster`, AcrPull for the kubelet identity | `identity.tf` | Network Contributor on the node subnet only |
| Static public IP with DNS label for the ingress, Network Contributor on the resource group for the cluster identity (the cloud provider lists the group's public IPs) | `ingress.tf` | Output `ingress_public_ip`, `manager_url` |
| Namespace `factory`, service account `factory` (workload identity annotation) | `k8s.tf` | Namespace is Pod Security `privileged` (the pod needs the relaxations) |
| StorageClass `factory-disk` (Premium_LRS, `Retain`, `WaitForFirstConsumer`, expandable), PVC `factory-data` | `k8s.tf` | The disk is in the cluster's node resource group |
| SecretProviderClass `factory-keyvault` (pinned secret versions, synced Secret `factory-keyvault`) | `k8s.tf` | `kubernetes_manifest` (a CRD) |
| ConfigMap `factory-repos-<hash>` (immutable) | `k8s.tf` | One file per repository, mounted at `/config/repos` |
| ConfigMap `container-azm-ms-agentconfig` in `kube-system` | `k8s.tf` | Stops Container Insights from copying container environments |
| Deployment `factory` (1 replica, `Recreate`, the three pod relaxations) and ClusterIP Service `factory` | `k8s.tf` | The Deployment is a `kubernetes_manifest`, see below |
| Namespace `ingress` (Pod Security `restricted`), Caddy Deployment, ConfigMap (Caddyfile), PVC `caddy-data`, LoadBalancer Service | `ingress.tf` | Caddy runs on the system node |
| **Only with `monitoring_enabled`:** namespace `monitoring` (Pod Security `restricted`), Prometheus (Deployment, ConfigMaps, 8 GiB disk, ClusterRole for node metrics), Grafana (Deployment, ConfigMaps, `emptyDir`), optional Alertmanager, ClusterIP Services | `monitoring.tf`, files in `monitoring/` | See "Monitoring" |
| **Only with `monitoring_enabled`:** one identity + federated credential + one-secret `Key Vault Secrets User` role per component (`prometheus`, `grafana`, optional `alertmanager`) | `identity.tf` | Each reads exactly one secret |
| **Only with `monitoring_enabled`:** ClusterIP Service `factory-metrics`; the Factory Deployment declares port `9464`, binds the pod address and gets the token | `k8s.tf` | Not routed by the ingress |
| **Only with `monitoring_enabled` (issue #149):** kube-state-metrics (Deployment, service account, ClusterRole and binding for `list` and `watch` of `pods` and `nodes`, ClusterIP Service): **one more small pod** | `monitoring.tf` | New objects only: a default plan is unchanged. With `monitoring_nodeclaim_metrics_enabled` (default off, needs `nap_mode = "Auto"`) also a ConfigMap with the custom resource state file and a second ClusterRole and binding for `nodeclaims` and `customresourcedefinitions` |
| **Only with `monitoring_enabled` and `monitoring_node_pool_vm_size`:** node pool `mon`, tainted `workload=monitoring:NoSchedule` | `aks.tf` | Default none: the system node is used |
| **Only with `worker_nodepool_enabled` (default off, needs Cilium and node auto-provisioning on):** `AKSNodeClass` and `NodePool` `factory-workers` (`kubernetes_manifest`, cluster-scoped) | `worker_pool.tf` | The pool of the pod backend: `Standard_D4s_v6` (a Dsv6 size rather than the `Dds_v5` family of the other pools, whose quota is small), `on-demand`, label `pool=worker`, taint `workload=worker:NoSchedule`, `limits.cpu` 32, `WhenEmpty` after 30 s, `expireAfter: Never`, no `terminationGracePeriod`. Nothing runs on it yet. See "Switching the data plane" |
| **Only with `pod_backend_enabled` (default off; needs Cilium, node auto-provisioning and the worker pool):** namespace `factory-workers` (Pod Security `privileged`), service account `factory-worker` (no API token, no RoleBinding), NetworkPolicy `default-deny` (ingress denied; egress only to cluster DNS, the relay port of the manager pod and TCP 443 outside the metadata service, the Azure host service and the pod, service and node ranges) (`kubernetes_namespace_v1.worker`, `kubernetes_service_account_v1.worker`, `kubernetes_network_policy_v1.worker_default_deny`) | `worker_ns.tf` | The fence of the pod backend; no pod runs in it yet. The agent ConfigMap row above then also excludes the namespace from log collection (`locals.tf`), and the prepared policy below admits the relay port from it. The Role and RoleBinding of slice 16 MUST `depends_on` `kubernetes_network_policy_v1.worker_default_deny`, so that the fence exists before any Role does (`test_worker_ns_design.py` scans `infra/*.tf` for one that lacks it) |
| **Only with `pod_backend_enabled` (same gate and preconditions):** the admission policies of the pod backend: the ValidatingAdmissionPolicy and ValidatingAdmissionPolicyBinding of worker pods (`factory-worker-pods`) and of their per-session Secrets (`factory-worker-secrets`), read from `infra/policy/*.json` (`kubernetes_manifest.worker_policy_pods`, `worker_policy_pods_binding`, `worker_policy_secrets`, `worker_policy_secrets_binding`), and the parameter of the pod policy, a ConfigMap whose `image` is `local.image`, the launcher's own image local (`kubernetes_config_map_v1.worker_policy_params`) | `worker_policy.tf`, `policy/` | A HARD PREREQUISITE of the pod backend, not defence in depth: the namespace is Pod Security `privileged`, so only these rules pin what the launcher may say in a pod (the image digest, the three relaxations and nothing else, one container, no host namespace, no host path, no secret source, the allow-listed environment, the worker pool and one repository per node) and in a Secret (named after its pod, Opaque, two keys, owned by that pod). Bound to ONE identity (the service account `factory` of the manager namespace, by `request.userInfo.username`), `factory-workers` only, `failurePolicy: Fail`, actions `Deny` and `Audit`; every validation carries the stable rule id of `src/factory/podspec.py` as `[rule:<id>]`. The Role and RoleBinding of slice 16 MUST `depends_on` both policy bindings as well as the default-deny policy |
| **Only with `pod_backend_enabled` (same gate):** the launcher's Role `factory-launcher` (pods create, get, list, delete; Secrets create, delete; nothing else) and its RoleBinding to the service account `factory` of the manager namespace (`kubernetes_role_v1.worker_launcher`, `kubernetes_role_binding_v1.worker_launcher`) | `worker_rbac.tf` | Slice 16. Both `depends_on` the default-deny policy AND both admission policy bindings, so the fence exists before any Role does. No new data action for the plan identity. Listed under `POOL_OBJECTS` |
| **Only with `pod_backend_enabled` (same gate):** the launcher and relay containers of the manager pod, the init container `volume-perms`, the `emptyDir`s `launcher-socket` (1Mi) and `spool` (16Gi), the projected token that only the launcher mounts, the environment and mounts of the main container, the annotation `azure.workload.identity/skip-containers` and the toleration `workload=manager:NoSchedule`; ClusterIP Service `factory-relay-svc` (`kubernetes_service_v1.factory_relay`) | `pod_backend.tf`, `k8s.tf` | Slice 17. The `pod_backend_*` locals are empty lists and maps while the variable is off, appended to the Deployment with `concat` and `merge`: the Deployment of a default plan is unchanged. The manager still refuses `execution = "pod"` |
| **Only with `manager_pool_taint_enabled` (default off, needs `pod_backend_enabled`):** `node_taints = ["workload=manager:NoSchedule"]` on the node pool `factory` | `aks.tf` | A separate, later flip; the toleration ships with the backend. May REPLACE the pool (UNVERIFIED), so it is a `stage=cluster` change with `cluster_snapshots_taken` and is run on the mirror first |
| **Prepared, off (`factory_network_policy_enabled`):** NetworkPolicy `factory-ingress` | `monitoring.tf` | Enforced only with the Cilium data plane (`network_data_plane`); the reference deployment has none today |

Outputs (`outputs.tf`): `nat_public_ip`, `ingress_public_ip`, `manager_url`, `aks_cluster_name`,
`aks_get_credentials_command`, `aks_oidc_issuer_url`, `identity_principal_id`, `identity_client_id`,
`lock_container_url`, `logs_archive_container_url`, `key_vault_uri`.

**Why two objects are `kubernetes_manifest`.** The typed `kubernetes_deployment_v1` (also `kubernetes_pod_v1` and
`kubernetes_stateful_set_v1`) of the provider (3.5.0) has no argument for `hostUsers`, `procMount` or `appArmorProfile`,
the three settings the sandbox passed with, so the Deployment is a manifest. The SecretProviderClass is a custom resource with no
typed resource. Everything else is typed. `kubernetes_manifest` reads the API server (and the CRD) at **plan** time, which is
why the first apply has two stages ("First apply"), and why every later plan needs a reachable API server.

## What the pod gets

| In the container | Value | Source |
|---|---|---|
| `FACTORY_CONFIG` | `/config` | `src/factory/config.py` reads `$FACTORY_CONFIG/repos/<org>__<name>.toml` |
| `FACTORY_DATA` | `/data` | the Azure Disk (PVC `factory-data`) |
| `FACTORY_MANAGER_HOST`, `FACTORY_MANAGER_PORT` | `0.0.0.0`, `8080` | the Service and Caddy target 8080 |
| `FACTORY_MANAGER_AUTH` | `entra-jwt` | the manager validates the bearer token itself |
| `FACTORY_ENTRA_TENANT_ID`, `FACTORY_ENTRA_AUDIENCES` | `tenant_id`, `entra_allowed_audiences` joined by commas | `src/factory/entra_jwt.py` |
| `FACTORY_LOCK_ACCOUNT_URL`, `FACTORY_LOCK_CONTAINER` | lock account blob endpoint, `locks` | `src/factory/locks.py`; the blob name is `<org>__<repo>` |
| `FACTORY_LOCK_CLIENT_ID` | the `workload` identity's client ID | the lock client uses the workload identity token |
| `FACTORY_LOGS_ARCHIVE_ACCOUNT_URL`, `FACTORY_LOGS_ARCHIVE_CONTAINER`, `FACTORY_LOGS_ARCHIVE_RETENTION_DAYS` | lock account blob endpoint, `logs-archive`, `logs_archive_retention_days` (30) | `src/factory/logarchive.py`; see "Retained logs" |
| `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_FEDERATED_TOKEN_FILE`, `AZURE_AUTHORITY_HOST` | injected by the workload identity webhook | pod label `azure.workload.identity/use=true` + the service account annotation |
| `FACTORY_GITHUB_APP_ID`, `_SLUG`, `_USER_ID` | variables | `src/factory/github_auth.py` |
| `FACTORY_TOKEN_BROKER` | only if `token_broker_command` is set | normally set by the image's `ENV` |
| `AZMON_COLLECT_ENV` | `False` | second layer next to the Container Insights ConfigMap |
| `ANTHROPIC_API_KEY`, `GITHUB_APP_PRIVATE_KEY` | `secretKeyRef` into the Secret `factory-keyvault` | the only secrets (with monitoring off); synced by the CSI driver from the pinned Key Vault versions |
| `FACTORY_MANAGER_METRICS_PORT` | `9464` | the metrics listener is on; without monitoring it binds the pod's loopback only |
| `FACTORY_MANAGER_METRICS_HOST` | the pod address (`status.podIP`) | **only with `monitoring_enabled`**; the port is then declared on the container |
| `FACTORY_MANAGER_METRICS_TOKEN` | `secretKeyRef` into the same Secret | **only with `monitoring_enabled`**: Key Vault secret `metrics-bearer-token`, a third object of the SecretProviderClass |

Mounts: `/data` (disk), `/config/repos` (ConfigMap, read-only: one `<org>__<name>.toml` per `repo_configs` entry, the same file
names the Container Apps secret volume used). **The Key Vault volume is mounted only by an init container** (`keyvault-sync`,
the Factory image with a `test -s` command, which also fails the pod early and legibly if a secret file is missing). The CSI
driver only publishes a volume that some container mounts, and the synced Secret only exists after that; the Factory
container itself never sees the files, so the GitHub App key is only an environment variable of the supervisor and never a
readable file. Probes are `exec` probes (`python` + `urllib` against `127.0.0.1:8080/healthz`): in `entra-jwt` mode `/healthz`
is unauthenticated **only for loopback peers**, so the kubelet's HTTP probe would get 401. The pod does not mount the
Kubernetes API token and has `enableServiceLinks: false` (a Service named `factory` would otherwise inject `FACTORY_PORT` and
friends).

The Factory container's security context is exactly `procMount: Unmasked` and `appArmorProfile: Unconfined`, and the pod has
`hostUsers: false` (spikes/S1.md). Nothing else is set: no `privileged`, no capabilities (the entrypoint needs the default set,
then keeps `setuid`, `setgid`, `kill`), no `runAsUser`/`fsGroup` (the entrypoint chowns `/data`), no `readOnlyRootFilesystem` (the
entrypoint prepares `/config` and `/data` as root), no seccomp field (S1 passed with the node default). `test_workload_design.py`
fails if any of this changes.

## Inputs

All operator inputs are variables **without defaults** (see `variables.tf` for the validations): `subscription_id`,
`tenant_id`, `location`, `resource_group_name`, `name_prefix`, `tags`, `acr_name`, `acr_resource_group_name`,
`image_repository`, `image_digest`, `key_vault_name`, `anthropic_api_key_secret_version`,
`github_app_private_key_secret_version`, `entra_allowed_audiences`, `github_app_id`, `github_app_slug`,
`github_app_user_id`, `ingress_dns_label`, `caddy_image_digest`, `repo_configs`.

Defaults exist for sizing and network ranges (`aks_sku_tier`, `aks_kubernetes_version`, `aks_system_pool_vm_size`,
`aks_user_pool_vm_size`, `container_cpu`, `container_memory`, `data_disk_size_gib`, `termination_grace_period_seconds`,
`log_retention_days`, `logs_archive_retention_days`, `vnet_address_space`, `node_subnet_prefix`, `aks_pod_cidr`, `aks_service_cidr`) and for optional extras
that default to nothing or to a safe choice (`key_vault_ip_rules`, `key_vault_secrets_officer_principal_ids`,
`token_broker_command`, `caddy_image_repository`, `acme_email`, `ingress_allowed_cidrs`, `aks_api_authorized_ip_ranges`,
`aks_cluster_admin_principal_ids`, `aks_node_os_upgrade_channel`) and for the dashboard's sign-in and host name (`ingress_custom_host`, `entra_spa_client_id`,
`entra_spa_scope`; see "Browser sign-in and custom host name"). The monitoring stack adds variables that all have defaults and leave it
off (`monitoring_enabled`, `metrics_token_secret_version`, `grafana_oauth_client_id`, `grafana_oauth_client_secret_version`,
`grafana_operator_role`, `alert_receiver`, `alert_receiver_secret_version`, `monitoring_node_pool_vm_size`, `prometheus_retention`,
`prometheus_storage_gib`, `prometheus_scrape_interval`, `prometheus_image`, `grafana_image`, `alertmanager_image`,
`factory_network_policy_enabled`; see "Monitoring"). Cilium, node auto-provisioning and the worker pool add nine variables that are all unset or off
(`network_data_plane`, `network_policy`, `nap_mode`, `nap_default_node_pools`, `worker_nodepool_enabled`, `worker_pool_limits_cpu`, `worker_pool_sku`,
`worker_pool_pods_per_node`, `family_vcpu_quota`; see "Switching the data plane"); the pod backend adds two more, `pod_backend_enabled` and `manager_pool_taint_enabled` (both off). `entra_app_client_id`, `workload_profile_type`,
`share_quota_gib` and the two old subnet prefixes no longer exist: a leftover assignment in `INFRA_TFVARS` only produces an
"undeclared variable" warning, remove it.

`container_cpu` and `container_memory` default to **6 vCPU and 24Gi** on the D8 node and are validated at no more than 75 % of
the node: there is only one replica and `Recreate` never runs two, so the old halving for a rollout overlap is gone.

`terraform.tfvars.example` shows every input with `<<PLACEHOLDER>>` values. It is not auto-loaded. `image_digest` is the one
exception: it lives in the committed `image.auto.tfvars` (a placeholder until you set your own digest, rejected by the validation) so that git
can record what is deployed; you update it when you promote an image ("Promote an image digest"). `caddy_image_digest` is a normal input in `INFRA_TFVARS` (the operator's tfvars, see the note at the top): pin the digest of the official Caddy 2 image
(`docker buildx imagetools inspect caddy:2 --format '{{json .Manifest.Digest}}'` prints it; the value is the digest of the multi-architecture index), and update it by changing that variable.

## Network design

Sources are listed at the end. Items that could not be verified are in "UNVERIFIED".

- **Egress.** All outbound traffic from the node subnet leaves through the NAT gateway (`outbound_type =
  userAssignedNATGateway`), so the cluster has one fixed address (`nat_public_ip`). Publish it to the GitHub organization IP
  allow list. There is no Azure Firewall; domain filtering happens only in the Claude Code sandbox allowlist.
- **NSG on the node subnet.** AKS does not edit an NSG on a subnet you provide, so the rules are here.
  - Inbound: TCP 80 and 443 from `ingress_allowed_cidrs` (the internet if the list is empty); node, node-to-pod and
    pod-to-pod traffic of the overlay network.
  - Outbound: TCP 443 to the internet; the Azure platform address `168.63.129.16` (53, 80, 32526: nodes do not work
    without it); the instance metadata address; the overlay network; then **deny every other port to the internet**.
- **Ingress.** Caddy (official image pinned by digest, one replica, system node, `Recreate`) behind a Standard load balancer
  bound to a static public IP with a DNS label: `https://<ingress_dns_label>.<location>.cloudapp.azure.com`. It obtains a Let's
  Encrypt certificate (HTTP-01 on 80, TLS-ALPN-01 on 443) and keeps it on a 4 GiB disk. Caddy removes the inbound Easy Auth headers
  (`X-MS-CLIENT-PRINCIPAL`, `-ID`, `-NAME`, `-IDP`) and forwards `Authorization` unchanged to `factory.factory.svc.cluster.local:8080`. The
  Service uses `externalTrafficPolicy: Local` (real client address) and, when `ingress_allowed_cidrs` is set,
  `loadBalancerSourceRanges`. HTTP/3 is off (the Service is TCP only). The Caddy pod meets the Pod Security `restricted` level
  (non-root, no privilege escalation, all capabilities dropped, `RuntimeDefault` seccomp, sysctl
  `net.ipv4.ip_unprivileged_port_start=0` for the low ports).
- **Grafana (only with `monitoring_enabled`).** The same site serves `https://<host>/grafana/`: `redir /grafana /grafana/`, then `handle /grafana/*`
  proxies to the ClusterIP Service `grafana.monitoring:3000` without the `Authorization` header, and every other path goes to the manager exactly
  as before. Grafana authenticates people itself (Entra OIDC); the manager's bearer check is not involved and its routes are not named. The metrics
  port (`9464`) and Prometheus have no route at all, and Grafana's own `/metrics` endpoint is switched off (see "Security summary"). Pod-to-pod traffic (Prometheus to the metrics Service, to the kubelet on TCP 10250) is covered by the overlay rules above.
- **Authentication.** Caddy does not authenticate anyone. The manager requires `Authorization: Bearer <Entra access token>` on every
  path, including `/healthz` for non-loopback peers, so the public URL answers 401 without a token; the one exception is the dashboard page's three static files under `/ui/`, which carry no data and are not the gate for any data call. It verifies RS256
  signature, issuer, audience, tenant, expiry and maps the `roles` claim to `Factory.Operator` and `Factory.Viewer`
  (`docs/manager.md`). `ingress_allowed_cidrs` is optional extra hardening, not the gate. **A restricted list blocks Let's Encrypt**
  (it validates from unpublished addresses), so issuance and renewal fail unless Caddy gets another challenge type or a
  certificate loaded some other way.
- **Storage and Key Vault.** The lock account and the vault use `default_action = Deny` and admit the node subnet through the
  `Microsoft.Storage` and `Microsoft.KeyVault` service endpoints. The Secrets Store CSI provider and the lock client both run on a
  node, so their traffic carries the node subnet's identity. The vault has no trusted-service bypass any more. Account keys are
  off: only RBAC works.
- **Cluster API.** Public, protected by Entra ID and Azure RBAC; local accounts are off and `az aks command invoke` is off. Set
  `aks_api_authorized_ip_ranges` to restrict it (the NAT address is added automatically). GitHub-hosted runners have no stable
  address, so a non-empty list needs a runner with a known egress address, otherwise a pipeline's plans and applies cannot reach the API.
  This is why the IaC scanner rule `terraform-azure-aks-private-cluster-disabled` (Medium) is switched off for `aks.tf` by the
  `dd-iac-scan disable=` comment on its first line, together with `terraform-azure-aks-network-policy-misconfigured` (Low, while
  `network_policy` is null); a private cluster is an operator decision, not part of the Cilium and worker pool slice.
- **One replica, one node.** Pool `factory` has exactly one node and no autoscaler; the Deployment has one replica and `Recreate`;
  the disk is ReadWriteOnce. The blob lease is the second line (a second supervisor per repository cannot take the lock).
- **Termination.** `terminationGracePeriodSeconds` defaults to 600 (up to 3600). A rollout or a node drain waits that long, so
  **drain every repository first** (`docs/operations.md`).

## Bootstrap

All commands below are for the operator to run; this repository runs none of them. Replace every `<<PLACEHOLDER>>`.
You need `terraform`, `az` (logged in), `kubectl` and `kubelogin`.

1. **Resource providers** (the provider is configured with `resource_provider_registrations = "none"`, so the deploying identity
   needs no subscription-wide rights):
   ```sh
   for ns in Microsoft.ContainerService Microsoft.Compute Microsoft.Network Microsoft.Storage Microsoft.KeyVault \
             Microsoft.ManagedIdentity Microsoft.ContainerRegistry Microsoft.OperationalInsights \
             Microsoft.OperationsManagement Microsoft.Insights; do
     az provider register --namespace "$ns" --wait
   done
   ```
   `Microsoft.Insights` is needed for the Key Vault diagnostic setting and for the Container Insights data collection rule
   (`observability.tf`). The list must cover every namespace the Terraform resources touch, because nothing registers a provider at apply
   time. Check the vCPU quota of the `Standard DDSv5 Family` in the region: an upgrade or node image update surges a second D8,
   so plan for about 16 vCPU plus the system node.
2. **Resource group and registry.** Create the resource group named by `resource_group_name` in the region named by `location`
   (Terraform looks it up and checks the region). The registry (`acr_name`) is yours: push the first image yourself
   (`image/README.md`, "Build and check"), then copy the digest that `docker push` prints.
3. **State storage** (separate resource group, not managed by this Terraform):
   ```sh
   az storage account create --name <<TFSTATE_STORAGE_ACCOUNT>> --resource-group <<TFSTATE_RESOURCE_GROUP>> \
     --location <<AZ_LOCATION>> --sku Standard_LRS --min-tls-version TLS1_2 \
     --allow-blob-public-access false --allow-shared-key-access false
   az storage account blob-service-properties update --account-name <<TFSTATE_STORAGE_ACCOUNT>> \
     --resource-group <<TFSTATE_RESOURCE_GROUP>> --enable-versioning true \
     --enable-delete-retention true --delete-retention-days 14
   az storage container create --name <<TFSTATE_CONTAINER>> --account-name <<TFSTATE_STORAGE_ACCOUNT>> --auth-mode login
   ```
   The backend block in `versions.tf` is empty on purpose: the account, container and key are passed with `-backend-config`
   (a pipeline can pass them from its own variables):
   ```sh
   terraform -chdir=infra init \
     -backend-config="resource_group_name=<<TFSTATE_RESOURCE_GROUP>>" \
     -backend-config="storage_account_name=<<TFSTATE_STORAGE_ACCOUNT>>" \
     -backend-config="container_name=<<TFSTATE_CONTAINER>>" \
     -backend-config="key=<<TFSTATE_KEY>>" \
     -backend-config="use_azuread_auth=true"
   ```
4. **Identities for plans and applies (OIDC is optional, only if you run them from CI).** `providers.tf` does not pin an authentication
   method: the providers and the backend use whatever the environment offers. Locally that is `az login`; from a pipeline a login step
   (for example the `azure/login` action with a federated credential) signs in and Terraform then uses the Azure CLI session. The
   kubernetes provider reaches the cluster with the same session through `kubelogin --login azurecli`. If you run plans and applies
   from GitHub Actions, create **two** identities (Entra app registrations or user-assigned managed identities), each with federated
   credentials with issuer `https://token.actions.githubusercontent.com` and audience `api://AzureADTokenExchange` (another CI system
   has its own issuer and subjects). Repositories created after 2026-07-15, and repositories that opt in, use the immutable form with
   owner and repository IDs, for example `repo:<<ORG>>@<<OWNER_ID>>/<<REPO>>@<<REPO_ID>>:pull_request`; use the form your
   repository's tokens carry.

   | Identity | Federated credential subject(s) | Roles |
   |---|---|---|
   | **Plan** (read-only) | `repo:<<ORG>>/<<REPO>>:pull_request` (a plan on pull requests), `repo:<<ORG>>/<<REPO>>:ref:refs/heads/<<DEFAULT_BRANCH>>` (the preview plan of an apply; a job that references no environment) | `Reader` on the resource group and the registry, `Storage Blob Data Reader` on the state container, **`Azure Kubernetes Service Cluster User Role`, `Azure Kubernetes Service RBAC Reader` and the custom role `Factory Plan Cluster Reader` on the resource group** (the provider reads the cluster credentials and the Kubernetes objects; none of the roles can change anything; the custom role is created from `infra/roles/factory-plan-cluster-reader.json`, see below) |
   | **Apply** | `repo:<<ORG>>/<<REPO>>:environment:production` (the subject of an apply job that runs in a protected environment) | `Contributor` and `Role Based Access Control Administrator` (or `User Access Administrator`) on the resource group, the ability to create the `AcrPull` assignment on the registry, `Storage Blob Data Contributor` on the state container. Its **object ID must be listed in `aks_cluster_admin_principal_ids`** (the plan identity must not be: the variable is the only source of these role assignments, so the preview plan and the apply plan have the same action list). Terraform then grants it `Azure Kubernetes Service RBAC Cluster Admin` on the cluster, which it needs to create the Kubernetes objects |

   An identity that pushes images (if you build in CI) is a third one: `AcrPush` on the registry and nothing else, and not the plan or
   apply identity (`image/README.md`, "Build and check").

   **Custom role `Factory Plan Cluster Reader`.** The built-in RBAC Reader does not cover the cluster-scoped reads of the
   kubernetes provider: it lists CRDs (to look up the `SecretProviderClass` kind), storage classes, custom resources and, once
   monitoring is on, the cluster role and cluster role binding Prometheus needs, and without them every plan fails with
   `customresourcedefinitions ... is forbidden` or `clusterroles.rbac.authorization.k8s.io "factory-prometheus" is forbidden`.
   `infra/roles/factory-plan-cluster-reader.json` defines the role with exactly seven data actions and no `Actions` or write data actions:
   `Microsoft.ContainerService/managedClusters/apiextensions.k8s.io/customresourcedefinitions/read`,
   `Microsoft.ContainerService/managedClusters/storage.k8s.io/storageclasses/read`,
   `Microsoft.ContainerService/managedClusters/customresources/read`,
   `Microsoft.ContainerService/managedClusters/rbac.authorization.k8s.io/clusterroles/read`,
   `Microsoft.ContainerService/managedClusters/rbac.authorization.k8s.io/clusterrolebindings/read`,
   `Microsoft.ContainerService/managedClusters/rbac.authorization.k8s.io/roles/read` and
   `Microsoft.ContainerService/managedClusters/rbac.authorization.k8s.io/rolebindings/read` (the last two are for the Role and RoleBinding of the worker namespace that the pod backend adds; both names are in the public Azure permissions reference for `Microsoft.ContainerService`). Create it once (you need a role that can write role
   definitions, such as `Owner` or `User Access Administrator`, on the resource group), then assign it to the plan identity:

   ```sh
   SUB=<<SUBSCRIPTION_ID>>; RG=<<RESOURCE_GROUP>>   # the subscription and the value of resource_group_name
   ROLE_FILE=$(mktemp)
   sed -e "s|<<SUBSCRIPTION_ID>>|$SUB|" -e "s|<<RESOURCE_GROUP>>|$RG|" \
     infra/roles/factory-plan-cluster-reader.json > "$ROLE_FILE"
   az role definition create --role-definition "$ROLE_FILE"
   az role assignment create --assignee-object-id <<PLAN_PRINCIPAL_ID>> --assignee-principal-type ServicePrincipal \
     --role "Factory Plan Cluster Reader" --scope "/subscriptions/$SUB/resourceGroups/$RG"
   ```

   **The pod backend needs the role updated BEFORE its first enabling flip (a human action, the runbook's Part F).** With `pod_backend_enabled` off the plan reads none of the new kinds and passes with the old role, but the first plan after the flip (and the preview plan of an apply, if it also runs as the plan identity) reads the Role, the RoleBinding and the two admission kinds of `admissionregistration.k8s.io`, `validatingadmissionpolicies` and `validatingadmissionpolicybindings`. **The two admission kinds have no data action in the public Azure permissions reference** (it lists the webhook configurations and the discovery paths of that group, not these two), so they are NOT in the role definition: a role definition that names an action the provider does not define is refused by `az role definition create`, which would block the two names above as well. Whether the Azure authorizer maps the two kinds to `customresources/read` (already granted) is UNVERIFIED; check it with a plan run as the plan identity on the mirror, whose role definition is updated first, and if that plan fails with Forbidden on them, stop and report: do not widen the role by guessing a name.

   **When the JSON gains a data action, update the live role too** (the Terraform does not manage it; seen on 2026-11-09, when turning monitoring on made every pull
   request plan fail until the two RBAC reads were added). The same copy works, and the assignment stays:

   ```sh
   az role definition update --role-definition "$ROLE_FILE"
   ```

   A new data action takes a few minutes to reach the plan identity's tokens; re-run the failed plan afterwards.

   Edit `AssignableScopes` in the copy instead if you want a wider scope.
   `customresources/read` cannot be scoped per CRD: it reads every custom resource in the cluster. It exposes no secret
   values: Kubernetes `Secret` objects are not custom resources and are covered by no data action of this role.
   The Terraform in this directory does not manage the role or its assignment: the plan identity must not be able to
   grant itself anything, and the role has to exist before the first pull request plan.

   Why separate identities: a pull request runs **its own copy** of your pipeline definition, so whatever the plan identity can do,
   any author with write access can do. It therefore cannot change Azure resources or Kubernetes objects (read-only roles) and cannot write the
   Terraform state (a plan never writes state, so every plan runs with `-lock=false` and needs no write role; keep
   blob versioning and soft delete on anyway). An image push identity is separate for the same reason: it must not be reachable
   from a pull request's subject, and the plan identity must not be able to push images.
5. **GitHub settings.** Optional, and only if you run Terraform from GitHub Actions (or another CI) of your own; the pipelines of the
   reference deployment are not part of this repository, so write yours with a read-only plan identity (step 4) and an apply behind an
   approval. What this document and the scripts assume: a repository (or CI) variable named `INFRA_TFVARS` holding the operator's tfvars
   text, that your pipeline passes to `terraform` with `-var-file`: the Terraform inputs from `terraform.tfvars.example` **except**
   `image_digest` (committed in `image.auto.tfvars`), and with `subscription_id` and `tenant_id` either in it or taken from your pipeline's
   own variables. None of it is secret. `INFRA_TFVARS` needs the new inputs `ingress_dns_label`, `caddy_image_digest` and
   `aks_cluster_admin_principal_ids` (the apply identity and the operators), and `image_repository` must equal the repository
   name you push the image to. `scripts/register-repo.sh` edits the `repo_configs` map of an Actions variable of that name
   (`docs/enrolling-repositories.md`); if you keep the text elsewhere, edit the map there. If you use a GitHub environment named
   `production` for the apply job, give it **all** of these settings; they are the real controls, an `if` in the pipeline file is not (a
   dispatched run executes the pipeline file of the branch it is dispatched from, and the OIDC subject of a job with an environment
   carries the environment name, not the branch):
   - *Deployment branches and tags*: "Selected branches and tags" with one rule, the default branch. Do not add `refs/pull/*/merge`.
   - *Required reviewers*, with "Prevent self-review" when there are two or more eligible reviewers (with a single reviewer it
     would block every apply). Required reviewers need a public repository or a paid plan.
   - "Allow administrators to bypass configured protection rules" turned off.

   If you run `tools/infra_cluster_guard.py` on pull requests, also create the label `infra-cluster-change` (any colour; it carries no
   secret). A pull request whose plan changes the AKS cluster or a node pool needs it, otherwise the guard refuses its plan (see "Safe
   deployments", "Cluster guard"); a pipeline should plan again when a label is added or removed, so that the guard always reads the
   current labels.
6. **Entra app registration for the API** (the audience of the manager's bearer tokens). Terraform does not create it:
   - Create a single-tenant app registration. Set the Application ID URI to `api://<<ENTRA_APP_CLIENT_ID>>` (Expose an API).
   - Make it issue **v2 tokens**: set `accessTokenAcceptedVersion` to `2` in the manifest (the Microsoft Graph manifest calls the field
     `api.requestedAccessTokenVersion`; `az ad app update --id <<ENTRA_APP_CLIENT_ID>> --set api.requestedAccessTokenVersion=2`).
     The manager accepts only the issuer `https://login.microsoftonline.com/<tenant>/v2.0`; a v1 token (issuer `sts.windows.net`) is
     refused.
   - Two app roles, values `Factory.Operator` and `Factory.Viewer` (generate a fresh GUID for each role `id`):
     ```json
     [
       {"allowedMemberTypes": ["User", "Application"], "displayName": "Factory Operator", "description": "Run and change Factory runs",
        "id": "<<ROLE_ID_OPERATOR>>", "isEnabled": true, "value": "Factory.Operator"},
       {"allowedMemberTypes": ["User", "Application"], "displayName": "Factory Viewer", "description": "Read Factory status",
        "id": "<<ROLE_ID_VIEWER>>", "isEnabled": true, "value": "Factory.Viewer"}
     ]
     ```
   - On the enterprise application (the service principal of the registration) turn on **"Assignment required"** and assign people,
     groups or workload identities to the roles. By default any user of the tenant can request a token for the app; this step is
     what limits access, together with the role check in the manager (a valid token without a role gets 403).
   - Pass `entra_allowed_audiences` as `["api://<<ENTRA_APP_CLIENT_ID>>", "<<ENTRA_APP_CLIENT_ID>>"]`: a v2 token's `aud` is the
     client ID, whichever form the client asked for.
   - **Getting a token (see "Call the API") needs one more decision, and the consent caveat below applies to people.**
7. **First apply, stage 0: create only the vault.** The two secret versions are required variables, and the secrets cannot be
   uploaded before the vault exists. Create just the vault with syntactically valid dummy versions, upload the secrets, then apply
   for real:
   ```sh
   terraform -chdir=infra apply -var-file=<<YOUR_TFVARS>> \
     -var=anthropic_api_key_secret_version=00000000000000000000000000000000 \
     -var=github_app_private_key_secret_version=00000000000000000000000000000000 \
     -target=azurerm_key_vault.this -target='azurerm_role_assignment.key_vault_secrets_officer'
   ```
   Grant yourself `Key Vault Secrets Officer` (list your object ID in `key_vault_secrets_officer_principal_ids`, or assign the role by
   hand) and add your public IP to `key_vault_ip_rules` for the duration of the upload, because the vault firewall is default-deny.
8. **Upload the two secrets** (names are fixed). Use files in a private directory, not command-line values, so that nothing ends
   up in shell history, then delete them. Keep `-o none`: `secret set` otherwise prints the secret's value:
   ```sh
   az keyvault secret set --vault-name <<KEY_VAULT_NAME>> --name github-app-private-key --file <<PRIVATE_KEY_PEM_PATH>> -o none
   az keyvault secret set --vault-name <<KEY_VAULT_NAME>> --name anthropic-api-key --file <<API_KEY_FILE_PATH>> -o none
   az keyvault secret show --vault-name <<KEY_VAULT_NAME>> --name anthropic-api-key --query id -o tsv
   ```
   The last path segment of each printed ID is the 32-character **version**: put them in `anthropic_api_key_secret_version` and
   `github_app_private_key_secret_version`. Then remove the temporary `key_vault_ip_rules`.

## First apply

The first apply has **two stages**, because `kubernetes_manifest` needs a reachable API server (and the Secrets Store CRD) when
Terraform plans, and the cluster does not exist yet. Until it does, a plan of everything fails with a kubernetes provider error
about the unknown host: that is expected, and it is also what a pipeline's plan on a pull request reports in a repository whose cluster has
not been created (the offline checks still run).

1. **Stage `cluster`**: the cluster, the `factory` node pool and the role assignments (and everything they need: network,
   identities, workspace). The names `stage=cluster` and `stage=all` are used below and by `tools/infra_cluster_guard.py`
   (`GUARD_STAGE`): `cluster` is the four `-target` lines here, `all` is a plain apply of everything. Your pipeline may offer them as an
   input; by hand they are the two commands in this section. Spikes S1-S3 must be recorded PASS.
   - Run it (`az login` as the apply identity or as an operator listed in `aks_cluster_admin_principal_ids`):
     ```sh
     terraform -chdir=infra apply -var-file=<<YOUR_TFVARS>> \
       -target=azurerm_kubernetes_cluster.this -target=azurerm_kubernetes_cluster_node_pool.factory \
       -target=azurerm_role_assignment.aks_cluster_admin -target=azurerm_role_assignment.aks_cluster_user
     ```
   Expect 10-20 minutes. If node pool creation times out, the outbound deny rule in `network.tf` is the first thing to relax
   ("UNVERIFIED", 2).
2. **Stage `all`** (the default): everything else. Wait a few minutes after stage 1: Azure role assignments can take time to
   propagate, and the CSI add-on installs its CRD asynchronously. If this stage fails with `Forbidden`/`Unauthorized` from the API
   server, or `no matches for kind "SecretProviderClass"`, wait and apply again. The Terraform resource does not wait for the
   rollout (a `wait` makes the kubernetes provider fail with "inconsistent result" on every image promotion, see `k8s.tf`), so follow every apply with
   `kubectl -n factory rollout status deploy/factory`: a pod that cannot start (image pull, Key Vault, disk, sandbox flags) fails there.
3. **After the apply.**
   - Add `nat_public_ip` to the GitHub organization's IP allow list (and to the registry firewall if the registry restricts public
     access).
   - Check the ingress: `curl -sS -o /dev/null -w '%{http_code}\n' https://<manager host>/healthz` must print `401` (no token), and
     `curl -sSI http://<manager host>/` must redirect to https. The host is in the output `manager_url`. The certificate comes
     from Let's Encrypt a minute or two after Caddy starts (`kubectl -n ingress logs deploy/caddy`).
   - Get a token and call the API ("Call the API"): `GET /repos` with a principal holding `Factory.Viewer` must return 200.
   - Re-run the sandbox check once on the node of this cluster (S1 ran on a different cluster):
     `kubectl -n factory exec deploy/factory -- factory-sandbox-check` must print `all expectations held`.
   - Credentials for `kubectl`: output `aks_get_credentials_command` (needs `aks_cluster_admin_principal_ids` membership).

## Call the API

The manager checks `Authorization: Bearer <access token>`. An operator or a script gets a token for the API's app registration:

```sh
TOKEN=$(az account get-access-token --resource api://<<ENTRA_APP_CLIENT_ID>> --query accessToken -o tsv)
curl -sS -H "Authorization: Bearer $TOKEN" "$(terraform -chdir=infra output -raw manager_url)/repos"
```

(`--scope api://<<ENTRA_APP_CLIENT_ID>>/.default` is the same request on the v2 endpoint.) The token holds the roles assigned to the
caller in its `roles` claim. Treat it as a password: do not paste it into logs or tickets.

**Known caveat (consent).** The registration above defines only app roles. `az account get-access-token --resource api://...`
for a **person** is a request by the first-party "Microsoft Azure CLI" application, and Entra can answer `AADSTS65001` (the user or
administrator has not consented to use the application) or "application does not expose any scope". Ways out, pick one with
your tenant administrator; none of them is applied by this repository:

- *Expose a scope and pre-authorize the CLI.* In Expose an API add a scope (for example `access_as_user`) and add the Azure CLI's
  client ID (`<<AZURE_CLI_CLIENT_ID>>`, the ID printed in the `AADSTS65001` message) as an authorized client application for it. Users
  then get tokens without a prompt, and the `roles` claim still comes from their app role assignments.
- *Grant tenant-wide admin consent* for the Azure CLI application on that scope.
- *Use a workload identity for scripts and CI.* A service principal or managed identity assigned the **Application** app role needs
  no consent: `az login --service-principal` (client secret or federated credential) and the same `get-access-token` command. A
  client secret is a secret, so keep it out of the repository and out of Terraform; prefer a federated credential.

If the token is accepted by Entra but the manager answers 401, the usual causes are a v1 token (`accessTokenAcceptedVersion` not 2),
an audience not listed in `entra_allowed_audiences`, or a token from another tenant. The manager answers the same 401 for all of
them on purpose.

## Promote an image digest

A release is **one changed value**. Nothing else in Terraform is edited for it.

1. Build and smoke-test the image (`docker build`, `bash image/smoke.sh`, `image/README.md`, "Build and check"), scan it if you like
   (fixable HIGH and CRITICAL findings, no suppression), and push it to your registry **by digest**: `docker push` prints the
   `sha256:` digest. The pipeline that built and pushed the reference deployment's image is not part of this repository.
2. Put the digest in **`infra/image.auto.tfvars`** (or in your `-var-file`: a `-var` or `-var-file` value beats the auto-loaded file), run
   `terraform -chdir=infra plan` and read it: the action list is normally only `update-in-place kubernetes_manifest.factory_deployment`
   (with the pod backend also the policy parameter ConfigMap, see "Cluster guard"), and the plan shows the image digest.
3. Drain every repository first (`drain` through the manager API, or the pause and drain steps of "Safe deployments"), apply, and wait for
   the rollout (`kubectl -n factory rollout status deploy/factory`): with `Recreate` the old pod is stopped (after up to
   `termination_grace_period_seconds`) before the new one starts, so the API is unavailable meanwhile. Afterwards read `status` for each
   repository; `interrupted` jobs, and `failed` ones with `agent exited -15` after a rollout that was not drained, need `retry` or `resume`.
4. Only after a successful apply and rollout, commit the digest to `infra/image.auto.tfvars` on the default branch, so that git says what is
   deployed. A failed release leaves git untouched.

Do not apply unless `spikes/S1.md`, `S2.md` and `S3.md` begin with `# S<n>: PASS` (`spikes/README.md`); nothing checks that for you.

**If you apply from a pipeline of your own,** make the apply a deliberate step: post the preview (the action list, the digest and a
fingerprint that covers both), let a reviewer approve, plan again in the apply and refuse to apply if the fingerprint differs from the
preview, so that a newer image that arrived while the approval was pending is caught instead of being deployed unseen; and cancel an
older run that is still waiting when a newer one reaches the same point, so that one approval is pending and it is for the newest digest.
Two applies at a time must not run (use one concurrency group for the apply).

A tag is never accepted (the variable validates `sha256:` + 64 hex characters), so what runs is always the exact image your build
produced. The Caddy image is pinned the same way (`caddy_image_digest`).

**Why not just ignore the image in Terraform?** It was tried on a real cluster and does not work with this Deployment:
`lifecycle { ignore_changes }` on a path inside a `kubernetes_manifest` is not honoured, and a deploy job that changes the image
makes the next apply fail with a server-side-apply field-manager conflict (forcing it makes Terraform revert the release). The
typed `kubernetes_deployment_v1` could ignore the image but cannot express `hostUsers`, `procMount` or `appArmorProfile`.

## Safe deployments

Applying restarts the Factory pod. With the watcher running there is almost never an idle moment, and a restart in the middle of a
build ends the job (`failed` with `agent exited -15` when the supervisor gets to terminate the worker and record it, `interrupted` when
the supervisor is killed first: `docs/operations.md`, "Drain, pause, and what each leaves behind") and wastes its model spend. So pause, drain
and wait **after the apply was approved (and its plan matched the preview, if you compare), and before `terraform apply`**, and resume after
the rollout. `tools/infra_deploy_gate.py` does the steps (an optional operator script: run it by hand or from your own pipeline;
`docs/operations.md`, "Safe deployments"):

| Step | What it does |
|---|---|
| `infra_deploy_gate.py pause` | `POST /trigger-pause {"paused": true}`: a marker in the manager's data directory; no label trigger, dependency resume or restart auto-resume starts a plan or run while it exists. Runs in progress are untouched. |
| `infra_deploy_gate.py drain` | `POST .../drain` for every repository that has a `run` supervisor (from `GET /repos`). |
| `infra_deploy_gate.py wait` | Polls `GET /repos` every 15 s for **30 minutes at most** and drains a supervisor that appears meanwhile. On timeout it exits 1 **before `terraform apply`** and its summary names the active runs; nothing was changed. A manager with the pod backend wired in (none is yet) also reports its worker pods in `GET /repos` (`worker_pods`), and the wait holds while one runs or cannot be read (`docs/pod-backend.md`, slice 20); a manager without that key adds nothing. |
| `terraform apply`, then `kubectl -n factory rollout status deploy/factory` | As before. |
| `infra_deploy_gate.py resume` | `POST /trigger-pause {"paused": false}`, retried for 5 minutes, then `POST .../run` for each run the script drained. Run it after the rollout check and also when an earlier step failed, so the pause is cleared on every path; if it cannot be, it exits 1 and says the pause is **STILL SET**. |

```sh
export FACTORY_MANAGER_URL=$(terraform -chdir=infra output -raw manager_url)
export FACTORY_MANAGER_AUDIENCE=api://<<ENTRA_APP_CLIENT_ID>>     # a value of entra_allowed_audiences
python3 tools/infra_deploy_gate.py pause && python3 tools/infra_deploy_gate.py drain && python3 tools/infra_deploy_gate.py wait
terraform -chdir=infra apply -var-file=<<YOUR_TFVARS>>
kubectl -n factory rollout status deploy/factory
python3 tools/infra_deploy_gate.py resume
```

**Setup.** The script needs `FACTORY_MANAGER_URL` (the `manager_url` output) and `FACTORY_MANAGER_AUDIENCE` (a value of
`entra_allowed_audiences`) in its environment and a logged-in `az` session; `GITHUB_STEP_SUMMARY` (optional) is where it writes its
summary and `GATE_STATE_FILE` (optional) where it remembers the drained runs between `drain` and `resume`.

**Prerequisites (not done by the repository, not verified).**

1. The apply identity's service principal (the identity of the `az` session that runs the script) needs the **`Factory.Operator`**
   app role on the hand-managed manager app registration (step 6 above creates the role; assign it on the enterprise application,
   "Users and groups" or `az rest` on `appRoleAssignments`). It is a workload identity with the **Application** app role: no secret and no consent
   prompt, the token comes from the logged-in session (`az account get-access-token --resource <audience>`). This needs a
   human with Entra administrator rights; without it the pause step fails with "az could not issue a token" and nothing is applied.
   Without the role, drain by hand before every apply.
2. The machine that runs the script must reach the manager ingress (`manager_url`). `ingress_allowed_cidrs`, if set, blocks CI runners (they have no
   stable address): then the pause step fails to connect and nothing is applied. Use a runner with a known egress address that is on
   the list, or leave the list empty.

The script never receives or prints the bearer token: `tools/infra_deploy_gate.py` fetches it from the logged-in session, keeps it in
memory, sends it only as the `Authorization` header and reports HTTP failures by status code only.

**If the wait times out**, read the summary, let (or tell) those runs finish, and run the steps again; clear the pause with `resume`
(or by hand: `curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d
'{"paused": false}' "$(terraform -chdir=infra output -raw manager_url)/trigger-pause"`; `GET /repos` shows `trigger_paused`).

**After the restart.** The pod restarts while the trigger is paused, and both continuations happen only after `resume` clears the pause.
`resume` first clears the pause, then starts `run` again (`POST .../run`) for every run `drain` drained (the script keeps their names in
its state file; a drained run is not `interrupted`, so nothing else would continue it). A run that cannot be started
is named in the summary and does not fail the script; an Operator starts it with `POST .../run`. Whatever the new pod finds
`interrupted` (normally nothing, since the wait drained every supervisor) is resumed once when the repository has
`auto_resume_after_restart = "once"` (the default `scripts/register-repo.sh` writes for new registrations; an existing entry keeps its
value): recovery holds that resume back while the pause is set and the manager runs it when the pause is cleared. A started run whose
jobs wait for a dependency merge is picked up by the dependency resume (`docs/manager.md`).

**Cluster guard.** Nothing else keeps a change to the AKS cluster or to a node pool away from an automatic path: if your pipeline applies
the whole configuration after an image build behind only one approval, and every input except the image digest comes from
`INFRA_TFVARS`, which is not in a pull request, nothing else stands between a changed input and the cluster. `tools/infra_cluster_guard.py` is the
control (an optional operator script; its vocabulary is GitHub Actions': event names, `::error::` lines, `GITHUB_STEP_SUMMARY`). Run it
right after each `terraform plan` (the preview plan, and the apply's own re-plan) and before an approval is asked and before the pause, the
drain and `terraform apply`: `tools/infra_cluster_guard.py ACTIONS_FILE [PLAN_JSON]`, with `GUARD_EVENT`, `GUARD_STAGE`,
`GUARD_SNAPSHOTS_TAKEN` and `GUARD_PR_LABELS` in the environment (the script's docstring has the rules). It only reads the action list
(`<actions> <address>` per changing resource) that the fingerprint, if you keep one, is computed from. Two classes of address are known, as data
in the tool; `tests/test_infra_cluster_guard.py` reads the `.tf` files and fails when a resource of these classes exists that is not
listed:

- `CLUSTER`: `azurerm_kubernetes_cluster.this` and `azurerm_kubernetes_cluster_node_pool.factory`, exactly the two addresses the
  `stage=cluster` targets reach.
- `POOL_OBJECTS`: node pools that stage cannot apply: `azurerm_kubernetes_cluster_node_pool.monitoring` (any index) and the
  `NodePool` and `AKSNodeClass` objects of node auto-provisioning (`kubernetes_manifest.worker_nodepool` and `kubernetes_manifest.worker_nodeclass`, `worker_pool.tf`) and the three objects of the worker namespace
  (`kubernetes_namespace_v1.worker`, `kubernetes_service_account_v1.worker` and `kubernetes_network_policy_v1.worker_default_deny`, `worker_ns.tf`) and the admission policy parameter and the four policy and binding manifests (`kubernetes_config_map_v1.worker_policy_params` and `kubernetes_manifest.worker_policy_pods`, `worker_policy_pods_binding`, `worker_policy_secrets`, `worker_policy_secrets_binding`, `worker_policy.tf`). Changing or deleting one is a manual disruption that ignores
  `do-not-disrupt`: deleting the NodePool or the namespace ends every worker session on it, and deleting the policy removes the fence around a worker pod. A later Kubernetes object of the worker namespace is listed in
  the pull request that adds it: `tests/test_infra_cluster_guard.py` reads the `.tf` files as text and fails for a Kubernetes resource that names the worker namespace and is not listed, whether it names it by `local.worker_ns_count`, by `var.pod_backend_enabled`, by a reference to `kubernetes_namespace_v1.worker`, `kubernetes_service_account_v1.worker` or `kubernetes_network_policy_v1.worker_default_deny`, by `local.worker_contract.names.worker_namespace`, by the literal `factory-workers`, through a local that holds one of these (in a `namespace` argument or a namespace label selector), or by naming another resource that does. One resource is exempted by name, `kubernetes_manifest.factory_network_policy`: it names the namespace only as the peer of its relay rule, lives in the manager namespace and is not gated by the variable, so turning the variable off updates it in place. The rule reads text, so the following is **not detected**: a namespace that is computed from other strings (`format`, an interpolation), read from a `var.` or a data source, or decoded from a file (`yamldecode`), in a resource that `pod_backend_enabled` does not gate (the same resource with the gate is detected).

| Run (`GUARD_EVENT`) | `CLUSTER` | `POOL_OBJECTS` |
|---|---|---|
| automatic, after an image build (`workflow_run`) | every action is refused | every action is refused, **except** an update of `kubernetes_config_map_v1.worker_policy_params[0]` that the plan JSON proves changes only `data.image`, from one `<repository>@sha256:<64 hex>` reference to another (with the pod backend the allowed image of the worker admission policy moves with every release; issue #265) |
| manual, `stage=all` (`workflow_dispatch`) | every action is refused | allowed; the apply must go through the drain gate |
| manual, `stage=cluster` | allowed; an update, delete or replace also needs the input `cluster_snapshots_taken` (a plain create, the first apply, does not) | allowed; the apply must go through the drain gate |
| pull request (`pull_request`) | refused unless the pull request carries the label `infra-cluster-change` (then a warning) | the same |

**The one exception on the automatic path (issue #265).** With the pod backend on, every image release updates the policy parameter ConfigMap (its `image` key is the one digest a worker pod may use, `local.image`, the same value as the launcher's environment), so without an exception every release would need a manual run. The pipeline therefore keeps `terraform show -json` of each plan in a file (that it removes afterwards) and passes it to the guard as a second argument (`PLAN_JSON`), which reads exactly one thing from it: that the resource change of `worker_policy_params[0]` is exactly `["update"]`, that `data` holds only `image` before and after, each a digest reference, that everything else of the object is equal and that nothing is unknown or sensitive. Then, and only then, the automatic run applies it, with the drain-gate notice (the apply pauses the trigger and drains the workers like the Deployment's). A missing, unreadable or oversized plan JSON for such an update makes the guard exit 2 (it cannot decide, nothing is applied); any other change of that object (another key, a tag, a replace, a delete, a create), any other pool object action in the plan and every cluster action stay refused as before. The manual run and the pull request plan behave as before (a pull request that changes the digest still needs the label `infra-cluster-change`, which no release needs any more).

`cluster_snapshots_taken` (`GUARD_SNAPSHOTS_TAKEN=true`) is a human attestation that a snapshot of the data disk and of the Caddy disk (and of the Prometheus disk when
monitoring is on) exists. The guard cannot check that and says so in the log. The step summary always shows "cluster-touching actions in
this plan: N" and the lines. A refusal fails the step with a fixed message and names no value; if the action list cannot be read, or the
event or stage is not one the guard knows, it refuses as well (exit status 2). A pull request with the label that is then merged applies
nothing; when its plan has such an action, every automatic run after the merge is refused until the change was applied by a manual `stage=cluster` apply: apply it promptly.

**Switching the data plane or turning on node auto-provisioning (issue #144).** Everything is **off by default**: with every variable below at
its default the plan has no line for the cluster, a node pool, the Deployment or the data volume, and no line at all for the new objects (a real
`terraform plan` is the proof; a text test checks only defaults and gating). The Cilium and node auto-provisioning values are
**never set by a pull request**: they live in `INFRA_TFVARS`, not in a pull request, so the guard is the only control. The operator sets a value
immediately before the manual `stage=cluster` apply that applies it (step (3) is a manual `stage=all` apply); any automatic run that races in is refused by the guard before the
approval. The order is fixed, each step is applied by hand, behind the drain gate (above), after snapshots, and only after the throwaway mirror has run it (done once on 2026-11-10; the live runbook is slice 21 of the pod backend program, `docs/pod-backend-runbook.md`, whose Part F is the plan-identity gate):

| Step | `INFRA_TFVARS` | Run | What it does |
|---|---|---|---|
| (1) Cilium | `network_data_plane = "cilium"` **and `network_policy = "cilium"`** (the in-place switch is refused without the second: `Cilium dataplane requires network policy cilium`, `CiliumDataplaneRequiresNetworkPolicyCilium`, seen on the first apply of 2026-11-10 on the reference deployment; only a cluster created fresh with the data plane alone, mirror 2, did not need it) | manual, `stage=cluster`, `cluster_snapshots_taken` | `update azurerm_kubernetes_cluster.this` **in place**: the provider documents that every node is reimaged at once; **measured on the mirror (2026-11-10): AKS replaced the nodes with surge nodes, the apply took 512 s and a single-replica `Recreate` workload on a ReadWriteOnce disk (the manager's shape) was down twice, 34 s and 43 s (77 s in total)**; the operator decides the window after that. **The in-place switch is the decision**: re-creating the cluster takes 500 s too (mirror 2) and adds a data disk copy and a cut-over. The drain gate pauses the trigger and drains the supervisors first; a session that is still running when its node goes ends as the section "Safe deployments" says for any restart (`failed` with `agent exited -15`, or `interrupted` when the supervisor is killed first), and the interrupted work is resumed from its branch tip. |
| (2) node auto-provisioning | `nap_mode = "Auto"` (`nap_default_node_pools` stays `None`: Factory defines its own pool) | manual, `stage=cluster`, `cluster_snapshots_taken` | `update azurerm_kubernetes_cluster.this` in place. Measured on the mirror: 229 s, no node restarted and no probe failure. Not combined with step (1): node auto-provisioning cannot be enabled while the data plane switches. |
| (3) NodePool objects | `worker_nodepool_enabled = true` and `family_vcpu_quota = <the limit of the VM family of worker_pool_sku>` | manual, `stage=all` (the `-target` lines of `stage=cluster` do not reach them) | `create` of `kubernetes_manifest.worker_nodeclass` and `kubernetes_manifest.worker_nodepool`. Both are listed under `POOL_OBJECTS`, so the guard refuses a plan that touches them in an automatic run after an image build (your own pipeline's) and a manual run prints the drain-gate reminder. They are planned only after step (2): the custom resource definitions must exist at plan time. |
| (4) pod backend | `pod_backend_enabled = true` (needs steps (1) to (3); the plan refuses it otherwise) | manual, `stage=all` (the namespace and its objects are `POOL_OBJECTS`: the automatic run refuses them, and turning the variable off later deletes the namespace and every worker pod in it) | `create` of `kubernetes_namespace_v1.worker`, `kubernetes_service_account_v1.worker` and `kubernetes_network_policy_v1.worker_default_deny`, and an in-place `update` of the agent ConfigMap (`kubernetes_config_map_v1.insights_agent`: `exclude_namespaces` under stdout and stderr, the full list `kube-system`, `gatekeeper-system`, `factory-workers`; a ConfigMap change restarts the agent, so the runbook checks that it has before the first session) and, with `factory_network_policy_enabled`, of the prepared `factory-ingress` policy (the relay rule). The namespace is created after the ConfigMap, so the exclusion exists before any worker pod. The admission policies and their parameter are created with it (`create` of `kubernetes_config_map_v1.worker_policy_params` and the four `kubernetes_manifest.worker_policy_*` objects; the plan identity must already have the updated custom role, see "Custom role"). The Role, the launcher and the relay are later slices: with this step alone nothing can create a pod in the namespace. The same apply creates the launcher's Role and RoleBinding (`worker_rbac.tf`, after the fence), the relay Service and the pod-backend additions of the Deployment (`pod_backend.tf`: the launcher, the relay, the init container `volume-perms`, the toleration), which **rolls the manager pod** behind the drain gate; the manager still refuses `execution = "pod"`. The pool taint is a later step (`manager_pool_taint_enabled`, `stage=cluster`, mirror first). |
| (5) right-size the manager (issue #148; **only after every enrolled repository runs with `execution = "pod"` and a week of sessions has been measured**, `docs/capacity.md`) | first `container_cpu = 3` and `container_memory = "12Gi"`, then `aks_user_pool_vm_size = "Standard_D4ds_v5"` | the container values: manual, `stage=all` (the Deployment restarts the manager: through the drain gate); the pool: manual, `stage=cluster`, `cluster_snapshots_taken` | `update` of the Deployment, then `delete,create azurerm_kubernetes_cluster_node_pool.factory` (a new node: the manager comes back on it from its ReadWriteOnce disk, a rollout with downtime). About $165 a month less. `factory-sandbox-check` and the Premium-disk attach are re-run afterwards. Not part of steps (1) to (4): with workers in the manager pod the size is the budget. |

**The plan for the window, as the mirror ran it (2026-11-10).** Only step (1) interrupts the manager; steps (2) to (4) did not on the mirror.
For step (1): pick a time with no session that matters; stop the trigger and drain through the gate (the supervisors end `interrupted` and resume from their branch tips when the manager is back); take the two disk snapshots and set
`cluster_snapshots_taken`; set `network_data_plane = "cilium"` in `INFRA_TFVARS` and start the manual `stage=cluster` apply. Expect **about 9 minutes of apply** (512 s on the mirror) and **about 80 seconds in which the manager is down**
(two moves, 34 s and 43 s, because of `Recreate` and one ReadWriteOnce disk); the reference deployment has one more pool (`mon`), which the mirror did not have, so allow 15 minutes and do not read a longer silence as a failure until the apply itself fails.
Afterwards, in this order: all nodes `Ready` and the manager pod `Running`; `factory status` answers; `factory-sandbox-check` passes (`docs/operations.md`, "Platform maintenance"); a second plan (`stage=all`) shows **no action on the
Deployment**; resume the trigger. Step (2) needs no window (a pod that moves would be the exception: watch it), step (3) and step (4) need none either, but each ends with the checks of Part D and Part G of the runbook
(`tools/live_pod_probe.py`, `tools/local_pod_e2e/harness.py run --no-cluster`) before the next one. **Turning the backend on for a repository** (`execution = "pod"` in its `repo_configs` entry; the manager's `FACTORY_POD_BACKEND` is already `on` after step (4)) is the last
act, and the only one that is a plain configuration change: it is undone by removing the line and applying again (no cluster action).

After a step the value **stays set**: removing `network_data_plane` or `nap_mode` later would ask for a replace of the cluster (going back from Cilium
forces a new cluster), which `prevent_destroy` on `azurerm_kubernetes_cluster.this` refuses. `worker_nodepool_enabled = false` later DELETES the NodePool,
a disruption that ignores `do-not-disrupt`: only by a manual apply through the drain gate, when no worker session runs. A second full plan (`stage=all`) after a step must
show no action on the Deployment: a Deployment replace restarts the manager on its ReadWriteOnce disk. The surge reserve is checked at plan time
(`worker_pool_limits_cpu` plus twice the vCPUs of the production pools in the same VM family must not exceed `family_vcpu_quota`; the default 32 is 8 nodes).
The figures depend on the configured pools and the worker size and the quotas are your subscription's own; a worked example, from one reference subscription
(read on 2026-11-09; yours will differ), in which the deployment ran
**12 vCPUs**, all `Dds_v5` (system `D2ds_v5`, factory `D8ds_v5`, `mon` `D2ds_v5`; 10 without the `mon` pool), the `Standard DDSv5 Family` limit was 65,
`Dsv6` and `Dsv7` carried 350 each and the regional total was 150. With the default `Standard_D4s_v6` no production pool is in the worker family, so this check only asks for the limit
within the family quota (`family_vcpu_quota = 350`); what binds is the **regional total, which Terraform does not check**: the limit plus twice 12 must fit 150, so the largest limit
is 124 (31 nodes), and the default 32 leaves 106 vCPUs before a surge and 94 while system, factory and `mon` surge together (read `cores` in `az vm list-usage`). With a
size of the production family (`Standard_D4ds_v5`, quota 65) the largest limit is 40 (10 nodes; 44 and 11 nodes only without the `mon` pool) and the default 32 leaves 21 before a
surge and 9 while all three production pools surge; a size can also be `NotAvailableForSubscription` in some zones of a region (in the reference subscription `Standard_D4ds_v5` was,
in one zone; `az vm list-skus` shows it for yours). The throwaway mirror shares the subscription and the
quotas, so its runbook prints these sums before it creates anything, which the validation cannot see.

**One local, one ceiling.** `local.worker_pool` (`node_vcpu`, `limits_cpu`, `pods_per_node`, `max_sessions`) is the only place the size of the pool is computed:
the NodePool `limits.cpu` reads it here, and the concurrency ceiling of the pod pool and the launcher's maximum of live pods will read `max_sessions`
(`floor(limits_cpu / node_vcpu) * pods_per_node`, 8 by default; `worker_pool_pods_per_node` is 1 because one repository per node is a prerequisite, so N
repositories need N nodes). `FACTORY_MAX_TOTAL_WORKERS` is not derived from it: it keeps governing the work that runs inside the manager pod.

**Deliberate removal of `prevent_destroy`.** A lifecycle block takes no expression, so the cluster cannot be protected only in production. To tear
the stack down on purpose, to re-create the cluster after a copy of the data disk (the fallback if the downtime of step (1) is not acceptable: the data
disk, the Caddy disk and the Prometheus disk live in the node resource group and are deleted with the cluster), or to delete the throwaway mirror, remove the
line in a scratch copy of `infra/` and never commit that. The data volume claim in `k8s.tf` carries its own `prevent_destroy`, so a Terraform destroy
of the mirror needs BOTH lines removed in the scratch copy (deleting the mirror's resource group avoids Terraform altogether).
`infra/test/test_worker_pool_design.py` and `infra/test/test_workload_design.py` fail if the committed files do not have them.

## Roll back

Set `image_digest` back to the previous digest in `image.auto.tfvars` (or your `-var-file`), drain as in "Safe deployments", and apply. The result is a new pod that
runs the old image. **Do not apply from a stale checkout while another apply is still running**: a plan takes the digest *committed* in
`image.auto.tfvars` at that moment, and if your release commits the new digest only **after** its apply and rollout, a plan made in that window applies the **previous** digest
and puts the old image back. Wait for the release to finish, including the commit of the new digest, and check that
`image.auto.tfvars` on the default branch has the digest you expect before you apply. Do not re-run an old automatic run to roll back.

Emergency path when you cannot run Terraform (the Deployment has two containers that use the image, the init container and the
Factory container):
`kubectl -n factory set image deploy/factory factory=<registry>/<repo>@sha256:<old> keyvault-sync=<registry>/<repo>@sha256:<old>`.
Afterwards put the same digest into `image.auto.tfvars`, otherwise the next apply moves the pod forward again. Rolling back never
rolls back run state on `/data`: it is not versioned with the image.

## Rotate secrets and change repository configuration

A pod reads its secrets and its repository config when it starts. The Deployment's pod template carries
`checksum/deploy-inputs` (a hash of `repo_configs` and the two secret versions), and the repository ConfigMap is immutable and named
with a hash of its content, so changing any of them rolls the pod at the next apply, which is the deliberate rollout point. The Secrets
Store driver's rotation is off and every object is pinned to a Key Vault **version**.

- **Anthropic key:** upload a new version (step 8's `secret set`, with `-o none`), drain all repositories, set
  `anthropic_api_key_secret_version`, apply, `run` again.
- **GitHub App key:** add a new key in the App settings, upload it as a new version of `github-app-private-key` (step 8), drain, set
  `github_app_private_key_secret_version`, apply, then delete the old App key.
- **Repository configs:** drain every repository, edit `repo_configs` (in `INFRA_TFVARS`), apply. The plan shows a new ConfigMap, an
  updated Deployment and the old ConfigMap deleted afterwards. This includes the operations settings `abandon_after`,
  `run_budget_usd` and `auto_resume_after_restart` (`docs/operations.md`).

## Registering a repository

A new repository is one more `repo_configs` entry in `INFRA_TFVARS` plus the Factory App on that repository.
`scripts/register-repo.sh` does both for **manual-merge** mode (dry run by default; `--apply` edits the variable
with a lost-update guard and prints only a diff of the `repo_configs` region); `scripts/enroll-repo.sh` does the
hand-off mode. Which to use, the options and the exit codes: `docs/enrolling-repositories.md`. Either way the
entry only takes effect when you drain, apply the Terraform (`terraform -chdir=infra plan`, read it, then apply, with the new `INFRA_TFVARS`
value) and check `GET /repos` (above, "Rotate secrets and change repository configuration"); the scripts never do that.

## Retained logs

A merged run is purged by the manager's janitor within about 15 minutes, and its worker transcripts, stderr and the supervisor and plan
logs go with the run directory. So that they can still be inspected, `factory purge` first copies them, **redacted** (token shapes
replaced, escape sequences removed) and **gzipped**, to the `logs-archive` container of the lock storage account, one blob per file:
`<org>__<repo>/<run>/<file>.gz` and `<org>__<repo>/<run>/<job>/<file>.gz`. The manager's janitor deletes copies older than
`logs_archive_retention_days` (default 30) once a day. Settings, prompt and result files are never copied.

- **Least privilege.** The `workload` identity gets *Storage Blob Data Contributor* on the `logs-archive` container only
  (`azurerm_role_assignment.logs_archive_blob_data_contributor`), the same role and scope pattern as the `locks` container. No account
  key exists. The lock account's firewall is unchanged (default deny, node subnet through the `Microsoft.Storage` endpoint).
- **No new input is required.** `logs_archive_retention_days` has a default. To use a directory on the data disk instead (the
  code's default when no account URL is set), remove the three `FACTORY_LOGS_ARCHIVE_*` lines from `app_env` in `locals.tf`; to turn the copy
  off, set `FACTORY_LOGS_ARCHIVE=off`.
- **Reading a copy.** Nothing in the manager serves them. With the *Storage Blob Data Reader* role on the container:
  `az storage blob list --auth-mode login --account-name <account> --container-name logs-archive --prefix <org>__<repo>/<run>/`, then
  `az storage blob download ... | zcat`. They hold what the log endpoints would have shown (redacted), which can still contain
  private repository content: grant that role as carefully as the Operator role.
- **Failure policy.** A copy that fails never blocks the purge (a run directory kept forever would fill the disk): the CLI prints one fixed
  line, `factory: log archive failed (<error type>); the logs go with the run`, the manager logs `log-archive failed ...` and counts it in
  `factory_log_archive_failures_total` (`docs/metrics.md`). Alert on it.

## Operations notes

- **Upgrades interrupt work.** A Kubernetes version change (`aks_kubernetes_version`) or a node image update drains the only
  `factory` node and surges a second D8 (the quota above); the disk follows the pod. `aks_node_os_upgrade_channel` defaults to
  `NodeImage`, which reimages weekly and so interrupts running work: set it to `None` and reimage deliberately, after a drain.
  After every upgrade run `factory-sandbox-check` again (`docs/operations.md`, "Platform maintenance"). A change that updates or replaces the
  cluster or the `factory` pool is refused by the cluster guard on the automatic path and with `stage=all` (see "Cluster guard"): apply it
  by hand with `stage=cluster` and `cluster_snapshots_taken=true`.
- **The data disk** is created in the cluster's node resource group (`rg-<name_prefix>-aks-nodes`). `reclaimPolicy: Retain` keeps it
  when the claim is deleted, **but deleting the cluster deletes that resource group and the disk with it.** Snapshot it first if
  the run state matters. `data_disk_size_gib` can be raised (the class allows expansion) and never lowered; the claim has
  `prevent_destroy`, so a plan that would replace it fails instead of starting on an empty disk. To tear the stack down on
  purpose, remove that `lifecycle` block first.
- **Logs.** `kubectl -n factory logs deploy/factory` and Container Insights (4-day retention). The Container Insights environment
  collection is off (ConfigMap `container-azm-ms-agentconfig` plus `AZMON_COLLECT_ENV=False`): leave both.
- **Container Insights** data collection rule `MSCI-<location>-<cluster>` and its association are declared in `observability.tf`
  (the add-on sends nothing without them), so `terraform destroy` removes them. The streams are `ContainerLogV2`, `KubeEvents`
  and `KubePodInventory`; `ContainerInventory` (container environments) is deliberately not collected.

## Monitoring

Prometheus and Grafana run **in this cluster**, in the namespace `monitoring`, as plain Kubernetes manifests
(`monitoring.tf`; the scrape config, the alert rules and the dashboards are files under `infra/monitoring/`). Alertmanager runs only
if you name a receiver. **Everything is off by default** (`monitoring_enabled = false`): nothing described here exists, and the
Factory pod and the Caddyfile are byte for byte what they were, until you turn it on. It is off because it needs three Key Vault
secrets and an Entra client that only you can create, and because, with it on, the Factory pod's own `SecretProviderClass` gains the
metrics token: a missing secret version would stop the Factory pod from starting.

| Piece | What it does |
|---|---|
| Manager metrics listener (`docs/metrics.md`) | Binds the **pod address** (downward API `status.podIP`, never `0.0.0.0`), port `9464` declared on the container, and answers `GET /metrics` only to `Authorization: Bearer <token>`. The manager refuses to start the listener on a non-loopback address without a token (`FACTORY_MANAGER_METRICS_TOKEN`), with a fixed error |
| ClusterIP Service `factory-metrics` (namespace `factory`) | The only way to the port. Not a LoadBalancer, not routed by Caddy (the Caddyfile never names it; `test_workload_design.py` fails if it does) |
| Prometheus `v3.15.1` | Scrapes `factory-metrics:9464` with the token (`credentials_file`) and the kubelet's cAdvisor series of each node (memory and CPU against limits, restarts, out-of-memory events); evaluates `monitoring/alert-rules.json`; 15 days on its own 8 GiB Premium disk (`Retain`, stops at 80 % of it). One replica, `Recreate` |
| Grafana `13.2.5` | Dashboards as code (`monitoring/dashboards/*.json`, provisioned from ConfigMaps, not editable in the UI), Prometheus as the only data source. State is an `emptyDir`: disposable |
| Alertmanager `v0.34.2` | Only with `alert_receiver != "none"`. The destination URL is a Key Vault secret |
| kube-state-metrics `v2.15.0` (issue #149) | **One more small pod** whenever monitoring is on: reads pods and nodes (and, with `monitoring_nodeclaim_metrics_enabled`, the Karpenter `NodeClaim` objects) through a read-only ClusterRole and serves them on a ClusterIP Service that Prometheus scrapes (job `kube-state-metrics`). It gives pending worker pods, the node count per NodePool, creation-to-Ready time per pod and NodeClaim condition times (`docs/metrics.md`, "NAP-side series"). No secret, no Azure identity, no route in the ingress. Requests 10m CPU and 64Mi, limits 200m and 256Mi. Created only with `monitoring_enabled`, so a default plan is unchanged |
| Identities | One user-assigned identity per component, federated to the service account of the same name, with `Key Vault Secrets User` **scoped to the one secret it reads** (not the vault) |

### What was left out, and why

- **No `kube-prometheus-stack`, no Helm provider.** A new provider changes `.terraform.lock.hcl` hashes that cannot be produced
  offline, the operator's CRDs complicate `validate` and every plan, and the stack's defaults (dozens of rule sets and dashboards)
  would have to be switched off one by one. Plain manifests through the kubernetes provider this directory already uses keep the
  scrape config, the rules and the dashboards in files that tests read. The cost is that you own the manifests: a Prometheus or
  Grafana upgrade is a change to `monitoring-images.auto.tfvars` (and, if a setting changed, to `monitoring/`), not a chart bump.
- **No node-exporter.** node-exporter is a DaemonSet with host access that would force the namespace to
  `privileged`; the kubelet's cAdvisor series already give pod memory and CPU against their limits (`container_memory_working_set_bytes`
  against `container_spec_memory_limit_bytes`), out-of-memory events and container starts, which is what the capacity question
  needs. Per-session footprint is already a manager metric (`factory_phase_p95_rss_mib`). A restart is `changes(container_start_time_seconds)`.
- **kube-state-metrics was left out at first and is run since issue #149**, as one small Deployment and not as part of a chart: it is the only
  source in the cluster of pending pods, the NodePool node count and the NodeClaim condition times (`docs/metrics.md`). It needs no host access
  (UNVERIFIED that it starts under the `restricted` settings of the namespace: nothing was applied). Karpenter's own metrics and events are
  **not** collected (they live in the AKS-managed control plane; the Azure-side route was not chosen, see `docs/metrics.md`, "Which source").
- **No `ServiceMonitor`/`PodMonitor`** (they are operator CRDs): the scrape target is a static address in `prometheus.yml`.

### Turn it on (in this order)

1. **Three Key Vault secrets** (the names are fixed; Terraform declares names only). Upload them yourself, then read the version ID
   from the printed `id` (its last path segment, 32 hex characters):
   ```sh
   az keyvault secret set --vault-name <<KEY_VAULT_NAME>> --name metrics-bearer-token --value "$(openssl rand -hex 32)" --query id -o tsv
   az keyvault secret set --vault-name <<KEY_VAULT_NAME>> --name grafana-oauth-client-secret --value "<<ENTRA_CLIENT_SECRET>>" --query id -o tsv
   # only with an alert receiver: the receiver's URL
   az keyvault secret set --vault-name <<KEY_VAULT_NAME>> --name alert-receiver-url --value "<<RECEIVER_URL>>" --query id -o tsv
   ```
   The metrics token needs at least 32 printable characters without spaces (`openssl rand -hex 32` gives 64). It is the same secret for the
   manager (as `FACTORY_MANAGER_METRICS_TOKEN`) and for Prometheus (a file); rotating it is a new version and a new variable value, and both
   pods roll together. You need `Key Vault Secrets Officer` on the vault (`key_vault_secrets_officer_principal_ids`) and, from outside the
   VNet, your address in `key_vault_ip_rules` while you upload.
2. **Entra.** Grafana signs in with the app registration that already protects the manager API, so the app roles `Factory.Operator` and
   `Factory.Viewer` arrive in the `roles` claim of the ID token without a second set of roles:
   - Authentication, platform **Web**, redirect URI **`https://<ingress host>/grafana/login/generic_oauth`** (the Terraform output
     `grafana_oauth_redirect_uri` prints it; the host is `<ingress_dns_label>.<location>.cloudapp.azure.com`).
   - Certificates and secrets: a new **client secret**; upload its value as `grafana-oauth-client-secret` (step 1). The client ID is
     the variable `grafana_oauth_client_id`.
   - Optional ID token claim `email` (Token configuration). Without it Grafana falls back to `preferred_username` for the e-mail address.
   - Enterprise application: set **Assignment required** and assign people and groups to `Factory.Operator` or `Factory.Viewer`. Grafana's
     role mapping is `Factory.Operator` to `Editor` (`grafana_operator_role = "Admin"` makes it `Admin`), `Factory.Viewer` to `Viewer`, and a
     person with neither role **cannot sign in** (`role_attribute_strict`). Nobody is anonymous, there is no local login form and no local admin user.
3. **Variables** (`INFRA_TFVARS`, see `terraform.tfvars.example`): `monitoring_enabled = true`, `metrics_token_secret_version`,
   `grafana_oauth_client_id`, `grafana_oauth_client_secret_version`. Optional: `monitoring_nodeclaim_metrics_enabled = true` (needs
   `nap_mode = "Auto"`) adds the NodeClaim series of kube-state-metrics; leave it off on a cluster without node auto-provisioning. The plan shows the new namespace, identities, role assignments,
   the Factory Deployment gaining the token, the metrics port and the pod-address host, and a new Caddy route. **Drain first**, as for every
   change that rolls the Factory pod (`docs/operations.md`, "Safe deployments").

   **The new Factory pod will not start until the synced Secret is recreated** (seen on the first deployment, 2026-11-09: the manager was down for
   about three minutes). The CSI driver here runs with rotation off (`aks.tf`), so it creates the Kubernetes Secret `factory-keyvault` once and never adds a
   *new key* to it: the Secret keeps `ANTHROPIC_API_KEY` and `GITHUB_APP_PRIVATE_KEY`, the pod asks for `FACTORY_MANAGER_METRICS_TOKEN`, and it sits in
   `CreateContainerConfigError: couldn't find key FACTORY_MANAGER_METRICS_TOKEN in Secret factory/factory-keyvault` even though the mount itself succeeded and lists
   all three objects (`kubectl -n factory get secretproviderclasspodstatus`). The Secret is only a copy of Key Vault, so deleting it is safe; the driver rebuilds it
   with every key the next time a pod mounts the volume. **Do this once the apply has started the rollout** (or at the first sign of the error):
   ```sh
   kubectl -n factory delete secret factory-keyvault
   kubectl -n factory delete pod -l app.kubernetes.io/name=factory     # the label is NOT app=factory
   kubectl -n factory get secret factory-keyvault -o json | python3 -c 'import sys,json; print(sorted(json.load(sys.stdin)["data"]))'   # three keys
   ```
   The same applies to **any new key** added to `secretObjects` later, and in reverse to nothing else: a changed secret *version* is a new pinned object and rolls with the Deployment.
4. **Check** (all of this is UNVERIFIED until you do it, item 22 onward below): `kubectl -n monitoring get pods`; the Prometheus targets page
   through `kubectl -n monitoring port-forward svc/prometheus 9090` shows `factory-manager` and `kubelet-cadvisor` up; browse to
   `https://<ingress host>/grafana/`, sign in, open "Factory overview"; `kubectl -n monitoring top pods` stays within the limits below; the target `kube-state-metrics` is up and the dashboard "Factory node auto-provisioning"
   renders (its worker-pod panels stay at zero until the pod backend creates worker pods; the NodeClaim panels need the optional variable and the API
   version in `monitoring.tf`, `ksm_nodeclaim_api_version`, checked with `kubectl get crd nodeclaims.karpenter.sh -o jsonpath='{.spec.versions[*].name}'`). Then, without
   signing in (a private window, or `curl` with no header): `https://<ingress host>/grafana/metrics` must not return Prometheus text, and
   `https://<ingress host>/grafana/api/health` must show `database` only, with no `version` or `commit`.

### Where an alert goes

By default **nowhere**: Prometheus evaluates `monitoring/alert-rules.json` and the alerts are visible on the Prometheus alerts page
(`kubectl -n monitoring port-forward svc/prometheus 9090`, then `/alerts`) and in Grafana (Alerting, "Alert rules", the Prometheus
data source's own rules). No Alertmanager runs, and no URL is invented. To get notifications set **both**:

| Variable | Value |
|---|---|
| `alert_receiver` | `slack` (an incoming-webhook URL), `msteams` (a Teams workflow or connector URL; Alertmanager's `msteamsv2` receiver) or `webhook` (any HTTPS endpoint that accepts Alertmanager's webhook JSON) |
| `alert_receiver_secret_version` | the version of the Key Vault secret `alert-receiver-url` that holds that URL (step 1) |

The URL lives only in Key Vault and in a file in the Alertmanager pod (`url_file`); it is never in Terraform, state, a ConfigMap or a log.
Alerts are grouped by name and repository, sent after 30 s, repeated every 12 h, with resolutions. The rules (each has a one-sentence
`runbook` annotation): manager not scraped for 15 min, kubelet series missing, scrape errors, watcher paused for 2 h or backing off, GitHub
App check unhealthy, manager 5xx, 401/403 above 1 per second, Entra key refresh failing, run at 90 % of `run_budget_usd`, a run drained
by the cost guardrail, jobs interrupted for 1 h or waiting for a person for 4 h, workers waiting for 2 h, history ledger above 80 % of its
default cap or truncated, `/data` below 15 % free, Factory pod above 90 % of its memory limit, out-of-memory kills and container restarts.
With kube-state-metrics (issue #149): it not scraped for 30 min, a worker pod Pending for more than 10 min or slow to Ready (more than 5 min from creation), a NodeClaim older
than 15 min that is not Initialized (only with `monitoring_nodeclaim_metrics_enabled`), a node of node auto-provisioning for 30 min with no worker pod running, and a worker pod Evicted by the
kubelet. They cannot fire before the pod backend creates worker pods, except the first. What kube-state-metrics cannot give (Karpenter's own evictions and `DisruptionBlocked` events) is in `docs/metrics.md`, "Not available, and why".
Thresholds that mention the default 64 MiB history cap must be changed with `FACTORY_HISTORY_MAX_BYTES`.

### Where it runs, and what it costs

By default the monitoring pods run on the **system node** next to Caddy, selected by `kubernetes.azure.com/agentpool=system`, with small
requests so that the sum fits next to the AKS add-ons of a `Standard_D2ds_v5` (2 vCPU, 8 GiB). Requests and limits (the pods are `Burstable`):

| Component | CPU request / limit | Memory request / limit |
|---|---|---|
| Prometheus | 100m / 1 | 512Mi / 1Gi |
| Grafana | 100m / 1 | 384Mi / 768Mi |
| kube-state-metrics (issue #149) | 10m / 200m | 64Mi / 256Mi |
| Alertmanager (optional) | 10m / 200m | 32Mi / 128Mi |
| Total requests | 210m (220m with Alertmanager) | 960Mi (992Mi with Alertmanager) |

The issue asks that the node-sizing decision be made **with measurements**. None could be taken here (no cluster). Measure on the first
deployment (`kubectl top node`, `kubectl -n monitoring top pods`, `kubectl describe node` for the allocatable and requested figures of the
system node, and Prometheus' own `container_memory_working_set_bytes{namespace="monitoring"}` on the "Factory resources and manager" dashboard
after a week), record the numbers and the node cost next to the managed alternative's estimate **in issue 48**, and then decide. If the system
node is too tight, set `monitoring_node_pool_vm_size` (for example `Standard_D2ds_v5`): a one-node pool `mon`, tainted
`workload=monitoring:NoSchedule`, that only the monitoring pods tolerate, so they can never compete with the Factory pod or the add-ons. That is
a real recurring node cost, which is why it is a variable and not the default. The size must be Premium storage capable (the Prometheus disk).

### Images

Pinned by tag **and digest** in `monitoring-images.auto.tfvars` (Kubernetes pulls the digest); `variables.tf` carries the same versions
without the digest as defaults and refuses `latest`, so a change of one without the other is caught by a test. How they were chosen and checked
(2026-11-08): the version tags were listed on Docker Hub (`prom/prometheus`, `grafana/grafana`, `prom/alertmanager`; the tag list API with an
anonymous pull token), a version with patch releases was chosen in each line (`v3.15.1`, `13.2.5`, `v0.34.2`), and the digest of the
multi-architecture index was read from the `Docker-Content-Digest` header of `GET /v2/<repository>/manifests/<tag>`. The images were **not**
started (no cluster, no container runtime for them here): item 23 below. The images are pulled from Docker Hub through the NAT address
(anonymous pull limits are per address, as for Caddy); point `*_image` at a copy in your registry (the cluster has `AcrPull` on `acr_name` only) if that proves unreliable.

kube-state-metrics' image is `registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.15.0` (`kube_state_metrics_image`). **Its digest is not pinned yet, and it was not
checked that this tag exists**: it was written without network access, and a digest is not typed from memory. Until an operator reads the digest of the
multi-architecture index (the `Docker-Content-Digest` header of `GET /v2/kube-state-metrics/kube-state-metrics/manifests/<tag>` on `registry.k8s.io`) and adds the
commented line of `monitoring-images.auto.tfvars`, the cluster pulls the tag, which a registry can move. The image has not been started either.

### Security summary

- **Metrics port.** Cluster-internal (ClusterIP, bound to the pod address), no route in the ingress, no Entra check (a scraper has no Entra token) but a
  **bearer token** from Key Vault (constant-time comparison, `401` otherwise, never logged, not inherited by child processes). Anyone in the
  cluster who has not got the token gets `401`. A `NetworkPolicy` that lets only Caddy reach `8080` and only Prometheus reach `9464` is **prepared
  and off** (`factory_network_policy_enabled`, the end of `monitoring.tf`): a `NetworkPolicy` is enforced only by the Cilium data plane
  (`network_data_plane = "cilium"`, issue #144), which the reference deployment does not run today, so until then the object would not be enforced, and a policy that
  selects the Factory pod admits only what it lists, so check the rules before turning it on (with `pod_backend_enabled` it carries a third rule, the relay port from
  the worker namespace).
- **Grafana.** Served only below `/grafana/`, Entra OIDC with PKCE, tenant-specific endpoints, role from the `roles` claim, strict: no role, no
  sign-in; no anonymous, basic, proxy or JWT auth, no sign-up, no local admin, no login form; session cookie `Secure`, `HttpOnly` (Grafana always sets it)
  and `SameSite=Lax` (Strict would break the return from Entra); no framing; HSTS; no Gravatar, no telemetry, no update or plugin update checks, no plugin
  catalogue or install at runtime (`plugin_admin_enabled = false`, `preinstall_disabled = true`), no external snapshots, no public dashboards. The client secret is a file from Key Vault
  (`GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET__FILE`). Caddy does not pass the `Authorization` header on to Grafana, so a manager bearer token that reaches
  `/grafana/` goes no further than Caddy; the manager's routes (including `/dashboard` and `/ui/`) are not named in the route and are untouched.
- **What answers without a sign-in.** Caddy hands every path below `/grafana/` to Grafana, so only Grafana's own settings decide what a stranger sees.
  Grafana's built-in Prometheus endpoint (`/grafana/metrics`) is on by default and has no password, so `[metrics] enabled = false` turns it off
  (nothing scrapes Grafana), and `[auth.anonymous] hide_version = true` removes the version and commit from `/grafana/api/health` and from the login
  page. What is left for an unauthenticated visitor: the redirect from `/grafana/` and `/grafana/login` to Entra (`auto_login`), `/grafana/api/health`
  (database status only) and the static files of the sign-in page. Everything else, the data sources and dashboards included, needs a Grafana session.
- **Break-glass.** There is none inside Grafana: with Entra down nobody signs in. The numbers stay reachable through
  `kubectl -n monitoring port-forward svc/prometheus 9090` (Cluster Admin required, no Grafana login involved) and, for the manager alone,
  `kubectl -n factory exec deploy/factory -- ...` against the pod address with the token.
- **Secrets.** Each secret is readable by one identity only and arrives as a file in the pod that needs it; no Kubernetes Secret is created for
  the monitoring stack (`check_refs.py` forbids one). Only Prometheus mounts a Kubernetes API token (read of nodes and `nodes/metrics`, nothing else).
- **What the numbers can leak.** Nothing beyond `docs/metrics.md`'s threat model: numbers and closed words plus the configured repository names.
  Grafana's `Editor` role can query anything Prometheus holds, including those repository names, which operators already know.

### Upgrades and teardown

Images: edit `monitoring-images.auto.tfvars` (tag and digest) and the default in `variables.tf`, in a reviewed pull request; the pods roll
(`Recreate`; Prometheus is down for the length of a start, which is a gap in the graphs, not a loss of data on its disk). There are no CRDs. Turning
`monitoring_enabled` off removes the namespace, the identities, the Caddy route and the metrics Service, and returns the Factory pod to the
loopback listener (it rolls once); the Prometheus disk is `Retain` and is deleted with the cluster's node resource group, or by hand.

## Changing the Factory pod's environment variables

The Factory Deployment is a `kubernetes_manifest`, and that provider fills the planned object from the prior state **by list position**. The container's
`env` list is therefore ordered so that existing positions never change: the sorted plain variables, then the Key Vault `secretKeyRef` entries, then the
metrics host (`fieldRef`, monitoring only), then **the optional plain variables (`app_env_optional` in `locals.tf`: the token broker command and the two
`entra_spa_*` inputs) last**. Setting, changing or unsetting one of those is safe. Inserting a variable into the sorted middle shifts every later entry, and where
one index changes from one kind of entry to another the apply fails *after* the preview passed, with
`Deployment.apps "factory" is invalid: ... env[N].valueFrom: Invalid value: "": may not have more than one field specified at a time` (seen 2026-11-09, the
first time `entra_spa_scope` was set; nothing was applied). A new optional variable belongs in `app_env_optional`. If you must change the middle, recover by
deleting the Deployment first (`kubectl -n factory delete deployment factory`, the Factory is down until the apply recreates it, and its data disk is untouched), then
apply: the plan then shows a *create*, which has no prior state to overlay. If your pipeline compares the apply's plan with a preview, delete the Deployment **before** the preview is made, or the apply refuses a plan that differs
from it.

## Browser sign-in and custom host name

The manager serves a dashboard at `https://<host>/ui/` (`docs/manager.md`, "The browser page"). The page signs people in with **Microsoft Entra ID in the browser**
(MSAL.js, authorization code flow with PKCE, redirect flow) and calls the manager's API with the access token; **the manager keeps verifying the bearer token
and the app roles itself** (`Factory.Viewer`, `Factory.Operator` from the `roles` claim), exactly as for the command line. There is no second trust root, no cookie
and no new secret. Everything below is off or unchanged until you do the steps.

### 1. The app registration (the one that already protects the API)

The page signs in with the registration that carries the app roles: its client ID is the API's audience, so the token the page gets is a token for this
API. Terraform does not manage the registration; do this in the portal or with `az`:

- **Authentication** > **Add a platform** > **Single-page application**, and add one redirect URI per host, exactly as printed by the Terraform output
  `dashboard_spa_redirect_uris` (for example `https://factory.example.org/ui/` and `https://<label>.<location>.cloudapp.azure.com/ui/`). Use the *Single-page
  application* platform and not *Web*: only that platform lets the browser redeem the code (CORS on the token endpoint) and issues refresh tokens to a browser.
  A URI cannot be in both platforms; the Grafana URIs (`https://<host>/grafana/login/generic_oauth`) stay under **Web**. The registration then has both platforms.
- **The command that works** (Microsoft Graph, by the application's object ID; the app roles and the token version are untouched):
  ```sh
  OBJ=$(az ad app show --id <client id> --query id -o tsv)
  az rest --method PATCH --uri "https://graph.microsoft.com/v1.0/applications/$OBJ" \
    --headers "Content-Type=application/json" \
    --body '{"spa":{"redirectUris":["https://<host>/ui/","https://<custom host>/ui/"]}}'      # 204, no output
  az ad app show --id <client id> --query spa.redirectUris      # read it back
  ```
  The PATCH **replaces** the list, so include every URI you want to keep. `az ad app update --set spa.redirectUris=...` is not an alternative: on a registration
  whose single-page-application platform is still empty it fails with `Couldn't find 'spa' in ''` and changes nothing. The `az ad app show` read-back is also the check
  that the platform is right (`spa` holds the URIs, `web` does not). Entra validates the redirect URI only after the user signs in, so an unauthenticated request to the
  authorize endpoint cannot tell a registered URI from an unregistered one: the first real sign-in is the test.
- The page asks for the redirect URI `<origin>/ui/` and no other; **do not register the host root** (`https://<host>/`), a wildcard or `http://` (except
  `http://localhost` for a local test). The post-logout redirect is the same `/ui/` URI. Leave **implicit grant** and hybrid flow off (both boxes unticked): the code flow does not use them.
- The manager's own audience handling is unchanged: `entra_allowed_audiences` still lists `api://<client id>` and the bare client ID. The page derives its client ID and scope
  from that list (`<client id>/.default`, which puts the user's app roles in `roles`; **the GUID form, not `api://<client id>/.default`: the page's registration is also the API, and Entra answers `invalid_request` / AADSTS90009 "requesting a token for itself" to a self-request by application id URI**, which the page shows as "The Microsoft sign-in did not complete (code: invalid_request)"). Only if the API is a different registration set
  `entra_spa_client_id` and `entra_spa_scope` (they become `FACTORY_ENTRA_SPA_CLIENT_ID` and `FACTORY_ENTRA_SPA_SCOPE`); with neither derivable the page says "not configured".
- **Version 2 tokens are required** for this flow too: the manager accepts only the issuer `https://login.microsoftonline.com/<tenant>/v2.0`. Check the registration:
  `az ad app show --id <client id> --query api.requestedAccessTokenVersion` must print `2` (the manifest calls it `accessTokenAcceptedVersion`; set it with
  `az ad app update --id <client id> --set api.requestedAccessTokenVersion=2`). A decoded access token (jwt.ms) shows `ver` `2.0`, `iss` ending in `/v2.0`, the client ID as `aud`
  and your role in `roles`.
- **Assignment required** and the role assignments of the enterprise application are the same as for the API (step 6 of "Bootstrap"): a person with no role signs in and then sees
  "no Factory role" (a `403`), never data. Consent: the page asks for the registration's own `.default`, which has no consent screen of its own beyond the standard sign-in
  (an administrator may pre-consent for the tenant if your tenant requires it).

### 2. Custom host name (optional)

Set `ingress_custom_host = "factory.example.org"` (in `INFRA_TFVARS`, or committed in `infra/ingress.auto.tfvars`, whose committed `factory.example.org` is a **placeholder**: replace it with a name you control or delete the file before a real deploy, or Caddy asks Let's Encrypt for a certificate for a name that is not yours; a value in `INFRA_TFVARS` wins over the file) to serve the same API, dashboard and (with monitoring) Grafana under a name of your own **as well as** the Azure name; the
Azure name keeps working. Nothing changes while it is null. What happens when it is set: the Caddyfile's one site block names both hosts, so Caddy asks Let's Encrypt for **a
certificate for each name** and renews them itself (no secret, no new component); Grafana's `root_url` follows the primary host (the custom one); the outputs `ingress_hosts`,
`dashboard_url`, `dashboard_spa_redirect_uris` and `grafana_oauth_redirect_uris` list what to register. The manager's audience and the token checks do not depend on the host.

Order that avoids a window where something does not work (the example uses Cloudflare for DNS; Let's Encrypt can issue the certificate for a Cloudflare-proxied name over HTTP-01, as explained below):

1. **DNS.** Create `factory.example.org` as a **CNAME** to `<ingress_dns_label>.<location>.cloudapp.azure.com` (the output `custom_host_dns_target`) or an **A** record to `ingress_public_ip`.
   Start with the record **DNS only** (grey cloud), so Let's Encrypt reaches Caddy directly.
2. **Registrations.** Add the redirect URIs of step 1 (the SPA ones, and the Web ones if monitoring is on) *before* you rely on the name: a missing URI shows as "Sign-in failed (code: ...)" in the page.
3. **Apply** with `ingress_custom_host` set. Caddy's pod rolls (the Caddyfile checksum changes) and obtains the second certificate on the first request or at start:
   `kubectl -n ingress logs deploy/caddy` shows `certificate obtained successfully` for each name. If `ingress_allowed_cidrs` is set, Let's Encrypt cannot validate (its addresses are not
   published): lift it for the issuance.
4. **Then, optionally, turn the proxy on** (orange cloud). In Cloudflare set **SSL/TLS encryption mode: Full (strict)**: Cloudflare talks HTTPS to Caddy and checks the Let's Encrypt
   certificate Caddy holds for that name. **Flexible** (plain HTTP to the origin) and **Full** without strict are wrong here: Caddy redirects HTTP to HTTPS (a redirect loop) and the strict mode is what makes the
   origin certificate matter. Leave "Always Use HTTPS" on if you like; Cloudflare does not redirect the ACME path `/.well-known/acme-challenge/`. Do not put a WAF or bot rule in front of that path.
   Cloudflare's firewall in front of an API with bearer tokens is a decision of its own; Cloudflare Access is not used here (it would also block command line clients on the same host).

**How the certificate is obtained behind a proxied record.** Caddy uses the ACME challenges Let's Encrypt offers, HTTP-01 (port 80) and TLS-ALPN-01 (port 443). Behind Cloudflare's proxy the
connection from Let's Encrypt ends at Cloudflare, so **TLS-ALPN-01 cannot succeed and HTTP-01 can**: the request for `/.well-known/acme-challenge/<token>` is forwarded to the origin on port 80, where Caddy
answers it. That is the only mechanism this repository implements (it works with the pinned stock Caddy image and needs no secret). Renewal repeats the same exchange, so keep port 80 open to Cloudflare's addresses.

*Documented alternative, not implemented:* the **DNS-01** challenge with a Cloudflare API token (scope: Zone DNS Edit for the one zone). It needs no inbound port 80/443 for issuance and survives a restricted
`ingress_allowed_cidrs`, but it needs a Caddy image built with the `caddy-dns/cloudflare` module (a custom image, pinned by digest like the current one), the token as a **new Key Vault secret** mounted through the
Secrets Store CSI driver into the Caddy namespace (a new identity, role and `SecretProviderClass`), and `tls { dns cloudflare {env.CF_API_TOKEN} }` in the Caddyfile. Choose it only if HTTP-01 cannot work (for example a locked-down NSG you cannot open).

**What the manager does with the host.** It reads no host name. The page takes its redirect URI from the address it was loaded from, so each host needs its URI registered. `Strict-Transport-Security` is added by the manager only
when Caddy says the request was HTTPS (`X-Forwarded-Proto`, from a private peer; `docs/manager.md`), for both hosts; with Cloudflare's proxy in front, Cloudflare connects to Caddy over HTTPS (Full strict) and Caddy sets the header.

## Checks (run offline)

```sh
terraform -chdir=infra fmt -check -recursive
terraform -chdir=infra init -backend=false -input=false
terraform -chdir=infra validate
tflint --chdir infra                       # bundled terraform ruleset only, recommended preset, no plugin download
python3 infra/test/check_refs.py           # unused variables, dangling references, GUID-like or hex literals, secret resources
python3 -m unittest discover -s infra/test # tests of the checker and the design guards of the Terraform and the monitoring files
```

`tests/test_infra_cluster_guard.py` (the main suite) runs the cluster guard on a sample action list for every event, stage and action and checks
its address lists against the `.tf` files.
`test_worker_pool_design.py` reads `variables.tf`, `aks.tf` and `worker_pool.tf` as text: every new cluster and worker-pool variable is off by default and no committed `*.auto.tfvars` sets one, the cluster arguments are omitted when null, the NodePool and AKSNodeClass objects are count-gated with their preconditions and role dependencies, `expireAfter: Never` and no `terminationGracePeriod`, one local feeds the limit and the ceiling, the surge-reserve formula counts only the production pools of the worker VM family, and the cluster has `prevent_destroy`.
`test_worker_ns_design.py` reads `worker_ns.tf`, `locals.tf`, `monitoring.tf` and `k8s.tf` as text: one variable, `pod_backend_enabled`, gates the namespace, the service account and the default-deny policy (count 0 by default); the namespace is privileged with its three preconditions and waits for the agent ConfigMap; the service account mounts no token and no binding names it; the policy denies ingress and allows exactly DNS, the relay port and TCP 443 outside five ranges, each egress rule with exactly one `to` block (the namespace and pod selectors of a rule are ANDed only inside one); the log exclusion is the full list under stdout and stderr and the off-state text is byte-identical to today's; the relay rule of the prepared policy exists if and only if the variable is on and its peers each select the namespace and the pod in one entry; any Role, RoleBinding, ClusterRole or ClusterRoleBinding that names the worker namespace or account lists the default-deny policy in its `depends_on` (none exists yet; the scan is shown on synthetic examples); the old "decoration" sentence of `k8s.tf` is gone.

`test_pod_backend_design.py` reads `worker_rbac.tf`, `pod_backend.tf`, `k8s.tf`, `aks.tf` and `locals.tf` as text and `src/factory/kube.py`, `launcher.py`, `relay.py` and `podcontract.py` with `ast` and regular expressions (never imported): the Role grants exactly the verbs the launcher's client uses and nothing else, the binding names the one subject the admission policies are bound to, both wait for the fence; every addition to the Deployment is a `concat` or `merge` of a local that is empty while the backend is off, the Deployment never spells the gate; the sidecars carry none of the three relaxations, the token is mounted by the launcher only, the init command is the contract's ids and paths, every environment name is produced once from the contract, the ceilings read the one pool local, and the relay Service cannot make Kubernetes inject `FACTORY_RELAY_PORT`; the manager taint is a separate default-off flip.

`test_policy_json.py` reads `policy/*.json`, `worker_policy.tf`, `locals.tf` and `src/factory/podcontract.py` (loaded by path) and `src/factory/podspec.py` (its rule ids, read with `ast`): the four objects, one per file; every literal (namespace, user name, labels, taint, keys, ports, paths, caps, class maxima, the image pattern, the environment allow-list) equals the contract; the rule ids equal `podspec.POLICY_RULES` in order; the policies are bound to one identity and one namespace with `failurePolicy: Fail` and the actions Deny and Audit; every Terraform object is gated by `pod_backend_enabled`, waits for both role assignments and takes the image from the launcher's local; a Role or RoleBinding that names the worker namespace must wait for both policy bindings. `tests/test_policy_cel.py` runs the CEL through an offline model (`tests/celmodel.py`) against the golden pod, the golden Secrets and every mutation of `tests/golden/policy_mutations.json` and `policy_secret_mutations.json`, and shows that removing or emptying any one rule is noticed; it is a model, not evidence that a real API server accepts the expressions.

**Admission matrix against a real API server (by hand: a kind cluster, `tools/local_pod_e2e/`, or the mirror).** `tools/policy_matrix.py` submits the golden objects (all must be admitted) and every mutation (each must be denied by the policy with a rule id, or be turned back into the golden object by the server, or be rejected before the policy) to an API server with `kubectl create --dry-run=server` as the launcher identity (`--as system:serviceaccount:factory:factory`). It uses argv lists only, bounds every call with a timeout and prints no manifest, response or secret. Without a kind cluster, run the admission matrix on the mirror from this branch and attach the output to the pull request: on the mirror cluster (a different `name_prefix`, its own state) with a kubeconfig that may impersonate, `PYTHONPATH=src python3 tools/policy_matrix.py fixtures | kubectl apply -f -`, then `kubectl apply -f infra/policy/pod-policy.json -f infra/policy/pod-binding.json -f infra/policy/secret-policy.json -f infra/policy/secret-binding.json`, then `PYTHONPATH=src python3 tools/policy_matrix.py status` (fails on a type-check warning of the API server) and `PYTHONPATH=src python3 tools/policy_matrix.py run`. The fixtures apply a namespace, a service account, the parameter ConfigMap with the golden image and a Role that lets the launcher identity create pods and Secrets (RBAC is not under test). A pull request that adds or changes the policies should not merge until the recorded output is attached.
`test_monitoring_design.py` reads the monitoring files as text: everything is off by default, each component reads one secret, the Grafana
configuration is parsed and checked (sub path, Secure cookies, no anonymous access, no `/metrics` endpoint and no version without a sign-in, strict role mapping, no telemetry or plugin installs), the route in the Caddyfile, the pinned images, the
network policy, and that every metric an alert rule or a dashboard uses is in the catalogue (`docs/metrics.md`). `tests/test_monitoring_assets.py` (the main
suite) checks the same files against `factory.metrics.CATALOGUE`: label names, closed label values and groupings.

`.terraform.lock.hcl` is committed. It carries the `h1:` hash for `linux_amd64` and the `zh:` hashes of the release zips of the three
providers. To add the `h1:` hashes of other platforms run `terraform providers lock -platform=darwin_arm64 -platform=windows_amd64`
against the real registry.

## UNVERIFIED

Everything below is a decision made without being able to test it against Azure. Check each item on the first real plan or apply.

0. **Retained logs (`logs-archive`).** The Blob REST calls of `src/factory/logarchive.py` (Put Blob, List Blobs with a marker, Delete
   Blob, API version 2023-11-03) run only against a fake transport in the tests. Unconfirmed on a real account: that the workload
   identity token for `https://storage.azure.com/` is accepted with the *Storage Blob Data Contributor* assignment on the one container
   (including List Blobs at the container and Delete Blob), that the node subnet's service endpoint admits the pod's requests the same
   way it admits the lock requests, that a few hundred MiB of gzip per run upload inside the 60 s request timeout, and that a role assignment
   at container scope needs no extra time to propagate before the first purge after the apply (a purge in that window logs the fixed
   failure line and loses that run's copy).

1. **Staged first apply and plan-time API access.** `kubernetes_manifest` (the Deployment and the SecretProviderClass) needs a
   reachable API server at plan time. The local kind test proved the manifests apply, show no drift on a second plan and roll
   correctly, with unknown values inside the manifest; it did not cover an unknown provider host. If
   `aks_api_authorized_ip_ranges` is non-empty, GitHub-hosted runners cannot reach the API server and every plan and apply from
   CI fails: use a runner with a known egress address.
2. **NSG lockdown against the AKS node bootstrap.** The outbound deny of everything except 443, `168.63.129.16` (53, 80, 32526), the
   instance metadata address and the overlay network has not run against a real node bootstrap. If node pool creation times
   out on the first apply, remove `deny-other-internet-outbound` first. Also unconfirmed: that AKS leaves the NSG of a
   bring-your-own subnet alone (documented, not seen), so the explicit 80/443 inbound rule is what admits the ingress.
3. **Binding the load balancer to the static address.** The Service sets `azure-pip-name`, the load-balancer resource-group
   annotation and `loadBalancerIP`, and the cluster identity has Network Contributor on the resource group (the cloud provider
   lists the group's public IPs when it reconciles the Service; the AKS static-IP documentation asks for the role on the
   resource group; a role on the single address was dropped for that reason). Not run against a real cluster. `externalTrafficPolicy: Local` with the
   `Microsoft.Network` health probe on the node is expected, not confirmed.
4. **Let's Encrypt.** HTTP-01 and TLS-ALPN-01 need ports 80 and 443 open to the internet, which `ingress_allowed_cidrs` (when set)
   prevents; the DNS label must resolve before issuance; rate limits apply to repeated issuance (the certificate disk avoids
   re-issuing on every restart). Caddy's behaviour, the Caddyfile and the removal of the `X-MS-CLIENT-PRINCIPAL*` headers with
   `Authorization` kept were checked locally with the official image; the real ACME exchange was not.
5. **Volumes in a `hostUsers: false` pod.** The spikes validated the Azure Disk. The ConfigMap volume, the CSI Secrets Store volume,
   the projected service account token of the workload identity webhook and the in-memory volumes are unproven with a user
   namespace on AKS (the Kubernetes documentation lists them as supported; NFS is the known exception). If the pod stays in
   `Init` or `ContainerCreating`, `kubectl -n factory describe pod` names the volume.
6. **Workload identity with the CSI driver and a pod that does not mount the API token.** `automountServiceAccountToken: false`
   is set; the webhook projects its own token and the driver requests tokens for the pod's service account, which is expected
   to be independent of it. If the secrets do not mount, that setting is the first suspect.
7. **Seccomp default.** The Factory container has no seccomp setting; S1 passed with the node default (Unconfined on AKS at the
   time). If a future AKS version turns `SeccompDefault` on, `bwrap` may fail again: re-run `factory-sandbox-check` after every
   Kubernetes or node image change.
8. **Pod Security labels.** The `factory` namespace is explicitly `privileged` and `ingress` is `restricted`. AKS enforces Pod
   Security Admission natively; the `restricted` pod was admitted by a local Kubernetes 1.33 API server, not by AKS.
9. **Rollout timing.** The Terraform resource does not wait for the rollout (see `k8s.tf`); allow about 25 minutes for the rollout.
   The first pull of the large image, the disk attach and the entrypoint's `chown` of a big `/data` happen inside the startup probe
   (5 minutes) and the Deployment's progress deadline (20 minutes).
10. **Node capacity.** 6 vCPU and 24Gi (the defaults) must fit next to the node's add-on pods on a D8: validated at 75 % of the VM,
    not seen on a real node. If the pod stays `Pending` for resources, lower `container_cpu` and `container_memory`.
11. **Container Insights.** The data collection rule and association are in Terraform (`observability.tf`), following the Azure
    Monitor Terraform onboarding text ("Copy the DCR and DCRA resources"), which gives no snippet in the page that was read; the
    resource arguments follow the azurerm schema and were only validated offline. Not observed: that the platform does not also
    create its own `MSCI-` rule (a second rule with the same name would make the first apply fail; then import it), that rows
    reach the workspace, and whether the ConfigMap's `env_var` switch takes effect immediately or after an agent restart. The apply
    identity needs the Microsoft.Insights rights to write the rule (`Contributor` on the resource group covers it).
12. **Roles.** AKS RBAC Cluster Admin including `listClusterUserCredential` (the Cluster User Role is assigned as well); that the
    read-only plan identity (Cluster User Role + RBAC Reader + the custom `Factory Plan Cluster Reader` on the resource group)
    can read everything the kubernetes provider reads at plan time, including the CRD and `kube-system`, and that a pull request
    plan no longer fails with `customresourcedefinitions ... is forbidden` after a fresh bootstrap (never run in a subscription
    where custom roles can be created; the role JSON and its data action names are unchecked against Azure); that the apply identity with resource-group scope can create the
    cluster together with its node resource group; `os_sku = AzureLinux3` being accepted in the target region.
13. **Disk expansion.** `data_disk_size_gib` raised on an existing claim is expected to expand it in place; if the provider plans a
    replacement, `prevent_destroy` stops the plan and the fallback is `kubectl patch pvc` plus aligning the variable.
14. **Log Analytics retention of 4 days.** Verified on a real apply: the workspace default cannot be set below 30 days on `PerGB2018`
    (the service answers 400 for 4 and for 7); the four-day floor of the Azure Monitor documentation is for tables, and the
    standard tables accept it. `observability.tf` therefore sets 4 days on `ContainerLogV2`, `KubeEvents`, `KubePodInventory` and
    `AZKVAuditLogs` only. Any other table, and anything an operator adds later, keeps the 30-day workspace default. Still
    unconfirmed: that `azapi_update_resource` keeps the standard tables' other properties intact on every re-apply.
15. **Terraform runner access to the vault.** The vault is default-deny; `azurerm_key_vault` may call the vault data plane when it is
    read. A GitHub-hosted runner is not in the VNet. If a plan fails on a 403 from the vault, run the plan from a self-hosted runner
    in the VNet, or allow the runner with `key_vault_ip_rules`. The lock account is managed through Azure Resource Manager only
    (`data_plane_available = false`, `storage_account_id`).
16. **Provider quirks.** The typed Kubernetes resources send some fields the API server defaults (the local test found one that
    mattered: the container-level `runAsNonRoot=false`, which Pod Security `restricted` refuses, hence the explicit
    `run_as_non_root = true` in `ingress.tf`). Other server-side defaults on the real AKS version could still show as plan noise;
    `lifecycle { ignore_changes }` is the fix.
17. **Registry reachability.** If the registry restricts public access, add `nat_public_ip` to its firewall, or use a private
    endpoint on the registry. The default Caddy image comes from Docker Hub through the NAT address (anonymous pull rate limits are
    per address); `caddy_image_repository` can point at a copy in your registry (the cluster has AcrPull on `acr_name` only).
18. **Image contract.** Not part of this directory but required by it: the manager listens on port 8080 and serves `/healthz`; the image has
    `/opt/factory/bin/python`, `/bin/sh` and `factory-sandbox-check`; the image sets `FACTORY_TOKEN_BROKER` (or `token_broker_command` is
    set); the entrypoint creates and owns `/data/repos` before dropping privileges; every `repo_configs` entry carries `[worker] uid =
    10002` and `gid = 10010` (the image sets `FACTORY_REQUIRE_WORKER_IDENTITY=1` and `variables.tf` checks the two lines at plan time).
19. **Key Vault audit logs.** The vault sends its `AuditEvent` log (identities, operations and secret names, never values) to the Log
    Analytics workspace, which also receives the pods' stdout.
20. **Safe deployments.** Not run against Azure (no network): that the apply identity's service principal, once it has the
    `Factory.Operator` app role, gets a token from `az account get-access-token --resource <audience>` in the session that runs
    `tools/infra_deploy_gate.py` (a federated service principal should); that the machine that runs it reaches `manager_url`
    (`ingress_allowed_cidrs` may block a CI runner). The role assignment needs a
    human Entra administrator. Live acceptance: apply while a build is in flight on a registered repository and confirm
    the job is drained rather than interrupted and resumes or finishes after the rollout.
21. **Provider lock file.** Only the `linux_amd64` `h1:` hash was computed; the checks of this repository run on `linux_amd64`. The monitoring stack adds no provider, so the lock file is unchanged.
22. **Monitoring: nothing was planned, validated by Terraform or applied.** The package registry was not reachable from where this was written, so
    `terraform init`, `terraform validate` and `tflint` could **not** be run on `monitoring.tf`; `terraform fmt`, `check_refs.py` and the tests ran. Run
    `validate` and `tflint` locally and read their result first. The new objects are `kubernetes_manifest` where the typed schema was not checked (Deployments, RBAC,
    NetworkPolicy, SecretProviderClass) and typed where it is the pattern already used here (namespace, service accounts, ConfigMaps, claim, Services).
    Conditional values (`cond ? {...} : {}`) rely on Terraform unifying them to maps, as `locals.tf` already does for `token_broker_command`.
23. **Monitoring images under these settings.** No image was started. Unconfirmed: that Prometheus, Grafana and Alertmanager run with a read-only root
    file system, a non-root numeric user, all capabilities dropped and `RuntimeDefault` seccomp (Grafana writes only `/var/lib/grafana` and `/tmp`, both
    `emptyDir`); that Grafana's `/run.sh` entrypoint exists at that path (the container command only generates a random signing key and then execs it); that every
    Grafana setting of `grafana.ini` is still valid in `13.2.5` and the Prometheus flags in `v3.15.1` (an unknown setting or flag stops the pod: read `kubectl -n monitoring logs`);
    that `/grafana/api/health` is the health path with `serve_from_sub_path`; that `[metrics] enabled = false` and `[auth.anonymous] hide_version = true` do what the
    defaults of Grafana's `main` branch say in `13.2.5` (the live check at the end of "Turn it on" tests exactly that); that `msteamsv2_configs` and `webhook_url_file` exist in `v0.34.2`.
24. **Entra sign-in.** Not run against Entra: that the ID token of the manager's app registration carries `roles` for an assigned user (it does for the app
    that issues the token), that `preferred_username` is present for guests, that PKCE and the `generic_oauth` redirect URI are accepted for a Web registration with a client
    secret, and that Grafana reads the role from the ID token without `api_url`. A person without an app role is refused by design (`role_attribute_strict`).
25. **Scraping across the cluster.** Not run: pod to pod from Prometheus to the `factory-metrics` Service (the NSG admits the overlay ranges), the kubelet scrape
    of `https://<node>:10250/metrics/cadvisor` with the service account token (needs `nodes/metrics`, granted by the ClusterRole; the NSG admits TCP 10250 inside the overlay ranges),
    and that the kubelet's certificate is **not verified** (`insecure_skip_verify`): AKS' kubelet serving certificates were not confirmed to chain to the cluster CA, and the token
    that is sent can only read node metrics. If your cluster's kubelet certificates verify against the cluster CA, replace it with `ca_file` in `monitoring/prometheus.yml.tftpl`.
26. **One secret per identity.** A `Key Vault Secrets User` role scoped to a single secret (`<vault id>/secrets/<name>`) is supported by Azure RBAC and expected to work with the
    Secrets Store CSI driver's `getSecret`; not seen. If the volume does not mount, widen it to the vault first to tell a scoping problem from an identity problem. Also not seen: the federated
    credentials for the three new service accounts, and the new service accounts' CSI mounts without the pod mounting the API token (Grafana, Alertmanager).
27a. **kube-state-metrics (issue #149).** Not run: that the image tag exists and starts under the `restricted` settings as 65534 (and that `/readyz` on 8081 and `/livez` on 8080 are the probe paths of that version), that its digest is pinned
    (it is **not**, see "Images"), the NodeClaim API version NAP serves (`ksm_nodeclaim_api_version` in `monitoring.tf` is `v1`), that the custom resource state file converts the condition times to numbers and the
    metric names it produces, how it behaves without the NodeClaim definition, its real memory and CPU, and that the Prometheus job keeps the series that the rules and the dashboard use. No NetworkPolicy
    limits who may read its port (`docs/metrics.md`, "Network isolation"). The live checks are listed there.
27. **Fit and cost of the monitoring pods.** Requests total 0.21 vCPU and 0.94 GiB (with kube-state-metrics, 10m and 64Mi); whether they fit on the system node beside the AKS add-ons and Caddy was not measured, and the
    measured node cost is **not recorded** (the issue requires it before the node-sizing decision is merged): see "Where it runs, and what it costs". Also unmeasured: Prometheus memory with 15 days of
    the series of this catalogue plus cAdvisor (the 1 GiB limit is a guess).
28. **Caddy and the Grafana route.** The `redir` and `handle` blocks and `header_up -Authorization` were written from the Caddy documentation and not run (no live check of a Grafana WebSocket through the proxy, of the
    redirect from `/grafana`, or that `/healthz`, `/dashboard` and the other manager paths answer as before). Check them after the first apply; with monitoring off the Caddyfile is unchanged.
29. **Live acceptance of the issue (operator).** Prometheus has the series; Grafana renders the Overview dashboard after an Entra sign-in; the monitoring components stay within their limits
    over a week; an alert reaches the receiver (set one and stop the manager for 15 minutes in a test deployment); the node cost is recorded in issue 48.
30. **Browser sign-in (operator).** Not run against Entra: that the vendored MSAL 5.25.1 completes the redirect flow against this tenant (the page was only exercised under node with a fake browser and a fake token endpoint:
    the authorize request carried `response_type=code`, PKCE `S256`, the `<origin>/ui/` redirect URI, the scope with `.default` and the fragment response mode, and the return leg reached the token endpoint; the
    processing of the answer was not completed offline); that the **Single-page application** redirect URIs are accepted and issue refresh tokens; that `api://<client id>/.default` is accepted when the page and the API are
    one registration; that the **`roles` claim is in the access token** of a single-page-application flow for a user with an app role (the design relies on it; if it is missing, check `accessTokenAcceptedVersion`,
    the role assignment and "Assignment required", or request an API scope you exposed with `entra_spa_scope`); that refresh-token renewal works from the browser (24 hours) and that a silent renewal needs no frame.
    Check: sign in, open the browser's network tab, see `Authorization` only on same-origin calls, then sign out.
31. **Content Security Policy in a real browser.** The policy (`connect-src 'self' https://login.microsoftonline.com`, no inline script or style, `frame-ancestors 'none'`, `form-action 'none'`) and `Cross-Origin-Opener-Policy: same-origin`
    were checked as text and against the page's code, not in a browser: that MSAL makes no request to another origin (its instance discovery for a well-known host is built in, so none is expected), that the redirect flow works with the
    opener policy, and that the `Permissions-Policy` names raise no console error. A console violation report is the first thing to check if the sign-in button does nothing.
32. **Custom host certificate (operator).** Not run: that Let's Encrypt issues for the custom name through a Cloudflare **proxied** record with HTTP-01 (and that Cloudflare forwards `/.well-known/acme-challenge/`), that Caddy
    holds two certificates from one site block, that **Full (strict)** accepts the Let's Encrypt certificate, and that renewal works. The DNS-01 alternative above was not built or tried.
33. **HSTS.** That the header reaches the browser for both hosts only over HTTPS, and that nothing in front (Cloudflare's own HSTS setting) conflicts with `max-age=31536000` without subdomains. `X-Forwarded-Proto` from Caddy to the manager
    was taken from Caddy's documentation (it sets the header and replaces a client's value when `trusted_proxies` is not configured), not observed.
34. **Grafana on two hosts.** Grafana's `root_url` is the primary host, so a visit through the other host is sent to the primary one at sign-in; the redirect behaviour with `serve_from_sub_path` on both names was not run.
35. *(Withdrawn: it concerned a pipeline trigger that is not part of this repository. The number stays so that the items below keep theirs.)*
36. **Cluster guard (`tools/infra_cluster_guard.py`).** Covered by unit tests (`tests/test_infra_cluster_guard.py`) and the `terraform plan` of the pull request that first wires it
    into your pipeline (the guard is evaluated on that pull request's own plan); it was not exercised in a real pipeline run, and nothing exercises it on the throwaway mirror (which
    never runs your pipeline). Until the first real run, unverified: that the plan of production today shows no action on the four addresses (if it does, the
    first automatic run after an image build is refused; that is a finding to fix first, not a reason to weaken the guard); the exact action strings
    Terraform prints for a replace (`delete,create` or `create,delete`; the guard accepts both and any other change of a listed address); that your CI plans again when a label is
    added to or removed from a pull request that touches `infra/` and so refreshes the labels the guard reads; and that the label list your pipeline passes for an event without a pull
    request is `[]`.

37. **Cilium, node auto-provisioning and the worker pool (`worker_pool.tf`, the cluster arguments of `aks.tf`).** Nothing was applied, planned against Azure or run against a
    cluster; the checks that ran are the offline ones above, and `terraform validate` cannot check the fields of a `kubernetes_manifest`. Unverified: that omitting
    `network_data_plane`, `network_policy` and the `node_provisioning_profile` block gives no plan line (a real `terraform plan` is the check; provider source reading says the block
    is optional and computed in azurerm 4.81 and the data plane defaults to `azure`); that both attributes are "update in place", not "replace", on a scratch copy of the mirror; the
    downtime of the Cilium switch and the NAP-before-Cilium ordering; whether `network_policy` is needed with the Cilium data plane; a possible update gap on a
    NAT gateway cluster; Free-tier node auto-provisioning; node auto-provisioning under the reference deployment's NSG and NAT gateway (the outbound type cannot change once it is on, and whether
    that applies to an existing cluster is not stated); that the kubernetes provider's `kube_config` is not unknown at plan time after an in-place cluster update (every `kubernetes_manifest`
    would then misplan); `karpenter.sh/v1` and `karpenter.azure.com/v1beta1` against the installed definitions; where `expireAfter` belongs (`spec.template.spec`, as in the default pool, or
    `spec.disruption`, as one page shows); the kubelet field `podPidsLimit` and its 4096 (the documentation's example, PROVISIONAL); `startupTaints` on a custom pool; whether a node
    that holds only DaemonSet pods counts as empty for `WhenEmpty`; whether the apply identity may create these custom resources and whether the read-only plan identity can read them at plan time
    (a new kind may need one more read data action of the custom plan role, which a human changes in Azure); the vCPU quota figures (for example, one reference subscription read on 2026-11-09 showed a regional total of 150 with 12 in use,
    `Dds_v5` 65, `Dsv6` and `Dsv7` 350; `family_vcpu_quota` is an input the operator reads with `az vm list-usage`, and the regional total is not checked by Terraform); the allocatable CPU and memory
    of `Standard_D4s_v6` and whether the NodePool `sku-name` requirement accepts it (measured on `Standard_D4ds_v5` only). S1 and `factory-sandbox-check` must be re-run on a node of the pool after the apply (`docs/operations.md`, "Platform maintenance").

38. **The worker namespace (`worker_ns.tf`, the log exclusion in `locals.tf`, the relay rule in `monitoring.tf`).** Nothing was applied, planned against Azure or run against a cluster; the checks that ran
    are the offline ones above, the provider-free evaluation of the two data locals (the ConfigMap text and the policy rules, off and on) and `terraform validate`, which checks the typed resources
    but cannot enforce anything. Unverified: that a real `terraform plan` shows no line with the default (a text test pins only the gating); that Cilium enforces `ipBlock.except` on this cluster
    (the Cilium documentation says an `ipBlock` cannot select pod or node addresses, so the pod, service and node ranges in the exception may be redundant or may be handled as pod and node identities) and
    whether `network_policy = "cilium"` must also be set; that the DNS rule (the kube-dns pods, port 53) still matches after Cilium translates the service address; that the API server's public address is
    NOT excluded (not relied on: the canary and the live negative control decide); the `exclude_namespaces` key spelling and the agent's restart behaviour on AKS, and the agent's documented default list
    (`kube-system`, `gatekeeper-system`), which was read in Microsoft's data-collection ConfigMap article and not tested on a cluster; that the read-only plan identity can read a namespace, a service
    account and a network policy through the built-in reader role (the custom plan role is not changed in this slice); that a worker pod's own `stdout` is then really absent from Log Analytics. The
    prepared `factory_network_policy` is not turned on by this change.

39. **The admission policies (`worker_policy.tf`, `policy/*.json`, `tools/policy_matrix.py`).** Nothing was applied or run against a cluster by the author; the CEL ran only through an offline model (`tests/celmodel.py`). Unverified: that the `kubernetes_manifest` provider path accepts the `admissionregistration.k8s.io/v1` kinds (plan and apply); that a real API server accepts and evaluates the CEL the way the model does (the type check against the pod schema, the cost estimate and the exact function semantics are the API server's): only the admission matrix against a real API server (a kind cluster, or the recorded run on the mirror) shows it; differences of CEL semantics and of the pod schema between the kind version and AKS; the fields added to the pod API after the version the deny-lists were written for (a field no validation names is not refused: the mirror's allow-list of pod keys cannot be expressed for a typed object in CEL); that the server's defaulting and mutation (two NoExecute tolerations, `schedulerName`, the empty pod `securityContext`, a defaulted `requests`) is exactly what `tests/test_policy_cel.py` emulates; that ephemeral containers are refused on create before the policy runs; the ordering of the parameter ConfigMap update during an automatic image promotion (a bounded window in which the launcher's pods are denied is possible); the data-action names and the plan role's coverage of the two admission kinds (see "Custom role").

40. **The launcher's Role and the manager pod (`worker_rbac.tf`, `pod_backend.tf`, the additions in `k8s.tf` and `aks.tf`).** Nothing was applied, planned against Azure or run against a cluster; the checks that ran are the offline ones above and an evaluation of the new locals, off and on, in a provider-free scratch module with `terraform console` (an evaluation of expressions, not a plan). Unverified: that a real plan shows no line at all with the defaults, in particular none for the Deployment and the pool `factory` (a `merge` or `concat` that adds nothing should leave the planned manifest equal to the state; the plan decides); that the provider accepts the typed Role and RoleBinding with the empty core `api_groups` and that the plan identity can read them (the custom role is unchanged); that the kubelet starts the pod: the projected token and the CA readable by uid 10003, the owners and modes of the two `emptyDir`s for containers of different uids under `hostUsers: false`, the init container with `CHOWN`, `FOWNER` and `FSETID` only, a read-only mount of the socket directory that still lets the main container connect, what `SO_PEERCRED` reports across containers; that the workload identity webhook honours `skip-containers` for the init container and both sidecars (the relay refuses to start with an `AZURE_*` name); that a worker pod reaches the relay through `factory-relay-svc` and cannot reach the manager address under Cilium; that `azurerm_kubernetes_cluster.this.fqdn` is the public FQDN the canary needs; the sidecar sizes; and, for `manager_pool_taint_enabled`, whether `node_taints` replaces the pool and whether the Secrets Store CSI and Azure Disk CSI drivers tolerate the taint on this cluster.


## Sources

Fetched on 2026-11-06 (raw Markdown of the Microsoft Learn pages comes from https://github.com/MicrosoftDocs/azure-aks-docs and
https://github.com/MicrosoftDocs/azure-docs, `articles/...`).

- azurerm 4.81.0 documentation: `https://raw.githubusercontent.com/hashicorp/terraform-provider-azurerm/v4.81.0/website/docs/` (resources
  `r/*.html.markdown`, `index.html.markdown`, `guides/features-block.html.markdown`).
- hashicorp/kubernetes 3.5.0: the provider schema (`terraform providers schema -json`) for `kubernetes_deployment_v1` (no `hostUsers`,
  `procMount` or `appArmorProfile`), `kubernetes_manifest`, `kubernetes_persistent_volume_claim_v1`, `kubernetes_service_v1`.
- azapi 2.13.0 documentation: `https://raw.githubusercontent.com/Azure/terraform-provider-azapi/v2.13.0/docs/`.
- AKS (`azure-aks-docs`, `articles/aks/`): `csi-secrets-store-driver.md`, `csi-secrets-store-identity-access.md`,
  `workload-identity-deploy-cluster.md`, `static-ip.md`, `concepts-network.md`, `concepts-network-azure-cni-overlay.md`,
  `nat-gateway.md`, `outbound-rules-control-egress.md`, `manage-azure-rbac.md`, `api-server-authorized-ip-ranges.md`,
  `kubelogin-authentication.md`.
- Kubernetes: user namespaces (`hostUsers`), Pod Security Standards (baseline and restricted, safe sysctls).
- Azure Monitor: `containers/kubernetes-data-collection-configmap.md`, `logs/data-retention-configure.md`.
- Caddy documentation: Caddyfile global options (`acme_ca`, `admin`, `servers`), `request_header`, `reverse_proxy`, `redir`, `handle`.
- Monitoring: Prometheus configuration (`scrape_configs`, `authorization.credentials_file`, `kubernetes_sd_configs` role `node`, rule files), Alertmanager configuration
  (`slack_configs.api_url_file`, `msteamsv2_configs.webhook_url_file`, `webhook_configs.url_file`), Grafana configuration (`server.serve_from_sub_path`, `security`, `auth.generic_oauth`,
  `__file` secrets), the Docker Hub registry API for the image tags and digests.
- Microsoft.OperationalInsights workspaces, stable 2023-09-01: `.../specification/operationalinsights/resource-manager/Microsoft.OperationalInsights/OperationalInsights/stable/2023-09-01/Workspaces.json`.
- Azure Storage: `articles/storage/common/storage-network-security.md`.
- Terraform azurerm backend arguments: `https://raw.githubusercontent.com/hashicorp/terraform/v1.13.5/internal/backend/remote-state/azure/backend.go`.
- Tools: `https://releases.hashicorp.com/terraform/1.13.5/terraform_1.13.5_SHA256SUMS`,
  `https://github.com/terraform-linters/tflint/releases/tag/v0.65.0` (`checksums.txt`),
  `https://github.com/Azure/kubelogin/releases/tag/v0.2.22` (`kubelogin-linux-amd64.zip.sha256`).
- GitHub OIDC subject formats: `https://raw.githubusercontent.com/github/docs/main/content/actions/reference/security/oidc.md`.
- Repository: `spikes/S1.md`, `S2.md`, `S3.md`, `docs/manager.md`, `docs/operations.md`;
  `src/factory/config.py`, `locks.py`, `github_auth.py`, `entra_jwt.py`, `manager.py`.

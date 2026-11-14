# Factory infrastructure (Terraform)

An **optional reference deployment** of the Factory manager on **Azure Kubernetes Service (AKS)**: a small system pool and a one-node
`factory` pool that runs exactly one manager pod, a VNet with a NAT gateway (one fixed egress address), run data on an Azure Disk,
per-repository locks as blob leases, secrets in Key Vault read through the Secrets Store CSI driver, a public HTTPS ingress (Caddy,
Let's Encrypt) and Microsoft Entra authentication that **the manager itself performs** (bearer tokens; there is no Easy Auth on AKS).
Prometheus, Grafana and Alertmanager, Cilium, node auto-provisioning and the pod backend are optional and **off by default**.

This repository ships **no deployment pipeline** (`.github/workflows/ci.yml` runs the tests and deploys nothing): run `terraform plan` and
`terraform apply` by hand or from your own pipeline and approval process. `INFRA_TFVARS` below is the name this document uses for your tfvars
text, for example a CI variable your pipeline passes to `terraform` with `-var-file`; `<<PLACEHOLDER>>` marks a value you replace. Providers:
`azurerm ~> 4.81`, `azapi ~> 2.13` (table retention only) and `kubernetes ~> 3.5`; Terraform `>= 1.9, < 2.0` (builds pinned in the committed
`.terraform.lock.hcl`, `linux_amd64` hash only). Whoever plans or applies needs the Azure CLI (logged in) and **kubelogin** on the `PATH`: the
kubernetes provider gets its token from `kubelogin get-token --login azurecli` (`providers.tf`).

## What it creates

| Resource | File | Notes |
|---|---|---|
| VNet, node subnet (`/26`, service endpoints), NAT gateway with a static public IP, NSG; Log Analytics workspace (30 days) with 4 days on `ContainerLogV2`, `KubeEvents`, `KubePodInventory`, `AZKVAuditLogs` (`log_retention_days`, set through `azapi`), Container Insights rule | `network.tf`, `observability.tf` | The resource group is looked up, not created |
| AKS cluster (Azure Linux 3, local accounts off, Entra ID + Azure RBAC, OIDC issuer, workload identity, CNI Overlay, Key Vault CSI add-on with rotation off); system pool 1 x `Standard_D2ds_v5`; pool `factory` 1 x `Standard_D8ds_v5` (`pool=factory`, no autoscaler); "Cluster Admin" and "Cluster User Role" for each id in `aks_cluster_admin_principal_ids` | `aks.tf` | `prevent_destroy` on the cluster. The admin list is required, non-empty, includes the apply identity, and the caller is never added implicitly, so a plan and an apply see the same addresses |
| Lock storage account (containers `locks`, `logs-archive`); Key Vault (RBAC, purge protection, default-deny) | `storage.tf`, `keyvault.tf` | No account keys; the vault holds secret NAMES only, no value is in Terraform |
| Identity `workload` federated to `system:serviceaccount:factory:factory` (Key Vault Secrets User; Blob Data Contributor on `locks` and `logs-archive` only), control-plane identity `cluster`, AcrPull on `acr_name` for the kubelet | `identity.tf` | |
| Namespace `factory` (Pod Security `privileged`: the pod needs the relaxations below), StorageClass `factory-disk` (Premium, `Retain`) and PVC `factory-data`, SecretProviderClass, immutable ConfigMap `factory-repos-<hash>`, Deployment `factory` (1 replica, `Recreate`), Service | `k8s.tf` | The disk lives in the node resource group |
| Static ingress IP with a DNS label; namespace `ingress` (Pod Security `restricted`), Caddy, PVC `caddy-data`, LoadBalancer Service | `ingress.tf` | Caddy runs on the system node |
| **Only with `monitoring_enabled`:** namespace `monitoring`, Prometheus, Grafana, kube-state-metrics, optional Alertmanager, identities | `monitoring.tf`, `monitoring/` | "Monitoring" |
| **Optional, off:** Cilium, node auto-provisioning, worker NodePool, pod backend | `aks.tf`, `worker_*.tf`, `pod_backend.tf`, `policy/` | "Optional features" |

Outputs (`outputs.tf`): `nat_public_ip`, `ingress_public_ip`, `manager_url`, `aks_cluster_name`, `aks_get_credentials_command`,
`aks_oidc_issuer_url`, `identity_principal_id`, `identity_client_id`, `lock_container_url`, `logs_archive_container_url`, `key_vault_uri`,
`grafana_url`, `ingress_hosts`, `custom_host_dns_target`, `dashboard_url`, `dashboard_spa_redirect_uris`, `grafana_oauth_redirect_uris`,
`grafana_oauth_redirect_uri`.

**Why AKS, not Container Apps.** The workers' bubblewrap sandbox did not start on Azure Container Apps. It starts on an Azure Linux 3 node
when the Factory pod sets `hostUsers: false`, `procMount: Unmasked` and an `Unconfined` AppArmor profile together (smaller sets failed; two
subsets were not tried). A `hostUsers: false` pod cannot mount NFS, so `/data` is an Azure Disk (ReadWriteOnce): one replica, `Recreate`.

**Why two objects are `kubernetes_manifest`.** The typed Deployment of the provider (3.5) has no `hostUsers`, `procMount` or
`appArmorProfile`, and the SecretProviderClass is a custom resource. `kubernetes_manifest` reads the API server at **plan** time, hence the
two-stage first apply, and every later plan needs a reachable API server.

## What the pod gets

`locals.tf` (`app_env`) sets `FACTORY_CONFIG=/config` (`/config/repos/<org>__<name>.toml`, one ConfigMap file per `repo_configs` entry),
`FACTORY_DATA=/data`, port 8080, `FACTORY_MANAGER_AUTH=entra-jwt` with the tenant and `FACTORY_ENTRA_AUDIENCES`, the lock and log-archive
account settings and the GitHub App ids. `ANTHROPIC_API_KEY` and `GITHUB_APP_PRIVATE_KEY` are `secretKeyRef`s into the Secret
`factory-keyvault`, synced by the CSI driver from the pinned versions; **the Key Vault volume is mounted only by an init container**
(`keyvault-sync`), because the driver publishes a volume only when some container mounts it, and the Factory container never sees the files.
Probes are `exec` probes against `127.0.0.1:8080/healthz`; the pod mounts no Kubernetes API token. The Factory container's security context
is exactly `procMount: Unmasked` and `appArmorProfile: Unconfined` with `hostUsers: false` on the pod, and nothing else (no `privileged`, no
capabilities, no `runAsUser`, no `readOnlyRootFilesystem`, no seccomp field); `infra/test/test_workload_design.py` fails if this changes.

## Network design

- **Egress.** Everything leaves through the NAT gateway (`nat_public_ip`): publish it to the GitHub organization IP allow list and, if the
  registry restricts access, its firewall. There is no Azure Firewall; domain filtering is the Claude Code sandbox allowlist.
- **NSG** (AKS does not edit an NSG on a subnet you provide). Inbound: TCP 80 and 443 from `ingress_allowed_cidrs` (the internet if
  empty) and the overlay network. Outbound: TCP 443, the Azure platform address `168.63.129.16` (53, 80, 32526), the metadata address and
  the overlay network, then **deny every other port to the internet**. If node pool creation times out on the first apply, relax
  `deny-other-internet-outbound` in `network.tf` first.
- **Ingress.** Caddy (image pinned by digest) behind a Standard load balancer on the static IP,
  `https://<ingress_dns_label>.<location>.cloudapp.azure.com`, with a Let's Encrypt certificate (HTTP-01 on 80, TLS-ALPN-01 on 443) that Caddy keeps on the 4 GiB PVC `caddy-data`. It
  strips the inbound Easy Auth headers (`X-MS-CLIENT-PRINCIPAL*`) and forwards `Authorization` unchanged to the manager. With monitoring on,
  `/grafana/*` goes to Grafana; the metrics port and Prometheus have no route.
- **Authentication.** Caddy authenticates nobody. The manager requires `Authorization: Bearer <Entra access token>` on every path
  (`/healthz` for non-loopback peers too; the only exception is the dashboard page's three static files under `/ui/`), verifies signature,
  issuer, audience, tenant and expiry, and maps the `roles` claim to `Factory.Operator` and `Factory.Viewer` (`docs/manager.md`).
  `ingress_allowed_cidrs` is extra hardening, and **a restricted list blocks Let's Encrypt** (it validates from unpublished addresses).
- **Storage and Key Vault** are default-deny and admit the node subnet through service endpoints. The vault has **no trusted-service
  bypass** (the storage account keeps its Azure-services bypass), and the storage account has **no account keys**: only Entra identities
  use it. A plan run from outside the VNet may get a 403 from the vault: allow the runner in `key_vault_ip_rules`.
- **Cluster API.** Public, protected by Entra ID and Azure RBAC; local accounts and `az aks command invoke` are off. Set
  `aks_api_authorized_ip_ranges` to restrict it (the NAT address is added for you); a hosted CI runner has no stable address, so a non-empty
  list needs a runner with a known one. A private cluster is an operator decision that needs a runner inside the VNet.
- **One replica, one node.** The `factory` pool has one node and no autoscaler, the disk is ReadWriteOnce and the blob lease is the second
  line. `terminationGracePeriodSeconds` is 600 by default: a rollout or node drain waits that long, so **drain every repository first**.

## Prerequisites

- **Tools:** `terraform`, `az` (logged in), `kubectl`, `kubelogin`, `docker`, `python3` (the `tools/` scripts and one check below), `curl` and
  `openssl` (used below); optional `tflint`, `jq` (the guard recipe) and `gh` (the bot user ID below).
- **Azure:** a subscription, an existing resource group and a container registry. Check your vCPU quotas
  (`az vm list-usage --location <location>`): the default pools are 2 + 8 vCPUs of the `Dds_v5` family (plus 2 with the optional `mon`
  pool), and an upgrade surges one more node of each pool, so the family and regional limits must cover the pools plus one extra node each.
- **A GitHub App** (the Factory App) owned by your organization and installed on each repository you enroll: its numeric ID, slug and bot
  user ID (`github_app_id`, `github_app_slug`, `github_app_user_id`), the installation ID per repository (in its `repo_configs` entry) and a
  private key (step 8). Its repository permissions are exactly those `tools/token_broker.py` requests: `actions: read`,
  `contents: write`, `pull_requests: write`, `issues: write`, `statuses: write` (commit statuses), `checks: read` and `metadata: read`.
  GitHub refuses a token request for a permission the installation has not accepted, so accept them on each installation before the first run
  (`docs/operations.md`, "Roll-out order matters"; `docs/handoff.md`). **Never give it `workflows`** (`docs/handoff.md`, "Why
  `pull_request_target`, and what really protects the key"). The bot user ID is the `id` of the account `<slug>[bot]`:
  `gh api 'users/<<GITHUB_APP_SLUG>>[bot]' --jq .id`.
- **An Anthropic API key** (step 8) and **the image in your registry**. The repository ships a Dockerfile and no image workflow; build,
  smoke-test and push it yourself and keep the digest `docker push` prints (`image/README.md`, "Build and check"):
  ```sh
  docker build -t <<ACR_NAME>>.azurecr.io/<<IMAGE_REPOSITORY>>:dev .     # BuildKit, linux/amd64 only
  bash image/smoke.sh <<ACR_NAME>>.azurecr.io/<<IMAGE_REPOSITORY>>:dev
  az acr login --name <<ACR_NAME>> && docker push <<ACR_NAME>>.azurecr.io/<<IMAGE_REPOSITORY>>:dev    # prints sha256:<digest>
  ```
  The tag is only a transport name: Terraform accepts a digest, never a tag, and `image_repository` must equal the repository you push to.

## Variables

Prepare your inputs **before the first `terraform` command** (step 7 of "Bootstrap" is the first). `variables.tf` has every description and
validation; `terraform.tfvars.example` has `<<PLACEHOLDER>>` values for every input without a default and, commented out, the common
optional ones.

1. **Your tfvars file.** Copy the example to a name that `infra/.gitignore` covers (`*.tfvars`; it is not auto-loaded) and replace every
   `<<PLACEHOLDER>>`: `cp infra/terraform.tfvars.example infra/prod.tfvars`. `<<YOUR_TFVARS>>` below is that file, written `prod.tfvars`
   (the commands run `terraform -chdir=infra`, so a relative path is relative to `infra/`). Leave the two
   `*_secret_version` values as placeholders until step 8 has uploaded the secrets: step 7 overrides them on the command line (a `-var` beats
   the file).
2. **`image_digest`** is not in that file. It is the one value of the committed `infra/image.auto.tfvars`, a placeholder that `variables.tf`
   rejects (`sha256:` plus 64 lowercase hex characters). Put the digest of the image you pushed (Prerequisites) there ("Promote an image
   digest"); until you do, every plan and apply fails, stage 0 included.
3. **`ingress_custom_host`.** `infra/ingress.auto.tfvars` holds the placeholder host `factory.example.org`. Replace it with a name you
   control or delete the file before a real deploy (or set `ingress_custom_host = null` in your tfvars; "Browser sign-in and custom host
   name"): left alone, Caddy asks Let's Encrypt for a certificate for a name that is not yours.
4. **The vault inputs of stage 0.** Put `key_vault_secrets_officer_principal_ids` (your object ID: step 7 creates that `Key Vault Secrets
   Officer` assignment, which you need to upload the secrets) and `key_vault_ip_rules` (your public IP: the vault is created default-deny with
   these rules) in your tfvars **before** step 7, not after it. Remove `key_vault_ip_rules` again once step 8 is done.
5. **`caddy_image_digest`.** Pin it to the multi-architecture index digest of the official Caddy 2 image
   (`docker buildx imagetools inspect caddy:2 --format '{{json .Manifest.Digest}}'`).

**Inputs without a default** (the example and `image.auto.tfvars` together assign exactly these; `check_refs.py` enforces it):
`subscription_id`, `tenant_id`, `location`, `resource_group_name`, `name_prefix`, `tags` (`{}` for none), `acr_name`,
`acr_resource_group_name`, `image_repository`, `image_digest`, `key_vault_name`, `anthropic_api_key_secret_version`,
`github_app_private_key_secret_version`, `entra_allowed_audiences`, `github_app_id`, `github_app_slug`, `github_app_user_id`,
`ingress_dns_label`, `caddy_image_digest`, `aks_cluster_admin_principal_ids` and `repo_configs`.

**Everything else is optional**: sizing and network (the other `aks_*` variables: `aks_sku_tier`, `aks_kubernetes_version`,
`aks_node_os_upgrade_channel`, `aks_system_pool_vm_size`, `aks_user_pool_vm_size`, `aks_pod_cidr`, `aks_service_cidr`; `container_cpu` and
`container_memory` at 6 vCPU and 24Gi, at most 75 % of the node; `data_disk_size_gib`; `vnet_address_space`, `node_subnet_prefix`; retention
days), access and ingress extras (`key_vault_ip_rules`, `key_vault_secrets_officer_principal_ids`, `token_broker_command`,
`caddy_image_repository`, `acme_email`, `ingress_allowed_cidrs`, `aks_api_authorized_ip_ranges`, `ingress_custom_host`, `entra_spa_client_id`,
`entra_spa_scope`) and the feature groups under "Monitoring" and "Optional features", all off. The example does not list
`manager_pool_taint_enabled`, `logs_archive_retention_days` or the four monitoring image variables (`prometheus_image`, `grafana_image`,
`alertmanager_image`, `kube_state_metrics_image`, pinned in `monitoring-images.auto.tfvars`; "Monitoring", "Images"): `variables.tf` describes
them.

## Bootstrap

All commands are for the operator to run; this repository runs none of them. Prepare your inputs first ("Variables"): step 7 is the first
`terraform` command and needs a complete tfvars file and a real `image_digest`.

1. **Resource providers.** The provider has `resource_provider_registrations = "none"`, so the deploying identity needs no
   subscription-wide rights and nothing registers a provider at apply time:
   ```sh
   for ns in Microsoft.ContainerService Microsoft.Compute Microsoft.Network Microsoft.Storage Microsoft.KeyVault \
             Microsoft.ManagedIdentity Microsoft.ContainerRegistry Microsoft.OperationalInsights \
             Microsoft.OperationsManagement Microsoft.Insights; do
     az provider register --namespace "$ns" --wait
   done
   ```
2. **Resource group and registry.** Create the resource group `resource_group_name` in `location` (Terraform looks it up and checks the
   region). The registry (`acr_name`) is yours: push the first image (Prerequisites) and put the digest `docker push` printed into
   `infra/image.auto.tfvars` ("Variables", item 2).
3. **State storage** (a separate resource group, not managed here):
   ```sh
   az storage account create --name <<TFSTATE_STORAGE_ACCOUNT>> --resource-group <<TFSTATE_RESOURCE_GROUP>> \
     --location <<AZ_LOCATION>> --sku Standard_LRS --min-tls-version TLS1_2 \
     --allow-blob-public-access false --allow-shared-key-access false
   az storage account blob-service-properties update --account-name <<TFSTATE_STORAGE_ACCOUNT>> \
     --resource-group <<TFSTATE_RESOURCE_GROUP>> --enable-versioning true \
     --enable-delete-retention true --delete-retention-days 14
   az storage container create --name <<TFSTATE_CONTAINER>> --account-name <<TFSTATE_STORAGE_ACCOUNT>> --auth-mode login
   terraform -chdir=infra init \
     -backend-config="resource_group_name=<<TFSTATE_RESOURCE_GROUP>>" \
     -backend-config="storage_account_name=<<TFSTATE_STORAGE_ACCOUNT>>" \
     -backend-config="container_name=<<TFSTATE_CONTAINER>>" \
     -backend-config="key=<<TFSTATE_KEY>>" \
     -backend-config="use_azuread_auth=true"
   ```
   The backend block in `versions.tf` is empty on purpose; a pipeline can pass the `-backend-config` values from its own variables.
4. **Identities for plans and applies.** `providers.tf` pins no authentication method: locally it is `az login`; from a pipeline a login
   step (for example `azure/login` with a federated credential) signs in and Terraform and kubelogin use that session. If you plan on pull
   requests, use **two** identities with federated credentials (issuer `https://token.actions.githubusercontent.com`, audience
   `api://AzureADTokenExchange` for GitHub Actions; other CI systems have their own; use the subject form your tokens carry):

   | Identity | Federated credential subject(s) | Roles |
   |---|---|---|
   | **Plan** (read-only) | `repo:<<ORG>>/<<REPO>>:pull_request`, `repo:<<ORG>>/<<REPO>>:ref:refs/heads/<<DEFAULT_BRANCH>>` | `Reader` on the resource group and the registry, `Storage Blob Data Reader` on the state container, **`Azure Kubernetes Service Cluster User Role`, `Azure Kubernetes Service RBAC Reader` and the custom role `Factory Plan Cluster Reader` on the resource group** (none can change anything; the custom role is created from `infra/roles/factory-plan-cluster-reader.json`, see below) |
   | **Apply** | `repo:<<ORG>>/<<REPO>>:environment:production` | `Contributor` and `Role Based Access Control Administrator` (or `User Access Administrator`) on the resource group, the ability to create the `AcrPull` assignment on the registry, `Storage Blob Data Contributor` on the state container. Its **object ID must be listed in `aks_cluster_admin_principal_ids`** (the plan identity must not be: that variable is the only source of the cluster role assignments, so the preview plan and the apply plan have the same action list); Terraform then grants it the cluster admin role it needs to create the Kubernetes objects |

   A pipeline that pushes images uses a third identity: `AcrPush` on the registry and nothing else. A pull request runs **its own copy** of
   your pipeline, so whatever the plan identity can do, any author with write access can do: it has read-only roles and no write role on
   the state (plans run with `-lock=false`).

   **Custom role `Factory Plan Cluster Reader`.** The built-in RBAC Reader does not cover the cluster-scoped reads of the kubernetes
   provider (CRDs, storage classes, custom resources, cluster roles), and without them every plan fails with
   `customresourcedefinitions ... is forbidden`. `infra/roles/factory-plan-cluster-reader.json` defines exactly seven data actions and no
   `Actions` or write data actions:
   `Microsoft.ContainerService/managedClusters/apiextensions.k8s.io/customresourcedefinitions/read`,
   `Microsoft.ContainerService/managedClusters/storage.k8s.io/storageclasses/read`,
   `Microsoft.ContainerService/managedClusters/customresources/read`,
   `Microsoft.ContainerService/managedClusters/rbac.authorization.k8s.io/clusterroles/read`,
   `Microsoft.ContainerService/managedClusters/rbac.authorization.k8s.io/clusterrolebindings/read`,
   `Microsoft.ContainerService/managedClusters/rbac.authorization.k8s.io/roles/read` and
   `Microsoft.ContainerService/managedClusters/rbac.authorization.k8s.io/rolebindings/read` (the last two serve the pod backend's worker
   namespace). Create it once (you need `Owner` or `User Access Administrator` on the resource group), then assign it to the plan identity:

   ```sh
   SUB=<<SUBSCRIPTION_ID>>; RG=<<RESOURCE_GROUP>>   # the subscription and the value of resource_group_name
   ROLE_FILE=$(mktemp)
   sed -e "s|<<SUBSCRIPTION_ID>>|$SUB|" -e "s|<<RESOURCE_GROUP>>|$RG|" \
     infra/roles/factory-plan-cluster-reader.json > "$ROLE_FILE"
   az role definition create --role-definition "$ROLE_FILE"
   az role assignment create --assignee-object-id <<PLAN_PRINCIPAL_ID>> --assignee-principal-type ServicePrincipal \
     --role "Factory Plan Cluster Reader" --scope "/subscriptions/$SUB/resourceGroups/$RG"
   ```

   Terraform does not manage the role (the plan identity must not be able to grant itself anything); when the JSON gains a data action, run
   `az role definition update --role-definition "$ROLE_FILE"` with a fresh copy and re-run the failed plan a few minutes later.
   `customresources/read` cannot be scoped per CRD: it reads every custom resource in the cluster. It exposes no secret values: Kubernetes
   `Secret` objects are not custom resources and are covered by no data action of this role. With the pod backend on, the first plan also reads
   `validatingadmissionpolicies` and their bindings, which have no documented data action and so are not in the role: if a plan run as the
   plan identity fails with Forbidden on them, do not widen the role by guessing a name.
5. **Pipeline settings (only if you run Terraform from CI).** Keep your tfvars text (the inputs of `terraform.tfvars.example` **except**
   `image_digest`) in a CI variable `INFRA_TFVARS` that your pipeline passes with `-var-file`; none of it is secret (`scripts/register-repo.sh`
   edits the `repo_configs` map of a variable of that name: `docs/enrolling-repositories.md`). For a GitHub environment `production` that
   guards the apply job set deployment branches to the default branch only, required reviewers (with "Prevent self-review" when there are two
   or more) and "Allow administrators to bypass" off: these are the real controls, an `if` in the pipeline file is not. If you run
   `tools/infra_cluster_guard.py` on pull requests, create the label `infra-cluster-change` (any colour).
6. **Entra app registration for the API** (the audience of the bearer tokens; Terraform does not create it):
   - A single-tenant registration with Application ID URI `api://<<ENTRA_APP_CLIENT_ID>>`, issuing **v2 tokens**:
     `az ad app update --id <<ENTRA_APP_CLIENT_ID>> --set api.requestedAccessTokenVersion=2`. The manager accepts only the issuer
     `https://login.microsoftonline.com/<tenant>/v2.0`; a v1 token (issuer `sts.windows.net`) is refused.
   - Two app roles (generate a fresh GUID for each `id`):
     ```json
     [
       {"allowedMemberTypes": ["User", "Application"], "displayName": "Factory Operator", "description": "Run and change Factory runs",
        "id": "<<ROLE_ID_OPERATOR>>", "isEnabled": true, "value": "Factory.Operator"},
       {"allowedMemberTypes": ["User", "Application"], "displayName": "Factory Viewer", "description": "Read Factory status",
        "id": "<<ROLE_ID_VIEWER>>", "isEnabled": true, "value": "Factory.Viewer"}
     ]
     ```
   - On the enterprise application turn on **"Assignment required"** and assign people, groups or workload identities to the roles.
     Without it any user of the tenant can request a token; with it, and the manager's role check (a token without a role gets 403),
     access is limited.
   - `entra_allowed_audiences = ["api://<<ENTRA_APP_CLIENT_ID>>", "<<ENTRA_APP_CLIENT_ID>>"]`: a v2 token's `aud` is the client ID,
     whichever form the client asked for.
7. **Stage 0: create only the vault.** The two secret versions are required variables and the secrets cannot be uploaded before the vault
   exists, so create just the vault with syntactically valid dummy versions:
   ```sh
   terraform -chdir=infra apply -var-file=<<YOUR_TFVARS>> \
     -var=anthropic_api_key_secret_version=00000000000000000000000000000000 \
     -var=github_app_private_key_secret_version=00000000000000000000000000000000 \
     -target=azurerm_key_vault.this -target='azurerm_role_assignment.key_vault_secrets_officer'
   ```
   Your tfvars must already hold `key_vault_secrets_officer_principal_ids` (your object ID, for `Key Vault Secrets Officer`) and
   `key_vault_ip_rules` (your public IP) when this runs ("Variables", item 4): this apply creates the vault with that firewall and the
   role assignment, and the vault is default-deny, so without them you cannot upload.
8. **Upload the two secrets** (names are fixed) from files in a private directory, not command-line values; delete the files
   afterwards. `--query id -o tsv` prints only the new secret's ID, never its value (without it `secret set` prints the value):
   ```sh
   az keyvault secret set --vault-name <<KEY_VAULT_NAME>> --name github-app-private-key --file <<PRIVATE_KEY_PEM_PATH>> --query id -o tsv
   az keyvault secret set --vault-name <<KEY_VAULT_NAME>> --name anthropic-api-key --file <<API_KEY_FILE_PATH>> --query id -o tsv
   ```
   Two IDs are printed, one per secret, each ending in the 32-character **version**: the last path segment of the `github-app-private-key`
   ID is `github_app_private_key_secret_version` and that of the `anthropic-api-key` ID is `anthropic_api_key_secret_version`. Put both in
   your tfvars, then remove the temporary `key_vault_ip_rules`.

## First apply

The first apply has **two stages**, because `kubernetes_manifest` needs a reachable API server (and the Secrets Store CRD) when Terraform
plans and the cluster does not exist yet; until it does, a plan of everything fails with a kubernetes provider error about the unknown
host. The names `stage=cluster` and `stage=all` are used below and by `tools/infra_cluster_guard.py` (`GUARD_STAGE`): `cluster` is the
four `-target` lines here, `all` is a plain apply of everything.

1. **Stage `cluster`** (10-20 minutes), after `az login` as the apply identity or an operator in `aks_cluster_admin_principal_ids`:
   ```sh
   terraform -chdir=infra apply -var-file=<<YOUR_TFVARS>> \
     -target=azurerm_kubernetes_cluster.this -target=azurerm_kubernetes_cluster_node_pool.factory \
     -target=azurerm_role_assignment.aks_cluster_admin -target=azurerm_role_assignment.aks_cluster_user
   ```
2. **Stage `all`** (no `-target`). Wait a few minutes after stage 1: role assignments take time to propagate and the CSI add-on installs
   its CRD asynchronously; on `Forbidden`/`Unauthorized` or `no matches for kind "SecretProviderClass"`, wait and apply again. The
   Terraform resource does not wait for the rollout, so follow every apply with `kubectl -n factory rollout status deploy/factory` (allow
   up to 25 minutes the first time: image pull, disk attach, the entrypoint's `chown` of `/data`); a pod that cannot start fails there.
3. **After the apply.** Add `nat_public_ip` to the GitHub organization's IP allow list. `curl -sS -o /dev/null -w '%{http_code}\n'
   https://<manager host>/healthz` must print `401` and `http://<manager host>/` must redirect to https (host: output `manager_url`; the
   certificate arrives a minute or two after Caddy starts, `kubectl -n ingress logs deploy/caddy`). Call the API ("Call the API"):
   `GET /repos` with a `Factory.Viewer` principal returns 200. Run `kubectl -n factory exec deploy/factory -- factory-sandbox-check`
   once: it must print `all expectations held`. `kubectl` credentials: the output `aks_get_credentials_command`.

## Call the API

```sh
TOKEN=$(az account get-access-token --resource api://<<ENTRA_APP_CLIENT_ID>> --query accessToken -o tsv)
curl -sS -H "Authorization: Bearer $TOKEN" "$(terraform -chdir=infra output -raw manager_url)/repos"
```

The token holds the caller's app roles in `roles`; treat it as a password. If Entra issues it but the manager answers 401, the usual causes
are a v1 token (`accessTokenAcceptedVersion` not 2), an audience not in `entra_allowed_audiences`, or another tenant (the same 401 on
purpose). **Consent for people:** a **person's** `az account get-access-token --resource api://...` is a request by the first-party Azure CLI
application and can fail with `AADSTS65001`. With your tenant administrator, expose a scope (for example `access_as_user`) and pre-authorize the
CLI's client ID (printed in the error), or grant tenant-wide admin consent for the CLI; or use a workload identity for scripts (a service
principal or managed identity with the **Application** app role needs no consent; prefer a federated credential to a client secret).

## Promote an image digest

A release is **one changed value**; nothing else in Terraform is edited for it.

1. Build, smoke-test and push the image (Prerequisites); keep the `sha256:` digest.
2. Put it in **`infra/image.auto.tfvars`** (or your `-var-file`: a `-var` or `-var-file` value beats the auto-loaded file), run
   `terraform -chdir=infra plan` and read it: normally only `update-in-place kubernetes_manifest.factory_deployment` (with the pod backend
   also the policy parameter ConfigMap, see "Cluster guard").
3. Drain every repository first ("Safe deployments"), apply, and wait for the rollout. With `Recreate` the old pod stops (after up to
   `termination_grace_period_seconds`) before the new one starts, so the API is unavailable meanwhile. Then read `status`: `interrupted`
   jobs, and `failed` ones with `agent exited -15` after a rollout that was not drained, need `retry` or `resume`.
4. After a successful apply and rollout, commit the digest to `infra/image.auto.tfvars` on the default branch so that git says what is deployed.

A tag is never accepted (`sha256:` plus 64 hex characters is validated); the Caddy image is pinned the same way. From your own pipeline make
the apply deliberate: show the plan (action list and digest) to a reviewer, plan again in the apply job and refuse to apply if it differs from
what was approved, and allow one apply at a time. Do not try to ignore the image in Terraform: `lifecycle { ignore_changes }` inside a
`kubernetes_manifest` is not honoured, and an image changed outside Terraform makes the next apply fail with a field-manager conflict.

## Safe deployments

Applying restarts the Factory pod. A restart in the middle of a build ends the job (`failed` with `agent exited -15` when the supervisor
gets to terminate the worker and record it, `interrupted` when the supervisor is killed first: `docs/operations.md`, "Drain, pause, and
what each leaves behind") and wastes its model spend. So pause, drain and wait **before `terraform apply`**, and resume after the rollout.
`tools/infra_deploy_gate.py` does the steps (an optional script, by hand or from your pipeline; `docs/operations.md`, "Safe deployments:
pause, drain, apply, resume"):

| Step | What it does |
|---|---|
| `infra_deploy_gate.py pause` | `POST /trigger-pause {"paused": true}`: no label trigger, dependency resume or restart auto-resume starts a plan or run while it is set. Runs in progress are untouched |
| `infra_deploy_gate.py drain` | `POST .../drain` for every repository that has a `run` supervisor (from `GET /repos`) |
| `infra_deploy_gate.py wait` | Polls `GET /repos` every 15 s for **30 minutes at most** and drains a supervisor that appears meanwhile; with the pod backend it also holds while a worker pod runs or cannot be read. On timeout it exits 1 **before `terraform apply`**, names the active runs and has changed nothing |
| `terraform apply`, then `kubectl -n factory rollout status deploy/factory` | As usual |
| `infra_deploy_gate.py resume` | Clears the pause (retried for 5 minutes), then `POST .../run` for each run it drained. Run it after the rollout and also when an earlier step failed; if it cannot clear the pause it exits 1 and says the pause is **STILL SET** |

```sh
export FACTORY_MANAGER_URL=$(terraform -chdir=infra output -raw manager_url)
export FACTORY_MANAGER_AUDIENCE=api://<<ENTRA_APP_CLIENT_ID>>     # a value of entra_allowed_audiences
python3 tools/infra_deploy_gate.py pause \
  && python3 tools/infra_deploy_gate.py drain \
  && python3 tools/infra_deploy_gate.py wait \
  && terraform -chdir=infra apply -var-file=<<YOUR_TFVARS>> \
  && kubectl -n factory rollout status deploy/factory
python3 tools/infra_deploy_gate.py resume      # on its own line: run it after a failure above too
```

The `&&` chain is deliberate: pasted as separate lines, a `wait` that times out (exit 1) would still be followed by `terraform apply`, which
restarts the pod under the running builds. With the chain, a failed `pause`, `drain`, `wait`, apply or rollout stops everything after it;
`resume` is the one step you run in every case, because it clears the pause.

It needs a logged-in `az` session whose identity holds the **`Factory.Operator`** app role on the manager registration (a workload identity with
the **Application** role needs no secret and no consent; without it, drain by hand) and a machine that reaches `manager_url`
(`ingress_allowed_cidrs` blocks runners without a known address); `GITHUB_STEP_SUMMARY` and `GATE_STATE_FILE` are optional. It never prints the
token. After the restart, `resume` starts `run` again for every run it drained (a drained run is not `interrupted`, so nothing else would
continue it), and whatever the new pod finds `interrupted` is resumed once when the repository has `auto_resume_after_restart = "once"`
(`docs/manager.md`).

**Cluster guard.** If your pipeline applies the whole configuration after an image build behind a single approval, and every input but the
image digest comes from `INFRA_TFVARS`, nothing else keeps a change to the AKS cluster or a node pool off an automatic path.
`tools/infra_cluster_guard.py ACTIONS_FILE [PLAN_JSON]` is that control (an optional script; its vocabulary is GitHub Actions': event
names, `::error::` lines, `GITHUB_STEP_SUMMARY`). Run it right after each `terraform plan`, before an approval is asked and before the pause,
the drain and `terraform apply`. `ACTIONS_FILE` has one `<actions> <address>` line per changing resource; `GUARD_EVENT`
(`pull_request`, `workflow_run` for an automatic run after an image build, `workflow_dispatch` for a manual run), `GUARD_STAGE` (`all` or
`cluster`), `GUARD_SNAPSHOTS_TAKEN` and `GUARD_PR_LABELS` come from the environment (the script's docstring has the rules). By hand:

```sh
PLAN_DIR=$(mktemp -d)       # the plan and its JSON hold sensitive values: keep them out of the repository
terraform -chdir=infra plan -var-file=<<YOUR_TFVARS>> -out="$PLAN_DIR/tfplan"
terraform -chdir=infra show -json "$PLAN_DIR/tfplan" > "$PLAN_DIR/plan.json"
jq -r '.resource_changes[] | select(.mode == "managed" and .change.actions != ["no-op"]) | "\(.change.actions | join(",")) \(.address)"' "$PLAN_DIR/plan.json" > "$PLAN_DIR/actions.txt"
GUARD_EVENT=workflow_dispatch GUARD_STAGE=all python3 tools/infra_cluster_guard.py "$PLAN_DIR/actions.txt"    # exit 0 allowed, 1 refused, 2 cannot decide
rm -rf "$PLAN_DIR"          # delete the plan files when you are done
```

Two classes of address are known, as data in the tool; `tests/test_infra_cluster_guard.py` reads the `.tf` files and fails when a resource
of these classes exists that is not listed:

- `CLUSTER`: `azurerm_kubernetes_cluster.this` and `azurerm_kubernetes_cluster_node_pool.factory`, exactly the two addresses the
  `stage=cluster` targets reach.
- `POOL_OBJECTS`: node pools that stage cannot apply (`azurerm_kubernetes_cluster_node_pool.monitoring`, any index), the NodePool and
  AKSNodeClass objects, the objects of the worker namespace, the admission policies and their parameter, the launcher's Role and
  RoleBinding and the relay Service (the full list is `LISTED` in the tool). Changing or deleting one is a manual disruption that ignores
  `do-not-disrupt`: deleting the NodePool or the namespace ends every worker session on it, and deleting the policy removes the fence
  around a worker pod. A later Kubernetes object of the worker namespace must be listed in the pull request that adds it:
  `tests/test_infra_cluster_guard.py` reads the `.tf` files as text and fails for a Kubernetes resource that names the worker namespace
  and is not listed, whether it names it by `local.worker_ns_count`, by `var.pod_backend_enabled`, by a reference to
  `kubernetes_namespace_v1.worker`, `kubernetes_service_account_v1.worker` or `kubernetes_network_policy_v1.worker_default_deny`, by
  `local.worker_contract.names.worker_namespace`, by the literal `factory-workers`, through a local that holds one of these (in a
  `namespace` argument or a namespace label selector), or by naming another resource that does. One resource is exempted by name,
  `kubernetes_manifest.factory_network_policy`: it names the namespace only as the peer of its relay rule, lives in the manager
  namespace and is not gated by the variable, so turning the variable off updates it in place. The rule reads text, so the following is
  **not detected**: a namespace that is computed from other strings (`format`, an interpolation), read from a `var.` or a data source, or
  decoded from a file (`yamldecode`), in a resource that `pod_backend_enabled` does not gate (the same resource with the gate is detected).

| Run (`GUARD_EVENT`) | `CLUSTER` | `POOL_OBJECTS` |
|---|---|---|
| automatic, after an image build (`workflow_run`) | every action is refused | every action is refused, **except** an update of `kubernetes_config_map_v1.worker_policy_params[0]` that the plan JSON proves changes only `data.image`, from one `<repository>@sha256:<64 hex>` reference to another (the allowed image of the worker admission policy moves with every release) |
| manual, `stage=all` (`workflow_dispatch`) | every action is refused | allowed; the apply must go through the drain gate |
| manual, `stage=cluster` | allowed; an update, delete or replace also needs `cluster_snapshots_taken` (a plain create, the first apply, does not) | allowed; the apply must go through the drain gate |
| pull request (`pull_request`) | refused unless the pull request carries the label `infra-cluster-change` (then a warning) | the same |

For that one exception the pipeline passes `terraform show -json` of the plan as `PLAN_JSON`; the guard reads only that the change of
`worker_policy_params[0]` is exactly `["update"]`, that `data` holds only `image` before and after (each a digest reference) and that
nothing else changes, is unknown or is sensitive; a missing or unreadable plan JSON makes it exit 2 (nothing is applied).
`cluster_snapshots_taken` (`GUARD_SNAPSHOTS_TAKEN=true`) is a human attestation that a snapshot of the data disk and the Caddy disk (and the
Prometheus disk with monitoring) exists; the guard cannot check it. A refusal fails the step with a fixed message and names no value; an
unreadable list or an unknown event or stage also refuses (exit 2). A pull request with the label that is then merged applies nothing: every
automatic run after the merge is refused until a manual `stage=cluster` apply has applied the change, so apply it promptly.

## Roll back

Set `image_digest` back to the previous digest in `image.auto.tfvars` (or your `-var-file`), drain as in "Safe deployments", and apply: a
new pod runs the old image. **Do not apply from a stale checkout while another apply is still running**: a plan takes the digest
*committed* in `image.auto.tfvars` at that moment, and if your release commits the new digest only after its apply and rollout, a plan made
in that window puts the old image back. Emergency path when you cannot run Terraform (the init container and the Factory container use
the image): `kubectl -n factory set image deploy/factory factory=<registry>/<repo>@sha256:<old> keyvault-sync=<registry>/<repo>@sha256:<old>`,
then put the same digest into `image.auto.tfvars`, or the next apply moves the pod forward again. Rolling back never rolls back run
state on `/data`.

## Rotate secrets and change repository configuration

A pod reads its secrets and repository config when it starts. The pod template carries `checksum/deploy-inputs` (a hash of `repo_configs`
and the secret versions), the repository ConfigMap is immutable and named by a hash of its content, CSI rotation is off and every object
is pinned to a Key Vault **version**, so a change rolls the pod at the next apply.

- **Anthropic key:** upload a new version (step 8: `--query id -o tsv` prints the ID, not the value), drain all repositories, set
  `anthropic_api_key_secret_version`, apply, `run` again.
- **GitHub App key:** add a new key in the App settings, upload it as a new version of `github-app-private-key`, drain, set
  `github_app_private_key_secret_version`, apply, then delete the old App key.
- **Repository configs:** drain every repository, edit `repo_configs`, apply: a new ConfigMap, an updated Deployment, the old ConfigMap
  deleted afterwards (`docs/operations.md` for `abandon_after`, `run_budget_usd`, `auto_resume_after_restart`).

A new repository is one more `repo_configs` entry plus the Factory App on that repository: `scripts/register-repo.sh` (manual-merge mode,
dry run by default) and `scripts/enroll-repo.sh` (hand-off mode) do both (`docs/enrolling-repositories.md`). The entry takes effect only
when you drain, plan, read the plan and apply with the new value; the scripts never do that.

## Retained logs

`factory purge` copies a purged run's transcripts and logs, **redacted** and **gzipped**, to the `logs-archive` container of the lock account
(`<org>__<repo>/<run>/<file>.gz`) before deleting them; copies older than `logs_archive_retention_days` (30) are deleted daily. The `workload`
identity has Blob Data Contributor on that container only. To use a directory on the data disk instead, remove the three
`FACTORY_LOGS_ARCHIVE_*` lines from `app_env` in `locals.tf`; to turn the copy off, set `FACTORY_LOGS_ARCHIVE=off`. Nothing in the manager
serves the copies: read them with *Storage Blob Data Reader* (`az storage blob list --auth-mode login --account-name <account>
--container-name logs-archive --prefix <org>__<repo>/<run>/`, then `az storage blob download ... | zcat`) and grant that role as carefully as
the Operator role, since they can contain private repository content. A failed copy never blocks the purge; it is counted in
`factory_log_archive_failures_total` (`docs/metrics.md`): alert on it.

## Operations notes

- **Upgrades interrupt work.** A Kubernetes version change or a node image update drains the only `factory` node and surges a second one.
  `aks_node_os_upgrade_channel` defaults to `NodeImage`, which reimages weekly: set it to `None` and reimage deliberately, after a drain, then run
  `factory-sandbox-check` again (`docs/operations.md`, "Platform maintenance"). A change that updates or replaces the cluster or the `factory`
  pool is refused by the cluster guard on the automatic path: apply it by hand with `stage=cluster` and `cluster_snapshots_taken=true`.
- **The data disk** lives in the node resource group (`rg-<name_prefix>-aks-nodes`). `Retain` keeps it when the claim is deleted, **but
  deleting the cluster deletes that group and the disk**: snapshot it first. `data_disk_size_gib` can be raised, never lowered; the claim has
  `prevent_destroy`, so a plan that would replace it fails (if the provider plans a replacement for a size increase, `kubectl patch pvc` and
  align the variable).
- **`prevent_destroy` on the cluster** cannot depend on a variable. To tear a stack down on purpose, to re-create the cluster after copying the
  disks, or to delete a test deployment, remove the line in a scratch copy of `infra/` and never commit that. The data volume claim in `k8s.tf`
  has its own, so a destroy needs BOTH removed (deleting the resource group avoids Terraform altogether).
- **Logs.** `kubectl -n factory logs deploy/factory` and Container Insights (4-day retention); its environment collection is off (ConfigMap
  `container-azm-ms-agentconfig` plus `AZMON_COLLECT_ENV=False`): leave both.
- **The pod's `env` list is ordered.** The Deployment is a `kubernetes_manifest`, which fills the plan from the prior state **by list position**,
  so existing positions must never change: sorted plain variables, the Key Vault `secretKeyRef` entries, the metrics host, then **the optional
  plain variables (`app_env_optional` in `locals.tf`) last**. A new optional variable belongs there; inserting into the middle can fail an apply
  after the preview passed (`env[N].valueFrom: Invalid value: "": may not have more than one field specified`). Then delete the Deployment first
  (`kubectl -n factory delete deployment factory`; the Factory is down until the apply recreates it, the disk is untouched) and apply; if your
  pipeline compares the apply's plan with a preview, delete it **before** the preview is made.

## Monitoring

Prometheus and Grafana run **in this cluster**, namespace `monitoring`, as plain Kubernetes manifests (`monitoring.tf`; the scrape config,
alert rules and dashboards are files under `infra/monitoring/`); Alertmanager runs only if you name a receiver. **Everything is off by
default** (`monitoring_enabled = false`): the Factory pod and the Caddyfile are unchanged until you turn it on. It needs three Key Vault
secrets and an Entra client only you can create, and with it on the Factory pod's `SecretProviderClass` gains the metrics token, so a
missing secret version would stop the pod from starting.

| Piece | What it does |
|---|---|
| Manager metrics listener (`docs/metrics.md`) | Binds the **pod address** (never `0.0.0.0`), port `9464`, answers `GET /metrics` only to `Authorization: Bearer <token>`; refuses to start on a non-loopback address without `FACTORY_MANAGER_METRICS_TOKEN`. Reached only through the ClusterIP Service `factory-metrics`, which Caddy does not route |
| Prometheus | Scrapes the manager (with the token) and each node's cAdvisor series; evaluates `monitoring/alert-rules.json`; keeps `prometheus_retention` (15 days) on its own `prometheus_storage_gib` (8) GiB Premium disk (`Retain`) |
| Grafana | Dashboards as code (`monitoring/dashboards/`: Factory overview, Factory resources and manager, Factory node auto-provisioning; not editable), Prometheus as the only data source, state in an `emptyDir` |
| Alertmanager | Only with `alert_receiver != "none"`; the destination URL is a Key Vault secret |
| kube-state-metrics | **one more small pod**: reads pods and nodes (and, with `monitoring_nodeclaim_metrics_enabled`, `NodeClaim` objects) through a read-only ClusterRole (`docs/metrics.md`) |
| Identities | One per component, with `Key Vault Secrets User` **scoped to the one secret it reads** |

Plain manifests instead of `kube-prometheus-stack` avoid a Helm provider and operator CRDs; you own them (an upgrade is a change to
`monitoring-images.auto.tfvars` and, if a setting changed, `monitoring/`). node-exporter is not run (a DaemonSet with host access would force
`privileged`); cAdvisor gives pod memory and CPU against limits.

### Turn it on

1. **Three Key Vault secrets** (names fixed). Upload them and read each version from the printed `id` (last path segment):
   ```sh
   az keyvault secret set --vault-name <<KEY_VAULT_NAME>> --name metrics-bearer-token --value "$(openssl rand -hex 32)" --query id -o tsv
   az keyvault secret set --vault-name <<KEY_VAULT_NAME>> --name grafana-oauth-client-secret --value "<<ENTRA_CLIENT_SECRET>>" --query id -o tsv
   # only with an alert receiver: the receiver's URL
   az keyvault secret set --vault-name <<KEY_VAULT_NAME>> --name alert-receiver-url --value "<<RECEIVER_URL>>" --query id -o tsv
   ```
   The metrics token needs at least 32 printable characters without spaces and is shared by the manager and Prometheus (a new version
   rolls both pods). You need `Key Vault Secrets Officer` and, from outside the VNet, your address in `key_vault_ip_rules`.
2. **Entra.** Grafana signs in with the registration that already protects the API: platform **Web**, redirect URI
   `https://<ingress host>/grafana/login/generic_oauth` (output `grafana_oauth_redirect_uri`), a new **client secret** uploaded as
   `grafana-oauth-client-secret` (the client ID is `grafana_oauth_client_id`), optionally the ID token claim `email`. With **Assignment
   required**, `Factory.Operator` is a Grafana `Editor` (`grafana_operator_role = "Admin"` makes it `Admin`), `Factory.Viewer` a `Viewer`, and
   a person with neither role **cannot sign in** (`role_attribute_strict`). There is no local login form and no local admin user.
3. **Variables:** `monitoring_enabled = true`, `metrics_token_secret_version`, `grafana_oauth_client_id`,
   `grafana_oauth_client_secret_version`; optional `grafana_operator_role`, `alert_receiver`, `alert_receiver_secret_version`,
   `monitoring_node_pool_vm_size`, `monitoring_nodeclaim_metrics_enabled` (needs `nap_mode = "Auto"`), `prometheus_retention`,
   `prometheus_storage_gib`, `prometheus_scrape_interval`, `prometheus_image`, `grafana_image`, `alertmanager_image`,
   `kube_state_metrics_image`, `factory_network_policy_enabled`, `grafana_anonymous_viewer_enabled`. **Drain first**: the Factory pod rolls.

   **The new pod will not start until the synced Secret is recreated.** The driver (rotation off) creates the Secret `factory-keyvault`
   once and never adds a *new key*, so the pod sits in `CreateContainerConfigError: couldn't find key FACTORY_MANAGER_METRICS_TOKEN in
   Secret factory/factory-keyvault`. The Secret is only a copy of Key Vault: delete it once the apply has started the rollout and the
   driver rebuilds it with every key (the same applies to any key added to `secretObjects` later):
   ```sh
   kubectl -n factory delete secret factory-keyvault
   kubectl -n factory delete pod -l app.kubernetes.io/name=factory     # the label is NOT app=factory
   kubectl -n factory get secret factory-keyvault -o json | python3 -c 'import sys,json; print(sorted(json.load(sys.stdin)["data"]))'   # three keys
   ```
4. **Check:** `kubectl -n monitoring get pods`; the Prometheus targets page (`kubectl -n monitoring port-forward svc/prometheus 9090`)
   shows `factory-manager`, `kubelet-cadvisor` and `kube-state-metrics` up; sign in at `https://<ingress host>/grafana/` and open "Factory
   overview" (worker-pod panels stay at zero until the pod backend creates pods; the NodeClaim panels need the optional variable and the API
   version `ksm_nodeclaim_api_version` in `monitoring.tf`). Without signing in, with the default settings, `/grafana/metrics` must not return
   Prometheus text and `/grafana/api/health` must show `database` only, with no `version` or `commit`.

### Publishing the dashboards read-only

`grafana_anonymous_viewer_enabled` (`bool`, default `false`, needs `monitoring_enabled`; Terraform refuses it without) makes Grafana readable by
anyone who can reach the ingress host as an anonymous **Viewer**: a visitor opens `https://<ingress host>/grafana/` without signing in, lands
on the Factory overview dashboard. A visitor cannot change a dashboard, the data source or any setting (it can still create Grafana short
URLs). It is off by default. To enable it, set `grafana_anonymous_viewer_enabled = true`
in your tfvars (with `monitoring_enabled = true` already set) and apply as for any change: only the Grafana ConfigMap and pod change (its
`emptyDir` is emptied). Operators still sign in with Entra at `https://<ingress host>/grafana/login`, and `ingress_allowed_cidrs`, when set,
still limits who can reach the host. **Review first:** a visitor reads not only the three dashboards but, through Grafana's query API, every
series Prometheus holds (repository names, spend, pod and node names) and the alert rules. What a visitor can read and still write, the review
checklist, the checks and how to turn it off again: `docs/metrics.md`, "Publishing the dashboards read-only".

### Alerts and cost

- **Alerts** go **nowhere** by default: they show on Prometheus' alerts page and in Grafana. For notifications set **both** `alert_receiver`
  (`slack`, `msteams` or `webhook`) and `alert_receiver_secret_version` (the Key Vault secret `alert-receiver-url`; the URL is never in
  Terraform, state or a ConfigMap).
- **Cost.** The pods run on the **system node** with small requests (about 0.2 vCPU and 1 GiB in total) beside the AKS add-ons of a
  `Standard_D2ds_v5`; measure with `kubectl top node` and `kubectl -n monitoring top pods`. If it is too tight, set
  `monitoring_node_pool_vm_size`: a one-node pool `mon`, tainted `workload=monitoring:NoSchedule`, a real recurring node cost.

### Images

Pinned by tag **and digest** in `monitoring-images.auto.tfvars` (Kubernetes pulls the digest); `variables.tf` has the same
versions as digest-less defaults, and a test catches a change of one without the other. To upgrade, change tag, digest (the
multi-architecture index digest: `docker buildx imagetools inspect <image>:<tag>`) and default. If anonymous pull limits of the NAT address
bite, point `*_image` at a copy in your registry (the cluster has `AcrPull` on `acr_name` only).

### Security summary

- **Metrics port:** cluster-internal, no ingress route, a **bearer token** from Key Vault (constant-time comparison, `401` otherwise). A
  `NetworkPolicy` for it is **prepared and off** (`factory_network_policy_enabled`): only the Cilium data plane enforces it, and a policy that
  selects the Factory pod admits only what it lists, so check the rules first.
- **Grafana:** below `/grafana/` only; Entra OIDC with PKCE, strict roles; basic, proxy and JWT auth, sign-up and the login form are off, and
  **anonymous access is off unless you set `grafana_anonymous_viewer_enabled`**; no telemetry, plugin installs, external snapshots or public
  dashboards; its own `/metrics` is off and the version hidden; Caddy does not pass `Authorization` on to it. With Entra down nobody signs in;
  `kubectl -n monitoring port-forward svc/prometheus 9090` still works.
- **Secrets:** each readable by one identity only, as a file in the pod that needs it; no Kubernetes Secret exists for the stack. The kubelet
  scrape does not verify the kubelet certificate (`insecure_skip_verify` in `monitoring/prometheus.yml.tftpl`): use `ca_file` if yours verify
  against the cluster CA.

Turning `monitoring_enabled` off removes the namespace, identities, Caddy route and metrics Service and returns the Factory pod to the
loopback listener (it rolls once); the Prometheus disk is `Retain` and goes with the node resource group, or by hand.

## Browser sign-in and custom host name

The manager serves a dashboard at `https://<host>/ui/` (`docs/manager.md`, "The browser page"). It signs people in with **Microsoft Entra ID
in the browser** (MSAL.js, authorization code flow with PKCE) and calls the API with the access token; **the manager keeps verifying the
bearer token and the app roles itself**. There is no second trust root, no cookie and no new secret.

### 1. The app registration (the one that already protects the API)

Terraform does not manage it; use the portal or `az`:

- **Authentication** > **Add a platform** > **Single-page application**, one redirect URI per host, exactly as printed by the output
  `dashboard_spa_redirect_uris` (for example `https://factory.example.org/ui/`). Use the *Single-page application* platform, not *Web*: only
  it lets the browser redeem the code and issues it refresh tokens. A URI cannot be in both platforms; the Grafana URIs stay under **Web**.
- Through Microsoft Graph, by the application's object ID (the PATCH **replaces** the list, so include every URI to keep; `az ad app update
  --set spa.redirectUris=...` fails on a registration whose SPA platform is still empty):
  ```sh
  OBJ=$(az ad app show --id <client id> --query id -o tsv)
  az rest --method PATCH --uri "https://graph.microsoft.com/v1.0/applications/$OBJ" \
    --headers "Content-Type=application/json" \
    --body '{"spa":{"redirectUris":["https://<host>/ui/","https://<custom host>/ui/"]}}'      # 204, no output
  az ad app show --id <client id> --query spa.redirectUris      # read it back
  ```
  Entra validates the redirect URI only after sign-in, so the first real sign-in is the test.
- The page asks for `<origin>/ui/` and no other: **do not register the host root**, a wildcard or `http://` (except `http://localhost` for a
  test). Leave **implicit grant** and hybrid flow off.
- The page derives its client ID and scope from `entra_allowed_audiences` (`<client id>/.default`: **the GUID form, not
  `api://<client id>/.default`**, because the registration is also the API and Entra answers AADSTS90009 to a self-request by application
  ID URI). Only if the API is a different registration set `entra_spa_client_id` and `entra_spa_scope`.
- Version 2 tokens are required here too (`az ad app show --id <client id> --query api.requestedAccessTokenVersion` must print `2`), and
  **Assignment required** as in Bootstrap step 6: a person with no role signs in, sees "no Factory role" (a `403`) and never data.

### 2. Custom host name (optional)

Set `ingress_custom_host = "factory.example.org"` (in your tfvars, or in `infra/ingress.auto.tfvars`, whose committed
`factory.example.org` is a **placeholder**: replace it with a name you control or delete the file before a real deploy, or Caddy asks Let's
Encrypt for a name that is not yours; a value in your tfvars wins over the file) to serve the API, the dashboard and (with monitoring)
Grafana under your own name **as well as** the Azure name, which keeps working. Caddy gets and renews a certificate for each name;
Grafana's `root_url` follows the custom host; the outputs `ingress_hosts`, `dashboard_url`, `dashboard_spa_redirect_uris` and
`grafana_oauth_redirect_uris` list what to register. In this order:

1. **DNS.** A **CNAME** to `<ingress_dns_label>.<location>.cloudapp.azure.com` (output `custom_host_dns_target`) or an **A** record to
   `ingress_public_ip`; if your DNS provider proxies traffic (for example Cloudflare), start **DNS only** so Let's Encrypt reaches Caddy.
2. **Registrations.** Add the redirect URIs of step 1 (SPA, and the Web ones with monitoring) *before* you rely on the name.
3. **Apply** with `ingress_custom_host` set. Caddy's pod rolls and obtains the second certificate (`kubectl -n ingress logs deploy/caddy` shows
   `certificate obtained successfully` for each name). If `ingress_allowed_cidrs` is set, lift it for the issuance.
4. **Optionally turn the proxy on.** In Cloudflare use **SSL/TLS mode Full (strict)** (Flexible and plain Full cause a redirect loop). Behind a
   proxied record HTTP-01 works and TLS-ALPN-01 does not: keep port 80 open to the proxy and put no WAF or bot rule in front of
   `/.well-known/acme-challenge/`. A DNS-01 challenge would need a Caddy image with the DNS provider module and a new secret; it is not implemented.

The manager reads no host name: the page takes its redirect URI from the address it was loaded from, so each host needs its URI registered.

## Optional features

Everything here is **off by default**: with every variable at its default the plan has no line for the cluster, a node pool, the Deployment or
the data volume, and none for the new objects.

### Switching the data plane

Variables (all unset or off): `network_data_plane` and `network_policy` (`"cilium"` for both: the in-place switch is refused without the
second; only Cilium enforces a `NetworkPolicy`); `nap_mode` (`"Auto"`) and `nap_default_node_pools` (`"None"`: Factory defines its own pool);
`worker_nodepool_enabled` (the `AKSNodeClass` and `NodePool` `factory-workers` of `worker_pool.tf`; needs `nap_mode = "Auto"`,
`network_data_plane = "cilium"` and `family_vcpu_quota`); `worker_pool_sku` (`Standard_D4s_v6`), `worker_pool_limits_cpu` (32, a multiple of the node's vCPUs) and
`worker_pool_pods_per_node` (1); `family_vcpu_quota`, the limit of the VM family of `worker_pool_sku` in `az vm list-usage --location
<location>`, against which the plan checks `worker_pool_limits_cpu` plus twice the production vCPUs of that family (the surge reserve).
Terraform does **not** check the regional vCPU total (`cores`), so read it too.

These values are **never set by a pull request**: they live in `INFRA_TFVARS`, not in a pull request, so the guard is the only control. The
operator sets a value immediately before the manual `stage=cluster` apply that applies it, and an automatic run that races in is refused by
the guard before the approval. The order is fixed; each step is applied by hand, behind the drain gate (above), after snapshots, and is best
rehearsed first on a scratch deployment (its own `name_prefix` and state):

| Step | Set in `INFRA_TFVARS` | Run | What it does |
|---|---|---|---|
| (1) Cilium | `network_data_plane = "cilium"` and `network_policy = "cilium"` | manual, `stage=cluster`, `cluster_snapshots_taken` | `update azurerm_kubernetes_cluster.this` **in place**: the provider documents that every node is reimaged at once, and a single-replica `Recreate` workload on a ReadWriteOnce disk is down while its node is replaced. Plan the window; the drain gate pauses the trigger and drains first |
| (2) node auto-provisioning | `nap_mode = "Auto"` | manual, `stage=cluster`, `cluster_snapshots_taken` | `update azurerm_kubernetes_cluster.this` in place; not combined with step (1) |
| (3) NodePool objects | `worker_nodepool_enabled = true`, `family_vcpu_quota` | manual, `stage=all` (the `-target` lines of `stage=cluster` do not reach them) | `create` of `kubernetes_manifest.worker_nodeclass` and `worker_nodepool`, both `POOL_OBJECTS`; planned only after step (2), because the CRDs must exist at plan time |
| (4) pod backend | `pod_backend_enabled = true` (needs (1) to (3); the plan refuses it otherwise) | manual, `stage=all` | The objects of "Pod backend"; the same apply rolls the manager pod, behind the drain gate |

After a step the value **stays set**: removing `network_data_plane` or `nap_mode` later would ask for a replace of the cluster (going back from
Cilium forces a new cluster), which `prevent_destroy` refuses, and `worker_nodepool_enabled = false` DELETES the NodePool, a disruption that
ignores `do-not-disrupt`. A second full plan (`stage=all`) after a step must show no action on the Deployment (a replace restarts the manager on
its ReadWriteOnce disk), and `factory-sandbox-check` is re-run on a node of the new pool (`docs/operations.md`, "Platform maintenance").

### Pod backend

`pod_backend_enabled` creates the infrastructure for running each agent session in its own pod instead of inside the manager pod (design,
contract and migration of a repository: `docs/pod-backend.md`). The `pod_backend_*` locals are empty lists and maps while it is off, appended
to the Deployment with `concat` and `merge`, so a default plan is unchanged. What it adds:

- **The fence** (`worker_ns.tf`): namespace `factory-workers` (Pod Security `privileged`), service account `factory-worker` (no API token, no
  RoleBinding) and the NetworkPolicy `default-deny` (`kubernetes_namespace_v1.worker`, `kubernetes_service_account_v1.worker`,
  `kubernetes_network_policy_v1.worker_default_deny`): egress only to cluster DNS, the relay port of the manager pod and TCP 443 outside the
  metadata service, the Azure host service and the pod, service and node ranges. The agent ConfigMap also stops collecting the namespace's logs.
- **The admission policies** (`worker_policy.tf`, `infra/policy/*.json`): a ValidatingAdmissionPolicy and binding for worker pods
  (`factory-worker-pods`) and for their per-session Secrets (`factory-worker-secrets`) (`kubernetes_manifest.worker_policy_pods`,
  `worker_policy_pods_binding`, `worker_policy_secrets`, `worker_policy_secrets_binding`) and their parameter ConfigMap
  `kubernetes_config_map_v1.worker_policy_params` (its `image` is `local.image`). A HARD PREREQUISITE, not defence in depth: the namespace is
  `privileged`, so only these rules pin what the launcher may put in a pod (the image digest, the three relaxations and nothing else, one
  container, no host namespace or path, no secret source, the allow-listed environment, the worker pool, one repository per node) and in a
  Secret. Bound to ONE identity (the service account `factory` of the manager namespace), `factory-workers` only, `failurePolicy: Fail`, actions
  `Deny` and `Audit`; each validation carries the rule id of `src/factory/podspec.py` as `[rule:<id>]`.
- **The launcher's Role and RoleBinding** (`worker_rbac.tf`): `factory-launcher` (pods create, get, list, delete; Secrets create, delete) bound
  to the service account `factory`. Both `depends_on` the default-deny policy AND both admission policy bindings, so the fence exists before any
  Role does: every Role or RoleBinding of the worker namespace MUST `depends_on` `kubernetes_network_policy_v1.worker_default_deny`
  (`test_worker_ns_design.py` scans `infra/*.tf` for one that lacks it).
- **The manager pod** (`pod_backend.tf`, `k8s.tf`): the launcher and relay containers, the init container `volume-perms`, the `emptyDir`s
  `launcher-socket` and `spool`, the projected token only the launcher mounts, the annotation `azure.workload.identity/skip-containers`, the
  toleration `workload=manager:NoSchedule` and the ClusterIP Service `factory-relay-svc`. The main container also gets
  `FACTORY_POD_BACKEND=on`; a repository's sessions still run inside the manager pod until its `repo_configs` entry sets `execution = "pod"`
  (a plain configuration change, undone by removing the line).
- **Separate, default-off flips:** `manager_pool_taint_enabled` (needs `pod_backend_enabled`) taints pool `factory` with
  `workload=manager:NoSchedule` and may REPLACE the pool, so it is a `stage=cluster` change with `cluster_snapshots_taken`;
  `factory_network_policy_enabled` enables the prepared NetworkPolicy `factory-ingress` (`monitoring.tf`, enforced only with Cilium).

These objects are `POOL_OBJECTS` of the cluster guard: turning the variable off deletes the namespace and every worker pod in it, so do it only
through the drain gate. **Admission matrix against a real API server.** The CEL of the policies runs offline only through a model
(`tests/celmodel.py`), which is no evidence that a real API server accepts the expressions. `tools/policy_matrix.py` submits golden objects (all
must be admitted) and mutations (each must be denied with a rule id) with `kubectl create --dry-run=server` as the launcher identity; its
docstring has the four commands. Run the admission matrix on a scratch cluster (a kind cluster or a copy of this deployment; a kubeconfig that
may impersonate) before the first enabling flip, and read the first real plan and apply closely: the offline checks cannot show that the provider
accepts the `admissionregistration.k8s.io` kinds or how Cilium treats the namespace's `ipBlock.except` rule.

## Checks (run offline)

```sh
terraform -chdir=infra fmt -check -recursive
terraform -chdir=infra init -backend=false -input=false
terraform -chdir=infra validate
tflint --chdir infra                       # bundled terraform ruleset only, recommended preset, no plugin download
python3 infra/test/check_refs.py           # unused variables, dangling references, GUID-like or hex literals, secret resources
python3 -m unittest discover -s infra/test # the checker's tests and the design guards of the Terraform, policies and monitoring files
```

The design guards read the `.tf` files as text (and `src/factory/podcontract.py`, `podspec.py`, `kube.py` with `ast`): they pin, for example, the
three pod relaxations, that the optional variables are off by default, that the policy JSON equals the Python contract and that each monitoring
component reads one secret. `tests/test_infra_cluster_guard.py` checks the guard's address lists against the `.tf` files. These checks cannot show
what Azure accepts: read the first real plan and apply of each optional feature closely.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| A plan before the cluster exists fails with an error about the unknown host | Expected: apply `stage=cluster` first ("First apply") |
| Stage 2 fails with `Forbidden`/`Unauthorized` or `no matches for kind "SecretProviderClass"` | Role assignments or the CSI CRD have not propagated: wait and apply again |
| Every plan fails with `customresourcedefinitions ... is forbidden` | The plan identity lacks the custom role or its newest data action (Bootstrap step 4) |
| Node pool creation times out | The NSG outbound deny: relax `deny-other-internet-outbound` |
| The pod is `Pending`, or stuck in `Init` or `ContainerCreating` | Too little node capacity (lower `container_cpu`/`container_memory`), or a volume or secret did not mount: `kubectl -n factory describe pod` |
| `couldn't find key ... in Secret factory/factory-keyvault` | A key was added to the synced Secret: delete the Secret and the pod ("Monitoring", "Turn it on") |
| `403` from the Key Vault data plane during a plan | Default-deny vault: allow the runner in `key_vault_ip_rules` or plan from inside the VNet |
| An apply fails with `env[N].valueFrom: Invalid value` | An env entry was inserted into the middle of the list ("Operations notes") |
| The public URL answers 401 | Expected without a token; with one, a v1 token or a wrong audience or tenant ("Call the API") |
| No certificate | `kubectl -n ingress logs deploy/caddy`: DNS must resolve and ports 80 and 443 must be open (`ingress_allowed_cidrs` blocks validation) |
| The image digest is rejected | `image.auto.tfvars` still holds the placeholder, or the value is a tag |

## Where to read more

`docs/operations.md` (running and recovering the manager: drain, pause, platform maintenance, logs, the dashboard), `docs/manager.md` (the API,
its authentication, the browser page), `docs/pod-backend.md` (the optional pod backend), `docs/metrics.md` (the metrics catalogue and
"Publishing the dashboards read-only"), `docs/enrolling-repositories.md` and `docs/configuration.md` (registering a repository and its config),
`image/README.md` (building and checking the image).

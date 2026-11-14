# Identities.
#
#  - `workload`: the user-assigned identity of the Factory pod. It is NOT assigned to any node
#    or resource: the pod's service account (system:serviceaccount:factory:factory) is
#    federated to it on the cluster's OIDC issuer, and the pod exchanges its projected token
#    for an Entra token (workload identity). It reads two Key Vault secrets (through the
#    Secrets Store CSI driver), holds the blob leases (src/factory/locks.py) and writes the retained
#    log copies (src/factory/logarchive.py). No subscription-scope roles.
#  - `cluster`: the control-plane identity of the AKS cluster (network rights on the subnet
#    and, in ingress.tf, on the ingress public IP).
#  - the kubelet identity, created by AKS, pulls the image (AcrPull).

resource "azurerm_user_assigned_identity" "workload" {
  name                = local.names.identity
  location            = var.location
  resource_group_name = data.azurerm_resource_group.this.name
  tags                = var.tags
}

# Trust exactly one Kubernetes service account on exactly this cluster's issuer. A token from
# any other namespace, service account or cluster does not match issuer + subject + audience.
# https://raw.githubusercontent.com/MicrosoftDocs/azure-aks-docs/main/articles/aks/workload-identity-deploy-cluster.md
resource "azurerm_federated_identity_credential" "workload" {
  name                      = local.names.federated_credential
  user_assigned_identity_id = azurerm_user_assigned_identity.workload.id
  audience                  = ["api://AzureADTokenExchange"]
  issuer                    = azurerm_kubernetes_cluster.this.oidc_issuer_url
  subject                   = local.workload_identity_subject
}

# Read secret values from the vault through the Secrets Store CSI driver. "Key Vault
# Secrets User" is the role Microsoft documents for RBAC-authorized vaults
# (azure-aks-docs articles/aks/csi-secrets-store-identity-access.md).
resource "azurerm_role_assignment" "key_vault_secrets_user" {
  scope                = azurerm_key_vault.this.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.workload.principal_id
  principal_type       = "ServicePrincipal"
}

# Lease and metadata on the lock blobs, scoped to the `locks` container only and not to the
# account. Because the container is created with storage_account_id, its `id` is the ARM ID
# (.../storageAccounts/<name>/blobServices/default/containers/locks); azurerm 4.81 marks
# resource_manager_id as deprecated in favour of `id`
# (internal/services/storage/storage_container_resource.go in v4.81.0).
resource "azurerm_role_assignment" "locks_blob_data_contributor" {
  scope                = azurerm_storage_container.locks.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.workload.principal_id
  principal_type       = "ServicePrincipal"
}

# Write, list and delete the retained log copies, scoped to the `logs-archive` container only: the identity cannot
# touch anything else in the account through this assignment (it has the `locks` container's role separately).
# "Storage Blob Data Contributor" is the least built-in role that can put and delete blobs (there is no
# list-and-delete-only role); a blob lifecycle rule is not needed because the manager sweeps by age.
resource "azurerm_role_assignment" "logs_archive_blob_data_contributor" {
  scope                = azurerm_storage_container.logs_archive.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.workload.principal_id
  principal_type       = "ServicePrincipal"
}

# Pull the image from the operator-owned registry. The registry is looked up by name and
# resource group; Terraform does not manage it. The kubelet identity is the one AKS creates
# for the node pools (the nodes pull images, not the pod).
data "azurerm_container_registry" "this" {
  name                = var.acr_name
  resource_group_name = var.acr_resource_group_name
}

resource "azurerm_role_assignment" "acr_pull" {
  scope                = data.azurerm_container_registry.this.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_kubernetes_cluster.this.kubelet_identity[0].object_id
  principal_type       = "ServicePrincipal"
}

# Control plane of the cluster.
resource "azurerm_user_assigned_identity" "cluster" {
  name                = local.names.cluster_identity
  location            = var.location
  resource_group_name = data.azurerm_resource_group.this.name
  tags                = var.tags
}

# AKS needs to join the nodes to the node subnet (a bring-your-own subnet). Scoped to the
# subnet, not the VNet or the resource group.
# https://raw.githubusercontent.com/MicrosoftDocs/azure-aks-docs/main/articles/aks/configure-azure-cni.md
resource "azurerm_role_assignment" "cluster_network_contributor" {
  scope                = azurerm_subnet.nodes.id
  role_definition_name = "Network Contributor"
  principal_id         = azurerm_user_assigned_identity.cluster.principal_id
  principal_type       = "ServicePrincipal"
}

# --- Monitoring identities (monitoring.tf) ---------------------------------------------------------
#
# One identity per monitoring component (prometheus, grafana and, with a receiver, alertmanager), each
# federated to the service account of the same name in the namespace `monitoring` and allowed to read
# exactly ONE Key Vault secret: the role is scoped to that secret, not to the vault. A compromised
# Grafana pod can therefore not read the metrics token, and Prometheus cannot read the OIDC client
# secret. Nothing exists while monitoring_enabled is false.
resource "azurerm_user_assigned_identity" "monitoring" {
  for_each = local.monitoring_components

  name                = "${local.names.identity}-${each.key}"
  location            = var.location
  resource_group_name = data.azurerm_resource_group.this.name
  tags                = var.tags
}

resource "azurerm_federated_identity_credential" "monitoring" {
  for_each = local.monitoring_components

  name                      = "${local.names.federated_credential}-${each.key}"
  user_assigned_identity_id = azurerm_user_assigned_identity.monitoring[each.key].id
  audience                  = ["api://AzureADTokenExchange"]
  issuer                    = azurerm_kubernetes_cluster.this.oidc_issuer_url
  subject                   = "system:serviceaccount:${local.k8s_monitoring_namespace}:${each.key}"
}

# A role assignment may be scoped to one secret of an RBAC vault (the secret itself is uploaded by the
# operator, never by Terraform; only its name is spelled here).
resource "azurerm_role_assignment" "monitoring_secret_reader" {
  for_each = local.monitoring_components

  scope                = "${azurerm_key_vault.this.id}/secrets/${each.value.secret_name}"
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.monitoring[each.key].principal_id
  principal_type       = "ServicePrincipal"
}

# Optional: people or groups that upload the two secrets.
resource "azurerm_role_assignment" "key_vault_secrets_officer" {
  for_each = var.key_vault_secrets_officer_principal_ids

  scope                = azurerm_key_vault.this.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = each.value
}

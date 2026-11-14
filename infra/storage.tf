# Storage: a blob container for the per-repository leases that guarantee one supervisor per
# repository. Run state and worktrees live on an Azure Disk (k8s.tf), not in a storage
# account: a hostUsers=false pod cannot mount NFS.

# --- Lock account -----------------------------------------------------------------
#
# Holds one blob per repository (`<org>__<name>`) whose lease is the cross-replica lock.
# Default-deny firewall with the node subnet admitted through its Microsoft.Storage service
# endpoint. Public network access must stay "enabled" for that path: disabling it also
# blocks service-endpoint clients
# (https://raw.githubusercontent.com/MicrosoftDocs/azure-docs/main/articles/storage/files/storage-files-networking-overview.md).
# Account keys are off: only the workload identity (RBAC) can use the blobs.
resource "azurerm_storage_account" "locks" {
  name                     = local.names.locks_account
  location                 = var.location
  resource_group_name      = data.azurerm_resource_group.this.name
  account_kind             = "StorageV2"
  account_tier             = "Standard"
  account_replication_type = "LRS"

  https_traffic_only_enabled      = true
  shared_access_key_enabled       = false
  default_to_oauth_authentication = true
  allow_nested_items_to_be_public = false
  min_tls_version                 = "TLS1_2"

  network_rules {
    default_action             = "Deny"
    bypass                     = ["AzureServices"]
    virtual_network_subnet_ids = [azurerm_subnet.nodes.id]
  }

  tags = var.tags
}

# Created through Azure Resource Manager (storage_account_id), not the data plane.
resource "azurerm_storage_container" "locks" {
  name                  = local.lock_container_name
  storage_account_id    = azurerm_storage_account.locks.id
  container_access_type = "private"
}

# Retained logs: one gzip per transcript or log of a purged run, redacted before it is written, named
# `<org>__<repo>/<run>/<job>/<file>.gz` (src/factory/logarchive.py). The manager removes copies older than
# `logs_archive_retention_days` in its daily sweep; nothing else reads or deletes them. Same account, same
# default-deny firewall, no account keys: only the workload identity (identity.tf) can write here.
resource "azurerm_storage_container" "logs_archive" {
  name                  = local.logs_archive_container_name
  storage_account_id    = azurerm_storage_account.locks.id
  container_access_type = "private"
}

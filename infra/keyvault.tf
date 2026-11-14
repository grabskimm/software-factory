# Key Vault for the two secrets the app needs. Terraform declares NO secret values:
# the operator uploads `anthropic-api-key` and `github-app-private-key` out of band
# (README.md, "Bootstrap"), then passes their VERSION IDs as variables. The SecretProviderClass
# (k8s.tf) pins each secret to that version.
#
# - RBAC authorization and purge protection (azurerm 4.81 argument names:
#   rbac_authorization_enabled, purge_protection_enabled).
# - Default-deny firewall. The node subnet is admitted through its Microsoft.KeyVault service
#   endpoint: the Secrets Store CSI driver reads the secrets from a node (its provider pods
#   use the host network, and overlay pod traffic leaves the node with the node's address).
#   No trusted-service bypass is needed any more (it served Container Apps Key Vault
#   references), so it is off.
# - Public network access has to stay "enabled" for the service-endpoint path; the
#   firewall (default Deny) is what restricts it.
resource "azurerm_key_vault" "this" {
  name                = var.key_vault_name
  location            = var.location
  resource_group_name = data.azurerm_resource_group.this.name
  tenant_id           = var.tenant_id
  sku_name            = "standard"

  rbac_authorization_enabled    = true
  purge_protection_enabled      = true
  soft_delete_retention_days    = 90
  public_network_access_enabled = true

  network_acls {
    default_action             = "Deny"
    bypass                     = "None"
    ip_rules                   = var.key_vault_ip_rules
    virtual_network_subnet_ids = [azurerm_subnet.nodes.id]
  }

  tags = var.tags
}

# Audit trail for the vault: who read or changed which secret and when (the AuditEvent
# category carries identities, operations and secret names, never a secret value). It goes
# to the same Log Analytics workspace as the manager's stdout, with the same short retention.
resource "azurerm_monitor_diagnostic_setting" "key_vault" {
  name                       = "audit-to-log-analytics"
  target_resource_id         = azurerm_key_vault.this.id
  log_analytics_workspace_id = azurerm_log_analytics_workspace.this.id

  # Resource-specific table (AZKVAuditLogs) instead of the shared AzureDiagnostics table: the
  # dedicated table exists in every workspace from the start, so its retention can be set
  # before the first record arrives (the shared table only appears with the first log).
  log_analytics_destination_type = "Dedicated"

  enabled_log {
    category = "AuditEvent"
  }
}

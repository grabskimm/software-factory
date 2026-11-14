# Log Analytics for the cluster (Container Insights, aks.tf).
#
# Only the pods' stdout/stderr and the cluster's own inventory land here. Factory never prints agent
# output, prompts, issue text or request bodies (Invariant 6), and a 4-day retention keeps
# even operational lines short-lived.
#
# Container Insights would also copy every container's environment variables into the
# workspace (ContainerInventory). The manager's environment holds the Anthropic API key and the
# GitHub App private key, so the agent's ConfigMap switches that collection off
# (locals.tf k8s_insights_agent_config, created in k8s.tf), and the container sets
# AZMON_COLLECT_ENV=False as well (locals.tf app_env).
#
# Retention: the WORKSPACE default cannot go below 30 days on PerGB2018 (the service answers
# 400 "'RetentionInDays' property doesn't match the SKU limits" for 4 and for 7; observed on a
# real apply). The Azure Monitor documentation's "as little as four days" applies to the
# retention of individual Analytics TABLES, set through the API or CLI:
#   https://raw.githubusercontent.com/MicrosoftDocs/azure-monitor-docs/main/articles/azure-monitor/logs/data-retention-configure.md
# So the workspace keeps 30 days (which also covers every table Factory does not write to) and
# azapi_update_resource.table_retention sets var.log_retention_days on the tables that do hold
# Factory's output: the pods' stdout/stderr, the cluster events and inventory, and the Key
# Vault audit records. The standard tables exist in every workspace from the start, so the
# override needs no ordering against the cluster.
resource "azurerm_log_analytics_workspace" "this" {
  name                = local.names.log_analytics
  location            = var.location
  resource_group_name = data.azurerm_resource_group.this.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
  tags                = var.tags

  # local_authentication_enabled stays at its default (true) so that nothing about the
  # workspace depends on a feature this configuration does not exercise; the Container
  # Insights agent itself authenticates with the cluster's managed identity
  # (msi_auth_for_monitoring_enabled in aks.tf).
}

# PATCH-style override of the retention of the tables Factory writes to. Deleting these
# resources does nothing (azapi_update_resource leaves the properties as they are); the replace
# trigger re-applies the override if the workspace is ever re-created.
locals {
  retention_tables = toset(["ContainerLogV2", "KubeEvents", "KubePodInventory", "AZKVAuditLogs"])
}

resource "azapi_update_resource" "table_retention" {
  for_each = local.retention_tables

  type      = "Microsoft.OperationalInsights/workspaces/tables@2022-10-01"
  name      = each.key
  parent_id = azurerm_log_analytics_workspace.this.id

  body = {
    properties = {
      retentionInDays      = var.log_retention_days
      totalRetentionInDays = var.log_retention_days
    }
  }

  replace_triggers_external_values = [azurerm_log_analytics_workspace.this.workspace_id]
}

# Container Insights with managed-identity authentication (msi_auth_for_monitoring_enabled in
# aks.tf) sends nothing until the cluster is associated with a data collection rule: the
# Azure Monitor documentation for Terraform onboarding says to add the DCR and its association
# to the configuration ("Copy the DCR and DCRA resources from the Terraform templates"). Both
# are therefore owned here, so a destroy removes them too.
#   https://raw.githubusercontent.com/MicrosoftDocs/azure-monitor-docs/main/articles/azure-monitor/containers/kubernetes-monitoring-enable.md
#
# Streams: the pods' stdout/stderr (ContainerLogV2) and the cluster's events and pod inventory.
# Microsoft-ContainerInventory is NOT listed: that stream carries every container's
# environment variables, and the manager's environment holds two secrets (see the header).
# The environment-variable switch in the agent ConfigMap and AZMON_COLLECT_ENV=False stay as
# further layers. The name follows the platform convention MSCI-<region>-<cluster>.
resource "azurerm_monitor_data_collection_rule" "insights" {
  name                = local.names.insights_dcr
  location            = var.location
  resource_group_name = data.azurerm_resource_group.this.name
  kind                = "Linux"
  tags                = var.tags

  destinations {
    log_analytics {
      workspace_resource_id = azurerm_log_analytics_workspace.this.id
      name                  = "ciworkspace"
    }
  }

  data_flow {
    streams      = local.insights_streams
    destinations = ["ciworkspace"]
  }

  data_sources {
    extension {
      name           = "ContainerInsightsExtension"
      extension_name = "ContainerInsights"
      streams        = local.insights_streams
      extension_json = jsonencode({
        dataCollectionSettings = {
          interval               = "1m"
          namespaceFilteringMode = "Off"
          enableContainerLogV2   = true
        }
      })
    }
  }
}

resource "azurerm_monitor_data_collection_rule_association" "insights" {
  name                    = "ContainerInsightsExtension"
  target_resource_id      = azurerm_kubernetes_cluster.this.id
  data_collection_rule_id = azurerm_monitor_data_collection_rule.insights.id
}

locals {
  insights_streams = [
    "Microsoft-ContainerLogV2",
    "Microsoft-KubeEvents",
    "Microsoft-KubePodInventory",
  ]
}

# The control-plane log of node auto-provisioning (issue #149): Karpenter's own controller lines (nodeclaim created, registered and
# initialized, node deleted, pods that could not be scheduled, drift) reach Log Analytics only through a diagnostic setting with this one
# category; nothing else in the cluster scrapes Karpenter (it runs in the AKS control plane, no pod or Service exists in the cluster). Measured on a
# throwaway cluster: 396 records in 35 minutes of deliberate churn, at most about 7 minutes after the event, in the shared AzureDiagnostics
# table (the default destination, which the measurement used). Only the one category, so no other control-plane log is billed. It exists only with
# node auto-provisioning, whose category the cluster offers (docs/metrics.md).
resource "azurerm_monitor_diagnostic_setting" "karpenter_events" {
  count = var.nap_mode == "Auto" ? 1 : 0

  name                       = "karpenter-events-to-log-analytics"
  target_resource_id         = azurerm_kubernetes_cluster.this.id
  log_analytics_workspace_id = azurerm_log_analytics_workspace.this.id

  enabled_log {
    category = "karpenter-events"
  }
}

# Values the operator needs after an apply.

output "nat_public_ip" {
  description = "Static egress address of the Factory pods (NAT gateway). Add it to the GitHub organization IP allow list and to the registry firewall if the registry restricts public access."
  value       = azurerm_public_ip.nat.ip_address
}

output "ingress_public_ip" {
  description = "Static public address of the ingress (Caddy behind the Azure load balancer). The DNS name below resolves to it."
  value       = azurerm_public_ip.ingress.ip_address
}

output "manager_url" {
  description = "HTTPS base URL of the manager API. Every call needs `Authorization: Bearer <Entra access token>` (README.md, \"Call the API\"); without a token every path, /healthz included, answers 401."
  value       = "https://${local.ingress_fqdn}"
}

output "aks_cluster_name" {
  description = "Name of the AKS cluster (resource group: resource_group_name)."
  value       = azurerm_kubernetes_cluster.this.name
}

output "aks_get_credentials_command" {
  description = "Commands that write a kubeconfig for the cluster and make it use kubelogin with your Azure CLI login. Needs the Cluster User Role (granted to aks_cluster_admin_principal_ids)."
  value       = "az aks get-credentials --resource-group ${data.azurerm_resource_group.this.name} --name ${azurerm_kubernetes_cluster.this.name} && kubelogin convert-kubeconfig --login azurecli"
}

output "aks_oidc_issuer_url" {
  description = "OIDC issuer of the cluster: the issuer of the workload identity federated credential."
  value       = azurerm_kubernetes_cluster.this.oidc_issuer_url
}

output "identity_principal_id" {
  description = "Object ID of the workload's user-assigned identity, for the operator's own role checks."
  value       = azurerm_user_assigned_identity.workload.principal_id
}

output "identity_client_id" {
  description = "Client ID of the workload's user-assigned identity (annotation azure.workload.identity/client-id of the service account; AZURE_CLIENT_ID in the pod)."
  value       = azurerm_user_assigned_identity.workload.client_id
}

output "lock_container_url" {
  description = "Blob URL of the container whose per-repository blobs carry the leases."
  value       = "${trimsuffix(azurerm_storage_account.locks.primary_blob_endpoint, "/")}/${azurerm_storage_container.locks.name}"
}

output "logs_archive_container_url" {
  description = "Blob URL of the container that holds the retained, redacted copies of purged runs' logs (docs/operations.md, \"Logs\")."
  value       = "${trimsuffix(azurerm_storage_account.locks.primary_blob_endpoint, "/")}/${azurerm_storage_container.logs_archive.name}"
}

output "key_vault_uri" {
  description = "Vault URI. Upload the secrets `anthropic-api-key` and `github-app-private-key` here, then pass their version IDs as variables."
  value       = azurerm_key_vault.this.vault_uri
}

output "grafana_url" {
  description = "Where Grafana is served (below the API host, Entra sign-in), or null while monitoring_enabled is false."
  value       = var.monitoring_enabled ? "https://${local.ingress_fqdn}/grafana/" : null
}

output "ingress_hosts" {
  description = "Every host name Caddy serves and holds a certificate for: the Azure name and, when ingress_custom_host is set, the name of your own. Each answers the API, /ui/ and (with monitoring) /grafana/."
  value       = local.ingress_hosts
}

output "custom_host_dns_target" {
  description = "The name to point a CNAME record for ingress_custom_host at (an A record to ingress_public_ip works as well), or null while ingress_custom_host is unset."
  value       = local.ingress_custom_host == "" ? null : local.ingress_fqdn
}

output "dashboard_url" {
  description = "Where the dashboard page is served (the primary host: the custom host when set, else the Azure name). Opening it asks for a Microsoft sign-in; it needs the Factory.Viewer or Factory.Operator role."
  value       = "https://${local.ingress_primary_host}/ui/"
}

output "dashboard_spa_redirect_uris" {
  description = "The redirect URIs to add, under the platform \"Single-page application\", to the Entra app registration that protects the manager API: one per host, exactly as listed (README.md, \"Browser sign-in\"). Not secrets."
  value       = [for host in local.ingress_hosts : "https://${host}/ui/"]
}

output "grafana_oauth_redirect_uris" {
  description = "The Grafana redirect URIs (platform Web) for every host, or an empty list while monitoring_enabled is false. Grafana itself signs in through the primary host (its root_url); the other host is listed so that a bookmark on it works after the redirect."
  value       = var.monitoring_enabled ? [for host in local.ingress_hosts : "https://${host}/grafana/login/generic_oauth"] : []
}

output "grafana_oauth_redirect_uri" {
  description = "The redirect URI to add (platform Web) to the Entra app registration that Grafana signs in with. Not a secret."
  value       = "https://${local.ingress_fqdn}/grafana/login/generic_oauth"
}

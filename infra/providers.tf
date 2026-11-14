# Provider configuration.
#
# Authentication is deliberately not configured here so that the same code runs
# with `az login` locally and with OIDC in a CI pipeline: the providers read
# ARM_USE_OIDC / ARM_CLIENT_ID / ARM_USE_CLI from the environment, and the
# pipeline logs in to Azure first.
# Provider arguments: https://raw.githubusercontent.com/hashicorp/terraform-provider-azurerm/v4.81.0/website/docs/index.html.markdown

provider "azurerm" {
  subscription_id = var.subscription_id
  tenant_id       = var.tenant_id

  # The deploying identity is scoped to the resource group, so it cannot register
  # resource providers at subscription scope. Register them once (README.md).
  resource_provider_registrations = "none"

  # Use Entra ID rather than account keys for the few storage data-plane calls.
  # The lock storage account also disables shared-key access.
  storage_use_azuread = true

  features {
    key_vault {
      # The vault has purge protection; never try to purge on destroy.
      purge_soft_delete_on_destroy    = false
      recover_soft_deleted_key_vaults = true
    }
    storage {
      # The lock storage account is default-deny and Terraform runs outside the VNet,
      # so the provider must not call the storage data plane when it manages the
      # account. Documented at
      # https://raw.githubusercontent.com/hashicorp/terraform-provider-azurerm/v4.81.0/website/docs/guides/features-block.html.markdown
      data_plane_available = false
    }
  }
}

provider "azapi" {
  subscription_id = var.subscription_id
  tenant_id       = var.tenant_id
}

# In-cluster objects (k8s.tf, ingress.tf). The cluster has local accounts DISABLED and uses
# Entra ID with Azure RBAC, so there is no client certificate or static token to read from
# the cluster resource: only the API server address and its CA come from azurerm, and the
# bearer token is fetched on demand by kubelogin (`get-token`, Azure CLI login: the same
# `az login` session, local or the pipeline's, that the azurerm provider uses). kubelogin must be on
# the PATH. The identity running Terraform holds "Azure Kubernetes Service RBAC Cluster
# Admin" on the cluster (aks.tf). Exec plugin protocol:
# https://kubernetes.io/docs/reference/access-authn-authz/authentication/#client-go-credential-plugins
# kubelogin arguments: https://azure.github.io/kubelogin/concepts/login-modes/azurecli.html
#
# These values are unknown until the cluster exists. Resources of the typed kubernetes_*
# kinds are simply planned after it; `kubernetes_manifest` needs a reachable API server at
# PLAN time, which is why the first apply is staged (README.md).
provider "kubernetes" {
  host                   = azurerm_kubernetes_cluster.this.kube_config[0].host
  cluster_ca_certificate = base64decode(azurerm_kubernetes_cluster.this.kube_config[0].cluster_ca_certificate)

  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "kubelogin"
    args = [
      "get-token",
      "--login", "azurecli",
      "--server-id", local.aks_aad_server_app_id,
      "--tenant-id", var.tenant_id,
    ]
  }
}

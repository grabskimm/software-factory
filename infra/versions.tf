# Provider and Terraform version pins.
#
# azurerm ~> 4.81 was verified against the 4.81.0 documentation
# (https://raw.githubusercontent.com/hashicorp/terraform-provider-azurerm/v4.81.0/website/docs/).
# azapi ~> 2.13 was verified against the 2.13.0 documentation
# (https://raw.githubusercontent.com/Azure/terraform-provider-azapi/v2.13.0/docs/). azapi is used only where azurerm
# has no argument: Log Analytics retention below 30 days (observability.tf).
# kubernetes ~> 3.5 (hashicorp/kubernetes) creates the in-cluster objects (k8s.tf, ingress.tf).
# It is configured in providers.tf and authenticates to the Entra-protected API server with
# kubelogin, which must be on the PATH of whoever runs Terraform (README.md).
#
# Terraform >= 1.9 is required because variable validations read other variables
# (variables.tf). The exact provider builds are pinned in the committed .terraform.lock.hcl.

terraform {
  required_version = ">= 1.9.0, < 2.0.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.81"
    }
    azapi = {
      source  = "Azure/azapi"
      version = "~> 2.13"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 3.5"
    }
  }

  # Partial backend: no state location is invented here. Supply the storage
  # account, container and key at init time with -backend-config (see README.md).
  backend "azurerm" {}
}

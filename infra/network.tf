# Network: one VNet, one subnet for the AKS nodes, a NAT gateway with a static address as the
# cluster's only egress path, and an NSG.
#
# Sources used for the design (fetched 2026-11-06; the raw Markdown of the Microsoft Learn
# pages lives in https://github.com/MicrosoftDocs/azure-aks-docs/ and
# https://github.com/MicrosoftDocs/azure-docs/):
#   - azure-aks-docs articles/aks/nat-gateway.md              (userAssignedNATGateway: the subnet
#     must already have the NAT gateway when the cluster is created; Standard SKU)
#   - azure-aks-docs articles/aks/outbound-rules-control-egress.md (what nodes need outbound)
#   - azure-aks-docs articles/aks/azure-cni-overlay.md        (nodes take subnet addresses, pods
#     and services use their own ranges)
#   - azure-docs articles/storage/common/storage-network-security.md (service endpoints and
#     same-region IP rules)

data "azurerm_resource_group" "this" {
  name = var.resource_group_name

  lifecycle {
    postcondition {
      condition     = lower(replace(self.location, " ", "")) == var.location
      error_message = "var.location (${var.location}) does not match the region of resource group ${var.resource_group_name} (${self.location})."
    }
  }
}

resource "azurerm_virtual_network" "this" {
  name                = local.names.vnet
  location            = var.location
  resource_group_name = data.azurerm_resource_group.this.name
  address_space       = var.vnet_address_space
  tags                = var.tags
}

# Subnet for the AKS nodes (both pools). Not delegated.
# - Service endpoints for Storage and Key Vault: the lock account and the vault are
#   default-deny and admit this subnet. IP rules do not apply to same-region traffic and
#   traffic over a service endpoint uses a private source address, so a virtual-network
#   rule is the only way to admit the nodes (storage-network-security.md, "IP network rules").
#   The Secrets Store CSI driver and the blob lease client both run on a node, so their
#   traffic carries this subnet's identity.
resource "azurerm_subnet" "nodes" {
  name                 = local.names.node_subnet
  resource_group_name  = data.azurerm_resource_group.this.name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = [var.node_subnet_prefix]
  service_endpoints    = ["Microsoft.Storage", "Microsoft.KeyVault"]
}

# Static egress address (Invariant 8): all outbound traffic from the node subnet leaves
# through this NAT gateway (the cluster's outbound_type is userAssignedNATGateway, aks.tf).
# Standard SKU. Publish the address to the GitHub organization IP allow list and to the ACR
# firewall if the registry restricts public access; with API server authorized IP ranges it
# is added to that list automatically (locals.tf).
resource "azurerm_public_ip" "nat" {
  name                = local.names.nat_public_ip
  location            = var.location
  resource_group_name = data.azurerm_resource_group.this.name
  allocation_method   = "Static"
  sku                 = "Standard"
  tags                = var.tags
}

resource "azurerm_nat_gateway" "this" {
  name                = local.names.nat_gateway
  location            = var.location
  resource_group_name = data.azurerm_resource_group.this.name
  sku_name            = "Standard"
  tags                = var.tags
}

resource "azurerm_nat_gateway_public_ip_association" "this" {
  nat_gateway_id       = azurerm_nat_gateway.this.id
  public_ip_address_id = azurerm_public_ip.nat.id
}

resource "azurerm_subnet_nat_gateway_association" "nodes" {
  subnet_id      = azurerm_subnet.nodes.id
  nat_gateway_id = azurerm_nat_gateway.this.id

  depends_on = [azurerm_nat_gateway_public_ip_association.this]
}

locals {
  # NSG rules for the node subnet. Port filtering only: domain filtering happens in the
  # Claude Code sandbox allowlist, not here (Invariant 8).
  #
  # Inbound: the NSG default rules stay in force (virtual network, Azure Load Balancer probes,
  # deny everything else), and one rule admits the ingress ports. AKS does not touch an NSG
  # that is associated with a subnet you provide, so this rule exists at all
  # (azure-aks-docs articles/aks/concepts-network.md, "Network security groups"). The AKS load
  # balancer uses floating IP, so the destination the NSG sees is the ingress public IP and the
  # port is the Service port: destination "*" with ports 80 (ACME HTTP-01 and the redirect to
  # HTTPS) and 443.
  #
  # Pod network: with Azure CNI Overlay, pod-to-pod traffic between nodes is not encapsulated and
  # the pod addresses are NOT part of the VNet's address space, so the NSG sees them as foreign
  # (and, for outbound, as "Internet"). The documented requirement is to allow node to node,
  # node to pod and pod to pod traffic explicitly
  # (azure-aks-docs articles/aks/concepts-network-azure-cni-overlay.md, "Network security groups").
  # Traffic from a pod to anything outside the pod range leaves with the node's address.
  #
  # Outbound: HTTPS, the Azure platform address (DNS on 53, and the VM agent on 80 and 32526:
  # nodes cannot be provisioned without it) and the instance metadata service, then a deny for
  # every other internet-bound flow. Traffic inside the virtual network (nodes, the API
  # server's private tunnel endpoints, Azure DNS) is covered by the default outbound rules.
  # Not verified against every node bootstrap (README.md, "Network design"): if the first apply times out creating the node
  # pools, this deny rule is the first thing to relax.
  node_nsg_rules = {
    # Inbound.
    "allow-ingress-inbound" = {
      priority         = 100
      direction        = "Inbound"
      access           = "Allow"
      protocol         = "Tcp"
      sources          = local.ingress_source_prefixes
      destinations     = ["*"]
      destination_port = ["80", "443"]
      description      = "Ingress ports for Caddy (certificate validation on 80, API on 443) from the allowed clients."
    }
    "allow-pod-network-inbound" = {
      priority         = 110
      direction        = "Inbound"
      access           = "Allow"
      protocol         = "*"
      sources          = [var.node_subnet_prefix, var.aks_pod_cidr]
      destinations     = [var.node_subnet_prefix, var.aks_pod_cidr]
      destination_port = ["*"]
      description      = "Node-to-node, node-to-pod and pod-to-pod traffic of the cluster (Azure CNI Overlay)."
    }
    # Outbound.
    "allow-https-outbound" = {
      priority         = 100
      direction        = "Outbound"
      access           = "Allow"
      protocol         = "Tcp"
      sources          = [var.node_subnet_prefix]
      destinations     = ["Internet"]
      destination_port = ["443"]
      description      = "HTTPS to GitHub, Anthropic, Entra ID, Key Vault, storage, the registry, Let's Encrypt and the AKS control plane."
    }
    "allow-azure-platform-outbound" = {
      priority         = 110
      direction        = "Outbound"
      access           = "Allow"
      protocol         = "*"
      sources          = [var.node_subnet_prefix]
      destinations     = ["168.63.129.16"]
      destination_port = ["53", "80", "32526"]
      description      = "Azure DNS (53) and the VM agent (80, 32526). Never block 168.63.129.16 or the nodes stop working."
    }
    "allow-imds-outbound" = {
      priority         = 120
      direction        = "Outbound"
      access           = "Allow"
      protocol         = "Tcp"
      sources          = [var.node_subnet_prefix]
      destinations     = ["169.254.169.254"]
      destination_port = ["80"]
      description      = "Instance metadata service (kubelet and the Azure disk driver read the node identity from it)."
    }
    "allow-pod-network-outbound" = {
      priority         = 130
      direction        = "Outbound"
      access           = "Allow"
      protocol         = "*"
      sources          = [var.node_subnet_prefix, var.aks_pod_cidr]
      destinations     = [var.node_subnet_prefix, var.aks_pod_cidr]
      destination_port = ["*"]
      description      = "Node-to-node, node-to-pod and pod-to-pod traffic of the cluster (Azure CNI Overlay)."
    }
    "deny-other-internet-outbound" = {
      priority         = 4000
      direction        = "Outbound"
      access           = "Deny"
      protocol         = "*"
      sources          = [var.node_subnet_prefix]
      destinations     = ["Internet"]
      destination_port = ["*"]
      description      = "Everything else to the internet: no ports other than 443. Domains are filtered by the sandbox."
    }
  }
}

resource "azurerm_network_security_group" "nodes" {
  name                = local.names.node_nsg
  location            = var.location
  resource_group_name = data.azurerm_resource_group.this.name
  tags                = var.tags

  dynamic "security_rule" {
    for_each = local.node_nsg_rules

    content {
      name                    = security_rule.key
      priority                = security_rule.value.priority
      direction               = security_rule.value.direction
      access                  = security_rule.value.access
      protocol                = security_rule.value.protocol
      source_port_range       = "*"
      description             = security_rule.value.description
      destination_port_range  = length(security_rule.value.destination_port) == 1 ? security_rule.value.destination_port[0] : null
      destination_port_ranges = length(security_rule.value.destination_port) > 1 ? security_rule.value.destination_port : null
      # Exactly one of the singular and plural address arguments may be set, the same as
      # for ports.
      source_address_prefix        = length(security_rule.value.sources) == 1 ? security_rule.value.sources[0] : null
      source_address_prefixes      = length(security_rule.value.sources) > 1 ? security_rule.value.sources : null
      destination_address_prefix   = length(security_rule.value.destinations) == 1 ? security_rule.value.destinations[0] : null
      destination_address_prefixes = length(security_rule.value.destinations) > 1 ? security_rule.value.destinations : null
    }
  }
}

resource "azurerm_subnet_network_security_group_association" "nodes" {
  subnet_id                 = azurerm_subnet.nodes.id
  network_security_group_id = azurerm_network_security_group.nodes.id
}

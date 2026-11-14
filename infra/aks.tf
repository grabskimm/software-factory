# dd-iac-scan disable=terraform-azure-aks-private-cluster-disabled,terraform-azure-aks-network-policy-misconfigured
# The line above switches two IaC scanner rules off for this file only (every other rule still runs). The scanner reads it
# only at the very beginning of a file, and the two rule ids are the whole list. Both are decisions, not oversights:
#   - terraform-azure-aks-private-cluster-disabled: the API server is public on purpose, protected by Entra ID and Azure
#     RBAC with local accounts off, and restricted by aks_api_authorized_ip_ranges when the operator sets it. A private cluster
#     cannot be reached from a hosted CI runner that plans and applies this stack (infra/README.md, "Network design",
#     bullet "Cluster API"). Moving to one is an operator decision that needs a self-hosted runner.
#   - terraform-azure-aks-network-policy-misconfigured: network_policy is null (the argument is omitted) until the operator
#     stages the switch to the Cilium data plane (variables network_data_plane and network_policy; infra/README.md,
#     "Switching the data plane").
# The scanner's other findings on the cluster (a customer-managed disk encryption set, the Azure Policy add-on) are not
# decided here and stay visible.

# The AKS cluster that runs Factory: one small system pool and one user pool `factory` with
# exactly one Standard_D8ds_v5-class node that holds the manager pod.
#
# Why AKS and not Container Apps: the bubblewrap sandbox did not start on Container Apps and works only in a pod
# with spec.hostUsers=false, securityContext.procMount=Unmasked and an Unconfined AppArmor
# profile, on an Azure Linux node with runc. Those settings do not exist on Container Apps. A
# hostUsers=false pod cannot mount NFS, so /data is an Azure Disk (k8s.tf), which is
# ReadWriteOnce: exactly one replica, strategy Recreate.
#
# azurerm 4.81 arguments:
#   https://raw.githubusercontent.com/hashicorp/terraform-provider-azurerm/v4.81.0/website/docs/r/kubernetes_cluster.html.markdown
#   https://raw.githubusercontent.com/hashicorp/terraform-provider-azurerm/v4.81.0/website/docs/r/kubernetes_cluster_node_pool.html.markdown
# AKS concepts: azure-aks-docs articles/aks/{managed-azure-ad,azure-ad-rbac,workload-identity-overview,
# csi-secrets-store-driver,azure-cni-overlay,nat-gateway,api-server-authorized-ip-ranges}.md

resource "azurerm_kubernetes_cluster" "this" {
  name                = local.names.cluster
  location            = var.location
  resource_group_name = data.azurerm_resource_group.this.name
  tags                = var.tags

  dns_prefix          = var.name_prefix
  node_resource_group = local.names.node_resource_group
  sku_tier            = var.aks_sku_tier
  kubernetes_version  = var.aks_kubernetes_version

  # Upgrades are deliberate: the Kubernetes version is never upgraded automatically (the
  # attribute is omitted, which means `none`). The node OS image channel is a variable
  # because an image upgrade drains the only user node and so interrupts running work.
  node_os_upgrade_channel = var.aks_node_os_upgrade_channel

  # No static credentials: local accounts are disabled, authentication is Entra ID and
  # authorization is Azure RBAC for Kubernetes (the role assignments below). `az aks
  # get-credentials` then returns a kubeconfig that calls kubelogin.
  # https://raw.githubusercontent.com/MicrosoftDocs/azure-aks-docs/main/articles/aks/manage-azure-rbac.md
  local_account_disabled            = true
  role_based_access_control_enabled = true

  azure_active_directory_role_based_access_control {
    azure_rbac_enabled = true
    tenant_id          = var.tenant_id
  }

  # Workload identity: the OIDC issuer publishes the cluster's service account keys, and the
  # webhook injects the projected token into pods labelled azure.workload.identity/use=true.
  # identity.tf trusts one service account of this issuer.
  oidc_issuer_enabled       = true
  workload_identity_enabled = true

  # `az aks command invoke` would run kubectl through ARM with the caller's rights and bypass
  # the API server's authorized IP ranges. Nobody needs it here.
  run_command_enabled = false

  # The image cleaner (eraser) is off. Its per-node pod runs at `system-cluster-critical`
  # priority and requests resources, and the manager's node is requested to 100 percent of its
  # memory, so the kubelet preempted the manager ("Preempted in order to admit critical pod")
  # every six minutes while the cleaner's pod, which could not mount its exclusion ConfigMap,
  # stayed Pending: the manager was down for four hours on 2026-11-12. Old image digests now
  # stay on the node until the kubelet's own image garbage collection removes them under disk
  # pressure; watch the node's disk if that proves too slow.
  image_cleaner_enabled = false

  # The control plane runs as a user-assigned identity so that its role on the subnet (and,
  # in ingress.tf, on the ingress public IP) exists BEFORE the cluster does.
  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.cluster.id]
  }

  # Public API server, restricted to the authorized ranges when any are configured.
  dynamic "api_server_access_profile" {
    for_each = length(var.aks_api_authorized_ip_ranges) == 0 ? [] : [local.aks_authorized_ip_ranges]

    content {
      authorized_ip_ranges = api_server_access_profile.value
    }
  }

  # System pool: cluster add-ons only in practice, but no CriticalAddonsOnly taint, so a
  # small pool still schedules everything that is not pinned to the factory pool (Caddy).
  default_node_pool {
    name                         = local.names.system_pool
    vm_size                      = var.aks_system_pool_vm_size
    node_count                   = 1
    auto_scaling_enabled         = false
    os_sku                       = "AzureLinux3"
    vnet_subnet_id               = azurerm_subnet.nodes.id
    only_critical_addons_enabled = false
    temporary_name_for_rotation  = local.names.system_pool_rotation
    tags                         = var.tags

    upgrade_settings {
      max_surge = "1"
    }
  }

  # Azure CNI Overlay: nodes take addresses from the node subnet, pods and services from
  # their own ranges. All egress goes through the NAT gateway that is already attached to the
  # node subnet (network.tf), which is the static address of Invariant 8.
  network_profile {
    network_plugin      = "azure"
    network_plugin_mode = "overlay"
    outbound_type       = "userAssignedNATGateway"
    load_balancer_sku   = "standard"
    pod_cidr            = var.aks_pod_cidr
    service_cidr        = var.aks_service_cidr
    dns_service_ip      = local.aks_dns_service_ip

    # Both are null (the argument is omitted, the Azure data plane stays) until the operator sets them in INFRA_TFVARS for
    # the manual `stage=cluster` run (README.md, "Switching the data plane"). `azure` to `cilium` is an in-place update
    # that reimages every node at once; going back forces a new cluster, which prevent_destroy below refuses.
    network_data_plane = var.network_data_plane
    network_policy     = var.network_policy
  }

  # Node auto-provisioning (issue #144): the whole block is omitted while nap_mode is null. The provider reads the
  # argument as optional and computed, so omitting it cannot roll the mode back: that needs an explicit `Manual` after the
  # clean-up: no NAP node left and every NodePool at `limits.cpu` 0. The default pools are off (`None`): worker_pool.tf defines the one pool.
  dynamic "node_provisioning_profile" {
    for_each = var.nap_mode == null ? [] : [var.nap_mode]

    content {
      mode               = node_provisioning_profile.value
      default_node_pools = var.nap_default_node_pools
    }
  }

  # Key Vault Secrets Store CSI driver (add-on azure-keyvault-secrets-provider). Rotation is
  # off on purpose: secrets are pinned to a Key Vault version (locals.tf) and a new version
  # arrives through a Deployment rollout. The attribute is written out because the provider
  # requires one of the two rotation arguments to enable the add-on.
  key_vault_secrets_provider {
    secret_rotation_enabled = false
  }

  # Container Insights: stdout and stderr of the pods go to the workspace (observability.tf).
  oms_agent {
    log_analytics_workspace_id      = azurerm_log_analytics_workspace.this.id
    msi_auth_for_monitoring_enabled = true
  }

  # Only the Azure Disk CSI driver is used (/data). Azure Files and Blob volumes are not
  # possible for a hostUsers=false pod, so their drivers are not run.
  storage_profile {
    disk_driver_enabled         = true
    file_driver_enabled         = false
    blob_driver_enabled         = false
    snapshot_controller_enabled = true
  }

  # Egress and filtering must exist before the platform starts creating nodes, and the
  # control-plane identity needs its role on the subnet.
  depends_on = [
    azurerm_subnet_nat_gateway_association.nodes,
    azurerm_subnet_network_security_group_association.nodes,
    azurerm_role_assignment.cluster_network_contributor,
  ]

  # The data disk, the Caddy disk and the Prometheus disk live in the node resource group and are deleted with the
  # cluster, so a plan that would REPLACE or destroy it fails instead. It cannot depend on a variable (a lifecycle block
  # takes no expression). To tear the stack down on purpose, to re-create the cluster after a disk copy, or to delete a
  # test deployment, remove the line in a scratch copy of infra/ and never commit that (README.md, "Operations notes").
  # The data volume claim in k8s.tf has its own prevent_destroy: a destroy of a test deployment needs both removed.
  lifecycle {
    prevent_destroy = true
  }
}

# User pool for the manager pod: exactly one node, no autoscaler, labelled pool=factory (the
# Deployment's nodeSelector). One node is a deliberate single point of failure: the manager
# is one process per repository (blob lease lock) and its /data disk is ReadWriteOnce.
# A node image or Kubernetes upgrade surges one extra node of this size (the subscription
# needs vCPU quota for two); the disk then moves with the pod.
#
# node_taints is null (the argument is omitted, the plan has no line for it) until the operator sets manager_pool_taint_enabled
# (variables.tf, pod_backend.tf): then the pool is tainted workload=manager:NoSchedule, the manager pod's toleration (which ships
# with the pod backend) lets it stay, and nothing else without a toleration is scheduled here. A change of node_taints may
# REPLACE this pool (UNVERIFIED for the pinned provider): the pool is a CLUSTER object of the guard, so it needs a manual
# stage=cluster run with cluster_snapshots_taken=true.
resource "azurerm_kubernetes_cluster_node_pool" "factory" {
  name                  = local.names.user_pool
  kubernetes_cluster_id = azurerm_kubernetes_cluster.this.id
  mode                  = "User"
  vm_size               = var.aks_user_pool_vm_size
  node_count            = 1
  auto_scaling_enabled  = false
  os_sku                = "AzureLinux3"
  vnet_subnet_id        = azurerm_subnet.nodes.id
  node_labels           = local.user_pool_labels
  node_taints           = local.manager_pool_node_taints
  tags                  = var.tags

  upgrade_settings {
    max_surge = "1"
  }
}

# Optional dedicated node for the monitoring stack (monitoring.tf), only when monitoring_node_pool_vm_size is
# set. Tainted so nothing but the monitoring pods, which tolerate the taint, is scheduled here: the
# Factory pod and the cluster add-ons never compete with Prometheus and Grafana for it. Without it the
# monitoring pods run on the system node. A node costs real money: measure first (README.md, Monitoring).
resource "azurerm_kubernetes_cluster_node_pool" "monitoring" {
  count = var.monitoring_enabled && var.monitoring_node_pool_vm_size != null ? 1 : 0

  name                  = local.names.monitoring_pool
  kubernetes_cluster_id = azurerm_kubernetes_cluster.this.id
  mode                  = "User"
  vm_size               = coalesce(var.monitoring_node_pool_vm_size, "unset")
  node_count            = 1
  auto_scaling_enabled  = false
  os_sku                = "AzureLinux3"
  vnet_subnet_id        = azurerm_subnet.nodes.id
  node_labels           = local.monitoring_pool_labels
  node_taints           = ["workload=monitoring:NoSchedule"]
  tags                  = var.tags

  upgrade_settings {
    max_surge = "1"
  }
}

# Kubernetes authorization is Azure RBAC, so an Azure role on the cluster is the whole
# permission model: Cluster Admin on the cluster scope for exactly the principals in
# var.aks_cluster_admin_principal_ids, the apply identity among them (the variable requires
# it to be non-empty and README.md Bootstrap step 4 says who must be in it). The set is NOT
# extended with whoever runs Terraform: the read-only plan identity and the apply identity
# are different principals, and a for_each keyed by the caller would give the preview plan and
# the apply plan different action lists, which a pipeline that compares the two (a fingerprint gate) refuses. The Cluster
# User Role is what allows `listClusterUserCredential`, which `az aks get-credentials` and the
# azurerm provider's kube_config use; Cluster Admin is documented to include it, and listing
# it explicitly costs nothing.
# Role assignments can take minutes to propagate: if the first apply of the in-cluster
# objects answers Forbidden, apply again (README.md).
resource "azurerm_role_assignment" "aks_cluster_admin" {
  for_each = var.aks_cluster_admin_principal_ids

  scope                = azurerm_kubernetes_cluster.this.id
  role_definition_name = "Azure Kubernetes Service RBAC Cluster Admin"
  principal_id         = each.value
}

resource "azurerm_role_assignment" "aks_cluster_user" {
  for_each = var.aks_cluster_admin_principal_ids

  scope                = azurerm_kubernetes_cluster.this.id
  role_definition_name = "Azure Kubernetes Service Cluster User Role"
  principal_id         = each.value
}

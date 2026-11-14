# The worker node pool of the pod backend (issues #144 and #146): an AKSNodeClass and a NodePool of node
# auto-provisioning, plus the ONE local that every number of the pool derives from. Everything here is OFF by default
# (worker_nodepool_enabled = false, count 0): with the defaults the plan has no line for any of it.
#
# Both objects are `kubernetes_manifest`, count-gated by worker_nodepool_enabled, for two reasons. The custom resource
# definitions exist only after the cluster runs node auto-provisioning (aks.tf, nap_mode = Auto), and a
# `kubernetes_manifest` reads them at PLAN time, so the objects cannot be planned in the run that switches it on (the
# staged order is in README.md, "Switching the data plane"). And the typed Kubernetes resources know no NodePool.
# Deleting or changing a NodePool is a manual disruption that ignores `karpenter.sh/do-not-disrupt`, which is why both
# addresses are listed under POOL_OBJECTS in tools/infra_cluster_guard.py: an automatic run after an image build refuses
# a plan that touches them, and a manual run applies them through the drain gate.
#
# What the NodePool deliberately does not set:
#   * terminationGracePeriod. A NodePool-level value takes precedence over a pod's own grace period and bypasses
#     `do-not-disrupt`. With it unset, only the pod deadline, a manual deletion and the
#     loss of the node can end a session early.
#   * a spot capacity type. Sessions are hours long and a spot eviction is a lost session.
# `expireAfter: Never` because a node that holds a session must not be rotated by the one forceful path whose behaviour
# is disputed; an empty node is consolidated after 30 seconds anyway (WhenEmpty).
#
# UNVERIFIED against the installed custom resource definitions (check on a scratch cluster first, docs/pod-backend.md): the API versions,
# whether `expireAfter` belongs under spec.template.spec (written here, as in the default pool the add-on creates) or
# under spec.disruption (as one documentation page shows), the kubelet field `podPidsLimit`, `startupTaints`, and whether the
# `karpenter.azure.com/sku-name` requirement accepts the default size Standard_D4s_v6 (not tested on it).
#   https://raw.githubusercontent.com/MicrosoftDocs/azure-aks-docs/main/articles/aks/node-auto-provisioning-node-pools.md
#   https://raw.githubusercontent.com/MicrosoftDocs/azure-aks-docs/main/articles/aks/node-auto-provisioning-aksnodeclass.md
#   https://raw.githubusercontent.com/MicrosoftDocs/azure-aks-docs/main/articles/aks/node-auto-provisioning-disruption.md

locals {
  worker_pool_count = var.worker_nodepool_enabled ? 1 : 0

  # --- VM sizes and the vCPU quota family ----------------------------------------------------------------------------

  # Standard_<letters><vCPUs><feature letters>_v<version>. The vCPU count is the regex that container_cpu applies to
  # aks_user_pool_vm_size, widened to every letter because the system and monitoring size variables accept more than D
  # and E. The quota is counted per VM FAMILY: the family key is the name without its vCPU digits, so Standard_D4ds_v5
  # and Standard_D8ds_v5 are both "Dds_v5" and a pool of another family (an E or a Das size) is not counted.
  vm_size_pattern = "^Standard_(?P<letters>[A-Za-z]+)(?P<vcpu>[0-9]+)(?P<feature>[A-Za-z]*)_v(?P<version>[0-9]+)$"

  # The pools that exist today, one node each (node_count = 1 in aks.tf): the system pool, the manager's `factory` pool
  # and, only when monitoring runs on a node of its own, the `mon` pool (the same condition as its count in aks.tf).
  production_pool_sizes = concat(
    [var.aks_system_pool_vm_size, var.aks_user_pool_vm_size],
    var.monitoring_enabled && var.monitoring_node_pool_vm_size != null ? [coalesce(var.monitoring_node_pool_vm_size, "unset")] : [],
  )

  vm_size_matches = {
    for size in distinct(concat([var.worker_pool_sku], local.production_pool_sizes)) : size => try(regex(local.vm_size_pattern, size), null)
  }

  # null for a size name that does not parse: the precondition of the NodePool then fails closed.
  vm_size_facts = {
    for size, found in local.vm_size_matches : size => found == null ? null : {
      vcpu   = tonumber(found.vcpu)
      family = "${found.letters}${found.feature}_v${found.version}"
    }
  }

  production_pools_parse = alltrue([for size in local.production_pool_sizes : local.vm_size_facts[size] != null])

  # The vCPUs of the production pools that are in the worker pool's VM family, and only those.
  worker_family_production_vcpu = sum(concat([0], [
    for size in local.production_pool_sizes : local.vm_size_facts[size].vcpu
    if try(local.vm_size_facts[size].family, "") == local.vm_size_facts[var.worker_pool_sku].family
  ]))

  # --- The one local ----------------------------------------------------------------------------------------------

  # Everything that depends on the size of the worker pool reads THIS local and nothing else reads
  # var.worker_pool_limits_cpu, var.worker_pool_sku or var.worker_pool_pods_per_node: the NodePool limit below and, in
  # the slices that add the launcher and the pod pool, the concurrency ceiling FACTORY_MAX_POD_SESSIONS and the
  # launcher's maximum of live pods. (FACTORY_MAX_TOTAL_WORKERS keeps governing in-process work and is not derived
  # from it.) One repository per node is a prerequisite of the pod backend, so N repositories need N nodes and
  # pods_per_node stays 1 until a single repository runs in pod mode. Default: floor(32 / 4) * 1 = 8.
  worker_pool = {
    node_vcpu     = local.vm_size_facts[var.worker_pool_sku].vcpu
    limits_cpu    = var.worker_pool_limits_cpu
    pods_per_node = var.worker_pool_pods_per_node
    max_sessions  = floor(var.worker_pool_limits_cpu / local.vm_size_facts[var.worker_pool_sku].vcpu) * var.worker_pool_pods_per_node
  }

  # The surge reserve. A node image or Kubernetes upgrade surges one extra node of each production pool (max_surge = 1
  # in aks.tf), so the vCPUs of the worker family that must stay available are the pool's own limit plus TWICE the
  # production vCPUs of that family. The figures are a function of the configured pools, never a constant:
  #   largest limit = floor((quota - 2 * production vCPUs of the family) / node vCPUs) * node vCPUs.
  # With the default pools (system D2ds_v5 + factory D8ds_v5, plus D2ds_v5 with the optional mon pool: 12 vCPUs of the
  # Dds_v5 family) and the default worker size Standard_D4s_v6 (family Ds_v6), no production vCPU is in the worker family, so
  # this check only needs the limit within the family quota. What binds is then the REGIONAL vCPU total (`cores` in
  # `az vm list-usage`), which this check cannot see: limit + 2 * production vCPUs must fit it too. A worker size of the
  # production family (Standard_D4ds_v5) shares that family's quota with the production pools and is not offered in every
  # zone (az vm list-skus shows it for your subscription).
  worker_pool_vcpu_need = local.worker_pool.limits_cpu + 2 * local.worker_family_production_vcpu

  # The pods per node of the default resource class of the pod contract: worker_pool_pods_per_node may not exceed it.
  worker_class_pods_per_node = local.worker_contract.classes.table[local.worker_contract.classes.default].pods_per_node
}

# The Azure half of the node settings: the image family and a bound on the processes of a pod. A pod spec cannot carry a
# pids limit, so the kubelet of the node does (podPidsLimit is the field the documentation names: UNVERIFIED against the
# installed definition; 4096 is the value of the documentation's example, PROVISIONAL until the footprint is measured).
# Changing a field of an AKSNodeClass marks the nodes provisioned from the old one as drifted and replaces them, which a
# `do-not-disrupt` session holds back.
resource "kubernetes_manifest" "worker_nodeclass" {
  count = local.worker_pool_count

  manifest = {
    apiVersion = "karpenter.azure.com/v1beta1"
    kind       = "AKSNodeClass"
    metadata = {
      name   = local.worker_contract.names.nodeclass
      labels = local.k8s_labels
    }
    spec = {
      # There is no `AzureLinux3` value: `AzureLinux` is Azure Linux 3 on Kubernetes 1.32 and later.
      imageFamily = "AzureLinux"
      kubelet = {
        podPidsLimit = 4096
      }
    }
  }

  lifecycle {
    precondition {
      condition     = var.nap_mode == "Auto"
      error_message = "worker_nodepool_enabled needs nap_mode = Auto: the AKSNodeClass definition exists only with node auto-provisioning on."
    }

    precondition {
      condition     = var.network_data_plane == "cilium"
      error_message = "worker_nodepool_enabled needs network_data_plane = cilium."
    }
  }

  depends_on = [
    azurerm_role_assignment.aks_cluster_admin,
    azurerm_role_assignment.aks_cluster_user,
  ]
}

resource "kubernetes_manifest" "worker_nodepool" {
  count = local.worker_pool_count

  manifest = {
    apiVersion = "karpenter.sh/v1"
    kind       = "NodePool"
    metadata = {
      name   = local.worker_contract.names.nodepool
      labels = local.k8s_labels
    }
    spec = {
      template = {
        metadata = {
          labels = {
            (local.worker_contract.nodes.worker_label_key) = local.worker_contract.nodes.worker_label_value
            # Required for Karpenter to predict the overhead of the Cilium DaemonSet (the default pool carries it).
            "kubernetes.azure.com/ebpf-dataplane" = "cilium"
          }
        }
        spec = {
          # No `terminationGracePeriod` here, by design (see the header).
          expireAfter = "Never"
          nodeClassRef = {
            group = "karpenter.azure.com"
            kind  = "AKSNodeClass"
            name  = local.worker_contract.names.nodeclass
          }
          taints = [
            {
              key    = local.worker_contract.nodes.worker_taint_key
              value  = local.worker_contract.nodes.worker_taint_value
              effect = local.worker_contract.nodes.taint_effect
            },
          ]
          # The default pool holds a new node back until the Cilium agent is ready; NAP does not require a pod to
          # tolerate a startup taint.
          startupTaints = [
            {
              key    = "node.cilium.io/agent-not-ready"
              value  = "true"
              effect = "NoExecute"
            },
          ]
          requirements = [
            {
              key      = "karpenter.azure.com/sku-name"
              operator = "In"
              values   = [var.worker_pool_sku]
            },
            {
              key      = "karpenter.sh/capacity-type"
              operator = "In"
              values   = ["on-demand"]
            },
            {
              key      = "kubernetes.io/arch"
              operator = "In"
              values   = ["amd64"]
            },
            {
              key      = "kubernetes.io/os"
              operator = "In"
              values   = ["linux"]
            },
          ]
        }
      }
      disruption = {
        consolidationPolicy = "WhenEmpty"
        consolidateAfter    = "30s"
      }
      limits = {
        cpu = tostring(local.worker_pool.limits_cpu)
      }
    }
  }

  lifecycle {
    precondition {
      condition     = var.nap_mode == "Auto"
      error_message = "worker_nodepool_enabled needs nap_mode = Auto: the NodePool definition exists only with node auto-provisioning on."
    }

    precondition {
      condition     = var.network_data_plane == "cilium"
      error_message = "worker_nodepool_enabled needs network_data_plane = cilium."
    }

    precondition {
      condition     = local.worker_pool.max_sessions >= 1
      error_message = "worker_pool_limits_cpu must hold at least one node of worker_pool_sku."
    }

    precondition {
      condition     = local.worker_pool.pods_per_node <= local.worker_class_pods_per_node
      error_message = "worker_pool_pods_per_node must not exceed the pods per node of the default resource class of the pod contract."
    }

    precondition {
      condition     = local.production_pools_parse
      error_message = "A VM size of a production pool does not look like Standard_D2ds_v5, so its vCPU family cannot be counted."
    }

    precondition {
      condition     = var.family_vcpu_quota != null ? local.worker_pool_vcpu_need <= var.family_vcpu_quota : false
      error_message = "worker_pool_limits_cpu plus twice the production vCPUs of the same VM family must not exceed family_vcpu_quota (the reserve for node image and Kubernetes upgrade surges). Lower worker_pool_limits_cpu, or ask for more quota."
    }
  }

  depends_on = [
    kubernetes_manifest.worker_nodeclass,
    azurerm_role_assignment.aks_cluster_admin,
    azurerm_role_assignment.aks_cluster_user,
  ]
}

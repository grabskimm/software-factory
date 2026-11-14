# The fence of the pod backend (issues #144 and #145): the worker namespace, its service account and the default-deny
# NetworkPolicy. Everything here is OFF by default (pod_backend_enabled = false, count 0): with the default the plan has no
# line for any of it, and the Container Insights ConfigMap data (locals.tf, k8s_log_exclusion_lines) is byte-identical to
# what it was before. Nothing launches a pod in this namespace yet: the admission policies are worker_policy.tf, the Role and
# RoleBinding of the launcher are worker_rbac.tf (slice 16, each waiting for the policy below) and the launcher itself is a
# container of the manager pod (pod_backend.tf, slice 17); docs/pod-backend.md. There is NO RoleBinding in THIS file, so with
# it alone no identity can create a pod here.
#
# What the namespace is:
#   * `pod-security.kubernetes.io/enforce: privileged`. The three relaxations of a worker pod (hostUsers, procMount and
#     appArmorProfile, the same as the manager pod's, k8s.tf) are refused by the baseline and restricted levels, so the
#     namespace is explicitly privileged, like `factory`. What keeps that safe is not the label: creating a pod here needs a
#     Role that only the launcher is bound to (a later slice), a ValidatingAdmissionPolicy pins what a pod may say (a later
#     slice), and the policy below fences what a pod can reach.
#   * created AFTER the Container Insights agent ConfigMap (`depends_on`), so the log exclusion for this namespace exists
#     before the first worker pod does. A ConfigMap change restarts the agent; the runbook checks that it has restarted with
#     the new settings before the first session.
#
# What the service account is: a name for the pod to carry (`serviceAccountName`) with `automount_service_account_token =
# false`: a worker pod has no Kubernetes credential at all and no workload identity. No RoleBinding names it, in this slice or
# any later one.
#
# What the NetworkPolicy is, and why it is the control. The node network security group cannot tell a pod from the kubelet:
# network.tf lets the whole node subnet reach the instance metadata service (169.254.169.254:80) and the Azure host service
# (168.63.129.16), so only a policy enforced by the data plane keeps a worker pod away from them. The policy selects every pod
# of the namespace and:
#   * denies ingress entirely (there is no `ingress` block: no pod, not even another worker, can reach a worker pod);
#   * allows egress to exactly three things: (a) cluster DNS (the kube-dns pods, port 53), (b) the relay, which is a container
#     of the manager pod: the manager pod label in the `factory` namespace on the relay port (locals.tf, worker_contract), and
#     (c) TCP 443 to every address EXCEPT 169.254.0.0/16 (the metadata service), 168.63.129.16/32 (the Azure host service),
#     the pod range, the service range and the node subnet (the variables that already exist: aks_pod_cidr, aks_service_cidr,
#     node_subnet_prefix). The rest of the VNet address space is not listed in the exception: the node subnet is the only
#     subnet of this configuration (network.tf).
# Whether the API server's PUBLIC address is also excluded is NOT relied on: the network canary of the worker (a later slice)
# and the live negative control assert that an unauthenticated request to it is refused. FQDN filtering needs a paid add-on and
# is not used: 443 to the rest of the Internet stays open and the sandbox domain list stays the only domain filter, as today.
# The policy is enforced by Cilium: with `network_data_plane = "cilium"` (variables.tf) an engine exists; without it the policy
# is accepted by the API server and not enforced, which is why the namespace refuses to plan without it (the preconditions below)
# and why the canary FAILS a session whose pod can still reach a forbidden address.
#
# UNVERIFIED (the mirror, docs/pod-backend.md): that Cilium enforces `ipBlock.except` on this cluster and whether
# `network_policy` must also be set; that the DNS rule reaches the kube-dns pods after Cilium has translated the service
# address; the `exclude_namespaces` key spelling and the agent's restart behaviour; the documented default list.

locals {
  worker_ns_count = var.pod_backend_enabled ? 1 : 0

  worker_network_policy_name = "default-deny"

  # TCP 443 may go anywhere except these ranges. The first two are fixed addresses of the Azure platform; the other three are
  # the ranges of this cluster, taken from the variables that define them.
  worker_egress_excluded_cidrs = [
    "169.254.0.0/16",
    "168.63.129.16/32",
    var.aks_pod_cidr,
    var.aks_service_cidr,
    var.node_subnet_prefix,
  ]
}

resource "kubernetes_namespace_v1" "worker" {
  count = local.worker_ns_count

  metadata {
    name = local.worker_contract.names.worker_namespace
    labels = merge(local.k8s_labels, {
      "${local.worker_contract.labels.prefix}role" = local.worker_contract.labels.role_value
      # The three relaxations of the worker pod need it; see the header.
      "pod-security.kubernetes.io/enforce" = "privileged"
    })
  }

  lifecycle {
    precondition {
      condition     = var.network_data_plane == "cilium"
      error_message = "pod_backend_enabled needs network_data_plane = cilium: without it no engine enforces the NetworkPolicy of the worker namespace."
    }

    precondition {
      condition     = var.nap_mode == "Auto"
      error_message = "pod_backend_enabled needs nap_mode = Auto: the worker pods run on nodes that node auto-provisioning creates."
    }

    precondition {
      condition     = var.worker_nodepool_enabled
      error_message = "pod_backend_enabled needs worker_nodepool_enabled: the worker pods run on the NodePool of worker_pool.tf."
    }
  }

  depends_on = [
    azurerm_role_assignment.aks_cluster_admin,
    azurerm_role_assignment.aks_cluster_user,
    kubernetes_config_map_v1.insights_agent,
  ]
}

resource "kubernetes_service_account_v1" "worker" {
  count = local.worker_ns_count

  metadata {
    name      = local.worker_contract.names.worker_service_account
    namespace = kubernetes_namespace_v1.worker[0].metadata[0].name
    labels    = local.k8s_labels
  }

  automount_service_account_token = false

  depends_on = [
    azurerm_role_assignment.aks_cluster_admin,
    azurerm_role_assignment.aks_cluster_user,
  ]
}

# ORDER, binding on slice 16 and on every later object of this namespace: this policy must exist BEFORE any Role or RoleBinding
# does, so that no moment exists in which an identity may create a pod here without the fence. The RoleBinding of slice 16 (and
# any Role, RoleBinding, ClusterRole or ClusterRoleBinding that names this namespace or the worker service account) MUST list
# kubernetes_network_policy_v1.worker_default_deny in its `depends_on`, next to the namespace and the admission policy
# bindings (docs/pod-backend.md, "Terraform B1" and slice 16). This slice declares none of them; infra/test/test_worker_ns_design.py
# scans every infra/*.tf and fails for one that lacks the dependency. A comment changes no plan line.
resource "kubernetes_network_policy_v1" "worker_default_deny" {
  count = local.worker_ns_count

  metadata {
    name      = local.worker_network_policy_name
    namespace = kubernetes_namespace_v1.worker[0].metadata[0].name
    labels    = local.k8s_labels
  }

  spec {
    pod_selector {}

    policy_types = ["Ingress", "Egress"]

    # No `ingress` block: ingress is denied entirely.

    # (a) Cluster DNS.
    egress {
      ports {
        port     = "53"
        protocol = "UDP"
      }
      ports {
        port     = "53"
        protocol = "TCP"
      }
      to {
        namespace_selector {
          match_labels = {
            "kubernetes.io/metadata.name" = "kube-system"
          }
        }
        pod_selector {
          match_labels = {
            "k8s-app" = "kube-dns"
          }
        }
      }
    }

    # (b) The relay: a container of the manager pod.
    egress {
      ports {
        port     = tostring(local.worker_contract.ports.relay)
        protocol = "TCP"
      }
      to {
        namespace_selector {
          match_labels = {
            "kubernetes.io/metadata.name" = local.k8s_namespace
          }
        }
        pod_selector {
          match_labels = local.k8s_selector_labels
        }
      }
    }

    # (c) TCP 443 outside the metadata service, the Azure host service and the ranges of this cluster.
    egress {
      ports {
        port     = "443"
        protocol = "TCP"
      }
      to {
        ip_block {
          cidr   = "0.0.0.0/0"
          except = local.worker_egress_excluded_cidrs
        }
      }
    }
  }

  depends_on = [
    azurerm_role_assignment.aks_cluster_admin,
    azurerm_role_assignment.aks_cluster_user,
  ]
}

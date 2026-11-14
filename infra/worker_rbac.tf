# The launcher's permissions in the worker namespace (slice 16 of the pod backend, issues #144 and #145): ONE Role and ONE
# RoleBinding. Everything here is OFF by default (pod_backend_enabled = false, count 0): with the default the plan has no line
# for either of them.
#
# What the Role grants, and nothing else, in the namespace `factory-workers` only (a Role is namespaced; there is no ClusterRole
# and no ClusterRoleBinding of the pod backend):
#   * pods:    create, get, list, delete   (build the pod, read its state, count the labelled pods, delete it)
#   * secrets: create, delete              (the per-session Secret that carries the two secrets, and its compensation)
# It grants no `update` and no `patch` (so the two admission policies of worker_policy.tf, which judge CREATE only, see every
# way a pod or a Secret is written), no `watch`, no `deletecollection`, no `exec`, no `pods/log`, no `pods/attach`, no
# `pods/portforward`, no `pods/ephemeralcontainers`, no secret `get`, `list` or `watch` and no other resource: the launcher
# (src/factory/launcher.py, through src/factory/kube.py) uses exactly these six verbs on these two resources and a test reads
# the client for them. A Role with a wildcard, with `secrets` readable or with a subresource would let the holder of the token
# read the Anthropic key it writes or run a command in a pod, and fails infra/test/test_pod_backend_design.py.
#
# Who is bound: the service account `factory` of the manager namespace, the identity the manager pod runs as. Its token is NOT
# mounted by default (k8s.tf: automountServiceAccountToken = false): only the launcher container mounts a projected token
# (pod_backend.tf), so the main container, which runs the supervisors and every worker of today's in-process mode, holds no
# Kubernetes credential. The admission policies of worker_policy.tf are bound to the very same identity by name
# (request.userInfo.username), so the Role and the policies name one subject and cannot drift apart (a test compares them).
#
# ORDER, binding (docs/pod-backend.md, "Terraform B1" and "Terraform B2"): no moment may exist in which the launcher may create
# a pod in the namespace without the fence. Both resources therefore list in `depends_on` the default-deny NetworkPolicy
# (kubernetes_network_policy_v1.worker_default_deny) AND both admission policy bindings
# (kubernetes_manifest.worker_policy_pods_binding, kubernetes_manifest.worker_policy_secrets_binding), next to the cluster
# admin role assignments. The Role waits as well as the binding, so that not even an unbound Role exists before the fence.
# infra/test/test_worker_ns_design.py and infra/test/test_policy_json.py scan every infra/*.tf for a Role-like resource that
# names the worker namespace or account and fail without those entries. A comment changes no plan line.
#
# Deleting the binding (pod_backend_enabled = false) ends the launcher's right at once; the Role and the binding are listed
# under POOL_OBJECTS in tools/infra_cluster_guard.py with the other objects of the namespace.
#
# UNVERIFIED (docs/pod-backend.md): that the launcher token can create and delete a pod and a Secret in the namespace and cannot
# get or list Secrets or exec; that a plan run as the plan identity may read these two kinds (the custom role
# already carries roles/read and rolebindings/read since slice 15b: no new data action is needed); that the provider accepts the
# empty `api_groups` entry of the core group.

locals {
  worker_launcher_role_name = "factory-launcher"
}

resource "kubernetes_role_v1" "worker_launcher" {
  count = local.worker_ns_count

  metadata {
    name      = local.worker_launcher_role_name
    namespace = kubernetes_namespace_v1.worker[0].metadata[0].name
    labels    = local.k8s_labels
  }

  # Pods: build the pod, read its state, list the labelled pods, delete it.
  rule {
    api_groups = [""]
    resources  = ["pods"]
    verbs      = ["create", "get", "list", "delete"]
  }

  # Secrets: write the per-session Secret, delete it as the compensation of a failed launch. Never read.
  rule {
    api_groups = [""]
    resources  = ["secrets"]
    verbs      = ["create", "delete"]
  }

  depends_on = [
    azurerm_role_assignment.aks_cluster_admin,
    azurerm_role_assignment.aks_cluster_user,
    kubernetes_network_policy_v1.worker_default_deny,
    kubernetes_manifest.worker_policy_pods_binding,
    kubernetes_manifest.worker_policy_secrets_binding,
  ]
}

resource "kubernetes_role_binding_v1" "worker_launcher" {
  count = local.worker_ns_count

  metadata {
    name      = local.worker_launcher_role_name
    namespace = kubernetes_namespace_v1.worker[0].metadata[0].name
    labels    = local.k8s_labels
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.worker_launcher[0].metadata[0].name
  }

  subject {
    kind      = "ServiceAccount"
    name      = local.k8s_service_account
    namespace = local.k8s_namespace
  }

  depends_on = [
    azurerm_role_assignment.aks_cluster_admin,
    azurerm_role_assignment.aks_cluster_user,
    kubernetes_network_policy_v1.worker_default_deny,
    kubernetes_manifest.worker_policy_pods_binding,
    kubernetes_manifest.worker_policy_secrets_binding,
    kubernetes_role_v1.worker_launcher,
  ]
}

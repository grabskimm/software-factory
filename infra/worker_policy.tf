# The admission policies of the pod backend (issues #144 and #145): the HARD PREREQUISITE that pins what the launcher may say in a
# worker pod and in the per-session Secret that carries its two secrets. A pod created in `factory-workers` by the launcher is
# only as safe as these rules: the namespace is Pod Security `privileged` (the three relaxations of a worker pod need it), so the
# label alone fences nothing. Everything here is OFF by default (pod_backend_enabled = false, count 0): with the default the plan
# has no line for any of it, and the objects are not read from the cluster either.
#
# What is created:
#   * `kubernetes_config_map_v1.worker_policy_params`: the PARAMETER of the pod policy, `image` = local.image, the very local that
#     the launcher's environment takes its image from (slice 17), so the two cannot drift. The policy admits a pod only if every
#     container image equals it and has the digest form. The ConfigMap lives in the worker namespace, which no Role grants a verb
#     on configmaps. An automatic image promotion changes the local and so this ConfigMap and the launcher's environment: the order
#     of the two is not controlled and a bounded window in which the launcher's pods are denied is possible (UNVERIFIED: rehearse it
#     on a scratch cluster); a denial is a refusal to launch, never an admission.
#   * `kubernetes_manifest.worker_policy_pods` and its binding, `worker_policy_secrets` and its binding: the ValidatingAdmissionPolicy
#     and ValidatingAdmissionPolicyBinding objects, read from the JSON files of infra/policy/ with `jsondecode(file(...))`. The
#     same files are what tools/policy_matrix.py applies to a real API server (a kind cluster, say) before it replays the golden
#     objects and every mutation of tests/golden/, so Terraform and the test share one source. Typed
#     resources exist for the policy (kubernetes_validating_admission_policy_v1) but not, as far as the provider documentation
#     read for this change shows, for the binding; one manifest path for both keeps one lifecycle (the kubernetes_manifest path for
#     admissionregistration.k8s.io/v1 kinds is UNVERIFIED until a plan and an apply have run on a scratch cluster).
#
# What the policies are bound to: the requests of ONE identity, the service account `factory` of the manager namespace (the only
# identity whose token reaches the launcher, slice 17), by `matchConditions` on `request.userInfo.username`; the namespace
# `factory-workers` by the binding's namespace selector; `failurePolicy: Fail` (an error while evaluating is a refusal); the
# actions `Deny` and `Audit`; a missing parameter is a `Deny` (`parameterNotFoundAction`). They match CREATE of pods and of
# secrets only: the launcher's Role (slice 16) has no `update` or `patch`. Every validation carries the stable rule id of
# src/factory/podspec.py (POLICY_RULES) as `[rule:<id>]` in its message.
#
# ORDER, binding on slice 16: the RoleBinding that lets the launcher create pods MUST list both policy bindings
# (kubernetes_manifest.worker_policy_pods_binding and worker_policy_secrets_binding) in its `depends_on`, so that no moment exists in
# which the launcher may create a pod without the fence (docs/pod-backend.md, slice 16; infra/test/test_policy_json.py).
#
# What a text test cannot show (UNVERIFIED): that the provider path accepts these kinds, that the CEL of infra/policy/*.json is
# accepted and evaluated by the API server of AKS the way a real API server of the pinned kind version does (the CEL environment
# and the pod schema differ by Kubernetes version), and that the plan identity may read the kinds (infra/README.md, "Bootstrap", step 4).

locals {
  worker_policy_param_name = "worker-policy-params"
}

resource "kubernetes_config_map_v1" "worker_policy_params" {
  count = local.worker_ns_count

  metadata {
    name      = local.worker_policy_param_name
    namespace = kubernetes_namespace_v1.worker[0].metadata[0].name
    labels    = local.k8s_labels
  }

  data = {
    image = local.image
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
  ]
}

resource "kubernetes_manifest" "worker_policy_pods" {
  count = local.worker_ns_count

  manifest = jsondecode(file("${path.module}/policy/pod-policy.json"))

  depends_on = [
    azurerm_role_assignment.aks_cluster_admin,
    azurerm_role_assignment.aks_cluster_user,
  ]
}

resource "kubernetes_manifest" "worker_policy_pods_binding" {
  count = local.worker_ns_count

  manifest = jsondecode(file("${path.module}/policy/pod-binding.json"))

  depends_on = [
    azurerm_role_assignment.aks_cluster_admin,
    azurerm_role_assignment.aks_cluster_user,
    kubernetes_manifest.worker_policy_pods,
    kubernetes_config_map_v1.worker_policy_params,
  ]
}

resource "kubernetes_manifest" "worker_policy_secrets" {
  count = local.worker_ns_count

  manifest = jsondecode(file("${path.module}/policy/secret-policy.json"))

  depends_on = [
    azurerm_role_assignment.aks_cluster_admin,
    azurerm_role_assignment.aks_cluster_user,
    kubernetes_namespace_v1.worker,
  ]
}

resource "kubernetes_manifest" "worker_policy_secrets_binding" {
  count = local.worker_ns_count

  manifest = jsondecode(file("${path.module}/policy/secret-binding.json"))

  depends_on = [
    azurerm_role_assignment.aks_cluster_admin,
    azurerm_role_assignment.aks_cluster_user,
    kubernetes_manifest.worker_policy_secrets,
  ]
}

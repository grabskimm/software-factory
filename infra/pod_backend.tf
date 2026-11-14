# The manager pod of the pod backend (slice 17, issues #144, #145 and #147): the launcher and the relay as two more containers
# of the manager pod, the init container that sets the owner of the two directories they share with the main container, the
# projected token that ONLY the launcher mounts, the relay Service the worker pods reach it through, the environment of the main
# container, a toleration of the manager taint and the optional taint of the manager node pool.
#
# Everything here is OFF by default. `local.pod_backend_on` is `local.worker_ns_count == 1`, that is `pod_backend_enabled`, and
# every local below is an empty list or map (or null) while it is off: k8s.tf appends them to the lists and maps of the Deployment
# with `concat` and `merge`, so with the default the Deployment is the very object it was before (no entry moves, none is added)
# and the plan has no line for it. The relay Service has `count = local.worker_ns_count`. The design record is
# docs/pod-backend.md ("Terraform C"); the programs are src/factory/launcher.py and src/factory/relay.py.
#
# What the pod becomes with the backend on (names, uids, paths and ports are the contract's, locals.tf `worker_contract`, never
# retyped here):
#
#   container      user                      secrets it holds                       mounts
#   -------------  ------------------------  -------------------------------------  ----------------------------------------
#   factory        the image's (starts as    both Key Vault secrets (environment,   data, repos (as before); the launcher
#   (main)         root, drops to 10001)     as before); NO Kubernetes token        socket directory READ-ONLY, the spool
#   launcher       10003, group 10001        the Anthropic key (environment, to     the socket directory, the projected
#                                            write per-session Secrets); the only   token (token and CA, at the standard path)
#                                            Kubernetes token
#   relay          10004, group 10001        none; no token, no cloud identity      the spool
#   volume-perms   root of the user          none                                   the socket directory and the spool
#   (init)         namespace, fixed command
#
#   * The launcher and the relay have every capability dropped, no privilege escalation, a read-only root file system and the
#     runtime-default seccomp profile. They do NOT carry the three relaxations of the main container (procMount, AppArmor): the
#     sandbox needs those, they do not. `hostUsers: false` is the pod's and so theirs.
#   * The pod keeps `automountServiceAccountToken = false`. The token the launcher uses is a projected volume mounted at the
#     standard path in THAT container only (and the cluster CA from the `kube-root-ca.crt` ConfigMap that every namespace has),
#     so the main container, which runs every supervisor and every worker of today's in-process mode, and the relay, which parses
#     what untrusted pods send and refuses to start when a token file is mounted, hold no Kubernetes credential.
#   * The Azure workload identity webhook injects `AZURE_*` variables and a token volume mount into every container of a pod that
#     carries the label `azure.workload.identity/use`, the init containers too. The relay refuses to start with an `AZURE_*`
#     name and the launcher must hold no cloud identity, so the pod template gets the annotation
#     `azure.workload.identity/skip-containers` naming the three new containers (UNVERIFIED on the cluster).
#   * `/run/factory` (the launcher socket directory) and `/spool` (the relay's spool) are `emptyDir`s shared by the containers.
#     A sidecar that runs non-root with every capability dropped cannot set up its own directory, so the init container sets the
#     owner and mode once, before any of them starts: the socket directory is owned by the launcher uid, group 10001, mode 0750
#     (the main container's uid 10001 is in group 10001 and may enter it and connect, the worker uid 10002 may not), the spool is
#     owned by the relay uid, group 10001, mode 2770 (the supervisor writes the session record and the bundle there, the relay
#     the rest). These are exactly the modes src/factory/launcher.py and src/factory/relay.py check at start and refuse
#     without. The spool `sizeLimit` is the contract's 16Gi: the relay refuses uploads at 12 GiB, which is below it, because a
#     tripped `sizeLimit` evicts the manager pod.
#   * Sizes: requests equal limits for every container, so the pod stays Guaranteed. The two sidecars together ask for 0.35 vCPU
#     and 512 Mi next to the main container (variables.tf keeps the main container to at most 75 % of the node): PROVISIONAL,
#     nothing was measured. There is no probe on a sidecar: a crashed one is restarted by the kubelet and the manager's own
#     liveness probe is unchanged.
#
# The launcher's environment is the contract's `ENV_LAUNCHER` (the names are read from `worker_contract.env`, so no name is
# typed twice): the image is `local.image`, the very local that worker_policy.tf puts in the parameter of the admission policy,
# so the launcher and the policy cannot drift; the live-pod ceiling is `local.worker_pool.max_sessions`, the one local every
# number of the pool derives from (worker_pool.tf) and so is the main container's FACTORY_MAX_POD_SESSIONS; the manager address
# is the Service name of the app (the pod address is not known to Terraform and changes with every restart; the network canary of
# the worker pod asserts that it cannot connect to it); the API server's public host is the cluster's public FQDN (the canary asserts that an
# unauthenticated request to it is refused); only the default resource class is enabled. The Anthropic key is ONE `secretKeyRef`
# to the Secret the Secrets Store CSI driver syncs (the main container reads the same one).
#
# FACTORY_RELAY_PORT has the shape of the variable Kubernetes injects for a Service called `factory-relay`
# (docs/pod-backend.md, slice 8), and the relay refuses to start when it holds the injected `tcp://...` value. Two layers: the
# Deployment sets `enableServiceLinks = false` (k8s.tf), the relay's environment sets the name explicitly (an explicit entry
# wins over an injected one), and the Service is named `factory-relay-svc`, whose injected name would be FACTORY_RELAY_SVC_PORT.
#
# The optional taint of the manager node pool (`manager_pool_taint_enabled`, aks.tf) is separate from the toleration. The
# toleration comes with the backend (a Deployment change that rolls the pod anyway), the taint is a later flip of a pool that
# is a CLUSTER object of tools/infra_cluster_guard.py, so the toleration always exists before the taint does.
#
# UNVERIFIED (docs/pod-backend.md, "Terraform C": none of it can be seen without a cluster): that the kubelet accepts all of
# this as one pod and the three containers start; what `SO_PEERCRED` reports and what the modes of the two `emptyDir`s are for
# containers of different uids under `hostUsers: false`; that the init container with only CHOWN, FOWNER and FSETID can set the
# owner and the setgid bit; that a read-only mount of the socket directory still lets the main container connect; that the
# webhook honours the skip annotation for init containers; that the projected token and the CA are readable by uid 10003 at the
# standard path (the token file mode, no `fsGroup`); that a worker pod reaches the relay through the Service under Cilium (the
# policy of worker_ns.tf selects the manager pod and the Service is translated before it is applied); the sizes; that the
# Secrets Store CSI driver and the Azure Disk CSI driver tolerate the manager taint.

locals {
  pod_backend_on = local.worker_ns_count == 1

  pod_backend_relay_service_name = "${local.k8s_app_name}-relay-svc"
  pod_backend_relay_host         = "${local.pod_backend_relay_service_name}.${local.k8s_namespace}.svc.cluster.local"

  # The three containers the webhook must leave alone (see the header).
  pod_backend_container_names = {
    init     = "volume-perms"
    launcher = "launcher"
    relay    = "relay"
  }

  # Volumes of the backend: the two shared directories and the projected token. The names are not part of the contract: they are
  # the pod's own.
  pod_backend_volume_names = {
    socket = "launcher-socket"
    spool  = "spool"
    token  = "launcher-token"
  }

  # Where the Kubernetes client of the launcher looks for its token and the CA (src/factory/kube.py, SERVICE_ACCOUNT_DIR).
  pod_backend_token_mount_path = "/var/run/secrets/kubernetes.io/serviceaccount"

  # PROVISIONAL, nothing was measured. Requests equal limits.
  pod_backend_sizing = {
    launcher = { cpu = "100m", memory = "128Mi" }
    relay    = { cpu = "250m", memory = "384Mi" }
  }

  # Both sidecars: no privilege escalation, no capability, a read-only root file system, the runtime-default seccomp profile, and
  # a numeric non-root user so that the kubelet can verify it. The user and the group are added per container below. None of the
  # three relaxations of the main container is here.
  pod_backend_sidecar_security = {
    runAsNonRoot             = true
    allowPrivilegeEscalation = false
    readOnlyRootFilesystem   = true
    capabilities             = { drop = ["ALL"] }
    seccompProfile           = { type = "RuntimeDefault" }
  }

  # The container command is the image's interpreter and the module, the same interpreter k8s.tf uses for the probes.
  pod_backend_python = "/opt/factory/bin/python"

  # --- the main container -------------------------------------------------------------------------------------------------

  # Appended AFTER every other entry of the main container's environment (the provider fills a planned list from the prior
  # state by position, see app_env_optional in locals.tf), so with the backend off nothing moves. Names are the contract's.
  pod_backend_main_env_all = [
    { name = local.worker_contract.env.manager.pod_backend, value = local.worker_contract.env_values.pod_backend_on },
    { name = local.worker_contract.env.manager.max_pod_sessions, value = tostring(local.worker_pool.max_sessions) },
    { name = local.worker_contract.env.manager.launcher_socket, value = local.worker_contract.paths.launcher_socket },
    { name = local.worker_contract.env.manager.spool_dir, value = local.worker_contract.paths.spool_dir },
  ]
  pod_backend_main_env = [for entry in local.pod_backend_main_env_all : entry if local.pod_backend_on]

  # The socket directory is read-only for the main container: it connects to the socket and creates, renames or removes nothing
  # in it. The spool is read-write: the supervisor writes the session record and the bundle there.
  pod_backend_main_mounts_all = [
    { name = local.pod_backend_volume_names.socket, mountPath = local.worker_contract.paths.launcher_socket_dir, readOnly = true },
    { name = local.pod_backend_volume_names.spool, mountPath = local.worker_contract.paths.spool_dir },
  ]
  pod_backend_main_mounts = [for mount in local.pod_backend_main_mounts_all : mount if local.pod_backend_on]

  # --- the pod ------------------------------------------------------------------------------------------------------------

  pod_backend_pod_annotations_all = {
    "azure.workload.identity/skip-containers" = join(";", values(local.pod_backend_container_names))
  }
  pod_backend_pod_annotations = { for key, value in local.pod_backend_pod_annotations_all : key => value if local.pod_backend_on }

  # The toleration of the manager taint comes with the backend, never with the taint of the pool (see the header). It is a
  # merge into the pod spec because a key that is absent must stay absent while the backend is off.
  pod_backend_pod_spec_extra_all = {
    tolerations = [
      {
        key      = local.worker_contract.nodes.manager_taint_key
        operator = "Equal"
        value    = local.worker_contract.nodes.manager_taint_value
        effect   = local.worker_contract.nodes.taint_effect
      },
    ]
  }
  pod_backend_pod_spec_extra = { for key, value in local.pod_backend_pod_spec_extra_all : key => value if local.pod_backend_on }

  # The taint of the node pool `factory` (aks.tf). null, not an empty list: the argument stays out of the plan while it is off.
  manager_pool_node_taints = var.manager_pool_taint_enabled ? [
    "${local.worker_contract.nodes.manager_taint_key}=${local.worker_contract.nodes.manager_taint_value}:${local.worker_contract.nodes.taint_effect}",
  ] : null

  pod_backend_init_containers_all = [
    {
      name            = local.pod_backend_container_names.init
      image           = local.image
      imagePullPolicy = "IfNotPresent"
      # A fixed command: two owners and two modes, from the contract's ids and paths. `chown` first, so that the setgid bit of the
      # spool is set by `chmod` on the final owner.
      command = [
        "/bin/sh",
        "-c",
        join(" && ", [
          "chown ${local.worker_contract.ids.launcher_uid}:${local.worker_contract.ids.shared_gid} ${local.worker_contract.paths.launcher_socket_dir}",
          "chmod 0750 ${local.worker_contract.paths.launcher_socket_dir}",
          "chown ${local.worker_contract.ids.relay_uid}:${local.worker_contract.ids.shared_gid} ${local.worker_contract.paths.spool_dir}",
          "chmod 2770 ${local.worker_contract.paths.spool_dir}",
        ]),
      ]
      resources = {
        requests = local.secrets_init_resources
        limits   = local.secrets_init_resources
      }
      # Root of the user namespace (hostUsers: false) with three capabilities and nothing else: no secret, no network use, a
      # read-only root file system. FSETID keeps the setgid bit of the spool when the directory's group is not the caller's.
      securityContext = {
        runAsUser                = 0
        runAsNonRoot             = false
        allowPrivilegeEscalation = false
        readOnlyRootFilesystem   = true
        capabilities = {
          drop = ["ALL"]
          add  = ["CHOWN", "FOWNER", "FSETID"]
        }
        seccompProfile = { type = "RuntimeDefault" }
      }
      volumeMounts = [
        { name = local.pod_backend_volume_names.socket, mountPath = local.worker_contract.paths.launcher_socket_dir },
        { name = local.pod_backend_volume_names.spool, mountPath = local.worker_contract.paths.spool_dir },
      ]
    },
  ]
  pod_backend_init_containers = [for item in local.pod_backend_init_containers_all : item if local.pod_backend_on]

  pod_backend_containers_all = [
    {
      name            = local.pod_backend_container_names.launcher
      image           = local.image
      imagePullPolicy = "IfNotPresent"
      command         = [local.pod_backend_python, "-m", "factory.launcher"]
      env = [
        { name = local.worker_contract.env.launcher.image, value = local.image },
        { name = local.worker_contract.env.launcher.namespace, value = local.worker_contract.names.worker_namespace },
        { name = local.worker_contract.env.launcher.max_pods, value = tostring(local.worker_pool.max_sessions) },
        { name = local.worker_contract.env.launcher.repo_cap, value = tostring(local.worker_contract.limits.per_repo_cap) },
        { name = local.worker_contract.env.launcher.deadline_clamp, value = tostring(local.worker_contract.timing.deadline_clamp_seconds) },
        { name = local.worker_contract.env.launcher.allowed_uid, value = tostring(local.worker_contract.ids.supervisor_uid) },
        { name = local.worker_contract.env.launcher.relay_url, value = "http://${local.pod_backend_relay_host}:${local.worker_contract.ports.relay}" },
        { name = local.worker_contract.env.launcher.classes, value = local.worker_contract.classes.default },
        { name = local.worker_contract.env.launcher.api_public_host, value = azurerm_kubernetes_cluster.this.fqdn },
        { name = local.worker_contract.env.launcher.manager_address, value = local.k8s_app_service_fqdn },
        # The metrics listener has its own Service (factory-metrics, monitoring.tf); the canary's 9464 probe targets it, because
        # the Service of the application port exposes only 8080 and a probe of 9464 there can never fail (issue #232).
        { name = local.worker_contract.env.launcher.manager_metrics_address, value = local.k8s_metrics_service_fqdn },
        { name = local.worker_contract.env.launcher.socket, value = local.worker_contract.paths.launcher_socket },
        # The only secret of the launcher: the Anthropic key it writes into the per-session Secrets.
        {
          name = local.kv_secret_objects.anthropic_api_key.k8s_secret_key
          valueFrom = {
            secretKeyRef = {
              name = local.k8s_synced_secret
              key  = local.kv_secret_objects.anthropic_api_key.k8s_secret_key
            }
          }
        },
        # Second layer next to the agent ConfigMap: Container Insights does not copy this container's environment.
        { name = "AZMON_COLLECT_ENV", value = "False" },
      ]
      resources = {
        requests = local.pod_backend_sizing.launcher
        limits   = local.pod_backend_sizing.launcher
      }
      securityContext = merge(local.pod_backend_sidecar_security, {
        runAsUser  = local.worker_contract.ids.launcher_uid
        runAsGroup = local.worker_contract.ids.shared_gid
      })
      volumeMounts = [
        { name = local.pod_backend_volume_names.socket, mountPath = local.worker_contract.paths.launcher_socket_dir },
        { name = local.pod_backend_volume_names.token, mountPath = local.pod_backend_token_mount_path, readOnly = true },
      ]
    },
    {
      name            = local.pod_backend_container_names.relay
      image           = local.image
      imagePullPolicy = "IfNotPresent"
      command         = [local.pod_backend_python, "-m", "factory.relay"]
      ports           = [{ name = "relay", containerPort = local.worker_contract.ports.relay, protocol = "TCP" }]
      env = [
        # Set explicitly (see the header): an explicit entry wins over a Service-injected FACTORY_RELAY_PORT.
        { name = local.worker_contract.env.relay.port, value = tostring(local.worker_contract.ports.relay) },
        { name = local.worker_contract.env.relay.spool_dir, value = local.worker_contract.paths.spool_dir },
        { name = "AZMON_COLLECT_ENV", value = "False" },
      ]
      resources = {
        requests = local.pod_backend_sizing.relay
        limits   = local.pod_backend_sizing.relay
      }
      securityContext = merge(local.pod_backend_sidecar_security, {
        runAsUser  = local.worker_contract.ids.relay_uid
        runAsGroup = local.worker_contract.ids.shared_gid
      })
      volumeMounts = [
        { name = local.pod_backend_volume_names.spool, mountPath = local.worker_contract.paths.spool_dir },
      ]
    },
  ]
  pod_backend_containers = [for item in local.pod_backend_containers_all : item if local.pod_backend_on]

  pod_backend_volumes_all = [
    {
      name     = local.pod_backend_volume_names.socket
      emptyDir = { sizeLimit = local.worker_contract.volumes.socket_dir_size_limit }
    },
    {
      name     = local.pod_backend_volume_names.spool
      emptyDir = { sizeLimit = local.worker_contract.volumes.spool_size_limit }
    },
    {
      # The launcher's Kubernetes credential and nothing else: a projected token (the API server's audience, rotated by the
      # kubelet; the client reads the file on every request) and the cluster CA. Mounted by the launcher container only.
      name = local.pod_backend_volume_names.token
      projected = {
        sources = [
          { serviceAccountToken = { path = "token", expirationSeconds = 3600 } },
          { configMap = { name = "kube-root-ca.crt", items = [{ key = "ca.crt", path = "ca.crt" }] } },
        ]
      }
    },
  ]
  pod_backend_volumes = [for item in local.pod_backend_volumes_all : item if local.pod_backend_on]
}

# The Service the worker pods reach the relay through. The worker's default-deny policy (worker_ns.tf) allows TCP 8090 to the pods
# that carry the manager's labels in the manager namespace; the Service selects those pods. ClusterIP only: no external address
# and no route in the ingress. Its name is not `factory-relay` on purpose (see the header).
resource "kubernetes_service_v1" "factory_relay" {
  count = local.worker_ns_count

  metadata {
    name      = local.pod_backend_relay_service_name
    namespace = kubernetes_namespace_v1.factory.metadata[0].name
    labels    = local.k8s_labels
  }

  spec {
    type     = "ClusterIP"
    selector = local.k8s_selector_labels

    port {
      name        = "relay"
      port        = local.worker_contract.ports.relay
      target_port = "relay"
      protocol    = "TCP"
    }
  }

  depends_on = [
    azurerm_role_assignment.aks_cluster_admin,
    azurerm_role_assignment.aks_cluster_user,
  ]
}

# Public ingress: Caddy in its own namespace behind an Azure Standard load balancer with a
# static public IP and a DNS label, so the manager is served at
# https://<label>.<location>.cloudapp.azure.com with a certificate that Caddy obtains from
# Let's Encrypt (HTTP-01 on port 80, TLS-ALPN-01 on 443) and renews on its own. No CRDs, no
# Helm, no ingress controller. With ingress_custom_host set, the one site block names both hosts
# (local.ingress_site_address) and Caddy obtains a certificate for each name; the Azure name keeps
# working. Behind a Cloudflare proxied record only HTTP-01 can succeed (README.md, "Custom host name").
#
# Caddy terminates TLS and proxies to the ClusterIP Service of the manager. It does NOT
# authenticate anyone: the manager validates the Entra bearer token itself (src/factory/
# entra_jwt.py). With monitoring on (monitoring.tf) the path /grafana/ goes to Grafana instead, which
# signs people in with Entra itself; the manager's routes are untouched. Caddy's two jobs on the request are
#   * pass the Authorization header through unchanged (reverse_proxy does by default), and
#   * delete any inbound X-MS-CLIENT-PRINCIPAL* header. The manager ignores those headers in
#     entra-jwt mode; removing them here is the second layer, so a forged Easy Auth header can
#     never reach the application.
#
# The Azure side (public IP, the role that lets the cluster use it) is in this file; the
# NSG rule for ports 80 and 443 is in network.tf (local.ingress_source_prefixes).

# --- Azure: public IP ---------------------------------------------------------------------------

# Static Standard public IP in the resource group (NOT the cluster's node resource group, so
# it survives a rebuild of the cluster and its address and DNS name stay stable). The DNS
# label gives <label>.<location>.cloudapp.azure.com; Caddy's certificate is for that name.
resource "azurerm_public_ip" "ingress" {
  name                = local.names.ingress_public_ip
  location            = var.location
  resource_group_name = data.azurerm_resource_group.this.name
  allocation_method   = "Static"
  sku                 = "Standard"
  domain_name_label   = var.ingress_dns_label
  tags                = var.tags
}

# The cloud controller of the cluster binds the Service to this address with the cluster's
# control-plane identity. For an address outside the node resource group that identity needs
# network rights on the resource group that holds it: reconciling a LoadBalancer Service LISTS
# the public IP addresses of that resource group (a role on the single address does not grant
# that list, and the Service would stay pending), so the role is at resource-group scope.
# "If you use an inbound or outbound public IP in a different resource group, grant the
# control plane identity the required permissions on that resource group."
# https://raw.githubusercontent.com/MicrosoftDocs/azure-aks-docs/main/articles/aks/static-ip.md
resource "azurerm_role_assignment" "cluster_ingress_ip" {
  scope                = data.azurerm_resource_group.this.id
  role_definition_name = "Network Contributor"
  principal_id         = azurerm_user_assigned_identity.cluster.principal_id
  principal_type       = "ServicePrincipal"
}

# --- Caddy configuration -------------------------------------------------------------------------

locals {
  # Let's Encrypt production, as the ONLY issuer (Caddy's default also tries ZeroSSL). HTTP/3
  # is switched off: the Service carries TCP only. `admin off` removes the admin API endpoint.
  # Written with join() and not a heredoc so check_refs.py can see every reference.
  caddyfile = join("\n", concat(
    [
      "{",
      "\tadmin off",
      "\tacme_ca https://acme-v02.api.letsencrypt.org/directory",
    ],
    local.ingress_acme_email == "" ? [] : ["\temail ${local.ingress_acme_email}"],
    [
      "\tservers {",
      "\t\tprotocols h1 h2",
      "\t}",
      "}",
      "",
      "${local.ingress_site_address} {",
      "\t# Easy Auth headers are never trusted; the manager validates the Authorization bearer token itself.",
      "\trequest_header -X-MS-CLIENT-PRINCIPAL",
      "\trequest_header -X-MS-CLIENT-PRINCIPAL-ID",
      "\trequest_header -X-MS-CLIENT-PRINCIPAL-NAME",
      "\trequest_header -X-MS-CLIENT-PRINCIPAL-IDP",
      "",
    ],
    local.caddy_routes,
    [
      "}",
      "",
    ],
  ))

  # The routes of the site. Monitoring off (the default): the one line it always had. Monitoring on: the
  # Grafana path first, then everything else to the manager. Grafana authenticates people itself (Entra,
  # OIDC) and never sees the manager's bearer token; no manager route starts with /grafana, and /dashboard
  # (the JSON endpoint) and /ui/ (the browser page and its sign-in
  # config, /ui/config.json) are left to the manager.
  caddy_routes = var.monitoring_enabled ? [
    "\t# Grafana: its own Entra sign-in. The Authorization header is not passed on, so a manager bearer token",
    "\t# that reaches this path goes no further than Caddy.",
    "\tredir /grafana /grafana/ permanent",
    "\thandle /grafana/* {",
    "\t\treverse_proxy ${local.k8s_grafana_service_fqdn}:${local.grafana_port} {",
    "\t\t\theader_up -Authorization",
    "\t\t\theader_up -X-Webauth-User",
    "\t\t}",
    "\t}",
    "",
    "\t# Everything else is the manager API: Authorization is forwarded unchanged (the default of reverse_proxy).",
    "\thandle {",
    "\t\treverse_proxy ${local.k8s_app_service_fqdn}:${local.app_port}",
    "\t}",
    ] : [
    "\t# Authorization is forwarded unchanged (the default of reverse_proxy).",
    "\treverse_proxy ${local.k8s_app_service_fqdn}:${local.app_port}",
  ]

  # The ingress runs on the system pool node: the factory node is sized for the manager.
  ingress_node_selector = {
    "kubernetes.azure.com/agentpool" = local.names.system_pool
  }

  ingress_uid = 65532

  # Source ranges for the load balancer Service: the same list as the NSG rule, without the
  # "Internet" placeholder. Empty means the field is left out (the whole internet).
  ingress_lb_source_ranges = [for prefix in local.ingress_source_prefixes : prefix if prefix != "Internet"]
}

# --- Kubernetes objects ---------------------------------------------------------------------------

resource "kubernetes_namespace_v1" "ingress" {
  metadata {
    name = local.k8s_ingress_namespace
    labels = merge(local.k8s_labels, {
      # The Caddy pod below meets the "restricted" Pod Security level: non-root, no
      # privilege escalation, all capabilities dropped, RuntimeDefault seccomp.
      "pod-security.kubernetes.io/enforce" = "restricted"
    })
  }

  depends_on = [
    azurerm_role_assignment.aks_cluster_admin,
    azurerm_role_assignment.aks_cluster_user,
  ]
}

resource "kubernetes_config_map_v1" "caddy" {
  metadata {
    name      = "${local.k8s_ingress_app_name}-config"
    namespace = kubernetes_namespace_v1.ingress.metadata[0].name
    labels    = local.k8s_labels
  }

  data = {
    Caddyfile = local.caddyfile
  }
}

# Certificates and the ACME account live on a small disk, so a restart does not ask Let's
# Encrypt for a new certificate each time (the CA rate-limits duplicates). Same storage class
# as /data (Retain). Pending until the pod is scheduled (WaitForFirstConsumer).
resource "kubernetes_persistent_volume_claim_v1" "caddy_data" {
  metadata {
    name      = "${local.k8s_ingress_app_name}-data"
    namespace = kubernetes_namespace_v1.ingress.metadata[0].name
    labels    = local.k8s_labels
  }

  spec {
    access_modes       = ["ReadWriteOnce"]
    storage_class_name = kubernetes_storage_class_v1.factory_disk.metadata[0].name

    resources {
      requests = {
        storage = "4Gi"
      }
    }
  }

  wait_until_bound = false
}

resource "kubernetes_deployment_v1" "caddy" {
  metadata {
    name      = local.k8s_ingress_app_name
    namespace = kubernetes_namespace_v1.ingress.metadata[0].name
    labels    = merge(local.k8s_labels, local.k8s_ingress_selector_labels)
  }

  spec {
    # One replica with Recreate: the certificate disk is ReadWriteOnce.
    replicas                  = 1
    revision_history_limit    = 3
    progress_deadline_seconds = 600

    strategy {
      type = "Recreate"
    }

    selector {
      match_labels = local.k8s_ingress_selector_labels
    }

    template {
      metadata {
        labels = merge(local.k8s_labels, local.k8s_ingress_selector_labels)
        annotations = {
          # The Caddyfile is read at start: a change rolls the pod.
          "checksum/caddyfile" = sha256(local.caddyfile)
        }
      }

      spec {
        automount_service_account_token = false
        enable_service_links            = false
        node_selector                   = local.ingress_node_selector

        security_context {
          run_as_non_root = true
          run_as_user     = local.ingress_uid
          run_as_group    = local.ingress_uid
          fs_group        = local.ingress_uid

          seccomp_profile {
            type = "RuntimeDefault"
          }

          # Lets the non-root process bind ports 80 and 443 without any capability. It is on
          # the list of "safe" sysctls (namespaced, no effect on other pods).
          sysctl {
            name  = "net.ipv4.ip_unprivileged_port_start"
            value = "0"
          }
        }

        # The official image ships /usr/bin/caddy with the file capability cap_net_bind_service+ep.
        # With every capability dropped and allowPrivilegeEscalation=false the kernel refuses to
        # exec such a binary ("exec /usr/bin/caddy: operation not permitted", observed on a real
        # cluster). Low ports are already allowed by the pod sysctl, so the capability is not
        # needed: this init container copies the binary (cp does not copy the security.capability
        # attribute) into a volume and the main container runs the copy.
        init_container {
          name              = "caddy-binary"
          image             = local.ingress_image
          image_pull_policy = "IfNotPresent"
          command           = ["cp", "/usr/bin/caddy", "/opt/caddy-bin/caddy"]

          resources {
            requests = {
              cpu    = "10m"
              memory = "16Mi"
            }
            limits = {
              cpu    = "100m"
              memory = "64Mi"
            }
          }

          security_context {
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            run_as_non_root            = true
            run_as_user                = local.ingress_uid
            run_as_group               = local.ingress_uid

            capabilities {
              drop = ["ALL"]
            }
          }

          volume_mount {
            name       = "caddy-bin"
            mount_path = "/opt/caddy-bin"
          }
        }

        container {
          name              = local.k8s_ingress_app_name
          image             = local.ingress_image
          image_pull_policy = "IfNotPresent"
          command           = ["/opt/caddy-bin/caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]

          port {
            name           = "http"
            container_port = 80
            protocol       = "TCP"
          }
          port {
            name           = "https"
            container_port = 443
            protocol       = "TCP"
          }

          # Caddy keeps certificates under $XDG_DATA_HOME and its autosaved configuration under
          # $XDG_CONFIG_HOME; both are mounted writable, everything else is read-only.
          env {
            name  = "XDG_DATA_HOME"
            value = "/data"
          }
          env {
            name  = "XDG_CONFIG_HOME"
            value = "/config"
          }

          resources {
            requests = {
              cpu    = "100m"
              memory = "128Mi"
            }
            limits = {
              cpu    = "1"
              memory = "256Mi"
            }
          }

          # The typed resource sends runAsNonRoot=false for the container unless it is set
          # here, and the pod-level true is then overridden (Pod Security "restricted" refuses it).
          security_context {
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            run_as_non_root            = true
            run_as_user                = local.ingress_uid
            run_as_group               = local.ingress_uid

            capabilities {
              drop = ["ALL"]
            }
          }

          volume_mount {
            name       = "caddyfile"
            mount_path = "/etc/caddy"
            read_only  = true
          }
          volume_mount {
            name       = "data"
            mount_path = "/data"
          }
          volume_mount {
            name       = "config"
            mount_path = "/config"
          }
          volume_mount {
            name       = "caddy-bin"
            mount_path = "/opt/caddy-bin"
            read_only  = true
          }

          # Caddy listens on 80 whether or not it holds a certificate yet.
          readiness_probe {
            tcp_socket {
              port = "http"
            }
            initial_delay_seconds = 3
            period_seconds        = 10
          }
          liveness_probe {
            tcp_socket {
              port = "http"
            }
            initial_delay_seconds = 20
            period_seconds        = 30
            failure_threshold     = 3
          }
        }

        volume {
          name = "caddyfile"
          config_map {
            name = kubernetes_config_map_v1.caddy.metadata[0].name
          }
        }
        volume {
          name = "data"
          persistent_volume_claim {
            claim_name = kubernetes_persistent_volume_claim_v1.caddy_data.metadata[0].name
          }
        }
        volume {
          name = "caddy-bin"
          empty_dir {
            size_limit = "128Mi"
          }
        }
        volume {
          name = "config"
          empty_dir {
            size_limit = "16Mi"
          }
        }
      }
    }
  }

  timeouts {
    create = "15m"
    update = "15m"
  }
}

# The public entrance: a Standard load balancer rule per port, bound to the static address.
# externalTrafficPolicy Local keeps the client address (Caddy's log, the source-range check) and
# makes the load balancer probe the node that actually runs Caddy.
# https://raw.githubusercontent.com/MicrosoftDocs/azure-aks-docs/main/articles/aks/static-ip.md
resource "kubernetes_service_v1" "caddy" {
  metadata {
    name      = local.k8s_ingress_app_name
    namespace = kubernetes_namespace_v1.ingress.metadata[0].name
    labels    = local.k8s_labels
    annotations = {
      "service.beta.kubernetes.io/azure-load-balancer-resource-group" = data.azurerm_resource_group.this.name
      "service.beta.kubernetes.io/azure-pip-name"                     = azurerm_public_ip.ingress.name
    }
  }

  spec {
    type                    = "LoadBalancer"
    external_traffic_policy = "Local"
    load_balancer_ip        = azurerm_public_ip.ingress.ip_address
    # Optional hardening (variable ingress_allowed_cidrs): enforced here on the node and in
    # the subnet NSG (network.tf). Null leaves the field out.
    load_balancer_source_ranges = length(local.ingress_lb_source_ranges) == 0 ? null : local.ingress_lb_source_ranges
    selector                    = local.k8s_ingress_selector_labels

    port {
      name        = "http"
      port        = 80
      target_port = "http"
      protocol    = "TCP"
    }
    port {
      name        = "https"
      port        = 443
      target_port = "https"
      protocol    = "TCP"
    }
  }

  wait_for_load_balancer = true

  timeouts {
    create = "15m"
  }

  depends_on = [
    azurerm_role_assignment.cluster_ingress_ip,
  ]
}

# Input variables.
#
# Every value that identifies the operator's environment (IDs, names, versions,
# digests, GitHub App identifiers) is a variable WITHOUT a default, so a plan fails
# loudly instead of using an invented value. Defaults exist only for sizing and for
# network ranges. Copy terraform.tfvars.example to a file you keep private and
# pass it with -var-file; Terraform does not auto-load the example.

# ---------------------------------------------------------------------------
# Azure scope
# ---------------------------------------------------------------------------

variable "subscription_id" {
  description = "Azure subscription that holds every resource."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", var.subscription_id))
    error_message = "subscription_id must be a GUID."
  }
}

variable "tenant_id" {
  description = "Microsoft Entra tenant ID. Also used by the Key Vault, by the cluster's Entra integration and by the manager to validate bearer tokens (issuer and tenant)."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", var.tenant_id))
    error_message = "tenant_id must be a GUID."
  }
}

variable "location" {
  description = "Azure region in its normalized form (lowercase, no spaces), for example a value from `az account list-locations --query [].name`. Must match the resource group's region."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{2,29}$", var.location))
    error_message = "location must be a normalized region name: lowercase letters and digits, no spaces."
  }
}

variable "resource_group_name" {
  description = "Existing resource group that receives every resource. Terraform does not create or delete it, so the deploying identity needs no subscription-scope role."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9._()-]{1,89}[A-Za-z0-9_()-]$", var.resource_group_name))
    error_message = "resource_group_name must be 1-90 characters of letters, digits, '.', '_', '-', '(' and ')', and must not end with '.'."
  }
}

variable "name_prefix" {
  description = "Short prefix for the names of the resources Terraform creates. Lowercase letters, digits and hyphens, 2-12 characters, starting with a letter. Storage account names also embed a hash of the subscription and resource group so the prefix need not be globally unique."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,10}[a-z0-9]$", var.name_prefix)) && !strcontains(var.name_prefix, "--")
    error_message = "name_prefix must be 2-12 characters: lowercase letters, digits and single hyphens, starting with a letter and ending with a letter or digit."
  }
}

variable "tags" {
  description = "Tags applied to every resource that supports them. Pass {} for none."
  type        = map(string)
}

# ---------------------------------------------------------------------------
# Container registry (operator-owned, not created here)
# ---------------------------------------------------------------------------

variable "acr_name" {
  description = "Name of the existing Azure Container Registry that holds the Factory image. Terraform only looks it up and grants AcrPull on it to the cluster's kubelet identity."
  type        = string

  validation {
    condition     = can(regex("^[a-zA-Z0-9]{5,50}$", var.acr_name))
    error_message = "acr_name must be 5-50 letters and digits."
  }
}

variable "acr_resource_group_name" {
  description = "Resource group of the registry named by acr_name (it may differ from resource_group_name)."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9._()-]{1,89}[A-Za-z0-9_()-]$", var.acr_resource_group_name))
    error_message = "acr_resource_group_name must be a valid resource group name."
  }
}

variable "image_repository" {
  description = "Repository path of the Factory image inside the registry, without registry host, tag or digest (for example `team/factory`)."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]+([._-][a-z0-9]+)*(/[a-z0-9]+([._-][a-z0-9]+)*)*$", var.image_repository))
    error_message = "image_repository must be a lowercase OCI repository path such as `team/factory`."
  }
}

variable "image_digest" {
  description = "Image digest to run, `sha256:` followed by 64 lowercase hex characters. Tags are refused on purpose: promotion is a pull request that changes this value. See README.md."
  type        = string

  validation {
    condition     = can(regex("^sha256:[0-9a-f]{64}$", var.image_digest))
    error_message = "image_digest must be sha256:<64 lowercase hex characters>. Tags are not accepted."
  }
}

# ---------------------------------------------------------------------------
# Key Vault
# ---------------------------------------------------------------------------

variable "key_vault_name" {
  description = "Globally unique name of the Key Vault Terraform creates (3-24 characters)."
  type        = string

  validation {
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9-]{1,22}[a-zA-Z0-9]$", var.key_vault_name)) && !strcontains(var.key_vault_name, "--")
    error_message = "key_vault_name must be 3-24 characters: letters, digits and single hyphens, starting with a letter and ending with a letter or digit."
  }
}

variable "anthropic_api_key_secret_version" {
  description = "Version ID of the Key Vault secret `anthropic-api-key` that the app uses (32 hex characters, shown by `az keyvault secret show --query id`). The SecretProviderClass is pinned to this version: a new secret version reaches the pod only when this value changes and the Deployment is rolled out, never in the middle of running work."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-f]{32}$", var.anthropic_api_key_secret_version))
    error_message = "anthropic_api_key_secret_version must be a 32-character lowercase hex Key Vault secret version."
  }
}

variable "github_app_private_key_secret_version" {
  description = "Version ID of the Key Vault secret `github-app-private-key` that the app uses (32 hex characters). Pinned for the same reason as the Anthropic key."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-f]{32}$", var.github_app_private_key_secret_version))
    error_message = "github_app_private_key_secret_version must be a 32-character lowercase hex Key Vault secret version."
  }
}

variable "key_vault_ip_rules" {
  description = "Public IPv4 addresses or CIDR ranges allowed through the Key Vault firewall, only so the operator can upload the two secrets from outside the VNet. Leave empty and remove entries after the upload."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for rule in var.key_vault_ip_rules : can(regex("^([0-9]{1,3}\\.){3}[0-9]{1,3}(/[0-9]{1,2})?$", rule))])
    error_message = "key_vault_ip_rules entries must be IPv4 addresses or CIDR ranges."
  }
}

variable "key_vault_secrets_officer_principal_ids" {
  description = "Object IDs of the people or groups that get `Key Vault Secrets Officer` on the vault so they can upload the two secrets. Optional: you can also assign the role by hand."
  type        = set(string)
  default     = []

  validation {
    condition     = alltrue([for id in var.key_vault_secrets_officer_principal_ids : can(regex("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", id))])
    error_message = "key_vault_secrets_officer_principal_ids must be Entra object IDs (GUIDs)."
  }
}

# ---------------------------------------------------------------------------
# Entra ID (bearer token validation) and GitHub App identifiers
# ---------------------------------------------------------------------------

variable "entra_allowed_audiences" {
  description = "Token audiences the manager accepts in the `aud` claim (FACTORY_ENTRA_AUDIENCES), for example `api://<client id>` and the bare client ID of the Entra app registration that protects the manager API. The registration carries the app roles Factory.Operator and Factory.Viewer, issues v2 access tokens (accessTokenAcceptedVersion = 2) and must match this list."
  type        = list(string)

  validation {
    condition     = length(var.entra_allowed_audiences) > 0 && alltrue([for audience in var.entra_allowed_audiences : can(regex("^(api://[A-Za-z0-9._~:/-]+|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$", audience))])
    error_message = "entra_allowed_audiences needs at least one entry, each an api:// Application ID URI or a client ID GUID."
  }
}

variable "entra_spa_client_id" {
  description = "Optional. Client ID of the Entra app registration the dashboard page signs in with (FACTORY_ENTRA_SPA_CLIENT_ID). Null derives it from entra_allowed_audiences (the first client ID GUID or api://<GUID>), which is right when the page signs in with the registration that protects the API, as README.md describes. Set it only when the audiences name an Application ID URI that is not api://<client id>."
  type        = string
  default     = null

  validation {
    condition     = var.entra_spa_client_id == null || can(regex("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", coalesce(var.entra_spa_client_id, "x")))
    error_message = "entra_spa_client_id must be a client ID GUID or null."
  }
}

variable "entra_spa_scope" {
  description = "Optional. The one scope the dashboard page asks for (FACTORY_ENTRA_SPA_SCOPE). Null derives `<client id>/.default` from entra_allowed_audiences (the GUID form, which Entra requires when an app requests a token for itself, AADSTS90009), which puts the app roles in the roles claim without exposing a scope. Set it only for a scope you exposed yourself, for example api://<client id>/access_as_user."
  type        = string
  default     = null

  validation {
    condition     = var.entra_spa_scope == null || can(regex("^[A-Za-z0-9][A-Za-z0-9._~:/-]{0,199}$", coalesce(var.entra_spa_scope, "x")))
    error_message = "entra_spa_scope must be one scope (no spaces), such as api://<client id>/.default, or null."
  }
}

variable "github_app_id" {
  description = "Numeric ID of the org-owned GitHub App (FACTORY_GITHUB_APP_ID)."
  type        = string

  validation {
    condition     = can(regex("^[1-9][0-9]{0,18}$", var.github_app_id))
    error_message = "github_app_id must be a positive integer."
  }
}

variable "github_app_slug" {
  description = "Slug of the GitHub App (FACTORY_GITHUB_APP_SLUG); commits and PRs are authored as `<slug>[bot]`."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9-]{0,99}$", var.github_app_slug))
    error_message = "github_app_slug must be letters, digits and hyphens."
  }
}

variable "github_app_user_id" {
  description = "Numeric user ID of the App's bot account (FACTORY_GITHUB_APP_USER_ID), used for the commit author e-mail."
  type        = string

  validation {
    condition     = can(regex("^[1-9][0-9]{0,18}$", var.github_app_user_id))
    error_message = "github_app_user_id must be a positive integer."
  }
}

variable "token_broker_command" {
  description = "Optional command line for FACTORY_TOKEN_BROKER (the token broker venv and script). Leave null when the image already sets it in its ENV, which is the intended setup."
  type        = string
  default     = null
}

# ---------------------------------------------------------------------------
# Ingress (Caddy behind a public Azure load balancer; ingress.tf)
# ---------------------------------------------------------------------------

variable "ingress_dns_label" {
  description = "DNS label of the ingress public IP: the manager is served at <label>.<location>.cloudapp.azure.com with a Let's Encrypt certificate. Unique per region across Azure; lowercase letters, digits and hyphens."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,61}[a-z0-9]$", var.ingress_dns_label))
    error_message = "ingress_dns_label must be 3-63 characters: lowercase letters, digits and hyphens, starting with a letter and ending with a letter or digit."
  }
}

variable "ingress_custom_host" {
  description = "Optional host name of your own (for example factory.example.org) that serves the same API, dashboard (/ui/) and, with monitoring, Grafana as the Azure name above. Null (the default) changes nothing. When set, Caddy obtains a Let's Encrypt certificate for it as well as for the Azure name (both keep working), Grafana's root_url moves to it, and the outputs list the redirect URIs to add to the Entra registrations. Create the DNS record first (infra/README.md, \"Browser sign-in and custom host name\"): a CNAME to <label>.<location>.cloudapp.azure.com, or an A record to ingress_public_ip. Lowercase, no wildcard, not an azure name."
  type        = string
  default     = null

  validation {
    condition     = var.ingress_custom_host == null || can(regex("^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?$", coalesce(var.ingress_custom_host, "x")))
    error_message = "ingress_custom_host must be a lowercase DNS host name with at least one dot (for example factory.example.org), or null. Wildcards and ports are not accepted."
  }

  validation {
    condition     = var.ingress_custom_host == null || !can(regex("\\.cloudapp\\.azure\\.com$", coalesce(var.ingress_custom_host, "x")))
    error_message = "ingress_custom_host is for a name of your own: the Azure name is served already (ingress_dns_label)."
  }
}

variable "caddy_image_digest" {
  description = "Digest of the official Caddy image to run as the ingress, `sha256:` followed by 64 lowercase hex characters. Tags are refused on purpose, the same as image_digest; an update is a pull request that changes this value."
  type        = string

  validation {
    condition     = can(regex("^sha256:[0-9a-f]{64}$", var.caddy_image_digest))
    error_message = "caddy_image_digest must be sha256:<64 lowercase hex characters>. Tags are not accepted."
  }
}

variable "caddy_image_repository" {
  description = "Registry path of the Caddy image without tag or digest. The default is the official image on Docker Hub, which the cluster reaches through the NAT gateway (anonymous pulls share the NAT address's rate limit); point it at a copy in your own registry to avoid that, and grant the cluster AcrPull there."
  type        = string
  default     = "docker.io/library/caddy"

  validation {
    condition     = can(regex("^[a-z0-9.-]+(:[0-9]+)?(/[a-z0-9]+([._-][a-z0-9]+)*)+$", var.caddy_image_repository))
    error_message = "caddy_image_repository must be a lowercase registry path such as docker.io/library/caddy."
  }
}

variable "acme_email" {
  description = "Optional contact address registered with Let's Encrypt for certificate expiry notices. Null registers none."
  type        = string
  default     = null

  validation {
    condition     = var.acme_email == null || can(regex("^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}$", coalesce(var.acme_email, "x")))
    error_message = "acme_email must be an e-mail address or null."
  }
}

variable "ingress_allowed_cidrs" {
  description = "Optional extra hardening: client CIDR ranges allowed to reach the ingress (the load balancer Service's source ranges and the node subnet NSG). Empty means the internet. The bearer token check in the manager is the real gate. Let's Encrypt validates from addresses that are not published, so a restricted list stops certificate issuance and renewal for an address Caddy has no certificate for yet (README.md)."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for cidr in var.ingress_allowed_cidrs : can(regex("^([0-9]{1,3}\\.){3}[0-9]{1,3}/[0-9]{1,2}$", cidr)) && can(cidrhost(cidr, 0))])
    error_message = "ingress_allowed_cidrs entries must be valid IPv4 CIDR ranges."
  }
}

# ---------------------------------------------------------------------------
# Per-repository configuration
# ---------------------------------------------------------------------------

variable "repo_configs" {
  description = "Map of `<org>/<name>` to the full TOML text of that repository's Factory config. Each entry is mounted at /config/repos/<org>__<name>.toml from a ConfigMap. The files hold no secrets (the GitHub App key and the Anthropic key come from Key Vault)."
  type        = map(string)

  validation {
    condition     = length(var.repo_configs) > 0
    error_message = "repo_configs needs at least one repository: with none, the manager has nothing to supervise."
  }

  validation {
    condition = alltrue([
      for repo in keys(var.repo_configs) :
      can(regex("^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+$", repo))
      && !strcontains(repo, "..")
      && !strcontains(split("/", repo)[0], "__")
      && !endswith(split("/", repo)[0], "_")
      && try(split("/", repo)[1], ".") != "."
      && length(repo) <= 200
    ])
    error_message = "repo_configs keys must be `org/name` as accepted by src/factory/config.py check_repo: no '..', no '__' or trailing '_' in the owner."
  }

  validation {
    condition     = alltrue([for text in values(var.repo_configs) : length(trimspace(text)) > 0])
    error_message = "repo_configs values must be non-empty TOML text."
  }

  # The image sets FACTORY_REQUIRE_WORKER_IDENTITY=1, so a config without the worker ids
  # would make every agent call fail at run time. Catch it at plan time.
  validation {
    condition = alltrue([
      for text in values(var.repo_configs) :
      can(regex("(?m)^\\s*\\[worker\\]", text))
      && can(regex("(?m)^\\s*uid\\s*=\\s*10002\\s*(#.*)?$", text))
      && can(regex("(?m)^\\s*gid\\s*=\\s*10010\\s*(#.*)?$", text))
    ])
    error_message = "every repo_configs entry needs the image's worker identity: a [worker] table with uid = 10002 and gid = 10010 (the ids of the `worker` user and `factory-work` group in the Dockerfile)."
  }
}

# ---------------------------------------------------------------------------
# AKS cluster
# ---------------------------------------------------------------------------

variable "aks_sku_tier" {
  description = "AKS control plane tier: Free (no uptime SLA, enough for one workload) or Standard (financially backed SLA)."
  type        = string
  default     = "Free"

  validation {
    condition     = contains(["Free", "Standard"], var.aks_sku_tier)
    error_message = "aks_sku_tier must be Free or Standard."
  }
}

variable "aks_kubernetes_version" {
  description = "Kubernetes version of the cluster, `1.<minor>` (the latest patch of that minor) or `1.<minor>.<patch>`. Null takes the latest version AKS recommends at creation. The version is never upgraded automatically: raise it here, deliberately. The pod needs user namespaces (spec.hostUsers=false), which are on by default from 1.33; the pod's sandbox settings were exercised on 1.36."
  type        = string
  default     = null

  validation {
    condition     = var.aks_kubernetes_version == null || try(can(regex("^1\\.[0-9]{2}(\\.[0-9]+)?$", var.aks_kubernetes_version)) && tonumber(split(".", var.aks_kubernetes_version)[1]) >= 33, false)
    error_message = "aks_kubernetes_version must be null or 1.<minor>[.<patch>] with a minor version of 33 or higher (pod user namespaces)."
  }
}

variable "aks_node_os_upgrade_channel" {
  description = "How node OS images are upgraded. NodeImage (the AKS default) reimages the nodes weekly and so drains the only user node, which interrupts running work; None leaves every node image upgrade to you; Unmanaged and SecurityPatch patch in place."
  type        = string
  default     = "NodeImage"

  validation {
    condition     = contains(["None", "Unmanaged", "SecurityPatch", "NodeImage"], var.aks_node_os_upgrade_channel)
    error_message = "aks_node_os_upgrade_channel must be None, Unmanaged, SecurityPatch or NodeImage."
  }
}

variable "aks_system_pool_vm_size" {
  description = "VM size of the single system node (cluster add-ons and the ingress, whose certificate disk is Premium_LRS). AKS needs at least 2 vCPU and 4 GiB for a system pool. The name must end in `s` before the version (Premium storage capable, such as Standard_D2ds_v5): a size without it cannot attach the Premium disk."
  type        = string
  default     = "Standard_D2ds_v5"

  validation {
    condition     = can(regex("^Standard_[A-Za-z][A-Za-z0-9]*s_v[0-9]+$", var.aks_system_pool_vm_size))
    error_message = "aks_system_pool_vm_size must look like Standard_D2ds_v5 (a Premium storage capable size: the name ends in s before the version)."
  }
}

variable "aks_user_pool_vm_size" {
  description = "VM size of the single node of the `factory` pool, which runs the manager pod. Only D-series (4 GiB per vCPU) and E-series (8 GiB per vCPU) sizes with a ds, as, ads or s suffix are accepted: container_cpu and container_memory are checked against the size, and the suffix with an `s` is what makes the size Premium storage capable, which the Premium_LRS data disk requires."
  type        = string
  default     = "Standard_D8ds_v5"

  validation {
    condition     = can(regex("^Standard_[DE][0-9]+(ds|as|ads|s)_v[0-9]+$", var.aks_user_pool_vm_size))
    error_message = "aks_user_pool_vm_size must be a Premium storage capable D- or E-series size such as Standard_D8ds_v5 (suffix ds, as, ads or s)."
  }
}

variable "aks_api_authorized_ip_ranges" {
  description = "IPv4 CIDR ranges allowed to reach the Kubernetes API server, for example the operators' egress addresses and the runners that apply this configuration. The NAT gateway address is added automatically (the nodes need it). Empty means no restriction: the API server is then protected by Entra ID and Azure RBAC alone. GitHub-hosted runners have no stable address, so a non-empty list needs a runner whose egress you control."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for cidr in var.aks_api_authorized_ip_ranges : can(regex("^([0-9]{1,3}\\.){3}[0-9]{1,3}/[0-9]{1,2}$", cidr)) && can(cidrhost(cidr, 0)) && !endswith(cidr, "/0")])
    error_message = "aks_api_authorized_ip_ranges entries must be IPv4 CIDR ranges other than /0."
  }
}

variable "aks_cluster_admin_principal_ids" {
  description = "Object IDs of the people, groups or service principals that get `Azure Kubernetes Service RBAC Cluster Admin` on the cluster. It MUST contain the object ID of the identity that applies this configuration (the identity of your pipeline's apply step), otherwise the Kubernetes provider cannot create the in-cluster objects; the plan identity is deliberately not listed. Terraform cannot check that, so README.md Bootstrap step 4 requires it. The set is never extended with the caller, so every plan has the same role assignment addresses."
  type        = set(string)

  validation {
    condition     = length(var.aks_cluster_admin_principal_ids) > 0
    error_message = "aks_cluster_admin_principal_ids must not be empty: list at least the object ID of the identity that applies this configuration."
  }

  validation {
    condition     = alltrue([for id in var.aks_cluster_admin_principal_ids : can(regex("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", id))])
    error_message = "aks_cluster_admin_principal_ids must be Entra object IDs (GUIDs)."
  }
}

variable "aks_pod_cidr" {
  description = "Pod address range of the Azure CNI Overlay network. It is private to the cluster, so it must not overlap the VNet or any network connected to it, nor 169.254.0.0/16, 172.30.0.0/16, 172.31.0.0/16 or 192.0.2.0/24."
  type        = string
  default     = "10.244.0.0/16"

  validation {
    condition     = can(regex("^([0-9]{1,3}\\.){3}[0-9]{1,3}/[0-9]{1,2}$", var.aks_pod_cidr)) && can(cidrhost(var.aks_pod_cidr, 0))
    error_message = "aks_pod_cidr must be a valid IPv4 CIDR."
  }

  validation {
    condition     = try(tonumber(split("/", var.aks_pod_cidr)[1]) <= 24, false)
    error_message = "aks_pod_cidr must be a /24 or larger range."
  }

  validation {
    # Two CIDR ranges overlap exactly when one contains the other's network address. Terraform
    # has no cidrcontains (that is OpenTofu): masking an address with a prefix length through
    # cidrhost("<address>/<length>", 0) gives the network address of that prefix.
    condition = try(alltrue([
      for cidr in var.vnet_address_space :
      cidrhost("${cidrhost(var.aks_pod_cidr, 0)}/${split("/", cidr)[1]}", 0) != cidrhost(cidr, 0)
      && cidrhost("${cidrhost(cidr, 0)}/${split("/", var.aks_pod_cidr)[1]}", 0) != cidrhost(var.aks_pod_cidr, 0)
    ]), false)
    error_message = "aks_pod_cidr must not overlap vnet_address_space."
  }
}

variable "aks_service_cidr" {
  description = "Kubernetes service address range (cluster IPs). Same overlap rules as aks_pod_cidr; it must also not overlap the pod range. The DNS service address is its tenth address."
  type        = string
  default     = "10.0.0.0/16"

  validation {
    condition     = can(regex("^([0-9]{1,3}\\.){3}[0-9]{1,3}/[0-9]{1,2}$", var.aks_service_cidr)) && can(cidrhost(var.aks_service_cidr, 0))
    error_message = "aks_service_cidr must be a valid IPv4 CIDR."
  }

  validation {
    condition     = try(tonumber(split("/", var.aks_service_cidr)[1]) >= 13 && tonumber(split("/", var.aks_service_cidr)[1]) <= 27, false)
    error_message = "aks_service_cidr must be between /13 and /27."
  }

  validation {
    condition = try(alltrue([
      for cidr in concat(var.vnet_address_space, [var.aks_pod_cidr]) :
      cidrhost("${cidrhost(var.aks_service_cidr, 0)}/${split("/", cidr)[1]}", 0) != cidrhost(cidr, 0)
      && cidrhost("${cidrhost(cidr, 0)}/${split("/", var.aks_service_cidr)[1]}", 0) != cidrhost(var.aks_service_cidr, 0)
    ]), false)
    error_message = "aks_service_cidr must not overlap vnet_address_space or aks_pod_cidr."
  }
}

# ---------------------------------------------------------------------------
# Cilium, node auto-provisioning and the worker node pool (issue #144; ALL OFF by default)
#
# An unset variable leaves the plan exactly as it was: the arguments are omitted and every object
# below is count-gated. The Cilium and node auto-provisioning values are never set by a pull
# request: they live in INFRA_TFVARS (the operator's tfvars, which the operator's own pipeline passes
# to terraform with -var-file) and are set by the operator immediately before a manual
# `stage=cluster` apply (README.md, "Switching the data plane").
# ---------------------------------------------------------------------------

variable "network_data_plane" {
  description = "Data plane of the cluster network: `azure` or `cilium`. Null (the default) omits the argument, which is the Azure data plane today. Changing it from `azure` to `cilium` is an in-place update that REIMAGES EVERY NODE AT ONCE (provider documentation; the downtime is not measured), so it needs the drain gate, snapshots and a manual `stage=cluster` run (README.md, \"Switching the data plane\"). Once set to `cilium` it stays set: going back forces a new cluster, which `prevent_destroy` on the cluster refuses."
  type        = string
  default     = null

  validation {
    condition     = var.network_data_plane == null || contains(["azure", "cilium"], coalesce(var.network_data_plane, "azure"))
    error_message = "network_data_plane must be null, azure or cilium."
  }
}

variable "network_policy" {
  description = "Network policy of the cluster. Null (the default) omits the argument. Only `cilium` is accepted, and only together with network_data_plane = cilium (the provider's rule). Whether the Cilium data plane also needs this value for policies to be enforced is UNVERIFIED: try it on a scratch deployment before anything is set. The other provider values (`azure`, `calico`) are not offered: moving to them from the cluster's current state forces a new cluster, and Calico is not supported with node auto-provisioning."
  type        = string
  default     = null

  validation {
    condition     = var.network_policy == null || coalesce(var.network_policy, "none") == "cilium"
    error_message = "network_policy must be null or cilium."
  }

  validation {
    condition     = var.network_policy != "cilium" || var.network_data_plane == "cilium"
    error_message = "network_policy = cilium needs network_data_plane = cilium."
  }
}

variable "nap_mode" {
  description = "Node auto-provisioning mode of the cluster: `Auto` or `Manual`. Null (the default) omits the whole `node_provisioning_profile` block, which cannot change the cluster (the argument is optional and computed in azurerm 4.81). Switching it back needs an explicit `Manual` and a clean-up first (no NAP node left, every NodePool at limits.cpu 0): a deliberate manual procedure, not a variable edit."
  type        = string
  default     = null

  validation {
    condition     = var.nap_mode == null || contains(["Auto", "Manual"], coalesce(var.nap_mode, "Manual"))
    error_message = "nap_mode must be null, Auto or Manual."
  }
}

variable "nap_default_node_pools" {
  description = "Whether node auto-provisioning creates its own default NodePools (`Auto`) or none (`None`, the default here: Factory defines its own NodePool and AKSNodeClass, worker_pool.tf). Read only when nap_mode is set. Changing it from Auto to None later does not delete NodePools that exist."
  type        = string
  default     = "None"

  validation {
    condition     = contains(["Auto", "None"], var.nap_default_node_pools)
    error_message = "nap_default_node_pools must be Auto or None."
  }
}

variable "worker_nodepool_enabled" {
  description = "Creates the AKSNodeClass and NodePool objects of the worker pool (worker_pool.tf), as `kubernetes_manifest`: the custom resource definitions exist only after node auto-provisioning is on, and the plan reads them. Needs nap_mode = Auto, network_data_plane = cilium and family_vcpu_quota (checked as preconditions of both objects). Turning it off again DELETES the NodePool, a disruption that ignores `do-not-disrupt`: the cluster guard (tools/infra_cluster_guard.py) refuses that on the automatic path, and the apply must go through the drain gate. Nothing but the pool is created: no pod uses it until the pod backend exists."
  type        = bool
  default     = false

  validation {
    condition     = !var.worker_nodepool_enabled || var.family_vcpu_quota != null
    error_message = "worker_nodepool_enabled needs family_vcpu_quota: the vCPU quota of the VM family of worker_pool_sku."
  }
}

variable "worker_pool_limits_cpu" {
  description = "NodePool `limits.cpu`: the vCPUs the worker pool may hold in total, and the one number the concurrency ceiling derives from (`local.worker_pool` in worker_pool.tf: floor(limits_cpu / node vCPU) * worker_pool_pods_per_node). A whole multiple of the vCPUs of worker_pool_sku (32 is 8 nodes of 4). When the pool is enabled, limits_cpu + 2 * (vCPUs of the production pools in the same VM family) must not exceed family_vcpu_quota, so that node image and Kubernetes upgrades of the production pools can still surge. The production pools of the live configuration (system, factory and mon) are Standard_D2ds_v5, Standard_D8ds_v5 and Standard_D2ds_v5: 12 vCPUs of the Dds_v5 family, none of them in the family of the default worker size."
  type        = number
  default     = 32

  validation {
    condition     = var.worker_pool_limits_cpu >= 1 && floor(var.worker_pool_limits_cpu) == var.worker_pool_limits_cpu
    error_message = "worker_pool_limits_cpu must be a whole number of at least 1."
  }

  validation {
    condition     = try(var.worker_pool_limits_cpu % tonumber(regex("^Standard_[DE]([0-9]+)", var.worker_pool_sku)[0]) == 0, false)
    error_message = "worker_pool_limits_cpu must be a multiple of the vCPUs of worker_pool_sku, so that the NodePool limit is a whole number of nodes."
  }
}

variable "worker_pool_sku" {
  description = "VM size of every node of the worker pool. The default sizes the NAP user nodes from the Dsv6 or Dsv7 family, not Ddsv5, whose regional family quota is small in many subscriptions and whose Standard_D4ds_v5 can be NotAvailableForSubscription in a zone (check `az vm list-skus` for your region). The class sizes of the pod contract (2 vCPU, 4 GiB per `standard` session) assume one session per 4 vCPU node with 3.86 vCPU allocatable, which was measured on Standard_D4ds_v5 only: the allocatable CPU and memory of Standard_D4s_v6, and whether the NodePool sku-name requirement accepts it, are NOT measured, so measure the sandbox check (`factory-sandbox-check`) and the footprint on the chosen size before the pool is switched on. D- and E-series sizes only, like aks_user_pool_vm_size."
  type        = string
  default     = "Standard_D4s_v6"

  validation {
    condition     = can(regex("^Standard_[DE][0-9]+(ds|as|ads|s)_v[0-9]+$", var.worker_pool_sku))
    error_message = "worker_pool_sku must be a D- or E-series size such as Standard_D4s_v6 (suffix ds, as, ads or s)."
  }
}

variable "worker_pool_pods_per_node" {
  description = "Worker pods counted per node in the concurrency ceiling. 1 by default: one repository per node is a prerequisite of the pod backend, so N repositories need N nodes and the pool limit is reached at floor(limits_cpu / node vCPU) sessions of different repositories. A larger value is reachable only while one repository runs in pod mode; the operator then raises it. Not above the pods per node of the default resource class of the pod contract (checked as a precondition of the NodePool)."
  type        = number
  default     = 1

  validation {
    condition     = var.worker_pool_pods_per_node >= 1 && floor(var.worker_pool_pods_per_node) == var.worker_pool_pods_per_node
    error_message = "worker_pool_pods_per_node must be a whole number of at least 1."
  }
}

variable "family_vcpu_quota" {
  description = "Regional vCPU quota of the VM family of worker_pool_sku in this subscription, the `limit` of that family in `az vm list-usage --location <location>` (for the default Standard_D4s_v6 the family is `Standard Dsv6 Family`; for Standard_D4ds_v5 it is `Standard DDSv5 Family`, which the system, factory and monitoring pools also draw on). Null (the default) is accepted only while worker_nodepool_enabled is false; it is the input of the surge-reserve check of worker_pool_limits_cpu, which counts the family only: the regional vCPU total (`cores` in `az vm list-usage`, shared by every family) is NOT checked here, so the operator reads it too."
  type        = number
  default     = null

  validation {
    condition     = var.family_vcpu_quota == null || (coalesce(var.family_vcpu_quota, 1) >= 1 && floor(coalesce(var.family_vcpu_quota, 1)) == coalesce(var.family_vcpu_quota, 1))
    error_message = "family_vcpu_quota must be null or a whole number of at least 1."
  }
}

variable "pod_backend_enabled" {
  description = "Creates the pod backend of the cluster (issues #144, #145 and #147), all of it default off. The fence (worker_ns.tf): the namespace `factory-workers` (Pod Security privileged, the three relaxations of the worker pod need it), the service account `factory-worker` (no API token, no RoleBinding) and a default-deny NetworkPolicy that lets a worker pod reach only cluster DNS, the relay port of the manager pod and TCP 443 outside the cloud metadata, the pod, service and node ranges. With it on, the Container Insights agent ConfigMap also stops collecting the stdout and stderr of that namespace, and the prepared factory_network_policy (monitoring.tf) admits the relay port from it. The admission policies that pin what a worker pod and its Secret may say (worker_policy.tf, infra/policy/*.json) and their parameter ConfigMap. The Role and RoleBinding of the launcher (worker_rbac.tf, each waiting for the fence). And the manager pod itself (pod_backend.tf, k8s.tf): the launcher and relay containers, the init container that sets the owner of their two shared directories, the projected token that only the launcher mounts, the relay Service, the environment of the main container and a toleration of the manager taint, so that rolling the Deployment restarts the manager with the backend in it. Off (the default) creates nothing, leaves the plan without a line, the agent ConfigMap data and the Deployment byte-identical. Needs network_data_plane = cilium, nap_mode = Auto and worker_nodepool_enabled (checked as preconditions of the namespace): without an engine the policy is accepted and not enforced. With it on, the main container of the manager pod gets FACTORY_POD_BACKEND=on, but a repository's sessions still run inside the manager pod until its `repo_configs` entry sets execution = \"pod\" (the per-repository switch; removing the line undoes it). Turning it off again DELETES the namespace and every worker pod in it, a disruption that ignores `do-not-disrupt`: the cluster guard (tools/infra_cluster_guard.py) refuses that on the automatic path, and the apply must go through the drain gate."
  type        = bool
  default     = false
}

variable "manager_pool_taint_enabled" {
  description = "Taints the node pool `factory` of the manager pod with workload=manager:NoSchedule (the contract's manager taint, docs/pod-backend.md), so that nothing without the toleration is scheduled there: the manager pod (which already carries the toleration whenever pod_backend_enabled is on, so that this flip needs no Deployment change), every cluster add-on that tolerates all taints, and nothing else. Default false: the node_taints argument is omitted and the plan has no line for the pool. Needs pod_backend_enabled (checked here), so that the toleration is deployed before the taint. The provider documents `node_taints` of a node pool as forcing a new resource (UNVERIFIED for the pinned provider version): the flip may REPLACE the pool, which deletes its node (the manager pod is down until the new node is ready; the data disk is retained) and the cluster guard refuses it on the automatic path and on stage=all, and wants cluster_snapshots_taken=true on stage=cluster. Whether the add-on pods that the manager pod needs (the Secrets Store CSI driver and its Azure provider, the Azure Disk CSI driver) tolerate the taint on this cluster is UNVERIFIED: a manager that cannot mount its Key Vault volume is down. Try it on a scratch deployment first."
  type        = bool
  default     = false

  validation {
    condition     = !var.manager_pool_taint_enabled || var.pod_backend_enabled
    error_message = "manager_pool_taint_enabled needs pod_backend_enabled: the toleration of the manager pod ships with the pod backend and must exist before the taint does."
  }
}

# ---------------------------------------------------------------------------
# Sizing and network defaults
# ---------------------------------------------------------------------------

variable "container_cpu" {
  description = "vCPU request and limit of the Factory container. Exactly one replica runs (strategy Recreate), alone on the node of the `factory` pool next to the node's own add-on pods and the kubelet reservation, so the value may use at most 75 % of the node's vCPUs: 6 on a D8. Request equals limit, so the pod is Guaranteed."
  type        = number
  default     = 6

  validation {
    condition     = var.container_cpu >= 1 && var.container_cpu <= 64
    error_message = "container_cpu must be between 1 and 64."
  }

  # Terraform 1.9+ lets a validation read other variables. 75 % of the node is the margin
  # that leaves room for the node reservation and the DaemonSets (not measured on every node size).
  validation {
    condition     = try(var.container_cpu <= 0.75 * tonumber(regex("^Standard_[DE]([0-9]+)", var.aks_user_pool_vm_size)[0]), false)
    error_message = "container_cpu must fit the node of the factory pool: at most 75 % of the vCPUs of aks_user_pool_vm_size. Lower container_cpu or pick a larger aks_user_pool_vm_size."
  }
}

variable "container_memory" {
  description = "Memory request and limit of the Factory container, as `<n>Gi`. At most 75 % of the node's memory (D-series 4 GiB per vCPU, E-series 8 GiB per vCPU): 24Gi on a D8."
  type        = string
  default     = "24Gi"

  validation {
    condition     = can(regex("^[0-9]+(\\.[0-9]+)?Gi$", var.container_memory))
    error_message = "container_memory must look like 24Gi."
  }

  validation {
    condition = try(
      tonumber(trimsuffix(var.container_memory, "Gi")) >= 1 &&
      tonumber(trimsuffix(var.container_memory, "Gi")) <=
      0.75 * tonumber(regex("^Standard_[DE]([0-9]+)", var.aks_user_pool_vm_size)[0]) * (startswith(var.aks_user_pool_vm_size, "Standard_E") ? 8 : 4),
      false
    )
    error_message = "container_memory must be at least 1Gi and fit the node of the factory pool: at most 75 % of the memory of aks_user_pool_vm_size (D-series 4 GiB per vCPU, E-series 8 GiB per vCPU)."
  }
}

variable "data_disk_size_gib" {
  description = "Size of the Azure Disk (Premium_LRS, ext4) mounted at /data for run state and worktrees. It can be grown later (the storage class allows expansion) but never shrunk. Premium disks bill by tier: 128 is a P10."
  type        = number
  default     = 128

  validation {
    condition     = var.data_disk_size_gib >= 32 && var.data_disk_size_gib <= 32767 && floor(var.data_disk_size_gib) == var.data_disk_size_gib
    error_message = "data_disk_size_gib must be a whole number between 32 and 32767."
  }
}

variable "termination_grace_period_seconds" {
  description = "Seconds the container gets after SIGTERM before SIGKILL (terminationGracePeriodSeconds). Node drains for upgrades wait this long."
  type        = number
  default     = 600

  validation {
    condition     = var.termination_grace_period_seconds >= 30 && var.termination_grace_period_seconds <= 3600 && floor(var.termination_grace_period_seconds) == var.termination_grace_period_seconds
    error_message = "termination_grace_period_seconds must be a whole number between 30 and 3600."
  }
}

variable "log_retention_days" {
  description = "Retention of the Log Analytics tables Factory writes to (ContainerLogV2, KubeEvents, KubePodInventory, AZKVAuditLogs). 4 is the minimum the service accepts for a table; the workspace default stays at 30, its minimum (see observability.tf)."
  type        = number
  default     = 4

  validation {
    condition     = var.log_retention_days >= 4 && var.log_retention_days <= 730 && floor(var.log_retention_days) == var.log_retention_days
    error_message = "log_retention_days must be a whole number between 4 and 730."
  }
}

variable "logs_archive_retention_days" {
  description = "Days the redacted copies of a purged run's logs are kept in the logs-archive container before the manager's daily sweep removes them (FACTORY_LOGS_ARCHIVE_RETENTION_DAYS)."
  type        = number
  default     = 30

  validation {
    condition     = var.logs_archive_retention_days == floor(var.logs_archive_retention_days) && var.logs_archive_retention_days >= 1 && var.logs_archive_retention_days <= 3650
    error_message = "logs_archive_retention_days must be a whole number of days from 1 to 3650."
  }
}

variable "vnet_address_space" {
  description = "Address space of the VNet. It must not overlap 169.254.0.0/16, 172.30.0.0/16, 172.31.0.0/16 or 192.0.2.0/24, nor the pod and service ranges of the cluster (aks_pod_cidr, aks_service_cidr)."
  type        = list(string)
  default     = ["10.60.0.0/24"]

  validation {
    condition     = length(var.vnet_address_space) > 0 && alltrue([for cidr in var.vnet_address_space : can(cidrhost(cidr, 0))])
    error_message = "vnet_address_space must be a list of valid CIDR ranges."
  }
}

variable "node_subnet_prefix" {
  description = "CIDR of the subnet that holds the AKS nodes (one address per node, plus one for an upgrade surge and Azure's five reserved addresses). /27 or larger, inside vnet_address_space."
  type        = string
  default     = "10.60.0.0/26"

  validation {
    condition     = can(cidrhost(var.node_subnet_prefix, 0)) && tonumber(split("/", var.node_subnet_prefix)[1]) <= 27
    error_message = "node_subnet_prefix must be a valid CIDR of /27 or larger."
  }

  validation {
    # Inside means: not shorter than the range and, masked to its length, the same network.
    condition = try(anytrue([
      for cidr in var.vnet_address_space :
      tonumber(split("/", var.node_subnet_prefix)[1]) >= tonumber(split("/", cidr)[1])
      && cidrhost("${cidrhost(var.node_subnet_prefix, 0)}/${split("/", cidr)[1]}", 0) == cidrhost(cidr, 0)
    ]), false)
    error_message = "node_subnet_prefix must lie inside vnet_address_space."
  }
}

# ---------------------------------------------------------------------------
# Monitoring: Prometheus, Grafana and optionally Alertmanager (monitoring.tf)
# ---------------------------------------------------------------------------

variable "monitoring_enabled" {
  description = "Run Prometheus and Grafana in the cluster (namespace `monitoring`), scrape the manager's metrics listener and serve Grafana at https://<ingress host>/grafana/ with Entra sign-in. Off by default because it needs three Key Vault secrets and an Entra client that you create first: with it on, the Factory pod's SecretProviderClass gains the metrics token secret, so a missing secret version stops the pod from starting. README.md, \"Monitoring\", lists the order."
  type        = bool
  default     = false

  validation {
    condition = !var.monitoring_enabled || (
      var.metrics_token_secret_version != null &&
      var.grafana_oauth_client_id != null &&
      var.grafana_oauth_client_secret_version != null
    )
    error_message = "monitoring_enabled needs metrics_token_secret_version, grafana_oauth_client_id and grafana_oauth_client_secret_version."
  }
}

variable "metrics_token_secret_version" {
  description = "Version ID of the Key Vault secret `metrics-bearer-token` (32 hex characters): the bearer token of the manager's metrics listener, read by the manager (FACTORY_MANAGER_METRICS_TOKEN) and by Prometheus. At least 32 printable characters without spaces (`openssl rand -hex 32`). Required when monitoring_enabled."
  type        = string
  default     = null

  validation {
    condition     = var.metrics_token_secret_version == null || can(regex("^[0-9a-f]{32}$", var.metrics_token_secret_version))
    error_message = "metrics_token_secret_version must be a 32-character lowercase hex Key Vault secret version."
  }
}

variable "grafana_oauth_client_id" {
  description = "Client ID of the Entra app registration Grafana signs in with. Use the registration that protects the manager API (it already carries the app roles Factory.Operator and Factory.Viewer, which then arrive in the `roles` claim of the ID token), with the redirect URI from the output grafana_oauth_redirect_uri added. Required when monitoring_enabled."
  type        = string
  default     = null

  validation {
    condition     = var.grafana_oauth_client_id == null || can(regex("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", var.grafana_oauth_client_id))
    error_message = "grafana_oauth_client_id must be a client ID (GUID)."
  }
}

variable "grafana_oauth_client_secret_version" {
  description = "Version ID of the Key Vault secret `grafana-oauth-client-secret` (32 hex characters): a client secret of that app registration. Required when monitoring_enabled."
  type        = string
  default     = null

  validation {
    condition     = var.grafana_oauth_client_secret_version == null || can(regex("^[0-9a-f]{32}$", var.grafana_oauth_client_secret_version))
    error_message = "grafana_oauth_client_secret_version must be a 32-character lowercase hex Key Vault secret version."
  }
}

variable "grafana_operator_role" {
  description = "Grafana role of a person who holds the Entra app role Factory.Operator: Editor (the default) or Admin. Factory.Viewer is always Viewer, and a person with neither role cannot sign in."
  type        = string
  default     = "Editor"

  validation {
    condition     = contains(["Editor", "Admin"], var.grafana_operator_role)
    error_message = "grafana_operator_role must be Editor or Admin."
  }
}

variable "grafana_anonymous_viewer_enabled" {
  description = "Publishes Grafana read-only: anyone who can reach the ingress host opens https://<ingress host>/grafana/ without signing in, as a Viewer of the one organization, and lands on the Factory overview dashboard. Off by default; off, the rendered grafana.ini and so the Grafana pod are unchanged. Only has an effect with monitoring_enabled = true and is refused without it. Everything in the Grafana organization then becomes readable by anyone who can reach the host, not only the three provisioned dashboards: a dashboard an operator saves there, the alert rules and their state and, through Grafana's query API, every series Prometheus holds (repository names, spend, pod and node names). A visitor cannot change a dashboard, the data source or any setting (viewers_can_edit = false, the dashboards and the data source are provisioned as not editable, and a Viewer cannot use Explore or create snapshots); it can still create short URLs, which Grafana stores in its own database. Operators still sign in with Entra at /grafana/login. ingress_allowed_cidrs, when set, still limits who can reach the host at all. docs/metrics.md, \"Publishing the dashboards read-only\", lists what to review first and how to turn it off again."
  type        = bool
  default     = false

  validation {
    condition     = !var.grafana_anonymous_viewer_enabled || var.monitoring_enabled
    error_message = "grafana_anonymous_viewer_enabled needs monitoring_enabled: without Grafana there is nothing to publish."
  }
}

variable "alert_receiver" {
  description = "Where Alertmanager sends alerts: none (the default: no Alertmanager runs, alerts are visible in Prometheus and Grafana only), slack (an incoming webhook URL), msteams (a Teams workflow or connector webhook URL, Alertmanager's msteamsv2 receiver) or webhook (any HTTPS endpoint that accepts Alertmanager's webhook JSON). The URL is a secret: it lives in Key Vault (alert_receiver_secret_version), never here."
  type        = string
  default     = "none"

  validation {
    condition     = contains(["none", "slack", "msteams", "webhook"], var.alert_receiver)
    error_message = "alert_receiver must be none, slack, msteams or webhook."
  }

  validation {
    condition     = var.alert_receiver == "none" || (var.monitoring_enabled && var.alert_receiver_secret_version != null)
    error_message = "alert_receiver other than none needs monitoring_enabled and alert_receiver_secret_version."
  }
}

variable "alert_receiver_secret_version" {
  description = "Version ID of the Key Vault secret `alert-receiver-url` (32 hex characters) that holds the receiver's URL. Only used when alert_receiver is not none."
  type        = string
  default     = null

  validation {
    condition     = var.alert_receiver_secret_version == null || can(regex("^[0-9a-f]{32}$", var.alert_receiver_secret_version))
    error_message = "alert_receiver_secret_version must be a 32-character lowercase hex Key Vault secret version."
  }
}

variable "monitoring_node_pool_vm_size" {
  description = "Null (the default) runs the monitoring pods on the system node next to Caddy, with small requests (about 0.2 vCPU and 0.9 GiB). A VM size (for example Standard_D2ds_v5) adds a one-node pool `mon`, tainted workload=monitoring:NoSchedule, that only the monitoring pods use, so they can never compete with the Factory pod or the cluster add-ons. Measure first (README.md, Monitoring); a node costs real money. A Premium storage capable size is required (the Prometheus disk)."
  type        = string
  default     = null

  validation {
    condition     = var.monitoring_node_pool_vm_size == null || can(regex("^Standard_[A-Za-z][A-Za-z0-9]*s_v[0-9]+$", coalesce(var.monitoring_node_pool_vm_size, "x")))
    error_message = "monitoring_node_pool_vm_size must be null or look like Standard_D2ds_v5 (a Premium storage capable size)."
  }
}

variable "prometheus_retention" {
  description = "How long Prometheus keeps samples (--storage.tsdb.retention.time), for example 15d."
  type        = string
  default     = "15d"

  validation {
    condition     = can(regex("^[1-9][0-9]*[dwy]$", var.prometheus_retention))
    error_message = "prometheus_retention must look like 15d (a whole number of days, weeks or years)."
  }
}

variable "prometheus_storage_gib" {
  description = "Size of the Prometheus disk (Premium_LRS through the factory-disk class, `Retain`). Prometheus also stops at 80 percent of it (--storage.tsdb.retention.size), so a full disk drops the oldest samples instead of failing."
  type        = number
  default     = 8

  validation {
    condition     = var.prometheus_storage_gib >= 4 && var.prometheus_storage_gib <= 1024 && floor(var.prometheus_storage_gib) == var.prometheus_storage_gib
    error_message = "prometheus_storage_gib must be a whole number between 4 and 1024."
  }
}

variable "prometheus_scrape_interval" {
  description = "Scrape and rule evaluation interval. The manager caches its ledger aggregates for 30 seconds, so a shorter interval buys nothing."
  type        = string
  default     = "30s"

  validation {
    condition     = can(regex("^[1-9][0-9]*[sm]$", var.prometheus_scrape_interval))
    error_message = "prometheus_scrape_interval must look like 30s or 1m."
  }
}

variable "prometheus_image" {
  description = "Prometheus image: `<registry path>:<version tag>` and optionally `@sha256:<digest>`. A floating tag is refused. The committed monitoring-images.auto.tfvars pins the digest; override it in your own tfvars to move to another version."
  type        = string
  default     = "docker.io/prom/prometheus:v3.15.1"

  validation {
    condition     = can(regex("^[a-z0-9.-]+(:[0-9]+)?(/[a-z0-9]+([._-][a-z0-9]+)*)+(:[A-Za-z0-9._-]+)?(@sha256:[0-9a-f]{64})?$", var.prometheus_image)) && !can(regex(":(latest|main|master)($|@)", var.prometheus_image)) && can(regex("(:v?[0-9][A-Za-z0-9._-]*|@sha256:)", var.prometheus_image))
    error_message = "prometheus_image must be a registry path with a version tag or a digest, never latest."
  }
}

variable "grafana_image" {
  description = "Grafana image: `<registry path>:<version tag>` and optionally `@sha256:<digest>`. A floating tag is refused (see prometheus_image)."
  type        = string
  default     = "docker.io/grafana/grafana:13.2.5"

  validation {
    condition     = can(regex("^[a-z0-9.-]+(:[0-9]+)?(/[a-z0-9]+([._-][a-z0-9]+)*)+(:[A-Za-z0-9._-]+)?(@sha256:[0-9a-f]{64})?$", var.grafana_image)) && !can(regex(":(latest|main|master)($|@)", var.grafana_image)) && can(regex("(:v?[0-9][A-Za-z0-9._-]*|@sha256:)", var.grafana_image))
    error_message = "grafana_image must be a registry path with a version tag or a digest, never latest."
  }
}

variable "alertmanager_image" {
  description = "Alertmanager image (only run when alert_receiver is not none), pinned like prometheus_image."
  type        = string
  default     = "docker.io/prom/alertmanager:v0.34.2"

  validation {
    condition     = can(regex("^[a-z0-9.-]+(:[0-9]+)?(/[a-z0-9]+([._-][a-z0-9]+)*)+(:[A-Za-z0-9._-]+)?(@sha256:[0-9a-f]{64})?$", var.alertmanager_image)) && !can(regex(":(latest|main|master)($|@)", var.alertmanager_image)) && can(regex("(:v?[0-9][A-Za-z0-9._-]*|@sha256:)", var.alertmanager_image))
    error_message = "alertmanager_image must be a registry path with a version tag or a digest, never latest."
  }
}

variable "kube_state_metrics_image" {
  description = "kube-state-metrics image (issue 149; one small pod next to Prometheus whenever monitoring_enabled is true): `<registry path>:<version tag>` and optionally `@sha256:<digest>`, pinned like prometheus_image. The version and the digest of the multi-architecture index are pinned in monitoring-images.auto.tfvars, and Kubernetes pulls the digest (infra/README.md, \"Monitoring\", \"Images\"); the default here is the same version without a digest."
  type        = string
  default     = "registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.15.0"

  validation {
    condition     = can(regex("^[a-z0-9.-]+(:[0-9]+)?(/[a-z0-9]+([._-][a-z0-9]+)*)+(:[A-Za-z0-9._-]+)?(@sha256:[0-9a-f]{64})?$", var.kube_state_metrics_image)) && !can(regex(":(latest|main|master)($|@)", var.kube_state_metrics_image)) && can(regex("(:v?[0-9][A-Za-z0-9._-]*|@sha256:)", var.kube_state_metrics_image))
    error_message = "kube_state_metrics_image must be a registry path with a version tag or a digest, never latest."
  }
}

variable "monitoring_nodeclaim_metrics_enabled" {
  description = "Issue 149. Adds the NodeClaim part to kube-state-metrics: a custom resource state ConfigMap for `karpenter.sh` NodeClaim (creation time and the transition time of each condition), a ClusterRole and binding that read `nodeclaims` and `customresourcedefinitions`, and the two arguments of the pod. Needs monitoring_enabled and nap_mode = Auto (the NodeClaim definition exists only with node auto-provisioning on); with it false (the default) kube-state-metrics runs with pods and nodes only and works on a cluster without NAP. Which API version NAP serves for NodeClaim is UNVERIFIED (locals.tf, ksm_nodeclaim_api_version)."
  type        = bool
  default     = false
}

variable "factory_network_policy_enabled" {
  description = "PREPARED, OFF. Creates a NetworkPolicy in the factory namespace that lets only Caddy reach the manager API port and only Prometheus reach the metrics port (and, with pod_backend_enabled, the worker namespace the relay port). Enforcement needs the Cilium data plane (network_data_plane = cilium, issue 144): with the production cluster's default data plane no engine runs, so the object would be accepted and not enforced, and turning it on there protects nothing. With the engine, turning it on without checking the rules can cut the manager off from the ingress. The metrics bearer token is the control that works today."
  type        = bool
  default     = false
}

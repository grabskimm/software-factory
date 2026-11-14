#!/usr/bin/env python3
"""Render the cluster objects of the pod backend for the local end-to-end run (docs/pod-backend-local-e2e.md).

Terraform cannot run offline, so this is a HAND TRANSLATION of what ``infra/worker_ns.tf``, ``infra/worker_policy.tf``,
``infra/worker_rbac.tf``, ``infra/pod_backend.tf`` and ``infra/k8s.tf`` render with ``pod_backend_enabled = true``, written so that
every name, uid, path, port and environment name comes from ``factory.podcontract`` (which ``infra/test/test_pod_contract.py``
keeps equal to ``locals.tf``), never retyped. Where the translation had to GUESS or to differ, the line says ``DEVIATION:``
and ``docs/pod-backend-local-e2e.md`` lists it. Standard library only (and ``factory.podcontract`` from ``src``).

    PYTHONPATH=src python3 tools/local_pod_e2e/render.py <part> --image <ref@sha256:...> [options] > objects.json

Parts, in the order Terraform applies them (the fence first, so that no moment exists in which the launcher may create a pod
without it, exactly the ``depends_on`` order of ``worker_rbac.tf``):

    namespaces   the manager and worker namespaces, the service accounts            (worker_ns.tf, k8s.tf)
    netpol       the default-deny NetworkPolicy of the worker namespace             (worker_ns.tf)
    params       the parameter ConfigMap of the pod admission policy                (worker_policy.tf)
    rbac         the launcher's Role and RoleBinding                                (worker_rbac.tf)
    manager      the manager stand-in Deployment, its Services and the key Secret   (pod_backend.tf, k8s.tf)

The policy and binding objects themselves are ``infra/policy/*.json``, applied unchanged by ``run.sh``.
"""

import argparse
import json
import sys

from factory import podcontract as pc

PART_OF = {"app.kubernetes.io/part-of": "factory", "app.kubernetes.io/managed-by": "terraform"}
APP_NAME = "factory"
SELECTOR = {"app.kubernetes.io/name": APP_NAME}
MANAGER_SA = "factory"
SYNCED_SECRET = "factory-keyvault"
KEY_NAME = "ANTHROPIC_API_KEY"
RELAY_SERVICE = f"{APP_NAME}-relay-svc"
POLICY_PARAMS = "worker-policy-params"
LAUNCHER_ROLE = "factory-launcher"
TOKEN_MOUNT = "/var/run/secrets/kubernetes.io/serviceaccount"
PYTHON = "/opt/factory/bin/python"
VOLUMES = {"socket": "launcher-socket", "spool": "spool", "token": "launcher-token"}
SIDECAR_SIZING = {"launcher": {"cpu": "100m", "memory": "128Mi"}, "relay": {"cpu": "250m", "memory": "384Mi"}}
INIT_RESOURCES = {"cpu": "50m", "memory": "32Mi"}
MANAGER_POOL_LABEL = {"pool": "factory"}


def namespaces(_args):
    return [
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": pc.MANAGER_NAMESPACE, "labels": {**PART_OF, "pod-security.kubernetes.io/enforce": "privileged"}},
        },
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": pc.WORKER_NAMESPACE,
                "labels": {**PART_OF, f"{pc.LABEL_PREFIX}role": pc.LABEL_ROLE_VALUE, "pod-security.kubernetes.io/enforce": "privileged"},
            },
        },
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {"name": MANAGER_SA, "namespace": pc.MANAGER_NAMESPACE, "labels": PART_OF},
            "automountServiceAccountToken": False,
        },
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {"name": pc.WORKER_SERVICE_ACCOUNT, "namespace": pc.WORKER_NAMESPACE, "labels": PART_OF},
            "automountServiceAccountToken": False,
        },
    ]


def netpol(args):
    # worker_ns.tf: DNS, the relay (the manager pod's label in the manager namespace), TCP 443 except the metadata service, the Azure
    # host service and the three ranges of the cluster (worker_egress_excluded_cidrs).
    excluded = ["169.254.0.0/16", "168.63.129.16/32", args.pod_cidr, args.service_cidr, args.node_cidr]
    return [
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "default-deny", "namespace": pc.WORKER_NAMESPACE, "labels": PART_OF},
            "spec": {
                "podSelector": {},
                "policyTypes": ["Ingress", "Egress"],
                "egress": [
                    {
                        "ports": [{"port": 53, "protocol": "UDP"}, {"port": 53, "protocol": "TCP"}],
                        "to": [
                            {
                                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                                "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                            }
                        ],
                    },
                    {
                        "ports": [{"port": pc.RELAY_PORT, "protocol": "TCP"}],
                        "to": [
                            {
                                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": pc.MANAGER_NAMESPACE}},
                                "podSelector": {"matchLabels": SELECTOR},
                            }
                        ],
                    },
                    {
                        "ports": [{"port": 443, "protocol": "TCP"}],
                        "to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": excluded}}],
                    },
                ],
            },
        }
    ]


def params(args):
    return [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": POLICY_PARAMS, "namespace": pc.WORKER_NAMESPACE, "labels": PART_OF},
            "data": {"image": args.image},
        }
    ]


def rbac(_args):
    metadata = {"name": LAUNCHER_ROLE, "namespace": pc.WORKER_NAMESPACE, "labels": PART_OF}
    return [
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": metadata,
            "rules": [
                {"apiGroups": [""], "resources": ["pods"], "verbs": ["create", "get", "list", "delete"]},
                {"apiGroups": [""], "resources": ["secrets"], "verbs": ["create", "delete"]},
            ],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": metadata,
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": LAUNCHER_ROLE},
            "subjects": [{"kind": "ServiceAccount", "name": MANAGER_SA, "namespace": pc.MANAGER_NAMESPACE}],
        },
    ]


def _sidecar_security(uid):
    return {
        "runAsNonRoot": True,
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
        "seccompProfile": {"type": "RuntimeDefault"},
        "runAsUser": uid,
        "runAsGroup": pc.SHARED_GID,
    }


def _stand_in_command():
    # DEVIATION: the real main container runs ``factory.manager``. The stand-in holds the manager's two listeners (the application
    # port and the metrics port) so that the worker pod's network canary has something real to FAIL to connect to, and otherwise
    # sleeps; the driver runs in it through ``kubectl exec`` as the supervisor identity. It runs through the image's own entrypoint
    # (which drops to uid 10001, group 10001 + 10010 with the three ambient capabilities), exactly as the manager does.
    code = (
        "import socket, threading, time\n"
        "def serve(port):\n"
        "    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(('0.0.0.0', port)); s.listen(16)\n"
        "    while True:\n"
        "        c, _ = s.accept(); c.close()\n"
        f"for p in ({pc.APP_PORT}, {pc.METRICS_PORT}):\n"
        "    threading.Thread(target=serve, args=(p,), daemon=True).start()\n"
        "time.sleep(10**9)\n"
    )
    return [PYTHON, "-c", code]


def manager(args):
    image = args.image
    env_manager = pc.ENV_MANAGER
    env_launcher = pc.ENV_LAUNCHER
    env_relay = pc.ENV_RELAY
    ids = {"launcher": pc.LAUNCHER_UID, "relay": pc.RELAY_UID}
    relay_host = f"{RELAY_SERVICE}.{pc.MANAGER_NAMESPACE}.svc.cluster.local"
    app_fqdn = f"{APP_NAME}.{pc.MANAGER_NAMESPACE}.svc.cluster.local"
    init = {
        "name": "volume-perms",
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "command": [
            "/bin/sh",
            "-c",
            " && ".join(
                [
                    f"chown {pc.LAUNCHER_UID}:{pc.SHARED_GID} {pc.LAUNCHER_SOCKET_DIR}",
                    f"chmod 0750 {pc.LAUNCHER_SOCKET_DIR}",
                    f"chown {pc.RELAY_UID}:{pc.SHARED_GID} {pc.SPOOL_DIR}",
                    f"chmod 2770 {pc.SPOOL_DIR}",
                ]
            ),
        ],
        "resources": {"requests": INIT_RESOURCES, "limits": INIT_RESOURCES},
        "securityContext": {
            "runAsUser": 0,
            "runAsNonRoot": False,
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"], "add": ["CHOWN", "FOWNER", "FSETID"]},
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "volumeMounts": [
            {"name": VOLUMES["socket"], "mountPath": pc.LAUNCHER_SOCKET_DIR},
            {"name": VOLUMES["spool"], "mountPath": pc.SPOOL_DIR},
        ],
    }
    main = {
        "name": "factory",
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "command": None,  # replaced below: the image's ENTRYPOINT is kept, the stand-in program is its argument
        "args": _stand_in_command(),
        "ports": [
            {"name": "http", "containerPort": pc.APP_PORT, "protocol": "TCP"},
            {"name": "metrics", "containerPort": pc.METRICS_PORT, "protocol": "TCP"},
        ],
        "env": [
            {"name": "FACTORY_DATA", "value": pc.SCRATCH_DIR},
            {"name": env_manager["pod_backend"], "value": pc.POD_BACKEND_ON},
            {"name": env_manager["max_pod_sessions"], "value": str(args.max_sessions)},
            {"name": env_manager["launcher_socket"], "value": pc.LAUNCHER_SOCKET},
            {"name": env_manager["spool_dir"], "value": pc.SPOOL_DIR},
        ],
        "resources": {"requests": {"cpu": "1", "memory": "2Gi"}, "limits": {"cpu": "1", "memory": "2Gi"}},
        # Relaxations 2 and 3 of 3 (k8s.tf).
        "securityContext": {"procMount": "Unmasked", "appArmorProfile": {"type": "Unconfined"}},
        "volumeMounts": [
            {"name": "data", "mountPath": pc.SCRATCH_DIR},
            {"name": VOLUMES["socket"], "mountPath": pc.LAUNCHER_SOCKET_DIR, "readOnly": True},
            {"name": VOLUMES["spool"], "mountPath": pc.SPOOL_DIR},
        ],
    }
    del main["command"]
    launcher = {
        "name": "launcher",
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "command": [PYTHON, "-m", "factory.launcher"],
        "env": [
            {"name": env_launcher["image"], "value": image},
            {"name": env_launcher["namespace"], "value": pc.WORKER_NAMESPACE},
            {"name": env_launcher["max_pods"], "value": str(args.max_sessions)},
            {"name": env_launcher["repo_cap"], "value": str(pc.PER_REPO_CAP)},
            {"name": env_launcher["deadline_clamp"], "value": str(pc.DEADLINE_CLAMP_SECONDS)},
            {"name": env_launcher["allowed_uid"], "value": str(pc.SUPERVISOR_UID)},
            {"name": env_launcher["relay_url"], "value": f"http://{relay_host}:{pc.RELAY_PORT}"},
            {"name": env_launcher["classes"], "value": pc.DEFAULT_CLASS},
            # DEVIATION: the cluster's public FQDN. On kind there is none; the host is the address of the API proxy stand-in
            # (run.sh), which sits outside the node subnet and forwards 443 to the API server, so an anonymous request really is
            # answered with 401/403 (the canary's "public" probe passes for the right reason).
            {"name": env_launcher["api_public_host"], "value": args.api_public_host},
            {"name": env_launcher["manager_address"], "value": app_fqdn},
            {"name": env_launcher["manager_metrics_address"], "value": f"{APP_NAME}-metrics.{pc.MANAGER_NAMESPACE}.svc.cluster.local"},
            {"name": env_launcher["socket"], "value": pc.LAUNCHER_SOCKET},
            {"name": KEY_NAME, "valueFrom": {"secretKeyRef": {"name": SYNCED_SECRET, "key": KEY_NAME}}},
            {"name": "AZMON_COLLECT_ENV", "value": "False"},
        ],
        "resources": {"requests": SIDECAR_SIZING["launcher"], "limits": SIDECAR_SIZING["launcher"]},
        "securityContext": _sidecar_security(ids["launcher"]),
        "volumeMounts": [
            {"name": VOLUMES["socket"], "mountPath": pc.LAUNCHER_SOCKET_DIR},
            {"name": VOLUMES["token"], "mountPath": TOKEN_MOUNT, "readOnly": True},
        ],
    }
    relay = {
        "name": "relay",
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "command": [PYTHON, "-m", "factory.relay"],
        "ports": [{"name": "relay", "containerPort": pc.RELAY_PORT, "protocol": "TCP"}],
        "env": [
            {"name": env_relay["port"], "value": str(pc.RELAY_PORT)},
            {"name": env_relay["spool_dir"], "value": pc.SPOOL_DIR},
            {"name": "AZMON_COLLECT_ENV", "value": "False"},
        ],
        "resources": {"requests": SIDECAR_SIZING["relay"], "limits": SIDECAR_SIZING["relay"]},
        "securityContext": _sidecar_security(ids["relay"]),
        "volumeMounts": [{"name": VOLUMES["spool"], "mountPath": pc.SPOOL_DIR}],
    }
    volumes = [
        # DEVIATION: an emptyDir for the data volume (the real one is the Azure Disk claim); the worktrees and the ledger live
        # here for the life of the stand-in pod only.
        {"name": "data", "emptyDir": {}},
        {"name": VOLUMES["socket"], "emptyDir": {"sizeLimit": pc.SOCKET_DIR_SIZE_LIMIT}},
        {"name": VOLUMES["spool"], "emptyDir": {"sizeLimit": pc.SPOOL_SIZE_LIMIT}},
        {
            "name": VOLUMES["token"],
            "projected": {
                "sources": [
                    {"serviceAccountToken": {"path": "token", "expirationSeconds": 3600}},
                    {"configMap": {"name": "kube-root-ca.crt", "items": [{"key": "ca.crt", "path": "ca.crt"}]}},
                ]
            },
        },
    ]
    spec = {
        "serviceAccountName": MANAGER_SA,
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "nodeSelector": MANAGER_POOL_LABEL,
        "hostUsers": False,  # relaxation 1 of 3
        "terminationGracePeriodSeconds": 30,
        "tolerations": [
            {"key": pc.MANAGER_TAINT_KEY, "operator": "Equal", "value": pc.MANAGER_TAINT_VALUE, "effect": pc.TAINT_EFFECT}
        ],
        "initContainers": [init],
        "containers": [main, launcher, relay],
        "volumes": volumes,
    }
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": APP_NAME, "namespace": pc.MANAGER_NAMESPACE, "labels": {**PART_OF, **SELECTOR}},
        "spec": {
            "replicas": 1,
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": SELECTOR},
            "template": {
                "metadata": {
                    "labels": {**PART_OF, **SELECTOR},
                    # DEVIATION: no ``azure.workload.identity/use`` label (no webhook on kind), so no skip-containers annotation either.
                },
                "spec": spec,
            },
        },
    }
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": SYNCED_SECRET, "namespace": pc.MANAGER_NAMESPACE, "labels": PART_OF},
        "type": "Opaque",
        # DEVIATION: the real Secret is synced from Key Vault. This one holds a fixed placeholder that is not a key; nothing in the
        # run calls an API, and the value is only a string the launcher writes into the per-session Secret.
        "stringData": {KEY_NAME: args.placeholder_key},
    }
    services = [
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": APP_NAME, "namespace": pc.MANAGER_NAMESPACE, "labels": PART_OF},
            "spec": {"type": "ClusterIP", "selector": SELECTOR, "ports": [{"name": "http", "port": pc.APP_PORT, "targetPort": "http"}]},
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": RELAY_SERVICE, "namespace": pc.MANAGER_NAMESPACE, "labels": PART_OF},
            "spec": {"type": "ClusterIP", "selector": SELECTOR, "ports": [{"name": "relay", "port": pc.RELAY_PORT, "targetPort": "relay"}]},
        },
        {
            # monitoring.tf: the metrics listener has its own Service; the canary's 9464 probe targets it (issue #232).
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": f"{APP_NAME}-metrics", "namespace": pc.MANAGER_NAMESPACE, "labels": PART_OF},
            "spec": {"type": "ClusterIP", "selector": SELECTOR, "ports": [{"name": "metrics", "port": pc.METRICS_PORT, "targetPort": "metrics"}]},
        },
    ]
    return [secret, *services, deployment]


PARTS = {"namespaces": namespaces, "netpol": netpol, "params": params, "rbac": rbac, "manager": manager}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("part", choices=sorted(PARTS))
    parser.add_argument("--image", required=True, help="the pinned image reference (registry/repo@sha256:...)")
    parser.add_argument("--pod-cidr", default="10.244.0.0/16")
    parser.add_argument("--service-cidr", default="10.96.0.0/16")
    parser.add_argument("--node-cidr", default="172.31.0.0/24")
    parser.add_argument("--api-public-host", default="172.31.1.10")
    parser.add_argument("--max-sessions", type=int, default=2)
    parser.add_argument("--placeholder-key", default="sk-ant-placeholder-not-a-real-key")
    args = parser.parse_args(argv)
    json.dump({"apiVersion": "v1", "kind": "List", "items": PARTS[args.part](args)}, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Run INSIDE the launcher container (``kubectl exec -i ... -c launcher -- /opt/factory/bin/python - <mutation>``).

It uses the launcher's OWN Kubernetes token and Role (the real ``factory.kube`` client, the real configuration read from the
container's environment) to attempt what the launcher must never be able to do, and prints one line per attempt:
``PROBE <label> <status-or-error> <first line of the denial, sanitised>``. No token, key or environment value is ever printed.

The mutations start from the golden worker pod of ``factory.podspec.build_pod`` (which the real policy admits) and change ONE thing, so
a denial names the rule that the change breaks. Nothing here is created if the policy works: every attempt must be refused.
"""

import copy
import json
import os
import re
import sys
import uuid

from factory import kube, launcher
from factory import podcontract as pc
from factory import podspec

SAFE = re.compile(r"[^A-Za-z0-9 ._:'/,()\[\]=-]")


def line(label, outcome, detail=""):
    sys.stdout.write(f"PROBE {label} {outcome} {SAFE.sub('?', detail)[:200]}\n")
    sys.stdout.flush()


def golden(config):
    session = uuid.uuid4().hex
    request = {
        "session_id": session,
        "repository": "e2e/pod",
        "run_id": "run-probe",
        "job_id": "job-1",
        "attempt": 0,
        "phase": "build",
        "resource_class": pc.DEFAULT_CLASS,
        "timeout_seconds": 120,
        "manager_instance": "0" * 12,
    }
    return session, podspec.build_pod(request, config.pod_config())


def mutate(pod, name):
    pod = copy.deepcopy(pod)
    spec = pod["spec"]
    container = spec["containers"][0]
    if name == "host-network":
        spec["hostNetwork"] = True
    elif name == "privileged":
        container["securityContext"]["privileged"] = True
    elif name == "other-image":
        container["image"] = "docker.io/library/factory-podspike@sha256:" + "0" * 64
    elif name == "host-path":
        spec["volumes"].append({"name": "h", "hostPath": {"path": "/"}})
        container["volumeMounts"].append({"name": "h", "mountPath": "/host"})
    elif name == "service-account-token":
        spec["automountServiceAccountToken"] = True
    elif name == "host-users-true":
        spec["hostUsers"] = True
    elif name == "extra-capability":
        container["securityContext"].setdefault("capabilities", {})["add"] = ["SYS_ADMIN"]
    elif name == "node-name":
        spec["nodeName"] = "podspike-control-plane"
    elif name == "command":
        container["command"] = ["/bin/sh", "-c", "id"]
    return pod


def main():
    config = launcher.Config.from_environment(os.environ)
    client = kube.Client.in_cluster()
    session, pod = golden(config)
    for name in sys.argv[1:]:
        if name.startswith("rbac-"):
            continue
        broken = mutate(pod, name)
        try:
            client.create_pod(broken)
            line(name, "ADMITTED", "the policy let it through")
            with_name = broken["metadata"]["name"]
            client.delete_pod(with_name, 0)
        except kube.KubeError as error:
            line(name, f"{type(error).__name__}:{getattr(error, 'status', None)}", f"rules={list(getattr(error, 'rules', ()))}")
        except Exception as error:  # noqa: BLE001
            line(name, type(error).__name__, str(error))
    if "rbac-secret-read" in sys.argv[1:]:
        # The Role has no get, list or watch on secrets and no pods/exec: the raw API answers 403.
        for label, path in (
            ("rbac-secret-list", f"/api/v1/namespaces/{pc.WORKER_NAMESPACE}/secrets"),
            ("rbac-pod-log", f"/api/v1/namespaces/{pc.WORKER_NAMESPACE}/pods/x/log"),
            ("rbac-other-namespace-pods", f"/api/v1/namespaces/{pc.MANAGER_NAMESPACE}/pods"),
            ("rbac-nodes", "/api/v1/nodes"),
        ):
            line(label, _raw_get(path))
    json.dump({"session": session[:8]}, open(os.devnull, "w"))


def _raw_get(path):
    import http.client
    import ssl

    token = open("/var/run/secrets/kubernetes.io/serviceaccount/token").read().strip()
    context = ssl.create_default_context(cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    connection = http.client.HTTPSConnection(os.environ["KUBERNETES_SERVICE_HOST"], int(os.environ["KUBERNETES_SERVICE_PORT"]), context=context, timeout=10)
    connection.request("GET", path, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    response = connection.getresponse()
    status = response.status
    response.read()
    connection.close()
    return status


main()

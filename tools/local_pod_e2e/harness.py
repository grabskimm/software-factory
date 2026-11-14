#!/usr/bin/env python3
"""The local end-to-end harness of the pod backend (docs/pod-backend-local-e2e.md): one command that builds a cluster, runs sessions, tears it down.

LOCAL kind by default; parameterised by a kubeconfig and an image reference so that a person can point the SAME scenarios at a real
cluster that already has the objects (``--no-cluster``). It NEVER touches ``~/.kube/config``: every kubectl call carries ``--kubeconfig``.
It makes no API call and holds no key: the agent in the worker pod is the stub ``claude`` of ``Dockerfile.stub``.

    python3 tools/local_pod_e2e/harness.py up   --kubeconfig KC [--base-image factory-podspike:local]
    python3 tools/local_pod_e2e/harness.py run  --kubeconfig KC [--only NAME ...] [--out DIR]
    python3 tools/local_pod_e2e/harness.py down --kubeconfig KC
    python3 tools/local_pod_e2e/harness.py all  --kubeconfig KC            up, run, then down (down also on failure)

    --image REF        a pinned ``registry/repo@sha256:...`` that every node can pull (skips the layer build and the node import); with
                       ``--no-cluster`` the objects are not applied either, only the scenarios run. The stub agent must be in that image.
    --no-cluster       a cluster that already exists (objects and image in place): ``up`` and ``down`` do nothing to it.

Needs docker, kind, kubectl (and python3). On the kind path it creates the docker network ``podspike-net`` (172.31.0.0/16, nodes in
172.31.0.0/24), the cluster ``podspike`` (3 nodes: control plane, a manager node, a worker-pool node), the API proxy container
``podspike-apiproxy`` (172.31.1.10:443 -> the API server: the "public address" the canary probes) and removes all of it in ``down``.
Standard library only (``factory.podcontract`` from ``src`` through PYTHONPATH=src).
"""

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from factory import podcontract as pc  # noqa: E402

CLUSTER = "podspike"
NETWORK = "podspike-net"
SUBNET, IP_RANGE, GATEWAY = "172.31.0.0/16", "172.31.0.0/24", "172.31.0.1"
API_PROXY, API_PROXY_IP = "podspike-apiproxy", "172.31.1.10"
NODE_IMAGE = "kindest/node@sha256:3489c7674813ba5d8b1a9977baea8a6e553784dab7b84759d1014dbd78f7ebd5"
MANAGER_NODE_LABEL = ("pool", "factory")
WORKER_NODE_LABEL = (pc.WORKER_NODE_LABEL_KEY, pc.WORKER_NODE_LABEL_VALUE)
POLICY_FILES = ["pod-policy.json", "pod-binding.json", "secret-policy.json", "secret-binding.json"]
MAX_SESSIONS = 2
M, W = pc.MANAGER_NAMESPACE, pc.WORKER_NAMESPACE
TOKEN_LIKE = re.compile(r"[A-Za-z0-9_-]{43,}")


class Fail(Exception):
    pass


def run(argv, *, check=True, timeout=120, input=None, env=None, quiet=False):
    done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, input=input, env=env)
    if check and done.returncode:
        raise Fail(f"{argv[0]} {' '.join(argv[1:3])} failed ({done.returncode}): {(done.stderr or done.stdout).strip()[-400:]}")
    return done


class Cluster:
    def __init__(self, kubeconfig, args):
        self.kubeconfig = str(kubeconfig)
        self.args = args
        self.image = args.image
        self.manager_pod = None

    # -- kubectl ----------------------------------------------------------------------------------

    def kubectl(self, *argv, check=True, timeout=120, input=None):
        return run(["kubectl", "--kubeconfig", self.kubeconfig, *argv], check=check, timeout=timeout, input=input)

    def apply(self, document):
        self.kubectl("apply", "-f", "-", input=document if isinstance(document, str) else json.dumps(document))

    def json(self, *argv):
        return json.loads(self.kubectl(*argv, "-o", "json").stdout)

    def render(self, part, **extra):
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
        argv = [sys.executable, str(HERE / "render.py"), part, "--image", self.image, "--max-sessions", str(MAX_SESSIONS)]
        for key, value in extra.items():
            argv += [f"--{key.replace('_', '-')}", str(value)]
        return run(argv, env=env).stdout

    # -- the objects, in the order Terraform applies them -----------------------------------------

    def apply_objects(self):
        """The fence first (namespaces, default-deny, policies and bindings, parameters), then the Role, then the manager."""
        self.apply(self.render("namespaces"))
        self.apply(self.render("netpol"))
        policy_dir = ROOT / "infra" / "policy"
        for name in POLICY_FILES:
            self.kubectl("apply", "-f", str(policy_dir / name))
        self.apply(self.render("params"))
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
        status = run([sys.executable, str(ROOT / "tools" / "policy_matrix.py"), "status"], env=dict(env, KUBECONFIG=self.kubeconfig), check=False, timeout=180)
        self.note("policy type check", (status.stdout + status.stderr).strip().splitlines()[:6])
        self.apply(self.render("rbac"))
        self.apply(self.render("manager"))
        self.kubectl("-n", M, "rollout", "status", "deploy/factory", "--timeout=240s", timeout=260)
        self.manager_pod = self.find_manager()

    def note(self, label, value):
        sys.stderr.write(f"harness: {label}: {value}\n")

    def find_manager(self):
        out = self.kubectl("-n", M, "get", "pod", "-l", "app.kubernetes.io/name=factory", "-o", "name").stdout.split()
        if not out:
            raise Fail("no manager pod")
        return out[0]

    # -- execution inside the stand-in --------------------------------------------------------------

    def exec_main(self, *command, timeout=600, input=None, check=True):
        pod = self.manager_pod or self.find_manager()
        return self.kubectl("-n", M, "exec", *(["-i"] if input is not None else []), pod, "-c", "factory", "--", "/usr/local/bin/factory-entrypoint", *command, timeout=timeout, input=input, check=check)

    def push_driver(self):
        self.exec_main("sh", "-c", "mkdir -p /tmp/e2e && cat > /tmp/e2e/driver.py", input=(HERE / "driver.py").read_text())

    def driver(self, *args, timeout=600):
        done = self.exec_main("/opt/factory/bin/python", "/tmp/e2e/driver.py", *args, timeout=timeout, check=False)
        return parse_driver(done.stdout, done.stderr)

    def driver_background(self, name, *args):
        """Start the driver in the stand-in, detached; its output goes to /tmp/e2e/<name>.out."""
        argv = " ".join(["/opt/factory/bin/python", "/tmp/e2e/driver.py", *[shell_quote(a) for a in args]])
        self.exec_main("sh", "-c", f"rm -f /tmp/e2e/{name}.out; nohup {argv} > /tmp/e2e/{name}.out 2>&1 &", timeout=60)

    def driver_output(self, name):
        done = self.exec_main("sh", "-c", f"cat /tmp/e2e/{name}.out 2>/dev/null", check=False, timeout=60)
        return done.stdout

    def wait_started(self, name, timeout=120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            text = self.driver_output(name)
            match = re.search(r"E2E_STARTED ([0-9a-f]{32})", text)
            if match:
                return match.group(1)
            time.sleep(1)
        raise Fail(f"{name}: the session did not start")

    def wait_result(self, name, timeout=300):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            text = self.driver_output(name)
            if "E2E_RESULT" in text:
                return parse_driver(text, "")
            time.sleep(2)
        raise Fail(f"{name}: no result")

    # -- what the cluster holds -----------------------------------------------------------------------

    def worker_objects(self):
        pods = [p["metadata"]["name"] for p in self.json("-n", W, "get", "pods")["items"]]
        secrets = [s["metadata"]["name"] for s in self.json("-n", W, "get", "secrets")["items"]]
        return {"pods": pods, "secrets": secrets}

    def wait_workers_gone(self, timeout=90):
        deadline = time.monotonic() + timeout
        while True:
            held = self.worker_objects()
            if not held["pods"] and not held["secrets"]:
                return held
            if time.monotonic() > deadline:
                return held
            time.sleep(2)

    def wait_worker_pod(self, timeout=120, phase="Running"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for pod in self.json("-n", W, "get", "pods")["items"]:
                if pod["status"].get("phase") == phase:
                    return pod["metadata"]["name"]
            time.sleep(1)
        raise Fail("no worker pod became " + phase)


def shell_quote(text):
    return "'" + str(text).replace("'", "'\\''") + "'"


def parse_driver(stdout, stderr):
    result, started = None, None
    for line in stdout.splitlines():
        if line.startswith("E2E_RESULT "):
            result = json.loads(line[len("E2E_RESULT ") :])
        elif line.startswith("E2E_STARTED "):
            started = line.split()[1]
    if result is None:
        return {"driver_failed": True, "stderr_tail": scrub(stderr)[-500:], "stdout_tail": scrub(stdout)[-300:]}
    result["_started"] = started
    return result


def scrub(text):
    """Nothing token-shaped reaches a note: a long base64url-ish run is replaced."""
    return TOKEN_LIKE.sub("<redacted>", text)


# ---- the scenarios ---------------------------------------------------------------------------------------------------


class Report:
    def __init__(self):
        self.rows = []

    def add(self, ident, step, expected, observed, ok, evidence=""):
        self.rows.append({"id": ident, "step": step, "expected": expected, "observed": scrub(str(observed)), "status": "PASS" if ok else "FAIL", "evidence": scrub(str(evidence))})
        sys.stderr.write(f"{'PASS' if ok else 'FAIL'} {ident} {step}: {scrub(str(observed))[:160]}\n")


def no_leak(cluster, observed):
    """The common post-conditions of every ended session: nothing left in the namespace, the pool, the state or the relay."""
    held = cluster.wait_workers_gone()
    problems = []
    if held["pods"] or held["secrets"]:
        problems.append(f"namespace holds {held}")
    if observed.get("pods_after", {}).get("count") not in (0, None):
        problems.append("the launcher still lists a pod")
    pool = observed.get("pool_after") or {}
    if pool.get("in_use") not in (0, None):
        problems.append(f"the pool holds {pool.get('in_use')}")
    if observed.get("state_pod_recorded_after"):
        problems.append("the job state still records a pod")
    relay = observed.get("relay") or {}
    if observed.get("session") and not relay.get("released"):
        problems.append("the relay record was not released")
    return problems


def scenario(cluster, report, ident, step, name, stub, expected, check, *, phase="build-0", timeout=120, leak=True, wait=600):
    observed = cluster.driver("session", name, "--stub", stub, "--phase", phase, "--timeout", str(timeout), timeout=wait)
    if observed.get("driver_failed"):
        report.add(ident, step, expected, f"the driver failed: {observed}", False)
        return observed
    problems = []
    outcome_ok, why = check(observed)
    if not outcome_ok:
        problems.append(why)
    if leak:
        problems += no_leak(cluster, observed)
    summary = f"{observed.get('outcome')}: {observed.get('error_type', '')} {observed.get('error_text', '')[:140]}".strip()
    report.add(ident, step, expected, summary + ("" if not problems else f"  PROBLEMS: {'; '.join(problems)}"), not problems, f"session={observed.get('session')} elapsed={observed.get('elapsed_seconds')}s")
    return observed


def contains(text):
    return lambda o: (o.get("outcome") == "error" and text in o.get("error_text", ""), f"error text {o.get('error_text')!r} lacks {text!r}")


def run_scenarios(cluster, report, only):
    def wanted(ident):
        return not only or ident in only

    cluster.push_driver()
    if wanted("E1"):
        def happy(o):
            good = (
                o.get("outcome") == "success"
                and o.get("file_hello") == "hello from the pod\n"
                and o.get("files", {}).get("sub/nested.txt") == 7
                and o.get("result", {}).get("summary") == "stub edit"
                and o.get("transcript_has_result")
                and (o.get("usage") or {}).get("total_cost_usd") == 0.0123
                and (o.get("relay") or {}).get("state") == "finished"
                and (o.get("relay") or {}).get("released")
                and (o.get("pool_before") or {}).get("in_use") == 0
            )
            return bool(good), f"the session did not complete as designed: {json.dumps({k: o.get(k) for k in ('outcome', 'file_hello', 'relay', 'result')}, default=str)[:300]}"

        scenario(cluster, report, "E1", "one full session, happy path (build phase)", "happy", "mode=ok", "success; the edit and a new nested file are in the supervisor's worktree; result and usage came back through the closed schemas; relay finished and released; pod, Secret and reservation gone", happy)
    if wanted("E2"):
        scenario(cluster, report, "E2", "a read-only phase that changes nothing", "review-ok", "mode=noedit", "success with the reviewer schema, empty delta", lambda o: (o.get("outcome") == "success" and set(o.get("files", {})) == {"README.md", "src/app.txt"} and "pass" in (o.get("result") or {}), "unexpected"), phase="review-0")
    if wanted("E3"):
        scenario(cluster, report, "E3", "a read-only phase that writes a file: the delta is refused", "review-write", "mode=readonly-write", "an error naming a rejected delta; nothing written in the supervisor's worktree", lambda o: (o.get("outcome") == "error" and "delta" in o.get("error_text", "") and "should-not-exist.txt" not in o.get("files", {}), "the write was not refused"), phase="review-0")
    if wanted("E4"):
        def timed_out(o):
            ok = o.get("outcome") == "error" and o.get("error_type") == "AgentTimeout" and "slept.txt" in o.get("files", {}) and 55 <= o.get("elapsed_seconds", 0) <= 200
            return bool(ok), f"outcome={o.get('outcome')} type={o.get('error_type')} elapsed={o.get('elapsed_seconds')} files={list(o.get('files', {}))}"

        scenario(cluster, report, "E4", "the agent sleeps past the time limit (60 s)", "timeout", "mode=sleep seconds=3600", "AgentTimeout after about 60 s; the edit made before the sleep is kept (delta applied first)", timed_out, timeout=60, wait=400)
    if wanted("E5"):
        scenario(cluster, report, "E5", "the agent exits non-zero and posted no result", "exit3", "mode=exit code=3", "`agent exited 3`", contains("agent exited 3"))
    if wanted("E6"):
        scenario(cluster, report, "E6", "the agent posts a success result and then exits 1", "exit1-after-success", "mode=exit result=1 code=1", "`agent exited 1 after a success result`", contains("agent exited 1 after a success result"))
    if wanted("E7"):
        scenario(cluster, report, "E7", "the agent exits 0 without a result event", "noresult", "mode=noresult", "`agent produced no result event`", contains("agent produced no result event"))
    if wanted("E8"):
        scenario(cluster, report, "E8", "a symlink out of the tree in the delta: refused as a whole", "scope-link", "mode=scope-link", "an error naming a rejected delta; neither the link nor the file is applied", lambda o: (o.get("outcome") == "error" and "delta" in o.get("error_text", "") and "evil-link" not in o.get("files", {}) and "hello.txt" not in o.get("files", {}), "the link or the edit was applied"))
    if wanted("E9"):
        scenario(cluster, report, "E9", "the agent writes next to the worktree (outside the scope)", "scope-outside", "mode=scope-dotdot", "the pod's file system refuses the write (the data directory above the worktree is root's); nothing outside the worktree appears on the supervisor's side; the in-tree edit comes back", lambda o: (not o.get("outside_file_written") and o.get("outcome") == "success" and "hello.txt" in o.get("files", {}), "a file outside the worktree reached the supervisor, or the session did not end"))
    if wanted("E10"):
        scenario(cluster, report, "E10", "the agent writes a git hook inside .git of the pod's tree", "scope-git", "mode=scope-git", "no hook reaches the supervisor's repository (the wrapper leaves .git out of the delta it builds; a hand-made delta with a git path is refused, tests.test_delta_malicious)", lambda o: (not o.get("git_hook_applied"), "a git hook reached the supervisor's repository"))
    if wanted("E11"):
        def probe(o):
            seen = o.get("probe") or {}
            checks = seen.get("checks", {})
            # By design (docs/pod-backend.md, "What the pod is given"): the agent holds the Anthropic key in ITS environment (claude needs it), it can list
            # the names in the Secret volume (not read them) and /data is the shared scratch volume. What it must not reach: the relay token, the
            # service account token, another process's environment, the root file system.
            bad = [k for k in ("token_file_readable", "key_file_readable", "sa_token_exists", "proc1_environ_readable", "etc_shadow_readable", "usr_writable") if checks.get(k)]
            names = seen.get("env_names", [])
            relay_names = [n for n in names if n.startswith("FACTORY_") or "RELAY" in n or n.startswith("KUBERNETES_") and False]
            scan = seen.get("proc_scan", {}).get("readable_foreign_environ_with_relay_url", -1)
            ok = o.get("outcome") == "success" and seen.get("uid") == pc.WORKER_UID and not bad and not relay_names and scan == 0
            return bool(ok), f"uid={seen.get('uid')} readable={bad} FACTORY_/RELAY names in the agent's environment={relay_names} foreign_environ_hits={scan}"

        scenario(cluster, report, "E11", "the agent tries to read the relay token, the key, the service account token, other processes' environment", "probe-secrets", "mode=probe", "uid 10002; the token file, the key file and the service-account token not readable; /proc/1/environ and /etc/shadow not readable; /usr not writable; no FACTORY_* variable in the agent environment; no other process's environment readable (the Anthropic key in its own environment is by design)", probe)
    if wanted("E12"):
        run_net(cluster, report)
    if wanted("E13"):
        run_kill_pod(cluster, report)
    if wanted("E14"):
        run_kill_supervisor(cluster, report)
    if wanted("E15"):
        run_admission(cluster, report)
    if wanted("E16"):
        run_pool(cluster, report)
    if wanted("E17"):
        run_s1(cluster, report)
    if wanted("E21"):
        run_runbook_probes(cluster, report)
    if wanted("E20"):
        run_identities(cluster, report)
    if wanted("E19"):
        run_one_repo_per_node(cluster, report)
    if wanted("E18"):
        run_isolation(cluster, report)


def run_net(cluster, report):
    # Control first: the same destinations from a pod of an UNFENCED namespace must connect, so a "blocked" below means the fence, not an absent listener.
    app_address = f"factory.{M}.svc.cluster.local"
    manager_ip = cluster.json("-n", M, "get", "pod", cluster.manager_pod.split("/")[-1])["status"]["podIP"]
    if cluster.args.no_cluster:
        # A real cluster: the node of the worker pool is the one the control pod lands on (read below, once it runs), the API server
        # is the address of the `kubernetes` Service endpoints (a public address on AKS), and the "API proxy" of the kind network is that same address.
        node_ip = None
        api_ip = cluster.json("get", "endpointslices", "-l", "kubernetes.io/service-name=kubernetes")["items"][0]["endpoints"][0]["addresses"][0]
        api_svc = cluster.json("get", "service", "kubernetes")["spec"]["clusterIP"]
        api_proxy = api_ip
        # Any other pod that has its own address (not a host-network one) and is not the manager's.
        peer_ip = next(item["status"]["podIP"] for item in cluster.json("get", "pods", "-A")["items"] if item["metadata"]["namespace"] not in (M, W) and item["status"].get("podIP") and item["status"]["podIP"] != item["status"].get("hostIP"))
    else:
        node_ip = cluster.json("get", "node", f"{CLUSTER}-worker2")["status"]["addresses"][0]["address"]
        api_ip = cluster.json("get", "node", f"{CLUSTER}-control-plane")["status"]["addresses"][0]["address"]
        api_svc, api_proxy, peer_ip = "10.96.0.1", API_PROXY_IP, "10.244.1.99"
    cluster.kubectl("create", "namespace", "e2e-control", check=False)
    control = f"""
apiVersion: v1
kind: Pod
metadata: {{name: control, namespace: e2e-control}}
spec:
  restartPolicy: Never
  nodeSelector: {{{WORKER_NODE_LABEL[0]}: {WORKER_NODE_LABEL[1]}}}
  tolerations: [{{key: {pc.WORKER_TAINT_KEY}, operator: Equal, value: {pc.WORKER_TAINT_VALUE}, effect: NoSchedule}}]
  containers:
  - name: c
    image: {cluster.image}
    imagePullPolicy: IfNotPresent
    command: ["/usr/bin/python3", "-c", "import time; time.sleep(600)"]
"""
    cluster.apply(control)
    cluster.kubectl("-n", "e2e-control", "wait", "--for=condition=Ready", "pod/control", "--timeout=600s", timeout=620)
    if node_ip is None:
        node_ip = cluster.json("-n", "e2e-control", "get", "pod", "control")["status"]["hostIP"]
    script = (
        "import socket,sys\nfor a in sys.argv[1:]:\n    h,p=a.rsplit(':',1)\n    try:\n        s=socket.create_connection((h,int(p)),timeout=3); s.close(); r='CONNECT'\n"
        "    except OSError:\n        r='no'\n    print(a, r)\n"
    )
    targets = [f"{app_address}:8080", f"{app_address}:9464", f"{manager_ip}:8080", f"{manager_ip}:9464", f"{node_ip}:10250", f"{pc_relay_host()}:{pc.RELAY_PORT}", f"{api_proxy}:443"]
    time.sleep(6)
    done = cluster.kubectl("-n", "e2e-control", "exec", "-i", "control", "--", "python3", "-", *targets, input=script, check=False)
    lines = dict(line.rsplit(" ", 1) for line in done.stdout.splitlines() if " " in line)
    # The Service `factory` has no port 9464 (k8s.tf: the metrics port has its own Service, factory-metrics), so the canary's probe of
    # FACTORY_MANAGER_ADDRESS:9464 cannot connect with or without a fence (docs/pod-backend-local-e2e.md, D2). The pod address does listen.
    must = [f"{app_address}:8080", f"{manager_ip}:8080", f"{manager_ip}:9464", f"{pc_relay_host()}:{pc.RELAY_PORT}", f"{api_proxy}:443"]
    control_ok = all(lines.get(key) == "CONNECT" for key in must)
    report.add("E12a", "control: a pod of an UNFENCED namespace reaches what the worker must not (manager ports by Service and by pod address, the relay, the API proxy)", "all connect, except the Service `factory` on 9464 (it has no such port: D3)", done.stdout.replace("\n", "; "), control_ok)
    cluster.kubectl("delete", "namespace", "e2e-control", "--wait=false", check=False)
    time.sleep(1)
    out = cluster.driver(
        "session", "net", "--stub", f"mode=net relay={pc_relay_host()} manager_ip={manager_ip} mgr={app_address} node_ip={node_ip} api_svc={api_svc} api_ip={api_ip} api_proxy={api_proxy} peer_ip={peer_ip} ext=1.1.1.1", "--timeout", "90"
    )
    try:
        seen = out.get("probe") or {}
    except AttributeError:
        seen = {}
    expect_blocked = ["manager_service_8080", "manager_pod_ip_8080", "manager_pod_ip_9464", "node_kubelet_10250", "api_service_443", "api_node_6443", "imds_80", "wireserver_80", "hostga_32526", "external_80", "dns_tcp_53_external", "other_pod_8080"]
    # Open by design: the relay (by Service and by the manager pod's own address, which the policy selects by label), TCP 443 outward
    # (including the API proxy stand-in, which is outside the node subnet: the canary's "public address" probe), cluster DNS.
    expect_open = ["relay_control", "relay_pod_ip_direct", "external_443", "api_proxy_443", "dns_resolves_cluster"]
    if cluster.args.no_cluster:
        # On a real cluster the API server answers on a public address (AKS), TCP 443 outward is open by design, and the `kubernetes` Service is
        # that address after DNAT: the service is as open as the "api proxy" (the worker has no credentials: the canary expects 401/403).
        expect_blocked = [k for k in expect_blocked if k != "api_service_443"]
        expect_open = expect_open + ["api_service_443"]
    wrong_open = [k for k in expect_blocked if seen.get(k) is True]
    wrong_closed = [k for k in expect_open if seen.get(k) is not True]
    ok = out.get("outcome") == "success" and not wrong_open and not wrong_closed
    report.add("E12b", "the agent (uid 10002 in a worker pod, no sandbox) tries the manager's ports, the node's kubelet, the API server (service and node address), the metadata and wire services, plain 80, DNS to the outside, a peer pod", "everything blocked except the relay port, TCP 443 outward and cluster DNS", f"open that must be closed: {wrong_open}; closed that must be open: {wrong_closed}; raw: {json.dumps(seen, sort_keys=True)}", ok, f"session={out.get('session')}")
    report.rows[-1]["observed"] = report.rows[-1]["observed"][:900]
    problems = no_leak(cluster, out)
    report.add("E12c", "nothing leaks after the network session", "no pod, Secret, reservation", "; ".join(problems) or "clean", not problems)
    report.add("E12d", "the canary of the worker passed (it is part of every session: a failed canary is an isolation failure with the breaker tripped)", "no breaker, the launcher not denying", f"stopworld={out.get('stopworld')}", out.get("stopworld") is False)


def pc_relay_host():
    return f"factory-relay-svc.{M}.svc.cluster.local"


def wait_streaming(cluster, session, timeout=120):
    """Until the relay says the agent is running and streaming (the start-up shim, the canary and the fetch are over)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = (cluster.driver("inspect").get("relay") or {}).get(session, {}).get("state")
        if state in ("streaming", "finished"):
            return state
        time.sleep(1)
    raise Fail("the agent never started streaming")


def kill_container_hard(cluster, pod):
    """SIGKILL the worker's container with no warning (what an OOM kill or a lost node looks like to the wrapper). Kind only; elsewhere a force delete."""
    node = cluster.json("-n", W, "get", "pod", pod)["spec"]["nodeName"]
    if cluster.args.no_cluster:
        cluster.kubectl("-n", W, "delete", "pod", pod, "--grace-period=0", "--force", check=False)
        return "force-delete"
    ids = run(["docker", "exec", node, "crictl", "ps", "-q", "--label", f"io.kubernetes.pod.name={pod}"], check=False).stdout.split()
    for ident in ids:
        run(["docker", "exec", node, "crictl", "stop", "-t", "0", ident], check=False)
    return f"crictl stop -t 0 on {len(ids)} container(s)"


def run_kill_pod(cluster, report):
    cluster.driver("breaker-clear")
    cluster.driver_background("killpod", "session", "killpod", "--stub", "mode=sleep seconds=600", "--timeout", "300")
    session = cluster.wait_started("killpod")
    pod = cluster.wait_worker_pod()
    wait_streaming(cluster, session)  # the agent is running and has edited (slept.txt is written before it sleeps)
    how = kill_container_hard(cluster, pod)
    observed = cluster.wait_result("killpod", timeout=400)
    problems = no_leak(cluster, observed)
    text = observed.get("error_text", "")
    # A force delete is not a SIGKILL: the kubelet still sends SIGTERM and gives the wrapper two seconds, so on a real cluster (no crictl) the
    # wrapper may return the delta it had (the same as E13b); only the kind path proves "no delta after a SIGKILL".
    ok = observed.get("outcome") == "error" and not problems and (cluster.args.no_cluster or "slept.txt" not in observed.get("files", {}))
    report.add("E13", "the worker's container is SIGKILLed with no warning in the middle of the session", "a fixed failure text, no hang; NO delta comes back (a pod killed from outside returns none: the worktree keeps what it had); the pod, Secret, reservation and relay record are all released", f"{how}: {observed.get('error_type')}: {text[:140]} files={sorted(observed.get('files', {}))} after {observed.get('elapsed_seconds')}s" + (f"  PROBLEMS: {problems}" if problems else ""), ok, f"session={session}")
    cluster.driver_background("killpod-force", "session", "killpodforce", "--stub", "mode=sleep seconds=600", "--timeout", "300")
    session = cluster.wait_started("killpod-force")
    pod = cluster.wait_worker_pod()
    wait_streaming(cluster, session)
    cluster.kubectl("-n", W, "delete", "pod", pod, "--grace-period=0", "--force", check=False)
    observed = cluster.wait_result("killpod-force", timeout=400)
    problems = no_leak(cluster, observed)
    ok = observed.get("outcome") == "error" and not problems
    report.add("E13c", "the worker pod is force-deleted (`--grace-period=0 --force`) in the middle of the session", "a fixed failure text, no hang, nothing leaks; whether a delta comes back is recorded (the kubelet still sends SIGTERM and waits its minimum grace, so a tiny tree may be returned)", f"{observed.get('error_type')}: {observed.get('error_text', '')[:140]} files={sorted(observed.get('files', {}))}" + (f"  PROBLEMS: {problems}" if problems else ""), ok)
    cluster.driver_background("killpod-term", "session", "killpodterm", "--stub", "mode=sleep seconds=600", "--timeout", "300")
    session = cluster.wait_started("killpod-term")
    pod = cluster.wait_worker_pod()
    wait_streaming(cluster, session)
    t0 = time.monotonic()
    cluster.kubectl("-n", W, "delete", "pod", pod, "--wait=false", check=False)  # the default grace: SIGTERM first
    observed = cluster.wait_result("killpod-term", timeout=400)
    problems = no_leak(cluster, observed)
    ok = observed.get("outcome") == "error" and not problems and "slept.txt" in observed.get("files", {})
    report.add("E13b", "the worker pod is deleted gracefully (SIGTERM, 30 s grace) in the middle of the session", "the wrapper posts `terminated`, the delta comes back (slept.txt applied), a retryable failure `the pod was stopped before the agent finished`; nothing leaks", f"{observed.get('error_type')}: {observed.get('error_text', '')[:140]} files={sorted(observed.get('files', {}))} after {round(time.monotonic() - t0, 1)}s" + (f"  PROBLEMS: {problems}" if problems else ""), ok)


def run_kill_supervisor(cluster, report):
    cluster.driver("breaker-clear")
    cluster.driver_background("killsup", "session", "killsup", "--stub", "mode=sleep seconds=900", "--timeout", "600")
    session = cluster.wait_started("killsup")
    pod = cluster.wait_worker_pod()
    time.sleep(5)
    # 1. the supervisor is ALIVE: the sweep must leave the running session alone, however many passes it makes.
    live = cluster.driver("sweep", "killsup", "--gone-after", "2", "--passes", "3", "--pause", "4")
    still = pod in cluster.worker_objects()["pods"]
    nothing = all(not p["deleted"] and not p["released"] for p in live.get("sweep", [])) and still
    report.add("E14a", "the orphan sweep (the manager's podsweep, 3 passes, gone-after shortened to 2 s) runs while the supervisor is ALIVE", "the live session is not touched: nothing deleted, nothing released, the pod still there", f"passes={json.dumps(live.get('sweep'), sort_keys=True)[:300]} pod_still_there={still}", nothing)
    # 2. the supervisor is killed hard.
    cluster.exec_main("sh", "-c", "kill -9 $(cat /data/e2e/killsup/driver.pid)", check=False, timeout=60)
    time.sleep(3)
    held = cluster.worker_objects()
    report.add("E14b", "the supervisor process is killed (SIGKILL) mid-session", "the pod outlives it (that is the design: a pod outlives its launcher)", f"pods={held['pods']} secrets={len(held['secrets'])}", pod in held["pods"])
    first = cluster.driver("sweep", "killsup", "--gone-after", "2", "--passes", "1")
    time.sleep(3)
    second = cluster.driver("sweep", "killsup", "--gone-after", "2", "--passes", "2", "--pause", "3")
    deleted = sum(sum(p["deleted"].values()) for p in (first.get("sweep", []) + second.get("sweep", [])))
    gone = cluster.wait_workers_gone(timeout=120)
    report.add("E14c", "the sweep passes after the supervisor is gone (first pass starts the silence clock, a later one acts)", "the pod is deleted by reason supervisor_gone, then the Secret goes with it", f"deleted={deleted} first={first.get('sweep')} second={second.get('sweep')} namespace after={gone}", deleted >= 1 and not gone["pods"] and not gone["secrets"])
    recovered = cluster.driver("recover", "killsup")
    inspect = cluster.driver("inspect")
    report.add("E14d", "recovery over the dead supervisor's state (State.recover through the launcher's reader)", "the recorded pod reads dead; the job is marked interrupted; no PodStillLive", f"{json.dumps({k: recovered.get(k) for k in ('job_before', 'job_after', 'recovered', 'recover_error', 'pod_status')}, default=str)}", recovered.get("recover_error") is None and recovered.get("job_after") == "interrupted")
    report.add("E14e", "the pool released the dead supervisor's reservation", "in_use 0, holders 0", json.dumps(inspect.get("pool")), (inspect.get("pool") or {}).get("in_use") == 0)
    report.add("E14f", "the relay record of the killed session ended", "revoked or released (the sweep revokes before it deletes)", json.dumps(inspect.get("relay", {}).get(session)), bool(inspect.get("relay", {}).get(session, {}).get("revoked") or inspect.get("relay", {}).get(session, {}).get("released")))
    # The driver's own output file for the killed supervisor is gone with it; nothing else to wait for.


def run_admission(cluster, report):
    pod = cluster.manager_pod
    mutations = ["host-network", "privileged", "other-image", "host-path", "service-account-token", "host-users-true", "extra-capability", "node-name", "command", "rbac-secret-read"]
    done = cluster.kubectl("-n", M, "exec", "-i", pod, "-c", "launcher", "--", "/opt/factory/bin/python", "-", *mutations, input=(HERE / "launcher_probe.py").read_text(), check=False, timeout=180)
    lines = [line for line in done.stdout.splitlines() if line.startswith("PROBE ")]
    admitted = [line for line in lines if " ADMITTED " in line]
    denied = {line.split()[1]: line for line in lines if " ADMITTED " not in line}
    policy_named = [k for k, v in denied.items() if "Denied" in v or "rules=" in v]
    pod_variants = [k for k in denied if not k.startswith("rbac")]
    report.add("E15a", f"with the LAUNCHER's own token, create a pod that breaks one rule of the policy ({len(mutations) - 1} variants through the real client)", "every one denied by the API server (none admitted)", f"admitted={len(admitted)} denied={len(pod_variants)} of {len(mutations) - 1}: " + "; ".join(f"{k}->{denied[k].split(' ', 2)[2][:44]}" for k in pod_variants), not admitted and len(pod_variants) == len(mutations) - 1, done.stderr[-200:])
    named = [k for k in pod_variants if "Denied:403" in denied[k] or "Denied:422" in denied[k]]
    report.add("E15b", "the client reads the denial as a denial, not as bad credentials", "each answer is `Denied` (403 from the policy, 422 from the API server's own validation), never `ConfigError`", f"{len(named)} of {len(pod_variants)} are Denied; others: {[denied[k][:60] for k in pod_variants if k not in named]}", len(named) == len(pod_variants))
    rbac = {k: v for k, v in denied.items() if k.startswith("rbac")}
    report.add("E15c", "with the same token, read Secrets, read a pod log, list the manager namespace's pods, list nodes", "all four 403 (the Role has create/get/list/delete on pods and create/delete on secrets only)", "; ".join(f"{k}={v.split()[2]}" for k, v in rbac.items()), len(rbac) == 4 and all(v.split()[2] == "403" for v in rbac.values()))
    held = cluster.worker_objects()
    report.add("E15d", "no pod or Secret was created by the attempts", "namespace empty", str(held), not held["pods"] and not held["secrets"])


def run_pool(cluster, report):
    cluster.driver("breaker-clear")
    names = ["pool1", "pool2", "pool3"]
    for name in names:
        cluster.driver_background(name, "session", name, "--stub", "mode=sleep seconds=20", "--timeout", "120")
    peak = 0
    deadline = time.monotonic() + 420
    results = {}
    while time.monotonic() < deadline and len(results) < len(names):
        peak = max(peak, len(cluster.worker_objects()["pods"]))
        for name in names:
            if name not in results and "E2E_RESULT" in cluster.driver_output(name):
                results[name] = parse_driver(cluster.driver_output(name), "")
        time.sleep(2)
    outcomes = {name: (results.get(name) or {}).get("outcome") for name in names}
    report.add("E16a", f"three sessions started at once against a ceiling of {MAX_SESSIONS}", f"never more than {MAX_SESSIONS} worker pods at any moment; the third waits for capacity and then runs; all three end", f"peak live pods={peak}; outcomes={outcomes}; elapsed={[ (results.get(n) or {}).get('elapsed_seconds') for n in names]}", peak <= MAX_SESSIONS and len(results) == 3, "the slowest waited for the pool, not for a pod")
    held = cluster.wait_workers_gone()
    inspect = cluster.driver("inspect")
    report.add("E16b", "after all three: nothing leaks", "no pod, Secret, reservation", f"namespace={held} pool={inspect.get('pool')}", not held["pods"] and not held["secrets"] and (inspect.get("pool") or {}).get("in_use") == 0)
    refusal = cluster.driver("pool-refuse")
    report.add("E16c", "ask the pool directly for more than the ceiling (real ledger on the real volume)", "the admission over the ceiling is refused with the code `ceiling` (waiting), the per-repository cap and the pacing refuse with their codes; every granted reservation is given back", json.dumps(refusal.get("refusals", refusal), sort_keys=True)[:400], refusal.get("ok") is True)


def run_s1(cluster, report):
    cluster.driver("breaker-clear")
    cluster.driver_background("s1", "session", "s1", "--stub", "mode=sleep seconds=240", "--timeout", "300")
    cluster.wait_started("s1")
    pod = cluster.wait_worker_pod()
    time.sleep(3)
    s1 = cluster.kubectl("-n", W, "exec", pod, "-c", "worker", "--", "bash", "/opt/factory-spikes/s1_bwrap.sh", check=False, timeout=180)
    report.add("E17a", "S1 part 1 (the image's s1_bwrap.sh) inside a real worker pod: hostUsers false, procMount Unmasked, AppArmor Unconfined, the pod's capability set", "every check PASS (bubblewrap can create the namespaces the sandbox needs)", (s1.stdout.strip().splitlines()[-1] if s1.stdout.strip() else s1.stderr[-200:]) + f" (exit {s1.returncode})", s1.returncode == 0, scrub(s1.stdout)[-1500:])
    report.rows[-1]["full"] = scrub(s1.stdout)
    e2e = cluster.kubectl("-n", W, "exec", pod, "-c", "worker", "--", "/usr/local/bin/factory-sandbox-check", check=False, timeout=400)
    report.add("E17b", "tools/sandbox_e2e.py (`factory-sandbox-check`: the real claude binary against a local FAKE API, no key, no real model) inside the same pod", "every check PASS", (e2e.stdout.strip().splitlines()[-1] if e2e.stdout.strip() else e2e.stderr[-200:]) + f" (exit {e2e.returncode})", e2e.returncode == 0, scrub(e2e.stdout)[-1500:])
    report.rows[-1]["full"] = scrub(e2e.stdout + e2e.stderr)
    cluster.kubectl("-n", W, "delete", "pod", pod, "--wait=false", check=False)
    cluster.wait_result("s1", timeout=300)
    cluster.wait_workers_gone()


IDENTITY_PROBE = r"""
import errno, json, os, socket
out = {}
def attempt(label, fn):
    try:
        fn()
        out[label] = "ok"
    except OSError as error:
        out[label] = errno.errorcode.get(error.errno, str(error.errno))
def connect():
    sock = socket.socket(socket.AF_UNIX)
    sock.settimeout(3)
    try:
        sock.connect("/run/factory/launcher.sock")
    finally:
        sock.close()
def create(path):
    def go():
        with open(path, "w"):
            pass
        os.unlink(path)
    return go
attempt("connect_launcher_socket", connect)
attempt("list_socket_dir", lambda: os.listdir("/run/factory"))
attempt("create_in_socket_dir", create("/run/factory/x-probe"))
attempt("unlink_the_socket", lambda: os.unlink("/run/factory/launcher.sock"))
attempt("list_spool", lambda: os.listdir("/spool"))
attempt("create_in_spool", create("/spool/x-probe"))
attempt("read_service_account_token", lambda: open("/var/run/secrets/kubernetes.io/serviceaccount/token").read(1))
out["uid"] = os.getuid()
out["groups"] = sorted(os.getgroups())
print("IDENTITY " + json.dumps(out, sort_keys=True))
"""


def run_identities(cluster, report):
    pod = cluster.manager_pod
    worker = cluster.kubectl("-n", M, "exec", "-i", pod, "-c", "factory", "--", "setpriv", f"--reuid={pc.WORKER_UID}", f"--regid={pc.WORKER_GID}", "--clear-groups", "--inh-caps=-all", "--ambient-caps=-all", "--no-new-privs", "/opt/factory/bin/python", "-", input=IDENTITY_PROBE, check=False, timeout=60)
    seen = json.loads(worker.stdout.split("IDENTITY ", 1)[1]) if "IDENTITY " in worker.stdout else {}
    refused = all(seen.get(k) == "EACCES" for k in ("connect_launcher_socket", "list_socket_dir", "create_in_socket_dir", "unlink_the_socket", "list_spool", "create_in_spool"))
    report.add("E20a", "a process with the WORKER uid (10002, no groups, no capabilities) inside the manager's main container tries the launcher socket, the socket directory and the spool", "every one refused (EACCES): the directories are 0750 and 2770 for group 10001", json.dumps(seen, sort_keys=True), refused and seen.get("read_service_account_token") == "ENOENT", "the main container mounts no service account token")
    supervisor = cluster.exec_main("/opt/factory/bin/python", "-", input=IDENTITY_PROBE, check=False, timeout=60)
    seen = json.loads(supervisor.stdout.split("IDENTITY ", 1)[1]) if "IDENTITY " in supervisor.stdout else {}
    ok = seen.get("uid") == pc.SUPERVISOR_UID and seen.get("connect_launcher_socket") == "ok" and seen.get("list_socket_dir") == "ok" and seen.get("create_in_socket_dir") in ("EROFS", "EACCES") and seen.get("unlink_the_socket") in ("EROFS", "EACCES") and seen.get("list_spool") == "ok" and seen.get("create_in_spool") == "ok" and seen.get("read_service_account_token") == "ENOENT"
    report.add("E20b", "the SUPERVISOR identity (uid 10001 through the entrypoint) in the same container: the same attempts", "connects to the socket and reads and writes the spool; cannot create or unlink in the socket directory (read-only mount); holds no Kubernetes token", json.dumps(seen, sort_keys=True), ok)
    launcher = cluster.kubectl("-n", M, "exec", pod, "-c", "launcher", "--", "/opt/factory/bin/python", "-c", "import os;print(os.getuid(), os.path.exists('/var/run/secrets/kubernetes.io/serviceaccount/token'), sorted(k for k in os.environ if k.startswith('AZURE_')))", check=False, timeout=60)
    relay = cluster.kubectl("-n", M, "exec", pod, "-c", "relay", "--", "/opt/factory/bin/python", "-c", "import os;print(os.getuid(), os.path.exists('/var/run/secrets/kubernetes.io/serviceaccount/token'), sorted(k for k in os.environ if k.startswith('AZURE_') or k == 'ANTHROPIC_API_KEY'))", check=False, timeout=60)
    report.add("E20c", "who holds the Kubernetes token: the launcher container yes, the relay container no; neither has an AZURE_* variable; the relay has no Anthropic key", f"launcher: `{pc.LAUNCHER_UID} True []`; relay: `{pc.RELAY_UID} False []`", f"launcher: `{launcher.stdout.strip()}`; relay: `{relay.stdout.strip()}`", launcher.stdout.strip() == f"{pc.LAUNCHER_UID} True []" and relay.stdout.strip() == f"{pc.RELAY_UID} False []")


def run_runbook_probes(cluster, report):
    """The probes of the runbook's G2, G3 and G5 (tools/live_pod_probe.py), exactly as the runbook runs them, on this cluster."""
    probe = (ROOT / "tools" / "live_pod_probe.py").read_text()
    pod = cluster.manager_pod

    def exec_probe(namespace, target, container, *args):
        argv = ["-n", namespace, "exec", "-i", target]
        argv += ["-c", container] if container else []
        done = cluster.kubectl(*argv, "--", "python3", "-", *args, input=probe, check=False, timeout=120)
        return done.returncode, scrub((done.stdout + done.stderr).strip())

    cases = [
        ("E21a", "G2 socket as the worker identity (uid 10002, gid 10010)", ("factory", "socket", "--expect", "worker", "--as-uid", "10002", "--as-gid", "10010")),
        ("E21b", "G2 socket as the supervisor (uid 10001, gid 10001)", ("factory", "socket", "--expect", "supervisor", "--as-uid", "10001", "--as-gid", "10001")),
        ("E21c", "G2 spool as the worker identity", ("factory", "spool", "--expect", "worker", "--as-uid", "10002", "--as-gid", "10010")),
        ("E21d", "G2 environment of the relay container", ("relay", "environment")),
        ("E21e", "G2 environment of the launcher container", ("launcher", "environment")),
    ]
    for ident, step, (container, *args) in cases:
        code, text = exec_probe(M, pod, container, *args)
        report.add(ident, f"runbook {step}", "exit 0, every line `ok`", f"exit {code}: {' | '.join(text.splitlines()[-6:])}"[:600], code == 0)
    code, text = exec_probe(M, pod, "launcher", "statuses", "--bodies")
    cases_seen = {}
    try:
        for entry in json.loads(text.splitlines()[-1])["cases"]:
            cases_seen[entry["case"]] = entry["http"]
    except (ValueError, KeyError, IndexError):
        pass
    report.add("E21f", "runbook G3 `statuses` in the launcher container (its own token, the real API server)", "404 for a missing pod, 403 for a Secret list, 401 for an invalid token, 403 for a policy denial (the 422 the client had assumed was wrong: D1)", f"exit {code}: {cases_seen}", code == 0 and cases_seen.get("not_found") == 404 and cases_seen.get("forbidden") == 403 and cases_seen.get("unauthorized") == 401 and cases_seen.get("denied") in (403, 422))
    cluster.driver("breaker-clear")
    cluster.driver_background("probes", "session", "probes", "--stub", "mode=sleep seconds=90", "--timeout", "120")
    cluster.wait_started("probes")
    worker = cluster.wait_worker_pod()
    time.sleep(3)
    for ident, step, args in (("E21g", "G5 secret-volume as uid 10002", ("secret-volume", "--as-uid", "10002", "--as-gid", "10010")), ("E21h", "G5 environment of a worker pod", ("environment", "--expect", "worker-pod"))):
        code, text = exec_probe(W, worker, None, *args)
        report.add(ident, f"runbook {step}", "exit 0, every line `ok`", f"exit {code}: {' | '.join(text.splitlines()[-8:])}"[:700], code == 0)
    tree = cluster.kubectl("-n", W, "exec", worker, "--", "ls", "-lnd", "/data/e2e/probes/wt", check=False)
    for _ in range(40):  # the start-up shim and the fetch come first: wait for the wrapper to lay the tree out
        if tree.returncode == 0:
            break
        time.sleep(2)
        tree = cluster.kubectl("-n", W, "exec", worker, "--", "ls", "-lnd", "/data/e2e/probes/wt", check=False)
    report.add("E21i", "runbook G5 the worktree is owned by the worker identity (the wrapper's chown under hostUsers false)", f"owner {pc.WORKER_UID}:{pc.WORKER_GID}", tree.stdout.strip(), f" {pc.WORKER_UID} {pc.WORKER_GID} " in tree.stdout)
    cluster.kubectl("-n", W, "delete", "pod", worker, "--wait=false", check=False)
    cluster.wait_result("probes", timeout=300)
    cluster.wait_workers_gone()


def run_one_repo_per_node(cluster, report):
    """The required pod anti-affinity: a node that holds a pod of another repository is refused, so with ONE worker-pool node two repositories take turns."""
    cluster.driver("breaker-clear")
    cluster.driver_background("repoa", "session", "repoa", "--stub", "mode=sleep seconds=25", "--timeout", "120", "--repo", "e2e/alpha")
    cluster.wait_started("repoa")
    cluster.driver_background("repob", "session", "repob", "--stub", "mode=sleep seconds=5", "--timeout", "120", "--repo", "e2e/beta")
    cluster.wait_started("repob")
    seen, overlap, pending_seen = [], False, False
    deadline = time.monotonic() + 240
    done = {}
    while time.monotonic() < deadline and len(done) < 2:
        phases = sorted(p["status"].get("phase") for p in cluster.json("-n", W, "get", "pods")["items"])
        seen.append(phases)
        overlap = overlap or phases.count("Running") > 1
        pending_seen = pending_seen or "Pending" in phases
        for name in ("repoa", "repob"):
            if name not in done and "E2E_RESULT" in cluster.driver_output(name):
                done[name] = parse_driver(cluster.driver_output(name), "")
        time.sleep(2)
    outcomes = {k: v.get("outcome") for k, v in done.items()}
    report.add("E19", "two repositories start together on a cluster with ONE worker-pool node (required anti-affinity: one repository per node)", "the second pod waits Pending while the first runs and never runs beside it; both sessions end", f"pending_seen={pending_seen} two_running_at_once={overlap} outcomes={outcomes}", pending_seen and not overlap and len(done) == 2 and all(v == "success" for v in outcomes.values()) or (pending_seen and not overlap and len(done) == 2))
    cluster.wait_workers_gone()


def run_isolation(cluster, report):
    """A negative control of the whole fence: with the default-deny policy REMOVED, the canary must fail the session (the product's answer to a leak)."""
    cluster.driver("breaker-clear")
    cluster.kubectl("-n", W, "delete", "networkpolicy", "default-deny")
    time.sleep(8)  # the engine takes a moment to drop its rules
    first = cluster.driver("session", "iso1", "--stub", "mode=ok", "--timeout", "90")
    cluster.apply(cluster.render("netpol"))  # the fence is back before anything else runs
    problems = no_leak(cluster, first)
    ok = first.get("outcome") == "error" and "isolation check failed (reach_" in first.get("error_text", "") and first.get("stopworld") is True and (first.get("relay") or {}).get("reject") == "preflight_failed" and not problems
    report.add("E18a", "negative control: the default-deny NetworkPolicy is removed and a session is started", "the network canary fails the session at its start (`reach_kubelet` or another forbidden destination), the bundle is never served, the stop-the-world breaker is tripped, the pod is gone", f"{first.get('error_type')}: {first.get('error_text', '')[:170]} reject={(first.get('relay') or {}).get('reject')} breaker={first.get('stopworld')} files={sorted(first.get('files', {}))}" + (f"  PROBLEMS: {problems}" if problems else ""), ok, f"session={first.get('session')}")
    second = cluster.driver("session", "iso2", "--stub", "mode=ok", "--timeout", "90")
    report.add("E18b", "the next session while the breaker is tripped (the fence is back)", "refused before any pod, with the breaker's fixed text", f"{second.get('error_type')}: {second.get('error_text', '')[:170]} pods={second.get('pods_after')}", second.get("outcome") == "error" and "breaker is tripped" in second.get("error_text", "") and (second.get("pods_after") or {}).get("count") == 0)
    cluster.driver("breaker-clear")
    third = cluster.driver("session", "iso3", "--stub", "mode=ok", "--timeout", "90")
    report.add("E18c", "the breaker is cleared but the launcher was told `deny` (isolation_failed)", "the launcher still refuses every launch until it restarts (a fixed rejection, no pod)", f"{third.get('error_type')}: {third.get('error_text', '')[:170]} pods={third.get('pods_after')}", third.get("outcome") == "error" and (third.get("pods_after") or {}).get("count") == 0 and "success" != third.get("outcome"))
    cluster.kubectl("-n", M, "rollout", "restart", "deploy/factory")
    cluster.kubectl("-n", M, "rollout", "status", "deploy/factory", "--timeout=240s", timeout=260)
    cluster.manager_pod = cluster.find_manager()
    cluster.push_driver()
    time.sleep(8)
    fourth = cluster.driver("session", "iso4", "--stub", "mode=ok", "--timeout", "90")
    report.add("E18d", "after the manager pod restarts (the launcher forgets the denial) and the fence is back", "the next session runs to the end", f"{fourth.get('outcome')} {fourth.get('error_text', '')[:120]}", fourth.get("outcome") == "success" and not no_leak(cluster, fourth))


# ---- the kind path ----------------------------------------------------------------------------------------------------


def up(cluster, args):
    scratch = Path(args.scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, KIND_EXPERIMENTAL_DOCKER_NETWORK=NETWORK)
    if run(["docker", "network", "inspect", NETWORK], check=False).returncode:
        run(["docker", "network", "create", NETWORK, "--subnet", SUBNET, "--ip-range", IP_RANGE, "--gateway", GATEWAY])
    clusters = run(["kind", "get", "clusters"], check=False).stdout.split()
    if CLUSTER not in clusters:
        config = scratch / "kind.yaml"
        config.write_text((HERE / "kind.yaml").read_text())
        run(["kind", "create", "cluster", "--name", CLUSTER, "--image", NODE_IMAGE, "--config", str(config), "--kubeconfig", cluster.kubeconfig, "--wait", "180s"], env=env, timeout=600)
    else:
        run(["kind", "export", "kubeconfig", "--name", CLUSTER, "--kubeconfig", cluster.kubeconfig], env=env)
    cluster.kubectl("wait", "--for=condition=Ready", "node", "--all", "--timeout=180s", timeout=200)
    workers = [f"{CLUSTER}-worker", f"{CLUSTER}-worker2"]
    cluster.kubectl("label", "node", workers[0], f"{MANAGER_NODE_LABEL[0]}={MANAGER_NODE_LABEL[1]}", "--overwrite")
    cluster.kubectl("label", "node", workers[1], f"{WORKER_NODE_LABEL[0]}={WORKER_NODE_LABEL[1]}", "--overwrite")
    cluster.kubectl("taint", "node", workers[0], f"{pc.MANAGER_TAINT_KEY}={pc.MANAGER_TAINT_VALUE}:{pc.TAINT_EFFECT}", "--overwrite")
    cluster.kubectl("taint", "node", workers[1], f"{pc.WORKER_TAINT_KEY}={pc.WORKER_TAINT_VALUE}:{pc.TAINT_EFFECT}", "--overwrite")
    if not cluster.image:
        cluster.image = build_and_load(args, scratch, workers)
    control_plane = f"{CLUSTER}-control-plane"
    address = run(["docker", "inspect", control_plane, "--format", "{{(index .NetworkSettings.Networks \"" + NETWORK + "\").IPAddress}}"]).stdout.strip()
    run(["docker", "rm", "-f", API_PROXY], check=False)
    run(["docker", "run", "-d", "--name", API_PROXY, "--network", NETWORK, "--ip", API_PROXY_IP, "--entrypoint", "socat", args.base_image, "TCP-LISTEN:443,fork,reuseaddr", f"TCP:{address}:6443"])
    cluster.apply_objects()
    (scratch / "image.ref").write_text(cluster.image + "\n")
    return cluster.image


def build_and_load(args, scratch, workers):
    tag = "factory-podspike:stub"
    run(["docker", "build", "-q", "--build-arg", f"BASE={args.base_image}", "-t", tag, "-f", str(HERE / "Dockerfile.stub"), str(HERE)], timeout=900)
    archive = scratch / "stub.tar"
    run(["docker", "save", tag, "-o", str(archive)], timeout=600)
    digest = None
    for node in workers:
        with open(archive, "rb") as handle:
            done = subprocess.run(["docker", "exec", "-i", node, "ctr", "-n", "k8s.io", "images", "import", "--snapshotter", "overlayfs", "-"], stdin=handle, capture_output=True, text=True, timeout=600)
        if done.returncode:
            raise Fail("image import failed: " + done.stderr[-300:])
        listing = run(["docker", "exec", node, "ctr", "-n", "k8s.io", "images", "ls", f"name==docker.io/library/{tag}"]).stdout.splitlines()
        digest = listing[1].split()[2]
        run(["docker", "exec", node, "ctr", "-n", "k8s.io", "images", "tag", "--force", f"docker.io/library/{tag}", f"docker.io/library/factory-podspike@{digest}"])
    return f"docker.io/library/factory-podspike@{digest}"


def down(cluster, args):
    env = dict(os.environ, KIND_EXPERIMENTAL_DOCKER_NETWORK=NETWORK)
    run(["kind", "delete", "cluster", "--name", CLUSTER, "--kubeconfig", cluster.kubeconfig], check=False, env=env, timeout=300)
    run(["docker", "rm", "-f", API_PROXY], check=False)
    run(["docker", "network", "rm", NETWORK], check=False)


def write_report(report, out):
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(report.rows, indent=1) + "\n")
    lines = ["| Id | Step | Expected | Observed | Status |", "|---|---|---|---|---|"]
    for row in report.rows:
        cells = [row["id"], row["step"], row["expected"], row["observed"], row["status"]]
        lines.append("| " + " | ".join(str(c).replace("|", "\\|").replace("\n", " ") for c in cells) + " |")
    (out / "results.md").write_text("\n".join(lines) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("up", "run", "down", "all"))
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--image", default=None)
    parser.add_argument("--base-image", default="factory-podspike:local")
    parser.add_argument("--no-cluster", action="store_true")
    parser.add_argument("--scratch", default=str(Path(os.environ.get("TMPDIR", "/tmp")) / "pod-e2e-scratch"))
    parser.add_argument("--out", default=None)
    parser.add_argument("--only", nargs="*", default=[])
    args = parser.parse_args(argv)
    if "kube/config" in str(Path(args.kubeconfig).resolve()) and not args.no_cluster:
        sys.stderr.write("refusing: the kind path never uses ~/.kube/config\n")
        return 2
    cluster = Cluster(args.kubeconfig, args)
    report = Report()
    out = Path(args.out or Path(args.scratch) / "out")
    status = 0
    try:
        if args.command in ("up", "all") and not args.no_cluster:
            up(cluster, args)
        elif args.no_cluster:
            if not cluster.image:
                raise Fail("--no-cluster needs --image")
            cluster.manager_pod = cluster.find_manager()
        if args.command in ("run", "all"):
            if cluster.image is None:
                ref = Path(args.scratch) / "image.ref"
                cluster.image = ref.read_text().strip() if ref.exists() else None
            if cluster.manager_pod is None:
                cluster.manager_pod = cluster.find_manager()
            run_scenarios(cluster, report, set(args.only))
            write_report(report, out)
            status = 0 if all(row["status"] == "PASS" for row in report.rows) else 1
    except (Fail, subprocess.TimeoutExpired) as error:
        sys.stderr.write(f"harness: {scrub(str(error))}\n")
        status = 2
        if report.rows:
            write_report(report, out)
    finally:
        if args.command == "all" and not args.no_cluster:
            down(cluster, args)
        elif args.command == "down" and not args.no_cluster:
            down(cluster, args)
    return status


if __name__ == "__main__":
    sys.exit(main())

# Pod backend: the first end-to-end run, on a local kind cluster

Status: **run once, on one machine, on kind, with a stub agent.** This is the first time a pod of the pod backend (issues #144 to #147,
`docs/pod-backend.md`) ever ran: until this page, every claim about a pod was "contract-tested against assumed shapes". It follows the method
of the AKS spikes (`spikes/S1.md`, `spikes/README.md`) and the shape of
`docs/pod-backend-runbook.md` Parts E and G, but **no Azure, no AKS, no Terraform and no Anthropic API was involved**: the cluster is a
kind cluster on a local machine, the objects are a hand translation of what Terraform renders, and the agent in the worker pod is a stub.
It settles what a kind cluster can settle and says, row by row, what it cannot.

## What ran, and what did not

| Ran for real | Did NOT run (so nothing below says anything about it) |
|---|---|
| The image of the repository's `Dockerfile`, built from this tree, plus one test-only layer (the stub agent and a start-up shim, below) | Azure: AKS, Cilium, node auto-provisioning, Karpenter, the Azure Disk, Key Vault, the workload identity webhook, Container Insights |
| A kind cluster of three nodes (control plane, a manager node, a worker-pool node) with `UserNamespacesSupport` and `ProcMountType` on, the pinned node image `kindest/node:v1.36.1@sha256:3489c767...` (containerd 2.3.1), and kindnet's NetworkPolicy enforcer | Cilium's `ipBlock` semantics (kindnet's engine is another implementation of the same API: D1) |
| The real objects of the fence: namespaces, default-deny NetworkPolicy, the four admission policy files of `infra/policy/` unchanged, the parameter ConfigMap, the launcher Role and RoleBinding | `terraform plan` or `apply`: the objects are a translation (`tools/local_pod_e2e/render.py`); where it had to guess it says `DEVIATION` |
| The manager pod with `launcher`, `relay`, the init container, the shared `emptyDir`s, the projected token only in the launcher, both Services; the **real** `factory.launcher`, `factory.relay` and `factory.podcontract` | The manager itself (`factory.manager`), the runner, a real repository, GitHub, the real `claude` CLI against a real model, a real key (a placeholder string stands in) |
| The real `PodBackend` path: `podexecution.activate`, `agent_kwargs`, `agent.run_agent`, `podpool`, `bundle.stage`, `podhandle`, `podliveness`, `podsweep` over the real launcher socket, the real relay, the real wrapper (`factory.podworker`) in a real pod (`hostUsers: false`, `procMount: Unmasked`, AppArmor `Unconfined`) | A real repository's size and time, NFS or Azure Disk behaviour (the data volume is an `emptyDir` here), more than one node of the worker pool, node provisioning delays, spot or eviction behaviour |
| Failure modes: timeout, non-zero exit, hostile delta, hostile agent probes, pod killed, supervisor killed, the sweep, recovery, admission denial with the launcher's own token, pool ceiling and pacing, an isolation failure | The spend limit, 401 and 429 shapes of `claude` (runbook G8), the `claude` sandbox through a real model |

**The stub agent** (`tools/local_pod_e2e/stub-claude`, baked into the test layer as `/usr/local/bin/stub-claude`, selected through the
supervisor's `FACTORY_AGENT_CMD`) calls no API and holds no key. It reads the prompt, takes its behaviour from the first line, and writes a
valid stream-json transcript with a `result` event. It runs **without** the `claude` sandbox: it is a deliberately worse agent than the real
one, so a probe that fails here fails for the pod's reasons (uid, mounts, network policy), not because `bwrap` stopped it.

## How to run it again

```sh
# needs docker, kind, kubectl, python3; about 25 minutes; touches nothing but its own docker network, containers and kind cluster
docker build -t factory-podspike:local .
PYTHONPATH=src python3 tools/local_pod_e2e/harness.py all --kubeconfig /tmp/podspike-kubeconfig
# or in steps: up, run [--only E1 E4 ...], down
# against a cluster that a person built (objects and image in place; nothing is created or deleted there):
PYTHONPATH=src python3 tools/local_pod_e2e/harness.py run --no-cluster --kubeconfig KC --image REGISTRY/REPO@sha256:...
```

The harness never reads `~/.kube/config` (every kubectl call carries `--kubeconfig`), makes no Azure call, and `down` removes the kind cluster, the
docker network `podspike-net` and the container `podspike-apiproxy`. `tests/test_local_pod_e2e.py` pins the renderer against `podcontract` and the
Terraform files (names, ids, paths, verbs, the three egress rules and their excluded ranges), so the translation cannot drift silently.

## The environment, and every deviation from the real design

| Item | Value |
|---|---|
| Date | 2026-11-10 |
| Host | a Linux machine under WSL2, with Docker (20 CPUs, 31 GB) |
| kind | v0.29.0 (the earlier run of the admission matrix on kind used 0.32.0; see V2) |
| Node image | `kindest/node:v1.36.1@sha256:3489c7674813ba5d8b1a9977baea8a6e553784dab7b84759d1014dbd78f7ebd5`, Kubernetes v1.36.1, containerd 2.3.1, feature gates `UserNamespacesSupport` and `ProcMountType` (the two the admission policies need: `tools/local_pod_e2e/kind.yaml`) |
| CNI and NetworkPolicy enforcer | kindnet `docker.io/kindest/kindnetd:v20260528-9350166c`, whose own engine enforces NetworkPolicy through nftables and NFQUEUE (V1) |
| Tree under test | the integrated pod backend at the commit that added this page |
| Image | the repository's `Dockerfile` (Claude Code 2.1.318, bubblewrap 0.8.0 from the image, git, gh, terraform) plus `tools/local_pod_e2e/Dockerfile.stub`; referenced in the cluster as `docker.io/library/factory-podspike@sha256:<index digest>` |
| Nodes | `podspike-control-plane`; `podspike-worker` (label `pool=factory`, taint `workload=manager:NoSchedule`: the manager); `podspike-worker2` (label `pool=worker`, taint `workload=worker:NoSchedule`: the worker pool) |
| Network | docker network `podspike-net` 172.31.0.0/16 (nodes in 172.31.0.0/24), pod CIDR 10.244.0.0/16, service CIDR 10.96.0.0/16: the three ranges the egress rule excludes |
| Pool | `FACTORY_MAX_POD_SESSIONS=2`, per-repository cap 4, the contract's pacing (burst 2, one token per 15 s) |

**Deviations (V), each one a place where the run is not the production design:**

* **V1, the NetworkPolicy enforcer is kindnet's, not Cilium and not Calico.** Calico was the first choice, and the reference deployment uses Cilium. Both were tried and
  **neither can start on this host**: this WSL2 kernel cannot auto-load a kernel module (`call_usermodehelper_exec` never returns: `ip link add t type
  dummy`, `type ipip` and `type wireguard` hang for minutes inside a node, and so did Calico's felix, which probes wireguard, and Cilium's agent,
  which needs `sch_ingress` and `cls_bpf`). kind's default CNI, kindnet, enforces NetworkPolicy with modules that were already loaded, so it was used. The
  engine is a different implementation of the same API: **nothing here settles Cilium's `ipBlock.except` or its DNS rule** (the UNVERIFIED items of slice 15a stay open).
* **V2, the kind CLI is 0.29.0, not the 0.32.0 of the earlier matrix run.** `kind load` fails against this node image (`unknown containerd config version: 4`), so the harness imports
  the image into the two worker nodes with `ctr images import` and tags it by its digest. A newer kind binary was not used.
* **V3, the image is not pulled from a registry.** The reference is `docker.io/library/factory-podspike@sha256:<digest>`, present in each node's containerd; the admission policy's
  parameter and `FACTORY_LAUNCHER_IMAGE` carry the same string, so the policy still compares a pinned digest reference.
* **V4, a start-up shim in the test layer.** kindnet starts enforcing about **one second after a container starts** (measured: a pod that connects to the kubelet port every 250 ms
  succeeds for the first four attempts). The first real session failed its own network canary for that reason (D3). The worker command is pinned by the admission policy (`tini`, `python`, `-m factory.podworker`),
  so the test layer renames `/usr/bin/tini` to `tini.real` and puts a shim in its place that sleeps `E2E_ENGINE_SETTLE_SECONDS` (5) before it executes the real binary. The canary,
  the podspec, the policy and the wrapper are untouched; E18 shows the canary still fails a session when the fence is really missing.
* **V5, the manager is a stand-in.** The Deployment has the three containers, the init container, the volumes and the Services of `pod_backend.tf`, but the main container runs
  the image's entrypoint (so it drops to uid 10001 with the three ambient capabilities, as the manager does) with a small program that listens on 8080 and 9464, so the canary's "manager" targets
  are real listeners. The driver runs in it through `kubectl exec` as the supervisor identity. The data volume is an `emptyDir` (not the Azure Disk), the Anthropic key is a placeholder
  string in a plain Secret (not Key Vault), and there is no `azure.workload.identity/use` label and so no skip annotation.
* **V6, one node in the worker pool.** No Karpenter and no node provisioning: the pool is one labelled and tainted node. The required anti-affinity is honoured by the ordinary scheduler (E19); whether NAP honours it is not shown.
* **V7, the API server's "public address".** kind has none. A container `podspike-apiproxy` (`socat`, 172.31.1.10:443, outside the node subnet) forwards to the API server, so the canary's
  `public` probe reaches a real API server over TCP 443 and is answered 403 to an anonymous request, which is the pass it is designed to give.
* **V8, the sweep's two callables.** `podsweep.Sweeper` is the real one over the real launcher and spool, but `recorded` (the job states) and `supervisor_live` (a live control record) are
  the manager's `Operations` methods, which need a configured manager. The driver supplies the same shapes: `podsweep.read_recorded` over the run's real `state.json` and a pid probe. `gone_after` is 2 s here (the product's is 300 s).
* **V9, the pool ceiling is 2** (the product derives it from the NodePool's limit) so that the ceiling can be reached with three sessions.

## Results

One complete run of `harness.py all` from nothing (no cluster, no docker network) to nothing, on 2026-11-10: **50 of 50 checks passed**
(none failed). The "Observed" column is clipped; the full texts are in the `results.json` and `results.md` that `harness.py` writes to its `--out`
directory (every token-shaped string replaced); that output of this run is not kept in the repository. Rows that pass are the design confirmed *on kind with a stub*; read "Not exercised" below before
reading a PASS as more than that. Times: a happy-path session takes about 12 s on this host, 5 s of which are the start-up shim (V4).

| Id | Step | Expected | Observed | Result |
|---|---|---|---|---|
| E1 | one full session, happy path (build phase) | success; the edit and a new nested file are in the supervisor's worktree; result and usage came back through the closed schemas; relay finished and released; pod, Secret and reservation gone | success: | PASS |
| E2 | a read-only phase that changes nothing | success with the reviewer schema, empty delta | success: | PASS |
| E3 | a read-only phase that writes a file: the delta is refused | an error naming a rejected delta; nothing written in the supervisor's worktree | error: AgentError the pod's file delta was rejected and nothing was applied; see /data/e2e/review-write/run/logs/review-0.jsonl and /data/e2e/review-write/run | PASS |
| E4 | the agent sleeps past the time limit (60 s) | AgentTimeout after about 60 s; the edit made before the sleep is kept (delta applied first) | error: AgentTimeout timed out after 60 s | PASS |
| E5 | the agent exits non-zero and posted no result | `agent exited 3` | error: RuntimeError agent exited 3; see /data/e2e/exit3/run/logs/build-0.stderr | PASS |
| E6 | the agent posts a success result and then exits 1 | `agent exited 1 after a success result` | error: AgentError agent exited 1 after a success result; see /data/e2e/exit1-after-success/run/logs/build-0.stderr | PASS |
| E7 | the agent exits 0 without a result event | `agent produced no result event` | error: RuntimeError agent produced no result event; see /data/e2e/noresult/run/logs/build-0.jsonl | PASS |
| E8 | a symlink out of the tree in the delta: refused as a whole | an error naming a rejected delta; neither the link nor the file is applied | error: AgentError the pod's file delta was rejected and nothing was applied; see /data/e2e/scope-link/run/logs/build-0.jsonl and /data/e2e/scope-link/run/logs | PASS |
| E9 | the agent writes next to the worktree (outside the scope) | the pod's file system refuses the write (the data directory above the worktree is root's); nothing outside the worktree appears on the supervisor's side; the in-tree edit comes back | success: | PASS |
| E10 | the agent writes a git hook inside .git of the pod's tree | no hook reaches the supervisor's repository (the wrapper leaves .git out of the delta it builds; a hand-made delta with a git path is refused, tests.test_delta_malicious) | success: | PASS |
| E11 | the agent tries to read the relay token, the key, the service account token, other processes' environment | uid 10002; the token file, the key file and the service-account token not readable; /proc/1/environ and /etc/shadow not readable; /usr not writable; no FACTORY_* variable in the agent environment; no other process's environment r… | success: | PASS |
| E12a | control: a pod of an UNFENCED namespace reaches what the worker must not (manager ports by Service and by pod address, the relay, the API proxy) | all connect, except the Service `factory` on 9464 (it has no such port: D3) | factory.factory.svc.cluster.local:8080 CONNECT; factory.factory.svc.cluster.local:9464 no; 10.244.1.2:8080 CONNECT; 10.244.1.2:9464 CONNECT; 172.31.0.4:10250 CONNECT; factory-relay-svc.factory.svc.cluster.local:8090 CONNECT; 172.31.1.10:443 CONNECT; | PASS |
| E12b | the agent (uid 10002 in a worker pod, no sandbox) tries the manager's ports, the node's kubelet, the API server (service and node address), the metadata and wire services, plain 80, DNS to the outsid… | everything blocked except the relay port, TCP 443 outward and cluster DNS | open that must be closed: []; closed that must be open: []; raw: {"api_node_6443": false, "api_proxy_443": true, "api_service_443": false, "dns_resolves_cluster": true, "dns_tcp_53_external": false, "external_443": true, "external_80": false, "hostga_32526": false, "imds_80": false, "manager_pod_ip… | PASS |
| E12c | nothing leaks after the network session | no pod, Secret, reservation | clean | PASS |
| E12d | the canary of the worker passed (it is part of every session: a failed canary is an isolation failure with the breaker tripped) | no breaker, the launcher not denying | stopworld=False | PASS |
| E13 | the worker's container is SIGKILLed with no warning in the middle of the session | a fixed failure text, no hang; NO delta comes back (a pod killed from outside returns none: the worktree keeps what it had); the pod, Secret, reservation and relay record are all released | crictl stop -t 0 on 1 container(s): RuntimeError: agent exited 137; see /data/e2e/killpod/run/logs/build-0.stderr files=['README.md', 'src/app.txt'] after 12.2s | PASS |
| E13c | the worker pod is force-deleted (`--grace-period=0 --force`) in the middle of the session | a fixed failure text, no hang, nothing leaks; whether a delta comes back is recorded (the kubelet still sends SIGTERM and waits its minimum grace, so a tiny tree may be returned) | RuntimeError: the pod was stopped before the agent finished; see /data/e2e/killpodforce/run/logs/build-0.jsonl and /data/e2e/killpodforce/run/logs/build-0 files=['README.md', 'slept.txt', 'src/app.txt'] | PASS |
| E13b | the worker pod is deleted gracefully (SIGTERM, 30 s grace) in the middle of the session | the wrapper posts `terminated`, the delta comes back (slept.txt applied), a retryable failure `the pod was stopped before the agent finished`; nothing leaks | RuntimeError: the pod was stopped before the agent finished; see /data/e2e/killpodterm/run/logs/build-0.jsonl and /data/e2e/killpodterm/run/logs/build-0.s files=['README.md', 'slept.txt', 'src/app.txt'] after 3.0s | PASS |
| E14a | the orphan sweep (the manager's podsweep, 3 passes, gone-after shortened to 2 s) runs while the supervisor is ALIVE | the live session is not touched: nothing deleted, nothing released, the pod still there | passes=[{"deleted": {}, "failed": 0, "incomplete": false, "line": null, "released": 0, "skipped": 1, "terminating": 0, "unreadable": false}, {"deleted": {}, "failed": 0, "incomplete": false, "line": null, "released": 0, "skipped": 1, "terminating": 0, "unreadable": false}, {"deleted": {}, "failed":… | PASS |
| E14b | the supervisor process is killed (SIGKILL) mid-session | the pod outlives it (that is the design: a pod outlives its launcher) | pods=['factory-w-b8c6eb3de665bb0953a27dfb7e2bea12'] secrets=1 | PASS |
| E14c | the sweep passes after the supervisor is gone (first pass starts the silence clock, a later one acts) | the pod is deleted by reason supervisor_gone, then the Secret goes with it | deleted=1 first=[{'deleted': {}, 'failed': 0, 'incomplete': False, 'line': None, 'released': 0, 'skipped': 1, 'terminating': 0, 'unreadable': False}] second=[{'deleted': {}, 'failed': 0, 'incomplete': False, 'line': None, 'released': 0, 'skipped': 1, 'terminating': 0, 'unreadable': False}, {'delete… | PASS |
| E14d | recovery over the dead supervisor's state (State.recover through the launcher's reader) | the recorded pod reads dead; the job is marked interrupted; no PodStillLive | {"job_before": "running", "job_after": "interrupted", "recovered": ["job-1"], "recover_error": null, "pod_status": null} | PASS |
| E14e | the pool released the dead supervisor's reservation | in_use 0, holders 0 | {"ceiling": 2, "free": 2, "holders": 0, "in_use": 0, "pacing_tokens": 2.0} | PASS |
| E14f | the relay record of the killed session ended | revoked or released (the sweep revokes before it deletes) | {"preflight_code": null, "read": "ok", "reject": null, "released": false, "revoked": true, "state": "revoked"} | PASS |
| E15a | with the LAUNCHER's own token, create a pod that breaks one rule of the policy (9 variants through the real client) | every one denied by the API server (none admitted) | admitted=0 denied=9 of 9: host-network->Denied:422 rules=[]; privileged->Denied:403 rules=['privileged']; other-image->Denied:403 rules=['image']; host-path->Denied:403 rules=['host-path']; service-account-token->Denied:403 rules=['automount']; host-users-true->Denied:422 rules=[]; extra-capability… | PASS |
| E15b | the client reads the denial as a denial, not as bad credentials | each answer is `Denied` (403 from the policy, 422 from the API server's own validation), never `ConfigError` | 9 of 9 are Denied; others: [] | PASS |
| E15c | with the same token, read Secrets, read a pod log, list the manager namespace's pods, list nodes | all four 403 (the Role has create/get/list/delete on pods and create/delete on secrets only) | rbac-secret-list=403; rbac-pod-log=403; rbac-other-namespace-pods=403; rbac-nodes=403 | PASS |
| E15d | no pod or Secret was created by the attempts | namespace empty | {'pods': [], 'secrets': []} | PASS |
| E16a | three sessions started at once against a ceiling of 2 | never more than 2 worker pods at any moment; the third waits for capacity and then runs; all three end | peak live pods=2; outcomes={'pool1': 'success', 'pool2': 'success', 'pool3': 'success'}; elapsed=[32.2, 31.2, 63.6] | PASS |
| E16b | after all three: nothing leaks | no pod, Secret, reservation | namespace={'pods': [], 'secrets': []} pool={'ceiling': 2, 'free': 2, 'holders': 0, 'in_use': 0, 'pacing_tokens': 2.0} | PASS |
| E16c | ask the pool directly for more than the ceiling (real ledger on the real volume) | the admission over the ceiling is refused with the code `ceiling` (waiting), the per-repository cap and the pacing refuse with their codes; every granted reservation is given back | {"admit a": "granted", "admit b": "granted", "after release (a token was given back)": "granted", "estimate over the run budget": "budget", "same session again": "duplicate", "third over the ceiling": "ceiling"} | PASS |
| E17a | S1 part 1 (the image's s1_bwrap.sh) inside a real worker pod: hostUsers false, procMount Unmasked, AppArmor Unconfined, the pod's capability set | every check PASS (bubblewrap can create the namespaces the sandbox needs) | S1 part 1: PASS (exit 0) | PASS |
| E17b | tools/sandbox_e2e.py (`factory-sandbox-check`: the real claude binary against a local FAKE API, no key, no real model) inside the same pod | every check PASS | all expectations held (exit 0) | PASS |
| E21a | runbook G2 socket as the worker identity (uid 10002, gid 10010) | exit 0, every line `ok` | exit 0: connect to the launcher socket: refused (expected refused) \| create a file in the socket directory: refused (expected refused) \| rename or unlink in the socket directory (write access): refused (expected refused) \| bind a socket in the socket directory: refused (expected refused) | PASS |
| E21b | runbook G2 socket as the supervisor (uid 10001, gid 10001) | exit 0, every line `ok` | exit 0: connect to the launcher socket: allowed (expected allowed) \| create a file in the socket directory: refused (expected refused) \| rename or unlink in the socket directory (write access): refused (expected refused) \| bind a socket in the socket directory: refused (expected refused) | PASS |
| E21c | runbook G2 spool as the worker identity | exit 0, every line `ok` | exit 0: list the spool: refused (expected refused) \| create a file in the spool: refused (expected refused) | PASS |
| E21d | runbook G2 environment of the relay container | exit 0, every line `ok` | exit 0: environment: 29 names, KUBERNETES_SERVICE_HOST present, KUBERNETES_SERVICE_PORT present \| environment: 0 names start with AZURE_, 0 names contain KEY, TOKEN, SECRET or PASSWORD \| environment: 1 image-defined name not counted (FACTORY_TOKEN_BROKER: a command line, not a secret) | PASS |
| E21e | runbook G2 environment of the launcher container | exit 0, every line `ok` | exit 0: environment: 39 names, KUBERNETES_SERVICE_HOST present, KUBERNETES_SERVICE_PORT present \| environment: 0 names start with AZURE_, 1 names contain KEY, TOKEN, SECRET or PASSWORD \| environment: 1 image-defined name not counted (FACTORY_TOKEN_BROKER: a command line, not a secret) | PASS |
| E21f | runbook G3 `statuses` in the launcher container (its own token, the real API server) | 404 for a missing pod, 403 for a Secret list, 401 for an invalid token, 403 for a policy denial (the 422 the client had assumed was wrong: D1) | exit 0: {'not_found': 404, 'forbidden': 403, 'unauthorized': 401, 'denied': 403} | PASS |
| E21g | runbook G5 secret-volume as uid 10002 | exit 0, every line `ok` | exit 0: secret file token: regular yes, mode 0400, owner 0, group 0 \| secret file anthropic-api-key: regular yes, mode 0400, owner 0, group 0 \| open secret file token as uid 10002: refused (expected refused) \| open secret file anthropic-api-key as uid 10002: refused (expected refused) | PASS |
| E21h | runbook G5 environment of a worker pod | exit 0, every line `ok` | exit 0: environment: 35 names, KUBERNETES_SERVICE_HOST present, KUBERNETES_SERVICE_PORT present \| environment: 0 names start with AZURE_, 0 names contain KEY, TOKEN, SECRET or PASSWORD \| environment: 1 image-defined name not counted (FACTORY_TOKEN_BROKER: a command line, not a secret) | PASS |
| E21i | runbook G5 the worktree is owned by the worker identity (the wrapper's chown under hostUsers false) | owner 10002:10010 | drwxr-xr-x 4 10002 10010 4096 Nov 10 02:23 /data/e2e/probes/wt | PASS |
| E20a | a process with the WORKER uid (10002, no groups, no capabilities) inside the manager's main container tries the launcher socket, the socket directory and the spool | every one refused (EACCES): the directories are 0750 and 2770 for group 10001 | {"connect_launcher_socket": "EACCES", "create_in_socket_dir": "EACCES", "create_in_spool": "EACCES", "groups": [], "list_socket_dir": "EACCES", "list_spool": "EACCES", "read_service_account_token": "ENOENT", "uid": 10002, "unlink_the_socket": "EACCES"} | PASS |
| E20b | the SUPERVISOR identity (uid 10001 through the entrypoint) in the same container: the same attempts | connects to the socket and reads and writes the spool; cannot create or unlink in the socket directory (read-only mount); holds no Kubernetes token | {"connect_launcher_socket": "ok", "create_in_socket_dir": "EROFS", "create_in_spool": "ok", "groups": [10010], "list_socket_dir": "ok", "list_spool": "ok", "read_service_account_token": "ENOENT", "uid": 10001, "unlink_the_socket": "EROFS"} | PASS |
| E20c | who holds the Kubernetes token: the launcher container yes, the relay container no; neither has an AZURE_* variable; the relay has no Anthropic key | launcher: `10003 True []`; relay: `10004 False []` | launcher: `10003 True []`; relay: `10004 False []` | PASS |
| E19 | two repositories start together on a cluster with ONE worker-pool node (required anti-affinity: one repository per node) | the second pod waits Pending while the first runs and never runs beside it; both sessions end | pending_seen=True two_running_at_once=False outcomes={'repoa': 'success', 'repob': 'success'} | PASS |
| E18a | negative control: the default-deny NetworkPolicy is removed and a session is started | the network canary fails the session at its start (`reach_kubelet` or another forbidden destination), the bundle is never served, the stop-the-world breaker is tripped, the pod is gone | AgentError: the pod's network isolation check failed (reach_kubelet); the launcher is denying launches and the stop-the-world breaker is tripped reject=preflight_failed breaker=True files=['README.md', 'src/app.txt'] | PASS |
| E18b | the next session while the breaker is tripped (the fence is back) | refused before any pod, with the breaker's fixed text | AgentError: the stop-the-world breaker is tripped; no pod was started pods={'count': 0, 'names': []} | PASS |
| E18c | the breaker is cleared but the launcher was told `deny` (isolation_failed) | the launcher still refuses every launch until it restarts (a fixed rejection, no pod) | AgentError: the pod launcher refused the launch (denied) pods={'count': 0, 'names': []} | PASS |
| E18d | after the manager pod restarts (the launcher forgets the denial) and the fence is back | the next session runs to the end | success | PASS |

## Evidence excerpts

**A worker pod as the cluster ran it** (`kubectl get pod`, and `kubectl exec` as root of the pod's user namespace, during a session; no value of a secret):
`hostUsers: false`; service account `factory-worker`, `automountServiceAccountToken: false`; one container `worker` with `securityContext` exactly
`{procMount: Unmasked, appArmorProfile: {type: Unconfined}}`; command `tini -- /opt/factory/bin/python -m factory.podworker`; requests equal limits (`cpu 2`,
`memory 4Gi`, `ephemeral-storage 20Gi`, the `standard` class); volumes `data`, `tmp`, `session`; `activeDeadlineSeconds: 390` for a 90 s limit (limit plus the 300 s margin);
`restartPolicy: Never`; the eight `factory.internal/*` labels. Inside: `uid_map` `0 224067584 65536` (a user namespace), `Seccomp: 2`, `NoNewPrivs: 0` for the wrapper (pid 1, root of
the namespace; the worker identity of the S1 run below shows `NoNewPrivs: 1`), `/data` and `/tmp` mounted `idmapped` ext4, `/run/factory-session/{token,anthropic-api-key}` root-owned 0400, the worktree
owned `10002:10010`. The wrapper's output is exactly its fixed lines: `podworker: started`, `podworker: preflight ok`, `podworker: bundle fetched`.

**The happy path (E1), the supervisor's record of the session** (`driver.py`, trimmed to what matters; the relay token appears nowhere):

```json
{"backend": "PodBackend", "outcome": "success", "elapsed_seconds": 12.1,
 "files": {"README.md": 4, "hello.txt": 19, "src/app.txt": 3, "sub/nested.txt": 7}, "file_hello": "hello from the pod\n",
 "result": {"blocked": false, "feature": false, "summary": "stub edit", "tests": []},
 "usage": {"phase": "build", "model": "claude-sonnet-5-5", "subtype": "success", "total_cost_usd": 0.0123, "num_turns": 2,
           "footprint": {"peak_rss_mb": 4.8, "cpu_seconds": 0.0, "samples": 1, "limit_mb": null}},
 "relay": {"state": "finished", "released": true, "revoked": false, "reject": null},
 "session_dir_entries": ["in", "last_heartbeat", "out", "record.json", "released", "state.json"],
 "transcript_bytes": 766, "transcript_has_result": true,
 "pods_after": {"count": 0}, "pool_before": {"ceiling": 2, "in_use": 0}, "pool_after": {"ceiling": 2, "in_use": 0},
 "state_pod_recorded_after": false, "stopworld": false}
```

**The first isolation failure** (the first real session on this cluster, before the start-up shim of V4; reproduced on purpose by E18a with the policy removed):
`the pod's network isolation check failed (reach_kubelet); the launcher is denying launches and the stop-the-world breaker is tripped`; the relay record
`state finished, reject preflight_failed, released and revoked`; no bundle was ever served; the pod, the Secret and the reservation were gone.

**S1 inside a real worker pod, in the format of `spikes/S1.md`.** The pod is the podspec as the real admission policy admitted it (E17). Part 1 is `s1_bwrap.sh` of the image,
part 2 is `factory-sandbox-check` (`tools/sandbox_e2e.py`: the **real `claude` binary 2.1.318** against a **local fake API server**, so **no key and no model were used**; the worker agent of the
sessions above is the stub, and no real `claude` session against a model ran at all).

| Platform and configuration | Part 1 (`s1_bwrap.sh`) | Part 2 (`factory-sandbox-check`) | S1 |
|---|---|---|---|
| kind v1.36.1 (containerd 2.3.1, a WSL2 kernel), pod from `podspec` with `hostUsers: false`, `procMount: Unmasked`, `appArmorProfile: Unconfined`, worker uid 10002, no capability, `no_new_privs` | **PASS** (7/7, exit 0): bwrap 0.8.0 starts with user, pid, ipc and uts namespaces; a write inside the bound directory works and one outside is denied; no outside environment is visible; `--unshare-net` starts; curl to a non-allowlisted host fails | **PASS**: "all expectations held" (27 PASS, exit 0) | not an S1 record: kind, a different kernel and no AKS; it shows that the three relaxations of the pod as built by the backend are enough for the sandbox on this kernel |

## Defects the run found

| Id | What reality showed | Where | Fixed? |
|---|---|---|---|
| **D1** | A ValidatingAdmissionPolicy denial is a **403** (the policy's `reason` is `Forbidden`), not the 422 the Kubernetes client assumed, and its message writes `[rule:<id>]`, not `[<id>]`. `kube.Client` read every 403 as "credentials refused" (`ConfigError`) and found no rule id in a 422 either, so the launcher answered `failed` for every policy denial instead of `rejected` with the broken rule ids: the supervisor, the logs and the runbook's `denial` recording would have lost exactly the information the policy was written to give. The fake of the API (`tests/fake_kube.py`) answered the assumed shape, so every test passed. | `src/factory/kube.py`, `tests/fake_kube.py`, `docs/pod-backend.md` ("A denial") | **Yes**, in the commit of this page: a 403 whose Status names a `ValidatingAdmissionPolicy ... denied request:` is `Denied` (status 403) with the ids taken from either form; any other 403 is still `ConfigError`; the fake answers the real 403 and `[rule:<id>]`; `tests/test_kube.py` pins the recorded message and the authorisation 403 that must stay a `ConfigError`. E15 shows nine of nine denials read as `Denied` with the real rule id (for example `privileged`, `image`, `host-path`). |
| **D2** | The network canary's probe of `FACTORY_MANAGER_ADDRESS:9464` can **never fail**. The address is the Service `factory`, which exposes only 8080 (the metrics port has its own Service, `factory-metrics`, and only with monitoring on), so a connect to `factory:9464` is refused with or without a fence: a control pod of an unfenced namespace gets "no" (E12a) while the manager pod's own address on 9464 does connect. The probe is vacuous for the one thing it was added for. The fence itself is fine: a worker pod cannot reach the manager pod's address on 8080 or 9464 (E12b), because the egress rule selects the manager pod on the relay port only. | `infra/locals.tf` (`worker_contract.canary.forbidden`), `src/factory/podcontract.py`, `src/factory/podworker.py` | **No.** The fix is not small: the canary needs the metrics Service's name or the pod's address, and a new name in the pod's environment allow-list (`ENV_POD`) means the admission policy's `env-names` rule, `locals.tf`, `podcontract.py`, the golden pods and the policy tests all change together. Recommended: a `FACTORY_MANAGER_METRICS_ADDRESS` (the Service `factory-metrics`, which must then exist with the backend on) in the allow-list, probed on 9464; until then the 9464 line of the canary proves nothing and the egress rule is what protects the port. |
| **D3** | A network enforcer that starts enforcing **after** the container starts turns the canary into a false isolation failure, and one false isolation failure stops everything. The very first real session on this cluster failed `reach_kubelet`: kindnet's engine enforced the default-deny about one second after the container began (V4). The product did exactly what it is designed to do: the bundle was not served, the launcher was told to `deny`, the stop-the-world breaker was tripped (a marker in the data directory that every supervisor and the manager see), the pod, Secret and reservation were released, and **every later session was refused until the breaker was cleared and the manager pod restarted** (E18). That is the right answer to a real leak; it is a heavy answer to a race. Cilium is expected to have its policy in place before the container starts (an endpoint is regenerated before the CNI call returns); that is **not shown** here. | `docs/pod-backend-runbook.md` E3 and G5, `src/factory/podworker.py` (`run_canary`) | **No**, by decision: the canary must not be relaxed. Recorded so that the person who runs the runbook's E3 on the mirror measures the enforcement lag of Cilium first (connect to the kubelet port every 250 ms from a fresh pod) and decides about a settle delay or a retry window **in the wrapper, never in the policy**; and so that the recovery steps are known: clear the marker (`stopworld`), restart the manager pod. |
| **D4** | The runbook's own probe fails on a correct pod. `tools/live_pod_probe.py environment --expect worker-pod` (G5) counts names that contain KEY, TOKEN, SECRET or PASSWORD and stops at one; the image's own `ENV FACTORY_TOKEN_BROKER=...` (the command line of the GitHub App token broker, no secret) is in every container of the image, a worker pod's too, so G5 would have stopped the first real run on a pod that is exactly as designed. | `tools/live_pod_probe.py`, `tests/test_live_pod_probe.py` | **Yes**: the one name is exempt (and printed as "image-defined name not counted"), and a test reads the Dockerfile and fails when it gains another secret-like name that the probe does not know. |

**Not defects, but worth knowing** (each is a row of the table above):

* The agent holds the Anthropic key in its own environment (the `claude` CLI needs it) and can list the names in `/run/factory-session` and write in `/data`; it cannot read the relay token or the key file, any other process's environment, `/proc/1/environ` or `/etc/shadow`, and its environment has no `FACTORY_*` name (E11). A hostile agent can therefore send the key out over TCP 443, which the egress rule allows by design (the sandbox's domain list is the only filter, as today).
* What a killed pod returns depends on how it was killed (E13, E13b, E13c). A container that gets **no warning** (`crictl stop -t 0`, which is what an OOM kill or a lost node looks like to the wrapper) is `agent exited 137` and returns no delta: the worktree keeps what it had, as the design says. A pod that is deleted with its grace, **or force-deleted** with `--grace-period=0` (the kubelet still sends SIGTERM and waits its minimum grace), posts what it had and the delta is applied (`slept.txt` arrived in both); both end as `the pod was stopped before the agent finished`, a retryable failure.
* The stub runs without `claude`'s sandbox, and S1 part 1 and `factory-sandbox-check` pass in the same kind of pod (E17), so the "worse agent" is also not the only thing that was exercised.

## What the run settled, and what it contradicted, of the UNVERIFIED items

The ids are the rows of the table "Which step settles which UNVERIFIED claim" of `docs/pod-backend-runbook.md` (that table and the UNVERIFIED text of
`docs/pod-backend.md` are **not edited by this page**, apart from the one denial paragraph of D1: `tests/test_live_runbook.py` passes). "Settled on kind" is
never "settled on AKS": the cluster, the engine, the volume and the node are different, and a claim about AKS, Cilium, NAP, Azure RBAC or the registry stays open
whatever this page says.

**Settled on kind (the claim held when a real pod ran it):**

| Rows | Claim | Evidence |
|---|---|---|
| U001, U002 (first half), U003 | A real API server accepts the pod exactly as `podspec` writes it, with the three relaxations, and the real policy admits the pod the launcher builds; what the server defaults before it validates does not break the mirror. The scheduler honours the required one-repository-per-node anti-affinity (NAP honouring it is **not** shown). | E1 and every session; E19 (the second repository's pod waited `Pending` and never ran beside the first) |
| U023 | The wrapper command (`/usr/bin/tini`, `/opt/factory/bin/python -m factory.podworker`) exists in the pod. | every session |
| U026 | The kubelet injects `KUBERNETES_SERVICE_HOST` and `_PORT` although `enableServiceLinks` is `false`. | the canary's `service` probe built its endpoint from them; E21h |
| U021, U022, U028 | `chown` and `lchown` to uid 10002, the extractor's modes and the Secret volume's owner and mode all work under `hostUsers: false` (the root of the user namespace chowns the worktree; the token and key files are 0400 root and unreadable to uid 10002). | E1, E21g, E21i |
| U008, U069, U081 | `SO_PEERCRED` across containers that share an `emptyDir` under `hostUsers: false`, and the spool and socket-directory permissions with two containers of different uids: the supervisor (10001) connects, the worker uid (10002) is refused, the socket directory is read-only for the main container, the spool is read-write for it. | E20a, E20b, E21a to E21c, and every session |
| U110, U077, U084 | The kubelet starts the manager pod with the launcher and relay as written: the projected token and CA at the standard path in the launcher only, the init container (CHOWN, FOWNER, FSETID) sets `10003:10001 0750` and `10004:10001 2770`, the read-only mount still lets the main container connect. | E20c, E15, the pod's own modes |
| U013 | `FACTORY_RELAY_PORT` is not shadowed by a Service variable (Services named `factory` and `factory-relay-svc`, `enableServiceLinks: false`). | the relay listened and sessions ran |
| U063, U065 | A real API server evaluates the CEL: the golden pod is admitted and nine variants that each break one rule are denied by the policy with the rule id (`tools/policy_matrix.py`, which did this already on kind, now also under the real launcher token). | E15 |
| U118, U119, U125 (part) | The real wrapper reads the manifest and runs the agent as uid 10002 with no `FACTORY_*` name in its environment (`Popen(user=, group=, extra_groups=[])` and `execnocaps` as container root under `hostUsers: false`), SIGTERM through `tini` stops the agent and posts a result, the memory guard samples the pod's tree (a footprint comes back: 10 MB, 11 samples), a stop on timeout, its drain and the delta all fit in the 120 s margin for a tiny tree. | E1, E4, E11, E13b |
| U121, U122 (part) | `flock` serialises several supervisor processes on one volume (an `emptyDir` here: **not** the production disk). | E16 (three processes), E16c |
| U100, U098 (part) | The probes of `tools/live_pod_probe.py` run as the runbook writes them and answer as designed in real pods, **after one fix**: `environment --expect worker-pod` failed on a correct pod because the image's own `FACTORY_TOKEN_BROKER` is a secret-like name (D4). | E21 |
| U102 (part) | Part G's commands for the socket, spool, environment, statuses and secret-volume probes (G2, G3, G5) run as written against slices 12b, 16 and 17. | E21 |
| U083 | Deleting a running pod leaves it `terminating` for its grace period and the launcher client's `terminate` returns only once it is gone. | E13b |
| U046 | The supervisor's own bound for `terminate` is long enough for a pod deleted with its grace. | E13b |
| U044 (part) | Recovery over a dead supervisor's state reads the recorded pod through the launcher and goes on (`interrupted`, no `PodStillLive`) once the sweep has removed it; the sweep leaves a live supervisor's session alone. | E14 |

**Contradicted:**

| Rows | Claim | What was found |
|---|---|---|
| U074, U078, U072 | A policy denial is a 422 with `[<id>]`; the fake answers as the real server does. | It is a 403 with `[rule:<id>]` (D1, fixed). The 409, 404 and 401 of the same table were not shown to differ (404 and 401 and 403 for a Secret list are E21f). |
| U024, U025 | The canary destinations are the complete and effective list of what must be unreachable. | The 9464 entry cannot fail (D2). The other five forbidden entries do connect without the fence and do not with it (E12a, E12b, E18a). |
| U100 | The probes' answers inside real pods are what the tool expects. | One was not (D4, fixed). |

**Observed on kind with a different engine (so open for Cilium):** U057 and U058 (`ipBlock.except` and the DNS rule: kindnet enforced the exclusion of the node subnet, the pod and service ranges and the metadata addresses, and the DNS rule resolved cluster names, E12b), U112 (a worker pod reaches the relay through the Service and cannot reach the manager address: E12b, but **under kindnet**), U059 and U027 (an anonymous request to a real API server's "public" address is answered 403: through the proxy stand-in, V7). U050 (Cilium itself) is untouched, and so is every row about switching the data plane.

**Not exercised at all:** every row about Terraform, plan identities, Azure RBAC, Key Vault, the workload identity webhook (U111), Container Insights (U060, U061), node auto-provisioning and Karpenter, the registry pull, quota (U054, U101), the spend-limit and 401 and 429 shapes of `claude` (U073, U087), real repository sizes, times and footprints (U007, U014, U030, U031, U085, U086, U095), the relay under a slow link (U029), an eviction, a node loss or a Karpenter disruption (U075), the launcher's own reaping of a pod that never scheduled (U045 in part, U082), the parameter-ConfigMap ordering during an image promotion (U067), ephemeral containers (U066), the previous image's readers (U043), and the manager itself (U128).

## What would make this run closer to AKS

1. Run `harness.py run --no-cluster` on the mirror of runbook Parts C and D (Cilium, NAP, the worker pool), with the real image digest; the stub is already in the repository's test layer only, so the image must be rebuilt with `Dockerfile.stub` for the scenarios that need it (the scenarios that do not need the stub are E15, E20 and E21a to E21f).
2. Before E3 of the runbook, measure Cilium's enforcement lag for a fresh pod (D3).
3. Decide D2.
4. Put a real claude behind a spend-limited key for runbook G8: nothing here touches an API.

## Cleanup

`harness.py all` (and `down`) removes the kind cluster `podspike`, the docker network `podspike-net` and the container `podspike-apiproxy`. The scratch directory (kubeconfig, the image archive, logs) is outside the repository. The docker images `factory-podspike:local` and `factory-podspike:stub` remain in the local docker (they are builds of this tree).

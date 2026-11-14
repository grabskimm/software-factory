# Pod backend: design record and shared contract

Status: **design record, default off, nothing is switched on.** This page belongs to the program of
issues #144 (Cilium and node auto-provisioning), #145 (an execution backend that runs sessions as
pods), #146 (pod pool, pacing and spend) and #147 (pod-side liveness and orphans). The first slice
only fixes the names and numbers that Terraform and Python must agree on (`src/factory/podcontract.py`
and the Terraform local `worker_contract` in `infra/locals.tf`). It adds no behaviour. Slice 3 changes how the
supervisor runs git on a worktree a worker touched: every such call goes through one hardened function
(`src/factory/gitsafe.py`, below), in today's in-process mode too. The sixth slice adds
the worker pod template and a Python mirror of its admission policy (`src/factory/podspec.py`, below); slice 8
adds the relay and its session record (section "The relay and the session record"); slice 12a adds the
per-repository `execution` setting and the manager-wide switch (below), which **refuse** until the backend of slice 12b can be built; slice 5 adds the `pod`
handle beside `pid` in a job's state and makes recovery, `purge` and `status` fail closed on it (section "The pod
handle in job state"); slice 9a adds the bundle wire format and the ship-in staging (`src/factory/bundle.py`, section
"The bundle and ship-in staging"); slice 10a adds the pod worker wrapper, part A (`src/factory/podworker.py`, section
"The pod worker, part A"); slice 10b completes it (section "The pod worker, part B": run `claude`, stream, delta, footprint,
SIGTERM) and adds `src/factory/podreport.py`, the report the wrapper posts. Nothing imports the template, the mirror, the relay,
the bundle or the launcher (the wrapper imports the bundle, the memory guard and the report, and nothing launches the wrapper):
nothing starts the launcher (below), so no pod can be started, and no job ever records a pod handle. Slice 9b adds to the bundle module the validation and the safe apply of the file delta a pod returns
(section "The file delta: validation and safe apply"); nothing calls it yet.
Slices 19 and 20 add the pod-side liveness verdict (`src/factory/podliveness.py`), the orphan sweep and the pod census (`src/factory/podsweep.py`),
a recovery that waits for a live pod and a deploy gate that waits for running pods (section "Pod-side liveness, the orphan sweep and the deploy gate"); the hooks exist in `operations` and `manager` but are
dormant until `Manager.pods` is set by the pod backend.

Nothing but the pod backend of slice 12b imports the relay's session record, the launcher client, the bundle's
staging or the delta (the wrapper imports the bundle, and nothing launches the wrapper): nothing starts the launcher (below), so no pod
can be started, and no job ever records a pod handle unless the switch is on. Slice 9b adds to the bundle module the validation and the safe apply of the file delta a pod returns
(section "The file delta: validation and safe apply"); only the pod backend calls it.
Every repository still runs its sessions inside the manager pod, exactly as before. The four issues stay open
until a person has run the pod backend against a real cluster and the owner has decided to switch anything on. One thing has run: a first
end-to-end run on a local kind cluster with a stub agent (a pod ran for the first time; the harness that ran it is not shipped here). It found that an admission denial is a 403 and not a 422
(fixed: "The client", "A denial") and settled nothing about Azure, AKS, Cilium, node auto-provisioning or a real `claude` session.
Slice 11 adds the launcher program and its manager-side client (section "The launcher and its client"); nothing imports or starts
them yet. Slice 18 adds the pod pool (`src/factory/podpool.py`, section "The pod pool: ceiling, pacing and reserve-on-admit"): the
ceiling, pacing and reserve-on-admit guard the pod backend will ask before every launch; nothing imports it yet.

Slices 16 and 17 (section "Terraform C") add the Terraform that would start them: the launcher's Role and RoleBinding and the
launcher and relay containers of the manager pod, all default off with `pod_backend_enabled`; the manager still refuses `execution = "pod"`.

Slice 12b adds `PodBackend` (section "The pod backend (slice 12b)"): the one place that uses the launcher client, the relay's
spool, the bundle and the delta. It is selected only by `execution = "pod"` of a repository AND `FACTORY_POD_BACKEND=on` of the manager, both
off by default; nothing starts a launcher or a relay, so a switched-on deployment would still have no pod to launch.

Slice 14 (below, "Terraform A") adds the first Terraform for it, and it is default off as well: nine
variables that are unset or false, a count-gated NodePool and AKSNodeClass, the one local the pool
ceiling derives from, and `prevent_destroy` on the cluster. With every variable at its default the
plan has no line for any of it.

Slice 15b (below, "Terraform B2") adds the admission policies that pin what the launcher may say in a worker pod and in its
Secret (`infra/policy/*.json`, `infra/worker_policy.tf`), the tool that replays the golden pods and every mutation against a real
API server (`tools/policy_matrix.py`) and an offline model of the CEL (`tests/celmodel.py`); it is default off as well.

Wording used here and in every pull request of the program: a fact that only a live cluster can
show is called **live** or **UNVERIFIED**; anything checked against an in-memory fake of the
Kubernetes API or of the relay is "contract-tested against assumed shapes", never "proven".

> **Status after slice 12b (read this first).** The sentences below that say "nothing imports it", "nothing launches a pod" or "delivered,
> not wired in" describe each slice **as it was delivered**. Slice 12b (`src/factory/podbackend.py`, section "The pod backend (slice 12b)")
> wires them together: the bundle and the delta (9a, 9b), the launcher client (11), the relay record (8), the pod handle (5), the pool
> (18), the liveness and the sweep (19, 20) and the report and manifest of the pod worker (10b). Everything is still **off by default**:
> `execution = "inprocess"` per repository and `FACTORY_POD_BACKEND` off in the manager; with the switch off no pod module is imported
> (a test starts a fresh interpreter and checks `sys.modules`), the launcher and the relay are not started (slice 17 creates them only with
> `pod_backend_enabled`), and the in-process path is unchanged. **No pod has ever run on a real deployment** (one ran once on a local kind cluster with a stub agent): every claim is contract-tested against fakes and
> the real code of both sides, and the list of what only a cluster can settle is the one near the end of this page; a run against a real cluster is the
> way to settle it, and slice 22 (recorded fixtures) waits for it.

## What it is for

Today every session (plan, build, review, verify, preflight) is a child of the one manager pod, so
capacity is the size of one fixed node (`docs/capacity.md`). A per-session pod on an
auto-provisioned node (NAP) removes that ceiling: a throwaway cluster measured a pod ready from zero
nodes in about 100 s, a warm start in 1 s, working `emptyDir` worktrees and `do-not-disrupt`
holding a running session. The cost case for production also
needs the fixed node to shrink (#148); this program does not claim a saving.

## Decisions that shape the design

* **Only the `claude` session becomes a pod** ("worker pod, minimal secrets"). The supervisor stays
  in the manager pod, commits and pushes there, and holds the GitHub credentials. The worker pod
  holds the Anthropic key and a one-time relay token and no GitHub, cloud or Kubernetes credential.
* **A small launcher holds the only Kubernetes token** and builds pods from a fixed template. An
  admission policy, a default-deny network policy and one repository per node are prerequisites,
  not options.
* **Transcripts go to a relay, not to pod logs**, so they stay out of Log Analytics.
* **Offline first.** Everything is built and tested without a cluster, including the Terraform,
  which is default off. No apply is run by an assistant; someone with Azure access runs the checks
  (read-only production checks, then a scratch deployment: a copy of `infra/` deployed for testing, with its
  own name prefix and state). Recorded responses later become test fixtures.
* **Spend guard:** a pod session starts only if spent plus reserved caps fit `run_budget_usd`; an
  explicit concurrency ceiling (default 8) derived from the NodePool limit; a stop-the-world breaker
  on spend-limit errors; an Anthropic workspace spend limit set by a person in the Console.
* **Defaults:** supervised pull requests, every one `Refs #N`; no pod issue is labelled
  `factory-build`; the `execution` setting is per repository, `inprocess` by default (slice 12a, below).

## Architecture

**Manager pod** (namespace `factory`, one replica, `Recreate`) gets two more containers and one init
container, only when the backend is enabled:

* **main container**: unchanged identity (uid 10001). Supervisors, manager API, the GitHub App key
  and the Anthropic key in its environment. No Kubernetes token, no listener for pods.
* **launcher** (own non-root uid): the only holder of a Kubernetes token (a projected token mounted
  in this container only) and of the Anthropic key it writes into per-session Secrets. It listens on
  a unix socket and authenticates the caller by `SO_PEERCRED` (uid 10001 only), so no bearer sits in
  any environment. Its Role: `pods` create, get, list and delete and `secrets` create and delete in
  the worker namespace; no `exec`, no secret get, list or watch, no `pods/log`.
* **relay** (own non-root uid, primary group 10001): a TCP listener for worker pods and a spool
  directory. No secret, no Kubernetes token, no cloud token; it stores a hash of each token only.
* **init container `volume-perms`** (root inside the user namespace, no secret, fixed command): sets
  the owner and mode of the two shared `emptyDir` roots once, before any sidecar starts (the socket
  directory and the spool), because the sidecars run non-root with all capabilities dropped.

**Worker pod** (namespace `factory-workers`, a bare Pod, `restartPolicy: Never`): one container
running a small root wrapper that reads two root-owned 0400 files from a Secret volume (the
Anthropic key and the one-time relay token), fetches its tree from the relay, runs `claude` as the
unprivileged worker uid with the allow-listed environment, streams the transcript to the relay and
posts a file delta. It carries the three relaxations of the manager pod (`hostUsers: false`,
`procMount: Unmasked`, an unconfined AppArmor profile) and nothing more, `do-not-disrupt`,
`activeDeadlineSeconds`, a required anti-affinity so that one node serves one repository, and no
workload-identity label, no projected token volume and no `AZURE_*` environment.

**Flow of one session:** (1) the supervisor builds the launch spec, mints a session id and a
one-time token, stages a bundle in the spool and writes a session record; (2) it asks the launcher
to launch; (3) the launcher creates the Pod, then the per-session Secret with an `ownerReference` to
the pod, and returns the name and uid; (4) the supervisor binds them to the session record and
records a `pod` handle in state; (5) the pod runs its network canary, then fetches the bundle once;
(6) `claude` runs and the wrapper streams to the relay and heartbeats; (7) the wrapper posts the
file delta and footprint once; (8) the supervisor validates the delta, applies it to its own
worktree and collects as today; (9) the supervisor, not the pod, commits and pushes; (10) the
launcher deletes the pod and the Secret goes with it.

| Holder | Anthropic key | GitHub App key and tokens | Kubernetes token | Cloud (federated) token | Relay token |
|---|---|---|---|---|---|
| manager main container | yes (environment) | yes (environment) | no | yes (as today) | mints it |
| launcher | yes (environment, to write Secrets) | no | yes (its container only) | no | passes it through, never logs it |
| relay | no | no | no | no | stores its hash only |
| worker pod | yes (root-only file, then `claude`'s environment as today) | no | no | no | yes (root-only file) |

## The shared contract

`SHARED` in `src/factory/podcontract.py` is a tree of strings, integers, lists and maps. The
Terraform local `worker_contract` in `infra/locals.tf` holds **the same tree as literals only** (no
`var.` or `local.` reference, so a test can parse it without evaluating HCL). Nothing reads either
yet. The sections:

| Section | Holds | Python names |
|---|---|---|
| `names` | manager and worker namespace (`factory`, `factory-workers`), worker service account (`factory-worker`), pod name prefix (`factory-w-`), Secret name suffix (`-s`, so `<pod name>-s`) and keys (`token`, `anthropic-api-key`), NodePool and node class name (`factory-workers`), the image reference pattern (`<registry>/<repo>@sha256:<64 hex>`) | `MANAGER_NAMESPACE`, `WORKER_NAMESPACE`, `WORKER_SERVICE_ACCOUNT`, `POD_NAME_PREFIX`, `SECRET_NAME_SUFFIX`, `SECRET_KEY_TOKEN`, `SECRET_KEY_ANTHROPIC`, `NODEPOOL_NAME`, `NODECLASS_NAME`, `IMAGE_REFERENCE_PATTERN` |
| `labels` | label prefix `factory.internal/`, keys `role`, `repo`, `run`, `job`, `attempt`, `phase`, `manager`, `session`, the role value, the phase words, the hex length of each digest label | `LABEL_PREFIX`, `LABEL_KEYS`, `LABEL_ROLE_VALUE`, `PHASES`, `LABEL_HEX_LENGTHS` |
| `annotations` | `karpenter.sh/do-not-disrupt` and its value, and the keys under the prefix (`deadline`, `repo`, `run-id`, `job-id`) | `ANNOTATION_DO_NOT_DISRUPT`, `ANNOTATION_DO_NOT_DISRUPT_VALUE`, `ANNOTATION_KEYS` |
| `nodes` | worker node label `pool=worker`, worker taint `workload=worker:NoSchedule`, the future manager-pool taint `workload=manager:NoSchedule` | `WORKER_NODE_LABEL_KEY`, `WORKER_NODE_LABEL_VALUE`, `WORKER_TAINT_KEY`, `WORKER_TAINT_VALUE`, `MANAGER_TAINT_KEY`, `MANAGER_TAINT_VALUE`, `TAINT_EFFECT` |
| `ports` | relay 8090, and the existing app 8080 and metrics 9464 repeated so a test sees no clash | `RELAY_PORT`, `APP_PORT`, `METRICS_PORT` |
| `paths` | in-pod paths: Secret volume `/run/factory-session`, scratch `/data`, `/tmp`, launcher socket directory `/run/factory` and socket, spool `/spool` | `SESSION_SECRET_DIR`, `SCRATCH_DIR`, `TMP_DIR`, `LAUNCHER_SOCKET_DIR`, `LAUNCHER_SOCKET`, `SPOOL_DIR` |
| `ids` | the image users (supervisor 10001, worker 10002 and group 10010), the sidecar uids (launcher 10003, relay 10004, PROVISIONAL) and the shared group 10001 | `SUPERVISOR_UID`, `WORKER_UID`, `WORKER_GID`, `LAUNCHER_UID`, `RELAY_UID`, `SHARED_GID` |
| `env` | environment variable names, in four groups (below) | `ENV_MANAGER`, `ENV_LAUNCHER`, `ENV_RELAY`, `ENV_POD` |
| `env_values` | the two values of the manager switch | `POD_BACKEND_ON`, `POD_BACKEND_OFF` |
| `timing` | deadline clamp 14400 s, deadline margin 300 s, termination grace 30 s (the numbers the admission policy pins) | `DEADLINE_CLAMP_SECONDS`, `DEADLINE_MARGIN_SECONDS`, `TERMINATION_GRACE_SECONDS` |
| `limits` | per-repository cap 4 | `PER_REPO_CAP` |
| `classes` | the resource classes and the default (below) | `CLASSES`, `DEFAULT_CLASS` |
| `volumes` | spool `sizeLimit` 16Gi, socket directory `1Mi` | `SPOOL_SIZE_LIMIT`, `SOCKET_DIR_SIZE_LIMIT` |
| `canary` | the closed list of forbidden TCP destinations of the worker's network canary, the two API-server probes, the probe path and the denied statuses | `CANARY_FORBIDDEN`, `CANARY_API_PROBES`, `CANARY_API_PROBE_PATH`, `CANARY_API_DENIED_STATUSES` |

**Resource classes** (PROVISIONAL until a read-only production check reads the footprint records of
`status --json`; requests equal limits): `standard` is cpu 2, memory 4Gi, ephemeral-storage 20Gi, one
pod per `D4ds_v5` node, `/data` limit 14Gi and `/tmp` limit 2Gi; `small` is cpu 1, memory 2Gi,
ephemeral-storage 10Gi, three pods per node, `/data` 7Gi and `/tmp` 1Gi. A test keeps at least 2Gi
of each class's ephemeral-storage free for the writable layer and logs, and derives pods per node
from the 3.86 vCPU a `D4ds_v5` node has allocatable (measured on that size only; the default worker size
is now `Standard_D4s_v6`, whose allocatable is not measured). The default class is `standard`.

**Environment variable names.** Each name is produced by exactly one Terraform local and read by
exactly one Python module (a later slice adds that check); none appears twice.

| Group | Names |
|---|---|
| `manager` (main container, when the backend is on) | `FACTORY_POD_BACKEND` (`on` or `off`), `FACTORY_MAX_POD_SESSIONS`, `FACTORY_POD_LAUNCHER_SOCKET`, `FACTORY_POD_SPOOL_DIR` |
| `launcher` (immutable configuration) | `FACTORY_LAUNCHER_IMAGE`, `FACTORY_LAUNCHER_NAMESPACE`, `FACTORY_LAUNCHER_MAX_PODS`, `FACTORY_LAUNCHER_REPO_CAP`, `FACTORY_LAUNCHER_DEADLINE_CLAMP`, `FACTORY_LAUNCHER_ALLOWED_UID`, `FACTORY_LAUNCHER_RELAY_URL`, `FACTORY_LAUNCHER_CLASSES`, `FACTORY_LAUNCHER_API_PUBLIC_HOST`, `FACTORY_LAUNCHER_MANAGER_ADDRESS`, `FACTORY_LAUNCHER_MANAGER_METRICS_ADDRESS`, `FACTORY_LAUNCHER_SOCKET` |
| `relay` | `FACTORY_RELAY_PORT`, `FACTORY_RELAY_SPOOL_DIR` |
| `pod` (the worker pod's allow-list: non-secret names only, none matching KEY, TOKEN, SECRET or PASSWORD, none starting with `AZURE_`) | `FACTORY_SESSION_ID`, `FACTORY_RELAY_URL`, `FACTORY_SESSION_DEADLINE`, `FACTORY_POD_NAME`, `FACTORY_POD_UID`, `FACTORY_NODE_IP`, `FACTORY_MANAGER_ADDRESS`, `FACTORY_MANAGER_METRICS_ADDRESS`, `FACTORY_API_PUBLIC_HOST`, `AZMON_COLLECT_ENV` |

`FACTORY_MANAGER_INSTANCE` is the one name that Terraform does not set: the manager sets it for its
children (below).

**Labels and annotations.** A label key is `factory.internal/<key>`. Label values are lowercase hex
digests or fixed words, at most 63 characters, alphanumeric at both ends: the repository label is
the first 12 hex characters of `sha256("<org>/<name>".lower())`; `run` and `job` are the same kind
of 12-character digest of the run and job id; `session` is 32 hex characters; `attempt` and `phase`
are digits and one of the phase words. Readable ids (the repository, run and job id, the deadline)
go in annotations, never in labels and never issue text or titles.

**Manager instance id.** 12 lowercase hex characters drawn at random at **every** manager process
start (`podcontract.new_instance_id`), kept in memory, exported to the supervisors as
`FACTORY_MANAGER_INSTANCE`, written into the `manager` label of every pod that process launches and
into its session records. A restart draws a new one, so a pod carrying an older id was launched by a
previous manager process. Nothing else about the manager goes into a pod, label or record. The
manager draws it in `Manager.__init__` (slice 12a, below); nothing labels a pod with it yet.

**Python-only constants** (`podcontract.PYTHON_ONLY`; a test proves no Terraform file mentions them).
All PROVISIONAL; real-repository throughput was never measured.

| Constant | Value |
|---|---|
| `MANAGER_INSTANCE_ENV` | `FACTORY_MANAGER_INSTANCE` |
| `HEARTBEAT_SECONDS`, `ORPHAN_SILENCE_SECONDS` | 30, 300 |
| `SCHEDULING_TIMEOUT_SECONDS`, `PULL_ERROR_SECONDS` | 600, 120 |
| `LAUNCH_GRACE_SECONDS`, `ORPHAN_SWEEP_SECONDS`, `SPOOL_RETENTION_SECONDS` | 120, 120, 3600 |
| `BUNDLE_MAX_BYTES`, `DELTA_MAX_BYTES`, `FILE_MAX_BYTES` | 1 GiB, 256 MiB, 64 MiB |
| `DELTA_MAX_FILES`, `PATH_MAX_BYTES` | 20,000, 4096 |
| `TRANSCRIPT_MAX_BYTES`, `STDERR_MAX_BYTES` | 512 MiB, 16 MiB |
| `RELAY_IDLE_TIMEOUT_SECONDS`, `RELAY_STREAM_TOTAL_SECONDS`, `RELAY_TRANSFER_TOTAL_SECONDS`, `RELAY_MAX_THREADS` | 60, 120 (stream chunk, heartbeat, preflight), 900 (bundle or result transfer), 32 |
| `SPOOL_REFUSE_BYTES` | 12 GiB: at most 75% of the spool `sizeLimit`, because a tripped `sizeLimit` evicts the manager pod, so the refusal must come first |
| `PACING_BURST`, `PACING_INTERVAL_SECONDS` | 2, one session per 15 s |
| `KUBE_CONNECT_TIMEOUT_SECONDS`, `KUBE_READ_TIMEOUT_SECONDS`, `KUBE_RETRIES`, `KUBE_RETRY_BACKOFF_SECONDS` | 5, 30, 3, (1, 2, 4) |

## How the two sides stay equal

* `infra/test/test_pod_contract.py` parses `worker_contract` (a small literal reader, not an HCL
  evaluator) and compares it with `podcontract.SHARED`. It fails on a value that differs and on a
  key or list entry that exists on one side only. It loads `podcontract.py` by path, because CI runs
  the infra tests with `python3 -I` and no package installed, so the module is standard library
  only and has no relative import.
* `tests/test_podcontract.py` reads the syntax tree of `podcontract.py`: every public constant is
  either read from `SHARED` by subscript (so it cannot be retyped with another number) or listed in
  `PYTHON_ONLY`, and the Python-only list is pinned. It also checks label and name syntax, that no
  two ports or uids clash, that class sizes are Kubernetes quantities and leave room for the writable
  layer, that every environment name appears once, and the closed canary list.
* `tflint`'s recommended preset includes `terraform_unused_declarations`, which would fail CI on a
  local nothing reads. While nothing read `local.worker_contract` (slice 0) the local carried an ignore
  annotation for that rule on the line above it, and the infra test failed when the annotation was
  missing while nothing read the local **and** when it was still there once something did. Slice 14
  (`infra/worker_pool.tf` reads the names, the node label and taint and the class table) removed it;
  the test now fails if it is put back.
* **To change the contract**, edit `SHARED` (and the constant that reads it), the Terraform local and
  this page together; both tests fail otherwise. A PROVISIONAL number is replaced by a small pull
  request after a measurement, never silently.

## Supervisor git hardening (slice 3; it also protects today's in-process mode)

A worker owns the files of its worktree (in a pod it would own a whole `.git`). git reads
`core.fsmonitor`, `core.hooksPath` and the hook directory from the repository it discovers, and each of them names a
program that would run as the supervisor, with the supervisor's environment (the GitHub credentials). So
`src/factory/gitsafe.py` is the only way supervisor code runs git on a tree a worker has touched:

* `GIT_DIR` is the per-worktree administrative directory of the main clone (`<repo>/.git/worktrees/<name>`) and
  `GIT_WORK_TREE` the worktree, both explicit; a `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE` or similar variable of the
  caller's environment is dropped, and `GIT_CONFIG_NOSYSTEM=1` is set.
* Every call, also `status`, `rev-parse`, `diff` and `merge-base`, carries `-c core.fsmonitor=false -c
  core.hooksPath=/dev/null` (`status` runs the fsmonitor program and the `post-index-change` hook; `diff` runs the
  fsmonitor program). A call in the supervisor's own clone (`repo_git`) gets the same pairs and the same environment
  without an explicit `GIT_DIR`: it reads the common configuration that every worktree shares.
* A nested repository is a second source of programs. When the index holds a gitlink, or the worktree holds an untracked
  repository, `status`, `diff` and `add` start a child `git status` inside it, and that child reads the nested
  repository's own `.git/config` and the worktree's `.gitattributes`: a `filter.<name>.clean` program runs as the
  supervisor, with its environment. The sandbox only denies writes to the worktree's own `.git`, so a worker can write
  `vendor/lib/.git`, and the supervisor's own `add --all` records it as a gitlink (the next `status` fires the filter).
  Three layers stop this. Every call carries `-c diff.ignoreSubmodules=dirty` (it reaches the child through
  `GIT_CONFIG_PARAMETERS`), and `status` and `diff` get `--ignore-submodules=dirty` on the command line, because a
  `.gitmodules` `ignore` setting of the worktree overrides the configuration but not the option; `dirty` starts no child
  git and still shows a gitlink whose commit moved. `add` has no such option and the checkout commands read the worktree
  too, so before any subcommand outside a fixed allow-list of ones that never read the worktree (`rev-parse`,
  `merge-base`, `fetch`, `push`, `branch`, ...) the call lists the gitlinks of the index and the untracked nested
  repositories (`ls-files`) and raises `GitSafetyError("worktree holds a nested git repository that is not a
  submodule of it")` unless each one has no `.git`, or a `.git` FILE pointing into the `modules` directory of the
  trusted git directory (what `git submodule update` writes, where a worker cannot write) and the path is not reached
  through a symlink; a submodule that passes is checked the same way with its own git directory (a submodule of a
  submodule starts a child git of its own; nesting deeper than 8 is refused). A repository a builder left behind, or planted, therefore fails the job before the supervisor
  stages it; a `.gitignore`d one is never staged or visited. `-c submodule.recurse=false -c
  fetch.recurseSubmodules=false` keep `fetch` and the checkout commands out of submodules.
* The environment is `shell.supervisor_env()`, never the process environment, so a git child (and every hook, helper or
  submodule transport it starts) no longer inherits the Anthropic key or the App private key. `agent.repo_guidance` and
  `agent.git_common_dir` used to run raw `git` with no `env=` on every phase; they go through `gitsafe` now.
* The administrative directory is learned from the main clone (the `gitdir` back-pointer of
  `<repo>/.git/worktrees/<name>`, which a worker cannot write), recorded as `gitdir` in the job state when the worktree
  is created and, for a run that already had a worktree, resolved the same way on first use. It is never read from the
  worktree's own `.git` pointer after a worker has run. A recorded value that disagrees with the clone fails the call.
* Before each call the pointer `<worktree>/.git` must still read `gitdir: <administrative directory>` exactly (a symlink,
  a directory or any other text is a change). Otherwise the call raises `GitSafetyError("worktree .git pointer was
  changed")`, git is not started and the job fails. A linked worktree nobody registered is refused the same way
  (`agent.py` has only a path; the supervisor registers every worktree it creates or reuses, in `gitsafe`, before a
  worker runs there).
* The `CalledProcessError` it raises carries the caller's argv without the injected pairs, so `Runner.push_or_park`
  (`error.cmd[:2] == ["git", "push"]`) still classifies a workflows-permission rejection as `needs_human_publish`.

What still runs raw, and why (the explicit allow-list of `tests/test_gitsafe_lint.py`): in `cli.py` the clone, the
`core.sharedRepository` setting, `rev-parse` of the operator's own repository, and in `planner_worktree` the `fetch`, the
`show-ref` and the `worktree add`, all in the supervisor's own clone and never in a tree a worker touched.
`preflight.py` has a `git rev-parse` too, but it is inside the probe script that the worker itself runs in its sandbox, not a
supervisor call. `tools/sandbox_e2e.py` is not the supervisor, but it calls
`run_agent` on a worktree it creates, so it registers that worktree with `gitsafe.adopt` the way the supervisor does.
The same test fails on any new call outside `gitsafe.py` and that allow-list that its scanner can read as running git:

* a list or tuple whose first element is `"git"`, a path whose last component is `git` (`"/usr/bin/git"`) or a name bound
  to such a string literal in the same module (`GIT = "git"`, one level), also `["git", *args]`, whichever function
  consumes it;
* a list that starts a shell with a command string that runs git (`["sh", "-c", "cd x && git status"]`; `sh`, `bash`,
  `dash` or `zsh`, with `-c` or `-lc`);
* a string with a git word anywhere in it (`cd x && git status`, `GIT_PAGER=cat git status`, `/usr/bin/git status`,
  `sudo git status`, `$(git status)`), split on whitespace, `; & | ( )` and the backtick, with `NAME=value` words
  never counted as a program, handed to a runner (`subprocess` functions, `command`, `os.system`, `os.popen`,
  `getoutput`, under any import alias, as `args=`/`command=`/`cmd=`, or any call that passes `shell=True`), written
  as a literal, an f-string, `%`, `.format` or a `+` chain (every literal part is read), or turned into an argv by
  `.split()` or `shlex.split()`;
* `os.exec*` and `os.spawn*` that name git.

The lint is a safety net, not a proof. It reads one module at a time and decides only what the syntax decides, so it
cannot see these, and a reviewer is still the check on them (a test of the lint pins each one, so this list and the
scanner cannot drift apart):

* a command string built at run time (a git word assembled from parts, a call result, a function's return value);
* a helper that takes the command as a parameter (`def sh(c): subprocess.run(c, shell=True)` called with
  `sh("git status")`: the literal and the runner are in different functions and the scanner does not follow a value
  into a parameter);
* a command read from data (the environment, a file, a configuration, a database row);
* a program found at run time (`GIT = shutil.which("git")`, `self.git`, `settings.GIT`, an imported constant, a constant
  bound to another name, a name interpolated into a string);
* a wrapper such as env or sudo ahead of git in a list (`["env", "git", "status"]`; in a string it is seen);
* a runner the scanner does not know by name (`asyncio.create_subprocess_exec`, `pexpect.spawn`, an attribute called
  something else);
* shell quoting or expansion that spells git without writing it (`$GIT status`, `g''it status`).

None of them is a git call in `src/factory` today (the one command read from data, `FACTORY_TOKEN_BROKER` in
`github_auth.py`, is the operator's own token program).

## The worker pod template and the policy mirror (slice 6)

`src/factory/podspec.py` is the single source of the pod design. It is a set of pure functions over
plain dicts (standard library only, no Kubernetes client, no environment, no clock of its own):

| Function | What it does |
|---|---|
| `build_pod(request, config)` | the manifest of one worker pod, from a **closed** request and the launcher's `PodConfig` |
| `build_secret(pod, token, key)` | the per-session Secret of a created pod (the values are arguments, never constants) |
| `check_policy(pod, allowed_image)` | the ids of the pod rules that `pod` breaks (an empty list means admitted) |
| `check_secret_policy(secret, pod_name)` | the same for a Secret |
| `apply_patch(document, patch)` | a small JSON patch (`add`, `remove`, `replace`) used to replay the mutation files |

**The request** has exactly these fields, and an unknown or missing one is an error: `session_id`,
`repository` (`<org>/<name>`; the repository label is derived from it with `repo_key`, so the label
and the anti-affinity value cannot disagree), `run_id`, `job_id`, `attempt`, `phase`,
`resource_class` (an enum of the contract), `timeout_seconds`, `manager_instance`. The image, the
namespace, the relay address, the two canary hosts and the clock come from the launcher's own
`PodConfig`, never from a request. Errors carry fixed texts and never echo the rejected value.

**The pod** (every name, label, port, path, class and number is read from `podcontract`):

* metadata: name `factory-w-<session id>`, namespace `factory-workers`, the eight contract labels
  and the annotations `karpenter.sh/do-not-disrupt: "true"` and the readable ids; no
  `azure.workload.identity/use` label, no `ownerReferences`.
* spec: `restartPolicy: Never`; `activeDeadlineSeconds` is the timeout clamped to 14400 s plus the
  300 s margin; `terminationGracePeriodSeconds: 30`; `hostUsers: false`; service account
  `factory-worker`; `automountServiceAccountToken: false`; `enableServiceLinks: false`; no
  `hostNetwork`, `hostPID`, `hostIPC`, `nodeName`, `shareProcessNamespace`, `priorityClassName`,
  `schedulerName` and no pod-level `securityContext`.
* one container `worker`: the pinned image digest of the config, a fixed command (the root wrapper
  under `tini`), a `securityContext` of exactly `procMount: Unmasked` and
  `appArmorProfile: {type: Unconfined}`, no ports, probes or `envFrom`, requests equal to limits for
  cpu, memory and ephemeral-storage from the class table.
* environment: exactly the allow-list of the contract (`ENV_POD`), non-secret names only, plain
  values except the pod name, uid and node IP, which come from `fieldRef`; `AZMON_COLLECT_ENV` is
  `False` so that Insights does not collect the container environment. No `secretKeyRef`, no name
  that looks like a key, token, secret or password, no `AZURE_*`.
* volumes: only `emptyDir` at `/data` and at `/tmp`, each with the `sizeLimit` of the class, and the
  pod's own Secret at `/run/factory-session` (`defaultMode` 0400, mounted read-only). No `hostPath`,
  `projected`, `csi`, `nfs`, `configMap` or `downwardAPI` volume and no service-account token volume.
* scheduling: `nodeSelector` exactly `pool=worker` and `kubernetes.io/os=linux`; the toleration of the
  worker taint and no other; a **required** `podAntiAffinity` on `kubernetes.io/hostname` whose
  selector is `factory.internal/repo NotIn [<this pod's own repo label>]`, so a node that holds a pod
  of another repository (or an unlabelled pod) of the same namespace is refused: one repository per
  node. The `NotIn` value is the pod's own label by construction, and the mirror pins it.
* the Secret: Opaque, named `<pod name>-s`, in `factory-workers`, keys `token` and
  `anthropic-api-key` (base64 in `data`, which is what admission sees), an `ownerReference` to the
  pod's name and uid and **no** `blockOwnerDeletion`, so no finalizer permission is needed.

**Golden files** are in `tests/golden/`: one pod per resource class and phase (`pod_<class>_<phase>.json`,
ten files; the verify one shows the clamp) and two Secrets with placeholders (`secret_*.json`; the
real values appear nowhere). They are byte-compared with the builder's output; regenerate them with
`PYTHONPATH=src python3 -m tests.test_podspec --regenerate` and review the diff.

**The policy mirror.** `check_policy` and `check_secret_policy` are a Python model of the intended
ValidatingAdmissionPolicies (a later slice writes the CEL). Every rule has a stable id; the CEL
carries the same ids and that slice compares the two lists with `podspec.POLICY_RULES`. The mirror
fails closed: a field that is missing, of the wrong type or unknown is a violation, never an
exception, and so is a value nested too deeply to walk (the rule that cannot read it is broken; a
test builds one 20000 levels deep). The rules:

| Area | Rule ids |
|---|---|
| Image and process | `image` (equal to the launcher's pinned digest, the parameter of the real policy), `command`, `container-fields` (a closed key set per container, `imagePullPolicy: IfNotPresent`), `containers` (exactly one, named `worker`; no init and no ephemeral container) |
| User namespace and host | `host-users` (`false`, an absent field is a denial), `host-network`, `host-pid`, `host-ipc`, `node-name` |
| Security context | `privileged`, `capabilities`, `run-as`, `seccomp`, `proc-mount` (`Unmasked`), `security-context-keys` (exactly the two relaxations and no pod-level context) |
| Identity | `service-account` (`factory-worker`), `automount` (`false`, an absent field is a denial), `identity-label`, `service-links` |
| Volumes | `host-path`, `volumes` (exactly the three volumes, a `sizeLimit` at most the largest class, the pod's **own** Secret, mode 0400), `volume-mounts` |
| Environment | `env-names` (the allow-list, each name once), `env-source` (no `secretKeyRef` and no `envFrom`; `fieldRef` only for the three downward-API names; the session id and deadline equal the label and the annotation; the relay URL is `http` on the relay port), `env-collect` |
| Scheduling | `node-selector`, `tolerations`, `anti-affinity` (required, hostname topology, `NotIn` the pod's own repo label and nothing else) |
| Lifecycle and size | `restart-policy` (`Never`), `deadline` (an integer from 1 to clamp plus margin), `grace` (an integer from 0 to 30), `resources` (requests equal limits, exactly the three resources, at most the largest class) |
| Metadata | `labels` (exactly the eight contract labels, valid values, the session label equals the name suffix), `annotations` (exactly the contract annotations), `owner-references`, `pod-name`, `pod-metadata` (a closed set of top-level and metadata keys), `spec-fields` (a closed set of spec keys: `schedulerName`, `priorityClassName`, `hostAliases`, `dnsPolicy`, `runtimeClassName` and the rest are refused) |
| Secrets | `secret-namespace`, `secret-name` (`<pod name>-s`), `secret-type` (`Opaque`), `secret-keys` (exactly `token` and `anthropic-api-key`, both non-empty base64), `secret-owner` (one `ownerReference` to that Pod, no `blockOwnerDeletion: true`), `secret-fields` (a closed set of top-level and metadata keys) |

The rule ids that the program's spec names are the first ones of each area; the others were added
because a field that a launcher could vary had no rule of its own (`volume-mounts`, `env-collect`,
`service-links`, `command`, `container-fields`, `pod-name`, `pod-metadata`, `spec-fields`,
`secret-fields`). The later CEL slice must carry them too.

**Completeness is enforced, not hoped for.** `tests/golden/policy_mutations.json` (the pod) and
`tests/golden/policy_secret_mutations.json` (the Secret) hold mutations: JSON patches of the golden
objects, each with `must_deny: true` and the id of the rule it exercises. `tests/test_podspec.py`
fails unless every id has at least one mutation, every mutation names a known id and really changes
the golden, every golden pod and Secret is admitted, and every mutation is denied naming its rule.
A second test sweeps the golden pod systematically: every field is replaced by a wrong value,
removed, and every object and list gets an unknown key or entry; each must be denied. **The sweep
replaces a field with a malformed value on purpose** (`None`, an empty string, a string with a
space, a boolean, a negative number, a float, an empty or odd list or object). It therefore proves
that every field is checked, not that every field is pinned to one value. A deleted rule of the
mirror, or a loosened one, fails these tests.

What the mirror pins, in plain words:

* **Pinned to an exact value** (any other value, well-formed or not, is denied): the image (the one
  parameter), the command, the security context, the host flags, the service account, the token
  automount, service links, the node selector, the toleration, the anti-affinity (its `NotIn` value
  must be the pod's own `repo` label), the restart policy, the closed key sets of the metadata, the
  labels, the annotations, the container and the spec, the volume names, the mount paths and the
  session volume (name, mode, secret name), the three downward-API variables, `AZMON_COLLECT_ENV`,
  the scheme and port of the relay URL, and in the Secret the namespace, name, type, key names and
  the owner's kind and name.
* **Pinned only by format or range** (another well-formed value is ADMITTED): the `repo`, `run`,
  `job` and `manager` labels (12 hex characters each), `attempt` (one to four digits), `phase` (any
  of the five phases) and the session id (the name, the `session` label, `FACTORY_SESSION_ID` and the
  session volume's secret name must agree with each other, but any 32-hex id does); the `repo`,
  `run-id` and `job-id` annotations (any value of the contract's shape); the deadline annotation and
  `FACTORY_SESSION_DEADLINE` (any number of up to 12 digits, if the two are equal); the **host** of
  `FACTORY_RELAY_URL` (any host name or address, for example `http://203.0.113.9:8090`),
  `FACTORY_MANAGER_ADDRESS`, `FACTORY_MANAGER_METRICS_ADDRESS` and `FACTORY_API_PUBLIC_HOST` (the three canary hosts); the resources
  (requests equal to limits, and any quantity above zero up to the largest class: the mirror does
  not know the pod's own class, so `"1"` for the cpu of a pod built for `"2"` is admitted);
  `activeDeadlineSeconds`, the grace period and the two `sizeLimit` values (a range); and in the
  Secret the two `data` values (any non-empty base64) and the owner's `uid` (any UID-shaped value).

`test_well_formed_changes_stay_admitted_where_the_policy_pins_a_format_or_a_range` (the pod) and
`test_well_formed_changes_of_a_secret_stay_admitted_too` name these changes and assert each is
admitted; `test_the_parts_of_those_fields_that_are_exact_are_still_denied` pins the exact parts next
to them. They document what the mirror does today; they are not a promise.
**Decided in slice 15b:** the CEL pins the same things the mirror does, in the same way: the relay URL by a pattern
(`http`, a host of letters, digits, dots and hyphens, the relay port), the deadline by a range (1 to the clamp plus the
margin), the labels by their shapes and the resource quantities by the largest value of the class table (not a class).
Where the CEL is stricter, it says so ("Terraform B2", below).

**The mirror is not evidence that the CEL works.** It is an offline model written next to the
template, and the real policy is the ValidatingAdmissionPolicy of slice 15b, which replays the same
golden files against a real API server (`tools/policy_matrix.py`). Until then every claim about admission is UNVERIFIED: that
a real API server accepts the pod as written (field names, `appArmorProfile` and `procMount` with
`hostUsers: false`), that the CEL can express each rule (it cannot list the fields of a typed pod, so
a closed key set must be written as one denial per field of the pinned API version), and what the API
server does to the pod before validating it (it defaults and mutates: for example it adds two
`NoExecute` tolerations, a scheduler name and a DNS policy, none of which the mirror models). The
mirror also judges a `CREATE` of the object as the launcher builds it.

## To change the pod design

Edit `podspec.py` (and `podcontract.py` and the Terraform local for a shared value), regenerate the
golden files, add or adjust the mutations that exercise the changed rule and update this page. A rule
whose mutation is missing, or whose id is missing here, fails a test.

## The backend seam (slice 4)

Every session (plan, build, review, verify, preflight) goes through `agent.run_agent`, called by
`Runner.invoke`, the planner and the preflight probe. Slice 4 splits it into three steps behind
`AgentBackend` (`src/factory/backend.py`) and moves the code, with **no behaviour change**:
`run_agent` keeps its signature and its callers and gains one optional argument, `backend`, whose
default is the `LocalBackend` (the in-process backend, the code that used to be the function).
The `execution` setting that chooses between backends is slice 12a (below); the second backend is `PodBackend` of slice 12b (below).

| Step | What it does | Returns |
|---|---|---|
| `prepare(request)` | Refuses a call without a worker identity when the image requires one, decides the memory limit, resolves the phase policy, writes the prompt, settings and system-prompt files and the throwaway home, builds argv and, for a separate worker identity, shares those files with its group. Starts nothing. | `LaunchSpec` |
| `launch(spec, on_start)` | Starts the session, calls `on_start(handle)`, waits for it, kills its process group (SIGTERM, five seconds, SIGKILL) on a failure or at the time limit (`AgentTimeout`), and releases what it held: the live-group entry and the throwaway home. | `Outcome` |
| `collect(spec, outcome)` | Reads `<prefix>.jsonl` and the outcome and returns the structured result and the usage record, or raises the error the callers already handle (the fixed failure text, the exit-code messages, the memory-limit message with the spend kept). | `(result, usage)` |

* `AgentRequest` is the arguments of `run_agent` as one value.
* `LaunchSpec` is a plain, immutable description a backend can run or ship: the phase and model,
  `argv` (the `claude` command line, the binary first), the non-secret environment additions
  (`HOME`, `CLAUDE_CONFIG_DIR` and the three switches; the Anthropic key is added by the backend that
  starts the process and is never part of the description), the working directory and prefix, the
  run artifacts as `SessionFile`s (absolute path, bytes, mode), the extra read-only paths, the prompt, the time
  limit, the optional user and group, the write scope of the phase policy (`none` or `cwd`), the
  resource class (the contract's default; a process backend ignores it) and the memory limit.
  Immutable means all of it: the attributes cannot be rebound (it is a frozen dataclass), `env` is a
  read-only mapping (a `types.MappingProxyType` over a copy, so assigning to it raises `TypeError`)
  and `argv`, `files` and `read_paths` are tuples, whatever the code that built the spec passed in.
  A backend that ships `env` copies it first (`dict(spec.env)`: `json.dumps` and
  `dataclasses.asdict` do not take a read-only mapping) and adds the Anthropic key to its own copy,
  never to the description.
* `AgentHandle` is what `on_start` receives once the session runs. Its `pid` is the worker's
  process group id for the local backend, which is what callers record; `run_agent` hands its own
  callers that number, so `record_pid` is unchanged. A handle without a pid (a session that does
  not run in a local process) records none. The additive pod handle is slice 5 (below); `AgentHandle` carries it as `pod` since slice
  12b, and `agent._pid_callback` forwards it to `on_start` (which `State.record_started` records as the job's `pod`). A handle has a
  pid or a pod, never both.
* `Outcome` is what `launch` observed: the exit code, the footprint and, when the memory guard
  killed the session, its fixed message.

**What a second backend must provide.** All three steps. `prepare` raises before it writes or
starts anything (`ValueError` for a bad phase or option, `AgentError` for a refused deployment).
`launch` calls `on_start` at most once, after the session exists and before it is awaited (an
exception from `on_start` ends the session); leaves the transcript in `<prefix>.jsonl` and the
error stream in `<prefix>.stderr`; raises `AgentTimeout` at the time limit; and releases what it
holds, the throwaway home that `prepare` created included, on every path. `collect` reads only the
files under the prefix and the outcome. A backend may reuse `LocalBackend.prepare` and `collect` by
inheritance and replace `launch` (a test does exactly that with a backend that runs no process).

**Where a failure is classified (slice 1).** In `collect`, because that is the one step that sees the
transcript: `_failure` (the fixed failure text) and `_no_result` (a session that left no `result`
event) in `backend.py` ask `apierror.classify` and attach its closed code to the `AgentError` they
raise (`AgentError.api_error`, `None` unless the classifier recognised an API error; the message text
is the same with or without it). `_no_result` reads a bounded tail of `<prefix>.jsonl` with
`agent.log_tail` and raises the plain `RuntimeError` of before when the tail shows no API error. The
runner records the code from the error in the job's state and the breaker reads it back from there
(`docs/capacity.md`, "Stop-the-world breaker"), so a second backend **must classify its failure the
same way**: either inherit `LocalBackend.collect`, or build its failure errors with `_failure` and
`_no_result`, so that a spend limit that ends a session in a pod reaches the breaker exactly like one
that ends a local session. A `collect` that raised an `AgentError` without the code would silently
turn the breaker off for that backend.

**What did not move.** The helpers (`build_argv`, `child_env`, `last_result`, `log_tail`,
`usage_record`, `signal_group`, `kill_live_agents` and its live set, the error types) stay in
`agent.py`, and `LocalBackend` reaches them through that module, because other modules import them
from there and existing tests patch `factory.agent.git_common_dir`, `last_result`, `subprocess.Popen` and
`permissions`. The lease-loss hook registered in `cli.py` is still `kill_live_agents`; a later slice
registers a second hook for pods.

**The transcript is data, not a trusted source.** For a pod session `collect` reads a transcript the pod wrote,
so `agent.last_result` and `agent.usage_record` do not trust it (`tests/test_transcript_trust.py`), for every
backend alike. `last_result` reads the file in bounded pieces and skips any line longer than
`agent.MAX_LINE_BYTES` (1 MiB), so one 512 MiB line costs the manager about a megabyte, not gigabytes. The
result event's `subtype` is used only when it matches the closed token pattern (`[A-Za-z0-9_.:-]{1,64}`, the
one `terminal_reason` and `api_error_status` already needed); anything else is the fixed word `unknown`, in the
failure message and in state. `usage` and `modelUsage` are cut before they are stored: token-pattern keys only,
finite non-negative numbers (at most 2^53), short tokens, at most 32 keys per object, 16 models and 2 levels
of nesting. `total_cost_usd`, `num_turns` and `duration_ms` are finite and non-negative, else 0; a cost above
`agent.MAX_COST_USD` (100000, the value of `podpool.MAX_USD`; a test pins that they are equal) is clamped to it.
`status.cost_usd` and `Runner.run_cost` ignore a negative or non-finite stored value and never return more
than that bound, so a pod can neither lower the run's cost (and slip past `run_budget_usd`) nor push the spent
figure out of the range `podpool.Request.for_launch` accepts (which would refuse every later pod session of the
run). The bound is a constant, not the phase's budget: the budget is not in the launch spec.

**How the move is proven.** `tests/golden/agent/*.json` were captured from `run_agent` before the
move (argv, environment, the prompt, settings and system files with their modes, the permission
calls for a separate worker identity, the order of the launch steps and the text of every error);
`tests/test_backend.py` replays them against the moved code, next to the unchanged existing suite.
These checks run locally and in CI; nothing about this slice needs a cluster.

## The `execution` setting and the manager switch (slice 12a)

Everything that must exist before pod code can be reached, and nothing that launches a pod. Pod mode
is **refused** here unless the backend of slice 12b can be built (below).

**`execution`** is a key of the repository's config file (`docs/configuration.md`),
validated like the other choice keys (`config.EXECUTION_MODES`, listed in `RepoConfig`):

| Value | Meaning |
|---|---|
| `"inprocess"` (the default) | Every session is a child of the supervisor, as it always was. The three callers of `agent.run_agent` pass no `backend`, so the call, its argv and its behaviour are unchanged; the golden captures of slice 4 (`tests/golden/agent`) and every existing test pass unedited. |
| `"pod"` | A pod per session. **Refused**, with a fixed text, before the session's own files are written or its process is started (below), unless the switch is on and the backend can be built (slice 12b); a `plan` or `preflight` run has by then fetched and prepared its worktree, which the refusal does not undo. |

Only the owner sets it. It is per repository and covers **every** session of the repository (plan,
build, review, verify and the preflight probe): `Runner.invoke`, the planner and the preflight probe
read it the same way (`podexecution.agent_kwargs`), and there is no per-phase split. A change is a
change of the repository config, which on AKS is a new immutable ConfigMap and a Deployment rollout
behind the drain gate (`docs/operations.md`, "Where the settings live").

**`FACTORY_POD_BACKEND`** is the manager-wide enabling switch (`podcontract.ENV_MANAGER`, the values
`podcontract.POD_BACKEND_ON` and `POD_BACKEND_OFF`). It is set to `on` only by Terraform when it
enables the backend (a later slice). Only the exact value `on` is on: unset, `off`, any other
spelling or garbage is off, so a typo fails closed. The manager reads it once at start and gives
every supervisor it spawns the canonical word (`on` or `off`).

**What happens with `execution = "pod"`:**

| Switch | Result |
|---|---|
| not `on` (unset, `off`, anything else) | the session is refused: `execution = "pod" is refused: the pod backend is switched off on this deployment (FACTORY_POD_BACKEND is not on) and nothing was started.` plus the way back |
| `on`, but the backend cannot be built for this session | refused, with its own text: `execution = "pod" is refused: the pod backend is not configured for this session (see docs/pod-backend.md) and nothing was started.` plus the way back (the supervisor did not wire it in, no manager instance id, an unusable launcher socket, spool or data directory, an invalid job or base commit) |
| `on` and the backend can be built | `podbackend.PodBackend` (slice 12b) |

Slice 12b replaced the one refusal that said "this build has no pod backend yet" (`podexecution.backend_for` now builds the backend, or
refuses with the text above when it cannot); the first row is unchanged. The way back, in both texts, is to remove `execution = "pod"`
from the repository config.

* **No silent fallback.** A refused session is never run in process: that would run the workers of a
  repository that asked for pods inside the manager pod. The refusal is `RefusedBackend`, an
  `AgentBackend` whose `prepare` raises `agent.AgentError` before a file is written or a process
  exists, so it takes the error path the callers already handle (the job fails with the fixed text,
  the planner or the probe stops with it). The texts are constants of `podexecution`; they never
  contain a value from the configuration, an issue or the environment.
* **Manager instance id.** `Manager.__init__` draws `podcontract.new_instance_id()` at every start of
  the process and sets it as `FACTORY_MANAGER_INSTANCE` in the environment of every supervisor and
  command it starts. A value already present in the inherited environment is replaced, so a
  restart is never mistaken for the process before it. Slice 12b sends it to the launcher with every launch
  (the launcher labels the pod with it and the session record carries it); slice 20 sweeps by it.
* **Where it is documented:** the repository key in `docs/configuration.md`, `docs/manager.md` ("Repository
  settings") and `docs/operations.md`; the two environment names in the variable table of
  `docs/manager.md`.

## Terraform A: the cluster and the worker pool (slice 14, issues #144 and #146)

Everything here is **default off, nothing is switched on**. No apply is run by an assistant: the checks
that ran are named in the pull request, and the live ones are for a person with Azure access.

| Piece | Where | Default |
|---|---|---|
| `network_data_plane` (`azure` or `cilium`), `network_policy` (`cilium` only) | the `network_profile` of `azurerm_kubernetes_cluster.this` | `null`: the argument is omitted |
| `nap_mode` (`Auto` or `Manual`), `nap_default_node_pools` (`Auto` or `None`) | a `dynamic "node_provisioning_profile"` block | `nap_mode` `null`: no block; pools `None` |
| `worker_nodepool_enabled` | count of `kubernetes_manifest.worker_nodeclass` and `kubernetes_manifest.worker_nodepool` (`infra/worker_pool.tf`) | `false`: 0 objects |
| `worker_pool_limits_cpu`, `worker_pool_sku`, `worker_pool_pods_per_node` | `local.worker_pool` | 32, `Standard_D4s_v6` (a Dsv6 size rather than the `Dds_v5` family of the other pools), 1: a ceiling of 8 |
| `family_vcpu_quota` | the surge-reserve precondition | `null`, required only with the pool |
| `lifecycle { prevent_destroy = true }` | `azurerm_kubernetes_cluster.this` | always on |

* **Order (never a pull request).** (1) Cilium, (2) node auto-provisioning, (3) the NodePool objects,
  (4) the pod backend (slices 15a to 17). The values live in `INFRA_TFVARS`; each step is a manual run
  behind the drain gate, after snapshots, and only after a scratch deployment has run it. The steps and
  what each does are in `infra/README.md`, "Safe deployments", "Switching the data plane".
* **The NodePool.** `Standard_D4s_v6` (not measured: the allocatable of the contract's 3.86 vCPU is from `D4ds_v5`), `on-demand`, `amd64`, `linux`, label `pool=worker` and
  `kubernetes.azure.com/ebpf-dataplane: cilium`, taint `workload=worker:NoSchedule`, `limits.cpu` from the
  local, `WhenEmpty` after 30 s, **`expireAfter: Never`** and **no `terminationGracePeriod`** (it bypasses
  `do-not-disrupt`). The only things that can then end a session early are
  its deadline, a manual deletion and the loss of the node. The AKSNodeClass is `imageFamily: AzureLinux`
  (there is no `AzureLinux3` value) with a kubelet `podPidsLimit` (a pod spec cannot carry one).
* **One local, one ceiling.** `max_sessions = floor(limits_cpu / node_vcpu) * pods_per_node`, 8 by
  default. The NodePool limit reads it now; `FACTORY_MAX_POD_SESSIONS` and the launcher's maximum of live
  pods will (slices 17 and 18); `FACTORY_MAX_TOTAL_WORKERS` is not derived from it. `pods_per_node` is 1
  because one repository per node is a prerequisite: N repositories need N nodes.
* **Surge reserve.** With the pool on, `limits_cpu + 2 * production_vcpu <= family_vcpu_quota`, where
  `production_vcpu` counts only the production pools in the worker pool's VM family (the quota is per
  family), one node each; the figures are a function of the configured pools and the worker size.
  A worked example with invented figures, which are not any real subscription's (read your own quotas with `az vm list-usage`):
  a deployment runs 12 vCPUs, all `Dds_v5` (system `D2ds_v5`, factory `D8ds_v5`, `mon` `D2ds_v5`; 10
  without `mon`), the `Dds_v5` family quota is 80, `Dsv6` and `Dsv7` carry 200 each, the regional total is
  120. With the default `Standard_D4s_v6` the production pools are not in the worker family, so the
  check asks only that the limit fit the family quota (up to 200); the **regional total is not checked by
  Terraform** and binds: the largest limit is 96 (24 nodes) and the default 32 leaves 76 before a
  surge and 64 while system, factory and `mon` surge together. With a size of the production family
  (`Standard_D4ds_v5`, quota 80) the largest limit is 56 (14 nodes; 60 and 15 only without `mon`) and
  the default 32 leaves 36 before a surge and 24 while all three surge. A scratch deployment in the same
  subscription shares the quotas, which the validation cannot see.
* **Guard.** Both manifests are listed under `POOL_OBJECTS` in `tools/infra_cluster_guard.py`: setting
  `worker_nodepool_enabled = false` in `INFRA_TFVARS` would DELETE the NodePool, a disruption that ignores
  `do-not-disrupt`, so an automatic run after an image build (your own pipeline's) refuses it and a manual run applies it
  through the drain gate.
* **`prevent_destroy`.** A plan that would replace or destroy the cluster now fails, because the data
  disk, the Caddy disk and the Prometheus disk die with it. It cannot be conditional; the teardown of a
  scratch deployment and a deliberate re-create work on a scratch copy of `infra/` with the line removed, never
  committed.

## Terraform B1: the worker namespace (slice 15a, issues #144 and #145)

Everything here is **default off, nothing is switched on**: one new variable, `pod_backend_enabled` (default `false`),
gates the three objects of `infra/worker_ns.tf`, the log exclusion and the relay rule, so a default merge has no plan
line. No apply is run by an assistant; the checks that ran are named in the pull request. With the variable off the Container Insights ConfigMap data is byte-identical to what it was before.

| Piece | Where | Default |
|---|---|---|
| `pod_backend_enabled` | `count` of the namespace, the service account and the NetworkPolicy; the log exclusion; the relay rule | `false`: 0 objects, no exclusion, no relay rule |
| namespace `factory-workers` (Pod Security `privileged`, label `factory.internal/role=worker`) | `kubernetes_namespace_v1.worker` | not created |
| service account `factory-worker` (`automount_service_account_token = false`, **no RoleBinding**) | `kubernetes_service_account_v1.worker` | not created |
| default-deny NetworkPolicy `default-deny` | `kubernetes_network_policy_v1.worker_default_deny` | not created |
| `exclude_namespaces` for stdout and stderr | `local.k8s_log_exclusion_lines` in the `container-azm-ms-agentconfig` data | empty: the data is unchanged |
| the relay rule of the prepared `factory-ingress` policy | `kubernetes_manifest.factory_network_policy` (`monitoring.tf`) | absent, and the policy itself is off |

* **Preconditions of the namespace.** `network_data_plane == "cilium"`, `nap_mode == "Auto"` and
  `worker_nodepool_enabled`: without an engine the policy is accepted and not enforced, and without the pool no
  node can run a worker pod. They are `lifecycle` preconditions, so they are evaluated only when the variable is on.
* **The namespace is privileged** because the three relaxations of the worker pod need it. What keeps that safe is
  not the label: creating a pod there needs a Role that only the launcher is bound to (slice 16), a
  ValidatingAdmissionPolicy pins what a pod may say (slice 15b) and the policy below fences what a pod can reach.
  This slice creates **no Role and no RoleBinding**: with it alone no identity can create a pod in the namespace.
* **The default-deny policy** selects every pod of the namespace. Ingress is denied entirely (there is no `ingress`
  block, so not even another worker can connect). Egress allows exactly (a) cluster DNS (the kube-dns pods, port 53,
  UDP and TCP), (b) the relay (the manager pod label in the `factory` namespace, on the relay port 8090) and (c) TCP 443
  to `0.0.0.0/0` **except** `169.254.0.0/16`, `168.63.129.16/32`, the pod range, the service range and the node subnet
  (`aks_pod_cidr`, `aks_service_cidr`, `node_subnet_prefix`). The node network security group cannot tell a pod from
  the kubelet (it allows the metadata service and the Azure host service for the whole node subnet), so this policy,
  enforced by Cilium, is the control. FQDN filtering needs a paid add-on and is not used: 443 to the rest of the
  Internet stays open and the sandbox domain list stays the only domain filter, as today. Whether the API server's
  public address is also excluded is **not relied on**: the worker's network canary and the live negative control
  assert that an unauthenticated request to it is refused. **If the engine does not enforce the policy, the canary
  fails the session.** Each egress rule has exactly one `to` entry, so the namespace selector and the pod selector of the DNS
  rule and of the relay rule are ANDed (two `to` entries would be ORed: every pod of the namespace, or every pod with the
  label in any namespace); a design test counts the `to` blocks and pins which selectors sit in each.
* **Log exclusion.** With the variable on, `exclude_namespaces = ["kube-system","gatekeeper-system","factory-workers"]`
  stands under both `[log_collection_settings.stdout]` and `[log_collection_settings.stderr]`. An explicit list
  **replaces** the agent's documented default (`["kube-system","gatekeeper-system"]`), so naming only the worker
  namespace would newly collect the output of both; the list is spelled out in full. The namespace depends on the
  ConfigMap, so the exclusion exists before any worker pod, and a ConfigMap change restarts the agent (check that it has restarted
  with the new settings before the first session). The session transcript goes to the
  relay, not to the pod log; this exclusion is the second layer for a stray line.
* **The relay rule.** The prepared `factory_network_policy` selects the manager pod and admits only what it lists.
  With `pod_backend_enabled` it is given a third, **last** rule: the relay port from the pods labelled
  `factory.internal/role=worker` in the worker namespace. Without it the relay would be cut off the moment that policy
  is turned on; with the variable off the rule is absent. The policy itself stays off (`factory_network_policy_enabled`).
* **Guard.** The three Kubernetes addresses are listed under `POOL_OBJECTS` in `tools/infra_cluster_guard.py`: turning
  `pod_backend_enabled` off in `INFRA_TFVARS` would DELETE the namespace and every worker pod in it (a disruption that
  ignores `do-not-disrupt`) and the policy that fences them, so an automatic run after an image build (your own pipeline's) refuses it and a
  manual run applies it through the drain gate. A later object of the namespace is listed in the pull request that adds it: `tests/test_infra_cluster_guard.py` reads the `.tf` files as text and fails for a Kubernetes resource that names the worker namespace and is not listed, whether it names it by `local.worker_ns_count`, by `var.pod_backend_enabled`, by a reference to `kubernetes_namespace_v1.worker`, `kubernetes_service_account_v1.worker` or `kubernetes_network_policy_v1.worker_default_deny`, by `local.worker_contract.names.worker_namespace`, by the literal `factory-workers`, through a local that holds one of these (in a `namespace` argument or a namespace label selector), or by naming another resource that does. One resource is exempted by name, `kubernetes_manifest.factory_network_policy`: it names the namespace only as the peer of its relay rule, lives in the manager namespace and is not gated by the variable, so turning the variable off updates it in place. The rule reads text, so the following is **not detected**: a namespace that is computed from other strings (`format`, an interpolation), read from a `var.` or a data source, or decoded from a file (`yamldecode`), in a resource that `pod_backend_enabled` does not gate (the same resource with the gate is detected).
* **Order (binding on slice 16).** The default-deny policy must exist before any Role or RoleBinding of the namespace does, so that
  no moment exists in which the launcher may create a pod without the fence. The Role and the RoleBinding of slice 16 (and any Role,
  RoleBinding, ClusterRole or ClusterRoleBinding that names the namespace or the worker service account) therefore MUST
  `depends_on` `kubernetes_network_policy_v1.worker_default_deny`, next to the namespace and the admission policy bindings of slice
  15b: if neither resource lists the other and they share only the namespace, Terraform is free to create them concurrently or in
  either order. `infra/test/test_worker_ns_design.py` scans every `infra/*.tf` and fails for such a resource without the dependency;
  none existed in this slice, so the scan passed with nothing to check, and its own tests run it on synthetic Role and RoleBinding
  resources to show that it fails without the dependency (slice 16 added the two real ones, which it now checks: "Terraform C"). A comment above the policy in `worker_ns.tf` says the same (a comment
  changes no plan line).
* **The wording.** `infra/k8s.tf` no longer says that a NetworkPolicy "would be decoration": enforcement needs the Cilium
  data plane, and the comments, the variable description and the documents that carried the old sentence were rewritten
  together with the tests that pinned it (`tests/test_network_policy_wording.py`).
* **Order.** Step (4) of "Switching the data plane" (`infra/README.md`): `pod_backend_enabled = true` after Cilium, node
  auto-provisioning and the NodePool objects, by a manual `stage=all` run behind the drain gate. The enabling apply
  creates the three objects and updates the agent ConfigMap in place.

## Terraform B2: the admission policies (slice 15b, issues #144 and #145)

Default off, with the same gate as slice 15a: `pod_backend_enabled` (count 0 by default, the same three preconditions), so a default
merge has no plan line. The namespace is Pod Security `privileged`, so the label fences nothing: **these policies are a hard
prerequisite of the pod backend, not defence in depth**. Nothing can create a pod in the namespace yet (the Role is slice 16).

* **The objects.** Four JSON files, one object each, in `infra/policy/`: `pod-policy.json`, `pod-binding.json`,
  `secret-policy.json`, `secret-binding.json` (ValidatingAdmissionPolicy and ValidatingAdmissionPolicyBinding,
  `admissionregistration.k8s.io/v1`). `infra/worker_policy.tf` reads them with `jsondecode(file(...))` into four
  `kubernetes_manifest` resources, and creates the parameter of the pod policy, the ConfigMap `worker-policy-params` in the worker
  namespace with `image = local.image`: the very local the launcher's environment takes its image from (slice 17), so the two
  cannot drift. The typed `kubernetes_validating_admission_policy_v1` exists in the provider, but a typed binding was not found and
  the same files are what the admission matrix applies to a real API server, so one manifest path for both kinds keeps one lifecycle and one
  source (the provider path for these kinds is UNVERIFIED).
* **What they bind.** The requests of one identity (`request.userInfo.username` is the service account `factory` of the manager
  namespace), in `factory-workers` only (the binding's namespace selector), `failurePolicy: Fail`, the actions `Deny` and `Audit`,
  a missing parameter is a `Deny`. They match `CREATE` of pods and of secrets: the launcher's Role has no `update` or `patch`. Every
  validation is one rule with its stable id (`podspec.POLICY_RULES`, the same 38 pod and 6 Secret ids and the same order as the
  mirror) as `[rule:<id>]` at the start of its message; the Kubernetes API reports the first failing validation only.
* **Where the CEL differs from the mirror, on purpose.** (1) A typed object has no key list in CEL, so a closed key set is one
  denial per field of the API version the policy was written for (`spec-fields`, `container-fields`, `security-context-keys`): a
  field added to the pod API later is not named and therefore not refused until a later change adds it (UNVERIFIED). (2) The API
  server defaults and mutates the pod before validation: a `schedulerName`, `dnsPolicy`, `priority`, `preemptionPolicy`,
  `serviceAccount` alias, the empty pod `securityContext`, the two `NoExecute` tolerations and the `memory-pressure` one, the termination message fields and the
  `apiVersion` of a `fieldRef` are accepted only with their default value; the `tolerations` rule ignores exactly the two
  defaulted `NoExecute` ones and the defaulted `memory-pressure` `NoSchedule` one (the API server adds it to every pod that is not
  BestEffort; the scratch-deployment run of issue #144 on AKS found that a policy without it refuses every worker pod). (3) Absence cannot be tested where the server fills the default in: `terminationGracePeriodSeconds`, a request
  that equals its limit, a Secret `type`, the `status`, an empty list: a handful of mutations of the golden files (six in the first live run) therefore reach the
  policy as the golden object ("no effect", below). (4) An `emptyDir` `sizeLimit` is whole `Mi` or `Gi` (stricter than the mirror:
  the server writes `1e3` as `1k`). (5) A Secret has no pod to compare with, so `secret-name` checks the form (`<pod name>-s`) and
  `secret-owner` that the single owner reference is that Pod. (6) Init and ephemeral containers are covered by the per-container
  rules and refused by `containers`. (7) Only `CREATE` of pods and of Secrets is judged: no other kind, no other verb.
* **The matrix, three ways.** `tests/test_policy_cel.py` runs the CEL through a small model (`tests/celmodel.py`: missing field
  access is an error, `has()`, error absorption of `&&` and `||`, the macros, the quantity library) after emulating the server's
  defaulting, and shows that removing or emptying any one rule is noticed; it is a model and not evidence. `tools/policy_matrix.py`
  replays the same files against an API server with `kubectl create --dry-run=server` as the launcher identity: every golden
  object must be admitted; a mutation must be denied by the policy with a rule id (its own, one the mirror also breaks for that
  object, or one of `SERVER_VIEW`: what the server shows the policy after decoding, e.g. `stringData` merged into `data`), or be
  turned back into the golden object by the server ("no effect": the returned objects are compared), or be rejected before the
  policy ("not exercised"; more than 30 percent of them fails the run). An admitted mutation, a denied golden object, a denial by
  another policy or without a rule id, and a timeout fail it. It uses argv lists only (no shell), bounds every call with a timeout
  and prints no manifest, response or secret. Its `status` command waits for the API server to type-check both policies and fails on
  any expression warning (the type check against the pod schema is asynchronous); `fixtures` prints the namespace, service account,
  parameter and Role a bare cluster needs. **Where it runs:** by hand, against a kind cluster (with the two feature gates below) or a scratch deployment; a pull
  request that adds or changes the policies should attach the recorded output.
* **The admission matrix on kind.** `tools/policy_matrix.py` was validated on a kind cluster (v1.36.1, node image digest pinned, the two feature gates a worker pod
  needs: `UserNamespacesSupport`, `ProcMountType`): apply `tools/policy_matrix.py fixtures` and the four files of
  `infra/policy/`, wait for the type check (`status`) and run the matrix (`run`). The evaluator is the API server of that node image, not AKS.
  **First live run (2026-11-09, kind v1.36.1, reproduced locally):** three things the offline tests could not show. (1) `status` ran
  as the impersonated launcher identity, which may only create pods and secrets, so it could never read a policy and always reported
  "not observed"; it now reads as the caller (the cluster's administrator) and only `run` impersonates. (2) The probe pod that
  waits for the policy to start denying used `hostNetwork`, `hostPID` and `hostIPC`, which the API server rejects itself with
  `hostUsers: false` ("Forbidden: when hostUsers is false") before any policy runs, so the wait never ended; the probe now uses
  `restartPolicy: Always`, `automountServiceAccountToken: true`, `shareProcessNamespace: true` and a foreign image. (3) The API
  server's type checker reports three warnings on the pod policy, all `undefined field` for a Quantity or ResourceList field
  (`emptyDir.sizeLimit`, `resources.requests` and `resources.limits`, `spec.overhead`; the `resources` rule alone has 36 of them); the `run` replay proves those rules at run time, so exactly these
  three messages are printed as `tolerated` and any other warning still fails. The first live matrix: 12 golden objects
  admitted, 172 mutations denied naming their rule, 6 without effect, 37 rejected before the policy (not exercised), 0 problems.
* **The plan identity.** The custom role `Factory Plan Cluster Reader` gains the read data actions for `roles` and `rolebindings`
  (seven in all). `validatingadmissionpolicies` and `validatingadmissionpolicybindings` have **no data action in the public Azure
  permissions reference** (checked 2026-11-09: it lists the webhook configurations and the discovery paths of the group), so they
  are not in the role: a definition that names an action the provider does not define is refused. Whether the authorizer maps them
  to `customresources/read` is UNVERIFIED. A human updates the role definition before any enabling flip, with a
  plan run as the plan identity on a scratch deployment as the check: that plan passes before any production enabling flip, any apply of the admission
  policies in production and any update of the production role, and a `Forbidden` is a stop and a report, never a widening of the role by
  guessing a name. A custom role name is tenant-wide: `az role definition update` on `Factory Plan Cluster Reader` changes the **production**
  role, so the plan identity of a scratch deployment gets a role of another name (`Factory Plan Cluster Reader <prefix>`) and the production role is
  updated in exactly one step, after that plan has passed, by the owner.
* **Guard.** The parameter and the four manifests are `POOL_OBJECTS` in `tools/infra_cluster_guard.py`: turning the variable off
  would remove the fence while pods may still run.
* **Order (binding on slice 16).** The RoleBinding that lets the launcher create pods MUST `depends_on` both policy bindings
  (`kubernetes_manifest.worker_policy_pods_binding` and `worker_policy_secrets_binding`) as well as the default-deny policy and the
  namespace; `infra/test/test_policy_json.py` fails for a Role or RoleBinding of the worker namespace that lacks them.
* **Amended frozen tests** (old, new, reason): `infra/test/test_plan_role.py`, five read data actions, seven (the Role and
  RoleBinding reads); `tests/test_infra_cluster_guard.py`, the worker-namespace address set gains the parameter ConfigMap and a new
  test lists the four manifests.

## Terraform C: the launcher's Role and the manager pod (slices 16 and 17, issues #144, #145 and #147)

Everything here is **default off, delivered, not wired in**: it is created only by `pod_backend_enabled` (the gate of slices 15a and
15b, count 0 by default, the same three preconditions), plus one separate flip, `manager_pool_taint_enabled`, so a default merge has
no plan line for any of it and the Deployment of a default plan is the object it was. No apply is run by an assistant; the checks that
ran are named in the pull request, and the live ones are for a person with Azure access. Even with
the variable on, **nothing launches a pod**: the manager refuses `execution = "pod"` (slice 12a) because no pod backend is built into
it (slice 12b).

| Piece | Where | Default |
|---|---|---|
| Role `factory-launcher` and its RoleBinding (`kubernetes_role_v1.worker_launcher`, `kubernetes_role_binding_v1.worker_launcher`) | `infra/worker_rbac.tf` | not created |
| the launcher and relay containers, the init container `volume-perms`, the two `emptyDir`s and the projected token, the environment and mounts of the main container, the skip annotation and the toleration | the `pod_backend_*` locals of `infra/pod_backend.tf`, appended to the Deployment in `infra/k8s.tf` | empty lists and maps: the Deployment is unchanged |
| Service `factory-relay-svc` (`kubernetes_service_v1.factory_relay`) | `infra/pod_backend.tf` | not created |
| `manager_pool_taint_enabled` (`node_taints` of the pool `factory`) | `infra/variables.tf`, `infra/aks.tf` | `false`: the argument is omitted (`null`) |

### Slice 16: the Role and the RoleBinding

* **The Role** (namespace `factory-workers`): `pods` create, get, list, delete and `secrets` create, delete, and nothing else: no
  wildcard, no `update` or `patch` (so the two admission policies, which judge `CREATE`, see every way a pod or a Secret is written), no
  `watch`, no `deletecollection`, no subresource (`exec`, `log`, `attach`, `portforward`, `ephemeralcontainers`), no `get`, `list` or
  `watch` of a Secret (the launcher writes the Anthropic key and must never be able to read one back). `infra/test/test_pod_backend_design.py`
  reads `src/factory/kube.py` for its `_call(<method>, self._collection|_item(<resource>)` lines, maps them to verbs and fails when the Role grants more or less
  than the client uses. There is no ClusterRole and no ClusterRoleBinding of the pod backend.
* **The RoleBinding** names one subject: the service account `factory` of the namespace `factory`, the identity the manager pod runs as
  and the one the admission policies are bound to by name (`request.userInfo.username`); a test compares the two. The worker
  service account `factory-worker` is bound to nothing.
* **Order, binding and tested.** Both resources `depends_on` the default-deny NetworkPolicy
  (`kubernetes_network_policy_v1.worker_default_deny`) **and** both admission policy bindings
  (`kubernetes_manifest.worker_policy_pods_binding`, `worker_policy_secrets_binding`) next to the two cluster role assignments, the Role as well as
  the binding, so that not even an unbound Role exists before the fence. The scans of slices 15a and 15b (`test_worker_ns_design.py`,
  `test_policy_json.py`) now run on real resources and pass; a new test pins the three entries in both. The Deployment does **not**
  depend on the binding: a launcher that starts before the binding exists only gets `403` answers, which fail closed.
* **The plan identity.** The custom role already carries `roles/read` and `rolebindings/read` (slice 15b): this slice needs **no new
  data action**, so `infra/roles/factory-plan-cluster-reader.json` is unchanged and the test that pins its seven actions still holds. That
  the plan identity can read these two kinds in a plan is UNVERIFIED (a plan on a scratch deployment as that identity decides).
* **Guard.** The Role, the binding and the relay Service are listed under `POOL_OBJECTS` in `tools/infra_cluster_guard.py`
  (the completeness rule of `tests/test_infra_cluster_guard.py` failed until they were: it detects them by `local.worker_ns_count` and by the
  namespace). Turning the variable off ends the launcher's right to create a pod at once, so the automatic run refuses it and a manual run
  applies it through the drain gate. The Deployment is not listed: an image promotion and the flip update it in place.

### Slice 17: the manager pod

What the pod becomes with the backend on (every name, uid, path and port is read from the contract local, none is retyped):

| Container | User | Secrets | Mounts |
|---|---|---|---|
| `factory` (main, unchanged identity) | the image's: starts as root, drops to 10001 | both Key Vault secrets, as before; **no Kubernetes token** | `data`, `repos` as before; the socket directory **read-only**; the spool |
| `launcher` | 10003, group 10001 | the Anthropic key (one `secretKeyRef` to the synced Secret); the only Kubernetes token | the socket directory; the projected token and the CA at `/var/run/secrets/kubernetes.io/serviceaccount`, read-only |
| `relay` | 10004, group 10001 | none | the spool |
| `volume-perms` (init) | root of the user namespace, fixed command | none | the socket directory and the spool |

* **Both sidecars** drop every capability, forbid privilege escalation, have a read-only root file system and the runtime-default seccomp
  profile, and carry **none** of the three relaxations (`procMount`, AppArmor; `hostUsers: false` is the pod's). They run the image of
  the Deployment (`local.image`): the same local that is the parameter of the admission policy and the launcher's
  `FACTORY_LAUNCHER_IMAGE`, so the three cannot drift (an automatic image promotion changes all three in one apply; the order of the
  parameter and the launcher's environment is the window already noted under slice 15b). The commands are the image's interpreter and
  the module (`python -m factory.launcher`, `python -m factory.relay`). Requests equal limits, so the pod stays Guaranteed: 100m and
  128Mi for the launcher, 250m and 384Mi for the relay (PROVISIONAL, nothing measured; the main container is still capped at 75 % of the
  node). A sidecar has no probe: a crashed one is restarted by the kubelet.
* **The Kubernetes token.** The pod keeps `automountServiceAccountToken = false`. The launcher's credential is a projected volume
  (`serviceAccountToken` and the `kube-root-ca.crt` ConfigMap's `ca.crt`, the file names `src/factory/kube.py` reads, a test compares them)
  mounted at the standard path in **that container only**. The main container, which runs every supervisor and every in-process worker,
  and the relay, which refuses to start when a token file exists, hold none. A test fails when any container but the launcher mounts the
  volume or any other file of `infra/` declares a projected token.
* **The shared directories.** `/run/factory` (the socket, `emptyDir` `1Mi`) and `/spool` (`emptyDir` `16Gi`, the relay refuses uploads at
  12 GiB below it) are shared by the containers. The init container sets them once because a non-root sidecar with no capability cannot:
  `chown 10003:10001 /run/factory && chmod 0750 /run/factory && chown 10004:10001 /spool && chmod 2770 /spool`, with exactly `CHOWN`, `FOWNER` and
  `FSETID` (the setgid bit survives a `chmod` by an owner who is not in the group only with `FSETID`), no secret, no environment and a
  read-only root file system. These are the owner and modes `launcher.py` and `relay.py` check at start and refuse without; the
  main container's uid 10001 is in group 10001 and may enter the directory and connect, the worker uid 10002 may not.
* **The workload identity webhook** injects `AZURE_*` variables and a token mount into every container (and init container) of a pod with the label
  `azure.workload.identity/use`. The relay refuses to start with an `AZURE_*` name and the launcher must hold no cloud identity, so the pod
  template carries `azure.workload.identity/skip-containers: volume-perms;launcher;relay`; the main container keeps its identity.
* **The environment.** The launcher's names are the contract's `ENV_LAUNCHER`: the image, the namespace, the maximum of live pods
  (`local.worker_pool.max_sessions`, the one local every number of the pool derives from, which is also the main container's
  `FACTORY_MAX_POD_SESSIONS`; the ceiling test of slice 14 now also reads the contract spelling of the two names), the per-repository cap, the clamp and the allowed uid (all from
  the contract), the relay URL `http://factory-relay-svc.factory.svc.cluster.local:8090`, **only the default resource class** (`standard`),
  the API server's public FQDN (`azurerm_kubernetes_cluster.this.fqdn`, which the worker's canary asserts refuses an unauthenticated request), the manager address (the Service name of the app,
  `factory.factory.svc.cluster.local`: the pod address is not known to Terraform and changes at every restart, and the canary asserts that
  the worker cannot connect to it) and the socket path. The main container gets `FACTORY_POD_BACKEND=on`, `FACTORY_MAX_POD_SESSIONS`,
  the socket path and the spool path, **appended after** every existing entry (the provider fills a planned list by position). Every name appears once in the Terraform
  tree, as a reference into the contract, never as a string; the Python side is read with `ast`: the launcher and relay groups have
  one reader each, the manager group the three that exist (the switch is read by `podexecution.py` and written for the children by `manager.py`, the socket path
  by `launcher_client.py`; `FACTORY_MAX_POD_SESSIONS` and `FACTORY_POD_SPOOL_DIR` have none until slices 12b and 18), and a new reader fails the test until its table is changed.
  `AZMON_COLLECT_ENV=False` is set on both sidecars as the second layer next to the agent ConfigMap.
* **The relay Service** `factory-relay-svc` (ClusterIP, port 8090, target port `relay`) selects the manager pod; the worker's default-deny
  policy already allows TCP 8090 to the manager pod's labels. It is **not** named `factory-relay` on purpose: Kubernetes would inject
  `FACTORY_RELAY_PORT=tcp://...` for that name, which the relay refuses (slice 8). Two more layers stand behind the name: the
  Deployment sets `enableServiceLinks = false` and the relay's environment sets `FACTORY_RELAY_PORT` itself (an explicit entry wins).
* **The toleration and the optional taint.** With the backend on the pod spec gets one toleration, `workload=manager:NoSchedule` (the
  contract's manager taint), merged in only then. The taint itself is a **separate flip**, `manager_pool_taint_enabled` (default `false`, needs
  `pod_backend_enabled`, checked in the variable), which sets `node_taints` of the pool `factory`; the toleration therefore always exists before the
  taint. A change of `node_taints` may **replace** the pool (the provider documents it as forcing a new resource; UNVERIFIED for the pinned
  version), which deletes the manager's node: the pool is a CLUSTER object of the guard, so it is refused on the automatic path and on
  `stage=all` and needs `cluster_snapshots_taken=true` on `stage=cluster`. Run it on a scratch deployment first; whether the Secrets Store CSI driver and
  the Azure Disk CSI driver tolerate the taint on this cluster is UNVERIFIED, and a manager that cannot mount its Key Vault volume is down.
  The worker pool's own taint (`workload=worker:NoSchedule`) is slice 14's and is unchanged; worker pods select `pool=worker` and tolerate only it.
* **Why locals and not a conditional.** A conditional `cond ? [a, b] : []` needs one type for both branches, which a list of unlike
  objects does not have, so each value is built unconditionally (`pod_backend_*_all`) and exposed through `[for x in all : x if
  local.pod_backend_on]`, an empty list or map while off; the Deployment appends them with `concat` and `merge` (and never spells the
  gate, so the guard's completeness rule does not list it as an object of the worker namespace). The expressions were evaluated off and on with
  `terraform console` in a provider-free scratch module (stubbed inputs, not a plan): off, every one is empty and `manager_pool_node_taints` is
  `null`; on, the shapes above. That evaluation proves the expressions, **not** that a plan shows no line for the Deployment.
* **Amended frozen tests** (old, new, reason): `infra/test/test_workload_design.py`, the literal `containers = [` and `initContainers = [` became
  `... = concat([` and the environment pattern ends with `local.pod_backend_main_env` (the lists are appended to); `infra/test/test_policy_json.py`,
  `role_bodies` no longer fails for a resource that ends a file; `infra/test/test_worker_pool_design.py`, the two ceiling names are also found
  as references into the contract; `tests/test_infra_cluster_guard.py`, the worker-namespace address set gains the Role and the binding and two new tests list the relay
  Service and the pool taint.

**Checks.** L: `python3 -I -m unittest discover -s infra/test`, `python3 -I infra/test/check_refs.py`, `terraform fmt -check -recursive infra`, and
`PYTHONPATH=src python3 -m unittest tests.test_infra_pod_backend tests.test_infra_cluster_guard`. C: compile, the unit tests and the infra offline tests (`.github/workflows/ci.yml`); `terraform fmt`, `validate` and `tflint` run locally. P: a
`terraform plan` with the read-only identity (your pipeline's plan step) must show **no line at all** with the defaults: no Role, binding, Service or Deployment change and no change of the pool `factory`. V (live only): a person with Azure access.

**UNVERIFIED (slices 16 and 17: none of it can be seen without a cluster).**
* That a default plan shows no line for the Deployment, the pool `factory` or anything else (a text test and an expression evaluation pin the gating; only a `terraform plan` with the read-only identity decides),
  and in particular that a `merge` and `concat` that add nothing leave the planned `manifest` equal to the state the provider holds.
* That the provider accepts the typed Role and RoleBinding with the empty core `api_groups` entry, and that the plan identity can read them.
* That the kubelet starts the pod as written: the projected token and the CA readable by uid 10003 at the standard path (the token file mode, no `fsGroup`); the
  `emptyDir` ownership and modes for containers of different uids under `hostUsers: false`; that the init container with `CHOWN`, `FOWNER` and `FSETID` only can set the owners and the
  setgid bit; that a read-only mount of the socket directory still lets the main container connect; what `SO_PEERCRED` reports across the containers (slice 11).
* That the webhook honours `skip-containers` for the init container and the two sidecars (if it injects `AZURE_*` the relay refuses to start).
* That a worker pod reaches the relay through the Service and cannot reach the manager address, under Cilium (the policy selects the manager pod; the Service is translated before it is applied).
* That `fqdn` is the public FQDN the canary needs and that it is known at plan time after a cluster change.
* The sidecar sizes, and that the pod still fits its node with them.
* That the Secrets Store CSI and Azure Disk CSI drivers tolerate `workload=manager:NoSchedule`, and whether `node_taints` replaces the pool.

## Where each check runs

| Name | Where | What it covers for this slice |
|---|---|---|
| L | a local checkout | `PYTHONPATH=src python3 -m unittest tests.test_podcontract tests.test_podspec`, `python3 -I -m unittest discover -s infra/test`, `python3 -I infra/test/check_refs.py`, `compileall` |
| C | CI on the pull request (`.github/workflows/ci.yml`), and locally for the Terraform tools | compile, the unit tests on Python 3.11 to 3.13 and the infra offline checks run in CI; `terraform fmt`, `init -backend=false`, `validate` and `tflint` (clean, with the annotation) run locally |
| P | a `terraform plan` with the read-only plan identity | a real plan with the read-only identity; slice 0 must show no line for the cluster, any node pool, the manager Deployment or the data volume, and no line at all (a local only); slice 6 touches no Terraform |
| V | live, a person with Azure access | none for slices 0 and 6; the template is applied for real only on a scratch deployment in a later slice |

## The relay and the session record (slice 8)

`python -m factory.relay` (`src/factory/relay.py`) is the relay of the architecture above, as its own program.
A network-reachable parser fed by untrusted pods must not live in the manager container, so the relay has no
secret, no Kubernetes token, no cloud token and makes no outbound call. `src/factory/sessionrecord.py` is the one
module that defines what the relay and the supervisor (`PodBackend`, a later slice) share in the spool: the
layout, the record and state schemas, the marker names and the atomic file functions. Neither module is imported
by anything else yet, so nothing changes for any repository. Wording: **contract-tested with an in-process client**
(and a few tests over a real socket on 127.0.0.1); nothing was run in a container or against a cluster.

**The spool** (`/spool`, the root of an `emptyDir`; `FACTORY_RELAY_SPOOL_DIR`):

| Path under `<spool>/<session id>/` | Written by | Content |
|---|---|---|
| `record.json` | supervisor | the session record, before the launch; rewritten once to bind the pod name and uid (atomic) |
| `in/bundle` | supervisor | the tree for the pod (slice 9a stages it) |
| `state.json` | relay only | the token state, the number of fetch attempts, the preflight code, the reject code, the time it ended |
| `last_heartbeat` | relay | an integer, epoch seconds; a heartbeat or a stream chunk writes it |
| `out/transcript`, `out/stderr` | relay | the appended chunks |
| `out/delta`, `out/report.json` | relay | the result (the delta is data for the supervisor to validate, slice 9b) |
| `revoke`, `released` | supervisor | markers, empty files |

The session id is the contract's 32 lowercase hex digits and is validated before it is a path component. Every
file is opened relative to a directory descriptor with `O_NOFOLLOW` and must be a regular file (a link, a fifo or a
file in the place of a directory is refused, never followed or waited on). The set of names under `out/` is fixed;
nothing from a request, and no path inside an upload, is ever used as a path. Files are mode 660 and directories
2770 whatever the umask; writes are a temporary name and one rename.

**The session record** (`sessionrecord.validate`; closed fields, bounded sizes, a boolean is not a number): `version`
(1), `session`, `token_sha256`, `pod_name` and `pod_uid` (both null until bound; the name must be the contract pod
name of this session), `deadline` (epoch seconds, the expiry of the token), `caps` (the six caps below; a record may
lower a cap, never raise it above the contract), `repo` (the 12-hex repository label), `write_scope` (`none` or
`cwd`, the values of `phases.py`) and `manager` (the manager instance id). The marker names `revoke` and `released`
are part of the schema, not a field. The supervisor-side calls are `build`, `write`, `bind`, `read`, `read_state`,
`read_heartbeat`, `mark` and `is_marked`; the relay uses the same functions on a directory descriptor.

**What a pod may call.** Every request carries `Authorization: Bearer <token>`, `X-Factory-Pod-Name` (the contract
pod name, which carries the session id) and `X-Factory-Pod-Uid`. One request per connection, `Content-Length`
required for a body. The route table is closed: no endpoint lists sessions or reads another session's data.

| Call | Allowed in state | Result |
|---|---|---|
| `GET /v1/bundle` | `preflighted` | the bundle, served in 64 KiB slices; at most three attempts |
| `POST /v1/preflight` | `bound` (a repeated good one in `preflighted`) | body `{"result": "ok"}` or `{"result": "failed", "code": "<[a-z0-9_]{1,32}>"}`; a failure ends the session (`reject = preflight_failed`) and the bundle is never served |
| `POST /v1/stream/transcript`, `POST /v1/stream/stderr` | `fetched`, `streaming` | appends one chunk, all of it or none of it; at most 8 MiB per chunk |
| `POST /v1/heartbeat` | `bound` to `streaming` | answers `{"state": ...}` and writes `last_heartbeat` |
| `POST /v1/result` | `fetched`, `streaming` | body: a 4-byte big-endian length, that many bytes of report (a JSON object of at most 64 KiB), then the delta bytes; accepted once |

**Answers** (always a fixed document `{"error": "<code>"}` or `{"state": "<state>"}`; no input is echoed):
200 done; 400 malformed (a preflight or result body that is not the fixed shape); 401 no valid credentials or a wrong
token; 403 the pod uid is not the bound one; 404 unknown path; 405 known path, wrong method; 409 the call is not
allowed in this state, or the same transfer is already running (`order`, `busy`); 410 gone; 411 a body without a
length; 413 an over-cap body (`transcript_cap`, `stderr_cap`, `delta_cap`, `report_cap`, `bundle_cap`, or
`chunk_too_large` and `body_too_large` for one request); 425 the session is known but the supervisor has not bound the pod yet (the
wrapper retries with a bounded backoff, slice 10a); 500 internal; 503 `busy` (the thread bound) or `unavailable`
(the spool root is wrong); 507 `spool_full`.

**Order of checks**, so that nothing is an oracle: the credentials are parsed; a session with no record answers 410
whatever the token is (this is what ends the pods of a manager that was replaced: the new relay's spool is empty);
the token is compared in constant time as a SHA-256 hash; then a finished, expired, revoked or released session
answers 410, an unbound one 425, another pod uid 403, and a call that does not fit the state 409; only then are the
length, the cap and the body looked at. A request whose body is not needed is not read.

**The state machine** (one way, stored in `state.json`, so it survives a restart of the relay):
`issued` (a record without a pod), `bound` (the supervisor wrote the pod name and uid), `preflighted`, `fetched`,
`streaming`, `finished`, `expired`, `revoked`; `released` is the supervisor's marker. A terminal state is sticky: a
session that finished is not turned into `expired` or `revoked` later. Expiry is the deadline of the record. A
session cut off by a cap or a failed preflight is `finished` with a `reject` code; the supervisor reads the code
before it trusts anything under `out/`.

* **Fetch.** A fetch counts as used only if the last byte was written without error: the state becomes `fetched` just
  before the last slice is written and goes back to `preflighted` if that write fails (recording it after the write
  would let the pod's next call, which can arrive the moment it has the last byte, find the old state). A download that
  drops may be retried; every attempt is counted before the first byte, the fourth is refused and rejects the session
  (`bundle_attempts`). A second complete fetch answers 410. Two downloads at the same time: the second answers 409.
* **Result.** Accepted once. It is received into temporary files and committed under a re-check of the state: a
  session revoked or expired while the upload ran gets no result and no file. The state is written last.
* **Revoke and release.** The supervisor writes `revoke` for every termination it requests (slice 12b); the relay
  looks for it on every request. After `collect` the supervisor writes `released`.

**Retention.** Output is never deleted before the supervisor has had its chance to collect it, also not at expiry. The
relay removes a session directory only when `released` exists, or when the retention (`SPOOL_RETENTION_SECONDS`, 3600
s) has passed since the session ended (finished: when it finished; expired: the deadline; revoked: the time of the
marker). A sweep every 60 s does this and also stores a newly seen `expired` or `revoked`. Leftovers with no usable
record wait the retention out by the age of their directory. Only directories whose name is a session id are touched;
links and other names are left alone. At start the relay removes the half-written temporary files of a stopped relay.

**Caps.** Every number is the contract's (`podcontract.py`), reused and never retyped (a test fails on a retyped
number); `sessionrecord.CAP_LIMITS` carries them per session.

| Cap | Enforced where |
|---|---|
| bundle (1 GiB), transcript (512 MiB), stderr (16 MiB), delta (256 MiB), report (64 KiB), chunk (8 MiB) | the relay: an over-cap upload is cut off before its body is read when the length shows it, and the session is `finished` with the reject code; nothing is truncated silently |
| delta file count (20,000) and per-file size (64 MiB) | carried in the record and enforced by the delta validation of slice 9b, because only the code that parses the wire format can count files; the relay bounds the total size, which bounds both |
| per-request idle timeout (60 s) and total time (120 s for a stream chunk, heartbeat or preflight, 900 s for a bundle or result transfer) | the relay: a read or write waits at most the smaller of the idle timeout and the time left; the total time starts when the connection is accepted |
| header time (10 s) | the relay: a connection must deliver a complete header section within 10 s of being accepted (a drip of bytes does not extend it); only then does the total time of its route apply. A body is read only for an authenticated caller, so a connection that cannot authenticate never holds a thread longer than this |
| threads (32), and at most 4 connections per peer address | the relay: a request beyond either bound is answered 503 at once, without being read. The per-peer bound is relay-local (not in the contract): without it one hostile pod could hold all 32 threads with silent sockets and starve every other session's heartbeat |
| spool refusal threshold (12 GiB) | the relay: an upload that would take the used bytes plus the bytes of uploads in flight above it is refused with 507 before it is read. It is at most 75% of the `emptyDir` `sizeLimit` (16Gi) because a tripped `sizeLimit` evicts the manager pod; a test on both numbers is in `tests/test_relay.py` and `infra/test/test_pod_contract.py` |

Memory is bounded by construction: every copy, in either direction, moves at most 64 KiB at a time.

**What the relay refuses to start or serve without.** The spool root must be exactly what the init container of
slice 17 makes it: a real directory owned by the relay uid with the shared group and mode 2770 (a link, another
owner, another group, a world-accessible or setgid-less mode all refuse). The check runs at start and on every
request (503 `unavailable` if the root stops being right). `main` also refuses to start when it runs as root, when
`FACTORY_RELAY_PORT` is not a plain port number, when an environment variable name looks like a credential (`KEY`,
`TOKEN`, `SECRET`, `PASSWORD`, `CREDENTIAL` as a word, or the prefixes `AZURE_`, `ANTHROPIC_`, `GITHUB_`, `GH_`), or
when a Kubernetes token file is mounted. The three names the image sets itself that would match
(`FACTORY_TOKEN_BROKER`, `GH_NO_UPDATE_NOTIFIER`, `GH_PROMPT_DISABLED`: a command line and two switches) are exempt by
exact name, because a pod template cannot remove an image `ENV` name and the relay runs from that image; a test reads
the Dockerfile's runtime `ENV` block and fails if the image gains a name the guard would refuse. The refusal is one
fixed line and exit code 2.

**Logs and bodies.** A log line is a fixed text with at most a number or an exception class name; the relay never
logs a token, a header, a body, a path or agent text, and there is no per-request log. The standard `http.server`
error pages (which echo the request line) and its `Server` and `Date` headers are replaced.

**For the supervisor (slice 12b), the sequence is:** `sessionrecord.build` with `new_token()` and `new_session_id()`
and `write` before the launch, stage `in/bundle`, `bind` with the name and uid the launcher returned, poll `read_state`
and `read_heartbeat`, `mark(..., "revoke")` for every termination it requests, copy `out/` and then `mark(...,
"released")` in a `finally`. The token travels to the pod only through the launcher; the record holds its hash.

**Slice 8, where each check runs.** L: `PYTHONPATH=src python3 -m unittest tests.test_relay tests.test_sessionrecord`
(deterministic: a fake clock, no sleeps, no network beyond 127.0.0.1). C: compile and the unit tests on Python 3.11 to 3.13
(`.github/workflows/ci.yml`). P: not applicable (no Terraform). V (live only): uid 10002 in the manager's main container
cannot list `/spool`; relay throughput on a real repository.

**UNVERIFIED (slice 8).** Relay throughput and the 12 GiB / 16Gi numbers on a real repository; the spool permission
behaviour under `hostUsers: false` with two containers sharing an `emptyDir` whose owner an init container sets; plain
HTTP inside the cluster (TLS is out of scope); that a pod's connection that drops really surfaces as a failed write on
the last slice (the kernel may accept the last bytes into a buffer before the client notices); that the relay sees
each pod's own address (a source NAT in front of it would put every pod under one peer's bound of 4 connections); that the workload
identity webhook leaves the relay container alone (`skip-containers`; if it injects `AZURE_*` the relay refuses to
start); and a naming hazard for slice 17: `FACTORY_RELAY_PORT` has the shape of the variable Kubernetes injects for a
Service called `factory-relay`, so the container must set it explicitly (an explicit entry wins) or the Service must
have another name. If the injected value (`tcp://...`) ever reaches the relay it refuses to start instead of guessing.

## The pod handle in job state (slice 5)

A session that runs as a pod has no process group that the supervisor can probe or signal, and only the
launcher can reach the Kubernetes API. So a job's state gains a `pod` field **beside** `pid` and `pid_host`, never
instead of them (`src/factory/podhandle.py`, `src/factory/state.py`):

```json
"pod": {"name": "factory-w-<32 hex>", "uid": "<pod uid>", "session": "<32 hex>", "launcher": "<word>", "created_at": "<UTC ISO time>"}
```

* **Only ids, words and one timestamp, no secret.** The record is a closed set of five keys. `PodHandle`
  validates every one, and the name must be the contract's pod name of the session (`podcontract.pod_name`), so
  a state file that was edited cannot point a termination at another pod. A record that does not validate is
  `unknown` (below), never `dead`, and is never passed on to the launcher; the refusal texts name the session id or a
  fixed phrase, never the rejected value.
* **`pid` and `pid_host` keep their exact meaning, and a job never has both.** `State.record_pod` refuses while a
  process group is recorded, `State.record_pid` refuses while a pod is recorded, and a second, different pod is
  refused: forgetting a pod that may still run would let a second session start on the same worktree. Each check
  and its write are one critical section under the state mutex (`State._write`, which runs the observers after it),
  so two calls that race cannot both pass the check and leave both fields set (a test forces the second call into the
  gap between a check and its write, in both orders). The rule is enforced for what this code writes; a state file
  that nevertheless has both (a stale `pod` key left by a rollback, a hand edit) is handled **fail closed** by every
  consumer: `enforce_stop` and `send_stop`, `kill_children` and the abort path terminate the pod AND signal the
  process group (a pid is signalled only while it reads as alive, as before), `State.recover`, `purge` and
  `recovery` check each field on its own, and `status` counts the job as live when either one is.
  `State.record_started` is what the runner and the preflight probe hand to `run_agent` as `on_start`: an int goes
  to `record_pid` exactly as before, a handle to `record_pod`. `State.end_session(key)` ends a session:
  the pid is always forgotten, the pod only when it is known to be gone (the paragraph on the end of a session, below); `clear_pod` writes nothing for a
  job that has no pod, so a state file written before this slice is never given a new key by a local run.
* **`pod_status(handle, reader)` is `"alive"`, `"dead"` or `"unknown"`, mirroring `pid_status`.** No pod is `dead`.
  `reader` is injected (the launcher client of slice 11, wired by the pod backend of slice 12b; `podhandle.install`
  makes it the process-wide default, which is how `State.recover`, `status`, `purge` and `recovery` use it without
  a new argument). **No reader, a reader that raises, a corrupt record and any answer other than exactly `"alive"` or
  `"dead"` are `unknown`.**

The two callables are injected. The tests of this slice use fakes that answer NotFound, a pod phase or nothing; the
real pair is the launcher client of slice 11 (`LauncherClient.reader` and `LauncherClient.terminate`, "The client and
the seam of slice 5" below), which `PodHandleSeamTests` runs against these functions, a Running pod included.

* `reader(handle) -> "alive" | "dead" | "unknown"`: `dead` is NotFound, a pod with another uid, or a terminal phase;
  Pending and Running are `alive`, and so is a pod that is terminating (deleted but still in the API, its containers
  running for the termination grace).
* `terminate(handle) -> None`: asks the launcher to delete the pod. NotFound is success, a failure raises, it is
  idempotent and it must be bounded in time (the supervisor's pool loop waits for it). **It returns normally only
  once the launcher has confirmed the deletion or NotFound**: that return is what lets the end of a session forget
  the record (below). A client that returns for a delete that was merely accepted, while the pod is still
  terminating, would make the supervisor forget a pod that may still run; it must wait for the pod to be gone, or
  raise.

**Fail closed.** The hazard is a second session on the same worktree. Only `dead` lets anything go on:

| Reader says | `State.recover` (restart, `resume`, `run`) | `purge` | `recovery` (`resume` of a retained worktree) | `status` |
|---|---|---|---|---|
| `alive` | raises `PodStillLive` ("wait or stop it"), nothing is changed | refuses | refuses | a live worker |
| `unknown` (no reader, launcher down, corrupt record) | raises `PodStillLive` ("cannot be verified"), the job stays `running`, nothing is marked `interrupted` | refuses | refuses | a live worker, and counted in `active_worker_pods_unknown` |
| `dead` | the job becomes `interrupted` and its pod is forgotten (a pod of a job that is not running is just forgotten) | proceeds | proceeds | not a live worker |

A lease says nothing about a pod, which outlives the container that launched it, so the "a foreign host's child died
with its container" fast path that applies to a pid on another host never applies to a pod: `holds_lease` is not
consulted for one. `PodStillLive` is a `RuntimeError` of its own type (with `.status`, `"alive"` or `"unknown"`),
so that the restart pass can treat that refusal as a wait and retry, and any other as a failure (it does, since slice 20: "Recovery retry").
`status --json` gains `active_worker_pods_unknown` (always present, 0 without pods) and `format_status` prints a line
when it is not 0: the pod-aware deploy gate of slice 20 treats "cannot read" as busy (it reads the manager's `worker_pods` census, where an unreadable launcher is `unknown`). The operations janitor already
reads `active_worker_count`, which counts a pod that cannot be read, so it never purges such a run.

**Stop, abort and lease loss.** `enforce_stop` (a single-job stop), `kill_children` (a lost lease) and the abort path
of `Runner.run_pool` (now `Runner.abort_workers`) call `terminate(handle)` for a recorded pod and signal the process
group for a pid, at the same moments as before. A recorded handle means the session has not been released, so the stop
path does not probe first, and a pod that is already gone is not an error. The request is made at the SIGTERM step and
repeated at the SIGKILL step (the kubelet's own kill comes after the pod's termination grace, `TERMINATION_GRACE_SECONDS`).
A terminator that raises, or that is not installed, is reported with a fixed line (`factory: could not terminate pod
session <id>: <exception class>`) and never raises into the cleanup path. In the abort path the process groups are
signalled first and the pods are terminated in a `finally` (a `killpg` that raises does not skip them), within ONE
deadline of `podhandle.ABORT_TIMEOUT` (10 s, the order of the kill hooks'): a hung terminator can neither hold back the
SIGTERM of the workers on this host nor hold the abort for ever, and a pod whose termination timed out stays recorded,
so recovery refuses on it. `kill_children` is the hook the runner registers;
the registry covers the other half. `podhandle.register` and `unregister` keep the handles of the pod sessions this
process started and has not released, and `kill_live_pods` terminates every registered one, each in its own thread so
that a hung call holds up no other. **It returns only when every termination has finished or its deadline has passed**
(`terminate_all(handles, terminate, timeout, clock)`; `timeout` is ONE deadline for the whole wait, measured from the
start of the call with a monotonic clock that the tests inject, so with N hung terminators the wait is `timeout`, not N
times it; a call that did not finish counts as failed and is reported by the returned count): the lease-loss path
(`locks.exit_process_on_lost`) calls `os._exit` right after the hooks return and the terminate threads are daemons, so
a call that returned early would end the process with the pods still running. The kill hooks get
`locks.KILL_HOOK_TIMEOUT` seconds in all (`locks.run_kill_hooks` joins against one deadline of its own, then the process
exits whatever a hook is still doing), so a `timeout` passed through a `functools.partial` is optional there. `cli.py` registers `kill_live_agents`, which signals local process
groups only; slice 12b registers `kill_live_pods` as a second hook through `locks.register_kill_hook`. Tested here with
the lock of `tests/test_locks.py` and fake terminators: losing the lease terminates every registered handle once and
none after `unregister`, and the process does not exit while a termination is still under way.

**The end of a session forgets the pod only when it is known to be gone.** `Runner.invoke`, `Runner.execute` and the
preflight probe end a session with `State.end_session`: the process group is forgotten as it always was, and the pod
record only when `podhandle.confirm_gone` says so, that is when the reader reads the pod as `dead` (the launcher answers
NotFound, another uid or a terminal phase) or the terminator ran and returned without raising. If the terminator
raised, if there is no terminator or no reader, or if the launcher does not answer, the record **stays** (and the
failure is reported with the fixed line above): `State.recover`, `purge` and `recovery` refuse on it until the reader
says `dead`, and `Runner.invoke` starts no further session of that job while it stays (it first asks again, so a launcher
that is back clears it, and otherwise refuses with a fixed text, `... not confirmed gone ...`). So the fail-closed property
holds for a failed termination in a live process, not only for a crash. **What this cannot do by itself:** it takes the
injected callables at their word. A `terminate` that returns for a delete that was only accepted (contract above), and a
`reader` that answers `dead` for a pod that still runs, defeat it, and nothing in this tree can tell. The launcher client
of slice 11 honours both: its `terminate` returns only once the launcher says the pod is no longer in the API (it waits
for the termination grace, with a margin, and raises when the pod is still there), and its `reader` answers `alive` for a
terminating pod. Slice 12b must `install` the reader and the terminator before any pod can be recorded. Until both are installed every recorded pod is permanent by design (nothing
can confirm it gone), so an operator clears it by bringing the launcher up, not by editing state. The slice 20 orphan
sweep stays the backstop for a pod that outlived a failed delete.

**What this slice does not do (on purpose).** (Slice 12b has since done what this paragraph leaves out: `AgentHandle` carries the pod, `_pid_callback` forwards
it, `cli.run` installs the reader and the terminator and registers the second kill hook when the manager switch is on, and `PodBackend` registers and unregisters the handles: see
"The pod backend (slice 12b)".) Nothing records a pod yet: `AgentHandle` has no pod, `_pid_callback` forwards
a pid only, no reader or terminator is installed, and no hook is registered, so every path above is reached only by the
tests. Slice 11 provides the reader and terminator, slice 12b the wiring (the handle, `install`, the second kill hook,
`register` and `unregister` around a session). The old obligation on `launch` ("returns or raises only after the pod was
asked to be deleted") is no longer what the fail-closed property rests on: the supervisor now confirms the deletion itself
at the end of every session, and `launch` deleting the pod in its own `finally` is only an optimisation (the end of the
session then finds the pod dead).

**Rollback.** The previous image ignores `pod`: it sees `pid` as `None`, which `pid_status` reads as `dead`, so its
`State.recover` marks a running job `interrupted`, and the pod then ends itself by silence (slice 19). What this tree
pins: no reader indexes the `pod` key (`tests/test_podhandle.py` scans the sources for a subscript, so a job without
the key can never raise), and a state file written before this slice recovers exactly as before and gains no `pod` key.
The previous image itself is not run.

Checks for this slice: L (`tests.test_podhandle` plus the tests of the modules it touches) and C. P is not applicable
(no Terraform). Live (not run): restart recovery with a real pod and a real launcher outage.

## The bundle and ship-in staging (slice 9a)

`src/factory/bundle.py` is what a pod gets and, in slice 9b, what it sends back, as ONE module that the supervisor side
(here and in 9b) and the pod side (slices 10a and 10b) import, so the two cannot diverge. The supervisor's worktree stays
authoritative: the pod gets a **self-contained copy at the identical path under its data directory** (so the generated
settings, which carry the absolute worktree path, need no change) and returns a file delta. Nothing imports the module
yet. Wording: **contract-tested with real local git repositories**; nothing ran in a pod.

**Wire format (version 1).** A magic (`FCTYBNDL`), a version (`u16`, 1) and then records until the end of the stream:
`<kind u8> <flags u8> <path length u16> <path> <size u64> <payload>`, all big endian.

| Kind | Byte | Payload | Direction |
|---|---|---|---|
| `file` | 1 | the bytes of a regular file | both |
| `symlink` | 2 | the link target as text | both |
| `delete` | 3 | none | delta only |
| `baseline` | 4 | the kind byte of the shipped path and the SHA-256 of a file's bytes or of a symlink's target (33 bytes); the exec bit of the record is the exec bit of the shipped file | bundle only |

The set is **closed**: any other kind byte is a protocol error, so a hard link, a device, a fifo or a directory
cannot even be expressed; directories are implied by paths. The only flag is the exec
bit (bit 0; any other bit is an error, and it means something only on `file` and `baseline`); only the exec bit of a mode is
ever carried, never setuid, setgid or sticky.

A path is `<area>/<relative path>`; the areas are a closed set and say where a record goes:

| Area | Meaning | Extracted at |
|---|---|---|
| `tree` | every regular file and symlink of the worktree, in whatever ignore state | `<worktree>/<path>` |
| `git` | the files of the staged git directory (`file` only) | `<worktree>/.git/<path>` |
| `read` | the `read_paths` files, read-only (`file` only) | their own path under the data directory, mode 0444 or 0555 |
| `meta` | `manifest.json`, the one JSON object (at most 256 KiB, no secret) that describes the session | not extracted; it must be the FIRST record and names the worktree (`worktree`, a path under the data directory) |

A delta is the same format with only the area `tree` and the kinds `file`, `symlink` and `delete` (slice 9b validates and
applies it). **One function checks every path, for the writer and the reader:** no absolute path, no `..`, no empty and no
`.` component, no NUL, no backslash, at most 4096 bytes (`PATH_MAX_BYTES`), at most 100 components, and **no component that
is `.git` in any case or Unicode variant** (the NFKC-casefolded name after removing default-ignorable code points and
trailing dots and spaces, also `git~1`: `.GIT`, `.Git`, `.git.`, `.git `, a zero-width character inside the name, the
fullwidth full stop). The git directory travels under the area `git` precisely so that no record ever spells that name; the
extractor spells it itself. The writer cannot produce what the reader refuses.

**Bounds on decode.** The header and the path (at most 4096 bytes) are read exactly; every `size` is checked against the cap
of its kind and area (64 MiB per file in `tree` and `read`, 256 KiB for the manifest, 4096 for a link target, 33 for a
baseline, 0 for a delete; the git area has no per-file cap, because a pack is one file, only the bundle cap; a test pins both
sides) and against what is left of the byte cap BEFORE anything is read, so a hostile length never allocates: nothing is read in one piece larger than 64 KiB
except the manifest. The caps are the contract's (`podcontract.BUNDLE_MAX_BYTES` 1 GiB, `FILE_MAX_BYTES`,
`PATH_MAX_BYTES`; the delta's `DELTA_MAX_BYTES` and `DELTA_MAX_FILES`) plus two bundle-local numbers, because the contract
has no bundle file count and no depth: `MAX_FILES` 200,000 and `MAX_DEPTH` 100 (both PROVISIONAL). An unknown or disallowed
kind, a flag, an unclean path, a link target with a NUL byte and a stream cut off inside a record are `WireError`; a cap is
`BundleTooLarge` ("bundle too large"; nothing is ever truncated). A `symlink` target is at most 4096 bytes, so the reader reads
and checks it when it decodes the record (`check_target`, reason `target`, both directions), and the writer refuses to write
one: a hostile delta cannot hand the applier of slice 9b a target no operating system can hold. **Every failure of the module
is a `BundleError`** (`WireError`, `BundleTooLarge`, `SpoolFull`, `TreeError`): what the operating system refuses while a tree is
written (`OSError`, or the `ValueError` Python raises for a NUL byte in a name or a link target) is a `TreeError` (`write` or
`path`), so a caller needs to catch one class. Every error text is fixed and carries a short reason word, never a path or a byte of a
tree. A record's payload is read from the stream while the record is current; the reader drains what is left of it when the
caller moves on.

**Reading the worktree (supervisor).** `walk` reads a directory by descriptor: every directory is opened with `O_NOFOLLOW`
and `O_DIRECTORY`, every entry is classified from `lstat`, a symlink is shipped as its target text and is never entered, a
file is opened with `O_NOFOLLOW` and `O_NONBLOCK` and its open descriptor must be a regular file (a fifo that replaced a file
since `lstat` is refused, never waited on: the same lesson as the relay's), and it holds at most `MAX_DEPTH` descriptors open.
The size, the count and the path caps are checked from `lstat` data in a measuring pass, before a byte is read. Left out,
and counted in `Staged` (`skipped`, `git_names`): fifos, sockets and devices, names with a backslash, and every entry whose
name spells a git directory (the supervisor's own `.git` pointer, a submodule's pointer, a nested repository a previous worker
planted). A hard link is just a regular file (its bytes ship once per path). A file larger than the per-file cap fails
the session; it is not skipped.

**What the pod gets.** The manifest, then the `read_paths` files, the staged git directory (with the supervisor's local
`info/exclude` and `info/attributes`), the tree and, last, one `baseline`
record per shipped tree path (the pod computes its delta by walking its tree against that manifest: a path whose kind, exec
bit or hash differs is a `file` or `symlink` record, a baseline path that is gone is a `delete`; ALL changed paths come back,
ignored and untracked ones too). `extract(source, root)` is the pod side: it writes through `TreeWriter` (one component at a
time with `O_NOFOLLOW`, directories created on the way, a file created with `O_EXCL`, so no link is followed and no record
overwrites an earlier one), and at the end it requires the baseline to match what it wrote path for path and digest for digest.
Because the format carries paths, never directories, `extract` then makes the three empty directories a git directory needs
(`objects`, `refs/heads`, `refs/tags`; `GIT_SKELETON`): a detached head staged with no base ships no ref, and without `refs/` git
would refuse the copy as "not a git repository". A bundle with no git area gets no `.git` at all.

**The staged git directory** is built supervisor-side by `build_git_dir`, ONLY through `gitsafe` (`gitsafe.run` in the
worktree, whose pointer is checked first, and `gitsafe.repo_run` in a directory it just created): an empty repository with no
template (no hooks), a shallow `fetch` of the head commit and of the base commit from the supervisor's common git directory
over `file://` (so objects are copied, never linked, and no alternates and no remote are recorded), `HEAD` pointing at the
same branch (or detached), `refs/factory/base` for the base commit and the index of the head commit (`read-tree`; the
supervisor's own index is not shipped, see the differences below). The supervisor's local **`info/exclude` and `info/attributes`**
(the common git directory's, which git reads for a linked worktree) are copied into the staged git directory as plain files, so
the ignore rules and attributes that are local to the repository answer the same in the pod. They are read like any
untrusted file (the directory and the file are opened with `O_NOFOLLOW`, with `O_NONBLOCK` and a regular-file check, at most
`INFO_MAX_BYTES` 256 KiB each, PROVISIONAL): a link, a fifo, a linked `info` directory or anything that is not a small regular
file fails the staging (`TreeError` `git` or `BundleTooLarge`) rather than being followed or skipped; a missing file is simply
not copied. Its size counts against the bundle cap and it is built in a private directory of `scratch` that is removed on every path. **Depth:** `git log
base..HEAD` reads as it does in-process only when the commits it lists AND their fork point are present and the fork point is
reachable from the base, so the depth is `max(commits on head not on base, commits on base since the fork point) + 1`
(the base side is exactly what a busy base moves by since a job branch was cut); it is capped at 10,000 commits
(`MAX_HISTORY`, `BundleTooLarge` beyond) and then by the byte cap. Verified in tests: `diff`, `log base..HEAD`,
`status --porcelain`, `show HEAD:CLAUDE.md`, `rev-parse` and `ls-tree -r HEAD` of the copy equal the supervisor's worktree for a
branch with commits and **unstaged** uncommitted work (edits, new and deleted files, ignored files, a changed mode, a link), with
ignore rules and attributes from `info/exclude` and `info/attributes`, for a branch that merged the base (a merge commit), for a
detached head and (committed answers) for a conflicted merge in progress.

**Where the pod's git answers differ from in-process mode: the exact set.** The pod's git directory is a small, shallow copy
of the head commit and the base commit, not the supervisor's repository, so these differ, and each is pinned by a test so this
list cannot drift:

* **The index is the head commit's** (`read-tree HEAD`, no stat data), not the supervisor's. Work the supervisor has already
  staged but not committed (a `git add` or `git rm` without a commit) reads as unstaged work in the pod: `M ` is ` M`, `A ` is
  `??`, `D ` is ` D`, `git diff --cached` is empty and `git diff` shows the change. It is not shipped because slice 9b item 4
  keeps the index in the supervisor's own git directory, and the blobs a staged change names are not in the shallow copy. It
  needs a commit that failed after `add --all` (`runner.stage_and_commit` runs the two back to back), so it is rare; the files
  themselves are identical. The first `git status` in the pod also re-hashes the tree once, because the index has no stat data.
* **A conflicted merge in progress** (by the design of slice 9b item 4): `MERGE_HEAD` and the unmerged index stay in the
  supervisor's own git directory, so the pod's `status` and worktree `diff` show the conflict-marker files as ordinary edits of the
  head commit (`UU` there, ` M` here); the conflict-marker files themselves travel, the pod edits them, the delta returns them and
  the unchanged `commit()` of the supervisor concludes the merge.
* **The repository configuration is not shipped** (neither the common configuration nor a per-worktree one): the pod's git reads
  what `git init` wrote. A setting such as `status.showUntrackedFiles`, `core.autocrlf`, `core.filemode` or a diff, filter or
  merge driver of the supervisor's clone has no effect in the pod, and `user.*` is absent (the worker's `safe.directory` comes from its
  throwaway home, slice 10a).
* **Refs:** only the job branch (or a detached head) and `refs/factory/base` exist; there is no other branch, no tag, no
  remote-tracking branch, no stash and no reflog.
* **History is shallow** (the depth above): `git log base..HEAD` and everything on the head commit and the base commit read as
  in-process, but a command that needs history beyond them stops at the shallow boundary (without a base only the head commit is there).

Submodule files travel as ordinary tree files; the copy's git directory has no `modules`
directory and no submodule configuration, so no gitlink metadata exists for the pod to rewrite. The worker's own `git`
needs `safe.directory` in its throwaway home (the supervisor's tree and the pod's are owned by different users): that file is
part of the home the wrapper writes from the manifest (slice 10a).

**Staging** (`stage(spool, session, data_root=..., worktree=..., manifest=..., scratch=..., base=..., read_paths=...)`)
measures first (a cap is `BundleTooLarge` before any read), then checks the spool: **it waits instead of failing.** When the
bundle would take the spool (bytes already in it, counted like the relay counts them, plus the bundle) past
`SPOOL_REFUSE_BYTES` it raises `SpoolFull`, which the caller treats like a capacity wait and retries later; only then does it
build the git directory, measures it (its bytes count against the bundle cap and the spool limit like the tree's), checks
again and writes `<spool>/<session>/in/bundle` as a temporary name and one
rename (the relay never sees a partial bundle, and a failed run leaves the previous bundle and no temporary file). `base`
must be a hex commit id; anything else is refused before it reaches git. `worktree` and every `read_paths` entry must be
under `data_root` (a read path must be a regular file; a link, a directory or a missing path is refused). A read path inside
the worktree, or listed twice (also through a link to its directory), is refused at staging with `TreeError` (`read path`): the
pod creates every file with `O_EXCL`, so a bundle with two records for one pod path could never be extracted, and the caller
should hear of it when it stages, not in the pod.

**Concurrent stagings share one limit.** The manager stages sessions in threads, so a check followed later by a write would
let several runs pass the same check and together overshoot `SPOOL_REFUSE_BYTES`. Each run therefore holds a claim (under one
module lock, per spool) for the bytes it is about to write, from its first check until its bundle is renamed into place or it
fails; every check counts the bytes on disk plus the claims of the other runs. The claim is released on every path, and the
bytes of a bundle that is being written are briefly counted twice, which can only make another run wait a little longer.

**Limit, inherited by slices 11, 12b and 10a: two accountings, not one.** Only stagings of this process are counted, and the
claim lives in `bundle.py` alone. The relay (`relay.py`, slice 8) keeps its own: `_spool_usage` counts bytes on disk, plus
`_reserved` for the transfers it is serving, and it knows nothing of a staging that has claimed its bytes and not yet written them
(a test pins that a held claim is not on disk). Likewise `stage` does not see what the relay has reserved. The two checks can
therefore pass at the same moment and together take the spool past `SPOOL_REFUSE_BYTES`; the overshoot is bounded by what is in
flight on the other side (at most `BUNDLE_MAX_BYTES` for each staging, one reservation for each relay transfer), and the contract
leaves 4 GiB between the refusal limit (12 GiB) and the spool's `sizeLimit` (16 GiB, where the kubelet evicts the manager pod).
Whether that headroom is enough for the manager's real concurrency is not shown. This slice does not touch the relay or the
manager (nothing imports the module yet), so it does not unify them: the slice that wires the manager must make the relay's
admission and `stage` consult one shared reservation (or run them from one lock).

**Slice 9a, where each check runs.** L: `PYTHONPATH=src python3 -m unittest tests.test_bundle tests.test_gitsafe_lint`
(real local git repositories; the fifo tests use a thread with a timeout, not a sleep). C: compile and the unit tests on Python
3.11 to 3.13 (`.github/workflows/ci.yml`). P: not applicable (no Terraform). V (live only): bundle size and time on real
repositories.

**UNVERIFIED (slice 9a).** Throughput and size on real repositories (only synthetic and small test clones were run); the
shallow-clone depth that review diffs really need (the formula above is exact for the shapes tested, not proven for criss-cross
merges); the exact set of differences between the pod's git answers and in-process mode (staged work, a conflicted merge, the
repository configuration, refs and history beyond the shallow boundary: all listed above, not proven to be the complete set for
every repository shape); that a staging and the relay can together overshoot the spool's refusal limit (two separate accountings,
above); how much disk the shallow fetch uses before its size can be measured (the cap is checked after it, not during);
git version behaviour on the production image (tests ran git 2.43: `--template=`, a fetch by commit id with
`uploadpack.allowAnySHA1InWant` passed as `-c`, `read-tree`); the bundle-local `MAX_FILES` and `MAX_DEPTH`; and that the extractor's
modes and ownership (the wrapper chowns, slice 10a) work with `hostUsers: false`.

## The pod worker, part A: fixed output, network canary, fetch and extract (slice 10a)

`src/factory/podworker.py` (`python -m factory.podworker`, the command of `podspec.WORKER_COMMAND` under `tini`) is the root
wrapper a worker pod runs. Part A is everything before `claude` starts: it checks its own environment and token file, proves
the pod cannot reach what it must not reach, fetches its tree from the relay once and lays it out. Part B (slice 10b, the next
section) adds running `claude` as the worker identity, streaming, the delta, the footprint and SIGTERM; part A left one seam for it
(`Worker.execute`, which fails closed with the stage `run` when the manifest has no valid `run` section, so a pod that fetched its
tree and can run nothing never looks like a session that ran). Nothing launches a pod yet. Wording: **contract-tested** against the in-process
relay, a fake network and real temporary directories; no container ran and no cluster was consulted.

**Output discipline.** Log Analytics collects stdout and stderr cluster-wide, so the wrapper's own streams carry
only these lines, each a fixed text:

| Line | When |
|---|---|
| `podworker: started` | after the arguments were checked |
| `podworker: preflight ok` | the relay accepted the canary result |
| `podworker: bundle fetched` | the bundle was fetched and extracted |
| `podworker: exit <n>` and `podworker: result posted` | part B: the agent's exit status (or 128 plus its signal) before the delta is produced, and the report and delta were accepted |
| `podworker: failed <stage>` | on standard error, once, for any failure |

One top-level handler turns EVERY exception (a parse error of the bundle, the relay's answer, the file system, an import, a
`SystemExit` carrying text, a keyboard interrupt) into the failure line, whose `<stage>` is the closed table below and whose
exit code is fixed per stage; the exception object itself is never formatted. `sys.excepthook`, `threading.excepthook` and
`sys.unraisablehook` are replaced, `faulthandler` is off, warnings are off, there is no argument parser (the arguments must be
exactly the program name; anything else is `argv`, and `--help` prints nothing), nothing of the package is imported before the
handler is in place (an import that fails is `import`), and the container's termination message file (`/dev/termination-log`)
is never opened, so no free text reaches the pod status or its events. When the relay is unreachable nothing is printed
about it: part B drops the stream.

| Stage | Exit | What failed |
|---|---|---|
| `argv` | 64 | the arguments are not exactly the program name |
| `environment` | 65 | a variable of the pod's allow-list is missing or malformed, or the pod name is not the session's |
| `token` | 66 | the token file is missing, not a regular file, has a group or other permission bit, or does not hold a token |
| `import` | 67 | a module of the package cannot be imported |
| `canary` | 68 | a canary check failed (the code was posted to the relay first) |
| `preflight` | 69 | the relay is unreachable or refused the preflight post |
| `wait` | 70 | the relay answered 425 twelve times or for 60 seconds |
| `bundle` | 71 | the relay is unreachable, refused the fetch, or the body is shorter than its `Content-Length` |
| `extract` | 72 | the bundle is not a valid bundle (the wire format, a cap, a link in the way, a baseline mismatch) |
| `manifest` | 73 | the manifest is not as documented below |
| `files` | 74 | a manifest directory or file could not be created |
| `chown` | 75 | the worktree could not be handed to the worker identity |
| `run` | 76 | part B: the manifest has no valid `run` section, the token deadline leaves no time, or anything unexpected happened while the session ran |
| `internal` | 77 | anything that no stage claimed (an uncaught exception, a thread that died) |
| `key` | 78 | part B: the Anthropic key file is missing, not a regular file, has a group or other permission bit, or does not hold a key |
| `spawn` | 79 | part B: the agent could not be started |
| `result` | 80 | part B: the report and delta could not be posted (three tries) or were refused with a 4xx other than 410 |
| `gone` | 81 | part B: the relay says the session is over (a 4xx answer to a heartbeat or a stream post, 410 above all, or a 410 to the result): the agent was stopped and nothing is posted |

The numbers avoid 126 and 127 (`execnocaps`) and the 128-plus-signal range. A failure after the fetch and before the agent runs
(`manifest` to `spawn`) is reported by the exit code only: the wrapper posts no result for it, so the supervisor sees a pod that
ended with a fixed code and no result. Part B posts the final outcome of a session that ran, see below.

**The network canary, before any tree is fetched.** In parallel, one thread per check, three seconds each, every
join bounded:

* a TCP connect that **sends nothing** to each forbidden destination of `podcontract.CANARY_FORBIDDEN`: the instance metadata
  address `169.254.169.254:80`, the Azure host channel `168.63.129.16` on 80 and 32526, the node's kubelet port
  (`FACTORY_NODE_IP`:10250) and the manager pod's API and metrics ports (`FACTORY_MANAGER_ADDRESS`:8080 and `FACTORY_MANAGER_METRICS_ADDRESS`:9464, the Service that exposes the metrics port). A successful
  connect is a failure;
* the API server at BOTH of its addresses (`podcontract.CANARY_API_PROBES`): the service address from the kubelet-injected
  `KUBERNETES_SERVICE_HOST` and `KUBERNETES_SERVICE_PORT`, and `FACTORY_API_PUBLIC_HOST`:443. Each gets one UNAUTHENTICATED
  `GET` of `/api/v1/namespaces/factory/secrets` over TLS. Only 401, 403 or no connection (or no HTTP answer) passes; any other
  status fails. The TLS handshake does not verify the server (the pod has no copy of the cluster CA, and the request carries
  no credential and no body: the only question is whether an anonymous request is answered);
* a CONTROL connect to the relay must succeed, so a dead network cannot pass as an isolated one.

A check that is neither clearly passed nor clearly failed fails (a probe that raises is `probe_error`; a destination whose
address cannot be resolved from the environment is `canary_config`); a probe that has not finished when the ten-second join
time ends **fails closed** (a forbidden or API probe is `probe_error`, the control `control_failed`): a hostname that blocks in
`getaddrinfo` (`FACTORY_MANAGER_ADDRESS`, `FACTORY_MANAGER_METRICS_ADDRESS` and `FACTORY_NODE_IP` may be names) proved nothing. The first failed check in
probe order (the contract's forbidden list, the two API probes, the control) is the one reported. On any failure the wrapper posts
`{"result": "failed", "code": "<code>"}` to the relay, which refuses to serve the bundle from then on (slice 8), and exits with the
`canary` code; the bundle is never requested. The codes are fixed words, never a destination: `reach_imds`, `reach_wireserver`,
`reach_hostga`, `reach_kubelet`, `reach_manager_api`, `reach_manager_metrics`, `api_service_open`, `api_public_open`,
`control_failed`, `probe_error`, `canary_config` (a test pins that every contract entry has one and that the relay's code pattern
accepts them all). The supervisor treats a failed preflight as `isolation_failed` (slice 12b).

**Coverage, stated so nothing is assumed.** `kubernetes.default` is covered by the service-address probe. "Another worker pod"
is NOT probed (the pod cannot know a peer's address): it is covered by the network policy only (ingress denied, slice 15a) and
by a live control from a pod of a real cluster, which has not been run. The canary proves what is reachable from THIS pod at THIS moment; it is not a proof of the
policy.

**Fetch.** After a good canary the wrapper posts `{"result": "ok"}` and `GET`s `/v1/bundle` once. A relay answer of 425 (the
session is not bound to its pod yet, slice 8) is retried with a bounded backoff on an injectable clock: at most 12 requests that
are answered 425 in all, with the waits 1, 2, 4 and then 5 seconds (47 seconds), and never past 60 seconds measured from the
first 425; then the stage `wait`. The preflight post and the bundle fetch **share one budget** (the preflight can also meet a
425: the pod may start before the supervisor has bound its name and uid). Any other status is not retried. The body must carry a
`Content-Length` of at most `BUNDLE_MAX_BYTES` (one byte more is the stage `bundle` and no body byte is read); a body that ends
early is `bundle`, not a tree error. **No overall deadline in part A:** `FACTORY_SESSION_DEADLINE` is validated and not used here;
a slow-drip body is bounded per socket operation (60 s), not as a whole. Part B (or the extraction loop it drives) must enforce an
overall fetch deadline from `FACTORY_SESSION_DEADLINE`. The relay marks the fetch
used only when the last byte was written (slice 8), so a second run for the same session is refused at the preflight.

**Extract and lay out.** `bundle.extract(source, root)` (slice 9a, not copied) writes the tree under the data directory at the
identical absolute paths, one component at a time without following links and without overwriting. Then, in this order: the
manifest is validated (`manifest`), the worktree and everything in it (the `.git` directory included) is handed to the worker
identity (uid 10002, gid 10010) with `lchown`, so a link is changed itself and never followed (`chown`), and the manifest's
directories and files are created (`files`). The `read` files stay root's and world-readable (0444 or 0555). The manifest
(`meta/manifest.json` of the bundle, written by the supervisor, at most 256 KiB, never a secret) is read here for the keys below;
the `run` key is part B's:

| Key | Meaning |
|---|---|
| `version` | the integer 1 |
| `run` | part B's section, below |
| `worktree` | completed by `bundle.stage`: the worktree's path under the data directory |
| `dirs` | at most 16 absolute paths under the data directory: the worker's throwaway home and config dir. Created 0700 and owned by the worker identity |
| `files` | at most 64 of `{"path", "data", "mode"}`: `path` absolute under the data directory, `data` the base64 of the bytes, `mode` 0600 (384) or 0640 (416) and nothing else |

Every path must be clean (the rules of `bundle.split_relative`: no `..`, no empty or `.` component, no `.git` in any spelling),
outside the worktree, and unique among the manifest's paths. A file is created with `O_EXCL` through `bundle.TreeWriter`. A 0600
file (the prompt) stays root's: only the wrapper reads it and hands it to `claude` on its standard input, as `LocalBackend` does.
A 0640 file (settings, system prompt, the `.gitconfig` with `safe.directory`) gets the worker's group, so the worker reads it and
cannot change it. `PodBackend` (slice 12b) writes these keys from the `LaunchSpec` (`files`, `home`, `config_dir`); a prompt or a
settings file too large for the manifest (`bundle.MANIFEST_MAX_BYTES`, **2 MiB**, sized so that a long system prompt built from a repository's skills fits) fails the staging (`BundleTooLarge`, "bundle too large"). The files are base64 in the manifest (4/3 of their size), so the largest system prompt is about 1.4 MiB.

**The token.** The relay token and the key are root-only files of the Secret volume (`/run/factory-session`). The wrapper reads
the token file only: it must be a regular file with no group or other permission bit (the kubelet's volume is a set of links
that nobody else can write, so the final component is followed and the open descriptor is what is checked), and its content is
held in memory: never printed, never copied to a file, never in an argument or in a child's environment, and the client's
`repr` does not contain it. The owner of the file is not checked (under `hostUsers: false` what the volume shows as root is
UNVERIFIED); the mode is.

**Where each check runs (slice 10a).** L: `PYTHONPATH=src python3 -m unittest tests.test_podworker` (a real `RelayServer` on
127.0.0.1, a scripted HTTP server, a fake network, real temporary directories, a real `python -m factory.podworker` process; every
thread has a bounded join, no test sleeps and the backoff runs on an injected clock; `chown` is patched, plus one run with the
real `lchown` to the current identity). C: compile and the unit tests on Python 3.11 to 3.13
(`.github/workflows/ci.yml`). An image build and its smoke test belong to the operator's own pipeline or a manual `docker build`
(`image/README.md`, "Build and check"): the module is part of the image; the smoke test does not run it. P: not applicable. V (live only): everything in a real pod:
the canary outcomes on a real node, `KUBERNETES_SERVICE_HOST` in a pod with `enableServiceLinks: false`, the token
file's mode and owner, uid 10002 unable to read it.

**UNVERIFIED (slice 10a).** The wrapper `command` and the `tini` path in the image; that the canary destinations are the complete
list of what must be unreachable (they are the contract's list, not a proof); that the kubelet injects `KUBERNETES_SERVICE_HOST`
and `KUBERNETES_SERVICE_PORT` with `enableServiceLinks: false` (a pod without them fails the canary closed with
`canary_config`, which the first live run would show); how a real API server answers an unauthenticated request over the service
and the public address (an answer other than 401 or 403, a 404 or a 5xx from an overloaded server included, fails the canary:
fail closed); that the token file's owner and mode in a Secret volume are as the pod spec says under `hostUsers: false`;
`chown` and `lchown` to uid 10002 with `hostUsers: false`; the relay's behaviour under a real slow link (only a local socket was
used).

## The pod worker, part B: run, stream, delta, footprint, SIGTERM (slice 10b)

Part B is `Worker.execute` of `src/factory/podworker.py` (the section "The run" of the module) and the report module
`src/factory/podreport.py`. It runs after part A has laid the tree out. The output discipline of part A is unchanged and
extended: the wrapper's own streams carry the lines of the table in part A and the failure line, nothing else. The agent's
standard output and standard error go to pipes that the wrapper reads and sends to the relay, **never to its own streams**
(`stdout=PIPE`, `stderr=PIPE`, `stdin=PIPE`; a test writes the poison text to the agent's file descriptors 1 and 2 directly), and
the memory guard's own fixed lines (`factory: memory guard ...`) are muted for the run because they are not on the whitelist.
Nothing launches a pod yet. Wording: **contract-tested** against the in-process relay, a scripted HTTP server, real temporary
directories and **real child processes** (a small script stands in for `claude`); no container ran and no cluster was consulted.

**The `run` section of the manifest.** `PodBackend` (slice 12b) writes it from the `LaunchSpec`; part B checks all of it before it starts
anything (the stage `run`, nothing is spawned, nothing is streamed):

| Key | Meaning and checks |
|---|---|
| `argv` | the agent's command line, the binary first: 1 to 256 strings of at most 64 KiB, no NUL, encodable, the first not empty |
| `env` | exactly the non-secret names of `agent.worker_env` (`HOME`, `CLAUDE_CONFIG_DIR`, `DISABLE_AUTOUPDATER`, `DISABLE_TELEMETRY`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`; a test pins that the list equals the backend's), `HOME` and `CLAUDE_CONFIG_DIR` required and each one of the manifest's `dirs` (so it exists and is the worker's); values at most 4096 characters. No other name is accepted: not `PATH`, not the key, not `LD_PRELOAD` |
| `cwd` | the worktree itself, as part A laid it out |
| `prompt` | the path of the manifest file of mode 0600 that holds the prompt; it must name exactly one such file. The wrapper takes the bytes from the manifest and writes them to the agent's standard input from a thread (a prompt larger than a pipe must not block the wrapper); the file stays root's, so the worker cannot read it. The thread writes to the **raw descriptor**, which is set non-blocking, and waits for room with a 0.25 s timeout, looking at a stop flag each time: it never writes through the `BufferedWriter` of `Popen`, whose lock `close()` needs. At the end the wrapper stops the thread (within a second) and only then closes the descriptor, so a process outside the agent's group that inherited the agent's input and never reads it cannot keep the wrapper from exiting after the result was posted (a test does exactly that with a 150 KiB prompt) |
| `timeout` | seconds, at least 0.1 and at most the contract's deadline clamp (14400); a bool, text or NaN is refused |
| `memory_limit_mb` | an integer from 1 to 2^20, or null (no limit, the footprint is still recorded) |

**The key.** The Anthropic key is the other root-only file of the Secret volume (`podcontract.SECRET_KEY_ANTHROPIC`). It is read
like the token (a regular file with no group or other permission bit, at most 4096 bytes, 8 to 4096 printable ASCII characters,
one trailing newline dropped; the stage `key`) and it is put in **one** place: the agent's `ANTHROPIC_API_KEY`. It is never in an
argument, a file, a log line, the report or the delta (a test searches all of them). The relay token is never given to the agent
at all (a test searches its argument list and environment).

**The agent's identity and environment.** `Popen(argv, cwd=..., user=10002, group=10010, extra_groups=[], umask=002,
start_new_session=True, close_fds=True, env=...)`, exactly what `LocalBackend` does, so the agent is the worker and nobody else, with
no supplementary group, in its own session and process group. The command line is wrapped in `execnocaps.py` (`python -I -S
execnocaps.py -- <argv>`, as `agent.drop_capabilities_first`), which clears the ambient capabilities and fails closed with 126. The
environment is built, never inherited: the ten names of `agent.CHILD_ENV_KEYS` that the pod has (`PATH`, `LANG`, ...; a test pins
that the list equals the backend's), the manifest's `env`, and `ANTHROPIC_API_KEY`. None of the pod's `FACTORY_*`,
`KUBERNETES_*` or `AZMON_*` names reaches it. A program that cannot be started is the stage `spawn`.

**Streaming.** Each of the agent's two pipes has a reader thread and a sender thread (`Pump`). The reader drains the pipe into a
buffer of at most 8 MiB; the sender posts what is buffered, in order, coalesced into posts of at most 1 MiB
(`POST /v1/stream/transcript` and `/v1/stream/stderr`). **While the relay answers nothing is dropped**: a full buffer stops the
reader and the agent blocks on its pipe, because the transcript carries the agent's `result` event. A post is tried three times
(waits 1 s and 2 s). Then:

* a **4xx** answer (the session is over, a cap was reached, the pod is not the bound one) ends the session at once (`gone`);
* an unreachable relay, or three answers of 5xx, ends **that stream**: from then on everything the agent writes is read and
  **dropped**, so a dead relay never blocks the agent, and the report says so (`dropped`). Nothing is printed (part A: "part B drops
  the stream");
* a chunk whose answer was lost, but which the relay stored, is sent again and appears twice (the relay's append is "all of it or
  none of it", and it cannot tell a retry from a new chunk): UNVERIFIED how often that happens on a real network; the transcript's
  line reader (`agent.last_result`) skips a half line and reads the result event whichever copy is whole.

The wrapper never reads what the agent wrote. When the pipes do not reach the end (a process that left the agent's group by
`setsid` holds one open) the readers are abandoned after 5 seconds and the senders after 60 seconds of flushing; the descriptor is
closed only once its reader is done, so its number is never reused under a reader. The two pumps share **one** drain deadline and
**one** flush deadline (`finish_pumps`; a test with four wedged pumps pins that more pumps add no time): the total is at most `drain
+ flush + 2 s` (the threads that did not end are given one second, once, together, after each deadline), never the sum over the pumps.

**The heartbeat** is posted at once and then every `HEARTBEAT_SECONDS` (30, the contract's), **until the result is posted** (the
wrapper stops it only after the result was accepted, or the session failed): it goes on through the drain, the hashing and reading
of every file for the delta (up to 1 GiB) and the result upload (up to 900 s), so a long delta never makes a live pod look silent to
pod-side liveness (slice 19, `ORPHAN_SILENCE_SECONDS` 300). A test pins that beats arrive during the delta read and that none follows
the posted result. The longest gap between two beats is the interval plus one post (a socket operation waits at most 60 s): about 90 s,
under the 300 s limit; a heartbeat post that hangs does not delay the delta, because it has its own thread. A 4xx answer
ends the session (before the delta is produced, or, if it comes later, as the refusal of the result post); a lost or 5xx answer is
ignored (the next one tries again). A stream post also writes the relay's heartbeat file, so a busy agent beats twice over.

**How a session ends.** The wrapper waits for the first of: the agent exits; the **time limit** (the manifest's `timeout`, at most
the token deadline `FACTORY_SESSION_DEADLINE` less `RESULT_RESERVE_SECONDS`, 60, so the result can still be posted; no time left is
the stage `run`); **SIGTERM** (the kubelet deleting the pod or its `activeDeadlineSeconds` passing; `tini` forwards it); the relay
ending the session. Ending it by force is SIGTERM to the agent's process group, five seconds of grace and SIGKILL, and the
group gets SIGKILL last in **every** case, as `LocalBackend` does, so whatever the agent left running in its group is gone before the
tree is read. The memory guard (`memwatch.Watchdog`, the same code as in-process mode) samples the agent's tree and kills an
over-limit session; its footprint (`peak_rss_mb`, `cpu_seconds`, `samples`, `limit_mb`) goes into the report.

**SIGTERM.** The handler only sets a flag (it is installed for the run, in the main thread only, and the previous handler is
restored). The wrapper then stops the agent as above (at most 5 s of grace, and at most 5 s more for the group to be reaped after SIGKILL), drains the
pipes for at most 2 s and flushes the senders for at most 8 s (one deadline each, shared by the two pumps), and **still produces the
delta and posts the report and the result**: the agent's work up to the signal is not lost. The real worst case before the delta read
starts, with every wait running out, is `TERMINATED_WORST_SECONDS` = 5 + 5 + 2 + 8 + 1 + 1 = **22 s** of the contract's 30 s
termination grace (`TERMINATION_GRACE_SECONDS`; the two 1 s are the joins of the threads that did not end), leaving **8 s** for the
delta and the result. That worst case needs a stuck relay and a holder outside the group; a normal stop is the agent's exit plus a
drain that ends at the end of the pipe, which leaves nearly all of the grace. A test pins the arithmetic (at least 5 s must remain);
a second SIGTERM changes nothing; the kubelet's SIGKILL at the end of the grace is the only limit (PROVISIONAL: whether 8 s, or the
larger share a normal stop leaves, is enough for the delta of a real tree is UNVERIFIED). The outcome is `terminated`. The wrapper's exit code is **0 whenever the result was posted**,
whatever the agent's own status was (it is in the report): the supervisor decides by the relay's state and the report, and the pod
phase is `Succeeded`.

**The delta (`bundle.produce_delta`)** is produced after the agent's group is gone and the pipes are drained, from the worktree and the
baseline part A kept, by the same `Writer` and the same `walk` that the supervisor reads its own worktree with (descriptors,
`O_NOFOLLOW`, a fifo never waited on, a git directory of any spelling never entered, nothing followed): a path whose kind, exec bit or
hash differs from the baseline is a `file` or `symlink` record, a baseline path that is gone is a `delete`; ALL changed paths come
back, ignored and untracked ones too; the pod's `.git` never does (a test has the agent rewrite its config, add a hook and a
`MERGE_HEAD`). It is written to an unnamed temporary file (`O_TMPFILE`, in `/tmp`: no name for the worker to find) and sent from
there. Its form is the one slice 9b validates; a test applies it to a supervisor tree with `declared_records` and `declared_bytes`
and compares the result, and the delta of the tests' stand-in (`pod_delta` of `tests/test_bundle.py`) is byte for byte the same. The
agent's changes are returned **whatever the outcome** (a timeout, a SIGTERM, a memory kill, a non-zero exit): the supervisor decides
what a failed job leaves in its worktree, as in-process mode does. A phase whose write scope is `none` is not special-cased here: the
wrapper reports the truth and the supervisor refuses a non-empty delta (`scope`).

A cap (the delta's bytes or records, one file over 64 MiB, a path, the depth, a tree larger than a bundle could be) is reported as
`too_large`; a tree that changed while it was read, a worktree that was swapped for a link (a test does it) or a scratch directory
that cannot be used is `failed`. A file whose size is not the one the walk's `lstat` counted when it is opened, or that has a byte
more than that after the counted ones were read (it **grew** between the walk and the read), is a change just like one that shrank:
`TreeError("changed")`, so the delta is `failed` and never a silently truncated copy (tests: `open_file`, `put_file`, `digest_of` and
`produce_delta`, for a growth before the open and after it). Nothing is truncated: **the delta part of the request is then empty (zero bytes, not even a header)**
and the report's `delta` says why. The supervisor must read the report before it opens the delta.

**The report (`podreport`, `POST /v1/result`).** One JSON object of at most 64 KiB, with a closed key set and no free text:

| Key | Content |
|---|---|
| `version` | 1 |
| `outcome` | `exited`, `timeout`, `terminated` or `memory_limit` |
| `exit_code` | the agent's status; a negative number is the signal that ended it |
| `footprint`, `exceeded_mb` | the memory guard's record, or null; the resident MiB of the sample over the limit, or null |
| `delta` | `status` (`ok`, `too_large`, `failed`), `records` and `bytes`: the numbers the supervisor passes to `apply_delta` as `declared_records` and `declared_bytes` (the stream has no trailer, section "The file delta") |
| `streamed`, `dropped` | per stream (`transcript`, `stderr`): the bytes the relay accepted, which must equal the size of `out/transcript` and `out/stderr`; and whether the wrapper had to discard data |

`podreport.validate` (and `loads`, which reads the bytes the relay stored) is the one function that checks it: the key set is closed at
every level, a bool is never a number and a number never a bool, every number is bounded, a delta that is not `ok` declares nothing,
and the error text is the fixed `report`. Slice 12b calls it on `out/report.json` before it trusts a word of it. The wrapper posts the report
and the delta **once** (a lost answer is tried three times, 1 s and 2 s apart on the injected clock; a retry after a commit whose
answer was lost meets a 410, which the wrapper reports as `gone`: the relay's state `finished` is what tells the supervisor the
session did end); a 410 is `gone`, any other 4xx, three failures or a report that cannot be built is `result`.

**For the supervisor (slice 12b), in addition to the sequence of the relay section:** write the `run` section and its files
from the `LaunchSpec` (the prompt 0600, settings and system prompt 0640, `home` and `config_dir` in `dirs`, the session's phase `timeout`
and `memory_limit_mb`); read `out/report.json` with `podreport.loads` and refuse the session on `ValueError`; refuse a non-`ok` delta
status before opening the delta; pass the report's numbers to `apply_delta`; compare `streamed` with the sizes of the two stream
files and treat `dropped` as an incomplete transcript (a session with no `result` event is then `agent produced no result event`, as
in-process mode); turn `outcome` `timeout` into `agent.AgentTimeout`, `memory_limit` into the memory-guard error, and `terminated` into a
retryable failure; and treat a pod that ended with the stage `gone`, `result`, `key`, `spawn`, `run` or any earlier stage and no `finished`
relay state as a failed session.

**Where each check runs (slice 10b).** L: `PYTHONPATH=src python3 -m unittest tests.test_podworker tests.test_podworker_run
tests.test_podreport tests.test_bundle tests.test_delta_malicious` (real children started in the test's own process group of
sessions, the relay's real server on 127.0.0.1, a scripted server for answers the relay does not give, a FIFO for the one rendezvous
with the child; the worker identity is the test's own, and the identity keywords are checked on a recording stand-in for `Popen`).
C: compile and the unit tests on Python 3.11 to 3.13 (`.github/workflows/ci.yml`); the new module is part of the image, and an image
build belongs to the operator's own pipeline or a manual `docker build`. P: not applicable. V (live only): everything in a real pod, see the list below.

**UNVERIFIED (slice 10b).** `Popen(user=, group=, extra_groups=[])` and `execnocaps` as container root under `hostUsers: false` (the
`setgroups` and `prctl` calls with the pod's capability set and seccomp profile); that the agent really runs as uid 10002 with an
empty permitted set; that `tini` forwards SIGTERM to the wrapper and that the 8 s of the 30 s grace that the worst case leaves (more in a normal stop) are enough to stop, drain and
produce the delta of a real tree; that the memory guard works in the pod (`/proc` visibility, pidfd, the wrapper not being pid 1); that
`claude` is found on the `PATH` the image sets (the pod's allow-list has no `PATH`, so the child's `PATH` is the image's); that a
process which left the agent's group (`setsid`) cannot change the tree between the group's death and the end of the delta read (it can:
only the group is killed; the delta is a snapshot per file, a file that shrank or grew while read is `failed`; a change with the same
size is not detected: the bytes of that file may be a mix of two states, which only a quiet group makes impossible); that the longest
gap between heartbeats during a long delta stays well under `ORPHAN_SILENCE_SECONDS` on a real network (about 90 s by the arithmetic
above); that a non-blocking write end of the agent's input behaves as on the test host under the pod's seccomp profile; throughput of the pipe to
the relay and of the 1 MiB posts over plain HTTP, and the 8 MiB buffer, on a real session; the duplicate chunk after a lost answer;
the delta read time of a real worktree (every file is hashed) and its memory; `O_TMPFILE` on the `emptyDir` of `/tmp`; whether the
relay's four connections per peer address hold on a real network (the wrapper holds at most three: two streams and the heartbeat; the
result is posted after the streams are done, and the heartbeat goes on beside it: two at that time); the numbers `RESULT_RESERVE_SECONDS` 60, the stop grace 5 s, the drain and flush times and
the 8 MiB and 1 MiB sizes (all PROVISIONAL); and that exit code 0 for a posted result is what slice 12b's classifier wants (it reads
the report, not the exit code).

## The file delta: validation and safe apply (slice 9b)

What a pod sends back is a **file delta**, in the wire format of slice 9a (the same `Writer` and `Reader` write and read it, so the pod
side of slice 10b and this side cannot diverge): only the area `tree` and only the kinds `file`, `symlink` and `delete`; a rename is a
`delete` plus an add. The pod's `.git`, `config`, `MERGE_HEAD`, `index` and hooks have no way into it: the area `git` is not a delta
area, and no record can spell a git name (slice 9a). The delta is **hostile input**: a process that a possibly prompt-injected model
ran wrote it, and the supervisor applies it to ITS OWN worktree with ITS privileges. It is applied by
`bundle.apply_delta(source, worktree, write_scope=..., scratch=...)`, in three steps, each of which can only fail the whole delta:

1. **`validate_delta` (the stream, no tree in sight).** The whole stream is read once, through the 9a `Reader`, into a private copy
   (memory up to 4 MiB, an unnamed temporary file in `scratch` above that), so what is applied is exactly what was checked even if the
   source changes afterwards. Everything below is checked before any byte of any tree is touched.
2. **`ValidDelta.check_tree` (the tree, read-only).** Every directory above a path is looked at with `lstat` and opened with
   `O_NOFOLLOW`: nothing is written, deleted or created. It refuses what would fail or go wrong part-way (below), so a delta that is
   going to be refused is refused before the first write.
3. **`ValidDelta.apply` (the only step that writes).** Deletions first (`unlinkat`), then the directories the deletions emptied
   (`unlinkat` with `AT_REMOVEDIR`, deepest first: only an empty directory goes), then the files and links in stream order. Each
   path is resolved from one descriptor of the worktree (opened once, `O_NOFOLLOW`), **one component at a time with
   `O_NOFOLLOW`**, so a path never goes through a link and a swap of the worktree's own path after the open changes nothing. A file is
   created under a temporary name in the TARGET directory (`O_CREAT|O_EXCL|O_NOFOLLOW`, mode `0644` or `0755`, `fchmod` so no umask
   matters) and renamed over the old entry; a link is created under a temporary name (`symlinkat`) and renamed. A rename replaces the
   directory entry itself, never what a link points at and never a hard-linked inode (a hard link to the old file keeps the old bytes).
   Only the exec bit of the mode exists on the wire and is honoured; there is no bit for setuid, setgid or sticky.

**Errors** (`BundleError`s with a fixed text and a short `reason` word; no path, name or byte of a delta is ever in one):
`DeltaRejected` ("delta rejected", nothing was written), `DeltaTooLarge` ("delta too large", nothing was written: a cap fails the
session and nothing is truncated) and `DeltaApplyFailed` ("delta could not be applied": the operating system refused a step while
applying; the worktree is left exactly as it is, no rollback, retained for inspection like any failed job, and no temporary file is
left behind).

| Step | Reasons |
|---|---|
| stream | `magic`, `version`, `truncated`, `kind` (any kind outside `file`, `symlink`, `delete`, or an area that is not `tree`: a hard link, a device, a fifo, a directory, the byte a tar archive uses for a hard link, a baseline record), `flags` (anything but the exec bit, and the exec bit on anything but a file), `path`, `size`, `target` (a NUL in a link target), `read` (the source could not be read) |
| names | `link`, `duplicate`, `collision`, `clash`, `scope`, `declared` |
| tree | `path`, `clash`, `changed`, `worktree` |
| too large | `size`, `bytes`, `files`, `directories` |
| apply | `path`, `write`, `changed` |

**What the stream check refuses.** A path that is not a clean relative path (no `..`, no empty or `.` component, no NUL, no backslash,
at most 4096 bytes and 100 components: the 9a rule, one function for both directions); **a component that is `.git` in any case or
Unicode variant** (the NFKC-casefolded name after removing default-ignorable code points and trailing dots and spaces) or `git~1`,
in any record, so `.git`, `.GIT`, `.Git/`, `git~1`, a zero-width character or a trailing dot or space inside the name, and a config
file as `.git/config` are all refused, for a `delete` too; a component over 255 bytes (it could only fail part-way); **two paths that
are the same name after folding** (`collision`: `Readme` and `README`, the ligature and `fi`, a precomposed and a decomposed accent,
`a` and `a.`; also when only a directory of the two paths differs), a path written twice (`duplicate`) and **a path below another
record's path** (`clash`: a file and a directory of one name, and in particular a symlink followed by a file written "through" it,
in either order and with other records between). The one allowed shape is a **transition**: exactly one of the two paths is a
`delete` (`delete a`, `file a/b` turns a file into a directory; `delete a/b`, `file a` a directory into a file). A symlink is
accepted only with a **relative target that goes up first and then only down**: a run of `..` (at most as many as the link has
directories above it, so it cannot pass the root) followed by names, and **never a `..` after a name**. That is what makes the lexical
check sound against a chain. A link that is created sits in a real directory of the tree (the tree check), so a leading `..` leaves it
for a real ancestor and the names after it only go down; by induction no link, and no chain of links, leads out of the tree. A check
that only kept the depth from going negative accepted `l1 -> .` followed by `l2 -> l1/..`, where `l2` is the parent of the tree, and
the supervisor follows links when it reads the files it applied (`refuse_conflict_markers`); the price of the stricter rule is that a
worker must write `../README.md` and not `src/../README.md`. Beyond the spec a link is also refused when a component of its target
names a git directory, when the target has a backslash, and when the target is 4096 bytes or more (the wire format allows 4096, but
`symlinkat` takes 4095 at most and would fail part-way through the apply). The check sorts the folded components once, so it is
`O(n log n)` however the records are arranged, and the same pass counts the **distinct directories the paths name** (`DELTA_MAX_DIRECTORIES`,
40,000: twice the record cap, a supervisor limit that is not part of the contract), because the apply creates every missing one and
20,000 empty files with 99 new directories each would otherwise make about two million of them from a few megabytes. The caps are the
contract's (`DELTA_MAX_BYTES` 256 MiB, `DELTA_MAX_FILES` 20,000, `FILE_MAX_BYTES` 64 MiB, `PATH_MAX_BYTES`), enforced while reading and from the
declared size BEFORE a payload is read, so a lying length allocates nothing; a breach is `DeltaTooLarge` (`directories` for the
directory cap).

**A read-only phase.** For a session whose write scope is not `cwd` (`none`: `sessionrecord.WRITE_SCOPES`; any other value is
treated the same, fail closed) the delta MUST be empty: the header and no record at all. Any other byte is `scope`, whatever it
holds. An empty delta applies to nothing and the worktree is not even opened.

**What the tree check refuses** (read-only, before any write): a link or a file where a directory has to be (so nothing is written or
deleted THROUGH a link that an earlier worker planted in the worktree, and a `delete` below a link is refused too), unless the
delta deletes that file or link first (`delete d`, `file d/x`); a directory where a file or link goes, unless the deletions empty
it (`clash`: a directory that still holds other files cannot be replaced); a `delete` that names a directory (`unlinkat` without
`AT_REMOVEDIR`; the delta never removes a directory it did not empty). A `delete` of a path that is already gone is fine, so
**a delta applies twice with the same result** (a re-run after a crash). Directories are implied by paths: the deletions remove the
directories they emptied (the bundle cannot carry an empty one either) and leave every other directory alone.

**The tree can still change between the steps** (a worker's leftovers are not the only writer in a real deployment). Nothing is
written through what changes: the directories are opened with `O_NOFOLLOW` again at the apply, the worktree path is open by
descriptor, and a rename never follows. Such a swap is caught by the tree check (`DeltaRejected`) or, after it, by the apply
(`DeltaApplyFailed`). `apply_delta(..., checkpoint=callable)` calls `callable("validated")` and `callable("checked")` between the
steps; the tests use it to plant a link in the gap, and it has no other use.

**Conflicted merge round trip.** `MERGE_HEAD` and the unmerged index live only in the supervisor's git directory (the pod's copy has
no merge state, slice 9a). The staged copy carries the conflict-marker FILES; the pod edits them; the delta returns them; and the
**unchanged** `Runner.commit()` finds `MERGE_HEAD`, runs `refuse_conflict_markers` on the applied files and concludes the merge
(a test runs it for real: a resolved file makes a two-parent merge commit, a file that still has markers is refused and the head
does not move). Submodule files travel as ordinary tree files and come back as such; the pointer files (`.git`) of the worktree
and of a submodule are never in a delta and are never touched.

**The pod side (slice 10b)** is `bundle.produce_delta` (section "The pod worker, part B"): the same `Writer`, the same `walk`, the delta that
the validator above reads; the report that goes with it names `declared_records` and `declared_bytes`.

**The delta's integrity is the caller's.** The wire format has no trailer or count, so a stream cut at a record boundary reads as a
valid shorter delta (a test pins this). The relay bounds a stored delta by the length of the request that carried it; the manager
that wires this in (slice 12b) should also pass `declared_records` and `declared_bytes` from the pod's report: a stream that does
not have exactly those numbers is `declared`. Without them a cut at a boundary is applied as the shorter delta.

**Limitations that are decisions.** A case-only rename (`delete a`, `file A`) is refused as a `collision` (the supervisor's file system
is case-sensitive, but the two names are one on every other one, and the safe answer is to refuse and let a person look). A link
target is checked lexically, and is sound because it may only go up first and then down (see the stream check): a chain of links
inside the tree is not followed to see where it ends, and it does not need to be. A build that installs a
large dependency tree into the worktree can exceed the delta caps (20,000 records, 256 MiB, 64 MiB a file); that fails the session
with `delta too large`, it is never truncated, and the owner decides per repository later (UNVERIFIED on real repositories).

**Slice 9b, where each check runs.** L: `PYTHONPATH=src python3 -m unittest tests.test_delta_malicious tests.test_bundle` (real
directories and real local git repositories; the malicious-payload suite checks that after every rejected delta the whole tree, the
directory a link points at and the scratch directory are byte-identical: type, mode, inode, modification time and content). C: compile and the
unit tests on Python 3.11 to 3.13 (`.github/workflows/ci.yml`). P: not applicable. V (live only): real-repository delta sizes and times.
The suite proves the supervisor's side of the validator; it does not prove the pod side.

**Measured worst cases (slice 9b, one machine, local tmpfs).** The work follows the paths times their depth and the directories the
delta names, not the depth squared: the deletions' parent directories are made once for the tree check and the apply together (a walk up
stops at the first one already known) and the folding of a name is C code (an ASCII name is only lowercased; any other name goes
through a translation table). 20,000 `delete` records with 99 components each (a 4.4 MB stream) are refused as `delta too large`
(`directories`) after 1 s and 155 MB (before the directory cap: 21 s and 1 GB, and the same records as files made about two million
directories); 20,000 `delete` records with 4096-byte paths (a 76 MB stream) are refused after 0.7 s (before: 106 s); 20,000 deletions of
files in one directory 98 levels deep (the most the cap allows, 8 MB of stream) apply in about 9 s and 245 MB, nearly all of it the
`O(records x depth)` system calls that resolve every path from the root one component at a time. The validation holds the paths of all
records in memory (`O(records x depth)`), which is what the 245 MB is.

**UNVERIFIED (slice 9b).** Throughput and size of a real delta (only synthetic and small ones were run, and the worst cases above on
one machine only); behaviour of
`O_TMPFILE` for the private copy and of `renameat` on the production file system (the tests ran on a local ext4 or tmpfs
directory); the git version behaviour on the production image; that a Linux file system with a name limit other than 255 bytes
does not fail part-way; the `.gitmodules` of a worktree a worker touched (it is an ordinary file to this slice, which does not read
its content; whether a later supervisor step ever acts on a new submodule URL in it is the open question of slice 3, above).

## UNVERIFIED

* Every PROVISIONAL number: the resource classes and `emptyDir` limits, the byte, file, time and
  thread caps, the sidecar uids (they only have to avoid 10001, 10002 and group 10010) and the timing
  constants. A measurement on a real deployment replaces them.
* That the label prefix `factory.internal/` needs no domain the owner controls (it is private to this
  cluster).
* That `tflint-ignore` applies to a multi-line `locals` entry in CI (the fallback is above).
* Everything about Kubernetes, Karpenter, Cilium and the registry that this page assumes: nothing in
  these slices touches a cluster.
* Slice 3: whether Claude's own sandbox already blocks rewriting a worktree's `.git` pointer (the pointer check does not
  rely on it); behaviour on the production git version (checked here with git 2.43; a pointer written with relative paths,
  `worktree.useRelativePaths` in newer git, would not match and fails closed; the image pins git only when the
  `GIT_VERSION` build argument is set, and `core.fsmonitor=false` means "off" from git 2.36, an older git would run the word
  as a command, which is harmless but not what is intended); that the settings above are every way a worker's tree can
  make a supervisor git call run a program. What is proven here (git 2.43, real repositories, a marker file): the
  fsmonitor and hook settings, and the clean filter of a nested repository reached by `status`, `diff` and `add` (the
  tests in `tests/test_gitsafe.py`, each with a plain-git control that fires). Filter drivers of the supervisor's own
  repository come from the common configuration, which workers cannot write, but the `.gitattributes` that select them are
  in the worktree; other ways in (`submodule update` on a tree a worker has touched, other git versions, any git command
  that starts a child git in a gitlink and is not in the allow-list of commands that never read the worktree) were not
  exercised.
* Slice 9a: real-repository bundle size and time, the shallow-clone depth review diffs need, git version behaviour on the
  production image, the exact set of differences between the pod's git answers and in-process mode (staged work, the
  configuration, refs) and the two separate spool accountings of a staging and the relay (details in "The bundle and ship-in
  staging").
* Slice 9b: real-repository delta size and time, the memory of a worst-case delta, `O_TMPFILE` and `renameat` on the production
  file system, and that a worker's `.gitmodules` is not read by any supervisor step (details in "The file delta: validation and
  safe apply").
* Slice 10a: the wrapper `command` and `tini` path in the image; that the canary destinations are the complete list of what must be
  unreachable; the kubelet-injected API server variables with `enableServiceLinks: false`; the answers of a real API server to the
  unauthenticated probe; the owner and mode of the token file and `lchown` to uid 10002 under `hostUsers: false` (details in "The
  pod worker, part A").
* Slice 10b: `Popen(user=, group=, extra_groups=[])` and `execnocaps` as container root under `hostUsers: false`; SIGTERM through `tini` and
  whether 15 s of the 30 s grace suffice for a real tree; the memory guard, `claude` on `PATH`, a process that left the agent's group,
  pipe-to-relay throughput, the duplicate chunk after a lost answer, the real delta read time and memory, `O_TMPFILE` in `/tmp`, the
  provisional numbers of the run, and exit code 0 for a posted result (details in "The pod worker, part B").
* Slice 6: that a real API server accepts the pod as written; that Karpenter and NAP honour the
  required anti-affinity when they provision a node and that the scheduler keeps one repository per
  node (a run with two repositories at once on a real cluster; until it has passed, no more than one repository may use the pod backend); that the wrapper command (`/usr/bin/tini` and
  `/opt/factory/bin/python` in the image) exists in a pod; that the kubelet retries a Secret that is
  created a moment after its pod; and every statement about the real admission policy (above).
* Slice 5: that the previous image tolerates the new `pod` key in every reader (not run: the test only pins that this
  tree's readers never subscript it); recovery, `purge` and `status` with a real launcher outage, and what a real
  launcher answers for a pod that is Pending, terminating, evicted or replaced (the fake's answers are assumed
  shapes); that the pool loop can wait for a `terminate` call (its bound is the launcher client's, slice 11: 45 s, longer than
  the 10 s abort deadline, which stops waiting and leaves the pod recorded);
  and the cost of the reader: `status` asks it once per running pod job, and the dashboard asks for a report on every
  refresh. The end of a session forgets a pod record only after the reader said `dead` or the injected terminator
  returned without raising (`podhandle.confirm_gone`): a terminator that returns for a delete that was merely accepted,
  or a reader that says `dead` for a pod that still runs, is trusted, because nothing in this tree can check it; the
  launcher client of slice 11 does neither (its `terminate` waits for the pod to be gone, its `reader` calls a
  terminating pod `alive`), against the fake API only. Slice 12b must install the reader and the terminator before any
  pod is recorded: without them a recorded pod is kept for ever (fail closed). Not run live.
* Slice 14: that an unset variable gives no plan line and that both cluster attributes are "update in
  place", not "replace" (provider source reading only; a `terraform plan` with the read-only identity and a scratch deployment
  decide); the Cilium downtime and the NAP-before-Cilium order; whether `network_policy` is needed; the
  `karpenter.sh/v1` and `karpenter.azure.com/v1beta1` field names against the installed definitions
  (`expireAfter` under `spec.template.spec` or `spec.disruption`, `podPidsLimit`, `startupTaints`);
  whether the kubernetes provider's `kube_config` stays known at plan time after an in-place cluster
  update; that the plan identity can read the new custom resources; and the quota figures, which were
  read once (2026-11-09), together with the allocatable CPU and memory of the default `Standard_D4s_v6`
  size and whether the NodePool `sku-name` requirement accepts it.
* Slice 15a: that an unset `pod_backend_enabled` gives no plan line (only a `terraform plan` with the read-only identity decides; a text test pins the
  gating, and the data of the agent ConfigMap was rendered off and on by a provider-free scratch module, not by a plan);
  that Cilium enforces `ipBlock.except` on AKS (the Cilium documentation says an `ipBlock` cannot select pod or node
  addresses, so the three cluster ranges in the exception may be redundant or handled as identities) and whether
  `network_policy` must be set; that the DNS rule matches after the service address is translated; that the API
  server's public address is refused without a credential (decided by the canary and by a request from a pod of a real cluster); the
  `exclude_namespaces` key spelling and the agent's restart behaviour on AKS, and the documented default list, which was
  read in Microsoft's data-collection ConfigMap article and not tested on a cluster; that a worker pod's output is
  really absent from Log Analytics; that the read-only plan identity can read a namespace, a service account and a
  network policy through the built-in reader role (the custom plan role is changed in slice 15b, not here).
* Slice 15b: that the `kubernetes_manifest` provider path accepts the `admissionregistration.k8s.io/v1` kinds; that a real API
  server accepts and evaluates the CEL as the offline model does (type check against the pod schema, cost, function semantics:
  only a run against a real API server (the admission matrix on kind, or a recorded run on a scratch deployment) shows it); differences of CEL semantics and of the pod schema between the kind
  version and AKS; fields added to the pod API after the version the deny-lists were written for; that the server's
  defaulting is what `tests/test_policy_cel.py` emulates; that ephemeral containers are refused on create before the policy runs;
  the ordering of the parameter ConfigMap update during an automatic image promotion (a bounded window in which the launcher's pods
  are denied is possible); the data-action names and the plan role's coverage of the two admission kinds.

* Slice 18: see "The pod pool, Checks and UNVERIFIED": `flock` across processes on the production state volume, the default ceiling,
  burst and interval against the real NodePool limit and provisioning rate, the phase budget as an upper bound of a session's cost, and
  the launcher's `info` count under a slow API server.

* Slices 19 and 20: see "Pod-side liveness, the orphan sweep and the deploy gate, Checks and UNVERIFIED": the heartbeat window and its relation to the
  wrapper's interval, the order in which Kubernetes and the relay learn of a finished session, the real listing for the states the table guesses, the
  deletion of a Secret whose pod is gone, the cost of a sweep pass, and a deploy that waits for pods while a launcher is down.

* Slices 16 and 17: see "Terraform C", Checks and UNVERIFIED: a default plan with no line for the Deployment and the pool `factory`; the
  Role and the RoleBinding through the provider and the plan identity; the projected token, the `emptyDir` owners and the init container's
  capabilities under `hostUsers: false`; the webhook's skip annotation; a worker pod's path to the relay Service; the sidecar sizes; and the
  manager taint (CSI driver tolerations, whether `node_taints` replaces the pool).

* Slice 12b: see "The pod backend, Checks and UNVERIFIED": that slice 10b reads the manifest's `run` section and posts the report as this slice
  expects; every threshold of the wait loop; the delta a killed pod does not return; the launcher's answers for a pod it reaped; the breaker marker and
  the spool as the supervisor's uid sees them; `FACTORY_DATA=/data` in the manager's main container.

* Slice 11: see "The launcher and its client, Checks and UNVERIFIED": `SO_PEERCRED` across containers that share an
  `emptyDir` under `hostUsers: false`; the kubelet's retry delay for a pod whose Secret appears a moment later; the
  garbage collection timing of a Secret with an owner reference; every Kubernetes shape the fake assumes.

## Slice 1: API errors and the stop-the-world breaker

Slice 1 (issue #146) adds behaviour that already helps the in-process mode and needs no pod: the
manager now tells a failed session's API error apart and stops starting work when the service says
the spend limit is reached. It is default off in effect: with no API error the argv, the state and the
behaviour are unchanged, and nothing has to be switched on.

* `src/factory/apierror.py` classifies the final `result` event of a failed session (or, when there is
  none, a bounded tail of its log) into one closed code, in the backend's `collect` (see "The backend
  seam"): `spend_limit`, `rate_limit`, `overloaded`, `auth`, `rate_limit_unknown`, `other` or `none`. The rules are data, an unrecognised shape is never a
  spend limit, and the classifier is total: a malformed value degrades to `none`, so the failure being
  reported is never replaced. Free-form agent text is never copied anywhere.
* `src/factory/stopworld.py` and `operations.py` hold the breaker. A `spend_limit` trips it at once,
  `auth` after 2 and `rate_limit_unknown` after 3 within 5 minutes; `rate_limit`, `overloaded` and
  `other` only count in `factory_api_errors_total`. A trip sets its own marker (not the manual trigger
  pause that the deploy gate clears), drains every run in progress once, never retries and never
  resumes by itself; an Operator clears it with `POST /spend-breaker`. `trip(kind)` is generic so that
  a later slice can add the isolation failure of a pod.
* The breaker pass reads each run with `status --json --offline`: a local state read, no GitHub call.
* Documented in `docs/capacity.md` (the breaker), `docs/operations.md`, `docs/manager.md` and
  `docs/metrics.md` (the `code` label and the two counters).

Checks for this slice: L (the unit tests `tests.test_apierror`, `tests.test_stopworld`,
`tests.test_metrics` and the deploy-gate end-to-end test) and C; P is not applicable and V is none.
Everything the classifier knows about the real `claude` error shapes is contract-tested against
assumed shapes. UNVERIFIED: the real shape of a spend-limit, an authentication and a rate-limit
error, and that a rate-limit error reaches the `result` event at all. Until a person has recorded
them from a real run (slice 22), an unrecognised 429 is `rate_limit_unknown` and trips after 3 in 5
minutes rather than at once. Setting the workspace spend limit in the Anthropic Console stays a human
action.

## Slice 7: the Kubernetes client, its fake and the pod status classifier

Slice 7 (issue #145) adds what the launcher of a later slice needs from the Kubernetes API and
nothing more: `src/factory/kube.py` (a standard-library client), `src/factory/podstatus.py` (a
table-driven classifier of a pod's state) and, for the tests, `tests/fake_kube.py` (an in-memory fake
API server). Nothing imports the two modules yet, nothing launches a pod and the backend stays off by
default. **Everything here is contract-tested against assumed shapes: no cluster was consulted.** The
paths, the `DeleteOptions` body, the `Status` answers, the pod statuses of every failure mode and the
way an admission denial reaches the client are the documented Kubernetes API as the author knows it,
checked only against the fake over a local HTTP server on `127.0.0.1` with a fake clock and an injected
sleep. A recording of the real shapes from a cluster (slice 22) replaces the assumptions.
The new modules are not imported by the manager, so the running behaviour does not change; building an image from them and promoting it
are the operator's own pipeline's work (the reference deployment's pipeline is not shipped here).

### The client (`kube.Client`)

Six operations, in ONE namespace (the worker namespace of the contract by default), and no others:
there is no `watch` (callers poll), no `pods/log`, no `exec` and no Secret read, which is also what the
launcher's Role will allow. The fake answers 403 to any of them, as a Role without that verb would, and
a test asserts the client never asks.

| Method | Request | Returns |
|---|---|---|
| `create_pod(manifest)` | `POST /api/v1/namespaces/<ns>/pods` | the pod as the API answered (it has the uid that `podspec.build_secret` needs) |
| `get_pod(name)` | `GET .../pods/<name>` | the pod; `NotFound` when it does not exist |
| `list_pods(selector)` | `GET .../pods?labelSelector=k=v,...&limit=500`, following `continue` for at most 10 pages | the pods whose labels equal every pair of the selector mapping (equality only, validated) |
| `delete_pod(name, grace_seconds)` | `DELETE .../pods/<name>` with `DeleteOptions` (`gracePeriodSeconds`, default the contract's 30, `propagationPolicy: Background`) | the answer of the API |
| `create_secret(manifest)` | `POST .../secrets` | `{"name", "uid"}` only: the API echoes the Secret's data back, and the client drops it |
| `delete_secret(name)` | `DELETE .../secrets/<name>` | the answer of the API |

* **Address and credentials.** `Client.in_cluster()` reads the API host from the in-pod environment
  (`KUBERNETES_SERVICE_HOST` and `KUBERNETES_SERVICE_PORT`, the names the contract's network canary
  already uses), the CA from `/var/run/secrets/kubernetes.io/serviceaccount/ca.crt` (read once) and the
  token from `.../token`. The contract has no entry for those two standard paths, so `kube.py` holds
  them. **The token is re-read on every request and on every retry** (projected tokens rotate). Plain
  `http` is refused for any host but loopback, so a token is never sent in the clear.
* **The token file** is read as a regular file of at most 16 KiB: opened without blocking (a FIFO in its
  place cannot hang the read), checked with `fstat`, and its text must be bearer-token characters (so a
  newline cannot become a header). Symbolic links are followed on purpose: a projected token is a link
  into a `..data` directory that the kubelet swaps atomically, so refusing links would refuse every real
  token; the directory is a read-only mount of the launcher container, not a path another process can
  plant. A token that cannot be used is a `ConfigError` before anything is sent.
* **Transport.** `http.client` and `ssl` (the layer beneath `urllib`: `urllib` has one timeout and the
  contract has two, and it would follow a redirect with the token). A redirect is never followed; a
  response over 8 MiB is a `BadResponse`. Certificates and host names are verified against the CA file
  (`ssl.create_default_context(cafile=...)`: only that CA is trusted); a verification failure is a
  `ConfigError` and is never retried, and nothing is sent to that peer.
* **Timeouts and the deadline of an answer.** The connect timeout is 5 s and the read timeout 30 s (the
  contract's numbers), and **the read timeout is per socket operation, not for a call**: alone it would not
  stop a server that sends a byte every few seconds. So each attempt also has a DEADLINE for the whole
  answer (status line, headers and body): 60 s after the connection was made and never later than 120 s
  after the call began, checked on the injected clock before every receive (a receive waits at most the
  smaller of the read timeout and the time left); past it the attempt ends as a timeout (a transport
  failure, see below). **Connecting and sending the request (at most 1 MiB) are not interrupted by the
  deadline**: they have only the per-operation socket timeouts, so one call can run past 120 s by what
  those two phases of its last attempt take. A host name (not an IP address) is resolved without any
  timeout, which does not matter in the pod, where `KUBERNETES_SERVICE_HOST` is an address (ASSUMED).
* **Errors**, all with fixed texts (a message never holds a response body, a pod spec or a token):

| Answer | Error | Retried |
|---|---|---|
| 2xx | the parsed JSON object. Anything else is `BadResponse`: for a create that still means the API answered 2xx, so the object exists and the caller gets it by name | no |
| 409 | `AlreadyExists` | no |
| 404 | `NotFound` (after creation that means the pod is **gone**, never succeeded) | no |
| 401, 403 | `ConfigError`: a fatal configuration error (a 403 whose Status is not a ValidatingAdmissionPolicy denial: the token is refused) | no |
| 403 that is a ValidatingAdmissionPolicy denial (`ValidatingAdmissionPolicy '<name>' with binding '<name>' denied request:`), 422 | `Denied` with `rules`: the admission rule ids and nothing else | no |
| 429, 5xx | `Unavailable` once the retries are used up | yes |
| other (400, 405, 3xx ...) | `Rejected(status)` | no |
| connection failure, a malformed answer on the wire (a bad status line, a negative or huge chunk size, bytes that are not HTTP), or a 2xx body that ends before its `Content-Length` | `Unavailable` | see below |

* **Retry.** At most 3 retries, after 1, 2 and 4 s (the contract's numbers; a `Retry-After` of a 429 may
  lengthen the wait up to 30 s), and no new attempt starts past 120 s into one call. The sleep and the clock
  are injected, so no test sleeps. A connection that failed before the request was sent is retried for every
  method; a network failure after a `POST` was sent is NOT retried (the pod may exist) and
  `Unavailable.outcome_unknown` says so, and so does a `POST` that was answered 5xx other than 503 on ANY attempt.
  The flag is sticky across the attempts: a 500 followed by 429s, 503s or a refused connection until the retries run
  out is still "may have been applied", and only a POST whose every attempt was a 429, a 503 or an unsent
  connection failure says "was not applied". A retried `create_pod` that answers 409 after an attempt that may have landed (a 5xx other than 503, a
  sent and dropped connection) raises `AlreadyExists` with `outcome_unknown=True`: the object may be the caller's own. A 409 on the
  first attempt, or after attempts that were certainly not applied (429, 503, never sent), has `outcome_unknown=False`. A malformed answer, a 2xx body cut short by a clean close (the bytes read are fewer
  than the `Content-Length`) and an attempt that passes its deadline are transport failures like a dropped
  connection: retried for a `GET` or `DELETE`, and for a `POST` that was sent `outcome_unknown`. A body that is
  delimited only by the close of the connection (no `Content-Length`, not chunked) cannot be checked this way and
  is parsed as it came: a cut-off JSON object is a `BadResponse`.
* **A denial.** The mirror of the policy (`podspec.check_policy`, `check_secret_policy`) answers with
  rule ids; the real policy of a later slice must put each id in square brackets in its message (for
  example `[host-path]`), and the client keeps exactly those ids that `podspec.POLICY_RULES` knows, in the
  contract's order. Until that slice agrees, a denial from a real server may carry an empty `rules` tuple.
  That bracket form is an assumption of this slice (UNVERIFIED).
  First live run (a local kind v1.36.1 cluster): both assumptions were wrong in the same way. The policy's
  `reason` is `Forbidden`, so a denial is a **403**, not a 422 (the API server's own validation, for example `hostNetwork` with
  `hostUsers: false`, is the 422), and each message carries `[rule:<id>]`, not `[<id>]`. The client had read every 403 as refused credentials
  and found no id; it now reads a 403 whose Status names a ValidatingAdmissionPolicy as `Denied` (status 403) and takes the id from either
  form. `tests/fake_kube.py` answers the real shape.
* **Logs.** The only log line is the retry notice, `kubernetes <METHOD> request failed (<status or
  network or timeout>), retry <n> of <max>`: words and numbers only.

### The fake (`tests/fake_kube.py`)

An in-memory API server on `127.0.0.1` for exactly the six operations, like `tests/fake_blob.py`:
label selectors (`=`, `==`, `!=`, `in`, `notin`, existence) with pages; owner reference garbage
collection (deleting a pod deletes the Secrets it owns, and a Secret whose owners are all absent is
collected); a scriptable pod lifecycle driven by the injected clock (`Step`, `SCENARIOS`: `succeeds`,
`unschedulable`, `image_pull_backoff`, `config_error`, `exit_nonzero`, `oom`, `oom_while_running`,
`evicted`, `deadline`, `kubelet_rejected`, `disruption_target`, `node_deleted`, `karpenter_eviction`);
an optional admission hook that runs `podspec.check_policy` for pods and `podspec.check_secret_policy`
for Secrets and answers 422; and fault injection (a status with headers and body, a dropped connection, a
hang). A Secret whose pod is deleted goes with it; a pod that is deleted while running terminates for its
grace period first.

### The classifier (`podstatus.classify`)

`classify(pod)` answers an `Outcome` (`state`, `detail`, `row`, `final`, `exit_code`) whose `state` is one
of the closed set `pending`, `unschedulable`, `starting_error`, `running`, `succeeded`, `failed_exit`,
`oom`, `evicted`, `deadline`, `gone`. It is table-driven: `podstatus.TABLE` lists the rows, the first row
that matches wins (so the order is part of the table), and each row names the Kubernetes field it reads.
**Every row is marked ASSUMED** until slice 22 replaces it with a recorded fixture. A test checks the
`reads` column against what each matcher really reads, in both directions.

| Row | State | Final | Reads (ASSUMED) |
|---|---|---|---|
| `gone` | `gone` | yes | response: 404 NotFound on get pod (`classify(None)`) |
| `succeeded` | `succeeded` | yes | `status.phase` is `Succeeded` |
| `eviction-reason` | `evicted` | yes | `status.phase` is `Failed` and `status.reason` is `Evicted`, `Terminated`, `NodeShutdown` or `Shutdown` |
| `deadline-reason` | `deadline` | yes | `status.reason` is `DeadlineExceeded` |
| `disruption-condition` | `evicted` | yes | `status.conditions[]` with `status.conditions[].type` `DisruptionTarget` and `status.conditions[].status` `True` |
| `oom-killed` | `oom` | yes | `status.containerStatuses[]` with `status.containerStatuses[].state.terminated` whose `status.containerStatuses[].state.terminated.reason` is `OOMKilled` (`status.containerStatuses[].state.terminated.exitCode` kept) |
| `failed-exit` | `failed_exit` | yes | `status.phase` is `Failed` with a terminated container in `status.containerStatuses[]` (`status.containerStatuses[].state.terminated`, and its `status.containerStatuses[].state.terminated.exitCode` kept) |
| `failed-never-started` | `starting_error` | yes | `status.phase` is `Failed` and no container terminated (a kubelet admission failure) |
| `terminating` | `evicted` | yes | `metadata.deletionTimestamp` is set while the pod is not over |
| `start-error` | `starting_error` | no | `status.phase` Pending or Running with a `status.containerStatuses[].state.waiting.reason` of `ImagePullBackOff`, `ErrImagePull`, `ErrImageNeverPull`, `InvalidImageName`, `CreateContainerConfigError`, `CreateContainerError`, `RunContainerError` or `CrashLoopBackOff` |
| `unschedulable` | `unschedulable` | no | `status.phase` is `Pending` with a `status.conditions[]` whose `status.conditions[].type` is `PodScheduled`, `status.conditions[].status` is `False` and `status.conditions[].reason` is `Unschedulable` (or `SchedulingGated`) |
| `running` | `running` | no | `status.phase` is `Running` |
| `pending` | `pending` | no | `status.phase` is `Pending` |
| `unrecognised` | `pending` | no | anything else: not a dict, no status, a phase the table does not know (`status.phase`) |

Rules of the classifier:

* **Nothing is ever `succeeded` by default.** A pod that cannot be read is `pending` with the detail
  `unrecognised`, never `succeeded` and never `running`; the classifier never raises.
* **`gone` is not `succeeded`.** A `NotFound` after creation is `classify(None)`: a pod that finished and
  was deleted, a node that was deleted and an eviction that completed look the same from outside, and
  only the result the session posted to the relay can show that a session succeeded. A poll is
  `try: classify(client.get_pod(name))` and `except NotFound: classify(None)`.
* **No timers.** The classifier has no clock: the scheduling timeout, the pull and config error window and
  the orphan silence belong to the caller. `unschedulable` is normal for about 100 s while NAP provisions a
  node. `starting_error` from `start-error` can recover (a pull that backs off and succeeds); the rows
  marked final cannot change.
* **No free text.** `detail` is a fixed word or a Kubernetes reason that the table lists; the exit code is an
  integer 0 to 255 (a boolean is not one); messages, names and unlisted reasons are ignored.
* **`terminating` cannot tell who deleted the pod.** A pod with a `deletionTimestamp` is `evicted`, whether
  Karpenter evicted it or the launcher deleted it; the caller knows when it asked for the delete.
* **Only the pod is read.** No events and no logs: a pod that waits for a Secret that does not exist stays
  `pending` and only a timer of the caller ends it.

### Checks and UNVERIFIED

Checks for this slice: L (`PYTHONPATH=src python3 -m unittest tests.test_kube tests.test_podstatus`) and
C; P is not applicable (no infrastructure change); V is none. Live (not run): real pod, event and
log JSON for each failure mode on a real cluster, and that the launcher token can create and delete a
pod in the worker namespace.

UNVERIFIED: every real pod and event shape, in particular the Pending reason at a NodePool limit and the shape of a Karpenter-initiated eviction (the `eviction-reason`, `disruption-condition`
and `terminating` rows are guesses); that the real admission denial puts the rule ids in square brackets;
that a native Role works next to Azure RBAC for Kubernetes; that the projected token path is the standard
one in the launcher container; that a real API server answers 409, 404, 401, 403 and 422 as the fake does; what a real API server or an
intermediary puts on the wire when it misbehaves (the malformed and the cut-short answers come from a
raw-socket test server, not from observation); and the HTTPS path against a real API server (the tests use a
stand-in connection class and a throwaway CA file: no network, no handshake with a real certificate).

## The launcher and its client (slice 11)

Slice 11 (issue #145) adds the program that holds the only Kubernetes token and the client the supervisor
uses to reach it: `src/factory/launcher.py` (`python -m factory.launcher`) and
`src/factory/launcher_client.py`. Nothing imports either module yet, nothing starts the launcher and no pod
can be launched by the running manager: the backend is off by default. **Everything here is
contract-tested against assumed shapes**: the launcher talks to the in-memory fake of the Kubernetes API
(`tests/fake_kube.py`, slice 7) through the real client of `kube.py`; the sockets and `SO_PEERCRED` are
real, inside one process and one container. No cluster was consulted.

### Who may talk to whom

* **Directory.** `/run/factory` is the root of an `emptyDir` shared with the main container. It is not the
  launcher's to set up (it is non-root with every capability dropped): the init container of a later slice makes
  it owned by the launcher uid, group 10001, mode 0750. `main` **refuses to start** when the directory is not
  owned by its own uid, is writable by group or others, is a symbolic link, is missing, or when anything that is
  not the launcher's own stale socket sits at the socket path (a stale socket of its own is replaced; anything
  else is left alone). The socket is created 0660. The launcher also refuses to run as root.
* **Server side.** Authentication is `SO_PEERCRED`: the uid of the connecting process must equal the configured
  allowed uid (the supervisor's, 10001). Any other peer is closed without a byte being read, without an answer and
  without taking a thread. There is no bearer token anywhere, so no environment deny-list has to learn a name.
* **Client side.** The client authenticates the server **before it sends a byte**, because a launch request
  carries the one-time relay token and the shared directory is next to a container that runs other code. It
  checks with `lstat` (never following a link) that the socket directory is a real directory owned by the
  configured launcher uid that neither group nor others can write to, and that the socket is a socket owned by
  that uid; it connects, reads `SO_PEERCRED` of the connected socket, and requires the same uid. Any mismatch is the
  fixed error `launcher identity not trusted` with nothing sent (the tests plant a listener and assert it read
  zero bytes). A launcher that is merely not there yet (no directory or socket, connection refused) is retried
  three times with the contract's backoff; an impostor is never retried.

### The protocol

One request per connection: one line of JSON, one line of JSON back. The schema is closed; an unknown or missing
field, a duplicate key or a wrong type is `invalid` and reaches no API. The image digest, the namespace and
every cap come only from the launcher's own environment, never from a request.

| `op` | Fields | Answer |
|---|---|---|
| `launch` | the closed request of `podspec` (`session_id`, `repository`, `run_id`, `job_id`, `attempt`, `phase`, `resource_class`, `timeout_seconds`, `manager_instance`) and `token` (the one-time relay token) | `name`, `uid`, `timeout_seconds` (as applied), `clamped` |
| `status` | `name`, optional `uid` | `state` (the table of `podstatus`), `detail`, `final`, `exit_code`, `present` (the pod is still in the API) and `terminating` (it is being deleted and is not over, so its containers may still run, although `final` is true for it); another uid under that name is `gone` and not `present` |
| `terminate` | `name`, optional `uid` | `deleted`; the pod and its Secret are deleted, a missing pod is success. It answers when the API has accepted the delete: a running pod is still there for its grace period (the client waits, below) |
| `list` | none | the labelled pods: `name`, `uid`, `state`, `final`, `terminating` (slice 20: a final pod that is only being deleted still runs for its grace), `manager`, `created`, `deadline` (validated fields only) |
| `info` | none | caps, live count, in-flight launches, deny state, counts of pods the launcher itself deleted |
| `deny` | `reason` (a fixed lowercase word) | the launcher refuses every launch until it restarts |

Failures answer `{"ok": false, "error": <code>}` with a code of the closed set `invalid`, `too_large`, `timeout`,
`busy`, `denied`, `capacity`, `unavailable`, `rejected` (with the admission rule ids), `conflict`, `failed`. A
request is bounded in size (16 KiB) and in time (5 s for the whole line, however slowly it trickles in); at most
16 connections are served at once and the 17th is answered `busy` at once (a peer of another uid is dropped in the accept
path before it takes a thread or is told `busy`, even at the bound: a test pins it); every thread is joined with a bound
when the server closes. An `accept()` error is told apart: the listener closed by `close()` ends the accept loop, any other
`OSError` (a descriptor limit, an aborted connection) logs the fixed line `accept failed`, pauses half a second and keeps
serving, so the launcher is never left up and answering nobody.

### What the launcher enforces

* **Caps.** A maximum number of live pods (counted by listing the pods labelled `role=worker`, plus an in-process
  count of launches that have been admitted and are not yet in a listing, so two launches at the same moment
  cannot both take the last slot: every admission is made under one lock, and the in-flight count is read before the
  listing it is added to, so a launch that finishes while another one lists is counted by one of the two), a per-repository cap (default 4), the deadline clamp (default and at most
  14400 s: a longer request is reduced and the answer says so, a request below 60 s is `invalid`), the class enum
  (the contract's classes, narrowed by `FACTORY_LAUNCHER_CLASSES`) and the phase enum. A launch over a cap answers
  the fixed code `capacity`: the supervisor waits and asks again, it is not a failure. A launcher that cannot list
  answers `unavailable` and creates nothing.
* **Launch order and compensation.** Build the pod (`podspec.build_pod`, the only way a pod is built here), create
  it, create the Secret with an `ownerReference` to the pod (`podspec.build_secret`), return name and uid. Any
  failure after the pod may exist deletes the pod and then the Secret by their derived names (a 404 is fine). The
  pod is not deleted when the API certainly refused to create it (a pod of that name is then someone else's: a
  409 on a first attempt is the code `conflict`), and is deleted when the outcome of the create is unknown (a dropped
  connection, a 500, and a 409 that follows a 5xx the first attempt got: the retry found the pod the first attempt
  created; the pod and its Secret are deleted and the reply is `unavailable`, a failed create to try again). The relay token and the Anthropic key are written to the API and nowhere else: never logged, never
  returned, never in an error text (a poisoned-environment test).
* **The scheduling timeout** (`Launcher.sweep`, every 15 s, on an injectable clock): a pod that stays Pending
  (waiting for a node, its Secret or its image) for 600 s, a pod that outlives its deadline plus the grace (the
  kubelet enforces `activeDeadlineSeconds` only once it has accepted the pod, so a pod stuck Pending has no
  deadline), and a pod in `ImagePullBackOff`, `ErrImagePull` or `CreateContainerConfigError` for 120 s (from when
  the launcher first saw the error) are deleted by the launcher. The creation time is derived from the launcher's
  own clock (the deadline annotation minus `activeDeadlineSeconds`), not from the API server's timestamp. The
  launcher remembers, in memory and bounded, why it deleted a pod, so `status` answers `unschedulable`,
  `starting_error` or `deadline` for it instead of `gone`; a launcher that restarts forgets and answers `gone`.
  A sweep that cannot list pods deletes nothing.
* **`deny(reason)`** fails closed until restart. Status, terminate, list and info keep working, so pods can still
  be read and deleted.
* **No child process, no outbound call except to the API server, fixed log lines.** A test reads the syntax tree.
  Log lines are fixed words and numbers: no request text, no pod name, no manifest.

### The client and the seam of slice 5

`LauncherClient` has `launch`, `pod_state`, `delete_pod`, `list_pods`, `info` and `deny`, with timeouts on every
connect, send and read, a reply read against a total deadline and bounded to 1 MiB, and only the connect retried
(a request that was sent is never repeated: a launch may have been applied). `reader(handle)` answers `alive`,
`dead` or `unknown` for the pod handle of slice 5 (`dead` is `gone` or a final state, `unknown` is anything the
launcher could not answer, never a guess; a pod that is `terminating` is `alive` even though `final` is true for it,
because the deletion is accepted at once and the pod then runs for its termination grace) and `terminate(handle)`
deletes the pod and **returns only once the pod is gone** (a pod already gone is success): after the launcher accepts
the delete it polls `status` once a second until the pod is no longer `present`, within one bound for the whole call
(`TERMINATE_WAIT_SECONDS`, the 30 s grace and a 15 s margin; each request is also bounded by what is left of it), and
raises `Unavailable` when the pod is still there at the bound or the launcher cannot answer. The supervisor's abort
waits only `podhandle.ABORT_TIMEOUT` (10 s) for it: a termination that is still under way then leaves the pod recorded
(fail closed), and the next call asks again. A `status` reply without `present` and `terminating` is a `BadReply`
(the reader is then `unknown`). The launcher's `delete_pod` of the client alone returns when the delete is accepted.
`Capacity` is its own exception, distinct from a refusal.
`tests/test_launcher.py` runs `podhandle.pod_status`, `podhandle.terminate_pod`, `podhandle.confirm_gone` and
`podhandle.install` against a real launcher over a real socket with these two callables (class `PodHandleSeamTests`), so the
seam is pinned against the real handle, not a stand-in.

### What the launcher expects of the Terraform slice that runs it

The environment names are the contract's (`ENV_LAUNCHER`): the image, the maximum number of live pods and the
relay URL, the manager address and the API-server host are required (no default: a missing value refuses to
start); the per-repository cap, the clamp, the allowed uid, the classes and the socket path have restrictive
defaults. Two points the spec left open and this slice fixes: **`FACTORY_LAUNCHER_CLASSES` is a comma-separated
list of class names** (the sizes come from the contract through `podspec`, so the environment only says which
names are enabled), and **`FACTORY_LAUNCHER_NAMESPACE` must repeat the contract's worker namespace** (`podspec`
builds pods only there; a different value refuses to start). The Anthropic key is `ANTHROPIC_API_KEY`, one
`secretKeyRef`.

### Checks and UNVERIFIED

Checks for this slice: L (`PYTHONPATH=src python3 -m unittest tests.test_launcher`) and C; P is not applicable
(no infrastructure change); V is none. Live (not run): a process with uid 10002 in the manager's main container
cannot connect to the socket, cannot create, rename or unlink anything in `/run/factory` and cannot replace the
socket; the launcher token can create and delete a pod and a Secret in the worker namespace and cannot get or
list Secrets or exec.

UNVERIFIED: what `SO_PEERCRED` reports for a peer in another container that shares an `emptyDir` under
`hostUsers: false` (the tests meet a foreign peer only by configuring another allowed uid); how the kubelet behaves
for a pod whose Secret is created a moment after it (the retry delay; the pod is `pending` meanwhile and the 600 s
timeout is the bound); when the garbage collector removes a Secret after its pod; that the Pending reason at a
NodePool limit is one the `podstatus` table reads as `pending` or `unschedulable`; that deleting a Running pod
leaves it terminating for its grace period (the launcher reports the remembered reason meanwhile, and `terminating` true;
the fake API is the only evidence, and so is the 45 s bound of the client's `terminate`); that the
standard in-pod token and CA paths are the ones the launcher container sees.

## The pod pool: ceiling, pacing and reserve-on-admit (slice 18)

Slice 18 (issue #146) adds `src/factory/podpool.py`, the guard the pod backend of slice 12b asks **once per session, before it
asks the launcher for a pod**. It launches nothing, imports no launcher or Kubernetes code, reads the environment only in `from_env`, `parse_ceiling` and `pool_directory` (the switch, the ceiling, the data directory) and is
imported by nothing that runs today: **delivered, not wired in**. With `execution = "inprocess"` (the default) none of it is
reached, and `capacity.py` (the in-process pod budget, `FACTORY_MAX_TOTAL_WORKERS`) is unchanged and not involved: a session in
a pod uses none of the manager pod's cpu or memory, so the two budgets never mix. **Everything here is contract-tested against
assumed shapes** (a fake launcher count, a temporary directory, a fake clock, real child processes for the locks); no cluster was
consulted.

### What one admission checks

`PodPool.admit(request)` answers at once with a **reservation** or one **refusal code**; the checks run in this order and the first
one that fails is the code.

| Code | When | Waits? |
|---|---|---|
| `breaker` | the stop-the-world marker (`stopworld`, slice 1) exists, readable or not | no |
| `invalid` | the argument is not a `Request` | no |
| `duplicate` | a live reservation of the same session (repository, run, job, phase, attempt) exists | no |
| `budget` | `spent + estimate > run_budget_usd`: even an otherwise idle run cannot fit another session | no |
| `budget_held` | it fits only when other live reservations of the same run are released | yes |
| `repo_cap` | the repository holds `PER_REPO_CAP` (4) reservations | yes |
| `ceiling` | live reservations, or the launcher's live plus in-flight pods when larger, have reached the ceiling | yes |
| `pacing` | the bucket (`PACING_BURST` 2, one token per `PACING_INTERVAL_SECONDS` 15) is empty; the refusal carries `retry_after` seconds | yes |
| `queued` | admissible, but waiters of a repository holding fewer slots (the oldest first) go first | yes |
| `unavailable` | the ledger cannot be locked within 5 s or written, or the launcher's count cannot be read | no |

A refused request consumes no pacing token, no estimate and no slot: the token is spent only when the reservation is granted, and the
reservation file is written only after every check passed, under one lock. (A refusal for `repo_cap`, `ceiling`, `pacing` or `queued` does
write or refresh the request's waiting ticket in `queue/`, and the bucket file is rewritten when it was corrupt or stamped in the future;
neither takes anything from anyone.) The estimate of a request is the phase's `--max-budget-usd` (`phases.py`), `spent`
the run's cumulative cost as the supervisor already computes it (`Runner.run_cost`), and `budget` the repository's `run_budget_usd`
(None: no limit, the estimate is still recorded). Amounts are exact: whole micro-dollars, the estimate and spent rounded up, the
budget down, so `0.1 + 0.2` fits `0.3`. A reservation of another run, or of another repository with the same run id, never counts.

### The ceiling

`FACTORY_MAX_POD_SESSIONS` (`podcontract.ENV_MANAGER["max_pod_sessions"]`) is the NodePool ceiling local of Terraform A
(`floor(limits_cpu / node_vcpu) * pods_per_node`, 8 by default) and is read only by `podpool.parse_ceiling`. It must be a plain
integer `0` to `1024`: unset, empty and anything else (`8.0`, ` 8`, `-1`, a trailing newline, a non-ASCII digit) is
`ConfigRefused` (`ceiling_unset`, `ceiling_invalid`), because Terraform always sets it and an unset value is a misconfiguration, not a
default. `0` is valid and refuses every admission (a person pauses pod sessions without switching the backend off). The ceiling
is the manager-side half of the cap: the launcher still refuses a launch over `FACTORY_LAUNCHER_MAX_PODS` (`capacity`), so a pool
that miscounts cannot put more pods on the cluster than the launcher allows.

The pool counts live reservations. A pod whose supervisor crashed holds no reservation but still occupies a node, so when a
`live_count` callable is given (`podpool.launcher_live_count(client)` reads `LauncherClient.info()`: `live + inflight`, the number the
launcher's own cap counts) the pool uses `max(reservations, launcher count)`, **not their sum**: the pods of the reservations are in
the launcher's count too. A reply with a missing, negative, boolean or non-integer number, an error or a launcher that could not
list (`live: null`) is `unavailable`, never zero. A ceiling lowered below the live count refuses until enough sessions end.

### Pacing

One token bucket for the whole cluster, in `pacing` in the ledger directory, changed only under the ledger lock: `PACING_BURST` tokens,
one more per `PACING_INTERVAL_SECONDS`, never more than the burst. The clock is injectable (`clock=`). A bucket file that is missing is
full (the first launch ever), one that is corrupt is empty and refills (fail closed); a clock that goes back neither refills nor
freezes the bucket (the stamp is rewritten to now). Whether a pod launch rate of one per 15 s after a burst of 2 is the right smoothing
for node provisioning is **UNVERIFIED** (the numbers are the PROVISIONAL `PACING_*` of the contract).

### Reservations never leak

A reservation is a file under `$FACTORY_DATA/.capacity/pods/` (`FACTORY_CAPACITY_DIR/pods` when that is set), held under `flock` by the process that was
granted it; `admit.lock` serialises every change (so two supervisors cannot both take the last slot), `pacing` is the bucket and `queue/` holds the
tickets of waiting requests (the machinery of `capacity.SlotPool`, so the fair-share rule is `capacity.admission_order`).

* **A crash**: the kernel drops the lock when the process dies, and the next scan, by any supervisor or by `sweep()`, removes the file nobody holds. `sweep()`
  returns what it removed (`why` is `dead` or `expired`, `entry` the repository, run, job, phase, attempt and `requested`); `requested: true`
  means a launch request may have created a pod nobody tracks, which the orphan sweep of slice 20 finds by its labels. A manager restart
  therefore starts with an empty pool whatever the ledger held, and recovery calls `adopt(request)` for each pod it finds still running.
* **A wedged holder**: every reservation expires at the pod's `activeDeadlineSeconds` plus the termination grace and the launch grace (`Request.hold_seconds`, at most
  `MAX_HOLD_SECONDS`, 14850 s), when the kubelet and the launcher's own sweep have ended the pod. An expired file is removed even
  though its holder is alive; that holder's later `requested()` raises `Lost`, and its later `release()` leaves a newer reservation of the
  same session alone: it checks that the file is still the one it created and unlinks it in one step **under the ledger lock**, so a sweep and a
  new admission of the same session cannot fall between the check and the unlink. A release that cannot get the lock within `LOCK_WAIT_SECONDS`
  leaves the file where it is and closes its descriptor; the file is then unheld and the next scan removes it as `dead`.
* **An exit path**: `release()` is idempotent and thread-safe, the reservation is a context manager, and an object dropped without a release is released by the collector.
  The lock is on a close-on-exec descriptor, so a worker process the supervisor started never keeps a dead supervisor's slot alive (a test kills a
  supervisor whose child outlives it).
* A held file that is not a valid entry (corrupt) **still counts as one slot** and expires one maximum hold after its last write: an unreadable
  entry must never free a slot. The same holds for a file that exists but cannot be opened (a mode change, a symlink caught by `O_NOFOLLOW`, any
  open error but "gone") and for one that is not a regular file: each counts as one slot (`repo` None) and is never removed by the pool, so an
  operator removes it. A scan that cannot open a file for want of descriptors (`EMFILE`, `ENFILE`) refuses the admission (`unavailable`), because
  no honest count can be made.
* `cancel()` takes the descriptor and the right to a refund out of the reservation in one step, under the reservation's own lock: two cancels
  refund one token, and a `requested()` that arrives while a cancel is refunding raises `Lost` instead of sending a launch whose token was given back.

### It fails closed

`capacity.py` fails open (a local guess must not stop work); this pool fails closed, because a wrong "yes" starts a billed node. A state
directory that cannot be used, a lock not taken within `LOCK_WAIT_SECONDS`, a launcher count that cannot be read, a bad ceiling and an invalid
request all refuse and start nothing; `from_env` with the switch on and no usable guard raises `ConfigRefused` (`ceiling_unset`,
`ceiling_invalid`, `state_dir`) instead of running unguarded; `state_dir` also covers a missing `FACTORY_DATA` when only `FACTORY_CAPACITY_DIR` is set,
because the stop-the-world marker is read from `FACTORY_DATA` and a pool that never consults the breaker is not built, and `hold(None, ...)` raises `Refused`. With `FACTORY_POD_BACKEND` not exactly `on`,
`from_env` returns None and reads, writes and creates nothing. Log lines are fixed phrases and integers, printed once per process where they could repeat
(a test reads the syntax tree); no repository, run, job, token or issue text reaches a log or an error.

### The call contract for slice 12b

```
pool = podpool.from_env(env, live_count=podpool.launcher_live_count(client))     # once; None while the switch is off
request = podpool.Request.for_launch(launch_request, estimate_usd=policy.budget_usd,
                                      spent_usd=runner.run_cost(), budget_usd=cfg.run_budget_usd)
with podpool.hold(pool, request, seconds) as reservation:   # Refused: final code; Timeout: a waiting code persisted
    ...build the spec, mint the session id and token, stage the bundle...
    reservation.requested()                                  # immediately BEFORE the launch request; Lost/Unavailable: do NOT launch
    handle = client.launch(launch_request, token)            # a launcher `capacity` answer: leave the block (the slot and the estimate
                                                              # are released, the token stays spent: the request was sent), ask again later
    ...run, collect...
# leaving the block (any way) releases the slot and the estimate
```

* Call `admit` before anything is staged or minted; a `Refused` costs nothing to undo. `reservation.cancel()` (slot, estimate **and** the pacing token
  back) is only for a launch that certainly did not happen (nothing was sent): it refunds only while `requested()` was never called.
* The reservation lives as long as the session may: it is held for the whole block, so the estimate stays reserved until the session's cost is in
  the run's `spent` (the runner records the cost, then the block ends).
* After a restart call `pool.adopt(request)` for each recovered live pod: it never refuses for the ceiling, budget, breaker or pacing, takes no token, and
  is `duplicate` when the session is already counted.
* A final refusal (`budget`, `breaker`, `duplicate`, `invalid`, `unavailable`) is not polled; the supervisor treats `budget` like the existing
  `run_budget_usd` drain and `breaker` like the paused trigger. The waiting codes are retried by `hold`, which sleeps `retry_after` or `POLL_SECONDS` (never
  more than `MAX_POLL_SECONDS`, so a waiting request outlives no ticket) and withdraws its ticket on a timeout.
* `PodPool.snapshot()` is the read-only view for `status` (counts, holders, tokens, reserved dollars, waiting tickets); it changes no reservation, bucket or ticket and
  removes nothing, but taking the ledger lock creates the `pods` directory and `admit.lock` when they do not exist. Nothing calls it yet.

### Checks and UNVERIFIED

Checks for this slice: L (`PYTHONPATH=src python3 -m unittest tests.test_podpool`) and C; P is not applicable; V is none. The tests cover the request, the
ceiling and its edge cases (0, 1, lowered below the live count, the launcher's count above the reservations), the per-repository cap, pacing on a fake
clock (burst, refill, cap, refund, clock going back, corrupt bucket), the budget (exact arithmetic, held versus final), fairness, duplicates, adoption, crash
recovery with real killed child processes, expiry, races between threads, objects and processes, fail-closed behaviour, `hold`, and the syntax tree of the
module (standard library plus `capacity`, `stopworld` and `podcontract` only, no process, no socket, fixed log lines).

UNVERIFIED: that `flock` on the state directory behaves across processes on the production volume as it does on a local file system (`capacity.py` relies on
the same assumption and it is no more proven for the pods ledger); that the kernel's drop of the lock on death is seen through the volume by every
supervisor; that the default ceiling (8), the burst and the interval fit the real NodePool limit, node provisioning rate and the Anthropic rate limit; that
the phase's `--max-budget-usd` is a fair upper bound of what a session costs (a session that overshoots it by the model's own accounting is not stopped
by this pool); what `LauncherClient.info()` returns for a launcher whose API server is slow (the pool refuses); whether a release that waits for the ledger lock (up to `LOCK_WAIT_SECONDS`, 5 s) on a busy
production volume stalls a supervisor's exit path noticeably (a collector-driven release does the same); whether descriptor exhaustion (`EMFILE`) is ever seen in the manager
process (it refuses admissions until it clears); and that a reservation of a pod whose
supervisor crashed is reported to the orphan sweep (the slice 20 caller does not exist yet).

## Pod-side liveness, the orphan sweep and the deploy gate (slices 19 and 20, issue #147)

Slice 19 (`src/factory/podliveness.py`) is what the supervisor may conclude about a pod session without trusting the pod's text. Slice 20
(`src/factory/podsweep.py`, hooks in `src/factory/operations.py` and `src/factory/manager.py`, `tools/infra_deploy_gate.py`) removes what a dead
supervisor or a cut-off launch leaves behind, makes restart recovery WAIT for a pod instead of failing on it, and teaches the deploy gate to wait for
running pods. **Delivered, dormant by default:** `operations` imports `podsweep` (which imports `podliveness`) for its hooks, but `Manager.pods` is
`None` until the pod backend (slice 12b) sets it, so nothing sweeps, `GET /repos` has no `worker_pods` key, no repository can record a pod handle, and the in-process mode is unchanged. Everything
here is **contract-tested against assumed shapes**: hand-built launcher listings and state files, a real spool directory in a temporary directory, and
one class per module that runs the real launcher over a real unix socket against the in-memory fake of the Kubernetes API (`tests/fake_kube.py`) on one
fake clock. No cluster was consulted; nothing sleeps.

### Pod-side liveness (slice 19)

Three sources, weighed by `podliveness.assess(relay, pod, now, created)`:

| Source | What it gives | Who writes it |
|---|---|---|
| the relay's files (`podliveness.read_relay`) | the token state, the record's `deadline`, `last_heartbeat` (epoch seconds, written with the relay's own clock when a call arrives), the `revoke` and `released` markers, the fixed `reject` code | the relay and the supervisor, never the pod's text: the body of a heartbeat is not read, so a pod chooses only whether to call |
| Kubernetes' account of the pod | the launcher's `status` answer (`LauncherClient.pod_state`): one of the closed states of `podstatus`, `final`, `present`, `terminating` | the API server, read through the launcher |
| the clock | the supervisor's own, against the deadline and the contract's windows (`HEARTBEAT_SECONDS` 30, `ORPHAN_SILENCE_SECONDS` 300, `SCHEDULING_TIMEOUT_SECONDS` 600, `PULL_ERROR_SECONDS` 120, `LAUNCH_GRACE_SECONDS` 120) | no one |

**The verdict** is `Verdict(state, reason, action, silence, reject)`, every field from a closed set. States: `starting`, `running`, `terminating`, `silent`,
`expired`, `revoked`, `finished`, `released`, `lost`, `unreadable`. Actions: `wait`, `collect` (only for `finished`), `terminate`, `fail`, `none`.
Reasons: `result`, `collected`, `heartbeat`, `not_heard_yet`, `waiting_for_node`, `age_unknown`, `never_started`, `never_heard`, `no_heartbeat`,
`deadline`, `revoke`, `launcher_unreadable`, `relay_unreadable`, `no_session_record`, `terminating`, and the words of `podstatus.STATES` (`pending`,
`unschedulable`, `starting_error`, `running`, `succeeded`, `failed_exit`, `oom`, `evicted`, `deadline`, `gone`) for a session whose pod is over without a result.

| Situation (first row that matches wins) | Verdict |
|---|---|
| the supervisor's `released` marker | `released`, `none` |
| the relay state is `finished` (a `reject` code, if the relay cut the session off, comes with it) | `finished`, `collect` |
| revoked (marker or state) | `revoked`, `terminate` while the pod may be there, `fail` when it is gone |
| `expired` state, or `now >= deadline` of the record | `expired`, the same actions |
| Kubernetes could not be asked, or answered something that is not the closed shape | `unreadable` (`launcher_unreadable`), `wait` |
| the relay has no record (the spool was replaced: every call of the pod answers 410) | `lost` (`no_session_record`), `terminate` / `fail` |
| the relay's files could not be read | `unreadable` (`relay_unreadable`), `wait` |
| the pod is over for Kubernetes (final, not merely terminating) and the relay has no result | `lost` (reason: the Kubernetes state), `fail` when gone, `terminate` when the object is still listed |
| the pod is terminating | `terminating`, `wait` |
| a pod waiting for a node, a Secret or an image, no heartbeat, inside `SCHEDULING_TIMEOUT_SECONDS` + `LAUNCH_GRACE_SECONDS` (`PULL_ERROR_SECONDS` + the grace for an image error) | `starting`, `wait` (`age_unknown`: the creation time is not known, so no window is measured) |
| the same past the window | `silent` (`never_started`), `terminate` |
| the later of the last heartbeat and the creation is `ORPHAN_SILENCE_SECONDS` ago or more | `silent` (`no_heartbeat`, or `never_heard` when there never was one), `terminate` |
| otherwise | `running` (`heartbeat`, or `not_heard_yet` before the first one), `wait` |

**The rules** (each has a test that fails when it is removed):

* **A heartbeat is evidence of life, never of death and never of success.** It does not outrank Kubernetes saying the pod is over, it never makes a
  session `finished`, and no verdict is "dead": only the launcher (`podhandle.pod_status`) may call a pod dead. Silence can lead to a termination
  request (`enforce`) and never to a pod record being forgotten, so the fail-closed property of slice 5 is untouched.
* **Only the relay can say a session finished**, and it is read AFTER Kubernetes (`observe`): the wrapper posts its result and then exits, so a pod
  that is `succeeded` with no result is `lost`, and a result that lands between the two reads is seen, not lost (a test makes the pod "finish" inside the
  Kubernetes read).
* **What cannot be read is `unreadable` and nothing is terminated on a guess.** A missing record is a different fact (`lost`), a read error is not.
* **Silence is measured from the later of the last heartbeat and the creation**, to the second (`>=` the window). A heartbeat dated more than
  `FUTURE_SKEW_SECONDS` (60) ahead of the clock is ignored, so a clock jump cannot keep a verdict `running` for ever; the deadline ends every session
  whatever the heartbeat says, so a pod that heartbeats for ever is bounded.
* **`enforce(verdict, handle, spool, terminate)`** is the one function that acts. For `terminate` it writes the `revoke` marker FIRST (the pod's next call
  answers 410 and a result in flight is refused), then calls `podhandle.terminate_pod` (which returns only once the pod is gone) and reports whether that
  was confirmed; a marker that cannot be written does not stop the termination. Every other action touches nothing.
* **Nothing prints, logs, reads the environment or opens a socket**; a test reads the syntax tree.

`Probe(spool, client.pod_state).assess(handle)` is `observe` bound to one spool and one client. **For slice 12b:** poll `assess` for each running pod
session, `collect` on `finished`, `enforce` on `terminate`, and treat `lost`, `silent`, `expired` and `revoked` as the end of the session without a
result (the job fails or is retried as for any failed session); `unreadable` and `starting` are waits.

### The orphan sweep (slice 20)

A pod belongs to the job whose state records its `pod` handle, and to nobody when no job does. `podsweep.read_recorded` reads every started run's
`state.json` (opened without following a link, regular files only, at most 64 MiB) and answers who owns which session **and whether that answer is
complete**: one unreadable state file, one `pod` record that does not validate, a `jobs` that is not a mapping, **a job status that is not a plain word**
(`[a-z_]{1,32}`: missing, non-string, upper case, hyphenated or too long is unknown, never "not running"), a repository whose configuration does
not load, a cut run listing, **a missing `config_dir/repos` directory or a data directory without its `repos` directory** (a config mount or a
data volume that is not there is not an empty set of owners) makes it incomplete, and an incomplete answer is never a reason to delete anything. A
configured repository with no `runs` directory while the data volume is there has simply never run and has no pods.

`Sweeper.run_once(recorded, supervisor_live)` lists the pods through the launcher, THEN reads the owners (so a pod launched and recorded in between is
never both unlisted and unrecorded), decides with the pure `plan`, and acts. A pod that is already terminating is never touched. The reasons (closed set,
`podsweep.REASONS`):

| Reason | Meaning |
|---|---|
| `unrecorded` | no job records the pod, it is older than `LAUNCH_GRACE_SECONDS` (by the launcher's own creation time; a pod whose age cannot be read is never judged by age), no launch of this process holds it (`podsweep.protect(session)`) and **its relay record is not live** (below) |
| `finished` | the pod is over (a final state, not terminating) and no running job can still collect it |
| `stale_record` | a job that is NOT running still records the pod (the end of a session keeps the record when the termination was not confirmed): deleting it lets the next read say `dead`, so recovery goes on. It has the same protection as `unrecorded`: the pod must be older than `LAUNCH_GRACE_SECONDS` (an unreadable age is never judged), no launch of this process holds it (`podsweep.protect(session)`) and its relay record is not live, because a session that no supervisor runs (the `factory preflight` probe records a pod under a job that is not `running`) looks exactly like a stale record while it is in use |
| `supervisor_gone` | a RUNNING job records the pod and its run has had no live supervisor for `ORPHAN_SILENCE_SECONDS` by THIS sweeper's clock, counted from the first pass that saw it (a manager restart starts the count again, which only delays; a supervisor that comes back resets it) |
| `overdue` | the pod is past its deadline plus the termination grace plus the launch grace and is not terminating: the second line behind the launcher's own sweep, and the only reason that needs no owner |
| `unrecorded_session` | a relay record (a spool directory, not a pod): no job records the session, no pod of that name is listed, the relay session is over (finished, expired, revoked, released or past its deadline), the directory has been quiet for `LAUNCH_GRACE_SECONDS`, the listing is complete (shorter than the launcher's 1000-pod limit) and the owners were read in full |

**The relay record is the cross-process hold.** `unrecorded`, `finished` and `stale_record` never apply to a pod whose relay record is LIVE: `podsweep.read_sessions`
marks a session `live` when its record is readable and not ended (no terminal state, no `revoke` or `released` marker, `now < deadline`) or cannot be read, and
`plan` skips such a pod (counted as skipped). The sweep runs in the manager and the sessions in other processes (the `factory plan` child records no job in any
`state.json`, a `factory preflight` probe records its pod under a job that is not `running`, a supervisor may have restarted), so the process-local `protect`
cannot cover them; the record can: the supervisor writes it before it launches, refreshes its deadline just before the launch request, and ends it with the
`released` marker (after `revoke`, unless the result was delivered). A record that ended, was released, expired or is missing holds nothing, so the pod of a
session whose owner is gone is still swept, and `overdue` needs no owner at all; a record is live at most until its deadline (the scheduling window, the time
limit and the margin), so the cost of the hold is that a truly orphaned pod that has a record is deleted at its deadline, not 120 s after its creation. When the
spool is configured but cannot be read, any record may be live: no pod without a running owner is deleted (`overdue` excepted). `supervisor_gone` is not held
by the record: a RUNNING job's pod whose supervisor has been gone for `ORPHAN_SILENCE_SECONDS` has nobody to collect it. Tests: `tests/test_podsweep_crossprocess.py`
runs the sweeper in a SEPARATE Python process (empty `protected()`) against the spool of a real `PodBackend` session for a run session, a plan session
(no job), a preflight session (job not running) and a pod that is already over, and in every record state.

A pod is removed through the launcher's `terminate` (the pod and its Secret); the session's relay record is REVOKED first, so a pod that is still talking
is cut off, and the delete is accepted, not awaited (a running pod stays for its termination grace and is then `terminating`, which the next pass leaves
alone). An `unrecorded_session` is revoked, its derived pod name is asked to be deleted without a uid (the launcher deletes the Secret by its derived name
even when the pod is gone, which is the only way to clear a Secret that outlived its pod: the launcher's Role has no `secrets` list or get), and the
`released` marker is written so the relay frees the directory at once (the spool shares 16 GiB). A directory with no record, or one whose record cannot
be read, is left to the relay's own retention. Fixed counts only are logged (`pod sweep deleted=<n> released=<n> failed=<n> incomplete=<0|1>
unreadable=<0|1>`, and only when a pass did something); a name, a session id or a pod's text never is.

**Wiring.** `Operations.pod_sweep_pass` runs every `ORPHAN_SWEEP_SECONDS` (120) in its own thread, started only when `manager.pods` is a sweeper (the
existing test that pins the three threads of a manager without one is unchanged), after the restart pass. `Operations.recorded_pods` reads every
configured repository's runs; `Operations._supervisor_live` is true for a child this manager started or a live control record, an unreadable one counts
as live, and it does NOT probe the repository lease (a probe can make the `recover` of the same run fail with a busy lease). **The pod backend of slice
12b must** call `podsweep.protect(session)` from before it launches until after it has written `released`, **for the whole session of every pod it
launches, preflight sessions included** (the launch grace covers a launch that took a moment, not a collect that copies a large result, and not a
session of a job that is not `running`), and set `manager.pods = podsweep.Sweeper(client.list_pods, client.delete_pod, spool)`. A run directory
of a repository that is no longer configured is not read, so its pods look unowned and are swept: with no configuration nothing can resume them.
`protect` is a set in ONE process: it holds a session out of the sweep of the manager process only when the launch runs in that process. The session
of any other process (`factory plan`, `factory preflight`, a supervisor) is held by its live relay record (above), not by `protect`.

`launcher._listed` gained one field, `terminating`, because the listing's `final` is true for a pod that is only being deleted (it still runs for its
grace); a listing without the field (an older launcher) reads a final pod as terminating, so it counts as running and is never taken for a finished one.

### Recovery retry (slice 20)

`State.recover` refuses with `podhandle.PodStillLive` while a recorded pod is alive or cannot be read (slice 5). The CLI turns that into an ordinary
refusal, which the restart pass used to report as `failed=recover` and never ask again, leaving the job `running` until the next restart. Now
`operations.refusal_cause` knows the texts (the new cause `pod_live`, matched against the real messages of `podhandle.still_live` and
`recovery.MSG_POD_LIVE` by a test that builds them from the source) and the restart pass treats it as a wait:

* `restart repo=<r> run=<r> waiting=pod` is printed once and no job is marked `interrupted` (so no second session can start on the worktree);
* after the pass over every other run (and after `_recovered` is set, so the janitor and the trigger are not held back), the recovery thread asks again
  every `POD_RETRY_SECONDS` (30), takes the repository lease as at start, and resumes what recovers like any other run (`auto_resume_after_restart`, the
  trigger pause and the budget rules apply unchanged); the resumes the pause held back run as soon as it clears, not after the wait;
* the wait is bounded by `POD_RECOVERY_WAIT_SECONDS` = `DEADLINE_CLAMP_SECONDS` + `DEADLINE_MARGIN_SECONDS` + `TERMINATION_GRACE_SECONDS` (14 730 s, longer
  than any pod can run), then one warning per run and the run is left for the next restart; a shutdown ends it silently; any other failure on the retry is
  reported as at start and ends the wait for that run;
* a timeout is never read as a pod refusal, and a refusal that merely looks like one (extra text, a wrong prefix) is `other`.

A run without a pod handle never reaches any of this (no wait, no line). Until the pod backend installs a reader in the CLI process (12b), a recorded pod
reads `unknown` there, which is a wait, not a failure.

### The pod-aware deploy gate (slice 20)

A Terraform apply of `infra/` restarts the manager pod, which replaces the relay's spool: every running worker pod would then answer 410 and end with its work lost.
`Manager.worker_pods()` serves the census of `podsweep.census` (`{"live", "final", "unknown"}`: every listed pod that is not over counts as live, a
terminating pod included; the launcher that cannot be asked, an entry that is not a worker pod, a non-list answer or a listing as long as the launcher's
limit is `unknown`, never zero) as the top-level `worker_pods` key of `GET /repos`, **only when `manager.pods` is set**. `tools/infra_deploy_gate.py wait`
adds one active entry (`pods`) while `unknown` is not exactly `false` or `live` is not exactly the integer 0, so a malformed census is busy, never idle, and
the wait names `worker pods (<n>-running)` or `worker pods (unreadable)` in its summary on a timeout (no markup can reach it). A pod is waited for, never
drained. **A manager that does not report the key (every manager today, and any older image) adds nothing to the wait**: the gate runs from the repository
checkout against the deployed image, and waiting for what an older image cannot say would deadlock every deploy. The 30-minute bound is unchanged, so a
launcher that stays down fails the gate before `terraform apply`, which is the fail-closed answer; an operator brings the launcher up.

### Checks and UNVERIFIED

Checks: L (`PYTHONPATH=src python3 -m unittest tests.test_podliveness tests.test_podsweep tests.test_podsweep_recorded tests.test_operations_pods tests.test_infra_deploy_gate
tests.test_operations tests.test_launcher`) and C; P is not applicable (no Terraform); V is none. Live (not run): a pod's heartbeats and silence against a
real relay, the sweep against a real launcher with a pod left behind by a killed manager, and the gate against a manager with the backend wired in.

UNVERIFIED: that the relay's `last_heartbeat` is written on every call a live wrapper makes (slice 10b sets the heartbeat interval; the window of 300 s
is ten beats of `HEARTBEAT_SECONDS` and is PROVISIONAL); that Kubernetes reports `Succeeded` only after the relay has the result (the read order assumes it,
and a wrapper that exits first would turn a finished session into `lost`); what the real listing says for a pod that is Pending at a NodePool limit, an
evicted pod or a replaced one (the `unschedulable` and `evicted` rows of `podstatus` are guesses); that the launcher's `terminate` deletes the Secret of a
pod that is already gone when no uid is given (read from the launcher's code and exercised against the fake only), and that the garbage collector removes a
Secret with a dead owner as the fake does (a Secret cannot be listed, so nothing else would find an orphan one); the cost of a sweep pass (one `list`, one
read of every started run's `state.json`, a stat of every session directory, per 120 s) with real run counts; that `supervisor_status` of a run is a
cheap, lock-free read on the real disk; whether a stuck terminating pod (a finalizer, a lost node) is ever deleted by anything but the API server; that
`SO_PEERCRED` and the client's identity checks hold for the sweep's calls (slice 11's UNVERIFIED); and that a deploy that waits for pods is acceptable
to the owner when a launcher outage blocks it (the bound fails the gate before the apply, it does not apply anyway); that the relay record of a plan or preflight session
is live for as long as the session runs on the real spool (the sweep's hold is read from the record's state, markers and deadline: the unit and cross-process tests
use real records, but not a real relay on a real volume); that a never-run
repository, a briefly missing config mount or a purged runs directory behave as documented against the real volumes (a mount point that exists but is
empty cannot be told from a data volume with nothing written yet: only a missing `repos` directory is detected, so a mount that comes up empty still
reads as a complete, empty set of owners).

## The pod backend (slice 12b)

`src/factory/podbackend.py` is the second `AgentBackend` (section "The backend seam"). It inherits `LocalBackend.prepare` and `collect` and replaces
`launch`. It is selected only by `execution = "pod"` of the repository AND `FACTORY_POD_BACKEND=on` of the manager (section "The `execution` setting");
both are off by default, nothing imports the module while the switch is off (a test starts a fresh interpreter and checks `sys.modules` for the backend, the
pool, the sweep, the liveness module, the report, the launcher client, the relay and the bundle), and the in-process path is byte for byte what it was (the
golden captures of slice 4 and every existing test pass; the three callers pass the same arguments to `run_agent` for `inprocess`). **Wording:
contract-tested against in-memory fakes** (a fake of the launcher client, the real launcher over a real unix socket on the in-memory Kubernetes API, the
real `relay.Relay`, and, in `tests/test_podbackend_integration.py`, the REAL pod wrapper of slice 10b running the bundle this backend staged, against the
real relay over a socket, with a real child process for the agent); **no pod ran, no cluster was consulted.**

The backend was first written without reading the pod worker (10b), the pool (18), the liveness module (19) or the sweep (20), which were delivered "not wired
in". This section describes the integrated result; each of the four seams has tests that fail when it is removed.

### How a repository gets it, and what it needs

* **Selection.** `podexecution.backend_for(execution, env, job_id=..., attempt=..., base=..., spent_usd=..., budget_usd=...)` returns `None` for `inprocess`,
  `RefusedBackend` for the switch off (unchanged), and, with the switch on, `PodBackend.from_environment`. It refuses with `REFUSED_UNCONFIGURED` when the
  backend cannot be built (no scope, **no pool**, no manager instance, a bad socket path, a data directory that is not `/data`), never falling back to
  in-process. `agent_kwargs` takes the same keywords and still returns `{}` for `inprocess`. `Runner.invoke` and the preflight probe pass the job id, the
  attempt and the job's base commit; the planner passes the job id `planner` and attempt 0. `spent_usd` (a number, or a callable such as `Runner.run_cost`) and
  `budget_usd` (`run_budget_usd` of the repository) are the two amounts the pool checks the run's budget with; **no caller passes them yet** (see "What is NOT
  done here"), so the pool's budget check is idle and the runner's own `run_budget_usd` drain remains the budget guard.
* **Wiring, once per supervisor process.** `cli.run` calls `podexecution.activate(repository, run directory name)` before it takes the lease and undoes
  it in its `finally`. With the switch off it does nothing and imports nothing. With it on, `podbackend.activate` (1) remembers the repository and the
  run (a supervisor serves exactly one of each; an invalid id leaves the scope unset and the sessions refused), (2) **installs the launcher client's
  `reader` and `terminate` as the pair of `podhandle.install`**, so `State.recover`, `purge`, `recovery` and `status` can read a pod that an earlier
  process recorded (slice 5: without them a recorded pod is permanent, which is the fail-closed answer), (3) registers `podhandle.kill_live_pods` as a second
  lease-loss hook (`locks.register_kill_hook`, with `podhandle.ABORT_TIMEOUT` as its one deadline) and (4) **builds the process's one pod pool**
  (`podpool.from_env(env, live_count=podpool.launcher_live_count(client))`: `FACTORY_MAX_POD_SESSIONS`, a plain integer 0 to 1024, and the ledger under
  `FACTORY_DATA`). A pool that cannot be built (`ceiling_unset`, `ceiling_invalid`, `state_dir`) is **not** replaced by running unguarded: one fixed line is
  printed, the pool stays unset and every pod session is refused with `REFUSED_UNCONFIGURED`. An unusable socket path wires nothing and prints one fixed line.
* **The manager's sweeper.** `podexecution.sweeper(env)` (`podbackend.build_sweeper`) returns a `podsweep.Sweeper` over the launcher client's `list_pods` and
  `delete_pod` and the spool, or `None` unless the switch is exactly `on` and the socket path and the spool directory are usable; with the switch off it imports
  nothing. **The manager still has to set `manager.pods` to it** (its hook exists in `operations` and `manager`; the one-line change is a request, see "What is
  NOT done here"): until it does, nothing sweeps and `GET /repos` has no `worker_pods` key.
* **What the environment must hold** (the contract's names): `FACTORY_POD_LAUNCHER_SOCKET` (the client's default is the contract's path),
  `FACTORY_POD_SPOOL_DIR` (default `/spool`), `FACTORY_MANAGER_INSTANCE` (set by the manager for its children, slice 12a), `FACTORY_MAX_POD_SESSIONS` (set by
  Terraform, slice 17) and `FACTORY_DATA`, which **must be `/data`** when it is set: the pod lays a session's files out at the same absolute paths under its own
  `/data` (`podcontract.SCRATCH_DIR`), so a supervisor whose data directory is elsewhere is refused rather than shipped to a pod that would fail at its `manifest`
  stage; and it must be set for the pool, whose ledger and breaker directory come from it (slice 18: a pool with the switch on and no data directory is
  `state_dir`). A session whose worktree, prefix or read paths are not under the data directory is refused in `prepare`, before a file is written.
* **No worker identity of the repository is applied.** The pod runs `claude` as its own uid 10002. `prepare` therefore drops `[worker] uid` and `gid` of the
  request before it calls the local `prepare` (so nothing is shared or chowned locally) and does not require them when `FACTORY_REQUIRE_WORKER_IDENTITY`
  is set (`LocalBackend._require_identity` is the one hook, so the local backend's refusal is unchanged). For the same reason the memory guard's limit is the one
  the request names or **none**, never the one `capacity` derives from the manager pod's own resources (a share of THIS pod's memory is not the worker pod's
  limit); the worker pod's own cgroup limit (its class) still bounds it, and the guard still records the footprint.

### The integrated call sequence

| Step | What it does |
|---|---|
| `prepare` | Refuses a tripped breaker (below), a phase outside the contract and a directory outside the data directory, then the local `prepare`: the prompt, settings and system-prompt files and the throwaway home are written, nothing is started. |
| `launch`: protect | Mints the session id and holds it out of the orphan sweep (`podsweep.protect`) **until after the `released` marker is written**, on every path. |
| `launch`: pool | `podpool.hold(pool, request, seconds)` with the launch request's own repository, run, job, phase, attempt and time limit, the phase's `--max-budget-usd` as the estimate (read from the command line; none is `the pod session's budget could not be determined`), `spent_usd` and `budget_usd`. It is asked **before anything is staged or written**, so a refusal costs nothing to undo. A waiting refusal (`budget_held`, `repo_cap`, `ceiling`, `pacing`, `queued`) is waited on for the scheduling time (600 s) and then fails with `no pod capacity became available in time (<code>); nothing was started`; a final one (`budget`, `duplicate`, `invalid`, `unavailable`) fails at once with `the pod pool refused the session (<code>); nothing was started`; the breaker is the breaker's own text. The code is one word of the pool's closed set. |
| `launch`: record | Mints the one-time token (`sessionrecord.new_token`), builds the record (`sessionrecord.build`: the token's hash, the repository label, the write scope of the phase, the manager instance, the contract's caps) and writes it. The record's `deadline`, the expiry of the token, is the wall clock plus the scheduling timeout (600 s), the session's time limit (clamped to the contract's 14400 s) and the margin (300 s), **computed again, with a rewrite of the record, just before every launch request** (`_refresh_deadline`: staging and the capacity wait can each last 600 s and must not eat the session's time limit; nothing else in the record changes, and a record that cannot be rewritten fails the session before any pod). |
| `launch`: stage | `bundle.stage` of the worktree, the read paths and the manifest (below), with the job's base commit when it has one, in the job's own directory as scratch. A full spool (`SpoolFull`) is waited on, like capacity, for the scheduling time and then fails; a tree too large or a tree error (a link, a path that clashes) fails the session with a fixed text and the reason word. |
| `launch`: pod | The breaker is checked, then **`reservation.requested()` immediately before** `LauncherClient.launch` with exactly the closed request of `podspec` and the token (`Lost` or `Unavailable` from the pool: nothing is sent). `Capacity` from the launcher is **waited on** (every 5 s, for the scheduling time, then a fixed failure): the reservation (slot and estimate) is given back for the wait, the pacing token stays spent (the request was sent), and the pool is asked again. A refusal (`Rejected`, with only its closed code), an untrusted socket and a launcher that did not answer are fixed texts; a launcher that did not answer may have applied the launch, so the pod's derived name is asked to be deleted. |
| `launch`: bind | `sessionrecord.bind` with the name and uid the launcher returned, then the `PodHandle` (`launcher` word `launcher`, `created_at` the supervisor's wall clock) is **registered** (`podhandle.register`) and handed to `on_start` as `AgentHandle(pod=...)`, which the runner records beside `pid`. A failure to bind, or an exception from `on_start`, ends the session and deletes the pod. |
| `launch`: wait | Every 2 s: `podliveness.Probe(spool, pod_state).assess(handle)` (the launcher's account of the pod, THEN the relay's record: a pod that succeeded after the relay was read must not look like one that returned no result), the relay's state and heartbeat for the supervisor's own timers, and the verdict table below. A `terminate` verdict is carried out by **`podliveness.enforce`**: the `revoke` marker first, then the pod is deleted. |
| `launch`: end | Revoke (unless the result was delivered), delete the pod (unless `enforce` already did), copy the outputs, release the session, remove the home, release the pool's reservation: the release below. |
| `collect` | The local `collect`, plus `PodOutcome.failure`: a failure that no result event explains is raised with the usage of the transcript (when it has a result event) and the closed API-error code of `apierror`, so **a spend limit that ends a pod session reaches the breaker exactly like a local one** (`AgentError.api_error`, from the event or, with none, from the log tail). |

### The verdicts, and what each becomes

The time limit is measured from the moment the relay says the pod fetched its bundle (the session started), not from the launch: a pod that waits for a node
for a few minutes does not lose them. The wrapper enforces the limit itself and posts its result (`outcome` `timeout`); the supervisor's own backstop is the limit plus
`TIMEOUT_MARGIN_SECONDS` (120 s: the stop, the drain and the delta), so a normal timeout arrives as a result and keeps the edits. Two more bounds guard the rest: a pod that
has not fetched its bundle `SCHEDULING_TIMEOUT + LAUNCH_GRACE + RELAY_TRANSFER_TOTAL` after the launch (1620 s: a node, the image, the canary and the longest transfer the
relay allows), and a pod whose relay state is `fetched` or `streaming` and whose heartbeat has not changed for `ORPHAN_SILENCE_SECONDS` (300 s). A launcher that does not answer for 300 s ends the session too (a
result the relay already holds is still collected: an outage alone ends nothing).

**Liveness verdicts** (`podliveness`, slice 19) and what the backend does with each:

| Verdict | Becomes |
|---|---|
| `finished` (the relay holds a result) | collect: the next table; a `reject` code of the relay is the cut-off or the isolation failure |
| `revoked`, `expired` | `PodOutcome.failure` (`revoked`, `expired`); `enforce` when the pod may still be there |
| `lost`, reason `no_session_record` (the spool was replaced) | `the relay no longer has the pod session's record`; `enforce` |
| `lost`, any pod state (the pod is over and posted no result) | the table of Kubernetes states below; `enforce` when the object is still listed |
| `silent`, reason `no_heartbeat` | `the pod stopped sending heartbeats`; `enforce` |
| `silent`, reason `never_started` (still not running after the scheduling window) | `the pod did not start in time`; `enforce` |
| `silent`, reason `never_heard` | **not acted on**: silence is measured from the pod's creation, which cannot tell a pod that waited 6 minutes for its node from one that never started; the supervisor's own bounds (the fetch bound, the heartbeat after the fetch) decide it |
| `terminating` | waited on for its grace period and a margin (45 s): it may still post a result |
| `unreadable` (`launcher_unreadable`, `relay_unreadable`) | waited on, never acted on; after 300 s `the pod launcher stopped answering` or `the relay's session record could not be read` |
| `released` (someone else wrote the marker, the sweep) | `the pod ended without returning a result` |
| `starting`, `running` | the supervisor's own bounds above |

**What a finished session becomes** (`podreport.loads` on `out/report.json`; an invalid one is `the pod's result report is not valid` and nothing is applied):

| `outcome` of the report | Becomes | Same as `LocalBackend` |
|---|---|---|
| `exited`, delta fine | the structured result; the delta is applied with `declared_records` and `declared_bytes` of the report's `delta`; `Outcome.returncode` is the report's `exit_code` (a negative number is the signal that ended the agent), `footprint` the report's | success, or `agent exited N after a success result` (`-9` for a SIGKILL) |
| `timeout` | the delta is applied first (the edits stay in the worktree, as in process), then `AgentTimeout(spec.timeout)` | `AgentTimeout` |
| `memory_limit` | the delta is applied, then `exceeded`: `worker exceeded its memory limit: peak <exceeded_mb> MiB over <limit_mb> MiB` (the numbers of the report; the pod-level text when it has none), with the spend and the footprint kept | the memory-guard error |
| `terminated` (SIGTERM: the pod is being deleted or its deadline passed) | the delta is applied, then `PodOutcome.failure`: `the pod was stopped before the agent finished` (a retryable failure; the transcript's usage is kept) | `agent produced no result event` |
| `delta.status` `too_large` | `the pod's file delta was too large and nothing was applied`; the delta is **not opened** | (none) |
| `delta.status` `failed` | `the pod could not read its tree to return a file delta; nothing was applied` | (none) |
| `streamed` differs from the size of `out/transcript` or `out/stderr` | `the pod's output does not match what the pod reported sending`; nothing is applied | (none) |
| `dropped` true | an incomplete transcript: with no `result` event the local `agent produced no result event` (or `agent exited N`), with one a success | the same |

**What a pod that posted no result becomes** (Kubernetes' account, through the launcher's `status`; the relay's `finished` always wins, so a wrapper that exits 81 after a lost answer is still collected):

| Observed | Becomes | Same as `LocalBackend` |
|---|---|---|
| pod `failed_exit` with the code of a wrapper stage (`podworker.STAGES`: 64 to 81, kept equal by a test) | `the pod worker failed at its <stage> stage` (`key`, `spawn`, `result`, `gone`, `run`, `bundle`, ...: the stage word only, never what the pod said) | `agent produced no result event` |
| pod `failed_exit` with the stage `canary` (68) | an **isolation failure**, below, even though the wrapper could not post why (`the pod's network isolation check failed (unknown)`) | (none) |
| pod `failed_exit` with another code | `Outcome(returncode=N)`: `agent exited N; see ...stderr` | the same message |
| pod `oom` | `exceeded`: `worker exceeded its memory limit: the pod was killed at its standard class limit of 4Gi`, with the spend kept | the memory-limit error |
| pod `deadline` (the pod's `activeDeadlineSeconds`) | `AgentTimeout(spec.timeout)` after the release; no delta is returned for a pod killed this way | `AgentTimeout` |
| pod `evicted`, `gone`, `succeeded` with no result, `unschedulable` or `starting_error` after the launcher deleted it | `PodOutcome.failure`, one of the fixed texts of `podbackend.FAILURES`, with the usage and the API-error code of the transcript | `agent produced no result event` |
| a stop (`enforce_stop` deletes the pod) | the pod is `gone`: the failure above, and the runner turns any exception into `StopRequested` when the stop was recorded, as it does for a killed process | the same path |
| the relay's `preflight_failed` | **isolation failure**, below | (none) |
| a relay cut-off (`transcript_cap`, ...) | `the relay cut the pod session off (<code>)`, the code from the relay's closed set | `agent produced no result event` |

### The isolation failure and the breaker

A pod whose network canary failed (`preflight_failed`, which the relay turns into a finished session whose bundle was never served; or the wrapper's exit with its
`canary` stage when it could not post why) means a pod could reach what it must not, **or** that the canary itself could not run to a pass (the wrapper reports an
exception nobody classified against the stage it was in: a fail-closed reading, so a bug in the canary stops launches as loudly as a leak). The supervisor (1) asks the
launcher to `deny("isolation_failed")`, so it refuses every launch until it restarts, (2) trips the
stop-the-world breaker with the kind `isolation_failed` (`stopworld.Breaker(FACTORY_DATA).trip`: its marker is a file at the top of the data directory, which
every supervisor and the manager see, and the manager's pass drains the runs in progress), and (3) fails the session with
`the pod's network isolation check failed (<code>); ...`. The code is named only when it is one of `podbackend.ISOLATION_CODES` (a test keeps that set equal to what
the wrapper can post); anything else is `unknown`, because the code is a string a pod chose. Both steps are best effort and neither replaces the failure. While
the breaker is tripped, **no pod session starts**: `prepare`, the pool (`breaker`) and every launch attempt refuse with a fixed text, so a supervisor that is already running cannot add pods
after another one saw the failure. The breaker's own metric (`factory_stop_world_trips_total`) counts only the trips the manager's pass makes; this trip
is made by the supervisor, and the manager sees the marker.

### What the pod is given, and what it sends back

* **The bundle's manifest** (`meta/manifest.json`). `version`, `dirs` (the throwaway home and its config directory) and `files` are what part A of the wrapper lays out
  (the prompt at mode 0600, which only the wrapper reads, the settings and system prompt at 0640, and `<home>/.gitconfig` with `safe.directory`). The key **`run`** is
  for part B and is **exactly** `podworker.RUN_KEYS` (a test keeps `podbackend.RUN_KEYS` equal to it, and another runs the real `podworker.read_run` on the manifest
  the backend writes; the wrapper refuses a missing or an extra key): `argv` (the `claude` command line, the binary first), `env` (the non-secret environment additions of the
  `LaunchSpec`: `HOME`, `CLAUDE_CONFIG_DIR` and the three switches), `cwd` (the worktree), `prompt` (the path of the 0600 file), `timeout` (seconds, at least 60 and
  at most the contract's clamp, 14400) and `memory_limit_mb` (the request's limit, or null). The session's prefix, write scope and phase **do not travel**: the supervisor keeps them (the
  prefix for its own files, the write scope for `apply_delta`). No secret: the Anthropic key is the launcher's and reaches the pod only through the Secret, the token only through the launcher.
* **The report** (`out/report.json`, at most 64 KiB, posted with the delta) is read by `podreport.loads` and by nothing else: the closed schema of slice 10b (`version`, `outcome`,
  `exit_code`, `footprint`, `exceeded_mb`, `delta` with `status`, `records` and `bytes`, `streamed`, `dropped`; its footprint is the four numbers `memwatch` records: `peak_rss_mb`,
  `cpu_seconds`, `samples`, `limit_mb`). A report that is not exactly that fails the session (`ValueError("report")` is the only text), including the flat schema this slice
  first assumed. The table above says what each field becomes.
* **The delta** is applied only through `bundle.apply_delta(source, worktree, write_scope=<the phase's>, scratch=<the job directory>, declared_records=, declared_bytes=)`, with the report's
  `delta.records` and `delta.bytes`, and only when `delta.status` is `ok`: validated as a whole before the first write, a read-only phase must return the empty delta, a git path, a path
  outside the tree, a link out of it or a size that differs from the report changes nothing (`delta rejected`, `delta too large` or `delta could not be applied`, with the usage of the
  transcript kept). It is applied whenever a result was posted, **also for a session that timed out, ran out of memory or was terminated**, as a local session's edits stay in the worktree
  (the wrapper returns the agent's changes whatever its outcome). **A pod that is killed from outside (the kubelet's deadline, an eviction, a deleted node) returns no delta, so the
  worktree keeps what it had before the session**; in-process the edits made before the kill stay. That difference is a decision, not a bug, and the next attempt of the job starts from the
  unchanged worktree.
* **The outputs.** `out/transcript` and `out/stderr` are copied to `<prefix>.jsonl` and `<prefix>.stderr` (64 KiB at a time, within the contract's caps, opened relative to the
  session directory with `O_NOFOLLOW` and `O_NONBLOCK`, anything but a regular file refused). They are copied on **every** path, also after a kill, so that the classifier can read
  an API error from the tail of a session that never returned a result. Nothing from them is put in a message except by the local `collect` code that already filters it.

### The release, on every path

In this order, each step on its own (one failing does not skip the next, none replaces the error being raised): the `revoke` marker (unless the result was delivered), the
termination of the pod through the client (**it returns only once the pod is gone**, slice 11; skipped when `podliveness.enforce` already did it and confirmed it), the handle is
unregistered **only when that succeeded** (a failed termination leaves it registered, so the lease-loss hook can try again, and the recorded pod stays in job state, so recovery
refuses on it until the reader says `dead`), the outputs are copied, the `released` marker is written, the throwaway home is removed, and then the pool's reservation is
released (`cancel`, which also gives the pacing token back when no launch request was sent) and the sweep's protection ends. A pod whose handle was never made is deleted by its name. The relay token is a local variable of one function,
deleted after the launch request, never an attribute, never printed (the only `print`s of the module are the two fixed lines of `activate`).

### What is NOT done here

* **`manager.pods` and the run's budget are wired** (the integration pass; `tests/test_podexecution_wiring.py` pins the arguments): `Manager.__init__` sets `self.pods = podexecution.sweeper(self.env)`
  (a `Sweeper` with the switch on, `None` and no pod module imported with it off), and `Runner.invoke` and the preflight probe pass `job_id`, `attempt`, `base`, `spent_usd=self.run_cost` (a callable, read when the pool
  asks) and `budget_usd=self.cfg.run_budget_usd`; the planner passes `job_id="planner"`, `attempt=0` and no budget. The reservation still ends when `launch` returns, a moment before the runner records the cost in
  `spent`: the estimate is released slightly before the cost is counted.
* **`podsweep.protect` is process-local, and is no longer what protects a session of another process.** The manager's sweep is another process; a plan session (no job in any state), a
  preflight session (a job that is not `running`) and a session of a supervisor that restarted are held by their live relay record (see the sweep's reasons above), which every process can read.
* The relay's admission and `bundle.stage`'s claim are still two accountings of the spool (slice 9a). They are not unified here. Nothing starts the launcher or the relay (slice 17).

### Checks and UNVERIFIED

Checks: L `PYTHONPATH=src python3 -m unittest tests.test_podbackend tests.test_podbackend_integration tests.test_podexecution tests.test_backend tests.test_podhandle tests.test_podworker_run tests.test_podpool tests.test_podsweep tests.test_podliveness tests.test_launcher tests.test_bundle tests.test_delta_malicious`.
`tests/test_podbackend.py` covers the happy path, every verdict above on the fake launcher AND on the real launcher over the in-memory API's scenarios (`succeeds`, `oom`, `exit_nonzero`,
`evicted`, `deadline`, `node_deleted`, `karpenter_eviction`, `unschedulable`, `image_pull_backoff`), the hostile deltas and reports, the output files as data (a link, a fifo, an oversized file), a poisoned
environment, the isolation failure, the breaker, the wiring and a real git repository for the ship-in and ship-back. `tests/test_podbackend_integration.py` pins the four seams: the `run` section
against the real `podworker.read_run`, every outcome and wrapper stage, the pool's call contract (admit before staging, `requested` right before the launch, the token refunded only for a launch that
was not sent, a slot released on every way out, every refusal a fixed text), the sweep's protection until after `released`, `enforce` writing the revoke marker before it terminates, a pod that waited
400 s for its node not being killed, and `EndToEnd`, in which the real wrapper (part A and part B) runs the bundle the real backend staged against the real relay over a socket and the delta lands in
the supervisor's worktree. P is not applicable (no Terraform); V (live only): everything with a real pod.

UNVERIFIED: that the real wrapper, as a container with `hostUsers: false` and the pod's capability set, reads the manifest as the test's own-identity wrapper does (the end-to-end test runs the agent as the test's own user, with no capability-dropping
wrapper, and the supervisor's and the pod's data directories are the same path only because the test swaps them); that the thresholds are right on real hardware (`START_BOUND_SECONDS`, the 300 s of silence, the 45 s of a
terminating pod, `TIMEOUT_MARGIN_SECONDS`, the 2 s poll: PROVISIONAL); that the wrapper's stop, drain and delta fit in the 120 s margin for the delta of a real tree; that a pod that is `terminating` and posts nothing is really
gone within the grace period; that a pod killed from outside rightly loses its delta for the repositories the owner will enrol; that the launcher's `status` answers the states this table reads for a pod it reaped (`unschedulable`,
`starting_error` with `final`; the fake is the only evidence); that the wrapper's exit 68 (`canary`) is only ever an isolation failure or a canary that could not run (the stage a never-classified exception is reported against is
the one it was in); that `podliveness` should measure `never_heard` from the pod's start rather than its creation (the backend ignores that verdict and uses its own fetch bound; a change to `podliveness` is the right fix); that a supervisor in the manager's
main container can write the breaker's marker and the pool's ledger and read the spool as the relay's group (the permissions are the init container's, slice 17); that `flock` on the ledger volume serialises supervisors (slice 18's own
assumption); the cost of one `pod_state` call every 2 s per running session and of a `launcher_live_count` call per admission; that the phase's `--max-budget-usd` is a fair estimate for the pool; and that
`FACTORY_DATA=/data` is what the manager's main container really has under AKS (the image's `ENV`, not read from a running pod).

## The program, slice by slice

The numbering is the program's own; every pull request carries `Refs #N` and none closes an issue.

| Slice | Content | Refs |
|---|---|---|
| 0 | this contract: Terraform locals, Python constants, a test that parses both | 144 145 146 147 |
| 1 | API-error classifier and a stop-the-world breaker (also helps in-process mode) | 146 |
| 2 | documentation and issue-text corrections, the ported items | 144 to 147 |
| 3 | supervisor git hardening (explicit git directory and work tree, hooks and fsmonitor off) | 145 |
| 4 | the backend seam (prepare, launch, collect) and `LocalBackend`, no behaviour change | 145 |
| 5 | an additive pod handle beside the pid, recovery fails closed (delivered, used by `PodBackend` (12b), which is off by default: see "The pod handle in job state") | 145 147 |
| 6 | worker pod template, golden tests, a Python mirror of the admission policy | 145 |
| 7 | a standard-library Kubernetes client, an in-memory fake, the pod status classifier | 145 |
| 8 | the relay as its own container: token state machine, spool, caps, session record (delivered, used by `PodBackend` (12b), which is off by default: see "The relay and the session record") | 145 |
| 9a, 9b | bundle wire format and ship-in staging (9a, delivered, used by `PodBackend` (12b), which is off by default: see "The bundle and ship-in staging"); file delta validation and safe apply (9b, delivered, used by `PodBackend` (12b), which is off by default: see "The file delta: validation and safe apply") | 145 |
| 10a, 10b | pod worker: fixed output, network canary, fetch and extract (10a, delivered, used by `PodBackend` (12b), which is off by default: see "The pod worker, part A"); run, stream, delta, footprint, SIGTERM and the report (10b, delivered, used by `PodBackend` (12b), which is off by default: see "The pod worker, part B") | 145 |
| 11 | the launcher program and its client (delivered, used by `PodBackend` (12b), which is off by default: see "The launcher and its client") | 145 |
| 12a, 12b | the `execution` setting, the `FACTORY_POD_BACKEND` refusal and the manager instance id (12a, delivered: refuses unless 12b can be built); `PodBackend` (12b, delivered, selected only by `execution = "pod"` and `FACTORY_POD_BACKEND=on`, both off by default, nothing starts a launcher or a relay: see "The pod backend (slice 12b)") | 145 |
| 13 | cluster-action guard (`tools/infra_cluster_guard.py`) and snapshot attestation | 144 |
| 14 | Terraform: default-off Cilium and NAP variables, NodePool objects, the ceiling local (this page, "Terraform A") | 144 146 |
| 15a, 15b | Terraform: worker namespace, default-deny policy, log exclusion (15a: this page, "Terraform B1"); admission policies | 144 145 |
| 16, 17 | Terraform: Role and RoleBinding (each `depends_on` `kubernetes_network_policy_v1.worker_default_deny`: the fence exists before any Role does); launcher and relay containers, init container, pool taint (delivered, not wired in: default off, see "Terraform C"; the pod backend that would use it is off by default as well) | 145 147 |
| 18 | pod pool and ceiling, pacing, reserve-on-admit (delivered, used by `PodBackend` (12b), which is off by default: see "The pod pool: ceiling, pacing and reserve-on-admit") | 146 |
| 19, 20 | pod-side liveness; orphan sweep, recovery retry, pod-aware deploy gate (delivered, used by `PodBackend` (12b), which is off by default: see "Pod-side liveness, the orphan sweep and the deploy gate") | 147 |
| 22 | after a person has run the pod backend on a real cluster: recorded fixtures replace assumed shapes (not done) | 145 146 147 |

Slices 13 to 17 are Terraform and default off; each pull request shows the `plan` summary and the
required zero-cluster-change result. The Python slices run in parallel after this one.

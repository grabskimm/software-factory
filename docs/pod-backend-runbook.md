# Pod backend: the live runbook

Status: **delivered, nothing was run.** This is slice 21 of the program of issues #144 to #147 (`docs/pod-backend.md`, "The program,
slice by slice"). It is a document and four small programs; no assistant ran any of it, and nothing here switches anything on. A **person
with Azure access** runs it, first read-only against production, then against a **throwaway mirror** built from the same `infra/` code with
its own `name_prefix` and its own state. What the person records becomes the test fixtures of slice 22, which replace the assumed shapes
of slices 1, 7 and 11.

The owner's decisions behind it (issue #145 and `docs/pod-backend.md`, "Decisions that shape the design"): no assistant runs
Azure, no `terraform apply` is run by an assistant, read-only production checks come first, the mirror is built from the same code, the
recordings become fixtures. The wording of the program applies: a fact that only a live cluster can show is **live** or **UNVERIFIED**; a
check against a fake is "contract-tested against assumed shapes", never "proven".

## How to read this page

* **Seven parts, in this order.** A (before anything), B (read-only production checks), C (build the mirror), D (the cluster layers on
  the mirror: Cilium, node auto-provisioning, the worker pool), E (the worker namespace and the admission policies on the mirror), F (the
  plan identity: **a hard gate**), G (the session path on the mirror, and the teardown). Do not skip ahead: each part states what it needs.
* **Every step has the same eight fields:** Needs, Run, Expect, Stop if, Rollback, Approves, Cost, Record. "Stop if" means stop, change
  nothing more, write down what you saw and tell the owner; a stop is a result, not a failure of the person. A step that is not "Rollback:
  none, read-only" has its undo written next to it.
* **Placeholders** are `<<LIKE_THIS>>`; replace each one yourself and never paste a value that is a secret into a file of the repository. The
  commands are an outline written without access to Azure: **none of them was run by their author**. A command that fails for a reason of
  syntax is a defect of this page: fix the page in a small pull request and say so in the log.
* **Everything is read-only unless a step says "Rollback:" with an action.** The write steps are on the mirror, except **three production writes**,
  each of which needs the Owner's explicit approval, in writing, before it is run: the `AcrPull` assignment on the **production registry** that C4's apply makes
  for the mirror's kubelet identity, the `Reader` assignment on the same registry that F1 makes for the mirror's plan identity (G9 deletes both: the deletion of the
  mirror's resource group does not), and F4 (the production role, after F3).
* **The check scripts** print only fixed text, counts and numbers, call `az` and `kubectl` with argv lists and a timeout, and never print a
  name of a pod or a node, an environment value, a token or a secret (their own tests pin that). They are standard library only:
  `tools/live_checks.py` (read-only calls and verdicts), `tools/live_reports.py` (verdicts over files you saved),
  `tools/live_recording.py` (the recording format) and `tools/live_pod_probe.py` (probes you feed to an interpreter inside a pod of the mirror).
* **The table** at the end ("Which step settles which UNVERIFIED claim") maps every UNVERIFIED claim of `docs/pod-backend.md`,
  `docs/capacity.md` and `infra/README.md` to the step that settles it, or says why no step does.

## Part A: before anything

### A1. People, roles and approvals

* **Needs:** the owner's go for the part you are about to start. In a team of one the owner is the Runner; write the decision down as a `note`
  recording anyway.
* **Run:** nothing. The roles are:

  | Role | Who | May |
  |---|---|---|
  | Owner | the person who decides on the production and the program | approve a part, approve every production write (the two registry role assignments of C4 and F1, and F4), decide on every "Stop if" |
  | Runner | a person with Azure access (Contributor and role-assignment rights on the mirror's resource group, read on production, and the right to assign and delete a role on the registry for C4, F1 and G9) | run the steps |
  | Reviewer | a second pair of eyes, or the Owner | read every recording before it leaves the Runner's machine (A6) |

* **Expect:** an entry in the log (A6) per part: who approved, when, which commit of the repository.
* **Stop if:** a step would write to production and the Owner has not approved it in writing. Three do: C4 and F1 (a role assignment on the registry that production uses) and F4 (the production role).
* **Rollback:** none, read-only.
* **Approves:** Owner, per part.
* **Cost:** none.
* **Record:** `note` (the approval).

### A2. What must be true before anything starts (statements, not steps)

* **Needs:** the commit under test (A3).
* **Run:** read these; each is checked where the table says.
  1. **One repository per node.** The worker pod carries a required pod anti-affinity on the repository label, the NodePool allows one pod
     per node by default (`worker_pool_pods_per_node = 1`), so N repositories running at once need N nodes. This is a prerequisite of the pod
     design, not an option: a node that held the sessions of two repositories would put two repositories' files on one disk and one kernel.
     Step G6 checks it live; until it passes, no more than one repository may use the pod backend.
  2. **The admission policy matrix is a prerequisite.** `tools/policy_matrix.py` replays the golden pods and every mutation against an API
     server; it was validated on kind (2026-11-09, kind v1.36.1: 12 golden objects admitted, 172
     mutations denied naming their rule, 6 without effect, 37 rejected before the policy, 0 problems). Replay it on kind for the commit under test
     (`tools/local_pod_e2e`, `infra/README.md`) and keep the totals: they must match. The same tool is run again on AKS in E4, because the evaluator there is not kind's.
  3. **Which slices must be in the tree.** Parts A to D need slices 14, 15a and 15b (Terraform A, B1 and B2) and the tools of this slice. Part E needs slices 16 and 17 as well: `pod_backend_enabled` (E1) makes the namespace and the policies **and** the
     launcher's Role and RoleBinding (slice 16) **and** the launcher and relay containers, the init container and the relay Service of the manager pod (slice 17, which rolls the manager). Part G needs slices
     12b (the backend), 16 (the Role and RoleBinding), 17 (the launcher and relay containers) and, for G7, slices 18, 19 and 20 (the pool, pod-side liveness, the sweep and the deploy gate); the program table in `docs/pod-backend.md` says which are delivered
     (at the time of writing all of them are in the tree). A part whose slice is not delivered is **not run**; its table rows stay UNVERIFIED.
  4. **Everything is off.** Nothing in this runbook sets `pod_backend_enabled`, `worker_nodepool_enabled`, `network_data_plane` or `nap_mode` in
     production. The production steps of the infra README ("Switching the data plane") are the owner's, afterwards, and are not part of this page.
* **Expect:** the matrix replayed clean on kind for the commit under test, the program table consistent with 3.
* **Stop if:** the replay shows a problem or was not run, or a slice that a part needs is not delivered.
* **Rollback:** none, read-only.
* **Approves:** Owner.
* **Cost:** none.
* **Record:** `note`.

### A3. Tools and the tree under test

* **Needs:** bash (the guards of C1 use `declare` and `[[ ]]`), a checkout of the repository at the commit under test, `az` 2.76 or later (the documented minimum for
  node auto-provisioning), `kubectl`, `kubelogin`, `terraform` (the version of `infra/versions.tf`), `jq`, Python 3.11 or later and `git`.
* **Run:**

  ```sh
  export TREE=<<PATH_OF_THE_CHECKOUT>> WORK=<<SCRATCH_DIRECTORY_OUTSIDE_THE_CHECKOUT>> REC=<<RECORDINGS_DIRECTORY>>
  mkdir -p -m 0700 "$WORK" "$REC"
  export PYTHONPATH="$TREE/src"
  unset KUBECONFIG                                   # no default cluster: every call names its own file
  export KC_PROD="$WORK/kubeconfig-production" KC_MIRROR="$WORK/kubeconfig-mirror"
  live()    { python3 "$TREE/tools/live_checks.py" "$@"; }
  reports() { python3 "$TREE/tools/live_reports.py" "$@"; }
  record()  { python3 "$TREE/tools/live_recording.py" "$@"; }
  git -C "$TREE" rev-parse HEAD && git -C "$TREE" status --short
  PYTHONPATH="$TREE/src" python3 -m unittest tests.test_live_checks tests.test_live_reports tests.test_live_recording tests.test_live_pod_probe tests.test_live_runbook
  ```

  Every new shell starts with the block above, and, once C1 has written it, with `. "$WORK/mirror-names.sh"` (the mirror's names, the guards of C1 and, after F1, `$APP_ID`).
* **Expect:** a clean tree, the commit id in your log, the five test modules green (run from `$TREE`).
* **Stop if:** the tree has local changes or a test fails: the tools are not trustworthy, so nothing they print is.
* **Rollback:** none, read-only.
* **Approves:** Runner.
* **Cost:** none.
* **Record:** `note` (the commit id).

### A4. Separation rules of the mirror

* **Needs:** A3.
* **Run:** hold to these rules for the whole runbook.
  * The mirror has its **own `name_prefix`** (`$MIRROR_PREFIX`, at most 12 characters, never the production one), its **own resource group**
    (`$MIRROR_RG`, which Terraform does not create), its **own Terraform state in its own container** (`$MIRROR_STATE_CONTAINER`) and its own `*.tfvars` file
    in `$WORK`. The tfvars of the reference deployment (`INFRA_TFVARS`) are never touched and no pipeline runs for the mirror. **The names are shell variables, set once, in C1, with guards that refuse
    an empty name, a name of production's and a group that does not hold the prefix; every command that creates or deletes uses the variables and starts with a guard,
    never a name typed or pasted into the command.**
  * The mirror has **its own secrets**: a throwaway Anthropic key from a **separate workspace with a small spend limit**, and a throwaway GitHub App installed
    on one sandbox repository only. The production keys never go near it.
  * **Custom role names are tenant-wide.** `az role definition update` on the name `Factory Plan Cluster Reader` changes the **production** role. On the mirror
    use the role that F1 makes with `mirror-role` (another name, the mirror's scope). Never run `az role definition create` or `update` with the production name
    before F4.
  * Use `--kubeconfig "$KC_MIRROR"` for the mirror and `--kubeconfig "$KC_PROD"` for production; never `kubectl` without one, **in a sentence of this page as in a block**
    (`~/.kube/config` may still point at production: A3 unsets `KUBECONFIG`, it does not remove that file; `live_checks.py` refuses to run a `kubectl` command without `--kubeconfig`).
    A write verb (`apply`, `create`, `delete`, `patch`, `exec`) is never run with `$KC_PROD`.
  * The mirror shares the **subscription and its vCPU quotas** and the **registry** with production, **and it writes two role assignments on that registry**: C4's Terraform
    makes `AcrPull` for the mirror's kubelet identity (`azurerm_role_assignment.acr_pull`, the registry is production's and looked up by name) and F1 gives the mirror's plan
    identity `Reader` on it (the plan reads the registry). **Both are production writes**: the Owner approves them in writing before C4 and F1, you list them in the log, and G9 deletes them
    by name (the deletion of the mirror's resource group does not: the registry is not in it). F1 also assigns `Storage Blob Data Reader` on the mirror's own state container in the
    shared state account; that scope is the mirror's and disappears with the container (G9).
* **Expect:** you can say, for each command you run, which cluster it addresses.
* **Stop if:** a command's cluster is in doubt. Do not run it.
* **Rollback:** none, read-only.
* **Approves:** Runner.
* **Cost:** none.
* **Record:** none.

### A5. Cost and quota rules

* **Needs:** the prices of the sizes you will use, read on the day: `curl -sS -G https://prices.azure.com/api/retail/prices --data-urlencode "\$filter=armRegionName eq '<<LOCATION>>' and armSkuName eq '<<SIZE>>' and priceType eq 'Consumption'"`
  (read on 2026-11-09: `D2ds_v5` $0.113, `D8ds_v5` $0.452, `D4s_v6` $0.202 an hour; any other dollar figure is UNVERIFIED).
* **Run:** write the budget down before C starts.
  * **The mirror's floor** is about the production floor: system `D2ds_v5` plus the `factory` pool `D8ds_v5`, about $0.565 an hour, $13.6 a day of VM time, plus
    the load balancer, the NAT gateway, the disks and Log Analytics. Plan for **three days** and delete it (G9).
  * **The NodePool is capped** at `worker_pool_limits_cpu = 8` on the mirror (two `D4s_v6` nodes, $0.40 an hour at most).
  * **The Anthropic spend** is capped by the throwaway workspace's limit (`<<WORKSPACE_LIMIT_USD>>`), which a person sets in the Console: nothing here sets it.
  * **The quota** is shared with production: C2 prints the sums before anything is created.
  * Look at the bill the day after C3 and the day after G9 (Cost Management, resource group `$MIRROR_RG` and its node resource group).
* **Expect:** a number you wrote down.
* **Stop if:** the number is not acceptable to the Owner.
* **Rollback:** none, read-only.
* **Approves:** Owner (the budget).
* **Cost:** none by itself.
* **Record:** `note` (the prices and the budget).

### A6. Recordings: what is kept, and who reviews it

* **Needs:** `$REC` (A3).
* **Run:** keep a log (a text file in `$WORK`) with one line per step: time, step, result, recording file names. Every observation that slice 22 should turn into a fixture is a
  **recording** (the format is at the end of this page): `live --record "$REC" --step <<STEP>> <<command>>` writes one when the command observes something;
  `record import --step <<STEP>> --kind <<KIND>> --out "$REC" <<FILE>>` imports a file you saved. The Reviewer reads each file **before** it is copied anywhere:
  `record validate "$REC"/*.json` must pass, and a person reads the payloads. A Kubernetes Secret is refused by the tool; an environment value, a token and a GUID are scrubbed; **issue
  text, pull request titles and repository names are not recognised by the scrubber**: do not record a `status --json` as it is (B4 shows the footprint block alone). **The Reviewer's checklist**
  (what a person must still look for; the recording format, at the end of this page, says what the scrubber does): issue text, a title, a repository or person's name, a host name, a node or pod name; a secret in
  a form the scrubber does not know (a secret in prose with no `password` or `token` word beside it, a connection string, a Secret copied by hand into a `note`, a long hexadecimal string that is a
  digest of something else); the **last-applied-configuration annotation** and any text that holds a manifest (the tool refuses a `"kind": "Secret"` inside a string, but not a secret in another field of it); an IPv6 address is replaced, a
  fully qualified host name is not.
* **Expect:** `record validate "$REC"/*.json` prints `ok` for every file and no `FAIL`.
* **Stop if:** the validator fails, or the Reviewer finds a value that does not belong.
* **Rollback:** delete the file (`$REC` is yours alone, mode 0700).
* **Approves:** Reviewer (before any file is copied into the repository, which is slice 22's pull request, not this runbook).
* **Cost:** none.
* **Record:** none: the recordings themselves are the output of the other steps.

### A7. Stop rules that hold everywhere

* **Needs:** nothing.
* **Run:** stop, change nothing more and tell the Owner when: a call is `Forbidden` and the cure would be to widen a role (**never widen a role by guessing a name**: F3); a plan shows a
  `must be replaced` or `will be destroyed` for the cluster, a node pool or the data disk; a command reaches the production cluster that should not; a recording contains a value that
  does not belong; the quota sums do not fit; the bill moves faster than A5 said.
* **Expect:** you stop rather than work around.
* **Stop if:** (this is the list).
* **Rollback:** none; each step has its own.
* **Approves:** Owner decides how to go on.
* **Cost:** none.
* **Record:** `note` (what you saw).

## Part B: read-only production checks

Everything in B is a read. It uses `$KC_PROD` and an account with read rights on production. It changes nothing, and it comes **before** the mirror because it replaces
numbers that are still PROVISIONAL (the resource classes) and checks the assumptions the mirror is built on.

### B1. The cluster's profile and tier

* **Needs:** A3; read access to the production cluster.
* **Run:**

  ```sh
  az aks get-credentials --resource-group <<RESOURCE_GROUP>> --name <<CLUSTER>> --file "$KC_PROD"
  kubelogin convert-kubeconfig -l azurecli --kubeconfig "$KC_PROD"
  live --record "$REC" --step B1 capture --kind az-aks --resource-group <<RESOURCE_GROUP>> --cluster <<CLUSTER>>
  ```

* **Expect:** the recording holds the Kubernetes version (1.36.x), the tier (Free or Standard: write it in the log), network plugin `azure` with mode `overlay`, data plane `azure` (or null), outbound
  type `userAssignedNATGateway`, `nap` null, and the pools with their sizes (system `Standard_D2ds_v5`, factory `Standard_D8ds_v5`, `mon` if monitoring is on) and `AzureLinux3`.
* **Stop if:** the data plane is already `cilium` or NAP is already on: the infra README's order (Switching the data plane) starts from another state; tell the Owner.
* **Rollback:** none, read-only.
* **Approves:** Runner.
* **Cost:** none.
* **Record:** `az-aks`.

### B2. vCPU quota and the sums

* **Needs:** B1 (the pool sizes). The reference deployment used 12 vCPUs (system 2, factory 8, `mon` 2); read yours again.
* **Run:**

  ```sh
  live --record "$REC" --step B2 quota --location <<LOCATION>> --family standardDSv6Family \
       --worker-limit-cpu 32 --new-vcpu 0 --surge-vcpu 24 --family-new-vcpu 0
  ```

  `--worker-limit-cpu` is the NodePool limit you intend for production (the default 32), `--surge-vcpu` is twice the production vCPUs (a node-image or upgrade surge doubles each
  fixed pool at the same time), `--family` is the quota name of the worker size (`standardDSv6Family` for `Standard_D4s_v6`).
* **Expect:** `regional: used 12 of 150, planned 56, margin 82`, `family: used 0 of 350, planned 32, margin 318` and `quota: fits`. The regional total binds and Terraform does **not** check it.
* **Stop if:** `FAIL quota`. Lower the limit or ask for more quota; do not continue to C.
* **Rollback:** none, read-only.
* **Approves:** Runner.
* **Cost:** none.
* **Record:** `az-quota`.

### B3. System-node headroom, before any change

* **Needs:** `$KC_PROD`.
* **Run:** `live --kubeconfig "$KC_PROD" node-requests`
* **Expect:** one line per node. The system node was measured on 2026-11-08 at 1,676 of 1,900 mCPU requested (88 percent): write down today's figure. This is the "before" of D2 (the
  size of the Cilium agent on this node is UNVERIFIED).
* **Stop if:** the output has a `FAIL` line (an allocatable or a request that is not understood).
* **Rollback:** none, read-only.
* **Approves:** Runner.
* **Cost:** none.
* **Record:** `note` (the three numbers of the system node).

### B4. The footprint of real sessions (replaces the PROVISIONAL classes)

* **Needs:** at least 20 build, 20 review and 20 verify sessions on real repositories of the production manager (`docs/capacity.md`, "Procedure"); if there are fewer, say so: the tool notes it and the figures are not a measurement.
* **Run:** for each run read its status (the manager API, `docs/operations.md`, "Status") and keep only the footprint block in the recording:

  ```sh
  TOKEN=$(az account get-access-token --resource api://<<ENTRA_APP_CLIENT_ID>> --query accessToken -o tsv)
  for RUN in <<RUN_ID_1>> <<RUN_ID_2>>; do
    curl -sS -H "Authorization: Bearer $TOKEN" "https://<<MANAGER_HOST>>/repos/<<ORG>>/<<REPO>>/runs/$RUN/status" > "$WORK/status-$RUN.json"
    python3 -c 'import json,sys; print(json.dumps({"footprint": json.load(open(sys.argv[1]))["footprint"]}))' "$WORK/status-$RUN.json" > "$WORK/footprint-$RUN.json"
  done
  reports footprint "$WORK"/footprint-*.json
  for F in "$WORK"/footprint-*.json; do record import --step B4 --kind status-footprint --out "$REC" "$F"; done
  ```

  (`status --json` holds issue titles and objectives: never record or paste it as it is. `unset TOKEN` afterwards.)
* **Expect:** per phase the invocations, the peak and an upper bound of the p95 resident memory, and for each class (`standard` 4096 MiB, `small` 2048 MiB) whether the peak is below its memory limit. A class
  that fits is a candidate; changing `podcontract.CLASSES` is a small pull request (the contract tests fail until Terraform and Python agree), never a silent edit.
* **Stop if:** `FAIL phase ...: no resource class has a memory limit above the peak`: the classes are too small for a real session; the Owner decides.
* **Rollback:** none, read-only.
* **Approves:** Runner.
* **Cost:** none.
* **Record:** `status-footprint` (the footprint block only).

### B5. The live plan role against the repository file

* **Needs:** a role that may read role definitions.
* **Run:**

  ```sh
  az role definition list --name "Factory Plan Cluster Reader" -o json > "$WORK/role-before.json"
  reports role-diff "$WORK/role-before.json"
  record import --step B5 --kind az-role --out "$REC" "$WORK/role-before.json"
  ```

* **Expect:** `role-diff: the live role equals the repository file` (seven data actions), or `missing in the live role` for the two actions the Role and RoleBinding reads added (slice 15b): then F4 is needed before any enabling flip.
  Keep `role-before.json`: it is the rollback of F4.
* **Stop if:** `extra in the live role`: someone widened it; the Owner decides.
* **Rollback:** none, read-only.
* **Approves:** Runner.
* **Cost:** none.
* **Record:** `az-role`.

### B6. The production plan shows no line for any of it

* **Needs:** a saved `terraform plan` text of production, read as the read-only plan identity: the latest plan of your own pipeline, or one you make with the recipe of F3 (`AZURE_CONFIG_DIR` of the plan identity, `-lock=false`,
  a scratch copy of `infra/` made as in C1) with production's backend and tfvars. A plan never writes state.
* **Run:**

  ```sh
  grep -E "Plan: |No changes|\.worker|factory-workers|pod_backend" <<PRODUCTION_PLAN_LOG>>
  ```

  If you also run the optional `tools/infra_cluster_guard.py` on the action list of that plan, it prints `cluster-touching actions in this plan: 0` for such a plan.
* **Expect:** a plan with no line for `worker_nodepool`, `worker_nodeclass`, the worker namespace or the policies (the variables are unset in production). This settles that an unset
  variable gives no plan line, on production's real state.
* **Stop if:** any of the new addresses is in the plan, or the cluster guard (if you ran it) refused: that is a finding to fix first, not a reason to weaken the guard.
* **Rollback:** none, read-only.
* **Approves:** Runner.
* **Cost:** none.
* **Record:** `plan-actions` (the lines you grepped).

### B7. Versions, operating system and the registry pull identity

* **Needs:** `az` and `$KC_PROD`.
* **Run:**

  ```sh
  az aks get-versions --location <<LOCATION>> --output table
  az aks get-upgrades --resource-group <<RESOURCE_GROUP>> --name <<CLUSTER>> --output table
  az aks nodepool list --resource-group <<RESOURCE_GROUP>> --cluster-name <<CLUSTER>> \
    --query "[].{name:name,size:vmSize,os:osSku,count:count,version:orchestratorVersion}" --output table
  az role assignment list --assignee "$(az aks show -g <<RESOURCE_GROUP>> -n <<CLUSTER>> --query identityProfile.kubeletidentity.objectId -o tsv)" \
    --scope "$(az acr show --name <<ACR_NAME>> --query id -o tsv)" --output table
  ```

* **Expect:** whether 1.37 is offered in the region today; the OS SKU the pools run (`AzureLinux3`); the kubelet identity holds `AcrPull` on the registry (the pull of a NAP node relies on the same assignment, D7).
* **Stop if:** nothing here stops the program; a surprise goes to the Owner.
* **Rollback:** none, read-only.
* **Approves:** Runner.
* **Cost:** none.
* **Record:** `az-aks` (version table as a text `note`).

### B8. Limits outside Azure

* **Needs:** access to the Anthropic Console (organisation and the throwaway workspace) and to the GitHub App's settings.
* **Run:** read and write down, in the log and as a `note` recording: the Console's usage **tier**, the limits of the workspace (requests, input and output tokens a minute), whether the **workspace spend limit**
  is set (it is a human action; nothing in the program sets it) and to what, the **list of API keys** of the organisation (is there any key outside the workspace the Factory uses?), and the GitHub installation's
  rate-limit headroom (`x-ratelimit-remaining` of the manager's own calls, `docs/metrics.md`; never print a token).
* **Expect:** numbers and a yes or no per item.
* **Stop if:** a key exists that should not, or the spend limit is not set: set it first (Console, a human action).
* **Rollback:** none, read-only.
* **Approves:** Owner (the spend limit).
* **Cost:** none.
* **Record:** `note`.

## Part C: build the mirror

### C1. Names, a scratch copy and its own state

* **Needs:** A4, the budget of A5, the Owner's go for Part C.
* **Run:** first the **names, once, as shell variables, and the guards** (production's own values are asked for on purpose, so that a mirror name that is production's is refused by the machine and not only by your eyes). `<<PRODUCTION_STATE_CONTAINER>>` is the container of production's state in the same account:

  ```sh
  export PROD_RG=<<RESOURCE_GROUP>> PROD_PREFIX=<<PRODUCTION_NAME_PREFIX>> PROD_STATE_CONTAINER=<<PRODUCTION_STATE_CONTAINER>>
  export MIRROR_PREFIX=<<MIRROR_PREFIX>> MIRROR_RG=<<MIRROR_RG>> MIRROR_STATE_CONTAINER=<<MIRROR_STATE_CONTAINER>>
  mirror_guard() {
    for NAME in PROD_RG PROD_PREFIX PROD_STATE_CONTAINER MIRROR_PREFIX MIRROR_RG MIRROR_STATE_CONTAINER; do
      [ -n "${!NAME}" ] || { echo "refused: $NAME is empty" >&2; return 1; }
    done
    [ "$MIRROR_PREFIX" != "$PROD_PREFIX" ] || { echo "refused: the mirror prefix is production's" >&2; return 1; }
    [ "$MIRROR_RG" != "$PROD_RG" ] || { echo "refused: the mirror group is production's" >&2; return 1; }
    [ "$MIRROR_STATE_CONTAINER" != "$PROD_STATE_CONTAINER" ] || { echo "refused: the mirror state container is production's" >&2; return 1; }
    [[ "$MIRROR_PREFIX" =~ ^[a-z][a-z0-9-]{0,10}[a-z0-9]$ && "$MIRROR_PREFIX" != *--* ]] || { echo "refused: the mirror prefix is not a name_prefix" >&2; return 1; }
    case "$MIRROR_RG" in *"$MIRROR_PREFIX"*) ;; *) echo "refused: the mirror group name does not hold the mirror prefix" >&2; return 1 ;; esac
    case "$MIRROR_STATE_CONTAINER" in *"$MIRROR_PREFIX"*) ;; *) echo "refused: the mirror state container name does not hold the mirror prefix" >&2; return 1 ;; esac
  }
  mirror_group_is_ours() {
    mirror_guard && [ "$(az group show --name "$MIRROR_RG" --query tags.purpose -o tsv)" = pod-backend-mirror ] || { echo "refused: the group is not the mirror's" >&2; return 1; }
  }
  mirror_tfvars_ok() {
    grep -qx "name_prefix *= *\"$MIRROR_PREFIX\".*" "$WORK/mirror.tfvars" && grep -qx "resource_group_name *= *\"$MIRROR_RG\".*" "$WORK/mirror.tfvars" || { echo "refused: mirror.tfvars does not name the mirror" >&2; return 1; }
  }
  mirror_app_is_ours() {
    mirror_guard && [ -n "$APP_ID" ] && [ "$(az ad app show --id "$APP_ID" --query displayName -o tsv)" = "sp-plan-$MIRROR_PREFIX" ] || { echo "refused: the application is not the mirror's" >&2; return 1; }
  }
  { declare -p PROD_RG PROD_PREFIX PROD_STATE_CONTAINER MIRROR_PREFIX MIRROR_RG MIRROR_STATE_CONTAINER; declare -f mirror_guard mirror_group_is_ours mirror_tfvars_ok mirror_app_is_ours; } > "$WORK/mirror-names.sh"
  mirror_guard && echo "the names are accepted"
  ```

  Then the group (created **only if it does not exist**: `az group create` on an existing group would overwrite its tags) and the scratch copy of **the committed files only** (`git archive`, so that a production `terraform.tfvars`,
  `*.tfstate` or `override.tf` that a first apply left in the checkout's gitignored files cannot come along: Terraform would load them silently):

  ```sh
  mirror_guard && [ "$(az group exists --name "$MIRROR_RG")" = false ] && az group create --name "$MIRROR_RG" --location <<LOCATION>> --tags purpose=pod-backend-mirror owner=<<OWNER>> delete-after=<<DATE>>
  mkdir -m 0700 "$WORK/infra-mirror" && git -C "$TREE" archive HEAD infra | tar -x -C "$WORK/infra-mirror" --strip-components=1
  cp "$TREE/infra/terraform.tfvars.example" "$WORK/mirror.tfvars"      # then edit it: name_prefix, resource_group_name, the mirror's secrets' versions,
                                                                     # ingress_custom_host = null, repo_configs of the sandbox repository only,
                                                                     # aks_cluster_admin_principal_ids = [your own object id]  (never the plan identity's)
  find "$WORK/infra-mirror" \( -name '*.tfstate*' -o -name 'override.tf*' -o -name '*_override.tf*' -o -name '.terraform*' -o \( \( -name '*.tfvars' -o -name '*.tfvars.json' \) ! -name '*.auto.tfvars' ! -name '*.auto.tfvars.json' \) \) -print
  mirror_tfvars_ok && echo "mirror.tfvars names the mirror"
  ```

  The scratch copy keeps the committed `*.auto.tfvars` (the image digest is production's: the mirror pulls the same image from the same registry). Do not delete the two
  `prevent_destroy` lines now (G9 does): they stop a replace of the cluster from being applied by mistake.
  Follow `infra/README.md`, "Bootstrap", steps 1 to 8, with the mirror's names: the state container `$MIRROR_STATE_CONTAINER`, the vault and its two secrets
  (the **throwaway** Anthropic key and the sandbox GitHub App key), the same Entra values as production (they are not secrets).
* **Expect:** the guard prints `the names are accepted`; `az group exists` printed `false` before the create and `az group show --name "$MIRROR_RG"` answers after it; the `find` prints **nothing**; `mirror_tfvars_ok` prints that `mirror.tfvars` names the mirror;
  no file of `$TREE` changed (`git -C "$TREE" status --short` is empty).
* **Stop if:** the guard refuses; `az group exists` prints `true` (the group exists already: it may be production's, or an earlier mirror's that G9 did not remove; do **not** create over it); the `find` prints any path (a `*.tfvars`, `*.tfstate` or `override.tf` in the scratch copy other than the committed `*.auto.tfvars`: delete
  the copy, find out how it got there, and copy again); the prefix, the resource group or the state container in `mirror.tfvars` is one of production's.
* **Rollback:** `mirror_group_is_ours && az group delete --name "$MIRROR_RG" --yes` (the group holds nothing yet; the tag check refuses a group that is not the mirror's), `rm -rf "$WORK/infra-mirror"`.
* **Approves:** Owner (the budget and the names).
* **Cost:** none yet.
* **Record:** `note` (the prefix and the group, not a secret).

### C2. The sums before anything is created

* **Needs:** C1; the mirror's fixed pools (system `Standard_D2ds_v5` 2, factory `Standard_D8ds_v5` 8: 10 vCPUs, 12 with `mon`).
* **Run:** the mirror adds its own fixed pools, the NodePool limit (8) and the surges of both clusters at once:

  ```sh
  live --record "$REC" --step C2 quota --location <<LOCATION>> --family standardDSv6Family \
       --worker-limit-cpu 8 --new-vcpu 10 --surge-vcpu 22 --family-new-vcpu 0
  ```

  (`--new-vcpu` the mirror's fixed pools, `--surge-vcpu` the surge of production's 12 and of the mirror's 10 in the same minute.)
* **Expect:** `quota: fits` with the regional margin written in the log.
* **Stop if:** `FAIL quota`. Do not create the mirror: it would take production's headroom for a surge.
* **Rollback:** none, read-only.
* **Approves:** Runner.
* **Cost:** none.
* **Record:** `az-quota`.

### C3. First apply, stage cluster

* **Needs:** C1, C2, the Owner's go.
* **Run:**

  ```sh
  mirror_guard && mirror_tfvars_ok && terraform -chdir="$WORK/infra-mirror" init \
    -backend-config="resource_group_name=<<TFSTATE_RESOURCE_GROUP>>" -backend-config="storage_account_name=<<TFSTATE_STORAGE_ACCOUNT>>" \
    -backend-config="container_name=$MIRROR_STATE_CONTAINER" -backend-config="key=<<MIRROR_STATE_KEY>>" -backend-config="use_azuread_auth=true"
  terraform -chdir="$WORK/infra-mirror" state list                  # must print NOTHING
  terraform -chdir="$WORK/infra-mirror" plan -var-file="$WORK/mirror.tfvars" -out="$WORK/c3.plan" \
    -target=azurerm_kubernetes_cluster.this -target=azurerm_kubernetes_cluster_node_pool.factory \
    -target=azurerm_role_assignment.aks_cluster_admin -target=azurerm_role_assignment.aks_cluster_user | tee "$WORK/c3-plan.txt"
  mirror_guard && mirror_tfvars_ok && terraform -chdir="$WORK/infra-mirror" apply "$WORK/c3.plan"       # after you read the plan: only creates, every name holds $MIRROR_PREFIX
  ```

  (A plan of the whole configuration fails before the cluster exists, because a `kubernetes_manifest` needs a reachable API server at plan time: stage 1 is these four targets, `infra/README.md`, "First apply".)
* **Expect:** an empty `state list`; creates only; 10 to 20 minutes.
* **Stop if:** `state list` prints anything; a name in the plan does not carry `$MIRROR_PREFIX`; the plan has a `destroy`.
* **Rollback:** the teardown of G9 (its guards run first).
* **Approves:** Owner.
* **Cost:** starts the meter: about $0.57 an hour of VM time plus the network resources.
* **Record:** `timing` (how long the apply took).

### C4. First apply, stage all

* **Needs:** C3; wait a few minutes for the role assignments to propagate; **the Owner's written approval of the production write this apply makes**: `azurerm_role_assignment.acr_pull` gives the mirror's kubelet identity `AcrPull` on the **production registry**
  (`infra/identity.tf`; G9 deletes it). Write the approval in the log before the apply.
* **Run:**

  ```sh
  mirror_guard && mirror_tfvars_ok && terraform -chdir="$WORK/infra-mirror" apply -var-file="$WORK/mirror.tfvars"
  az aks get-credentials --resource-group "$MIRROR_RG" --name "aks-$MIRROR_PREFIX" --file "$KC_MIRROR"
  kubelogin convert-kubeconfig -l azurecli --kubeconfig "$KC_MIRROR"
  kubectl --kubeconfig "$KC_MIRROR" -n factory rollout status deploy/factory
  ```

* **Expect:** the apply succeeds (a `Forbidden` or a missing `SecretProviderClass` kind is the documented propagation delay: wait and apply again), and the manager Deployment rolls out; the one assignment on the registry is `AcrPull` for the mirror's kubelet identity (log it).
* **Stop if:** the rollout does not finish (image pull, Key Vault, disk, sandbox flags: `kubectl --kubeconfig "$KC_MIRROR" -n factory describe pod`); the plan shows any other write on the registry or on a resource outside the mirror's group; the Owner has not approved the registry assignment.
* **Rollback:** the teardown of G9 (it deletes the registry assignment first, delete the mirror and start again from C1).
* **Approves:** Owner (the production write: `AcrPull` on the registry).
* **Cost:** as C3.
* **Record:** none.

### C5. The baseline equals production, a second plan is empty, a replace is seen

* **Needs:** C4.
* **Run:**

  ```sh
  terraform -chdir="$WORK/infra-mirror" plan -var-file="$WORK/mirror.tfvars" -detailed-exitcode -no-color > "$WORK/c5-plan.txt"; echo "exit $?"
  reports plan-log "$WORK/c5-plan.txt" --expect-no-changes
  # a plan-only replace probe (NEVER applied): another Premium-storage D or E size (for example Standard_D4ds_v5) for the factory pool forces a replacement,
  # which shows the action words Terraform prints for one
  terraform -chdir="$WORK/infra-mirror" plan -var-file="$WORK/mirror.tfvars" -var=aks_user_pool_vm_size=<<OTHER_PREMIUM_SIZE>> -out="$WORK/probe.plan" > /dev/null
  terraform -chdir="$WORK/infra-mirror" show -json "$WORK/probe.plan" \
    | jq -r '.resource_changes[] | select(.change.actions != ["no-op"]) | "\(.change.actions | join(",")) \(.address)"' > "$WORK/probe-actions.txt"
  rm -f "$WORK/probe.plan"
  record import --step C5 --kind plan-actions --out "$REC" "$WORK/probe-actions.txt"
  live --kubeconfig "$KC_MIRROR" --record "$REC" --step C5 nodes
  kubectl --kubeconfig "$KC_MIRROR" get nodes -o json > "$WORK/nodes-C5.json"
  ```

* **Expect:** exit 0 and `plan-log: passed` (no changes), and **no** `worker_`, namespace or policy address in `c5-plan.txt` (the defaults give no plan line: `grep -c "worker_\|factory-workers" "$WORK/c5-plan.txt"` prints 0). The probe lists the replacement as
  `delete,create` or `create,delete` (the guard accepts both). The `nodes` line: 2 nodes (system, factory), none worker.
* **Stop if:** the second plan is not empty (an apply that does not converge is a defect to report before D), or the probe is **applied** (it never is).
* **Rollback:** none; the probe plan file is deleted.
* **Approves:** Runner.
* **Cost:** none.
* **Record:** `plan-actions`, `nodes`.

### C6. The sandbox check on the baseline node

* **Needs:** C4.
* **Run:** `kubectl --kubeconfig "$KC_MIRROR" -n factory exec deploy/factory -- /usr/local/bin/factory-entrypoint factory-sandbox-check`
* **Expect:** `all expectations held` (27 PASS, 0 HARNESS: `spikes/S1.md`). This is the baseline D9 compares with.
* **Stop if:** it fails on the baseline: the mirror does not behave like production and nothing later means anything.
* **Rollback:** none, read-only.
* **Approves:** Runner.
* **Cost:** none.
* **Record:** `probe-output` (the last lines).

## Part D: the cluster layers on the mirror

Each step of D is one variable in `$WORK/mirror.tfvars` and one apply, **in the order of the infra README** (Cilium, then node auto-provisioning, then the NodePool objects), never combined. The mirror's apply is run by hand, so
the drain gate (`tools/infra_deploy_gate.py`) does not apply: nothing of value runs on the mirror's manager.

### D1. Switch the data plane to Cilium

* **Needs:** C5; `$WORK/nodes-C5.json`.
* **Run:** set `network_data_plane = "cilium"` in `mirror.tfvars`.

  ```sh
  terraform -chdir="$WORK/infra-mirror" plan -var-file="$WORK/mirror.tfvars" -out="$WORK/d1.plan" -no-color | tee "$WORK/d1-plan.txt"
  reports plan-log "$WORK/d1-plan.txt" --allow-cluster
  # second terminal, for the downtime: a probe of the manager pod (on its ReadWriteOnce disk) every 10 seconds
  while true; do date -u +%FT%TZ; kubectl --kubeconfig "$KC_MIRROR" -n factory get pod -l app.kubernetes.io/name=factory \
      -o jsonpath='{.items[0].status.phase} {.items[0].status.containerStatuses[*].ready}{"\n"}' 2>&1 | head -c 200; sleep 10; done | tee "$WORK/d1-downtime.log"
  terraform -chdir="$WORK/infra-mirror" apply "$WORK/d1.plan"
  kubectl --kubeconfig "$KC_MIRROR" get nodes -o json > "$WORK/nodes-D1.json"
  reports boot-compare "$WORK/nodes-C5.json" "$WORK/nodes-D1.json"
  live --kubeconfig "$KC_MIRROR" --record "$REC" --step D1 capture --kind az-aks --resource-group "$MIRROR_RG" --cluster "aks-$MIRROR_PREFIX"
  ```

* **Expect:** the plan has exactly one action, `update azurerm_kubernetes_cluster.this` **in place** (the plan-log counts a replace as a failure), and nothing for the Deployment; the apply finishes; `boot-compare` says how many
  nodes restarted (the provider documents that every node is reimaged at once); the log holds the first not-ready time and the last ready time: **the downtime**, in seconds, written as a `timing` recording
  (`{"what": "cilium switch, manager pod not ready", "seconds": <<N>>}`); `az aks show` now says data plane `cilium`; the kubernetes provider planned the objects after the cluster update without an unknown `kube_config` (a plan in D3 confirms it).
  This is also the evidence that enabling Cilium on an Azure CNI Overlay cluster works in place, and how long it takes.
* **Stop if:** the plan says `must be replaced` or `will be destroyed` for the cluster (`prevent_destroy` stops it, which is correct; do **not** remove the line), or the manager pod does not come back.
* **Rollback:** none in place (going back from Cilium forces a new cluster, `infra/README.md`): delete the mirror (G9) and rebuild from C if the experiment must be repeated.
* **Approves:** Owner (the downtime is not measured anywhere yet).
* **Cost:** the pod is down for the measured time; the meter keeps running.
* **Record:** `az-aks`, `timing`.

### D2. What Cilium changed

* **Needs:** D1.
* **Run:**

  ```sh
  kubectl --kubeconfig "$KC_MIRROR" -n kube-system get ds,pods -o wide | grep -i cilium
  live --kubeconfig "$KC_MIRROR" node-requests
  ```

  Then the enforcement test, **without** `network_policy` set: a scratch namespace `mirror-netcheck` with a server pod that listens, a client pod, and a deny policy. Use the production image digest (`<<IMAGE_REF>>`) for both pods.

  ```sh
  kubectl --kubeconfig "$KC_MIRROR" create namespace mirror-netcheck
  kubectl --kubeconfig "$KC_MIRROR" -n mirror-netcheck run server --image=<<IMAGE_REF>> --labels=app=server --port=8000 --command -- python3 -m http.server 8000
  kubectl --kubeconfig "$KC_MIRROR" -n mirror-netcheck run client --image=<<IMAGE_REF>> --command -- sleep 3600
  SERVER_IP=$(kubectl --kubeconfig "$KC_MIRROR" -n mirror-netcheck get pod server -o jsonpath='{.status.podIP}')
  kubectl --kubeconfig "$KC_MIRROR" -n mirror-netcheck exec client -- python3 -c "import urllib.request,sys; print(urllib.request.urlopen('http://$SERVER_IP:8000', timeout=5).status)"
  kubectl --kubeconfig "$KC_MIRROR" -n mirror-netcheck apply -f - <<'EOF'
  apiVersion: networking.k8s.io/v1
  kind: NetworkPolicy
  metadata: {name: deny-all-ingress}
  spec: {podSelector: {}, policyTypes: [Ingress]}
  EOF
  kubectl --kubeconfig "$KC_MIRROR" -n mirror-netcheck exec client -- python3 -c "import urllib.request,sys; print(urllib.request.urlopen('http://$SERVER_IP:8000', timeout=5).status)"
  ```

  Optional, only if the mirror runs monitoring: set `factory_network_policy_enabled = true`, apply, and check what `infra/monitoring.tf` ("Network isolation of the metrics port: PREPARED, OFF") expects: the UI and the API still work through both hosts, the Prometheus target for the
  metrics port is `up`, and a pod in another namespace is refused on 9464 and 8080 (a timeout, not a `401`).
* **Expect:** the Cilium DaemonSet is running on every node; the system node's requested CPU after the switch (compare B3: the agent's size on this node); the first request prints `200`, the second times out (`URLError ... timed out`): the engine
  enforces policy **without** `network_policy`. If the second request also prints `200`, set `network_policy = "cilium"`, apply (a separate step, in place) and repeat: then that variable is needed.
* **Stop if:** the policy is still not enforced with `network_policy = "cilium"`: the worker namespace's fence cannot work; the Owner decides (this blocks E).
* **Rollback:** `kubectl --kubeconfig "$KC_MIRROR" delete namespace mirror-netcheck`.
* **Approves:** Runner.
* **Cost:** negligible.
* **Record:** `probe-output` (the two answers), `note` (whether `network_policy` was needed).

### D3. Turn node auto-provisioning on

* **Needs:** D2 (**not** combined with D1: NAP cannot be enabled while the data plane switches).
* **Run:** set `nap_mode = "Auto"` (`nap_default_node_pools` stays `None`).

  ```sh
  kubectl --kubeconfig "$KC_MIRROR" get nodes -o json > "$WORK/nodes-before-D3.json"
  terraform -chdir="$WORK/infra-mirror" plan -var-file="$WORK/mirror.tfvars" -out="$WORK/d3.plan" -no-color | tee "$WORK/d3-plan.txt"
  reports plan-log "$WORK/d3-plan.txt" --allow-cluster
  date -u +%FT%TZ; terraform -chdir="$WORK/infra-mirror" apply "$WORK/d3.plan"; date -u +%FT%TZ
  until kubectl --kubeconfig "$KC_MIRROR" get crd nodepools.karpenter.sh aksnodeclasses.karpenter.azure.com; do sleep 10; done; date -u +%FT%TZ
  kubectl --kubeconfig "$KC_MIRROR" get nodes -o json > "$WORK/nodes-after-D3.json"
  reports boot-compare "$WORK/nodes-before-D3.json" "$WORK/nodes-after-D3.json"
  kubectl --kubeconfig "$KC_MIRROR" get nodepools.karpenter.sh
  ```

* **Expect:** a single in-place cluster update accepted on the mirror's tier (the Free-tier answer, written in the log); the update's duration and the time until both definitions exist (`timing`); `boot-compare` says whether existing nodes restarted;
  the fixed pools keep running (`nodes` recording: every node Ready); `get nodepools` lists none (default pools off). Enabling NAP **after** Cilium was accepted: the other order (NAP first) is **not** tried by this runbook.
* **Stop if:** the update is refused (write the exact message), the manager pod does not come back, or the plan wants to replace the cluster.
* **Rollback:** switching NAP off needs the clean-up (no NAP node left, every NodePool at `limits.cpu 0`), or delete the mirror (G9). A NAP cluster cannot be stopped.
* **Approves:** Owner.
* **Cost:** as C3.
* **Record:** `timing`, `nodes`, `az-aks`.

### D4. The NodePool objects

* **Needs:** D3.
* **Run:** set `worker_nodepool_enabled = true`, `family_vcpu_quota = <<FAMILY_QUOTA>>` (350 for `Dsv6`), `worker_pool_limits_cpu = 8`, `worker_pool_sku = "Standard_D4s_v6"`.

  ```sh
  terraform -chdir="$WORK/infra-mirror" plan -var-file="$WORK/mirror.tfvars" -out="$WORK/d4.plan" -no-color | tee "$WORK/d4-plan.txt"
  reports plan-log "$WORK/d4-plan.txt"
  terraform -chdir="$WORK/infra-mirror" apply "$WORK/d4.plan"
  live --kubeconfig "$KC_MIRROR" --record "$REC" --step D4 crds --crd nodepool
  live --kubeconfig "$KC_MIRROR" --record "$REC" --step D4 capture --kind nodepool
  kubectl --kubeconfig "$KC_MIRROR" get nodepool,aksnodeclass -o wide
  ```

* **Expect:** the plan creates exactly the NodeClass and the NodePool (`kubernetes_manifest.worker_nodeclass[0]`, `kubernetes_manifest.worker_nodepool[0]`) and the apply identity (you) may create the custom resources; `crds` prints `present` for every
  field of the Terraform: **where `expireAfter` lives** (`spec.template.spec`, as written; the `info` line says whether `spec.disruption.expireAfter` also exists), `podPidsLimit`, `startupTaints`, the API versions `v1` and `v1beta1`, the `WhenEmpty` value; the stored NodePool is the recording slice 22 reads; the object
  accepted `consolidationPolicy: WhenEmpty` with `consolidateAfter: 30s` (the documentation's disagreement settled for this combination).
* **Stop if:** `crds` prints `FAIL`: a field is where the Terraform does not write it. That is a change to `infra/worker_pool.tf` (a pull request), never an edit on the mirror that is then forgotten. Also stop if the apply cannot create the custom resources.
* **Rollback:** set `worker_nodepool_enabled = false` and apply (it deletes the NodePool; the mirror has no worker session, so no drain gate is needed).
* **Approves:** Runner.
* **Cost:** none until a pod needs a node.
* **Record:** `crd`, `nodepool`.

### D5. Cold start, allocatable and the size

* **Needs:** D4. A scratch namespace that the Pod Security admission lets run the test pods: `kubectl --kubeconfig "$KC_MIRROR" create namespace mirror-pool` (privileged is not needed for this step).
* **Run:** a pod that selects the pool, tolerates its taint and asks what the `small` class asks; **first a pause image** (it isolates provisioning), then the Factory image (`<<IMAGE_REF>>`, it adds the pull); repeat each **at least ten times at different hours**, deleting the pod and letting the node go in between.

  ```sh
  kubectl --kubeconfig "$KC_MIRROR" -n mirror-pool apply -f - <<'EOF'
  apiVersion: v1
  kind: Pod
  metadata: {name: cold-1}
  spec:
    restartPolicy: Never
    nodeSelector: {pool: worker}
    tolerations: [{key: workload, operator: Equal, value: worker, effect: NoSchedule}]
    containers:
      - name: p
        image: registry.k8s.io/pause:3.10
        resources: {requests: {cpu: "1", memory: 2Gi}, limits: {cpu: "1", memory: 2Gi}}
  EOF
  kubectl --kubeconfig "$KC_MIRROR" -n mirror-pool wait pod/cold-1 --for=condition=Ready --timeout=600s
  live --kubeconfig "$KC_MIRROR" --record "$REC" --step D5 capture --kind pod --name cold-1 --namespace mirror-pool
  live --kubeconfig "$KC_MIRROR" --record "$REC" --step D5 capture --kind pod-events --name cold-1 --namespace mirror-pool
  live --kubeconfig "$KC_MIRROR" --record "$REC" --step D5 capture --kind nodeclaim
  live --kubeconfig "$KC_MIRROR" --record "$REC" --step D5 nodes
  ```

* **Expect:** the pod is Ready from zero nodes in a time of the order the spike measured (about 100 s from zero nodes to a ready pod, and about 200 s per node launch, on `D4ds_v5` in an earlier spike; **not** measured on `D4s_v6`): the median and the maximum of ten runs go in the log, the raw pod,
  event and NodeClaim recordings are the evidence; `nodes` prints one worker node of size `Standard_D4s_v6` with its **allocatable** CPU and memory (the contract's 3.86 vCPU is from `D4ds_v5`: write down the real figure and what it means for pods per node), so the NodePool's `sku-name`
  requirement accepted the size and Karpenter acted on an unschedulable pod.
* **Stop if:** the pod stays Pending for more than 10 minutes: capture the `pod-events` and stop (the requirement may not accept the size, or the quota is short).
* **Rollback:** `kubectl --kubeconfig "$KC_MIRROR" -n mirror-pool delete pod cold-1`; the node goes after `consolidateAfter` (30 s).
* **Approves:** Runner.
* **Cost:** about $0.20 an hour while a node exists; ten runs are under a dollar.
* **Record:** `pod`, `pod-events`, `nodeclaim`, `nodes`, `timing`.

### D6. Limits, disruption and scale to zero

* **Needs:** D5.
* **Run:** each item is its own small experiment on the mirror with the scratch namespace; capture the pod, its events and the NodeClaims after each.
  1. **The pending reason at the pool's limit.** With `limits.cpu: 8` and nodes of about 4 vCPUs, create three pods that each request 3 CPUs: two get nodes, the third stays Pending. `capture --kind pod` and `--kind pod-events` for the Pending one (the `FailedScheduling` and
     Karpenter events carry the reason the `podstatus` table must read).
  2. **Drift, with and without `karpenter.sh/do-not-disrupt`.** Change `kubelet.podPidsLimit` of the AKSNodeClass with a `kubectl --kubeconfig "$KC_MIRROR" patch` of the object (a mutation of a Terraform-managed object on the **mirror**: `terraform apply` puts it back) while a pod with the annotation runs on one node and a pod without it on another; capture the NodeClaims and the pods.
  3. **Expiry ignores `do-not-disrupt`?** In a **scratch NodePool** `scratch-expire` (a copy of the stored NodePool with another name, `expireAfter: 5m` and `limits.cpu: 4`) run a pod with the annotation past five minutes; capture what happens. (The production pool uses `expireAfter: Never`.)
  4. **A PodDisruptionBudget** (`minAvailable: 1`) on a running pod, then a drift: does the budget hold the node?
  5. **`kubectl --kubeconfig "$KC_MIRROR" delete node <<NODE_NAME>>`** on a node with a pod: capture what the process sees (a pod that traps `SIGTERM` and prints a line with the time and exits with a code of your choice), the exit code and the pod's final state.
  6. **Scale to zero.** Delete every pod, time from the last pod gone to the NodeClaim gone to the VM gone; `kubectl --kubeconfig "$KC_MIRROR" get nodes -l pool=worker` returns nothing; a node that holds only DaemonSet pods (the Cilium agent) is consolidated; repeat with `consolidateAfter` 0 s, 30 s and 5 m on a scratch pool, and record which combinations the API accepts.
  7. **Metrics.** `az monitor diagnostic-settings list --resource "$(az aks show -g "$MIRROR_RG" -n "aks-$MIRROR_PREFIX" --query id -o tsv)"`; if no setting sends the `karpenter-events` category to the workspace, add one **on the mirror by hand**, repeat item 1 and read
     `AKSControlPlane | where Category == "karpenter-events" | take 20` in Log Analytics; write down which Karpenter metrics arrive (the list goes to the metrics work, issue #48).
* **Expect:** an event text for each item, written in the log and recorded; the Pending reason at the limit (item 1) and the eviction shape (item 5) are what `podstatus` classifies; a PDB and an expiring annotated pod behave as the NAP note's table says or do not (that is a finding).
* **Stop if:** an item would change something other than the mirror's scratch objects.
* **Rollback:** delete the scratch pods, PDB, scratch pool and (item 7) the diagnostic setting; `terraform apply` restores the AKSNodeClass.
* **Approves:** Runner.
* **Cost:** a few node-hours; under five dollars.
* **Record:** `pod`, `pod-events`, `nodeclaim`, `nodepool`, `timing`.

### D7. Registry pull and egress

* **Needs:** D5 (a node of the pool).
* **Run:** with the Factory image as in D5, no `imagePullSecrets`:

  ```sh
  kubectl --kubeconfig "$KC_MIRROR" -n mirror-pool describe pod cold-image-1 | sed -n '/Events:/,$p'
  kubectl --kubeconfig "$KC_MIRROR" -n mirror-pool exec cold-image-1 -- python3 -c "import urllib.request; print(urllib.request.urlopen('<<ECHO_URL>>', timeout=10).read().decode().strip())"
  az network public-ip show --resource-group "$MIRROR_RG" --name "pip-ngw-$MIRROR_PREFIX" --query ipAddress -o tsv
  ```

  (`<<ECHO_URL>>` is any HTTPS service that answers with the caller's address; compare it with the NAT gateway's address by eye and write `same` or `different` in the log. Do not paste the address into a file of the repository.)
* **Expect:** the image pulled without a secret (the `Pulled` event says how long: the cold and the warm pull), so the kubelet identity's `AcrPull` applies to NAP nodes; the address the echo service sees is the NAT gateway's; the node joined (the NSG rules do not block bootstrap).
* **Stop if:** `ErrImagePull`/`ImagePullBackOff`: the node's identity cannot pull; record the events and stop.
* **Rollback:** delete the test pod (`kubectl --kubeconfig "$KC_MIRROR" -n mirror-pool delete pod cold-image-1`).
* **Approves:** Runner.
* **Cost:** negligible.
* **Record:** `pod-events`, `timing`.

### D8. The worktree volume on the size without a local disk

* **Needs:** D5.
* **Run:** `live --kubeconfig "$KC_MIRROR" nodes` (the **ephemeral-storage** allocatable of the worker node in GiB), then a pod of the `standard` class with an `emptyDir` of 14Gi at `/data` that clones a medium and a large public repository and prints the time and the space:

  ```sh
  kubectl --kubeconfig "$KC_MIRROR" -n mirror-pool exec clone-1 -- sh -c 'cd /data && time git clone --quiet <<MEDIUM_REPO_URL>> m && time git clone --quiet <<LARGE_REPO_URL>> l && df -h /data'
  ```

* **Expect:** the node's allocatable ephemeral storage is at least the class's 20Gi plus room (the `s` sizes have no local temporary disk, so `emptyDir` lives on the OS disk: the number says how much); clone times for one medium and one large repository on `emptyDir`.
* **Stop if:** the allocatable ephemeral storage is below the `standard` class's request: the class cannot be scheduled on this size.
* **Rollback:** delete the clone pod and let its node go (30 seconds after the last pod).
* **Approves:** Runner.
* **Cost:** negligible.
* **Record:** `nodes`, `timing`.

### D9. S1 and the sandbox check on a pool node

* **Needs:** D5; a scratch namespace with the label `pod-security.kubernetes.io/enforce=privileged`.
* **Run:** a pod with the three relaxations of the worker pod (`hostUsers: false`, `procMount: Unmasked`, an `Unconfined` AppArmor profile) on the worker pool, running `sleep 3600`; then, as in `spikes/S1.md`:

  ```sh
  kubectl --kubeconfig "$KC_MIRROR" -n mirror-s1 exec s1 -- bash /opt/factory-spikes/s1_bwrap.sh
  kubectl --kubeconfig "$KC_MIRROR" -n mirror-s1 exec s1 -- factory-sandbox-check
  ```

  and, as the **negative control**, the same pod **without** the three relaxations: it must fail as the default pod did in the S1 runs, so the test is known to discriminate.
* **Expect:** `s1_bwrap.sh` 7 of 7 and `factory-sandbox-check` 27 PASS, 0 HARNESS on a node of the NodePool's size; the control fails. Record `uname -r`, the node image, the Kubernetes and bubblewrap versions.
* **Stop if:** S1 fails on `Standard_D4s_v6`: the default size is not usable for workers; the Owner chooses another size and D5 to D9 are repeated.
* **Rollback:** delete the pods and the namespace.
* **Approves:** Runner.
* **Cost:** negligible.
* **Record:** `probe-output`.

## Part E: the worker namespace and the admission policies on the mirror

### E1. Enable the worker namespace and the policies

* **Needs:** D4 (the preconditions of the namespace are Cilium, NAP `Auto` and the pool); slices 16 and 17 in the tree (A2): this one flip also creates the launcher's Role and RoleBinding and **rolls the manager pod** with the launcher and relay containers (nothing of value runs on the mirror's manager).
* **Run:** set `pod_backend_enabled = true`.

  ```sh
  terraform -chdir="$WORK/infra-mirror" plan -var-file="$WORK/mirror.tfvars" -out="$WORK/e1.plan" -no-color | tee "$WORK/e1-plan.txt"
  reports plan-log "$WORK/e1-plan.txt" --allow-deployment
  mirror_guard && mirror_tfvars_ok && terraform -chdir="$WORK/infra-mirror" apply "$WORK/e1.plan"
  kubectl --kubeconfig "$KC_MIRROR" -n factory rollout status deploy/factory
  kubectl --kubeconfig "$KC_MIRROR" get namespace factory-workers --show-labels
  kubectl --kubeconfig "$KC_MIRROR" -n factory-workers get serviceaccount,networkpolicy,configmap,role,rolebinding
  kubectl --kubeconfig "$KC_MIRROR" get validatingadmissionpolicy,validatingadmissionpolicybinding
  kubectl --kubeconfig "$KC_MIRROR" -n factory get service factory-relay-svc
  kubectl --kubeconfig "$KC_MIRROR" -n factory get pod -l app.kubernetes.io/name=factory -o jsonpath='{range .items[0].spec.containers[*]}{.name}{"\n"}{end}{range .items[0].spec.initContainers[*]}init {.name}{"\n"}{end}'
  ```

* **Expect:** the plan creates the namespace, the service account, the default-deny policy, the parameter ConfigMap and the four policy objects, the Role and the RoleBinding `factory-launcher` (slice 16) and the Service `factory-relay-svc` (slice 17), **updates in place** the agent ConfigMap (`kubernetes_config_map_v1.insights_agent`)
  and the manager Deployment (`kubernetes_manifest.factory_deployment`: the containers `launcher` and `relay`, the init container `volume-perms`, the projected token volume, the two `emptyDir`s, the toleration and the environment of the main container; the plan-log is told so with `--allow-deployment`, which allows nothing else),
  and touches nothing else; the `kubernetes_manifest` provider **accepted** the `admissionregistration.k8s.io/v1` kinds (plan and apply); the namespace objects exist and both policies are bound; the manager rolled out with the containers `factory`, `launcher` and `relay` and the init containers of the Key Vault volume and `volume-perms`.
  A plan that changes the Deployment **without** `pod_backend_enabled` is not this step: it is a defect (the default plan has no line for it).
* **Stop if:** the plan or the apply fails on an admission kind (record the message as a `note`: it is the answer to "does the provider path work", and the fix is a change of `infra/worker_policy.tf`, not an edit here); the plan-log fails for anything but the Deployment; the manager pod does not come back
  (`kubectl --kubeconfig "$KC_MIRROR" -n factory describe pod`: the kubelet may refuse the pod as written, which is the UNVERIFIED of slice 17: record the events).
* **Rollback:** set `pod_backend_enabled = false` and apply: it deletes the namespace and everything in it (the mirror has no session) and rolls the manager back to its pod without the two containers.
* **Approves:** Owner (the first enabling flip anywhere).
* **Cost:** none.
* **Record:** `plan-actions`.

### E2. The log agent and the exclusion

* **Needs:** E1.
* **Run:**

  ```sh
  kubectl --kubeconfig "$KC_MIRROR" -n kube-system get pods -l component=ama-logs-agent -o jsonpath='{range .items[*]}{.status.startTime}{"\n"}{end}'
  kubectl --kubeconfig "$KC_MIRROR" -n kube-system get configmap container-azm-ms-agentconfig -o jsonpath='{.data.log-data-collection-settings}'
  MARK=factory-exclusion-$(date -u +%s)
  kubectl --kubeconfig "$KC_MIRROR" -n factory-workers run marker --image=<<IMAGE_REF>> --restart=Never --command -- python3 -c "print('$MARK')"
  kubectl --kubeconfig "$KC_MIRROR" -n mirror-pool run marker-control --image=<<IMAGE_REF>> --restart=Never --command -- python3 -c "print('$MARK-control')"
  # after ten minutes:
  az monitor log-analytics query --workspace <<WORKSPACE_ID>> --analytics-query "ContainerLogV2 | where LogMessage startswith 'factory-exclusion' | summarize count() by PodNamespace" -o json
  ```

* **Expect:** the agent pods **restarted** after the apply (a ConfigMap change restarts the agent; start times later than the apply), the setting carries `exclude_namespaces` under stdout and stderr with all three names (`kube-system`, `gatekeeper-system`, `factory-workers`: the key spelling is accepted); the marker line of the worker namespace is **absent** from Log Analytics and the control
  line of the scratch namespace is **present** (the control proves the query can see a line at all).
* **Stop if:** the worker marker is present: worker output reaches Log Analytics, which the design says must not happen; the Owner decides (the transcript goes to the relay, so a stray line is the second layer).
* **Rollback:** delete the marker pods.
* **Approves:** Runner.
* **Cost:** none.
* **Record:** `note` (the two counts).

### E3. The network fence, from inside a pod

* **Needs:** E1, D2.
* **Run:** a hand-made probe pod **in the worker namespace** (the namespace's default-deny policy selects every pod there; the admission policy binds only the launcher identity, so you are not bound), with the production image, `enableServiceLinks: false`, the pool's selector and toleration and two downward-API variables:

  ```sh
  kubectl --kubeconfig "$KC_MIRROR" -n factory-workers apply -f - <<'EOF'
  apiVersion: v1
  kind: Pod
  metadata: {name: fence-probe, labels: {factory.internal/role: worker}}
  spec:
    restartPolicy: Never
    enableServiceLinks: false
    nodeSelector: {pool: worker}
    tolerations: [{key: workload, operator: Equal, value: worker, effect: NoSchedule}]
    containers:
      - name: p
        image: <<IMAGE_REF>>
        command: [sleep, "3600"]
        env:
          - {name: FACTORY_NODE_IP, valueFrom: {fieldRef: {fieldPath: status.hostIP}}}
          - {name: FACTORY_MANAGER_ADDRESS, value: "<<MANAGER_POD_IP>>"}
  EOF
  kubectl --kubeconfig "$KC_MIRROR" -n factory-workers exec -i fence-probe -- python3 - network --control-host <<EGRESS_CONTROL_HOST>> --api-public-host <<API_PUBLIC_HOST>> < "$TREE/tools/live_pod_probe.py" | tee "$WORK/e3-fenced.txt"
  # the negative control: the same pod, same probe, in a namespace WITHOUT the fence (mirror-pool): it must FAIL on the forbidden destinations
  ```

  (`<<MANAGER_POD_IP>>`: `kubectl --kubeconfig "$KC_MIRROR" -n factory get pod -l app.kubernetes.io/name=factory -o jsonpath='{.items[0].status.podIP}'`; `<<API_PUBLIC_HOST>>`: `az aks show -g "$MIRROR_RG" -n "aks-$MIRROR_PREFIX" --query fqdn -o tsv`; run the unfenced copy as `e3-unfenced.txt`.)
* **Expect:** in the fenced namespace every forbidden destination of the contract (the metadata address, the Azure host channel on 80 and 32526, the node's kubelet port, the manager's two ports) is `blocked`, the unauthenticated request to the API server at its service address and at its public
  address gets no connection, 401 or 403, DNS resolves, the control host on 443 connects (so a dead network cannot pass as an isolated one); in the unfenced copy the forbidden destinations are `connected` (**the probe discriminates**). That settles whether Cilium enforces `ipBlock.except` here (the service and node ranges), whether the DNS rule still matches, whether the API server's public address is refused without a credential, and exercises each destination of the canary's list (the list's
  completeness is a design judgement no run proves).
* **Stop if:** a forbidden destination is `connected` in the fenced namespace, or the API server answers 200 or 404 to the unauthenticated request: the fence has a hole; the Owner decides before anything else is enabled.
* **Rollback:** `kubectl --kubeconfig "$KC_MIRROR" -n factory-workers delete pod fence-probe`.
* **Approves:** Runner.
* **Cost:** one node while the pod runs.
* **Record:** `probe-output` (both outputs), `canary`.

### E4. The admission matrix on the mirror

* **Needs:** E1 (the Role and RoleBinding `factory-launcher` of slice 16 exist: they are what lets the launcher identity create a pod and a Secret in the worker namespace) and the matrix prerequisite of A2 (kind). You need the right to impersonate the launcher identity (`system:serviceaccount:factory:factory`); the mirror's Runner holds cluster admin, which carries it. A `cannot impersonate` is an access problem, not a policy result.
* **Run:** the Terraform already created the namespace, the service account, the parameter ConfigMap and the launcher's Role and RoleBinding, so **nothing of the matrix's fixtures is applied** (its `matrix-launcher` Role grants `create` only and is for a cluster that has no Role of its own, such as kind). Point the parameter at the golden image **for the length of the run** (the golden pods carry a placeholder image; the policy compares a pod's image with the parameter):

  ```sh
  cd "$TREE"
  kubectl --kubeconfig "$KC_MIRROR" auth can-i create pods -n factory-workers --as system:serviceaccount:factory:factory
  kubectl --kubeconfig "$KC_MIRROR" -n factory-workers get configmap worker-policy-params -o jsonpath='{.data.image}' > "$WORK/param-image.txt"
  kubectl --kubeconfig "$KC_MIRROR" -n factory-workers patch configmap worker-policy-params --type merge -p "{\"data\":{\"image\":\"$(live golden-image)\"}}"
  python3 tools/policy_matrix.py --kubeconfig "$KC_MIRROR" status
  python3 tools/policy_matrix.py --kubeconfig "$KC_MIRROR" run | tee "$WORK/e4-matrix.txt"
  live --kubeconfig "$KC_MIRROR" pod-fields
  kubectl --kubeconfig "$KC_MIRROR" -n factory-workers patch configmap worker-policy-params --type merge -p "{\"data\":{\"image\":\"$(cat "$WORK/param-image.txt")\"}}"
  record import --step E4 --kind note --out "$REC" "$WORK/e4-matrix.txt"
  ```

  (The full check of the Role, every verb it grants and every one it does not, is G1: the matrix needs only `create`.)
* **Expect:** `auth can-i create pods` prints `yes` (a native Role works next to Azure RBAC for Kubernetes); `status` finds no type-check warning but the three tolerated ones; `run` ends `matrix: 12 golden admitted, <<N>> denied by a rule, <<N>> no effect, <<N>> not exercised, 0 problems`; compare the counts with the kind run of A2 (172, 6, 37): a **difference is a finding** (E7), not an error of the tool.
  The golden pods being **admitted** settles that a real API server accepts the pod as written (field names, `appArmorProfile` and `procMount` with `hostUsers: false`); every mutation denied naming its rule settles that the CEL is evaluated; the "no effect" lines list what the server defaults; "not exercised" includes the ephemeral-container mutation (rejected before the policy). `pod-fields` lists the fields of this cluster's pod API
  that neither the policy nor a golden pod names (each is **not refused** today): the output is empty or a list for the Owner.
* **Stop if:** `auth can-i create pods` prints `no` (the Role is missing: E1 did not make it; do not apply the matrix's own Role to patch it over), a golden object is denied, a mutation is admitted, or `pod-fields` lists a field: the Owner decides whether it is named in the policy (a change to `infra/policy/pod-policy.json`, the golden files and the mirror) or accepted with a reason; never silently.
* **Rollback:** the last patch restores the parameter; confirm with `terraform plan -var-file="$WORK/mirror.tfvars" -detailed-exitcode` (exit 0).
* **Approves:** Runner.
* **Cost:** none.
* **Record:** `note` (the matrix output), `denial` (E6).

### E5. The parameter update during an image promotion

* **Needs:** E4 (the policy is bound to the image of the parameter).
* **Run:** two golden-shaped pods, one with the image the Deployment runs now (`<<IMAGE_A>>`) and one with another digest of the same repository (`<<IMAGE_B>>`, for example the previous production digest), are dry-run created every five seconds as the launcher identity **while** you promote on the mirror:

  ```sh
  for IMG in A B; do python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); d["spec"]["containers"][0]["image"]=sys.argv[2]; print(json.dumps(d))' \
    "$TREE/tests/golden/pod_standard_build.json" "<<IMAGE_$IMG>>" > "$WORK/golden-$IMG.json"; done
  while true; do for IMG in A B; do printf '%s %s ' "$(date -u +%T)" "$IMG"; kubectl --kubeconfig "$KC_MIRROR" --as system:serviceaccount:factory:factory \
    create --dry-run=server -f "$WORK/golden-$IMG.json" -o name 2>&1 | head -c 120; echo; done; sleep 5; done | tee "$WORK/e5-timeline.txt"
  # in another terminal: set image_digest to <<DIGEST_B>> in the scratch copy's image.auto.tfvars, apply, wait for the rollout, then put it back and apply again
  ```

* **Expect:** a timeline: which image is admitted when. A **bounded window** in which pods with the new (or the old) image are denied (`[rule:image]`) between the parameter's update and the launcher's restart is possible; its length goes in the log. The golden
  pod's image placeholder is the reason this runs with the parameter at the real image: restore E4's patch first.
* **Stop if:** both images are denied for longer than a minute after the rollout finished: the parameter and the launcher disagree; the Owner decides before any promotion with the backend on.
* **Rollback:** `image_digest` back to the committed digest and apply.
* **Approves:** Owner (a promotion on the mirror).
* **Cost:** none.
* **Record:** `timing` (the window).

### E6. The denial text as the API server writes it

* **Needs:** E4.
* **Run:** `live --kubeconfig "$KC_MIRROR" --record "$REC" --step E6 denial-sample`
* **Expect:** `denial: policy factory-worker-pods, N rule ids named` (N at least 1), and a `denial` recording with the real first line, which shows whether the rule ids really come in square brackets (`[rule:<id>]`) as the client of slice 7 assumes.
* **Stop if:** the probe pod is admitted (the policy is not enforcing) or the message names no bracketed id.
* **Rollback:** none (a server-side dry run persists nothing).
* **Approves:** Runner.
* **Cost:** none.
* **Record:** `denial`.

### E7. The matrix totals against the kind run

* **Needs:** E4.
* **Run:** write down, side by side, the kind run (A2) and the AKS run: golden admitted, denied, no effect, not exercised, problems; the Kubernetes server version of each (`kubectl --kubeconfig "$KC_MIRROR" version -o json`, the pinned node image of `tools/local_pod_e2e/kind.yaml`), and the list of mutations whose class differs.
* **Expect:** the same 12 golden pods admitted and no problem; differences in `no effect` or `not exercised` are the differences of schema or CEL semantics between the two servers (write each).
* **Stop if:** a mutation is denied on kind and admitted on AKS.
* **Rollback:** none, read-only.
* **Approves:** Owner (reads the differences).
* **Cost:** none.
* **Record:** `note`.

## Part F: the plan identity (a hard gate)

**The plan on the mirror, run as the plan identity, is a hard gate.** No production enabling flip of `pod_backend_enabled`, no apply of the admission policies in production and no update of the production role happens before F3 has passed on the mirror and the Owner has said so
in writing. The reason is in `infra/README.md` ("Custom role"): the first plan after the flip, and the preview plan of the apply run, read the Role, the RoleBinding and the two admission kinds of `admissionregistration.k8s.io` as the **read-only plan identity**, and **the two admission kinds have no data action in the public Azure permissions reference**: whether the Azure authorizer maps
`validatingadmissionpolicies` and `validatingadmissionpolicybindings` to `customresources/read` (already granted) is UNVERIFIED. If it does not, the plan fails with `Forbidden` and the answer is **never to widen the role by guessing an action name**: a role definition that names an action the provider does not define is refused and would block the two names that are valid.

### F1. The mirror's plan identity and role

* **Needs:** E1 (a mirror whose state holds the namespace and the policies, so a plan has something to read); a role that can create role definitions and assignments on `$MIRROR_RG` **and on the registry**; **the Owner's written approval of the production write this step makes**: the `Reader` assignment of the mirror's
  plan identity on the **production registry** (the plan reads the registry by name, `data.azurerm_container_registry`; G9 deletes it). Write the approval in the log before the first command.
* **Run:** a service principal for the mirror only, with a client secret that lives **one day** and **only in one shell's environment**; the role definition from `reports mirror-role` (another name, the mirror's scope), and the roles of the infra README's table for the plan identity, on the mirror's resource group. Every command
  that creates starts with a guard, and `$APP_ID` goes into the names file so that G9 (a new shell) can delete exactly this application:

  ```sh
  mirror_guard && reports mirror-role --prefix "$MIRROR_PREFIX" --subscription <<SUBSCRIPTION_ID>> --resource-group "$MIRROR_RG" > "$WORK/mirror-role.json"
  mirror_guard && az role definition create --role-definition "$WORK/mirror-role.json"
  APP_ID=$(mirror_guard && az ad app create --display-name "sp-plan-$MIRROR_PREFIX" --query appId -o tsv)
  mirror_app_is_ours && echo "export APP_ID=$APP_ID" >> "$WORK/mirror-names.sh"
  mirror_app_is_ours && az ad sp create --id "$APP_ID"
  PLAN_SECRET=$(mirror_app_is_ours && az ad app credential reset --id "$APP_ID" --end-date <<TOMORROW_YYYY_MM_DD>> --query password -o tsv)
  SCOPE=/subscriptions/<<SUBSCRIPTION_ID>>/resourceGroups/$MIRROR_RG
  for ROLE in "Reader" "Azure Kubernetes Service Cluster User Role" "Azure Kubernetes Service RBAC Reader" "Factory Plan Cluster Reader $MIRROR_PREFIX"; do
    mirror_app_is_ours && az role assignment create --assignee "$APP_ID" --role "$ROLE" --scope "$SCOPE"
  done
  # a PRODUCTION write (the registry is production's): only with the Owner's written approval, and G9 step 2 deletes it
  mirror_app_is_ours && az role assignment create --assignee "$APP_ID" --role "Reader" --scope "$(az acr show --name <<ACR_NAME>> --query id -o tsv)"
  mirror_app_is_ours && az role assignment create --assignee "$APP_ID" --role "Storage Blob Data Reader" \
    --scope "$(az storage account show --name <<TFSTATE_STORAGE_ACCOUNT>> --query id -o tsv)/blobServices/default/containers/$MIRROR_STATE_CONTAINER"
  AZURE_CONFIG_DIR="$WORK/az-plan" az login --service-principal --username "$APP_ID" --password "$PLAN_SECRET" --tenant <<TENANT_ID>>
  unset PLAN_SECRET
  ```

  (`mirror-role` refuses a prefix that is not a `name_prefix`; the name it prints is `Factory Plan Cluster Reader $MIRROR_PREFIX`, never the production name; the role has the seven data actions of the repository file and no admission action. **Do not** pass `--role "Factory Plan Cluster Reader"`: that is production's.)
  Role assignments take a few minutes; the plan identity must **not** be listed in `aks_cluster_admin_principal_ids`.
* **Expect:** `az role definition list --name "Factory Plan Cluster Reader $MIRROR_PREFIX"` shows the mirror role; the production role is unchanged (`reports role-diff` of a fresh `az role definition list --name "Factory Plan Cluster Reader"` equals the B5 result); `az role assignment list --assignee "$APP_ID" --all -o table` lists the five assignments
  you made (four on the mirror's group, one on the registry) and the one on the mirror's state container: write the registry one in the log.
* **Stop if:** the production role changed (you used the wrong name): restore it from `role-before.json` (F4's rollback) and tell the Owner; a guard refuses; an assignment would be made on a scope that is neither the mirror's group, the mirror's state container nor the registry; the Owner has not approved the registry assignment.
* **Rollback:** the deletes of G9 (step 2 for the assignments on the registry, step 4 for the role definition and the application), which carry the guards; run those two steps now and not the whole teardown.
* **Approves:** Owner (a production write: the `Reader` assignment on the registry).
* **Cost:** none.
* **Record:** `az-role` (the mirror role).

### F2. Reads as the plan identity

* **Needs:** F1 and five minutes for the assignments.
* **Run:**

  ```sh
  AZURE_CONFIG_DIR="$WORK/az-plan" az aks get-credentials --resource-group "$MIRROR_RG" --name "aks-$MIRROR_PREFIX" --file "$WORK/kubeconfig-plan"
  AZURE_CONFIG_DIR="$WORK/az-plan" kubelogin convert-kubeconfig -l azurecli --kubeconfig "$WORK/kubeconfig-plan"
  AZURE_CONFIG_DIR="$WORK/az-plan" live --kubeconfig "$WORK/kubeconfig-plan" reads --profile plan
  ```

* **Expect:** `read <kind>: ok` for every kind the plan reads: the namespace, the service account, the network policy, the Role and RoleBinding of the worker namespace, the custom resource definitions, the NodePool and the AKSNodeClass (so the plan identity can read the new custom resources), **and the two admission kinds**.
* **Stop if:** any line is `forbidden`: write down which, stop, and go to F3's stop rule. Do not edit the role.
* **Rollback:** none, read-only.
* **Approves:** Runner.
* **Cost:** none.
* **Record:** `probe-output` (the output of the reads check).

### F3. HARD GATE: `terraform plan` as the plan identity on the mirror

* **Needs:** F2 passed; the mirror's configuration with every variable on (`pod_backend_enabled`, the pool, Cilium, NAP) and its state; the names file sourced (`. "$WORK/mirror-names.sh"`) and `mirror_guard` accepted.
* **Run:**

  ```sh
  export AZURE_CONFIG_DIR="$WORK/az-plan"
  terraform -chdir="$WORK/infra-mirror" init -reconfigure \
    -backend-config="resource_group_name=<<TFSTATE_RESOURCE_GROUP>>" -backend-config="storage_account_name=<<TFSTATE_STORAGE_ACCOUNT>>" \
    -backend-config="container_name=$MIRROR_STATE_CONTAINER" -backend-config="key=<<MIRROR_STATE_KEY>>" -backend-config="use_azuread_auth=true"
  terraform -chdir="$WORK/infra-mirror" plan -lock=false -input=false -no-color -detailed-exitcode -var-file="$WORK/mirror.tfvars" > "$WORK/f3-plan.txt" 2>&1; echo "exit $?"
  unset AZURE_CONFIG_DIR
  reports plan-log "$WORK/f3-plan.txt" --expect-no-changes
  ```

  (`-lock=false` is what a plan with the read-only plan identity should use: a plan never writes state, so the plan identity has no write role.)
* **Expect:** exit 0 and `plan-log: passed`: **no `Forbidden`, no error, no change**: the plan identity reads the Role, the RoleBinding **and `validatingadmissionpolicies` and `validatingadmissionpolicybindings`**, so the Azure authorizer covers them with `customresources/read` and the repository's role file needs no further action. That is also the settled answer on the data-action names.
* **Stop if:** `Forbidden` on either admission kind, or any error. **Stop and report; do not widen the role by guessing a name.** The Owner's options are (a) ask Microsoft for the data action of those two kinds, (b) a change of `infra/worker_policy.tf` so that the plan does not read them (a pull request, then F1 to F3 again), (c) not enable the backend. Nothing in F4 or in production is done meanwhile.
  Save the plan text (it has no secret) with `record import --step F3 --kind plan-actions --out "$REC" "$WORK/f3-plan.txt"`.
* **Rollback:** none, read-only.
* **Approves:** Owner, in writing, before F4 and before any production flip.
* **Cost:** none.
* **Record:** `plan-actions`.

### F4. The production role (a human action, only after F3)

* **Needs:** F3 **passed** and the Owner's written approval; B5 showed the live role **missing** actions (if it equals the repository file there is nothing to do: write that in the log and stop here); a role that can write role definitions on the production resource group.
* **Run:**

  ```sh
  SUB=<<YOUR_SUBSCRIPTION_ID>>; RG=<<YOUR_PRODUCTION_RESOURCE_GROUP>>        # the file's own placeholders are <<SUBSCRIPTION_ID>> and <<RESOURCE_GROUP>>
  ROLE_FILE=$(mktemp)
  sed -e "s|<<SUBSCRIPTION_ID>>|$SUB|" -e "s|<<RESOURCE_GROUP>>|$RG|" "$TREE/infra/roles/factory-plan-cluster-reader.json" > "$ROLE_FILE"
  az role definition update --role-definition "$ROLE_FILE"
  az role definition list --name "Factory Plan Cluster Reader" -o json > "$WORK/role-after.json"
  reports role-diff "$WORK/role-after.json"
  rm -f "$ROLE_FILE"
  ```

  A new data action takes a few minutes to reach the plan identity's tokens: re-run a failed plan afterwards (`infra/README.md`, "Custom role").
* **Expect:** `role-diff: the live role equals the repository file`; the next `terraform plan` of your pipeline (the plan step with the read-only identity) still passes.
* **Stop if:** `az role definition update` is refused (a data action name that does not exist): do not try another name; tell the Owner.
* **Rollback:** the file as it was before the added actions, with the same two `sed` substitutions: `git -C "$TREE" show <<COMMIT_BEFORE_THE_CHANGE>>:infra/roles/factory-plan-cluster-reader.json | sed -e "s|<<SUBSCRIPTION_ID>>|$SUB|" -e "s|<<RESOURCE_GROUP>>|$RG|" > "$ROLE_FILE"`, then `az role definition update --role-definition "$ROLE_FILE"` (`role-before.json` from B5 shows what it was).
* **Approves:** Owner (a production write: the production role; the other two are the registry assignments of C4 and F1).
* **Cost:** none.
* **Record:** `az-role`.

## Part G: the session path on the mirror, and the teardown

**Part G needs slices 12b, 16 and 17 delivered and applied on the mirror** (A2; the program table in `docs/pod-backend.md` says which are delivered, and at the time of writing all three are in the tree). If one of them is not, G is **not run** and the table rows that name a G step stay UNVERIFIED. G9 (the teardown) is always run. Part G is written against the slices as they are in the tree:

* **The manager pod** has the containers `factory` (the main one, as before, with the supervisor and every in-process worker), `launcher` (`python -m factory.launcher`, uid 10003) and `relay` (`python -m factory.relay`, uid 10004), and the init containers of the Key Vault volume and `volume-perms` (`infra/pod_backend.tf`). The two `emptyDir`s are `/run/factory` (the launcher's socket, owner 10003, group 10001, mode 0750, read-only in the main container) and `/spool` (the relay's, owner 10004, group 10001, mode 2770). The relay is reached through the Service `factory-relay-svc` (not `factory-relay`, on purpose: a Service called `factory-relay` would inject a `FACTORY_RELAY_PORT`).
* **The settings come with E1** and are never set by hand: `pod_backend_enabled = true` puts `FACTORY_POD_BACKEND=on`, `FACTORY_MAX_POD_SESSIONS`, `FACTORY_POD_LAUNCHER_SOCKET` (`/run/factory/launcher.sock`) and `FACTORY_POD_SPOOL_DIR` (`/spool`) into the main container, and the launcher's own environment (the image, the namespace, the pod ceiling, the per-repository cap, the deadline clamp, the allowed peer uid 10001, the relay URL, the resource class, the API server's public host, the manager address and the socket) into the `launcher` container. The only repository-level setting is `execution = "pod"` in the sandbox repository's entry (`repo_configs` of `mirror.tfvars`, by hand: `docs/enrolling-repositories.md`).
* **The Role** is `factory-launcher` (with a RoleBinding of the same name) in `factory-workers`, bound to the service account `factory` of the manager namespace: pods create, get, list and delete, Secrets create and delete, nothing else. Only the `launcher` container mounts a token for that account (a projected volume at the standard path); the main container and the relay hold no Kubernetes credential.
* **Uids and groups** (the contract's): supervisor 10001 (group 10001), worker 10002 (group 10010), launcher 10003, relay 10004.

### G1. Identities: what the launcher may do

* **Needs:** E1 applied with slice 16's Role (`kubectl --kubeconfig "$KC_MIRROR" -n factory-workers get role,rolebinding factory-launcher` answers; this is where the full launcher check belongs: E4 has only the Role's `create`).
* **Run:**

  ```sh
  kubectl --kubeconfig "$KC_MIRROR" -n factory-workers get role,rolebinding factory-launcher
  live --kubeconfig "$KC_MIRROR" can-i --profile launcher
  live --kubeconfig "$KC_MIRROR" can-i --profile worker
  ```

* **Expect:** every answer as expected: the launcher may create, get, list and delete pods and create and delete Secrets in `factory-workers`, and may **not** get or list Secrets, exec, read logs, update, patch or watch pods, or touch the `factory` namespace; the worker service account may do nothing at all. A Role (not Azure RBAC) was honoured by the mirror's authorizer next to Azure RBAC for Kubernetes.
* **Stop if:** any `FAIL can-i`, or the Role or the RoleBinding is missing.
* **Rollback:** none, read-only.
* **Approves:** Runner.
* **Cost:** none.
* **Record:** `probe-output`.

### G2. Socket, spool and the peer credentials

* **Needs:** G1; the manager pod with its containers `factory`, `launcher` and `relay` (E1).
* **Run:** the probe is fed to an interpreter inside the pod; an `exec` into the main container runs as root inside the pod's user namespace, which may switch identity (`CAP_SETUID`), so each probe names the identity it asks as. **The supervisor line names its own uid and gid** (10001 and 10001: the main container's root would pass every permission check and report a directory it should not be able to write as writable).

  ```sh
  P="kubectl --kubeconfig $KC_MIRROR -n factory exec -i deploy/factory"
  $P -c factory -- python3 - socket --expect worker     --as-uid 10002 --as-gid 10010 < "$TREE/tools/live_pod_probe.py"
  $P -c factory -- python3 - socket --expect supervisor --as-uid 10001 --as-gid 10001  < "$TREE/tools/live_pod_probe.py"
  $P -c factory -- python3 - spool  --expect worker     --as-uid 10002 --as-gid 10010 < "$TREE/tools/live_pod_probe.py"
  $P -c relay   -- python3 - environment                                               < "$TREE/tools/live_pod_probe.py"
  $P -c launcher -- python3 - environment                                              < "$TREE/tools/live_pod_probe.py"
  ```

  (If the relay or launcher image has no `python3` on the path, use the image's own interpreter, `/opt/factory/bin/python`.)
* **Expect:** as uid 10002, connecting to the launcher socket, creating, binding or writing in `/run/factory` and listing or writing `/spool` are all `refused`; as the supervisor (uid 10001, group 10001: the group of the socket directory) the connect is `allowed` and the directory stays closed. In the relay container the environment has **no** `AZURE_` name (the workload identity webhook left it alone) and the relay's
  port variable is not a Service-injected `tcp://` value. A real launch in G4 is the positive evidence of what `SO_PEERCRED` reports for the supervisor across containers that share the `emptyDir` under `hostUsers: false`.
* **Stop if:** uid 10002 can reach the socket or the spool, or the relay container has an `AZURE_` name (it refuses to start, and so would you).
* **Rollback:** none (the probe's attempts are refusals or are removed again).
* **Approves:** Runner.
* **Cost:** none.
* **Record:** `probe-output`.

### G3. The API's answers to the launcher's client

* **Needs:** G1.
* **Run:** in the launcher container, with its own service account token:

  ```sh
  kubectl --kubeconfig "$KC_MIRROR" -n factory exec -i deploy/factory -c launcher -- python3 - statuses --bodies < "$TREE/tools/live_pod_probe.py" > "$WORK/g3-statuses.json"
  kubectl --kubeconfig "$KC_MIRROR" create configmap probe-exists --from-literal=a=b -n mirror-pool
  kubectl --kubeconfig "$KC_MIRROR" create configmap probe-exists --from-literal=a=b -n mirror-pool 2> "$WORK/g3-409.txt"
  record import --step G3 --kind kube-status --out "$REC" "$WORK/g3-statuses.json"
  record import --step G3 --kind kube-status --out "$REC" "$WORK/g3-409.txt"
  ```

* **Expect:** the four cases answer 404 (a pod that does not exist), 403 (list Secrets), 401 (an invalid token) and 422 or 403 for a policy denial (record which; the client reads 422), the bodies are `Status` objects, and the 409 text says `AlreadyExists`. The projected token and the CA file are at the standard paths (the probe read them), and the TLS handshake with the real API server succeeded
  (the client's HTTPS path).
* **Stop if:** a case answers something the launcher's client does not map (a 5xx, a redirect): record it and tell the Owner.
* **Rollback:** `kubectl --kubeconfig "$KC_MIRROR" -n mirror-pool delete configmap probe-exists`.
* **Approves:** Runner.
* **Cost:** none.
* **Record:** `kube-status`.

### G4. One real session, end to end

* **Needs:** G1 to G3; the sandbox repository enrolled on the mirror (`docs/enrolling-repositories.md`) with `execution = "pod"` in its `repo_configs` entry (add it, apply, and the manager rolls: the settings of the backend are already on from E1); a small issue in it; the throwaway key's workspace limit set (B8).
* **Run:** start a session the usual way (label the issue; `docs/operations.md`, "Start a run"), watch it, and capture each stage:

  ```sh
  kubectl --kubeconfig "$KC_MIRROR" -n factory-workers get pods -w
  live --kubeconfig "$KC_MIRROR" --record "$REC" --step G4 capture --kind pod --name <<POD_NAME>>
  live --kubeconfig "$KC_MIRROR" --record "$REC" --step G4 capture --kind pod-events --name <<POD_NAME>>
  kubectl --kubeconfig "$KC_MIRROR" -n factory-workers exec <<POD_NAME>> -- ls -ln /usr/bin/tini /opt/factory/bin/python     # may be refused by the pod's own exec rules: then read the pod's logs instead
  kubectl --kubeconfig "$KC_MIRROR" -n factory logs deploy/factory -c relay --tail=200
  ```

  Then, with a **real, medium repository** instead of the sandbox one (the Owner's choice), measure: the **bundle** size and the time to stage and fetch it, the **delta** size and the time to validate and apply it, the transcript size, the disk the shallow fetch used in the pod (`df` before and after), the `git --version` of the manager and the pod
  images, the worktree's owner in the pod, the time between the pod's creation and its Secret being usable, and the time from the end of the session to the Secret being gone. Compare the same issue run `inprocess` and `pod`: `git status` and `git diff` of the two worktrees, staged work, the configuration, the refs beyond the shallow boundary and a review diff over a merge history.
* **Expect:** the session completes through the relay; the supervisor (not the pod) commits and pushes; the pod's command and `tini` path exist; the pod and its events carry the labels (`factory.internal/` needs no domain of the owner); whether the transcript holds a chunk twice (a lost answer: the line reader keeps the whole copy; count them); a Secret created after its pod is retried by the kubelet (write down the delay); the Secret goes with the pod (write down the delay); the numbers go
  into `timing` recordings with the repository's rough size (no name): bundle bytes and seconds, delta bytes and seconds, the memory of the validation, relay throughput, the disk of the fetch; the in-process and pod results differ only in the ways `docs/pod-backend.md` lists (anything else is a finding); `delta too large` appears or does not on a repository that installs a large dependency tree; the PROVISIONAL caps, classes and timing constants have a measurement beside them.
* **Stop if:** the session leaves a pod running after the manager says it ended (go to G7 and G9 now), a second repository's data is visible in the pod, or the spend of the throwaway key passes the Owner's cap.
* **Rollback:** `kubectl --kubeconfig "$KC_MIRROR" -n factory-workers delete pod <<POD_NAME>>`; purge the run.
* **Approves:** Owner (spend on a real repository).
* **Cost:** the Anthropic spend of the session (bounded by the workspace limit) and a node for its duration.
* **Record:** `pod`, `pod-events`, `timing`.

### G5. Inside a worker pod

* **Needs:** a running worker pod (G4).
* **Run:** the probes that need the pod's own view, with the pod's wrapper still running:

  ```sh
  W="kubectl --kubeconfig $KC_MIRROR -n factory-workers exec -i <<POD_NAME>>"
  $W -- python3 - secret-volume --as-uid 10002 --as-gid 10010 < "$TREE/tools/live_pod_probe.py"
  $W -- python3 - environment --expect worker-pod             < "$TREE/tools/live_pod_probe.py"
  kubectl --kubeconfig "$KC_MIRROR" -n factory-workers exec <<POD_NAME>> -- ls -lnd <<WORKTREE_PATH>>
  ```

  and the **canary's negative control**: during an extra session, delete the default-deny policy (`kubectl --kubeconfig "$KC_MIRROR" -n factory-workers delete networkpolicy default-deny`), watch the session **fail closed** with a canary code and never fetch its bundle, then `terraform apply` the mirror again to put the policy back.
* **Expect:** both secret files are regular files with no group or other permission, the probe reports their real owner and mode under `hostUsers: false`, and **opening them as uid 10002 is refused**; the environment has no `AZURE_` name and no secret-like name and **does** have `KUBERNETES_SERVICE_HOST` and `_PORT` although `enableServiceLinks` is `false`; the worktree is owned by the worker identity (the `chown` worked); the wrapper's fixed canary lines
  and the session's failure code with the policy missing are the `canary` recordings (fixed text, no address).
* **Stop if:** uid 10002 can open a secret file, or the session with the policy deleted runs.
* **Rollback:** `terraform -chdir="$WORK/infra-mirror" apply -var-file="$WORK/mirror.tfvars"` restores the policy (a plan shows it as one create).
* **Approves:** Runner.
* **Cost:** one more short session.
* **Record:** `probe-output`, `canary`.

### G6. One repository per node, live

* **Needs:** two sandbox repositories enrolled with `execution = "pod"`, or one repository and a second one on the throwaway App; the `small` class (three pods per node by CPU) so that a node **could** hold both.
* **Run:** start sessions of the two repositories at the same time, and more than one session of one repository, up to the ceiling the pool allows; while they run:

  ```sh
  live --kubeconfig "$KC_MIRROR" colocation
  live --kubeconfig "$KC_MIRROR" nodes
  ```

* **Expect:** `colocation: no node holds the pods of two repositories` (the scheduler and Karpenter honoured the required anti-affinity: the pods of the second repository got **another** node, which the pool then provisioned); `nodes` shows more than one worker node; every pod's fetch of its bundle succeeded while the others ran (the relay saw each pod's own address, so no source NAT folded them into
  one peer: a bound of four connections per peer would have refused the fifth); the pods of one repository may share a node; the launches were paced by the pool (a burst, then one per interval: write down the times of the launches and whether any session waited for a token).
* **Stop if:** `FAIL colocation`: two repositories shared a node. Do not enable the pod backend for more than one repository; the Owner decides.
* **Rollback:** let the sessions end; the nodes consolidate.
* **Approves:** Runner.
* **Cost:** two or more nodes while the sessions run.
* **Record:** `pod`, `nodes`.

### G7. Failure shapes and recovery

* **Needs:** G4; slices 18, 19 and 20 (the pool, pod-side liveness, the orphan sweep, recovery retry and the deploy gate: delivered).
* **Run:** each is a separate session on the sandbox repository; capture `pod`, `pod-events` and the status the manager reports (`docs/operations.md`, "Status") after each:
  1. a session killed with `kubectl --kubeconfig "$KC_MIRROR" -n factory-workers delete pod <<POD_NAME>>` while running (a Running pod leaves **terminating** for its grace period; time from the delete to the manager's view of it);
  2. a node deleted under a session (`kubectl --kubeconfig "$KC_MIRROR" delete node <<NODE_NAME>>`): the eviction shape and the job's state afterwards;
  3. the session's `activeDeadlineSeconds` reached;
  4. an image that cannot be pulled (a digest that does not exist): the Pending-to-`pull_error` timing;
  5. the manager **restarted** during a session (`kubectl --kubeconfig "$KC_MIRROR" -n factory rollout restart deploy/factory`) and, separately, the **launcher outage** (the launcher container killed): what `recovery`, `purge` and `status` do with a recorded pod;
  6. the **previous image** (an `image_digest` rollback) read once a job holds a `pod` record: `status` still works with the old image;
  7. a pod whose connection drops mid-transfer (delete the pod while the delta is posted) and the **spool**'s accounting with a staging and the relay both writing (write down the free space while two sessions stage);
  8. the dashboard's refresh time with a recorded pod, and `status`'s cost per running pod job.
* **Expect:** for each, the manager's classification matches the pod's real state (`podstatus`), a pod that is not `dead` is never forgotten, the 45-second bound of the launcher client's `terminate` holds, and the previous image tolerates the new `pod` key in every reader; the shapes are recorded.
* **Stop if:** a recorded pod is forgotten while it still runs, or a job is left `running` with no pod.
* **Rollback:** delete leftover pods and purge the runs.
* **Approves:** Runner.
* **Cost:** a node per session; several sessions.
* **Record:** `pod`, `pod-events`, `launcher-reply`, `timing`.

### G8. The `claude` error shapes: spend limit, 401 and 429

* **Needs:** the **throwaway** Anthropic key and workspace (A4, B8); never the production key.
* **Run:** provoke each on purpose, on the mirror, with a session of the sandbox repository, and keep the end of the session log (the `result` event, or its tail when no `result` event arrives):
  1. **Spend limit:** set the throwaway workspace's spend limit in the Console (a human action) to a figure a short session passes, run sessions until one fails on it.
  2. **401:** upload a new version of the mirror's `anthropic-api-key` with one character changed, roll the mirror, run a session; then restore the real throwaway key.
  3. **429:** the real rate limit cannot be provoked on purpose without load; keep any 429 that happens during G4 to G7 and the burst of the ceiling's concurrent sessions in G6. If none occurs, say so: that row stays UNVERIFIED.
  Then import the log tail after you read it: `record import --step G8 --kind claude-result --out "$REC" <<FILE>>` (a session log can hold repository text: the Reviewer reads it).
* **Expect:** the real events for a spend limit, an authentication failure and, if seen, a rate limit, and whether a 429 reaches the `result` event at all; the manager's breaker (`stopworld`) tripped on the spend limit (or `rate_limit_unknown` three in five minutes for an unrecognised shape). Until slice 22 reads these, the classifier's assumed shapes stand.
* **Stop if:** the spend passes the cap or a production key is in use.
* **Rollback:** put the real throwaway key back; clear the breaker (`POST /spend-breaker` with `{"clear": true}`, `docs/capacity.md`) after the limit was raised.
* **Approves:** Owner (a deliberate failure with spend).
* **Cost:** the throwaway workspace's limit.
* **Record:** `claude-result`.

### G9. Teardown (always run, always last)

* **Needs:** nothing else running on the mirror; the Owner's go. A new shell starts with the names file: the guards must be defined, and each deleting command below starts with one.
* **Run:**

  ```sh
  . "$WORK/mirror-names.sh"                       # the names, the guards and $APP_ID of F1: a new shell has none of them
  mirror_group_is_ours && echo "the group is the mirror's (name, prefix and tag purpose=pod-backend-mirror)"
  # 1. no worker pods, no NodePool nodes, no scratch pools:  (a NAP cluster cannot be stopped)
  kubectl --kubeconfig "$KC_MIRROR" get nodepool,nodeclaims.karpenter.sh,nodes
  # 2. the two assignments the mirror made on the PRODUCTION registry (production writes: delete them FIRST, while the kubelet identity still exists; a deletion of the
  #    group or a destroy would leave the AcrPull behind under a principal that is gone). Each command needs a non-empty assignee: an empty one must never reach az.
  ACR_ID=$(az acr show --name <<ACR_NAME>> --query id -o tsv)
  KUBELET_OID=$(az aks show --resource-group "$MIRROR_RG" --name "aks-$MIRROR_PREFIX" --query identityProfile.kubeletidentity.objectId -o tsv)
  [ -n "$ACR_ID" ] && [ -n "$KUBELET_OID" ] && mirror_group_is_ours && az role assignment delete --assignee "$KUBELET_OID" --role AcrPull --scope "$ACR_ID"
  [ -n "$ACR_ID" ] && mirror_app_is_ours && az role assignment delete --assignee "$APP_ID" --role Reader --scope "$ACR_ID"
  [ -n "$ACR_ID" ] && [ -n "$KUBELET_OID" ] && az role assignment list --scope "$ACR_ID" --assignee "$KUBELET_OID" -o tsv   # must print nothing
  [ -n "$ACR_ID" ] && [ -n "$APP_ID" ] && az role assignment list --scope "$ACR_ID" --assignee "$APP_ID" -o tsv             # must print nothing
  # 3. prevent_destroy: the scratch copy loses BOTH lines (the cluster in aks.tf and the data volume claim in k8s.tf; never commit that):
  grep -n "prevent_destroy" "$WORK/infra-mirror"/*.tf
  # edit them out in the scratch copy, then:
  mirror_group_is_ours && mirror_tfvars_ok && terraform -chdir="$WORK/infra-mirror" destroy -var-file="$WORK/mirror.tfvars"
  # or, to avoid Terraform altogether, delete the resource group (the node resource group goes with the cluster):
  mirror_group_is_ours && az group delete --name "$MIRROR_RG" --yes --no-wait
  # 4. what the group does not hold (each starts with its guard; the role definition cannot be deleted while it is still assigned in a group that is still being deleted: wait until `az group exists --name "$MIRROR_RG"` prints false, then repeat):
  mirror_guard && az role definition delete --name "Factory Plan Cluster Reader $MIRROR_PREFIX"
  mirror_app_is_ours && az ad app delete --id "$APP_ID"
  az role assignment list --assignee "$APP_ID" --all -o table                                           # must print nothing (the state-container assignment goes with the container below)
  az keyvault list-deleted -o table                                                                      # the mirror's soft-deleted vault: purge it when its name is needed again
  mirror_guard && az storage container delete --name "$MIRROR_STATE_CONTAINER" --account-name <<TFSTATE_STORAGE_ACCOUNT>> --auth-mode login   # the mirror's own container, never production's
  rm -rf "$WORK/az-plan" "$WORK/kubeconfig-"*
  ```

  Revoke the throwaway Anthropic key and uninstall the throwaway GitHub App; delete the workspace's remaining limit settings if the workspace is not reused. **Deleting the resource group does not remove the two assignments on the registry**: step 2 does, and it comes first.
  If the cluster is already gone when you arrive (an earlier delete), `KUBELET_OID` is empty and the `AcrPull` assignment may be left under a principal that no longer exists: list the registry's assignments (`az role assignment list --scope "$ACR_ID" -o table`), and give the Owner the ones whose principal is shown as unknown; the Owner deletes them.
* **Expect:** `az group show --name "$MIRROR_RG"` answers `ResourceGroupNotFound`; the node resource group is gone; **no assignment of the mirror remains on the registry** (the list of step 2 printed nothing, for the kubelet identity and for the plan identity); the role definition and the application are gone and the assignment list of `$APP_ID` is empty; no `Standard_D4s_v6` node of the mirror runs. Cost Management shows the daily cost falling to zero the next day (A5).
* **Stop if:** a guard refuses (do not delete: the name is not the mirror's); the production resource group, cluster or role is named in any command (re-read before pressing enter); an assignment on the registry cannot be deleted or is not the mirror's.
* **Rollback:** none (this is the undo of everything above); a repeated experiment rebuilds from C.
* **Approves:** Owner.
* **Cost:** stops the meter.
* **Record:** `note` (the teardown's checks and the next day's bill).

## The recording format

Every observation that slice 22 turns into a fixture is one JSON file with exactly these keys (`tools/live_recording.py`, which writes and checks it):

| Key | Holds |
|---|---|
| `format` | `factory-runbook-recording/1` |
| `step` | the runbook step that produced it: a part letter A to G and one or two digits (`D5`) |
| `kind` | one word of the closed vocabulary below |
| `recorded_at` | UTC, `YYYY-MM-DDTHH:MM:SSZ` |
| `tool` | which program wrote it (a short fixed string, never a path) |
| `argv` | the command that produced the payload as a list of strings, or `null` for a file a person supplied |
| `returncode` | its exit status, or `null` |
| `redactions` | how many values the scrubber replaced or dropped |
| `payload` | a JSON document, or `{"text": "..."}` for a text kind |
| `sha256` | the hash of the canonical JSON of the payload, so a hand edit is noticed |

The kinds and what slice 22 replaces with them: `az-quota` (the quota figures), `az-aks` (the cluster profile), `az-role` (a role definition), `nodes` (sizes, allocatable, labels and taints), `pod` (phase, conditions, container states and the reason a pod is Pending or was evicted: the table of `podstatus`),
`pod-events` (the events of one pod: `FailedScheduling` at a NodePool limit, `Evicted`, `Killing`), `nodeclaim` (the disruption and termination shapes), `nodepool` (the object as the server stored it), `crd` (the field names), `status-footprint` (the sizing block of `status --json`), `kube-status` (the answers 404, 403, 401, 409 and 422 as the launcher's client meets them),
`denial` (the first line of an admission denial), `launcher-reply` (an answer over the launcher's socket), `canary` (the wrapper's fixed lines), `claude-result` (the spend-limit, 401 and 429 events of `claude`), `plan-actions` (a plan's action list), `probe-output` (the output of the probes of this slice),
`timing` (`{"what": "<fixed phrase>", "seconds": <number>}` and any numbers that belong to it) and `note` (a number a person read and where).

What the scrubber does, and does not: **a Kubernetes Secret or SecretList is refused, never scrubbed**, whether it is an object or a `"kind": "Secret"` written inside a string (the last-applied-configuration annotation, a YAML in a `note`); credential shapes (an Anthropic or GitHub key, a bearer token, a JSON web token, a SAS signature `sig=`, a private key header, a long opaque string with letters of both cases and a digit, a long lower-case hexadecimal string that is not a `sha256:` digest) are replaced by `<redacted>`; a GUID becomes the zero GUID; an IPv4 address becomes `0.0.0.0` (the two fixed Azure addresses stay) and an IPv6 address becomes `::`;
the value of an environment entry whose name says key, token, secret, password, credential or authorization is replaced, and so is the value of a JSON key that ends in token, secret, password or key material; **in a text, the value after `token:`, `password=`, `secret=`, `--token=` and the like is replaced, and so is the word after a lone `--token` in a list of arguments**; `managedFields` is dropped. `record validate` scans a recording again and fails on anything the scrubber would still change. **It does not recognise issue text, pull request titles, repository names or a person's name**: the Reviewer does. A file is written once (no overwrite, no
symbolic link followed) with mode 0600. Slice 22 copies the reviewed files into `tests/` and writes the tests that read them; nothing in this slice reads them.

## Which step settles which UNVERIFIED claim

Every row names a claim, where it is made (a file and a verbatim phrase of it), the step that settles it and the kind of recording that keeps the answer. A claim that **no step settles** says why: a decision, a source other than the cluster, or a later change. `tests/test_live_runbook.py` checks that every step named here exists in this page, that the phrase of every row
is still in its file, that every recording kind is in the vocabulary above, and that **no UNVERIFIED paragraph, bullet or item of `docs/pod-backend.md`, `docs/capacity.md` or `infra/README.md` is missing from the table**. A claim made twice, in a paragraph and again in the list that repeats it, has a row for each place.

| ID | Claim | Source | Step | Recording |
|---|---|---|---|---|
| U001 | a real API server accepts the pod as written, with its field names and the three relaxations | `docs/pod-backend.md` "accepts the pod as written (field names" | E4 | denial |
| U002 | a real API server accepts the pod as written, and Karpenter and NAP honour the required anti-affinity (slice 6) | `docs/pod-backend.md` "accepts the pod as written; that Karpenter and NAP honour" | E4, G6 | pod |
| U003 | what the API server defaults and mutates before it validates (it is not modelled by the mirror) | `docs/pod-backend.md` "what the API server does to the pod before validating it" | E4 | - |
| U004 | the provider path accepts the admissionregistration kinds | `docs/pod-backend.md` "the provider path for these kinds is UNVERIFIED" | E1 | - |
| U005 | a field added to the pod API after the policy was written is not refused | `docs/pod-backend.md` "therefore not refused until a later change adds it" | E4 | - |
| U006 | the Azure authorizer maps the two admission kinds to customresources/read | `docs/pod-backend.md` "Whether the authorizer maps them" | F3 | plan-actions |
| U007 | relay throughput and the 12 GiB and 16Gi numbers on a real repository | `docs/pod-backend.md` "Relay throughput and the 12 GiB" | G4 | timing |
| U008 | the spool permission behaviour under hostUsers false with two containers sharing an emptyDir | `docs/pod-backend.md` "the spool permission behaviour under" | G2 | probe-output |
| U009 | plain HTTP between a worker pod and the relay (TLS is out of scope) | `docs/pod-backend.md` "plain HTTP inside the cluster" | none: owner decision (TLS is out of scope of the program) | - |
| U010 | a pod's dropped connection surfaces as a failed write on the last slice | `docs/pod-backend.md` "a pod's connection that drops really surfaces" | G7 | - |
| U011 | the relay sees each pod's own address (no source NAT in front of it) | `docs/pod-backend.md` "that the relay sees each pod's own address" | G6 | - |
| U012 | the workload identity webhook leaves the relay container alone | `docs/pod-backend.md` "that the workload identity webhook leaves the relay" | G2 | probe-output |
| U013 | FACTORY_RELAY_PORT is not shadowed by a Service variable | `docs/pod-backend.md` "a naming hazard for slice 17" | G2 | probe-output |
| U014 | bundle size and time on a real repository | `docs/pod-backend.md` "Throughput and size on real repositories (only" | G4 | timing |
| U015 | the shallow-clone depth that review diffs need | `docs/pod-backend.md` "the shallow-clone depth that review diffs really need" | G4 | - |
| U016 | the differences between the pod's git answers and in-process mode (slice 9a paragraph) | `docs/pod-backend.md` "not proven for criss-cross merges); the exact set" | G4 | - |
| U017 | the differences between the pod's git answers and in-process mode (slice 9a bullet) | `docs/pod-backend.md` "production image, the exact set of differences" | G4 | - |
| U018 | a staging and the relay can overshoot the spool's refusal limit | `docs/pod-backend.md` "can together overshoot the spool's refusal limit" | G7 | timing |
| U019 | the disk a shallow fetch uses before its size can be measured | `docs/pod-backend.md` "how much disk the shallow fetch uses" | G4 | timing |
| U020 | git version behaviour on the production image (template, fetch by commit id, read-tree) | `docs/pod-backend.md` "git version behaviour on the production image (tests" | G4 | - |
| U021 | the extractor's modes and ownership with hostUsers false | `docs/pod-backend.md` "the extractor's modes and ownership (the wrapper chowns" | G5 | probe-output |
| U022 | what the Secret volume shows as owner and mode under hostUsers false | `docs/pod-backend.md` "what the volume shows as root is" | G5 | probe-output |
| U023 | the wrapper command and the tini path exist in the pod | `docs/pod-backend.md` "The wrapper `command` and the `tini` path in the image" | G4 | - |
| U024 | the canary destinations are the complete list of what must be unreachable (slice 10a paragraph) | `docs/pod-backend.md` "must be unreachable (they are the contract's list" | E3 | canary |
| U025 | the canary destinations are the complete list of what must be unreachable (slice 10a bullet) | `docs/pod-backend.md` "must be unreachable; the kubelet-injected API server variables" | E3 | canary |
| U026 | the kubelet injects the API server variables with enableServiceLinks false | `docs/pod-backend.md` "that the kubelet injects `KUBERNETES_SERVICE_HOST`" | G5 | probe-output |
| U027 | a real API server's answer to the unauthenticated probe over both addresses | `docs/pod-backend.md` "how a real API server answers an unauthenticated request" | E3 | probe-output |
| U028 | chown and lchown to uid 10002 with hostUsers false | `docs/pod-backend.md` "`chown` and `lchown` to uid 10002 with `hostUsers: false`" | G5 | probe-output |
| U029 | the relay under a real slow link | `docs/pod-backend.md` "the relay's behaviour under a real slow link" | G4 | timing |
| U030 | delta size on real repositories (a dependency tree can exceed the caps) | `docs/pod-backend.md` "the owner decides per repository later (UNVERIFIED" | G4 | timing |
| U031 | delta size and time and the memory of a worst-case delta on a real repository | `docs/pod-backend.md` "Throughput and size of a real delta (only synthetic" | G4 | timing |
| U032 | O_TMPFILE and renameat on the production file system | `docs/pod-backend.md` "behaviour of `O_TMPFILE` for the private copy and of" | G4 | - |
| U033 | git version behaviour of the delta step on the production image | `docs/pod-backend.md` "the git version behaviour on the production image; that a Linux" | G4 | - |
| U034 | a Linux file system with another name limit than 255 bytes | `docs/pod-backend.md` "does not fail part-way" | none: the file system is fixed (an Azure disk with ext4); no other limit exists to test | - |
| U035 | a worker's .gitmodules is not read by any supervisor step | `docs/pod-backend.md` "the open question of slice 3, above" | none: a design question of slice 3, closed by code reading and not by a cluster | - |
| U036 | every PROVISIONAL number (classes, emptyDir limits, byte, file, time and thread caps, sidecar uids, timing constants) | `docs/pod-backend.md` "Every PROVISIONAL number" | B4, D8, G4 | timing |
| U037 | the label prefix factory.internal/ needs no domain the owner controls | `docs/pod-backend.md` "That the label prefix" | G4 | pod |
| U038 | the tflint-ignore annotation applies to a multi-line locals entry in CI | `docs/pod-backend.md` "That `tflint-ignore` applies to a multi-line `locals` entry in CI" | none: obsolete (slice 14 removed the annotation) | - |
| U039 | everything the program assumes about Kubernetes, Karpenter, Cilium and the registry | `docs/pod-backend.md` "Everything about Kubernetes, Karpenter, Cilium and the registry" | D1, D2, D3, D4, D5, D6, D7 | - |
| U040 | Claude's own sandbox already blocks rewriting a worktree's .git pointer | `docs/pod-backend.md` "whether Claude's own sandbox already blocks" | none: needs a red-team session whose prompt asks for it; the owner decides separately | - |
| U041 | behaviour on the production git version, and a worktree pointer written with relative paths | `docs/pod-backend.md` "behaviour on the production git version (checked here" | G4 | - |
| U042 | the settings are every way a worker's tree can make a supervisor git call run a program | `docs/pod-backend.md` "that the settings above are every way a worker's tree" | none: an open-ended security claim; a live run only adds evidence | - |
| U043 | the previous image tolerates the new pod key in every reader | `docs/pod-backend.md` "that the previous image tolerates the new `pod` key" | G7 | - |
| U044 | recovery, purge and status with a real launcher outage | `docs/pod-backend.md` "recovery, `purge` and `status` with a real launcher outage" | G7 | launcher-reply |
| U045 | what a real launcher answers for a pod that is Pending, terminating, evicted or replaced | `docs/pod-backend.md` "what a real launcher answers for a pod that is Pending" | G7 | pod |
| U046 | the pool loop can wait for a terminate call (45 s) | `docs/pod-backend.md` "that the pool loop can wait for a `terminate` call" | G7 | timing |
| U047 | the cost of the reader (status asks once per running pod job, the dashboard on every refresh) | `docs/pod-backend.md` "and the cost of the reader" | G7 | timing |
| U048 | a terminator that returns for a delete that was merely accepted is trusted | `docs/pod-backend.md` "a terminator that returns for a delete that was merely accepted" | G7 | - |
| U049 | the Cilium downtime, the NAP-before-Cilium order and the in-place update (provider reading only) | `docs/pod-backend.md` "the Cilium downtime and the NAP-before-Cilium order" | D1, D3 | timing |
| U050 | whether network_policy is needed with the Cilium data plane | `docs/pod-backend.md` "whether `network_policy` is needed" | D2 | - |
| U051 | the karpenter.sh/v1 and karpenter.azure.com/v1beta1 field names against the installed definitions | `docs/pod-backend.md` "field names against the installed definitions" | D4 | crd |
| U052 | the kubernetes provider's kube_config is known at plan time after an in-place cluster update | `docs/pod-backend.md` "whether the kubernetes provider's `kube_config` stays known" | D1, D3 | - |
| U053 | the plan identity can read the new custom resources | `docs/pod-backend.md` "that the plan identity can read the new custom resources" | F2 | - |
| U054 | the quota figures, read once on 2026-11-09 | `docs/pod-backend.md` "and the quota figures, which were" | B2 | az-quota |
| U055 | the allocatable CPU and memory of Standard_D4s_v6 and whether the NodePool sku-name requirement accepts it | `docs/pod-backend.md` "allocatable CPU and memory of the default `Standard_D4s_v6`" | D5 | nodes |
| U056 | an unset pod_backend_enabled gives no plan line | `docs/pod-backend.md` "that an unset `pod_backend_enabled` gives no plan line" | B6, C5 | - |
| U057 | Cilium enforces ipBlock.except on AKS | `docs/pod-backend.md` "that Cilium enforces `ipBlock.except` on AKS" | E3 | probe-output |
| U058 | the DNS rule matches after the service address is translated | `docs/pod-backend.md` "that the DNS rule matches after the service address" | E3 | probe-output |
| U059 | the API server's public address is refused without a credential | `docs/pod-backend.md` "that the API server's public address is refused without" | E3 | probe-output |
| U060 | the exclude_namespaces key spelling and the agent's restart behaviour on AKS | `docs/pod-backend.md` "the `exclude_namespaces` key spelling" | E2 | - |
| U061 | a worker pod's output is really absent from Log Analytics | `docs/pod-backend.md` "that a worker pod's output is" | E2 | - |
| U062 | the read-only plan identity can read a namespace, a service account and a network policy through the built-in reader role | `docs/pod-backend.md` "that the read-only plan identity can read a namespace" | F2 | - |
| U063 | a real API server accepts and evaluates the CEL as the offline model does (type check, cost, semantics) | `docs/pod-backend.md` "that a real API server accepts and evaluates the CEL as" | E4 | denial |
| U064 | differences of CEL semantics and of the pod schema between the kind version of CI and AKS | `docs/pod-backend.md` "differences of CEL semantics and of the pod schema between" | E7 | - |
| U065 | the server's defaulting is what tests/test_policy_cel.py emulates | `docs/pod-backend.md` "that the server's defaulting is what" | E4 | - |
| U066 | ephemeral containers are refused on create before the policy runs | `docs/pod-backend.md` "that ephemeral containers are refused on create" | E4 | - |
| U067 | the order of the parameter ConfigMap update during an automatic image promotion | `docs/pod-backend.md` "the ordering of the parameter ConfigMap update" | E5 | timing |
| U068 | the data-action names and the plan role's coverage of the two admission kinds | `docs/pod-backend.md` "the data-action names and the plan role's coverage" | F3 | - |
| U069 | SO_PEERCRED across containers that share an emptyDir under hostUsers false | `docs/pod-backend.md` "`SO_PEERCRED` across containers that share an" | G2 | probe-output |
| U070 | the kubelet's retry delay for a pod whose Secret is created a moment later | `docs/pod-backend.md` "the kubelet's retry delay for a pod whose Secret" | G4 | timing |
| U071 | the garbage collection timing of a Secret with an owner reference | `docs/pod-backend.md` "the garbage collection timing of a Secret" | G4 | timing |
| U072 | every Kubernetes shape the fake assumes (slice 11) | `docs/pod-backend.md` "every Kubernetes shape the fake assumes" | G3, G7 | kube-status |
| U073 | the real shape of a spend-limit, an authentication and a rate-limit error, and that a 429 reaches the result event | `docs/pod-backend.md` "the real shape of a spend-limit, an authentication" | G8 | claude-result |
| U074 | the bracketed rule ids in a real admission denial (the client's reading of a 422) | `docs/pod-backend.md` "That bracket form is an assumption of this slice" | E6 | denial |
| U075 | every real pod and event shape, the Pending reason at a NodePool limit, a Karpenter eviction | `docs/pod-backend.md` "UNVERIFIED: every real pod and event shape, in particular" | D6, G7 | pod-events |
| U076 | a native Role works next to Azure RBAC for Kubernetes | `docs/pod-backend.md` "that a native Role works next to Azure RBAC" | G1 | - |
| U077 | the projected token path is the standard one in the launcher container | `docs/pod-backend.md` "that the projected token path is the standard one" | G3 | - |
| U078 | a real API server answers 409, 404, 401, 403 and 422 as the fake does | `docs/pod-backend.md` "that a real API server answers 409, 404, 401, 403 and 422" | G3 | kube-status |
| U079 | what an API server or an intermediary puts on the wire when it misbehaves | `docs/pod-backend.md` "what a real API server or an intermediary puts" | none: a misbehaving server cannot be provoked on purpose; the raw-socket test server stays the evidence | - |
| U080 | the HTTPS path against a real API server (certificate, handshake) | `docs/pod-backend.md` "the HTTPS path against a real API server" | G3 | - |
| U081 | what SO_PEERCRED reports for a peer in another container (slice 11) | `docs/pod-backend.md` "what `SO_PEERCRED` reports for a peer in another container" | G2 | probe-output |
| U082 | the Pending reason at a NodePool limit is one the podstatus table reads as pending or unschedulable | `docs/pod-backend.md` "that the Pending reason at a" | D6 | pod-events |
| U083 | deleting a Running pod leaves it terminating for its grace period, and the 45 s bound of the client's terminate | `docs/pod-backend.md` "that deleting a Running pod" | G7 | pod |
| U084 | the standard in-pod token and CA paths are the ones the launcher container sees | `docs/pod-backend.md` "that the standard in-pod token and CA paths" | G3 | - |
| U085 | the footprint of a build, review or verify session (the estimates of capacity.py) | `docs/capacity.md` "a worker is mostly waiting for the" | B4 | status-footprint |
| U086 | the per-phase footprint figures: measured 2026-11-10 on 97 sessions (repo-b has 4 per phase: re-measure at the new defaults) | `docs/capacity.md` "page cache against working set over time" | B4 | status-footprint |
| U087 | the real claude error for a spend limit, a 401 and a 429, and Anthropic's acceleration limits | `docs/capacity.md` "error for a spend limit, a 401" | G8, B8 | claude-result |
| U088 | the ceiling of 14 workers has never been reached under load (at most 3 ran at once) | `docs/capacity.md` "The ceiling of 14 workers is" | B4 | status-footprint |
| U089 | the role must be updated before the first enabling flip, and whether the authorizer maps the admission kinds to customresources/read | `infra/README.md` "pod backend needs the role updated BEFORE" | F3, F4 | plan-actions |
| U090 | the first production plan shows no action on the four addresses of the cluster guard | `infra/README.md` "that the plan of production today shows no action on the four addresses" | B6, C5 | - |
| U091 | the exact action strings Terraform prints for a replace | `infra/README.md` "the exact action strings Terraform prints for a replace" | C5 | plan-actions |
| U092 | Cilium, node auto-provisioning and the worker pool, item 37 (every sub-claim has its own row above) | `infra/README.md` "Unverified: that omitting" | D1, D2, D3, D4, D5, D6, D9 | - |
| U093 | the worker namespace, item 38 (every sub-claim has its own row above) | `infra/README.md` "(a text test pins only the gating); that Cilium enforces" | E1, E2, E3, F2 | - |
| U094 | the admission policies, item 39 (every sub-claim has its own row above) | `infra/README.md` "Nothing was applied or run against a" | E1, E4, E5, E7, F3 | - |
| U095 | real-repository delta size and time, and the memory of a worst-case delta (slice 9b bullet) | `docs/pod-backend.md` "real-repository delta size and time, the memory of" | G4 | timing |
| U096 | O_TMPFILE and renameat on the production file system, and the .gitmodules question (slice 9b bullet) | `docs/pod-backend.md` "on the production file system, and that a worker's" | G4 | - |
| U097 | every command of the runbook runs as written (none was run) | `docs/pod-backend.md` "Every command of the runbook was written without access to Azure" | none: settled by running the runbook itself; a command that fails is a defect of the page, fixed in a small pull request | - |
| U098 | the tools read the real shapes of az, kubectl and Terraform output as they read the canned ones | `docs/pod-backend.md` "The tools read real answers they have only seen as canned ones" | B2, B5, D4, E4, F3 | - |
| U099 | the scrubber is enough with a reviewer, for what a real status or a session log holds | `docs/pod-backend.md` "The scrubber is a safety net: it finds the credential shapes" | A6 | - |
| U100 | the probes' answers inside real pods (identity switch, access, errno on the real file systems) | `docs/pod-backend.md` "The probes' answers inside real pods" | G2, G5 | - |
| U101 | the quota sums are a conservative estimate of Azure's own arithmetic at create time | `docs/pod-backend.md` "The quota sums are a conservative estimate" | C2, C3 | - |
| U102 | Part G was written against slices 12b, 16 and 17 as read in the tree (the container names, the settings, the Role), not as run | `docs/pod-backend.md` "Part G was written against slices 12b, 16 and 17 as they are in the tree" | E1, G1, G2 | - |
| U103 | slice 21: the runbook's commands, the tools' reading of real shapes, the scrubber, the probes, the quota arithmetic, Part G | `docs/pod-backend.md` "that the commands of the runbook run as written" | A6, B2, C2, F3, G2 | - |
| U104 | the plan identity can read the Role and the RoleBinding in a plan (slice 16, the plan identity bullet) | `docs/pod-backend.md` "That the plan identity can read these two kinds in a plan is UNVERIFIED" | F2, F3 | plan-actions |
| U105 | replacing the pool `factory` by a change of node_taints (provider documentation only) | `docs/pod-backend.md` "provider documents it as forcing a new resource; UNVERIFIED for the pinned version" | none: the manager taint (`manager_pool_taint_enabled`) is a separate later flip that no step of this runbook makes; it is rehearsed on the mirror on its own | - |
| U106 | the Secrets Store CSI driver and the Azure Disk CSI driver tolerate the manager taint on this cluster (slice 17 paragraph) | `docs/pod-backend.md` "whether the Secrets Store CSI driver and the Azure Disk CSI driver tolerate the taint on this cluster is UNVERIFIED" | none: the manager taint (`manager_pool_taint_enabled`) is a separate later flip that no step of this runbook makes; it is rehearsed on the mirror on its own | - |
| U107 | everything of slices 16 and 17 that only a cluster shows (the heading of the list) | `docs/pod-backend.md` "UNVERIFIED (slices 16 and 17: none of it can be seen without a cluster)" | B6, C5, E1, F3, G1, G2, G6 | - |
| U108 | a default plan shows no line for the Deployment, the pool factory or anything else, though merge and concat are in the Deployment (slice 17) | `docs/pod-backend.md` "That a default plan shows no line for the Deployment, the pool `factory` or anything else" | B6, C5 | - |
| U109 | the provider accepts the typed Role and RoleBinding with the empty core api_groups entry (slice 16) | `docs/pod-backend.md` "That the provider accepts the typed Role and RoleBinding with the empty core `api_groups` entry" | E1, F3 | - |
| U110 | the kubelet starts the manager pod with the launcher and the relay as written: projected token, emptyDir owners, init container capabilities, read-only socket mount (slice 17) | `docs/pod-backend.md` "That the kubelet starts the pod as written: the projected token and the CA readable by uid 10003" | E1, G2, G3 | probe-output |
| U111 | the webhook honours skip-containers for the init container and the two sidecars | `docs/pod-backend.md` "That the webhook honours `skip-containers` for the init container and the two sidecars" | G2 | probe-output |
| U112 | a worker pod reaches the relay through the Service and cannot reach the manager address, under Cilium | `docs/pod-backend.md` "That a worker pod reaches the relay through the Service and cannot reach the manager address" | E3, G4, G6 | - |
| U113 | the cluster's fqdn is the public FQDN the canary needs and is known at plan time after a cluster change | `docs/pod-backend.md` "That `fqdn` is the public FQDN the canary needs" | E1, E3 | - |
| U114 | the sidecar sizes, and that the manager pod still fits its node with them | `docs/pod-backend.md` "The sidecar sizes, and that the pod still fits its node with them." | E1, G4 | - |
| U115 | the CSI drivers tolerate the manager taint and whether node_taints replaces the pool (slice 17 list) | `docs/pod-backend.md` "tolerate `workload=manager:NoSchedule`, and whether `node_taints` replaces the pool." | none: the manager taint (`manager_pool_taint_enabled`) is a separate later flip that no step of this runbook makes; it is rehearsed on the mirror on its own | - |
| U116 | how often a chunk whose answer was lost is sent twice on a real network (slice 10b) | `docs/pod-backend.md` "UNVERIFIED how often that happens on a real network" | G4 | - |
| U117 | whether the share of the termination grace that a normal stop leaves is enough for the delta of a real tree (slice 10b, SIGTERM) | `docs/pod-backend.md` "is enough for the delta of a real tree is UNVERIFIED" | G4, G7 | timing |
| U118 | the pod worker's run as seen from a pod: identity of the agent, tini and SIGTERM, the memory guard, claude on the PATH, pipe throughput, delta read time, O_TMPFILE, the relay's connections per peer (slice 10b paragraph) | `docs/pod-backend.md` "that the agent really runs as uid 10002 with an empty permitted set" | G4, G5, G6, G7 | - |
| U119 | the same list of the pod worker's run, in the UNVERIFIED section (slice 10b bullet) | `docs/pod-backend.md` "as container root under `hostUsers: false`; SIGTERM through `tini`" | G4, G5, G7 | - |
| U120 | whether one pod launch per 15 s after a burst of 2 is the right smoothing for node provisioning (slice 18) | `docs/pod-backend.md` "Whether a pod launch rate of one per 15 s after a burst of 2 is the right smoothing" | G6 | - |
| U121 | flock across processes on the production volume, the default ceiling, burst and interval against the real NodePool limit, the phase budget as an upper bound, the launcher's info count (slice 18 paragraph) | `docs/pod-backend.md` "UNVERIFIED: that `flock` on the state directory behaves across processes" | G4, G6, G7 | - |
| U122 | the same list of slice 18 in the UNVERIFIED section (bullet) | `docs/pod-backend.md` "across processes on the production state volume, the default ceiling" | G4, G6 | - |
| U123 | the heartbeat window, the order in which Kubernetes and the relay learn of a finished session, the real listing for the states the table guesses, the Secret of a pod that is gone, the cost of a sweep pass (slices 19 and 20 paragraph) | `docs/pod-backend.md` "UNVERIFIED: that the relay's `last_heartbeat` is written on every call a live wrapper makes" | G4, G7 | pod |
| U124 | the same list of slices 19 and 20 in the UNVERIFIED section (bullet) | `docs/pod-backend.md` "the heartbeat window and its relation to the wrapper's interval, the order in which Kubernetes and the relay learn of a finished session" | G7 | pod |
| U125 | the real wrapper reads the manifest in a pod under hostUsers false; the thresholds of the wait loop on real hardware; the stop, drain and delta inside the margin; a terminating pod gone within the grace period; the exit 68 of the canary; the breaker marker and the ledger as the supervisor sees them (slice 12b paragraph) | `docs/pod-backend.md` "UNVERIFIED: that the real wrapper, as a container with `hostUsers: false` and the pod's capability set, reads the manifest" | E3, G2, G4, G5, G7 | - |
| U126 | the launcher's answers for a pod it reaped, the breaker marker and the spool as the supervisor's uid sees them, FACTORY_DATA=/data (slice 12b bullet) | `docs/pod-backend.md` "the launcher's answers for a pod it reaped; the breaker marker and the spool as the supervisor's uid sees them" | G2, G4, G7 | - |
| U127 | the Role and the RoleBinding through the provider and the plan identity, the projected token and the init container under hostUsers false, the webhook, the Service path, the sizes, the manager taint (slices 16 and 17 bullet) | `docs/pod-backend.md` "the Role and the RoleBinding through the provider and the plan identity; the projected token" | E1, F3, G1, G2 | - |
| U128 | the manager's orphan sweep holds a plan or preflight session (which no job's state records) by its live relay record: that the record stays live for as long as the session runs, on the real spool (slice 20 wiring paragraph and its UNVERIFIED bullet) | `docs/pod-backend.md` "that the relay record of a plan or preflight session is live for as long as the session runs on the real spool" | none: no step of this runbook runs `factory plan` or `factory preflight` on the mirror; the hold is read from the record's state, markers and deadline, and is tested with real records in tests/test_podsweep_crossprocess.py | - |
| U129 | the `az` forms of the guards of C1 and G9 behave as written, and the enabling flip plans the Deployment change the page expects | `docs/pod-backend.md` "The guards of C1 and G9 are shell functions that the tests run with bash and a stub `az`" | C1, E1, G9 | - |
| U130 | the hourly cost figures per worker (a median session priced per wall minute, not a billed hour) | `docs/capacity.md` "The hourly figures are" | none: no step bills a worker; the first live sessions and the Console usage settle it | claude-result |
| U131 | the measurements are one repository deep (repo-a 29 to 30 sessions per phase, repo-b 4) | `docs/capacity.md` "the measurements are one repository deep" | B4 | status-footprint |
| U132 | the manager's own working set and CPU with no in-process worker, and the relay's memory while three transcripts stream (the numbers behind the right-size of issue #148) | `docs/capacity.md` "the manager's own working set and CPU with no in-process worker" | B4 | status-footprint |

The steps that settle many rows (D1 to D9, E1 to E7, G4) are the ones to schedule first; a row whose step is a part G step stays UNVERIFIED until slices 12b, 16 and 17 exist and Part G has been run.

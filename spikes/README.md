# Spikes S1 to S3: decide before you deploy

## Recorded result (2026-11-06)

The three records exist: `S1.md`, `S2.md` and `S3.md`. **S1 FAILED on ACA and is recorded PASS for AKS only**, with three
accepted pod relaxations (`hostUsers: false`, `procMount: Unmasked`, `appArmorProfile: Unconfined`); S2 passed on ACA (Azure Files
NFS) and on AKS (Azure Disk ext4); S3 and S3b passed on both. The `nfs4` criteria below apply to the ACA runs; on AKS `/data` is a
disk because a `hostUsers: false` pod cannot mount NFS. The procedures and failure table below were written for ACA and are kept as
the method: on AKS use `kubectl -n factory exec` in place of `az containerapp exec`. The blockquote below is the first
text, kept as history; the three files are no longer empty and the spikes have been run.

> **Do not `terraform apply` (and do not deploy the image) until S1, S2 and S3 are
> recorded PASS in `spikes/S1.md`, `spikes/S2.md` and `spikes/S3.md`.**
> (Historical: the three files were empty and nobody had run the spikes on Azure
> Container Apps (ACA). That is superseded by the recorded result above; the rule stays as
> advice, but nothing in this repository enforces it: do not apply unless all three read PASS.)
> A FAIL changes the hosting decision (see
> the last section) and must be taken to the maintainers, not worked around: the
> sandbox is never weakened to make a spike pass.

The spikes check the three assumptions of the design (one ACA app, Dedicated workload profile, Azure Files NFS, workers as
subprocesses under another UID):

| Spike | Question |
|---|---|
| S1 | Can bubblewrap create namespaces on ACA Dedicated, so Claude Code's sandbox runs with `failIfUnavailable`? |
| S2 | Do git worktrees, `os.replace` and `flock` behave on an Azure Files NFS mount? |
| S3 | Does `Popen(user=worker)` work with only ambient capabilities, can the worker not reach the supervisor's secrets, and does the data-tree permission scheme work on the NFS mount (S3b)? |

The scripts here are plain bash or Python (`s3b_data_tree.py` imports the `factory` package and runs with
`/opt/factory/bin/python`) and are copied into the image at
`/opt/factory-spikes` (`tools/sandbox_e2e.py` is at `/opt/factory-tools`, wrapped by the
command `factory-sandbox-check`). Run them **inside the container on ACA**, as the
unprivileged `factory` user the entrypoint drops to.

## Prerequisites: a throwaway environment

Use a disposable resource group and subscription, never the production one. Nothing in
this repository creates it for you; the outline below uses `az` and the placeholders
from the <<PLACEHOLDER>> convention of this repository. Flag names drift between `az containerapp` releases: run
`az containerapp <command> --help` first.

1. Build the image from this repository (`Dockerfile`; see `image/README.md`, "Build and check") and push
   it to a throwaway registry, for example `az acr build --registry <<ACR_NAME>> --image factory-spike:1 .`
2. VNet with an ACA subnet delegated to `Microsoft.App/environments` (`/27` or larger), and a
   second subnet for the private endpoint.
3. ACA environment on that subnet with workload profiles; add the profile
   `Dedicated-D8` (type `D8`, min 1, max 1).
4. Premium `FileStorage` account, an NFS share (100 GiB minimum), `https_traffic_only_enabled=false`
   (NFS has no encryption in transit on ACA), public access disabled, a private endpoint
   for the `file` sub-resource, and the private DNS zone `privatelink.file.core.windows.net`
   linked to the VNet.
5. Register the share as environment storage of kind NFS
   (`az containerapp env storage set --storage-type NfsAzureFile ...`).
6. A container app from the image: workload profile `Dedicated-D8`, 1 replica, the NFS volume
   mounted at `/data`, command `sleep infinity` (so that it keeps running) and **no secrets**
   (the spikes need none). Create it from a YAML file with `az containerapp create --yaml`.
7. Open a shell: `az containerapp exec --name <app> --resource-group <<RESOURCE_GROUP>> --command bash`.
   The container starts through `factory-entrypoint`, so a plain shell may be root. Switch to the
   same identity the real app uses with
   `/usr/local/bin/factory-entrypoint bash` (it drops to uid 10001 with the ambient set) and
   confirm with `id` and `grep Cap /proc/self/status`.

## S1: bubblewrap and the Claude Code sandbox

```sh
bash /opt/factory-spikes/s1_bwrap.sh
factory-sandbox-check          # tools/sandbox_e2e.py as the worker identity, no capabilities
```

Both run as the **worker identity** (uid 10002, gid 10010, no capabilities, `no_new_privs`), because that is
what bubblewrap sees in production: bubblewrap refuses to start in any process that holds capabilities
("Unexpected capabilities but not setuid"), and both the root shell of `az containerapp exec` and the `factory`
identity (ambient `CAP_SETUID`/`CAP_SETGID`/`CAP_KILL`) do. Neither command needs you to switch: each one
re-executes itself through `setpriv` when it starts as root or with capabilities. If `s1_bwrap.sh` still
reports `bwrap: Unexpected capabilities`, it exits 2 with `S1 INCONCLUSIVE`: that is a harness problem, not an S1 result.

Part 1 uses `bwrap` directly. Part 2 drives the real `claude` CLI, with the settings Factory
generates (`sandbox.enabled`, `failIfUnavailable`, `allowUnsandboxedCommands=false`), against a
fake Messages API on localhost, so it needs no Anthropic key and spends nothing.

**PASS** when both exit 0:

- `s1_bwrap.sh` prints `S1 part 1: PASS` (bwrap starts with user, pid, ipc, uts and net
  namespaces; a write outside the working directory is denied; no outside process
  environment is visible; `curl` to a non-allowlisted host fails; `socat` is installed);
- `factory-sandbox-check` prints `all expectations held` (Bash cannot write outside the working tree,
  a non-allowlisted host is unreachable, the API key is not visible to commands, a sibling repository's
  data is unreadable, repo settings cannot redirect the endpoint, ...).

**FAIL** if bwrap cannot create namespaces (`setting up uid map`, `Operation not permitted`) while running as
the worker identity, or the e2e check lists failed expectations. Do not set `enableWeakerNestedSandbox` to get past it.

## S2: Azure Files NFS

```sh
python3 /opt/factory-spikes/s2_nfs.py /data/spike-s2        # the NFS volume
python3 /opt/factory-spikes/s2_nfs.py /tmp/spike-s2         # reference: local ephemeral disk
```

It creates a 3,000-file git repository on the mount and measures: `git worktree add` and
`remove`, `git status` (best of 3), 100 atomic `os.replace` rounds while another process reads
the file continuously, `flock` exclusion between processes (including release when the holder is
killed), the executable bit and symlinks through git and directly, and the owner of new files.
`mount.fstype` in the report must show `nfs4` for the first run: **a first run on any other file system
does not count** (the script exits 0 on any path, because the local reference run must pass too).

**PASS** when the first run (on `nfs4`) exits 0 (`"pass": true`) and `git_status_seconds` is at most
10 seconds. The comparison with the local figure is a guide, not a gate: if `git_status_seconds` is
more than ten times the local number but still under 10 s, record both numbers and let a person
decide (no number is set, and a cold NFS attribute cache makes the first `git status`
of 3,000 files slow by nature). Record both JSON reports. Also run one real
`git worktree add` of `<<SANDBOX_TEST_REPO>>` on the mount if you can (clone it with a token you
supply for the spike; do not put the token in the file).

**FAIL** if `reader_torn_or_missing` is not 0, `flock` does not exclude, the exec bit or
symlinks are lost, or `git status` takes more than 10 s on the mount.

## S3: UID split with ambient capabilities

```sh
/opt/factory/bin/python /opt/factory-spikes/s3_uid_split.py --mechanism none     # baseline
/opt/factory/bin/python /opt/factory-spikes/s3_uid_split.py                      # what Factory does
```

The supervisor runs as uid 10001 (primary group `factory` 10001, supplementary `factory-work`
10010) with the ambient capabilities `cap_setuid`, `cap_setgid` and `cap_kill` and nothing else.
Workers are `Popen(user=10002, group=10010, extra_groups=[])` children.

Two things the original plan missed, found while writing this spike on a real kernel (so the
default is not "only setuid and setgid"):

1. The kernel clears capabilities on a UID change only to or from 0. A worker started by
   `Popen(user=)` from a supervisor with ambient capabilities therefore kept them through exec,
   could `setuid()` back to the supervisor or to root and read its environment. Factory now starts
   workers through `factory.execnocaps`, which clears the ambient set and execs the real command
   (fail closed). `--mechanism none` reproduces the problem and **must FAIL**; the default
   mechanism must PASS.
2. `kill(2)` between two different UIDs needs `CAP_KILL`. Without it the supervisor cannot kill a
   worker's process group (invariant 11), so `cap_kill` is ambient too. Workers get no
   capabilities.

**PASS** when the default run exits 0 and its report has `"pass": true`: the ambient set equals
the expected set, the worker has the worker uid/gid, no supplementary groups and zero
`CapPrm/CapEff/CapAmb`, every `os.setuid`/`os.setgid` attempt raises `PermissionError`, the worker
cannot read `/proc/<supervisor pid>/environ` nor any other readable `environ` containing the
canary, cannot list the supervisor's private directory, can write the setgid shared directory,
the supervisor can read what it wrote, and the supervisor can SIGKILL the worker's process group.
The `--mechanism none` run must show the failures listed above (that proves the test can see the
problem on this platform).

**FAIL** if `Popen(user=)` is refused (no ambient `cap_setuid`/`cap_setgid` on ACA), if the
worker keeps capabilities with the default mechanism, or if `environ` is readable.

### S3b: the data-tree permission scheme with real ids on the NFS mount

`s3_uid_split.py` runs on local disk. The scheme the supervisor uses to share only what the worker
needs (`0710` data directories, group-readable git directory and worktree admin directory,
group-writable setgid worktree, `safe.directory` through the per-invocation `HOME`) must also work
on the Azure Files NFS mount, where `chown`/`chgrp` and modes are subject to the server's id mapping.
It was first carried as part of S3; it is S3b here and it is part of the S3 record:

```sh
/opt/factory/bin/python /opt/factory-spikes/s3b_data_tree.py --data-dir /data/spike-s3b   # the NFS volume
/opt/factory/bin/python /opt/factory-spikes/s3b_data_tree.py                              # reference: local disk
```

It runs the real Factory code (`cli.prepare_worker_access`, `planner_worktree`, `permissions.share_worktree`,
`run_agent(user=, group=)`) against a local bare "origin": the worker (this script, in worker mode, without
network or Anthropic key) runs `git status`, `git log`, `git cat-file` and writes files in its worktree, and
tries to list or read the clone, the clone's working files and the run state; the supervisor then reads what
the worker wrote, fetches into the shared clone and adds a second worktree.

**PASS** when the NFS run exits 0 with `"pass": true` and `"fstype": "nfs4"` (a run on another file system does
not count). **FAIL** if the worker cannot use its worktree (`not a git repository`, `dubious ownership`), can
read a private path, or the supervisor cannot read, fetch or add a worktree afterwards: then the permission
scheme (`src/factory/permissions.py`, `cli.prepare_worker_access`) needs another design for NFS, for example one
worktree volume per uid, which is taken to the maintainers.

## Recording a result

Write the outcome into `spikes/S1.md`, `S2.md` or `S3.md` in a PR (these are the only files a
spike changes). Use this shape:

```markdown
# S<n>: PASS | FAIL        (keep the first line exactly like this)
- Date, operator, region, ACA environment name (no subscription id)
- Image digest under test (`sha256:...`) and Claude Code version
- Commands run (copy from above) and their complete output
- Anything that surprised you
```

S3.md records both `s3_uid_split.py` and `s3b_data_tree.py`. The deployment procedure in `infra/README.md` assumes all three are PASS.

## If a spike FAILs

| Failed | What changes | Files |
|---|---|---|
| S1 | (Historical: this is what happened; the move to AKS was made.) The in-container sandbox is impossible on ACA. Move to AKS with a node pool that allows unprivileged user namespaces (or Kata/pod sandboxing), same image, NFS and lease. Never weaken the sandbox. | `infra/` (ACA environment, container app, NSG and subnet parts replaced by AKS; now `infra/aks.tf`, `infra/k8s.tf`), `Dockerfile` entrypoint capability notes |
| S2 | Keep `state.json` on NFS (or a blob with ETag compare-and-swap) and put worktrees on the replica's ephemeral storage. A restart then loses unpushed worktrees: the job becomes `interrupted` and `retry` rebuilds from the branch tip. | `infra/k8s.tf` (volume layout; `infra/environment.tf` and `infra/app.tf` are ACA-era files that no longer exist), `src/factory/config.py` (`data_paths`), `docs/operations.md` |
| S3 | No UID switch is possible (or the supervisor cannot SIGKILL a worker process group: missing `cap_kill`). Run the token broker and git/gh in a **separate internal container app** (managed identity only) that the supervisor calls for push and PR operations; or move workers to dynamic sessions. | new `infra/` app, `src/factory/github_auth.py`, `Dockerfile` |

If a platform behaviour contradicts a frozen invariant, stop and ask; do not work around it.

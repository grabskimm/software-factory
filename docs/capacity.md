# Capacity: the pod worker budget, the memory guard and sizing

All worker sessions (`claude -p` processes for plan, build, review, verify and preflight) run as
children inside the one Factory pod. Each repository has its own supervisor process, so the
per-run limits (`--concurrency`, `max_active_agents`) say nothing about what the pod can carry:
a few busy repositories could ask for far more sessions than the pod fits, and exceeding the
container's memory limit gets the whole pod OOM-killed, every run with it.

This page describes what Factory does about it, the settings, how to size the pod, and what is
**not yet known**. Read "Unproven" before relying on any default.

| Control | What it does | Default | Turn it off or set it by hand |
|---|---|---|---|
| Pod worker budget | At most N worker sessions run at once across **all** supervisors of the pod; the surplus waits in a fair queue and `status` says why | derived from the container's cpu and memory (below) | `FACTORY_MAX_TOTAL_WORKERS=<n>` sets it, `0` or `off` removes the limit |
| Memory guard | Samples each session's process tree and kills a session whose resident memory exceeds a ceiling; the job fails with a clear reason | four fair shares of the pod's memory, at most the memory minus a reserve (below) | `FACTORY_WORKER_MEMORY_MB=<mib>` sets it, `0` or `off` removes the limit (the footprint is still recorded) |
| Footprint record | Every session records its peak resident memory and cpu time in its usage entry; `status` summarises peak and p95 per phase | always on where `/proc` is readable | none needed (it only reads) |

## The default is chosen not to change a running deployment

Before this change there was no pod-wide limit. A deployment that works today must keep working
exactly as it did unless the pod's real size says the sessions could not fit:

* The budget and the memory ceiling are computed **at runtime from the container's own limits**
  (cgroup v2 `cpu.max` and `memory.max`; cgroup v1 `cpu.cfs_quota_us` and
  `memory.limit_in_bytes`; the host's cpu count and `MemTotal` when a cgroup is unlimited or
  unreadable; the smaller value wins), not from a number baked into the image.
* The per-worker figures behind the budget are deliberately **generous** (small), so the budget is
  high. On the deployed pod (6 vCPU, 24 GiB) it is **14 workers** (measured figures, "Measurements" below; it was 21 on the
  earlier estimates of 1 GiB and 250 millicores per worker): two repositories at the
  per-repository defaults (2 build + 2 review + 1 verify, capped to `max_active_agents = 6`, so
  6 each) need 12, and run unqueued. A third busy repository, or a smaller pod, queues the surplus.
* The memory ceiling is generous but finite: **four fair shares** of the pod (a fair share is
  `(memory - reserve) / budget`, never below 1 GiB), at most everything except the reserve. The
  deployed pod gets **4 GiB** per session. A default ceiling of "everything except the reserve"
  (21 GiB there) was rejected: with more than one session running, the kernel OOM-kills the pod
  long before one session reaches it, so it would protect nothing. The cost of a finite default
  is that a session that really needs more than four fair shares is now stopped, where before it
  ran until the pod was OOM-killed. If a repository's builds need more, raise
  `FACTORY_WORKER_MEMORY_MB` (or set `off`); see "The memory guard" for what the ceiling does and
  does not protect.
* Both limits fail open. If the pod's cpu and memory cannot be read, if the state directory of the
  budget is not writable, or if `/proc` is not readable, sessions start without that limit and
  one fixed line says so on the supervisor's stderr (they are listed below). Nothing refuses to
  start and nothing is killed on a guess; a session is only ever stopped by a successful sample
  that is over the limit.
* With no `FACTORY_DATA` (local mode) there is no pool, exactly as before.

### How the default budget is derived

```
workers_by_cpu    = floor(cpus * 1000 / WORKER_CPU_MILLI)               # 400 millicores each
workers_by_memory = floor((memory - reserve) / WORKER_MEMORY_MIB)       # 1.5 GiB each
reserve           = min(3 GiB, memory / 4)
budget            = max(1, min(workers_by_cpu, workers_by_memory))
fair share        = max(WORKER_MEMORY_MIB, (memory - reserve) / budget)
memory ceiling    = min(memory - reserve, 4 * fair share)               # per session
```

The fair share follows the worker budget in force: `FACTORY_MAX_TOTAL_WORKERS` when set, else the
derived budget (also when the budget is turned off, as an estimate). The multiple is
`MEMORY_CEILING_SHARES`.

The constants are `WORKER_CPU_MILLI`, `WORKER_MEMORY_MIB` and `RESERVE_MAX_MIB` in
`src/factory/capacity.py`. They rest on the measurements below (2026-11-10, 97 sessions of the reference deployment): a worker is
mostly waiting for the model API (about 0.3 to 0.4 cores on average), but a session that builds and
tests reaches 1.4 to 1.5 GiB at the p95. Whether the agents plus their tests fit on the pool is
answered for the measured pod at the observed load; re-measure at the new concurrency. The reserve is for the manager, one supervisor per repository,
the short CLI commands the manager spawns (up to 6 `status` plus 8 others at a time), git and
the page cache. The budget is never below 1.

| Pod | by cpu | by memory | Budget | Memory ceiling |
|---|---|---|---|---|
| 6 vCPU, 24 GiB (the deployed D8 pod) | 24 | 21 | **21** | 4 GiB (4 x 1 GiB) |
| 4 vCPU, 16 GiB | 16 | 13 | **13** | 4 GiB (4 x 1 GiB) |
| 2 vCPU, 24 GiB | 8 | 21 | **8** | 10.5 GiB (4 x 2.6 GiB) |
| 2 vCPU, 4 GiB | 8 | 3 | **3** | 3 GiB (all but the reserve) |
| 1 vCPU, 1 GiB | 4 | 0 | **1** | 0.75 GiB (all but the reserve) |

**The derived figure (21 on the deployed pod) holds only while every worker runs inside the manager pod, as they all do today.** It comes
from the manager container's own cpu and memory and the slots are `flock` files on that pod's disk. A worker that ran in a pod of its own
(an execution backend proposed in #145 to #147: not implemented, off by default, nothing in this document applies to it) would use none of
that cpu or memory and hold none of those slots, so 21 would be wrong for such sessions: not a limit on them, and not a statement of what
the cluster could run. They would need ceilings of their own, set by the node pool's `limits`, the vCPU quota and a spend guard
and the per-session memory limit would be the pod's own.
Those ceilings are `src/factory/podpool.py` (`docs/pod-backend.md`, "The pod pool"): delivered, not wired in, and independent of
everything on this page.

## Settings

Environment of the manager (every supervisor inherits it). The values are never echoed into logs.

| Variable | Default | Meaning |
|---|---|---|
| `FACTORY_MAX_TOTAL_WORKERS` | derived | Positive integer: the pod worker budget. `0` or `off`: no limit. Anything else is reported once and ignored (the derived default is used). |
| `FACTORY_WORKER_MEMORY_MB` | derived | Positive integer: per-session memory limit in MiB. `0` or `off`: no limit. Anything else is reported once and ignored. |
| `FACTORY_CAPACITY_DIR` | `$FACTORY_DATA/.capacity` | Where the slot files and waiting tickets live. It must be on one local POSIX filesystem shared by every supervisor of the pod (the data disk). |
| `FACTORY_CAPACITY_WAIT_SECONDS` | `900` | How long a `plan` or `preflight` session (which run outside the scheduler) waits for a slot before failing with the reason. |

There is no Terraform variable for these: the defaults follow `container_cpu` and
`container_memory`, so nothing changes with an `apply`. To set one by hand, add the name to
`app_env` in `infra/locals.tf` (a persistent change that rolls the pod), or for a try-out
`kubectl -n factory set env deployment/factory FACTORY_MAX_TOTAL_WORKERS=8` (the next `apply`
reverts it; it restarts the pod, so drain first).
`$FACTORY_DATA/.capacity` is at the top of the data directory, which the worker sandbox hides, so
workers cannot read or alter it.

## The pod worker budget

The budget is a set of slot files under `.capacity/slots/`, each locked with `flock` by the
supervisor that runs a session in it. The kernel drops the lock when its holder dies, so a
killed supervisor or a crashed pod leaks nothing. A session of phase build, review or verify
(and the planner and the preflight probe) takes a slot before it starts and gives it back when
it ends. Publishing runs no agent and takes none, like `max_active_agents`.

A job that cannot get a slot stays `queued` / `waiting_*` and `status` shows

```
Wait for pod worker budget: 14/14 workers in use
Wait for pod worker budget: 12/14 workers in use; 2 waiting ahead
```

(`waits` in `status --json`, and the text line `Pod worker budget: 14/14 workers in use, 3
waiting` with the holders and waiters in the `capacity` key of `status --json`). Only counts are
shown, never names. The supervisor keeps polling (about once a second) and does not exit while
the budget is the only thing blocking; a drain still ends it at once. A wait for the budget is
transient, so it does not change the issue labels.

**Fair queueing.** A waiter leaves a short-lived ticket; when slots free up they go first to the
repository that holds the fewest slots (counting the picks already made, so two repositories
alternate), and among equals to the oldest ticket. A repository holding many slots cannot starve
one holding none. A ticket not refreshed for 15 seconds (a dead supervisor) is dropped. A waiter
that is granted the turn but loses the race for the file simply tries again on the next tick.

If an operator lowers the budget while sessions run, the running sessions are not touched; new
sessions wait until fewer than the new total are running.

A `plan` or `preflight` waits in the foreground (one stderr line per distinct reason) and fails with
`pod worker budget: ... no slot became free within N s` after `FACTORY_CAPACITY_WAIT_SECONDS`.

## The memory guard

A thread started with each worker reads `/proc` every 2 seconds, adds up the resident memory of
the worker's process tree (descendants, plus any re-parented process of a session a member leads),
tracks the peak and the cpu time, and, if one sample is over the limit, stops the session: the
worker's process group is frozen, every process of the sampled tree is killed one by one, and the
group is killed last (next section). The call raises

```
worker exceeded its memory limit: peak 5120 MiB over 4096 MiB; see <run>/<job>/<n>-<phase>.stderr
```

The message contains numbers only, never agent text. The job fails like any other failed phase
(`retry` or `resume` it); the usage entry keeps `subtype: memory_limit` and the footprint.

### How a tripped session is stopped, and where that stops working

A descendant that moved to its own session or process group (`setsid`, `setpgid`) is outside the
worker's group but inside its tree, so signalling the group alone leaves it running. Nothing is
killed before everything that can be found is frozen: a kill first re-parents the children of the
killed process, and one that forked after the sample was read and has left the group since (a
forker whose children call `setsid`, say) would belong to no member any more. So the watchdog:

1. Freezes (`SIGSTOP`) the worker's whole process group with one signal, so nothing in it can fork
   any further.
2. Scans `/proc` and adds to the tree every process whose parent is a member, or that is in a
   session a member leads or once led (an orphan whose parent died keeps its session).
3. Freezes every live member it has not frozen yet and scans again. A frozen process cannot fork,
   and its children stay attached to it, so the sweep kills only once a scan finds nothing new.
4. Kills (`SIGKILL`) every live member, pauses 50 ms and scans again until none is left.
5. The sweep ends after 4 seconds either way. If something is still alive then, it prints
   `factory: memory guard could not stop every process of an over-limit worker` (once per
   supervisor) and everything it froze has been sent `SIGKILL`; it is never left frozen. The
   supervisor waits for the sweep at most 5 seconds (`stop()` in `LocalBackend.launch`, which `agent.run_agent` runs), so a session
   that cannot be stopped never blocks it for longer.
6. Sends `SIGKILL` to the worker's process group last, whatever happened before: if `/proc` could
   not be read, the group is killed, not left frozen.

What keeps the sweep from signalling the wrong process:

* A process is identified by its pid **and** its start time from `/proc/<pid>/stat`. A pid that
  was reused, by a stranger or by a new child of the worker, is a different process and is
  handled as one: a stranger is never signalled, and the new child is adopted and killed.
* The signal goes through a pidfd (`pidfd_open`, then the start time is checked against that
  handle, then `pidfd_send_signal`), so a pid cannot be reused between the check and the signal.
  Where the kernel or its syscall filter has no pidfd, the start time is read again right before
  `kill(2)`; only that window remains.
* pid 1, the supervisor itself, its parent and every ancestor (the manager) are never signalled
  and never adopted into the tree, whatever `/proc` shows; a session or parent link into them is
  ignored. If `/proc` or a start time cannot be read, nothing is signalled for that process.
* The watchdog pins the worker's start time at its first sample. If the worker's pid is later
  reused (the supervisor reaped it), the sample is treated as "no worker", never as a stranger's
  memory over the limit.
* `/proc` is read as bytes. A worker names its own processes (`prctl`, the file name it runs): a
  name that is not UTF-8, or that holds spaces, parentheses or fake fields, no longer hides the
  process from the sum, and cannot hide the whole tree if it is the worker's own.

What the sweep cannot do:

* **A process that detaches before a scan sees it is outside the tree**: neither counted nor
  killed. Double fork plus `setsid` makes its parent init and its session its own, so nothing
  links it to the worker. Only a cgroup (kill the whole group at once) closes that, and the pod
  cannot write cgroupfs. A worker that does this on purpose can outlive its session and keep
  its memory.
* Between two samples (2 s) nothing is seen. A fork bomb that exhausts the pids or the memory
  inside one interval is not stopped in time; Kubernetes acts first.
* A process in uninterruptible sleep (`D`) cannot be signalled until it wakes: the sweep gives up
  after 4 seconds and says so.
* The scan reads every `/proc/<pid>/stat`: a pod with very many processes makes each pass slower
  (still bounded by the 4 seconds, plus the pass in flight). A pid that is reused *within* one
  scan of `/proc` could be attributed to the wrong parent; the signal itself is still guarded
  by the start time of the process it is sent to.

What it is **not**: a kernel limit.

* It is sampled. A fast allocator can pass the limit between two samples, and Kubernetes may
  still OOM-kill.
* It protects the pod only while the rest of the pod leaves room for a session to reach the
  ceiling. A runaway is stopped first only if the other sessions, the manager and the supervisors
  together use less than `container memory - ceiling` at that moment (20 GiB on the deployed pod
  with a 4 GiB ceiling). At the budget's full load, with every session at the 1 GiB estimate, that
  margin is close to nothing, so the kernel can still act first; a ceiling nearer the fair share
  (`FACTORY_WORKER_MEMORY_MB`, "Sizing" step 5) widens it at the price of stopping heavier
  sessions. The reserve covers the manager, the supervisors and git, not the other workers.
  Whether this holds on the real pod is unproven (below).
* Resident size double counts shared pages, which errs towards stopping a session that is really
  over.
* `RLIMIT_AS` was not used: it is per process, not per tree, and Node reserves huge virtual
  ranges. Per-session cgroups were not used: the pod is unprivileged (`hostUsers: false`) and
  cannot write cgroupfs.
* It needs `/proc/<pid>/stat` and `/proc/<pid>/statm` (Linux). Without them the session runs
  unlimited and records no footprint.
* Stopping the session reaches the processes it can see (above): a worker that detaches a process
  on purpose can outlive the kill.

### Lines the supervisor prints when a limit is off (fixed text, no values)

| Line | Meaning |
|---|---|
| `factory: capacity: pod worker budget off: the pod's cpu and memory could not be read` | nothing could be read from cgroups, `/proc/meminfo` or the cpu count; no budget |
| `factory: capacity: memory guard limit off: the pod's memory could not be read` | no default memory ceiling; the footprint is still recorded |
| `factory: capacity: pod worker budget off: its state directory cannot be created` | `.capacity` could not be made |
| `factory: capacity: pod worker budget unavailable: its state directory is not usable; running without it` | a slot operation failed at run time; that request was admitted without a slot |
| `factory: memory guard unavailable: /proc is not readable; sessions run without a memory limit or footprint record` | not Linux, or `/proc` hidden |
| `factory: memory guard unavailable: its thread could not start; session runs without it` | the sampling thread could not start |
| `factory: memory guard cannot read this worker's process tree; the session runs without a memory limit or footprint record` | two samples in a row found no worker (for example `/proc` shows another pid namespace); the session runs unlimited |
| `factory: memory guard could not stop every process of an over-limit worker` | after 4 s something of the tripped session's tree was still alive (a process in uninterruptible sleep, or `kill` refused); the job still fails with the memory reason |
| `factory: memory guard could not signal a worker process: <reason>` | the supervisor may not signal a process of the worker (for example `Operation not permitted`: no `CAP_KILL` for the worker's uid); the reason is the operating system's text, never the worker's |
| `factory: capacity: FACTORY_MAX_TOTAL_WORKERS is not a positive integer, 0 or off; using the derived default` (and the same for `FACTORY_WORKER_MEMORY_MB`) | a typo in a setting; the derived default is used |

## Footprint record and how to measure

Every session's usage entry (`state.json`, `jobs.<id>.usage[]`) carries

```json
"footprint": {"peak_rss_mb": 812.4, "cpu_seconds": 63.1, "samples": 41, "limit_mb": 21504}
```

`peak_rss_mb` is the highest sampled sum of resident memory of the worker's tree, `cpu_seconds`
the sampled user plus system time of the tree (an approximation: cpu of a descendant that exited
between two samples is only counted once its parent has reaped it), `samples` how many reads it
rests on, `limit_mb` the limit in force (`null` when none). Sessions shorter than the 2-second
interval are under-sampled: read `samples`. `status --json` summarises them read-only, per phase:

```
"footprint": {"build": {"invocations": 12, "peak_rss_mb": 1630.2, "p95_rss_mb": 1411.0,
                         "peak_cpu_seconds": 212.4, "p95_cpu_seconds": 180.3}, "review": {...}}
```

Entries written before this feature, or on a platform without `/proc`, carry none and count as
nothing.

### Procedure (not run; needs a live pod and real repositories)

1. On the real pod, enrol the repositories whose sizing matters and run a representative batch
   (several build, review and verify sessions, at least 20 per phase, at the concurrency you
   intend to use).
2. Read `status --json` for each run: the `footprint` block gives invocations, peak and p95 per
   phase. Copy them into the table below with the repository, the date and the image tag.
3. Cross-check against the pod: `kubectl -n factory top pod --containers` while the batch is at
   its widest, and the container's `memory.peak` / `cpu.stat` if the node exposes them. The
   sampled figures are a lower bound on the instant peak.
4. Derive per-worker values (for example peak p95 of the phase mix) and either change
   `WORKER_CPU_MILLI` / `WORKER_MEMORY_MIB` or set `FACTORY_MAX_TOTAL_WORKERS` and
   `FACTORY_WORKER_MEMORY_MB` explicitly.

### Measurements

**Measured 2026-11-10 on one deployed pod (read-only), the reference deployment.** Sources: the footprint records in the `state.json` of every retained run on
the pod's data volume (`usage[].footprint`: `peak_rss_mb`, `cpu_seconds`; 97 sessions with a record, the runs of 2026-11-07 to 2026-11-10), the phase
times in the same files (`phase_events`), the manager's `/metrics` through the cluster Prometheus (`factory_capacity_workers`,
`factory_phase_p95_rss_mib`, `factory_jobs`) and cAdvisor for the pod. Nothing was run to produce load; these are the sessions the deployment did.

| Phase | Repository | Sessions | Median RSS (MiB) | p95 RSS (MiB) | Peak RSS (MiB) | Median cpu (s) | p95 cpu (s) | Peak cpu (s) |
|---|---|---|---|---|---|---|---|---|
| build | repo-a | 30 | 365 | 1,468 | 1,751 | 102 | 467 | 644 |
| review | repo-a | 29 | 330 | 1,377 | 1,404 | 31 | 455 | 479 |
| verify | repo-a | 30 | 348 | 1,375 | 1,383 | 163 | 460 | 469 |
| build | repo-b | 4 | 248 | 275 | 276 | 3 | 5 | 5 |
| review | repo-b | 4 | 266 | 273 | 274 | 4 | 10 | 11 |
| plan | (all) | no record | | | | | | |

Wall time per phase (`phase_events`, all repositories): build median 4.9 min (n 77, p95 27.9), review 1.9 min (76, p95 10.9), verify 3.5 min (39, p95 23.6), publish 0.1 min.
`repo-b` has 4 sessions per phase, below the 20 this section asks for, so its figures are indicative only; `repo-a` has 29 to 30 per phase (the two repositories of that deployment).

**Reading them.** (1) Memory: the median session holds 250 to 370 MiB; the heavy tail (p95 1.4 to 1.5 GiB, the test suite and the build of `repo-a`) is
what a budget has to cover, and it is above the earlier 1 GiB estimate. (2) CPU: the p95 session used 450 to 470 cpu-seconds over a median of 2 to 5 minutes
of wall time and a p95 of 11 to 28, i.e. about 0.3 to 0.4 cores on average while it ran; the pod's 5-minute cpu rate peaked at 2.7 cores in 24 hours (0.1
to 0.5 cores on average) with up to 3 workers at once. (3) Concurrency actually reached: `factory_capacity_workers` peaked at **3** of the budget of 21 in 24
hours, and `factory_manager_children` at 2 runs and 1 append; at most 2 builds and 2 reviews of one repository were running at once. The limit the
deployment hit was the supply of work and the per-repository settings, not the pod. (4) **UNVERIFIED, not measured:** page cache against working set over time (the
manager's working set peaked at 1,635 MiB of 24 GiB), and how many workers ran at once before 2026-11-09 (no series; `factory_capacity_workers` is the
pod-wide count in use and is now the thing to graph).

**What changed from this (the constants and defaults).** The ceiling of 14 workers is **UNVERIFIED** under load (never more than 3 ran at once). `WORKER_MEMORY_MIB` 1024 to **1536** (the p95 of the heavy repository) and `WORKER_CPU_MILLI` 250 to
**400** (the average per running worker, rounded up for the bursts of a test run): the budget on the deployed pod falls from 21 to **14** workers and the
per-session memory ceiling (four fair shares) rises from 4 GiB to **6 GiB**, against a measured peak of 1.75 GiB. `resources.max_active_agents` 4 to **6**, so
that one repository can use more of a pod that was observed at 3 workers. The per-run flags keep their defaults (`--concurrency 2`, `--review-concurrency 2`,
`--verification-concurrency 1`; the last one stays low on purpose: verification runs the repository's test suite). `FACTORY_MAX_TOTAL_WORKERS` still overrides the
derived figure, and the memory guard stays on.

**What the pod carries, and what it costs.** The hourly figures are **UNVERIFIED** estimates (a median session priced per wall minute, not a billed hour). At the p95 footprint, 14 workers hold 14 x 1.5 = 21 GiB (the pod has 21 GiB beyond its reserve) and about 14 x 0.4 =
5.6 cores of the 6, so 14 is the safe ceiling; at the median footprint the same pod could hold five times as many, which is why the guard rests on the p95 and
the memory ceiling, not on the budget. Cost: the recorded sessions cost a median of $0.29 (build), $0.40 (review) and $0.03 (verify) and $67.5 in all for 193
sessions in these runs; a worker that builds then reviews continuously spends about **$4 an hour** (a build of 4.9 minutes at $0.29, a review of 1.9 minutes at
$0.40), so the budget of 14 workers at full tilt is about **$55 an hour**, 6 workers (one repository at the new default) about $25 an hour. The limits that bind
before the pod: the model's rate and spend limits (`run_budget_usd` per run, and the stop-the-world breaker below), GitHub API limits, and the single node.
**Suggested settings, not applied:** `auto_build_concurrency = 2` for `repo-b` (its sessions held 270 MiB and 3 to 10 cpu-seconds); `repo-a` already has 2.
Re-measure after a week at the new settings with `tools/cache_report.py` and the `status --json` footprint block; the procedure above is unchanged.

## Sizing the pod

1. **Count the concurrent sessions you want**: repositories that run at once times their
   `max_active_agents` (default 6), or the pod-wide number you accept.
2. **Pick per-worker figures**: from the table above once it is filled in; until then the
   estimates (0.25 vCPU, 1 GiB) and the knowledge of what your repositories' builds and test
   suites need. A repository whose tests are heavy needs more per worker than a documentation
   repository.
3. **Set the container**: `container_cpu` and `container_memory` in `infra/terraform.tfvars`
   (request equals limit). `memory = workers * per_worker_memory + reserve`; the reserve is 3 GiB
   on a large pod. They may use at most 75 % of the node (`infra/variables.tf` checks it): 6 vCPU
   and 24 GiB on `Standard_D8ds_v5`.
4. **Pick `aks_user_pool_vm_size`** so the 75 % cap covers step 3. D-series nodes have 4 GiB per
   vCPU, E-series 8 GiB per vCPU, so memory-hungry workloads should move to an E-series size
   (`Standard_E8ds_v5` allows 48 GiB). Changing the VM size replaces the node, and with a single
   replica on a ReadWriteOnce disk that is a rollout with downtime: drain first
   (`docs/operations.md`).
5. **Leave the defaults** (the budget follows the container, the memory ceiling is four fair
   shares) or set `FACTORY_MAX_TOTAL_WORKERS` and `FACTORY_WORKER_MEMORY_MB` by hand in the
   container environment. For protection that holds at full load use a ceiling near one fair
   share: `FACTORY_WORKER_MEMORY_MB = (container_memory - reserve) / budget`, as long as no
   build needs more.
6. **Watch `status`**: sustained `pod worker budget` waits mean the budget, not Factory, is the
   limit on throughput: raise the container size (step 3) or accept the queue.

### Right-sizing the manager and the fixed node (issue #148)

**Measured (2026-11-10, the section "Measurements" above):** the manager pod's working set peaked at 1,635 MiB of 24 GiB over 24 hours and its 5-minute CPU rate at 2.7 cores of 6 (0.1 to 0.5 on average), **with up to 3 claude workers running inside it**. The node carries 85 % of its CPU and 96 % of its memory as *requests* (container 6 vCPU / 24 GiB, the add-on pods and the kubelet reserve) and uses a fraction of them. The `factory` pool is one `D8ds_v5`, $0.452 an hour, about **$330 a month**.

**Decision.** Two sizes, tied to where the workers run:

| State | `aks_user_pool_vm_size` | `container_cpu` / `container_memory` | Why |
|---|---|---|---|
| Workers run in the manager pod (today, `execution = "inprocess"`) | `Standard_D8ds_v5` (unchanged) | 6 / `24Gi` (unchanged) | 14 workers at the p95 of 1.5 GiB and 0.4 cores are the reason for the size (the section above). Shrinking it would shrink the budget with it. |
| Every enrolled repository has `execution = "pod"` and a week of sessions has run | `Standard_D4ds_v5` ($0.226 an hour, about $165 a month) | 3 / `12Gi` (the 75 % cap of 4 vCPU / 16 GiB) | The manager then keeps only the supervisors, the relay, the launcher and git. Its measured peak (1.6 GiB, 2.7 cores) already contained the workers; without them the need is a fraction of it. |

**Saving: about $165 a month** (the VM, half of the fixed node). Together with NAP, whose worker nodes cost nothing at zero sessions, the floor becomes the system node, the manager node and the `mon` node.

**Terraform change: none, on purpose.** `aks_user_pool_vm_size`, `container_cpu` and `container_memory` are variables already; changing the *defaults* would resize the node (a replace of the pool and a rollout of the manager on its ReadWriteOnce disk) at the next apply of any pull request, with the workers still in the pod. The values are set in `INFRA_TFVARS` as the last stage of the migration (`infra/README.md`, step (5)), through the drain gate, **after** the last repository runs in pods, and the plan's check (75 % of the node) accepts exactly the pair above.

**To check before the last stage (UNVERIFIED, not measured):** the manager's own working set and CPU with no in-process worker (graph `container_memory_working_set_bytes` and `rate(container_cpu_usage_seconds_total[5m])` of the `factory` container for a week in pod mode; the stage waits if the p95 is above 4 GiB or 1.5 cores), and the relay's memory while three transcripts stream. The S1 constraint does not apply to the manager node once no worker runs bubblewrap in it, but **`factory-sandbox-check` and the Premium-disk attach are re-run after the node is replaced** (`docs/operations.md`, "Platform maintenance"). A metrics scraper does not change this: it runs on the `mon` pool.

## Stop-the-world breaker: spend limits and 429 storms

Issue #146 (slice 1 of the pod backend program, `Refs` only: nothing in it needs a pod). A spend-limit 429 carries no `retry-after`,
and "retrying, including the SDK's automatic retries, fails until access resumes". Every session
fails the same way, so a trigger that keeps starting runs only multiplies the failures. Factory therefore classifies the API error behind
a failed session into a **closed code** and, for the codes that cannot clear by themselves, **pauses the trigger and drains**. It never
retries and never resumes by itself.

**The classifier** (`src/factory/apierror.py`) reads the final `result` event of the session (or, when there is none, the last 64 KiB of
`<n>-<phase>.jsonl`) and returns one code. The rules are the table `SIGNATURES`, each row naming the fields it reads; a shape that no row
recognises is `other` or `rate_limit_unknown`, **never `spend_limit`**:

| Code | What it means | Read from (assumed shapes) |
|---|---|---|
| `spend_limit` | a spend cap was reached | status 429 and the type `enforced_spend_limit_reached` (the one documented by Anthropic) in `terminal_reason`, `subtype`, an `error` type or the text of `result` |
| `rate_limit` | a rate limit that clears by itself | status 429 and the type `rate_limit_error`, or a `retry-after` value |
| `overloaded` | the API is overloaded | status 529, or 500/503 with the type `overloaded_error` |
| `auth` | the Anthropic key was refused | status 401 |
| `rate_limit_unknown` | a 429 that no row recognises | status 429, nothing else matched |
| `other` | an API error (a status or an `error` type) that no row recognises | any other status |
| `none` | the failure carries no API-error evidence (`error_max_turns`, a failed task) | never recorded, never counted |

The text of `result` is the one free-form field, and a model can write it: it is searched for the fixed type words **only when the
event carries an integer status** (which the model cannot set), only in its first 4096 characters, and it is never copied. The classifier
returns a code and nothing else, so no agent text reaches the state, a log line or a metric. `AgentError` carries the code
(`api_error`), the runner records the **closed code and the time** in the failed job's state (`api_error`, `api_error_at`; a later failure
without one clears them), and `status --json` shows both for a failed job.

**Trip rules** (`src/factory/stopworld.py`, constants `RULES`):

| Code | Trips |
|---|---|
| `spend_limit` | at once |
| `auth` | at 2 within 5 minutes of each other, across the manager (a bad key fails every session; one event can be a misread, two in five minutes cannot) |
| `rate_limit_unknown` | at 3 within 5 minutes of each other, across the manager (the safety net for a spend-limit shape the table does not know yet) |
| `rate_limit` (with its `retry-after`), `overloaded`, `other`, `none` | never; they only count in `factory_api_errors_total` |

The manager's operations loop reads `api_error` from `status --json --offline` (every `FACTORY_MANAGER_POLL_SECONDS`; the report is built from the
run's own state, so the breaker makes no GitHub call) for each run it started that is running, **and once for each run that exited since the last pass** (a run whose sessions all fail can end
in seconds). One failure counts once (job, attempt and time), and a failure older than 15 minutes, or recorded before the last clear,
is history and does not trip. The 5 minutes of `auth` and `rate_limit_unknown` are measured **between the failures' own times**, not
from the moment the poll reads them: two failures 10 seconds apart count as two whether they are read as they happen or all at once
400 seconds later (a poll interval of 5 minutes or more, up to the hour `FACTORY_MANAGER_POLL_SECONDS` allows, or a restart), and two
failures more than 5 minutes apart never count as two. Only the 15-minute freshness looks back from the reading.

**A trip** (idempotent, restart-safe, `factory_stop_world_trips_total{code}`):

1. sets its **own marker** `$FACTORY_DATA/spend-breaker` (mode `0600`, JSON `{"code", "at"}`), which is **not** the manual pause marker
   `trigger-paused`: `tools/infra_deploy_gate.py resume` clears the manual marker after every rollout it brackets, which would
   clear a breaker while the spend limit is still hit. `trigger_paused()` is true while either marker exists; `POST /trigger-pause`
   `{"paused": false}` clears only the manual one and still answers `{"trigger_paused": false}`, so the gate passes;
2. requests the existing `drain` on **every run in progress** (the same path as the cost guardrail), once each (marker
   `spend-breaker-drained` in the run directory, so a restart does not forget it). A run an Operator starts later, while the breaker is
   set, is drained again within one poll. An Operator's own HTTP `run` is not refused;
3. logs one fixed line (`breaker code=<code> repo=<r> run=<r> action=trip`, then `action=drain` per run) and shows
   `breaker: {code, at}` in `GET /repos` (`trigger_paused` there is the OR of the two markers) and in `status`.

**Clearing** is a person's decision, after the limit was raised or the key replaced: `POST /spend-breaker` with `{"clear": true}`
(Operator only, audited like every request, `docs/manager.md`). It resumes nothing: the trigger starts new work again, and a drained
run is started by a person (`run`). The clear is stamped (`spend-breaker-cleared`), so the failed jobs that remain in a run's state
cannot trip it again.

**Not covered.** Only the failed jobs of a run are read: a plan session or an append that hits the limit leaves no job, and the label
trigger's own backoff (60 s doubling to 1 h) is what paces it. A preflight session fails its job and is read like any other. **The
Anthropic Console workspace spend limit is a human action** (`docs/operations.md`, "Cost guardrails"): the breaker reacts to the limit,
it does not set one. The runbook that records the real error shapes is slice 21 of the pod backend program.

**UNVERIFIED.** The real `claude` error for a spend limit, a 401 and a 429; that a 429 reaches the `result` event at all (the tail of the
log is the fallback); and Anthropic's acceleration limits. The tests inject **assumed** shapes, so they pin the rules and not the real
events; recordings of a live run replace them (slice 22). Until then an unrecognised spend-limit shape is `rate_limit_unknown` and
trips after three in five minutes, never at once.

## Scaling past one pod

More replicas of the same Deployment cannot work today: each run's `state.json`, worktrees and
clones live on that pod's own data disk (ReadWriteOnce; NFS is ruled out by `hostUsers: false`),
and the ingress would send a call to any replica, including one that never saw the run. The blob
lease only prevents two supervisors on one repository; it does not share data. In increasing cost:

1. **Vertical**: a larger node pool VM and `container_cpu` / `container_memory` (replaces the node).
2. **Sharded deployments** (or a StatefulSet): each pod owns a set of repositories with its own
   disk and lease; routing sends `/repos/<org>/<repo>/...` to the owner. Capacity grows with the
   number of repositories, not with sessions on one repository.
3. **A pod per run** with the cluster autoscaler: scales with demand but needs run state off the
   local disk (for example blob state with compare-and-swap). A real redesign.

### Decision: partitioning by repository needs Terraform and routing, not Factory code

Several Factory deployments, each owning a subset of repositories, work with the code as it is:

* A repository exists for a deployment if and only if its config file is in that deployment's
  `FACTORY_CONFIG` (`repo_configs` in Terraform), so a shard is a deployment whose
  `repo_configs` lists only its repositories.
* Everything a repository owns is below `$FACTORY_DATA/repos/<org>/<name>` on the shard's own disk,
  and its lease is the blob `<org>__<name>`. If a repository were configured in two shards by
  mistake, the lease still lets only one supervisor run; that is the safety net.
* The pod worker budget lives on the shard's own disk and reads the shard's own container
  limits, so every shard gets its own correctly sized budget with no cross-pod mechanism. This is
  why the budget deliberately uses local `flock` files and nothing shared.

What a shard needs that is **not Factory code and not done here**: the Terraform root applied once
per shard (own cluster node or pool, disk, identity and `repo_configs` subset), and an ingress or
gateway rule that maps each repository path to its owner shard. `GET /repos` of one shard lists
only its repositories; a client that wants one view merges the lists.

Use it when the budget waits (above) persist even at the largest node you are willing to run, or
when blast radius matters (one shard's OOM cannot stop another's runs). Until the measurements
show the single pod is the limit, vertical scaling is the cheaper step. A pod per run (option 3)
would need code (run state off the disk) and is not started.

## Unproven

* **UNVERIFIED: the measurements are one repository deep.** `repo-a` has 29 to 30 sessions per phase, `repo-b` 4; at most 3 workers ran at
  once, so the behaviour at 10 or more is extrapolated from the p95 footprints, not observed. Page cache against working
  set over time was not measured. Re-measure after a week at the new defaults.
* The guard has been exercised only with stub processes, a fake `/proc` and, for the kill, real
  processes on a development machine (a descendant in its own session, a chain of sessions, a fork
  flood in a group of its own, a process with a command name that is not UTF-8, pid reuse by start
  time, the time bound). That it reads the right tree under the real sandbox (`bwrap` pid
  namespaces, the worker uid, `/proc` visibility), that `pidfd_open` and `pidfd_send_signal` are
  allowed by the pod's seccomp profile (the sweep falls back to `kill(2)`, but that has not been
  seen on the pod), that the supervisor may signal the worker's uid there, and that it protects the
  real pod against a Kubernetes OOM kill is untested. Nothing here was run in a cgroup, a pid
  namespace of its own or on Kubernetes.
* The default ceiling (four fair shares, 6 GiB on the deployed pod) rests on the 1.5 GiB estimate.
  If real builds need more, they are stopped by the default until `FACTORY_WORKER_MEMORY_MB` is
  raised; if real sessions use far less, a tighter value protects more. Only the measurements
  can settle it.
* The cgroup layouts were tested with fake trees (v1 and v2), not on the AKS node.
* The stop-the-world breaker (above) was tested only against assumed API-error shapes; no real spend-limit, 401 or 429 event was recorded.

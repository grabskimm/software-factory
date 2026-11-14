# Metrics

Factory exposes numbers about itself in the Prometheus text format (0.0.4) from the manager process. This page is the catalogue
(name, type, labels, meaning, owner, what happens on a restart), the rules that keep the labels bounded, the threat model and the
list of what is **not yet exposed**. It is issue #48 in two phases: the code that produces and serves the numbers, and the deployment
(`infra/README.md`, "Monitoring": Prometheus and Grafana as plain manifests in the same cluster, the dashboards and alert rules as files, the bearer token on the
listener, a prepared network policy). The alert rules and the dashboards under `infra/monitoring/` use only the metrics of this catalogue and the platform series they name (cAdvisor's `container_*`, and kube-state-metrics' `kube_*` with the recorded `factory:*` of "NAP-side series"), and tests fail when one does not.

## Turning it on

The listener is **off** unless `FACTORY_MANAGER_METRICS_PORT` is set.

| Variable | Default | Meaning |
| --- | --- | --- |
| `FACTORY_MANAGER_METRICS_PORT` | unset (off) | The port of the metrics listener (0 to 65535). Unset, empty or `off` means no listener. `0` picks a free port (tests). It may not equal `FACTORY_MANAGER_PORT`: a clash exits 2 naming the variable, and a port that cannot be bound exits 2 as well. |
| `FACTORY_MANAGER_METRICS_HOST` | `127.0.0.1` | The address it binds. Anything but loopback exposes it to everything that can reach the pod (see the threat model), so it **needs a token**. |
| `FACTORY_MANAGER_METRICS_TOKEN` | unset | The bearer token of the listener: 32 to 512 printable characters without spaces (surrounding whitespace is trimmed). Set, the listener answers `401` (with `WWW-Authenticate: Bearer`) to any `GET` that does not carry `Authorization: Bearer <token>`, for every path. It is compared in constant time (digests of both sides), never logged, never in a `repr`, and not passed to any child process (the CLI children, `git`, `gh`). **Unset with a host that is not a loopback address, the manager exits 2 before listening on anything**, with the fixed message `FACTORY_MANAGER_METRICS_TOKEN is required when FACTORY_MANAGER_METRICS_HOST is not a loopback address`. `localhost` and loopback literals (`127.0.0.0/8`, `::1`) count as loopback; every other name or address does not (fail closed). |

The listener is a second, separate server inside the manager process. It answers `GET /metrics` and nothing else (`404` for any other
path, `405` with `Allow: GET` for any other method, `503` when more than 8 scrapes are in flight), with
`Content-Type: text/plain; version=0.0.4; charset=utf-8` and `Cache-Control: no-store`. It does **not** check an Entra identity (the numbers carry no
secret and no text, and a Prometheus scraper has no Entra token; the bearer token above is its own check), and it writes **no audit line** and no log
line: a scrape every 15 seconds would drown the audit trail the API keeps. The API port has no `/metrics` path: it answers `401` without an identity and `404` with one.
The Deployment sets `FACTORY_MANAGER_METRICS_PORT=9464`. Without monitoring it does not set the host, so on AKS the listener is on the loopback of the pod (a process in the
pod reads it with `kubectl exec`) and `infra/` does not declare the port. With `monitoring_enabled` (`infra/README.md`, "Monitoring") it sets the host to the pod address, declares the
port on the container, adds the token from Key Vault, and a ClusterIP Service `factory-metrics` is the only way to it; the ingress does not route it.

```sh
FACTORY_MANAGER_METRICS_PORT=9464 factory-manager &
curl -fsS http://127.0.0.1:9464/metrics | head
```

## Where the numbers come from

All of it is computed inside the manager process. A scrape starts no process, runs no `factory` command, makes no GitHub call and mints no token.

* **`live`**: memory of this process (requests, children, the pause marker, the GitHub App check table, the false-incomplete cache, the label
  watcher's counters and backoff), plus two cheap host reads: the slot files of the worker budget (`docs/capacity.md`) and the disk usage of
  `FACTORY_DATA`. Counters start at 0 when the manager restarts; Prometheus `rate()` and `increase()` treat that as a counter reset.
* **`ledger`**: totals worked out at scrape time from the history ledger (`docs/manager.md`, "History ledger"), which outlives `purge`: runs and
  jobs finished by outcome, reason codes, purges, USD, token counts per phase, the plan-to-pull-request time and the time per phase. The ledger is read through the
  same bounded, change-keyed cache as `GET /dashboard` (at most `8 MiB` of the newest events) and aggregated at most every 30 seconds. These
  are the series that would otherwise reset when the janitor purges a run, and they do not.
* **`runs`**: the run directories that still exist (`state.json` of at most 50 runs per repository and 200 in all, refreshed at most every 30
  seconds). Jobs by status and phase, USD of the retained runs by phase, `run_budget_usd` against the most any retained run spent, the sampled
  footprint per phase. A purge removes the run, so these are **gauges** and are named as gauges: they fall, and none of them ends in `_total`.

**`factory_jobs` is the raw `status` of `state.json`, and `reconcile` never changes it.** The idle `reconcile` writes only labels and
commit statuses (`docs/operations.md`, "Pull requests merged or closed after the run went idle"); it never sets a job's status in `state.json`
to `merged` or `closed`. So a job whose pull request was merged keeps reading `pr_ready` (or `pr_incomplete`) in `factory_jobs` until its run is
purged, and a merged `pr_ready` job is a `pr_ready` series, not a `merged` one. `FactoryJobsWaitingForHuman` selects
`status=~"needs_human_publish|blocked"` only, so a `pr_ready` job, merged or open, **adds nothing to it**; what it counts is a job that needs a
person to publish it or whose blocker needs one (tested over `infra/monitoring/alert-rules.json` and the `factory_jobs` collection, `tests/test_metrics.py`).
The janitor does not read this gauge: its terminal verdict uses the live `handoff_status` that `status` reads from GitHub, which is why a run
of merged `pr_ready` jobs is purged although `state.json` still says `pr_ready`. Until it is, its cost and reason codes are not in the ledger
counters (`factory_cost_usd_total`, `factory_job_reason_codes_total`): the `purge` event that carries them is written when the run is purged.

### How ledger counters stay honest

A figure worked out from a window over the ledger can shrink: the read bound slides past old events, the size cap or retention removes a
segment, a blocked job is retried and is no longer blocked. A Prometheus counter that goes down looks like a reset and makes `rate()`
report a spike. So each ledger counter and each histogram **follows its source figure but never goes down inside one manager process**:
the first figure seen becomes the value, every later increase is added, every decrease is ignored. After a manager restart the series starts again from
the figure the ledger holds then, so it continues smoothly while the ledger still has the history. Consequences to know:

* A job that finishes `blocked`, is retried and finishes `blocked` again counts twice in `factory_jobs_finished_total{outcome="blocked"}`:
  the counter counts times a job was seen finishing, not distinct jobs. `merged` and `closed` are final.
* When the ledger is larger than the 8 MiB read bound, or the cap removes segments, the totals cover the newest part only and new work that
  coincides with old events leaving the window can be missed until the window grows again. `factory_history_truncated` is `1` while the last read left
  older events unread, and `factory_history_bytes` is the size on disk: alert on the first one before trusting a rate over a long span.
* The ledger knows when a run was planned, not when its issue was created or labelled, so the lead time
  `factory_plan_to_pr_seconds` starts at the planning (the closest start the ledger has), not at the issue. A phase time is a stretch in which a job
  was `running` in one phase, from its first event to the next event of the job; an open stretch is not counted and a stretch longer than a day is
  dropped (the manager was down in the middle of it).
* The ledger holds the cost of a run as a total, not per phase, so `factory_cost_usd_total` is per repository; the USD per phase exists only for
  the retained runs (`factory_retained_cost_usd`, a gauge).
* The ledger does hold **token counts per phase** (the `job` entry's `tokens`, the `planned` entry's `planner_tokens`: `docs/manager.md`, "History ledger"),
  so the four token counters below carry a `phase` label and, like the other ledger counters, do not go down when the janitor purges a run. A job's counts
  are cumulative over its sessions and the series follows the latest figure the ledger holds for each job; the planner's counts are the `plan` phase, those of the `plan` that created the run only (a later `plan --append-to` session is in `status --json` but not in the ledger or in these counters). A
  session that reported no usage (an older run, a session that failed before its result) adds nothing. The split of cache writes by 5 minute and 1 hour
  lifetime is in `status --json` and the ledger, not in a metric. The hit rate is not a metric either: compute it in PromQL from the counters,
  `increase(factory_cache_read_input_tokens_total[1d]) / (increase(factory_cache_read_input_tokens_total[1d]) + increase(factory_cache_creation_input_tokens_total[1d]) + increase(factory_input_tokens_total[1d]))`
  (cache read over cache read plus cache write plus uncached input; the same definition as `status --json`, `docs/operations.md`, "Tokens and cache hit rate").

## Catalogue

Names are prefixed `factory_`. Every metric below is owned by the manager process (the one that serves `/metrics`); the **Source** column says
where its number is read. `_total` marks counters only. The `Meaning` text is the `# HELP` line.

### From the history ledger (outlives purge)

| Name | Type | Labels | Source | Meaning | Resets when the manager restarts |
| --- | --- | --- | --- | --- | --- |
| `factory_runs_finished_total` | counter | repo, outcome | ledger | Runs the history ledger shows as finished, by the least successful outcome of their jobs. | no: rebuilt from the ledger, never lower inside a process |
| `factory_jobs_finished_total` | counter | repo, outcome | ledger | Jobs the history ledger shows as finished, by outcome. | no: rebuilt from the ledger, never lower inside a process |
| `factory_job_reason_codes_total` | counter | repo, reason | ledger | Jobs the history ledger shows with a verification reason code (reason_codes.ORDER). | no: rebuilt from the ledger, never lower inside a process |
| `factory_runs_purged_total` | counter | repo, purge_reason | ledger | Runs the janitor or an operator purged, from the history ledger. | no: rebuilt from the ledger, never lower inside a process |
| `factory_cost_usd_total` | counter | repo | ledger | Client-side estimate of the USD spent by the runs in the history ledger (planner and jobs). | no: rebuilt from the ledger, never lower inside a process |
| `factory_input_tokens_total` | counter | repo, phase | ledger | Uncached input tokens of the sessions in the history ledger, by phase (token counts, not credentials). | no: rebuilt from the ledger, never lower inside a process |
| `factory_output_tokens_total` | counter | repo, phase | ledger | Output tokens of the sessions in the history ledger, by phase. | no: rebuilt from the ledger, never lower inside a process |
| `factory_cache_creation_input_tokens_total` | counter | repo, phase | ledger | Input tokens written to the prompt cache by the sessions in the history ledger, by phase. | no: rebuilt from the ledger, never lower inside a process |
| `factory_cache_read_input_tokens_total` | counter | repo, phase | ledger | Input tokens read from the prompt cache by the sessions in the history ledger, by phase. | no: rebuilt from the ledger, never lower inside a process |
| `factory_plan_to_pr_seconds` | histogram | repo | ledger | Seconds from a run being planned to the first pull request of one of its jobs. | no: rebuilt from the ledger, never lower inside a process |
| `factory_phase_seconds` | histogram | repo, phase | ledger | Seconds a job spent in one phase, from the history ledger. | no: rebuilt from the ledger, never lower inside a process |
| `factory_history_bytes` | gauge | - | ledger | Bytes of history ledger segments on disk. | recomputed on every refresh (30 s) |
| `factory_history_truncated` | gauge | - | ledger | 1 when the last read of the history ledger left older events unread, so ledger totals cover only the newest part. | recomputed on every refresh (30 s) |

### Over the run directories that still exist (gauges)

| Name | Type | Labels | Source | Meaning | Resets when the manager restarts |
| --- | --- | --- | --- | --- | --- |
| `factory_jobs` | gauge | repo, status, phase | runs | Jobs in the retained runs by raw status and phase. | recomputed every 30 s; falls when a run is purged |
| `factory_retained_cost_usd` | gauge | repo, phase | runs | USD estimate spent by the jobs of the retained runs, by phase. | recomputed every 30 s; falls when a run is purged |
| `factory_run_budget_usd` | gauge | repo | runs | The configured run_budget_usd of a repository (absent when it has none). | recomputed every 30 s; falls when a run is purged |
| `factory_run_spent_usd_max` | gauge | repo | runs | The most USD spent by any retained run of a repository (compare with factory_run_budget_usd). | recomputed every 30 s; falls when a run is purged |
| `factory_phase_peak_rss_mib` | gauge | repo, phase | runs | Peak resident MiB sampled for one session of a phase, over the retained runs. | recomputed every 30 s; falls when a run is purged |
| `factory_phase_p95_rss_mib` | gauge | repo, phase | runs | 95th percentile of the peak resident MiB of the sessions of a phase, over the retained runs. | recomputed every 30 s; falls when a run is purged |
| `factory_retained_runs` | gauge | repo | runs | Run directories of a repository that were read for the run gauges. | recomputed every 30 s; falls when a run is purged |
| `factory_retained_runs_truncated` | gauge | - | runs | 1 when a cap left some run directories unread, so the run gauges cover only part of them. | recomputed every 30 s; falls when a run is purged |

### The manager process

| Name | Type | Labels | Source | Meaning | Resets when the manager restarts |
| --- | --- | --- | --- | --- | --- |
| `factory_manager_requests_total` | counter | route, method, status_class | live | Requests to the manager API by route, method and status class. | yes: starts at 0 when the manager restarts |
| `factory_manager_request_seconds` | histogram | route | live | Seconds the manager API took to answer, by route. | yes: starts empty when the manager restarts |
| `factory_manager_auth_failures_total` | counter | http_status | live | Requests the manager API answered with 401 or 403. | yes: starts at 0 when the manager restarts |
| `factory_manager_jwks_refresh_failures_total` | counter | - | live | Failed refreshes of the Entra signing keys (entra-jwt mode only). | yes: starts at 0 when the manager restarts |
| `factory_manager_children` | gauge | kind | live | Plan, run and append processes the manager started that are alive. | recomputed on every scrape |
| `factory_manager_commands_running` | gauge | - | live | Short CLI commands the manager is running right now. | recomputed on every scrape |
| `factory_trigger_paused` | gauge | - | live | 1 while the label watcher is paused, else 0. | recomputed on every scrape |
| `factory_github_app_state` | gauge | repo, state | live | 1 for the current GitHub App check state of a repository, 0 for the others. | recomputed on every scrape |
| `factory_false_incomplete_state` | gauge | repo, state | live | 1 for the current state of a repository's false-incomplete summary, 0 for the others. | recomputed on every scrape |
| `factory_false_incomplete_prs` | gauge | repo, count | live | Pull requests in the 30-day false-incomplete window by kind (a window count, so a gauge). | recomputed on every scrape |
| `factory_false_incomplete_reason_prs` | gauge | repo, reason | live | Counted pull requests of the false-incomplete window by verification reason code. | recomputed on every scrape |
| `factory_false_incomplete_rate` | gauge | repo | live | Share of held-back pull requests merged unchanged (absent while the sample is too small). | recomputed on every scrape |
| `factory_capacity_workers` | gauge | part | live | The global worker budget: total slots, in use and waiting. **In-process workers only**: with `execution = "pod"` the workers are pods and this stays near zero. | recomputed on every scrape |
| `factory_worker_pods` | gauge | part | live | Worker pods of the pod backend: `live` (not over), `final` (over, not yet swept) and `ceiling` (`FACTORY_MAX_POD_SESSIONS`, the sessions at once). Absent with the backend off and while the census cannot be read. | recomputed on every scrape |
| `factory_data_disk_bytes` | gauge | part | live | Bytes of the volume that holds FACTORY_DATA: total, used and free. | recomputed on every scrape |
| `factory_watcher_passes_total` | counter | - | live | Label watcher passes that looked at the repositories. | yes: starts at 0 when the manager restarts |
| `factory_watcher_paused_passes_total` | counter | - | live | Label watcher passes skipped because the watcher is paused. | yes: starts at 0 when the manager restarts |
| `factory_watcher_skips_total` | counter | repo, skip_reason | live | Times the label watcher left a repository or an issue alone, by closed reason. | yes: starts at 0 when the manager restarts |
| `factory_watcher_failures_total` | counter | repo | live | Label watcher passes over a repository that failed and started a backoff. | yes: starts at 0 when the manager restarts |
| `factory_watcher_backoff_seconds` | gauge | repo | live | Seconds left of the label watcher's backoff for a repository (0 when it is not backing off). | recomputed on every scrape |
| `factory_budget_drains_total` | counter | repo | live | Runs the cost guardrail drained because they reached run_budget_usd. | yes: starts at 0 when the manager restarts |
| `factory_api_errors_total` | counter | repo, code | live | Failed sessions whose API error the classifier recognised, by closed code (a spend limit or a rate limit, say). | yes: starts at 0 when the manager restarts |
| `factory_plan_failures_total` | counter | repo, plan_failure_code | live | Plans that failed (an append that failed counts as other), by closed plan failure code. | yes: starts at 0 when the manager restarts |
| `factory_stop_world_trips_total` | counter | code | live | Times the stop-the-world breaker paused the trigger and drained the runs, by closed code. | yes: starts at 0 when the manager restarts |
| `factory_log_archive_failures_total` | counter | - | live | Purged runs whose logs could not be copied to the retained location first, plus failed daily sweeps of it. | yes: starts at 0 when the manager restarts |
| `factory_metrics_scrape_errors_total` | counter | - | live | Collectors that failed during a scrape (the scrape still answers). | yes: starts at 0 when the manager restarts |

Histogram buckets: `factory_manager_request_seconds` 0.005 to 30 s, `factory_plan_to_pr_seconds` 1 minute to 3 days, `factory_phase_seconds` 30 seconds to 4 hours.
A histogram series is rendered as `_bucket{le=...}` (cumulative, with `+Inf`), `_sum` and `_count`.

## Labels are bounded by design

A metric may declare only the label names below, and every value is checked when a number is recorded: a value outside its closed set is counted as
`other` and never becomes a new series; a `repo` that is not a configured repository is dropped. A metric has at most 2000 series. Not one label can
carry an issue or pull request title or body, a comment, a branch name, a job id (job ids are derived from issue text), a user login, a run name, a
path, an error message or a credential: no such label name exists, and a catalogue that declares one does not build. `tests/test_metrics.py` enumerates every
label of every metric, fails on a name that is not allowed or has no closed set, and checks a full scrape (with hostile issue text, a token-shaped string and a 64 character job id planted in
every source) for anything outside the closed sets.

| Label | Allowed values |
| --- | --- |
| `code` | spend_limit, rate_limit, overloaded, auth, rate_limit_unknown, none, isolation_failed, other |
| `count` | counted, merged_unchanged, merged_changed, closed_unmerged, unresolved, workflow_hold, uncoded, other |
| `http_status` | 401, 403, other |
| `kind` | plan, run, append, other |
| `method` | GET, POST, other |
| `outcome` | merged, closed, blocked, abandoned, stopped, other |
| `part` | total, in_use, waiting, used, free, live, final, ceiling, other |
| `phase` | plan, build, review, verify, preflight, none, other |
| `plan_failure_code` | invalid_plan_paths, schema_invalid, planner_error, snapshot_failed, timeout, other |
| `purge_reason` | terminal, abandoned, manual, other |
| `reason` | real_failure, ci_failed, ci_pending, workflow_hold, env_noise, unverified_other, other |
| `repo` | the configured repositories (`repos/<org>__<name>.toml`), nothing else |
| `route` | healthz, repos, trigger-pause, spend-breaker, ui, dashboard, history-runs, history-jobs, job-detail, run-logs, run-log, job-logs, job-log, plan, run, status, drain, pause, resume, retry, stop, preflight, record-capabilities, record-blocker, import-evidence, purge, unknown, other |
| `skip_reason` | disabled, backoff, busy, ignored, in-flight, unauthorised, config, archived, unremoved, draining, run_full, budget, slots_full, paused, epic_cap, planner_spend_cap, other |
| `state` | ok, ambient, checking, permission_missing, unavailable, stale, other |
| `status` | interrupted, queued, pr_ready, running, pr_incomplete, failed, blocked, needs_human_publish, stopped, merged, closed, not_started, other |
| `status_class` | 2xx, 3xx, 4xx, 5xx, other |

`reason` is exactly `reason_codes.ORDER` as it is on main, plus `other`. The issue's inventory names an `operator_item` code: it does not exist
(operator criteria never make a pull request incomplete), and `workflow_hold`, which does, is included. `status` is `operations.JOB_STATUSES`,
`phase` is `phases.PHASES` plus `none` (a job in no phase), `outcome` and `purge_reason` are the ledger's own vocabularies.
`code` is `apierror.CODES` (`docs/capacity.md`, "Stop-the-world breaker") plus `isolation_failed`, which only a breaker trip can say; it is a label of its own, so
the verification `reason` set is never reused for it. `factory_api_errors_total` counts a failed session once, from the `api_error` the job records (`none` is never counted: it
means the failure was no API error), and only the codes `spend_limit`, `auth` and `rate_limit_unknown` can lead to a trip. `plan_failure_code` is exactly `plan_failure.CODES` (`src/factory/plan_failure.py`), the code a failed plan leaves in its run directory (a missing or unknown file reads as `other`); an append that fails counts as `other`. It is a label of its own, so `code` keeps to `stopworld.KINDS`. `skip_reason` is the closed set of the skips emitted through the watcher's `_skip` (`docs/manager.md`, "Label-triggered builds"); the lines the watcher writes in other ways (`dependency`, `over-budget`, `resumed`, a follow-up `config`) are not counted in this slice, and `paused` passes have their own counter. Free text such as the GitHub health tail
(`permission missing: actions`) is reduced to a closed state before it is a label value.

## Threat model

* **What it shows.** Numbers and closed words, plus the configured repository names, which an operator already knows. A metric is not a side
  channel for private repository content: no text from an issue, a pull request, a comment, a log or the model is ever read into a label or a value.
* **Who can read it.** Whoever holds the bearer token and can open a connection to the port. There is no Entra check on purpose (a scraper has no Entra token),
  so the token is the control: without `FACTORY_MANAGER_METRICS_TOKEN` the listener only starts on the loopback of the pod, which only a process in the pod (a sidecar,
  `kubectl exec`) reaches, and on any other address it refuses to start. The token is a shared secret in Key Vault, read by the manager and by Prometheus only; a pod
  that reaches the Service without it gets `401`. A cluster on the default data plane enforces no `NetworkPolicy`, so a `NetworkPolicy` that limits the callers to Prometheus is
  prepared in `infra/` and **off** (it would not be enforced); it becomes the second layer once the Cilium data plane runs (issue #144). Do not put the token in an argument, a log or a
  label, and rotate it as a new Key Vault version (the manager and Prometheus roll together).
* **What it cannot do.** It answers `GET /metrics` only; it has no way to start a process, write a file, call GitHub or change state. It sends
  no secret: no credential, key or header value is stored in the registry. The token counters hold counts of tokens a model read or wrote (numbers,
  per `repo` and `phase`), which are not secrets.
* **Cost and abuse.** At most 8 scrapes run at once (a ninth gets `503`), a scrape reads at most 8 MiB of ledger and 200 `state.json` files and
  caches the aggregates for 30 seconds, and the listener's handler threads are daemon threads, so a stuck scraper never delays shutdown.

## Not yet exposed

These items of issue #48 are **not** in this slice. Several need a channel between the CLI processes (the supervisor, `plan`, `append`) and the
manager, which have no way to report to it today; the rest needs the infrastructure phase.

* Lease acquire, renew and loss, and the fencing token (born in the CLI processes).
* GitHub API calls and errors by endpoint class, rate-limit remaining, token mint latency and failures, label write failures and App permission
  error counts (only the current App check state per repository, `factory_github_app_state`, is exposed).
* Run directory sizes, restarts and recoveries as counters, planner cost apart from the run total, USD per phase from
  the ledger (it holds a run total; tokens per phase **are** exposed, `factory_input_tokens_total` and its three siblings), the 5 minute / 1 hour split of cache
  writes as a metric (it is in `status --json` and the ledger), the cache hit rate as a ready-made series (compute it from the counters), queue depth and the waiting reason per job, active workers by role, retry requests and refusals, plan
  failures apart from the watcher's backoff, and pull request outcome counters apart from the ledger's job outcomes.
* Jobs and issues started, gate states, JWKS refreshes (only the failures are exposed), watcher label-removal failures as a count, and the waiting reasons `dependency` and `over-budget` that the watcher reports through its lines.
* Pod CPU and memory against limits are the platform's container metrics: the kubelet's cAdvisor series are scraped (`kubelet-cadvisor`), node-exporter is not run.
* The NAP side of the cluster is **partly** exposed (issue #149, `monitoring_enabled`): pending worker pods, the node count per NodePool, creation-to-Ready time per pod, NodeClaim condition times (opt-in) and kubelet evictions come from kube-state-metrics, see "NAP-side series (kube-state-metrics, issue #149)". Not exposed, and why: time to node as a distribution, Karpenter's evictions and disruption reasons, `DisruptionBlocked`, and a per-session footprint that outlives a purge (same section, "Not available, and why").
* The deployment is written (`infra/README.md`, "Monitoring": plain manifests, not the `kube-prometheus-stack` chart, with digest-pinned images, dashboards and alert
  rules as files, Grafana with Entra sign-in, a bearer token and a prepared `NetworkPolicy`) and **no check of it on a live cluster is recorded here** (`infra/README.md` mentions a first turn-on on 2026-11-09, with its Check step still marked UNVERIFIED). Still open: the measured node
  cost of the stack and the node-sizing decision (node-exporter is not run: the kubelet's cAdvisor series give pod memory and CPU against limits; kube-state-metrics is run since #149, one more small pod), the live check that Prometheus has the series
  and Grafana renders the Overview dashboard, and the network policy once the cluster has an engine.

### NAP-side series (kube-state-metrics, issue #149)

Issue #149, a follow-up of #83 and #121 and part of #48. The design is **self-hosted kube-state-metrics only**: it adds no Azure resource and gives the series that
#121 read by hand. What it can give and what it cannot is stated below, series by series. **Nothing here has run on a cluster**: "measured" means measured by hand in #121 on 2026-11-08 on a throwaway cluster that
was deleted afterwards; "documented" means read in a source listed at the end of this section on 2026-11-09; **UNVERIFIED** means neither (the tests of `infra/test/test_monitoring_design.py`
pin the files, not the behaviour of the pod). The section is not part of the catalogue: no row of it starts with a `factory_` name (`tests/test_metrics.py` and `infra/test/test_monitoring_design.py` find the catalogue
tables by that pattern); the recorded series use a colon (`factory:...`) for the same reason and because that is the naming of Prometheus recording rules.

**What changes when monitoring is on.** Everything is behind `monitoring_enabled` (default `false`), so a default plan is unchanged and production behaves as before. With it on, **one more small pod appears**
next to Prometheus and Grafana: `kube-state-metrics` in the namespace `monitoring` (one replica, requests 10m CPU and 64Mi, limits 200m and 256Mi, on the same node as the other monitoring pods: the system node, or the pool `mon`
of `monitoring_node_pool_vm_size`). Prometheus gets one more scrape job and its pod rolls once (its configuration checksum changes); the Factory pod and the Caddyfile do not change. The pod has a read-only service account
(`list` and `watch` on `pods` and `nodes`), no secret, no Azure identity and no route in the ingress. The optional NodeClaim part (`monitoring_nodeclaim_metrics_enabled`, default `false`, needs `nap_mode = "Auto"`) adds a
custom resource state file, a second read-only ClusterRole (`list` and `watch` on `nodeclaims` of `karpenter.sh` and on `customresourcedefinitions`) and two arguments; a cluster without node auto-provisioning runs
the base pod and never touches the NodeClaim definition. Its image, `registry.k8s.io/kube-state-metrics/kube-state-metrics`, is pinned to a version tag; its digest was **not** read (no network when it was written) and
is an open step (`infra/README.md`, "Images").

**What is deployed** (`infra/monitoring.tf`, `infra/monitoring/`):

* kube-state-metrics with `--resources=pods,nodes` and `--metric-labels-allowlist=nodes=[karpenter.sh/nodepool]` (the label that names the NodePool of a node on NAP, documented), scraped by the Prometheus job `kube-state-metrics`
  (`prometheus.yml.tftpl`) over its ClusterIP Service. The job keeps only the series below, and the pod series only for the worker namespace (`factory-workers`, `worker_contract` in `infra/locals.tf`): a pod name is a per-object label,
  so pods of other namespaces would add churn for nothing. A test fails when the kept list and the names used by the rules and the dashboard differ in either direction.
* Recording and alert rules in `infra/monitoring/alert-rules.json` (the groups `factory-nap-recording` and `factory-nap`) and the dashboard `factory-nap.json` ("Factory node auto-provisioning"), without the repository variable (none of its series has a repository).

**Implemented series.** Upstream names are never renamed. "Stable" and "EXPERIMENTAL" are the levels of the kube-state-metrics references read on 2026-11-09 (the pinned version `v2.15.0` was not run; whether the series and levels are the same there is UNVERIFIED).

| Question | Series (type) | Labels kept | Source | What it can and cannot say |
| --- | --- | --- | --- | --- |
| Pending worker pods | `factory:worker_pods_pending` (recorded gauge) from `kube_pod_status_phase{phase="Pending"}` (gauge, stable); `factory:worker_pods_unschedulable` from `kube_pod_status_unschedulable` (gauge, stable) | none (summed); raw: `namespace`, `pod`, `uid`, `phase` | kube-state-metrics, pods | Zero until the pod backend (#145) creates worker pods. Alert `FactoryWorkerPodPendingTooLong`: a worker pod Pending for more than 10 minutes (a cold start measured 94 to 101 s). It says a pod waits, not why: the pod's events say why |
| Time to node (pod created to `Ready`) | `factory:worker_pod_time_to_ready_seconds` (recorded gauge, one per pod) = `kube_pod_status_ready_time` (gauge, **EXPERIMENTAL**) minus `kube_pod_created` (gauge, stable), for pods whose `kube_pod_status_ready{condition="true"}` is 1 | `namespace`, `pod`, `uid` (aggregated away in the dashboard and the alert) | kube-state-metrics, pods | A per-pod gauge, **not a histogram**: kube-state-metrics gives no distribution, and a percentile taken over these samples would weight each pod by how long it lives. The dashboard shows the slowest and the average pod now present; alert `FactoryWorkerPodSlowToReady` fires above 300 s for a pod that became Ready in the last hour. It is the time of the **last** transition to Ready, so a pod whose readiness flapped shows too short a time. The creation-to-Ready time contains the node's cold start, the image pull and the startup taints, and it cannot tell them apart; the NodeClaim conditions below split the node part |
| NAP node count | `factory:nap_nodes` (recorded gauge) and `factory:nap_nodes_total` from `kube_node_labels` (gauge, stable) counted by its label `label_karpenter_sh_nodepool` | `nodepool` (NodePool names are operator-defined: a handful, not issue text) | kube-state-metrics, nodes | Needs the label allow-list above; whether the label is present on the nodes of the live cluster is documented, not checked. Alert `FactoryNapNodesIdle`: a NAP node for 30 minutes while no worker pod runs (consolidation held back) |
| NodeClaim conditions | `kube_customresource_nodeclaim_created_timestamp_seconds` and `kube_customresource_nodeclaim_condition_last_transition_timestamp_seconds` (gauges, Unix seconds, custom resource state); recorded `factory:nodeclaim_condition_seconds` = condition time minus creation time for `Launched`, `Registered` and `Initialized` | `nodepool`, `type`, `status`; `name` (generated by Karpenter, no issue text) is aggregated away | kube-state-metrics, custom resources (`infra/monitoring/kube-state-metrics-nodeclaim.yml.tftpl`) | Only with `monitoring_nodeclaim_metrics_enabled`. **UNVERIFIED:** the API version NAP serves for NodeClaim (`v1` is configured; the NodePool of `infra/worker_pool.tf` is `karpenter.sh/v1`; check `kubectl get crd nodeclaims.karpenter.sh -o jsonpath='{.spec.versions[*].name}'`), that this kube-state-metrics turns an RFC 3339 `lastTransitionTime` into Unix seconds, the exact metric names the default prefix gives, and how it behaves when the definition is missing. Its documentation marks custom resource state feature-frozen in favour of resource-state-metrics: a maintenance risk. Alert `FactoryNodeClaimNotInitialized`: a claim older than 15 minutes that is not Initialized |
| Evictions (by the kubelet only) | `kube_pod_status_reason{reason="Evicted"}` (gauge, stable); alert `FactoryWorkerPodEvicted` | `namespace`, `pod`, `uid`, `reason` | kube-state-metrics, pods | Pods the **kubelet** evicted under node pressure and left behind as `Evicted`. It does **not** see a pod that node auto-provisioning drained: that pod is deleted through the Eviction API and leaves no object. See "Not available" |
| The source itself | `up{job="kube-state-metrics"}`; alert `FactoryKubeStateMetricsNotScraped` | `job`, `instance` | Prometheus | Its own telemetry port (`kube_state_metrics_list_total` and the watch counters, which would show a failed list of the NodeClaim definition) is exposed by the container and **not scraped**: add it if the live check shows silent failures |

**Per-session footprint (checked: the existing series cover the memory part).** `factory_phase_peak_rss_mib` and `factory_phase_p95_rss_mib` (catalogue above) are exactly the per-session footprint the issue asks about: peak
and 95th percentile resident MiB of the sessions of a phase, per `repo` and `phase`, from the `memwatch` samples of the retained runs. Nothing new is added for them. Two limits stay and are not closed here: both fall when a
run is purged (they are recomputed from the run directories), and CPU seconds per session (`status` computes them in `footprint_summary`) are not exposed at all. A ledger-backed footprint that outlives `purge` needs a ledger field
and a change to `metrics_collect.py`: a separate code issue, listed below with the other proposals. With worker pods, cAdvisor's `container_memory_working_set_bytes` and `container_cpu_usage_seconds_total` would add the
pod's own view, but the `kubelet-cadvisor` job keeps only the namespaces of `kept_namespaces` (the manager's, the ingress's and `monitoring`), not `factory-workers`: adding it belongs to #145.

#### Not available, and why

The rows below are **proposals, not implemented**: nothing in the registry, the catalogue, `infra/` or the alert rules, and no code ships with them. A proposed name that starts with `factory_` is not in the catalogue
tables of this page.

| Question | Proposed series (type) | Labels kept | Where the value would come from | What it needs |
| --- | --- | --- | --- | --- |
| Time to node as a distribution | `factory_session_ready_seconds` (histogram, buckets from measurement). Managed: `karpenter_pods_startup_duration_seconds` (summary, stable) | `repo`, `phase` (this page's closed sets) | The ledger, if the supervisor records pod creation and `Ready` per session (read like `factory_phase_seconds`). Karpenter's series measure "the time from pod creation until the pod is running", so not `Ready`; that the two are equal is UNVERIFIED | The per-session pod backend (#145), which does not exist: today the only pod is the manager. A ledger field. Buckets from more than #121's three cold starts. kube-state-metrics cannot give a histogram |
| Evictions and disruption reasons | Platform (managed only): `karpenter_pods_eviction_requests_total` (counter, alpha, `code`), `karpenter_pods_drained_total` (counter, alpha, `reason`), `karpenter_nodeclaims_disrupted_total` (counter, alpha, minimal profile), `karpenter_voluntary_disruption_decisions_total` (counter, stable, minimal profile), `karpenter_voluntary_disruption_eligible_nodes` (gauge, beta, minimal profile), `karpenter_nodepools_allowed_disruptions` (gauge, stable). Factory-owned: `factory_session_interruptions_total` (counter) | Karpenter: `reason`, `decision`, `consolidation_type`, `nodepool` (closed sets in its reference). Factory: `repo`, `phase` and a new label name `interruption_cause` with its own closed set (for example `node_disruption`, `out_of_memory`, `other`); not `reason`, which is the verification reason codes (see **Labels**) | Karpenter's metrics: the control-plane target `node-auto-provisioning` of managed Prometheus (the only documented route). The Factory counter: the supervisor marking a session `interrupted`, through the ledger | Managed path: the managed Prometheus add-on and an Azure Monitor workspace. Factory path: #145 and #147. The Factory side sees only that a session was lost (in #121 the process saw only `SIGTERM`), not why a node was disrupted: only Karpenter knows the reason. **Decision:** neither is built; kube-state-metrics sees only kubelet evictions (above) |
| `DisruptionBlocked` counts and reasons | `factory_nap_disruption_blocked_total` (counter), only if the live check below finds a readable event stream | `blocked_reason`: a new label name with its own closed set (for example `do_not_disrupt`, `budget`, `pdb`, `other`) mapped from the event message; the message itself is never a label. Not `reason` (see **Labels**) | It is a Kubernetes **event**, not a metric (in #121: reason `DisruptionBlocked`, message `Pod has "karpenter.sh/do-not-disrupt" annotation`, a pod held for 6 minutes). No Karpenter metric of that meaning is in the reference read, and kube-state-metrics does not list events among its resources (whether it can be enabled: UNVERIFIED, and it is not turned on here). Documented stores: the control-plane log (`AKSControlPlane`, category `karpenter-events`) and `kubectl get events --field-selector source=karpenter-events` | A control-plane diagnostic setting with the `node-auto-provisioning` category (not declared in `infra/`), or `KubeEvents` through Container insights (declared; whether Karpenter's events reach it: UNVERIFIED). The messages are free text and untrusted: only a fixed mapping to the closed set may reach a series. **Decision:** it stays a log or event query until the live check shows the event reaches a store |
| Footprint that outlives a purge, and CPU seconds per session | `factory_session_peak_rss_mib` and `factory_session_cpu_seconds` (histograms) | `repo`, `phase` | `usage[*].footprint` (`memwatch`: `peak_rss_mb`, `cpu_seconds`), written to the ledger as costs are | A ledger field and its read in `metrics_collect.py` (a code change, separate from #149), buckets from the #71 records of many real sessions (not measured) |

**Where Karpenter's events and metrics go (the question of #149).** As documented in the sources below (read on 2026-11-09, not confirmed on a cluster): Karpenter runs in the **AKS-managed control plane**, so no Prometheus
in the cluster can scrape it; its **metrics** are documented to land in an **Azure Monitor workspace** through the managed Prometheus add-on (the target `node-auto-provisioning`, off by default), **not in Log Analytics**; its **events**
are in the control-plane log (`AKSControlPlane`, category `karpenter-events`, a Log Analytics table, once a diagnostic setting with that category exists) and as Kubernetes events. Nothing in `infra/` enables any of those paths
(the only diagnostic setting is the Key Vault's), and the Container insights streams that are declared (`ContainerLogV2`, `KubeEvents`, `KubePodInventory`) are not shown to carry Karpenter's events. So today neither Karpenter's metrics nor
its events reach any store, and the self-hosted stack above works without them.

**Checked on a cluster (a throwaway cluster, 2026-11-10).** (1) **No Karpenter pod, Service or metrics endpoint exists in the cluster** (`kubectl get pods,svc -A` has none, only the `nodepools`, `nodeclaims`, `aksnodeclasses` and `nodeoverlays` custom resource definitions), so nothing can scrape Karpenter: the NodeClaim series of this change are the only in-cluster source. (2) **The control-plane events do arrive** once a diagnostic setting with the category `karpenter-events` exists (the category is offered on a cluster with node auto-provisioning; the mirror got one by hand): 396 records in 35 minutes of deliberate churn, in `AzureDiagnostics` (not `AKSControlPlane`, because the setting used the default destination), **at most about 7 minutes after the event**. They are Karpenter's controller log lines (JSON in `log_s`): `created nodeclaim`, `registered nodeclaim`, `initialized nodeclaim`, `deleted node`, `could not schedule pod`, `drift triggered as nodeClassHash != nodeClaimHash`, `marking consolidatable`. (3) **The Kubernetes events carry the disruption reasons**, which is the readable event stream the `factory_nap_disruption_blocked_total` row waited for: `DisruptionBlocked` with the messages `Pod has "karpenter.sh/do-not-disrupt" annotation`, `Pdb prevents pod evictions`, `Node is nominated for a pending pod`, `Node is deleting or marked for deletion` and `Node isn't initialized`, and `FailedDraining` (`1 pods are waiting to be evicted`); the pod at the pool's limit got `all available instance types exceed limits for nodepool`. Kube-state-metrics does not expose events: counting them needs an event exporter or the Log Analytics query, which is not added here. Terraform now defines it (`azurerm_monitor_diagnostic_setting.karpenter_events` in `infra/observability.tf`: only the category `karpenter-events`, to the existing workspace, only with `nap_mode = "Auto"`); the plan identity's `Reader` role on the resource group covers reading it. Counting `DisruptionBlocked` events as a Prometheus series would need an event exporter, which is not added: the Log Analytics query is the source for them.

**Labels.** The policy above applies unchanged to every series with a `factory_` name: closed sets only, new values added to `LABEL_VALUES` together with the series, and a test that enumerates them. One closed set exists per label name, so a new vocabulary gets a new label name, as `skip_reason` and `purge_reason` did: the two Factory series proposed above would add `interruption_cause` and `blocked_reason` to `LABEL_NAMES` and `LABEL_VALUES` with their series (and a row each in the label table under "Labels are bounded by design"). They must not reuse `reason`, which is exactly `reason_codes.ORDER` plus `other` (the test pins it), and whose values would otherwise become valid for `factory_job_reason_codes_total`. The platform series carry
per-object labels (`pod`, `uid`, `name`, `node`, `owner`): none of them may be put in a `factory_` series, a panel legend or an alert annotation, and queries aggregate them away (`sum without (pod, uid)`). The one object label that may stay is `nodepool`.
Be clear about what this does not give: the implemented per-pod series (`kube_pod_*`, and the recorded `factory:worker_pod_time_to_ready_seconds`, which keeps `pod` and `uid`) are bounded by no closed set, and not by the worker budget or the NodePool `limits` either: those cap the pods running at one time, but the samples of a deleted pod stay until `prometheus_retention` (default `15d`) or the size limit (`--storage.tsdb.retention.size`, 80 percent of `prometheus_storage_gib`, which drops the oldest samples; `infra/variables.tf`, `infra/monitoring.tf`) removes them. The number of distinct `pod` and `uid` series is therefore the number of worker pods created in that window (churn), and the effective cap is that retention and size limit. That is a
deviation from this page's rule, acceptable only for platform series and only if the pod names carry no issue text (the name scheme belongs to #145; a name derived from a job id would break it). The strict alternative is the Factory-owned histograms, which need #145. Self-hosted kube-state-metrics accepts the deviation: the pod series are kept for the worker namespace only (the scrape job), and the dashboard and the alerts aggregate `pod` and `uid` away.

**Reference values from #121** (one cluster, a few runs each, for choosing buckets and thresholds, not a distribution): cold start 94 s, 93 s and 101 s (about 100 s with the Factory image); one run split as NodeClaim created +1 s, `Launched` +7, `Registered` +74,
node `Ready` +76, `Initialized` +79, pod scheduled +91 (after about 12 s of startup taints), container started +94; node teardown `consolidateAfter` plus 90 to 115 s; a drifted node held 6 minutes by `karpenter.sh/do-not-disrupt`; PDB behaviour not tested.

#### Which source: self-hosted Prometheus or Azure Monitor

The comparison that led to the decision (self-hosted kube-state-metrics only). The first column is what this change deploys; the other two are not built.

| | In-cluster Prometheus with kube-state-metrics (deployed, off with monitoring) | Azure Monitor managed Prometheus (control plane metrics) | Log Analytics (Container insights, control-plane logs) |
| --- | --- | --- | --- |
| Reaches | Pods, nodes (with the NodePool label), NodeClaim custom resources: pending pods, node count, time to node, NodeClaim condition times | Karpenter's own series, including evictions and disruption decisions with reasons. The NAP target is **off by default** (`node-auto-provisioning = false` under `controlplane-metrics`); the API server and etcd targets are on (documented) | Events and logs: the `karpenter-events` category and, perhaps, `KubeEvents`. Karpenter **metrics are documented to land in an Azure Monitor workspace, not in Log Analytics** |
| Cannot reach | Karpenter's metrics (the controller is in the managed control plane; that its endpoint is unreachable from the cluster is not stated and is UNVERIFIED) and Karpenter's events | Anything about Factory itself; the Factory series would still come from the manager | Numbers over time as Prometheus series, without a log-to-metric step |
| In `infra/` today | Deployed with `monitoring_enabled`: the Deployment, service account, read-only ClusterRoles, Service, scrape job, rules and dashboard | Nothing (no add-on, no workspace) | Container insights streams only; no control-plane log setting |
| Needs | Nothing beyond the monitoring stack of #139 | The managed Prometheus add-on, `--enable-control-plane-metrics`, an Azure Monitor workspace, managed identity (the cluster has one), an edit of `ama-metrics-settings-configmap`; and a way for the self-hosted Grafana to read the workspace (UNVERIFIED) | A diagnostic setting with the `node-auto-provisioning` category (documented) |
| Cost | No Azure meter. Node headroom: #121 measured the reference deployment's system node at 88 % of its allocatable CPU requested (about 224 mCPU left; whether monitoring was on then is not recorded); the monitoring pods request 210m with this pod (200m before), so the system node is tight and the dedicated monitoring node (`monitoring_node_pool_vm_size`, a `Standard_D2ds_v5` is $0.113/h, about $82 a month, VM only, price fetched in #121 on 2026-11-08) is the safe place. Real memory and CPU of the pod: UNVERIFIED | Billing dimensions (samples ingested and queried) are UNVERIFIED: they come from a search result, not from a page read; the rates were **not read** (UNVERIFIED) either, and the add-on's pods need node capacity too (sizes UNVERIFIED). This reverses part of the cost decision recorded in #48 | Already paid for the streams in use; the cost of more categories is UNVERIFIED |
| Network isolation | One more in-cluster scrape target with pod and node names and **no authentication** (see below) | Control plane metrics do not support Azure Private Link (documented), so the Azure-side data path cannot be made private; whether that matters for the isolation #48 asks for is the operator's call | Unchanged |

#### Network isolation

The decision #48 waits on is the isolation of the metrics port: a bearer token now, and a NetworkPolicy once the cluster has the Cilium data plane (#144). This change does **not** decide it, and it adds no NetworkPolicy:

* kube-state-metrics' port 8080 serves pod and node names (and the names of worker pods once #145 runs them) to **any pod that can reach the Service**, without a token. That is the same trust level as the Prometheus
  port in the same namespace, which also has no authentication; the manager's port keeps its bearer token. The series carry no secret and no issue text (the name scheme of worker pods belongs to #145: a name derived from a job id would
  break that). The worker pods run under relaxed pod settings, so a compromised worker could read the names of every pod and node of the cluster from it; that is information, not a credential.
* The `monitoring` namespace has no NetworkPolicy, and the only one of the stack (`factory-ingress`, the factory namespace) is prepared and **off** because the default data plane does not enforce it. A policy that limits
  kube-state-metrics' port to the Prometheus pod belongs to the same prepared switch (`factory_network_policy_enabled`) and should be added to it when Cilium runs; until then it would be accepted by the API server and not enforced.
  A policy that selects the Prometheus pod would also cut it off from its own scrape targets unless its rules are checked first: that is why none is written blind.
* The Azure-side managed route (not built) cannot use Private Link. It stays off the table until an operator accepts that.

**Open live checks** (none was run). **The control-plane `karpenter-events` log and Karpenter metrics are not shown to arrive anywhere by this section.**
1. Start kube-state-metrics under the `restricted` settings (does the image start as 65534 with a read-only root file system, do `/readyz` on 8081 and `/livez` on 8080 answer), with and without the NodeClaim config, and record its memory and CPU, its series count with a worker namespace (series per pod times pods created per day, over `prometheus_retention`, against the size limit) and the fit on the node. Check `kubectl get crd nodeclaims.karpenter.sh -o jsonpath='{.spec.versions[*].name}'` against `ksm_nodeclaim_api_version`, that `label_karpenter_sh_nodepool` is on the nodes of the NodePool, and that the two NodeClaim series exist with the names above.
2. Read the digest of the image from `registry.k8s.io` and pin it in `infra/monitoring-images.auto.tfvars`.
3. Add a diagnostic setting with the `node-auto-provisioning` category on a cluster and check that `AKSControlPlane | where Category == "karpenter-events"` returns rows during a cold start and a blocked drift. Check separately whether the same events appear in `KubeEvents`, and for how long (table retention).
4. Turn on the `node-auto-provisioning` target of managed Prometheus and list which `karpenter_*` and `operator_*` series reach the workspace, against the minimal profile and against the series named in the table; record the Karpenter version NAP runs.
5. List the distinct `DisruptionBlocked` messages seen over a week and fix the closed `blocked_reason` set from them.

**Decisions.** Taken: the source is **self-hosted kube-state-metrics only** (implemented here). Still open for the operator:
1. Network isolation (#48, #144): whether the open kube-state-metrics port is acceptable until Cilium and a policy exist (the default here), or the pod waits for it.
2. Where it runs: the system node has little CPU headroom, so with monitoring on the dedicated monitoring node pool is the safe choice and its recurring cost is the operator's call.
3. Whether Karpenter's own reasons (evictions, disruption decisions) are wanted enough to take the Azure-side managed Prometheus, which reverses part of the #48 cost decision and has no Private Link; and whether the Factory-owned counters and the ledger-backed footprint (a separate code issue, with #145 and #147) are built.

**Sources read on 2026-11-09** (`learn.microsoft.com` and `karpenter.sh` did not resolve from the session, so the pages Microsoft Learn is published from and the Karpenter reference as published in the source tree were read instead; that they match the rendered pages today is UNVERIFIED):
* AKS, enable and observe NAP (ms.date 09/29/2025): `https://raw.githubusercontent.com/MicrosoftDocs/azure-aks-docs/main/articles/aks/use-node-auto-provisioning.md`. AKS control plane metrics (ms.date 09/06/2026): `https://raw.githubusercontent.com/MicrosoftDocs/azure-aks-docs/main/articles/aks/control-plane-metrics-monitor.md`.
  AKS monitoring reference with the minimal-profile series (ms.date 09/06/2024, possibly out of date): `https://raw.githubusercontent.com/MicrosoftDocs/azure-aks-docs/main/articles/aks/monitor-aks-reference.md`.
  AKS, configure AKSNodeClass resources (ms.date 09/18/2026; `kubectl get nodeclaims`): `https://raw.githubusercontent.com/MicrosoftDocs/azure-aks-docs/main/articles/aks/node-auto-provisioning-aksnodeclass.md`.
  Not read: `control-plane-metrics-default-list.md` (the page the NAP article links for the minimal list; 404 at the same raw path).
* Karpenter metrics reference, `main` branch: `https://raw.githubusercontent.com/aws/karpenter-provider-aws/main/website/content/en/docs/reference/metrics.md`.
* kube-state-metrics, `main` branch (the deployed version is `v2.15.0`, a tag chosen without reading the registry; its digest and its behaviour are not checked): `https://raw.githubusercontent.com/kubernetes/kube-state-metrics/main/docs/metrics/workload/pod-metrics.md`, `.../docs/metrics/cluster/node-metrics.md`, `.../docs/metrics/extend/customresourcestate-metrics.md` and `.../docs/developer/cli-arguments.md`; and its `README.md` (`https://raw.githubusercontent.com/kubernetes/kube-state-metrics/main/README.md`, Usage: a service account token with read-only access).
* Web search, results only (pages not read): the Azure Monitor cost page `https://learn.microsoft.com/en-us/azure/azure-monitor/cost-usage` is listed as covering managed Prometheus billing; no price was taken from any source.
* In the repository: `infra/monitoring.tf`, `infra/monitoring/prometheus.yml.tftpl`, `infra/observability.tf`, `infra/README.md` ("Monitoring").

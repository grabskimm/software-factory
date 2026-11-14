---
name: factory
description: Operate Factory, which turns GitHub issues into Claude Code pull requests, through its CLI (plan, run, status, drain, stop, resume, retry, purge) and the factory-build and factory:* labels. Use to run a batch, read status and the factory/gate hand-off, or recover blocked or failed jobs and issues.
---

# Factory operator

For a person or agent driving the `factory` CLI (`pip install -e .`, then `factory` or `python -m factory`; `--help` and
`SUBCOMMAND --help` list every flag), not for a Factory worker session: that only implements its job and has no access to
a supervisor. Never start a second supervisor or redo work a running one owns. Factory stops at a published pull request
plus a `factory/gate` commit status (ready when review and verification passed, a draft with a failing gate otherwise); it
never merges and never closes issues. A person merges, or GitHub auto-merge does once the operator applied the hand-off in
`handoff/` (`docs/handoff.md`); `Closes #N` in the PR body closes issues. Identify the run before acting, keep the user's
issue selection and do not disturb active work to answer a status question. Report each PR with its readiness,
`handoff_status`, exact blocker and next action with its owner; name what changed before repeating a failed operation;
keep transcripts, prompts and issue bodies out of shared channels. The `docs/` and `handoff/` paths are in the Factory
repository (a copied skill does not carry them).

## Setup

Needs Python 3.11+, `git`, `gh` (logged in), the `claude` CLI, `ANTHROPIC_API_KEY` (the only secret a worker receives)
and, on Linux, `bubblewrap` and `socat` (the worker sandbox is mandatory). `--repo` and `--run-dir` are global options and
go before the subcommand.

- Local mode (no `--repo`): the current directory's checkout (an `origin` remote and a `main` branch), every config key
  keeps its default, no `FACTORY_*` variable is needed, no manager and no label watcher; the run directory is
  `factory/runs/pilot` unless `--run-dir`.
- Managed mode (`--repo ORG/NAME`): a clone in `$FACTORY_DATA/repos/ORG/NAME/` and the config
  `$FACTORY_CONFIG/repos/ORG__NAME.toml` (unknown keys are errors); `FACTORY_CONFIG` and `FACTORY_DATA` are required, with
  no defaults; `--run-dir` is required, except that `purge-clone` takes none and `plan --append-to RUN` derives it from
  RUN. Identity is your `gh` and `git` login or a GitHub App (`installation_id` in the config, `FACTORY_GITHUB_APP_ID`,
  `FACTORY_TOKEN_BROKER`, `GITHUB_APP_PRIVATE_KEY`; a half-configured App fails instead of falling back).

## Start a run

`run` pushes branches, opens real pull requests and spends real money: try a scratch repository first. Define `f` once (a
function, not a variable, so it also works in zsh) and use it for every command below:

```sh
f() { factory --repo ORG/NAME --run-dir "$FACTORY_DATA/repos/ORG/NAME/runs/RUN" "$@"; }   # managed mode
f() { factory --run-dir factory/runs/RUN "$@"; }                                          # local mode
f plan --limit 30                       # newest open issues; or: plan --issues 12,15 (exactly these, not with --limit)
f run --concurrency 2 --verification-concurrency 1
f status --json                         # add --offline to skip the GitHub lookups
```

`plan` skips issues labelled `factory-ignore` and writes `plan.json`; read it first. Every issue has exactly one
disposition (`job`, `investigation`, `covered`, `blocked` or `epic`); `likely_paths` are literal repository paths (no
globs, `.` locks the whole repository); overlapping jobs build serially and `depends_on` waits for the dependency's PR to
merge. `run --plan-file FILE` imports a hand-written plan. A started run's plan is immutable: add issues with
`f plan --issues 20 --append-to RUN` (refused for a run that has not started, is draining, has 10 jobs or is over
`run_budget_usd`), anything else needs a new run. `run` exits 2 while any job is not `pr_ready`, `merged` or `closed` (0
after a drain). One-off overrides: `--review-rounds 0-3`, `--budget-build|review|verify USD`, `--timeout SECONDS`.

`preflight JOB --phase build|verify` runs a short real Claude session (cap `--budget-preflight`, default $0.50) and
records which of `workspace_write`, `shared_git_write`, `loopback_listener` and `network_download` the sandbox allows; a
denied capability is not a code defect, so never rerun an unchanged denied operation. `record-capabilities --file FILE`
records a JSON array of such results from elsewhere (`id`, `environment`, `status` available|unavailable|unconfirmed,
`evidence`, `tested_at`).

## Read the status

`status --json` has `jobs[]` with `id`, `status`, `phase`, `cost_usd`, `handoff_status`, `reason_code`, `next_action`,
`owner`, `pr`, `error`, `blockers`, `waits`, `needs_human_publish`, `workflow_paths`, `models`, and, at run level,
`supervisor`, `cost_usd`, `active_worker_count`, `live_refresh`, `breaker`, `capacity`. Before `run` has started anything
it only prints the planned issue and job counts. Job `status` is one of `not_started`, `queued`, `running`, `interrupted`,
`failed`, `blocked`, `stopped`, `needs_human_publish`, `pr_incomplete`, `pr_ready`, `merged`, `closed`. A job that waits
for a worker slot of its phase is `waiting_build`, `waiting_review`, `waiting_verify` or `waiting_publish`: `run`
continues it, `retry` refuses it.

`live_refresh` is `{ok, failed}`: a failed live PR lookup leaves those jobs on saved, possibly stale state, so look at the
PR first. `breaker` is not null while the manager's stop-the-world breaker is tripped (a spend limit, a refused key or a
429 storm: every session fails and retrying cannot help); it never clears itself, a person fixes the cause and clears it
(`docs/operations.md`). `capacity` is the pod-wide worker budget (null when none, `docs/capacity.md`): a job queued behind
it is waiting, not failing.

`handoff_status` is `null` (no pull request yet: follow `next_action`), `published` (PR open, no failing required check:
wait, it merges once the checks and `factory/gate` pass and a person or auto-merge acts), `ci_failed` (a required check
failed, any failing check when none is required or readable, never `factory/gate`: diagnose, then `retry JOB --reason ...`
or fix by hand, a new head invalidates the gate) or `merged` / `closed` (start `run` again so dependent jobs proceed).
`--offline` shows `published` for every job with a PR not already recorded as merged or closed. A `pr_incomplete` job is a
draft with a failing gate: `reason_code` says why (`real_failure`, `ci_failed`, `ci_pending`, `workflow_hold`,
`env_noise`, `unverified_other`); `next_action` and `owner` say what to do and who does it, which is not always `resume`
or `retry`.

A `needs_human_publish` job passed review and verification, but GitHub refused its push of `.github/workflows` files (an
App without the `workflows` permission, or a token without `workflow` scope; never grant a Factory App `workflows`). A
person reads every workflow diff, pushes the branch with their own credentials from the Factory clone (never with git in
the worker-writable worktree), then `resume JOB --phase publish` opens a draft whose gate fails until they apply
`factory-verified`, mark it ready and apply `factory:ready` (`docs/operations.md`, "Jobs that change
`.github/workflows`").

## Recover

```sh
f drain                                 # finish active phases, start nothing new; the supervisor then exits (returns at once)
f stop JOB --reason "why"               # one job: its step may finish (FACTORY_STOP_GRACE_SECONDS, 120), then it is killed
f resume JOB --phase publish --reason "what changed"   # review|verify|publish from a saved checkpoint, then run
f retry JOB --reason "diagnosed cause"  # new build attempt, then run
f import-evidence JOB --file evidence.json   # verification done elsewhere; queues publish, then run
f record-blocker --file blocker.json    # one diagnosis for several jobs; job gates unchanged
f recover                               # a dead supervisor's running jobs become interrupted
f reconcile                             # idle bookkeeping of an unsupervised run: labels, pending gate, verified PRs
f block JOB --reason "why"              # a queued job that can never start
f purge                                 # finished run: worktrees, local branches, run dir; settles its issues' labels
factory --repo ORG/NAME purge-clone     # remove the managed clone once no run is left
```

`retry` is for `failed`, `blocked`, `interrupted`, `stopped`, `needs_human_publish`, `pr_incomplete` or CI-failing
`pr_ready` jobs; `resume` needs `--reason` only to repeat a phase on an unchanged head (`build` needs `retry`, which needs
it after the first attempt). `evidence.json` is
`{"job_id": JOB, "commit": <current head>, "tested_commit", "complete", "scenario", "environment", "assertions": [...]}`.
With `complete` true, `scenario`, `environment` and `assertions` must be non-empty and, when `[acceptance] mode` is
`recording`, so must an https `recording_url`; with `complete` false a `blocker` is needed. `blocker.json` is
`{"id", "affected_jobs": [...], "cause", "evidence": [...], "remedy", "owner"}`.

There is no CLI `pause`: SIGINT to the supervisor (what the manager's pause sends) kills live workers and leaves their
jobs `failed` (`agent exited -15`), so `drain` first. `drain` and `stop` need a live supervisor; `retry`, `resume`,
`recover` and `purge` refuse while a supervisor or worker may be alive, so wait until `supervisor.active` in
`status --json` is false. After a restart, interrupted jobs need an explicit `retry` or `resume` (unless
`auto_resume_after_restart = "once"`).

## Labels and the manager

`factory-manager` (`docs/manager.md`) is an optional HTTP front of the same CLI plus a label watcher; the CLI alone never
acts on `factory-build`. The config keys and labels below are described in `docs/configuration.md`.

- `factory-build`: a person with `write` access labels an open issue; the watcher plans exactly that issue, removes the
  label, builds it and opens the PR. Re-apply it to ask for another attempt on a `factory:blocked`, `factory:ci-failed`,
  `factory:needs-info` or `factory:awaiting-approval` issue. `auto_build = false` or `FACTORY_MANAGER_TRIGGER_SECONDS=0`
  turns the watcher off. `factory-ignore` opts an issue out (read at `plan` and again before the build; if it appears
  while building, the PR says `Refs #N`).
- `factory:*`: one status label per issue (`planned`, `building`, `in-review`, `pr-open`, `ci-failed`, `merged`, `blocked`
  = needs a human; epics: `needs-info`, `awaiting-approval`, `plan-approved`). Every command that changes a job, and
  `plan`, `reconcile` and `purge`, writes the issue labels; `status`, `preflight`, `record-blocker` and
  `record-capabilities` write none, and `drain` and `stop` only ask the supervisor (which does). An issue that needs a
  human also gets one outcome comment (`comment_on_blocked = false` or `FACTORY_ISSUE_COMMENTS=off` silences it).
- `factory-model:<alias or id>` and `factory-effort:<low|medium|high|xhigh|max>`: at most one of each, for build and
  verify only, honoured only when applied by someone holding `auto_build_min_permission`. An invalid one blocks the issue
  (no fallback); `allowed_models` and `max_effort` bound them.
- Epic mode (on by default; `epic_mode = false` or `FACTORY_EPIC_MODE=off`): a too-large but splittable issue gets one
  phase-plan comment and waits for an answer, then `factory-approve-plan`; phases open one at a time. Observed issues
  (`observed_issues`, on by default): Factory opens an issue labelled `factory:observed` for what a worker noticed, never
  with `factory-build`; a person adopts it by labelling it.
- Follow-ups (`followup = true`, off by default): `@<app slug or mention_handle> what to change` on an open Factory PR
  runs a session on its branch (the manager uses `retry JOB --followup` and `followup-release JOB`). Also
  `pr_style = "factory" | "conventional"` (Conventional Commits titles for squash-merge release tooling) and
  `execution = "inprocess"` (workers are children of the supervisor; `"pod"` is the optional reference deployment's,
  refused unless `FACTORY_POD_BACKEND=on` and its backend is configured, `docs/pod-backend.md`).
- `repo_skills` (default true): the text of the target repository's own `.claude/skills/*/SKILL.md`, read from the job's
  base commit, goes into the worker's system prompt as instructions only (no scripts, other files or front-matter
  settings). A skill over 64 KiB, or past `repo_skills_budget_kib` (default 256 KiB) in all, is listed (name, description,
  path), not included.

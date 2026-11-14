# Architecture and operation reference

How Factory turns a plan into pull requests, what it publishes and what it leaves to GitHub, how to
operate a run, and where its trust boundaries are. The per-repository configuration file, the model
and effort rules and the issue labels are in [configuration.md](configuration.md); installing and
starting Factory is in the [README](../README.md). The manager (the optional HTTP service), the
optional Azure/AKS reference deployment and the module layout are at the end of this page.

## Run

`--repo` and `--run-dir` are global options and go before the subcommand.

```sh
factory --repo org/name --run-dir "$FACTORY_DATA/repos/org/name/runs/r1" plan --limit 30    # or: plan --issues 12,15
# Inspect plan.json before executing; an imported plan (`run --plan-file`) also works.
factory --repo org/name --run-dir ... run --concurrency 2 --verification-concurrency 1
factory --repo org/name --run-dir ... status --json
```

On a deployed manager nobody types these: a `factory-build` label starts them (see [Labels on issues](configuration.md#labels-on-issues)); the
commands are for local use and the break-glass path. `plan --issues N,N,...` plans exactly those issues
(at most 100) instead of every open issue and cannot be combined with `--limit`.

`plan --issues N,N,... --append-to <run>` plans more issues for a run that is already running (no new
run directory, no repository lease). The planner sees the run's current jobs; the result is written as a
validated plan fragment, `inbox/fragment-append-<time>-<random>.json`, which the running supervisor ingests
(`plan.json` and `state.json` are never touched by the command). A fragment may also carry `dispositions`
(`blocked`, `covered` or `investigation`, each with a reason capped at 500 characters and never logged) for
issues that become no job; they are recorded on the run and labelled (`blocked` and `investigation` become `factory:blocked`).
It is refused, with a fixed-format error, when the run does not exist, was not started, is draining, has
reached the job cap (10) or has reached `run_budget_usd` (checked before the planner runs); issues that are
closed, ignored, pull requests or already in the run are dropped with one line each. Two appends to one run
are serialised by `<run>/append.lock`, and each writes its own `planner-usage-<name>.json` (never over
`planner-usage.json`); the run's cost sums them all. See "Plan fragments" in `docs/manager.md`.

`run` exits 2 while any job is not `pr_ready`, `merged` or `closed`. Plans become
immutable when execution starts. `--timeout`, `--review-rounds`,
`--budget-<phase>` and `--verify-domain` override the config for one command.
Each worker invocation has a dollar cap (`--max-budget-usd`: plan $5, build $8,
review $4, verify $6, preflight $0.50 by default); usage and cost are recorded in
`state.jobs[*].usage[]` and `status --json` reports `cost_usd` per job and per run. It also reports `tokens` per phase: the input, output,
cache write and cache read token counts of the sessions and the prompt cache hit rate (cache read over cache read plus cache write plus uncached
input; `null` when there is no input at all). The counts are also kept in the history ledger, so they outlive a purge, and exposed as
counters per repository and phase (`docs/operations.md`, "Tokens and cache hit rate"; `docs/metrics.md`).

### Assignment contract

```json
{
  "version": 1,
  "issues": [{"number": 123, "title": "Example", "body": "Details"}],
  "jobs": [{
    "id": "fix-example", "issues": [123], "context_issues": [],
    "objective": "Concrete outcome", "likely_paths": ["src/example.py"],
    "acceptance": ["A testable outcome"], "depends_on": [], "feature": false
  }],
  "dispositions": [{"issue": 123, "status": "job", "job": "fix-example", "reason": "Why ready"}]
}
```

Every selected issue gets exactly one disposition: `job`, `investigation`,
`covered` or `blocked`. `likely_paths` are literal repository paths (no globs); `.`
locks the whole repository. Overlapping builders run serially; reviews and
verifications run concurrently in their own worktrees. A job with `depends_on`
waits until the dependency's PR is merged; `run` again after the merge refreshes
GitHub state. `feature: true` marks user-visible behaviour changes; whether they
trigger the verify phase is `verify.when`. `artifacts.roots` (default none) names
directories under which each job reserves `factory-<run>-<job>` paths for
verification artifacts; they are stored in the plan.

#### Where an acceptance criterion is proven

Each `acceptance` item is a plain string or an object
`{"text": "...", "tag": "sandbox|ci|operator", "checks": ["..."], "workflow": "..."}`:

- `sandbox` (the default): Factory can prove it in the worker sandbox. A plain string, and an
  object without a `tag`, are `sandbox`, so existing plans and fragments stay valid.
- `ci`: proven by named required checks of the base branch (`checks`) or a named
  `workflow`, on the exact head commit. At least one name is required, and only `ci` takes names.
- `operator`: only a person can prove it (production behaviour, a paid step, anything that
  needs credentials the sandbox must not have). It may carry a `reason` (one sentence, at most 300
  characters; only `operator` takes one) saying why it cannot be proven automatically. A reason never costs the
  plan: a longer one is cut to 300 characters ending in `...`, and one that is blank, made only of invisible
  characters or given on another tag is dropped (the checklist then says `Only a person can prove this.`). Only a
  `reason` that is not a string is refused.

The tag says *where* a criterion is checked, never *whether*. The verify phase is told that a
`ci` criterion is checked elsewhere and must not be skipped, and is given `operator` criteria apart
(a person's checklist, never a reason for `complete=false`); a `ci` criterion
with no matching required check is reported as **unproven**, never as satisfied. The PR body
lists each criterion as `[tag] text (checks: ...)`, and `status` (text and `--json`
`criteria`) shows the tag, the names and a `proof` of `satisfied`, `pending`, `failed` or `unproven`
for each: a `ci` criterion is `satisfied` only when all its named checks are among the PR's required
checks and passing (`pending` while one still runs, `failed` when one failed or was cancelled; a workflow name alone cannot be matched, so it stays `unproven`), an
`operator` criterion stays `unproven`. A required check that GitHub skipped (bucket `skipping`, for
example behind a path filter) counts as passing, as it does for GitHub's merge rules and for `factory/gate`.
A `sandbox` criterion is `satisfied` only by a verification that ran: when `verify.when` skipped the verify
phase (the record carries `skipped: true`, and `status` also treats an older complete record without outcome
assertions that way) the criterion stays `unproven`, with a `Verification: not run` line in the text output wherever it explains such a row (`verification_skipped` in `--json`),
because nothing will ever turn it into evidence. `Runner.publish` decides which `ci` criteria the base
branch's required checks cover (branch protection and rulesets) and records those names
(`published_required` in `state.json`); `status` reads the pull request's own required list
(`gh pr checks --required`). When the pull request lists the checks a `ci` criterion names as required but publish did not
judge them required, the criterion is shown `unproven` with a `required_sources` field (`publish` and `pr`,
in `--json`) and a `Required checks disagree` line, never as satisfied. A check publish judged required
that the pull request's list does not hold yet is not a disagreement (the list only holds checks that
already reported): the criterion stays `unproven` until it does. A run that recorded no list is not compared. A job with a `ci` criterion that no required check of the base
branch covers is published as a draft (no `factory:ready`, `factory/gate` failing) with those criteria
listed: nothing proves them before merge, so they never count as satisfied. An `operator` criterion
is different: it is listed as an unchecked task under `## Needs a person` at the end of the PR body (see
"Hand-off to GitHub") and never holds the job back. Criteria text, tags and names are untrusted model output:
they are validated and displayed, never executed. A criterion is rejected for an unsupported shape, text that
is not a non-empty string, an unknown tag, a `ci` criterion with neither a check nor a workflow name,
names on any other tag, a `reason` that is not a string (any other problem with a `reason` is cut or dropped, as above),
and any check or workflow name that is not printable, has leading or trailing whitespace, is longer than 120
characters or is repeated (at most 20 checks). Control characters in the criterion `text` are not rejected:
the text is neutralised where it is shown (control characters blanked on one line in `status`; issue references broken and the `Needs a person` heading and task marks defused in the PR body).

#### Environmental test failures: the baseline comparison

The worker sandbox has no network and a read-only temp area, so some of a repository's own tests can fail there for
reasons that have nothing to do with the change. Factory does not let those hide a real failure, and does not let them
make every pull request incomplete:

- **What it does.** The verifier reports the exact test command it ran and the ids of the tests that failed or errored
  (`test_run`). Factory runs that same command at the job's recorded base commit, in a sandboxed worker session (phase
  `verify-baseline`, never in the supervisor), and compares the two lists by test id. A test that fails on the head and
  not on the base is **new**: it always makes verification incomplete, and the blocker names it, even when the verifier
  said `complete=true`. A test that fails on both is **environmental**.
- **What clears a verdict.** Environmental failures alone make the pull request complete only when the verifier set
  `test_run.sole_blocker` (it names those tests as the only reason for `complete=false`). Without it the comparison is
  still recorded, but the verdict stays incomplete: the claim that nothing else blocks comes from the verifier, not from
  the data. The required metadata (`scenario`, `environment`, `assertions`, the commit) is checked afterwards as before.
- **Fallback.** When the baseline cannot finish (timeout, crash, an unparseable result, a different command, no recorded
  base commit) the verifier's verdict stands unchanged, whichever way it points, the reason is recorded, and nothing is
  cached. A verifier that reports no `test_run`, or no failing test, is not compared at all.
- **Cache.** Results are kept per repository, base commit and test command under `${FACTORY_DATA}/repos/<org>/<name>/baseline/`
  and never expire: a new base commit or a different command runs a fresh baseline, and deleting the directory forces one.
- **In the pull request.** The Verification section has a `Baseline comparison` block (status, command, base commit,
  cache hit or miss, the new failures and the environmental ones, or the fallback reason), then the `Where the checks
  ran` block (see "Hand-off to GitHub"), then the verifier's verdict as JSON. The body prints at most 20 test ids per
  list and then `and N more`; the record in `state.json` keeps them all.

`docs/operations.md` ("Verification baseline") has the operating details.

## Hand-off to GitHub

For a job whose independent review passed and whose verification is complete
(`pr_ready`), Factory pushes `factory/<run>/<job>`, then:

| Item | Value |
|---|---|
| PR | opened **ready for review** against `base_branch`, authored by the GitHub App |
| Title | `factory: <summary> (#N)`, or `(#N, #M)` for a job with several issues. The summary is neutralised and, to fit GitHub's 256-character limit, cut with `…`; the issue suffix is never cut. Set on `gh pr create` and again on every `gh pr edit`, so ready/draft flips keep the format |
| Body | The **first line** names what the PR resolves, built only from the plan's job issues: `Closes #N` when the PR is ready, `Refs #N` when it is incomplete or the issue is labelled `factory-ignore` (or its label could not be read) by publish time. Several issues share the line with the keyword repeated (`Closes #A, closes #B, refs #C`); a cross-repository issue is written `owner/repo#N`. A job with no issues is an error. **A one-line notice under the first line says in words what merging does** (`closes these issues: #A, #B` or `does not close the issue it references (#N)` with the reason and what to do: apply `factory-verified`); it never puts a closing keyword before an issue number (GitHub reads one without understanding a negation), and it is rewritten with the first line when the completion label or the epic pass changes it. `Refs #M` for context issues and the ignored/unread notes sit below, in the Assignment section, followed by Acceptance, Implementation, Changed scope, Validation and Verification (the baseline comparison, a `Where the checks ran` block and the verifier's verdict, bounded) |
| Label | `factory:ready` only (the auto-merge workflow enrols only PRs that carry it) |
| Status | `factory/gate` = `pending` while publishing, then `success` on the exact head (`review+verify passed @<sha7>`); `pending` (`awaiting required CI: <checks>`) while a `ci` criterion still waits for required checks, resolved by `reconcile` (below) |

**Needs a person.** Each `operator` criterion of the job is also listed as an unchecked task, with its
`reason`, under a fixed `## Needs a person` heading that is the last section of the body (the first line and
every other section are unchanged). It is information for the reviewer: it never changes
`verification.complete`, `factory:ready`, `factory/gate`, the draft state or `Closes`/`Refs`, so an
unchecked list does not hold a merge; review and verification are told these items are not theirs to prove.
**With `required_approving_review_count` at 0 (the value in `handoff/ruleset.json`), a plan with only `operator`
criteria, or a mixed plan whose other criteria pass, is ready: `factory/gate` goes `success`, auto-merge proceeds
and `Closes #N` fires with every item unticked.** A repository that wants a person to check the list first raises
`required_approving_review_count` (`docs/handoff.md`, "Operator setup", the ruleset step); auto-merge then also waits
for an approval, and a retry's new head needs a fresh one. Each item has a
stable id (a hidden marker derived from the criterion text) and Factory never ticks one: when it
rebuilds the body it carries over only the ticks a person made in the live body, for ids the plan still
has, and only while the `## Needs a person` section is the last block of the body. Text added after the list
(a note, a comment, another section) makes Factory ignore every tick until it is removed, so keep the list last;
a copy of the heading anywhere else in the body, for example after the real section was deleted, is not read unless it too is the final block, and one item line that lost its hidden marker (edited in the web editor) also makes Factory ignore the ticks of the whole list until the marker is back.
The ticks are as trustworthy as the body: anyone who can edit the pull request can make one, a tick made on an old
head carries onto a changed head (so a person re-checks after a material change), and a tick made in the moment
between Factory's read of the live body and its edit is overwritten and has to be made again. A plan with only `operator` criteria is published ready like any other when review and verification pass (the
verifier must still report the checks it actually ran as `assertions`, such as the repository's tests), and
the section says that Factory proved none of them. `status` shows the number of such criteria per job
(`needs_a_person` in `--json`). `docs/handoff.md` ("The Needs a person checklist") has the details.

**Where the checks ran.** The Verification section of the body says which checks ran where, in one bounded block
(`**Where the checks ran**`) with three bullets. **Sandbox**: the worker session's own run (the test command, the base
commit, the baseline status and the counts of failing, new and environmental tests). **CI**: the required checks of the base
branch, which run on GitHub and which Factory only reads: at publish it names the checks `factory/gate` awaits, and when
`reconcile` resolves the gate (green, red or the timeout) it replaces that one bullet, between two fixed markers, with the
head commit, the outcome and each required check's name and conclusion. **Operator**: the items of the `Needs a person`
checklist, which Factory proved none of. Nothing else of the body changes when the CI bullet is updated (a tick a person
made stays), a body without the markers is left alone, and a failed update changes nothing about the gate: the gate
status stays the authority and the block is a report. Check names, the test command and criterion text are untrusted:
they are shown flattened, cut and inert. Updating the body needs no permission the Factory App does not already have
(`pull_requests: write`, which opening the pull request needs): it is the same `PATCH` of the pull request that
`completion` uses. As at publish, a tick made in the moment between Factory's read of the live body and its edit is
overwritten and has to be made again.

A required pending `factory/gate` blocks the merge until it resolves: when only `ci` criteria covered by required checks of the
base branch remain, `reconcile` (the manager's idle loop) reads `gh pr checks --required` on the exact published head
and posts `success` when they are green, `failure` (and `factory:ci-failed`) when one is red, or a `timed out waiting for
CI` failure after one hour; a moved head is never gated, and `docs/handoff.md` ("The pending gate") has the details. A
repository without required checks never gets a pending gate: its `ci` criteria stay unproven.

A job that changes `.github/workflows` cannot be pushed by the Factory App, which has no `workflows` permission and must
never get it. `plan` (and `plan --append-to`, for the jobs it appends) prints the jobs whose planned paths include
workflow files, and `status` does too before the run has started (and marks them in its rows afterwards), the builder
is told a person will publish it, and when GitHub refuses the push of finished work (review and verification passed) the job ends `needs_human_publish` instead of `failed`: the commit,
branch and worktree are kept, a `git bundle` is written next to the run's files, and the redacted stderr of the push is
kept in the job state. A person reads the workflow diff, pushes the branch with their own credentials from the Factory
clone (never with git inside the worker-writable worktree), then `resume JOB --phase publish` publishes it as a
**draft** with `factory/gate` failing: a person's push is not a review, so such a pull request (also one whose App push
was accepted) is enrolled for auto-merge only after a person applies `factory-verified` and `factory:ready`. The
janitor never purges a run that holds a `needs_human_publish` job. `docs/operations.md`, "Jobs that change
`.github/workflows`", has the steps.

A job that could not pass review or verification (`pr_incomplete`) becomes a
**draft** with no label and `factory/gate` = `failure`, and `Refs`
instead of `Closes` (a person who verified it by hand can apply the `factory-verified` label to complete it, or close the issue by hand; `docs/handoff.md`, "After the supervisor is done"). Repository rulesets that require `factory/gate` from the
Factory App plus GitHub auto-merge then merge only passing PRs. If CI fails
nothing merges; `status` reports `handoff_status` (`published`, `ci_failed`,
`merged`, `closed`) from the PR's check rollup, and an operator chooses
`retry JOB --reason ...` or a manual fix (a new head invalidates the gate status).
`status` asks GitHub about published PRs unless `--offline` is given; `run`
reconciles merged/closed PRs into job status.

Every incomplete or pending outcome of a published job carries one fixed **reason code**, worked out from the
verdicts, the baseline record and the gate outcome already on hand (no extra GitHub call, and no issue, criterion or PR text in it):

| Code | Meaning |
|---|---|
| `real_failure` | review did not pass, or verification found tests failing on the head and not on the base |
| `ci_failed` | a required check is red, or waiting for CI timed out |
| `ci_pending` | an awaiting required-CI gate: the required checks have not finished on the head |
| `workflow_hold` | review and verification passed, but the head changes workflow files: a person must review the diff and apply `factory-verified` (a deliberate hold) |
| `env_noise` | verification incomplete only because of failures the baseline comparison classed as environmental (the verifier named them the sole blocker and called the verdict incomplete, and the baseline did not already clear it) |
| `unverified_other` | incomplete for any other reason (the catch-all) |

Precedence when several apply: `real_failure`, `ci_failed`, `ci_pending`, `workflow_hold`, `env_noise`, `unverified_other` (first wins). A complete, ready
job has no code. There is no `operator_item` code. The code is the ` [<code>]` suffix of the `factory/gate` description, one line in
the PR's Verification section, `reason_code` per job in `status --json` (and in the text view), and one log line per published job
(`factory: reason_code job=... code=...`, ids and counts only). `docs/handoff.md` ("Reason codes for incomplete or pending outcomes") has the details.

`handoff/` holds the files an operator applies on the GitHub side (an organization ruleset and a
per-repository auto-merge workflow); `docs/handoff.md` explains them. Nothing here installs them.

## Operating a run

```sh
factory ... drain                       # finish active phases, then exit; does not mark work complete
factory ... stop JOB [--reason ...]     # stop one running job: its step may finish within a grace period, then its worker is terminated
factory ... resume JOB --phase review|verify|publish [--reason ...]   # then `run`
factory ... retry JOB --reason "what changed"                         # new implementation attempt
factory ... import-evidence JOB --file evidence.json                  # externally obtained verification
factory ... record-blocker --file blocker.json                        # shared diagnosis
factory ... record-capabilities --file caps.json
factory ... preflight JOB --phase build|verify                        # probe the real worker sandbox
factory ... purge                       # remove a finished run: worktrees, factory/<run>/* branches, run dir, then release its issues' factory:* labels (best effort)
factory ... recover                     # mark a dead supervisor's running jobs interrupted; prints "Recovered <n> interrupted job(s)"
factory ... reconcile                   # idle run: follow published PRs (factory:merged / clear label) and answer the factory-verified label; the manager runs it
factory --repo org/name purge-clone     # no --run-dir: remove the managed clone once no run directory or linked worktree is left
```

`recover` takes the repository lease and does what every mutating command does first: a job
is made `interrupted` only when no recorded child can still be alive (a pid from another host
counts as dead only while the lease is held). It refuses otherwise, and prints `Nothing to
recover` without writing anything when the run has no `state.json`. `purge-clone` takes the
lease too and refuses (exit 1) while any run directory or linked worktree remains, when the
clone path is a symlink, or when it does not resolve inside `${FACTORY_DATA}`; it prints
`Removed clone` or `Nothing to remove`, and the next command that needs the clone makes a new one.

- **One supervisor per repository.** A local `flock`, or an Azure Blob lease with a
  fencing counter when `FACTORY_LOCK_ACCOUNT_URL` is set. Every `git push`, PR
  create/edit and status post first asserts the lock is still held; a lost lease
  stops the supervisor and kills its children.
- **Recovery.** Interrupted jobs need an explicit `retry` or `resume` (`recover` makes
  them `interrupted` on its own, without choosing what happens next). Recorded pids
  carry a host id: a pid from another host is "unknown", and recovery treats it as
  dead only while this process holds the repository lease.
- **Capacity.** All workers of the pod share one worker budget (default derived at runtime
  from the container's cpu and memory, 21 on the 6 vCPU / 24 GiB pod of the reference deployment) and each
  session is watched by a memory guard. A job over the budget waits (`status` says
  `pod worker budget: n/N workers in use`) and the supervisor keeps polling; a session whose
  process tree exceeds the memory ceiling is killed (the whole tree, one process at a time,
  including descendants that left the worker's process group) and its job fails with a clear reason.
  Both limits fail open, can be set by hand or turned off (`FACTORY_MAX_TOTAL_WORKERS`,
  `FACTORY_WORKER_MEMORY_MB`), and every session records its peak memory and cpu.
  `docs/capacity.md` has the derivation, sizing, the scaling options and what is **not yet
  measured** (whether the agents and their tests fit on the pool is still UNVERIFIED).
- **Stop-the-world breaker.** A failed session's API error is classified into a closed code
  (`apierror.py`: `spend_limit`, `rate_limit`, `overloaded`, `auth`, `rate_limit_unknown`, `other`;
  anything unrecognised is never a spend limit). A spend limit trips the breaker at once, a refused
  key at 2 and an unclassified 429 at 3 within 5 minutes: it pauses the trigger through its own
  marker (the deploy gate's resume does not clear it), drains the runs in progress and waits for an
  Operator to clear it with `POST /spend-breaker` after the limit was raised. The error shapes are
  assumed, not recorded from a real run (`docs/capacity.md`, "Stop-the-world breaker").
- **Purge** refuses while the supervisor or any recorded child may be alive. It never
  touches remote branches.
- `import-evidence` manifest: `job_id`, `commit` (the current head), `tested_commit`,
  `complete`, `scenario`, `environment`, `assertions` (plus `recording_url` in
  recording mode). Complete evidence needs all of them; incomplete evidence needs a
  `blocker`.
- A retry of a `pr_ready` job with failing CI needs a sanitized diagnosis in
  `--reason`; pending, cancelled or missing checks do not qualify.
- **A follow-up is a retry on the pull request's own branch.** With `followup = true`, an `@<App slug>`
  comment (or `@<mention_handle>`) by someone holding `followup_min_permission` on an open Factory pull request is
  recorded, and the oldest request becomes a session: `retry <job> --followup`, then `run`. It is refused (a fixed
  code in `followups/pr-<N>.json`, no worktree, no new run) when the run is gone, the job is not published, the pull
  request is closed or its head is not the commit the job retained. The builder gets the triggering comment, the
  later comments of authorised people and the failing checks' log tails as bounded, redacted, untrusted data; a
  session that commits nothing leaves job and issue status unchanged; the new head gets fresh review, verification
  and its own `factory/gate`. Per-user cooldown 300 s, `followup_max_sessions` per pull request, `run_budget_usd`
  for the run as a whole (the original build included). Once a request is settled Factory says so on the pull request:
  ONE comment per pull request from the Factory App, edited in place (hidden marker, fixed text plus the new head,
  nothing derived from comments, no `@mention`), only for requests that were queued and then resolved or refused by a
  state check; `followup_reply = false` or `FACTORY_ISSUE_COMMENTS=off` stops it, and Factory's own reply never starts a
  follow-up. `docs/operations.md` ("Follow-ups on pull requests", with the threat model) is how to use it;
  `docs/manager.md` has the guards, codes and lines.
- **A retry starts from the current base.** Before a builder session on an existing job
  branch (retry, CI repair, follow-up) the supervisor fetches `origin/<base>`; if the base
  moved it fetches the PR head and merges the base into the job branch itself (the sandboxed
  builder has no network and no git writes). A clean merge is pushed with
  `--force-with-lease` to `factory/<run>/<job>` only, under the lock, and `factory/gate` is
  re-posted `pending` on the new head. A conflict stays in the worktree as conflict markers
  and the builder is told to resolve them; the supervisor commits the resolution and review
  and verification run as usual. An unchanged base does nothing. A worktree with
  uncommitted changes, or a remote branch with commits the local one lacks, stops the sync
  with an error instead of overwriting anything.

## Authentication

Local mode uses your own `gh` and `git` login. With the GitHub App
(`FACTORY_GITHUB_APP_ID`, `FACTORY_GITHUB_APP_SLUG`, `FACTORY_GITHUB_APP_USER_ID`,
`FACTORY_TOKEN_BROKER`, plus the private key in `GITHUB_APP_PRIVATE_KEY`) the
supervisor asks `tools/token_broker.py` (its own virtualenv; the only code that
imports `cryptography`) for a repository-scoped installation token, keeps it in
memory, refreshes it ten minutes before expiry and injects it only into its own
`git`/`gh` children through the environment (`GH_TOKEN` and a credential helper).
It is never written to `.git/config`, a remote URL, `hosts.yml` or a run file, and
workers never receive it. Commits and PRs are authored as `<slug>[bot]`. A
half-configured App fails loudly instead of falling back to ambient credentials.

## Worker boundaries

Workers run `claude -p --bare --setting-sources user --permission-mode dontAsk
--permission-prompts none` with a generated `--settings` file, never
`bypassPermissions`. The sandbox is mandatory (`failIfUnavailable`, no unsandboxed
retries); plan and review are read-only; build and verify write only their own
worktree; the shared `.git` is read-only everywhere (workers cannot commit); network
is an empty allowlist except verify (`sandbox.verify_domains`). The sandbox hides
`ANTHROPIC_API_KEY` from the commands a worker runs. Each invocation has a throwaway
`HOME`. The repository's `CLAUDE.md` is read from the job's base commit, and so are its skills (`.claude/skills/<name>/SKILL.md`, text only: no script, file or setting of a skill reaches the worker; a skill of at most 64 KiB goes in full while the full texts stay within `repo_skills_budget_kib` (default 256 KiB, about 64k tokens), in name order; every other skill is listed with its description and path so the worker can read the file; `repo_skills = false` turns it off): Claude Code does not discover a repository's skills under `--bare`, and the one flag that would load them (`--add-dir`) reads the worktree, which a builder can edit, so a pull request could plant instructions that its own reviewer follows. With
`worker.uid`/`worker.gid` configured, workers run as a separate user that shares only
a group with the supervisor (worktrees and the per-invocation home are group-writable;
the rest of the run directory is private).
The supervisor's own git calls on those worktrees go through `gitsafe`: explicit `GIT_DIR` and `GIT_WORK_TREE`
(the worktree's administrative directory, learned from the main clone and recorded as `gitdir` in the job state),
`core.fsmonitor` and hooks off on every call, the supervisor's environment without its secrets, submodules ignored
by `status` and `diff` (`--ignore-submodules=dirty`), and a refusal (`worktree .git pointer was changed`) when a worker
rewrote the worktree's `.git` pointer or (`worktree holds a nested git repository that is not a submodule of it`) planted a
nested repository, so a planted fsmonitor program, hook or nested clean filter cannot run as the supervisor
(`docs/pod-backend.md`). `managed-settings.json` is the matching
non-overridable baseline for `/etc/claude-code/`. `python tools/sandbox_e2e.py`
checks the real sandbox behaviour.

Agent transcripts, prompts and issue text stay in files under the run directory and
never go to stdout. They may contain customer data: do not commit run directories.

The supervisor holds only the ambient capabilities `CAP_SETUID`, `CAP_SETGID` and `CAP_KILL` (to start
workers under another UID and to kill their process groups). Workers start through
`factory.execnocaps`, which drops every ambient capability first; without that the worker would
keep them across exec (the kernel clears capabilities on a UID change only to or from 0) and
could switch back to the supervisor's UID.

## The manager

`factory-manager` (`python -m factory.manager`) is an optional multi-repo HTTP service in the standard
library that sits in front of the CLI; nothing above needs it. In `FACTORY_MANAGER_AUTH=entra-jwt` mode the
manager itself requires `Authorization: Bearer <Entra v2 access token>` (RS256, checked against the tenant's
signing keys, issuer, audience and tenant), ignores `X-MS-CLIENT-PRINCIPAL`, requires the app role
`Factory.Operator` (mutations) or `Factory.Viewer` (GET), and maps each endpoint to the matching `factory`
CLI argv (`plan`, `run`, `status --json`, `drain`, `stop` (one job), `pause`, `resume`, `retry`, `preflight`,
`record-*`, `import-evidence`, `purge`); it never reimplements CLI logic. The default mode, `easy-auth`, trusts
the `X-MS-CLIENT-PRINCIPAL` header that the Easy Auth layer of Azure Container Apps sets in front of an app:
anything that can reach the port can write that header, so this mode is for loopback use, for tests and for a
deployment behind such a layer. There is no unauthenticated mode. Endpoints, status codes, environment
variables and the audit line format: `docs/manager.md`.

Besides serving requests, the manager runs three background loops (`src/factory/operations.py`): it
recovers runs after a restart, drains a run that reaches `run_budget_usd`, and purges finished or
abandoned runs and, with the last run, the managed clone. With a GitHub App configured it also checks at
start, and every few minutes, that it can mint a token for each repository: `GET /repos` shows `"github": "ok"`
or, when the installation has not accepted a permission Factory requests, `"permission missing: actions"`, so a
bad rollout is visible before the first job (`docs/manager.md`). Day-to-day operation (start, status, drain vs
pause, recovery, purge, key rotation, the Console spend limit) is `docs/operations.md`. The manager can also serve
Prometheus metrics on a separate internal port (off by default, numbers only, behind a bearer token off the loopback): the catalogue, the threat model and what is not exposed yet are in
`docs/metrics.md`.

What Factory did before is not lost with a purged run: the supervisor, the CLI and the manager append ids, statuses, numbers, pull
request URLs, reason codes, costs, token counts per phase and durations (never issue, pull request or agent text, transcripts, prompts, or any credential or secret; a token count is a number, not a secret) to a bounded history
ledger in `${FACTORY_DATA}/history`, outside the repositories' directories, so `purge` and `purge-clone` leave it alone
(`FACTORY_HISTORY_MAX_BYTES`, default 64 MiB, and `FACTORY_HISTORY_RETENTION_DAYS`, default 365). Four read-only, authenticated
`GET` endpoints of the manager serve it, and the active runs, as JSON for a dashboard: `/dashboard`, `/history/runs`, `/history/jobs`
and `/repos/{o}/{r}/runs/{run}/jobs/{job}` (any Factory role; no `gh` call and no CLI per request; `docs/manager.md`, "Dashboard
endpoints"). A static browser page at `/ui/` (no build step, no third-party script) shows them: active runs and jobs, the filtered history and one job in
detail; the page files and its sign-in settings (`/ui/config.json`) are the only thing served without a token. With `entra-jwt` the page **signs in with Microsoft** (MSAL, code flow with PKCE, redirect
flow, the library vendored and hash-pinned) and the manager still verifies the bearer token and the app roles itself. In `easy-auth` mode the page takes a pasted token and
sends it as `Authorization: Bearer`, which `easy-auth` mode ignores: it only works behind a platform that turns that bearer token into the `X-MS-CLIENT-PRINCIPAL` header, so in a plain local run
the page loads but every data call is answered `401` (use `curl` with the header, as the README shows, or `entra-jwt`). The responses carry a strict
Content Security Policy (`connect-src` allows only the page's origin and Microsoft's login origin), a permissions policy and, behind HTTPS, `Strict-Transport-Security`
(`docs/manager.md`, "The browser page" and "Browser sign-in"; `docs/operations.md`, "The dashboard in a browser"). Operators can also read a live job's transcripts, stderr and the supervisor and plan logs there, and through `GET .../logs` (allow-listed files, plain text, redacted, bounded ranges, a Viewer gets `403`; `docs/manager.md`, "Log endpoints"). `purge` first keeps a redacted, compressed copy of a run's logs for 30 days (`docs/manager.md`, "Log retention"; `docs/operations.md`, "Logs").

## Optional reference deployment on AKS

Nothing here needs a cluster: Factory runs from a checkout on a developer machine or a VM. The repository
also contains one worked deployment of the manager on Azure Kubernetes Service, with its Terraform, its
container image and its operating guide. It is a reference that you can read, adapt or ignore, not a
requirement: the values it needs (the host name, the tenant, the image digest) are yours to set, and
the digest it ships is a placeholder that `infra/variables.tf` rejects.

One Kubernetes **Deployment** (`replicas: 1`, strategy `Recreate`) on a dedicated single-node Azure Linux 3 pool of an AKS
cluster (static egress through a NAT gateway) runs the manager behind an HTTPS ingress (Caddy, Let's Encrypt). Run state and
worktrees live on an **Azure Disk** (ext4, ReadWriteOnce, hence one replica), the one-supervisor-per-repo lease is a blob
lease taken with a workload identity, and workers are `claude -p` subprocesses under a separate UID.

The sandbox passed in this pod with three settings together (`hostUsers: false`, `procMount: Unmasked`, `appArmorProfile: Unconfined`); the smaller sets that were tried failed, and two subsets were not tried, so no single setting is shown to be necessary on its own;
Factory's own sandbox, capability model and managed settings are not weakened. The operator of the reference deployment accepted them; the sets that were tried and what each showed are in `spikes/S1.md`.
They also make NFS impossible, which is why `/data` is a disk.

* `infra/README.md` and `infra/`: Terraform (azurerm 4.x plus azapi, and the kubernetes provider): VNet, NAT gateway, NSG, the AKS cluster
  (Entra ID and Azure RBAC, workload identity, Key Vault secrets-store add-on), Container Insights with a Log Analytics
  workspace (4-day retention), the locks storage account, Key Vault, the identity, the namespace, Deployment, disk and ingress,
  and an optional Prometheus and Grafana deployment in the same cluster (Grafana at `/grafana/` with Entra sign-in, alert rules and
  dashboards as files, off until enabled; "Monitoring"). The repository does not ship the pipelines that applied it: keep your values in a repository (or CI) variable of your own
  (the docs call it `INFRA_TFVARS`) that your own pipeline passes to `terraform` with `-var-file`, and run `tools/infra_deploy_gate.py`
  (pause, drain, wait, resume) around any `terraform apply` that restarts the manager. `ingress_custom_host` serves the same site under a
  name of your own as well as the Azure name (`docs/manager.md`, "The browser page"; `infra/README.md`, "Browser sign-in and custom host name").
* `Dockerfile` and `image/`: the pinned image (base digest, gh, terraform and Node by sha256, Claude Code by
  npm lockfile integrity, hash-locked Python dependencies), the small offline verification tools a worker can
  use (`jq`, `make`, `shellcheck`, `terraform fmt`), the entrypoint and its capability
  model, the smoke test. See `image/README.md`; what is verified where (tools in the pod, CI as the authority
  for the rest, isolated runners, and what is deliberately not in the pod) is `docs/verification-tiers.md`.
* `docs/operations.md`: day-to-day operation (start, status, drain vs pause, recovery, purge, key rotation).
  `docs/pod-backend.md` and `docs/pod-backend-runbook.md`: the experimental per-session pod backend, off by default.
* `spikes/`: the runnable S1 to S3 procedures and their recorded results. S1 failed on ACA and is recorded PASS on AKS with the three
  relaxations; S2 and S3 passed on both. **Do not `terraform apply` unless S1, S2 and S3 are recorded PASS** (`spikes/README.md`;
  the first line of each record says which it is, and nothing checks it for you).

## Layout

`src/factory/` is the package:

* `agent.py` Claude adapter, `apierror.py` API-error classifier and `stopworld.py` its breaker, `backend.py` how a session is prepared, launched and collected (`LocalBackend` runs it as a child process), `capacity.py` pod worker budget and
  resource defaults, `memwatch.py` per-session memory guard and footprint, `execnocaps.py` capability-dropping
  worker launcher, `phases.py` policy table, `model_choice.py` model and effort choice, `config.py`, `github.py`, `github_auth.py`, `github_health.py`, `locks.py`.
* The optional pod backend (off by default): `podcontract.py` names shared with Terraform for the pod backend (read by `backend.py`, `podexecution.py` and `manager.py` so far), `podexecution.py` the `execution` setting and the pod switch (selects the pod backend only when both are on, refuses `pod` otherwise; off by default), `podworker.py` the pod backend's worker wrapper (`python -m factory.podworker`: fixed output, network canary, fetch and extract, then runs the agent, streams, posts the delta and the report, handles SIGTERM), `podreport.py` the closed report it posts with the delta, `launcher.py` the pod launcher program and `launcher_client.py` its manager-side client (started only by the launcher container of `infra/pod_backend.tf`, which is off by default; `infra/worker_rbac.tf` and `infra/pod_backend.tf` declare its Role and the manager-pod containers that would run it and the relay, only with `pod_backend_enabled`, default off), `podpool.py` the pod pool: ceiling, pacing and reserve-on-admit before a pod is requested (used only by `podbackend.py`, which is off by default), `podliveness.py` what the supervisor may conclude about a pod session from the relay's record, the pod's status and the clock, and `podsweep.py` the orphan sweep and the pod census for the deploy gate (the manager sets the sweeper only when the pod switch is on and the backend can be built: `podexecution.sweeper`; with the switch off nothing sweeps and `GET /repos` has no `worker_pods`), `podbackend.py` the pod backend of one worker session (`PodBackend` over the launcher client, the relay's spool, the bundle and the delta; delivered, selected only by `execution = "pod"` and `FACTORY_POD_BACKEND=on`, nothing starts a launcher or a relay yet).
* `gitsafe.py` the only way the supervisor runs git on a worktree a worker touched, `purge.py`, `runner.py`, `manager.py` remote control surface, `history.py` history ledger, `dashboard.py` its read-only endpoints, `webui.py` with `ui/` its static browser page, `cli.py`.

`tools/` holds the token
broker, the sandbox end-to-end check and three read-only reports over run directories (`cache_report.py`: cache hit rate and
cold starts per phase; `cache_write_sources.py`: an estimate of which tools' results fill the prompt cache, per phase; `effort_report.py`: tokens, cost, turns and
outcome per recorded effort, `docs/operations.md`, "Comparing efforts"), `effort_ab.py` (the effort A/B harness that runs the controlled experiment against a sandbox repository, never merging, with a budget guard: `docs/operations.md`, "Running the effort A/B") and the check scripts of the live runbook of the pod backend (`live_checks.py` read-only `az` and `kubectl` checks, `live_reports.py` verdicts over saved files, `live_recording.py` the recording format and `live_pod_probe.py` probes for a pod of the mirror: delivered, not run; `docs/pod-backend-runbook.md`). `image/`, `infra/` and `spikes/` are described under "Optional reference deployment on AKS";
`handoff/` has the GitHub ruleset and auto-merge workflow for operators; `managed-settings.json` is the Claude Code sandbox baseline that the image copies; `.github/workflows/ci.yml` is the one CI workflow of this repository (compile, the unit tests, the token broker, the infra offline checks; it deploys nothing).

`docs/` holds every page of the reference documentation:

* `architecture.md` this page, `configuration.md` the per-repository config file, model and effort, labels and comments, and `manager.md` the manager reference (endpoints, authentication modes, settings).
* `operations.md` day-to-day operation on AKS, `handoff.md` the GitHub ruleset and auto-merge that finish what Factory publishes, `enrolling-repositories.md` how a repository is registered, `metrics.md` the Prometheus series and dashboards, and `capacity.md` the worker budget, the memory guard and sizing.
* `verification-tiers.md` what is verified in the pod, in CI and in isolated runners.
* `pod-backend.md` the design record and shared contract of an optional per-session pod backend (default off, nothing is switched on, no behaviour yet), `pod-backend-runbook.md` the live runbook for it (read-only checks of a live deployment, a throwaway mirror, the plan identity gate and the recording format; nothing in it was run) and `pod-backend-local-e2e.md` the first end-to-end run of its pod on a local kind cluster, with a stub agent.

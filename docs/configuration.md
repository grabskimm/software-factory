# Configuring a repository

The reference for the per-repository configuration file, the model and effort rules, and the issue
labels and comments Factory reads and writes. How a run works from plan to pull request, and how to
operate one, is in [architecture.md](architecture.md); how to install and start Factory is in the
[README](../README.md).

## Configure a repository

Each target repository has a file `${FACTORY_CONFIG}/repos/<org>__<name>.toml`
(unknown keys are errors):

```toml
repo = "org/name"
base_branch = "main"            # default; must be the repo's default branch
allow_non_default_base = false  # true: allow another base, issues then stay open
installation_id = 123456        # GitHub App installation (App auth only)
toolchains = ["node@22"]       # recorded for the image build
setup_commands = []             # validated but NOT executed by this version
prompt_addendum = "Extra operator guidance appended after the repo's CLAUDE.md."
repo_skills = true               # the repo's own skills (.claude/skills/<name>/SKILL.md, read from the job's base commit) go into every session's system prompt as instructions; false: none (default true, issue #260)
repo_skills_budget_kib = 256      # the most the full text of those skills may take in a session's prompt (16 to 1024 KiB; a skill over 64 KiB or past the budget is listed by name and path; a heavy repository raises it)
model = "claude-sonnet-5-5"     # default model of plan, build, review and verify without a [models] entry (default: the built-in per-phase table; never the preflight probe)
effort = "high"                 # default effort of those phases: low | medium | high | xhigh | max (default: the built-in per-phase table)
allowed_models = ["claude-sonnet-5-5", "claude-opus-5-5"]  # the ids an issue's factory-model label may select (exact when set; default: the built-in ids plus the ids this file configures)
max_effort = "high"             # the highest effort an issue's factory-effort label may select (default: the highest effort the built-in table and this file use)
abandon_after = "7d"            # manager: purge a run idle this long; "<n>d", "<n>h", "<n>m" or seconds, min 1h (default 7d)
run_budget_usd = 25.0           # manager: drain a run whose cost exceeds this many dollars (default: no limit)
planner_max_usd_per_hour = 2.0  # manager: no planner starts once the last hour's planner usage reaches this many dollars (default 2.0; 0 or "off": no cap)
auto_resume_after_restart = "never"   # manager: never (default) | once = run interrupted work once after a restart
auto_build = true               # manager: watch for the trigger label (default true; false opts this repository out)
auto_build_label = "factory-build"      # manager: the issue label that asks for a build
auto_build_min_permission = "write"     # manager: read | triage | write | maintain | admin; the labeller's role (below write: private repositories only)
auto_build_concurrency = 1      # manager: workers of an automatic run (1 to 64)
auto_build_max_issues = 3       # manager: issues taken per cycle (1 to 50); the rest wait
auto_build_timeout_seconds = 1800  # manager: per agent session limit of automatic runs (60 to 86400; anything else means 1800)
comment_on_blocked = true       # post one comment on an issue that is blocked, needs a decision or failed (false opts out)
epic_max_open_prs = 3          # the manager starts the next phase of an approved epic itself only while fewer than this many pull requests of Factory are open (the per-run budget applies to each phase's run, never summed)
epic_mode = true                # on by default: a too-large but decomposable issue gets a phase plan comment and a human gate instead of `blocked` (false keeps `blocked`)
observed_issues = true          # open an issue for what a builder or reviewer observed outside its job or could not fix (on by default; false opts out; FACTORY_OBSERVED_ISSUES=off stops it everywhere)
pr_style = "factory"            # "conventional" titles the PR and commit as Conventional Commits (<type>: <subject> (#N)) and keeps the description safe for a squash merge; for repositories that release from commit messages (semantic-release)
execution = "inprocess"         # where worker sessions run: "inprocess" (default, a child of the supervisor) | "pod" (a pod per session; REFUSED with a fixed text, nothing started, unless the manager switch FACTORY_POD_BACKEND is on and the pod backend is configured for the session; no fallback to inprocess; off by default, nothing starts a launcher yet; docs/pod-backend.md)
followup = false                # manager: off by default; true opts in to @<App slug> / mention_handle follow-ups on open Factory PRs (a session on the PR's own branch)
followup_reply = true           # manager: with followup on, say what became of a settled request in one comment per PR, edited in place (false opts out; FACTORY_ISSUE_COMMENTS=off stops it everywhere)
followup_min_permission = "write"       # manager: read | triage | write | maintain | admin; the commenter's role (below write: private repositories only)
followup_cooldown_seconds = 300 # manager: one request per user per PR in this many seconds (0 to 86400)
followup_max_sessions = 5       # manager: follow-up requests (sessions) per PR (1 to 100)
mention_handle = ""             # manager: an account name to mention (and whose PRs to follow) when there is no App identity; "" means the App slug

[verify]
when = "feature"                # always | feature | never
[acceptance]
mode = "tests"                  # tests | recording (recording also needs artifacts.roots)
[artifacts]
roots = []                      # directories that may hold verification artifacts
[sandbox]
verify_domains = ["registry.npmjs.org"]   # the only hosts the verify phase may reach
[preflight]
probe_url = "https://registry.npmjs.org/"
[models]                        # optional per-phase model overrides
[efforts]                       # optional per-phase effort overrides, e.g. review = "medium"
[aliases]                       # optional: alias = model id; the built-in opus, sonnet, haiku can be redefined here
[budgets]                       # optional per-phase dollar caps, e.g. build = 8
[resources]
max_active_agents = 6
[worker]                        # separate worker identity (numeric ids, set together)
uid = 10002                     # required in the container image: its `worker` user
gid = 10010                     # and `factory-work` group (FACTORY_REQUIRE_WORKER_IDENTITY=1)
```

Closing keywords only work when a PR targets the repository's default branch, so
`plan` and `run` fail when `base_branch` is not the default branch unless
`allow_non_default_base = true`. The managed clone lives at
`${FACTORY_DATA}/repos/<org>/<name>/clone` and runs under `.../runs/<run>`.
Without `--repo` (local mode) the checkout containing the current directory is
used and every config key keeps its default.

`abandon_after`, `run_budget_usd`, `planner_max_usd_per_hour`, `auto_resume_after_restart` and the five `auto_build*` keys
are top-level keys (above the first `[table]`) that only the manager reads; the CLI ignores them.
`abandon_after` is stored as whole seconds (1 hour to 3650 days); a bare number is
seconds, so `7` is rejected and `"7d"` is how to say a week.

`execution` is read by the supervisor (the CLI) for every session it starts, plan, build, review, verify and
the preflight probe alike. `"inprocess"` (the default) is how Factory has always run: the worker is a child of
the supervisor. `"pod"` is part of the pod backend program (`docs/pod-backend.md`) and is **off**: a repository
that says it is refused at the first session with a fixed text and nothing is started, both while the
manager-wide switch `FACTORY_POD_BACKEND` is not `on` and, with the switch on, whenever the pod backend cannot be built for the session.
There is no fallback to `"inprocess"`; the operator removes the line to go back.

### Model and effort

The Claude model and effort of each phase are chosen, most specific first, by:

1. **the issue's labels** `factory-model:<alias or id>` and `factory-effort:<level>` (at most one of each),
   for the **build and verify** phases only;
2. the repository's per-phase `[models]` and `[efforts]`;
3. the repository's `model` and `effort` defaults, for plan, build, review and verify (**not** for the
   preflight probe, which stays on its cheap built-in Haiku/low unless `[models]`/`[efforts]` say otherwise);
4. the built-in table (planner Opus 5.5 / high, builder Sonnet 5.5 / high, reviewer Opus 5.5 / high,
   verifier Sonnet 5.5 / medium, probe Haiku 5.5 / low).

A configuration with only `[models]` behaves exactly as before. The **reviewer stays independent** of the
builder (it uses the repository's configuration, never an issue's labels) and the planner runs before any job
exists, so neither is changed per issue. Aliases are one-to-one and case-insensitive: `opus` is
`claude-opus-5-5`, `sonnet` is `claude-sonnet-5-5`, `haiku` is `claude-haiku-5-5`, redefinable or extendable in
`[aliases]`; there is no prefix or fuzzy matching, and a label value that is not an alias must be a full model id
that `allowed_models` lists. Factory creates the label set (one `factory-model:<alias>` per alias, one
`factory-effort:<level>` per effort) when it plans, best effort; the `factory:` status labels are never read as a choice.

The labels are **authorised and validated when the issue is planned and never trusted**. Applying a label needs only
triage access, so a choice label counts only when the user who applied it last holds at least
`auto_build_min_permission` (`write` by default; a bot never counts), the same bar as `factory-build`; a label applied
by anyone else blocks the issue (`label-not-authorised`). Then `allowed_models` bounds the model
(default: the built-in ids plus the ids the repository itself configures, an explicit list is exact) and
`max_effort` bounds the effort (default: the highest effort the built-in table and the repository's own `effort` /
`[efforts]` use, so `high` unless the repository raises it). A label that is not authorised, unknown, not allowed, above the cap,
or a second one of its family (two `factory-model:` labels, or two `factory-effort:` labels) does **not** fall back to a default: the issue gets
a `blocked` disposition with a fixed reason, no job and no worker, and the issue comment of "Comment on non-building
outcomes" says so (the label text is never quoted); fix the labels and re-apply `factory-build`. If the planner groups
issues with different choices into one job, those issues (and the jobs that depend on them) are blocked the same
way, and the planner is told to keep them in separate jobs. The resolved model and effort, and where each came from
(`built-in`, `repository`, `repository-phase`, `issue`), are resolved once at planning time and recorded per job in
`plan.json` (`choice`, build and verify), so a resumed run uses the same values even if an alias or the config changes
later; `status` prints them (`Models:`) and the pull request has a `## Models` section. The per-phase dollar budgets
and `run_budget_usd` apply unchanged. Who may set what and the cost implication: `docs/operations.md`
("Model and effort"). Extended thinking is not part of this.

### Labels on issues

**`factory-build` starts the work.** The manager watches **every registered repository** (a
`repo_configs` entry; `auto_build = false` opts one out, `FACTORY_MANAGER_TRIGGER_SECONDS=0` turns the
watcher off everywhere). Nobody calls `plan` or `run` by hand in normal operation: when a person with
`write` access (the repository role of whoever applied the label; `auto_build_min_permission`) puts the
label **`factory-build`** on an open issue, the next poll (60 s) plans exactly that issue, **removes the
label once the plan succeeded**, builds it, and opens the pull request; the issue then carries the
`factory:*` status labels below. To build it again, add the label again. The whole lifecycle of a
labelled issue is automatic:

```
factory-build  ->  (plan ok, label consumed)  ->  factory:planned -> factory:building -> factory:in-review
               ->  factory:pr-open  ->  factory:merged        (factory:blocked / factory:ci-failed: needs a human)
```

Re-applying `factory-build` to an issue labelled `factory:blocked`, `factory:ci-failed`, `factory:needs-info` or `factory:awaiting-approval` asks for
**another attempt** (the job is retried while its run exists, otherwise the issue is planned afresh).
An issue the planner only investigated (a decision is needed, nothing is queued) carries `factory:blocked`,
not `factory:planned`, so re-applying `factory-build` once the questions are answered plans it afresh.
Any other `factory:*` label means "in flight or done" and the trigger is ignored, `factory-ignore`
always wins, bots and users below the minimum permission are ignored (the label stays, one log line),
and a failed plan keeps the label and retries with a growing pause (up to an hour). Details, the log
lines and the threat model are in `docs/manager.md` ("Label-triggered builds"). The manager's HTTP
API (`plan`, `run`) stays as the break-glass path, and `factory plan --issues 12,15` plans exactly
those issues by hand (a closed, missing, ignored or pull-request number is dropped with one line on
stderr; it cannot be combined with `--limit`).

A plain `plan` takes every open issue (up to `--limit`) **except those labelled `factory-ignore`**
(case-insensitive, surrounding spaces ignored): add that label to an issue to opt it out. Factory lists
the open issues as before and drops the ignored ones, fetching more until `--limit` counts only
issues that remain, so an ignored issue never reaches `issues.json`, `plan.json` or a
disposition, and never gets a status label. The label is read when `plan` runs, and read **again**
by `run`: an issue labelled `factory-ignore` after the plan was made is not built. The job is parked as
`blocked` just before its build would start (no worktree, no worker; its issue loses any `factory:*`
label when GitHub accepts the write, which it may not during a label cool-down), and remove the label
and `retry` the job to build it. A label added while the job is already
building makes the pull request say `Refs #N` instead of `Closes #N`, so merging it does not close the
issue. If GitHub cannot be read just before the build the job goes on (a label never stops a job on a
guess); if it cannot be read when the pull request is published, that issue gets `Refs #N` as well and the
body says it could not be checked, because closing an issue cannot be undone. This look is a read, so it does not wait for the label cool-down below: an App that cannot write labels
still has the opt-out honoured.

**Comment on non-building outcomes.** When an issue reaches an outcome that needs a human (the plan marked it
`blocked`, or `investigation` because a decision is needed, or its job ended `failed`, `blocked` or `needs_human_publish`; an `interrupted` job, one whose supervisor was killed before it recorded the end, does not comment, but one that a `pause` or an undrained shutdown ended `failed` does), Factory
posts ONE comment on it from the Factory App, from a fixed template: the outcome, a sanitised reason in a fenced code
block (for an investigation, its numbered questions), what would unblock it, the run name, and, for a blocked or failed
or investigated issue, a footer saying to re-apply the `factory-build` label. A hidden marker per (issue, outcome kind) makes it idempotent: an unchanged outcome posts nothing,
a changed reason edits the same comment, and a restart never duplicates it. The reason is model output derived from
untrusted issue text, so it is capped and stripped of `@mentions`, HTML, control characters, foreign links, secret-like
values and paths outside the repository, and when in doubt the comment carries only the template and the disposition
code. It is best effort like the labels (one line `factory: issue comment failed (<exception class>)` per process, never
a failed or delayed job). It needs no new permission: `issues: write` covers it. `comment_on_blocked = false` in a
repository's config, or `FACTORY_ISSUE_COMMENTS=off` in the environment, switches it off, and
`FACTORY_ISSUE_COMMENTS_PER_CYCLE` (default 5) caps the comments one supervisor invocation writes. Threat model:
`docs/operations.md`, "Comments on issues".

**Epic mode (on by default).** An issue that is too large for one change but can be split is no longer a dead-end `blocked`: the planner returns an
`epic` and Factory posts ONE comment on the issue (fixed template, hidden marker, edited in place) with up to 8 phases (each independently shippable: objective, paths, acceptance, size, order) and up to 10
numbered questions, then **waits for a person**: `factory:needs-info` until someone with write access (`auto_build_min_permission`) comments, then `factory:awaiting-approval` until someone with that
permission applies the `factory-approve-plan` label, then `factory:plan-approved`. Re-apply `factory-build` after answering to have the plan redone. The words of a reply are never read, bots and Factory's own comments never
count, and anything unclear keeps the issue where it is. `epic_mode = false` (per repository) or `FACTORY_EPIC_MODE=off` (deployment-wide) keeps today's `blocked`, and so does a repository where Factory cannot comment
(`comment_on_blocked = false`) or that opted out of label triggers (`auto_build = false`: no gate pass would run). **After the approval (the phase delivery)** Factory opens the phases as issues, one at a time, and keeps one status comment on the parent (checklist and ledger): phase 1 right after the approval, phase k+1 only when phase k's pull request is merged, each labelled `factory:epic-phase`; from the next pass the manager starts the build of the open phase issue itself (the approval is the only authority and is proven again from GitHub when the plan starts and again right before its build, so removing an approval label, `factory-ignore`, closing the parent or any kill switch also stops the start of a build whose plan is already made, and the same proof stands before a restart's auto-resume or a dependency resume of that run (a `run` that is already running, and what a person does by hand, are not stopped by them); Factory never applies the trigger label; at most one start per repository per pass, held while `epic_max_open_prs` pull requests of Factory are open; a started phase with no pull request and no run in progress is reported stalled and its parent moves to `factory:blocked`; a person re-applies the trigger label to the parent to re-plan), and the approval is judged again on every pass. Recommended for a repository that uses epic mode: at least one approving review in its ruleset, since an approved epic is as autonomous as that ruleset. A phase's pull request says `Closes #<phase issue>, refs #<parent>`; the last phase's also says `closes #<parent>`, but only when that is proven live at publish and at every completion (parent open, not `factory-ignore`, the approval still stands, the phase issue is Factory's own for the current plan, every earlier phase merged), and anything that cannot be read says `refs`. A phase issue is never planned as an epic of its own (it is blocked with a fixed reason). Not done yet: no live run on a real deployment (the automatic start is tested against fakes only), and a halted epic (a phase issue closed without a merged pull request, deleted or duplicated) is reported but not relabelled and nothing resumes it by itself. An epic is as autonomous as the repository's ruleset: with `required_approving_review_count: 0` (the shipped `handoff/ruleset.json`) phase pull requests can merge on passing checks alone, so require an approving review where epic mode is used.
Because it is on by default, issues the planner used to mark `blocked` as too large now become epics on deploy. State machine, threat model and switches: `docs/operations.md`, "Epic mode"; the pass: `docs/manager.md`, "6. Epic gate".

**Issues opened by Factory.** A builder or a reviewer may report what it noticed outside its own job or could not
fix (a bug in a neighbouring file, a flaky test, a missing tool in the sandbox, a follow-up the issue implies) as
`observations` in its result, and the supervisor opens ONE issue for each, from a fixed template, when the job ends:
the title is a fixed prefix and bounded plain text, the model-written detail sits in a single code block (nothing in
it links, mentions or renders, and secret shapes are redacted), and the issue links the source issue, the pull request
(when there is one), the run and the job. The new issue carries the label **`factory:observed`** and **never
`factory-build`**, and it has no assignee: a person reads it and applies `factory-build` if it is worth building
(`factory:observed` still lets that label start a plan), or closes it. A plain `plan` leaves such an issue out until
the trigger label is on it. Observations about the pull request's own defects stay review findings, not issues; an
observation is not added to the pull request body. The same observation (a fingerprint of its title and paths, in a
hidden marker, found only on issues Factory itself authored) is never opened twice, **closed issues included**, so a
won't-fix stays closed. **It is ON by default and has no cap on the number of issues:** a poisoned or flooded
repository is silenced with `observed_issues = false` in its config or `FACTORY_OBSERVED_ISSUES=off` in the
environment, and an issue labelled `factory-ignore` never gets one. It is best effort like the comments (one line
`factory: observed issue failed (<exception class>)` per process, never a failed or delayed job) and needs no new
permission (`issues: write`). Threat model: `docs/operations.md`, "Issues opened by Factory".

Factory shows each issue's status with **one `factory:*` label at a time**, created on demand
and replaced as the job moves on: `factory:planned`, `factory:building`, `factory:in-review`,
`factory:pr-open`, `factory:ci-failed`, `factory:merged`, `factory:blocked`, and for an epic plan `factory:needs-info`,
`factory:awaiting-approval`, `factory:plan-approved` (the exact mapping
from job states is in `docs/handoff.md`; `factory:planned` means a job is queued for the issue,
an issue the planner only investigated gets `factory:blocked`, and `covered` issues get none). They are best effort: a
failure is swallowed after one line `factory: issue label sync failed (<exception class>)` on
stderr, and no job decision reads them (only the label watcher does, to leave an issue that carries one alone).
After three failed writes in a row the labeller rests for five minutes (a fixed cool-down), keeps the
updates that arrive meanwhile and writes them with the next update once the rest is over (nothing
wakes it on a timer, so a run that ends first leaves them to the next `run`'s reconcile); if the
first write after the rest fails it rests again at once, and a lost repository lock stops it for good. `purge` (so the janitor's
purge of a finished or abandoned run too) releases the labels of the run's issues once the run is
gone: a pull request that was merged leaves `factory:merged`, one that is still open keeps its
label, every other issue loses its `factory:*` label, except an issue that another run's plan
lists. The Factory App needs `issues: write` for this and for the outcome comment below, and for nothing
else; Factory never closes an issue or edits its title or body. **The installation must accept
`issues: write` before an image with this change is deployed**: the token request includes it,
and GitHub refuses the whole request otherwise (see `docs/operations.md`). `status` only reads
and never writes a label. To add a repository to an existing setup, use
`scripts/register-repo.sh` (manual merge: a human merges Factory's ready pull requests) or
`scripts/enroll-repo.sh` (hand-off to GitHub auto-merge); [enrolling-repositories.md](enrolling-repositories.md)
says which to choose (the hand-off ruleset blocks human pull requests into the default branch).


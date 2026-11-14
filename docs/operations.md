# Operating Factory

Day-to-day operation of a deployed Factory (the manager as a single-replica Deployment on AKS, `infra/`).
Everything here is also possible with the CLI on any machine with the same configuration; the
manager is only the HTTP front of the same commands (`docs/manager.md` has every endpoint,
status code and environment variable). The merge side is `docs/handoff.md`.

All examples use `$M` for the manager's base URL (the public HTTPS name of the ingress,
`https://<<DNS_LABEL>>.<<AZ_LOCATION>>.cloudapp.azure.com`) and a bearer token for an Entra principal holding
`Factory.Operator` (mutations) or `Factory.Viewer` (reads). `ORG/REPO` is a repository that has a
config file, `RUN` a run name (`[a-zA-Z0-9][a-zA-Z0-9_-]{0,49}`).

```sh
TOKEN=$(az account get-access-token --resource api://<<ENTRA_APP_CLIENT_ID>> --query accessToken -o tsv)
api() { curl -sS -H "Authorization: Bearer $TOKEN" "$@"; }
```

There is no Easy Auth on AKS: the manager validates the bearer token itself (`FACTORY_MANAGER_AUTH=entra-jwt`; `docs/manager.md`).
It accepts only a v2.0 Entra access token (RS256) for the configured tenant and audience, so the app registration must issue v2
tokens, and it ignores any `X-MS-CLIENT-PRINCIPAL` header. Every request needs the token, except the three static files of the dashboard page (`/ui/`, below); **the public URL answers `401` without
one for every API path, `/healthz` included** (`/healthz` is open only to loopback, which is what the pod's exec probes use). So an external uptime
check must send a token.

## Reaching the pod and the cluster

Where an Azure Container Apps runbook would use `az containerapp exec`, use `kubectl exec`. The cluster uses Microsoft Entra ID with Azure RBAC for Kubernetes and has
no local accounts, so you need an Azure role that allows `pods/exec` in the `factory` namespace (for example *Azure Kubernetes
Service RBAC Writer* scoped to that namespace) and `kubelogin`:

```sh
az aks get-credentials --resource-group <<RESOURCE_GROUP>> --name <<AKS_CLUSTER>>
kubelogin convert-kubeconfig -l azurecli
kubectl -n factory get deploy,pod,pvc
kubectl -n factory exec -it deploy/factory -- /usr/local/bin/factory-entrypoint bash   # a shell as the factory identity (uid 10001)
kubectl -n factory logs deploy/factory --tail=100
```

Go through `factory-entrypoint`: a plain `kubectl exec ... bash` is root inside the pod's user namespace (an unprivileged id on the
node) with the container's default capabilities instead of the supervisor's three ambient ones, and anything it creates under `/data` would be owned by root and unusable
by the supervisor and the workers. Check with `id` and `grep Cap /proc/self/status` (`CapAmb: 00000000000000e0`). `factory-sandbox-check`
and the CLI commands in `docs/architecture.md` ("Run") work from that shell. The CLI there is the same one the manager shells out to; prefer the API
for anything the manager offers, because the audit lines are written there.

Roll-outs are Deployments, managed by Terraform (a change of image digest, secret version or repository configuration is an
apply). To watch one: `kubectl -n factory rollout status deploy/factory` and `kubectl -n factory get pods -w`; `kubectl -n factory
describe pod ...` shows probe failures and volume attach errors. Do not `kubectl rollout undo` or `kubectl edit` the Deployment: Terraform
would revert it or drift from it. Roll back by a pull request that sets the previous `image_digest` (or secret version) and an apply,
after a `drain`. The ingress is a separate Deployment in namespace `ingress` (`kubectl -n ingress get deploy,pod,svc`, then `kubectl -n ingress logs deploy/<name>`).

## Start a run

**Normally you do not.** The manager watches every registered repository: an issue that a person with `write` access labels
**`factory-build`** is planned (exactly that issue), built, reviewed, verified and published as a pull request, with no API call. The
label is consumed once the plan succeeded and the issue then carries the `factory:*` status labels (`docs/handoff.md`); re-applying the
label to a `factory:blocked`, `factory:ci-failed`, `factory:observed` or `factory:epic-phase` issue asks for another attempt (the last two are issues Factory opened itself, for a worker's observation or for a phase of an approved epic: they are planned afresh). What it does, how it decides, the log lines and the
threat model are in `docs/manager.md`, "Label-triggered builds". To watch it work:
`kubectl -n factory logs deploy/factory | grep '^trigger '` (lines such as `trigger repo=ORG/REPO issues=1 run=auto-20270115T080000Z planning`, then `started`).
While a plan is going for the repository, or its lease is held by something the manager did not start, it waits (`skipped reason=busy`). While a run (supervisor) of this manager is
going it does not wait for it to finish: an issue labelled meanwhile is **topped up** into that run (`plan --issues N --append-to <run>`, logged `trigger repo=... issues=1 run=... appending`) when a build slot is free
(active build workers < `auto_build_concurrency`, at most `auto_build_max_issues` issues); the label is removed once the fragment is in the run's `inbox/`. When it cannot, it logs `skipped reason=slots_full` (no free slot),
`run_full` (the run is at its job cap, waiting fragments included), `budget` (`run_budget_usd` reached) or `draining` (the supervisor has a drain request) and the label waits for a later pass. Otherwise it ignores an issue
labelled `factory-ignore` or already carrying a `factory:*` status label, ignores a label applied by anyone below the repository's
`auto_build_min_permission` (default `write`) or by a bot (the label stays), and after a failed plan it keeps the label and retries
after 1, 2, 4 ... up to 60 minutes. Turn it off per repository with `auto_build = false` in its config, for the whole deployment with
`FACTORY_MANAGER_TRIGGER_SECONDS=0` (then use the calls below).

The HTTP API remains the **break-glass path**, and is also how you plan a chosen set of issues or run something the labels cannot express.
A run has two steps, and both return at once (`202`) because they last minutes to hours:

```sh
api -X POST "$M/repos/ORG/REPO/runs/RUN/plan"  -d '{"limit": 30}'    # reads open issues (except factory-ignore), writes plan.json
api -X POST "$M/repos/ORG/REPO/runs/RUN/plan"  -d '{"issues": [12, 15]}'   # or exactly these issues (CLI: plan --issues 12,15); not with "limit"
api -X POST "$M/repos/ORG/REPO/runs/RUN/run"   -d '{"concurrency": 2}' # builds, reviews, verifies, publishes PRs
```

One plan or run per **repository** at a time (`409` if another is running or the repository lease is
held elsewhere; the label watcher obeys the same rule and waits). The lease is the single-supervisor guarantee: a blob lease with a fencing token, held
by the supervisor process for its whole life. If it is lost, the supervisor stops its workers.
Factory stops at a ready pull request and the `factory/gate` status; `docs/handoff.md` covers the
rest. The run's logs (`plan.log`, `supervisor.log`) stay in the run directory on the data disk (`/data`, an Azure Disk) and
are served by the API only to an Operator, redacted and in bounded ranges (`docs/manager.md`, "Log endpoints"), because they can contain
agent-authored text; a redacted copy outlives the run (see "Logs"). What the API serves about a purged run is the history
ledger (below, "History"), which holds only ids, statuses, numbers and costs.

## Opting an issue out, and the status labels

`plan` skips every open issue labelled `factory-ignore` (case-insensitive); the issue never appears in `plan.json`, in a
disposition or in the planner's prompt. For the label watcher `factory-ignore` always wins over `factory-build`. An issue
labelled **after** `plan` stays in `plan.json`, but `run` looks at its labels again before it builds the job: the job is parked as
`blocked` (error `issue #N was labelled factory-ignore after planning; not built`, no worktree, no worker, and the issue's
`factory:*` label is removed), and `retry` builds it once the label is gone (the retry needs a `--reason` because the attempt was
counted). A label that appears while the job is building makes the pull request say `Refs #N` instead of `Closes #N`, so
merging does not close that issue. If GitHub cannot be read at either moment the job goes on, as it would without the
check. The look is a read: it goes on while the label writes rest (an App without `issues: write` refuses every write
but still answers reads), and only three failed reads in a row pause it, for five minutes. Nothing is written to the issue besides its `factory:*` label and the outcome comment (next section); the one other thing Factory writes to GitHub about its work is a NEW issue for what a worker observed ("Issues opened by Factory", below), which it never labels, comments on or edits afterwards.

Factory keeps one `factory:*` status label on each issue it works on (`planned`, `building`, `in-review`, `pr-open`,
`ci-failed`, `merged`, `blocked`; see `docs/handoff.md`), written by the supervisor (`run`, `retry`, the recovery commands and
`recover`, all under the repository lock), and closes no issue; the only thing it writes besides labels is the outcome comment below. `status` only reads and writes no label,
so the Viewer role cannot cause writes. `run` re-derives every label from the persisted state when it starts, so a
missed update heals, and so does the `factory:blocked` of a job that `recover` left `interrupted` when the label could not
be written then. After three failed writes in a row the labeller rests for 300 seconds (not configurable): updates made meanwhile are
kept, the latest per issue, and written with the next update once the rest is over (nothing wakes it on a timer: a run
that ends first leaves them to the next `run`'s reconcile); its first write after the rest is a probe, and one more failure rests it
again at once. A lost repository lock stops it for good. `purge` releases the labels of the run's issues (next section).

What the labels that can be mistaken for one another mean, one sentence each:

* `factory:planned`: a job is queued for the issue and has not started; it never means the planner only looked at the issue, so counting
  `factory:planned` issues counts the queued work.
* `factory:blocked`: the issue needs a human, because the plan marked it `blocked`, or the planner only investigated it and needs a decision
  (nothing is queued; the outcome comment carries the questions), or its job is `blocked`, `failed`, `interrupted` or `needs_human_publish`, or its
  pull request was closed unmerged.
* `factory:needs-info`, `factory:awaiting-approval`, `factory:plan-approved`: the gate of an `epic` plan (a too-large issue the planner split into phases): questions
  waiting for an answer, a plan waiting for a person to approve it, a plan approved. Only the manager's gate pass moves them, not `reconcile` and not `purge`;
  the human label `factory-approve-plan` is the approval. See "Epic mode" below.
* An issue the plan marked `covered` gets no `factory:*` label.

Both plan dispositions without a job (`blocked`, `investigation`) get `factory:blocked`, when the plan is made (`sync_plan`), when `run` starts
(`reconcile`, which also replaces a `factory:planned` that an older version put on an investigated issue) and when a plan fragment adds them.
Re-applying the trigger label to a `factory:blocked` issue asks for another attempt (`docs/manager.md`, "Label-triggered builds"): for an issue
whose plan only investigated it, no job exists, so the issue is planned afresh; answer the questions first.

**Roll-out order matters.** The token request now includes `issues: write` (and `actions: read`). GitHub refuses a token
request that asks for a permission the installation has not accepted (the broker sees `422`, "GitHub rejected the requested
repository or permissions"), and there is no fallback to a narrower set: until an organisation owner has **accepted the new
permissions on the installation**, no job can mint a token, so nothing can push, open a pull request or post `factory/gate`.
Accept them (GitHub: organisation settings, Installed GitHub Apps, the Factory App, "Review request") **before** deploying
an image that contains this change, and again for every repository registered later (`docs/enrolling-repositories.md`).
If the order was missed, it shows at once instead of at the first job: the manager mints a token for every configured
repository when it starts (and again every 5 minutes), `GET /repos` shows `"github": "permission missing: actions, issues"` for
each repository whose installation lacks them, the log has the line `github repo=ORG/REPO status=permission missing: actions, issues`,
and a job's broker error says `token_broker: permission missing: actions, issues (HTTP 422: ...)`. The names are exactly the
permissions to accept (the App's own permission settings must list them too). Once accepted, the entry turns `ok` within
5 minutes with no restart. Check `api "$M/repos"` shows `"github": "ok"` for every repository before you start a run.
Once accepted, a label failure (403, 404, a timeout) never stops a run: the supervisor log has one line
`factory: issue label sync failed (CalledProcessError)`.

## Retry or re-plan a blocked issue

Re-applying the trigger label (`factory-build`, removed and added again) to an issue that carries only `factory:blocked`, `factory:ci-failed`,
`factory:needs-info`, `factory:awaiting-approval` and/or `factory:plan-approved` (an approved epic, only while none of its phases is in flight) asks for another attempt, and Factory does one of two different things (`docs/manager.md`, "Retry"):

| why the issue is blocked | what re-applying the label does |
|---|---|
| a job failed, was blocked for another reason, or its CI failed | **retries** the old job: `retry` in its old run with its old plan, then `run` (`started ... retry=yes`) |
| a job was blocked because a dependency was **closed without merging** (reason `dependency X was closed without merging`: the dependency's job is closed or its pull request was closed unmerged) | **re-plans**: the old job is not retried (its plan still holds the dependency, so it would only be blocked again); the issue is planned again in a new run against the current `main`, where the dependency no longer exists |
| a dependency is still open | the job resumes by itself once the dependency has merged (the dependency-merge resume); a retry also works |
| the newest plan only investigated or blocked the issue (no job) | **re-plans** once the questions are answered |
| the newest plan called the issue an `epic` (`factory:needs-info` or `factory:awaiting-approval`) | **re-plans**: the answers are folded into a new plan and the plan comment is edited in place (see "Epic mode"); `factory:plan-approved` is not re-planned |

In the log, a retry handed to a fresh plan is `trigger repo=... issue=N retry replanned reason=dependency-closed`, a retry the CLI refuses is
`... retry refused issues=N backoff=60s reason=cli-refused` (the label stays and that issue is paused), and a queued job blocked on a closed
dependency is `... job=J blocked dependency=D reason=dependency-closed`.

## Comments on issues

Factory posts ONE comment on an issue when it reaches an outcome that needs a human and it will not build the issue
(`src/factory/issue_comments.py`, called from the same persisted-state hooks as the status labels, with the same `gh`,
fence and best-effort rules):

| trigger | outcome in the comment | code |
|---|---|---|
| plan disposition `blocked` (`sync_plan` after `plan`, `reconcile` at the start of every `run`, an ingested fragment) | blocked | `blocked` |
| plan disposition `investigation` | needs a decision (the planner's questions, numbered) | `investigation` |
| job status `failed` | failed | `failed` |
| job status `blocked` (also the `block` command) | blocked | `blocked` |
| job status `needs_human_publish` (finished work GitHub refused: workflow files) | built and checked, but a person has to publish it | `needs_human_publish` |
| a plan hold starts (the same `plan` failure code twice in a row for an issue set, `docs/manager.md` "Plan holds"), one comment on each issue of the set | planning is on hold: edit the issue or change its labels (fixed text, no reason) | the `plan_failure` code, e.g. `schema_invalid` (`kind=plan_held`) |

A job that is `interrupted` does **not** comment: a supervisor that is killed before it records the end of its jobs (node loss, a pod killed
without its grace period, the shutdown grace running out) leaves them `interrupted`, and they are resumed a moment later (auto resume, or a
person), so a public "failed" comment would land on every in-flight issue and never be retracted. Its label (`factory:blocked`) already says
it needs attention; a job that ends `failed` or `blocked` after that comments. A graceful stop is not that case: a `pause` or an undrained
shutdown makes the supervisor terminate its live workers, so those jobs end `failed` (`agent exited -15`; "Drain, pause, and what each leaves
behind") and the outcome comment is attempted for them like for any failed job (best effort: the process is on its way out), once per issue
and outcome kind. `drain` before a planned rollout, so that no in-flight issue gets it.

Not covered yet (follow-ups of the issue): trigger-side outcomes (a label ignored because its applier lacks permission, a
refused retry), the CI give-up comment, and a distinct "needs a decision" status label (an investigation shows `factory:blocked`
like the other issues that need a human; the comment says what is needed). A comment is not removed or edited when the job later moves on
(a retry that succeeds leaves the old comment; the next failure edits it).

**Template** (fixed text; only the reason is model-derived): a hidden first line
`<!-- factory-outcome:v1 issue=N kind=blocked|needs_decision|failed -->`, the headline `Factory: blocked` (`needs a decision`,
`failed`), the sanitised reason inside **one fenced code block** (for an investigation: the context sentences, then the questions numbered
inside the block), `What would unblock it:` (one fixed sentence per kind), `Run: <run name> · Code: <disposition or status>`, and a
footer. For a blocked, failed or investigated issue the footer says to re-apply the trigger label (`factory-build` unless `auto_build_label` says
otherwise): those issues carry `factory:blocked`, which the watcher takes as a request for another attempt, and for an investigation (no job
owns it) that means planning it afresh once the questions are answered. A finished job waiting for a person to publish it gets no such
sentence, because a rebuild would hit the same rule. The footer follows the cause of a `blocked` job, because the same label does different things
(see "Retry or re-plan a blocked issue"): for **a dependency closed without merging** it says that retrying the old plan cannot help and that
re-applying the label plans the issue again in a new run against the current `main`; for **any other blocked or failed cause** (a dependency
that is still open included: free text is not classified) it says that re-applying the label asks Factory to try again (a retry). The cause is read
from the raw reason, not from the sanitised text. When a re-plan takes an issue into a new run, the old run's reconcile leaves that issue's label
and comment to the new run. If a second run still holds a live job for the issue, the stale blocked job is ignored and that one is retried.

**Idempotence.** Before writing, the supervisor lists the issue's comments and looks for one written by Factory's own identity (the App's bot user id, or the ambient `gh` login; if that cannot be
determined a new comment is posted rather than editing an unverified one) whose body starts
with the exact marker of this (issue, kind). Same text: nothing is written. Other text (a changed reason, another run): that
comment is edited in place. None: one is posted. A process remembers what it wrote, so an unchanged outcome costs no call after the
first; a restart (new process, same persisted state) finds the comment by its marker and does not duplicate it. Two
outcome kinds on one issue (say blocked, later failed) are two comments, one each. A person's comment that quotes the marker is never
touched, and neither is another App's or `github-actions[bot]`'s (the author must be Factory itself).

**Switches and limits.** `comment_on_blocked = false` in a repository's config suppresses every comment for it (and with it epic mode: the planner's `epic` is then downgraded to `blocked`, see "Epic mode");
`FACTORY_ISSUE_COMMENTS=off` (also `0`, `false`, `no`, `disabled`) in the supervisor's environment (the manager passes its own on to the
supervisors it starts) suppresses them everywhere. `FACTORY_ISSUE_COMMENTS_PER_CYCLE` (0 to 100, default 5) is the most comments (posts and
edits) one supervisor invocation writes; outcomes over the cap wait for the next `run`'s reconcile. The one exception is the comment a plan
hold starts with (see "The comment a hold starts with" in `manager.md`): a positive cap is raised to the size of the held set for that one call,
so every issue of the set gets its comment, and a cap of 0 posts none (as for every other writer). An issue labelled `factory-ignore`
(read just before a write) is never commented on, and neither is a **closed** issue (a maintainer who closed it as won't-fix does not get
a "Factory: blocked" comment on every reconcile). The issue's state is read in the same single `GET` as its labels, each time, so an issue that is reopened
is commented on again at the next reconcile. Neither switch affects labels or jobs; label sync does not depend on the issue state.

**Default: on.** `comment_on_blocked` is on by default. The reason: without the comment a blocked, failed or investigated issue is silent and the reason only lives in the pod's run directory. A repository that wants silence opts out with `comment_on_blocked = false` in its config, or a deployment with `FACTORY_ISSUE_COMMENTS=off`; there is no per-repository allow-list. Flipping the default to off would silence every already-deployed repository until its config said `true`.

**Permissions.** Commenting on an issue is covered by the `issues: write` the App already has (and `pull_requests: write`); GitHub has no narrower
one, so no installation change is needed beyond what the labels required. Writes are the issue comments endpoints only (`POST` a
comment, `PATCH` Factory's own comment); Factory still never closes an issue, edits a title or body, or deletes a comment.

**Failure.** A 403, 404, a timeout or a malformed answer is swallowed after ONE line on stderr per process:
`factory: issue comment failed (<exception class>)`: no issue text, no response body, no token. Three failures in a row rest the
commenter for five minutes (so an unreachable GitHub adds at most three timeouts of 20 seconds to a run), a lost repository lock stops it for
good. Comment failures never fail or change a job, and never count toward the label writer's failure streak.

### Threat model: posting model-derived text

The reason is written by a model that has read untrusted issue text (and, for a failed job, its error, which can quote tool output).
Posting it makes Factory's GitHub App identity say it, in front of the issue's readers and notification lists.

| threat | what stops it |
|---|---|
| Prompt-injected text makes Factory ping people or teams (`@user`, `@org/team`) | every `@` that begins a mention is replaced by a fullwidth `＠`; nobody is notified |
| Phishing or tracking links, images (GitHub fetches an image), autolinks, reference-style links | **the reason is rendered inside a fenced code block, where GitHub renders nothing**: no link, image, autolink, `#N` reference or notification can come out of it, whatever Markdown syntax the text uses (including schemeless, nested and reference-style forms that a pattern cannot list). Beyond that, defence in depth: URLs (also with a word character in front, such as `_http://x`), `[x](y)` links and images are removed, only a `#N` for an issue or pull request of this plan stays a reference, any other `#N`, `owner/repo#N` and `GH-N` is broken |
| Raw HTML (scripts, iframes, hidden comments), a forged marker that would make Factory edit or hide another comment | HTML entities are decoded, then comments and tags are stripped, then any remaining `<` and `>` are escaped; the reason is whitespace-collapsed to one line per item and sits inside the code block (backticks inside it are replaced, so it cannot close the fence), so no heading, list, table or marker can start a line in the rendered comment; a text that is mostly markup is dropped |
| Terminal escapes, NUL, bidi overrides and zero-width characters | every control and format character is removed; line breaks and tabs become spaces |
| Leaking a secret or an environment value that the model or a tool echoed | token and key shapes (GitHub, Anthropic-style `sk-`, AWS, Slack, JWT, bearer, private-key blocks), `NAME=value` and `token|secret|password|api key: value` assignments and long mixed-alphanumeric strings are redacted; the literal values of the supervisor's secret-looking environment variables and of the GitHub token in use are redacted wherever they appear |
| Content from outside the repository (host paths such as `/etc/...`, `/home/...`, `~/...`, `../`) | those paths are replaced by `[path removed]`; repository-relative paths stay |
| Flooding an issue or the notification stream, or a restart storm | at most one comment per (issue, outcome kind), edited in place; no comment for `interrupted` jobs (a supervisor killed before it records the end leaves its running jobs so); a pause or an undrained shutdown ends a live job `failed`, which does comment, so `drain` before a planned restart; at most `FACTORY_ISSUE_COMMENTS_PER_CYCLE` writes per invocation (a plan-hold comment raises a positive cap to the held set's size for that one call; 0 posts none); a length cap of 900 characters (questions: 5, 300 each); a cool-down after three failures |
| Impersonating or hijacking Factory's own comment | only a comment by Factory's own identity that starts with the exact marker is edited: the App's bot user id (type `Bot` and id); with an ambient `gh` login (development only) that login, which is usually a person. A comment by anyone else, including one that quotes the marker, is never touched (the App can edit any comment, so this check is Factory's own, not GitHub's) |
| The comment text steers Factory later (second-order injection) | the planner reads issue comments, so **Factory's own outcome comments are removed from the issue snapshot it reads** (`without_factory_comments`: a comment that starts with the marker; the most a person gains by forging the marker is hiding their own comment from the planner). Beyond that, no job, run, retry or merge decision depends on a comment; the trigger is still the label alone |
| Attacker-sized text stalling the supervisor (a regular-expression blow-up) | every pattern is bounded (a name part of at most 40 characters, a scheme of at most 30), the input is capped at 20,000 characters, and an unchanged outcome is not sanitised again (the raw inputs are digested; the secret lookup, which can call the token broker, happens only when the raw inputs of an outcome changed, not on every announce) |
| A doubtful reason | when nothing is left after cleaning, too little survives, too much was redacted (more than three placeholders), or the input is not text, the reason is dropped and the comment is the template plus the disposition code only |

Residual risk: the sanitiser is a filter, not a proof. Plain-text social engineering ("run this command", "ask X for access") that survives is still
visible to anyone who can read the issue, and a secret in a shape the patterns do not know would be posted; the comment is
therefore worded as Factory's own report, the reason sits in a quote, and the whole feature can be switched off per repository or globally.

## Issues opened by Factory

A builder or a reviewer often notices something that is not part of its job, or that it cannot fix from where it is: a bug in a neighbouring file,
a flaky test, a missing tool in the worker sandbox (`jq`, `ps`, exec bits), a change the App may not push (workflow files), a session that ran out
of time, a follow-up the issue implies. That used to die in a summary or a log. A worker may now report it as `observations` in its result, and the
supervisor opens ONE issue for each, from a fixed template, **when the job ends**. The worker never talks to GitHub: it only reports, and the
supervisor decides. The code is `src/factory/observed_issues.py`; the prompts are `builder.md` and `reviewer.md`.

**What is never opened: limits of Factory's own sandbox.** A worker that reports kind `environment` (no `node`, `uv` or `ruff` in the sandbox, a
read-only filesystem, tests it could not run there) describes Factory's worker environment, not a defect of the repository the job targets, and the
repository's maintainers cannot act on it. The supervisor drops such an item with the fixed line `factory: observation dropped job=<id> reason=factory_scope`
(the schema still accepts the kind, so a worker that reports it does not fail its result, and an item an older supervisor stored is not opened either), and
the worker prompts tell workers to state a sandbox limit in `tests` instead.

**ON by default, with no cap.** `observed_issues` defaults to `true` per repository. It stops with `observed_issues = false` in the repository's
config, with `FACTORY_OBSERVED_ISSUES=off` (also `0`, `false`, `no`, `disabled`) in the supervisor's environment (the manager passes its own on to the
supervisors it starts), and for an issue labelled `factory-ignore` (read just before the write; an issue that cannot be read counts as opted out for
that moment, and the observations stay in the job's state for the next reconcile; an issue that does carry the label settles its job's observations for
good: they are not opened later if the label is removed). A run with no configured repository never opens issues: the target is
always the configured `repo`, never a guess from a checkout. `FACTORY_ISSUE_COMMENTS=off` and `comment_on_blocked` are the commenter's switches and
do not apply here. **There is no limit on how many issues are opened** (no per-job, per-run or per-day cap): the only bounds are the dedupe below, the
schema bound on how many observations ONE worker result may carry (at most 5, each with a title of 12 to 100 characters, a detail of at most 1000, and
at most 5 paths; this is the shape of a result, not a cap on issues), the two switches and the failure cool-down. A job can therefore open several issues
(a builder result and one reviewer result per round), and a repository many over time. Read the threat model below before leaving it on for a repository
whose issues strangers can write.

**Who may report, and what is not an observation.** Builders and reviewers (not the planner and not the verifier). A defect in the pull request's own diff
is a review finding (the reviewer) or a fix (the builder), never an observation: the prompts say so, and an observation never changes whether a review
passes. The observation is **not added to the pull request body**; the issue Factory opens links to where it was found instead.

**What opens, and when.** The validated observations are kept in the job's persisted state (`observed`) and opened when the job reaches `pr_ready`,
`pr_incomplete`, `blocked`, `failed`, `needs_human_publish`, `merged` or `closed` (a blocked builder, the "cannot fix" case, has no pull request: its
observations ride on the same state update as its status), from the same hooks as the labels and the comments (`State.update`, and `reconcile` at
every `run` and in the manager's idle reconcile). A flush that GitHub refused is asked for again: the run directory holds `observed.json`, which records
per job the fingerprints that have their issue and those dismissed on purpose, and the manager's idle reconcile asks for any run with a finished job
(also one that ended `blocked`, `failed` or `needs_human_publish` and has no pull request) that still holds an observation in neither, at most every
five minutes per run, until a flush goes through (it stops by itself when the run is purged, and never runs while `observed_issues` is off for the
repository or `FACTORY_OBSERVED_ISSUES=off` is set). A job that is still running or only `interrupted` opens nothing. An issue that another
run's plan also lists (a second run of the same issue) does not suppress the observation: opening an issue contends for no label, the issue links the one
the job worked on, and the fingerprint keeps two runs from opening it twice. Failures never fail or delay a
job: see "Failure" below.

**Template** (fixed text; only the title's words and the code block are model-derived): the title is `[Factory observed] <title>` with the title bounded
plain text (12 to 100 characters, no backticks); the body starts with the hidden marker `<!-- factory-observed:v1 fp=<32 hex> -->`, then the headline
`Factory observed: <fixed text for the kind>` (the kinds are `out_of_scope`, `cannot_fix`, `bug_found`, `follow_up`, `environment`), a fixed sentence
naming the source issue(s) (`#N`) and the pull request (`#N`, when one exists), saying that it is a worker's own, unchecked report, then the detail and
the paths in **one fenced code block** wrapped at 100 characters, then `Reported by the builder|reviewer of job <job> in run <run> · Kind: <kind>` and one
fixed paragraph on what to do (read it and decide; apply the trigger label yourself to have it built; close it to drop it; Factory never applies that label,
assigns anybody or closes anything on its own). The issue numbers, the pull request number, the run name and the job id are values Factory produced and
validated (a name that is not a plain name prints `unknown`), never text from the worker.

**Never more work by itself.** The new issue gets the label **`factory:observed`** and **never the trigger label** (`factory-build`), and it has no
assignee. A model-written issue that started a build would be an injection amplification loop (an attacker's issue text steers a model to open an
issue that starts another build); here a person reads it and applies `factory-build` if it is worth doing. `factory:observed` is an *origin* label, not a
status label (`docs/handoff.md`): it is never removed by the status label writer, it stays through triage, build and close, and it is what finds the issue
again for the dedupe below. For the label watcher it is one of the labels that still allows a request: `factory-build` on an issue that carries only
`factory:observed` (and/or `factory:blocked`, `factory:ci-failed`) plans it afresh (`docs/manager.md`, "Retry"); once a job holds the issue its
`factory:planned` label makes it in flight like any other. A plain `plan` (every open issue) leaves out an issue that carries `factory:observed` and not
the trigger label (`issue_labels.drop_unadopted`; `plan --issues N` names an issue a person has chosen already), so a model-written issue reaches the
planner, which treats issue text as untrusted data, only after a person has adopted it. The outcome commenter and the status labeller act only on the
issues of a plan's jobs and dispositions, so the new issue is never a source for them either: nothing is written to it after it is created.

**Idempotence, closed issues included.** Before opening anything the supervisor lists the issues that carry `factory:observed` (state `all`: open AND
closed; one `gh api --paginate` call), keeps only those authored by Factory's own identity (the App's bot user id, or the ambient `gh` login; a pull
request, another App or a person is never trusted) and reads the fingerprint from the marker. The fingerprint is a hash of the normalised (cleaned,
case-folded, whitespace-collapsed) title and the sorted paths; the kind and the detail are not part of it. An observation whose fingerprint is already
there opens nothing, so a restart (a new process, the same persisted state) never duplicates an issue, and **an issue a person closed as won't-fix is not
opened again**. When the list cannot be read or the identity is unknown nothing is opened (fail closed: a restart would otherwise duplicate). A process
remembers what it knows, so a later flush with nothing new costs no call. The dedupe is exact, not semantic: the same problem worded with another title
opens another issue. **Do not remove the `factory:observed` label** from an issue to "adopt" it: the label is the index of this lookup, so an identical
observation would open a new issue. To drop an observation for good, close the issue and leave the label.

**Permissions.** Creating an issue and a label is covered by the `issues: write` the App already has (`tools/token_broker.py`); no installation
change is needed. Writes are `POST .../issues` (title, body and the one label, no assignee, no milestone) and `gh label create factory:observed
--force` (fixed colour and description, so it is idempotent); Factory still never closes an issue, edits another issue's title or body or deletes a
comment.

**Failure.** A 403, 404, a timeout (20 seconds per call, 40 for the list), a malformed answer or an unknown identity is swallowed after ONE line on
stderr per process: `factory: observed issue failed (<exception class>)`; no issue text, no response body, no token. Three failures in a row rest the
writer for five minutes (a probe follows: one more failure rests it again), a lost repository lock stops it for good. The observations stay in the job's
state and are not in `observed.json`, so the manager's idle reconcile (about every five minutes per run, whether or not the run has a pull request) and
every `run` try again. An observation that is not usable is dropped when the result is read, with ONE fixed line per observation and
nothing else: `factory: observation dropped job=<job id> reason=<shape|kind|oversized|title|detail|type|excess|error>`; the job goes on, the other
observations of the result are kept. A path that is not a plain repository-relative path is left out of an observation and the observation stays.

### Threat model: issues opened from worker observations

The input is untrusted: issue text, comments, the repository's files and tool output steer the model that writes an observation, and Factory then
posts it under its own App identity, in a place people read and get notified from, and without anybody having asked for it. With **no cap and the feature
on by default**, the one real defence against volume is the dedupe; the rest limit what one issue can do.

| threat | what stops it |
|---|---|
| Spam or flooding: a poisoned issue or file makes workers report many distinct observations, and every job of a busy repository opens issues | **there is no cap, by decision.** The bounds are: the dedupe (one issue per fingerprint, closed issues included, so a repeated or a rejected observation costs nothing), at most 5 observations per worker result (a schema bound, re-applied by the supervisor), the title (12 to 100 characters) and detail (1000) limits, and the failure cool-down. A repository that is being flooded is silenced with `observed_issues = false` or `FACTORY_OBSERVED_ISSUES=off`; issues already opened are closed by a person (`gh issue list --label factory:observed`) |
| Amplification: injected text steers a model to open an issue that starts another build, which reads that issue and opens more | the new issue never carries the trigger label and has no assignee: a person has to adopt it. The planner's unfiltered listing leaves it out until then, and the observation text is written by a model, so it is read by the planner only after a person applied the label |
| Injected links, images, autolinks, `@mentions`, `#N` references, raw HTML | the detail and the paths sit in **one fenced code block**, where GitHub renders nothing; before that the commenter's sanitiser (`sanitize_reason`) decodes entities, strips comments and tags, removes URLs, links and images, breaks `@mentions`, `#N`, `owner/repo#N` and `GH-N`, and drops a text that is mostly markup. The title is plain text with the same filter and no backticks. The template names only numbers and names Factory produced |
| Secrets in the detail or the title (a tool printed a token, an environment value) | token and key shapes, `NAME=value` assignments, long mixed-alphanumeric strings and the literal secret values of the supervisor's environment and token are redacted (the commenter's patterns); a text with more than three redactions, or nothing left, is dropped |
| Title forgery or impersonation ("[Security advisory] contact ...") | a fixed prefix, bounded plain text with no link or mention, and a body that says in its first sentence that it is a worker's own, unchecked report from an automated job |
| Paths that leak or mislead (`/etc/passwd`, `../`, `~/.ssh`) | only plain repository-relative paths (`A-Z a-z 0-9 _ . + -` segments, no `..`, no leading `/` or `-`) are kept, at most 5, and they too are shown inside the code block |
| Observations about private content appearing in a public repository, or in another repository | the target is always the configured repository (`repos/<repo>/issues`), never another one, and a run with no configured repository opens nothing. The content comes from the repository's own worker, so it is as visible as the pull request would be; an operator who does not want worker text on a public repository's issue list sets `observed_issues = false` for it |
| A poisoned dedupe marker hides a real observation (a person or another bot posts the marker) | the marker is trusted only on issues authored by Factory's own identity (the App's bot user id, or the ambient `gh` login), never on pull requests or on another author's issue; an issue Factory opened cannot be edited by the worker. When the identity is unknown, nothing is opened |
| A person-closed issue is opened again and again | the dedupe lists closed issues too; the label is the index (do not remove it) |
| An observation about something a person opted out of | an issue labelled `factory-ignore` (read just before the write, an unreadable one counts as opted out for that moment) never gets an issue about its job; its observations are dismissed in `observed.json` and stay in the job's state |
| A slow or failing GitHub delays or fails a job | a bounded call (20 s, 40 s for the list), three failures rest the writer for five minutes, nothing is written inside a phase, and every failure is swallowed after one fixed line; the observations are kept for the next reconcile |
| A stale or tampered state file opens something odd | every stored observation is checked again before it is rendered (kind, sizes, paths, and the fingerprint must match its title and paths); one that is not valid is skipped |

Residual risk: the sanitiser is a filter, not a proof, and a model can write a convincing plain-text lie ("this file leaks a key, copy it from ...") that
survives. It is shown in a quote, inside an issue that says it is unchecked and that nothing was done with it, and a person decides what happens next.
With no cap, a determined poisoning of the issues a repository accepts from strangers can still open many distinct issues before somebody turns the
feature off; the dedupe only stops repeats. Set `observed_issues = false` on a repository that takes issues from the public until you have watched the
feature on one you control.

## Follow-ups on pull requests

A person with write access can ask Factory to change one of **its own open pull requests** by commenting on it with the App's mention. Factory
turns that into a session on the pull request's own branch, reviews and verifies the new head, posts `factory/gate` again for that head, and says
on the pull request what became of the request. It is OFF until a repository says `followup = true`. The guards, the outcome codes and every log
line are in `docs/manager.md` ("Follow-up requests"); this section is how to use and operate it.

**How to use it.**

1. Set `followup = true` in the repository's config (on AKS the `repo_configs` entry; changing it is a rollout, see "Where the settings live"). The
   manager must know whom to look for: the App's slug (`FACTORY_GITHUB_APP_SLUG`), or, with no App identity, `mention_handle` (the account name; its
   pull requests are followed and its own comments are ignored).
2. On an **open pull request that Factory opened** (never a closed one, never another author's), comment `@<app slug> <what to change>`. The
   mention must be a whole token outside a quote, a code block or an inline code span. Your role on the repository must reach `followup_min_permission`
   (default `write`).
3. Factory records the request, and the oldest queued request becomes a retry of the job that published the pull request: the builder works on the
   pull request's branch with your comment, the later comments of authorised people and the failing checks' log tails as context, then review,
   verification and publication run on the new head and `factory/gate` is posted again for it.
4. Factory edits **its one reply** on the pull request to say what happened (below). A new head, a changed `factory/gate` and the reply are the visible result.

**Config keys** (per repository, manager only; `docs/configuration.md`):

| Key | Default | What it does |
|---|---|---|
| `followup` | `false` | `true` opts the repository in. Anything but `true` is off. |
| `followup_reply` | `true` | With `followup` on, say on the pull request what became of a settled request. `false` posts nothing (the outcome is still in the log and the state file). |
| `followup_min_permission` | `"write"` | Role the commenter must hold, read again when the session would start. Below `write` is refused for a repository that is not private. |
| `followup_cooldown_seconds` | `300` | One request per user per pull request in this many seconds (0 to 86400). |
| `followup_max_sessions` | `5` | Requests (sessions) per pull request in all (1 to 100). |
| `mention_handle` | `""` | The account to mention when there is no App identity; empty means the App's slug. |

Deployment-wide: `FACTORY_MANAGER_TRIGGER_SECONDS=0` or `FACTORY_MANAGER_OPERATIONS=off` stops the whole pass, `trigger-pause` pauses it,
`FACTORY_ISSUE_COMMENTS=off` (the kill switch of "Comments on issues") stops the reply, and `FACTORY_ISSUE_COMMENTS_PER_CYCLE` bounds its writes
per pass.

**Cursor, cooldown and cap.** The state is one file per pull request, `followups/pr-<N>.json` in the repository's data directory. A **cursor** per comment
kind (conversation comments, review comments, review bodies) is the highest comment id decided, and every comment is decided exactly once, in a
fixed order: a bot or the `mention_handle` account (skipped), no mention (skipped), a role below `followup_min_permission` (skipped), the
`followup_max_sessions` cap (skipped), the per-user cooldown (skipped), else a request is queued. A comment that was edited afterwards never queues
anything, and a comment older than 24 hours when it is first seen is history, not a request. A queued request that has not started within 24 hours
is refused (`expired`). One session runs per repository at a time and never in a repository that is paused, archived, busy or over its
`run_budget_usd` (the original build and every follow-up count together). The decisions are persisted before they are logged, so a crash repeats a
decision and never loses one.

**The reply.** After the manager has settled a request (recorded its outcome, released the lock) it writes ONE comment per pull request from Factory's
own identity: the App's bot user, or the ambient `gh` login. It starts with the hidden marker `<!-- factory-followup:v1 pr=<N> -->`. Later outcomes
**edit that comment in place**; Factory never adds a second one and never edits a comment it did not write (the author must be Factory's own
identity, so a person or another bot that copies the marker is ignored), and a restart finds the comment by its marker instead of posting again.
The text is fixed per outcome code, plus the new head (the first 12 characters of a hex commit id) for `pushed`: no builder summary, no model
text, nothing from a comment, no `@mention`, no HTML, no link, at most 1,500 characters. A body that broke these rules would not be sent.

| Outcome | Replied | The comment says |
|---|---|---|
| `pushed` | yes | a new head was pushed; review, verification and `factory/gate` run again for it; nothing merges before they pass |
| `no-change` | yes | the session made no change; a person has to make anything Factory could not do |
| `session-failed` | yes | the session ended failed or blocked; the operator can see why in the Factory run |
| `session-lost` | yes | the session could not be completed (interrupted too long, or the run is gone) |
| `run-gone` | yes | the run that built the pull request is gone; nothing was started |
| `job-status` | yes | the job cannot take a follow-up (still working, or failed); nothing was started |
| `head-moved` | yes | the head is not the commit Factory built and reviewed; nothing was started |
| `pr-closed` | no | nobody reads a closed pull request's thread; a pull request found closed or merged when the reply is due gets none either |
| `not-authorised` | no | no public answer to someone who may no longer ask. The permission re-check comes after the state checks (`run-gone`, `job-status`, `head-moved`), so a requester who lost access after the request was queued can still be told one of those; the text names nobody |
| `expired`, `cli-refused` | no | Factory's own queue, not a decision about the request |
| (never queued) | no | an unauthorised commenter, the cooldown, the cap: no request, so no reply |

The reply is best effort and is sent from the manager's pass, never from a worker. A 403, a timeout, an identity that cannot be told or a malformed
answer is one line, `followup repo=... pr=N reply failed error=<ExceptionType>`, and changes nothing about the session, the state file or the next
request; the reply is not tried again, and a reply that was lost (a failure, the cap, the cooldown, a restart between the state write and the reply) leaves the previous text on the pull request until the next settled request edits it, so the comment can say an earlier outcome. Three failures in a row rest the replier for five minutes
(`reply skipped reason=cooldown`); more than `FACTORY_ISSUE_COMMENTS_PER_CYCLE` writes in one pass (default 5) are left out
(`reply skipped reason=cap`); a pull request that is not open gets none (`reply skipped reason=closed`). A sent reply is `followup repo=... pr=N replied outcome=<code>`.
Editing a comment sends no notification and the reply contains no `@mention`, so a second follow-up on the same pull request notifies nobody:
the person who asked finds the answer by looking at the pull request.

**Factory's own reply never starts anything.** It is a comment of a bot account (or of the `mention_handle` account), which the pass skips before it
looks at any text (`skipped reason=bot` or `skipped reason=self`), it has no mention in it, and it is never part of a session's context.

**Checking it live** (the sandbox repository; this needs a person and the real App, no test can stand in for it):

1. Enable `followup = true` there and make sure the manager is running with the App identity.
2. Have Factory publish a pull request whose CI check fails on purpose, from the commit Factory itself built (do not push to the pull request's branch by hand first: a head that is not the retained commit is answered `head-moved`, which is what step 5 provokes deliberately).
3. From an account with `write` access, comment `@<app slug> please make the failing check pass`.
4. Expect, within a trigger cycle: `followup ... request queued`, `started run=... job=...`; then a new head on the pull request's branch, a fresh
   `factory/gate` for that head (first `pending`, then `success` or `failure` as usual), `followup ... done outcome=pushed` and `replied outcome=pushed`,
   and exactly one comment from the App that starts with the marker and shows the new head.
5. Comment again so that the outcome differs (for example after pushing to the branch by hand: `head-moved`). Expect the **same** comment to be edited, not a second one.
6. Comment from an account without `write` access: expect `skipped reason=unauthorised` and no reply.

### Threat model: @mention follow-ups

A follow-up lets a comment on a pull request cause Factory to run a builder session. The mechanism has to be safe against what a comment can say, who
can say it and what a session can then do.

| Question | Answer |
|---|---|
| Who can trigger it? | Only someone whose repository role reaches `followup_min_permission` (default `write`), asked from GitHub (`collaborators/<login>/permission`) when the comment is decided **and again when the session would start**; a failing lookup starts nothing and keeps the request. A bot, the `mention_handle` account and a comment that merely quotes or code-fences the mention are never a request. Below `write` is refused for a repository that is not private. Only open pull requests authored by Factory's own identity are read at all. On a public repository this is the only line between a stranger and a build, so keep it at `write` or above |
| Why is a comment untrusted data? | Its author may be anybody who can comment, may have pasted attacker text, and may be wrong. Whether a comment mentions the App is decided inside `gh` (a `--jq` test), so the manager process never receives the text for that; the text is only read, capped, for the context file of a session. It is never logged, never stored in the state file, never put into a command line or a log line. The context is labelled `untrusted: true`, holds only the triggering comment and later comments of people who themselves pass the permission check (a stranger's comment is never injected into a session somebody else asked for), is size-capped, has control characters and secret-looking strings removed, and the builder prompt repeats that it is data, not instructions |
| What does the sandbox protect? | The builder runs in the same mandatory sandbox as any build: only its own worktree is writable, the shared `.git` is read-only (it cannot commit or push; Factory's supervisor does), the network is an empty allowlist, the API key and the GitHub token are not visible to commands it runs, and it holds no credential to merge or close anything. That limits what an injected instruction can **reach**: secrets, the network and other repositories. It does **not** judge whether the code it writes is good: that is what the gates below are for |
| Which gates still apply before anything merges? | The new head goes through the same phases as any head: an independent read-only **review**, **verification** (`verify.when`), then publication, which posts `factory/gate` for that exact head (the old head's status does not carry over). A repository that requires `factory/gate` and uses GitHub auto-merge merges only a passing head; a head that changes workflow files is held for a person (`factory-verified`); incomplete evidence stays a draft with `factory/gate` failing. Factory itself never merges and never closes anything, and the reply says so |
| What can an authorised person get merged? | Whoever holds `followup_min_permission` can ask for **any** change the builder can be talked into, and if review, verification and `factory/gate` pass and auto-merge is enabled, it merges without a second human. That is the reason for the write-access rule and for the gates, and it is the same trust as handing that person the label trigger (`auto_build_min_permission`). A repository that wants a human in the loop for every change requires an approving review in its ruleset (`docs/handoff.md`) |
| What is the cost bound? | One request per user per pull request per cooldown, `followup_max_sessions` per pull request, one session per repository at a time, `auto_build_timeout_seconds` per session, `run_budget_usd` for the run as a whole (the original build included), a request expires after 24 hours, and a pass reads at most 50 pull requests |
| Can Factory's own comments steer it (second-order injection)? | The reply is fixed text plus a validated commit id: nothing a model or a commenter wrote reaches it. It is written by a bot account, which the pass skips and which never enters a context, so a reply cannot start a session or be fed back to a builder. The reply is never edited by anyone but Factory (the identity is checked before every edit) |
| Can the reply be used to spam or notify? | One comment per pull request, edited in place, with no `@mention`, no link and no HTML; no reply to a commenter who was never queued (so a stranger cannot make Factory write anything); a per-pass cap and a cooldown after three failures; `followup_reply = false` and `FACTORY_ISSUE_COMMENTS=off` stop it |
| What is a request refused for? | The run is gone, the job is not published, the pull request is closed, its head is not the commit the job retained (someone pushed: Factory never builds on a head nobody reviewed), the author lost permission, it expired, or the CLI refused it. Each refusal is a fixed code in the state file and one fixed log line |

Residual risk, stated plainly: the sandbox protects secrets and the network, not the outcome of a review. A person with write access, or a comment
that persuades the builder, can produce a change that passes a model review and the repository's checks. The gates reduce that; they do not make
it zero. A comment's words also reach the builder's context after redaction, which is a filter and not a proof. Use `followup_min_permission` at
`write` or higher, keep `factory/gate` and an approval requirement in the ruleset where the change matters, and leave `followup` off where nobody
should be able to start a session from a comment.

## Epic mode

Factory builds one bounded change per issue. An issue the planner finds too large for one change but able to be split used to end as
`blocked` with a reason and no way forward. With **epic mode** (on by default, for every watched repository) the planner returns the
disposition `epic` instead: a bounded phase plan and numbered questions, posted as ONE comment on that issue, and a human gate. The
planner keeps `blocked` for what stops the work from outside the issue (a decision, an approval, a dependency) and for an issue it cannot split,
and `investigation` for missing evidence (`src/factory/prompts/planner.md`).

**The phase delivery.** The plan and the human gate come first; what follows an approval is this: Factory opens the
phase issues one at a time and keeps one status comment on the parent, starts the plan and the build of the open phase itself (below, "Automatic start"), links a phase's pull request to its
issue and to the parent, blocks a phase that the planner calls an epic, relabels a parent whose started phase stalled and can plan an approved epic afresh. **Not done yet:** the live run on a real
large issue (all of it is tested against fakes), and a halted epic (a phase issue closed without a merged pull request, deleted or duplicated) is reported but not relabelled, and nothing resumes it by itself. Per-run budgets (`run_budget_usd`) apply to each phase's own run and are
never summed.

**What happens, in order.**

1. A person with write access applies `factory-build` (as always). The planner returns `epic`. `plan` puts the first status label on the issue and
   Factory posts one comment (fixed template, hidden marker `<!-- factory-outcome:v1 issue=N kind=epic -->`, then a hidden line
   `<!-- factory-epic-plan:v1 ... -->` with the sanitised plan): why one change cannot hold it, up to 8 phases (title, objective, paths,
   acceptance, order and dependencies, size S/M/L), up to 10 numbered open questions, and the next human step. With questions the label is
   `factory:needs-info`, without them `factory:awaiting-approval`. The trigger label is consumed like after any plan. Nothing else is written.
2. **needs-info to awaiting-approval.** A comment by a person at or above `auto_build_min_permission` (`write` by default) written after the plan comment moves
   the label to `factory:awaiting-approval`. Factory never reads the words of the comment: any authorised comment counts, so a reply can only move the state one step.
3. **awaiting-approval to plan-approved.** A person at or above the same permission applies `factory-approve-plan` (Factory creates the label if it is missing). The
   label must be on the issue now, its newest applier must be an authorised person, and it must have been applied after the plan comment was last edited and,
   when the plan had questions, after the first authorised answer. Then the label becomes `factory:plan-approved` and the phase pass (below) takes over.
4. **Re-plan.** To fold the answers into a new plan, re-apply `factory-build` (remove it and add it again): `factory:needs-info` and `factory:awaiting-approval` are
   retryable status labels and the old run has no job to retry, so the issue is planned afresh in a new run. The plan comment is edited in place, the first label is set again and a stale
   `factory-approve-plan` is taken off, so an approval always refers to the plan it saw.
5. **Phase issues.** While the approval stands (below) the manager opens phase 1 as its own issue, and each next phase only after the pull request of the one before it is merged.
   The manager then starts the plan and the build of the open phase itself (see "Automatic start" below); a person with write access can still apply the trigger label to a phase issue to have it built by hand, exactly as for any issue.

Only the manager's trigger pass moves the gate (`src/factory/epic_gate.py`, `docs/manager.md`, "6. Epic gate"). It reads GitHub only, never the run directory, so it
keeps working after the run was purged and after a manager restart; a restart loses nothing but the pacing memory. `purge` does not release the epic labels.
The state machine is pure (`epic_gate.decide`) and every unknown keeps the lower state.

| state (label) | set by | next state | moved by |
|---|---|---|---|
| `factory:needs-info` | `plan` (the plan has questions) | `factory:awaiting-approval` | an authorised comment after the plan comment |
| `factory:awaiting-approval` | `plan` (no questions) or the gate | `factory:plan-approved` | `factory-approve-plan` applied by an authorised person after the plan (and the first answer) |
| `factory:plan-approved` | the gate | `factory:blocked` when a started phase stalls (otherwise none: the phase pass reads it and judges the approval again every time) | the phase pass, through `epic_gate.apply` |
| any | | no change | `factory-ignore`, a closed issue, a lookup that failed, a missing plan comment |

**Switches and the fall-back.** `epic_mode = false` in a repository's config, or `FACTORY_EPIC_MODE=off` in the manager's environment (deployment-wide, like
`FACTORY_ISSUE_COMMENTS=off`), turns epic mode off: the planner is not told about epics, a stray `epic` in its answer is turned into `blocked`
(`epic.downgrade`) and the gate pass makes no GitHub call. **Epic mode also needs a comment channel**: with `comment_on_blocked = false` or
`FACTORY_ISSUE_COMMENTS=off` nobody would see the questions, so the planner's `epic` is downgraded to today's `blocked` at plan time (never a silent dead end).
A repository with `auto_build = false` is not watched, so its gate pass does not run either, and for the same reason the planner's `epic` there is downgraded to `blocked` at plan time (nothing would ever move the labels; the `plan` CLI and `POST /plan` follow the repository's config). An `epic` whose payload breaks `epic.validate` (a forward or repeated dependency, blank text: the planner schema cannot express those rules) is turned into `blocked` as well, keeping its reason, so one bad epic never fails the plan of its batch. **Deployment note:** because epic mode is on by default, issues
the planner used to mark `blocked` as too large now become `epic` (label `factory:needs-info` or `factory:awaiting-approval`, a plan comment, a different
next step) in every repository, on deploy, without a config change. A repository that wants the old behaviour sets `epic_mode = false`.

**Cost of the pass.** Two listings per watched repository per trigger cycle (the issues with each waiting label); an issue is read only when its `updated_at` changed (or hourly), at
most 20 issues per pass, and an issue in `factory:awaiting-approval` without the approval label costs no further call. A failing pass backs off like the follow-up pass.
The log has one fixed line per move, `epic repo=org/name issue=N state=awaiting-approval`, and failures as `epic repo=... issue=N failed error=<Type>` or
`epic repo=... pass failed error=<Type> backoff=60s`: never a comment, a reason or any text.

### Phase issues and the status comment

Only where Factory may comment (the same condition as the plan comment: `comment_on_blocked`, `FACTORY_ISSUE_COMMENTS`, `auto_build`, epic mode), the manager's epic pass
(`src/factory/epic_phases.py`, `docs/manager.md`, "6. Epic gate") goes on after the gate for every open issue that carries `factory:plan-approved`:

* **The approval must stand, judged again on every pass.** The label only FINDS a parent. Authority is `epic_gate.standing`: the gate's own decision, asked again from GitHub as if the
  issue still waited for approval, so `factory-approve-plan` must be on the issue now, its newest applier must be an authorised person, it must be newer than the plan comment's last
  edit and, when the plan had questions, than the first authorised answer, and every lookup must have finished. A `factory:plan-approved` label applied by hand (a triager can apply labels),
  an approval older than a plan edit, an approval of a plan whose questions nobody answered, a removed label, `factory-ignore`, a closed parent, a plan comment that is missing, unreadable or
  names another issue: nothing is created and nothing is written. Re-applying `factory-approve-plan` by someone who may not approve stalls an epic in flight (fail closed, by design).
* **One phase issue at a time.** Phase 1 after the approval, phase k+1 only when phase k is merged: its issue is closed AND a pull request of this repository that GitHub lists as
  closing it (`closedByPullRequestsReferences`, read with a fixed GraphQL query that returns numbers, repositories and states, never a title or a body, and that asks for `includeClosedPrs: true`: GitHub leaves merged and closed pull requests out of the answer without it) is merged, which `pull_state` confirms.
  The phase pull request must therefore say `Closes #<phase issue>` and target the default branch; a build a person starts by hand does that today (`Closes #N` for the job's issue).
  An open, closed-unmerged or other-repository pull request, an unknown state, a truncated or failed read never unlocks and never halts. At most one phase issue per parent per pass,
  never more than the plan has phases (8 at most), and the open phase is looked at again after 2 minutes (a merge does not touch the parent).
* **Pull request links (slice 2).** The first line of a phase's pull request is built by ONE helper (`epic.first_line`), shared by the runner's publish, the completion of an incomplete pull request
  (`factory-verified`) and the demotion below, so the published line and a rewritten line cannot disagree. For a job that is exactly one phase issue it is `Closes #<phase issue>, refs #<parent>`
  (the `refs` for the parent sits on the same first line, the form `closing_line` already uses for several references); for the LAST phase it is `Closes #<phase issue>, closes #<parent>`, but only when
  every one of these holds, computed again from GitHub at publish AND at every completion, never taken from what was true at planning time: the pull request itself closes the phase issue (it is complete),
  the parent is open and has no `factory-ignore`, carries `factory:plan-approved` and `factory-approve-plan`, `epic_gate.standing` says the approval still stands, the phase issue is Factory's own valid child
  of the CURRENT plan (author id, marker and digest, the same reading as the pass) and is the only one for that phase, and every earlier phase is merged (the ledger and the merged evidence of the pass). A phase that is not the last always says
  `refs` and reads nothing. Anything that cannot be read, a parent that cannot be proven, a kill switch (`epic_mode = false`, `FACTORY_EPIC_MODE=off`, `auto_build = false`) says `refs`. Only a job whose issues are exactly the
  phase issue carries the link: an issue the planner grouped with it (the job is blocked, see below) or appended to the run gets the plain line, so an unrelated pull request can never close the parent.
* **A phase is not an epic.** Whether an issue is a phase is decided at plan time from the issue itself (one read of it with Factory's own identity): the origin label `factory:epic-phase` or the phase marker as the first
  line of its body, AND Factory's own identity (by id) as its author; the plan records the parent link on the job (`epic_phase` in `plan.json`, the stamp is `{parent, child, phase, of, digest}`) and never reads a run-directory file for
  the decision, so a child a person plans by hand (the trigger label) gets the link too. A label or a marker on someone else's issue makes nothing a phase. If the planner calls a phase an `epic`, the disposition becomes `blocked` with a fixed reason
  (no model text, no issue number) and the usual blocked comment says why; the gate refuses gate labels on a phase issue (`factory:needs-info`, `factory:awaiting-approval`) and the phase pass never reads a phase as a parent. An issue that looks like a phase and
  cannot be read is neither linked nor planned as an epic. A job that groups a phase issue with other issues is not run (its issues and their dependents are `blocked`, as for conflicting model labels): a phase is planned and built on its own.
* **A stale last-phase pull request.** When the phase pass sees a halted or stopped epic, or the first read of a plan whose digest differs from the ledger's, it rewrites the first line of every open pull request of this repository that GitHub lists as closing
  one of the old or open phase issues from `Closes #<phase issue>, closes #<parent>` to `Closes #<phase issue>, refs #<parent>` through the existing pull request body write (`set_pull_body`), and nothing else of the body. A first line that is anything else (a person edited it) is left alone. A phase issue of the old plan that no longer exists (deleted or transferred) is left out, as for a parent that cannot be proven, and so is one that is not Factory's (Factory publishes `closes #<parent>` only through its own phase issues): GitHub lists the pull requests of an issue only while it exists, and asking about a missing one fails for good, which would keep the new plan from ever starting. So the open pull request of a DELETED phase issue is not found and is **not** rewritten (see "Not covered" below). The old ledger is kept until the rewrite succeeded, so a failed
  call is tried again on the next pass.
* **A parent that cannot be proven any more.** The pass takes `Closes #<parent>` back in the same way (the same exact-line guard, the same `set_pull_body`, nothing else of the body) when it cannot prove the parent, so that a kill switch or a withdrawn
  approval is not defeated by a pull request that was published earlier and merges later (with auto-merge, a person has no chance to look). Four cases, each of them covered by a test: (1) a parent that carries `factory:plan-approved` and has `factory-ignore`, or no
  `factory-approve-plan`, or an approval that does not stand (the plan comment was edited after it, or the applier may not approve any more), or no valid Factory plan comment, or the origin label `factory:epic-phase` forged onto it (such a parent is never read or settled, whatever else it carries, and a label on a parent never keeps `Closes #<parent>` alive): the unmerged phase issues of the ledger (Factory's own status comment, found by marker and
  author id, readable without `standing`; a phase issue that no longer exists or is not Factory's is left out, and the pull request of a deleted one is not found, see "Not covered" below) are treated as those of a halted epic; this is one cheap drop (it counts for the 30 per pass) and a call that failed is tried again after 2 minutes;
  (2) a parent that lost `factory:plan-approved` is not listed at all, so the pass also lists the open issues that carry `factory:epic-phase` (one more call per pass), reads the marker of each one once (an issue that is not Factory's names no parent) and, for a phase
  issue whose parent is open and no longer carries `factory:plan-approved`, treats it the same way: at most 10 such phase issues per pass, and not looked at again until the issue or the hour changes (a failed one after 2 minutes); (3) a halted or stopped epic and (4) a plan that
  was replaced, as above. **Not covered**, so the line stays until a person edits the pull request or its next publish or completion rewrites it: a read that fails or stops at a bound (`epic_gate.Truncated`, more than 3,000 label events) demotes nothing; a phase issue that was **deleted** (or transferred to another repository): GitHub cannot list the pull requests of an issue that no longer exists and Factory does not search pull requests for one, so an open pull request whose first line is still `Closes #<deleted phase issue>, closes #<parent>` is not found, is not rewritten and keeps `closes #<parent>` whether the epic is halted (`child-deleted`), its plan was replaced or the parent cannot be proven; merging it closes the parent although the epic is not done (an early close, which a person undoes by reopening the parent), so a person who deletes a phase issue must also look at its open pull request and take `closes #<parent>` off its first line, or close it; a pass that does not run
  at all (`epic_mode = false`, `FACTORY_EPIC_MODE=off`, `auto_build = false`, `comment_on_blocked = false`, `FACTORY_ISSUE_COMMENTS=off`, a trigger pause, a repository the manager refuses, a manager that is down) demotes nothing, so turn those switches on a
  repository only after taking `closes #<parent>` off its open last-phase pull request by hand; a closed parent needs nothing (the keyword cannot close it again); and between a change on GitHub and the next pass (a few minutes) a merge can still close the parent.
* **What a phase issue is.** Opened by Factory's own identity with the origin label `factory:epic-phase` and no other label (not the trigger label: Factory never applies it and never relabels a phase;
  the manager starts the run itself, see "Automatic start"; `factory:epic-phase` is an origin label like `factory:observed`, not a status label, so purge never removes it, and the planner's unfiltered listing leaves
  the issue out until a person has applied the trigger label). Title `Phase n/m: <title>`. The body starts with the marker
  `<!-- factory-epic-phase:v1 parent=P phase=N of=M plan=<sha256 of the plan's hidden payload> -->`, then a fixed text: it names the parent (`part of #P`, no closing keyword), holds the plan's
  objective, paths and acceptance in ONE fenced block (sanitised again by the plan comment's sanitiser: the only model-derived text) and says that Factory starts the phase itself when every check passes, that a person with write access may apply the trigger label to build it by hand instead, and that Factory never applies it.
* **A child is Factory's only by author and marker.** Its author must be Factory's own identity (the App's bot user id, or the ambient `gh` login), its first line must be this marker for this parent
  and for the CURRENT plan. A plan that changes in any way (phases, order, questions) has a new digest, so existing phase issues belong to the old generation and are ignored; an unchanged plan keeps its
  merged phases (a template upgrade rebuilds nothing). Children are found WITHOUT trusting any label: by the ledger's numbers first, united with a bounded listing of the issues Factory's identity opened
  since the plan comment's last edit (at most 5 pages of 100 items; Factory's own pull requests are listed with its issues and count towards the pages, so a page full of them never reads as the end). Removing the origin label makes no child invisible and causes no second one. Right before every write the search is repeated, so a crash after the write,
  a failed ledger update or a second manager leaves one child (the next pass finds it by search). Two valid children for one phase is a stop-and-say state (nothing more is opened); a deleted child halts
  the epic and is never created again; an unknown identity creates nothing. The listing reads at most 5 pages of 100 items (newest first, since the plan's last edit) and is the only proof that the next phase issue does not
  exist yet, so on a repository where Factory opened more than 500 issues and pull requests since then, a phase whose issue is not in the ledger cannot be opened: nothing is created, the log says `truncated` and the status comment
  says `unknown` / `listing-truncated`, so the stall is visible. It is not narrowed to the status comment's last edit on purpose: a second manager's older view or a person's edit of that comment can leave it newer than a child
  it does not list, and a second phase issue would follow. Nothing resumes such an epic by itself (see "Getting an epic unstuck" below).
* **The status comment is the ledger.** One more Factory comment on the parent (hidden marker `<!-- factory-outcome:v1 issue=P kind=epic-status -->`, found by marker AND Factory's identity before any write,
  edited in place, never the plan comment: editing that would void the approval). Its text is a pure function of the plan's size and the evidence, ints and fixed words only: a checklist
  (`- [x] Phase 1: #101, pull request #110 merged`), a state word (`in-progress`, `done`, `halted`, `stopped`), a fixed code (`phase-open`, `checking`, `all-merged`, `closed-unmerged`, `child-deleted`,
  `duplicate-children`, `listing-truncated`) and a hidden ledger (phase to child number, plan digest, the sha256 of the child's title and body when it was opened, the closing pull request, merged, and the first sighting of a
  closed-without-merge state). The ledger is monotone: a merged phase stays merged (checked again with `pull_state`, never undone by a label or a deleted issue), and a merged claim that is not true (a forged
  ledger) falls back to the evidence. An edit of a child is noticed by comparing the hash, never by rendering it again, so a template change does not make every child look edited.
* **Getting an epic unstuck.** A stopped, halted or unknown epic is looked at again within an hour (`epic_gate.RESCAN_SECONDS`; an edit of a phase issue does not touch the parent, so Factory does not see it sooner). Never close a phase
  issue to resolve one of these states: closing a phase issue without a merged pull request is exactly what makes `closed-unmerged`, and Factory counts closed phase issues as phase issues. What a person can do, by code:
  `duplicate-children` (two phase issues of this plan claim one phase): keep one and delete the first line of the other (the hidden `factory-epic-phase` marker line), so it no longer reads as a phase issue of this plan, or, with
  administrator rights, delete the other issue; the next pass sees one phase issue and goes on (the merged phases stay in the ledger). Closing the extra issue changes nothing. Before deleting any phase issue, look at its open pull request: once the issue is gone Factory cannot find that pull request, so a line `Closes #<phase issue>, closes #<parent>` on it stays and merging it closes the parent early (see "Not covered" above).
  `closed-unmerged` (a phase issue was closed without a merged closing pull request, for longer than 10 minutes and on two reads): reopen the phase issue; it is then the open phase again. If it was closed on purpose, or the
  code is `child-deleted` or `listing-truncated`, nothing resumes the epic by itself: a person applies the trigger label to the parent for a new plan ("Re-plan" above) or the remaining phases are opened and built by hand, like any issue. Deleting Factory's status comment is
  NOT a remedy: it is the ledger, and without it a merged phase whose issue is gone would be opened again. Whether a person with write access may edit the body of an issue Factory opened, and whether only an administrator
  may delete one, is not verified on a real deployment.
* **No false halt.** A phase issue closed without a merged closing pull request is not a halt at first sight: GitHub needs a moment to link the pull request. The state must hold on two reads and for 10 minutes
  after the issue was closed (the first sighting is kept in the ledger). Only then the status comment says `halted` / `closed-unmerged`, and the log says it once. A halt is only reported; a stall (below) is the one state that relabels the parent.
* **Automatic start.** The manager begins the build of the open phase issue itself (`epic_phases.prepare_start`, called by the label pass only when nothing else is to start: no process of ours in the repository, the lease free, no person's labelled issue waiting; one start per repository per pass). Its authority is the approval, never a label and never a stamp: everything is proven again from GitHub and the ledger at the moment of the start, and any read that fails or is cut off starts nothing. A phase is started only when ALL of these hold: the parent is open, has neither `factory-ignore` nor the origin label and carries both labels; `epic_gate.standing` says the approval stands (an approver at or above `auto_build_min_permission`, newer than the plan, questions answered); every earlier phase is merged (the walk stops at the first one that is not); exactly one valid open phase issue exists for the phase and its title and body, read again right before the start, still hash to the ledger's (an edit, however small, is never started); the phase issue has no `factory-ignore`, no status label and no trigger label (a person's own request is the ordinary label path's, which the manager does not race); GitHub lists no pull request that closes it; no run directory lists it; the ledger has no `started` record and fewer than 3 recorded plan attempts; and fewer than `epic_max_open_prs` (default 3, 1 to 50) pull requests of Factory's own App identity are open in the repository. That last read (`GithubReader.open_pulls_of`) HOLDS the start when it is cut off at its page bound, fails, or Factory has no App identity (an ambient `gh` login cannot tell Factory's pull requests from a person's): one rate-limited `skipped reason=epic_cap` line, a count in the closed `skip_reason` set and `start-held` in the status comment. The manager never applies the trigger label and never relaxes `auto_build_min_permission` (the bar for the approver; a repository that is not private with a minimum below `write` is refused as for any start).
  **Order of writes.** The attempt (`plans` + 1) goes to the status comment FIRST; only when that write succeeded does the start go on, so a crash can cost an attempt, never a second run (a refused start keeps its attempt too: fail closed). The run directory then gets the stamp `epic-phase` (parent, child, phase, plan digest and the verified title and body) beside the `trigger-run` marker. The two are written and removed together on every failure path (a refused start, a stamp that cannot be written, a plan that ends without `plan.json`), each step on its own; a directory that cannot be removed is said (`cleanup incomplete`) instead of failing silently as before. `plan --issues=<phase issue>` reads the stamp: the planner's input for that issue is the verified text and an empty list of comments, never the issue's title, body or comments on GitHub (only its state and labels are read, so `factory-ignore` and a close are still honoured); a stamp that is invalid or does not name exactly that issue refuses the plan. A comment or an edit made after the issue was opened never reaches `issues.json`.
  **Proven again at the build.** The plan can wait many minutes before its `run` starts (the planner itself, a trigger pause, a backoff, a manager restart: the plan is found again from the run directory), so the approval is
  asked again where the money is spent, not only when the plan starts. Before the `run` of a run directory that holds the stamp, `operations._epic_build_proven` calls `epic_phases.may_build`: epic mode and Factory's right to comment are still on; the
  parent is open, has neither `factory-ignore` nor the origin label and carries both labels; `epic_gate.standing` says the approval stands for the plan digest of the stamp; the phase issue is still Factory's own, open, without `factory-ignore`, with
  this parent's and this plan's marker, and no pull request closes it. A read that fails for a reason that can pass (a timeout, a rate limit, a 5xx) holds the build (one `epic build not proven error=<Type>` line and the repository's backoff; the plan stays pending), but for 24 hours at most
  (`operations.EPIC_BUILD_HOLD_SECONDS`, 24 hours, counted from the first pass that could not prove it), because a pending plan is served before every other start of the repository and a hold without an end would keep
  all of its work waiting. That first moment is recorded in the file `epic-build-held` of the plan's run directory (one number, whole epoch seconds, written atomically, removed when the build is proven or refused), so a manager restart does not start the count again; a record that is not one plain number or lies in the future counts as now and is written again, and when the record cannot be written this process's own count still bounds the hold. It is wall time: a pause, a stopped breaker or a manager that was down between two failed reads counts as hold time, so a plan held across a long pause is refused by its next failed read (the safe side: a refused plan is a stalled phase that a person re-plans). A read that is cut off at a bound (more than 20 pull requests that reference the phase issue, a plan comment past the search bound) or that GitHub answers with 404 or 410 for the parent, or for the read of the phase issue's labels, cannot be proven by asking again, so
  it is refused at once, as is a hold that reached its limit, with one fixed line, `epic repo=... issue=P phase=N child=C build refused: the approval could not be proven`. A phase issue that GitHub says is gone is not that case: the reader's `phase_issue` answers "no such issue" itself for a 404 or 410, so it is a definite no and gets the other fixed line. Anything else that is not so (a withdrawn approval, an approver who lost access, an approval
  older than the plan, a phase issue that is gone, changed or not Factory's own) refuses it for good with the other fixed line, `epic repo=... issue=P phase=N child=C build refused: the approval no longer stands`. Either refusal takes the `trigger-run` marker off, so the plan is never picked up again and the repository is not kept
  waiting behind it. Recovery by hand, when a held plan must not wait for its limit: remove the `trigger-run` file from the plan's run directory (the same effect), or remove the directory. The
  stamp stays, and the run counts as dead (it was never built), so once the approval is back the phase is reported `stalled` and the parent moves to `factory:blocked` for a person to re-plan, and nothing builds it. A stamp that is not valid refuses the build too
  (`build refused reason=stamp`). A run without a stamp is a person's own request and is not asked. The open pull request cap is a condition of starting a phase, not of finishing one that was started under it.
  **Every start of `run` the manager makes by itself for a stamped run asks the same question.** Two more automatic paths start `run` for a run directory that already holds jobs: the restart's auto-resume (`auto_resume_after_restart = "once"`, which `scripts/register-repo.sh` writes for enrolled repositories) and the dependency resume (a queued job whose dependency's pull request merged). For a run directory that holds the stamp both call the same verdict as the build (`operations._epic_verdict`, the only place that calls `epic_phases.may_build`) BEFORE anything is claimed or retried. A run that cannot be proven (a definite no, a read that fails or is cut off, a stamp that is not valid, no reader) is not started: it stays as it is (an `interrupted` job stays interrupted, a queued job keeps waiting), nothing is held, counted or marked, and one fixed line says why: `epic repo=... issue=P phase=N child=C resume refused: the approval no longer stands` (or `... resume refused: the approval could not be proven`; a stamp that is not valid says `trigger repo=... run=R resume refused reason=stamp`). The restart's auto-resume does not claim its once-only marker then, so a later restart (or a person's `resume`) can still resume the run; the dependency resume proves it again on the next pass (its line is rate-limited). A refused resume is not reported by the epic pass: it reads an `interrupted` job as a run still going (only a failed, blocked, stopped or closed job is dead), so the phase is neither started again nor reported stalled until a person resumes or blocks that job. What the kill switches do NOT stop: a `run` that is already running (its supervisor goes on with the jobs of the run until it ends or an operator's drain or stop, or the run budget's drain, ends it: the switches decide starts, not a run in flight), and what a person does by hand, which is a person's authority and not the approval's: the trigger label on a phase issue after a failed job (the owed retry, authorised like any issue), and `retry`, `resume` and `run` from the dashboard or the API.
  **Started, stalled, never twice.** "Started" is a run directory whose plan lists the phase issue, or a status label on it applied by Factory's own App identity (a person's label proves nothing); a pass writes it to the ledger (`started`), which outlives a purge. Up to 3 plan attempts are recorded per phase issue (each after the repository's usual backoff). A phase that was started and has no open or merged closing pull request (GitHub's list) and no run that can still produce one (the planner chose no job for it, or its job failed, was blocked, stopped or closed, or the run's budget was drained and the job is not running: a drain lets the active phases finish, so a running job is still live, while a queued or interrupted one never starts; or a plan whose build was refused), or whose 3 attempts never produced a run, is **stalled** once seen so on two reads at least 2 minutes apart: the status comment says `stalled` (`phase-stalled` or `plan-attempts`), the log says it once, and the parent moves from `factory:plan-approved` to `factory:blocked` through `epic_gate.apply` (the new label first; the human's `factory-approve-plan` is not touched). Factory never starts that phase again and never plans it again in a loop, purge included. A halted epic (`closed-unmerged`, `child-deleted`, `duplicate-children`) keeps its label and is read again hourly as before. A plan attempt recorded but never seen to produce a run, with its directory purged and no label of Factory's on the issue, can be retried up to the cap: that is the one case a second run is not ruled out (UNVERIFIED on a real deployment, see below).
  **The run directories are read newest first and a cut-off listing is not "no run".** The evidence comes from at most 500 run directories (`manager.MAX_LISTED_RUNS`), newest first, and the newest run that lists the phase issue decides. When more exist and none of those listed names the issue, "no run" is not proven: the phase counts as having a run in progress, so it is neither started nor reported stalled, one rate-limited line says so, and it resolves when old runs are purged.
  **A hash is never taken at first sight.** The hash of a phase issue's title and body is the ledger's, or the one the manager computed when it created the issue (remembered in memory only, for the case the ledger write failed), never what GitHub shows when it first sees the issue: a person who can edit it could otherwise choose the text of a paid build. A phase issue with no recorded hash (a restart between its creation and the ledger write) is never started by the manager: the status comment says `its text was not recorded, so Factory will not start it`, and a person's trigger label on the phase issue is the way to build it.
  **Re-plan.** A person re-applies the trigger label to the parent: `factory:blocked` is retryable, and so is `factory:plan-approved` now, but only while no phase is in flight (a started phase with a live run or a pull request, or a closed one being checked, holds it with `skipped reason=in-flight`; anything unreadable counts as in flight). A fresh plan edits the plan comment (a new digest, so the old phase issues are old-generation and ignored) and strips the approval; once it is approved again the new generation starts at its first phase. The planner reads the repository at the latest base head (every plan resets its worktree to `origin/<base>`, pinned by `ReplanReadsMergedCodeTests`), so the merged phases are in the code it plans against; it is not told their names.
  **Budget.** One run per phase, so `run_budget_usd` applies to each phase's run on its own and is never summed over an epic (owner decision); `epic_max_open_prs` caps open pull requests, not money.
* **Bounded.** At most 10 approved parents are read in full per pass, round robin (the cursor survives passes). A full read counts from the moment it is made, whatever it ends in: a read that fails, stops at a bound
  or finds that the approval does not stand costs calls too, so parents that keep failing or that a triager labelled by hand cannot make a pass unbounded, and the cursor moves on, so they cannot starve the others.
  A parent with no valid Factory plan comment is a cheap drop: one call (to read the parent's comments, 100 to a page), and one more for each further 100 comments, at most 10 pages (1,000 comments), plus the same read for its status comment (the ledger, to take `Closes` back, see above); a parent with more
  comments than that and no plan comment is reported as `truncated` and counts as a full read. It is then skipped until it changes (hand-labelled junk cannot starve a real epic; at most 30 such parents are looked at per
  pass); the status writer's per-pass comment cap (`FACTORY_ISSUE_COMMENTS_PER_CYCLE`) ends the pass.
  A typical read of an epic with a phase in progress is about 9 calls every 2 minutes: the plan comment, the approval's evidence, the status comment, one listing, one read of the open child and, once it is closed, one GraphQL read and one pull request read.
  Every pass adds one listing of the open phase issues (the sweep above); a phase issue of an approved parent costs nothing more once its parent is known.
* **Switches.** `epic_mode = false`, `FACTORY_EPIC_MODE=off`, `auto_build = false` (no GitHub call at all), `comment_on_blocked = false`, `FACTORY_ISSUE_COMMENTS=off`, `factory-ignore` on the parent, a trigger pause, removing
  `factory:plan-approved` or `factory-approve-plan`, or editing the plan comment: no new phase issue, no start and no issue or comment write (the one write left is taking `Closes #<parent>` back from an open last-phase pull request, see above). The log has `epic repo=org/name issue=N phase=K child=C created` for each phase issue opened and,
  rate-limited, `epic repo=... issue=N state=halted code=closed-unmerged` (or `state=stopped code=duplicate-children`).
* **No new GitHub App permission.** `issues: write` already covers creating issues and comments. The writers of issues are now `issue_labels.py` (labels), `issue_comments.py` (outcome comments),
  `observed_issues.py` (observation issues) and `epic_phases.py` (phase issues and the status comment). Slice 3 adds one writer of an existing kind: moving a stalled parent from `factory:plan-approved` to `factory:blocked` is an issue label write (`epic_gate.apply`, called by `epic_phases.py`), covered by `issues: write`; the start itself writes nothing to GitHub but the attempt in the status comment, and its stamp is a file on the manager's own disk. Slice 2 adds no writer of issues and no permission: the pull request first line is written by the runner at publish, by the completion step and, for a stale
  last-phase pull request, by `epic_phases.py`, all through the pull request body write that `pull_requests: write` already covers.

**Deviations from the original design, on purpose.** The status comment is a SECOND Factory comment on the parent (this document used to say "never a second comment"): the plan comment cannot carry the ledger because any
edit of it voids the approval. The phases are strictly sequential (`depends_on` never parallelises them). The start is a manager action whose only authority is the approval (never a label), with a per-run budget that is never summed over the epic. The `refs` for the parent is on the same first line as the phase issue's `Closes` (`Closes #c, refs #P`), not on a line of its own; the nested-epic block takes the set of phase issues of the plan instead of a flag (`epic.downgrade(..., nested=)`); the stale-line rewrite also covers a halted epic and a replaced plan, but only for a parent the pass reads.
**Slice 3, unverified on a real deployment** (the unit tests use fakes): a real epic from the plan comment through the approval to the first phase pull request, started by the manager from the stamp (that `plan --issues=<phase issue>` really begins from the stamp's text on a deployment, with the worker identity and the data directory's modes); that a phase pull request merged under auto-merge unlocks the next phase and what `merged_by` is then; that the closing-pull-request read lists the merged pull request across repositories and in the timeline; whether a triager can edit Factory's status comment or the body of a phase issue (the hash check would then refuse the start, which is safe, but a person would have to say it) and whether the conversation of a phase issue can be locked; that `creator=<slug>[bot]` and the open pull request listing identify Factory's own pull requests; that `trigger.is_gone` recognises the error text `gh api` really prints for a deleted parent or a deleted phase issue's labels (tested against the text shape the existing not-found handling uses); and the one residual above (a recorded attempt with no run, its directory purged and no label of Factory's on the issue). Phases are strictly sequential, and a person has to look at a stalled or halted epic.

**Unverified on a real deployment** (the unit tests use fakes): that Factory's own GraphQL query (`closedByPullRequestsReferences(first:20, includeClosedPrs:true, orderByState:true)`; it never ran against GitHub, and the phase unlock depends on `includeClosedPrs`,
which GitHub's own MCP server also sends) lists the merged pull request, and does so for a repository other than the one probed (the only probe made was an MCP read, for a `Closes #N` first line), the timeline and closing behaviour of phase pull requests, that `creator=<slug>[bot]` lists the App's issues, whether a triager can edit Factory's comments or an issue body,
whether the conversation of a phase issue can be locked, and what `merged_by` is under auto-merge. Slice 2: that GitHub closes the parent when the last phase's pull request merges with the first line `Closes #c, closes #P` (the same shape grouped jobs already use, but never seen merging with a parent that is not the pull request's own issue), that `refs #P` shows in the parent's timeline, that the live proof (`standing`, the listing of Factory's own issues, `closing_pulls`) is fast enough at publish on a real repository, and that the rewrite of a stale pull request first line behaves on a real open pull request.

### Threat model: the epic gate

An epic puts model-derived text from an untrusted issue in front of readers (as the outcome comment does, see above) and gives people a way to make Factory move a label, which is
the first step of a later build. An approval now makes Factory open issues, keep one status comment and START the build of the open phase itself: the first place Factory spends money from an approval made on GitHub. Its branch and pull request are the ordinary ones.

| threat | what stops it |
|---|---|
| A prompt-injected plan (pings, links, images, HTML, secrets, paths outside the repository, a forged marker) | every phase title, objective, path, criterion, question and the reason goes through the same sanitiser as the outcome comment and sits inside fenced code blocks, where GitHub renders nothing; a field that cannot be trusted is shown as `(withheld)`. The hidden marker carries only the sanitised plan. Caps: 8 phases, 10 questions, a length per field; a plan that breaks them is never posted: the planner schema carries the length and count limits, and `epic.validate` the rest (dependency order and uniqueness, non-blank text), where an invalid epic becomes `blocked`. The comment cannot exceed 60,000 characters |
| Factory's comment steers the planner later | the plan comment starts with the shared outcome marker, so `without_factory_comments` removes it from the planner's snapshot |
| A person without access answers or approves (a triager can apply labels; anyone can comment on a public issue) | the commenter and the label's applier must be users (never bots, Factory's own comments included) whose collaborator permission is at or above `auto_build_min_permission`; a custom role, `none`, a failed lookup, more events than the page bound (3,000) or commenters that were not looked up counts as no. The first 10 distinct commenters are looked up whoever they are; after that only commenters GitHub itself calls an owner, member or collaborator (`author_association`, which a commenter cannot set; it never authorises anyone, the permission lookup does), up to 40, so drive-by or sock-puppet accounts cannot use up the lookups before the maintainer's answer. When a bound still hides the answer (a person with a private membership comes after 10 drive-by commenters) the pass says so with `epic repo=... issue=N truncated`, once per issue per rate-limit period. A minimum below `write` is refused for a repository that is not private, as for the trigger label |
| A forged or stale approval | the label must be on the issue now; its newest applier is the one judged, so an unauthorised re-apply after an authorised one fails closed; it must be newer than the plan comment's last edit and, with questions, the first answer, so an approval from before a re-plan approves nothing; a fresh plan takes a stale approval label off |
| An answer approves, or an approval skips the questions | the words of a comment are never read: the projection keeps the author, the type and the times only; a reply can only move `factory:needs-info` to `factory:awaiting-approval`, and only the label approves; the label is ignored while the issue is in `factory:needs-info` |
| A person's comment quoting the marker is edited, or hides the plan | the plan comment is found by its marker AND by Factory's own identity (the App's bot user id, or the ambient `gh` login); the lowest id wins; no other comment is touched. Without a known identity nothing is found and nothing moves |
| Flooding or rate-limit pressure | at most one plan comment and one status comment per epic, each edited in place (`FACTORY_ISSUE_COMMENTS_PER_CYCLE` still applies); a bounded, paged read; unchanged issues skipped; a per-repository backoff |
| An issue Factory must not touch | `factory-ignore` and a closed issue are never written to; a paused trigger runs no gate pass |
| A forged marker (a person writes the phase marker, or the status comment, or the ledger) | a phase issue counts only when its author is Factory's own identity (by id) AND its first line is the marker of this parent and of the current plan; the status comment and the ledger are read only from a comment of Factory's own identity. A forged child neither counts nor unlocks a phase; a forged ledger claim of a merge is checked again with `pull_state` |
| A label is removed or changed (the origin label, `factory:plan-approved`, `factory-approve-plan`) | no label is authority: the origin label is not used to find children (removing it creates no second child), `factory:plan-approved` only finds a parent (`standing` is re-derived), removing either approval label creates nothing more; re-applying the approval label by someone who may not approve stalls the epic (fail closed) |
| A phase issue is edited | its title and body are hashed when it is opened and compared by hash (never rendered again); the status comment shows `edited since Factory opened it`. An edited phase issue is never started by the manager (a person's trigger label on it plans it like any issue), the hash is never adopted from what GitHub shows at first sight (an issue with no recorded hash is not started), and the planner reads only the verified text recorded at the start |
| Hand-labelled junk floods the pass | a parent with no valid Factory plan comment is one call (one more per further 100 comments on it, at most 10 pages), then skipped until it changes; 10 full reads (a read that fails, stops at a bound or ends with an approval that does not stand counts as one, so failing or triager-labelled parents cannot make a pass unbounded) and 30 cheap drops per pass, round robin |
| Model-derived text in an issue Factory creates itself | the only model text is the plan's, sanitised twice, in ONE fenced block; the title is the sanitised phase title; the template names the parent without a closing keyword, mentions nobody and links nothing; the status comment carries no model text at all |
| The last phase closes the parent when it should not (a stale plan, a withdrawn approval, an ignored or closed parent, a phase issue that is not Factory's, an unmerged earlier phase) | `Closes #<parent>` is computed live at publish and at every completion from the current state and fails safe to `refs` for anything that cannot be read; the pass rewrites the line of an open pull request of a halted epic or a replaced plan back to `refs`. A parent that is ignored, lost an approval label (either one), has an approval that does not stand or no valid plan comment is not settled, but the pass still takes the line back from the ledger's open pull requests, and a parent that lost `factory:plan-approved` is found through its open phase issues. What stays until a person edits the pull request: a pass that does not run (every kill switch of epic mode, a trigger pause, a manager that is down), a read that fails or stops at a bound, and the few minutes before the next pass |
| An unrelated issue rides on a phase's link (an issue appended to the run, or grouped by the planner with the phase issue) | the link is carried only by a job whose issues are exactly the phase issue; a stamped issue grouped with others is blocked at plan time, and the helper gives any other job the plain line |
| A forged label or marker to fake a phase (to get a `Refs` line or to block a person's epic) or to hide one (to nest an epic) | a phase is decided from the issue itself and needs Factory's own identity as its author (by id): a label or a marker on a person's issue makes nothing a phase. Faking can only block an epic or omit a `Closes #<parent>`, the safe directions. This holds for the label on a PARENT too: the pass never reads or settles it (its epic is blocked), takes `Closes #<parent>` back from the open pull requests of the unmerged phase issues of its ledger exactly as for an ignored parent, and the live proof says `refs` for it, so no label can keep a stale `Closes #<parent>` alive (a test per case: ignored, no approval label, an approval that does not stand, no plan comment, and a parent that is otherwise approved). Hiding one needs a person to remove both the label and the marker from Factory's issue, who could open the same issue by hand |
| A phase issue planned as an epic of its own (grandchildren without a depth bound) | the planner's `epic` for a phase issue is blocked with a fixed reason; the gate refuses gate labels on it and the phase pass never reads it as a parent |
| An epic is as autonomous as the repository's ruleset | the phase pull requests go through the same review, verification and `factory/gate` as any pull request, but `handoff/ruleset.json` ships `required_approving_review_count: 0`: with that, a phase pull request can merge on passing checks alone and unlock the next phase without a person reading it. **Require at least one approving review for repositories that use epic mode** (`docs/handoff.md`). Do not read `merged_by` as a person under auto-merge (unverified) |
| Factory spends money from a GitHub-side approval (the automatic start) | the approval is the only authority and is proven again at the moment the plan starts AND right before its build (`epic_phases.may_build`; however long the plan waited, and after a restart), and a refused build is never picked up again (`standing`: an approver at or above `auto_build_min_permission`, newer than the plan, questions answered; the ledger; every earlier phase merged); nothing is trusted from an earlier pass, a label or a stamp; a read that fails or is cut off starts nothing; one phase per repository per pass, never while an earlier phase is unmerged; the kill switches (`epic_mode = false`, `FACTORY_EPIC_MODE=off`, `auto_build = false`, a trigger pause, `FACTORY_ISSUE_COMMENTS=off`, `factory-ignore`, removing either approval label) stop the plan and every start of its `run` by the manager (the first build, the restart's auto-resume and the dependency resume ask the same question, `operations._epic_verdict`); a `run` already running, and what a person does by hand (the trigger label on a phase issue, `retry`, `resume` or `run` from the dashboard or API), are not stopped by them; `epic_max_open_prs` holds the start of a phase; the run is the ordinary run (its own `run_budget_usd`, never summed) |
| A phase is started twice, retried in a loop, or re-planned after a purge | the attempt is written to the status comment BEFORE the start (a crash costs an attempt, never a second run); a phase with a `started` record, a run, a closing pull request, a status label or 3 recorded attempts is never started; the record outlives the run directory; a started phase with no way to a pull request is reported `stalled` and its parent is moved to `factory:blocked`, and a person re-plans |
| An edited phase issue, or a comment made after it was opened, steers the planner | the manager hashes the title and body against the ledger right before the start (an edit is never started) and records the verified text in the run's stamp; `plan` reads only that text and an empty comment list for the issue (a test pins that an edit or a comment after creation never reaches `issues.json`) |
| Many pull requests at once | the start holds while `epic_max_open_prs` pull requests of Factory's own App identity are open; a listing that is cut off, fails or has no App identity (ambient `gh`) holds too, and the status comment says `start-held` |
| A person is raced by the manager on a phase issue | a phase issue that carries the trigger label, a status label or `factory-ignore` is never started by the manager: the ordinary label path (a person's authority) decides |

Residual risk: any authorised comment, even an unrelated one, moves the issue from needs-info to awaiting-approval, because the text is deliberately not read; approval still needs the label,
so the person who approves has seen the plan and the answers. The sanitiser is a filter, not a proof (see above).

## Status

```sh
api "$M/repos/ORG/REPO/runs/RUN/status"        # the CLI's status --json: jobs, cost, supervisor
api "$M/repos"                                  # every configured repository, its runs, recent exits and "github" (token check)
```

Poll no faster than every few seconds: each `status` asks GitHub for the state of every open pull
request. `status` is where a failing CI run shows up (`handoff_status: ci_failed`, job still
`pr_ready`); `run` does not check CI. Job states: `queued`, `running`, `interrupted`, `failed`,
`blocked`, `needs_human_publish`, `pr_incomplete`, `pr_ready`, `merged`, `closed`. The row's `next_action` and `owner` say
what is expected and from whom.

A job that waits for a worker slot shows `Wait for pod worker budget: n/N workers in use` (all
supervisors of the pod share one budget; `docs/capacity.md`). `status --json` also has `capacity`
(`total`, `in_use`, `free`, the `holders` and the `waiting` tickets, or `null` when there is no
budget), `footprint` (per phase: invocations, peak and p95 of resident MiB and cpu seconds,
sampled from the workers; `{}` until a session has recorded one) and `tokens` (per phase: the token counts of the
sessions and the prompt cache hit rate; `{}` until a session has reported usage; "Tokens and cache hit rate" below).

### Tokens and cache hit rate

Every session's usage entry (`state.json`, `jobs.<id>.usage[]`; the planner's is `planner-usage*.json`) keeps the `usage` object
of the Claude Code result as the CLI wrote it. `status --json` reads it, read-only, and adds a `tokens` block per phase (`plan`,
`build`, `review`, `verify`, `preflight`; the planner's counts are the `plan` phase, every planner session of the run including a `plan --append-to`), summed over the sessions of the run:

```
"tokens": {"build": {"invocations": 12, "input_tokens": 2600, "output_tokens": 410000,
                     "cache_creation_input_tokens": 1500000, "cache_read_input_tokens": 21000000,
                     "ephemeral_5m_input_tokens": 1500000, "ephemeral_1h_input_tokens": 0,
                     "cache_hit_rate": 0.933}, "review": {...}}
```

* `input_tokens` is the **uncached** input, `cache_creation_input_tokens` what was written to the prompt cache, `cache_read_input_tokens` what was
  read back from it, `output_tokens` what the model wrote. `invocations` counts the sessions that reported any of them.
* `ephemeral_5m_input_tokens` and `ephemeral_1h_input_tokens` split the cache writes by cache lifetime (from `usage.cache_creation`). They are shown **only** for a phase
  whose sessions reported them: whether the CLI's `stream-json` result carries them is not verified here, so a missing split means "not reported", never zero. The split covers only the sessions
  that reported it, so the two figures can add up to less than `cache_creation_input_tokens` when some sessions of the phase did not.
* **`cache_hit_rate` = `cache_read_input_tokens` / (`cache_read_input_tokens` + `cache_creation_input_tokens` + `input_tokens`)**, the share of all input tokens
  that came from the cache. It is `null`, not `0`, when that sum is zero (nothing to divide): `0` means input
  was used and none of it came from the cache. It is a ratio of token counts, not of cost: cache writes cost more than uncached input and reads less.
* A value is used only if it is a non-negative whole number (a `true` is not a number); anything else is ignored, never an error. Sessions that
  reported no usage (older runs, a session that died before its result) count as nothing, so a job with no usage data shows no row.
* The counts are client-side figures from the CLI, like `cost_usd`; the Console usage page is authoritative.

They outlive a purge: each `job` entry of the history ledger carries the cumulative counts of its job per phase (`tokens`), and the `planned` entry the planner's
(`planner_tokens`), so the numbers survive the run directory (`docs/manager.md`, "History ledger"). The metrics listener exposes them as
counters per repository and phase (`factory_input_tokens_total`, `factory_output_tokens_total`, `factory_cache_creation_input_tokens_total`,
`factory_cache_read_input_tokens_total`; `docs/metrics.md`).

If a live pull-request lookup fails (a gh error, a timeout, a failing token broker, an exhausted 30 s budget), `status`
still answers from the persisted state and says so: `status --json` has `live_refresh: {ok, failed: [{job, error, reason}]}`
(`error` is the exception class, `reason` a short code such as `gh_failed`, `timeout`, `auth_failed`, `invalid_response`,
`not_reached_after_timeout` or `time_budget_exhausted`; never the gh message), the text output starts with one
`Warning:` line, and each affected job's `next_action` says its information may be stale. A timeout or an authentication
failure stops the refresh, so every job it did not reach is listed as failed. `run`'s label reconcile degrades the same way.

What counts as a failing check is reported as `ci_basis` in `status --json`. `factory/gate` is Factory's own
signal and never counts, under any basis.

- `required`: the base branch's required checks were read and the set is non-empty. Only a failing required check
  makes the handoff `ci_failed`; other failing checks are listed as informational (`other_failing_checks`).
- `rollup_no_required`: the required set was read and is empty (nothing gates the merge, for example a repository
  without branch rules). Any failing check in the rollup makes the handoff `ci_failed`, and text `status` says
  "no required checks are configured; any failing check counts". CI repair (`retry --reason`) works the same way.
- `rollup_fallback`: the required set could not be read, so the whole rollup decides, and text `status` says so.

## Jobs that change `.github/workflows`

The Factory App has no `workflows` permission, on purpose (`docs/handoff.md`, "Why `pull_request_target`"). GitHub
therefore refuses any push by the App that creates or updates a file under `.github/workflows`. This repository is
mostly CI and deployment code, so Factory handles that case in three steps instead of finding out at the push.

1. **Marked as soon as the plan exists.** A job whose `likely_paths` are, or contain, `.github/workflows` (a path
   under it, the directory itself, or `.github`) is marked from the plan alone. `plan` prints, after its `Planned N
   issues in M jobs` line, one line per such job (`<job>: changes files under .github/workflows; the Factory App
   cannot push them, so a person publishes this job's branch after it is built and checked`; the job id only, never a
   path, because the output goes to the manager's log), `plan --append-to` prints the same lines for the jobs it
   appends, after its `Appended N issues ...` line, and `status` prints the same lines while the run has no
   `state.json` yet, that is between `plan` and the first `run`. Once the run has started, the `status` row has
   `needs_human_publish: true` and `workflow_paths`, and the text output says `Workflow files: the Factory App cannot
   push them, so a person must publish this job's branch`. Jobs with no dependencies start building as soon as `run`
   starts, so the window before the build is the time between `plan` and `run`: look at the `plan` output. `.` (the
   planner's "whole repository" estimate) does not count, so a workflow edit the plan did not predict is marked as
   soon as the build has changed the file (`actual_paths`). The job is still built, reviewed and verified as usual; the
   builder is only told, in a "Publishing note" of its task, that a person will publish it. That note is advice: it
   changes no sandbox setting and no phase, and the plan itself (digest-pinned) is not edited.
2. **A refused push keeps its reason.** `publish` keeps the stderr of a failed `git push` in the job state
   (`push_error`: secret and credential patterns and this process's secret values redacted, control characters
   removed, at most 2000 characters, the tail), and sets `push_rejection` to `workflows_permission` when GitHub's text
   is the workflow-file rule (matched loosely: "refusing to allow ... to create or update workflow ... without
   ... permission"), else `other`. The job's `error` then reads `rejected: workflow files need the workflows
   permission`; for any other refusal it reads `git push failed (exit N): <the redacted tail on one line>` and the job
   is `failed`, as before. `status` prints it as `Push refused: ...`.
3. **The finished work is kept.** When review and verification passed and the push is refused for workflow files, the
   job ends `needs_human_publish` instead of `failed`. Its commit, review, verification, branch and worktree stay as
   they are, a `git bundle` of its commits (base excluded) is written to `<run dir>/<job>/<job>.bundle`, and `status`
   shows the steps in `next_action` (owner `operator`). The issue gets `factory:blocked` and the one outcome comment
   (code `needs_human_publish`, from a fixed template; the same `comment_on_blocked` and `FACTORY_ISSUE_COMMENTS`
   switches as the other outcomes: a job that used to end `failed` here commented too). `run` exits 2 and prints the
   same steps. Work that did not pass review or verification is not parked: it fails with the reason and stays in its
   retained worktree, like any other failed job.

**A person's push is not an approval of the diff, so the pull request waits for one.** The ruleset requires no approving
review, and the App's private key and the deployment pipeline's access sit behind workflow files. A job that changes
workflow files (the head being published changes one, read from git at publish time, or it was parked) is therefore
published as a **draft** with no `factory:ready` label, `Refs #N` instead of `Closes #N`, and `factory/gate` = `failure`
(`workflow files changed: a person must review the diff and apply factory-verified @<sha7>`), whatever its review and
verification said and also when the App's own push was accepted. A job retried to drop its workflow change is not held
by the paths it touched before: the hold follows what its new head changes. Nothing enrols a held job for auto-merge
until a person has read the diff of every workflow file and then applies the `factory-verified` label (it passes the
gate and turns `Refs` into `Closes`, `docs/handoff.md`), marks the pull request ready and applies `factory:ready`. The
job's status is `pr_incomplete`.

**What a person does** (there is no extra command). Run git in the repository's own clone, **not in the retained
worktree**: the worktree is writable by the worker, including the `.git` file that says where its repository is, and
git run there with your credentials would execute whatever a worker made that file point to (hooks, `core.fsmonitor`).
The supervisor itself does not run git that way: its calls on a worktree name the git directory explicitly, switch hooks and
`core.fsmonitor` off and stop with `worktree .git pointer was changed` when that file was rewritten, or with `worktree holds a
nested git repository that is not a submodule of it` when a worker left a repository inside the worktree (git would start a
child git in it and run its clean filter); remove that directory from the worktree with a plain file command, never with git run inside it.
The job branch is a local branch of the clone, so the clone has everything. `status` and the `run` output name the clone
(`Factory clone <path>`; in a local run it is the repository you ran `factory` in).

1. Look at the work, in the clone: `git -C <clone> log <base>..<branch>` and `git -C <clone> diff <base>...<branch>`.
   If you cannot reach the Factory clone (the manager runs in a container, say), use the bundle instead. It holds the
   job's commits without the base, so it cannot be cloned or verified on its own: make a fresh `git clone` of the
   repository (it has the base), then run `git bundle verify <bundle>` and `git fetch <bundle> <branch>:<branch>` in
   that clone, and inspect the branch there. **Read the diff of every file under `.github/workflows` before the next
   step.** Pushing a branch runs its `on: push` workflows at once, with the repository's secrets, before any pull
   request exists; a workflow change from a worker is code that will run.
2. Push the branch **with your own credentials** (a person who may change workflow files), as the same branch name:
   `git -C <clone> push origin <branch>` (or, from the fresh clone you fetched the bundle into, `git push origin
   <branch>`). The `workflows` permission stays off for the App.
3. Ask the operator to publish: `factory ... resume <job> --phase publish` (a second `resume` on the same head needs a
   `--reason`), then `run`. The resumed publish reads `origin/<branch>` once: when it already is the reviewed commit the
   push is skipped, and Factory opens a **draft** pull request, posts `factory/gate` = `failure` and moves the job to
   `pr_incomplete`; when a person's push left other commits on the branch, the job fails with `origin/<branch> has
   commits the local job branch lacks` rather than overwrite them; when nothing was pushed it parks again.
   `retry <job> --reason ...` is still allowed if the work should be redone (for example split so the workflow part is a
   separate job).
4. Release it: apply `factory-verified` to the pull request (the diff has been read), mark it ready for review and
   apply `factory:ready`. Until then it cannot merge.

**Keep the work until it is published.** The worktree, the local branch and the bundle live in the run directory. The
manager's janitor does **not** purge a run that has a `needs_human_publish` job just because it is idle: that commit
exists nowhere else, so such a run stays until a person publishes it (`resume`), retries it, or purges it on purpose
(`factory ... purge`). The janitor logs `operations: purge kept repo=<r> run=<r> reason=awaiting_person` once per manager
process when it would otherwise have called the run abandoned. A run that is waiting costs its worktree and clone on
disk, so do not leave one parked.

**Never grant the App `workflows: write` as the fix.** With it, a worker-influenced branch could change the deployment
pipeline that holds OIDC access to Azure. That is an operator decision; if it is ever taken it is recorded in the issue
that asked for it, not made by this change.

The exact text GitHub returns is confirmed only by a live rejection: the classifier is loose on purpose, the redacted
stderr is kept either way, and a refusal it does not recognise is a plain `failed` job with that stderr in `push_error`.

## Drain, pause, and what each leaves behind

| | `drain` | `pause` | SIGTERM of the container (pod termination) |
|---|---|---|---|
| What happens | Asks the supervisor to finish the **active phases**, start nothing new, and exit | SIGINT to the supervisor's process group; waits up to `FACTORY_MANAGER_PAUSE_SECONDS`; then `status` | SIGINT to every supervisor, then SIGKILL after `FACTORY_MANAGER_SHUTDOWN_SECONDS` |
| Work in flight | completes (a phase can take many minutes) | the supervisor terminates its live workers on the way out: a job with a live worker ends `failed` (`agent exited -15`), not `interrupted` (below) | same as pause |
| Checkpoints | kept; nothing is marked complete | kept in the retained worktrees | kept |
| Use it for | planned rollouts, key rotation, the cost guardrail | an emergency stop | Kubernetes stopping the pod (rollout, node upgrade or drain, delete) |

**What a pause or a shutdown leaves in the job** (checked on 2026-11-09 with a real supervisor and the fake agent of `tests/agentstub.py`,
one worker running, SIGINT sent to the supervisor, which is what `pause` and the shutdown send): the supervisor sends SIGTERM to the
process group of each live worker, waits for the worker threads and exits on the interrupt, and the worker's thread records the exit.
The job therefore ends **`failed`** with `agent exited -15; see <n>-<phase>.stderr` (-15 is SIGTERM), and `recover` finds nothing to
turn into `interrupted`. A job stays `running` and becomes `interrupted` at the next `recover` (or manager start) only when the supervisor
was killed before it recorded the end, for example because the shutdown grace ran out (`FACTORY_MANAGER_SHUTDOWN_SECONDS`), the node was
lost or the pod was replaced. The two are treated differently afterwards: a `failed` job is reported like any failed job (the outcome comment
in "Comments on issues" above), an `interrupted` one is not, and `auto_resume_after_restart = "once"` resumes only `interrupted` jobs, so a
person runs `retry` or `resume` for a job that a pause or an undrained rollout ended `failed`. `drain` first, so that neither happens.

`drain` is the polite one and is what you do before any planned change (`infra/README.md`), including a node image or Kubernetes
upgrade (there is one node: it evicts the pod, and the pod gets only `terminationGracePeriodSeconds`, 600 by default): it
returns when the request is recorded, not when the supervisor has exited. Wait until `GET /repos` shows
`supervisor: null` for the repository (the process has exited), and the run's `status` shows `supervisor`
null or with `active` false (status `drained`, `stopped`, `interrupted` or `stale`, never `running` or
`draining`) and `active_worker_count` 0 (and `active_worker_pods_unknown` 0: a session that runs as a pod and cannot
be read counts as running, see `docs/pod-backend.md`; no repository can run pods yet), before you deploy. The wait in "Safe deployments" below also holds for
the worker pods themselves (`GET /repos` `worker_pods`; slice 20 of the pod backend). A `status` supervisor record stays after the
run ends, so "empty" is not the test. A drained or paused run is continued with `run` again after the
interrupted jobs (after a pause, the `failed` ones that the pause ended with `agent exited -15`) are `retry`'d or `resume`'d; a run
**drained by the cost guardrail** is not (see Cost guardrails).

## Stopping one job

`drain` and `pause` act on a whole run. To stop **one running job** (it is going the wrong way, or its issue changed) use the **Stop** button on the
dashboard (**Operator** role; a viewer sees it disabled), or `POST /repos/<org>/<name>/runs/<run>/stop` with `{"job":"fix-a","reason":"why"}`, or
`factory --repo <org>/<name> --run-dir <run dir> stop fix-a --reason "why"` in the pod. The reason is optional, at most 200 characters, kept with the run
as plain text. Drain semantics: the worker's current step may finish for up to `FACTORY_STOP_GRACE_SECONDS` (default 120), then its process group gets
SIGTERM and, 5 seconds later, SIGKILL. The job starts nothing more and shows `stopped`; the supervisor and the run's other jobs go on.

* **What you see:** the job `stopped` (dashboard and `status`), its issue labelled `factory:blocked`, and in the history the outcome `stopped` with
  who stopped it and when. Asking twice is harmless: the second answer is the first.
* **What it leaves alone:** the pull request (if there is one), the branch and the worktree. It never closes or merges anything.
* **Continue it:** `retry JOB --reason "what changed"` for a new attempt in the retained worktree, or `resume JOB --phase review|verify|publish` from a
  saved checkpoint; then `run`. Either one drops the stop note, so the job can be stopped again. Nothing continues by itself, and the janitor treats a run with a stopped job like any unfinished run: it is purged only after `abandon_after` without activity.
* **When it says no:** `409` when the job is not running (it may have just finished: look at its status) or no supervisor is running the run (a paused or
  drained run, or a stale supervisor: use `recover`, `retry` or `run`); `404` for an unknown job. If the job finishes while the stop is pending, it is
  left as it finished.

## Safe deployments: pause, drain, apply, resume

Applying the Terraform restarts the Factory pod. With the label watcher running there is hardly ever an idle moment, so make the restart safe by
following these steps in order (`infra/README.md`, "Safe deployments", has the setup). This repository ships no deploy pipeline: run the steps by hand, or
from your own pipeline. `tools/infra_deploy_gate.py` is an optional script for steps 1 to 3 and 5 (`pause`, `drain`, `wait`, `resume`): it takes
`FACTORY_MANAGER_URL` and `FACTORY_MANAGER_AUDIENCE` from the environment and gets its Entra token from your `az login` identity, which needs the `Factory.Operator`
role. Without the script you drain by hand, as in "Drain, pause, and what each leaves behind".

1. **Trigger pause.** `POST /trigger-pause` with `{"paused": true}` (Operator only, audited, no new public endpoint) creates the marker
   `$FACTORY_DATA/trigger-paused`. While it exists the label trigger, the dependency resume and the restart auto-resume start no new plan or
   run. A run in progress is not affected, and the marker survives a restart. `GET /repos` shows `"trigger_paused": true`.
   `{"paused": false}` clears it (only this manual marker: a tripped stop-the-world breaker has its own marker `spend-breaker`, which this call and the
   resume step never clear, and `trigger_paused` in `GET /repos` is true while either exists). By hand: `curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json'
   -d '{"paused": true}' "$MANAGER_URL/trigger-pause"` (token as in `infra/README.md`, "Call the API").
2. **Drain.** When the apply is approved, ask every active `run` supervisor to `drain` (finish the active phases, start nothing new, release the lease):
   `POST /repos/<org>/<name>/runs/<run>/drain` for each. Write down which runs you drained: step 5 starts them again.
3. **Bounded wait.** Poll `GET /repos` until no repository has a supervisor, for at most 30 minutes (a supervisor that shows up meanwhile is
   drained too). A manager with the pod backend wired in also reports its worker pods (`worker_pods`: how many may still run, and whether the
   launcher could not be asked), and the wait then also holds while a pod runs or cannot be read: a rollout replaces the relay's spool, so every
   running pod would be cut off. A pod is waited for, never drained; the orphan sweep ends the pods of a supervisor that is gone within a few
   minutes. A manager that does not report the key (every manager today) adds nothing to the wait. If the bound passes, do not apply: nothing
   was changed, so clear the pause (step 5) and try again later. A manager that cannot be reached counts the same way.
4. **Apply and rollout.** `terraform apply`, then wait for the rollout (`kubectl -n factory rollout status deploy/factory`).
5. **Resume.** Clear the pause, then start `run` again for each run step 2 drained. Do this after the rollout check and also when the wait timed out or the apply or
   rollout failed, so a failed deployment does not leave the trigger paused. If the pause cannot be cleared (the manager does not answer
   for 5 minutes), say so loudly: **the pause is STILL SET**, and an Operator must clear it by hand (the command in
   step 1 with `false`): until then no label starts a build.

After the restart, two things continue the work, both only once the pause is cleared. (1) Step 5 starts `run` again for
every run it drained (a drained run is not `interrupted`, so nothing else would continue it); a run that cannot be started needs `POST .../run` from an Operator.
(2) What the new pod finds `interrupted` (it should not be anything, since
the wait drained every supervisor, but a crash can) is resumed once when the repository sets `auto_resume_after_restart = "once"`
(the default `scripts/register-repo.sh` writes for new repositories). The pod restarts while the trigger is paused, so recovery holds
that resume back and the recovery thread runs it as soon as the pause is cleared; the `auto-resumed` marker is claimed only when it
runs. A started run whose queued jobs wait for a dependency merge is picked up by the dependency resume once the pause is clear. A
drained run that step 5 did not start again (drained by hand, or not at all) needs `run` again, as after any drain (see above).

## What CI runs, and a bad merge

This repository ships one workflow, `.github/workflows/ci.yml`. It needs no secret and no cloud access, and it deploys nothing: it compiles the Python files,
runs the unit tests on Python 3.11 to 3.13, the token broker tests in their own virtual environment and the offline checks of `infra/`, and ends with `CI result`, the
one check to require (`docs/handoff.md`, step 4). It runs on every pull request to `main`, on every push to `main` and whenever a person starts it
(`gh workflow run CI --ref main`). Building, scanning and deploying an image, and planning and applying Terraform, are your own pipeline's work.

**What a pull request cannot catch.** Two pull requests that are each green can be red together. The push to `main` runs the full `CI` again on the merged tree, so
the red shows there; start `gh workflow run CI --ref main` after merging a risky pair if you want to know at once. The settings that would catch it at the merge
(require branches to be up to date before merging, or a merge queue) are repository settings, not code, and are **not enabled**; turn on "Require branches to be up
to date" in the ruleset if the delay is not acceptable.

**A bad merge, in order.** (1) Pause the trigger and drain running supervisors first if the bad build touches the manager or the supervisor ("Safe deployments").
(2) Revert on a pull request (`git revert <merge commit>`): it runs the full `CI`, so the previous behaviour returns through the normal path. (3) If your own
pipeline already rolled the bad image out, roll back by pinning the previous digest (`image_digest` in `infra/image.auto.tfvars`, or in your tfvars) and applying by hand
after draining ("Safe deployments").

## Recovery after a restart or a redeploy

A supervisor that died (SIGKILL, node loss, pod replacement) leaves jobs marked `running` with
a dead process, its lease held until it expires (about a minute for a blob lease), and its retained
worktrees. Nothing is lost, but nothing continues by itself unless you ask it to:

1. **The manager recovers on start.** It waits (up to `FACTORY_MANAGER_LEASE_WAIT_SECONDS`) until each
   repository's lease can be taken, runs `recover` for every run that has a state file (running jobs whose process is
   provably dead become `interrupted`; a process from another host counts as dead only because the lease expired),
   and writes one `restart repo=... run=... interrupted=N ...` line per run (a run whose pod session is still alive prints `... waiting=pod` instead and is asked
   again every 30 s, see `docs/manager.md`). It does **not** start a run. (The label watcher
   starts after it: it finishes a labelled plan that was waiting for its `run`, and plans labelled issues that are not in flight; it never
   resumes interrupted jobs, that stays `retry`/`resume` or `auto_resume_after_restart`, or a fresh `factory-build` label on a `factory:blocked` issue.)
   If the lease is still held when the wait ends (the Deployment strategy is `Recreate`, so the old pod is gone before the new one starts,
   but a supervisor that was killed rather than drained leaves its lease for up to the lease's own minute), the manager
   logs `restart repo=... lease busy|unavailable after Ns; recovery skipped` once, leaves that repository alone until
   the next restart and does not auto-resume it. Nothing is lost: `retry`, `resume` and `run` recover the run themselves
   once the lease is free.
2. **You decide per job.** `retry JOB --reason "what changed"` starts a new implementation attempt;
   `resume JOB --phase review|verify|publish` continues from a saved phase (earlier checkpoints are kept);
   `import-evidence` accepts verification obtained elsewhere. Then `run`.
3. **Or let the manager do step 2 once** (new registrations get this setting from `scripts/register-repo.sh`; the manager's own default for
   an entry without the key is `"never"`). A repository config with `auto_resume_after_restart = "once"` makes the
   manager `retry` each interrupted job with the reason "automatic resume after manager restart" and start
   `run`, **once per run directory ever** (a marker file `auto-resumed` is written first, so a crash loop cannot resume
   twice). Anything after that is manual.

If `recover`, `retry` or `purge` says a child is still alive or a pid was recorded on another host, the
previous holder may still be running: wait for the lease to expire or find that process; never delete state files.

## Where the settings live

`abandon_after`, `run_budget_usd`, `auto_resume_after_restart` and `auto_build`, `auto_build_label`, `auto_build_min_permission`,
`auto_build_concurrency`, `auto_build_max_issues`, `auto_build_timeout_seconds` are top-level keys of a repository's config
(`docs/configuration.md`; `docs/manager.md` for the `auto_build*` ones). So are `comment_on_blocked` and `observed_issues` (default `true`: Factory opens an
issue for what a builder or reviewer observed outside its job; "Issues opened by Factory" above). The `followup*` keys and `mention_handle` (an `@mention` on an open Factory pull request starts a session on its own
branch) are off by default: `followup = true` per repository turns them on, see "Follow-ups on pull requests" above and `docs/manager.md` "Follow-up requests" for the guards, the outcome codes and the log lines. `followup_reply` (default true) governs the reply on the pull request. `execution` (default `"inprocess"`) says where a repository's worker sessions run, all phases alike: `"inprocess"` is a child of the supervisor in the manager pod, as always; `"pod"` belongs to the pod backend program (`docs/pod-backend.md`) and is **not available in any deployment yet**: a repository that sets it has every session refused with a fixed text and nothing started (the job fails with it) while `FACTORY_POD_BACKEND` is not `on`, and nothing starts the launcher or the relay that a pod session needs (slices 16 and 17), so it cannot be switched on; there is no fallback to in-process execution. To go back, remove the line (a rollout, as below). On AKS that file comes from the `repo_configs` variable of the operator's tfvars (`INFRA_TFVARS`) through an immutable
ConfigMap (`factory-repos-<hash>`) mounted at `/config/repos`, so changing one is a **rollout**: drain every repository, apply, `run` again (`infra/README.md`, "Rotate secrets and
change repository configuration"). A budget added to a run that is already running does not guard it; the next `run` does.
Setting `auto_resume_after_restart = "once"` while jobs are interrupted makes the restart of that very rollout resume them.

**Registering a repository** (a new `repo_configs` entry, so a rollout as above) is scripted, as a dry run first, for
both modes: `scripts/register-repo.sh` (manual merge) and `scripts/enroll-repo.sh` (hand-off); see
`docs/enrolling-repositories.md`. Both print the drain, dispatch and verify steps; neither runs them. **Registering is enough to be
watched**: once the rollout is done, a `factory-build` label from someone with write access starts the work. `register-repo.sh --no-auto-build`
registers a repository without the watcher.

## Session time limit

Every agent session runs for at most `--timeout` seconds (default 1800); on the label-triggered path that is the repository key `auto_build_timeout_seconds` (60 to 86400,
default 1800; an out-of-range value means the default and the config still loads), passed to the planner and to the `run` the manager starts, including dependency resumes and a
resume after a restart. A running run keeps the limit it started with; the next `run` picks up a changed key (on AKS a config change is a rollout, see above).

A session that hits the limit is killed and its job ends `failed` with the fixed reason `timed out after N s`. Where to see it:

* `status --json`: the job has `"failure": "timeout"`, `"timeout_seconds": N` and `"error": "timed out after N s"`; `next_action` says to raise the key or split the issue.
* The outcome comment on the issue (`failed`) says the agent session ran out of time and advises the same.

**Raise `auto_build_timeout_seconds`** when the issue is one coherent change that needs more time (a large cohesive refactor, a slow test suite) and the builder was making
progress when it was killed. **Split the issue** when it is several changes in one, or when raising the limit repeatedly does not help: a longer limit lets an oversized job run
longer and cost more per attempt. The planner is told the limit and gives grouped issues that would not fit separate jobs, saying so in its disposition reason (one issue is always one job, so a single oversized issue is raised, not split: raise the key or split the issue yourself). For a manual `plan` and `run`, pass the same `--timeout` to both: the planner is told the `plan` one. After raising the key (and rolling it out), apply the trigger
label again, or `retry` the job.

## Model and effort

The Claude model and effort are chosen, most specific first, by: the issue's `factory-model:<alias or id>` and `factory-effort:<level>` labels
(build and verify only), the repository's per-phase `[models]` / `[efforts]`, the repository's `model` / `effort` defaults (plan, build, review and verify;
never the preflight probe, so a Sonnet or Opus default does not make the 0.5 USD capability probe dearer), and the built-in table. The keys, the alias table
and the validation rules are in `docs/manager.md` ("Model and effort"); the repository key table is in `docs/configuration.md`.

**Who may set what.**

| Who | May set | Checked by |
|---|---|---|
| the operator, in the repository's config file | `model`, `effort`, `[models]`, `[efforts]`, `[aliases]`, and the guard rails `allowed_models` and `max_effort` | the config loader at every start (unknown or malformed values refuse the file); on AKS an edit is a rollout, see "Where the settings live" |
| a person who holds `auto_build_min_permission` (`write` by default) or more, on one issue | one `factory-model:` and one `factory-effort:` label, for the build and verify of that issue only | Factory, when the issue is planned: the newest applier of each label must be a user (not a bot) with that permission, then the value must be an alias or an id in `allowed_models`, and an effort not above `max_effort` |
| nobody, in this version | the reviewer's and the planner's model or effort per issue; anything written in an issue body, title or comment | those are never read |

Applying a label needs only triage access, which is below the bar for `factory-build` (`auto_build_min_permission`), so Factory checks who applied each choice label as well: at planning time the newest
applier of every `factory-model:` and `factory-effort:` label of the issue must be a user (not a bot) holding at least `auto_build_min_permission`, or the issue is blocked with `label-not-authorised`
(an unreadable answer from GitHub stops the plan with `factory: could not establish who applied the model or effort label` instead, and nothing is planned). A triage-level user can therefore not move the spend of an issue a
maintainer queued. Even an authorised label is never trusted: the model must be one `allowed_models` names (by default the built-in ids plus the ids the
repository's file already uses, so a repository that sets `model = X` can select X) and the effort must not be above `max_effort` (by default the highest effort the built-in
table and the file already use, so `high` unless the file raises it). **A repository that wants to bound what even its writers may ask for sets `allowed_models` and `max_effort` to what it
accepts**; an explicit `allowed_models` is exact, and an empty list forbids every model label.

**What a refused label does.** It never falls back to a default (a person who asked for Opus must not silently get Sonnet). The issue gets a `blocked` disposition with a fixed reason, **no
job is created and no worker or planner session starts for it**, it carries `factory:blocked`, and the outcome comment ("Comments on issues") says which rule it broke without quoting the label. Fix or remove the
label and apply `factory-build` again: no job exists for the issue, so it is planned afresh and the labels are read again. The reason codes are in the log as
`factory: issue directive refused issue=N reason=<code>[,<code>]` (the issue number and fixed codes only, never an issue's text or a label's). Issues the planner would put into one job with
different choices are blocked the same way (the planner is told to keep them apart; an issue without a label differs from one with a label), together with the jobs that depend on them.

**Cost implication.** Model choice is the largest cost lever Factory has: in the measurements behind issue 85 the Opus reviewer was about a third of the model spend and the verifier about 1.6 %. A label can
raise the cost of one issue (an Opus builder and verifier at a higher effort) or lower it (Haiku, low effort); `allowed_models` and `max_effort` bound the worst case, and the per-invocation caps (`--max-budget-usd`
per phase, `[budgets]`) and `run_budget_usd` are **not** changed by the model choice, so a more expensive model reaches its cap sooner and a job can end `failed` on budget. The reviewer is deliberately
independent of the builder: a cheaper reviewer is a repository decision (`[models].review`, `[efforts].review`), not an issue's. Spend per model is visible in `status --json` (`usage[].model` of each job).

**Where to see it.** The resolved model and effort of a job's build and verify, with where each came from (`built-in`, `repository`, `repository-phase`, `issue`), are recorded once in `plan.json` (`choice`),
shown by `status` (`Models: build ... ; verify ...`, `models` in `--json`) and in the pull request's `## Models` section. They are resolved when the issue is planned (an alias is resolved then too), so a
retried, resumed or restarted job, and a follow-up session on its pull request, runs with the same values even if the file changed in between; to apply a changed setting, plan the issue again.
Extended thinking is not chosen here: it is tracked in issue 90.

### Comparing efforts: the read-only effort report

The default efforts should rest on measured tokens, cost, turns and outcome rather than on assumption.
`tools/effort_report.py` is the read-only half of that: it tabulates what the run directories and the history ledger already hold, per effort. **No experiment has been
run, and nothing in the report is a conclusion.** Running one needs live sessions and a person to choose the sample and to judge whether the work was good; the issue stays
open for that (the harness for the experiment is "Running the effort A/B" below). The script only reads: no network, no GitHub call, nothing written, regular files only (never a symlink), every read bounded, and its output is numbers
and a fixed vocabulary (efforts, phases, outcomes, `claude-...` model ids): never an issue, pull request, comment, prompt or log text, never a run or job name.

**What a "labelled experiment" means here.** The data holds two things, so the script has two filters and no more. `plan.json` records, per job, the resolved build and verify
model and effort and where each came from (`built-in`, `repository`, `repository-phase`, `issue`; "Where to see it" above). A job whose effort came from an issue's
`factory-effort:<level>` label has the source `issue`, so `--labelled` keeps exactly those jobs. The sample itself is the person's: `--issues 12,15 --issues 20` keeps the jobs whose
issues (recorded in `plan.json` and in the ledger) are in that list. The two combine. Without either flag every job found is in the tables, the ones on the repository's defaults included.

**What it reads, and which source wins.** A job is a (repository, run, job id) triple. A run directory is matched with the ledger by its path, which must end in
`repos/<org>/<repo>/runs/<run>`, so a copy kept anywhere else counts as a run of its own.

| Figure | Source | Notes |
|---|---|---|
| effort and model of a job | `plan.json` `choice` (build phase) | the job's effort is its **build** effort; a session of the verify phase is shown with the verify effort in the per phase table. The ledger does not record the effort: a purged run, a job planned before the choice was recorded and an unreadable `plan.json` all land in the row `unrecorded`. The reviewer's and the planner's efforts are not recorded per job, so review sessions are `unrecorded` in the per phase table. The planner's own sessions belong to no job and are left out. |
| tokens, cost, turns of a session | `state.json` `jobs.<id>.usage[]` (one record per `claude -p` session) | tokens are read with the same code as `status` (`status.usage_tokens`) and the hit rate is `status.cache_hit_rate`. A session whose record has no token count (it ended without a result) is a session and is in no figure; its default zeros for cost and turns are not figures either |
| the same, for a purged run | the ledger's `job` entries (cumulative tokens per phase, cumulative cost) | no turns, no model and no session count there: the job counts in the job tables, with its tokens and cost, and in no turns figure and no per phase row |
| outcome | `history.outcome_of` | when the ledger says `merged` or `closed` (GitHub's word, seen by a status read; `state.json` keeps `pr_ready` for a pull request a person merged) it wins; otherwise the run directory's status decides; otherwise the ledger's word (`abandoned` for a run purged before its jobs finished). A job with no outcome yet is `other` |

**When both sources have the same job** the run directory wins everything it has (effort, model, issues, the per-session figures); the ledger's figures stand in only for a job whose run directory has no
measured session, and its `merged` or `closed` word wins the outcome as above. `--check` says how many jobs are in both and in how many the token sums or the cost sums differ: the ledger's
counts are those at its last recorded transition, so a small lag is normal, a large number is not. The ledger is read newest first up to `--ledger-mib` (default 8 MiB, at most 128); older events are left
out and the report says so.

**What it prints.** Per effort (`low` to `max`, then `unrecorded`), and again per effort and model: the number of jobs, how many of them have usage data, the sessions, the outcome counts
(`merged`, `closed`, `blocked`, `abandoned`, `stopped`, `other`), the cache hit rate, and for input, output, cache write and cache read tokens, cost and turns the **n, mean, median and total**.
n is the number of jobs (or sessions, in the third table: per phase, effort and model) that have that figure, so a reader never mistakes three sessions for a trend. Cost is the CLI's client-side estimate,
not billing data. The report ends with a plain line that a difference between efforts is not evidence without repeats.

**Running it.** The image does not ship `tools/`, so an operator pipes the script into the pod's Python (the example only; running it is the operator's action, through `factory-entrypoint` like the other CLI
commands; the report itself writes nothing):

```sh
kubectl -n factory exec -i deploy/factory -- /usr/local/bin/factory-entrypoint /opt/factory/bin/python - --check /data < tools/effort_report.py
kubectl -n factory exec -i deploy/factory -- /usr/local/bin/factory-entrypoint /opt/factory/bin/python - --labelled --issues 12,15,20 --json /data < tools/effort_report.py
```

The path is the data directory (everything below it is searched; a run directory is never entered and the managed clone is skipped), the `runs` directory, one run directory, the `history` directory or one ledger segment.
On a workstation, `python tools/effort_report.py --check DIR` works the same on a copy of those directories, with the `factory` package installed or the checkout's `src/` beside the script. Run it **before** the janitor
purges the runs of the experiment: the effort exists only in `plan.json`, so a purged job still counts (with its tokens, cost and outcome from the ledger) but under `unrecorded`. Recording the effort in the ledger
would lift that limit; it is not done here.

### Running the effort A/B

`tools/effort_ab.py` is the half of issues 132 and 154 that **makes** the data `tools/effort_report.py` tabulates: a controlled experiment in which effort is the only variable. It runs against a **sandbox repository** through GitHub issues
and Factory's own label trigger, one issue per (task, effort, repeat) with identical title and body. It prints fixed text and numbers only (never an issue, a task title or body, a pull request path, a log, a token or a gh message), uses `gh` through argv lists
(no shell, every call bounded to 60 seconds) and needs the standard library plus the checkout's `src/`. **It has not been run for real**: the first run is the pilot below, by an operator, and nothing it prints is a conclusion.

**Before the first run (the sandbox).**

* A repository that is registered with this Factory and whose pull requests may all be closed unmerged, with `auto_build = true`. Use a **repository of its own**; never a repository whose pull requests matter.
* It must have **no auto-merge**: no `.github/workflows/factory-automerge.yml` (`handoff/`) and no ruleset that holds a merge for `factory/gate`. The harness never merges, but a repository that merges green pull requests would change the base commit under it; the harness stops (exit 4)
  at a pull request that is merged or has auto-merge enabled.
* Set `run_budget_usd` in the repository's config to a small number (each arm is its own run, so it is a per-arm cap; "Cost guardrails"), and make sure `max_effort` allows every effort you test (the built-in ceiling is `high`; a label above it blocks the issue, and the harness records `blocked_before_job`).
* Keep the per-phase budgets (`[budgets]`) high enough that no arm is cut short by a cap: a session ended by its cap (its `subtype` is not `success`) makes that arm's cost and outcome incomparable. The harness records each session's `subtype`, so a cut-off shows in the record file.
* Whoever runs the harness applies the labels with their own `gh` login: that login must be a **user** (not a bot) holding at least `auto_build_min_permission` (default `write`), or the manager refuses the effort label and the trigger.
* The harness needs the **run directories** to read tokens and cost: a readable copy of the manager's data directory. Point `--data` at it and, to keep it current, give `--refresh-json` an argv list that is run before each poll (nothing it prints is shown), for example
  `'["kubectl","-n","factory","cp","<pod>:/data/repos","/work/abdata/repos"]'` (`kubectl cp` needs `tar` in the container; any other way to copy the run directories works). Without `--data`, give `--cost-file` and the cost is read from there, but the record then has no token figures.

**The task file** (JSON; a list, or `{"tasks": [...]}`; 1 to 50 tasks; small, real, independent tasks that touch a few files each):

```json
[{"id": "add-flag", "title": "Add a --verbose flag to the CLI", "body": "What to do, in the words a person would use.",
  "expected_paths": ["src/cli.py", "tests/test_cli.py"], "acceptance": ["--verbose prints each step", "the existing tests still pass"]}]
```

`id` is `[a-z0-9-]`, up to 32 characters, and is the only part of a task that is ever printed. The issue is the title, the body and the acceptance list as bullets, the same for every arm. `expected_paths` is used only to count how many of them the pull request touched.

**What an arm is.** The efforts (`--efforts`, default `low,medium,high`), the repeats per arm (`--repeats`) and the tasks make the arms. **The order is randomised from a recorded seed** (`--seed`; by default random and written to the state file), in blocks: the (task, repeat) blocks are shuffled and the efforts inside each block,
from a SHA-256 of the seed and the names, so the same seed gives the same order on any Python and whatever the order of the task file; an experiment stopped early is still balanced across efforts as far as it got. Arms run one at a time. Each arm's issue carries `factory-effort:<e>`, `factory-model:<alias>` (`--model`, default `sonnet`, so the model is not a variable either)
and `effort-ab:<name>` (the experiment label), and **the trigger label (`factory-build`) is added last**, so the manager plans and builds the issue exactly as it does for a person. The effort the plan recorded for the job is compared with the label and recorded as `effort_confirmed`.

**Commands** (run from the checkout; the tools directory is not in the image):

```sh
# 1. the plan and its cost estimate, no call, nothing written
python3 tools/effort_ab.py run --dry-run --tasks tasks.json --efforts low,medium,high --repeats 3 --seed 20261110

# 2. the pilot: one repeat of every task at every effort, then the spread
python3 tools/effort_ab.py pilot --tasks tasks.json --state ab.json --repo ORG/SANDBOX --data /work/abdata --confirm-sandbox \
  --refresh-json '["kubectl","-n","factory","cp","<pod>:/data/repos","/work/abdata/repos"]' --budget-usd 15

# 3. the experiment (the same state file: the pilot's arms count as repeat 1 and the new repeats follow them)
python3 tools/effort_ab.py run --tasks tasks.json --state ab.json --repo ORG/SANDBOX --data /work/abdata --confirm-sandbox \
  --refresh-json '["kubectl","-n","factory","cp","<pod>:/data/repos","/work/abdata/repos"]' --repeats 4 --budget-usd 60

# 4. again, with the same arguments, after any stop: it continues
# 5. the numbers of a state file, without a call
python3 tools/effort_ab.py report --state ab.json --data /work/abdata
```

`--confirm-sandbox` is required for any run that calls GitHub. The state file is the experiment: it holds the plan, the seed and every arm's progress (atomic writes, mode 0600); running again with the same repository, tasks, efforts, model, seed and name continues, **never creates a second issue for an arm** (an issue created
but not yet recorded is found again by its experiment label; two candidates are refused, not guessed) and never adds the trigger label twice. Only the repeats may grow, and the new arms follow the old ones. `--records` (default `<state>.records.json`) is the record file, rewritten after every arm.
Other options: `--poll-seconds` (60), `--arm-timeout-minutes` (120), `--ci-wait-minutes` (15: how long a published pull request's pending checks are waited for), `--max-hours` (12 for the whole run), `--trigger-label`, `--cost-file`, `--delete-branches`.
Exit codes: `0` done, `2` refused (arguments, files, a state of another experiment), `3` budget stop, `4` stopped for safety or a failed call (the state is kept), `5` time limit (the state is kept).

**The budget guard.** Before it starts each arm the harness takes *spent so far* plus *the estimate for that arm* and refuses to start it when the sum would pass `--budget-usd` (default 10): it prints `budget stop: spent_usd=... next_arm_estimate_usd=... budget_usd=...`, keeps the state and exits 3; a later run with a larger budget continues.
*Spent so far* is the sum, over the arms started, of the arm's cost (its sessions' `total_cost_usd` plus the planner's, read with the readers `effort_report` uses, and kept in the state when the arm finishes, so a purge of the run does not lose it), an arm whose cost cannot be read counting as its estimate; with `--cost-file` (`{"cost_usd": N}`, the experiment's cost so far)
that number replaces the sum. *The estimate* is the dearest finished arm of the same effort, else the dearest finished arm of any, else `--arm-estimate-usd` (default 3.00). It cannot stop a session that is already running: the per-phase caps (`--max-budget-usd`: plan 5, build 8, review 4, verify 6) and `run_budget_usd` bound one arm, and the dry run prints that ceiling.
Cost is the CLI's client-side estimate, not billing data.

**Cost estimate.** `arms = tasks x efforts x repeats`; `expected cost = arms x mean cost per arm`, where the mean per arm is measured (the pilot prints it per effort: planner, build, review and verify sessions together; higher efforts cost more) and until then the default is `--arm-estimate-usd`. The dry run prints `per_arm_usd`, `total_usd` and how many arms fit the budget with that estimate.
Example: 6 tasks x 3 efforts x 3 repeats = 54 arms; at a measured 2.50 per arm on average that is about 135 USD. Set the budget below what you are willing to lose, not at the estimate.

**How many repeats.** The pilot (one repeat) prints, for each effort against the next, the mean and the standard deviation across tasks of the **relative cost difference** (e.g. `low -> medium tasks=6 mean_relative_cost_difference=45.0% sd=30.0%`) and the repeats per arm needed to detect a difference of `--detect` (default 25 %) in cost with a paired comparison, 80 % power and a 5 % two-sided test: pairs needed
`n = 7.85 x (sd / detect)^2`, repeats `= ceil(n / tasks)`, the largest over the effort pairs. Read it as a rough guide from a handful of numbers (a pilot's standard deviation is itself uncertain; double it if the budget allows). **It sizes the cost comparison only.** Whether a lower effort produces worse pull requests is a rate of a yes/no judgement
and needs many more arms than a cost difference does; with the numbers a budget buys, expect to see whether the effort changes cost, turns and thinking tokens and to find **large** quality differences only. A smaller spread than the cost difference you care about means one repeat may do; a spread that is larger means more repeats or more tasks of the same kind.

**What it records per session** (the record file, and the state): `usage.output_tokens` and `input_tokens`, `thinking_tokens` (only when the CLI reports them in `usage`: `thinking_tokens` or `output_tokens_details.thinking_tokens`; otherwise `null`), `cache_creation_input_tokens` and `cache_read_input_tokens`, `modelUsage` per model (numbers only),
`total_cost_usd`, `num_turns`, `duration_ms` and the `subtype`. **Per arm, the outcome that matters:** the job's final status, `effort_confirmed`, whether verification was complete and matched the head, whether the review passed and **how many findings** it had, whether the pull request is **incomplete** (`pr_incomplete`), the **gate** state (`factory/gate`: `success`, `pending`, `failure`, `error`, `none`)
and the **CI** state (`passing`, `failing`, `pending`, `none`) when the harness last looked, the files the pull request changed, how many of the `expected_paths` it touched and how many files it touched outside them, the attempts, and the planner's cost. No issue text, pull request text, finding or path is stored. The harness prints, per effort, the counts and each figure's n, mean, standard deviation, minimum and maximum, then calls `effort_report`
for its table on the experiment's issues (`--issues` the experiment's numbers, `--labelled`). Because every pull request is closed unmerged, that table's outcome column says `closed` for every arm; the harness says so beneath it, and the outcomes that matter are in the record file.

**How quality is judged.** Not by the harness. It records proxies (verification, review findings, gate, CI, incomplete, expected files touched) that Factory's own checks produced; **a person reviews the pull requests** of the arms, ideally without seeing the effort (the arm key in the state names the effort; the branch and pull request do not). A closed pull request keeps its diff and its branch (`--delete-branches` is off by default),
so the review can happen after the experiment. A cheaper arm that is incomplete or fails review more often is not cheaper. Nothing in the report is a conclusion.

**Safety rules.**

* **It never merges and has no call that could.** The client class is the only place with GitHub calls and its allowlist is label create, issue create/edit/view/list/close, pr view/close and one ref deletion (`DELETE git/refs/heads/<branch>`, only with `--delete-branches`, only for the head branch of a pull request of the sandbox repository other than the base): no merge, no auto-merge, no push, no force push. A test fails if a merge call appears.
* **After each session** it records the outcome, then **closes the pull request without merging** and closes the issue as not planned, so the base commit stays identical for every arm. The branch is **left** unless `--delete-branches`. A pull request of another repository is never touched. A pull request found merged ends the experiment for good (the base commit moved; start a new one); an auto-merge request is ended by the close and stops the run once (exit 4).
* **It cannot stop a running session.** If an arm times out (`--arm-timeout-minutes`), the harness records `timeout`, closes up and stops (exit 4): stop the job in the manager (`POST .../stop`, "Stopping a job") before running again, or its cost lands outside the budget.
* One arm at a time; a state write after every step; every call and every wait is bounded; three failed polls in a row stop the run with the state kept.
* The experiment spends real money in the Factory's Anthropic account and writes issues, labels and closed pull requests to the sandbox repository. Nothing here is run by CI or by the manager.

## Cost guardrails

* `run_budget_usd` (per repository, optional): the manager polls `status` for every run it started every
  `FACTORY_MANAGER_POLL_SECONDS` (60) and, when the run's `cost_usd` reaches the limit, issues a
  `drain` once (marker `budget-drained` in the run directory). The run finishes its active phases, so the final
  cost can exceed the limit by the phases in flight; the figure is a client-side estimate from the agent's reports.
  `cost_usd` is cumulative, so a budget drain is **not** continued with `run` alone: a run started again while it is
  still at or above the limit is guarded afresh and drained again within one poll, after it has paid for another
  phase. Raise `run_budget_usd` above the run's current `cost_usd` (or remove the key; on AKS that is a rollout, see
  above), then `run`. A restart with `auto_resume_after_restart = "once"` does not resume a run that is already at its
  budget (`resume ... skipped=over-budget`; the once-marker stays free).
* **The stop-the-world breaker** (`docs/capacity.md`, "Stop-the-world breaker") reacts to the API errors that retrying cannot fix. A
  spend-limit 429 trips it at once; a refused key (`auth`) trips it at 2 in 5 minutes and an unclassified 429 (`rate_limit_unknown`) at
  3 in 5 minutes; a rate limit with a `retry-after`, an overload and any other error only count in `factory_api_errors_total`. A trip
  sets its own marker `$FACTORY_DATA/spend-breaker`, so the trigger starts nothing (`GET /repos`: `trigger_paused: true` and
  `breaker: {"code", "at"}`) and every run in progress is drained once. **It never resumes by itself.** After you raised the spend
  limit (or replaced the key), clear it: `curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json'
  -d '{"clear": true}' "$MANAGER_URL/spend-breaker"` (Operator only, audited; token as in `infra/README.md`, "Call the API"), then
  `run` the drained runs again. The deploy gate does not clear it, on purpose: its `resume` posts `paused: false` after every rollout
  it brackets and clears only the manual `trigger-paused` marker (a gate summary that says "trigger resumed" is then about that
  marker; check `GET /repos`).
* **Label-triggered builds** are bounded by the same limits: `run_budget_usd` guards the automatic runs too (the loop starts them through the same
  function as the API, so the guardrail sees them), at most `auto_build_max_issues` (3) issues are taken per cycle, and only one plan or run
  per repository runs at a time. A retry of a run that the guardrail drained is drained again within one poll if its cost is still at the limit (raise
  `run_budget_usd` first).
* Per-invocation caps: each worker call has its own dollar cap (`--max-budget-usd`: plan 5, build 8, review 4, verify 6,
  preflight 0.5 by default; `[budgets]` in the repository config overrides them).
* **The hard stop is the Anthropic Console, not Factory.** Create a workspace for Factory, put its API key in Key
  Vault, and set a monthly **spend limit** on that workspace: Console, Workspaces page, select the workspace, **Spend
  limits** (limits can be set lower than the organization's, not on the Default Workspace;
  <https://platform.claude.com/docs/en/manage-claude/workspaces>). Set an alert threshold too. Factory's own numbers are
  estimates and a drained run does not stop anything it does not own.

## Verification baseline

When the verifier reports a structured `test_run` (the exact test command plus the ids of the failing and erroring tests) with failures, Factory runs **that same command at the job's recorded `base` commit** and counts only head failures missing from the baseline (set difference by test id) as real. Failures present on both are *environmental*: listed in the PR body, and they do not make the PR incomplete when the verifier named them the only reason for `complete=false` (`test_run.sole_blocker`). A test that passes on the base and fails on the head always makes verification incomplete, and the blocker names it.

* The baseline runs only in a sandboxed worker session (phase name `verify-baseline`, the verify phase's policy and network allowlist) in a detached worktree of the base, removed afterwards; never as a supervisor subprocess. The base comes from the job state and the command from the verifier's output, never from issue text.
* Results are cached per (repository, base commit, test command) in `${FACTORY_DATA}/repos/<org>/<name>/baseline/`; a new base or a different command runs a fresh baseline. Delete the directory to force fresh runs.
* If the baseline cannot finish (timeout, crash, unparseable result), the verifier's verdict stands unchanged and the reason is recorded under `baseline.reason` in the PR body's Verification section, which also shows the command, base SHA, cache hit or miss, new failures and environmental failures. Without `test_run` verification behaves as before.
* What the body prints is bounded: each list of ids (the new and the environmental failures in the `Baseline comparison` block, and every list in the verification verdict that is dumped as JSON below it) shows at most 20 entries, each cut to 200 characters, then `and N more`, and the JSON dump is cut at 16,000 characters. `state.json` keeps the whole record (up to 5,000 ids per test run), so a run with hundreds of environmental failures still publishes. The block right after it, `Where the checks ran`, says what ran in the sandbox (the command, the base commit, the baseline status and the failing, new and environmental counts), which required checks the gate awaits and, once the gate resolved, each check's conclusion on the head, and the operator items; `docs/handoff.md` ("The pending gate") has the rules for that update.

## False-incomplete rate

Is verification crying wolf? `GET /repos` shows, per repository, `false_incomplete` (field shape: `docs/manager.md`,
"GET /repos"): of the pull requests Factory **published as incomplete or pending** (a `factory/gate` that is a
`failure`, or a pending one waiting for CI; `pr_incomplete`) that a person **merged**, the share merged with **the same
head commit Factory published**, with no change after the verdict. Such a pull request was held back for nothing.

* **Targets.** Under **10 %** is healthy. If the rate stays above **25 %** over a meaningful sample (the minimum of 10
  counted pull requests, and in practice a few weeks of them), the verification design needs revisiting: the verdicts
  are stopping work that people accept as it is.
* **Reading it.** `rate` is `merged_unchanged / counted` over the last 30 days (by merge time). It is `null` while
  `sample` is `below_minimum` or `empty`: a handful of pull requests says nothing, so do not read a number into it.
  `counts.merged_changed` are the verdicts people acted on (they pushed before merging), which is the system working.
  Read `by_reason` before the total: a high rate on `env_noise` or `ci_pending` points at the baseline comparison or at
  CI waiting, a high one on `real_failure` at review or verification itself. `workflow_hold` is a deliberate hold
  that a person always lifts, so it is merged unchanged by design: it is **left out of the headline `counted` and
  `rate`** (the targets apply to the headline), counted in `counts.workflow_hold` and shown in its own `by_reason` row.
  A code with fewer than 10 counted pull requests has no rate of its own. `counts.unresolved` are merged pull requests
  whose published commit could not be found (more than five commits of later pushes, or a force-push): they are in no
  rate, and because every unchanged merge is found, a large `unresolved` makes the rate an upper bound.
* **What counts.** The sample is the Factory App's pull requests merged in the window. The published commit and the
  verdict are read from the `factory/gate` statuses the Factory App itself wrote on the pull request's commits (the
  head, or the nearest of the five commits below it with a verdict), with the `[code]` suffix of the description as the
  reason. A pull request whose first verdict was `success` is not in the sample, and neither is a pending gate that the
  required checks turned green (`required CI passed`): the design worked. A person applying `factory-verified` after an
  incomplete verdict does not remove it from the sample: that is the person overruling it. Statuses written by anyone
  else are ignored. A merge of a later head (including a base branch merged in by hand) is `merged_changed` as long as the
  published commit is within five commits below the head; beyond that it is `unresolved`, not `merged_changed`.
* **Closed without merging is excluded** from the rate and from every other count except `closed_unmerged`. Closing says
  nothing about whether the verdict was right (the work may have been wrong, replaced or abandoned), and counting it
  either way would hide or inflate the rate. Open pull requests are not yet decided and are not counted.
* **Where it comes from.** Only GitHub, never the run directories: the janitor purges terminal runs, and those are exactly
  the merged ones, so a window built from run directories would lose them. The janitor sweep (no new thread) rebuilds the
  value from scratch at most every hour per repository, read-only: up to five pages of closed pull requests, then for each
  merged one (at most 100, newest first) one paginated status read of the head and, when the head carries no Factory
  verdict, the commit list and up to five more status reads. The worst case is therefore about 700 sequential `gh` calls
  per repository per hour, in the janitor's thread (30 s timeout each; a shutdown stops the pass between pull requests). A restart starts at `checking` and the first sweep rebuilds it. It needs `FACTORY_GITHUB_APP_SLUG` (the
  author of the pull requests); without it the value is `unavailable`. A failed refresh is one fixed log line
  (`false-incomplete repo=... refresh failed error=<type>`) and the old value stays, marked `stale`. Pull requests
  published before reason codes (#102) have no code: they are in the totals and in `counts.uncoded`.

## Purge

`purge` deletes everything a finished or abandoned run left (and then releases the run's issue labels, below): its worktrees, its local `factory/<run>/*`
branches and the run directory. Remote branches, pull requests and the issues' text are never touched; an issue is only changed to release its `factory:*` status label. It refuses unless the run is
provably dead (no live child, no running supervisor, foreign pids only with the lease).

* **Automatic.** Every `FACTORY_MANAGER_JANITOR_SECONDS` (900) the manager purges a run that is **terminal** (it has jobs
  and every one is `merged` or `closed`, by job state or by the pull request's state on GitHub) or **abandoned**
  (no live worker and nothing written for longer than `abandon_after`, default 7 days, per repository:
  `"7d"`, `"36h"` or seconds, at least an hour). Nothing keeps a run alive but activity: a job waiting for you in
  `pr_ready`, `ci_failed` included, does not, and open pull requests are left on GitHub untouched. The one exception is a
  run with a `needs_human_publish` job: its finished commit is not on GitHub, so the janitor never calls that run
  abandoned (it says `purge kept ... reason=awaiting_person` once; "Jobs that change `.github/workflows`"). **Work done by hand
  in a retained worktree does not count as activity** (only the run's own files do) and is deleted with it, so commit
  and push it or raise `abandon_after` first. After the last run directory of a repository is gone, the managed
  clone is removed too (`purge-clone`, retried at every tick until it succeeds); it is cloned again on the next
  `plan`. A run that is busy or refused is skipped and retried at the next tick.
* **Safe against a late operator.** The janitor tells the CLI what it saw (`purge --unchanged-since`), and the CLI
  refuses under the lease if `retry`, `resume` or `import-evidence` touched the run since. One window remains: a
  `run` started in the second between the janitor's registry check and its `purge` takes the lease after the
  purge and finds nothing; start it again (the run is gone, so `plan` anew). It needs a run that has been idle for
  days and an operator returning in that second.
* **Labels are released.** Once the run directory is gone, `purge` (the janitor's terminal and abandoned purges
  run the same command) takes the `factory:*` status label off the run's issues, so an abandoned run does not leave
  `factory:building` behind and the issue can be planned again. A job whose pull request is
  merged (persisted, or found live) leaves `factory:merged`; one whose pull request is still open (or cannot be
  looked up) keeps its label; a closed pull request, a job without one (queued, running, waiting, failed,
  blocked, interrupted, closed) and the issues the plan only investigated or blocked lose theirs. **An issue that
  another run's `plan.json` in the same runs directory lists is left alone**, because that run may own the label now. This is best
  effort: a failure is one stderr line, never a failed purge, and a purge that is refused changes no label. The
  `Purged run ...` line on stdout is unchanged. A purge without a managed repository (local mode) releases the
  labels through `gh` too, so without a usable `gh` it prints that one stderr line and still exits 0.
* **Pull requests merged or closed after the run went idle.** While a run still exists, the janitor's sweep and the
  trigger pass run the CLI `reconcile` for it (at most every five minutes per run; only a run with a published pull
  request, or with a finished job whose observations have no issue yet; never beside a supervisor): a merged pull request puts `factory:merged` on the issues of its job, one
  closed without merging clears the `factory:*` label (never an issue another run's plan lists, never one labelled
  `factory-ignore`), with the same best-effort rules as above. The same step answers the `factory-verified` label on
  an incomplete pull request (permission and head checks; `Refs` becomes `Closes`; `factory/gate` is posted on
  that head once, not on every pass): see `docs/handoff.md`, "After the supervisor is done", which also gives the manual alternative
  (close the issue by hand and link the evidence) and why incomplete pull requests default to `Refs`.
  **`reconcile` writes only labels, one pull request body and commit statuses. It never changes a job's status in
  `state.json`, so it never sets one to `merged` or `closed`**: a job whose pull request merged keeps reading `pr_ready`
  (or `pr_incomplete`) in `status`'s stored state, in `factory_jobs` and in the dashboard's raw status until its run is purged. The janitor's
  terminal verdict does not use the stored status: it uses the live `handoff_status` that `status` takes from GitHub (a pull request
  that is `merged` or `closed` there counts as finished), and the `FactoryJobsWaitingForHuman` alert counts only
  `needs_human_publish` and `blocked` jobs, so a merged `pr_ready` job is never "waiting for a person" (`docs/metrics.md`).
* **Why a purge or a reconcile was skipped.** The janitor's `purge-skipped repo=<r> run=<r> reason=<..> rc=<n|timeout>` and
  `reconcile-skipped repo=<r> run=<r> rc=<n|timeout>` lines end with `cause=<code>`, taken from the first `factory: ...` line of the CLI's
  stderr and matched against a fixed table; the text itself is never logged. `unchanged_since` (the run was used after the janitor judged it
  finished; the next sweep looks again), `alive` (a child process group or the supervisor is still alive), `pod_live` (a pod session of the run is alive or cannot be read), `foreign_pid` (a pid recorded on another
  host while the CLI holds no blob lease: `FACTORY_LOCK_ACCOUNT_URL` is not set for it, or the lease is not a blob lease),
  `branch_checked_out` (a `factory/<run>/...` branch is checked out outside the run's own worktrees), `other_repo` (the run belongs to another
  repository than `--repo`), `state_unreadable` (`state.json` cannot be read), `lease_busy` (another supervisor holds the repository lease),
  `git_failed` (a git command failed or could not run), `remove_failed` (the run directory could not be deleted: a file or directory in it belongs to a uid the supervisor may not delete from; purge hands each directory the supervisor may not list, the topmost ones, to the worker identity to delete, then removes the rest itself, so this means that did not work either) and `other` (anything else, including a timeout and text that matches nothing). Run the same
  `purge` or `reconcile` by hand under the lease to read the CLI's own message.
* **A pending `factory/gate` is resolved by the same step.** A job whose `ci` criteria are covered by required
  checks of the base branch is published with `factory/gate` = `pending` (`awaiting required CI: ...`) on its exact
  head, not `success`. Only a `pr_ready` job's gate is resolved, and only a pending status **written by Factory's own
  identity** (the App's bot user), never one that merely has the right words. `reconcile` reads `gh pr checks --required`
  for that head (only while the pull request head is still the published commit; a moved head gets nothing for either
  SHA): all green posts `success`, a red check puts `factory:ci-failed` on the issues and then posts `failure` (a check
  name listed twice is red if any entry is), and checks still running post nothing until one hour after the pending
  status (`CI_WAIT_SECONDS` in `src/factory/ci_gate.py`), then a `failure` that says `timed out waiting for CI`. **Such a
  CI failure is not final**: if the required checks of the same head later all pass (a flaky check re-run green, CI that
  finished after the hour) the next pass restores `factory:pr-open` and then posts `success`; a failure about review or
  verification is never touched. This only happens while no supervisor works in the repository (the idle reconcile), so
  a pull request can wait for the rest of a multi-job run, and with the **manager stopped nothing resolves it and
  nothing alerts anyone**: run `reconcile` for the run by hand or post the status yourself. State comes from GitHub (the
  newest `factory/gate` status, its time and its author) and the run directory, which is only read, so a restart
  resumes without a duplicate status. **A required pending `factory/gate` blocks the merge until it
  resolves**, so a pull request whose run was purged while pending stays blocked (nothing resolves it any more): close it and
  re-plan; see `docs/handoff.md`, "The pending gate". A repository without required checks never gets a pending gate; its
  `ci` criteria stay unproven and the pull request stays an incomplete draft. Failures are one
  `factory: ci gate failed (<exception class>)` line and never stop the loop.
* **Interrupted purges finish.** A purge that fails or is killed halfway (manager shutdown, a timeout on a large
  data tree, a busy file) keeps the run directory's marker files until last, and a worktree that git cannot remove
  any more is deleted with the run; the next purge completes it. **The labels are the exception**: they are
  released after the run directory is gone, so a purge killed in that window (or one whose label calls fail)
  leaves the `factory:*` label on the issue and the next purge finds no run to finish from. Remove it by hand;
  until then the label watcher treats the issue as in flight.
* **The history stays.** `purge` and `purge-clone` remove the run directory and the clone under `repos/<org>/<name>`; the history ledger
  (`${FACTORY_DATA}/history`, below) is outside it and is not touched. A purge adds a `purged` entry to it: what each job last was,
  and, for the janitor's purges, why (`terminal` or `abandoned`) and GitHub's word for each pull request.
* **By hand.** `api -X POST "$M/repos/ORG/REPO/runs/RUN/purge"`.
* `FACTORY_MANAGER_OPERATIONS=off` turns all three background loops (restart recovery, budget guardrail, purge) off.

## History

A purged run leaves nothing in its directory, so what Factory did before is kept in the **history ledger**: an append-only record of
runs and jobs in `${FACTORY_DATA}/history/` on the data disk, written by the supervisor, the CLI and the manager and read back by the
manager's read-only endpoints (`GET /dashboard`, `GET /history/runs`, `GET /history/jobs`, `GET .../jobs/{job}`; `docs/manager.md`,
"Dashboard endpoints" and "History ledger" have the fields, the filters and the exact bounds).

```sh
api() { curl -fsS -H "Authorization: Bearer $TOKEN" "$M$1"; }
api "/dashboard"                                              # what is running now, per repository
api "/history/runs?repo=ORG/REPO&outcome=blocked&since=2026-11-01"   # what ended blocked this month, purged runs included
api "/history/jobs?outcome=abandoned&limit=50"                # jobs of runs the janitor gave up on
api "/repos/ORG/REPO/runs/RUN/jobs/JOB"                       # one job: live while the run exists, else from the ledger
```

* **What is kept.** Per run: its issues, when it was planned, started and finished and how each supervisor invocation ended, who
  drained, paused, retried or purged it (`principal`), and per job each real transition (status, phase, attempt, pull request URL,
  cumulative cost and token counts per phase, active seconds, reason code), the last pull request state GitHub reported and when, and the outcome (`merged`,
  `closed`, `blocked`, `abandoned`, `stopped`; a stopped job's entry also names who stopped it). **Never** a transcript, a prompt, a job error, a review finding, a retry reason, an
  issue or pull request text, or a credential or secret of any kind: the ledger holds only ids, statuses, numbers, URLs, reason codes, costs, token counts and durations.
  A token **count** (how many tokens a session read or wrote) is a number, not a secret.
* **How long and how big.** At most `FACTORY_HISTORY_MAX_BYTES` (default 64 MiB) and nothing older than `FACTORY_HISTORY_RETENTION_DAYS`
  (default 365 days); the oldest segments are removed first, when a segment fills or a month ends and once per janitor pass. Both are
  environment variables of the manager (the CLI processes it starts share them), not repository settings. The Deployment sets neither, so the
  defaults apply; changing one is an edit of the container environment in `infra/locals.tf` and a rollout.
  The ledger is shared with whatever later counts runs and costs, so a run's numbers do not reset when the janitor purges it.
* **Where.** On the data disk next to `repos/`, directory `0700` and files `0600`. The disk's reclaim policy is `Retain`, so the ledger
  survives a pod replacement and is lost only with the disk. `purge` and `purge-clone` never touch it. To keep a copy, copy the
  directory; to start over, remove it (the next entry recreates it; nothing else depends on it).
* **Best effort.** A ledger that cannot be written (disk full, a symlink in the way) loses history and never stops a run, a purge or a
  request. Nothing is written in local mode (no `--repo`) or without `FACTORY_DATA`. A command run by hand on the pod through the CLI records
  its job transitions and its purge but has no principal; the manager's `operator` entries exist only for what came through the API.
* **Stale pull request state.** The pull request word in the history and on the dashboard is the last one a status read of the cost
  guardrail or the janitor saw, with its `observed_at`, never a lookup made for the page view; a merge a person made a minute ago may
  not show yet. When that read could not look that job's pull request up (a failure of another job's lookup does not count), the word is
  only what `state.json` says and is marked `fresh: false`; it never replaces a word GitHub confirmed earlier, which keeps its own `observed_at`. `status` (`GET .../status`) is the live view.
* **Logs are served now, to Operators only, and a redacted copy outlives the run.** `plan.log`, `supervisor.log`, the planner's transcript and
  the per-job transcripts and stderr are removed with the run directory, but an Operator can read them while the run exists through the log
  endpoints (`docs/manager.md`, "Log endpoints"), and `purge` first keeps a redacted, compressed copy for 30 days (see "Logs"). The history
  ledger itself still holds no log text.

### The dashboard in a browser

The manager serves a page at `https://<manager host>/ui/` (`docs/manager.md`, "The browser page" and "Browser sign-in"): what is running now, the history, and one
job in detail, with its logs for an Operator. It works on every host name the ingress serves (the Azure name, and your own name when `ingress_custom_host` is set).

**With Microsoft sign-in (the AKS deployment, `FACTORY_MANAGER_AUTH=entra-jwt`):**

1. Open `/ui/`. The panel at the top says **Not signed in**; press **Sign in with Microsoft**. The tab goes to Microsoft and comes back to the page you were on
   (an Entra single sign-on session returns at once; otherwise sign in as usual, with your own work account). No token is pasted, shown or put in an address.
2. The panel then says **Signed in as <your account>**. `Factory.Viewer` is enough to look (the page only reads); the **Stop** button of a running job needs `Factory.Operator` (a token that names its roles without it shows the button disabled); a role is an app role assigned to your account
   on the API's enterprise application (`infra/README.md`, "Browser sign-in"). **Sign out** clears the tab and ends the Microsoft session.
3. What the other states mean:
   * **Signed in as ...: no Factory role** is a `403`: the token is valid but your account has neither `Factory.Viewer` nor `Factory.Operator`. Ask an
     administrator to assign one, then **Sign out** and sign in again: a role is only in tokens issued after it was assigned. **Sign in with another account** picks a different account.
   * **Signed in as ...: session expired** is a `401` that a fresh token did not fix (the token was refused, or the session ended). The page already tried once
     to renew it and to sign you in again by itself; it does not try again for 5 minutes, so it can never loop. Press **Sign in again**.
   * **Sign-in failed (code: ...)**: Microsoft sent the tab back with an error. The code is the standard Entra one (for example `access_denied`);
     the text Microsoft sends is not shown. A redirect URI that is not registered on the app registration is the usual cause (`infra/README.md`).
   * **Sign-in: not configured**: the manager verifies Entra tokens but cannot tell which app registration the page signs in with: set
     `entra_spa_client_id` (Terraform) or `FACTORY_ENTRA_SPA_CLIENT_ID`. The page offers no other way in.
   * **Sign-in: not available**: the page could not read `/ui/config.json` from the manager (reload; check the manager is up).
4. An access token lasts about an hour and is renewed by itself while the tab is open; the refresh token behind it lasts 24 hours, after which one redirect signs you in again.

**Without it (`easy-auth`)** the manager has no browser sign-in of its own, so the page says Microsoft sign-in is not configured there and offers a paste field. **A pasted token helps only when something in front of the manager turns the bearer token into the `X-MS-CLIENT-PRINCIPAL` header** (Azure Container Apps Easy Auth, or an authenticating proxy of your own that validates the token and sets that header from it): `easy-auth` mode reads only that header and ignores `Authorization` (`docs/manager.md`, "easy-auth"), so in a plain local run the page loads and every data call answers `401`. For a local run, call the API with `curl` and a fake principal as `docs/manager.md` shows, or run in `entra-jwt` mode. Behind such a front:

1. Get a token as for the API (`TOKEN=$(az account get-access-token --resource api://<<ENTRA_APP_CLIENT_ID>> --query accessToken -o tsv)`; a
   `Factory.Viewer` or `Factory.Operator` role is enough to look; stopping a job needs the Operator role).
2. Paste the token into **Access token** and press **Use token**. The field is cleared at once. The token stays in that tab's session storage
   only: closing the tab forgets it, it is never put in an address, a cookie or a log line, and the page sends it only to the same host as an
   `Authorization` header. Paste it nowhere else, and not into a page you did not open from your own manager's address.
3. An Entra access token lives about an hour. When it expires the page says "Sign in required, or the token was rejected": paste a new one.
   "The token is valid but has no Factory role" is a `403`: the account lacks `Factory.Viewer` and `Factory.Operator`.

The paste field is offered only in `easy-auth` mode: with Microsoft sign-in on there is no token to copy and none to leave in a clipboard or a shell history.

The page file itself needs no token (a browser cannot send one when it opens an address); every number on it does. The header shows how old the data on
screen is. **Active** lists only runs with a live supervisor or with activity in the last 12 hours; older runs are in **History**. Every repository is
collapsed until you open it, except that while a search or status filter is set the repositories with a match open by themselves (**Expand all**
opens every one shown, **Collapse all** closes them), and the toolbar filters the board by search, status and repository. Active refreshes
every 15 seconds while the tab is visible and costs the disk, not GitHub: the pull request word of a job is the last one
Factory recorded, with its age and whether GitHub confirmed it (it is not a live lookup; `status` is). Both views keep their filters in the address
(`#/active?status=attention`, `#/history?kind=jobs&outcome=blocked&since=2026-11-01`), so a link can be shared with a colleague who has a role. If the page stays blank, check that nothing
in front of the manager rewrites the `Content-Security-Policy` header (the page loads only its own four files and calls only its own host, plus `login.microsoftonline.com` while Microsoft sign-in is on).

## Metrics

The manager can serve Prometheus metrics (numbers and closed words, never issue, pull request or agent text) on a **separate internal
port**: set `FACTORY_MANAGER_METRICS_PORT` (off when unset; loopback by default; never the API port). `GET /metrics` on that port needs no Entra
identity and writes no audit line; it is protected by a **bearer token** (`FACTORY_MANAGER_METRICS_TOKEN`, a Key Vault secret) and the manager refuses to
listen on anything but the loopback without one. The deployment that scrapes it and draws it (Prometheus and Grafana in the same cluster, Grafana at
`https://<host>/grafana/` with Entra sign-in, the alert rules and dashboards, where an alert goes, what it costs) is described in `infra/README.md`, "Monitoring"; it
is off until `monitoring_enabled` is set. A network policy for the port is prepared and off until the Cilium data plane runs. The counters that must not reset when the janitor purges a run (runs and jobs
finished by outcome, reason codes, cost, plan-to-pull-request and per-phase time) are worked out from the history ledger above; the figures over the
runs that still exist (jobs by status and phase, the footprint, `run_budget_usd` against spend) are gauges. The catalogue (name, type, labels,
meaning), how the ledger counters stay honest, the threat model and what is not exposed yet are in `docs/metrics.md`.

## Rotate keys

Both keys live in Key Vault; the pod gets them through the Key Vault secrets-store CSI add-on, pinned to a secret **version**, so a rotation is a deliberate rollout
(`infra/README.md`, "Rotate secrets and change repository configuration"). The sequence is the same: drain, change,
roll out, continue.

* **Anthropic API key:** create a new key in the Factory workspace (keep the workspace spend limit), upload it
  as a new version of the Key Vault secret `anthropic-api-key`, `drain` every repository, set
  `anthropic_api_key_secret_version` to the new version ID and apply (a new pod starts), `run` again, then delete the old key in the
  Console.
* **GitHub App key:** generate a **second** private key in the App's settings (do not delete the first yet), upload it as a new
  version of `github-app-private-key`, `drain`, set `github_app_private_key_secret_version` and apply, check
  that a `plan` still authenticates, then delete the old key in the App's settings.
* **Merge-enrolment App key** (`docs/handoff.md`): a GitHub Actions secret, rotated in the organization settings; Factory does
  not hold it.
* After a rollout, read `status` for each repository: `interrupted` jobs, and `failed` ones with `agent exited -15` after a rollout that was
  not drained, need `retry` or `resume`.

## Logs

The manager writes one audit line per request and one line per background action to stdout (`kubectl -n factory logs deploy/factory` for
the recent ones; Container Insights ships them to the Log Analytics workspace, table `ContainerLogV2`, retention 4 days). They carry identities, methods, paths, status codes, ids, counts and numbers, never
request bodies, prompts, transcripts, issue text or job errors. To check that after a run:

```kusto
ContainerLogV2 | where tostring(LogMessage) has "<a unique marker you put in an issue>" | count
```

must return 0 (`ContainerLogV2` is the Container Insights default schema; if the cluster was set up with the older `ContainerLog` table, use
`ContainerLog | where LogEntry has ...`). Agent transcripts and prompts stay as files under the run directory (they may contain customer
data: do not copy them out, do not commit them), and the history ledger (above) does not contain them. The key vault's audit events (who read
which secret) go to the same workspace.

### Reading a worker's logs (Operators)

An Operator can read a live run's logs in the dashboard (job view, **Logs** panel) or with the API (`docs/manager.md`, "Log endpoints"):

```sh
BASE=https://<<MANAGER_HOST>>/repos/<<ORG>>/<<REPO>>/runs/<<RUN>>
curl -s -H "Authorization: Bearer $TOKEN" $BASE/logs                                        # run-level files
curl -s -H "Authorization: Bearer $TOKEN" $BASE/jobs/<<JOB>>/logs                           # one job's transcripts and stderr
curl -si -H "Authorization: Bearer $TOKEN" "$BASE/jobs/<<JOB>>/logs/1-build-0.jsonl"        # the last 64 KiB, with X-Log-* range headers
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/logs/supervisor.log?offset=0&limit=65536"  # from the start, in parts
```

A `Factory.Viewer` token gets `403`: **logs are Operator-only**, deliberately (they can contain private repository content and agent-authored
text). What is served is cleaned (escape sequences removed) and **redacted** (token shapes replaced), as `text/plain`, in bounded line-aligned
windows; the audit line of each view records who, which path and how many bytes, never content. **The redaction is a safety net, not a
guarantee**: do not treat a log as safe to paste elsewhere.

**Retention: a copy before the purge.** The janitor purges a merged run within about 15 minutes, and the logs go with it. So that they are not
lost at once, `purge` first copies them, redacted and gzipped, to `logs-archive`: a container of the lock storage account on AKS (`FACTORY_LOGS_ARCHIVE_ACCOUNT_URL`,
set by `infra/`) or, without that variable, `${FACTORY_DATA}/logs-archive/<org>__<name>/<run>/...gz` on the data disk. The manager removes copies
older than `FACTORY_LOGS_ARCHIVE_RETENTION_DAYS` (default **30**) once a day. They are not served by the API; read one with
`kubectl -n factory exec deploy/factory -- zcat /data/logs-archive/<<ORG>>__<<REPO>>/<<RUN>>/<<JOB>>/1-build-0.jsonl.gz` (local) or
`az storage blob download --auth-mode login --account-name <<ACCOUNT>> -c logs-archive -n <<ORG>>__<<REPO>>/<<RUN>>/<<JOB>>/1-build-0.jsonl.gz -f - | zcat` (needs
*Storage Blob Data Reader* on the container: grant it as carefully as the Operator role). **If a copy fails, the purge still deletes the run**: look for
`log-archive failed` or `log-archive sweep failed` in the manager's logs and for a rise of `factory_log_archive_failures_total`; the logs of that run
are then gone. `FACTORY_LOGS_ARCHIVE=off` turns the copy off.

## Platform maintenance

* **One node, one replica.** The `factory` pool has exactly one node and no autoscaler, and `/data` is a ReadWriteOnce disk, so a node
  image upgrade, a Kubernetes upgrade or a node failure stops the pod. Planned ones: `drain` every repository first (above), upgrade, then
  `status` for interrupted jobs. There is no second replica to take over, by design.
* **After every node image, kernel or Kubernetes upgrade, and after every base-image or Claude Code bump, run the sandbox check** from the
  pod: `kubectl -n factory exec deploy/factory -- factory-sandbox-check` must print `all expectations held`. The sandbox passed in this pod
  with `hostUsers: false`, `procMount: Unmasked` and `appArmorProfile: Unconfined` together (the smaller sets tried failed, two subsets were not tried: `spikes/S1.md`), and the result is tied to the
  runtime and kernel that were tested (Azure Linux 3, kernel `6.6.159.1-2.azl3`, Kubernetes 1.36.4 on the spike cluster). Do not "fix" a failure by
  enabling `enableWeakerNestedSandbox` or adding capabilities.
  The same check, and S1 on a node of the worker pool, follows every step of the Cilium and node auto-provisioning change: the Cilium switch reimages every node
  and the first worker node is a new node image (`infra/README.md`, "Safe deployments", "Switching the data plane").
* **The verification tools are part of the image.** `jq`, `make`, `shellcheck` and `terraform` (for `terraform fmt`
  only) are pinned in the Dockerfile and checked by `image/smoke.sh` as the worker user, so bumping one is a rebuild and a smoke run
  (`image/README.md`, "Bumping a pin") and reaches the pod only with the next promoted image digest. A tool being present is not a
  sandbox guarantee: no domain, permission or capability was added for it. Docker, a Docker daemon and privileged containers are
  deliberately not in the pod; what a worker cannot run is read from the repository's required CI checks. The tiers and the reasons are
  in `docs/verification-tiers.md`. The image grows by roughly 140 MB (the `terraform` binary alone is 129 MB; not yet measured on a built image: `docker image inspect` shows the size).
* **The data disk survives the pod and a deleted claim, not the cluster.** The `factory-disk` StorageClass has `reclaimPolicy: Retain`:
  deleting the pod or the PVC leaves the Azure Disk (and its cost and its run data) behind, and you delete it by hand when you mean to. The
  disks are created in the cluster's node resource group (`rg-<name_prefix>-aks-nodes`), so **deleting or re-creating the cluster deletes
  them with it**: the data disk, the Caddy certificate disk and, with `monitoring_enabled`, the Prometheus disk (`infra/k8s.tf`, the
  StorageClass comment; `infra/README.md`, "Operations notes"). Snapshot the data disk before any change that can replace the cluster.

## Quick troubleshooting

| Symptom | Likely cause and next step |
|---|---|
| `409 ... lease` on plan/run | another supervisor holds the repository lease, or a crashed one's lease has not expired yet (about a minute); wait, then `status` |
| a `factory-build` label does nothing | `kubectl -n factory logs deploy/factory \| grep '^trigger '` says why: `skipped reason=busy` (a plan or an append is going, or the lease is held elsewhere; it waits for the next pass), `slots_full` / `run_full` / `budget` / `draining` (a run is going and cannot take the issue yet: no free build slot, the run is at its job cap, its budget is spent, or the supervisor is draining), `unauthorised` (the person who applied it lacks `auto_build_min_permission`, or it was a bot: have a maintainer remove and re-apply the label), `ignored` (`factory-ignore`), `in-flight` (the issue has a `factory:*` label; to rebuild remove it), `disabled` (`auto_build = false`), `config` (a permission below write on a repository that is not private); no `trigger` line at all: `FACTORY_MANAGER_TRIGGER_SECONDS=0`, `FACTORY_MANAGER_OPERATIONS=off` (a repository registered after the pod started is picked up by the next pass, no restart needed); `skipped reason=unremoved`: the label could not be removed after the build (the App lacks `issues: write`?) so the issue is not built again until the label is re-applied |
| `breaker code=spend_limit repo=... run=... action=trip` (or `auth`, `rate_limit_unknown`), `GET /repos` shows a `breaker` | the stop-the-world breaker tripped: sessions fail with an API error that retrying cannot fix. Read the failed jobs' `api_error` in `status --json` and the Console for the workspace; raise the spend limit (a human action) or replace the key, then `POST /spend-breaker` with `{"clear": true}` and `run` the drained runs (`docs/capacity.md`, "Stop-the-world breaker"). The label trigger starts nothing meanwhile |
| `trigger ... plan failed rc=... code=... backoff=60s` | the plan child failed (read the run's `plan.log` on the pod). `code=` is one word of a closed table that `plan` leaves in the run's `plan-failure-code` file (never issue or model text): `invalid_plan_paths` (a job's `likely_paths` are not canonical repository-relative paths), `schema_invalid` (the planner's result is not a valid plan), `planner_error` (the planner session failed or gave no usable result), `snapshot_failed` (the GitHub evidence could not be captured), `timeout` (the planner hit its time limit), `other` (anything else, or no readable code file, e.g. a plan killed outright). The label is still on the issue and the loop tries again after the backoff (60 s doubling to 1 h). Fix the cause, or remove the label to stop it |
| `trigger ... append failed rc=... backoff=60s` | a top-up (`plan --append-to`) of a running run failed or wrote no fragment (read the run's `append.log` on the pod); the label is still on the issue and the loop tries again after the backoff, like a failed plan |
| `409 a factory process is already running` | this manager already runs a plan/run for that repository; `status`, then `drain` or `pause` |
| a job stays `queued` / `waiting_*` with `Wait for pod worker budget: N/N workers in use` | the pod-wide worker budget is full (other repositories' sessions hold the slots) or lowered; it is not an error and the supervisor keeps polling. See who holds them in `status --json` `capacity`; raise the container size or `FACTORY_MAX_TOTAL_WORKERS`, or accept the queue (`docs/capacity.md`). `plan` and `preflight` fail with `no slot became free within N s` after `FACTORY_CAPACITY_WAIT_SECONDS` |
| a job failed with `worker exceeded its memory limit: peak X MiB over Y MiB` | the memory guard killed the session's process tree (the group frozen, then every process it could see in the tree, then the group; `docs/capacity.md` says what it cannot reach). Read the job's `<n>-<phase>.stderr`; if the repository's work needs more, set `FACTORY_WORKER_MEMORY_MB` higher (or `off`) and `retry` with a `--reason`; if it was a runaway, fix the cause. `usage[].footprint` in the state shows the peak (`docs/capacity.md`) |
| job `interrupted` | the supervisor was killed before it recorded the end of the job (node loss, a pod killed without its grace period, the shutdown grace ran out); `retry`/`resume`, then `run` |
| job `failed` with `agent exited -15` | the session got SIGTERM: a `pause` or a shutdown made the supervisor terminate its worker (`failed`, not `interrupted`; "Drain, pause, and what each leaves behind"). `retry`/`resume`, then `run`; `drain` first next time |
| job `needs_human_publish`, issue `factory:blocked` | the work passed review and verification but GitHub refused the push because it changes `.github/workflows` (the App has no `workflows` permission, and must not get it). Read the workflow diff, push the branch yourself from the Factory clone (not the worktree), then `resume <job> --phase publish`; the janitor keeps such a run. The pull request is a draft until a person applies `factory-verified`. "Jobs that change `.github/workflows`" |
| job `pr_ready` with `handoff_status: ci_failed` | CI failed on the head; `docs/handoff.md`, "When CI fails" |
| every plan/build/preflight call fails at once with `this deployment requires a separate worker identity: set [worker] uid and gid` | the repository config lacks `[worker] uid = 10002` / `gid = 10010` (the image sets `FACTORY_REQUIRE_WORKER_IDENTITY=1`) |
| every Bash call in a worker fails with a `bwrap` "Unexpected capabilities" error | a worker kept the supervisor's ambient capabilities: the image or `execnocaps` is wrong; run `factory-sandbox-check` |
| every Bash call of a worker fails with `bwrap: Failed to make / slave: Permission denied` or `Can't mount proc on /newroot/proc`, or `factory-sandbox-check` lists failures | the pod lost one of the three settings the sandbox passed with (`hostUsers: false`, `procMount: Unmasked`, `appArmorProfile: Unconfined`), or a node or Kubernetes upgrade changed how they behave: `kubectl -n factory get pod -o yaml`, compare with `spikes/S1.md`, apply the Terraform again; never weaken the sandbox settings |
| `401` from the API | no token, an expired one, a v1 token, or a token for another tenant or audience (the manager accepts v2.0 tokens for its tenant and audiences only); `403`: a valid token without the `Factory.Operator`/`Factory.Viewer` role (`docs/manager.md`) |
| the pod stays `ContainerCreating` with `failed to set MOUNT_ATTR_IDMAP ... invalid argument` | a volume that cannot be ID-mapped (Azure Files NFS, for example) was added to a pod with `hostUsers: false`; `/data` must stay the Azure Disk PVC |
| the pod stays `ContainerCreating` with a `Multi-Attach error` or the disk attached elsewhere | the ReadWriteOnce disk is still attached to the previous pod's node; wait for the detach and check `kubectl -n factory describe pod`; there must never be two replicas |
| the first requests after (re)creating the ingress fail with a TLS error | Caddy obtains its certificate on start (HTTP-01/TLS-ALPN); read `kubectl -n ingress logs`. Let's Encrypt rate limits apply if the certificate storage is lost again and again (`infra/README.md` says where it lives) |
| `503 too many commands` | too many concurrent `status` calls; slow the poller (`docs/manager.md`) |
| an issue still carries `factory:building` or another `factory:*` label that nothing updates | the label write failed (one stderr line `factory: issue label sync failed`, then a 5-minute rest after three in a row) or the run is gone: `run` re-derives the labels of its issues when it starts, and `purge` removes them; an issue that two runs list keeps its label until the last of them is purged. Remove the label by hand if that does not apply |
| an issue is `factory:blocked` with a comment about its `factory-model` or `factory-effort` label | the label was refused when the issue was planned: applied by someone below `auto_build_min_permission` or by a bot (`label-not-authorised`), a model that is not an alias and not in `allowed_models`, an effort above `max_effort`, or two labels of one family (the log line is `factory: issue directive refused issue=N reason=<code>`). Fix or remove the label and re-apply `factory-build`; to allow it, change `allowed_models` / `max_effort` (a rollout on AKS). "Model and effort" |
| a job is `blocked` with `was labelled factory-ignore after planning` | the label was added after `plan`; remove it and `retry` the job (with `--reason`), or leave it parked |
| a run directory that never goes away | not terminal and not idle for `abandon_after` yet, or `purge` refused (the `purge-skipped` line ends in a fixed `cause=<code>`, see "Pull requests merged or closed after the run went idle"; a merged `pr_ready` job does not hold a run back, the janitor reads GitHub's word, not the stored status), or it holds a `needs_human_publish` job (the janitor keeps those; publish or retry it); read `status` and purge by hand once it is safe |

## Pull request conventions per repository (`pr_style`)

By default a pull request is titled `factory: <objective> (#N)` and its commit says `factory: <objective>`. A repository that releases from its commit
history (semantic-release, release-please, commitlint) cannot take that: under a squash merge the pull request title and description are the commit message
the release tool reads, and a title that is not Conventional Commits silently skips or miscategorises a release. `pr_style = "conventional"` in the
repository's config (`src/factory/conventions.py`) changes three things:

* **Title and commit subject** are `<type>: <subject> (#N)` / `<type>: <subject>`, at most 100 characters, never with the `!` breaking-change marker. The
  type is the job's `change_type` (the planner copies the type from the issue's own title prefix, for example `ci: ...`, or chooses it from the change;
  schema and fragments accept only `feat fix docs style refactor perf test build ci chore revert`). A job without one, or with an unknown one, gets
  `chore`, which releases nothing: a wrong type is never allowed to cut a version. The subject is the objective, cut at a word with an ellipsis, with
  issue references neutralised as before; a type the objective already carries is not repeated.
* **Description** text a model wrote is made safe for a squash message: a line a commit parser reads as a `BREAKING CHANGE` note (it would cut a major
  release) gets a `> ` quote marker, a CI-skip keyword (`[skip ci]` and the other four bracketed forms) and a `skip-checks:` trailer get a zero-width
  space. Fixed Factory text is unchanged.
* The branch name, labels, status and the rest of the flow are unchanged.

A repository whose pull request titles are linted for Conventional Commits needs `pr_style = "conventional"`. A pull request published before the setting existed keeps its title until its job publishes again
(a person can also edit the title; Factory rewrites it on the next publish of the same job).

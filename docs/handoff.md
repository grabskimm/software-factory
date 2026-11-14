# Hand-off to GitHub: ruleset and auto-merge

Factory stops at a **published pull request plus a `factory/gate` commit status**. It has no
merge, auto-merge or issue-closing capability, by design. GitHub does the merging, under rules
the organization owns. This document describes what Factory produces, the two files in
`handoff/` that turn that into an automatic merge once CI and the gate pass, and what an
operator has to do. **Nothing in this repository installs any of it**: `handoff/` holds files
for the operator to review and apply (`<<PLACEHOLDER>>`s are not filled in).

## What Factory produces

For a job whose independent review passed and whose verification is complete (`pr_ready`):

| Item | Value |
|---|---|
| Branch | `factory/<run>/<job>`, in the target repository (never a fork) |
| Pull request | opened **ready for review** (not a draft) against `base_branch`, authored by the Factory GitHub App (`<<GITHUB_APP_SLUG>>[bot]`) |
| Body | `Closes #N` for each issue the job fixes, `Refs #M` for context issues (and `Refs #N` for a job issue that carries `factory-ignore` when the pull request is published). For the job of a phase issue of an approved epic (`docs/operations.md`, "Phase issues and the status comment") the first line is `Closes #N, refs #P` (`P` is the epic's parent issue), and `Closes #N, closes #P` only for the last phase and only when a live check at publish says the parent can be closed; anything unread says `refs`. The Verification section holds the baseline comparison, a `Where the checks ran` block (sandbox, CI, operator; see "The pending gate") and the verifier's verdict, with every list of ids bounded. A job with `operator` criteria ends it with an unchecked "Needs a person" checklist (below), which does not change anything else here |
| Label | `factory:ready` only, requested when the pull request is created. GitHub may deliver it as part of the `opened` event or as a separate `labeled` event; either enrols the pull request, and the workflow's guard makes a second one a no-op. It is the one label the workflow below acts on; no other label is put on a pull request |
| Commit status | `factory/gate` = `pending` while publishing, then `success` on the **exact head SHA** (`review+verify passed @<sha7>`), posted with the Factory App's installation token. When a `ci` criterion relies on required checks of the base branch (see "The pending gate"), the status stays `pending` (`awaiting required CI: <checks> @<sha7>`) until those checks have run on that head |

A job that could not pass review or verification is published as a **draft** with no label,
with `factory/gate` = `failure` and `Refs` instead of `Closes`. It never gets `factory:ready`
(and a pull request that stops qualifying loses it), so nothing below acts on it. `Closes #N` only closes issues when the pull
request targets the repository's default branch (Factory refuses to run against another base
unless `allow_non_default_base = true`; the workflow below leaves such pull requests to a human).

## The "Needs a person" checklist

An acceptance criterion tagged `operator` can only be proven by a person (production behaviour, a
paid step, credentials the sandbox must not have). Factory does not try to prove it, and it does not
hold the job back for it. Instead the pull request body ends with a fixed section:

```
## Needs a person

Factory could not prove these automatically and never ticks them: ...

- [ ] corepack can fetch pnpm (why: needs a pnpm repository) <!-- factory:operator-item:op-1a2b3c4d5e -->
```

* **Information only.** An `operator` criterion never changes `verification.complete`, `factory:ready`,
  `factory/gate`, the draft or ready state, or `Closes` versus `Refs`. The pull request is complete
  when review passed, every `sandbox` criterion passed and every `ci` criterion is proven; the
  ruleset and the auto-merge workflow read only CI and the gate, so **an unticked list does not
  hold a merge**. With `required_approving_review_count` at 0 (the value in `handoff/ruleset.json`) an
  `operator`-only plan, or a mixed plan whose other criteria pass, is ready: `factory/gate` goes
  `success`, auto-merge proceeds and `Closes #N` fires with every item unticked. That is the intended
  definition of complete, not a fault. A repository that wants a person to check the list first raises
  `required_approving_review_count` (see "Operator setup", the ruleset step): auto-merge then also
  waits for an approval, and a retry's new head needs a fresh one. Review and verification are not asked to prove these items: their prompts list them apart
  and say they never make a phase fail. The pull request still shows every criterion, `operator` ones
  included, in the unchanged `Acceptance` section.
* **The reason** is the planner's optional `reason` for the criterion (at most 300 characters); a
  criterion without one gets the fixed text `Only a person can prove this.` A reason never rejects a
  plan or a fragment: a longer one is cut to 300 characters ending in `...`; one that is blank, made only
  of invisible characters (spaces, zero-width or control characters, a Hangul filler or the blank braille
  cell) or given on a tag other than `operator` is dropped, so the criterion gets the fixed text. Only a
  `reason` that is not a string (a number, a list) is still refused as a shape error.
* **Stable ids.** Each item ends with a hidden marker `<!-- factory:operator-item:op-<10 hex> -->`, the
  first ten hex digits of the SHA-256 of the criterion text (whitespace-normalised), numbered `-2`,
  `-3` for a repeated text. An id depends on the text alone, not on the order or on the other
  criteria, so editing the body, adding a criterion elsewhere or reordering the plan keeps it.
* **Factory never ticks an item.** Every body it writes has `- [ ]`. When it rebuilds the body (a retry,
  `resume --phase publish`, a new head after a branch sync) it first reads the live body of the existing
  pull request and carries over only ticks a person made: whole `- [x]` / `- [X]` task lines of the last
  `## Needs a person` section that end with a marker whose id the plan still has (CRLF line ends, which
  GitHub writes after a web edit, are accepted). A tick for an id that is gone, a tick anywhere else in the body (an
  agent's text included) and a line someone edited so that the marker is lost are not carried over (see
  below: such a line also drops the ticks of the other items).
  **The section must be the final block of the body.** After the intro lines under the heading, only item
  lines (any id, ticked or not) and blank lines may follow. Factory writes the section last, after text that
  is never an item line, so a copy of the heading left anywhere else in the body, for example when the real
  section was deleted or renamed, is followed by such text and yields no tick. The cost: text a person or a
  bot appends after the list (a note, a comment, another section) makes Factory ignore every tick until it
  is removed, so the items show unchecked in the rebuilt body. The same happens when one item line is damaged
  (a person edited it in the web editor and the hidden marker is gone): the whole list is ignored, not only
  that line, until the marker is back. Ticks are dropped, never invented.
* **What a tick is worth.** Factory reads the body as untrusted text and a tick only as a carry-over of what
  the body said: anyone who can edit the pull request can make or remove one, and it says nothing about
  who. A tick survives a new head: Factory does not know whether the person checked the old one, so a
  tick made on an old head can carry onto changed code and a person re-checks after a material change
  (Factory does not drop ticks when the head changes). Factory reads the live body right before it
  replaces it (after the label step, with no other GitHub call in between). GitHub has no
  compare-and-set for a pull request body, so a tick made in the fraction of a second between that
  read and the edit can still be overwritten; the item then shows unchecked and the person ticks it again.
  A tick never reaches `factory/gate`, which does not look at `operator` criteria at all.
* **Safe text.** Criterion text and reason are model output: they are flattened to one printable line
  (cut at 400 and 300 characters), `<` and `>` are escaped so they cannot forge a marker, a comment or a
  second item, and issue references are neutralised like every other agent text. The free-form
  sections written from model output (the implementation summary, review findings, validation lines,
  paths, and the criteria in `Acceptance`) cannot forge the list either: a line there that begins
  with a `Needs a person` heading, or with a task mark (`[ ]`, `[x]`, after indentation, `>` or a list
  marker), gets a zero-width space in front, and `<!--` is broken so that no comment can hide the
  real section. The ticks Factory reads are only those of the last real section, so such text neither
  shows as a person's sign-off nor counts as one. One limit remains: an unbalanced code fence in an
  agent's text turns the rest of the page, the checklist included, into a code block, where it reads as
  literal text and not as checkboxes; `Acceptance` still lists every `operator` criterion.
* **An `operator`-only plan** (no `sandbox` or `ci` criterion) is published ready when review and
  verification pass, and the section says plainly that Factory proved none of the criteria itself.
  Verification keeps its usual requirements: with no criterion left in its assignment the verifier is
  told to run the repository's own tests and report what it actually ran as `scenario`, `environment`
  and `assertions`, because a verdict with no `assertions` is incomplete and the job is then a draft.
  The planner is told never to use `operator` for anything the sandbox or CI can prove. GitHub merges
  without waiting for a person, so the list is information for whoever reviews the pull request, and
  `status` (text and `--json`, `needs_a_person`) shows how many `operator` criteria a job has.

## The pending gate: waiting for the required CI checks

A job whose review and verification passed, whose `sandbox` criteria all passed and whose only
remaining criteria are `ci` criteria covered by **required checks of the base branch** (each named
check is required there) is published ready, with `Closes`, but its `factory/gate` is not `success`
yet: Factory has not seen those checks finish. It posts `pending` on the exact head SHA instead, with the
description `awaiting required CI: <check names> @<sha7>` (cut to GitHub's 140 characters). The
pull request is not a draft and carries `factory:ready`; the pending status is what holds it.

**A required pending `factory/gate` blocks the merge until it resolves.** The ruleset requires
`factory/gate` from the Factory App, and GitHub treats `pending` as not yet passing, so neither
auto-merge nor a manual merge can happen in the meantime. Nothing needs changing in the ruleset.

Resolving it is the job of the CLI `reconcile`, which the manager's existing idle-reconcile and trigger
loop already runs for a run with a published pull request (no new thread; see `docs/operations.md`).
For each **`pr_ready`** job of the run (a job published as incomplete never had an awaiting gate and is
never promoted) whose newest `factory/gate` status on its published commit is such an awaiting `pending`
one **written by Factory's own identity** (the App's bot user, matched by type and user id; a status with
the right words from anyone else is ignored with one `factory: ci gate ignored a status that Factory did
not write` line), it:

1. does nothing unless the pull request is open and its head is **still the commit Factory published**
   (checked before and after the checks are read). A head that moved is never gated: nothing is posted
   for the old or the new SHA, because Factory never posts a gate for a head it did not verify;
2. reads the required checks on that head (`gh pr checks --required`; `factory/gate` itself is
   left out, since the ruleset requires it too). A check name that appears more than once counts as
   red if any entry is red and as green only if every entry is green; the order of the list never decides;
3. if every required check is green (`pass` or `skipping`) and every check a `ci` criterion names is
   among them, posts `success` on that same SHA (`review+verify+required CI passed @<sha7>`);
4. if any is red, first puts `factory:ci-failed` on each of the job's issues (an issue another run of the
   repository owns is not touched), checking every write, and only once all are in place posts `failure`
   (`required CI failed: <checks> @<sha7>`). A label write that did not land is retried on the next pass with
   the gate still pending and one `factory: ci gate held back until its issue label lands` line; after
   `CI_WAIT_SECONDS` the failure is posted without the label, so a red pull request is never left pending
   for ever. `retry --reason` then repairs it as for any red CI (see below);
5. if checks are still running (or not reported yet, or none are required any more), it posts nothing
   and stays `pending`, until `CI_WAIT_SECONDS` (one hour, `src/factory/ci_gate.py`) after the awaiting
   status was posted. Then it posts a `failure` whose description starts `timed out waiting for CI:`
   and names the checks. That failure does not set `factory:ci-failed`: no check was red.

The state is GitHub's (the newest `factory/gate` status on the head, its time and its author) plus the
run directory, which is only read, so a manager or CLI restart changes nothing: the next pass resumes,
posts nothing twice, and the timeout is counted from the status, not from a process. A `success` is
never touched again. **A `failure` that Factory itself posted for CI is not final**: after a red check
or the timeout (`required CI failed:` and `timed out waiting for CI:`), if the required checks of the
same head later all pass (a flaky check re-run green, CI that finished after the hour), the next pass
puts the issues back to `factory:pr-open` (the success follows only once that label is in place) and posts
`success`; while a check is still red or running nothing is posted again. Any `success` also heals a
`factory:ci-failed` left by a failure whose status post did not land. A failure about review or verification (`incomplete: ...`) is never touched.
Reads and writes that fail are swallowed after one fixed `factory: ci gate failed (<exception class>)`
line, and the App needs `commit statuses: write` and read access to checks/actions.

**The CI evidence in the pull request body.** The pull request body's Verification section carries a
`Where the checks ran` block with a Sandbox, a CI and an Operator bullet (`src/factory/checks_ran.py`).
`Runner.publish` writes the CI bullet as `awaiting on this head: <checks>` (or says the gate waits for no check). Once the pass above
has posted the gate (`success`, `failure` or the timeout), it replaces **only that bullet**, between the two markers
`<!-- factory:ci-evidence:begin -->` and `<!-- factory:ci-evidence:end -->`, with the head commit, the outcome and each
required check's name and conclusion (`pass`, `fail`, `pending`, `skipping`, `cancel`; at most 20 names, then `and N more`;
`factory/gate` itself is left out). A recovery (`failure` to `success`) rewrites the same bullet again. The rule for the edit:

* it comes **after** the gate was posted and is **never part of it**: a failed read or write prints one fixed
  `factory: ci evidence not written to the pull request body (<exception class>)` line and changes nothing about
  the status, the labels or the other jobs (it is not counted towards the three failures that end the step);
* it reads the live body as late as possible, writes only while the pull request is open on the published head, writes
  nothing when the block is already what it would write, and leaves every other byte of the body alone, a person's ticks
  in `Needs a person` included. GitHub has no compare-and-set, so a tick made between that read and the write is lost and
  has to be made again, as at publish;
* a body that does not hold the marker pair exactly once (published before this existed, or edited by hand) is left
  alone with one `factory: ci evidence not written: the pull request body has no evidence block` line;
* every word is Factory's own: fixed text, counts, hashes and the plan's data. A check name is untrusted
  GitHub text: it is flattened to one line, cut to 80 characters, stripped of backticks and shown in a code span, and a
  conclusion is one word of a fixed list, so neither can forge a marker, a heading or a reference;
* **no new permission**: the write is `PATCH /repos/<repo>/pulls/<n>`, the same call `completion` makes, and the
  App already has `pull_requests: write` (`tools/token_broker.py`) to open and edit the pull request at publish. Reading
  the body is the same `GET /repos/<repo>/pulls/<n>` that resolving the gate already makes. `workflows` is not involved.

The gate status stays the authority: if the body update fails, the block keeps saying `awaiting` while the status is
already `success` or `failure`, and nothing retries it (the next pass no longer sees an awaiting gate).

**When it resolves.** Only from the idle reconcile, which skips a repository while any process of
this manager works in it: in a run with several jobs a pull request whose required checks are already
green waits until the supervisor is idle (plus up to five minutes), where before this change it
would have merged at once. If the manager is **stopped**, nothing resolves a pending gate and the
pull request simply stays blocked, with nothing alerting anyone: run the CLI `reconcile` for the run
by hand (it needs the repository lease), or post `factory/gate` yourself once the checks are green.

A repository with **no required checks** never gets a pending gate: its `ci` criteria are
**unproven**, the pull request stays an incomplete draft with `factory/gate` = `failure` and says
that these criteria are outstanding, as for any criterion nothing proves before merge.

**Publish and `status` read the required checks from two places.** `Runner.publish` decides which `ci`
criteria are covered from the base branch's rules (`gh api` branch protection and rulesets) and keeps the
names it saw in the job's `published_required`; it has to, because the pull request does not exist yet.
`status` reads the pull request's own list (`gh pr checks --required`); the comparison adds no GitHub call beyond the snapshot `status` already takes. Publish cannot tell "not required" from "could not read the rules" (a lookup that fails contributes nothing), so the line says the checks were not *confirmed* as required. If
the pull request lists the checks a `ci` criterion names as required although publish did not judge them
required (a ruleset changed after publishing), `status` shows that criterion `unproven`, never satisfied,
with a `Required checks disagree` line and a `required_sources` field in `--json`
(`{"publish": false, "pr": true}`). The other direction is not a disagreement: the pull request's list holds
only the required checks that already reported on the head, so a check publish judged required but the list
lacks may just not have reported yet; the criterion stays `unproven` until it does, without a disagreement
line. A job published before the list was recorded, or a pull request whose list could not be read, is not
compared. A skipped required check (`skipping`) counts as passing in `status`,
as it does here in step 3 and for GitHub.

## Reason codes for incomplete or pending outcomes

Every incomplete or pending outcome of a published job carries exactly one fixed reason code:

| Code | Meaning | Derived from |
|---|---|---|
| `real_failure` | review did not pass, or verification found tests that fail on the head and not on the base (a real failure of a `sandbox` criterion) | `review.pass` is `false`; `verification.baseline.new_failures` not empty while verification is incomplete |
| `ci_failed` | a required check is red, or waiting for CI timed out | the gate description `required CI failed: ...` / `timed out waiting for CI: ...`; a failed required check in the live PR snapshot |
| `ci_pending` | an awaiting required-CI gate: the required checks have not finished on this head | the gate description `awaiting required CI: ...` |
| `workflow_hold` | review and verification passed and nothing is outstanding, but the head changes workflow files, so a person must review the diff and apply `factory-verified` before it can merge (a deliberate hold, not a defect) | `publish_block.review_hold` is not empty; the gate description `workflow files changed: ...` |
| `env_noise` | verification is incomplete only because of failures the baseline comparison classed as environmental (they fail on the base commit too) | `verification.baseline.environmental_failures` not empty, `new_failures` empty, `baseline.sole_blocker` true (the verifier named those tests the only reason for `complete=false`), `baseline.verifier_complete` false (the verifier itself called the verdict incomplete; a verdict that says complete and names a sole blocker contradicts itself, and a later metadata failure on it is missing evidence) and the baseline did not already clear the verdict (`baseline.cleared` false); verification still incomplete |
| `unverified_other` | incomplete for any other reason (the catch-all, meant to shrink): e.g. a `ci` criterion no required check covers, missing verification metadata (also when the baseline had cleared the tests), environmental failures the verifier did not name the sole blocker, a verifier that did not finish | nothing above applies and the outcome is not complete |

**Precedence** when several causes apply at once: the first one in this order wins:
`real_failure`, then `ci_failed`, then `ci_pending`, then `workflow_hold`, then `env_noise`, then `unverified_other`
(`factory.reason_codes.ORDER`). A defect in the change outranks CI, a red check outranks one that
is only outstanding, and evidence still to come outranks environmental noise. A complete, ready job
whose gate is `success` has **no** code (`reason_code` is `null` in `status --json`, nothing is
added to the gate description, and the pull request has no `Reason code` line). There is no
`operator_item` code: `operator` criteria are a checklist and never make a pull request incomplete.

The code comes from data Factory already has (the review and verification verdicts and the
baseline record in `state.json`, and the gate outcome); no extra GitHub call is made. It never
contains issue, criterion, review or pull request text. It appears in four places:

1. the `factory/gate` description, as the suffix ` [<code>]` inside the 140 characters (for example
   `awaiting required CI: build @0123456 [ci_pending]`, `required CI failed: build @0123456 [ci_failed]`,
   `incomplete: review or verification did not pass @0123456 [real_failure]`). The existing prefixes
   (`awaiting required CI: `, `required CI failed: `, `timed out waiting for CI: `) stay first, so
   the reconcile step recognises and recovers these gates as before;
2. one line in the pull request's Verification section: ``**Reason code**: `<code>` (<meaning>)``;
3. `status --json`, per job as `reason_code` (`null` when there is none), and a `Reason code:` line in the text
   view; a live `status` refines a stored `ci_pending` (and ends a stored `workflow_hold` once the gate is green) from the PR's gate and check results. Without a live PR snapshot (`status` offline, or a PR the reconcile has not visited yet) the stored code is shown as it was at the last publish, so a `ci_pending` or `workflow_hold` can be stale until the next reconcile;
4. one log line per published job, printed by `Runner.publish`, with ids, the code and counts only:
   `factory: reason_code job=<id> issues=<n,m> commit=<sha7> code=<code|none> review_findings=<n> new_failures=<n> env_failures=<n> outstanding=<n> awaited=<n>`.

## Issue status labels

Separately from the pull request, Factory keeps **one status label at a time on each issue it
works on**, so the issue list shows where each one stands. The labels are created on demand with
a fixed colour and description:

| Label | Set when |
|---|---|
| `factory:planned` | `plan` queued a job for the issue (also a job queued or re-queued by `retry`/`resume`); it never means the planner only investigated the issue |
| `factory:building` | a builder is working, or the job waits for a build slot |
| `factory:in-review` | review, verification or publishing is running or waiting |
| `factory:pr-open` | the pull request is published (`pr_ready` or `pr_incomplete`) |
| `factory:ci-failed` | `run` saw failed CI on the head of a published pull request when it started, or the idle reconcile found a red required check while resolving a pending `factory/gate` (see "The pending gate") |
| `factory:merged` | Factory observed the pull request merged |
| `factory:blocked` | the plan marked the issue `blocked`, or only investigated it (a decision is needed and nothing is queued), or its job is `blocked`, `failed`, `interrupted`, `needs_human_publish` (finished, but a person must push workflow files) or its pull request was closed unmerged |
| `factory:needs-info` | the plan called the issue an `epic` (too large for one change, split into phases) and its plan comment has numbered questions nobody authorised has answered (`docs/operations.md`, "Epic mode") |
| `factory:awaiting-approval` | an `epic` plan with no open questions (or whose questions got an authorised answer) waits for a person to apply the `factory-approve-plan` label |
| `factory:plan-approved` | a person at or above `auto_build_min_permission` approved the epic plan, and the approval still stands (the manager's epic phase pass judges it again on every pass and opens the phase issues one at a time; no phase pull request is created by Factory itself) |

The three epic labels are the one exception to "re-derived at the start of every `run`": the manager's epic gate pass moves them (`docs/manager.md`, "6. Epic gate"), `reconcile` and `purge` leave them alone, and
the gate relies on GitHub only. `factory-approve-plan` is a human label: Factory only makes sure it exists (and takes a stale one off an issue when it plans it again); it is never applied by Factory.

`factory:observed` is not in this table because it is not a status: it marks an issue Factory opened itself for something a worker observed outside its job
(`docs/operations.md`, "Issues opened by Factory"). It is created with the issue, never replaced by the labels above and never removed by them (it is what
finds the issue again so that the same observation is not opened twice), and no job state sets it. `factory:epic-phase` is the same kind of label: the origin of a phase issue Factory opened for an approved epic plan (`docs/operations.md`, "Phase issues and the status comment"); it is not in `STATUS_LABELS`, so `reconcile` and `purge` never remove it, and Factory never applies the trigger label to such an issue. The writers of issues are `issue_labels.py`, `issue_comments.py`, `observed_issues.py` and `epic_phases.py` (which also writes the one label of a stalled parent through `epic_gate.apply`); none needs more than `issues: write`.

Setting one removes the issue's previous `factory:*` status label (so a `factory:planned` that an older
version put on an investigated issue is replaced by `factory:blocked` at the next `run`). Issues in the `covered`
disposition get none. The labels are written by the supervisor's own `gh` when job state changes
and re-derived at the start of every `run`, so a missed update heals the next time the run is run
(`status` only reads and never writes a label; `purge`, which the janitor also runs for a finished
or abandoned run, removes the label of the run's issues afterwards, leaves `factory:merged` for a merged
pull request and keeps the label of an open one, and never touches an issue that another run's plan lists). They are
**best effort and never a control of a job**: no job, run or merge decision reads them (the readers are the manager's label watcher, which treats an issue that carries one as already in flight or done and so does not start it a second time, `docs/manager.md`, "Label-triggered builds", and the epic gate pass, which moves an epic's issue between the three gate labels, `docs/operations.md`, "Epic mode"), and any failure is swallowed
after one line on stderr, `factory: issue label sync failed (<exception class>)`. Writing them
and the one outcome comment (`docs/operations.md`, "Comments on issues", which includes the epic plan comment) and the new issues it opens for what a worker observed
(`docs/operations.md`, "Issues opened by Factory") are the only things the Factory App's
`issues: write` permission is for (the epic gate pass writes only the status label, with the same permission; the epic phase pass opens the phase issues of an approved plan with their one origin label and keeps one status comment, `docs/operations.md`, "Phase issues and the status comment": no new App permission; it moves the parent of a stalled epic from `factory:plan-approved` to `factory:blocked` (a label write, `epic_gate.apply`) and also rewrites the first line of a stale last-phase pull request, a pull request body write, which `pull_requests: write` already covers); Factory never closes or edits an issue or its title or body. An issue labelled `factory-ignore` when `plan` runs is not planned and
never gets a status label. Labelled later, it stays in the plan, but `run` parks its job as `blocked`
before building it, and a pull request published after the label appeared (or at a moment when the
label could not be read) says `Refs #N` instead of `Closes #N` (see `docs/operations.md`). After three
failed label writes in a row the labeller rests for five minutes and writes the updates it kept with the
next update once the rest is over; the late `factory-ignore` look is a read and goes on during that rest.
**Accept the `issues: write` request on the installation before deploying**; without it no
installation token can be minted at all (`docs/operations.md`, "Roll-out order matters").

## After the supervisor is done: idle reconcile and the completion label

Two things happen to a published pull request when no supervisor is working on its repository.

**Idle reconcile (status labels).** The manager's trigger pass (and the janitor, as a fallback when the
trigger loop is off) runs the lease-guarded CLI `reconcile` for every started, not purged run that holds
a job with a published pull request (`pr_ready` or `pr_incomplete` with a pull request URL), at most once
per five minutes per run, and only while no process of this manager works in the repository. `reconcile` reads
the live state of each such pull request: a **merged** one sets `factory:merged` on the job's issues, one
**closed without merging** clears their `factory:*` status label, an open one gets no label change from this step (but see the next paragraph and "The pending gate": the same `reconcile` also posts `factory/gate` for an open one whose gate is pending). Persisted job
state is not edited and the run is not purged (the janitor still decides that). These writes follow the
rules of every other status label write: fenced by the repository lease, one `factory: issue label sync
failed (<exception class>)` line on stderr, a five-minute rest after three failures in a row, never an issue
labelled `factory-ignore`, and never an issue that another run's plan lists. Without this step the label of a
pull request merged by a person after the run went idle stayed `factory:pr-open`.

**The completion label `factory-verified`.** Factory publishes a job whose review or verification
did not pass as a draft that says `Refs #N`, and it keeps doing so by default: GitHub closes an issue
only for a closing keyword, a merge cannot be undone, and Factory must not claim a completion it
could not verify itself (the worker sandbox cannot always tell an environmental failure from a real one, so
many incomplete pull requests are in fact fine). A person who verified the change by other means says so by
applying the label `factory-verified` (a fixed name) to the **open** pull request. The same `reconcile` step then:

1. checks **who applied it** exactly as for the `factory-build` trigger: the newest `labeled` event
   for that label must come from a `User` (never a bot) whose repository permission is at least
   `auto_build_min_permission` (`write` by default; a lower minimum is refused on a repository that is not
   private). Anyone else is ignored without a write or a log line;
2. refuses, with one line `factory: verified completion refused: the pull request head is not the
   published commit (#<number>)` and no write, when the pull request head is not the commit Factory published
   (`commit` of the job in `state.json`);
3. otherwise rewrites **only the first line of the body** (and the notice line under it, which says in words whether merging closes the issue: `github.refresh_notice`), `Refs #N` to `Closes #N` for the job's issues
   (for a phase issue of an epic the parent is `refs` or, for the last phase, `closes` as the live check says now; an issue that carries `factory-ignore`, or whose label cannot be read, stays `Refs`; a body whose
   first line is not exactly what Factory published is left alone, with one `skipped` line), then
4. re-posts `factory/gate` as `success` **on that exact head SHA**, with the description `verified by hand
   by @<login> (factory-verified) @<sha7>`, so that the status names the human verification instead of
   pretending a review passed.

It never pushes, never changes the head, the draft state or the `factory:ready` label (so it does not by
itself enrol the pull request in auto-merge: the person marks it ready and merges, or lets the
`factory/gate` status unblock a merge they start), and applying the label again is harmless (the body is
rewritten once; the gate is posted only if it is not already that `success` status, which Factory reads first). The label only works for a run that still exists: after the janitor purged
the run (terminal or abandoned) it is ignored. Reads and writes that fail (403, 404, a timeout) are
swallowed after one fixed `factory: verified completion failed (<exception class>)` line and never stop a
manager loop. The App needs `pull requests: write` (body), `commit statuses: write` (gate) and `issues: write`.

**Manual alternative.** A repository that does not use the label (or a pull request whose run is gone) closes
the issue by hand after merging, with a comment that links the evidence (the manual run, the CI run or the
verification record). Nothing else is needed: the issue's `factory:*` label follows through the idle reconcile
above while the run exists.

## The two files and GitHub

1. **`handoff/ruleset.json`**: an organization ruleset for the repositories matching
   `<<REPO_PATTERN>>` on `~DEFAULT_BRANCH`. It requires a pull request, allows **squash only**,
   dismisses stale approvals on push, blocks force-push and deletion, has no bypass actors, and
   requires two kinds of status check: the repository's own CI checks (`<<REPO_CI_CHECK>>`) and
   `factory/gate` **with `integration_id` set to the Factory App**. A `factory/gate` status posted
   by any other identity (a user token, `GITHUB_TOKEN`, another App) does not satisfy the rule.
   The file is a template: it holds only the `<<REPO_CI_CHECK>>` placeholder, and for a repository
   whose CI has a matrix or skips jobs on some changes the value to put there is one always-reporting
   aggregator job (Factory's own `ci.yml` ends in one, `CI result`), not the individual jobs (step 4
   below says why).
2. **`handoff/factory-automerge.yml`**: a workflow for each target repository. When a pull
   request is opened, becomes ready for review, or gets a label, and it is a non-draft pull
   request from a branch of this repository into the default branch, authored by
   `<<GITHUB_APP_SLUG>>[bot]`, with the label `factory:ready`, it first checks that an active
   ruleset requires `factory/gate` from a pinned App on that branch, then runs
   `gh pr merge --auto --squash`. That **enables auto-merge** when the pull request cannot be
   merged yet (the gate is still `pending`, or CI is running). A pull request that is already
   mergeable is **merged at once**, never held, and depending on the gh version either gh does that
   itself or GitHub refuses to enable auto-merge ("Pull request is in clean status"). In the
   second case the step squash-merges the pull request directly, pinned to the head the event saw
   (`--match-head-commit`), and only when its merge state is `CLEAN`; any other state fails the
   job visibly. This document does not claim which of the two a given gh does; the step handles
   both, and the live check in "Acceptance in the sandbox repository", item 7, is how to find out.
   Either way the ruleset check comes first: the ruleset is the only thing that holds a merge.
   The workflow uses a token of a **separate merge-enrolment App**, not
   `GITHUB_TOKEN` (events caused by `GITHUB_TOKEN` do not start new workflow runs, so push
   workflows on the default branch after the merge might not run).

   Factory puts a single label, `factory:ready`, on its pull request, and the `opened`,
   `ready_for_review` and `labeled` events for one pull request can each start a run. The
   workflow's concurrency group is per pull request, so those runs queue in turn and the second
   one logs `nothing to do (...)` instead of racing the first. A `labeled` event for any other
   label (a person's, another tool's) gets a group of its own: a run whose job is skipped still
   takes the group's single pending slot and would cancel the pending `factory:ready` run, the
   only one that enrols. The group is a change to the template: `scripts/enroll-repo.sh` never
   overwrites an existing `.github/workflows/factory-automerge.yml`, so a repository enrolled
   earlier keeps its copy (still correct, with the older per-label group) until its operator
   updates the file by hand, as an ordinary pull request that needs the required checks.
3. **GitHub itself**: auto-merge, the ruleset and issue linking. After the merge GitHub closes
   the linked issues, and Factory's next `status --json` reports the job as `merged` (`run` and
   `status` reconcile merged and closed pull requests from GitHub).

```
Factory: push branch, open PR (Closes #N, label factory:ready), post factory/gate=success @head
         (pending @head while it awaits required CI checks; reconcile resolves it on that head)
   -> factory-automerge.yml: ruleset active? then gh pr merge --auto --squash   (merge-enrolment App token)
   -> GitHub waits for <<REPO_CI_CHECK>> and factory/gate (from the Factory App) on that head
   -> GitHub squash-merges, closes #N, runs push workflows on the default branch
```

### Why `pull_request_target`, and what really protects the key

A first design would name `pull_request`. A `pull_request` run uses the workflow file **from the pull
request itself** and, for a branch of the same repository, the repository's secrets;
`pull_request_target` runs the reviewed copy on the default branch, and the job neither checks
out nor runs pull request code. The activity types (`opened`, `ready_for_review`, `labeled`)
are the same, and the job refuses forks and every author but the Factory App.

That is a second layer, not the control. A repository or organization secret is readable by
**every** workflow in the repository, so an agent that could push a new workflow file to its
branch could read `AUTOMERGE_APP_PRIVATE_KEY` whatever this file does. What stops it is that
the **Factory App has no `workflows` permission**: GitHub refuses a push that creates or changes
files under `.github/workflows`. **Never grant the Factory App `workflows`** (for example to let
it fix CI workflows); with it, `pull_request_target` on this one file would not protect the key.
This is a deliberate deviation from that first design; validate it in the sandbox repository.

A job that changes workflow files is therefore built and checked as usual, but a person publishes its branch: it ends
`needs_human_publish` with the work kept, see `docs/operations.md`, "Jobs that change `.github/workflows`". A person's
push is not a review of the diff (the ruleset requires no approving review), so the pull request of such a job is
always published as a draft with `factory/gate` = `failure` and no `factory:ready`, and only a person's `factory-verified`
label (after reading the workflow diff) lets it through; this holds for every job whose build changed a workflow file,
also when the App's push was accepted.

## Operator setup (once per organization, then per repository)

The per-repository steps (2 to 5 and 8 below) are automated, idempotently and as a dry run by default, by
`scripts/enroll-repo.sh`; see [enrolling-repositories.md](enrolling-repositories.md).

None of these commands has been run by the build that produced this repository.

1. **Two GitHub Apps.**
   - The **Factory App**: contents (write), pull requests (write), issues (**write**, used only to
     maintain the `factory:*` status labels on issues, see "Issue status labels", and to post the one outcome comment
     of an issue that is blocked, needs a decision or failed, see `docs/operations.md`, "Comments on issues"; Factory never
     closes or edits an issue or its title or body; **the installation must accept this request before an image that asks for it is deployed**), commit statuses (write), checks (read), **actions (read)**, metadata (read), as minted by `tools/token_broker.py`.
     **Not** `workflows`. Actions (read) is required for Factory to read check results at all:
     `gh pr view --json statusCheckRollup` queries each check's workflow run, and without it every
     lookup fails with "Resource not accessible by integration". Factory then cannot see a merged
     or closed pull request or a CI failure (`status` falls back to stale persisted state without
     saying so). Actions (read) cannot change a workflow; only `workflows` can, and that stays off. Note its numeric **App ID**: it goes into the ruleset as `integration_id`.
   - A **merge-enrolment App**, separate, installed on the target repositories only:
     Pull requests: read and write; Contents: read and write; Metadata: read. Note its **Client ID**
     (the `Iv...` value on the App's settings page), generate a private key and store it as the
     organization secret `AUTOMERGE_APP_PRIVATE_KEY` (available to the target repositories).
     The two Apps are not a capability boundary (see Security notes); they keep the enrolment
     key out of Factory's code path, Key Vault and every worker.
2. **Repository settings** in each target repository: "Allow squash merging" on (the ruleset
   allows nothing else) and "Allow auto-merge" on (needed whenever the pull request is not yet
   mergeable at enrolment, which is the normal case; without it the enrol job fails visibly).
3. **The workflow, before the ruleset.** Copy `handoff/factory-automerge.yml` to
   `.github/workflows/` in the default branch of each target repository and replace
   `<<GITHUB_APP_SLUG>>` (the Factory App's slug, without `[bot]`) and `<<AUTOMERGE_APP_CLIENT_ID>>`.
   Left unreplaced, the job never matches an author or cannot create a token, so it fails closed.
   Do this **before** step 4 for that repository: once the ruleset is active, a change to the
   workflow needs `factory/gate` like any other pull request, which only Factory can supply.
   Until the ruleset is active the workflow's first real step refuses to enrol anything, so the
   order is safe. Pin changes to the action by commit SHA.
4. **The ruleset.** Fill in `<<REPO_PATTERN>>` and `<<REPO_CI_CHECK>>`, and set the Factory
   App's ID as a number (the API wants an integer, so the file keeps a string placeholder that
   stays valid JSON):

   ```sh
   jq --argjson app_id <<GITHUB_APP_ID>> \
     '(.rules[] | select(.type == "required_status_checks")
       | .parameters.required_status_checks[] | select(.context == "factory/gate")
       | .integration_id) = $app_id' handoff/ruleset.json \
     | gh api -X POST /orgs/<<ORG>>/rulesets --input -
   ```

   - `<<REPO_CI_CHECK>>` is the check name exactly as the job reports it, the job's `name:` (or
     its id) after any matrix expansion, so one entry per expanded name such as
     `unit tests (py3.12)`, and `<caller job> / <reusable job>` for a reusable workflow. That holds
     for a job that runs. A matrix or reusable job that is skipped is, as far as is known, not
     reported under those names (see the UNVERIFIED note below), so such a job must never be
     required individually (the next bullet says why). It is not
     the `CI / ... (pull_request)` label in the pull request's checks list. An organization
     ruleset does not validate the name: a name a repository never reports blocks that
     repository's pull requests at "waiting for status to be reported". Read the exact strings
     with `gh pr view <n> --json statusCheckRollup --jq '.statusCheckRollup[].name'`. A repository
     without CI keeps only `factory/gate`.
   - **Require the aggregator, not each job, when the CI has a matrix or skips jobs.** Factory's own
     `ci.yml` runs every job on every change and is a matrix (`unit tests (py3.11)`, ...); its last job,
     `CI result` (`if: always()`, `needs:` every other job, one inline step and no script), is green only
     when every needed job succeeded, and its name is the value of `<<REPO_CI_CHECK>>`, and the only one.
     The same advice holds for a repository whose CI skips jobs on some changes. The reasons:
     - a workflow that a `paths` or `paths-ignore` filter keeps from starting leaves its checks
       "expected" forever, so such a filter is never used on the trigger and a skip is decided per
       job. A plain job skipped by its `if:` reports `skipped`, which GitHub (and Factory's gate,
       above) counts as passing, so nothing waits and nothing blocks. That is true of a plain job
       only. A matrix job skipped by a job-level `if:` is never expanded, so it reports once under
       its raw template name (`unit tests (py${{ matrix.python }})`, not `unit tests (py3.12)`),
       and a skipped caller of a reusable workflow reports only the caller's name (for example `build`,
       `deploy`), never `build / compile, test, scan`. A skipped matrix or reusable job must
       therefore never be required individually: a required entry for its expanded name would stay
       at "waiting for status to be reported" on every pull request that skips it;
     - a skip must not be able to pass for a failure: an aggregator that counts `skipped` as green
       would turn a job that never ran into a pass. `CI result` fails when a needed job failed, was
       cancelled or was skipped, and passes only when every one succeeded;
     - one stable name does not change with a matrix (`unit tests (py3.12)`), a renamed job or a
       reusable workflow (`build / compile, test, scan`), and a new job only has to join the
       aggregator's `needs:` (`tests/test_ci_workflow.py` fails if it does not);
     - **the verdict must not come from code the pull request can edit.** `CI result` is a few lines
       of shell in the workflow file itself, which Factory's workflow guard holds for a person (a head
       that changes `.github/workflows` is held, `docs/operations.md`); it checks out nothing and runs
       no script of the pull request. An aggregator that ran a script from the pull request's own
       checkout would let a one-line change turn it green with failing tests.
     Source for the skipped-job rules: GitHub's "Troubleshooting required status checks" page says
     that a job skipped by a conditional reports its status as success and that a workflow skipped
     by a path filter, a branch filter or a commit message stays pending. The raw template name of
     a skipped matrix job and the caller-only name of a skipped reusable-workflow caller are
     UNVERIFIED: docs.github.com could not be fetched from the environment where this was written
     (the rule was taken from a search summary of that page), and the community report in the
     actions/runner repository ("skipping a matrix job hangs the pull request when inner matrix
     jobs are required status checks") describes the hang without being documentation. Before
     relying on the exact names, read them from a pull request that skips the job with the `gh pr view`
     command above.
   - Every listed check is required in **every** repository the ruleset targets, so repositories
     whose CI check names differ need one ruleset per group of repositories that share names
     (for example targeted by a repository property).
   - Rehearsal: `"enforcement": "evaluate"` exists only on GitHub Enterprise plans (the REST
     description of `repository-rule-enforcement` says so; other plans have no such mode). On
     other plans narrow `<<REPO_PATTERN>>` to the sandbox repository, apply `active`, run the
     acceptance below, and widen the pattern afterwards. In evaluate mode nothing is held, and the
     workflow's ruleset check refuses to enrol (evaluate and disabled rulesets are not returned by
     the API it asks).
   - Zero required approving reviews matches an unattended flow. If a human must approve first,
     raise `required_approving_review_count`; auto-merge then waits for the approval too, and
     because stale approvals are dismissed on push, a retry (a new head) needs a fresh approval.
   - **The ruleset covers every pull request into the default branch of every matching
     repository, not only Factory's.** Only the Factory App can supply `factory/gate` and
     `bypass_actors` is empty, so human, Dependabot and release pull requests (including a change
     to `factory-automerge.yml`) cannot merge while the ruleset is `active`, and the default
     branch cannot be renamed. Match only repositories where Factory is the intended writer, and
     finish steps 2 and 3 in a repository before it matches. To land anything else, an
     organization owner temporarily sets the ruleset to `disabled` or excludes the repository.
     Do not post `factory/gate` yourself with the Factory App's credentials to get a human change
     through: the gate says that an independent review and verification passed.
5. **Egress.** If the organization restricts GitHub by IP allow list: the NAT address from
   `terraform output nat_public_ip` must be on it for Factory, and the merge-enrolment job is
   subject to it too, because its App-token calls come from the runner. `ubuntu-latest` has
   changing addresses, so change `runs-on` in the copied workflow to a larger GitHub-hosted
   runner with a static IP range (or a self-hosted runner) and add that range to the list;
   otherwise the enrol job is rejected and nothing is enrolled.

## When CI fails

Nothing merges: the required check is not green, so auto-merge keeps waiting. Factory reports
this **only through `status`** (`status --json`; `run` does not check CI, and `status --offline`
does not ask GitHub): the job stays `pr_ready`, its `handoff_status` is `ci_failed`, and the next
action says to diagnose, then `retry JOB --reason ...`. `status` does not list the failing checks:
read them on the pull request (`gh pr checks <url>`); `retry` records them as `ci_repair` evidence
and refuses unless a check on the exact head has positively failed. The operator chooses:

- `factory ... retry JOB --reason "what the CI failure was"`, which queues a new implementation
  attempt, and then `factory ... run`, which executes it, pushes a new head to the same pull
  request and publishes a new gate for it; or
- close the pull request and re-plan, so that Factory builds a new branch.

A hand fix pushed to `factory/<run>/<job>` leaves the hand-off. `factory/gate` is a status on one
SHA, so a new head invalidates the gate: the old `success` does not apply to it; Factory never posts a gate for a head
it did not build and review, and `retry`, `resume` and `import-evidence` all refuse a head that
differs from the retained commit. Under this ruleset such a pull request cannot merge. Do not
post the gate yourself with the Factory App's credentials (that attests a review that did not
happen) and do not add bypass actors; put the branch back on the retained commit and use
`retry`, or close the pull request.

A run with no live worker is purged after `abandon_after` (default 7 days) of inactivity, open pull requests and
`ci_failed` jobs included (`docs/operations.md`, "Purge"); `retry` then has no run to act on. Diagnose and `retry`
within that window, raise `abandon_after` for the repository, or close the pull request and re-plan.

A pull request that is closed without merging becomes `closed` in Factory's status.

## Security notes

- Required status checks identify their source by `integration_id`. Without it any user with
  write access (or `GITHUB_TOKEN`) could post `factory/gate = success`. Keep `bypass_actors`
  empty: a bypass actor can merge around both the gate and CI.
- The two Apps are **not** a capability boundary. The Factory App's token carries
  `contents: write`, which is the permission of the merge endpoint, and Factory posts the gate
  itself, so anyone holding the Factory App's key (or a compromised supervisor) with green CI
  can merge without the enrolment App. What keeps Factory from merging is its code (no merge
  call; `factory --help` lists none) and custody of its key in Key Vault. The enrolment App's key
  is a GitHub Actions secret that never reaches a worker, and it cannot satisfy the gate on its
  own, because the ruleset pins `factory/gate` to the Factory App's `integration_id`. A leaked
  Factory App key is an accepted residual risk with the same blast radius as the App's
  `contents: write`.
- The label is a convenience, not a control: the job also requires the Factory App as author
  and a ruleset that pins the gate, and the ruleset is what actually holds the merge.
- The workflow's ruleset check asks the API for the active rules of the pull request's base
  branch and refuses unless `factory/gate` is required with a pinned App. That makes it fail
  closed when the ruleset is missing, disabled, in evaluate mode, or not covering the repository
  (for example after a rename). Branch protection set directly on a repository, not through a
  ruleset, does not satisfy it by design.
- Never grant the Factory App `workflows` (see "Why `pull_request_target`").

## Acceptance in the sandbox repository (operator-run)

In `<<SANDBOX_TEST_REPO>>` with the ruleset and workflow applied and the Apps installed:

1. The Factory pull request auto-merges only after CI and `factory/gate` pass: watch
   `gh pr view <url> --json autoMergeRequest,statusCheckRollup,mergeStateStatus`; it must stay
   open while either is pending or failing and merge after both pass. The enrol job's ruleset
   check must pass (it needs the merge-enrolment App token to read the branch's rules; if that is
   refused, the job fails closed and nothing is enrolled).
2. A `factory/gate` status posted by any other identity does not satisfy the rule: on a second pull request
   from the Factory App, post `gh api -X POST repos/<<ORG>>/<<SANDBOX_TEST_REPO>>/statuses/<sha> -f state=success -f context=factory/gate`
   with a personal token or `GITHUB_TOKEN`; the pull request must still report the required
   check as expected/not satisfied.
3. GitHub closes the linked issue on merge.
4. Factory's next `factory --repo ... --run-dir ... status --json` shows the job `merged`.
5. A push workflow on the default branch runs after the auto-merge (this is why the App token
   is used and not `GITHUB_TOKEN`).
6. Controls, each its own check:
   - (a) A push by the Factory App that creates or changes a file under `.github/workflows` is
     refused by GitHub (the App has no `workflows` permission, see "Why `pull_request_target`").
   - (b) A pull request whose branch edits `factory-automerge.yml` does not change what the run
     does (`pull_request_target` runs the copy on the default branch).
   - (c) With the ruleset set to `disabled`, a labelled Factory pull request is not enrolled (the
     enrol job fails at the ruleset check) and is not merged.
   - (d) A Factory pull request, created with its single `factory:ready` label, is enrolled exactly
     once: the `opened`, `labeled` and `ready_for_review` runs for it are serialised by the per-pull-request
     concurrency group, and every run after the one that enrols it logs `nothing to do (...)`
     in the enrol step. A `labeled` event for another label, added by hand while the pull
     request is open, starts a run that the job condition skips and does not disturb that.
7. The already-mergeable path (live). A pull request that is already `CLEAN` when its label
   lands must be merged by the enrol job, not left unmerged and not reported as a CI failure.
   Do this with the operator's own token (Factory's credentials cannot do any of it):
   1. Before Factory publishes the pull request, run `gh workflow disable factory-automerge.yml`.
   2. Let Factory publish it. Wait until CI and `factory/gate` are green and
      `gh pr view <url> --json mergeStateStatus,autoMergeRequest` shows `CLEAN` and no auto-merge
      request. The pull request must still be open.
   3. Run `gh workflow enable factory-automerge.yml`, then remove and re-add the label with
      `gh pr edit <url> --remove-label factory:ready` and `gh pr edit <url> --add-label factory:ready`.
      The re-added label starts a `labeled` run, the first run for this pull request.
   4. PASS: the enrol job's "Enable auto-merge (squash)" step succeeds, and logs either
      `already mergeable: merging now` (GitHub refused auto-merge and the direct squash merge ran)
      or nothing more than gh's own output (`gh pr merge --auto` itself merged the pull request);
      the pull request ends `MERGED` by the merge-enrolment App with the ruleset still `active`;
      GitHub closes the linked issue; and the job is not reported as `ci_failed`. Record which of
      the two happened (that is the answer to what the installed gh does), the run URL and the
      log excerpt. A `BLOCKED`, `BEHIND` or `DIRTY` pull request must instead leave the job failing with
      `auto-merge could not be enabled and the pull request is not mergeable (...)` and unmerged.

   Status: **not yet run.** The step's control flow is tested against a stub `gh`
   (`tests/test_handoff_files.py`, `StepScriptTests`), which proves the branches of the script and
   not what GitHub or the installed gh does. This item is operator work; until its result is
   recorded, the fallback has not been exercised against a real repository.

Record the outputs in the pull request that enrols the repository.

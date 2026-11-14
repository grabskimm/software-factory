# Registering a repository

Factory works on a repository only when it has a config entry (`repo_configs`, in the
`INFRA_TFVARS` variable of the Factory repository: the operator's tfvars, which the operator's own
pipeline passes to `terraform` with `-var-file`) and the Factory App is installed on it. There
are **two ways** to register a repository, one script each, both **dry run by default** and both
idempotent:

| | Manual merge | Hand-off (auto-merge) |
| --- | --- | --- |
| Script | `scripts/register-repo.sh` | `scripts/enroll-repo.sh` |
| Factory does | plans, builds, reviews, verifies, opens **ready** pull requests with a `factory/gate` status | the same, and a ruleset plus an enrolment workflow let GitHub auto-merge it once CI and `factory/gate` pass |
| Who merges | **a human** | GitHub auto-merge (Factory never merges itself) |
| Enrolment workflow, ruleset, org secret | none | yes |
| Human pull requests into the default branch | unaffected | **blocked**: the ruleset requires `factory/gate`, which only Factory supplies |

## Which mode?

The hand-off ruleset makes **every** pull request into the default branch require `factory/gate`.
Nobody but the Factory App can post that status, so a person's pull request there can never
merge. Choose:

- **Manual merge** for a repository that has its own rulesets, receives human pull requests,
  or holds deploy or infrastructure code (including this Factory repository itself, whose infra
  and promotion pull requests are written by people).
- **Hand-off** only for a repository where Factory is the sole author and you want merging
  without a person, and which has no rule of its own that conflicts with the hand-off ruleset.

When in doubt, start with manual merge: it changes nothing in the repository, and moving to
hand-off later is one more script run (below).

## Manual-merge mode: `scripts/register-repo.sh`

What Factory does for such a repository is described in the table above. The script takes only
two things: the Factory App on the repository, and a `repo_configs` entry in `INFRA_TFVARS`. It
creates no workflow, no ruleset, no secret and changes no repository setting.

### Prerequisites

- `gh`, `jq` and `python3` on the PATH (`curl` only with `--verify-url`), and a `gh` login of an
  **organisation owner** (listing the App installations and the rulesets needs it).
- Token scopes (classic token): `repo` (read the repository; **write the `INFRA_TFVARS`
  variable of the Factory repository**) and `admin:org` (installations, rulesets). With a
  fine-grained token the script warns that it cannot check scopes; it then needs Variables
  (write) on the Factory repository and organisation Administration (read).
- The Factory repository, as `--factory-repo ORG/NAME`. Without it the script asks `gh repo view`
  of the **current checkout**, so run it from a checkout of the Factory repository (or pass the option).
- The variable `INFRA_TFVARS` exists in that repository and contains one literal
  `repo_configs = { ... }` map (`infra/terraform.tfvars.example` shows the shape).
- The Factory App's slug (`--factory-app-slug`, without `[bot]`); its installation id is read from the
  organisation. Its installation must already have accepted its current permissions
  (`docs/operations.md`, "Roll-out order matters").

### Usage

```sh
scripts/register-repo.sh --org <<ORG>> --repo <<REPO>> --factory-app-slug <<GITHUB_APP_SLUG>> \
  [--factory-repo <<FACTORY_ORG>>/<<FACTORY_REPO>>] [--verify-domain <<VERIFY_DOMAIN>> ...] \
  [--probe-url https://<<VERIFY_DOMAIN>>/] [--run-budget <<USD>>] [--ruleset NAME] \
  [--no-auto-build] [--auto-build-label NAME] [--auto-build-concurrency N] \
  [--auto-build-max-issues N] [--auto-build-min-permission LEVEL] \
  [--replace-config] [--verify-url https://<<MANAGER_HOST>>] [--assume-installed] \
  [--continue-after-manual] [--apply]
```

1. Run it without `--apply` and read the plan: the entry, the diff of the `repo_configs` region
   and the command it would run.
2. Add `--apply`. It stops with a documented exit code whenever a person must act. With a `gh`
   OAuth token that is **exit 10 on the first run**: the token can neither list nor change the
   App installation. Add the repository in the installation's Settings (the script prints the
   URL), then re-run the same command **with `--assume-installed`** (or `--continue-after-manual`).
   Without that flag the re-run stops at exit 10 again, because the script cannot see that you
   did it.
3. Do the remaining steps it prints (drain, apply the Terraform, check `GET /repos`). The script
   never does those.

| Option | Meaning |
| --- | --- |
| `--verify-domain HOST` | a host the verify phase may reach; repeatable; default `pypi.org` and `files.pythonhosted.org`. Plain host names only (no scheme, port, wildcard or trailing dot). |
| `--probe-url URL` | the preflight probe; default `https://<first verify domain>/`; its host must be one of the verify domains. |
| `--run-budget USD` | `run_budget_usd`, a positive number such as `25` or `12.5`; default none (no guardrail). |
| `--ruleset NAME` | the hand-off ruleset that must not match the repository (default `factory-handoff`). |
| `--no-auto-build` | writes `auto_build = false`: the manager does **not** watch this repository for the `factory-build` label. **A default registration writes no `auto_build` key at all, because watching every registered repository is the default**; once the rollout is done, an issue labelled `factory-build` by someone with write access is planned and built with no API call (`docs/manager.md`, "Label-triggered builds"). |
| `--auto-build-label NAME` | `auto_build_label`: the label that asks for a build (default `factory-build`); 1 to 50 letters, digits, space, `.`, `_`, `/`, `-`, never `factory-ignore` or a `factory:*` label. |
| `--auto-build-concurrency N` | `auto_build_concurrency`: workers of an automatic run, 1 to 64 (default 1). |
| `--auto-build-max-issues N` | `auto_build_max_issues`: issues taken per cycle, 1 to 50 (default 3); the rest wait for the next cycle. |
| `--auto-build-min-permission LEVEL` | `auto_build_min_permission`: `read`, `triage`, `write`, `maintain` or `admin` (default `write`): the repository role the user who applied the label must have. `read` and `triage` are refused (exit 3) unless the repository is private, and the manager refuses them again at every poll for a repository that is not private. |
| *(no option)* | `auto_build_timeout_seconds` (per-session limit of automatic runs, 60 to 86400, default 1800) has no option: add it to the entry by hand when an issue needs longer sessions (`docs/manager.md`, "The session limit"). |
| `--replace-config` | overwrite an existing entry whose content differs (see below). |
| `--verify-url URL` | also run `GET URL/repos` (read-only) and report `listed`, `listed with error` or `absent`. The bearer token comes **only** from the environment variable `FACTORY_API_TOKEN`; it is handed to `curl` on stdin (`--config -`), never in an argument and never printed. `https://` only (`http://` for localhost). |
| `--assume-installed` | you have added the repository to the Factory App installation in Settings. Used only when this token cannot list the installation's repositories (a `gh` OAuth token never can): no API call is made for the add (it would be refused anyway) and the script does not stop with exit 10. Reach stays **unproven**: the output says `ASSUMED`. It does not override a listing that says the repository is missing. |
| `--continue-after-manual` | do not stop (exit 10) when the App must be added by hand; the same assumption as `--assume-installed`, but also after a listing that says the repository is missing, and after the refused add. |

An existing entry is compared **on the fields you passed**: `run_budget_usd` only if you gave
`--run-budget`, `verify_domains` only with `--verify-domain`, `probe_url` only with `--probe-url`, and each
`auto_build*` key only with its option (the output lists what was not compared; with `--replace-config` the `auto_build*` keys are compared too, so
`--replace-config` alone turns an `auto_build = false` entry back into the default of watching). So the plain command with the three required options is a
no-op for a repository that was registered by hand with a budget or more verify domains, and a
value you do pass must match. A new entry is always written with all fields (the defaults for
those you did not pass), and `--replace-config` replaces the whole entry with the one printed in
step 2, so a field you did not pass is then dropped.

The key of the entry and its `repo =` line use the repository's name **as GitHub spells it**
(`full_name`), not as typed; a name that is not the same repository under another letter case
(renamed, transferred) is refused.

Every value is validated before the first `gh` call (exit 2): names, hosts, URL and number have
fixed shapes, so nothing can reach the TOML or the HCL text (no quotes, newlines, `<<`, `${`,
`%{`, `--`, trailing dots).

`abandon_after = "7d"` and `auto_resume_after_restart = "once"` are written (`"once"` so that a job interrupted by a restart
is picked up again by the manager once; a run drained by the deploy gate (`tools/infra_deploy_gate.py`) is started again by its `resume` step instead;
`docs/manager.md`, "Automatic resume". An entry
registered earlier keeps the value it has, and a different entry is reported and kept, as always; set it by hand if you want it), as are
`[worker] uid = 10002 / gid = 10010` (required in the image). `base_branch` is the repository's
default branch **as GitHub reports it**, `installation_id` the Factory App's installation id in
the organisation.

`execution` (where the repository's worker sessions run: `"inprocess"`, the default, or `"pod"`) has no option and
neither script writes it: a new entry runs its sessions in process, as every repository does. Only the owner adds it, by hand,
to the entry, and `"pod"` is refused today (fixed text, nothing started, no fallback to in-process; `docs/pod-backend.md`). Note that
`--replace-config` replaces the whole entry with the one printed in step 2, so it drops an `execution` line you added by hand and the
repository goes back to in-process execution: set it again afterwards.

### What each step does and how to undo it

| Step | Action | Undo |
| --- | --- | --- |
| 0 Preflight | Reads only, and decides **every refusal before the first write**: auth and scopes; the Factory repository; the target repository exists in the organisation, is not archived, not a fork, not empty; its default branch; the Factory App's installation; that the hand-off ruleset does **not** match the repository (and that no active rule on its default branch requires `factory/gate`); reads `INFRA_TFVARS` (the `gh variable list` call, filtered to that one variable) and plans the edit, including the 48 KB limit. | nothing to undo |
| 1 App reach | Proves the Factory App can reach the repository: installed on all repositories, or listed by `GET user/installations/<id>/repositories`. **GitHub refuses that listing for `gh` OAuth tokens too (403), not only the add**, so with such a token reach is never proven. If it is not listed (or this token cannot list), the script tries `PUT user/installations/<id>/repositories/<repo id>` once; that is refused as well (403), so it prints the exact Settings URL (`https://github.com/organizations/<org>/settings/installations/<id>`) and stops with exit 10. You add the repository there and re-run with `--assume-installed` (no API call, reach stated as assumed) or `--continue-after-manual`. | remove the repository in that installation's Settings |
| 2 Entry | Renders the `repo_configs` entry (shown with the plan). | n/a |
| 3 Variable | The new text is first **read back locally** (it is tokenised and parsed again: every other entry unchanged, this entry equal to the requested one, the text outside the map byte for byte the same); a text that does not verify is never written (exit 3). Then it re-reads `INFRA_TFVARS` and **refuses (exit 1) if it differs from the first read** (lost-update guard), and runs `gh variable set INFRA_TFVARS -R <factory repo>` with the new text on stdin from a mode-0600 file in a `mktemp -d` directory that a trap removes. It then reads the variable again and checks the entry is there; **any** failure after the write (not shown, unreadable, unparsable) is exit 5 with "fix it by hand". | edit the variable by hand and remove the entry (then apply the Terraform again) |
| 4 Remaining steps | Prints what a person does next (below). | n/a |
| 5 Check (only with `--verify-url`) | `GET <url>/repos`. | n/a |

What is printed of `INFRA_TFVARS`: **only a unified diff of the `repo_configs` region** (or, for an
existing different entry, of that entry), never another variable and never another part of the
variable. The region runs from the start of the `repo_configs` line to the closing brace itself: a
comment behind the opening or the closing brace is not printed. (Lines of entries, and the comments on
them, are printed as diff context: `INFRA_TFVARS` is meant to hold no secrets, see
`infra/terraform.tfvars.example`.) The edit keeps every other byte of the text as it was (comments, other keys, line
endings: a CRLF file stays CRLF); a `tfvars_repo_configs.py` helper next to the script does the
parsing (comments, strings, heredocs and nested braces are understood) and **refuses with exit 3**
when it cannot find exactly one literal top-level `repo_configs = { ... }` map. Note that
`gh variable set` itself trims trailing newlines of the value. With `--replace-config`, whatever
stood behind the old value on its line (a comma, a comment, the next entry or the closing brace)
is moved to a line of its own, because the heredoc terminator must stand alone; a comma that only
separated two entries is dropped (the line break separates them as well).

An **existing entry** for the repository is detected (exact key, case-insensitively): identical
content (indentation and line endings do not count) is a no-op; different content is reported
as a diff of that entry only and is **not overwritten** unless `--replace-config`. That run
exits **0** with nothing written, like a no-op: tell the two apart by the output (`NO-OP` against
`DIFFERENT content ... NOT overwritten`), not by the exit code.

### Remaining steps (printed by the script, done by you)

A `repo_configs` change is a rollout of the manager, so:

1. **Drain** every active run: `GET <<MANAGER_URL>>/repos` (entries with a `supervisor` that is not
   null), then for each run `GET <<MANAGER_URL>>/repos/<<ORG>>/<<REPO>>/runs/<<RUN>>/status` and
   `POST <<MANAGER_URL>>/repos/<<ORG>>/<<REPO>>/runs/<<RUN>>/drain`; repeat the first call until
   every `supervisor` is null (`docs/operations.md`, "Drain, pause, and what each leaves behind").
2. **Apply** the Terraform of `infra/` with the new `INFRA_TFVARS` value (`terraform -chdir=infra plan`, read it, then apply, from the
   checkout of <<FACTORY_ORG>>/<<FACTORY_REPO>> on its default branch; `infra/README.md`, "Safe deployments"). The plan shows a new ConfigMap and
   an updated Deployment; wait for the rollout of the new pod.
3. **Verify**: `GET <<MANAGER_URL>>/repos` lists the repository with no `error` field and `"github": "ok"`
   (`checking` for the first moments after a start; `permission missing: ...` means the installation has not
   accepted that permission yet). The script's `--verify-url` does the GET for you but reports only whether the
   repository is listed, so read `github` yourself.

### Exit codes

`0` done, dry run finished, or nothing to do (this includes "a different entry exists and was
kept", see above); `1` a call failed, or `INFRA_TFVARS` changed between
the read and the write (re-run), or `--verify-url` got no usable answer; `2` usage or an invalid
value; `3` preflight refused (scopes, repository missing/archived/fork/empty/foreign, Factory
App not installed, `INFRA_TFVARS` missing or without exactly one literal `repo_configs` map, over
the 48 KB limit, an edited text that does not verify locally; all decided before the first write); `4` the repository is **already matched by the hand-off ruleset** (or its
default branch already requires `factory/gate`): manual mode would be meaningless, and human pull
requests are already blocked; `5` verification failed (the written variable does not show the
entry, or cannot be read or parsed again after the write, in which case fix it by hand; `--verify-url` found the repository `listed with error`, or `absent` although the entry
was already in the variable); `10` manual step needed (add the repository to the App
installation in Settings, then re-run with `--assume-installed`). Any other unexpected failure exits `1`.

### Running it again

When everything is in place (the entry is present and identical on the fields you pass, and the
App reaches the repository) the script writes nothing and ends with `NO-OP: ... nothing to do`,
exit 0. "The App reaches the repository" is proven only when the installation covers all
repositories or this token can list it; with a `gh` OAuth token add `--assume-installed` (the line
then says the installation was assumed). Without it the run ends at the manual step (exit 10 with
`--apply`) and prints no `NO-OP` line. With `--verify-url` and
a manager that lists the repository it says so; without it the line also reminds you to check
`GET /repos`, because the script cannot see whether the Terraform was applied.

### Moving between the modes

- **Manual to hand-off.** The `repo_configs` entry is the same in both modes, so it stays as it
  is and no rollout is needed. Check first that the repository has no rule or human workflow that the
  hand-off would break (every pull request into the default branch will need `factory/gate`; open
  human pull requests will stop being mergeable), then run `scripts/enroll-repo.sh` for it
  (below). It enables squash and auto-merge, installs the enrolment workflow through a pull
  request, and only then adds the repository to the ruleset. After that `register-repo.sh` refuses
  with exit 4 for this repository, which is correct.
- **Hand-off back to manual.** (1) Remove the repository name from `conditions.repository_name.include`
  of its ruleset (Settings, Rules, or `PUT /orgs/<org>/rulesets/{id}`); if the ruleset was created
  for this repository alone (`--new-ruleset`), disable or delete it. From then on human pull requests
  can merge again. (2) Remove what the hand-off added, in this order: the file
  `.github/workflows/factory-automerge.yml` (a normal pull request, now that no ruleset holds the
  repository; the workflow does nothing without the ruleset, but any workflow in the repository can read
  `AUTOMERGE_APP_PRIVATE_KEY`), then the repository from the selected repositories of that organisation secret and
  from the merge-enrolment App's installation. Repository settings (squash merge, auto-merge) can stay.
  (3) Keep the `repo_configs` entry; nothing to roll out. `register-repo.sh` then reports a no-op.

### What the script deliberately does not do

- It never dispatches or approves a workflow, drains a run, merges, or changes Azure or
  Kubernetes. It never creates a workflow, ruleset or secret.
- It never reads or prints a secret or another variable, and keeps no token in an argument.
- It never overwrites a different entry without `--replace-config`, and never writes if the variable
  changed after it was read.

## Hand-off mode: `scripts/enroll-repo.sh`

`scripts/enroll-repo.sh` automates the per-repository steps of `docs/handoff.md` ("Operator
setup", steps 2 to 4) for a repository in the **same organisation** as the two GitHub Apps.
Use it when the organisation-level setup already exists (both Apps, the organisation secret
`AUTOMERGE_APP_PRIVATE_KEY`, at least one ruleset) and you want to add one more repository.
It is not for the first repository of a new organisation: do `docs/handoff.md` and the sandbox
acceptance first.

The default is a **dry run**. It reads from GitHub, prints every write it would make (the exact
`gh` command, with the JSON it would send) and changes nothing. Only `--apply` writes. Every
step first checks whether it is already done, so running it again is safe and a finished
repository produces no writes at all.

### Prerequisites

- `gh`, `jq` and `base64` on the PATH, and a `gh` login of an **organisation owner**.
- Token scopes (classic token; `gh auth refresh -s <scope>`): `admin:org` (organisation secrets,
  organisation rulesets, installations), `repo` (settings, branch, pull request, installation
  repositories) and `workflow` (creating `.github/workflows/factory-automerge.yml` through the
  API; this is your token, never the Factory App). A fine-grained token carries no scope header;
  the script then warns that it cannot check scopes.
- The identifiers of both Apps:
  - the **Factory App** slug (`--factory-app-slug`, without `[bot]`). Its numeric App ID is read
    from the installation and goes into the ruleset as `integration_id`;
  - the **merge-enrolment App** Client ID (`--automerge-client-id`, the `Iv...` value on its
    settings page).
  Both must already be installed on the organisation, and the organisation secret
  `AUTOMERGE_APP_PRIVATE_KEY` must exist with visibility "Selected repositories".
- The repository's CI checks, exactly as the job reports them (see below).
- The Factory App's installation must already have **accepted its current permissions**, in
  particular `issues: write` and `actions: read` (`docs/operations.md`, "Roll-out order matters").
  A repository added to an installation that has not accepted them cannot get a Factory token;
  `GET /repos` shows it as `"github": "permission missing: <names>"`.

### Usage

```sh
scripts/enroll-repo.sh --org <<ORG>> --repo <<REPO>> \
  --factory-app-slug <<GITHUB_APP_SLUG>> --automerge-client-id <<AUTOMERGE_APP_CLIENT_ID>> \
  --ci-check "<<REPO_CI_CHECK>>" [--ci-check ...] [--ruleset NAME] [--new-ruleset] \
  [--workflow-mode pr|direct] [--allow-unseen-check] [--continue-after-manual] [--no-ci] [--apply]
```

1. Run it without `--apply` and without `--ci-check` to see the discovered check names.
2. Re-run with `--ci-check` for each confirmed name, still as a dry run, and read the plan.
3. Add `--apply`. It stops with a documented exit code whenever a human must act, then you
   re-run the same command.

Exit codes: `0` done; `1` a call failed; `2` usage; `3` preflight refused (scopes, repository
missing/archived/foreign, App, secret or ruleset missing); `4` ruleset step refused; `5`
verification failed; `10` manual step needed (install an App in Settings); `11` merge the
enrolment pull request, then re-run; `12` CI check names not confirmed. Any other unexpected
failure exits `1`, never with the status of a tool the script called.

### What each step does and how to undo it

| Step | Action | Undo |
| --- | --- | --- |
| 0 Preflight | Reads only, and decides **every refusal before the first write**. Checks scopes, that you own the organisation, that the repository is in it, not archived, not a fork, not empty and has Actions enabled, its default branch and visibility, that both Apps are installed in the organisation, that the organisation secret exists and covers the repository, that the ruleset exists (or `--new-ruleset`) and is JSON, that it can take the repository (check names equal, not excluded, `factory/gate` pinned to the Factory App, selected by repository name), that the repository is not already held by a ruleset without the workflow, and discovers CI check names. A failed read of the active rules is an error (exit 1), never "no rules". | nothing to undo |
| 1 Settings | `gh repo edit --enable-squash-merge --enable-auto-merge`. | Settings, General, Pull Requests |
| 2 Apps | `PUT /user/installations/{id}/repositories/{repo_id}` for the Factory App and the merge-enrolment App. If GitHub refuses the call the script prints the Settings URL (`https://github.com/organizations/<org>/settings/installations/<id>`) and stops with exit 10; install by hand, or pass `--continue-after-manual`. | remove the repository in that installation's Settings |
| 3 Secret | `PUT /orgs/<org>/actions/secrets/AUTOMERGE_APP_PRIVATE_KEY/repositories/{repo_id}`. The secret value is never read. | `DELETE` on the same path, or Settings, Secrets (by hand) |
| 4 Workflow | Renders `handoff/factory-automerge.yml` with the slug and Client ID (refuses if any `<<` remains). `pr` mode: branch `factory/enroll`, one commit, one pull request, then exit 11 until you merge it; the next run sees the file on the default branch and continues. `direct` mode commits to the default branch, and is refused once a ruleset already holds the repository. An existing file is never overwritten. | revert the commit (before the ruleset covers the repository; afterwards a change needs `factory/gate`, see the break-glass in `docs/handoff.md`) |
| 5 CI checks | Uses your `--ci-check` values. Without them the discovered names are only printed and `--apply` stops with 12 before any write. | n/a |
| 6 Ruleset | Re-reads that the workflow is on the default branch, then adds the repository name to `conditions.repository_name.include` of the named ruleset, only if the ruleset's required checks (other than `factory/gate`) equal this repository's. Otherwise it refuses (exit 4). `--new-ruleset` creates a ruleset from `handoff/ruleset.json` for this repository only, with your checks, `factory/gate` pinned to the Factory App id, and an empty `bypass_actors`. | remove the name from `include` (or disable/delete the new ruleset) in Settings, Rules, or with `PUT /orgs/<org>/rulesets/{id}` |
| 7 Verify | `GET repos/<org>/<repo>/rules/branches/<default>` must list `factory/gate` with the Factory App id (the same call the merge-enrolment workflow makes) **and exactly the confirmed CI checks and no others** (another ruleset may add one). Exit 5 if not. | n/a |
| 8 Hand-over | Prints the `repo_configs` snippet and the remaining manual steps. | n/a |

### CI check groups

A ruleset requires every listed check in every repository it matches. So repositories whose CI
check names differ need **one ruleset per group of repositories that share names**. The script
enforces this: it extends an existing ruleset only when the required checks are exactly the
repository's; otherwise use `--new-ruleset --ruleset <name>` (default name
`factory-handoff-<repo>`), and add later repositories of that group with `--ruleset <name>`.

Use the check name as the job reports it (the job `name:` after matrix expansion, or
`<caller job> / <reusable job>`), not the `CI / ... (pull_request)` label in the checks list. A
name the repository never reports blocks every pull request there at "waiting for status to be
reported". That is why the script never guesses: discovery only prints, and you confirm with
`--ci-check`. A repository without CI uses `--no-ci` and keeps only `factory/gate`.

A repository whose CI has a matrix or skips jobs on some changes should confirm only its
always-reporting aggregator job (here `CI result`, a one-step job that needs every other job), not the
jobs that can be skipped or expand (`docs/handoff.md`, step 4 of "The two files and GitHub", has the
reasons).

With `--new-ruleset` a name that was not seen on the default branch's head (checks that only run
on pull requests are legitimate) is refused with exit 12 until you add `--allow-unseen-check`:
a typo would otherwise create a ruleset nothing can satisfy, with no bypass actor.
Extending an existing ruleset needs no such flag, because its required names must already equal
yours.

### Rollout order with the infrastructure apply

Every `repo_configs` change is a new rollout of the manager, so:

1. Run the script (dry run, then `--apply`) until it exits 0 and step 7 says verified.
2. **Drain first** (`docs/operations.md`): nothing in flight when the revision changes.
3. Add the printed `repo_configs` entry to `INFRA_TFVARS` (fill in the `<<VERIFY_DOMAIN>>`
   placeholders and any toolchains; a toolchain needs an image rebuild before its config), apply
   the Terraform (`infra/README.md`), and wait for the new revision to be ready.
4. Check that `GET /repos` on the manager lists the repository without an `error` field and with `"github": "ok"`
   (a `permission missing: ...` value names the permissions the installation still has to accept).

The hand-off side can safely come first: until the repository is in `repo_configs`, Factory
never touches it. The reverse order (config first) is not harmful but leaves a configured
repository whose pull requests cannot be gated.

### What the script deliberately does not do

- It never reads or prints a secret value, and keeps no token in arguments or output.
- It never grants the Factory App `workflows` (or any permission), never changes either App's
  settings or the organisation secret's visibility.
- It never adds, removes or edits bypass actors. If the target ruleset already has some it only
  warns.
- It never merges (no `gh pr merge`, no `--admin`), deletes a branch or force-pushes. The
  enrolment pull request is merged by you.
- It never overwrites an existing workflow file, edits a ruleset other than adding the name, or
  touches a ruleset that selects repositories by id or property.
- It does not guess CI check names, does not change the infrastructure and does not run the
  drain or the apply.
- It does not enrol a repository from another organisation, and it refuses archived repositories
  and a repository already held by an active ruleset that has no enrolment workflow (an owner
  must exclude it from the ruleset temporarily first).

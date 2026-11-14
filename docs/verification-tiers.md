# Verification tiers

What can be checked where, and what the Factory pod will and will not carry. This is the
specification behind issue #40; `image/README.md` describes the image itself.

## Principle

**Broad tools, not broad privileges.** Tools belong in an isolated verification tier. The
supervisor pod stays small, pinned and scanned, because it sits next to the Anthropic key,
the Key Vault identity and the GitHub App credentials.

A runner-style base image (tens of GB, hundreds of CVEs, Docker) is the wrong answer to
"verification is often incomplete":

* it cannot pass a blocking Trivy scan (fixable HIGH or CRITICAL findings stop the push to
  the registry; no suppression is used);
* it would slow every rollout on a pod that has exactly one node and one replica
  (`docs/operations.md`, "Platform maintenance");
* it would put a container runtime, and with it the means to start privileged containers,
  in the same pod as every Factory credential.

| Tier | Where it runs | What it is for | Status |
|---|---|---|---|
| 1 | the supervisor image | small, pinned, offline, unprivileged tools | specified and implemented here |
| 2 | the repository's CI | everything the sandbox cannot run | specified here, implemented by the issues below |
| 3 | throwaway runner pods on a separate node pool | checks CI cannot give in time or at all | design stub, own design issue if wanted |
| 4 | a per-repository verifier image as a one-shot Job | repositories whose CI image should be reused as is | design stub, own design issue if wanted |

## Tier 1: small tools in the supervisor image

The image carries `jq`, `make`, `shellcheck` and `terraform` (for
`terraform fmt` only). The rule for a tool to be in this tier is that it is all of:

* **small and pinned**: a Debian package (the image digest is the pin, `image/README.md`,
  "Pinned git") or a release download with an exact version and a sha256 that the build
  verifies;
* **offline**: it needs no network at run time (the worker sandbox has none, apart from the
  verify allow-list);
* **unprivileged**: it runs as the `worker` user without capabilities, behind
  `factory.execnocaps`;
* **daemon-free** and free of a vulnerability database of its own;
* **proven by `image/smoke.sh` as the worker user**, with an input it must accept and an
  input it must reject;
* **clean under the blocking Trivy scan** with no suppression. A tool that cannot pass is
  left out of the image and the README says why; it is never waived.

What each tool is for:

| Tool | Use by a worker |
|---|---|
| `jq` | read and check JSON (`jq -e` turns a false result into an exit status) |
| `make` | run a repository's own `make` targets |
| `shellcheck` | lint shell scripts (`scripts/`, `image/`) |
| `terraform` | `terraform fmt -check` on `*.tf` and `*.tfvars`. Not `init`, `validate` or `plan`: those need provider downloads, a backend or credentials, which a worker has none of. `CHECKPOINT_DISABLE=1` is set so it never calls home |

`actionlint` is deliberately not in the image. Its newest release (1.7.12) is built with Go
1.26.1, and a Trivy scan of the image reported 24 fixable HIGH findings in that
standard library, which a blocking scan would refuse. Linting
workflows is a tier-2 check (an `actionlint` job in your CI) until a release built with a fixed
Go exists (`image/README.md`, "Deliberately not in the image").

What tier 1 does **not** change:

* A tool being present is not a sandbox guarantee. Workers still run in the bubblewrap
  sandbox with the policies of `src/factory/phases.py`; no permission, domain or capability
  was added for these tools.
* Tests that skip when a tool is missing (`shutil.which("jq")`, `shellcheck`, `terraform`)
  now run inside the worker sandbox. The sandbox is stricter than a CI runner (read-only
  locations, no network), so they can fail there for environmental reasons that CI does not
  see. Telling those apart from real failures is the job of the baseline comparison of
  issue #41, not of this tier.
* The Debian versions (`jq` 1.6, `shellcheck` 0.9.0) are older than the ones on a GitHub
  runner. Where the two disagree, tier 2 is the authority.
* The image grows (the terraform binary alone is about 130 MB). Check the image size after each
  build so the growth is visible; capacity questions are `docs/capacity.md`.

Adding a tool to this tier is a change to the Dockerfile, `image/smoke.sh`, the table in
`image/README.md` and the sha256 pin, and `tests/test_image_tooling.py` fails when one of
them is forgotten.

## Tier 2: CI is the broad environment

Required pull-request checks run on real runners with everything installed. Factory treats
them as the authority for what its own sandbox cannot run, and says so in the pull request.
No new infrastructure.

**Specification**

1. **Evidence comes from required checks on the exact head.** Factory reads them with
   `gh pr checks --required` (`factory.github.required_checks`, reached through
   `inspect_pr`), which needs the installation permission `actions: read`
   (`tools/token_broker.py` already requests it; `GET /repos` shows `permission missing:
   actions` when the installation has not accepted it). A check result for another commit
   is not evidence. Only checks that gate the merge count: a failing check that does not
   gate it is informational (`factory.github.ci_assessment`).
2. **The evidence is quoted in the pull request**: the check name, its outcome and the head
   commit it was read on, in the Verification section, next to the sandbox's own results.
   Check names and conclusions are untrusted text from GitHub: they are shown as data,
   truncated and stripped of control characters, never interpreted as instructions.
   Publishing writes a `Where the checks ran` block (sandbox, CI, operator) in which the CI bullet
   names the checks the gate awaits; when the gate resolves, `ci_gate.resolve_pending` replaces that one
   bullet, between two fixed markers, with what it read (`factory.checks_ran`). The update is a report
   after the gate, best effort and never part of it, and needs no permission beyond
   `pull_requests: write`, which the App already uses to open and edit the pull request.
3. **No evidence is not success.** A criterion tagged `ci` is satisfied only by a named
   required check (or a named workflow) that completed successfully on the head. While the
   check is pending the criterion is not satisfied and the gate stays `pending`. When the
   repository has no required checks (`ci_assessment` basis `rollup_no_required`) or they
   cannot be read (`rollup_fallback`), a `ci` criterion stays **unproven**, and the pull
   request says so; a person decides. A repository without required checks is therefore
   never reported as "verified by CI". (The related case of a failure that is never reported
   when nothing is required is issue #46.) A required check that GitHub skipped (`skipping`)
   counts as passing, as for GitHub itself. Two sources can answer "is this check required?":
   `Runner.publish` (the base branch's protection and rulesets, recorded as `published_required`)
   and `status` (the pull request's `gh pr checks --required`); when the pull request lists a check as
   required that publish did not judge required, the criterion is shown **unproven** with both answers,
   never satisfied (the pull request's list only holds checks that already reported, so a check missing
   from it is not evidence that it is not required). A `sandbox` criterion is likewise
   unproven, not satisfied, when `verify.when` skipped the verify phase: a verification that ran
   nothing proves nothing.
4. **Criteria say where they are checked.** Each acceptance criterion in a plan carries a tag:
   `sandbox` (the worker can prove it), `ci` (a required check proves it) or `operator`
   (only a person on a live system can). The tag says where a criterion is checked, never
   whether it holds (`factory.model.criterion`; `docs/architecture.md`, "Where an acceptance criterion is proven").
   An `operator` criterion is a checklist item for a person in the pull request body, not a reason
   to call the job incomplete (`docs/handoff.md`, "The Needs a person checklist").
5. **Factory only reads.** It does not re-run, dispatch or edit workflows to obtain evidence
   (the GitHub App must not have the `workflows` permission), and it never merges or closes
   anything; GitHub auto-merge decides on the same required checks (`docs/handoff.md`).

**Implemented by** the verification-environment issue #37 and its children, not by the
change that wrote this document:

| Issue | Part of this specification |
|---|---|
| #42 | acceptance-criterion tags (`sandbox`, `ci`, `operator`) in plans and fragments (point 4) |
| #43 | `factory/gate` stays `pending` until the `ci` criteria have their evidence (point 3) |
| #44 | `operator` criteria as a checklist in the pull request (point 4) |
| #41 | baseline comparison, so failures that tier 1 tools cause only in the sandbox are cancelled |
| #37 | the pull request body states which checks ran where, and the CI evidence is written into it when the gate resolves (point 2) |
| #45 (#102) | reason codes that tell environmental noise from a real failure from CI still outstanding or failed (the false-incomplete rate is #103) |

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

## Tier 3: ephemeral isolated runners (design stub)

Optional, and its own design issue when somebody wants it. The shape:

* GitHub's runner controller (Actions Runner Controller) runs **one throwaway pod per job**
  with the runner image, on a **separate, autoscaled node pool**. This is where node
  autoscaling is actually useful: the `factory` pool stays at one node and no autoscaler
  (`docs/operations.md`), and nothing here changes it.
* **Hard requirements**, all of them, or tier 3 does not exist:
  * no Factory secrets, no cloud credentials, no write token beyond the job's own;
  * a network allow-list;
  * a fresh pod per job, never reused;
  * selected private repositories only;
  * its own namespace and its own identity (not the Factory pod's workload identity);
  * no Docker socket and no privileged pod. If a job needs to build images it uses a
    rootless builder with no privileges, or it stays in tier 2.
* **A threat model comes first**, because the code it runs was written by an agent: what a
  hostile or merely wrong change can reach from a runner pod (the cluster API, the node's
  metadata endpoint, other namespaces, the registry), what is logged, who can see the output
  and how a runner is destroyed after a job.
* Open questions for the design issue: cost and idle capacity, how a job is requested and
  who may request it, how results are fed back as tier-2-style evidence, and what happens
  to runners when the Factory pod drains.

## Tier 4: per-repository verifier image (design stub)

Optional, own design issue. A repository whose CI image should be reused as is gets a
one-shot Kubernetes Job built from that image:

* the image is named in the repository's Factory configuration and pinned by digest;
* the Job runs under the same hard requirements as tier 3 (separate namespace and identity,
  no Factory secrets, network allow-list, fresh pod, no privileges);
* the job gets the worktree read-only, writes its results to a single output file, and the
  supervisor reads that file as data;
* open questions: who builds and scans those images, how a repository's CI image gets a
  digest, and how the result becomes evidence for a `ci`-style criterion.

## Explicitly not planned

* **Docker, or a Docker-in-Docker daemon, inside the Factory pod.**
* **Any privileged container next to the supervisor**, and with it a container runtime
  socket, `CAP_SYS_ADMIN` or a relaxed security context beyond the three pod settings
  recorded for the sandbox (`spikes/S1.md`).
* **A runner-style base image** for the supervisor.
* Trivy or another scanner with its own vulnerability database in the image (the scan runs
  outside the image, against the built image: `image/README.md`).
* Browsers, `pytest` and repository toolchains beyond the optional pinned `node@22`
  (`TOOLCHAINS`): what a repository needs beyond tier 1 is tier 2 or, if wanted, tier 3 or 4.

`image/smoke.sh` guards the first and last of these: the build fails the check when
`docker`, `dockerd` or `trivy` is in the image.

"""The files Factory hands to the operator for the GitHub side of the hand-off (``handoff/``).

They are never installed by this repository, so what they must guarantee is pinned here:
the ruleset only lets Factory's own gate satisfy the gate check, squash only, no bypass; the
auto-merge workflow runs from the default branch, holds no token of its own and cannot
be turned against its own secrets.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RULESET = ROOT / "handoff" / "ruleset.json"
WORKFLOW = ROOT / "handoff" / "factory-automerge.yml"
DOC = ROOT / "docs" / "handoff.md"
PLACEHOLDER = re.compile(r"<<([A-Z0-9_]+)>>")


def ruleset():
    return json.loads(RULESET.read_text())


def rule(kind):
    found = [item for item in ruleset()["rules"] if item["type"] == kind]
    assert len(found) == 1, kind
    return found[0]


def workflow_lines():
    """The workflow without comment-only lines."""
    return [line for line in WORKFLOW.read_text().splitlines() if not line.lstrip().startswith("#")]


def run_blocks(lines):
    """The text of every ``run: |`` block."""
    blocks, current, indent = [], None, 0
    for line in lines:
        stripped = line.strip()
        if current is None:
            if re.match(r"^(- )?run:\s*[|>]", stripped):
                current, indent = [], len(line) - len(line.lstrip())
            elif re.match(r"^(- )?run:\s*\S", stripped):
                blocks.append(stripped)
            continue
        if stripped and len(line) - len(line.lstrip()) <= indent:
            blocks.append("\n".join(current))
            current = None
            if re.match(r"^(- )?run:\s*[|>]", stripped):
                current, indent = [], len(line) - len(line.lstrip())
            continue
        current.append(line)
    if current is not None:
        blocks.append("\n".join(current))
    return blocks


class RulesetTests(unittest.TestCase):
    def test_it_targets_the_default_branch_of_the_matching_repositories(self):
        data = ruleset()
        self.assertEqual((data["target"], data["enforcement"]), ("branch", "active"))
        # an exclude would silently cancel the protection while the ruleset stays "active"
        self.assertEqual(data["conditions"]["ref_name"], {"include": ["~DEFAULT_BRANCH"], "exclude": []})
        self.assertEqual(data["conditions"]["repository_name"]["include"], ["<<REPO_PATTERN>>"])
        self.assertEqual(data["conditions"]["repository_name"]["exclude"], [])
        self.assertEqual(set(data["conditions"]), {"ref_name", "repository_name"})

    def test_nothing_can_bypass_it(self):
        self.assertEqual(ruleset()["bypass_actors"], [])

    def test_force_push_and_deletion_are_blocked(self):
        kinds = {item["type"] for item in ruleset()["rules"]}
        self.assertLessEqual({"deletion", "non_fast_forward"}, kinds)

    def test_only_the_rules_the_brief_asks_for_exist(self):
        self.assertEqual(
            {item["type"] for item in ruleset()["rules"]},
            {"deletion", "non_fast_forward", "pull_request", "required_status_checks"},
        )

    def test_pull_request_is_required_and_squash_is_the_only_merge_method(self):
        parameters = rule("pull_request")["parameters"]
        self.assertEqual(parameters["allowed_merge_methods"], ["squash"])
        # a retry pushes a new head to the same pull request: an approval must not carry over
        self.assertIs(parameters["dismiss_stale_reviews_on_push"], True)
        self.assertEqual(parameters["required_approving_review_count"], 0)
        # the API requires all five of these
        for key in (
            "dismiss_stale_reviews_on_push",
            "require_code_owner_review",
            "require_last_push_approval",
            "required_approving_review_count",
            "required_review_thread_resolution",
        ):
            self.assertIn(key, parameters)

    def test_the_gate_must_come_from_the_factory_app_and_ci_is_required_too(self):
        checks = rule("required_status_checks")["parameters"]["required_status_checks"]
        by_context = {check["context"]: check for check in checks}
        self.assertEqual(set(by_context), {"<<REPO_CI_CHECK>>", "factory/gate"})
        self.assertEqual(by_context["factory/gate"]["integration_id"], "<<GITHUB_APP_ID>>")
        self.assertNotIn("integration_id", by_context["<<REPO_CI_CHECK>>"])

    def test_ids_are_placeholders_never_real_values(self):
        text = RULESET.read_text()
        self.assertNotRegex(text, r'"(integration_id|actor_id)"\s*:\s*\d')
        self.assertEqual(
            set(PLACEHOLDER.findall(text)), {"REPO_PATTERN", "REPO_CI_CHECK", "GITHUB_APP_ID"}
        )


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text()
        self.lines = workflow_lines()
        self.code = "\n".join(self.lines)

    def test_it_runs_from_the_default_branch_for_the_three_events(self):
        self.assertRegex(self.code, r"(?m)^on:\n  pull_request_target:\n    types: \[opened, ready_for_review, labeled\]$")
        self.assertNotRegex(self.code, r"(?m)^  pull_request:")

    def test_it_never_checks_out_or_runs_pull_request_code(self):
        self.assertNotIn("actions/checkout", self.code)
        self.assertNotIn("github.event.pull_request.head.ref", self.code)
        self.assertNotIn("github.head_ref", self.code)

    def test_it_has_no_token_of_its_own(self):
        self.assertRegex(self.code, r"(?m)^permissions: \{\}$")
        self.assertNotIn("secrets.GITHUB_TOKEN", self.code)
        self.assertNotIn("github.token", self.code)
        # the only permissions key anywhere is the top-level empty one: no job-level block,
        # scalar (write-all) or flow ({ contents: write }) form
        self.assertEqual(re.findall(r"(?m)^[ \t]*permissions:.*$", self.code), ["permissions: {}"])

    def test_every_action_is_pinned_to_a_commit(self):
        uses = re.findall(r"uses:\s*(\S+)", self.code)
        self.assertTrue(uses)
        for reference in uses:
            self.assertRegex(reference, r"^[\w./-]+@[0-9a-f]{40}$", reference)

    def test_the_app_token_is_narrow(self):
        self.assertIn("actions/create-github-app-token@", self.code)
        self.assertIn("repositories: ${{ github.event.repository.name }}", self.code)
        self.assertIn("permission-contents: write", self.code)
        self.assertIn("permission-pull-requests: write", self.code)
        self.assertEqual(len(re.findall(r"permission-[a-z-]+:", self.code)), 2)
        self.assertIn("secrets.AUTOMERGE_APP_PRIVATE_KEY", self.code)
        self.assertIn('client-id: "<<AUTOMERGE_APP_CLIENT_ID>>"', self.code)

    def test_every_condition_of_the_brief_is_checked_and_combined_with_and(self):
        condition = self.code.split("if: >-", 1)[1].split("steps:", 1)[0]
        # The whole expression, not its fragments: dropping the parentheses, turning an && into
        # an || or negating a term must fail here, because actionlint accepts all of them.
        self.assertEqual(
            " ".join(condition.split()),
            "github.event.pull_request.head.repo.full_name == github.repository && "
            "github.event.pull_request.base.ref == github.event.repository.default_branch && "
            "github.event.pull_request.user.login == '<<GITHUB_APP_SLUG>>[bot]' && "
            "github.event.pull_request.draft == false && "
            "contains(github.event.pull_request.labels.*.name, 'factory:ready') && "
            "(github.event.action != 'labeled' || github.event.label.name == 'factory:ready')",
        )

    def test_it_refuses_unless_an_active_ruleset_pins_the_gate(self):
        """gh merges an already-mergeable pull request at once, so the ruleset must hold the merge."""
        scripts = run_blocks(self.lines)
        guard = next(block for block in scripts if "rules/branches" in block)
        enrol = next(block for block in scripts if "gh pr merge" in block)
        self.assertLess(scripts.index(guard), scripts.index(enrol))
        self.assertIn('select(.type == "required_status_checks")', guard)
        self.assertIn('.context == "factory/gate" and .integration_id != null', guard)
        self.assertIn('if [ "$held" -lt 1 ]; then', guard)
        self.assertRegex(guard, r"exit 1")
        # the base branch reaches the script through the environment, not by interpolation
        self.assertIn("BASE: ${{ github.event.pull_request.base.ref }}", self.code)

    def test_it_only_squash_merges_the_exact_head_and_only_when_the_ruleset_allows_it(self):
        scripts = "\n".join(run_blocks(self.lines))
        # Auto-merge first; GitHub refuses it on a pull request that is already mergeable
        # ("Pull request is in clean status"), so a guarded direct squash merge follows.
        self.assertIn('gh pr merge --auto --squash --match-head-commit "$HEAD_SHA" "$PR_URL"', scripts)
        self.assertIn('gh pr merge --squash --match-head-commit "$HEAD_SHA" "$PR_URL"', scripts)
        for forbidden in ("--admin", "--merge", "--rebase", "--delete-branch", "gh pr close", "gh issue"):
            self.assertNotIn(forbidden, scripts)
        self.assertEqual(scripts.count("gh pr merge"), 2)
        # The direct merge is reachable only after auto-merge failed and the pull request is CLEAN.
        direct = scripts.index('gh pr merge --squash --match-head-commit')
        self.assertLess(scripts.index("gh pr merge --auto"), direct)
        self.assertLess(scripts.index('"$merge_state" != "CLEAN"'), direct)
        # ... and only after the step that requires an active ruleset holding factory/gate.
        self.assertLess(scripts.index("required_status_checks"), scripts.index("gh pr merge"))

    def test_no_expression_is_interpolated_into_a_script(self):
        for block in run_blocks(self.lines):
            self.assertNotIn("${{", block)

    def test_the_token_reaches_gh_only_through_the_environment(self):
        self.assertIn("GH_TOKEN: ${{ steps.app-token.outputs.token }}", self.code)
        self.assertIn("PR_URL: ${{ github.event.pull_request.html_url }}", self.code)
        self.assertIn("HEAD_SHA: ${{ github.event.pull_request.head.sha }}", self.code)

    def test_runs_that_can_enrol_share_one_group_per_pull_request(self):
        """Factory puts the single label `factory:ready` on its pull request, so `opened`,
        `ready_for_review` and `labeled` runs for one pull request are serialised: the second one reaches
        the "nothing to do" guard instead of racing the first. A running enrolment is never cancelled."""
        groups = re.findall(r"(?m)^  group: .*$", self.code)
        self.assertEqual(len(groups), 1)
        self.assertTrue(
            groups[0].startswith("  group: factory-automerge-${{ github.event.pull_request.number }}${{ "),
            groups[0],
        )
        self.assertIn("cancel-in-progress: false", self.code)
        # the old per-label key made every label its own group, so two label events never queued
        self.assertNotIn("}}-${{ github.event.label.name }}", self.code)

    def test_events_for_other_labels_cannot_displace_the_enrolling_run(self):
        """A run whose job is skipped still takes its group's one pending slot, and a pending run is
        cancelled by the next one. A `labeled` event for any label but `factory:ready` (a person's, another
        tool's) never enrols, so it gets a group of its own, keyed by the run id, instead of sharing the
        pull request's group and displacing the pending `factory:ready` run, the only one that enrols."""
        self.assertIn(
            "  group: factory-automerge-${{ github.event.pull_request.number }}"
            "${{ (github.event.action == 'labeled' && github.event.label.name != 'factory:ready')"
            " && format('-other-{0}', github.run_id) || '' }}",
            self.code,
        )
        self.assertIn("cancel-in-progress: false", self.code)

    def test_the_comments_describe_the_single_label_flow(self):
        for stale in ("three labels", "factory:run/", "one after the other"):
            self.assertNotIn(stale, self.text)
        self.assertIn("single label", self.text)
        # gh's behaviour on a mergeable pull request is not asserted as a fact: the step handles both
        self.assertIn("Depending on the gh version", self.text)
        self.assertIn("Pull request is in clean status", self.text)


STUB_GH = """\
import json
import os
import sys

argv = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as log:
    log.write(f"{json.dumps(argv)}\\n")
joined = " ".join(argv)
if argv[:2] == ["pr", "view"]:
    if "autoMergeRequest" in joined:
        print(os.environ["STUB_STATE"])
    elif "mergeStateStatus" in joined:
        print(os.environ["STUB_MERGE_STATE"])
    else:
        sys.exit(99)
elif argv[:2] == ["pr", "merge"]:
    if "--auto" in argv:
        print(os.environ.get("STUB_AUTO_MESSAGE", ""), file=sys.stderr)
        sys.exit(int(os.environ["STUB_AUTO_RC"]))
    sys.exit(int(os.environ["STUB_SQUASH_RC"]))
else:
    sys.exit(99)
"""

PR_URL = "https://github.com/acme/app/pull/7"
HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
CLEAN_REFUSAL = "GraphQL: Pull request is in clean status (enablePullRequestAutoMerge)"


@unittest.skipUnless(shutil.which("bash"), "bash is required to run the workflow step")
class StepScriptTests(unittest.TestCase):
    """The control flow of the "Enable auto-merge (squash)" run block, run under bash against a stub `gh`.

    The stub answers what Factory assumes `gh` and GitHub answer (it returns the already-filtered output of
    the ``--jq`` expressions). It proves the branches of the script, not what GitHub or an installed gh
    version really does; that is the live acceptance in docs/handoff.md."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="automerge-step-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        blocks = [block for block in run_blocks(workflow_lines()) if "gh pr merge" in block]
        self.assertEqual(len(blocks), 1)
        self.script = self.tmp / "enrol.sh"
        self.script.write_text(blocks[0])
        bindir = self.tmp / "bin"
        bindir.mkdir()
        gh = bindir / "gh"
        gh.write_text("\n".join([f"#!{sys.executable}", STUB_GH]))
        gh.chmod(0o755)
        self.bindir = bindir
        self.log = self.tmp / "gh-calls.jsonl"

    def run_step(self, state="OPEN false", merge_state="CLEAN", auto_rc=1, squash_rc=0, pr_url=PR_URL):
        env = {
            "PATH": os.pathsep.join([str(self.bindir), os.environ.get("PATH", "")]),
            "HOME": str(self.tmp),
            "GH_TOKEN": "stub-token",
            "PR_URL": pr_url,
            "HEAD_SHA": HEAD_SHA,
            "STUB_LOG": str(self.log),
            "STUB_STATE": state,
            "STUB_MERGE_STATE": merge_state,
            "STUB_AUTO_RC": str(auto_rc),
            "STUB_AUTO_MESSAGE": CLEAN_REFUSAL if auto_rc else "",
            "STUB_SQUASH_RC": str(squash_rc),
        }
        done = subprocess.run(
            ["bash", "--noprofile", "--norc", "-e", str(self.script)],
            env=env,
            cwd=str(self.tmp),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return done, calls

    def merges(self, calls):
        return [call for call in calls if call[:2] == ["pr", "merge"]]

    def test_auto_merge_accepted_is_the_only_merge_call(self):
        done, calls = self.run_step(auto_rc=0)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(
            self.merges(calls),
            [["pr", "merge", "--auto", "--squash", "--match-head-commit", HEAD_SHA, PR_URL]],
        )
        # the merge state is only looked at after auto-merge was refused
        self.assertFalse([call for call in calls if "mergeStateStatus" in " ".join(call)])
        self.assertNotIn("already mergeable", done.stdout)

    def test_auto_merge_refused_on_a_clean_pull_request_squash_merges_the_exact_head(self):
        done, calls = self.run_step(auto_rc=1, merge_state="CLEAN")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("already mergeable: merging now", done.stdout)
        self.assertEqual(
            self.merges(calls),
            [
                ["pr", "merge", "--auto", "--squash", "--match-head-commit", HEAD_SHA, PR_URL],
                ["pr", "merge", "--squash", "--match-head-commit", HEAD_SHA, PR_URL],
            ],
        )
        # the direct merge is the last call, and comes after the merge state was read
        self.assertEqual(calls[-1], ["pr", "merge", "--squash", "--match-head-commit", HEAD_SHA, PR_URL])
        self.assertTrue(any("mergeStateStatus" in " ".join(call) for call in calls[:-1]))

    def test_auto_merge_refused_on_anything_but_clean_fails_without_a_direct_merge(self):
        # HAS_HOOKS and UNSTABLE are mergeable for gh, but the fallback deliberately accepts CLEAN only
        for merge_state in ("BLOCKED", "BEHIND", "DIRTY", "UNKNOWN", "DRAFT", "HAS_HOOKS", "UNSTABLE", ""):
            with self.subTest(merge_state=merge_state):
                self.log.unlink(missing_ok=True)
                done, calls = self.run_step(auto_rc=1, merge_state=merge_state)
                self.assertEqual(done.returncode, 1)
                self.assertIn(f"::error::auto-merge could not be enabled and the pull request is not mergeable ({merge_state})", done.stdout)
                self.assertEqual(len(self.merges(calls)), 1)
                self.assertIn("--auto", self.merges(calls)[0])

    def test_a_pull_request_that_is_already_handled_is_left_alone(self):
        for state in ("MERGED false", "CLOSED false", "OPEN true", "MERGED true"):
            with self.subTest(state=state):
                self.log.unlink(missing_ok=True)
                done, calls = self.run_step(state=state, auto_rc=0)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertIn(f"nothing to do ({state})", done.stdout)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][:2], ["pr", "view"])
                self.assertEqual(self.merges(calls), [])

    def test_a_refused_direct_merge_fails_the_step(self):
        """The ruleset decides: a head that moved, or a required check that is not green, makes the call fail."""
        done, calls = self.run_step(auto_rc=1, merge_state="CLEAN", squash_rc=1)
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(calls[-1][:3], ["pr", "merge", "--squash"])
        self.assertEqual(len(self.merges(calls)), 2)

    def test_the_pull_request_url_is_one_argument_and_never_run_as_a_command(self):
        hostile = "https://github.com/acme/app/pull/7;touch PWNED;$(touch PWNED2)"
        done, calls = self.run_step(auto_rc=1, merge_state="CLEAN", pr_url=hostile)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(calls[-1], ["pr", "merge", "--squash", "--match-head-commit", HEAD_SHA, hostile])
        self.assertEqual(sorted(path.name for path in self.tmp.iterdir() if path.name.startswith("PWNED")), [])

    def test_the_token_never_reaches_the_output(self):
        for auto_rc in (0, 1):
            done, _ = self.run_step(auto_rc=auto_rc)
            self.assertNotIn("stub-token", f"{done.stdout}{done.stderr}")


class DocumentTests(unittest.TestCase):
    def test_every_placeholder_in_the_files_is_explained(self):
        doc = DOC.read_text()
        names = set(PLACEHOLDER.findall(RULESET.read_text() + WORKFLOW.read_text()))
        self.assertTrue(names)
        for name in names:
            self.assertIn(f"<<{name}>>", doc, name)

    def test_the_document_covers_the_contract(self):
        doc = DOC.read_text()
        for fragment in (
            "Never grant the Factory App `workflows`",
            "merged at once",
            "covers every pull request into the default branch",
            "Closes #N",
            "factory:ready",
            "factory/gate",
            "ci_failed",
            "retry JOB",
            "invalidates the gate",
            "pull_request_target",
            "integration_id",
            "merged",
            # the fallback for a pull request that is already mergeable, and the live check of it
            "Pull request is in clean status",
            "already mergeable: merging now",
            "gh workflow disable factory-automerge.yml",
            "gh workflow enable factory-automerge.yml",
            "`CLEAN`",
            "not yet run",
        ):
            self.assertIn(fragment, doc)

    def test_the_ci_check_to_require_is_the_aggregator_and_the_reason_is_given(self):
        doc = DOC.read_text()
        flat = " ".join(doc.split())  # the document is wrapped; the fragments are not
        for fragment in (
            "Require the aggregator, not each job, when the CI has a matrix or skips jobs",
            "`CI result` (`if: always()`, `needs:` every other job, one inline step and no script)",
            "is the value of `<<REPO_CI_CHECK>>`, and the only one",
            'leaves its checks "expected" forever',
            "`CI result` fails when a needed job failed, was cancelled or was skipped, and passes only when every one succeeded",
            "tests/test_ci_workflow.py",
            # a skip is only `skipped` for a plain job; a matrix or reusable job is never required individually
            "A plain job skipped by its `if:` reports `skipped`",
            "That is true of a plain job only",
            "A matrix job skipped by a job-level `if:` is never expanded",
            "`unit tests (py${{ matrix.python }})`, not `unit tests (py3.12)`",
            "a skipped caller of a reusable workflow reports only the caller's name (for example `build`, `deploy`), never `build / compile, test, scan`",
            "A skipped matrix or reusable job must therefore never be required individually",
            # hedged: the same note below says the exact names of a skipped matrix or reusable job are UNVERIFIED
            "A matrix or reusable job that is skipped is, as far as is known, not reported under those names (see the UNVERIFIED note below), so such a job must never be required individually",
            "UNVERIFIED",
            # the verdict must not come from code the pull request can edit
            "the verdict must not come from code the pull request can edit",
            "a few lines of shell in the workflow file itself, which Factory's workflow guard holds for a person",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, flat)
        self.assertNotIn("is skipped is not reported under those names", flat)  # unverified: the doc says so itself
        # the name the document gives is the name the job reports, and the template stays a template
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        self.assertRegex(workflow, r"(?m)^  ci-result:\n    name: CI result\n    if: always\(\)\n")
        self.assertIn("`CI result`", doc)
        self.assertNotIn("CI result", RULESET.read_text())
        self.assertIn("`CI result`", (ROOT / "docs" / "enrolling-repositories.md").read_text())

    def test_the_controls_are_lettered_and_match_the_single_label_flow(self):
        doc = DOC.read_text()
        controls = doc.split("6. Controls, each its own check:", 1)[1].split("7. The already-mergeable path", 1)[0]
        for letter in "abcd":
            self.assertIn(f"- ({letter}) ", controls)
        self.assertIn("single `factory:ready` label", controls)
        self.assertIn("nothing to do (...)", controls)
        self.assertNotIn("three labels", doc)
        self.assertNotIn("factory:run/", doc)


if __name__ == "__main__":
    unittest.main()

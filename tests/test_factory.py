import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from factory.agent import run_agent
from factory.config import RepoConfig
from factory.model import MAX_REASON, criteria, describe_criterion, overlaps, runnable, validate_plan
from factory.phases import AgentOptions
from factory.runner import Runner
from factory.state import State, lock

from tests import agentstub


# A fake `gh`: PR state lives in $FAKE_PR, and every call (argv, PR body text, the
# GH_TOKEN it was given) is appended to $FAKE_GH_LOG for assertions.
FAKE_GH = """
import json, os, pathlib, re, sys
path = pathlib.Path(os.environ['FAKE_PR'])
# Issue labels live in issue-labels.json next to the PR file: {"<number>": [names]}.
label_store = path.with_name('issue-labels.json')
args = sys.argv[1:]
entry = {'args': args, 'token': os.environ.get('GH_TOKEN')}
def option(name):
    return args[args.index(name) + 1] if name in args else None
def load():
    return json.loads(path.read_text()) if path.exists() else []
def issue_labels():
    return json.loads(label_store.read_text()) if label_store.exists() else {}
issue_path = re.fullmatch(r'repos/[^/]+/[^/]+/issues/(\\d+)(/labels(?:/.+)?)?', next((a for a in args[1:] if a.startswith('repos/')), '')) if args[:1] == ['api'] else None
if '--body-file' in args:
    entry['body'] = pathlib.Path(option('--body-file')).read_text()
with open(os.environ['FAKE_GH_LOG'], 'a') as log:
    log.write(json.dumps(entry) + '\\n')
if args[:2] == ['pr', 'list']:
    # Like gh: a field is printed only when --json names it (the file may hold more).
    named = (option('--json') or '').split(',')
    print(json.dumps([{k: v for k, v in pr.items() if k in named} for pr in load()] if '--json' in args else load()))
elif args[:2] == ['pr', 'create']:
    assert option('--base') == os.environ.get('FAKE_BASE', 'main'), args
    labels = [args[i + 1] for i, a in enumerate(args) if a == '--label']
    path.write_text(json.dumps([{'url': 'https://example.invalid/pr/1', 'state': 'OPEN', 'baseRefName': option('--base'), 'headRefOid': 'fake', 'isDraft': '--draft' in args, 'labels': labels}]))
    print('https://example.invalid/pr/1')
elif args[:2] == ['pr', 'ready']:
    prs = load()
    prs[0]['isDraft'] = '--undo' in args
    path.write_text(json.dumps(prs))
elif args[:2] == ['issue', 'list']:
    issues = pathlib.Path(os.environ.get('FAKE_ISSUES', ''))
    print(issues.read_text() if issues.is_file() else '[]')
elif args[:2] == ['repo', 'view']:
    print(os.environ.get('FAKE_DEFAULT_BRANCH', 'main'))
elif os.environ.get('FAKE_ISSUE_WRITE_FAIL') and (
    (issue_path and args[1:3] in (['-X', 'POST'], ['-X', 'DELETE']))
    or (args[:2] == ['label', 'create'] and args[2] != 'factory:ready')
):
    sys.stderr.write('HTTP 403 Resource not accessible by integration')
    sys.exit(1)
elif issue_path and os.environ.get('FAKE_ISSUE_API_FAIL'):
    sys.stderr.write('HTTP 403 SECRET-ISSUE-TEXT Resource not accessible by integration')
    sys.exit(1)
elif issue_path and args[1:3] != ['-X', 'POST'] and args[1:3] != ['-X', 'DELETE']:
    print(json.dumps({'state': 'open', 'labels': issue_labels().get(issue_path.group(1), [])}))
elif issue_path and args[:3] == ['api', '-X', 'POST']:
    store = issue_labels()
    for a in args:
        if a.startswith('labels[]='):
            store.setdefault(issue_path.group(1), []).append(a.split('=', 1)[1])
    label_store.write_text(json.dumps(store))
elif issue_path and args[:3] == ['api', '-X', 'DELETE']:
    store = issue_labels()
    name = __import__('urllib.parse').parse.unquote(args[3].rsplit('/', 1)[1])
    store[issue_path.group(1)].remove(name)
    label_store.write_text(json.dumps(store))
elif args == ['api', 'user', '--jq', '.login']:
    print('factory-bot[bot]')
elif os.environ.get('FAKE_OBSERVED_FAIL') and args[:3] == ['api', '-X', 'POST'] and args[3].endswith('/issues'):
    sys.stderr.write('HTTP 403 SECRET-ISSUE-TEXT Resource not accessible by integration')
    sys.exit(1)
elif args[:2] == ['pr', 'edit'] or args[:2] == ['label', 'create'] or args[:1] == ['api']:
    pass
else:
    raise RuntimeError(args)
"""


def assignment():
    return {
        "version": 1,
        "artifact_roots": ["plans/active", "plans/archive"],
        "issues": [{"number": 1, "title": "Fix A", "body": "A"}],
        "jobs": [
            {
                "id": "fix-a",
                "issues": [1],
                "context_issues": [],
                "objective": "Fix A",
                "likely_paths": ["src"],
                "acceptance": ["A works"],
                "depends_on": [],
                "feature": False,
            }
        ],
        "dispositions": [
            {"issue": 1, "status": "job", "job": "fix-a", "reason": "Concrete fix"}
        ],
    }


class PlanTests(unittest.TestCase):
    def test_ownership_dependencies_and_paths_are_validated(self):
        mutations = [
            lambda p: p["issues"].append(p["issues"][0]),
            lambda p: p["dispositions"].clear(),
            lambda p: p["dispositions"][0].update(status="covered"),
            lambda p: p["jobs"][0].update(depends_on=["missing"]),
            lambda p: p["jobs"][0].update(depends_on=["fix-a"]),
            lambda p: p["jobs"][0].update(likely_paths=["../elsewhere"]),
            lambda p: p["jobs"][0].update(likely_paths=["src/*"]),
            lambda p: p["jobs"][0].update(feature="false"),
        ]
        validate_plan(assignment())
        for mutation in mutations:
            plan = assignment()
            mutation(plan)
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                validate_plan(plan)

    def criteria_of(self, *acceptance):
        plan = assignment()
        plan["jobs"][0]["acceptance"] = list(acceptance)
        return validate_plan(plan)["jobs"][0]

    def test_tagged_criteria_are_accepted_and_plain_ones_default_to_sandbox(self):
        job = self.criteria_of(
            "plain text",
            {"text": "untagged object"},
            {"text": "tagged", "tag": "operator"},
            {"text": "ci by checks", "tag": "ci", "checks": ["build", "scan"]},
            {"text": "ci by workflow", "tag": "ci", "workflow": "release"},
        )
        self.assertEqual(
            [(c["text"], c["tag"], c["checks"], c.get("workflow")) for c in criteria(job)],
            [
                ("plain text", "sandbox", [], None),
                ("untagged object", "sandbox", [], None),
                ("tagged", "operator", [], None),
                ("ci by checks", "ci", ["build", "scan"], None),
                ("ci by workflow", "ci", [], "release"),
            ],
        )
        # validation never rewrites the plan: its digest pins the run
        self.assertEqual(job["acceptance"][0], "plain text")
        self.assertEqual(
            describe_criterion(job["acceptance"][3]), "[ci] ci by checks (checks: build, scan)"
        )
        self.assertEqual(describe_criterion("plain text"), "[sandbox] plain text")

    def test_invalid_criteria_are_rejected_without_echoing_their_text(self):
        secret = "TOPSECRET-text"
        bad = [
            {"text": secret, "tag": "prod"},
            {"text": secret, "tag": ["ci"]},
            {"text": secret, "tag": "ci"},
            {"text": secret, "tag": "ci", "checks": []},
            {"text": secret, "tag": "ci", "checks": [""]},
            {"text": secret, "tag": "ci", "checks": ["a\nb"]},
            {"text": secret, "tag": "ci", "checks": ["a", "a"]},
            {"text": secret, "tag": "ci", "checks": "build"},
            {"text": secret, "tag": "sandbox", "checks": ["build"]},
            {"text": secret, "tag": "operator", "workflow": "w"},
            {"text": secret, "run": "rm -rf /"},
            {"text": "  "},
            {"tag": "sandbox"},
            "",
            "   ",
            7,
            None,
        ]
        for item in bad:
            with self.subTest(item=item), self.assertRaises(ValueError) as caught:
                self.criteria_of(item)
            self.assertNotIn(secret, str(caught.exception))
        with self.assertRaises(ValueError):
            self.criteria_of()
        plan = assignment()
        plan["jobs"][0]["acceptance"] = "not a list"
        with self.assertRaises(ValueError):
            validate_plan(plan)

    def test_an_operator_criterion_may_carry_a_reason(self):
        job = self.criteria_of(
            "plain",
            {"text": "prod", "tag": "operator", "reason": "needs production"},
            {"text": "no reason", "tag": "operator"},
            {"text": "longest", "tag": "operator", "reason": "r" * MAX_REASON},
        )
        canonical = criteria(job)
        self.assertEqual(canonical[1]["reason"], "needs production")
        self.assertEqual(canonical[3]["reason"], "r" * MAX_REASON)
        self.assertNotIn("reason", canonical[0])
        self.assertNotIn("reason", canonical[2])
        self.assertEqual(job["acceptance"][1]["reason"], "needs production")
        # the Acceptance line of the PR body and the status line stay as before
        self.assertEqual(describe_criterion(job["acceptance"][1]), "[operator] prod")

    def reason_of(self, **extra):
        """The canonical criterion of one ``operator`` criterion carrying the given reason."""
        job = self.criteria_of({"text": "x", "tag": "operator", **extra})
        return criteria(job)[0]

    def test_an_over_long_reason_is_cut_and_never_rejects_the_plan(self):
        long = "TOPSECRET-reason " * 30
        kept = self.reason_of(reason=long)["reason"]
        self.assertLessEqual(len(kept), MAX_REASON)
        self.assertTrue(kept.endswith("..."))
        self.assertTrue(long.startswith(kept[:-3]))
        # criteria() runs on every read: cutting a cut reason changes nothing
        self.assertEqual(self.reason_of(reason=kept)["reason"], kept)
        # one character over the limit is cut too, the limit itself is kept whole
        self.assertEqual(len(self.reason_of(reason="r" * (MAX_REASON + 1))["reason"]), MAX_REASON)
        self.assertEqual(self.reason_of(reason="r" * MAX_REASON)["reason"], "r" * MAX_REASON)
        # what is kept is judged after the cut: nothing visible survives, so nothing is kept
        self.assertNotIn("reason", self.reason_of(reason=f"{' ' * MAX_REASON}visible"))

    def test_a_reason_that_shows_nothing_is_dropped_and_never_rejects_the_plan(self):
        for reason in (
            "",
            "   ",
            "\n\t \r",
            "\u200b" * 10,
            "\x00",
            "\ufeff\u200d\u2060",
            "\u3164\u2800",
            f" \u200b\x00{' ' * 20}",
        ):
            with self.subTest(reason=reason):
                self.assertNotIn("reason", self.reason_of(reason=reason))
        self.assertEqual(self.reason_of(reason="\u200bneeds prod")["reason"], "\u200bneeds prod")

    def test_a_reason_on_a_tag_that_takes_none_is_dropped_and_never_rejects_the_plan(self):
        job = self.criteria_of(
            {"text": "x", "reason": "misplaced"},
            {"text": "x", "tag": "sandbox", "reason": "misplaced"},
            {"text": "x", "tag": "ci", "checks": ["build"], "reason": "misplaced"},
            {"text": "x", "tag": "ci", "workflow": "release", "reason": "m" * 5000},
        )
        found = criteria(job)
        self.assertEqual([c["tag"] for c in found], ["sandbox", "sandbox", "ci", "ci"])
        self.assertTrue(all("reason" not in c for c in found))
        self.assertEqual(found[2]["checks"], ["build"])
        self.assertEqual(found[3]["workflow"], "release")

    def test_a_reason_that_is_not_text_is_still_a_shape_error_without_echoing_it(self):
        secret = "TOPSECRET-reason"
        bad = [
            {"text": "x", "tag": "operator", "reason": 7},
            {"text": "x", "tag": "operator", "reason": [secret]},
            {"text": "x", "tag": "operator", "reason": {"why": secret}},
            {"text": "x", "tag": "operator", "reason": False},
            {"text": "x", "reason": [secret]},
            {"text": "x", "tag": "ci", "checks": ["build"], "reason": 3.5},
        ]
        for item in bad:
            with self.subTest(item=item):
                with self.assertRaises(ValueError) as caught:
                    self.criteria_of(item)
                self.assertNotIn(secret, str(caught.exception))
        # an unknown key is still refused
        with self.assertRaises(ValueError):
            self.criteria_of({"text": "x", "tag": "operator", "why": "no"})

    def test_scheduler_waits_for_merge_and_locks_path_boundaries(self):
        self.assertTrue(overlaps(["."], ["src/a"]))
        self.assertTrue(overlaps(["src"], ["src/a"]))
        self.assertFalse(overlaps(["src"], ["src-other"]))
        job = assignment()["jobs"][0]
        job["depends_on"] = ["prerequisite"]
        jobs = {"fix-a": job, "other": {"likely_paths": ["src/a"]}}
        state = {"fix-a": {"status": "queued"}, "prerequisite": {"status": "pr_ready"}}
        self.assertFalse(runnable(job, state, [], jobs))
        state["prerequisite"]["status"] = "merged"
        self.assertTrue(runnable(job, state, [], jobs))
        self.assertFalse(runnable(job, state, ["other"], jobs))


class StateTests(unittest.TestCase):
    def test_crash_recovery_requires_explicit_retry_and_refuses_live_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = State(Path(tmp), assignment())
            state.update("fix-a", status="running", pid=99)
            with (
                patch("factory.state.alive", return_value=True),
                self.assertRaises(RuntimeError),
            ):
                state.recover()
            self.assertEqual(state.data["jobs"]["fix-a"]["status"], "running")
            with patch("factory.state.alive", return_value=False):
                state.recover()
            reloaded = State(Path(tmp), assignment())
            self.assertEqual(reloaded.data["jobs"]["fix-a"]["status"], "interrupted")
            self.assertIsNone(reloaded.data["jobs"]["fix-a"]["pid"])

    def test_repository_lock_is_exclusive(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            lock(Path(tmp) / "lock"),
            self.assertRaises(RuntimeError),
            lock(Path(tmp) / "lock"),
        ):
            self.fail("second lock acquired")


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.run_dir = self.root / "pilot"
        self.run_dir.mkdir()
        self.binary = self.root / "bin"
        self.binary.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.email", "factory@example.invalid")
        self.git("config", "user.name", "Factory Test")
        (self.repo / "src").mkdir()
        (self.repo / "src/a.txt").write_text("before\n")
        self.git("add", ".")
        self.git("commit", "-m", "base")
        origin = self.root / "origin.git"
        subprocess.run(
            ["git", "init", "--bare", str(origin)], capture_output=True, check=True
        )
        self.git("remote", "add", "origin", str(origin))
        self.git("push", "-u", "origin", "main")
        self.install_agent(
            """
prompt = sys.stdin.read()
if 'independent reviewer' in prompt:
    result = {'pass': True, 'findings': []}
else:
    if pathlib.Path(__file__).with_name('slow-enabled').exists():
        import time; time.sleep(30)
    pathlib.Path('src/a.txt').write_text('fixed\\n')
    if pathlib.Path(__file__).with_name('outside-enabled').exists():
        pathlib.Path('outside.txt').write_text('unexpected')
    result = {'summary': 'Fix A', 'blocked': False, 'blocker': '', 'tests': ['fake focused check passed'], 'feature': False}
if pathlib.Path(__file__).with_name('bad-feature-enabled').exists() and 'feature' in result:
    result['feature'] = None
emit(result)
"""
        )
        self.script("gh", FAKE_GH)
        self.env = patch.dict(
            os.environ,
            {
                "PATH": str(self.binary) + os.pathsep + os.environ["PATH"],
                "FACTORY_AGENT_CMD": str(self.binary / "fake-agent"),
                "FAKE_PR": str(self.root / "pr.json"),
                "FAKE_GH_LOG": str(self.root / "gh-calls.jsonl"),
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def script(self, name, source):
        path = self.binary / name
        path.write_text(f"#!{sys.executable}\n" + source)
        path.chmod(0o755)

    def gh_calls(self):
        log = self.root / "gh-calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def gate_statuses(self):
        """(state, description) of every factory/gate status the supervisor posted, in order."""
        found = []
        for call in self.gh_calls():
            args = call["args"]
            if args[:1] == ["api"] and "context=factory/gate" in args:
                values = dict(a.split("=", 1) for a in args if "=" in a and a.split("=", 1)[0] in {"state", "description"})
                found.append((values["state"], values["description"], args[3]))
        return found

    def install_agent(self, body):
        """Install the fake Claude executable that FACTORY_AGENT_CMD points at."""
        self.script("fake-agent", agentstub.source(body))

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.repo, text=True, capture_output=True, check=True
        ).stdout.strip()

    def test_real_worktrees_commit_review_draft_and_resume_without_duplicate(self):
        runner = Runner(self.repo, self.run_dir, assignment(), timeout=10)
        runner.run()
        state = runner.state.data["jobs"]["fix-a"]
        self.assertEqual(state["status"], "pr_ready", state.get("error"))
        self.assertEqual(len(state["usage"]), 2)
        worktrees = self.git("worktree", "list", "--porcelain")
        self.assertIn("fix-a-review-1-0", worktrees)
        self.assertEqual((self.repo / "src/a.txt").read_text(), "before\n")
        self.assertEqual((Path(state["worktree"]) / "src/a.txt").read_text(), "fixed\n")
        # Crash after PR creation but before recording its URL: reconcile it.
        runner.state.update("fix-a", status="running", pr=None)
        resumed = Runner(self.repo, self.run_dir, assignment(), timeout=10)
        resumed.run()
        self.assertEqual(resumed.state.data["jobs"]["fix-a"]["status"], "pr_incomplete")
        self.assertEqual(
            resumed.state.data["jobs"]["fix-a"]["pr"], "https://example.invalid/pr/1"
        )
        self.assertEqual(resumed.state.data["jobs"]["fix-a"]["attempt"], 1)

    def test_reconcile_merged_pr_clears_previous_attempt_error(self):
        runner = Runner(self.repo, self.run_dir, assignment(), timeout=10)
        runner.state.update(
            "fix-a",
            status="blocked",
            branch="factory/manual-fix",
            error="old setup failure",
        )
        (self.root / "pr.json").write_text(
            json.dumps(
                [
                    {
                        "url": "https://example.invalid/pr/1",
                        "state": "MERGED",
                        "baseRefName": "main",
                        "headRefOid": "fixed",
                    }
                ]
            )
        )
        runner.reconcile()
        state = runner.state.data["jobs"]["fix-a"]
        self.assertEqual(state["status"], "merged")
        self.assertIsNone(state["error"])

    def test_scope_estimate_allows_reviewed_caller_changes_and_records_them(self):
        (self.binary / "outside-enabled").touch()
        runner = Runner(self.repo, self.run_dir, assignment(), timeout=10)
        runner.run()
        state = runner.state.data["jobs"]["fix-a"]
        self.assertEqual(state["status"], "pr_ready", state.get("error"))
        self.assertEqual(state["additional_paths"], ["outside.txt"])
        self.assertIn("outside.txt", state["actual_paths"])
        self.assertTrue((Path(state["worktree"]) / "outside.txt").exists())
        self.assertTrue((self.root / "pr.json").exists())

    def test_a_session_that_hits_the_timeout_fails_the_job_with_the_fixed_reason(self):
        from factory.status import format_status, status_report

        (self.binary / "slow-enabled").touch()
        runner = Runner(self.repo, self.run_dir, assignment(), timeout=1)
        runner.run()
        state = runner.state.data["jobs"]["fix-a"]
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["error"], "timed out after 1 s")  # not the command line
        self.assertFalse((self.root / "pr.json").exists())
        row = status_report(runner.plan, runner.state.data)["jobs"][0]
        self.assertEqual((row["failure"], row["timeout_seconds"], row["error"]), ("timeout", 1, "timed out after 1 s"))
        self.assertIn("auto_build_timeout_seconds", row["next_action"])
        self.assertIn("split the issue", row["next_action"])
        self.assertIn("timed out after 1 s", format_status(status_report(runner.plan, runner.state.data)))

    def test_malformed_builder_feature_cannot_skip_verification(self):
        (self.binary / "bad-feature-enabled").touch()
        runner = Runner(self.repo, self.run_dir, assignment(), timeout=10)
        runner.run()
        state = runner.state.data["jobs"]["fix-a"]
        self.assertEqual(state["status"], "failed")
        self.assertIn("invalid builder response", state["error"])
        self.assertFalse((self.root / "pr.json").exists())

    def test_agent_environment_excludes_supervisor_secrets_and_network_is_explicit(
        self,
    ):
        self.install_agent(
            """
emit({'keys': sorted(os.environ), 'args': ARGS, 'settings': json.loads(pathlib.Path(arg('--settings')).read_text())})
"""
        )
        with patch.dict(
            os.environ,
            {
                "SUPERVISOR_SECRET": "synthetic-test-value",
                "GH_TOKEN": "synthetic-token",
                "ANTHROPIC_API_KEY": "synthetic-anthropic-value",
                "CLAUDE_CODE_EFFORT_LEVEL": "max",
            },
        ):
            result, _ = run_agent("test", self.repo, self.run_dir / "environment", "build")
            online, _ = run_agent(
                "test",
                self.repo,
                self.run_dir / "online",
                "verify",
                options=AgentOptions(verify_domains=("registry.npmjs.org",)),
            )
        self.assertNotIn("SUPERVISOR_SECRET", result["keys"])
        self.assertNotIn("GH_TOKEN", result["keys"])
        self.assertNotIn("CLAUDE_CODE_EFFORT_LEVEL", result["keys"])
        self.assertIn("PATH", result["keys"])
        self.assertEqual(
            result["settings"]["sandbox"]["network"]["allowedDomains"], []
        )
        self.assertEqual(
            online["settings"]["sandbox"]["network"]["allowedDomains"],
            ["registry.npmjs.org"],
        )

    def test_each_invocation_records_phase_model_and_cost(self):
        runner = Runner(self.repo, self.run_dir, assignment(), timeout=10)
        runner.run()
        usage = runner.state.data["jobs"]["fix-a"]["usage"]
        self.assertEqual([u["phase"] for u in usage], ["build", "review"])
        self.assertEqual([u["model"] for u in usage], ["claude-sonnet-5-5", "claude-opus-5-5"])
        self.assertEqual([u["total_cost_usd"] for u in usage], [0.0125, 0.0125])
        from factory.status import status_report

        report = status_report(assignment(), runner.state.data)
        self.assertEqual(report["jobs"][0]["cost_usd"], 0.025)
        self.assertEqual(report["cost_usd"], 0.025)
        # No per-invocation HOME survives, and the fake saw the throwaway HOME.
        self.assertEqual(list(self.run_dir.rglob(".home-*")), [])

    def test_failed_agent_call_still_records_its_cost_and_fails_the_job(self):
        self.install_agent(
            """
sys.stdin.read()
emit(None, subtype='error_max_budget_usd', is_error=True, total_cost_usd=8.5, result='PRIVATE-AGENT-TEXT')
sys.exit(1)
"""
        )
        runner = Runner(self.repo, self.run_dir, assignment(), timeout=10)
        runner.run()
        state = runner.state.data["jobs"]["fix-a"]
        self.assertEqual(state["status"], "failed")
        self.assertIn("error_max_budget_usd", state["error"])
        self.assertNotIn("PRIVATE-AGENT-TEXT", state["error"])
        self.assertEqual([u["total_cost_usd"] for u in state["usage"]], [8.5])
        self.assertFalse((self.root / "pr.json").exists())

    def test_worker_gets_base_commit_claude_md_not_the_worktree_copy(self):
        (self.repo / "CLAUDE.md").write_text("trusted base guidance\n")
        self.git("add", ".")
        self.git("commit", "-m", "add guidance")
        self.git("push", "origin", "main")
        self.install_agent(
            """
sys.stdin.read()
pathlib.Path('CLAUDE.md').write_text('planted by builder')
seen = pathlib.Path(arg('--append-system-prompt-file')).read_text()
pathlib.Path(__file__).with_name('seen-system-prompt').write_text(seen)
emit({'pass': True, 'findings': []} if 'independent reviewer' in sys.argv else {'summary': 's', 'blocked': False, 'blocker': '', 'tests': [], 'feature': False})
"""
        )
        runner = Runner(self.repo, self.run_dir, assignment(), timeout=10)
        job = assignment()["jobs"][0]
        runner.state.update("fix-a", attempt=1)
        worktree = runner.prepare_worktree(job)
        runner.invoke("fix-a", "build-0", "prompt", worktree)
        seen = (self.binary / "seen-system-prompt").read_text()
        self.assertIn("trusted base guidance", seen)
        self.assertNotIn("planted", seen)

    def test_committed_builder_paths_are_recorded_and_unrelated_head_rejected(self):
        runner = Runner(self.repo, self.run_dir, assignment(), timeout=10)
        job = assignment()["jobs"][0]
        base = self.git("rev-parse", "HEAD")
        runner.state.update("fix-a", branch="main", base=base)
        (self.repo / "outside.txt").write_text("caller update")
        self.git("add", ".")
        self.git("commit", "-m", "builder committed caller change")
        runner.commit(job, self.repo)
        self.assertEqual(
            runner.state.data["jobs"]["fix-a"]["additional_paths"], ["outside.txt"]
        )
        self.git("checkout", "--orphan", "unrelated")
        self.git("commit", "-m", "unrelated history")
        runner.state.update("fix-a", branch="unrelated")
        with self.assertRaises(subprocess.CalledProcessError):
            runner.commit(job, self.repo)

    def test_timeout_kills_descendant_even_when_parent_exits_on_term(self):
        self.script(
            "fake-agent",
            """
import os, pathlib, signal, time
child = os.fork()
if child == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    pathlib.Path('child.pid').write_text(str(os.getpid()))
    time.sleep(30)
else:
    time.sleep(30)
""",
        )
        with self.assertRaises(subprocess.TimeoutExpired):
            run_agent(
                "test", self.repo, self.run_dir / "descendant", "build", timeout=0.5
            )
        child = (self.repo / "child.pid").read_text()
        status = subprocess.run(
            ["ps", "-o", "stat=", "-p", child],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        self.assertTrue(not status or status.startswith("Z"), status)

    def test_agent_timeout_is_failure_and_retains_logs(self):
        self.script("fake-agent", "import time\ntime.sleep(20)\n")
        with self.assertRaises(subprocess.TimeoutExpired):
            run_agent("test", self.repo, self.run_dir / "timeout", "build", timeout=0.1)
        self.assertTrue((self.run_dir / "timeout.stderr").exists())

    # ---- issues opened for what a worker observed (src/factory/observed_issues.py)

    OBSERVATION = (
        "{'kind': 'bug_found', 'title': 'The sandbox has no jq binary', "
        "'detail': 'Every job reports failing tests that need jq.', 'paths': ['tests/run.sh']}"
    )

    def observing_agent(self, builder="", reviewer="", blocked=False):
        """A fake agent whose builder and reviewer results carry the given observations (Python source lists)."""
        self.install_agent(
            f"""
prompt = sys.stdin.read()
if 'independent reviewer' in prompt:
    result = {{'pass': True, 'findings': [], 'observations': [{reviewer}]}}
else:
    if not {blocked!r}:
        pathlib.Path('src/a.txt').write_text('fixed\\n')
    result = {{'summary': 'Fix A', 'blocked': {blocked!r}, 'blocker': 'needs jq', 'tests': [], 'feature': False,
              'observations': [{builder}]}}
emit(result)
"""
        )

    def run_observed(self, **config):
        runner = Runner(
            self.repo, self.run_dir, assignment(), timeout=10, cfg=RepoConfig(repo="org/name", **config)
        )
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            runner.run()
        return runner, err.getvalue()

    def issue_posts(self):
        return [
            call["args"]
            for call in self.gh_calls()
            if call["args"][:3] == ["api", "-X", "POST"] and call["args"][3] == "repos/org/name/issues"
        ]

    @staticmethod
    def field(args, name):
        return next(a.split("=", 1)[1] for a in args if a.startswith(f"{name}="))

    def test_a_builder_observation_becomes_one_issue_when_the_job_ends(self):
        self.observing_agent(builder=self.OBSERVATION)
        runner, err = self.run_observed()
        state = runner.state.data["jobs"]["fix-a"]
        self.assertEqual(state["status"], "pr_ready", state.get("error"))
        self.assertEqual(err, "")
        (post,) = self.issue_posts()
        self.assertEqual(self.field(post, "title"), "[Factory observed] The sandbox has no jq binary")
        self.assertEqual(self.field(post, "labels[]"), "factory:observed")
        body = self.field(post, "body")
        self.assertIn("Reported by the builder of job `fix-a` in run `pilot`", body)
        self.assertIn("worked on the issue (#1).", body)
        self.assertIn("Paths: tests/run.sh", body)
        self.assertNotIn("factory-build", json.dumps(post[4:]).replace("apply the `factory-build` label yourself", ""))
        # The raw result is not kept in the state: the validated observations are.
        self.assertNotIn("observations", state["build"])
        self.assertEqual([item["title"] for item in state["observed"]], ["The sandbox has no jq binary"])
        # Review and verification did not wait for, or depend on, the issue.
        self.assertEqual(state["review"], {"pass": True, "findings": []})

    def test_the_job_is_not_changed_by_an_observation_and_nothing_goes_into_the_pull_request(self):
        self.observing_agent(builder=self.OBSERVATION)
        runner, _ = self.run_observed()
        creates = [c for c in self.gh_calls() if c["args"][:2] == ["pr", "create"]]
        self.assertEqual(len(creates), 1)
        self.assertNotIn("jq", creates[0].get("body", ""))
        self.assertNotIn("observ", creates[0].get("body", "").lower())

    def test_a_blocked_builder_still_reports_what_it_could_not_do(self):
        self.observing_agent(builder=self.OBSERVATION, blocked=True)
        runner, _ = self.run_observed()
        state = runner.state.data["jobs"]["fix-a"]
        self.assertEqual((state["status"], state["error"]), ("blocked", "needs jq"))
        (post,) = self.issue_posts()
        self.assertNotIn("Pull request", self.field(post, "body"))

    def test_a_reviewer_observation_is_reported_too_and_never_changes_the_review(self):
        reviewer = "{'kind': 'follow_up', 'title': 'The helper next to this diff is untested', 'detail': 'Nothing covers it.'}"
        self.observing_agent(reviewer=reviewer)
        runner, _ = self.run_observed()
        state = runner.state.data["jobs"]["fix-a"]
        self.assertEqual(state["status"], "pr_ready")
        self.assertEqual(state["review"], {"pass": True, "findings": []})
        (post,) = self.issue_posts()
        self.assertIn("Reported by the reviewer of job `fix-a`", self.field(post, "body"))

    def test_an_invalid_observation_is_dropped_with_one_fixed_line_and_the_job_goes_on(self):
        self.observing_agent(builder=f"{self.OBSERVATION}, {{'kind': 'gossip', 'title': 'x', 'detail': 'y'}}")
        runner, err = self.run_observed()
        self.assertEqual(runner.state.data["jobs"]["fix-a"]["status"], "pr_ready")
        self.assertEqual(err, "factory: observation dropped job=fix-a reason=kind\n")
        self.assertEqual(len(self.issue_posts()), 1)

    def test_a_refused_issue_never_fails_the_job(self):
        self.observing_agent(builder=self.OBSERVATION)
        with patch.dict(os.environ, {"FAKE_OBSERVED_FAIL": "1"}):
            runner, err = self.run_observed()
        state = runner.state.data["jobs"]["fix-a"]
        self.assertEqual(state["status"], "pr_ready", state.get("error"))
        self.assertEqual(err, "factory: observed issue failed (CalledProcessError)\n")
        self.assertNotIn("SECRET-ISSUE-TEXT", err)
        self.assertEqual(len(state["observed"]), 1)  # kept: the next reconcile opens it

    def test_the_switches_suppress_everything(self):
        self.observing_agent(builder=self.OBSERVATION, reviewer=self.OBSERVATION)
        runner, err = self.run_observed(observed_issues=False)
        self.assertEqual((self.issue_posts(), err), ([], ""))
        self.assertNotIn("observed", runner.state.data["jobs"]["fix-a"])
        self.assertNotIn("observations", runner.state.data["jobs"]["fix-a"]["build"])

    def test_the_kill_switch_suppresses_everything(self):
        self.observing_agent(builder=self.OBSERVATION)
        with patch.dict(os.environ, {"FACTORY_OBSERVED_ISSUES": "off"}):
            runner, _ = self.run_observed()
        self.assertEqual(self.issue_posts(), [])
        self.assertEqual(runner.state.data["jobs"]["fix-a"]["status"], "pr_ready")
        self.assertNotIn("observed", runner.state.data["jobs"]["fix-a"])

    def test_a_run_without_a_configured_repository_opens_nothing(self):
        self.observing_agent(builder=self.OBSERVATION)
        runner = Runner(self.repo, self.run_dir, assignment(), timeout=10)
        runner.run()
        self.assertEqual(self.issue_posts(), [])
        self.assertNotIn("observed", runner.state.data["jobs"]["fix-a"])


if __name__ == "__main__":
    unittest.main()

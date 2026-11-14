"""``plan --issues N --append-to RUN``: a fake planner and a fake ``gh``, no network, no live run.

The planner is ``factory.planner.run_agent`` replaced by a function that records its prompt and
returns a canned result; ``gh`` is the fake of test_plan_issues (so the tests can show which issues
had their text fetched); the planner worktree is a temp directory.
"""

import contextlib
import copy
import hashlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from factory import append, cli, fragments
from factory.config import RepoConfig
from factory.control import RunControl, request_drain
from factory.issue_labels import LABEL_BLOCKED
from factory.model import validate_plan
from factory.planner import planner_usage_records
from factory.runner import Runner
from tests.test_factory import assignment
from tests.test_plan_fragments import job
from tests.test_plan_issues import SECRET, FakeGh, entry

RESULT_B = {
    "version": 1,
    "jobs": [
        {
            "id": "fix-b",
            "issues": [2],
            "context_issues": [],
            "objective": "Fix B",
            "likely_paths": ["docs/b"],
            "acceptance": ["B works"],
            "depends_on": [],
            "feature": False,
        }
    ],
    "dispositions": [{"issue": 2, "status": "job", "job": "fix-b", "reason": "ok"}],
}


def disposed(number, status, reason="because"):
    return {"issue": number, "status": status, "reason": reason}


class AppendCase(FakeGh):
    def setUp(self):
        super().setUp()
        self.directory = self.root / "run1"
        self.directory.mkdir()
        self.plan = assignment()
        self.cfg = RepoConfig(repo="org/name")
        self.serve(
            n1=entry("one"), n2=entry("two"), n3=entry("three"),
            n4=entry("four", state="closed"), n5=entry("five", "factory-ignore"),
            n6=entry("six", pr=True),
        )
        self.prompts, self.trees = [], []
        self.result = copy.deepcopy(RESULT_B)
        self.usage = {"total_cost_usd": 0.1}
        self.start()

    def start(self, state=True):
        """A started run: plan.json, its pin and a state.json written by the supervisor's State."""
        digest = fragments.plan_digest(self.plan)
        (self.directory / "plan.json").write_text(json.dumps(self.plan, indent=2))
        (self.directory / "plan.sha256").write_text(digest)
        self.runner = Runner(self.directory, self.directory, self.plan, cfg=self.cfg)
        self.runner.issue_labels.sync = lambda wanted: None
        if state:
            self.runner.state.save()

    def fake_agent(self, prompt, cwd, prefix, phase, *args, **kwargs):
        self.prompts.append(prompt)
        self.trees.append(Path(cwd))
        return copy.deepcopy(self.result), dict(self.usage)

    def worktree(self, cfg, repo, directory, name="planner"):
        tree = directory / "worktrees" / name
        tree.mkdir(parents=True, exist_ok=True)
        return tree

    def append(self, issues=(2,), cfg=None, directory=None, **args):
        namespace = SimpleNamespace(
            issues=list(issues), limit=None, use_snapshot=False, timeout=60, append_to="run1", **args
        )
        cfg = cfg or self.cfg
        with (
            patch("factory.planner.run_agent", side_effect=self.fake_agent),
            patch.object(cli, "planner_worktree", side_effect=self.worktree) as worktree,
            patch.object(cli, "ensure_clone", return_value=self.work),
            patch.object(cli, "default_branch", return_value=cfg.base_branch),
        ):
            self.worktree_calls = worktree.call_args_list
            return self.capture(cli.append_command, namespace, cfg, None, directory or self.directory)

    def fragments(self):
        inbox = self.directory / "inbox"
        return sorted(inbox.iterdir()) if inbox.exists() else []

    def snapshot_files(self):
        names = ("plan.json", "plan.sha256", "state.json")
        return {
            name: (self.directory / name).read_bytes() if (self.directory / name).exists() else None
            for name in names
        }

    def assertRefused(self, message, **kwargs):
        before = self.snapshot_files()
        self.prompts.clear()
        with self.assertRaises(ValueError) as caught:
            self.append(**kwargs)
        self.assertEqual(str(caught.exception), f"append refused: {message}")
        self.assertEqual(self.prompts, [])
        self.assertEqual(self.fragments(), [])
        self.assertEqual(self.fetched(), [])
        self.assertEqual(self.snapshot_files(), before)


class AppendWritesFragmentTests(AppendCase):
    def test_a_job_is_written_atomically_and_then_ingested(self):
        before = self.snapshot_files()
        renames = []
        real = os.replace

        def watch(source, target, **kwargs):
            renames.append((source, target))
            return real(source, target, **kwargs)

        with patch("factory.append.os.replace", side_effect=watch):
            self.append()
        (path,) = self.fragments()
        self.assertTrue(fragments.NAME_PATTERN.fullmatch(path.name))
        renames = [r for r in renames if fragments.NAME_PATTERN.fullmatch(str(r[1]))]
        self.assertEqual(len(renames), 1)
        self.assertTrue(renames[0][0].startswith(".") and renames[0][0].endswith(".tmp"))
        self.assertEqual(renames[0][1], path.name)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        issues, jobs, dispositions = fragments.parse(path.read_bytes(), 10)
        self.assertEqual([i["number"] for i in issues], [2])
        self.assertEqual([j["id"] for j in jobs], ["fix-b"])
        self.assertEqual(dispositions, [])
        self.assertEqual(self.snapshot_files(), before)
        self.assertEqual(self.fetched(), [2])
        self.assertEqual(self.err, "")
        self.assertNotIn(SECRET, self.out)
        # The supervisor applies it.
        with contextlib.redirect_stderr(io.StringIO()):
            self.runner.ingest_fragments()
        self.assertEqual([j["id"] for j in self.runner.plan["jobs"]], ["fix-a", "fix-b"])
        self.assertEqual(self.runner.state.data["jobs"]["fix-b"]["status"], "queued")
        saved = json.loads((self.directory / "plan.json").read_text())
        validate_plan(saved)
        self.assertEqual(self.fragments(), [path.with_name(path.name + ".applied")])

    def test_an_appended_job_that_changes_workflow_files_is_marked_when_it_is_appended(self):
        self.result["jobs"][0]["likely_paths"] = ["docs/b", ".github/workflows/deploy.yml"]
        self.append()
        lines = self.out.splitlines()
        self.assertEqual(len(lines), 2, self.out)
        self.assertTrue(lines[0].startswith("Appended 1 issues (1 jobs, 0 dispositions) to the run: "), lines[0])
        self.assertEqual(
            lines[1],
            "fix-b: changes files under .github/workflows; the Factory App cannot push them, "
            "so a person publishes this job's branch after it is built and checked",
        )
        self.assertNotIn("deploy.yml", self.out, "the job id only, never a path")
        self.assertEqual(self.err, "")

    def test_an_appended_job_without_workflow_paths_prints_only_the_appended_line(self):
        self.append()
        self.assertEqual(len(self.out.splitlines()), 1, self.out)
        self.assertTrue(self.out.startswith("Appended 1 issues"))

    def test_the_prompt_lists_the_jobs_and_a_dependency_on_an_unmerged_job_is_ingested(self):
        self.runner.state.update("fix-a", status="running")
        self.result["jobs"][0]["depends_on"] = ["fix-a"]
        self.append()
        context = self.prompts[0].split(append.CONTEXT)[1]
        self.assertEqual(
            json.loads(context),
            [{"id": "fix-a", "likely_paths": ["src"], "status": "running", "depends_on": []}],
        )
        self.assertEqual(len(self.trees), 1)
        with contextlib.redirect_stderr(io.StringIO()):
            self.runner.ingest_fragments()
        self.assertEqual(self.runner.jobs["fix-b"]["depends_on"], ["fix-a"])

    def test_a_waiting_fragment_counts_as_part_of_the_run(self):
        self.append()
        self.result = {
            **copy.deepcopy(RESULT_B),
            "jobs": [{**RESULT_B["jobs"][0], "id": "fix-c", "issues": [3], "depends_on": ["fix-b"]}],
            "dispositions": [{"issue": 3, "status": "job", "job": "fix-c", "reason": "ok"}],
        }
        self.append((2, 3))  # 2 is waiting in the inbox: dropped like an issue already in the run
        self.assertIn("factory: plan issue=2 dropped reason=in-run", self.err)
        self.assertEqual(self.fetched(), [2, 3])  # the first append's fetch of 2, then 3 only
        context = json.loads(self.prompts[1].split(append.CONTEXT)[1])
        self.assertEqual([(j["id"], j["status"]) for j in context], [("fix-a", "queued"), ("fix-b", "pending")])
        log = io.StringIO()
        with contextlib.redirect_stderr(log):
            self.runner.ingest_fragments()
        self.assertEqual([j["id"] for j in self.runner.plan["jobs"]], ["fix-a", "fix-b", "fix-c"], log.getvalue())

    def test_a_planner_result_the_supervisor_would_refuse_is_not_written(self):
        self.result["jobs"][0]["depends_on"] = ["nope"]
        with self.assertRaises(ValueError) as caught:
            self.append()
        self.assertEqual(str(caught.exception), "append refused: planner fragment invalid (unknown-dependency:nope)")
        self.assertEqual(self.fragments(), [])
        # The planner ran, so its cost is on the run.
        self.assertEqual(len(planner_usage_records(self.directory)), 1)

    def test_every_issue_must_be_a_job_or_a_disposition(self):
        self.result["dispositions"] = []
        self.result["jobs"] = []
        with self.assertRaises(ValueError):
            self.append()
        self.assertEqual(self.fragments(), [])

    def test_non_job_dispositions_are_written_and_a_run_of_only_dispositions_is_allowed(self):
        self.result = {
            "version": 1,
            "jobs": [],
            "dispositions": [disposed(2, "blocked"), disposed(3, "covered")],
        }
        self.append((2, 3))
        (path,) = self.fragments()
        document = json.loads(path.read_text())
        self.assertEqual(document["jobs"], [])
        self.assertEqual([d["status"] for d in document["dispositions"]], ["blocked", "covered"])
        self.assertIn("(0 jobs, 2 dispositions)", self.out)
        self.assertNotIn("because", self.out + self.err)

    def test_long_reasons_are_capped(self):
        self.result = {"version": 1, "jobs": [], "dispositions": [disposed(2, "blocked", "x" * 5000)]}
        self.append()
        (path,) = self.fragments()
        self.assertEqual(len(json.loads(path.read_text())["dispositions"][0]["reason"]), fragments.MAX_REASON)


class AppendRefusalTests(AppendCase):
    def test_unknown_run(self):
        missing = self.root / "nope"
        with self.assertRaises(ValueError) as caught:
            self.append(directory=missing)
        self.assertEqual(str(caught.exception), "append refused: run not found")
        self.assertFalse(missing.exists())
        self.assertEqual(self.prompts, [])

    def test_run_without_a_plan_is_unknown(self):
        (self.directory / "plan.json").unlink()
        self.assertRefused("run not found")

    def test_run_that_was_not_started(self):
        (self.directory / "state.json").unlink()
        with self.assertRaises(ValueError) as caught:
            self.append()
        self.assertEqual(str(caught.exception), "append refused: run not started")
        self.assertEqual((self.prompts, self.fragments(), self.fetched()), ([], [], []))

    def test_draining_run(self):
        common = self.root / "git"
        with RunControl(self.directory, common):
            request_drain(self.directory, common)
            self.assertRefused("run is draining")

    def test_a_stopped_run_with_an_old_drain_request_is_not_draining(self):
        common = self.root / "git"
        with RunControl(self.directory, common):
            request_drain(self.directory, common)
        self.append()
        self.assertEqual(len(self.fragments()), 1)

    def test_run_at_the_job_cap(self):
        with patch.object(fragments, "MAX_RUN_JOBS", 1):
            self.assertRefused("run is at the job cap")

    def test_waiting_fragments_count_against_the_cap(self):
        self.append()
        with patch.object(fragments, "MAX_RUN_JOBS", 2):
            self.assertRefusedKeeping("run is at the job cap")

    def assertRefusedKeeping(self, message):
        kept = self.fragments()
        with self.assertRaises(ValueError) as caught:
            self.append((3,))
        self.assertEqual(str(caught.exception), f"append refused: {message}")
        self.assertEqual(self.fragments(), kept)

    def test_issues_that_cannot_be_planned_are_dropped_with_fixed_lines(self):
        self.append((1, 4, 5, 6, 7, 2))
        self.assertEqual(
            sorted(self.err.splitlines()),
            sorted(
                f"factory: plan issue={n} dropped reason={r}"
                for n, r in ((1, "in-run"), (4, "closed"), (5, "ignored"), (6, "not-an-issue"), (7, "missing"))
            ),
        )
        # Only the issue that was planned had its text fetched.
        self.assertEqual(self.fetched(), [2])
        self.assertNotIn(SECRET, self.err + self.out)
        self.assertEqual(len(self.fragments()), 1)

    def test_when_nothing_can_be_planned_nothing_runs(self):
        for numbers in ((1,), (4, 5, 6)):
            with self.subTest(numbers=numbers):
                before = self.snapshot_files()
                with self.assertRaises(ValueError) as caught:
                    self.append(numbers)
                self.assertEqual(str(caught.exception), "none of the listed issues can be planned")
                self.assertEqual((self.prompts, self.fragments(), self.fetched()), ([], [], []))
                self.assertNotIn(SECRET, self.err)
                self.assertEqual(self.snapshot_files(), before)

    def test_options_that_do_not_fit_are_refused(self):
        for extra in ({"limit": 3}, {"use_snapshot": True}):
            with self.subTest(extra=extra):
                namespace = SimpleNamespace(
                    issues=[2], limit=None, use_snapshot=False, timeout=60, append_to="run1"
                )
                vars(namespace).update(extra)
                with self.assertRaises(ValueError):
                    cli.append_command(namespace, self.cfg, None, self.directory)
        namespace = SimpleNamespace(issues=None, limit=None, use_snapshot=False, timeout=60, append_to="run1")
        with self.assertRaises(ValueError):
            cli.append_command(namespace, self.cfg, None, self.directory)

    def test_append_to_must_name_the_run_directory(self):
        args = SimpleNamespace(run_dir=self.directory, append_to="other")
        with self.assertRaisesRegex(ValueError, "must name the run"):
            cli.resolve_run_dir(args, self.cfg)
        with self.assertRaisesRegex(ValueError, "run slug"):
            cli.resolve_run_dir(SimpleNamespace(run_dir=None, append_to="../x"), self.cfg)
        directory, _ = cli.resolve_run_dir(SimpleNamespace(run_dir=self.directory, append_to="run1"), self.cfg)
        self.assertEqual(directory, self.directory.resolve())

    def test_the_parser_accepts_append_to(self):
        args = cli.build_parser().parse_args(["--repo", "o/n", "plan", "--issues", "2", "--append-to", "run1"])
        self.assertEqual((args.append_to, args.issues), ("run1", [2]))
        self.assertIsNone(cli.build_parser().parse_args(["plan"]).append_to)


class AppendPodBudgetTests(AppendCase):
    """The appending planner is a worker session: it holds a pod budget slot while it runs."""

    def recording_hold(self, events):
        @contextlib.contextmanager
        def hold(pool, owner, wait_seconds):
            events.append(("take", owner.repo, owner.job, owner.phase, len(self.prompts)))
            try:
                yield
            finally:
                events.append(("release", len(self.prompts)))

        return hold

    def test_the_planner_runs_inside_one_slot_held_for_the_run(self):
        events = []
        with patch.object(cli.capacity, "hold", self.recording_hold(events)):
            self.append()
        # taken before the planner prompt exists, released only after the planner has answered
        self.assertEqual(events, [("take", "org/name", "run1", "plan", 0), ("release", 1)])

    def test_the_slot_is_released_when_the_planner_fails(self):
        events = []
        with patch.object(cli.capacity, "hold", self.recording_hold(events)):
            with patch.object(self, "fake_agent", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    self.append()
        self.assertEqual([event[0] for event in events], ["take", "release"])

    def test_an_append_that_is_refused_never_takes_a_slot(self):
        events = []
        with patch.object(cli.capacity, "hold", self.recording_hold(events)):
            with self.assertRaises(ValueError):
                self.append(directory=self.root / "nope")
        self.assertEqual(events, [])


class AppendBudgetTests(AppendCase):
    def test_total_cost_over_the_budget_refuses_before_the_planner(self):
        cfg = RepoConfig(repo="org/name", run_budget_usd=1.0)
        self.runner.state.update("fix-a", usage=[{"total_cost_usd": 0.4}])
        (self.directory / "planner-usage.json").write_text(json.dumps({"total_cost_usd": 0.3}))
        (self.directory / "planner-usage-append-old.json").write_text(json.dumps({"total_cost_usd": 0.3}))
        self.assertEqual(Runner(self.directory, self.directory, self.plan, cfg=cfg).run_cost(), 1.0)
        before = self.snapshot_files()
        with self.assertRaises(ValueError) as caught:
            self.append(cfg=cfg)
        self.assertEqual(str(caught.exception), "append refused: run budget reached")
        self.assertEqual((self.prompts, self.fragments(), self.fetched()), ([], [], []))
        self.assertEqual(self.snapshot_files(), before)

    def test_each_append_has_its_own_usage_file_and_run_cost_sums_them(self):
        cfg = RepoConfig(repo="org/name", run_budget_usd=1.0)
        original = json.dumps({"total_cost_usd": 0.5})
        (self.directory / "planner-usage.json").write_text(original)
        self.runner.state.update("fix-a", usage=[{"total_cost_usd": 0.1}])
        self.usage = {"total_cost_usd": 0.2}
        self.append(cfg=cfg)
        self.assertEqual((self.directory / "planner-usage.json").read_text(), original)
        extra = sorted(p.name for p in self.directory.glob("planner-usage-*.json"))
        self.assertEqual(len(extra), 1)
        self.assertEqual(json.loads((self.directory / extra[0]).read_text()), self.usage)
        self.assertAlmostEqual(Runner(self.directory, self.directory, self.plan, cfg=cfg).run_cost(), 0.8, places=6)
        # A second append (0.8 < 1.0) writes another file; now 1.0 has been reached.
        self.result = {"version": 1, "jobs": [], "dispositions": [disposed(3, "covered")]}
        self.usage = {"total_cost_usd": 0.2}
        self.append((3,), cfg=cfg)
        self.assertEqual(len(list(self.directory.glob("planner-usage-*.json"))), 2)
        self.assertEqual((self.directory / "planner-usage.json").read_text(), original)
        self.assertEqual(Runner(self.directory, self.directory, self.plan, cfg=cfg).run_cost(), 1.0)
        with self.assertRaises(ValueError):
            self.append((2,), cfg=cfg)

    def test_the_cli_cost_reader_sums_every_file(self):
        self.assertIsNone(cli.planner_usage(self.directory))
        (self.directory / "planner-usage.json").write_text(json.dumps({"total_cost_usd": 0.5, "x": 1}))
        self.assertEqual(cli.planner_usage(self.directory), {"total_cost_usd": 0.5, "x": 1})
        (self.directory / "planner-usage-append-a.json").write_text(json.dumps({"total_cost_usd": 0.25}))
        (self.directory / "planner-usage-append-b.json.tmp").write_text("{}")
        self.assertEqual(cli.planner_usage(self.directory)["total_cost_usd"], 0.75)

    def test_the_cli_reader_sums_the_token_counts_of_every_file_too(self):
        counts = {"input_tokens": 1, "output_tokens": 2, "cache_creation_input_tokens": 3, "cache_read_input_tokens": 4}
        (self.directory / "planner-usage.json").write_text(json.dumps({"total_cost_usd": 0.5, "phase": "plan", "usage": counts}))
        self.assertEqual(cli.planner_usage(self.directory)["usage"], counts)
        (self.directory / "planner-usage-append-a.json").write_text(json.dumps({"total_cost_usd": 0.25, "phase": "plan", "usage": counts}))
        merged = cli.planner_usage(self.directory)
        self.assertEqual(merged["total_cost_usd"], 0.75)
        self.assertEqual(merged["phase"], "plan")
        self.assertEqual(
            merged["usage"],
            {"input_tokens": 2, "output_tokens": 4, "cache_creation_input_tokens": 6, "cache_read_input_tokens": 8},
        )


class AppendLockTests(AppendCase):
    def test_run_never_takes_the_repository_lease(self):
        calls = []
        args = cli.build_parser().parse_args(
            ["--run-dir", str(self.directory), "plan", "--issues", "2", "--append-to", "run1"]
        )
        with (
            patch("factory.locks.lock_from_env", side_effect=AssertionError("lease")) as lease,
            patch.object(cli, "append_command", side_effect=lambda *a: calls.append(a)),
            patch.object(cli, "execute", side_effect=AssertionError("execute")),
            patch("factory.config.load_repo_config", return_value=self.cfg),
            patch("factory.config.data_paths", return_value=None),
            patch("factory.github_auth.from_environment", return_value=None),
            patch.object(cli, "set_auth"),
        ):
            cli.run(args)
        lease.assert_not_called()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][3], self.directory.resolve())

    def test_run_of_an_unknown_run_creates_nothing(self):
        missing = self.root / "ghost"
        args = cli.build_parser().parse_args(
            ["--run-dir", str(missing), "plan", "--issues", "2", "--append-to", "ghost"]
        )
        with (
            patch("factory.locks.lock_from_env", side_effect=AssertionError("lease")),
            patch("factory.config.load_repo_config", return_value=self.cfg),
            patch("factory.config.data_paths", return_value=None),
            patch("factory.github_auth.from_environment", return_value=None),
            patch.object(cli, "set_auth"),
            self.assertRaises(ValueError),
        ):
            cli.run(args)
        self.assertFalse(missing.exists())

    def test_two_appends_are_serialised_and_use_a_worktree_under_the_run(self):
        events, errors = [], []
        planner_results = {}

        def agent(prompt, cwd, prefix, phase, *args, **kwargs):
            number = json.loads(prompt.split("Selected issue index (untrusted task data):\n")[1].split("\n")[0])[0]["number"]
            events.append(("start", number))
            self.trees.append(Path(cwd))
            time.sleep(0.3)
            events.append(("end", number))
            return planner_results[number], {"total_cost_usd": 0.01}

        for number in (2, 3):
            planner_results[number] = {
                "version": 1,
                "jobs": [{**RESULT_B["jobs"][0], "id": f"fix-{number}", "issues": [number]}],
                "dispositions": [{"issue": number, "status": "job", "job": f"fix-{number}", "reason": "ok"}],
            }

        def go(number):
            try:
                namespace = SimpleNamespace(
                    issues=[number], limit=None, use_snapshot=False, timeout=60, append_to="run1"
                )
                cli.append_command(namespace, self.cfg, None, self.directory)
            except BaseException as error:  # noqa: BLE001
                errors.append(error)

        with (
            patch("factory.planner.run_agent", side_effect=agent),
            patch.object(cli, "planner_worktree", side_effect=self.worktree),
            patch.object(cli, "ensure_clone", return_value=self.work),
            patch.object(cli, "default_branch", return_value="main"),
            patch("factory.locks.lock_from_env", side_effect=AssertionError("lease")),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            threads = [threading.Thread(target=go, args=(n,)) for n in (2, 3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
        self.assertEqual(errors, [])
        self.assertEqual([kind for kind, _ in events], ["start", "end", "start", "end"])
        self.assertEqual(events[0][1], events[1][1])
        self.assertEqual(len(self.fragments()), 2)
        self.assertEqual(
            self.trees, [self.directory / "worktrees" / append.PLANNER_TREE] * 2
        )
        self.assertTrue(self.directory.resolve() in self.trees[0].resolve().parents)

    def test_the_planner_worktree_is_named_for_the_append(self):
        self.append()
        (call,) = self.worktree_calls
        self.assertEqual(call.args[3], "planner-append")


class DispositionFragmentTests(unittest.TestCase):
    """The supervisor side: fragments that carry dispositions."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.inbox = self.directory / "inbox"
        self.inbox.mkdir(mode=0o700)
        self.plan = assignment()
        self.original = copy.deepcopy(self.plan)
        (self.directory / "plan.json").write_text(json.dumps(self.plan))
        self.runner = Runner(self.directory, self.directory, self.plan)
        self.log = io.StringIO()

    def drop(self, name, document):
        temp = self.inbox / f".{name}.tmp"
        temp.write_bytes(json.dumps(document).encode())
        os.replace(temp, self.inbox / f"fragment-{name}.json")

    def tick(self):
        with contextlib.redirect_stderr(self.log):
            self.runner.ingest_fragments()

    def document(self, dispositions, jobs=(), issues=None):
        owned = [n for j in jobs for n in j["issues"]]
        numbers = owned + [d["issue"] for d in dispositions] if issues is None else issues
        return {
            "version": 1,
            "kind": "plan_fragment",
            "issues": [{"number": n, "title": f"Issue {n}", "body": "body"} for n in numbers],
            "jobs": list(jobs),
            "dispositions": dispositions,
        }

    def test_only_dispositions_are_applied_with_their_labels_and_no_job(self):
        wanted = []
        self.drop(
            "one",
            self.document([disposed(2, "blocked"), disposed(3, "covered"), disposed(4, "investigation")]),
        )
        with patch.object(self.runner.issue_labels, "sync", side_effect=lambda w: wanted.append(dict(w))):
            self.tick()
        self.assertEqual(
            self.log.getvalue().splitlines(),
            ["factory: fragment name=fragment-one.json decision=applied jobs=- reason=ok"],
        )
        self.assertEqual(wanted, [{2: LABEL_BLOCKED, 4: LABEL_BLOCKED}])
        saved = json.loads((self.directory / "plan.json").read_text())
        validate_plan(saved)
        self.assertEqual([j["id"] for j in saved["jobs"]], ["fix-a"])
        self.assertEqual(list(self.runner.state.data["jobs"]), ["fix-a"])
        self.assertEqual(
            [(d["issue"], d["status"]) for d in saved["dispositions"]],
            [(1, "job"), (2, "blocked"), (3, "covered"), (4, "investigation")],
        )
        self.assertEqual(saved["fragments"][0]["dispositions"], [2, 3, 4])
        self.assertEqual(sorted(p.name for p in self.inbox.iterdir()), ["fragment-one.json.applied"])
        # The pin moved with it, and a replay is only retired.
        self.assertEqual(
            (self.directory / "plan.sha256").read_text(), fragments.plan_digest(saved)
        )

    def test_jobs_and_dispositions_in_one_fragment(self):
        self.drop("one", self.document([disposed(3, "blocked")], [job("fix-b", 2)]))
        self.tick()
        self.assertEqual([j["id"] for j in self.runner.plan["jobs"]], ["fix-a", "fix-b"])
        self.assertEqual(
            [d["status"] for d in self.runner.plan["dispositions"]], ["job", "job", "blocked"]
        )

    def test_the_import_is_still_accepted_after_a_disposition_fragment(self):
        self.drop("one", self.document([disposed(2, "blocked")]))
        self.tick()
        live = json.loads((self.directory / "plan.json").read_text())
        self.assertEqual(cli.original_plan(live), self.original)

    def test_rejections_use_fixed_reasons_and_echo_nothing(self):
        text = "SECRET-REASON-TEXT"
        cases = {
            "jobstatus": (self.document([{**disposed(2, "job"), "reason": text}]), "schema:dispositions"),
            "overlap": (self.document([disposed(2, "blocked", text)], [job("fix-b", 2)], issues=[2]), "issue-ownership"),
            "long": (
                self.document([disposed(2, "blocked", text + "x" * fragments.MAX_REASON)]),
                "schema:reason-length",
            ),
            "inplan": (self.document([disposed(1, "blocked", text)]), "issue-collision"),
            "unlisted": (self.document([disposed(2, "blocked", text)], issues=[]) | {"issues": [
                {"number": 5, "title": "t", "body": "b"}]}, "issue-ownership"),
            "empty": ({**self.document([]), "issues": []}, "schema:issues"),
            "nothing": (self.document([], issues=[5]), "schema:jobs"),
            "extra": ({**self.document([disposed(2, "blocked")], ), "x": 1}, "schema:fields"),
            "noreason": (self.document([{"issue": 2, "status": "blocked"}]), "schema:dispositions"),
            "blank": (self.document([disposed(2, "blocked", "  ")]), "schema:reason"),
            "dup": (self.document([disposed(2, "blocked"), disposed(2, "covered")], issues=[2]), "issue-ownership"),
        }
        for name, (document, _) in cases.items():
            self.drop(name, document)
        self.tick()
        lines = self.log.getvalue().splitlines()
        for name, (_, reason) in cases.items():
            with self.subTest(name=name):
                self.assertIn(
                    f"factory: fragment name=fragment-{name}.json decision=quarantined jobs=- reason={reason}",
                    lines,
                )
        self.assertNotIn(text, self.log.getvalue())
        self.assertEqual([j["id"] for j in self.runner.plan["jobs"]], ["fix-a"])
        self.assertEqual(len(self.runner.plan["dispositions"]), 1)

    def test_a_fragment_without_dispositions_is_still_accepted(self):
        document = self.document([], [job("fix-b", 2)])
        del document["dispositions"]
        self.drop("one", document)
        self.tick()
        self.assertEqual([j["id"] for j in self.runner.plan["jobs"]], ["fix-a", "fix-b"])
        self.assertNotIn("dispositions", self.runner.plan["fragments"][0])


if __name__ == "__main__":
    unittest.main()


class AppendWiresThePodBackendTests(unittest.TestCase):
    """An append runs a planner session, so it needs the pod backend wired in like ``plan`` and ``run`` have it (found on production)."""

    def call(self, append_result=None, append_error=None):
        events = []

        def activate(repository, run_id, env=None):
            events.append(("activate", repository, run_id))
            return lambda: events.append(("undo",))

        def append_command(args, cfg, paths, directory):
            events.append(("append", directory.name))
            if append_error is not None:
                raise append_error
            return append_result

        cfg = SimpleNamespace(repo="Org/repo")
        with patch.object(cli, "append_command", append_command), patch("factory.podexecution.activate", activate):
            try:
                returned = cli.append_with_pods(SimpleNamespace(), cfg, None, Path("/data/runs/run1"))
            except RuntimeError as error:
                return events, error
        return events, returned

    def test_the_backend_is_activated_for_the_repository_and_run_before_the_append_and_undone_after(self):
        events, returned = self.call(append_result=None)
        self.assertEqual(events, [("activate", "Org/repo", "run1"), ("append", "run1"), ("undo",)])

    def test_the_backend_is_undone_when_the_append_fails(self):
        events, error = self.call(append_error=RuntimeError("append refused"))
        self.assertEqual(events, [("activate", "Org/repo", "run1"), ("append", "run1"), ("undo",)])
        self.assertEqual(str(error), "append refused")

    def test_run_sends_an_append_through_it(self):
        source = Path(cli.__file__).read_text(encoding="utf-8")
        self.assertIn("return append_with_pods(args, cfg, paths, directory)", source)
        self.assertNotIn("return append_command(args, cfg, paths, directory)\n    if args.command not in", source)

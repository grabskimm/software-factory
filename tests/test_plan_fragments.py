"""Plan fragments ingested from a run's inbox at the start of a scheduling tick.

No agents, GitHub or live runs: the scheduler runs on immediate pools and a fake ``execute``.
"""

import contextlib
import copy
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from factory import cli, fragments
from factory.config import RepoConfig
from factory.model import validate_plan
from factory.runner import Runner
from factory.state import atomic_text
from tests.test_factory import assignment
from tests.test_scheduling import Control, ImmediatePool


def job(key, issue, **fields):
    value = {
        "id": key,
        "issues": [issue],
        "objective": f"Do {key}",
        "likely_paths": [f"docs/{key}"],
        "acceptance": [f"{key} works"],
        "feature": False,
    }
    value.update(fields)
    return value


def fragment(*jobs, **fields):
    document = {
        "version": 1,
        "kind": "plan_fragment",
        "issues": [
            {"number": n, "title": f"Issue {n}", "body": "body"}
            for item in jobs
            for n in item["issues"]
        ],
        "jobs": list(jobs),
    }
    document.update(fields)
    return document


class FragmentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.inbox = self.directory / "inbox"
        self.inbox.mkdir(mode=0o700)
        self.plan = assignment()
        self.runner = self.make_runner()
        self.log = io.StringIO()

    def make_runner(self, plan=None, **options):
        plan = self.plan if plan is None else plan
        (self.directory / "plan.json").write_text(json.dumps(plan))
        return Runner(self.directory, self.directory, plan, **options)

    def drop(self, name, document, raw=None):
        """Write like a well behaved client: temp file, then rename."""
        data = raw if raw is not None else json.dumps(document).encode()
        temp = self.inbox / f".{name}.tmp"
        temp.write_bytes(data)
        os.replace(temp, self.inbox / f"fragment-{name}.json")
        return self.inbox / f"fragment-{name}.json"

    def tick(self, runner=None):
        with contextlib.redirect_stderr(self.log):
            (runner or self.runner).ingest_fragments()
        return self.log.getvalue().splitlines()

    def inbox_names(self):
        return sorted(p.name for p in self.inbox.iterdir())

    def run_pool(self, runner=None, execute=None):
        runner = runner or self.runner
        calls = []

        def finish(spec, phase):
            calls.append((spec["id"], phase))
            runner.state.update(spec["id"], status="merged")

        with (
            patch("factory.runner.ThreadPoolExecutor", ImmediatePool),
            patch("factory.recovery.validate_resume"),
            patch.object(runner, "execute", side_effect=execute or finish),
            contextlib.redirect_stderr(self.log),
        ):
            runner.run_pool(Control())
        return calls

    def assertUntouched(self, runner=None):
        runner = runner or self.runner
        self.assertEqual([j["id"] for j in runner.plan["jobs"]], ["fix-a"])
        self.assertEqual(list(runner.state.data["jobs"]), ["fix-a"])
        self.assertEqual(runner.plan["jobs"][0], assignment()["jobs"][0])

    def quarantined(self, name, reason):
        line = (
            f"factory: fragment name=fragment-{name}.json decision=quarantined jobs=- "
            f"reason={reason}"
        )
        self.assertIn(line, self.log.getvalue().splitlines())
        self.assertIn(f"fragment-{name}.json.rejected", self.inbox_names())
        self.assertNotIn(f"fragment-{name}.json", self.inbox_names())

    # -- acceptance -------------------------------------------------------------

    def test_tagged_and_plain_criteria_are_accepted_like_validate_plan(self):
        acceptance = [
            "plain",
            {"text": "untagged"},
            {"text": "elsewhere", "tag": "ci", "checks": ["build"]},
            {"text": "by hand", "tag": "operator"},
        ]
        issues, jobs, _ = fragments.parse(
            json.dumps(fragment(job("fix-b", 2, acceptance=acceptance))).encode(), 10
        )
        self.assertEqual(jobs[0]["acceptance"], acceptance)
        plan = assignment()
        plan["jobs"][0]["acceptance"] = acceptance
        validate_plan(plan)

    def test_a_bad_reason_is_cut_or_dropped_and_never_rejects_the_fragment(self):
        acceptance = [
            {"text": "by hand", "tag": "operator", "reason": "r" * 5000},
            {"text": "blank", "tag": "operator", "reason": "\u200b"},
            {"text": "elsewhere", "tag": "sandbox", "reason": "misplaced"},
        ]
        _, jobs, _ = fragments.parse(
            json.dumps(fragment(job("fix-b", 2, acceptance=acceptance))).encode(), 10
        )
        self.assertEqual(len(jobs[0]["acceptance"]), 3)
        # a reason that is not text is still a shape error
        bad = [{"text": "by hand", "tag": "operator", "reason": 7}]
        with self.assertRaises(fragments.FragmentError):
            fragments.parse(json.dumps(fragment(job("fix-b", 2, acceptance=bad))).encode(), 10)

    def test_valid_fragment_is_picked_up_and_scheduled_under_dependencies(self):
        self.drop(
            "one",
            fragment(job("fix-b", 2, depends_on=["fix-a"]), job("fix-c", 3)),
        )
        calls = self.run_pool()
        self.assertEqual(
            self.log.getvalue().splitlines(),
            [
                "factory: fragment name=fragment-one.json decision=applied "
                "jobs=fix-b,fix-c reason=ok"
            ],
        )
        self.assertEqual(sorted(calls), [("fix-a", "build"), ("fix-b", "build"), ("fix-c", "build")])
        self.assertLess(calls.index(("fix-a", "build")), calls.index(("fix-b", "build")))
        self.assertEqual(self.inbox_names(), ["fragment-one.json.applied"])
        on_disk = json.loads((self.directory / "plan.json").read_text())
        self.assertEqual([j["id"] for j in on_disk["jobs"]], ["fix-a", "fix-b", "fix-c"])
        validate_plan(on_disk)
        self.assertEqual(on_disk["jobs"][1]["fragment"], "fragment-one.json")
        self.assertEqual(
            {i["number"] for i in on_disk["issues"]}, {1, 2, 3}
        )
        state = json.loads((self.directory / "state.json").read_text())
        self.assertEqual(set(state["jobs"]), {"fix-a", "fix-b", "fix-c"})
        self.assertEqual(self.runner.issue_labels.owned["fix-b"], [2])

    def test_added_jobs_obey_shared_paths_and_builder_overlap(self):
        self.drop("one", fragment(job("fix-b", 2, likely_paths=["src/deep"])))
        self.runner.state.update("fix-a", status="running")
        self.tick()
        self.runner.state.update("fix-b", status="queued")
        self.assertEqual(
            self.runner.schedule_blocker("fix-b", "build", {}), "shared paths: fix-a"
        )

    def test_quarantine_cases_leave_existing_jobs_untouched(self):
        cases = {
            "reuse": (fragment(job("fix-a", 2)), "id-collision:fix-a"),
            "duplicate": (fragment(job("fix-b", 2), job("fix-b", 3)), "id-collision:fix-b"),
            "cycle": (
                fragment(
                    job("fix-b", 2, depends_on=["fix-c"]),
                    job("fix-c", 3, depends_on=["fix-b"]),
                ),
                "cycle:fix-b",
            ),
            "self": (fragment(job("fix-b", 2, depends_on=["fix-b"])), "cycle:fix-b"),
            "dangling": (
                fragment(job("fix-b", 2, depends_on=["nope"])),
                "unknown-dependency:nope",
            ),
            "issue": (fragment(job("fix-b", 1)), "issue-collision"),
            "charset": (fragment(job("Fix_B", 2)), "schema:job-id"),
            "extra": (fragment(job("fix-b", 2), nothing=1), "schema:fields"),
            "mutate": (fragment(job("fix-b", 2, status="merged")), "schema:job-fields"),
            "feature": (fragment(job("fix-b", 2, feature="no")), "schema:feature"),
            "paths": (fragment(job("fix-b", 2, likely_paths=["../x"])), "invalid-plan"),
            "tag": (
                fragment(job("fix-b", 2, acceptance=[{"text": "SECRET", "tag": "prod"}])),
                "schema:acceptance",
            ),
            "ci-bare": (
                fragment(job("fix-b", 2, acceptance=[{"text": "SECRET", "tag": "ci"}])),
                "schema:acceptance",
            ),
            "empty-text": (
                fragment(job("fix-b", 2, acceptance=[{"text": " ", "tag": "operator"}])),
                "schema:acceptance",
            ),
            "empty-list": (fragment(job("fix-b", 2, acceptance=[])), "schema:acceptance"),
            "blank": (fragment(job("fix-b", 2, acceptance=[" "])), "schema:acceptance"),
            "orphan": (
                {**fragment(job("fix-b", 2)), "issues": [
                    {"number": 2, "title": "t", "body": "b"},
                    {"number": 9, "title": "t", "body": "b"},
                ]},
                "issue-ownership",
            ),
        }
        for name, (document, _) in cases.items():
            self.drop(name, document)
        self.drop("garbage", None, raw=b'{"version": 1, "jobs"')
        self.tick()
        for name, (_, reason) in cases.items():
            with self.subTest(name=name):
                self.quarantined(name, reason)
        self.quarantined("garbage", "invalid-json")
        self.assertUntouched()
        self.assertEqual(
            json.loads((self.directory / "plan.json").read_text())["jobs"],
            assignment()["jobs"],
        )

    def test_oversize_fragment_is_quarantined(self):
        document = fragment(job("fix-b", 2, objective="x" * fragments.MAX_FRAGMENT_BYTES))
        self.drop("big", document)
        self.tick()
        self.quarantined("big", "size")
        self.assertUntouched()

    def test_symlink_is_quarantined_and_its_target_is_not_read(self):
        target = self.directory / "outside.json"
        target.write_text(json.dumps(fragment(job("fix-b", 2))))
        (self.inbox / "fragment-link.json").symlink_to(target)
        self.tick()
        self.quarantined("link", "symlink")
        self.assertTrue(target.exists())
        self.assertUntouched()

    def test_symlinked_inbox_is_not_followed(self):
        real = self.directory / "elsewhere"
        real.mkdir()
        (real / "fragment-x.json").write_text(json.dumps(fragment(job("fix-b", 2))))
        os.rmdir(self.inbox)
        self.inbox.symlink_to(real)
        self.tick()
        self.assertUntouched()
        self.assertTrue((real / "fragment-x.json").exists())

    def test_file_of_the_wrong_owner_is_quarantined(self):
        self.drop("alien", fragment(job("fix-b", 2)))
        with patch("factory.fragments.os.geteuid", return_value=os.geteuid() + 1):
            self.tick()
        self.quarantined("alien", "owner")
        self.assertUntouched()

    def test_special_file_is_quarantined_without_blocking(self):
        os.mkfifo(self.inbox / "fragment-pipe.json")
        self.tick()
        self.quarantined("pipe", "not-regular")

    def test_temp_files_and_other_inbox_documents_are_not_fragments(self):
        body = json.dumps(fragment(job("fix-b", 2))).encode()
        (self.inbox / ".fragment-half.json.tmp").write_bytes(body[:20])
        (self.inbox / "fragment-half.json.tmp").write_bytes(body)
        (self.inbox / "fragment-half.tmp").write_bytes(body)
        evidence = json.dumps({"job_id": "fix-a", "jobs": [job("fix-b", 2)]}).encode()
        digest = hashlib.sha256(evidence).hexdigest()
        (self.inbox / f"{digest}.json").write_bytes(evidence)
        before = self.inbox_names()
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.inbox_names(), before)
        self.assertUntouched()

    def test_manager_write_inbox_files_are_left_alone(self):
        from factory.manager import write_inbox

        path = write_inbox(self.directory, json.dumps(fragment(job("fix-b", 2))).encode())
        self.assertEqual(self.tick(), [])
        self.assertTrue(path.exists())
        self.assertUntouched()

    def test_two_fragments_apply_in_name_order_and_only_the_conflicting_one_is_refused(self):
        self.drop("b-late", fragment(job("fix-b", 3), job("fix-d", 4)))
        self.drop("a-early", fragment(job("fix-b", 2)))
        self.drop("c-last", fragment(job("fix-e", 5, depends_on=["fix-b"])))
        lines = self.tick()
        self.assertEqual(
            lines,
            [
                "factory: fragment name=fragment-a-early.json decision=applied jobs=fix-b reason=ok",
                "factory: fragment name=fragment-b-late.json decision=quarantined jobs=- reason=id-collision:fix-b",
                "factory: fragment name=fragment-c-last.json decision=applied jobs=fix-e reason=ok",
            ],
        )
        self.assertEqual(list(self.runner.state.data["jobs"]), ["fix-a", "fix-b", "fix-e"])
        self.assertEqual(
            self.inbox_names(),
            [
                "fragment-a-early.json.applied",
                "fragment-b-late.json.rejected",
                "fragment-c-last.json.applied",
            ],
        )

    def test_restart_between_drop_and_ingestion_ingests_once(self):
        self.drop("one", fragment(job("fix-b", 2)))
        restarted = self.make_runner(copy.deepcopy(assignment()))
        self.assertEqual(self.inbox_names(), ["fragment-one.json"])
        self.tick(restarted)
        self.assertEqual(list(restarted.state.data["jobs"]), ["fix-a", "fix-b"])
        again = Runner(
            self.directory,
            self.directory,
            json.loads((self.directory / "plan.json").read_text()),
        )
        self.tick(again)
        self.assertEqual(list(again.state.data["jobs"]), ["fix-a", "fix-b"])
        self.assertEqual(len(again.plan["jobs"]), 2)
        self.assertEqual(len(again.plan["fragments"]), 1)

    def load(self, command="recover"):
        return cli.load_plan(SimpleNamespace(command=command, plan_file=None), self.directory)

    def test_failed_state_write_is_retried_in_the_same_process(self):
        self.drop("one", fragment(job("fix-b", 2)))
        with patch.object(self.runner.state, "add_jobs", side_effect=OSError("disk")):
            self.tick()
        self.assertIn("decision=deferred jobs=fix-b reason=commit-failed", self.log.getvalue())
        self.assertEqual(self.inbox_names(), ["fragment-one.json"])
        self.assertEqual([j["id"] for j in self.runner.plan["jobs"]], ["fix-a"])
        self.assertNotIn("fix-b", self.runner.jobs)
        self.assertEqual(self.run_pool(), [("fix-a", "build"), ("fix-b", "build")])
        self.assertEqual(self.runner.state.data["jobs"]["fix-b"]["status"], "merged")
        self.assertEqual(self.inbox_names(), ["fragment-one.json.applied"])

    def test_crash_after_plan_before_state_is_repaired_on_restart(self):
        self.load()
        self.drop("one", fragment(job("fix-b", 2)))
        with patch.object(self.runner.state, "add_jobs", side_effect=OSError("disk")):
            self.tick()
        self.assertIn("decision=deferred jobs=fix-b reason=commit-failed", self.log.getvalue())
        self.assertEqual(self.inbox_names(), ["fragment-one.json"])
        plan = self.load()  # a real restart goes through the digest pin
        self.assertEqual([j["id"] for j in plan["jobs"]], ["fix-a", "fix-b"])
        restarted = Runner(self.directory, self.directory, plan)
        self.assertEqual(restarted.state.data["jobs"]["fix-b"]["status"], "queued")
        self.log.truncate(0)
        self.log.seek(0)
        self.tick(restarted)
        self.assertEqual(
            self.log.getvalue().splitlines(),
            [
                "factory: fragment name=fragment-one.json decision=already-applied "
                "jobs=fix-b reason=replay"
            ],
        )
        self.assertEqual(self.inbox_names(), ["fragment-one.json.applied"])
        self.assertEqual(len(restarted.plan["jobs"]), 2)

    def test_ingested_plan_passes_the_digest_pin_on_restart_commands(self):
        self.load()
        self.drop("one", fragment(job("fix-b", 2)))
        self.tick()
        for command in ("run", "recover", "resume", "retry", "block"):
            plan = self.load(command)
            self.assertEqual([j["id"] for j in plan["jobs"]], ["fix-a", "fix-b"])
        restarted = Runner(self.directory, self.directory, plan)
        self.assertEqual(list(restarted.state.data["jobs"]), ["fix-a", "fix-b"])

    def test_crash_between_plan_write_and_pin_write_passes_the_pin(self):
        self.load()
        self.drop("one", fragment(job("fix-b", 2)))
        real = atomic_text

        def crash_on_pin(path, text):
            if Path(path).name == "plan.sha256":
                raise OSError("crash")
            real(path, text)

        with patch("factory.runner.atomic_text", side_effect=crash_on_pin):
            self.tick()
        self.assertEqual(self.inbox_names(), ["fragment-one.json"])
        self.assertEqual([j["id"] for j in self.load()["jobs"]], ["fix-a", "fix-b"])

    def test_outside_edit_of_the_plan_is_still_refused_after_ingestion(self):
        self.load()
        self.drop("one", fragment(job("fix-b", 2)))
        self.tick()
        path = self.directory / "plan.json"
        edited = json.loads(path.read_text())
        edited["jobs"][1]["objective"] = "Something else"
        path.write_text(json.dumps(edited))
        with self.assertRaisesRegex(ValueError, "plan changed after execution began"):
            self.load()

    def test_depends_on_outside_the_id_charset_is_quarantined_in_one_line(self):
        self.drop(
            "one",
            fragment(job("fix-b", 2, depends_on=["x\nfactory: fragment name=forged"])),
        )
        self.assertEqual(
            self.tick(),
            [
                "factory: fragment name=fragment-one.json decision=quarantined "
                "jobs=- reason=schema:depends_on"
            ],
        )
        self.assertEqual(self.inbox_names(), ["fragment-one.json.rejected"])

    def test_crash_after_commit_before_rename_does_not_apply_twice(self):
        path = self.drop("one", fragment(job("fix-b", 2)))
        saved = path.read_bytes()
        self.tick()
        path.write_bytes(saved)  # the rename never happened
        self.tick()
        self.assertIn("decision=already-applied jobs=fix-b reason=replay", self.log.getvalue())
        self.assertEqual(len(self.runner.plan["jobs"]), 2)
        self.assertEqual(
            self.inbox_names(),
            ["fragment-one.json.applied", "fragment-one.json.applied.1"],
        )

    def test_job_cap_refuses_the_whole_fragment(self):
        self.runner = self.make_runner(max_jobs=3)
        self.drop("a", fragment(job("fix-b", 2), job("fix-c", 3), job("fix-d", 4)))
        self.drop("b", fragment(job("fix-e", 5), job("fix-f", 6)))
        self.drop("c", fragment(job("fix-g", 7)))
        self.tick()
        self.quarantined("a", "job-cap:4>3")
        self.assertIn("name=fragment-b.json decision=applied jobs=fix-e,fix-f", self.log.getvalue())
        self.quarantined("c", "job-cap:4>3")
        self.assertEqual(len(self.runner.state.data["jobs"]), 3)

    def test_default_job_cap_is_ten(self):
        self.assertEqual(self.runner.max_jobs, 10)
        self.drop("many", fragment(*(job(f"j{i}", i + 10) for i in range(10))))
        self.tick()
        self.quarantined("many", "job-cap:11>10")
        self.drop("nine", fragment(*(job(f"k{i}", i + 30) for i in range(9))))
        self.tick()
        self.assertEqual(len(self.runner.plan["jobs"]), 10)

    def test_added_jobs_are_not_started_when_the_run_is_over_budget(self):
        self.runner = self.make_runner(cfg=RepoConfig(repo="", run_budget_usd=1.0))
        self.runner.state.update("fix-a", usage=[{"total_cost_usd": 0.4}])
        self.drop("one", fragment(job("fix-b", 2)))
        self.assertEqual(self.run_pool(), [("fix-a", "build"), ("fix-b", "build")])

        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.directory))
        self.inbox = self.directory / "inbox"
        self.inbox.mkdir()
        self.runner = self.make_runner(
            copy.deepcopy(assignment()), cfg=RepoConfig(repo="", run_budget_usd=1.0)
        )
        self.runner.state.update(
            "fix-a", status="merged", usage=[{"total_cost_usd": 0.6}]
        )
        self.drop("one", fragment(job("fix-b", 2)))
        (self.directory / "planner-usage.json").write_text(json.dumps({"total_cost_usd": 0.5}))
        self.assertEqual(self.run_pool(), [])
        self.assertIn("decision=applied jobs=fix-b", self.log.getvalue())
        self.assertEqual(self.runner.state.data["jobs"]["fix-b"]["status"], "queued")
        self.assertEqual(
            self.runner.schedule_blocker("fix-b", "build", {}),
            "budget: run_budget_usd reached",
        )

    def test_without_a_budget_added_jobs_start(self):
        self.runner.state.update("fix-a", usage=[{"total_cost_usd": 1000}])
        self.drop("one", fragment(job("fix-b", 2)))
        self.assertEqual(self.run_pool(), [("fix-a", "build"), ("fix-b", "build")])

    def test_draining_run_ingests_nothing(self):
        self.drop("one", fragment(job("fix-b", 2)))

        class Draining(Control):
            def draining(self):
                return True

        with (
            patch("factory.runner.ThreadPoolExecutor", ImmediatePool),
            contextlib.redirect_stderr(self.log),
        ):
            self.runner.run_pool(Draining())
        self.assertEqual(self.inbox_names(), ["fragment-one.json"])
        self.assertUntouched()

    def test_lost_lease_stops_ingestion(self):
        class Lost(Exception):
            pass

        def fence():
            raise Lost

        self.drop("one", fragment(job("fix-b", 2)))
        with patch.object(self.runner, "assert_held", side_effect=fence):
            with self.assertRaises(Lost):
                self.tick()
        self.assertEqual(self.inbox_names(), ["fragment-one.json"])
        self.assertUntouched()

    def test_missing_inbox_is_fine(self):
        os.rmdir(self.inbox)
        self.assertEqual(self.tick(), [])

    def test_failed_state_save_leaves_the_live_state_as_it_was(self):
        # state.json cannot be written (disk full): the new job must not stay in the live
        # state, where the scheduler would index it in a plan that does not know it yet.
        self.drop("one", fragment(job("fix-b", 2)))
        with patch.object(self.runner.state, "save", side_effect=OSError("disk full")):
            lines = self.tick()
        self.assertIn("decision=deferred", lines[-1])
        self.assertIn("reason=commit-failed", lines[-1])
        self.assertEqual(set(self.runner.state.data["jobs"]), {"fix-a"})
        self.assertEqual(set(self.runner.jobs), {"fix-a"})
        self.assertEqual(self.inbox_names(), ["fragment-one.json"])

    def test_idle_tick_after_a_failed_state_save_does_not_crash_and_the_next_tick_retries(self):
        # Nothing else is running, so the pool ends; before the fix it died with a KeyError
        # on the job the state held but the plan in memory did not.
        self.runner.state.update("fix-a", status="merged")
        self.drop("one", fragment(job("fix-b", 2)))
        with patch.object(self.runner.state, "save", side_effect=OSError("disk full")):
            self.assertEqual(self.run_pool(), [])
        self.assertEqual(self.inbox_names(), ["fragment-one.json"])
        self.assertUntouched()
        ran = self.run_pool()  # the disk recovered: the same file is ingested and run
        self.assertIn(("fix-b", "build"), ran)
        self.assertEqual(self.runner.state.data["jobs"]["fix-b"]["status"], "merged")

    def test_prompt_reads_the_job_keys_under_the_state_mutex(self):
        seen = []
        real = self.runner.state.mutex

        class Probe:
            def __enter__(self):
                seen.append("enter")
                return real.__enter__()

            def __exit__(self, *exc):
                return real.__exit__(*exc)

        with patch.object(self.runner.state, "mutex", Probe()):
            self.runner.prompt("builder", self.runner.jobs["fix-a"])
        self.assertIn("enter", seen)


if __name__ == "__main__":
    unittest.main()

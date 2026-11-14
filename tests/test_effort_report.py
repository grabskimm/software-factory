"""tools/effort_report.py: the read-only report of tokens, cost, turns and outcome per recorded effort (issue 132).

Every fixture is hand made: ``plan.json`` and ``state.json`` of run directories in the layout of the data disk
(``<data>/repos/<org>/<repo>/runs/<run>``) and JSON lines of the history ledger. Every free-text position of the fixtures (plan
titles, job errors, usage subtypes, ledger extras, run directory names) carries a canary that must never reach the output.
"""

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from factory import dashboard, history, status

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("effort_report_under_test", ROOT / "tools" / "effort_report.py")
er = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(er)

CANARY = "CANARY-FREE-TEXT-do-not-print"
SONNET, OPUS, HAIKU = "claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"
COUNTS = {"input_tokens": 10, "output_tokens": 50, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000}


def session(phase="build", model=SONNET, cost=1.0, turns=5, counts=None, **extra):
    """One ``state.json`` ``usage[]`` record, as ``agent.usage_record`` writes it."""
    record = {
        "phase": phase, "model": model, "usage": dict(COUNTS if counts is None else counts),
        "model_usage": {SONNET: {"costUSD": cost}, CANARY: {"costUSD": 0}}, "total_cost_usd": cost, "num_turns": turns,
        "duration_ms": 1000, "subtype": CANARY,
    }
    record.update(extra)
    return record


def planned(job_id, issues, effort=None, source="issue", model=SONNET, verify_effort=None):
    """One ``plan.json`` job; ``effort`` None is a plan from before the choice was recorded."""
    job = {"id": job_id, "issues": issues, "title": CANARY, "summary": CANARY}
    if effort is not None:
        entry = {"model": model, "effort": effort, "model_source": "built-in", "effort_source": source}
        job["choice"] = {"build": dict(entry), "verify": {**entry, "effort": verify_effort or effort}}
    return job


def state_job(status_word, usage=(), **extra):
    return {"status": status_word, "attempt": 1, "error": CANARY, "blockers": [CANARY], "usage": list(usage), **extra}


def entry(kind, run, **fields):
    return {"v": 1, "kind": kind, "at": "2026-11-08T10:00:00Z", "repo": "org/name", "run": run, **fields}


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"

    def run_dir(self, run="run-a", org="org", name="name"):
        return self.data / "repos" / org / name / "runs" / run

    def write_run(self, jobs, states, run="run-a", directory=None):
        directory = directory or self.run_dir(run)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "plan.json").write_text(json.dumps({"version": 1, "title": CANARY, "jobs": jobs}))
        (directory / "state.json").write_text(json.dumps({"version": 1, "jobs": states}))
        return directory

    def write_ledger(self, lines, name="events-202611-001.jsonl", raw=b""):
        path = self.data / "history" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as handle:
            for line in lines:
                handle.write(f"{json.dumps(line)}\n".encode())
            handle.write(raw)
        return path

    def report(self, **kwargs):
        return er.aggregate([str(self.data)], **kwargs)


class Experiment(Base):
    """Nine jobs: seven in the run directory ``run-a``, two only in the ledger (``run-b`` was purged), two of them in both."""

    def setUp(self):
        super().setUp()
        review = session("review", OPUS, cost=3.0, turns=7)
        self.write_run(
            [
                planned("hi1", [1], "high"), planned("hi2", [2], "high"), planned("lo1", [3], "low", model=HAIKU),
                planned("cl1", [4], "medium"), planned("rep1", [5], "medium", source="repository"), planned("old1", [6]),
                planned("nodata", [7], "high"),
            ],
            {
                "hi1": state_job("pr_ready", [session(), review]),
                "hi2": state_job("blocked", [session(cost=2.0, turns=9)]),
                "lo1": state_job("merged", [session(model=HAIKU, cost=0.5, turns=3, counts={
                    "input_tokens": 5, "output_tokens": 20, "cache_creation_input_tokens": 50, "cache_read_input_tokens": 500})]),
                "cl1": state_job("closed", [session(cost=1.0, turns=4)]),
                "rep1": state_job("stopped", [session(cost=0.5, turns=2)]),
                "old1": state_job("queued", [session(cost=0.25, turns=1)]),
                "nodata": state_job("running"),
            },
        )
        per_phase = {"build": dict(COUNTS), "review": dict(COUNTS)}
        self.write_ledger([
            # hi1 is also in the ledger: the same numbers, and GitHub's word that the pull request was merged
            entry("planned", "run-a", jobs=[{"id": "hi1", "issues": [1]}, {"id": "lo1", "issues": [3]}], note=CANARY),
            entry("job", "run-a", job="hi1", status="pr_ready", phase="review", attempt=1, cost_usd=4.0, tokens=per_phase, issues=[1]),
            entry("pr_observed", "run-a", job="hi1", handoff_status="merged", fresh=True, pr=f"https://github.com/org/name/pull/9?{CANARY}"),
            # lo1 disagrees with its run directory in tokens and in cost
            entry("job", "run-a", job="lo1", status="merged", phase="verify", attempt=1, cost_usd=0.7,
                  tokens={"build": {**COUNTS, "input_tokens": 6}}, issues=[3]),
            # run-b is gone: gone1 merged, gone2 was never measured and was abandoned by the purge
            entry("planned", "run-b", jobs=[{"id": "gone1", "issues": [8]}, {"id": "gone2", "issues": [9]}]),
            entry("job", "run-b", job="gone1", status="pr_ready", phase="verify", attempt=1, cost_usd=4.0,
                  tokens={"build": {"input_tokens": 100, "output_tokens": 500, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 10000}}),
            entry("pr_observed", "run-b", job="gone1", handoff_status="merged", fresh=True),
            entry("job", "run-b", job="gone2", status="running", phase="build", attempt=1),
            entry("purged", "run-b", reason="abandoned", jobs=[{"id": "gone1", "status": "pr_ready"}, {"id": "gone2", "status": "running"}]),
            entry("operator", "run-a", action="drain", principal=CANARY),
        ])

    def row(self, rows, **keys):
        found = [r for r in rows if all(r[k] == v for k, v in keys.items())]
        self.assertEqual(len(found), 1, f"{keys}: {[{k: r[k] for k in keys} for r in rows]}")
        return found[0]


class TableTests(Experiment):
    def test_there_is_one_row_per_recorded_effort_in_the_order_of_the_scale_and_unrecorded_last(self):
        report = self.report()
        self.assertEqual([r["effort"] for r in report["by_effort"]], ["low", "medium", "high", "unrecorded"])
        self.assertEqual([r["jobs"] for r in report["by_effort"]], [1, 2, 3, 3])
        self.assertEqual(report["jobs"], {"found": 9, "selected": 9, "in_run_directory_only": 5, "in_ledger_only": 2, "in_both": 2, "over_limit": 0})

    def test_a_row_has_the_job_and_session_counts_and_the_closed_outcome_vocabulary(self):
        high = self.row(self.report()["by_effort"], effort="high")
        self.assertEqual((high["jobs"], high["jobs_with_usage"], high["sessions"], high["sessions_with_usage"]), (3, 2, 3, 3))
        # hi1 is pr_ready in state.json and merged in the ledger (GitHub's word wins), hi2 blocked, nodata still running
        self.assertEqual(high["outcomes"], {"merged": 1, "closed": 0, "blocked": 1, "abandoned": 0, "stopped": 0, "other": 1})
        self.assertEqual(tuple(high["outcomes"]), (*history.OUTCOMES, "other"))
        medium = self.row(self.report()["by_effort"], effort="medium")
        self.assertEqual(medium["outcomes"], {"merged": 0, "closed": 1, "blocked": 0, "abandoned": 0, "stopped": 1, "other": 0})
        unrecorded = self.row(self.report()["by_effort"], effort="unrecorded")
        self.assertEqual(unrecorded["outcomes"], {"merged": 1, "closed": 0, "blocked": 0, "abandoned": 1, "stopped": 0, "other": 1})

    def test_every_mean_has_its_n_median_and_total(self):
        high = self.row(self.report()["by_effort"], effort="high")["metrics"]
        # hi1 (build + review) and hi2; the job without usage data is a job and is in no mean
        self.assertEqual(high["cost_usd"], {"n": 2, "mean": 3.0, "median": 3.0, "total": 6.0})
        self.assertEqual(high["turns"], {"n": 2, "mean": 10.5, "median": 10.5, "total": 21})
        self.assertEqual(high["input"], {"n": 2, "mean": 15.0, "median": 15.0, "total": 30})
        self.assertEqual(high["output"]["total"], 150)
        self.assertEqual(high["cache_write"], {"n": 2, "mean": 150.0, "median": 150.0, "total": 300})
        self.assertEqual(high["cache_read"], {"n": 2, "mean": 1500.0, "median": 1500.0, "total": 3000})
        self.assertEqual(tuple(high), er.METRICS)

    def test_the_median_is_not_the_mean(self):
        self.write_run(
            [planned(f"j{n}", [n], "xhigh") for n in (21, 22, 23)],
            {f"j{n}": state_job("merged", [session(cost=cost, turns=turns)]) for n, cost, turns in ((21, 1.0, 1), (22, 2.0, 2), (23, 9.0, 12))},
            run="run-c",
        )
        row = self.row(self.report()["by_effort"], effort="xhigh")["metrics"]
        self.assertEqual(row["cost_usd"], {"n": 3, "mean": 4.0, "median": 2.0, "total": 12.0})
        self.assertEqual(row["turns"], {"n": 3, "mean": 5.0, "median": 2.0, "total": 15})

    def test_the_cache_hit_rate_is_the_statuses_arithmetic_over_the_summed_counts(self):
        high = self.row(self.report()["by_effort"], effort="high")
        sums = {"input_tokens": 30, "cache_creation_input_tokens": 300, "cache_read_input_tokens": 3000}
        self.assertEqual(high["cache_hit_rate"], status.cache_hit_rate(sums))
        self.assertAlmostEqual(high["cache_hit_rate"], 3000 / 3330, places=6)

    def test_the_model_split_uses_the_recorded_choice_then_the_first_build_session(self):
        rows = self.report()["by_effort_and_model"]
        self.assertEqual([(r["effort"], r["model"], r["jobs"]) for r in rows], [
            ("low", HAIKU, 1), ("medium", SONNET, 2), ("high", SONNET, 3), ("unrecorded", SONNET, 1), ("unrecorded", "unrecorded", 2),
        ])

    def test_sessions_are_split_by_phase_effort_and_model_and_a_phase_without_a_recorded_effort_says_so(self):
        rows = self.report()["sessions"]
        self.assertEqual([(r["phase"], r["effort"], r["model"], r["sessions"]) for r in rows], [
            ("build", "low", HAIKU, 1), ("build", "medium", SONNET, 2), ("build", "high", SONNET, 2),
            ("build", "unrecorded", SONNET, 1), ("review", "unrecorded", OPUS, 1),
        ])
        review = self.row(rows, phase="review")
        self.assertEqual(review["metrics"]["cost_usd"], {"n": 1, "mean": 3.0, "median": 3.0, "total": 3.0})
        self.assertEqual(review["metrics"]["turns"]["total"], 7)

    def test_the_effort_of_a_job_is_its_build_phases_and_each_session_has_its_own_phases_effort(self):
        self.write_run(
            [planned("bv1", [41], "low", verify_effort="max")],
            {"bv1": state_job("merged", [session("build"), session("verify", OPUS)])}, run="run-g",
        )
        report = self.report()
        self.assertEqual(self.row(report["by_effort"], effort="low")["jobs"], 2)  # lo1 and bv1: the job is counted at its build effort
        self.assertNotIn("max", [r["effort"] for r in report["by_effort"]])
        self.assertEqual(self.row(report["sessions"], phase="verify", effort="max", model=OPUS)["sessions"], 1)

    def test_a_job_without_a_recorded_effort_is_its_own_row_not_a_default(self):
        unrecorded = self.row(self.report()["by_effort"], effort="unrecorded")
        self.assertEqual(unrecorded["jobs"], 3)  # the plan without a choice and the two purged jobs
        self.assertNotIn("unrecorded", [r["effort"] for r in self.report()["by_effort"] if r["effort"] in ("low", "medium", "high")])


class SourceTests(Experiment):
    def test_a_purged_run_counts_from_the_ledger_with_figures_but_no_sessions_turns_or_effort(self):
        unrecorded = self.row(self.report()["by_effort"], effort="unrecorded")
        # old1 (run directory, 1 session) and gone1 (ledger only): the ledger knows tokens and cost, not turns or sessions
        self.assertEqual(unrecorded["sessions"], 1)
        self.assertEqual(unrecorded["jobs_with_usage"], 2)
        self.assertEqual(unrecorded["metrics"]["input"]["total"], 110)
        self.assertEqual(unrecorded["metrics"]["cost_usd"], {"n": 2, "mean": 2.125, "median": 2.125, "total": 4.25})
        self.assertEqual(unrecorded["metrics"]["turns"], {"n": 1, "mean": 1.0, "median": 1.0, "total": 1})

    def test_the_run_directory_wins_the_figures_when_both_sources_have_the_job(self):
        lo = self.row(self.report()["by_effort"], effort="low")["metrics"]
        # the ledger says input 6 and cost 0.7 for lo1; the run directory's 5 and 0.5 are what is counted
        self.assertEqual((lo["input"]["total"], lo["cost_usd"]["total"], lo["turns"]["total"]), (5, 0.5, 3))

    def test_the_ledger_word_merged_or_closed_wins_the_outcome_over_the_status(self):
        high = self.row(self.report()["by_effort"], effort="high")
        self.assertEqual(high["outcomes"]["merged"], 1)  # hi1: pr_ready in state.json, merged by GitHub's word in the ledger

    def test_the_ledger_word_merged_beats_a_status_that_says_blocked(self):
        self.write_run(
            [planned("pub1", [40], "low")], {"pub1": state_job("needs_human_publish", [session()])}, run="run-f",
        )
        self.assertEqual(next(r for r in self.report()["by_effort"] if r["effort"] == "low")["outcomes"]["blocked"], 1)
        self.write_ledger([entry("pr_observed", "run-f", job="pub1", handoff_status="merged", fresh=True)], name="events-202611-002.jsonl")
        low = next(r for r in self.report()["by_effort"] if r["effort"] == "low")
        self.assertEqual((low["outcomes"]["merged"], low["outcomes"]["blocked"]), (2, 0))  # lo1 and pub1

    def test_without_that_word_the_run_directorys_status_wins_over_the_ledgers_status(self):
        self.write_ledger([
            entry("job", "run-a", job="hi2", status="stopped", phase="build", attempt=1),
            entry("job", "run-a", job="nodata", status="blocked", phase="build", attempt=1),
        ], name="events-202611-002.jsonl")
        high = self.row(self.report()["by_effort"], effort="high")
        # hi2 is blocked in state.json (the ledger's stopped loses); nodata has no outcome there, so the ledger's blocked fills it
        self.assertEqual(high["outcomes"], {"merged": 1, "closed": 0, "blocked": 2, "abandoned": 0, "stopped": 0, "other": 0})

    def test_the_ledgers_figures_stand_in_for_a_job_whose_run_directory_has_none(self):
        self.write_ledger([
            entry("job", "run-a", job="nodata", status="running", phase="build", attempt=1, cost_usd=1.5,
                  tokens={"build": {"input_tokens": 7, "output_tokens": 1}}),
        ], name="events-202611-002.jsonl")
        high = self.row(self.report()["by_effort"], effort="high")
        self.assertEqual(high["jobs_with_usage"], 3)
        self.assertEqual(high["metrics"]["input"]["total"], 37)
        self.assertEqual(high["metrics"]["cost_usd"]["total"], 7.5)
        self.assertEqual(high["sessions"], 3)  # the ledger has no session count: no session is invented

    def test_the_planned_effort_is_only_in_the_run_directory_so_a_job_that_is_in_both_keeps_it(self):
        self.assertEqual(self.row(self.report()["by_effort"], effort="high")["jobs"], 3)

    def test_a_run_directory_copied_elsewhere_is_a_run_of_its_own_not_matched_with_the_ledger(self):
        copy = self.root / "elsewhere" / "run-a"
        self.write_run([planned("hi1", [1], "high")], {"hi1": state_job("pr_ready", [session()])}, directory=copy)
        report = er.aggregate([str(copy), str(self.data / "history")])
        self.assertEqual((report["jobs"]["in_run_directory_only"], report["jobs"]["in_ledger_only"], report["jobs"]["in_both"]), (1, 4, 0))

    def test_the_identity_of_a_run_directory_is_its_place_in_the_layout_else_a_key_of_its_own(self):
        inside = str(self.run_dir())
        self.assertEqual(er.identity(inside, "j1"), ("org/name", "run-a", "j1"))
        for directory in (
            str(self.run_dir(name="bad name")),  # a repository name that is refused
            str(self.run_dir(run="bad run!")),  # a run name that is not a slug
            str(self.root / "elsewhere" / "run-a"),  # outside the layout
            str(self.data / "repos" / "org" / "name" / "other" / "run-a"),
        ):
            with self.subTest(directory=directory):
                self.assertEqual(er.identity(directory, "j1"), (None, directory, "j1"))

    def test_the_same_run_given_twice_is_read_once(self):
        report = er.aggregate([str(self.data), str(self.run_dir()), str(self.data / "history"), str(self.data / "history" / "events-202611-001.jsonl")])
        self.assertEqual((report["sources"]["run_directories"], report["sources"]["ledger_files"], report["jobs"]["found"]), (1, 1, 9))

    def test_a_run_directory_without_state_has_no_jobs_and_is_unreadable_not_an_error(self):
        directory = self.run_dir("run-z")
        directory.mkdir(parents=True)
        (directory / "plan.json").write_text(json.dumps({"jobs": [planned("x", [1], "high")]}))
        report = self.report()
        self.assertEqual((report["sources"]["run_directories"], report["sources"]["unreadable"], report["jobs"]["found"]), (2, 1, 9))


class FilterTests(Experiment):
    def test_issues_keep_the_jobs_of_those_issues_from_either_source(self):
        report = self.report(issues={1, 3, 8})
        self.assertEqual(report["jobs"]["selected"], 3)
        self.assertEqual(sum(r["jobs"] for r in report["by_effort"]), 3)
        self.assertEqual({r["effort"] for r in report["by_effort"]}, {"low", "high", "unrecorded"})
        self.assertEqual(report["filters"], {"issues": 3, "labelled": False})

    def test_issues_given_to_the_command_line_are_repeatable_and_comma_separated(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(er.main([str(self.data), "--json", "--issues", "1,2", "--issues", "3"]), 0)
        self.assertEqual(json.loads(out.getvalue())["jobs"]["selected"], 3)

    def test_a_job_without_a_known_issue_is_not_selected_by_an_issue_filter(self):
        self.write_run([{"id": "noissue", "choice": planned("x", [1], "low")["choice"]}], {"noissue": state_job("merged", [session()])}, run="run-d")
        self.assertEqual(self.report()["jobs"]["found"], 10)
        self.assertEqual(self.report(issues={1})["jobs"]["selected"], 1)

    def test_labelled_keeps_only_jobs_whose_effort_an_issue_label_chose(self):
        report = self.report(labelled=True)
        # hi1, hi2, lo1, cl1 and nodata; not the repository's choice (rep1), not the plan without a choice, not a purged job
        self.assertEqual(report["jobs"], {"found": 9, "selected": 5, "in_run_directory_only": 3, "in_ledger_only": 0, "in_both": 2, "over_limit": 0})
        self.assertEqual([r["effort"] for r in report["by_effort"]], ["low", "medium", "high"])
        self.assertEqual(self.row(report["by_effort"], effort="medium")["jobs"], 1)

    def test_both_filters_together(self):
        self.assertEqual(self.report(issues={1, 5}, labelled=True)["jobs"]["selected"], 1)  # issue 5 is the repository's choice

    def test_a_bad_issue_list_is_refused_with_a_fixed_message_that_does_not_echo_it(self):
        for text in (CANARY, "1,,2", "0", "-3", "99999999999", ""):
            with self.subTest(text=text), contextlib.redirect_stderr(io.StringIO()) as err:
                with self.assertRaises(SystemExit) as raised:
                    er.main([str(self.data), "--issues", text])
                self.assertEqual(raised.exception.code, 2)
                self.assertNotIn("CANARY", err.getvalue())

    def test_the_ledger_limit_is_a_whole_number_of_mebibytes(self):
        for text in ("0", "129", "x", "1.5"):
            with self.subTest(text=text), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    er.main([str(self.data), "--ledger-mib", text])


class HostileValueTests(Base):
    def test_hostile_and_malformed_values_are_ignored_never_trusted_or_raised(self):
        hostile = [
            session(counts={"input_tokens": True, "output_tokens": -5, "cache_creation_input_tokens": "9", "cache_read_input_tokens": 10**30}),
            session(counts={"input_tokens": 1.5, "output_tokens": [1], "cache_creation_input_tokens": None, "cache_read_input_tokens": {"a": 1}}),
            session(usage=[1, 2, 3]),
            {"phase": "build", "model": SONNET},
            "not a record", 17, None, [1], {"phase": ["unhashable"], "model": {"a": 1}, "usage": COUNTS, "total_cost_usd": True, "num_turns": True},
        ]
        self.write_run(
            [planned("h1", [1, True, -4, "7", 2**40], "high"), planned("bad id!", [2], "high"), {"id": ["unhashable"]}, "not a job",
             {"id": "h2", "issues": "1", "choice": {"build": {"effort": "ultra"}}}],
            {"h1": state_job("pr_ready", hostile), "h2": state_job(["unhashable"]), "bad id!": state_job("merged"), "h3": "not a dict", "h4": None},
        )
        report = self.report()
        self.assertEqual(report["jobs"]["found"], 2)  # h1 and h2: a bad id, a state that is not an object and a job that is not one are no jobs
        high = next(r for r in report["by_effort"] if r["effort"] == "high")
        self.assertEqual((high["jobs"], high["jobs_with_usage"], high["sessions_with_usage"]), (1, 1, 1))
        # only the one record with a valid count counts: the 10**30 and the negative, string and bool counts are dropped
        self.assertEqual(high["metrics"]["input"]["total"], COUNTS["input_tokens"])  # the unhashable-phase record: a valid usage object
        self.assertEqual(high["metrics"]["cost_usd"]["n"], 0)  # that record's cost was a bool: no figure
        self.assertEqual(high["metrics"]["turns"]["n"], 0)
        self.assertEqual(self.report(issues={2**40})["jobs"]["selected"], 0)  # a huge number is not an issue number
        text = er.render(report, check=True)
        self.assertIn("effort", text)

    def test_hostile_costs_turns_and_models_are_not_figures(self):
        sessions = [
            session(total_cost_usd=float("nan")), session(total_cost_usd=float("inf")), session(total_cost_usd=-1.0),
            session(total_cost_usd="1e9"), session(total_cost_usd=1e300), session(total_cost_usd=True),
            session(num_turns=True), session(num_turns=-3), session(num_turns=1.5), session(num_turns="4"), session(num_turns=2**40),
            session(model="not-a-claude-model; rm -rf"), session(model=CANARY), session(model=["list"]), session(model=None),
        ]
        self.write_run([planned("h1", [1], "low")], {"h1": state_job("merged", sessions)})
        report = self.report()
        row = report["by_effort"][0]
        self.assertEqual((row["sessions"], row["sessions_with_usage"]), (15, 15))
        # per job: the nine sessions whose cost is valid sum to 9.0 and the ten whose turns are valid to 50
        self.assertEqual(row["metrics"]["cost_usd"], {"n": 1, "mean": 9.0, "median": 9.0, "total": 9.0})
        self.assertEqual(row["metrics"]["turns"], {"n": 1, "mean": 50.0, "median": 50.0, "total": 50})
        self.assertEqual(sum(r["metrics"]["cost_usd"]["n"] for r in report["sessions"]), 9)
        self.assertEqual(sum(r["metrics"]["turns"]["n"] for r in report["sessions"]), 10)
        models = {r["model"] for r in report["sessions"]}
        self.assertEqual(models, {SONNET, "other", "unrecorded"})
        self.assertNotIn(CANARY, json.dumps(report))

    def test_a_cost_too_large_for_a_float_is_not_a_figure_and_does_not_abort_the_report(self):
        sessions = [session(total_cost_usd=10**400), session(total_cost_usd=2.0)]
        self.write_run([planned("h1", [1], "low")], {"h1": state_job("merged", sessions)})
        row = self.report()["by_effort"][0]
        self.assertEqual((row["sessions"], row["metrics"]["cost_usd"]["total"]), (2, 2.0))

    def test_a_state_that_is_not_json_or_not_an_object_is_unreadable(self):
        for text in ("{not json", "[1, 2]", "null", '"text"', "{" * 100000):
            with self.subTest(text=text[:10]):
                directory = self.run_dir("run-x")
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "state.json").write_text(text)
                report = self.report()
                self.assertEqual((report["jobs"]["found"], report["sources"]["unreadable"]), (0, 1))

    def test_an_oversized_state_is_unreadable_and_jobs_that_state_gives_no_usage_list_still_count(self):
        directory = self.run_dir("run-x")
        directory.mkdir(parents=True)
        (directory / "state.json").write_text(json.dumps({"jobs": {"a": {"status": "merged", "usage": "x" * 10}}}))
        self.assertEqual(self.report()["jobs"]["found"], 1)
        (directory / "state.json").write_text(json.dumps({"jobs": {}, "pad": "x" * (dashboard.MAX_STATE_BYTES + 10)}))
        report = self.report()
        self.assertEqual((report["jobs"]["found"], report["sources"]["unreadable"]), (0, 1))

    def test_state_jobs_that_are_not_an_object_make_no_jobs(self):
        directory = self.run_dir("run-x")
        directory.mkdir(parents=True)
        (directory / "state.json").write_text(json.dumps({"jobs": [1, 2]}))
        self.assertEqual(self.report()["jobs"]["found"], 0)


class LedgerReadingTests(Base):
    def test_malformed_oversized_and_unknown_lines_are_skipped_and_counted(self):
        good = entry("job", "run-a", job="j1", status="merged", phase="build", attempt=1, cost_usd=1.0, tokens={"build": dict(COUNTS)}, issues=[1])
        big = json.dumps(entry("job", "run-a", job="j2", status="merged", note="x" * (history.MAX_LINE + 10)))
        raw = b"\n".join([
            b"{not json", b"[1, 2]", b'"text"', big.encode(), json.dumps({**good, "v": 0}).encode(), json.dumps({**good, "kind": "nope"}).encode(),
            json.dumps({**good, "repo": "../escape"}).encode(), json.dumps({**good, "at": "yesterday"}).encode(), b'{"v": 1, "kind": "job", "at"',
        ])
        self.write_ledger([good], raw=raw)
        report = self.report()
        self.assertEqual(report["jobs"]["found"], 1)
        self.assertEqual(report["sources"]["ledger_lines_skipped"], 9)
        self.assertEqual(report["by_effort"][0]["outcomes"]["merged"], 1)

    def test_a_kind_that_is_a_list_or_an_object_and_a_cost_too_large_for_a_float_are_skipped_or_dropped_never_raised(self):
        good = entry("job", "run-a", job="j1", status="merged", phase="build", attempt=1, cost_usd=1.0, tokens={"build": dict(COUNTS)}, issues=[1])
        huge = entry("job", "run-a", job="j2", status="merged", phase="build", attempt=1, cost_usd=10**400, tokens={"build": dict(COUNTS)}, issues=[2])
        planner = entry("planned", "run-a", planner_cost_usd=10**400)
        hostile = [{**good, "kind": kind} for kind in (["job"], {}, {"job": 1}, [])]
        self.write_ledger([good, huge, planner, *hostile])
        report = self.report()
        self.assertEqual(report["sources"]["ledger_lines_skipped"], 4)
        self.assertEqual(report["jobs"]["found"], 2)
        row = report["by_effort"][0]
        self.assertEqual((row["jobs"], row["metrics"]["cost_usd"]["total"]), (2, 1.0))

    def test_the_ledger_is_read_newest_first_up_to_the_byte_limit_and_says_when_it_cut(self):
        lines = [entry("job", "run-a", job=f"j{n}", status="merged", phase="build", attempt=1, cost_usd=1.0, tokens={"build": dict(COUNTS)}) for n in range(40)]
        path = self.write_ledger(lines)
        size = path.stat().st_size
        whole = er.read_ledger([str(path)], size)
        self.assertEqual((len(whole["jobs"]), whole["truncated"]), (40, False))
        cut = er.read_ledger([str(path)], size // 2)
        self.assertTrue(cut["truncated"])
        self.assertTrue(0 < len(cut["jobs"]) < 40)
        self.assertIn(("org/name", "run-a", "j39"), cut["jobs"])  # the newest events survive, the oldest are left out
        self.assertNotIn(("org/name", "run-a", "j0"), cut["jobs"])
        self.assertEqual(cut["lines_skipped"], 0)  # the cut line is not a skipped line: it is not read

    def test_older_segments_are_left_out_when_the_budget_is_spent(self):
        old = self.write_ledger([entry("job", "run-a", job="old", status="merged")], name="events-202610-001.jsonl")
        new = self.write_ledger([entry("job", "run-a", job="new", status="merged")], name="events-202611-001.jsonl")
        got = er.read_ledger([str(old), str(new)], new.stat().st_size)
        self.assertEqual((sorted(k[2] for k in got["jobs"]), got["truncated"]), (["new"], True))

    def test_an_empty_or_missing_ledger_is_not_an_error(self):
        path = self.write_ledger([])
        self.assertEqual(er.aggregate([str(path)])["jobs"]["found"], 0)

    def test_a_job_in_the_ledger_with_no_tokens_has_no_figures_but_an_outcome(self):
        self.write_ledger([entry("job", "run-a", job="j1", status="stopped", phase="build", attempt=1, stopped_by="cli")])
        row = self.report_row()
        self.assertEqual((row["jobs"], row["jobs_with_usage"], row["outcomes"]["stopped"]), (1, 0, 1))
        self.assertEqual(row["metrics"]["cost_usd"]["n"], 0)

    def report_row(self):
        return er.aggregate([str(self.data)])["by_effort"][0]


class ReadOnlyTests(Experiment):
    def test_only_regular_files_are_read_and_no_symlink_is_followed(self):
        outside = self.root / "outside"
        self.write_run([planned("x1", [50], "max")], {"x1": state_job("merged", [session()])}, directory=outside / "run-q")
        os.symlink(outside / "run-q", self.run_dir("linked-run"))  # a symlinked run directory
        directory = self.run_dir("run-s")
        directory.mkdir(parents=True)
        os.symlink(self.run_dir() / "state.json", directory / "state.json")  # a symlinked state file
        os.symlink(self.data / "history" / "events-202611-001.jsonl", self.data / "history" / "events-202611-009.jsonl")  # a symlinked segment
        dot = self.run_dir(".home-run")
        self.write_run([planned("x2", [51], "max")], {"x2": state_job("merged", [session()])}, directory=dot)
        nested = self.run_dir() / "hi1" / "worktree"  # a job directory holds worktrees: a run directory is never entered
        self.write_run([planned("x3", [52], "max")], {"x3": state_job("merged", [session()])}, directory=nested)
        clone = self.data / "repos" / "org" / "name" / "clone"  # the managed checkout may hold any file, a state.json too
        self.write_run([planned("x4", [53], "max")], {"x4": state_job("merged", [session()])}, directory=clone / "docs")
        report = self.report()
        self.assertEqual(report["jobs"]["found"], 9)  # nothing from the outside, the dot directory, the worktree or the clone
        self.assertNotIn("max", [r["effort"] for r in report["by_effort"]])
        self.assertEqual(report["sources"]["run_directories"], 2)  # run-a and run-s (whose state.json is refused)
        self.assertEqual(report["sources"]["unreadable"], 2)  # the symlinked state file and the symlinked segment
        self.assertEqual(er.aggregate([str(self.run_dir("linked-run"))])["jobs"]["found"], 0)
        self.assertEqual(er.aggregate([str(self.data / "history" / "events-202611-009.jsonl")])["jobs"]["found"], 0)

    def test_the_directory_is_unchanged_after_a_report(self):
        def snapshot():
            return sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in self.root.rglob("*"))

        before = snapshot()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(er.main([str(self.data), "--check"]), 0)
            self.assertEqual(er.main([str(self.data), "--json"]), 0)
        self.assertEqual(before, snapshot())

    def test_a_missing_path_and_an_empty_directory_are_just_empty(self):
        for target in (self.root / "nope", self.root / "empty"):
            (self.root / "empty").mkdir(exist_ok=True)
            report = er.aggregate([str(target)])
            self.assertEqual((report["by_effort"], report["sessions"], report["jobs"]["found"]), ([], [], 0))
            text = er.render(report, check=True)
            self.assertIn("jobs found: 0", text)
            self.assertIn("(nothing)", text)
            self.assertIn("jobs in the tables: 0 of 0 selected", text)


class OutputTests(Experiment):
    def test_no_free_text_run_name_job_name_or_path_reaches_the_text_or_the_json(self):
        weird = self.root / "CANARY-FREE-TEXT-dir" / "run-w"  # a run directory outside the layout, with a canary in its path
        self.write_run([planned("hi1", [1], "high")], {"hi1": state_job("pr_ready", [session(model="claude-canary-free-text-model!")])}, directory=weird)
        report = er.aggregate([str(self.data), str(weird.parent)])
        text = f"{er.render(report, check=True)}\n{json.dumps(report, sort_keys=True)}"
        for forbidden in ("CANARY", "canary", "run-a", "run-b", "run-w", "hi1", "hi2", "gone1", "nodata", str(self.root), "github.com", "drain"):
            self.assertNotIn(forbidden, text, forbidden)

    def test_the_command_line_output_is_text_and_one_json_document(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            er.main([str(self.data), "--json"])
        document = json.loads(out.getvalue())
        self.assertEqual(sorted(document), ["by_effort", "by_effort_and_model", "check", "filters", "jobs", "note", "sessions", "sources"])
        self.assertIn("not evidence without repeats", document["note"])
        text = io.StringIO()
        with contextlib.redirect_stdout(text):
            er.main([str(self.data)])
        self.assertIn("not evidence without repeats", text.getvalue())
        self.assertNotIn("token sums that disagree", text.getvalue())  # only with --check

    def test_the_text_prints_n_next_to_every_mean(self):
        text = er.render(self.report())
        self.assertRegex(text, r"effort\s+metric\s+n\s+mean\s+median\s+total")
        self.assertRegex(text, r"phase\s+effort\s+model\s+metric\s+n\s+mean\s+median\s+total")
        row = next(line for line in text.splitlines() if line.startswith("high") and "cost_usd" in line)
        self.assertEqual(row.split(), ["high", "cost_usd", "2", "3.00", "3.00", "6.00"])  # n comes right before the mean
        self.assertIn("cost is the CLI's client-side estimate, not billing data", text)

    def test_check_numbers(self):
        check = self.report()["check"]
        self.assertEqual(check, {
            "jobs_in_both_sources": 2, "token_sums_that_disagree": 1, "cost_sums_that_disagree": 1, "jobs_in_the_tables": 9,
            "jobs_selected": 9, "sessions_without_token_counts": 0,
        })
        text = er.render(self.report(), check=True)
        self.assertIn("jobs in both sources: 2; of those, token sums that disagree: 1, cost sums that disagree: 1", text)
        self.assertIn("jobs in the tables: 9 of 9 selected", text)

    def test_a_session_without_token_counts_is_a_session_and_in_no_figure(self):
        self.write_run([planned("e1", [30], "low")], {"e1": state_job("blocked", [session(counts={}, cost=99.0, turns=99), session(counts={"input_tokens": 3})])}, run="run-e")
        report = self.report()
        low = next(r for r in report["by_effort"] if r["effort"] == "low")
        self.assertEqual((low["sessions"], low["sessions_with_usage"]), (3, 2))  # lo1's one, e1's two records (one measured)
        self.assertEqual(low["metrics"]["cost_usd"]["total"], 1.5)  # the 99.0 of the record without a result is not a figure
        self.assertEqual(low["metrics"]["input"]["total"], 8)
        self.assertEqual(report["check"]["sessions_without_token_counts"], 1)

    def test_a_row_with_no_input_at_all_has_no_hit_rate_not_a_zero_division(self):
        self.write_run([planned("z1", [31], "max")], {"z1": state_job("running")}, run="run-z")
        row = next(r for r in self.report()["by_effort"] if r["effort"] == "max")
        self.assertEqual((row["jobs"], row["jobs_with_usage"], row["cache_hit_rate"]), (1, 0, None))
        self.assertEqual(row["metrics"]["turns"], {"n": 0, "mean": None, "median": None, "total": 0})
        self.assertIn("-", er.render(self.report()))


if __name__ == "__main__":
    unittest.main()

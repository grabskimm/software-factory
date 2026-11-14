"""The false-incomplete rate (src/factory/false_incomplete.py), its refresh by the janitor sweep and GET /repos.

GitHub is a fake reader that serves closed pull requests, commit lists and ``factory/gate`` histories; nothing
here calls ``gh``. Every status carries a canary in the words a real one could carry text in: it must never reach
a result, a response or a log.
"""

import json
import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from factory import ci_gate
from factory import false_incomplete as fi
from factory import reason_codes, runner, trigger
from factory.false_incomplete import FalseIncompleteCache
from factory.trigger import ClosedPull, GateStatus
from tests.test_manager import VIEWER, ServerCase
from tests.test_operations import CANARY, NOW, OpsCase

LOGIN = "factory-app[bot]"
USER_ID = 789
FACTORY = {"type": "Bot", "id": USER_ID, "login": LOGIN}
HUMAN = {"type": "User", "id": 5, "login": "alice"}
START = datetime.fromtimestamp(NOW, timezone.utc)
DAY = timedelta(days=1)


def stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def gate(state, description, creator=FACTORY):
    return GateStatus(state, description, "2026-01-01T00:00:00Z", creator)


def incomplete(code="real_failure", sha="a" * 7):
    return gate("failure", f"incomplete: review or verification did not pass @{sha}" + reason_codes.suffix(code))


def awaiting(sha="a" * 7):
    return gate("pending", f"awaiting required CI: build @{sha}" + reason_codes.suffix("ci_pending"))


def complete(sha="a" * 7):
    return gate("success", f"review+verify passed @{sha}")


def ci_green(sha="a" * 7):
    return gate("success", f"review+verify+required CI passed @{sha}")


def publishing(sha="a" * 7):
    return gate("pending", f"publishing @{sha}")


class FakeGithub:
    """Closed pull requests, commits and gate histories (newest status first). Only read methods exist."""

    app_user_id = USER_ID

    def __init__(self):
        self.pulls = []
        self.commits = {}  # number -> [sha oldest first]
        self.history = {}  # sha -> [GateStatus newest first]
        self.truncated = False
        self.calls = []
        self.fail = None

    def merged(self, number, head, history, *, days=1, commits=None):
        """A pull request merged ``days`` ago at ``head``; ``history`` is the gate history of ``head``."""
        when = stamp(START - days * DAY)
        self.pulls.append(ClosedPull(number, when, when, head))
        self.history[head] = history
        self.commits[number] = commits or [head]

    def closed(self, number, head, *, days=1):
        when = stamp(START - days * DAY)
        self.pulls.append(ClosedPull(number, "", when, head))

    def closed_pulls(self, login, since, max_pages=5):
        self.calls.append("closed_pulls")
        if self.fail is not None:
            raise self.fail
        assert login == LOGIN
        return list(self.pulls), self.truncated

    def pull_commits(self, number, max_pages=3):
        self.calls.append("pull_commits")
        return list(self.commits[number])

    def gate_history(self, sha, context):
        self.calls.append("gate_history")
        assert context == "factory/gate"
        return list(self.history.get(sha, []))


def head(n):
    return f"{n:040x}"


def compute(github, **extra):
    return fi.compute(github, LOGIN, USER_ID, START, **extra)


class RateTests(unittest.TestCase):
    def test_a_pull_request_merged_with_the_head_factory_published_is_a_false_incomplete(self):
        github = FakeGithub()
        github.merged(1, head(1), [incomplete(), publishing()])
        result = compute(github, min_sample=1)
        self.assertEqual(result["counts"]["counted"], 1)
        self.assertEqual(result["counts"]["merged_unchanged"], 1)
        self.assertEqual(result["rate"], 1.0)

    def test_a_pull_request_merged_after_a_later_push_does_not_count_as_unchanged(self):
        github = FakeGithub()
        # Factory published head(1) as incomplete; a person pushed head(2) (no Factory status) and merged it.
        github.merged(1, head(2), [], commits=[head(1), head(2)])
        github.history[head(1)] = [incomplete(), publishing()]
        result = compute(github, min_sample=1)
        self.assertEqual(result["counts"]["counted"], 1)
        self.assertEqual(result["counts"]["merged_unchanged"], 0)
        self.assertEqual(result["counts"]["merged_changed"], 1)
        self.assertEqual(result["rate"], 0.0)

    def test_closed_unmerged_pull_requests_are_counted_apart_and_never_in_the_rate(self):
        github = FakeGithub()
        github.merged(1, head(1), [incomplete()])
        github.closed(2, head(2))
        github.history[head(2)] = [incomplete()]
        result = compute(github, min_sample=1)
        self.assertEqual(result["counts"]["closed_unmerged"], 1)
        self.assertEqual(result["counts"]["counted"], 1)
        self.assertEqual(result["rate"], 1.0)
        self.assertEqual(github.calls.count("gate_history"), 1)  # the closed one cost no status read

    def test_an_empty_window_has_no_rate_and_no_division(self):
        result = compute(FakeGithub())
        self.assertIsNone(result["rate"])
        self.assertEqual(result["sample"], fi.SAMPLE_EMPTY)
        self.assertEqual(result["counts"]["counted"], 0)
        for row in result["by_reason"].values():
            self.assertIsNone(row["rate"])
            self.assertEqual(row["sample"], fi.SAMPLE_EMPTY)

    def test_a_window_under_the_minimum_sample_withholds_the_rate_and_says_so(self):
        github = FakeGithub()
        for n in range(1, fi.MIN_SAMPLE):
            github.merged(n, head(n), [incomplete()])
        result = compute(github)
        self.assertEqual(result["counts"]["counted"], fi.MIN_SAMPLE - 1)
        self.assertIsNone(result["rate"])
        self.assertEqual(result["sample"], fi.SAMPLE_BELOW_MINIMUM)
        self.assertEqual(result["min_sample"], fi.MIN_SAMPLE)
        github.merged(99, head(99), [incomplete()])
        result = compute(github)
        self.assertEqual((result["rate"], result["sample"]), (1.0, fi.SAMPLE_OK))

    def test_pull_requests_outside_the_window_are_ignored(self):
        github = FakeGithub()
        github.merged(1, head(1), [incomplete()], days=fi.WINDOW_DAYS + 1)
        github.closed(2, head(2), days=fi.WINDOW_DAYS + 1)
        result = compute(github, min_sample=1)
        self.assertEqual(result["counts"]["counted"], 0)
        self.assertEqual(result["counts"]["closed_unmerged"], 0)
        self.assertEqual(result["window"], {"from": stamp(START - 30 * DAY), "to": stamp(START), "days": 30})

    def test_a_complete_job_is_not_a_sample(self):
        github = FakeGithub()
        github.merged(1, head(1), [complete(), publishing()])
        self.assertEqual(compute(github, min_sample=1)["counts"]["counted"], 0)

    def test_a_pending_gate_that_the_required_checks_resolved_is_not_a_false_incomplete(self):
        github = FakeGithub()
        github.merged(1, head(1), [ci_green(), awaiting(), publishing()])
        github.merged(2, head(2), [awaiting(), publishing()])  # merged while still pending: counted
        result = compute(github, min_sample=1)
        self.assertEqual(result["counts"]["counted"], 1)
        self.assertEqual(result["by_reason"]["ci_pending"]["merged_unchanged"], 1)

    def test_a_person_applying_the_completion_label_does_not_hide_the_incomplete_verdict(self):
        github = FakeGithub()
        github.merged(1, head(1), [gate("success", "verified by hand by @alice @aaaaaaa"), incomplete("env_noise")])
        result = compute(github, min_sample=1)
        self.assertEqual(result["by_reason"]["env_noise"]["merged_unchanged"], 1)

    def test_only_statuses_written_by_the_factory_app_count(self):
        github = FakeGithub()
        github.merged(1, head(1), [gate("failure", "incomplete: x [real_failure]", HUMAN)])
        self.assertEqual(compute(github, min_sample=1)["counts"]["counted"], 0)
        other = {"type": "Bot", "id": 1, "login": LOGIN}  # same login, another user id
        github.merged(2, head(2), [gate("failure", "incomplete: x [real_failure]", other)])
        self.assertEqual(compute(github, min_sample=1)["counts"]["counted"], 0)

    def test_without_a_user_id_the_bot_login_decides(self):
        github = FakeGithub()
        github.merged(1, head(1), [incomplete()])
        result = fi.compute(github, LOGIN, None, START, min_sample=1)
        self.assertEqual(result["counts"]["counted"], 1)

    def test_the_breakdown_covers_every_reason_code_and_uncoded_statuses(self):
        github = FakeGithub()
        for n, code in enumerate(reason_codes.ORDER, 1):
            github.merged(n, head(n), [incomplete(code)])
        github.merged(50, head(50), [incomplete(code="real_failure")], commits=[head(49), head(50)])
        github.history[head(50)] = []  # changed after the verdict
        github.history[head(49)] = [incomplete("real_failure")]
        github.merged(60, head(60), [gate("failure", "incomplete: review or verification did not pass @aaaaaaa")])
        result = compute(github, min_sample=1)
        self.assertEqual(list(result["by_reason"]), list(reason_codes.ORDER))
        self.assertEqual(result["by_reason"]["real_failure"]["counted"], 2)
        self.assertEqual(result["by_reason"]["real_failure"]["merged_changed"], 1)
        self.assertEqual(result["by_reason"]["real_failure"]["rate"], 0.5)
        for code in reason_codes.ORDER[1:]:
            self.assertEqual(result["by_reason"][code]["counted"], 1)
        self.assertEqual(result["counts"]["uncoded"], 1)
        # the workflow_hold one is in its own row and counts.workflow_hold, not in the headline
        self.assertEqual(result["counts"]["counted"], len(reason_codes.ORDER) + 1)
        self.assertEqual(result["counts"]["workflow_hold"], 1)

    def test_the_per_code_rate_has_its_own_minimum_sample(self):
        github = FakeGithub()
        for n in range(1, fi.MIN_SAMPLE + 1):
            github.merged(n, head(n), [incomplete("ci_failed")])
        github.merged(99, head(99), [incomplete("real_failure")])
        result = compute(github)
        self.assertEqual(result["by_reason"]["ci_failed"]["sample"], fi.SAMPLE_OK)
        self.assertEqual(result["by_reason"]["real_failure"]["sample"], fi.SAMPLE_BELOW_MINIMUM)
        self.assertIsNone(result["by_reason"]["real_failure"]["rate"])
        self.assertEqual(result["sample"], fi.SAMPLE_OK)

    def test_the_newest_merged_pull_requests_are_examined_up_to_the_cap(self):
        github = FakeGithub()
        for n in range(1, 6):
            github.merged(n, head(n), [incomplete()], days=n)
        result = compute(github, min_sample=1, max_pulls=3)
        self.assertEqual(result["counts"]["counted"], 3)
        self.assertTrue(result["truncated"])
        self.assertFalse(compute(github, min_sample=1)["truncated"])

    def test_a_merged_pull_request_without_any_factory_verdict_is_unresolved_not_counted(self):
        github = FakeGithub()
        github.merged(1, head(1), [], commits=[head(1)])
        counts = compute(github, min_sample=1)["counts"]
        self.assertEqual((counts["counted"], counts["unresolved"]), (0, 1))

    def test_the_look_back_is_bounded(self):
        github = FakeGithub()
        shas = [head(n) for n in range(100, 100 + fi.MAX_COMMITS_BACK + 3)]
        github.merged(1, shas[-1], [], commits=shas)
        github.history[shas[0]] = [incomplete()]
        counts = compute(github, min_sample=1)["counts"]
        self.assertEqual((counts["counted"], counts["unresolved"]), (0, 1))  # surfaced, not silently lost

    def test_pull_requests_changed_beyond_the_look_back_or_force_pushed_are_reported_not_dropped(self):
        github = FakeGithub()
        for n in range(1, 11):
            github.merged(n, head(n), [incomplete()])  # merged unchanged
        for n in range(11, 21):  # verdict at the first commit, then more pushes than the look-back reaches
            shas = [head(1000 + 10 * n + k) for k in range(fi.MAX_COMMITS_BACK + 2)]
            github.merged(n, shas[-1], [], commits=shas)
            github.history[shas[0]] = [incomplete()]
        for n in range(21, 26):  # force-pushed: the verdict commit is gone from the pull request
            github.merged(n, head(n), [], commits=[head(n)])
        result = compute(github)
        self.assertEqual(result["counts"]["counted"], 10)
        self.assertEqual(result["counts"]["unresolved"], 15)
        self.assertEqual(result["rate"], 1.0)  # an upper bound, which `unresolved` says

    def test_a_workflow_hold_is_kept_out_of_the_headline_but_has_its_row(self):
        github = FakeGithub()
        for n in range(1, 6):
            github.merged(n, head(n), [incomplete("workflow_hold")])
        for n in range(6, 11):  # real failures a person fixed before merging
            github.merged(n, head(n + 100), [], commits=[head(n), head(n + 100)])
            github.history[head(n)] = [incomplete("real_failure")]
        result = compute(github, min_sample=1)
        self.assertEqual(result["counts"]["workflow_hold"], 5)
        self.assertEqual(result["counts"]["counted"], 5)
        self.assertEqual(result["counts"]["merged_unchanged"], 0)
        self.assertEqual(result["rate"], 0.0)
        self.assertEqual(result["by_reason"]["workflow_hold"]["counted"], 5)
        self.assertEqual(result["by_reason"]["workflow_hold"]["merged_unchanged"], 5)

    def test_the_newest_held_back_code_is_the_reason(self):
        # ci_failed was posted over the earlier awaiting ci_pending: the outcome is ci_failed
        history = [incomplete("ci_failed"), awaiting()]
        self.assertEqual(fi.classify(history, fi.factory_creator(LOGIN, USER_ID)), (True, "ci_failed"))

    def test_a_login_alone_is_not_enough_without_a_user_id_it_must_also_be_a_bot(self):
        human = {"type": "User", "id": 5, "login": LOGIN}
        self.assertFalse(fi.factory_creator(LOGIN, None)(human))
        self.assertTrue(fi.factory_creator(LOGIN, None)(FACTORY))
        self.assertFalse(fi.factory_creator(LOGIN, USER_ID)(human))

    def test_a_first_verdict_of_success_is_not_held_back_even_if_a_failure_follows_on_the_same_head(self):
        history = [incomplete(), complete()]  # newest first: success posted first, failure later
        self.assertEqual(fi.classify(history, fi.factory_creator(LOGIN, USER_ID))[0], False)

    def test_a_stop_request_interrupts_between_pull_requests(self):
        github = FakeGithub()
        github.merged(1, head(1), [incomplete()])
        github.merged(2, head(2), [incomplete()])
        with self.assertRaises(InterruptedError):
            compute(github, stop=lambda: True)
        self.assertNotIn("gate_history", github.calls)

    def test_a_github_error_propagates_for_the_caller_to_report(self):
        github = FakeGithub()
        github.fail = RuntimeError(CANARY)
        with self.assertRaises(RuntimeError):
            compute(github)

    def test_no_text_of_any_status_reaches_the_result(self):
        github = FakeGithub()
        github.merged(1, head(1), [gate("failure", f"incomplete: {CANARY} @aaaaaaa [real_failure]")])
        github.merged(2, head(2), [gate("failure", f"{CANARY} [not_a_code]")])
        github.closed(3, head(3))
        result = compute(github, min_sample=1)
        self.assertNotIn(CANARY, json.dumps(result))
        self.assertEqual(result["by_reason"]["real_failure"]["counted"], 1)
        self.assertEqual(result["counts"]["uncoded"], 1)

    def test_the_description_markers_follow_what_factory_posts(self):
        runner_source = Path(runner.__file__).read_text()
        gate_source = Path(ci_gate.__file__).read_text()
        self.assertIn(f'f"{fi.TRANSIENT_PREFIX}{{sha[:7]}}"', runner_source)
        self.assertIn(fi.TRANSIENT_SYNC, runner_source)
        self.assertIn(f'f"{fi.CI_RESOLVED_PREFIX} @{{sha}}"', gate_source)
        self.assertEqual(fi.code_of("incomplete: x @abc1234 [env_noise]"), "env_noise")
        self.assertIsNone(fi.code_of("incomplete: x @abc1234"))
        self.assertEqual(
            fi.code_of(ci_gate.awaiting_description(["build"], "a" * 40, reason_codes.CI_PENDING)), "ci_pending"
        )


class CacheTests(unittest.TestCase):
    def test_nothing_known_yet_is_checking(self):
        self.assertEqual(FalseIncompleteCache().get("org/name"), {"state": "checking"})

    def test_a_failure_keeps_the_last_value_stale_and_otherwise_is_unavailable(self):
        cache = FalseIncompleteCache()
        cache.failed("org/name")
        self.assertEqual(cache.get("org/name"), {"state": "unavailable", "reason": fi.REFRESH_FAILED})
        cache.set("org/name", compute(FakeGithub()))
        self.assertEqual(cache.get("org/name")["state"], "ok")
        cache.failed("org/name")
        stale = cache.get("org/name")
        self.assertEqual((stale["state"], stale["counts"]["counted"]), ("stale", 0))

    def test_a_returned_value_cannot_change_the_cache(self):
        cache = FalseIncompleteCache()
        cache.set("org/name", compute(FakeGithub()))
        cache.get("org/name")["counts"]["counted"] = 9
        self.assertEqual(cache.get("org/name")["counts"]["counted"], 0)


class ReaderTests(unittest.TestCase):
    """``GithubReader``'s new reads, against a fake ``gh`` that answers with the already-projected rows."""

    def reader(self, answers):
        calls = []

        def run(argv, cwd, timeout=None, auth=None):
            calls.append(argv)
            return answers(argv)

        return trigger.GithubReader("org/name", None, Path("."), run=run), calls

    def test_closed_pulls_keep_only_the_apps_and_stop_at_the_window(self):
        rows = [
            {"number": 3, "login": "factory-app[bot]", "type": "Bot", "merged": "2026-01-03T00:00:00Z",
             "closed": "2026-01-03T00:00:00Z", "updated": "2026-01-03T00:00:00Z", "head": "c3"},
            {"number": 2, "login": "alice", "type": "User", "merged": "", "closed": "x", "updated": "2026-01-02T00:00:00Z", "head": "c2"},
            {"number": 1, "login": "factory-app[bot]", "type": "Bot", "merged": "", "closed": "2025-12-01T00:00:00Z",
             "updated": "2025-12-01T00:00:00Z", "head": "c1"},
        ]
        reader, calls = self.reader(lambda argv: "\n".join(json.dumps(row) for row in rows))
        found, truncated = reader.closed_pulls("Factory-App[bot]", "2026-01-01T00:00:00Z")
        self.assertEqual(found, [ClosedPull(3, "2026-01-03T00:00:00Z", "2026-01-03T00:00:00Z", "c3")])
        self.assertFalse(truncated)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("-X", calls[0])  # a GET

    def test_gate_history_and_commits_are_read_only_calls(self):
        statuses = {"state": "failure", "description": "d", "created": "t", "ctype": "Bot", "cid": 7, "clogin": "f[bot]"}
        reader, calls = self.reader(
            lambda argv: json.dumps(statuses) if "statuses" in argv[-3] else json.dumps({"sha": "abc"})
        )
        history = reader.gate_history("abc", "factory/gate")
        self.assertEqual(history, [GateStatus("failure", "d", "t", {"type": "Bot", "id": 7, "login": "f[bot]"})])
        self.assertEqual(reader.pull_commits(4), ["abc"])
        for argv in calls:
            self.assertNotIn("-X", argv)
            self.assertNotIn("-f", argv)

    def test_the_app_user_id_comes_from_the_identity_when_it_has_one(self):
        class Auth:
            app_user_id = 9

        self.assertEqual(trigger.GithubReader("o/n", Auth(), Path(".")).app_user_id, 9)
        self.assertIsNone(trigger.GithubReader("o/n", None, Path(".")).app_user_id)


def tree(root):
    """Every path under ``root`` with its size and modification time: a read-only pass changes none of it."""
    return sorted((str(p.relative_to(root)), p.stat().st_size, p.stat().st_mtime_ns) for p in Path(root).rglob("*"))


class JanitorRefreshTests(OpsCase):
    def setUp(self):
        super().setUp()
        self.manager.env["FACTORY_GITHUB_APP_SLUG"] = "factory-app"
        self.github = FakeGithub()
        for n in range(1, fi.MIN_SAMPLE + 1):
            self.github.merged(n, head(n), [incomplete("ci_failed")])
        self.github.merged(40, head(41), [], commits=[head(40), head(41)])
        self.github.history[head(40)] = [incomplete("real_failure")]
        self.github.closed(50, head(50))
        self.readers = []
        self.ops = self.new_ops(self.manager, github=self.reader_for)

    def reader_for(self, cfg):
        self.readers.append(cfg.repo)
        return self.github

    def test_a_sweep_fills_the_cache_without_a_thread_or_a_write(self):
        self.make_run("r1")
        before = tree(self.data_dir)
        self.ops.sweep()
        value = self.manager.false_incomplete.get("org/name")
        self.assertEqual(value["state"], "ok")
        self.assertEqual(value["counts"]["counted"], fi.MIN_SAMPLE + 1)
        self.assertEqual(value["counts"]["merged_changed"], 1)
        self.assertEqual(value["counts"]["closed_unmerged"], 1)
        self.assertEqual(value["by_reason"]["ci_failed"]["rate"], 1.0)
        self.assertEqual(self.ops.threads, [])
        self.assertIsNone(self.manager.operations)  # the manager started no operations (no thread) either
        self.assertEqual(set(self.github.calls), {"closed_pulls", "gate_history", "pull_commits"})
        self.assertEqual(tree(self.data_dir), before)  # no run directory touched

    def test_the_refresh_is_paced_not_done_on_every_sweep(self):
        self.ops.sweep()
        calls = len(self.github.calls)
        self.time.now += 600
        self.ops.sweep()
        self.assertEqual(len(self.github.calls), calls)
        self.time.now += 3600
        self.ops.sweep()
        self.assertGreater(len(self.github.calls), calls)

    def test_a_restart_rebuilds_the_same_value_from_github_alone(self):
        self.ops.sweep()
        first = self.manager.false_incomplete.get("org/name")
        self.manager = self.new_manager()
        self.manager.env["FACTORY_GITHUB_APP_SLUG"] = "factory-app"
        self.assertEqual(self.manager.false_incomplete.get("org/name"), {"state": "checking"})
        self.ops = self.new_ops(self.manager, github=self.reader_for)
        self.ops.sweep()
        self.assertEqual(self.manager.false_incomplete.get("org/name"), first)

    def test_purged_runs_do_not_leave_the_window(self):
        self.make_run("r1")
        self.verdict(("a", "merged"))
        self.ops.sweep()
        self.assertEqual(self.names("purge"), [("org/name", "r1")])  # the run is gone from disk
        self.assertEqual(self.manager.false_incomplete.get("org/name")["counts"]["counted"], fi.MIN_SAMPLE + 1)

    def test_without_the_app_slug_nothing_is_read_and_the_cache_says_why(self):
        del self.manager.env["FACTORY_GITHUB_APP_SLUG"]
        self.ops.sweep()
        self.assertEqual(self.github.calls, [])
        self.assertEqual(self.readers, [])
        self.assertEqual(self.manager.false_incomplete.get("org/name"), {"state": "unavailable", "reason": fi.NO_APP})

    def test_a_github_failure_is_one_fixed_line_and_keeps_the_last_value_stale(self):
        self.ops.sweep()
        self.github.fail = RuntimeError(CANARY)
        self.time.now += 4000
        self.ops.sweep()
        value = self.manager.false_incomplete.get("org/name")
        self.assertEqual(value["state"], "stale")
        self.assertEqual(value["counts"]["counted"], fi.MIN_SAMPLE + 1)
        lines = [line for line in self.lines() if line.startswith("false-incomplete repo=org/name")]
        self.assertEqual(lines, ["false-incomplete repo=org/name refresh failed error=RuntimeError"])
        self.assertNotIn(CANARY, json.dumps(value))

    def verdict(self, *jobs, run="r1", repo="org/name"):
        from tests.test_operations import report

        self.runner.reports[(repo, run)] = report(*jobs)


class ReposListingTests(ServerCase):
    def test_get_repos_returns_the_summary_per_repository_and_no_text(self):
        github = FakeGithub()
        for n in range(1, fi.MIN_SAMPLE + 1):
            github.merged(n, head(n), [gate("failure", f"incomplete: {CANARY} [real_failure]")])
        self.manager.false_incomplete.set("org/name", compute(github))
        status, payload, _ = self.call("GET", "/repos", principal=VIEWER)
        self.assertEqual(status, 200)
        by_repo = {item["repo"]: item for item in payload["repos"]}
        value = by_repo["org/name"]["false_incomplete"]
        self.assertEqual(
            set(value),
            {"state", "window", "min_sample", "counts", "rate", "sample", "truncated", "by_reason"},
        )
        self.assertEqual(set(value["window"]), {"from", "to", "days"})
        self.assertEqual(value["rate"], 1.0)
        self.assertEqual(list(value["by_reason"]), list(reason_codes.ORDER))
        self.assertEqual(value["by_reason"]["real_failure"]["counted"], fi.MIN_SAMPLE)
        self.assertEqual(by_repo["org/other"]["false_incomplete"], {"state": "checking"})
        self.assertNotIn(CANARY, json.dumps(payload))
        self.assertTrue(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", value["window"]["from"]))

    def test_a_withheld_rate_is_null_and_flagged(self):
        github = FakeGithub()
        github.merged(1, head(1), [incomplete()])
        self.manager.false_incomplete.set("org/name", compute(github))
        _, payload, _ = self.call("GET", "/repos")
        value = payload["repos"][0]["false_incomplete"]
        self.assertIsNone(value["rate"])
        self.assertEqual(value["sample"], "below_minimum")
        self.assertEqual(value["counts"]["counted"], 1)


if __name__ == "__main__":
    unittest.main()

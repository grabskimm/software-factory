"""src/factory/trigger.py: the pure decisions of label-triggered builds, and the GitHub reader.

The decisions need no threads and no manager. The reader is run against a fake ``gh`` (a script
that serves a small JSON "GitHub" from $FAKE_DIR, pages included) so that the arguments, the
paging, the error handling and the per-repository token are tested for real, without a network.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from factory import shell, trigger
from factory.config import RepoConfig
from factory.trigger import (
    Actor,
    Backoff,
    GithubReader,
    IssueFacts,
    LineLimiter,
    classify,
    job_owning,
    latest_actor,
    level_rank,
    minimum_allowed,
    permission_ok,
    pick,
    plan_issue_numbers,
    run_name,
    settings_of,
)

CANARY = "CANARY-issue-title-and-body-text"
LABEL = "factory-build"


def facts(*labels, number=1):
    return IssueFacts(number, tuple(labels), "2027-01-01T00:00:00Z")


def event(kind, label=LABEL, login="alice", actor="User"):
    return {"event": kind, "label": label, "login": login, "type": actor}


class ClassifyTests(unittest.TestCase):
    def test_a_labelled_issue_without_other_labels_is_eligible(self):
        self.assertEqual(classify(facts(LABEL), LABEL), "eligible")
        self.assertEqual(classify(facts(LABEL, "bug", "help wanted"), LABEL), "eligible")

    def test_an_issue_without_the_label_is_not_triggered(self):
        self.assertIsNone(classify(facts("bug"), LABEL))
        self.assertIsNone(classify(facts(), LABEL))
        self.assertIsNone(classify(facts("factory-builder", "factory"), LABEL))

    def test_the_label_matches_like_the_ignore_label_does(self):
        for name in ("Factory-Build", " factory-build ", "FACTORY-BUILD"):
            self.assertEqual(classify(facts(name), LABEL), "eligible", name)
        self.assertEqual(classify(facts("Ship It"), "ship it"), "eligible")

    def test_ignore_always_wins(self):
        for extra in ("factory-ignore", "Factory-Ignore", " FACTORY-IGNORE "):
            self.assertEqual(classify(facts(LABEL, extra), LABEL), "ignored")
            self.assertEqual(classify(facts(LABEL, extra, "factory:blocked"), LABEL), "ignored")

    def test_any_status_label_means_in_flight_except_the_two_retry_ones(self):
        for status in ("planned", "building", "in-review", "pr-open", "merged"):
            self.assertEqual(classify(facts(LABEL, f"factory:{status}"), LABEL), "in-flight", status)
        self.assertEqual(classify(facts(LABEL, "factory:something-new"), LABEL), "in-flight")
        self.assertEqual(classify(facts(LABEL, "factory:blocked"), LABEL), "retry")
        self.assertEqual(classify(facts(LABEL, "factory:ci-failed"), LABEL), "retry")
        self.assertEqual(classify(facts(LABEL, "Factory:Blocked"), LABEL), "retry")

    def test_a_retry_label_next_to_another_status_label_is_in_flight(self):
        self.assertEqual(classify(facts(LABEL, "factory:blocked", "factory:building"), LABEL), "in-flight")
        self.assertEqual(classify(facts(LABEL, "factory:blocked", "factory:ci-failed"), LABEL), "retry")


class PermissionTests(unittest.TestCase):
    def test_the_order_is_read_triage_write_maintain_admin(self):
        levels = ["read", "triage", "write", "maintain", "admin"]
        self.assertEqual([level_rank(level) for level in levels], [0, 1, 2, 3, 4])

    def test_a_level_must_reach_the_minimum(self):
        for level in ("write", "maintain", "admin"):
            self.assertTrue(permission_ok(level, "write"), level)
        for level in ("read", "triage"):
            self.assertFalse(permission_ok(level, "write"), level)
        self.assertTrue(permission_ok("triage", "triage"))
        self.assertFalse(permission_ok("write", "maintain"))
        self.assertTrue(permission_ok("admin", "admin"))

    def test_anything_unknown_is_refused(self):
        for level in ("none", "", None, "owner", "Write", "custom-role", 3, ["write"]):
            self.assertFalse(permission_ok(level, "read"), repr(level))
        self.assertFalse(permission_ok("admin", "nonsense"))

    def test_below_write_is_for_private_repositories_only(self):
        for minimum in ("write", "maintain", "admin"):
            self.assertTrue(minimum_allowed(minimum, private=False), minimum)
            self.assertTrue(minimum_allowed(minimum, private=True), minimum)
        for minimum in ("read", "triage"):
            self.assertFalse(minimum_allowed(minimum, private=False), minimum)
            self.assertTrue(minimum_allowed(minimum, private=True), minimum)
        self.assertFalse(minimum_allowed("nonsense", private=False))


class ActorTests(unittest.TestCase):
    def test_the_newest_labeled_event_for_the_label_wins(self):
        events = [event("labeled", login="alice"), event("unlabeled", login="alice"), event("labeled", login="bob")]
        self.assertEqual(latest_actor(events, LABEL), Actor("bob", "User"))

    def test_other_labels_and_other_events_do_not_count(self):
        events = [
            event("labeled", login="alice"),
            event("labeled", label="bug", login="mallory"),
            event("assigned", login="mallory"),
            event("unlabeled", login="mallory"),
            "not an event",
            {"event": "labeled"},
        ]
        self.assertEqual(latest_actor(events, LABEL), Actor("alice", "User"))
        self.assertIsNone(latest_actor([event("labeled", label="bug")], LABEL))
        self.assertIsNone(latest_actor([], LABEL))

    def test_the_event_id_tells_a_label_applied_anew_from_the_one_built_for(self):
        first = latest_actor([{**event("labeled", login="alice"), "id": 1001}], LABEL)
        again = latest_actor([{**event("labeled", login="alice"), "id": 1001}, {**event("labeled", login="alice"), "id": 1002}], LABEL)
        self.assertEqual((first.event, again.event), ("1001", "1002"))
        for odd in (None, True, "x y", "../1", -5, 1.5, [1]):
            self.assertEqual(latest_actor([{**event("labeled", login="alice"), "id": odd}], LABEL).event, "", repr(odd))
        self.assertEqual(latest_actor([event("labeled", login="alice")], LABEL).event, "")

    def test_label_names_compare_case_insensitively(self):
        self.assertEqual(latest_actor([event("labeled", label="Factory-Build")], LABEL), Actor("alice", "User"))

    def test_only_a_plain_user_counts(self):
        self.assertTrue(Actor("alice", "User").is_user)
        self.assertTrue(Actor("octo_cat-1", "User").is_user)
        for actor in (Actor("dependabot[bot]", "Bot"), Actor("x", "Organization"), Actor("", "User"),
                      Actor("a b", "User"), Actor("../x", "User"), Actor("alice", ""), Actor("alice[bot]", "User")):
            self.assertFalse(actor.is_user, actor)


class NamingAndPacingTests(unittest.TestCase):
    def test_run_names_are_the_utc_timestamp(self):
        now = 1_800_000_000.0
        expected = "auto-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
        self.assertEqual(run_name(now, lambda name: False), expected)
        self.assertRegex(expected, r"auto-\d{8}T\d{6}Z")

    def test_a_taken_name_gets_a_counter_and_stays_a_valid_run_slug(self):
        from factory.config import RUN_SLUG

        taken = set()
        names = []
        for _ in range(3):
            name = run_name(1_800_000_000.0, taken.__contains__)
            taken.add(name)
            names.append(name)
        self.assertEqual(len(set(names)), 3)
        self.assertTrue(names[1].endswith("-2") and names[2].endswith("-3"))
        for name in names:
            self.assertRegex(name, RUN_SLUG.pattern)

    def test_pick_takes_the_lowest_numbers_and_drops_duplicates(self):
        self.assertEqual(pick([9, 3, 7, 3, 1, 5], 3), [1, 3, 5])
        self.assertEqual(pick([], 3), [])
        self.assertEqual(pick([4], 3), [4])

    def test_backoff_doubles_to_an_hour_and_a_success_forgets_it(self):
        now = [1000.0]
        backoff = Backoff(lambda: now[0])
        self.assertEqual(backoff.remaining("r"), 0)
        delays = [backoff.failed("r") for _ in range(9)]
        self.assertEqual(delays, [60, 120, 240, 480, 960, 1920, 3600, 3600, 3600])
        self.assertEqual(backoff.remaining("r"), 3600)
        now[0] += 3599
        self.assertEqual(backoff.remaining("r"), 1)
        now[0] += 1
        self.assertEqual(backoff.remaining("r"), 0)
        self.assertEqual(backoff.failures("r"), 9)
        self.assertEqual(backoff.remaining("other"), 0)  # per key
        backoff.succeeded("r")
        self.assertEqual((backoff.failures("r"), backoff.remaining("r")), (0, 0))
        self.assertEqual(backoff.failed("r"), 60)

    def test_backoff_cannot_overflow(self):
        backoff = Backoff(lambda: 0.0)
        for _ in range(5000):
            delay = backoff.failed("r")
        self.assertEqual(delay, 3600)

    def test_the_limiter_lets_a_changed_line_through_and_an_unchanged_one_once_an_hour(self):
        now = [0.0]
        limiter = LineLimiter(lambda: now[0])
        self.assertTrue(limiter.allow("k", "busy"))
        self.assertFalse(limiter.allow("k", "busy"))
        self.assertTrue(limiter.allow("other", "busy"))
        self.assertTrue(limiter.allow("k", "unauthorised"))
        self.assertTrue(limiter.allow("k", "busy"))
        now[0] += 3599
        self.assertFalse(limiter.allow("k", "busy"))
        now[0] += 1
        self.assertTrue(limiter.allow("k", "busy"))
        self.assertTrue(limiter.allow("once", "disabled", repeat=trigger.NEVER))
        now[0] += 10**9
        self.assertFalse(limiter.allow("once", "disabled", repeat=trigger.NEVER))

    def test_the_limiter_forgets_by_predicate_and_is_bounded(self):
        limiter = LineLimiter(lambda: 0.0, keep=3)
        for key in range(3):
            limiter.allow(key, "x")
        limiter.forget(lambda key: key == 1)
        self.assertTrue(limiter.allow(1, "x"))
        self.assertFalse(limiter.allow(0, "x"))
        limiter.allow(99, "x")  # full: starts afresh instead of growing
        self.assertLessEqual(len(limiter._last), 3)


class PlanDocumentTests(unittest.TestCase):
    PLAN = {
        "issues": [{"number": 7}, {"number": 9}, {"number": 7}, {"number": "x"}, {"number": -1}, {"number": True}, "n"],
        "jobs": [{"id": "fix-a", "issues": [7]}, {"id": "fix-b", "issues": [9, 11], "context_issues": [3]}, {"id": 4}],
    }

    def test_issue_numbers_are_the_positive_ints_once_each(self):
        self.assertEqual(plan_issue_numbers(self.PLAN), [7, 9])
        for junk in (None, [], "x", {}, {"issues": "x"}, {"issues": None}):
            self.assertEqual(plan_issue_numbers(junk), [])

    def test_the_owning_job_is_found_by_its_issues_not_its_context(self):
        self.assertEqual(job_owning(self.PLAN, 7), "fix-a")
        self.assertEqual(job_owning(self.PLAN, 11), "fix-b")
        self.assertIsNone(job_owning(self.PLAN, 3))
        self.assertIsNone(job_owning(self.PLAN, 99))
        self.assertIsNone(job_owning(None, 7))
        self.assertIsNone(job_owning({"jobs": "x"}, 7))

    def test_a_disposition_is_found_by_its_issue_whatever_its_status(self):
        plan = {
            "jobs": [],
            "dispositions": [
                {"issue": 5, "status": "investigation"},
                {"issue": 6, "status": "blocked"},
                {"issue": "7", "status": "covered"},
                {"issue": True, "status": "covered"},
                "n",
            ],
        }
        self.assertTrue(trigger.has_disposition(plan, 5))
        self.assertTrue(trigger.has_disposition(plan, 6))
        self.assertFalse(trigger.has_disposition(plan, 7))
        self.assertFalse(trigger.has_disposition(plan, 1))  # True is not the number 1
        self.assertFalse(trigger.has_disposition(plan, 99))
        self.assertTrue(trigger.has_disposition(plan, 5, ("investigation", "blocked")))
        self.assertTrue(trigger.has_disposition(plan, 6, ("investigation", "blocked")))
        self.assertFalse(trigger.has_disposition(plan, 6, ("investigation",)))
        self.assertFalse(trigger.has_disposition({"dispositions": [{"issue": 8, "status": "covered"}]}, 8, ("blocked",)))
        for junk in (None, [], "x", {}, {"dispositions": "x"}, {"dispositions": None}):
            self.assertFalse(trigger.has_disposition(junk, 5))

    def test_read_plan_is_tolerant(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "plan.json"
            self.assertIsNone(trigger.read_plan(path))
            path.write_text("{not json")
            self.assertIsNone(trigger.read_plan(path))
            path.write_text("[1]")
            self.assertIsNone(trigger.read_plan(path))
            path.write_text('{"issues": []}')
            self.assertEqual(trigger.read_plan(path), {"issues": []})
            with patch.object(trigger, "MAX_PLAN_BYTES", 4):
                self.assertIsNone(trigger.read_plan(path))


class SettingsTests(unittest.TestCase):
    def test_a_real_config_gives_its_values(self):
        cfg = RepoConfig(
            "acme/widgets", auto_build=False, auto_build_label="ship it", auto_build_min_permission="admin",
            auto_build_concurrency=4, auto_build_max_issues=7,
        )
        self.assertEqual(settings_of(cfg), trigger.TriggerSettings(False, "ship it", "admin", 4, 7))

    def test_the_session_timeout_is_the_configured_one_or_the_default(self):
        self.assertEqual(settings_of(RepoConfig("acme/widgets", auto_build_timeout_seconds=7200)).timeout, 7200)
        self.assertEqual(settings_of(object()).timeout, 1800)
        for odd in (5, 10**6, True, "3600", None):
            with self.subTest(odd=odd):
                self.assertEqual(settings_of(type("Odd", (), {"auto_build_timeout_seconds": odd})()).timeout, 1800)

    def test_a_config_that_carries_nothing_means_watching_with_the_defaults(self):
        self.assertEqual(settings_of(object()), trigger.TriggerSettings(True, "factory-build", "write", 1, 3))

    def test_odd_values_fall_to_the_safe_side(self):
        class Odd:
            auto_build = "yes"
            auto_build_label = "  "
            auto_build_min_permission = "owner"
            auto_build_concurrency = True
            auto_build_max_issues = 10**6

        self.assertEqual(settings_of(Odd()), trigger.TriggerSettings(False, "factory-build", "admin", 1, 3))


# --- the GitHub reader against a fake gh -------------------------------------------

FAKE_GH = r"""
import json, os, pathlib, re, sys, urllib.parse
root = pathlib.Path(os.environ['FAKE_DIR'])
args = sys.argv[1:]
with open(root / 'calls.jsonl', 'a') as log:
    log.write(json.dumps({'args': args, 'token': bool(os.environ.get('GH_TOKEN')),
                          'token_is_repo_a': os.environ.get('GH_TOKEN') == 'token-for-a'}) + '\n')
data = json.loads((root / 'github.json').read_text())

def fail(message, code=1):
    sys.stderr.write(message + '\n')
    sys.exit(code)

if args[:1] != ['api']:
    fail('unexpected command')
writing = args[1:3] == ['-X', 'DELETE']
if not writing:
    if '--jq' not in args:
        fail('every read must project with --jq')
    if re.search(r'title|body|comments', args[args.index('--jq') + 1]):
        fail('the projection must not carry issue text')
repo = os.environ.get('FAKE_REPO', 'acme/widgets')
path = [a for a in args[1:] if a.startswith('repos/')][0]
route, _, query = path.partition('?')
params = dict(urllib.parse.parse_qsl(query))
if data.get('fail_all'):
    fail('HTTP 500 ' + data['fail_all'])

def page(rows):
    size = int(params['per_page'])
    number = int(params['page'])
    for row in rows[(number - 1) * size:number * size]:
        print(json.dumps(row))

if route == f'repos/{repo}':
    print(json.dumps(data['repo']))
elif route == f'repos/{repo}/issues':
    assert params['state'] == 'open', params
    wanted = params['labels'].casefold()
    page([row for row in data['issues'] if any(n.casefold() == wanted for n in row['labels'])])
elif re.fullmatch(re.escape(f'repos/{repo}') + r'/issues/\d+/events', route):
    page(data['events'][route.split('/')[-2]])
elif re.fullmatch(re.escape(f'repos/{repo}') + r'/collaborators/[^/]+/permission', route):
    user = urllib.parse.unquote(route.split('/')[-2])
    if user not in data['permissions']:
        fail('gh: Not Found (HTTP 404)')
    print(data['permissions'][user])
elif re.fullmatch(re.escape(f'repos/{repo}') + r'/pulls/\d+', route):
    pull = data['pulls'].get(route.split('/')[-1])
    if pull is None:
        fail('gh: Not Found (HTTP 404)')
    print(json.dumps(pull))
elif re.fullmatch(re.escape(f'repos/{repo}') + r'/issues/\d+/labels/.+', route):
    assert args[1:3] == ['-X', 'DELETE'], args
    name = urllib.parse.unquote(route.split('/labels/')[1])
    data.setdefault('removed', []).append([route.split('/')[-3], name])
    (root / 'github.json').write_text(json.dumps(data))
    if data.get('label_missing'):
        fail('gh: Not Found (HTTP 404)')
    if data.get('label_forbidden'):
        fail('gh: Resource not accessible by integration (HTTP 403) ' + data['label_forbidden'])
else:
    fail('fake gh: unhandled ' + route)
"""


class FakeAuth:
    def __init__(self, token="token-for-a"):
        self.token = token

    def env_for_supervisor_subprocess(self):
        return {"GH_TOKEN": self.token}

    def commit_identity(self):
        return None


class Reader(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text(f"#!{sys.executable}\n" + FAKE_GH)
        gh.chmod(0o755)
        patcher = patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_DIR": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.data = {
            "repo": {"archived": False, "private": True, "visibility": "private"},
            "issues": [], "events": {}, "permissions": {},
        }
        self.reader = GithubReader("acme/widgets", FakeAuth(), self.root)

    def save(self):
        (self.root / "github.json").write_text(json.dumps(self.data))

    def calls(self):
        path = self.root / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def issue(self, number, *labels, pr=False):
        return {"number": number, "pr": pr, "labels": list(labels), "updated": f"2027-01-{number % 28 + 1:02d}T00:00:00Z"}

    # -- repository

    def test_repo_facts(self):
        self.save()
        self.assertEqual(self.reader.repo_facts(), trigger.RepoFacts(archived=False, private=True))
        for repo, expected in (
            ({"archived": True, "private": True, "visibility": "private"}, (True, True)),
            ({"archived": False, "private": False, "visibility": "public"}, (False, False)),
            ({"archived": False, "private": True, "visibility": "internal"}, (False, False)),
            ({"archived": False, "private": None, "visibility": None}, (False, False)),
            ({"archived": None, "private": True, "visibility": "private"}, (True, True)),
            ({"archived": False, "private": True}, (False, True)),
            ({"archived": False}, (False, False)),
        ):
            self.data["repo"] = repo
            self.save()
            got = self.reader.repo_facts()
            self.assertEqual((got.archived, got.private), expected, repo)

    # -- pull requests

    def test_pull_state_reads_merged_closed_and_open_through_a_projection(self):
        self.data["pulls"] = {
            "1": {"state": "closed", "merged": True}, "2": {"state": "closed", "merged": False},
            "3": {"state": "open", "merged": False}, "4": {"state": "weird", "merged": False},
        }
        self.save()
        self.assertEqual([self.reader.pull_state(n) for n in (1, 2, 3)], ["merged", "closed", "open"])
        with self.assertRaises(ValueError):
            self.reader.pull_state(4)
        with self.assertRaises(subprocess.CalledProcessError):
            self.reader.pull_state(9)
        args = self.calls()[0]["args"]
        self.assertIn("repos/acme/widgets/pulls/1", args)
        self.assertNotIn("title", args[args.index("--jq") + 1])

    # -- issues

    def test_open_issues_returns_only_issues_with_the_label_and_never_pull_requests(self):
        self.data["issues"] = [
            self.issue(1, LABEL, "bug"), self.issue(2, "bug"), self.issue(3, LABEL, pr=True), self.issue(4, "Factory-Build"),
        ]
        self.save()
        found = self.reader.open_issues(LABEL)
        self.assertEqual([item.number for item in found], [1, 4])
        self.assertEqual(found[0].labels, (LABEL, "bug"))
        self.assertTrue(found[0].updated.startswith("2027-"))

    def test_issue_facts_reads_the_labels_and_the_update_of_one_issue_through_a_projection(self):
        answer = json.dumps({"labels": ["bug", LABEL], "updated": "2027-01-02T00:00:00Z"})
        with patch.object(self.reader, "_gh", return_value=answer) as gh:
            found = self.reader.issue_facts(7)
        self.assertEqual(found, trigger.IssueFacts(7, ("bug", LABEL), "2027-01-02T00:00:00Z"))
        args = gh.call_args[0][0]
        self.assertIn("repos/acme/widgets/issues/7", args)
        self.assertNotIn("title", args[args.index("--jq") + 1])
        self.assertNotIn("body", args[args.index("--jq") + 1])
        for odd in ("[]", json.dumps({"labels": "x", "updated": "t"}), json.dumps({"labels": [], "updated": None})):
            with patch.object(self.reader, "_gh", return_value=odd), self.assertRaises(ValueError):
                self.reader.issue_facts(7)

    def test_open_issues_pages_and_encodes_the_label(self):
        self.data["issues"] = [self.issue(number, "ship it") for number in range(1, 251)]
        self.save()
        found = self.reader.open_issues("ship it")
        self.assertEqual(len(found), 250)
        paths = [next(a for a in call["args"] if a.startswith("repos/")) for call in self.calls()]
        self.assertEqual(len(paths), 3)
        self.assertTrue(all("labels=ship%20it" in path for path in paths))
        self.assertEqual([path.rsplit("page=", 1)[1] for path in paths], ["1", "2", "3"])

    def test_an_exact_page_boundary_costs_one_more_empty_page_and_no_more(self):
        self.data["issues"] = [self.issue(number, LABEL) for number in range(1, 101)]
        self.save()
        self.assertEqual(len(self.reader.open_issues(LABEL)), 100)
        self.assertEqual(len(self.calls()), 2)

    def test_the_issue_listing_is_bounded(self):
        self.data["issues"] = [self.issue(number, LABEL) for number in range(1, 1500)]
        self.save()
        with patch.object(trigger, "MAX_ISSUE_PAGES", 2):
            self.assertEqual(len(self.reader.open_issues(LABEL)), 200)

    # -- who applied the label

    def events(self, number, rows):
        self.data["events"][str(number)] = rows
        self.save()

    def test_the_actor_is_the_newest_labeler(self):
        self.events(5, [event("labeled", login="alice"), event("unlabeled", login="alice"), event("labeled", login="bob")])
        self.assertEqual(self.reader.label_actor(5, LABEL), Actor("bob", "User"))

    def test_no_event_no_actor(self):
        self.events(5, [event("labeled", label="bug")])
        self.assertIsNone(self.reader.label_actor(5, LABEL))
        self.events(6, [])
        self.assertIsNone(self.reader.label_actor(6, LABEL))

    def test_the_actor_is_found_on_a_later_page(self):
        rows = [event("commented", label="", login="chatty") for _ in range(250)]
        rows[40] = event("labeled", login="early")
        rows[230] = event("labeled", login="late")
        self.events(8, rows)
        self.assertEqual(self.reader.label_actor(8, LABEL), Actor("late", "User"))
        pages = [next(a for a in call["args"] if a.startswith("repos/")).rsplit("page=", 1)[1] for call in self.calls()]
        self.assertEqual(pages, ["1", "2", "3"])

    def test_the_actor_is_found_when_the_events_end_exactly_on_a_page(self):
        rows = [event("commented", label="") for _ in range(199)] + [event("labeled", login="last")]
        self.events(8, rows)
        self.assertEqual(self.reader.label_actor(8, LABEL), Actor("last", "User"))

    def test_too_many_events_fail_closed(self):
        self.events(9, [event("labeled", login="alice")] + [event("commented", label="") for _ in range(1000)])
        with patch.object(trigger, "MAX_EVENT_PAGES", 3):
            self.assertIsNone(self.reader.label_actor(9, LABEL))
        self.assertEqual(self.reader.label_actor(9, LABEL), Actor("alice", "User"))  # the real cap is far larger

    def test_a_bot_is_reported_as_a_bot(self):
        self.events(5, [event("labeled", login="github-actions[bot]", actor="Bot")])
        actor = self.reader.label_actor(5, LABEL)
        self.assertEqual((actor.login, actor.kind, actor.is_user), ("github-actions[bot]", "Bot", False))

    def test_several_labels_of_one_issue_cost_one_read_of_its_events(self):
        self.events(5, [event("labeled", label="a", login="alice"), event("labeled", label="b", login="bob"), event("labeled", label="a", login="carol")])
        found = self.reader.label_actors(5, ["a", "B", "missing"])
        self.assertEqual(found, {"a": Actor("carol", "User"), "B": Actor("bob", "User"), "missing": None})
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.reader.label_actors(5, []), {})

    def test_the_actors_of_too_many_events_fail_closed(self):
        self.events(9, [event("labeled", login="alice")] + [event("commented", label="") for _ in range(1000)])
        with patch.object(trigger, "MAX_EVENT_PAGES", 3):
            self.assertEqual(self.reader.label_actors(9, [LABEL]), {LABEL: None})
        self.assertEqual(self.reader.label_actors(9, [LABEL]), {LABEL: Actor("alice", "User")})

    # -- who may choose what a build uses

    def authority(self, minimum="write"):
        return trigger.LabelAuthority(self.reader, minimum)

    def roles(self, **roles):
        self.data["permissions"] = roles
        self.save()

    def test_a_label_counts_when_its_newest_applier_is_a_user_who_may_start_a_build(self):
        self.roles(alice="write", bob="maintain", carol="admin", dave="triage", erin="read", frank="none", gina="custom-role")
        for login, allowed in (("alice", True), ("bob", True), ("carol", True), ("dave", False), ("erin", False), ("frank", False), ("gina", False), ("ghost", False)):
            with self.subTest(login):
                self.events(5, [event("labeled", login="mallory"), event("labeled", label="x", login=login)])
                self.assertIs(self.authority()(5, ["x"]), allowed)

    def test_the_newest_applier_is_the_one_asked_about(self):
        self.roles(alice="write", dave="triage")
        self.events(5, [event("labeled", label="x", login="alice"), event("unlabeled", label="x", login="alice"), event("labeled", label="x", login="dave")])
        self.assertFalse(self.authority()(5, ["x"]))
        self.events(5, [event("labeled", label="x", login="dave"), event("unlabeled", label="x", login="dave"), event("labeled", label="x", login="alice")])
        self.assertTrue(self.authority()(5, ["x"]))

    def test_every_label_asked_about_must_pass_and_none_at_all_is_not_a_pass_for_a_missing_event(self):
        self.roles(alice="write", dave="triage")
        self.events(5, [event("labeled", label="x", login="alice"), event("labeled", label="y", login="alice"), event("labeled", label="z", login="dave")])
        self.assertTrue(self.authority()(5, ["x", "y"]))
        self.assertFalse(self.authority()(5, ["x", "y", "z"]))
        self.assertFalse(self.authority()(5, ["x", "never-applied"]))

    def test_a_bot_and_too_many_events_are_never_enough(self):
        self.roles(alice="write", **{"github-actions[bot]": "write"})
        self.events(5, [event("labeled", label="x", login="github-actions[bot]", actor="Bot")])
        self.assertFalse(self.authority()(5, ["x"]))
        self.events(6, [event("labeled", label="x", login="alice")] + [event("commented", label="") for _ in range(1000)])
        with patch.object(trigger, "MAX_EVENT_PAGES", 3):
            self.assertFalse(self.authority()(6, ["x"]))
        self.assertTrue(self.authority()(6, ["x"]))

    def test_the_minimum_is_the_repositorys_and_below_write_needs_a_private_repository(self):
        self.roles(dave="triage", alice="write")
        self.events(5, [event("labeled", label="x", login="dave")])
        self.assertFalse(self.authority("write")(5, ["x"]))
        self.assertTrue(self.authority("triage")(5, ["x"]))  # private
        self.data["repo"] = {"archived": False, "private": False, "visibility": "public"}
        self.save()
        self.assertFalse(self.authority("triage")(5, ["x"]))
        self.events(6, [event("labeled", label="x", login="alice")])
        self.assertTrue(self.authority("write")(6, ["x"]))
        self.assertFalse(self.authority("not-a-level")(6, ["x"]))
        self.assertFalse(self.authority("admin")(6, ["x"]))

    def test_the_repository_and_each_role_are_read_once_per_instance(self):
        self.roles(alice="write")
        self.events(5, [event("labeled", label="x", login="alice")])
        self.events(6, [event("labeled", label="x", login="alice")])
        check = self.authority()
        self.assertTrue(check(5, ["x"]) and check(6, ["x"]))
        paths = [next(a for a in call["args"] if a.startswith("repos/")) for call in self.calls()]
        self.assertEqual(sum(path == "repos/acme/widgets" for path in paths), 1)
        self.assertEqual(sum(path.endswith("/permission") for path in paths), 1)
        self.assertEqual(sum(path.endswith("/events?per_page=100&page=1") for path in paths), 2)

    def test_a_failed_read_raises_a_fixed_message_without_the_error(self):
        self.events(5, [event("labeled", label="x", login="alice")])
        self.data["fail_all"] = "SECRET-FROM-GH"
        self.save()
        with self.assertRaises(RuntimeError) as caught:
            self.authority()(5, ["x"])
        self.assertEqual(str(caught.exception), trigger.AUTHORITY_UNKNOWN)
        self.assertNotIn("SECRET-FROM-GH", f"{caught.exception!r} {caught.exception.__cause__!r}")

    # -- permission

    def test_permission_reads_the_role(self):
        self.data["permissions"] = {"alice": "maintain", "bob": "triage", "ghost": "none"}
        self.save()
        self.assertEqual(self.reader.permission("alice"), "maintain")
        self.assertEqual(self.reader.permission("bob"), "triage")
        self.assertEqual(self.reader.permission("ghost"), "none")

    def test_a_user_gh_does_not_know_has_no_permission(self):
        self.save()
        self.assertEqual(self.reader.permission("stranger"), "none")

    def test_a_login_that_is_not_a_login_is_never_sent(self):
        self.save()
        for login in ("", "a/b", "../x", "a b", "x?y=1", "x\n", "-x"):
            self.assertEqual(self.reader.permission(login), "none", repr(login))
        self.assertEqual(self.calls(), [])

    def test_other_errors_are_raised_not_read_as_no_permission(self):
        self.data["fail_all"] = "boom"
        self.save()
        with self.assertRaises(subprocess.CalledProcessError):
            self.reader.permission("alice")

    def test_only_a_404_status_means_not_found_not_a_404_somewhere_in_the_message(self):
        # a repository called n404: a bad gateway whose URL holds "404" is an error, not "no such user"
        self.data["fail_all"] = "https://api.github.com/repos/o/n404/collaborators"
        self.save()
        with self.assertRaises(subprocess.CalledProcessError):
            self.reader.permission("alice")
        with self.assertRaises(subprocess.CalledProcessError):
            self.reader.remove_label(12, LABEL)
        error = subprocess.CalledProcessError(1, ["gh"], stderr="gh: Not Found (HTTP 404)")
        self.assertTrue(trigger._is_missing(error))
        for text in ("HTTP 502: Bad Gateway (https://api.github.com/repos/o/n404/x)", "Not Found", "HTTP 4040", "", None):
            self.assertFalse(trigger._is_missing(subprocess.CalledProcessError(1, ["gh"], stderr=text)), repr(text))

    def test_is_gone_is_a_definite_not_found_or_gone_status_and_nothing_else(self):
        def failed(text):
            return subprocess.CalledProcessError(1, ["gh"], stderr=text)

        for text in ("gh: Not Found (HTTP 404)", "gh: Gone (HTTP 410)"):
            self.assertTrue(trigger.is_gone(failed(text)), text)
        for text in ("gh: Bad Gateway (HTTP 502)", "gh: Forbidden (HTTP 403)", "HTTP 4040", "HTTP 4100", "Not Found", "", None):
            self.assertFalse(trigger.is_gone(failed(text)), repr(text))
        for other in (RuntimeError("HTTP 404"), subprocess.TimeoutExpired(["gh"], 30), ValueError()):
            self.assertFalse(trigger.is_gone(other), type(other).__name__)  # only a failed ``gh`` command carries a status

    # -- removing the label

    def test_the_label_is_removed_with_a_delete_and_a_quoted_name(self):
        self.save()
        self.reader.remove_label(12, "ship it")
        self.assertEqual(json.loads((self.root / "github.json").read_text())["removed"], [["12", "ship it"]])
        call = self.calls()[-1]["args"]
        self.assertEqual(call[:3], ["api", "-X", "DELETE"])
        self.assertTrue(call[3].endswith("/issues/12/labels/ship%20it"))

    def test_a_label_that_is_already_gone_is_fine_but_a_refusal_is_not(self):
        self.data["label_missing"] = True
        self.save()
        self.reader.remove_label(12, LABEL)
        self.data["label_missing"] = False
        self.data["label_forbidden"] = CANARY
        self.save()
        with self.assertRaises(subprocess.CalledProcessError):
            self.reader.remove_label(12, LABEL)

    # -- one token per repository, and no issue text

    def test_the_repositorys_own_auth_is_used_for_every_call(self):
        self.save()
        self.reader.repo_facts()
        self.assertTrue(all(call["token_is_repo_a"] for call in self.calls()))
        other = GithubReader("acme/widgets", FakeAuth("token-for-b"), self.root)
        other.repo_facts()
        self.assertFalse(self.calls()[-1]["token_is_repo_a"])
        self.assertTrue(self.calls()[-1]["token"])

    def test_the_ambient_login_adds_no_token(self):
        self.save()
        with patch.dict(os.environ):
            os.environ.pop("GH_TOKEN", None)
            GithubReader("acme/widgets", None, self.root).repo_facts()
        self.assertFalse(self.calls()[-1]["token"])

    def test_every_read_projects_with_jq_and_the_projection_carries_no_issue_text(self):
        # The fake gh refuses a call without --jq or whose projection names title, body or comments.
        self.data["issues"] = [self.issue(1, LABEL)]
        self.data["permissions"] = {"alice": "write"}
        self.events(1, [event("labeled")])
        self.reader.repo_facts()
        self.reader.open_issues(LABEL)
        self.reader.label_actor(1, LABEL)
        self.reader.permission("alice")
        self.assertEqual(len(self.calls()), 4)

    def test_a_failing_gh_surfaces_as_an_error_with_no_text_of_its_own(self):
        self.data["fail_all"] = CANARY
        self.save()
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            self.reader.repo_facts()
        self.assertNotIn(CANARY, str(caught.exception))  # only the stderr attribute carries it


class ShellAuthTests(unittest.TestCase):
    """shell.command takes the identity per call, so one process can serve several repositories."""

    def test_an_explicit_auth_replaces_the_process_wide_one_for_that_call_only(self):
        with patch.dict(os.environ, {"GH_TOKEN": "ambient"}):
            self.assertEqual(shell.supervisor_env(FakeAuth("one"))["GH_TOKEN"], "one")
            self.assertEqual(shell.supervisor_env(None)["GH_TOKEN"], "ambient")
            with shell.using_auth(FakeAuth("process")):
                self.assertEqual(shell.supervisor_env()["GH_TOKEN"], "process")
                self.assertEqual(shell.supervisor_env(FakeAuth("one"))["GH_TOKEN"], "one")
                self.assertEqual(shell.supervisor_env(None)["GH_TOKEN"], "ambient")
            self.assertEqual(shell.supervisor_env()["GH_TOKEN"], "ambient")


if __name__ == "__main__":
    unittest.main()

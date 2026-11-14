"""The read-only @mention follow-up pass (src/factory/followup.py, docs/manager.md "Follow-up requests").

A fake ``gh`` (an injected ``run`` for ``trigger.GithubReader``) keeps pull requests, their comments of the three
kinds, and what each user may do. It pages like GitHub, and it applies the ``--jq`` projection the reader sends
(it evaluates the mention test from the pattern inside the jq text), so a comment text never leaves it. It raises when
a closed pull request, or one the Factory App did not author, has its comments read. The pass runs on a real
``Operations`` over a real data directory; the clock never moves unless a test moves it.
"""

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from factory import followup, trigger
from factory.followup import FollowupSettings, StateError
from factory.manager import Manager
from factory.operations import Operations
from tests.test_operations import CANARY, NOW, OpsCase

REPO = "org/name"
SLUG = "factory-bot"
APP = f"{SLUG}[bot]"
PR = 12


def ago(seconds):
    """An ISO time ``seconds`` before the test clock: comments must be younger than the age bound to be decided."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - seconds))


OLD = ago(3600)  # what the fixtures call 10:00
LATER = ago(1800)  # and 11:00


def called_process_error(message, args=()):
    return subprocess.CalledProcessError(1, list(args), stderr=message)


class FakeGh:
    """``run(argv, cwd, timeout=, auth=)`` of a repository's GitHub, for ``GithubReader``."""

    def __init__(self):
        self.pulls = {PR: {"state": "open", "login": APP, "type": "Bot"}}
        self.comments = {}  # (kind, pr) -> [{"id", "login", "type", "updated", "body"}]
        self.permissions = {"alice": "write", "bob": "triage", "mallory": "read", "carol": "admin", "dave": "maintain"}
        self.calls = []
        self.permission_error = None
        self.fail_listing = None
        self.author = APP  # the account whose pull requests are followed (a user account for ``mention_handle``)

    def add(self, kind, ident, login, body, *, pr=PR, type="User", updated=OLD, created=None):
        self.comments.setdefault((kind, pr), []).append(
            {"id": ident, "login": login, "type": type, "updated": updated, "body": body, "created": created or updated}
        )

    def edit(self, kind, ident, *, pr=PR, updated=LATER, body=None):
        for row in self.comments[(kind, pr)]:
            if row["id"] == ident:
                row["updated"] = updated
                if body is not None:
                    row["body"] = body

    def paths_read(self):
        return [call for call in self.calls if "/comments" in call or "/reviews" in call]

    def __call__(self, argv, cwd, timeout=None, auth=None):
        assert argv[:2] == ["gh", "api"], argv
        assert "--jq" in argv, "every read must project with --jq"
        jq = argv[argv.index("--jq") + 1]
        route, _, query = [a for a in argv[2:] if a.startswith("repos/")][0].partition("?")
        self.calls.append(route)
        params = dict(item.split("=") for item in query.split("&")) if query else {}
        assert route.startswith(f"repos/{REPO}"), route
        rest = route[len(f"repos/{REPO}"):]
        if self.fail_listing and rest == "/pulls":
            raise called_process_error("gh: server error (HTTP 500)")
        if rest == "":
            return json.dumps({"archived": False, "private": True, "visibility": "private"})
        if rest == "/pulls":
            assert params["state"] == "open"
            rows = [
                {"number": n, "state": p["state"], "login": p["login"], "type": p["type"], "updated": p.get("updated", "")}
                for n, p in sorted(self.pulls.items(), reverse=True)
                if p["state"] == "open"
            ]
            return self.page(rows, params)
        found = re.fullmatch(r"/(issues|pulls)/(\d+)/(comments|reviews)", rest)
        if found:
            kind = {"issues/comments": "issue", "pulls/comments": "review_comment", "pulls/reviews": "review"}[
                f"{found[1]}/{found[3]}"
            ]
            number = int(found[2])
            pull = self.pulls.get(number)
            assert pull is not None and pull["state"] == "open" and pull["login"].casefold() == self.author.casefold(), (
                f"comments of pull request {number} must not be read"
            )
            pattern = json.loads(re.search(r'test\(("(?:[^"\\]|\\.)*"); "i"\)', jq)[1])
            since = urllib.parse.unquote(params["since"]) if "since" in params else None
            rows = [
                {
                    "id": row["id"], "login": row["login"], "type": row["type"], "updated": row["updated"],
                    "created": row.get("created", row["updated"]),
                    "mention": re.search(pattern, row["body"], re.I) is not None,
                }
                for row in self.comments.get((kind, number), [])
                if since is None or kind == "review" or row["updated"] >= since  # the reviews list has no ``since``
            ]
            return self.page(rows, params)
        found = re.fullmatch(r"/collaborators/([^/]+)/permission", rest)
        if found:
            if self.permission_error:
                raise called_process_error("gh: server error (HTTP 500)")
            user = found[1].casefold()  # logins are case-insensitive
            if user not in self.permissions:
                raise called_process_error("gh: Not Found (HTTP 404)")
            return self.permissions[user]
        raise AssertionError("fake gh: unhandled " + route)

    @staticmethod
    def page(rows, params):
        size, number = int(params["per_page"]), int(params["page"])
        return "\n".join(json.dumps(row) for row in rows[(number - 1) * size : number * size])


class FakeAuth:
    def env_for_supervisor_subprocess(self):
        return {}

    def commit_identity(self):
        return None


class FollowupCase(OpsCase):
    env = {"FACTORY_GITHUB_APP_SLUG": SLUG}

    def setUp(self):
        super().setUp()
        self.gh = FakeGh()
        self.configure()
        self.install()

    def install(self):
        self.manager = Manager(
            self.manager.settings, env=dict(self.env), runner=self.runner, spawner=self.spawner,
            lock_factory=self.lease, stdout=self.out, stderr=self.err,
        )
        reader = trigger.GithubReader(REPO, FakeAuth(), self.data_dir, run=self.gh)
        self.ops = self.new_ops(self.manager, github=lambda cfg: reader)
        # This module is about recording the requests; serving them is tests/test_followup_serve.py.
        self.ops._serving.serve = lambda *args: None

    def configure(self, extra=""):
        """The config of org/name: follow-ups are off by default, so a test opts in unless it sets ``followup`` itself."""
        opt_in = "" if "followup =" in extra else "followup = true\n"
        (self.config_dir / "repos" / "org__name.toml").write_text(f'repo = "{REPO}"\n{opt_in}{extra}')

    def run_pass(self):
        self.ops._followup_repo(REPO)

    def followup_lines(self):
        return [line for line in self.lines() if line.startswith("followup ")]

    @property
    def root(self):
        return self.data_dir / "repos" / "org" / "name"

    def make_state_dir(self):
        (self.root / "followups").mkdir(parents=True, mode=0o700)
        (self.root / "followups").chmod(0o700)

    def state_path(self, number=PR):
        return self.root / "followups" / f"pr-{number}.json"

    def state(self, number=PR):
        return json.loads(self.state_path(number).read_text())

    def queued(self, number=PR):
        return [item["comment_id"] for item in self.state(number)["requests"]]


QUEUED = f"followup repo={REPO} pr={PR} request queued"


def skipped(reason, number=PR):
    return f"followup repo={REPO} pr={number} skipped reason={reason}"


class Queueing(FollowupCase):
    def test_an_authorised_mention_queues_exactly_one_request_and_logs_it(self):
        self.gh.add("issue", 101, "alice", f"@{SLUG} please fix the typo")
        self.run_pass()
        self.assertEqual(self.followup_lines(), [QUEUED])
        state = self.state()
        self.assertEqual(
            state["requests"],
            [{"pr": PR, "kind": "issue", "comment_id": 101, "user": "alice", "permission": "write",
              "queued_at": "2027-01-15T08:00:00Z"}],
        )
        self.assertEqual((state["cursors"]["issue"], state["sessions"], state["max_sessions"], state["lock"]), (101, 1, 5, None))
        self.assertEqual(state["fingerprints"], {"issue:101": OLD})
        self.run_pass()  # nothing is decided twice
        self.assertEqual(self.followup_lines(), [QUEUED])
        self.assertEqual(self.queued(), [101])
        self.assertEqual(self.spawner.calls, [])  # no session is started

    def test_write_maintain_and_admin_are_authorised_on_every_kind_of_comment(self):
        self.configure("followup_cooldown_seconds = 0\n")
        self.gh.add("issue", 1, "alice", f"@{SLUG} a")
        self.gh.add("review_comment", 2, "dave", f"@{SLUG} b")
        self.gh.add("review", 3, "carol", f"@{SLUG} c")
        self.run_pass()
        self.assertEqual(self.followup_lines(), [QUEUED] * 3)
        self.assertEqual(
            [(r["kind"], r["comment_id"], r["permission"]) for r in self.state()["requests"]],
            [("issue", 1, "write"), ("review_comment", 2, "maintain"), ("review", 3, "admin")],
        )

    def test_the_pass_is_one_line_per_decision_in_a_fixed_format(self):
        self.configure("followup_cooldown_seconds = 0\n")
        for number, who, body in ((1, "alice", f"@{SLUG}"), (2, "mallory", f"@{SLUG}"), (3, "alice", "hello")):
            self.gh.add("issue", number, who, body)
        self.run_pass()
        self.assertEqual(self.followup_lines(), [QUEUED, skipped("unauthorised"), skipped("no-mention")])
        shape = re.compile(r"followup repo=[A-Za-z0-9._/-]+ pr=\d+ (request queued|skipped reason=[a-z-]+)")
        self.assertTrue(all(shape.fullmatch(line) for line in self.followup_lines()))


class Skipping(FollowupCase):
    def test_a_read_only_commenter_is_unauthorised_and_so_is_an_account_without_a_role(self):
        self.gh.add("issue", 1, "mallory", f"@{SLUG} do it")
        self.gh.add("issue", 2, "bob", f"@{SLUG} do it")  # triage is below write
        self.gh.add("issue", 3, "stranger", f"@{SLUG} do it")  # 404
        self.run_pass()
        self.assertEqual(self.followup_lines(), [skipped("unauthorised")] * 3)
        self.assertEqual(self.queued(), [])

    def test_bots_are_ignored_the_factorys_own_comments_included_without_a_permission_lookup(self):
        self.gh.add("issue", 1, "dependabot[bot]", f"@{SLUG} hi", type="Bot")
        self.gh.add("issue", 2, APP, f"reply with @{SLUG}", type="Bot")
        self.gh.add("review", 3, "lookalike[bot]", f"@{SLUG}", type="User")  # a login ending in [bot] is a bot too
        self.run_pass()
        self.assertEqual(self.followup_lines(), [skipped("bot")] * 3)
        self.assertFalse([call for call in self.gh.calls if "collaborators" in call])

    def test_a_comment_without_a_whole_token_mention_is_skipped(self):
        for number, body in enumerate(
            ("nothing", f"mail me@{SLUG}.example", f"@{SLUG}-two do this", f"x@{SLUG}", f"@{SLUG}/team", ""), start=1
        ):
            self.gh.add("issue", number, "alice", body)
        self.run_pass()
        self.assertEqual(self.followup_lines(), [skipped("no-mention")] * 6)
        self.assertEqual(self.queued(), [])

    def test_mentions_are_whole_tokens_in_any_case(self):
        for number, body in enumerate(
            (f"@{SLUG.upper()} go", f"(@{SLUG})", f"hello\n@{SLUG}, please", f"@{SLUG}.", f"@{SLUG}"), start=1
        ):
            self.assertRegex(body, re.compile(trigger.mention_pattern(SLUG), re.I), body)
        with self.assertRaises(ValueError):
            trigger.mention_pattern("a b")
        with self.assertRaises(ValueError):
            trigger.mention_pattern('a")|(')

    def test_an_edit_of_an_already_seen_comment_never_triggers(self):
        self.configure("followup_cooldown_seconds = 0\n")
        self.gh.add("issue", 1, "alice", "just a note")
        self.gh.add("issue", 2, "alice", f"@{SLUG} first")
        self.run_pass()
        self.assertEqual(self.followup_lines(), [skipped("no-mention"), QUEUED])
        self.gh.edit("issue", 1, body=f"@{SLUG} now with a mention")
        self.gh.edit("issue", 2)
        self.run_pass()
        self.assertEqual(self.followup_lines()[2:], [skipped("edited")] * 2)
        self.assertEqual(self.queued(), [2])
        self.run_pass()  # the new fingerprints are kept: each edit is reported once
        self.assertEqual(len(self.followup_lines()), 4)
        self.assertEqual(self.state()["fingerprints"]["issue:1"], LATER)

    def test_a_review_comment_and_a_review_with_an_id_below_the_cursor_are_new(self):
        # ids of a pending review are assigned when it is started, so it can be submitted after later comments
        self.configure("followup_cooldown_seconds = 0\n")
        self.gh.add("review_comment", 50, "bob", "looks fine")
        self.gh.add("review", 60, "bob", "ok")
        self.run_pass()
        self.assertEqual(self.followup_lines(), [skipped("no-mention")] * 2)
        self.assertEqual(self.state()["cursors"]["review_comment"], 50)
        self.gh.add("review_comment", 40, "alice", f"@{SLUG} please fix this line")  # draft started before id 50
        self.gh.add("review", 55, "carol", f"@{SLUG} please fix the rest")
        self.run_pass()
        self.assertEqual(self.followup_lines()[2:], [QUEUED, QUEUED])
        self.assertEqual(
            [(r["kind"], r["comment_id"]) for r in self.state()["requests"]], [("review_comment", 40), ("review", 55)]
        )
        self.assertEqual(self.state()["cursors"]["review_comment"], 50)  # the cursor never moves back
        self.run_pass()  # decided once: no duplicate
        self.assertEqual(len(self.followup_lines()), 4)

    def test_a_late_comment_below_the_cursor_gets_every_decision_and_its_line(self):
        self.gh.add("review_comment", 50, "bob", "x")
        self.gh.add("review", 50, "bob", "x")
        self.run_pass()
        self.assertEqual(self.followup_lines(), [skipped("no-mention")] * 2)
        self.gh.add("review_comment", 20, "mallory", f"@{SLUG} a")
        self.gh.add("review_comment", 21, "dependabot[bot]", f"@{SLUG} b", type="Bot")
        self.gh.add("review_comment", 22, "alice", "no mention")
        self.run_pass()
        self.assertEqual(
            self.followup_lines()[2:],
            [skipped("unauthorised"), skipped("bot"), skipped("no-mention")],
        )
        self.assertEqual(self.state()["requests"], [])

    def test_only_ids_at_or_below_the_floor_are_old(self):
        self.gh.add("review_comment", 50, "bob", "x")
        self.run_pass()
        state = self.state()
        state["floors"]["review_comment"] = 30  # fingerprints up to id 30 were forgotten
        self.state_path().write_text(json.dumps(state))
        os.chmod(self.state_path(), 0o600)
        self.gh.add("review_comment", 30, "alice", f"@{SLUG} old")
        self.gh.add("review_comment", 31, "alice", f"@{SLUG} new")
        self.run_pass()
        self.assertEqual(self.queued(), [31])

    def test_forgetting_fingerprints_raises_the_floor_and_the_forgotten_stay_old(self):
        state = followup.new_state(PR)
        state["fingerprints"] = {f"issue:{n}": OLD + "x" * 80 for n in range(1, 4000)}
        state["cursors"]["issue"] = 3999
        followup.save_state(self.root, PR, state)
        saved = followup.load_state(self.root / "followups", PR)
        floor = saved["floors"]["issue"]
        self.assertGreater(floor, 0)
        self.assertNotIn(f"issue:{floor}", saved["fingerprints"])
        self.assertIn(f"issue:{floor + 1}", saved["fingerprints"])
        self.gh.add("issue", floor, "alice", f"@{SLUG} forgotten")
        self.run_pass()
        self.assertEqual(self.followup_lines(), [])
        self.assertEqual(self.state()["requests"], [])

    def test_an_edit_that_adds_no_mention_is_silent(self):
        self.gh.add("issue", 1, "alice", "note")
        self.run_pass()
        self.gh.edit("issue", 1, body="other note")
        self.run_pass()
        self.assertEqual(self.followup_lines(), [skipped("no-mention")])

    def test_the_per_user_cooldown(self):
        self.gh.add("issue", 1, "alice", f"@{SLUG} one")
        self.gh.add("issue", 2, "alice", f"@{SLUG} two")
        self.gh.add("issue", 3, "carol", f"@{SLUG} three")  # another user: not held back
        self.run_pass()
        self.assertEqual(self.followup_lines(), [QUEUED, skipped("cooldown"), QUEUED])
        self.time.now += 299
        self.gh.add("issue", 4, "alice", f"@{SLUG} four")
        self.run_pass()
        self.assertEqual(self.followup_lines()[3:], [skipped("cooldown")])
        self.time.now += 2  # 301 seconds after the first request: the default cooldown is five minutes
        self.gh.add("issue", 5, "alice", f"@{SLUG} five")
        self.run_pass()
        self.assertEqual(self.followup_lines()[4:], [QUEUED])
        self.assertEqual(self.queued(), [1, 3, 5])

    def test_the_cooldown_is_configurable(self):
        self.configure("followup_cooldown_seconds = 30\n")
        self.gh.add("issue", 1, "alice", f"@{SLUG} one")
        self.run_pass()
        self.time.now += 31
        self.gh.add("issue", 2, "Alice", f"@{SLUG} two")
        self.run_pass()
        self.assertEqual(self.queued(), [1, 2])

    def test_the_session_cap(self):
        self.configure("followup_cooldown_seconds = 0\nfollowup_max_sessions = 2\n")
        for number in (1, 2, 3, 4):
            self.gh.add("issue", number, "alice", f"@{SLUG} {number}")
        self.run_pass()
        self.assertEqual(self.followup_lines(), [QUEUED, QUEUED, skipped("cap"), skipped("cap")])
        self.assertEqual((self.queued(), self.state()["sessions"], self.state()["max_sessions"]), ([1, 2], 2, 2))

    def test_closed_pull_requests_and_pull_requests_of_others_are_never_read(self):
        self.gh.pulls[3] = {"state": "closed", "login": APP, "type": "Bot"}
        self.gh.pulls[4] = {"state": "open", "login": "alice", "type": "User"}
        self.gh.pulls[5] = {"state": "open", "login": "other-app[bot]", "type": "Bot"}
        for number in (3, 4, 5):
            self.gh.add("issue", 1, "alice", f"@{SLUG} hi", pr=number)
        self.gh.add("issue", 1, "alice", f"@{SLUG} hi")
        self.run_pass()
        self.assertEqual(self.followup_lines(), [QUEUED])
        read = set(self.gh.paths_read())
        self.assertEqual({path.split("/")[4] for path in read}, {str(PR)})
        for number in (3, 4, 5):
            self.assertFalse(self.state_path(number).exists())

    def test_every_open_factory_pull_request_has_its_own_state_file(self):
        self.gh.pulls[13] = {"state": "open", "login": APP.upper(), "type": "Bot"}
        self.gh.add("issue", 1, "alice", f"@{SLUG} a")
        self.gh.add("issue", 1, "carol", f"@{SLUG} b", pr=13)
        self.run_pass()
        self.assertEqual(sorted(self.followup_lines()), [QUEUED, f"followup repo={REPO} pr=13 request queued"])
        self.assertEqual(sorted(p.name for p in (self.root / "followups").iterdir()), ["pr-12.json", "pr-13.json"])


class Paging(FollowupCase):
    def fill(self, kind, count, mention_at):
        for number in range(1, count + 1):
            self.gh.add(kind, number, "alice", f"@{SLUG} here" if number == mention_at else "chatter")

    def test_mentions_beyond_the_first_page_of_every_kind_are_found(self):
        self.configure("followup_cooldown_seconds = 0\n")
        self.fill("issue", 250, 230)
        self.fill("review_comment", 150, 120)
        self.fill("review", 101, 101)
        self.run_pass()
        self.assertEqual(self.followup_lines().count(QUEUED), 3)
        self.assertEqual([(r["kind"], r["comment_id"]) for r in self.state()["requests"]],
                         [("issue", 230), ("review_comment", 120), ("review", 101)])
        self.assertEqual(self.state()["cursors"], {"issue": 250, "review_comment": 150, "review": 101})
        pages = [c for c in self.gh.calls if c.endswith("/comments") and "issues" in c]
        self.assertEqual(len(pages), 3)  # 100 + 100 + 50 comments: the third page is short and ends the reading

    def test_the_reading_is_bounded(self):
        self.fill("issue", 250, 230)
        reader = trigger.GithubReader(REPO, FakeAuth(), self.data_dir, run=self.gh)
        seen = reader.remarks(PR, "issue", SLUG, max_pages=2)
        self.assertEqual((len(seen), seen[-1].id, [r.id for r in seen if r.mention]), (200, 200, []))
        self.assertEqual([r.id for r in reader.remarks(PR, "issue", SLUG, max_pages=3) if r.mention], [230])
        self.assertEqual(trigger.MAX_REMARK_PAGES, 10)

    def test_pull_requests_are_listed_paged_and_bounded(self):
        for number in range(100, 400):
            self.gh.pulls[number] = {"state": "open", "login": APP if number % 2 else "alice", "type": "Bot" if number % 2 else "User"}
        reader = trigger.GithubReader(REPO, FakeAuth(), self.data_dir, run=self.gh)
        everything = reader.pulls_by(APP)
        self.assertEqual((len(everything), everything[0], everything[-1]), (151, 399, PR))
        self.assertEqual(len(reader.pulls_by(APP, max_pages=1)), 50)  # the newest hundred pull requests only


class Persistence(FollowupCase):
    def test_a_crash_before_the_state_is_written_loses_nothing_and_logs_nothing(self):
        self.gh.add("issue", 1, "alice", f"@{SLUG} go")
        with patch.object(followup, "save_state", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.run_pass()
        self.assertEqual(self.followup_lines(), [])
        self.assertFalse(self.state_path().exists())
        self.run_pass()  # the restart: the same comment is read again
        self.assertEqual(self.followup_lines(), [QUEUED])
        self.assertEqual(self.queued(), [1])

    def test_a_crash_after_the_state_is_written_duplicates_nothing(self):
        self.gh.add("issue", 1, "alice", f"@{SLUG} go")
        reader = trigger.GithubReader(REPO, FakeAuth(), self.data_dir, run=self.gh)
        conf = FollowupSettings()

        def crash(text):
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            followup.follow_pull(reader, self.root, PR, SLUG, NOW, conf, reader.permission, crash)
        self.assertEqual(self.queued(), [1])  # durable before anything was reported
        self.time.now += 5000
        self.run_pass()  # restart: no second request, even long after the cooldown
        self.assertEqual(self.queued(), [1])
        self.assertEqual(self.followup_lines(), [])

    def test_a_write_that_fails_is_reported_and_retried_without_a_duplicate(self):
        self.gh.add("issue", 1, "alice", f"@{SLUG} go")
        with patch.object(followup.os, "replace", side_effect=OSError(28, "no space")):
            self.run_pass()
        self.assertEqual(self.followup_lines(), [f"followup repo={REPO} pr={PR} failed error=OSError"])
        directory = self.root / "followups"
        self.assertEqual(list(directory.iterdir()), [])  # no state, no stray temp file
        self.run_pass()
        self.assertEqual((self.followup_lines()[1:], self.queued()), ([QUEUED], [1]))

    def test_a_failing_permission_lookup_fails_closed_and_is_retried(self):
        self.gh.add("issue", 1, "alice", f"@{SLUG} go")
        self.gh.permission_error = True
        self.run_pass()
        self.assertEqual(self.followup_lines(), [f"followup repo={REPO} pr={PR} failed error=CalledProcessError"])
        self.assertFalse(self.state_path().exists())  # nothing recorded, nothing marked as seen
        self.gh.permission_error = False
        self.run_pass()
        self.assertEqual(self.followup_lines()[1:], [QUEUED])

    def test_decisions_before_a_failing_lookup_are_kept(self):
        self.gh.add("issue", 1, "alice", "chatter")
        self.gh.add("issue", 2, "alice", f"@{SLUG} go")
        self.gh.add("issue", 3, "carol", f"@{SLUG} go")
        self.gh.permissions.pop("alice")
        self.gh.permission_error = True
        self.run_pass()
        self.assertEqual(self.followup_lines(), [skipped("no-mention"), f"followup repo={REPO} pr={PR} failed error=CalledProcessError"])
        self.assertEqual(self.state()["cursors"]["issue"], 1)
        self.gh.permission_error = False
        self.gh.permissions["alice"] = "write"
        self.run_pass()
        self.assertEqual(self.queued(), [2, 3])

    def test_one_failing_pull_request_does_not_stop_the_others(self):
        self.gh.pulls[13] = {"state": "open", "login": APP, "type": "Bot"}
        self.gh.add("issue", 1, "alice", f"@{SLUG} a")
        self.gh.add("issue", 1, "carol", f"@{SLUG} b", pr=13)
        self.make_state_dir()
        os.symlink("/nonexistent", self.state_path(13))
        self.run_pass()
        self.assertEqual(
            sorted(self.followup_lines()),
            [QUEUED, f"followup repo={REPO} pr=13 failed error=StateError"],
        )

    def test_no_comment_text_reaches_a_log_line_or_a_state_file(self):
        for number, kind in enumerate(("issue", "review_comment", "review"), start=1):
            self.gh.add(kind, number, "alice", f"@{SLUG} {CANARY}")
            self.gh.add(kind, number + 10, "mallory", f"@{SLUG} {CANARY}")
            self.gh.add(kind, number + 20, "bot[bot]", f"@{SLUG} {CANARY}", type="Bot")
            self.gh.add(kind, number + 30, "alice", f"plain {CANARY}")
        self.run_pass()
        self.gh.edit("issue", 1, body=f"@{SLUG} {CANARY} edited")
        self.run_pass()
        self.assertTrue(self.followup_lines())
        for text in (self.out.getvalue(), self.err.getvalue(), *(p.read_text() for p in (self.root / "followups").iterdir())):
            self.assertNotIn(CANARY, text)
        self.assertNotIn(".body", "".join(self.gh.calls))


class StateFile(FollowupCase):
    def setUp(self):
        super().setUp()
        self.gh.add("issue", 1, "alice", f"@{SLUG} go")

    def test_the_file_is_private_and_replaced_atomically(self):
        self.run_pass()
        info = self.state_path().stat()
        self.assertEqual((stat.S_IMODE(info.st_mode), info.st_uid), (0o600, os.geteuid()))
        self.assertEqual(stat.S_IMODE((self.root / "followups").stat().st_mode), 0o700)
        before = self.state_path().stat().st_ino
        self.gh.add("issue", 2, "carol", f"@{SLUG} again")
        self.run_pass()
        self.assertNotEqual(self.state_path().stat().st_ino, before)  # a new file renamed over the old one
        self.assertEqual(sorted(p.name for p in (self.root / "followups").iterdir()), [f"pr-{PR}.json"])

    def test_a_failed_replace_leaves_the_old_file_intact(self):
        self.run_pass()
        old = self.state_path().read_bytes()
        self.gh.add("issue", 2, "carol", f"@{SLUG} again")
        with patch.object(followup.os, "replace", side_effect=OSError(5, "io")):
            self.run_pass()
        self.assertEqual(self.state_path().read_bytes(), old)
        self.assertEqual([p.name for p in (self.root / "followups").iterdir()], [f"pr-{PR}.json"])

    def test_a_symlinked_state_file_is_refused_and_never_written_through(self):
        target = self.root / "elsewhere.json"
        self.make_state_dir()
        target.write_text("{}")
        os.symlink(target, self.state_path())
        self.run_pass()
        self.assertEqual(self.followup_lines(), [f"followup repo={REPO} pr={PR} failed error=StateError"])
        self.assertEqual(target.read_text(), "{}")
        self.assertTrue(self.state_path().is_symlink())

    def test_a_symlinked_state_directory_is_refused(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir(parents=True)
        os.symlink(elsewhere, self.root / "followups")
        self.run_pass()
        self.assertEqual(self.followup_lines(), [f"followup repo={REPO} pr={PR} failed error=StateError"])
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_a_state_file_of_another_owner_is_refused(self):
        self.run_pass()
        with patch.object(followup.os, "geteuid", return_value=os.geteuid() + 1):
            with self.assertRaises(StateError):
                followup.load_state(self.root / "followups", PR)
        self.gh.add("issue", 2, "carol", f"@{SLUG} again")
        with patch.object(followup.os, "geteuid", return_value=os.geteuid() + 1):
            self.run_pass()
        self.assertEqual(self.followup_lines()[-1], f"followup repo={REPO} pr={PR} failed error=StateError")
        self.assertEqual(self.queued(), [1])

    def test_a_group_writable_state_file_is_refused(self):
        self.run_pass()
        self.state_path().chmod(0o660)
        with self.assertRaises(StateError):
            followup.load_state(self.root / "followups", PR)

    def test_an_oversized_or_malformed_state_file_is_refused(self):
        self.run_pass()
        path = self.state_path()
        path.write_text(" " * (followup.MAX_STATE_BYTES + 1))
        with self.assertRaisesRegex(StateError, "too large"):
            followup.load_state(self.root / "followups", PR)
        for text in ("not json", "[]", json.dumps({**followup.new_state(PR), "pr": 99}),
                     json.dumps({**followup.new_state(PR), "sessions": -1}),
                     json.dumps({**followup.new_state(PR), "cursors": {"issue": "x"}}),
                     json.dumps({**followup.new_state(PR), "requests": [{"pr": PR}]}),
                     json.dumps({**followup.new_state(PR), "lock": 5})):
            with self.subTest(text[:40]):
                path.write_text(text)
                with self.assertRaises(StateError):
                    followup.load_state(self.root / "followups", PR)

    def test_a_state_that_would_exceed_the_size_cap_forgets_the_oldest_fingerprints_first(self):
        state = followup.new_state(PR)
        state["fingerprints"] = {f"issue:{n}": OLD + "x" * 80 for n in range(1, 4000)}
        followup.save_state(self.root, PR, state)
        self.assertLessEqual(self.state_path().stat().st_size, followup.MAX_STATE_BYTES)
        kept = followup.load_state(self.root / "followups", PR)["fingerprints"]
        self.assertIn("issue:3999", kept)
        self.assertNotIn("issue:1", kept)

    def test_the_lock_field_of_a_later_phase_survives_a_rewrite(self):
        self.run_pass()
        document = self.state()
        document["lock"] = "r1/fix-a/7"
        self.state_path().write_text(json.dumps(document))
        os.chmod(self.state_path(), 0o600)
        self.gh.add("issue", 2, "carol", f"@{SLUG} again")
        self.run_pass()
        self.assertEqual((self.state()["lock"], self.queued()), ("r1/fix-a/7", [1, 2]))

    def test_an_unchanged_pull_request_writes_nothing(self):
        self.run_pass()
        before = self.state_path().stat().st_ino
        with patch.object(followup, "save_state", side_effect=AssertionError("written")):
            self.run_pass()
        self.assertEqual(self.state_path().stat().st_ino, before)

    def test_expired_cooldowns_are_dropped_when_the_file_is_written(self):
        self.run_pass()
        self.time.now += 700
        self.gh.add("issue", 2, "carol", f"@{SLUG} again")
        self.run_pass()
        self.assertEqual(list(self.state()["cooldowns"]), ["carol"])


class Settings(FollowupCase):
    def test_settings_of_a_config_use_the_documented_defaults_and_the_safe_side(self):
        self.assertEqual(followup.settings_of(object()), FollowupSettings(False, "write", 300, 5, ""))
        bad = type("Cfg", (), {"followup": "yes", "followup_min_permission": "owner",
                               "followup_cooldown_seconds": -1, "followup_max_sessions": True,
                               "mention_handle": "a b"})()
        self.assertEqual(followup.settings_of(bad), FollowupSettings(False, "admin", 300, 5, ""))

    def test_a_repository_that_never_set_followup_is_not_read(self):
        # off by default: flipping the default pauses the phase 1 recorder on every repository without ``followup = true``
        (self.config_dir / "repos" / "org__name.toml").write_text(f'repo = "{REPO}"\n')
        self.gh.add("issue", 1, "alice", f"@{SLUG} go")
        self.run_pass()
        self.assertEqual((self.gh.calls, self.followup_lines(), self.root.exists()), ([], [], False))

    def test_a_repository_that_opted_out_is_not_read(self):
        self.configure("followup = false\n")
        self.gh.add("issue", 1, "alice", f"@{SLUG} go")
        self.run_pass()
        self.assertEqual((self.gh.calls, self.followup_lines()), ([], []))

    def test_without_an_app_slug_the_pass_does_nothing_and_logs_nothing(self):
        self.env = {}
        self.install()
        self.gh.add("issue", 1, "alice", f"@{SLUG} go")
        self.run_pass()
        self.assertEqual((self.gh.calls, self.lines(), self.warnings()), ([], [], []))
        self.assertFalse(self.root.exists())

    def test_the_minimum_permission_applies(self):
        self.configure('followup_min_permission = "admin"\nfollowup_cooldown_seconds = 0\n')
        self.gh.add("issue", 1, "alice", f"@{SLUG} go")
        self.gh.add("issue", 2, "carol", f"@{SLUG} go")
        self.run_pass()
        self.assertEqual(self.followup_lines(), [skipped("unauthorised"), QUEUED])

    def test_a_minimum_below_write_is_refused_unless_the_repository_is_private(self):
        self.configure('followup_min_permission = "triage"\n')
        self.gh.add("issue", 1, "bob", f"@{SLUG} go")
        private = {"archived": False, "private": False, "visibility": "public"}
        original = self.gh.__call__
        self.gh.private = False

        def public(argv, cwd, timeout=None, auth=None):
            if argv[2] == f"repos/{REPO}":
                return json.dumps(private)
            return original(argv, cwd, timeout, auth)

        reader = trigger.GithubReader(REPO, FakeAuth(), self.data_dir, run=public)
        self.ops = self.new_ops(self.manager, github=lambda cfg: reader)
        self.run_pass()
        self.run_pass()
        self.assertEqual(self.followup_lines(), [f"followup repo={REPO} skipped reason=config"])  # once
        self.assertFalse(self.gh.paths_read())
        self.install()  # private repository: triage is allowed
        self.run_pass()
        self.assertEqual(self.followup_lines()[1:], [QUEUED])

    def test_a_failure_to_list_the_pull_requests_is_one_line_and_a_backoff(self):
        self.gh.fail_listing = True
        self.run_pass()
        self.assertEqual(self.followup_lines(), [f"followup repo={REPO} pass failed error=CalledProcessError backoff=60s"])
        calls = len(self.gh.calls)
        self.run_pass()  # backing off: nothing is asked
        self.assertEqual(len(self.gh.calls), calls)
        self.gh.fail_listing = False
        self.time.now += 61
        self.gh.add("issue", 1, "alice", f"@{SLUG} go")
        self.run_pass()
        self.assertEqual(self.followup_lines()[1:], [QUEUED])

    def test_the_trigger_pass_runs_it_even_when_the_label_watcher_is_off(self):
        self.configure("auto_build = false\n")
        (self.config_dir / "repos" / "org__other.toml").write_text('repo = "org/other"\nfollowup = false\nauto_build = false\n')
        self.gh.add("issue", 1, "alice", f"@{SLUG} go")
        self.ops.trigger_pass()
        self.assertEqual(self.followup_lines(), [QUEUED])
        self.assertIn(f"trigger repo={REPO} skipped reason=disabled", self.lines())

    def test_a_paused_trigger_reads_nothing(self):
        self.gh.add("issue", 1, "alice", f"@{SLUG} go")
        with patch.object(Manager, "trigger_paused", return_value=True):
            self.ops.trigger_pass()
        self.assertEqual((self.gh.calls, self.followup_lines()), ([], []))


JQ = shutil.which("jq")


class MentionFilter(unittest.TestCase):
    """What addresses the App is decided inside ``gh`` by the ``--jq`` filter, so it is tested with the real jq."""

    def verdicts(self, *bodies):
        rows = [
            {"id": number, "user": {"login": "alice", "type": "User"}, "body": body, "updated_at": OLD, "created_at": OLD}
            for number, body in enumerate(bodies, 1)
        ]

        def run(argv, cwd, timeout=None, auth=None):
            jq = argv[argv.index("--jq") + 1]
            done = subprocess.run(["jq", "-c", jq], input=json.dumps(rows), capture_output=True, text=True, check=True)
            return done.stdout

        reader = trigger.GithubReader(REPO, FakeAuth(), ".", run=run)
        return [remark.mention for remark in reader.remarks(PR, "issue", SLUG)]

    @unittest.skipUnless(JQ, "needs jq")
    def test_a_mention_counts_only_in_the_text_the_person_wrote_to_the_app(self):
        addressed = [
            f"@{SLUG} please fix it",
            f"hello @{SLUG}",
            f"@{SLUG.upper()} in any case",
            f"_@{SLUG}_ emphasised",
            f"__@{SLUG}__ strongly emphasised",
            f"*@{SLUG}* starred",
            f"> what someone else wrote\n\n@{SLUG} please do that",
            f"```\ncode\n```\n@{SLUG} after a block",
            f"`x` then @{SLUG}",
            f"line one\r\n@{SLUG} on a CRLF line",
        ]
        not_addressed = [
            f"> @{SLUG} run this",
            f"do not `@{SLUG}` this",
            f"```\n@{SLUG}\n```",
            f"~~~\n@{SLUG}\n~~~",
            f"```\n@{SLUG} inside a block that is never closed",
            f"``@{SLUG}`` in a double-backtick span",
            f"> quoted\n> @{SLUG} still quoted",
            f"  > @{SLUG} an indented quote",
            f"foo_@{SLUG}",
            f"mail me@{SLUG}.com",
        ]
        for body, verdict in zip(addressed, self.verdicts(*addressed)):
            with self.subTest(body=body):
                self.assertTrue(verdict, body)
        for body, verdict in zip(not_addressed, self.verdicts(*not_addressed)):
            with self.subTest(body=body):
                self.assertFalse(verdict, body)

    @unittest.skipUnless(JQ, "needs jq")
    def test_hostile_markup_cannot_make_a_read_slow_and_only_the_start_of_a_comment_is_searched(self):
        # Before the cut, a 65 KB comment of these took 1.4 to 2.5 s each in jq: a page of them outlasted the call timeout.
        hostile = ["`" * 65536, "`a" * 32768, "~" * 65536, "> q\n" * 16384, "```a" * 16384, "  >\n" * 13107]
        started = time.perf_counter()
        self.assertEqual(self.verdicts(*hostile), [False] * len(hostile))
        self.assertLess(time.perf_counter() - started, 3.0)
        limit = trigger.MAX_SEARCHED_CHARS
        near, far = f"@{SLUG} " + "a" * 10, "a" * limit + f" @{SLUG}"
        self.assertEqual(self.verdicts(near, far), [True, False])

    def test_the_pattern_accepts_an_emphasised_mention_and_still_refuses_an_identifier(self):
        pattern = re.compile(trigger.mention_pattern(SLUG), re.I)
        for text in (f"_@{SLUG}_ hi", f"__@{SLUG}__ hi", f"x _@{SLUG}_.", f"@{SLUG}"):
            self.assertRegex(text, pattern)
        for text in (f"foo_@{SLUG}", f"_@{SLUG}x", f"me@{SLUG}.com", f"@{SLUG}-two", f"@{SLUG}/team"):
            self.assertNotRegex(text, pattern)


class Windows(FollowupCase):
    def test_a_mention_after_a_thousand_old_comments_is_found(self):
        # Oldest first and capped at 1000 per kind, this one (number 1051) was never read.
        for number in range(1, 1051):
            self.gh.add("issue", number, "alice", "just talking", updated=ago(3 * 86400))
        self.gh.add("issue", 1051, "alice", f"@{SLUG} now")
        self.run_pass()
        self.assertEqual(self.queued(), [1051])
        reads = [call for call in self.gh.calls if call == f"repos/{REPO}/issues/{PR}/comments"]
        self.assertEqual(len(reads), 1)  # one page: the window left the old comments out

    def test_the_next_read_asks_only_for_what_changed_since_the_newest_update_seen(self):
        self.gh.add("issue", 1, "alice", "chat")
        self.run_pass()
        self.assertEqual(self.state()["since"]["issue"], OLD)
        self.gh.add("issue", 2, "alice", f"@{SLUG} later", updated=LATER)
        self.gh.pulls[PR]["updated"] = LATER
        self.run_pass()
        self.assertEqual(self.queued(), [2])
        self.assertEqual(self.state()["since"]["issue"], LATER)

    def test_hitting_the_page_bound_says_so_in_one_fixed_line(self):
        for number in range(1, 1101):
            self.gh.add("issue", number, "alice", "chat")
        self.run_pass()
        line = f"followup repo={REPO} pr={PR} truncated kind=issue"
        self.assertEqual(self.followup_lines().count(line), 1)

    def test_history_older_than_a_day_is_a_baseline_never_a_request(self):
        # Edited just now, so inside the window, but written three days ago: it was there before.
        self.gh.add("issue", 1, "alice", f"@{SLUG} an old request", created=ago(3 * 86400))
        self.gh.add("review", 2, "alice", f"@{SLUG} an old review", created=ago(3 * 86400), updated=ago(3 * 86400))
        self.gh.add("issue", 3, "alice", f"@{SLUG} a live request")
        self.run_pass()
        self.assertEqual(self.queued(), [3])
        self.assertEqual(self.followup_lines().count(f"followup repo={REPO} pr={PR} baseline comments=2"), 1)
        # It is recorded as seen: a later edit of it is an edit, never a new request.
        self.gh.edit("issue", 1)
        self.gh.pulls[PR]["updated"] = LATER
        self.run_pass()
        self.assertEqual(self.queued(), [3])
        self.assertIn(skipped("edited"), self.followup_lines())

    def test_a_pull_request_nothing_happened_on_is_not_read_again(self):
        self.gh.pulls[PR]["updated"] = OLD
        self.gh.add("issue", 1, "alice", "chat")
        self.run_pass()
        first = len(self.gh.paths_read())
        self.assertGreater(first, 0)
        self.run_pass()
        self.assertEqual(len(self.gh.paths_read()), first)  # the same updated_at: not a call
        self.gh.pulls[PR]["updated"] = LATER
        self.run_pass()
        second = len(self.gh.paths_read())
        self.assertGreater(second, first)
        self.time.now += followup.RESCAN_SECONDS + 1  # nothing changed, but it is looked at again now and then
        self.run_pass()
        self.assertGreater(len(self.gh.paths_read()), second)

    def test_a_quiet_pull_request_does_not_starve_a_busy_one(self):
        self.gh.pulls = {n: {"state": "open", "login": APP, "type": "Bot", "updated": OLD} for n in range(1, 61)}
        self.run_pass()
        self.assertEqual(len([n for n in range(1, 61) if self.state_path(n).exists()]), followup.MAX_PULLS_PER_PASS)
        self.run_pass()  # the fifty are unchanged and cost nothing: this pass reaches the other ten
        self.assertEqual(len([n for n in range(1, 61) if self.state_path(n).exists()]), 60)

    def test_a_failed_lookup_does_not_move_the_window(self):
        self.gh.add("issue", 1, "alice", f"@{SLUG} please")
        self.gh.pulls[PR]["updated"] = OLD
        self.gh.permission_error = True
        self.run_pass()
        # Nothing was decided, so the window did not move and nothing was written.
        self.assertFalse(self.state_path().exists())
        self.gh.permission_error = None
        self.run_pass()  # retried, nothing lost
        self.assertEqual(self.queued(), [1])
        self.assertEqual(self.state()["pr_updated"], OLD)


class WindowState(unittest.TestCase):
    def test_the_window_fields_are_optional_in_an_older_file_and_validated(self):
        older = followup.new_state(PR)
        for key in ("since", "pr_updated", "read_at"):
            del older[key]
        state = followup._validated(older, PR)
        self.assertEqual((state["since"]["issue"], state["pr_updated"], state["read_at"]), ("", "", 0))
        for bad in (
            {"since": {"issue": 5}},
            {"since": []},
            {"since": {"issue": "x" * 50}},
            {"pr_updated": 7},
            {"read_at": -1},
            {"read_at": "now"},
        ):
            with self.subTest(bad=bad), self.assertRaises(StateError):
                followup._validated({**followup.new_state(PR), **bad}, PR)

    def test_times_are_parsed_strictly(self):
        self.assertEqual(followup._epoch("2026-11-07T10:00:00Z"), 1794045600.0)
        for text in ("", "2026-11-07", "2026-11-07T10:00:00+00:00", "yesterday", None, 5):
            self.assertIsNone(followup._epoch(text), text)


if __name__ == "__main__":
    unittest.main()

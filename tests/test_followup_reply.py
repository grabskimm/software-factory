"""Phase 3 of the @mention follow-up: the reply on the pull request (src/factory/followup_reply.py).

Three layers. ``Template`` renders every outcome and checks what may never be in a body. ``Replier`` drives
``FollowupReplies`` against an in-memory conversation (a fake of the manager's reader) for the idempotent update, the
identity rules, the switches, the cap and the failure cooldown. ``Wiring`` runs the manager's serve pass on a real
``Operations`` over the fakes of tests/test_followup_serve.py, with a ``gh`` that also keeps the conversation, to prove
what is replied, what is not, and that a failing reply changes nothing about the session.
"""

import json
import re
import shutil
import unittest
from unittest.mock import patch

from factory import followup, followup_reply, trigger
from factory.followup_reply import FollowupReplies, render
from tests import test_followup as base
from tests import test_followup_serve as serve
from tests.test_followup import APP, LATER, OLD, PR, REPO, SLUG, called_process_error
from tests.test_followup_serve import HEAD, NEWER, RUN_FIELDS

BOT_ID = 4242
CANARY = "REPLY-CANARY-9c1e-never-posted"
SECRET = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
ALL_OUTCOMES = sorted(followup.OUTCOMES - {""})
NO_REPLY = ("pr-closed", "not-authorised", "expired", "cli-refused")


def comment_ids(rows):
    return [row["id"] for row in rows]


class Template(unittest.TestCase):
    def test_exactly_the_agreed_outcomes_get_a_reply(self):
        self.assertEqual(
            sorted(followup_reply.REPLY_OUTCOMES),
            sorted(["pushed", "no-change", "session-failed", "session-lost", "run-gone", "job-status", "head-moved"]),
        )
        self.assertEqual(sorted(set(ALL_OUTCOMES) - set(followup_reply.REPLY_OUTCOMES)), sorted(NO_REPLY))
        for outcome in (*NO_REPLY, "", "pushed ", "nonsense", None):
            with self.subTest(outcome):
                with self.assertRaises(ValueError):
                    render(PR, outcome)

    def test_every_reply_is_the_fixed_template_with_the_marker_first_and_the_code(self):
        for outcome in followup_reply.REPLY_OUTCOMES:
            with self.subTest(outcome):
                body = render(PR, outcome, HEAD)
                headline, text = followup_reply.REPLIES[outcome]
                self.assertEqual(body.splitlines()[0], f"<!-- factory-followup:v1 pr={PR} -->")
                self.assertTrue(body.startswith(followup_reply.marker(PR)))
                self.assertEqual(body.splitlines()[1], f"**Factory follow-up: {headline}**")
                self.assertIn(text, body)
                self.assertIn(f"Code: `{outcome}`", body)
                self.assertTrue(body.endswith(followup_reply.FOOTER))
                self.assertEqual(body, render(PR, outcome, HEAD))  # deterministic: an unchanged outcome is an unchanged body

    def test_a_reply_holds_no_mention_no_html_no_link_no_control_character_and_stays_short(self):
        for outcome in followup_reply.REPLY_OUTCOMES:
            body = render(PR, outcome, HEAD)
            visible = body.split("\n", 1)[1]  # the hidden marker is the one allowed piece of markup
            with self.subTest(outcome):
                self.assertNotIn("@", body)
                for forbidden in ("<", ">", "http", "www.", "](", "```", "\t", "\r"):
                    self.assertNotIn(forbidden, visible)
                self.assertLessEqual(len(body), followup_reply.MAX_BODY)
                self.assertEqual([char for char in body if ord(char) < 32 and char != "\n"], [])
                for slug in (SLUG, "a", "factory-app", "Example-Handle"):  # it can never address the App, whatever its name
                    self.assertIsNone(re.search(trigger.mention_pattern(slug), body, re.I))

    def test_only_a_valid_head_is_shown_and_only_for_a_pushed_head(self):
        self.assertIn(f"New head: `{NEWER[:12]}`", render(PR, "pushed", NEWER))
        self.assertNotIn(NEWER, render(PR, "pushed", NEWER))  # shortened
        self.assertIn("New head: `abcdef1`", render(PR, "pushed", "abcdef1"))
        for head in ("", None, 12345, "abc", "ABCDEF1234", "<script>alert(1)</script>", f"{CANARY}", "a" * 65, "abcdef1 @mallory", "abcdef1\n```"):
            with self.subTest(head):
                body = render(PR, "pushed", head)
                self.assertNotIn("New head", body)
                self.assertEqual(body, render(PR, "pushed"))
        for outcome in followup_reply.REPLY_OUTCOMES:
            if outcome != "pushed":
                self.assertNotIn("New head", render(PR, outcome, NEWER))

    def test_a_template_that_breaks_the_rules_never_reaches_github(self):
        bad = {
            "a mention": "thanks @mallory",
            "html": "see <b>this</b>",
            "a link": "read https://example.org/x",
            "a control character": "x\x1b[31mred",
            "a long text": "x " * followup_reply.MAX_BODY,
        }
        for name, text in bad.items():
            with self.subTest(name):
                with patch.dict(followup_reply.REPLIES, {"pushed": ("pushed a new head", text)}):
                    with self.assertRaises(ValueError):
                        render(PR, "pushed", HEAD)

    def test_no_secret_and_no_comment_text_can_be_in_a_body(self):
        with patch.dict("os.environ", {"FACTORY_TEST_TOKEN": SECRET, "OTHER_SECRET": CANARY}):
            for outcome in followup_reply.REPLY_OUTCOMES:
                body = render(PR, outcome, HEAD)
                self.assertNotIn(SECRET, body)
                self.assertNotIn(CANARY, body)

    def test_the_reply_of_the_app_or_of_the_handle_account_never_queues_a_follow_up_request(self):
        now = 1_800_000_600.0
        stamp = "2027-01-15T08:00:00Z"
        body = render(PR, "pushed", HEAD)
        mention = re.search(trigger.mention_pattern(SLUG), body, re.I) is not None
        self.assertFalse(mention)
        for conf, login, kind in (
            (followup.FollowupSettings(enabled=True), APP, "Bot"),
            (followup.FollowupSettings(enabled=True, handle="example-handle"), "example-handle", "User"),
        ):
            with self.subTest(login):
                state = followup.new_state(PR)
                remark = trigger.Remark("issue", 900, login, kind, stamp, mention, stamp)
                # even if the text did mention the App (a mention test of ours that failed), the author alone stops it
                forced = trigger.Remark("issue", 901, login, kind, stamp, True, stamp)
                outcomes, error = followup.evaluate(state, {"issue": [remark, forced]}, now, conf, lambda who: "admin")
                self.assertIsNone(error)
                self.assertEqual(state["requests"], [])
                self.assertEqual(state["sessions"], 0)
                self.assertEqual(outcomes[-1], "skipped reason=self" if conf.handle else "skipped reason=bot")


class FakeReader:
    """What ``FollowupReplies`` asks of the manager's reader, over an in-memory conversation per pull request."""

    def __init__(self, app_id=BOT_ID, login="factory-app[bot]"):
        self.app_user_id = app_id
        self.me = {"type": "Bot" if app_id else "User", "id": app_id or 7, "login": login}
        self.calls = []
        self.comments = {}  # pr -> [{"id", "body", "user"}]
        self.states = {}  # pr -> open | closed | merged
        self.fail = None  # an exception raised by every comment call
        self.login_error = None
        self.next_id = 5000

    def pull_state(self, number):
        self.calls.append(["pull_state", number])
        if self.fail is not None:
            raise self.fail
        return self.states.get(number, "open")

    def put(self, pr, body, user=None):
        self.next_id += 1
        self.comments.setdefault(pr, []).append({"id": self.next_id, "body": body, "user": user or self.me})
        return self.next_id

    def call(self, args):
        self.calls.append(list(args))
        if args == ["api", "user", "--jq", ".login"]:
            if self.login_error is not None:
                raise self.login_error
            return self.me["login"]
        if self.fail is not None:
            raise self.fail
        path = next(arg for arg in args if arg.startswith("repos/"))
        listing = re.fullmatch(rf"repos/{REPO}/issues/(\d+)/comments", path)
        editing = re.fullmatch(rf"repos/{REPO}/issues/comments/(\d+)", path)
        body = next((arg[5:] for arg in args if arg.startswith("body=")), None)
        if listing and "--paginate" in args:
            jq = args[args.index("--jq") + 1]
            wanted = re.search(r'startswith\("(.*?)"\)', jq)[1]
            bot = re.search(r'\.user\.type == "Bot" and \.user\.id == (\d+)', jq)
            login = re.search(r'\.user\.login == "(.*?)"', jq)
            rows = []
            for item in self.comments.get(int(listing[1]), []):
                mine = (
                    (item["user"].get("type") == "Bot" and item["user"].get("id") == int(bot[1]))
                    if bot
                    else item["user"].get("login") == login[1]
                )
                if mine and item["body"].startswith(wanted):
                    rows.append(json.dumps({"id": item["id"], "body": item["body"]}))
            return "\n".join(rows)
        if listing and args[1:3] == ["-X", "POST"]:
            return str(self.put(int(listing[1]), body))
        if editing and args[1:3] == ["-X", "PATCH"]:
            for rows in self.comments.values():
                for item in rows:
                    if item["id"] == int(editing[1]):
                        item["body"] = body
                        return ""
            raise called_process_error("gh: Not Found (HTTP 404)")
        raise AssertionError(f"fake gh: unhandled {args}")

    def writes(self):
        return [call[2] for call in self.calls if call[:1] == ["api"] and call[1:2] == ["-X"]]

    def ours(self, pr=PR):
        return [item for item in self.comments.get(pr, []) if item["user"] == self.me]


class Replier(unittest.TestCase):
    def setUp(self):
        self.reader = FakeReader()
        self.now = [1000.0]
        self.replier = self.make()

    def make(self, reader=None, env=None, enabled=True):
        replier = FollowupReplies(reader or self.reader, REPO, lambda: self.now[0], env={} if env is None else env)
        replier.configure(enabled)
        return replier

    def test_the_first_reply_posts_one_comment_with_the_template(self):
        self.assertEqual(self.replier.reply(PR, "pushed", NEWER), "replied")
        self.assertEqual([item["body"] for item in self.reader.ours()], [render(PR, "pushed", NEWER)])
        self.assertEqual(self.reader.writes(), ["POST"])
        posted = [call for call in self.reader.calls if call[:1] == ["api"] and "POST" in call][0]
        self.assertEqual(posted[posted.index("-X") + 2], f"repos/{REPO}/issues/{PR}/comments")  # the conversation, not a review

    def test_a_later_outcome_edits_the_same_comment_in_place(self):
        self.replier.reply(PR, "pushed", NEWER)
        before = comment_ids(self.reader.ours())
        self.assertEqual(self.replier.reply(PR, "head-moved"), "replied")
        self.assertEqual(comment_ids(self.reader.ours()), before)  # still one, the same id
        self.assertEqual(self.reader.ours()[0]["body"], render(PR, "head-moved"))
        self.assertEqual(self.reader.writes(), ["POST", "PATCH"])

    def test_an_unchanged_outcome_writes_nothing(self):
        self.replier.reply(PR, "no-change")
        self.assertEqual(self.replier.reply(PR, "no-change"), "unchanged")
        self.assertEqual(self.reader.writes(), ["POST"])
        self.assertEqual(len(self.reader.ours()), 1)

    def test_a_restart_never_duplicates_and_edits_the_comment_it_finds(self):
        self.replier.reply(PR, "pushed", NEWER)
        reborn = self.make()  # a new process, the same conversation
        self.assertEqual(reborn.reply(PR, "pushed", NEWER), "unchanged")
        self.assertEqual(reborn.reply(PR, "session-failed"), "replied")
        self.assertEqual(len(self.reader.ours()), 1)
        self.assertEqual(self.reader.ours()[0]["body"], render(PR, "session-failed"))
        self.assertEqual(self.reader.writes(), ["POST", "PATCH"])

    def test_a_comment_somebody_deleted_is_posted_again_and_never_patched_blind(self):
        self.replier.reply(PR, "pushed", NEWER)
        self.reader.comments[PR].clear()  # a person deleted it
        self.assertEqual(self.replier.reply(PR, "pushed", NEWER), "replied")
        self.assertEqual(self.reader.writes(), ["POST", "POST"])
        self.assertEqual(len(self.reader.ours()), 1)

    def test_each_pull_request_has_its_own_reply(self):
        self.replier.reply(PR, "pushed", NEWER)
        self.replier.reply(PR + 1, "no-change")
        self.assertEqual((len(self.reader.ours(PR)), len(self.reader.ours(PR + 1))), (1, 1))
        self.assertIn(f"pr={PR + 1} -->", self.reader.ours(PR + 1)[0]["body"])

    def test_only_a_comment_of_factorys_own_identity_is_ever_edited(self):
        forged = render(PR, "pushed", HEAD)
        stranger = {"type": "Bot", "id": BOT_ID + 1, "login": "other-app[bot]"}
        person = {"type": "User", "id": 9, "login": "mallory"}
        theirs = [self.reader.put(PR, forged, stranger), self.reader.put(PR, forged, person),
                  self.reader.put(PR, f"> {followup_reply.marker(PR)}\nquoting it", person)]
        self.assertEqual(self.replier.reply(PR, "no-change"), "replied")
        by_id = {item["id"]: item["body"] for item in self.reader.comments[PR]}
        for ident in theirs[:2]:
            self.assertEqual(by_id[ident], forged)  # a bot's and a person's comment with the marker: untouched
        self.assertEqual(self.reader.writes(), ["POST"])  # Factory's own was not there: one new comment
        self.assertEqual(len(self.reader.ours()), 1)

    def test_an_ambient_identity_edits_only_comments_of_its_own_login(self):
        reader = FakeReader(app_id=None, login="ci-user")
        replier = self.make(reader)
        replier.reply(PR, "pushed", NEWER)
        reader.put(PR, render(PR, "pushed", NEWER), {"type": "User", "id": 8, "login": "mallory"})
        self.assertEqual(self.make(reader).reply(PR, "no-change"), "replied")
        self.assertEqual(reader.writes(), ["POST", "PATCH"])
        self.assertEqual([item["user"]["login"] for item in reader.comments[PR]], ["ci-user", "mallory"])
        self.assertEqual(reader.comments[PR][0]["body"], render(PR, "no-change"))
        self.assertEqual(reader.comments[PR][1]["body"], render(PR, "pushed", NEWER))

    def test_an_identity_that_cannot_be_told_posts_nothing(self):
        reader = FakeReader(app_id=None, login="not a login")  # not a valid account name
        self.assertEqual(self.make(reader).reply(PR, "pushed", NEWER), "failed")
        self.assertEqual((reader.writes(), reader.comments), ([], {}))
        reader = FakeReader(app_id=None)
        reader.login_error = called_process_error("HTTP 403")
        self.assertEqual(self.make(reader).reply(PR, "pushed", NEWER), "failed")
        self.assertEqual((reader.writes(), reader.comments), ([], {}))

    def test_a_pull_request_that_is_not_open_gets_no_comment(self):
        for state in ("closed", "merged"):
            with self.subTest(state):
                self.reader.states[PR] = state
                self.assertEqual(self.replier.reply(PR, "pushed", NEWER), "closed")
        self.assertEqual((self.reader.writes(), self.reader.comments), ([], {}))
        self.assertEqual(self.reader.calls, [["pull_state", PR]] * 2)  # nothing else was asked

    def test_an_outcome_without_a_reply_and_a_bad_pull_request_touch_nothing(self):
        for pr, outcome in ((PR, "pr-closed"), (PR, "not-authorised"), (PR, "expired"), (PR, "cli-refused"), (0, "pushed"), ("12", "pushed"), (True, "pushed")):
            with self.subTest((pr, outcome)):
                self.assertEqual(self.replier.reply(pr, outcome, NEWER), "off")
        self.assertEqual(self.reader.calls, [])

    def test_the_repository_switch_and_the_deployment_kill_switch_stop_every_call(self):
        self.replier.configure(False)
        self.assertEqual(self.replier.reply(PR, "pushed", NEWER), "off")
        for value in ("off", "0", "false", "no", "disabled", " OFF "):
            with self.subTest(value):
                self.assertEqual(self.make(env={"FACTORY_ISSUE_COMMENTS": value}).reply(PR, "pushed", NEWER), "off")
        self.assertEqual(self.reader.calls, [])
        self.assertEqual(self.make(env={"FACTORY_ISSUE_COMMENTS": "on"}).reply(PR, "pushed", NEWER), "replied")

    def test_the_cap_limits_the_writes_of_a_pass_and_the_next_pass_starts_from_zero(self):
        replier = self.make(env={"FACTORY_ISSUE_COMMENTS_PER_CYCLE": "2"})
        results = [replier.reply(PR + n, "no-change") for n in range(3)]
        self.assertEqual(results, ["replied", "replied", "cap"])
        self.assertEqual(self.reader.writes(), ["POST", "POST"])
        replier.new_cycle()
        self.assertEqual(replier.reply(PR + 2, "no-change"), "replied")
        self.assertEqual(self.make(env={"FACTORY_ISSUE_COMMENTS_PER_CYCLE": "0"}).reply(PR, "no-change"), "cap")

    def test_a_failure_is_reported_by_its_class_only_and_three_in_a_row_rest_the_replier(self):
        self.reader.fail = called_process_error(f"HTTP 403 {SECRET} {CANARY}")
        for _ in range(3):
            self.assertEqual(self.replier.reply(PR, "pushed", NEWER), "failed")
            self.assertEqual(self.replier.last_error, "CalledProcessError")
        calls = len(self.reader.calls)
        self.assertEqual(self.replier.reply(PR, "pushed", NEWER), "cooldown")
        self.assertEqual(len(self.reader.calls), calls)  # resting: no call at all
        self.now[0] += 301
        self.reader.fail = None
        self.assertEqual(self.replier.reply(PR, "pushed", NEWER), "replied")
        self.assertEqual(len(self.reader.ours()), 1)

    def test_a_success_in_between_resets_the_failure_streak(self):
        for _ in range(2):
            self.reader.fail = called_process_error("HTTP 500")
            self.assertEqual(self.replier.reply(PR, "pushed", NEWER), "failed")
            self.reader.fail = None
            self.assertEqual(self.replier.reply(PR, "pushed", NEWER), "replied")
            self.reader.comments.clear()
        self.assertEqual(self.replier.failures, 0)

    def test_a_malformed_listing_is_a_failure_and_not_a_duplicate(self):
        original = self.reader.call

        def garbled(args):
            return "this is not json" if "--paginate" in args else original(args)

        self.reader.call = garbled
        self.assertEqual(self.replier.reply(PR, "pushed", NEWER), "failed")
        self.assertEqual(self.reader.comments, {})

    def test_nothing_of_the_pull_request_or_the_environment_reaches_the_body(self):
        with patch.dict("os.environ", {"FACTORY_TEST_TOKEN": SECRET}):
            self.reader.put(PR, f"@{SLUG} please do {CANARY} {SECRET}", {"type": "User", "id": 3, "login": "alice"})
            self.replier.reply(PR, "pushed", NEWER)
        body = self.reader.ours()[0]["body"]
        self.assertEqual(body, render(PR, "pushed", NEWER))
        self.assertNotIn(CANARY, json.dumps(self.reader.calls))  # nothing of the conversation was sent back
        self.assertNotIn(SECRET, json.dumps(self.reader.calls))


class AppAuth(base.FakeAuth):
    app_user_id = BOT_ID


class ReplyGh(serve.ServeGh):
    """``ServeGh`` that also keeps what Factory wrote on the conversation, as the App's bot user (or an ambient user)."""

    def __init__(self):
        super().__init__()
        self.me = {"type": "Bot", "id": BOT_ID, "login": APP}
        self.writes = []  # ("POST", pr) or ("PATCH", comment id)
        self.write_error = None  # a message: every comment write fails with it
        self.next_id = 7000

    def mine(self, pr=PR):
        return [
            row for row in self.comments.get(("issue", pr), []) if row["body"].startswith(followup_reply.MARKER_PREFIX)
        ]

    def __call__(self, argv, cwd, timeout=None, auth=None):
        if argv[2:] == ["user", "--jq", ".login"]:
            return self.me["login"]
        path = next((arg for arg in argv[2:] if arg.startswith("repos/")), "")
        rest = path.partition("?")[0][len(f"repos/{REPO}") :]
        listing = re.fullmatch(r"/issues/(\d+)/comments", rest)
        editing = re.fullmatch(r"/issues/comments/(\d+)", rest)
        body = next((arg[5:] for arg in argv if arg.startswith("body=")), None)
        if listing and "--paginate" in argv:
            jq = argv[argv.index("--jq") + 1]
            wanted = re.search(r'startswith\("(.*?)"\)', jq)[1]
            bot = re.search(r'\.user\.type == "Bot" and \.user\.id == (\d+)', jq)
            login = re.search(r'\.user\.login == "(.*?)"', jq)
            rows = []
            for row in self.comments.get(("issue", int(listing[1])), []):
                mine = (row["type"] == "Bot" and row.get("uid") == int(bot[1])) if bot else row["login"] == login[1]
                if mine and row["body"].startswith(wanted):
                    rows.append(json.dumps({"id": row["id"], "body": row["body"]}))
            return "\n".join(rows)
        if (listing and "POST" in argv) or (editing and "PATCH" in argv):
            if self.write_error:
                raise called_process_error(self.write_error)
            if listing:
                pr = int(listing[1])
                self.next_id += 1
                self.add("issue", self.next_id, self.me["login"], body, pr=pr, type=self.me["type"], updated=OLD)
                self.comments[("issue", pr)][-1]["uid"] = self.me["id"]
                self.writes.append(("POST", pr))
                return str(self.next_id)
            for rows in self.comments.values():
                for row in rows:
                    if row["id"] == int(editing[1]):
                        row["body"] = body
            self.writes.append(("PATCH", int(editing[1])))
            return ""
        return super().__call__(argv, cwd, timeout, auth)


class ReplyCase(serve.ServeBase):
    """The world of tests/test_followup_serve.py with a ``gh`` that keeps Factory's comments and an App identity.

    The reply is on here (it is the default); ``serve.ServeBase`` switches it off for the serving tests.
    """

    CONFIG = "followup_cooldown_seconds = 0\nauto_build_timeout_seconds = 5400\n"
    GH = ReplyGh
    AUTH = AppAuth

    def setUp(self):
        for target, name, value in ((serve, "ServeGh", self.GH), (base, "FakeAuth", self.AUTH)):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        super().setUp()
        self.configure(self.CONFIG)

    def replied(self, pr=PR):
        return self.gh.mine(pr)

    def settle_pushed(self, comment=101):
        self.mention(comment)
        self.run_pass()
        self.end_session(status="pr_ready", commit=NEWER)
        self.run_pass()

    def assertSilent(self):
        self.assertEqual((self.gh.writes, self.replied()), ([], []))
        self.assertNotIn("replied", "\n".join(self.followup_lines()))


class Wiring(ReplyCase):
    """The serve pass tells the pull request what became of a settled request (and only that)."""

    def test_a_pushed_follow_up_is_told_in_one_comment_and_the_next_outcome_edits_it_in_place(self):
        self.mention()
        self.run_pass()
        self.assertEqual((self.gh.writes, self.replied()), ([], []))  # nothing is said while the session works
        self.end_session(status="pr_ready", commit=NEWER)
        self.run_pass()
        rows = self.replied()
        self.assertEqual([row["body"] for row in rows], [render(PR, "pushed", NEWER)])
        self.assertIn(f"New head: `{NEWER[:12]}`", rows[0]["body"])
        self.assertEqual((rows[0]["type"], rows[0]["login"]), ("Bot", APP))
        self.assertEqual(self.gh.writes, [("POST", PR)])
        self.assertEqual(
            self.followup_lines()[-2:], [f"{RUN_FIELDS} done outcome=pushed", f"{RUN_FIELDS} replied outcome=pushed"]
        )
        ident = rows[0]["id"]
        self.gh.heads[PR] = NEWER  # the publication moved the head
        self.mention(102)
        self.run_pass()
        self.end_session(status="pr_ready", followup={**self.job()["followup"], "outcome": "no-change"})
        self.run_pass()
        self.assertEqual([(row["id"], row["body"]) for row in self.replied()], [(ident, render(PR, "no-change"))])
        self.assertEqual(self.gh.writes, [("POST", PR), ("PATCH", ident)])  # one comment, edited, never a second
        self.assertEqual(self.followup_lines()[-1], f"{RUN_FIELDS} replied outcome=no-change")

    def test_a_restart_finds_the_comment_by_its_marker_and_does_not_duplicate_it(self):
        self.settle_pushed()
        self.assertEqual(self.gh.writes, [("POST", PR)])
        self.ops._serving._replies.clear()  # a new manager process: no memory of what it wrote
        self.gh.heads[PR] = NEWER
        self.mention(102)
        self.run_pass()
        self.end_session(status="pr_ready", commit="c" * 40)  # pushed again, another head
        self.run_pass()
        self.assertEqual([row["body"] for row in self.replied()], [render(PR, "pushed", "c" * 40)])
        self.assertEqual(self.gh.writes, [("POST", PR), ("PATCH", self.replied()[0]["id"])])

    def refused_and_told(self, code):
        self.mention()
        self.run_pass()
        self.assertEqual([row["body"] for row in self.replied()], [render(PR, code)])
        self.assertEqual(self.followup_lines()[-2:], [f"{RUN_FIELDS} refused reason={code}", f"{RUN_FIELDS} replied outcome={code}"])
        self.assertEqual((self.retries, self.spawner.calls), ([], []))  # the reply started nothing

    def test_a_head_somebody_else_pushed_is_told(self):
        self.gh.heads[PR] = NEWER
        self.refused_and_told("head-moved")

    def test_a_job_that_is_not_published_is_told(self):
        self.make_publication(status="failed")
        self.refused_and_told("job-status")

    def test_a_run_that_is_gone_is_told(self):
        shutil.rmtree(self.run_dir(serve.RUN))
        self.refused_and_told("run-gone")

    def test_a_failed_session_is_told_without_the_jobs_error(self):
        self.mention()
        self.run_pass()
        self.end_session(status="failed", error=CANARY)
        self.run_pass()
        self.assertEqual([row["body"] for row in self.replied()], [render(PR, "session-failed")])
        self.assertNotIn(CANARY, self.replied()[0]["body"])
        self.assertNotIn(CANARY, str(self.gh.comments))

    def test_a_lost_session_is_told(self):
        self.mention()
        self.run_pass()
        self.spawner.processes[-1].returncode = -9
        self.set_job(status="interrupted")
        self.time.now += 3601
        self.run_pass()
        self.assertEqual(self.followup_lines()[-2:], [f"{RUN_FIELDS} failed outcome=session-lost", f"{RUN_FIELDS} replied outcome=session-lost"])
        self.assertEqual([row["body"] for row in self.replied()], [render(PR, "session-lost")])

    # -- what is not told ---------------------------------------------------------------------

    def test_a_closed_pull_request_gets_no_reply(self):
        self.gh.states[PR] = "closed"
        self.mention()
        self.run_pass()
        self.assertEqual(self.followup_lines()[-1], f"{RUN_FIELDS} refused reason=pr-closed")
        self.assertSilent()
        self.assertEqual(self.ops._serving._replies, {})  # an outcome without a reply does not even build the replier

    def test_a_pull_request_closed_while_the_session_ran_gets_no_comment(self):
        self.mention()
        self.run_pass()
        self.end_session(status="pr_ready", commit=NEWER)
        self.gh.states[PR] = "closed"
        self.run_pass()
        self.assertEqual(self.followup_lines()[-2:], [f"{RUN_FIELDS} done outcome=pushed", f"{RUN_FIELDS} reply skipped reason=closed"])
        self.assertSilent()

    def test_an_author_who_lost_permission_gets_no_reply(self):
        self.manager.set_trigger_pause(True)
        self.mention(101, "alice")
        self.run_pass()
        self.manager.set_trigger_pause(False)
        self.gh.permissions["alice"] = "read"
        self.run_pass()
        self.assertEqual(self.followup_lines()[-1], f"{RUN_FIELDS} refused reason=not-authorised")
        self.assertSilent()

    def test_an_expired_request_gets_no_reply(self):
        self.manager.set_trigger_pause(True)
        self.mention()
        self.run_pass()
        self.manager.set_trigger_pause(False)
        self.time.now += followup.MAX_REQUEST_AGE_SECONDS + 1
        self.run_pass()
        self.assertEqual(self.followup_lines()[-1], f"{RUN_FIELDS} refused reason=expired")
        self.assertSilent()

    def test_a_retry_the_cli_refused_three_times_gets_no_reply(self):
        self.runner.override[("retry",)] = lambda repo, run, tail: serve.failure()
        self.mention()
        for pause in (0, 61, 121):
            self.time.now += pause
            self.run_pass()
        self.assertEqual(self.followup_lines()[-1], f"{RUN_FIELDS} refused reason=cli-refused")
        self.assertSilent()

    def test_comments_that_never_became_requests_get_no_reply_and_no_call(self):
        self.mention(101, "mallory")  # read access: skipped as unauthorised
        self.mention(102, "alice")
        self.mention(103, "alice")  # the same user inside the cooldown window
        self.configure("followup_cooldown_seconds = 3600\nauto_build_timeout_seconds = 5400\n")
        self.run_pass()
        text = " ".join(self.followup_lines())
        self.assertIn("pr=12 skipped reason=unauthorised", text)
        self.assertIn("pr=12 skipped reason=cooldown", text)
        self.assertSilent()
        self.end_session(status="pr_ready", commit=NEWER)
        self.run_pass()
        self.assertEqual(len(self.replied()), 1)  # only the one request that was queued and then settled

    def test_the_repository_switch_off_means_no_comment_call_at_all(self):
        self.configure(f"{self.CONFIG}followup_reply = false\n")
        self.settle_pushed()
        self.assertEqual(self.followup_lines()[-1], f"{RUN_FIELDS} done outcome=pushed")
        self.assertSilent()
        self.assertEqual(self.ops._serving._replies, {})  # not even built

    def test_a_reply_that_stops_being_wanted_between_passes_is_not_sent(self):
        self.mention()
        self.run_pass()
        self.end_session(status="pr_ready", commit=NEWER)
        self.configure(f"{self.CONFIG}followup_reply = false\n")
        self.run_pass()
        self.assertSilent()

    def test_a_manager_that_is_closing_sends_nothing(self):
        self.mention()
        self.run_pass()
        self.end_session(status="pr_ready", commit=NEWER)
        self.manager.closing.set()
        self.run_pass()
        self.assertEqual(self.status(), "done")
        self.assertSilent()

    # -- failures change nothing ---------------------------------------------------------------

    def test_a_reply_that_cannot_be_written_changes_nothing_about_the_session_or_the_pass(self):
        self.gh.write_error = f"HTTP 403 {SECRET}"
        self.mention()
        self.run_pass()
        self.end_session(status="pr_ready", commit=NEWER)
        self.run_pass()
        self.assertEqual((self.state()["lock"], self.status(), self.request()["outcome"]), (None, "done", "pushed"))
        self.assertEqual(
            self.followup_lines()[-2:],
            [f"{RUN_FIELDS} done outcome=pushed", f"{RUN_FIELDS} reply failed error=CalledProcessError"],
        )
        self.assertNotIn("pass failed", "\n".join(self.followup_lines()))
        self.assertNotIn(SECRET, "".join([self.out.getvalue(), self.err.getvalue()]))
        self.gh.write_error = None
        self.gh.heads[PR] = NEWER
        self.mention(102)
        self.run_pass()
        self.assertEqual(len(self.spawned()), 2)  # the next request is served as if nothing had happened
        self.end_session(status="pr_ready", commit="c" * 40)
        self.run_pass()
        self.assertEqual([row["body"] for row in self.replied()], [render(PR, "pushed", "c" * 40)])


class KillSwitch(ReplyCase):
    """The deployment-wide ``FACTORY_ISSUE_COMMENTS=off`` stops the reply as it stops the issue comments."""

    env = {"FACTORY_GITHUB_APP_SLUG": SLUG, "FACTORY_ISSUE_COMMENTS": "off"}

    def test_the_kill_switch_stops_the_reply(self):
        self.settle_pushed()
        self.assertEqual(self.followup_lines()[-1], f"{RUN_FIELDS} done outcome=pushed")
        self.assertSilent()


class CapPerPass(ReplyCase):
    """``FACTORY_ISSUE_COMMENTS_PER_CYCLE`` counts the reply writes of one serve pass, from zero in every pass."""

    env = {"FACTORY_GITHUB_APP_SLUG": SLUG, "FACTORY_ISSUE_COMMENTS_PER_CYCLE": "1"}

    def test_the_cap_of_the_replies_counts_from_zero_in_every_pass(self):
        self.settle_pushed()
        self.assertEqual(self.followup_lines()[-1], f"{RUN_FIELDS} replied outcome=pushed")  # the one write of that pass
        self.gh.heads[PR] = NEWER
        self.mention(102)
        self.run_pass()
        self.end_session(status="pr_ready", followup={**self.job()["followup"], "outcome": "no-change"})
        self.run_pass()
        # a manager that never reset the count would stop replying for good after its first write
        self.assertEqual(self.followup_lines()[-1], f"{RUN_FIELDS} replied outcome=no-change")
        self.assertNotIn("reply skipped reason=cap", "\n".join(self.followup_lines()))


class AmbientLogin(ReplyCase):
    """Without an App identity the reply is written, and found again, as the ambient ``gh`` login."""

    AUTH = base.FakeAuth  # no app_user_id

    def setUp(self):
        super().setUp()
        self.gh.me = {"type": "User", "id": 9, "login": "ci-user"}

    def test_the_reply_is_written_as_the_login_and_edited_in_place(self):
        self.settle_pushed()
        self.assertEqual([(row["login"], row["body"]) for row in self.replied()], [("ci-user", render(PR, "pushed", NEWER))])
        self.gh.heads[PR] = NEWER
        self.mention(102)
        self.run_pass()
        self.end_session(status="pr_ready", followup={**self.job()["followup"], "outcome": "no-change"})
        self.run_pass()
        self.assertEqual([row["body"] for row in self.replied()], [render(PR, "no-change")])
        self.assertEqual([kind for kind, _ in self.gh.writes], ["POST", "PATCH"])

    def test_an_identity_that_cannot_be_told_posts_nothing_and_changes_nothing(self):
        self.gh.me = {"type": "User", "id": 9, "login": "not a login"}
        self.settle_pushed()
        self.assertEqual(self.status(), "done")
        self.assertEqual(self.followup_lines()[-1], f"{RUN_FIELDS} reply failed error=LookupError")
        self.assertSilent()


class OwnReply(ReplyCase):
    """Factory's reply is a bot comment: read by the next follow-up pass, it neither queues a request nor reaches a context."""

    def test_the_reply_is_skipped_as_a_bot_comment_and_never_becomes_context_or_a_request(self):
        self.settle_pushed()
        [reply] = self.gh.mine()
        self.gh.heads[PR] = NEWER
        self.mention(102, "alice", f"@{SLUG} and please also update the changelog")
        self.run_pass()  # reads the reply along with the new mention
        self.assertEqual(self.queued(), [101, 102])  # the reply queued nothing
        self.assertIn(f"{RUN_FIELDS} skipped reason=bot", self.followup_lines())  # it was seen, and decided by its author
        self.assertEqual(self.context()["request"]["comment_id"], 102)
        self.assertEqual([row["id"] for row in self.context()["comments"]], [102])  # the reply is not handed to a session
        self.assertNotIn("Factory follow-up", json.dumps(self.context()))
        self.assertIsNone(re.search(trigger.mention_pattern(SLUG), reply["body"], re.I))

    def test_even_a_reply_that_carried_a_mention_would_queue_nothing(self):
        self.settle_pushed()
        [reply] = self.gh.mine()
        self.gh.edit("issue", reply["id"], body=f"@{SLUG} {reply['body']}", updated=LATER)  # worst case: it names the App
        self.run_pass()
        self.assertEqual(self.queued(), [101])
        self.assertIn(f"{RUN_FIELDS} skipped reason=bot", self.followup_lines())


if __name__ == "__main__":
    unittest.main()

"""Phase 3 of the @mention follow-up: ONE rolling reply per pull request, from a fixed template.

When a follow-up request that was queued has been settled (``followup_serve.py`` records the outcome in
``followups/pr-<N>.json``), the manager says on the pull request what became of it: ``FollowupReplies.reply``
posts, or edits in place, ONE comment from Factory's own identity (the App's bot user, or the ambient ``gh``
login). It is built on ``IssueComments`` (``issue_comments.py``) and so has the same marker search, identity
test, failure cooldown and per-cycle cap; only the marker, the template and the way it is driven differ.

* **Fixed text, nothing else.** The body is a template per outcome code plus, for ``pushed``, the new head
  commit (validated hex, shortened). No builder summary, no model text and nothing taken from a comment, a
  title or a log: there is nothing to sanitise because nothing untrusted goes in. ``render`` still refuses a
  body that holds an ``@``, a ``<``, a ``>``, a link, a control character or more than ``MAX_BODY``
  characters, so an edit of a template cannot reach GitHub unnoticed (the tests render every outcome).
  The reply contains no ``@`` at all, so it never mentions the App or anybody else.
* **One comment per pull request, edited in place.** It starts with a hidden marker,
  ``<!-- factory-followup:v1 pr=<N> -->``. Before writing, the pull request's conversation is searched for a
  comment of Factory's own identity that starts with that exact marker: found with the same text, nothing is
  written; found with another text, that comment is edited; not found, one is posted. So a restart never
  duplicates it, a person's comment that quotes the marker (or another bot's) is never touched, and the
  newest outcome always replaces the older one. An edit sends no notification: that is the price of a reply
  that cannot flood a pull request.
* **Only for what was asked and then decided.** A reply goes out for a request that was queued and then
  resolved (``pushed``, ``no-change``, ``session-failed``, ``session-lost``) or refused by a check of the
  state (``run-gone``, ``job-status``, ``head-moved``). Never for a comment that was skipped before it was
  queued (a stranger, the cooldown, the cap), never for a refusal because the author lost permission or the
  request expired or the CLI refused it, and never on a pull request that is no longer open (``pr-closed``
  gets no reply, and a pull request found closed or merged when the reply is due gets none either).
* **Best effort.** The caller is the manager's settlement step, never a worker. Every failure (a 403, a
  timeout, an unknown identity, a malformed answer) is swallowed and reported as one fixed line by the
  caller; three in a row rest the replier for five minutes; the outcome stays recorded in the state file and
  nothing is retried. The ``followup_reply`` switch of the repository (default true), the deployment-wide
  ``FACTORY_ISSUE_COMMENTS=off`` and the cap ``FACTORY_ISSUE_COMMENTS_PER_CYCLE`` (per settlement pass here)
  all apply.
* **Factory's reply never asks for anything.** It is written by a bot account (or by the ``mention_handle``
  account), which the follow-up pass skips (``skipped reason=bot`` or ``skipped reason=self``), and it holds
  no mention. See ``docs/operations.md`` ("Follow-ups on pull requests").
"""

from __future__ import annotations

import re

from . import followup
from .issue_comments import IssueComments, switched_off

KIND = "followup"
MARKER_PREFIX = "<!-- factory-followup:v1 "
MAX_BODY = 1500
HEAD_SHOWN = 12
_HEAD = re.compile(r"[0-9a-f]{7,64}")
_FORBIDDEN = re.compile(r"[@<>\x00-\x08\x0b-\x1f\x7f]|https?:|www\.", re.I)

# The outcomes that get a reply, each with its headline and its fixed text. Nothing a model or a commenter
# wrote goes in here. An outcome that is not a key gets no reply: ``pr-closed`` (nobody reads a closed pull
# request's thread), ``not-authorised`` (the author lost the permission: no public answer to somebody who is
# not allowed to ask), ``expired`` and ``cli-refused`` (Factory's own queue, not a decision about the request).
REPLIES = {
    followup.OUTCOME_PUSHED: (
        "pushed a new head",
        "Factory worked on a follow-up request for this pull request and pushed a new head. Review, "
        "verification and the `factory/gate` status run again for that head, and nothing merges before "
        "they pass.",
    ),
    followup.OUTCOME_NO_CHANGE: (
        "made no change",
        "Factory worked on a follow-up request for this pull request but made no change, so its head is "
        "the same as before. If the request needs something Factory could not do by itself, a person has "
        "to make that change.",
    ),
    followup.OUTCOME_FAILED: (
        "the session did not finish",
        "The follow-up session on this pull request ended without a usable result (it failed or was "
        "blocked), so its head is unchanged. An operator can see why in the Factory run (`factory status`).",
    ),
    followup.OUTCOME_LOST: (
        "the session was lost",
        "The follow-up session on this pull request could not be completed (its run was interrupted for "
        "too long, or is gone), so its head is unchanged. An operator can look at the Factory run "
        "(`factory status`).",
    ),
    followup.OUTCOME_RUN_GONE: (
        "no run to build on",
        "Factory no longer holds the run that built this pull request, so it cannot follow up on it and "
        "nothing was started. The head is unchanged.",
    ),
    followup.OUTCOME_JOB_STATUS: (
        "the job cannot take a follow-up",
        "The Factory job that built this pull request is not in a state that can take a follow-up (it is "
        "still being worked on, or it ended in a failure), so nothing was started. The head is unchanged.",
    ),
    followup.OUTCOME_HEAD_MOVED: (
        "the head is not the one Factory built",
        "The head of this pull request is not the commit Factory built and had reviewed (someone pushed to "
        "the branch or updated it). Factory only follows up on the exact commit it built, so nothing was "
        "started. A person has to handle the change that moved the head.",
    ),
}
REPLY_OUTCOMES = tuple(REPLIES)
FOOTER = (
    "This comment was posted automatically. Each later follow-up outcome edits it in place instead of "
    "adding another, and Factory never merges or closes a pull request."
)


def marker(pr: int) -> str:
    return f"{MARKER_PREFIX}pr={int(pr)} -->"


def short_head(head: object) -> str:
    """The first ``HEAD_SHOWN`` characters of a commit id, or "" for anything that is not lower-case hex."""
    return head[:HEAD_SHOWN] if isinstance(head, str) and _HEAD.fullmatch(head) else ""


def render(pr: int, outcome: str, head: object = "") -> str:
    """The reply body for one outcome. ``ValueError`` for an outcome without a reply or a body that breaks the rules."""
    if outcome not in REPLIES:
        raise ValueError("no reply for this outcome")
    headline, text = REPLIES[outcome]
    lines = [marker(pr), f"**Factory follow-up: {headline}**", "", text]
    shown = short_head(head) if outcome == followup.OUTCOME_PUSHED else ""
    if shown:
        lines += ["", f"New head: `{shown}`"]
    lines += ["", f"Code: `{outcome}`", "", FOOTER]
    body = "\n".join(lines)
    if len(body) > MAX_BODY or _FORBIDDEN.search(body[len(lines[0]) :]):
        raise ValueError("the reply breaks the template rules")
    return body


class FollowupReplies(IssueComments):
    """Posts or edits Factory's one reply per pull request, best effort and fenced, as ``reader``'s identity.

    ``reader`` is the manager's ``trigger.GithubReader`` of the repository: it supplies the identity of the
    writes (``call``), the bot user id (``app_user_id``), the login of an ambient identity (``login``) and the
    pull request's state (``pull_state``). One instance serves one repository for the life of the manager;
    ``new_cycle`` starts the next settlement pass and ``configure`` re-reads the switch.
    """

    def __init__(self, reader, slug, clock, env=None):
        super().__init__(None, slug, "", enabled=True, clock=clock, env=env)
        self.reader = reader
        self.env = env
        self.last_error = None  # class name of the last failure, for the caller's one fixed line
        self.skipped = None

    def configure(self, enabled):
        """The repository's ``followup_reply`` and the deployment-wide kill switch, as of this pass."""
        self.enabled = bool(enabled) and not switched_off(self.env)

    def new_cycle(self):
        """A new settlement pass: the per-cycle cap counts from zero again."""
        self.written = 0

    def reply(self, pr, outcome, head=""):
        """Make the pull request carry the reply for this outcome. Never raises; the result says what happened.

        ``replied`` (posted or edited), ``unchanged`` (already there, word for word), ``off``, ``cap`` or
        ``cooldown`` (nothing may be written now), ``closed`` (the pull request is not open) or ``failed``
        (``last_error`` names the exception class).
        """
        if type(pr) is not int or pr <= 0 or outcome not in REPLIES:
            return "off"
        with self.mutex:
            if not self.active:
                return self._refusal()
            self.last_error = self.skipped = None
            try:
                body = render(pr, outcome, head)
            except ValueError as error:
                self._failed(error)
                return "failed"
            key = (pr, KIND)
            # A long-lived process must not trust what it wrote earlier (a person may have deleted or edited
            # the comment since): every reply looks the comment up again, which is one call.
            self.ids.pop(key, None)
            self.done.pop(key, None)
            before = self.written
            if not self._attempt(self._place, pr, body):
                return "failed"
            if self.skipped is not None:
                return self.skipped
            return "replied" if self.written > before else "unchanged"

    def _refusal(self):
        if not self.enabled or self.lost:
            return "off"
        return "cap" if self.written >= self.cap else "cooldown"

    def _place(self, pr, body):
        if self.reader.pull_state(pr) != "open":
            self.skipped = "closed"
            return
        self._write(pr, KIND, body)

    # -- what differs from the issue comment ------------------------------------------------

    def _marker(self, issue, kind):
        return marker(issue)

    def _failed(self, error):
        self.last_error = type(error).__name__

    def _identity(self):
        return self.reader.app_user_id, True  # no App id: the login of the ambient ``gh`` user is asked

    def _find(self, path, wanted):
        if self._author_test() is None:
            raise LookupError("the identity of the writer is unknown")  # never post blind: a restart would duplicate
        return super()._find(path, wanted)

    def _gh(self, args):
        return self.reader.call(args)

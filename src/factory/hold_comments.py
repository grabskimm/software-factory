"""The one comment Factory leaves on each issue of a plan set when the set is held (``plan_holds``).

A hold keeps the trigger loop from planning a set whose plan failed the same way twice. Without a word on the issue nobody who
only reads GitHub can tell why nothing happens, so when the hold starts ``HoldComments.announce`` posts ONE comment on each
issue of the set from Factory's own identity. It is built on ``IssueComments`` (``issue_comments.py``, through ``FollowupReplies``'
reader plumbing) and so has the same marker search by author, failure cooldown, per-cycle cap, ``comment_on_blocked`` (through
``configure``) and ``FACTORY_ISSUE_COMMENTS=off``.

* **Fixed text.** The body is ``issue_comments.render_held``: the marker, the failure code from ``plan_failure.CODES`` and a fixed
  line saying that editing the issue or changing its labels unblocks it. No issue text, no model text.
* **Once.** The comment starts with a hidden marker (``kind=plan_held``). Before writing, the issue's comments are searched for a
  comment of Factory's own identity that starts with it: found with the same body, nothing is written; found with another code,
  that comment is edited; not found, one is posted. So a later failing pass, a hold that starts again after an edit, and a manager
  restart never add a second comment.
* **Best effort.** ``announce`` never raises; a failure is reported by its result (``failed`` and ``last_error``, the exception
  class name) for the caller's one fixed line. Closed and ``factory-ignore`` issues get nothing.
"""

from __future__ import annotations

from . import issue_comments
from .followup_reply import FollowupReplies
from .issue_comments import KIND_HELD, render_held

IGNORE_LABEL = "factory-ignore"  # the same name as issue_labels.IGNORE_LABEL (which imports issue_comments)


class HoldComments(FollowupReplies):
    """Posts Factory's one hold comment per issue, best effort and fenced, as the reader's identity."""

    def _marker(self, issue, kind):
        return issue_comments.marker(issue, kind)

    def announce(self, issue, code):
        """Make the issue carry the hold comment for ``code``. Never raises.

        ``written`` (posted or edited), ``unchanged`` (already there, word for word), ``off``, ``cap`` or ``cooldown`` (nothing may be
        written now), ``skipped`` (the issue is closed or ignored) or ``failed`` (``last_error`` names the exception class).
        """
        if type(issue) is not int or issue <= 0:  # type(), not isinstance: a bool is not an issue number
            return "off"
        with self.mutex:
            if not self.active:
                return self._refusal()
            self.last_error = self.skipped = None
            key = (issue, KIND_HELD)
            # A long-lived process must not trust what it wrote earlier (a person may have deleted the comment): look it up again.
            self.ids.pop(key, None)
            self.done.pop(key, None)
            before = self.written
            if not self._attempt(self._place_held, issue, render_held(issue, code)):
                return "failed"
            if self.skipped is not None:
                return self.skipped
            return "written" if self.written > before else "unchanged"

    def _place_held(self, issue, body):
        state, names = self.reader.issue_state(issue)
        if state != "open" or IGNORE_LABEL in {name.strip().casefold() for name in names}:
            self.skipped = "skipped"
            return
        self._write(issue, KIND_HELD, body)

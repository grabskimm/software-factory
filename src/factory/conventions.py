"""A repository's own pull request conventions: the title, the commit message and a squash-safe body.

By default Factory titles a pull request ``factory: <objective> (#N)``. A repository that releases from
its commit history (semantic-release, release-please, commitlint) needs a Conventional Commits title
instead, because under a squash merge the pull request title and description are the commit message the
release tool reads. ``pr_style = "conventional"`` in the repository's config selects that:

* the title is ``<type>: <subject> (#N)`` with ``<type>`` one of ``TYPES``. The type is the job's
  ``change_type`` (the planner copies it from the issue's own title prefix or picks it from the change);
  a job without one gets ``chore``, which releases nothing: a wrong type is never allowed to cut a version.
  The title never carries ``!``, so it never announces a breaking change on its own;
* the commit message is the same ``<type>: <subject>`` line;
* text a model wrote and that goes into the pull request body is made safe for a squash message: a line
  that a commit parser reads as a ``BREAKING CHANGE`` note (which would cut a major release), a CI-skip
  keyword (which would stop the merge commit's workflows) and a ``skip-checks:`` trailer are defused.

Nothing here reads or writes GitHub; the runner calls it with the job and the repository's config.
"""

import re

from .github import (
    TITLE_LIMIT,
    TITLE_PREFIX,
    _ELLIPSIS,
    _require_issues,
    issue_reference,
    neutralise_refs,
)

STYLES = ("factory", "conventional")
DEFAULT_STYLE = "factory"
# The types every Conventional Commits preset knows (conventionalcommits.org, @commitlint/config-conventional).
TYPES = ("feat", "fix", "docs", "style", "refactor", "perf", "test", "build", "ci", "chore", "revert")
FALLBACK_TYPE = "chore"
# Many commitlint configurations cap the header at 100 characters, so the conventional title stays within it.
CONVENTIONAL_LIMIT = 100

_TYPE_PREFIX = re.compile(r"^(?:" + "|".join(TYPES) + r")(?:\([^)\n]{0,40}\))?!?:\s+", re.IGNORECASE)
_ZWSP = "​"
# GitHub's five bracketed CI-skip forms (any case, spaces or tabs inside the brackets) ...
_SKIP_BRACKETED = re.compile(
    r"\[([ \t]*)(skip[ \t]+ci|ci[ \t]+skip|no[ \t]+ci|skip[ \t]+actions|actions[ \t]+skip)([ \t]*)\]",
    re.IGNORECASE | re.ASCII,
)
# ... and a line that starts with the ``skip-checks:`` trailer.
_SKIP_CHECKS = re.compile(r"^([ \t]*)(skip-checks):", re.IGNORECASE | re.ASCII | re.MULTILINE)
# A commit parser's breaking-change note: a line that starts with the keyword after only whitespace, ``|`` and ``*``.
# The whitespace is the one of a single LINE (JavaScript's ``\s`` without the line feed and carriage return): the
# parser splits the message into lines first, so a note never reaches back over a blank line.
_LINE_SPACE = "\t\v\f \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_BREAKING = re.compile(
    rf"^([{_LINE_SPACE}|*]*)(BREAKING)([ -])(CHANGE)(?=[:\s]|$)", re.IGNORECASE | re.ASCII | re.MULTILINE
)


def valid_style(value):
    return isinstance(value, str) and value in STYLES


def style_of(runner):
    """The convention of the repository a runner works on (``factory`` when its config does not say)."""
    style = getattr(getattr(runner, "cfg", None), "pr_style", DEFAULT_STYLE)
    return style if valid_style(style) else DEFAULT_STYLE


def change_type(job):
    """The job's Conventional Commits type, ``chore`` when the plan gave none (or one that is not a known type)."""
    value = job.get("change_type") if isinstance(job, dict) else None
    return value if isinstance(value, str) and value in TYPES else FALLBACK_TYPE


def _subject(objective):
    """One plain line from the job objective, without issue references or a type prefix of its own."""
    text = " ".join(neutralise_refs(objective).split())
    text = _TYPE_PREFIX.sub("", text, count=1).rstrip(" .")
    return text


def _cut(text, room):
    if len(text) <= room:
        return text
    if room <= 1:
        return ""
    cut = text[: room - 1]
    # End at a word boundary when one is near, so the title does not stop in the middle of a word.
    space = cut.rfind(" ")
    if space >= room // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:-") + _ELLIPSIS


def commit_subject(style, job, limit=CONVENTIONAL_LIMIT):
    """The first line of the job's commit: ``factory: <objective>`` or ``<type>: <subject>``."""
    objective = job["objective"]
    if style != "conventional":
        return f"{TITLE_PREFIX}{neutralise_refs(objective[:100])}"
    prefix = f"{change_type(job)}: "
    return prefix + _cut(_subject(objective), limit - len(prefix))


def pr_title(style, job, limit=None):
    """The pull request title: ``factory: <objective> (#N)`` or ``<type>: <subject> (#N)``."""
    from . import github

    if style != "conventional":
        return github.pr_title(job["objective"], job["issues"], limit=limit or TITLE_LIMIT)
    limit = min(limit or CONVENTIONAL_LIMIT, TITLE_LIMIT)
    suffix = " (" + ", ".join(issue_reference(i) for i in _require_issues(job["issues"])) + ")"
    prefix = f"{change_type(job)}: "
    room = limit - len(prefix) - len(suffix)
    subject = _cut(_subject(job["objective"]), room)
    return (prefix + subject).rstrip() + suffix


def squash_safe(text):
    """Model text with what a squash-merge message must not carry defused; every other character is kept.

    The breaking-change keyword gets a ``> `` quote marker (a parser only reads a note after whitespace,
    ``|`` and ``*``), a bracketed CI-skip form loses its brackets' meaning with a zero-width space after the
    ``[``, and a ``skip-checks:`` line gets a zero-width space before its colon.
    """
    plain = str(text)
    plain = _BREAKING.sub(lambda m: f"> {m.group(1)}{m.group(2)}{m.group(3)}{m.group(4)}", plain)
    plain = _SKIP_BRACKETED.sub(lambda m: f"[{_ZWSP}{m.group(1)}{m.group(2)}{m.group(3)}]", plain)
    return _SKIP_CHECKS.sub(lambda m: f"{m.group(1)}{m.group(2)}{_ZWSP}:", plain)

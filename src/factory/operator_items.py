"""The "Needs a person" checklist: ``operator`` criteria as task-list items in the PR body.

An acceptance criterion tagged ``operator`` can only be proven by a person (production
behaviour, a paid step, credentials the sandbox must not have). Factory lists each one as an
unchecked task under a fixed heading, with the reason it cannot be proven automatically, and
nothing else: the list is information for the reviewer. It never makes verification
incomplete, never decides whether the job is ready and never touches ``factory/gate``.

* Every item carries a **stable id** derived from the criterion text (not from its position), in
  a hidden marker at the end of its line, so rebuilding the body keeps the item's identity.
* **Factory never ticks an item.** When it rebuilds the body (a retry, a re-publish) it reads
  the live pull request body and carries over only the ticks a person made, and only for ids
  that the plan still has. The reader is strict: whole lines of the last checklist section, a
  known id, nothing else, and only when that section is the final block of the body (Factory
  writes it last; anything that follows the list makes the reader ignore every tick).
* Criterion text and reasons are untrusted model output. They are flattened to one printable
  line, ``<`` and ``>`` are escaped (so they cannot forge a marker or a comment), and issue
  references are neutralised, exactly like every other agent text in the body.
* The free-form sections written from model output (the summary, review findings, paths, the
  criteria in ``Acceptance``) go through ``agent_text``: no line of it can start a second
  ``Needs a person`` heading or a task-list item, and no ``<!--`` can open a comment that hides
  the real section. The real checklist stays the only one.

Everything here is a pure function of its arguments; nothing runs or writes anything.
"""

import hashlib
import re

from .github import neutralise_refs
from .model import MAX_REASON, criteria, criterion

HEADING = "## Needs a person"
MARKER = "factory:operator-item:"
DEFAULT_REASON = "Only a person can prove this."
MAX_TEXT = 400

_ZWSP = "\u200b"
_HEADING = re.compile(rf"^{re.escape(HEADING)}[ \t]*$", re.MULTILINE)
_ITEM = re.compile(
    r"^[ \t]*[-*+][ \t]+\[([ xX])\][ \t].*"
    rf"<!-- {re.escape(MARKER)}(op-[0-9a-f]{{10}}(?:-[0-9]{{1,3}})?) -->[ \t]*$"
)
# What can sit in front of the text of a line without changing what the line is: blockquote
# marks, indentation and list markers (``- ``, ``* ``, ``+ ``, ``1. ``, ``1) ``).
_LEAD = re.compile(r"(?:[ \t>]|[-*+](?=[ \t])|[0-9]{1,9}[.)](?=[ \t]))*")
# A line that opens another block (a heading or an HTML comment) is never part of the intro of the
# checklist, so a heading copy followed by one is not read.
_OPENS_BLOCK = re.compile(r"[ \t]*(#{1,6}[ \t]|<!--)")
_FORGED_HEADING = re.compile(r"#{1,6}[ \t]*needs[ \t]+a[ \t]+person", re.IGNORECASE)
_TASK_MARK = re.compile(r"\[[ xX]\](?=[ \t]|$)")
# CommonMark ends a line at \n, \r or \r\n; str.splitlines splits on more, so split by hand.
_LINE_END = re.compile(r"(\r\n|\r|\n)")


def defuse(value):
    """Agent text that cannot start the checklist heading, a task-list item or a comment.

    A line whose text (after blockquote marks, indentation and list markers) begins with a
    ``Needs a person`` heading or a ``[ ]`` / ``[x]`` task mark gets a zero-width space in
    front of that text, so a markdown renderer shows plain text and ``ticked_ids`` sees no
    heading or item; ``<!--`` gets one inside it, so it opens no comment. Applied to the
    start of ``value`` too, because callers put it behind ``- `` in a list. Nothing else
    changes, and the text reads the same.
    """
    parts = _LINE_END.split(str(value))
    for index in range(0, len(parts), 2):
        line = parts[index]
        start = _LEAD.match(line).end()
        rest = line[start:]
        if _FORGED_HEADING.match(rest) or _TASK_MARK.match(rest):
            parts[index] = f"{line[:start]}{_ZWSP}{rest}"
    return "".join(parts).replace("<!--", f"<{_ZWSP}!--")


def agent_text(value):
    """Model output as safe pull request body text: ``neutralise_refs`` and ``defuse``.

    Issue references cannot close an issue and the text cannot forge the ``Needs a person``
    section; newlines and markdown are otherwise kept.
    """
    return defuse(neutralise_refs(value))


def flatten(value, limit=MAX_TEXT):
    """Untrusted text as one safe printable line, at most ``limit`` characters before escaping.

    Control and invisible characters and line breaks become single spaces, ``<`` and ``>`` are
    escaped, and issue references are neutralised, so the text can neither start a new line of
    the body nor forge a ``<!-- ... -->`` marker.
    """
    plain = "".join(c if c.isprintable() else " " for c in str(value))
    plain = " ".join(plain.split())
    if len(plain) > limit:
        plain = f"{plain[: limit - 3].rstrip()}..."
    plain = plain.replace("<", "&lt;").replace(">", "&gt;")
    return neutralise_refs(plain)


def operator_items(job):
    """The job's ``operator`` criteria in plan order: ``{id, text, reason}`` each.

    The id is ``op-`` and ten hex digits of the SHA-256 of the whitespace-normalised text, so it
    does not depend on the criterion's position or on the other criteria; a repeated text gets a
    ``-2``, ``-3`` suffix. ``reason`` is the plan's (cut to ``MAX_REASON`` by
    ``model.criterion``) and falls back to a fixed sentence when the plan gave none or one that
    shows nothing.
    """
    items = []
    seen = {}
    for item in criteria(job):
        if item["tag"] != "operator":
            continue
        digest = hashlib.sha256(" ".join(item["text"].split()).encode()).hexdigest()
        base = f"op-{digest[:10]}"
        seen[base] = seen.get(base, 0) + 1
        items.append(
            {
                "id": base if seen[base] == 1 else f"{base}-{seen[base]}",
                "text": item["text"],
                "reason": item.get("reason") or DEFAULT_REASON,
            }
        )
    return items


def without_operator(job):
    """``(job, texts)``: the job minus its operator criteria, and those criteria's texts.

    Used for the prompts of the phases that decide whether a job is complete (review and
    verification): an operator criterion is never theirs to prove or to fail on. The job is
    returned unchanged (same object) when it has no operator criterion.
    """
    kept = []
    texts = []
    for raw in job.get("acceptance") or []:
        canonical = criterion(raw)
        if canonical["tag"] == "operator":
            texts.append(canonical["text"])
        else:
            kept.append(raw)
    if not texts:
        return bounded(job), []
    return bounded({**job, "acceptance": kept}), texts


def bounded(job):
    """``job`` with every criterion's ``reason`` as ``model.criterion`` keeps it (cut, or dropped).

    A reason is no longer refused when it is over-long or sits on a tag that takes none, so the
    raw plan can still hold one of any length. A prompt must not carry it: the reason is replaced
    by its canonical form, or removed. The same object comes back when nothing needs changing.
    """
    items = job.get("acceptance")
    if not isinstance(items, list):
        return job
    out = []
    changed = False
    for raw in items:
        if isinstance(raw, dict) and "reason" in raw:
            copy = {key: value for key, value in raw.items() if key != "reason"}
            reason = criterion(raw).get("reason")
            if reason is not None:
                copy["reason"] = reason
            out.append(copy)
            changed = changed or copy != raw
        else:
            out.append(raw)
    return {**job, "acceptance": out} if changed else job


def ticked_ids(body, valid_ids):
    """The ids a person has ticked in ``body``'s checklist, restricted to ``valid_ids``.

    ``body`` is the live pull request body (untrusted: anyone who may edit it, and any text an
    agent put in it, is just data). Only lines of the last ``Needs a person`` section count,
    each as ``- [x] ... <!-- factory:operator-item:ID -->`` (``[X]`` and CRLF line ends, which
    GitHub writes when a body is edited in the browser, are accepted); an id that appears on
    several lines is ticked only if every line is. Anything else, a missing or non-string body
    included, yields no tick.

    That section must also be the final block of the body: after the intro lines that follow
    the heading, nothing but item lines (of any id, ticked or not) and blank lines may come
    before the end (a line that opens another block, a heading or an HTML comment, among the intro
    lines also yields no tick). Factory writes the checklist last (``Runner.pr_body``), after sections that
    are never item lines, so a heading that sits anywhere else, for example a copy left behind
    when the real section was deleted or renamed, is followed by text that is not an item and
    yields no tick. The cost is that text a person or a bot appends after the list makes the
    reader ignore every tick until it is removed (the ticks are dropped, never invented).

    Limits that stay: the body can be edited by anyone with write access to the pull request,
    so a tick proves what the editing account claimed and not who it was; a tick made on one
    head carries over to a later head, because Factory does not know what the person looked at;
    and a tick made between Factory's read and its edit of the body is overwritten. A tick is
    information for the next body only, never an input to ``factory/gate``.
    """
    if not isinstance(body, str):
        return frozenset()
    valid = set(valid_ids)
    text = body.replace("\r\n", "\n").replace("\r", "\n")
    headings = list(_HEADING.finditer(text))
    if not headings or not valid:
        return frozenset()
    states = {}
    in_items = False
    for line in text[headings[-1].end():].split("\n"):
        if not line.strip():
            continue
        found = _ITEM.match(line)
        if found is None:
            if in_items or _OPENS_BLOCK.match(line):
                return frozenset()
            continue
        in_items = True
        if found.group(2) in valid:
            states.setdefault(found.group(2), []).append(found.group(1) != " ")
    return frozenset(item for item, marks in states.items() if all(marks))


def render_checklist(items, ticked=(), only_operator=False):
    """The ``Needs a person`` section for ``items``; the empty string when there are none.

    ``- [x]`` is written only for an id in ``ticked`` (what ``ticked_ids`` found in the live
    body); Factory never produces a tick of its own. ``only_operator`` says that the job has
    no other criterion, so Factory proved none of them itself.
    """
    if not items:
        return ""
    ticked = set(ticked)
    lines = [
        HEADING,
        "",
        "Factory could not prove these automatically and never ticks them: a person checks each "
        "one and ticks it here. They are information for the reviewer and do not block this "
        "pull request or change its `factory/gate` status.",
    ]
    if only_operator:
        lines += [
            "",
            "Every acceptance criterion of this change is on this list, so Factory proved none "
            "of them itself.",
        ]
    lines.append("")
    for item in items:
        mark = "x" if item["id"] in ticked else " "
        lines.append(
            f"- [{mark}] {flatten(item['text'])} (why: {flatten(item['reason'], MAX_REASON)}) "
            f"<!-- {MARKER}{item['id']} -->"
        )
    return "\n".join(lines)


def checklist_for(job, previous_body=None):
    """The ``Needs a person`` section of a job's body (empty without operator criteria).

    ``previous_body`` is the live pull request body of an earlier publish, if any; the ticks a
    person made in it are carried over for the ids the job still has.
    """
    items = operator_items(job)
    ticked = ticked_ids(previous_body, [item["id"] for item in items])
    only = bool(items) and all(c["tag"] == "operator" for c in criteria(job))
    return render_checklist(items, ticked, only)

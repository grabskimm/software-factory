"""The "Where the checks ran" block of a pull request body, and its CI part updated in place.

Issue #37 asks that the pull request body state exactly which checks ran where. The block has three
bullets, one per place a criterion can be proven (``model.criterion`` tags: ``sandbox``, ``ci``,
``operator``):

* **Sandbox**: the worker session's own run: the test command, the base commit, the baseline
  comparison status and the failure counts (``baseline.apply`` is what decided them);
* **CI**: the required checks of the base branch, which run on GitHub, never in Factory. At publish
  time the bullet names the checks ``factory/gate`` awaits; when ``ci_gate.resolve_pending``
  resolves the gate it replaces the bullet, and only the bullet, with what it read: the check
  names, their conclusions and the head commit;
* **Operator**: the items of the ``Needs a person`` checklist, which only a person can prove.

Every word is built here from fixed text, counts, hashes and the plan's own data. Check names and the
test command are untrusted text (GitHub, the verifier): they go through ``operator_items.flatten``
(control characters blanked, cut, ``<`` and ``>`` escaped, issue references broken) inside an inline
code span that holds no backtick, and a conclusion is one word of a fixed vocabulary, so none of
them can start a heading, a task item or a hidden marker. Lists are bounded (``MAX_CHECKS``,
``MAX_OPERATOR``); the record on disk keeps everything.

The CI bullet sits between two fixed HTML comment markers. ``replace_ci`` swaps what is between them
and leaves every other byte of the body alone (a person's ticks in the ``Needs a person`` section
included); it refuses a body that does not hold the pair exactly once, so an edited or older body is
never appended to. Agent text cannot forge a marker: ``operator_items.defuse`` breaks every ``<!--``.

Everything here is a pure function of its arguments; nothing reads or writes GitHub.
"""

import re

from .baseline import parse_test_run
from .github import GATE_CONTEXT, GREEN_BUCKETS
from .operator_items import flatten, operator_items

CI_BEGIN = "<!-- factory:ci-evidence:begin -->"
CI_END = "<!-- factory:ci-evidence:end -->"
HEADING = "**Where the checks ran**"
MAX_CHECKS = 20  # check names shown; then "and N more"
MAX_OPERATOR = 10  # operator items shown; then "and N more"
MAX_NAME = 80
MAX_COMMAND = 160
BUCKETS = ("pass", "fail", "pending", "skipping", "cancel")  # what ``gh pr checks`` can say
OUTCOMES = {
    "success": "every required check passed",
    "failure": "a required check failed",
    "timeout": "timed out waiting for the required checks",
}

_HEX = re.compile(r"[0-9a-f]{7,64}")
_OPERATOR_LABEL = "**Operator** (only a person can prove these, on a live system; Factory proved none of them)"


def _code(value, limit):
    """Untrusted text as one inline code span: flattened, cut and free of backticks."""
    plain = flatten(value, limit).replace("`", "'")
    return f"`{plain}`"


def _names(names):
    """``a``, ``b`` and ``and N more`` for the first ``MAX_CHECKS`` names."""
    shown = ", ".join(_code(name, MAX_NAME) for name in names[:MAX_CHECKS])
    more = len(names) - MAX_CHECKS
    return f"{shown}, and {more} more" if more > 0 else shown


def _commit(value, size=12):
    """A commit id cut to ``size`` hex digits, or ``unknown`` for anything that is not one."""
    text = str(value or "")
    return text[:size] if _HEX.fullmatch(text) else "unknown"


def _test_command(verification):
    try:
        return parse_test_run(verification.get("test_run"))["command"]
    except ValueError:
        return None


def _failing_ids(verification):
    try:
        return parse_test_run(verification.get("test_run"))["ids"]
    except ValueError:
        return []


def sandbox_line(verification, sha):
    """The sandbox bullet: what the worker session ran, and what the baseline comparison made of it."""
    facts = verification if isinstance(verification, dict) else {}
    label = "- **Sandbox** (a worker session: no network access, a read-only temp area)"
    if facts.get("skipped") is True:
        return (
            f"{label}: verification did not run for this job (`verify.when`), so no test ran "
            "and nothing is proven here."
        )
    verdict = "complete" if facts.get("complete") is True else "incomplete"
    parts = [f"verdict {verdict} on commit `{_commit(sha)}`"]
    record = facts.get("baseline")
    record = record if isinstance(record, dict) else {}
    command = record.get("command") or _test_command(facts)
    if not isinstance(command, str) or not command.strip():
        parts.append(
            "the verifier reported no structured test run, so there is no baseline comparison"
        )
        return f"{label}: {'; '.join(parts)}."
    parts.append(f"test command {_code(command, MAX_COMMAND)}")
    status = record.get("status")
    if status == "compared":
        new = record.get("new_failures")
        environmental = record.get("environmental_failures")
        new = len(new) if isinstance(new, list) else 0
        environmental = len(environmental) if isinstance(environmental, list) else 0
        parts.append(
            f"baseline: the same command at base commit `{_commit(record.get('base'))}` "
            f"compared; {new + environmental} failing on the head, {new} new on the head, "
            f"{environmental} environmental (they fail on the base too)"
        )
    elif status == "fallback":
        parts.append(
            "baseline: the run at the base commit did not decide, so the verifier's verdict stands"
        )
    elif _failing_ids(verification):
        parts.append("baseline: no comparison was recorded, so the failing tests were not compared with the base")
    else:
        parts.append(
            "baseline: not run (the verifier reported no failing test, so there was nothing to compare)"
        )
    return f"{label}: {'; '.join(parts)}."


def _ci_label():
    return (
        "**CI** (the base branch's required checks, run by GitHub on the exact head; "
        "Factory reads them and runs none)"
    )


def ci_awaiting(awaited, ready):
    """The CI block of a body that was just published: the checks the gate waits for, by name."""
    if awaited:
        text = (
            f"awaiting on this head: {_names(awaited)}. `{GATE_CONTEXT}` stays pending until "
            "they pass, and this block is then replaced by what Factory read."
        )
    elif ready:
        text = (
            f"`{GATE_CONTEXT}` waits for no check here, because no `ci` criterion relies on one. "
            "The repository's required checks still gate the merge on GitHub."
        )
    else:
        text = (
            f"`{GATE_CONTEXT}` does not wait for CI on this head, because it is failing for "
            "another reason (above). The repository's required checks still gate the merge on GitHub."
        )
    return f"{CI_BEGIN}\n- {_ci_label()}: {text}\n{CI_END}"


def conclusions(checks):
    """``[(name, bucket)]`` sorted by name, the gate itself left out, one bucket per name.

    A name reported more than once is ``fail`` if any entry is, else the first bucket that is not
    green, else ``skipping`` only when every entry was skipped, else ``pass``. A bucket outside the
    fixed vocabulary reads ``unknown``: no text of GitHub's reaches the body but the name.
    """
    seen = {}
    for check in checks or ():
        name = check.get("name") if isinstance(check, dict) else None
        if isinstance(name, str) and name and name != GATE_CONTEXT:
            seen.setdefault(name, []).append(check.get("bucket"))
    found = []
    for name in sorted(seen):
        buckets = seen[name]
        if "fail" in buckets:
            bucket = "fail"
        else:
            bucket = next((b for b in buckets if b not in GREEN_BUCKETS), None)
            if bucket is None:
                bucket = "skipping" if all(b == "skipping" for b in buckets) else "pass"
        found.append((name, bucket if bucket in BUCKETS else "unknown"))
    return found


def ci_result(commit, outcome, checks, awaited):
    """The CI block once the gate resolved: the head, the outcome, each check and its conclusion.

    ``outcome`` is ``success``, ``failure`` or ``timeout`` (the gate's own words). ``checks`` are the
    required checks as ``gh pr checks --required`` printed them for that head.
    """
    verdict = OUTCOMES.get(outcome, "unknown outcome")
    lines = [
        f"- {_ci_label()}: {verdict} on head `{_commit(commit, 40)}`. `{GATE_CONTEXT}` is the "
        "authority; this block reports what Factory read."
    ]
    found = conclusions(checks)
    for name, bucket in found[:MAX_CHECKS]:
        lines.append(f"  - {_code(name, MAX_NAME)}: {bucket}")
    if len(found) > MAX_CHECKS:
        lines.append(f"  - and {len(found) - MAX_CHECKS} more")
    if not found:
        lines.append("  - no required check had reported on this head")
    if awaited:
        lines.append(f"  - awaited by the gate: {_names(sorted(awaited))}")
    return "\n".join([CI_BEGIN, *lines, CI_END])


def operator_block(job):
    """The operator bullet: the checklist items by id and text (the ticks stay in the checklist)."""
    items = operator_items(job)
    if not items:
        return "- **Operator** (a person, on a live system): no criterion of this job needs one."
    lines = [
        f"- {_OPERATOR_LABEL}: {len(items)} item(s), in the checklist at the end of this body."
    ]
    for item in items[:MAX_OPERATOR]:
        lines.append(f"  - `{item['id']}`: {flatten(item['text'], 120)}")
    if len(items) > MAX_OPERATOR:
        lines.append(f"  - and {len(items) - MAX_OPERATOR} more")
    return "\n".join(lines)


def render(job, verification, sha, awaited, ready):
    """The whole block for a body being published (the CI bullet shows what is awaited)."""
    return "\n".join(
        [
            HEADING,
            "",
            sandbox_line(verification, sha),
            ci_awaiting(awaited, ready),
            operator_block(job),
            "",
            "",
        ]
    )


def replace_ci(body, block):
    """``body`` with the CI block replaced; None unless the body holds the marker pair exactly once.

    Only the text from the begin marker to the end marker changes. A body without the pair (an older
    one, or one edited by hand), with a repeated or reversed pair is left alone by the caller.
    """
    if (
        not isinstance(body, str)
        or body.count(CI_BEGIN) != 1
        or body.count(CI_END) != 1
        or body.index(CI_BEGIN) > body.index(CI_END)
    ):
        return None
    start = body.index(CI_BEGIN)
    stop = body.index(CI_END) + len(CI_END)
    return f"{body[:start]}{block}{body[stop:]}"

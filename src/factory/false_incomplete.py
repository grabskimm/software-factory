"""The false-incomplete rate of a repository: is verification crying wolf?

Of the pull requests Factory published as incomplete or pending (``pr_incomplete``, a ``factory/gate`` that is
a failure, or a pending gate that waits for CI), how many did a person merge **unchanged**, with the very head
commit Factory published? A high share means the incomplete verdicts were not worth stopping for.

Read-only and restart-safe: the inputs are GitHub (the Factory App's closed pull requests, their head SHAs and
the ``factory/gate`` commit statuses of the commits) and nothing else. The run directories cannot be the source:
the janitor purges terminal runs, and those are exactly the merged ones. What Factory published (the verdict and
its ``[reason code]`` suffix, ``reason_codes.suffix``) is read from the statuses Factory itself wrote, so the
window never loses a merged run. Nothing is written anywhere, and a result is rebuilt from scratch on every call.

Definitions (docs/operations.md, "False-incomplete rate"):

* The **window** is the last ``WINDOW_DAYS`` days by merge time.
* A pull request is **counted** when it was merged in the window and the gate history of the commit Factory
  published starts as a verdict that held it back (``failure``, or ``pending`` that is not a mere transient
  note) and was not later resolved by Factory's own CI check (a pending gate that the required checks turned
  green is the design working, not a wolf cry). The **published commit** is the newest commit of the pull
  request with such a gate history written by the Factory App: the head if it has one, else the nearest
  earlier commit, looking back at most ``MAX_COMMITS_BACK`` commits.
* **Merged unchanged**: the head at merge is the published commit. **Merged changed**: it is a later one
  (someone pushed after the verdict). The rate is ``merged_unchanged / counted``.
* **Closed without merging** is excluded from the rate and from every other count except ``closed_unmerged``:
  closing says nothing about whether the verdict was right (the work may have been wrong, replaced or
  abandoned), and it costs no GitHub call beyond the list.
* A status without a ``[code]`` suffix (published before reason codes existed) is counted in the totals and
  in ``uncoded``, in no per-code row.
* A ``workflow_hold`` is a deliberate hold that a person always lifts (the head changes workflow files), so
  it is merged unchanged by design. It has its own ``by_reason`` row and ``counts.workflow_hold``, and is
  left out of the headline ``counted``/``rate``, which the 10 %/25 % targets apply to.
* A merged pull request whose head carries no Factory verdict and whose published commit is not within
  ``MAX_COMMITS_BACK`` commits below it (a long tail of later pushes, or a force-push that rewrote the
  commits) cannot be classified: it may have been a verdict overruled, or never held back. It is counted in
  ``counts.unresolved`` and in no rate. Every unchanged merge is found (its verdict is on the head), so a
  large ``unresolved`` means the rate leans high: read it as an upper bound then.
* Below ``MIN_SAMPLE`` counted pull requests the rate is withheld (``None``) and ``sample`` says why.

Only ids, counts, times and fixed words leave this module: never an issue, criterion, PR, commit or status
text. A status description is parsed for its prefix and its code only.
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable

from . import reason_codes
from .github import GATE_CONTEXT

WINDOW_DAYS = 30
MIN_SAMPLE = 10
MAX_PULLS = 100  # merged Factory pull requests examined per computation, newest first
MAX_COMMITS_BACK = 5  # commits below the head looked at for the published one

# Descriptions Factory posts that are not a verdict (runner.Runner.publish and sync_branch): the gate is
# ``pending`` while a job is being published or after a base merge, before review and verification speak.
TRANSIENT_PREFIX = "publishing @"
TRANSIENT_SYNC = "review and verification pending"
# The success ``ci_gate.resolve_pending`` posts when the required checks turned green on the exact head.
CI_RESOLVED_PREFIX = "review+verify+required CI passed"

OK = "ok"
CHECKING = "checking"
STALE = "stale"
UNAVAILABLE = "unavailable"
NO_APP = "no GitHub App slug"
REFRESH_FAILED = "refresh failed"

SAMPLE_OK = "ok"
SAMPLE_EMPTY = "empty"
SAMPLE_BELOW_MINIMUM = "below_minimum"

_CODE = re.compile(r" \[([a-z_]+)\]$")


def _time(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(text: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def factory_creator(login: str, user_id: int | None) -> Callable[[dict], bool]:
    """``creator -> bool``: True only for the Factory App's own bot user.

    With the App's user id known it must match; otherwise the bot login must (never the status text alone).
    """
    if user_id is not None:
        return lambda creator: creator.get("type") == "Bot" and creator.get("id") == user_id
    return lambda creator: (
        creator.get("type") == "Bot" and str(creator.get("login", "")).casefold() == login.casefold()
    )


def code_of(description: str) -> str | None:
    """The reason code in a gate description's `` [code]`` suffix, or None (none, or not one of ``ORDER``)."""
    found = _CODE.search(description) if isinstance(description, str) else None
    return found.group(1) if found and found.group(1) in reason_codes.ORDER else None


def _verdicts(history: list, is_factory: Callable[[dict], bool]) -> list:
    """Factory's own non-transient gate statuses, oldest first."""
    return [
        status
        for status in reversed(history)
        if is_factory(status.creator)
        and not (
            status.state == "pending"
            and (status.description.startswith(TRANSIENT_PREFIX) or TRANSIENT_SYNC in status.description)
        )
    ]


def classify(history: list, is_factory: Callable[[dict], bool]) -> tuple | None:
    """``None`` when Factory published no verdict on this commit, else ``(counted, code)``.

    ``history`` is the commit's ``factory/gate`` statuses, newest first. ``counted`` is True when the first
    verdict held the job back and no later success came from the required checks. ``code`` is the reason code
    of the newest held-back verdict that has one (None when none has).
    """
    verdicts = _verdicts(history, is_factory)
    if not verdicts:
        return None
    first = verdicts[0]
    held_back = first.state != "success"
    resolved = any(
        later.state == "success" and later.description.startswith(CI_RESOLVED_PREFIX) for later in verdicts[1:]
    )
    codes = [code_of(s.description) for s in reversed(verdicts) if s.state != "success"]
    return held_back and not resolved, next((code for code in codes if code), None)


def _row(unchanged: int, changed: int, min_sample: int) -> dict:
    counted = unchanged + changed
    if counted == 0:
        sample, rate = SAMPLE_EMPTY, None
    elif counted < min_sample:
        sample, rate = SAMPLE_BELOW_MINIMUM, None
    else:
        sample, rate = SAMPLE_OK, round(unchanged / counted, 4)
    return {
        "counted": counted,
        "merged_unchanged": unchanged,
        "merged_changed": changed,
        "rate": rate,
        "sample": sample,
    }


def summarize(
    outcomes: list,
    closed_unmerged: int,
    start: datetime,
    end: datetime,
    truncated: bool,
    min_sample: int = MIN_SAMPLE,
    unresolved: int = 0,
) -> dict:
    """The ``GET /repos`` value from ``outcomes``: ``[(unchanged, code or None)]`` of each counted pull request."""
    per_code = {}
    for code in reason_codes.ORDER:
        same = sum(1 for flag, name in outcomes if name == code and flag)
        other = sum(1 for flag, name in outcomes if name == code and not flag)
        per_code[code] = _row(same, other, min_sample)
    # The headline leaves out the deliberate workflow holds (a person lifts every one): see the module docstring.
    headline = [(same, name) for same, name in outcomes if name != reason_codes.WORKFLOW_HOLD]
    unchanged = sum(1 for same, _ in headline if same)
    total = _row(unchanged, len(headline) - unchanged, min_sample)
    return {
        "state": OK,
        "window": {"from": _time(start), "to": _time(end), "days": WINDOW_DAYS},
        "min_sample": min_sample,
        "counts": {
            "counted": total["counted"],
            "merged_unchanged": total["merged_unchanged"],
            "merged_changed": total["merged_changed"],
            "closed_unmerged": closed_unmerged,
            "unresolved": unresolved,
            "workflow_hold": len(outcomes) - len(headline),
            "uncoded": sum(1 for _, name in headline if name is None),
        },
        "rate": total["rate"],
        "sample": total["sample"],
        "truncated": truncated,
        "by_reason": per_code,
    }


def compute(
    reader: object,
    login: str,
    user_id: int | None,
    now: datetime,
    *,
    days: int = WINDOW_DAYS,
    min_sample: int = MIN_SAMPLE,
    max_pulls: int = MAX_PULLS,
    stop: Callable[[], bool] = lambda: False,
) -> dict:
    """The false-incomplete summary of one repository, read from GitHub through ``reader`` (read methods only).

    ``reader`` offers ``closed_pulls(login, since)``, ``pull_commits(number)`` and ``gate_history(sha, context)``
    (``trigger.GithubReader``); ``login`` is the Factory App's bot login and ``user_id`` its user id or None.
    A GitHub error propagates: the caller keeps its previous value and says the refresh failed.
    """
    start = now - timedelta(days=days)
    is_factory = factory_creator(login, user_id)
    listed, truncated = reader.closed_pulls(login, _time(start))
    outcomes: list = []
    closed_unmerged = 0
    merged = []
    for pull in listed:
        if pull.merged_at:
            when = _parse(pull.merged_at)
            if when is not None and start <= when <= now:
                merged.append((when, pull))
        else:
            when = _parse(pull.closed_at)
            if when is not None and start <= when <= now:
                closed_unmerged += 1
    unresolved = 0
    merged.sort(key=lambda item: item[0], reverse=True)
    if len(merged) > max_pulls:
        merged, truncated = merged[:max_pulls], True
    for _, pull in merged:
        if stop():
            raise InterruptedError("stopping")
        found = _published(reader, pull, is_factory)
        if found is None:
            unresolved += 1  # no verdict on the head or within the look-back: counted apart, in no rate
            continue
        sha, verdict = found
        if verdict[0]:
            outcomes.append((sha == pull.head, verdict[1]))
    return summarize(outcomes, closed_unmerged, start, now, truncated, min_sample, unresolved)


def _published(reader: object, pull: object, is_factory: Callable[[dict], bool]) -> tuple | None:
    """``(published commit, classify() result)`` for a merged pull request, or None when it cannot be found."""
    verdict = classify(reader.gate_history(pull.head, GATE_CONTEXT), is_factory)
    if verdict is not None:
        return pull.head, verdict
    below = [sha for sha in reversed(reader.pull_commits(pull.number)) if sha != pull.head]
    for sha in below[:MAX_COMMITS_BACK]:
        verdict = classify(reader.gate_history(sha, GATE_CONTEXT), is_factory)
        if verdict is not None:
            return sha, verdict
    return None


class FalseIncompleteCache:
    """What ``GET /repos`` reports as ``false_incomplete`` for each repository, kept in memory.

    Filled by the janitor sweep (``Operations``); a manager restart starts empty (``checking``) and the next
    sweep rebuilds it from GitHub. Reading never calls GitHub. A failed refresh keeps the last good value
    marked ``stale``; with none it is ``unavailable`` with a fixed reason.
    """

    def __init__(self) -> None:
        self._mutex = threading.Lock()
        self._values: dict[str, dict] = {}

    def get(self, repo: str) -> dict:
        with self._mutex:
            value = self._values.get(repo)
        return _copy(value) if value is not None else {"state": CHECKING}

    def set(self, repo: str, value: dict) -> None:
        with self._mutex:
            self._values[repo] = _copy(value)

    def failed(self, repo: str, reason: str = REFRESH_FAILED) -> None:
        with self._mutex:
            previous = self._values.get(repo)
            if previous is not None and previous.get("state") in (OK, STALE):
                self._values[repo] = {**_copy(previous), "state": STALE}
            else:
                self._values[repo] = {"state": UNAVAILABLE, "reason": reason}


def _copy(value: dict) -> dict:
    return {key: (_copy(item) if isinstance(item, dict) else item) for key, item in value.items()}

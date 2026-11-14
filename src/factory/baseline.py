"""Baseline comparison for verification: failures that also fail on the base are environmental.

The verifier reports the exact test command it ran on the head and the ids of the tests that
failed or errored (``test_run``). Factory runs that same command at the job's recorded base
commit in a sandboxed worker session (``Runner.baseline_failures``), never in the supervisor,
and counts only head failures missing from the baseline as real.

This module is pure data handling: parsing, the cache and the set difference. The base commit
comes from the job state and the command from the verifier's structured output, never from
issue text.
"""

import hashlib
import json
from pathlib import Path

from .state import atomic_json

MAX_COMMAND = 2000
MAX_IDS = 5000
MAX_ID = 500
MAX_LISTED = 50  # ids named in the blocker; the baseline record keeps them all
# What the PR body prints (the record on disk keeps every id): at most MAX_RENDERED ids of a list,
# each cut to MAX_SHOWN_ID characters, then "and N more"; the JSON dump of the verification verdict
# is cut at MAX_DUMP characters as a last bound, so a body never nears GitHub's 65,536-character limit.
MAX_RENDERED = 20
MAX_SHOWN_ID = 200
MAX_DUMP = 16000


class BaselineError(Exception):
    """The baseline could not be established; the message is the recorded fallback reason."""


def _ids(value, label):
    if value is None:
        return []
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() or len(item) > MAX_ID for item in value
    ):
        raise ValueError(f"{label} must be a list of test ids")
    return value


def parse_test_run(raw):
    """``{"command", "ids", "sole_blocker"}`` from the structured test run; ValueError if unusable."""
    if not isinstance(raw, dict):
        raise ValueError("test_run must be an object")
    command = raw.get("command")
    if (
        not isinstance(command, str)
        or not command.strip()
        or len(command) > MAX_COMMAND
        or "\0" in command
    ):
        raise ValueError("test_run.command must be a non-empty command string")
    ids = sorted(
        {i.strip() for i in _ids(raw.get("failing"), "test_run.failing")}
        | {i.strip() for i in _ids(raw.get("erroring"), "test_run.erroring")}
    )
    if len(ids) > MAX_IDS:
        raise ValueError("test_run lists too many test ids")
    return {
        "command": command.strip(),
        "ids": ids,
        "sole_blocker": raw.get("sole_blocker") is True,
    }


def parse_baseline_result(result, command):
    """The failing and erroring ids of a finished baseline session; BaselineError otherwise."""
    if not isinstance(result, dict) or type(result.get("complete")) is not bool:
        raise BaselineError("baseline result was not a verification object")
    if not result["complete"]:
        why = str(result.get("blocker") or "").strip()
        raise BaselineError(
            "baseline run did not finish" + (f": {why[:300]}" if why else "")
        )
    try:
        parsed = parse_test_run(result.get("test_run"))
    except ValueError as error:
        raise BaselineError(f"baseline result unparseable: {error}") from None
    if parsed["command"] != command:
        raise BaselineError("baseline result is for a different command")
    return parsed["ids"]


def compare(head_ids, base_ids):
    """``(new, environmental)``: head failures absent from / present in the baseline."""
    base = set(base_ids)
    head = set(head_ids)
    return sorted(head - base), sorted(head & base)


def cache_key(repo, base, command):
    payload = json.dumps([repo, base, command], separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


class Cache:
    """Baseline results per (repository, base commit, test command), one file per key."""

    def __init__(self, directory, repo):
        self.directory, self.repo = Path(directory), repo

    def path(self, base, command):
        return self.directory / f"{cache_key(self.repo, base, command)}.json"

    def load(self, base, command):
        """The cached failing ids, or None on a miss (absent, unreadable or for another key)."""
        try:
            record = json.loads(self.path(base, command).read_text())
        except (OSError, ValueError):
            return None
        if (
            not isinstance(record, dict)
            or record.get("repo") != self.repo
            or record.get("base") != base
            or record.get("command") != command
        ):
            return None
        try:
            return _ids(record.get("ids"), "ids") if "ids" in record else None
        except ValueError:
            return None

    def store(self, base, command, ids):
        try:
            atomic_json(
                self.path(base, command),
                {"repo": self.repo, "base": base, "command": command, "ids": list(ids)},
            )
        except OSError:
            pass  # a cache that cannot be written only costs another baseline run


def prompt(command, base):
    text = (Path(__file__).parent / "prompts" / "verify-baseline.md").read_text()
    return text + f"\nBase commit: {base}\nTest command (JSON string): {json.dumps(command)}\n"


def _listed(ids):
    shown = ", ".join(ids[:MAX_LISTED])
    return shown + (f" (+{len(ids) - MAX_LISTED} more)" if len(ids) > MAX_LISTED else "")


def bounded_view(value):
    """``value`` for display: every list cut to ``MAX_RENDERED`` entries plus an ``and N more`` entry.

    Strings inside lists (test ids, assertions) are cut to ``MAX_SHOWN_ID`` characters. A copy: the
    verdict and the baseline record in the job state are never changed.
    """
    if isinstance(value, dict):
        return {key: bounded_view(item) for key, item in value.items()}
    if isinstance(value, list):
        shown = [_shown(item) for item in value[:MAX_RENDERED]]
        if len(value) > MAX_RENDERED:
            shown.append(f"and {len(value) - MAX_RENDERED} more")
        return shown
    return value


def _shown(item):
    if isinstance(item, str):
        return item if len(item) <= MAX_SHOWN_ID else f"{item[:MAX_SHOWN_ID]}..."
    return bounded_view(item)


def bounded_dump(verification):
    """The verification verdict as indented JSON for the PR body: lists and total size bounded."""
    dump = json.dumps(bounded_view(verification), indent=2)
    if len(dump) > MAX_DUMP:
        dump = f"{dump[:MAX_DUMP]}\n... and {len(dump) - MAX_DUMP} more characters"
    return dump


def apply(verification, test_run, info, new, environmental):
    """Verdict after a completed comparison; the record goes under ``baseline``."""
    result = dict(verification)
    result["baseline"] = {
        **info,
        "status": "compared",
        "new_failures": new,
        "environmental_failures": environmental,
        # What the verifier said about its own verdict, so a later reader (the reason code) can tell
        # "the tests were the only blocker" from "something else also blocked", and whether this
        # comparison already cleared the verdict.
        "sole_blocker": test_run["sole_blocker"] is True,
        # ... and whether it called its own verdict complete: a verdict that says complete AND names
        # a sole blocker contradicts itself, and a later failure of the metadata check on it is
        # missing evidence, not environmental noise.
        "verifier_complete": verification.get("complete") is True,
        "cleared": False,
    }
    old = str(result.get("blocker") or "").strip()
    base = info["base"]
    if new:
        result["complete"] = False
        result["blocker"] = (
            f"New failing tests on the head (not failing on base {base[:12]}): {_listed(new)}"
            + (f". Verifier: {old}" if old else "")
        )
    elif result.get("complete") is not True and test_run["sole_blocker"]:
        # Every head failure also fails on the base, and the verifier named them the only
        # reason for complete=false: they are environmental. The required metadata
        # (scenario, environment, assertions, commit) is still checked afterwards.
        result["complete"] = True
        result["baseline"]["cleared"] = True
        if old:
            result["agent_blocker"] = old
        result["blocker"] = ""
    return result


def fallback(verification, info, reason):
    """The agent's verdict stands; the reason the baseline did not decide is recorded."""
    result = dict(verification)
    result["baseline"] = {**info, "status": "fallback", "reason": reason}
    return result


def render(baseline, text):
    """Markdown lines for the PR body's Verification section; ``text`` neutralises references."""
    if not isinstance(baseline, dict):
        return ""
    lines = ["**Baseline comparison**: " + text(baseline.get("status", "unknown"))]
    if baseline.get("command"):
        lines.append("- Test command: " + text(json.dumps(baseline["command"])))
    if baseline.get("base"):
        lines.append("- Base commit: " + text(baseline["base"]))
    if baseline.get("cache"):
        lines.append("- Baseline run: cache " + text(baseline["cache"]))
    if baseline.get("status") == "compared":
        for label, key in (
            ("New failures (fail on the head only)", "new_failures"),
            ("Environmental failures (fail on the base too)", "environmental_failures"),
        ):
            ids = baseline.get(key) or []
            shown = ", ".join(text(json.dumps(_shown(i))) for i in ids[:MAX_RENDERED])
            if len(ids) > MAX_RENDERED:
                shown = f"{shown}, and {len(ids) - MAX_RENDERED} more"
            lines.append(f"- {label}: {shown if ids else 'none'}")
    if baseline.get("reason"):
        lines.append(
            "- Fallback (the verifier's verdict stands unchanged): " + text(baseline["reason"])
        )
    return "\n".join(lines) + "\n\n"

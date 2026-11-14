#!/usr/bin/env python3
"""Effort A/B harness: the same small tasks at different efforts, through Factory's own label trigger, in a SANDBOX repository.

    effort_ab.py run   --tasks T.json --state S.json --repo ORG/NAME --data DIR --confirm-sandbox [--budget-usd 10]
    effort_ab.py pilot --tasks T.json --state S.json --repo ORG/NAME --data DIR --confirm-sandbox   (one repeat, then the spread)
    effort_ab.py run --dry-run --tasks T.json --efforts low,medium,high --repeats 2   (the plan only: no call, nothing written)
    effort_ab.py report --state S.json [--data DIR]   (the numbers of a state file; no GitHub call)

Issues #132 and #154. ``tools/effort_report.py`` only reads what ran; this is the half that makes it run, so that **effort is the only
variable**: one GitHub issue per (task, effort, repeat) with identical title and body, labelled ``factory-effort:<e>``,
``factory-model:<alias>`` and ``effort-ab:<name>`` (the experiment label), then the trigger label (``factory-build``) last, so the manager's own
label trigger plans and builds it exactly as it would for a person. Nothing here is a conclusion: it records numbers and a person reads the pull requests.

* **Plan.** The arms are ordered at random from a recorded seed, in blocks: the (task, repeat) blocks are shuffled and the efforts inside a block
  too (a SHA-256 of the seed and the arm's names, so the order does not depend on the Python version or on the order of the task file). A run
  stopped early is therefore still balanced across efforts as far as it got. Arms run one at a time.
* **Never merges.** The client has no merge call (``Client.ALLOWED`` is the whole list of gh verbs: label create, issue create/edit/view/list/close,
  pr view/close, and the deletion of a branch ref). After a session ends the harness records the outcome, closes the pull request WITHOUT merging,
  closes the issue as not planned, and goes on, so the base commit is the same for every arm. The branch is LEFT by default (a closed pull request keeps its diff;
  a person reviews it); ``--delete-branches`` deletes the pull request's head branch with one ``DELETE git/refs/heads/<name>`` after the close
  (never a push, never a force push; only a branch of the sandbox repository other than the base). A pull request found merged, or with auto-merge
  enabled, stops the experiment (exit 4): the sandbox must not carry ``handoff/factory-automerge.yml``.
* **Budget.** Before each arm: spent so far + the estimate for that arm must not pass ``--budget-usd``, else it stops cleanly (exit 3), keeps its
  state, and a later run with a larger budget continues. Spent comes from the run directories (the readers of ``effort_report``: session costs of the
  experiment's jobs plus the planner's), from the cost recorded for finished arms, or from ``--cost-file`` (JSON ``{"cost_usd": N}``) which replaces them.
  An arm whose cost cannot be read counts as the estimate. Cost is the CLI's client-side estimate, not billing data.
* **Resume.** The state file (atomic writes, mode 0600) holds the plan and every arm's progress; running again with the same arguments continues and never
  creates a second issue for an arm (an issue created but not yet recorded is found again by its experiment label). The repeats may be raised, the rest may not change.
* **Output.** Fixed text and numbers only: never an issue, a task title or body, a pull request path, a log, a token or a gh message.
  Task ids (``[a-z0-9-]``) are the operator's own names. The record file may hold counts but no text of an issue.

Exit codes: 0 done, 2 refused (arguments, files, state mismatch), 3 budget stop, 4 stopped for safety or a failed call (state kept), 5 time limit reached (state kept).
Standard library plus ``tools/effort_report.py`` and the ``factory`` package (the same readers Factory uses).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import secrets
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _load_report():
    spec = importlib.util.spec_from_file_location("effort_report", Path(__file__).with_name("effort_report.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


effort_report = _load_report()  # also puts the checkout's src/ on sys.path when factory is not installed
from factory import config, github, history, issue_labels, model_choice, phases, planner  # noqa: E402
from factory.dashboard import read_json  # noqa: E402

VERSION = 1
EXPERIMENT_LABEL_PREFIX = "effort-ab:"
DEFAULT_EFFORTS = ("low", "medium", "high")
DEFAULT_TRIGGER_LABEL = "factory-build"
TASK_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,23}")
PR_URL = re.compile(r"https://github\.com/([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)/pull/([1-9][0-9]{0,9})")
TRIGGER_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._/-]{0,49}")
SAFE_PATH = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._/@+-]{0,199}")
WORD = re.compile(r"[a-z][a-z0-9_]{0,39}")
MODEL_ID = re.compile(r"claude-[a-z0-9][a-z0-9.\-]{0,40}(?:\[[0-9a-z]{1,4}\])?")
KEY = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,39}")
EXPERIMENT_COLOR = "fbca04"
MAX_TASKS = 50
MAX_FILE_BYTES = 256 * 1024
MAX_TITLE = 200
MAX_BODY = 8000
MAX_LIST = 20
MAX_ITEM = 500
MAX_COST_USD = 1e9
CALL_TIMEOUT = 60
REFRESH_TIMEOUT = 300
# A job whose session is over: published (a pull request exists), or stopped for a reason a person must look at.
PUBLISHED = ("pr_ready", "pr_incomplete")
ENDED_JOB = frozenset({*PUBLISHED, "failed", "blocked", "needs_human_publish", "stopped", "interrupted", "merged", "closed"})
EXIT_DONE, EXIT_REFUSED, EXIT_BUDGET, EXIT_STOPPED, EXIT_TIME = 0, 2, 3, 4, 5


class Refused(Exception):
    """A refusal with a fixed sentence: it names a field, never a value."""


class ClientError(Exception):
    """A failed call; ``op`` is a fixed verb of this file, never gh's message."""

    def __init__(self, op):
        super().__init__(op)
        self.op = op


class Paused(Exception):
    """The overall time limit (``--max-hours``) was reached while an arm was waiting; the state is kept."""


class Unsafe(Exception):
    """The experiment must stop: a pull request of the sandbox was merged or set to auto-merge."""


# --- tasks and the plan --------------------------------------------------------------------------------


def _read_file(path: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
    except OSError:
        raise Refused("a file cannot be read") from None
    if len(raw) > MAX_FILE_BYTES:
        raise Refused("a file is too large")
    return raw


def _text(value, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise Refused(f"task field {field} is not a text within its limit")
    return value


def _list(value, field: str, pattern=None) -> list:
    if not isinstance(value, list) or len(value) > MAX_LIST:
        raise Refused(f"task field {field} is not a short list")
    out = []
    for item in value:
        _text(item, field, MAX_ITEM)
        if pattern is not None and (not pattern.fullmatch(item) or ".." in item.split("/")):
            raise Refused(f"task field {field} holds an entry that is not a relative path")
        out.append(item)
    return out


def load_tasks(path: str) -> list:
    """The task file: a JSON list (or ``{"tasks": [...]}``) of ``{id, title, body, expected_paths, acceptance}``, validated."""
    try:
        document = json.loads(_read_file(path))
    except ValueError:
        raise Refused("the task file is not JSON") from None
    items = document.get("tasks") if isinstance(document, dict) else document
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_TASKS:
        raise Refused(f"the task file needs 1 to {MAX_TASKS} tasks")
    tasks, seen = [], set()
    for item in items:
        if not isinstance(item, dict) or set(item) - {"id", "title", "body", "expected_paths", "acceptance"}:
            raise Refused("a task has missing or unknown fields")
        identity = item.get("id")
        if not isinstance(identity, str) or not TASK_ID.fullmatch(identity) or identity in seen:
            raise Refused("a task id is not unique lowercase letters, digits and dashes")
        seen.add(identity)
        tasks.append({
            "id": identity,
            "title": _text(item.get("title"), "title", MAX_TITLE),
            "body": _text(item.get("body"), "body", MAX_BODY),
            "expected_paths": _list(item.get("expected_paths", []), "expected_paths", SAFE_PATH),
            "acceptance": _list(item.get("acceptance", []), "acceptance"),
        })
    return tasks


def tasks_digest(tasks: list) -> str:
    return hashlib.sha256(json.dumps(tasks, sort_keys=True).encode()).hexdigest()[:16]


def issue_text(task: dict) -> tuple:
    """(title, body) of the issue of a task: the same for every effort and repeat, so nothing but the label differs."""
    body = task["body"]
    if task["acceptance"]:
        body += "\n\nAcceptance criteria:\n" + "\n".join(f"- {line}" for line in task["acceptance"])
    return task["title"], body


def _order(seed: int, *parts) -> str:
    return hashlib.sha256(":".join(str(p) for p in (seed, *parts)).encode()).hexdigest()


def make_arms(tasks: list, efforts: list, repeats: int, seed: int) -> list:
    """The arms in their randomised order: blocks of (task, repeat) shuffled, the efforts inside a block shuffled, from ``seed`` only."""
    blocks = sorted(((t["id"], r) for r in range(1, repeats + 1) for t in tasks), key=lambda b: _order(seed, "block", *b))
    arms = []
    for task, repeat in blocks:
        for effort in sorted(efforts, key=lambda e: _order(seed, "effort", task, repeat, e)):
            arms.append({"key": f"{task}.{effort}.{repeat}", "task": task, "effort": effort, "repeat": repeat})
    return arms


def parse_efforts(text: str) -> list:
    efforts = text.split(",")
    if not 1 <= len(efforts) <= len(phases.EFFORTS) or len(set(efforts)) != len(efforts) or any(e not in phases.EFFORTS for e in efforts):
        raise Refused(f"efforts must be distinct names from {', '.join(phases.EFFORTS)}")
    return efforts


def new_state(args, tasks: list, efforts: list, repeats: int) -> dict:
    seed = args.seed if args.seed is not None else secrets.randbelow(2**31)
    digest = tasks_digest(tasks)
    name = args.name or "ab-" + hashlib.sha256(f"{seed}:{digest}:{','.join(efforts)}".encode()).hexdigest()[:8]
    if not NAME.fullmatch(name):
        raise Refused("the experiment name is not 1 to 24 lowercase letters, digits and dashes")
    arms = make_arms(tasks, efforts, repeats, seed)
    for number, arm in enumerate(arms, 1):
        arm.update({"order": number, "status": "pending", "issue": None, "pr": None, "started_at": None, "finished_at": None,
                    "outcome": None, "cost_usd": None, "record": None})
    return {"version": VERSION, "name": name, "seed": seed, "repo": args.repo, "model": args.model, "efforts": efforts,
            "repeats": repeats, "tasks": [t["id"] for t in tasks], "tasks_digest": digest, "arms": arms}


def extend_state(state: dict, tasks: list, repeats: int) -> None:
    """Raise the repeats of a state: the new arms follow the old ones (the finished ones never move)."""
    known = {arm["key"] for arm in state["arms"]}
    for arm in make_arms(tasks, state["efforts"], repeats, state["seed"]):
        if arm["key"] not in known:
            arm.update({"order": len(state["arms"]) + 1, "status": "pending", "issue": None, "pr": None, "started_at": None,
                        "finished_at": None, "outcome": None, "cost_usd": None, "record": None})
            state["arms"].append(arm)
    state["repeats"] = repeats


# --- state file ---------------------------------------------------------------------------------------


def save_json(path: str, document: dict) -> None:
    """Write atomically (a temporary file in the same directory, fsync, rename), mode 0600."""
    directory = os.path.dirname(os.path.abspath(path))
    handle, temporary = tempfile.mkstemp(prefix=".effort-ab-", dir=directory)
    try:
        with os.fdopen(handle, "w") as out:
            json.dump(document, out, indent=1, sort_keys=True)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise Refused("a file cannot be written") from None


def load_state(path: str) -> dict | None:
    """The state file, or None when it does not exist; a file that is not a state is refused (never overwritten)."""
    if not os.path.exists(path):
        return None
    try:
        state = json.loads(_read_file(path))
    except ValueError:
        raise Refused("the state file is not JSON") from None
    arms = state.get("arms") if isinstance(state, dict) else None
    if (not isinstance(arms, list) or state.get("version") != VERSION or not isinstance(state.get("seed"), int)
            or not isinstance(state.get("efforts"), list) or not isinstance(state.get("name"), str)
            or not all(isinstance(a, dict) and a.get("status") in ("pending", "creating", "waiting", "finished", "done") for a in arms)):
        raise Refused("the state file is not an effort A/B state")
    return state


# --- the one client ----------------------------------------------------------------------------------


class Client:
    """Every GitHub and manager call of the harness, as gh argv lists (no shell). Tests replace the whole class with a fake.

    ``ALLOWED`` is the complete list of gh verbs this class may run: there is no merge, no auto-merge and no push.
    """

    ALLOWED = frozenset({
        ("label", "create"), ("issue", "create"), ("issue", "edit"), ("issue", "view"), ("issue", "list"), ("issue", "close"),
        ("pr", "view"), ("pr", "close"), ("api", "-X"),
    })

    def __init__(self, repo: str, refresh_argv: list | None = None, runner=subprocess.run):
        self.repo = repo
        self.refresh_argv = refresh_argv
        self.runner = runner

    def _gh(self, op: str, *args: str) -> str:
        if (args[0], args[1]) not in self.ALLOWED:
            raise ClientError(op)
        if args[0] == "api" and not (args[:3] == ("api", "-X", "DELETE") and args[3].startswith(f"repos/{self.repo}/git/refs/heads/")):
            raise ClientError(op)
        try:
            done = self.runner(["gh", *args], capture_output=True, text=True, timeout=CALL_TIMEOUT, check=False,
                               env={**os.environ, "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1"})
        except (OSError, subprocess.SubprocessError):
            raise ClientError(op) from None
        if done.returncode != 0:
            raise ClientError(op)
        return done.stdout

    def _json(self, op: str, *args: str):
        try:
            return json.loads(self._gh(op, *args))
        except ValueError:
            raise ClientError(op) from None

    def ensure_label(self, name: str, color: str, description: str) -> None:
        self._gh("label", "label", "create", name, "--repo", self.repo, "--color", color, "--description", description, "--force")

    def create_issue(self, title: str, body: str, labels: list) -> int:
        args = ["issue", "create", "--repo", self.repo, f"--title={title}", f"--body={body}", *(f"--label={label}" for label in labels)]
        match = re.search(r"/issues/([1-9][0-9]{0,9})\s*$", self._gh("create_issue", *args))
        if match is None:
            raise ClientError("create_issue")
        return int(match.group(1))

    def add_label(self, number: int, label: str) -> None:
        self._gh("add_label", "issue", "edit", str(number), "--repo", self.repo, "--add-label", label)

    def issue(self, number: int) -> dict:
        """``{"state": "OPEN"|"CLOSED", "labels": [names]}``."""
        data = self._json("issue", "issue", "view", str(number), "--repo", self.repo, "--json", "state,labels")
        return {"state": data.get("state"), "labels": [x.get("name") for x in data.get("labels") or () if isinstance(x, dict)]}

    def find_issues(self, label: str) -> list:
        """``[{"number", "title", "labels"}]`` of the issues (any state) carrying ``label``."""
        rows = self._json("find_issues", "issue", "list", "--repo", self.repo, "--label", label, "--state", "all", "--limit", "200",
                          "--json", "number,title,labels")
        return [{"number": r.get("number"), "title": r.get("title"), "labels": [x.get("name") for x in r.get("labels") or () if isinstance(x, dict)]}
                for r in rows if isinstance(r, dict)]

    def pr(self, number: int) -> dict:
        fields = "state,headRefName,baseRefName,isCrossRepository,statusCheckRollup,autoMergeRequest,files"
        return self._json("pr", "pr", "view", str(number), "--repo", self.repo, "--json", fields)

    def close_pr(self, number: int) -> None:
        self._gh("close_pr", "pr", "close", str(number), "--repo", self.repo)

    def close_issue(self, number: int) -> None:
        self._gh("close_issue", "issue", "close", str(number), "--repo", self.repo, "--reason", "not planned")

    def delete_branch(self, name: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", name) or ".." in name:
            raise ClientError("delete_branch")
        self._gh("delete_branch", "api", "-X", "DELETE", f"repos/{self.repo}/git/refs/heads/{name}")

    def refresh(self) -> None:
        """Bring the data directory up to date (the operator's own command, e.g. a ``kubectl cp``); nothing of it is shown."""
        if not self.refresh_argv:
            return
        try:
            done = self.runner(self.refresh_argv, capture_output=True, text=True, timeout=REFRESH_TIMEOUT, check=False)
        except (OSError, subprocess.SubprocessError):
            raise ClientError("refresh") from None
        if done.returncode != 0:
            raise ClientError("refresh")


# --- reading what Factory recorded ---------------------------------------------------------------------


def _clean_numbers(value, depth: int = 0) -> dict:
    out = {}
    for key, item in (value or {}).items() if isinstance(value, dict) else ():
        if not isinstance(key, str) or not KEY.fullmatch(key) or len(out) >= 40:
            continue
        if isinstance(item, dict) and depth < 1:
            out[key] = _clean_numbers(item, depth + 1)
        elif (n := history.number_of(item)) is not None:
            out[key] = n
    return out


def _model_usage(value) -> dict:
    """``model_usage``: per model id (anything else is ``other``), the numbers only; at most 8 models."""
    out = {}
    for key, item in list(value.items())[:8] if isinstance(value, dict) else ():
        name = key if isinstance(key, str) and MODEL_ID.fullmatch(key) else "other"
        if isinstance(item, dict) and name not in out:
            out[name] = _clean_numbers(item, 1)
    return out


def thinking_tokens(usage: dict) -> float | None:
    """The thinking tokens of a session when the CLI reports them (``thinking_tokens``, or in ``output_tokens_details``), else None."""
    for source in (usage, usage.get("output_tokens_details") if isinstance(usage.get("output_tokens_details"), dict) else {}):
        value = history.number_of(source.get("thinking_tokens"))
        if value is not None:
            return value
    return None


def session_record(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    usage = _clean_numbers(raw.get("usage"))
    subtype = raw.get("subtype")
    model = raw.get("model")
    return {
        "phase": raw["phase"] if raw.get("phase") in phases.PHASES else "other",
        "model": model if isinstance(model, str) and MODEL_ID.fullmatch(model) else "other",
        "output_tokens": usage.get("output_tokens"),
        "input_tokens": usage.get("input_tokens"),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        "thinking_tokens": thinking_tokens(usage),
        "model_usage": _model_usage(raw.get("model_usage")),
        "total_cost_usd": history.number_of(raw.get("total_cost_usd")),
        "num_turns": history.count_of(raw.get("num_turns")),
        "duration_ms": history.number_of(raw.get("duration_ms")),
        "subtype": subtype if isinstance(subtype, str) and WORD.fullmatch(subtype) else "unknown",
    }


def read_arm_data(data_dir: str, issue: int) -> dict | None:
    """What the run directories hold for the job of ``issue`` (the newest run that planned it), or None. Read-only, via effort_report's finders."""
    runs, _ledgers, _skipped = effort_report.find_sources([data_dir])
    found = None
    for directory in sorted(runs):
        plan, state = read_json(Path(directory) / "plan.json"), read_json(Path(directory) / "state.json")
        if plan is None or state is None or not isinstance(plan.get("jobs"), list) or not isinstance(state.get("jobs"), dict):
            continue
        for entry in plan["jobs"]:
            if not isinstance(entry, dict) or issue not in history.issues_of(entry.get("issues")):
                continue
            job = state["jobs"].get(entry.get("id"))
            if not isinstance(job, dict):
                continue
            planner_cost = sum(c for c in (history.number_of(r.get("total_cost_usd")) for r in planner.planner_usage_records(directory)) if c)
            usage = job.get("usage") if isinstance(job.get("usage"), list) else []
            found = {"status": history.word_of(job.get("status")), "job": job, "entry": entry, "planner_cost": planner_cost,
                     "sessions": [s for s in (session_record(r) for r in usage[:effort_report.MAX_SESSIONS_PER_JOB]) if s is not None]}
    return found


def pending_checks(rollup) -> int:
    count = 0
    for check in rollup if isinstance(rollup, list) else ():
        if not isinstance(check, dict):
            continue
        if check.get("__typename") == "CheckRun":
            count += 1 if check.get("status") != "COMPLETED" else 0
        elif check.get("state") in ("PENDING", "EXPECTED"):
            count += 1
    return count


def pr_facts(pr: dict, expected: list) -> dict:
    """The pull request's gate state, CI state and the share of the expected paths it touched, as words and counts only."""
    rollup = pr.get("statusCheckRollup") if isinstance(pr.get("statusCheckRollup"), list) else []
    gate = next((str(c.get("state")).lower() for c in rollup if isinstance(c, dict) and c.get("context") == github.GATE_CONTEXT), None)
    others = [c for c in rollup if isinstance(c, dict) and c.get("context") != github.GATE_CONTEXT and c.get("name") != github.GATE_CONTEXT]
    if github.failed_checks(rollup):
        ci = "failing"
    elif pending_checks(others):
        ci = "pending"
    else:
        ci = "passing" if others else "none"
    paths = {f.get("path") for f in pr.get("files") or () if isinstance(f, dict) and isinstance(f.get("path"), str)}
    return {"gate": gate if gate in ("success", "pending", "failure", "error") else "none", "ci": ci, "files_changed": len(paths),
            "expected_paths_total": len(expected), "expected_paths_touched": len(paths & set(expected)),
            "unexpected_paths": len(paths - set(expected))}


def arm_record(arm: dict, task: dict, data: dict | None, pr: dict | None, outcome: str) -> dict:
    """The record of one finished arm: numbers and fixed words (no issue, task or pull request text)."""
    record = {"outcome": outcome, "sessions": [], "effort_confirmed": None, "job_status": None}
    if data is not None:
        job, entry = data["job"], data["entry"]
        choice = model_choice.recorded_choice(entry)
        build = choice["build"] if choice else None
        verification = job.get("verification") if isinstance(job.get("verification"), dict) else {}
        review = job.get("review") if isinstance(job.get("review"), dict) else {}
        findings = review.get("findings")
        sessions = data["sessions"]
        costs = [s["total_cost_usd"] for s in sessions if s["total_cost_usd"] is not None]
        tokens = {name: sum(s[name] or 0 for s in sessions if s[name] is not None) if any(s[name] is not None for s in sessions) else None
                  for name in ("output_tokens", "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "thinking_tokens")}
        record.update({
            "job_status": data["status"], "sessions": sessions, "totals": {
                **tokens, "num_turns": sum(s["num_turns"] or 0 for s in sessions), "duration_ms": sum(s["duration_ms"] or 0 for s in sessions),
                "session_cost_usd": round(sum(costs), 6)},
            "planner_cost_usd": round(data["planner_cost"], 6),
            "cost_usd": round(sum(costs) + data["planner_cost"], 6),
            "effort_confirmed": bool(build) and build["effort"] == arm["effort"] and build["effort_source"] == "issue",
            "verification": {"complete": verification.get("complete") if isinstance(verification.get("complete"), bool) else None,
                             "matches_head": bool(job.get("commit")) and verification.get("commit") == job.get("commit")},
            "review": {"passed": review.get("pass") if isinstance(review.get("pass"), bool) else None,
                       "findings": len(findings) if isinstance(findings, list) else None},
            "incomplete": data["status"] == "pr_incomplete",
            "attempts": history.count_of(job.get("attempt")),
        })
    if pr is not None:
        record["pr"] = pr_facts(pr, task["expected_paths"])
    return record


# --- the budget -------------------------------------------------------------------------------------


def read_cost_file(path: str) -> float:
    try:
        value = json.loads(_read_file(path)).get("cost_usd")
    except (ValueError, AttributeError):
        raise Refused("the cost file is not JSON with cost_usd") from None
    number = history.number_of(value)
    if number is None or number > MAX_COST_USD:
        raise Refused("the cost file has no usable cost_usd")
    return number


def estimate_for(state: dict, effort: str, default: float) -> float:
    """What the next arm may cost: the dearest finished arm of its effort, else the dearest of any, else the default."""
    costs = [(a["effort"], a["cost_usd"]) for a in state["arms"] if isinstance(a.get("cost_usd"), (int, float))]
    same = [c for e, c in costs if e == effort]
    return max(same) if same else (max(c for _e, c in costs) if costs else default)


def spent_so_far(state: dict, cost_file: str | None, default: float) -> float:
    if cost_file:
        return read_cost_file(cost_file)
    total = 0.0
    for arm in state["arms"]:
        if arm["status"] in ("waiting", "creating", "finished", "done") or arm.get("issue") is not None:
            total += arm["cost_usd"] if isinstance(arm.get("cost_usd"), (int, float)) else estimate_for(state, arm["effort"], default)
    return round(total, 6)


# --- running ----------------------------------------------------------------------------------------


class Runner:
    """One experiment: arms one at a time, a state write after every step. Everything outside is injected."""

    def __init__(self, args, state: dict, tasks: list, client, say, now=time.time, sleep=time.sleep):
        self.args, self.state, self.client, self.say, self.now, self.sleep = args, state, client, say, now, sleep
        self.tasks = {t["id"]: t for t in tasks}
        self.label = f"{EXPERIMENT_LABEL_PREFIX}{state['name']}"
        self.started = now()

    def save(self) -> None:
        save_json(self.args.state, self.state)
        if self.args.records:
            save_json(self.args.records, records_of(self.state))

    # -- one arm
    def labels_of(self, arm: dict) -> list:
        return [f"{model_choice.EFFORT_LABEL_PREFIX}{arm['effort']}", f"{model_choice.MODEL_LABEL_PREFIX}{self.state['model']}", self.label]

    def start(self, arm: dict) -> None:
        arm["status"], arm["started_at"] = "creating", self.now()
        self.save()
        if arm["issue"] is None:
            arm["issue"] = self.adopt_orphan(arm)
        if arm["issue"] is None:
            title, body = issue_text(self.tasks[arm["task"]])
            arm["issue"] = self.client.create_issue(title, body, self.labels_of(arm))
            self.save()
        view = self.client.issue(arm["issue"])
        known = any(label.startswith("factory:") for label in view["labels"]) or (
            self.args.data and read_arm_data(self.args.data, arm["issue"]) is not None)
        if not known and self.args.trigger_label not in view["labels"]:
            self.client.add_label(arm["issue"], self.args.trigger_label)  # last: every other label is on the issue before the trigger
        arm["status"] = "waiting"
        self.save()
        self.say(f"effort_ab: started arm={arm['order']} effort={arm['effort']} repeat={arm['repeat']} issue={arm['issue']}")

    def adopt_orphan(self, arm: dict):
        """An issue created before a crash that the state does not know: the only one of this experiment with this arm's effort and task title."""
        known = {a["issue"] for a in self.state["arms"] if a["issue"] is not None}
        title = issue_text(self.tasks[arm["task"]])[0]
        effort_label = f"{model_choice.EFFORT_LABEL_PREFIX}{arm['effort']}"
        found = [i["number"] for i in self.client.find_issues(self.label)
                 if i["number"] not in known and i["title"] == title and effort_label in i["labels"]]
        if len(found) > 1:
            raise Refused("several unrecorded issues of this experiment match one arm")
        return found[0] if found else None

    def wait(self, arm: dict) -> str | None:
        """Poll until the session is over; returns the outcome word, or None when the arm timed out (state kept)."""
        deadline = arm["started_at"] + self.args.arm_timeout_minutes * 60
        published_at, failures = None, 0
        while True:
            try:
                self.client.refresh()
                data = read_arm_data(self.args.data, arm["issue"]) if self.args.data else None
                view = self.client.issue(arm["issue"])
                failures = 0
            except ClientError as error:
                failures += 1
                if failures >= 3:
                    raise
                self.say(f"effort_ab: call failed op={error.op}; trying again")
                data, view = None, None
            if view is not None:
                status = data["status"] if data else None
                pr = None
                if data is not None and self.pr_number(arm, data) is not None:
                    pr = self.client.pr(arm["pr"])
                if pr is not None and pr.get("state") == "MERGED":
                    return "merged_unexpectedly"
                if status in PUBLISHED:
                    published_at = published_at or self.now()
                    risky = pr is not None and bool(pr.get("autoMergeRequest"))
                    settled = pr is None or pending_checks(pr.get("statusCheckRollup")) == 0
                    if risky or settled or self.now() - published_at > self.args.ci_wait_minutes * 60:
                        return status
                elif status in ENDED_JOB:
                    return status
                elif data is None and issue_labels.LABEL_BLOCKED in view["labels"]:
                    return "blocked_before_job"
                elif view["state"] == "CLOSED":
                    return "issue_closed"
            if self.now() > deadline:
                return None
            if self.now() - self.started > self.args.max_hours * 3600:
                raise Paused
            self.sleep(self.args.poll_seconds)

    def pr_number(self, arm: dict, data: dict) -> int | None:
        """The pull request number of the job, recorded on the arm; one outside the sandbox repository is never touched."""
        url = data["job"].get("pr")
        if not isinstance(url, str) or not url:
            return None
        match = PR_URL.fullmatch(url)
        if match is None or match.group(1).lower() != self.state["repo"].lower():
            raise Unsafe
        arm["pr"] = int(match.group(2))
        return arm["pr"]

    def finish(self, arm: dict, outcome: str) -> None:
        data = read_arm_data(self.args.data, arm["issue"]) if self.args.data else None
        if data is not None:
            self.pr_number(arm, data)
        pr = self.client.pr(arm["pr"]) if arm["pr"] is not None else None
        arm["record"] = arm_record(arm, self.tasks[arm["task"]], data, pr, outcome)
        arm["outcome"], arm["finished_at"] = outcome, self.now()
        arm["cost_usd"] = arm["record"].get("cost_usd")
        arm["status"] = "finished"
        self.save()
        self.say(f"effort_ab: finished arm={arm['order']} outcome={outcome} cost_usd={arm['cost_usd'] if arm['cost_usd'] is not None else 'unknown'}")

    def cleanup(self, arm: dict) -> None:
        """Close the pull request WITHOUT merging, close the issue as not planned; the branch stays unless ``--delete-branches``."""
        if arm["pr"] is not None:
            pr = self.client.pr(arm["pr"])
            if pr.get("state") == "MERGED":  # sticky: the base commit changed, so this experiment is over
                arm["record"]["merge_risk"] = True
                self.save()
                raise Unsafe
            risky = bool(pr.get("autoMergeRequest")) and not arm["record"].get("auto_merge_seen")
            if pr.get("state") == "OPEN":
                self.client.close_pr(arm["pr"])  # also ends any auto-merge request: a closed pull request cannot merge
            if (self.args.delete_branches and not pr.get("isCrossRepository")
                    and pr.get("headRefName") and pr.get("headRefName") != pr.get("baseRefName")):
                self.client.delete_branch(pr["headRefName"])
            if risky:
                arm["record"]["auto_merge_seen"] = True
                self.save()
                raise Unsafe
        if self.client.issue(arm["issue"])["state"] == "OPEN":
            self.client.close_issue(arm["issue"])
        arm["status"] = "done"
        self.save()

    # -- the whole run
    def refuse_over_budget(self, arm: dict) -> bool:
        estimate = estimate_for(self.state, arm["effort"], self.args.arm_estimate_usd)
        spent = spent_so_far(self.state, self.args.cost_file, self.args.arm_estimate_usd)
        if spent + estimate > self.args.budget_usd:
            self.say(f"effort_ab: budget stop: spent_usd={spent:.2f} next_arm_estimate_usd={estimate:.2f} budget_usd={self.args.budget_usd:.2f}")
            return True
        return False

    def run(self) -> int:
        try:
            return self._run()
        except Paused:
            self.save()
            self.say("effort_ab: time limit reached; the state is kept, run again to continue")
            return EXIT_TIME
        except Unsafe:
            self.save()
            self.say("effort_ab: stopped: a sandbox pull request was merged or set to auto-merge, or is not in the sandbox repository; "
                     "check the repository's auto-merge workflow and ruleset before going on")
            return EXIT_STOPPED
        except ClientError as error:
            self.save()
            self.say(f"effort_ab: stopped: a call failed op={error.op}; the state is kept, run again to continue")
            return EXIT_STOPPED

    def _run(self) -> int:
        for arm in self.state["arms"]:
            if arm["status"] == "done":
                continue
            if arm["status"] == "finished":
                self.cleanup(arm)
                continue
            if self.now() - self.started > self.args.max_hours * 3600:
                self.say("effort_ab: time limit reached; the state is kept, run again to continue")
                return EXIT_TIME
            if arm["status"] == "pending" and self.refuse_over_budget(arm):
                return EXIT_BUDGET
            if arm["status"] in ("pending", "creating"):
                self.start(arm)
            outcome = self.wait(arm)
            if outcome is None:
                self.finish(arm, "timeout")
                self.cleanup(arm)
                self.say("effort_ab: stopped: an arm timed out; a session may still be running, stop it in the manager before running again")
                return EXIT_STOPPED
            self.finish(arm, outcome)
            self.cleanup(arm)
        self.say("effort_ab: all arms done")
        return EXIT_DONE


# --- the numbers ------------------------------------------------------------------------------------


def records_of(state: dict) -> dict:
    return {key: state[key] for key in ("version", "name", "seed", "repo", "model", "efforts", "repeats", "tasks", "tasks_digest")} | {
        "arms": [{k: a.get(k) for k in ("key", "task", "effort", "repeat", "order", "status", "issue", "pr", "outcome", "cost_usd", "record")}
                 for a in state["arms"]]}


def _values(arms: list, getter) -> list:
    out = []
    for arm in arms:
        value = getter(arm["record"]) if arm.get("record") else None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out.append(float(value))
    return out


METRICS = (
    ("cost_usd", lambda r: r.get("cost_usd")),
    ("output_tokens", lambda r: (r.get("totals") or {}).get("output_tokens")),
    ("thinking_tokens", lambda r: (r.get("totals") or {}).get("thinking_tokens")),
    ("turns", lambda r: (r.get("totals") or {}).get("num_turns")),
    ("duration_s", lambda r: ((r.get("totals") or {}).get("duration_ms") or 0) / 1000 if r.get("totals") else None),
    ("findings", lambda r: (r.get("review") or {}).get("findings")),
)


def summary_rows(state: dict) -> list:
    """Per effort: arms, outcome counts and each metric's n, mean, sd, min, max over the finished arms."""
    rows = []
    for effort in state["efforts"]:
        arms = [a for a in state["arms"] if a["effort"] == effort and a.get("record")]
        row = {"effort": effort, "arms": len([a for a in state["arms"] if a["effort"] == effort]), "finished": len(arms),
               "effort_confirmed": sum(1 for a in arms if a["record"].get("effort_confirmed") is True),
               "published": sum(1 for a in arms if a["record"].get("job_status") in PUBLISHED),
               "incomplete": sum(1 for a in arms if a["record"].get("incomplete")),
               "verification_complete": sum(1 for a in arms if (a["record"].get("verification") or {}).get("complete") is True),
               "gate_success": sum(1 for a in arms if (a["record"].get("pr") or {}).get("gate") == "success"),
               "ci_passing": sum(1 for a in arms if (a["record"].get("pr") or {}).get("ci") == "passing"),
               "review_passed": sum(1 for a in arms if (a["record"].get("review") or {}).get("passed") is True),
               "metrics": {}}
        for name, getter in METRICS:
            values = _values(arms, getter)
            row["metrics"][name] = {"n": len(values), "mean": statistics.fmean(values) if values else None,
                                    "sd": statistics.stdev(values) if len(values) > 1 else None,
                                    "min": min(values) if values else None, "max": max(values) if values else None}
        rows.append(row)
    return rows


def _num(value, digits: int = 2) -> str:
    return "-" if value is None else f"{value:,.{digits}f}"


def render_summary(state: dict) -> list:
    lines = [f"effort_ab: results name={state['name']} seed={state['seed']} (mean, sd, min, max per arm; n = arms with that figure)"]
    for row in summary_rows(state):
        lines.append(
            f"effort={row['effort']} arms={row['arms']} finished={row['finished']} effort_confirmed={row['effort_confirmed']} published={row['published']} "
            f"incomplete={row['incomplete']} verification_complete={row['verification_complete']} gate_success={row['gate_success']} "
            f"ci_passing={row['ci_passing']} review_passed={row['review_passed']}")
        for name, cell in row["metrics"].items():
            lines.append(f"  {name} n={cell['n']} mean={_num(cell['mean'])} sd={_num(cell['sd'])} min={_num(cell['min'])} max={_num(cell['max'])}")
    return lines


def _paired(state: dict, metric_getter) -> dict:
    """``{(effort_a, effort_b): [relative difference of b to a per task]}`` over tasks with a finished arm of both (repeats averaged)."""
    per = {}
    for arm in state["arms"]:
        value = _values([arm], metric_getter)
        if value:
            per.setdefault((arm["task"], arm["effort"]), []).append(value[0])
    ordered = sorted(state["efforts"], key=phases.EFFORTS.index)
    out = {}
    for a, b in zip(ordered, ordered[1:]):
        diffs = []
        for task in state["tasks"]:
            if (task, a) in per and (task, b) in per:
                base = statistics.fmean(per[(task, a)])
                if base > 0:
                    diffs.append(statistics.fmean(per[(task, b)]) / base - 1)
        out[(a, b)] = diffs
    return out


def suggested_repeats(state: dict, detect: float) -> tuple:
    """(repeats, per pair [(a, b, n_tasks, mean, sd)]): paired t-test on the relative cost difference, 80 % power, 5 % two-sided: n = 7.85 (sd/d)^2."""
    pairs, need = [], 1
    for (a, b), diffs in _paired(state, METRICS[0][1]).items():
        if len(diffs) < 2:
            continue
        sd = statistics.stdev(diffs)
        pairs.append((a, b, len(diffs), statistics.fmean(diffs), sd))
        need = max(need, math.ceil(math.ceil(7.85 * (sd / detect) ** 2) / max(1, len(state["tasks"]))))
    return min(need, 99), pairs


def render_spread(state: dict, detect: float) -> list:
    repeats, pairs = suggested_repeats(state, detect)
    lines = ["effort_ab: pilot spread (cost per task, one effort against the next; a pilot is a rough guide, not a result)"]
    for a, b, n, mean, sd in pairs:
        lines.append(f"  {a} -> {b} tasks={n} mean_relative_cost_difference={_num(mean * 100, 1)}% sd={_num(sd * 100, 1)}%")
    if pairs:
        lines.append(f"effort_ab: to detect a {detect * 100:.0f}% cost difference (paired, 80% power, 5% two-sided): repeats per arm = {repeats} "
                     f"(arms = {repeats * len(state['tasks']) * len(state['efforts'])})")
    else:
        lines.append("effort_ab: not enough finished arms across two efforts and two tasks to say")
    lines.append("effort_ab: this sizes the cost comparison only; a rate of good pull requests needs far more arms and a person's judgement")
    return lines


# --- command line ------------------------------------------------------------------------------------


def _positive(low: float, high: float, kind=float):
    def parse(text: str):
        try:
            value = kind(text)
        except ValueError:
            raise argparse.ArgumentTypeError("not a number") from None
        if not (low <= value <= high):
            raise argparse.ArgumentTypeError(f"expected {low} to {high}")
        return value
    return parse


def _argv_list(text: str) -> list:
    try:
        value = json.loads(text)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a JSON list of strings") from None
    if not isinstance(value, list) or not value or not all(isinstance(x, str) and x for x in value) or len(value) > 50:
        raise argparse.ArgumentTypeError("expected a JSON list of strings")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Effort A/B harness (issues 132 and 154): never merges, budget-guarded, resumable.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, text in (("run", "run (or resume) the experiment"), ("pilot", "one repeat, then print the spread")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--tasks", required=True, help="task file (JSON)")
        p.add_argument("--state", help="state file (created; run again with the same file to resume)")
        p.add_argument("--records", help="write the JSON record file here (after every arm)")
        p.add_argument("--repo", help="the SANDBOX repository, ORG/NAME")
        p.add_argument("--data", help="a readable copy of the manager's data directory (run directories)")
        p.add_argument("--refresh-json", type=_argv_list, help='JSON argv list run before each poll to refresh --data, e.g. \'["kubectl","cp",...]\'')
        p.add_argument("--cost-file", help='JSON {"cost_usd": N}: the experiment\'s cost so far, instead of the run directories')
        p.add_argument("--efforts", default=",".join(DEFAULT_EFFORTS))
        p.add_argument("--repeats", type=_positive(1, 99, int), default=1 if name == "pilot" else 3)
        p.add_argument("--seed", type=_positive(0, 2**31 - 1, int), help="randomisation seed (default: random, recorded in the state)")
        p.add_argument("--name", help="experiment name (label effort-ab:<name>)")
        p.add_argument("--model", default="sonnet", choices=sorted(model_choice.DEFAULT_ALIASES), help="factory-model label of every arm")
        p.add_argument("--trigger-label", default=DEFAULT_TRIGGER_LABEL)
        p.add_argument("--budget-usd", type=_positive(0, 10000), default=10.0)
        p.add_argument("--arm-estimate-usd", type=_positive(0.01, 1000), default=3.0, help="cost assumed for an arm before one has been measured")
        p.add_argument("--poll-seconds", type=_positive(1, 3600, int), default=60)
        p.add_argument("--arm-timeout-minutes", type=_positive(5, 1440, int), default=120)
        p.add_argument("--ci-wait-minutes", type=_positive(0, 240, int), default=15)
        p.add_argument("--max-hours", type=_positive(1, 720, int), default=12)
        p.add_argument("--detect", type=_positive(0.01, 5), default=0.25, help="relative cost difference the repeats should detect (pilot)")
        p.add_argument("--delete-branches", action="store_true", help="delete each closed pull request's head branch (default: leave it)")
        p.add_argument("--confirm-sandbox", action="store_true", help="REQUIRED: the repository is a sandbox whose pull requests and issues may be closed")
        p.add_argument("--dry-run", action="store_true", help="print the plan only: no call, nothing written")
    report = sub.add_parser("report", help="the numbers of a state file (no GitHub call)")
    report.add_argument("--state", required=True)
    report.add_argument("--data", help="also run tools/effort_report.py on this data directory for the experiment's issues")
    report.add_argument("--detect", type=_positive(0.01, 5), default=0.25)
    return parser


def print_plan(args, state: dict, say) -> None:
    ceiling = sum(p.budget_usd for name, p in phases.POLICIES.items() if name in ("plan", "build", "review", "verify"))
    arms = state["arms"]
    say(f"effort_ab: plan name={state['name']} seed={state['seed']} tasks={len(state['tasks'])} efforts={','.join(state['efforts'])} "
        f"repeats={state['repeats']} arms={len(arms)} model={state['model']}")
    say(f"effort_ab: estimate per_arm_usd={args.arm_estimate_usd:.2f} total_usd={args.arm_estimate_usd * len(arms):.2f} budget_usd={args.budget_usd:.2f} "
        f"arms_within_budget={min(len(arms), int(args.budget_usd // args.arm_estimate_usd))} ceiling_per_attempt_usd={ceiling:.2f}")
    for arm in arms:
        say(f"effort_ab: arm {arm['order']} task={arm['task']} effort={arm['effort']} repeat={arm['repeat']}")


def table_of(state: dict, data: str, say) -> None:
    issues = {a["issue"] for a in state["arms"] if a.get("issue")}
    if not issues:
        return
    report = effort_report.aggregate([data], issues, True)
    say(effort_report.render(report))
    say("effort_ab: every arm's pull request was closed unmerged, so the outcome column above is `closed` by design; quality is in the record file")


def main(argv: list | None = None, client=None, say=print, now=time.time, sleep=time.sleep) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _main(args, client, say, now, sleep)
    except Refused as refusal:
        say(f"effort_ab: refused: {refusal}")
        return EXIT_REFUSED


def _main(args, client, say, now, sleep) -> int:
    if args.command == "report":
        state = load_state(args.state)
        if state is None:
            raise Refused("the state file does not exist")
        for line in render_summary(state) + render_spread(state, args.detect):
            say(line)
        if args.data:
            table_of(state, args.data, say)
        return EXIT_DONE
    if args.command == "pilot":
        args.repeats = 1
    efforts = parse_efforts(args.efforts)
    tasks = load_tasks(args.tasks)
    if args.dry_run:
        args.repo = args.repo or "dry/run"
        print_plan(args, new_state(args, tasks, efforts, args.repeats), say)
        return EXIT_DONE
    args.records = args.records or (args.state + ".records.json" if args.state else None)
    for name in ("repo", "state"):
        if not getattr(args, name):
            raise Refused(f"--{name} is required")
    if not args.confirm_sandbox:
        raise Refused("--confirm-sandbox is required: every pull request and issue of the experiment is closed unmerged")
    try:
        args.repo = config.check_repo(args.repo)
    except ValueError:
        raise Refused("the repository is not ORG/NAME") from None
    if not TRIGGER_LABEL.fullmatch(args.trigger_label) or args.trigger_label.casefold() == issue_labels.IGNORE_LABEL or args.trigger_label.startswith("factory:"):
        raise Refused("the trigger label is not a valid trigger label")
    if not args.data and not args.cost_file:
        raise Refused("--data (run directories) or --cost-file is needed to read the cost")
    state = load_state(args.state)
    if state is None:
        state = new_state(args, tasks, efforts, args.repeats)
    else:
        same = (state["repo"] == args.repo and state["efforts"] == efforts and state["tasks"] == [t["id"] for t in tasks]
                and state["tasks_digest"] == tasks_digest(tasks) and state["model"] == args.model
                and (args.seed is None or args.seed == state["seed"]) and (args.name in (None, state["name"])))
        if not same:
            raise Refused("the state file belongs to a different experiment (repository, tasks, efforts, model, seed or name)")
        extend_state(state, tasks, max(args.repeats, state["repeats"]))
    save_json(args.state, state)
    client = client or Client(args.repo, args.refresh_json)
    runner = Runner(args, state, tasks, client, say, now, sleep)
    try:
        styles = {f"{model_choice.EFFORT_LABEL_PREFIX}{e}": ("d4c5f9", f"Factory: build and verify this issue at {e} effort") for e in efforts}
        styles[f"{model_choice.MODEL_LABEL_PREFIX}{args.model}"] = ("bfd4f2", f"Factory: build and verify this issue with the {args.model} model")
        styles[runner.label] = (EXPERIMENT_COLOR, "Factory effort A/B experiment")
        for label, (color, description) in styles.items():
            client.ensure_label(label, color, description)
    except ClientError as error:
        say(f"effort_ab: stopped: a call failed op={error.op}")
        return EXIT_STOPPED
    print_plan(args, state, say)
    code = runner.run()
    for line in render_summary(state):
        say(line)
    if args.command == "pilot":
        for line in render_spread(state, args.detect):
            say(line)
    if args.data:
        table_of(state, args.data, say)
    return code


if __name__ == "__main__":
    sys.exit(main())

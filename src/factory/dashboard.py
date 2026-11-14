"""Read-only JSON for the dashboard: what Factory works on now, and what it did before.

Four ``GET`` endpoints of the manager (docs/manager.md, "Dashboard endpoints"). They read the run
directories (``plan.json``, ``state.json``, the supervisor control record) and the history ledger
(``history.py``) directly, as ``status`` does, and never start a process: no ``gh``, no CLI. The pull
request state they show is therefore what ``state.json`` records, overlaid with the last state a status
read of the guardrail or the janitor wrote to the ledger, each with its ``observed_at`` so the reader can
see how stale it is, and ``fresh`` (false when that read could not look the pull request up).

* ``overview``: per repository the supervisor and, for each active run directory (one with activity in the
  last ``ACTIVE_WINDOW_SECONDS`` or a live supervisor), its jobs in flight with phase, age, cost against
  ``run_budget_usd``, pull request, owner and next action. Older run directories are only counted: they
  are in the history.
* ``history_runs`` and ``history_jobs``: every past run and job from the ledger, purged ones included,
  filterable by repository, outcome and date, paged by a keyset cursor.
* ``job_detail``: one job, from the live run while it exists, otherwise from the ledger.

Nothing here is unbounded: bytes read per file and per ledger read, runs and jobs per response and the page
size are capped. Free text from a run (a finding, the next action) is control-stripped and clipped; job
errors, retry reasons and criteria text are not returned at all. Never a log: logs are served only by ``logs.py``, to Operators.

Importing this module has no side effects.
"""

from __future__ import annotations

import json
import os
import re
import stat
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import parse_qsl

from . import history, memwatch, planner
from .config import RUN_SLUG, check_repo, data_paths, load_repo_config
from .manager import JOB_ID, _scan_runs
from .model import validate_plan
from .operations import policy_of
from .state import timestamp
from .status import cost_usd, status_report

MAX_STATE_BYTES = 8 * 1024 * 1024
MAX_REPOS = 100
MAX_ACTIVE_RUNS = 50  # per repository
MAX_ACTIVE_ROWS = 500  # job rows in one overview
ACTIVE_WINDOW_SECONDS = 12 * 3600  # a run with no activity for this long is left off the overview (it is in the history)
LIVE_SUPERVISOR = frozenset({"running", "draining", "planning"})  # a run with such a supervisor is active however quiet it is
JOB_STAMPS = ("updated_at", "status_started_at", "phase_started_at", "created_at")
SUPERVISOR_STAMPS = ("started_at", "finished_at", "drain_requested_at")
ACTIVITY_FILES = ("plan.json", "state.json", "supervisor-control.json")
# Seconds a supervisor may seem to have started after its control record says it did: the boot time in /proc is whole
# seconds, and a small step of the clock moves it. A process that started later is another one that got the same id.
SUPERVISOR_START_SLACK = 60.0
MAX_EMBEDDED_JOBS = 25  # jobs shown inside a run of the history list
DEFAULT_RUNS = 25
MAX_RUNS_PAGE = 100
DEFAULT_JOBS = 50
MAX_JOBS_PAGE = 200
MAX_PHASE_EVENTS = 200
MAX_FINDINGS = 20
MAX_TEXT = 300
MAX_QUERY_CHARS = 1024
OVERVIEW_TTL = 5.0  # seconds an overview is reused: polling the dashboard costs the disk once per interval

_WHEN = re.compile(r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?)?")
_CURSOR_EPOCH = re.compile(r"\d{1,12}\.\d{6}")
_SHA = re.compile(r"[0-9a-f]{40}")


class QueryError(ValueError):
    """A query parameter that is unknown, repeated or invalid; ``parameter`` is safe to show."""

    def __init__(self, parameter: str):
        super().__init__(parameter)
        self.parameter = parameter


# --- query parameters --------------------------------------------------------------------


def _repo(value: str) -> str:
    return check_repo(value)


def _run(value: str) -> str:
    if not RUN_SLUG.fullmatch(value):
        raise ValueError(value)
    return value


def _outcome(value: str) -> str:
    if value not in history.OUTCOMES:
        raise ValueError(value)
    return value


def _since(value: str) -> float:
    return _when(value, end=False)


def _until(value: str) -> float:
    return _when(value, end=True)


def _when(value: str, end: bool) -> float:
    """An ISO date or date and time as seconds; a bare ``until`` date covers that whole day."""
    if not _WHEN.fullmatch(value):
        raise ValueError(value)
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        if end and len(value) == 10:
            moment += timedelta(days=1)
        return moment.timestamp()
    except (OverflowError, OSError):  # the year 9999 and the like
        raise ValueError(value) from None


def _limit(top: int) -> Callable[[str], int]:
    def check(value: str) -> int:
        if not value.isascii() or not value.isdigit() or not 1 <= int(value) <= top:
            raise ValueError(value)
        return int(value)

    return check


def _cursor(value: str) -> tuple:
    """``<epoch>|<org>/<name>|<run>[|<job>]``: the sort key of the last row of the previous page."""
    parts = value.split("|")
    if len(parts) not in (3, 4) or not _CURSOR_EPOCH.fullmatch(parts[0]):
        raise ValueError(value)
    check_repo(parts[1])
    _run(parts[2])
    if len(parts) == 4 and not JOB_ID.fullmatch(parts[3]):
        raise ValueError(value)
    return (float(parts[0]), *parts[1:])


def parse_query(target: str, allowed: dict) -> dict:
    """The validated query string of a request target (``{}`` for none); anything else is a ``QueryError``."""
    query = target.split("#", 1)[0].partition("?")[2]
    if not query:
        return {}
    if len(query) > MAX_QUERY_CHARS:
        raise QueryError("query")
    try:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True, max_num_fields=len(allowed) + 1)
    except ValueError:
        raise QueryError("query") from None
    values: dict = {}
    for name, value in pairs:
        if name not in allowed or name in values:
            raise QueryError("query")  # an unknown name is never echoed
        try:
            values[name] = allowed[name](value)
        except ValueError:
            raise QueryError(name) from None
    return values


RUN_FILTERS = {
    "repo": _repo,
    "outcome": _outcome,
    "since": _since,
    "until": _until,
    "limit": _limit(MAX_RUNS_PAGE),
    "cursor": _cursor,
}
JOB_FILTERS = {
    **RUN_FILTERS,
    "run": _run,
    "limit": _limit(MAX_JOBS_PAGE),
}


# --- bounded reads of a run directory ------------------------------------------------------------


def read_json(path, cap: int = MAX_STATE_BYTES) -> dict | None:
    """A regular file (never through a symlink) of at most ``cap`` bytes as a JSON object, else None."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > cap:
                return None
            raw = handle.read(cap + 1)
        document = json.loads(raw) if len(raw) <= cap else None
    except (OSError, ValueError, RecursionError):
        return None
    return document if isinstance(document, dict) else None


def text_of(value: object, limit: int = MAX_TEXT) -> str:
    """One printable line, clipped: free text from a run, made safe to show."""
    flat = " ".join("".join(ch if ch.isprintable() else " " for ch in str(value)).split())
    return flat if len(flat) <= limit else f"{flat[: limit - 3]}..."


def stop_view(value: object, status: object) -> dict | None:
    """The record of a stop (``state.json`` job ``stop``) as the dashboard shows it, or None.

    A fixed word for its state, stamps, the phase, who asked, and the operator's reason as clipped plain text
    (it is data typed by a person: the page shows it as text and nothing else ever reads it).

    The record belongs to the job only while it is stopped, or running with the request pending: a job that
    went on (resumed, retried, recovered) shows no stop, whatever a stale record says.
    """
    if not isinstance(value, dict):
        return None
    if (status, value.get("state")) not in {("stopped", "stopped"), ("running", "stopping")}:
        return None
    reason = value.get("reason")
    return {
        "state": history.word_of(value.get("state")),
        "requested_at": history.stamp_of(value.get("requested_at")),
        "requested_by": text_of(value.get("requested_by"), 128) if value.get("requested_by") else None,
        "reason": text_of(reason, 200) if isinstance(reason, str) and reason else None,
        "phase": history.phase_of(value.get("phase")),
        "stopped_at": history.stamp_of(value.get("stopped_at")),
        "terminated": value.get("terminated") if isinstance(value.get("terminated"), bool) else None,
    }


def _planner_usage(run_dir) -> dict | None:
    records = planner.planner_usage_records(run_dir)
    return {"total_cost_usd": cost_usd(records)} if records else None


def _load_plan(run_dir) -> dict | None:
    """The validated plan of a run directory, or None when it cannot be read."""
    document = read_json(run_dir / "plan.json")
    if document is None:
        return None
    try:
        return validate_plan(document)
    except Exception:  # noqa: BLE001  (a plan file is data: whatever is wrong with it, it is not readable)
        return None


def _load_run(run_dir, started: bool) -> tuple:
    """(validated plan, state document or None) of a run directory, or (None, None) when it cannot be read."""
    plan = _load_plan(run_dir)
    if plan is None:
        return None, None
    state = read_json(run_dir / "state.json") if started else None
    return plan, state


def _modified(run_dir) -> float | None:
    """The newest modification time of the run directory and of the files that hold its stamps, or None.

    Each is looked at with ``lstat``: a symlink is never followed, and one that cannot be looked at (gone,
    not permitted) is skipped. A file written atomically (``atomic_json``) also renews the directory's time.
    """
    newest = None
    for path in (run_dir, *(run_dir / name for name in ACTIVITY_FILES)):
        try:
            moment = os.lstat(path).st_mtime
        except OSError:
            continue
        newest = moment if newest is None else max(newest, moment)
    return newest


def _last_stamp(state: dict | None, supervisor: dict) -> float | None:
    """The newest valid stamp a run recorded, as seconds, or None when it recorded none.

    The stamps are those ``State`` writes on every job of ``state.json`` and those of the supervisor control
    record's current entry (``_supervisor`` has already checked them). The ledger is not read: a status read
    of the guardrail or the janitor writes ``pr_observed`` entries for runs that do nothing any more.
    """
    moments = [history.epoch_of(supervisor.get(key)) for key in SUPERVISOR_STAMPS]
    jobs = state.get("jobs") if isinstance(state, dict) else None
    for job in jobs.values() if isinstance(jobs, dict) else ():
        if isinstance(job, dict):
            moments.extend(history.epoch_of(job.get(key)) for key in JOB_STAMPS)
    return max((moment for moment in moments if moment is not None), default=None)


def _stamp_at(moment: float | None) -> str | None:
    """Seconds as a UTC ISO 8601 stamp (the form ``timestamp`` writes), or None."""
    if moment is None:
        return None
    try:
        return datetime.fromtimestamp(moment, timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):  # a stamp out of the range a datetime can hold
        return None


def _report(plan: dict, state: dict | None, run_dir) -> dict | None:
    """The read-only status report of a run: no GitHub lookup (``pr_snapshots`` stays empty)."""
    data = state if state and isinstance(state.get("jobs"), dict) else {"jobs": {}}
    try:
        return status_report(plan, data, planner_usage=_planner_usage(run_dir), pr_snapshots={})
    except Exception:  # noqa: BLE001  (a state file is data: a malformed one is a row with an error, never a 500)
        return None


# --- the service ------------------------------------------------------------------------------------


class Dashboard:
    """The read endpoints of one ``Manager``. Holds a small cache and no other state."""

    def __init__(self, manager, clock: Callable[[], float] = time.monotonic):
        self.manager = manager
        self._clock = clock
        self._overview_lock = threading.Lock()
        self._overview: tuple | None = None
        self._history_lock = threading.Lock()
        self._folded: tuple | None = None

    # -- the ledger, folded ---------------------------------------------------------------

    def _history(self) -> tuple:
        """(runs by (repo, run), whether older events were left unread): re-read only when a segment changed."""
        ledger = self.manager.ledger
        with self._history_lock:
            signature = ledger.signature()
            if self._folded is None or self._folded[0] != signature:
                snapshot = ledger.read(history.DEFAULT_READ_BYTES)
                self._folded = (signature, history.fold(snapshot.events), snapshot.truncated)
            return self._folded[1], self._folded[2]

    def ledger_runs(self) -> tuple:
        """(runs by (repo, run), whether older events were left unread): the folded ledger the metrics also read."""
        return self._history()

    def _ledger_info(self, truncated: bool) -> dict:
        return {**self.manager.ledger.describe(), "truncated": truncated}

    # -- active ---------------------------------------------------------------------------

    def overview(self) -> dict:
        with self._overview_lock:
            now = self._clock()
            if self._overview is not None and now - self._overview[0] < OVERVIEW_TTL:
                return self._overview[1]
            payload = self._build_overview()
            self._overview = (now, payload)
            return payload

    def _build_overview(self) -> dict:
        manager = self.manager
        runs, truncated = self._history()
        generated = timestamp()
        horizon = history.epoch_of(generated) - ACTIVE_WINDOW_SECONDS  # the wall clock of the answer, not the cache's
        budget = MAX_ACTIVE_ROWS
        repos = []
        names = manager.configured_repos()
        cut = len(names) > MAX_REPOS
        for repo in names[:MAX_REPOS]:
            try:
                cfg = load_repo_config(repo, config_dir=manager.settings.config_dir)
            except ValueError:
                repos.append({"repo": repo, "error": "invalid configuration"})
                continue
            paths = data_paths(cfg, data_dir=manager.settings.data_dir)
            policy = policy_of(cfg)
            active = manager.children.active_for_repo(repo)
            listed = []
            inactive = 0
            by_status: dict = {}
            # The newest run directories are the ones read when there are more than the scan lists, and the ones
            # kept when more than ``MAX_ACTIVE_RUNS`` are active: they get the job rows first, too. A run left out
            # because it is quiet is counted in ``runs_inactive``, which is not a cut: the history has it.
            entries, scan_cut = _scan_runs(paths, newest_first=True)
            cut = cut or scan_cut
            for entry in entries:
                run_dir = paths.run_dir(entry["run"])
                supervisor = self._supervisor(repo, entry["run"], run_dir)
                activity = self._activity(run_dir, entry, supervisor, horizon)
                if activity is None:
                    inactive += 1
                    continue
                if len(listed) >= MAX_ACTIVE_RUNS:
                    cut = True  # an active run over the cap: an older one than those kept
                    continue
                view = self._run_view(
                    repo, paths, entry, policy, runs.get((repo, entry["run"])), generated, budget, supervisor, *activity
                )
                budget -= len(view["jobs"])
                cut = cut or view["jobs_total"] > len(view["jobs"])
                for job in view["jobs"]:
                    by_status[job["status"]] = by_status.get(job["status"], 0) + 1
                listed.append(view)
            repos.append(
                {
                    "repo": repo,
                    "supervisor": (
                        {key: value for key, value in active.describe().items() if key != "pid"} if active else None
                    ),
                    "jobs_by_status": by_status,
                    "runs_total": len(entries),
                    "runs_inactive": inactive,
                    "runs": listed[::-1],  # read newest first, listed oldest first by name as before
                }
            )
        return {
            "generated_at": generated,
            "trigger_paused": manager.trigger_paused(),
            "pr_state": "recorded",  # from state.json and the last status read, never a lookup made for this request
            "truncated": cut,  # a cap on repositories, runs or job rows left something out (quiet runs are not a cut)
            "active_window_seconds": ACTIVE_WINDOW_SECONDS,
            "history": self._ledger_info(truncated),
            "repos": repos,
        }

    @staticmethod
    def _activity(run_dir, entry: dict, supervisor: dict, horizon: float) -> tuple | None:
        """``(state document or None, last activity in seconds or None)`` of an active run, or None for a quiet one.

        A run is active while its supervisor is live (running, draining or planning: a supervisor that waits
        records nothing for hours), or when its last activity is less than ``ACTIVE_WINDOW_SECONDS`` old. The last
        activity is the newest stamp the run recorded (``_last_stamp``); a run that recorded none, such as a planned
        run that never started, falls back to the newest modification time of its directory and files, and so does a
        started run whose ``state.json`` cannot be read when those times are newer than its supervisor's stamps (its job
        stamps are unknown, and the file's time is the only bound on them), so it stays listed with its error.

        A stamp is never newer than the file that holds it, so when those modification times are all older than
        the window and the supervisor is not live the run is quiet without parsing ``state.json``: polling a
        repository with hundreds of old run directories reads one small control record of each and stats the rest.
        """
        live = supervisor["state"] in LIVE_SUPERVISOR
        modified = _modified(run_dir)
        if not live and (modified is None or modified <= horizon):
            return None
        state = read_json(run_dir / "state.json") if entry["started"] else None
        last = _last_stamp(state, supervisor)
        if modified is not None and (last is None or (entry["started"] and state is None)):
            last = modified if last is None else max(last, modified)
        if not live and (last is None or last <= horizon):
            return None
        return state, last

    def _run_view(self, repo, paths, entry, policy, ledger_run, generated, room, supervisor, state, last) -> dict:
        """One active run: ``supervisor``, ``state`` and ``last`` are what ``_activity`` already read of it."""
        run = entry["run"]
        run_dir = paths.run_dir(run)
        view: dict = {
            "run": run,
            "planned": entry["planned"],
            "started": entry["started"],
            "supervisor": supervisor,
            "last_activity_at": _stamp_at(last),
            "budget_usd": policy.run_budget_usd,
            "planner_cost_usd": None,
            "cost_usd": None,
            "jobs_total": 0,
            "jobs": [],
        }
        plan = _load_plan(run_dir)
        if plan is None:
            view["error"] = "plan unreadable" if entry["planned"] else "not planned"
            return view
        report = None if entry["started"] and state is None else _report(plan, state, run_dir)
        if report is None:
            view["error"] = "state unreadable"
            return view
        issues = {job["id"]: history.issues_of(job.get("issues")) for job in plan["jobs"]}
        seen = (ledger_run or {}).get("jobs", {})
        view["planner_cost_usd"] = report["planner_cost_usd"]
        view["cost_usd"] = report["cost_usd"]
        view["observed_at"] = report["observed_at"]
        rows = report["jobs"][: max(0, room)]
        view["jobs_total"] = len(report["jobs"])
        recorded = (state or {}).get("jobs")
        recorded = recorded if isinstance(recorded, dict) else {}
        view["jobs"] = [
            self._job_row(row, issues.get(row["id"], []), seen.get(row["id"]), generated, recorded.get(row["id"]))
            for row in rows
        ]
        return view

    @staticmethod
    def _job_row(row: dict, issues: list, ledger_job: dict | None, generated: str, recorded: object = None) -> dict:
        """One job in flight: numbers and fixed vocabulary, and the next action clipped."""
        observation = None
        if ledger_job and ledger_job.get("handoff_status"):
            observed = ledger_job.get("observed_at")
            now, then = history.epoch_of(generated), history.epoch_of(observed)
            observation = {
                "handoff_status": ledger_job["handoff_status"],
                "observed_at": observed,
                "age_seconds": None if now is None or then is None else round(max(0.0, now - then), 3),
                "fresh": ledger_job.get("observed_fresh"),
            }
        return {
            "id": row["id"],
            "issues": issues,
            "status": history.word_of(row["status"]) or "unknown",
            "phase": history.phase_of(row["phase"]),
            "active_worker": bool(row["active_worker"]),
            "attempt": row["attempt"] if history.is_int(row["attempt"]) else 0,
            "age_seconds": history.number_of(row["age_seconds"]),
            "status_age_seconds": history.number_of(row["status_age_seconds"]),
            "phase_age_seconds": history.number_of(row["phase_age_seconds"]),
            "cost_usd": history.number_of(row["cost_usd"]),
            "pr": history.pr_url_of(row["pr"]),
            "handoff_status": history.word_of(row["handoff_status"]),
            "reason_code": history.code_of(row["reason_code"]),
            "blockers": len(row["blockers"]),
            "waits": [text_of(wait) for wait in row["waits"][:5]],
            "owner": text_of(row["owner"], 60),
            "next_action": text_of(row["next_action"]),
            "pr_observed": observation,
            "stop": stop_view(recorded.get("stop"), recorded.get("status")) if isinstance(recorded, dict) else None,
        }

    def _supervisor(self, repo: str, run: str, run_dir) -> dict:
        """The supervisor of a run: running, draining, planning, idle or stale (a record whose process is gone).

        Read from the control record and the manager's own child registry. It never takes the locks that
        ``control.supervisor_status`` probes with, so a poll can not race a supervisor that is starting; a record of a
        process the manager did not start is checked with ``_is_supervisor`` instead.
        """
        record = read_json(run_dir / "supervisor-control.json", 1024 * 1024)
        current = record.get("current") if record else None
        current = current if isinstance(current, dict) else {}
        recorded = current.get("status")
        child = self.manager.children.get(repo, run)
        if child is not None and child.running():
            state = "planning" if child.kind == "plan" else ("draining" if recorded == "draining" else "running")
        elif recorded in ("running", "draining"):
            state = recorded if _is_supervisor(current.get("pid"), current.get("started_at")) else "stale"
        else:
            state = "idle"
        return {
            "state": state,
            "managed": child is not None and child.running(),
            "started_at": history.stamp_of(current.get("started_at")),
            "finished_at": history.stamp_of(current.get("finished_at")),
            "drain_requested_at": history.stamp_of(current.get("drain_requested_at")),
            "result": recorded if recorded in history.RUN_RESULTS else None,
        }

    # -- history --------------------------------------------------------------------------

    def history_runs(self, target: str) -> dict:
        params = parse_query(target, RUN_FILTERS)
        runs, truncated = self._history()
        rows = [run for run in runs.values() if params.get("repo", run["repo"]) == run["repo"] and self._wanted(run, params)]
        rows.sort(key=_run_key, reverse=True)
        limit = params.get("limit", DEFAULT_RUNS)
        page, more = _page(rows, _run_key, limit, params.get("cursor"))
        return {
            "generated_at": timestamp(),
            "ledger": self._ledger_info(truncated),
            "limit": limit,
            "runs": [history.public_run(run, MAX_EMBEDDED_JOBS) for run in page],
            "next_cursor": _cursor_of(_run_key(page[-1])) if more else None,
        }

    def history_jobs(self, target: str) -> dict:
        params = parse_query(target, JOB_FILTERS)
        runs, truncated = self._history()
        rows = []
        for run in runs.values():
            if params.get("run", run["run"]) != run["run"] or params.get("repo", run["repo"]) != run["repo"]:
                continue
            rows.extend((run, job) for job in run["jobs"].values() if self._wanted(job, params))
        rows.sort(key=_job_key, reverse=True)
        limit = params.get("limit", DEFAULT_JOBS)
        page, more = _page(rows, _job_key, limit, params.get("cursor"))
        return {
            "generated_at": timestamp(),
            "ledger": self._ledger_info(truncated),
            "limit": limit,
            "jobs": [history.public_job(run, job) for run, job in page],
            "next_cursor": _cursor_of(_job_key(page[-1])) if more else None,
        }

    @staticmethod
    def _wanted(item: dict, params: dict) -> bool:
        """Whether a run or job passes the outcome filter and its last event lies within since..until."""
        if "outcome" in params and item["outcome"] != params["outcome"]:
            return False
        if "since" in params and item["last_epoch"] < params["since"]:
            return False
        return not ("until" in params and item["last_epoch"] >= params["until"])

    # -- one job --------------------------------------------------------------------------

    def stop_check(self, repo: str, run: str, job: str, paths) -> tuple | None:
        """``(status word, supervisor state)`` of a job of a run on disk, read-only; None when the run or job is unknown."""
        run_dir = paths.run_dir(run)
        plan, state = _load_run(run_dir, os.path.lexists(run_dir / "state.json"))
        jobs = (state or {}).get("jobs")
        item = jobs.get(job) if isinstance(jobs, dict) else None
        if plan is None or not isinstance(item, dict) or job not in {entry["id"] for entry in plan["jobs"]}:
            return None
        return history.word_of(item.get("status")) or "unknown", self._supervisor(repo, run, run_dir)["state"]

    def job_detail(self, repo: str, run: str, job: str, paths) -> dict | None:
        """The job from the live run directory when it exists, else from the ledger; None when neither knows it."""
        runs, _ = self._history()
        ledger_run = runs.get((repo, run))
        ledger_job = ledger_run["jobs"].get(job) if ledger_run else None
        live = self._live_job(repo, run, job, paths)
        if live is None and ledger_job is None:
            return None
        detail: dict = live if live is not None else {"source": "ledger", "repo": repo, "run": run, "job": job}
        if ledger_job is not None and ledger_run is not None:
            fields = history.public_job(ledger_run, ledger_job)
            detail["history"] = {
                key: fields[key]
                for key in ("outcome", "observed_at", "observed_fresh", "duration_seconds", "first_at", "last_at", "purged", "purge_reason")
            }
            if live is None:
                detail.update({key: fields[key] for key in fields if key not in detail})
                detail["timeline"] = ledger_job["timeline"]
            elif fields["handoff_status"]:
                detail["pr_observed"] = {
                    "handoff_status": fields["handoff_status"],
                    "observed_at": fields["observed_at"],
                    "fresh": fields["observed_fresh"],
                }
        return detail

    def _live_job(self, repo: str, run: str, job: str, paths) -> dict | None:
        run_dir = paths.run_dir(run)
        started = os.path.lexists(run_dir / "state.json")
        plan, state = _load_run(run_dir, started)
        if plan is None or (started and state is None) or job not in {item["id"] for item in plan["jobs"]}:
            return None  # no readable run: the ledger may still know the job
        report = _report(plan, state, run_dir)
        row = next((item for item in report["jobs"] if item["id"] == job), None) if report else None
        if row is None:
            return None
        recorded = ((state or {}).get("jobs") or {}).get(job)
        recorded = recorded if isinstance(recorded, dict) else {}
        issues = next(history.issues_of(item.get("issues")) for item in plan["jobs"] if item["id"] == job)
        review = recorded.get("review") if isinstance(recorded.get("review"), dict) else {}
        findings = review.get("findings") if isinstance(review.get("findings"), list) else []
        events = recorded.get("phase_events") if isinstance(recorded.get("phase_events"), list) else []
        commit = recorded.get("commit")
        return {
            "source": "state",
            "repo": repo,
            "run": run,
            "job": job,
            "issues": issues,
            "status": history.word_of(row["status"]) or "unknown",
            "phase": history.phase_of(row["phase"]),
            "attempt": row["attempt"] if history.is_int(row["attempt"]) else 0,
            "retries": row["retries"] if history.is_int(row["retries"]) else 0,
            "pr": history.pr_url_of(row["pr"]),
            "handoff_status": history.word_of(row["handoff_status"]),
            "reason_code": history.code_of(row["reason_code"]),
            "commit": commit if isinstance(commit, str) and _SHA.fullmatch(commit) else None,
            "cost_usd": history.number_of(row["cost_usd"]),
            "age_seconds": history.number_of(row["age_seconds"]),
            "status_age_seconds": history.number_of(row["status_age_seconds"]),
            "phase_age_seconds": history.number_of(row["phase_age_seconds"]),
            "phase_seconds": {
                name: history.number_of(value)
                for name, value in list(row["phase_seconds"].items())[:20]
                if history.phase_of(name) and history.number_of(value) is not None
            },
            "owner": text_of(row["owner"], 60),
            "next_action": text_of(row["next_action"]),
            "waits": [text_of(wait) for wait in row["waits"][:5]],
            "blockers": len(row["blockers"]),
            "review": {
                "passed": row["review_passed"],
                "matches_head": row["review_matches_head"],
                "findings": [text_of(finding, 500) for finding in findings[:MAX_FINDINGS]],
                "findings_total": len(findings),
            },
            "verification": {
                "complete": row["feature_acceptance_complete"],
                "matches_head": row["acceptance_matches_head"],
            },
            "phase_events": [_phase_event(event) for event in events[-MAX_PHASE_EVENTS:] if isinstance(event, dict)],
            "stop": stop_view(recorded.get("stop"), recorded.get("status")),
            "observed_at": report["observed_at"],
            "pr_state": "recorded",
        }


# --- helpers ------------------------------------------------------------------------------------------


def _alive(pid: object) -> bool:
    """Whether a recorded process id exists (signal 0 is a probe, nothing is sent)."""
    if not history.is_int(pid) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # not ours to signal, but there
        return True
    return True


def _is_supervisor(pid: object, started_at: object) -> bool:
    """Whether the process at ``pid`` can be the supervisor whose control record says it started at ``started_at``.

    A process id is reused (a container restart counts them from 1 again), and a supervisor that was killed leaves its
    record saying running, so the bare probe (``_alive``) can take any process for it. Where ``/proc`` tells, the
    process must also be alive (not a zombie) and have started no later than the record says, give or take
    ``SUPERVISOR_START_SLACK``: the supervisor writes ``started_at`` after it starts, and a process that started later
    is another one. A record without a valid ``started_at`` is then not trusted. Nothing is locked or signalled, so a
    poll cannot race a supervisor that is starting. Without ``/proc`` the bare probe is all there is.
    """
    if not _alive(pid):
        return False
    if not memwatch.supported():
        return True
    recorded = history.epoch_of(started_at)
    started = _process_start(pid)
    return recorded is not None and started is not None and started <= recorded + SUPERVISOR_START_SLACK


def _process_start(pid: int) -> float | None:
    """When a live process started, in seconds since the epoch (from ``/proc``); None when it is gone, a zombie or unreadable."""
    found = memwatch._stat_fields("/proc", pid)
    if found is None or found.state in memwatch._GONE_STATES:
        return None
    try:
        with open("/proc/stat", "rb") as handle:
            boot = next((int(line.split()[1]) for line in handle if line.startswith(b"btime ")), None)
        ticks = os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return None
    return None if boot is None or ticks <= 0 else boot + found.started / ticks


def _phase_event(event: dict) -> dict:
    return history.clean(
        {
            "at": history.stamp_of(event.get("at")),
            "status": history.word_of(event.get("status")),
            "phase": history.phase_of(event.get("phase")),
            "attempt": history.count_of(event.get("attempt")),
            "previous_phase": history.phase_of(event.get("previous_phase")),
            "previous_phase_seconds": history.number_of(event.get("previous_phase_seconds")),
        }
    )


def _run_key(run: dict) -> tuple:
    return (run["last_epoch"], run["repo"], run["run"])


def _job_key(item: tuple) -> tuple:
    run, job = item
    return (job["last_epoch"], run["repo"], run["run"], job["id"])


def _page(rows: list, key: Callable, limit: int, cursor: tuple | None) -> tuple:
    """(the page after ``cursor``, whether more follow): rows sorted newest first by ``key``, a keyset page."""
    if cursor is not None:
        if rows and len(cursor) != len(key(rows[0])):
            raise QueryError("cursor")  # a run cursor on the jobs list, or the other way round
        rows = [row for row in rows if key(row) < cursor]
    return rows[:limit], len(rows) > limit


def _cursor_of(key: tuple) -> str:
    return f"{key[0]:.6f}|{'|'.join(key[1:])}"

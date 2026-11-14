#!/usr/bin/env python3
"""Effort report: tokens, cost, turns and outcome of Factory jobs, tabulated per recorded effort.

    effort_report.py PATH [PATH ...]            print the tables (text)
    effort_report.py --json PATH [...]          the same numbers as one JSON document
    effort_report.py --check PATH [...]         also say where the two sources disagree (a self-check)
    effort_report.py --issues 12,15 PATH [...]  only the jobs of these issues (repeatable)
    effort_report.py --labelled PATH [...]      only the jobs whose effort an issue label chose

The default efforts should rest on measured tokens, cost, turns and
outcome per effort. This script only reads and tabulates. It runs no experiment, picks no sample and draws no conclusion:
a difference between efforts is not evidence without repeats, and whether the work was good is for a person to judge.

What a PATH may be: a run directory (``<data>/repos/<org>/<repo>/runs/<run>``), the ``runs`` directory above them, the data
directory (everything below it is searched; a run directory is never entered and the managed clone is skipped), the history directory (``<data>/history``) or one
ledger segment (``events-YYYYMM-NNN.jsonl``).

Where each figure comes from, and what wins when both sources know a job (a job is a (repository, run, job id) triple; a run
directory is matched with the ledger by its path, which must end in ``repos/<org>/<repo>/runs/<run>``, so a copy kept
elsewhere is a run of its own):

* **Effort** and **model** of a job: ``plan.json`` ``choice`` (``model_choice``: the build and verify phase's resolved model and
  effort, with where each came from, validated by ``model_choice.recorded_choice``). The effort of a job is its **build**
  phase's. The ledger does not record the effort, so a job that is only in the ledger (its run directory was purged), a job
  planned before the choice was recorded and a job whose ``plan.json`` is unreadable are counted under ``unrecorded``. The
  reviewer's and the planner's efforts are not recorded per job at all, so their sessions are ``unrecorded`` in the per phase table.
* **Tokens, cost and turns** of a session: the run directory, ``state.json`` ``jobs.<id>.usage[]``: one record per ``claude -p``
  session with ``usage`` (read by ``status.usage_tokens``), ``total_cost_usd`` and ``num_turns``. A job's figures are the sums
  over its sessions. A session whose record carries no token count (it ended without a result) is a session and is in no figure.
  The ledger's ``job`` entries keep cumulative token counts per phase and the cumulative cost, but no turns, no model and no
  session count: they fill in a job only when the run directory has no measured session for it (a purged run).
* **Outcome**: ``history.outcome_of``. GitHub's word that a pull request was merged or closed exists only in the ledger
  (``pr_observed``; ``state.json`` keeps ``pr_ready`` for a pull request a person merged), so when the ledger says ``merged`` or
  ``closed`` it wins; otherwise the run directory's status decides; otherwise the ledger's word (``abandoned`` for a run purged
  before its jobs finished). A job with no outcome yet is ``other``.
* The planner's sessions belong to no job and are left out.

Every mean is printed with its n (the number of jobs or sessions that have that figure), next to the median and the total, so a
reader never mistakes three sessions for a trend. Cost is the CLI's client-side estimate, not billing data. ``--check`` prints
how many jobs are in both sources and how many of those disagree (the ledger's counts are those at its last recorded transition,
so a small lag is normal).

Read-only: nothing is written, no network, no GitHub. Only regular files are read (never a symlink; dot directories are skipped),
each file is read once and bounded, and the ledger is read newest first up to ``--ledger-mib`` (older events are left out and the
report says so). The output has numbers and a fixed vocabulary only (efforts, phases, outcomes, model ids that match
``claude-...``): never an issue, pull request, comment, prompt or log text, never a run or job name. Standard library plus the
``factory`` package itself (``history``, ``model_choice``, ``status``), so the readings are the ones Factory uses.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import statistics
import sys
from pathlib import Path

try:
    from factory import config, history, model_choice, phases, status
    from factory.dashboard import read_json
except ImportError:  # run from a checkout that is not installed
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from factory import config, history, model_choice, phases, status
    from factory.dashboard import read_json

UNRECORDED = "unrecorded"
OTHER = "other"
OUTCOMES = (*history.OUTCOMES, OTHER)
MAX_RUNS = 20000
MAX_DEPTH = 8
MAX_JOBS_PER_RUN = 2000
MAX_SESSIONS_PER_JOB = 1000
MAX_JOBS = 200000
MAX_LEDGER_FILES = 5000
MAX_ISSUE_NUMBERS = 1000
MAX_LEDGER_MIB = 128
DEFAULT_LEDGER_MIB = history.DEFAULT_READ_BYTES // (1024 * 1024)
MAX_COST_USD = 1e9  # no real session costs this much; it keeps a hostile number out of the sums
CLONE_DIR = config.DataPaths(Path()).clone.name  # the managed checkout next to ``runs``: not a run, and large
LEDGER_KINDS = frozenset({"planned", "job", "pr_observed", "purged"})  # what ``history.fold`` needs for the jobs

# state.json usage field -> the short name of the report
TOKEN_NAMES = {
    "input_tokens": "input",
    "output_tokens": "output",
    "cache_creation_input_tokens": "cache_write",
    "cache_read_input_tokens": "cache_read",
}
METRICS = (*TOKEN_NAMES.values(), "cost_usd", "turns")
_MODEL = re.compile(r"claude-[a-z0-9][a-z0-9.\-]{0,40}(?:\[[0-9a-z]{1,4}\])?")
NOTE = (
    "A difference between efforts is not evidence without repeats: a few jobs per effort, picked by hand, on different issues, "
    "say nothing about quality. This report counts tokens, cost, turns and outcomes; it draws no conclusion."
)


# --- reading -----------------------------------------------------------------------------------------


def _money(value: object) -> float | None:
    number = history.number_of(value)
    return number if number is not None and number <= MAX_COST_USD else None


def _model(value: object) -> str:
    if value is None:
        return UNRECORDED
    return value if isinstance(value, str) and _MODEL.fullmatch(value) else OTHER


def identity(directory: str, job_id: str) -> tuple:
    """(repository, run, job) of a run directory's job: from ``.../repos/<org>/<repo>/runs/<run>``, else a key of its own."""
    parts = Path(directory).parts
    if len(parts) >= 5 and parts[-2] == "runs" and parts[-5] == "repos" and config.RUN_SLUG.fullmatch(parts[-1]):
        try:
            repo = config.check_repo(f"{parts[-4]}/{parts[-3]}")
        except ValueError:
            return (None, directory, job_id)
        return (repo, parts[-1], job_id)
    return (None, directory, job_id)


def session_of(record: object, choice: dict | None) -> dict | None:
    """One ``usage[]`` record as a session: phase, model, the effort recorded for that phase and its figures (None when unmeasured)."""
    if not isinstance(record, dict):
        return None
    phase = record.get("phase") if isinstance(record.get("phase"), str) and record.get("phase") in phases.PHASES else OTHER
    counts = status.usage_tokens(record.get("usage"))
    tokens = {name: counts[name] for name in TOKEN_NAMES if name in counts}
    effort = choice[phase]["effort"] if choice is not None and phase in phases.ISSUE_PHASES else UNRECORDED
    return {
        "phase": phase,
        "model": _model(record.get("model")),
        "effort": effort,
        "measured": bool(tokens),
        "tokens": tokens,
        # a record that ended without a result carries default zeros for cost and turns: they are not figures
        "cost": _money(record.get("total_cost_usd")) if tokens else None,
        "turns": history.count_of(record.get("num_turns")) if tokens else None,
    }


def _sums(sessions: list) -> tuple:
    """(token sums or None, cost or None, turns or None) over the measured sessions."""
    measured = [s for s in sessions if s["measured"]]
    if not measured:
        return None, None, None
    tokens = {name: sum(s["tokens"].get(name, 0) for s in measured) for name in TOKEN_NAMES}
    costs = [s["cost"] for s in measured if s["cost"] is not None]
    turns = [s["turns"] for s in measured if s["turns"] is not None]
    return tokens, (round(sum(costs), 6) if costs else None), (sum(turns) if turns else None)


def read_run(directory: str) -> list | None:
    """The jobs of one run directory (``state.json`` is needed), or None when it cannot be read."""
    state = read_json(Path(directory) / "state.json")
    if state is None:
        return None
    jobs = state.get("jobs")
    if not isinstance(jobs, dict):
        return []
    plan = read_json(Path(directory) / "plan.json")
    planned = {}
    for entry in plan.get("jobs") if isinstance(plan, dict) and isinstance(plan.get("jobs"), list) else ():
        job_id = history.job_id_of(entry.get("id")) if isinstance(entry, dict) else None
        if job_id is not None:
            planned[job_id] = entry
    found = []
    for job_id, current in list(jobs.items())[:MAX_JOBS_PER_RUN]:
        if history.job_id_of(job_id) is None or not isinstance(current, dict):
            continue
        entry = planned.get(job_id)
        choice = model_choice.recorded_choice(entry) if entry is not None else None
        usage = current.get("usage") if isinstance(current.get("usage"), list) else []
        sessions = [s for s in (session_of(r, choice) for r in usage[:MAX_SESSIONS_PER_JOB]) if s is not None]
        tokens, cost, turns = _sums(sessions)
        build = choice["build"] if choice is not None else None
        first_build = next((s["model"] for s in sessions if s["phase"] == "build" and s["model"] != UNRECORDED), None)
        found.append({
            "key": identity(directory, job_id),
            "issues": history.issues_of(entry.get("issues")) if entry is not None else [],
            "effort": build["effort"] if build else UNRECORDED,
            "labelled": bool(build) and build["effort_source"] == "issue",
            "model": build["model"] if build and _model(build["model"]) != OTHER else (first_build or UNRECORDED),
            "outcome": history.outcome_of(history.word_of(current.get("status"))),
            "sessions": sessions,
            "tokens": tokens,
            "cost": cost,
            "turns": turns,
            "origin": "run",
        })
    return found


def find_sources(paths: list) -> tuple:
    """(run directories, ledger files, unreadable count): regular files only, no symlink followed, no dot directory."""
    runs: list = []
    ledgers: list = []
    skipped = 0
    seen: set = set()
    for root in paths:
        root = os.path.abspath(root)
        if os.path.islink(root):
            skipped += 1
            continue
        if os.path.isfile(root):
            if history.SEGMENT.fullmatch(os.path.basename(root)) and root not in seen:
                seen.add(root)
                ledgers.append(root)
            continue
        base_depth = root.rstrip(os.sep).count(os.sep)
        for directory, names, files in os.walk(root, followlinks=False):
            names[:] = sorted(n for n in names if not n.startswith(".") and not os.path.islink(os.path.join(directory, n)))
            if directory.rstrip(os.sep).count(os.sep) - base_depth >= MAX_DEPTH:
                names[:] = []
            if Path(directory).parts[-3:-2] == ("repos",):
                names[:] = [n for n in names if n != CLONE_DIR]  # ``repos/<org>/<repo>/clone`` is a checkout, not runs
            if "state.json" in files or "plan.json" in files:
                names[:] = []  # a run directory holds worktrees: never walk into one
                if directory not in seen and len(runs) < MAX_RUNS:
                    seen.add(directory)
                    runs.append(directory)
                continue
            for name in sorted(files):
                path = os.path.join(directory, name)
                if history.SEGMENT.fullmatch(name) and path not in seen and len(ledgers) < MAX_LEDGER_FILES:
                    seen.add(path)
                    ledgers.append(path)
    return runs, ledgers, skipped


def read_tail(path: str, limit: int) -> tuple | None:
    """(the last ``limit`` bytes of a regular file from a line start, whether the start was cut off), or None."""
    try:
        handle = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        info = os.fstat(handle)
        if not stat.S_ISREG(info.st_mode):
            return None
        want = min(info.st_size, limit)
        offset = info.st_size - want
        data = os.pread(handle, want, offset)
    except OSError:
        return None
    finally:
        os.close(handle)
    if offset > 0:
        _, newline, data = data.partition(b"\n")
        return (data if newline else b""), True
    return data, False


def read_ledger(files: list, max_bytes: int) -> dict:
    """The ledger jobs of ``files`` (newest segments first, up to ``max_bytes``): ``{key: job}`` and what was skipped."""
    chunks: list = []
    remaining = max_bytes
    truncated = False
    unreadable = 0
    for path in sorted(files, key=os.path.basename, reverse=True):
        if remaining <= 0:
            truncated = True
            break
        got = read_tail(path, remaining)
        if got is None:
            unreadable += 1
            continue
        data, cut = got
        remaining -= len(data)
        truncated = truncated or cut
        chunks.append(data)
    events: list = []
    skipped_lines = 0
    for data in reversed(chunks):
        parsed = history.parse_lines(data)
        skipped_lines += sum(1 for line in data.split(b"\n") if line) - len(parsed)
        events.extend(event for event in parsed if event["kind"] in LEDGER_KINDS)
    jobs: dict = {}
    for (repo, run_name), run in history.fold(events).items():
        for job_id, job in run["jobs"].items():
            tokens = {field: sum(row.get(field, 0) for row in job["tokens"].values()) for field in TOKEN_NAMES} if job["tokens"] else None
            jobs[(repo, run_name, job_id)] = {
                "key": (repo, run_name, job_id),
                "issues": job["issues"],
                "effort": UNRECORDED,
                "labelled": False,
                "model": UNRECORDED,
                "outcome": job["outcome"],
                "sessions": [],
                "tokens": tokens,
                "cost": job["cost_usd"] if tokens else None,
                "turns": None,
                "origin": "ledger",
            }
    return {"jobs": jobs, "truncated": truncated, "unreadable": unreadable, "lines_skipped": skipped_lines}


def merge(run_jobs: list, ledger_jobs: dict) -> tuple:
    """(jobs, in both sources, token sums that disagree, cost sums that disagree).

    The run directory wins for effort, model, issues and the figures of a measured job; the ledger's merged or closed word wins
    for the outcome (see the module text); the ledger's figures stand in for a job whose run directory has none.
    """
    merged: dict = {}
    for job in run_jobs:
        merged.setdefault(job["key"], job)
    both = token_mismatches = cost_mismatches = 0
    for key, ledger in ledger_jobs.items():
        run = merged.get(key)
        if run is None:
            merged[key] = ledger
            continue
        both += 1
        run["origin"] = "both"
        if ledger["outcome"] in ("merged", "closed") or run["outcome"] is None:
            run["outcome"] = ledger["outcome"] or run["outcome"]
        run["issues"] = run["issues"] or ledger["issues"]
        if run["tokens"] is None:
            run["tokens"], run["cost"] = ledger["tokens"], ledger["cost"]
        elif ledger["tokens"] is not None:
            token_mismatches += 1 if run["tokens"] != ledger["tokens"] else 0
            if run["cost"] is not None and ledger["cost"] is not None:
                cost_mismatches += 1 if abs(run["cost"] - ledger["cost"]) > 1e-6 else 0
    return list(merged.values()), both, token_mismatches, cost_mismatches


# --- tabulating --------------------------------------------------------------------------------------


def _stat(values: list) -> dict:
    if not values:
        return {"n": 0, "mean": None, "median": None, "total": 0}
    total = sum(values)
    median = statistics.median(values)
    return {"n": len(values), "mean": round(total / len(values), 3), "median": round(float(median), 3), "total": round(total, 6)}


def _metrics(units: list) -> dict:
    """``{metric: {n, mean, median, total}}`` over units with ``tokens``, ``cost`` and ``turns`` (None where unknown)."""
    have = [unit["tokens"] for unit in units if unit["tokens"] is not None]
    out = {short: _stat([tokens.get(field, 0) for tokens in have]) for field, short in TOKEN_NAMES.items()}
    out["cost_usd"] = _stat([u["cost"] for u in units if u["cost"] is not None])
    out["turns"] = _stat([u["turns"] for u in units if u["turns"] is not None])
    return out


def _hit_rate(units: list) -> float | None:
    sums = {field: sum(u["tokens"].get(field, 0) for u in units if u["tokens"] is not None) for field in TOKEN_NAMES}
    return status.cache_hit_rate(sums)


def _order(value: str, vocabulary: tuple) -> tuple:
    return (vocabulary.index(value), "") if value in vocabulary else (len(vocabulary), value)


def job_rows(jobs: list, by_model: bool) -> list:
    """One row per effort (and model): job counts, outcomes, cache hit rate and the per job figures."""
    groups: dict = {}
    for job in jobs:
        groups.setdefault((job["effort"], job["model"] if by_model else None), []).append(job)
    rows = []
    for (effort, model), members in sorted(groups.items(), key=lambda item: (_order(item[0][0], phases.EFFORTS), item[0][1] or "")):
        row = {"effort": effort}
        if by_model:
            row["model"] = model
        row.update({
            "jobs": len(members),
            "jobs_with_usage": sum(1 for job in members if job["tokens"] is not None),
            "sessions": sum(len(job["sessions"]) for job in members),
            "sessions_with_usage": sum(1 for job in members for s in job["sessions"] if s["measured"]),
            "outcomes": {name: sum(1 for job in members if (job["outcome"] or OTHER) == name) for name in OUTCOMES},
            "cache_hit_rate": _hit_rate(members),
            "metrics": _metrics(members),
        })
        rows.append(row)
    return rows


def session_rows(jobs: list) -> list:
    """One row per phase, effort and model over the sessions of the run directories."""
    groups: dict = {}
    for job in jobs:
        for session in job["sessions"]:
            groups.setdefault((session["phase"], session["effort"], session["model"]), []).append(session)
    rows = []
    for (phase, effort, model), members in sorted(
        groups.items(), key=lambda item: (_order(item[0][0], phases.PHASES), _order(item[0][1], phases.EFFORTS), item[0][2])
    ):
        measured = [s for s in members if s["measured"]]
        rows.append({
            "phase": phase, "effort": effort, "model": model, "sessions": len(members), "sessions_with_usage": len(measured),
            "cache_hit_rate": _hit_rate(measured), "metrics": _metrics(measured),
        })
    return rows


def selected(jobs: list, issues: set, labelled: bool) -> list:
    """The jobs the filters keep: of the named issues (a job without a known issue is not), and/or with a label-chosen effort."""
    return [
        job for job in jobs
        if (not issues or issues.intersection(job["issues"])) and (not labelled or job["labelled"])
    ]


def aggregate(paths: list, issues: set | None = None, labelled: bool = False, ledger_mib: int = DEFAULT_LEDGER_MIB) -> dict:
    """The report over every run directory and ledger file under ``paths``."""
    runs, ledgers, skipped = find_sources(paths)
    run_jobs: list = []
    for directory in runs:
        found = read_run(directory)
        if found is None:
            skipped += 1
        else:
            run_jobs.extend(found)
    ledger = read_ledger(ledgers, ledger_mib * 1024 * 1024) if ledgers else {"jobs": {}, "truncated": False, "unreadable": 0, "lines_skipped": 0}
    everything, both, token_mismatches, cost_mismatches = merge(run_jobs, ledger["jobs"])
    over = max(0, len(everything) - MAX_JOBS)
    everything = everything[:MAX_JOBS]
    kept = selected(everything, issues or set(), labelled)
    origins = {name: sum(1 for job in kept if job["origin"] == name) for name in ("run", "ledger", "both")}
    return {
        "by_effort": job_rows(kept, False),
        "by_effort_and_model": job_rows(kept, True),
        "sessions": session_rows(kept),
        "jobs": {
            "found": len(everything), "selected": len(kept), "in_run_directory_only": origins["run"],
            "in_ledger_only": origins["ledger"], "in_both": origins["both"], "over_limit": over,
        },
        "sources": {
            "run_directories": len(runs), "ledger_files": len(ledgers), "unreadable": skipped + ledger["unreadable"],
            "ledger_truncated": ledger["truncated"], "ledger_lines_skipped": ledger["lines_skipped"],
        },
        "filters": {"issues": len(issues or ()), "labelled": labelled},
        "check": {
            "jobs_in_both_sources": both, "token_sums_that_disagree": token_mismatches, "cost_sums_that_disagree": cost_mismatches,
            "jobs_in_the_tables": sum(row["jobs"] for row in job_rows(kept, False)), "jobs_selected": len(kept),
            "sessions_without_token_counts": sum(1 for job in kept for s in job["sessions"] if not s["measured"]),
        },
        "note": NOTE,
    }


# --- output ------------------------------------------------------------------------------------------


def _cell(value: float | None, digits: int = 0) -> str:
    return "-" if value is None else f"{value:,.{digits}f}"


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f} %"


def _grid(head: tuple, rows: list, left: int) -> list:
    table = [head, *rows]
    widths = [max(len(line[i]) for line in table) for i in range(len(head))]
    return ["  ".join(cell.ljust(widths[i]) if i < left else cell.rjust(widths[i]) for i, cell in enumerate(line)) for line in table]


def _render_group(title: str, rows: list, keys: tuple, with_outcomes: bool, noun: str) -> list:
    out = [title]
    if not rows:
        return [*out, "  (nothing)", ""]
    head = (*keys, noun, "with usage", *(("sessions", *OUTCOMES) if with_outcomes else ()), "cache hit")
    counts = []
    for row in rows:
        total = row["jobs"] if with_outcomes else row["sessions"]
        usage = row["jobs_with_usage"] if with_outcomes else row["sessions_with_usage"]
        counts.append((
            *(str(row[key]) for key in keys), str(total), str(usage),
            *((str(row["sessions"]), *(str(row["outcomes"][name]) for name in OUTCOMES)) if with_outcomes else ()),
            _pct(row["cache_hit_rate"]),
        ))
    out.extend(_grid(head, counts, len(keys)))
    out.append("")
    figures = []
    for row in rows:
        for metric in METRICS:
            cell = row["metrics"][metric]
            digits = 2 if metric == "cost_usd" else (1 if metric == "turns" else 0)
            figures.append((
                *(str(row[key]) for key in keys), metric, str(cell["n"]), _cell(cell["mean"], digits),
                _cell(cell["median"], digits), _cell(cell["total"], digits),
            ))
    out.extend(_grid((*keys, "metric", "n", "mean", "median", "total"), figures, len(keys) + 1))
    out.append("")
    return out


def render(report: dict, check: bool = False) -> str:
    out = _render_group("Jobs by effort (the build phase's recorded effort; mean, median and total are per job)",
                        report["by_effort"], ("effort",), True, "jobs")
    out += _render_group("Jobs by effort and model", report["by_effort_and_model"], ("effort", "model"), True, "jobs")
    out += _render_group("Sessions by phase, effort and model (mean, median and total are per session)",
                         report["sessions"], ("phase", "effort", "model"), False, "sessions")
    jobs, sources = report["jobs"], report["sources"]
    out.append(
        f"jobs found: {jobs['found']}; selected: {jobs['selected']} (run directory only {jobs['in_run_directory_only']}, "
        f"ledger only {jobs['in_ledger_only']}, both {jobs['in_both']}); beyond the limit: {jobs['over_limit']}"
    )
    out.append(
        f"run directories: {sources['run_directories']}; ledger files: {sources['ledger_files']}; unreadable or not regular: {sources['unreadable']}; "
        f"ledger lines skipped: {sources['ledger_lines_skipped']}; ledger cut at the byte limit: {'yes' if sources['ledger_truncated'] else 'no'}"
    )
    if report["filters"]["issues"] or report["filters"]["labelled"]:
        out.append(f"filters: issues named {report['filters']['issues']}; only label-chosen efforts: {'yes' if report['filters']['labelled'] else 'no'}")
    if check:
        c = report["check"]
        out.append(
            f"jobs in both sources: {c['jobs_in_both_sources']}; of those, token sums that disagree: {c['token_sums_that_disagree']}, "
            f"cost sums that disagree: {c['cost_sums_that_disagree']}; sessions without token counts: {c['sessions_without_token_counts']}; "
            f"jobs in the tables: {c['jobs_in_the_tables']} of {c['jobs_selected']} selected"
        )
    out.append("effort unrecorded: no plan choice was found for the job (a purged run, or one planned before the choice was recorded), or the phase's effort is not recorded per job")
    out.append("cost is the CLI's client-side estimate, not billing data")
    out.append(report["note"])
    return "\n".join(out)


def _issue_list(text: str) -> list:
    try:
        numbers = [int(part) for part in text.split(",")]
    except ValueError:
        raise argparse.ArgumentTypeError("expected issue numbers separated by commas") from None
    if not numbers or len(numbers) > MAX_ISSUE_NUMBERS or any(not 0 < n < 2**31 for n in numbers):
        raise argparse.ArgumentTypeError("expected issue numbers separated by commas")
    return numbers


def _mebibytes(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number from 1 to {MAX_LEDGER_MIB}") from None
    if not 1 <= value <= MAX_LEDGER_MIB:
        raise argparse.ArgumentTypeError(f"expected a whole number from 1 to {MAX_LEDGER_MIB}")
    return value


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tokens, cost, turns and outcome per recorded effort, read-only (no experiment is run here).")
    parser.add_argument("paths", nargs="+", help="a run directory, a runs directory, the data directory, the history directory or one ledger segment")
    parser.add_argument("--issues", action="append", type=_issue_list, metavar="N[,N...]", help="only the jobs of these issue numbers (repeatable)")
    parser.add_argument("--labelled", action="store_true", help="only jobs whose build effort an issue label chose (factory-effort:<level>)")
    parser.add_argument("--ledger-mib", type=_mebibytes, default=DEFAULT_LEDGER_MIB, metavar="N", help=f"read at most the newest N MiB of the ledger (default {DEFAULT_LEDGER_MIB})")
    parser.add_argument("--json", action="store_true", help="print one JSON document instead of the tables")
    parser.add_argument("--check", action="store_true", help="also report where the run directories and the ledger disagree")
    args = parser.parse_args(argv)
    issues = {number for group in args.issues or () for number in group}
    report = aggregate(args.paths, issues, args.labelled, args.ledger_mib)
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else render(report, args.check))
    return 0


if __name__ == "__main__":
    sys.exit(main())

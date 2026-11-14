"""The manager's operations loop: restart recovery, cost guardrail, auto-purge and label triggers.

Small jobs that run beside the HTTP server so a deployment looks after itself:

* **Restart recovery** (once, after the listening line): for every configured repository
  and every run directory with a ``state.json``, wait for the repository lease to be free
  (a supervisor that died with the previous container leaves a lease that expires), run
  the CLI ``recover`` (jobs whose worker is provably dead become ``interrupted``), read
  ``status --json`` and log one counts-only line. It never starts ``run`` unless the
  repository config says ``auto_resume_after_restart = "once"``.
* **Cost guardrail** (every ``FACTORY_MANAGER_POLL_SECONDS``): for each run this manager
  started whose repository sets ``run_budget_usd``, ``status --json`` and, when
  ``cost_usd`` has reached the limit, the CLI ``drain``.
* **Stop-the-world breaker** (the same tick): for each run this manager started and that is running, or exited
  since the last pass, the ``api_error`` of its failed jobs in ``status --json --offline`` (the report built from
  the run's state alone: no GitHub call per tick; the closed code of ``apierror.py``, never agent text) is counted
  (``factory_api_errors_total``) and, by the rules of ``stopworld.py``, trips the breaker: its own marker ``spend-breaker`` pauses the trigger, every run in progress
  is drained (once, marker ``spend-breaker-drained`` in the run directory), and nothing resumes by itself.
* **Auto-purge** (every ``FACTORY_MANAGER_JANITOR_SECONDS``): the CLI ``purge`` for runs
  that are terminal (every job merged or closed) or abandoned (no live worker and idle
  longer than ``abandon_after``; a run with a ``needs_human_publish`` job is never abandoned, its commit
  exists only there), then ``purge-clone`` once a repository has no run left.
* **Log archive retention** (the janitor sweep, at most daily): removes the retained copies of purged runs' logs that
  are older than ``FACTORY_LOGS_ARCHIVE_RETENTION_DAYS`` (``logarchive.py``). The copies themselves are made by the
  CLI ``purge``, just before it deletes a run directory.
* **False-incomplete rate** (the janitor sweep, per repository, at most hourly): rebuilds the repository's
  ``false_incomplete`` summary from GitHub, read-only (``false_incomplete.py``), into the manager's in-memory cache
  that ``GET /repos`` serves. No thread of its own; a restart starts empty and the next sweep rebuilds it.
* **Idle reconcile** (the janitor's sweep and the trigger pass): for a started run with a published pull request
  (``pr_ready``/``pr_incomplete``), or with a finished job whose observations no issue is known to carry yet (a
  flush GitHub refused; ``observed_issues.pending``, only while issues for observations are on), and no process of
  this manager working in the repository, the CLI ``reconcile`` (lease-guarded, at most every
  ``RECONCILE_SECONDS`` per run) sets ``factory:merged`` after a merge, clears the status label after a close
  without merge, answers the ``factory-verified`` completion label, resolves a pending ``factory/gate`` of a
  ``pr_ready`` job (``ci_gate``, docs/handoff.md "The pending gate") and opens the issues for those observations.
* **Follow-up requests** (the trigger pass, per repository with ``followup = true`` and a configured
  ``FACTORY_GITHUB_APP_SLUG`` or ``mention_handle``): new, authorised ``@<slug>`` mentions on open pull requests of
  the Factory App are recorded under ``followups/`` in the repository's data directory, one fixed line per decision
  (``followup.py``). The oldest queued request then becomes a session on the pull request's own branch: a retry of
  the job that published it (``Manager.retry_followup`` and ``run``), under the guards of ``followup_serve.py``, with
  its outcome recorded as a fixed code (docs/manager.md "Follow-up requests").
* **Epic gate** (the trigger pass, per repository with epic mode on, which is the default; ``epic_gate.py``): an
  issue the planner called an ``epic`` carries ``factory:needs-info`` or ``factory:awaiting-approval``. The pass
  moves needs-info to awaiting-approval when an authorised person has commented after the plan comment, and
  awaiting-approval to ``factory:plan-approved`` when an authorised person applied ``factory-approve-plan`` after
  the plan; it reads only GitHub (never the run directory or a comment's words), writes only that status label,
  and logs one fixed line per decision. A re-plan is the existing re-applied trigger label.
* **Epic phases** (the same pass, only where Factory may comment; ``epic_phases.py``): for an approved epic whose approval still
  stands (``epic_gate.standing``, asked again from GitHub on every pass) it opens ONE phase issue per pass, the next one only after the
  previous phase's pull request is merged, with the origin label ``factory:epic-phase`` and nothing else, and keeps one status
  comment (checklist and ledger) on the parent. It never applies the trigger label and never edits the plan
  comment (the start of a phase is the next bullet, run by ``_work``, not by this pass). It also takes ``Closes #<parent>`` back from the open
  pull request of a halted epic or a replaced plan, and of a parent it cannot prove any more (ignored, no approval label, an approval that does not
  stand, no plan comment, or no ``factory:plan-approved``): the first line of the body only, ``epic_phases._demote_all``. Bounded (10 parents read per pass,
  one new issue per parent per pass, 10 phase issues swept per pass), written by Factory's own identity only, and it opens nothing for a parent that is ignored
  or whose approval does not stand.
* **Epic phase start** (``_start_phase``, called by ``_work`` when nothing else is to start; the same preconditions as any plan: no process
  of ours, the lease free, the repository not refused): the manager begins the build of an approved epic's open phase issue ITSELF. Its
  authority is the approval, proven again from GitHub and the ledger at that moment (``epic_phases.prepare_start``); the trigger label is
  never applied and never relaxed. The attempt is written to the status comment first, the run directory gets a stamp (the verified title
  and body) beside the trigger-run marker, written and removed together, and ``plan --issues=<child>`` reads that stamp, never the issue's
  text. Every later start of that run's ``run`` by the manager (the build, the restart's auto-resume, a dependency resume) proves the
  approval again first (``_epic_verdict``); a person's own actions are not asked. One start per repository per pass; a repository holds it while ``epic_max_open_prs`` pull requests of Factory are open
  (``skipped reason=epic_cap``). A phase that was started and has no open or merged pull request and no run in progress is reported
  stalled and its parent moves from ``factory:plan-approved`` to ``factory:blocked`` (``epic_gate.apply``): the only writers of the epic
  feature are the issue label, the phase issues, the status comment and the first line of a phase pull request body, all under the
  existing ``issues: write`` and ``pull_requests: write``; there is no new GitHub App permission.
* **Trigger pause**: while the marker ``trigger-paused`` exists in the data directory (set and cleared
  by ``POST /trigger-pause``), the label trigger, the dependency resume and the restart auto-resume start
  nothing; runs in progress are not affected (docs/manager.md, "Trigger pause").
* **Label triggers** (every ``FACTORY_MANAGER_TRIGGER_SECONDS``, 0 turns it off): for every
  registered repository that has not opted out (``auto_build = false``), open issues that an
  authorised person labelled ``factory-build`` are planned (``plan`` with exactly those issues)
  and then built (``run``), through the same internal functions as the HTTP actions. The trigger
  label is removed once the plan succeeded; a failed plan keeps it and backs off. The same pass
  resumes a *started* run whose queued jobs wait for dependency merges: from the run directory and
  the dependencies' pull requests on GitHub it starts ``run`` once every dependency is merged, or
  marks the job blocked when a dependency's pull request was closed unmerged. While a supervisor of
  this manager runs for the repository, the pass tops that run up instead of waiting: with a free build
  slot it starts ``plan --issues N,... --append-to <run>`` (``Manager.start_append``, no lease probe) and
  takes the label off once the fragment is in the run's inbox; otherwise it skips with ``slots_full``,
  ``run_full``, ``budget`` or ``draining``. Logic that needs no thread is in ``trigger.py``.

Nothing here reimplements CLI logic and nothing here starts a process by itself: every
command goes through ``Manager.run_short`` (the injected runner, in the same ``status``
and ``other`` lanes the HTTP handlers use) and a resumed ``run`` goes through
``Manager.start_run`` (the injected spawner and the child registry), so pause, drain,
shutdown and ``status`` see it like any other. Waiting is a ``threading.Event.wait`` that
shutdown sets, and the clock and the wait are injectable, so tests never sleep.

Output is counts, statuses and ids only: never a job error, blocker, issue text or any
CLI stream. A failing pass is one fixed line (the exception type) and the loop goes on.

The three repository settings (``abandon_after``, ``run_budget_usd``,
``auto_resume_after_restart``) are read through ``policy_of`` with the documented
defaults for any that the config object does not carry.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Callable, Collection

from . import (
    append,
    apierror,
    epic,
    epic_gate,
    epic_phases,
    false_incomplete,
    followup,
    history,
    hold_comments,
    issue_labels,
    logarchive,
    observed_issues,
    plan_failure,
    plan_holds,
    planner,
    podcontract,
    podsweep,
    stopworld,
    trigger,
)
from .config import DataPaths, RepoConfig, data_paths, load_repo_config
from .control import supervisor_status
from .followup_serve import FollowupServing
from .github_auth import from_environment
from .manager import (
    JOB_ID,
    ApiError,
    Child,
    CommandResult,
    Manager,
    RunnerClosed,
    SlotsBusy,
    Target,
    _list_runs,
    _scan_runs,
    command_tail,
    escape_field,
)
from .issue_comments import CLOSED_DEPENDENCY_REASON
from .purge import ACTIVITY_FILES, newest_activity_ns
from .state import timestamp
from .status import cost_usd

DEFAULT_ABANDON_AFTER = 7 * 24 * 3600
MIN_ABANDON_AFTER = 3600
# How often a busy lease is probed while recovery waits for it.
LEASE_POLL_SECONDS = 3.0
# Recovery has one chance per run, so a full command lane is retried a few times first.
SLOT_ATTEMPTS = 3
SLOT_RETRY_SECONDS = 1.0

# The window of the planner spend cap (``planner_max_usd_per_hour``) and the skip reason when the cap is reached.
PLANNER_SPEND_WINDOW_SECONDS = 3600
PLANNER_SPEND_REASON = "planner_spend_cap"

# Restart recovery meets a pod session that is still alive (docs/pod-backend.md, slice 20): ``recover`` refuses with
# ``podhandle.PodStillLive`` and the run waits. It is asked again every POD_RETRY_SECONDS, for as long as a pod can still
# run (the longest session the launcher admits, the margin and the termination grace), then skipped with one warning.
POD_RETRY_SECONDS = 30.0
POD_RECOVERY_WAIT_SECONDS = float(
    podcontract.DEADLINE_CLAMP_SECONDS + podcontract.DEADLINE_MARGIN_SECONDS + podcontract.TERMINATION_GRACE_SECONDS
)

RESUME_MARKER = "auto-resumed"
BUDGET_MARKER = "budget-drained"
# The run-directory marker of a drain the stop-the-world breaker requested (``stopworld.py``), so that a restart does not forget it.
BREAKER_DRAIN_MARKER = "spend-breaker-drained"
RESUME_REASON = "automatic resume after manager restart"
# Files the trigger loop leaves in a run directory. TRIGGER_MARKER: this loop planned the run (only
# such a run is finished by it, whatever a human names a run). OWED_MARKER: a retry has queued jobs
# of this run and its `run` has not started yet (the count of issues retried); both survive a restart.
TRIGGER_MARKER = "trigger-run"
OWED_MARKER = "trigger-run-owed"
# Dispositions by which a plan chose not to build an issue and asks a person (see ``_owner``).
# ``epic`` too: a re-plan after the questions were answered is a fresh plan, never a retry (``epic_gate.py``).
NOT_BUILT = ("investigation", "blocked", "epic")
# A planned epic phase whose build cannot be proven is held at most this long, then refused for good; the two fixed reasons of a refusal (see _epic_build_proven).
EPIC_BUILD_HOLD_SECONDS = 24 * 3600
# The run directory's record of the first pass that could not prove the build (whole epoch seconds, one number): it is what keeps the bound across manager restarts.
EPIC_HELD_FILE = "epic-build-held"
EPIC_REFUSED_STANDING = "the approval no longer stands"
EPIC_REFUSED_UNPROVEN = "the approval could not be proven"
# What the proof of a run directory's epic stamp says (``Operations._epic_verdict``): no stamp (a person's run), proven, a definite no, a read cut off or
# answered with 404/410 (asking again changes nothing), a read that failed for a reason that can pass, a stamp that is no stamp.
VERDICT_NONE, VERDICT_PROVEN, VERDICT_STANDING = "none", "proven", "standing"
VERDICT_CUT_OFF, VERDICT_UNREADABLE, VERDICT_STAMP = "cut-off", "unreadable", "stamp"
# Job statuses that end without a pull request and without anyone being at work (an epic phase issue whose job ended so is stalled).
EPIC_DEAD_JOB_STATUSES = frozenset({"failed", "blocked", "stopped", "closed"})
# Under a drain also: a job that was waiting (or never recorded, or cut off by a crash) is never started, while one that is running is still live.
EPIC_DRAINED_DEAD_STATUSES = frozenset({"queued", "interrupted", "not_started"})
MARKER_READ_BYTES = 64
# The jobs of a run whose dependency merges the trigger loop has already answered with a `run`
# (one job id per line). A job listed there is never judged again, so a `run` that exits without
# moving it cannot be started over and over; it needs a person (or a new retry).
DEPENDENCY_MARKER = "dependency-resumed"
DEPENDENCY_MARKER_BYTES = 16 * 1024
MAX_RESUME_ATTEMPTS = 3  # starts per job that a marker may record before only a person moves it on
RESUME_RETRY_SECONDS = 600.0  # the wait after the first such start; it doubles with each further one
# Pull requests read per pass for jobs waiting on a dependency merge; the rest wait for the next pass.
MAX_DEPENDENCY_LOOKUPS = 20

# The idle reconcile of a run (``reconcile``: status labels after a merge, the completion label, the
# resolution of a pending ``factory/gate``) is
# asked for a run at most this often; only a run with a published pull request is asked at all.
RECONCILE_SECONDS = 300.0
PUBLISHED = frozenset({"pr_ready", "pr_incomplete"})
# The false-incomplete rate of a repository is rebuilt from GitHub at most this often (the janitor sweep is
# more frequent): one list of closed pull requests and a few status reads per merged one.
FALSE_INCOMPLETE_SECONDS = 3600.0

DONE = frozenset({"merged", "closed"})
HUMAN_PUBLISH = "needs_human_publish"  # finished, but only a person can push it (docs/operations.md)
LIVE_SUPERVISOR = frozenset({"running", "draining"})
# Raw job statuses in the order the restart line reports them; anything else is "other".
JOB_STATUSES = (
    "interrupted",
    "queued",
    "pr_ready",
    "running",
    "pr_incomplete",
    "failed",
    "blocked",
    "needs_human_publish",
    "stopped",
    "merged",
    "closed",
    "not_started",
)
# The files whose modification time shows that a run is still moving.


# --- repository settings -----------------------------------------------------------


@dataclass(frozen=True)
class Policy:
    """What the operations loop needs to know about one repository."""

    abandon_after: int = DEFAULT_ABANDON_AFTER
    run_budget_usd: float | None = None
    auto_resume_after_restart: str = "never"


def _abandon_after(value: object) -> int:
    # bool is a subclass of int; True must not count as one second.
    ok = isinstance(value, int) and not isinstance(value, bool) and value >= MIN_ABANDON_AFTER
    return value if ok else DEFAULT_ABANDON_AFTER


def _budget_limit(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None


def _resume_mode(value: object) -> str:
    return "once" if value == "once" else "never"


def policy_of(cfg: object) -> Policy:
    """The operations settings of a repository config, with the contract defaults.

    Read through ``getattr`` so a config object that does not carry a setting means
    its default, and a value outside the contract falls back to the safe default
    (a malformed budget is no guardrail, anything but ``"once"`` is ``"never"``),
    never to something that could purge or resume more than the operator asked for.
    """
    return Policy(
        abandon_after=_abandon_after(getattr(cfg, "abandon_after", DEFAULT_ABANDON_AFTER)),
        run_budget_usd=_budget_limit(getattr(cfg, "run_budget_usd", None)),
        auto_resume_after_restart=_resume_mode(getattr(cfg, "auto_resume_after_restart", "never")),
    )


# --- markers -----------------------------------------------------------------------


def claim_marker(path: Path, text: str) -> bool:
    """Create ``path`` holding ``text``; True only for the caller that created it.

    ``O_EXCL`` makes the creation itself the atomic step (it also refuses a planted
    symlink), so a crash loop or two managers can never both win. A crash between the
    create and the write leaves an empty marker, which still counts as taken.
    """
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    return True


def write_marker(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` atomically: temp file next to it, fsync, rename."""
    temp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):  # renamed already, or never created
            os.unlink(temp)
        raise


def parse_time(text: str) -> datetime | None:
    """A timezone-aware datetime from a ``state.timestamp()`` string, else None."""
    try:
        moment = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def read_marker(path: Path) -> tuple[bool, datetime | None]:
    """(exists, the timestamp inside, if it parses). Never blocks and never follows a symlink."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return False, None
    except OSError:  # a symlink or a file we cannot open: something is there
        return True, None
    with os.fdopen(descriptor, "rb") as handle:
        raw = handle.read(MARKER_READ_BYTES)
    return True, parse_time(raw.decode("utf-8", errors="replace"))


def read_owed(path: Path) -> int | None:
    """The issue count in an owed-run marker, 0 when it cannot be read, None when there is no marker."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError:  # a symlink or a file we cannot open: something is there
        return 0
    with os.fdopen(descriptor, "rb") as handle:
        text = handle.read(MARKER_READ_BYTES).decode("utf-8", errors="replace").strip()
    return int(text) if text.isascii() and text.isdigit() and len(text) <= 6 else 0


def read_held(path: Path) -> float | None:
    """The moment in a hold record (whole epoch seconds), None when there is no record or it is not one plain number.

    Never blocks and never follows a symlink. Whether the moment can be believed (it must not lie in the future) is the caller's to judge (``_hold_over``).
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(descriptor, "rb") as handle:
            text = handle.read(MARKER_READ_BYTES).decode("utf-8", errors="replace").strip()
    except OSError:  # no record, a symlink, a directory, or a file we cannot read: nothing to believe
        return None
    return float(int(text)) if text.isascii() and text.isdigit() else None  # at most MARKER_READ_BYTES digits


def read_resumed(path: Path) -> dict | None:
    """The jobs in a dependency-resume marker as ``{job: (starts, time of the last start)}`` (empty without one).

    One line per job: ``<job> <starts> <epoch seconds>``. A bare job id (an older marker) counts as one
    start long ago. None when the marker exists but cannot be read: it covers every job.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return {}
    except OSError:  # a symlink or a file we cannot open: treated as covering every job
        return None
    with os.fdopen(descriptor, "rb") as handle:
        raw = handle.read(DEPENDENCY_MARKER_BYTES)
    jobs: dict = {}
    for line in raw.decode("utf-8", errors="replace").splitlines():
        fields = line.split()
        if not fields:
            continue
        try:
            starts, at = int(fields[1]), float(fields[2])
        except (IndexError, ValueError):
            starts, at = 1, 0.0
        jobs[fields[0]] = (max(starts, 0), at)
    return jobs


def write_resumed(path: Path, jobs: dict) -> None:
    """Replace the dependency-resume marker with ``jobs`` (``{job: (starts, time)}``); no jobs, no marker."""
    if not jobs:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        return
    write_marker(path, "".join(f"{job} {starts} {at:.0f}\n" for job, (starts, at) in sorted(jobs.items())))


def resume_retry_due(entry: tuple, now: float) -> bool:
    """Whether a job that an earlier resume answered may be started again: a bounded, backed-off retry.

    The marker cannot say how that run ended (the exit code lives in memory only, and a manager
    restart forgets it), so a job still queued after its start is tried again after 10 min, 20 min ...
    and left to a person once ``MAX_RESUME_ATTEMPTS`` starts did not move it.
    """
    starts, at = entry
    return starts < MAX_RESUME_ATTEMPTS and now - at >= RESUME_RETRY_SECONDS * 2 ** max(starts - 1, 0)


def drain_covers(path: Path, child: Child) -> bool:
    """Whether the budget marker at ``path`` records a drain of ``child`` (or of an older process).

    A marker older than the child means an operator started the run again after the
    drain, and the new process is guarded afresh. A marker whose time cannot be read
    still means a drain happened, so it covers the child: the guardrail never loops.
    """
    exists, drained_at = read_marker(path)
    if not exists:
        return False
    started = parse_time(child.started)
    return drained_at is None or started is None or drained_at >= started


# --- reading the status document ---------------------------------------------------


def count_jobs(report: dict) -> dict:
    """Jobs per raw status (``JOB_STATUSES`` plus ``other``); a malformed document counts as no jobs."""
    counts = dict.fromkeys((*JOB_STATUSES, "other"), 0)
    jobs = report.get("jobs")
    for job in jobs if isinstance(jobs, list) else ():
        status = job.get("status") if isinstance(job, dict) else None
        known = isinstance(status, str) and status in JOB_STATUSES
        counts[status if known else "other"] += 1
    return counts


def interrupted_jobs(report: dict) -> list:
    """Ids of the interrupted jobs; an id that is not a plain job id is dropped, never put in argv."""
    jobs = report.get("jobs")
    rows = [job for job in jobs if isinstance(job, dict)] if isinstance(jobs, list) else []
    idents = [job.get("id") for job in rows if job.get("status") == "interrupted"]
    return [ident for ident in idents if isinstance(ident, str) and JOB_ID.fullmatch(ident)]


def cost_of(report: dict) -> float | None:
    """``cost_usd`` as a finite float, or None when the document does not carry a usable one."""
    value = report.get("cost_usd")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def is_terminal(report: dict) -> bool:
    """At least one job, and every job merged or closed (by its own status or by its PR's)."""
    jobs = report.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        return False
    return all(
        isinstance(job, dict) and (job.get("status") in DONE or job.get("handoff_status") in DONE)
        for job in jobs
    )


def awaits_person(report: dict) -> bool:
    """Whether a job is finished work that only a person can publish (``needs_human_publish``).

    Its commit exists nowhere but in the run: GitHub refused the push, and the run writes nothing
    while the person decides. The janitor must not take that run for an abandoned one.
    """
    jobs = report.get("jobs")
    return isinstance(jobs, list) and any(
        isinstance(job, dict) and job.get("status") == HUMAN_PUBLISH for job in jobs
    )


def has_live_worker(report: dict) -> bool:
    """Whether a worker or supervisor may still be working; anything unreadable counts as live."""
    count = report.get("active_worker_count")
    if type(count) is not int or count > 0:
        return True
    supervisor = report.get("supervisor")
    if supervisor is None:
        return False
    return not isinstance(supervisor, dict) or supervisor.get("status") in LIVE_SUPERVISOR


def last_activity(run_dir: Path) -> float | None:
    """Newest modification time of the run's state, plan and supervisor-control files.

    A run directory with none of them (a plan that failed early) falls back to its own
    time. None when the directory is gone.
    """
    newest = newest_activity_ns(run_dir)
    return None if newest is None else newest / 1e9


def no_runs_left(paths: DataPaths) -> bool:
    """True when ``runs/`` is missing or empty, the same condition ``purge-clone`` enforces."""
    try:
        with os.scandir(paths.runs) as entries:
            return next(entries, None) is None
    except FileNotFoundError:
        return True


def clone_exists(paths: DataPaths) -> bool:
    """Whether a managed clone is on disk (a dangling symlink counts: purge-clone refuses it loudly)."""
    return os.path.lexists(paths.clone)


# --- log lines ---------------------------------------------------------------------


def _rc(result: CommandResult) -> str:
    return "timeout" if result.timed_out else str(result.returncode)


# The closed set of ``cause=`` codes of a skipped purge or reconcile. Each is matched against the first
# ``factory: ...`` line of the CLI's stderr (``cli.main`` prints one for every refusal) with a pattern that
# fixes its wording, in this order; the first match wins. The code is the only thing that is logged: the
# line itself may carry a job id, a branch or a path, and none of it is echoed. Anything else is ``other``.
_ID = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"
CAUSE_OTHER = "other"
CAUSES: tuple[tuple[str, re.Pattern], ...] = tuple(
    (code, re.compile(pattern))
    for code, pattern in (
        ("unchanged_since", r"the run was used after it was judged finished; not purged"),
        ("alive", rf"{_ID}: child process group \d{{1,10}} is still alive"),
        ("alive", r"supervisor \d{1,10} is still (?:running|draining); wait for it to stop"),
        (
            "pod_live",
            rf"{_ID}: pod session (?:[0-9a-f]{{32}}|\(unreadable record\)) "
            r"(?:is still alive; wait or stop it before (?:resuming|purging|recovering)"
            r"|cannot be verified \(the launcher did not answer\); not (?:resuming|purging|recovering) until it can)"
            rf"|{_ID}: pod worker is still alive or cannot be verified",
        ),
        (
            "foreign_pid",
            rf"{_ID}: child pid [^\s]{{1,20}} was recorded on another host \(or cannot be verified\); "
            r"purge needs the repository lease",
        ),
        ("branch_checked_out", r"factory/[A-Za-z0-9_-]{1,50}/[^\s]{1,200} is checked out in .{1,500}"),
        (
            "other_repo",
            r"the run belongs to a different repository than --repo"
            rf"|worktree {_ID} belongs to a different repository than --repo",
        ),
        ("state_unreadable", r"state\.json is unreadable, so recorded child processes cannot be checked"),
        ("lease_busy", r"another factory supervisor holds the repository lock"),
        ("git_failed", r"git [^\s]{1,40}(?: [^\s]{1,40})? (?:failed|could not run)(?::.*)?"),
        ("remove_failed", r"could not remove run directory: [^:]{1,100}"),
    )
)
_STDERR_READ = 16384  # bytes of stderr looked at: the first ``factory:`` line is near its start


def refusal_cause(stderr: bytes | None) -> str:
    """The fixed ``cause=`` code for a failed CLI command: the first ``factory: `` line of its stderr that is in the table.

    The CLI may print other ``factory: `` lines before the refusal (a config warning, ``cli.py`` loads the
    repository config first), so every such line is tried, in order. A line that matches nothing is skipped, never
    echoed; no match at all is ``other``. Only a code from the closed table is returned.
    """
    text = (stderr or b"")[:_STDERR_READ].decode("utf-8", errors="replace")
    for line in text.splitlines():
        if line.startswith("factory: "):
            message = line[len("factory: ") :]
            for code, pattern in CAUSES:
                if pattern.fullmatch(message):
                    return code
    return CAUSE_OTHER


def succeeded(result: CommandResult | None) -> bool:
    return result is not None and not result.timed_out and result.returncode == 0


def restart_line(repo: str, run: str, counts: dict, recovered: int | str) -> str:
    fields = " ".join(f"{key}={counts[key]}" for key in (*JOB_STATUSES, "other"))
    return f"restart repo={escape_field(repo)} run={escape_field(run)} {fields} recovered={recovered}"


def recovered_count(result: CommandResult) -> int | str:
    """``n`` from "Recovered <n> interrupted job(s)", 0 for "Nothing to recover", else ``unknown``."""
    words = result.stdout.decode("utf-8", errors="replace").split()
    if words[:1] == ["Recovered"] and len(words) == 4 and words[1].isascii() and words[1].isdigit():
        return int(words[1])
    return 0 if words == ["Nothing", "to", "recover"] else "unknown"


# --- label triggers: the default GitHub reader -------------------------------------


def github_reader(manager: Manager, cfg: RepoConfig) -> trigger.GithubReader:
    """The reader for one repository: the Factory App's installation token, or the ambient ``gh`` login.

    The same wiring the CLI uses for the status labels (``github_auth.from_environment``);
    it raises ``ValueError`` for a half-configured App.
    """
    auth = from_environment(cfg.repo, cfg.installation_id, env=manager.env)
    return trigger.GithubReader(cfg.repo, auth, manager.settings.data_dir)


@dataclass(frozen=True)
class PlannedRun:
    """A plan this loop started and has not yet seen end."""

    run: str
    issues: tuple
    stamped: bool = False  # the manager started a phase issue itself: the run directory holds an epic stamp until the plan ends well
    fingerprint: str = ""  # of the issues' labels and updated_at when the plan started (plan_holds.fingerprint_of)


@dataclass
class TopUp:
    """An append this loop started into a running run and has not yet seen end.

    ``labelled`` are the issues whose trigger label is still on: each is taken off once a fragment
    for it exists in the run's inbox (or the supervisor already ingested it into ``plan.json``).
    """

    run: str
    issues: tuple
    labelled: set
    seen: bool = False  # a fragment for at least one of the issues was found
    fingerprint: str = ""  # of the issues' labels and updated_at when the append started (plan_holds.fingerprint_of)


# --- the loop ----------------------------------------------------------------------


class Operations:
    """Restart recovery, cost guardrail and auto-purge for one ``Manager``.

    ``start()`` runs them in three threads that are not daemons: they end when
    ``manager.closing`` is set (``Manager.terminate_children``), which wakes every wait,
    and the manager joins them within its shutdown budget. The public ``recover_all``,
    ``check_budgets`` and ``sweep`` are single passes, so tests drive them directly.
    """

    def __init__(
        self,
        manager: Manager,
        *,
        clock: Callable[[], float] = time.time,
        wait: Callable[[float], bool] | None = None,
        policy: Callable[[object], Policy] = policy_of,
        lease_poll: float = LEASE_POLL_SECONDS,
        github: Callable[[RepoConfig], object] | None = None,
    ):
        self.manager = manager
        self._clock = clock  # wall time, comparable with file modification times
        self._wait = wait or manager.closing.wait  # True means "stop now"
        self._policy = policy
        self._lease_poll = lease_poll
        self._threads: list[threading.Thread] = []
        self._recovered = threading.Event()  # set when the restart pass is over (or never will run)
        self._drained: dict[tuple, Child] = {}  # (repo, run) -> the process drained for budget
        # The stop-the-world breaker: the process drained for it, the exits already read, the failures already
        # counted, and the trip whose marker could not be written yet (retried on the next pass).
        self._breaker_drained: dict[tuple, Child] = {}
        self._exits_read: set = set()
        self._api_counted: dict = {}
        self._pending_trip: str | None = None
        # (target, interrupted jobs) whose automatic resume was held back by the trigger pause; the
        # recovery thread runs them once the pause is cleared. Not persisted: a restart recovers again.
        self._deferred: list[tuple[Target, list]] = []
        # (repo, run) whose restart recovery was refused because a pod session is still alive or cannot be read: asked again
        # every POD_RETRY_SECONDS (``retry_pod_pass``). Not persisted: a restart recovers again.
        self._pod_waiting: set = set()
        self._warned: set = set()
        self._mutex = threading.Lock()
        # Label triggers. Nothing here decides anything that a restart could not re-derive from
        # GitHub labels and the run directories: it only paces (backoff, rate-limited lines),
        # remembers the plan it started, and skips a label decision that cannot have changed.
        self._github = github or partial(github_reader, manager)
        self._readers: dict = {}  # (repo, installation id) -> reader
        self._trigger_busy = threading.Lock()  # one pass at a time
        self._backoff = trigger.Backoff(self._clock)
        self._limiter = trigger.LineLimiter(self._clock)
        self._plans: dict[str, PlannedRun] = {}  # repo -> the plan started by the last pass
        self._holds: dict[str, plan_holds.PlanHolds] = {}  # repo -> its plan-holds.json (plan_holds.py), read when it changes
        self._hold_lines: set = set()  # (repo, issue set, fingerprint) whose hold line was logged
        self._issue_facts: dict[str, dict] = {}  # repo -> {number -> IssueFacts} of the last listing
        self._hold_comments: dict = {}  # repo -> hold_comments.HoldComments: the writer of the comment a hold starts with
        self._topups: dict[str, TopUp] = {}  # repo -> the append started into a running run
        self._consumed: set = set()  # (repo, run) whose trigger labels were taken off
        self._runs: dict[str, str] = {}  # repo -> the run this loop started
        self._resuming: dict[str, tuple] = {}  # repo -> (run, jobs) of a dependency resume this loop started
        self._denied: dict = {}  # (repo, issue) -> last update seen when the actor was refused
        # (repo, issue) whose trigger label could not be taken off after a build -> the id of the
        # label event that was built; None until it is known. Such an issue is not started again
        # unless the label is applied anew (a new event), whatever its other labels say.
        self._unremoved: dict = {}
        self._reconciled: dict = {}  # (repo, run) -> clock() of the last idle reconcile
        self._observed: dict = {}  # (repo, run, job) -> the pull request state last written to the history ledger
        self._archive_swept: float | None = None  # clock() of the last retention sweep of the log archive
        self._measured: dict = {}  # repo -> clock() of the last false-incomplete refresh (not persisted: a restart redoes it)
        self._refused = trigger.Backoff(self._clock)  # (repo, issue) -> pause after a retry the CLI refused
        self._blocking = trigger.Backoff(self._clock)  # (repo, run, job) -> pause after a `block` the CLI refused
        self._followups = trigger.Backoff(self._clock)  # repo -> pause after a follow-up pass that failed as a whole
        self._serving = FollowupServing(self)  # the serve half of the follow-up pass (followup_serve.py)
        self._epics = trigger.Backoff(self._clock)  # repo -> pause after an epic gate pass that failed as a whole
        self._epic_seen: dict = {}  # repo -> {issue -> (updated_at, rescan bucket)} of the issues the gate pass read
        self._epic_phases: dict = {}  # repo -> epic_phases.PhaseState: what the phase pass remembers (cursor, issues already read)
        self._epic_status: dict = {}  # repo -> epic_phases.EpicStatus: the writer of the status comment on an approved parent
        # (repo, run) -> clock() of the first pass that could not prove the build of a planned phase. The run directory keeps the same moment
        # (EPIC_HELD_FILE) so that a restart does not start the count again; this copy is what bounds the hold when that record cannot be written.
        self._build_held: dict = {}

    # -- lifecycle ------------------------------------------------------------------

    def start(self) -> None:
        settings = self.manager.settings
        sweeping = partial(self._loop, "janitor", settings.janitor_seconds, self.sweep)
        jobs = (
            ("recovery", self._recover_then_signal),
            ("guardrail", partial(self._loop, "guardrail", settings.poll_seconds, self.guardrail_pass)),
            ("janitor", partial(self._after_recovery, sweeping)),
        )
        if self.manager.pods is not None:  # the pod backend is wired in (slice 20); without it there is nothing to sweep
            jobs += (("podsweep", partial(self._after_recovery, partial(self._loop, "podsweep", podcontract.ORPHAN_SWEEP_SECONDS, self.pod_sweep_pass))),)
        if self.trigger_wanted():
            watching = partial(self._loop, "trigger", settings.trigger_seconds, self.trigger_pass)
            jobs += (("trigger", partial(self._after_recovery, watching)),)
        for name, target in jobs:
            thread = threading.Thread(
                target=partial(self._guard, name, target), name=f"factory-manager-{name}", daemon=False
            )
            thread.start()
            self._threads.append(thread)

    def trigger_wanted(self) -> bool:
        """Whether the trigger loop should exist: whenever its interval is positive.

        Not only when a repository watches at start: every pass re-reads the configuration and
        skips a repository that opted out without a GitHub call, so a repository registered (or
        switched to ``auto_build = true``) after the manager booted is watched from the next pass.
        """
        return self.manager.settings.trigger_seconds > 0

    @property
    def threads(self) -> list:
        """The threads ``start()`` created; one that has nothing left to do (recovery) may have ended."""
        return list(self._threads)

    def join(self, timeout: float) -> int:
        """Wait up to ``timeout`` seconds for the threads; returns how many are still alive."""
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        return sum(thread.is_alive() for thread in self._threads)

    def _recover_then_signal(self) -> None:
        try:
            self.recover_all()
        finally:
            self._recovered.set()
        self._retry_pod_recoveries()
        self._resume_when_unpaused()

    def _resume_when_unpaused(self) -> None:
        """Hold the thread until the resumes the trigger pause held back have run (a safe deploy restarts the pod paused)."""
        while self._deferred and not self._stopping():
            if self.manager.trigger_paused():
                if self._wait(self._lease_poll):
                    return
                continue
            self.resume_deferred()

    def resume_deferred(self) -> None:
        """One pass: the automatic resumes the pause held back, now that it is clear; nothing while it is set.

        A target the pause holds back again (it was set once more) is kept for the next pass.
        """
        if self.manager.trigger_paused():
            return
        pending, self._deferred = self._deferred, []
        for target, jobs in pending:
            if self._stopping():
                return
            self._guard("recovery", self._auto_resume, target, jobs)

    def _after_recovery(self, then: Callable[[], None]) -> None:
        """Hold the janitor back until the restart pass is over, so that its probes of the
        repository lease cannot make a ``recover`` fail; gives up when shutdown begins."""
        while not self._recovered.wait(0.2):
            if self._stopping():
                return
        then()

    def _loop(self, name: str, interval: float, tick: Callable[[], None]) -> None:
        """Wait, then pass, until the wait says stop. The first pass is one interval after start."""
        while not self._wait(interval):
            self._guard(name, tick)

    def _guard(self, name: str, action: Callable, *args: object) -> object:
        """Run ``action``; a failure is one fixed line (the exception type only) and never leaves the thread."""
        try:
            return action(*args)
        except Exception as error:
            self.manager.sink.warn(f"operations {name} failed ({type(error).__name__})")
            return None

    def _stopping(self) -> bool:
        return self.manager.closing.is_set()

    # -- shared pieces --------------------------------------------------------------

    def _line(self, text: str) -> None:
        self.manager.sink.line(text)

    def _warn_once(self, key: str, message: str) -> None:
        with self._mutex:
            fresh = key not in self._warned
            self._warned.add(key)
        if fresh:
            self.manager.sink.warn(message)

    def _load(self, repo: str) -> tuple[RepoConfig, Policy] | None:
        """The repository's config and policy, re-read on every pass so edits apply; None if it does not load."""
        key = f"config:{repo}"
        try:
            cfg = load_repo_config(repo, config_dir=self.manager.settings.config_dir)
        except ValueError:
            self._warn_once(key, f"operations: the configuration of {escape_field(repo)} does not load")
            return None
        with self._mutex:
            self._warned.discard(key)
        return cfg, self._policy(cfg)

    def _paths(self, cfg: RepoConfig) -> DataPaths:
        return data_paths(cfg, data_dir=self.manager.settings.data_dir)

    def _cli(
        self, repo: str, run_dir: Path | None, tail: list, lane: str = "other", attempts: int = 1
    ) -> CommandResult | None:
        """Run one CLI command through the manager's runner; None when it did not run (already reported)."""
        scope = ["--run-dir", str(run_dir)] if run_dir is not None else []
        argv = [*self.manager.settings.cli, "--repo", repo, *scope, *tail]
        for attempt in range(1, attempts + 1):
            try:
                return self.manager.run_short(argv, lane)
            except SlotsBusy:
                if attempt < attempts and not self._wait(SLOT_RETRY_SECONDS):
                    continue
                if not self._stopping():
                    self.manager.sink.warn("operations skipped a command: no free command slot")
                return None
            except RunnerClosed:
                return None
            except OSError as error:
                self.manager.sink.warn(f"cannot start the factory CLI ({type(error).__name__})")
                return None
        return None

    def _status(self, repo: str, run_dir: Path, attempts: int = 1, offline: bool = False) -> dict | None:
        """The parsed ``status --json`` document of a run, or None if it could not be had.

        ``offline`` adds ``--offline``: the report is built from the run's own state and asks GitHub nothing.
        """
        tail = command_tail("status", {})
        if offline:
            tail = [*tail, "--offline"]
        result = self._cli(repo, run_dir, tail, lane="status", attempts=attempts)
        if not succeeded(result):
            return None
        try:
            report = json.loads(result.stdout.decode("utf-8"))
        except (ValueError, RecursionError):  # not JSON, or not UTF-8
            return None
        return report if isinstance(report, dict) else None

    # -- 1. restart recovery --------------------------------------------------------

    def recover_all(self) -> None:
        """One pass over every configured repository's started runs, then return."""
        for repo in self.manager.configured_repos():
            if self._stopping():
                return
            self._guard("recovery", self._recover_repo, repo)

    def _recover_repo(self, repo: str) -> None:
        loaded = self._load(repo)
        if loaded is None:
            return
        cfg, policy = loaded
        paths = self._paths(cfg)
        resumable = []
        for entry in _list_runs(paths):
            if self._stopping():
                return
            if not entry["started"]:
                continue
            if not self._await_lease(repo, cfg, paths):
                return  # gave up (or shutdown): the rest of this repository waits for the next restart
            found = self._guard("recovery", self._recover_run, repo, cfg, policy, paths, entry["run"])
            if found:
                resumable.append(found)
        # Every run is recovered before any is resumed: a resumed supervisor holds the
        # repository lease, which would make the recovery of the next run fail.
        for target, jobs in resumable:
            if self._stopping():
                return
            self._guard("recovery", self._auto_resume, target, jobs)

    def _retry_pod_recoveries(self) -> None:
        """After the restart pass: ask again, every POD_RETRY_SECONDS, for the runs a live pod held back; bounded.

        Nothing happens (no wait, no line) when no run waits, which is every run without a pod handle. A run still
        held back after POD_RECOVERY_WAIT_SECONDS (longer than any pod can run) is skipped with one warning; the next
        restart recovers it again.
        """
        began = self._clock()
        while self._pod_waiting and not self._stopping():
            if self._wait(POD_RETRY_SECONDS):
                return
            self._guard("recovery", self.retry_pod_pass)
            if self._pod_waiting and self._clock() - began >= POD_RECOVERY_WAIT_SECONDS:
                for repo, run in sorted(self._pod_waiting):
                    self.manager.sink.warn(
                        f"restart repo={escape_field(repo)} run={escape_field(run)} "
                        f"pod still live after {POD_RECOVERY_WAIT_SECONDS:g}s; recovery skipped"
                    )
                self._pod_waiting.clear()
                return

    def retry_pod_pass(self) -> None:
        """One pass: recover again every run a live pod held back, and resume what that recovered (like ``_recover_repo``).

        A run that recovers leaves the waiting set; one still refused for a pod stays; any other outcome is reported by
        ``_recover_run`` as it would have been at start and the run leaves the set. The automatic resumes the trigger
        pause holds back are run as soon as it is clear, so a long wait here does not hold them back.
        """
        for repo, run in sorted(self._pod_waiting):
            if self._stopping():
                return
            loaded = self._load(repo)
            if loaded is None:
                self._pod_waiting.discard((repo, run))
                continue
            cfg, policy = loaded
            paths = self._paths(cfg)
            if not self._await_lease(repo, cfg, paths):
                continue  # the lease is busy: still waiting
            found = self._guard("recovery", self._recover_run, repo, cfg, policy, paths, run)
            if found:
                self._guard("recovery", self._auto_resume, *found)
        if self._deferred and not self.manager.trigger_paused():
            self.resume_deferred()

    def _await_lease(self, repo: str, cfg: RepoConfig, paths: DataPaths) -> bool:
        """Poll until the lease can be acquired and released, up to FACTORY_MANAGER_LEASE_WAIT_SECONDS.

        Elapsed time is the larger of the seconds actually waited and what the injected clock
        says, so a slow probe (a blob backend that answers after its 5 s request timeout) uses
        up the wait instead of stretching it, and it can overshoot by at most one probe. One
        warning when it gives up; nothing while waiting.
        """
        limit = self.manager.settings.lease_wait_seconds
        began, waited = self._clock(), 0.0
        while True:
            state = self.manager.lease_state(cfg, paths, warn=False)
            if state == "free":
                return True
            elapsed = max(waited, self._clock() - began)
            if elapsed >= limit:
                self.manager.sink.warn(
                    f"restart repo={escape_field(repo)} lease {state} after {limit:g}s; recovery skipped"
                )
                return False
            step = min(self._lease_poll, limit - elapsed)
            if self._wait(step):
                return False
            waited += step

    def _recover_run(
        self, repo: str, cfg: RepoConfig, policy: Policy, paths: DataPaths, run: str
    ) -> tuple[Target, list] | None:
        """Recover one run and log its counts; (target, interrupted job ids) when it should be resumed."""
        who = f"repo={escape_field(repo)} run={escape_field(run)}"
        waiting = (repo, run) in self._pod_waiting
        self._pod_waiting.discard((repo, run))
        if self.manager.children.active_for_repo(repo) is not None:
            self._line(f"restart {who} skipped=process-running")
            return None
        run_dir = paths.run_dir(run)
        done = self._cli(repo, run_dir, ["recover"], attempts=SLOT_ATTEMPTS)
        if done is None:
            return None
        if not succeeded(done):
            if not done.timed_out and refusal_cause(done.stderr) == "pod_live":
                # A pod session of this run is alive, or the launcher cannot say: not a failure, a wait. Marking the job
                # interrupted now would let a second session start on its worktree. Asked again by ``retry_pod_pass``.
                self._pod_waiting.add((repo, run))
                if not waiting:
                    self._line(f"restart {who} waiting=pod")
                return None
            self._line(f"restart {who} failed=recover rc={_rc(done)}")
            return None
        recovered = recovered_count(done)
        report = self._status(repo, run_dir, attempts=SLOT_ATTEMPTS)
        if report is None:
            if not self._stopping():  # at shutdown the runner refuses, which is no failure of the run
                self._line(f"restart {who} failed=status recovered={recovered}")
            return None
        counts = count_jobs(report)
        self._line(restart_line(repo, run, counts, recovered))
        if policy.auto_resume_after_restart == "once" and counts["interrupted"]:
            if self._over_budget(run_dir, policy, report):
                self._line(f"resume {who} skipped=over-budget")  # the marker stays unclaimed
                return None
            return Target(repo, run, cfg, paths, run_dir), interrupted_jobs(report)
        return None

    @staticmethod
    def _over_budget(run_dir: Path, policy: Policy, report: dict) -> bool:
        """Whether the run already spent its budget, so a restart must not start it again.

        ``cost_usd`` is cumulative: a run started over its limit is drained again within one
        poll, after paying for another phase. An unreadable cost counts as over when the
        guardrail has drained this run before.
        """
        limit = policy.run_budget_usd
        if limit is None:
            return False
        cost = cost_of(report)
        if cost is None:
            return read_marker(run_dir / BUDGET_MARKER)[0]
        return cost >= limit
    def _auto_resume(self, target: Target, jobs: list) -> None:
        """Retry each interrupted job and start one ``run``; at most once per run directory, ever.

        The marker is created first and exclusively, before anything else happens, so a
        crash anywhere after it (or a crash loop) can never resume the same run twice.
        """
        who = f"repo={escape_field(target.repo)} run={escape_field(target.run)}"
        if self.manager.children.active_for_repo(target.repo) is not None:
            self._line(f"resume {who} skipped=process-running")
            return
        if self.manager.trigger_paused():  # before the claim: it runs when the pause is cleared (resume_deferred)
            self._line(f"resume {who} skipped=trigger-paused")
            if not any(held.run_dir == target.run_dir for held, _ in self._deferred):
                self._deferred.append((target, jobs))
            return
        # The approval of an epic phase is proven again before anything is claimed or retried: a run left as it is can be resumed by a person or by the next restart.
        if not self._epic_resume_proven(target.repo, target.cfg, target, lambda: self._reader(target.cfg), self._line):
            return
        if not claim_marker(target.run_dir / RESUME_MARKER, timestamp()):
            self._line(f"resume {who} skipped=already-resumed")
            return
        retried = 0
        for job in jobs:
            if self._stopping():
                return
            tail = command_tail("retry", {"job": job, "reason": RESUME_REASON})
            result = self._cli(target.repo, target.run_dir, tail)
            if succeeded(result):
                retried += 1
            elif result is not None:
                self._line(f"resume {who} job={escape_field(job)} retry=failed rc={_rc(result)}")
        if not retried:
            self._line(f"resume {who} retried=0 action=none")
            return
        try:
            # The session limit is the repository's, not the CLI default. ``target.cfg`` is already loaded
            # and validated: a second read could fail and silently put the run back on 1800 s.
            reply = self.manager.start_run(target, {"timeout": trigger.settings_of(target.cfg).timeout})
        except ApiError as error:
            self._line(f"resume {who} retried={retried} action=run-refused status={error.status}")
            return
        self._line(f"resume {who} retried={retried} action=run pid={reply.payload.get('pid')}")

    # -- 1b. the pod orphan sweep (slice 20) -----------------------------------------

    def pod_sweep_pass(self) -> None:
        """One pass of the orphan sweep (``podsweep.Sweeper``); nothing at all unless the pod backend is wired in."""
        sweeper = self.manager.pods
        if sweeper is None:
            return
        report = sweeper.run_once(self.recorded_pods, self._supervisor_live)
        text = report.line()
        if text is not None:
            self._line(text)

    def recorded_pods(self) -> podsweep.Recorded:
        """Every pod handle the job states of every configured repository record, and whether the answer is complete.

        A repository whose configuration does not load, a run listing that was cut, a configuration directory
        (``repos``) that is missing and a data directory without its ``repos`` directory make it incomplete: a
        directory that is gone (a config mount or a data volume that is not there) is not an empty set of owners, and
        the sweep then deletes nothing for want of one. A configured repository that has no ``runs`` directory while
        the data volume has its ``repos`` directory has simply never run (it has no pods). A run directory of a
        repository that is no longer configured is not read (its pods look unowned: with no configuration nothing
        can resume them).
        """
        files, complete = [], True
        if not (self.manager.settings.config_dir / "repos").is_dir():
            # ``configured_repos`` would say "none", which is not what a missing directory means.
            return podsweep.read_recorded([], False)
        for repo in self.manager.configured_repos():
            loaded = self._load(repo)
            if loaded is None:
                complete = False
                continue
            paths = self._paths(loaded[0])
            if not paths.runs.is_dir():
                if not paths.root.parent.parent.is_dir():
                    complete = False  # the data volume is not there: ``_scan_runs`` would read "no runs"
                continue
            runs, truncated = _scan_runs(paths)
            complete = complete and not truncated
            files.extend((repo, entry["run"], paths.run_dir(entry["run"]) / "state.json") for entry in runs)
        return podsweep.read_recorded(files, complete)

    def _supervisor_live(self, repo: str, run: str) -> bool:
        """Whether a supervisor may be running the run: one this process started, or a live control record. Unreadable is live.

        The repository lease is NOT probed: a probe can make the ``recover`` of the same run fail with a busy lease.
        """
        if self.manager.children.active_for_repo(repo) is not None:
            return True
        loaded = self._load(repo)
        if loaded is None:
            return True
        try:
            record = supervisor_status(self._paths(loaded[0]).run_dir(run))
        except (OSError, ValueError, KeyError, TypeError):
            return True
        return record is not None and record.get("status") in LIVE_SUPERVISOR and bool(record.get("active"))

    # -- 2. cost guardrail ----------------------------------------------------------

    def guardrail_pass(self) -> None:
        """One tick of the guardrail thread: the cost guardrail, then the stop-the-world breaker."""
        self.check_budgets()
        if not self._stopping():
            self._guard("breaker", self.check_api_errors)

    def check_budgets(self) -> None:
        """One pass over the runs this manager started: drain the ones that reached their budget."""
        for child in self.manager.children.running():
            if self._stopping():
                return
            if child.kind == "run":
                self._guard("guardrail", self._check_budget, child)

    def _check_budget(self, child: Child) -> None:
        loaded = self._load(child.repo)
        if loaded is None:
            return
        cfg, policy = loaded
        limit = policy.run_budget_usd
        if limit is None:
            return
        run_dir = self._paths(cfg).run_dir(child.run)
        key = (child.repo, child.run)
        if self._drained.get(key) is child or drain_covers(run_dir / BUDGET_MARKER, child):
            return
        report = self._status(child.repo, run_dir)
        self._observe(child.repo, child.run, report)
        cost = cost_of(report) if report is not None else None
        if cost is None or cost < limit:
            return
        who = f"repo={escape_field(child.repo)} run={escape_field(child.run)} cost={cost:.2f} limit={limit:.2f}"
        result = self._cli(child.repo, run_dir, command_tail("drain", {}))
        if result is None:
            return
        if not succeeded(result):  # not remembered: the next pass tries again
            self._line(f"budget {who} action=drain-failed rc={_rc(result)}")
            return
        self._drained[key] = child
        self.manager.metrics["factory_budget_drains_total"].inc(repo=child.repo)
        try:
            write_marker(run_dir / BUDGET_MARKER, timestamp())
        except OSError as error:  # the in-memory record still prevents a repeat in this process
            self.manager.sink.warn(f"cannot write the budget marker ({type(error).__name__})")
        self._line(f"budget {who} action=drain")

    # -- 2b. stop-the-world breaker -------------------------------------------------

    def check_api_errors(self) -> None:
        """One pass of the breaker: read the API errors of failed jobs, trip by the rules, keep the runs drained.

        The runs read are the ones this manager started and that are running, and, once each, the ones that exited
        recently: a run whose sessions all fail can end within seconds, before a minute-long poll sees it. While the
        breaker is tripped nothing is read: every run in progress is drained instead (once each).
        """
        breaker = self.manager.breaker
        children = self.manager.children
        running = {(child.repo, child.run): child for child in children.running() if child.kind == "run"}
        if self._pending_trip is not None and not breaker.tripped():
            self._trip(self._pending_trip, None, None)
        if breaker.tripped():
            self._drain_running(running)
            return
        now = self._clock()
        exits = []
        for repo, row in children.recent_exits():
            finished = stopworld.parse_epoch(row.get("finished"))
            if row.get("kind") == "run" and finished is not None and now - finished <= stopworld.FRESH_SECONDS:
                exits.append((repo, row.get("run"), (repo, row.get("run"), row.get("pid"))))
        self._exits_read &= {ident for _, _, ident in exits}  # a row that aged out is forgotten
        watched = [(repo, run, None) for repo, run in running]
        watched.extend((repo, run, ident) for repo, run, ident in exits if ident not in self._exits_read)
        for repo, run, ident in watched:
            if self._stopping() or breaker.tripped():
                break
            if not isinstance(run, str):
                continue
            read = self._guard("breaker", self._read_api_errors, repo, run)
            if ident is not None and read is True:  # an exit whose status could not be had is tried again
                self._exits_read.add(ident)
        if breaker.tripped():  # tripped by this pass: the runs in progress drain now, not a minute later
            self._drain_running(running)

    def _drain_running(self, running: dict) -> None:
        for child in running.values():
            if self._stopping():
                return
            self._guard("breaker", self._drain_for_breaker, child)

    def _read_api_errors(self, repo: str, run: str) -> bool:
        """Count and judge the ``api_error`` of the failed jobs of one run; False when its status could not be read."""
        loaded = self._load(repo)
        if loaded is None:
            return False
        # Offline: ``api_error`` and ``api_error_at`` come from the run's state, so no GitHub call is made per tick.
        report = self._status(repo, self._paths(loaded[0]).run_dir(run), offline=True)
        jobs = report.get("jobs") if report is not None else None
        if not isinstance(jobs, list):
            return False
        now = self._clock()
        for job in jobs:
            row = job if isinstance(job, dict) else {}
            code = row.get("api_error")
            at = stopworld.parse_epoch(row.get("api_error_at"))
            if not isinstance(code, str) or code not in apierror.CODES or code == apierror.NONE or at is None:
                continue
            if now - at > stopworld.FRESH_SECONDS:  # history, not news
                continue
            ident = (repo, run, row.get("id") if isinstance(row.get("id"), str) else None, row.get("attempt"), at)
            if ident in self._api_counted:
                continue
            self._api_counted[ident] = None
            while len(self._api_counted) > stopworld.MAX_SEEN:
                self._api_counted.pop(next(iter(self._api_counted)))
            self.manager.metrics["factory_api_errors_total"].inc(repo=repo, code=code)
            if self.manager.breaker.observe(code, ident, at, now):
                self._trip(code, repo, run)
        return True

    def _trip(self, code: str, repo: str | None, run: str | None) -> None:
        """Set the breaker's marker; a failure to write it is retried on the next pass, never forgotten."""
        try:
            first = self.manager.breaker.trip(code)
        except OSError as error:
            self._pending_trip = code
            self.manager.sink.warn(f"cannot write the breaker marker ({type(error).__name__})")
            return
        self._pending_trip = None
        if not first:
            return
        self.manager.metrics["factory_stop_world_trips_total"].inc(code=code)
        where = f" repo={escape_field(repo)} run={escape_field(run)}" if repo is not None and run is not None else ""
        self._line(f"breaker code={code}{where} action=trip")

    def _drain_for_breaker(self, child: Child) -> None:
        """Ask a run in progress to drain, once per process (the marker in its directory survives a restart)."""
        loaded = self._load(child.repo)
        if loaded is None:
            return
        run_dir = self._paths(loaded[0]).run_dir(child.run)
        key = (child.repo, child.run)
        if self._breaker_drained.get(key) is child or drain_covers(run_dir / BREAKER_DRAIN_MARKER, child):
            return
        who = f"repo={escape_field(child.repo)} run={escape_field(child.run)}"
        result = self._cli(child.repo, run_dir, command_tail("drain", {}))
        if result is None:
            return
        if not succeeded(result):  # not remembered: the next pass tries again
            self._line(f"breaker {who} action=drain-failed rc={_rc(result)}")
            return
        self._breaker_drained[key] = child
        try:
            write_marker(run_dir / BREAKER_DRAIN_MARKER, timestamp())
        except OSError as error:  # the in-memory record still prevents a repeat in this process
            self.manager.sink.warn(f"cannot write the breaker drain marker ({type(error).__name__})")
        self._line(f"breaker {who} action=drain")

    # -- 3. auto-purge --------------------------------------------------------------

    def sweep(self) -> None:
        """One pass over every configured repository's runs: purge the finished and the abandoned."""
        for repo in self.manager.configured_repos():
            if self._stopping():
                return
            self._guard("janitor", self._sweep_repo, repo)
            if not self._stopping():
                self._guard("janitor", self._refresh_false_incomplete, repo)
        self._guard("janitor", self.manager.ledger.prune)  # the history ledger's age and size bounds
        self._guard("janitor", self._sweep_archive)

    def _sweep_archive(self) -> None:
        """Remove retained log copies past their retention, at most once a day; failure is one fixed line and a count."""
        now = self._clock()
        if self._archive_swept is not None and now - self._archive_swept < logarchive.SWEEP_SECONDS:
            return
        config = logarchive.Config.from_env(self.manager.env)
        if not config.enabled:
            return
        self._archive_swept = now
        try:
            backend = logarchive.backend_for(config, self.manager.settings.data_dir, self.manager.env)
            removed = logarchive.sweep(backend, config.retention_days, self._clock)
        except Exception as error:  # the identity, the network or the disk: the next day tries again
            self.manager.metrics["factory_log_archive_failures_total"].inc()
            self._line(f"log-archive sweep failed error={type(error).__name__}")
            return
        if removed:  # a day with nothing to remove says nothing
            self._line(f"log-archive swept removed={removed} retention_days={config.retention_days}")

    def _sweep_repo(self, repo: str) -> None:
        loaded = self._load(repo)
        if loaded is None:
            return
        cfg, policy = loaded
        paths = self._paths(cfg)
        entries = _list_runs(paths)
        if not entries and not clone_exists(paths):
            return  # nothing here to clean up
        # One probe of the repository's lease per repository and pass: a busy lease means a
        # supervisor or an operator command is at work, and the CLI re-checks under the lease anyway.
        state = self.manager.lease_state(cfg, paths, warn=False)
        if state != "free":
            if state == "unavailable":
                self._warn_once(f"lease:{repo}", f"operations: lease backend unavailable for {escape_field(repo)}")
            return
        with self._mutex:
            self._warned.discard(f"lease:{repo}")
        purged = 0
        for entry in entries:
            if self._stopping():
                return
            if self.manager.children.active_for_repo(repo) is not None:
                return  # a process this manager started works in this repository: leave it alone
            gone = bool(self._guard("janitor", self._sweep_run, repo, policy, paths, entry))
            purged += gone
            if not gone:  # a run that stays: its published pull requests may have moved on
                self._guard("janitor", self._reconcile_run, repo, paths, entry)
        # Not only in the pass that purged the last run: a clone removal that failed, timed out
        # or was killed is finished by the next sweep (the CLI refuses while a run remains).
        if no_runs_left(paths) and (purged or clone_exists(paths)):
            self._purge_clone(repo)

    def _refresh_false_incomplete(self, repo: str) -> None:
        """Rebuild the repository's false-incomplete summary from GitHub (read-only) into the manager's cache.

        Runs in the janitor's thread, at most every ``FALSE_INCOMPLETE_SECONDS`` per repository; the first sweep
        after a restart always does it. It needs the Factory App's slug (the author of its pull requests): without
        one the cache says ``unavailable``. A failure keeps the last value (marked stale) and is one fixed line.
        """
        last = self._measured.get(repo)
        if last is not None and self._clock() - last < FALSE_INCOMPLETE_SECONDS:
            return
        loaded = self._load(repo)
        if loaded is None:
            return
        cache = self.manager.false_incomplete
        slug = str(self.manager.env.get("FACTORY_GITHUB_APP_SLUG") or "").strip()
        if not slug:
            cache.failed(repo, false_incomplete.NO_APP)
            return
        self._measured[repo] = self._clock()
        try:
            reader = self._reader(loaded[0])
            now = datetime.fromtimestamp(self._clock(), timezone.utc)
            value = false_incomplete.compute(
                reader, f"{slug}[bot]", getattr(reader, "app_user_id", None), now, stop=self._stopping
            )
        except InterruptedError:
            self._measured.pop(repo, None)
            return
        except Exception as error:  # GitHub, the token broker, a bad config: one line, the old value stays
            cache.failed(repo)
            self._say(repo, f"false-incomplete repo={escape_field(repo)} refresh failed error={type(error).__name__}", "false-incomplete")
            return
        cache.set(repo, value)

    def _sweep_run(self, repo: str, policy: Policy, paths: DataPaths, entry: dict) -> bool:
        """Purge one run if it is terminal or abandoned; True when it was purged."""
        run = entry["run"]
        run_dir = paths.run_dir(run)
        seen = newest_activity_ns(run_dir)  # before the status read: later use of the run changes it
        reason, report = self._purge_verdict(repo, run_dir, entry["started"], policy)
        self._observe(repo, run, report)
        if reason is None:
            return False
        who = f"repo={escape_field(repo)} run={escape_field(run)}"
        tail = command_tail("purge", {})
        if seen is not None:
            tail.append(f"--unchanged-since={seen}")  # the CLI refuses under the lease if the run was used since
        result = self._cli(repo, run_dir, tail)
        if result is None:
            return False
        if not succeeded(result):
            self._line(f"purge-skipped {who} reason={reason} rc={_rc(result)} cause={refusal_cause(result.stderr)}")
            return False
        if logarchive.FAILED_MARKER.encode() in (result.stderr or b""):
            self.manager.metrics["factory_log_archive_failures_total"].inc()
            self._line(f"log-archive failed repo={escape_field(repo)} run={escape_field(run)} the logs went with the run")
        self.manager.children.forget(repo, run)
        self._reconciled.pop((repo, run), None)
        # The run is gone; its history stays. The CLI recorded what the jobs last were, this says why
        # it was purged and what GitHub said about each pull request (best effort, no extra call).
        history.record_purge(self.manager.ledger, repo, run, reason, report)
        for key in [key for key in self._observed if key[:2] == (repo, run)]:
            del self._observed[key]
        self._line(f"purge {who} reason={reason}")
        return True

    def _observe(self, repo: str, run: str, report: dict | None) -> None:
        """Write the pull request states of a status read to the history ledger, when they changed.

        The status reads the guardrail and the janitor make anyway carry GitHub's word on each pull
        request; keeping it costs no GitHub call of its own. A restart repeats each state once.
        """
        jobs = report.get("jobs") if isinstance(report, dict) else None
        for job in jobs if isinstance(jobs, list) else ():
            if not isinstance(job, dict) or not job.get("pr") or not job.get("handoff_status"):
                continue
            key = (repo, run, job.get("id"))
            # Per job: one pull request whose lookup failed does not make its siblings' confirmed words guesses.
            fresh = history.lookup_fresh(report, job.get("id")) is True
            seen = (job.get("pr"), job.get("handoff_status"), fresh)
            if self._observed.get(key) == seen:
                continue
            if history.record_observation(self.manager.ledger, repo, run, job, report.get("observed_at"), fresh):
                self._observed[key] = seen

    # -- idle reconcile of published pull requests --------------------------------------

    @staticmethod
    def _has_published(run_dir: Path) -> bool:
        """Whether the run's ``state.json`` holds a job with a published pull request (``pr_ready``/``pr_incomplete``)."""
        state = trigger.read_plan(run_dir / "state.json")
        jobs = state.get("jobs") if state else None
        return isinstance(jobs, dict) and any(
            isinstance(job, dict) and job.get("status") in PUBLISHED and isinstance(job.get("pr"), str) and job["pr"]
            for job in jobs.values()
        )

    def _has_unflushed(self, repo: str, run_dir: Path) -> bool:
        """Whether a job that ended holds observations no issue is known to carry (and issues for them are on).

        A flush that GitHub refused leaves them in ``state.json`` only; a job that ended blocked, failed or
        waiting for a person has no published pull request to bring the run back here otherwise.
        """
        state = trigger.read_plan(run_dir / "state.json")
        if not observed_issues.pending(run_dir, state.get("jobs") if state else None):
            return False
        loaded = self._load(repo)
        return loaded is not None and observed_issues.wanted(loaded[0], self.manager.env)

    def _reconcile_due(self, repo: str, paths: DataPaths, entry: dict) -> bool:
        last = self._reconciled.get((repo, entry["run"]))
        if not entry["started"] or (last is not None and self._clock() - last < RECONCILE_SECONDS):
            return False
        run_dir = paths.run_dir(entry["run"])
        return self._has_published(run_dir) or self._has_unflushed(repo, run_dir)

    def _reconcile_run(self, repo: str, paths: DataPaths, entry: dict) -> None:
        """Ask the CLI to reconcile one run whose pull requests may have moved on; the caller saw the lease free.

        The CLI takes the lease itself (a busy one refuses) and writes only status labels, PR descriptions
        and ``factory/gate`` (best effort, see ``reconcile`` in the CLI). Only a refusal is reported, as a
        rate-limited fixed line.
        """
        if not self._reconcile_due(repo, paths, entry) or self.manager.children.active_for_repo(repo) is not None:
            return
        run = entry["run"]
        self._reconciled[(repo, run)] = self._clock()
        result = self._cli(repo, paths.run_dir(run), ["reconcile"])
        if result is not None and not succeeded(result):
            who = f"repo={escape_field(repo)} run={escape_field(run)}"
            line = f"reconcile-skipped {who} rc={_rc(result)} cause={refusal_cause(result.stderr)}"
            self._say(repo, line, ("reconcile", run))

    def _reconcile_repo(self, repo: str) -> None:
        """The trigger pass's share: reconcile the due runs of a repository that no supervisor of ours works in
        (a lease held elsewhere ends the CLI step quietly)."""
        loaded = self._load(repo)
        if loaded is None:
            return
        paths = self._paths(loaded[0])
        due = [entry for entry in _list_runs(paths) if self._reconcile_due(repo, paths, entry)]
        if not due or self.manager.children.active_for_repo(repo) is not None:
            return
        # No lease probe here: ``reconcile`` takes the lease itself and ends quietly when it is busy.
        for entry in due:
            if self._stopping():
                return
            self._guard("trigger", self._reconcile_run, repo, paths, entry)

    def _purge_verdict(
        self, repo: str, run_dir: Path, started: bool, policy: Policy
    ) -> tuple[str | None, dict | None]:
        """(``terminal``, ``abandoned`` or None, the status report it read). A run whose status cannot be read,
        or that waits for a person, is left alone."""
        report = None
        if started:  # a run that never started has no status document and no workers
            report = self._status(repo, run_dir)
            if report is None:
                return None, None
            if is_terminal(report):
                return "terminal", report
            if has_live_worker(report):
                return None, report
        active = last_activity(run_dir)
        if active is not None and self._clock() - active > policy.abandon_after:
            if report is not None and awaits_person(report):
                # Finished work whose only copy is in this run, waiting for a person to push it:
                # idleness is not abandonment here. It stays until a person publishes it, retries
                # it or purges the run by hand.
                who = f"repo={escape_field(repo)} run={escape_field(run_dir.name)}"
                self._warn_once(
                    f"awaiting-person:{repo}:{run_dir.name}",
                    f"operations: purge kept {who} reason=awaiting_person",
                )
                return None, report
            return "abandoned", report
        return None, report

    def _purge_clone(self, repo: str) -> None:
        """Remove the managed clone of a repository that has no run left; the CLI re-checks under the lease."""
        result = self._cli(repo, None, ["purge-clone"])
        if result is None:
            return
        who = f"repo={escape_field(repo)}"
        if succeeded(result):
            removed = result.stdout.strip() == b"Removed clone"
            self._line(f"purge-clone {who} removed={'yes' if removed else 'no'}")
        else:
            self._line(f"purge-clone-skipped {who} rc={_rc(result)}")

    # -- 4. label triggers ----------------------------------------------------------

    def trigger_pass(self) -> None:
        """One pass over every watching repository: at most one start per repository, never two passes at once."""
        if not self._trigger_busy.acquire(blocking=False):
            return  # another pass is at work; the next interval looks again
        try:
            if self.manager.trigger_paused():  # no label is read, no plan or run started, no dependency resumed
                self.manager.metrics["factory_watcher_paused_passes_total"].inc()
                self._say("*", "trigger skipped reason=paused", "paused")
                return
            self.manager.metrics["factory_watcher_passes_total"].inc()
            for repo in self.manager.configured_repos():
                if self._stopping():
                    return
                self._guard("trigger", self._trigger_repo, repo)
                if not self._stopping():
                    self._guard("trigger", self._reconcile_repo, repo)
                if not self._stopping():
                    self._guard("trigger", self._followup_repo, repo)
                if not self._stopping():
                    self._guard("trigger", self._epic_repo, repo)
        finally:
            self._trigger_busy.release()

    def _say(self, repo: str, text: str, key: object = None, repeat: float | None = None) -> None:
        """A rate-limited trigger line: the same text for the same key is not repeated within the limiter's period."""
        if self._limiter.allow((repo, key), text, repeat):
            self._line(text)

    def backoff_remaining(self, repo: str) -> float:
        """Seconds left of the label watcher's backoff for ``repo`` (0 when it is not backing off)."""
        return self._backoff.remaining(repo)

    def _skip(self, repo: str, reason: str, issue: int | None = None, repeat: float | None = None) -> None:
        self.manager.metrics["factory_watcher_skips_total"].inc(repo=repo, skip_reason=reason)  # counted even when the line is rate-limited
        who = f"repo={escape_field(repo)}" + (f" issue={issue}" if issue is not None else "")
        self._say(repo, f"trigger {who} skipped reason={reason}", issue, repeat)

    def _planner_capped(self, repo: str, conf: trigger.TriggerSettings, paths: DataPaths) -> bool:
        """Whether the repository's planner spend of the last hour has reached ``planner_max_usd_per_hour``: then skip, start nothing.

        The spend is the sum of ``total_cost_usd`` over the planner usage files of the repository's run directories written in the
        last hour (their modification time). A planner session that failed without writing a usage record is not counted. 0 means no cap.
        """
        cap = conf.planner_usd_per_hour
        if cap <= 0:
            return False
        since = self._clock() - PLANNER_SPEND_WINDOW_SECONDS
        spent = 0.0
        for entry in _scan_runs(paths, newest_first=True)[0]:
            spent += cost_usd(planner.planner_usage_records(paths.run_dir(entry["run"]), since=since))
        if spent < cap:
            return False
        self._skip(repo, PLANNER_SPEND_REASON)
        return True

    def _started(self, repo: str, text: str) -> None:
        """A line that always shows (work began); it also lets the next "skipped" line show again."""
        self._limiter.forget(lambda key: key[0] == repo)
        self._line(text)

    def _plan_failed(self, repo: str, code: str) -> None:
        """Count one failed plan under its closed code (``plan_failure.CODES``; anything else is ``other``)."""
        self.manager.metrics["factory_plan_failures_total"].inc(repo=repo, plan_failure_code=code)

    def _fail(self, repo: str, what: str) -> None:
        """One fixed line for a failure and an exponentially longer pause (an hour at most) for this repository."""
        delay = self._backoff.failed(repo)
        self.manager.metrics["factory_watcher_failures_total"].inc(repo=repo)
        self._line(f"trigger repo={escape_field(repo)} {what} backoff={delay:g}s")

    def _reader(self, cfg: RepoConfig) -> object:
        key = (cfg.repo, cfg.installation_id)
        reader = self._readers.get(key)
        if reader is None:
            reader = self._readers[key] = self._github(cfg)
        return reader

    def _trigger_repo(self, repo: str) -> None:
        loaded = self._load(repo)
        if loaded is None:
            return
        cfg = loaded[0]
        conf = trigger.settings_of(cfg)
        if not conf.enabled:
            self._skip(repo, "disabled", repeat=trigger.NEVER)
            return
        if self._backoff.remaining(repo) > 0:
            self._skip(repo, "backoff")
            return
        try:
            paths = self._paths(cfg)
            if self._top_up(repo, cfg, conf, loaded[1], paths):  # a supervisor of ours runs here
                return
            if self.manager.children.active_for_repo(repo) is not None:
                self._skip(repo, "busy")
                return
            self._work(repo, cfg, conf, paths)
        except Exception as error:  # GitHub, the token broker, a bad config: one line, then back off
            if not self._stopping():
                self._fail(repo, f"pass failed error={type(error).__name__}")

    # -- follow-up requests: @mentions on open Factory pull requests --------

    def _followup_repo(self, repo: str) -> None:
        """Record the new, authorised ``@<App slug>`` mentions on this repository's open Factory pull requests,
        then settle the follow-up session in progress or start the oldest queued request (``followup_serve.py``).

        Without a configured App slug or ``mention_handle`` (ambient ``gh``) there is no identity to look
        for, and the pass does nothing and says nothing.
        """
        loaded = self._load(repo)
        if loaded is None:
            return
        cfg = loaded[0]
        conf = followup.settings_of(cfg)
        if not conf.enabled or self._followups.remaining(repo) > 0:
            return
        # The account to mention: ``mention_handle`` when the deployment has no App identity, else the App's slug.
        slug = conf.handle or str(self.manager.env.get("FACTORY_GITHUB_APP_SLUG") or "").strip()
        if not slug:
            return
        who = f"repo={escape_field(repo)}"
        try:
            paths = self._paths(cfg)
            reader = self._reader(cfg)
            if trigger.level_rank(conf.min_permission) < trigger.level_rank("write") and not reader.repo_facts().private:
                self._say(repo, f"followup {who} skipped reason=config", "followup-config", repeat=trigger.NEVER)
                return

            def decided(number: int, outcome: str) -> None:
                self._line(f"followup {who} pr={number} {outcome}")  # one line per decision, never rate-limited

            def failed(number: int, error: Exception) -> None:
                if not self._stopping():
                    self._say(
                        repo, f"followup {who} pr={number} failed error={type(error).__name__}", ("followup", number)
                    )

            followup.run_pass(reader, repo, paths.root, slug, self._clock(), conf, decided, failed)
            self._followups.succeeded(repo)
            self._serving.serve(repo, cfg, conf, paths, reader)
        except Exception as error:  # GitHub, the token broker, a bad config: one line, then back off
            if not self._stopping():
                delay = self._followups.failed(repo)
                self._line(f"followup {who} pass failed error={type(error).__name__} backoff={delay:g}s")

    # -- epic plans: the human gate (epic_gate.py) and the phase issues (epic_phases.py) --------------

    def _epic_repo(self, repo: str) -> None:
        """Move the issues whose epic plan a person has answered or approved to their next gate label, then open the
        phase issues of the approved ones.

        The gate half writes only the status label of an issue that has a gate label (``factory:needs-info`` or
        ``factory:awaiting-approval``): no plan, no run and no comment is started or written there, and the run directory
        is not read. The phase half (``_epic_phases_repo``) opens at most one phase issue per approved epic and keeps its
        status comment; it starts no run either. A repository without epic mode (``epic_mode = false`` or
        ``FACTORY_EPIC_MODE=off``) or without label triggers (``auto_build = false``) gets no GitHub call and no output.
        One fixed line per decision.
        """
        loaded = self._load(repo)
        if loaded is None:
            return
        cfg = loaded[0]
        settings = epic_gate.settings_of(cfg, self.manager.env)
        conf = trigger.settings_of(cfg)
        # A repository that opted out of label-triggered work (``auto_build = false``) is not watched at all.
        if not settings.enabled or not conf.enabled or self._epics.remaining(repo) > 0:
            return
        who = f"repo={escape_field(repo)}"
        try:
            reader = self._reader(cfg)

            def decided(number: int, label: str) -> None:
                self._line(f"epic {who} issue={number} state={label.removeprefix(trigger.STATUS_PREFIX)}")

            def failed(number: int, error: Exception) -> None:
                if self._stopping():
                    return
                if isinstance(error, epic_gate.Truncated):  # a bound was hit: fixed words, no commenter, no text
                    self._say(repo, f"epic {who} issue={number} truncated", ("epic", number))
                else:
                    self._say(repo, f"epic {who} issue={number} failed error={type(error).__name__}", ("epic", number))

            def allowed() -> bool:
                return not self._repo_refused(repo, reader, conf)

            seen = self._epic_seen.setdefault(repo, {})
            epic_gate.run_pass(reader, settings, decided, failed, seen, self._clock(), allowed=allowed)
            # Phase issues are opened only where the plan comment itself can stand (Factory may comment): the status comment is their ledger.
            if epic.can_plan(cfg, self.manager.env) and not self._stopping():
                self._epic_phases_repo(repo, cfg, reader, settings, who, failed, allowed)
            self._epics.succeeded(repo)
        except Exception as error:  # GitHub, the token broker, a bad config: one line, then back off
            if not self._stopping():
                delay = self._epics.failed(repo)
                self._line(f"epic {who} pass failed error={type(error).__name__} backoff={delay:g}s")

    def _epic_phases_repo(
        self, repo: str, cfg: RepoConfig, reader: object, settings: object, who: str, failed: Callable, allowed: Callable
    ) -> None:
        """Open the next phase issue of each approved epic that is due and keep its status comment (``epic_phases.py``)."""
        status = self._epic_status.get(repo)
        if status is None or status.reader is not reader:
            status = self._epic_status[repo] = epic_phases.EpicStatus(
                reader, getattr(cfg, "repo", None), self._clock, self.manager.env
            )
        status.configure(getattr(cfg, "comment_on_blocked", True) is True)
        status.new_cycle()

        def decided(number: int, text: str) -> None:
            self._line(f"epic {who} issue={number} {text}")  # a phase issue was opened: always shown

        def noted(number: int, text: str) -> None:
            if not self._stopping():  # halted or stopped: fixed words, said again only after the limiter's period
                self._say(repo, f"epic {who} issue={number} {text}", ("epic-phase", number))

        epic_phases.run_pass(
            reader, settings, status, self._epic_phases.setdefault(repo, epic_phases.PhaseState()), self._clock(),
            decided, noted, failed, allowed=allowed, trigger_label=getattr(cfg, "auto_build_label", "factory-build"),
            runs=self._epic_runs(repo, self._paths(cfg)),
        )

    # -- label triggers: topping up a run that is already running ---------------------

    def _top_up(
        self, repo: str, cfg: RepoConfig, conf: trigger.TriggerSettings, policy: Policy, paths: DataPaths
    ) -> bool:
        """Plan newly labelled issues into the running run of this repository; False when no supervisor of ours runs.

        Only the run directory and GitHub decide (never memory): with a free build slot (``auto_build_concurrency``
        minus the build workers in ``state.json``), and a run that ``append.check_run`` does not refuse (draining,
        at the job cap, over its budget), up to ``min(free slots, auto_build_max_issues)`` eligible issues go to one
        ``plan --append-to`` child, started through ``Manager.start_append`` (no lease probe: the supervisor holds it).
        An issue already in ``plan.json`` or in a waiting fragment is never planned again; its label is only taken off.
        At most one append per repository exists at any time, so there is at most one start per pass.
        """
        children = self.manager.children
        appending = children.active_for_repo(repo, "append")
        record = self._topups.get(repo)
        if record is not None:
            self._settle_topup(repo, cfg, conf, paths, record)
            if appending is None:
                del self._topups[repo]
                if not self._topup_ended(repo, cfg, paths, record):
                    return True
        if appending is not None:
            self._skip(repo, "busy")
            return True
        supervisor = children.active_for_repo(repo, "run")
        if supervisor is None:
            return False
        run_dir = paths.run_dir(supervisor.run)
        reason, free = self._topup_room(conf, policy, run_dir)
        if reason == "busy":  # the run has no plan or state yet (or they cannot be read): nothing to decide
            self._skip(repo, "busy")
            return True
        waiting = append.waiting_issues(run_dir)
        if reason is not None and not waiting:
            self._skip(repo, reason)
            return True
        reader = self._reader(cfg)
        if self._repo_refused(repo, reader, conf):
            return True
        limit = 0 if reason is not None else min(free, conf.max_issues)
        inside = set(trigger.plan_issue_numbers(trigger.read_plan(run_dir / "plan.json"))) | waiting
        covered: list = []
        eligible, _ = self._candidates(repo, reader, conf, limit=limit, inside=inside, covered=covered, paths=paths)
        if covered:  # a restart between the fragment and the label removal: only the label goes
            self._consume(repo, cfg, conf, covered)
        if reason is not None:
            self._skip(repo, reason)
            return True
        if self._stopping():
            return True
        numbers = trigger.pick(eligible, limit)
        if numbers:
            self._append(repo, conf, paths, supervisor.run, numbers)
        else:
            self._backoff.succeeded(repo)
        return True

    @staticmethod
    def _topup_room(conf: trigger.TriggerSettings, policy: Policy, run_dir: Path) -> tuple:
        """``(skip reason or None, free build slots)`` of a running run, from its directory alone."""
        try:
            _, _, states = append.check_run(run_dir, policy.run_budget_usd)
        except ValueError as error:
            refusals = {append.DRAINING: "draining", append.JOB_CAP: "run_full", append.OVER_BUDGET: "budget"}
            return refusals.get(str(error).removeprefix("append refused: "), "busy"), 0
        free = conf.concurrency - trigger.building_workers(states)
        return ("slots_full" if free <= 0 else None), free

    def _settle_topup(
        self, repo: str, cfg: RepoConfig, conf: trigger.TriggerSettings, paths: DataPaths, record: TopUp
    ) -> None:
        """Take the trigger label off each appended issue whose fragment now exists (in the inbox, or ingested)."""
        run_dir = paths.run_dir(record.run)
        inside = set(trigger.plan_issue_numbers(trigger.read_plan(run_dir / "plan.json"))) | append.waiting_issues(run_dir)
        present = record.labelled & inside
        record.seen = record.seen or bool(inside & set(record.issues))
        if present:
            record.labelled -= present  # a removal that fails is remembered in `_unremoved` and retried
            self._consume(repo, cfg, conf, sorted(present))

    def _topup_ended(self, repo: str, cfg: RepoConfig, paths: DataPaths, record: TopUp) -> bool:
        """Judge a finished append: a non-zero exit, or no fragment for any of its issues, is a failure.

        A failure backs off and is counted like a failed plan (code ``other``: an append leaves no closed code of its own): the
        same set failing twice in a row is held until a label or an edit changes it (``_hold_failed``). A good append clears the entry.
        """
        exits = self.manager.children.last_exit(repo, record.run, limit=1, kind="append")
        code = exits[-1]["returncode"] if exits else None
        if code == 0 and record.seen:
            self._hold_cleared(repo, paths, record.issues)
            self._backoff.succeeded(repo)
            return True
        self._plan_failed(repo, plan_failure.OTHER)
        self._hold_failed(repo, cfg, paths, record.issues, plan_failure.OTHER, record.fingerprint)
        self._fail(repo, f"run={escape_field(record.run)} append failed rc={'unknown' if code is None else code}")
        return False

    def _append(self, repo: str, conf: trigger.TriggerSettings, paths: DataPaths, run: str, numbers: list) -> None:
        """Start ``plan --issues N,... --append-to <run>`` through the manager's own start (no lease probe)."""
        if self._planner_capped(repo, conf, paths):
            return
        try:
            self.manager.start_append(self.manager.target_for(repo, run), numbers, conf.timeout)
        except ApiError as error:
            if error.status in (409, 503):  # an append or shutdown: look again next cycle
                self._skip(repo, "busy")
            else:
                self._fail(repo, f"run={escape_field(run)} append start refused status={error.status}")
            return
        self._topups[repo] = TopUp(run, tuple(numbers), set(numbers), fingerprint=self._issues_stamp(repo, numbers))
        self._started(repo, f"trigger repo={escape_field(repo)} issues={len(numbers)} run={escape_field(run)} appending")

    def _work(self, repo: str, cfg: RepoConfig, conf: trigger.TriggerSettings, paths: DataPaths) -> None:
        """What a repository with no process of ours running needs next; at most one start."""
        planned = self._plans.pop(repo, None)
        if planned is not None:  # a plan this loop started has ended: the run directory says how it went
            if not (paths.run_dir(planned.run) / "plan.json").is_file():
                if planned.stamped:
                    self._clear_run(repo, paths.run_dir(planned.run))  # a failed start leaves neither the stamp nor the marker behind
                exits = self.manager.children.last_exit(repo, planned.run, limit=1)
                code = plan_failure.read(paths.run_dir(planned.run))  # a closed code from the plan, never text
                self._plan_failed(repo, code)
                if not planned.stamped:  # a phase start has its own guard (epic_phases)
                    self._hold_failed(repo, cfg, paths, planned.issues, code, planned.fingerprint)
                rc = exits[-1]["returncode"] if exits else "unknown"
                self._fail(repo, f"run={escape_field(planned.run)} plan failed rc={rc} code={code}")
                return
            self._backoff.succeeded(repo)
            self._hold_cleared(repo, paths, planned.issues)
        launched = self._runs.pop(repo, None)
        if launched is not None and not (paths.run_dir(launched) / "state.json").is_file():
            # A run that ended without ever writing its state failed to start (a lease lost to someone
            # else, a bad plan): its plan stays pending, so the next try waits for the backoff.
            exits = self.manager.children.last_exit(repo, launched, limit=1)
            self._fail(repo, f"run={escape_field(launched)} run failed rc={exits[-1]['returncode'] if exits else 'unknown'}")
            return
        resumed = self._resuming.pop(repo, None)
        if resumed is not None and launched == resumed[0] and self._resume_ended(repo, paths, *resumed):
            return
        owed = self._owed_run(paths)  # a retry whose `run` did not start (busy lease, shutdown, restart)
        pending = None if owed is not None else self._pending_run(paths)  # a plan never run (busy lease, restart)
        if owed is None and pending is None and self.manager.lease_state(cfg, paths, warn=False) != "free":
            self._skip(repo, "busy")
            return
        reader = self._reader(cfg)
        if self._repo_refused(repo, reader, conf):  # every start, finishing old work included, passes this
            return
        if owed is not None:
            self._start_build(repo, conf, self.manager.target_for(repo, owed[0]), owed[1], retry=True)
            return
        if pending is not None:
            self._finish_pending(repo, cfg, conf, pending, reader)
            return
        if self._dependents(repo, cfg, conf, paths, reader):  # a started run whose jobs waited for a merge
            return
        eligible, retries = self._candidates(repo, reader, conf, paths=paths)
        if self._stopping():
            return
        if retries and self._retry(repo, cfg, conf, paths, retries, eligible):
            return
        numbers = self._unheld(repo, paths, conf, eligible)
        if numbers:
            self._plan(repo, conf, paths, numbers)
        elif not self._start_phase(repo, cfg, conf, paths, reader):  # a person's label always goes first
            self._backoff.succeeded(repo)  # a whole pass that read GitHub and found nothing to start: earlier blips are forgotten

    def _holds_of(self, repo: str, paths: DataPaths) -> plan_holds.PlanHolds:
        """The repository's hold table, up to date with its file; one line when the file could not be used."""
        holds = self._holds.get(repo)
        if holds is None:
            holds = self._holds[repo] = plan_holds.PlanHolds(paths.plan_holds)
        if holds.refresh():
            self._line(f"trigger repo={escape_field(repo)} plan holds unusable reason={holds.problem} file=plan-holds.json")
        return holds

    def _issues_stamp(self, repo: str, numbers: Collection) -> str:
        known = self._issue_facts.get(repo, {})
        return plan_holds.fingerprint_of([known.get(number, (number,)) for number in sorted(numbers)])

    def _hold_failed(
        self, repo: str, cfg: RepoConfig, paths: DataPaths, issues: tuple, code: str, fingerprint: str
    ) -> None:
        """Count a failed plan (or append) of an issue set; never raises (a hold that cannot be kept must not stop the loop).

        When the count reaches ``plan_holds.THRESHOLD`` the hold has started and each issue of the set gets its one comment
        (``_hold_comment``).
        """
        try:
            holds = self._holds_of(repo, paths)
            count = holds.failed(issues, code, fingerprint)
            if holds.write_failed:
                self._line(f"trigger repo={escape_field(repo)} plan holds not saved file=plan-holds.json")
        except Exception as error:  # noqa: BLE001 - one line with the type and no text
            self._line(f"trigger repo={escape_field(repo)} plan holds failed error={type(error).__name__}")
            return
        if count >= plan_holds.THRESHOLD:
            self._hold_comment(repo, cfg, paths, issues, code, fingerprint)

    def _hold_comment(self, repo: str, cfg: RepoConfig, paths: DataPaths, issues: tuple, code: str, fingerprint: str) -> None:
        """Say on each issue of a set whose hold has started why nothing happens (``hold_comments.py``); never raises.

        Best effort: a failure is one fixed line (the class of the error) and nothing is retried or stopped. A comment found by
        its marker is not posted again, so a later failing pass or a restart adds none. A comment moves the issue's ``updated_at``,
        which is part of the hold's fingerprint, so after one was written the issues are read once more and the hold takes the
        new fingerprint (``_hold_rebased``): otherwise the hold would end by the loop's own doing and one more plan would run.
        ``FACTORY_ISSUE_COMMENTS_PER_CYCLE=0`` posts nothing (``hold comment skipped reason=cap``); a positive cap is raised to the set's size.
        """
        try:
            writer = self._hold_comments.get(repo)
            reader = self._reader(cfg)
            if writer is None or writer.reader is not reader:
                writer = self._hold_comments[repo] = hold_comments.HoldComments(
                    reader, getattr(cfg, "repo", None), self._clock, self.manager.env
                )
            writer.configure(getattr(cfg, "comment_on_blocked", True) is True)
            writer.new_cycle()
            numbers = sorted(set(issues))
            if writer.cap > 0:  # a positive cap is raised so that one hold comments on its whole set (bounded by the set's size)
                writer.cap = max(writer.cap, len(numbers))  # a cap of 0 stays 0: the operator silenced every comment writer
            known = self._issue_facts.get(repo, {})
            steady = self._issues_stamp(repo, issues) == fingerprint
            written, failed, refused = [], None, None
            for number in numbers:
                if self._stopping():
                    break
                if steady and writer.active:  # a person's edit made since the listing must not be hidden by Factory's own comment
                    steady = self._facts_steady(reader, known.get(number), number)
                result = writer.announce(number, code)
                if result == "written":
                    written.append(number)
                elif result == "failed":
                    failed = writer.last_error or "Error"
                if result in ("off", "cap", "cooldown"):
                    refused = result
                    break  # nothing may be written now: the rest of the set would be refused the same way
            if written and steady:
                self._hold_rebased(repo, reader, paths, issues, fingerprint, written)
            if refused in ("cap", "cooldown"):
                self._line(f"trigger repo={escape_field(repo)} hold comment skipped reason={refused}")
            if failed is not None:
                self._line(f"trigger repo={escape_field(repo)} hold comment failed error={failed}")
        except Exception as error:  # noqa: BLE001 - never stops the loop; one line with the type and no text
            self._line(f"trigger repo={escape_field(repo)} hold comment failed error={type(error).__name__}")

    @staticmethod
    def _facts_steady(reader: object, before: object, number: int) -> bool:
        """Whether the issue still has the labels and ``updated_at`` of the listing (one fresh read); False when it cannot be told."""
        try:
            facts = reader.issue_facts(number)
        except Exception:  # noqa: BLE001 - not knowing counts as a change: the hold keeps its fingerprint
            return False
        return before is not None and facts.updated == before.updated and (
            {n.strip().casefold() for n in facts.labels} == {n.strip().casefold() for n in before.labels}
        )

    def _hold_rebased(
        self, repo: str, reader: object, paths: DataPaths, issues: tuple, fingerprint: str, written: list
    ) -> None:
        """Move a hold's fingerprint over the ``updated_at`` that Factory's own comments gave the issues.

        Each issue that got a comment is read once; one whose labels are no longer what the listing saw (a person changed
        them) leaves the hold alone, so that change still releases it.
        """
        known = self._issue_facts.setdefault(repo, {})
        fresh = {}
        for number in written:
            facts = reader.issue_facts(number)
            before = known.get(number)
            if before is None or {n.strip().casefold() for n in facts.labels} != {n.strip().casefold() for n in before.labels}:
                return
            fresh[number] = facts
        known.update(fresh)
        self._holds_of(repo, paths).rebase(issues, fingerprint, self._issues_stamp(repo, issues))

    def _hold_cleared(self, repo: str, paths: DataPaths, issues: tuple) -> None:
        """A plan of an issue set ended well: its entry goes."""
        try:
            self._holds_of(repo, paths).succeeded(issues)
        except Exception as error:  # noqa: BLE001
            self._line(f"trigger repo={escape_field(repo)} plan holds failed error={type(error).__name__}")

    def _unheld(self, repo: str, paths: DataPaths, conf: trigger.TriggerSettings, eligible: list) -> list:
        """The issues to plan now: the first set ``pick`` makes that is not held, or ``[]``.

        A set whose plan failed with the same code twice (and whose labels and updated_at are unchanged) is not planned: one
        line says so, once, and the next set of the pool is looked at.
        """
        pool = sorted(set(eligible))
        if not pool:
            return []
        try:
            holds = self._holds_of(repo, paths)
        except Exception as error:  # noqa: BLE001
            self._line(f"trigger repo={escape_field(repo)} plan holds failed error={type(error).__name__}")
            return trigger.pick(pool, conf.max_issues)
        while pool:
            numbers = trigger.pick(pool, conf.max_issues)
            if not self._group_held(repo, holds, numbers):
                return numbers
            pool = [number for number in pool if number not in numbers]
        return []

    def _group_held(self, repo: str, holds: plan_holds.PlanHolds, numbers: list) -> bool:
        """Whether this issue set is held now; the first time it is seen held, one line says so."""
        print_ = self._issues_stamp(repo, numbers)
        held = holds.held(numbers, print_)
        if held is None:
            return False
        key = (repo, tuple(numbers), print_)
        if key not in self._hold_lines:
            if len(self._hold_lines) >= 1024:
                self._hold_lines.clear()
            self._hold_lines.add(key)
            code, count = held
            self._line(f"trigger repo={escape_field(repo)} issues={plan_holds.key_of(numbers)} held code={code} failures={count}")
        return True

    def _repo_refused(self, repo: str, reader: object, conf: trigger.TriggerSettings) -> bool:
        """Whether the repository itself rules out an automatic start (archived, or a permission minimum
        below ``write`` on a repository that is not private); one line says which."""
        facts = reader.repo_facts()
        if facts.archived:
            self._skip(repo, "archived")
            return True
        if not trigger.minimum_allowed(conf.min_permission, facts.private):
            self._skip(repo, "config")
            return True
        return False

    @staticmethod
    def _pending_run(paths: DataPaths) -> str | None:
        """The oldest run this loop planned (``auto-*`` with its marker) that has a plan and was never run."""
        for entry in _list_runs(paths):
            if not (entry["run"].startswith(trigger.RUN_PREFIX) and entry["planned"] and not entry["started"]):
                continue
            if read_marker(paths.run_dir(entry["run"]) / TRIGGER_MARKER)[0]:
                return entry["run"]
        return None

    @staticmethod
    def _owed_run(paths: DataPaths) -> tuple | None:
        """(run, issue count) of the oldest started run whose retried jobs are still waiting for ``run``."""
        for entry in _list_runs(paths):
            if entry["started"]:
                count = read_owed(paths.run_dir(entry["run"]) / OWED_MARKER)
                if count is not None:
                    return entry["run"], count
        return None

    def _finish_pending(self, repo: str, cfg: RepoConfig, conf: trigger.TriggerSettings, run: str, reader: object) -> None:
        """Consume the trigger labels of a planned run (until it worked), then start its ``run``.

        A run the manager planned for an epic phase (it holds a stamp) starts only if the approval still stands (``_epic_build_proven``).
        """
        target = self.manager.target_for(repo, run)
        if not self._epic_build_proven(repo, cfg, reader, target):
            return
        document = trigger.read_plan(target.run_dir / "plan.json")
        numbers = trigger.plan_issue_numbers(document) if document is not None else []
        if (repo, run) not in self._consumed and self._consume(repo, cfg, conf, numbers):
            self._consumed.add((repo, run))
        self._start_build(repo, conf, target, len(numbers), retry=False)

    def _epic_verdict(self, cfg: RepoConfig, target: Target, reader_of: Callable) -> tuple:
        """``(stamp, verdict, error type name)``: the ONE place a run directory's epic stamp is proven again (``epic_phases.may_build``).

        Asked by every start of a ``run`` the manager makes by itself for a stamped run directory (the build of a planned phase, the restart's auto-resume,
        a dependency resume), so none of them can be looser than another. ``reader_of`` makes the GitHub reader only when a read is needed. The verdicts:
        ``VERDICT_NONE`` (no stamp: a person's run, nothing to prove), ``VERDICT_PROVEN``, ``VERDICT_STANDING`` (a definite no: a kill switch, a withdrawn approval,
        a phase issue that changed), ``VERDICT_CUT_OFF`` (a read cut off at a bound, or answered with 404 or 410: asking again changes nothing),
        ``VERDICT_UNREADABLE`` (a failure that can pass: a timeout, a rate limit, a 5xx, no reader) and ``VERDICT_STAMP`` (a stamp that is no stamp).
        """
        try:
            stamp = epic_phases.run_stamp(target.run_dir)
        except ValueError:
            return None, VERDICT_STAMP, None
        if stamp is None:
            return None, VERDICT_NONE, None
        try:
            standing = epic.can_plan(cfg, self.manager.env) and epic_phases.may_build(
                reader_of(), epic_gate.settings_of(cfg, self.manager.env), stamp
            )
        except Exception as error:  # noqa: BLE001 - nothing can be proven: the caller holds or refuses, and says the type only
            cut = isinstance(error, (epic_gate.Truncated, epic.PlanSearchTruncated)) or trigger.is_gone(error)
            return stamp, (VERDICT_CUT_OFF if cut else VERDICT_UNREADABLE), type(error).__name__
        return stamp, (VERDICT_PROVEN if standing else VERDICT_STANDING), None

    def _epic_build_proven(self, repo: str, cfg: RepoConfig, reader: object, target: Target) -> bool:
        """Whether the ``run`` of a planned run may start. A run without an epic stamp is a person's request and always may.

        With a stamp (the manager planned the phase itself) the approval is proven again from GitHub right now (``_epic_verdict``),
        however long the plan waited (the planner, a pause, a backoff, a restart): the kill switches and a withdrawn approval stop the
        paid part as well as the plan. A read that fails for a reason that can pass (a timeout, a rate limit, a 5xx) holds the build for the
        repository's backoff, but only for ``EPIC_BUILD_HOLD_SECONDS`` since the first such pass (recorded in the run directory, so that a restart
        does not start the count again): a plan is served before every other start, so a hold without an end would keep the whole repository
        waiting. Anything else that is not so refuses it for good: a withdrawn approval, a stamp that is no stamp, a read that stopped at a bound
        (``Truncated``: nothing more will be read by asking again) or that GitHub answers with 404 or 410 (the parent or the phase issue is gone),
        and a hold that lasted too long. A refusal takes the loop's marker off, so the plan is never picked up again and the repository is not kept
        waiting behind it; the stamp stays and the run counts as dead (a stalled phase, once the epic is read again). One fixed line either way.
        """
        key = (repo, target.run)
        stamp, verdict, error = self._epic_verdict(cfg, target, lambda: reader)
        if verdict == VERDICT_NONE:
            return True
        if verdict == VERDICT_PROVEN:
            self._release_hold(key, target.run_dir)
            return True
        if verdict == VERDICT_UNREADABLE and not self._hold_over(key, target.run_dir):
            self._fail(repo, f"run={escape_field(target.run)} epic build not proven error={error}")
            return False
        self._release_hold(key, target.run_dir)
        with contextlib.suppress(OSError):
            (target.run_dir / TRIGGER_MARKER).unlink(missing_ok=True)
        if stamp is None:
            self._line(f"trigger repo={escape_field(repo)} run={escape_field(target.run)} build refused reason=stamp")
        else:
            why = EPIC_REFUSED_STANDING if verdict == VERDICT_STANDING else EPIC_REFUSED_UNPROVEN
            self._line(f"epic repo={escape_field(repo)} issue={stamp['parent']} phase={stamp['phase']} child={stamp['child']} build refused: {why}")
        return False

    def _hold_over(self, key: tuple, run_dir: Path) -> bool:
        """Whether a build that cannot be proven has been held for ``EPIC_BUILD_HOLD_SECONDS``, counted from the first pass that could not prove it.

        That moment is the earliest of the run directory's record and this process's own count, else now (and it is recorded). A record that is
        not one plain number, or that lies in the future (a clock that went back), cannot be believed and counts as now, and is written again.
        A pause or a stopped breaker between two such passes counts as hold time (it is wall time): a plan held across a long pause is refused
        by its next failed read, and that is the safe side.
        """
        now = self._clock()
        recorded = read_held(run_dir / EPIC_HELD_FILE)
        since = min(now, now if recorded is None else recorded, self._build_held.get(key, now))  # a record from the future is clamped to now
        self._build_held[key] = since
        if recorded != since:  # no record, one that is no number or lies in the future, or a later one than this process's count
            with contextlib.suppress(OSError):  # not recorded: this process's count still bounds the hold
                write_marker(run_dir / EPIC_HELD_FILE, str(int(since)))
        return now - since >= EPIC_BUILD_HOLD_SECONDS

    def _release_hold(self, key: tuple, run_dir: Path) -> None:
        """Forget a hold: the build was proven, or refused for good."""
        self._build_held.pop(key, None)
        with contextlib.suppress(OSError):
            (run_dir / EPIC_HELD_FILE).unlink(missing_ok=True)

    def _epic_resume_proven(self, repo: str, cfg: RepoConfig, target: Target, reader_of: Callable, say: Callable) -> bool:
        """Whether the manager may start ``run`` again for a started run directory by itself (the restart's auto-resume, a dependency resume).

        A run without a stamp is a person's own request and always may. A run the manager started for an epic phase is proven again from GitHub
        right now by the same verdict as the build of a planned phase (``_epic_verdict``): epic mode and the comment right, the parent's labels
        and ``factory-ignore``, the approval's standing, the phase issue. A run that cannot be proven (a definite no, or any read that fails or
        is cut off) is NOT started: it is left as it is (interrupted, or waiting) for a person, nothing is held, counted or marked, and a later
        restart or pass proves it again. ``say`` writes the one fixed line.
        """
        stamp, verdict, _ = self._epic_verdict(cfg, target, reader_of)
        if verdict in (VERDICT_NONE, VERDICT_PROVEN):
            return True
        if stamp is None:
            say(f"trigger repo={escape_field(repo)} run={escape_field(target.run)} resume refused reason=stamp")
        else:
            why = EPIC_REFUSED_STANDING if verdict == VERDICT_STANDING else EPIC_REFUSED_UNPROVEN
            say(f"epic repo={escape_field(repo)} issue={stamp['parent']} phase={stamp['phase']} child={stamp['child']} resume refused: {why}")
        return False

    def _consume(self, repo: str, cfg: RepoConfig, conf: trigger.TriggerSettings, numbers: list) -> bool:
        """Take the trigger label off each of these issues, best effort; True when every label is gone.

        A failure is one line and never stops the build. A label that stays is not harmless (the
        status labels are best effort too, and may have failed for the same reason), so the issue is
        remembered in ``_unremoved``: it is not started again unless the label is applied anew, and
        every cycle tries the removal again.
        """
        failed: list = []
        error: Exception | None = None
        reader = None
        try:
            reader = self._reader(cfg)
        except Exception as caught:
            failed, error = list(numbers), caught
        else:
            for number in numbers:
                try:
                    reader.remove_label(number, conf.label)
                except Exception as caught:
                    failed.append(number)
                    error = error or caught
        if not failed:
            return True
        self._line(f"trigger repo={escape_field(repo)} label-remove failed error={type(error).__name__}")
        for number in failed:
            self._remember_unremoved(repo, reader, conf, number)
        return False

    def _remember_unremoved(self, repo: str, reader: object, conf: trigger.TriggerSettings, number: int) -> None:
        key = (repo, number)
        if key in self._unremoved:
            return
        stamp = None  # unknown: the first later sighting records it
        if reader is not None:
            try:
                actor = reader.label_actor(number, conf.label)
                stamp = actor.event if actor is not None else ""
            except Exception:
                pass
        self._unremoved[key] = stamp

    def _held(self, repo: str, reader: object, conf: trigger.TriggerSettings, number: int) -> bool:
        """Whether an issue whose trigger label stayed after its build must wait (and try to remove the label).

        It waits until the label is gone or has been applied anew (a different ``labeled`` event),
        which is a fresh request that goes through the authorisation like any other.
        """
        key = (repo, number)
        if key not in self._unremoved:
            return False
        actor = reader.label_actor(number, conf.label)
        current = actor.event if actor is not None else ""
        known = self._unremoved[key]
        if known is not None and current != known:
            del self._unremoved[key]
            return False
        self._unremoved[key] = current
        try:
            reader.remove_label(number, conf.label)
        except Exception:
            pass
        else:
            del self._unremoved[key]
        self._skip(repo, "unremoved", number)
        return True

    def _forget_unremoved(self, repo: str, listed: set) -> None:
        """Drop the memory of issues that no longer carry the label (or are closed)."""
        for key in [key for key in self._unremoved if key[0] == repo and key[1] not in listed]:
            del self._unremoved[key]

    def _start_build(
        self, repo: str, conf: trigger.TriggerSettings, target: Target, count: int, *, retry: bool
    ) -> bool:
        """``run`` for a planned run through the manager's own start; True when it started."""
        try:
            self.manager.start_run(target, {"concurrency": conf.concurrency, "timeout": conf.timeout})
        except ApiError as error:
            if error.status in (409, 503):  # a process or the lease is busy, or shutdown: look again next cycle
                self._skip(repo, "busy")
            else:
                self._fail(repo, f"run={escape_field(target.run)} start refused status={error.status}")
            return False
        if retry:
            with contextlib.suppress(OSError):
                (target.run_dir / OWED_MARKER).unlink()
        self._runs[repo] = target.run
        self._backoff.succeeded(repo)
        who = f"repo={escape_field(repo)} issues={count} run={escape_field(target.run)}"
        self._started(repo, f"trigger {who} started" + (" retry=yes" if retry else ""))
        return True

    def _dependents(
        self, repo: str, cfg: RepoConfig, conf: trigger.TriggerSettings, paths: DataPaths, reader: object
    ) -> bool:
        """Resume started runs whose queued jobs only wait for dependency merges; True when the pass is used up.

        Everything is derived from the run directories and GitHub, never from memory. For each started
        run, each queued job with an unmerged ``depends_on`` is judged by the pull requests of those
        dependencies: all merged -> the run is ready; one closed without merging -> the job can never
        start and is marked blocked (``block``, which also labels its issue); otherwise it keeps waiting.
        At most one ``run`` is started per pass (through ``Manager.start_run``: registry, lease probe), and
        at most ``MAX_DEPENDENCY_LOOKUPS`` pull requests are read.
        """
        lookups = [MAX_DEPENDENCY_LOOKUPS]
        for entry in _list_runs(paths):
            if not entry["started"] or self._stopping():
                continue
            run, run_dir = entry["run"], paths.run_dir(entry["run"])
            state = trigger.read_plan(run_dir / "state.json")
            plan = trigger.read_plan(run_dir / "plan.json")
            # (job, what it waits for, whether that is a shared-path wait); a shared-path wait is
            # released by the sibling merging, being closed or holding nothing, never blocks the job.
            waiting = [(job, names, False) for job, names in trigger.waiting_on_dependencies(plan, state)]
            waiting += [(job, names, True) for job, names in trigger.waiting_on_shared_paths(plan, state)]
            if not waiting:
                continue
            resumed = read_resumed(run_dir / DEPENDENCY_MARKER)
            if resumed is None:
                continue
            seen: dict = {}
            ready: list = []
            pending = False
            now = self._clock()
            for job, dependencies, shared in waiting:
                # A shared-path wait moves on to the next sibling after a resume, so each sibling
                # is answered once on its own.
                marker = f"{job}@{','.join(dependencies)}" if shared else job
                if marker in resumed and not resume_retry_due(resumed[marker], now):
                    self._say(
                        repo,
                        f"trigger repo={escape_field(repo)} run={escape_field(run)} job={escape_field(job)} skipped reason=resumed",
                        ("resumed", run, job),
                    )
                    continue
                if shared:
                    verdicts = [
                        "merged"
                        if trigger.shared_path_released(state, dependency)
                        else self._judge(repo, reader, state, dependency, seen, lookups)
                        for dependency in dependencies
                    ]
                    if all(verdict in ("merged", "closed") for verdict in verdicts):
                        ready.append(marker)
                    else:
                        pending = True
                    continue
                verdicts = [self._judge(repo, reader, state, dependency, seen, lookups) for dependency in dependencies]
                closed = [d for d, verdict in zip(dependencies, verdicts) if verdict == "closed"]
                if closed:
                    self._block(repo, run, run_dir, job, closed[0])
                elif all(verdict == "merged" for verdict in verdicts):
                    ready.append(marker)
                else:
                    pending = True
            if ready:
                if self._over_budget_for_resume(repo, cfg, run, run_dir):
                    continue
                target = self.manager.target_for(repo, run)
                refusal = partial(self._say, repo, key=("epic-resume", run))  # the same fixed line as the restart's, once in a while
                if not self._epic_resume_proven(repo, cfg, target, lambda: reader, refusal):
                    continue  # the run waits as it is; the next pass proves it again
                self._resume(repo, conf, target, run_dir, ready, resumed)
                return True
            if pending:
                self._say(repo, f"trigger repo={escape_field(repo)} run={escape_field(run)} skipped reason=dependency", ("dependency", run))
        return False

    @staticmethod
    def _judge(repo: str, reader: object, state: object, dependency: str, seen: dict, lookups: list) -> str:
        """``merged``, ``closed``, ``open`` or ``waiting`` (no pull request yet, or none read this pass) for a dependency."""
        if dependency in seen:
            return seen[dependency]
        if trigger.dependency_status(state, dependency) == "closed":
            verdict = "closed"
        else:
            number = trigger.pull_number(state, dependency, repo)
            if number is None:
                verdict = "waiting"
            elif lookups[0] <= 0:
                verdict = "waiting"
            else:
                lookups[0] -= 1
                verdict = reader.pull_state(number)
        seen[dependency] = verdict
        return verdict

    def _block(self, repo: str, run: str, run_dir: Path, job: str, dependency: str) -> None:
        """Mark a queued job blocked because its dependency's pull request was closed unmerged.

        The CLI's ``block`` does it under the repository lease and puts ``factory:blocked`` on the
        job's issue (best effort). The job is then no longer queued, so no pass looks at it again; a
        refusal (a busy lease) pauses only this job, with the usual exponential backoff.
        """
        who = f"repo={escape_field(repo)} run={escape_field(run)} job={escape_field(job)}"
        key = (repo, run, job)
        if self._blocking.remaining(key) > 0:
            self._say(repo, f"trigger {who} skipped reason=backoff", ("block", run, job))
            return
        reason = CLOSED_DEPENDENCY_REASON.format(dependency)
        result = self._cli(repo, run_dir, ["block", job, f"--reason={reason}"])
        if result is None:
            return
        if succeeded(result):
            self._blocking.succeeded(key)
            self._line(f"trigger {who} blocked dependency={escape_field(dependency)} reason={trigger.REASON_DEPENDENCY_CLOSED}")
            return
        delay = self._blocking.failed(key)
        self._line(f"trigger {who} block failed rc={_rc(result)} backoff={delay:g}s")

    def _over_budget_for_resume(self, repo: str, cfg: RepoConfig, run: str, run_dir: Path) -> bool:
        """The restart auto-resume's budget rule: a run that already spent its budget is not started again."""
        policy = self._policy(cfg)
        if policy.run_budget_usd is None:
            return False
        report = self._status(repo, run_dir) or {}
        if not self._over_budget(run_dir, policy, report):
            return False
        self._say(repo, f"trigger repo={escape_field(repo)} run={escape_field(run)} skipped reason=over-budget", ("budget", run))
        return True

    def _resume(
        self, repo: str, conf: trigger.TriggerSettings, target: Target, run_dir: Path, ready: list, before: dict
    ) -> None:
        """``run`` for a started run through the manager's own start (same lease probe and child registry).

        The marker naming the jobs (with their number of starts and the time) goes first, so that a crash
        cannot lead to a second start for them; a start that was refused (busy, shutdown, failure) takes
        it back, and the next pass looks again. A run that was started and then fails is dealt with by
        ``_resume_ended``.
        """
        marker = run_dir / DEPENDENCY_MARKER
        now = self._clock()
        after = dict(before)
        for job in ready:
            after[job] = (before.get(job, (0, 0.0))[0] + 1, now)
        with contextlib.suppress(OSError):
            write_resumed(marker, after)
        try:
            self.manager.start_run(target, {"concurrency": conf.concurrency, "timeout": conf.timeout})
        except ApiError as error:
            with contextlib.suppress(OSError):
                write_resumed(marker, before)
            if error.status in (409, 503):  # a process or the lease is busy, or shutdown: look again next cycle
                self._skip(repo, "busy")
            else:
                self._fail(repo, f"run={escape_field(target.run)} start refused status={error.status}")
            return
        self._runs[repo] = target.run
        self._resuming[repo] = (target.run, list(ready))
        # No success is recorded for the backoff yet: the run may still fail (see ``_resume_ended``).
        self._started(
            repo, f"trigger repo={escape_field(repo)} run={escape_field(target.run)} jobs={len(ready)} started resume=dependencies"
        )

    def _resume_ended(self, repo: str, paths: DataPaths, run: str, jobs: list) -> bool:
        """What became of a dependency resume this loop started; True when the pass is used up.

        A non-zero exit (the lease lost between the probe and the CLI's own lock, a failed fetch, GitHub
        unreachable inside ``run``) takes the jobs back out of the marker and backs the repository
        off, so the same decision is taken again after the pause. A clean exit that left a job queued
        keeps the marker with the starts used up, so that it is not started over and over. The
        exit code is memory only: after a manager restart the marker's own bounded retry applies.
        """
        marker = paths.run_dir(run) / DEPENDENCY_MARKER
        exits = self.manager.children.last_exit(repo, run, limit=1)
        code = exits[-1]["returncode"] if exits else None
        current = read_resumed(marker)
        if code is None or current is None:
            return False
        if code != 0:
            with contextlib.suppress(OSError):
                write_resumed(marker, {job: entry for job, entry in current.items() if job not in jobs})
            self._fail(repo, f"run={escape_field(run)} run failed rc={code}")
            return True
        spent = {job: (max(MAX_RESUME_ATTEMPTS, entry[0]) if job in jobs else entry[0], entry[1]) for job, entry in current.items()}
        with contextlib.suppress(OSError):
            write_resumed(marker, spent)
        self._backoff.succeeded(repo)
        return False

    def _candidates(
        self,
        repo: str,
        reader: object,
        conf: trigger.TriggerSettings,
        *,
        limit: int | None = None,
        inside: Collection = (),
        covered: list | None = None,
        paths: DataPaths | None = None,
    ) -> tuple:
        """(eligible numbers, retry numbers) among the open issues carrying the trigger label, authorised ones only.

        ``limit`` (default ``auto_build_max_issues``) is how many of each kind are accepted. A top-up passes
        ``covered`` (a list): retries are then not collected, and an eligible issue whose number is in ``inside``
        (already in the run or in a waiting fragment) is not planned again but added to ``covered``.

        The issues are looked at lowest number first and the (costly) authorisation of an issue
        happens only while fewer than ``auto_build_max_issues`` of its kind are accepted. An eligible issue that belongs to a held
        plan set (``plan_holds``, its fingerprint unchanged) is skipped before its authorisation: it costs no GitHub call and
        takes no place in the cap, so an idle cycle costs a bounded number of GitHub calls however many issues carry the label,
        held or not. A top-up (``covered``) skips the members of a held set the same way, so a held set is never appended.
        """
        listed = sorted(reader.open_issues(conf.label), key=lambda facts: facts.number)
        self._issue_facts[repo] = {facts.number: facts for facts in listed}
        self._forget_unremoved(repo, {facts.number for facts in listed})
        eligible, retries = [], []
        table = self._holds_for_candidates(repo, paths)
        holds = table if covered is None else None  # the set check at the end is for new plans; a top-up only skips held members
        members = table.held_sets(lambda numbers: self._issues_stamp(repo, numbers)) if table is not None else {}
        cap = conf.max_issues if limit is None else limit
        for facts in listed:
            if self._stopping():
                break
            verdict = trigger.classify(facts, conf.label)
            if verdict in ("ignored", "in-flight"):
                self._skip(repo, verdict, facts.number)
                continue
            if covered is not None:
                if verdict == "retry":
                    continue  # a retry needs the run's lease: it waits until the repository is idle
                if verdict == "eligible" and facts.number in inside:
                    covered.append(facts.number)
                    continue
            if verdict == "retry" and self._epic_in_flight(repo, reader, conf, facts, paths):
                self._skip(repo, "in-flight", facts.number)  # a new plan for an approved epic waits until no phase is in flight
                continue
            wanted = retries if verdict == "retry" else eligible
            if verdict is None or len(wanted) >= (conf.max_issues if limit is None else limit):
                continue  # enough of this kind for the cycle: the rest wait
            if self._refused.remaining((repo, facts.number)) > 0:
                self._skip(repo, "backoff", facts.number)
            elif self._held(repo, reader, conf, facts.number):
                continue
            elif verdict == "eligible" and facts.number in members:
                for numbers in members[facts.number]:
                    self._group_held(repo, table, numbers)  # no read at all; one line, once per set
            elif self._authorised(repo, reader, conf, facts):
                wanted.append(facts.number)
                if wanted is eligible and holds is not None and len(eligible) >= cap and self._group_held(repo, holds, eligible):
                    eligible.clear()  # a held set takes no place in the cycle: the issues after it are looked at
        if holds is not None and eligible and self._group_held(repo, holds, eligible):
            eligible.clear()
        return eligible, retries

    def _holds_for_candidates(self, repo: str, paths: DataPaths | None) -> plan_holds.PlanHolds | None:
        """The hold table for ``_candidates`` (None without a data directory, or when it cannot be had)."""
        if paths is None:
            return None
        try:
            return self._holds_of(repo, paths)
        except Exception as error:  # noqa: BLE001
            self._line(f"trigger repo={escape_field(repo)} plan holds failed error={type(error).__name__}")
            return None

    def _epic_in_flight(
        self, repo: str, reader: object, conf: trigger.TriggerSettings, facts: trigger.IssueFacts, paths: DataPaths | None
    ) -> bool:
        """Whether the trigger label sits on an approved epic that has a phase in flight (``epic_phases.in_flight``; fails closed)."""
        if issue_labels.LABEL_PLAN_APPROVED not in {name.strip().casefold() for name in facts.labels}:
            return False
        runs = self._epic_runs(repo, paths) if paths is not None else None
        settings = epic_gate.EpicSettings(True, conf.min_permission)
        return epic_phases.in_flight(reader, settings, facts.number, self._clock(), runs)

    def _authorised(
        self, repo: str, reader: object, conf: trigger.TriggerSettings, facts: trigger.IssueFacts
    ) -> bool:
        """Whether the user who applied the label holds the minimum permission.

        A refusal is remembered until the issue is updated again (any label change updates it),
        so an unauthorised label costs two GitHub calls once, not every cycle.
        """
        key = (repo, facts.number)
        if facts.updated and self._denied.get(key) == facts.updated:
            return False
        actor = reader.label_actor(facts.number, conf.label)
        allowed = (
            actor is not None
            and actor.is_user
            and trigger.permission_ok(reader.permission(actor.login), conf.min_permission)
        )
        if allowed:
            self._denied.pop(key, None)
            return True
        if len(self._denied) >= 4096:
            self._denied.clear()
        self._denied[key] = facts.updated
        self._skip(repo, "unauthorised", facts.number)
        return False

    def _clear_run(self, repo: str, run_dir: Path) -> None:
        """Take the trigger-run marker and the epic stamp out of the run directory of a start that did not happen, then the directory.

        Each step stands alone: a marker that cannot be removed does not keep the stamp in place, and a directory that cannot be removed
        (the planner left files in it) does not matter once the marker and the stamp are gone, but it is said (it used to be silent).
        """
        for name in (epic.STAMP_FILE, TRIGGER_MARKER):
            with contextlib.suppress(OSError):
                (run_dir / name).unlink(missing_ok=True)
        try:
            run_dir.rmdir()
        except OSError:
            if run_dir.exists():
                self._say(repo, f"trigger repo={escape_field(repo)} run={escape_field(run_dir.name)} cleanup incomplete", ("cleanup", run_dir.name))

    def _plan(
        self, repo: str, conf: trigger.TriggerSettings, paths: DataPaths, numbers: list, start: epic_phases.Start | None = None
    ) -> None:
        """Start ``plan`` for exactly these issues through the manager's own start.

        The run directory gets the loop's marker first: only a run so marked is finished by the loop
        after a restart, and a plan without the marker would be planned again and again. A phase issue the manager starts itself
        (``start``) gets its stamp beside it; the two are written and removed together on every failure path (``_clear_run``).
        """
        if self._planner_capped(repo, conf, paths):
            return
        run = trigger.run_name(self._clock(), lambda name: paths.run_dir(name).exists())
        target = self.manager.target_for(repo, run)
        target.run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            write_marker(target.run_dir / TRIGGER_MARKER, timestamp())
            if start is not None:
                epic_phases.write_stamp(target.run_dir, start)
        except OSError:
            self._clear_run(repo, target.run_dir)
            self._fail(repo, f"run={escape_field(run)} plan start refused status=marker")
            return
        try:
            self.manager.start_plan(target, {"issues": numbers, "timeout": conf.timeout})
        except ApiError as error:
            self._clear_run(repo, target.run_dir)  # nothing was started: leave no half-made run behind
            if error.status in (409, 503):
                self._skip(repo, "busy")
            else:
                self._fail(repo, f"run={escape_field(run)} plan start refused status={error.status}")
            return
        self._plans[repo] = PlannedRun(run, tuple(numbers), start is not None, self._issues_stamp(repo, numbers))
        self._started(repo, f"trigger repo={escape_field(repo)} issues={len(numbers)} run={escape_field(run)} planning")

    def _epic_runs(self, repo: str, paths: DataPaths) -> Callable:
        """``child -> epic_phases.RunFacts`` from this repository's run directories: the newest run whose plan lists the issue decides.

        A run directory that carries a stamp for the issue but no plan yet is an attempt in flight while a process of ours works in the
        repository, and a leftover of a failed attempt otherwise (it counts for nothing: the attempt is in the ledger).
        """
        entries, cut = _scan_runs(paths, newest_first=True)  # the newest decide, so the oldest are the ones left out of a long listing
        busy = repo in self._plans or self.manager.children.active_for_repo(repo) is not None
        if cut:
            self._say(repo, f"epic repo={escape_field(repo)} run directories cut off at the listing bound: no phase is started or stalled on that evidence", ("epic-runs", repo))

        def facts(child: int) -> epic_phases.RunFacts:
            for entry in entries:
                run_dir = paths.run_dir(entry["run"])
                document = trigger.read_plan(run_dir / "plan.json") if entry["planned"] or entry["started"] else None
                if document is None:
                    stamp = self._stamp_of(run_dir)
                    if stamp is not None and stamp["child"] == child:
                        return epic_phases.RunFacts(live=busy)
                elif child in trigger.plan_issue_numbers(document):
                    if not entry["started"] and self._refused_plan(run_dir, child):
                        return epic_phases.RunFacts(found=True, dead=True)  # planned by the manager, never to be built
                    return self._run_facts(run_dir, document, child)
            # Nothing listed names the issue, but older directories were left out: "no run" is not proven, so a run may be going (fails closed).
            return epic_phases.RunFacts(live=cut)

        return facts

    @staticmethod
    def _stamp_of(run_dir: Path) -> dict | None:
        try:
            return epic_phases.run_stamp(run_dir)
        except ValueError:
            return None

    @classmethod
    def _refused_plan(cls, run_dir: Path, child: int) -> bool:
        """Whether a plan the manager made for this phase issue was refused its build (``_epic_build_proven``): the stamp is there, the loop's marker is gone."""
        stamp = cls._stamp_of(run_dir)
        return stamp is not None and stamp["child"] == child and not read_marker(run_dir / TRIGGER_MARKER)[0]

    @staticmethod
    def _run_facts(run_dir: Path, document: dict, child: int) -> epic_phases.RunFacts:
        """What one run's plan and state say about a phase issue: no job for it, or its job's status, decides ``live`` and ``dead``."""
        job = trigger.job_owning(document, child)
        if job is None:  # a disposition only: the planner built nothing for it
            dead = trigger.has_disposition(document, child, (*NOT_BUILT, "covered"))
            return epic_phases.RunFacts(found=True, live=not dead, dead=dead)
        status = trigger.dependency_status(trigger.read_plan(run_dir / "state.json"), job)
        drained = read_marker(run_dir / BUDGET_MARKER)[0]
        # A drain (the cost guardrail) lets the active phases finish and starts nothing new: a job that is running still can publish.
        dead = status in EPIC_DEAD_JOB_STATUSES or (drained and (status is None or status in EPIC_DRAINED_DEAD_STATUSES))
        return epic_phases.RunFacts(found=True, live=not dead, dead=dead)

    def _start_phase(self, repo: str, cfg: RepoConfig, conf: trigger.TriggerSettings, paths: DataPaths, reader: object) -> bool:
        """Begin the build of an approved epic's open phase issue by itself (``epic_phases.prepare_start``); True when a plan was started.

        Called from ``_work``, so every precondition of a plan holds (no process of ours, the lease free, nothing pending, the repository
        not refused) and a person's labelled issue was started first. Only a phase the epic pass found startable is looked at
        (``PhaseState.ready``), and the start proves everything again from GitHub. Epic mode off, ``auto_build = false``, Factory unable to
        comment, a failing epic pass or a paused trigger start nothing. At most one start per repository per pass.
        """
        settings = epic_gate.settings_of(cfg, self.manager.env)
        state, status = self._epic_phases.get(repo), self._epic_status.get(repo)
        if state is None or status is None or not state.ready or not settings.enabled or not epic.can_plan(cfg, self.manager.env):
            return False
        if self._epics.remaining(repo) > 0 or self._stopping():
            return False
        status.configure(getattr(cfg, "comment_on_blocked", True) is True)
        if not status.active:
            return False
        if self._planner_capped(repo, conf, paths):  # before the start is prepared: a held phase leaves no attempt behind
            return True
        who = f"repo={escape_field(repo)}"
        now = self._clock()
        runs = self._epic_runs(repo, paths)
        for parent in sorted(state.ready):
            if now - state.ready[parent] > epic_phases.READY_SECONDS:
                del state.ready[parent]  # not seen startable for a while: the pass has to find it again
                continue
            try:
                status.new_cycle()
                start, held = epic_phases.prepare_start(reader, settings, status, state, now, parent, runs, reader.open_pulls_of, conf.label)
            except Exception as error:  # noqa: BLE001 - one parent never stops the others; one line with the type and no text
                self._say(repo, f"epic {who} issue={parent} start failed error={type(error).__name__}", ("epic-start", parent))
                continue
            if held:
                self._skip(repo, "epic_cap", parent)
            elif start is not None:
                del state.ready[parent]
                self._line(f"epic {who} issue={parent} phase={start.phase} child={start.child} start")
                self._plan(repo, conf, paths, [start.child], start)
                return True
        return False

    def _retry(
        self, repo: str, cfg: RepoConfig, conf: trigger.TriggerSettings, paths: DataPaths, retries: list, eligible: list
    ) -> bool:
        """Another attempt for blocked or CI-failed issues whose run still exists; True when a start used this pass.

        The manager's own ``retry`` (the HTTP action's function) for each job, then ``run``. An
        issue that no run holds any more is handed to a fresh plan (it joins ``eligible``). A job the
        CLI refuses to retry pauses only its own issues (exponentially, like a failing repository),
        so the other issues of the repository are still planned in the same pass.
        """
        owners: dict = {}  # run -> {job: [issues]}
        for number in trigger.pick(retries, conf.max_issues):
            found, why = self._owner_and_reason(paths, number)
            if found is None:
                eligible.append(number)
                if why:  # blocked on a dependency that can never merge: a fresh plan, not a retry
                    self._say(
                        repo,
                        f"trigger repo={escape_field(repo)} issue={number} retry replanned reason={why}",
                        ("replan", number),
                    )
            else:
                owners.setdefault(found[0], {}).setdefault(found[1], []).append(number)
        if not owners:
            return False
        run = max(owners)  # the newest run; the others keep their label and wait
        target = self.manager.target_for(repo, run)
        asked = sum(len(numbers) for numbers in owners[run].values())
        owed = target.run_dir / OWED_MARKER
        write_marker(owed, str(asked))  # before the first retry: a restart then still finds the run owed
        retried: list = []
        refused: list = []
        for job, numbers in sorted(owners[run].items()):
            try:
                ok = self.manager.retry_job(target, job, trigger.RETRY_REASON).status == 200
            except ApiError:
                ok = False
            (retried if ok else refused).extend(numbers)
        delay = max((self._refused.failed((repo, number)) for number in refused), default=0.0)
        if refused:
            self._line(
                f"trigger repo={escape_field(repo)} run={escape_field(run)} retry refused issues={len(refused)} backoff={delay:g}s reason={trigger.REASON_REFUSED}"
            )
        if not retried:
            with contextlib.suppress(OSError):
                owed.unlink()
            return False
        for number in retried:
            self._refused.succeeded((repo, number))
        if refused:
            write_marker(owed, str(len(retried)))
        self._consume(repo, cfg, conf, retried)
        self._start_build(repo, conf, target, len(retried), retry=True)
        return True

    @classmethod
    def _owner(cls, paths: DataPaths, number: int) -> tuple | None:
        """(run, job id) of the job a retry of this issue resumes, or None (see ``_owner_and_reason``)."""
        return cls._owner_and_reason(paths, number)[0]

    @staticmethod
    def _owner_and_reason(paths: DataPaths, number: int) -> tuple:
        """(run, job id) of the newest started run that mentions this issue, if that mention is a job.

        The newest started run whose plan has a job, an investigation, a blocked or an epic disposition for the
        issue decides. A job is what a retry resumes. An investigation, a blocked or an epic disposition means
        the latest plan chose not to build it, so an older run's job must not be retried in its place:
        its plan never saw the answers a person gave since, and the issue needs a fresh plan (None). A
        ``covered`` disposition does not end the search: the plan only says another job's work
        resolves the issue, and it gives the issue no label that could ask for a retry.

        A job that was blocked because a dependency was closed without merging has no owner either:
        a retry would restart it under its old plan, which still holds that dependency, and it would be
        blocked again at once. The issue needs a fresh plan against the current main. The answer is
        ``(owner, why)``; ``why`` is the fixed reason (``dependency-closed``) when that is the case, else "".
        """
        why = ""
        for entry in sorted(_list_runs(paths), key=lambda item: item["run"], reverse=True):
            if not entry["started"]:
                continue
            document = trigger.read_plan(paths.run_dir(entry["run"]) / "plan.json")
            if document is None:
                continue
            job = trigger.job_owning(document, number)
            if job is not None and JOB_ID.fullmatch(job):
                state = trigger.read_plan(paths.run_dir(entry["run"]) / "state.json")
                if trigger.blocked_by_closed_dependency(state, job):
                    # It can never run: not an owner. Another run may hold a live job for the issue (the
                    # runs are ordered by name, not by time), so keep looking before planning afresh.
                    why = trigger.REASON_DEPENDENCY_CLOSED
                    continue
                return (entry["run"], job), ""
            if trigger.has_disposition(document, number, NOT_BUILT):
                return None, why
        return None, why

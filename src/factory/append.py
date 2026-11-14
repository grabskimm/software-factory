"""``plan --issues N,... --append-to <run>``: plan more issues for a run that is already running.

The planner sees the run's current jobs and the result is written as a validated plan fragment
(``fragments.py``) into the run's ``inbox/``; the supervisor ingests it under its own lease. Nothing
here touches ``plan.json``, ``plan.sha256`` or ``state.json``, and the repository lease is not taken
(the running supervisor holds it): two appends are serialised by a per-run lock file instead
(``append.lock`` in the run directory), and the planner works in its own worktree under the run.

Every refusal is a fixed-format ``ValueError``; issue titles, bodies and the planner's reasons are
never printed. Each append writes its own ``planner-usage-<name>.json`` next to the run's
``planner-usage.json`` (never over it): the run's cost is the sum of all of them.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import secrets
import sys
from datetime import datetime, timezone

from . import epic, fragments
from .control import supervisor_status
from .planner import USAGE_PATTERN, planner_usage_records
from .state import atomic_text
from .status import cost_usd

LOCK_FILE = "append.lock"
PLANNER_TREE = "planner-append"  # under <run>/worktrees
WORKDIR = "appends"  # <run>/appends/<name>: the snapshot and planner files of one append
# The refusals of ``check_run`` that the label trigger answers with a skip reason of its own.
DRAINING = "run is draining"
JOB_CAP = "run is at the job cap"
OVER_BUDGET = "run budget reached"
CONTEXT = (
    "\nThis is an append to a run that is already running. Plan only the selected issues: do not "
    "repeat, change or re-plan any job listed below. A new job may list one of these jobs (or a new "
    "job) in depends_on when it needs that work merged first, and should avoid likely_paths that "
    "overlap jobs that are not merged yet. Zero jobs is fine when every selected issue is "
    "blocked, covered or an investigation. The jobs of the run (untrusted task data):\n"
)


def refuse(reason):
    raise ValueError(f"append refused: {reason}")


@contextlib.contextmanager
def append_lock(directory):
    """Serialise appends to one run (blocking); the file stays in the run directory."""
    descriptor = os.open(
        directory / LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _load(path):
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        refuse("run files unreadable")
    if not isinstance(value, dict):
        refuse("run files unreadable")
    return value


def _waiting(directory):
    """``(name, raw bytes, (issues, jobs, dispositions))`` of every readable fragment waiting in the inbox, by name."""
    try:
        handle = os.open(
            directory / "inbox", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
    except OSError:
        return
    try:
        for name in sorted(n for n in os.listdir(handle) if fragments.NAME_PATTERN.fullmatch(n)):
            try:
                raw = fragments.read(handle, name, os.geteuid())
                parsed = fragments.parse(raw, fragments.MAX_RUN_JOBS)
            except (FileNotFoundError, fragments.FragmentError):
                continue
            yield name, raw, parsed
    finally:
        os.close(handle)


def pending(directory, plan):
    """``(plan with the fragments waiting in the inbox applied, their job ids)``.

    A fragment that does not apply is skipped: the supervisor quarantines it, and it takes
    nothing from the run. Read-only.
    """
    effective, waiting = plan, []
    for name, raw, (issues, jobs, dispositions) in _waiting(directory):
        try:
            effective = fragments.merge(
                effective,
                name,
                hashlib.sha256(raw).hexdigest(),
                issues,
                jobs,
                fragments.MAX_RUN_JOBS,
                dispositions,
            )
        except fragments.FragmentError:
            continue
        waiting += [job["id"] for job in jobs]
    return effective, waiting


def waiting_issues(directory):
    """The issue numbers of the fragments waiting in the inbox (those the supervisor has not ingested yet). Read-only."""
    return {issue["number"] for _, _, (issues, _, _) in _waiting(directory) for issue in issues}


def check_run(directory, budget):
    """The run's ``(effective plan, pending job ids, job states)``, or a refusal.

    Refused: no such run, a run that was not started (no ``state.json``), a draining run, a run
    at ``fragments.MAX_RUN_JOBS`` jobs (counting the fragments still waiting), and a run whose
    cost so far (jobs plus every planner usage file) has reached ``budget``.
    """
    if not directory.is_dir() or not (directory / "plan.json").is_file():
        refuse("run not found")
    if not (directory / "state.json").is_file():
        refuse("run not started")
    plan, data = _load(directory / "plan.json"), _load(directory / "state.json")
    owner = supervisor_status(directory)
    if owner and owner.get("status") in {"running", "draining"} and (
        owner["status"] == "draining" or owner.get("drain_requested_at")
    ):
        refuse(DRAINING)
    try:
        effective, waiting = pending(directory, plan)
        states = {key: dict(value) for key, value in data["jobs"].items()}
        count = len(effective["jobs"])
    except (KeyError, TypeError, AttributeError):
        refuse("run files unreadable")
    if count >= fragments.MAX_RUN_JOBS:
        refuse(JOB_CAP)
    spent = sum(cost_usd(s.get("usage")) for s in states.values())
    spent += cost_usd(planner_usage_records(directory))
    if budget is not None and spent >= budget:
        refuse(OVER_BUDGET)
    return effective, waiting, states


def candidates(plan, numbers):
    """The listed issues that are not in the run yet; the others get one fixed line each."""
    known = {issue["number"] for issue in plan["issues"]}
    kept = []
    for number in numbers:
        if number in known:
            print(f"factory: plan issue={number} dropped reason=in-run", file=sys.stderr)
        else:
            kept.append(number)
    if not kept:
        raise ValueError("none of the listed issues can be planned")
    return kept


def context(plan, waiting, states):
    """The prompt text that lists the run's jobs: id, likely_paths, status, depends_on."""
    jobs = [
        {
            "id": job["id"],
            "likely_paths": job["likely_paths"],
            "status": "pending" if job["id"] in waiting else (states.get(job["id"]) or {}).get("status"),
            "depends_on": job.get("depends_on", []),
        }
        for job in plan["jobs"]
    ]
    return CONTEXT + json.dumps(jobs)


def new_name():
    """``append-<UTC time, microseconds>-<random>``: names sort (so ingest) in the order appends were made."""
    return f"append-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%f}-{secrets.token_hex(3)}"


def write_usage(directory, name, usage):
    """The append's own usage record; never ``planner-usage.json`` (``USAGE_PATTERN`` finds it)."""
    path = directory / f"planner-usage-{name}.json"
    if not USAGE_PATTERN.fullmatch(path.name):
        raise ValueError("append: invalid name")
    atomic_text(path, json.dumps(usage))
    return path


def build_fragment(result, issues):
    """The fragment document for a planner result: jobs, plus a disposition for every other issue."""
    try:
        jobs = [
            {k: v for k, v in job.items() if k in fragments._REQUIRED | fragments._OPTIONAL}
            for job in result["jobs"]
        ]
        dispositions = [
            {
                "issue": item["issue"],
                "status": item["status"],
                "reason": str(item["reason"])[: fragments.MAX_REASON].strip() or "none given",
                **({"epic": item["epic"]} if item["status"] == epic.STATUS else {}),
            }
            for item in result["dispositions"]
            if item["status"] != "job"
        ]
    except (KeyError, TypeError, AttributeError):
        raise ValueError("append refused: planner result invalid") from None
    return {
        "version": 1,
        "kind": "plan_fragment",
        "issues": [
            {"number": i["number"], "title": i["title"], "body": i["body"]} for i in issues
        ],
        "jobs": jobs,
        "dispositions": dispositions,
    }


def validated(document, name, plan):
    """The fragment's bytes, checked as the supervisor will check them against ``plan``."""
    raw = (json.dumps(document, indent=2) + "\n").encode()
    if len(raw) > fragments.MAX_FRAGMENT_BYTES:
        refuse("fragment too large")
    try:
        issues, jobs, dispositions = fragments.parse(raw, fragments.MAX_RUN_JOBS)
        fragments.merge(
            plan,
            f"{fragments.PREFIX}{name}.json",
            hashlib.sha256(raw).hexdigest(),
            issues,
            jobs,
            fragments.MAX_RUN_JOBS,
            dispositions,
        )
    except fragments.FragmentError as error:
        refuse(f"planner fragment invalid ({error.reason})")
    return raw


def write_fragment(directory, name, raw):
    """``inbox/fragment-<name>.json``: a temp file in the inbox, then a rename (atomic)."""
    inbox = directory / "inbox"
    inbox.mkdir(mode=0o700, exist_ok=True)
    handle = os.open(inbox, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    final = f"{fragments.PREFIX}{name}.json"
    temp = f".{final}.{secrets.token_hex(4)}.tmp"
    try:
        descriptor = os.open(
            temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=handle
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, final, src_dir_fd=handle, dst_dir_fd=handle)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp, dir_fd=handle)
            raise
        os.fsync(handle)
    finally:
        os.close(handle)
    return inbox / final

"""Plan fragments: jobs appended to a running run through its ``inbox/``.

A fragment is a JSON file ``<run>/inbox/fragment-<name>.json`` (``NAME_PATTERN``), written
atomically (temp file in the same directory, then rename). The supervisor, and only the
supervisor, ingests it at the start of a scheduling tick, under its own lease (``Runner``).
Nothing else is a fragment: the evidence, blocker and capability documents written by
``manager.write_inbox`` are named ``<sha256>.json`` and are never looked at, and a temp
file (any name that does not match) is left alone until its writer renames it.

Document (every field required, no others)::

    {"version": 1, "kind": "plan_fragment",
     "issues": [{"number": 7, "title": "...", "body": "..."}],
     "jobs": [{"id": "...", "issues": [7], "objective": "...", "likely_paths": ["src"],
               "acceptance": ["..."], "feature": false,
               "depends_on": ["..."], "context_issues": []}],
     "dispositions": [{"issue": 8, "status": "blocked", "reason": "..."}]}

An ``epic`` disposition (``epic.py``) also carries its ``epic`` object (phases and questions).

Each ``acceptance`` item is a string (a ``sandbox`` criterion) or an object
``{"text", "tag": "sandbox"|"ci"|"operator", "checks": [...], "workflow": "...", "reason": "..."}`` as in a
plan (``model.criterion``).
``depends_on`` and ``context_issues`` are optional, and so is ``dispositions``: issues the
planner did not turn into a job (``status`` is ``blocked``, ``covered``, ``investigation`` or ``epic``,
never ``job``; ``reason`` is planner prose, at most ``MAX_REASON`` characters, and never
logged). A fragment may have no job if it has a disposition. A fragment may only append: job
ids and issue numbers must be new, ``depends_on`` may name existing or new jobs but not form a
cycle, the fragment's issues are exactly the issues its jobs own plus its disposition issues
(no overlap), and no existing job, issue or disposition is modified. It is applied whole or
quarantined whole. ``plan --append-to`` (``append.py``) writes these files.

Every file is opened relative to the inbox directory descriptor with ``O_NOFOLLOW``, must be
a regular file owned by the supervisor's user and at most ``MAX_FRAGMENT_BYTES``. Files are
taken in name order, so the result of several fragments in one tick is deterministic. A
rejected file is renamed ``<file>.rejected`` (never deleted, never applied), an applied one
``<file>.applied``; neither matches ``NAME_PATTERN`` again.

Each decision logs one line (``format_decision``)::

    factory: fragment name=<name> decision=<applied|already-applied|quarantined|deferred> jobs=<id,id|-> reason=<reason>
"""

import errno
import hashlib
import json
import os
import re
import stat

from . import epic
from .artifact_paths import artifact_roots
from .conventions import TYPES as CHANGE_TYPES
from .model import criteria, validate_plan
from .model_choice import check_choice

PREFIX = "fragment-"
NAME_PATTERN = re.compile(r"fragment-[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\.json")
MAX_FRAGMENT_BYTES = 128 * 1024
MAX_RUN_JOBS = 10
MAX_REASON = 500
DISPOSITION_STATUSES = ("blocked", "covered", "investigation", epic.STATUS)
MAX_PER_TICK = 32
REJECTED = ".rejected"
APPLIED = ".applied"

_JOB_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,59}")
_REQUIRED = {"id", "issues", "objective", "likely_paths", "acceptance", "feature"}
_OPTIONAL = {"depends_on", "context_issues", "choice", "change_type", "epic_phase"}


class FragmentError(Exception):
    """A fragment that cannot be applied; ``reason`` is a fixed token (no file content)."""

    def __init__(self, reason, detail=None):
        self.reason = f"{reason}:{detail}" if detail is not None else reason
        super().__init__(self.reason)


def plan_digest(plan):
    """The digest that pins a plan for the run (``plan.sha256``); ``cli.load_plan`` computes the same."""
    return hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()


def format_decision(name, decision, jobs, reason):
    return (
        f"factory: fragment name={name} decision={decision} "
        f"jobs={','.join(jobs) or '-'} reason={reason}"
    )


def _is_int(value):
    return type(value) is int and value > 0


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _string_list(value, field, empty_ok):
    if (
        not isinstance(value, list)
        or (not empty_ok and not value)
        or not all(_text(item) for item in value)
    ):
        raise FragmentError("schema", field)
    return value


def _dispositions(value):
    """The validated ``dispositions`` field: a list of ``{issue, status, reason}``."""
    if not isinstance(value, list):
        raise FragmentError("schema", "dispositions")
    for item in value:
        # An ``epic`` disposition also carries its bounded phase plan (``epic.validate``).
        keys = {"issue", "status", "reason"} | ({"epic"} if isinstance(item, dict) and item.get("status") == epic.STATUS else set())
        if (
            not isinstance(item, dict)
            or set(item) != keys
            or not _is_int(item["issue"])
            or not isinstance(item["status"], str)
            or item["status"] not in DISPOSITION_STATUSES
        ):
            raise FragmentError("schema", "dispositions")
        if "epic" in item:
            try:
                epic.validate(item["epic"])
            except ValueError:
                raise FragmentError("schema", "epic") from None
        if not _text(item["reason"]):
            raise FragmentError("schema", "reason")
        if len(item["reason"]) > MAX_REASON:
            raise FragmentError("schema", "reason-length")
    return value


def parse(raw, max_jobs):
    """The validated ``(issues, jobs, dispositions)`` of a fragment document, or ``FragmentError``."""
    try:
        document = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        raise FragmentError("invalid-json") from None
    if not isinstance(document, dict):
        raise FragmentError("schema", "document")
    if not {"version", "kind", "issues", "jobs"} <= set(document) <= {
        "version",
        "kind",
        "issues",
        "jobs",
        "dispositions",
    }:
        raise FragmentError("schema", "fields")
    if document["version"] != 1 or type(document["version"]) is not int:
        raise FragmentError("schema", "version")
    if document["kind"] != "plan_fragment":
        raise FragmentError("schema", "kind")
    issues, jobs = document["issues"], document["jobs"]
    if not isinstance(issues, list) or not issues:
        raise FragmentError("schema", "issues")
    for issue in issues:
        if (
            not isinstance(issue, dict)
            or set(issue) != {"number", "title", "body"}
            or not _is_int(issue["number"])
            or not isinstance(issue["title"], str)
            or not isinstance(issue["body"], str)
        ):
            raise FragmentError("schema", "issues")
    dispositions = _dispositions(document.get("dispositions", []))
    if not isinstance(jobs, list) or not (jobs or dispositions):
        raise FragmentError("schema", "jobs")
    if len(jobs) > max_jobs:
        raise FragmentError("job-cap", f"{len(jobs)}>{max_jobs}")
    for job in jobs:
        if not isinstance(job, dict):
            raise FragmentError("schema", "jobs")
        keys = set(job)
        if not _REQUIRED <= keys or keys - _REQUIRED - _OPTIONAL:
            raise FragmentError("schema", "job-fields")
        if not isinstance(job["id"], str) or not _JOB_ID.fullmatch(job["id"]):
            raise FragmentError("schema", "job-id")
        if not _text(job["objective"]):
            raise FragmentError("schema", "objective")
        if type(job["feature"]) is not bool:
            raise FragmentError("schema", "feature")
        if "change_type" in job and job["change_type"] not in CHANGE_TYPES:
            raise FragmentError("schema", "change_type")
        for field, empty_ok in (
            ("likely_paths", False),
            ("depends_on", True),
        ):
            if field in job:
                _string_list(job[field], field, empty_ok)
        # Criteria are plain strings or tagged objects (model.criterion); no text is echoed.
        try:
            criteria(job)
        except ValueError:
            raise FragmentError("schema", "acceptance") from None
        # Ids end up in log lines (unknown-dependency, cycle): only the id charset gets there.
        if not all(_JOB_ID.fullmatch(item) for item in job.get("depends_on", [])):
            raise FragmentError("schema", "depends_on")
        if "choice" in job:
            try:
                check_choice(job["choice"])
            except ValueError:
                raise FragmentError("schema", "choice") from None
        for field in ("issues", "context_issues"):
            value = job.get(field, [])
            if not isinstance(value, list) or not all(_is_int(n) for n in value):
                raise FragmentError("schema", field)
        if not job["issues"]:
            raise FragmentError("schema", "issues")
        if "epic_phase" in job:
            try:
                stamp = epic.check_phase_stamp(job["epic_phase"])
            except ValueError:
                raise FragmentError("schema", "epic_phase") from None
            if job["issues"] != [stamp["child"]]:
                raise FragmentError("schema", "epic_phase")
    return issues, jobs, dispositions


def _check_cycles(jobs):
    """New jobs depend only on existing or new jobs, so a cycle can only be among the new."""
    dependencies = {job["id"]: job.get("depends_on", []) for job in jobs}
    visited, visiting = set(), set()

    def visit(key):
        if key in visiting:
            raise FragmentError("cycle", key)
        if key in visited or key not in dependencies:
            return
        visiting.add(key)
        for dependency in dependencies[key]:
            visit(dependency)
        visiting.remove(key)
        visited.add(key)

    for key in dependencies:
        visit(key)


def merge(plan, name, digest, issues, jobs, max_jobs, dispositions=()):
    """The plan with the fragment appended (a new dict, the argument is untouched), or ``FragmentError``."""
    existing = {job["id"] for job in plan["jobs"]}
    seen = set()
    for job in jobs:
        if job["id"] in existing or job["id"] in seen:
            raise FragmentError("id-collision", job["id"])
        seen.add(job["id"])
    if len(existing) + len(jobs) > max_jobs:
        raise FragmentError("job-cap", f"{len(existing) + len(jobs)}>{max_jobs}")
    known = {issue["number"] for issue in plan["issues"]}
    numbers = [issue["number"] for issue in issues]
    owned = [number for job in jobs for number in job["issues"]]
    declared = [item["issue"] for item in dispositions]
    if len(set(numbers)) != len(numbers) or known & (set(numbers) | set(declared)):
        raise FragmentError("issue-collision")
    if set(owned) & set(declared):
        raise FragmentError("issue-ownership")
    if sorted([*owned, *declared]) != sorted(numbers):
        raise FragmentError("issue-ownership")
    for job in jobs:
        for dependency in job.get("depends_on", []):
            if dependency not in existing and dependency not in seen:
                raise FragmentError("unknown-dependency", dependency)
    _check_cycles(jobs)
    added = [{**job, "fragment": name} for job in jobs]
    candidate = {
        **plan,
        "issues": [*plan["issues"], *issues],
        "jobs": [*plan["jobs"], *added],
        "dispositions": [
            *plan["dispositions"],
            *(
                {
                    "issue": number,
                    "status": "job",
                    "job": job["id"],
                    "reason": f"added by plan fragment {name}",
                }
                for job in jobs
                for number in job["issues"]
            ),
            *(dict(item) for item in dispositions),
        ],
        "fragments": [
            *plan.get("fragments", []),
            {
                "name": name,
                "sha256": digest,
                "jobs": [job["id"] for job in jobs],
                **({"dispositions": declared} if declared else {}),
            },
        ],
    }
    try:
        before = {job["id"]: artifact_roots(job, plan) for job in plan["jobs"]}
        validate_plan(candidate)
        after = {job["id"]: artifact_roots(job, candidate) for job in plan["jobs"]}
    except (ValueError, KeyError, TypeError):
        raise FragmentError("invalid-plan") from None
    if before != after:
        raise FragmentError("artifact-ownership")
    return candidate


def read(directory, name, uid):
    """The bytes of one inbox file, or ``FragmentError``: no-follow, regular, owned, size-capped."""
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        raise
    except OSError:
        raise FragmentError("unreadable") from None
    if stat.S_ISLNK(info.st_mode):
        raise FragmentError("symlink")
    if not stat.S_ISREG(info.st_mode):
        raise FragmentError("not-regular")
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=directory,
        )
    except FileNotFoundError:
        raise
    except OSError as error:
        raise FragmentError(
            "symlink" if error.errno == errno.ELOOP else "unreadable"
        ) from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise FragmentError("not-regular")
        if info.st_uid != uid:
            raise FragmentError("owner")
        if info.st_size > MAX_FRAGMENT_BYTES:
            raise FragmentError("size")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(MAX_FRAGMENT_BYTES + 1)
    except OSError:
        raise FragmentError("unreadable") from None
    finally:
        os.close(descriptor)
    if len(raw) > MAX_FRAGMENT_BYTES:
        raise FragmentError("size")
    return raw


def _retire(directory, name, suffix):
    """Rename ``name`` to ``name + suffix`` (a numbered variant if that exists); the new name."""
    for attempt in range(100):
        target = name + suffix + (f".{attempt}" if attempt else "")
        try:
            os.stat(target, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            os.rename(name, target, src_dir_fd=directory, dst_dir_fd=directory)
            return target
    raise OSError(errno.EEXIST, "no free name")


def _retire_quietly(directory, name, suffix):
    """``_retire`` that reports failure as False instead of raising."""
    try:
        _retire(directory, name, suffix)
    except OSError:
        return False
    return True


def ingest(inbox, plan, commit, log, max_jobs=MAX_RUN_JOBS, uid=None, skip=None, fence=None):
    """Apply the fragments waiting in ``inbox``; returns the names applied, in order.

    ``plan`` is the live plan; ``commit(merged)`` must persist the merged plan and make it
    the live one before returning (so the next fragment is checked against it). A fragment
    whose digest the plan already records was applied before a crash: it is only retired.
    ``skip`` is a set of names that could not be renamed on an earlier tick; it is added to.
    ``fence`` is called once before anything is changed and raises when the lease is lost.
    """
    uid = os.geteuid() if uid is None else uid
    skip = set() if skip is None else skip
    try:
        directory = os.open(
            inbox, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
    except OSError:
        return []
    applied = []
    try:
        names = sorted(
            name for name in os.listdir(directory) if NAME_PATTERN.fullmatch(name)
        )
        names = [n for n in names if n not in skip][:MAX_PER_TICK]
        if names and fence is not None:
            fence()
        for name in names:
            try:
                raw = read(directory, name, uid)
                digest = hashlib.sha256(raw).hexdigest()
                ledger = {item["sha256"]: item for item in plan.get("fragments", [])}
                if digest in ledger:
                    done = ledger[digest]["jobs"]
                    if _retire_quietly(directory, name, APPLIED):
                        log(format_decision(name, "already-applied", done, "replay"))
                    else:
                        skip.add(name)
                        log(format_decision(name, "deferred", done, "retire-failed"))
                    continue
                issues, jobs, dispositions = parse(raw, max_jobs)
                merged = merge(plan, name, digest, issues, jobs, max_jobs, dispositions)
            except FileNotFoundError:
                continue  # taken away between listing and reading
            except FragmentError as error:
                if _retire_quietly(directory, name, REJECTED):
                    log(format_decision(name, "quarantined", [], error.reason))
                else:
                    skip.add(name)
                    log(format_decision(name, "deferred", [], "quarantine-failed"))
                continue
            ids = [job["id"] for job in jobs]
            try:
                commit(merged)
            except OSError:
                log(format_decision(name, "deferred", ids, "commit-failed"))
                break
            applied.append(name)
            log(format_decision(name, "applied", ids, "ok"))
            if not _retire_quietly(directory, name, APPLIED):
                skip.add(name)  # the plan records it; a restart retires it as a replay
    finally:
        os.close(directory)
    return applied

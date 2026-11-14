"""Read-only operational summaries, including retained runs without timing metadata."""

import math
from copy import deepcopy
from datetime import datetime, timezone

from . import apierror, podhandle
from .agent import MAX_COST_USD
from .github import GREEN_BUCKETS, ci_assessment
from .github import handoff_status as pr_handoff_status
from .model import criteria, overlaps, shared_path_blockers, verification_skipped
from .model_choice import describe, recorded_choice
from .publish_block import (
    MAX_SHOWN,
    one_line,
    publish_blocked_paths,
    publish_hint,
    recorded_hold,
)
from .reason_codes import CI_PENDING, WORKFLOW_HOLD, derive
from .session_timeout import CONFIG_KEY as TIMEOUT_KEY
from .session_timeout import seconds_of as timeout_seconds_of
from .state import elapsed, pid_status, timestamp


def _phase(job):
    phase = (
        job.get("worker_phase")
        or job.get("pending_phase")
        or job.get("phase")
        or "unknown"
    )
    return phase.split("-", 1)[0]


def _now(value):
    if value is None:
        return timestamp()
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return value


def cost_usd(records):
    """Sum client-side cost estimates; legacy or malformed entries (negative or not finite included) count as zero.

    The sum is never above ``MAX_COST_USD``.
    """
    total = 0.0
    for record in records or ():
        value = record.get("total_cost_usd") if isinstance(record, dict) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
            total += value
    return round(min(total, MAX_COST_USD), 6)


def _p95(values):
    """Nearest-rank 95th percentile of a non-empty list."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def footprint_summary(jobs):
    """Per phase: invocations with a footprint record, peak and p95 of resident MiB and cpu seconds.

    Read-only, from ``usage[*].footprint`` (``memwatch``); records without one (older runs, a
    platform without ``/proc``) count as nothing. These are sampled figures, not a
    measurement campaign: ``docs/capacity.md`` says what they do and do not show.
    """
    found = {}
    for job in jobs:
        for record in job.get("usage") or ():
            footprint = record.get("footprint") if isinstance(record, dict) else None
            if not isinstance(footprint, dict):
                continue
            rss, cpu = footprint.get("peak_rss_mb"), footprint.get("cpu_seconds")
            if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (rss, cpu)):
                found.setdefault(str(record.get("phase")), []).append((rss, cpu))
    return {
        phase: {
            "invocations": len(pairs),
            "peak_rss_mb": max(rss for rss, _ in pairs),
            "p95_rss_mb": _p95([rss for rss, _ in pairs]),
            "peak_cpu_seconds": max(cpu for _, cpu in pairs),
            "p95_cpu_seconds": _p95([cpu for _, cpu in pairs]),
        }
        for phase, pairs in sorted(found.items())
    }


# Token counts of one session, as the Claude Code ``result.usage`` object names them.
# ``input_tokens`` is the uncached input.
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
# The split of ``cache_creation_input_tokens`` by cache lifetime: ``usage.cache_creation`` is a nested object
# that holds these two, and only when the CLI reports them (they are absent otherwise, never assumed).
CACHE_LIFETIME_FIELDS = ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")
# No real count comes near this; it keeps a hostile or corrupt number out of sums, JSON and the ledger.
MAX_TOKEN_COUNT = 2**53


def token_count(value):
    """A token count: a non-negative int (a bool is not one) within ``MAX_TOKEN_COUNT``, else None."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_TOKEN_COUNT:
        return None
    return value


def usage_tokens(usage):
    """The token counts of one ``usage`` object, by name: only those that are present and valid.

    Reads ``input_tokens``, ``output_tokens``, ``cache_creation_input_tokens`` and ``cache_read_input_tokens``
    and, from the nested ``cache_creation`` object, ``ephemeral_5m_input_tokens`` and ``ephemeral_1h_input_tokens``
    when the CLI reports them. Anything that is not a non-negative int is ignored, never raised.
    """
    if not isinstance(usage, dict):
        return {}
    found = {name: token_count(usage.get(name)) for name in TOKEN_FIELDS}
    split = usage.get("cache_creation")
    if isinstance(split, dict):
        found.update({name: token_count(split.get(name)) for name in CACHE_LIFETIME_FIELDS})
    return {name: count for name, count in found.items() if count is not None}


def cache_hit_rate(counts):
    """``cache_read / (cache_read + cache_creation + uncached input)``, or None when that sum is zero.

    ``counts`` maps token field names to counts (a missing one is zero). A session that read nothing from the cache has
    a rate of 0.0; one that used no input tokens at all has none (None, not 0): there is nothing to divide.
    """
    read = counts.get("cache_read_input_tokens", 0)
    total = read + counts.get("cache_creation_input_tokens", 0) + counts.get("input_tokens", 0)
    return round(read / total, 6) if total else None


def token_totals(records):
    """``{phase: {"invocations": n, <name>: summed count}}`` over usage records.

    A record counts when its ``usage`` carries at least one valid token count; a name appears in a phase's row only when
    some record of the phase reported it. Read-only; records without usage (older runs, a failed session) count as nothing.
    """
    totals = {}
    for record in records or ():
        counts = usage_tokens(record.get("usage")) if isinstance(record, dict) else {}
        if not counts:
            continue
        row = totals.setdefault(str(record.get("phase")), {"invocations": 0})
        row["invocations"] += 1
        for name, count in counts.items():
            row[name] = row.get(name, 0) + count
    return totals


def token_summary(jobs, planner_usage=None):
    """Per phase: invocations with token counts, the four counts, the lifetime split when reported, the hit rate.

    From ``usage[*].usage`` of every job, plus the planner's record. The split keys appear only for a phase whose records
    carried them; ``cache_hit_rate`` is ``cache_hit_rate`` of the row (None while its input total is zero). Read-only:
    ``docs/operations.md`` ("Tokens and cache hit rate") says what these figures are and are not.
    """
    records = []
    for job in jobs:
        usage = job.get("usage") if isinstance(job, dict) else None
        records.extend(usage if isinstance(usage, list) else ())
    if isinstance(planner_usage, dict):
        records.append(planner_usage)
    summary = {}
    for phase, row in sorted(token_totals(records).items()):
        full = {name: row.get(name, 0) for name in TOKEN_FIELDS}
        full.update({name: row[name] for name in CACHE_LIFETIME_FIELDS if name in row})
        summary[phase] = {"invocations": row["invocations"], **full, "cache_hit_rate": cache_hit_rate(full)}
    return summary


def planner_record(records):
    """One usage record for the planner's usage files (``plan`` and each ``plan --append-to`` write one), or None.

    With one file it is that record unchanged. With several it is the first, its ``total_cost_usd`` summed over all of
    them and its ``usage`` holding the summed token counts (in the CLI's own shape), so cost and tokens both cover the run.
    """
    if len(records) < 2:
        return records[0] if records else None
    sums = {}
    for record in records:
        for name, count in usage_tokens(record.get("usage")).items():
            sums[name] = sums.get(name, 0) + count
    usage = {name: sums[name] for name in TOKEN_FIELDS if name in sums}
    split = {name: sums[name] for name in CACHE_LIFETIME_FIELDS if name in sums}
    if split:
        usage["cache_creation"] = split
    merged = {**records[0], "total_cost_usd": cost_usd(records)}
    if usage:
        merged["usage"] = usage
    return merged


def _one_line(text):
    """Untrusted criterion text on one line, without terminal control characters."""
    return "".join(c if c.isprintable() else " " for c in text)


def criteria_report(specification, snapshot, acceptance_proven, skipped=False, published_required=None):
    """Each criterion with its tag, check names and what is proven about it right now.

    ``proof`` is ``satisfied``, ``pending``, ``failed`` or ``unproven``. A ``sandbox`` criterion follows the
    verify phase for the exact head, and is ``unproven`` when that phase was skipped (``skipped``,
    from ``verify.when``): a verification that ran nothing proves nothing. A ``ci`` criterion is
    satisfied only when every named check is among the base branch's required checks on the PR and
    passing (``pending`` while one is still running, ``failed`` when one failed or was cancelled; a
    skipped required check counts as passing, as it does for GitHub and ``ci_gate``); named checks that
    are not required (or not readable) leave it ``unproven``, never satisfied, and a workflow
    name alone cannot be matched to a required check here. ``published_required`` is the list of
    required-check names ``Runner.publish`` judged the job against (``None`` for a run that did
    not record it): when the pull request lists the checks a ``ci`` criterion names as required but
    publish did not judge them required, the row stays ``unproven`` and carries ``required_sources``
    (``{"publish": False, "pr": True}``). The other direction is not a disagreement: the pull
    request's list only holds required checks that already reported on the head, so a check publish
    judged required but the list lacks may simply not have reported yet; the row stays ``unproven``
    (never satisfied) without claiming that the pull request says it is not required. An ``operator`` criterion is
    ``unproven`` until a person proves it; it is a ``Needs a person`` item of the PR and never
    holds the job back. Nothing here runs anything.
    """
    if not specification.get("acceptance"):
        return []
    required = (snapshot or {}).get("requiredChecks")
    buckets = {}
    if isinstance(required, list):
        for check in required:
            # factory/gate is Factory's own signal, never proof of a criterion.
            if (
                isinstance(check, dict)
                and isinstance(check.get("name"), str)
                and check["name"] != "factory/gate"
            ):
                buckets.setdefault(check["name"], []).append(check.get("bucket"))
    # Only a recorded list is compared, and only against a required list that could be read.
    published = (
        {name for name in published_required if isinstance(name, str)}
        if isinstance(published_required, list) and isinstance(required, list)
        else None
    )
    rows = []
    for item in criteria(specification):
        extra = {}
        pr_covers = bool(item["checks"]) and all(name in buckets for name in item["checks"])
        if item["tag"] == "sandbox":
            if not acceptance_proven:
                proof = "pending"
            else:
                proof = "unproven" if skipped else "satisfied"
        elif item["tag"] == "operator":
            proof = "unproven"
        elif pr_covers:
            found = [b for name in item["checks"] for b in buckets[name]]
            if any(b in ("fail", "cancel") for b in found):
                proof = "failed"
            elif all(b in GREEN_BUCKETS for b in found):
                proof = "satisfied"
            else:
                proof = "pending"
        else:
            proof = "unproven"
        if item["tag"] == "ci" and item["checks"] and published is not None:
            # The PR's list is positive evidence only: it holds the required checks that already
            # reported, so a name missing from it may be required and not reported yet. Only a check
            # the PR lists as required that publish did not judge required is a real disagreement.
            publish_covers = all(name in published for name in item["checks"])
            if pr_covers and not publish_covers:
                # Two sources, two answers: never satisfied, and both are shown.
                proof = "unproven"
                extra = {"required_sources": {"publish": False, "pr": True}}
        rows.append({**item, "proof": proof, **extra})
    return rows


def job_reason_code(job, status, handoff, snapshot):
    """The reason code of a published job's incomplete or pending outcome, else None.

    From ``state.json`` (the verdicts and the code ``Runner.publish`` stored) and, when a live PR
    snapshot is at hand, the gate bucket and CI handoff: a pending gate that has since gone green
    carries no code, a red required check (or a gate that failed) is ``ci_failed``. Read only.
    """
    if status not in ("pr_ready", "pr_incomplete"):
        return None
    stored = job.get("reason_code")
    gate = None
    for check in (snapshot or {}).get("requiredChecks") or ():
        if isinstance(check, dict) and check.get("name") == "factory/gate":
            gate = check.get("bucket")
    if stored == WORKFLOW_HOLD:
        # A deliberate hold for a person; it ends when the person verified it (the gate turned green).
        return None if gate == "pass" else WORKFLOW_HOLD
    ci = None
    if handoff == "ci_failed" or (stored == CI_PENDING and gate == "fail"):
        ci = "failed"
    elif stored == CI_PENDING and gate != "pass":
        ci = "pending"
    return derive(job.get("review"), job.get("verification"), ci, held=status == "pr_incomplete")


def _worker_liveness(job, status, pod_reader):
    """``(live, pod_unknown)`` of a job: whether its worker may be running, and whether that rests on an unreadable pod.

    A running job whose session is a pod is live unless the pod is read as ``dead``: a pod that cannot be read
    counts as live (fail closed) and is flagged ``pod_unknown`` so the caller can count it. A job never has both
    a pid and a pod; a state file that does (a stale key, a hand edit) is live when either one is, so a pod read
    as dead never hides a process group that is still alive.
    """
    pod = job.get("pod") if status == "running" else None
    if pod is not None:
        pod_state = podhandle.pod_status(pod, pod_reader)
        if pod_state != podhandle.DEAD or not job.get("pid"):
            return pod_state != podhandle.DEAD, pod_state == podhandle.UNKNOWN
    live = status == "running" and (
        # A pid recorded on another host is "unknown", not a live local worker.
        pid_status(job["pid"], job.get("pid_host")) == "alive"
        if job.get("pid")
        else bool(job.get("worker_phase"))
    )
    return live, False


def _waits(key, specification, job, status, planned, states, active):
    """What a job is waiting for (dependencies, shared paths, a worker slot, capacity), as short phrases."""
    waits = []
    if status == "queued":
        dependencies = [
            dependency
            for dependency in specification.get("depends_on", [])
            if states.get(dependency, {}).get("status") != "merged"
        ]
        if dependencies:
            waits.append(f"dependency merges: {', '.join(dependencies)}")
        conflicts = [
            other
            for other in active
            if other != key
            and (job.get("resume_phase") or "build") == "build"
            and (
                active[other].get("worker_phase")
                or active[other].get("phase")
                or "unknown"
            ).split("-", 1)[0]
            == "build"
            and overlaps(
                specification.get("likely_paths", []),
                planned.get(other, {}).get("likely_paths", []),
            )
        ]
        siblings = (
            shared_path_blockers(key, planned, states)
            if (job.get("resume_phase") or "build") == "build"
            else []
        )
        if siblings:
            waits.append(f"shared paths: {siblings[0]}")
        elif conflicts:
            waits.append(f"shared paths: {', '.join(conflicts)}")
    if job.get("waiting_for") and str(job["waiting_for"]) not in waits:
        waits.append(str(job["waiting_for"]))
    elif status.startswith("waiting_"):
        waits.append(f"{status.removeprefix('waiting_')} worker slot")
    if status == "queued" and not waits:
        waits.append("scheduler capacity")
    return waits


def _next_action(job, row):
    """``(next_action, owner)`` for a job, from its state and the report row built so far (read only)."""
    key, status, phase, live = row["id"], row["status"], row["phase"], row["active_worker"]
    blockers, waits, handoff = row["blockers"], row["waits"], row["handoff_status"]
    timeout_seconds = row["timeout_seconds"]
    heads_match = row["review_matches_head"] and row["acceptance_matches_head"]
    if blockers:
        return blockers[0]["remedy"], blockers[0]["owner"]
    if status == "merged":
        return "Merged by GitHub; nothing left for Factory", "none"
    if status == "closed":
        return "Inspect the closed PR's disposition", "operator"
    if status == "needs_human_publish":
        return (
            "Review and verification passed, but the Factory App cannot push workflow "
            f"files: {publish_hint(key, job.get('human_publish'))}",
            "operator",
        )
    if status == "stopped":
        return (
            "Stopped by an operator; the pull request and branch were left as they are. "
            f"Run retry {key} --reason ... to build again, or resume a saved phase",
            "operator",
        )
    if status == "running":
        if live:
            return f"Continue {phase}", f"{phase} worker"
        return "Inspect supervisor and child process before recovery", "operator"
    if status == "failed" and timeout_seconds is not None:
        return (
            f"An agent session timed out after {timeout_seconds} s. Raise {TIMEOUT_KEY} for this repository "
            f"(60 to 86400) if the work is one coherent change, or split the issue into smaller ones; "
            f"then retry {key}",
            "operator",
        )
    if waits:
        return f"Wait for {'; '.join(waits)}", job.get("waiting_owner", "scheduler")
    if not row["review_passed"]:
        return "Resolve findings or resume independent review", "builder/reviewer"
    if not row["feature_acceptance_complete"]:
        return "Prepare missing capability and resume verification", "verifier/operator"
    if status == "pr_ready":
        if handoff == "ci_failed":
            return _ci_failed_action(key, row["ci_basis"]), "operator"
        if heads_match:
            return (
                "Waiting for CI and the merge rules; GitHub merges when the required "
                "checks and factory/gate pass",
                "GitHub",
            )
        return "Validate retained review and verification against the current head", "GitHub"
    if status == "pr_incomplete" and heads_match and recorded_hold(job):
        # Held on purpose: a person pushed (or may have pushed) workflow files, so a person
        # reads their diff before the gate passes.
        return (
            "Review and verification passed; the PR is a draft because it changes workflow "
            "files. Read the diff of every one, then apply the factory-verified label (it "
            "passes the gate), mark the PR ready and apply factory:ready",
            "operator",
        )
    if status == "pr_incomplete" and heads_match:
        # Review and verification both passed on this head: what holds the PR back is a ci
        # criterion no required check covers, not an unfinished phase.
        return (
            "Review and verification passed; the PR is a draft because criteria proven "
            "elsewhere (ci) are outstanding. Read 'Outstanding criteria' in "
            "the PR, prove them, then merge by hand",
            "operator",
        )
    return "Inspect retained evidence and resume the incomplete phase", "operator"


def _ci_failed_action(key, ci_basis):
    """The next action of a ready PR whose CI failed; the wording follows how the failure was judged."""
    if ci_basis == "rollup_no_required":
        failure = "A check failed on the PR head (no required checks are configured; any failing check counts)"
        ending = "; fix it before merging"
    elif ci_basis == "rollup_fallback":
        failure = "A check failed on the PR head (required checks unreadable; judged from all checks)"
        ending = ", so GitHub will not merge it"
    else:
        failure = "A required check failed on the PR head"
        ending = ", so GitHub will not merge it"
    return (
        f"{failure}{ending}. Diagnose, then "
        f"retry {key} --reason with the diagnosis, or fix manually (a new head "
        "invalidates the factory/gate status)"
    )


def status_report(
    plan, data, now=None, planner_usage=None, pr_snapshots=None, refresh_failures=None, pod_reader=None
):
    """Do not instantiate State: viewing a missing/old run must not write it.

    ``pr_snapshots`` maps job ids to a live ``gh pr view`` result. It only
    overlays the report (a merged or closed PR shows as such before the next
    ``run`` reconciles it); nothing is persisted here. ``refresh_failures`` lists the
    lookups that failed (``github.lookup_failure``): the report says so, and an affected
    job's ``next_action`` says its information may be stale.

    A running job whose session is a pod is a live worker unless the pod is read as ``dead`` (``pod_reader``,
    default the installed one, ``podhandle``): a pod that cannot be read counts as live too, and is also counted
    in ``active_worker_pods_unknown``, so that a gate can treat "cannot read" as busy.
    """
    pr_snapshots = pr_snapshots or {}
    failed = {}
    for failure in refresh_failures or ():
        failed.setdefault(failure["job"], failure)
    now = _now(now)
    states = data.get("jobs", {})
    planned = {job["id"]: job for job in plan.get("jobs", [])}
    active = {key: job for key, job in states.items() if job.get("status") == "running"}
    workers = {}
    pods_unknown = 0
    rows = []
    for key, specification in planned.items():
        job = states.get(key, {})
        status = job.get("status", "not_started")
        phase = _phase(job)
        live, pod_unknown = _worker_liveness(job, status, pod_reader)
        pods_unknown += pod_unknown
        if live:
            workers[phase] = workers.get(phase, 0) + 1
        blockers = [
            deepcopy(blocker)
            for blocker in data.get("blockers", {}).values()
            if blocker.get("status", "open") == "open"
            and key in blocker.get("affected_jobs", [])
        ]
        waits = _waits(key, specification, job, status, planned, states, active)
        verification = job.get("verification") or {}
        review = job.get("review") or {}
        exact = job.get("commit")
        review_passed = review.get("pass") is True
        review_matches = bool(exact and job.get("reviewed_commit") == exact)
        acceptance_complete = verification.get("complete") is True
        acceptance_matches = bool(exact and verification.get("commit") == exact)
        error = job.get("error") or verification.get("blocker")
        if not error and review.get("pass") is False and review.get("findings"):
            error = "; ".join(str(finding) for finding in review["findings"])
        # The fixed reason of a session that ran out of time (``session_timeout``), never a guess from text.
        timeout_seconds = timeout_seconds_of(error)
        snapshot = pr_snapshots.get(key)
        handoff = None
        ci_basis = None
        other_failing = []
        if status == "merged":
            handoff = "merged"
        elif status == "closed":
            handoff = "closed"
        elif snapshot:
            handoff = pr_handoff_status(snapshot)
            if handoff in {"published", "ci_failed"}:
                assessment = ci_assessment(snapshot)
                ci_basis = assessment["basis"]
                other_failing = assessment["informational"]
            if handoff in {"merged", "closed"} and status in {
                "pr_ready",
                "pr_incomplete",
            }:
                status = handoff
        elif job.get("pr"):
            handoff = "published"
        phase_seconds = deepcopy(job.get("phase_seconds", {}))
        current_seconds = (
            elapsed(job.get("phase_started_at"), now) if status == "running" else None
        )
        if current_seconds is not None:
            current_phase = (
                job.get("worker_phase")
                or job.get("pending_phase")
                or job.get("phase")
                or "unknown"
            )
            phase_seconds[current_phase] = (
                phase_seconds.get(current_phase, 0) + current_seconds
            )
        reason_code = job_reason_code(job, status, handoff, snapshot)
        skipped = verification_skipped(verification)
        proofs = criteria_report(
            specification,
            snapshot,
            acceptance_complete and acceptance_matches,
            skipped=skipped,
            published_required=job.get("published_required"),
        )
        # Paths the Factory App cannot push (workflow files), from the plan and from what the
        # build changed. Known before the first build; moot once a pull request exists.
        unpushable = publish_blocked_paths(specification, job)
        needs_publish = status == "needs_human_publish" or (
            bool(unpushable)
            and status not in {"pr_ready", "pr_incomplete", "merged", "closed"}
        )
        row = {
            "id": key,
            "status": status,
            "phase": phase,
            "active_worker": live,
            "pid": job.get("pid"),
            "age_seconds": elapsed(job.get("created_at"), now),
            "status_age_seconds": elapsed(job.get("status_started_at"), now),
            "phase_age_seconds": current_seconds,
            "phase_seconds": phase_seconds,
            "attempt": job.get("attempt", 0),
            "retries": max(0, job.get("attempt", 0) - 1),
            "ci_repair": deepcopy(job.get("ci_repair")),
            "waits": waits,
            "blockers": blockers,
            "error": error,
            "failure": "timeout" if timeout_seconds is not None else None,
            # The closed code of the API error behind a failed session (``apierror``) and when it was seen,
            # for a failed job only: the manager's stop-the-world breaker reads them (``stopworld``).
            **api_error_of(job, status),
            "timeout_seconds": timeout_seconds,
            "next_action": None,  # derived from the other fields below
            "owner": None,
            "commit": exact,
            "pr": job.get("pr"),
            "cost_usd": cost_usd(job.get("usage")),
            "review_passed": review_passed,
            "review_matches_head": review_matches,
            "feature_acceptance_complete": acceptance_complete,
            "acceptance_matches_head": acceptance_matches,
            "handoff_status": handoff,
            "reason_code": reason_code,
            "criteria": proofs,
            # A complete verification that ran nothing (verify.when): its sandbox criteria
            # can never become evidence, so they are shown as unproven, not pending.
            "verification_skipped": skipped,
            # How many items the PR's "Needs a person" checklist has: they never hold the
            # job back, so this is the one place a reviewer sees that a person still has some.
            "needs_a_person": sum(1 for item in proofs if item["tag"] == "operator"),
            "ci_basis": ci_basis,
            "other_failing_checks": other_failing,
            "needs_human_publish": needs_publish,
            "workflow_paths": unpushable[:MAX_SHOWN] if needs_publish else [],
            "push_rejection": job.get("push_rejection"),
            "push_error": job.get("push_error"),
            # The recorded model and effort of the build and verify phases with their sources
            # (model_choice), or None for a job planned before they were recorded.
            "models": recorded_choice(specification),
        }
        row["next_action"], row["owner"] = _next_action(job, row)
        if key in failed:
            row["next_action"] = (
                f"{row['next_action']} (live PR lookup failed: {failed[key]['reason']}; "
                "this information may be stale)"
            )
        rows.append(row)
    planner_cost = cost_usd([planner_usage] if planner_usage else [])
    return {
        "observed_at": now,
        "active_workers": workers,
        "active_worker_count": sum(workers.values()),
        "active_worker_pods_unknown": pods_unknown,
        "planner_cost_usd": planner_cost,
        "cost_usd": round(sum(row["cost_usd"] for row in rows) + planner_cost, 6),
        "footprint": footprint_summary(states.values()),
        "tokens": token_summary(states.values(), planner_usage),
        "jobs": rows,
        "live_refresh": {
            "ok": not failed,
            "failed": [deepcopy(failure) for failure in failed.values()],
        },
        "blockers": deepcopy(data.get("blockers", {})),
        "capabilities": deepcopy(data.get("capabilities", {})),
    }


def api_error_of(job, status):
    """``{"api_error", "api_error_at"}`` of a failed job: the closed code and its time, else both None."""
    code, at = job.get("api_error"), job.get("api_error_at")
    if status == "failed" and isinstance(code, str) and code in apierror.CODES and code != apierror.NONE:
        return {"api_error": code, "api_error_at": at if isinstance(at, str) else None}
    return {"api_error": None, "api_error_at": None}


def format_status(report):
    phases = ", ".join(
        f"{phase}={count}" for phase, count in sorted(report["active_workers"].items())
    )
    lines = [
        f"Active workers: {report['active_worker_count']}"
        + (f" ({phases})" if phases else "")
        + f" | cost=${report['cost_usd']:.2f}"
    ]
    unreadable = report.get("active_worker_pods_unknown")
    if unreadable:
        lines.append(
            f"Pod workers that cannot be read: {unreadable} (counted as running; "
            "nothing may start on their worktrees until the launcher answers)"
        )
    breaker = report.get("breaker")
    if breaker:
        since = f" since {breaker['at']}" if breaker.get("at") else ""
        lines.append(
            f"Stop-the-world breaker: tripped ({breaker.get('code') or 'unknown'}){since}; "
            "the trigger is paused until an Operator clears it (docs/operations.md)"
        )
    pod = report.get("capacity")
    if pod:
        waiting = f", {len(pod['waiting'])} waiting" if pod.get("waiting") else ""
        lines.append(f"Pod worker budget: {pod['in_use']}/{pod['total']} workers in use{waiting}")
    supervisor = report.get("supervisor")
    if supervisor:
        lines.insert(
            0,
            f"Supervisor: {supervisor['status']} | PID={supervisor['pid']} | drain requested={'yes' if supervisor.get('drain_requested_at') else 'no'}",
        )
    refresh = report.get("live_refresh") or {}
    if refresh.get("failed"):
        lines.append(
            "Warning: live PR lookup failed for "
            + ", ".join(failure["job"] for failure in refresh["failed"])
            + "; those jobs may show stale information"
        )
    for job in report["jobs"]:
        age = "unknown" if job["age_seconds"] is None else f"{int(job['age_seconds'])}s"
        lines.append(
            f"{job['id']}: {job['status']} | phase={job['phase']} | age={age} | attempts={job['attempt']} | cost=${job['cost_usd']:.2f}"
        )
        if job.get("models"):
            shown = "; ".join(f"{phase} {describe(entry)}" for phase, entry in job["models"].items())
            lines.append(f"  Models: {shown}")
        lines.append(
            f"  Review={'passed' if job['review_passed'] else 'incomplete'} (head={'matched' if job['review_matches_head'] else 'unconfirmed'}); acceptance={'complete' if job['feature_acceptance_complete'] else 'incomplete'} (head={'matched' if job['acceptance_matches_head'] else 'unconfirmed'}); handoff={job['handoff_status'] or 'none'}"
        )
        if job["pr"]:
            lines.append(f"  PR: {job['pr']}")
        if job.get("reason_code"):
            lines.append(f"  Reason code: {job['reason_code']}")
        if job.get("api_error"):
            lines.append(f"  API error: {job['api_error']}")
        if job.get("needs_a_person"):
            count = job["needs_a_person"]
            lines.append(
                f"  Needs a person: {count} operator {'criterion' if count == 1 else 'criteria'} "
                "in the PR checklist (a person checks them; they do not hold the PR back)"
            )
        # Said only where it explains something: a sandbox row that stays unproven because of it.
        if job.get("verification_skipped") and any(
            item["tag"] == "sandbox" and item["proof"] == "unproven" for item in job.get("criteria", [])
        ):
            lines.append(
                "  Verification: not run for this job (verify.when); "
                "sandbox criteria are unproven, not satisfied"
            )
        for item in job.get("criteria", []):
            names = []
            if item["checks"]:
                names.append("checks: " + ", ".join(item["checks"]))
            if item.get("workflow"):
                names.append("workflow: " + item["workflow"])
            lines.append(
                f"  Criterion [{item['tag']}] {item['proof']}: {_one_line(item['text'])}"
                + (f" ({'; '.join(names)})" if names else "")
            )
            if item.get("required_sources"):
                lines.append(
                    "    Required checks disagree: at publish the named checks were not confirmed as required "
                    "on the base branch, the PR lists them as required; unproven until a new publish agrees"
                )
        if job.get("ci_basis") == "rollup_fallback":
            lines.append(
                "  CI: required checks could not be read; fell back to all checks in the rollup"
            )
        if job.get("ci_basis") == "rollup_no_required":
            lines.append(
                "  CI: no required checks are configured; any failing check counts"
            )
        if job.get("other_failing_checks"):
            lines.append(
                "  Other failing checks (not required, informational): "
                + ", ".join(job["other_failing_checks"])
            )
        for blocker in job["blockers"]:
            lines.append(f"  Blocker {blocker['id']}: {blocker['cause']}")
        if job.get("needs_human_publish"):
            names = ", ".join(_one_line(path) for path in job.get("workflow_paths", []))
            listed = f"; paths: {names}" if names else ""
            lines.append(
                "  Workflow files: the Factory App cannot push them, so a person must publish "
                f"this job's branch (review and verification still run){listed}"
            )
        if job["error"]:
            lines.append(f"  Detail: {job['error']}")
        if job.get("push_error"):
            lines.append(f"  Push refused: {one_line(job['push_error'])}")
        lines.append(f"  Next: {job['next_action']} (owner: {job['owner']})")
    return "\n".join(lines)

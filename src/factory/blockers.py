"""Validated operator observations; never promote review, acceptance, or merge gates."""

from copy import deepcopy
from datetime import datetime

from .state import timestamp


def _text(record, field):
    if not isinstance(record.get(field), str) or not record[field].strip():
        raise ValueError(f"{field} must be a nonempty string")


def _evidence(record, required=True):
    evidence = record.get("evidence")
    if not isinstance(evidence, list) or any(
        not isinstance(item, str) or not item.strip() for item in evidence
    ):
        raise ValueError("evidence must be a list of nonempty references")
    if required and not evidence:
        raise ValueError("evidence is required for confirmed observations")


def _save_record(state, collection, key, record):
    records = state.data.setdefault(collection, {})
    previous = records.get(key)
    if (
        previous
        and all(previous.get(k) == v for k, v in record.items())
        and all(
            k in record or k in {"created_at", "updated_at", "history"}
            for k in previous
        )
    ):
        return deepcopy(previous)
    now = timestamp()
    value = deepcopy(record)
    value["created_at"] = previous["created_at"] if previous else now
    value["updated_at"] = now
    value["history"] = deepcopy(previous.get("history", [])) if previous else []
    if previous:
        value["history"].append({k: v for k, v in previous.items() if k != "history"})
    records[key] = value
    state.save()
    return deepcopy(value)


def record_blocker(state, record):
    """Upsert an explicitly identified shared diagnosis without changing jobs."""
    if not isinstance(record, dict):
        raise ValueError("blocker must be an object")
    allowed = {
        "id",
        "affected_jobs",
        "cause",
        "evidence",
        "remedy",
        "owner",
        "status",
        "commit",
        "phase",
    }
    if set(record) - allowed:
        raise ValueError("unrecognized blocker fields")
    record = deepcopy(record)
    record.setdefault("status", "open")
    for field in ("id", "cause", "remedy", "owner"):
        _text(record, field)
    _evidence(record)
    if record["status"] not in {"open", "resolved"}:
        raise ValueError("blocker status must be open or resolved")
    jobs = record.get("affected_jobs")
    if (
        not isinstance(jobs, list)
        or not jobs
        or any(
            not isinstance(key, str) or key not in state.data["jobs"] for key in jobs
        )
    ):
        raise ValueError("affected_jobs must identify known jobs")
    record["affected_jobs"] = sorted(set(jobs))
    for field in ("commit", "phase"):
        if field in record:
            _text(record, field)
    with state.mutex:
        return _save_record(state, "blockers", record["id"], record)


def record_capabilities(state, records):
    """Store tested capabilities per environment; unknown fixtures stay unconfirmed."""
    if not isinstance(records, list):
        raise ValueError("capabilities must be a list")
    validated = []
    allowed = {
        "id",
        "environment",
        "status",
        "tested_at",
        "evidence",
        "owner",
        "remedy",
    }
    for source in records:
        if not isinstance(source, dict) or set(source) - allowed:
            raise ValueError("invalid capability fields")
        record = deepcopy(source)
        for field in ("id", "environment"):
            _text(record, field)
        if record.get("status") not in {"available", "unavailable", "unconfirmed"}:
            raise ValueError("invalid capability status")
        confirmed = record["status"] != "unconfirmed"
        _evidence(record, required=confirmed)
        tested = record.get("tested_at")
        if confirmed or tested is not None:
            _text(record, "tested_at")
            try:
                value = datetime.fromisoformat(tested.replace("Z", "+00:00"))
                if value.tzinfo is None:
                    raise ValueError("missing timezone")
            except ValueError as error:
                raise ValueError(
                    "tested_at must be an ISO timestamp with timezone"
                ) from error
        for field in ("owner", "remedy"):
            if field in record:
                _text(record, field)
        validated.append(record)
    with state.mutex:
        return [
            _save_record(state, "capabilities", f"{r['environment']}::{r['id']}", r)
            for r in validated
        ]

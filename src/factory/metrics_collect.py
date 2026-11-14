"""What a scrape reads: the manager's live state, the history ledger and the retained runs (docs/metrics.md).

``Collector.collect`` runs on every ``GET /metrics`` and fills the gauges and the ledger-derived counters of
``manager.metrics``. It starts no process, runs no ``factory`` command, makes no GitHub call and mints no token:

* live state is read from memory (children, the pause marker, the GitHub health table, the false-incomplete
  cache, the watcher's backoff) plus two cheap reads of the host (the slot files of the worker budget and the
  disk usage of ``FACTORY_DATA``);
* the history ledger is read through ``Dashboard.ledger_runs`` (a bounded read, ``history.DEFAULT_READ_BYTES``,
  folded once per change of a segment) and aggregated at most every ``LEDGER_TTL_SECONDS``;
* the retained runs are read from their ``state.json`` (at most ``MAX_RETAINED_RUNS`` per refresh, every
  ``RUNS_TTL_SECONDS`` at most), as gauges.

A part that fails is counted in ``factory_metrics_scrape_errors_total`` and the rest still answers. Only
numbers and closed words leave this module: a job status or phase that is not in the closed set is
``other``, and no id, title, branch or login is read into a label.

Stdlib only.
"""

import shutil
import threading
import time
from dataclasses import dataclass, field

from . import capacity, history, metrics, phases, planner, podcontract
from .config import data_paths, load_repo_config
from .dashboard import read_json
from .github_health import AMBIENT, CHECKING, MISSING_PREFIX, OK
from .manager import _list_runs
from .operations import policy_of
from .status import TOKEN_FIELDS, cost_usd, footprint_summary

LEDGER_TTL_SECONDS = 30.0
RUNS_TTL_SECONDS = 30.0
MAX_RETAINED_RUNS = 200  # state.json files read per refresh, over all repositories
MAX_RUNS_PER_REPO = 50
# A stretch of one phase longer than this is not a phase time: the manager was down in the middle of it.
MAX_PHASE_SECONDS = 86400.0
# The counter each token count feeds: one metric per count, so the labels stay ``repo`` and ``phase`` only.
TOKEN_METRICS = {
    "input_tokens": "factory_input_tokens_total",
    "output_tokens": "factory_output_tokens_total",
    "cache_creation_input_tokens": "factory_cache_creation_input_tokens_total",
    "cache_read_input_tokens": "factory_cache_read_input_tokens_total",
}


def github_state(text):
    """The closed state of a ``GithubHealth.status`` text (its free-text tail is never kept)."""
    status = str(text)
    if status in (OK, AMBIENT, CHECKING):
        return status
    if status.startswith(MISSING_PREFIX):
        return "permission_missing"
    if status.startswith("unavailable"):
        return "unavailable"
    return metrics.OTHER


# --- the ledger, aggregated ----------------------------------------------------------------


@dataclass
class LedgerTotals:
    """Totals over the runs of a folded ledger, per repository (``history.fold`` in, plain numbers out)."""

    runs: dict = field(default_factory=dict)  # (repo, outcome) -> runs
    jobs: dict = field(default_factory=dict)  # (repo, outcome) -> jobs
    reasons: dict = field(default_factory=dict)  # (repo, reason code) -> jobs
    purges: dict = field(default_factory=dict)  # (repo, purge reason) -> runs
    cost: dict = field(default_factory=dict)  # repo -> USD
    tokens: dict = field(default_factory=dict)  # (repo, phase, token field name) -> count
    lead: dict = field(default_factory=dict)  # repo -> [seconds from plan to first pull request]
    phase: dict = field(default_factory=dict)  # (repo, phase) -> [seconds]


def _add(table, key, amount=1):
    table[key] = table.get(key, 0) + amount


def _add_tokens(totals, repo, phase, counts):
    """Add one row of token counts (``{name: count}``, any name outside ``TOKEN_FIELDS`` is not counted)."""
    for name in TOKEN_FIELDS:
        value = (counts or {}).get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            _add(totals.tokens, (repo, phase, name), value)


def phase_segments(job):
    """``[(phase, seconds)]`` of a folded job: each stretch in which it was ``running`` in one phase.

    A stretch runs from its first event to the next event of the job (a change of phase or status). One that
    is still open has no end yet and is not counted; one longer than ``MAX_PHASE_SECONDS`` is dropped.
    """
    segments = []
    start = phase = None
    for entry in job.get("timeline") or ():
        at = history.epoch_of(entry.get("at"))
        if at is None:
            continue
        running = entry.get("phase") if entry.get("status") == "running" else None
        if start is not None and running != phase:
            seconds = at - start
            if 0 <= seconds <= MAX_PHASE_SECONDS:
                segments.append((phase, seconds))
            start = phase = None
        if running is not None and start is None:
            start, phase = at, running
    return segments


def lead_seconds(run, job):
    """Seconds from the run being planned (else the job's first event) to the job's first pull request, or None."""
    start = history.epoch_of(run.get("planned_at")) or history.epoch_of(job.get("first_at"))
    if start is None:
        return None
    for entry in job.get("timeline") or ():
        if entry.get("pr"):
            end = history.epoch_of(entry.get("at"))
            return end - start if end is not None and end >= start else None
    return None


def aggregate_ledger(runs):
    """``LedgerTotals`` over ``history.fold`` output (``{(repo, run): summary}``)."""
    totals = LedgerTotals()
    for run in runs.values():
        repo = run["repo"]
        if run["outcome"] is not None:
            _add(totals.runs, (repo, run["outcome"]))
        if run["purged_at"] is not None:
            _add(totals.purges, (repo, run["purge_reason"] or "manual"))
        _add(totals.cost, repo, run["cost_usd"])
        # The planner's counts belong to the plan phase; a job's are kept by the phase of each session.
        _add_tokens(totals, repo, "plan", run.get("planner_tokens"))
        for job in run["jobs"].values():
            for phase, counts in (job.get("tokens") or {}).items():
                _add_tokens(totals, repo, phase, counts)
            if job["outcome"] is not None:
                _add(totals.jobs, (repo, job["outcome"]))
            if job["reason_code"] is not None:
                _add(totals.reasons, (repo, job["reason_code"]))
            lead = lead_seconds(run, job)
            if lead is not None:
                totals.lead.setdefault(repo, []).append(lead)
            for phase, seconds in phase_segments(job):
                totals.phase.setdefault((repo, phase), []).append(seconds)
    return totals


# --- the collector -------------------------------------------------------------------------


class Collector:
    """Fills ``manager.metrics`` at scrape time. One scrape at a time; the aggregates are cached for a short while."""

    def __init__(self, manager, clock=time.monotonic):
        self.manager = manager
        self._clock = clock
        self._lock = threading.Lock()
        self._ledger = None  # (stamp, LedgerTotals, truncated)
        self._runs = None  # (stamp, gauge rows by metric name, truncated)

    @property
    def registry(self):
        return self.manager.metrics

    def collect(self):
        with self._lock:
            self.registry.forget_repos()  # a repository enrolled since the last scrape is a label value now
            for part in (self._live, self._from_ledger, self._from_runs):
                try:
                    part()
                except Exception:  # noqa: BLE001  (a part that fails costs its own numbers, never the scrape)
                    self.registry["factory_metrics_scrape_errors_total"].inc()

    def _fresh(self, held, ttl):
        return held is not None and self._clock() - held[0] < ttl

    # -- live state ---------------------------------------------------------------------

    def _live(self):
        manager, registry = self.manager, self.registry
        repos = manager.configured_repos()
        running = manager.children.running()
        registry["factory_manager_children"].replace(
            [({"kind": kind}, sum(1 for child in running if child.kind == kind)) for kind in metrics.CHILD_KINDS]
        )
        registry["factory_manager_commands_running"].set(manager.runner.live_count())
        registry["factory_trigger_paused"].set(1 if manager.trigger_paused() else 0)
        registry["factory_github_app_state"].replace(
            self._one_hot(repos, manager.github_health.status, github_state, metrics.GITHUB_STATES)
        )
        self._false_incomplete(repos)
        self._capacity()
        self._pods()
        self._disk()
        operations = manager.operations
        registry["factory_watcher_backoff_seconds"].replace(
            [({"repo": repo}, operations.backoff_remaining(repo)) for repo in repos] if operations is not None else []
        )

    @staticmethod
    def _state_rows(repo, current, states):
        """One row per state (``other`` included) of a repository: 1 for the state it is in, 0 for the rest."""
        known = (*states, metrics.OTHER)
        held = current if current in known else metrics.OTHER
        return [({"repo": repo, "state": state}, 1 if state == held else 0) for state in known]

    def _one_hot(self, repos, read, name_of, states):
        rows = []
        for repo in repos:
            rows.extend(self._state_rows(repo, name_of(read(repo)), states))
        return rows

    def _false_incomplete(self, repos):
        registry = self.registry
        states, counts, reasons, rates = [], [], [], []
        for repo in repos:
            value = self.manager.false_incomplete.get(repo)
            current = str(value.get("state"))
            states.extend(self._state_rows(repo, current, metrics.FALSE_INCOMPLETE_STATES))
            if current not in ("ok", "stale"):
                continue
            held = value.get("counts") if isinstance(value.get("counts"), dict) else {}
            counts.extend(({"repo": repo, "count": name}, held.get(name, 0)) for name in metrics.FALSE_INCOMPLETE_COUNTS)
            by_reason = value.get("by_reason") if isinstance(value.get("by_reason"), dict) else {}
            for code in registry.values["reason"][:-1]:
                row = by_reason.get(code)
                reasons.append(({"repo": repo, "reason": code}, row.get("counted", 0) if isinstance(row, dict) else 0))
            if value.get("rate") is not None:
                rates.append(({"repo": repo}, value["rate"]))
        registry["factory_false_incomplete_state"].replace(states)
        registry["factory_false_incomplete_prs"].replace(counts)
        registry["factory_false_incomplete_reason_prs"].replace(reasons)
        registry["factory_false_incomplete_rate"].replace(rates)

    def _capacity(self):
        snapshot = capacity.snapshot_from_env(self.manager.env)
        rows = []
        if snapshot:
            rows = [
                ({"part": "total"}, snapshot.get("total", 0)),
                ({"part": "in_use"}, snapshot.get("in_use", 0)),
                ({"part": "waiting"}, len(snapshot.get("waiting") or ())),
            ]
        self.registry["factory_capacity_workers"].replace(rows)

    def _pods(self):
        """The worker pods of the pod backend (``factory_worker_pods``): the sweeper's census, and the ceiling of the pool.

        ``factory_capacity_workers`` is the manager pod's own in-process budget and says nothing about pods: with
        ``execution = "pod"`` the workers are pods, so this is the number of workers that run. Empty without a pod
        sweeper (the backend is off) and while the census cannot be read (``unknown``: never a zero for "cannot read").
        """
        rows = []
        census = self.manager.worker_pods()
        if isinstance(census, dict) and not census.get("unknown"):
            rows = [({"part": "live"}, census.get("live", 0)), ({"part": "final"}, census.get("final", 0))]
            ceiling = str(self.manager.env.get(podcontract.ENV_MANAGER["max_pod_sessions"], "")).strip()
            if ceiling.isdigit() and 0 < int(ceiling) < 10_000:
                rows.append(({"part": "ceiling"}, int(ceiling)))
        self.registry["factory_worker_pods"].replace(rows)

    def _disk(self):
        try:
            usage = shutil.disk_usage(self.manager.settings.data_dir)
        except OSError:
            self.registry["factory_data_disk_bytes"].replace([])
            return
        self.registry["factory_data_disk_bytes"].replace(
            [({"part": "total"}, usage.total), ({"part": "used"}, usage.used), ({"part": "free"}, usage.free)]
        )

    # -- the history ledger ---------------------------------------------------------------

    def _from_ledger(self):
        registry, manager = self.registry, self.manager
        if not self._fresh(self._ledger, LEDGER_TTL_SECONDS):
            runs, truncated = manager.dashboard.ledger_runs()
            self._ledger = (self._clock(), aggregate_ledger(runs), truncated)
        _, totals, truncated = self._ledger
        outcomes, reasons = registry.values["outcome"][:-1], registry.values["reason"][:-1]
        purge_reasons = registry.values["purge_reason"][:-1]
        for repo in manager.configured_repos():
            for outcome in outcomes:
                registry["factory_runs_finished_total"].track(totals.runs.get((repo, outcome), 0), repo=repo, outcome=outcome)
                registry["factory_jobs_finished_total"].track(totals.jobs.get((repo, outcome), 0), repo=repo, outcome=outcome)
            for code in reasons:
                registry["factory_job_reason_codes_total"].track(totals.reasons.get((repo, code), 0), repo=repo, reason=code)
            for why in purge_reasons:
                registry["factory_runs_purged_total"].track(totals.purges.get((repo, why), 0), repo=repo, purge_reason=why)
            registry["factory_cost_usd_total"].track(round(totals.cost.get(repo, 0.0), 6), repo=repo)
            for phase in phases.PHASES:
                for name, metric in TOKEN_METRICS.items():
                    registry[metric].track(totals.tokens.get((repo, phase, name), 0), repo=repo, phase=phase)
            if totals.lead.get(repo):
                registry["factory_plan_to_pr_seconds"].track(totals.lead[repo], repo=repo)
            for phase in registry.values["phase"][:-1]:
                if totals.phase.get((repo, phase)):
                    registry["factory_phase_seconds"].track(totals.phase[(repo, phase)], repo=repo, phase=phase)
        registry["factory_history_bytes"].set(manager.ledger.describe()["bytes"])
        registry["factory_history_truncated"].set(1 if truncated else 0)

    # -- the retained runs ----------------------------------------------------------------

    def _from_runs(self):
        if not self._fresh(self._runs, RUNS_TTL_SECONDS):
            self._runs = (self._clock(), *self._read_runs())
        _, rows, truncated = self._runs
        registry = self.registry
        for name, held in rows.items():
            registry[name].replace(held)
        registry["factory_retained_runs_truncated"].set(1 if truncated else 0)

    def _read_runs(self):
        """(gauge rows by metric name, whether a cap left runs unread) from the ``state.json`` of retained runs."""
        manager, registry = self.manager, self.registry
        phases_known, statuses = registry.values["phase"], registry.values["status"]
        rows = {
            name: []
            for name in (
                "factory_jobs",
                "factory_retained_cost_usd",
                "factory_run_budget_usd",
                "factory_run_spent_usd_max",
                "factory_phase_peak_rss_mib",
                "factory_phase_p95_rss_mib",
                "factory_retained_runs",
            )
        }
        room = MAX_RETAINED_RUNS
        truncated = False
        for repo in manager.configured_repos():
            try:
                cfg = load_repo_config(repo, config_dir=manager.settings.config_dir)
            except ValueError:
                continue
            paths = data_paths(cfg, data_dir=manager.settings.data_dir)
            budget = policy_of(cfg).run_budget_usd
            if budget is not None:
                rows["factory_run_budget_usd"].append(({"repo": repo}, budget))
            entries = _list_runs(paths)
            wanted = entries[:MAX_RUNS_PER_REPO]
            truncated = truncated or len(entries) > len(wanted) or len(wanted) > room
            jobs_by, cost_by, footprints, states = {}, {}, [], 0
            spent_max = None
            for entry in wanted[: max(0, room)]:
                spent, jobs = self._run_figures(paths.run_dir(entry["run"]), entry, phases_known, statuses, jobs_by, cost_by)
                states += 1
                footprints.extend(jobs)
                spent_max = spent if spent_max is None else max(spent_max, spent)
            room -= states
            rows["factory_retained_runs"].append(({"repo": repo}, states))
            rows["factory_jobs"].extend(({"repo": repo, "status": s, "phase": p}, n) for (s, p), n in jobs_by.items())
            rows["factory_retained_cost_usd"].extend(({"repo": repo, "phase": p}, round(c, 6)) for p, c in cost_by.items())
            if spent_max is not None:
                rows["factory_run_spent_usd_max"].append(({"repo": repo}, round(spent_max, 6)))
            self._footprint_rows(repo, footprints, phases_known, rows)
        return rows, truncated

    @staticmethod
    def _run_figures(run_dir, entry, phases_known, statuses, jobs_by, cost_by):
        """Fold one retained run into ``jobs_by`` (per status and phase) and ``cost_by`` (per phase).

        Returns ``(what the run spent, its job records)``; the spend adds the usage records in order, so the sum is
        the same one a flat loop over them would give.
        """
        spent = cost_usd(planner.planner_usage_records(run_dir))
        state = read_json(run_dir / "state.json") if entry["started"] else None
        held = state.get("jobs") if state else None
        jobs = [job for job in held.values() if isinstance(job, dict)] if isinstance(held, dict) else []
        for job in jobs:
            status = job.get("status") if job.get("status") in statuses else metrics.OTHER
            phase = job.get("worker_phase") or job.get("phase") or metrics.NONE
            _add(jobs_by, (status, phase if phase in phases_known else metrics.OTHER))
            for record in [item for item in job.get("usage") or () if isinstance(item, dict)]:
                kind = record.get("phase") if record.get("phase") in phases_known else metrics.OTHER
                amount = cost_usd([record])
                _add(cost_by, kind, amount)
                spent += amount
        return spent, jobs

    @staticmethod
    def _footprint_rows(repo, jobs, phases_known, rows):
        peak, p95 = {}, {}
        for phase, found in footprint_summary(jobs).items():
            phase = phase if phase in phases_known else metrics.OTHER
            peak[phase] = max(peak.get(phase, 0), found["peak_rss_mb"])
            p95[phase] = max(p95.get(phase, 0), found["p95_rss_mb"])
        rows["factory_phase_peak_rss_mib"].extend(({"repo": repo, "phase": p}, v) for p, v in peak.items())
        rows["factory_phase_p95_rss_mib"].extend(({"repo": repo, "phase": p}, v) for p, v in p95.items())

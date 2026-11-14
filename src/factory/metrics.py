"""Prometheus metrics: a small registry, a declared catalogue and the text exposition (docs/metrics.md).

Numbers only. The catalogue below is the whole of what this process can expose: a metric that is not
declared in ``CATALOGUE`` cannot be created, a label that is not in ``LABEL_NAMES`` cannot be declared,
and every label except ``repo`` has a closed set of values (``LABEL_VALUES``): a value outside the set is
counted as ``other``, never minted as a new series. ``repo`` is checked against the configured
repositories (``Registry(repos=...)``); an observation for any other name is dropped. Issue or pull
request text, comments, branch names, job ids, logins and tokens are not label values and cannot become
one, whatever a caller passes in.

Three kinds of source feed the registry (``Spec.source``):

* ``live``: counters and gauges of this process, set where the event happens. They start from zero when
  the manager restarts (Prometheus ``rate()`` and ``increase()`` treat that as a counter reset).
* ``ledger``: totals worked out at scrape time from the history ledger (``history.fold``), which outlives
  ``purge``. ``Counter.track`` and ``Histogram.track`` keep such a series from ever going down inside
  one process, whatever happens to the window the ledger was read through.
* ``runs``: gauges over the run directories that still exist. They fall when the janitor purges a run,
  so they are gauges and are named as gauges (never ``_total``).

``render`` produces the text format 0.0.4. The HTTP listener at the end of the module answers
``GET /metrics`` and nothing else; it is a separate port from the manager API (``manager.py``), has no
Entra check (a scraper has no Entra token), and writes nothing to the audit sink. With a ``token`` it
answers ``401`` unless the request carries ``Authorization: Bearer <token>`` (compared in constant time
and never written anywhere); ``manager.py`` makes the token mandatory off the loopback.

Stdlib only.
"""

import hashlib
import hmac
import math
import socket
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import apierror, history, phases, plan_failure, reason_codes, stopworld

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
METRICS_PATH = "/metrics"
OTHER = "other"
NONE = "none"
MAX_SERIES = 2000  # per metric: a bug that mints labels costs memory up to this, never more
REPO_TTL_SECONDS = 2.0
MAX_CONNECTIONS = 8
HEADER_TIMEOUT_SECONDS = 5.0
MAX_CREDENTIAL_CHARS = 1024  # a longer Authorization value is refused before it is hashed
PREFIX = "factory_"

LIVE = "live"
LEDGER = "ledger"
RUNS = "runs"
SOURCES = (LIVE, LEDGER, RUNS)
COUNTER, GAUGE, HISTOGRAM = "counter", "gauge", "histogram"

# --- label names and their closed value sets --------------------------------------------

# The only label names a metric may declare. Adding one is a reviewed change to this file and to the test
# that enumerates every label.
LABEL_NAMES = frozenset(
    {
        "repo",
        "phase",
        "status",
        "state",
        "reason",
        "skip_reason",
        "purge_reason",
        "route",
        "method",
        "status_class",
        "http_status",
        "kind",
        "outcome",
        "part",
        "count",
        "code",
        "plan_failure_code",
    }
)

# Raw job statuses (operations.JOB_STATUSES; a test keeps the two equal).
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
# Why the label watcher left a repository or an issue alone (operations.Operations._skip and trigger.classify).
SKIP_REASONS = (
    "disabled",
    "backoff",
    "busy",
    "ignored",
    "in-flight",
    "unauthorised",
    "config",
    "archived",
    "unremoved",
    "draining",
    "run_full",
    "budget",
    "slots_full",
    "paused",
    "epic_cap",
    "planner_spend_cap",
)
# The routes of the manager API (manager.match_route and manager.ACTIONS; a test keeps them equal).
ROUTES = (
    "healthz",
    "repos",
    "trigger-pause",
    "spend-breaker",
    "ui",
    "dashboard",
    "history-runs",
    "history-jobs",
    "job-detail",
    "run-logs",
    "run-log",
    "job-logs",
    "job-log",
    "plan",
    "run",
    "status",
    "drain",
    "pause",
    "resume",
    "retry",
    "stop",
    "preflight",
    "record-capabilities",
    "record-blocker",
    "import-evidence",
    "purge",
    "unknown",
)
GITHUB_STATES = ("ok", "ambient", "checking", "permission_missing", "unavailable")
FALSE_INCOMPLETE_STATES = ("ok", "stale", "checking", "unavailable")
FALSE_INCOMPLETE_COUNTS = (
    "counted",
    "merged_unchanged",
    "merged_changed",
    "closed_unmerged",
    "unresolved",
    "workflow_hold",
    "uncoded",
)
CHILD_KINDS = ("plan", "run", "append")
PARTS = ("total", "in_use", "waiting", "used", "free", "live", "final", "ceiling")


def _closed(*groups):
    """The values of the groups in order, without duplicates, then ``other``."""
    seen = []
    for group in groups:
        for value in group:
            if value not in seen:
                seen.append(value)
    return (*seen, OTHER)


LABEL_VALUES = {
    "phase": _closed(phases.PHASES, (NONE,)),
    "status": _closed(JOB_STATUSES),
    "state": _closed(GITHUB_STATES, FALSE_INCOMPLETE_STATES),
    "reason": _closed(reason_codes.ORDER),
    "skip_reason": _closed(SKIP_REASONS),
    "purge_reason": _closed(history.PURGE_REASONS),
    "route": _closed(ROUTES),
    "method": ("GET", "POST", OTHER),
    "status_class": ("2xx", "3xx", "4xx", "5xx", OTHER),
    "http_status": ("401", "403", OTHER),
    "kind": _closed(CHILD_KINDS),
    "outcome": _closed(history.OUTCOMES),
    "part": _closed(PARTS),
    "count": _closed(FALSE_INCOMPLETE_COUNTS),
    # apierror.CODES plus what only a breaker trip says (stopworld.KINDS): the vocabulary of the API-error metrics.
    "code": _closed(tuple(code for code in stopworld.KINDS if code != apierror.OTHER)),
    # plan_failure.CODES: the closed reasons a plan failed (the table already ends with other).
    "plan_failure_code": tuple(plan_failure.CODES),
}


# --- the catalogue ---------------------------------------------------------------------


@dataclass(frozen=True)
class Spec:
    """One declared metric: its name, kind, help text, label names, buckets and where its numbers come from."""

    name: str
    kind: str
    help: str
    labels: tuple = ()
    buckets: tuple = ()
    source: str = LIVE


REQUEST_BUCKETS = (0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
LEAD_BUCKETS = (60.0, 300.0, 900.0, 1800.0, 3600.0, 7200.0, 14400.0, 28800.0, 86400.0, 259200.0)
PHASE_BUCKETS = (30.0, 60.0, 120.0, 300.0, 600.0, 1200.0, 1800.0, 3600.0, 7200.0, 14400.0)


def _counter(name, help_text, labels=(), source=LIVE):
    return Spec(name, COUNTER, help_text, tuple(labels), (), source)


def _gauge(name, help_text, labels=(), source=LIVE):
    return Spec(name, GAUGE, help_text, tuple(labels), (), source)


def _histogram(name, help_text, buckets, labels=(), source=LIVE):
    return Spec(name, HISTOGRAM, help_text, tuple(labels), tuple(buckets), source)


CATALOGUE = (
    # -- from the history ledger (outlives purge) -------------------------------------
    _counter(
        "factory_runs_finished_total",
        "Runs the history ledger shows as finished, by the least successful outcome of their jobs.",
        ("repo", "outcome"),
        LEDGER,
    ),
    _counter(
        "factory_jobs_finished_total",
        "Jobs the history ledger shows as finished, by outcome.",
        ("repo", "outcome"),
        LEDGER,
    ),
    _counter(
        "factory_job_reason_codes_total",
        "Jobs the history ledger shows with a verification reason code (reason_codes.ORDER).",
        ("repo", "reason"),
        LEDGER,
    ),
    _counter(
        "factory_runs_purged_total",
        "Runs the janitor or an operator purged, from the history ledger.",
        ("repo", "purge_reason"),
        LEDGER,
    ),
    _counter(
        "factory_cost_usd_total",
        "Client-side estimate of the USD spent by the runs in the history ledger (planner and jobs).",
        ("repo",),
        LEDGER,
    ),
    _counter(
        "factory_input_tokens_total",
        "Uncached input tokens of the sessions in the history ledger, by phase (token counts, not credentials).",
        ("repo", "phase"),
        LEDGER,
    ),
    _counter(
        "factory_output_tokens_total",
        "Output tokens of the sessions in the history ledger, by phase.",
        ("repo", "phase"),
        LEDGER,
    ),
    _counter(
        "factory_cache_creation_input_tokens_total",
        "Input tokens written to the prompt cache by the sessions in the history ledger, by phase.",
        ("repo", "phase"),
        LEDGER,
    ),
    _counter(
        "factory_cache_read_input_tokens_total",
        "Input tokens read from the prompt cache by the sessions in the history ledger, by phase.",
        ("repo", "phase"),
        LEDGER,
    ),
    _histogram(
        "factory_plan_to_pr_seconds",
        "Seconds from a run being planned to the first pull request of one of its jobs.",
        LEAD_BUCKETS,
        ("repo",),
        LEDGER,
    ),
    _histogram(
        "factory_phase_seconds",
        "Seconds a job spent in one phase, from the history ledger.",
        PHASE_BUCKETS,
        ("repo", "phase"),
        LEDGER,
    ),
    _gauge("factory_history_bytes", "Bytes of history ledger segments on disk.", (), LEDGER),
    _gauge(
        "factory_history_truncated",
        "1 when the last read of the history ledger left older events unread, so ledger totals cover only the newest part.",
        (),
        LEDGER,
    ),
    # -- over the run directories that still exist (gauges: a purge lowers them) --------------
    _gauge("factory_jobs", "Jobs in the retained runs by raw status and phase.", ("repo", "status", "phase"), RUNS),
    _gauge(
        "factory_retained_cost_usd",
        "USD estimate spent by the jobs of the retained runs, by phase.",
        ("repo", "phase"),
        RUNS,
    ),
    _gauge("factory_run_budget_usd", "The configured run_budget_usd of a repository (absent when it has none).", ("repo",), RUNS),
    _gauge(
        "factory_run_spent_usd_max",
        "The most USD spent by any retained run of a repository (compare with factory_run_budget_usd).",
        ("repo",),
        RUNS,
    ),
    _gauge(
        "factory_phase_peak_rss_mib",
        "Peak resident MiB sampled for one session of a phase, over the retained runs.",
        ("repo", "phase"),
        RUNS,
    ),
    _gauge(
        "factory_phase_p95_rss_mib",
        "95th percentile of the peak resident MiB of the sessions of a phase, over the retained runs.",
        ("repo", "phase"),
        RUNS,
    ),
    _gauge("factory_retained_runs", "Run directories of a repository that were read for the run gauges.", ("repo",), RUNS),
    _gauge(
        "factory_retained_runs_truncated",
        "1 when a cap left some run directories unread, so the run gauges cover only part of them.",
        (),
        RUNS,
    ),
    # -- the manager process --------------------------------------------------------------
    _counter(
        "factory_manager_requests_total",
        "Requests to the manager API by route, method and status class.",
        ("route", "method", "status_class"),
    ),
    _histogram(
        "factory_manager_request_seconds",
        "Seconds the manager API took to answer, by route.",
        REQUEST_BUCKETS,
        ("route",),
    ),
    _counter(
        "factory_manager_auth_failures_total",
        "Requests the manager API answered with 401 or 403.",
        ("http_status",),
    ),
    _counter(
        "factory_manager_jwks_refresh_failures_total",
        "Failed refreshes of the Entra signing keys (entra-jwt mode only).",
    ),
    _gauge("factory_manager_children", "Plan, run and append processes the manager started that are alive.", ("kind",)),
    _gauge("factory_manager_commands_running", "Short CLI commands the manager is running right now."),
    _gauge("factory_trigger_paused", "1 while the label watcher is paused, else 0."),
    _gauge(
        "factory_github_app_state",
        "1 for the current GitHub App check state of a repository, 0 for the others.",
        ("repo", "state"),
    ),
    _gauge(
        "factory_false_incomplete_state",
        "1 for the current state of a repository's false-incomplete summary, 0 for the others.",
        ("repo", "state"),
    ),
    _gauge(
        "factory_false_incomplete_prs",
        "Pull requests in the 30-day false-incomplete window by kind (a window count, so a gauge).",
        ("repo", "count"),
    ),
    _gauge(
        "factory_false_incomplete_reason_prs",
        "Counted pull requests of the false-incomplete window by verification reason code.",
        ("repo", "reason"),
    ),
    _gauge(
        "factory_false_incomplete_rate",
        "Share of held-back pull requests merged unchanged (absent while the sample is too small).",
        ("repo",),
    ),
    _gauge("factory_capacity_workers", "The global worker budget: total slots, in use and waiting.", ("part",)),
    _gauge(
        "factory_worker_pods",
        "Worker pods of the pod backend: live (not over), final (over, not yet swept) and the ceiling of sessions at once.",
        ("part",),
    ),
    _gauge("factory_data_disk_bytes", "Bytes of the volume that holds FACTORY_DATA: total, used and free.", ("part",)),
    _counter("factory_watcher_passes_total", "Label watcher passes that looked at the repositories."),
    _counter("factory_watcher_paused_passes_total", "Label watcher passes skipped because the watcher is paused."),
    _counter(
        "factory_watcher_skips_total",
        "Times the label watcher left a repository or an issue alone, by closed reason.",
        ("repo", "skip_reason"),
    ),
    _counter(
        "factory_watcher_failures_total",
        "Label watcher passes over a repository that failed and started a backoff.",
        ("repo",),
    ),
    _gauge(
        "factory_watcher_backoff_seconds",
        "Seconds left of the label watcher's backoff for a repository (0 when it is not backing off).",
        ("repo",),
    ),
    _counter(
        "factory_budget_drains_total",
        "Runs the cost guardrail drained because they reached run_budget_usd.",
        ("repo",),
    ),
    _counter(
        "factory_api_errors_total",
        "Failed sessions whose API error the classifier recognised, by closed code (a spend limit or a rate limit, say).",
        ("repo", "code"),
    ),
    _counter(
        "factory_plan_failures_total",
        "Plans that failed (an append that failed counts as other), by closed plan failure code.",
        ("repo", "plan_failure_code"),
    ),
    _counter(
        "factory_stop_world_trips_total",
        "Times the stop-the-world breaker paused the trigger and drained the runs, by closed code.",
        ("code",),
    ),
    _counter(
        "factory_log_archive_failures_total",
        "Purged runs whose logs could not be copied to the retained location first, plus failed daily sweeps of it.",
    ),
    _counter("factory_metrics_scrape_errors_total", "Collectors that failed during a scrape (the scrape still answers)."),
)


def catalogue_problems(specs, values):
    """What is wrong with a catalogue, as plain sentences; empty when it is sound."""
    problems = []
    names = [spec.name for spec in specs]
    for name in sorted({name for name in names if names.count(name) > 1}):
        problems.append(f"{name} is declared twice")
    for spec in specs:
        if not spec.name.startswith(PREFIX):
            problems.append(f"{spec.name} does not start with {PREFIX}")
        if spec.kind not in (COUNTER, GAUGE, HISTOGRAM):
            problems.append(f"{spec.name} has unknown kind {spec.kind}")
        if spec.source not in SOURCES:
            problems.append(f"{spec.name} has unknown source {spec.source}")
        if (spec.kind == COUNTER) != spec.name.endswith("_total"):
            problems.append(f"{spec.name}: only counters end in _total, and every counter does")
        if spec.kind == HISTOGRAM and (not spec.buckets or list(spec.buckets) != sorted(set(spec.buckets))):
            problems.append(f"{spec.name} needs strictly increasing buckets")
        if len(set(spec.labels)) != len(spec.labels):
            problems.append(f"{spec.name} repeats a label")
        for label in spec.labels:
            if label not in LABEL_NAMES:
                problems.append(f"{spec.name} uses label {label}, which is not an allowed label name")
            elif label != "repo" and not values.get(label):
                problems.append(f"{spec.name} uses label {label}, which has no closed value set")
        if "le" in spec.labels:
            problems.append(f"{spec.name} declares le, which a histogram adds itself")
    return problems


# --- formatting ---------------------------------------------------------------------------


def escape_label(value):
    """A label value as the text format wants it: backslash, double quote and newline escaped."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def escape_help(text):
    return str(text).replace("\\", "\\\\").replace("\n", "\\n")


def format_number(value):
    """A sample value: integers as such, floats with ``repr`` (shortest round trip), the specials spelled out."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    number = float(value)
    if math.isnan(number):
        return "NaN"
    if math.isinf(number):
        return "+Inf" if number > 0 else "-Inf"
    if number == int(number) and abs(number) < 1e15:
        return str(int(number))
    return repr(number)


def _labels_text(names, values, extra=None):
    pairs = [f'{name}="{escape_label(value)}"' for name, value in zip(names, values, strict=True)]
    if extra is not None:
        pairs.append(f'le="{extra}"')
    return f"{{{','.join(pairs)}}}" if pairs else ""


def _finite(value):
    """``value`` as a finite float, or None for a bool, NaN, infinity or anything that is not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


# --- the registry --------------------------------------------------------------------------


class _Metric:
    def __init__(self, spec, registry):
        self.spec = spec
        self._registry = registry
        self._lock = registry.lock
        self._series = {}

    def _key(self, labels):
        """The label values in declared order, or None for an observation that must be dropped.

        Raises ValueError when the caller names labels the metric does not declare (a bug, caught by tests).
        """
        if set(labels) != set(self.spec.labels):
            raise ValueError(f"{self.spec.name} takes the labels {list(self.spec.labels)}, got {sorted(labels)}")
        key = []
        for name in self.spec.labels:
            value = str(labels[name])
            if name == "repo":
                if value not in self._registry.repos():
                    return None
            else:
                allowed = self._registry.values[name]
                value = value if value in allowed else OTHER
            key.append(value)
        key = tuple(key)
        if key not in self._series and len(self._series) >= MAX_SERIES:
            return None
        return key

    def series_labels(self):
        """The label values of every series now held (a copy, for tests and diagnostics)."""
        with self._lock:
            return sorted(self._series)

    def lines(self):  # pragma: no cover - overridden
        raise NotImplementedError


class Counter(_Metric):
    """A value that only goes up. ``inc`` for an event, ``track`` for a total worked out from another source."""

    def inc(self, amount=1, **labels):
        step = _finite(amount)
        if step is None or step < 0:
            return
        with self._lock:
            key = self._key(labels)
            if key is not None:
                value, seen = self._series.get(key, (0, 0))
                self._series[key] = (value + step, seen)

    def track(self, total, **labels):
        """Follow ``total``, a figure computed from a source that can shrink (a window slides, a job is retried).

        The first figure seen becomes the value; after that the value grows by every increase of ``total`` and
        ignores every decrease, so the series never goes down inside this process.
        """
        figure = _finite(total)
        if figure is None or figure < 0:
            return
        with self._lock:
            key = self._key(labels)
            if key is None:
                return
            if key not in self._series:
                self._series[key] = (figure, figure)
                return
            value, seen = self._series[key]
            self._series[key] = (value + max(0.0, figure - seen), figure)

    def value(self, **labels):
        with self._lock:
            key = self._key(labels)
            return self._series.get(key, (0, 0))[0] if key is not None else 0

    def lines(self):
        with self._lock:
            rows = sorted(self._series.items())
        return [
            f"{self.spec.name}{_labels_text(self.spec.labels, key)} {format_number(value)}" for key, (value, _) in rows
        ]


class Gauge(_Metric):
    """A value that goes up and down. ``replace`` swaps the whole set of series, so a vanished one disappears."""

    def set(self, value, **labels):
        number = _finite(value)
        if number is None:
            return
        with self._lock:
            key = self._key(labels)
            if key is not None:
                self._series[key] = number

    def replace(self, rows):
        """``rows`` is ``[(labels, value)]``; the gauge holds exactly these series afterwards."""
        with self._lock:
            held, self._series = self._series, {}
            try:
                for labels, value in rows:
                    value = _finite(value)
                    key = self._key(labels)
                    if key is not None and value is not None:
                        self._series[key] = value
            except ValueError:
                self._series = held
                raise

    def value(self, **labels):
        with self._lock:
            key = self._key(labels)
            return self._series.get(key) if key is not None else None

    def lines(self):
        with self._lock:
            rows = sorted(self._series.items())
        return [f"{self.spec.name}{_labels_text(self.spec.labels, key)} {format_number(value)}" for key, value in rows]


class Histogram(_Metric):
    """Cumulative buckets, a sum and a count. ``observe`` for an event, ``track`` for counts from another source."""

    def _blank(self):
        return [[0] * (len(self.spec.buckets) + 1), 0.0, [0] * (len(self.spec.buckets) + 1), 0.0]

    def bucket_index(self, value):
        for index, bound in enumerate(self.spec.buckets):
            if value <= bound:
                return index
        return len(self.spec.buckets)

    def observe(self, value, **labels):
        number = _finite(value)
        if number is None or number < 0:
            return
        with self._lock:
            key = self._key(labels)
            if key is None:
                return
            held = self._series.setdefault(key, self._blank())
            held[0][self.bucket_index(number)] += 1
            held[1] += number

    def counts_of(self, values):
        """The per-bucket counts (not cumulative, the last is ``+Inf``) and the sum of ``values`` that are usable."""
        counts = [0] * (len(self.spec.buckets) + 1)
        total = 0.0
        for value in values:
            value = _finite(value)
            if value is None or value < 0:
                continue
            counts[self.bucket_index(value)] += 1
            total += value
        return counts, total

    def track(self, values, **labels):
        """Follow the observations in ``values``, a list recomputed from a source that can shrink.

        Each bucket and the sum grow by their increase since the last call and ignore decreases, like
        ``Counter.track``; a series first seen starts from what ``values`` holds.
        """
        counts, total = self.counts_of(values)
        with self._lock:
            key = self._key(labels)
            if key is None:
                return
            held = self._series.get(key)
            if held is None:
                self._series[key] = [list(counts), total, list(counts), total]
                return
            shown, shown_sum, seen, seen_sum = held
            for index, now in enumerate(counts):
                shown[index] += max(0, now - seen[index])
                seen[index] = now
            held[1] = shown_sum + max(0.0, total - seen_sum)
            held[3] = total

    def snapshot(self, **labels):
        """``(cumulative bucket counts, sum, count)`` of a series, or None."""
        with self._lock:
            key = self._key(labels)
            held = self._series.get(key) if key is not None else None
            if held is None:
                return None
            return _cumulative(held[0]), held[1], sum(held[0])

    def lines(self):
        with self._lock:
            rows = sorted((key, list(held[0]), held[1]) for key, held in self._series.items())
        out = []
        bounds = [*(format_number(bound) for bound in self.spec.buckets), "+Inf"]
        for key, counts, total in rows:
            cumulative = _cumulative(counts)
            for bound, count in zip(bounds, cumulative, strict=True):
                out.append(f"{self.spec.name}_bucket{_labels_text(self.spec.labels, key, bound)} {count}")
            out.append(f"{self.spec.name}_sum{_labels_text(self.spec.labels, key)} {format_number(total)}")
            out.append(f"{self.spec.name}_count{_labels_text(self.spec.labels, key)} {cumulative[-1]}")
        return out


def _cumulative(counts):
    running = 0
    out = []
    for count in counts:
        running += count
        out.append(running)
    return out


_KINDS = {COUNTER: Counter, GAUGE: Gauge, HISTOGRAM: Histogram}


class Registry:
    """The metrics of one manager: built from the catalogue, thread safe, rendered on demand.

    ``repos`` returns the configured repositories (the only values the ``repo`` label may take); it is
    asked at most every ``REPO_TTL_SECONDS``. ``values`` replaces closed value sets (tests use it).
    """

    def __init__(self, specs=CATALOGUE, repos=lambda: (), values=None, clock=time.monotonic):
        self.values = {**LABEL_VALUES, **(values or {})}
        problems = catalogue_problems(specs, self.values)
        if problems:
            raise ValueError("; ".join(problems))
        self.lock = threading.RLock()
        self._repos = repos
        self._clock = clock
        self._repo_cache = (None, frozenset())
        self._metrics = {spec.name: _KINDS[spec.kind](spec, self) for spec in specs}

    def repos(self):
        now = self._clock()
        with self.lock:
            stamp, known = self._repo_cache
            if stamp is not None and now - stamp < REPO_TTL_SECONDS:
                return known
        try:
            known = frozenset(str(repo) for repo in self._repos())
        except Exception:  # noqa: BLE001  (a repository listing that fails means no repo label this time)
            known = frozenset()
        with self.lock:
            self._repo_cache = (now, known)
        return known

    def forget_repos(self):
        """Drop the cached repository list (a test or a configuration reload)."""
        with self.lock:
            self._repo_cache = (None, frozenset())

    def __getitem__(self, name):
        return self._metrics[name]

    def __iter__(self):
        return iter(self._metrics.values())

    def specs(self):
        return [metric.spec for metric in self._metrics.values()]

    def render(self):
        """The whole exposition in text format 0.0.4: HELP and TYPE of every metric, then its series."""
        out = []
        for metric in self._metrics.values():
            spec = metric.spec
            out.append(f"# HELP {spec.name} {escape_help(spec.help)}")
            out.append(f"# TYPE {spec.name} {spec.kind}")
            out.extend(metric.lines())
        return f"{chr(10).join(out)}\n"


# --- the listener --------------------------------------------------------------------------


class MetricsHandler(BaseHTTPRequestHandler):
    """``GET /metrics`` and nothing else. No identity is read, no state is changed, nothing is logged."""

    protocol_version = "HTTP/1.0"
    timeout = HEADER_TIMEOUT_SECONDS

    def version_string(self):
        return "factory-metrics"

    def log_message(self, fmt, *args):
        """Silenced: this listener never writes a line (the audit trail belongs to the manager API)."""

    def send_error(self, code, message=None, explain=None):
        self._answer(code, "text/plain; charset=utf-8", f"{code}\n".encode("ascii"))

    def _answer(self, status, content_type, body, headers=()):
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except OSError:  # the scraper went away
            self.close_connection = True

    def _authorised(self):
        """True when no token is required, or the request carries ``Authorization: Bearer <token>``.

        Both sides are hashed first so the comparison is constant time whatever the lengths; nothing about the
        header (or the token) is stored or written.
        """
        expected = self.server.token_digest
        if expected is None:
            return True
        scheme, _, credential = self.headers.get("Authorization", "").partition(" ")
        credential = credential.strip()
        if scheme.lower() != "bearer" or not credential or len(credential) > MAX_CREDENTIAL_CHARS:
            return False
        return hmac.compare_digest(hashlib.sha256(credential.encode("latin-1", "replace")).digest(), expected)

    def do_GET(self):
        if not self._authorised():
            self._answer(401, "text/plain; charset=utf-8", b"401\n", (("WWW-Authenticate", 'Bearer realm="factory-metrics"'),))
            return
        if self.path.split("?", 1)[0] != METRICS_PATH:
            self._answer(404, "text/plain; charset=utf-8", b"404\n")
            return
        server = self.server
        if not server.slots.acquire(blocking=False):
            self._answer(503, "text/plain; charset=utf-8", b"503\n", (("Retry-After", "5"),))
            return
        try:
            body = server.scrape().encode("utf-8")
        except Exception:  # noqa: BLE001  (a scrape that fails answers a bare 500: no message, no traceback)
            self._answer(500, "text/plain; charset=utf-8", b"500\n")
            return
        finally:
            server.slots.release()
        self._answer(200, CONTENT_TYPE, body)

    def _refuse(self):
        self._answer(405, "text/plain; charset=utf-8", b"405\n", (("Allow", "GET"),))

    do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _refuse


class MetricsServer(ThreadingHTTPServer):
    """The metrics listener: daemon handler threads (shutdown never waits for a scraper) and a connection cap."""

    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address, scrape, ipv6=False, token=""):
        self.scrape = scrape
        # Only the digest is kept (the token itself is not an attribute): a repr or a debugger shows nothing useful.
        self.token_digest = hashlib.sha256(token.encode("utf-8")).digest() if token else None
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        if ipv6:
            self.address_family = socket.AF_INET6
        super().__init__(address, MetricsHandler)

    @property
    def requires_token(self):
        return self.token_digest is not None

    def handle_error(self, request, client_address):
        """The default prints a traceback to stderr; this listener says nothing."""

"""Issues Factory opens itself for what a worker could not fix or found outside its assignment (#104).

A builder or a reviewer often notices something that is not part of its job: a bug in a neighbouring file, a
flaky test, a missing tool in the sandbox, a follow-up the issue implies. Until now that knowledge died in a
summary or a log. A worker may now report such things as ``observations`` in its result (``schemas.py``); this
module is the supervisor's side. The worker only reports: it never talks to GitHub, and nothing it writes
reaches GitHub except through the validation, the sanitising and the fixed template below.

* **ON by default, with three ways to stop it.** ``observed_issues = false`` in a repository's config, the
  deployment-wide ``FACTORY_OBSERVED_ISSUES=off`` in the environment, and the ``factory-ignore`` label on the
  issue a job works on (never an issue about something a person opted out; read just before the write, and an
  issue that cannot be read counts as opted out for that moment). A run with no configured repository
  (``cfg.repo`` empty) never opens issues: the target is never inferred from a checkout.
* **There is no cap.** No count of issues per job, per run or per day exists in this module; the bounds are the
  dedupe below, the schema bound on how many observations ONE worker result may carry (``MAX_OBSERVATIONS``: the
  shape of a result, not a limit on issues), the two switches and the failure cool-down. See
  ``docs/operations.md`` ("Issues opened by Factory") for what that means for a flooded or poisoned repository.
* **The supervisor decides.** ``parse_observations`` validates every item of a result (shape, kind, sizes) and
  sanitises its title and detail with the commenter's ``sanitize_reason`` (control characters, HTML, links,
  ``@mentions``, secret shapes and the literal secret values of the process, paths outside the repository);
  what it cannot trust is dropped with ONE fixed line on stderr and the job goes on. Only plain
  repository-relative paths survive. The kept items are stored in the job's persisted state (``observed``),
  where the next step finds them: nothing is written to GitHub inside a phase.
* **Opened when the job ends.** ``IssueLabels`` calls ``due``/``flush`` from the persisted-state hooks it
  already has, when a job reaches ``pr_ready``, ``pr_incomplete``, ``blocked``, ``failed``,
  ``needs_human_publish``, ``merged`` or ``closed`` (so the pull request, when there is one, can be linked), and from
  ``reconcile`` (every ``run`` and the CLI ``reconcile``). It is best effort and never a control of a job: every
  failure (403, 404, a timeout, a malformed answer) is swallowed after ONE fixed line per process,
  ``factory: observed issue failed (<exception class>)``; three in a row rest the writer for five minutes; a lost
  repository lock stops it for good.
* **A flush that did not go through is asked for again.** A job that ended blocked, failed or waiting for a person
  has no pull request, so nothing else would bring its run back. ``observed.json`` in the run directory records,
  per job, the fingerprints that have their issue (found by the listing or opened) and those dismissed on
  purpose (a person opted the issue out, or the job owns no issue); ``pending`` says whether a finished job
  still holds one that is in neither. The manager's idle reconcile asks for such a run (while issues for
  observations are on at all: ``wanted``), at most every ``RECONCILE_SECONDS`` like any other, until the flush
  goes through. The record is a hint, not a source of truth: it holds fingerprints only, and without it the next
  flush lists the issues again (a duplicate is still impossible).
* **Idempotent, closed issues included.** Each issue starts with a hidden marker holding a fingerprint (a hash of
  the normalised title and the sorted paths). Before opening anything the supervisor lists the issues that carry
  ``factory:observed``, open AND closed, keeps only those authored by Factory's own identity (the App's bot user
  id, or the ambient ``gh`` login; never another author's: a forged marker suppresses nothing) and opens only
  what no such issue has. A person who closed one as won't-fix is therefore not shown it again. When the list
  cannot be read or the identity is unknown, nothing is opened (fail closed). The label stays on the issue for
  good: it is the index of this lookup, which is why it is an origin label and not one of the status labels.
* **A fixed template, not model prose.** The title is a fixed prefix and bounded plain text; the body is a fixed
  frame whose only model-derived text (the detail and the paths) sits in ONE fenced code block, where nothing
  links, notifies or renders. The frame names the source issue(s), the pull request when there is one, the run
  and the job: references Factory itself produced.
* **Never more work by itself.** The new issue gets ``factory:observed`` and NEVER the trigger label; it has no
  assignee. A person triages it and applies ``factory-build`` if it is worth doing (``trigger.py`` lets that
  label start a plan on an issue that carries ``factory:observed``). Until then the planner's unfiltered listing
  leaves it out (``issue_labels.drop_unadopted``).

The same repository only: the target is always ``cfg.repo``.
"""

import hashlib
import json
import os
import re
import sys
import textwrap
import threading
import time
from pathlib import Path
from urllib.parse import quote

from . import issue_comments
from .github import repo_path
from .issue_comments import (
    _LABEL_NAME,
    _OFF,
    _RUN_NAME,
    DEFAULT_TRIGGER_LABEL,
    MAX_CONSECUTIVE_FAILURES,
    IssueComments,
    sanitize_reason,
)
from .issue_labels import LABEL_OBSERVED, OBSERVED_LABEL_STYLE
from .schemas import (
    MAX_OBSERVATIONS,
    OBSERVATION_DETAIL_MAX,
    OBSERVATION_MAX_PATHS,
    OBSERVATION_PATH_MAX,
    OBSERVATION_TITLE_MAX,
)
from .shell import command
from .state import atomic_json

ENV_SWITCH = "FACTORY_OBSERVED_ISSUES"

# Next to ``state.json`` in the run directory: for each job, the fingerprints whose issue is known to exist (or
# for which nothing will be opened). A hint and nothing more: without it the next flush reads the issues again.
DONE_FILE = "observed.json"

CALL_TIMEOUT = 20  # seconds per gh call that writes
LIST_TIMEOUT = 40  # the listing of the issues that carry the label can span several pages

TITLE_PREFIX = "[Factory observed] "
MARKER_PREFIX = "<!-- factory-observed:v1 fp="
FINGERPRINT_LENGTH = 32
DETAIL_WIDTH = 100

# The job statuses at which a job's observations are opened: its own work is over (or parked for a person),
# so the pull request, when there is one, is known.
STATUSES = frozenset(
    {"pr_ready", "pr_incomplete", "blocked", "failed", "needs_human_publish", "merged", "closed"}
)
BY = {"build": "builder", "review": "reviewer"}

# kind -> headline. Fixed text: nothing a model wrote goes in here.
KINDS = {
    "out_of_scope": "outside the issue it was given",
    "cannot_fix": "something it could not fix",
    "bug_found": "a defect found while working",
    "follow_up": "a follow-up the work implies",
    "environment": "a problem with the worker environment",
}

# A kind a worker may still report (the result schema accepts it) but Factory never opens an issue for: a limit of
# the worker's own sandbox (a missing tool, a read-only filesystem) is Factory's concern, not a defect of the
# repository the job targets, so it does not belong in that repository's issue tracker.
NOT_OPENED = frozenset({"environment"})

_JOB = re.compile(r"[a-z0-9][a-z0-9-]{0,59}")
_SEGMENT = re.compile(r"[A-Za-z0-9_.+-]+")
_FINGERPRINT = re.compile(rf"[0-9a-f]{{{FINGERPRINT_LENGTH}}}")


def switched_off(env=None):
    """True when the deployment-wide kill switch (``FACTORY_OBSERVED_ISSUES=off``) is set."""
    source = os.environ if env is None else env
    return (source.get(ENV_SWITCH) or "").strip().lower() in _OFF


def wanted(cfg, env=None):
    """True when a repository's config and the environment allow issues for observations at all.

    Off without a configured repository: the target of a write is the configured one, never a checkout's guess.
    """
    slug = getattr(cfg, "repo", "") or None
    return getattr(cfg, "observed_issues", False) is True and slug is not None and not switched_off(env)


# ---- what a worker reported -------------------------------------------------------------


def valid_path(path):
    """True for a plain repository-relative path: segments of ``A-Z a-z 0-9 _ . + -``, no ``..``, no leading ``/``."""
    if not isinstance(path, str) or not 0 < len(path) <= OBSERVATION_PATH_MAX:
        return False
    parts = path.split("/")
    return all(
        _SEGMENT.fullmatch(part) is not None and part not in (".", "..") and not part.startswith("-")
        for part in parts
    )


def fingerprint(title, paths):
    """The stable identity of an observation: its normalised (cleaned) title and its sorted paths."""
    text = "\0".join([" ".join(str(title).casefold().split()), *sorted(set(paths))])
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:FINGERPRINT_LENGTH]


def _dropped(job, reason):
    """One fixed-format line per observation left out: a job id and a reason code, never any text."""
    safe_job = job if isinstance(job, str) and _JOB.fullmatch(job) else "unknown"
    print(f"factory: observation dropped job={safe_job} reason={reason}", file=sys.stderr)


def clean_observation(entry, by, slug=None, refs=(), secrets=()):
    """``(item, None)`` for a usable observation, ``(None, reason)`` for one that is not. Never trusts the text."""
    if not isinstance(entry, dict):
        return None, "shape"
    kind, title, detail = entry.get("kind"), entry.get("title"), entry.get("detail")
    paths = entry.get("paths", [])
    if not isinstance(title, str) or not isinstance(detail, str):
        return None, "shape"
    if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
        return None, "shape"
    if not isinstance(kind, str) or kind not in KINDS:
        return None, "kind"
    if kind in NOT_OPENED:
        return None, "factory_scope"
    if (
        len(title) > OBSERVATION_TITLE_MAX
        or len(detail) > OBSERVATION_DETAIL_MAX
        or len(paths) > OBSERVATION_MAX_PATHS
        or any(len(path) > OBSERVATION_PATH_MAX for path in paths)
    ):
        return None, "oversized"
    clean_title = sanitize_reason(title, refs, slug, limit=OBSERVATION_TITLE_MAX, secrets=secrets)
    if clean_title is None:
        return None, "title"
    clean_title = clean_title.replace("`", "'")
    clean_detail = sanitize_reason(detail, refs, slug, limit=OBSERVATION_DETAIL_MAX, secrets=secrets)
    if clean_detail is None:
        return None, "detail"
    # A path that is not a plain repository-relative path is left out; the observation stays.
    kept = [path for path in dict.fromkeys(paths) if valid_path(path)]
    return (
        {
            "kind": kind,
            "title": clean_title,
            "detail": clean_detail,
            "paths": kept,
            "by": by,
            "fp": fingerprint(clean_title, kept),
        },
        None,
    )


def parse_observations(raw, job="", by="build", slug=None, refs=()):
    """The usable observations of one worker result, cleaned. Never raises.

    ``raw`` is the ``observations`` value of a builder or reviewer result (None when absent). Every item that
    is invalid, oversized, past ``MAX_OBSERVATIONS`` or not trustworthy after sanitising is dropped with one
    fixed line on stderr (``factory: observation dropped job=<id> reason=<code>``) and the others are kept.
    """
    if raw is None:
        return []
    kept = []
    try:
        if not isinstance(raw, list):
            _dropped(job, "type")
            return []
        secrets = issue_comments._secret_values() if raw else ()
        seen = set()
        for index, entry in enumerate(raw):
            if index >= MAX_OBSERVATIONS:
                _dropped(job, "excess")
                continue
            try:
                item, reason = clean_observation(entry, by, slug, refs, secrets)
            except Exception:  # noqa: BLE001
                item, reason = None, "error"
            if item is None:
                _dropped(job, reason)
            elif item["fp"] not in seen:
                seen.add(item["fp"])
                kept.append(item)
    except Exception:  # noqa: BLE001
        _dropped(job, "error")
    return kept


def stored(item):
    """The observation as the job's state holds it, checked once more, or None. The fingerprint must match."""
    if not isinstance(item, dict):
        return None
    kind, title, detail = item.get("kind"), item.get("title"), item.get("detail")
    paths, by, fp = item.get("paths"), item.get("by"), item.get("fp")
    if not isinstance(kind, str) or kind not in KINDS or kind in NOT_OPENED:
        return None
    if not isinstance(by, str) or by not in BY:
        return None
    if not isinstance(title, str) or not 0 < len(title) <= OBSERVATION_TITLE_MAX:
        return None
    if not isinstance(detail, str) or not 0 < len(detail) <= OBSERVATION_DETAIL_MAX:
        return None
    if not isinstance(paths, list) or len(paths) > OBSERVATION_MAX_PATHS or not all(valid_path(p) for p in paths):
        return None
    if not isinstance(fp, str) or _FINGERPRINT.fullmatch(fp) is None or fp != fingerprint(title, paths):
        return None
    return {"kind": kind, "title": title, "detail": detail, "paths": list(paths), "by": by, "fp": fp}


def merge(existing, found):
    """The job's observations after ``found`` is added: first come, first kept, one per fingerprint."""
    merged = []
    seen = set()
    for item in [*(existing if isinstance(existing, list) else []), *found]:
        clean = stored(item)
        if clean is not None and clean["fp"] not in seen:
            seen.add(clean["fp"])
            merged.append(clean)
    return merged


# ---- which observations have their issue -------------------------------------------------


def read_done(directory):
    """``{job id: set of fingerprints}`` recorded in a run directory. Never raises; garbage reads as nothing."""
    try:
        raw = json.loads((Path(directory) / DONE_FILE).read_text())["done"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    done = {}
    for job, fingerprints in raw.items():
        if isinstance(job, str) and isinstance(fingerprints, list):
            done[job] = {fp for fp in fingerprints if isinstance(fp, str) and _FINGERPRINT.fullmatch(fp)}
    return done


def pending(directory, jobs):
    """True when a job that has ended holds an observation no issue is known to carry yet.

    ``jobs`` is the ``jobs`` object of a run's ``state.json``. This is what lets the manager's idle reconcile
    ask for a run none of whose jobs has a published pull request, after a flush that GitHub refused.
    """
    if not isinstance(jobs, dict):
        return False
    done = None
    for key, job in jobs.items():
        if not isinstance(job, dict) or job.get("status") not in STATUSES:
            continue
        items = job.get("observed")
        wanted_fps = {kept["fp"] for kept in map(stored, items if isinstance(items, list) else []) if kept}
        if not wanted_fps:
            continue
        if done is None:
            done = read_done(directory)
        if not wanted_fps <= done.get(key, set()):
            return True
    return False


# ---- the issue ---------------------------------------------------------------------------


def marker(fp):
    return f"{MARKER_PREFIX}{fp} -->"


def _number(value):
    """True for a positive integer that is not a bool (the numbers Factory itself produced)."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def render(item, issues, pr, job, run, trigger_label=DEFAULT_TRIGGER_LABEL):
    """``(title, body)`` of the issue for one stored observation, from a fixed template.

    ``issues`` and ``pr`` are numbers Factory itself produced (the job's issues and its pull request).
    """
    observation = stored(item)
    if observation is None:
        raise ValueError("not an observation")
    safe_job = job if isinstance(job, str) and _JOB.fullmatch(job) else "unknown"
    safe_run = run if isinstance(run, str) and _RUN_NAME.fullmatch(run) else "unknown"
    label = trigger_label if _LABEL_NAME.fullmatch(str(trigger_label)) else DEFAULT_TRIGGER_LABEL
    numbers = sorted({n for n in issues if _number(n)})
    where = f" ({', '.join(f'#{n}' for n in numbers)})" if numbers else ""
    pull = f" Pull request: #{pr}." if _number(pr) else ""
    block = textwrap.wrap(
        observation["detail"], width=DETAIL_WIDTH, break_long_words=True, break_on_hyphens=False
    ) or [observation["detail"]]
    if observation["paths"]:
        block += ["", f"Paths: {', '.join(observation['paths'])}"]
    lines = [
        marker(observation["fp"]),
        f"**Factory observed: {KINDS[observation['kind']]}**",
        "",
        f"A Factory worker reported the text below while it worked on the issue{where}.{pull} It is the "
        "worker's own report: nobody has checked it, and it may be wrong or incomplete.",
        "",
        *issue_comments._fenced(block),
        "",
        f"Reported by the {BY[observation['by']]} of job `{safe_job}` in run `{safe_run}` · Kind: `{observation['kind']}`",
        "",
        "**What to do with it:** read it and decide. To have Factory build it, apply the "
        f"`{label}` label yourself. To drop it, close this issue: Factory also looks at closed issues, so "
        "the same observation is not opened again. Factory never applies that label, assigns anybody or "
        "closes anything on its own.",
        "",
        "This issue was opened automatically by Factory from a worker's report.",
    ]
    return f"{TITLE_PREFIX}{observation['title']}", "\n".join(lines)


# ---- reporting and writing ---------------------------------------------------------------

_reported = False
_report_lock = threading.Lock()


def reset_report():
    """Allow the next failure to print its line again (tests; a process is one run)."""
    global _reported
    with _report_lock:
        _reported = False


def _report(error):
    global _reported
    with _report_lock:
        if _reported:
            return
        _reported = True
    print(f"factory: observed issue failed ({type(error).__name__})", file=sys.stderr)


class ObservedIssues(IssueComments):
    """Opens one issue per observation that none carries yet, best effort and fenced.

    It is built on ``IssueComments`` for what it has in common with the commenter (the identity test, the
    fence, the failure streak and the cool-down) and differs where the decisions differ: its own switch
    (``FACTORY_ISSUE_COMMENTS`` does not apply) and NO cap (``active`` ignores the comment cap).
    """

    def __init__(
        self,
        repo,
        slug,
        run,
        enabled=True,
        trigger_label=DEFAULT_TRIGGER_LABEL,
        fence=None,
        clock=time.monotonic,
        env=None,
        directory=None,
    ):
        super().__init__(repo, slug, run, enabled=False, trigger_label=trigger_label, fence=fence, clock=clock)
        self.enabled = bool(enabled) and not switched_off(env)
        self.directory = None if directory is None else Path(directory)  # where ``DONE_FILE`` is kept, if anywhere
        self.known = set()  # fingerprints an issue of ours is known to carry (listed, or opened by this process)
        self.ensured = False
        self.opened = 0  # a count of what this process wrote, for tests and logs: it limits nothing

    @property
    def active(self):
        """False while nothing may be written: switched off, lost or cooling down. There is no cap."""
        if not self.enabled or self.lost:
            return False
        if self.suspended_until is not None:
            if self.clock() < self.suspended_until:
                return False
            self.suspended_until = None
            self.failures = MAX_CONSECUTIVE_FAILURES - 1  # on probation: one failure rests again
        return True

    def report(self, error):
        _report(error)

    def _failed(self, error):
        _report(error)

    def due(self, previous, current, job=None):
        """Whether a job state change is the moment to open what the job observed.

        ``job`` (its id) lets the record in the run directory answer too: an observation settled by an earlier
        process (opened, or dismissed on purpose) is not asked for again, not even with a read.
        """
        if not isinstance(current, dict) or not self.active or current.get("status") not in STATUSES:
            return False
        items = current.get("observed")
        if not isinstance(items, list) or not items:
            return False
        if all(isinstance(item, dict) and item.get("fp") in self.known for item in items):
            return False  # every one is known to have its issue: no call, not even a read of the source issue
        if job is not None and self.directory is not None:
            settled = {kept["fp"] for kept in map(stored, items) if kept}
            if settled and settled <= read_done(self.directory).get(job, set()):
                return False  # settled by an earlier process: nothing to open, nothing to read
        return previous is None or _signature(previous) != _signature(current)

    def flush(self, items, issues, pr, job):
        """Open an issue for each stored observation that none carries yet. Returns how many were opened.

        Never raises. ``issues`` are the job's issue numbers, ``pr`` its pull request number (or None).
        """
        try:
            with self.mutex:
                opened = self._flush(items, issues, pr, job)
                self._settle(items, job, only_known=True)
                return opened
        except Exception as error:  # noqa: BLE001
            _report(error)
            return 0

    def dismiss(self, items, job):
        """Record that nothing will be opened for these observations (a person opted the issue out, or the
        job owns no issue), so that nothing asks for them again. Never raises."""
        try:
            with self.mutex:
                self._settle(items, job, only_known=False)
        except Exception as error:  # noqa: BLE001
            _report(error)

    def _settle(self, items, job, only_known):
        """Write down which of a job's observations are done: those with a known issue, or all of them."""
        if self.directory is None or not isinstance(items, list):
            return
        fingerprints = [kept["fp"] for kept in map(stored, items) if kept]
        if only_known:
            fingerprints = [fp for fp in fingerprints if fp in self.known]
        if fingerprints:
            self._attempt(self._record, job, fingerprints)

    def _record(self, job, fingerprints):
        done = read_done(self.directory)
        have = done.get(job, set())
        if set(fingerprints) <= have:
            return
        done[job] = have | set(fingerprints)
        atomic_json(
            self.directory / DONE_FILE,
            {"version": 1, "done": {key: sorted(value) for key, value in sorted(done.items())}},
        )

    def _flush(self, items, issues, pr, job):
        if not self.active or not isinstance(items, list):
            return 0
        recorded = read_done(self.directory).get(job, set()) if self.directory is not None else set()
        todo = []
        seen = set()
        for raw in items:
            item = stored(raw)
            if item is not None and item["fp"] not in self.known and item["fp"] not in recorded and item["fp"] not in seen:
                seen.add(item["fp"])
                todo.append(item)
        if not todo or not self._attempt(self._learn):
            return 0
        opened = 0
        for item in todo:
            if item["fp"] in self.known:
                continue  # an issue of ours carries it already
            if not self.active:
                break
            if self._attempt(self._open, item, issues, pr, job):
                opened += 1
        return opened

    def _learn(self):
        """Read the fingerprints of Factory's own observed issues (open and closed) into ``known``."""
        author = self._author_test()
        if author is None:
            raise LookupError("the identity of the writer is unknown")  # never open blind: a restart would duplicate
        # The marker has no character that is special in a regular expression.
        jq = (
            f'.[] | select(({author}) and (.pull_request == null)) | (.body // "")'
            f' | capture("^{MARKER_PREFIX}(?<fp>[0-9a-f]{{{FINGERPRINT_LENGTH}}}) -->") | .fp | tojson'
        )
        path = f"{repo_path(self.slug)}/issues?state=all&per_page=100&labels={quote(LABEL_OBSERVED, safe='')}"
        output = self._gh(["api", "--paginate", path, "--jq", jq], timeout=LIST_TIMEOUT)
        found = set()
        for line in output.splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
                raise ValueError("unexpected issue list")
            found.add(value)
        self.known |= found

    def _open(self, item, issues, pr, job):
        self._ensure_label()
        title, body = render(item, issues, pr, job, self.run, self.trigger_label)
        # No assignee, and the one label is Factory's own origin label: never the trigger label.
        self._gh(
            [
                "api",
                "-X",
                "POST",
                f"{repo_path(self.slug)}/issues",
                "--silent",
                "-f",
                f"title={title}",
                "-f",
                f"body={body}",
                "-f",
                f"labels[]={LABEL_OBSERVED}",
            ]
        )
        self.known.add(item["fp"])
        self.opened += 1

    def _ensure_label(self):
        if self.ensured:
            return
        color, description = OBSERVED_LABEL_STYLE
        args = ["label", "create", LABEL_OBSERVED, "--force", "--color", color, "--description", description]
        if self.slug:
            args += ["-R", self.slug]
        self._gh(args)
        self.ensured = True

    def _gh(self, args, timeout=None):
        return command(["gh", *args], self.repo, timeout=CALL_TIMEOUT if timeout is None else timeout)


def _signature(job):
    items = job.get("observed") if isinstance(job, dict) else None
    return (
        job.get("status") if isinstance(job, dict) else None,
        job.get("pr") if isinstance(job, dict) else None,
        len(items) if isinstance(items, list) else 0,
    )


def for_run(repo, cfg, directory, fence=None):
    """The writer of one run's supervisor invocation, configured from the repository's config.

    Off without a configured repository: the target of a write is the configured one, never a checkout's guess.
    """
    slug = getattr(cfg, "repo", "") or None
    return ObservedIssues(
        repo,
        slug,
        Path(directory).name,
        enabled=wanted(cfg),
        trigger_label=getattr(cfg, "auto_build_label", DEFAULT_TRIGGER_LABEL),
        fence=fence,
        directory=directory,
    )

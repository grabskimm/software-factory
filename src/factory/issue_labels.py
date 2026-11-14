"""Status labels on the issues Factory works on (best effort, never fatal).

Each issue carries at most one ``factory:*`` status label showing where the job that
owns it stands. They are written with the supervisor's own ``gh`` (the Factory App
needs ``issues: write`` for exactly this, see ``tools/token_broker.py``); nothing else
is ever written to an issue except the one outcome comment of ``issue_comments.py`` (blocked,
needs a decision, failed), posted from the same hooks, with the same ``gh`` and the same
best-effort rules: no close, no title or body edit. The other writes Factory makes are a NEW issue
for what a worker observed outside its job (``observed_issues.py``), opened from the same hooks
when a job reaches a terminal status, and the phase issues of an approved epic with their status comment
(``epic_phases.py``, opened by the manager's epic pass); neither touches an existing issue and neither ever carries the
trigger label.

* A label is **never** a control of a job: no job, run or merge decision reads these labels
  (the manager's label watcher, ``trigger.py``, reads them only to leave an issue that carries
  one alone, because it is in flight or done, and the epic gate pass, ``epic_gate.py``, reads the three gate
  labels and ``factory-approve-plan`` to move an epic's issue between them), and a failed update must not stop a job. Every failure (403, 404, a
  timeout, a malformed answer) is swallowed after ONE fixed-format line on stderr per
  process, ``factory: issue label sync failed (<exception class>)``: no issue text, no
  response body, no token. After three consecutive failed writes the labeller pauses for a
  cool-down, so an unreachable GitHub cannot add a timeout per issue to a run. The updates
  that came in meanwhile are kept (the latest per issue) and written when the cool-down is
  over, through a probe: one more failure pauses it again at once. A lost repository lock
  stops it for good. The late ``factory-ignore`` read below is not a write: it goes on while
  the writes rest, and it has a streak and a rest of its own, so a GitHub that refuses
  writes (an App without ``issues: write``) never switches the opt-out check off.
* Updates hang off ``State.update`` (the one place job state is persisted: ``run``,
  ``retry``, ``recover`` and the recovery commands all set the observer), and are
  re-derived from the persisted state by ``reconcile`` at the start of every ``run``, so a
  missed update heals when the run is next run. ``status`` only reads (it takes no lock) and
  never writes labels. ``purge`` (and so the janitor's purge of a finished or abandoned run)
  releases the status labels of the run's issues afterwards, see ``settle_after_purge``.
* After a run goes idle (no supervisor, run not purged) the manager has the CLI ``reconcile`` read the live
  pull request of each published job: ``settle_idle`` sets ``factory:merged`` after a merge and clears the
  label after a close without merging, with the same fenced, best-effort, factory-ignore-respecting writes.
* An issue labelled ``factory-ignore`` is never given a status label, even if it is
  still listed in an old plan. The runner looks again just before it builds a job and
  before it publishes (``ignored_among``): a late label parks the job, or turns ``Closes`` into ``Refs``.
"""

import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

from . import epic, issue_comments
from .github import handoff_status, inspect_pr, lookup_failure, repo_path
from .locks import LockError
from .model import validate_plan
from .shell import command

IGNORE_LABEL = "factory-ignore"

LABEL_PLANNED = "factory:planned"
LABEL_BUILDING = "factory:building"
LABEL_IN_REVIEW = "factory:in-review"
LABEL_PR_OPEN = "factory:pr-open"
LABEL_MERGED = "factory:merged"
LABEL_BLOCKED = "factory:blocked"
LABEL_CI_FAILED = "factory:ci-failed"
# The epic gate (``epic_gate.py``): an ``epic`` disposition is labelled when it is planned, then the manager's
# gate pass moves it on. They exclude each other and every other status label.
LABEL_NEEDS_INFO = "factory:needs-info"
LABEL_AWAITING_APPROVAL = "factory:awaiting-approval"
LABEL_PLAN_APPROVED = "factory:plan-approved"
EPIC_LABELS = (LABEL_NEEDS_INFO, LABEL_AWAITING_APPROVAL, LABEL_PLAN_APPROVED)
# The origin label of an issue Factory opened itself for something a worker observed
# (``observed_issues.py``). It is NOT a status label (it is not in ``STATUS_LABELS``): it stays on the
# issue while a person triages it, a job builds it and it is closed, because it is also what finds
# the issue again when an identical observation comes up. ``trigger.py`` lists it among the labels
# that still allow a retry, and the planner's unfiltered listing leaves such an issue out until a
# person has applied the trigger label.
LABEL_OBSERVED = "factory:observed"
OBSERVED_LABEL_STYLE = ("fef2c0", "Factory: opened automatically for something a worker observed; a person decides what happens to it")
# The origin label of a phase issue Factory opened for an approved epic plan (``epic_phases.py``). Like
# ``factory:observed`` it is NOT a status label: it is not in ``STATUS_LABELS``, so a purge never removes it, it
# stays while the phase is built and closed, ``trigger.py`` lists it among the labels that still allow a retry
# (a person applies the trigger label to run the phase), and the planner's unfiltered listing leaves such an
# issue out until a person has applied the trigger label. Factory never applies the trigger label itself.
LABEL_EPIC_PHASE = epic.LABEL_PHASE
EPIC_PHASE_LABEL_STYLE = ("d4c5f9", "Factory: one phase of an approved epic plan; a person applies the trigger label to run it")

# name -> (colour, description). Fixed, so `label create --force` is idempotent and
# never recolours a label an operator already looks at.
STATUS_LABELS = {
    LABEL_PLANNED: ("c5def5", "Factory: a job is queued for this issue and has not started yet"),
    LABEL_BUILDING: ("fbca04", "Factory: a worker is building the change"),
    LABEL_IN_REVIEW: ("1d76db", "Factory: independent review, verification or publishing"),
    LABEL_PR_OPEN: ("5319e7", "Factory: pull request published"),
    LABEL_MERGED: ("0e8a16", "Factory: the pull request was merged"),
    LABEL_BLOCKED: ("d93f0b", "Factory: needs a human (blocked, failed, interrupted, stopped or needs a decision)"),
    LABEL_CI_FAILED: ("b60205", "Factory: CI failed on the pull request head"),
    LABEL_NEEDS_INFO: ("fef2c0", "Factory: epic plan posted, waiting for answers to its questions"),
    LABEL_AWAITING_APPROVAL: ("c2e0c6", "Factory: epic plan posted, waiting for a person to approve it"),
    LABEL_PLAN_APPROVED: ("0e8a16", "Factory: the epic plan was approved (its phase issues are opened one at a time)"),
}
# Labels a person applies; Factory only makes sure they exist (never applies or removes them, except the
# stale approval of a plan that was just replaced, see ``prepare_epics``).
HUMAN_LABELS = {epic.LABEL_APPROVE: (epic.APPROVE_COLOR, epic.APPROVE_DESCRIPTION)}

CALL_TIMEOUT = 30  # seconds per gh call
MAX_CONSECUTIVE_FAILURES = 3
COOLDOWN_SECONDS = 300  # how long the labeller rests after MAX_CONSECUTIVE_FAILURES
# Job statuses with a published pull request whose live state decides the label at purge.
PUBLISHED_STATUSES = ("pr_ready", "pr_incomplete")

# The job status -> label table. ``running`` and the ``waiting_<phase>`` statuses take
# their label from the phase; ``pr_ready`` becomes ``factory:ci-failed`` when live
# evidence says CI failed on the head. A status absent from here changes nothing.
JOB_STATUS_LABELS = {
    "queued": LABEL_PLANNED,
    "interrupted": LABEL_BLOCKED,
    # An operator stopped the job (``factory stop``): it waits for a person to retry or resume it.
    "stopped": LABEL_BLOCKED,
    "failed": LABEL_BLOCKED,
    "blocked": LABEL_BLOCKED,
    "needs_human_publish": LABEL_BLOCKED,
    "closed": LABEL_BLOCKED,
    "pr_ready": LABEL_PR_OPEN,
    "pr_incomplete": LABEL_PR_OPEN,
    "merged": LABEL_MERGED,
}
PHASE_LABELS = {
    "build": LABEL_BUILDING,
    "review": LABEL_IN_REVIEW,
    "verify": LABEL_IN_REVIEW,
    "publish": LABEL_IN_REVIEW,
}
# Plan dispositions that have no job. Both need a human (a blocked issue, or an investigation
# that found a gap and queued nothing), so neither may look like queued work: ``factory:planned``
# means only that a job exists. ``covered`` gets no label at all.
DISPOSITION_LABELS = {"investigation": LABEL_BLOCKED, "blocked": LABEL_BLOCKED}
# ``epic`` is deliberately NOT in that table: ``reconcile`` re-derives labels from it at the start of every
# ``run``, which would put the same label on an epic again and again and overwrite what the gate pass has
# moved it to. An epic gets its first label once (``disposition_label`` with ``fresh``), and the gate owns
# it after that; ``settle_after_purge`` does not release it either, so the gate survives the run directory.


def disposition_label(item, fresh=False, phase=False):
    """The status label of a plan disposition that has no job, or None.

    ``fresh`` is for a disposition that was just planned: an ``epic`` then starts as ``factory:needs-info``
    when it has questions and as ``factory:awaiting-approval`` when it has none. ``phase`` says the issue is a phase
    issue of an epic: it never gets a gate label (the planner's ``epic`` for it is blocked before this, see ``epic.downgrade``).
    """
    status = item.get("status")
    if status == epic.STATUS:
        if not fresh or phase:
            return None
        return LABEL_NEEDS_INFO if epic.question_count(item) else LABEL_AWAITING_APPROVAL
    return DISPOSITION_LABELS.get(status)


def _is_ignore_name(name):
    return name.strip().casefold() == IGNORE_LABEL


def is_ignored(issue):
    """True when the issue carries the factory-ignore label (case-insensitive, edges trimmed)."""
    labels = issue.get("labels") if isinstance(issue, dict) else None
    for label in labels if isinstance(labels, list) else []:
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str) and _is_ignore_name(name):
            return True
    return False


def drop_ignored(issues):
    return [issue for issue in issues if not is_ignored(issue)]


def _label_names(issue):
    labels = issue.get("labels") if isinstance(issue, dict) else None
    names = set()
    for label in labels if isinstance(labels, list) else []:
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str):
            names.add(name.strip().casefold())
    return names


def is_unadopted(issue, trigger_label):
    """True for an issue Factory opened itself that no person has asked to build yet.

    It carries ``factory:observed`` (an observation) or ``factory:epic-phase`` (a phase of an approved epic plan)
    and not the trigger label. The text of such an issue was written by a model (``observed_issues.py``,
    ``epic_phases.py``), so it reaches a plan only after a person has applied the trigger label.
    """
    names = _label_names(issue)
    return bool(names & {LABEL_OBSERVED, LABEL_EPIC_PHASE}) and str(trigger_label).strip().casefold() not in names


def drop_unadopted(issues, trigger_label):
    return [issue for issue in issues if not is_unadopted(issue, trigger_label)]


def label_for_job(job, handoff=None):
    """The one status label for a persisted job state, or None when it has none."""
    status = job.get("status")
    if status == "running" or (isinstance(status, str) and status.startswith("waiting_")):
        phase = (
            job.get("worker_phase") or job.get("pending_phase") or job.get("phase") or ""
        )
        if status != "running":
            phase = status.removeprefix("waiting_")
        return PHASE_LABELS.get(str(phase).split("-", 1)[0])
    if status == "pr_ready" and handoff == "ci_failed":
        return LABEL_CI_FAILED
    return JOB_STATUS_LABELS.get(status)


def sibling_issues(directory):
    """The issue numbers listed by the plans of the other runs next to ``directory``.

    Such a run may own the label of an issue now (a re-plan, a second run of the same
    issue), so purging this one must leave it alone. An unreadable plan names nothing.
    """
    run_dir = Path(directory)
    found = set()
    try:
        entries = sorted(run_dir.parent.iterdir())
    except OSError:
        return found
    for entry in entries:
        if entry.name == run_dir.name:
            continue
        try:
            other = json.loads((entry / "plan.json").read_text())
            numbers = [n for job in other["jobs"] for n in job["issues"]]
            numbers.extend(item["issue"] for item in other["dispositions"])
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
        # A JSON true is a bool, which is also an int: it is not an issue number.
        found.update(n for n in numbers if isinstance(n, int) and not isinstance(n, bool))
    return found


def read_run(directory):
    """``(plan, job states, sibling issues)`` of a run directory, or None without a usable plan.

    A plan without a state file means that no job ever started, so every job is queued.
    """
    run_dir = Path(directory)
    try:
        plan = validate_plan(json.loads((run_dir / "plan.json").read_text()))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    try:
        states = json.loads((run_dir / "state.json").read_text())["jobs"]
    except (OSError, ValueError, KeyError, TypeError):
        states = {}
    return plan, states if isinstance(states, dict) else {}, sibling_issues(run_dir)


_reported = False
_report_lock = threading.Lock()


def reset_report():
    """Allow the next failure to print its line again (tests; a process is one run)."""
    global _reported
    with _report_lock:
        _reported = False
    issue_comments.reset_report()


def _report(error):
    global _reported
    with _report_lock:
        if _reported:
            return
        _reported = True
    print(f"factory: issue label sync failed ({type(error).__name__})", file=sys.stderr)


class IssueLabels:
    """Keeps the status label of the issues of one plan in step with its jobs."""

    def __init__(self, repo, slug, plan, fence=None, clock=time.monotonic, commenter=None, observed=None):
        self.repo, self.slug, self.plan = repo, slug, plan
        # Optional ``IssueComments``: the outcome comment, written from the same hooks.
        self.commenter = commenter
        # Optional ``ObservedIssues``: the issues opened for what a worker observed, flushed from the
        # same hooks when a job reaches a terminal status.
        self.observed = observed
        # Called before every outward action; it raises LockError when the repository
        # lock is no longer provably held (``Runner.assert_held``).
        self.fence = fence
        self.clock = clock
        self.owned = {job["id"]: list(job["issues"]) for job in plan["jobs"]}
        self.applied = {}  # issue -> the label this process last set (or saw) on it
        self.pending = {}  # issue -> the label wanted while the labeller was resting
        self.missed = {}  # issue -> the label whose write failed since the last success
        self.ignored = set()
        self.closed = set()
        self.ensured = set()
        self.failures = 0
        self.lost = False  # the repository lock is gone: permanent
        self.suspended_until = None  # clock() value the cool-down ends at, or None
        # The factory-ignore re-check only reads, so its failures are counted apart from the
        # label writes': neither streak starts or ends the other's rest.
        self.read_failures = 0
        self.read_suspended_until = None
        self.mutex = threading.RLock()

    @property
    def disabled(self):
        """True while nothing is written: the lock is lost or the cool-down is running."""
        return self.lost or self._cooling()

    def _cooling(self):
        return self.suspended_until is not None and self.clock() < self.suspended_until

    def _wake(self):
        """Leave the cool-down once it is over, on probation: one more failure rests again."""
        if self.suspended_until is not None and not self._cooling():
            self.suspended_until = None
            self.failures = MAX_CONSECUTIVE_FAILURES - 1

    # ---- what to write
    def on_job(self, key, previous, current):
        """``State.observer``: sync only when the persisted job's label really changed."""
        if key not in self.owned:
            return
        label = label_for_job(current)
        if label is not None and label != label_for_job(previous):
            self.sync({number: label for number in self.owned[key]})
        self._announce_job(key, current)
        self._observe_job(key, previous, current)

    def sync_plan(self):
        """Label every issue of a fresh plan: planned (it has a job) or blocked (it has none)."""
        wanted, epics = {}, {}
        phases = {
            issue.get("number") for issue in self.plan.get("issues", ()) if isinstance(issue, dict) and LABEL_EPIC_PHASE in _label_names(issue)
        }
        for item in self.plan["dispositions"]:
            label = LABEL_PLANNED if item["status"] == "job" else disposition_label(item, fresh=True, phase=item["issue"] in phases)
            if label:
                (epics if item["status"] == epic.STATUS else wanted)[item["issue"]] = label
        self.sync(wanted)
        self.prepare_epics(self.plan["dispositions"], fresh=True)
        self.announce_dispositions(self.plan["dispositions"])
        # The first gate label comes last: the gate must never read an old plan comment under a new label
        # (an answer to the old questions would move a re-planned issue on before the new plan is posted).
        if epics:
            self.sync(epics)

    def stale_issues(self, states):
        """The issues of owned jobs blocked because a dependency was closed without merging.

        Such a job can never run again; when a re-plan has taken the issue into another run, this run's
        claim on it is stale (``reconcile(..., skip=)``).
        """
        found = set()
        for key, numbers in self.owned.items():
            job = states.get(key)
            if isinstance(job, dict) and job.get("status") == "blocked" and issue_comments.closed_dependency(job.get("error")):
                found.update(numbers)
        return found

    def reconcile(self, states, handoffs=None, skip=()):
        """Re-derive every issue's label from the persisted job states (self-healing).

        ``handoffs`` maps job ids to a live ``handoff_status`` (``ci_failed`` and so on). ``skip`` holds
        issues this run no longer decides: their label and outcome comment belong to the run that re-planned them.
        """
        skip = set(skip)
        handoffs = handoffs or {}
        # Look at GitHub again rather than trust what this process last wrote.
        self.applied.clear()
        wanted = {}
        for item in self.plan["dispositions"]:
            label = DISPOSITION_LABELS.get(item["status"])
            if label:
                wanted[item["issue"]] = label
        for key, numbers in self.owned.items():
            if key in states:
                label = label_for_job(states[key], handoffs.get(key))
                if label is not None:
                    wanted.update({number: label for number in numbers})
        wanted = {number: label for number, label in wanted.items() if number not in skip}
        self.sync(wanted)
        kept = [item for item in self.plan["dispositions"] if item["issue"] not in skip]
        self.prepare_epics(kept)
        self.announce_dispositions(kept)
        for key in self.owned:
            if isinstance(states.get(key), dict):
                self._announce_job(key, states[key], skip)
        self.observe_jobs(states)

    # ---- the issues opened for observations (observed_issues.py): best effort, never raises
    def observe_jobs(self, states):
        """Open what the jobs observed and no issue carries yet (a flush that was missed is made up for here).

        What a flush could not settle (GitHub refused, an issue could not be read) is asked for again by the
        manager's idle reconcile (``observed_issues.pending``), whether or not the run has a published pull request.
        Never narrowed by the issues another run's plan lists: opening an issue contends for no label, the job did
        its work on these issues, and two runs of one issue would otherwise each leave the observation to the other.
        """
        for key in self.owned:
            if isinstance(states.get(key), dict):
                self._observe_job(key, None, states[key])

    def _observe_job(self, key, previous, current):
        observed = self.observed
        if observed is None:
            return
        try:
            if not observed.due(previous, current, key):
                return
            numbers = list(self.owned.get(key, ()))
            if not numbers:
                # The job owns no issue to report on: settled.
                observed.dismiss(current["observed"], key)
                return
            with self.mutex:
                if self.lost:
                    return
                # Never about something a person opted out, and not asked for again: that is settled. An
                # issue that cannot be read now counts as opted out for this moment only (the observations
                # stay in the job's state, and the idle reconcile tries again).
                ignored, unread = self.ignored_among(numbers, with_unread=True)
                if ignored:
                    observed.dismiss(current["observed"], key)
                    return
                if unread:
                    return
                pr = re.search(r"/pull/(\d{1,10})\b", str(current.get("pr") or ""))
                observed.flush(current["observed"], numbers, int(pr.group(1)) if pr else None, key)
        except Exception as error:  # noqa: BLE001
            observed.report(error)

    # ---- the outcome comment (issue_comments.py): best effort, never raises
    def announce_dispositions(self, items):
        """Comment on the issues the plan blocked, only investigated or called an epic (no job owns them)."""
        for item in items:
            kind = issue_comments.DISPOSITION_KINDS.get(item["status"])
            if kind:
                self._announce(item["issue"], kind, item["status"], item.get("reason"), (), item.get("epic"))

    def prepare_epics(self, items, fresh=False):
        """Make sure the human approval label exists for each epic disposition (best effort, fenced).

        ``fresh`` (a plan or fragment just made) also takes a stale ``factory-approve-plan`` off the issue:
        an approval given before this plan existed approves nothing. A re-run of the same plan
        (``reconcile``) never touches the label, so a real approval stays.
        """
        with self.mutex:
            for item in items:
                number = item.get("issue")
                if item.get("status") != epic.STATUS or number in self.ignored:
                    continue
                if self.lost or self._cooling():
                    return
                self._wake()
                self._attempt(self._prepare_epic, number, fresh)

    def _prepare_epic(self, number, fresh):
        self._ensure(epic.LABEL_APPROVE)
        if not fresh:
            return
        names = self._current(number)
        if any(_is_ignore_name(name) for name in names):
            self.ignored.add(number)
        elif epic.LABEL_APPROVE in names:
            path = f"{repo_path(self.slug)}/issues/{number}/labels"
            self._gh(["api", "-X", "DELETE", f"{path}/{quote(epic.LABEL_APPROVE, safe='')}"])

    def _announce_job(self, key, job, skip=()):
        status = job.get("status")
        kind = issue_comments.JOB_STATUS_KINDS.get(status)
        if kind:
            pr = re.search(r"/pull/(\d{1,10})\b", str(job.get("pr") or ""))
            extra = (int(pr.group(1)),) if pr else ()
            for number in self.owned.get(key, ()):
                if number not in skip:
                    self._announce(number, kind, status, job.get("error"), extra)

    def _refs(self, extra=()):
        """The issue and pull request numbers Factory itself produced for this plan."""
        numbers = set(extra)
        try:
            numbers.update(item["number"] for item in self.plan.get("issues", ()))
            for job in self.plan["jobs"]:
                numbers.update(job.get("context_issues", ()))
        except (KeyError, TypeError):
            pass
        return {n for n in numbers if type(n) is int}

    def _announce(self, number, kind, code, reason, extra, payload=None):
        commenter = self.commenter
        if commenter is None or not commenter.active:
            return
        try:
            with self.mutex:
                commenter.announce(
                    number, kind, code, reason, self._refs(extra), allow=self._may_comment, payload=payload
                )
        except Exception as error:  # noqa: BLE001
            issue_comments._report(error)

    def _may_comment(self, number):
        """A comment is a write: only on an issue read just now that is open and not ignored."""
        if self.lost or not self._read(self._look, number):
            return False
        return number not in self.ignored and number not in self.closed

    def live_handoffs(self, states, budget_seconds=30.0, statuses=("pr_ready",)):
        """``(handoffs, failures)``: ``handoff_status`` of each published, unmerged job.

        Needs live PR evidence. ``statuses`` are the persisted job statuses that count as
        published (``pr_ready`` by default; purge also looks at ``pr_incomplete``). A lookup that
        fails is recorded in ``failures`` (``github.lookup_failure``: job, exception class,
        reason code; never the message) and the persisted state decides that job's label. A
        timeout stops the refresh and records every job it did not reach, as does running out of
        the time budget.
        """
        found = {}
        failures = []
        pending = [
            key
            for key, job in states.items()
            if job.get("status") in statuses and job.get("pr") and not self.disabled
        ]
        deadline = time.monotonic() + budget_seconds
        for index, key in enumerate(pending):
            if time.monotonic() >= deadline:
                error, reason, unreached = None, "time_budget_exhausted", pending[index:]
            else:
                try:
                    found[key] = handoff_status(inspect_pr(self.repo, states[key]["pr"]))
                    continue
                except subprocess.TimeoutExpired as caught:
                    failures.append(lookup_failure(key, caught))
                    error, reason = caught, "not_reached_after_timeout"
                    unreached = pending[index + 1 :]
                except Exception as caught:  # noqa: BLE001
                    # Evidence is optional: without it the persisted state decides.
                    failures.append(lookup_failure(key, caught))
                    continue
            failures.extend(lookup_failure(other, error, reason) for other in unreached)
            break
        return found, failures

    def ignored_among(self, numbers, with_unread=False):
        """The issues among ``numbers`` that carry factory-ignore right now.

        Each is read again (fenced, one ``gh`` call), whether or not the label writes are
        resting: a refused write says nothing about whether GitHub can be read. A read that
        cannot be done (the lock is lost, GitHub does not answer, the reads themselves rest
        after three failures in a row) says nothing new: the job goes on, and only what this
        process saw earlier still counts.

        With ``with_unread`` the answer is ``(ignored, unread)``, ``unread`` being the other
        issues whose label could not be read this time. A caller whose wrong guess cannot be
        undone (closing an issue) treats those as ignored too.
        """
        wanted = sorted(set(numbers))
        unread = set()
        with self.mutex:
            for number in wanted:
                if self.lost or not self._read(self._look, number):
                    unread.add(number)
            found = {number for number in wanted if number in self.ignored}
        return (found, unread - found) if with_unread else found

    def clear(self, numbers, respect_ignore=False):
        """Remove every status label from each issue (best effort, fenced).

        With ``respect_ignore`` an issue that carries factory-ignore is left alone.
        """
        with self.mutex:
            for number in sorted(set(numbers)):
                self.pending.pop(number, None)
                if self.lost or self._cooling():
                    continue
                self._wake()
                self._attempt(self._release, number, respect_ignore)

    def settle_idle(self, states, handoffs, skip=()):
        """Follow the pull requests of published jobs after they left the supervisor's hands.

        For a run that is not active and not purged (the manager calls this under the repository
        lease, through ``reconcile``): a job whose ``pr_ready``/``pr_incomplete`` pull request is
        live-``merged`` puts ``factory:merged`` on its issues; one closed without merging clears
        their ``factory:*`` status label, unless another job of the run still holds the issue.
        Anything else (open, unknown, a failed lookup) changes nothing. ``skip`` holds the issues
        that a sibling run lists: they are not touched. Same fenced, best-effort writes as always;
        an issue labelled factory-ignore is left alone (``_write`` and ``_release`` check it).
        """
        handoffs = handoffs or {}
        merged, closed, held = set(), set(), set()
        for key, numbers in self.owned.items():
            job = states.get(key)
            job = job if isinstance(job, dict) else {}
            live = handoffs.get(key)
            published = job.get("status") in PUBLISHED_STATUSES and job.get("pr")
            if published and live == "merged":
                merged.update(numbers)
            elif published and live == "closed":
                closed.update(numbers)
            else:
                held.update(numbers)
        skip = set(skip)
        self.sync({number: LABEL_MERGED for number in merged - skip})
        self.clear(closed - merged - held - skip, respect_ignore=True)

    def settle_after_purge(self, states, handoffs=None, skip=()):
        """Release the labels of a purged run's issues, except those another run may own.

        ``states`` is the run's persisted ``jobs`` map (a missing job counts as queued).
        ``handoffs`` maps job ids to the live ``handoff_status`` of their pull request.
        A merged job leaves ``factory:merged``; a job whose pull request is still open
        keeps its label (the pull request stays, so does the sign that it is in flight);
        any other job, and the issues the plan only investigated or blocked, lose it. An
        issue the plan called an ``epic`` keeps its gate label (``DISPOSITION_LABELS``): that
        state lives on GitHub, not in the run.
        ``skip`` holds the issues that a sibling run lists: their labels are not touched.
        """
        handoffs = handoffs or {}
        release, merged = set(), set()
        for item in self.plan["dispositions"]:
            if item["status"] in DISPOSITION_LABELS:
                release.add(item["issue"])
        for key, numbers in self.owned.items():
            job = states.get(key)
            job = job if isinstance(job, dict) else {}
            live = handoffs.get(key)
            if job.get("status") == "merged" or live == "merged":
                merged.update(numbers)
            elif job.get("status") in PUBLISHED_STATUSES and job.get("pr") and live != "closed":
                continue
            else:
                release.update(numbers)
        skip = set(skip)
        self.sync({number: LABEL_MERGED for number in merged - skip})
        self.clear(release - merged - skip)

    # ---- how to write
    def sync(self, wanted):
        """Write the wanted label (issue -> label); while resting, keep it for later."""
        with self.mutex:
            if self.lost:
                return
            if self._cooling():
                self.pending.update(wanted)
                return
            self._wake()
            # What was kept while resting goes first; a newer wish for an issue replaces it.
            todo = sorted({**self.pending, **wanted}.items())
            self.pending = {}
            for index, (number, label) in enumerate(todo):
                if self.lost:
                    return
                if not self._apply(number, label) and self._cooling():
                    # The failures that led to the rest cost their updates, too.
                    self.pending.update({**self.missed, **dict(todo[index:])})
                    self.missed = {}
                    return

    def _attempt(self, action, *args):
        """Run one outward action: fenced, never raising. True when it was carried out."""
        try:
            if self.fence is not None:
                self.fence()
            action(*args)
            self.failures = 0
            self.missed = {}
            return True
        except LockError:
            # The repository lock is lost: do nothing more, and say nothing (the lock
            # layer already stops the supervisor).
            self.lost = True
        except Exception as error:  # noqa: BLE001
            _report(error)
            self.failures += 1
            if self.failures >= MAX_CONSECUTIVE_FAILURES:
                self.suspended_until = self.clock() + COOLDOWN_SECONDS
        return False

    def _read(self, action, *args):
        """Run one read-only outward action: fenced, never raising, apart from the write streak.

        True when it was carried out. It neither wakes the writes nor counts toward their
        failures; after three failed reads in a row the reads rest for a cool-down of their
        own (then one probe: a single further failure rests again), so an unreachable GitHub
        still adds no timeout per issue to a run.
        """
        if self.read_suspended_until is not None:
            if self.clock() < self.read_suspended_until:
                return False
            self.read_suspended_until = None
            self.read_failures = MAX_CONSECUTIVE_FAILURES - 1
        try:
            if self.fence is not None:
                self.fence()
            action(*args)
            self.read_failures = 0
            return True
        except LockError:
            self.lost = True
        except Exception as error:  # noqa: BLE001
            _report(error)
            self.read_failures += 1
            if self.read_failures >= MAX_CONSECUTIVE_FAILURES:
                self.read_suspended_until = self.clock() + COOLDOWN_SECONDS
        return False

    def _apply(self, number, label):
        if number in self.ignored or self.applied.get(number) == label:
            return True
        if self._attempt(self._write, number, label):
            return True
        self.missed[number] = label
        return False

    def _write(self, number, label):
        self._ensure(label)
        names = self._current(number)
        if any(_is_ignore_name(name) for name in names):
            self.ignored.add(number)
            return
        path = f"{repo_path(self.slug)}/issues/{number}/labels"
        if label not in names:
            self._gh(["api", "-X", "POST", path, "-f", f"labels[]={label}"])
        for stale in sorted(set(names) & (set(STATUS_LABELS) - {label})):
            self._gh(["api", "-X", "DELETE", f"{path}/{quote(stale, safe='')}"])
        self.applied[number] = label

    def _look(self, number):
        """Note whether the issue is closed or carries factory-ignore now (either may have changed)."""
        state, names = self._issue(number)
        if state == "closed":
            self.closed.add(number)
        else:
            self.closed.discard(number)
        if any(_is_ignore_name(name) for name in names):
            self.ignored.add(number)
        else:
            self.ignored.discard(number)

    def _release(self, number, respect_ignore=False):
        path = f"{repo_path(self.slug)}/issues/{number}/labels"
        names = self._current(number)
        if respect_ignore and any(_is_ignore_name(name) for name in names):
            self.ignored.add(number)
            return
        for stale in sorted(set(names) & set(STATUS_LABELS)):
            self._gh(["api", "-X", "DELETE", f"{path}/{quote(stale, safe='')}"])
        self.applied.pop(number, None)

    def _gh(self, args):
        return command(["gh", *args], self.repo, timeout=CALL_TIMEOUT)

    def _ensure(self, label):
        if label in self.ensured:
            return
        color, description = STATUS_LABELS.get(label) or HUMAN_LABELS[label]
        args = ["label", "create", label, "--force", "--color", color]
        args += ["--description", description]
        if self.slug:
            args += ["-R", self.slug]
        self._gh(args)
        self.ensured.add(label)

    def _current(self, number):
        return self._issue(number)[1]

    def _issue(self, number):
        """The issue's state and label names, from one GET."""
        output = self._gh(
            [
                "api",
                f"{repo_path(self.slug)}/issues/{number}",
                "--jq",
                "{state: .state, labels: [.labels[].name]}",
            ]
        )
        found = json.loads(output)
        state = found.get("state") if isinstance(found, dict) else None
        names = found.get("labels") if isinstance(found, dict) else None
        if not isinstance(state, str) or not isinstance(names, list) or any(not isinstance(n, str) for n in names):
            raise ValueError("unexpected issue answer")
        return state.lower(), names

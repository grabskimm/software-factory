"""Phase 2 of the @mention follow-up: the oldest queued request becomes a session on the pull request's own branch.

Held by ``operations.Operations`` (``FollowupServing(self)``: composition, so the oversized ``Operations`` class does
not grow) and called from its follow-up pass (``_followup_repo``), after the requests were recorded and independent of
``auto_build``. At most one session starts per pass and per repository.

A session is a **retry of the job that published the pull request**, through the same internal path as the HTTP
``retry`` (``Manager.retry_followup``, no HTTP route of its own) followed by the usual ``run``: the supervisor syncs the
branch to the base, the builder works on it, and review, verification and publication run on a new head (the gate is
posted again for that head only). The request's outcome is recorded in ``followups/pr-<N>.json`` as a fixed code. When
the pass has settled requests it then says so on the pull request (phase 3, ``followup_reply.py``): one fixed-template
comment per pull request, edited in place, after every state change of the pass was written, best effort, and only for the
outcomes that have a reply (never for ``pr-closed``, ``not-authorised``, ``expired`` or ``cli-refused``).

The guard chain for starting, in order (a failed guard leaves the request queued unless it says refused):

1. the request is younger than ``followup.MAX_REQUEST_AGE_SECONDS``; otherwise it is REFUSED ``expired`` (an old ask
   is never acted on, however long the pause or the budget kept it);
2. the trigger pause is not set, no supervisor child of this manager works in the repository, the repository lease is
   free, the repository is not archived (``skipped reason=paused|busy|archived``);
3. the run that holds the job is still there, the job is ``pr_ready`` or ``pr_incomplete``, the pull request is open and
   its head is exactly the commit the job retained (a push by anybody else, an update-branch or an auto-merge update
   changes it). Otherwise the request is REFUSED with a fixed code (``run-gone``, ``job-status``, ``pr-closed``,
   ``head-moved``): no worktree is built from a head nobody reviewed and no new single-job run is started;
4. the author of the request still holds ``followup_min_permission``, read again now and not when the request was
   recorded; otherwise REFUSED ``not-authorised``. A failing lookup raises, so the request waits (fail closed);
5. the run's cumulative cost (the original build included) is below ``run_budget_usd`` (``skipped reason=over-budget``);
6. the per-pull-request lock ``<run>/<job>/<comment id>`` is written before anything is started, so a crash or a second
   pass cannot start a second session for the pull request.

A busy lease, a refused retry or a failed start keeps the request and waits (``trigger.Backoff``); a retry the CLI
refused three times is given up (``cli-refused``). A session that ended is settled from ``state.json`` (never from
memory): a new commit is ``pushed``, none is ``no-change`` (job and issue status are unchanged), a failed or blocked
job is ``session-failed``; the lock is released and the context file removed. A lock whose job is queued or waiting is
finished by starting its ``run``, under the same budget rule as the restart recovery, with a pause that doubles after
each start and at most ``MAX_RESUME_STARTS`` times; one whose job is running or interrupted without a process is left
to the restart recovery (``auto_resume_after_restart``). Either kind is released as ``session-lost`` once
``STALE_LOCK_SECONDS`` passed since the session started, so a lock that can never be finished does not hold up the pull
requests of the repository. A job that never got past the retry (no new commit) is first put back as the published job
it was (the CLI's ``followup-release``, under the repository lease), so a later mention is served as it would have been.

Only fixed words, numbers and run and job ids reach a log line; a comment's text goes to the context file only.
"""

from __future__ import annotations

from pathlib import Path

from . import followup, followup_context, followup_reply, trigger
from .manager import JOB_ID, ApiError, _list_runs, escape_field

STALE_LOCK_SECONDS = 3600.0
MAX_RESUME_STARTS = 3  # restarts of a session's supervisor that stopped before the job's next phase, per request
MAX_RELEASE_TRIES = 3  # failed ``followup-release`` commands (the lease free) before the lock is released without it
PUBLISHED = ("pr_ready", "pr_incomplete")
WAITING_FOR_RUN = ("queued", "waiting_build", "waiting_review", "waiting_verify", "waiting_publish")
IN_DOUBT = ("running", "interrupted")  # a process may still come back (restart recovery) until the lock is stale
FINISHED_BADLY = ("failed", "blocked", "needs_human_publish")
CLOSED = ("merged", "closed")


class FollowupServing:
    """The serve half of the follow-up pass; ``host`` (the ``Operations``) supplies the clock, the manager and the lines."""

    def __init__(self, host: object) -> None:
        self.host = host
        self._wait = trigger.Backoff(host._clock)  # (repo, pr) -> pause after a start that did not happen
        self._release_wait = trigger.Backoff(host._clock)  # (repo, pr) -> pause after a ``followup-release`` that failed
        self._replies: dict = {}  # repo -> its ``followup_reply.FollowupReplies`` (one for the life of the manager)
        self._outbox: dict = {}  # pr -> (outcome, new head) settled in this pass and still to be told on the pull request

    # -- entry --------------------------------------------------------------------------

    def serve(self, repo: str, cfg: object, conf: followup.FollowupSettings, paths: object, reader: object) -> None:
        """Settle the sessions in progress, then start at most one queued request, then tell the pull requests the outcomes.

        The replies come last and in a ``finally``: every state change of the pass is on disk by then, a pass that
        raised after settling a request still says so, and a reply that fails changes nothing (``_reply_all``).
        """
        replier = self._replies.get(repo)
        if replier is not None:
            replier.new_cycle()  # the per-cycle cap of the replies counts from zero in every pass
        self._outbox = {}
        try:
            self._serve(repo, cfg, conf, paths, reader)
        finally:
            self._reply_all(repo, cfg, conf, reader)

    def _serve(self, repo: str, cfg: object, conf: followup.FollowupSettings, paths: object, reader: object) -> None:
        numbers = followup.numbers_with_state(paths.root)
        if not numbers:
            return
        who = f"repo={escape_field(repo)}"
        states = {}
        for number in numbers:
            try:
                states[number] = followup.load_state(paths.root / followup.STATE_DIR, number)
            except (followup.StateError, OSError):
                self.host._say(repo, f"followup {who} pr={number} state unreadable", ("followup-state", number))
        locked = False
        for number, state in list(states.items()):
            if state["lock"] is not None:
                locked = self._followup_settle(repo, cfg, conf, paths, number, state) or locked
                states[number] = followup.load_state(paths.root / followup.STATE_DIR, number)  # as settled
        if locked or self.host._stopping():
            return
        now = self.host._clock()
        queue = sorted(
            (item["queued_at"], number, item["comment_id"])
            for number, state in states.items()
            for item in state["requests"]
            if followup.due(item, now) and state["lock"] is None
        )
        for _, number, comment_id in queue:
            if self._wait.remaining((repo, number)) > 0:
                continue
            try:
                self._followup_start(repo, cfg, conf, paths, reader, number, comment_id)
            except Exception as error:  # GitHub, the token broker, a full disk: back off, the request stays
                if not self.host._stopping():
                    delay = self._wait.failed((repo, number))
                    self.host._line(f"followup {who} pr={number} start failed error={type(error).__name__} backoff={delay:g}s")
            return  # one candidate per pass

    # -- settling -----------------------------------------------------------------------

    def _followup_settle(self, repo: str, cfg: object, conf: followup.FollowupSettings, paths: object, number: int, state: dict) -> bool:
        """What became of the session a lock stands for; True while the lock is still needed."""
        lock = followup.parse_lock(state["lock"])
        request = followup.request_for(state, lock[2]) if lock else None
        if lock is None or request is None:
            self._followup_finish(repo, paths, number, None, followup.FAILED, followup.OUTCOME_LOST)
            return False
        run, job, comment_id = lock
        active = self.host.manager.children.active_for_repo(repo)
        if active is not None and active.run == run:
            return True  # the session (or something else of this run) is working
        run_dir = paths.run_dir(run)
        document = trigger.read_plan(run_dir / "state.json")
        jobs = document.get("jobs") if isinstance(document, dict) else None
        item = jobs.get(job) if isinstance(jobs, dict) else None
        if not isinstance(item, dict):
            self._followup_finish(repo, paths, number, comment_id, followup.FAILED, followup.OUTCOME_LOST, run_dir, job)
            return False
        status = item.get("status")
        record = item.get("followup") if isinstance(item.get("followup"), dict) else {}
        mine = record.get("request") == comment_id
        if status in PUBLISHED:
            if not mine:  # the retry never took effect (a crash after the lock): the request is served again
                self._followup_requeue(repo, paths, number, comment_id, run_dir, job)
                return False
            same = item.get("commit") == request.get("commit_before") or record.get("outcome") == followup.OUTCOME_NO_CHANGE
            outcome = followup.OUTCOME_NO_CHANGE if same else followup.OUTCOME_PUSHED
            head = "" if same else item.get("commit")  # the new head is the job's own record, never anything from a comment
            self._followup_finish(repo, paths, number, comment_id, followup.DONE, outcome, run_dir, job, head)
            return False
        if status in FINISHED_BADLY and mine:
            self._followup_finish(repo, paths, number, comment_id, followup.FAILED, followup.OUTCOME_FAILED, run_dir, job)
            return False
        if status in CLOSED:
            self._followup_finish(repo, paths, number, comment_id, followup.REFUSED, followup.OUTCOME_PR_CLOSED, run_dir, job)
            return False
        stale = (followup.age(request.get("started_at", ""), self.host._clock()) or 0.0) > STALE_LOCK_SECONDS
        if status in WAITING_FOR_RUN and mine:
            if stale or request.get("resumes", 0) >= MAX_RESUME_STARTS:  # a start that never takes hold is given up
                return self._followup_give_up(repo, cfg, paths, number, comment_id, run_dir, job)
            self._followup_resume_start(repo, cfg, paths, number, run, job, comment_id)
            return True
        if status in IN_DOUBT and not stale:
            return True  # the restart recovery may still resume it
        self._followup_finish(repo, paths, number, comment_id, followup.FAILED, followup.OUTCOME_LOST, run_dir, job)
        return False

    def _followup_give_up(
        self, repo: str, cfg: object, paths: object, number: int, comment_id: int, run_dir: Path, job: str
    ) -> bool:
        """A session whose start never took hold: put the job back as published, then release the lock as lost.

        Without this the job would stay ``queued`` behind a follow-up record whose context file is gone: a later mention
        is refused ``job-status``, ``retry`` refuses a queued job and a plain ``run`` fails in the builder. The CLI's
        ``followup-release`` restores the status and phase of the published job, under the repository lease, and changes
        nothing when the job already built something (``run`` finishes that one). True while the lease is held
        elsewhere or the command is being retried (a pause that doubles; ``MAX_RELEASE_TRIES`` failures with a free
        lease release the lock without it, so a command that can never succeed does not hold the repository).
        """
        key = (repo, number)
        if self._release_wait.remaining(key) > 0 or self.host.manager.lease_state(cfg, paths, warn=False) != "free":
            return True
        result = self.host._cli(repo, run_dir, ["followup-release", job])
        if result is None:
            return True  # no command slot (or the manager is closing): the next pass asks again
        if result.timed_out or result.returncode != 0:
            delay = self._release_wait.failed(key)
            if self._release_wait.failures(key) < MAX_RELEASE_TRIES:
                self.host._line(f"followup repo={escape_field(repo)} pr={number} release failed backoff={delay:g}s")
                return True
        self._release_wait.succeeded(key)
        self._followup_finish(repo, paths, number, comment_id, followup.FAILED, followup.OUTCOME_LOST, run_dir, job)
        return False

    def _followup_finish(
        self,
        repo: str,
        paths: object,
        number: int,
        comment_id: int | None,
        status: str,
        outcome: str,
        run_dir: Path | None = None,
        job: str | None = None,
        head: object = "",
    ) -> None:
        """Record the outcome, release the lock, remove the context file, then one fixed line (in that order).

        A request that was found is also put in the pass's outbox for its reply (``head``: the new commit of ``pushed``).
        """
        found = {"request": False}

        def change(state: dict) -> None:
            request = followup.request_for(state, comment_id) if comment_id is not None else None
            if request is not None:
                request["status"], request["outcome"] = status, outcome
                found["request"] = True
            state["lock"] = None

        followup.mutate(paths.root, number, change)
        self._wait.succeeded((repo, number))  # a pause of the finished session does not hold up the next request
        if run_dir is not None and job is not None:
            followup_context.remove(run_dir, job)
        word = {followup.DONE: "done", followup.REFUSED: "refused", followup.FAILED: "failed"}[status]
        self.host._line(f"followup repo={escape_field(repo)} pr={number} {word} outcome={outcome}")
        if found["request"]:
            self._outbox[number] = (outcome, head)

    def _followup_requeue(self, repo: str, paths: object, number: int, comment_id: int, run_dir: Path, job: str) -> None:
        def change(state: dict) -> None:
            request = followup.request_for(state, comment_id)
            if request is not None:
                request["status"] = followup.QUEUED
            state["lock"] = None

        followup.mutate(paths.root, number, change)
        followup_context.remove(run_dir, job)
        self.host._line(f"followup repo={escape_field(repo)} pr={number} requeued reason=retry-not-applied")

    def _followup_resume_start(
        self, repo: str, cfg: object, paths: object, number: int, run: str, job: str, comment_id: int
    ) -> None:
        """The job is queued behind a retry whose ``run`` did not start (busy lease, shutdown, restart): start it now.

        The run budget applies as it does to the restart recovery (a drained session is not paid for again), and every
        start that took hold is counted on the request: ``_followup_settle`` gives up after ``MAX_RESUME_STARTS``.
        """
        key = (repo, number)
        if self._wait.remaining(key) > 0 or self.host.manager.trigger_paused():
            return
        if self.host.manager.children.active_for_repo(repo) is not None:
            return
        if self.host.manager.lease_state(cfg, paths, warn=False) != "free":
            return
        if self._followup_over_budget(repo, cfg, paths.run_dir(run)):
            delay = self._wait.failed(key)  # the status is not asked every pass
            self.host._say(repo, f"followup repo={escape_field(repo)} pr={number} skipped reason=over-budget", ("followup", number))
            self._line_backoff(repo, number, delay)
            return
        if not self._followup_run(repo, cfg, number, self.host.manager.target_for(repo, run), job, resume=True):
            return

        def count(state: dict) -> None:
            request = followup.request_for(state, comment_id)
            if request is not None:
                request["resumes"] = request.get("resumes", 0) + 1

        followup.mutate(paths.root, number, count)
        self._wait.failed(key)  # the pause before the next resume doubles each time (see ``_followup_run``)

    def _followup_over_budget(self, repo: str, cfg: object, run_dir: Path) -> bool:
        """Whether the run's cumulative cost (the original build included) has reached ``run_budget_usd``."""
        policy = self.host._policy(cfg)
        return policy.run_budget_usd is not None and self.host._over_budget(run_dir, policy, self.host._status(repo, run_dir) or {})

    # -- starting -----------------------------------------------------------------------

    def _followup_start(
        self, repo: str, cfg: object, conf: followup.FollowupSettings, paths: object, reader: object, number: int, comment_id: int
    ) -> None:
        who = f"repo={escape_field(repo)} pr={number}"
        key = ("followup", number)
        state = followup.load_state(paths.root / followup.STATE_DIR, number)
        request = followup.request_for(state, comment_id)
        if request is None:
            return
        waited = followup.age(request["queued_at"], self.host._clock())
        if waited is None or waited > followup.MAX_REQUEST_AGE_SECONDS:  # an unreadable time counts as old
            self._followup_refuse(repo, paths, number, comment_id, followup.OUTCOME_EXPIRED)
            return
        if self.host.manager.trigger_paused():
            self.host._say(repo, f"followup {who} skipped reason=paused", key)
            return
        if self.host.manager.children.active_for_repo(repo) is not None or self.host.manager.lease_state(cfg, paths, warn=False) != "free":
            self.host._say(repo, f"followup {who} skipped reason=busy", key)
            return
        if reader.repo_facts().archived:
            self.host._say(repo, f"followup {who} skipped reason=archived", key)
            return
        found = self._followup_owner(repo, paths, number)
        if found is None:
            self._followup_refuse(repo, paths, number, comment_id, followup.OUTCOME_RUN_GONE)
            return
        run, job, item = found
        run_dir = paths.run_dir(run)
        if item.get("status") not in PUBLISHED or not isinstance(item.get("commit"), str):
            self._followup_refuse(repo, paths, number, comment_id, followup.OUTCOME_JOB_STATUS)
            return
        facts = reader.pull_facts(number)
        if facts.state != "open":
            self._followup_refuse(repo, paths, number, comment_id, followup.OUTCOME_PR_CLOSED)
            return
        if facts.head != item["commit"]:
            self._followup_refuse(repo, paths, number, comment_id, followup.OUTCOME_HEAD_MOVED)
            return
        levels = {request["user"]: reader.permission(request["user"])}  # a failure raises: the request waits
        if not trigger.permission_ok(levels[request["user"]], conf.min_permission):
            self._followup_refuse(repo, paths, number, comment_id, followup.OUTCOME_NOT_AUTHORISED)
            return
        if self._followup_over_budget(repo, cfg, run_dir):
            delay = self._wait.failed((repo, number))  # the request stays; the status is not asked every pass
            self.host._say(repo, f"followup {who} skipped reason=over-budget", key)
            self._line_backoff(repo, number, delay)
            return
        document, served = self._followup_context(reader, conf, state, request, item["commit"], levels)
        followup_context.write(run_dir, job, document)
        now = self.host._clock()

        def lock(current: dict) -> None:
            mine = followup.request_for(current, comment_id)
            current["lock"] = followup.make_lock(run, job, comment_id)
            mine["status"] = followup.RUNNING
            mine["attempts"] = mine.get("attempts", 0) + 1
            mine["commit_before"] = item["commit"]
            mine["started_at"] = followup.stamp(now)
            mine["not_before"] = ""

        followup.mutate(paths.root, number, lock)  # before anything starts: a crash cannot start it twice
        target = self.host.manager.target_for(repo, run)
        try:
            reply = self.host.manager.retry_followup(target, job)
            refused = reply.status != 200
            counted = True
        except ApiError:
            refused, counted = True, False  # the lane was busy or the manager is closing: not the CLI's refusal
        if refused:
            self._followup_retry_refused(repo, paths, number, comment_id, run_dir, job, counted)
            return

        def advance(current: dict) -> None:
            for kind, highest in served.items():
                current["served"][kind] = max(current["served"][kind], highest)

        followup.mutate(paths.root, number, advance)
        self._followup_run(repo, cfg, number, target, job)

    def _followup_run(self, repo: str, cfg: object, number: int, target: object, job: str, resume: bool = False) -> bool:
        """``run`` for the retried job, with the repository's session limit; a refused start waits (the job stays queued).

        True when the run was started. A start that is a resume of an earlier one is followed by a pause that doubles
        with each (60 s, 120 s, ...), so a supervisor that cannot take hold (the token broker or the environment is
        down for a few minutes) is not asked again every pass and the bounded number of starts outlives a short outage.
        """
        who = f"repo={escape_field(repo)} pr={number}"
        conf = trigger.settings_of(cfg)
        try:
            self.host.manager.start_run(target, {"concurrency": conf.concurrency, "timeout": conf.timeout})
        except ApiError as error:
            delay = self._wait.failed((repo, number))
            self.host._line(f"followup {who} start deferred status={error.status} backoff={delay:g}s")
            return False
        if not resume:
            self._wait.succeeded((repo, number))
        self.host._started(repo, f"followup {who} started run={escape_field(target.run)} job={escape_field(job)}")
        return True

    def _line_backoff(self, repo: str, number: int, delay: float) -> None:
        self.host._say(repo, f"followup repo={escape_field(repo)} pr={number} backoff={delay:g}s", ("followup-backoff", number))

    def _followup_retry_refused(
        self, repo: str, paths: object, number: int, comment_id: int, run_dir: Path, job: str, counted: bool
    ) -> None:
        delay = self._wait.failed((repo, number))
        now = self.host._clock()
        final = {"given_up": False}

        def change(state: dict) -> None:
            request = followup.request_for(state, comment_id)
            state["lock"] = None
            if request is None:
                return
            if not counted:
                request["attempts"] = max(0, request.get("attempts", 1) - 1)
            if request.get("attempts", 0) >= followup.MAX_START_ATTEMPTS:
                request["status"], request["outcome"] = followup.REFUSED, followup.OUTCOME_CLI_REFUSED
                final["given_up"] = True
            else:
                request["status"], request["not_before"] = followup.QUEUED, followup.after(now, delay)

        followup.mutate(paths.root, number, change)
        followup_context.remove(run_dir, job)
        who = f"repo={escape_field(repo)} pr={number}"
        if final["given_up"]:
            self.host._line(f"followup {who} refused reason={followup.OUTCOME_CLI_REFUSED}")
        else:
            self.host._line(f"followup {who} retry refused backoff={delay:g}s")

    def _followup_refuse(self, repo: str, paths: object, number: int, comment_id: int, code: str) -> None:
        """A permanent refusal: a fixed code in the state file, one fixed line, no session, no new run, no retry."""

        def change(state: dict) -> None:
            request = followup.request_for(state, comment_id)
            if request is not None:
                request["status"], request["outcome"] = followup.REFUSED, code

        followup.mutate(paths.root, number, change)
        self.host._line(f"followup repo={escape_field(repo)} pr={number} refused reason={code}")
        self._outbox[number] = (code, "")

    # -- the reply on the pull request ---------------------------------------------------

    def _reply_all(self, repo: str, cfg: object, conf: followup.FollowupSettings, reader: object) -> None:
        """Tell each pull request settled in this pass what became of its request; best effort, never raises.

        Fixed text only (``followup_reply.py``). A repository with ``followup_reply = false``, a manager that is closing
        and an outcome without a reply cost nothing. A failure is one rate-limited line with the exception class; the
        outcome stays in the state file and the reply is not tried again.
        """
        outbox, self._outbox = self._outbox, {}
        todo = [(number, *item) for number, item in outbox.items() if item[0] in followup_reply.REPLIES]
        if not todo or not conf.reply or self.host._stopping():
            return
        try:
            replier = self._replies.get(repo)
            if replier is None or replier.reader is not reader:
                replier = self._replies[repo] = followup_reply.FollowupReplies(
                    reader, getattr(cfg, "repo", None), self.host._clock, self.host.manager.env
                )
            replier.configure(conf.reply)
            for number, outcome, head in todo:
                who = f"repo={escape_field(repo)} pr={number}"
                result = replier.reply(number, outcome, head)
                if result == "replied":
                    self.host._line(f"followup {who} replied outcome={outcome}")
                elif result == "failed":
                    self.host._say(repo, f"followup {who} reply failed error={replier.last_error}", ("followup-reply", number))
                elif result in ("cap", "cooldown", "closed"):
                    self.host._line(f"followup {who} reply skipped reason={result}")
        except Exception as error:  # a reply is never worth a failed pass
            self.host._say(repo, f"followup repo={escape_field(repo)} reply failed error={type(error).__name__}", "followup-reply")

    # -- context and owner --------------------------------------------------------------

    def _followup_context(
        self, reader: object, conf: followup.FollowupSettings, state: dict, request: dict, head: str, known: dict
    ) -> tuple:
        """``(context document, new served cursors)``: the request, the authorised comments after the cursor, failing checks.

        ``known`` holds the roles already looked up for this start (the request's author), so none is asked twice.
        """
        number = state["pr"]
        levels: dict = dict(known)

        def allowed(row: object) -> bool:
            if row.login not in levels:
                levels[row.login] = reader.permission(row.login)  # a failure raises: nothing starts, the request waits
            return trigger.permission_ok(levels[row.login], conf.min_permission)

        rows: list = []
        for kind in trigger.REMARK_KINDS:
            after = state["served"][kind]
            if kind == request["kind"]:
                after = min(after, request["comment_id"] - 1)  # the triggering comment is always read
            rows.extend(reader.remark_texts(number, kind, after))
        checks = []
        try:
            for failed in reader.failed_checks(head)[: followup_context.MAX_CHECKS]:
                try:
                    tail = reader.job_log_tail(failed.job_id, followup_context.MAX_LOG_CHARS)
                except Exception:  # an expired or missing log is no excerpt, not a failure
                    tail = ""
                checks.append((failed.name, failed.conclusion, tail))
        except Exception:  # the checks are context, not a condition
            checks = []
        document = followup_context.assemble(
            number=number,
            trigger_kind=request["kind"],
            trigger_id=request["comment_id"],
            comments=rows,
            served=state["served"],
            allowed=allowed,
            handle=conf.handle,
            checks=checks,
        )
        served = {kind: 0 for kind in trigger.REMARK_KINDS}
        for row in document["comments"]:
            served[row["kind"]] = max(served[row["kind"]], row["id"])
        return document, served

    @staticmethod
    def _followup_owner(repo: str, paths: object, number: int) -> tuple | None:
        """``(run, job id, job state)`` of the newest started run holding the job that published pull request ``number``."""
        for entry in sorted(_list_runs(paths), key=lambda item: item["run"], reverse=True):
            if not entry["started"]:
                continue
            document = trigger.read_plan(paths.run_dir(entry["run"]) / "state.json")
            jobs = document.get("jobs") if isinstance(document, dict) else None
            for job in sorted(jobs) if isinstance(jobs, dict) else ():
                if isinstance(jobs[job], dict) and JOB_ID.fullmatch(job) and trigger.pull_number(document, job, repo) == number:
                    return entry["run"], job, jobs[job]
        return None

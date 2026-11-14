"""Completing an incomplete pull request by hand: the ``factory-verified`` label.

Factory publishes a pull request whose review or verification did not pass as a draft that
says ``Refs #N``: GitHub closes an issue only for a closing keyword, and Factory never
claims a completion it could not verify. A person who verified the change by other means
can say so by applying ``COMPLETION_LABEL`` to that pull request. This module is the one
step that answers it (``cli.reconcile_command`` runs it under the repository lease):

* **Authority**: the same as for the build trigger. The user who applied the label (the newest
  ``labeled`` event, ``trigger.latest_actor``) must be a ``User`` (never a bot) whose repository
  permission reaches ``auto_build_min_permission`` (``trigger.permission_ok``), on a repository
  where that minimum is allowed (``trigger.minimum_allowed``). Anyone else is ignored, silently.
* **The head**: only while the pull request head is still the commit Factory published (the job's
  ``commit``). A head that moved is refused with one fixed line on stderr and nothing is written.
* **What is written**: the first line of the body, ``Refs #N`` -> ``Closes #N`` for the job's
  issues, except those that carry ``factory-ignore`` or whose label cannot be read (they stay
  ``Refs``; for a phase issue of an epic the parent is ``refs``, or ``closes`` for the last phase when the live check in
  ``epic_phases.phase_link`` says so now); then ``factory/gate`` is re-posted as ``success`` on that exact head, with a
  description that names the person, unless that status is already the newest one (a pass that finds
  nothing to change posts nothing, so the label's staying on the pull request is harmless). Nothing is pushed, the head, the draft state and the
  ``factory:ready`` label are never touched: the person decides about the merge.

Everything is best effort like the status labels: a failed read or write is one fixed line
(the exception class only), three failures in a row end the step, and nothing here stops a loop.
"""

import re
import sys

from . import epic
from .github import GATE_CONTEXT, post_gate_status, refresh_notice
from .locks import LockError
from .trigger import Actor, minimum_allowed, permission_ok, pull_number

COMPLETION_LABEL = "factory-verified"
MAX_FAILURES = 3

REFUSED_LINE = "factory: verified completion refused: the pull request head is not the published commit (#{number})"
BODY_LINE = "factory: verified completion skipped: the pull request body no longer starts as published (#{number})"
FAILED_LINE = "factory: verified completion failed ({kind})"

_SHA = re.compile(r"[A-Za-z0-9._-]{1,64}")


def candidates(slug, states):
    """``(job id, pull request number, published commit)`` of every incomplete, published job."""
    found = []
    for key, job in states.items():
        if not isinstance(job, dict) or job.get("status") != "pr_incomplete":
            continue
        number = pull_number({"jobs": {key: job}}, key, slug)
        commit = job.get("commit")
        if number is not None and isinstance(commit, str) and _SHA.fullmatch(commit):
            found.append((key, number, commit))
    return found


def rewritten_body(body, issues, closing, link=None):
    """``body`` with its first line made ``Closes``; None when that line is not the published one.

    ``closing(issue)`` says which issues may be closed. The line is rebuilt from the job's own
    issue numbers (never from the old text), and only a body whose first line is exactly what an
    incomplete publication wrote is touched. The rest of the body is returned unchanged.

    ``link`` (``epic.PhaseLink``) is the parent link of a phase issue's job, computed live by the caller: the line is then
    built by ``epic.first_line`` exactly as the publication built it, and both forms of the parent (``refs`` and ``closes``)
    are recognised as Factory's own, so that a pull request demoted by the epic pass can still be completed.
    """
    first, separator, rest = body.partition("\n")
    carriage = "\r" if first.endswith("\r") else ""
    first = first.removesuffix("\r")
    published = epic.first_line(issues, lambda _: False, link)
    wanted = epic.first_line(issues, closing, link)
    if first not in (published, wanted, *epic.linked_forms(issues, link)):
        return None
    return f"{wanted}{carriage}{separator}{refresh_notice(wanted, rest)}"


def complete_verified(slug, cwd, selected, states, reader, settings, labels, fence=None, out=None, links=None):
    """Answer the completion label on the open pull requests of this run's incomplete jobs.

    ``reader`` is a ``trigger.GithubReader``, ``settings`` the repository's ``TriggerSettings``,
    ``labels`` the run's ``IssueLabels`` (for the late ``factory-ignore`` read). ``links(job)`` (optional) gives the
    parent link of a phase issue's job (``epic_phases.phase_link``), computed live now. Returns the
    pull request numbers completed.
    """
    out = out or sys.stderr
    jobs = {job["id"]: job for job in selected["jobs"]}
    todo = [item for item in candidates(slug, states) if item[0] in jobs]
    done, failures = [], 0
    if not todo:
        return done

    def guarded(action, *args):
        nonlocal failures
        try:
            if fence is not None:
                fence()
            result = action(*args)
            failures = 0
            return True, result
        except LockError:
            failures = MAX_FAILURES
        except Exception as error:  # noqa: BLE001
            print(FAILED_LINE.format(kind=type(error).__name__), file=out)
            failures += 1
        return False, None

    ok, facts = guarded(reader.repo_facts)
    if not ok or facts.archived or not minimum_allowed(settings.min_permission, facts.private):
        return done
    ok, labelled = guarded(reader.open_pulls, COMPLETION_LABEL)
    if not ok:
        return done
    for key, number, commit in todo:
        if failures >= MAX_FAILURES:
            break
        if number not in labelled:
            continue
        ok, pull = guarded(reader.pull_facts, number)
        if not (ok and pull.state == "open"):
            continue
        ok, actor = guarded(reader.label_actor, number, COMPLETION_LABEL)
        if not (ok and isinstance(actor, Actor) and actor.is_user):
            continue
        ok, level = guarded(reader.permission, actor.login)
        if not (ok and permission_ok(level, settings.min_permission)):
            continue
        if pull.head != commit:
            print(REFUSED_LINE.format(number=number), file=out)
            continue
        issues = jobs[key]["issues"]
        ok, found = guarded(labels.ignored_among, issues, True)
        ignored, unread = found if ok else (set(), set(issues))
        ok, link = guarded(links, jobs[key]) if links is not None else (True, None)
        if not ok:
            continue
        body = rewritten_body(pull.body, issues, lambda n: n not in ignored and n not in unread, link)
        if body is None:
            print(BODY_LINE.format(number=number), file=out)
            continue
        if body != pull.body and not guarded(reader.set_pull_body, number, body)[0]:
            continue
        description = f"verified by hand by @{actor.login} ({COMPLETION_LABEL}) @{commit[:7]}"
        # Idempotent: the label stays on the pull request, so every pass sees it again. GitHub keeps at
        # most 1000 statuses per commit and context; post only when the gate is not already this one.
        # A failed read writes nothing.
        ok, current = guarded(reader.status_of, commit, GATE_CONTEXT)
        if not ok:
            continue
        if current == ("success", description):
            continue
        posted = guarded(post_gate_status, cwd, slug, commit, "success", description)[0]
        if posted:
            done.append(number)
    return done


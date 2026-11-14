"""Capture GitHub evidence and group it with a read-only repository inspection."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

from . import epic, plan_failure
from .agent import guidance_with_skills, repo_guidance, run_agent
from .artifact_paths import relative_path
from .github import repo_args, repo_path
from .issue_comments import DEFAULT_TRIGGER_LABEL, without_factory_comments
from .issue_labels import drop_ignored, drop_unadopted, is_ignored
from .model import validate_plan
from .model_choice import assign, blocked, screen
from .phases import AgentOptions
from .podexecution import EXECUTION_INPROCESS, agent_kwargs
from .shell import command  # noqa: F401  (re-exported: other modules import it from here)

MAX_FETCH = 1000  # the most issues one plan will look through to find `limit` that are not ignored


ISSUE_FIELDS = "number,title,body,comments,labels,url"
# What is read of an issue whose text the manager already verified (``verified`` below): never its title, body or comments.
STAMPED_FIELDS = "number,labels,url"

# The planner's own usage record (``plan``) and one per ``plan --append-to`` (``planner-usage-<name>.json``):
# the run's planner cost is their sum, so an append never overwrites an earlier record.
USAGE_FILE = "planner-usage.json"
USAGE_PATTERN = re.compile(r"planner-usage(?:-[A-Za-z0-9][A-Za-z0-9_.-]{0,79})?\.json")


def planner_usage_records(directory, since=None):
    """Every readable planner usage record of the run, in file name order (best effort, read-only).

    With ``since`` (a time in seconds since the epoch) only the records whose file was last written then or later:
    the record carries no time of its own, so its file's modification time is the time of the session.
    """
    try:
        names = sorted(n for n in os.listdir(directory) if USAGE_PATTERN.fullmatch(n))
    except OSError:
        return []
    records = []
    for name in names:
        try:
            if since is not None and (Path(directory) / name).stat().st_mtime < since:
                continue
            value = json.loads((Path(directory) / name).read_text())
        except (OSError, ValueError):
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _dropped(number, reason):
    """One fixed-format line per issue left out of an explicit list: ids and a reason, never text."""
    print(f"factory: plan issue={number} dropped reason={reason}", file=sys.stderr)


def _issue_head(repo, number, slug):
    """(state, is a pull request, label names) of one issue, or None when GitHub has no such issue.

    Only these three facts are read here: the title and body of an issue that is going to be
    dropped are never fetched. A 404 is the only failure that means "missing"; anything else
    (rate limit, network) raises and fails the plan rather than silently planning less.
    """
    jq = "{state: .state, pr: (.pull_request != null), labels: [.labels[].name]}"
    try:
        output = command(["gh", "api", f"{repo_path(slug)}/issues/{number}", "--jq", jq], repo)
    except subprocess.CalledProcessError as error:
        if "404" in str(error.stderr or "") or "Not Found" in str(error.stderr or ""):
            return None
        raise
    head = json.loads(output)
    return head["state"], bool(head["pr"]), head["labels"]


def snapshot_issues(repo, numbers, slug=None, verified=None):
    """Capture exactly the listed issues (``plan --issues``), in the order given.

    An issue that is missing, a pull request, closed or labelled ``factory-ignore`` is
    dropped with a fixed-format line on stderr, and its text is never fetched. At least one
    must remain. Ignore always wins: the label is checked on the cheap read and again on the
    full one, so a label added in between is still honoured.

    ``verified`` maps an issue number to the ``(title, body)`` the manager checked against the ledger before it started a phase issue
    itself (``epic_phases.prepare_start``). For such an issue the planner's input is THAT text and an empty comment list: its title, body
    and comments are not read from GitHub at all (only its state and labels, so ``factory-ignore`` and a close are still honoured), so an
    edit or a comment made after the phase issue was opened never reaches ``issues.json``.
    """
    texts = verified or {}
    issues = []
    for number in numbers:
        head = _issue_head(repo, number, slug)
        if head is None:
            _dropped(number, "missing")
        elif head[1]:
            _dropped(number, "not-an-issue")
        elif head[0] != "open":
            _dropped(number, "closed")
        elif is_ignored({"labels": head[2]}):
            _dropped(number, "ignored")
        else:
            fields = STAMPED_FIELDS if number in texts else ISSUE_FIELDS
            fetched = json.loads(command(["gh", "issue", "view", str(number), "--json", fields, *repo_args(slug)], repo))
            if number in texts:
                fetched = {**fetched, "title": texts[number][0], "body": texts[number][1], "comments": []}
            if is_ignored(fetched):
                _dropped(number, "ignored")
            else:
                issues.append(fetched)
    if not issues:
        raise ValueError("none of the listed issues can be planned")
    return issues


def snapshot(repo, destination, limit, slug=None, numbers=None, trigger_label=DEFAULT_TRIGGER_LABEL, verified=None):
    """Capture the open issues a human has not opted out with the ``factory-ignore`` label.

    With ``numbers`` (``plan --issues``) only those issues are captured, see ``snapshot_issues``;
    ``limit`` is then not used.

    An issue Factory opened itself for a worker's observation (``factory:observed``, text written by a
    model) is left out of this unfiltered listing until a person has applied ``trigger_label`` to it.
    A person who names it in ``numbers`` has chosen it already.

    The plain issue listing is used (not GitHub's search index, which can lag behind a
    new issue or label) and ignored issues are dropped here. ``--limit`` must count only
    the issues that remain, so more are fetched until ``limit`` survive or the listing
    ends. An ignored issue is never written to ``issues.json``.
    """
    if numbers is not None:
        issues = snapshot_issues(repo, numbers, slug, verified)
        return _write_snapshot(repo, destination, issues, slug)
    wanted = limit
    while True:
        fetched = json.loads(
            command(
                [
                    "gh",
                    "issue",
                    "list",
                    "--state",
                    "open",
                    "--limit",
                    str(wanted),
                    "--json",
                    "number,title,body,comments,labels,url",
                    *repo_args(slug),
                ],
                repo,
            )
        )
        issues = drop_unadopted(drop_ignored(fetched), trigger_label)
        if len(issues) >= limit or len(fetched) < wanted or wanted >= MAX_FETCH:
            break
        wanted = min(MAX_FETCH, max(wanted * 2, limit + len(fetched) - len(issues)))
    return _write_snapshot(repo, destination, issues[:limit], slug)


def _write_snapshot(repo, destination, issues, slug):
    prs = json.loads(
        command(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "open",
                "--limit",
                "1000",
                "--json",
                "number,title,body,headRefName,url",
                *repo_args(slug),
            ],
            repo,
        )
    )
    destination.mkdir(parents=True, exist_ok=True)
    # Factory's own outcome comments are never planner input (see without_factory_comments).
    issues = [without_factory_comments(issue) for issue in issues]
    (destination / "issues.json").write_text(json.dumps(issues, indent=2))
    (destination / "open-prs.json").write_text(json.dumps(prs, indent=2))
    return issues, prs


def _tidy_path(value):
    """``value`` spelled canonically when the only problem is spelling (``docs/decisions/``, ``./src``, ``a//b``, ``a/./b``).

    Anything that is not just a spelling problem is returned unchanged, so validation still rejects it: an absolute path, a
    ``..`` component, a glob, a backslash, an empty string or a non-string.
    """
    if not isinstance(value, str) or not value or "\\" in value or any(c in value for c in "*?[]"):
        return value
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        return value
    return str(path)


def normalize_likely_paths(result):
    """Spell every job's ``likely_paths`` canonically, in place, and return ``result``.

    The planner is told to name narrow directories, and a directory is naturally written ``docs/decisions/``; the plan validator
    (``artifact_paths.relative_path``) accepts only the canonical spelling, so one trailing slash used to reject the whole plan, on
    every retry (seen on a real issue: six plans in a row). The meaning is the same path prefix.
    """
    jobs = result.get("jobs") if isinstance(result, dict) else None
    for job in jobs if isinstance(jobs, list) else ():
        paths = job.get("likely_paths") if isinstance(job, dict) else None
        if isinstance(paths, list):
            job["likely_paths"] = list(dict.fromkeys(_tidy_path(path) for path in paths if not isinstance(path, (list, dict))))
    return result


def _paths_invalid(result):
    """Whether some job's ``likely_paths`` is not a list of literal, canonical repository-relative paths."""
    jobs = result.get("jobs") if isinstance(result, dict) else None
    for job in jobs if isinstance(jobs, list) else ():
        paths = job.get("likely_paths") if isinstance(job, dict) else None
        for path in paths if isinstance(paths, list) else ():
            try:
                relative_path(path)
            except ValueError:
                return True
    return False


def add_artifact_paths(result, run_name, roots=()):
    """Reserve unique artifact paths under the configured roots (default none).

    The roots are recorded in the plan so later checks use the same set.
    """
    result["artifact_roots"] = list(roots)
    for job in result["jobs"]:
        name = f"factory-{run_name}-{job['id']}"
        paths = [Path(root, name).as_posix() for root in roots]
        job["artifact_paths"] = paths
        job["likely_paths"] = list(dict.fromkeys(job["likely_paths"] + paths))
    return result


def session_limit_text(timeout):
    """The per-session limit as prompt text (the same ``--timeout`` bounds every build, review and verify session)."""
    seconds = int(timeout)
    minutes = f"{seconds / 60:g}"
    return (
        f"\nSession time limit: the agent sessions of the run that follows are killed after {seconds} seconds "
        f"({minutes} minutes) each (the build, the review and the verification of a job are separate sessions); "
        "a job whose build does not finish in time ends failed with no pull request. Do not group issues into one "
        "job when the group would need more than that: give them separate jobs (chained with depends_on) and say in "
        "the disposition reason that you separated them because of the limit. One issue is always one job: for a "
        "single issue you expect not to fit, say so in its job's disposition reason, or block it asking for smaller issues."
    )


def _index_entry(issue, directive):
    """One row of the planner's issue index; ``choice`` (validated tokens only) only when labelled."""
    entry = {"number": issue["number"], "title": issue["title"], "preview": issue["body"][:1200]}
    if directive is not None and not directive.empty:
        entry["choice"] = directive.token()
    return entry


def planner_evidence(destination, issues, refused):
    """The issues file the planner may read: ``issues.json``, or a copy without the refused issues.

    An issue whose model or effort labels were refused is never planned, so its text is not handed to
    the planner either.
    """
    if not refused:
        return destination / "issues.json"
    path = destination / "issues-planned.json"
    path.write_text(json.dumps([i for i in issues if i["number"] not in refused], indent=2))
    return path


def run_screened(
    repo,
    destination,
    issues,
    prs,
    timeout,
    options,
    user,
    group,
    context="",
    authority=None,
    epic_on=False,
    execution=EXECUTION_INPROCESS,
):
    """Plan the issues whose model and effort labels are authorised and valid: ``(result, usage)``.

    ``authority`` (``trigger.LabelAuthority``) says whether whoever applied an issue's choice labels
    may choose; without it an issue that carries one is refused, see ``model_choice.screen``.
    An issue with a refused label gets a ``blocked`` disposition and never reaches the planner (usage
    is None when no issue was left to plan, and no session ran). Every job of the result records its
    resolved ``choice`` (``model_choice.assign``); a job whose issues disagree is blocked with its
    dependents. ``epic_on`` is passed on to the planner (``run_planner``). The caller validates the plan.
    """
    chosen = options or AgentOptions()
    directives, refused = screen(issues, chosen, authority)
    for number, codes in refused.items():
        # Fixed tokens only: never the label's text.
        print(f"factory: issue directive refused issue={number} reason={','.join(codes)}", file=sys.stderr)
    planned = [issue for issue in issues if issue["number"] not in refused]
    if planned:
        result, usage = run_planner(
            repo,
            destination,
            planned,
            prs,
            timeout,
            chosen,
            user,
            group,
            context=context,
            directives=directives,
            evidence=planner_evidence(destination, issues, refused),
            epic_on=epic_on,
            execution=execution,
        )
    else:
        result, usage = {"version": 1, "jobs": [], "dispositions": []}, None
    jobs, dispositions = assign(result["jobs"], result["dispositions"], directives, chosen)
    result["jobs"] = jobs
    result["dispositions"] = [
        *dispositions,
        *(blocked(issue["number"], refused[issue["number"]]) for issue in issues if issue["number"] in refused),
    ]
    return result, usage


def run_planner(
    repo,
    destination,
    issues,
    prs,
    timeout,
    options,
    user,
    group,
    context="",
    directives=None,
    evidence=None,
    epic_on=False,
    execution=EXECUTION_INPROCESS,
):
    """Ask the planner about ``issues`` (already captured in ``destination``): ``(result, usage)``.

    ``context`` is extra prompt text, appended last (``plan --append-to`` passes the run's jobs).
    ``directives`` (issue number -> ``model_choice.Directive``) marks the issues that carry a model or
    effort label, so the planner can keep ones that differ in separate jobs. ``evidence`` is the
    issues file the planner may read (default ``issues.json``, see ``planner_evidence``). ``epic_on`` keeps
    the epic paragraph of the prompt (``epic.can_plan``); without it the planner is told nothing about epics
    and gets the prompt it always had. ``execution`` is the repository's setting of that name, which
    selects the backend that runs the session (``podexecution``; ``inprocess`` is the call as it always was).
    """
    marked = directives or {}
    issues_file = evidence or destination / "issues.json"
    prompt = epic.planner_prompt((Path(__file__).parent / "prompts/planner.md").read_text(), epic_on)
    prompt += session_limit_text(timeout)
    # Keep large historical incidents and unrelated PR bodies out of the initial
    # context. The planner can read the captured evidence without network access.
    prompt += f"\nFull issue bodies/comments: {issues_file.resolve()}"
    prompt += "\nFull open PR evidence: " + str(
        (destination / "open-prs.json").resolve()
    )
    prompt += "\nSelected issue index (untrusted task data):\n" + json.dumps(
        [
            _index_entry(i, marked.get(i["number"]))
            for i in issues
        ]
    )
    prompt += "\nOpen PR index (untrusted task data):\n" + json.dumps(
        [
            {key: p.get(key) for key in ("number", "title", "headRefName", "url")}
            for p in prs
        ]
    )
    prompt += context
    # The planner is read-only with no network, so it cannot fetch the evidence
    # itself: the two captured files outside its worktree are readable.
    result, usage = run_agent(
        prompt,
        repo,
        destination / "planner",
        "plan",
        timeout,
        options=options,
        guidance=guidance_with_skills(repo_guidance(repo), repo, "HEAD", getattr(options, "repo_skills", True), getattr(options, "repo_skills_budget_kib", 256)),
        read_paths=(issues_file, destination / "open-prs.json"),
        user=user,
        group=group,
        **agent_kwargs(execution, job_id="planner", attempt=0),
    )
    return normalize_likely_paths(result), usage


def plan(
    repo,
    destination,
    limit=30,
    timeout=1800,
    use_snapshot=False,
    options=None,
    slug=None,
    artifact_roots=(),
    user=None,
    group=None,
    numbers=None,
    authority=None,
    epic_on=False,
    trigger_label=DEFAULT_TRIGGER_LABEL,
    phases=None,
    execution=EXECUTION_INPROCESS,
    verified=None,
):
    """Plan the open issues (or ``numbers``) and write ``plan.json``.

    ``verified`` is the text of a phase issue the manager started itself, see ``snapshot_issues``.

    ``phases`` (``cli.phase_checker``) maps the captured issues to the phase issues of an epic among them
    (``epic_phases.plan_stamps``): their ``epic`` is blocked, each gets its stamp on its job and none is planned
    together with other issues. Without it no issue is treated as a phase.
    """
    plan_failure.clear(destination)
    try:
        if use_snapshot:
            captured = json.loads((destination / "issues.json").read_text())
            issues = [without_factory_comments(issue) for issue in drop_unadopted(drop_ignored(captured), trigger_label)]
            if issues != captured:
                # The planner is pointed at this file: it must not hold an ignored issue, nor an observed
                # issue nobody has adopted, nor Factory's own outcome comments (an older snapshot may still
                # carry them).
                (destination / "issues.json").write_text(json.dumps(issues, indent=2))
            prs = json.loads((destination / "open-prs.json").read_text())
        else:
            issues, prs = snapshot(repo, destination, limit, slug, numbers, trigger_label, verified)
    except Exception:
        plan_failure.write(destination, plan_failure.SNAPSHOT_FAILED)
        raise
    try:
        result, usage = run_screened(
            repo,
            destination,
            issues,
            prs,
            timeout,
            options,
            user,
            group,
            authority=authority,
            epic_on=epic_on,
            execution=execution,
        )
    except subprocess.TimeoutExpired:  # ``agent.AgentTimeout`` is one
        plan_failure.write(destination, plan_failure.TIMEOUT)
        raise
    except (KeyError, TypeError, AttributeError):  # the planner's result is not shaped like a plan
        plan_failure.write(destination, plan_failure.SCHEMA_INVALID)
        raise
    except Exception:
        plan_failure.write(destination, plan_failure.PLANNER_ERROR)
        raise
    try:
        # Epic mode cannot work here: today's ``blocked``. Where it can, an epic that breaks the bounds the
        # schema does not carry is ``blocked`` too, so that it cannot fail the plan of the whole batch.
        stamps = phases(issues) if phases is not None else {}
        epic.downgrade(result, only_invalid=epic_on, nested=stamps)
        epic.bind_phases(result, stamps)
        # Source issue text always comes from the captured GitHub snapshot.
        result["issues"] = issues
        validate_plan(result)
        add_artifact_paths(result, destination.name, artifact_roots)
        validate_plan(result)
    except Exception:
        plan_failure.write(
            destination, plan_failure.INVALID_PLAN_PATHS if _paths_invalid(result) else plan_failure.SCHEMA_INVALID
        )
        raise
    (destination / "plan.json").write_text(json.dumps(result, indent=2) + "\n")
    if usage is not None:
        (destination / USAGE_FILE).write_text(json.dumps(usage))
    return result

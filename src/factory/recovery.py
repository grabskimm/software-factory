"""Explicit checkpoint recovery; callers hold the repository factory lock."""

import copy
import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from . import gitsafe, podhandle
from .artifact_paths import artifact_roots, relative_path
from .github import parse_gh_json
from .model import verify_required
from .planner import command
from .state import pid_status

# The refusal of a pod that is not read as dead (alive, or the launcher cannot say); the operations loop classifies it
# as the cause ``pod_live`` (a wait, not a failure), so the text is a constant that its test reads.
MSG_POD_LIVE = "{key}: pod worker is still alive or cannot be verified"


def _policy(cfg):
    """(verify.when, acceptance.mode, base_branch) from a RepoConfig; defaults without one."""
    return (
        getattr(cfg, "verify_when", "feature"),
        getattr(cfg, "acceptance_mode", "tests"),
        getattr(cfg, "base_branch", "main"),
    )


def _repo_git(repo, *args):
    """git in the supervisor's own clone (gitsafe: hooks and fsmonitor off, no inherited GIT_DIR)."""
    return gitsafe.repo_git(args, repo).strip()


def _gitdir(repo, item, worktree):
    """The trusted git directory of a retained worktree, from the main clone and the recorded one.

    A worktree the clone does not know, or whose recorded directory differs, is a refusal.
    """
    try:
        return gitsafe.gitdir_of(repo, worktree, item.get("gitdir"))
    except gitsafe.GitSafetyError as error:
        raise ValueError(str(error)) from None


def _git(worktree, gitdir, *args):
    """git in a worktree a worker touched, through gitsafe; a changed ``.git`` pointer is a refusal."""
    try:
        return gitsafe.git(args, worktree, gitdir).strip()
    except gitsafe.GitSafetyError as error:
        raise ValueError(str(error)) from None


def _commit(worktree, gitdir, value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("checkpoint must identify an exact commit SHA")
    if _git(worktree, gitdir, "rev-parse", "--verify", f"{value}^{{commit}}") != value:
        raise ValueError("checkpoint is not a commit")
    return value


def _context(repo, plan, state, job_id, allow_dirty_artifacts=False, execution=False):
    jobs = {job["id"]: job for job in plan["jobs"]}
    if job_id not in jobs or job_id not in state.data["jobs"]:
        raise ValueError(f"unknown job: {job_id}")
    for key, item in state.data["jobs"].items():
        # "unknown" (recorded on another host) must never be read as dead.
        if (not execution or key == job_id) and item.get("pid") and pid_status(
            item["pid"], item.get("pid_host")
        ) != "dead":
            raise ValueError(f"{key}: worker is still alive or cannot be verified")
        # A pod that is not read as dead (alive, or the launcher cannot say) refuses the same way.
        if (not execution or key == job_id) and podhandle.pod_status(item.get("pod")) != podhandle.DEAD:
            raise ValueError(MSG_POD_LIVE.format(key=key))
    item = state.data["jobs"][job_id]
    if item.get("status") in {"running", "merged", "closed"}:
        raise ValueError("cannot recover a running or completed job")
    if not item.get("worktree") or not item.get("branch"):
        raise ValueError("job has no retained worktree and branch")
    worktree = Path(item["worktree"]).resolve()
    if not worktree.is_dir():
        raise ValueError("retained worktree is missing")
    gitdir = _gitdir(repo, item, worktree)
    if Path(_git(worktree, gitdir, "rev-parse", "--show-toplevel")).resolve() != worktree:
        raise ValueError("retained path is not a worktree root")
    common = _repo_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if (
        _git(worktree, gitdir, "rev-parse", "--path-format=absolute", "--git-common-dir")
        != common
    ):
        raise ValueError("retained worktree belongs to another repository")
    if _git(worktree, gitdir, "branch", "--show-current") != item["branch"]:
        raise ValueError("retained worktree branch changed")
    if _git(worktree, gitdir, "status", "--porcelain", "--untracked-files=all"):
        if not allow_dirty_artifacts:
            raise ValueError(
                "retained worktree is dirty; inspect and commit changes first"
            )
        paths = set()
        for args in (
            ("diff", "--name-only", "--no-renames", "-z"),
            ("diff", "--cached", "--name-only", "--no-renames", "-z"),
            ("ls-files", "--others", "--exclude-standard", "-z"),
        ):
            paths.update(filter(None, _git(worktree, gitdir, *args).split("\0")))
        for value in paths:
            _artifact_path(worktree, jobs[job_id], value, plan)
    head = _git(worktree, gitdir, "rev-parse", "HEAD")
    _commit(worktree, gitdir, head)
    build = item.get("build")
    if (
        not isinstance(build, dict)
        or build.get("blocked") is not False
        or not isinstance(build.get("summary"), str)
        or not isinstance(build.get("tests"), list)
        or type(build.get("feature")) is not bool
    ):
        raise ValueError("job has no usable completed build checkpoint")
    return jobs[job_id], item, worktree, head


def _artifact_path(worktree, job, value, plan):
    path = relative_path(value)
    roots = artifact_roots(job, plan)
    if not any(root in path.parents for root in roots):
        raise ValueError(f"source changed outside assigned plan artifacts: {value}")
    current = worktree
    for part in path.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"artifact path contains a symlink: {value}")
    if current.exists() and (not current.is_file() or current.stat().st_mode & 0o111):
        raise ValueError(f"artifact must be a regular non-executable file: {value}")


def _artifact_descendant(worktree, gitdir, job, ancestor, head, plan):
    """Check every intervening commit, including edits later reverted."""
    _commit(worktree, gitdir, ancestor)
    _commit(worktree, gitdir, head)
    _git(worktree, gitdir, "merge-base", "--is-ancestor", ancestor, head)
    roots = artifact_roots(job, plan)
    for line in _git(
        worktree, gitdir, "rev-list", "--parents", f"{ancestor}..{head}"
    ).splitlines():
        commit, *parents = line.split()
        if len(parents) != 1:
            raise ValueError(
                "artifact evidence reuse requires linear descendant commits"
            )
        paths = _git(
            worktree, gitdir, "diff", "--name-only", "--no-renames", "-z", parents[0], commit
        )
        for value in filter(None, paths.split("\0")):
            path = relative_path(value)
            if not any(root in path.parents for root in roots):
                raise ValueError(
                    f"source changed outside assigned plan artifacts: {value}"
                )
            # Inspect the committed tree, not only the current filesystem: a
            # symlink or executable artifact may have been removed subsequently.
            entry = _git(worktree, gitdir, "ls-tree", commit, "--", value)
            if entry and not entry.startswith("100644 blob "):
                raise ValueError(
                    f"artifact must be a regular non-executable file: {value}"
                )
            current = worktree
            for part in path.parts:
                current /= part
                if current.is_symlink():
                    raise ValueError(f"artifact path contains a symlink: {value}")


def _review(worktree, gitdir, job, item, head, plan):
    review = item.get("review")
    if (
        not isinstance(review, dict)
        or review.get("pass") is not True
        or not isinstance(review.get("findings"), list)
    ):
        raise ValueError("a passing stored independent review is required")
    _artifact_descendant(worktree, gitdir, job, item.get("reviewed_commit"), head, plan)


def review_matches(repo, job_state, job_plan, plan):
    """Whether the final commit preserves the stored passing review's source."""
    try:
        # The check runs in the main checkout, so its trusted git directory is the clone's own. The job
        # state's recorded ``gitdir`` is the job worktree's, a different directory, and must not be passed.
        gitdir = _gitdir(repo, {}, Path(repo))
        _review(Path(repo), gitdir, job_plan, job_state, job_state.get("commit"), plan)
        return True
    except (ValueError, OSError, subprocess.SubprocessError, TypeError):
        return False


def _verification(
    worktree, gitdir, job, item, evidence, head, *, plan, require_tested=False, cfg=None
):
    when, mode, _ = _policy(cfg)
    if not isinstance(evidence, dict) or type(evidence.get("complete")) is not bool:
        raise ValueError("verification must specify complete as a boolean")
    if evidence.get("commit") != head:
        raise ValueError("verification commit differs from current HEAD")
    tested = evidence.get("tested_commit")
    if require_tested and not tested:
        raise ValueError("imported evidence must identify tested_commit")
    tested = tested or head
    _artifact_descendant(worktree, gitdir, job, item.get("reviewed_commit"), tested, plan)
    _artifact_descendant(worktree, gitdir, job, tested, head, plan)
    if evidence["complete"] and verify_required(when, job, item):
        for key in ("scenario", "environment"):
            if not isinstance(evidence.get(key), str) or not evidence[key].strip():
                raise ValueError(f"completed verification evidence requires {key}")
        assertions = evidence.get("assertions")
        if (
            not isinstance(assertions, list)
            or not assertions
            or any(not isinstance(a, str) or not a.strip() for a in assertions)
        ):
            raise ValueError(
                "completed verification evidence requires outcome assertions"
            )
        if mode == "recording":
            url = urlparse(str(evidence.get("recording_url", "")))
            if url.scheme != "https" or not url.netloc or url.username or url.password:
                raise ValueError(
                    "completed recording-mode evidence requires an HTTPS recording_url"
                )
    if not evidence["complete"] and not evidence.get("blocker"):
        raise ValueError("incomplete verification must record a blocker")


def _audit(item, action, **details):
    history = copy.deepcopy(item.get("recovery_history", []))
    history.append(
        {
            "at": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "previous": {
                key: copy.deepcopy(value)
                for key, value in item.items()
                if key != "recovery_history"
            },
            **details,
        }
    )
    return history


def validate_resume(repo, plan, state, job_id, phase, *, execution=False, cfg=None):
    """Revalidate a queued checkpoint immediately before execution; never mutate state."""
    if phase not in {"review", "verify", "publish"}:
        raise ValueError(
            "resume phase must be review, verify, or publish; use retry for build"
        )
    job, item, worktree, head = _context(
        repo,
        plan,
        state,
        job_id,
        allow_dirty_artifacts=phase == "verify",
        execution=execution,
    )
    if item.get("commit") != head:
        raise ValueError("worktree HEAD differs from the stored checkpoint")
    gitdir = _gitdir(repo, item, worktree)
    if phase in {"verify", "publish"}:
        _review(worktree, gitdir, job, item, head, plan)
    if phase == "publish":
        _verification(
            worktree, gitdir, job, item, item.get("verification"), head, plan=plan, cfg=cfg
        )
    return job, item, worktree, head


def queue_resume(
    repo, directory, plan, state, job_id, phase, reason=None, cfg=None
):
    """Queue one retained checkpoint; no implementation or publication occurs."""
    job, item, worktree, head = validate_resume(
        repo, plan, state, job_id, phase, cfg=cfg
    )
    if reason is not None and (not isinstance(reason, str) or not reason.strip()):
        raise ValueError("resume reason must describe a changed premise")
    previous = item.get("recovery_history", [])
    if (
        any(
            event.get("action") == "resume"
            and event.get("phase") == phase
            and event.get("commit") == head
            for event in previous
        )
        and not reason
    ):
        raise ValueError("repeating this phase on unchanged HEAD requires a reason")
    history = _audit(item, "resume", phase=phase, reason=reason, commit=head)
    state.update(
        job_id,
        status="queued",
        resume_phase=phase,
        pid=None,
        error=None,
        recovery_history=history,
        **({"stop": None} if item.get("stop") else {}),  # the record of a stop does not outlive it
    )
    return state.data["jobs"][job_id]


def import_evidence(repo, directory, plan, state, job_id, evidence_path, cfg=None):
    """Import exact-head acceptance evidence, preserving review and previous results.

    Manifest fields are job_id, commit, tested_commit, complete, plus verification
    details. Complete evidence requires scenario, environment and assertions (and
    an HTTPS recording_url in recording mode). Import only queues publication.
    """
    job, item, worktree, head = _context(repo, plan, state, job_id)
    raw = Path(evidence_path).read_bytes()
    evidence = json.loads(raw)
    if not isinstance(evidence, dict) or evidence.get("job_id") != job_id:
        raise ValueError("evidence manifest job_id differs from assigned job")
    gitdir = _gitdir(repo, item, worktree)
    _review(worktree, gitdir, job, item, head, plan)
    _artifact_descendant(worktree, gitdir, job, item.get("commit"), head, plan)
    _verification(
        worktree, gitdir, job, item, evidence, head, plan=plan, require_tested=True, cfg=cfg
    )
    if item.get("pr"):
        pr = parse_gh_json(
            command(
                [
                    "gh",
                    "pr",
                    "view",
                    item["pr"],
                    "--json",
                    "headRefOid,headRefName,baseRefName,state",
                ],
                repo,
            ),
            "inspect PR for evidence import",
            dict,
        )
        if (
            pr.get("headRefOid") != head
            or pr.get("headRefName") != item["branch"]
            or pr.get("baseRefName") != _policy(cfg)[2]
            or pr.get("state") != "OPEN"
        ):
            raise ValueError("PR head, branch, base, or state differs from evidence")
    history = _audit(
        item,
        "import_evidence",
        commit=head,
        evidence_sha256=hashlib.sha256(raw).hexdigest(),
        evidence_path=str(Path(evidence_path).resolve()),
    )
    state.update(
        job_id,
        commit=head,
        verification=evidence,
        status="queued",
        resume_phase="publish",
        pid=None,
        error=None,
        recovery_history=history,
        **({"stop": None} if item.get("stop") else {}),  # the record of a stop does not outlive it
    )
    return state.data["jobs"][job_id]

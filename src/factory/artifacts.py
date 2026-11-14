"""Commit inspected acceptance artifacts without changing tested behavior."""

import re
import stat
from urllib.parse import quote

from .artifact_paths import artifact_roots, relative_path
from .planner import command
from .shell import identity_args


def finalize_verification(
    repo, worktree, job, verification, tested_commit, git, *, plan
):
    """Return evidence and final SHA; only assigned plan artifacts may be committed.

    `tested_commit` remains the behavior revision exercised by the recording.
    `commit` identifies the final PR head that CI and merging must check.
    """
    result = dict(verification)
    if git("rev-parse", "HEAD", cwd=worktree) != tested_commit:
        raise ValueError(
            "verification changed HEAD; inspect worktree before publishing"
        )
    paths = set()
    for args in (
        ("diff", "--name-only", "--no-renames", "-z"),
        ("diff", "--cached", "--name-only", "--no-renames", "-z"),
        ("ls-files", "--others", "--exclude-standard", "-z"),
    ):
        paths.update(filter(None, git(*args, cwd=worktree).split("\0")))

    recording = result.get("recording_path")
    if not recording and not paths:
        return result, tested_commit
    if result.get("commit") != tested_commit:
        raise ValueError("artifact publication requires evidence of the tested commit")
    if recording and result.get("complete") is not True:
        raise ValueError("recording publication requires completed verification")
    if result.get("complete") is True and not all(
        result.get(key) for key in ("scenario", "environment", "assertions")
    ):
        raise ValueError(
            "artifact publication requires scenario, environment, and outcome assertions"
        )

    roots = artifact_roots(job, plan)

    def validate_path(value):
        path = relative_path(value)
        if not any(root in path.parents for root in roots):
            raise ValueError(
                f"verification changed files outside assigned plan artifact directories: {value}"
            )
        current = worktree
        for part in path.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError(f"artifact paths cannot contain symlinks: {value}")
        if current.exists() and (
            not stat.S_ISREG(current.stat().st_mode) or current.stat().st_mode & 0o111
        ):
            raise ValueError(f"artifact must be a regular non-executable file: {value}")
        return current

    for path in paths:
        validate_path(path)
    if recording:
        recording_file = validate_path(recording)
        if not recording_file.is_file() or recording_file.stat().st_size == 0:
            raise ValueError(
                "recording_path must name an existing nonempty regular file"
            )
        # An ignored recording must not produce a URL to an absent committed file.
        if recording not in paths:
            git("ls-files", "--error-unmatch", "--", recording, cwd=worktree)
        slug = command(
            ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
            repo,
        )
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", slug):
            raise ValueError("unexpected GitHub repository name")
    if paths:
        git("add", "--all", "--", *sorted(paths), cwd=worktree)
        for path in paths:
            entry = git("ls-files", "--stage", "--", path, cwd=worktree)
            if entry and not entry.startswith("100644 "):
                raise ValueError(
                    f"artifact must be a regular non-executable file: {path}"
                )
        git(
            "-c",
            "core.hooksPath=/dev/null",
            *identity_args(),
            "commit",
            "-m",
            f"factory: record acceptance evidence for {job['id']}",
            cwd=worktree,
        )
    final_commit = git("rev-parse", "HEAD", cwd=worktree)
    committed = git(
        "diff",
        "--name-only",
        "--no-renames",
        "-z",
        tested_commit,
        final_commit,
        cwd=worktree,
    )
    for path in filter(None, committed.split("\0")):
        validate_path(path)
    result.update(
        commit=final_commit,
        tested_commit=tested_commit,
        artifact_commit=final_commit,
    )
    if recording:
        if int(git("cat-file", "-s", f"{final_commit}:{recording}", cwd=worktree)) <= 0:
            raise ValueError("committed recording is empty")
        result["recording_url"] = (
            f"https://github.com/{slug}/blob/{final_commit}/{quote(recording, safe='/')}"
        )
    return result, final_commit

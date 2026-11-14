"""Canonical artifact permissions; resolving roots never changes an immutable plan."""

import re
from pathlib import PurePosixPath


def relative_path(value):
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or any(c in value for c in "*?[]")
    ):
        raise ValueError("artifact paths must be literal repository-relative paths")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value:
        raise ValueError(
            "artifact paths must be normalized and cannot traverse directories"
        )
    return path


def plan_roots(plan):
    """Directories under which a job may own artifact paths (``artifacts.roots`` at plan time).

    Stored in the immutable plan so every later check agrees with the plan that
    reserved the paths. Default none: no artifact directory exists unless the
    repository's configuration asks for one.
    """
    values = plan.get("artifact_roots", [])
    if not isinstance(values, list):
        raise ValueError("artifact_roots must be a list when present")
    return [relative_path(value) for value in values]


def _root_depth(path, roots):
    """Number of components of the configured root that is a proper prefix of ``path``, else None."""
    for root in roots:
        if len(path.parts) > len(root.parts) and path.parts[: len(root.parts)] == root.parts:
            return len(root.parts)
    return None


def artifact_roots(job, plan):
    """Explicit roots are authoritative; omitted roots require provable unique ownership."""
    if "artifact_paths" in job:
        if not isinstance(job["artifact_paths"], list):
            raise ValueError("artifact_paths must be a list when present")
        roots = plan_roots(plan)
        paths = [relative_path(value) for value in job["artifact_paths"]]
        return [path for path in paths if _root_depth(path, roots) is not None]
    matches = [candidate for candidate in plan["jobs"] if candidate["id"] == job["id"]]
    if len(matches) != 1 or matches[0] != job:
        raise ValueError("artifact inference requires the exact job in the full plan")
    candidates = []
    roots = plan_roots(plan)
    for value in job.get("likely_paths", []):
        path = relative_path(value)
        depth = _root_depth(path, roots)
        if (
            depth is not None
            and len(path.parts) == depth + 1
            and re.fullmatch(r"[a-z0-9][a-z0-9-]*", path.parts[depth])
        ):
            candidates.append(path)
    others = []
    for other in plan["jobs"]:
        if other["id"] == job["id"]:
            continue
        declared = other.get("artifact_paths", [])
        if not isinstance(declared, list):
            raise ValueError("artifact_paths must be a list when present")
        others.extend(
            relative_path(value) for value in other.get("likely_paths", []) + declared
        )
    return list(
        dict.fromkeys(
            root
            for root in candidates
            if not any(
                root == other or root in other.parents or other in root.parents
                for other in others
            )
        )
    )

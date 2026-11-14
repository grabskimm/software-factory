"""The closed reason a ``plan`` failed: one code from a fixed table, in a small file of the run directory.

``plan`` writes the code when it fails (``planner.plan``); the trigger loop reads it back into its ``plan failed`` line
(``operations``). The file holds the code and nothing else: never an exception message, issue text or model output. A missing,
unreadable or unknown value reads as ``other``.
"""

import os
from pathlib import Path

FILE = "plan-failure-code"

INVALID_PLAN_PATHS = "invalid_plan_paths"  # a job's likely_paths are not literal, canonical repository-relative paths
SCHEMA_INVALID = "schema_invalid"  # the planner's result does not form a valid plan
PLANNER_ERROR = "planner_error"  # the planner session failed or produced no usable result
SNAPSHOT_FAILED = "snapshot_failed"  # the GitHub evidence could not be captured or read
TIMEOUT = "timeout"  # the planner session hit its time limit
OTHER = "other"  # anything else, and what a missing or unknown file reads as

MAX_BYTES = 64  # the file is a code and a newline; anything longer is not ours

CODES = (INVALID_PLAN_PATHS, SCHEMA_INVALID, PLANNER_ERROR, SNAPSHOT_FAILED, TIMEOUT, OTHER)


def clear(directory):
    """Remove an earlier attempt's code file, if any (best effort)."""
    try:
        (Path(directory) / FILE).unlink()
    except OSError:
        pass


def write(directory, code):
    """Record ``code`` (anything outside the table is recorded as ``other``); never raises."""
    code = code if code in CODES else OTHER
    try:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / FILE).write_text(code + "\n")
    except OSError:
        pass


def read(directory):
    """The code recorded in ``directory``; ``other`` when the file is missing, unreadable or holds anything else."""
    try:
        descriptor = os.open(Path(directory) / FILE, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            raw = os.read(descriptor, MAX_BYTES + 1)
        finally:
            os.close(descriptor)
        if len(raw) > MAX_BYTES:
            return OTHER
        value = raw.decode("ascii").strip()
    except (OSError, ValueError):
        return OTHER
    return value if value in CODES else OTHER

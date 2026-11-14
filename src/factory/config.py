"""Per-repo configuration: one operator-owned TOML file per allowlisted repository.

The file lives under ``$FACTORY_CONFIG/repos/<org>__<name>.toml`` and is the
only source of per-repo settings. The target repository is never read for
configuration: its contents are written by the builder (which prompt-injected
issue text can steer), so letting it choose its own sandbox domains, setup
commands or prompt text would let a worker reconfigure the supervisor.

Validation is strict on purpose. Unknown keys fail instead of being ignored,
because a typo such as ``verify_domain`` would otherwise silently leave a
repository on a looser default. Every ``RepoConfig`` is validated when it is
constructed, so values that later reach ``git`` argv, filesystem paths or the
sandbox allowlist cannot have bypassed the checks.

Error messages never echo ``prompt_addendum``, ``setup_commands`` or the probe
URL, because those may legitimately carry text the operator treats as private.
"""

import math
import os
import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from urllib.parse import urlsplit

from .artifact_paths import relative_path
from .model_choice import ALIAS, MODEL_ID
from .phases import (
    DEFAULT_PROBE_URL,
    EFFORTS,
    PHASES,
    AgentOptions,
    domain_allows,
    normalize_domains,
    positive_budget,
    probe_host,
)

REPO_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+")
# The same rule the CLI applies to a run directory's basename.
RUN_SLUG = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,49}")

VERIFY_WHEN = ("always", "feature", "never")
PR_STYLES = ("factory", "conventional")  # src/factory/conventions.py STYLES
ACCEPTANCE_MODES = ("tests", "recording")
RESUME_MODES = ("never", "once")
# Where a repository's worker sessions run (``execution``, src/factory/podexecution.py): in a child of the
# supervisor (the default) or, behind a manager-wide switch and not yet available, in a pod of their own.
EXECUTION_INPROCESS = "inprocess"
EXECUTION_POD = "pod"
EXECUTION_MODES = (EXECUTION_INPROCESS, EXECUTION_POD)
# Repository permission levels from least to most; the order is the meaning of
# ``auto_build_min_permission`` (src/factory/trigger.py compares with it).
PERMISSION_LEVELS = ("read", "triage", "write", "maintain", "admin")
# Defaults of the label-triggered builds (docs/manager.md, "Label-triggered builds").
DEFAULT_TRIGGER_LABEL = "factory-build"
DEFAULT_MIN_PERMISSION = "write"
DEFAULT_TRIGGER_CONCURRENCY = 1
DEFAULT_TRIGGER_MAX_ISSUES = 3
MAX_TRIGGER_CONCURRENCY = 64
MAX_TRIGGER_MAX_ISSUES = 50
DEFAULT_EPIC_OPEN_PRS = 3
MAX_EPIC_OPEN_PRS = 50
# The per-session agent limit of automatic runs (``run --timeout``), in seconds.
DEFAULT_TRIGGER_TIMEOUT_SECONDS = 1800
MIN_TRIGGER_TIMEOUT_SECONDS = 60
MAX_TRIGGER_TIMEOUT_SECONDS = 86_400
# Defaults of the @mention follow-up pass (docs/manager.md, "Follow-up requests").
DEFAULT_FOLLOWUP_COOLDOWN_SECONDS = 300
MAX_FOLLOWUP_COOLDOWN_SECONDS = 24 * 3600
# Planner spend allowed per repository in any hour, in USD; 0 (or "off") disables the cap.
DEFAULT_PLANNER_USD_PER_HOUR = 2.0
DEFAULT_FOLLOWUP_MAX_SESSIONS = 5
MAX_FOLLOWUP_MAX_SESSIONS = 100

# How long a run may sit idle before the manager purges it as abandoned. The
# floor also catches a bare ``abandon_after = 7`` that meant days; the ceiling
# keeps date arithmetic in the manager far from overflow.
DEFAULT_ABANDON_SECONDS = 7 * 24 * 3600
MIN_ABANDON_SECONDS = 3600
MAX_ABANDON_SECONDS = 3650 * 24 * 3600

MAX_CONFIG_BYTES = 256 * 1024
MAX_ADDENDUM_CHARS = 20_000

_MAX_REPO_CHARS = 200
_MAX_BRANCH_CHARS = 255
# Anything that is not a printable, non-space ASCII character, plus the
# characters `git check-ref-format` forbids.
_BAD_REF_CHARS = re.compile(r"[^\x21-\x7e]|[~^:?*\[\\]")
# ``7d``, ``12h`` or ``90m``: nine digits at most, so the product stays a small int.
_DURATION = re.compile(r"([0-9]{1,9})([dhm])")
_UNIT_SECONDS = {"d": 24 * 3600, "h": 3600, "m": 60}
_MODEL_ID = MODEL_ID
MAX_ALIASES = 32
MAX_ALLOWED_MODELS = 64
# Toolchains become image build arguments and verify setup: a conservative
# `name@version` alphabet with no whitespace or shell metacharacters.
_TOOLCHAIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+:=-]{0,63}")
# C0 controls (NUL, newline, escape, ...) except tab, and DEL.
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")


class RepoNotConfigured(ValueError):
    """The repository has no config file, so it is not on the operator's allowlist."""


def _show(value):
    """A bounded ``repr`` so an error message cannot become a data dump."""
    text = repr(value)
    return text if len(text) <= 80 else text[:77] + "..."


def _is_int(value):
    # bool is an int subclass; ``true`` must not pass for a count or an ID.
    return isinstance(value, int) and not isinstance(value, bool)


# ---------------------------------------------------------------------------
# Field validators. Each returns the normalized value or raises ValueError.
# Messages use the TOML key names because operators read the TOML.
# ---------------------------------------------------------------------------


def check_repo(value):
    """``org/name`` as used for ``gh -R``, the lock key and every storage path."""
    if (
        not isinstance(value, str)
        or len(value) > _MAX_REPO_CHARS
        or not REPO_PATTERN.fullmatch(value)
        or ".." in value
    ):
        raise ValueError(f"repo must look like 'org/name', got {_show(value)}")
    owner, name = value.split("/")
    # '.' would make the repo directory collide with its owner directory. An
    # owner holding '__' or ending in '_' would let two repos share one
    # `<org>__<name>` slug (acme_/x and acme/_x), and with it one config file,
    # one lock and one set of run directories.
    if name == "." or "__" in owner or owner.endswith("_"):
        raise ValueError(f"repo must look like 'org/name', got {_show(value)}")
    return value


def _repo_or_local(value):
    """"" is local mode (no named repository: gh infers it from the checkout)."""
    return "" if value == "" else check_repo(value)


def check_base_branch(value):
    """A branch short name that is safe as git argv, a refspec and a path part.

    Follows `git check-ref-format` and additionally refuses a leading ``-``
    (option injection), non-ASCII and non-printable characters, a ``refs/``
    prefix and the name ``HEAD``, none of which name a branch.
    """
    if not isinstance(value, str) or not value:
        raise ValueError("base_branch must be a non-empty string")
    parts = value.split("/")
    problem = None
    if len(value) > _MAX_BRANCH_CHARS:
        problem = "is too long"
    elif value.startswith("-"):
        problem = "must not start with '-'"
    elif _BAD_REF_CHARS.search(value):
        problem = "contains a space, control, non-ASCII or git-reserved character"
    elif ".." in value or "@{" in value:
        problem = "must not contain '..' or '@{'"
    elif value in {"@", "HEAD"} or value.startswith("refs/"):
        problem = "is not a branch name"
    elif value.endswith(".") or "" in parts:
        problem = "must not end with '.', start or end with '/', or contain '//'"
    elif any(p.startswith(".") or p.endswith(".lock") for p in parts):
        problem = "must not have a path component starting with '.' or ending in '.lock'"
    if problem:
        raise ValueError(f"base_branch {_show(value)} {problem}")
    return value


def check_run_slug(value):
    """The run directory name; also the last component of a path built from it."""
    if not isinstance(value, str) or not RUN_SLUG.fullmatch(value):
        raise ValueError("run slug must be 1-50 characters of [a-zA-Z0-9_-], not starting with '_' or '-'")
    return value


def _flag(key):
    def check(value):
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be true or false")
        return value

    return check


def _installation_id(value):
    if value is None:
        return None
    if not _is_int(value) or value <= 0:
        raise ValueError("installation_id must be a positive integer")
    return value


_MENTION_HANDLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")


def _mention_handle(value):
    """The account name a deployment without an App identity is mentioned by (letters, digits and hyphens); "" is unset."""
    if value == "":
        return value
    if not isinstance(value, str) or not _MENTION_HANDLE.fullmatch(value):
        raise ValueError("mention_handle must be 1-39 characters of letters, digits and '-', starting with a letter or digit")
    return value


def _choice(key, allowed):
    def check(value):
        if not isinstance(value, str) or value not in allowed:
            raise ValueError(f"{key} must be one of {', '.join(allowed)}, got {_show(value)}")
        return value

    return check


def _strings(key, value):
    """A list of non-blank strings; a bare string must not be iterated per character."""
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{key} must be a list of strings")
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{key}[{index}] must be a non-empty string")
    return list(value)


def _toolchains(value):
    items = _strings("toolchains", value)
    for index, item in enumerate(items):
        if not _TOOLCHAIN.fullmatch(item):
            raise ValueError(f"toolchains[{index}] must look like 'node@22', got {_show(item)}")
    return tuple(items)


def _setup_commands(value):
    """Shell lines the supervisor runs later. Trusted operator input; only the shape is checked."""
    items = _strings("setup_commands", value)
    for index, item in enumerate(items):
        if _CONTROL.search(item):
            raise ValueError(f"setup_commands[{index}] must be a single line without control characters")
    return tuple(items)


def _artifact_roots(value):
    roots = []
    for item in _strings("artifacts.roots", value):
        try:
            path = relative_path(item)
        except ValueError as exc:
            raise ValueError(f"artifacts.roots: {exc}") from None
        if (
            str(path) == "."
            or ".git" in path.parts
            or item != item.strip()
            or _CONTROL.search(item)
        ):
            raise ValueError(f"artifacts.roots: unsafe root {_show(item)}")
        roots.append(str(path))
    return tuple(dict.fromkeys(roots))


def _verify_domains(value):
    try:
        domains = normalize_domains(_strings("sandbox.verify_domains", value))
    except ValueError as exc:
        raise ValueError(f"sandbox.verify_domains: {exc}") from None
    for domain in domains:
        # `*.com` would be an allowlist of the whole TLD, not of a service.
        if domain.startswith("*.") and "." not in domain[2:].split(":")[0]:
            raise ValueError(
                f"sandbox.verify_domains: wildcard {_show(domain)} must name a registrable domain"
            )
    return domains


def _probe_url(value):
    if not isinstance(value, str) or not value or value != value.strip() or re.search(r"[\x00-\x20\x7f]", value):
        raise ValueError("preflight.probe_url must be an https URL without whitespace")
    try:
        probe_host(value)
        parts = urlsplit(value)
        _ = parts.port  # raises ValueError on a malformed port
    except ValueError:
        raise ValueError("preflight.probe_url must be a valid https URL") from None
    if "@" in parts.netloc:
        raise ValueError("preflight.probe_url must not embed credentials")
    return value


def _addendum(value):
    if not isinstance(value, str):
        raise ValueError("prompt_addendum must be a string of inline text, not a path")
    if len(value) > MAX_ADDENDUM_CHARS or "\x00" in value:
        raise ValueError(f"prompt_addendum must be text of at most {MAX_ADDENDUM_CHARS} characters")
    return value


def _phase_map(key, convert):
    def check(value):
        if not isinstance(value, dict):
            raise ValueError(f"{key} must be a table of phase = value")
        result = {}
        for phase, item in value.items():
            if phase not in PHASES:
                raise ValueError(f"{key}: unknown phase {_show(phase)}; expected one of {', '.join(PHASES)}")
            result[phase] = convert(phase, item)
        return result

    return check


def _model(phase, value):
    if not isinstance(value, str) or not _MODEL_ID.fullmatch(value):
        raise ValueError(f"models.{phase} must be a model id such as 'claude-sonnet-5-5'")
    return value


def _effort(phase, value):
    if not isinstance(value, str) or value not in EFFORTS:
        raise ValueError(f"efforts.{phase} must be one of {', '.join(EFFORTS)}, got {_show(value)}")
    return value


def _optional_model(value):
    """``model``: the repository's default model id for every phase without a ``[models]`` entry."""
    if value is None:
        return None
    if not isinstance(value, str) or not _MODEL_ID.fullmatch(value):
        raise ValueError(f"model must be a model id such as 'claude-sonnet-5-5', got {_show(value)}")
    return value


def _optional_choice(key, allowed):
    check = _choice(key, allowed)

    def run(value):
        return None if value is None else check(value)

    return run


def _allowed_models(value):
    """``allowed_models``: the model ids an issue's ``factory-model`` label may select (exact when set)."""
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) > MAX_ALLOWED_MODELS:
        raise ValueError(f"allowed_models must be a list of at most {MAX_ALLOWED_MODELS} model ids")
    for index, item in enumerate(value):
        if not isinstance(item, str) or not _MODEL_ID.fullmatch(item):
            raise ValueError(f"allowed_models[{index}] must be a model id such as 'claude-sonnet-5-5'")
    return tuple(dict.fromkeys(value))


def _aliases(value):
    """``[aliases]``: lowercase alias -> model id, merged over the built-in opus, sonnet and haiku."""
    if not isinstance(value, dict) or len(value) > MAX_ALIASES:
        raise ValueError(f"aliases must be a table of alias = model id with at most {MAX_ALIASES} entries")
    result = {}
    for alias, model in value.items():
        if not isinstance(alias, str) or not ALIAS.fullmatch(alias):
            raise ValueError(
                f"aliases: {_show(alias)} must be 1-36 lowercase letters, digits, '.', '_' or '-', "
                "starting with a letter or digit"
            )
        if not isinstance(model, str) or not _MODEL_ID.fullmatch(model):
            raise ValueError(f"aliases.{alias} must be a model id such as 'claude-sonnet-5-5'")
        result[alias] = model
    return result


def _budget(phase, value):
    try:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError
        return positive_budget(value)
    except (ValueError, OverflowError):
        raise ValueError(f"budgets.{phase} must be a positive, finite number of dollars") from None


def _max_active_agents(value):
    if not _is_int(value) or value <= 0:
        raise ValueError("resources.max_active_agents must be a positive integer")
    return value


def _skills_budget(value):
    if not _is_int(value) or not 16 <= value <= 1024:
        raise ValueError("repo_skills_budget_kib must be a whole number of KiB from 16 to 1024")
    return value


def _worker_id(key):
    def check(value):
        if value is None:
            return None
        # 0 is root: a root worker would defeat the point of a separate identity.
        # 2**32-1 is (uid_t)-1, which setuid() treats as "no change".
        if not _is_int(value) or not 0 < value < 2**32 - 1:
            raise ValueError(f"{key} must be an integer in 1..4294967294")
        return value

    return check


def _abandon_after(value):
    """Idle time before a run counts as abandoned, as whole seconds.

    Accepts ``"<n>d"``, ``"<n>h"``, ``"<n>m"`` or an integer number of seconds.
    The same bounds apply to both forms, because the stored seconds are what
    ``dataclasses.replace`` validates again: a value that passed once must pass
    every time.
    """
    shape = "abandon_after must look like '7d', '12h' or '90m', or be a number of seconds"
    if _is_int(value):
        seconds = value
    elif isinstance(value, str):
        match = _DURATION.fullmatch(value)
        if not match:
            raise ValueError(f"{shape}, got {_show(value)}")
        seconds = int(match[1]) * _UNIT_SECONDS[match[2]]
    else:
        raise ValueError(f"{shape}, got {_show(value)}")
    if not MIN_ABANDON_SECONDS <= seconds <= MAX_ABANDON_SECONDS:
        raise ValueError(
            f"abandon_after must be between 1h and {MAX_ABANDON_SECONDS // 86400}d, got {_show(value)}"
        )
    return seconds


def _run_budget(value):
    """The per-run dollar cap, or None for no guardrail."""
    if value is None:
        return None
    problem = "run_budget_usd must be a positive, finite number of dollars"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(problem)
    try:
        return positive_budget(value)
    except (ValueError, OverflowError):
        raise ValueError(problem) from None


def _planner_hourly_cap(value):
    """The planner's hourly dollar cap: a finite number of dollars >= 0, or ``"off"``; stored as a float, 0.0 meaning off."""
    problem = 'planner_max_usd_per_hour must be a finite number of dollars (0 or "off" disables the cap)'
    if isinstance(value, str) and value.strip().lower() == "off":
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(problem)
    try:
        number = float(value)
    except OverflowError:
        raise ValueError(problem) from None
    if not math.isfinite(number) or number < 0:
        raise ValueError(problem)
    return number


_TRIGGER_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/ -]{0,49}")


def _trigger_label(value):
    """The label that asks Factory to build an issue: plain characters, never a control label.

    ``factory-ignore`` always wins over everything and ``factory:*`` are Factory's own status
    labels, so neither can be the trigger. The name ends up in API paths and log fields, so
    the alphabet is conservative (no quotes, colons, commas or non-ASCII).
    """
    problem = (
        "auto_build_label must be 1-50 characters of letters, digits, space, '.', '_', '/' and '-', "
        "without spaces at its edges, and neither factory-ignore nor a factory:* label"
    )
    if not isinstance(value, str) or not _TRIGGER_LABEL.fullmatch(value) or value != value.strip():
        raise ValueError(problem)
    folded = value.casefold()
    if folded == "factory-ignore" or folded.startswith("factory:"):
        raise ValueError(problem)
    return value


def _count_in(key, high):
    def check(value):
        if not _is_int(value) or not 1 <= value <= high:
            raise ValueError(f"{key} must be an integer from 1 to {high}")
        return value

    return check


def _int_between(key, low, high):
    def check(value):
        if not _is_int(value) or not low <= value <= high:
            raise ValueError(f"{key} must be an integer from {low} to {high}")
        return value

    return check


def _timeout_ok(value):
    return _is_int(value) and MIN_TRIGGER_TIMEOUT_SECONDS <= value <= MAX_TRIGGER_TIMEOUT_SECONDS


def _session_timeout(value):
    """``auto_build_timeout_seconds``: 60 to 86400, and anything else is the default.

    Unlike its siblings an out-of-range value does not refuse the config: a mistyped limit must
    not stop the repository's builds, so it means the default (``load_repo_config`` logs it).
    """
    return value if _timeout_ok(value) else DEFAULT_TRIGGER_TIMEOUT_SECONDS


_NORMALIZERS = {
    "repo": _repo_or_local,
    "base_branch": check_base_branch,
    "allow_non_default_base": _flag("allow_non_default_base"),
    "installation_id": _installation_id,
    "toolchains": _toolchains,
    "setup_commands": _setup_commands,
    "verify_when": _choice("verify.when", VERIFY_WHEN),
    "acceptance_mode": _choice("acceptance.mode", ACCEPTANCE_MODES),
    "artifact_roots": _artifact_roots,
    "verify_domains": _verify_domains,
    "probe_url": _probe_url,
    "prompt_addendum": _addendum,
    "models": _phase_map("models", _model),
    "efforts": _phase_map("efforts", _effort),
    "model": _optional_model,
    "effort": _optional_choice("effort", EFFORTS),
    "max_effort": _optional_choice("max_effort", EFFORTS),
    "allowed_models": _allowed_models,
    "aliases": _aliases,
    "budgets": _phase_map("budgets", _budget),
    "max_active_agents": _max_active_agents,
    "worker_uid": _worker_id("worker.uid"),
    "worker_gid": _worker_id("worker.gid"),
    "abandon_after": _abandon_after,
    "run_budget_usd": _run_budget,
    "planner_max_usd_per_hour": _planner_hourly_cap,
    "auto_resume_after_restart": _choice("auto_resume_after_restart", RESUME_MODES),
    "auto_build": _flag("auto_build"),
    "repo_skills": _flag("repo_skills"),
    "repo_skills_budget_kib": _skills_budget,
    "auto_build_label": _trigger_label,
    "auto_build_min_permission": _choice("auto_build_min_permission", PERMISSION_LEVELS),
    "auto_build_concurrency": _count_in("auto_build_concurrency", MAX_TRIGGER_CONCURRENCY),
    "auto_build_max_issues": _count_in("auto_build_max_issues", MAX_TRIGGER_MAX_ISSUES),
    "auto_build_timeout_seconds": _session_timeout,
    "comment_on_blocked": _flag("comment_on_blocked"),
    "epic_mode": _flag("epic_mode"),
    "epic_max_open_prs": _count_in("epic_max_open_prs", MAX_EPIC_OPEN_PRS),
    "observed_issues": _flag("observed_issues"),
    "pr_style": _choice("pr_style", PR_STYLES),
    "execution": _choice("execution", EXECUTION_MODES),
    "followup": _flag("followup"),
    "followup_reply": _flag("followup_reply"),
    "followup_min_permission": _choice("followup_min_permission", PERMISSION_LEVELS),
    "followup_cooldown_seconds": _int_between("followup_cooldown_seconds", 0, MAX_FOLLOWUP_COOLDOWN_SECONDS),
    "followup_max_sessions": _int_between("followup_max_sessions", 1, MAX_FOLLOWUP_MAX_SESSIONS),
    "mention_handle": _mention_handle,
}


@dataclass(frozen=True)
class RepoConfig:
    """Everything the supervisor may know about one repository.

    Frozen so a config cannot be altered after validation. ``models`` and
    ``budgets`` are plain dicts (the type ``AgentOptions`` takes); treat them
    as read-only, ``agent_options()`` hands out copies.
    """

    repo: str = ""  # "" is local mode; load_repo_config always requires org/name
    base_branch: str = "main"
    allow_non_default_base: bool = False
    installation_id: int | None = None
    toolchains: tuple[str, ...] = ()
    setup_commands: tuple[str, ...] = ()
    verify_when: str = "feature"
    acceptance_mode: str = "tests"
    artifact_roots: tuple[str, ...] = ()
    verify_domains: tuple[str, ...] = ()
    probe_url: str = DEFAULT_PROBE_URL
    # Inline operator text from the config file, never a path into the target repo.
    prompt_addendum: str = ""
    models: dict = field(default_factory=dict)
    budgets: dict = field(default_factory=dict)
    # Model and effort choice (src/factory/model_choice.py). ``model`` and ``effort`` are the defaults of
    # every phase but the preflight probe that has no ``models`` / ``efforts`` entry; None keeps the
    # built-in table. ``allowed_models``, ``max_effort`` and ``aliases`` bound and name what an issue's
    # factory-model / factory-effort labels may select (builder and verifier only): None means the built-in
    # ids plus the repository's own configured ones, and the highest effort the repository already uses.
    efforts: dict = field(default_factory=dict)
    model: str | None = None
    effort: str | None = None
    allowed_models: tuple[str, ...] | None = None
    max_effort: str | None = None
    aliases: dict = field(default_factory=dict)
    max_active_agents: int = 6
    # Optional separate worker identity, used later for Popen(user=, group=).
    worker_uid: int | None = None
    worker_gid: int | None = None
    # Policy the manager applies to this repository's runs; the supervisor
    # (CLI) never reads it. ``abandon_after`` is seconds, ``run_budget_usd`` is
    # None when no guardrail is configured.
    abandon_after: int = DEFAULT_ABANDON_SECONDS
    run_budget_usd: float | None = None
    # The most the planner may spend per hour in this repository (0.0: no cap); see ``operations``.
    planner_max_usd_per_hour: float = DEFAULT_PLANNER_USD_PER_HOUR
    auto_resume_after_restart: str = "never"
    # Label-triggered builds (manager only): the manager watches the repository for issues
    # that carry ``auto_build_label``. Watching is the default; ``auto_build = false`` opts out.
    # A permission below ``write`` is refused by the manager for a repository that is not
    # private (it needs GitHub to know, so it cannot be checked here).
    auto_build: bool = True
    # The repository's skills (.claude/skills, text from the base ref) are part of every session's system prompt (issue #260).
    repo_skills: bool = True
    # The most the skills' full texts may take in a session's system prompt (KiB); the rest are listed by name and path.
    repo_skills_budget_kib: int = 256
    auto_build_label: str = DEFAULT_TRIGGER_LABEL
    auto_build_min_permission: str = DEFAULT_MIN_PERMISSION
    auto_build_concurrency: int = DEFAULT_TRIGGER_CONCURRENCY
    auto_build_max_issues: int = DEFAULT_TRIGGER_MAX_ISSUES
    auto_build_timeout_seconds: int = DEFAULT_TRIGGER_TIMEOUT_SECONDS
    # Factory posts one comment on an issue that reaches a non-building outcome that needs a
    # human (blocked, needs a decision, failed; src/factory/issue_comments.py). ``false`` opts
    # this repository out; the deployment-wide switch is ``FACTORY_ISSUE_COMMENTS=off``.
    comment_on_blocked: bool = True
    # Epic mode (src/factory/epic.py, src/factory/epic_gate.py): an issue the planner finds too large for one
    # change but decomposable becomes a phase plan comment with numbered questions and a human gate instead of
    # a dead-end ``blocked``. It is ON by default for every watched repository; ``false`` keeps ``blocked``.
    # It needs the outcome comment (``comment_on_blocked``): without a way to comment the planner's ``epic``
    # result is downgraded to ``blocked``. The deployment-wide switch is ``FACTORY_EPIC_MODE=off``.
    epic_mode: bool = True
    # The manager starts the next phase of an approved epic itself (src/factory/epic_phases.py) only while Factory has fewer than this
    # many pull requests open in the repository (a read that cannot be completed holds the start too). Per run, never summed: the
    # per-run budget ``run_budget_usd`` applies to each phase's run.
    epic_max_open_prs: int = DEFAULT_EPIC_OPEN_PRS
    # Factory opens an issue for what a builder or reviewer observed outside its job, or could not fix
    # (src/factory/observed_issues.py). ON by default; ``false`` opts this repository out, and the
    # deployment-wide ``FACTORY_OBSERVED_ISSUES=off`` stops it everywhere. Needs ``repo`` to be set.
    observed_issues: bool = True
    # How a pull request is titled and its commit worded (src/factory/conventions.py). ``factory`` is
    # ``factory: <objective> (#N)``; ``conventional`` is a Conventional Commits ``<type>: <subject> (#N)`` and a
    # squash-safe description, for a repository whose release tooling reads the merged commit message.
    pr_style: str = "factory"
    # Where this repository's worker sessions (plan, build, review, verify and the preflight probe) run, every one
    # of them, with no per-phase split: ``inprocess`` is a child of the supervisor, as always; ``pod`` asks for a pod
    # per session (docs/pod-backend.md). Only the owner sets it. ``pod`` is refused, with a fixed text and nothing
    # started, until the manager-wide ``FACTORY_POD_BACKEND=on`` is set and the pod backend exists
    # (src/factory/podexecution.py); it never falls back to ``inprocess`` on its own.
    execution: str = EXECUTION_INPROCESS
    # @mention follow-up requests (manager only; src/factory/followup.py records them and
    # src/factory/followup_serve.py turns the oldest into a session on the pull request's own branch):
    # they are OFF by default, ``followup = true`` opts this repository in. As for
    # ``auto_build_min_permission``, a permission below ``write`` is refused by the manager for a
    # repository that is not private. ``mention_handle`` is the account to mention (and the author of
    # the pull requests to follow) for a deployment that has no App identity; "" means the App's slug.
    followup: bool = False
    # Whether Factory says on the pull request what became of a request it settled (src/factory/followup_reply.py):
    # one comment per pull request from the Factory App, edited in place. Only read when ``followup`` is true; the
    # deployment-wide ``FACTORY_ISSUE_COMMENTS=off`` also stops it.
    followup_reply: bool = True
    followup_min_permission: str = DEFAULT_MIN_PERMISSION
    followup_cooldown_seconds: int = DEFAULT_FOLLOWUP_COOLDOWN_SECONDS
    followup_max_sessions: int = DEFAULT_FOLLOWUP_MAX_SESSIONS
    mention_handle: str = ""

    def __post_init__(self):
        # A missing normalizer is a KeyError, so a new field cannot skip validation.
        for item in fields(self):
            object.__setattr__(
                self, item.name, _NORMALIZERS[item.name](getattr(self, item.name))
            )
        if (self.worker_uid is None) != (self.worker_gid is None):
            raise ValueError("worker.uid and worker.gid must be set together")

    @property
    def slug(self):
        """``org__name``: a flat, filesystem- and lock-key-safe form of the repo."""
        return self.repo.replace("/", "__")

    @property
    def owner(self):
        return self.repo.split("/")[0]

    @property
    def name(self):
        return self.repo.split("/")[1]

    def agent_options(self):
        """The slice of this config the phase policy table consumes."""
        return AgentOptions(
            verify_domains=self.verify_domains,
            probe_url=self.probe_url,
            models=dict(self.models),
            efforts=dict(self.efforts),
            budgets=dict(self.budgets),
            addendum=self.prompt_addendum,
            repo_skills=self.repo_skills,
            repo_skills_budget_kib=self.repo_skills_budget_kib,
            default_model=self.model,
            default_effort=self.effort,
            aliases=dict(self.aliases),
            allowed_models=self.allowed_models,
            max_effort=self.max_effort,
        )

    def probe_problem(self):
        """Why the preflight probe could not succeed in verify, or None.

        The probe runs under the verify allowlist; a probe host outside it
        would report "network unavailable" for a correctly configured repo.
        """
        if self.verify_when == "never" or not self.verify_domains:
            return None
        host = probe_host(self.probe_url)
        if domain_allows(self.verify_domains, host):
            return None
        return (
            f"preflight.probe_url host {host!r} is not on sandbox.verify_domains, "
            "so the verify-phase capability probe will report the network as unavailable"
        )


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

# TOML layout -> RepoConfig field. ``None`` is the top level. The file schema
# lives here and nowhere else, so adding a key is a one-line change.
_TOP_LEVEL = {
    "repo": "repo",
    "base_branch": "base_branch",
    "allow_non_default_base": "allow_non_default_base",
    "installation_id": "installation_id",
    "toolchains": "toolchains",
    "setup_commands": "setup_commands",
    "prompt_addendum": "prompt_addendum",
    "model": "model",
    "effort": "effort",
    "max_effort": "max_effort",
    "allowed_models": "allowed_models",
    "abandon_after": "abandon_after",
    "run_budget_usd": "run_budget_usd",
    "planner_max_usd_per_hour": "planner_max_usd_per_hour",
    "auto_resume_after_restart": "auto_resume_after_restart",
    "auto_build": "auto_build",
    "repo_skills": "repo_skills",
    "repo_skills_budget_kib": "repo_skills_budget_kib",
    "auto_build_label": "auto_build_label",
    "auto_build_min_permission": "auto_build_min_permission",
    "auto_build_concurrency": "auto_build_concurrency",
    "auto_build_max_issues": "auto_build_max_issues",
    "auto_build_timeout_seconds": "auto_build_timeout_seconds",
    "comment_on_blocked": "comment_on_blocked",
    "epic_mode": "epic_mode",
    "epic_max_open_prs": "epic_max_open_prs",
    "observed_issues": "observed_issues",
    "pr_style": "pr_style",
    "execution": "execution",
    "followup": "followup",
    "followup_reply": "followup_reply",
    "followup_min_permission": "followup_min_permission",
    "followup_cooldown_seconds": "followup_cooldown_seconds",
    "followup_max_sessions": "followup_max_sessions",
    "mention_handle": "mention_handle",
}
_TABLES = {
    "verify": {"when": "verify_when"},
    "acceptance": {"mode": "acceptance_mode"},
    "artifacts": {"roots": "artifact_roots"},
    "sandbox": {"verify_domains": "verify_domains"},
    "preflight": {"probe_url": "probe_url"},
    "resources": {"max_active_agents": "max_active_agents"},
    "worker": {"uid": "worker_uid", "gid": "worker_gid"},
}
# Free-form tables whose keys are phase names (validated by the field checks).
_PHASE_TABLES = ("models", "efforts", "budgets")
# Free-form tables whose keys are names of the operator's choosing (validated by the field checks).
_FREE_TABLES = ("aliases",)


def _unknown(where, key, allowed):
    name = f"{where}.{key}" if where else key
    return ValueError(
        f"unknown config key {_show(name)} (allowed here: {', '.join(sorted(allowed))})"
    )


def _fields_from_document(document):
    """Map a parsed TOML document to RepoConfig kwargs, refusing anything unknown."""
    values = {}
    for key, value in document.items():
        if key in _TOP_LEVEL:
            values[_TOP_LEVEL[key]] = value
        elif key in _TABLES or key in _PHASE_TABLES or key in _FREE_TABLES:
            if not isinstance(value, dict):
                raise ValueError(f"{key} must be a table")
            if key in _PHASE_TABLES or key in _FREE_TABLES:
                values[key] = dict(value)
                continue
            for sub, item in value.items():
                if sub not in _TABLES[key]:
                    raise _unknown(key, sub, _TABLES[key])
                values[_TABLES[key][sub]] = item
        else:
            raise _unknown("", key, [*_TOP_LEVEL, *_TABLES, *_PHASE_TABLES, *_FREE_TABLES])
    return values


def _operator_dir(value, label):
    """An operator-supplied directory as an absolute path, rejecting ``..``.

    ``abspath`` folds ``a/link/..`` lexically while the kernel would follow the
    link first; refusing ``..`` outright removes the ambiguity.
    """
    if value is None or value == "":
        raise ValueError(f"{label} is not set")
    try:
        text = os.fspath(value)
    except TypeError:
        raise ValueError(f"{label} must be a path") from None
    if not isinstance(text, str) or not text or "\x00" in text:
        raise ValueError(f"{label} must be a path")
    if ".." in Path(text).parts:
        raise ValueError(f"{label} must not contain '..' components")
    return Path(os.path.abspath(text))


def _read_config(path, repo):
    try:
        if not path.exists():
            raise RepoNotConfigured(
                f"repository {repo} is not configured: no file at {path}"
            )
        if not path.is_file():
            raise ValueError(f"{path} is not a regular file")
        with path.open("rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc.strerror or type(exc).__name__}") from None
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError(f"{path} is larger than {MAX_CONFIG_BYTES} bytes")
    try:
        return tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        raise ValueError(f"{path} is not valid UTF-8") from None
    except (tomllib.TOMLDecodeError, RecursionError) as exc:
        raise ValueError(f"{path} is not valid TOML: {type(exc).__name__}: {exc}") from None


def load_repo_config(repo, config_dir=None, env=os.environ, log=None):
    """Load ``<config_dir>/repos/<org>__<name>.toml`` for ``repo``.

    ``config_dir`` defaults to ``$FACTORY_CONFIG``. Raises ``RepoNotConfigured``
    (a ``ValueError``) when the file is missing, ``ValueError`` for anything
    else wrong. ``log`` receives non-fatal warnings (one string per call).
    """
    wanted = check_repo(repo)
    directory = config_dir if config_dir is not None else env.get("FACTORY_CONFIG")
    if directory is None or directory == "":
        raise ValueError("no config directory: pass config_dir or set FACTORY_CONFIG")
    base = _operator_dir(directory, "config directory")
    path = base / "repos" / f"{wanted.replace('/', '__')}.toml"
    document = _read_config(path, wanted)

    if "repo" not in document:
        raise ValueError(f"{path}: missing required key 'repo'")
    if document["repo"] != wanted:
        raise ValueError(
            f"{path}: repo {_show(document['repo'])} does not match requested {_show(wanted)}"
        )
    try:
        cfg = RepoConfig(**_fields_from_document(document))
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from None

    if "auto_build_timeout_seconds" in document and not _timeout_ok(document["auto_build_timeout_seconds"]) and log:
        log(
            f"{path}: auto_build_timeout_seconds must be an integer from {MIN_TRIGGER_TIMEOUT_SECONDS} "
            f"to {MAX_TRIGGER_TIMEOUT_SECONDS}; using {DEFAULT_TRIGGER_TIMEOUT_SECONDS}"
        )

    problem = cfg.probe_problem()
    if problem:
        # An operator who wrote both lines made a contradiction; one who only
        # set the domains is relying on a default probe and gets a warning.
        # (Both tables are dicts here: the config already validated.)
        if "verify_domains" in document.get("sandbox", {}) and "probe_url" in document.get(
            "preflight", {}
        ):
            raise ValueError(f"{path}: {problem}")
        if log:
            log(f"{path}: {problem}")
    return cfg


# ---------------------------------------------------------------------------
# Storage paths
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DataPaths:
    """Where one repository's managed clone and runs live under ``$FACTORY_DATA``.

    Pure path arithmetic: nothing is created or resolved on disk, and every
    user-influenced component (org, name, run slug) was validated to hold no
    separator and no ``..``, so a join can never leave ``root``.
    """

    root: Path  # ${FACTORY_DATA}/repos/<org>/<name>

    @property
    def clone(self):
        return self.root / "clone"

    @property
    def runs(self):
        return self.root / "runs"

    @property
    def plan_holds(self):
        return self.root / "plan-holds.json"  # plan_holds.FILE

    def run_dir(self, run_slug):
        return self.runs / check_run_slug(run_slug)


def data_paths(cfg, data_dir=None, env=os.environ):
    """Storage layout for ``cfg``; ``data_dir`` defaults to ``$FACTORY_DATA``."""
    owner, name = check_repo(cfg.repo).split("/")
    base = _operator_dir(
        data_dir if data_dir is not None else env.get("FACTORY_DATA"), "data directory (FACTORY_DATA)"
    )
    return DataPaths(base / "repos" / owner / name)


def hidden_paths(cfg, data_dir=None, config_dir=None, env=os.environ):
    """Directories a worker of ``cfg`` must not read: everything but its own repository.

    All workers share one uid and group, and the group can read every clone's
    git directory, so the sandbox hides, per invocation: each other repository's
    root under ``$FACTORY_DATA/repos``, anything else at the top of the data
    directory, and ``$FACTORY_CONFIG``. The worker's own repository root is
    left alone: its working directory is inside it.
    """
    base = _operator_dir(
        data_dir if data_dir is not None else env.get("FACTORY_DATA"), "data directory (FACTORY_DATA)"
    )
    own = data_paths(cfg, base, env).root
    hidden = []
    try:
        for entry in sorted(base.iterdir()):
            if entry.name != "repos":
                hidden.append(entry)
                continue
            for owner in sorted(entry.iterdir()):
                if not owner.is_dir():
                    hidden.append(owner)
                    continue
                hidden.extend(root for root in sorted(owner.iterdir()) if root != own)
    except OSError:
        pass  # a directory that cannot be listed cannot be read by the worker either
    config = config_dir if config_dir is not None else env.get("FACTORY_CONFIG")
    if config:
        hidden.append(_operator_dir(config, "config directory (FACTORY_CONFIG)"))
    return tuple(path for path in hidden if path.exists())


# ---------------------------------------------------------------------------
# Base branch rule
# ---------------------------------------------------------------------------


def validate_base_branch(cfg, default_branch, log=None):
    """Refuse a base branch that is not the default unless explicitly allowed.

    GitHub only honours ``Closes #N`` in a PR that targets the repository's
    default branch, so any other base leaves issues open. The caller supplies
    ``default_branch`` (this module never talks to GitHub).
    """
    if not isinstance(default_branch, str) or not default_branch:
        raise ValueError("default_branch must be the repository's default branch name")
    if cfg.base_branch == default_branch:
        return
    if not cfg.allow_non_default_base:
        raise ValueError(
            f"base_branch {_show(cfg.base_branch)} is not the default branch {_show(default_branch)} of "
            f"{cfg.repo}: closing keywords such as 'Closes #N' only work on PRs that target the default "
            "branch. Set base_branch to the default branch, or set allow_non_default_base = true."
        )
    if log:
        log(
            f"{cfg.repo}: base_branch {_show(cfg.base_branch)} differs from the default branch "
            f"{_show(default_branch)} (allow_non_default_base is set): issues will stay open "
            "after merge because 'Closes #N' only applies to PRs into the default branch"
        )

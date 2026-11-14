"""Per-phase worker policy: tools, sandbox scope, network, model, effort, budget.

This table is the single source of truth for what a ``claude -p`` worker may do.
``agent.py`` renders a policy into argv and ``backend.py`` into a generated ``--settings`` file;
``managed_settings()`` is the non-overridable baseline shipped to
``/etc/claude-code/managed-settings.json``.

Nothing here disables or weakens the Claude Code sandbox. The sandbox covers
Bash only, so file tools are fenced separately with permission rules.
"""

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit

PHASES = ("plan", "build", "review", "verify", "preflight")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# The phases a repository-wide default model or effort applies to. The preflight probe is
# deliberately left out: it is a cheap capability check (Haiku, low, 0.5 USD), so a Sonnet or
# Opus repository default must not make it dearer. ``[models]``/``[efforts]`` can still set it.
DEFAULT_PHASES = ("plan", "build", "review", "verify")
# The phases a per-issue model or effort label applies to (docs/manager.md, "Model and effort").
# The planner runs before any job exists and the reviewer stays independent of the builder.
ISSUE_PHASES = ("build", "verify")

# Denied in every phase. Subagents (Task) and web tools would run outside the
# per-phase sandbox policy.
ALWAYS_DISALLOWED = ("WebFetch", "WebSearch", "Task")
# Read-only phases (plan, review) may not use the file-editing tools at all.
EDIT_TOOLS = ("Edit", "Write", "NotebookEdit")

# Permission rules shared by every generated settings file and by the managed
# baseline. Paths follow permission-rule syntax: ``//x`` is absolute, a bare
# path is relative to the working directory.
BASELINE_DENY = (
    "Edit(.claude/**)",
    "Edit(.mcp.json)",
    "Edit(.git)",
    "Edit(.git/**)",
    "Read(//proc/**)",
)

# Generic default; a repository may override it (Phase 2 config). It must be on
# the verify allowlist for a verify-phase probe to be meaningful.
DEFAULT_PROBE_URL = "https://registry.npmjs.org/"

_DOMAIN = re.compile(r"(\*\.)?[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]{1,5})?")


@dataclass(frozen=True)
class PhasePolicy:
    name: str
    tools: tuple
    disallowed: tuple
    write_scope: str  # "none": Bash cannot write the working tree; "cwd": it can
    network: str  # "off": no domains; "allowlist": verify domains; "target": see domains_for
    model: str
    effort: str
    budget_usd: float


POLICIES = {
    "plan": PhasePolicy(
        name="plan",
        tools=("Read", "Grep", "Glob", "Bash"),
        disallowed=ALWAYS_DISALLOWED + EDIT_TOOLS,
        write_scope="none",
        network="off",
        model="claude-opus-5-5",
        effort="high",
        budget_usd=5.0,
    ),
    "build": PhasePolicy(
        name="build",
        tools=("Read", "Grep", "Glob", "Edit", "Write", "Bash"),
        disallowed=ALWAYS_DISALLOWED,
        write_scope="cwd",
        network="off",
        model="claude-sonnet-5-5",
        effort="high",
        budget_usd=8.0,
    ),
    "review": PhasePolicy(
        name="review",
        tools=("Read", "Grep", "Glob", "Bash"),
        disallowed=ALWAYS_DISALLOWED + EDIT_TOOLS,
        write_scope="none",
        network="off",
        model="claude-opus-5-5",
        effort="high",
        budget_usd=4.0,
    ),
    "verify": PhasePolicy(
        name="verify",
        tools=("Read", "Grep", "Glob", "Edit", "Write", "Bash"),
        disallowed=ALWAYS_DISALLOWED,
        write_scope="cwd",
        network="allowlist",
        model="claude-sonnet-5-5",
        effort="medium",
        budget_usd=6.0,
    ),
    # The probe runs in the same sandbox as the phase it measures, so its
    # network setting follows the target phase (see domains_for).
    "preflight": PhasePolicy(
        name="preflight",
        tools=("Bash", "Read"),
        disallowed=ALWAYS_DISALLOWED,
        write_scope="cwd",
        network="target",
        model="claude-haiku-5-5",
        effort="low",
        budget_usd=0.5,
    ),
}


@dataclass(frozen=True)
class AgentOptions:
    """Operator-tunable inputs to the policy table (Phase 2 fills these from config)."""

    verify_domains: tuple = ()
    probe_url: str = DEFAULT_PROBE_URL
    models: dict = field(default_factory=dict)
    efforts: dict = field(default_factory=dict)
    budgets: dict = field(default_factory=dict)
    addendum: str = ""
    # Whether the repository's own skills (``.claude/skills``, read from the base ref) go into the system prompt (agent.repo_skills).
    repo_skills: bool = True
    # The most the full texts of those skills may take together, in KiB (agent.repo_skills; the rest are listed by name and path).
    repo_skills_budget_kib: int = 256
    # Repository defaults (``model`` / ``effort``): used by DEFAULT_PHASES that have no
    # ``models`` / ``efforts`` entry. None means the built-in table.
    default_model: str | None = None
    default_effort: str | None = None
    # What an issue's model and effort labels may select (see ``model_choice``): ``aliases`` are the
    # repository's own alias targets (merged over the built-in three), ``allowed_models`` None means
    # the built-in ids plus the repository's own configured ids, ``max_effort`` None means the
    # highest effort the repository already uses (never below the built-in table's).
    aliases: dict = field(default_factory=dict)
    allowed_models: tuple | None = None
    max_effort: str | None = None
    # Called once per agent invocation: directories no worker of this repository may
    # read (other repositories' data, the config directory). A callable, not a tuple,
    # because a repository can be cloned while a run is already in progress.
    hidden_paths: Callable[[], tuple] | None = None


def phase_name(phase):
    """``build-0`` and ``review-2`` are attempts of the ``build`` and ``review`` phases."""
    root = str(phase).split("-", 1)[0]
    if root not in POLICIES:
        raise ValueError(f"unknown agent phase: {phase!r}")
    return root


def positive_budget(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("budget must be a positive number of dollars")
    return number


def _checked_model(root, value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{root}: model must be a non-empty string")
    return value.strip()


def _checked_effort(root, value):
    if value not in EFFORTS:
        raise ValueError(f"{root}: effort must be one of {', '.join(EFFORTS)}")
    return value


def policy_for(phase, options=None, choice=None):
    """The policy of one phase with the operator's options and a job's recorded choice applied.

    Precedence for model and effort, most specific first: ``choice`` (the job's recorded
    ``{"model", "effort"}`` for build and verify, which came from an issue's labels or was resolved
    from the options at plan time), the per-phase ``options.models`` / ``options.efforts``, the
    repository default (``DEFAULT_PHASES`` only), then the built-in table. ``choice`` is only ever
    passed for the phases of ``ISSUE_PHASES``; the caller decides (``Runner.invoke``).
    """
    options = options or AgentOptions()
    root = phase_name(phase)
    policy = POLICIES[root]
    changes = {}
    if root in options.models:
        changes["model"] = _checked_model(root, options.models[root])
    elif root in DEFAULT_PHASES and options.default_model is not None:
        changes["model"] = _checked_model(root, options.default_model)
    if root in options.efforts:
        changes["effort"] = _checked_effort(root, options.efforts[root])
    elif root in DEFAULT_PHASES and options.default_effort is not None:
        changes["effort"] = _checked_effort(root, options.default_effort)
    if choice:
        if "model" in choice:
            changes["model"] = _checked_model(root, choice["model"])
        if "effort" in choice:
            changes["effort"] = _checked_effort(root, choice["effort"])
    if root in options.budgets:
        changes["budget_usd"] = positive_budget(options.budgets[root])
    return replace(policy, **changes) if changes else policy


def normalize_domains(values):
    """Validate sandbox allowlist entries: bare hostnames, optional ``*.`` prefix and port."""
    result = []
    for value in values:
        domain = str(value).strip().lower()
        if not _DOMAIN.fullmatch(domain):
            raise ValueError(f"invalid sandbox domain: {value!r}")
        result.append(domain)
    return tuple(sorted(set(result)))


def domains_for(policy, options=None, target=None):
    """Sandbox network allowlist. Empty unless the policy (or its target) is verify."""
    options = options or AgentOptions()
    if policy.network == "allowlist" or (
        policy.network == "target" and target == "verify"
    ):
        return normalize_domains(options.verify_domains)
    return ()


def probe_host(url):
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("preflight probe URL must be an https URL")
    return parts.hostname.lower()


def domain_allows(domains, host):
    for entry in domains:
        pattern = entry.split(":", 1)[0]
        if pattern == host or (
            pattern.startswith("*.") and host.endswith(pattern[1:])
        ):
            return True
    return False


# The worker's own credential must not be readable by the commands it runs:
# Bash would otherwise inherit it and could write it into a file that the
# supervisor later commits. The claude process itself still authenticates.
CREDENTIALS = {"envVars": [{"name": "ANTHROPIC_API_KEY", "mode": "deny"}]}


def settings_for(
    policy, cwd, domains=(), read_paths=(), deny_write=(), deny_read=()
):
    """The generated ``--settings`` document for one invocation.

    ``failIfUnavailable`` makes a missing bubblewrap fatal; without it Claude
    Code would silently run Bash unsandboxed. ``strictAllowlist`` is honoured
    from ``--settings`` (never from repository files) and denies every host
    outside ``allowedDomains``. Absolute paths are used throughout because a
    ``--settings`` file has no project root to anchor relative paths.

    ``--permission-mode dontAsk`` denies whatever is not pre-approved, and the
    sandbox's auto-allow still sends many ordinary command shapes (heredocs,
    loops, ``find -exec``) through the permission flow, so Bash is allowed
    explicitly. That does not leave the sandbox: unsandboxed retries stay off.

    ``deny_write`` lists extra paths the sandbox must keep read-only. Claude
    Code deliberately leaves a linked worktree's shared ``.git`` writable, so
    the caller passes it here; otherwise a "read-only" reviewer could move
    refs, plant tags or write ``info/attributes``.

    ``deny_read`` lists directories nothing the worker runs may read (other
    repositories' clones and runs, the config directory): all workers share one
    uid and group, and the shared group can read a clone's git directory. Each
    path is denied to the sandbox (Bash) and, because ``denyRead`` does not
    cover the Read tool, also by a ``Read`` permission rule. The paths must be
    disjoint from ``cwd``: a denied region that contains the working directory
    breaks the sandbox setup.
    """
    cwd = str(Path(cwd).resolve())
    sandbox = {
        "enabled": True,
        "failIfUnavailable": True,
        "allowUnsandboxedCommands": False,
        "autoAllowBashIfSandboxed": True,
        "credentials": CREDENTIALS,
        "network": {"allowedDomains": sorted(domains), "strictAllowlist": True},
    }
    permissions = {"allow": ["Bash"], "deny": list(BASELINE_DENY)}
    read_only = []
    if policy.write_scope == "cwd":
        # Editing the assigned worktree (and only it) is explicitly allowed.
        permissions["allow"].append(f"Edit(/{cwd}/**)")
    else:
        read_only.append(cwd)
    read_only.extend(str(Path(path).resolve()) for path in deny_write)
    filesystem = {}
    if read_only:
        filesystem["denyWrite"] = list(dict.fromkeys(read_only))
    hidden = []
    for path in deny_read:
        resolved = Path(path).resolve()
        if Path(cwd) == resolved or Path(cwd).is_relative_to(resolved):
            continue  # never hide the working directory
        hidden.append(str(resolved))
    hidden = list(dict.fromkeys(hidden))
    if hidden:
        filesystem["denyRead"] = hidden
        permissions["deny"].extend(f"Read(/{path}/**)" for path in hidden)
    if filesystem:
        sandbox["filesystem"] = filesystem
    for path in read_paths:
        permissions["allow"].append(f"Read(/{Path(path).resolve()})")
    return {"sandbox": sandbox, "permissions": permissions}


def managed_settings():
    """Baseline for ``/etc/claude-code/managed-settings.json``.

    Boolean and enum keys set here win over every other source, so a worker
    (or a repository) cannot loosen them. Per-phase domains are deliberately
    not managed: they arrive through ``--settings``.
    """
    return {
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "autoAllowBashIfSandboxed": True,
            "enableWeakerNestedSandbox": False,
            "enableWeakerNetworkIsolation": False,
            "credentials": CREDENTIALS,
            "network": {"strictAllowlist": True, "allowAllUnixSockets": False},
        },
        "permissions": {
            "deny": list(BASELINE_DENY + ALWAYS_DISALLOWED),
            "disableBypassPermissionsMode": "disable",
            "disableAutoMode": "disable",
        },
        "disableAllHooks": True,
    }

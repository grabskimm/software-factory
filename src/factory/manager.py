"""Remote control surface: an authenticated HTTP API in front of the factory CLI.

The manager is a thin adapter. Every endpoint turns a validated request into the
argv of one ``factory`` CLI command and runs it as a subprocess; no CLI logic is
reimplemented here. Short commands run synchronously, ``plan`` and ``run`` start
a long-lived child the manager keeps a handle on (so it can pause it and stop it
on SIGTERM).

Trust and hygiene rules (details in docs/manager.md):

* Identity is checked on every request except ``/healthz``, and there is no switch
  that turns the check off. ``FACTORY_MANAGER_AUTH=easy-auth`` (the default) takes it
  only from the Easy Auth ``X-MS-CLIENT-PRINCIPAL`` header; ``entra-jwt`` takes it only
  from a verified Entra bearer token (``entra_jwt.py``) and ignores that header.
* Path parameters are percent-decoded once and validated before any filesystem or
  argv use; request bodies are strictly validated; free text only ever reaches
  argv in ``--flag=value`` form, never through a shell.
* Request bodies, CLI output and agent text are never written to the manager's
  stdout or stderr. The only stdout lines are one startup line, one ``audit``
  line per request, the fixed-format operations lines (docs/manager.md, "Operations loop")
  and one ``github`` line whenever a repository's GitHub App check changes its result.
* The dashboard endpoints (``dashboard.py``) are ``GET`` only, read the run directories and the history
  ledger in this process, and never start a process: no ``gh``, no CLI, no log content.
* The log endpoints (``logs.py``) are ``GET`` only and **Operator only**: a Viewer gets 403. They serve an
  allow-list of a run's logs as plain text, redacted, in bounded ranges; the audit line carries the path and
  the byte count, never content.
* The one exception to the identity check, besides ``/healthz``, is the static shell of the browser page
  (``webui.py``): ``GET /ui/`` and its two assets, an allow-list of exact paths carrying no data and no secret.
  Every data call the page makes is authenticated like any other request.
* ``FACTORY_MANAGER_METRICS_PORT`` (off unless set) opens a second listener that answers only
  ``GET /metrics`` (``metrics.py``): numbers, no Entra identity check, no audit line, a port other than
  the API's. ``FACTORY_MANAGER_METRICS_TOKEN`` makes it want ``Authorization: Bearer <token>``, and the
  listener refuses to start on an address that is not loopback without one.
* The repository lease belongs to the CLI process that holds it. The manager only
  probes that the lease is free before it spawns a supervisor.

Stdlib only. Importing this module has no side effects; ``main()`` does the work.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import ipaddress
import json
import math
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, Callable, Mapping, Protocol, TextIO
from urllib.parse import unquote, urlsplit

from . import history, metrics, podcontract, podexecution, stopworld, webui
from .config import (
    RUN_SLUG,
    DataPaths,
    RepoConfig,
    RepoNotConfigured,
    check_repo,
    data_paths,
    load_repo_config,
)
from .control import MAX_STOP_REASON, clean_text
from .entra_jwt import (
    EntraJwtVerifier,
    JwtError,
    KeysUnavailable,
    validate_audiences,
    validate_client_id,
    validate_scope,
    validate_tenant,
)
from .false_incomplete import FalseIncompleteCache
from .github_health import GithubHealth
from .issue_numbers import check_issue_numbers, issues_flag
from .locks import LockBusy, LockError, RepoLock, lock_from_env
from .phases import positive_budget
from .state import kill_group, timestamp

if TYPE_CHECKING:
    from .operations import Operations

# --- constants ---------------------------------------------------------------------

PRINCIPAL_HEADER = "X-MS-CLIENT-PRINCIPAL"
AUTHORIZATION_HEADER = "Authorization"
AUTH_EASY = "easy-auth"
AUTH_JWT = "entra-jwt"
# Seconds a client is told to wait when the token signing keys cannot be obtained (503).
AUTH_RETRY_SECONDS = 5
ROLE_OPERATOR = "Factory.Operator"
ROLE_VIEWER = "Factory.Viewer"
# Claim types that carry app roles: the principal's own role_typ, plus these two.
ROLE_CLAIM_TYPES = ("roles", "http://schemas.microsoft.com/ws/2008/06/identity/claims/role")
ID_CLAIM_TYPES = (
    "objectidentifier",
    "oid",
    "http://schemas.microsoft.com/identity/claims/objectidentifier",
)
NAME_CLAIM_TYPES = ("name", "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name")
MAX_PRINCIPAL_CHARS = 64 * 1024

JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
MAX_REASON_CHARS = 2000
MAX_TIMEOUT_SECONDS = 86_400
MAX_CONCURRENCY = 64

DEFAULT_MAX_BODY = 64 * 1024
DEFAULT_MAX_INBOX = 1024 * 1024
MAX_OUTPUT_CHARS = 4 * 1024 * 1024
MAX_ERROR_CHARS = 64 * 1024
BODY_READ_SECONDS = 30.0
# Total time for the request line and headers; a client that drips them is dropped.
HEADER_READ_SECONDS = 20.0
# How long a request waits for a free CLI slot before it gets 503 + Retry-After.
SLOT_WAIT_SECONDS = 2.0
DRAIN_SECONDS = 2.0  # lingering close: how long to swallow an unread request body
DRAIN_LIMIT = 8 * 1024 * 1024
# How long shutdown waits for the operations threads after the children are gone.
OPERATIONS_JOIN_SECONDS = 5.0
MAX_LISTED_RUNS = 500
MAX_FINISHED_KEPT = 64
ID_LOG_CHARS = 128
FIELD_LOG_CHARS = 200

LOG_NAMES = {"plan": "plan.log", "run": "supervisor.log", "append": "append.log"}
NOT_FOUND = "not found"
LEASE_BUSY = "lease held elsewhere"
FOLLOWUP_REASON = "follow-up requested"  # the fixed reason of a follow-up session (never a comment's words)
# The trigger-pause marker, at the top of the data directory (not in any repository's tree, so it
# survives a restart and no worker can read it). While it exists the manager starts no plan or run by itself.
TRIGGER_PAUSE_FILE = "trigger-paused"
TRIGGER_PAUSED = "trigger paused"
# The stop-the-world breaker (stopworld.py) pauses the trigger through its OWN marker, ``spend-breaker``: the
# manual marker above is cleared by the deploy gate after every automatic image promotion, the breaker only by
# an Operator (``POST /spend-breaker``).


class ApiError(Exception):
    """A request failure that maps to one HTTP status and a short, fixed message."""

    def __init__(self, status: int, message: str, headers: Mapping[str, str] | None = None, **extra: object):
        super().__init__(message)
        self.status = status
        self.message = message
        self.headers = dict(headers or {})
        self.extra = extra

    def reply(self) -> "Reply":
        return Reply(self.status, {"error": self.message, **self.extra}, self.headers)


@dataclass
class Reply:
    status: int
    payload: dict
    headers: dict = field(default_factory=dict)
    body: bytes | None = None  # a raw body (the static page) instead of the JSON of ``payload``


@dataclass
class Trace:
    """What the audit line needs to know about one request, filled in as it proceeds."""

    principal: str = "anonymous"
    argv: list = field(default_factory=list)
    job: str | None = None  # the job a successful action named, for the history ledger's operator entry
    bytes: int | None = None  # the bytes of log text one view returned (audit only; never the text)



class HeaderSource(Protocol):
    """Request headers: anything with a case-insensitive ``get(name, default)``."""

    def get(self, name: str, default: str | None = None) -> str | None: ...

@dataclass
class Request:
    method: str
    target: str
    headers: HeaderSource  # anything with a case-insensitive .get(name)
    read_body: Callable[[int], bytes]
    peer: str = ""  # the TCP peer address (never taken from a header)


# --- settings ----------------------------------------------------------------------


def _number(env: Mapping[str, str], name: str, default: float, minimum: float, maximum: float) -> float:
    text = (env.get(name) or "").strip()
    if not text:
        return default
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"{name} must be a number") from None
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
    return value


METRICS_TOKEN_ENV = "FACTORY_MANAGER_METRICS_TOKEN"


def _is_loopback(host: str) -> bool:
    """True for ``localhost`` and a loopback address literal; any other name or address is not (fail closed)."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _whole(env: Mapping[str, str], name: str, default: int, minimum: int, maximum: int) -> int:
    value = _number(env, name, default, minimum, maximum)
    if value != int(value):
        raise ValueError(f"{name} must be an integer")
    return int(value)


def _switch(env: Mapping[str, str], name: str, default: bool) -> bool:
    text = (env.get(name) or "").strip().lower()
    if not text:
        return default
    if text not in ("on", "off"):
        raise ValueError(f"{name} must be 'on' or 'off'")
    return text == "on"


def _required_dir(env: Mapping[str, str], name: str) -> Path:
    text = (env.get(name) or "").strip()
    if not text:
        raise ValueError(f"{name} is not set")
    if "\x00" in text or ".." in Path(text).parts:
        raise ValueError(f"{name} must be a path without '..' components")
    return Path(os.path.abspath(text))


@dataclass(frozen=True)
class Settings:
    """Everything the manager itself is configured with (all from the environment)."""

    config_dir: Path
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = 8080
    command_timeout: float = 300.0
    shutdown_seconds: float = 25.0
    pause_seconds: float = 60.0
    request_timeout: float = 15.0
    cli: tuple = (sys.executable, "-m", "factory")
    max_body: int = DEFAULT_MAX_BODY
    max_inbox: int = DEFAULT_MAX_INBOX
    # Concurrent short CLI commands, in two lanes so that read-only polling can never starve
    # an operator action: `status` (Viewer and Operator) and everything else (Operator).
    max_status_commands: int = 6
    max_other_commands: int = 8
    # The operations loop (restart recovery, cost guardrail, auto-purge); see operations.py.
    operations: bool = True
    lease_wait_seconds: float = 120.0
    poll_seconds: float = 60.0
    janitor_seconds: float = 900.0
    # Label-triggered builds (trigger.py, operations.py): 0 turns the watcher off for every repository.
    trigger_seconds: float = 60.0
    # The history ledger's bounds (history.py): total bytes on disk, and days before a segment is dropped.
    history_max_bytes: int = history.DEFAULT_MAX_BYTES
    history_retention_days: int = history.DEFAULT_RETENTION_DAYS
    # Authentication mode (see docs/manager.md, "Trust model"); the Entra settings matter only for entra-jwt.
    auth: str = AUTH_EASY
    entra_tenant_id: str = ""
    entra_audiences: tuple = ()
    # What the browser page signs in with (webui.sign_in_config): empty means "derive it from the audiences".
    entra_spa_client_id: str = ""
    entra_spa_scope: str = ""
    # The metrics listener (metrics.py, docs/metrics.md): None means off, which is the default. The token is
    # a secret: it stays out of repr() and of every line the manager writes.
    metrics_port: int | None = None
    metrics_host: str = "127.0.0.1"
    metrics_token: str = field(default="", repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        host = (env.get("FACTORY_MANAGER_HOST") or "127.0.0.1").strip()
        if not host or re.search(r"\s", host):
            raise ValueError("FACTORY_MANAGER_HOST must be a host name or address")
        port = _number(env, "FACTORY_MANAGER_PORT", 8080, 0, 65535)
        if port != int(port):
            raise ValueError("FACTORY_MANAGER_PORT must be an integer")
        cli = tuple((env.get("FACTORY_MANAGER_CLI") or "").split()) or cls.cli
        auth, tenant, audiences = cls._auth_from_env(env)
        spa_client_id, spa_scope = cls._spa_from_env(env, auth)
        metrics_port, metrics_host, metrics_token = cls._metrics_from_env(env, int(port))
        from .logarchive import Config as LogArchiveConfig  # an invalid FACTORY_LOGS_ARCHIVE* setting fails at startup

        LogArchiveConfig.from_env(env)
        return cls(
            config_dir=_required_dir(env, "FACTORY_CONFIG"),
            data_dir=_required_dir(env, "FACTORY_DATA"),
            host=host,
            port=int(port),
            command_timeout=_number(env, "FACTORY_MANAGER_COMMAND_TIMEOUT", 300, 1, 86_400),
            shutdown_seconds=_number(env, "FACTORY_MANAGER_SHUTDOWN_SECONDS", 25, 0, 3_600),
            pause_seconds=_number(env, "FACTORY_MANAGER_PAUSE_SECONDS", 60, 1, 3_600),
            request_timeout=_number(env, "FACTORY_MANAGER_REQUEST_TIMEOUT", 15, 1, 600),
            cli=cli,
            max_status_commands=int(_number(env, "FACTORY_MANAGER_MAX_STATUS_COMMANDS", 6, 1, 64)),
            max_other_commands=int(_number(env, "FACTORY_MANAGER_MAX_COMMANDS", 8, 1, 64)),
            operations=_switch(env, "FACTORY_MANAGER_OPERATIONS", True),
            lease_wait_seconds=_number(env, "FACTORY_MANAGER_LEASE_WAIT_SECONDS", 120, 0, 3_600),
            poll_seconds=_number(env, "FACTORY_MANAGER_POLL_SECONDS", 60, 1, 3_600),
            janitor_seconds=_number(env, "FACTORY_MANAGER_JANITOR_SECONDS", 900, 1, 86_400),
            trigger_seconds=_number(env, "FACTORY_MANAGER_TRIGGER_SECONDS", 60, 0, 86_400),
            history_max_bytes=_whole(
                env, history.ENV_MAX_BYTES, history.DEFAULT_MAX_BYTES, history.MIN_MAX_BYTES, history.MAX_MAX_BYTES
            ),
            history_retention_days=_whole(
                env,
                history.ENV_RETENTION_DAYS,
                history.DEFAULT_RETENTION_DAYS,
                history.MIN_RETENTION_DAYS,
                history.MAX_RETENTION_DAYS,
            ),
            auth=auth,
            entra_tenant_id=tenant,
            entra_audiences=audiences,
            entra_spa_client_id=spa_client_id,
            entra_spa_scope=spa_scope,
            metrics_port=metrics_port,
            metrics_host=metrics_host,
            metrics_token=metrics_token,
        )

    @staticmethod
    def _metrics_from_env(env: Mapping[str, str], api_port: int) -> tuple:
        """(port or None, host, token) of the metrics listener.

        Unset, empty or ``off`` is off, and the port is never the API's. A token, when given, is checked for shape
        (the messages never carry its value). A listener on an address that is not loopback must have one: it has
        no other check, so it fails closed here, before anything listens.
        """
        host = (env.get("FACTORY_MANAGER_METRICS_HOST") or "127.0.0.1").strip()
        if not host or re.search(r"\s", host):
            raise ValueError("FACTORY_MANAGER_METRICS_HOST must be a host name or address")
        token = (env.get(METRICS_TOKEN_ENV) or "").strip()
        if token and not re.fullmatch(r"[\x21-\x7e]{32,512}", token):
            raise ValueError(f"{METRICS_TOKEN_ENV} must be 32 to 512 printable characters without spaces")
        text = (env.get("FACTORY_MANAGER_METRICS_PORT") or "").strip().lower()
        if text in ("", "off"):
            return None, host, token
        port = _whole(env, "FACTORY_MANAGER_METRICS_PORT", 0, 0, 65535)
        if port != 0 and port == api_port:
            raise ValueError("FACTORY_MANAGER_METRICS_PORT must differ from FACTORY_MANAGER_PORT")
        if not token and not _is_loopback(host):
            raise ValueError(f"{METRICS_TOKEN_ENV} is required when FACTORY_MANAGER_METRICS_HOST is not a loopback address")
        return port, host, token

    @staticmethod
    def _auth_from_env(env: Mapping[str, str]) -> tuple:
        mode = (env.get("FACTORY_MANAGER_AUTH") or "").strip() or AUTH_EASY
        if mode not in (AUTH_EASY, AUTH_JWT):
            raise ValueError(f"FACTORY_MANAGER_AUTH must be '{AUTH_EASY}' or '{AUTH_JWT}'")
        if mode == AUTH_EASY:
            return mode, "", ()
        try:
            tenant = validate_tenant(env.get("FACTORY_ENTRA_TENANT_ID") or "")
        except ValueError as error:
            raise ValueError(f"FACTORY_ENTRA_TENANT_ID {error}") from None
        try:
            audiences = validate_audiences(env.get("FACTORY_ENTRA_AUDIENCES") or "")
        except ValueError as error:
            raise ValueError(f"FACTORY_ENTRA_AUDIENCES {error}") from None
        return mode, tenant, audiences

    @staticmethod
    def _spa_from_env(env: Mapping[str, str], mode: str) -> tuple:
        """(client id, scope) the browser page signs in with; both optional, and only read in ``entra-jwt`` mode."""
        if mode != AUTH_JWT:
            return "", ""
        try:
            client_id = validate_client_id(env.get("FACTORY_ENTRA_SPA_CLIENT_ID") or "")
        except ValueError as error:
            raise ValueError(f"FACTORY_ENTRA_SPA_CLIENT_ID {error}") from None
        try:
            scope = validate_scope(env.get("FACTORY_ENTRA_SPA_SCOPE") or "")
        except ValueError as error:
            raise ValueError(f"FACTORY_ENTRA_SPA_SCOPE {error}") from None
        return client_id, scope


# --- identity ----------------------------------------------------------------------


class PrincipalError(ValueError):
    """The X-MS-CLIENT-PRINCIPAL header is missing or not a well-formed principal."""


@dataclass(frozen=True)
class Principal:
    ident: str
    roles: frozenset

    def can_read(self) -> bool:
        return bool(self.roles & {ROLE_OPERATOR, ROLE_VIEWER})

    def can_mutate(self) -> bool:
        return ROLE_OPERATOR in self.roles

    def can_read_logs(self) -> bool:
        """Transcripts and logs hold agent-authored text and repository content: Operator only, no new role."""
        return ROLE_OPERATOR in self.roles


def _decode_principal(header: str) -> dict:
    text = header.strip()
    try:
        raw = base64.b64decode(f"{text}{'=' * (-len(text) % 4)}", validate=True)
        document = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):  # bad base64, bad UTF-8 and bad JSON are all ValueErrors
        raise PrincipalError("principal header is not base64-encoded JSON") from None
    if not isinstance(document, dict):
        raise PrincipalError("principal must be a JSON object")
    return document


def _claims(document: Mapping[str, object]) -> list:
    claims = document.get("claims")
    if not isinstance(claims, list):
        raise PrincipalError("principal has no claims list")
    for claim in claims:
        if not (
            isinstance(claim, dict) and isinstance(claim.get("typ"), str) and isinstance(claim.get("val"), str)
        ):
            raise PrincipalError("principal has a malformed claim")
    return claims


def _role_types(document: Mapping[str, object]) -> set:
    types = set(ROLE_CLAIM_TYPES)
    role_typ, name_typ = document.get("role_typ"), document.get("name_typ")
    # A principal whose "role" type is really its name or object id would let a user
    # pick a display name that equals a role; never honour that.
    if isinstance(role_typ, str) and role_typ and role_typ != name_typ and role_typ not in ID_CLAIM_TYPES:
        types.add(role_typ)
    return types


def _identifier(document: Mapping[str, object], claims: list) -> str:
    name_types = list(NAME_CLAIM_TYPES)
    if isinstance(document.get("name_typ"), str):
        name_types.append(document["name_typ"])
    for wanted in (ID_CLAIM_TYPES, name_types):
        for claim in claims:
            if claim["typ"] in wanted and claim["val"]:
                return claim["val"][:ID_LOG_CHARS]
    return "unknown"


def parse_principal(header: str | None) -> Principal:
    """The caller's identifier and app roles from the Easy Auth header, or PrincipalError."""
    if not header or len(header) > MAX_PRINCIPAL_CHARS:
        raise PrincipalError("principal header is missing or too large")
    document = _decode_principal(header)
    claims = _claims(document)
    role_types = _role_types(document)
    roles = frozenset(claim["val"] for claim in claims if claim["typ"] in role_types)
    return Principal(_identifier(document, claims), roles)


# --- body validation ---------------------------------------------------------------


def _int_in(low: int, high: int) -> Callable[[object], int]:
    def check(value: object) -> int:
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"must be an integer from {low} to {high}")
        return value

    return check


def _job_id(value: object) -> str:
    if not isinstance(value, str) or not JOB_ID.fullmatch(value):
        raise ValueError("must match [A-Za-z0-9][A-Za-z0-9_-]{0,63}")
    return value


def _reason(value: object) -> str:
    if not isinstance(value, str) or len(value) > MAX_REASON_CHARS or "\x00" in value:
        raise ValueError(f"must be text of at most {MAX_REASON_CHARS} characters without NUL")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("must be valid Unicode text") from None
    return value


def _stop_reason(value: object) -> str | None:
    """The optional reason of a stop: text of at most ``MAX_STOP_REASON`` characters, stored as sanitised data.

    Control and format characters become spaces and runs of spaces collapse (``control.clean_text``); a reason
    that is empty after that is no reason.
    """
    if not isinstance(value, str) or len(value) > MAX_STOP_REASON:
        raise ValueError(f"must be text of at most {MAX_STOP_REASON} characters")
    return clean_text(value, MAX_STOP_REASON)


def _flag(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError("must be true or false")
    return value


def _choice(*allowed: str) -> Callable[[object], str]:
    def check(value: object) -> str:
        if not isinstance(value, str) or value not in allowed:
            raise ValueError(f"must be one of {', '.join(allowed)}")
        return value

    return check


def _budget(value: object) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("must be a positive number of dollars")
    try:
        positive_budget(value)
    except (ValueError, OverflowError):
        raise ValueError("must be a positive, finite number of dollars") from None
    return value


def _budgets(*phases: str) -> Callable[[object], dict]:
    def check(value: object) -> dict:
        if not isinstance(value, dict) or not value:
            raise ValueError(f"must be an object with keys from: {', '.join(phases)}")
        unknown = [key for key in value if key not in phases]
        if unknown:
            raise ValueError(f"has unknown phase keys; allowed: {', '.join(phases)}")
        return {phase: _budget(value[phase]) for phase in phases if phase in value}

    return check


@dataclass(frozen=True)
class Action:
    """One run-scoped endpoint: its HTTP method and what its body may contain."""

    method: str
    fields: Mapping[str, Callable[[object], object]] = field(default_factory=dict)
    required: tuple = ()
    inbox: str | None = None  # "object" or "array": the body itself is the CLI's --file
    # A browser could be tricked into sending it (a cookie session in easy-auth mode): require a JSON content
    # type and refuse a request that a page of another origin sent (Origin, Sec-Fetch-Site).
    same_origin_json: bool = False


_TIMEOUT = _int_in(1, MAX_TIMEOUT_SECONDS)
_WORKERS = _int_in(1, MAX_CONCURRENCY)

ACTIONS: dict[str, Action] = {
    "plan": Action(
        "POST",
        {"limit": _int_in(1, 500), "issues": check_issue_numbers, "timeout": _TIMEOUT, "budgets": _budgets("plan")},
    ),
    "run": Action(
        "POST",
        {
            "concurrency": _WORKERS,
            "verification_concurrency": _WORKERS,
            "review_concurrency": _WORKERS,
            "max_active_agents": _WORKERS,
            "timeout": _TIMEOUT,
            "review_rounds": _int_in(0, 3),
            "budgets": _budgets("build", "review", "verify"),
        },
    ),
    "status": Action("GET"),
    "drain": Action("POST"),
    "pause": Action("POST"),
    "resume": Action(
        "POST",
        {"job": _job_id, "phase": _choice("review", "verify", "publish"), "reason": _reason},
        required=("job", "phase"),
    ),
    "retry": Action("POST", {"job": _job_id, "reason": _reason}, required=("job",)),
    "stop": Action(
        "POST", {"job": _job_id, "reason": _stop_reason}, required=("job",), same_origin_json=True
    ),
    "preflight": Action(
        "POST", {"job": _job_id, "phase": _choice("build", "verify")}, required=("job", "phase")
    ),
    "record-capabilities": Action("POST", inbox="array"),
    "record-blocker": Action("POST", inbox="object"),
    "import-evidence": Action("POST", inbox="object"),
    "purge": Action("POST"),
}

# Read-only endpoints (dashboard.py): path shape -> name. All are GET and open to Viewer and Operator alike.
READS = ("dashboard", "history-runs", "history-jobs", "job-detail")
# Log endpoints (logs.py): GET, and Operator only (a Viewer gets 403). The listing of a run's or a job's logs, and one file.
LOG_READS = ("run-logs", "run-log", "job-logs", "job-log")
MAX_LOG_READERS = 4  # concurrent log reads; another waits SLOT_WAIT_SECONDS, then 503

# Manager-wide, not run-scoped: sets or clears the trigger pause (Operator only, audited like every request).
TRIGGER_PAUSE_ACTION = Action("POST", {"paused": _flag}, required=("paused",))
# Manager-wide too: clears the stop-the-world breaker (Operator only, audited). ``clear`` must be true.
SPEND_BREAKER_ACTION = Action("POST", {"clear": _flag}, required=("clear",))

# Body key -> CLI flag, in the order they appear in argv. Budgets are handled apart.
PLAN_FLAGS = (("limit", "--limit"), ("timeout", "--timeout"))
RUN_FLAGS = (
    ("concurrency", "--concurrency"),
    ("verification_concurrency", "--verification-concurrency"),
    ("review_concurrency", "--review-concurrency"),
    ("max_active_agents", "--max-active-agents"),
    ("timeout", "--timeout"),
    ("review_rounds", "--review-rounds"),
)
BUDGET_PHASES = {"plan": ("plan",), "run": ("build", "review", "verify")}


def _reject_duplicates(pairs: list) -> dict:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def _reject_constant(name: str) -> None:
    raise ValueError(f"unsupported JSON constant {name}")


def parse_json(raw: bytes) -> object:
    """Strict JSON: UTF-8, no duplicate keys, no NaN or Infinity."""
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant)
    except (ValueError, RecursionError):  # includes bad UTF-8 and over-long integers
        raise ApiError(400, "body is not valid JSON") from None


def validate_fields(raw: bytes, action: Action) -> dict:
    """The normalized body of a field-based endpoint (an empty body means no fields)."""
    document = parse_json(raw) if raw.strip() else {}
    if not isinstance(document, dict):
        raise ApiError(400, "body must be a JSON object")
    unknown = [key for key in document if key not in action.fields]
    if unknown:
        allowed = ", ".join(sorted(action.fields)) or "none"
        raise ApiError(400, "invalid body", detail=f"unknown field; allowed: {allowed}")
    body = {}
    for key, value in document.items():
        try:
            body[key] = action.fields[key](value)
        except ValueError as error:
            raise ApiError(400, "invalid body", detail=f"{key} {error}") from None
    missing = [key for key in action.required if key not in body]
    if missing:
        raise ApiError(400, "invalid body", detail=f"missing required field: {', '.join(missing)}")
    return body


def validate_inbox(raw: bytes, action: Action) -> object:
    """The parsed document of an inbox endpoint; its type is what the CLI file loader expects."""
    if not raw.strip():
        raise ApiError(400, "body is empty")
    document = parse_json(raw)
    expected = dict if action.inbox == "object" else list
    if not isinstance(document, expected):
        raise ApiError(400, f"body must be a JSON {action.inbox}")
    if action.inbox == "array" and not all(isinstance(item, dict) for item in document):
        raise ApiError(400, "body must be an array of JSON objects")
    return document


# --- argv --------------------------------------------------------------------------


def _number_text(value: float | int) -> str:
    return str(value) if isinstance(value, int) else repr(value)


def _flag_args(body: Mapping[str, object], flags: tuple) -> list:
    argv: list = []
    for key, flag in flags:
        if key in body:
            argv += [flag, _number_text(body[key])]
    return argv


def _budget_args(body: Mapping[str, object], phases: tuple) -> list:
    argv: list = []
    for phase in phases:
        if phase in body.get("budgets", {}):
            argv += [f"--budget-{phase}", _number_text(body["budgets"][phase])]
    return argv


def _reason_args(body: Mapping[str, object]) -> list:
    # --flag=value: a reason that starts with '-' can never be read as an option.
    return [f"--reason={body['reason']}"] if "reason" in body else []


def command_tail(action: str, body: Mapping[str, object]) -> list:
    """The CLI subcommand and its arguments for a field-based endpoint (after --repo/--run-dir)."""
    if action == "plan":
        issues = [issues_flag(body["issues"])] if "issues" in body else []
        return ["plan", *_flag_args(body, PLAN_FLAGS), *issues, *_budget_args(body, BUDGET_PHASES["plan"])]
    if action == "run":
        return ["run", *_flag_args(body, RUN_FLAGS), *_budget_args(body, BUDGET_PHASES["run"])]
    if action == "status":
        return ["status", "--json"]
    if action == "resume":
        return ["resume", body["job"], "--phase", body["phase"], *_reason_args(body)]
    if action == "retry":
        return ["retry", body["job"], *_reason_args(body)]
    if action == "stop":
        # The reason (sanitised, possibly empty) and the principal (a clipped identity) are data after --flag=value.
        reason = [f"--reason={body['reason']}"] if body.get("reason") else []
        principal = [f"--principal={body['principal']}"] if body.get("principal") else []
        return ["stop", body["job"], "--json", *reason, *principal]
    if action == "preflight":
        return ["preflight", body["job"], "--phase", body["phase"]]
    return [action]  # drain, purge


REDACTED_FLAGS = ("--reason=", "--principal=")


def redact_argv(argv: list) -> list:
    """The argv as audited: free text (--reason, and the --principal of a stop) is replaced by its length."""
    out = []
    for item in argv:
        prefix = next((flag for flag in REDACTED_FLAGS if item.startswith(flag)), None)
        out.append(f"{prefix}<{len(item) - len(prefix)} chars>" if prefix else item)
    return out


def require_same_origin_json(request: "Request") -> None:
    """415 without a JSON content type; 403 for a request a page of another origin sent.

    A browser attaches the Origin header to a ``POST`` and, from any current one, Sec-Fetch-Site. Clients that
    are no browser (curl, the CLI) send neither and pass. The API reads a bearer token, never a cookie, in
    entra-jwt mode, but easy-auth mode runs behind a cookie session: there a cross-origin form could otherwise
    reach it, and a JSON content type cannot be sent by a form (a cross-origin fetch needs a preflight).
    """
    kind = (request.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if kind != "application/json":
        raise ApiError(415, "Content-Type must be application/json")
    site = (request.headers.get("Sec-Fetch-Site") or "").strip().lower()
    origin = request.headers.get("Origin")
    host = (request.headers.get("Host") or "").strip().lower()
    if site not in ("", "same-origin", "none") or (
        origin is not None and (not host or urlsplit(origin.strip()).netloc.lower() != host)
    ):
        raise ApiError(403, "cross-origin request refused")


# --- audit formatting --------------------------------------------------------------

_LOG_SAFE = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._~:/@%+-")


def escape_field(value: object, limit: int = FIELD_LOG_CHARS) -> str:
    """Printable, space-free ASCII: controls, spaces, '=' and backslashes become \\xNN escapes."""
    text = str(value)
    clipped = text[:limit]
    parts = []
    for char in clipped:
        code = ord(char)
        if char in _LOG_SAFE:
            parts.append(char)
        elif code < 0x100:
            parts.append(f"\\x{code:02x}")
        elif code < 0x10000:
            parts.append(f"\\u{code:04x}")
        else:
            parts.append(f"\\U{code:08x}")
    if len(text) > limit:
        parts.append("...")
    return "".join(parts)


def audit_line(trace: Trace, method: str, path: str, status: int) -> str:
    argv = json.dumps(redact_argv(trace.argv), separators=(",", ":"))
    viewed = f" bytes={trace.bytes}" if trace.bytes is not None else ""
    return (
        f"audit principal={escape_field(trace.principal, ID_LOG_CHARS)} method={escape_field(method, 16)} "
        f"path={escape_field(path)} status={status} argv={argv}{viewed}"
    )


class Sink:
    """The manager's only outputs. stdout carries the startup, audit, operations and GitHub App check lines; stderr carries warnings.

    The streams are looked up at write time so redirecting ``sys.stdout`` works.
    Nothing from a request body or a CLI process is ever passed in here.
    """

    def __init__(self, stdout: TextIO | None = None, stderr: TextIO | None = None):
        self._stdout, self._stderr = stdout, stderr
        self._mutex = threading.Lock()

    def line(self, text: str) -> None:
        self._emit(sys.stdout if self._stdout is None else self._stdout, text)

    def warn(self, message: str) -> None:
        self._emit(sys.stderr if self._stderr is None else self._stderr, f"factory-manager: {message}")

    def _emit(self, stream: TextIO, text: str) -> None:
        with self._mutex:
            # The sink being gone (closed pipe) leaves nowhere else to report to.
            with contextlib.suppress(OSError, ValueError):
                stream.write(f"{text}\n")
                stream.flush()


# --- subprocesses ------------------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    returncode: int | None
    stdout: bytes = b""
    stderr: bytes = b""
    timed_out: bool = False


class RunnerClosed(Exception):
    """The runner no longer starts commands: the manager is shutting down."""


class SlotsBusy(Exception):
    """Every slot of the command's lane stayed taken for ``SLOT_WAIT_SECONDS``."""


class Runner(Protocol):
    """How short CLI commands run. ``SubprocessRunner`` is the real one; tests inject fakes."""

    def __call__(self, argv: list, timeout: float) -> CommandResult: ...

    def close(self) -> None:
        """Refuse every later command with ``RunnerClosed``."""

    def interrupt_all(self) -> int:
        """SIGINT every in-flight command's group; returns how many were running."""

    def live_count(self) -> int:
        """How many commands are in flight."""

    def kill_all(self) -> int:
        """SIGKILL every in-flight command's group; returns how many were still running."""


class SubprocessRunner:
    """Runs a short CLI command in its own session with a timeout; the group is killed on expiry."""

    def __init__(self, env: Mapping[str, str], cwd: Path):
        self._env, self._cwd = dict(env), str(cwd)
        self._live: set = set()
        self._closed = False
        self._mutex = threading.Lock()

    def __call__(self, argv: list, timeout: float) -> CommandResult:
        # Started and registered under the mutex, so a command can never slip in after
        # close() and escape kill_all().
        with self._mutex:
            if self._closed:
                raise RunnerClosed()
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._env,
                cwd=self._cwd,
                start_new_session=True,
            )
            self._live.add(process)
        try:
            try:
                out, err = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                kill_group(process.pid)
                return CommandResult(self._reap(process), timed_out=True)
            return CommandResult(process.returncode, out, err)
        finally:
            with self._mutex:
                self._live.discard(process)

    @staticmethod
    def _reap(process: subprocess.Popen) -> int | None:
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:  # a grandchild outside the group still holds the pipes
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()
            process.wait(timeout=5)
        return process.returncode

    def close(self) -> None:
        with self._mutex:
            self._closed = True

    def _running(self) -> list:
        with self._mutex:
            return [p for p in self._live if p.poll() is None]

    def live_count(self) -> int:
        return len(self._running())

    def interrupt_all(self) -> int:
        """SIGINT every in-flight command's group so the CLI can release its lease and clean up."""
        live = self._running()
        for process in live:
            kill_group(process.pid, signal.SIGINT)
        return len(live)

    def kill_all(self) -> int:
        """SIGKILL every in-flight command's group (shutdown); returns how many were still running."""
        live = self._running()
        for process in live:
            kill_group(process.pid)
        return len(live)


class GroupProcess:
    """A child the manager started as its own session/group leader."""

    def __init__(self, process: subprocess.Popen):
        self._process = process
        self.pid = process.pid

    def poll(self) -> int | None:
        return self._process.poll()

    def wait(self, timeout: float | None = None) -> int | None:
        try:
            return self._process.wait(timeout)
        except subprocess.TimeoutExpired:
            return None

    def signal_group(self, sig: int) -> bool:
        if self._process.poll() is not None:
            return False  # reaped: the pid may already belong to something else
        return kill_group(self.pid, sig)


class SubprocessSpawner:
    """Starts a detached child whose stdout and stderr go to a private (0600) log file."""

    def __init__(self, env: Mapping[str, str], cwd: Path):
        self._env, self._cwd = dict(env), str(cwd)

    def __call__(self, argv: list, log_path: Path) -> GroupProcess:
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC
        descriptor = os.open(log_path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)  # also tightens a log that already existed
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=descriptor,
                stderr=descriptor,
                env=self._env,
                cwd=self._cwd,
                start_new_session=True,
            )
        finally:
            os.close(descriptor)
        return GroupProcess(process)


@dataclass
class Child:
    """A plan or run process this manager started."""

    kind: str
    repo: str
    run: str
    process: subprocess.Popen
    started: str
    interrupted: bool = False
    # Makes "interrupt once" atomic between a pause and a shutdown.
    gate: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @property
    def pid(self) -> int:
        return self.process.pid

    def running(self) -> bool:
        return self.process.poll() is None

    def describe(self) -> dict:
        return {"kind": self.kind, "run": self.run, "pid": self.pid, "started": self.started, "running": self.running()}


def _key(repo: str, run: str, kind: str) -> tuple:
    """The registry slot of a child: ``(repo, run)``, or ``(repo, run, "append")`` for an append.

    An append runs beside the supervisor of its run, never instead of it, so it has a slot of its own.
    """
    return (repo, run, "append") if kind == "append" else (repo, run)


class Children:
    """The registry of started children, keyed by (repo, run), or (repo, run, "append") for an append. Owns no global state.

    A ``plan`` or ``run`` child holds the main slot of its run; an ``append`` child (a
    ``plan --append-to`` started while the supervisor of that run is alive) holds the append slot,
    so it neither replaces the supervisor's entry nor can it be mistaken for it by pause or status.
    """

    def __init__(self) -> None:
        self._mutex = threading.Lock()
        self._active: dict[tuple, Child] = {}
        self._finished: dict[tuple, dict] = {}

    def register(self, child: Child) -> None:
        with self._mutex:
            self._active[_key(child.repo, child.run, child.kind)] = child

    def reap(self) -> None:
        """Collect exit codes of finished children so none stays a zombie."""
        with self._mutex:
            for key, child in list(self._active.items()):
                code = child.process.poll()
                if code is None:
                    continue
                del self._active[key]
                self._finished.pop(key, None)  # the newest exit of a slot is the last row
                self._finished[key] = {
                    "kind": child.kind,
                    "run": child.run,
                    "pid": child.pid,
                    "started": child.started,
                    "finished": timestamp(),
                    "returncode": code,
                }
                while len(self._finished) > MAX_FINISHED_KEPT:
                    self._finished.pop(next(iter(self._finished)))

    def active_for_repo(self, repo: str, kind: str | None = None) -> Child | None:
        """A running child of this repository (of this ``kind`` when given); the main slots come first."""
        self.reap()
        with self._mutex:
            found = [child for key, child in self._active.items() if key[0] == repo]
        found.sort(key=lambda child: child.kind == "append")
        return next((child for child in found if kind in (None, child.kind)), None)

    def get(self, repo: str, run: str) -> Child | None:
        """The ``plan`` or ``run`` child of a run (what pause and status act on), never its append."""
        self.reap()
        with self._mutex:
            return self._active.get((repo, run))

    def running(self) -> list:
        self.reap()
        with self._mutex:
            return list(self._active.values())

    def recent_exits(self) -> list:
        """``(repo, row)`` of every exit that is remembered, oldest first (the rows ``last_exit`` returns)."""
        self.reap()
        with self._mutex:
            return [(key[0], dict(row)) for key, row in self._finished.items()]

    def last_exit(self, repo: str, run: str | None = None, limit: int = 5, kind: str | None = None) -> list:
        """The newest exits of this repository, oldest first.

        With a ``run`` the answer is that run's ``plan``/``run`` exits, or its append exits when
        ``kind="append"``; without one it lists every exit of the repository.
        """
        with self._mutex:
            rows = [
                row
                for key, row in self._finished.items()
                if key[0] == repo and (run is None or key == _key(repo, run, kind or ""))
            ]
        return rows[-limit:]

    def forget(self, repo: str, run: str) -> None:
        with self._mutex:
            for kind in ("", "append"):
                self._finished.pop(_key(repo, run, kind), None)


# --- inbox -------------------------------------------------------------------------


def write_inbox(run_dir: Path, raw: bytes) -> Path:
    """Persist a request body as ``<run>/inbox/<sha256>.json`` (file 0600, directory 0700, atomic).

    Content addressed, so replaying the same body rewrites the same file. The
    directory is opened with O_NOFOLLOW and the file is created relative to that
    descriptor, so a planted symlink cannot redirect the write.
    """
    digest = hashlib.sha256(raw).hexdigest()
    inbox = run_dir / "inbox"
    inbox.mkdir(mode=0o700, exist_ok=True)
    directory = os.open(inbox, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fchmod(directory, 0o700)
        temp = f".{digest}.{secrets.token_hex(4)}.tmp"
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, f"{digest}.json", src_dir_fd=directory, dst_dir_fd=directory)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):  # already renamed, or never created
                os.unlink(temp, dir_fd=directory)
            raise
        os.fsync(directory)
    finally:
        os.close(directory)
    return inbox / f"{digest}.json"


# --- the manager -------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """A validated repository and run: only these values ever reach argv or the filesystem."""

    repo: str
    run: str
    cfg: RepoConfig
    paths: DataPaths
    run_dir: Path


def _lease_lost_ignored() -> None:
    """on_lost for the probe: it holds the lease for milliseconds and nothing depends on it."""


def env_lock_factory(env: Mapping[str, str]) -> Callable[[RepoConfig, DataPaths], RepoLock]:
    """The same lock the supervisor takes: a blob lease or a flock, named after the repo."""

    def make(cfg: RepoConfig, paths: DataPaths) -> RepoLock:
        return lock_from_env(cfg.slug, Path(paths.root) / "factory.lock", env=env, on_lost=_lease_lost_ignored)

    return make


def split_target(target: str) -> list:
    """Origin-form path segments, each percent-decoded exactly once; anything odd is not found."""
    if not target.startswith("/"):
        raise ApiError(404, NOT_FOUND)
    path = target.split("#", 1)[0].split("?", 1)[0]
    segments = path[1:].split("/")
    if any(not segment for segment in segments):
        raise ApiError(404, NOT_FOUND)
    try:
        return [unquote(segment, errors="strict") for segment in segments]
    except ValueError:  # not valid UTF-8 after decoding
        raise ApiError(404, NOT_FOUND) from None


def is_loopback(peer: str) -> bool:
    """Whether a TCP peer address is this host itself (127.0.0.0/8, ::1, or the IPv4-mapped form)."""
    try:
        address = ipaddress.ip_address(peer.partition("%")[0])
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return (mapped or address).is_loopback


# The hops whose ``X-Forwarded-Proto`` is believed: this host and private networks (the ingress pod in the cluster).
_TRUSTED_HOPS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "fc00::/7", "fe80::/10",
    )
)


def forwarded_https(request: Request) -> bool:
    """Whether the client's own connection was HTTPS, as told by the proxy in front (``X-Forwarded-Proto: https``).

    The header is believed only from a peer on this host or a private network (Caddy, in the cluster) and only with
    the exact value ``https``. A peer anywhere else, or any other value, counts as plain HTTP. Nothing but the
    ``Strict-Transport-Security`` header depends on it, and a browser ignores that header on a plain connection, so
    a forged header can do no more than make a response carry it.
    """
    if (request.headers.get("X-Forwarded-Proto") or "").strip().lower() != "https":
        return False
    try:
        address = ipaddress.ip_address(request.peer.partition("%")[0])
    except ValueError:
        return False
    address = getattr(address, "ipv4_mapped", None) or address
    return any(address.version == network.version and address in network for network in _TRUSTED_HOPS)


def bearer_token(header: str | None) -> str | None:
    """The token of ``Authorization: Bearer <token>`` (one space, no other whitespace), else None."""
    if not header:
        return None
    scheme, separator, token = header.partition(" ")
    if scheme.lower() != "bearer" or not separator or not token or re.search(r"\s", token):
        return None
    return token


def match_route(segments: list) -> tuple:
    """("healthz"|"repos"|<action>, method) for a path shape, or a 404 (the static page is matched earlier: ``Manager._route``)."""
    if segments == ["healthz"]:
        return "healthz", "GET"
    if segments == ["repos"]:
        return "repos", "GET"
    if segments == ["trigger-pause"]:
        return "trigger-pause", "POST"
    if segments == ["spend-breaker"]:
        return "spend-breaker", "POST"
    if segments == ["dashboard"]:
        return "dashboard", "GET"
    if segments == ["history", "runs"]:
        return "history-runs", "GET"
    if segments == ["history", "jobs"]:
        return "history-jobs", "GET"
    if len(segments) == 7 and segments[0] == "repos" and segments[3] == "runs" and segments[5] == "jobs":
        return "job-detail", "GET"
    if len(segments) in (6, 7) and segments[0] == "repos" and segments[3] == "runs" and segments[5] == "logs":
        return ("run-logs" if len(segments) == 6 else "run-log"), "GET"
    if len(segments) in (8, 9) and segments[0] == "repos" and segments[3] == "runs" and segments[5] == "jobs" and segments[7] == "logs":
        return ("job-logs" if len(segments) == 8 else "job-log"), "GET"
    if len(segments) == 6 and segments[0] == "repos" and segments[3] == "runs" and segments[5] in ACTIONS:
        return segments[5], ACTIONS[segments[5]].method
    raise ApiError(404, NOT_FOUND)


class Manager:
    """Owns the child registry and turns requests into CLI invocations.

    ``children`` is the registry of started processes; ``runner`` and ``spawner``
    are the only two ways a CLI process is ever started. The operations loop
    (``operations.py``: restart recovery, cost guardrail, auto-purge, label triggers) is a
    second caller of the same ``run_short``, ``lease_state``, ``start_plan``, ``start_run``
    and ``retry_job`` that the HTTP handlers use, so it can never act in a way an operator
    could not.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        env: Mapping[str, str] | None = None,
        runner: Runner | None = None,
        spawner: Callable[[list, Path], subprocess.Popen] | None = None,
        lock_factory: Callable[[RepoConfig, DataPaths], RepoLock] | None = None,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
        verifier: EntraJwtVerifier | None = None,
    ):
        self.settings = settings
        # The metrics token is for this process only: no child (supervisor, CLI command) inherits it.
        self.env = {key: value for key, value in dict(os.environ if env is None else env).items() if key != METRICS_TOKEN_ENV}
        # The pod backend (docs/pod-backend.md, slice 12a), for every supervisor this process starts: a fresh instance id
        # drawn at EVERY start (an older value in the inherited environment is replaced, so a restart is never mistaken for
        # the process before it) and the manager-wide switch in its canonical form (only the exact word "on" is on).
        self.instance_id = podcontract.new_instance_id()
        self.env[podcontract.MANAGER_INSTANCE_ENV] = self.instance_id
        self.env[podcontract.ENV_MANAGER["pod_backend"]] = podexecution.switch_value(self.env)
        # The orphan sweeper and census of the pod backend (``podsweep.Sweeper``, slice 20): None unless the manager switch
        # is on and the backend can be built (``podexecution.sweeper``, which imports nothing while the switch is off);
        # while it is None nothing sweeps and ``GET /repos`` has no ``worker_pods`` key.
        self.pods = podexecution.sweeper(self.env)
        self.runner = runner or SubprocessRunner(self.env, settings.data_dir)
        self.spawner = spawner or SubprocessSpawner(self.env, settings.data_dir)
        self.lock_factory = lock_factory or env_lock_factory(self.env)
        self.sink = Sink(stdout, stderr)
        # Numbers for GET /metrics (metrics.py): the repository label may only be a configured repository.
        self.metrics = metrics.Registry(repos=self.configured_repos)
        self.verifier = verifier
        if verifier is None and settings.auth == AUTH_JWT:
            self.verifier = EntraJwtVerifier.from_config(
                settings.entra_tenant_id,
                settings.entra_audiences,
                report=self._key_refresh_failed,
            )
        self.children = Children()
        self.closing = threading.Event()
        self.breaker = stopworld.Breaker(settings.data_dir)
        # The history ledger (history.py): every writer and reader of it, this process included, goes
        # through ``Ledger``; the directory is only created by the first entry.
        self.ledger = history.Ledger(
            settings.data_dir, history.Limits(settings.history_max_bytes, settings.history_retention_days)
        )
        from .dashboard import Dashboard  # imports operations, which imports this module

        self.dashboard = Dashboard(self)
        self.webui = webui.Shell()
        from .metrics_collect import Collector  # imports operations, which imports this module

        self.collector = Collector(self)
        self.operations: Operations | None = None  # set by start_operations()
        self.github_health = GithubHealth(self)  # its thread is started by serve(), like the operations
        # The false-incomplete rate per repository: refreshed by the janitor sweep, read by GET /repos.
        self.false_incomplete = FalseIncompleteCache()
        self._start_mutex = threading.Lock()
        self._log_slots = threading.BoundedSemaphore(MAX_LOG_READERS)
        self._lanes = {
            "status": threading.BoundedSemaphore(settings.max_status_commands),
            "other": threading.BoundedSemaphore(settings.max_other_commands),
        }
        self._handlers: dict[str, Callable[..., Reply]] = {
            "plan": self._start_plan,
            "run": self._start_run,
            "status": self._status,
            "pause": self._pause,
            "purge": self._purge,
            "stop": self._stop,
        }

    # -- request pipeline -----------------------------------------------------------

    def handle(self, request: Request) -> Reply:
        """Authenticate, route, validate and execute one request; always returns a Reply."""
        trace = Trace()
        started = time.monotonic()
        path = request.target.split("?", 1)[0]
        try:
            reply = self._dispatch(request, trace)
        except ApiError as error:
            reply = error.reply()
        except Exception as error:  # last resort: the type name only, never the message
            self.sink.warn(f"internal error ({type(error).__name__})")
            reply = Reply(500, {"error": "internal error"})
        if forwarded_https(request):
            reply.headers.setdefault("Strict-Transport-Security", webui.HSTS)
        if not (path == "/healthz" and reply.status == 200):  # probes would flood the audit trail
            self.sink.line(audit_line(trace, request.method, path, reply.status))
        self._count_request(request, reply, time.monotonic() - started)
        return reply

    def _count_request(self, request: Request, reply: Reply, seconds: float) -> None:
        """Route, method, status class and duration of one request, as closed words and numbers (never the path)."""
        with contextlib.suppress(Exception):  # numbers must never cost a request
            route = self._route(request.target)
            name = "unknown" if isinstance(route, ApiError) else route[1]
            method = request.method if request.method in ("GET", "POST") else metrics.OTHER
            self.metrics["factory_manager_requests_total"].inc(
                route=name, method=method, status_class=f"{reply.status // 100}xx"
            )
            self.metrics["factory_manager_request_seconds"].observe(seconds, route=name)
            if reply.status in (401, 403):
                self.metrics["factory_manager_auth_failures_total"].inc(http_status=str(reply.status))

    def _key_refresh_failed(self, kind: str) -> None:
        self.metrics["factory_manager_jwks_refresh_failures_total"].inc()
        self.sink.warn(f"signing key refresh failed ({kind})")

    def scrape_metrics(self) -> str:
        """The text of ``GET /metrics``: collected from memory, the ledger and the run directories; no process, no GitHub."""
        self.collector.collect()
        return self.metrics.render()

    def _dispatch(self, request: Request, trace: Trace) -> Reply:
        route = self._route(request.target)
        if not isinstance(route, ApiError) and route[1] == "ui" and request.method == "GET":
            return self._ui(request.target)  # the static shell and its config: no data, no secret, so no token (webui.py)
        if not isinstance(route, ApiError) and route[1] == "healthz":
            if self.settings.auth == AUTH_JWT and not is_loopback(request.peer):
                self._authenticate(request, trace)  # only the pod itself may probe without a token
            if request.method != route[2]:
                raise ApiError(405, "method not allowed", {"Allow": route[2]})
            return self._health()
        # Everything else is authenticated first, so an anonymous caller learns nothing about paths.
        principal = self._authenticate(request, trace)
        if not principal.can_read():
            raise ApiError(403, "forbidden")
        if isinstance(route, ApiError):
            raise route
        segments, name, method = route
        if request.method != method:
            raise ApiError(405, "method not allowed", {"Allow": method})
        if method == "POST" and not principal.can_mutate():
            raise ApiError(403, "forbidden")
        if name == "repos":
            listing = {"trigger_paused": self.trigger_paused(), "breaker": self.breaker.state(), "repos": self._repos_listing()}
            pods = self.worker_pods()
            if pods is not None:  # only when the pod backend is wired in: nothing changes for the in-process mode
                listing["worker_pods"] = pods
            return Reply(200, listing)
        if name == "trigger-pause":
            return self._trigger_pause(request, trace)
        if name == "spend-breaker":
            return self._spend_breaker(request, trace)
        if name in READS:
            return self._read(name, segments, request)
        if name in LOG_READS:
            if not principal.can_read_logs():
                raise ApiError(403, "forbidden")
            return self._logs(name, segments, request, trace)
        target = self._resolve(segments)
        return self._run_action(name, target, request, trace)

    def sign_in_config(self) -> dict:
        """What ``GET /ui/config.json`` says: how the browser page signs in, from this manager's own settings."""
        settings = self.settings
        return webui.sign_in_config(
            settings.auth == AUTH_JWT,
            settings.entra_tenant_id,
            settings.entra_audiences,
            settings.entra_spa_client_id,
            settings.entra_spa_scope,
        )

    def _ui(self, target: str) -> Reply:
        """The page, its three assets and ``/ui/config.json``, from the allow-list of ``webui`` only; ``/ui`` redirects to ``/ui/``."""
        path, query = webui.split_target(target)
        config = self.sign_in_config()
        login_origin = webui.LOGIN_ORIGIN if config["mode"] == webui.MODE_ENTRA else None
        if path == webui.PAGE_REDIRECT:
            return Reply(307, {}, webui.redirect_headers(login_origin), b"")
        if path == webui.CONFIG:
            return Reply(200, {}, webui.config_headers(config), webui.config_body(config))
        asset = self.webui.asset(path)
        if asset is None:  # a file missing from the installation: the same answer as an unknown path
            raise ApiError(404, NOT_FOUND)
        return Reply(200, {}, webui.headers_for(asset, query, login_origin), asset.body)

    @staticmethod
    def _route(target: str) -> tuple | ApiError:
        """(segments, name, method) for a known path shape, or the 404 to raise once authenticated."""
        path = webui.split_target(target)[0]
        if webui.addresses(path):  # exact allow-listed paths only: any other path under /ui/ is an ordinary unknown path
            return path[1:].split("/"), "ui", "GET"
        try:
            segments = split_target(target)
            name, method = match_route(segments)
        except ApiError as error:
            return error
        return segments, name, method

    def _authenticate(self, request: Request, trace: Trace) -> Principal:
        if self.settings.auth == AUTH_JWT:
            principal = self._authenticate_jwt(request)
        else:
            try:
                principal = parse_principal(request.headers.get(PRINCIPAL_HEADER))
            except PrincipalError:
                raise ApiError(401, "authentication required") from None
        trace.principal = principal.ident
        return principal

    def _authenticate_jwt(self, request: Request) -> Principal:
        """A verified bearer token, or 401 (anything wrong with the token) / 503 (keys unobtainable).

        X-MS-CLIENT-PRINCIPAL is not even read in this mode: nothing in front of the manager is trusted.
        """
        token = bearer_token(request.headers.get(AUTHORIZATION_HEADER))
        try:
            if token is None or self.verifier is None:
                raise JwtError("missing")
            identity = self.verifier.verify(token)
        except JwtError:
            raise ApiError(401, "authentication required", {"WWW-Authenticate": "Bearer"}) from None
        except KeysUnavailable:
            raise ApiError(503, "authentication unavailable", {"Retry-After": str(AUTH_RETRY_SECONDS)}) from None
        return Principal(identity.ident[:ID_LOG_CHARS], identity.roles)

    def _health(self) -> Reply:
        self.housekeeping()
        return Reply(200, {"status": "ok"})

    def housekeeping(self) -> None:
        """Upkeep on every health probe: reap finished children. The long loops live in operations.py."""
        self.children.reap()

    def _resolve(self, segments: list) -> Target:
        """The configured repository and run directory, or 404 (unknown repo) / 400 (bad run name)."""
        try:
            repo = check_repo(f"{segments[1]}/{segments[2]}")
        except ValueError:  # same answer as an unknown repo, so the response cannot be used to probe
            raise ApiError(404, NOT_FOUND) from None
        try:
            cfg = load_repo_config(repo, config_dir=self.settings.config_dir)
        except RepoNotConfigured:
            raise ApiError(404, NOT_FOUND) from None
        except ValueError:
            self.sink.warn("repository configuration is invalid")
            raise ApiError(500, "repository configuration is invalid") from None
        run = segments[4]
        if not RUN_SLUG.fullmatch(run):
            raise ApiError(400, "invalid run name")
        paths = data_paths(cfg, data_dir=self.settings.data_dir)
        return Target(repo, run, cfg, paths, paths.run_dir(run))

    def _read(self, name: str, segments: list, request: Request) -> Reply:
        """A dashboard endpoint: read-only, in this process, never a CLI or a GitHub call (dashboard.py)."""
        from .dashboard import QueryError, parse_query

        try:
            if name == "dashboard":
                parse_query(request.target, {})  # takes no parameters
                return Reply(200, self.dashboard.overview())
            if name == "history-runs":
                return Reply(200, self.dashboard.history_runs(request.target))
            if name == "history-jobs":
                return Reply(200, self.dashboard.history_jobs(request.target))
            target = self._resolve(segments)
            if not JOB_ID.fullmatch(segments[6]):
                raise ApiError(400, "invalid job name")
            parse_query(request.target, {})
            detail = self.dashboard.job_detail(target.repo, target.run, segments[6], target.paths)
        except QueryError as error:
            raise ApiError(400, "invalid query parameter", parameter=error.parameter) from None
        if detail is None:
            raise ApiError(404, NOT_FOUND)
        return Reply(200, detail)

    def _logs(self, name: str, segments: list, request: Request, trace: Trace) -> Reply:
        """A log endpoint: the allow-listed names of a run or a job, or one file in a bounded, redacted range (logs.py).

        Read in this process, never through a CLI. The run and job names are matched before any path is built;
        a name outside the allow-list, a missing file and a link are all the same 404.
        """
        from . import logs
        from .dashboard import QueryError, parse_query

        target = self._resolve(segments)
        job = None
        if name.startswith("job-"):
            job = segments[6]
            if not JOB_ID.fullmatch(job):
                raise ApiError(400, "invalid job name")
        shape = logs.JOB_FILE if job else logs.RUN_FILES
        root = target.paths.root
        base, parts = root.parents[2], (*root.parts[-3:], "runs", target.run)
        if not self._log_slots.acquire(timeout=SLOT_WAIT_SECONDS):
            raise ApiError(503, "busy", {"Retry-After": "2"})
        try:
            if name in ("run-logs", "job-logs"):
                parse_query(request.target, {})
                return Reply(200, self._log_listing(target, job, base, parts, shape))
            params = logs.parse_view(parse_query, request.target)
            descriptor = logs.open_log(base, (*parts, job) if job else parts, segments[-1], shape)
            try:
                window = logs.view(descriptor, params, jsonl=logs.is_jsonl(segments[-1]))
            finally:
                os.close(descriptor)
        except QueryError as error:
            raise ApiError(400, "invalid query parameter", parameter=error.parameter) from None
        except logs.LogNotFound:
            raise ApiError(404, NOT_FOUND) from None
        finally:
            self._log_slots.release()
        body = window.body()
        trace.bytes = len(body)
        return Reply(200, {}, logs.headers_for(window), body)

    @staticmethod
    def _log_listing(target: Target, job: str | None, base: Path, parts: tuple, shape) -> dict:
        from . import logs

        descriptor = logs.open_directory(base, parts)
        try:
            if job is not None:
                try:
                    inner = logs.enter_directory(descriptor, job)
                except logs.LogNotFound:
                    return {"repo": target.repo, "run": target.run, "job": job, "files": []}  # a job with no logs yet
                os.close(descriptor)
                descriptor = inner
            files = logs.list_files(descriptor, shape)
        finally:
            os.close(descriptor)
        listing = {"repo": target.repo, "run": target.run, "files": files}
        return {**listing, "job": job} if job else listing

    def _run_action(self, name: str, target: Target, request: Request, trace: Trace) -> Reply:
        """Execute one action; a successful ``POST`` is also written to the history ledger with the principal."""
        reply = self._execute(name, target, request, trace)
        if ACTIONS[name].method == "POST" and reply.status < 400:
            history.record_operator(self.ledger, target.repo, target.run, name, trace.principal, trace.job, reply.status)
        return reply

    def _execute(self, name: str, target: Target, request: Request, trace: Trace) -> Reply:
        action = ACTIONS[name]
        if action.inbox:
            document, raw = self._read_inbox(request, action)
            return self._inbox_command(name, target, document, raw, trace)
        body = {}
        if action.method == "POST":
            if action.same_origin_json:
                require_same_origin_json(request)
            body = validate_fields(request.read_body(self.settings.max_body), action)
            trace.job = body.get("job")
        handler = self._handlers.get(name)
        if handler is not None:
            return handler(target, body, trace)
        return self._command(target, command_tail(name, body), trace)

    def _trigger_pause(self, request: Request, trace: Trace) -> Reply:
        """``POST /trigger-pause`` ``{"paused": true|false}``: set or clear the marker; idempotent."""
        body = validate_fields(request.read_body(self.settings.max_body), TRIGGER_PAUSE_ACTION)
        paused = body["paused"]
        try:
            self.set_trigger_pause(paused)
        except OSError as error:
            self.sink.warn(f"cannot change the trigger pause ({type(error).__name__})")
            raise ApiError(500, "could not change the trigger pause") from None
        trace.argv = ["trigger-pause", "on" if paused else "off"]
        return Reply(200, {"trigger_paused": paused})

    def _spend_breaker(self, request: Request, trace: Trace) -> Reply:
        """``POST /spend-breaker`` ``{"clear": true}``: clear the stop-the-world breaker; idempotent.

        Only an Operator reaches this (the dispatcher refuses every POST from anyone else). It resumes nothing: the
        trigger starts new work again, and a run the breaker drained is started by a person.
        """
        body = validate_fields(request.read_body(self.settings.max_body), SPEND_BREAKER_ACTION)
        if body["clear"] is not True:
            raise ApiError(400, "invalid body", detail="clear must be true")
        try:
            self.breaker.clear()
        except OSError as error:
            self.sink.warn(f"cannot clear the breaker ({type(error).__name__})")
            raise ApiError(500, "could not clear the breaker") from None
        trace.argv = ["spend-breaker", "clear"]
        return Reply(200, {"breaker": None, "trigger_paused": self.trigger_paused()})

    def worker_pods(self) -> dict | None:
        """The pod census for the deploy gate (``{"live", "final", "unknown"}``), or None without a pod sweeper.

        The Infra apply waits until ``live`` is 0 and ``unknown`` is false (``tools/infra_deploy_gate.py``): a pod
        that is running, or that cannot be read, is a session a rollout would cut off.
        """
        pods = self.pods
        if pods is None:
            return None
        try:
            census = pods.census()
        except Exception:  # noqa: BLE001 - whatever it raised, the pods cannot be read: busy, never idle
            return {"live": 0, "final": 0, "unknown": True}
        return census if isinstance(census, dict) else {"live": 0, "final": 0, "unknown": True}

    def trigger_paused(self) -> bool:
        """Whether the trigger is paused: the manual marker or the breaker's. No plan or run is started by the manager itself."""
        return self.trigger_pause_set() or self.breaker.tripped()

    def trigger_pause_set(self) -> bool:
        """Whether the MANUAL pause marker exists (what ``POST /trigger-pause`` sets and clears, and its reply reports)."""
        return os.path.lexists(self.settings.data_dir / TRIGGER_PAUSE_FILE)

    def set_trigger_pause(self, paused: bool) -> None:
        """Create (atomically, fsynced) or remove the manual pause marker. Runs in progress are not touched."""
        path = self.settings.data_dir / TRIGGER_PAUSE_FILE
        if not paused:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            return
        if self.trigger_pause_set():
            return
        temp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        try:
            with os.fdopen(descriptor, "w") as handle:
                handle.write(timestamp())
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp)
            raise

    def _read_inbox(self, request: Request, action: Action) -> tuple:
        raw = request.read_body(self.settings.max_inbox)
        return validate_inbox(raw, action), raw

    # -- short commands -------------------------------------------------------------

    def _base_argv(self, target: Target) -> list:
        return [*self.settings.cli, "--repo", target.repo, "--run-dir", str(target.run_dir)]

    def run_short(self, argv: list, lane: str = "other") -> CommandResult:
        """Run one short CLI command in a slot of ``lane``: the only place a slot is taken.

        Shared by the HTTP handlers and the operations loop, so background polling is
        capped by the same semaphores and can never starve an operator action.
        Raises ``SlotsBusy``, ``RunnerClosed`` (shutting down) or ``OSError`` (could not start).
        """
        slots = self._lanes[lane]
        if not slots.acquire(timeout=SLOT_WAIT_SECONDS):
            raise SlotsBusy()
        try:
            return self.runner(argv, self.settings.command_timeout)
        finally:
            slots.release()

    def _command(
        self, target: Target, tail: list, trace: Trace, parse_status: bool = False, lane: str = "other"
    ) -> Reply:
        argv = [*self._base_argv(target), *tail]
        trace.argv = argv
        try:
            result = self.run_short(argv, lane)
        except SlotsBusy:
            raise ApiError(503, "too many commands in progress; retry shortly", {"Retry-After": "5"}) from None
        except RunnerClosed:
            raise ApiError(503, "manager is shutting down") from None
        except OSError as error:
            self.sink.warn(f"cannot start the factory CLI ({type(error).__name__})")
            raise ApiError(500, "could not start the factory CLI") from None
        if result.timed_out:
            raise ApiError(504, "command timed out", timeout_seconds=self.settings.command_timeout)
        out, out_cut = _clip(result.stdout, MAX_OUTPUT_CHARS)
        err, err_cut = _clip(result.stderr, MAX_ERROR_CHARS)
        payload: dict = {"returncode": result.returncode, "output": out, "error": err}
        if out_cut or err_cut:
            payload["truncated"] = True
        if parse_status and not out_cut:
            with contextlib.suppress(ValueError):  # plain text, e.g. "Planned N issues": no parsed form
                payload["status"] = json.loads(out)
        return Reply(200 if result.returncode == 0 else 422, payload)

    def _status(self, target: Target, body: dict, trace: Trace) -> Reply:
        reply = self._command(target, command_tail("status", body), trace, parse_status=True, lane="status")
        reply.payload["manager"] = self._manager_view(target.repo, target.run)
        return reply

    def _manager_view(self, repo: str, run: str) -> dict:
        child = self.children.get(repo, run)
        exits = self.children.last_exit(repo, run, limit=1)
        return {"child": child.describe() if child else None, "last_exit": exits[-1] if exits else None}

    def _stop(self, target: Target, body: dict, trace: Trace) -> Reply:
        """``POST .../stop`` ``{"job", "reason"?}``: ask the supervisor to stop one running job (drain semantics).

        404 for a job the run does not have, 409 (state untouched) for a job that is not running or a run with
        no live supervisor. Otherwise the CLI records the request and the supervisor carries it out; asking
        again while it is pending gets the first answer.
        """
        found = self.dashboard.stop_check(target.repo, target.run, body["job"], target.paths)
        if found is None:
            raise ApiError(404, NOT_FOUND)
        status, supervisor = found
        if status != "running":
            raise ApiError(409, "job is not running", job_status=status)
        if supervisor not in ("running", "draining"):
            raise ApiError(409, "no supervisor is running this run", supervisor=supervisor)
        reply = self._command(target, command_tail("stop", {**body, "principal": trace.principal}), trace)
        if reply.status == 200:
            with contextlib.suppress(ValueError):  # the CLI answers with one JSON object (--json)
                reply.payload["stop"] = json.loads(reply.payload["output"])
        return reply

    def _purge(self, target: Target, body: dict, trace: Trace) -> Reply:
        reply = self._command(target, command_tail("purge", body), trace)
        if reply.status == 200:
            self.children.forget(target.repo, target.run)
        return reply

    def _inbox_command(self, name: str, target: Target, document: object, raw: bytes, trace: Trace) -> Reply:
        tail = [name]
        if name == "import-evidence":
            try:
                tail.append(_job_id(document.get("job_id")))
            except ValueError:
                raise ApiError(400, "invalid body", detail="job_id must match [A-Za-z0-9][A-Za-z0-9_-]{0,63}") from None
        if not target.run_dir.is_dir():
            raise ApiError(404, "run not found")
        try:
            path = write_inbox(target.run_dir, raw)
        except OSError as error:
            self.sink.warn(f"cannot write the inbox file ({type(error).__name__})")
            raise ApiError(500, "could not store the request body") from None
        return self._command(target, [*tail, "--file", str(path)], trace)

    # -- long-lived children --------------------------------------------------------

    def _start_plan(self, target: Target, body: dict, trace: Trace) -> Reply:
        if "issues" in body and "limit" in body:
            raise ApiError(400, "invalid body", detail="issues and limit cannot be combined")
        if (target.run_dir / "plan.json").exists() or (target.run_dir / "state.json").exists():
            raise ApiError(409, "run already planned")
        return self._start("plan", target, command_tail("plan", body), trace)

    def _start_run(self, target: Target, body: dict, trace: Trace) -> Reply:
        if not (target.run_dir / "plan.json").is_file():
            raise ApiError(404, "run has no plan")
        return self._start("run", target, command_tail("run", body), trace)

    def start_run(self, target: Target, body: Mapping[str, object] | None = None) -> Reply:
        """``POST .../run`` for in-process callers (automatic resume, label triggers): same checks, same child registry.

        Refused with 503 while the trigger is paused (an HTTP request from an Operator is not).
        """
        if self.trigger_paused():
            raise ApiError(503, TRIGGER_PAUSED)
        return self._start_run(target, dict(body or {}), Trace())

    def start_plan(self, target: Target, body: Mapping[str, object] | None = None) -> Reply:
        """``POST .../plan`` for in-process callers (label triggers): validated like a request body, same checks and registry.

        Refused with 503 while the trigger is paused.
        """
        if self.trigger_paused():
            raise ApiError(503, TRIGGER_PAUSED)
        fields = validate_fields(json.dumps(dict(body or {})).encode(), ACTIONS["plan"])
        return self._start_plan(target, fields, Trace())

    def start_append(self, target: Target, issues: list, timeout: int | None = None) -> Reply:
        """``plan --issues N,... --append-to <run>`` as a child, for in-process callers (the label trigger's top-up).

        The repository lease is *not* probed: the running supervisor of this run holds it, and the append
        takes none (it is serialised per run by its own lock file). So it is refused with 409 unless this
        manager has a supervisor child for the repository, and with 409 while an append is already running
        for the repository (one at a time). It has its own registry slot and log (``append.log``).
        ``timeout`` is the planner session's limit in seconds (the repository's ``auto_build_timeout_seconds``).
        Refused with 503 while the trigger is paused or the manager is shutting down.
        """
        if self.trigger_paused():
            raise ApiError(503, TRIGGER_PAUSED)
        body = {"issues": list(issues), **({} if timeout is None else {"timeout": timeout})}
        fields = validate_fields(json.dumps(body).encode(), ACTIONS["plan"])
        tail = [*command_tail("plan", fields), "--append-to", target.run]
        return self._start("append", target, tail, Trace())

    def retry_job(self, target: Target, job: str, reason: str) -> Reply:
        """``POST .../retry`` for in-process callers (label triggers): the same short command in the same lane."""
        fields = validate_fields(json.dumps({"job": job, "reason": reason}).encode(), ACTIONS["retry"])
        return self._command(target, command_tail("retry", fields), Trace())

    def retry_followup(self, target: Target, job: str) -> Reply:
        """The retry of a follow-up session, for the operations loop only: ``retry <job> --followup`` in the same lane.

        It is not an HTTP action (``ACTIONS`` has no entry for it, so the HTTP surface does not grow): the CLI checks
        that the job is a published one, that its pull request is open on the base branch with exactly the retained
        head, and that the context file the loop wrote exists. The caller starts the ``run`` (``start_run``) itself,
        so that a busy lease or a restart in between leaves the queued job for the next pass.
        """
        fields = validate_fields(json.dumps({"job": job, "reason": FOLLOWUP_REASON}).encode(), ACTIONS["retry"])
        return self._command(target, [*command_tail("retry", fields), "--followup"], Trace())

    def target_for(self, repo: str, run: str) -> Target:
        """The validated ``Target`` of a configured repository and a run name (ValueError when either is not valid)."""
        cfg = load_repo_config(repo, config_dir=self.settings.config_dir)
        paths = data_paths(cfg, data_dir=self.settings.data_dir)
        return Target(repo, run, cfg, paths, paths.run_dir(run))

    def _start(self, kind: str, target: Target, tail: list, trace: Trace) -> Reply:
        """Probe the lease (not for an append), then spawn ``factory ... plan|run`` detached; 202 with its pid."""
        argv = [*self._base_argv(target), *tail]
        with self._start_mutex:
            if self.closing.is_set():
                raise ApiError(503, "manager is shutting down")
            active = self.children.active_for_repo(target.repo)
            if active is not None and kind != "append":
                raise ApiError(409, "a factory process is already running for this repository", kind=active.kind, run=active.run)
            if kind == "append":  # the supervisor holds the lease; a second append is never allowed
                busy = self.children.active_for_repo(target.repo, "append")
                supervisor = self.children.get(target.repo, target.run)
                if busy is not None:
                    raise ApiError(409, "an append is already running for this repository", kind=busy.kind, run=busy.run)
                if supervisor is None or supervisor.kind != "run":
                    raise ApiError(409, "no supervisor of this manager is running for this run")
            else:
                self._probe_lease(target)
            try:
                target.run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                process = self.spawner(argv, target.run_dir / LOG_NAMES[kind])
            except OSError as error:
                self.sink.warn(f"cannot start the {kind} process ({type(error).__name__})")
                raise ApiError(500, f"could not start the {kind} process") from None
            child = Child(kind, target.repo, target.run, process, timestamp())
            self.children.register(child)
        trace.argv = argv
        return Reply(202, {"pid": child.pid, "started": child.started, "kind": kind})

    def _probe_lease(self, target: Target) -> None:
        """Acquire and immediately release the repo lease: busy -> 409, backend down -> 503.

        The supervisor takes the lease itself for its whole life; holding it here
        would block that child. The probe only proves it is free right now.
        """
        state = self.lease_state(target.cfg, target.paths)
        if state == "busy":
            raise ApiError(409, LEASE_BUSY)
        if state == "unavailable":
            raise ApiError(503, "lease backend unavailable")

    def lease_state(self, cfg: RepoConfig, paths: DataPaths, *, warn: bool = True) -> str:
        """``free`` (acquired and released at once), ``busy`` or ``unavailable`` for one repository.

        ``warn=False`` is for callers that poll and report once themselves.
        """
        try:
            os.makedirs(paths.root, mode=0o700, exist_ok=True)
            lock = self.lock_factory(cfg, paths)
            lock.acquire()
        except LockBusy:
            return "busy"
        except (LockError, OSError) as error:
            if warn:
                self.sink.warn(f"lease probe failed ({type(error).__name__})")
            return "unavailable"
        lock.release()
        return "free"

    def _pause(self, target: Target, body: dict, trace: Trace) -> Reply:
        child = self.children.get(target.repo, target.run)
        if child is None:
            raise ApiError(409, "this manager has no running process for this run")
        self._interrupt(child)
        if child.process.wait(self.settings.pause_seconds) is None and child.running():
            raise ApiError(504, "process did not exit in time", pid=child.pid, exited=False)
        reply = self._command(target, command_tail("status", {}), trace, parse_status=True)
        reply.payload["pause"] = {"pid": child.pid, "kind": child.kind, "exited": True}
        return reply

    def _signal(self, child: Child, sig: int) -> bool:
        """Deliver ``sig`` to the child's group; False if it did not arrive."""
        try:
            return bool(child.process.signal_group(sig))
        except OSError as error:  # for example EPERM: report it and carry on with the others
            self.sink.warn(f"cannot signal process group {child.pid} ({type(error).__name__})")
            return False

    def _interrupt(self, child: Child) -> None:
        """SIGINT a child at most once, however pause and shutdown interleave.

        A second SIGINT would land inside the supervisor's own interrupt cleanup
        and release its lease while a worker thread is still tearing down.
        Only a signal that was delivered counts: after EPERM the next caller tries again.
        """
        with child.gate:
            if not child.interrupted and self._signal(child, signal.SIGINT):
                child.interrupted = True

    # -- listing --------------------------------------------------------------------

    def configured_repos(self) -> list:
        """``org/name`` for every ``repos/<org>__<name>.toml`` whose file name is a valid repo."""
        try:
            names = sorted(os.listdir(self.settings.config_dir / "repos"))
        except FileNotFoundError:
            return []
        found = (_repo_from_filename(name) for name in names)
        return [repo for repo in found if repo is not None]

    def _repos_listing(self) -> list:
        listing = []
        for repo in self.configured_repos():
            try:
                cfg = load_repo_config(repo, config_dir=self.settings.config_dir)
            except ValueError:
                listing.append({"repo": repo, "error": "invalid configuration"})
                continue
            paths = data_paths(cfg, data_dir=self.settings.data_dir)
            active = self.children.active_for_repo(repo)
            listing.append(
                {
                    "repo": repo,
                    "base_branch": cfg.base_branch,
                    "github": self.github_health.status(repo),
                    "false_incomplete": self.false_incomplete.get(repo),
                    "supervisor": active.describe() if active else None,
                    "recent": self.children.last_exit(repo),
                    "runs": _list_runs(paths),
                }
            )
        return listing

    # -- operations loop ------------------------------------------------------------

    def start_operations(self) -> None:
        """Start restart recovery, the cost guardrail, auto-purge and (when wanted) the label watcher unless ``FACTORY_MANAGER_OPERATIONS=off``.

        Called by ``serve()`` after the listening line, never by ``__init__``, so building
        a ``Manager`` (as every test does) starts no thread.
        """
        if not self.settings.operations or self.operations is not None or self.closing.is_set():
            return
        from .operations import Operations  # operations.py imports this module

        self.operations = Operations(self)
        self.operations.start()

    # -- shutdown -------------------------------------------------------------------

    def terminate_children(self, grace: float | None = None) -> dict:
        """SIGINT every started process group, wait up to ``grace`` seconds, then SIGKILL the rest.

        Children release their own leases on the way out. In-flight short commands
        get the same SIGINT and the same grace, and are then killed too. New starts
        and new commands are refused. A child that a pause already interrupted is
        not signalled again, but it is waited for and killed after the grace.
        """
        wait_seconds = self.settings.shutdown_seconds if grace is None else grace
        self.closing.set()
        self.runner.close()
        with self._start_mutex:  # wait for a start that is half done
            children = self.children.running()
        for child in children:
            self._interrupt(child)
        self.runner.interrupt_all()
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline and (
            any(child.running() for child in children) or self.runner.live_count() > 0
        ):
            time.sleep(0.05)
        stragglers = [child for child in children if child.running()]
        for child in stragglers:
            self._signal(child, signal.SIGKILL)
            child.process.wait(5)
        killed_commands = self.runner.kill_all()
        self.children.reap()
        if stragglers or killed_commands:
            self.sink.warn(
                f"{len(stragglers)} process group(s) and {killed_commands} command(s) were killed at shutdown"
            )
        if self.operations is not None:
            # `closing` woke every wait and the runner is closed, so the threads end at once.
            stuck = self.operations.join(OPERATIONS_JOIN_SECONDS)
            if stuck:
                self.sink.warn(f"{stuck} operations thread(s) did not stop in time")
        return {"interrupted": len(children), "killed": len(stragglers), "killed_commands": killed_commands}


def _repo_from_filename(name: str) -> str | None:
    """``org/name`` for ``<org>__<name>.toml``; None for any file that names no valid repository."""
    stem, dot, suffix = name.rpartition(".")
    owner, separator, repo_name = stem.partition("__")
    if not (dot and suffix == "toml" and separator):
        return None
    try:
        return check_repo(f"{owner}/{repo_name}")
    except ValueError:
        return None  # such a name can never be addressed, so it is not allowlisted


def _clip(data: bytes, limit: int) -> tuple:
    text = data.decode("utf-8", errors="replace")
    return (text[:limit], True) if len(text) > limit else (text, False)


def _list_runs(paths: DataPaths) -> list:
    return _scan_runs(paths)[0]


def _scan_runs(paths: DataPaths, newest_first: bool = False) -> tuple:
    """``(runs, truncated)``: at most ``MAX_LISTED_RUNS`` run directories, by name (the oldest first unless ``newest_first``), and whether more exist.

    Run names are ``auto-<UTC time>`` and the like, so the names that are cut off are the newest by default: a caller whose answer
    depends on the newest run, or on there being no run at all, asks for ``newest_first`` and must not treat a cut listing as complete.
    """
    try:
        names = sorted(os.listdir(paths.runs), reverse=newest_first)
    except FileNotFoundError:
        return [], False
    runs = []
    for name in names[:MAX_LISTED_RUNS]:
        directory = paths.runs / name
        if RUN_SLUG.fullmatch(name) and directory.is_dir() and not directory.is_symlink():
            runs.append(
                {
                    "run": name,
                    "planned": (directory / "plan.json").is_file(),
                    "started": (directory / "state.json").is_file(),
                }
            )
    return runs, len(names) > MAX_LISTED_RUNS


# --- HTTP --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    """Adapts http.server to ``Manager.handle``; subclassed per manager by ``make_server``."""

    manager: Manager
    protocol_version = "HTTP/1.0"  # one request per connection: no thread is held by an idle client
    _consumed = 0

    def version_string(self) -> str:
        return "factory-manager"

    _header_timer: threading.Timer | None = None

    def setup(self) -> None:
        super().setup()
        # The request line and headers are parsed by http.server with only the per-recv
        # timeout; this timer bounds the whole phase and wakes a blocked read by shutting the socket.
        timer = threading.Timer(HEADER_READ_SECONDS, self._header_deadline)
        timer.daemon = True
        self._header_timer = timer
        timer.start()

    def _header_deadline(self) -> None:
        with contextlib.suppress(OSError):
            self.connection.shutdown(socket.SHUT_RDWR)

    def _stop_header_timer(self) -> None:
        timer, self._header_timer = self._header_timer, None
        if timer is not None:
            timer.cancel()

    def parse_request(self) -> bool:
        try:
            return super().parse_request()
        finally:
            self._stop_header_timer()

    def finish(self) -> None:
        self._stop_header_timer()
        super().finish()

    def log_message(self, fmt: str, *args: object) -> None:
        """Silenced: the audit line is the only per-request output."""

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self._send(Reply(code, {"error": message or "bad request"}))

    def _serve(self) -> None:
        self._consumed = 0
        request = Request(self.command, self.path, self.headers, self._read_body, self.client_address[0])
        self._send(self.manager.handle(request))
        self._discard_unread()

    def _discard_unread(self) -> None:
        """Swallow a request body the pipeline never read (a 401, 413, ...).

        Closing a socket that still holds unread data makes the kernel send a reset,
        and the client can lose the response it was about to read.
        """
        headers = getattr(self, "headers", None)
        text = headers.get("Content-Length") if headers else None
        if not text or not (text.isascii() and text.isdigit()) or len(text) > 12:
            return
        remaining = min(int(text) - self._consumed, DRAIN_LIMIT)
        deadline = time.monotonic() + DRAIN_SECONDS
        with contextlib.suppress(OSError):  # a client that stalls or leaves is simply dropped
            self.connection.settimeout(DRAIN_SECONDS)
            while remaining > 0 and time.monotonic() < deadline:
                chunk = self.rfile.read1(min(remaining, 65_536))
                if not chunk:
                    break
                remaining -= len(chunk)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _serve

    def _read_body(self, limit: int) -> bytes:
        if self.headers.get("Transfer-Encoding"):
            raise ApiError(411, "Content-Length required; chunked bodies are not supported")
        text = self.headers.get("Content-Length")
        if text is None:
            return b""
        if not (text.isascii() and text.isdigit()) or len(text) > 12:
            raise ApiError(400, "invalid Content-Length")
        length = int(text)
        if length > limit:
            raise ApiError(413, "request body too large", limit=limit)
        return self._read_exactly(length)

    def _read_exactly(self, length: int) -> bytes:
        """Read the body in one ``recv`` per step, each bounded by the time left.

        ``rfile.read(n)`` would loop over ``recv`` until n bytes arrive, each call only
        bounded by the per-recv socket timeout, so a client dripping one byte at a time
        would never hit the total deadline.
        """
        deadline = time.monotonic() + BODY_READ_SECONDS
        chunks = []
        remaining = length
        while remaining:
            left = deadline - time.monotonic()
            if left <= 0:
                raise ApiError(408, "request body too slow")
            try:
                self.connection.settimeout(max(0.05, min(float(self.timeout or left), left)))
                chunk = self.rfile.read1(min(remaining, 65_536))
            except TimeoutError:
                raise ApiError(408, "request body too slow") from None
            except OSError:
                raise ApiError(400, "request body unreadable") from None
            if not chunk:
                raise ApiError(400, "request body shorter than Content-Length")
            chunks.append(chunk)
            remaining -= len(chunk)
            self._consumed += len(chunk)
        return b"".join(chunks)

    def _send(self, reply: Reply) -> None:
        body = reply.body if reply.body is not None else json.dumps(reply.payload, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Connection": "close",
            **reply.headers,
            "Content-Length": str(len(body)),
        }
        try:
            self.send_response(reply.status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except OSError:  # the client went away; the audit line was already written
            self.close_connection = True


class ManagerServer(ThreadingHTTPServer):
    """Threaded server with non-daemon handler threads and a bounded request timeout."""

    daemon_threads = False
    block_on_close = True
    request_queue_size = 64

    manager: Manager

    def __init__(self, address: tuple, handler: type, manager: Manager, ipv6: bool = False):
        self.manager = manager
        if ipv6:
            self.address_family = socket.AF_INET6
        self._connections: set = set()
        self._connections_mutex = threading.Lock()
        super().__init__(address, handler)

    def finish_request(self, request: socket.socket, client_address: tuple) -> None:
        with self._connections_mutex:
            self._connections.add(request)
        try:
            super().finish_request(request, client_address)
        finally:
            with self._connections_mutex:
                self._connections.discard(request)

    def handle_error(self, request: socket.socket, client_address: tuple) -> None:
        # The default prints a traceback to stderr; report only the exception type.
        kind = sys.exc_info()[0]
        self.manager.sink.warn(f"connection error ({kind.__name__ if kind else 'unknown'})")

    def close_connections(self) -> None:
        """Wake handler threads blocked on a slow client so shutdown does not wait for them."""
        with self._connections_mutex:
            sockets = list(self._connections)
        for connection in sockets:
            with contextlib.suppress(OSError):  # already closed by its own thread
                connection.shutdown(socket.SHUT_RDWR)


def make_server(manager: Manager) -> ManagerServer:
    settings = manager.settings
    handler = type("BoundHandler", (Handler,), {"manager": manager, "timeout": settings.request_timeout})
    return ManagerServer((settings.host, settings.port), handler, manager, ipv6=":" in settings.host)


def make_metrics_server(manager: Manager) -> metrics.MetricsServer | None:
    """The metrics listener, or None while ``FACTORY_MANAGER_METRICS_PORT`` is unset (the default)."""
    settings = manager.settings
    if settings.metrics_port is None:
        return None
    return metrics.MetricsServer(
        (settings.metrics_host, settings.metrics_port),
        manager.scrape_metrics,
        ipv6=":" in settings.metrics_host,
        token=settings.metrics_token,
    )


# --- entry point -------------------------------------------------------------------


def startup_line(server: ManagerServer, manager: Manager, metrics_server: metrics.MetricsServer | None = None) -> str:
    host, port = server.server_address[:2]
    line = f"factory-manager listening host={escape_field(host)} port={port} repos={len(manager.configured_repos())}"
    if manager.settings.auth != AUTH_EASY:
        line = f"{line} auth={manager.settings.auth}"
    if metrics_server is not None:
        line = f"{line} metrics_port={metrics_server.server_address[1]}"
        if metrics_server.requires_token:
            line = f"{line} metrics_auth=bearer"
    return line


def serve(manager: Manager, server: ManagerServer, metrics_server: metrics.MetricsServer | None = None) -> int:
    """Serve until SIGTERM/SIGINT, then stop accepting, stop the children and return 0."""
    stopping = threading.Event()

    def request_stop(signum: int, frame: FrameType | None) -> None:
        if not stopping.is_set():
            stopping.set()
            # shutdown() blocks until serve_forever returns, so it cannot run in the handler.
            threading.Thread(target=server.shutdown, name="factory-manager-stop", daemon=True).start()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    manager.sink.line(startup_line(server, manager, metrics_server))
    scraping: threading.Thread | None = None
    try:
        if metrics_server is not None:
            scraping = threading.Thread(
                target=metrics_server.serve_forever,
                kwargs={"poll_interval": 0.2},
                name="factory-manager-metrics",
                daemon=True,
            )
            scraping.start()
        manager.github_health.start()
        manager.start_operations()
        server.serve_forever(poll_interval=0.2)
    finally:
        if metrics_server is not None:  # first, and quick: scrapers never delay the shutdown budget
            if scraping is not None:
                metrics_server.shutdown()
                scraping.join(2)
            metrics_server.server_close()
        server.socket.close()  # stop accepting at once; handlers already running continue
        manager.terminate_children()
        server.close_connections()
        server.server_close()
    return 0


def main(argv: list | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="factory-manager",
        description="HTTP control surface for the factory CLI. Configured by FACTORY_CONFIG, FACTORY_DATA "
        "and FACTORY_MANAGER_* environment variables; see docs/manager.md.",
    )
    parser.parse_args(argv)
    environment = os.environ if env is None else env
    try:
        settings = Settings.from_env(environment)
    except ValueError as error:
        sys.stderr.write(f"factory-manager: {error}\n")
        return 2
    os.umask(0o077)  # everything the manager and its children create is private by default
    manager = Manager(settings, env=environment)
    try:
        server = make_server(manager)
    except OSError as error:
        sys.stderr.write(f"factory-manager: cannot listen on {escape_field(settings.host)}:{settings.port} ({error.strerror})\n")
        return 2
    try:
        metrics_server = make_metrics_server(manager)
    except OSError as error:
        server.server_close()
        sys.stderr.write(
            f"factory-manager: cannot listen for metrics on {escape_field(settings.metrics_host)}:{settings.metrics_port} ({error.strerror})\n"
        )
        return 2
    return serve(manager, server, metrics_server)


if __name__ == "__main__":
    sys.exit(main())

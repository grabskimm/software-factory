"""JSON Schemas passed to ``claude -p --json-schema``, one per phase.

Each schema mirrors the validation the supervisor already performs on the
parsed result (``model.validate_plan``, ``Runner.build/review/verify`` and
``preflight.probe_worker``). The supervisor keeps validating: the schema makes
Claude Code retry malformed output, it is not a security boundary.
"""

from . import epic
from .conventions import TYPES as CHANGE_TYPES  # the Conventional Commits types a job may name
from .phases import phase_name

_STRING = {"type": "string"}
_TEXT = {"type": "string", "minLength": 1}
_ISSUE = {"type": "integer", "minimum": 1}
_STRINGS = {"type": "array", "items": _STRING}

# What a builder or a reviewer may report beyond its own job (src/factory/observed_issues.py). The bounds
# are the shape of ONE worker result, not a limit on how many issues Factory opens: the supervisor validates
# every item again and applies no count of its own.
OBSERVATION_KINDS = ("out_of_scope", "cannot_fix", "bug_found", "follow_up", "environment")
MAX_OBSERVATIONS = 5
OBSERVATION_TITLE_MIN = 12
OBSERVATION_TITLE_MAX = 100
OBSERVATION_DETAIL_MAX = 1000
OBSERVATION_MAX_PATHS = 5
OBSERVATION_PATH_MAX = 200
_OBSERVATIONS = {
    "type": "array",
    "maxItems": MAX_OBSERVATIONS,
    "items": {
        "type": "object",
        "required": ["kind", "title", "detail"],
        "additionalProperties": False,
        "properties": {
            "kind": {"type": "string", "enum": list(OBSERVATION_KINDS)},
            "title": {
                "type": "string",
                "minLength": OBSERVATION_TITLE_MIN,
                "maxLength": OBSERVATION_TITLE_MAX,
            },
            "detail": {"type": "string", "minLength": 1, "maxLength": OBSERVATION_DETAIL_MAX},
            "paths": {
                "type": "array",
                "maxItems": OBSERVATION_MAX_PATHS,
                "items": {"type": "string", "minLength": 1, "maxLength": OBSERVATION_PATH_MAX},
            },
        },
    },
}

# An acceptance criterion: plain text (a ``sandbox`` criterion) or a tagged object. ``ci``
# needs ``checks`` or ``workflow``; model.criterion enforces that, this only shapes it.
_NAME = {"type": "string", "minLength": 1, "maxLength": 120}
_CRITERION = {
    "anyOf": [
        _TEXT,
        {
            "type": "object",
            "required": ["text"],
            "additionalProperties": False,
            "properties": {
                "text": _TEXT,
                "tag": {"type": "string", "enum": ["sandbox", "ci", "operator"]},
                "checks": {"type": "array", "items": _NAME, "maxItems": 20},
                "workflow": _NAME,
                # Why only a person can prove it; operator criteria only (model.MAX_REASON).
                "reason": {"type": "string", "minLength": 1, "maxLength": 300},
            },
        },
    ]
}

# The phase plan of an ``epic`` disposition; the limits mirror ``epic.py`` (which validates again).
_EPIC_LINES = {"type": "array", "minItems": 1, "maxItems": epic.MAX_ACCEPTANCE}
_EPIC = {
    "type": "object",
    "required": ["phases"],
    "additionalProperties": False,
    "properties": {
        "phases": {
            "type": "array",
            "minItems": 1,
            "maxItems": epic.MAX_PHASES,
            "items": {
                "type": "object",
                "required": ["title", "objective", "paths", "acceptance", "cost"],
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string", "minLength": 1, "maxLength": epic.MAX_TITLE},
                    "objective": {"type": "string", "minLength": 1, "maxLength": epic.MAX_OBJECTIVE},
                    "paths": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": epic.MAX_PATHS,
                        "items": {"type": "string", "minLength": 1, "maxLength": epic.MAX_PATH},
                    },
                    "acceptance": {
                        **_EPIC_LINES,
                        "items": {"type": "string", "minLength": 1, "maxLength": epic.MAX_CRITERION},
                    },
                    "depends_on": {"type": "array", "items": {"type": "integer", "minimum": 1}},
                    "cost": {"type": "string", "enum": list(epic.COSTS)},
                },
            },
        },
        "questions": {
            "type": "array",
            "maxItems": epic.MAX_QUESTIONS,
            "items": {"type": "string", "minLength": 1, "maxLength": epic.MAX_QUESTION},
        },
    },
}

PLANNER = {
    "type": "object",
    "required": ["version", "jobs", "dispositions"],
    "properties": {
        "version": {"type": "integer", "enum": [1]},
        "jobs": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "id",
                    "issues",
                    "objective",
                    "likely_paths",
                    "acceptance",
                    "feature",
                ],
                "properties": {
                    "id": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{0,59}$"},
                    "issues": {"type": "array", "items": _ISSUE, "minItems": 1},
                    "context_issues": {"type": "array", "items": _ISSUE},
                    "objective": _TEXT,
                    "likely_paths": {"type": "array", "items": _TEXT, "minItems": 1},
                    "acceptance": {"type": "array", "items": _CRITERION, "minItems": 1},
                    "depends_on": _STRINGS,
                    "feature": {"type": "boolean"},
                    "change_type": {"type": "string", "enum": list(CHANGE_TYPES)},
                },
            },
        },
        "dispositions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["issue", "status", "reason"],
                "properties": {
                    "issue": _ISSUE,
                    "status": {
                        "type": "string",
                        "enum": ["job", "investigation", "covered", "blocked", "epic"],
                    },
                    "job": _STRING,
                    "reason": _TEXT,
                    "epic": _EPIC,
                },
            },
        },
    },
}

BUILDER = {
    "type": "object",
    "required": ["summary", "blocked", "tests", "feature"],
    "properties": {
        "summary": _STRING,
        "blocked": {"type": "boolean"},
        "blocker": _STRING,
        "tests": _STRINGS,
        "feature": {"type": "boolean"},
        "observations": _OBSERVATIONS,
    },
}

REVIEWER = {
    "type": "object",
    "required": ["pass", "findings"],
    "properties": {
        "pass": {"type": "boolean"},
        "findings": _STRINGS,
        "observations": _OBSERVATIONS,
    },
}

# The test run a verifier (or a baseline session) reports: the exact command and the ids of
# the tests that failed or errored. baseline.parse_test_run validates it again.
_TEST_RUN = {
    "type": "object",
    "required": ["command"],
    "properties": {
        "command": _TEXT,
        "failing": _STRINGS,
        "erroring": _STRINGS,
        "sole_blocker": {"type": "boolean"},
    },
}

VERIFY = {
    "type": "object",
    "required": ["complete"],
    "properties": {
        "complete": {"type": "boolean"},
        "scenario": _STRING,
        "commit": _STRING,
        "environment": _STRING,
        "recording_path": _STRING,
        "recording_url": _STRING,
        "assertions": _STRINGS,
        "blocker": _STRING,
        "test_run": _TEST_RUN,
    },
}

# preflight.CAPABILITIES; spelled out here so the module has no import cycle.
_CAPABILITIES = (
    "workspace_write",
    "shared_git_write",
    "loopback_listener",
    "network_download",
)
_OBSERVATION = {
    "type": "object",
    "required": ["status", "evidence"],
    "properties": {
        "status": {"type": "string", "enum": ["available", "unavailable", "unconfirmed"]},
        "evidence": _TEXT,
    },
}
PREFLIGHT = {
    "type": "object",
    "required": list(_CAPABILITIES),
    "properties": {name: _OBSERVATION for name in _CAPABILITIES},
}

SCHEMAS = {
    "plan": PLANNER,
    "build": BUILDER,
    "review": REVIEWER,
    "verify": VERIFY,
    "preflight": PREFLIGHT,
}


def schema_for(phase):
    return SCHEMAS[phase_name(phase)]

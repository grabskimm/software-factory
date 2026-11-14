"""A fake agent executable that speaks Claude Code's stream-json protocol.

Tests point ``FACTORY_AGENT_CMD`` at a script built from ``PRELUDE`` plus a body.
The body reads the prompt from stdin and ends by calling ``emit(...)``, which
prints init/assistant lines and a final ``result`` event shaped like the real
CLI's (``is_error``, ``subtype``, ``structured_output``, ``usage``,
``modelUsage``, ``total_cost_usd``, ``num_turns``, ``duration_ms``).
"""

PRELUDE = r'''
import json, os, pathlib, sys
ARGS = sys.argv[1:]


def arg(name):
    return ARGS[ARGS.index(name) + 1]


def emit(structured=None, text=None, **overrides):
    print(json.dumps({"type": "system", "subtype": "init", "session_id": "stub", "tools": []}), flush=True)
    print(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "working"}]}}), flush=True)
    result = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": text if text is not None else (json.dumps(structured) if structured is not None else ""),
        "session_id": "stub",
        "num_turns": 2,
        "duration_ms": 25,
        "total_cost_usd": 0.0125,
        "usage": {"input_tokens": 12, "output_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        "modelUsage": {"stub-model": {"inputTokens": 12, "outputTokens": 5, "costUSD": 0.0125}},
        "permission_denials": [],
    }
    if structured is not None:
        result["structured_output"] = structured
    result.update(overrides)
    print(json.dumps(result), flush=True)
'''


def source(body):
    """Full script source (without shebang) for a stub with the given body."""
    return PRELUDE + "\n" + body

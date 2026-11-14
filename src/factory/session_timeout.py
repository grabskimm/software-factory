"""The fixed reason of an agent session that ran out of time.

``agent.run_agent`` raises ``AgentTimeout`` with exactly ``timed out after N s``; the runner stores
that sentence as the job's ``error``. Status and the outcome comment recognise a timeout by this
fixed form (never by the text of an exception or a model), so they can advise raising the limit
(``auto_build_timeout_seconds``) or splitting the issue.
"""

import re

REASON = "timed out after {} s"
_PATTERN = re.compile(r"timed out after ([0-9]{1,9}) s")
CONFIG_KEY = "auto_build_timeout_seconds"


def reason(seconds):
    """``timed out after N s`` for a limit given in seconds (whole seconds are shown)."""
    return REASON.format(int(seconds))


def seconds_of(text):
    """The limit in seconds when ``text`` is exactly a timeout reason, else None."""
    found = _PATTERN.fullmatch(text.strip()) if isinstance(text, str) else None
    return int(found.group(1)) if found else None

"""`baton:` commands: messages from the person that steer a job instead of being work for it.

They start with `baton:` because `/` belongs to OpenCode. Only the start of a message counts, so
text that merely mentions a command is an ordinary message, and nothing an agent writes is ever
parsed here: the gateway calls this only on the person's messages.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

GOAL, GOAL_OFF, NEXT, USE, HELP = "goal", "goal_off", "next", "use", "help"

_GOAL_RE = re.compile(r"baton:\s*goal\s+(.+)", re.IGNORECASE | re.DOTALL)
_GOAL_OFF_RE = re.compile(r"baton:\s*goal\s+off", re.IGNORECASE)
_NEXT_RE = re.compile(r"baton:\s*next(\s+now)?", re.IGNORECASE)
_USE_RE = re.compile(
    r"baton:\s*use\s+(codex|antigravity|claude(?:_api)?)(?:\s+(now))?",
    re.IGNORECASE,
)
_HELP_RE = re.compile(r"baton:\s*help", re.IGNORECASE)


@dataclass(frozen=True)
class Command:
    kind: str  # GOAL, GOAL_OFF, NEXT, USE or HELP
    text: str = ""  # the goal, for GOAL
    now: bool = False  # for NEXT: do not wait for the round to end
    agent: str = ""  # for USE


def parse_command(text: str) -> Command | None:
    """The command `text` is, or None for an ordinary message."""
    text = text.strip()
    if _HELP_RE.fullmatch(text):
        return Command(HELP)
    if _GOAL_OFF_RE.fullmatch(text):
        return Command(GOAL_OFF)
    found = _USE_RE.fullmatch(text)
    if found:
        agent = found.group(1).lower()
        return Command(
            USE,
            now=found.group(2) is not None,
            agent="claude_api" if agent == "claude" else agent,
        )
    found = _NEXT_RE.fullmatch(text)
    if found:
        return Command(NEXT, now=found.group(1) is not None)
    found = _GOAL_RE.fullmatch(text)
    if found and found.group(1).strip():
        return Command(GOAL, found.group(1).strip())
    return None

"""PreToolUse hook for Claude and Codex rounds: a subagent may use only the models baton allows.

Claude Code and Codex run this as `python -I subagent_guard.py`, with the hook's JSON on stdin. It
reads the allowed model names from `BATON_ALLOWED_MODELS` (comma separated), set in the round's
environment. For the tools that start a subagent (`Agent` and `Task` in Claude Code, any name
containing `spawn_agent` in Codex, which prefixes it, as in `collaborationspawn_agent`) it allows
a call that names no model or one in the list, and denies every other.

What this means in practice differs by vendor. In Codex it is the block: a model outside the pair
is denied before the subagent exists, and the agent can retry with an allowed one; a call that
names none inherits (unless a role pins one). In Claude, baton sets `CLAUDE_CODE_SUBAGENT_MODEL` to
the working model with `CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1`, which makes Claude Code drop the
`model` parameter: a named model is replaced, not denied, so every subagent runs on the working
model, and this hook only ever denies unreadable input or an empty allowed list. With no allowed
model at all, every subagent call is denied.

Standard library only, and it fails closed: any error, bad input or missing setting is a deny.
"""

from __future__ import annotations

import json
import os
import sys

SUBAGENT_TOOLS = ("Agent", "Task")
SUBAGENT_TOOL_PART = "spawn_agent"


def is_subagent_tool(name: object) -> bool:
    return isinstance(name, str) and (name in SUBAGENT_TOOLS or SUBAGENT_TOOL_PART in name)


def allowed_models(value: str) -> list[str]:
    return [m.strip() for m in value.split(",") if m.strip()]


def deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def decide(raw: str, allowed_env: str) -> dict | None:
    """The hook's answer for one call: a deny document, or None to leave the call allowed.

    Raises nothing it knows of; `main` still turns anything that escapes into a deny."""
    allowed = allowed_models(allowed_env)
    try:
        event = json.loads(raw)
    except ValueError:
        return deny("baton's subagent guard could not read the tool call, so it is denied")
    if not isinstance(event, dict):
        return deny("baton's subagent guard could not read the tool call, so it is denied")
    if not is_subagent_tool(event.get("tool_name")):
        return None
    if not allowed:
        return deny("baton allows no subagent model here, so every subagent call is denied")
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return deny("baton's subagent guard could not read the subagent's input, so it is denied")
    model = tool_input.get("model")
    if model is None:
        return None  # it inherits
    shown = ", ".join(allowed) or "none"
    if not isinstance(model, str) or model.strip().lower() not in {m.lower() for m in allowed}:
        return deny(
            f"a subagent may use only these models: {shown}. Use one of them, or name no model "
            "to take the default."
        )
    return None


def main() -> int:
    try:
        answer = decide(sys.stdin.read(), os.environ.get("BATON_ALLOWED_MODELS", ""))
    except BaseException:  # fail closed, whatever went wrong
        answer = deny("baton's subagent guard failed, so the call is denied")
    if answer is not None:
        sys.stdout.write(json.dumps(answer) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())

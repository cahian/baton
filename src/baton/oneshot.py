"""Whole-task instructions and the shared, sanitized transcript used across vendors."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from baton.proc import mask_secrets

TRANSCRIPTS = Path(".baton") / "conversations"


def transcript_path(workdir: Path, name: str = "conversation") -> Path:
    safe = "".join(char if char.isalnum() or char in "._-" else "_" for char in name)
    return workdir / TRANSCRIPTS / f"{safe or 'conversation'}.md"


def append_transcript(workdir: Path, text: str, name: str = "conversation") -> bool:
    """Append sanitized conversation text. A transcript failure never stops the coding task."""
    try:
        path = transcript_path(workdir, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(mask_secrets(text))
            if text and not text.endswith("\n"):
                stream.write("\n")
        return True
    except OSError:
        return False


class TranscriptWriter:
    """Stream one vendor prompt and its output into the full shared history."""

    def __init__(self, workdir: Path, name: str, header: str, prompt: str) -> None:
        self.stream = None
        try:
            path = transcript_path(workdir, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.stream = path.open("a", encoding="utf-8")
            self.write(
                f"\n## {header}\n\n### Prompt sent to vendor\n\n{prompt}\n\n### Vendor output\n\n"
            )
        except OSError:
            self.stream = None

    def write(self, text: str) -> None:
        if self.stream is None:
            return
        try:
            self.stream.write(mask_secrets(text))
        except OSError:
            self.close()

    def close(self) -> None:
        if self.stream is None:
            return
        try:
            self.stream.write("\n\n")
            self.stream.flush()
            self.stream.close()
        except OSError:
            pass
        finally:
            self.stream = None


def message_transcript(messages: Sequence[object]) -> str:
    """Readable full user/assistant/tool history; system setup is intentionally excluded."""
    parts: list[str] = []
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        role = message.get("role")
        if role not in ("user", "assistant", "tool"):
            continue
        parts.append(f"### {role}\n")
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, Mapping) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                else:
                    parts.append(json.dumps(item, ensure_ascii=False, default=str))
        elif content is not None:
            parts.append(json.dumps(content, ensure_ascii=False, default=str))
        for key in ("name", "tool_call_id", "tool_calls", "function_call"):
            value = message.get(key)
            if value is not None:
                parts.append(f"\n{key}: {json.dumps(value, ensure_ascii=False, default=str)}")
        parts.append("\n\n")
    return mask_secrets("".join(parts))


def session_context(agent: str, sessions: Mapping[str, str], transcript: Path) -> str:
    """Tell a vendor which native conversations already worked on this objective."""
    labels = {"codex": "Codex", "antigravity": "Antigravity", "claude_api": "Claude API"}
    entries = [
        f"- {labels.get(name, name)} session id: `{session}`"
        for name, session in sessions.items()
        if isinstance(session, str) and session
    ]
    if not entries:
        return (
            f"Shared transcript: `{transcript}` holds this task's full sanitized history. "
            "Read it in full before acting."
        )
    current = next((value for name, value in sessions.items() if name == agent), None)
    status = (
        f"This vendor's own session ({current}) is resumed automatically."
        if current
        else "This vendor is starting a new native session."
    )
    return (
        "Vendor sessions already used for this objective:\n"
        + "\n".join(entries)
        + "\n"
        + status
        + f" Read the entire `{transcript}` before acting; it contains the full "
        "sanitized user and vendor conversation. Do not try to resume another vendor's session."
    )


def one_shot_block(agent: str, sessions: Mapping[str, str], transcript: Path) -> str:
    """Instructions for one complete vendor-owned task, with Codex's native goal when available."""
    parts = [
        "Whole-task ownership: complete the full objective in this vendor session. Do not stop "
        "after planning, a partial task, or a handoff proposal. Use this vendor's native tools, "
        "skills, and subagents as useful; invoke its native `oneshot` skill when that skill is "
        "installed. Make the changes, run the relevant verification, review the result, and return "
        "only when the objective is complete or a genuine blocker prevents progress.",
        "Baton preserves this vendor's native session id and transfers the complete sanitized "
        "conversation to the next vendor only when a configured usage/turn limit is reached or the "
        "person asks to switch.",
        session_context(agent, sessions, transcript),
    ]
    if agent == "codex":
        parts.append(
            "Use Codex's native Goal feature for this objective: create a native Goal with the "
            "requested outcome and its completion condition before working, keep it active while "
            "the task is incomplete, and mark it complete only after checking the result. Invoke "
            "the native `$oneshot` skill if it is installed; otherwise complete the task directly "
            "with Codex's native workflow."
        )
    return "\n\n".join(parts)

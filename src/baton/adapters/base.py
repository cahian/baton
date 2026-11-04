"""Types shared by every agent adapter."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

COMPLETED, LIMIT, CREDIT_GUARD, AUTH, TRANSIENT, TIMEOUT = (
    "completed",
    "limit",
    "credit_guard",
    "auth",
    "transient",
    "timeout",
)


@dataclass(frozen=True)
class RoundResult:
    """How one round ended. `until` is an epoch time before which the agent should rest."""

    outcome: str
    until: float | None = None
    reason: str = ""
    resume_failed: bool = False


@dataclass(frozen=True)
class Availability:
    """Whether an agent may run now. Unknown means not ok."""

    ok: bool
    until: float | None = None
    reason: str = ""
    usage: dict = field(default_factory=dict)


@dataclass
class RoundContext:
    workdir: Path
    log_path: Path
    round_max_s: int
    idle_s: int
    on_output: Callable[[str], None]
    should_kill: Callable[[], bool]
    on_spawn: Callable[[int], None]
    session_id: str | None = None
    on_session: Callable[[str], None] = lambda _: None


class Adapter(Protocol):
    name: str

    def availability(self) -> Availability: ...

    def run_round(self, prompt: str, ctx: RoundContext) -> RoundResult: ...

    def render(self, line: str) -> str | None: ...

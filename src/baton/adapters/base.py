"""Types shared by every agent adapter."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

COMPLETED, LIMIT, CREDIT_GUARD, AUTH, TRANSIENT, TIMEOUT, PREEMPTED = (
    "completed",
    "limit",
    "credit_guard",
    "auth",
    "transient",
    "timeout",
    "preempted",
)


@dataclass(frozen=True)
class RoundResult:
    """How one round ended. `until` is an epoch time before which the agent should rest."""

    outcome: str
    until: float | None = None
    reason: str = ""
    resume_failed: bool = False
    back: str = ""  # PREEMPTED only: the plan agent whose return ended the round


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
    # True for an agent paid by use, with no reset that would ever stop it: it runs only while
    # no plan agent can, and a plan agent coming back ends its round.
    metered: bool

    def availability(self) -> Availability: ...

    def run_round(self, prompt: str, ctx: RoundContext) -> RoundResult: ...

    def render(self, line: str) -> str | None: ...

    def is_work(self, line: str) -> bool:
        """True when `line` shows the agent acting on its prompt. An error message or noise that
        `render` also shows does not count: a round that only said that never read the notes."""
        ...

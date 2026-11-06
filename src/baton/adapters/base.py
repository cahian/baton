"""Types shared by every agent adapter."""

from __future__ import annotations

import re
import shlex
import sys
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from baton import subagent_guard

if TYPE_CHECKING:
    from baton.config import AgentConfig

COMPLETED, LIMIT, CREDIT_GUARD, AUTH, TRANSIENT, TIMEOUT, PREEMPTED, SUBAGENT = (
    "completed",
    "limit",
    "credit_guard",
    "auth",
    "transient",
    "timeout",
    "preempted",
    "subagent",  # a visible subagent model outside the provider's two
)


@dataclass(frozen=True)
class RoundResult:
    """How one round ended. `until` is an epoch time before which the agent should rest."""

    outcome: str
    until: float | None = None
    reason: str = ""
    resume_failed: bool = False
    back: str = ""  # PREEMPTED only: the plan agent whose return ended the round
    # COMPLETED only: the round was cut off by its own cap, so its work is not finished. The
    # scheduler treats it as completed; a phase must not advance on it, nor a goal finish.
    bounded: bool = False
    # What the round cost, in USD, when the agent reports it (claude_api does): 0 otherwise.
    cost_usd: float = 0.0


@dataclass(frozen=True)
class Availability:
    """Whether an agent may run now. Unknown means not ok. `permanent` says a no that waiting does
    not end: the agent is disabled, has no usable key, or its settings cannot run a round. It
    differs from being out of quota, which ends by itself."""

    ok: bool
    until: float | None = None
    reason: str = ""
    usage: dict = field(default_factory=dict)
    permanent: bool = False


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
    # Claude reports its cost as the session's total so far. What the session had reported by the
    # end of its last round (None for a session with no saved total), so a round is charged only
    # for what it added; and told, as (session, total), the total a round reports.
    session_total: Decimal | None = None
    on_session_total: Callable[[str, Decimal], None] = lambda session, total: None
    # "strong" (plan, review and the plain relay) or "build": which of the two models runs.
    tier: str = "strong"
    # Summary-only requests must not write to the user's repository.
    read_only: bool = False
    # Use the vendor's native persistent goal feature for whole-task mode when available.
    native_goal: bool = False
    # Antigravity's first native /goal prompt is sent as a CLI argument because slash commands
    # cannot be submitted in stream-json input mode. The full task remains in the transcript.
    native_goal_prompt: str | None = None
    # Told, with the tool and the model when the stream shows it, of every subagent a round starts.
    on_subagent: Callable[[str, str | None], None] = lambda tool, model: None


def tier_config(cfg: AgentConfig, tier: str) -> AgentConfig:
    """`cfg` for a tier: the build tier swaps in the working model and effort, and nothing else
    (the guard, the ledger, the budget and the resume all read the same config)."""
    if tier == "build":
        return replace(cfg, model=cfg.build_model, effort=cfg.build_effort)
    return cfg


_EFFORT_SUFFIX = re.compile(r"-(minimal|low|medium|high|xhigh)$")


_CLAUDE_FAMILY = re.compile(r"^claude-([a-z]+)-")


def claude_allowed_models(cfg: AgentConfig) -> list[str]:
    """The names a Claude subagent may be given: the working model and the strong one, each by its
    id and by its family alias (`sonnet`, `opus`), which Claude Code accepts."""
    names: list[str] = []
    for model in (cfg.build_model, cfg.model):
        found = _CLAUDE_FAMILY.match(model)
        for name in ([found.group(1)] if found else []) + [model]:
            if name not in names:
                names.append(name)
    return names


def model_allowed(cfg: AgentConfig, model: str) -> bool:
    """Whether a subagent may use `model`: exactly the provider's two, the strong and the build
    one, and nothing that only looks like one. Antigravity keeps the effort in the model id, so
    for it the two ids also match without their effort suffix."""
    name = model.strip().lower()
    allowed = {cfg.model.lower(), cfg.build_model.lower()}
    if cfg.name == "antigravity":
        allowed |= {_EFFORT_SUFFIX.sub("", a) for a in allowed}
    if cfg.name == "claude_api":
        allowed |= {n.lower() for n in claude_allowed_models(cfg)}
    return name in allowed


def subagent_guard_command() -> str:
    """The command a vendor's PreToolUse hook runs: this Python, in isolated mode (`-I`: no
    environment or user site can change what it imports), on the guard module."""
    return shlex.join([sys.executable, "-I", str(Path(subagent_guard.__file__))])


def check_subagent(
    cfg: AgentConfig, ctx: RoundContext, tool: str, model: str | None, enforce: bool = False
) -> RoundResult | None:
    """Report a subagent the stream shows, and, with `enforce`, judge it: SUBAGENT only when its
    model is visible and outside the pair. A subagent whose model the stream does not show is
    logged and allowed, never guessed.

    Claude and Codex do not enforce here: their PreToolUse hook denies the call before the
    subagent exists, so the stream is a log (ending a round it would have handled costs a round).
    Antigravity has no hook, and its model is rarely visible, so it enforces what it can see."""
    ctx.on_subagent(tool, model)
    if not enforce or model is None or model_allowed(cfg, model):
        return None
    allowed = ", ".join(sorted({cfg.model, cfg.build_model}))
    return RoundResult(
        SUBAGENT, reason=f"{tool} started a subagent on {model}, outside the allowed {allowed}"
    )


_MODEL_KEYS = ("model", "model_name", "modelname")


def find_model(obj: object, depth: int = 2) -> str | None:
    """A model name a stream event shows: a `model` key in the event or in a dict directly under
    it. None when it does not show one."""
    if not isinstance(obj, dict) or depth < 0:
        return None
    for key, value in obj.items():
        if isinstance(key, str) and key.lower() in _MODEL_KEYS:
            if isinstance(value, str) and value.strip():
                return value
    for value in obj.values():
        found = find_model(value, depth - 1)
        if found is not None:
            return found
    return None


class ToolWatch:
    """The tool calls of one round that have started and not yet ended, read from its stream.

    The runner feeds it every output line and asks `busy` before it ends a round for a note: an
    interrupt then waits for the tool, so a command is not cut off halfway. A line it does not
    understand changes nothing. Adapters subclass it and say what a start and an end look like."""

    def __init__(self) -> None:
        self.open: set[object] = set()

    @property
    def busy(self) -> bool:
        return bool(self.open)

    def feed(self, line: str) -> None:
        raise NotImplementedError


class Adapter(Protocol):
    name: str
    # True for an agent paid by use, with no reset that would ever stop it. It only means budgets.
    metered: bool
    # True for an agent that waits for the others: it runs only while no other agent can, and
    # another coming back ends its round.
    reserve: bool

    def availability(self) -> Availability: ...

    def run_round(self, prompt: str, ctx: RoundContext) -> RoundResult: ...

    def render(self, line: str) -> str | None: ...

    def response_text(self, line: str) -> str | None:
        """Text safe to return for a response-only request; excludes commands and tool activity."""
        ...

    def is_work(self, line: str) -> bool:
        """True when `line` shows the agent acting on its prompt. An error message or noise that
        `render` also shows does not count: a round that only said that never read the notes."""
        ...

    def tool_watch(self) -> ToolWatch | None:
        """A new watch for one round, or None when the stream does not show tool calls. The
        runner reads it with getattr: an adapter without it is interrupted at once."""
        ...

"""Goal file loading and validation."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

AGENT_NAMES = ("codex", "antigravity")
DEFAULT_ORDER = ("codex", "antigravity")

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Per-agent defaults. `bin` is a bare command name, resolved from PATH later.
_AGENT_DEFAULTS: dict[str, dict[str, Any]] = {
    "codex": {
        "enabled": True,
        "model": "gpt-6.1-sol",
        "effort": "medium",
        "threshold_pct": 90,
        "poll_s": 15,
        "bin": "codex",
        "full_access": True,
    },
    "antigravity": {
        "enabled": True,
        "model": "gemini-4-argon-medium",
        "effort": "medium",  # unused by agy: effort is part of the model id
        "threshold_pct": 90,
        "poll_s": 30,
        "bin": "agy",
        "full_access": True,
    },
}


class GoalError(Exception):
    """The goal file is missing, malformed or invalid."""


@dataclass(frozen=True)
class AgentConfig:
    name: str
    enabled: bool
    model: str
    effort: str
    threshold_pct: int
    poll_s: int
    bin: str
    full_access: bool


@dataclass(frozen=True)
class Goal:
    name: str
    workdir: Path
    objective: str
    order: tuple[str, ...]
    round_max_s: int
    idle_s: int
    snapshot: bool
    verify_command: str | None
    verify_timeout_s: int
    agents: dict[str, AgentConfig]
    path: Path


def _table(data: dict[str, Any], key: str, where: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise GoalError(f"{where}{key} must be a table")
    return value


def _check_keys(table: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise GoalError(f"unknown key(s) in {where}: {', '.join(unknown)}")


def _required_str(table: dict[str, Any], key: str, where: str = "") -> str:
    if key not in table:
        raise GoalError(f"missing required key: {where}{key}")
    value = table[key]
    if not isinstance(value, str) or not value.strip():
        raise GoalError(f"{where}{key} must be a non-empty string")
    return value


def _str(table: dict[str, Any], key: str, default: str, where: str) -> str:
    value = table.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise GoalError(f"{where}{key} must be a non-empty string")
    return value


def _bool(table: dict[str, Any], key: str, default: bool, where: str) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise GoalError(f"{where}{key} must be true or false")
    return value


def _int(table: dict[str, Any], key: str, default: int, where: str, lo: int, hi: int) -> int:
    value = table.get(key, default)
    # bool is a subclass of int; reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, int):
        raise GoalError(f"{where}{key} must be an integer")
    if not lo <= value <= hi:
        raise GoalError(f"{where}{key} must be between {lo} and {hi}")
    return value


def _load_agent(name: str, table: dict[str, Any]) -> AgentConfig:
    where = f"agents.{name}."
    _check_keys(table, set(_AGENT_DEFAULTS[name]), f"[agents.{name}]")
    d = _AGENT_DEFAULTS[name]
    return AgentConfig(
        name=name,
        enabled=_bool(table, "enabled", d["enabled"], where),
        model=_str(table, "model", d["model"], where),
        effort=_str(table, "effort", d["effort"], where),
        threshold_pct=_int(table, "threshold_pct", d["threshold_pct"], where, 1, 99),
        poll_s=_int(table, "poll_s", d["poll_s"], where, 1, 3600),
        bin=_str(table, "bin", d["bin"], where),
        full_access=_bool(table, "full_access", d["full_access"], where),
    )


def default_agent(name: str) -> AgentConfig:
    """An agent with every documented default, for commands that run without a goal file."""
    return _load_agent(name, {})


def load_goal(path: Path) -> Goal:
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise GoalError(f"cannot read goal file {path}: {exc.strerror or exc}") from exc
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise GoalError(f"invalid goal file {path}: {exc}") from exc

    _check_keys(
        data,
        {
            "name",
            "workdir",
            "objective",
            "order",
            "round_max_s",
            "idle_s",
            "snapshot",
            "verify",
            "agents",
        },
        "goal file",
    )

    name = _required_str(data, "name")
    if not _NAME_RE.fullmatch(name):
        raise GoalError("name may contain only letters, digits, '.', '_' and '-'")

    workdir = Path(_required_str(data, "workdir")).expanduser()
    if not workdir.is_absolute():
        workdir = path.resolve().parent / workdir
    objective = _required_str(data, "objective")

    order_raw = data.get("order", list(DEFAULT_ORDER))
    if (
        not isinstance(order_raw, list)
        or not order_raw
        or not all(isinstance(a, str) for a in order_raw)
    ):
        raise GoalError("order must be a non-empty list of agent names")
    for agent in order_raw:
        if agent not in AGENT_NAMES:
            raise GoalError(f"unknown agent in order: {agent!r} (known: {', '.join(AGENT_NAMES)})")
    if len(set(order_raw)) != len(order_raw):
        raise GoalError("order lists an agent more than once")

    verify = _table(data, "verify", "")
    _check_keys(verify, {"command", "timeout_s"}, "[verify]")
    verify_command = _required_str(verify, "command", "verify.") if "command" in verify else None

    agents_table = _table(data, "agents", "")
    unknown = sorted(set(agents_table) - set(AGENT_NAMES))
    if unknown:
        raise GoalError(f"unknown agent in [agents]: {', '.join(unknown)}")
    agents: dict[str, AgentConfig] = {}
    for agent in AGENT_NAMES:
        sub = agents_table.get(agent, {})
        if not isinstance(sub, dict):
            raise GoalError(f"agents.{agent} must be a table")
        agents[agent] = _load_agent(agent, sub)
    if not any(agents[a].enabled for a in order_raw):
        raise GoalError("every agent in order is disabled; nothing could ever run")

    return Goal(
        name=name,
        workdir=workdir,
        objective=objective,
        order=tuple(order_raw),
        round_max_s=_int(data, "round_max_s", 3600, "", 1, 10**7),
        idle_s=_int(data, "idle_s", 1200, "", 1, 10**7),
        snapshot=_bool(data, "snapshot", True, ""),
        verify_command=verify_command,
        verify_timeout_s=_int(verify, "timeout_s", 3600, "verify.", 1, 10**7),
        agents=agents,
        path=path,
    )

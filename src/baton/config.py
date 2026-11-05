"""Goal file loading and validation."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

AGENT_NAMES = ("claude_api", "antigravity", "codex")
DEFAULT_ORDER = ("codex", "antigravity", "claude_api")

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Per-agent defaults. `bin` is a bare command name, resolved from PATH later, or a path, where a
# leading `~` is expanded.
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
    # Claude through a Console API key. Its limits are baton's own spend ledger, so it has no
    # threshold or poll interval, and it always runs with permissions skipped (the spec's command).
    "claude_api": {
        "enabled": True,
        "model": "claude-opus-5-5",
        "effort": "medium",
        "bin": "claude",
        "monthly_budget_usd": Decimal("200"),
        "reserve_pct": 10,
        "round_budget_usd": Decimal("10"),
        "budget_reset_day": 1,
        "key_file": "~/.config/baton/anthropic.key",
        "admin_key_file": "",
    },
}
# What claude_api carries in AgentConfig without the goal file being able to set it.
_CLAUDE_FIXED = {"threshold_pct": 95, "poll_s": 15, "full_access": True}


# Budget settings shared by every goal: they live only in the defaults file, because the one
# ledger they describe is shared too.
GOAL_KEYS = frozenset(
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
    }
)
GLOBAL_ONLY = ("monthly_budget_usd", "reserve_pct", "budget_reset_day")
# What a `baton.toml` may not set: it comes with the repository, so it must not choose which
# executables run, which credentials are read, whether the sandbox is on or how rarely the guard
# reads the limits.
REPO_FORBIDDEN = ("bin", "key_file", "admin_key_file", "full_access", "poll_s")
MAX_THRESHOLD_PCT = 90
ANTIGRAVITY_MODEL_PREFIX = "gemini-"


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
    # Only claude_api reads these; the goal file accepts them for no other agent.
    monthly_budget_usd: Decimal = Decimal("200")
    reserve_pct: int = 10
    round_budget_usd: Decimal = Decimal("10")
    budget_reset_day: int = 1
    key_file: str = "~/.config/baton/anthropic.key"
    admin_key_file: str = ""


@dataclass(frozen=True)
class GoalRef:
    """What names a goal: enough to find its state, whatever is wrong with its settings."""

    name: str
    workdir: Path
    path: Path


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


def _usd(table: dict[str, Any], key: str, default: Decimal, where: str) -> Decimal:
    """A positive amount of money as an exact Decimal. TOML floats arrive as Decimal already."""
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | Decimal):
        raise GoalError(f"{where}{key} must be a number")
    number = Decimal(value)
    if not number.is_finite() or number <= 0:
        raise GoalError(f"{where}{key} must be greater than zero")
    return number


def _optional_str(table: dict[str, Any], key: str, default: str, where: str) -> str:
    value = table.get(key, default)
    if not isinstance(value, str):
        raise GoalError(f"{where}{key} must be a string")
    return value


def _load_claude_api(table: dict[str, Any]) -> AgentConfig:
    name = "claude_api"
    where = f"agents.{name}."
    d = _AGENT_DEFAULTS[name]
    _check_keys(table, set(d), f"[agents.{name}]")
    return AgentConfig(
        name=name,
        enabled=_bool(table, "enabled", d["enabled"], where),
        model=_str(table, "model", d["model"], where),
        effort=_str(table, "effort", d["effort"], where),
        bin=os.path.expanduser(_str(table, "bin", d["bin"], where)),
        monthly_budget_usd=_usd(table, "monthly_budget_usd", d["monthly_budget_usd"], where),
        reserve_pct=_int(table, "reserve_pct", d["reserve_pct"], where, 1, 99),
        round_budget_usd=_usd(table, "round_budget_usd", d["round_budget_usd"], where),
        budget_reset_day=_int(table, "budget_reset_day", d["budget_reset_day"], where, 1, 31),
        key_file=_str(table, "key_file", d["key_file"], where),
        admin_key_file=_optional_str(table, "admin_key_file", d["admin_key_file"], where),
        **_CLAUDE_FIXED,
    )


def _load_agent(name: str, table: dict[str, Any]) -> AgentConfig:
    if name == "claude_api":
        return _load_claude_api(table)
    where = f"agents.{name}."
    _check_keys(table, set(_AGENT_DEFAULTS[name]), f"[agents.{name}]")
    d = _AGENT_DEFAULTS[name]
    model = _str(table, "model", d["model"], where)
    if name == "antigravity" and not model.startswith(ANTIGRAVITY_MODEL_PREFIX):
        raise GoalError(f"{where}model must start with {ANTIGRAVITY_MODEL_PREFIX!r}")
    return AgentConfig(
        name=name,
        enabled=_bool(table, "enabled", d["enabled"], where),
        model=model,
        effort=_str(table, "effort", d["effort"], where),
        threshold_pct=_int(table, "threshold_pct", d["threshold_pct"], where, 1, MAX_THRESHOLD_PCT),
        poll_s=_int(table, "poll_s", d["poll_s"], where, 1, 3600),
        bin=os.path.expanduser(_str(table, "bin", d["bin"], where)),
        full_access=_bool(table, "full_access", d["full_access"], where),
    )


def defaults_path() -> Path:
    """`~/.config/baton/defaults.toml`, or the file `BATON_CONFIG` names."""
    override = os.environ.get("BATON_CONFIG")
    return Path(override).expanduser() if override else Path.home() / ".config/baton/defaults.toml"


def load_defaults() -> dict[str, dict[str, Any]]:
    """The `[agents.*]` overrides applied under every goal, as raw tables by agent name.

    A missing file is no overrides. A file that cannot be read or has anything unknown or invalid
    in it raises GoalError: settings that cannot be trusted are not guessed around. Money is
    parsed as Decimal.
    """
    path = defaults_path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise GoalError(f"cannot read {path}: {exc.strerror or exc}") from exc
    try:
        data = tomllib.loads(raw.decode("utf-8"), parse_float=Decimal)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise GoalError(f"invalid defaults file {path}: {exc}") from exc
    try:
        _check_keys(data, {"agents"}, "the defaults file")
        agents = _table(data, "agents", "")
        unknown = sorted(set(agents) - set(AGENT_NAMES))
        if unknown:
            raise GoalError(f"unknown agent in [agents]: {', '.join(unknown)}")
        tables: dict[str, dict[str, Any]] = {}
        for name, table in agents.items():
            if not isinstance(table, dict):
                raise GoalError(f"agents.{name} must be a table")
            _load_agent(name, table)  # validates every value
            tables[name] = table
    except GoalError as exc:
        raise GoalError(f"{path}: {exc}") from exc
    return tables


def default_agent(name: str) -> AgentConfig:
    """An agent with every documented default and the defaults file applied, for commands that
    run without a goal file."""
    return _load_agent(name, load_defaults().get(name, {}))


def _read_goal_table(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise GoalError(f"cannot read goal file {path}: {exc.strerror or exc}") from exc
    try:
        return tomllib.loads(raw.decode("utf-8"), parse_float=Decimal)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise GoalError(f"invalid goal file {path}: {exc}") from exc


def _goal_ref(data: dict[str, Any], path: Path) -> GoalRef:
    name = _required_str(data, "name")
    if not _NAME_RE.fullmatch(name):
        raise GoalError("name may contain only letters, digits, '.', '_' and '-'")
    workdir = Path(_required_str(data, "workdir")).expanduser()
    if not workdir.is_absolute():
        workdir = path.resolve().parent / workdir
    return GoalRef(name, workdir, path)


def read_goal_ref(path: Path) -> GoalRef:
    """A goal file's name and directory, without applying the defaults file or judging any
    setting: what `baton status` and `baton stop` need, and all they may depend on."""
    path = Path(path)
    return _goal_ref(_read_goal_table(path), path)


def load_goal(path: Path) -> Goal:
    path = Path(path)
    data = _read_goal_table(path)
    _check_keys(data, GOAL_KEYS, "goal file")
    ref = _goal_ref(data, path)
    objective = _required_str(data, "objective")
    rest = {k: v for k, v in data.items() if k not in ("name", "workdir", "objective")}
    return goal_from_table(rest, name=ref.name, workdir=ref.workdir, objective=objective, path=path)


def check_repo_table(data: dict[str, Any]) -> None:
    """Refuse what a `baton.toml` may not set. A malformed `agents` is left for the builder."""
    agents = data.get("agents")
    for agent, table in agents.items() if isinstance(agents, dict) else ():
        for key in REPO_FORBIDDEN:
            if isinstance(table, dict) and key in table:
                raise GoalError(
                    f"agents.{agent}.{key} may not be set here: a file that comes with a "
                    "repository must not choose executables or credentials, or slow the guard's "
                    f"limit reads (use {defaults_path()})"
                )


def goal_from_table(
    data: dict[str, Any], *, name: str, workdir: Path, objective: str, path: Path
) -> Goal:
    """The Goal for a parsed goal table (`order`, timings, `[verify]`, `[agents.*]`).

    The one place a goal is validated and the defaults file applied, whether the table came from
    a goal file or from the `baton.toml` of an OpenCode session. `name`, `workdir` and
    `objective` are given, not read from the table, so a table that carries them is rejected.
    """
    _check_keys(data, GOAL_KEYS - {"name", "workdir", "objective"}, "goal table")

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
    defaults = load_defaults()
    agents: dict[str, AgentConfig] = {}
    for agent in AGENT_NAMES:
        sub = agents_table.get(agent, {})
        if not isinstance(sub, dict):
            raise GoalError(f"agents.{agent} must be a table")
        for key in GLOBAL_ONLY if agent == "claude_api" else ():
            if key in sub:
                raise GoalError(
                    f"agents.{agent}.{key} is a global setting: put it in {defaults_path()}, "
                    "not in a goal file"
                )
        agents[agent] = _load_agent(agent, {**defaults.get(agent, {}), **sub})
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

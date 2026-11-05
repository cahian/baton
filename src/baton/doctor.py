"""`baton doctor`: what is installed, logged in, configured wrongly and how much quota is left.

Everything here is read-only. Limits come from the same two reads the adapters use (the Codex
app-server and the Antigravity `/usage` and `/credits` commands); no prompt is ever sent.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple

from baton import superpowers
from baton.adapters import antigravity, claude_api, codex
from baton.config import (
    AGENT_NAMES,
    AgentConfig,
    Goal,
    GoalError,
    default_agent,
    load_memory,
    load_superpowers,
)
from baton.ledger import MIN_GRANT_USD, LedgerError
from baton.memory import Memory
from baton.proc import SCRUBBED_ENV

PS_TIMEOUT_S = 10
# A `codex` with one of these on its command line is a job or a server, not a person at a prompt.
_NOT_INTERACTIVE = {
    "exec",
    "e",
    "app-server",
    "mcp",
    "mcp-server",
    "login",
    "logout",
    "completion",
    "apply",
    "review",
    "cloud",
    "sandbox",
    "proto",
}


@dataclass
class Check:
    name: str
    ok: bool
    blocking: bool  # only meaningful when not ok: a blocking failure makes doctor exit non-zero
    detail: str


def _when(epoch: object) -> str:
    try:
        return datetime.fromtimestamp(float(epoch), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError, OSError):
        return "unknown"


def _pct(value: object) -> str:
    if isinstance(value, Decimal):
        value = int(value) if value == value.to_integral_value() else float(value)
    return f"{value:g}%" if isinstance(value, int | float) else "?"


# processes


class Proc(NamedTuple):
    pid: int
    ppid: int
    args: str


def list_processes() -> list[Proc]:
    """Every process, from `ps`. Raises OSError/SubprocessError."""
    out = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,args="],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=PS_TIMEOUT_S,
        check=True,
    ).stdout
    found = []
    for line in out.splitlines():
        pid, _, rest = line.strip().partition(" ")
        ppid, _, args = rest.strip().partition(" ")
        if pid.isdigit() and ppid.isdigit():
            found.append(Proc(int(pid), int(ppid), args.strip()))
    return found


def interactive_codex(processes: list[Proc]) -> list[Proc]:
    """The person's own `codex` sessions (not baton's `exec` or a server), one per session: the
    npm launcher and the native binary it starts count once."""
    found = []
    for proc in processes:
        if proc.pid == os.getpid():
            continue
        words = proc.args.split()
        if not words:
            continue
        program = os.path.basename(words[0])
        if program in ("node", "bun") and len(words) > 1:
            words = words[1:]
            program = os.path.basename(words[0])
        if program != "codex" or _NOT_INTERACTIVE & set(words[1:]):
            continue
        found.append(proc)
    pids = {proc.pid for proc in found}
    return [proc for proc in found if proc.ppid not in pids]


# checks


def _environment(env: Mapping[str, str]) -> list[Check]:
    present = [var for var in SCRUBBED_ENV if var in env]
    if not present:
        return [Check("environment", True, False, "no API-key variables are set")]
    # The value is never read into a message: a name is all the person needs.
    return [
        Check(
            f"environment {var}",
            False,
            False,
            f"{var} is set. baton removes it from every agent process, but a shell or tool "
            "started outside baton would still use it.",
        )
        for var in present
    ]


def _binary(cfg: AgentConfig, env: Mapping[str, str], blocking: bool) -> tuple[Check, bool]:
    name = f"{cfg.name} binary"
    if not cfg.enabled:
        return Check(name, True, False, "disabled in the goal file"), False
    path = shutil.which(cfg.bin, path=env.get("PATH"))
    if path is None:
        return Check(name, False, blocking, f"{cfg.bin} was not found on PATH"), False
    return Check(name, True, blocking, path), True


def _codex_limits(cfg: AgentConfig, now: float) -> Check:
    name = "codex limits"
    try:
        result = codex.read_limits(cfg.bin)
        verdict = codex.evaluate(result, cfg.threshold_pct, now)
    except Exception as exc:
        return Check(name, False, True, f"cannot read limits (not logged in?): {exc}")
    avail, usage = verdict.avail, verdict.avail.usage
    limits = result.get("rateLimits") if isinstance(result, dict) else None
    parts = []
    for window in ("primary", "secondary"):
        raw = limits.get(window) if isinstance(limits, dict) else None
        if isinstance(raw, dict):
            used = _pct(usage.get(f"{window}_pct"))
            parts.append(f"{window} {used} used, resets {_when(raw.get('resetsAt'))}")
    if usage.get("balance") is not None:
        parts.append(f"credit balance {usage['balance']}")
    if usage.get("plan"):
        parts.append(f"plan {usage['plan']}")
    detail = "; ".join(parts) or "no windows reported"
    if avail.ok:
        return Check(name, True, True, detail)
    # Not usable right now. A refusal is the quota doing its job; data baton cannot read (a bad
    # balance, a missing window) means it could not guard a round, which is a blocking problem.
    return Check(name, False, verdict.blind, f"{detail}; not usable now: {avail.reason}")


def _antigravity_limits(cfg: AgentConfig, now: float) -> Check:
    name = "antigravity limits"

    def read(command: str) -> str | Exception:
        try:
            return antigravity.read_text(cfg.bin, command)
        except Exception as exc:
            return exc

    reading = antigravity.Reading(usage=read("/usage"), credits=read("/credits"))
    verdict = antigravity.evaluate(reading, cfg.threshold_pct, now)
    avail = verdict.avail
    parts = []
    for w in avail.usage.get("windows", []):
        label = str(w["label"]).removesuffix(" Remaining")
        parts.append(
            f"{w['group']} / {label}: {_pct(w['used_pct'])} used, resets {_when(w['resets_at'])}"
        )
    if avail.usage.get("balance") is not None:
        parts.append(f"credit balance {avail.usage['balance']}")
    detail = "; ".join(parts) or "nothing could be read"
    if avail.ok:
        return Check(name, True, True, detail)
    return Check(name, False, verdict.blind, f"{detail}; not usable now: {avail.reason}")


def _usd(value: Decimal) -> str:
    return format(value.normalize() if value == value.to_integral_value() else value, "f")


NOT_CONFIGURED = "not configured (optional): rounds use the other agents only"


def _claude_unconfigured(cfg: AgentConfig) -> bool:
    """True when Claude is enabled but there is no key file: it was never set up, and Claude is
    optional. A key file in any state (even with the wrong mode) means a setup was begun."""
    return not os.path.lexists(Path(cfg.key_file).expanduser())


def _claude_checks(cfg: AgentConfig, env: Mapping[str, str], blocking: bool) -> list[Check]:
    """Everything about the Claude API agent that can be read without spending: its binary, its
    key file (never its content), the spend ledger against the ceiling, the optional cost report,
    and the Console settings that only the account owner can verify.

    `blocking` is true only when no other agent in the order could run instead: Claude is an
    optional agent, and a missing key must not stop the others.
    """
    binary, usable = _binary(cfg, env, blocking)
    checks = [binary]
    if not cfg.enabled:
        return checks
    try:
        claude_api.read_key(Path(cfg.key_file))
        checks.append(Check("claude_api key", True, blocking, f"{cfg.key_file} (mode 600)"))
    except claude_api.KeyFileError as exc:
        checks.append(Check("claude_api key", False, blocking, str(exc)))
    ledger = claude_api.ledger_for(cfg)
    ceiling = ledger.ceiling
    floor = None
    if cfg.admin_key_file:
        try:
            report = claude_api.make_cost_report(cfg, ledger)
            assert report is not None
            floor = report()
            checks.append(
                Check("claude_api cost report", True, blocking, f"organization spent {_usd(floor)}")
            )
        except Exception as exc:
            checks.append(Check("claude_api cost report", False, blocking, str(exc)))
            floor = None
    name = "claude_api spend"
    try:
        spent = ledger.spent(floor)
        exhausted = ledger.exhausted()
    except LedgerError as exc:
        checks.append(Check(name, False, blocking, f"ledger unreadable: {exc}"))
    else:
        left = ceiling - spent
        detail = (
            f"spent {_usd(spent)} of the {_usd(ceiling)} USD ceiling, {_usd(left)} left "
            f"(budget {_usd(cfg.monthly_budget_usd)} minus {cfg.reserve_pct}% reserve); "
            f"period resets {_when(ledger.next_period())}"
        )
        if exhausted:
            detail += "; the Console reported no credit left"
        ok = left >= MIN_GRANT_USD and not exhausted
        checks.append(Check(name, ok, False, detail if ok else f"{detail}; not usable now"))
    checks.append(
        Check(
            "claude_api console",
            True,
            False,
            "reminder, since neither can be checked with the key: in the Console turn "
            "auto-reload off and hold no purchased credits (spending would continue after the "
            "promotional credit ends), and put the key in a dedicated workspace with a monthly "
            f"spend limit of {_usd(ceiling)} USD",
        )
    )
    return checks


def _interactive() -> Check:
    name = "interactive codex"
    try:
        found = interactive_codex(list_processes())
    except Exception as exc:
        return Check(name, False, False, f"could not list processes: {exc}")
    if not found:
        return Check(name, True, False, "no other codex session is open")
    pids = ", ".join(str(proc.pid) for proc in found)
    return Check(
        name,
        False,
        False,
        f"{len(found)} other codex session(s) open (pid {pids}). They share this account's "
        "limits and credits, and baton cannot stop them.",
    )


def _superpowers() -> Check:
    """Superpowers is required: a goal waits, and runs no round, until it is found."""
    name = "superpowers"
    try:
        found = superpowers.find_skills(load_superpowers())
    except GoalError as exc:
        return Check(name, False, True, f"settings: {exc}")
    if found is not None:
        try:
            superpowers.Skills.load(found)
        except (superpowers.SkillsError, OSError) as exc:
            return Check(name, False, True, f"{found}: {exc}")
        return Check(name, True, False, str(found))
    return Check(
        name,
        False,
        True,
        "not found: a goal waits, and runs no round, until it is installed. Install it with: "
        f"{superpowers.INSTALL_COMMAND}",
    )


def _memory(goal: Goal | None) -> Check:
    """Long-term memory is always optional: whatever is wrong with it, rounds run without it."""
    name = "memory"
    if goal is not None and not goal.memory:
        return Check(name, True, False, "off (memory = false in the goal)")
    try:
        cfg = load_memory()
    except GoalError as exc:
        return Check(name, False, False, f"settings ignored, rounds run without memory: {exc}")
    if not cfg.enabled:
        why = (
            "BATON_MEMORY=off"
            if os.environ.get("BATON_MEMORY", "").strip().lower() == "off"
            else "enabled = false in [memory]"
        )
        return Check(name, True, False, f"off ({why})")
    mem = Memory(cfg, goal.workdir if goal is not None else Path.cwd(), "doctor")
    # A binary that is missing, or an old server in the way of the configured port, is a warning:
    # either way the rounds go on without memory.
    return Check(name, mem.binary() is not None and not mem.conflict(), False, mem.status())


def run_checks(goal: Goal | None, env: Mapping[str, str]) -> list[Check]:
    """All checks, in display order. Never starts a round."""
    checks = _environment(env)
    now = time.time()
    if goal is not None:
        agents = {a: goal.agents[a] for a in AGENT_NAMES}
        # An agent outside `order` never runs, so its problems cannot block.
        wanted = {a for a in AGENT_NAMES if a in goal.order}
        workdir = Path(goal.workdir)
        ok = workdir.is_dir()
        checks.append(
            Check("workdir", ok, True, str(workdir) if ok else f"{workdir} is not a directory")
        )
        if not goal.verify_command:
            checks.append(
                Check("verify", True, False, "no verify command: the goal ends after one round")
            )
    else:
        agents = {a: default_agent(a) for a in AGENT_NAMES}
        wanted = set(AGENT_NAMES)
    for name in AGENT_NAMES:
        cfg = agents[name]
        blocking = name in wanted
        if name == "claude_api":
            others = any(agents[a].enabled for a in wanted if a != name)
            if cfg.enabled and others and _claude_unconfigured(cfg):
                checks.append(Check("claude_api", True, False, NOT_CONFIGURED))
                continue
            checks += _claude_checks(cfg, env, blocking and not others)
            continue
        binary, usable = _binary(cfg, env, blocking)
        checks.append(binary)
        if not cfg.enabled:
            continue
        if usable:
            reader = _codex_limits if name == "codex" else _antigravity_limits
            limits = reader(cfg, now)
            limits.blocking = limits.blocking and blocking
            checks.append(limits)
        else:
            checks.append(Check(f"{name} limits", False, blocking, "skipped: no binary"))
        if name == "codex":
            checks.append(_interactive())
    checks.append(_superpowers())
    checks.append(_memory(goal))
    return checks

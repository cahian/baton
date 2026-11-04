"""`baton install-service`: keep `baton serve` running under launchd.

launchd loads no shell profile, so everything the service needs is resolved here, from the shell
that installs it, and written down as absolute paths.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from baton.config import AGENT_NAMES, default_agent
from baton.gateway import SHUTDOWN_S
from baton.proc import KILL_GRACE_S
from baton.store import state_base

LABEL = "local.baton.serve"
LOG_NAME = "serve.log"
# Agents that are scripts need more than their own directory: codex runs on `node`.
RUNTIMES = ("node",)
# What a child of the service needs whatever the agents are: sh, ps, git, caffeinate.
SYSTEM_PATH = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
# launchd's SIGKILL must not cut the shutdown short: every job gets SHUTDOWN_S to end, and a job
# that did not then has its group killed, with its own grace period.
EXIT_TIMEOUT_S = int(SHUTDOWN_S + KILL_GRACE_S + 5)
THROTTLE_S = 30
# Variables that decide where this baton keeps things, so the service and the shell agree.
PASSED_ON = ("BATON_STATE_DIR", "BATON_CONFIG", "BATON_OPENCODE_DB")
LAUNCHCTL_TIMEOUT_S = EXIT_TIMEOUT_S + 20
BOOTSTRAP_TRIES = 5
BOOTSTRAP_PAUSE_S = 1.0
# A service that was just booted out stays in its domain until it has stopped, which launchd allows
# ExitTimeOut for. Bootout can return before that.
BOOTOUT_TRIES = EXIT_TIMEOUT_S + 15


class ServiceError(Exception):
    """The service could not be installed. The message says why, in plain words."""


@dataclass(frozen=True)
class Installed:
    path: Path
    agents: dict[str, str]
    missing: dict[str, str]
    runtimes: dict[str, str]
    path_variable: str


def _which(command: str) -> str | None:
    found = shutil.which(command)
    return os.path.abspath(found) if found else None


def resolve_agents() -> tuple[dict[str, str], dict[str, str]]:
    """Each enabled agent's binary as this shell finds it, and the commands that were not found.
    A broken defaults file raises GoalError."""
    found: dict[str, str] = {}
    missing: dict[str, str] = {}
    for name in AGENT_NAMES:
        cfg = default_agent(name)
        if not cfg.enabled:
            continue
        path = _which(cfg.bin)
        if path is None:
            missing[name] = cfg.bin
        else:
            found[name] = path
    return found, missing


def build_path(binaries: list[str]) -> str:
    """PATH for the service: the directory of each binary, then the system's, without repeats."""
    return os.pathsep.join(dict.fromkeys([*(str(Path(b).parent) for b in binaries), *SYSTEM_PATH]))


def baton_command() -> list[str]:
    """The absolute command that runs this baton."""
    found = _which("baton")
    return [found] if found else [sys.executable, "-m", "baton.cli"]


def render_plist(port: int, path_variable: str) -> bytes:
    log = str(state_base() / LOG_NAME)
    environment = {"PATH": path_variable, "HOME": str(Path.home())}
    environment.update({k: os.environ[k] for k in PASSED_ON if k in os.environ})
    return plistlib.dumps(
        {
            "Label": LABEL,
            "ProgramArguments": [*baton_command(), "serve", "--port", str(port)],
            "EnvironmentVariables": environment,
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": THROTTLE_S,
            "ExitTimeOut": EXIT_TIMEOUT_S,
            "StandardOutPath": log,
            "StandardErrorPath": log,
        }
    )


def plist_path() -> Path:
    return Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"


def _launchctl(launchctl: str, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [launchctl, *args],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=LAUNCHCTL_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ServiceError(f"launchctl {args[0]} could not run: {exc}") from None


def load_service(launchctl: str, plist: Path) -> None:
    """Load `plist`, replacing a loaded copy, so installing twice is the same as once."""
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{LABEL}"
    tries = BOOTSTRAP_TRIES
    if _launchctl(launchctl, "print", target).returncode == 0:
        _launchctl(launchctl, "bootout", target)
        tries = BOOTOUT_TRIES
    problem = ""
    for attempt in range(tries):
        if attempt:
            time.sleep(BOOTSTRAP_PAUSE_S)
        done = _launchctl(launchctl, "bootstrap", domain, str(plist))
        if done.returncode == 0:
            return
        problem = (done.stderr or done.stdout).strip() or f"exit {done.returncode}"
    raise ServiceError(f"launchctl bootstrap failed: {problem}")


def install(port: int) -> Installed:
    """Write the plist and load it. Raises ServiceError with nothing changed when there is no
    launchctl or no agent to run."""
    launchctl = _which("launchctl")
    if launchctl is None:
        raise ServiceError("launchctl was not found: install-service needs macOS")
    agents, missing = resolve_agents()
    if not agents:
        names = ", ".join(sorted(missing.values())) or "every agent is disabled"
        raise ServiceError(f"no agent binary was found on this shell's PATH ({names})")
    runtimes = {name: path for name in RUNTIMES if (path := _which(name)) is not None}
    path_variable = build_path([*agents.values(), *runtimes.values()])

    plist = plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    base = state_base()
    base.mkdir(parents=True, exist_ok=True, mode=0o700)  # launchd opens the log but makes no dirs
    plist.write_bytes(render_plist(port, path_variable))
    load_service(launchctl, plist)
    return Installed(plist, agents, missing, runtimes, path_variable)

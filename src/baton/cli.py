"""The `baton` command line."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from baton.adapters.antigravity import AntigravityAdapter
from baton.adapters.base import Adapter
from baton.adapters.claude_api import ClaudeApiAdapter, ledger_for, make_cost_report
from baton.adapters.codex import CodexAdapter
from baton.config import AgentConfig, Goal, GoalError, load_goal
from baton.doctor import run_checks
from baton.gateway import DEFAULT_PORT, opencode_config, serve
from baton.runner import run_goal
from baton.store import AlreadyRunning, StateError, Store, state_dir

STATUS_EVENTS = 8


def build_adapters(goal: Goal) -> dict[str, Adapter]:
    """One adapter per enabled agent of the goal."""

    def claude_api(cfg: AgentConfig) -> ClaudeApiAdapter:
        ledger = ledger_for(cfg)
        return ClaudeApiAdapter(cfg, ledger, make_cost_report(cfg, ledger))

    makers: dict[str, Callable] = {
        "codex": CodexAdapter,
        "antigravity": AntigravityAdapter,
        "claude_api": claude_api,
    }
    return {name: makers[name](cfg) for name, cfg in goal.agents.items() if cfg.enabled}


def _err(message: str) -> None:
    print(f"baton: {message}", file=sys.stderr)


# run


class Terminated(BaseException):
    """SIGTERM or SIGINT arrived. A BaseException, so no `except Exception` in the loop eats it."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


class _Signals:
    """Turns the first SIGTERM, SIGINT or SIGHUP into `Terminated` and ignores the rest, so the
    cleanup that stops the agent's process group cannot itself be cut short."""

    SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)  # HUP: the terminal was closed

    def __init__(self) -> None:
        self.armed = False
        self.fired = False
        self._previous: dict[int, object] = {}

    def __enter__(self) -> _Signals:
        for sig in self.SIGNALS:
            # `nohup baton run` starts with SIGHUP ignored on purpose: keep it that way.
            if sig == signal.SIGHUP and signal.getsignal(sig) == signal.SIG_IGN:
                continue
            self._previous[sig] = signal.signal(sig, self._handle)
        self.armed = True
        return self

    def __exit__(self, *exc: object) -> None:
        self.armed = False
        for sig, previous in self._previous.items():
            signal.signal(sig, previous)  # type: ignore[arg-type]

    def _handle(self, signum: int, frame: object) -> None:
        if not self.armed or self.fired:
            return
        self.fired = True
        raise Terminated(signum)


def _hold_awake() -> subprocess.Popen | None:
    """`caffeinate -is -w <pid>` on macOS, so the machine does not sleep mid-goal."""
    if sys.platform != "darwin":
        return None
    exe = shutil.which("caffeinate")
    if exe is None:
        return None
    try:
        return subprocess.Popen(
            [exe, "-is", "-w", str(os.getpid())],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return None


def _release(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _forget_group(store: Store) -> None:
    """After an orderly shutdown the recorded group is dead; do not leave it for the next start
    to signal. A group that somehow survived stays recorded. Written under the lock, like every
    other write of the runner's state; if another runner has taken it by now, it owns the file."""
    try:
        with store.lock():
            state = store.load_state()
            group = state.get("group")
            if isinstance(group, int) and not isinstance(group, bool) and _group_alive(group):
                return
            state.update(group=None, group_start=None)
            store.save_state(state)
    except Exception:
        pass


def cmd_run(args: argparse.Namespace) -> int:
    goal = load_goal(Path(args.goal))
    store = Store(state_dir(goal.name))
    adapters = build_adapters(goal)
    # The stale group from a previous run is killed by run_goal once it holds the lock: killing
    # it here, before the lock, would shoot the live agent of a runner that is merely refusing us.
    awake = _hold_awake()
    try:
        with _Signals():
            try:
                return run_goal(goal, adapters, store)
            except Terminated as stop:
                name = signal.Signals(stop.signum).name
                try:
                    store.emit("terminated", signal=name)
                except Exception:
                    pass
                _forget_group(store)
                return 128 + stop.signum
    except AlreadyRunning as exc:
        _err(str(exc))
        return 1
    finally:
        _release(awake)


# status, say, stop


def _when(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _runner_pid(root: Path) -> int | None:
    """The pid of the runner that holds this goal's lock, or None when there is none.

    Reads the pid the runner wrote into the lock file and checks that the process is alive and is
    a baton. It never touches the lock itself: even a momentary probe could make a runner that is
    starting at that instant be refused as "already running".
    """
    try:
        pid = int((root / "lock").read_text().strip())
    except (OSError, ValueError):
        return None
    if pid <= 0 or pid == os.getpid():
        return None
    try:
        command = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return pid if "baton" in command else None


def _event_line(event: dict) -> str:
    stamp = datetime.fromtimestamp(float(event.get("ts", 0)), UTC).strftime("%H:%M:%S")
    skip = {"ts", "seq", "kind", "text", "tail", "agents"}
    details = " ".join(
        f"{k}={v}" for k, v in event.items() if k not in skip and v not in (None, "")
    )
    return f"  {stamp} {event.get('kind')} {details}".rstrip()


def cmd_status(args: argparse.Namespace) -> int:
    goal = load_goal(Path(args.goal))
    root = state_dir(goal.name)
    if not root.exists():
        print(f"{goal.name}: no state yet (it has never run)")
        return 0
    store = Store(root)
    pid = _runner_pid(root)
    print(f"goal {goal.name} ({goal.path})")
    print(f"workdir: {goal.workdir}")
    if pid is None:
        runner = "not running"
    else:
        runner = f"running (pid {pid})" if pid else "running"
    print(f"runner: {runner}")
    try:
        state = store.load_state()
    except StateError as exc:
        print(f"state: {exc}")
        state = {}
    now = time.time()
    current = state.get("current")
    print(f"round {state.get('round', 0)}, current agent: {current or 'none yet'}")
    agents = state.get("agents")
    for name, info in (agents if isinstance(agents, dict) else {}).items():
        if not isinstance(info, dict):
            continue
        until = info.get("until")
        cooling = isinstance(until, int | float) and until > now
        tag = " (current)" if name == current else ""
        if cooling:
            left = int(until - now)
            line = f"cooldown until {_when(until)} ({left // 3600}h{left % 3600 // 60:02d}m left)"
        else:
            line = "ready"
        print(
            f"  {name}{tag}: {line}, limit_n={info.get('limit_n', 0)}, "
            f"transient_n={info.get('transient_n', 0)}"
        )
        if info.get("reason"):
            print(f"    reason: {info['reason']}")
        if info.get("usage"):
            print(f"    usage: {json.dumps(info['usage'], sort_keys=True)}")
    if state.get("group"):
        print(f"active group: {state['group']}")
    request = store.stop_request()
    if request:
        print(f"stop requested: {request}")
    if (root / "interrupt").exists():
        print("interrupt pending")
    recent = store.events()[-STATUS_EVENTS:]
    if recent:
        print("last events:")
        for event in recent:
            print(_event_line(event))
    return 0


def cmd_say(args: argparse.Namespace) -> int:
    goal = load_goal(Path(args.goal))
    text = " ".join(args.text).strip()
    if not text:
        _err("the note is empty")
        return 2
    store = Store(state_dir(goal.name))
    store.say(text)
    if args.now:
        store.request_interrupt()
        print("note added; the current round is interrupted")
    else:
        print("note added; it is delivered with the next round")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    goal = load_goal(Path(args.goal))
    store = Store(state_dir(goal.name))
    store.request_stop(now=args.now)
    print("stop requested" + (" now" if args.now else " after the current round"))
    return 0


# doctor


def cmd_doctor(args: argparse.Namespace) -> int:
    goal = load_goal(Path(args.goal)) if args.goal else None
    checks = run_checks(goal, os.environ)
    for c in checks:
        tag = "ok" if c.ok else ("FAIL" if c.blocking else "warn")
        print(f"[{tag:>4}] {c.name}: {c.detail}")
    blocked = [c for c in checks if c.blocking and not c.ok]
    if blocked:
        print(f"{len(blocked)} blocking problem(s)")
        return 1
    return 0


# gateway


def cmd_serve(args: argparse.Namespace) -> int:
    awake = _hold_awake()
    try:
        serve(args.port, build_adapters)
    except OSError as exc:
        _err(f"cannot serve on 127.0.0.1:{args.port}: {exc.strerror or exc}")
        return 1
    finally:
        _release(awake)
    return 0


def cmd_opencode_config(args: argparse.Namespace) -> int:
    print(json.dumps(opencode_config(args.port), indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="baton", description="Relay one coding goal between Claude, Antigravity and Codex."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="check binaries, logins, stray keys and live limits")
    p.add_argument("goal", nargs="?", help="goal file (optional)")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("run", help="run one goal in the foreground")
    p.add_argument("goal", help="goal file")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("serve", help="the OpenCode gateway")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("status", help="one-shot status of a goal")
    p.add_argument("goal", help="goal file")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("say", help="leave a note for the next round")
    p.add_argument("goal", help="goal file")
    p.add_argument("--now", action="store_true", help="also interrupt the current round")
    p.add_argument("text", nargs="+", help="the note")
    p.set_defaults(func=cmd_say)

    p = sub.add_parser("stop", help="request a stop")
    p.add_argument("goal", help="goal file")
    p.add_argument("--now", action="store_true", help="kill the current round")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("opencode-config", help="print the provider JSON for opencode.json")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.set_defaults(func=cmd_opencode_config)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except GoalError as exc:
        _err(str(exc))
        return 2


if __name__ == "__main__":
    sys.exit(main())

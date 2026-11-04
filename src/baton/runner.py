"""The run loop: gate, rotate, run one round, snapshot, repeat."""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from baton.adapters.base import (
    AUTH,
    COMPLETED,
    CREDIT_GUARD,
    TRANSIENT,
    Adapter,
    RoundContext,
    RoundResult,
)
from baton.config import Goal
from baton.proc import kill_group
from baton.scheduler import Scheduler
from baton.store import Store

GATE_TAIL_CHARS = 4000
ROUND_TAIL_CHARS = 8000
HANDOFF_MAX_CHARS = 16000
# Waits are sliced so a stop request is noticed within this many seconds.
STEP_S = 5
ERROR_SLEEP_S = 60
GIT_TIMEOUT_S = 120

RULES = """\
Rules:
- You are one shift of a relay. Another agent may continue after you at any moment.
- Commit working progress often, in small steps.
- Keep `.baton/HANDOFF.md` current: what was tried, what was learned, what comes next.
- Do not declare success. Only the verify command decides."""


def build_prompt(
    goal: Goal,
    gate_tail: str,
    notes: str,
    handoff: str,
    round_tail: str,
    resumed: bool,
) -> str:
    """The prompt for one round.

    A fresh round gets the whole briefing. A resumed one already has it in its own history, so
    it only gets what changed: the gate output, the previous round's tail, notes and handoff.
    """
    parts: list[str] = []
    if resumed:
        parts.append("Your turn again. This is what changed since your last round.")
    else:
        parts.append(f"Objective:\n{goal.objective.strip()}")
        if goal.verify_command:
            parts.append(
                "Verify command (exit 0 means the objective is met):\n"
                f"{goal.verify_command.strip()}"
            )
        else:
            parts.append("There is no verify command: the goal ends when a round completes.")
    gate_tail = gate_tail.strip()
    if gate_tail:
        parts.append(f"Output of the last verify run (tail):\n{gate_tail[-GATE_TAIL_CHARS:]}")
    round_tail = round_tail.strip()
    if round_tail:
        parts.append(f"Tail of the previous round:\n{round_tail[-ROUND_TAIL_CHARS:]}")
    notes = notes.strip()
    if notes:
        parts.append(f"Notes from the person:\n{notes}")
    handoff = handoff.strip()
    if handoff:
        parts.append(f"Handoff (.baton/HANDOFF.md):\n{handoff}")
    if not resumed:
        parts.append(RULES)
    return "\n\n".join(parts) + "\n"


# gate


def _tail_of(fh, chars: int) -> str:
    size = fh.seek(0, os.SEEK_END)
    fh.seek(max(0, size - chars * 4))
    return fh.read().decode("utf-8", errors="replace")[-chars:]


def run_gate(goal: Goal) -> tuple[bool, str]:
    """Run the verify command. Passed only on exit 0; anything else, even a timeout or a command
    that cannot start, is not passed. Never raises."""
    command = goal.verify_command
    if not command:
        return False, "no verify command is configured"
    try:
        with tempfile.TemporaryFile() as out:
            proc = subprocess.Popen(
                command,
                shell=True,
                cwd=goal.workdir,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                code: int | None = proc.wait(timeout=goal.verify_timeout_s)
                note = f"[exit code {code}]"
            except subprocess.TimeoutExpired:
                code = None
                note = f"[verify timed out after {goal.verify_timeout_s} s]"
                _signal_group(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
                _signal_group(proc.pid, signal.SIGKILL)
                proc.wait()
            tail = _tail_of(out, GATE_TAIL_CHARS)
    except Exception as exc:  # the gate failing to run is never a pass
        return False, f"verify could not run: {exc}"
    return code == 0, f"{tail.rstrip()}\n{note}".strip()


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


# workdir and snapshots


def _git(workdir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=workdir,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=GIT_TIMEOUT_S,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def prepare_workdir(goal: Goal) -> None:
    """Create `.baton/` and keep it out of git."""
    (goal.workdir / ".baton").mkdir(parents=True, exist_ok=True)
    _exclude_baton(goal.workdir)


def _exclude_baton(workdir: Path) -> None:
    try:
        found = _git(workdir, "rev-parse", "--git-path", "info/exclude")
        if found.returncode != 0 or not found.stdout.strip():
            return
        path = workdir / found.stdout.strip()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []
        if any(line.strip() in (".baton", ".baton/", "/.baton", "/.baton/") for line in lines):
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            if lines and not path.read_text(encoding="utf-8").endswith("\n"):
                fh.write("\n")
            fh.write(".baton/\n")
    except (OSError, subprocess.SubprocessError):
        pass


def snapshot_work(goal: Goal, n: int, agent: str) -> bool:
    """Commit uncommitted work locally; True only when a commit was made. Never raises or pushes."""
    if not goal.snapshot:
        return False
    try:
        top = _git(goal.workdir, "rev-parse", "--show-toplevel")
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != goal.workdir.resolve():
            return False  # not a repository of its own: leave other people's history alone
        _exclude_baton(goal.workdir)
        status = _git(goal.workdir, "status", "--porcelain")
        if status.returncode != 0 or not status.stdout.strip():
            return False
        if _git(goal.workdir, "add", "-A").returncode != 0:
            return False
        message = f"baton: snapshot round {n} ({agent})"
        return _git(goal.workdir, "commit", "--no-verify", "-m", message).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# the loop


def run_goal(
    goal: Goal,
    adapters: Mapping[str, Adapter],
    store: Store,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run `goal` until the gate passes or a stop is honoured. Returns 0 either way.

    Holds the goal's lock for the duration; raises AlreadyRunning when another runner has it.
    Everything else that goes wrong is logged as an `error` event, followed by a 60 s sleep.
    """
    with store.lock():
        return _Run(goal, adapters, store, clock, sleep).run()


class _Run:
    def __init__(
        self,
        goal: Goal,
        adapters: Mapping[str, Adapter],
        store: Store,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        self.goal = goal
        self.adapters = adapters
        self.store = store
        self.clock = clock
        self.sleep = sleep
        self.ready = False
        self.sched: Scheduler | None = None
        self.sessions: dict[str, str] = {}
        self.round_no = 0
        self.round_tail = ""
        self.gate_tail = ""
        self.group: int | None = None
        self.need_gate = True
        self.prev_agent: str | None = None
        self.carried_notes = ""

    # plumbing

    def emit(self, kind: str, **fields: object) -> None:
        try:
            self.store.emit(kind, **fields)
        except Exception:  # a full disk must not stop the work
            pass

    def stop_requested(self) -> str:
        try:
            return self.store.stop_request()
        except Exception:
            return ""

    def nap_until(self, until: float) -> None:
        """Sleep in steps of at most STEP_S, returning early once a stop is requested."""
        remaining = until - self.clock()
        while remaining > 0 and self.clock() < until:
            if self.stop_requested():
                return
            step = min(STEP_S, remaining)
            self.sleep(step)
            remaining -= step

    def runner_state(self) -> dict:
        return {
            "round": self.round_no,
            "workdir": str(self.goal.workdir),
            "sessions": dict(self.sessions),
            "round_tail": self.round_tail,
            "group": self.group,
        }

    def persist(self) -> None:
        """Write the runner's part of state.json. Raises StateError when it cannot be read."""
        state = self.store.load_state()
        state.update(self.runner_state())
        self.store.save_state(state)

    def persist_softly(self) -> None:
        try:
            self.persist()
        except Exception as exc:
            self.emit("error", error=f"could not save state: {exc}")

    # the loop

    def run(self) -> int:
        while True:
            try:
                code = self.step()
                if code is not None:
                    return code
            except Exception as exc:
                self.emit("error", error=f"{type(exc).__name__}: {exc}")
                self.nap_until(self.clock() + ERROR_SLEEP_S)

    def start(self) -> None:
        goal = self.goal
        state = self.store.load_state()
        sched = Scheduler(goal.order, self.adapters, self.store, self.clock)
        group = state.get("group")
        if isinstance(group, int) and not isinstance(group, bool) and group > 1:
            if group != os.getpgrp():
                kill_group(group)
                self.emit("alert", message="stopped a process group left by a previous run")
        self.group = None
        same_dir = state.get("workdir") == str(goal.workdir)
        saved = state.get("sessions")
        if same_dir and isinstance(saved, dict):
            self.sessions = {
                a: s for a, s in saved.items() if isinstance(s, str) and s and a in goal.agents
            }
        else:
            self.sessions = {}
            if saved:
                self.emit("alert", message="working directory changed: session ids cleared")
        rounds = state.get("round")
        self.round_no = rounds if isinstance(rounds, int) and rounds > 0 else 0
        tail = state.get("round_tail")
        self.round_tail = tail if isinstance(tail, str) else ""
        prepare_workdir(goal)
        self.sched = sched
        self.persist()
        self.ready = True

    def step(self) -> int | None:
        if not self.ready:
            self.start()
        goal = self.goal
        if self.need_gate and goal.verify_command:
            passed, self.gate_tail = run_gate(goal)
            self.need_gate = False
            self.emit("gate", passed=passed, tail=self.gate_tail)
            if passed:
                self.emit("finished", reason="verify passed", round=self.round_no)
                return 0
        self.need_gate = False
        if self.stop_requested():
            self.store.clear_stop()
            self.emit("stopped", round=self.round_no)
            return 0
        assert self.sched is not None
        adapter = self.sched.pick()
        self.sched.save()
        if adapter is None:
            until = self.sched.next_wake()
            self.emit(
                "wait",
                until=until,
                seconds=max(0.0, until - self.clock()),
                agents=self.sched.snapshot()["agents"],
            )
            self.nap_until(until)
            return None
        finishing = self.run_round(adapter)
        if finishing:
            self.emit("finished", reason="round completed", round=self.round_no)
            return 0
        return None

    # one round

    def run_round(self, adapter: Adapter) -> bool:
        """Run one round for `adapter`. True when it ends the goal (no verify command)."""
        assert self.sched is not None
        goal, store = self.goal, self.store
        agent = adapter.name
        self.need_gate = True
        n = self.round_no + 1
        self.round_no = n
        try:
            self.persist()
        except Exception:
            self.round_no = n - 1
            raise
        try:
            store.take_interrupt()  # one that arrived between rounds has nothing left to end
        except OSError:
            pass
        self.carried_notes += store.take_inbox()
        notes = self.carried_notes
        handoff = self.read_handoff()
        sid = self.sessions.get(agent)

        buf = ""
        killed = False
        interrupted = False

        def on_output(line: str) -> None:
            nonlocal buf
            try:
                text = adapter.render(line)
            except Exception:
                text = None
            if text:
                buf = (buf + text + "\n")[-ROUND_TAIL_CHARS:]
                self.emit("output", agent=agent, round=n, text=text)

        def should_kill() -> bool:
            nonlocal killed, interrupted
            if killed:
                return True
            try:
                if store.stop_request() == "now":
                    killed = True
                elif store.take_interrupt():
                    killed = interrupted = True
            except OSError:
                pass
            return killed

        def on_spawn(pgid: int) -> None:
            self.group = pgid
            self.persist_softly()

        def on_session(session: str) -> None:
            self.sessions[agent] = session
            self.persist_softly()

        def attempt(session: str | None) -> RoundResult:
            prompt = build_prompt(
                goal, self.gate_tail, notes, handoff, self.round_tail, resumed=session is not None
            )
            ctx = RoundContext(
                workdir=goal.workdir,
                log_path=store.round_log(n, agent),
                round_max_s=goal.round_max_s,
                idle_s=goal.idle_s,
                on_output=on_output,
                should_kill=should_kill,
                on_spawn=on_spawn,
                session_id=session,
                on_session=on_session,
            )
            self.emit(
                "round_start",
                round=n,
                agent=agent,
                resumed=session is not None,
                previous=self.prev_agent,
            )
            self.carried_notes = ""  # delivered; from here the inbox holds only newer notes
            try:
                return adapter.run_round(prompt, ctx)
            except Exception as exc:
                self.emit("error", error=f"{agent} round failed: {type(exc).__name__}: {exc}")
                return RoundResult(TRANSIENT, reason=f"adapter error: {type(exc).__name__}")

        started = self.clock()
        result = attempt(sid)
        if result.resume_failed and sid is not None:
            self.sessions.pop(agent, None)
            self.emit("alert", agent=agent, message="resume failed: running the round fresh")
            result = attempt(None)

        self.group = None
        self.round_tail = buf
        snapshotted = snapshot_work(goal, n, agent)
        self.sched.record(agent, result)
        if result.outcome in (CREDIT_GUARD, AUTH):
            self.emit("alert", agent=agent, outcome=result.outcome, reason=result.reason)
        self.prev_agent = agent
        self.emit(
            "round_end",
            round=n,
            agent=agent,
            outcome=result.outcome,
            reason=result.reason,
            until=result.until,
            seconds=round(self.clock() - started, 1),
            interrupted=interrupted,
            snapshot=snapshotted,
        )
        self.persist_softly()
        return not goal.verify_command and result.outcome == COMPLETED and not killed

    def read_handoff(self) -> str:
        try:
            text = (self.goal.workdir / ".baton" / "HANDOFF.md").read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            return ""
        return text[-HANDOFF_MAX_CHARS:]

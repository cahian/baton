"""The run loop: gate, rotate, run one round, snapshot, repeat."""

from __future__ import annotations

import os
import re
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
from baton.proc import group_is_ours, kill_group, process_start
from baton.scheduler import Scheduler
from baton.store import Store

GATE_TAIL_CHARS = 4000
ROUND_TAIL_CHARS = 8000
HANDOFF_MAX_CHARS = 16000
# Waits are sliced so a stop request is noticed within this many seconds.
STEP_S = 5
ERROR_SLEEP_S = 60
GIT_TIMEOUT_S = 120
COMMITS_MAX_CHARS = 2000
# A round that completes sooner than this, without the goal being met, is followed by a pause of
# this length, so an agent that exits at once cannot burn quota in a tight loop.
MIN_ROUND_S = 30
GATE_POLL_S = 0.5

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
    since_commits: str = "",
) -> str:
    """The prompt for one round.

    A fresh round gets the whole briefing. A resumed one already has it in its own history, so
    it only gets what changed: the commits others made since, the gate output, the previous
    round's tail, notes and handoff.
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
    since_commits = since_commits.strip()
    if since_commits:
        parts.append(f"Commits made since your last round (newest first):\n{since_commits}")
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


def run_gate(goal: Goal, should_kill: Callable[[], bool] | None = None) -> tuple[bool, str]:
    """Run the verify command. Passed only on exit 0; anything else, even a timeout, a stop or a
    command that cannot start, is not passed. `should_kill` is polled while it runs and ends it.
    Raises nothing but KeyboardInterrupt and SystemExit, and the command's whole process group
    is gone by then."""
    command = goal.verify_command
    if not command:
        return False, "no verify command is configured"
    proc: subprocess.Popen | None = None
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
            deadline = time.monotonic() + goal.verify_timeout_s
            code: int | None = None
            note = ""
            while True:
                try:
                    code = proc.wait(timeout=GATE_POLL_S)
                    note = f"[exit code {code}]"
                    # Whatever the command left running in its session goes with it.
                    _signal_group(proc.pid, signal.SIGKILL)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if should_kill is not None and should_kill():
                    note = "[verify stopped]"
                elif time.monotonic() >= deadline:
                    note = f"[verify timed out after {goal.verify_timeout_s} s]"
                else:
                    continue
                _end_gate(proc)
                break
            tail = _tail_of(out, GATE_TAIL_CHARS)
    except Exception as exc:  # the gate failing to run is never a pass
        if proc is not None:
            _end_gate(proc)
        return False, f"verify could not run: {exc}"
    except BaseException:
        if proc is not None:
            _end_gate(proc)
        raise
    return code == 0, f"{tail.rstrip()}\n{note}".strip()


def _end_gate(proc: subprocess.Popen) -> None:
    """SIGTERM the gate's group, SIGKILL it after 3 s (or at once once the leader is gone)."""
    _signal_group(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    _signal_group(proc.pid, signal.SIGKILL)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


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
    """Commit uncommitted work locally; True only when a commit was made. Never raises or pushes.

    A workdir inside a larger repository is fine: only changes under the workdir are committed.
    """
    if not goal.snapshot:
        return False
    try:
        inside = _git(goal.workdir, "rev-parse", "--is-inside-work-tree")
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return False
        top = _git(goal.workdir, "rev-parse", "--show-toplevel")
        own_repo = top.returncode == 0 and Path(top.stdout.strip()).resolve() == (
            goal.workdir.resolve()
        )
        scope = [] if own_repo else ["--", "."]
        _exclude_baton(goal.workdir)
        status = _git(goal.workdir, "status", "--porcelain", *scope)
        if status.returncode != 0 or not status.stdout.strip():
            return False
        if _git(goal.workdir, "add", "-A", *scope).returncode != 0:
            return False
        message = f"baton: snapshot round {n} ({agent})"
        return _git(goal.workdir, "commit", "--no-verify", "-m", message, *scope).returncode == 0
    except Exception:
        return False


_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")


def _head(workdir: Path) -> str:
    """The checked-out commit, or "" when the workdir is not in a repository with commits."""
    try:
        found = _git(workdir, "rev-parse", "--verify", "-q", "HEAD")
    except Exception:
        return ""
    return found.stdout.strip() if found.returncode == 0 else ""


def commits_since(workdir: Path, sha: str) -> str:
    """One line per commit under `workdir` made after `sha`, newest first, capped. "" if none
    or if the history cannot be read (not a repository, `sha` unknown or rewritten away)."""
    if not _SHA_RE.fullmatch(sha or ""):
        return ""
    try:
        found = _git(workdir, "log", "--oneline", f"{sha}..HEAD", "--", ".")
    except Exception:
        return ""
    if found.returncode != 0:
        return ""
    text = found.stdout.strip()
    if len(text) > COMMITS_MAX_CHARS:
        text = text[:COMMITS_MAX_CHARS].rsplit("\n", 1)[0] + "\n..."
    return text


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
        self.group_start = ""
        self.need_gate = True
        self.prev_agent: str | None = None
        self.carried_notes = ""
        self.heads: dict[str, str] = {}
        self.pause_until = 0.0

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

    def nap(self, seconds: float, until: float | None = None) -> None:
        """Sleep `seconds` in steps of at most STEP_S, returning early once a stop is requested
        (or, with `until`, once the clock gets there). The step count does not depend on the
        clock, so this always sleeps."""
        remaining = seconds
        while remaining > 0 and (until is None or self.clock() < until):
            if self.stop_requested():
                return
            step = min(STEP_S, remaining)
            self.sleep(step)
            remaining -= step

    def nap_until(self, until: float) -> None:
        self.nap(until - self.clock(), until)

    def honour_stop(self) -> bool:
        """True, after consuming the request, when a stop was asked for."""
        if not self.stop_requested():
            return False
        try:
            self.store.clear_stop()
        except Exception:
            pass
        self.emit("stopped", round=self.round_no)
        return True

    def finish(self, reason: str) -> int:
        try:
            self.store.clear_stop()  # a stop asked for during the last round has nothing left to do
        except Exception:
            pass
        self.emit("finished", reason=reason, round=self.round_no)
        return 0

    def runner_state(self) -> dict:
        return {
            "round": self.round_no,
            "workdir": str(self.goal.workdir),
            "sessions": dict(self.sessions),
            "round_tail": self.round_tail,
            "heads": dict(self.heads),
            "group": self.group,
            "group_start": self.group_start if self.group is not None else None,
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
                if self.honour_stop():
                    return 0
                self.nap(ERROR_SLEEP_S)

    def start(self) -> None:
        goal = self.goal
        state = self.store.load_state()
        sched = Scheduler(goal.order, self.adapters, self.store, self.clock)
        group = state.get("group")
        if isinstance(group, int) and not isinstance(group, bool) and group > 1:
            # Under the lock, so this can never be another live runner's agent.
            if group == os.getpgrp():
                pass
            elif group_is_ours(group, state.get("group_start")):
                kill_group(group)
                self.emit("alert", message="stopped a process group left by a previous run")
            else:
                self.emit(
                    "alert",
                    message=f"process group {group} from the last run is not the one baton "
                    "started (or could not be checked): not killed",
                )
        self.group = None
        self.group_start = ""
        same_dir = state.get("workdir") == str(goal.workdir)
        saved = state.get("sessions")
        heads = state.get("heads")
        if same_dir and isinstance(saved, dict):
            self.sessions = {
                a: s for a, s in saved.items() if isinstance(s, str) and s and a in goal.agents
            }
        else:
            self.sessions = {}
            if saved:
                self.emit("alert", message="working directory changed: session ids cleared")
        self.heads = (
            {a: h for a, h in heads.items() if isinstance(h, str) and a in goal.agents}
            if same_dir and isinstance(heads, dict)
            else {}
        )
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
            passed, self.gate_tail = run_gate(goal, should_kill=self.stop_now)
            self.need_gate = False
            self.emit("gate", passed=passed, tail=self.gate_tail)
            if passed:
                return self.finish("verify passed")
        self.need_gate = False
        if self.pause_until:
            until, self.pause_until = self.pause_until, 0.0
            self.emit(
                "wait",
                reason="the last round ended at once",
                until=until,
                seconds=max(0.0, until - self.clock()),
                agents=self.sched.snapshot()["agents"] if self.sched else {},
            )
            self.nap_until(until)
        if self.honour_stop():
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
        if self.run_round(adapter):
            return self.finish("round completed")
        return None

    def stop_now(self) -> bool:
        return self.stop_requested() == "now"

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
        since = commits_since(goal.workdir, self.heads.get(agent, ""))

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

        spawned = False

        def on_spawn(pgid: int) -> None:
            # The process exists, so the prompt was delivered: notes are spent from here on.
            nonlocal spawned
            spawned = True
            self.carried_notes = ""
            self.group = pgid
            self.group_start = (process_start(pgid) or "") if pgid > 1 else ""
            self.persist_softly()

        def on_session(session: str) -> None:
            self.sessions[agent] = session
            self.persist_softly()

        def attempt(session: str | None) -> RoundResult:
            prompt = build_prompt(
                goal,
                self.gate_tail,
                notes,
                handoff,
                self.round_tail,
                resumed=session is not None,
                since_commits=since,
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
            # The failed attempt delivered nothing that the fresh one should treat as spent: if
            # its start is refused too, the notes and context must still be waiting.
            self.carried_notes = notes
            spawned = False
            buf = ""
            result = attempt(None)

        self.group = None
        ended = self.clock()
        # Record first: nothing after this can lose a credit_guard or limit outcome.
        self.sched.record(agent, result)
        if result.outcome in (CREDIT_GUARD, AUTH):
            self.emit("alert", agent=agent, outcome=result.outcome, reason=result.reason)
        snapshotted = snapshot_work(goal, n, agent)
        if spawned:  # a start that was refused leaves the context for the next round
            self.round_tail = buf
            self.prev_agent = agent
            head = _head(goal.workdir)
            if head:
                self.heads[agent] = head
        self.pause_until = (
            ended + MIN_ROUND_S
            if result.outcome == COMPLETED and not killed and ended - started < MIN_ROUND_S
            else 0.0
        )
        self.emit(
            "round_end",
            round=n,
            agent=agent,
            outcome=result.outcome,
            reason=result.reason,
            until=result.until,
            seconds=round(ended - started, 1),
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

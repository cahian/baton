"""The run loop: gate, rotate, run one round, snapshot, repeat."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path

from baton.adapters.base import (
    AUTH,
    COMPLETED,
    CREDIT_GUARD,
    PREEMPTED,
    TIMEOUT,
    TRANSIENT,
    Adapter,
    RoundContext,
    RoundResult,
)
from baton.config import Goal, GoalError, load_memory
from baton.memory import Memory, RoundEntry
from baton.proc import DEAD, OURS, UNKNOWN, check_group, clean_env, kill_group, process_start
from baton.scheduler import Scheduler, read_availability
from baton.store import Store, workdir_lock

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
# While a metered agent runs, the plan agents are read at most this often (seconds), and the
# watcher is given this long to finish when the round ends.
PREEMPT_POLL_S = 30
PREEMPT_JOIN_S = 2
# What a shell exits with when it cannot run the command: not executable, not found.
UNRUNNABLE_EXITS = (126, 127)
GATE_EXIT_RE = re.compile(r"\[exit code (\d+)\]\Z")
UNRUNNABLE_MESSAGE = (
    "the verify command exited {code}: it was not found or not executable, so it can never pass. "
    "The service's PATH holds only agent, node and system directories: call the tool by its full "
    "path. Rounds go on as usual."
)

LESSONS_RULE = (
    "- Keep durable findings (root causes, dead ends, what must not be retried) under a "
    "`## Lessons` heading in the handoff. They outlive this goal."
)
RULES = f"""\
Rules:
- You are one shift of a relay. Another agent may continue after you at any moment.
- Commit working progress often, in small steps.
- Keep `.baton/HANDOFF.md` current: what was tried, what was learned, what comes next.
{LESSONS_RULE}
- Do not declare success. Only the verify command decides."""


def build_prompt(
    goal: Goal,
    gate_tail: str,
    notes: str,
    handoff: str,
    round_tail: str,
    resumed: bool,
    since_commits: str = "",
    memory: str = "",
) -> str:
    """The prompt for one round.

    A fresh round gets the whole briefing. A resumed one already has it in its own history, so
    it only gets what changed: the commits others made since, the gate output, the previous
    round's tail, notes and handoff. `memory`, the text recalled from long-term memory, comes
    after the handoff in both.
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
    memory = memory.strip()
    if memory:
        parts.append(memory)
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
                env=clean_env(),
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


def gate_exit_code(tail: str) -> int | None:
    """The exit code `run_gate` put last in its text, None when the command did not exit."""
    found = GATE_EXIT_RE.search(tail)
    return int(found.group(1)) if found else None


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


# preemption


class PreemptionWatch:
    """Reads the plan agents from a thread of its own while a metered round runs.

    Every read happens here, never in the stream's callback, so a slow read cannot hold up the
    stream. It looks at most once per `poll_s`, and only at plan agents past their cooldown; each
    answer goes back through `Scheduler.record_read`, so a "no" (or a read that raised) moves that
    agent's `until` out and it is not asked again before then. The first agent that reads ok is
    left in `back` and `found` is set; the runner's should-kill callback does the killing.
    """

    def __init__(self, sched: Scheduler, adapters: Mapping[str, Adapter], poll_s: float) -> None:
        self._sched = sched
        self._adapters = adapters
        self._poll_s = poll_s
        self._over = threading.Event()
        self._thread = threading.Thread(target=self._run, name="baton-preempt", daemon=True)
        self.found = threading.Event()
        self.back = ""

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """End the watch and wait a bounded time for it. A read still stuck is abandoned: the
        thread is a daemon, and its answer is dropped."""
        self._over.set()
        self._thread.join(timeout=PREEMPT_JOIN_S)

    def _run(self) -> None:
        while not self._over.wait(self._poll_s):
            try:
                for name in self._sched.plan_ready_to_check():
                    avail = read_availability(self._adapters[name])
                    if self._over.is_set():
                        return  # the round is over: a late answer is nobody's to act on
                    self._sched.record_read(name, avail)
                    if avail.ok:
                        self.back = name
                        self.found.set()
                        return
            except Exception:
                pass  # a failed check leaves the round running


def preempted_sentence(event: Mapping) -> str:
    """What the person reads for a `round_end` that ended because a plan agent came back."""
    back = event.get("back") or "a plan agent"
    agent, n = event.get("agent"), event.get("round")
    return f"round {n}: {agent} stopped because {back} is available again"


# the loop


def run_goal(
    goal: Goal,
    adapters: Mapping[str, Adapter],
    store: Store,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    objective: Callable[[], str] | None = None,
    owes_round: bool = False,
) -> int:
    """Run `goal` until the gate passes or a stop is honoured. Returns 0 either way.

    A passing gate (or, with no gate, a completed round) only finishes the goal when no note from
    the person is left unread, in the inbox or carried over from a round that never got to them:
    the rotation goes on until a round has taken them. A stop request wins over notes. `objective`,
    when given, supplies the objective of each fresh round, for a caller whose briefing grows while
    the goal runs. `owes_round` says the objective is a message from a person, which gets a round
    of its own even if the gate already passes; like the notes, that is remembered in state.json
    until a round has taken it.

    Holds the goal's lock and a claim on its directory for the duration; raises AlreadyRunning
    when another runner has the goal, or the directory or one inside or around it. Everything else
    that goes wrong is logged as an `error` event, followed by a 60 s sleep.
    """
    with store.lock(), workdir_lock(goal.workdir, goal.name):
        return _Run(goal, adapters, store, clock, sleep, objective, owes_round).run()


class _Run:
    def __init__(
        self,
        goal: Goal,
        adapters: Mapping[str, Adapter],
        store: Store,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
        objective: Callable[[], str] | None = None,
        owes_round: bool = False,
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
        self.objective = objective
        self.prev_agent: str | None = None
        self.carried_notes = ""
        self.collected_inbox = ""
        self.owes_round = owes_round
        self.heads: dict[str, str] = {}
        self.pause_until = 0.0
        self.verify_warning = ""
        # Long-term memory: built the first time it is needed, None when it is off. A round that
        # started a process waits in `pending` until its gate has run.
        self.mem: Memory | None = None
        self.mem_checked = False
        self.pending: RoundEntry | None = None
        self.pending_saved = False  # its page is already stored, with gate "none"

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
            self.store.consume_stop()
        except Exception:
            pass
        self.emit("stopped", round=self.round_no)
        return True

    def finish(self, reason: str) -> int:
        self.record_pending()  # nothing may be left unrecorded: there is no next gate to wait for
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
            "notes": self.carried_notes,
            "collected_inbox": self.collected_inbox,
            "owes_round": self.owes_round,
            "verify_warning": self.verify_warning,
        }

    @staticmethod
    def start_time(pgid: int) -> str:
        """The group leader's start time for state.json, or "" when it cannot be read."""
        if pgid <= 1:
            return ""
        for _ in range(2):
            found = process_start(pgid)
            if found is not None:
                return found
        return ""

    def persist(self) -> None:
        """Write the runner's part of state.json. Raises StateError when it cannot be read."""
        state = self.store.load_state()
        state.update(self.runner_state())
        self.store.save_state(state)

    def persist_softly(self) -> bool:
        """`persist`, reporting a failure as an event. True when the state was saved."""
        try:
            self.persist()
        except Exception as exc:
            self.emit("error", error=f"could not save state: {exc}")
            return False
        return True

    # long-term memory: none of it may stop a goal

    def memory(self) -> Memory | None:
        """The goal's Memory, built once. None when it is off: `memory = false` in the goal,
        `BATON_MEMORY=off` or `enabled = false` in the settings, or settings that cannot be read
        (one event, and the goal goes on without memory)."""
        if self.mem_checked:
            return self.mem
        self.mem_checked = True
        if not self.goal.memory:
            return None
        try:
            cfg = load_memory()
            if cfg.enabled:
                self.mem = Memory(cfg, self.goal.workdir, self.goal.name)
        except GoalError as exc:
            self.emit("memory", event="error", reason=f"memory is off, settings: {exc}")
        except Exception as exc:
            self.emit("memory", event="error", reason=f"memory is off: {type(exc).__name__}: {exc}")
        return self.mem

    def memory_call(self, name: str, *args: object, default: object = None):
        """Call a Memory method. Whatever it raises is an event, never an error of the loop, and
        the reason it left for itself is reported once."""
        mem = self.memory()
        if mem is None:
            return default
        try:
            value = getattr(mem, name)(*args)
        except Exception as exc:
            self.emit("memory", event="error", reason=f"{type(exc).__name__}: {exc}")
            return default
        try:
            reason, mem.last_error = mem.last_error, ""
        except Exception:
            reason = ""
        if reason:
            self.emit("memory", event="error", reason=reason)
        return value

    def recall(self, n: int) -> str:
        mem = self.memory()
        if mem is None:
            return ""
        objective = self.current_objective()  # only here: a briefing callable is not asked in vain
        exclude = self.memory_call("round_path", n - 1, default="") if n > 1 else ""
        text = self.memory_call("recall", objective, self.gate_tail, exclude, default="")
        text = text if isinstance(text, str) else ""
        if text.strip():
            self.emit(
                "memory", event="recalled", hits=getattr(mem, "last_hits", 0), chars=len(text)
            )
        return text

    def save_pending(self) -> None:
        """Store the pending round now, with no gate result, so that a runner killed during
        the gate (or never reaching it) still leaves its page. The real result replaces it."""
        if self.pending is None:
            return
        self.memory_call("remember_round", self.pending)
        self.memory_call("remember_lessons", self.pending.handoff)
        self.pending_saved = True

    def record_pending(self, gate: str | None = None, gate_tail: str = "") -> None:
        """Settle the round that waits for its gate, once: with the gate's result when it has
        one (the page written at the round's end is replaced), or as it stands (nothing to do
        when it was already stored)."""
        entry, self.pending = self.pending, None
        saved, self.pending_saved = self.pending_saved, False
        if entry is None:
            return
        if gate is not None:
            entry = replace(entry, gate=gate, gate_tail=gate_tail)
        elif saved:
            return
        self.memory_call("remember_round", entry)
        self.memory_call("remember_lessons", entry.handoff)

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
            found = check_group(group, state.get("group_start"))
            if found == OURS:
                kill_group(group)
                self.emit("alert", message="stopped a process group left by a previous run")
            elif found == UNKNOWN:
                # Alive, and it cannot be proven that it is ours or not. Starting a round beside
                # a possibly live agent could spend credits twice, so keep the record and retry.
                message = (
                    f"process group {group} from the last run is alive but could not be "
                    "verified: waiting (remove 'group' from state.json if it is not baton's)"
                )
                self.emit("alert", message=message)
                raise RuntimeError(message)
            elif found != DEAD:
                self.emit(
                    "alert",
                    message=f"process group {group} from the last run is now another process: "
                    "not killed",
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
        notes = state.get("notes")
        self.carried_notes = notes if isinstance(notes, str) else ""
        collected = state.get("collected_inbox")
        self.collected_inbox = collected if isinstance(collected, str) else ""
        self.owes_round = self.owes_round or state.get("owes_round") is True
        warning = state.get("verify_warning")
        self.verify_warning = warning if isinstance(warning, str) else ""
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
            self.check_gate_ran()
            stopped = self.gate_tail.rstrip().endswith("[verify stopped]")
            self.record_pending(
                "none" if stopped else "passed" if passed else "failed", self.gate_tail
            )
            if passed and not self.owes_round and not self.unread_notes():
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
        if self.run_round(adapter) and not self.unread_notes():
            return self.finish("round completed")
        return None

    def check_gate_ran(self) -> None:
        """Warn once per run of gates that could not run; `baton status` shows the warning until
        a gate ends any other way. It only informs: the rotation goes on."""
        code = gate_exit_code(self.gate_tail)
        warned = bool(self.verify_warning)
        self.verify_warning = (
            UNRUNNABLE_MESSAGE.format(code=code) if code in UNRUNNABLE_EXITS else ""
        )
        if self.verify_warning and not warned:
            self.emit("verify_unrunnable", code=code, message=self.verify_warning)
        if bool(self.verify_warning) != warned:
            self.persist_softly()

    def unread_notes(self) -> bool:
        """True while a note from the person has not reached an agent. The inbox is moved into
        the carried notes (where the next round would take it anyway) so it is not lost."""
        try:
            left_in_inbox = self.collect_notes()
        except OSError:
            left_in_inbox = False
        return left_in_inbox or bool(self.carried_notes.strip())

    def collect_notes(self) -> bool:
        """Move the inbox into the carried notes: read, save, then delete, so a note is always in
        a file. True when the state could not be saved, which leaves the note in the inbox. Raises
        OSError when the inbox cannot be read or deleted.

        `collected_inbox` is the inbox text the save covers, kept until the delete is done: a kill
        between the two leaves it in state.json, so the next run adds only what came after it."""
        text = self.store.read_inbox()
        if not text:
            if self.collected_inbox:  # a kill between the delete and the save that follows it
                self.collected_inbox = ""
                self.persist_softly()
            return False
        carried, collected = self.carried_notes, self.collected_inbox
        self.carried_notes += text.removeprefix(collected)
        self.collected_inbox = text
        if not self.persist_softly():
            self.carried_notes, self.collected_inbox = carried, collected
            return True
        self.store.clear_inbox(text)
        self.collected_inbox = ""
        self.persist_softly()
        return False

    def stop_now(self) -> bool:
        return self.stop_requested() == "now"

    # one round

    def run_round(self, adapter: Adapter) -> bool:
        """Run one round for `adapter`. True when it ends the goal (no verify command)."""
        assert self.sched is not None
        goal, store = self.goal, self.store
        agent = adapter.name
        gate_was_due = self.need_gate
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
        self.collect_notes()
        notes = self.carried_notes
        handoff = self.read_handoff()
        sid = self.sessions.get(agent)
        since = commits_since(goal.workdir, self.heads.get(agent, ""))
        head_before = _head(goal.workdir) if self.memory() is not None else ""
        recalled = self.recall(n)
        if self.mem is not None and self.stop_requested():
            # A stop that came while memory was being read: no process is spawned for this round,
            # so it does not exist. The counter and the state go back, and the loop honours it.
            self.round_no = n - 1
            self.need_gate = gate_was_due
            self.persist_softly()
            return False

        buf = ""
        worked = False
        killed = False
        interrupted = False
        preempted = False
        watch = (
            PreemptionWatch(self.sched, self.adapters, PREEMPT_POLL_S)
            if getattr(adapter, "metered", False)
            else None
        )

        def on_output(line: str) -> None:
            nonlocal buf, worked
            try:
                text = adapter.render(line)
                is_work = getattr(adapter, "is_work", None)
                if text and (is_work is None or is_work(line)):
                    worked = True
            except Exception:
                text = None
            if text:
                buf = (buf + text + "\n")[-ROUND_TAIL_CHARS:]
                self.emit("output", agent=agent, round=n, text=text)

        def should_kill() -> bool:
            nonlocal killed, interrupted, preempted
            if killed:
                return True
            try:
                if store.stop_request() == "now":
                    killed = True
                elif store.take_interrupt():
                    killed = interrupted = True
            except OSError:
                pass
            if not killed and watch is not None and watch.found.is_set():
                killed = preempted = True  # a stop or a note, seen above, takes precedence
            return killed

        spawned = False

        def on_spawn(pgid: int) -> None:
            nonlocal spawned
            spawned = True
            self.group = pgid
            self.group_start = self.start_time(pgid)
            self.persist_softly()

        def on_session(session: str) -> None:
            self.sessions[agent] = session
            self.persist_softly()

        def attempt(session: str | None) -> RoundResult:
            prompt = build_prompt(
                goal if self.objective is None else replace(goal, objective=self.objective()),
                self.gate_tail,
                notes,
                handoff,
                self.round_tail,
                resumed=session is not None,
                since_commits=since,
                memory=recalled,
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
        if watch is not None:
            watch.start()
        try:
            result = attempt(sid)
            if result.resume_failed and sid is not None:
                self.sessions.pop(agent, None)
                self.emit("alert", agent=agent, message="resume failed: running the round fresh")
                # The failed attempt delivered nothing: the fresh one decides what was spent.
                spawned = worked = False
                buf = ""
                result = attempt(None)
        finally:
            if watch is not None:
                watch.stop()
        if preempted and watch is not None and result.outcome in (COMPLETED, TIMEOUT, TRANSIENT):
            # What the adapter made of being killed (a stop, a crash) is not the round's story.
            # A credit_guard, limit or auth that came with it is real and stays.
            result = RoundResult(
                PREEMPTED, reason=f"{watch.back} is available again", back=watch.back
            )

        self.group = None
        ended = self.clock()
        # Record first: nothing after this can lose a credit_guard or limit outcome.
        self.sched.record(agent, result, seconds=ended - started)
        if result.outcome in (CREDIT_GUARD, AUTH):
            self.emit("alert", agent=agent, outcome=result.outcome, reason=result.reason)
        snapshotted = snapshot_work(goal, n, agent)
        # A process that exists has not read its prompt: one that dies at once with a 429, or is
        # ended before it says anything, leaves the notes and the context for the next round.
        finished_its_turn = result.outcome in (COMPLETED, TIMEOUT) and not killed
        if spawned and (worked or finished_its_turn):
            self.carried_notes = ""
            self.owes_round = False
            self.round_tail = buf
        if spawned:
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
            back=watch.back if result.outcome == PREEMPTED and watch is not None else None,
        )
        self.persist_softly()
        if spawned and self.memory() is not None:
            # What the scheduler was told stays as it is; the page says what baton did.
            ended_by_us = (
                killed and not preempted and result.outcome in (COMPLETED, TIMEOUT, TRANSIENT)
            )
            self.pending = RoundEntry(
                n=n,
                agent=agent,
                outcome="interrupted" if ended_by_us else result.outcome,
                reason=result.reason or "",
                seconds=round(ended - started, 1),
                gate="none",
                gate_tail="",
                commits=commits_since(goal.workdir, head_before) if head_before else "",
                handoff=self.read_handoff(limit=False),  # whole, for the lessons at its top
                round_tail=buf,
            )
            if goal.verify_command:
                self.save_pending()  # the gate may never finish: see `step`
            else:
                self.record_pending()
        return not goal.verify_command and result.outcome == COMPLETED and not killed

    def current_objective(self) -> str:
        try:
            return self.objective() if self.objective is not None else self.goal.objective
        except Exception:
            return self.goal.objective

    def read_handoff(self, limit: bool = True) -> str:
        try:
            text = (self.goal.workdir / ".baton" / "HANDOFF.md").read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            return ""
        return text[-HANDOFF_MAX_CHARS:] if limit else text

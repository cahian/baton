"""The run loop: gate, rotate, run one round, snapshot, repeat."""

from __future__ import annotations

import hashlib
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from baton import superpowers
from baton.adapters.base import (
    AUTH,
    COMPLETED,
    CREDIT_GUARD,
    PREEMPTED,
    SUBAGENT,
    TIMEOUT,
    TRANSIENT,
    Adapter,
    RoundContext,
    RoundResult,
)
from baton.config import Goal, GoalError, load_memory, load_superpowers
from baton.memory import Memory, RoundEntry
from baton.phases import (
    BUILD,
    CHECK_FILE,
    CONSENSUS,
    PLAN,
    REVIEW,
    TIER,
    PhaseState,
    add_review_task,
    add_task,
    advance,
    archive_check,
    archive_plan,
    check_diff,
    clear_review,
    open_tasks,
    phase_prompt,
    read_plan,
    read_review,
    reason_for,
    verdict,
)
from baton.proc import DEAD, OURS, UNKNOWN, check_group, clean_env, kill_group, process_start
from baton.scheduler import Scheduler, TurnLimits, read_availability
from baton.store import Store, workdir_lock

GATE_TAIL_CHARS = 4000
ROUND_TAIL_CHARS = 8000
HANDOFF_MAX_CHARS = 16000
# The top of a long handoff holds the objective and what the person allowed: it is always kept.
HANDOFF_HEAD_CHARS = 4000
# Waits are sliced so a stop request is noticed within this many seconds.
STEP_S = 5
ERROR_SLEEP_S = 60
GIT_TIMEOUT_S = 120
COMMITS_MAX_CHARS = 2000
# A round that completes sooner than this, without the goal being met, is followed by a pause of
# this length, so an agent that exits at once cannot burn quota in a tight loop.
MIN_ROUND_S = 30
# Phases: a phase that never completes still reaches the gate, and a builder that closes no task
# cannot stay in build for ever.
GATE_EVERY_ROUNDS = 3
BUILD_IDLE_ROUNDS = 8
BUILD_ROUNDS_PER_TASK = 3
BUILD_ROUNDS_EXTRA = 5
# How long a job waits before it looks for Superpowers again.
SKILLS_RETRY_S = 300
GATE_POLL_S = 0.5
# While a reserve agent runs, the other agents are read at most this often (seconds), and the
# watcher is given this long to finish when the round ends.
PREEMPT_POLL_S = 30
PREEMPT_JOIN_S = 2
# A message that arrives during a tool call waits for it to end, but no longer than this.
MESSAGE_WAIT_S = 300
# A note over this many characters goes to a file in the workdir, and the prompt keeps its start.
NOTE_INLINE_CHARS = 8000
NOTE_HEAD_CHARS = 1000
# How many notes state.json keeps for the conversations that have not read them yet.
NOTE_LOG_MAX = 50
# What a shell exits with when it cannot run the command: not executable, not found.
# The check of a goal that has no verify command, and what a gate says when it has none or when
# the script is not the one a plan round saved.
CHECK_COMMAND = f"bash .baton/{CHECK_FILE}"
NO_CHECK = "no check"
CHECK_CHANGED = "the check changed outside plan"
# Every this many failed cycles (a failed gate or a rejection) the person is told baton goes on.
NO_PROGRESS_EVERY = 3
# Three consensus rounds in a row that do not complete count as one failed cycle.
INCOMPLETE_LIMIT = 3
CHANGED_PATHS_SHOWN = 10
# A goal needs these, and is refused where they are not so.
PHASES_REFUSAL = "a goal needs phases, and phases = false here"
REPO_REFUSAL = "a goal needs a git repository"
# How long a run that cannot do a goal waits before it looks again (it runs no round meanwhile).
HOLD_RETRY_S = 60
# What a rejection and a message during consensus add to the plan, for the build that follows.
REJECTION_TASK = "- [ ] Fix what the consensus review asked (see `.baton/REVIEW.md`)"
MESSAGE_TASK = "- [ ] Do what the person's message asks"
CHANGED_TASK = "- [ ] Review the changes made during consensus"
CHECK_TASK = "- [ ] Make the check pass again (see the verify output above)"
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
PINNED_GOAL_HEAD = "Goal, fixed by the person. It does not change until the person changes it:"
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
    phase_block: str = "",
    skills: str = "",
    pinned_goal: str = "",
    goal_check: bool = False,
) -> str:
    """The prompt for one round.

    A fresh round gets the whole briefing. A resumed one already has it in its own history, so
    it only gets what changed: the commits others made since, the gate output, the previous
    round's tail, notes and handoff. `memory`, the text recalled from long-term memory, comes
    after the handoff in both. `pinned_goal`, when the person has fixed one, opens every prompt.
    `goal_check` says the goal has no verify command but its check is `.baton/VERIFY.sh`.
    """
    parts: list[str] = []
    pinned_goal = pinned_goal.strip()
    if pinned_goal:
        parts.append(f"{PINNED_GOAL_HEAD}\n{pinned_goal}")
    if resumed:
        parts.append("Your turn again. This is what changed since your last round.")
    else:
        parts.append(f"Objective:\n{goal.objective.strip()}")
        if goal.verify_command:
            parts.append(
                "Verify command (exit 0 means the objective is met):\n"
                f"{goal.verify_command.strip()}"
            )
        elif goal_check:
            parts.append(
                f"The check is `.baton/{CHECK_FILE}`, written in plan (exit 0 means the objective "
                "is met). The goal ends when it passes and every agent approves."
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
    # The skills come first and the phase block last, so what baton asks of this round is read last.
    skills = skills.strip()
    if skills:
        parts.append(skills)
    phase_block = phase_block.strip()
    if phase_block:
        parts.append(phase_block)
    return "\n\n".join(parts) + "\n"


def file_large_note(workdir: Path, text: str, now: float) -> tuple[str, Path | None]:
    """`text` as a prompt will carry it, and the file that holds all of it. A note over
    NOTE_INLINE_CHARS goes to `.baton/notes/` in the workdir, and the prompt keeps its start and
    a line naming the file. One that cannot be filed stays whole: the person's words come first."""
    body = text.strip()
    if len(body) <= NOTE_INLINE_CHARS:
        return text, None
    stamp = datetime.fromtimestamp(now, UTC).strftime("%Y%m%dT%H%M%SZ")
    digest = hashlib.sha256(body.encode("utf-8", errors="replace")).hexdigest()[:8]
    relative = Path(".baton") / "notes" / f"{stamp}-{digest}.md"
    path = workdir / relative
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body + "\n", encoding="utf-8")
    except OSError:
        return text, None
    return (
        f"{body[:NOTE_HEAD_CHARS].rstrip()}\n"
        f"[This note is {len(body)} characters long and is cut here. The whole of it is in "
        f"`{relative}`: read that file in full before you act on it.]\n\n",
        path,
    )


def pin_goal(store: Store, workdir: Path, text: str, now: float) -> str:
    """Fix `text` as the goal. One over NOTE_INLINE_CHARS goes to a file the way a large note does,
    once, and what is pinned is its start and the line naming the file: every prompt carries the
    goal, so it must stay small. Returns what was pinned."""
    short, _ = file_large_note(workdir, text, now)
    store.set_goal(short)
    return short


# gate


def _tail_of(fh, chars: int) -> str:
    size = fh.seek(0, os.SEEK_END)
    fh.seek(max(0, size - chars * 4))
    return fh.read().decode("utf-8", errors="replace")[-chars:]


def run_gate(
    goal: Goal, should_kill: Callable[[], bool] | None = None, goal_set: bool = False
) -> tuple[bool, str]:
    """Run the verify command, or, while a goal is set (`goal_set`) and there is none, the
    script `.baton/VERIFY.sh`. With neither it fails with `no check`. Passed only on exit 0;
    anything else, even a timeout, a stop or a command that cannot start, is not passed.
    `should_kill` is polled while it runs and ends it. Raises nothing but KeyboardInterrupt and
    SystemExit, and the command's whole process group is gone by then."""
    command = goal.verify_command
    if not command and goal_set and (goal.workdir / ".baton" / CHECK_FILE).is_file():
        command = CHECK_COMMAND
    if not command:
        return False, NO_CHECK
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


def _is_repo(workdir: Path) -> bool:
    """True when the workdir is inside a git work tree."""
    try:
        found = _git(workdir, "rev-parse", "--is-inside-work-tree")
    except Exception:
        return False
    return found.returncode == 0 and found.stdout.strip() == "true"


def changed_paths(workdir: Path) -> list[str]:
    """The paths `git status --porcelain` lists outside `.baton/`, for a note that says what
    changed. Empty when there are none or git cannot say."""
    try:
        found = _git(workdir, "status", "--porcelain", "-z", "--untracked-files=all", "--", ".")
    except Exception:
        return []
    if found.returncode != 0:
        return []
    paths = []
    entries = found.stdout.split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if "R" in code or "C" in code:
            i += 1
        if path != ".baton" and not path.startswith(".baton/"):
            paths.append(path)
    return paths


def goal_refusal(goal: Goal) -> str:
    """Why a goal may not be set here, "" when it may: a goal ends only in consensus, which is
    phases, and an approval is bound to the state of the tree, which is git."""
    if not goal.phases:
        return PHASES_REFUSAL
    if not _is_repo(goal.workdir):
        return REPO_REFUSAL
    return ""


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


def _file_digest(path: Path) -> str:
    """What a path holds, for comparing the tree before and after a round."""
    try:
        if path.is_symlink():
            return "link:" + os.readlink(path)
        if not path.is_file():
            return "absent" if not path.exists() else "dir"
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return "unreadable"


def tree_fingerprint(workdir: Path) -> str | None:
    """HEAD, plus the status and the content of every changed or new file under the workdir,
    leaving out `.baton/`: two equal fingerprints mean nothing outside `.baton/` changed in
    between. None when git cannot say (not a repository, a failing call): the caller then cannot
    tell, and must not trust what it was about to rely on."""
    try:
        inside = _git(workdir, "rev-parse", "--is-inside-work-tree")
        top = _git(workdir, "rev-parse", "--show-toplevel")
        status = _git(workdir, "status", "--porcelain", "-z", "--untracked-files=all", "--", ".")
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return None
        if top.returncode != 0 or status.returncode != 0:
            return None
        root = Path(top.stdout.strip())
        entries = status.stdout.split("\0")
        found: list[tuple[str, str]] = []
        i = 0
        while i < len(entries):
            entry = entries[i]
            i += 1
            if len(entry) < 4:
                continue
            code, path = entry[:2], entry[3:]
            if "R" in code or "C" in code:
                i += 1  # the path it came from follows
            if path == ".baton" or path.startswith(".baton/"):
                continue
            found.append((code, path))
        digest = hashlib.sha256(_head(workdir).encode())
        for code, path in sorted(found):
            digest.update(f"\0{code}\0{path}\0{_file_digest(root / path)}".encode())
        return digest.hexdigest()
    except Exception:
        return None


# preemption


class PreemptionWatch:
    """Reads the queue agents from a thread of its own while a reserve round runs.

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


def turn_limits(goal: Goal) -> dict[str, TurnLimits]:
    """What ends each agent's turn, from the goal's settings. Only Claude has budgets."""
    limits = {}
    for name, cfg in goal.agents.items():
        paid = name == "claude_api"
        limits[name] = TurnLimits(
            turn_max_s=cfg.turn_max_s,
            turn_budget_usd=float(cfg.turn_budget_usd) if paid else None,
            goal_budget_usd=float(cfg.goal_budget_usd) if paid else None,
        )
    return limits


def preempted_sentence(event: Mapping) -> str:
    """What the person reads for a `round_end` that ended because a queue agent came back."""
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
        # Every note, newest last, each with a growing `seq`: a conversation reads the ones after
        # its mark in `note_seen` (per session key), whoever read them first.
        self.note_log: list[dict] = []
        self.note_seen: dict[str, int] = {}
        self.owes_round = owes_round
        self.heads: dict[str, str] = {}
        self.pause_until = 0.0
        self.verify_warning = ""
        # The goal text the scheduler last heard of: a different one is a new goal.
        self.goal_seen = ""
        # What the goal was set with: {"verify", "order", "enabled"}, saved when it is first seen.
        # A different goal file later is not the goal the person set (None while no goal is set).
        self.goal_terms: dict[str, Any] | None = None
        self.held = ""  # why this run holds (a goal it cannot do), said once
        # A rotation decided and not yet announced: (the agent that gave way, why). It is said at
        # the next pick, once it is known who takes over.
        self.rotating: tuple[str, str] | None = None
        # The agent a `rotation` event just named: its round_start does not say it again.
        self.announced = ""
        self.next_stuck = False  # a request for the next agent that could not be taken
        self.oneshot_pinned = False  # set when `oneshot` has pinned the objective, once
        # Long-term memory: built the first time it is needed, None when it is off. A round that
        # started a process waits in `pending` until its gate has run.
        self.mem: Memory | None = None
        self.mem_checked = False
        self.pending: RoundEntry | None = None
        self.pending_saved = False  # its page is already stored, with gate "none"
        # Phases: the state of the cycle, who built last (a review goes to someone else), and
        # whether the gate about to run is the one at the end of a cycle.
        self.phases = goal.phases
        self.ps = PhaseState()
        self.builder = ""
        self.cycle_gate = False
        self.from_gate = False  # the cycle at plan began with a failed gate
        self.idle_rounds = 0  # rounds since the phase last changed, whatever their outcome
        self.build_idle = 0  # rounds in build in a row that closed no task
        self.build_rounds = 0  # rounds in build in this cycle
        self.build_limit = 0  # their cap: set when the cycle's build begins
        self.seen_open: list[str] = []  # the open tasks after the last round in build
        self.archive_next = False  # the next round begins a cycle: PLAN.md and REVIEW.md go away
        # A goal the person set ends in consensus: who is still to approve, and who has. None
        # while there is none. `check_lock` is the hash of `.baton/VERIFY.sh` a plan round saved.
        self.consensus: dict[str, Any] | None = None
        self.incomplete_rounds = 0  # consensus rounds in a row that did not complete
        self.consensus_wait = ""  # why consensus last said it waits, so it says it once
        self.check_lock = ""
        self.failed_cycles = 0  # cycles that ended with a failed gate or a rejection
        self.warned_no_repo = False
        self.skills_why = ""
        # Superpowers, read once; until it is found the job waits and runs no round.
        self.skills: superpowers.Skills | None = None

    # plumbing

    def emit(self, kind: str, **fields: object) -> None:
        try:
            self.store.emit(kind, **fields)
        except Exception:  # a full disk must not stop the work
            pass

    def notice(self, message: str) -> None:
        """An automatic decision, said in the chat as `[baton] <message>`."""
        self.emit("notice", message=message)

    def stop_requested(self) -> str:
        try:
            return self.store.stop_request()
        except Exception:
            return ""

    def nap(
        self,
        seconds: float,
        until: float | None = None,
        wake_on_next: bool = False,
        wake_on_note: bool = False,
    ) -> None:
        """Sleep `seconds` in steps of at most STEP_S, returning early once a stop is requested
        (or, with `until`, once the clock gets there, or, with `wake_on_next`, once the person
        asks for the next agent, or, with `wake_on_note`, once a message of theirs is waiting).
        The step count does not depend on the clock, so this always sleeps."""
        remaining = seconds
        while remaining > 0 and (until is None or self.clock() < until):
            if (
                self.stop_requested()
                or (wake_on_next and self.next_pending())
                or (wake_on_note and self.note_waiting())
            ):
                return
            step = min(STEP_S, remaining)
            self.sleep(step)
            remaining -= step

    def nap_until(
        self, until: float, wake_on_next: bool = False, wake_on_note: bool = False
    ) -> None:
        self.nap(until - self.clock(), until, wake_on_next, wake_on_note)

    def note_waiting(self) -> bool:
        """A message from the person waits in the inbox (or was already taken in, unread)."""
        try:
            return bool(self.store.read_inbox().strip()) or bool(self.carried_notes.strip())
        except OSError:
            return False

    def next_pending(self) -> bool:
        """A request for the next agent waits and can be taken: one that cannot be would end
        every wait at once, so it is not waited for."""
        try:
            return not self.next_stuck and self.store.next_pending()
        except Exception:
            return False

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
            "note_log": list(self.note_log),
            "note_seen": dict(self.note_seen),
            "owes_round": self.owes_round,
            "oneshot_pinned": self.oneshot_pinned,
            "verify_warning": self.verify_warning,
            "goal_seen": self.goal_seen,
            "goal_terms": deepcopy(self.goal_terms),
            **(
                {
                    "phase": self.ps.to_json(),
                    "builder": self.builder,
                    "from_gate": self.from_gate,
                    "cycle_gate": self.cycle_gate,
                    "archive_next": self.archive_next,
                    "consensus": deepcopy(self.consensus),
                    "check_lock": self.check_lock,
                    "failed_cycles": self.failed_cycles,
                    "counters": {
                        "idle_rounds": self.idle_rounds,
                        "build_idle": self.build_idle,
                        "build_rounds": self.build_rounds,
                        "build_limit": self.build_limit,
                        "seen_open": list(self.seen_open),
                    },
                }
                if self.phases
                else {}
            ),
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
        if not self.phases:
            for key in (
                "phase",
                "builder",
                "from_gate",
                "cycle_gate",
                "archive_next",
                "consensus",
                "check_lock",
                "failed_cycles",
                "counters",
            ):
                state.pop(key, None)  # nothing left over from an earlier run with phases on
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
        self.store_round(self.pending)
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
        self.store_round(entry)
        self.memory_call("remember_lessons", entry.handoff)

    def store_round(self, entry: RoundEntry) -> None:
        """Write a round's page, and say so when it was written."""
        if self.memory_call("remember_round", entry) is True:
            path = self.memory_call("round_path", entry.n, default="")
            self.emit("memory", event="recorded", round=entry.n, path=path)

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
        sched = Scheduler(goal.order, self.adapters, self.store, self.clock, turn_limits(goal))
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
        # Sessions are kept per agent, or per agent and role with phases: a key this run does not
        # use (an old one, or one from the other mode) is ignored and its agent starts fresh.
        keys = (
            {f"{a}:{role}" for a in goal.agents for role in (PLAN, BUILD)}
            if self.phases
            else set(goal.agents)
        )
        if same_dir and isinstance(saved, dict):
            self.sessions = {
                k: s for k, s in saved.items() if isinstance(s, str) and s and k in keys
            }
        else:
            self.sessions = {}
            if saved:
                self.emit("alert", message="working directory changed: session ids cleared")
        self.heads = (
            {
                k: h
                for k, h in heads.items()
                if isinstance(k, str) and isinstance(h, str) and k.split(":")[0] in goal.agents
            }
            if same_dir and isinstance(heads, dict)
            else {}
        )
        rounds = state.get("round")
        self.round_no = rounds if isinstance(rounds, int) and rounds > 0 else 0
        self.oneshot_pinned = state.get("oneshot_pinned") is True
        if goal.oneshot and not self.oneshot_pinned:
            # Once, and saved below before any round: a restart must not undo `goal off`. A goal
            # the person already set counts as the pin.
            refusal = goal_refusal(goal)
            if refusal:
                self.emit("alert", message=f"{refusal}: the objective is not pinned as a goal")
            elif not self.store.goal():
                pin_goal(self.store, goal.workdir, self.current_objective(), self.clock())
            self.oneshot_pinned = True
        tail = state.get("round_tail")
        self.round_tail = tail if isinstance(tail, str) else ""
        notes = state.get("notes")
        self.carried_notes = notes if isinstance(notes, str) else ""
        collected = state.get("collected_inbox")
        self.collected_inbox = collected if isinstance(collected, str) else ""
        log = state.get("note_log")
        self.note_log = [
            {"seq": e["seq"], "text": e["text"]}
            for e in (log if isinstance(log, list) else ())
            if isinstance(e, dict)
            and isinstance(e.get("seq"), int)
            and not isinstance(e["seq"], bool)
            and isinstance(e.get("text"), str)
        ][-NOTE_LOG_MAX:]
        seen = state.get("note_seen")
        self.note_seen = {
            k: v
            for k, v in (seen.items() if isinstance(seen, dict) else ())
            if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
        }
        for saved in self.sessions:  # a conversation with no mark has read nothing it can rely on
            self.note_seen.setdefault(saved, 0)
        self.owes_round = self.owes_round or state.get("owes_round") is True
        warning = state.get("verify_warning")
        self.verify_warning = warning if isinstance(warning, str) else ""
        seen = state.get("goal_seen")
        self.goal_seen = seen if isinstance(seen, str) else ""
        self.goal_terms = self.read_terms(state.get("goal_terms"))
        if self.store.take_terms_reset():  # the goal was said again: its terms are as they are now
            self.goal_terms = self.terms_now()
        prepare_workdir(goal)
        if self.phases:
            self.start_phases(state, same_dir)
        self.sched = sched
        self.persist()
        self.ready = True

    def start_phases(self, state: dict, same_dir: bool) -> None:
        """Resume the cycle from state.json. A fresh job, a job from before phases, a state that
        cannot be read and a message that owes a round all start a cycle at plan, based on the
        commit the workdir is at now."""
        stored = PhaseState.parse(state.get("phase") if same_dir else None)
        # A job is fresh when no valid phase was stored (a stored start state, with an empty base,
        # is a state like any other), or when a message owes a round.
        if stored is None or self.owes_round:
            stored = PhaseState(PLAN, base=_head(self.goal.workdir))
            # A plan and a review from an earlier goal are not this one's.
            self.archive_cycle_files()
        self.ps = stored
        if same_dir and not self.owes_round:
            # A cycle that ended before the runner was killed: the gate that starts this run is
            # that cycle's gate; and a new cycle's plan files may still be waiting to move.
            self.cycle_gate = state.get("cycle_gate") is True
            self.archive_next = state.get("archive_next") is True
        counters = state.get("counters") if same_dir else None
        if isinstance(counters, dict) and stored.phase != PLAN:
            for name in ("idle_rounds", "build_idle", "build_rounds", "build_limit"):
                value = counters.get(name)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    setattr(self, name, value)
            seen = counters.get("seen_open")
            if isinstance(seen, list) and all(isinstance(t, str) for t in seen):
                self.seen_open = seen
        builder = state.get("builder")
        self.builder = builder if isinstance(builder, str) and builder in self.goal.agents else ""
        self.from_gate = state.get("from_gate") is True and not self.owes_round
        lock = state.get("check_lock")
        self.check_lock = lock if isinstance(lock, str) and same_dir else ""
        failed = state.get("failed_cycles")
        if isinstance(failed, int) and not isinstance(failed, bool) and failed >= 0:
            self.failed_cycles = failed
        # A consensus under way goes on, when its goal is still set. One whose state is gone or
        # damaged, or whose goal was turned off while baton was down, starts over from plan.
        resumed = self.read_consensus(state.get("consensus")) if same_dir else None
        if resumed is not None and self.ps.phase == CONSENSUS and self.goal_on():
            self.consensus = resumed
            self.need_gate = False  # its rounds rerun the check: the gate is not asked again
            drift = self.consensus_drift()
            if drift:  # what was approved is not what is here now
                self.reject_consensus(
                    f"The work changed while baton was down, so the approvals are void: {drift}.",
                    "the work changed since the check passed: back to build",
                    CHANGED_TASK,
                )
        elif self.ps.phase == CONSENSUS:
            self.ps = replace(self.ps, phase=PLAN, cycles=0, stuck=0, last_open=[])

    def read_consensus(self, raw: object) -> dict[str, Any] | None:
        """The consensus saved in state.json, None when it is missing or anything in it is bad."""
        if not isinstance(raw, dict):
            return None
        found: dict[str, Any] = {}
        for key in ("pending", "approved"):
            names = raw.get(key)
            if not isinstance(names, list) or not all(
                isinstance(n, str) and n in self.goal.agents for n in names
            ):
                return None
            found[key] = list(dict.fromkeys(names))
        tree, check = raw.get("tree"), raw.get("check_hash", "")
        # A state with no tree to compare cannot prove the work is still what was approved.
        found["tree"] = tree if isinstance(tree, str) else None
        found["check_hash"] = check if isinstance(check, str) else ""
        return found

    def archive_cycle_files(self) -> None:
        """Move PLAN.md and REVIEW.md out of the way when a new cycle begins (not when a failed
        gate sends the same cycle back to plan)."""
        try:
            archive_plan(self.goal.workdir, self.clock())
        except OSError as exc:
            self.emit("error", error=f"could not archive the plan: {exc}")

    def step(self) -> int | None:
        if not self.ready:
            self.start()
        self.sync_goal()
        with_goal = self.goal_on()
        if with_goal and self.hold():
            return 0 if self.honour_stop() else None
        if self.need_gate and self.gate_due(with_goal):
            passed, self.gate_tail = self.run_check(with_goal)
            self.need_gate = False
            if self.sync_goal():
                # The goal changed while the check ran: the result is for the goal it ran for, and
                # the new one must not inherit it. The check runs again.
                self.need_gate = True
                self.notice("the goal changed while the check ran: it runs again for the new goal")
                return None
            with_goal = self.goal_on()
            cycle_gate = self.cycle_gate
            self.emit("gate", passed=passed, tail=self.gate_tail)
            self.check_gate_ran()
            stopped = self.gate_tail.rstrip().endswith("[verify stopped]")
            self.record_pending(
                "none" if stopped else "passed" if passed else "failed", self.gate_tail
            )
            self.idle_rounds = 0  # whatever the reason it ran, the count starts again
            settled = passed and not self.owes_round and not self.unread_notes()
            if cycle_gate and not stopped:
                self.cycle_gate = False  # only once the gate has finished: a stopped one is redone
                # A pass that starts consensus keeps the plan files: a rejection builds on them.
                if not (with_goal and settled):
                    self.after_cycle_gate(passed)
            if settled and with_goal:
                self.start_consensus()
            elif settled:
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
            self.nap_until(until, wake_on_next=True)
        if self.honour_stop():
            return 0
        if self.skills is None and not self.load_skills():
            # Superpowers is mandatory: no round runs without it, and the job waits, running,
            # and looks again, so installing it is all that is needed.
            until = self.clock() + SKILLS_RETRY_S
            self.emit(
                "wait",
                reason=f"{self.skills_why}: no round runs without Superpowers. Install it with: "
                f"{superpowers.INSTALL_COMMAND}",
                until=until,
                seconds=SKILLS_RETRY_S,
                agents={},
            )
            self.nap_until(until)
            return None
        assert self.sched is not None
        if self.consensus is not None:
            return self.consensus_step()
        if self.take_next():
            self.rotate("asked by the person")
        # A review goes to someone other than the agent that built, when another can take it.
        avoid = self.builder if self.phases and self.ps.phase == REVIEW else ""
        adapter = self.sched.pick(avoid=avoid) if avoid else self.sched.pick()
        if adapter is not None and self.rotating:
            self.announce_rotation(adapter.name)
        self.sched.save()
        if adapter is None:
            until = self.sched.next_wake()
            self.emit(
                "wait",
                until=until,
                seconds=max(0.0, until - self.clock()),
                agents=self.sched.snapshot()["agents"],
            )
            self.nap_until(until, wake_on_next=True)
            return None
        if self.run_round(adapter) and not self.unread_notes():
            return self.finish("round completed")
        return None

    def sync_goal(self) -> bool:
        """A goal set, changed or cleared since the last look is a new one: the agents left out
        for its budget come back and its spend starts again, and its terms are saved as they are.
        Saying the goal again (`baton: goal`) saves the terms again too. Only between rounds. A goal
        that cannot be read is not a change. True when the goal changed."""
        assert self.sched is not None
        current = self.store.read_goal()
        reset = self.store.take_terms_reset()
        if current is None:
            return False
        if current == self.goal_seen:
            if current and (reset or self.goal_terms is None):
                self.goal_terms = self.terms_now()
                self.persist_softly()
            return False
        self.goal_seen = current
        self.goal_terms = self.terms_now() if current else None
        self.sched.new_goal()
        # The check, the lock on it and the count of failed cycles belong to one goal.
        self.set_check_aside()
        self.failed_cycles = 0
        if self.consensus is not None:
            self.notice("the goal changed: the approval under way is dropped")
            self.leave_consensus(PLAN)
        self.persist_softly()
        return True

    def hold(self) -> bool:
        """True when this run cannot do the goal that is set (no phases, no git repository): it says
        so once, runs no round and ends nothing, and looks again after a while, until the goal is
        cleared (`baton: goal off`) or a stop is asked for. It does not fall back to ending on a
        passed check."""
        refusal = goal_refusal(self.goal)
        if not refusal:
            self.held = ""
            return False
        if refusal != self.held:
            self.held = refusal
            self.emit(
                "alert",
                message=f"{refusal}: no round runs until the goal is cleared (`baton: goal off`) "
                "or this is fixed",
            )
        self.nap(HOLD_RETRY_S)
        return True

    def terms_now(self) -> dict[str, Any]:
        """What this run's goal file says that a goal is judged by: its verify command, who is in
        the queue and who is enabled."""
        goal = self.goal
        return {
            "verify": goal.verify_command or "",
            "order": list(goal.order),
            "enabled": [n for n, cfg in goal.agents.items() if cfg.enabled],
        }

    @staticmethod
    def read_terms(raw: object) -> dict[str, Any] | None:
        """The terms saved in state.json, None when missing or damaged (saved again later)."""
        if not isinstance(raw, dict) or not isinstance(raw.get("verify"), str):
            return None
        found: dict[str, Any] = {"verify": raw["verify"]}
        for key in ("order", "enabled"):
            names = raw.get(key)
            if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
                return None
            found[key] = list(names)
        return found

    def terms_changed(self) -> str:
        """How this run's goal file differs from the terms the goal was set with, in words, ""
        when it does not. Said as `the goal's terms changed: <what>` until the goal is set again."""
        saved = self.goal_terms
        if saved is None or not self.goal_on():
            return ""
        now = self.terms_now()
        found = []
        if saved["verify"] != now["verify"]:
            found.append(
                f"verify was `{saved['verify'] or 'none'}`, now `{now['verify'] or 'none'}`"
            )
        for key in ("order", "enabled"):
            if saved[key] != now[key]:
                was, is_now = ", ".join(saved[key]) or "none", ", ".join(now[key]) or "none"
                found.append(f"{key} was {was}, now {is_now}")
        return "; ".join(found)

    def take_next(self) -> bool:
        """Take the person's request for the next agent: True when there was one."""
        try:
            asked = self.store.take_next()
        except OSError:
            self.next_stuck = True
            return False
        self.next_stuck = False
        return bool(asked)

    def rotate(self, reason: str) -> None:
        """Hand the queue to the next agent. It is announced at the next pick, because only then
        is it known who runs: the next one may be unavailable, or the only one. A rotation
        already waiting to be announced is not made again: a second request in the meantime
        (a `next` while the gate runs) only changes its reason."""
        assert self.sched is not None
        if self.rotating:
            self.rotating = (self.rotating[0], reason)
            return
        self.rotating = (self.sched.current, reason)
        self.sched.rotate()

    def announce_rotation(self, agent: str) -> None:
        """Say the rotation decided, now that `agent` is the one picked. When it is the one that
        gave way (nobody else could run), nothing changed: no event, and its turn starts over."""
        assert self.sched is not None and self.rotating is not None
        (previous, reason), self.rotating = self.rotating, None
        if agent == previous:
            self.sched.new_turn(agent)
            return
        self.emit("rotation", round=self.round_no, previous=previous, agent=agent, reason=reason)
        self.announced = agent

    def end_turn(self, agent: str, spent: float, rotate: bool = True) -> None:
        """After a round: the goal's budget may leave `agent` out, and the person's `next` or the
        end of its turn gives the queue to the next one. Rotation happens only here and between
        rounds, never in the middle of one. A consensus round (`rotate=False`) rotates nothing."""
        assert self.sched is not None
        if (
            spent > 0
            and self.goal_seen
            and agent not in self.sched.dropped
            and self.sched.goal_over(agent)
        ):
            self.sched.drop(agent)
            budget = self.goal.agents[agent].goal_budget_usd
            self.emit(
                "alert",
                agent=agent,
                message=f"{agent} spent USD {budget:f} in this goal: it is left out of the queue "
                "until the goal changes",
            )
        if not rotate:
            return
        reason = "asked by the person" if self.take_next() else self.sched.turn_reason(agent)
        if reason:
            self.rotate(reason)

    def after_cycle_gate(self, passed: bool) -> None:
        """The gate at the end of a cycle is the only judge. Passed: the next cycle, if one is
        owed, starts from where the code is now. Failed: back to plan with the base kept, so the
        strong model diagnoses the output and adds tasks."""
        if passed:
            self.ps = replace(self.ps, base=_head(self.goal.workdir))
            self.archive_next = True
        else:
            self.ps = replace(self.ps, phase=PLAN, cycles=0, stuck=0, last_open=[])
            self.from_gate = True
            self.emit("phase", phase=PLAN, cycle=0, reason="verify failed: back to plan")
            if self.goal_on():
                self.count_failed_cycle()
        self.persist_softly()

    # a goal the person set: the check, the lock on it, and consensus

    def goal_on(self) -> bool:
        """True while a goal is set, whatever else this run is set up to do. A set goal is always
        goal mode: it ends only in consensus, and a run that cannot do that (no phases, no git
        repository: `goal_refusal`) holds rather than fall back to ending on a pass."""
        current = self.store.read_goal()
        return bool(self.goal_seen if current is None else current)

    def gated(self) -> bool:
        """Whether a gate judges this run: the verify command, or the goal's check."""
        return bool(self.goal.verify_command) or self.goal_on()

    def needs_check(self) -> bool:
        """A goal is set, its check is the script, and there is none yet."""
        return self.goal_on() and not self.goal.verify_command and not self.check_file().is_file()

    def check_file(self) -> Path:
        return self.goal.workdir / ".baton" / CHECK_FILE

    def gate_due(self, with_goal: bool) -> bool:
        """Whether the gate that is wanted can run. The end of a cycle always runs it while a goal
        is set (with no check it fails, and the run stays in plan); any other gate, the one before
        round 1 and the periodic one, needs a check that exists."""
        if self.goal.verify_command or (with_goal and self.check_file().is_file()):
            return True
        return with_goal and self.cycle_gate

    def check_hash(self) -> str:
        """The hash of the script, "" when there is none."""
        try:
            return hashlib.sha256(self.check_file().read_bytes()).hexdigest()
        except OSError:
            return ""

    def lock_check(self) -> None:
        """Save the script's hash, and keep its first version: what the plan wrote is the check."""
        try:
            data = self.check_file().read_bytes()
        except FileNotFoundError:
            self.check_lock = ""
            return
        except OSError:
            return
        self.check_lock = hashlib.sha256(data).hexdigest()
        try:
            self.store.keep_first_check(data.decode("utf-8", errors="replace"))
        except OSError as exc:
            self.emit("error", error=f"could not keep the first check: {exc}")

    def check_differs_from_lock(self) -> bool:
        """Whether the script is not the one a plan round saved. With no lock saved a script that
        exists is not one a plan wrote, so it differs; with none at all, nothing does."""
        return self.check_hash() != self.check_lock

    def run_check(self, with_goal: bool) -> tuple[bool, str]:
        """Run the gate. A goal file that is not what the goal was set with, or a script that is
        not the one a plan round saved, fails it, and alerts."""
        changed = self.terms_changed() if with_goal else ""
        if changed:
            self.emit(
                "alert",
                message=f"the goal's terms changed: {changed}. The gate fails until the goal is "
                "set again (`baton: goal` or `baton goal`)",
            )
            return False, f"the goal's terms changed: {changed}"
        if with_goal and not self.goal.verify_command and self.check_differs_from_lock():
            self.emit(
                "alert",
                message=f"{CHECK_CHANGED}: the gate fails until a plan round takes it as it is",
            )
            return False, CHECK_CHANGED
        return run_gate(self.goal, should_kill=self.stop_now, goal_set=with_goal)

    def check_text(self) -> tuple[str, str]:
        """The check as an approver reads it, and what it is called: the verify command, or the
        script ("" when there is none)."""
        verify = (self.goal_terms or {}).get("verify") or self.goal.verify_command
        if verify:
            return verify, "the verify command"
        try:
            text = self.check_file().read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "", ""
        return text, f"`.baton/{CHECK_FILE}`"

    def check_change(self) -> str:
        """How the script differs from its first version, "" when it does not or cannot say."""
        if self.goal.verify_command:
            return ""
        text, _ = self.check_text()
        return check_diff(self.store.first_check() or "", text)

    def set_check_aside(self) -> None:
        """A goal ended or changed: its check goes to `.baton/archive/`, and the lock and the first
        version go with it. The next goal writes a check of its own."""
        self.check_lock = ""
        try:
            archive_check(self.goal.workdir, self.clock())
            self.store.drop_first_check()
        except OSError as exc:
            self.emit("error", error=f"could not set the check aside: {exc}")

    def count_failed_cycle(self) -> None:
        """A cycle ended with a failed gate or a rejection. Baton never gives up: every few of them
        it only says so."""
        self.failed_cycles += 1
        if self.failed_cycles % NO_PROGRESS_EVERY == 0:
            self.emit("alert", message=f"no progress in {self.failed_cycles} cycles: still working")

    def add_note(self, text: str) -> None:
        """A note baton writes for the next round to read, like one from the person."""
        new, _ = self.shorten_note(f"{text.strip()}\n\n")
        self.carried_notes += new
        entry = {"seq": self.newest_note() + 1, "text": new}
        self.note_log = [*self.note_log, entry][-NOTE_LOG_MAX:]

    def start_consensus(self) -> None:
        """The check passed: every agent that can run is now to check the goal, one at a time.
        What they approve is the state it passed in: the tree and the check as they are now."""
        assert self.sched is not None
        sched = self.sched
        pending = [n for n in sched.order if sched.can_run(n)]
        self.consensus = {
            "pending": pending,
            "approved": [],
            "tree": tree_fingerprint(self.goal.workdir),
            "check_hash": self.check_hash() if not self.goal.verify_command else "",
        }
        self.incomplete_rounds = 0
        self.consensus_wait = ""
        self.ps = replace(self.ps, phase=CONSENSUS, cycles=0, stuck=0, last_open=[])
        self.emit(
            "phase",
            phase=CONSENSUS,
            cycle=0,
            reason="the check passed: every agent now checks the goal",
        )
        left = [n for n in sched.order if n not in pending]
        if left:
            self.notice(f"left out of consensus, as they cannot run: {', '.join(left)}")
        self.persist_softly()

    def leave_consensus(self, phase: str) -> None:
        """Consensus ends without a verdict (the goal changed or was cleared): back to `phase`."""
        self.consensus = None
        self.consensus_wait = ""
        self.ps = replace(self.ps, phase=phase, cycles=0, stuck=0, last_open=[])

    def consensus_drift(self) -> str:
        """What changed since the check passed, in words: "" when nothing did. The approvals given
        are for that state: a different tree or check voids them, whoever changed it and whether
        or not the round that did it completed."""
        if self.consensus is None:
            return ""
        found = []
        tree = tree_fingerprint(self.goal.workdir)
        before = self.consensus.get("tree")
        if tree is None or before is None:
            found.append("the working tree could not be read, so nothing can be approved")
        elif tree != before:
            paths = changed_paths(self.goal.workdir)
            shown = ", ".join(paths[:CHANGED_PATHS_SHOWN]) or "a commit moved HEAD"
            more = f" and {len(paths) - CHANGED_PATHS_SHOWN} more" if len(paths) > 10 else ""
            found.append(f"the code changed ({shown}{more})")
        if not self.goal.verify_command and self.check_hash() != self.consensus.get(
            "check_hash", ""
        ):
            found.append("the check changed")
        terms = self.terms_changed()
        if terms:
            found.append(f"the goal's terms changed ({terms})")
        return "; ".join(found)

    def consensus_step(self) -> int | None:
        """One step of consensus: the person's words end it; otherwise the next agent that has
        not approved is asked, or waited for."""
        assert self.sched is not None and self.consensus is not None
        sched, state = self.sched, self.consensus
        if not self.goal_on():
            self.leave_consensus(PLAN)
            self.emit("phase", phase=PLAN, cycle=0, reason="the goal was cleared: no approval")
            self.persist_softly()
            return None
        if self.take_next():
            self.notice(
                "next is ignored during consensus: the agents approving the goal do not take turns"
            )
        if self.owes_round or self.unread_notes():
            self.reject_consensus(
                "", "your message ends the approval: back to build", MESSAGE_TASK, count=False
            )
            return None
        pending = [n for n in state["pending"] if n not in sched.dropped]
        for name in state["pending"]:
            if name not in pending:
                self.notice(f"{name} is left out of consensus: it left the queue for this goal")
        if pending != state["pending"]:
            state["pending"] = pending
            self.persist_softly()
        if not pending:
            return self.consensus_close()
        name = pending[0]
        adapter = sched.pick_for(name)
        sched.save()
        if adapter is None:
            if not sched.can_run(name):  # it was able to, and is not now: it is left out
                state["pending"] = [n for n in pending if n != name]
                self.notice(f"{name} is left out of consensus: it cannot run")
                self.persist_softly()
                return None
            self.consensus_waits(f"waiting for {name} to approve")
            return None
        self.consensus_wait = ""
        self.run_round(adapter)
        return None

    def consensus_waits(self, reason: str) -> None:
        """Wait for the agents, saying why only when the reason is a new one. A person's message,
        `next` or a stop ends the wait."""
        assert self.sched is not None
        until = self.sched.next_wake()
        if reason != self.consensus_wait:
            self.consensus_wait = reason
            self.emit(
                "wait",
                reason=reason,
                until=until,
                seconds=max(0.0, until - self.clock()),
                agents=self.sched.snapshot()["agents"],
            )
        self.nap_until(until, wake_on_next=True, wake_on_note=True)

    def consensus_close(self) -> int | None:
        """Nobody is pending. The goal ends only if, right now, it is still the person's goal, every
        agent that can run has approved, and the work is as the check passed it. Anyone who can run
        and has not approved is asked first."""
        assert self.sched is not None and self.consensus is not None
        sched, state = self.sched, self.consensus
        self.sync_goal()  # a goal changed or cleared in the meantime ends this consensus
        if self.consensus is None or not self.goal_on():
            return None
        able = [n for n in sched.order if sched.can_run(n)]
        if not able:
            self.consensus_waits("no agent can approve the goal")
            return None
        missing = [n for n in able if n not in state["approved"]]
        if missing:
            state["pending"] = missing
            self.persist_softly()
            return None
        drift = self.consensus_drift()
        if drift:
            self.reject_consensus(
                f"The work changed after the check passed, so the approvals are void: {drift}.",
                "the work changed since the check passed: back to build",
                CHANGED_TASK,
            )
            return None
        # The tree cannot see what git ignores (a build, an output): the check says it.
        passed, tail = self.run_check(True)
        self.gate_tail = tail
        self.emit("gate", passed=passed, tail=tail)
        if self.sync_goal() or self.consensus is None:
            return None  # the goal changed while it ran: this result is not for the new one
        if tail.rstrip().endswith("[verify stopped]"):
            return None  # a stop was asked for: the loop honours it, and this runs again
        if not passed:
            self.reject_consensus(
                f"Every agent approved, but the check no longer passes:\n{tail.strip()}",
                "the check no longer passes: back to build",
                CHECK_TASK,
            )
            return None
        return self.achieve()

    def after_consensus(
        self, agent: str, completed: bool, spawned: bool, drift: str, trusted: bool
    ) -> None:
        """A consensus round ended. A round that changed the work (`drift`, completed or not),
        a missing verdict and CHANGES reject the goal; APPROVED takes `agent` out of the pending.
        A round that did not complete is retried, and three in a row count as one failed cycle."""
        if self.consensus is None:
            return
        text = read_review(self.goal.workdir) if trusted else ""
        wrote = f"\nWhat it wrote:\n{text.strip()}" if text.strip() else ""
        if drift:
            try:
                add_task(self.goal.workdir, CHANGED_TASK)
            except OSError as exc:
                self.emit("error", error=f"could not add the review task: {exc}")
            self.reject_consensus(
                f"{agent} rejected the goal: while it checked, {drift}.{wrote}",
                f"{agent} rejected: back to build",
                "",
            )
        elif not completed:
            if spawned:
                self.incomplete_rounds += 1
                if self.incomplete_rounds >= INCOMPLETE_LIMIT:
                    self.incomplete_rounds = 0
                    self.count_failed_cycle()
        elif verdict(text) == "approved":
            self.incomplete_rounds = 0
            state = self.consensus
            state["pending"] = [n for n in state["pending"] if n != agent]
            if agent not in state["approved"]:
                state["approved"].append(agent)
        else:
            self.reject_consensus(
                f"{agent} rejected the goal.{wrote}", f"{agent} rejected: back to build"
            )

    def reject_consensus(
        self, note: str, line: str, task: str = REJECTION_TASK, count: bool = True
    ) -> None:
        """Consensus ends: `note` (what was wrong) is left for the build that follows, `task` goes
        to the plan, `line` is said in the chat, and the run goes back to build. A rejection counts
        as a failed cycle; the person's message does not (`count=False`)."""
        if task:
            try:
                add_task(self.goal.workdir, task)
            except OSError as exc:
                self.emit("error", error=f"could not add the consensus task: {exc}")
        if note.strip():
            self.add_note(f"[baton] {note.strip()}")
        if count:
            self.count_failed_cycle()
        self.notice(line)
        self.consensus = None
        self.consensus_wait = ""
        open_now = open_tasks(read_plan(self.goal.workdir))
        self.ps = replace(self.ps, phase=BUILD, cycles=0, stuck=0, last_open=list(open_now))
        self.seen_open = list(open_now)
        self.build_idle = 0
        self.build_rounds = 0
        self.build_limit = len(open_now) * BUILD_ROUNDS_PER_TASK + BUILD_ROUNDS_EXTRA
        self.persist_softly()

    def achieve(self) -> int:
        """Every agent that could run has approved, the work is as the check passed it, and the
        goal is still the person's: it is done."""
        assert self.consensus is not None
        approved = ", ".join(self.consensus["approved"])
        self.consensus = None
        self.ps = replace(self.ps, phase=PLAN, cycles=0, stuck=0, last_open=[])
        self.set_check_aside()
        try:
            self.store.archive_goal()
        except OSError as exc:
            self.emit("error", error=f"could not archive the goal: {exc}")
        self.notice(f"goal achieved: check passed, approved by {approved}")
        self.persist_softly()
        return self.finish("goal achieved")

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
        carried, collected, log = self.carried_notes, self.collected_inbox, self.note_log
        new, filed = self.shorten_note(text.removeprefix(collected))
        self.carried_notes += new
        if new.strip():
            entry = {"seq": self.newest_note() + 1, "text": new}
            self.note_log = [*log, entry][-NOTE_LOG_MAX:]
        self.collected_inbox = text
        if not self.persist_softly():
            self.carried_notes, self.collected_inbox, self.note_log = carried, collected, log
            if filed is not None:
                filed.unlink(missing_ok=True)  # the note is collected again, and filed again
            return True
        self.store.clear_inbox(text)
        self.collected_inbox = ""
        self.persist_softly()
        return False

    def newest_note(self) -> int:
        return self.note_log[-1]["seq"] if self.note_log else 0

    def shorten_note(self, text: str) -> tuple[str, Path | None]:
        return file_large_note(self.goal.workdir, text, self.clock())

    def notes_since(self, mark: int | None) -> str:
        """The notes a round reads: every one after the conversation's `mark`, or, for a
        conversation with none (a fresh one, which holds the talk in its objective, and a review),
        only the ones no agent has read."""
        if mark is not None:
            text = "".join(e["text"] for e in self.note_log if e["seq"] > mark)
            if text.strip():
                return text
        return self.carried_notes

    def stop_now(self) -> bool:
        return self.stop_requested() == "now"

    # one round

    def run_round(self, adapter: Adapter) -> bool:
        """Run one round for `adapter`. True when it ends the goal (no verify command)."""
        assert self.sched is not None
        goal, store = self.goal, self.store
        agent = adapter.name
        if self.archive_next:
            self.archive_next = False
            self.archive_cycle_files()
        gate_was_due = self.need_gate
        # With phases the gate waits for the end of a cycle; without them it follows every round.
        self.need_gate = not self.phases
        phase = self.ps.phase if self.phases else ""
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
        newest_note = self.newest_note()
        handoff = self.read_handoff()
        # The conversation of an agent is tied to its model: per role with phases, and a review
        # is always a fresh one.
        key = agent
        if self.phases:
            key = f"{agent}:{phase}" if phase not in (REVIEW, CONSENSUS) else ""
        sid = self.sessions.get(key) if key else None
        notes = self.notes_since(self.note_seen.get(key) if key and sid else None)
        # What changed since this agent's last round in this role: kept per session key, with the
        # review (which has no session) under its own.
        head_key = key or f"{agent}:{phase}"
        since = commits_since(goal.workdir, self.heads.get(head_key, ""))
        head_before = _head(goal.workdir) if self.memory() is not None else ""
        recalled = self.recall(n)
        if self.mem is not None and self.stop_requested():
            # A stop that came while memory was being read: no process is spawned for this round,
            # so it does not exist. The counter and the state go back, and the loop honours it.
            self.round_no = n - 1
            self.need_gate = gate_was_due
            self.persist_softly()
            return False

        review_trusted, tree_before = True, None
        if phase in (REVIEW, CONSENSUS):
            # Immediately before the round: no earlier verdict, an old APPROVED or one a builder
            # wrote, can be read afterwards. And what the tree holds now, to see whether the
            # reviewer changes the code (a consensus round is judged against the tree the check
            # passed in).
            review_trusted = clear_review(goal.workdir)
        if phase == REVIEW:
            tree_before = tree_fingerprint(goal.workdir)

        buf = ""
        mid_line = False  # the text shown so far stops short of a line end
        worked = False
        killed = False
        interrupted = False
        preempted = False
        interrupt_at: float | None = None  # a note is waiting for the tool in flight to end
        tools = None  # this round's tool watch, None when the adapter has none or it failed
        spent = 0.0  # what the round cost, whichever attempt of it reported it
        announced = self.announced == agent
        self.announced = ""
        watch = (
            PreemptionWatch(self.sched, self.adapters, PREEMPT_POLL_S)
            if getattr(adapter, "reserve", False) and phase != CONSENSUS
            else None
        )

        def close_line() -> None:
            nonlocal buf, mid_line
            if mid_line:
                buf = (buf + "\n")[-ROUND_TAIL_CHARS:]
                self.emit("output", agent=agent, round=n, text="\n", fragment=True)
                mid_line = False

        def on_output(line: str) -> None:
            nonlocal buf, worked, mid_line, tools
            if tools is not None:
                try:
                    tools.feed(line)
                except Exception:
                    tools = None
            fragment = False
            try:
                text = adapter.render(line)
                is_work = getattr(adapter, "is_work", None)
                if text and (is_work is None or is_work(line)):
                    worked = True
                is_fragment = getattr(adapter, "is_fragment", None)
                fragment = bool(text and is_fragment is not None and is_fragment(line))
            except Exception:
                text = None
            if not text:
                return
            if fragment:
                # Joined with the next one by the reader, which adds no newline of its own.
                buf = (buf + text)[-ROUND_TAIL_CHARS:]
                mid_line = not text.endswith("\n")
                self.emit("output", agent=agent, round=n, text=text, fragment=True)
                return
            close_line()
            buf = (buf + text + "\n")[-ROUND_TAIL_CHARS:]
            self.emit("output", agent=agent, round=n, text=text)

        def tool_running() -> bool:
            nonlocal tools
            if tools is None:
                return False
            try:
                return tools.busy
            except Exception:
                tools = None
                return False

        def should_kill() -> bool:
            nonlocal killed, interrupted, preempted, interrupt_at
            if killed:
                return True
            try:
                if store.stop_request() == "now":
                    killed = True
                elif interrupt_at is None and store.take_interrupt():
                    interrupt_at = self.clock()
            except OSError:
                pass
            # A note ends the round between two tool calls, or after MESSAGE_WAIT_S at the latest.
            if (
                not killed
                and interrupt_at is not None
                and (not tool_running() or self.clock() - interrupt_at >= MESSAGE_WAIT_S)
            ):
                killed = interrupted = True
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

        def on_subagent(tool: str, model: str | None) -> None:
            self.emit("subagent", agent=agent, round=n, tool=tool, model=model)

        def on_session(session: str) -> None:
            if not key:
                return  # a review keeps no conversation
            self.sessions[key] = session
            # A conversation that begins here has the notes up to now in its objective or prompt.
            self.note_seen.setdefault(key, newest_note)
            self.persist_softly()

        def attempt(session: str | None) -> RoundResult:
            nonlocal tools, spent
            make_watch = getattr(adapter, "tool_watch", None)
            try:
                tools = make_watch() if make_watch is not None else None
            except Exception:
                tools = None
            prompt = build_prompt(
                goal if self.objective is None else replace(goal, objective=self.objective()),
                self.gate_tail,
                notes,
                handoff,
                "" if phase == CONSENSUS else self.round_tail,  # an approver reads the work, not it
                resumed=session is not None,
                since_commits=since,
                memory=recalled,
                phase_block=self.phase_block(phase),
                pinned_goal=self.store.goal(),
                goal_check=self.goal_on() and not goal.verify_command,
                # A resumed conversation already holds the methods from its first round, except
                # that the plan round after a failed gate always gets systematic-debugging.
                skills=self.skills_block(agent, phase, fresh=session is None),
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
                tier=TIER[phase] if phase else "strong",
                on_subagent=on_subagent,
            )
            self.emit(
                "round_start",
                round=n,
                agent=agent,
                resumed=session is not None,
                previous=None if phase == CONSENSUS else self.prev_agent,
                **({"announced": True} if announced else {}),
                **({"consensus": True} if phase == CONSENSUS else {}),
            )
            try:
                attempted = adapter.run_round(prompt, ctx)
                spent += attempted.cost_usd
            except Exception as exc:
                close_line()  # before any other event of this attempt
                self.emit("error", error=f"{agent} round failed: {type(exc).__name__}: {exc}")
                return RoundResult(TRANSIENT, reason=f"adapter error: {type(exc).__name__}")
            close_line()
            return attempted

        started = self.clock()
        if watch is not None:
            watch.start()
        try:
            result = attempt(sid)
            if result.resume_failed and sid is not None:
                self.sessions.pop(key, None)
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

        # A round cut off by its own cap (bounded) is complete for the scheduler, not for the work.
        completed = result.outcome == COMPLETED and not killed and not result.bounded
        # A reviewer is not to change the code: compared before baton's own snapshot commit.
        void = False
        drift = ""
        if phase == REVIEW and completed:
            tree_after = tree_fingerprint(goal.workdir)
            void = tree_before is None or tree_after is None or tree_before != tree_after
        elif phase == CONSENSUS and spawned:
            drift = self.consensus_drift()  # completed or not: what it left is what is there

        self.group = None
        ended = self.clock()
        # Record first: nothing after this can lose a credit_guard or limit outcome.
        self.sched.record(agent, result, seconds=ended - started)
        # A consensus round is a lent one: its cost counts for the goal, not for the agent's turn.
        self.sched.note_round(
            agent, ended - started, spent, goal=bool(self.goal_seen), turn=phase != CONSENSUS
        )
        if result.outcome in (CREDIT_GUARD, AUTH, SUBAGENT):
            self.emit("alert", agent=agent, outcome=result.outcome, reason=result.reason)
        snapshotted = snapshot_work(goal, n, agent)
        if phase == CONSENSUS and spawned and not drift and self.consensus is not None:
            # A clean round: baton's own snapshot may have committed what the check left behind,
            # and that is the tree the next approver is judged against.
            self.consensus["tree"] = tree_fingerprint(goal.workdir)
        cycle_ended = False
        if phase != CONSENSUS and self.phases:
            cycle_ended = self.after_round(phase, completed, void, review_trusted, spawned)
            if spawned and phase == BUILD:
                self.builder = agent
        # A process that exists has not read its prompt: one that dies at once with a 429, or is
        # ended before it says anything, leaves the notes and the context for the next round.
        finished_its_turn = result.outcome in (COMPLETED, TIMEOUT) and not killed
        if spawned and (worked or finished_its_turn):
            self.carried_notes = ""
            if key:
                self.note_seen[key] = max(self.note_seen.get(key, 0), newest_note)
            self.owes_round = False
            if phase != CONSENSUS:
                self.round_tail = buf
        if spawned:
            if phase != CONSENSUS:
                self.prev_agent = agent  # an approver takes nobody's place in the relay
            head = _head(goal.workdir)
            if head:
                self.heads[head_key] = head
        # A round that ends at once, or that baton ended for a subagent on a forbidden model, is
        # followed by a pause, so nothing can loop and burn usage.
        quick = result.outcome == COMPLETED and not killed and ended - started < MIN_ROUND_S
        self.pause_until = ended + MIN_ROUND_S if quick or result.outcome == SUBAGENT else 0.0
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
            cost_usd=round(spent, 6),
            **({"phase": phase} if self.phases else {}),
        )
        if phase == CONSENSUS:
            # After the notes were marked read: a rejection leaves one for the build that follows.
            self.after_consensus(agent, completed, spawned, drift, review_trusted)
        self.end_turn(agent, spent, rotate=phase != CONSENSUS)
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
                phase=phase,
            )
            if self.gated():
                self.save_pending()  # the gate may never finish: see `step`
            else:
                self.record_pending()
        if self.phases:
            return cycle_ended and not self.gated()
        return not goal.verify_command and completed

    def skills_block(self, agent: str, phase: str, fresh: bool) -> str:
        if self.skills is None:
            return ""
        debugging = self.from_gate and phase == PLAN
        if fresh:
            # An approver reads the work like a reviewer.
            return self.skills.block(agent, REVIEW if phase == CONSENSUS else phase, debugging)
        return self.skills.debug_block() if debugging else ""

    def load_skills(self) -> bool:
        """Read the Superpowers skills, once they are there. False, with `skills_why` saying what
        is wrong, when they are missing, unreadable or too big for a prompt."""
        try:
            found = superpowers.find_skills(load_superpowers())
            if found is not None:
                self.skills = superpowers.Skills.load(found)
                return True
            self.skills_why = "Superpowers was not found"
        except superpowers.SkillsError as exc:
            self.skills_why = f"Superpowers cannot be used: {exc}"
        except Exception as exc:
            self.skills_why = f"Superpowers could not be read ({exc})"
        return False

    def phase_block(self, phase: str) -> str:
        """The instructions of the phase for this round's prompt, "" without phases."""
        if not phase:
            return ""
        tasks = open_tasks(read_plan(self.goal.workdir))
        approving: dict[str, str] = {}
        if phase == CONSENSUS:
            text, label = self.check_text()
            approving = {
                "check_diff": self.check_change(),
                "check_text": text,
                "check_label": label,
                "first_path": str(self.store.first_check_path()),
            }
        return phase_prompt(
            phase,
            tasks[0] if tasks else "",
            self.ps.base,
            write_check=self.goal_on() and not self.goal.verify_command,
            **approving,
        )

    def after_round(
        self, phase: str, completed: bool, void: bool, trusted: bool, spawned: bool
    ) -> bool:
        """Move the cycle on after a round; True when the cycle ended (the gate's moment).

        Only a round that completed moves the phase: a preempted, killed, limited, timed-out or
        failed one is repeated by whoever runs next, in the same phase. But every round that ran
        is counted, so that a phase that never completes still reaches the gate (every third round
        without a phase change) and a build that closes nothing still reaches review."""
        goal = self.goal
        old = self.ps
        open_now = open_tasks(read_plan(goal.workdir))
        reason = ""
        ended = False
        review_text = ""
        if completed:
            if phase == REVIEW:
                if void:
                    # What the reviewer did to the code is left where it is; it is a task to review.
                    self.emit(
                        "phase",
                        phase=REVIEW,
                        cycle=old.cycles,
                        reason="reviewer changed the code",
                    )
                    try:
                        add_review_task(goal.workdir)
                    except OSError as exc:
                        self.emit("error", error=f"could not add the review task: {exc}")
                    review_text = "CHANGES"
                elif trusted:
                    review_text = read_review(goal.workdir)
            if phase == PLAN:
                self.from_gate = False  # the strong model has had its look at the failed gate
                if not goal.verify_command and self.goal_on():
                    self.lock_check()  # what the plan left is the check, from here on
            if void:
                open_now = open_tasks(read_plan(goal.workdir))
            if phase == PLAN and open_now and self.needs_check():
                # A goal is judged by its check: until the plan has written one, nothing is built.
                self.ps, ended = old, False
                reason = f"no check yet: the plan stays until `.baton/{CHECK_FILE}` exists"
                self.notice(reason)
            else:
                self.ps, ended = advance(
                    old, outcome_completed=True, open_now=open_now, review_text=review_text
                )
                reason = reason_for(old, self.ps, ended, open_now=open_now, review_text=review_text)
        if spawned and phase == BUILD:
            self.count_build_round(open_now)
            if not ended and self.ps.phase == BUILD:
                if self.build_idle >= BUILD_IDLE_ROUNDS:
                    reason = f"no task closed in {BUILD_IDLE_ROUNDS} rounds in a row"
                elif self.build_limit and self.build_rounds >= self.build_limit:
                    reason = f"build round cap reached ({self.build_limit} rounds)"
                if reason.startswith(("no task closed", "build round cap")):
                    self.ps = replace(self.ps, phase=REVIEW, stuck=0, last_open=list(open_now))
        if self.ps.phase == BUILD and old.phase != BUILD:
            # The build of this cycle begins (or resumes after a review).
            self.seen_open = list(open_now)
            self.build_idle = 0
            if old.phase == PLAN or not self.build_limit:
                self.build_rounds = 0
            # The cap is renewed whenever a review sends the build back: the rounds already
            # spent, plus what the tasks open now are worth.
            self.build_limit = (
                self.build_rounds + len(open_now) * BUILD_ROUNDS_PER_TASK + BUILD_ROUNDS_EXTRA
            )
        if self.ps.phase == REVIEW and old.phase != REVIEW and not _is_repo(goal.workdir):
            # Nothing to diff and nothing to judge a reviewer by: the review is skipped.
            if not self.warned_no_repo:
                self.warned_no_repo = True
                self.emit(
                    "alert",
                    message="the working directory is not a git repository: the review phase is "
                    "skipped, and the cycle ends after build",
                )
            self.ps = replace(self.ps, phase=PLAN, cycles=0, stuck=0, last_open=[])
            ended = True
            reason = "the working directory is not a git repository: review skipped"
        if ended:
            self.need_gate = True
            self.cycle_gate = True
            self.build_idle = self.build_rounds = self.build_limit = 0
            self.seen_open = []
            if not self.gated():
                self.ps = replace(self.ps, base=_head(goal.workdir))
                self.archive_next = True
        if ended or self.ps.phase != old.phase:
            self.idle_rounds = 0
            self.emit("phase", phase=self.ps.phase, cycle=self.ps.cycles, reason=reason)
        elif spawned:
            self.idle_rounds += 1
            if self.idle_rounds >= GATE_EVERY_ROUNDS and self.gated():
                self.need_gate = (
                    True  # not the end of a cycle: a pass finishes, a fail changes nothing
                )
        return ended

    def count_build_round(self, open_now: list[str]) -> None:
        """One more round in build: did it close a task, against what the round before left?"""
        closed = bool(Counter(self.seen_open) - Counter(open_now))
        self.build_idle = 0 if closed else self.build_idle + 1
        self.build_rounds += 1
        self.seen_open = list(open_now)

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
        if not limit or len(text) <= HANDOFF_MAX_CHARS:
            return text
        tail = HANDOFF_MAX_CHARS - HANDOFF_HEAD_CHARS
        left_out = len(text) - HANDOFF_HEAD_CHARS - tail
        marker = f"[{left_out} characters left out here; .baton/HANDOFF.md holds them]"
        return f"{text[:HANDOFF_HEAD_CHARS]}\n{marker}\n{text[-tail:]}"

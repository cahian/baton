"""Plan, build, review: the phase machine that sits on top of the rotation.

Who works is the scheduler's business. This module only says what the working agent does next and
with which of its two models. State lives in two files, `.baton/PLAN.md` and `.baton/REVIEW.md`,
so any provider can continue a cycle. Everything here is pure except reading (and, for the
review guard, appending to) those two files.
"""

from __future__ import annotations

import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path

PLAN, BUILD, REVIEW = "plan", "build", "review"
PHASES = (PLAN, BUILD, REVIEW)
TIER = {PLAN: "strong", BUILD: "build", REVIEW: "strong"}
MAX_CYCLES = 3
# A completed build round that closes no task this many times in a row ends the build phase.
STUCK_LIMIT = 2

PLAN_FILE = "PLAN.md"
REVIEW_FILE = "REVIEW.md"
# What baton adds to the plan when the reviewer changed the code: that work needs a second look.
REVIEW_TASK = "- [ ] Review the changes made during review"

_OPEN_RE = re.compile(r"^\s*- \[ \]")


def open_tasks(text: str) -> list[str]:
    """The open tasks of a plan, as their lines (stripped), in order."""
    return [line.strip() for line in (text or "").splitlines() if _OPEN_RE.match(line)]


def verdict(text: str) -> str:
    """ "approved" when the first non-blank line starts with APPROVED (any case), else "changes":
    a missing, empty or malformed review never approves."""
    for line in (text or "").splitlines():
        if line.strip():
            return "approved" if line.lstrip("﻿ \t").upper().startswith("APPROVED") else "changes"
    return "changes"


@dataclass
class PhaseState:
    phase: str = PLAN
    base: str = ""  # the commit the plan started from: what the reviewer diffs against
    cycles: int = 0  # review rounds that asked for changes in this cycle
    stuck: int = 0  # completed build rounds in a row that closed no task
    last_open: list[str] = field(default_factory=list)  # open tasks after the last round

    def to_json(self) -> dict:
        return {
            "phase": self.phase,
            "base": self.base,
            "cycles": self.cycles,
            "stuck": self.stuck,
            "last_open": list(self.last_open),
        }

    @classmethod
    def parse(cls, raw: object) -> PhaseState | None:
        """The stored state, or None when it is missing or anything in it is bad."""
        if not isinstance(raw, dict):
            return None
        phase, base = raw.get("phase"), raw.get("base")
        cycles, stuck, last_open = raw.get("cycles"), raw.get("stuck"), raw.get("last_open")
        for n in (cycles, stuck):
            if isinstance(n, bool) or not isinstance(n, int) or n < 0:
                return None
        if (
            phase not in PHASES
            or not isinstance(base, str)
            or not isinstance(last_open, list)
            or not all(isinstance(t, str) for t in last_open)
        ):
            return None
        return cls(phase, base, cycles, stuck, list(last_open))

    @classmethod
    def from_json(cls, raw: object) -> PhaseState:
        """The stored state, or the start state when it is missing or anything in it is bad."""
        return cls.parse(raw) or cls()


def _ended(state: PhaseState) -> PhaseState:
    """The state after a cycle ends: back at plan with the counters cleared, base kept."""
    return replace(state, phase=PLAN, cycles=0, stuck=0, last_open=[])


def advance(
    state: PhaseState,
    *,
    outcome_completed: bool,
    open_now: list[str],
    review_text: str,
    max_cycles: int = MAX_CYCLES,
) -> tuple[PhaseState, bool]:
    """The next state after a round, and whether the cycle ended (the gate's moment).

    A round that did not complete (preempted, killed, limited, failed) changes nothing, so the
    next agent repeats that work at the same tier. `open_now` is the plan's open tasks after the
    round; `review_text` is REVIEW.md after a review round ("" when there is none).
    """
    if not outcome_completed:
        return state, False
    if state.phase == PLAN:
        if not open_now:
            return _ended(state), True  # nothing to build: the request was answered or done
        return replace(state, phase=BUILD, stuck=0, last_open=list(open_now)), False
    if state.phase == BUILD:
        closed = bool(Counter(state.last_open) - Counter(open_now))
        stuck = 0 if closed else state.stuck + 1
        if not open_now or stuck >= STUCK_LIMIT:
            return replace(state, phase=REVIEW, stuck=0, last_open=list(open_now)), False
        return replace(state, stuck=stuck, last_open=list(open_now)), False
    # review
    if verdict(review_text) == "approved":
        return _ended(state), True
    cycles = state.cycles + 1
    if cycles >= max_cycles:
        return _ended(state), True
    return replace(state, phase=BUILD, cycles=cycles, stuck=0, last_open=list(open_now)), False


def reason_for(
    old: PhaseState,
    new: PhaseState,
    ended: bool,
    *,
    open_now: list[str],
    review_text: str,
    max_cycles: int = MAX_CYCLES,
) -> str:
    """One short sentence for the `phase` event of a transition."""
    if old.phase == PLAN:
        if ended:
            return "the plan has no open task: the cycle ends"
        return f"plan written: {len(open_now)} open task(s)"
    if old.phase == BUILD:
        if not open_now:
            return "every task is closed"
        return f"stuck: no task closed in {STUCK_LIMIT} rounds in a row"
    if verdict(review_text) == "approved":
        return "review approved: the cycle ends"
    if ended:
        return f"review cycle cap reached ({max_cycles}): the cycle ends"
    return f"review asked for changes (cycle {new.cycles} of {max_cycles})"


def phase_prompt(phase: str, next_task: str, base: str) -> str:
    """The instructions for this phase, appended to the round's prompt."""
    if phase == PLAN:
        return (
            "Phase: PLAN (you are the strong model; think, do not build).\n"
            "- Study the objective, the verify output above and the code.\n"
            "- Write `.baton/PLAN.md`: one line `- [ ] <task>` per task, each small enough for "
            "one round of a weaker model. If the file already exists, keep it: leave closed "
            "tasks as `- [x]` and add or reopen what is still missing.\n"
            "- If the request is small enough to answer or do at once, just do it and write no "
            "open task: the cycle ends there.\n"
            "- Do not mark tasks closed that you did not do."
        )
    if phase == BUILD:
        task = next_task.strip()
        which = (
            f"Your one task, the first open line of `.baton/PLAN.md`:\n{task}\n"
            if task
            else "`.baton/PLAN.md` has no open task: read `.baton/REVIEW.md` and do what it "
            "asks, adding each item to the plan as `- [ ]` and closing it.\n"
        )
        return (
            "Phase: BUILD (do one task only).\n"
            f"{which}"
            "- Do this task and no other. Do not start the next one.\n"
            "- A note from the person above outranks this task: do what it asks first.\n"
            "- When it is done and committed, mark it `- [x]` in `.baton/PLAN.md`.\n"
            "- Do not write `.baton/REVIEW.md`: only the reviewer does."
        )
    where = f"`git diff {base}..HEAD`" if base else "`git log -p` and `git status` (no base commit)"
    return (
        "Phase: REVIEW (you are a fresh reviewer; do not edit code).\n"
        f"- Inspect what was done since the plan with {where} and read `.baton/PLAN.md`.\n"
        "- Do NOT edit, create or delete any file except `.baton/REVIEW.md` and `.baton/PLAN.md`: "
        "a review that changes the code is void.\n"
        "- Write `.baton/REVIEW.md`. Its first line must be `APPROVED` or `CHANGES`.\n"
        "- For CHANGES, say what is wrong and add one new `- [ ]` line per fix to "
        "`.baton/PLAN.md`."
    )


# the two files


def _read(workdir: Path, name: str) -> str:
    try:
        return (workdir / ".baton" / name).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def read_plan(workdir: Path) -> str:
    return _read(workdir, PLAN_FILE)


def read_review(workdir: Path) -> str:
    return _read(workdir, REVIEW_FILE)


def clear_review(workdir: Path) -> bool:
    """Delete REVIEW.md before a review round, so no earlier verdict can be read afterwards.
    True when it is gone (or never was); False when it could not be removed."""
    try:
        os.unlink(workdir / ".baton" / REVIEW_FILE)
    except FileNotFoundError:
        pass
    except OSError:
        return False
    return True


def add_review_task(workdir: Path) -> None:
    """Append the follow-up task to PLAN.md, once. Raises OSError when it cannot be written."""
    path = workdir / ".baton" / PLAN_FILE
    text = read_plan(workdir)
    if REVIEW_TASK in open_tasks(text):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    sep = "" if not text or text.endswith("\n") else "\n"
    path.write_text(f"{text}{sep}{REVIEW_TASK}\n", encoding="utf-8")


def archive_plan(workdir: Path, now: float) -> None:
    """Move PLAN.md and REVIEW.md to `.baton/archive/<timestamp>-<name>`, when a new cycle begins:
    a plan from an earlier goal, or one a finished cycle left behind, is not this cycle's. Raises
    OSError when a file cannot be moved."""
    base = workdir / ".baton"
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(now))
    for name in (PLAN_FILE, REVIEW_FILE):
        source = base / name
        if not source.is_file():
            continue
        target_dir = base / "archive"
        target_dir.mkdir(parents=True, exist_ok=True)
        target, n = target_dir / f"{stamp}-{name}", 1
        while target.exists():
            n += 1
            target = target_dir / f"{stamp}-{n}-{name}"
        os.replace(source, target)

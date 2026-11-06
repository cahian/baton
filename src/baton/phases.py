"""Plan, build, review: the phase machine that sits on top of the rotation.

Who works is the scheduler's business. This module only says what the working agent does next and
with which of its two models. State lives in two files, `.baton/PLAN.md` and `.baton/REVIEW.md`,
so any provider can continue a cycle. Everything here is pure except reading (and, for the
review guard, appending to) those two files.
"""

from __future__ import annotations

import difflib
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path

PLAN, BUILD, REVIEW = "plan", "build", "review"
# A goal the person set ends in consensus: every agent that can run checks the work on its own.
CONSENSUS = "consensus"
PHASES = (PLAN, BUILD, REVIEW, CONSENSUS)
TIER = {PLAN: "strong", BUILD: "build", REVIEW: "strong", CONSENSUS: "strong"}
MAX_CYCLES = 3
# A completed build round that closes no task this many times in a row ends the build phase.
STUCK_LIMIT = 2

PLAN_FILE = "PLAN.md"
REVIEW_FILE = "REVIEW.md"
# The check of a goal that has no verify command: a script the plan writes, exit 0 meaning met.
CHECK_FILE = "VERIFY.sh"
# How much of the change to the check a consensus prompt carries.
CHECK_DIFF_CHARS = 12000
# How much of the check's own text a consensus prompt carries.
CHECK_TEXT_CHARS = 6000
# What baton adds to the plan when the reviewer changed the code: that work needs a second look.
REVIEW_TASK = "- [ ] Review the changes made during review"

_OPEN_RE = re.compile(r"^\s*- \[ \]")


def open_tasks(text: str) -> list[str]:
    """The open tasks of a plan, as their lines (stripped), in order."""
    return [line.strip() for line in (text or "").splitlines() if _OPEN_RE.match(line)]


_APPROVED_RE = re.compile(r"APPROVED(?:\s*$|\s*[.:\u2014-])", re.IGNORECASE)


def verdict(text: str) -> str:
    """ "approved" when the first non-blank line is APPROVED (any case) as a word, followed by
    nothing or by `.`, `:`, `-` or an em dash, else "changes": a missing, empty or malformed
    review never approves, and neither does `APPROVED only if ...`."""
    for line in (text or "").splitlines():
        if line.strip():
            return "approved" if _APPROVED_RE.match(line.lstrip("\ufeff \t")) else "changes"
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


def check_diff(first: str, now: str) -> str:
    """The unified diff from the first version of the check to what it is now: "" when they are
    the same, or when there is no first version to compare with. Cut to CHECK_DIFF_CHARS."""
    if not first or first == now:
        return ""
    lines = difflib.unified_diff(
        first.splitlines(),
        now.splitlines(),
        "VERIFY.sh (first version)",
        "VERIFY.sh (now)",
        lineterm="",
    )
    text = "\n".join(lines)
    if len(text) <= CHECK_DIFF_CHARS:
        return text
    return f"{text[:CHECK_DIFF_CHARS]}\n[{len(text) - CHECK_DIFF_CHARS} characters left out here]"


def phase_prompt(
    phase: str,
    next_task: str,
    base: str,
    *,
    write_check: bool = False,
    check_diff: str = "",
    check_text: str = "",
    check_label: str = "",
    first_path: str = "",
) -> str:
    """The instructions for this phase, appended to the round's prompt. `write_check` (a goal
    with no verify command) has the plan write the check. For the consensus prompt: `check_text`,
    the check's own text (`check_label` says which it is), always; `check_diff`, how it changed
    since its first version, and `first_path`, where that version is kept."""
    if phase == PLAN:
        check = (
            "- Write `.baton/VERIFY.sh`, the check of the goal: it must exit 0 only when the "
            "goal is met as the person would experience it, in the real setup, with no "
            "rounding. A check that measures a stand-in (a mock, a smaller input, a different "
            "setup) fails the check: do not write one. The check writes only under `.baton/` or "
            "outside the working directory: anything else it leaves in the tree counts as a "
            "change to the work. Only the plan may change it, and a weaker one is rejected.\n"
            if write_check
            else ""
        )
        return (
            "Phase: PLAN (you are the strong model; think, do not build).\n"
            "- Study the objective, the verify output above and the code.\n"
            f"{check}"
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
    if phase == CONSENSUS:
        text = check_text.strip()
        if len(text) > CHECK_TEXT_CHARS:
            left = len(text) - CHECK_TEXT_CHARS
            text = f"{text[:CHECK_TEXT_CHARS]}\n[{left} characters left out here]"
        shown = (
            f"- The check, {check_label or 'as it stands'}:\n```sh\n{text}\n```\n"
            "  Reject it (CHANGES) when it cannot fail (it always exits 0, or tests nothing the "
            "goal asks for) or when it measures a stand-in for what the person would "
            "experience.\n"
            if text
            else ""
        )
        where_first = f" (kept at `{first_path}`)" if first_path else ""
        changed = (
            f"- The check was changed since its first version{where_first}. This is the change:\n"
            f"```diff\n{check_diff}\n```\n"
            "  Reject it (CHANGES) when the check is weaker than the first version, however it "
            "is worded, unless the goal itself changed.\n"
            if check_diff
            else ""
        )
        return (
            "Phase: CONSENSUS (you are a fresh approver; do not edit code).\n"
            "- The goal ends only when its check passes and every agent approves. The check "
            "already passed: whether the goal is really met is yours to say.\n"
            "- Re-run the check yourself. Then inspect real evidence: run the thing as the person "
            "would use it and read what it does. Do not take the plan, the handoff or the last "
            "verify output for proof.\n"
            f"- What was done is in {where} and `.baton/PLAN.md`.\n"
            f"{shown}"
            f"{changed}"
            "- Do NOT edit, create or delete any file except `.baton/REVIEW.md` and "
            "`.baton/PLAN.md`: a round that changes the code is void and counts as a rejection.\n"
            "- Write `.baton/REVIEW.md`. Its first line must be `APPROVED` or `CHANGES`.\n"
            "- For CHANGES, say what is wrong and what evidence shows it."
        )
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


def add_task(workdir: Path, line: str) -> None:
    """Append an open task to PLAN.md, once. Raises OSError when it cannot be written."""
    path = workdir / ".baton" / PLAN_FILE
    text = read_plan(workdir)
    if line in open_tasks(text):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    sep = "" if not text or text.endswith("\n") else "\n"
    path.write_text(f"{text}{sep}{line}\n", encoding="utf-8")


def add_review_task(workdir: Path) -> None:
    """Append the follow-up task to PLAN.md, once. Raises OSError when it cannot be written."""
    add_task(workdir, REVIEW_TASK)


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


def archive_check(workdir: Path, now: float) -> Path | None:
    """Move `.baton/VERIFY.sh` to `.baton/archive/<timestamp>-VERIFY.sh`, when its goal ends or
    changes: the next goal writes a check of its own. The new place, or None when there was no
    check. Raises OSError when it cannot be moved."""
    source = workdir / ".baton" / CHECK_FILE
    if not source.is_file():
        return None
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(now))
    target_dir = workdir / ".baton" / "archive"
    target_dir.mkdir(parents=True, exist_ok=True)
    target, n = target_dir / f"{stamp}-{CHECK_FILE}", 1
    while target.exists():
        n += 1
        target = target_dir / f"{stamp}-{n}-{CHECK_FILE}"
    os.replace(source, target)
    return target

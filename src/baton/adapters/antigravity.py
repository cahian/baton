"""Antigravity adapter.

Antigravity (`agy`) can spend paid credits once its included usage runs out, so limits are read
before a round and while it runs, and the round is stopped before the included usage ends or as
soon as the credit balance moves. Unknown means stop.

Limits come from two slash commands that make no model call: `/usage` (rows per model group with
percent remaining) and `/credits` (the balance). Only Gemini rows count; the `Claude and GPT
models` rows belong to a pool baton never draws from.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from baton.adapters.base import (
    AUTH,
    COMPLETED,
    LIMIT,
    TIMEOUT,
    TRANSIENT,
    Availability,
    RoundContext,
    RoundResult,
    ToolWatch,
    check_subagent,
    find_model,
    tier_config,
)
from baton.adapters.guard import RETRY_S, Eval, Guard, LimitsError, run_guarded
from baton.config import AgentConfig
from baton.proc import clean_env, run_streaming

__all__ = [
    "AntigravityAdapter",
    "LimitsError",
    "check_usage",
    "classify",
    "parse_credits",
    "parse_usage",
    "read_text",
]

READ_TIMEOUT_S = 30
# A limits answer is a handful of rows; anything bigger than this is not one.
MAX_READ_BYTES = 256 * 1024
# How many error lines of a round are kept to classify its failure.
MAX_PROBLEMS = 20

_AUTH_RE = re.compile(r"authenticat|\bsign[ -]?in(?![a-z])|\blog[ -]?in(?![a-z])", re.IGNORECASE)
_LIMIT_RE = re.compile(r"RESOURCE_EXHAUSTED|quota|rate[ _-]?limit|\b429\b", re.IGNORECASE)
_PERCENT_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)%")
_NUMBER_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?")

GEMINI_MODELS = "Gemini Models"
GEMINI_WINDOWS = ("Five Hour", "Weekly")


# --- reading ---------------------------------------------------------------------------------


def read_text(bin_path: str, command: str, timeout_s: float = READ_TIMEOUT_S) -> str:
    """Run `agy -p <command> --output-format text` and return what it printed.

    Makes no model call. Stdin is closed, the process gets its own group and is killed afterwards,
    the wait is bounded by a deadline and so is the amount of output. Anything but a clean exit
    raises LimitsError.
    """
    try:
        child = subprocess.Popen(
            [bin_path, "-p", command, "--output-format", "text"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=clean_env(),
            start_new_session=True,
        )
    except OSError as exc:
        raise LimitsError(f"cannot start {bin_path}: {exc.strerror or exc}") from exc
    deadline = time.monotonic() + timeout_s
    chunks: list[bytes] = []
    overflow = threading.Event()

    def pump() -> None:
        size = 0
        assert child.stdout is not None
        try:
            while True:
                chunk = os.read(child.stdout.fileno(), 64 * 1024)
                if not chunk:
                    return
                size += len(chunk)
                if size > MAX_READ_BYTES:
                    overflow.set()
                    return
                chunks.append(chunk)
        except (OSError, ValueError):
            pass

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    try:
        reader.join(timeout=timeout_s)
        if overflow.is_set():
            raise LimitsError(f"{command} printed more than {MAX_READ_BYTES} bytes")
        if reader.is_alive():
            raise LimitsError(f"{command} did not finish within {timeout_s:g}s")
        try:
            code = child.wait(timeout=max(deadline - time.monotonic(), 0.1))
        except subprocess.TimeoutExpired:
            raise LimitsError(f"{command} did not finish within {timeout_s:g}s") from None
        if code != 0:
            raise LimitsError(f"{command} exited with {code}")
        return b"".join(chunks).decode("utf-8", errors="replace")
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        reader.join(timeout=1)
        if not reader.is_alive() and child.stdout is not None:
            try:
                child.stdout.close()
            except (OSError, ValueError):
                pass


# --- parsing ---------------------------------------------------------------------------------


def _percent_to_plain(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() else float(value)


def _parse_row(cols: list[str]) -> dict:
    if len(cols) != 4:
        raise LimitsError(f"usage row has {len(cols)} columns, expected 4: {cols[0]!r}")
    group, label, remaining_raw, reset_raw = (c.strip() for c in cols)
    match = _PERCENT_RE.fullmatch(remaining_raw)
    if match is None:
        raise LimitsError(f"{group!r}: percent remaining {remaining_raw!r} is not a percentage")
    remaining = Decimal(match.group(1))
    if remaining > 100:
        raise LimitsError(f"{group!r}: percent remaining {remaining_raw!r} is over 100%")
    try:
        reset = datetime.fromisoformat(reset_raw)
    except ValueError:
        raise LimitsError(f"{group!r}: reset time {reset_raw!r} is not ISO 8601") from None
    if reset.tzinfo is None:
        raise LimitsError(f"{group!r}: reset time {reset_raw!r} has no time zone")
    return {
        "group": group,
        "label": label,
        "used_pct": 100 - remaining,
        "resets_at": reset.timestamp(),
    }


def _is_gemini(group: str) -> bool:
    return group.strip().casefold().startswith("gemini")


def _missing_windows(rows: list[dict]) -> list[str]:
    """One message per window of `Gemini Models` that `rows` lacks. Each is a limit of its own, so
    a read with only one of them says nothing about the other."""
    labels = [r["label"].casefold() for r in rows if r["group"] == GEMINI_MODELS]
    return [
        f"usage has no readable {GEMINI_MODELS!r} {window} row"
        for window in GEMINI_WINDOWS
        if not any(window.casefold() in label for label in labels)
    ]


def _parse_usage_lenient(text: str) -> tuple[list[dict], list[str]]:
    """Every readable row, plus one message per problem found.

    A Gemini row that cannot be read is a problem; rows of other groups that cannot be read are
    skipped silently, as they take part in no decision. Each missing `Gemini Models` row is a
    problem.
    """
    rows: list[dict] = []
    problems: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        cols = line.split("\t")
        try:
            rows.append(_parse_row(cols))
        except LimitsError as exc:
            if _is_gemini(cols[0]):
                problems.append(str(exc))
    problems.extend(_missing_windows(rows))
    return rows, problems


def parse_usage(text: str) -> list[dict]:
    """Rows of `/usage` as `{"group", "label", "used_pct", "resets_at"}` (reset as epoch seconds).

    Used percent is 100 minus the percent remaining, kept exact as a Decimal. Strict: a Gemini row
    that cannot be read raises, and so does a text without both `Gemini Models` rows. (The guard
    uses the lenient reader instead, so a broken row never hides a readable saturated one.)
    """
    rows, problems = _parse_usage_lenient(text)
    if problems:
        raise LimitsError("; ".join(problems))
    return rows


def parse_credits(text: str) -> Decimal:
    """The balance printed by `/credits` (`Remaining credits<TAB>N`) as an exact Decimal."""
    found: list[str] = []
    for line in text.splitlines():
        cols = [c.strip() for c in line.split("\t")]
        if cols and cols[0].casefold() == "remaining credits":
            found.append(cols[1] if len(cols) > 1 else "")
    if not found:
        raise LimitsError("credit balance is missing")
    if len(found) > 1:
        raise LimitsError("credit balance appears more than once")
    if _NUMBER_RE.fullmatch(found[0]) is None:
        raise LimitsError(f"credit balance {found[0]!r} is not a number")
    return Decimal(found[0])


# --- evaluating ------------------------------------------------------------------------------


def _usage_summary(rows: list[dict] | None, balance: Decimal | None) -> dict:
    windows = [
        {
            "group": r["group"],
            "label": r["label"],
            "used_pct": _percent_to_plain(r["used_pct"]),
            "resets_at": r["resets_at"],
        }
        for r in rows or []
        if _is_gemini(r["group"])
    ]
    return {"windows": windows, "balance": None if balance is None else str(balance)}


def _saturated(rows: list[dict], threshold_pct: int) -> list[dict]:
    return [r for r in rows if _is_gemini(r["group"]) and r["used_pct"] >= threshold_pct]


def _reasons(saturated: list[dict]) -> list[str]:
    return [
        f"{r['group']} / {r['label']} at {_percent_to_plain(r['used_pct'])}%" for r in saturated
    ]


def _until(saturated: list[dict], now: float) -> float:
    """The latest reset among saturated rows; every parsed row has one."""
    until = max(float(r["resets_at"]) for r in saturated)
    return now + RETRY_S if until <= now else until  # a stale reset is no reset


def check_usage(rows: list[dict], threshold_pct: int, now: float) -> Availability:
    """Whether the Gemini rows allow a round. Rows of other groups are ignored.

    Unavailable until the latest reset among the saturated rows. Without both `Gemini Models`
    rows and nothing saturated the answer is unknown, so unavailable for the usual pause.
    """
    saturated = _saturated(rows, threshold_pct)
    usage = _usage_summary(rows, None)
    if saturated:  # a clear refusal wins, even without a `Gemini Models` row
        return Availability(
            False, until=_until(saturated, now), reason="; ".join(_reasons(saturated)), usage=usage
        )
    missing = _missing_windows(rows)
    if missing:
        return Availability(False, until=now + RETRY_S, reason="; ".join(missing), usage=usage)
    return Availability(True, usage=usage)


@dataclass(frozen=True)
class Reading:
    """One limits read: the text of each command, or the exception that replaced it."""

    usage: str | Exception
    credits: str | Exception


def _failed(command: str, answer: object) -> str:
    """Why a command's answer is no text: the exception that replaced it, or a wrong type."""
    if isinstance(answer, Exception):
        return f"{command} failed: {answer}"
    return f"{command} answered {type(answer).__name__}, not text"


def _evaluate(reading: Reading, threshold_pct: int, now: float) -> Eval:
    """Judge a read without letting one unreadable command hide the other.

    A parseable balance is always reported, so a drop is seen even if `/usage` is broken, and a
    clear refusal is honoured even if `/credits` is broken.
    """
    blind: list[str] = []
    rows: list[dict] | None = None
    balance: Decimal | None = None

    if not isinstance(reading.usage, str):
        blind.append(_failed("/usage", reading.usage))
    else:
        rows, problems = _parse_usage_lenient(reading.usage)
        blind.extend(f"/usage unreadable: {p}" for p in problems)
    if not isinstance(reading.credits, str):
        blind.append(_failed("/credits", reading.credits))
    else:
        try:
            balance = parse_credits(reading.credits)
        except LimitsError as exc:
            blind.append(f"/credits unreadable: {exc}")

    saturated = _saturated(rows, threshold_pct) if rows is not None else []
    usage = _usage_summary(rows, balance)
    if not saturated and not blind:
        return Eval(Availability(True, usage=usage), False, False, balance)
    until = _until(saturated, now) if saturated else now + RETRY_S
    if saturated:
        text = "; ".join(_reasons(saturated) + [f"unreadable: {b}" for b in blind])
    else:
        text = "; ".join(blind)
    return Eval(
        Availability(False, until=until, reason=text, usage=usage),
        refused=bool(saturated),
        blind=bool(blind),
        balance=balance,
    )


evaluate = _evaluate  # public name for callers that need the verdict


def classify(text: str, now: float) -> RoundResult | None:
    """A vendor refusal read from error text: `auth`, `limit`, or None when it is neither.

    A limit carries no `until`: the stream never says when it lifts, so the supervisor applies its
    own backoff. `now` is accepted so a reset hint in the text can be honoured here later.
    """
    if _AUTH_RE.search(text):
        return RoundResult(AUTH, reason=_summary(text))
    if _LIMIT_RE.search(text):
        return RoundResult(LIMIT, reason=_summary(text))
    return None


def _summary(text: str) -> str:
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    return (lines[-1] if lines else "")[:300]


# --- the adapter -----------------------------------------------------------------------------


def _parse_event(line: str) -> dict | None:
    try:
        event = json.loads(line)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


# A tool line shows at most this much of its value: a command can be a whole script.
BRIEF_CHARS = 200


def _brief(value: str) -> str:
    stripped = value.strip()
    shown = stripped.splitlines()[0][:BRIEF_CHARS]  # any break, a bare \r would overwrite text
    return shown if shown == stripped else f"{shown} …"


def _tool_line(step: dict) -> str | None:
    """One line for a tool step as it starts (ACTIVE; its DONE shows nothing), or None."""
    name = step.get("tool_name")
    if step.get("step_type") != "tool" or step.get("state") != "ACTIVE":
        return None
    if not isinstance(name, str) or not name:
        return None
    info = step.get("tool_info")
    params = info.get("parameters") if isinstance(info, dict) else None
    if not isinstance(params, dict):
        params = {}
    command = params.get("CommandLine")
    if name == "run_command" and isinstance(command, str) and command.strip():
        return f"$ {_brief(command)}"
    for value in params.values():
        if isinstance(value, str) and value.strip():
            return f"{name}: {_brief(value)}"
    return name


# The tools that start or manage a subagent, as `step_update` names them.
SUBAGENT_TOOLS = ("define_subagent", "invoke_subagent", "manage_subagents")


def _type_name(step: dict, depth: int = 2) -> str:
    """The `TypeName` a subagent call asks for, wherever the step carries it, or ""."""
    for key, value in step.items():
        if (
            isinstance(key, str)
            and key.lower() in ("typename", "type_name")
            and isinstance(value, str)
        ):
            return value
    if depth > 0:
        for value in step.values():
            if isinstance(value, dict):
                found = _type_name(value, depth - 1)
                if found:
                    return found
    return ""


def _payload(event: dict) -> dict:
    inner = event.get(event.get("event")) if isinstance(event.get("event"), str) else None
    return inner if isinstance(inner, dict) else {}


# What agy says when the only thing keeping it alive is background tasks.
_WAITING_RE = re.compile(r"waiting for (\d+) background task", re.IGNORECASE)
_TASK_LINE_RE = re.compile(r"^Task: *(\S+)", re.MULTILINE)
_STATUS_LINE_RE = re.compile(r"^Status: *(\S+)", re.MULTILINE)


class _ToolWatch(ToolWatch):
    """A tool step is open from its ACTIVE to any other state. A background task outlives its step:
    `manage_task` reports it RUNNING until it is DONE, ends in another status or is killed. And
    agy says when it only waits for tasks, which holds until a task goes or a tool starts."""

    def __init__(self) -> None:
        super().__init__()
        self.tasks: set[str] = set()
        self.waiting = False

    @property
    def busy(self) -> bool:
        return bool(self.open) or bool(self.tasks) or self.waiting

    def feed(self, line: str) -> None:
        event = _parse_event(line)
        if event is None:
            self._waiting_text(line)  # agy prints it as a plain line
            return
        if event.get("event") != "step_update":
            return
        step = _payload(event)
        delta = step.get("text_delta")
        if isinstance(delta, str):
            self._waiting_text(delta)
            return
        name = step.get("tool_name")
        index = step.get("step_index")
        if not isinstance(name, str) or not name or index is None:
            return
        key = (step.get("conversation_id"), index)
        state = step.get("state")
        if state == "ACTIVE":
            self.open.add(key)
            self.waiting = False
        elif isinstance(state, str):
            self.open.discard(key)  # DONE, or ERROR and the like: real logs have both
            if state == "DONE" and name == "manage_task":
                self._task(step)

    def _waiting_text(self, text: str) -> None:
        found = _WAITING_RE.search(text)
        if found and int(found.group(1)) > 0:
            self.waiting = True

    def _task(self, step: dict) -> None:
        info = step.get("tool_info")
        info = info if isinstance(info, dict) else {}
        params = info.get("parameters")
        params = params if isinstance(params, dict) else {}
        output = info.get("output")
        output = output if isinstance(output, str) else ""
        named = _TASK_LINE_RE.search(output)
        given = params.get("TaskId")
        task = named.group(1) if named else given if isinstance(given, str) else ""
        status = _STATUS_LINE_RE.search(output)
        if not task:
            return
        if params.get("Action") == "kill" or (status and status.group(1) != "RUNNING"):
            self.tasks.discard(task)
            self.waiting = False
        elif status:
            self.tasks.add(task)


class AntigravityAdapter:
    name = "antigravity"
    metered = False
    reserve = False
    # Seconds between the runner's checks; the limits are read every cfg.poll_s of poll_clock.
    tick_s = 1.0

    def __init__(
        self,
        cfg: AgentConfig,
        read: Callable[[str], str] | None = None,
        poll_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        self._text = read if read is not None else (lambda cmd: read_text(cfg.bin, cmd))
        self._poll_clock = poll_clock
        self._wall_clock = wall_clock

    def _read(self) -> Reading:
        """Both commands, each judged on its own: one failing does not hide the other."""

        def one(command: str) -> str | Exception:
            try:
                return self._text(command)
            except Exception as exc:
                return exc

        return Reading(usage=one("/usage"), credits=one("/credits"))

    def availability(self) -> Availability:
        if not self.cfg.enabled:
            return Availability(False, reason="antigravity is disabled in the goal file")
        now = self._wall_clock()
        try:
            return _evaluate(self._read(), self.cfg.threshold_pct, now).avail
        except Exception as exc:
            return Availability(False, until=now + RETRY_S, reason=f"cannot read limits: {exc}")

    def argv(self, prompt: str, session_id: str | None, tier: str = "strong") -> list[str]:
        cfg = tier_config(self.cfg, tier)
        # A NUL byte would make exec fail; a leading slash would run a slash command instead.
        prompt = prompt.replace("\x00", "")
        if prompt.lstrip().startswith("/"):
            prompt = "Task:\n" + prompt
        argv = [
            cfg.bin,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--model",
            cfg.model,
        ]
        if cfg.full_access:
            argv.append("--dangerously-skip-permissions")
        if session_id is not None:
            argv += ["--conversation", session_id]
        return argv

    def run_round(self, prompt: str, ctx: RoundContext) -> RoundResult:
        guard = Guard(self.cfg, self._read, _evaluate, self._poll_clock, self._wall_clock)
        problems: deque[str] = deque(maxlen=MAX_PROBLEMS)
        reported = False
        worked = False  # this attempt printed a step: whatever ends it, it did work
        final: dict | None = None
        subagents: set[tuple] = set()

        def on_line(line: str) -> RoundResult | None:
            nonlocal reported, worked, final
            event = _parse_event(line)
            if event is None:
                if line.strip():
                    problems.append(line.strip()[:500])
                return None
            kind = event.get("event")
            if kind == "init" and not reported:
                # agy puts the id at the top level of init; the payload is the older place.
                for cid in (event.get("conversation_id"), _payload(event).get("conversation_id")):
                    if isinstance(cid, str) and cid:
                        reported = True
                        if ctx.session_id is not None and cid != ctx.session_id:
                            # An id agy does not know is not an error to it: it starts a new
                            # conversation and runs, but a resumed prompt lacks the objective.
                            return RoundResult(
                                TRANSIENT,
                                reason=f"conversation {ctx.session_id} not found; agy began {cid}",
                                resume_failed=True,
                            )
                        ctx.on_session(cid)
                        break
            elif kind == "result":
                final = _payload(event)
            elif kind == "step_update":
                worked = True
                return self._subagent(event, ctx, subagents)
            return None

        argv = self.argv(prompt, ctx.session_id, ctx.tier)

        def start(run_ctx: RoundContext) -> RoundResult:
            # The prompt travels in argv; stdin stays closed (None), or agy waits on it.
            exit_ = run_streaming(
                argv, ctx.workdir, None, run_ctx, on_line, guard.tick, tick_s=self.tick_s
            )
            if exit_.stopped is not None:
                return exit_.stopped
            return self._outcome(exit_.timed_out, final, problems, ctx, worked)

        return run_guarded(guard, ctx, self.cfg.bin, start)

    def _subagent(self, event: dict, ctx: RoundContext, seen: set[tuple]) -> RoundResult | None:
        """A step that starts or manages a subagent. `TypeName` `self` runs on the round's own
        model and is allowed; a model the step shows must be one of the two."""
        step = _payload(event)
        tool = step.get("tool_name")
        if tool not in SUBAGENT_TOOLS and step.get("step_type") != "subagent":
            return None
        name = tool if isinstance(tool, str) and tool else "subagent"
        index = step.get("step_index")
        key = (step.get("conversation_id"), index, name)
        if index is not None and key in seen:
            return None  # the same step reported again as it progresses
        inherits = _type_name(step).lower() == "self"
        model = None if inherits else find_model(step)
        if index is not None and (inherits or model is not None):
            seen.add(key)  # only once its model is known: an earlier report may not show it
        return check_subagent(self.cfg, ctx, name, model, enforce=True)

    def _outcome(
        self,
        timed_out: bool,
        final: dict | None,
        problems: deque[str],
        ctx: RoundContext,
        worked: bool,
    ) -> RoundResult:
        if timed_out:
            return RoundResult(TIMEOUT, reason="round exceeded its time limit")
        if final is not None and final.get("status") == "SUCCESS":
            return RoundResult(COMPLETED)
        error = final.get("error") if final is not None else None
        lines = [error] if isinstance(error, str) and error.strip() else []
        text = "\n".join([*problems, *lines])
        if final is None:
            summary = "stream ended without a result event"
        else:
            summary = (lines[-1] if lines else f"status {final.get('status')!r}")[:300]
        refusal = classify(text, self._wall_clock())
        if refusal is not None:
            return refusal
        # A resumed round that never started a step failed to resume; one that worked and was then
        # cut off (agy's own "interrupted") keeps its conversation and what it did.
        if ctx.session_id is not None and not worked:
            return RoundResult(TRANSIENT, reason=f"resume failed: {summary}", resume_failed=True)
        return RoundResult(TRANSIENT, reason=summary)

    def tool_watch(self) -> ToolWatch:
        return _ToolWatch()

    def is_work(self, line: str) -> bool:
        event = _parse_event(line)
        return event is not None and event.get("event") == "step_update" and bool(self.render(line))

    def is_fragment(self, line: str) -> bool:
        """A text delta can end mid-word, so it is joined with the next one, not put on a line."""
        event = _parse_event(line)
        if event is None or event.get("event") != "step_update":
            return False
        delta = _payload(event).get("text_delta")
        return isinstance(delta, str) and bool(delta)

    def render(self, line: str) -> str | None:
        event = _parse_event(line)
        if event is None:
            text = line.strip()
            return text or None
        kind = event.get("event")
        body = _payload(event)
        if kind == "step_update":
            delta = body.get("text_delta")
            if isinstance(delta, str) and delta:
                return delta  # a fragment: never stripped, a space or a newline can be all of it
            return _tool_line(body)
        if kind == "result" and body.get("status") != "SUCCESS":
            error = body.get("error")
            return f"error: {error}" if isinstance(error, str) and error else None
        return None

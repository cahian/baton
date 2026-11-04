"""Codex adapter.

At 100% of its included usage the Codex CLI keeps working and silently debits account credits,
with no error. So limits are read before a round and while it runs, and the round is stopped
before the included usage ends or as soon as the credit balance moves. Unknown means stop.
"""

from __future__ import annotations

import json
import os
import queue
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from decimal import Decimal, InvalidOperation

from baton.adapters.base import (
    AUTH,
    COMPLETED,
    CREDIT_GUARD,
    LIMIT,
    TIMEOUT,
    TRANSIENT,
    Availability,
    RoundContext,
    RoundResult,
)
from baton.config import AgentConfig
from baton.proc import clean_env, run_streaming

# How long an agent rests after a failed or unreadable limits read.
RETRY_S = 300
READ_TIMEOUT_S = 15

_AUTH_RE = re.compile(
    r"not logged in|log ?in again|please log ?in|unauthori[sz]ed|\b401\b"
    r"|authentication (failed|required)|token (has )?expired|invalid (api key|token|credentials)",
    re.IGNORECASE,
)
_LIMIT_RE = re.compile(
    r"usage limit|rate.?limit|limit reached|quota|too many requests|\b429\b|credits",
    re.IGNORECASE,
)


class LimitsError(Exception):
    """Limits could not be read or understood."""


# --- reading ---------------------------------------------------------------------------------


def _reject_constant(name: str):
    raise ValueError(f"unexpected JSON constant {name}")


def _loads(raw: bytes | str) -> object:
    # Floats become Decimal so a balance sent as a JSON number never passes through binary float.
    return json.loads(raw, parse_float=Decimal, parse_constant=_reject_constant)


def _pump(stream, out: queue.Queue) -> None:
    try:
        for line in iter(stream.readline, b""):
            out.put(line)
    except (OSError, ValueError):
        pass
    finally:
        out.put(None)


def read_limits(bin_path: str, timeout_s: float = READ_TIMEOUT_S) -> dict:
    """Ask `codex app-server` for the account's rate limits. Returns the JSON-RPC `result`.

    Makes no model call. A fresh process per read; it is killed afterwards. Anything that is not a
    clean answer raises LimitsError.
    """
    try:
        child = subprocess.Popen(
            [bin_path, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=clean_env(),
            start_new_session=True,
        )
    except OSError as exc:
        raise LimitsError(f"cannot start {bin_path}: {exc.strerror or exc}") from exc
    lines: queue.Queue = queue.Queue()
    reader = threading.Thread(target=_pump, args=(child.stdout, lines), daemon=True)
    reader.start()
    try:
        requests = (
            {
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "baton", "title": "baton", "version": "1"}},
            },
            {"method": "initialized", "params": {}},
            {"id": 2, "method": "account/rateLimits/read"},
        )
        try:
            assert child.stdin is not None
            child.stdin.write("".join(json.dumps(r) + "\n" for r in requests).encode())
            child.stdin.flush()
        except (OSError, ValueError) as exc:
            raise LimitsError(f"cannot talk to app-server: {exc}") from exc
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                raw = lines.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                raise LimitsError(f"app-server did not answer within {timeout_s:g}s") from None
            if raw is None:
                raise LimitsError("app-server closed its output without answering")
            try:
                msg = _loads(raw)
            except ValueError:
                continue
            if not isinstance(msg, dict) or "id" not in msg:
                continue  # notifications and noise
            if msg["id"] == 1 and "error" in msg:
                raise LimitsError(f"app-server refused initialize: {msg['error']}")
            if msg["id"] == 2:
                if "error" in msg:
                    raise LimitsError(f"rateLimits/read failed: {msg['error']}")
                result = msg.get("result")
                if not isinstance(result, dict):
                    raise LimitsError("rateLimits/read returned no result object")
                return result
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        for stream in (child.stdin,):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):
                pass
        reader.join(timeout=1)
        if not reader.is_alive() and child.stdout is not None:
            try:
                child.stdout.close()
            except (OSError, ValueError):
                pass


# --- evaluating ------------------------------------------------------------------------------


def _is_number(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, Decimal):
        return value.is_finite()
    if isinstance(value, float):
        return value == value and value not in (float("inf"), float("-inf"))
    return False


def _plain(value: object) -> object:
    """JSON-friendly copy of a number for the usage dict."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def parse_balance(result: dict) -> Decimal:
    """The credit balance as an exact Decimal. Anything unparseable raises LimitsError."""
    try:
        raw = result["rateLimits"]["credits"]["balance"]
    except (KeyError, TypeError):
        raise LimitsError("credit balance is missing") from None
    if isinstance(raw, bool) or not isinstance(raw, str | int | Decimal):
        raise LimitsError(f"credit balance has unexpected type {type(raw).__name__}")
    try:
        balance = Decimal(raw)
    except InvalidOperation:
        raise LimitsError(f"credit balance {raw!r} is not a number") from None
    if not balance.is_finite():
        raise LimitsError(f"credit balance {raw!r} is not finite")
    return balance


def _evaluate(result: object, threshold_pct: int, now: float) -> tuple[Availability, bool]:
    """Availability plus whether the data was unreadable ("blind") rather than a clear no."""
    usage: dict = {"primary_pct": None, "secondary_pct": None, "balance": None, "plan": None}

    def blind(reason: str) -> tuple[Availability, bool]:
        return Availability(False, until=now + RETRY_S, reason=reason, usage=usage), True

    if not isinstance(result, dict):
        return blind("limits result is not an object")
    limits = result.get("rateLimits")
    if not isinstance(limits, dict):
        return blind("rateLimits is missing")
    plan = limits.get("planType")
    usage["plan"] = plan if isinstance(plan, str) else None

    primary = limits.get("primary")
    if not isinstance(primary, dict) or not _is_number(primary.get("usedPercent")):
        return blind("primary window is missing or unreadable")
    windows = {"primary": primary}
    secondary = limits.get("secondary")
    if secondary is not None:
        if not isinstance(secondary, dict) or not _is_number(secondary.get("usedPercent")):
            return blind("secondary window is unreadable")
        windows["secondary"] = secondary
    usage["primary_pct"] = _plain(primary["usedPercent"])
    if secondary is not None:
        usage["secondary_pct"] = _plain(secondary["usedPercent"])

    try:
        balance = parse_balance(result)
    except LimitsError as exc:
        return blind(str(exc))
    usage["balance"] = str(balance)

    if "rateLimitReachedType" not in limits:
        return blind("rateLimitReachedType is missing")
    allowed = result.get("ordinaryUsageAllowed")
    if not isinstance(allowed, bool):
        return blind("ordinaryUsageAllowed is missing or not a boolean")

    reasons: list[str] = []
    saturated: list[dict] = []
    if allowed is False:
        reasons.append("ordinary usage is not allowed")
    if limits["rateLimitReachedType"] is not None:
        reasons.append(f"rate limit reached ({limits['rateLimitReachedType']})")
    for name, window in windows.items():
        if window["usedPercent"] >= threshold_pct:
            reasons.append(f"{name} window at {_plain(window['usedPercent'])}%")
            saturated.append(window)
    if not reasons:
        return Availability(True, usage=usage), False

    def resets(group: list[dict]) -> list[float]:
        return [float(w["resetsAt"]) for w in group if _is_number(w.get("resetsAt"))]

    known = resets(saturated) or resets(list(windows.values()))
    until = max(known) if known else None
    if until is not None and until <= now:
        until = now + RETRY_S  # a stale reset is no reset
    return Availability(False, until=until, reason="; ".join(reasons), usage=usage), False


def check_limits(result: dict, threshold_pct: int, now: float) -> Availability:
    """Whether a round may start (or go on) given one limits read."""
    return _evaluate(result, threshold_pct, now)[0]


# --- the credit guard ------------------------------------------------------------------------


class _Guard:
    """Watches one round: usage threshold, credit balance, and being blind."""

    def __init__(
        self,
        cfg: AgentConfig,
        read: Callable[[], dict],
        poll_clock: Callable[[], float],
        wall_clock: Callable[[], float],
    ) -> None:
        self.cfg = cfg
        self.read = read
        self.poll_clock = poll_clock
        self.wall_clock = wall_clock
        self.baseline: Decimal | None = None
        self.failures = 0
        self.last_poll = 0.0

    def start(self) -> RoundResult | None:
        """Read the baseline immediately before spawning. A result means: do not spawn."""
        try:
            result = self.read()
        except Exception as exc:
            return RoundResult(
                LIMIT, until=self.wall_clock() + RETRY_S, reason=f"cannot read limits: {exc}"
            )
        avail, _ = _evaluate(result, self.cfg.threshold_pct, self.wall_clock())
        if not avail.ok:
            return RoundResult(LIMIT, until=avail.until, reason=avail.reason)
        self.baseline = parse_balance(result)
        self.last_poll = self.poll_clock()
        return None

    def tick(self) -> RoundResult | None:
        now = self.poll_clock()
        if now - self.last_poll < self.cfg.poll_s:
            return None
        self.last_poll = now
        return self.check()

    def check(self) -> RoundResult | None:
        assert self.baseline is not None
        try:
            result = self.read()
        except Exception as exc:
            return self._blind(f"limits read failed: {exc}")
        avail, blind = _evaluate(result, self.cfg.threshold_pct, self.wall_clock())
        if blind:
            return self._blind(avail.reason)
        self.failures = 0
        balance = parse_balance(result)
        if balance < self.baseline:
            return RoundResult(
                CREDIT_GUARD,
                until=None if avail.ok else avail.until,
                reason=f"credit balance dropped from {self.baseline} to {balance}",
            )
        if not avail.ok:
            return RoundResult(LIMIT, until=avail.until, reason=avail.reason)
        return None

    def _blind(self, why: str) -> RoundResult | None:
        self.failures += 1
        if self.failures >= 2:
            return RoundResult(
                CREDIT_GUARD, reason=f"limits unreadable {self.failures} times in a row: {why}"
            )
        return None


# --- the adapter -----------------------------------------------------------------------------


def _parse_event(line: str) -> dict | None:
    try:
        event = json.loads(line)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


def _event_message(event: dict) -> str:
    err = event.get("error")
    if isinstance(err, dict) and isinstance(err.get("message"), str):
        return err["message"]
    msg = event.get("message")
    return msg if isinstance(msg, str) else ""


class CodexAdapter:
    name = "codex"
    # Seconds between the runner's checks; the limits are read every cfg.poll_s of poll_clock.
    tick_s = 1.0

    def __init__(
        self,
        cfg: AgentConfig,
        read: Callable[[], dict] | None = None,
        poll_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        self._read = read if read is not None else (lambda: read_limits(cfg.bin))
        self._poll_clock = poll_clock
        self._wall_clock = wall_clock

    def availability(self) -> Availability:
        now = self._wall_clock()
        try:
            return check_limits(self._read(), self.cfg.threshold_pct, now)
        except Exception as exc:
            return Availability(False, until=now + RETRY_S, reason=f"cannot read limits: {exc}")

    def argv(self, session_id: str | None, workdir: str) -> list[str]:
        cfg = self.cfg
        common = [
            "--json",
            "--skip-git-repo-check",
            "-m",
            cfg.model,
            "-c",
            f"model_reasoning_effort={cfg.effort}",
        ]
        if session_id is None:
            access = (
                ["--dangerously-bypass-approvals-and-sandbox"]
                if cfg.full_access
                else ["--sandbox", "workspace-write"]
            )
            return [cfg.bin, "exec", *common, *access, "-C", workdir, "-"]
        # `exec resume` takes neither --sandbox nor -C: the sandbox goes through -c and the
        # working directory is the process's own.
        access = (
            ["--dangerously-bypass-approvals-and-sandbox"]
            if cfg.full_access
            else ["-c", 'sandbox_mode="workspace-write"']
        )
        return [cfg.bin, "exec", "resume", *common, *access, session_id, "-"]

    def run_round(self, prompt: str, ctx: RoundContext) -> RoundResult:
        guard = _Guard(self.cfg, self._read, self._poll_clock, self._wall_clock)
        refused = guard.start()
        if refused is not None:
            return refused

        problems: list[str] = []
        reported = False

        def on_line(line: str) -> RoundResult | None:
            nonlocal reported
            event = _parse_event(line)
            if event is None:
                if line.strip():
                    problems.append(line.strip()[:500])
                return None
            kind = event.get("type")
            if kind == "thread.started" and not reported:
                tid = event.get("thread_id")
                if isinstance(tid, str) and tid:
                    reported = True
                    ctx.on_session(tid)
            elif kind in ("error", "turn.failed"):
                text = _event_message(event)
                if text:
                    problems.append(text[:500])
            return None

        argv = self.argv(ctx.session_id, str(ctx.workdir))
        try:
            exit_ = run_streaming(
                argv, ctx.workdir, prompt, ctx, on_line, guard.tick, tick_s=self.tick_s
            )
        except OSError as exc:
            return RoundResult(TRANSIENT, reason=f"cannot start {self.cfg.bin}: {exc}")

        result = exit_.stopped
        if result is None:
            result = self._classify(exit_.returncode, exit_.timed_out, problems, ctx)
        # The process is gone; one last read catches a debit made since the previous poll.
        if result.outcome != CREDIT_GUARD:
            late = guard.check()
            if late is not None and late.outcome == CREDIT_GUARD:
                return late
        return result

    def _classify(
        self, returncode: int | None, timed_out: bool, problems: list[str], ctx: RoundContext
    ) -> RoundResult:
        if timed_out:
            return RoundResult(TIMEOUT, reason="round exceeded its time limit")
        if returncode == 0:
            return RoundResult(COMPLETED)
        text = "\n".join(problems[-20:])
        summary = (problems[-1] if problems else f"exit code {returncode}")[:300]
        if _AUTH_RE.search(text):
            return RoundResult(AUTH, reason=summary)
        if _LIMIT_RE.search(text):
            return RoundResult(LIMIT, reason=summary)
        if ctx.session_id is not None:
            return RoundResult(TRANSIENT, reason=f"resume failed: {summary}", resume_failed=True)
        return RoundResult(TRANSIENT, reason=summary)

    def render(self, line: str) -> str | None:
        event = _parse_event(line)
        if event is None:
            text = line.strip()
            return text or None
        kind = event.get("type")
        item = event.get("item")
        if isinstance(item, dict):
            if kind == "item.completed" and item.get("type") == "agent_message":
                text = item.get("text")
                return text if isinstance(text, str) and text.strip() else None
            if kind == "item.started" and item.get("type") == "command_execution":
                command = item.get("command")
                return f"$ {command}" if isinstance(command, str) and command else None
            return None
        if kind in ("error", "turn.failed"):
            text = _event_message(event)
            return f"error: {text}" if text else None
        return None

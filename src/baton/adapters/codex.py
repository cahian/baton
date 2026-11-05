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
from collections import deque
from collections.abc import Callable
from decimal import Decimal, InvalidOperation

from baton.adapters.base import (
    AUTH,
    COMPLETED,
    LIMIT,
    TIMEOUT,
    TRANSIENT,
    Availability,
    RoundContext,
    RoundResult,
)
from baton.adapters.guard import RETRY_S, Eval, Guard, LimitsError, run_guarded
from baton.config import AgentConfig
from baton.proc import clean_env, run_streaming

READ_TIMEOUT_S = 15
# Longest chunk taken from the server in one piece; a server cannot grow a line without bound.
MAX_LINE_BYTES = 64 * 1024
# How many error lines of a round are kept to classify its failure.
MAX_PROBLEMS = 20

_AUTH_RE = re.compile(
    r"not logged in|log ?in again|please log ?in|unauthori[sz]ed|\b401\b"
    r"|authentication (failed|required)|token (has )?expired|invalid (api key|token|credentials)",
    re.IGNORECASE,
)
_LIMIT_RE = re.compile(
    r"usage limit|rate.?limit|limit reached|quota|too many requests|\b429\b|credits",
    re.IGNORECASE,
)


# --- reading ---------------------------------------------------------------------------------


def _reject_constant(name: str):
    raise ValueError(f"unexpected JSON constant {name}")


def _loads(raw: bytes | str) -> object:
    # Floats become Decimal so a balance sent as a JSON number never passes through binary float.
    return json.loads(raw, parse_float=Decimal, parse_constant=_reject_constant)


def _pump(stream, out: queue.Queue) -> None:
    try:
        for line in iter(lambda: stream.readline(MAX_LINE_BYTES), b""):
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
    deadline = time.monotonic() + timeout_s
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
        while True:
            # Checked on every pass, not only when the queue is empty: a server that floods
            # output must not keep the deadline from firing.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LimitsError(f"app-server did not answer within {timeout_s:g}s")
            try:
                raw = lines.get(timeout=remaining)
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
        try:
            if child.stdin is not None:
                child.stdin.close()
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


def _is_percent(value: object) -> bool:
    """A usage percentage that fits a finite float: 1e5000 or 10**400 is blind, not a number."""
    if not _is_number(value):
        return False
    try:
        as_float = float(value)  # type: ignore[arg-type]
    except (OverflowError, ValueError):
        return False
    return as_float == as_float and as_float not in (float("inf"), float("-inf"))


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


def _evaluate(result: object, threshold_pct: int, now: float) -> Eval:
    """Judge a read without letting one unreadable field hide the rest.

    A parseable balance is always reported, so a drop is seen even if other fields are broken,
    and a clear refusal is honoured even when something else is unreadable.
    """
    usage: dict = {"primary_pct": None, "secondary_pct": None, "balance": None, "plan": None}
    blind: list[str] = []
    reasons: list[str] = []
    windows: dict[str, dict] = {}
    balance: Decimal | None = None
    limits: dict | None = None
    flag_only = False  # ordinaryUsageAllowed=false with no limit named: the cause is unknown

    if not isinstance(result, dict):
        blind.append("limits result is not an object")
    else:
        raw_limits = result.get("rateLimits")
        if not isinstance(raw_limits, dict):
            blind.append("rateLimits is missing")
        else:
            limits = raw_limits
        try:
            balance = parse_balance(result)
            usage["balance"] = str(balance)
        except LimitsError as exc:
            blind.append(str(exc))

    if limits is not None and isinstance(result, dict):
        plan = limits.get("planType")
        usage["plan"] = plan if isinstance(plan, str) else None
        for name, required in (("primary", True), ("secondary", False)):
            window = limits.get(name)
            if window is None and not required:
                continue
            if isinstance(window, dict) and _is_percent(window.get("usedPercent")):
                windows[name] = window
                usage[f"{name}_pct"] = _plain(window["usedPercent"])
            else:
                blind.append(f"{name} window is missing or unreadable")
        if "rateLimitReachedType" not in limits:
            blind.append("rateLimitReachedType is missing")
        elif limits["rateLimitReachedType"] is not None:
            reasons.append(f"rate limit reached ({limits['rateLimitReachedType']})")
        allowed = result.get("ordinaryUsageAllowed")
        if not isinstance(allowed, bool):
            blind.append("ordinaryUsageAllowed is missing or not a boolean")
        elif allowed is False:
            reasons.append("ordinary usage is not allowed")
            flag_only = limits.get("rateLimitReachedType") is None

    saturated = []
    for name, window in windows.items():
        if window["usedPercent"] >= threshold_pct:
            reasons.append(f"{name} window at {_plain(window['usedPercent'])}%")
            saturated.append(window)

    if not reasons and not blind:
        return Eval(Availability(True, usage=usage), False, False, balance)

    def resets(group: list[dict]) -> list[float]:
        # A reset that does not fit a finite float (10**400, 1e999) is no reset.
        epochs = []
        for w in group:
            if _is_number(w.get("resetsAt")):
                try:
                    epoch = float(w["resetsAt"])
                except OverflowError:
                    continue
                if epoch == epoch and epoch not in (float("inf"), float("-inf")):
                    epochs.append(epoch)
        return epochs

    if reasons and flag_only and not saturated:
        # No window explains the refusal, and the latest window reset can be days away.
        until = now + RETRY_S
    elif reasons:
        known = resets(saturated) or resets(list(windows.values()))
        until = max(known) if known else None
        if until is not None and until <= now:
            until = now + RETRY_S  # a stale reset is no reset
        elif until is None and blind:
            until = now + RETRY_S  # refused on partial data: retry after the usual pause
    else:
        until = now + RETRY_S
    text = "; ".join(reasons + [f"unreadable: {b}" for b in blind] if reasons else blind)
    return Eval(
        Availability(False, until=until, reason=text, usage=usage),
        refused=bool(reasons),
        blind=bool(blind),
        balance=balance,
    )


evaluate = _evaluate  # public name for callers that need the verdict


def check_limits(result: dict, threshold_pct: int, now: float) -> Availability:
    """Whether a round may start (or go on) given one limits read."""
    return _evaluate(result, threshold_pct, now).avail


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
    metered = False
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
        guard = Guard(self.cfg, self._read, _evaluate, self._poll_clock, self._wall_clock)
        problems: deque[str] = deque(maxlen=MAX_PROBLEMS)
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

        def start(run_ctx: RoundContext) -> RoundResult:
            exit_ = run_streaming(
                argv, ctx.workdir, prompt, run_ctx, on_line, guard.tick, tick_s=self.tick_s
            )
            if exit_.stopped is not None:
                return exit_.stopped
            return self._classify(exit_.returncode, exit_.timed_out, problems, ctx)

        return run_guarded(guard, ctx, self.cfg.bin, start)

    def _classify(
        self, returncode: int | None, timed_out: bool, problems: deque[str], ctx: RoundContext
    ) -> RoundResult:
        if timed_out:
            return RoundResult(TIMEOUT, reason="round exceeded its time limit")
        if returncode == 0:
            return RoundResult(COMPLETED)
        text = "\n".join(problems)
        summary = (problems[-1] if problems else f"exit code {returncode}")[:300]
        if _AUTH_RE.search(text):
            return RoundResult(AUTH, reason=summary)
        if _LIMIT_RE.search(text):
            return RoundResult(LIMIT, reason=summary)
        if ctx.session_id is not None:
            return RoundResult(TRANSIENT, reason=f"resume failed: {summary}", resume_failed=True)
        return RoundResult(TRANSIENT, reason=summary)

    def is_work(self, line: str) -> bool:
        event = _parse_event(line)
        return (
            event is not None
            and event.get("type") not in ("error", "turn.failed")
            and bool(self.render(line))
        )

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

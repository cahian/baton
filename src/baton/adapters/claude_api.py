"""Claude API adapter: the Claude Code CLI on a Console API key, under a hard spend ceiling.

Nothing here can read the credit balance, so the ceiling is baton's own (see `baton.ledger`): a
round reserves its budget before it starts, and settles with `total_cost_usd` when the stream's
`result` event arrives. Overspending is the failure this module exists to prevent, so wherever
something is unknown the answer is the expensive one:

- no `result` event means the whole reservation stays counted;
- an `init` event that does not name the API key as the credential stops the round (a subscription
  login would not spend the credit at all, and anything unexpected is not trusted);
- an unreadable ledger, key file or cost report means the agent is unavailable;
- the key is read when the process is spawned and travels only in that process's environment. It
  is in no argv, no log, no event, no state and no exception text (`run_streaming` also replaces it
  in everything the child prints).
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

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
from baton.adapters.guard import RETRY_S
from baton.config import AgentConfig
from baton.ledger import MIN_GRANT_USD, Ledger, LedgerError, ceiling_for
from baton.proc import run_streaming
from baton.store import state_base

__all__ = [
    "ClaudeApiAdapter",
    "CostReportError",
    "KeyFileError",
    "evaluate_line",
    "fetch_cost_report",
    "ledger_for",
    "make_cost_report",
    "read_key",
]

KEY_VARIABLE = "ANTHROPIC_API_KEY"
LEDGER_NAME = "claude-spend.json"
# Without a usable key file the agent is asked again after this long.
NO_KEY_RETRY_S = 3600
MAX_KEY_BYTES = 4096
# How many error lines of a round are kept to classify its failure.
MAX_PROBLEMS = 20

API_BASE = "https://api.anthropic.com"
REPORT_PAGES = 12  # a period is at most 31 daily buckets, which is one page of 31
REPORT_MAX_BYTES = 8 * 1024 * 1024
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}

_CREDIT_RE = re.compile(
    r"credit[ _-]?balance|billing|spend(?:ing)?[ _-]?limit|usage[ _-]?limits?", re.IGNORECASE
)
_RATE_RE = re.compile(r"\b429\b|\b529\b|overloaded|rate[ _-]?limit", re.IGNORECASE)
_AUTH_RE = re.compile(
    r"authenticat|invalid[ _-]?(?:x-)?api[ _-]?key|\b401\b|\bsign[ -]?in(?![a-z])"
    r"|\blog[ -]?in(?![a-z])|/login",
    re.IGNORECASE,
)
# The `error` field the CLI puts on the synthetic assistant message of an API failure.
_ERROR_KINDS = {
    "billing_error": "credit balance billing error",
    "rate_limit": "rate limit",
    "authentication_failed": "authentication failed",
}
# Results that end a round by its own limit: the work is kept, as with a timeout.
_BOUNDED_SUBTYPES = {"error_max_budget_usd", "error_max_turns"}


class KeyFileError(Exception):
    """A key file is missing, unsafe or malformed. The message never includes its content."""


class CostReportError(Exception):
    """The cost report could not be read. The message never includes a key."""


# --- the key ---------------------------------------------------------------------------------


def read_key(path: Path) -> str:
    """The key in `path`: a regular file, mode 600, holding one printable token.

    Raises `KeyFileError`, whose message names the problem and never the content.
    """
    path = Path(path).expanduser()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise KeyFileError(f"key file {path} not found") from None
    except OSError as exc:
        raise KeyFileError(
            f"key file {path} cannot be opened (a symlink is refused): {type(exc).__name__}"
        ) from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise KeyFileError(f"key file {path} is not a regular file")
        mode = stat.S_IMODE(info.st_mode)
        if mode != 0o600:
            raise KeyFileError(f"key file {path} must be mode 600 (it is {mode:03o})")
        raw = os.read(fd, MAX_KEY_BYTES + 1)
    except OSError as exc:
        raise KeyFileError(f"key file {path} cannot be read: {type(exc).__name__}") from None
    finally:
        os.close(fd)
    if len(raw) > MAX_KEY_BYTES:
        raise KeyFileError(f"key file {path} is too large to be a key")
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise KeyFileError(f"key file {path} is not plain text") from None
    key = text.strip()
    if not key:
        raise KeyFileError(f"key file {path} is empty")
    if not key.isprintable() or any(c.isspace() for c in key):
        raise KeyFileError(f"key file {path} must hold a single token and nothing else")
    return key


# --- the stream ------------------------------------------------------------------------------


def _reject_constant(name: str) -> None:
    raise ValueError(name)


def _parse(line: str) -> dict | None:
    """A stream line as a dict, with every number that has a point kept as an exact Decimal."""
    try:
        event = json.loads(line, parse_float=Decimal, parse_constant=_reject_constant)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


def evaluate_line(line: str) -> RoundResult | None:
    """A reason to stop the round, from one stream line; None for everything else.

    The `init` event says which credential the CLI is using. Anything but the API key (a login,
    a helper, a missing field) stops the round: a credential other than the one baton accounts
    for is not covered by the ledger.
    """
    event = _parse(line)
    if event is None or event.get("type") != "system" or event.get("subtype") != "init":
        return None
    source = event.get("apiKeySource")
    if source == KEY_VARIABLE:
        return None
    shown = source if isinstance(source, str) and len(source) <= 40 else type(source).__name__
    return RoundResult(
        CREDIT_GUARD,
        reason=f"apiKeySource is {shown!r}, not {KEY_VARIABLE!r}: the round is not on the key",
    )


def _cost(value: object) -> Decimal | None:
    """`total_cost_usd` as a usable amount, or None."""
    if isinstance(value, bool) or not isinstance(value, int | Decimal):
        return None
    number = Decimal(value)
    return number if number.is_finite() and number >= 0 else None


def _summary(text: str) -> str:
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    return (lines[-1] if lines else "")[:300]


class _Stream:
    """What one round's stream said."""

    def __init__(self) -> None:
        self.verified = False  # an init event named the key
        self.active = False  # something other than system events was said
        self.session_reported = False
        self.has_result = False
        self.is_error = False
        self.subtype = ""
        self.cost: Decimal | None = None
        self.result_text = ""
        self.kinds: set[str] = set()
        self.problems: deque[str] = deque(maxlen=MAX_PROBLEMS)

    def error_text(self) -> str:
        lines = [*self.problems, *(_ERROR_KINDS[k] for k in sorted(self.kinds))]
        if self.result_text:
            lines.append(self.result_text)
        return "\n".join(lines)


# --- spend -----------------------------------------------------------------------------------


def ledger_for(cfg: AgentConfig, clock: Callable[[], float] = time.time) -> Ledger:
    """The ledger every goal shares, with this config's ceiling and reset day."""
    return Ledger(
        state_base() / LEDGER_NAME,
        ceiling_for(cfg.monthly_budget_usd, cfg.reserve_pct),
        cfg.budget_reset_day,
        clock,
    )


def fetch_cost_report(
    key_file: Path,
    since: float,
    *,
    base_url: str = API_BASE,
    timeout: float = 15,
) -> Decimal:
    """Everything the organization has spent since `since`, in dollars, from the Admin API.

    The API reports cents; the sum is exact. Anything it cannot understand raises
    `CostReportError`, and no message or exception carries the key.
    """
    parts = urllib.parse.urlsplit(base_url)
    if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in _LOOPBACK):
        raise CostReportError("the Admin API must be reached over https")
    try:
        key = read_key(key_file)
    except KeyFileError as exc:
        raise CostReportError(f"admin key: {exc}") from None
    starting_at = datetime.fromtimestamp(since, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    cents = Decimal(0)
    page: str | None = None
    for _ in range(REPORT_PAGES):
        query = {"starting_at": starting_at, "bucket_width": "1d", "limit": "31"}
        if page is not None:
            query["page"] = page
        url = f"{base_url.rstrip('/')}/v1/organizations/cost_report?{urllib.parse.urlencode(query)}"
        body = _get(url, key, timeout)
        page_cents, page = _read_page(body)
        cents += page_cents
        if page is None:
            return cents / 100
    raise CostReportError(f"the cost report has more than {REPORT_PAGES} pages")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # the key must never follow a redirect
        return None


def _get(url: str, key: str, timeout: float) -> object:
    request = urllib.request.Request(
        url, headers={"x-api-key": key, "anthropic-version": "2023-06-01"}
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(REPORT_MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise CostReportError(f"the Admin API answered HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise CostReportError(f"cannot reach the Admin API: {type(reason).__name__}") from None
    if len(raw) > REPORT_MAX_BYTES:
        raise CostReportError("the cost report is too large")
    try:
        return json.loads(raw, parse_constant=_reject_constant)
    except ValueError:
        raise CostReportError("the cost report is not valid JSON") from None


def _read_page(body: object) -> tuple[Decimal, str | None]:
    """(cents in this page, the next page's token or None)."""
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise CostReportError("the cost report has no data list")
    cents = Decimal(0)
    for bucket in body["data"]:
        results = bucket.get("results") if isinstance(bucket, dict) else None
        if not isinstance(results, list):
            raise CostReportError("a cost bucket has no results list")
        for item in results:
            if not isinstance(item, dict) or item.get("currency") != "USD":
                raise CostReportError("a cost item is not in USD")
            amount = item.get("amount")
            try:
                value = Decimal(amount) if isinstance(amount, str) else None
            except InvalidOperation:
                value = None
            if value is None or not value.is_finite() or value < 0:
                raise CostReportError("a cost item has an unreadable amount")
            cents += value
    more = body.get("has_more")
    if not isinstance(more, bool):
        raise CostReportError("the cost report does not say whether there is more")
    if not more:
        return cents, None
    token = body.get("next_page")
    if not isinstance(token, str) or not token:
        raise CostReportError("the cost report has more pages but no next_page")
    return cents, token


def make_cost_report(cfg: AgentConfig, ledger: Ledger) -> Callable[[], Decimal] | None:
    """The reader for `admin_key_file`, or None when it is not set."""
    if not cfg.admin_key_file:
        return None
    path = Path(cfg.admin_key_file).expanduser()

    def report() -> Decimal:
        return fetch_cost_report(path, ledger.period_start())

    return report


# --- the adapter -----------------------------------------------------------------------------


def _fmt(value: Decimal) -> str:
    return format(value.normalize() if value == value.to_integral_value() else value, "f")


class ClaudeApiAdapter:
    name = "claude_api"
    # Seconds between the runner's checks. There is nothing to poll: the round's own cap is
    # `--max-budget-usd`, and the ledger already counts this round.
    tick_s = 1.0

    def __init__(
        self,
        cfg: AgentConfig,
        ledger: Ledger,
        cost_report: Callable[[], Decimal] | None = None,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        self.ledger = ledger
        self._cost_report = cost_report
        self._wall_clock = wall_clock

    @property
    def ceiling(self) -> Decimal:
        return self.ledger.ceiling

    # availability

    def _floor(self) -> Decimal | None:
        """The cost report's figure, or None without an admin key. Raises when it fails."""
        if self._cost_report is None:
            return None
        value = self._cost_report()
        if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
            raise CostReportError("the cost report gave an unusable figure")
        return value

    def availability(self) -> Availability:
        if not self.cfg.enabled:
            return Availability(False, reason="claude_api is disabled in the goal file")
        now = self._wall_clock()
        try:
            read_key(Path(self.cfg.key_file))
        except KeyFileError as exc:
            return Availability(False, until=now + NO_KEY_RETRY_S, reason=str(exc))
        if self.cfg.round_budget_usd < MIN_GRANT_USD:
            return Availability(
                False,
                until=now + NO_KEY_RETRY_S,
                reason=f"round_budget_usd is below the minimum round of {MIN_GRANT_USD} USD",
            )
        try:
            if self.ledger.exhausted():
                return Availability(
                    False,
                    until=self.ledger.next_period(),
                    reason="the Console reported no credit left: waiting for the next period",
                )
            floor = self._floor()
            spent = self.ledger.spent(floor)
        except LedgerError as exc:
            return Availability(False, until=now + RETRY_S, reason=f"ledger unreadable: {exc}")
        except Exception as exc:
            return Availability(False, until=now + RETRY_S, reason=f"cost report failed: {exc}")
        ceiling = self.ceiling
        left = ceiling - spent
        usage = {"spent_usd": _fmt(spent), "ceiling_usd": _fmt(ceiling), "left_usd": _fmt(left)}
        if left < MIN_GRANT_USD:
            return Availability(
                False,
                until=self.ledger.next_period(),
                reason=f"spent {_fmt(spent)} of the {_fmt(ceiling)} USD ceiling",
                usage=usage,
            )
        return Availability(True, usage=usage)

    # the round

    def argv(self, granted: Decimal, session_id: str | None) -> list[str]:
        argv = [
            self.cfg.bin,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--bare",
            "--model",
            self.cfg.model,
            "--effort",
            self.cfg.effort,
            "--dangerously-skip-permissions",
            "--max-budget-usd",
            format(granted, "f"),
        ]
        if session_id is not None:
            argv += ["--resume", session_id]
        return argv

    def run_round(self, prompt: str, ctx: RoundContext) -> RoundResult:
        now = self._wall_clock()
        if not self.cfg.enabled:
            return RoundResult(LIMIT, until=now + NO_KEY_RETRY_S, reason="claude_api is disabled")
        try:
            key = read_key(Path(self.cfg.key_file))
        except KeyFileError as exc:
            return RoundResult(AUTH, reason=str(exc))
        try:
            floor = self._floor()
            reservation = self.ledger.reserve(self.cfg.round_budget_usd, floor=floor)
        except LedgerError as exc:
            return RoundResult(LIMIT, until=now + RETRY_S, reason=f"ledger unreadable: {exc}")
        except Exception as exc:
            return RoundResult(LIMIT, until=now + RETRY_S, reason=f"cost report failed: {exc}")
        if reservation is None:
            return RoundResult(
                LIMIT,
                until=self.ledger.next_period(),
                reason="the Claude API spend ceiling leaves less than 1 USD for a round",
            )
        reservation_id, granted = reservation
        stream = _Stream()
        spawned = False

        def on_spawn(pid: int) -> None:
            nonlocal spawned
            spawned = True
            ctx.on_spawn(pid)

        def on_line(line: str) -> RoundResult | None:
            return self._on_line(line, stream, ctx)

        run_ctx = replace(ctx, on_spawn=on_spawn)
        argv = self.argv(granted, ctx.session_id)
        # Whatever happens from here on (an exception, a signal) leaves the reservation in the
        # ledger: it is only replaced when the stream really reported a cost, or released when
        # nothing was ever started.
        try:
            exit_ = run_streaming(
                argv,
                ctx.workdir,
                prompt,
                run_ctx,
                on_line,
                lambda: None,
                tick_s=self.tick_s,
                extra_env={KEY_VARIABLE: key, "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1"},
            )
        except OSError as exc:
            if spawned:
                return RoundResult(
                    TRANSIENT, reason=f"runner failed after start: {type(exc).__name__}"
                )
            self._release(reservation_id)
            return RoundResult(
                TRANSIENT,
                reason=f"cannot start {self.cfg.bin}: {exc.strerror or type(exc).__name__}",
            )
        settled = self._settle(reservation_id, stream)
        if exit_.stopped is not None:
            return exit_.stopped
        return self._outcome(exit_.timed_out, exit_.returncode, stream, ctx, settled)

    def _on_line(self, line: str, stream: _Stream, ctx: RoundContext) -> RoundResult | None:
        stop = evaluate_line(line)
        if stop is not None:
            return stop
        event = _parse(line)
        if event is None:
            if line.strip():
                stream.problems.append(line.strip()[:500])
            return None
        kind = event.get("type")
        if kind in ("assistant", "user", "stream_event", "result"):
            stream.active = True
        if kind == "system" and event.get("subtype") == "init":
            stream.verified = True
            session = event.get("session_id")
            if not stream.session_reported and isinstance(session, str) and session:
                stream.session_reported = True
                ctx.on_session(session)
        elif kind in ("assistant", "user", "stream_event") and not stream.verified:
            return RoundResult(
                CREDIT_GUARD,
                reason=f"a {kind} event came before an init event confirmed the key source",
            )
        elif kind == "assistant":
            error = event.get("error")
            if isinstance(error, str) and error in _ERROR_KINDS:
                stream.kinds.add(error)
        elif kind == "result":
            stream.has_result = True
            stream.is_error = event.get("is_error") is True
            subtype = event.get("subtype")
            stream.subtype = subtype if isinstance(subtype, str) else ""
            cost = _cost(event.get("total_cost_usd"))
            if cost is not None:
                stream.cost = cost if stream.cost is None else max(stream.cost, cost)
            texts = []
            if isinstance(event.get("result"), str):
                texts.append(event["result"])
            errors = event.get("errors")
            if isinstance(errors, list):
                texts.extend(e for e in errors if isinstance(e, str))
            stream.result_text = "\n".join(texts)[-2000:]
        return None

    def _release(self, reservation_id: str) -> None:
        """Give back a reservation for a process that never existed."""
        try:
            self.ledger.settle(reservation_id, Decimal(0))
        except Exception:
            pass  # the reservation stays: an overcount

    def _settle(self, reservation_id: str, stream: _Stream) -> bool:
        """Replace the reservation with the reported cost. Without one, it stays as it is."""
        if stream.cost is None:
            return False
        try:
            self.ledger.settle(reservation_id, stream.cost)
        except Exception:
            return False  # the reservation stays: an overcount
        return True

    def _refusal(self, text: str, stream: _Stream) -> RoundResult | None:
        """`limit` or `auth` from the error text of a failed round, or None."""
        if _CREDIT_RE.search(text):
            try:
                self.ledger.mark_exhausted()
            except Exception:
                pass
            return RoundResult(
                LIMIT, until=self.ledger.next_period(), reason=_summary(text) or "no credit left"
            )
        if _AUTH_RE.search(text):
            return RoundResult(AUTH, reason=_summary(text))
        if _RATE_RE.search(text):
            return RoundResult(LIMIT, reason=_summary(text))
        return None

    def _outcome(
        self,
        timed_out: bool,
        returncode: int | None,
        stream: _Stream,
        ctx: RoundContext,
        settled: bool,
    ) -> RoundResult:
        if timed_out:
            return RoundResult(TIMEOUT, reason="round exceeded its time limit")
        if not stream.verified:
            if not stream.active:
                # Nothing but noise before the process ended: it never got as far as the API.
                summary = f"exited before the init event (exit {returncode})"
                text = _summary(stream.error_text())
                if text:
                    summary = f"{summary}: {text}"
                refusal = self._refusal(stream.error_text(), stream)
                if refusal is not None:
                    return refusal
                if ctx.session_id is not None:
                    return RoundResult(
                        TRANSIENT, reason=f"resume failed: {summary}", resume_failed=True
                    )
                return RoundResult(TRANSIENT, reason=summary)
            if stream.has_result and stream.is_error and ctx.session_id is not None:
                return RoundResult(
                    TRANSIENT,
                    reason=f"resume failed: {_summary(stream.error_text())}",
                    resume_failed=True,
                )
            return RoundResult(
                CREDIT_GUARD, reason="no init event confirmed that the round ran on the API key"
            )
        if stream.has_result and not stream.is_error:
            return RoundResult(COMPLETED)
        if stream.has_result and stream.subtype in _BOUNDED_SUBTYPES:
            return RoundResult(COMPLETED, reason=_summary(stream.result_text) or stream.subtype)
        text = stream.error_text()
        refusal = self._refusal(text, stream)
        if refusal is not None:
            return refusal
        if not stream.has_result:
            summary = f"stream ended without a result event (exit {returncode})"
        else:
            summary = _summary(text) or f"result {stream.subtype or 'error'}"
        if ctx.session_id is not None:
            return RoundResult(TRANSIENT, reason=f"resume failed: {summary}", resume_failed=True)
        return RoundResult(TRANSIENT, reason=summary)

    # rendering

    def render(self, line: str) -> str | None:
        event = _parse(line)
        if event is None:
            text = line.strip()
            return text or None
        kind = event.get("type")
        if kind == "assistant":
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            parts = []
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    if block["text"].strip():
                        parts.append(block["text"].strip())
                elif block.get("type") == "tool_use" and isinstance(block.get("name"), str):
                    parts.append(f"[{block['name']}]")
            return "\n".join(parts) or None
        if kind == "result" and event.get("is_error") is True:
            texts = [event["result"]] if isinstance(event.get("result"), str) else []
            texts += [e for e in event.get("errors") or [] if isinstance(e, str)]
            return f"error: {' '.join(texts)}" if texts else None
        return None

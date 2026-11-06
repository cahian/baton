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
    ToolWatch,
    check_subagent,
    claude_allowed_models,
    subagent_guard_command,
    tier_config,
)
from baton.adapters.guard import RETRY_S
from baton.config import AgentConfig
from baton.ledger import MIN_GRANT_USD, Ledger, LedgerError, ceiling_for, default_path
from baton.proc import Exit, run_streaming
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
# Without a usable key file the agent is asked again after this long.
NO_KEY_RETRY_S = 3600
MAX_KEY_BYTES = 4096
# How many error lines of a round are kept to classify its failure.
MAX_PROBLEMS = 20
# A line this many BYTES long is a piece of an event (the runner cuts lines at 64 KB of bytes, so
# a line of multi-byte characters is far fewer characters than that): agent output, never an error
# message. A line that follows one is the rest of it.
LARGE_LINE = 32 * 1024
# How long after its time limit a round's reservation is still taken to belong to a live round.
EXPIRY_MARGIN_S = 3600
# The longest line that may be read as an error message when it is not JSON.
SHORT_ERROR_LINE = 300
# CLAUDE_CODE_SUBPROCESS_ENV_SCRUB forces Claude Code's permission mode to default, so
# --dangerously-skip-permissions is ignored and, without this list, every write is denied.
# `--tools` limits what exists (without --bare the CLI would offer far more), `--allowedTools` is
# what lets these write. Agent is there on purpose: subagents are allowed, on baton's two models
# only: CLAUDE_CODE_SUBAGENT_MODEL_FORCE makes every one run on the working model, and the
# PreToolUse hook (subagent_guard.py) is a second check; the stream only logs them.
# What the round's environment must not carry in: every way for the CLI to find a credential or a
# provider other than the key baton accounts for (a subscription token, Bedrock, Vertex, a
# gateway, a file descriptor to read a token from). Dropped before the key is added.
CREDENTIAL_ENV = (
    "ANTHROPIC_*",
    "CLAUDE_CODE_USE_*",
    "CLAUDE_CODE_OAUTH_*",
    "CLAUDE_CODE_*_FILE_DESCRIPTOR",
    "AWS_BEARER_TOKEN_BEDROCK",
)
TOOLS = "Bash,Edit,Read,Write,Agent"
ALLOWED_TOOLS = TOOLS
SUBAGENT_TOOL_NAMES = ("Agent", "Task")

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
    # Without --bare the CLI could use a subscription login: that is refused at once, before any
    # model call, and counts as an authentication problem.
    return RoundResult(
        AUTH,
        reason=f"apiKeySource is {shown!r}, not {KEY_VARIABLE!r}: the round is not on the key",
    )


def claude_config_dir() -> Path:
    """`<state base>/.claude-config`, mode 700, made when it is missing. Raises OSError."""
    path = state_base() / ".claude-config"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def guard_settings() -> str:
    """The `--settings` document: a PreToolUse hook on `Agent` and `Task` that runs the guard, and
    blocks the call if the hook itself fails."""
    hook = {
        "matcher": "Agent|Task",
        "hooks": [{"type": "command", "command": subagent_guard_command(), "onFailure": "block"}],
    }
    return json.dumps({"hooks": {"PreToolUse": [hook]}}, separators=(",", ":"))


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
        self.events = 0  # how many lines were JSON events
        self.rejected_init = False  # the init event named a credential other than the key
        self.large = False  # some line was (a piece of) a big event: the agent did real work
        self.continuing = False  # the previous line was a piece of a longer one
        self.session_reported = False
        self.has_result = False
        self.is_error = False
        self.subtype = ""
        self.cost: Decimal | None = None
        self.result_text = ""
        self.kinds: set[str] = set()
        self.subagents: set[str] = set()  # tool uses whose model was seen
        self.problems: deque[str] = deque(maxlen=MAX_PROBLEMS)

    def structured_text(self) -> str:
        """What the API said in the fields made for it: the error kind of an assistant message
        and the text of a failed result."""
        lines = [_ERROR_KINDS[k] for k in sorted(self.kinds)]
        if self.result_text:
            lines.append(self.result_text)
        return "\n".join(lines)

    def plain_text(self) -> str:
        """The short plain lines the process printed itself."""
        return "\n".join(self.problems)

    def error_text(self) -> str:
        return "\n".join(t for t in (self.plain_text(), self.structured_text()) if t)


# --- spend -----------------------------------------------------------------------------------


def ledger_for(cfg: AgentConfig, clock: Callable[[], float] = time.time) -> Ledger:
    """The one ledger every goal shares. Its ceiling and reset day come from the global budget
    settings, and the file refuses a goal that disagrees with them."""
    return Ledger(
        default_path(),
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


class _ToolWatch(ToolWatch):
    """A tool is open from the `tool_use` block of an assistant event to the `tool_result` block
    of a user event, by id."""

    def feed(self, line: str) -> None:
        event = _parse(line)
        if event is None or event.get("type") not in ("assistant", "user"):
            return
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else ():
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                self.open.add(block["id"])
            elif block.get("type") == "tool_result" and isinstance(block.get("tool_use_id"), str):
                self.open.discard(block["tool_use_id"])


class ClaudeApiAdapter:
    name = "claude_api"
    metered = True
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

    def argv(self, granted: Decimal, session_id: str | None, tier: str = "strong") -> list[str]:
        cfg = tier_config(self.cfg, tier)
        argv = [
            cfg.bin,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            # Not --bare: it would stop --tools and --settings from working. What it did, these do:
            # no user, project or local settings, no MCP servers but our own (none), and the key
            # source is checked on the init event (a login is refused).
            "--setting-sources=",
            "--strict-mcp-config",
            "--model",
            cfg.model,
            "--effort",
            cfg.effort,
            "--dangerously-skip-permissions",
            # `=` form: these flags are variadic and would eat the arguments after them
            f"--tools={TOOLS}",
            f"--allowedTools={ALLOWED_TOOLS}",
            f"--settings={guard_settings()}",
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
            config_dir = claude_config_dir()
        except OSError as exc:
            return RoundResult(
                TRANSIENT, reason=f"cannot make baton's Claude config directory: {exc.strerror}"
            )
        try:
            floor = self._floor()
            reservation = self.ledger.reserve(
                self.cfg.round_budget_usd,
                floor=floor,
                live_for_s=ctx.round_max_s + EXPIRY_MARGIN_S,
            )
        except LedgerError as exc:
            return RoundResult(LIMIT, until=now + RETRY_S, reason=f"ledger unreadable: {exc}")
        except Exception as exc:
            return RoundResult(LIMIT, until=now + RETRY_S, reason=f"cost report failed: {exc}")
        if reservation is None:
            return RoundResult(
                LIMIT,
                until=self.ledger.next_period(),
                reason=f"the spend ceiling leaves less than {MIN_GRANT_USD} USD for a round",
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
        argv = self.argv(granted, ctx.session_id, ctx.tier)
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
                scrub=CREDENTIAL_ENV,
                extra_env={KEY_VARIABLE: key, "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1"},
                # Not secrets, so the stream may show them. The hook reads the allowed names, and
                # every subagent runs on the working model, whatever it names. The CLI's own
                # memory is off: baton's handoff and ai-memory are what carry what was learned.
                plain_env={
                    "BATON_ALLOWED_MODELS": ",".join(claude_allowed_models(self.cfg)),
                    "CLAUDE_CODE_SUBAGENT_MODEL": self.cfg.build_model,
                    "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1",
                    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
                    # A directory of baton's own: the user's ~/.claude.json has an `env` map that
                    # could replace the key or the base URL. Every round uses this one, so a
                    # session started in one round can be resumed in the next.
                    "CLAUDE_CONFIG_DIR": str(config_dir),
                },
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
        if stream.cost is not None:
            self._settle(reservation_id, stream.cost)
        elif self._never_reached_the_api(exit_, stream):
            self._release(reservation_id)
        if exit_.stopped is not None:
            if exit_.stopped.outcome == COMPLETED and (stream.is_error or stream.kinds):
                # The stop (a plan agent is back, a stop request) landed after the API had
                # already refused the round: what it said is the story, and the credit stays
                # marked as gone.
                refusal = self._refusal(stream)
                if refusal is not None:
                    return refusal
            return exit_.stopped
        return self._outcome(exit_.timed_out, exit_.returncode, stream, ctx)

    def _on_line(self, line: str, stream: _Stream, ctx: RoundContext) -> RoundResult | None:
        size = len(line.encode("utf-8", errors="replace"))
        continuing, stream.continuing = stream.continuing, size >= LARGE_LINE
        if size >= LARGE_LINE:
            stream.large = True
        event = _parse(line)
        if event is not None:
            stream.events += 1
        stop = evaluate_line(line)
        if stop is not None:
            stream.rejected_init = True
            return stop
        if event is None:
            # Only a short line of plain text can be an error message (the process's own stderr).
            # A piece of a big event is agent output: a "billing" inside a file it wrote says
            # nothing about the account.
            text = line.strip()
            if text and not continuing and size <= SHORT_ERROR_LINE and text[0] not in '{["':
                stream.problems.append(text)
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
            return self._subagents(event, stream, ctx)
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

    def _subagents(self, event: dict, stream: _Stream, ctx: RoundContext) -> RoundResult | None:
        """Every subagent the stream shows, reported as an event. It never ends a round: the hook
        denies a third model before the subagent exists."""
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else ():
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool = block.get("name")
            if tool not in SUBAGENT_TOOL_NAMES:
                continue
            ident = block.get("id") if isinstance(block.get("id"), str) else None
            if ident is not None and ident in stream.subagents:
                continue
            tool_input = block.get("input")
            model = tool_input.get("model") if isinstance(tool_input, dict) else None
            model = model if isinstance(model, str) and model.strip() else None
            if ident is not None and model is not None:
                stream.subagents.add(ident)
            refused = check_subagent(self.cfg, ctx, tool, model)
            if refused is not None:
                return refused
        return None

    def _release(self, reservation_id: str) -> None:
        """Give back a reservation for a round that cannot have spent anything."""
        try:
            self.ledger.settle(reservation_id, Decimal(0))
        except Exception:
            pass  # the reservation stays: an overcount

    def _settle(self, reservation_id: str, cost: Decimal) -> None:
        """Replace the reservation with the reported cost."""
        try:
            self.ledger.settle(reservation_id, cost)
        except Exception:
            pass  # the reservation stays: an overcount

    @staticmethod
    def _never_reached_the_api(exit_: Exit, stream: _Stream) -> bool:
        """True only when it is certain that no model call was made, so nothing was spent.

        That is: the process left no JSON event at all (a binary that cannot even start prints
        noise), or its first and only event was the init that named the wrong credential, which
        comes before any call. Any other event, even a bare `api_retry` or `rate_limit_event`,
        can mean a request was made; and a timeout, a stop, a cost or a big line mean the round
        did something. Then the reservation stays.
        """
        if exit_.timed_out or stream.large or stream.cost is not None:
            return False
        if stream.rejected_init:
            return stream.events == 1
        return exit_.stopped is None and stream.events == 0

    def _refusal(self, stream: _Stream) -> RoundResult | None:
        """`limit` or `auth` from the error text of a failed round, or None.

        The credit is marked gone for every goal and for the whole period only on what the API
        said in its own structured fields. The process's plain stderr can say it too, and ends
        this round the same way, but a stray line is not allowed to block everyone for a month."""
        structured, plain = stream.structured_text(), stream.plain_text()
        if _CREDIT_RE.search(structured):
            try:
                self.ledger.mark_exhausted()
            except Exception:
                pass
            return RoundResult(
                LIMIT,
                until=self.ledger.next_period(),
                reason=_summary(structured) or "no credit left",
            )
        if _CREDIT_RE.search(plain):
            return RoundResult(
                LIMIT, until=self.ledger.next_period(), reason=_summary(plain) or "no credit left"
            )
        text = stream.error_text()
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
                refusal = self._refusal(stream)
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
            # Cut off by its own cap: the scheduler still sees a completed round, but the work was
            # not finished, so it must not advance a phase or finish a goal.
            return RoundResult(
                COMPLETED, reason=_summary(stream.result_text) or stream.subtype, bounded=True
            )
        text = stream.error_text()
        refusal = self._refusal(stream)
        if refusal is not None:
            return refusal
        if not stream.has_result:
            summary = f"stream ended without a result event (exit {returncode})"
        else:
            summary = _summary(text) or f"result {stream.subtype or 'error'}"
        # The session was loaded and the key confirmed, so this is not a failed resume.
        return RoundResult(TRANSIENT, reason=summary)

    # rendering

    def tool_watch(self) -> ToolWatch:
        return _ToolWatch()

    def is_work(self, line: str) -> bool:
        event = _parse(line)
        return (
            event is not None
            and event.get("type") == "assistant"
            and not event.get("error")  # the CLI's own message about a failed API call
            and bool(self.render(line))
        )

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

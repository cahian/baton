"""The credit guard shared by every adapter that can silently spend paid credits.

An adapter supplies `read()` (one limits read, whatever shape) and `evaluate(read, threshold, now)`
(which judges it). The guard enforces the order that keeps a debit from hiding behind a broken
field: a parseable balance is compared first, then clear refusals are honoured, and only then is
being blind counted, two in a row stopping the round. Unknown means stop.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from baton.adapters.base import CREDIT_GUARD, LIMIT, Availability, RoundResult
from baton.config import AgentConfig

# How long an agent rests after a failed or unreadable limits read.
RETRY_S = 300


class LimitsError(Exception):
    """Limits could not be read or understood."""


@dataclass(frozen=True)
class Eval:
    """One limits read, judged. Everything readable is used even when something else is not."""

    avail: Availability
    refused: bool  # a clear "no": flag false, reached type set, or a window at the threshold
    blind: bool  # some field the decision needs was missing or unreadable
    balance: Decimal | None  # the credit balance whenever it could be parsed


Evaluate = Callable[[Any, int, float], Eval]


class Guard:
    """Watches one round: usage threshold, credit balance, and being blind."""

    def __init__(
        self,
        cfg: AgentConfig,
        read: Callable[[], Any],
        evaluate: Evaluate,
        poll_clock: Callable[[], float],
        wall_clock: Callable[[], float],
    ) -> None:
        self.cfg = cfg
        self.read = read
        self.evaluate = evaluate
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
        ev = self.evaluate(result, self.cfg.threshold_pct, self.wall_clock())
        if not ev.avail.ok or ev.balance is None:
            return RoundResult(LIMIT, until=ev.avail.until, reason=ev.avail.reason)
        self.baseline = ev.balance
        self.last_poll = self.poll_clock()
        return None

    def tick(self) -> RoundResult | None:
        now = self.poll_clock()
        if now - self.last_poll < self.cfg.poll_s:
            return None
        self.last_poll = now
        verdict, unreadable, _ = self._observe()
        if verdict is not None:
            return verdict
        if unreadable is None:
            self.failures = 0
            return None
        self.failures += 1
        if self.failures >= 2:
            return RoundResult(
                CREDIT_GUARD,
                reason=f"limits unreadable {self.failures} times in a row: {unreadable}",
            )
        return None

    def _observe(self) -> tuple[RoundResult | None, str | None, bool]:
        """One read: (a verdict that stops the round, why it was not fully readable, whether
        the balance parsed).

        Order matters: any parseable balance is compared first, then clear refusals are honoured,
        and only then is being blind counted by the caller.
        """
        assert self.baseline is not None
        try:
            result = self.read()
        except Exception as exc:
            return None, f"limits read failed: {exc}", False
        ev = self.evaluate(result, self.cfg.threshold_pct, self.wall_clock())
        unreadable = ev.avail.reason if ev.blind else None
        if ev.balance is not None and ev.balance < self.baseline:
            return (
                RoundResult(
                    CREDIT_GUARD,
                    until=None if ev.avail.ok else ev.avail.until,
                    reason=f"credit balance dropped from {self.baseline} to {ev.balance}",
                ),
                unreadable,
                True,
            )
        seen = ev.balance is not None
        if ev.refused:
            result = RoundResult(LIMIT, until=ev.avail.until, reason=ev.avail.reason)
            return result, unreadable, seen
        return None, unreadable, seen

    def close(self) -> RoundResult | None:
        """The read after the process is gone: it must see the balance, or the round is suspect.

        A debit made since the last poll is only visible here, and the next round's baseline
        would absorb it. One failed read is retried once; a second failure is credit_guard.
        """
        why = ""
        for _ in range(2):
            verdict, unreadable, balance_seen = self._observe()
            if verdict is not None and verdict.outcome == CREDIT_GUARD:
                return verdict
            # What close() guards is the balance: once it parsed and did not drop, another
            # field staying blind says nothing about a debit.
            if unreadable is None or balance_seen:
                return None
            why = unreadable
        return RoundResult(
            CREDIT_GUARD, reason=f"cannot confirm the balance after the round: {why}"
        )

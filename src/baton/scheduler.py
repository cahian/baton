"""Sticky cyclic rotation with cooldowns and exponential backoff."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from baton.adapters.base import (
    AUTH,
    COMPLETED,
    CREDIT_GUARD,
    LIMIT,
    TIMEOUT,
    Adapter,
    Availability,
    RoundResult,
)
from baton.store import StateError, Store

# (first delay, ceiling) in seconds
LIMIT_BACKOFF = (15 * 60, 60 * 60)
TRANSIENT_BACKOFF = (60, 30 * 60)
CREDIT_GUARD_S = 30 * 60
AUTH_S = 30 * 60
# An agent that could not be checked is asked again after this long.
UNAVAILABLE_RETRY_S = 5 * 60
MIN_WAIT_S = 30
# A reset further out than this is a bad reading (milliseconds for seconds, say), not a reset,
# and counts as unknown.
MAX_COOLDOWN_S = 8 * 24 * 3600


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


class Scheduler:
    """Decides which agent runs next.

    The current agent keeps working until it is unavailable; only then does the other one take
    over, and an agent that comes back never preempts the one working. With everyone exhausted
    `pick` returns None and `next_wake` says when to look again.

    Cooldowns live in memory first. They are written to the store when `save` is called and
    after `record`, and a store that cannot be read or written never resets them.
    """

    def __init__(
        self,
        order: Sequence[str],
        adapters: Mapping[str, Adapter],
        store: Store,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.order = [name for name in dict.fromkeys(order) if name in adapters]
        if not self.order:
            raise ValueError("no adapter for any agent in order")
        self._adapters = adapters
        self._store = store
        self._clock = clock
        self._agents: dict[str, dict] = {name: self._blank() for name in self.order}
        self.current: str = self.order[0]
        saved = store.load_state()  # StateError is the caller's to survive
        if saved.get("current") in self._agents:
            self.current = saved["current"]
        stored = saved.get("agents")
        if isinstance(stored, dict):
            for name, raw in stored.items():
                if name in self._agents and isinstance(raw, dict):
                    self._restore(self._agents[name], raw)

    @staticmethod
    def _blank() -> dict:
        return {"until": 0.0, "limit_n": 0, "transient_n": 0, "reason": "", "usage": {}}

    @staticmethod
    def _restore(agent: dict, raw: dict) -> None:
        until = _num(raw.get("until"))
        agent["until"] = until if until is not None and until > 0 else 0.0
        for key in ("limit_n", "transient_n"):
            n = raw.get(key)
            agent[key] = n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else 0
        if isinstance(raw.get("reason"), str):
            agent["reason"] = raw["reason"]
        if isinstance(raw.get("usage"), dict):
            agent["usage"] = raw["usage"]

    def _known(self, until: Any, now: float) -> float | None:
        """`until` as a usable reset time, or None when it is missing, past or nonsense."""
        value = _num(until)
        if value is None or value <= now or value > now + MAX_COOLDOWN_S:
            return None
        return value

    # choosing

    def _candidates(self) -> list[str]:
        """The current agent first, then the others in `order`, wrapping around from it."""
        at = self.order.index(self.current)
        return self.order[at:] + self.order[:at]

    def pick(self) -> Adapter | None:
        """The adapter to run now, or None when every agent is cooling down or unavailable."""
        for name in self._candidates():
            agent = self._agents[name]
            now = self._clock()
            if agent["until"] > now:
                continue
            adapter = self._adapters[name]
            try:
                avail = adapter.availability()
            except Exception as exc:  # an unreadable agent is an unavailable one
                avail = Availability(False, reason=f"availability check failed: {exc}")
            agent["usage"] = dict(avail.usage)
            if avail.ok:
                agent["until"] = 0.0
                agent["reason"] = ""
                self.current = name
                return adapter
            now = self._clock()
            known = self._known(avail.until, now)
            agent["until"] = known if known is not None else now + UNAVAILABLE_RETRY_S
            agent["reason"] = avail.reason
        return None

    def next_wake(self) -> float:
        """Epoch of the earliest possible retry, never sooner than 30 s from now."""
        now = self._clock()
        untils = [a["until"] for a in self._agents.values() if a["until"] > now]
        return max(min(untils), now + MIN_WAIT_S) if untils else now + MIN_WAIT_S

    # outcomes

    def record(self, agent: str, result: RoundResult) -> None:
        """Apply the outcome table to `agent`, then write it down if the store allows."""
        if agent not in self._agents:
            return
        state = self._agents[agent]
        now = self._clock()
        outcome = result.outcome
        if result.resume_failed:
            return  # the round never really ran; it is retried fresh and is not the agent's fault
        if outcome in (COMPLETED, TIMEOUT):
            state.update(until=0.0, limit_n=0, transient_n=0, reason="")
        elif outcome == LIMIT:
            known = self._known(result.until, now)
            if known is None:
                known = now + self._backoff(state, "limit_n", LIMIT_BACKOFF)
            state["until"] = known
        elif outcome == CREDIT_GUARD:
            known = self._known(result.until, now)
            state["until"] = known if known is not None else now + CREDIT_GUARD_S
        elif outcome == AUTH:
            state["until"] = now + AUTH_S
        else:  # transient, and anything unrecognised
            state["until"] = now + self._backoff(state, "transient_n", TRANSIENT_BACKOFF)
        if outcome not in (COMPLETED, TIMEOUT):
            state["reason"] = result.reason
        try:
            self.save()
        except (StateError, OSError):
            pass  # memory is authoritative; the next save() writes all of it

    @staticmethod
    def _backoff(state: dict, counter: str, bounds: tuple[int, int]) -> float:
        first, ceiling = bounds
        delay = min(first * 2 ** state[counter], ceiling)
        state[counter] += 1
        return float(delay)

    # persistence

    def snapshot(self) -> dict:
        """Per-agent state for state.json and the panel."""
        now = self._clock()
        agents = {}
        for name, raw in self._agents.items():
            agents[name] = {
                "status": "cooldown" if raw["until"] > now else "ready",
                "until": raw["until"],
                "limit_n": raw["limit_n"],
                "transient_n": raw["transient_n"],
                "reason": raw["reason"],
                "usage": raw["usage"],
            }
        return {"current": self.current, "agents": agents}

    def save(self) -> None:
        """Write the rotation into state.json next to whatever else it holds.

        Raises StateError when the file cannot be read, and then leaves it alone.
        """
        state = self._store.load_state()
        state.update(self.snapshot())
        self._store.save_state(state)

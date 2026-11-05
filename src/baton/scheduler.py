"""Sticky cyclic rotation with cooldowns and exponential backoff."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from baton.adapters.base import (
    AUTH,
    COMPLETED,
    CREDIT_GUARD,
    LIMIT,
    PREEMPTED,
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
# A plan agent whose round, right after it ended a metered one, fails in less than this long
# without having worked is blocked from preempting again: it read ok but cannot hold a round.
PREEMPT_PROOF_S = 300
# A reset further out than this is a bad reading (milliseconds for seconds, say), not a reset,
# and counts as unknown.
MAX_COOLDOWN_S = 8 * 24 * 3600


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def read_availability(adapter: Adapter) -> Availability:
    """The adapter's availability; an agent that cannot be read is an unavailable one."""
    try:
        return adapter.availability()
    except Exception as exc:
        return Availability(False, reason=f"availability check failed: {exc}")


class Scheduler:
    """Decides which agent runs next.

    Plan agents (included usage that comes back at a reset) come first. The current plan agent
    keeps working until it is unavailable; only then does the next one in `order` take over,
    wrapping around, and a plan agent that comes back never preempts another that is working. A
    metered agent (paid by use, nothing that would ever stop it) is a fallback wherever it stands
    in `order`: it is picked only while no plan agent is available. While one runs, the runner
    reads the plan agents with `plan_ready_to_check` and `record_read`, and ends the metered round
    when one is back; `record` gives that round (`preempted`) no cooldown. A plan agent that
    reads ok but whose next round fails within `PREEMPT_PROOF_S` (and not as completed or
    timeout) is blocked: `plan_ready_to_check` leaves it out, so it stops costing a metered round
    per attempt, until a round of it completes, times out or lasts that long. `pick` still tries
    it at a round boundary. With everyone exhausted `pick` returns None and `next_wake` says when
    to look again.

    Cooldowns live in memory first. They are written to the store when `save` is called and
    after `record`, and a store that cannot be read or written never resets them. The runner's
    watcher thread calls `plan_ready_to_check` and `record_read` while the main thread holds the
    round, so every public method takes one lock.
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
        self._lock = threading.RLock()
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
        return {
            "until": 0.0,
            "limit_n": 0,
            "transient_n": 0,
            "reason": "",
            "usage": {},
            "blocked": False,
            "after_preempt": False,
        }

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
        agent["blocked"] = raw.get("blocked") is True
        agent["after_preempt"] = raw.get("after_preempt") is True

    def _known(self, until: Any, now: float) -> float | None:
        """`until` as a usable reset time, or None when it is missing, past or nonsense."""
        value = _num(until)
        if value is None or value <= now or value > now + MAX_COOLDOWN_S:
            return None
        return value

    # choosing

    def _plan_names(self) -> list[str]:
        return [n for n in self.order if not self._adapters[n].metered]

    def _candidates(self) -> list[str]:
        """The plan agents, from the current one when it is a plan agent and else from the start
        of `order`, wrapping around; then the metered agents, in `order`."""
        plan = self._plan_names()
        if self.current in plan:
            at = plan.index(self.current)
            plan = plan[at:] + plan[:at]
        return plan + [n for n in self.order if self._adapters[n].metered]

    def _apply(self, agent: dict, avail: Availability) -> None:
        """Write a read of `agent` into its state: ready, or resting until its reset (or, when
        that is unknown, until it is worth asking again)."""
        agent["usage"] = dict(avail.usage)
        if avail.ok:
            agent["until"] = 0.0
            agent["reason"] = ""
            return
        now = self._clock()
        known = self._known(avail.until, now)
        agent["until"] = known if known is not None else now + UNAVAILABLE_RETRY_S
        agent["reason"] = avail.reason

    def pick(self) -> Adapter | None:
        """The adapter to run now, or None when every agent is cooling down or unavailable."""
        with self._lock:
            for name in self._candidates():
                agent = self._agents[name]
                if agent["until"] > self._clock():
                    continue
                adapter = self._adapters[name]
                avail = read_availability(adapter)
                self._apply(agent, avail)
                if avail.ok:
                    self.current = name
                    return adapter
            return None

    def plan_ready_to_check(self) -> list[str]:
        """The plan agents whose cooldown has passed and that are not blocked, in `order`: the
        ones worth reading while a metered agent runs. Moves nothing."""
        with self._lock:
            now = self._clock()
            return [
                n
                for n in self._plan_names()
                if self._agents[n]["until"] <= now and not self._agents[n]["blocked"]
            ]

    def record_read(self, name: str, avail: Availability) -> None:
        """Write down a read made outside `pick`, with the same rules. Never moves `current`."""
        with self._lock:
            if name in self._agents:
                self._apply(self._agents[name], avail)

    def next_wake(self) -> float:
        """Epoch of the earliest possible retry, never sooner than 30 s from now."""
        with self._lock:
            now = self._clock()
            untils = [a["until"] for a in self._agents.values() if a["until"] > now]
            return max(min(untils), now + MIN_WAIT_S) if untils else now + MIN_WAIT_S

    # outcomes

    def record(self, agent: str, result: RoundResult, seconds: float | None = None) -> None:
        """Apply the outcome table to `agent`, then write it down if the store allows.

        `seconds` is how long the round ran; without it no block on preempting is set."""
        with self._lock:
            self._record(agent, result, seconds)

    def _record(self, agent: str, result: RoundResult, seconds: float | None) -> None:
        if agent not in self._agents:
            return
        state = self._agents[agent]
        now = self._clock()
        outcome = result.outcome
        if result.resume_failed:
            return  # the round never really ran; it is retried fresh and is not the agent's fault
        if outcome == PREEMPTED:
            # ended by baton because a plan agent is back: nobody's failure, no cooldown. The
            # agent that was back has a round to prove itself with, the next one it runs.
            if result.back in self._agents:
                self._agents[result.back]["after_preempt"] = True
            self._save_quietly()
            return
        if not self._adapters[agent].metered:
            self._judge_preempt(state, outcome, seconds)
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
        self._save_quietly()

    def _save_quietly(self) -> None:
        try:
            self.save()
        except (StateError, OSError):
            pass  # memory is authoritative; the next save() writes all of it

    @staticmethod
    def _judge_preempt(state: dict, outcome: str, seconds: float | None) -> None:
        """Block or unblock a plan agent for preempting, from a round of its own. The round that
        follows a preemption it caused is the one that counts against it."""
        followed, state["after_preempt"] = state["after_preempt"], False
        if outcome in (COMPLETED, TIMEOUT) or (seconds is not None and seconds >= PREEMPT_PROOF_S):
            state["blocked"] = False
        elif followed and seconds is not None:
            state["blocked"] = True

    @staticmethod
    def _backoff(state: dict, counter: str, bounds: tuple[int, int]) -> float:
        first, ceiling = bounds
        delay = min(first * 2 ** state[counter], ceiling)
        state[counter] += 1
        return float(delay)

    # persistence

    def snapshot(self) -> dict:
        """Per-agent state for state.json and the panel."""
        with self._lock:
            now = self._clock()
            agents = {}
            for name, raw in self._agents.items():
                agents[name] = {
                    "status": "cooldown" if raw["until"] > now else "ready",
                    "until": raw["until"],
                    "limit_n": raw["limit_n"],
                    "transient_n": raw["transient_n"],
                    "reason": raw["reason"],
                    "usage": dict(raw["usage"]),
                    "blocked": raw["blocked"],
                    "after_preempt": raw["after_preempt"],
                }
            return {"current": self.current, "agents": agents}

    def save(self) -> None:
        """Write the rotation into state.json next to whatever else it holds.

        Raises StateError when the file cannot be read, and then leaves it alone.
        """
        with self._lock:
            state = self._store.load_state()
            state.update(self.snapshot())
            self._store.save_state(state)

"""Sticky cyclic rotation with cooldowns and exponential backoff."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from baton.adapters.base import (
    AUTH,
    COMPLETED,
    CREDIT_GUARD,
    LIMIT,
    PREEMPTED,
    SUBAGENT,
    TIMEOUT,
    Adapter,
    Availability,
    RoundResult,
)
from baton.config import TURN_MAX_S
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
# At most this many metered rounds are ended to give way to a plan agent in 24 hours; each kill
# leaves a round's budget counted in the ledger, so this bounds that overcount whatever the cause.
MAX_PREEMPTS_PER_DAY = 6
PREEMPT_WINDOW_S = 24 * 3600
# A time further ahead than this is not a clock being a little off, it is a bad value.
PREEMPT_SKEW_S = 3600
# A reset further out than this is a bad reading (milliseconds for seconds, say), not a reset,
# and counts as unknown.
MAX_COOLDOWN_S = 8 * 24 * 3600
# A round ended for a subagent on a model outside the pair leaves the agent available, but this
# many in a row (in one run) rest it, with the backoff of a failing agent, until it completes one.
SUBAGENT_STRIKES = 3


@dataclass(frozen=True)
class TurnLimits:
    """What ends an agent's turn, and what keeps it out of a goal. A budget of None is none."""

    turn_max_s: int = TURN_MAX_S
    turn_budget_usd: float | None = None
    goal_budget_usd: float | None = None


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def preempts_in_window(raw: Any, now: float) -> list[float]:
    """The epoch times in `raw` that are valid and fall in the last 24 hours, oldest first, no
    more than the cap's worth (the newest). Anything else, or a `raw` that is no list, is
    dropped."""
    if not isinstance(raw, list):
        return []
    times = []
    for item in raw:
        t = _num(item)
        if t is not None and now - PREEMPT_WINDOW_S < t <= now + PREEMPT_SKEW_S:
            times.append(t)
    return sorted(times)[-MAX_PREEMPTS_PER_DAY:]


def preempt_cap_frees_at(raw: Any, now: float) -> float | None:
    """When the day's preemptions run out for good, or None while the cap is not reached."""
    window = preempts_in_window(raw, now)
    if len(window) < MAX_PREEMPTS_PER_DAY:
        return None
    return window[-MAX_PREEMPTS_PER_DAY] + PREEMPT_WINDOW_S


def _span(seconds: int) -> str:
    """A turn's length as the person reads it: `5 hours`, `90 minutes`."""
    for size, unit in ((3600, "hour"), (60, "minute")):
        if seconds % size == 0:
            n = seconds // size
            return f"{n} {unit}" + ("" if n == 1 else "s")
    return f"{seconds} seconds"


def _usd(amount: float) -> str:
    return f"USD {amount:.2f}".rstrip("0").rstrip(".")


def read_availability(adapter: Adapter) -> Availability:
    """The adapter's availability; an agent that cannot be read is an unavailable one."""
    try:
        return adapter.availability()
    except Exception as exc:
        return Availability(False, reason=f"availability check failed: {exc}")


class Scheduler:
    """Decides which agent runs next.

    The queue is the agents that do not `reserve`, in `order`. The current one keeps working
    until it is unavailable, its turn is over (`turn_over`, ended by the runner with `rotate`) or
    the person asks for the next; only then does the next one in `order` take over, wrapping
    around, and an agent that comes back never preempts another that is working. An agent that
    reserves (a metered one the person keeps for last) is a fallback wherever it stands in
    `order`: it is picked only while no queue agent is available. While one runs, the runner
    reads the queue agents with `plan_ready_to_check` and `record_read`, and ends the reserve
    round when one is back; `record` gives that round (`preempted`) no cooldown. A turn is the
    time an agent's rounds have worked since it took over, and what they spent; an agent
    `drop`ped is out of the queue until the goal changes. A queue agent that reads ok but whose
    next round fails within `PREEMPT_PROOF_S` (and not as completed or timeout) is blocked:
    `plan_ready_to_check` leaves it out, so it stops costing a reserve round per attempt, until a
    round of it completes, times out or lasts that long. `pick` still tries it at a round
    boundary. An agent that carries that mark and reads not available in `pick` (the watcher saw
    it ok, the read there said no) is blocked at once, as if its round had failed. As a
    backstop, `MAX_PREEMPTS_PER_DAY` preemptions in 24 hours make `plan_ready_to_check` return
    nothing, so the watcher stops reading and killing. With everyone exhausted `pick` returns
    None and `next_wake` says when to look again.

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
        limits: Mapping[str, TurnLimits] | None = None,
    ) -> None:
        self.order = [name for name in dict.fromkeys(order) if name in adapters]
        if not self.order:
            raise ValueError("no adapter for any agent in order")
        self._adapters = adapters
        self._store = store
        self._clock = clock
        self._limits = limits or {}
        self._lock = threading.RLock()
        self._agents: dict[str, dict] = {name: self._blank() for name in self.order}
        self.current: str = self.order[0]
        self._preempts: list[float] = []
        self._turn: dict = self._no_turn()
        self._dropped: set[str] = set()
        saved = store.load_state()  # StateError is the caller's to survive
        if saved.get("current") in self._agents:
            self.current = saved["current"]
        self._preempts = preempts_in_window(saved.get("preempts"), clock())
        self._restore_turn(saved.get("turn"))
        dropped = saved.get("dropped")
        if isinstance(dropped, list):
            self._dropped = {n for n in dropped if isinstance(n, str) and n in self._agents}
        stored = saved.get("agents")
        if isinstance(stored, dict):
            for name, raw in stored.items():
                if name in self._agents and isinstance(raw, dict):
                    self._restore(self._agents[name], raw)

    @staticmethod
    def _no_turn() -> dict:
        return {"agent": "", "worked_s": 0.0, "spent": 0.0}

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
            "goal_spent": 0.0,
            "subagent_n": 0,  # in a row, in this run: not written to state.json
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
        spent = _num(raw.get("goal_spent"))
        agent["goal_spent"] = spent if spent is not None and spent > 0 else 0.0

    def _restore_turn(self, raw: Any) -> None:
        """The turn saved in state.json. A damaged one is no turn; one from before turns counted
        worked time (it has `started`) keeps its spend and has worked nothing."""
        if not isinstance(raw, dict):
            return
        agent = raw.get("agent")
        if not isinstance(agent, str) or agent not in self._agents:
            return
        worked = _num(raw.get("worked_s")) if "worked_s" in raw else 0.0
        spent = _num(raw.get("spent"))
        if worked is None or worked < 0 or spent is None or spent < 0:
            return
        self._turn = {"agent": agent, "worked_s": worked, "spent": spent}

    def _known(self, until: Any, now: float) -> float | None:
        """`until` as a usable reset time, or None when it is missing, past or nonsense."""
        value = _num(until)
        if value is None or value <= now or value > now + MAX_COOLDOWN_S:
            return None
        return value

    # choosing

    def _plan_names(self) -> list[str]:
        """The queue: the agents that do not reserve and are not dropped, in `order`."""
        return [n for n in self.order if not self._adapters[n].reserve and n not in self._dropped]

    def _start_in(self, queue: list[str]) -> int:
        """Where in `queue` the walk begins: at the current agent, or, when that one is dropped,
        at the first agent after its place in `order`, and else at the top."""
        if self.current in queue:
            return queue.index(self.current)
        if self.current in self._dropped:
            at = self.order.index(self.current)
            for i, name in enumerate(queue):
                if self.order.index(name) > at:
                    return i
        return 0

    def _candidates(self) -> list[str]:
        """The queue, from the current agent (see `_start_in`), wrapping around; then the reserve
        agents, in `order`. A dropped agent is in neither."""
        plan = self._plan_names()
        at = self._start_in(plan)
        plan = plan[at:] + plan[:at]
        return plan + [
            n for n in self.order if self._adapters[n].reserve and n not in self._dropped
        ]

    def _begin(self, name: str) -> None:
        """`name` is the agent that runs now: a turn of its own starts unless it already holds
        the current one."""
        self.current = name
        if self._turn["agent"] != name:
            self._turn = {"agent": name, "worked_s": 0.0, "spent": 0.0}

    def new_turn(self, name: str) -> None:
        """Start `name`'s turn over: nothing worked, nothing spent."""
        with self._lock:
            self._turn = {"agent": name, "worked_s": 0.0, "spent": 0.0}
            self._save_quietly()

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

    def pick(self, avoid: str | None = None) -> Adapter | None:
        """The adapter to run now, or None when every agent is cooling down or unavailable.

        `avoid` names the agent a round should not go to, if another plan agent can take it (a
        review is better done by someone other than the builder). When it is the agent that would
        be returned and another queue agent reads available, that one is returned for this round
        only: `current` stays where it was, so the next ordinary round continues as before. A
        reserve agent is never returned because of `avoid`."""
        with self._lock:
            for name in self._candidates():
                found = self._try(name)
                if found is None:
                    continue
                if avoid is None or name != avoid or self._adapters[name].reserve:
                    self._begin(name)
                    return found
                # The agent to avoid is the one that would run: look for another queue agent.
                for other in self._candidates():
                    if other == name or self._adapters[other].reserve:
                        continue
                    alternative = self._try(other)
                    if alternative is not None:
                        return alternative
                self._begin(name)
                return found
            return None

    def _try(self, name: str) -> Adapter | None:
        """Read `name` if it is not cooling down: its adapter when it is available."""
        agent = self._agents[name]
        if agent["until"] > self._clock():
            return None
        adapter = self._adapters[name]
        avail = read_availability(adapter)
        self._apply(agent, avail)
        if avail.ok:
            return adapter
        self._block_unproven(agent)
        return None

    def _block_unproven(self, agent: dict) -> None:
        """An agent whose preemption it caused and that now reads not available: the watcher saw
        it ok and this read says no, so it cannot hold a round, and the watcher must not kill
        another one for it. Same as a round of it failing at once."""
        if agent["after_preempt"]:
            agent["blocked"], agent["after_preempt"] = True, False
            self._save_quietly()

    def plan_ready_to_check(self) -> list[str]:
        """The queue agents whose cooldown has passed and that are not blocked, in `order`: the
        ones worth reading while a reserve agent runs. Moves nothing. Nothing at all once the day's
        preemptions are used up."""
        with self._lock:
            now = self._clock()
            if len(preempts_in_window(self._preempts, now)) >= MAX_PREEMPTS_PER_DAY:
                return []
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

    # turns

    def note_round(self, agent: str, seconds: float, usd: float, goal: bool = False) -> None:
        """Count a round of `agent`: its time and cost go to the turn when `agent` holds it, and
        its cost to the goal's spend when a goal is set (`goal`). A round of an agent that does
        not hold the turn (a review it was lent for) counts for the goal alone."""
        if agent not in self._agents:
            return
        worked, amount = _num(seconds), _num(usd)
        worked = worked if worked is not None and worked > 0 else 0.0
        amount = amount if amount is not None and amount > 0 else 0.0
        with self._lock:
            if goal:
                self._agents[agent]["goal_spent"] += amount
            if self._turn["agent"] == agent:
                self._turn["worked_s"] += worked
                self._turn["spent"] += amount
            self._save_quietly()

    def turn_reason(self, agent: str) -> str:
        """Why the turn `agent` holds is over (`5 hours`, `USD 50`), or "" while it is not, when
        it holds none, or when `agent` reserves: the turn of a reserve agent never ends. Worked
        time counts first."""
        with self._lock:
            if self._turn["agent"] != agent or self._adapters[agent].reserve:
                return ""
            limits = self._limits.get(agent, TurnLimits())
            if self._turn["worked_s"] >= limits.turn_max_s:
                return _span(limits.turn_max_s)
            budget = limits.turn_budget_usd
            if budget is not None and self._turn["spent"] >= budget:
                return _usd(budget)
            return ""

    def turn_over(self, agent: str) -> bool:
        return bool(self.turn_reason(agent))

    def rotate(self) -> str:
        """Give the queue to the next agent in `order` that is not dropped, with a turn of its
        own, and return its name. With no other agent in the queue it stays where it is and
        nothing changes. An agent that cannot run is still moved to: `pick` skips it, as it skips
        any, so the one that runs may be another (or the same one)."""
        with self._lock:
            queue = self._plan_names()
            if len(queue) < 2:
                return self.current
            at = self._start_in(queue)
            nxt = queue[(at + 1) % len(queue)] if self.current in queue else queue[at]
            self._begin(nxt)
            self._save_quietly()
            return nxt

    def goal_over(self, agent: str) -> bool:
        """Whether `agent` has spent its goal budget (there is none for most agents)."""
        with self._lock:
            budget = self._limits.get(agent, TurnLimits()).goal_budget_usd
            return (
                agent in self._agents
                and budget is not None
                and self._agents[agent]["goal_spent"] >= budget
            )

    def drop(self, agent: str) -> None:
        """Leave `agent` out of the queue until the goal changes."""
        with self._lock:
            if agent in self._agents:
                self._dropped.add(agent)
                self._save_quietly()

    @property
    def dropped(self) -> set[str]:
        with self._lock:
            return set(self._dropped)

    def new_goal(self) -> None:
        """The goal was set, changed or cleared: every dropped agent is back and the goal's
        spend starts from nothing."""
        with self._lock:
            self._dropped.clear()
            for agent in self._agents.values():
                agent["goal_spent"] = 0.0
            self._save_quietly()

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
            self._preempts = preempts_in_window([*self._preempts, now], now)
            self._save_quietly()
            return
        if not self._adapters[agent].reserve:
            self._judge_preempt(state, outcome, seconds)
        if outcome in (COMPLETED, TIMEOUT):
            state.update(until=0.0, limit_n=0, transient_n=0, reason="", subagent_n=0)
        elif outcome == SUBAGENT:
            state["subagent_n"] += 1
            if state["subagent_n"] >= SUBAGENT_STRIKES:
                state["until"] = now + self._backoff(state, "transient_n", TRANSIENT_BACKOFF)
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
                    "goal_spent": raw["goal_spent"],
                }
            return {
                "current": self.current,
                "agents": agents,
                "preempts": preempts_in_window(self._preempts, now),
                "turn": dict(self._turn),
                "dropped": [n for n in self.order if n in self._dropped],
            }

    def save(self) -> None:
        """Write the rotation into state.json next to whatever else it holds.

        Raises StateError when the file cannot be read, and then leaves it alone.
        """
        with self._lock:
            state = self._store.load_state()
            state.update(self.snapshot())
            self._store.save_state(state)

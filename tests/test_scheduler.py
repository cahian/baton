import pytest

from baton.adapters.base import (
    AUTH,
    COMPLETED,
    CREDIT_GUARD,
    LIMIT,
    TIMEOUT,
    TRANSIENT,
    Availability,
    RoundResult,
)
from baton.scheduler import Scheduler
from baton.store import StateError, Store


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class Agent:
    """Adapter stand-in: availability is a settable value or a raising callable."""

    def __init__(self, name):
        self.name = name
        self.avail = Availability(True)
        self.calls = 0

    def availability(self):
        self.calls += 1
        if isinstance(self.avail, Exception):
            raise self.avail
        return self.avail

    def run_round(self, prompt, ctx):  # pragma: no cover
        raise AssertionError

    def render(self, line):  # pragma: no cover
        return line


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    agents = {"codex": Agent("codex"), "antigravity": Agent("antigravity")}
    store = Store(tmp_path / "goal")

    def make():
        return Scheduler(("codex", "antigravity"), agents, store, clock)

    return clock, agents, store, make


def test_starts_with_first_in_order(env):
    _, _, _, make = env
    assert make().pick().name == "codex"


def test_stays_on_current_until_unavailable(env):
    clock, agents, _, make = env
    s = make()
    assert s.pick().name == "codex"
    s.record("codex", RoundResult(COMPLETED))
    assert s.pick().name == "codex"
    agents["codex"].avail = Availability(False, reason="threshold")
    assert s.pick().name == "antigravity"


def test_returning_agent_does_not_preempt(env):
    clock, agents, _, make = env
    s = make()
    s.pick()
    s.record("codex", RoundResult(LIMIT, until=clock.now + 100))
    assert s.pick().name == "antigravity"
    clock.now += 200  # codex is back, antigravity is working
    assert s.pick().name == "antigravity"
    assert s.pick().name == "antigravity"


def test_both_exhausted_waits_then_earliest_reset_continues(env):
    clock, agents, _, make = env
    s = make()
    s.pick()
    s.record("codex", RoundResult(LIMIT, until=clock.now + 7200))
    s.pick()
    s.record("antigravity", RoundResult(LIMIT, until=clock.now + 3600))
    assert s.pick() is None
    assert s.next_wake() == clock.now + 3600
    clock.now += 3600
    assert s.pick().name == "antigravity"
    clock.now += 3600  # codex returns, still no preemption
    assert s.pick().name == "antigravity"


def test_skips_unavailable(env):
    clock, agents, _, make = env
    agents["codex"].avail = Availability(False, until=clock.now + 500, reason="blind")
    s = make()
    assert s.pick().name == "antigravity"
    assert s.snapshot()["agents"]["codex"]["until"] == clock.now + 500


def test_unavailable_without_reset_retries_after_five_minutes(env):
    clock, agents, _, make = env
    agents["codex"].avail = Availability(False, reason="unreadable")
    agents["antigravity"].avail = Availability(False, reason="unreadable")
    s = make()
    assert s.pick() is None
    assert s.next_wake() == clock.now + 300
    calls = agents["codex"].calls
    clock.now += 100
    assert s.pick() is None
    assert agents["codex"].calls == calls  # not asked again during the cooldown
    clock.now += 200
    agents["codex"].avail = Availability(True)
    assert s.pick().name == "codex"


def test_backoff_doubles_and_caps(env):
    clock, _, _, make = env
    s = make()
    t = clock.now

    def cooldown(outcome):
        s.record("codex", RoundResult(outcome))
        return s.snapshot()["agents"]["codex"]["until"] - t

    assert [cooldown(LIMIT) for _ in range(5)] == [900, 1800, 3600, 3600, 3600]
    s.record("codex", RoundResult(COMPLETED))
    assert [cooldown(TRANSIENT) for _ in range(7)] == [60, 120, 240, 480, 960, 1800, 1800]
    assert cooldown(CREDIT_GUARD) == 1800
    assert cooldown(AUTH) == 1800


def test_known_reset_is_used_for_limit_and_credit_guard(env):
    clock, _, _, make = env
    s = make()
    s.record("codex", RoundResult(LIMIT, until=clock.now + 4242))
    assert s.snapshot()["agents"]["codex"]["until"] == clock.now + 4242
    s.record("antigravity", RoundResult(CREDIT_GUARD, until=clock.now + 777))
    assert s.snapshot()["agents"]["antigravity"]["until"] == clock.now + 777


@pytest.mark.parametrize("until", [None, 0, 0.0, -5, "soon", float("nan")])
def test_past_or_null_until_treated_as_unknown(env, until):
    clock, _, _, make = env
    s = make()
    s.record("codex", RoundResult(LIMIT, until=until))
    assert s.snapshot()["agents"]["codex"]["until"] == clock.now + 900
    s.record("codex", RoundResult(CREDIT_GUARD, until=clock.now - 1))
    assert s.snapshot()["agents"]["codex"]["until"] == clock.now + 1800


def test_absurd_reset_counts_as_unknown(env):
    clock, _, _, make = env
    s = make()
    s.record("codex", RoundResult(LIMIT, until=clock.now * 1000))  # milliseconds by mistake
    assert s.snapshot()["agents"]["codex"]["until"] == clock.now + 900  # backoff, not 8 days
    s.record("antigravity", RoundResult(CREDIT_GUARD, until=clock.now + 9 * 86400))
    assert s.snapshot()["agents"]["antigravity"]["until"] == clock.now + 1800
    s.record("codex", RoundResult(COMPLETED))
    s.record("codex", RoundResult(LIMIT, until=clock.now + 7 * 86400))  # a weekly reset is real
    assert s.snapshot()["agents"]["codex"]["until"] == clock.now + 7 * 86400


@pytest.mark.parametrize("outcome", [COMPLETED, TIMEOUT])
def test_completed_clears_backoff(env, outcome):
    clock, _, _, make = env
    s = make()
    for _ in range(3):
        s.record("codex", RoundResult(TRANSIENT))
        s.record("codex", RoundResult(LIMIT))
    s.record("codex", RoundResult(outcome))
    snap = s.snapshot()["agents"]["codex"]
    assert snap["until"] == 0
    s.record("codex", RoundResult(TRANSIENT))
    assert s.snapshot()["agents"]["codex"]["until"] == clock.now + 60
    s.record("codex", RoundResult(LIMIT))
    assert s.snapshot()["agents"]["codex"]["until"] == clock.now + 900


def test_failed_resume_never_counts_against_the_agent(env):
    clock, _, _, make = env
    s = make()
    s.record("codex", RoundResult(TRANSIENT, resume_failed=True))
    snap = s.snapshot()["agents"]["codex"]
    assert snap["until"] == 0 and snap["transient_n"] == 0


def test_availability_exception_is_unavailable(env):
    clock, agents, _, make = env
    agents["codex"].avail = RuntimeError("boom")
    s = make()
    assert s.pick().name == "antigravity"
    assert s.snapshot()["agents"]["codex"]["until"] == clock.now + 300
    assert "boom" in s.snapshot()["agents"]["codex"]["reason"]


def test_next_wake_is_at_least_thirty_seconds_out(env):
    clock, agents, _, make = env
    s = make()
    s.record("codex", RoundResult(LIMIT, until=clock.now + 5))
    s.record("antigravity", RoundResult(LIMIT, until=clock.now + 10))
    assert s.next_wake() == clock.now + 30
    # nothing known at all
    assert make().next_wake() == clock.now + 30


def test_state_survives_new_scheduler(env):
    clock, agents, store, make = env
    s = make()
    s.pick()
    s.record("codex", RoundResult(LIMIT, until=clock.now + 3600))
    s.record("antigravity", RoundResult(TRANSIENT))
    s.record("antigravity", RoundResult(TRANSIENT))
    assert s.pick() is None
    s.save()
    again = make()
    assert again.snapshot()["agents"]["codex"]["until"] == clock.now + 3600
    assert again.snapshot()["current"] == s.snapshot()["current"]
    assert again.pick() is None
    again.record("antigravity", RoundResult(TRANSIENT))  # third failure: counter was restored
    assert again.snapshot()["agents"]["antigravity"]["until"] == clock.now + 240


def test_record_persists_without_a_separate_save(env):
    clock, _, _, make = env
    s = make()
    s.record("codex", RoundResult(LIMIT, until=clock.now + 3600))
    assert make().snapshot()["agents"]["codex"]["until"] == clock.now + 3600


def test_current_persists(env):
    clock, agents, _, make = env
    agents["codex"].avail = Availability(False, until=clock.now + 9999)
    s = make()
    assert s.pick().name == "antigravity"
    s.save()
    agents["codex"].avail = Availability(True)
    clock.now += 99999
    assert make().pick().name == "antigravity"  # sticky across restarts


def test_state_error_keeps_memory_and_never_rewrites(env):
    clock, agents, store, make = env
    s = make()
    s.record("codex", RoundResult(LIMIT, until=clock.now + 3600))
    (store.root / "state.json").write_text("{not json")
    with pytest.raises(StateError):
        s.save()
    assert (store.root / "state.json").read_text() == "{not json"
    s.record("antigravity", RoundResult(TRANSIENT))  # persisting fails quietly here
    assert s.snapshot()["agents"]["codex"]["until"] == clock.now + 3600
    assert s.pick() is None  # both cooling down, in memory
    (store.root / "state.json").unlink()
    s.save()  # the next save carries everything, nothing was lost
    again = make()
    assert again.snapshot()["agents"]["codex"]["until"] == clock.now + 3600
    assert again.snapshot()["agents"]["antigravity"]["until"] == clock.now + 60


def test_unknown_names_in_state_or_order_are_ignored(env):
    clock, agents, store, _ = env
    store.save_state({"current": "claude", "agents": {"claude": {"until": 9e12}, "codex": 7}})
    s = Scheduler(("codex", "antigravity", "claude"), agents, store, clock)
    assert s.pick().name == "codex"

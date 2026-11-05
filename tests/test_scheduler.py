import pytest

from baton.adapters.base import (
    AUTH,
    COMPLETED,
    CREDIT_GUARD,
    LIMIT,
    PREEMPTED,
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

    def __init__(self, name, metered=False):
        self.name = name
        self.metered = metered
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


def trio(tmp_path, order=("codex", "antigravity", "claude_api"), saved=None):
    clock = Clock()
    agents = {n: Agent(n, metered=n == "claude_api") for n in order}
    store = Store(tmp_path / "goal")
    if saved:
        store.save_state(saved)
    return clock, agents, Scheduler(order, agents, store, clock)


def out(agent, clock, seconds=10_000):
    agent.avail = Availability(False, until=clock.now + seconds, reason="used up")


def test_codex_out_goes_to_antigravity(tmp_path):
    clock, agents, s = trio(tmp_path)
    assert s.pick().name == "codex"
    out(agents["codex"], clock)
    assert s.pick().name == "antigravity"


def test_antigravity_out_goes_back_to_codex_when_it_is_back(tmp_path):
    clock, agents, s = trio(tmp_path)
    out(agents["codex"], clock, 500)
    assert s.pick().name == "antigravity"
    out(agents["antigravity"], clock)
    agents["codex"].avail = Availability(True)
    clock.now += 501
    assert s.pick().name == "codex"


def test_both_plan_agents_out_goes_to_claude(tmp_path):
    clock, agents, s = trio(tmp_path)
    out(agents["codex"], clock)
    out(agents["antigravity"], clock)
    assert s.pick().name == "claude_api"


def test_claude_first_in_order_still_runs_only_when_no_plan_agent_is_available(tmp_path):
    order = ("claude_api", "antigravity", "codex")
    clock, agents, s = trio(tmp_path, order)
    assert s.pick().name == "antigravity"  # not claude_api, though it is first
    out(agents["antigravity"], clock)
    assert s.pick().name == "codex"
    out(agents["codex"], clock)
    assert s.pick().name == "claude_api"


def test_from_claude_only_antigravity_back_then_codex_next(tmp_path):
    clock, agents, s = trio(tmp_path, saved={"current": "claude_api"})
    out(agents["codex"], clock, 20_000)
    out(agents["antigravity"], clock, 500)
    assert s.pick().name == "claude_api"
    clock.now += 501
    agents["antigravity"].avail = Availability(True)
    assert s.pick().name == "antigravity"
    out(agents["antigravity"], clock)
    agents["codex"].avail = Availability(True)
    clock.now += 20_000
    assert s.pick().name == "codex"  # antigravity ran out: wraps to the plan agent that is back


def test_from_claude_both_back_takes_the_first_in_order(tmp_path):
    order = ("antigravity", "codex", "claude_api")
    clock, agents, s = trio(tmp_path, order, saved={"current": "claude_api"})
    assert s.pick().name == "antigravity"


def test_a_plan_agent_coming_back_does_not_preempt_another_plan_agent(tmp_path):
    clock, agents, s = trio(tmp_path)
    s.pick()
    s.record("codex", RoundResult(LIMIT, until=clock.now + 100))
    assert s.pick().name == "antigravity"
    clock.now += 200
    assert s.pick().name == "antigravity"


def test_only_claude_enabled_still_runs(tmp_path):
    clock, agents, s = trio(tmp_path, ("claude_api",))
    assert s.pick().name == "claude_api"


def test_state_restored_with_current_claude_behaves_the_same(tmp_path):
    clock, agents, s = trio(tmp_path, saved={"current": "claude_api"})
    assert s.current == "claude_api"
    assert s.pick().name == "codex"  # plan agents first even when restored on claude
    out(agents["codex"], clock)
    out(agents["antigravity"], clock)
    assert s.pick().name == "claude_api"


def test_agents_in_cooldown_are_still_skipped_before_claude(tmp_path):
    clock, agents, s = trio(tmp_path)
    s.record("codex", RoundResult(LIMIT, until=clock.now + 100))
    calls = agents["codex"].calls
    assert s.pick().name == "antigravity"
    assert agents["codex"].calls == calls


def test_preempted_leaves_the_agent_untouched(tmp_path):
    clock, agents, s = trio(tmp_path)
    s.record("claude_api", RoundResult(TRANSIENT, reason="boom"))
    s.record("claude_api", RoundResult(LIMIT, until=clock.now + 900))
    before = s.snapshot()["agents"]["claude_api"]
    s.record("claude_api", RoundResult(PREEMPTED, reason="codex is back"))
    assert s.snapshot()["agents"]["claude_api"] == before
    assert before["until"] == clock.now + 900 and before["transient_n"] == 1


def test_preempted_gives_a_fresh_agent_no_cooldown(tmp_path):
    clock, _, s = trio(tmp_path)
    s.record("claude_api", RoundResult(PREEMPTED, reason="codex is back"))
    snap = s.snapshot()["agents"]["claude_api"]
    assert snap["until"] == 0 and snap["transient_n"] == 0 and snap["limit_n"] == 0
    assert snap["reason"] == ""


def test_plan_ready_to_check_lists_plan_agents_past_their_until(tmp_path):
    clock, agents, s = trio(tmp_path)
    out(agents["codex"], clock, 100)
    out(agents["antigravity"], clock, 200)
    assert s.pick().name == "claude_api"
    assert s.plan_ready_to_check() == []
    clock.now += 100
    assert s.plan_ready_to_check() == ["codex"]
    clock.now += 100
    assert s.plan_ready_to_check() == ["codex", "antigravity"]


def test_record_read_refreshes_until_like_pick_and_never_moves_current(tmp_path):
    clock, agents, s = trio(tmp_path)
    out(agents["codex"], clock, 100)
    out(agents["antigravity"], clock, 100)
    s.pick()
    clock.now += 100
    s.record_read("codex", Availability(False, until=clock.now + 900, reason="still full"))
    s.record_read("antigravity", Availability(False, reason="unreadable"))
    snap = s.snapshot()["agents"]
    assert snap["codex"]["until"] == clock.now + 900 and snap["codex"]["reason"] == "still full"
    assert snap["antigravity"]["until"] == clock.now + 300
    assert s.plan_ready_to_check() == []
    s.record_read("codex", Availability(True, usage={"five": 3}))
    assert s.snapshot()["agents"]["codex"]["until"] == 0
    assert s.snapshot()["agents"]["codex"]["usage"] == {"five": 3}
    assert s.plan_ready_to_check() == ["codex"]
    assert s.current == "claude_api"


def test_record_read_ignores_an_unknown_agent(tmp_path):
    _, _, s = trio(tmp_path)
    s.record_read("nope", Availability(True))


# a plan agent whose round fails at once after a preemption cannot preempt again


def preempt_then(s, agent, outcome, seconds, **kw):
    """Claude is ended because `agent` is back; then `agent` runs a round that ends as given."""
    s.record("claude_api", RoundResult(PREEMPTED, reason=f"{agent} is back", back=agent))
    s.record(agent, RoundResult(outcome, **kw), seconds=seconds)


def test_a_round_that_fails_at_once_after_a_preemption_blocks_that_agent(tmp_path):
    clock, agents, s = trio(tmp_path)
    preempt_then(s, "codex", TRANSIENT, 5, reason="boom")
    clock.now += 100_000
    assert s.plan_ready_to_check() == ["antigravity"]  # codex is out of the watcher's reach
    assert s.snapshot()["agents"]["codex"]["blocked"] is True


@pytest.mark.parametrize("outcome", [TRANSIENT, LIMIT, AUTH, CREDIT_GUARD])
def test_every_failure_other_than_completed_or_timeout_blocks(tmp_path, outcome):
    clock, _, s = trio(tmp_path)
    preempt_then(s, "codex", outcome, 5)
    clock.now += 100_000
    assert s.plan_ready_to_check() == ["antigravity"]


def test_a_blocked_agent_is_still_picked_at_a_round_boundary(tmp_path):
    clock, agents, s = trio(tmp_path)
    s.pick()
    preempt_then(s, "codex", TRANSIENT, 5)
    clock.now += 100_000
    assert s.pick().name == "codex"  # blocking stops kills, never normal selection


def test_a_completed_round_clears_the_block(tmp_path):
    clock, _, s = trio(tmp_path)
    preempt_then(s, "codex", TRANSIENT, 5)
    s.record("codex", RoundResult(COMPLETED), seconds=2)
    clock.now += 100_000
    assert s.plan_ready_to_check() == ["codex", "antigravity"]
    assert s.snapshot()["agents"]["codex"]["blocked"] is False


def test_a_timeout_clears_the_block(tmp_path):
    clock, _, s = trio(tmp_path)
    preempt_then(s, "codex", TRANSIENT, 5)
    s.record("codex", RoundResult(TIMEOUT), seconds=2)
    assert s.snapshot()["agents"]["codex"]["blocked"] is False


def test_a_long_round_clears_the_block_whatever_its_outcome(tmp_path):
    clock, _, s = trio(tmp_path)
    preempt_then(s, "codex", TRANSIENT, 5)
    s.record("codex", RoundResult(LIMIT, until=clock.now + 900), seconds=300)
    assert s.snapshot()["agents"]["codex"]["blocked"] is False


def test_a_limit_after_ten_minutes_of_work_following_a_preemption_does_not_block(tmp_path):
    clock, _, s = trio(tmp_path)
    preempt_then(s, "codex", LIMIT, 600, until=clock.now + 3600)
    clock.now += 3601
    assert s.plan_ready_to_check() == ["codex", "antigravity"]
    assert s.snapshot()["agents"]["codex"]["blocked"] is False


def test_a_round_just_under_the_proof_time_blocks(tmp_path):
    _, _, s = trio(tmp_path)
    preempt_then(s, "codex", TRANSIENT, 299.9)
    assert s.snapshot()["agents"]["codex"]["blocked"] is True


def test_a_failure_that_did_not_follow_a_preemption_does_not_block(tmp_path):
    clock, _, s = trio(tmp_path)
    s.record("codex", RoundResult(TRANSIENT, reason="boom"), seconds=5)
    clock.now += 100_000
    assert s.plan_ready_to_check() == ["codex", "antigravity"]


def test_the_mark_belongs_to_the_agent_that_was_back(tmp_path):
    _, _, s = trio(tmp_path)
    s.record("claude_api", RoundResult(PREEMPTED, back="antigravity"))
    s.record("codex", RoundResult(TRANSIENT), seconds=5)  # picked at the boundary instead
    assert s.snapshot()["agents"]["codex"]["blocked"] is False
    s.record("antigravity", RoundResult(TRANSIENT), seconds=5)
    assert s.snapshot()["agents"]["antigravity"]["blocked"] is True


def test_only_the_round_right_after_the_preemption_is_judged(tmp_path):
    _, _, s = trio(tmp_path)
    preempt_then(s, "codex", COMPLETED, 5)
    s.record("codex", RoundResult(TRANSIENT), seconds=5)
    assert s.snapshot()["agents"]["codex"]["blocked"] is False


def test_a_round_that_never_ran_does_not_use_up_the_mark(tmp_path):
    _, _, s = trio(tmp_path)
    s.record("claude_api", RoundResult(PREEMPTED, back="codex"))
    s.record("codex", RoundResult(TRANSIENT, resume_failed=True), seconds=1)
    s.record("codex", RoundResult(TRANSIENT), seconds=5)
    assert s.snapshot()["agents"]["codex"]["blocked"] is True


def test_without_a_duration_nothing_new_is_blocked(tmp_path):
    _, _, s = trio(tmp_path)
    s.record("claude_api", RoundResult(PREEMPTED, back="codex"))
    s.record("codex", RoundResult(TRANSIENT))  # an old caller
    assert s.snapshot()["agents"]["codex"]["blocked"] is False


def test_the_block_survives_a_restart(tmp_path):
    clock, agents, s = trio(tmp_path)
    preempt_then(s, "codex", TRANSIENT, 5)
    saved = s._store.load_state()
    clock2, agents2, s2 = trio(tmp_path / "again", saved=saved)
    clock2.now += 100_000
    assert s2.plan_ready_to_check() == ["antigravity"]
    assert s2.snapshot()["agents"]["codex"]["blocked"] is True


def test_the_mark_survives_a_restart(tmp_path):
    _, _, s = trio(tmp_path)
    s.record("claude_api", RoundResult(PREEMPTED, reason="codex is back", back="codex"))
    saved = s._store.load_state()  # the preemption was written as soon as it was recorded
    _, _, s2 = trio(tmp_path / "again", saved=saved)
    s2.record("codex", RoundResult(TRANSIENT), seconds=5)
    assert s2.snapshot()["agents"]["codex"]["blocked"] is True


def test_a_block_in_a_damaged_state_file_is_ignored(tmp_path):
    saved = {"agents": {"codex": {"blocked": "yes", "after_preempt": "yes"}}}
    _, _, s = trio(tmp_path, saved=saved)
    assert s.snapshot()["agents"]["codex"]["blocked"] is False
    s.record("codex", RoundResult(TRANSIENT), seconds=5)
    assert s.snapshot()["agents"]["codex"]["blocked"] is False


# a plan agent that read ok to the watcher but not to `pick` is blocked at once


def preempted_for(s, agent):
    s.record("claude_api", RoundResult(PREEMPTED, reason=f"{agent} is back", back=agent))


def test_an_agent_that_reads_not_available_in_pick_after_its_preemption_is_blocked(tmp_path):
    clock, agents, s = trio(tmp_path)
    preempted_for(s, "codex")
    out(agents["codex"], clock)  # the watcher read ok; the read in pick says no
    assert s.pick().name == "antigravity"
    snap = s.snapshot()["agents"]["codex"]
    assert snap["blocked"] is True and snap["after_preempt"] is False
    clock.now += 100_000
    assert s.plan_ready_to_check() == ["antigravity"]  # codex is out of the watcher's reach


def test_an_agent_whose_read_raises_in_pick_after_its_preemption_is_blocked(tmp_path):
    clock, agents, s = trio(tmp_path)
    preempted_for(s, "codex")
    agents["codex"].avail = RuntimeError("boom")
    assert s.pick().name == "antigravity"
    assert s.snapshot()["agents"]["codex"]["blocked"] is True


def test_the_block_set_in_pick_survives_a_restart(tmp_path):
    clock, agents, s = trio(tmp_path)
    preempted_for(s, "codex")
    out(agents["codex"], clock)
    s.pick()
    _, _, s2 = trio(tmp_path / "again", saved=s._store.load_state())
    assert s2.snapshot()["agents"]["codex"]["blocked"] is True


def test_an_agent_that_reads_available_in_pick_keeps_its_mark_and_is_not_blocked(tmp_path):
    _, _, s = trio(tmp_path)
    preempted_for(s, "codex")
    assert s.pick().name == "codex"
    snap = s.snapshot()["agents"]["codex"]
    assert snap["blocked"] is False and snap["after_preempt"] is True


def test_a_read_that_says_no_without_a_preemption_blocks_nothing(tmp_path):
    clock, agents, s = trio(tmp_path)
    out(agents["codex"], clock)
    s.pick()
    assert s.snapshot()["agents"]["codex"]["blocked"] is False


def test_the_block_set_in_pick_clears_as_any_other(tmp_path):
    clock, agents, s = trio(tmp_path)
    preempted_for(s, "codex")
    out(agents["codex"], clock, 10)
    out(agents["antigravity"], clock, 10_000)
    assert s.pick().name == "claude_api"
    clock.now += 100
    agents["codex"].avail = Availability(True)
    assert s.pick().name == "codex"  # still picked at a round boundary
    s.record("codex", RoundResult(COMPLETED), seconds=5)
    assert s.snapshot()["agents"]["codex"]["blocked"] is False
    assert s.plan_ready_to_check() == ["codex"]  # antigravity is still resting


# a day's worth of preemptions is bounded whatever the cause

DAY = 24 * 3600


def many_preemptions(s, n, back="codex"):
    for _ in range(n):
        s.record("claude_api", RoundResult(PREEMPTED, back=back))


def test_the_cap_is_six_a_day():
    from baton.scheduler import MAX_PREEMPTS_PER_DAY

    assert MAX_PREEMPTS_PER_DAY == 6


def test_five_preemptions_leave_the_watcher_reading_the_sixth_closes_it(tmp_path):
    _, _, s = trio(tmp_path)
    many_preemptions(s, 5)
    assert s.plan_ready_to_check() == ["codex", "antigravity"]
    many_preemptions(s, 1)
    assert s.plan_ready_to_check() == []


def test_the_cap_does_not_touch_selection_at_a_round_boundary(tmp_path):
    _, _, s = trio(tmp_path)
    many_preemptions(s, 6)
    assert s.plan_ready_to_check() == []
    assert s.pick().name == "codex"


def test_preemptions_older_than_a_day_free_the_cap(tmp_path):
    clock, _, s = trio(tmp_path)
    many_preemptions(s, 6)
    clock.now += DAY - 1
    assert s.plan_ready_to_check() == []
    clock.now += 2
    assert s.plan_ready_to_check() == ["codex", "antigravity"]
    assert s.snapshot()["preempts"] == []


def test_the_oldest_preemptions_leave_the_window_one_by_one(tmp_path):
    clock, _, s = trio(tmp_path)
    for _ in range(6):
        many_preemptions(s, 1)
        clock.now += 3600
    assert s.plan_ready_to_check() == []
    clock.now += DAY - 6 * 3600 + 1  # the first one is now over a day old
    assert s.plan_ready_to_check() == ["codex", "antigravity"]


def test_a_preempted_round_that_became_something_else_is_not_counted(tmp_path):
    _, _, s = trio(tmp_path)
    for _ in range(8):
        s.record("claude_api", RoundResult(LIMIT, until=None))
    assert s.snapshot()["preempts"] == []


def test_the_preemptions_survive_a_restart(tmp_path):
    clock, _, s = trio(tmp_path)
    many_preemptions(s, 6)
    saved = s._store.load_state()
    assert len(saved["preempts"]) == 6
    _, _, s2 = trio(tmp_path / "again", saved=saved)
    assert s2.plan_ready_to_check() == []


def test_corrupt_preemption_times_are_dropped(tmp_path):
    now = Clock().now
    junk = [None, True, "x", [], {}, float("nan"), float("inf"), -float("inf"), 0, -5.0]
    junk += [now - DAY - 10, now + 10 * DAY]  # a day old, and from the future
    saved = {"preempts": junk + [now - 10]}
    _, _, s = trio(tmp_path, saved=saved)
    assert s.snapshot()["preempts"] == [now - 10]
    _, _, s = trio(tmp_path / "b", saved={"preempts": "oops"})
    assert s.snapshot()["preempts"] == []


def test_a_damaged_list_of_six_valid_times_still_caps(tmp_path):
    now = Clock().now
    saved = {"preempts": ["x"] + [now - i for i in range(1, 7)]}
    _, _, s = trio(tmp_path, saved=saved)
    assert s.plan_ready_to_check() == []


def test_the_list_is_kept_short(tmp_path):
    now = Clock().now
    _, _, s = trio(tmp_path, saved={"preempts": [now - i for i in range(1, 500)]})
    assert len(s.snapshot()["preempts"]) == 6


# --- pick(avoid=...): review goes to another provider without moving the rotation -------------


def test_avoid_returns_the_other_plan_agent_and_leaves_current_alone(env):
    clock, agents, store, make = env
    sched = make()
    assert sched.pick().name == "codex"
    assert sched.pick(avoid="codex").name == "antigravity"
    assert sched.current == "codex"  # the next non-review round continues as before
    assert sched.pick().name == "codex"


def test_avoid_does_not_persist_a_change_of_current(env):
    clock, agents, store, make = env
    sched = make()
    sched.pick()
    sched.pick(avoid="codex")
    sched.save()
    assert make().current == "codex"


def test_avoid_with_no_other_plan_agent_returns_the_same_provider(env):
    clock, agents, store, make = env
    agents["antigravity"].avail = Availability(False, reason="out")
    sched = make()
    assert sched.pick(avoid="codex").name == "codex"


def test_avoid_not_the_pick_changes_nothing(env):
    clock, agents, store, make = env
    sched = make()
    assert sched.pick(avoid="antigravity").name == "codex"
    assert sched.current == "codex"


def test_avoid_never_returns_a_metered_agent(tmp_path):
    clock = Clock()
    agents = {
        "codex": Agent("codex"),
        "antigravity": Agent("antigravity"),
        "claude_api": Agent("claude_api", metered=True),
    }
    agents["antigravity"].avail = Availability(False, reason="out")
    sched = Scheduler(("codex", "antigravity", "claude_api"), agents, Store(tmp_path / "g"), clock)
    assert sched.pick(avoid="codex").name == "codex"  # Claude is not brought in because of avoid
    agents["codex"].avail = Availability(False, reason="out")
    assert sched.pick(avoid="codex").name == "claude_api"  # both out: Claude, as always
    assert sched.pick(avoid="claude_api").name == "claude_api"


def test_avoid_skips_an_alternative_that_is_cooling_down(env):
    clock, agents, store, make = env
    sched = make()
    sched.pick()
    sched.record("antigravity", RoundResult(LIMIT, until=clock.now + 3600))
    assert sched.pick(avoid="codex").name == "codex"


# --- the subagent outcome ----------------------------------------------------------------------


def test_a_subagent_round_keeps_the_agent_available_twice_and_rests_it_the_third_time(env):
    from baton.adapters.base import SUBAGENT

    clock, agents, store, make = env
    sched = make()
    for _ in range(2):
        assert sched.pick().name == "codex"
        sched.record("codex", RoundResult(SUBAGENT, reason="model"))
    assert sched.pick().name == "codex"  # still the one that works
    sched.record("codex", RoundResult(SUBAGENT, reason="model"))
    assert sched.pick().name == "antigravity"  # rested after three in a row
    clock.now += 61  # the first backoff of a failing agent
    assert sched.pick().name == "antigravity"  # sticky until it is unavailable
    agents["antigravity"].avail = Availability(False, reason="out")
    assert sched.pick().name == "codex"


def test_a_completed_round_resets_the_subagent_count(env):
    from baton.adapters.base import SUBAGENT

    clock, agents, store, make = env
    sched = make()
    for outcome in (SUBAGENT, SUBAGENT, COMPLETED, SUBAGENT, SUBAGENT):
        sched.record("codex", RoundResult(outcome))
    assert sched.pick().name == "codex"  # never three in a row


def test_the_subagent_count_is_for_one_run_only(env):
    from baton.adapters.base import SUBAGENT

    clock, agents, store, make = env
    sched = make()
    for _ in range(2):
        sched.record("codex", RoundResult(SUBAGENT))
    sched.save()
    again = make()  # a new run starts the count again
    again.record("codex", RoundResult(SUBAGENT))
    assert again.pick().name == "codex"

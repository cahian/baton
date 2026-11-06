import json
import os
import re
import subprocess
import threading
import time
import types
from dataclasses import replace

import pytest

from baton import gateway, runner
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
    ToolWatch,
)
from baton.config import MemoryConfig, load_goal
from baton.memory import Memory, RoundEntry
from baton.proc import process_start
from baton.runner import build_prompt, run_gate, run_goal, snapshot_work
from baton.store import (
    AlreadyRunning,
    StateError,
    Store,
    WorkdirBusy,
    state_base,
    workdir_conflict,
    workdir_lock,
)

OBJECTIVE = "Make the widget frobnicate."


class World:
    """A fake clock whose sleep moves time forward."""

    def __init__(self, now=1_000_000.0):
        self.now = now
        self.sleeps: list[float] = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class Fake:
    """Scripted adapter. Each script step is `fn(agent, prompt, ctx) -> RoundResult`."""

    def __init__(self, name, metered=False):
        self.name = name
        self.metered = metered
        self.avail = Availability(True)
        self.script = []
        self.prompts = []
        self.sessions = []

    def availability(self):
        return self.avail

    def run_round(self, prompt, ctx):
        self.prompts.append(prompt)
        self.sessions.append(ctx.session_id)
        step = self.script.pop(0) if self.script else (lambda a, p, c: RoundResult(COMPLETED))
        if not getattr(step, "refuses", False):
            ctx.on_spawn(1)  # the process started (group 1 is never killed by a later run)
        return step(self, prompt, ctx)

    def render(self, line):
        return None if line.startswith("#") else line


def done(workdir):
    """Step that satisfies the gate."""

    def step(agent, prompt, ctx):
        (workdir / "DONE").write_text("x")
        return RoundResult(COMPLETED)

    return step


def result(outcome, **kw):
    return lambda agent, prompt, ctx: RoundResult(outcome, **kw)


def refused(outcome=TRANSIENT, **kw):
    """Step for a start that was refused or failed to spawn: nothing ran."""

    def step(agent, prompt, ctx):
        return RoundResult(outcome, **kw)

    step.refuses = True
    return step


def say(text, then=None):
    def step(agent, prompt, ctx):
        for line in text:
            ctx.on_output(line)
        return then(agent, prompt, ctx) if then else RoundResult(COMPLETED)

    return step


@pytest.fixture
def world():
    return World()


@pytest.fixture
def work(tmp_path):
    path = tmp_path / "work"
    path.mkdir()
    return path


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "state")


def make_goal(
    tmp_path,
    work,
    verify="test -f DONE",
    snapshot=False,
    verify_extra="",
    order='"codex", "antigravity"',
    top="",
):
    lines = [
        'name = "demo"',
        f'workdir = "{work}"',
        f'objective = "{OBJECTIVE}"',
        f"snapshot = {str(snapshot).lower()}",
        f"order = [{order}]",
        top,
    ]
    if verify:
        lines += ["[verify]", f'command = "{verify}"', verify_extra]
    path = tmp_path / "goal.toml"
    path.write_text("\n".join(lines) + "\n")
    return load_goal(path)


@pytest.fixture
def agents():
    return {"codex": Fake("codex"), "antigravity": Fake("antigravity")}


@pytest.fixture
def go(tmp_path, work, store, agents, world):
    def run(goal=None, adapters=None, st=None, sleep=None):
        return run_goal(
            goal or make_goal(tmp_path, work),
            adapters or agents,
            st or store,
            clock=world.clock,
            sleep=sleep or world.sleep,
        )

    return run


def kinds(store):
    return [e["kind"] for e in store.events()]


# the loop


def test_gate_passing_before_first_round_runs_no_agent(go, agents, work, store):
    (work / "DONE").write_text("x")
    assert go() == 0
    assert agents["codex"].prompts == [] and agents["antigravity"].prompts == []
    assert kinds(store)[-1] == "finished"


def test_agent_claiming_done_does_not_finish(go, agents, work, store):
    agents["codex"].script = [say(["all done, truly done"]), done(work)]
    assert go() == 0
    assert len(agents["codex"].prompts) == 2
    assert kinds(store).count("gate") == 3  # before, after round 1, after round 2


def test_rotates_limit_then_finishes(go, agents, work, store, world):
    agents["codex"].script = [result(LIMIT, until=world.now + 3600)]
    agents["antigravity"].script = [done(work)]
    assert go() == 0
    assert len(agents["codex"].prompts) == 1 and len(agents["antigravity"].prompts) == 1
    starts = [e for e in store.events() if e["kind"] == "round_start"]
    assert [e["agent"] for e in starts] == ["codex", "antigravity"]
    assert starts[1]["previous"] == "codex"
    assert world.sleeps == []


def test_all_exhausted_waits_then_resumes(go, agents, work, store, world):
    t0 = world.now
    agents["codex"].script = [result(LIMIT, until=t0 + 1800), done(work)]
    agents["antigravity"].script = [result(LIMIT, until=t0 + 3600)]
    assert go() == 0
    assert 1800 <= sum(world.sleeps) < 1810
    assert max(world.sleeps) <= 5
    assert len(agents["codex"].prompts) == 2 and len(agents["antigravity"].prompts) == 1
    waits = [e for e in store.events() if e["kind"] == "wait"]
    assert len(waits) == 1 and waits[0]["until"] == t0 + 1800


def test_returning_agent_does_not_preempt_in_the_loop(go, agents, work, world):
    t0 = world.now
    agents["codex"].script = [result(LIMIT, until=t0 + 10)]
    agents["antigravity"].script = [
        lambda a, p, c: (setattr(world, "now", t0 + 500), RoundResult(COMPLETED))[1],
        done(work),
    ]
    assert go() == 0
    assert len(agents["codex"].prompts) == 1 and len(agents["antigravity"].prompts) == 2


def test_loop_exception_is_survived(go, agents, work, store, world, monkeypatch):
    real = runner.run_gate
    calls = []

    def flaky(goal, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return real(goal, **kw)

    monkeypatch.setattr(runner, "run_gate", flaky)
    agents["codex"].script = [done(work)]
    assert go() == 0
    errors = [e for e in store.events() if e["kind"] == "error"]
    assert len(errors) == 1 and "boom" in errors[0]["error"]
    assert sum(world.sleeps) == 60 and max(world.sleeps) <= 5


def test_adapter_exception_becomes_a_transient_round(go, agents, work, store, world):
    def boom(a, p, c):
        raise RuntimeError("adapter bug")

    agents["codex"].script = [boom]
    agents["antigravity"].script = [done(work)]
    assert go() == 0
    end = [e for e in store.events() if e["kind"] == "round_end"][0]
    assert end["outcome"] == TRANSIENT
    assert len(agents["codex"].prompts) == 1  # backed off, the other took over


class FlakyStore(Store):
    fail_loads = 0

    def load_state(self):
        if self.fail_loads > 0:
            self.fail_loads -= 1
            raise StateError("unreadable")
        return super().load_state()


def test_state_error_is_survived_and_cooldowns_are_kept(tmp_path, work, agents, world):
    flaky = FlakyStore(tmp_path / "flaky")

    def limit_then_break_store(agent, prompt, ctx):
        flaky.fail_loads = 3  # record's save, the next iteration's saves
        return RoundResult(LIMIT, until=world.now + 3600)

    agents["codex"].script = [limit_then_break_store]
    agents["antigravity"].script = [done(work)]
    assert run_goal(make_goal(tmp_path, work), agents, flaky, world.clock, world.sleep) == 0
    assert any(e["kind"] == "error" for e in flaky.events())
    assert sum(world.sleeps) >= 60
    assert len(agents["codex"].prompts) == 1  # its cooldown was not wiped by the failure
    assert len(agents["antigravity"].prompts) == 1


def test_corrupt_state_at_start_waits_and_never_rewrites(tmp_path, work, agents, world, store):
    (store.root / "state.json").write_text("{broken")
    ticks = []

    def sleep(seconds):
        world.sleep(seconds)
        ticks.append(seconds)
        if sum(ticks) >= 120:
            (store.root / "state.json").unlink()

    agents["codex"].script = [done(work)]
    assert run_goal(make_goal(tmp_path, work), agents, store, world.clock, sleep) == 0
    assert sum(1 for e in store.events() if e["kind"] == "error") >= 2


def test_stop_soon_exits_zero_with_goal_unfinished(go, agents, work, store):
    agents["codex"].script = [lambda a, p, c: (store.request_stop(), RoundResult(COMPLETED))[1]]
    assert go() == 0
    assert len(agents["codex"].prompts) == 1  # the round was allowed to end
    assert kinds(store)[-1] == "stopped" and "finished" not in kinds(store)
    assert store.stop_request() == ""


def test_stop_before_start_runs_nothing(go, agents, store):
    store.request_stop()
    assert go() == 0
    assert agents["codex"].prompts == []


def test_stop_now_kills_the_round_and_does_not_finish_a_goal_without_verify(
    tmp_path, work, store, agents, go
):
    def step(agent, prompt, ctx):
        store.request_stop(now=True)
        assert ctx.should_kill() is True
        return RoundResult(COMPLETED, reason="stop requested")

    agents["codex"].script = [step]
    assert go(make_goal(tmp_path, work, verify=None)) == 0
    assert "finished" not in kinds(store) and kinds(store)[-1] == "stopped"


def test_stop_during_wait_wakes_early(go, agents, store, world):
    agents["codex"].script = [result(LIMIT, until=world.now + 7200)]
    agents["antigravity"].script = [result(LIMIT, until=world.now + 7200)]

    def sleep(seconds):
        world.sleep(seconds)
        if sum(world.sleeps) >= 20:
            store.request_stop()

    assert go(sleep=sleep) == 0
    assert 20 <= sum(world.sleeps) <= 30


# no verify command


def test_without_verify_a_completed_round_finishes(tmp_path, work, go, agents, store):
    assert go(make_goal(tmp_path, work, verify=None)) == 0
    assert len(agents["codex"].prompts) == 1
    assert kinds(store)[-1] == "finished" and "gate" not in kinds(store)


def test_without_verify_a_timeout_round_does_not_finish(tmp_path, work, go, agents, store):
    agents["codex"].script = [result(TIMEOUT), result(TIMEOUT), result(COMPLETED)]
    assert go(make_goal(tmp_path, work, verify=None)) == 0
    assert len(agents["codex"].prompts) == 3


def test_without_verify_a_limit_round_does_not_finish(tmp_path, work, go, agents, world):
    agents["codex"].script = [result(LIMIT, until=world.now + 3600)]
    assert go(make_goal(tmp_path, work, verify=None)) == 0
    assert len(agents["codex"].prompts) == 1 and len(agents["antigravity"].prompts) == 1


# prompts


def test_notes_reach_next_prompt_once(go, agents, work, store):
    store.say("first note")
    agents["codex"].script = [
        lambda a, p, c: (store.say("second note"), RoundResult(COMPLETED))[1],
        result(COMPLETED),
        done(work),
    ]
    assert go() == 0
    p1, p2, p3 = agents["codex"].prompts
    assert "first note" in p1 and "second note" not in p1
    assert "second note" in p2 and "first note" not in p2
    assert "note" not in p3.replace("Notes from", "")


def test_prompt_contains_objective_gate_tail_handoff_and_rules(tmp_path, work, go, agents, store):
    goal = make_goal(tmp_path, work, verify="echo GATE-OUTPUT-LINE; test -f DONE")
    (work / ".baton").mkdir()
    (work / ".baton" / "HANDOFF.md").write_text("tried X, next try Y")
    store.say("please prefer small commits")
    agents["codex"].script = [done(work)]
    assert go(goal) == 0
    prompt = agents["codex"].prompts[0]
    for needle in (
        OBJECTIVE,
        "echo GATE-OUTPUT-LINE; test -f DONE",
        "GATE-OUTPUT-LINE",
        "tried X, next try Y",
        "please prefer small commits",
        "one shift of a relay",
        "Do not declare success",
        ".baton/HANDOFF.md",
    ):
        assert needle in prompt, needle


def test_previous_round_tail_reaches_next_agent(go, agents, work, world):
    agents["codex"].script = [
        say(["# hidden", "I was halfway through the parser"], result(LIMIT, until=world.now + 3600))
    ]
    agents["antigravity"].script = [done(work)]
    assert go() == 0
    prompt = agents["antigravity"].prompts[0]
    assert "I was halfway through the parser" in prompt
    assert "# hidden" not in prompt  # only rendered lines


def test_resumed_prompt_omits_objective_rules_keeps_changes(go, agents, work, store):
    def first(agent, prompt, ctx):
        ctx.on_session("sess-1")
        ctx.on_output("progress line from round one")
        (work / ".baton").mkdir(exist_ok=True)
        (work / ".baton" / "HANDOFF.md").write_text("handoff after round one")
        return RoundResult(COMPLETED)

    agents["codex"].script = [first, done(work)]
    assert go() == 0
    assert agents["codex"].sessions == [None, "sess-1"]
    first_prompt, second = agents["codex"].prompts
    assert OBJECTIVE in first_prompt and "one shift of a relay" in first_prompt
    assert OBJECTIVE not in second and "one shift of a relay" not in second
    assert "progress line from round one" in second
    assert "handoff after round one" in second
    assert "exit code 1" in second  # the gate output


def test_prompt_tails_are_bounded(tmp_path, work):
    goal = make_goal(tmp_path, work)
    prompt = build_prompt(goal, "g" * 9000, "", "", "r" * 20000, resumed=False)
    assert prompt.count("g") <= 4000 + 50 and prompt.count("r") <= 8000 + 50
    # the tails keep their end, which is what the next agent needs
    assert build_prompt(goal, "a" * 5000 + "END", "", "", "", resumed=False).count("END") == 1


def test_resume_failure_reruns_fresh_and_clears_id(tmp_path, work, go, agents, store, world):
    store.save_state({"workdir": str(work), "sessions": {"codex": "stale"}, "round": 4})
    store.say("note that must not be lost")

    def failing_resume(agent, prompt, ctx):
        return RoundResult(TRANSIENT, reason="resume failed: gone", resume_failed=True)

    agents["codex"].script = [failing_resume, done(work)]
    assert go() == 0
    assert agents["codex"].sessions == ["stale", None]
    resumed, fresh = agents["codex"].prompts
    assert OBJECTIVE not in resumed and OBJECTIVE in fresh
    assert "note that must not be lost" in resumed and "note that must not be lost" in fresh
    assert "codex" not in store.load_state()["sessions"]
    assert world.sleeps == []  # not a cooldown
    assert store.load_state()["round"] == 5


# interrupts


def interrupting(store, note):
    def step(agent, prompt, ctx):
        ctx.on_session("sess-i")
        store.say(note)
        store.request_interrupt()
        for _ in range(100):
            if ctx.should_kill():
                return RoundResult(COMPLETED, reason="stop requested")
        raise AssertionError("interrupt never reached the round")

    return step


def test_interrupt_ends_round_and_note_reaches_same_agent(go, agents, work, store):
    agents["codex"].script = [interrupting(store, "change of plan: use sqlite"), done(work)]
    assert go() == 0
    assert len(agents["antigravity"].prompts) == 0
    assert agents["codex"].sessions == [None, "sess-i"]
    assert "change of plan: use sqlite" in agents["codex"].prompts[1]
    assert store.take_interrupt() is False  # consumed
    first_end = [e for e in store.events() if e["kind"] == "round_end"][0]
    assert first_end["interrupted"] is True


def test_interrupted_round_does_not_finish_a_goal_without_verify(tmp_path, work, go, agents, store):
    agents["codex"].script = [interrupting(store, "redo it"), result(COMPLETED)]
    assert go(make_goal(tmp_path, work, verify=None)) == 0
    assert len(agents["codex"].prompts) == 2
    assert "redo it" in agents["codex"].prompts[1]


def test_interrupt_requested_between_rounds_does_not_kill_the_next_one(go, agents, work, store):
    store.request_interrupt()

    def check(agent, prompt, ctx):
        assert ctx.should_kill() is False
        return RoundResult(COMPLETED)

    agents["codex"].script = [check, done(work)]
    assert go() == 0


# the interrupt waits for a tool


class Watching(Fake):
    """A Fake whose stream shows tools: the line `start` opens one, `end` closes it."""

    def tool_watch(self):
        class Watch(ToolWatch):
            def feed(self, line):
                if line == "boom":
                    raise RuntimeError("cannot read this")
                if line == "start":
                    self.open.add("t")
                elif line == "end":
                    self.open.discard("t")

        return Watch()


def kills(ctx, times=5):
    return [ctx.should_kill() for _ in range(times)]


def test_an_interrupt_waits_for_the_running_tool_to_end(go, agents, work, store):
    agents["codex"] = Watching("codex")
    seen = {}

    def first(agent, prompt, ctx):
        ctx.on_session("sess-w")
        ctx.on_output("start")
        store.say("use sqlite")
        store.request_interrupt()
        seen["during"] = kills(ctx)
        ctx.on_output("end")
        seen["after"] = ctx.should_kill()
        return RoundResult(COMPLETED)

    agents["codex"].script = [first, done(work)]
    assert go() == 0
    assert seen["during"] == [False] * 5 and seen["after"] is True
    assert agents["codex"].sessions == [None, "sess-w"]
    assert "use sqlite" in agents["codex"].prompts[1]
    ends = [e for e in store.events() if e["kind"] == "round_end"]
    assert ends[0]["interrupted"] is True


def test_an_interrupt_with_no_tool_running_ends_the_round_at_once(go, agents, work, store):
    agents["codex"] = Watching("codex")
    seen = {}

    def first(agent, prompt, ctx):
        ctx.on_output("start")
        ctx.on_output("end")
        store.request_interrupt()
        seen["now"] = ctx.should_kill()
        return RoundResult(COMPLETED)

    agents["codex"].script = [first, done(work)]
    assert go() == 0
    assert seen["now"] is True


def test_an_interrupt_waits_for_a_tool_at_most_message_wait_s(go, agents, work, store, world):
    agents["codex"] = Watching("codex")
    seen = {}

    def first(agent, prompt, ctx):
        ctx.on_output("start")
        store.request_interrupt()
        seen["early"] = ctx.should_kill()  # the wait begins here
        world.now += runner.MESSAGE_WAIT_S - 1
        seen["almost"] = ctx.should_kill()
        world.now += 1
        seen["capped"] = ctx.should_kill()  # the tool is still running
        return RoundResult(COMPLETED)

    agents["codex"].script = [first, done(work)]
    assert go() == 0
    assert (seen["early"], seen["almost"], seen["capped"]) == (False, False, True)


def test_stop_now_ends_the_round_even_in_the_middle_of_a_tool(go, agents, work, store):
    agents["codex"] = Watching("codex")
    seen = {}

    def first(agent, prompt, ctx):
        ctx.on_output("start")
        store.request_stop(now=True)
        seen["killed"] = ctx.should_kill()
        return RoundResult(COMPLETED)

    agents["codex"].script = [first]
    assert go() == 0
    assert seen["killed"] is True


def test_a_watch_that_fails_is_dropped_and_the_interrupt_ends_the_round(go, agents, work, store):
    agents["codex"] = Watching("codex")
    seen = {}

    def first(agent, prompt, ctx):
        ctx.on_output("start")
        ctx.on_output("boom")  # the watch raises: this round is no longer watched
        store.request_interrupt()
        seen["killed"] = ctx.should_kill()
        return RoundResult(COMPLETED)

    agents["codex"].script = [first, done(work)]
    assert go() == 0
    assert seen["killed"] is True


def test_an_interrupt_still_waiting_when_the_agent_ends_by_itself_leaves_the_note(
    go, agents, work, store
):
    agents["codex"] = Watching("codex")

    def first(agent, prompt, ctx):
        ctx.on_output("start")
        store.say("read this one")
        store.request_interrupt()
        ctx.should_kill()
        return RoundResult(COMPLETED)  # it finished on its own

    agents["codex"].script = [first, done(work)]
    assert go() == 0
    assert "read this one" in agents["codex"].prompts[1]


# restart


def test_session_ids_survive_restart(tmp_path, work, agents, world, store):
    goal = make_goal(tmp_path, work)

    def first(agent, prompt, ctx):
        ctx.on_session("sess-keep")
        store.request_stop()
        return RoundResult(COMPLETED)

    agents["codex"].script = [first]
    assert run_goal(goal, agents, store, world.clock, world.sleep) == 0
    again = {"codex": Fake("codex"), "antigravity": Fake("antigravity")}
    again["codex"].script = [done(work)]
    assert run_goal(goal, again, Store(store.root), world.clock, world.sleep) == 0
    assert again["codex"].sessions == ["sess-keep"]
    assert store.load_state()["round"] == 2


def test_cooldown_and_current_survive_restart(tmp_path, work, agents, world, store):
    goal = make_goal(tmp_path, work)
    agents["codex"].script = [result(LIMIT, until=world.now + 3600)]
    agents["antigravity"].script = [
        lambda a, p, c: (store.request_stop(), RoundResult(COMPLETED))[1]
    ]
    assert run_goal(goal, agents, store, world.clock, world.sleep) == 0
    again = {"codex": Fake("codex"), "antigravity": Fake("antigravity")}
    again["antigravity"].script = [done(work)]
    assert run_goal(goal, again, Store(store.root), world.clock, world.sleep) == 0
    assert again["codex"].prompts == []


def test_workdir_change_clears_ids(tmp_path, work, go, agents, store):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    store.save_state({"workdir": str(elsewhere), "sessions": {"codex": "old"}})
    agents["codex"].script = [done(work)]
    assert go() == 0
    assert agents["codex"].sessions == [None]
    assert OBJECTIVE in agents["codex"].prompts[0]


def test_a_second_runner_is_refused(go, store):
    with store.lock(), pytest.raises(AlreadyRunning):
        go()


def test_two_goals_in_one_directory_are_refused(tmp_path, work, agents, world):
    goal = make_goal(tmp_path, work)
    with workdir_lock(work, "a-session"), pytest.raises(WorkdirBusy, match="a-session"):
        run_goal(goal, agents, Store(tmp_path / "other"), world.clock, world.sleep)
    assert agents["codex"].prompts == []


def test_a_goal_inside_the_directory_of_a_running_one_is_refused(tmp_path, work, agents, world):
    inner = work / "pkg"
    inner.mkdir()
    goal = make_goal(tmp_path, inner)
    with workdir_lock(work, "a-session"), pytest.raises(WorkdirBusy):
        run_goal(goal, agents, Store(tmp_path / "other"), world.clock, world.sleep)


def test_a_goal_around_the_directory_of_a_running_one_is_refused(tmp_path, work, agents, world):
    goal = make_goal(tmp_path, work)
    with workdir_lock(work / "pkg", "a-session"), pytest.raises(WorkdirBusy):
        run_goal(goal, agents, Store(tmp_path / "other"), world.clock, world.sleep)


def test_the_directory_is_claimed_for_the_whole_run(go, agents, work):
    seen = []

    def peek(agent, prompt, ctx):
        seen.append(workdir_conflict(work))
        (work / "DONE").write_text("x")
        return RoundResult(COMPLETED)

    agents["codex"].script = [peek]
    assert go() == 0
    assert "demo" in seen[0]
    assert workdir_conflict(work) is None


def test_the_gate_gets_no_api_keys(tmp_path, work, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-reach-the-gate")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-the-gate")
    monkeypatch.setenv("KEPT_VARIABLE", "kept")
    show = "a=${ANTHROPIC_API_KEY-unset} o=${OPENAI_API_KEY-unset} k=${KEPT_VARIABLE-unset}"
    passed, tail = run_gate(make_goal(tmp_path, work, verify=f"echo {show}"))
    assert passed
    assert "a=unset o=unset k=kept" in tail


def test_stray_process_group_is_killed_on_start(go, agents, work, store):
    stray = subprocess.Popen(["sleep", "60"], start_new_session=True)
    # In real life the stray is nobody's child; reap it here so it does not linger as a zombie.
    threading.Thread(target=stray.wait, daemon=True).start()
    try:
        store.save_state(
            {"group": stray.pid, "group_start": process_start(stray.pid), "workdir": str(work)}
        )
        (work / "DONE").write_text("x")
        assert go() == 0
        assert stray.wait(timeout=15) is not None
        assert store.load_state()["group"] is None
    finally:
        if stray.poll() is None:
            stray.kill()


def _stray(work, store, **state):
    stray = subprocess.Popen(["sleep", "60"], start_new_session=True)
    threading.Thread(target=stray.wait, daemon=True).start()
    store.save_state({"group": stray.pid, "workdir": str(work), **state})
    return stray


def test_a_live_group_that_cannot_be_verified_is_neither_killed_nor_forgotten(
    go, agents, work, store
):
    # No start time was recorded (ps failed at spawn, or old state): unknown means stop.
    stray = _stray(work, store)
    try:
        store.request_stop()  # so the retry loop ends after the first refusal
        assert go() == 0
        assert stray.poll() is None, "an unverified group was killed"
        assert store.load_state()["group"] == stray.pid, "the record was forgotten"
        assert agents["codex"].prompts == [] and agents["antigravity"].prompts == []
        kinds = [e for e in store.events() if e["kind"] == "alert"]
        assert any("could not be verified" in e["message"] for e in kinds)
    finally:
        stray.kill()


def test_an_unverifiable_group_blocks_every_attempt_until_it_is_gone(go, agents, work, store):
    stray = _stray(work, store)
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            stray.kill()  # it dies; the record is then dead and cleared
            stray.wait()

    try:
        (work / "DONE").write_text("x")
        assert go(sleep=sleep) == 0
        assert len(sleeps) >= 3 and sleeps[0] > 0
        assert store.load_state()["group"] is None
    finally:
        if stray.poll() is None:
            stray.kill()


def test_a_recycled_group_number_is_forgotten_without_killing(go, agents, work, store):
    stray = _stray(work, store, group_start="Mon Jan  1 00:00:00 2001")
    try:
        (work / "DONE").write_text("x")
        assert go() == 0
        assert stray.poll() is None
        assert store.load_state()["group"] is None
        assert any("another process" in e.get("message", "") for e in store.events())
    finally:
        stray.kill()


def test_group_is_recorded_while_running_and_cleared_after(go, agents, work, store):
    seen = []

    def step(agent, prompt, ctx):
        ctx.on_spawn(424242)
        seen.append(store.load_state()["group"])
        return done(work)(agent, prompt, ctx)

    agents["codex"].script = [step]
    assert go() == 0
    assert seen == [424242] and store.load_state()["group"] is None


# outcomes


def test_credit_guard_and_auth_raise_alerts(go, agents, work, store, world):
    agents["codex"].script = [result(CREDIT_GUARD, reason="balance moved")]
    agents["antigravity"].script = [result(AUTH, reason="signed out"), done(work)]
    assert go() == 0
    alerts = [e for e in store.events() if e["kind"] == "alert"]
    assert [a["outcome"] for a in alerts] == [CREDIT_GUARD, AUTH]
    assert sum(world.sleeps) >= 1800  # both cooled down 30 min, the earliest continued


def test_output_events_only_for_rendered_lines(go, agents, work, store):
    agents["codex"].script = [say(["# hidden", "visible"], done(work))]
    assert go() == 0
    outs = [e for e in store.events() if e["kind"] == "output"]
    assert [e["text"] for e in outs] == ["visible"]


class Streaming(Fake):
    """An adapter that streams fragments: a line starting with `~` is one, the rest are whole."""

    def render(self, line):
        return line.lstrip("~") or None

    def is_fragment(self, line):
        return line.startswith("~")


def shown(store):
    """What a person reads: the output events joined the way the gateway joins them."""
    return "".join(
        gateway.render_event("output", e) for e in store.events() if e["kind"] == "output"
    )


def streaming_run(go, store, work, steps):
    agents = {"codex": Streaming("codex"), "antigravity": Streaming("antigravity")}
    agents["codex"].script = steps
    agents["antigravity"].script = [done(work)]
    assert go(adapters=agents) == 0
    return agents


def test_fragments_are_joined_into_one_line(go, work, store):
    streaming_run(go, store, work, [say(["~rende", "~rização\n"], done(work))])
    outs = [e for e in store.events() if e["kind"] == "output"]
    assert [(e["text"], e.get("fragment")) for e in outs] == [
        ("rende", True),
        ("rização\n", True),
    ]
    assert shown(store) == "renderização\n"


def test_a_whole_line_after_a_fragment_closes_the_line_first(go, work, store):
    streaming_run(go, store, work, [say(["~abc", "$ ls"], done(work))])
    outs = [e for e in store.events() if e["kind"] == "output"]
    assert [(e["text"], e.get("fragment")) for e in outs] == [
        ("abc", True),
        ("\n", True),
        ("$ ls", None),
    ]
    assert shown(store) == "abc\n$ ls\n"


def test_a_round_ending_on_a_fragment_closes_the_line_before_round_end(go, work, store):
    streaming_run(go, store, work, [say(["~abc"], result(LIMIT, until=1_000_000.0 + 3600))])
    events = [e for e in store.events() if e["kind"] in ("output", "round_end")]
    assert [e["kind"] for e in events[:3]] == ["output", "output", "round_end"]
    assert events[1]["text"] == "\n" and events[1]["fragment"] is True
    assert shown(store) == "abc\n"


def test_a_round_ending_on_a_complete_fragment_adds_no_newline(go, work, store):
    streaming_run(go, store, work, [say(["~abc\n"], done(work))])
    assert shown(store) == "abc\n"


def test_an_adapter_that_raises_after_a_fragment_still_closes_the_line(go, work, store):
    def boom(agent, prompt, ctx):
        ctx.on_output("~abc")
        raise RuntimeError("adapter bug")

    streaming_run(go, store, work, [boom])
    events = [e for e in store.events() if e["kind"] in ("output", "error")]
    assert [e["kind"] for e in events] == ["output", "output", "error"]
    assert shown(store).startswith("abc\n")


def test_round_tail_holds_the_joined_text(go, work, store):
    def last(agent, prompt, ctx):
        ctx.on_output("~half a sent")
        ctx.on_output("~ence")
        return RoundResult(LIMIT, until=1_000_000.0 + 3600)

    agents = streaming_run(go, store, work, [last])
    prompt = agents["antigravity"].prompts[0]
    assert "half a sentence\n" in prompt
    assert "half a sent\n" not in prompt


def test_a_failed_resume_starts_the_fresh_attempt_on_a_clean_line(go, work, store):
    store.save_state({"workdir": str(work), "sessions": {"codex": "stale"}, "round": 4})

    def failing_resume(agent, prompt, ctx):
        ctx.on_output("~cut o")
        return RoundResult(TRANSIENT, reason="resume failed: gone", resume_failed=True)

    steps = [failing_resume, say(["~fresh\n"], done(work))]
    streaming_run(go, store, work, steps)
    assert shown(store) == "cut o\nfresh\n"


def test_an_adapter_without_is_fragment_gets_the_events_it_always_did(go, agents, work, store):
    agents["codex"].script = [say(["one", "two"], done(work))]
    assert go() == 0
    outs = [e for e in store.events() if e["kind"] == "output"]
    assert outs == [e for e in outs if "fragment" not in e]
    assert [e["text"] for e in outs] == ["one", "two"]


def test_an_is_fragment_that_raises_gives_no_output(go, work, store):
    class Broken(Streaming):
        def is_fragment(self, line):
            raise ValueError("bad")

    agents = {"codex": Broken("codex"), "antigravity": Streaming("antigravity")}
    agents["codex"].script = [say(["~abc"], done(work))]
    agents["antigravity"].script = [done(work)]
    assert go(adapters=agents) == 0
    assert [e for e in store.events() if e["kind"] == "output"] == []


def test_state_json_carries_rotation_and_runner_state(go, agents, work, store, world):
    agents["codex"].script = [result(LIMIT, until=world.now + 3600)]
    agents["antigravity"].script = [done(work)]
    assert go() == 0
    state = store.load_state()
    assert state["current"] == "antigravity" and state["round"] == 2
    assert state["agents"]["codex"]["until"] > 0 and state["workdir"] == str(work)


# workdir


def test_baton_dir_created_and_excluded(go, agents, work):
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    (work / "DONE").write_text("x")
    assert go() == 0
    assert (work / ".baton").is_dir()
    exclude = (work / ".git" / "info" / "exclude").read_text()
    assert exclude.splitlines().count(".baton/") == 1
    assert go() == 0
    assert (work / ".git" / "info" / "exclude").read_text().splitlines().count(".baton/") == 1


def test_non_git_workdir_is_fine(go, work):
    (work / "DONE").write_text("x")
    assert go() == 0
    assert (work / ".baton").is_dir()


# gate


def test_gate_passes_and_reports_output(tmp_path, work):
    ok, tail = run_gate(make_goal(tmp_path, work, verify="echo hello; exit 0"))
    assert ok and "hello" in tail
    ok, tail = run_gate(make_goal(tmp_path, work, verify="echo nope >&2; exit 3"))
    assert not ok and "nope" in tail and "exit code 3" in tail


def test_missing_command_is_not_passed(tmp_path, work):
    ok, tail = run_gate(make_goal(tmp_path, work, verify=None))
    assert ok is False and tail
    ok, tail = run_gate(make_goal(tmp_path, work, verify="definitely-not-a-command-xyz"))
    assert ok is False


def test_gate_that_cannot_run_is_not_passed(tmp_path, work):
    goal = make_goal(tmp_path, work, verify="true")
    gone = type(goal)(**{**goal.__dict__, "workdir": tmp_path / "missing"})
    ok, tail = run_gate(gone)
    assert ok is False and "could not run" in tail


def test_gate_timeout_is_not_passed(tmp_path, work):
    goal = make_goal(
        tmp_path, work, verify="sleep 30 & echo started; wait", verify_extra="timeout_s = 1"
    )
    began = time.monotonic()
    ok, tail = run_gate(goal)
    assert ok is False and "timed out" in tail and "started" in tail
    assert time.monotonic() - began < 10


def test_gate_output_is_a_tail(tmp_path, work):
    goal = make_goal(tmp_path, work, verify="yes line | head -c 200000; exit 1")
    ok, tail = run_gate(goal)
    assert ok is False and len(tail) <= 4100


# snapshots


@pytest.fixture
def repo(tmp_path, work, monkeypatch):
    for key, value in (("NAME", "Baton Tests"), ("EMAIL", "baton-tests@baton.invalid")):
        monkeypatch.setenv(f"GIT_AUTHOR_{key}", value)
        monkeypatch.setenv(f"GIT_COMMITTER_{key}", value)
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    (work / "a.txt").write_text("1")
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-q", "-m", "init"], check=True)
    return work


def git_log(repo):
    return subprocess.run(
        ["git", "-C", str(repo), "log", "--format=%s"], capture_output=True, text=True
    ).stdout.splitlines()


def test_snapshot_commits_dirty_repo(tmp_path, repo):
    goal = make_goal(tmp_path, repo, snapshot=True)
    (repo / "a.txt").write_text("2")
    (repo / "new.txt").write_text("n")
    assert snapshot_work(goal, 7, "codex") is True
    assert git_log(repo)[0] == "baton: snapshot round 7 (codex)"
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True
    )
    assert status.stdout == ""


def test_snapshot_noop_when_clean(tmp_path, repo):
    goal = make_goal(tmp_path, repo, snapshot=True)
    assert snapshot_work(goal, 1, "codex") is False
    assert git_log(repo) == ["init"]


def test_snapshot_skips_hooks(tmp_path, repo):
    goal = make_goal(tmp_path, repo, snapshot=True)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    (repo / "a.txt").write_text("3")
    assert snapshot_work(goal, 2, "codex") is True


def test_snapshot_off_does_nothing(tmp_path, repo):
    goal = make_goal(tmp_path, repo)  # snapshot = false
    (repo / "a.txt").write_text("2")
    assert snapshot_work(goal, 1, "codex") is False and git_log(repo) == ["init"]


def test_non_git_workdir_snapshot_is_fine(tmp_path, work):
    goal = make_goal(tmp_path, work, snapshot=True)
    (work / "x").write_text("1")
    assert snapshot_work(goal, 1, "codex") is False


def test_snapshot_in_a_subdirectory_commits_only_that_directory(tmp_path, repo):
    inner = repo / "sub"
    inner.mkdir()
    (inner / "kept.txt").write_text("1")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "sub"], check=True)
    goal = make_goal(tmp_path, inner, snapshot=True)
    (inner / "kept.txt").write_text("changed inside")
    (repo / "a.txt").write_text("changed outside")
    assert snapshot_work(goal, 3, "codex") is True
    assert git_log(repo)[0] == "baton: snapshot round 3 (codex)"
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True
    ).stdout
    assert status.strip() == "M a.txt"  # the file outside the workdir is left alone


def test_snapshot_never_raises(tmp_path, repo, monkeypatch):
    goal = make_goal(tmp_path, repo, snapshot=True)
    (repo / "a.txt").write_text("2")

    def boom(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(runner, "_git", boom)
    assert snapshot_work(goal, 1, "codex") is False


def test_baton_dir_excluded(tmp_path, repo):
    goal = make_goal(tmp_path, repo, snapshot=True)
    (repo / ".baton").mkdir()
    (repo / ".baton" / "HANDOFF.md").write_text("notes")
    assert snapshot_work(goal, 1, "codex") is False  # only .baton changed
    (repo / "a.txt").write_text("2")
    assert snapshot_work(goal, 2, "codex") is True
    tracked = subprocess.run(
        ["git", "-C", str(repo), "ls-files"], capture_output=True, text=True
    ).stdout.split()
    assert ".baton/HANDOFF.md" not in tracked and "a.txt" in tracked


def test_loop_snapshots_after_each_round(tmp_path, repo, agents, store, world):
    goal = make_goal(tmp_path, repo, snapshot=True)

    def edit(agent, prompt, ctx):
        (repo / "a.txt").write_text("edited")
        return RoundResult(COMPLETED)

    agents["codex"].script = [edit, done(repo)]
    assert run_goal(goal, agents, store, world.clock, world.sleep) == 0
    log = git_log(repo)
    assert "baton: snapshot round 1 (codex)" in log and "baton: snapshot round 2 (codex)" in log
    ends = [e for e in store.events() if e["kind"] == "round_end"]
    assert [e["snapshot"] for e in ends] == [True, True]


# fix round 1


@pytest.mark.timeout(15)
def test_stop_with_a_failing_start_exits_instead_of_spinning(go, store, monkeypatch):
    def unreadable(*args):
        raise StateError("unreadable")

    monkeypatch.setattr(store, "load_state", unreadable)
    store.request_stop()
    assert go() == 0
    assert kinds(store).count("error") <= 2
    assert kinds(store)[-1] == "stopped"


@pytest.mark.timeout(15)
@pytest.mark.parametrize("clock_moves", [True, False])
def test_failing_start_sleeps_before_every_retry(go, store, world, monkeypatch, clock_moves):
    def unreadable(*args):
        raise StateError("unreadable")

    monkeypatch.setattr(store, "load_state", unreadable)
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        if clock_moves:
            world.sleep(seconds)
        if sum(slept) >= 300:
            store.request_stop()

    assert go(sleep=sleep) == 0
    errors = kinds(store).count("error")
    assert errors >= 4 and sum(slept) >= 60 * (errors - 1)


def test_refused_start_keeps_notes_and_the_previous_tail(go, agents, work, store):
    def first(agent, prompt, ctx):
        ctx.on_output("tail from the last real round")
        store.say("keep me")
        return RoundResult(COMPLETED)

    agents["codex"].script = [first, refused(TRANSIENT, reason="start refused")]
    agents["antigravity"].script = [done(work)]
    assert go() == 0
    assert "keep me" in agents["codex"].prompts[1]
    delivered = agents["antigravity"].prompts[0]
    assert "keep me" in delivered  # the refused start did not swallow it
    assert "tail from the last real round" in delivered
    assert store.read_inbox() == ""


def test_delivered_notes_are_consumed_once_started(go, agents, work, store):
    store.say("once")
    agents["codex"].script = [result(COMPLETED), done(work)]
    assert go() == 0
    assert "once" in agents["codex"].prompts[0] and "once" not in agents["codex"].prompts[1]


def dies_at_once(outcome=LIMIT, **kw):
    """A process that started and then died without reading anything, as with a 429."""
    return result(outcome, reason="429", **kw)


@pytest.mark.parametrize("outcome", [LIMIT, TRANSIENT, AUTH, CREDIT_GUARD])
def test_notes_survive_an_agent_that_dies_at_once(go, agents, work, store, world, outcome):
    store.say("do not lose me")
    agents["codex"].script = [dies_at_once(outcome, until=world.now + 3600)]
    agents["antigravity"].script = [done(work)]
    assert go() == 0
    assert "do not lose me" in agents["codex"].prompts[0]
    assert "do not lose me" in agents["antigravity"].prompts[0]
    assert store.read_inbox() == ""


def test_a_round_that_dies_at_once_leaves_the_previous_tail(go, agents, work, store, world):
    agents["codex"].script = [say(["tail of the real round"]), dies_at_once(until=world.now + 60)]
    agents["antigravity"].script = [done(work)]
    assert go() == 0
    assert "tail of the real round" in agents["antigravity"].prompts[0]


@pytest.mark.parametrize("outcome", [COMPLETED, TIMEOUT])
def test_notes_are_spent_by_a_round_that_ended_normally(go, agents, work, store, outcome):
    store.say("once")
    agents["codex"].script = [result(outcome), done(work)]
    assert go() == 0
    assert "once" in agents["codex"].prompts[0] and "once" not in agents["codex"].prompts[1]


def test_notes_are_spent_by_a_failed_round_that_produced_output(go, agents, work, store, world):
    store.say("once")
    agents["codex"].script = [
        say(["I read your note"], then=result(TRANSIENT, reason="crashed later"))
    ]
    agents["antigravity"].script = [done(work)]
    assert go() == 0
    assert "once" not in agents["antigravity"].prompts[0]


def test_an_error_line_is_not_output_that_spends_the_notes(go, agents, work, store, world):
    """Every adapter renders error text too, and that must not pass for the agent working."""
    codex = agents["codex"]
    codex.is_work = lambda line: not line.startswith("error:")
    store.say("do not lose me")
    codex.script = [say(["error: 429"], then=result(LIMIT, until=world.now + 3600))]
    agents["antigravity"].script = [done(work)]
    assert go() == 0
    assert "do not lose me" in agents["antigravity"].prompts[0]


def test_an_interrupted_round_that_said_nothing_keeps_its_notes(go, agents, work, store):
    def first(agent, prompt, ctx):
        store.say("the newer note")
        store.request_interrupt()
        ctx.should_kill()
        return RoundResult(COMPLETED, reason="stop requested")

    store.say("the first note")
    agents["codex"].script = [first, done(work)]
    assert go() == 0
    second = agents["codex"].prompts[1]
    assert "the first note" in second and "the newer note" in second


def test_notes_carried_by_a_round_are_in_state_json_while_it_runs(go, agents, work, store):
    seen = {}

    def peek(agent, prompt, ctx):
        seen.update(store.load_state())
        (work / "DONE").write_text("x")
        return RoundResult(COMPLETED)

    store.say("still mine")
    agents["codex"].script = [peek]
    assert go() == 0
    assert "still mine" in seen["notes"]
    assert store.load_state()["notes"] == ""


def test_notes_left_by_a_run_that_died_reach_the_next_run(go, agents, work, store):
    store.save_state({"workdir": str(work), "notes": "from the run that died\n\n"})
    agents["codex"].script = [done(work)]
    assert go() == 0
    assert "from the run that died" in agents["codex"].prompts[0]
    assert store.load_state()["notes"] == ""


# a round owed to a person's message


@pytest.fixture
def owed(tmp_path, work, store, agents, world):
    def run(verify="true"):
        return run_goal(
            make_goal(tmp_path, work, verify=verify),
            agents,
            store,
            clock=world.clock,
            sleep=world.sleep,
            owes_round=True,
        )

    return run


def test_a_passing_gate_does_not_swallow_a_message(owed, agents, store):
    agents["codex"].script = [result(COMPLETED)]
    assert owed() == 0
    assert len(agents["codex"].prompts) == 1
    finished = [e for e in store.events() if e["kind"] == "finished"]
    assert finished[-1]["round"] == 1


def test_without_a_message_a_passing_gate_still_finishes_at_round_0(go, agents, store, tmp_path):
    assert go(make_goal(tmp_path, tmp_path, verify="true")) == 0
    assert agents["codex"].prompts == []


def test_the_owed_round_is_only_paid_by_a_round_that_took_the_message(owed, agents, store, world):
    agents["codex"].script = [dies_at_once(until=world.now + 3600)]
    agents["antigravity"].script = [result(COMPLETED)]
    assert owed() == 0
    assert len(agents["codex"].prompts) == 1 and len(agents["antigravity"].prompts) == 1


def test_a_refused_start_does_not_pay_the_owed_round(owed, agents, world):
    agents["codex"].script = [refused(TRANSIENT, reason="no")]
    agents["antigravity"].script = [result(COMPLETED)]
    assert owed() == 0
    assert len(agents["antigravity"].prompts) == 1


def test_an_owed_round_survives_a_restart(go, agents, store, tmp_path, work):
    store.save_state({"workdir": str(work), "owes_round": True})
    agents["codex"].script = [result(COMPLETED)]
    assert go(make_goal(tmp_path, work, verify="true")) == 0
    assert len(agents["codex"].prompts) == 1
    assert store.load_state()["owes_round"] is False


def test_a_stop_wins_over_an_owed_round(owed, agents, store):
    store.request_stop()
    assert owed() == 0
    assert agents["codex"].prompts == []


# commits since the agent's last round


@pytest.fixture
def commit(repo):
    def make(name, message):
        (repo / name).write_text(message)
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", message], check=True)

    return make


def test_build_prompt_lists_commits_since(tmp_path, work):
    goal = make_goal(tmp_path, work)
    prompt = build_prompt(goal, "", "", "", "", True, since_commits="abc123 other agent's fix")
    assert "other agent's fix" in prompt and OBJECTIVE not in prompt
    assert "Commits" not in build_prompt(goal, "", "", "", "", True, since_commits="")


def test_resumed_prompt_includes_other_agents_commits(tmp_path, repo, commit, agents, store, world):
    goal = make_goal(tmp_path, repo)

    def codex_first(agent, prompt, ctx):
        ctx.on_session("s1")
        commit("c.txt", "codex own work")
        return RoundResult(LIMIT, until=world.now + 10)

    def agy(agent, prompt, ctx):
        commit("g.txt", "antigravity change")
        return RoundResult(LIMIT, until=world.now + 3600)

    agents["codex"].script = [codex_first, done(repo)]
    agents["antigravity"].script = [agy]
    assert run_goal(goal, agents, store, world.clock, world.sleep) == 0
    resumed = agents["codex"].prompts[1]
    assert agents["codex"].sessions == [None, "s1"]
    assert "antigravity change" in resumed
    assert "codex own work" not in resumed
    assert set(store.load_state()["heads"]) == {"codex", "antigravity"}


def test_non_git_workdir_has_no_commit_section(go, agents, work):
    agents["codex"].script = [
        say(["x"], lambda a, p, c: (c.on_session("s"), RoundResult(COMPLETED))[1])
    ]
    agents["codex"].script.append(done(work))
    assert go() == 0
    assert "Commits" not in agents["codex"].prompts[1]


def test_commits_since_is_capped(repo, commit, monkeypatch):
    base = runner._head(repo)
    for i in range(6):
        commit(f"f{i}.txt", f"commit number {i}")
    monkeypatch.setattr(runner, "COMMITS_MAX_CHARS", 60)
    text = runner.commits_since(repo, base)
    assert len(text) <= 60 + 10 and "commit number 5" in text and "commit number 0" not in text
    assert runner.commits_since(repo, "not-a-sha") == ""
    assert runner.commits_since(repo, "--output=x") == ""


# a round that exits at once


def test_instant_completed_round_waits_before_the_next(go, agents, work, world):
    agents["codex"].script = [result(COMPLETED), result(COMPLETED), done(work)]
    assert go() == 0
    assert sum(world.sleeps) == 60 and max(world.sleeps) <= 5  # 30 s after each


def test_slow_completed_round_does_not_wait(go, agents, work, world):
    def slow(agent, prompt, ctx):
        world.now += 31
        return RoundResult(COMPLETED)

    agents["codex"].script = [slow, done(work)]
    assert go() == 0
    assert world.sleeps == []


def test_pause_after_instant_round_honours_stop(go, agents, store, world):
    agents["codex"].script = [result(COMPLETED)]

    def sleep(seconds):
        world.sleep(seconds)
        store.request_stop()

    assert go(sleep=sleep) == 0
    assert len(agents["codex"].prompts) == 1 and sum(world.sleeps) <= 5


def test_interrupted_instant_round_does_not_pause(go, agents, work, store, world):
    agents["codex"].script = [interrupting(store, "now please"), done(work)]
    assert go() == 0
    assert world.sleeps == []


# minors


def test_snapshot_failure_cannot_lose_a_credit_guard_outcome(
    tmp_path, repo, agents, store, world, monkeypatch
):
    goal = make_goal(tmp_path, repo, snapshot=True)

    def boom(*args, **kwargs):
        raise RuntimeError("snapshot exploded")

    monkeypatch.setattr(runner, "snapshot_work", boom)
    agents["codex"].script = [result(CREDIT_GUARD, reason="balance moved")]
    agents["antigravity"].script = [done(repo)]
    assert run_goal(goal, agents, store, world.clock, world.sleep) == 0
    assert len(agents["codex"].prompts) == 1  # its cooldown was recorded before the failure
    assert any(e["kind"] == "alert" for e in store.events())


def test_stop_file_is_cleared_when_the_goal_finishes(go, agents, work, store, tmp_path):
    (work / "DONE").write_text("x")
    store.request_stop()
    assert go() == 0
    assert store.stop_request() == ""
    assert kinds(store)[-1] == "finished"


def test_stop_file_is_cleared_when_a_goal_without_verify_finishes(
    tmp_path, work, go, agents, store
):
    agents["codex"].script = [lambda a, p, c: (store.request_stop(), RoundResult(COMPLETED))[1]]
    assert go(make_goal(tmp_path, work, verify=None)) == 0
    assert kinds(store)[-1] == "finished" and store.stop_request() == ""


@pytest.mark.timeout(20)
def test_stop_now_kills_a_running_gate(tmp_path, work, go, agents, store):
    store.request_stop(now=True)
    began = time.monotonic()
    assert go(make_goal(tmp_path, work, verify="sleep 30")) == 0
    assert time.monotonic() - began < 10
    assert agents["codex"].prompts == [] and kinds(store)[-1] == "stopped"


@pytest.mark.timeout(20)
def test_run_gate_should_kill_stops_it(tmp_path, work):
    goal = make_goal(tmp_path, work, verify="echo started; sleep 30")
    ok, tail = run_gate(goal, should_kill=lambda: True)
    assert ok is False and "stopped" in tail


@pytest.mark.timeout(20)
def test_run_gate_kills_its_session_on_ctrl_c(tmp_path, work):
    pidfile = work / "pid"
    goal = make_goal(tmp_path, work, verify="echo $$ > pid; sleep 30")

    def interrupt():
        if pidfile.exists() and pidfile.read_text().strip():
            raise KeyboardInterrupt
        return False

    with pytest.raises(KeyboardInterrupt):
        run_gate(goal, should_kill=interrupt)
    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise AssertionError("the gate's process group survived")


# fix round 2


def test_refused_retry_after_a_failed_resume_keeps_notes_tail_and_heads(
    tmp_path, repo, commit, agents, store, world
):
    goal = make_goal(tmp_path, repo)
    old_head = runner._head(repo)
    store.save_state(
        {
            "workdir": str(repo),
            "sessions": {"codex": "stale"},
            "round_tail": "tail of the last real round",
            "heads": {"codex": old_head},
        }
    )
    store.say("do not lose me")
    commit("later.txt", "later commit")

    def failing_resume(agent, prompt, ctx):
        ctx.on_output("noise from the dead resume")
        return RoundResult(TRANSIENT, reason="resume failed: gone", resume_failed=True)

    agents["codex"].script = [failing_resume, refused(TRANSIENT, reason="start refused")]
    agents["antigravity"].script = [done(repo)]
    assert run_goal(goal, agents, store, world.clock, world.sleep) == 0
    handed_over = agents["antigravity"].prompts[0]
    assert "do not lose me" in handed_over
    assert "tail of the last real round" in handed_over
    assert "noise from the dead resume" not in handed_over
    assert agents["codex"].sessions == ["stale", None]
    assert "do not lose me" in agents["codex"].prompts[1]  # the fresh retry carried them
    assert store.load_state()["heads"]["codex"] == old_head  # nothing ran, nothing was seen


@pytest.mark.timeout(20)
def test_gate_background_children_do_not_survive_a_normal_exit(tmp_path, work):
    goal = make_goal(tmp_path, work, verify="sleep 30 & echo $! > bgpid; exit 0")
    ok, _ = run_gate(goal)
    assert ok is True
    pid = int((work / "bgpid").read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise AssertionError("the gate's background child survived")


def test_pause_wait_event_carries_seconds_and_agents(go, agents, work, store, world):
    agents["codex"].script = [result(COMPLETED), done(work)]
    assert go() == 0
    waits = [e for e in store.events() if e["kind"] == "wait"]
    assert len(waits) == 1
    assert waits[0]["seconds"] == 30 and set(waits[0]["agents"]) == {"codex", "antigravity"}


# notes outrank a passing gate


def test_a_passing_gate_waits_for_an_unread_note(go, agents, work, store):
    (work / "DONE").write_text("x")
    store.say("read me before you finish")
    assert go() == 0
    assert len(agents["codex"].prompts) == 1
    assert "read me before you finish" in agents["codex"].prompts[0]
    assert kinds(store).count("finished") == 1 and kinds(store)[-1] == "finished"


def test_a_note_carried_by_a_refused_start_still_holds_the_finish(go, agents, work, store, world):
    (work / "DONE").write_text("x")
    store.say("this must reach an agent")
    agents["codex"].script = [refused(LIMIT, until=world.now + 3600)]
    assert go() == 0
    assert len(agents["codex"].prompts) == 1  # the refused start
    assert len(agents["antigravity"].prompts) == 1
    assert "this must reach an agent" in agents["antigravity"].prompts[0]
    ends = [(e["kind"], e.get("agent")) for e in store.events() if e["kind"] != "gate"]
    assert ends.index(("round_end", "antigravity")) < ends.index(("finished", None))


def test_without_a_gate_a_note_arriving_in_the_last_round_gets_another(
    tmp_path, work, store, agents, world
):
    goal = make_goal(tmp_path, work, verify=None)
    agents["codex"].script = [
        say([], then=lambda a, p, c: (store.say("late note"), done(work)(a, p, c))[1])
    ]
    assert run_goal(goal, agents, store, clock=world.clock, sleep=world.sleep) == 0
    assert len(agents["codex"].prompts) == 2
    assert "late note" in agents["codex"].prompts[1]
    assert kinds(store).count("finished") == 1


def test_a_stop_wins_over_an_unread_note(go, agents, work, store):
    (work / "DONE").write_text("x")
    store.say("never read")
    store.request_stop()
    assert go() == 0
    assert agents["codex"].prompts == [] and agents["antigravity"].prompts == []
    assert kinds(store)[-1] == "stopped"
    assert note_is_kept(store, "never read")


def test_a_goal_file_run_consumes_its_stop(go, store):
    store.request_stop()
    go()
    assert store.stop_request() == ""


def test_the_objective_callable_briefs_each_fresh_round(tmp_path, work, store, agents, world):
    goal = make_goal(tmp_path, work)
    texts = iter(["first briefing", "second briefing"])
    agents["codex"].script = [say(["x"]), done(work)]
    run_goal(
        goal, agents, store, clock=world.clock, sleep=world.sleep, objective=lambda: next(texts)
    )
    assert "first briefing" in agents["codex"].prompts[0]
    assert "second briefing" in agents["codex"].prompts[1]
    assert OBJECTIVE not in agents["codex"].prompts[0]


# a note survives whatever ends the run


class Died(BaseException):
    """Stands for a kill -9: nothing after the point that raises it runs."""


def dies(seconds):
    raise Died()


def note_is_kept(store, text):
    inbox = store.root / "inbox.md"
    in_inbox = inbox.exists() and text in inbox.read_text()
    return in_inbox or text in str(store.load_state().get("notes", ""))


def sent(agents):
    return agents["codex"].prompts + agents["antigravity"].prompts


def test_a_note_a_stop_left_unread_reaches_the_next_run(go, agents, work, store):
    (work / "DONE").write_text("x")
    store.say("never read")
    store.request_stop()
    go()
    assert sent(agents) == []
    go()
    assert sum("never read" in p for p in sent(agents)) == 1


def test_a_kill_during_a_wait_keeps_the_note(go, agents, work, store, world):
    (work / "DONE").write_text("x")
    store.say("person said something")
    for agent in agents.values():
        agent.avail = Availability(False, until=world.now + 7200, reason="spent")
    with pytest.raises(Died):
        go(sleep=dies)
    assert note_is_kept(store, "person said something")


def test_a_goal_does_not_finish_past_a_note_saved_before_a_kill(go, agents, work, store, world):
    (work / "DONE").write_text("x")
    store.say("person said something")
    for agent in agents.values():
        agent.avail = Availability(False, until=world.now + 7200, reason="spent")
    with pytest.raises(Died):
        go(sleep=dies)
    for agent in agents.values():
        agent.avail = Availability(True)
    world.now += 8000
    go()
    assert any("person said something" in p for p in sent(agents))


def test_a_kill_between_taking_the_inbox_and_spawning_keeps_the_note(go, agents, store):
    store.say("note before the baseline read")

    def dies_before_spawn(prompt, ctx):  # the baseline limits read, 15-60 s with the real CLIs
        raise Died()

    agents["codex"].run_round = dies_before_spawn
    with pytest.raises(Died):
        go()
    assert note_is_kept(store, "note before the baseline read")


def dies_saving_notes(store, monkeypatch):
    """Make the state write that first holds a note die before it lands."""
    real = store.save_state

    def save_state(state):
        if state.get("notes"):
            raise Died()
        real(state)

    monkeypatch.setattr(store, "save_state", save_state)


def test_the_inbox_outlives_the_save_in_unread_notes(go, work, store, monkeypatch):
    (work / "DONE").write_text("x")
    store.say("the gate passed")
    dies_saving_notes(store, monkeypatch)
    with pytest.raises(Died):
        go()
    assert "the gate passed" in (store.root / "inbox.md").read_text()


def test_the_inbox_outlives_the_save_in_a_round(go, store, monkeypatch):
    store.say("before a round")
    dies_saving_notes(store, monkeypatch)
    with pytest.raises(Died):
        go()
    assert "before a round" in (store.root / "inbox.md").read_text()


def fails_saving_notes(store, monkeypatch, times):
    """Make the state writes that hold a note fail, `times` times."""
    real = store.save_state
    left = [times]

    def save_state(state):
        if state.get("notes") and left[0]:
            left[0] -= 1
            raise OSError("no space left on device")
        real(state)

    monkeypatch.setattr(store, "save_state", save_state)


def test_a_failed_save_leaves_the_note_unread_and_the_goal_open(
    go, agents, work, store, monkeypatch
):
    (work / "DONE").write_text("x")
    store.say("hold on")
    fails_saving_notes(store, monkeypatch, 1)
    go()
    assert sum("hold on" in p for p in sent(agents)) == 1
    assert kinds(store)[-1] == "finished"
    assert any("could not save state" in str(e.get("error")) for e in store.events())


def test_a_failed_save_before_a_round_keeps_the_note_for_a_later_one(
    go, agents, work, store, monkeypatch
):
    store.say("hold on")
    fails_saving_notes(store, monkeypatch, 1)
    agents["codex"].script = [say(["x"]), done(work)]
    go()
    assert "hold on" not in agents["codex"].prompts[0]
    assert sum("hold on" in p for p in agents["codex"].prompts) == 1
    assert not (store.root / "inbox.md").exists()
    assert any("could not save state" in str(e.get("error")) for e in store.events())


def dies_deleting_the_inbox(go, store, monkeypatch):
    """Run until a kill lands between the save and the delete."""

    def died(read):
        raise Died()

    with monkeypatch.context() as patch:
        patch.setattr(store, "clear_inbox", died)
        with pytest.raises(Died):
            go()


def test_a_kill_after_the_save_does_not_repeat_the_note(go, agents, work, store, monkeypatch):
    (work / "DONE").write_text("x")
    store.say("said once")
    dies_deleting_the_inbox(go, store, monkeypatch)
    state = store.load_state()
    assert state["collected_inbox"] == "said once\n\n" and state["notes"] == "said once\n\n"
    assert "said once" in (store.root / "inbox.md").read_text()
    go()
    assert not (store.root / "inbox.md").exists()
    assert sum(p.count("said once") for p in sent(agents)) == 1
    assert store.load_state()["collected_inbox"] == ""


def test_a_note_newer_than_the_kill_is_still_added(go, agents, work, store, monkeypatch):
    (work / "DONE").write_text("x")
    store.say("said once")
    dies_deleting_the_inbox(go, store, monkeypatch)
    store.say("said later")
    go()
    assert not (store.root / "inbox.md").exists()
    prompts = sent(agents)
    assert sum(p.count("said once") for p in prompts) == 1
    assert sum(p.count("said later") for p in prompts) == 1


def test_a_note_that_ends_like_a_carried_one_is_kept(go, agents, work, store):
    store.save_state({"workdir": str(work), "notes": "do not push to main\n\n"})
    store.say("push to main")
    agents["codex"].script = [done(work)]
    assert go() == 0
    assert agents["codex"].prompts[0].count("push to main") == 2
    assert not (store.root / "inbox.md").exists()


def test_the_same_text_sent_again_after_a_clear_is_carried_again(go, agents, work, store, world):
    def repeated(agent, prompt, ctx):
        store.say("same words")
        return dies_at_once(until=world.now + 3600)(agent, prompt, ctx)

    store.say("same words")
    agents["codex"].script = [repeated]
    agents["antigravity"].script = [done(work)]
    assert go() == 0
    assert agents["antigravity"].prompts[0].count("same words") == 2


def test_the_same_text_after_a_kill_past_the_delete_is_carried(go, agents, work, store):
    """The kill left the mark in state.json with nothing in the inbox to match it."""

    def repeated(agent, prompt, ctx):
        store.say("same words")
        return dies_at_once()(agent, prompt, ctx)

    store.save_state({"workdir": str(work), "collected_inbox": "same words\n\n"})
    agents["codex"].script = [repeated]
    agents["antigravity"].script = [done(work)]
    assert go() == 0
    assert agents["antigravity"].prompts[0].count("same words") == 1


def test_a_completed_collect_leaves_no_mark_in_state_json(tmp_path, work, store, agents, world):
    run = runner._Run(
        make_goal(tmp_path, work), agents, store, world.clock, world.sleep, None, False
    )
    store.say("moved")
    assert run.collect_notes() is False
    state = store.load_state()
    assert state["notes"] == "moved\n\n" and state["collected_inbox"] == ""


def test_a_mark_that_is_not_text_counts_as_none(go, agents, work, store):
    store.save_state({"workdir": str(work), "collected_inbox": 7})
    store.say("still read")
    agents["codex"].script = [done(work)]
    assert go() == 0
    assert "still read" in agents["codex"].prompts[0]


def test_a_note_that_lands_during_the_save_stays_in_the_inbox(go, agents, work, store, monkeypatch):
    (work / "DONE").write_text("x")
    store.say("first")
    real = store.save_state
    said = []

    def save_state(state):
        real(state)
        if state.get("notes") and not said:
            said.append(1)
            store.say("second")

    monkeypatch.setattr(store, "save_state", save_state)
    go()
    assert any("first" in p and "second" in p for p in sent(agents))


# a verify command that cannot run


def exits_with(tmp_path, work):
    """A goal whose gate exits with the number in the file `code`."""
    (work / "code").write_text("127")
    return make_goal(tmp_path, work, verify='exit \\"$(cat code)\\"')


def set_code(work, code):
    def step(agent, prompt, ctx):
        (work / "code").write_text(str(code))
        return RoundResult(COMPLETED)

    return step


def warnings(store):
    return [e for e in store.events() if e["kind"] == "verify_unrunnable"]


def stops(store):
    def step(agent, prompt, ctx):
        store.request_stop()
        return RoundResult(COMPLETED)

    return step


@pytest.mark.parametrize(
    ("command", "code"),
    [("definitely-not-a-command-xyz", 127), ("./not-executable", 126)],
)
def test_a_verify_command_that_cannot_run_warns_once(
    go, tmp_path, agents, work, store, command, code
):
    (work / "not-executable").write_text("#!/bin/sh\n")
    agents["codex"].script = [say(["x"]), say(["x"]), stops(store)]
    go(make_goal(tmp_path, work, verify=command))
    assert kinds(store).count("gate") == 4  # before each of the 3 rounds, and after the last
    found = warnings(store)
    assert len(found) == 1 and found[0]["code"] == code
    assert "not found or not executable" in found[0]["message"]
    assert "agent, node and system directories" in found[0]["message"]


def test_the_warning_changes_no_rotation(tmp_path, world):
    def rounds_with(code):
        root = tmp_path / str(code)
        work = root / "work"
        work.mkdir(parents=True)
        goal = exits_with(root, work)
        (work / "code").write_text(str(code))
        agents = {"codex": Fake("codex"), "antigravity": Fake("antigravity")}
        agents["codex"].script = [result(LIMIT, until=world.now + 40), say(["x"])]
        agents["antigravity"].script = [say(["x"]), set_code(work, 0)]
        store = Store(root / "state")
        assert run_goal(goal, agents, store, clock=world.clock, sleep=world.sleep) == 0
        assert kinds(store)[-1] == "finished"
        return [(e["agent"], e["round"]) for e in store.events() if e["kind"] == "round_start"]

    assert rounds_with(127) == rounds_with(1)


def test_another_outcome_ends_the_run_of_failures(go, tmp_path, agents, work, store):
    goal = exits_with(tmp_path, work)
    agents["codex"].script = [
        set_code(work, 126),
        set_code(work, 1),
        set_code(work, 127),
        set_code(work, 0),
    ]
    go(goal)
    assert [e["code"] for e in warnings(store)] == [127, 127]


@pytest.mark.parametrize(
    ("verify", "extra"),
    [("echo [exit code 127]; exit 1", ""), ("echo [exit code 127]; sleep 30", "timeout_s = 1")],
)
def test_output_that_looks_like_an_exit_code_is_not_one(tmp_path, work, verify, extra):
    _, tail = run_gate(make_goal(tmp_path, work, verify=verify, verify_extra=extra))
    assert "[exit code 127]" in tail
    assert runner.gate_exit_code(tail) != 127


def test_the_warning_is_held_in_state_until_the_command_runs(go, tmp_path, agents, work, store):
    goal = exits_with(tmp_path, work)
    seen = []

    def look(agent, prompt, ctx):
        seen.append(store.load_state().get("verify_warning"))
        (work / "code").write_text("0")
        return RoundResult(COMPLETED)

    agents["codex"].script = [look]
    go(goal)
    assert "not found or not executable" in seen[0]
    assert store.load_state()["verify_warning"] == ""


def test_a_restart_does_not_repeat_the_warning(go, tmp_path, agents, work, store):
    goal = exits_with(tmp_path, work)

    def dies_in_round(agent, prompt, ctx):
        raise Died()

    agents["codex"].script = [dies_in_round, set_code(work, 0)]
    with pytest.raises(Died):
        go(goal)
    assert len(warnings(store)) == 1
    go(goal)
    assert len(warnings(store)) == 1


# a metered round gives way to a plan agent that comes back

FALLBACK = '"codex", "antigravity", "claude_api"'


@pytest.fixture
def fallback(tmp_path, work, store, agents, world, monkeypatch):
    """Codex and Antigravity are out until t0 + 3600; claude_api (metered) is next to run."""
    monkeypatch.setattr(runner, "PREEMPT_POLL_S", 0.02)
    agents["claude_api"] = Fake("claude_api", metered=True)
    for name in ("codex", "antigravity"):
        agents[name].avail = Availability(False, until=world.now + 3600, reason="used up")
    goal = make_goal(tmp_path, work, order=FALLBACK)

    class Setup:
        pass

    setup = Setup()
    setup.goal, setup.agents, setup.world, setup.work, setup.store = (
        goal,
        agents,
        world,
        work,
        store,
    )
    setup.run = lambda: run_goal(goal, agents, store, clock=world.clock, sleep=world.sleep)  # noqa: E501
    return setup


def comes_back(f, name="codex"):
    """Make `name` read as available, the way time passing would."""
    f.agents[name].avail = Availability(True)
    f.world.now += 4000


def until_true(ctx, seconds=3.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if ctx.should_kill():
            return True
        time.sleep(0.005)
    raise AssertionError("the round was never ended")


def test_a_metered_round_is_ended_when_a_plan_agent_reads_available(fallback, store):
    f = fallback

    def claude(agent, prompt, ctx):
        ctx.on_output("working on it")
        comes_back(f)
        until_true(ctx)
        return RoundResult(TRANSIENT, reason="killed")  # whatever Claude makes of its own kill

    f.agents["claude_api"].script = [claude]
    f.agents["codex"].script = [done(f.work)]
    assert f.run() == 0
    ends = [e for e in store.events() if e["kind"] == "round_end"]
    assert [(e["agent"], e["outcome"]) for e in ends] == [
        ("claude_api", PREEMPTED),
        ("codex", COMPLETED),
    ]
    assert "codex" in ends[0]["reason"] and ends[0]["back"] == "codex"
    state = store.load_state()["agents"]["claude_api"]
    assert state["until"] == 0 and state["transient_n"] == 0 and state["limit_n"] == 0
    assert state["reason"] == ""
    assert f.world.sleeps == []  # no backoff, no pause


@pytest.mark.parametrize("outcome", [LIMIT, AUTH, CREDIT_GUARD])
def test_a_refusal_that_came_with_the_kill_is_not_turned_into_a_preemption(
    fallback, store, outcome
):
    f = fallback

    def claude(agent, prompt, ctx):
        ctx.on_output("working on it")
        comes_back(f)
        until_true(ctx)  # the kill lands, and the adapter had already seen the API refuse
        return RoundResult(outcome, until=f.world.now + 7200, reason="Credit balance is too low")

    f.agents["claude_api"].script = [claude]
    f.agents["codex"].script = [done(f.work)]
    assert f.run() == 0
    first = [e for e in store.events() if e["kind"] == "round_end"][0]
    assert first["agent"] == "claude_api" and first["outcome"] == outcome
    assert first["back"] is None
    assert store.load_state()["agents"]["claude_api"]["until"] > f.world.now


def test_the_next_pick_after_a_preemption_is_the_plan_agent(fallback, store):
    f = fallback

    def claude(agent, prompt, ctx):
        ctx.on_output("working on it")
        comes_back(f, "antigravity")
        until_true(ctx)
        return RoundResult(COMPLETED, reason="stop requested")

    f.agents["claude_api"].script = [claude]
    f.agents["antigravity"].script = [done(f.work)]
    assert f.run() == 0
    starts = [e["agent"] for e in store.events() if e["kind"] == "round_start"]
    assert starts == ["claude_api", "antigravity"]
    assert len(f.agents["codex"].prompts) == 0
    first = [e for e in store.events() if e["kind"] == "round_end"][0]
    assert first["outcome"] == PREEMPTED and first["back"] == "antigravity"


def test_a_preempted_round_is_followed_by_the_gate_and_a_snapshot(fallback, store):
    f = fallback

    def claude(agent, prompt, ctx):
        ctx.on_output("half done")
        (f.work / "PARTIAL").write_text("x")
        comes_back(f)
        until_true(ctx)
        return RoundResult(COMPLETED, reason="stop requested")

    f.agents["claude_api"].script = [claude]
    f.agents["codex"].script = [done(f.work)]
    assert f.run() == 0
    kinds_seen = kinds(store)
    first_end = kinds_seen.index("round_end")
    assert [e["outcome"] for e in store.events() if e["kind"] == "round_end"][0] == PREEMPTED
    assert kinds_seen[first_end + 1] == "gate"  # the gate runs as after any round that worked
    assert "half done" in f.agents["codex"].prompts[0]  # the tail reaches the next agent


def test_no_availability_read_of_a_plan_agent_before_its_until(fallback):
    f = fallback
    calls, during = [], []

    def claude(agent, prompt, ctx):
        ctx.on_output("working")
        for name in ("codex", "antigravity"):
            f.agents[name].availability = lambda n=name: (calls.append(n), Availability(True))[1]
        time.sleep(0.3)  # many polls, but the cooldowns have not passed
        during.extend(calls)
        assert ctx.should_kill() is False
        return RoundResult(COMPLETED)

    f.agents["claude_api"].script = [claude]
    f.agents["codex"].script = [done(f.work)]
    f.run()
    assert during == []


def test_reads_at_most_once_per_poll_and_a_no_refreshes_until(fallback, store, monkeypatch):
    f = fallback
    monkeypatch.setattr(runner, "PREEMPT_POLL_S", 0.1)
    calls, during = [], []

    def claude(agent, prompt, ctx):
        ctx.on_output("working")
        f.world.now += 4000  # both cooldowns are over

        def no(n):
            calls.append(n)
            return Availability(False, until=f.world.now + 3600, reason="still full")

        for name in ("codex", "antigravity"):
            f.agents[name].availability = lambda n=name: no(n)
        time.sleep(0.55)
        during.extend(calls)
        assert ctx.should_kill() is False
        for name in ("codex", "antigravity"):  # let the loop move on to codex
            f.agents[name].availability = lambda: Availability(True)
        return RoundResult(COMPLETED)

    f.agents["claude_api"].script = [claude]
    f.agents["codex"].script = [done(f.work)]
    f.run()
    # one read each at the first poll; the answer pushed `until` out, so no more after that
    assert sorted(during) == ["antigravity", "codex"]


def test_a_read_that_raises_leaves_the_round_running_and_refreshes_until(fallback):
    f = fallback
    calls, during = [], []

    def claude(agent, prompt, ctx):
        ctx.on_output("working")
        f.world.now += 4000

        def boom():
            calls.append(1)
            raise RuntimeError("cannot read")

        f.agents["codex"].availability = boom
        f.agents["antigravity"].availability = lambda: Availability(
            False, until=f.world.now + 3600, reason="full"
        )
        time.sleep(0.4)
        during.extend(calls)
        assert ctx.should_kill() is False
        for name in ("codex", "antigravity"):  # let the loop move on to codex
            f.agents[name].availability = lambda: Availability(True)
        return RoundResult(COMPLETED)

    f.agents["claude_api"].script = [claude]
    f.agents["codex"].script = [done(f.work)]
    f.run()
    assert len(during) == 1  # asked again only after the refreshed until


def test_a_slow_read_never_blocks_the_stream(fallback, store):
    f = fallback
    release, entered = threading.Event(), threading.Event()
    lines_seen = []

    def claude(agent, prompt, ctx):
        f.world.now += 4000

        def slow():
            entered.set()
            release.wait(10)
            return Availability(False, until=f.world.now + 3600)

        f.agents["codex"].availability = slow
        f.agents["antigravity"].availability = slow
        time.sleep(0.15)  # the watcher is now stuck inside the read
        for i in range(20):
            ctx.on_output(f"line {i}")
            lines_seen.append(i)
            assert ctx.should_kill() is False
        for name in ("codex", "antigravity"):  # let the loop move on to codex
            f.agents[name].availability = lambda: Availability(True)
        return RoundResult(COMPLETED)

    f.agents["claude_api"].script = [claude]
    f.agents["codex"].script = [done(f.work)]
    started = time.monotonic()
    try:
        f.run()
    finally:
        release.set()
    assert entered.is_set()  # the watcher really was inside the read
    assert len(lines_seen) == 20
    assert time.monotonic() - started < 8  # the stuck read did not hold the end of the round


def test_the_watcher_thread_is_gone_after_the_round_even_when_it_raises(fallback, store):
    f = fallback
    before = {t.ident for t in threading.enumerate()}

    def claude(agent, prompt, ctx):
        f.world.now += 4000
        time.sleep(0.1)
        raise KeyboardInterrupt

    f.agents["claude_api"].script = [claude]
    with pytest.raises(KeyboardInterrupt):
        f.run()
    time.sleep(0.2)
    assert {t.ident for t in threading.enumerate()} <= before


def test_a_plan_round_is_never_preempted(go, agents, work, store, monkeypatch):
    monkeypatch.setattr(runner, "PREEMPT_POLL_S", 0.01)
    calls = []
    agents["antigravity"].availability = lambda: (calls.append(1), Availability(True))[1]

    def codex(agent, prompt, ctx):
        ctx.on_output("working")
        time.sleep(0.2)
        assert ctx.should_kill() is False
        (work / "DONE").write_text("x")
        return RoundResult(COMPLETED)

    agents["codex"].script = [codex]
    assert go() == 0
    assert calls == []
    assert [e["outcome"] for e in store.events() if e["kind"] == "round_end"] == [COMPLETED]


def test_a_stop_request_wins_over_preemption(fallback, store):
    f = fallback

    def claude(agent, prompt, ctx):
        ctx.on_output("working")
        store.request_stop("now")
        comes_back(f)
        time.sleep(0.2)  # the watcher has found codex by now
        assert ctx.should_kill() is True
        return RoundResult(COMPLETED, reason="stop requested")

    f.agents["claude_api"].script = [claude]
    assert f.run() == 0
    ends = [e for e in store.events() if e["kind"] == "round_end"]
    assert [e["outcome"] for e in ends] == [COMPLETED]  # not preempted
    assert kinds(store)[-1] == "stopped"


def test_a_note_wins_over_preemption(fallback, store):
    f = fallback

    def claude(agent, prompt, ctx):
        ctx.on_output("working")
        store.say("change of plan")
        store.request_interrupt()
        comes_back(f)
        time.sleep(0.2)
        assert ctx.should_kill() is True
        return RoundResult(COMPLETED, reason="stop requested")

    f.agents["claude_api"].script = [claude]
    f.agents["codex"].script = [done(f.work)]
    assert f.run() == 0
    first = [e for e in store.events() if e["kind"] == "round_end"][0]
    assert first["interrupted"] is True and first["outcome"] == COMPLETED
    assert "change of plan" in f.agents["codex"].prompts[0]


def test_notes_survive_a_preemption_by_the_kept_notes_rule(fallback, store):
    f = fallback

    def silent(agent, prompt, ctx):  # killed before it said anything: the notes are kept
        comes_back(f)
        until_true(ctx)
        return RoundResult(COMPLETED, reason="stop requested")

    store.say("the first note")
    f.agents["claude_api"].script = [silent]
    f.agents["codex"].script = [done(f.work)]
    assert f.run() == 0
    assert "the first note" in f.agents["codex"].prompts[0]


def test_notes_a_preempted_round_that_worked_has_read_are_spent(fallback, store):
    f = fallback

    def worked(agent, prompt, ctx):
        ctx.on_output("on it")
        comes_back(f)
        until_true(ctx)
        return RoundResult(COMPLETED, reason="stop requested")

    store.say("the first note")
    f.agents["claude_api"].script = [worked]
    f.agents["codex"].script = [done(f.work)]
    assert f.run() == 0
    assert "the first note" not in f.agents["codex"].prompts[0]


# a plan agent that reads ok but cannot hold a round stops being allowed to kill Claude


def fails_at_once(agent, prompt, ctx):
    return RoundResult(TRANSIENT, reason="exited at once")


def test_a_plan_agent_that_fails_at_once_after_a_preemption_costs_one_kill(fallback, store):
    f = fallback
    kills = []

    def claude_killed(agent, prompt, ctx):
        ctx.on_output("working on it")
        f.world.now += 4000  # every cooldown is over
        for name in ("codex", "antigravity"):
            f.agents[name].avail = Availability(True)  # and both read ok again
        until_true(ctx)
        kills.append(1)
        return RoundResult(COMPLETED, reason="stop requested")

    def claude_left_alone(agent, prompt, ctx):
        ctx.on_output("working on it")
        f.world.now += 4000
        time.sleep(0.3)  # many polls: both plan agents read ok, neither may kill
        assert ctx.should_kill() is False
        (f.work / "DONE").write_text("x")
        return RoundResult(COMPLETED)

    def runaway_stop(agent, prompt, ctx):  # only reached when the loop does not end by itself
        (f.work / "DONE").write_text("x")
        return RoundResult(COMPLETED)

    f.agents["claude_api"].script = [claude_killed, claude_killed, claude_left_alone, runaway_stop]
    f.agents["codex"].script = [fails_at_once, fails_at_once, runaway_stop]
    f.agents["antigravity"].script = [fails_at_once, fails_at_once, runaway_stop]
    assert f.run() == 0
    ends = [e for e in store.events() if e["kind"] == "round_end"]
    preempted = [e["back"] for e in ends if e["outcome"] == PREEMPTED]
    assert sorted(preempted) == ["antigravity", "codex"]  # one kill each, not one per attempt
    assert len(kills) == 2
    agents = store.load_state()["agents"]
    assert agents["codex"]["blocked"] and agents["antigravity"]["blocked"]


def test_a_blocked_plan_agent_is_still_run_at_a_round_boundary(fallback, store):
    f = fallback

    def claude(agent, prompt, ctx):
        ctx.on_output("working on it")
        f.world.now += 4000
        f.agents["codex"].avail = Availability(True)
        until_true(ctx)
        return RoundResult(COMPLETED, reason="stop requested")

    def claude_again(agent, prompt, ctx):
        ctx.on_output("working on it")
        f.world.now += 4000  # codex's cooldown is over, it reads ok, and it is blocked
        time.sleep(0.3)
        assert ctx.should_kill() is False
        return RoundResult(COMPLETED)

    f.agents["claude_api"].script = [claude, claude_again]
    f.agents["codex"].script = [fails_at_once, done(f.work)]
    assert f.run() == 0
    starts = [e["agent"] for e in store.events() if e["kind"] == "round_start"]
    assert starts == ["claude_api", "codex", "claude_api", "codex"]  # picked at the boundary
    state = store.load_state()["agents"]["codex"]
    assert state["blocked"] is False  # its completed round cleared the block


def test_a_limit_after_a_long_round_following_a_preemption_does_not_block(fallback, store):
    f = fallback

    def claude(agent, prompt, ctx):
        ctx.on_output("working on it")
        f.world.now += 4000
        f.agents["codex"].avail = Availability(True)
        until_true(ctx)
        return RoundResult(COMPLETED, reason="stop requested")

    def codex_works_then_hits_its_limit(agent, prompt, ctx):
        ctx.on_output("working")
        f.world.now += 600
        f.agents["codex"].avail = Availability(False, until=f.world.now + 3600, reason="full")
        return RoundResult(LIMIT, until=f.world.now + 3600, reason="usage limit")

    f.agents["claude_api"].script = [claude, done(f.work)]
    f.agents["codex"].script = [codex_works_then_hits_its_limit]
    assert f.run() == 0
    assert store.load_state()["agents"]["codex"]["blocked"] is False


# the watcher's read and the read in `pick` disagree: one kill per agent, and a day's cap

DISAGREE_NAME = "baton-preempt"


def flapping(adapter):
    """Reads ok to the watcher thread and not available to anyone else (`pick`)."""

    def availability():
        if threading.current_thread().name == DISAGREE_NAME:
            return Availability(True)
        return Availability(False, reason="not really")

    adapter.availability = availability


def killed_then_back(f, kills=None):
    """A Claude round that runs until the watcher ends it, with every cooldown over."""

    def step(agent, prompt, ctx):
        ctx.on_output("working on it")
        f.world.now += 4000
        until_true(ctx)
        if kills is not None:
            kills.append(1)
        return RoundResult(COMPLETED, reason="stop requested")

    return step


def left_alone(f, seen):
    """A Claude round that the watcher must not end, whatever it reads, and then finishes. Whether
    it was left alone goes into `seen` (an assert here would be swallowed as an adapter error)."""

    def step(agent, prompt, ctx):
        ctx.on_output("working on it")
        f.world.now += 4000
        time.sleep(0.3)  # many polls
        seen.append(ctx.should_kill() is False)
        (f.work / "DONE").write_text("x")
        return RoundResult(COMPLETED)

    return step


def test_a_flapping_read_costs_one_kill_per_agent_and_blocks_it(fallback, store):
    f = fallback
    for name in ("codex", "antigravity"):
        flapping(f.agents[name])
    seen = []
    f.agents["claude_api"].script = [killed_then_back(f), killed_then_back(f), left_alone(f, seen)]
    assert f.run() == 0
    assert seen == [True]
    events = store.events()
    backs = [e["back"] for e in events if e["kind"] == "round_end" and e["outcome"] == PREEMPTED]
    assert sorted(backs) == ["antigravity", "codex"]
    assert {e["agent"] for e in events if e["kind"] == "round_start"} == {"claude_api"}
    agents = store.load_state()["agents"]
    assert agents["codex"]["blocked"] and agents["antigravity"]["blocked"]
    assert not agents["codex"]["after_preempt"] and not agents["antigravity"]["after_preempt"]


def test_a_block_set_by_a_flapping_read_clears_when_the_agent_completes_a_round(fallback, store):
    f = fallback
    flapping(f.agents["codex"])

    def claude_then_codex_is_steady(agent, prompt, ctx):
        f.world.now += 4000
        del f.agents["codex"].availability  # back to the plain `avail` attribute
        f.agents["codex"].avail = Availability(True)
        time.sleep(0.3)
        assert ctx.should_kill() is False  # blocked: the watcher does not look
        return RoundResult(COMPLETED)

    f.agents["claude_api"].script = [killed_then_back(f), claude_then_codex_is_steady]
    f.agents["codex"].script = [done(f.work)]
    assert f.run() == 0
    starts = [e["agent"] for e in store.events() if e["kind"] == "round_start"]
    assert starts == ["claude_api", "claude_api", "codex"]
    assert store.load_state()["agents"]["codex"]["blocked"] is False


def test_the_daily_cap_stops_the_seventh_kill_when_the_blocks_do_not_hold(
    fallback, store, monkeypatch
):
    from baton.scheduler import MAX_PREEMPTS_PER_DAY, Scheduler

    monkeypatch.setattr(Scheduler, "_block_unproven", lambda self, agent: None)
    f = fallback
    flapping(f.agents["codex"])
    kills, seen = [], []
    f.agents["claude_api"].script = [killed_then_back(f, kills) for _ in range(6)] + [
        left_alone(f, seen)
    ]
    assert f.run() == 0
    assert seen == [True]
    assert MAX_PREEMPTS_PER_DAY == len(kills) == 6
    ends = [e for e in store.events() if e["kind"] == "round_end"]
    assert [e["outcome"] for e in ends].count(PREEMPTED) == 6
    assert len(store.load_state()["preempts"]) == 6


def test_the_daily_cap_stops_the_seventh_kill_when_the_plan_agent_never_blocks(fallback, store):
    f = fallback
    kills, seen = [], []

    def claude_killed(agent, prompt, ctx):
        f.agents["codex"].avail = Availability(True)
        return killed_then_back(f, kills)(agent, prompt, ctx)

    def codex_idles_a_round(agent, prompt, ctx):
        # it completes, so no block ever sets; then it is used up again
        f.agents["codex"].avail = Availability(False, until=f.world.now + 3600, reason="full")
        return RoundResult(COMPLETED)

    def last_claude(agent, prompt, ctx):
        f.agents["codex"].avail = Availability(True)
        return left_alone(f, seen)(agent, prompt, ctx)

    f.agents["claude_api"].script = [claude_killed] * 6 + [last_claude]
    f.agents["codex"].script = [codex_idles_a_round] * 6
    assert f.run() == 0
    assert seen == [True]
    assert len(kills) == 6
    assert len(f.agents["codex"].prompts) == 6
    assert len(store.load_state()["preempts"]) == 6


# long-term memory


class FakeMemory:
    """Stands in for `baton.memory.Memory`, recording what the runner asks of it. How it behaves
    comes from `Plan`, set by the `memory` fixture before the runner builds it."""

    class Plan:
        text = ""
        hits = 0
        raises: Exception | None = None
        error_on_recall = ""
        on_recall = None

    built: list["FakeMemory"] = []

    def __init__(self, cfg, workdir, job, **kw):
        self.cfg, self.workdir, self.job = cfg, workdir, job
        self.recalls: list[tuple] = []
        self.rounds: list[RoundEntry] = []
        self.lessons: list[str] = []
        self.last_hits = 0
        self.last_error = ""
        self.plan = FakeMemory.Plan
        FakeMemory.built.append(self)

    def round_path(self, n):
        return f"rounds/{self.job}/r{n}.md"

    def recall(self, objective, gate_tail, exclude=""):
        self.recalls.append((objective, gate_tail, exclude))
        if self.plan.on_recall:
            self.plan.on_recall()
        if self.plan.raises:
            raise self.plan.raises
        if self.plan.error_on_recall:
            self.last_error = self.plan.error_on_recall
            self.plan.error_on_recall = ""
        self.last_hits = self.plan.hits
        return self.plan.text

    def remember_round(self, entry):
        if self.plan.raises:
            raise self.plan.raises
        self.rounds.append(entry)

    def remember_lessons(self, handoff):
        if self.plan.raises:
            raise self.plan.raises
        self.lessons.append(handoff)


@pytest.fixture
def memory(monkeypatch):
    """Memory switched on, with a fake in place of the real class. Returns the fake's plan."""
    monkeypatch.delenv("BATON_MEMORY", raising=False)
    FakeMemory.built = []
    plan = type("Plan", (FakeMemory.Plan,), {})
    monkeypatch.setattr(FakeMemory, "Plan", plan)
    monkeypatch.setattr(runner, "Memory", FakeMemory)
    plan.mem = lambda: FakeMemory.built[0]
    return plan


def memory_events(store):
    return [e for e in store.events() if e["kind"] == "memory"]


def plain_errors(store):
    return [e for e in store.events() if e["kind"] == "error"]


def test_build_prompt_puts_memory_after_the_handoff_and_before_the_rules(tmp_path, work):
    goal = make_goal(tmp_path, work)
    prompt = build_prompt(goal, "", "", "the handoff", "", False, memory="MEMORY BLOCK")
    assert prompt.index("the handoff") < prompt.index("MEMORY BLOCK") < prompt.index("Rules:")


def test_a_resumed_prompt_also_carries_the_memory(tmp_path, work):
    goal = make_goal(tmp_path, work)
    prompt = build_prompt(goal, "", "", "the handoff", "", True, memory="MEMORY BLOCK")
    assert prompt.index("the handoff") < prompt.index("MEMORY BLOCK")
    assert "Rules:" not in prompt


def test_no_memory_changes_nothing_in_the_prompt(tmp_path, work):
    goal = make_goal(tmp_path, work)
    assert build_prompt(goal, "g", "n", "h", "t", False) == build_prompt(
        goal, "g", "n", "h", "t", False, memory=""
    )


def test_the_rules_ask_for_lessons_under_their_heading():
    assert (
        "- Keep durable findings (root causes, dead ends, what must not be retried) under a "
        "`## Lessons` heading in the handoff. They outlive this goal."
    ) in runner.RULES


def test_recalled_text_reaches_the_agent_and_is_announced(go, agents, work, store, memory):
    memory.text, memory.hits = "REMEMBERED the widget broke on boot", 2
    agents["codex"].script = [done(work)]
    go()
    assert "REMEMBERED the widget broke on boot" in agents["codex"].prompts[0]
    event = memory_events(store)[0]
    assert event["event"] == "recalled" and event["hits"] == 2
    assert event["chars"] == len("REMEMBERED the widget broke on boot")


def test_a_resumed_round_gets_the_memory_too(go, agents, work, world, memory):
    memory.text, memory.hits = "REMEMBERED the widget broke on boot", 1

    def first(agent, prompt, ctx):
        ctx.on_session("s1")
        return RoundResult(LIMIT, until=world.now + 10)

    agents["codex"].script = [first, done(work)]
    agents["antigravity"].script = [result(LIMIT, until=world.now + 3600)]
    go()
    assert agents["codex"].sessions == [None, "s1"]
    assert "REMEMBERED" in agents["codex"].prompts[1]


def test_no_hits_means_no_block_and_no_event(go, agents, work, store, memory):
    agents["codex"].script = [done(work)]
    go()
    assert "Memory from earlier" not in agents["codex"].prompts[0]
    assert memory_events(store) == []


def test_memory_is_recalled_before_every_round_excluding_the_page_before(go, agents, work, memory):
    agents["codex"].script = [result(COMPLETED), result(COMPLETED), done(work)]
    go()
    recalls = memory.mem().recalls
    assert len(recalls) == 3
    assert {r[0] for r in recalls} == {OBJECTIVE}
    assert [r[2] for r in recalls][:3] == ["", "rounds/demo/r1.md", "rounds/demo/r2.md"]


def test_the_gate_output_is_part_of_the_recall(tmp_path, work, go, agents, memory):
    goal = make_goal(tmp_path, work, verify="echo gate-says-hello; test -f DONE")
    agents["codex"].script = [done(work)]
    # the first gate fails, so the first round sees its output
    go(goal)
    assert "gate-says-hello" in memory.mem().recalls[0][1]


def test_the_recall_comes_after_the_handoff_is_read(go, agents, work, memory):
    (work / ".baton").mkdir()
    (work / ".baton" / "HANDOFF.md").write_text("older notes")
    agents["codex"].script = [done(work)]
    go()
    assert "older notes" in agents["codex"].prompts[0]


def test_a_failure_is_one_event_per_failure(go, agents, work, store, memory):
    memory.error_on_recall = "memory is unavailable: gone"
    agents["codex"].script = [result(TRANSIENT)]
    agents["antigravity"].script = [result(COMPLETED), done(work)]
    go()
    errors = [e for e in memory_events(store) if e["event"] == "error"]
    assert [e["reason"] for e in errors] == ["memory is unavailable: gone"]
    assert memory.mem().last_error == ""  # reported once, then cleared


def test_a_memory_that_raises_never_reaches_the_loop(go, agents, work, store, world, memory):
    """The guard around every call: a bug in Memory must cost the round nothing."""
    memory.raises = RuntimeError("memory exploded")
    agents["codex"].script = [done(work)]
    assert go() == 0
    assert len(agents["codex"].prompts) == 1  # the round ran, once
    assert "finished" in kinds(store)
    assert plain_errors(store) == []  # the loop's own error handler never saw it
    assert 60 not in world.sleeps and sum(world.sleeps) < 60  # and no error sleep
    reasons = [e["reason"] for e in memory_events(store) if e["event"] == "error"]
    assert reasons and all("memory exploded" in r for r in reasons)


def test_a_memory_that_raises_while_recording_does_not_stop_the_goal(
    tmp_path, go, agents, work, store, memory
):
    memory.raises = RuntimeError("write failed")
    agents["codex"].script = [result(COMPLETED), done(work)]
    agents["antigravity"].script = [result(COMPLETED)]
    assert go() == 0
    assert "finished" in kinds(store) and plain_errors(store) == []


# recording


def test_a_round_is_recorded_after_its_gate_with_the_gate_result(
    tmp_path, go, agents, work, store, memory
):
    goal = make_goal(tmp_path, work, verify="echo checking; test -f DONE")
    agents["codex"].script = [say(["round one talking"], result(COMPLETED)), done(work)]
    go(goal)
    rounds = memory.mem().rounds
    # Each round is written as soon as it ends, with no gate result, then again with the real one.
    assert [(r.n, r.agent, r.gate) for r in rounds] == [
        (1, "codex", "none"),
        (1, "codex", "failed"),
        (2, "codex", "none"),
        (2, "codex", "passed"),
    ]
    assert rounds[0].gate_tail == "" and "checking" in rounds[1].gate_tail
    assert rounds[1].outcome == COMPLETED
    assert rounds[1].round_tail.strip() == "round one talking"
    assert isinstance(rounds[1].seconds, float)


def test_without_a_verify_command_a_round_is_recorded_at_once(
    tmp_path, go, agents, work, store, memory
):
    agents["codex"].script = [result(COMPLETED)]
    go(make_goal(tmp_path, work, verify=None))
    rounds = memory.mem().rounds
    assert len(rounds) == 1 and rounds[0].gate == "none" and rounds[0].gate_tail == ""
    assert rounds[0].n == 1 and rounds[0].agent == "codex"


def test_a_round_that_never_spawned_a_process_is_not_recorded(
    tmp_path, go, agents, work, store, memory
):
    agents["codex"].script = [refused()]
    agents["antigravity"].script = [done(work)]
    go()
    ends = [e for e in store.events() if e["kind"] == "round_end"]
    assert [(e["round"], e["agent"]) for e in ends] == [(1, "codex"), (2, "antigravity")]
    assert {r.n for r in memory.mem().rounds} == {2}  # round 1 was refused: nothing ran


@pytest.mark.parametrize("outcome", [LIMIT, TIMEOUT, TRANSIENT, AUTH, CREDIT_GUARD])
def test_a_failed_round_is_recorded_with_its_real_outcome(go, agents, work, store, memory, outcome):
    agents["codex"].script = [result(outcome, reason="why it ended", until=1_000_500.0), done(work)]
    agents["antigravity"].script = [done(work)]
    go()
    first = memory.mem().rounds[0]
    assert first.outcome == outcome and first.reason == "why it ended" and first.n == 1


def test_a_preempted_round_is_recorded_as_preempted(go, agents, work, store, memory):
    agents["codex"].script = [result(PREEMPTED, reason="codex is back", back="codex"), done(work)]
    go()
    assert memory.mem().rounds[0].outcome == PREEMPTED


def test_a_stop_after_the_round_still_records_it_through_the_gate(go, agents, work, store, memory):
    """step runs the gate before honouring a stop, so the pending entry is settled there."""
    agents["codex"].script = [lambda a, p, c: (store.request_stop(), RoundResult(TIMEOUT))[1]]
    assert go() == 0
    rounds = memory.mem().rounds
    assert [(r.n, r.outcome, r.gate) for r in rounds] == [
        (1, TIMEOUT, "none"),
        (1, TIMEOUT, "failed"),
    ]
    assert kinds(store)[-1] == "stopped"


def test_a_stop_now_records_the_round_as_interrupted_with_no_gate_result(
    tmp_path, go, agents, work, store, memory
):
    def step(agent, prompt, ctx):
        (work / "STARTED").write_text("x")
        store.request_stop(now=True)
        assert ctx.should_kill() is True
        return RoundResult(COMPLETED, reason="stop requested")

    agents["codex"].script = [step]
    verify = "test -f STARTED && sleep 30; test -f DONE"  # slow only once a round has begun
    assert go(make_goal(tmp_path, work, verify=verify)) == 0
    rounds = memory.mem().rounds
    assert [r.outcome for r in rounds] == ["interrupted", "interrupted"]
    assert rounds[-1].gate == "none"  # the gate was ended, not failed
    assert rounds[-1].reason == "stop requested"
    assert kinds(store)[-1] == "stopped"
    # the scheduler and the person still see what the adapter reported
    end = [e for e in store.events() if e["kind"] == "round_end"][0]
    assert end["outcome"] == COMPLETED


def test_a_round_ended_by_an_interrupt_is_recorded_as_interrupted(
    tmp_path, go, agents, work, store, memory
):
    agents["codex"].script = [interrupting(store, "look at this"), done(work)]
    go(make_goal(tmp_path, work, verify=None))
    assert memory.mem().rounds[0].outcome == "interrupted"


def test_a_round_the_adapter_ended_on_its_own_stays_completed(go, agents, work, memory):
    agents["codex"].script = [result(COMPLETED), done(work)]
    go()
    assert memory.mem().rounds[0].outcome == COMPLETED


def test_a_credit_guard_that_came_with_a_kill_is_not_called_interrupted(
    tmp_path, go, agents, work, store, memory
):
    def step(agent, prompt, ctx):
        store.request_stop(now=True)
        ctx.should_kill()
        return RoundResult(CREDIT_GUARD, reason="balance moved")

    agents["codex"].script = [step]
    go(make_goal(tmp_path, work, verify=None))
    assert memory.mem().rounds[0].outcome == CREDIT_GUARD


def test_a_round_is_on_record_before_its_gate_runs(tmp_path, go, agents, work, store, memory):
    """A runner killed during the gate (a SIGTERM, a crash) must not lose the round."""
    agents["codex"].script = [say(["worked"], result(COMPLETED))]
    real = runner.run_gate
    calls = []

    def dies_in_the_second_gate(goal, **kw):
        calls.append(1)
        if len(calls) == 2:
            raise Died()
        return real(goal, **kw)

    runner.run_gate = dies_in_the_second_gate
    try:
        with pytest.raises(Died):
            go()
    finally:
        runner.run_gate = real
    rounds = memory.mem().rounds
    assert [(r.n, r.agent, r.outcome, r.gate) for r in rounds] == [(1, "codex", COMPLETED, "none")]
    assert rounds[0].round_tail.strip() == "worked"


def test_a_round_without_a_verify_command_that_does_not_finish_the_goal_is_still_recorded(
    tmp_path, go, agents, work, store, memory
):
    agents["codex"].script = [lambda a, p, c: (store.request_stop(), RoundResult(TIMEOUT))[1]]
    assert go(make_goal(tmp_path, work, verify=None)) == 0
    assert "finished" not in kinds(store)
    rounds = memory.mem().rounds
    assert [(r.n, r.outcome, r.gate) for r in rounds] == [(1, TIMEOUT, "none")]


def test_a_stop_that_arrives_while_memory_is_read_spawns_nothing(
    tmp_path, go, agents, work, store, memory
):
    memory.on_recall = lambda: store.request_stop()
    agents["codex"].script = [done(work)]
    assert go() == 0
    assert agents["codex"].prompts == []  # no process for that round
    assert memory.mem().rounds == [] and memory.mem().lessons == []
    assert "round_start" not in kinds(store) and "round_end" not in kinds(store)
    assert kinds(store)[-1] == "stopped"
    state = store.load_state()
    assert state["round"] == 0  # the number is not spent on a round that never was


def test_after_an_abandoned_round_the_next_run_starts_at_the_same_number(
    tmp_path, go, agents, work, store, memory
):
    memory.on_recall = lambda: store.request_stop()
    go()
    memory.on_recall = None
    FakeMemory.built.clear()
    agents["codex"].script = [done(work)]
    assert go() == 0
    assert [r.n for r in memory.mem().rounds][0] == 1


def test_an_abandoned_round_does_not_run_the_gate_a_second_time(
    tmp_path, go, agents, work, store, memory
):
    memory.on_recall = lambda: store.request_stop()
    gates = []
    real = runner.run_gate

    def counting(goal, **kw):
        gates.append(1)
        return real(goal, **kw)

    runner.run_gate = counting
    try:
        go()
    finally:
        runner.run_gate = real
    assert len(gates) == 1


def test_without_memory_a_stop_is_handled_exactly_as_before(go, agents, work, store):
    agents["codex"].script = [lambda a, p, c: (store.request_stop(), RoundResult(COMPLETED))[1]]
    assert go() == 0
    assert len(agents["codex"].prompts) == 1


def test_a_missing_ai_memory_binary_is_quiet_in_the_loop(
    tmp_path, work, agents, store, world, monkeypatch
):
    from test_memory import FakeAiMemory

    fake = FakeAiMemory()
    monkeypatch.delenv("BATON_MEMORY", raising=False)

    def real(cfg, workdir, job):
        return Memory(
            cfg,
            workdir,
            job,
            run=fake.run,
            popen=fake.popen,
            health=fake.health,
            clock=world.clock,
            sleep=world.sleep,
            which=lambda *a, **k: None,
        )

    monkeypatch.setattr(runner, "Memory", real)
    agents["codex"].script = [result(COMPLETED), done(work)]
    run_goal(make_goal(tmp_path, work), agents, store, world.clock, world.sleep)
    assert memory_events(store) == []
    assert fake.runs == [] and fake.popens == []
    assert not (state_base() / ".ai-memory").exists()


def test_a_goal_that_finishes_flushes_what_is_still_pending(
    tmp_path, work, store, agents, world, memory
):
    goal = make_goal(tmp_path, work)
    run = runner._Run(goal, agents, store, world.clock, world.sleep)
    entry = RoundEntry(1, "codex", "completed", "", 1.0, "none", "", "", "h", "t")
    run.pending = entry
    assert run.finish("round completed") == 0
    assert memory.mem().rounds == [entry]
    assert run.pending is None


def test_the_handoff_is_read_from_disk_after_the_round_without_the_prompt_cut(
    tmp_path, go, agents, work, store, memory
):
    long_handoff = "## Lessons\n- keep this\n\n## Notes\n" + "x" * 20000

    def writes(agent, prompt, ctx):
        (work / ".baton").mkdir(exist_ok=True)
        (work / ".baton" / "HANDOFF.md").write_text(long_handoff)
        return RoundResult(COMPLETED)

    agents["codex"].script = [writes]
    go(make_goal(tmp_path, work, verify=None))
    mem = memory.mem()
    assert mem.rounds[0].handoff == long_handoff  # what the agent wrote, whole
    assert mem.lessons == [long_handoff]  # so the lessons at the top survive the 16000 cut


def handoff_of(work, text, limit=True):
    # read_handoff only needs the goal's workdir.
    (work / ".baton").mkdir(exist_ok=True)
    (work / ".baton" / "HANDOFF.md").write_text(text)
    stub = types.SimpleNamespace(goal=types.SimpleNamespace(workdir=work))
    return runner._Run.read_handoff(stub, limit=limit)


def test_a_handoff_within_the_limit_is_returned_unchanged(work):
    for size in (10, runner.HANDOFF_MAX_CHARS):
        text = "h" * size
        assert handoff_of(work, text) == text


def test_a_long_handoff_keeps_its_top_and_its_end_and_says_what_it_left_out(work):
    head = "## Objective\n- ship it, authorised by the person\n" + "H" * 5000
    middle = "m" * 30000
    tail = "- next: run the tests\nEND"
    text = head + middle + tail
    shown = handoff_of(work, text)
    keep_tail = runner.HANDOFF_MAX_CHARS - runner.HANDOFF_HEAD_CHARS
    assert shown.startswith(text[: runner.HANDOFF_HEAD_CHARS])
    assert shown.endswith(text[-keep_tail:]) and shown.endswith("END")
    left_out = len(text) - runner.HANDOFF_MAX_CHARS
    (marker,) = [x for x in shown.splitlines() if "left out" in x]
    assert f"{left_out} characters" in marker and "HANDOFF.md" in marker
    assert len(shown) <= runner.HANDOFF_MAX_CHARS + len(marker) + 2


def test_a_handoff_one_over_the_limit_leaves_out_one_character(work):
    shown = handoff_of(work, "a" * (runner.HANDOFF_MAX_CHARS + 1))
    assert "1 characters" in shown


def test_the_whole_handoff_is_still_available_unlimited(work):
    text = "top\n" + "x" * 50000 + "\nbottom"
    assert handoff_of(work, text, limit=False) == text


def test_the_prompt_carries_the_top_of_a_long_handoff(go, agents, work):
    def writes(agent, prompt, ctx):
        (work / ".baton").mkdir(exist_ok=True)
        text = "OBJECTIVE-AT-THE-TOP\n" + "x" * 30000 + "\nLAST-LINE"
        (work / ".baton" / "HANDOFF.md").write_text(text)
        return RoundResult(COMPLETED)

    agents["codex"].script = [writes, done(work)]
    go()
    prompt = agents["codex"].prompts[1]
    assert "OBJECTIVE-AT-THE-TOP" in prompt and "LAST-LINE" in prompt


def test_lessons_are_offered_after_every_record(go, agents, work, store, memory):
    agents["codex"].script = [result(COMPLETED), done(work)]
    agents["antigravity"].script = [result(COMPLETED)]
    go()
    mem = memory.mem()
    assert len(mem.lessons) == len(mem.rounds) >= 2


def test_the_commits_of_a_round_are_in_its_entry(
    tmp_path, repo, commit, agents, store, world, memory
):
    goal = make_goal(tmp_path, repo, verify=None)

    def works(agent, prompt, ctx):
        commit("w.txt", "the widget fix")
        return RoundResult(COMPLETED)

    agents["codex"].script = [works]
    run_goal(goal, agents, store, world.clock, world.sleep)
    assert "the widget fix" in memory.mem().rounds[0].commits


# switches


def test_memory_false_in_the_goal_builds_no_memory_at_all(
    tmp_path, work, go, agents, store, memory, monkeypatch
):
    def must_not_load():
        raise AssertionError("settings were read for a goal that has memory off")

    monkeypatch.setattr(runner, "load_memory", must_not_load)
    agents["codex"].script = [done(work)]
    go(make_goal(tmp_path, work, top="memory = false"))
    assert FakeMemory.built == []
    assert memory_events(store) == []
    assert "Memory from earlier" not in agents["codex"].prompts[0]


def test_baton_memory_off_builds_no_memory_at_all(go, agents, work, memory, monkeypatch):
    monkeypatch.setenv("BATON_MEMORY", "off")
    agents["codex"].script = [done(work)]
    go()
    assert FakeMemory.built == []


def test_enabled_false_in_the_defaults_builds_no_memory(
    go, agents, work, memory, tmp_path, monkeypatch
):
    path = tmp_path / "off.toml"
    path.write_text("[memory]\nenabled = false\n")
    monkeypatch.setenv("BATON_CONFIG", str(path))
    agents["codex"].script = [done(work)]
    go()
    assert FakeMemory.built == []


def test_the_defaults_reach_the_memory_that_is_built(
    go, agents, work, memory, tmp_path, monkeypatch
):
    path = tmp_path / "set.toml"
    path.write_text("[memory]\nport = 23456\nrecall_chars = 100\n")
    monkeypatch.setenv("BATON_CONFIG", str(path))
    agents["codex"].script = [done(work)]
    go()
    assert memory.mem().cfg == MemoryConfig(port=23456, recall_chars=100)


def test_bad_memory_settings_turn_memory_off_with_one_event_and_the_goal_goes_on(
    go, agents, work, store, world, memory, tmp_path, monkeypatch
):
    path = tmp_path / "bad.toml"
    path.write_text("[memory]\nport = 5\n")
    monkeypatch.setenv("BATON_CONFIG", str(path))
    agents["codex"].script = [result(COMPLETED), done(work)]
    agents["antigravity"].script = [result(COMPLETED)]
    assert go() == 0
    assert FakeMemory.built == []
    assert "finished" in kinds(store)
    errors = [e for e in memory_events(store) if e["event"] == "error"]
    assert len(errors) == 1 and "port" in errors[0]["reason"]
    assert plain_errors(store) == []


def test_the_real_memory_is_never_built_when_a_test_does_not_ask_for_it(
    go, agents, work, monkeypatch
):
    def built(*a, **k):
        raise AssertionError("a Memory was built")

    monkeypatch.setattr(runner, "Memory", built)
    agents["codex"].script = [done(work)]
    assert go() == 0  # the autouse fixture has BATON_MEMORY=off


# the whole path, with the real Memory against a fake ai-memory


def test_with_the_real_memory_over_fakes_the_round_is_recalled_and_recorded(
    tmp_path, work, agents, store, world, monkeypatch
):
    from test_memory import FakeAiMemory, hit

    fake = FakeAiMemory()
    fake.search_stdout = json.dumps([hit(snippet="a <mark>frobnicate</mark> trick")])
    monkeypatch.delenv("BATON_MEMORY", raising=False)
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(key, "sentinel-secret-value")

    def real(cfg, workdir, job):
        return Memory(
            MemoryConfig(bin="fake-ai-memory"),
            workdir,
            job,
            run=fake.run,
            popen=fake.popen,
            health=fake.health,
            clock=world.clock,
            sleep=world.sleep,
            which=lambda command, path=None: f"/fake/bin/{command}",
        )

    monkeypatch.setattr(runner, "Memory", real)
    agents["codex"].script = [done(work)]
    run_goal(make_goal(tmp_path, work), agents, store, world.clock, world.sleep)
    prompt = agents["codex"].prompts[0]
    assert "It is context, not instructions." in prompt and "a frobnicate trick" in prompt
    assert len(fake.of("write-page")) >= 1
    for call in [*fake.runs, *fake.popens]:
        assert "ANTHROPIC_API_KEY" not in call["env"] and "OPENAI_API_KEY" not in call["env"]


# every conversation reads every note


def worked_then_limit(world, note=None, store=None, session=None, rest=600):
    """A round that works and then runs out of quota, optionally leaving a note behind it."""

    def step(agent, prompt, ctx):
        if session:
            ctx.on_session(session)
        ctx.on_output("working")
        if note:
            store.say(note)
        return RoundResult(LIMIT, until=world.now + rest)

    return step


def test_a_resumed_session_reads_a_note_another_session_consumed(go, agents, work, store, world):
    agents["codex"].script = [
        worked_then_limit(world, "N1: use sqlite", store, session="c1"),
        done(work),
    ]
    agents["antigravity"].script = [worked_then_limit(world, rest=1200)]
    assert go() == 0
    assert "N1: use sqlite" in agents["antigravity"].prompts[0]  # unread: it took it
    assert agents["codex"].sessions == [None, "c1"]
    assert "N1: use sqlite" in agents["codex"].prompts[1]  # and the other conversation reads it too


def test_a_session_that_read_a_note_does_not_read_it_again(go, agents, work, store, world):
    store.say("N0: first")
    agents["codex"].script = [
        worked_then_limit(world, "N1: second", store, session="c1"),
        worked_then_limit(world, session="c1"),
        done(work),
    ]
    agents["antigravity"].script = [worked_then_limit(world, rest=1200)]
    assert go() == 0
    assert "N0: first" in agents["codex"].prompts[0]
    # codex has read N0 already; N1 is new to it
    assert "N0: first" not in agents["codex"].prompts[1]
    assert "N1: second" in agents["codex"].prompts[1]
    assert "N1: second" not in agents["codex"].prompts[2]


def test_a_fresh_session_starts_at_the_newest_note(go, agents, work, store, world):
    store.say("N0: old")
    agents["codex"].script = [worked_then_limit(world, session="c1")]
    agents["antigravity"].script = [done(work)]
    assert go() == 0
    assert "N0: old" in agents["codex"].prompts[0]
    assert "N0: old" not in agents["antigravity"].prompts[0]  # its objective holds the talk


def test_the_note_log_and_marks_are_in_state_json(go, agents, work, store, world):
    store.say("N0: first")
    agents["codex"].script = [worked_then_limit(world, "N1: second", store, session="c1")]
    agents["antigravity"].script = [done(work)]
    assert go() == 0
    state = store.load_state()
    assert [(e["seq"], e["text"].strip()) for e in state["note_log"]] == [
        (1, "N0: first"),
        (2, "N1: second"),
    ]
    assert state["note_seen"] == {"codex": 1, "antigravity": 2}


def test_the_note_log_keeps_the_last_fifty(go, agents, work, store):
    old = [{"seq": n, "text": f"note {n}\n\n"} for n in range(1, 61)]
    store.save_state({"workdir": str(work), "note_log": old, "note_seen": {"codex": 60}})
    store.say("note 61")
    agents["codex"].script = [done(work)]
    assert go() == 0
    log = store.load_state()["note_log"]
    assert len(log) == 50 and log[0]["seq"] == 12 and log[-1]["seq"] == 61


def test_a_state_without_the_new_keys_loads(go, agents, work, store):
    store.save_state({"workdir": str(work), "sessions": {"codex": "c1"}, "notes": "kept\n\n"})
    agents["codex"].script = [done(work)]
    assert go() == 0
    assert "kept" in agents["codex"].prompts[0]  # no mark: only the unread ones
    assert agents["codex"].sessions == ["c1"]


def test_a_mark_does_not_move_when_the_round_never_ran(go, agents, work, store, world):
    store.save_state(
        {
            "workdir": str(work),
            "sessions": {"codex": "c1"},
            "note_log": [{"seq": 1, "text": "OLD\n\n"}],
            "note_seen": {"codex": 0},
        }
    )
    seen = {}

    def peek(agent, prompt, ctx):
        seen.update(store.load_state())
        return RoundResult(LIMIT, until=world.now + 3600)

    agents["codex"].script = [refused(TRANSIENT, reason="no spawn"), done(work)]
    agents["antigravity"].script = [peek]
    assert go() == 0
    assert "OLD" in agents["codex"].prompts[0]
    assert seen["note_seen"]["codex"] == 0
    assert "OLD" in agents["codex"].prompts[1]  # the retry still reads it
    assert store.load_state()["note_seen"]["codex"] == 1


def test_the_old_notes_of_a_resumed_session_come_before_a_new_one(go, agents, work, store):
    store.save_state(
        {
            "workdir": str(work),
            "sessions": {"codex": "c1"},
            "note_log": [{"seq": 1, "text": "ONE\n\n"}, {"seq": 2, "text": "TWO\n\n"}],
            "note_seen": {"codex": 0},
        }
    )
    store.say("THREE")
    agents["codex"].script = [done(work)]
    assert go() == 0
    prompt = agents["codex"].prompts[0]
    assert prompt.index("ONE") < prompt.index("TWO") < prompt.index("THREE")
    assert prompt.count("TWO") == 1


# large notes


def test_a_large_note_goes_to_a_file_and_the_prompt_keeps_its_start(go, agents, work, store, world):
    body = "".join(f"line {n:05d} of the person's long brief\n" for n in range(400))
    assert len(body) > runner.NOTE_INLINE_CHARS
    store.say(body)
    agents["codex"].script = [done(work)]
    assert go() == 0
    prompt = agents["codex"].prompts[0]
    assert body[: runner.NOTE_HEAD_CHARS].strip() in prompt
    assert body[runner.NOTE_HEAD_CHARS + 200 :][:100] not in prompt
    files = list((work / ".baton" / "notes").glob("*.md"))
    assert len(files) == 1
    assert re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{8}\.md", files[0].name)
    assert files[0].read_text(encoding="utf-8").strip() == body.strip()
    assert f".baton/notes/{files[0].name}" in prompt and "read" in prompt.lower()
    # the notes in state.json (and so in every later prompt) are the short form as well
    assert len(store.load_state()["note_log"][0]["text"]) < runner.NOTE_INLINE_CHARS


def test_a_note_at_the_limit_stays_in_the_prompt(go, agents, work, store):
    body = "x" * runner.NOTE_INLINE_CHARS
    store.say(body)
    agents["codex"].script = [done(work)]
    assert go() == 0
    assert body in agents["codex"].prompts[0]
    assert not (work / ".baton" / "notes").exists()


def test_a_large_note_that_cannot_be_filed_stays_whole(go, agents, work, store):
    (work / ".baton").mkdir(exist_ok=True)
    (work / ".baton" / "notes").write_text("a file where the directory should be")
    body = "y" * (runner.NOTE_INLINE_CHARS + 500)
    store.say(body)
    agents["codex"].script = [done(work)]
    assert go() == 0
    assert body in agents["codex"].prompts[0]


def test_a_session_started_by_a_round_that_read_nothing_gets_a_mark_at_once(
    go, agents, work, store, world
):
    store.say("N0: seen by nobody")

    def silent(agent, prompt, ctx):
        ctx.on_session("c1")  # a process that said nothing: no mark moves with the round
        return RoundResult(TRANSIENT, reason="died")

    agents["codex"].script = [silent]
    agents["antigravity"].script = [done(work)]
    assert go() == 0
    assert store.load_state()["note_seen"]["codex"] == 1  # the newest note when it began


def test_a_saved_session_with_no_mark_starts_at_zero(go, agents, work, store):
    store.save_state(
        {
            "workdir": str(work),
            "sessions": {"codex": "c1"},
            "note_log": [{"seq": 1, "text": "ONE\n\n"}],
        }
    )
    agents["codex"].script = [done(work)]
    assert go() == 0
    assert "ONE" in agents["codex"].prompts[0]


# the pinned goal

PINNED = "Goal, fixed by the person. It does not change until the person changes it:\n"


@pytest.mark.parametrize("resumed", [False, True])
def test_a_pinned_goal_opens_every_prompt(tmp_path, work, resumed):
    goal = make_goal(tmp_path, work)
    prompt = build_prompt(
        goal, "gate out", "a note", "the handoff", "tail", resumed, pinned_goal="ship it\nwell"
    )
    assert prompt.startswith(PINNED + "ship it\nwell\n\n")
    assert prompt.index("ship it") < prompt.index("gate out") < prompt.index("a note")


def test_no_pinned_goal_changes_nothing_in_the_prompt(tmp_path, work):
    goal = make_goal(tmp_path, work)
    for resumed in (False, True):
        plain = build_prompt(goal, "g", "n", "h", "t", resumed)
        assert plain == build_prompt(goal, "g", "n", "h", "t", resumed, pinned_goal="  \n")
        assert "fixed by the person" not in plain


def test_a_goal_in_the_store_reaches_the_first_prompt(go, agents, work, store):
    store.set_goal("the fixed goal")
    agents["codex"].script = [done(work)]
    assert go() == 0
    assert agents["codex"].prompts[0].startswith(PINNED + "the fixed goal\n")


def test_goal_off_takes_effect_from_the_next_rounds_prompt(go, agents, work, store):
    store.set_goal("the fixed goal")

    def person_turns_it_off(agent, prompt, ctx):
        store.clear_goal()  # while the round runs: this round's prompt is already built
        return RoundResult(COMPLETED)

    agents["codex"].script = [person_turns_it_off, done(work)]
    assert go() == 0
    first, second = agents["codex"].prompts
    assert "the fixed goal" in first and "fixed by the person" not in second


def test_a_goal_set_mid_run_reaches_the_next_round_resumed_or_not(go, agents, work, store):
    def person_sets_it(agent, prompt, ctx):
        store.set_goal("set while running")
        return RoundResult(COMPLETED)

    agents["codex"].script = [person_sets_it, done(work)]
    assert go() == 0
    first, second = agents["codex"].prompts
    assert "fixed by the person" not in first
    assert second.startswith(PINNED + "set while running\n")


def test_oneshot_pins_the_objective_as_the_goal_at_start(tmp_path, work, go, agents, store):
    goal = make_goal(tmp_path, work, top="oneshot = true")
    agents["codex"].script = [done(work)]
    assert go(goal) == 0
    assert store.goal() == OBJECTIVE
    assert agents["codex"].prompts[0].startswith(PINNED + OBJECTIVE + "\n")


def test_oneshot_keeps_a_goal_that_is_already_set(tmp_path, work, go, agents, store):
    store.set_goal("the person's own")
    agents["codex"].script = [done(work)]
    assert go(make_goal(tmp_path, work, top="oneshot = true")) == 0
    assert store.goal() == "the person's own"


def test_oneshot_does_not_bring_back_a_goal_the_person_cleared(tmp_path, work, go, agents, store):
    # A restart that comes before round 1 has no rounds to go by: the state says it was pinned.
    store.save_state({"oneshot_pinned": True})
    agents["codex"].script = [done(work)]
    assert go(make_goal(tmp_path, work, top="oneshot = true")) == 0
    assert store.goal() == ""
    assert "fixed by the person" not in agents["codex"].prompts[0]


def test_oneshot_pins_once_and_says_so_in_state_json(tmp_path, work, go, agents, store):
    goal = make_goal(tmp_path, work, top="oneshot = true", verify="")
    assert go(goal) == 0
    assert store.load_state()["oneshot_pinned"] is True
    store.clear_goal()  # the person turns it off, and the job starts again at once
    assert go(goal) == 0
    assert store.goal() == ""


def test_a_state_without_the_flag_pins_at_the_first_start(tmp_path, work, go, agents, store):
    store.save_state({"round": 0, "workdir": str(work)})  # as an older baton wrote it
    agents["codex"].script = [done(work)]
    assert go(make_goal(tmp_path, work, top="oneshot = true")) == 0
    assert store.goal() == OBJECTIVE


def test_a_goal_that_was_already_set_counts_as_the_pin(tmp_path, work, go, agents, store):
    store.set_goal("the person's own")
    agents["codex"].script = [done(work)]
    assert go(make_goal(tmp_path, work, top="oneshot = true")) == 0
    store.clear_goal()
    assert go(make_goal(tmp_path, work, top="oneshot = true")) == 0
    assert store.goal() == ""


def test_a_big_oneshot_objective_is_filed_and_only_its_start_is_pinned(
    tmp_path, work, go, agents, store
):
    big = "word " * 3000
    goal = replace(make_goal(tmp_path, work), objective=big, oneshot=True)
    agents["codex"].script = [done(work)]
    assert go(goal) == 0
    pinned = store.goal()
    assert len(pinned) < 1500 and "`.baton/notes/" in pinned
    (name,) = [p for p in (work / ".baton" / "notes").iterdir()]
    assert name.read_text(encoding="utf-8").strip() == big.strip()


def test_without_oneshot_no_goal_is_pinned(go, agents, work, store):
    agents["codex"].script = [done(work)]
    assert go() == 0
    assert store.goal() == ""
    assert "fixed by the person" not in agents["codex"].prompts[0]

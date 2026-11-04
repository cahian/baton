import os
import subprocess
import threading
import time

import pytest

from baton import runner
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
from baton.config import load_goal
from baton.proc import process_start
from baton.runner import build_prompt, run_gate, run_goal, snapshot_work
from baton.store import (
    AlreadyRunning,
    StateError,
    Store,
    WorkdirBusy,
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

    def __init__(self, name):
        self.name = name
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


def make_goal(tmp_path, work, verify="test -f DONE", snapshot=False, verify_extra=""):
    lines = [
        'name = "demo"',
        f'workdir = "{work}"',
        f'objective = "{OBJECTIVE}"',
        f"snapshot = {str(snapshot).lower()}",
        'order = ["codex", "antigravity"]',
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


def test_a_kill_after_the_save_does_not_repeat_the_note(go, agents, work, store):
    (work / "DONE").write_text("x")
    store.say("said once")
    store.save_state({"notes": "said once\n\n"})  # saved, and the inbox was never deleted
    go()
    assert not (store.root / "inbox.md").exists()
    assert sum(p.count("said once") for p in sent(agents)) == 1


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

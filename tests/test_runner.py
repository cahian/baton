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
from baton.runner import build_prompt, run_gate, run_goal, snapshot_work
from baton.store import AlreadyRunning, StateError, Store

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

    def flaky(goal):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return real(goal)

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


def test_stray_process_group_is_killed_on_start(go, agents, work, store):
    stray = subprocess.Popen(["sleep", "60"], start_new_session=True)
    # In real life the stray is nobody's child; reap it here so it does not linger as a zombie.
    threading.Thread(target=stray.wait, daemon=True).start()
    try:
        store.save_state({"group": stray.pid, "workdir": str(work)})
        (work / "DONE").write_text("x")
        assert go() == 0
        assert stray.wait(timeout=15) is not None
        assert store.load_state()["group"] is None
    finally:
        if stray.poll() is None:
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
    for key, value in (("NAME", "Baton Tests"), ("EMAIL", "baton-tests@baton.dev")):
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


def test_snapshot_ignores_a_repo_above_the_workdir(tmp_path, repo):
    inner = repo / "sub"
    inner.mkdir()
    goal = make_goal(tmp_path, inner, snapshot=True)
    (repo / "a.txt").write_text("changed")
    assert snapshot_work(goal, 1, "codex") is False
    assert git_log(repo) == ["init"]


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

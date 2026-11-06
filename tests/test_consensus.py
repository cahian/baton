"""A goal the person set ends only when its check passes and every agent approves."""

import hashlib

import pytest
from test_phases import Agent, close, git, plan, review, step, touch
from test_runner import World, costing, make_goal, result

from baton import runner
from baton.adapters.base import TIMEOUT, TRANSIENT, Availability
from baton.gateway import render_event
from baton.runner import run_goal
from baton.store import Store

pytestmark = pytest.mark.phases

GOAL = "ship it"


@pytest.fixture
def work(tmp_path, monkeypatch):
    for key, value in (("NAME", "Baton Tests"), ("EMAIL", "baton-tests@baton.invalid")):
        monkeypatch.setenv(f"GIT_AUTHOR_{key}", value)
        monkeypatch.setenv(f"GIT_COMMITTER_{key}", value)
    path = tmp_path / "work"
    path.mkdir()
    git(path, "init", "-q")
    (path / "a.txt").write_text("1")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


@pytest.fixture
def world():
    return World()


@pytest.fixture
def store(tmp_path):
    st = Store(tmp_path / "state")
    st.set_goal(GOAL)
    return st


@pytest.fixture
def agents():
    return {"codex": Agent("codex"), "antigravity": Agent("antigravity")}


@pytest.fixture
def go(tmp_path, work, store, agents, world):
    def run(goal=None, adapters=None, sleep=None):
        return run_goal(
            goal or make_goal(tmp_path, work),
            adapters or agents,
            store,
            world.clock,
            sleep or world.sleep,
        )

    return run


def approve(work):
    return step(review(work, "APPROVED\nit is met"))


def reject(work, text="CHANGES\nthe fps is rounded"):
    return step(review(work, text))


def check(work, text):
    def do():
        (work / ".baton").mkdir(exist_ok=True)
        (work / ".baton" / "VERIFY.sh").write_text(text)

    return do


def stop(store):
    return lambda: store.request_stop()


def events(store, kind):
    return [e for e in store.events() if e["kind"] == kind]


def notices(store):
    return [e["message"] for e in events(store, "notice")]


def consensus_rounds(store):
    return [e["agent"] for e in events(store, "round_start") if e.get("consensus")]


def waiting_for_an_agent(store):
    return any(e["reason"].startswith("waiting for") for e in events(store, "wait"))


def last_event(store):
    return store.events()[-1]


# a passed gate starts consensus


def test_a_passed_gate_starts_consensus_and_every_agent_approving_ends_the_goal(
    go, agents, work, store
):
    codex, agy = agents["codex"], agents["antigravity"]
    codex.script = [step(touch(work)), approve(work)]
    agy.script = [approve(work)]
    assert go() == 0
    assert (
        last_event(store)["kind"] == "finished" and last_event(store)["reason"] == "goal achieved"
    )
    assert consensus_rounds(store) == ["codex", "antigravity"]
    assert notices(store) == ["goal achieved: check passed, approved by codex, antigravity"]
    # a strong, fresh round for each, with the phase's own instructions
    assert codex.tiers == ["strong", "strong"] and agy.tiers == ["strong"]
    assert codex.sessions == [None, None] and agy.sessions == [None]
    assert "Phase: CONSENSUS" in codex.prompts[1] and "Phase: CONSENSUS" in agy.prompts[0]
    assert "Phase: CONSENSUS" not in codex.prompts[0]
    # the goal is done and archived, and nothing of consensus is left in the state
    assert store.goal() == ""
    assert (store.root / "goal.done.md").read_text().strip() == GOAL
    state = store.load_state()
    assert state["consensus"] is None and state["phase"]["phase"] == "plan"
    # it did not move the queue
    assert state["current"] == "codex" and state["turn"]["agent"] == "codex"
    assert "rotation" not in [e["kind"] for e in store.events()]


def test_the_gate_before_round_one_passing_starts_consensus_not_the_end(go, agents, work, store):
    (work / "DONE").write_text("x")
    agents["codex"].script = [approve(work)]
    agents["antigravity"].script = [approve(work)]
    assert go() == 0
    assert consensus_rounds(store) == ["codex", "antigravity"]
    assert last_event(store)["reason"] == "goal achieved"
    # to start consensus, and once more to end it
    assert [g["passed"] for g in events(store, "gate")] == [True, True]


@pytest.mark.parametrize("goal_set", [True, False])
def test_the_periodic_gate_passing_starts_consensus_while_a_goal_is_set(
    go, agents, work, store, goal_set
):
    if not goal_set:
        store.clear_goal()
    codex, agy = agents["codex"], agents["antigravity"]
    codex.script = [step(touch(work), outcome=TIMEOUT)] * 3 + [approve(work)]
    agy.script = [approve(work)]
    assert go() == 0
    if goal_set:
        assert last_event(store)["reason"] == "goal achieved"
        assert consensus_rounds(store) == ["codex", "antigravity"]
    else:  # no goal: as ever, the pass finishes
        assert last_event(store)["reason"] == "verify passed"
        assert consensus_rounds(store) == [] and len(codex.prompts) == 3


def test_without_a_goal_a_passed_gate_finishes_as_always(go, agents, work, store):
    store.clear_goal()
    agents["codex"].script = [step(touch(work))]
    assert go() == 0
    assert last_event(store)["reason"] == "verify passed"
    assert agents["antigravity"].prompts == [] and len(agents["codex"].prompts) == 1
    assert events(store, "notice") == [] and store.load_state().get("consensus") is None


def test_without_a_goal_and_without_a_check_the_cycle_ends_the_run(
    go, agents, work, store, tmp_path
):
    store.clear_goal()
    assert go(make_goal(tmp_path, work, verify=None)) == 0
    assert last_event(store)["reason"] == "round completed"
    assert not (work / ".baton" / "VERIFY.sh").exists()


# a rejection goes back to build


def test_one_agent_rejecting_ends_consensus_and_the_review_goes_back_to_build(
    go, agents, work, store
):
    codex, agy = agents["codex"], agents["antigravity"]
    codex.script = [step(touch(work)), approve(work), step(close(work)), approve(work)]
    agy.script = [reject(work), approve(work), approve(work)]
    assert go() == 0
    assert "antigravity rejected: back to build" in notices(store)
    # the second consensus asks everyone again, from the top
    assert consensus_rounds(store) == ["codex", "antigravity", "codex", "antigravity"]
    assert last_event(store)["reason"] == "goal achieved"
    # a task for the plan, and the review as a note the builder reads
    assert "Phase: BUILD" in codex.prompts[2]
    assert "the fps is rounded" in codex.prompts[2] and "antigravity" in codex.prompts[2]
    assert "REVIEW.md" in (work / ".baton" / "PLAN.md").read_text()
    assert store.load_state()["failed_cycles"] == 1
    assert "approved by codex, antigravity" in notices(store)[-1]


def test_a_consensus_round_that_changes_the_tree_is_a_rejection(go, agents, work, store):
    codex, agy = agents["codex"], agents["antigravity"]
    codex.script = [step(touch(work)), approve(work), step(stop(store))]
    agy.script = [step(lambda: (work / "a.txt").write_text("edited"), review(work, "APPROVED"))]
    assert go() == 0
    assert "antigravity rejected: back to build" in notices(store)
    assert "Review the changes made during consensus" in (work / ".baton" / "PLAN.md").read_text()
    assert "the code changed" in codex.prompts[2] and "a.txt" in codex.prompts[2]
    assert "APPROVED" in codex.prompts[2]  # what the void round wrote is in the note
    assert (work / "a.txt").read_text() == "edited"  # left where it is: it is a task to review
    assert store.load_state()["phase"]["phase"] == "build"


def test_a_consensus_round_that_leaves_no_verdict_is_a_rejection(go, agents, work, store):
    codex, agy = agents["codex"], agents["antigravity"]
    codex.script = [step(touch(work)), step(), step(stop(store))]
    assert go() == 0
    assert "codex rejected: back to build" in notices(store)
    assert agy.prompts == []  # consensus ended with the first no


def test_a_round_that_does_not_complete_stays_pending_and_is_retried(go, agents, work, store):
    codex, agy = agents["codex"], agents["antigravity"]
    codex.script = [
        step(touch(work)),
        step(review(work, "APPROVED"), outcome=TIMEOUT),  # not complete: nothing counts
        approve(work),
    ]
    agy.script = [approve(work)]
    assert go() == 0
    assert consensus_rounds(store) == ["codex", "codex", "antigravity"]
    assert last_event(store)["reason"] == "goal achieved"


# the people who can run


def test_an_agent_out_of_quota_is_waited_for(go, agents, work, store, world):
    codex, agy = agents["codex"], agents["antigravity"]
    ready = world.now + 3000
    agy.avail = Availability(False, until=ready, reason="weekly limit")

    def sleep(seconds):
        world.sleep(seconds)
        if world.now >= ready:
            agy.avail = Availability(True)

    codex.script = [step(touch(work)), approve(work)]
    agy.script = [approve(work)]
    assert go(sleep=sleep) == 0
    waits = [e for e in events(store, "wait") if e["reason"] != "the last round ended at once"]
    assert waits[0]["reason"] == "waiting for antigravity to approve"
    assert waits[0]["until"] >= ready - 5
    assert sum(world.sleeps) >= 3000
    assert last_event(store)["reason"] == "goal achieved"
    assert "[baton] waiting" in render_event("wait", waits[0])


def test_an_agent_out_of_quota_with_no_known_reset_is_waited_for_too(
    go, agents, work, store, world
):
    codex, agy = agents["codex"], agents["antigravity"]
    ready = world.now + 1000
    agy.avail = Availability(False, reason="limit", usage={"weekly": "100%"})

    def sleep(seconds):
        world.sleep(seconds)
        if world.now >= ready:
            agy.avail = Availability(True)

    codex.script = [step(touch(work)), approve(work)]
    agy.script = [approve(work)]
    assert go(sleep=sleep) == 0
    assert sum(world.sleeps) >= 1000
    assert last_event(store)["reason"] == "goal achieved"


def test_an_agent_that_can_never_run_is_left_out_of_consensus(tmp_path, work, store, agents, world):
    agents["claude_api"] = Agent("claude_api", metered=True)
    agents["claude_api"].avail = Availability(False, reason="no key file", permanent=True)
    codex, agy = agents["codex"], agents["antigravity"]
    codex.script = [step(touch(work)), approve(work)]
    agy.script = [approve(work)]
    goal = make_goal(tmp_path, work, order='"codex", "antigravity", "claude_api"')
    assert run_goal(goal, agents, store, world.clock, world.sleep) == 0
    assert consensus_rounds(store) == ["codex", "antigravity"]
    assert agents["claude_api"].prompts == []
    assert "approved by codex, antigravity" in notices(store)[-1]


def test_an_agent_that_stops_being_able_to_run_during_consensus_leaves_it(
    tmp_path, work, store, agents, world
):
    agents["claude_api"] = Agent("claude_api", metered=True)
    codex, agy, claude = agents["codex"], agents["antigravity"], agents["claude_api"]

    def key_goes_away(agent, prompt, ctx):
        claude.avail = Availability(False, reason="no key file", permanent=True)
        return approve(work)(agent, prompt, ctx)

    codex.script = [step(touch(work)), approve(work)]
    agy.script = [key_goes_away]
    goal = make_goal(tmp_path, work, order='"codex", "antigravity", "claude_api"')
    assert run_goal(goal, agents, store, world.clock, world.sleep) == 0
    assert claude.prompts == []
    assert last_event(store)["reason"] == "goal achieved"


GOAL_CAP = "\n[agents.claude_api]\ngoal_budget_usd = 150\n"


def test_an_agent_dropped_for_the_goal_budget_before_consensus_is_not_asked(
    tmp_path, work, store, agents, world
):
    agents["claude_api"] = Agent("claude_api", metered=True)
    claude, codex = agents["claude_api"], agents["codex"]
    claude.script = [costing(160.0, then=step(touch(work)))]
    codex.script = [approve(work)]
    goal = make_goal(tmp_path, work, order='"claude_api", "codex"', top=GOAL_CAP)
    assert run_goal(goal, agents, store, world.clock, world.sleep) == 0
    assert len(claude.prompts) == 1  # its plan round, and no consensus round
    assert consensus_rounds(store) == ["codex"]
    assert "approved by codex" in notices(store)[-1] and "claude_api" not in notices(store)[-1]


def test_an_agent_crossing_the_goal_budget_during_consensus_leaves_pending(
    tmp_path, work, store, agents, world
):
    agents["claude_api"] = Agent("claude_api", metered=True)
    claude, codex = agents["claude_api"], agents["codex"]
    codex.script = [step(touch(work)), approve(work)]
    claude.script = [costing(160.0, then=result(TRANSIENT))]  # costs, and does not complete
    goal = make_goal(tmp_path, work, order='"codex", "claude_api"', top=GOAL_CAP)
    assert run_goal(goal, agents, store, world.clock, world.sleep) == 0
    assert consensus_rounds(store) == ["codex", "claude_api"]
    assert "claude_api" in store.load_state()["dropped"]
    assert any("claude_api" in e["message"] for e in events(store, "alert"))
    assert last_event(store)["reason"] == "goal achieved"
    assert notices(store)[-1] == "goal achieved: check passed, approved by codex"


def test_consensus_rounds_cost_goes_to_the_goal_and_not_to_the_turn(
    tmp_path, work, store, agents, world
):
    agents["claude_api"] = Agent("claude_api", metered=True)
    codex, claude = agents["codex"], agents["claude_api"]
    codex.script = [step(touch(work)), costing(7.0, then=approve(work))]
    claude.script = [approve(work)]
    goal = make_goal(tmp_path, work, order='"codex", "claude_api"')
    assert run_goal(goal, agents, store, world.clock, world.sleep) == 0
    state = store.load_state()
    assert state["agents"]["codex"]["goal_spent"] == 7.0
    assert state["turn"]["spent"] == 0


def test_a_consensus_round_does_not_announce_a_rotation_that_waits(
    tmp_path, work, store, agents, world
):
    codex, agy = agents["codex"], agents["antigravity"]

    def plan_round(agent, prompt, ctx):
        world.now += 100  # codex's turn (10 s) is over
        return step(touch(work))(agent, prompt, ctx)

    codex.script = [plan_round, approve(work)]
    agy.script = [approve(work)]
    goal = make_goal(tmp_path, work, top="\n[agents.codex]\nturn_max_s = 10\n")
    assert run_goal(goal, agents, store, world.clock, world.sleep) == 0
    assert [e["kind"] for e in store.events()].count("rotation") == 0
    starts = events(store, "round_start")
    assert all(e["previous"] is None for e in starts if e.get("consensus"))
    assert "[baton] round 2: codex (consensus)" in render_event("round_start", starts[1])


# the person


def test_a_message_during_consensus_ends_it_and_goes_to_build(go, agents, work, store, world):
    codex, agy = agents["codex"], agents["antigravity"]
    agy.avail = Availability(False, until=world.now + 100_000, reason="weekly limit")
    said = []

    def sleep(seconds):
        world.sleep(seconds)
        if not said and waiting_for_an_agent(store):
            said.append(True)
            store.say("use the real device")

    codex.script = [step(touch(work)), approve(work), step(stop(store))]
    assert go(sleep=sleep) == 0
    assert sum(world.sleeps) < 200  # it woke, it did not wait out the quota
    assert agy.prompts == []
    assert "Phase: BUILD" in codex.prompts[2] and "use the real device" in codex.prompts[2]
    assert "Do what the person's message asks" in (work / ".baton" / "PLAN.md").read_text()
    assert store.load_state()["consensus"] is None
    assert store.load_state().get("failed_cycles", 0) == 0  # not a rejection
    assert any("back to build" in m for m in notices(store))
    assert "goal achieved" not in " ".join(notices(store))


def test_a_message_while_the_agent_is_picked_ends_consensus_and_no_approval_counts(
    go, agents, work, store
):
    codex = agents["codex"]
    said = []

    def availability():
        if not said and (store.load_state().get("consensus") or {}).get("pending"):
            said.append(True)
            store.say("use the real device")  # after the check for notes, before the round
        return Availability(True)

    codex.availability = availability
    codex.script = [step(touch(work)), step(stop(store))]
    assert go() == 0
    assert said and consensus_rounds(store) == []  # no approver was asked
    assert "Phase: BUILD" in codex.prompts[1] and "use the real device" in codex.prompts[1]
    assert store.load_state()["consensus"] is None
    assert store.load_state().get("failed_cycles", 0) == 0
    assert any("back to build" in m for m in notices(store))
    assert "goal achieved" not in " ".join(notices(store))


def test_a_tracked_change_during_the_final_check_rejects_consensus(
    go, agents, work, store, monkeypatch
):
    codex, agy = agents["codex"], agents["antigravity"]
    original = runner._Run.run_check

    def run_check(self, with_goal):
        if self.consensus is not None:
            (work / "a.txt").write_text("changed during check")
        return original(self, with_goal)

    monkeypatch.setattr(runner._Run, "run_check", run_check)
    codex.script = [step(touch(work)), approve(work), step(stop(store))]
    agy.script = [approve(work)]
    assert go() == 0
    assert store.goal() == GOAL
    assert "goal achieved" not in " ".join(notices(store))
    assert store.load_state()["phase"]["phase"] == "build"
    assert "Review the changes made during consensus" in (work / ".baton" / "PLAN.md").read_text()


def test_a_message_during_the_final_check_ends_consensus_instead_of_the_goal(
    go, agents, work, store, monkeypatch
):
    codex, agy = agents["codex"], agents["antigravity"]
    said = []
    original = runner._Run.run_check

    def run_check(self, with_goal):
        found = original(self, with_goal)
        if self.consensus is not None and not said:
            said.append(True)
            store.say("one more thing")
        return found

    monkeypatch.setattr(runner._Run, "run_check", run_check)
    codex.script = [step(touch(work)), approve(work), step(stop(store))]
    agy.script = [approve(work)]
    assert go() == 0
    assert said and "goal achieved" not in " ".join(notices(store))
    assert any("back to build" in m for m in notices(store))
    assert "Phase: BUILD" in codex.prompts[2] and "one more thing" in codex.prompts[2]
    assert store.goal() == GOAL and store.load_state()["consensus"] is None
    assert store.load_state().get("failed_cycles", 0) == 0


def test_next_during_consensus_rotates_nothing_and_is_dropped_with_a_line(
    go, agents, work, store, world
):
    codex, agy = agents["codex"], agents["antigravity"]
    ready = world.now + 1000
    agy.avail = Availability(False, until=ready, reason="weekly limit")
    asked = []

    def sleep(seconds):
        world.sleep(seconds)
        if not asked and waiting_for_an_agent(store):
            asked.append(True)
            store.request_next()
        if world.now >= ready:
            agy.avail = Availability(True)

    codex.script = [step(touch(work)), approve(work)]
    agy.script = [approve(work)]
    assert go(sleep=sleep) == 0
    assert any(m.startswith("next ") and "consensus" in m for m in notices(store))
    assert not store.next_pending()
    assert "rotation" not in [e["kind"] for e in store.events()]
    assert store.load_state()["current"] == "codex"
    assert last_event(store)["reason"] == "goal achieved"
    assert agy.prompts and "Phase: CONSENSUS" in agy.prompts[0]


def test_a_stop_during_the_wait_for_an_agent_wakes_it(go, agents, work, store, world):
    agents["antigravity"].avail = Availability(False, until=world.now + 100_000, reason="limit")

    def sleep(seconds):
        world.sleep(seconds)
        store.request_stop()

    agents["codex"].script = [step(touch(work)), approve(work)]
    assert go(sleep=sleep) == 0
    assert last_event(store)["kind"] == "stopped"
    assert sum(world.sleeps) < 100


def test_nobody_able_to_approve_waits_and_does_not_end_the_goal(go, agents, work, store, world):
    codex, agy = agents["codex"], agents["antigravity"]

    def sleep(seconds):
        world.sleep(seconds)
        if world.now > codex_gone[0] + 400:
            store.request_stop()

    codex_gone = [0]

    def plan_then_vanish(agent, prompt, ctx):
        codex.avail = Availability(False, reason="disabled in the goal file", permanent=True)
        agy.avail = Availability(False, reason="disabled in the goal file", permanent=True)
        codex_gone[0] = world.now
        return step(touch(work))(agent, prompt, ctx)

    codex.script = [plan_then_vanish]
    assert go(sleep=sleep) == 0
    waits = [e for e in events(store, "wait") if e["reason"] != "the last round ended at once"]
    assert waits and "no agent can approve" in waits[-1]["reason"]
    assert last_event(store)["kind"] == "stopped" and "finished" not in [
        e["kind"] for e in store.events()
    ]


# a restart


def test_a_restart_mid_consensus_goes_on_with_who_is_left(go, agents, work, store):
    codex, agy = agents["codex"], agents["antigravity"]
    codex.script = [step(touch(work)), step(review(work, "APPROVED"), stop(store))]
    assert go() == 0
    assert last_event(store)["kind"] == "stopped"
    state = store.load_state()
    assert state["phase"]["phase"] == "consensus"
    assert state["consensus"]["pending"] == ["antigravity"]
    assert state["consensus"]["approved"] == ["codex"]
    assert state["consensus"]["tree"] and "check_hash" in state["consensus"]
    gates = len(events(store, "gate"))
    agy.script = [approve(work)]
    assert go() == 0
    assert len(codex.prompts) == 2  # codex is not asked again
    # the gate is not rerun to start it over: only the last look before the end
    assert len(events(store, "gate")) == gates + 1
    assert last_event(store)["reason"] == "goal achieved"
    assert notices(store)[-1] == "goal achieved: check passed, approved by codex, antigravity"


def test_a_restart_mid_consensus_on_a_changed_tree_rejects_instead_of_going_on(
    go, agents, work, store
):
    codex, agy = agents["codex"], agents["antigravity"]
    codex.script = [step(touch(work)), step(review(work, "APPROVED"), stop(store))]
    assert go() == 0
    (work / "DONE").unlink()  # the person broke it while baton was down
    codex.script = [step(stop(store))]
    assert go() == 0
    assert agy.prompts == []
    assert any("back to build" in m for m in notices(store))
    assert "goal achieved" not in " ".join(notices(store))
    assert store.load_state()["consensus"] is None and store.load_state()["failed_cycles"] == 1
    assert "DONE" in codex.prompts[2] and "Phase: BUILD" in codex.prompts[2]


def test_a_consensus_in_the_state_with_no_goal_set_is_dropped(go, agents, work, store):
    codex, agy = agents["codex"], agents["antigravity"]
    codex.script = [step(touch(work)), step(review(work, "APPROVED"), stop(store))]
    assert go() == 0
    store.clear_goal()  # the person turned the goal off while baton was down
    codex.script = [step(touch(work))]
    assert go() == 0
    assert store.load_state()["consensus"] is None
    assert last_event(store)["reason"] == "verify passed"
    assert agy.prompts == []


def test_old_state_files_without_the_new_keys_load(go, agents, work, store):
    store.save_state({"round": 3, "workdir": str(work), "phase": {"phase": "consensus"}})
    agents["codex"].script = [step(touch(work))]
    agents["antigravity"].script = [approve(work)]
    agents["codex"].script += [approve(work)]
    assert go() == 0  # a consensus phase with no consensus behind it starts over from plan
    state = store.load_state()
    assert state["failed_cycles"] == 0 and state["check_lock"] == ""


# no progress


def test_every_third_failed_cycle_alerts_that_baton_is_still_working(go, agents, work, store):
    codex, agy = agents["codex"], agents["antigravity"]
    codex.script = [step(), step(), step(), step(touch(work)), approve(work)]
    agy.script = [approve(work)]
    assert go() == 0
    alerts = [e["message"] for e in events(store, "alert")]
    assert alerts == ["no progress in 3 cycles: still working"]
    assert store.load_state()["failed_cycles"] == 3
    assert last_event(store)["reason"] == "goal achieved"  # it never gave up


def test_the_count_of_failed_cycles_survives_a_restart(go, agents, work, store):
    store.save_state({"failed_cycles": 2, "goal_seen": GOAL})
    agents["codex"].script = [step(), step(touch(work)), approve(work)]
    agents["antigravity"].script = [approve(work)]
    assert go() == 0
    assert [e["message"] for e in events(store, "alert")] == [
        "no progress in 3 cycles: still working"
    ]


def test_a_new_goal_starts_the_count_again(go, agents, work, store):
    store.save_state({"failed_cycles": 2, "goal_seen": "an older goal"})
    agents["codex"].script = [step(), step(touch(work)), approve(work)]
    agents["antigravity"].script = [approve(work)]
    assert go() == 0
    assert events(store, "alert") == []


# the check


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def test_the_plan_is_told_to_write_the_check_when_there_is_no_verify_command(
    go, agents, work, store, tmp_path
):
    agents["codex"].script = [step(stop(store))]
    go(make_goal(tmp_path, work, verify=None))
    prompt = agents["codex"].prompts[0]
    assert "`.baton/VERIFY.sh`" in prompt and "stand-in" in prompt
    assert "no verify command: the goal ends when a round completes" not in prompt
    assert "ends when it passes and every agent approves" in prompt


def test_a_verify_command_in_the_goal_file_is_the_check_and_the_plan_writes_no_script(
    go, agents, work, store
):
    agents["codex"].script = [step(stop(store))]
    go()
    assert "VERIFY.sh" not in agents["codex"].prompts[0]


def test_no_check_at_all_fails_the_gate_and_the_run_stays_in_plan(
    go, agents, work, store, tmp_path
):
    codex, agy = agents["codex"], agents["antigravity"]
    codex.script = [step(), step(check(work, "exit 0\n")), approve(work)]
    agy.script = [approve(work)]
    assert go(make_goal(tmp_path, work, verify=None)) == 0
    gates = events(store, "gate")
    assert [(g["passed"], g["tail"]) for g in gates] == [
        (False, "no check"),
        (True, "[exit code 0]"),
        (True, "[exit code 0]"),  # and the last look before the end
    ]
    assert "Output of the last verify run (tail):\nno check" in codex.prompts[1]
    assert codex.tiers[:2] == ["strong", "strong"]  # back in plan, not build
    assert any(e["reason"] == "verify failed: back to plan" for e in events(store, "phase"))
    assert store.load_state()["failed_cycles"] == 1
    assert last_event(store)["reason"] == "goal achieved"


def test_the_gate_before_round_one_and_the_periodic_gate_need_a_check_that_exists(
    go, agents, work, store, tmp_path
):
    codex = agents["codex"]
    codex.script = [step(outcome=TIMEOUT)] * 3 + [step(stop(store), outcome=TIMEOUT)]
    go(make_goal(tmp_path, work, verify=None))
    assert events(store, "gate") == []  # never run, so never a failure either


def test_the_script_is_the_check_while_a_goal_is_set(go, agents, work, store, tmp_path):
    codex = agents["codex"]
    codex.script = [step(check(work, "test -f DONE\n")), step(stop(store))]
    go(make_goal(tmp_path, work, verify=None))
    gate = events(store, "gate")[0]  # it ran, and failed
    assert not gate["passed"] and gate["tail"].endswith("[exit code 1]")
    (work / "DONE").write_text("x")
    store.clear_stop()
    codex.script = [approve(work)]
    agents["antigravity"].script = [approve(work)]
    go(make_goal(tmp_path, work, verify=None))
    assert last_event(store)["reason"] == "goal achieved"


def test_the_script_is_not_a_check_without_a_goal(go, agents, work, store, tmp_path):
    store.clear_goal()
    (work / ".baton").mkdir()
    (work / ".baton" / "VERIFY.sh").write_text("exit 0\n")
    assert go(make_goal(tmp_path, work, verify=None)) == 0
    assert events(store, "gate") == []
    assert last_event(store)["reason"] == "round completed"


def weakened(go, agents, work, store, tmp_path):
    """Plan writes a strict check; build weakens it and meets the weak one; plan accepts it."""
    codex, agy = agents["codex"], agents["antigravity"]
    strict = "test -f DONE && test -f STRONG\n"
    weak = "test -f DONE\n"
    codex.script = [
        step(plan(work, "build it"), check(work, strict)),
        step(close(work), check(work, weak), touch(work)),
        step(plan(work)),  # the plan accepts what is there
        approve(work),
    ]
    agy.script = [approve(work), approve(work)]  # the review, then consensus
    assert go(make_goal(tmp_path, work, verify=None)) == 0
    return strict, weak


def test_a_check_changed_outside_plan_fails_the_gate_and_alerts(go, agents, work, store, tmp_path):
    weakened(go, agents, work, store, tmp_path)
    gates = events(store, "gate")
    assert [(g["passed"], g["tail"]) for g in gates] == [
        (False, "the check changed outside plan"),
        (True, "[exit code 0]"),
        (True, "[exit code 0]"),  # and the last look before the end
    ]
    alerts = [e["message"] for e in events(store, "alert")]
    assert len(alerts) == 1 and "the check changed outside plan" in alerts[0]
    assert any(e["reason"] == "verify failed: back to plan" for e in events(store, "phase"))


def test_the_consensus_prompt_shows_the_diff_of_a_weakened_check(go, agents, work, store, tmp_path):
    strict, weak = weakened(go, agents, work, store, tmp_path)
    for prompt in (agents["codex"].prompts[-1], agents["antigravity"].prompts[-1]):
        assert "```diff" in prompt and "first version" in prompt
        assert "-test -f DONE && test -f STRONG" in prompt and "+test -f DONE" in prompt
        assert "weaker" in prompt
    assert last_event(store)["reason"] == "goal achieved"


def test_an_unchanged_check_shows_no_diff(go, agents, work, store, tmp_path):
    codex, agy = agents["codex"], agents["antigravity"]
    codex.script = [step(check(work, "test -f DONE\n"), touch(work)), approve(work)]
    agy.script = [approve(work)]
    assert go(make_goal(tmp_path, work, verify=None)) == 0
    assert "```diff" not in codex.prompts[1] and "```diff" not in agy.prompts[0]


def test_the_lock_is_saved_at_the_end_of_a_completed_plan_round_with_the_first_version(
    go, agents, work, store, tmp_path
):
    seen = []

    def spy(agent, prompt, ctx):
        state = store.load_state()
        seen.append((state["check_lock"], store.first_check()))
        return step(stop(store))(agent, prompt, ctx)

    agents["codex"].script = [step(plan(work, "a"), check(work, "exit 1\n")), spy]
    go(make_goal(tmp_path, work, verify=None))
    assert seen == [(sha("exit 1\n"), "exit 1\n")]


def test_a_plan_round_that_did_not_complete_does_not_save_the_lock(
    go, agents, work, store, tmp_path
):
    seen = []

    def spy(agent, prompt, ctx):
        seen.append((store.load_state()["check_lock"], store.first_check()))
        return step(stop(store))(agent, prompt, ctx)

    agents["codex"].script = [step(check(work, "exit 1\n"), outcome=TIMEOUT), spy]
    go(make_goal(tmp_path, work, verify=None))
    assert seen == [("", None)]


def test_a_consensus_round_that_edits_the_check_is_a_rejection(go, agents, work, store, tmp_path):
    codex = agents["codex"]
    codex.script = [
        step(check(work, "exit 0\n")),
        step(check(work, "true\n"), review(work, "APPROVED")),
        step(stop(store)),
    ]
    assert go(make_goal(tmp_path, work, verify=None)) == 0
    assert "codex rejected: back to build" in notices(store)
    assert "the check changed" in codex.prompts[2]
    assert agents["antigravity"].prompts == []


def test_a_new_goal_forgets_the_lock_and_the_first_version(go, agents, work, store, tmp_path):
    seen = []

    def spy(agent, prompt, ctx):
        state = store.load_state()
        seen.append((state["check_lock"], store.first_check(), state["goal_seen"]))
        return step(stop(store))(agent, prompt, ctx)

    agents["codex"].script = [
        step(plan(work, "a"), check(work, "exit 1\n"), lambda: store.set_goal("another goal")),
        spy,
    ]
    go(make_goal(tmp_path, work, verify=None))
    assert seen == [("", None, "another goal")]


# the chat


def test_the_chat_says_notices_as_they_are(go):
    assert render_event("notice", {"message": "antigravity rejected: back to build"}) == (
        "\n[baton] antigravity rejected: back to build\n\n"
    )


# the review's proofs, inverted: no path reaches the end without the check and every approval

import subprocess  # noqa: E402

from test_runner import say  # noqa: E402


def vandal_note(store):
    return " ".join(e["text"] for e in store.load_state()["note_log"])


def test_an_incomplete_round_that_edits_the_tree_is_a_rejection(
    tmp_path, work, store, agents, world
):
    goal = make_goal(tmp_path, work, verify="test -f DONE", snapshot=True)
    c, a = agents["codex"], agents["antigravity"]
    c.script = [step(touch(work)), approve(work), step(stop(store))]
    a.script = [step(lambda: (work / "DONE").unlink(), outcome=TIMEOUT), approve(work)]
    assert run_goal(goal, agents, store, world.clock, world.sleep) == 0
    assert "goal achieved" not in " ".join(notices(store))
    assert "antigravity rejected: back to build" in notices(store)
    assert "the code changed" in vandal_note(store) and "DONE" in vandal_note(store)
    assert store.load_state()["phase"]["phase"] == "build"


def test_an_agent_that_edits_the_tree_and_the_check_in_an_incomplete_round_and_is_left_out(
    tmp_path, work, store, agents, world
):
    goal = make_goal(tmp_path, work, verify=None, snapshot=True)
    c, a = agents["codex"], agents["antigravity"]
    c.script = [
        step(plan(work), check(work, "test -f DONE\n"), touch(work)),
        approve(work),
        step(stop(store)),
    ]

    def vandal():
        (work / "DONE").unlink()
        (work / ".baton" / "VERIFY.sh").write_text("exit 1\n")
        a.avail = Availability(False, reason="disabled now", permanent=True)

    a.script = [step(vandal, outcome=TIMEOUT)]
    assert run_goal(goal, agents, store, world.clock, world.sleep) == 0
    assert "goal achieved" not in " ".join(notices(store))
    assert "antigravity rejected: back to build" in notices(store)
    assert "the code changed" in vandal_note(store) and "the check changed" in vandal_note(store)


def test_a_resume_on_a_changed_tree_does_not_achieve_the_goal(tmp_path, work, store, agents, world):
    goal = make_goal(tmp_path, work, verify="test -f DONE", snapshot=True)
    c, a = agents["codex"], agents["antigravity"]
    c.script = [step(touch(work)), approve(work), step(stop(store))]
    a.script = [step(review(work, "APPROVED"), stop(store))]
    run_goal(goal, agents, store, world.clock, world.sleep)
    assert last_event(store)["kind"] == "stopped"
    (work / "DONE").unlink()
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "the person breaks it")
    run_goal(goal, agents, store, world.clock, world.sleep)
    assert "goal achieved" not in " ".join(notices(store))
    assert store.goal() == GOAL and not (work / "DONE").exists()


def test_an_agent_added_to_order_on_a_restart_is_asked_before_the_goal_ends(
    tmp_path, work, store, agents, world
):
    one = make_goal(tmp_path, work, verify="test -f DONE", snapshot=False, order='"codex"')
    c, a = agents["codex"], agents["antigravity"]
    c.script = [step(touch(work)), step(review(work, "APPROVED"), stop(store))]
    run_goal(one, {"codex": c}, store, world.clock, world.sleep)
    both = make_goal(tmp_path, work, verify="test -f DONE", snapshot=False)
    store.set_goal(GOAL)  # the person says the goal again: the new order is accepted
    a.script = [approve(work)]
    run_goal(both, agents, store, world.clock, world.sleep)
    assert len(a.prompts) == 1 and "Phase: CONSENSUS" in a.prompts[0]
    assert last_event(store)["reason"] == "goal achieved"
    assert notices(store)[-1] == "goal achieved: check passed, approved by codex, antigravity"


def test_a_goal_set_while_the_gate_runs_still_ends_in_consensus(tmp_path, work, world, agents):
    st = Store(tmp_path / "state")
    goal = make_goal(tmp_path, work, verify=f"echo late goal > {st.root}/goal.md")
    agents["codex"].script = [approve(work)]
    agents["antigravity"].script = [approve(work)]
    assert run_goal(goal, agents, st, world.clock, world.sleep) == 0
    assert last_event(st)["reason"] == "goal achieved"
    assert consensus_rounds(st) == ["codex", "antigravity"]
    assert st.goal() == ""  # archived with the end


def test_a_workdir_that_is_not_a_repository_never_achieves_a_goal(tmp_path, store, agents, world):
    plain = tmp_path / "plain"
    plain.mkdir()
    goal = make_goal(tmp_path, plain, verify="true")
    count = {"n": 0}

    def appr():
        count["n"] += 1
        if count["n"] == 3:
            store.request_stop()

    for agent in agents.values():
        agent.script = [step(review(plain, "APPROVED\nmet"), appr) for _ in range(12)]
    run_goal(goal, agents, store, world.clock, world.sleep)
    assert any("rejected" in m for m in notices(store))
    assert not any("achieved" in m for m in notices(store))
    assert "could not be read" in vandal_note(store)


def test_a_check_that_writes_under_baton_does_not_void_the_rerun(
    tmp_path, work, store, agents, world
):
    cmd = "mkdir -p .baton && uuidgen > .baton/report.txt"
    goal = make_goal(tmp_path, work, verify=cmd, snapshot=False)

    def rerun():
        subprocess.run(cmd, shell=True, cwd=work, check=True)

    agents["codex"].script = [step(review(work, "APPROVED\nmet"), rerun)]
    agents["antigravity"].script = [step(review(work, "APPROVED\nmet"), rerun)]
    assert run_goal(goal, agents, store, world.clock, world.sleep) == 0
    assert last_event(store)["reason"] == "goal achieved"


def test_a_check_that_writes_into_the_tree_voids_the_rerun_and_the_note_names_the_file(
    tmp_path, work, store, agents, world
):
    cmd = "uuidgen > report.txt"
    goal = make_goal(tmp_path, work, verify=cmd, snapshot=False)

    def rerun():
        subprocess.run(cmd, shell=True, cwd=work, check=True)

    agents["codex"].script = [step(review(work, "APPROVED\nmet"), rerun, stop(store))]
    run_goal(goal, agents, store, world.clock, world.sleep)
    assert "codex rejected: back to build" in notices(store)
    assert "report.txt" in vandal_note(store)


def test_leftover_gate_output_committed_by_a_snapshot_is_no_false_void(
    tmp_path, work, store, agents, world
):
    goal = make_goal(tmp_path, work, verify="echo out > out.txt", snapshot=True)
    agents["codex"].script = [approve(work)]
    agents["antigravity"].script = [approve(work)]
    assert run_goal(goal, agents, store, world.clock, world.sleep) == 0
    assert last_event(store)["reason"] == "goal achieved"
    assert "out.txt" in git(work, "ls-files")  # the snapshot did move HEAD


def test_every_consensus_prompt_carries_the_check_and_what_to_reject(
    tmp_path, work, store, agents, world
):
    goal = make_goal(tmp_path, work, verify=None, snapshot=True)
    c, a = agents["codex"], agents["antigravity"]
    c.script = [step(check(work, "exit 0\n")), approve(work)]
    a.script = [approve(work)]
    assert run_goal(goal, agents, store, world.clock, world.sleep) == 0
    for prompt in (c.prompts[1], a.prompts[0]):
        block = prompt[prompt.index("Phase: CONSENSUS") :]
        assert "The check, `.baton/VERIFY.sh`:\n```sh\nexit 0\n```" in block
        assert "cannot fail" in block and "stand-in" in block
    assert last_event(store)["reason"] == "goal achieved"


def test_a_verify_command_is_shown_to_the_approvers_too(go, agents, work, store):
    agents["codex"].script = [step(touch(work)), approve(work)]
    agents["antigravity"].script = [approve(work)]
    go()
    assert "The check, the verify command:\n```sh\ntest -f DONE\n```" in agents["codex"].prompts[1]


def test_a_stale_check_from_an_earlier_goal_is_set_aside_and_the_plan_runs_first(
    tmp_path, work, store, agents, world
):
    (work / ".baton").mkdir()
    (work / ".baton" / "VERIFY.sh").write_text("exit 0\n# the old goal's check\n")
    store.save_state({"workdir": str(work), "goal_seen": "the earlier goal"})  # not the first goal
    goal = make_goal(tmp_path, work, verify=None, snapshot=True)
    agents["codex"].script = [step(stop(store))]
    run_goal(goal, agents, store, world.clock, world.sleep)
    assert "Phase: PLAN" in agents["codex"].prompts[0]
    assert not (work / ".baton" / "VERIFY.sh").exists()
    (moved,) = (work / ".baton" / "archive").glob("*-VERIFY.sh")
    assert "the old goal's check" in moved.read_text()
    assert events(store, "gate")[0]["tail"] == "no check"


def test_a_check_there_before_the_first_goal_is_kept_and_shown_to_the_approvers(
    tmp_path, work, store, agents, world
):
    (work / ".baton").mkdir()
    (work / ".baton" / "VERIFY.sh").write_text("exit 0\n# the person's own check\n")
    goal = make_goal(tmp_path, work, verify=None, snapshot=True)
    agents["codex"].script = [approve(work)]
    agents["antigravity"].script = [approve(work)]
    assert run_goal(goal, agents, store, world.clock, world.sleep) == 0
    assert "using the existing .baton/VERIFY.sh as the goal's check" in notices(store)
    assert events(store, "gate")[0]["passed"] is True  # it was the check from the start
    assert "# the person's own check" in agents["codex"].prompts[0]
    assert "Phase: PLAN" not in agents["codex"].prompts[0]
    (moved,) = (work / ".baton" / "archive").glob("*-VERIFY.sh")  # set aside when the goal ended
    assert "the person's own check" in moved.read_text()
    assert last_event(store)["reason"] == "goal achieved"


def test_a_plan_with_tasks_but_no_check_stays_in_plan(tmp_path, work, store, agents, world):
    goal = make_goal(tmp_path, work, verify=None, snapshot=False)
    c = agents["codex"]
    c.script = [step(plan(work, "a")), step(plan(work, "a")), step(stop(store))]
    run_goal(goal, agents, store, world.clock, world.sleep)
    assert [e.get("phase") for e in events(store, "round_end")][:3] == ["plan", "plan", "plan"]
    assert not (work / ".baton" / "VERIFY.sh").exists()
    assert c.tiers == ["strong"] * 3


def test_a_plan_that_writes_the_check_with_tasks_goes_on_to_build(
    tmp_path, work, store, agents, world
):
    goal = make_goal(tmp_path, work, verify=None, snapshot=False)
    c = agents["codex"]
    c.script = [step(plan(work, "a"), check(work, "exit 1\n")), step(stop(store))]
    run_goal(goal, agents, store, world.clock, world.sleep)
    assert [e.get("phase") for e in events(store, "round_end")] == ["plan", "build"]


# the goal ends: the check is set aside, and the last look comes just before


def test_the_end_of_a_goal_sets_the_check_aside_and_forgets_it(go, agents, work, store, tmp_path):
    codex, agy = agents["codex"], agents["antigravity"]
    codex.script = [step(check(work, "exit 0\n")), approve(work)]
    agy.script = [approve(work)]
    assert go(make_goal(tmp_path, work, verify=None)) == 0
    assert last_event(store)["reason"] == "goal achieved"
    assert not (work / ".baton" / "VERIFY.sh").exists()
    assert len(list((work / ".baton" / "archive").glob("*-VERIFY.sh"))) == 1
    assert store.load_state()["check_lock"] == "" and store.first_check() is None


def test_a_new_goal_sets_the_old_check_aside(go, agents, work, store, tmp_path):
    codex = agents["codex"]

    def new_goal(agent, prompt, ctx):
        store.set_goal("another goal")
        return step(plan(work, "a"), check(work, "exit 1\n"))(agent, prompt, ctx)

    codex.script = [new_goal, step(stop(store))]
    go(make_goal(tmp_path, work, verify=None))
    assert "Phase: PLAN" in codex.prompts[1] or "Phase: BUILD" in codex.prompts[1]
    assert not (work / ".baton" / "VERIFY.sh").exists()
    assert len(list((work / ".baton" / "archive").glob("*-VERIFY.sh"))) == 1


def test_a_goal_cleared_during_the_last_approval_does_not_achieve(
    go, agents, work, store, tmp_path
):
    codex, agy = agents["codex"], agents["antigravity"]
    codex.script = [step(touch(work)), approve(work), step(stop(store))]
    agy.script = [step(review(work, "APPROVED"), store.clear_goal)]
    assert go() == 0
    assert "goal achieved" not in " ".join(notices(store))
    assert any("goal changed" in m for m in notices(store))
    assert store.load_state()["consensus"] is None


def test_an_agent_that_can_run_and_has_not_approved_is_asked_before_the_end(
    go, agents, work, store
):
    codex, agy = agents["codex"], agents["antigravity"]

    def late_agent(agent, prompt, ctx):
        # antigravity was left out (it could not run at the start) and can now
        agy.avail = Availability(True)
        return approve(work)(agent, prompt, ctx)

    agy.avail = Availability(False, reason="disabled", permanent=True)
    codex.script = [step(touch(work)), late_agent]
    agy.script = [approve(work)]
    assert go() == 0
    assert len(agy.prompts) == 1
    assert notices(store)[-1] == "goal achieved: check passed, approved by codex, antigravity"


# the chat says who is left out and when consensus is dropped


def test_an_agent_left_out_of_consensus_is_named_in_the_chat(tmp_path, work, store, agents, world):
    agents["claude_api"] = Agent("claude_api", metered=True)
    agents["claude_api"].avail = Availability(False, reason="no key file", permanent=True)
    agents["codex"].script = [step(touch(work)), approve(work)]
    agents["antigravity"].script = [approve(work)]
    goal = make_goal(tmp_path, work, order='"codex", "antigravity", "claude_api"')
    run_goal(goal, agents, store, world.clock, world.sleep)
    assert any("left out of consensus" in m and "claude_api" in m for m in notices(store))


def test_a_goal_changed_during_consensus_says_it_is_dropped(go, agents, work, store, world):
    codex, agy = agents["codex"], agents["antigravity"]
    agy.avail = Availability(False, until=world.now + 100_000, reason="limit")
    said = []

    def sleep(seconds):
        world.sleep(seconds)
        if not said and waiting_for_an_agent(store):
            said.append(True)
            store.set_goal("a different goal")
            store.request_stop()

    codex.script = [step(touch(work)), approve(work)]
    go(sleep=sleep)
    assert any("goal changed" in m and "approval" in m for m in notices(store))
    assert store.load_state()["consensus"] is None


def test_the_wait_line_is_said_once_for_one_reason(go, agents, work, store, world):
    codex, agy = agents["codex"], agents["antigravity"]
    ready = world.now + 1000
    agy.avail = Availability(False, until=ready, reason="weekly limit")
    asked = []

    def sleep(seconds):
        world.sleep(seconds)
        if len(asked) < 3 and waiting_for_an_agent(store):
            asked.append(True)
            store.request_next()  # each wakes the wait, which then goes back to waiting
        if world.now >= ready:
            agy.avail = Availability(True)

    codex.script = [step(touch(work)), approve(work)]
    agy.script = [approve(work)]
    assert go(sleep=sleep) == 0
    waits = [e for e in events(store, "wait") if e["reason"].startswith("waiting for")]
    assert len(waits) == 1


# the smaller things


def test_a_consensus_round_starts_no_preemption_watch(
    tmp_path, work, store, agents, world, monkeypatch
):
    from baton import runner

    made = []

    class Watch:
        found = type("E", (), {"is_set": staticmethod(lambda: False)})()
        back = ""

        def __init__(self, *args):
            made.append(args)

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(runner, "PreemptionWatch", Watch)
    agents["claude_api"] = Agent("claude_api", metered=True, reserve=True)
    agents["codex"].script = [step(touch(work)), approve(work)]
    agents["claude_api"].script = [approve(work)]
    goal = make_goal(tmp_path, work, order='"codex", "claude_api"')
    run_goal(goal, agents, store, world.clock, world.sleep)
    assert last_event(store)["reason"] == "goal achieved" and made == []


def test_a_consensus_round_gets_no_round_tail(go, agents, work, store):
    codex, agy = agents["codex"], agents["antigravity"]
    codex.script = [say(["the last plan line"], then=step(touch(work))), approve(work)]
    agy.script = [approve(work)]
    go()
    assert "the last plan line" not in codex.prompts[1]
    assert "the last plan line" not in agy.prompts[0]


def test_three_incomplete_consensus_rounds_in_a_row_count_one_failed_cycle(go, agents, work, store):
    codex, agy = agents["codex"], agents["antigravity"]
    codex.script = [step(touch(work))] + [step(outcome=TIMEOUT)] * 3 + [approve(work)]
    agy.script = [approve(work)]
    assert go() == 0
    assert store.load_state()["failed_cycles"] == 1
    assert last_event(store)["reason"] == "goal achieved"


def test_two_incomplete_rounds_count_nothing(go, agents, work, store):
    codex, agy = agents["codex"], agents["antigravity"]
    codex.script = [step(touch(work))] + [step(outcome=TIMEOUT)] * 2 + [approve(work)]
    agy.script = [approve(work)]
    assert go() == 0
    assert store.load_state()["failed_cycles"] == 0


def test_approved_only_if_is_a_rejection(go, agents, work, store):
    codex = agents["codex"]
    codex.script = [
        step(touch(work)),
        step(review(work, "APPROVED only if the fps is 30")),
        step(stop(store)),
    ]
    go()
    assert "codex rejected: back to build" in notices(store)


@pytest.mark.refusals
def test_oneshot_in_a_directory_that_is_not_a_repository_pins_nothing_and_says_so(
    tmp_path, store, agents, world
):
    store.clear_goal()
    plain = tmp_path / "plain"
    plain.mkdir()
    goal = make_goal(tmp_path, plain, verify=None, top="oneshot = true")
    run_goal(goal, agents, store, world.clock, world.sleep)
    assert store.goal() == ""
    assert any("a goal needs a git repository" in e["message"] for e in events(store, "alert"))

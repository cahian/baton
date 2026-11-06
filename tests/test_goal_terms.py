"""A set goal is always goal mode: holds, the goal's terms, a goal that changes while the check
runs, and the check run once more before the end. Real refusals, real phases."""

import shutil
import subprocess

import pytest
from test_phases import Agent, git, plan, review, step, touch
from test_runner import World, make_goal

from baton.gateway import build_goal
from baton.runner import run_goal
from baton.store import Store

pytestmark = [pytest.mark.phases, pytest.mark.refusals]

GOAL = "ship it"
BASE = 'order = ["codex", "antigravity"]\nsnapshot = true\n[verify]\ncommand = "test -f DONE"\n'


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


def ev(store, kind):
    return [e for e in store.events() if e["kind"] == kind]


def last(store):
    return store.events()[-1]


def approve(work):
    return step(review(work, "APPROVED\nmet"))


def notices(store):
    return [e["message"] for e in ev(store, "notice")]


def alerts(store):
    return [e["message"] for e in ev(store, "alert")]


def toml(work, text):
    (work / "baton.toml").write_text(text)


def commit_toml(work, text=BASE):
    toml(work, text)
    git(work, "add", "-A")
    git(work, "commit", "-qm", "cfg")


def bg(work):
    return build_goal("job", work, GOAL)


def run(goal, adapters, store, world, sleep=None):
    return run_goal(goal, adapters, store, world.clock, sleep or world.sleep)


def fresh():
    return {"codex": Agent("codex"), "antigravity": Agent("antigravity")}


def edit_toml_then_stop(work, store, text):
    """A round in which an agent edits baton.toml, and the job is stopped (a service restart)."""

    def do():
        toml(work, text)
        store.request_stop()

    return do


def stop_when_held(store, world):
    """A sleep that asks for the stop once the run holds: the hold is what is being waited on."""

    def sleep(seconds):
        world.sleep(seconds)
        if any("no round runs" in m for m in alerts(store)):
            store.request_stop()

    return sleep


def stop_on_first_sleep(store, world):
    def sleep(seconds):
        world.sleep(seconds)
        store.request_stop()

    return sleep


# F1: a set goal is goal mode, and a run that cannot do it holds


def test_phases_false_in_the_repo_file_after_a_restart_holds_instead_of_finishing(
    work, store, agents, world
):
    commit_toml(work)
    new = 'phases = false\norder = ["codex", "antigravity"]\n[verify]\ncommand = "true"\n'
    agents["codex"].script = [step(plan(work), edit_toml_then_stop(work, store, new))]
    run(bg(work), agents, store, world)
    assert last(store)["kind"] == "stopped"
    again = fresh()
    run(bg(work), again, store, world, stop_when_held(store, world))
    assert last(store)["kind"] == "stopped" and "finished" not in [
        e["kind"] for e in store.events()
    ]
    assert any("a goal needs phases, and phases = false here" in m for m in alerts(store))
    assert again["codex"].prompts == [] and again["antigravity"].prompts == []
    assert store.goal() == GOAL


def test_phases_false_with_no_verify_holds_and_runs_no_round(work, store, agents, world):
    toml(work, 'phases = false\norder = ["codex", "antigravity"]\n')
    run(bg(work), agents, store, world, stop_when_held(store, world))
    assert last(store)["kind"] == "stopped"
    assert agents["codex"].prompts == [] and agents["antigravity"].prompts == []


def test_phases_false_in_a_directory_that_is_not_a_repository_holds(tmp_path, store, agents, world):
    plain = tmp_path / "plain"
    plain.mkdir()
    toml(plain, 'phases = false\norder = ["codex", "antigravity"]\n[verify]\ncommand = "true"\n')
    run(bg(plain), agents, store, world, stop_when_held(store, world))
    assert last(store)["kind"] == "stopped"
    assert any("a goal needs" in m for m in alerts(store))


def test_a_directory_that_is_not_a_repository_with_phases_on_holds_and_spends_nothing(
    tmp_path, store, agents, world
):
    plain = tmp_path / "plain"
    plain.mkdir()
    toml(plain, 'order = ["codex", "antigravity"]\n[verify]\ncommand = "true"\n')
    run(bg(plain), agents, store, world, stop_when_held(store, world))
    assert last(store)["kind"] == "stopped"
    assert any("a goal needs a git repository" in m for m in alerts(store))
    assert agents["codex"].prompts == [] and agents["antigravity"].prompts == []


def test_a_git_directory_removed_mid_run_holds_and_never_finishes(
    tmp_path, work, store, agents, world
):
    goal = make_goal(tmp_path, work, verify="true", snapshot=True)

    def remove_git():
        shutil.rmtree(work / ".git")

    for agent in agents.values():
        agent.script = [step(review(work, "APPROVED\nmet"), remove_git)] + [
            step(review(work, "APPROVED\nmet")) for _ in range(30)
        ]
    run(goal, agents, store, world, stop_when_held(store, world))
    assert last(store)["kind"] == "stopped"
    assert not any("achieved" in m for m in notices(store))
    assert any("a goal needs a git repository" in m for m in alerts(store))
    assert len(agents["codex"].prompts) + len(agents["antigravity"].prompts) == 1


def test_the_hold_says_so_once_and_ends_when_the_goal_is_cleared(work, store, agents, world):
    toml(work, 'phases = false\norder = ["codex", "antigravity"]\n')
    seen = []

    def sleep(seconds):
        world.sleep(seconds)
        seen.append(seconds)
        if len(seen) == 12:
            store.clear_goal()
        if len(seen) == 100:  # a hold that never ended would have to be stopped from outside
            store.request_stop()

    agents["codex"].script = [step(touch(work))]
    run(bg(work), agents, store, world, sleep)
    assert len([m for m in alerts(store) if "no round runs" in m]) == 1
    assert last(store)["reason"] == "round completed"  # no goal: as ever, with no phases


# F2: a goal that changes while the check runs does not inherit its pass


def test_a_goal_changed_while_the_script_runs_is_not_given_the_old_goals_pass(
    tmp_path, work, store, agents, world
):
    goal = make_goal(tmp_path, work, verify=None, snapshot=False)

    def script():
        (work / ".baton").mkdir(exist_ok=True)
        (work / ".baton" / "VERIFY.sh").write_text(f"echo goal B > {store.root}/goal.md\nexit 0\n")

    agents["codex"].script = [step(script), step(lambda: store.request_stop())]
    run(goal, agents, store, world)
    assert "goal achieved" not in " ".join(notices(store))
    assert all("Phase: CONSENSUS" not in p for p in agents["codex"].prompts)
    tails = {e["tail"] for e in ev(store, "gate")}
    assert tails == {"no check"}  # redone for goal B, and goal A's script was set aside
    assert not (work / ".baton" / "VERIFY.sh").exists()
    assert any("goal changed" in m and "check" in m for m in notices(store))


def test_a_goal_set_while_the_gate_runs_has_the_gate_redone_for_it(tmp_path, work, world, agents):
    st = Store(tmp_path / "state")
    goal = make_goal(tmp_path, work, verify=f"echo late goal > {st.root}/goal.md")
    agents["codex"].script = [approve(work)]
    agents["antigravity"].script = [approve(work)]
    assert run(goal, agents, st, world) == 0
    assert last(st)["reason"] == "goal achieved"
    assert len(ev(st, "gate")) >= 2  # the pass seen by no goal was thrown away


# F3/F4: the terms of a goal are saved when it is first seen


def test_the_goals_terms_are_saved_at_first_sight(work, store, agents, world):
    commit_toml(work)
    agents["codex"].script = [step(edit_toml_then_stop(work, store, BASE))]
    run(bg(work), agents, store, world)
    terms = store.load_state()["goal_terms"]
    assert terms["verify"] == "test -f DONE"
    assert terms["order"] == ["codex", "antigravity"]
    assert "antigravity" in terms["enabled"]


def one_round_then_restart_with(work, store, agents, world, new_toml):
    commit_toml(work)
    agents["codex"].script = [step(plan(work), edit_toml_then_stop(work, store, new_toml))]
    run(bg(work), agents, store, world)
    assert last(store)["kind"] == "stopped"


def terms_alerts(store):
    return [m for m in alerts(store) if "the goal's terms changed" in m]


def finish_after_a_restart(work, store, world, adapters=None):
    c2, a2 = Agent("codex"), Agent("antigravity")
    c2.script = [step(touch(work)), approve(work)]
    a2.script = [approve(work)]
    run(
        bg(work),
        adapters or {"codex": c2, "antigravity": a2},
        store,
        world,
        stop_on_first_sleep(store, world),
    )
    return c2, a2


def test_a_verify_command_weakened_in_the_repo_file_fails_the_gate(work, store, agents, world):
    one_round_then_restart_with(
        work, store, agents, world, BASE.replace("test -f DONE", "test -f a.txt")
    )
    c2, a2 = finish_after_a_restart(work, store, world)
    assert terms_alerts(store) and "verify" in terms_alerts(store)[0]
    assert "goal achieved" not in " ".join(notices(store))
    assert all("Phase: CONSENSUS" not in p for p in c2.prompts + a2.prompts)
    assert any("terms changed" in e["tail"] for e in ev(store, "gate"))


def test_a_verify_table_removed_from_the_repo_file_fails_the_gate(work, store, agents, world):
    one_round_then_restart_with(
        work, store, agents, world, 'order = ["codex", "antigravity"]\nsnapshot = true\n'
    )
    c2, a2 = Agent("codex"), Agent("antigravity")

    def weak():
        (work / ".baton" / "VERIFY.sh").write_text("test -f a.txt\n")

    c2.script = [step(weak), approve(work), step(weak), approve(work)]
    a2.script = [approve(work), approve(work)]
    run(bg(work), {"codex": c2, "antigravity": a2}, store, world, stop_on_first_sleep(store, world))
    assert terms_alerts(store) and "verify" in terms_alerts(store)[0]
    assert "goal achieved" not in " ".join(notices(store))
    assert all("Phase: CONSENSUS" not in p for p in c2.prompts + a2.prompts)


def test_an_order_cut_in_the_repo_file_fails_the_gate(work, store, agents, world):
    one_round_then_restart_with(
        work, store, agents, world, BASE.replace('"codex", "antigravity"', '"codex"')
    )
    c2 = Agent("codex")
    c2.script = [step(touch(work)), approve(work)]
    run(bg(work), {"codex": c2}, store, world, stop_on_first_sleep(store, world))
    assert terms_alerts(store) and "order" in terms_alerts(store)[0]
    assert "goal achieved" not in " ".join(notices(store))


def test_an_agent_switched_off_in_the_repo_file_fails_the_gate(work, store, agents, world):
    one_round_then_restart_with(
        work, store, agents, world, BASE + "[agents.antigravity]\nenabled = false\n"
    )
    goal = bg(work)
    assert goal.agents["antigravity"].enabled is False
    c2 = Agent("codex")
    c2.script = [step(touch(work)), approve(work)]
    run(goal, {"codex": c2}, store, world, stop_on_first_sleep(store, world))
    assert terms_alerts(store) and "enabled" in terms_alerts(store)[0]
    assert "goal achieved" not in " ".join(notices(store))


def test_setting_the_goal_again_accepts_the_new_terms_even_with_the_same_words(
    work, store, agents, world
):
    one_round_then_restart_with(
        work, store, agents, world, BASE.replace("test -f DONE", "test -f a.txt")
    )
    store.set_goal(GOAL)  # `baton goal` / `baton: goal`: the same words, said again
    c2, a2 = Agent("codex"), Agent("antigravity")
    c2.script = [approve(work)]
    a2.script = [approve(work)]
    run(bg(work), {"codex": c2, "antigravity": a2}, store, world)
    assert terms_alerts(store) == []
    assert last(store)["reason"] == "goal achieved"


def test_the_terms_are_judged_on_every_restart_until_they_are_accepted(work, store, agents, world):
    one_round_then_restart_with(
        work, store, agents, world, BASE.replace("test -f DONE", "test -f a.txt")
    )
    finish_after_a_restart(work, store, world)
    first = len(terms_alerts(store))
    assert first >= 1
    store.clear_stop()
    finish_after_a_restart(work, store, world)
    assert len(terms_alerts(store)) > first


def test_consensus_shows_the_saved_verify_command(work, store, agents, world):
    commit_toml(work)
    (work / "DONE").write_text("x")
    c, a = agents["codex"], agents["antigravity"]
    c.script = [approve(work)]
    a.script = [approve(work)]
    run(bg(work), agents, store, world)
    assert "The check, the verify command:\n```sh\ntest -f DONE\n```" in c.prompts[0]


def test_a_resumed_consensus_after_the_order_changed_is_void_even_when_the_file_is_ignored(
    work, store, agents, world
):
    (work / ".gitignore").write_text("baton.toml\n")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "ignore")
    toml(work, BASE.replace("test -f DONE", "test -f a.txt"))
    new = BASE.replace('"codex", "antigravity"', '"codex"').replace("test -f DONE", "test -f a.txt")
    agents["codex"].script = [step(review(work, "APPROVED"), edit_toml_then_stop(work, store, new))]
    run(bg(work), agents, store, world)
    assert store.load_state()["consensus"] is not None
    c2 = Agent("codex")
    c2.script = [step(lambda: store.request_stop())]
    run(bg(work), {"codex": c2}, store, world)
    assert "goal achieved" not in " ".join(notices(store))
    assert store.load_state()["consensus"] is None
    assert any("terms" in entry["text"] for entry in store.load_state()["note_log"])


# F5: the check runs once more just before the end


def test_a_gitignored_file_the_check_tests_is_found_broken_by_the_last_run(
    tmp_path, work, store, agents, world
):
    (work / ".gitignore").write_text("dist/\n")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "ignore")
    (work / "dist").mkdir()
    (work / "dist" / "app").write_text("ok")
    goal = make_goal(tmp_path, work, verify="test -f dist/app", snapshot=True)
    c, a = agents["codex"], agents["antigravity"]
    c.script = [approve(work), step(lambda: store.request_stop())]
    a.script = [step(review(work, "APPROVED"), lambda: (work / "dist" / "app").unlink())]
    run(goal, agents, store, world)
    assert "goal achieved" not in " ".join(notices(store))
    assert "the check no longer passes: back to build" in notices(store)
    assert subprocess.run("test -f dist/app", shell=True, cwd=work).returncode != 0
    assert store.load_state()["phase"]["phase"] == "build"
    assert store.load_state()["failed_cycles"] == 1


def test_the_check_runs_once_more_before_the_goal_ends(work, store, agents, world):
    commit_toml(work)
    (work / "DONE").write_text("x")
    agents["codex"].script = [approve(work)]
    agents["antigravity"].script = [approve(work)]
    run(bg(work), agents, store, world)
    assert last(store)["reason"] == "goal achieved"
    assert [g["passed"] for g in ev(store, "gate")] == [True, True]  # to start it, and to end it

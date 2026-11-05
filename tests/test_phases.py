"""Phases: the pure transition table here, the runner with fake adapters in the second half."""

import json
import subprocess

import pytest
from test_runner import Fake, World, make_goal

from baton import phases, runner
from baton.adapters.base import COMPLETED, LIMIT, RoundResult
from baton.gateway import render_event
from baton.phases import (
    BUILD,
    PLAN,
    REVIEW,
    TIER,
    PhaseState,
    advance,
    open_tasks,
    phase_prompt,
    verdict,
)
from baton.runner import run_goal
from baton.store import Store

T1, T2, T3 = "- [ ] one", "- [ ] two", "- [ ] three"


def st(phase=PLAN, **kw):
    return PhaseState(phase=phase, **kw)


# the small pieces


def test_tiers():
    assert TIER == {PLAN: "strong", BUILD: "build", REVIEW: "strong"}


def test_open_tasks_only_unchecked_lines():
    text = "# Plan\n- [ ] a\n  - [ ] b\n- [x] done\n* [ ] not a task\n-[ ] nope\ntext - [ ] no\n"
    assert open_tasks(text) == ["- [ ] a", "- [ ] b"]
    assert open_tasks("") == []


@pytest.mark.parametrize(
    "text,expected",
    [
        ("APPROVED", "approved"),
        ("\n\n  approved, looks good\nmore", "approved"),
        ("Approved\n", "approved"),
        ("CHANGES\n- [ ] x", "changes"),
        ("", "changes"),
        ("   \n\n", "changes"),
        ("# Review\nAPPROVED", "changes"),
        ("looks APPROVED to me", "changes"),
        ("NOT APPROVED", "changes"),
    ],
)
def test_verdict(text, expected):
    assert verdict(text) == expected


def test_phase_state_roundtrip_and_bad_data():
    s = PhaseState(REVIEW, "abc123", 2, 1, [T1])
    assert PhaseState.from_json(s.to_json()) == s
    assert PhaseState.from_json(json.loads(json.dumps(s.to_json()))) == s
    start = PhaseState()
    assert start.phase == PLAN and start.base == "" and start.cycles == 0
    for bad in (
        None,
        "x",
        [],
        {},
        {"phase": "nope", "base": "", "cycles": 0, "stuck": 0, "last_open": []},
        {"phase": BUILD, "base": 5, "cycles": 0, "stuck": 0, "last_open": []},
        {"phase": BUILD, "base": "", "cycles": -1, "stuck": 0, "last_open": []},
        {"phase": BUILD, "base": "", "cycles": True, "stuck": 0, "last_open": []},
        {"phase": BUILD, "base": "", "cycles": 0, "stuck": "1", "last_open": []},
        {"phase": BUILD, "base": "", "cycles": 0, "stuck": 0, "last_open": "x"},
        {"phase": BUILD, "base": "", "cycles": 0, "stuck": 0, "last_open": [1]},
    ):
        assert PhaseState.from_json(bad) == start


# advance: every transition


def run(state, completed=True, open_now=(), review=""):
    return advance(state, outcome_completed=completed, open_now=list(open_now), review_text=review)


@pytest.mark.parametrize("phase", [PLAN, BUILD, REVIEW])
def test_a_round_that_did_not_complete_changes_nothing(phase):
    # preempted, killed, limited and failed rounds all arrive as outcome_completed=False
    before = PhaseState(phase, "b", 2, 1, [T1, T2])
    after, ended = run(before, completed=False, open_now=[T1], review="APPROVED")
    assert after == before and ended is False


def test_plan_with_tasks_goes_to_build():
    after, ended = run(st(PLAN, base="b"), open_now=[T1, T2])
    assert (after.phase, after.base, after.last_open, ended) == (BUILD, "b", [T1, T2], False)


def test_plan_with_no_tasks_ends_the_cycle_at_once():
    # a missing or empty PLAN.md reads as no tasks
    after, ended = run(st(PLAN, base="b", cycles=2), open_now=[])
    assert ended is True and after.phase == PLAN and after.cycles == 0 and after.base == "b"


def test_build_closing_a_task_stays_in_build():
    before = st(BUILD, last_open=[T1, T2], stuck=1)
    after, ended = run(before, open_now=[T2])
    assert (after.phase, after.stuck, after.last_open, ended) == (BUILD, 0, [T2], False)


def test_build_with_every_task_closed_goes_to_review():
    after, ended = run(st(BUILD, last_open=[T1]), open_now=[])
    assert after.phase == REVIEW and ended is False


def test_build_stuck_twice_on_the_same_task_goes_to_review():
    s = st(BUILD, last_open=[T1, T2])
    s, ended = run(s, open_now=[T1, T2])
    assert s.phase == BUILD and s.stuck == 1 and ended is False
    s, ended = run(s, open_now=[T1, T2])
    assert s.phase == REVIEW and s.stuck == 0 and s.last_open == [T1, T2]


def test_stuck_counter_resets_when_a_task_closes_in_between():
    s = st(BUILD, last_open=[T1, T2])
    s, _ = run(s, open_now=[T1, T2])  # stuck 1
    s, _ = run(s, open_now=[T2])  # closed one: 0
    s, _ = run(s, open_now=[T2])  # stuck 1 again, not 2
    assert s.phase == BUILD and s.stuck == 1


def test_build_that_only_adds_tasks_still_counts_as_stuck():
    s, _ = run(st(BUILD, last_open=[T1]), open_now=[T1, T2])
    assert s.stuck == 1


def test_review_approved_ends_the_cycle():
    after, ended = run(st(REVIEW, base="b", cycles=1), open_now=[], review="APPROVED\nfine")
    assert ended is True and after.phase == PLAN and after.cycles == 0 and after.base == "b"


def test_review_changes_goes_back_to_build_and_counts():
    after, ended = run(st(REVIEW), open_now=[T3], review="CHANGES\n- [ ] three")
    assert (after.phase, after.cycles, after.last_open, ended) == (BUILD, 1, [T3], False)


@pytest.mark.parametrize("review", ["", "   ", "garbage", "# Review\nAPPROVED", "NOT APPROVED"])
def test_a_missing_or_malformed_verdict_counts_as_changes(review):
    after, ended = run(st(REVIEW), open_now=[], review=review)
    assert after.phase == BUILD and after.cycles == 1 and ended is False


def test_cycle_cap_ends_the_cycle_after_three_reviews():
    s = st(REVIEW)
    for expected in (1, 2):
        s, ended = run(s, open_now=[T1], review="CHANGES")
        assert (s.phase, s.cycles, ended) == (BUILD, expected, False)
        s = PhaseState(REVIEW, s.base, s.cycles, 0, [])
    s, ended = run(s, open_now=[T1], review="CHANGES")
    assert ended is True and s.phase == PLAN and s.cycles == 0


def test_cycle_cap_is_a_parameter():
    s, ended = advance(
        st(REVIEW, cycles=0), outcome_completed=True, open_now=[T1], review_text="x", max_cycles=1
    )
    assert ended is True


def test_advance_does_not_mutate_its_input():
    before = st(BUILD, last_open=[T1])
    run(before, open_now=[])
    assert before == st(BUILD, last_open=[T1])


# prompts


def test_plan_prompt_names_the_files_and_the_no_task_case():
    text = phase_prompt(PLAN, "", "abc")
    assert ".baton/PLAN.md" in text and "- [ ]" in text and "no open task" in text


def test_build_prompt_states_one_task_only():
    text = phase_prompt(BUILD, "- [ ] add the widget", "abc")
    assert "- [ ] add the widget" in text and "one task only" in text and "- [x]" in text
    assert ".baton/PLAN.md" in text


def test_review_prompt_forbids_editing_and_names_the_diff():
    text = phase_prompt(REVIEW, "", "abc123")
    assert "git diff abc123..HEAD" in text
    assert "do not edit code" in text
    assert ".baton/REVIEW.md" in text and "APPROVED" in text and "CHANGES" in text


def test_review_prompt_without_a_base_still_tells_where_to_look():
    assert "git log" in phase_prompt(REVIEW, "", "")


# the two files


def test_plan_and_review_readers(tmp_path):
    assert phases.read_plan(tmp_path) == "" and phases.read_review(tmp_path) == ""
    (tmp_path / ".baton").mkdir()
    (tmp_path / ".baton" / "PLAN.md").write_text("- [ ] a\n")
    assert phases.read_plan(tmp_path) == "- [ ] a\n"


def test_clear_review_removes_the_file_and_tolerates_none(tmp_path):
    assert phases.clear_review(tmp_path) is True
    (tmp_path / ".baton").mkdir()
    f = tmp_path / ".baton" / "REVIEW.md"
    f.write_text("APPROVED")
    assert phases.clear_review(tmp_path) is True and not f.exists()


def test_add_review_task_appends_once(tmp_path):
    phases.add_review_task(tmp_path)
    plan = tmp_path / ".baton" / "PLAN.md"
    assert plan.read_text() == phases.REVIEW_TASK + "\n"
    phases.add_review_task(tmp_path)
    assert plan.read_text().count(phases.REVIEW_TASK) == 1
    plan.write_text("- [x] done")  # no trailing newline
    phases.add_review_task(tmp_path)
    assert plan.read_text() == f"- [x] done\n{phases.REVIEW_TASK}\n"


# the runner, with fake adapters

pytestmark = pytest.mark.phases


class Agent(Fake):
    """A scripted adapter that also keeps the tier of each round, and reports a session id."""

    def __init__(self, name, metered=False):
        super().__init__(name, metered)
        self.tiers = []

    def run_round(self, prompt, ctx):
        if len(self.prompts) > 25:
            pytest.fail(f"{self.name} runaway: tiers {self.tiers}, last prompt {prompt[-300:]!r}")
        self.tiers.append(ctx.tier)
        ctx.on_session(f"sid-{self.name}-{len(self.prompts) + 1}")
        return super().run_round(prompt, ctx)


def git(work, *args):
    return subprocess.run(
        ["git", "-C", str(work), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


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
    return Store(tmp_path / "state")


@pytest.fixture
def agents():
    return {"codex": Agent("codex"), "antigravity": Agent("antigravity")}


@pytest.fixture
def go(tmp_path, work, store, agents, world):
    def run(goal=None, adapters=None):
        return run_goal(
            goal or make_goal(tmp_path, work), adapters or agents, store, world.clock, world.sleep
        )

    return run


def step(*actions, outcome=COMPLETED):
    """A round that runs `actions` (each `fn(work)`-style callables closed over their args) and
    ends as `outcome`."""

    def run(agent, prompt, ctx):
        for action in actions:
            action()
        return RoundResult(outcome)

    return run


def plan(work, *tasks):
    def do():
        (work / ".baton").mkdir(exist_ok=True)
        (work / ".baton" / "PLAN.md").write_text(
            "# Plan\n" + "".join(f"- [ ] {t}\n" for t in tasks)
        )

    return do


def close(work, extra=None):
    """Close the first open task, and commit."""

    def do():
        path = work / ".baton" / "PLAN.md"
        path.write_text(path.read_text().replace("- [ ]", "- [x]", 1))
        (work / "built.txt").write_text(str(len(list(work.iterdir()))) + (extra or ""))
        git(work, "add", "-A")
        git(work, "commit", "-q", "--allow-empty", "-m", "build")

    return do


def review(work, text, *tasks):
    def do():
        (work / ".baton").mkdir(exist_ok=True)
        (work / ".baton" / "REVIEW.md").write_text(text)
        if tasks:
            with (work / ".baton" / "PLAN.md").open("a") as fh:
                fh.write("".join(f"- [ ] {t}\n" for t in tasks))

    return do


def touch(work, name="DONE"):
    return lambda: (work / name).write_text("x")


def phase_events(store):
    return [e for e in store.events() if e["kind"] == "phase"]


def reasons(store):
    return [e["reason"] for e in phase_events(store)]


def starts(store):
    return [e for e in store.events() if e["kind"] == "round_start"]


def test_a_full_cycle_plan_build_review_changes_build_review_approved_gate(go, agents, work, store):
    base = git(work, "rev-parse", "HEAD")
    codex, agy = agents["codex"], agents["antigravity"]
    codex.script = [
        step(plan(work, "first", "second")),
        step(close(work)),
        step(close(work)),
        step(close(work), touch(work)),
    ]
    agy.script = [
        step(review(work, "CHANGES\nbad", "fix it")),
        step(review(work, "APPROVED\nfine")),
    ]
    assert go() == 0
    assert [(a.name, a.tiers) for a in (codex, agy)] == [
        ("codex", ["strong", "build", "build", "build"]),
        ("antigravity", ["strong", "strong"]),
    ]
    ends = [e for e in store.events() if e["kind"] == "round_end"]
    assert [e["phase"] for e in ends] == ["plan", "build", "build", "review", "build", "review"]
    assert [(e["phase"], e["cycle"]) for e in phase_events(store)] == [
        ("build", 0),
        ("review", 0),
        ("build", 1),
        ("review", 1),
        ("plan", 0),
    ]
    assert "approved" in phase_events(store)[-1]["reason"]
    # the gate runs before the first round and at the end of the cycle, and nowhere else
    gates = [e for e in store.events() if e["kind"] == "gate"]
    assert [g["passed"] for g in gates] == [False, True]
    assert store.events()[-1]["kind"] == "finished"
    # review went to the other provider without moving the rotation: codex builds again
    assert store.load_state()["current"] == "codex"
    # sessions are per agent and role; review neither resumes nor stores one
    assert codex.sessions == [None, None, "sid-codex-2", "sid-codex-3"]
    assert agy.sessions == [None, None]
    assert set(store.load_state()["sessions"]) == {"codex:plan", "codex:build"}
    assert "Phase: PLAN" in codex.prompts[0]
    assert "- [ ] first" in codex.prompts[1] and "one task only" in codex.prompts[1]
    assert "Your turn again" in codex.prompts[2] and "Phase: BUILD" in codex.prompts[2]
    assert f"git diff {base}..HEAD" in agy.prompts[0]
    assert "Phase: REVIEW" in agy.prompts[1]


def test_the_phase_is_persisted_in_state_json(go, agents, work, store):
    seen = []

    def spy(agent, prompt, ctx):
        seen.append(store.load_state().get("phase"))
        return RoundResult(COMPLETED)

    base = git(work, "rev-parse", "HEAD")
    agents["codex"].script = [
        step(plan(work, "a")),
        lambda a, p, c: (spy(a, p, c), step(close(work), touch(work))(a, p, c))[1],
    ]
    agents["antigravity"].script = [step(review(work, "APPROVED"))]
    go()
    assert seen[0]["phase"] == "build" and seen[0]["base"] == base
    assert seen[0]["last_open"] == ["- [ ] a"]


def test_a_stuck_builder_moves_on_to_review_after_two_rounds(go, agents, work, store):
    codex, agy = agents["codex"], agents["antigravity"]
    codex.script = [step(plan(work, "hard")), step(), step(touch(work))]
    agy.script = [step(review(work, "APPROVED"))]
    go()
    assert codex.tiers == ["strong", "build", "build"]
    assert agy.tiers == ["strong"]
    assert any("stuck" in r for r in reasons(store))


def test_a_failed_gate_returns_to_plan_and_keeps_the_base(go, agents, work, store):
    base = git(work, "rev-parse", "HEAD")
    codex, agy = agents["codex"], agents["antigravity"]
    at_second_plan = []

    def replan(agent, prompt, ctx):
        at_second_plan.append((store.load_state()["phase"], prompt))
        return step(plan(work, "repair"))(agent, prompt, ctx)

    codex.script = [
        step(plan(work, "one")),
        step(close(work)),
        replan,
        step(close(work), touch(work)),
    ]
    agy.script = [step(review(work, "APPROVED")), step(review(work, "APPROVED"))]
    go()
    (state, prompt) = at_second_plan[0]
    assert state["phase"] == "plan" and state["cycles"] == 0 and state["base"] == base
    assert "Phase: PLAN" in prompt and "[exit code 1]" in prompt  # the strong model sees the gate
    assert codex.tiers == ["strong", "build", "strong", "build"]
    assert codex.sessions[2] == "sid-codex-1"  # the plan conversation continues
    assert any("verify" in r for r in reasons(store))
    assert [g["passed"] for g in store.events() if g["kind"] == "gate"] == [False, False, True]


def test_a_plan_with_no_task_ends_the_cycle_at_once(go, agents, work, store, tmp_path):
    agents["codex"].script = [step(touch(work))]
    assert go() == 0
    assert agents["codex"].tiers == ["strong"] and agents["antigravity"].tiers == []
    assert store.events()[-1]["kind"] == "finished"


def test_a_missing_plan_means_no_tasks_and_without_a_gate_the_cycle_finishes(
    go, agents, work, store, tmp_path
):
    go(make_goal(tmp_path, work, verify=None))
    assert len(agents["codex"].prompts) == 1 and not agents["antigravity"].prompts
    assert store.events()[-1]["kind"] == "finished"


def test_without_a_gate_the_end_of_the_cycle_finishes_the_goal(go, agents, work, store, tmp_path):
    codex, agy = agents["codex"], agents["antigravity"]
    codex.script = [step(plan(work, "a")), step(close(work))]
    agy.script = [step(review(work, "APPROVED"))]
    go(make_goal(tmp_path, work, verify=None))
    assert len(codex.prompts) == 2 and len(agy.prompts) == 1
    assert store.events()[-1]["kind"] == "finished"


@pytest.mark.parametrize("outcome", ["limit", "transient"])
def test_a_round_that_did_not_complete_leaves_the_phase_for_the_next_agent(
    go, agents, work, store, outcome, world
):
    codex, agy = agents["codex"], agents["antigravity"]
    codex.script = [
        step(plan(work, "a")),
        step(outcome=outcome),
    ]
    agy.script = [step(close(work), touch(work)), step(review(work, "APPROVED"))]
    go()
    # the failed build round is repeated by the next agent, at the build tier, on the same task
    assert agy.tiers[0] == "build" and "- [ ] a" in agy.prompts[0]
    assert [e["phase"] for e in store.events() if e["kind"] == "round_end"][:3] == [
        "plan",
        "build",
        "build",
    ]
    assert reasons(store)[:2] == ["plan written: 1 open task(s)", "every task is closed"]


def test_a_limited_review_is_repeated_at_the_strong_tier(go, agents, work, store):
    codex, agy = agents["codex"], agents["antigravity"]
    codex.script = [
        step(plan(work, "a")),
        step(close(work), touch(work)),
        step(review(work, "APPROVED")),
    ]
    agy.script = [step(outcome="limit")]
    go()
    assert agy.tiers == ["strong"] and codex.tiers == ["strong", "build", "strong"]
    assert "Phase: REVIEW" in codex.prompts[2]


def test_a_note_in_the_middle_of_a_cycle_changes_no_phase(go, agents, work, store):
    codex = agents["codex"]
    codex.script = [
        step(plan(work, "a", "b")),
        step(close(work), lambda: store.say("also mind the margins")),
        step(close(work), touch(work)),
        step(),
    ]
    agents["antigravity"].script = [step(review(work, "APPROVED"))]
    go()
    assert "also mind the margins" in codex.prompts[2]
    assert codex.tiers[:3] == ["strong", "build", "build"]
    assert [e["kind"] for e in store.events()].count("gate") == 2


def test_the_review_cycle_cap_ends_the_cycle_after_three_reviews(go, agents, work, store, tmp_path):
    codex, agy = agents["codex"], agents["antigravity"]
    codex.script = [step(plan(work, "a"))] + [step(close(work)) for _ in range(4)]
    agy.script = [step(review(work, "CHANGES", "again")) for _ in range(3)]
    go(make_goal(tmp_path, work, verify=None))
    assert len(agy.prompts) == 3 and len(codex.prompts) == 4
    assert "cap" in reasons(store)[-1]
    assert store.events()[-1]["kind"] == "finished"


def test_a_job_from_before_phases_resumes_at_plan_and_ignores_old_session_ids(
    go, agents, work, store
):
    store.save_state(
        {"round": 3, "workdir": str(work), "sessions": {"codex": "old-sid"}, "heads": {}}
    )
    agents["codex"].script = [step(touch(work))]
    go()
    codex = agents["codex"]
    assert codex.sessions == [None] and codex.tiers == ["strong"]
    assert "Phase: PLAN" in codex.prompts[0]
    assert "codex" not in store.load_state()["sessions"]


def test_a_state_with_a_bad_phase_resumes_at_plan(go, agents, work, store):
    store.save_state({"round": 1, "workdir": str(work), "phase": {"phase": "nonsense"}})
    agents["codex"].script = [step(touch(work))]
    go()
    assert agents["codex"].tiers == ["strong"]


def test_a_stored_phase_is_resumed_not_restarted(go, agents, work, store):
    head = git(work, "rev-parse", "HEAD")
    state = phases.PhaseState(phases.BUILD, head, 1, 0, ["- [ ] a"])
    store.save_state({"round": 4, "workdir": str(work), "phase": state.to_json()})
    plan(work, "a")()
    agents["codex"].script = [step(close(work), touch(work))]
    go()
    assert agents["codex"].tiers[0] == "build" and "Phase: BUILD" in agents["codex"].prompts[0]


def test_a_person_message_that_owes_a_round_starts_a_new_cycle_at_plan(
    go, agents, work, store, tmp_path
):
    head = git(work, "rev-parse", "HEAD")
    state = phases.PhaseState(phases.BUILD, head, 1, 0, ["- [ ] a"])
    store.save_state({"round": 4, "workdir": str(work), "phase": state.to_json()})
    agents["codex"].script = [step(touch(work))]
    run_goal(make_goal(tmp_path, work), agents, store, lambda: 1e6, lambda s: None, owes_round=True)
    assert agents["codex"].tiers == ["strong"]


def test_phases_false_keeps_the_plain_relay(go, agents, work, store, tmp_path):
    agents["codex"].script = [step(), step(touch(work))]
    go(make_goal(tmp_path, work, top="phases = false"))
    codex = agents["codex"]
    assert codex.tiers == ["strong", "strong"]
    assert all("Phase:" not in p for p in codex.prompts)
    assert phase_events(store) == []
    state = store.load_state()
    assert "phase" not in state and set(state["sessions"]) == {"codex"}
    assert [g["passed"] for g in store.events() if g["kind"] == "gate"] == [False, False, True]
    assert all("phase" not in e for e in store.events() if e["kind"] == "round_end")


# the two native guards: baton enforces them, the prompt does not


def at_review_start(work, seen):
    """Record whether REVIEW.md exists the moment a review round starts."""
    return lambda: seen.append((work / ".baton" / "REVIEW.md").exists())


def to_review(work, store, agents, *reviews):
    """A cycle with one task built, then the given review rounds (steps) on antigravity."""
    agents["codex"].script = [step(plan(work, "a")), step(close(work))] + [
        step(close(work)) for _ in range(5)
    ]
    agents["antigravity"].script = list(reviews)


def test_an_old_approval_written_by_a_builder_never_approves(go, agents, work, store, tmp_path):
    seen = []
    agents["codex"].script = [
        step(plan(work, "a")),
        step(close(work), review(work, "APPROVED\nsneaky")),  # a builder writes a verdict
    ] + [step(close(work)) for _ in range(5)]
    agents["antigravity"].script = [
        step(at_review_start(work, seen)),  # writes nothing at all
        step(at_review_start(work, seen), review(work, "APPROVED")),
    ]
    go(make_goal(tmp_path, work, verify=None))
    assert seen == [False, False]  # deleted by baton before each review round
    assert reasons(store)[:3] == [
        "plan written: 1 open task(s)",
        "every task is closed",
        "review asked for changes (cycle 1 of 3)",  # a missing verdict is changes
    ]


def test_the_verdict_of_a_review_that_did_not_complete_is_not_read(go, agents, work, store):
    seen = []
    codex, agy = agents["codex"], agents["antigravity"]
    codex.script = [step(plan(work, "a")), step(close(work), touch(work)), step()]
    agy.script = [
        step(at_review_start(work, seen), review(work, "APPROVED"), outcome=LIMIT),
    ]
    codex.script[2] = step(at_review_start(work, seen), review(work, "APPROVED"))
    go()
    assert seen == [False, False]  # cleared again before the repeat
    assert [r for r in reasons(store)][:2] == [
        "plan written: 1 open task(s)",
        "every task is closed",
    ]
    assert "approved" in reasons(store)[2]  # only the completed repeat decided


def reviewer_edits(work, how):
    def do():
        if how == "edit":
            (work / "a.txt").write_text("edited by the reviewer")
        elif how == "new":
            (work / "new.txt").write_text("x")
        elif how == "commit":
            (work / "c.txt").write_text("x")
            git(work, "add", "-A")
            git(work, "commit", "-q", "-m", "reviewer commit")
        elif how == "delete":
            (work / "a.txt").unlink()

    return do


@pytest.mark.parametrize("how", ["edit", "new", "commit", "delete"])
def test_a_reviewer_that_changes_the_code_voids_its_approval(
    go, agents, work, store, tmp_path, how
):
    to_review(
        work,
        store,
        agents,
        step(reviewer_edits(work, how), review(work, "APPROVED\nlooks fine")),
        step(review(work, "APPROVED")),
    )
    go(make_goal(tmp_path, work, verify=None))
    assert "reviewer changed the code" in reasons(store)
    void = next(e for e in phase_events(store) if e["reason"] == "reviewer changed the code")
    assert void["phase"] == "review"
    assert "Review the changes made during review" in (work / ".baton" / "PLAN.md").read_text()
    if how == "edit":
        assert (work / "a.txt").read_text() == "edited by the reviewer"  # left in place
    # it counted as changes: back to build, with the follow-up task to build
    assert "review asked for changes (cycle 1 of 3)" in reasons(store)
    assert "Review the changes made during review" in agents["codex"].prompts[2]
    assert agents["codex"].tiers[2] == "build"


def test_a_reviewer_that_only_writes_under_baton_is_not_void(go, agents, work, store, tmp_path):
    to_review(
        work,
        store,
        agents,
        step(review(work, "APPROVED"), lambda: (work / ".baton" / "notes.txt").write_text("x")),
    )
    go(make_goal(tmp_path, work, verify=None))
    assert "reviewer changed the code" not in reasons(store)
    assert "approved" in reasons(store)[-1]


def test_a_tree_the_builder_left_dirty_is_not_the_reviewers_doing(
    go, agents, work, store, tmp_path
):
    def dirty():
        (work / "a.txt").write_text("builder left this uncommitted")

    agents["codex"].script = [step(plan(work, "a")), step(close(work), dirty)]
    agents["antigravity"].script = [step(review(work, "APPROVED"))]
    go(make_goal(tmp_path, work, verify=None))
    assert "reviewer changed the code" not in reasons(store)


def test_the_reviewer_editing_a_file_that_was_already_dirty_is_caught(
    go, agents, work, store, tmp_path
):
    def dirty():
        (work / "a.txt").write_text("builder left this uncommitted")

    agents["codex"].script = [step(plan(work, "a")), step(close(work), dirty)] + [
        step(close(work)) for _ in range(5)
    ]
    agents["antigravity"].script = [
        step(review(work, "APPROVED"), lambda: (work / "a.txt").write_text("and now the reviewer")),
        step(review(work, "APPROVED")),
    ]
    go(make_goal(tmp_path, work, verify=None))
    assert "reviewer changed the code" in reasons(store)


def test_a_workdir_that_is_not_a_repository_skips_the_review_with_one_alert(
    agents, tmp_path, store, world
):
    plain = tmp_path / "plain"
    plain.mkdir()
    codex, agy = agents["codex"], agents["antigravity"]
    codex.script = [step(plan(plain, "a")), step(close_plain(plain))] * 2
    run_goal(make_goal(tmp_path, plain, verify=None), agents, store, world.clock, world.sleep)
    assert agy.prompts == []  # no review round at all
    alerts = [e for e in store.events() if e["kind"] == "alert"]
    assert len(alerts) == 1 and "not a git repository" in alerts[0]["message"]
    assert "reviewer changed the code" not in reasons(store)
    assert store.events()[-1]["kind"] == "finished"  # the cycle ended after build


def test_the_non_git_alert_is_given_once_per_run_and_the_gate_still_judges(
    agents, tmp_path, store, world
):
    plain = tmp_path / "plain"
    plain.mkdir()
    codex = agents["codex"]
    codex.script = [
        step(plan(plain, "a")),
        step(close_plain(plain)),  # end of cycle 1: the gate fails (no DONE)
        step(plan(plain, "b")),
        step(close_plain(plain), touch(plain)),
    ]
    run_goal(make_goal(tmp_path, plain), agents, store, world.clock, world.sleep)
    assert len([e for e in store.events() if e["kind"] == "alert"]) == 1
    assert [g["passed"] for g in store.events() if g["kind"] == "gate"] == [False, False, True]
    assert agents["antigravity"].prompts == []


def close_plain(work):
    def do():
        path = work / ".baton" / "PLAN.md"
        path.write_text(path.read_text().replace("- [ ]", "- [x]", 1))

    return do


def test_a_reviewer_that_always_edits_is_still_bounded_by_the_cap(
    go, agents, work, store, tmp_path
):
    agents["codex"].script = [step(plan(work, "a"))] + [step(close(work)) for _ in range(8)]
    # each review writes different content, so each one changes the tree
    agents["antigravity"].script = [
        step(lambda i=i: (work / "new.txt").write_text(str(i)), review(work, "APPROVED"))
        for i in range(3)
    ]
    go(make_goal(tmp_path, work, verify=None))
    assert len(agents["antigravity"].prompts) == 3
    assert "cap" in reasons(store)[-1]


# what the person and the memory see


def test_the_gateway_shows_a_phase_line():
    text = render_event("phase", {"phase": "build", "cycle": 1, "reason": "every task is closed"})
    assert "build" in text and "every task is closed" in text and text.count("\n") <= 4


def test_round_entries_and_events_carry_the_phase(go, agents, work, store, tmp_path, monkeypatch):
    from test_runner import FakeMemory

    monkeypatch.delenv("BATON_MEMORY", raising=False)
    FakeMemory.built = []
    monkeypatch.setattr(runner, "Memory", FakeMemory)
    codex, agy = agents["codex"], agents["antigravity"]
    codex.script = [step(plan(work, "a")), step(close(work))]
    agy.script = [step(review(work, "APPROVED"))]
    go(make_goal(tmp_path, work, verify=None))
    assert [e.phase for e in FakeMemory.built[0].rounds] == ["plan", "build", "review"]


# Superpowers in the prompts, and the refusal to start without it


def waits(store):
    return [
        e for e in store.events() if e["kind"] == "wait" and "Superpowers" in e.get("reason", "")
    ]


def appears_after(world, fake_skills, seconds, monkeypatch):
    """Superpowers is missing until `seconds` of the fake clock have passed."""
    t0 = world.now
    calls = []

    def find(cfg=None, home=None):
        calls.append(world.now)
        return None if world.now < t0 + seconds else fake_skills

    monkeypatch.setattr("baton.superpowers.find_skills", find)
    return calls


def test_without_superpowers_a_job_waits_and_checks_again_every_300_seconds(
    go, agents, work, store, world, fake_skills, monkeypatch
):
    from baton.superpowers import INSTALL_COMMAND

    calls = appears_after(world, fake_skills, 700, monkeypatch)
    agents["codex"].script = [step(touch(work))]
    assert go() == 0
    found = waits(store)
    assert len(found) >= 3 and all(INSTALL_COMMAND in w["reason"] for w in found)
    assert all(w["seconds"] == 300 for w in found)
    assert not [e for e in store.events() if e["kind"] == "stopped"]  # never stopped
    first_round = next(i for i, e in enumerate(store.events()) if e["kind"] == "round_start")
    assert all(store.events().index(w) < first_round for w in found)  # no round before it
    gaps = [b - a for a, b in zip(calls, calls[1:], strict=False)]
    assert gaps and all(g >= 300 for g in gaps)
    assert INSTALL_COMMAND in render_event("wait", found[0])  # OpenCode shows it too
    assert store.events()[-1]["kind"] == "finished"


def test_a_running_job_waits_for_superpowers_too(
    go, agents, work, store, world, fake_skills, monkeypatch
):
    head = git(work, "rev-parse", "HEAD")
    state = phases.PhaseState(phases.BUILD, head, 0, 0, ["- [ ] a"])
    store.save_state({"round": 4, "workdir": str(work), "phase": state.to_json()})
    plan(work, "a")()
    appears_after(world, fake_skills, 400, monkeypatch)
    agents["codex"].script = [step(close(work), touch(work))]
    go()
    assert waits(store) and agents["codex"].tiers[0] == "build"
    assert all(e["kind"] != "stopped" for e in store.events())


def test_a_stop_request_ends_the_wait(go, agents, store, world, tmp_path, work, monkeypatch):
    monkeypatch.setattr("baton.superpowers.find_skills", lambda cfg=None, home=None: None)
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        store.request_stop()
        world.sleep(seconds)

    assert run_goal(make_goal(tmp_path, work), agents, store, world.clock, sleep) == 0
    assert waits(store) and sum(slept) < 300  # the nap was cut short
    assert store.events()[-1]["kind"] == "stopped"
    assert agents["codex"].prompts == []


def test_skills_over_the_size_cap_make_the_job_wait_with_the_reason(
    go, agents, work, store, world, fake_skills
):
    (fake_skills / "writing-plans/SKILL.md").write_text("x" * (64 * 1024 + 1))
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        if len(slept) > 3:
            store.request_stop()
        world.sleep(seconds)

    run_goal(make_goal_here(work), agents, store, world.clock, sleep)
    assert waits(store) and "64 KB" in waits(store)[0]["reason"]
    assert agents["codex"].prompts == []


def make_goal_here(work):
    return make_goal(work.parent, work)


def sections(prompt):
    return [line for line in prompt.splitlines() if line.startswith("Skill: ")]


def test_each_phase_gets_its_own_skills_in_a_fresh_round(go, agents, work, store):
    codex, agy = agents["codex"], agents["antigravity"]
    codex.script = [step(plan(work, "a")), step(close(work), touch(work))]
    agy.script = [step(review(work, "APPROVED"))]
    go()
    assert "Skill: writing-plans" in codex.prompts[0]
    assert "Skill: systematic-debugging" not in codex.prompts[0]  # no failed gate behind it
    assert "Skill: test-driven-development" in codex.prompts[1]
    assert "Skill: verification-before-completion" in codex.prompts[1]
    assert "Skill: requesting-code-review/code-reviewer.md" in agy.prompts[0]
    assert "TEXT OF writing-plans/SKILL.md" in codex.prompts[0]
    assert "TEXT OF test-driven-development/SKILL.md" not in codex.prompts[0]
    # each vendor gets its own tool names
    assert "codex-tools.md" in codex.prompts[0] and "antigravity-tools.md" not in codex.prompts[0]
    assert "antigravity-tools.md" in agy.prompts[0] and "codex-tools.md" not in agy.prompts[0]
    assert FIXED in codex.prompts[0] and FIXED in agy.prompts[0]


FIXED = "These are working methods. baton owns the workflow"


def test_a_resumed_conversation_is_not_given_the_skills_again(go, agents, work, store, tmp_path):
    codex = agents["codex"]
    codex.script = [step(plan(work, "a", "b")), step(close(work)), step(close(work))]
    agents["antigravity"].script = [step(review(work, "APPROVED"))]
    go(make_goal(tmp_path, work, verify=None))
    assert "Skill: test-driven-development" in codex.prompts[1]  # fresh: first build round
    assert "Your turn again" in codex.prompts[2]
    assert "Skill:" not in codex.prompts[2] and FIXED not in codex.prompts[2]
    # the review is always a fresh conversation, so it always has its skills
    assert "Skill: requesting-code-review" in agents["antigravity"].prompts[0]


def test_a_resumed_plan_after_a_failed_gate_gets_only_systematic_debugging(go, agents, work, store):
    codex, agy = agents["codex"], agents["antigravity"]
    codex.script = [
        step(plan(work, "one")),
        step(close(work)),
        step(plan(work, "repair")),
        step(close(work), touch(work)),
        step(),
        step(),
    ]
    agy.script = [step(review(work, "APPROVED")), step(review(work, "APPROVED"))]
    go()
    # round 3 is the plan that follows the failed gate; its conversation is the first plan one,
    # resumed, so the extra skill is only offered if that conversation is fresh
    assert "Your turn again" in codex.prompts[2]
    # a resumed conversation holds the methods already, except for the one the failed gate needs
    assert sections(codex.prompts[2]) == ["Skill: systematic-debugging"]
    assert "TEXT OF systematic-debugging/SKILL.md" in codex.prompts[2]
    assert "Phase: PLAN" in codex.prompts[2]


def test_the_failed_gate_plan_is_given_debugging_in_a_fresh_conversation(go, agents, work, store):
    # the plan conversation of the first cycle is gone: the other agent plans after the gate
    codex, agy = agents["codex"], agents["antigravity"]
    codex.script = [step(plan(work, "one")), step(close(work))]
    agy.script = [
        step(review(work, "APPROVED")),
        step(plan(work, "repair")),
        step(close(work), touch(work)),
    ]
    from baton.adapters.base import RoundResult as R

    codex.script.append(lambda a, p, c: R("limit", until=2e9))
    go()
    plans = [p for p in agy.prompts if "Phase: PLAN" in p]
    assert plans and "Skill: systematic-debugging" in plans[0]
    assert "Skill: writing-plans" in plans[0]


def test_the_plain_relay_gets_the_build_set(go, agents, work, store, tmp_path):
    codex = agents["codex"]
    codex.script = [step(), step(touch(work))]
    go(make_goal(tmp_path, work, top="phases = false"))
    assert "Skill: test-driven-development" in codex.prompts[0]
    assert "Skill: verification-before-completion" in codex.prompts[0]
    assert "writing-plans" not in codex.prompts[0]
    assert "Your turn again" in codex.prompts[1] and "Skill:" not in codex.prompts[1]


# the subagent guard in the loop


def forbidden(tool="spawn_agent", model="gpt-5-mini"):
    from baton.adapters.base import SUBAGENT

    def run(agent, prompt, ctx):
        ctx.on_subagent(tool, model)
        return RoundResult(SUBAGENT, reason=f"{tool} started a subagent on {model}")

    return run


def test_a_subagent_round_is_a_failed_round_for_the_phase(go, agents, work, store):
    codex = agents["codex"]
    codex.script = [step(plan(work, "a")), forbidden(), step(close(work), touch(work))]
    agents["antigravity"].script = [step(review(work, "APPROVED"))]
    go()
    assert codex.tiers[:3] == ["strong", "build", "build"]  # the build is repeated, same tier
    assert "- [ ] a" in codex.prompts[2]
    ends = [e for e in store.events() if e["kind"] == "round_end"]
    assert [e["outcome"] for e in ends][:3] == ["completed", "subagent", "completed"]
    assert [e["phase"] for e in ends][:3] == ["plan", "build", "build"]
    assert reasons(store)[:2] == ["plan written: 1 open task(s)", "every task is closed"]


def test_a_subagent_round_is_reported_alerted_and_followed_by_a_pause(go, agents, work, store):
    agents["codex"].script = [step(plan(work, "a")), forbidden(), step(close(work), touch(work))]
    agents["antigravity"].script = [step(review(work, "APPROVED"))]
    go()
    events = store.events()
    seen = [e for e in events if e["kind"] == "subagent"]
    assert [(e["agent"], e["tool"], e["model"]) for e in seen] == [
        ("codex", "spawn_agent", "gpt-5-mini")
    ]
    alert = next(e for e in events if e["kind"] == "alert")
    assert alert["outcome"] == "subagent" and alert["agent"] == "codex"
    waits = [e for e in events if e["kind"] == "wait"]
    assert any(w.get("reason") == "the last round ended at once" for w in waits)


def test_three_subagent_rounds_in_a_row_rest_the_agent(go, agents, work, store):
    codex, agy = agents["codex"], agents["antigravity"]
    codex.script = [step(plan(work, "a")), forbidden(), forbidden(), forbidden()]
    agy.script = [step(close(work), touch(work)), step(review(work, "APPROVED"))]
    go()
    # plan, three forbidden rounds, and then the other agent: no fourth forbidden one
    assert [e["agent"] for e in starts(store)][:5] == ["codex"] * 4 + ["antigravity"]
    assert agy.tiers[0] == "build" and "- [ ] a" in agy.prompts[0]
    assert store.events()[-1]["kind"] == "finished"


def test_a_subagent_that_stays_inside_the_pair_changes_nothing(go, agents, work, store):
    def fine(agent, prompt, ctx):
        ctx.on_subagent("spawn_agent", "gpt-6.1-terra")
        return step(close(work), touch(work))(agent, prompt, ctx)

    agents["codex"].script = [step(plan(work, "a")), fine]
    agents["antigravity"].script = [step(review(work, "APPROVED"))]
    go()
    seen = [e for e in store.events() if e["kind"] == "subagent"]
    assert len(seen) == 1 and not any(e["kind"] == "alert" for e in store.events())


def test_the_phase_block_is_read_last_after_the_skills(go, agents, work, store):
    codex, agy = agents["codex"], agents["antigravity"]
    codex.script = [step(plan(work, "a")), step(close(work), touch(work))]
    agy.script = [step(review(work, "APPROVED"))]
    go()
    for prompt, skill, phase in (
        (codex.prompts[0], "Skill: writing-plans", "Phase: PLAN"),
        (codex.prompts[1], "Skill: test-driven-development", "Phase: BUILD"),
        (agy.prompts[0], "Skill: requesting-code-review", "Phase: REVIEW"),
    ):
        assert prompt.index(skill) < prompt.index(FIXED) < prompt.index(phase) or (
            prompt.index(FIXED) < prompt.index(skill) < prompt.index(phase)
        )
        assert prompt.rstrip().endswith(prompt.split(phase, 1)[1].rstrip())  # nothing after it
    assert ".baton/PLAN.md` and `.baton/REVIEW.md` override" in codex.prompts[0]


def test_the_build_prompt_puts_a_persons_note_above_the_task(go, agents, work, store):
    agents["codex"].script = [step(plan(work, "a")), step(close(work), touch(work))]
    agents["antigravity"].script = [step(review(work, "APPROVED"))]
    go()
    assert "note from the person" in agents["codex"].prompts[1].lower()
    assert "outranks" in agents["codex"].prompts[1]


# a phase that never completes still reaches the gate


def test_every_three_rounds_without_a_phase_change_run_the_gate_and_a_pass_finishes(
    go, agents, work, store
):
    from baton.adapters.base import TIMEOUT

    codex = agents["codex"]
    codex.script = [
        step(plan(work, "big task")),
        step(outcome=TIMEOUT),
        step(outcome=TIMEOUT),
        step(touch(work), outcome=TIMEOUT),  # the verify starts passing in the third
    ]
    assert go() == 0
    assert len(codex.prompts) == 4  # no fifth round
    assert [g["passed"] for g in store.events() if g["kind"] == "gate"] == [False, True]
    assert store.events()[-1]["kind"] == "finished"
    assert codex.tiers == ["strong", "build", "build", "build"]  # a timeout resumes the build


def test_eight_rounds_without_closing_a_task_move_on_to_review(go, agents, work, store):
    from baton.adapters.base import TIMEOUT

    codex, agy = agents["codex"], agents["antigravity"]
    codex.script = [step(plan(work, "big task"))] + [step(outcome=TIMEOUT) for _ in range(8)]
    codex.script.append(step(plan(work), touch(work)))  # the plan after the failed gate: done
    agy.script = [step(review(work, "APPROVED"))]
    go()
    assert any("8 rounds" in r for r in reasons(store))
    # gates: the first, after rounds 3 and 6 of the build, at the end of the cycle (failed), and
    # after the plan that found nothing to do
    assert [g["passed"] for g in store.events() if g["kind"] == "gate"] == [False] * 4 + [True]
    assert agy.tiers == ["strong"] and len(codex.prompts) == 10
    # the mid-cycle gates changed no phase
    ends = [e for e in store.events() if e["kind"] == "round_end"]
    assert [e["phase"] for e in ends][:10] == ["plan"] + ["build"] * 8 + ["review"]


def churn(work):
    """Close the first open task and add a new one: a build that never runs out of work."""

    def do():
        path = work / ".baton" / "PLAN.md"
        text = path.read_text().replace("- [ ]", "- [x]", 1)
        path.write_text(text + f"- [ ] more {len(text)}\n")
        (work / "built.txt").write_text(text)
        git(work, "add", "-A")
        git(work, "commit", "-q", "--allow-empty", "-m", "build")

    return do


def test_build_rounds_are_capped_at_three_per_task_plus_five(go, agents, work, store, tmp_path):
    codex, agy = agents["codex"], agents["antigravity"]
    codex.script = [step(plan(work, "a", "b"))] + [step(churn(work)) for _ in range(11)]
    agy.script = [step(review(work, "APPROVED"))]
    go(make_goal(tmp_path, work, verify=None))
    builds = [e for e in store.events() if e["kind"] == "round_end" and e.get("phase") == "build"]
    assert len(builds) == 2 * 3 + 5  # the plan began the build with two open tasks
    assert any("build round cap" in r for r in reasons(store))
    assert len(agy.prompts) == 1 and "Phase: REVIEW" in agy.prompts[0]


# a round cut off by its own cap is not a finished round


def test_a_bounded_round_advances_no_phase(go, agents, work, store):
    bounded = lambda a, p, c: RoundResult(COMPLETED, bounded=True)  # noqa: E731
    codex = agents["codex"]
    codex.script = [bounded, step(touch(work))]
    go()
    assert codex.tiers == [
        "strong",
        "strong",
    ]  # the plan was repeated: the cut-off one counted for nothing
    ends = [e for e in store.events() if e["kind"] == "round_end"]
    assert [e["outcome"] for e in ends] == ["completed", "completed"]
    assert reasons(store)[:1] == ["the plan has no open task: the cycle ends"]


def test_a_bounded_round_does_not_finish_a_goal_without_a_gate(go, agents, work, store, tmp_path):
    bounded = lambda a, p, c: RoundResult(COMPLETED, bounded=True)  # noqa: E731
    for top in ("", "phases = false"):
        agents["codex"].prompts.clear()
        agents["codex"].script = [bounded, step()]
        go(make_goal(tmp_path, work, verify=None, top=top))
        assert len(agents["codex"].prompts) == 2, top  # the second round is the one that finishes


# PLAN.md and REVIEW.md do not outlive their cycle


def test_a_fresh_job_archives_the_plan_and_review_of_an_earlier_goal(go, agents, work, store):
    plan(work, "old task")()
    review(work, "APPROVED\nold")()
    seen = []

    def at_plan(agent, prompt, ctx):
        seen.append(
            ((work / ".baton" / "PLAN.md").exists(), (work / ".baton" / "REVIEW.md").exists())
        )
        return RoundResult(COMPLETED)

    agents["codex"].script = [at_plan, step(touch(work))]
    go()
    assert seen == [(False, False)]
    archived = sorted(p.name for p in (work / ".baton" / "archive").iterdir())
    assert len(archived) == 2 and any(n.endswith("-PLAN.md") for n in archived)
    assert any(n.endswith("-REVIEW.md") for n in archived)
    plan_copy = next(
        p for p in (work / ".baton" / "archive").iterdir() if p.name.endswith("PLAN.md")
    )
    assert "old task" in plan_copy.read_text()


def test_a_message_that_owes_a_round_archives_them_too(go, agents, work, store, tmp_path):
    head = git(work, "rev-parse", "HEAD")
    state = phases.PhaseState(phases.BUILD, head, 1, 0, ["- [ ] a"])
    store.save_state({"round": 4, "workdir": str(work), "phase": state.to_json()})
    plan(work, "a")()
    agents["codex"].script = [step(touch(work))]
    run_goal(make_goal(tmp_path, work), agents, store, lambda: 1e6, lambda s: None, owes_round=True)
    assert not (work / ".baton" / "PLAN.md").exists()
    assert list((work / ".baton" / "archive").glob("*-PLAN.md"))


def test_a_failed_gate_cycle_keeps_the_plan_and_review(go, agents, work, store):
    codex, agy = agents["codex"], agents["antigravity"]
    kept = []

    def replan(agent, prompt, ctx):
        kept.append(
            ((work / ".baton" / "PLAN.md").exists(), (work / ".baton" / "REVIEW.md").exists())
        )
        return step(plan(work, "repair"), touch(work))(agent, prompt, ctx)

    codex.script = [step(plan(work, "one")), step(close(work)), replan, step(close(work))]
    agy.script = [step(review(work, "APPROVED")), step(review(work, "APPROVED"))]
    go()
    assert kept == [(True, True)]
    assert not (work / ".baton" / "archive").exists()


def test_a_new_cycle_after_a_pass_starts_clean_when_a_note_is_owed(go, agents, work, store):
    codex, agy = agents["codex"], agents["antigravity"]
    seen = []

    def second_plan(agent, prompt, ctx):
        seen.append((work / ".baton" / "PLAN.md").exists())
        return RoundResult(COMPLETED)

    codex.script = [
        step(plan(work, "a")),
        step(close(work), touch(work)),
        second_plan,
    ]
    agy.script = [step(review(work, "APPROVED"), lambda: store.say("one more thing"))]
    go()
    assert seen == [False]  # the finished cycle's plan was archived before the next cycle's
    assert list((work / ".baton" / "archive").glob("*-PLAN.md"))


# heads are kept per session key


def test_heads_are_keyed_by_session_key_and_old_state_is_tolerated(go, agents, work, store):
    old_head = git(work, "rev-parse", "HEAD")
    (work / "later.txt").write_text("a commit made after the old head")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "later")
    store.save_state({"round": 2, "workdir": str(work), "heads": {"codex": old_head}})
    codex, agy = agents["codex"], agents["antigravity"]
    codex.script = [step(plan(work, "a")), step(close(work), touch(work))]
    agy.script = [step(review(work, "APPROVED"))]
    go()
    heads = store.load_state()["heads"]
    assert {"codex:plan", "codex:build", "antigravity:review"} <= set(heads)
    assert "Commits made since" not in codex.prompts[0]  # an old, unknown key claims nothing


def test_phases_off_leaves_no_phase_in_state_json(go, agents, work, store, tmp_path):
    agents["codex"].script = [step(plan(work, "a")), step(close(work)), step(touch(work))]
    agents["antigravity"].script = [step(review(work, "APPROVED"))]
    go()
    assert store.load_state()["phase"]
    agents["codex"].script = [step(touch(work))]
    (work / "DONE").unlink(missing_ok=True)
    go(make_goal(tmp_path, work, top="phases = false"))
    state = store.load_state()
    assert "phase" not in state and "builder" not in state and "from_gate" not in state


# fix round 2


def test_a_review_that_sends_the_build_back_renews_its_round_cap(go, agents, work, store, tmp_path):
    codex, agy = agents["codex"], agents["antigravity"]
    codex.script = [step(plan(work, "a")), step(close(work))] + [
        step(churn(work)) for _ in range(12)
    ]
    agy.script = [
        step(review(work, "CHANGES", "b")),  # one new task: the cap is renewed by 1 * 3 + 5
        step(review(work, "APPROVED")),
    ]
    go(make_goal(tmp_path, work, verify=None))
    builds = [e for e in store.events() if e["kind"] == "round_end" and e.get("phase") == "build"]
    # 1 round before the review, then 1 (rounds so far) + 1 task * 3 + 5 = 9 in all
    assert len(builds) == 9
    assert len(agy.prompts) == 2


def test_the_flags_of_the_cycle_are_kept_in_state_json(work, store, agents, world):
    from baton.runner import _Run

    run = _Run(make_goal(work.parent, work), agents, store, world.clock, world.sleep)
    run.cycle_gate, run.archive_next = True, True
    state = run.runner_state()
    assert state["cycle_gate"] is True and state["archive_next"] is True


def stored_state(work, store, phase, **extra):
    store.save_state({"round": 3, "workdir": str(work), "phase": phase.to_json(), **extra})


def test_a_gate_at_startup_counts_as_the_cycle_s_gate_when_the_cycle_had_ended(
    go, agents, work, store
):
    head = git(work, "rev-parse", "HEAD")
    stored_state(work, store, phases.PhaseState(phases.PLAN, head), cycle_gate=True)
    agents["codex"].script = [step(plan(work), touch(work))]
    go()
    assert "verify failed: back to plan" in reasons(store)  # the killed runner's gate failed
    assert store.load_state()["cycle_gate"] is False  # and it is cleared once the gate finished


def test_without_the_flag_a_startup_gate_changes_no_phase(go, agents, work, store):
    head = git(work, "rev-parse", "HEAD")
    stored_state(work, store, phases.PhaseState(phases.PLAN, head), cycle_gate=False)
    agents["codex"].script = [step(plan(work), touch(work))]
    go()
    assert "verify failed: back to plan" not in reasons(store)


def test_a_gate_that_was_stopped_keeps_the_flag(go, agents, work, store, tmp_path):
    head = git(work, "rev-parse", "HEAD")
    stored_state(work, store, phases.PhaseState(phases.PLAN, head), cycle_gate=True)
    store.request_stop(now=True)
    go(make_goal(tmp_path, work, verify="sleep 5"))
    assert store.load_state()["cycle_gate"] is True


def test_a_pending_archive_survives_a_restart(go, agents, work, store):
    head = git(work, "rev-parse", "HEAD")
    plan(work, "left over")()
    stored_state(work, store, phases.PhaseState(phases.PLAN, head), archive_next=True)
    seen = []

    def at_round(agent, prompt, ctx):
        seen.append((work / ".baton" / "PLAN.md").exists())
        return RoundResult(COMPLETED)

    agents["codex"].script = [at_round, step(touch(work))]
    go()
    assert seen == [False] and list((work / ".baton" / "archive").glob("*-PLAN.md"))


def test_a_stored_start_state_is_a_state_not_a_fresh_job(go, agents, work, store):
    """A valid stored phase is resumed even when it equals the start state (base is empty)."""
    stored_state(work, store, phases.PhaseState(phases.PLAN, ""))
    plan(work, "mine")()
    seen = []

    def at_round(agent, prompt, ctx):
        seen.append((work / ".baton" / "PLAN.md").exists())
        return RoundResult(COMPLETED)

    agents["codex"].script = [at_round, step(plan(work), touch(work))]
    go()
    assert seen == [True]  # not archived: this job was already running
    assert not (work / ".baton" / "archive").exists()


def test_a_missing_or_invalid_stored_phase_is_a_fresh_job(go, agents, work, store):
    plan(work, "old")()
    store.save_state({"round": 3, "workdir": str(work), "phase": {"phase": "plan"}})
    agents["codex"].script = [step(touch(work))]
    go()
    assert list((work / ".baton" / "archive").glob("*-PLAN.md"))


def test_parse_tells_a_stored_start_state_from_nothing():
    start = phases.PhaseState()
    assert phases.PhaseState.parse(start.to_json()) == start
    assert phases.PhaseState.parse(None) is None
    assert phases.PhaseState.parse({"phase": "build"}) is None
    assert phases.PhaseState.from_json(None) == start

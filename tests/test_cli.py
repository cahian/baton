import fcntl
import os
import signal
import subprocess
import sys
import time

import pytest

from baton import cli
from baton.adapters.antigravity import AntigravityAdapter
from baton.adapters.codex import CodexAdapter
from baton.cli import build_adapters, main
from baton.config import load_goal
from baton.proc import process_start
from baton.store import AlreadyRunning, Store, state_dir, workdir_lock


def store_for(fakes, name="demo") -> Store:
    return Store(state_dir(name))


# adapters


def test_build_adapters_wires_each_enabled_agent(fakes):
    goal = load_goal(fakes.goal())
    adapters = build_adapters(goal)
    assert set(adapters) == {"codex", "antigravity"}
    assert isinstance(adapters["codex"], CodexAdapter)
    assert isinstance(adapters["antigravity"], AntigravityAdapter)
    assert adapters["codex"].cfg is goal.agents["codex"]


def test_build_adapters_skips_disabled_agents(fakes):
    goal = load_goal(fakes.goal(agy="enabled = false"))
    assert set(build_adapters(goal)) == {"codex"}


# status, say, stop


def test_say_adds_to_the_inbox(fakes, capsys):
    assert main(["say", str(fakes.goal()), "use", "the", "new API"]) == 0
    assert store_for(fakes).read_inbox() == "use the new API\n\n"
    assert not store_for(fakes).take_interrupt()


def test_say_now_requests_interrupt(fakes):
    assert main(["say", str(fakes.goal()), "--now", "switch plans"]) == 0
    store = store_for(fakes)
    assert store.take_interrupt() is True
    assert "switch plans" in store.read_inbox()


def test_say_without_text_is_refused(fakes, capsys):
    assert main(["say", str(fakes.goal()), "   "]) == 2
    assert "empty" in capsys.readouterr().err


def test_goal_sets_the_pinned_goal_of_a_goal_file_or_a_job(fakes, capsys):
    assert main(["goal", str(fakes.goal()), "ship", "the", "parser"]) == 0
    assert store_for(fakes).goal() == "ship the parser"
    assert "goal set" in capsys.readouterr().out
    assert store_for(fakes).read_inbox() == ""  # the pinned goal is in every prompt: no note
    store_for(fakes, "session-ses_1").set_goal("old")
    assert main(["goal", "session-ses_1", "new one"]) == 0
    assert store_for(fakes, "session-ses_1").goal() == "new one"


@pytest.mark.refusals
def test_goal_is_refused_with_phases_off_and_pins_nothing(fakes, capsys):
    assert (
        main(["goal", str(fakes.goal()), "ship", "it"]) == 2
    )  # the fake goal file says phases = false
    assert "a goal needs phases, and phases = false here" in capsys.readouterr().err
    assert store_for(fakes).goal() == ""


@pytest.mark.refusals
def test_goal_is_refused_in_a_directory_that_is_not_a_repository(fakes, capsys):
    path = fakes.goal()
    path.write_text(path.read_text().replace("phases = false", "phases = true"))
    fakes.work.mkdir(exist_ok=True)
    assert main(["goal", str(path), "ship", "it"]) == 2
    assert "a goal needs a git repository" in capsys.readouterr().err
    assert store_for(fakes).goal() == ""


@pytest.mark.refusals
def test_goal_is_accepted_with_phases_in_a_repository(fakes, capsys):
    path = fakes.goal()
    path.write_text(path.read_text().replace("phases = false", "phases = true"))
    fakes.work.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q", str(fakes.work)], check=True)
    assert main(["goal", str(path), "ship", "it"]) == 0
    assert store_for(fakes).goal() == "ship it"


@pytest.mark.refusals
def test_goal_off_is_never_refused(fakes, capsys):
    store_for(fakes).set_goal("something")
    assert main(["goal", str(fakes.goal()), "--off"]) == 0
    assert store_for(fakes).goal() == ""


def test_a_big_goal_is_filed_and_only_its_start_is_pinned(fakes):
    big = "word " * 3000
    assert main(["goal", str(fakes.goal()), big]) == 0
    pinned = store_for(fakes).goal()
    assert len(pinned) < 1500 and "`.baton/notes/" in pinned
    (path,) = list((fakes.work / ".baton" / "notes").iterdir())
    assert path.read_text(encoding="utf-8").strip() == big.strip()


def test_a_big_goal_for_a_job_is_filed_in_the_directory_job_json_names(fakes):
    store = store_for(fakes, "session-ses_2")
    store.save_job({"workdir": str(fakes.work), "session": "ses_2"})
    assert main(["goal", "session-ses_2", "word " * 3000]) == 0
    assert "`.baton/notes/" in store.goal()
    assert len(list((fakes.work / ".baton" / "notes").iterdir())) == 1


def test_goal_off_clears_it(fakes, capsys):
    store_for(fakes).set_goal("something")
    assert main(["goal", str(fakes.goal()), "--off"]) == 0
    assert store_for(fakes).goal() == ""
    assert "goal cleared" in capsys.readouterr().out


def test_goal_needs_a_text_or_off_and_not_both(fakes, capsys):
    goal = str(fakes.goal())
    assert main(["goal", goal]) == 2
    assert "goal" in capsys.readouterr().err
    assert main(["goal", goal, "   "]) == 2
    assert main(["goal", goal, "--off", "and", "text"]) == 2
    assert store_for(fakes).goal() == ""


def test_goal_for_an_unknown_job_is_refused_and_creates_nothing(fakes, capsys):
    assert main(["goal", "session-nope", "x"]) == 2
    assert not state_dir("session-nope").exists()


def test_next_stores_the_request(fakes, capsys):
    goal = str(fakes.goal())
    assert main(["next", goal]) == 0
    store = store_for(fakes)
    assert store.take_interrupt() is False
    assert store.take_next() == "soon"
    assert "next requested" in capsys.readouterr().out


def test_next_now_also_requests_an_interrupt(fakes):
    assert main(["next", str(fakes.goal()), "--now"]) == 0
    store = store_for(fakes)
    assert store.take_interrupt() is True
    assert store.take_next() == "now"


def test_a_broken_defaults_file_does_not_block_goal_or_next(fakes):
    goal = fakes.goal()
    fakes.defaults("[agents.codex]\nthreshold_pct = 'high'\n")
    assert main(["goal", str(goal), "still", "here"]) == 0
    assert main(["next", str(goal)]) == 0
    assert store_for(fakes).goal() == "still here"


BROKEN_DEFAULTS = "[agents.codex]\nthreshold_pct = 'high'\n"


def test_a_broken_defaults_file_does_not_block_stop(fakes, capsys):
    goal = fakes.goal()
    fakes.defaults(BROKEN_DEFAULTS)
    assert main(["stop", str(goal), "--now"]) == 0
    assert Store(state_dir("demo")).stop_request() == "now"


def test_a_broken_defaults_file_does_not_block_status(fakes, capsys):
    goal = fakes.goal()
    Store(state_dir("demo")).save_state({"round": 3})
    fakes.defaults(BROKEN_DEFAULTS)
    assert main(["status", str(goal)]) == 0
    out = capsys.readouterr().out
    assert "goal demo" in out and "round 3" in out and str(fakes.work) in out


def test_a_broken_defaults_file_does_not_block_say(fakes):
    goal = fakes.goal()
    fakes.defaults(BROKEN_DEFAULTS)
    assert main(["say", str(goal), "still", "here"]) == 0
    assert "still here" in Store(state_dir("demo")).read_inbox()


def test_a_broken_defaults_file_still_blocks_run(fakes, capsys):
    goal = fakes.goal()
    fakes.defaults(BROKEN_DEFAULTS)
    assert main(["run", str(goal)]) == 2


def test_stop_still_needs_a_goal_file_it_can_name(fakes, capsys):
    bad = fakes.root / "bad.toml"
    bad.write_text('workdir = "/tmp"\n')
    assert main(["stop", str(bad)]) == 2
    assert "name" in capsys.readouterr().err


def test_run_is_refused_in_the_directory_of_a_session(fakes, capsys):
    goal = fakes.goal()
    with workdir_lock(fakes.work, "session-abc"):
        assert main(["run", str(goal)]) == 1
    assert "session-abc" in capsys.readouterr().err
    assert fakes.codex_calls() == []


def test_run_is_refused_in_a_subdirectory_of_a_session(fakes, capsys):
    goal = fakes.goal()
    with workdir_lock(fakes.work.parent, "session-abc"):
        assert main(["run", str(goal)]) == 1
    assert "session-abc" in capsys.readouterr().err


def test_say_and_stop_commands(fakes):
    goal = str(fakes.goal())
    assert main(["stop", goal]) == 0
    assert store_for(fakes).stop_request() == "soon"
    assert main(["stop", goal, "--now"]) == 0
    assert store_for(fakes).stop_request() == "now"
    assert main(["stop", goal]) == 0  # never downgraded
    assert store_for(fakes).stop_request() == "now"


def test_status_prints_cooldowns(fakes, capsys):
    store = store_for(fakes)
    until = time.time() + 3600
    store.save_state(
        {
            "round": 4,
            "current": "antigravity",
            "group": None,
            "agents": {
                "codex": {
                    "status": "cooldown",
                    "until": until,
                    "limit_n": 1,
                    "transient_n": 0,
                    "reason": "primary window at 95%",
                    "usage": {},
                },
                "antigravity": {
                    "status": "ready",
                    "until": 0.0,
                    "limit_n": 0,
                    "transient_n": 0,
                    "reason": "",
                    "usage": {},
                },
            },
        }
    )
    store.emit("round_end", round=4, agent="codex", outcome="limit")
    assert main(["status", str(fakes.goal())]) == 0
    out = capsys.readouterr().out
    assert "demo" in out and "round 4" in out
    assert "codex" in out and "cooldown" in out and "primary window at 95%" in out
    assert "antigravity" in out and "current" in out
    assert "not running" in out
    assert "limit" in out  # the last event


def test_status_says_in_a_sentence_which_agent_came_back(fakes, capsys):
    store = store_for(fakes)
    store.save_state({"round": 4, "current": "claude_api", "agents": {}})
    store.emit(
        "round_end",
        round=4,
        agent="claude_api",
        outcome="preempted",
        reason="codex is available again",
        back="codex",
        seconds=12.0,
    )
    assert main(["status", str(fakes.goal())]) == 0
    out = capsys.readouterr().out
    assert "round 4: claude_api stopped because codex is available again" in out
    assert "outcome=preempted" not in out


def test_status_shows_memory_events_through_its_generic_line(fakes, capsys):
    store = store_for(fakes)
    store.save_state({"round": 2, "current": "codex", "agents": {}})
    store.emit("memory", event="recalled", hits=3, chars=900)
    store.emit("memory", event="error", reason="memory is unavailable: gone")
    assert main(["status", str(fakes.goal())]) == 0
    out = capsys.readouterr().out
    assert "memory event=recalled hits=3 chars=900" in out
    assert "memory event=error reason=memory is unavailable: gone" in out


def test_status_does_not_print_the_fragment_flag_of_an_output_event(fakes, capsys):
    store = store_for(fakes)
    store.save_state({"round": 3, "current": "antigravity", "agents": {}})
    store.emit("output", agent="antigravity", round=3, text="rende", fragment=True)
    assert main(["status", str(fakes.goal())]) == 0
    out = capsys.readouterr().out
    assert "output agent=antigravity round=3" in out
    assert "fragment" not in out


def test_status_says_when_the_daily_preemption_cap_is_active(fakes, capsys):
    store = store_for(fakes)
    now = time.time()
    first = now - 3600
    store.save_state({"round": 9, "agents": {}, "preempts": [first + i for i in range(6)]})
    assert main(["status", str(fakes.goal())]) == 0
    out = capsys.readouterr().out
    assert "ended 6 times in the last 24 hours" in out
    assert cli._when(first + 24 * 3600) in out


def test_status_is_silent_about_the_cap_below_it_or_with_old_or_damaged_times(fakes, capsys):
    store = store_for(fakes)
    now = time.time()
    for preempts in ([now - i for i in range(5)], [now - 90_000 - i for i in range(6)], "oops"):
        store.save_state({"round": 9, "agents": {}, "preempts": preempts})
        assert main(["status", str(fakes.goal())]) == 0
        assert "preempt" not in capsys.readouterr().out.lower()


def test_status_reports_a_live_runner(fakes, capsys):
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time; from baton.store import Store; "
            "s = Store(sys.argv[1]); l = s.lock(); l.__enter__(); print('held', flush=True); "
            "time.sleep(60)  # a stand-in baton runner",
            str(state_dir("demo")),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        assert main(["status", str(fakes.goal())]) == 0
        out = capsys.readouterr().out
        assert f"running (pid {holder.pid})" in out and "not running" not in out
    finally:
        holder.kill()
        holder.wait()
        holder.stdout.close()


def test_status_never_touches_the_lock(fakes, monkeypatch):
    # A probe that took the lock, even for a moment, could make a starting runner be refused.
    store = store_for(fakes)
    store.save_state({"round": 1})
    taken = []
    monkeypatch.setattr(fcntl, "flock", lambda *a: taken.append(a))
    assert main(["status", str(fakes.goal())]) == 0
    assert taken == []


def test_status_of_a_finished_runner_says_not_running(fakes, capsys):
    store = store_for(fakes)
    with store.lock():
        pass  # the pid file is emptied on release
    assert main(["status", str(fakes.goal())]) == 0
    assert "not running" in capsys.readouterr().out


def test_status_of_an_unseen_goal_creates_nothing(fakes, capsys):
    assert main(["status", str(fakes.goal(name="fresh"))]) == 0
    assert "no state yet" in capsys.readouterr().out
    assert not (fakes.state / "fresh").exists()


def test_bad_goal_file_exits_2(fakes, capsys):
    path = fakes.root / "bad.toml"
    path.write_text('name = "x"\n')
    assert main(["status", str(path)]) == 2
    assert "workdir" in capsys.readouterr().err


def test_missing_goal_file_exits_2(fakes, capsys):
    assert main(["run", str(fakes.root / "nope.toml")]) == 2
    assert "cannot read" in capsys.readouterr().err


def test_no_command_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


# doctor


def test_doctor_exits_0_when_healthy(fakes, capsys):
    assert main(["doctor", str(fakes.goal())]) == 0
    out = capsys.readouterr().out
    assert "codex limits" in out and "antigravity limits" in out


def test_doctor_without_goal(fakes, capsys):
    assert main(["doctor"]) == 0


def test_doctor_exits_1_on_a_blocking_problem(fakes, capsys):
    assert main(["doctor", str(fakes.goal(codex='bin = "/nonexistent/codex"'))]) == 1
    assert "/nonexistent/codex" in capsys.readouterr().out


def test_doctor_prints_the_variable_never_its_value(fakes, monkeypatch, capsys):
    monkeypatch.setenv("GEMINI_API_KEY", "super-secret-gemini")
    assert main(["doctor", str(fakes.goal())]) == 0  # a warning does not fail doctor
    out = capsys.readouterr()
    assert "GEMINI_API_KEY" in out.out
    assert "super-secret-gemini" not in out.out + out.err


# run


def test_second_runner_is_refused(fakes, capsys):
    store = store_for(fakes)
    with store.lock():
        assert main(["run", str(fakes.goal(verify="true"))]) == 1
    assert "another baton runner" in capsys.readouterr().err


def test_run_does_not_wrap_run_goal_in_the_lock(fakes, monkeypatch):
    # run_goal takes the lock itself; a CLI that held it too would refuse itself.
    seen = {}

    def fake_run_goal(goal, adapters, store):
        with store.lock():  # would raise AlreadyRunning if main() already held it
            seen["ok"] = True
        return 0

    monkeypatch.setattr(cli, "run_goal", fake_run_goal)
    assert main(["run", str(fakes.goal())]) == 0 and seen["ok"]


def test_run_installs_and_restores_signal_handlers(fakes, monkeypatch):
    before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    seen = {}

    def fake_run_goal(goal, adapters, store):
        seen.update({s: signal.getsignal(s) for s in before})
        return 0

    monkeypatch.setattr(cli, "run_goal", fake_run_goal)
    assert main(["run", str(fakes.goal())]) == 0
    assert all(seen[s] != before[s] for s in before)
    assert {s: signal.getsignal(s) for s in before} == before


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_a_signal_ends_the_run_with_its_conventional_code(fakes, monkeypatch, sig):
    def fake_run_goal(goal, adapters, store):
        os.kill(os.getpid(), sig)
        time.sleep(5)
        raise AssertionError("the signal did not interrupt the run")

    monkeypatch.setattr(cli, "run_goal", fake_run_goal)
    assert main(["run", str(fakes.goal())]) == 128 + sig
    kinds = [e["kind"] for e in store_for(fakes).events()]
    assert "terminated" in kinds


def test_a_second_signal_during_shutdown_is_ignored(fakes, monkeypatch):
    def fake_run_goal(goal, adapters, store):
        try:
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(5)
        finally:
            os.kill(os.getpid(), signal.SIGINT)  # cleanup must not be interrupted again
            time.sleep(0.2)

    monkeypatch.setattr(cli, "run_goal", fake_run_goal)
    assert main(["run", str(fakes.goal())]) == 128 + signal.SIGTERM


def test_caffeinate_holds_the_machine_awake_on_macos(fakes, monkeypatch):
    log = fakes.root / "caffeinate.args"
    pidfile = fakes.root / "caffeinate.pid"
    wrapper = fakes.bindir / "caffeinate"
    wrapper.write_text(f'#!/bin/sh\necho "$@" > "{log}"\necho $$ > "{pidfile}"\nexec sleep 60\n')
    wrapper.chmod(0o755)
    monkeypatch.setattr(sys, "platform", "darwin")
    seen = {}

    def fake_run_goal(goal, adapters, store):
        for _ in range(100):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            time.sleep(0.05)
        seen["pid"] = int(pidfile.read_text())
        return 0

    monkeypatch.setattr(cli, "run_goal", fake_run_goal)
    assert main(["run", str(fakes.goal())]) == 0
    assert log.read_text().split() == ["-is", "-w", str(os.getpid())]
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(seen["pid"], 0)  # it does not outlive the run


def test_no_caffeinate_off_macos(fakes, monkeypatch):
    log = fakes.root / "caffeinate.args"
    wrapper = fakes.bindir / "caffeinate"
    wrapper.write_text(f'#!/bin/sh\necho "$@" > "{log}"\n')
    wrapper.chmod(0o755)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(cli, "run_goal", lambda goal, adapters, store: 0)
    assert main(["run", str(fakes.goal())]) == 0
    assert not log.exists()


def test_a_missing_caffeinate_does_not_stop_the_run(fakes, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("PATH", str(fakes.bindir))  # no caffeinate there
    monkeypatch.setattr(cli, "run_goal", lambda goal, adapters, store: 0)
    assert main(["run", str(fakes.goal())]) == 0


def test_run_reports_unexpected_failures_without_a_traceback(fakes, monkeypatch, capsys):
    def boom(goal, adapters, store):
        raise AlreadyRunning("another baton runner holds x")

    monkeypatch.setattr(cli, "run_goal", boom)
    assert main(["run", str(fakes.goal())]) == 1


def test_module_entry_point_runs(fakes):
    out = subprocess.run(
        [sys.executable, "-m", "baton.cli", "stop", str(fakes.goal())],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    assert out.returncode == 0, out.stderr


def test_refused_runner_leaves_the_live_agent_alone(fakes, capsys):
    # The stale-group kill belongs after the lock: before it, a second runner that is about to be
    # refused would kill the agent of the runner that is working.
    store = store_for(fakes)
    live = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        store.save_state(
            {
                "group": live.pid,
                "group_start": process_start(live.pid),
                "workdir": str(fakes.work),
            }
        )
        with store.lock():
            assert main(["run", str(fakes.goal(verify="true"))]) == 1
        time.sleep(0.3)
        assert live.poll() is None, "a refused runner killed the live runner's agent"
        assert store.load_state()["group"] == live.pid
    finally:
        live.kill()
        live.wait()


def test_forget_group_clears_a_dead_group_under_the_lock(fakes):
    store = store_for(fakes)
    child = subprocess.Popen(["true"], start_new_session=True)
    child.wait()
    store.save_state({"group": child.pid, "group_start": "x", "round": 3})
    cli._forget_group(store)
    state = store.load_state()
    assert state["group"] is None and state["group_start"] is None and state["round"] == 3


def test_forget_group_keeps_a_live_group(fakes):
    store = store_for(fakes)
    live = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        store.save_state({"group": live.pid, "group_start": "x"})
        cli._forget_group(store)
        assert store.load_state()["group"] == live.pid
    finally:
        live.kill()
        live.wait()


def test_forget_group_does_not_write_while_another_runner_holds_the_lock(fakes):
    store = store_for(fakes)
    child = subprocess.Popen(["true"], start_new_session=True)
    child.wait()
    store.save_state({"group": child.pid, "round": 3})
    with Store(store.root).lock():
        cli._forget_group(store)
    assert store.load_state()["group"] == child.pid  # not our file to write any more


def test_an_ignored_sighup_stays_ignored(fakes, monkeypatch):
    # `nohup baton run` must survive the terminal closing.
    before = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    seen = {}

    def fake_run_goal(goal, adapters, store):
        seen["handler"] = signal.getsignal(signal.SIGHUP)
        os.kill(os.getpid(), signal.SIGHUP)
        time.sleep(0.2)
        return 0

    try:
        monkeypatch.setattr(cli, "run_goal", fake_run_goal)
        assert main(["run", str(fakes.goal())]) == 0
        assert seen["handler"] == signal.SIG_IGN
        assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
    finally:
        signal.signal(signal.SIGHUP, before)


def test_build_adapters_adds_claude_api_only_when_enabled(fakes):
    from baton.adapters.claude_api import ClaudeApiAdapter
    from baton.cli import build_adapters
    from baton.config import load_goal

    off = build_adapters(load_goal(fakes.goal()))
    assert set(off) == {"codex", "antigravity"}
    on = build_adapters(load_goal(fakes.goal(claude="")))
    assert set(on) == {"codex", "antigravity", "claude_api"}
    adapter = on["claude_api"]
    assert isinstance(adapter, ClaudeApiAdapter)
    assert adapter._cost_report is None  # no admin key file
    assert adapter.ledger.path == fakes.ledger_file  # one ledger for every goal
    assert adapter.ceiling == 180


def test_build_adapters_reads_the_cost_report_when_an_admin_key_is_set(fakes):
    from baton.cli import build_adapters
    from baton.config import load_goal

    goal = load_goal(fakes.goal(claude='admin_key_file = "/some/admin.key"\n'))
    assert build_adapters(goal)["claude_api"]._cost_report is not None


# jobs, and commands that take a job name


def make_job(name="session-abc", **state):
    store = Store(state_dir(name))
    store.save_state({"round": 3, "current": "codex", **state})
    return store


def test_jobs_lists_every_job_with_its_status(fakes, capsys):
    make_job("session-abc").emit("round_end", round=3, agent="codex", outcome="limit")
    done = make_job("session-def", round=7, current="antigravity")
    done.emit("finished", reason="gate passed", round=7)
    (fakes.state / "claude-spend.json").write_text("{}")  # the shared ledger is not a job
    (fakes.state / "stray").mkdir()  # nor is a directory with no state in it
    assert main(["jobs"]) == 0
    out = capsys.readouterr().out
    lines = {line.split()[0]: line for line in out.splitlines() if line.startswith("session-")}
    assert set(lines) == {"session-abc", "session-def"}
    assert "not running" in lines["session-abc"] and "codex" in lines["session-abc"]
    assert "finished" in lines["session-def"] and "antigravity" in lines["session-def"]
    assert "stray" not in out and "claude-spend" not in out


def test_jobs_says_when_there_are_none(fakes, capsys):
    assert main(["jobs"]) == 0
    assert "no jobs" in capsys.readouterr().out


def test_jobs_shows_a_live_runner(fakes, capsys):
    make_job("session-live")
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time; from baton.store import Store; "
            "s = Store(sys.argv[1]); l = s.lock(); l.__enter__(); print('held', flush=True); "
            "time.sleep(60)  # a stand-in baton runner",
            str(state_dir("session-live")),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        assert main(["jobs"]) == 0
        out = capsys.readouterr().out
        assert f"running (pid {holder.pid})" in out
    finally:
        holder.kill()
        holder.wait()
        holder.stdout.close()


def test_status_takes_a_job_name(fakes, capsys):
    make_job("session-abc").emit("round_end", round=3, agent="codex", outcome="limit")
    assert main(["status", "session-abc"]) == 0
    out = capsys.readouterr().out
    assert "session-abc" in out and "round 3" in out and "limit" in out


def test_say_and_stop_take_a_job_name(fakes):
    store = make_job("session-abc")
    assert main(["say", "--now", "session-abc", "use", "the", "new API"]) == 0
    assert store.take_interrupt() is True
    assert "use the new API" in store.read_inbox()
    assert main(["stop", "session-abc", "--now"]) == 0
    assert store.stop_request() == "now"


@pytest.mark.parametrize("command", [["status"], ["say", "hi"], ["stop"]])
def test_an_unknown_job_name_is_refused_and_creates_nothing(fakes, capsys, command):
    argv = [command[0], "session-nope", *command[1:]]
    assert main(argv) == 2
    assert "baton jobs" in capsys.readouterr().err
    assert not (fakes.state / "session-nope").exists()


def test_a_goal_path_still_wins_over_a_job_name(fakes, capsys):
    make_job("demo", round=99)
    assert main(["status", str(fakes.goal())]) == 0  # a path to a goal file, not the job "demo"
    assert "round 99" in capsys.readouterr().out  # same state dir, read through the goal
    assert main(["status", "demo"]) == 0


def test_status_shows_a_verify_command_that_cannot_run(fakes, capsys):
    goal = fakes.goal()
    store_for(fakes).save_state(
        {"verify_warning": "verify exited 127: not found or not executable"}
    )
    assert main(["status", str(goal)]) == 0
    assert "warning: verify exited 127: not found or not executable" in capsys.readouterr().out


def test_status_says_nothing_when_verify_runs(fakes, capsys):
    goal = fakes.goal()
    store_for(fakes).save_state({"verify_warning": ""})
    assert main(["status", str(goal)]) == 0
    assert "warning" not in capsys.readouterr().out


def test_status_shows_the_turn_and_the_dropped_agents(fakes, capsys):
    store = store_for(fakes)
    store.save_state(
        {
            "round": 9,
            "current": "claude_api",
            "agents": {},
            "turn": {"agent": "claude_api", "worked_s": 9000, "spent": 12.5},
            "dropped": ["claude_api", "codex"],
        }
    )
    assert main(["status", str(fakes.goal())]) == 0
    out = capsys.readouterr().out
    assert "turn: claude_api, 2.5 hours worked, USD 12.50 spent" in out
    assert "left out of the queue for this goal: claude_api, codex" in out


def test_status_is_silent_about_a_turn_that_has_not_begun_or_is_damaged(fakes, capsys):
    store = store_for(fakes)
    for state in (
        {"round": 1, "agents": {}},
        {"round": 1, "agents": {}, "turn": {"agent": "", "worked_s": 0, "spent": 0}, "dropped": []},
        {"round": 1, "agents": {}, "turn": "oops", "dropped": "oops"},
        {"round": 1, "agents": {}, "turn": {"agent": "codex", "worked_s": "x", "spent": None}},
    ):
        store.save_state(state)
        assert main(["status", str(fakes.goal())]) == 0
        out = capsys.readouterr().out
        assert "turn:" not in out and "left out" not in out

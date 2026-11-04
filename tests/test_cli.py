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
from baton.store import AlreadyRunning, Store, state_dir


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
    assert store_for(fakes).take_inbox() == "use the new API\n\n"
    assert not store_for(fakes).take_interrupt()


def test_say_now_requests_interrupt(fakes):
    assert main(["say", str(fakes.goal()), "--now", "switch plans"]) == 0
    store = store_for(fakes)
    assert store.take_interrupt() is True
    assert "switch plans" in store.take_inbox()


def test_say_without_text_is_refused(fakes, capsys):
    assert main(["say", str(fakes.goal()), "   "]) == 2
    assert "empty" in capsys.readouterr().err


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


def test_serve_and_opencode_config_are_not_there_yet(capsys):
    assert main(["serve"]) == 2
    assert main(["opencode-config"]) == 2
    assert "not implemented" in capsys.readouterr().err


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
    assert adapter.ledger.path == fakes.state / "claude-spend.json"  # shared by every goal
    assert adapter.ceiling == 180


def test_build_adapters_reads_the_cost_report_when_an_admin_key_is_set(fakes):
    from baton.cli import build_adapters
    from baton.config import load_goal

    goal = load_goal(fakes.goal(claude='admin_key_file = "/some/admin.key"\n'))
    assert build_adapters(goal)["claude_api"]._cost_report is not None

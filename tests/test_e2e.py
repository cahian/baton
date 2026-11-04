"""The whole flow through `main`, with the fake CLIs standing in for both vendors."""

import os
import signal
import subprocess
import sys
import threading
import time
from decimal import Decimal

import pytest
from conftest import codex_limits

from baton.cli import main
from baton.proc import process_start
from baton.store import Store, state_dir


def events(fakes, name="demo") -> list[dict]:
    return Store(state_dir(name)).events()


def timeline(fakes) -> list[tuple]:
    out = []
    for e in events(fakes):
        if e["kind"] == "round_start":
            out.append(("start", e["agent"]))
        elif e["kind"] == "round_end":
            out.append(("end", e["agent"], e["outcome"]))
        elif e["kind"] in ("gate", "finished"):
            out.append((e["kind"],))
    return out


def gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def wait_for(predicate, seconds=20.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def agy_ran(fakes) -> str:
    # The gate: it passes once the Antigravity fake has been asked to work.
    return f"test -f {fakes.agy_calls_file}"


def test_e2e_rotation_with_fake_clis(fakes, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_EXEC_SLEEP", "30")
    # reads: availability, the baseline before the round, then the first poll finds 95% used
    fakes.codex_scenario(codex_limits(12), codex_limits(12), codex_limits(95))
    goal = fakes.goal(verify=agy_ran(fakes))
    assert main(["run", str(goal)]) == 0
    assert timeline(fakes) == [
        ("gate",),
        ("start", "codex"),
        ("end", "codex", "limit"),
        ("gate",),
        ("start", "antigravity"),
        ("end", "antigravity", "completed"),
        ("gate",),
        ("finished",),
    ]
    assert len(fakes.codex_calls()) == 1 and len(fakes.agy_calls()) == 1
    gates = [e["passed"] for e in events(fakes) if e["kind"] == "gate"]
    assert gates == [False, False, True]
    # The second agent was briefed with the objective, and nothing was spent.
    assert "frobnicate" in fakes.agy_calls()[0]["argv"][1]
    state = Store(state_dir("demo")).load_state()
    assert state["current"] == "antigravity" and state["group"] is None


def test_e2e_credit_drop_kills_codex_process(fakes, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_EXEC_SLEEP", "60")
    fakes.codex_scenario(
        codex_limits(balance="100.00"),
        codex_limits(balance="100.00"),
        codex_limits(balance="99.99"),  # the first poll sees a debit
    )
    assert main(["run", str(fakes.goal(verify=agy_ran(fakes)))]) == 0
    pid = int(fakes.codex_pidfile.read_text())
    assert gone(pid), "the credit guard must kill the codex process, not just stop watching it"
    ends = [e for e in events(fakes) if e["kind"] == "round_end"]
    assert [(e["agent"], e["outcome"]) for e in ends] == [
        ("codex", "credit_guard"),
        ("antigravity", "completed"),
    ]
    alerts = [e for e in events(fakes) if e["kind"] == "alert"]
    assert any(a.get("outcome") == "credit_guard" and "dropped" in a["reason"] for a in alerts)
    assert Decimal("100.00") > Decimal("99.99")  # the guard compared exact values


def stray(fakes):
    """A process in its own group, as a runner killed with SIGKILL would leave behind."""
    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
    threading.Thread(target=proc.wait, daemon=True).start()  # reap it; the runner is not its parent
    return proc


def test_startup_kills_stale_group(fakes):
    proc = stray(fakes)
    try:
        Store(state_dir("demo")).save_state(
            {"group": proc.pid, "group_start": process_start(proc.pid), "workdir": str(fakes.work)}
        )
        assert main(["run", str(fakes.goal(verify="true"))]) == 0
        assert wait_for(lambda: proc.poll() is not None), "the stale group is still running"
        assert any(
            e["kind"] == "alert" and "left by a previous run" in e["message"] for e in events(fakes)
        )
    finally:
        if proc.poll() is None:
            proc.kill()


def test_startup_leaves_a_recycled_group_id_alone(fakes):
    # Same number, different process: the recorded start time does not match.
    proc = stray(fakes)
    try:
        Store(state_dir("demo")).save_state(
            {
                "group": proc.pid,
                "group_start": "Mon Jan  1 00:00:00 2001",
                "workdir": str(fakes.work),
            }
        )
        assert main(["run", str(fakes.goal(verify="true"))]) == 0
        time.sleep(0.5)
        assert proc.poll() is None, "a process that is not ours was killed"
        assert any(e["kind"] == "alert" and "not killed" in e["message"] for e in events(fakes))
    finally:
        proc.kill()


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_signal_stops_the_agent_group_and_exits(fakes, monkeypatch, sig):
    monkeypatch.setenv("FAKE_CODEX_EXEC_SLEEP", "60")
    goal = fakes.goal(verify="test -f /nonexistent/never")
    proc = subprocess.Popen(
        [sys.executable, "-m", "baton.cli", "run", str(goal)],
        env=os.environ.copy(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        assert wait_for(lambda: fakes.codex_pidfile.exists() and fakes.codex_pidfile.read_text())
        pid = int(fakes.codex_pidfile.read_text())
        assert wait_for(lambda: Store(state_dir("demo")).load_state().get("group") == pid)
        proc.send_signal(sig)
        assert proc.wait(timeout=30) == 128 + sig
        assert gone(pid), "the agent outlived baton"
        state = Store(state_dir("demo")).load_state()
        assert state["group"] is None
        assert any(e["kind"] == "terminated" for e in events(fakes))
        # and the lock is free again
        with Store(state_dir("demo")).lock():
            pass
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.stderr.close()

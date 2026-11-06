import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest

from baton import subagent_guard
from baton.adapters.base import (
    AUTH,
    COMPLETED,
    CREDIT_GUARD,
    LIMIT,
    TRANSIENT,
    RoundContext,
)
from baton.adapters.codex import (
    CodexAdapter,
    LimitsError,
    check_limits,
    parse_balance,
    read_limits,
)
from baton.config import AgentConfig

FAKE = Path(__file__).parent / "fakes" / "fake_codex.py"
NOW = 1_700_000_000.0


def limits(
    primary=12,
    secondary=31,
    balance="1000.5000000000",
    allowed=True,
    reached=None,
    primary_reset=NOW + 1000,
    secondary_reset=NOW + 2000,
):
    """A successful `account/rateLimits/read` result."""
    rl = {
        "primary": None
        if primary is None
        else {"usedPercent": primary, "windowDurationMins": 300, "resetsAt": primary_reset},
        "secondary": None
        if secondary is None
        else {"usedPercent": secondary, "windowDurationMins": 10080, "resetsAt": secondary_reset},
        "credits": {"hasCredits": True, "unlimited": False, "balance": balance},
        "rateLimitReachedType": reached,
        "planType": "plus",
    }
    out = {"rateLimits": rl}
    if allowed is not None:
        out["ordinaryUsageAllowed"] = allowed
    return out


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """A `codex` executable that runs the fake, plus the files it writes."""
    wrapper = tmp_path / "codex"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    wrapper.chmod(0o755)
    calls = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_CODEX_CALLS", str(calls))
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", str(tmp_path / "scenario.json"))
    monkeypatch.setenv("FAKE_CODEX_RPC", str(tmp_path / "rpc.log"))
    monkeypatch.setenv("FAKE_CODEX_PIDFILE", str(tmp_path / "pid"))

    class Fake:
        bin = str(wrapper)
        pidfile = tmp_path / "pid"
        rpc = tmp_path / "rpc.log"

        @staticmethod
        def scenario(*entries):
            (tmp_path / "scenario.json").write_text(json.dumps(list(entries)))

        @staticmethod
        def exec_calls():
            if not calls.exists():
                return []
            return [json.loads(x) for x in calls.read_text().splitlines()]

    return Fake


def make_cfg(bin_path="codex", **kw) -> AgentConfig:
    d = dict(
        name="codex",
        enabled=True,
        model="gpt-6.1-sol",
        effort="medium",
        build_model="gpt-6.1-terra",
        build_effort="high",
        threshold_pct=90,
        poll_s=15,
        bin=bin_path,
        full_access=True,
    )
    d.update(kw)
    return AgentConfig(**d)


def make_ctx(tmp_path: Path, **kw) -> RoundContext:
    d = dict(
        workdir=tmp_path,
        log_path=tmp_path / "logs" / "1-codex.log",
        round_max_s=30,
        idle_s=30,
        on_output=lambda line: None,
        should_kill=lambda: False,
        on_spawn=lambda pid: None,
    )
    d.update(kw)
    return RoundContext(**d)


class Reads:
    """A scripted `read`: successive results, the last one repeats; an Exception is raised."""

    def __init__(self, *entries):
        self.entries = list(entries)
        self.calls = 0

    def __call__(self):
        entry = self.entries[min(self.calls, len(self.entries) - 1)]
        self.calls += 1
        if isinstance(entry, Exception):
            raise entry
        return entry


class Clock:
    """A poll clock that jumps a full poll interval every time it is read."""

    def __init__(self, step=100.0):
        self.t = 0.0
        self.step = step

    def __call__(self):
        self.t += self.step
        return self.t


def adapter(bin_path, reads, **kw) -> CodexAdapter:
    a = CodexAdapter(make_cfg(bin_path, **kw), read=reads, poll_clock=Clock())
    a.tick_s = 0.05
    return a


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_dead(pid: int, timeout: float = 5) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return False


# --- read_limits -----------------------------------------------------------------------------


def test_read_limits_parses_fake_server(fake):
    good = limits()
    fake.scenario(good)
    assert read_limits(fake.bin, timeout_s=10) == good
    sent = [json.loads(x) for x in fake.rpc.read_text().splitlines()]
    assert sent == [
        {
            "id": 1,
            "method": "initialize",
            "params": {"clientInfo": {"name": "baton", "title": "baton", "version": "1"}},
        },
        {"method": "initialized", "params": {}},
        {"id": 2, "method": "account/rateLimits/read"},
    ]


def test_read_limits_garbage_raises(fake):
    fake.scenario("garbage")
    with pytest.raises(LimitsError):
        read_limits(fake.bin, timeout_s=10)


def test_read_limits_rpc_error_raises(fake):
    fake.scenario("rpc_error")
    with pytest.raises(LimitsError, match="boom"):
        read_limits(fake.bin, timeout_s=10)


def test_read_limits_hang_times_out_and_kills_server(fake):
    fake.scenario("hang")
    t0 = time.monotonic()
    with pytest.raises(LimitsError, match="did not answer"):
        read_limits(fake.bin, timeout_s=0.5)
    assert time.monotonic() - t0 < 5
    assert wait_dead(int(fake.pidfile.read_text()))


def test_read_limits_flooding_server_still_times_out(fake, monkeypatch):
    from baton.adapters import codex

    # A consumer slower than the flood keeps the queue non-empty, which is when a deadline that
    # is only checked on an empty queue never fires.
    real_loads = codex._loads

    def slow_loads(raw):
        time.sleep(0.01)
        return real_loads(raw)

    monkeypatch.setattr(codex, "_loads", slow_loads)
    fake.scenario("flood")
    t0 = time.monotonic()
    with pytest.raises(LimitsError, match="did not answer"):
        read_limits(fake.bin, timeout_s=1)
    assert time.monotonic() - t0 < 5
    assert wait_dead(int(fake.pidfile.read_text()))


def test_read_limits_missing_binary_raises(tmp_path):
    with pytest.raises(LimitsError):
        read_limits(str(tmp_path / "nope"))


def test_read_limits_keeps_float_balance_exact(fake, tmp_path):
    # A balance sent as a JSON number must never pass through binary float.
    raw = '{"rateLimits": {"credits": {"balance": 100.0000000000000001}}}'
    fake.scenario({"__raw_result__": raw})
    result = read_limits(fake.bin, timeout_s=10)
    assert parse_balance(result) == Decimal("100.0000000000000001")


# --- check_limits ----------------------------------------------------------------------------


def test_check_limits_ok():
    a = check_limits(limits(), 90, NOW)
    assert a.ok and a.until is None
    assert a.usage == {
        "primary_pct": 12,
        "secondary_pct": 31,
        "balance": "1000.5000000000",
        "plan": "plus",
    }


def test_check_limits_ordinary_usage_false():
    a = check_limits(limits(allowed=False), 90, NOW)
    assert not a.ok and "not allowed" in a.reason


def test_check_limits_ordinary_usage_missing():
    a = check_limits(limits(allowed=None), 90, NOW)
    assert not a.ok and a.until == NOW + 300


def test_check_limits_ordinary_usage_string_true_is_not_true():
    a = check_limits(limits(allowed="true"), 90, NOW)
    assert not a.ok


def test_check_limits_rate_limit_reached_type_set():
    a = check_limits(limits(reached="primary"), 90, NOW)
    assert not a.ok and "primary" in a.reason


def test_check_limits_reached_type_key_missing_is_unknown():
    r = limits()
    del r["rateLimits"]["rateLimitReachedType"]
    assert not check_limits(r, 90, NOW).ok


def test_check_limits_threshold_is_inclusive():
    assert not check_limits(limits(primary=90), 90, NOW).ok
    assert check_limits(limits(primary=89), 90, NOW).ok
    assert check_limits(limits(primary=Decimal("89.9")), 90, NOW).ok
    assert not check_limits(limits(primary=Decimal("90.0")), 90, NOW).ok


def test_check_limits_secondary_saturated():
    assert not check_limits(limits(secondary=90), 90, NOW).ok
    assert check_limits(limits(secondary=89), 90, NOW).ok


def test_check_limits_secondary_null_is_ok():
    a = check_limits(limits(secondary=None), 90, NOW)
    assert a.ok and a.usage["secondary_pct"] is None


def test_check_limits_primary_missing_is_unavailable():
    a = check_limits(limits(primary=None), 90, NOW)
    assert not a.ok and a.until == NOW + 300


def test_check_limits_non_numeric_percent_is_unavailable():
    assert not check_limits(limits(primary="12"), 90, NOW).ok
    assert not check_limits(limits(primary=True), 90, NOW).ok
    assert not check_limits(limits(secondary="12"), 90, NOW).ok


@pytest.mark.parametrize("balance", [None, "abc", "NaN", "Infinity", True, 1.5, ""])
def test_check_limits_unparseable_balance_is_unavailable(balance):
    a = check_limits(limits(balance=balance), 90, NOW)
    assert not a.ok and a.until == NOW + 300


def test_check_limits_garbage_result_is_unavailable():
    assert not check_limits({}, 90, NOW).ok
    assert not check_limits({"rateLimits": []}, 90, NOW).ok
    assert not check_limits("nope", 90, NOW).ok  # type: ignore[arg-type]


def test_check_limits_until_is_the_saturated_window_reset():
    a = check_limits(limits(primary=95, secondary=10), 90, NOW)
    assert a.until == NOW + 1000
    a = check_limits(limits(primary=10, secondary=95), 90, NOW)
    assert a.until == NOW + 2000


def test_check_limits_until_is_the_latest_when_both_saturate():
    a = check_limits(limits(primary=95, secondary=95, primary_reset=NOW + 5000), 90, NOW)
    assert a.until == NOW + 5000
    a = check_limits(limits(primary=95, secondary=95, secondary_reset=NOW + 1), 90, NOW)
    assert a.until == NOW + 1000


def test_check_limits_stale_reset_does_not_become_a_busy_loop():
    a = check_limits(limits(primary=95, primary_reset=NOW - 50), 90, NOW)
    assert a.until == NOW + 300


def test_check_limits_flag_block_falls_back_to_latest_reset():
    a = check_limits(limits(reached="primary"), 90, NOW)
    assert a.until == NOW + 2000


def test_the_flag_alone_does_not_wait_for_the_latest_window_reset():
    # at 12% and 31% used no window explains the refusal: the cause is unknown, so ask again soon
    a = check_limits(limits(allowed=False), 90, NOW)
    assert not a.ok and a.until == NOW + 300


def test_the_flag_with_a_window_at_the_threshold_keeps_that_windows_reset():
    r = limits(allowed=False, primary=95, primary_reset=NOW + 700, secondary_reset=NOW + 9000)
    assert check_limits(r, 90, NOW).until == NOW + 700


# --- balance ---------------------------------------------------------------------------------


def test_balance_decimal_detects_tiny_drop():
    before = parse_balance(limits(balance="100.0000000000"))
    after = parse_balance(limits(balance="99.9999999999"))
    assert after < before
    # Beyond float precision: these are the same float but different balances.
    a = parse_balance(limits(balance="1000.0000000000000001"))
    b = parse_balance(limits(balance="1000.0000000000000000"))
    assert b < a and float(a) == float(b)


def test_parse_balance_accepts_integer_and_raises_on_garbage():
    assert parse_balance(limits(balance=7)) == Decimal(7)
    with pytest.raises(LimitsError):
        parse_balance({"rateLimits": {"credits": None}})
    with pytest.raises(LimitsError):
        parse_balance({})


# --- availability ----------------------------------------------------------------------------


def test_availability_reads_and_checks():
    a = CodexAdapter(make_cfg(), read=Reads(limits()), wall_clock=lambda: NOW)
    assert a.availability().ok
    b = CodexAdapter(make_cfg(), read=Reads(limits(primary=91)), wall_clock=lambda: NOW)
    assert not b.availability().ok


def test_availability_failed_read_is_unavailable_for_five_minutes():
    a = CodexAdapter(make_cfg(), read=Reads(LimitsError("down")), wall_clock=lambda: NOW)
    got = a.availability()
    assert not got.ok and got.until == NOW + 300 and "down" in got.reason


# --- rounds ----------------------------------------------------------------------------------


def test_round_completes_when_nothing_moves(fake, tmp_path):
    reads = Reads(limits())
    a = adapter(fake.bin, reads)
    res = a.run_round("do it", make_ctx(tmp_path))
    assert res.outcome == COMPLETED
    assert fake.exec_calls()[0]["stdin"] == "do it"


def test_round_stops_on_balance_drop(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_EXEC_SLEEP", "30")
    reads = Reads(limits(balance="100.0000000000"), limits(balance="99.9999999999"))
    pids = []
    a = adapter(fake.bin, reads)
    t0 = time.monotonic()
    res = a.run_round("go", make_ctx(tmp_path, on_spawn=pids.append))
    assert res.outcome == CREDIT_GUARD
    assert "99.9999999999" in res.reason
    assert time.monotonic() - t0 < 10
    assert wait_dead(pids[0])


def test_balance_drop_beats_threshold(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_EXEC_SLEEP", "30")
    reset = time.time() + 3600
    reads = Reads(limits(balance="5"), limits(primary=100, balance="4.9", primary_reset=reset))
    res = adapter(fake.bin, reads).run_round("go", make_ctx(tmp_path))
    assert res.outcome == CREDIT_GUARD
    assert res.until == reset  # the cooldown follows the saturated window


def test_balance_increase_is_not_a_drop(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_EXEC_SLEEP", "0.5")
    reads = Reads(limits(balance="5"), limits(balance="6"))
    assert adapter(fake.bin, reads).run_round("go", make_ctx(tmp_path)).outcome == COMPLETED


def test_round_stops_at_threshold(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_EXEC_SLEEP", "30")
    reset = time.time() + 3600
    reads = Reads(limits(primary=50, primary_reset=reset), limits(primary=90, primary_reset=reset))
    pids = []
    res = adapter(fake.bin, reads).run_round("go", make_ctx(tmp_path, on_spawn=pids.append))
    assert res.outcome == LIMIT
    assert res.until == reset
    assert wait_dead(pids[0])


def test_two_failed_reads_stop_round_as_credit_guard(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_EXEC_SLEEP", "30")
    # A healthy read follows: only the second failure in a row may stop the round.
    reads = Reads(limits(), LimitsError("down"), LimitsError("down"), limits())
    pids = []
    res = adapter(fake.bin, reads).run_round("go", make_ctx(tmp_path, on_spawn=pids.append))
    assert res.outcome == CREDIT_GUARD
    assert wait_dead(pids[0])


def test_two_unparseable_reads_stop_round_as_credit_guard(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_EXEC_SLEEP", "30")
    reads = Reads(limits(), limits(balance=None), limits(balance=None), limits())
    res = adapter(fake.bin, reads).run_round("go", make_ctx(tmp_path))
    assert res.outcome == CREDIT_GUARD


def test_one_failed_read_keeps_going(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_EXEC_SLEEP", "1")
    # baseline, one failure, recovery; the process then ends by itself.
    reads = Reads(limits(), LimitsError("blip"), limits())
    res = adapter(fake.bin, reads).run_round("go", make_ctx(tmp_path))
    assert res.outcome == COMPLETED
    assert reads.calls >= 3


def test_failures_must_be_consecutive(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_EXEC_SLEEP", "1")
    blip = LimitsError("blip")
    reads = Reads(limits(), blip, limits(), blip, limits())
    assert adapter(fake.bin, reads).run_round("go", make_ctx(tmp_path)).outcome == COMPLETED


def test_final_read_catches_debit_after_a_short_round(fake, tmp_path):
    # The process ends before the first poll; the closing read still sees the debit.
    reads = Reads(limits(balance="10"), limits(balance="9"))
    a = CodexAdapter(make_cfg(fake.bin), read=reads, poll_clock=lambda: 0.0)
    assert a.run_round("go", make_ctx(tmp_path)).outcome == CREDIT_GUARD


def test_round_not_started_when_precheck_fails(fake, tmp_path):
    for bad in (limits(primary=95), limits(allowed=False), LimitsError("down")):
        res = adapter(fake.bin, Reads(bad)).run_round("go", make_ctx(tmp_path))
        assert res.outcome == LIMIT
    assert fake.exec_calls() == []


def test_precheck_returns_the_reset_time(fake, tmp_path):
    reads = Reads(limits(primary=95, primary_reset=NOW + 777))
    res = CodexAdapter(make_cfg(fake.bin), read=reads, wall_clock=lambda: NOW).run_round(
        "go", make_ctx(tmp_path)
    )
    assert res.outcome == LIMIT and res.until == NOW + 777


def test_precheck_with_unparseable_balance_does_not_start(fake, tmp_path):
    res = adapter(fake.bin, Reads(limits(balance="x"))).run_round("go", make_ctx(tmp_path))
    assert res.outcome == LIMIT
    assert fake.exec_calls() == []


def test_polls_only_every_poll_s(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_EXEC_SLEEP", "0.6")
    reads = Reads(limits())
    a = CodexAdapter(make_cfg(fake.bin), read=reads, poll_clock=lambda: 0.0)
    a.tick_s = 0.05
    a.run_round("go", make_ctx(tmp_path))
    assert reads.calls == 2  # baseline and the closing read; the clock never reached poll_s


# --- argv ------------------------------------------------------------------------------------


def hook_args():
    """What every Codex round gets for the subagent guard, as `-c` and a flag."""
    command = json.dumps(f"{sys.executable} -I {Path(subagent_guard.__file__)}")
    config = (
        'hooks.PreToolUse=[{matcher=".*spawn_agent",'
        f'hooks=[{{type="command",command={command}}}]}}]'
    )
    # features.hooks=true first: the guard cannot be switched off by a config file
    return ["-c", "features.hooks=true", "-c", config, "--dangerously-bypass-hook-trust"]


def test_argv_has_model_effort_the_guard_hook_and_bypass(fake, tmp_path):
    a = adapter(fake.bin, Reads(limits()), model="m-x", effort="high")
    a.run_round("go", make_ctx(tmp_path))
    argv = fake.exec_calls()[0]["argv"]
    assert argv == [
        "exec",
        "--json",
        "--skip-git-repo-check",
        "-m",
        "m-x",
        "-c",
        "model_reasoning_effort=high",
        *hook_args(),
        "--dangerously-bypass-approvals-and-sandbox",
        "-C",
        str(tmp_path),
        "-",
    ]


def test_the_resumed_argv_carries_the_hook_too(fake, tmp_path):
    adapter(fake.bin, Reads(limits())).run_round("go", make_ctx(tmp_path, session_id="thread-abc"))
    argv = fake.exec_calls()[0]["argv"]
    assert argv == [
        "exec",
        "resume",
        "--json",
        "--skip-git-repo-check",
        "-m",
        "gpt-6.1-sol",
        "-c",
        "model_reasoning_effort=medium",
        *hook_args(),
        "--dangerously-bypass-approvals-and-sandbox",
        "thread-abc",
        "-",
    ]


@pytest.mark.parametrize("tier", ["strong", "build"])
@pytest.mark.parametrize("session", [None, "thread-abc"])
def test_the_allowed_models_reach_every_round_as_the_pair(fake, tmp_path, tier, session):
    adapter(fake.bin, Reads(limits())).run_round(
        "go", make_ctx(tmp_path, tier=tier, session_id=session)
    )
    call = fake.exec_calls()[0]
    assert call["baton_env"]["BATON_ALLOWED_MODELS"] == "gpt-6.1-sol,gpt-6.1-terra"
    assert any(a.startswith("hooks.PreToolUse=") for a in call["argv"])


def test_the_hook_is_given_per_run_with_no_file_written(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    adapter(fake.bin, Reads(limits())).run_round("go", make_ctx(tmp_path))
    assert not (tmp_path / "home" / ".codex").exists()  # nothing in ~/.codex is touched


@pytest.mark.parametrize(
    "tier,model,effort",
    [("strong", "gpt-6.1-sol", "medium"), ("build", "gpt-6.1-terra", "high")],
)
def test_the_round_tier_picks_the_model_and_effort(fake, tmp_path, tier, model, effort):
    a = CodexAdapter(make_cfg(fake.bin), read=Reads(limits()), poll_clock=lambda: 0.0)
    a.run_round("go", make_ctx(tmp_path, tier=tier))
    argv = fake.exec_calls()[0]["argv"]
    assert argv[argv.index("-m") + 1] == model
    assert f"model_reasoning_effort={effort}" in argv


def test_a_resumed_build_round_uses_the_build_model(fake, tmp_path):
    a = CodexAdapter(make_cfg(fake.bin), read=Reads(limits()), poll_clock=lambda: 0.0)
    a.run_round("go", make_ctx(tmp_path, tier="build", session_id="thread-abc"))
    argv = fake.exec_calls()[0]["argv"]
    assert argv[:2] == ["exec", "resume"] and argv[argv.index("-m") + 1] == "gpt-6.1-terra"


def test_no_tier_means_strong(fake, tmp_path):
    a = CodexAdapter(make_cfg(fake.bin), read=Reads(limits()), poll_clock=lambda: 0.0)
    a.run_round("go", make_ctx(tmp_path))
    argv = fake.exec_calls()[0]["argv"]
    assert argv[argv.index("-m") + 1] == "gpt-6.1-sol"


def test_full_access_false_uses_workspace_write(fake, tmp_path):
    a = adapter(fake.bin, Reads(limits()), full_access=False)
    a.run_round("go", make_ctx(tmp_path))
    argv = fake.exec_calls()[0]["argv"]
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"


def test_resume_argv_uses_stored_id(fake, tmp_path):
    a = adapter(fake.bin, Reads(limits()))
    a.run_round("go", make_ctx(tmp_path, session_id="thread-abc"))
    argv = fake.exec_calls()[0]["argv"]
    assert argv[:2] == ["exec", "resume"]
    assert argv[-2:] == ["thread-abc", "-"]
    assert "--dangerously-bypass-approvals-and-sandbox" in argv
    assert "-m" in argv and "-C" not in argv  # `exec resume` has no -C; cwd is the process's


def test_resume_without_full_access_sets_sandbox_by_config(fake, tmp_path):
    a = adapter(fake.bin, Reads(limits()), full_access=False)
    a.run_round("go", make_ctx(tmp_path, session_id="thread-abc"))
    argv = fake.exec_calls()[0]["argv"]
    assert "--sandbox" not in argv and "-s" not in argv
    assert 'sandbox_mode="workspace-write"' in argv


def test_round_runs_in_the_workdir(fake, tmp_path):
    # `exec resume` has no -C, so the working directory must come from the process itself.
    work = tmp_path / "work"
    work.mkdir()
    for session in (None, "thread-abc"):
        adapter(fake.bin, Reads(limits())).run_round(
            "go", make_ctx(tmp_path, workdir=work, session_id=session)
        )
    assert {Path(c["cwd"]).resolve() for c in fake.exec_calls()} == {work.resolve()}


# --- stream ----------------------------------------------------------------------------------


def test_render_extracts_agent_text():
    a = CodexAdapter(make_cfg())
    msg = {"type": "item.completed", "item": {"type": "agent_message", "text": "hello there"}}
    assert a.render(json.dumps(msg)) == "hello there"
    cmd = {"type": "item.started", "item": {"type": "command_execution", "command": "ls -la"}}
    assert a.render(json.dumps(cmd)) == "$ ls -la"
    assert a.render(json.dumps({"type": "turn.started"})) is None
    assert a.render(json.dumps({"type": "turn.failed", "error": {"message": "nope"}})) == (
        "error: nope"
    )
    assert a.render("plain stderr text") == "plain stderr text"
    assert a.render("   ") is None


def test_session_id_reported_from_stream(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_THREAD_ID", "abc-123")
    got = []
    a = adapter(fake.bin, Reads(limits()))
    a.run_round("go", make_ctx(tmp_path, on_session=got.append))
    assert got == ["abc-123"]


def test_failed_resume_sets_resume_failed(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_EXEC_FAIL", "Error: no rollout found for thread id gone")
    monkeypatch.setenv("FAKE_CODEX_EXEC_EXIT", "1")
    a = adapter(fake.bin, Reads(limits()))
    res = a.run_round("go", make_ctx(tmp_path, session_id="gone"))
    assert res.outcome == TRANSIENT and res.resume_failed


def test_failed_fresh_round_is_plain_transient(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_EXEC_FAIL", "Error: something odd")
    monkeypatch.setenv("FAKE_CODEX_EXEC_EXIT", "1")
    res = adapter(fake.bin, Reads(limits())).run_round("go", make_ctx(tmp_path))
    assert res.outcome == TRANSIENT and not res.resume_failed


def test_resume_hitting_a_usage_limit_is_a_limit_not_a_resume_failure(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_EXEC_FAIL", "You've hit your usage limit.")
    monkeypatch.setenv("FAKE_CODEX_EXEC_EXIT", "1")
    res = adapter(fake.bin, Reads(limits())).run_round("go", make_ctx(tmp_path, session_id="x"))
    assert res.outcome == LIMIT and not res.resume_failed


def test_login_error_is_auth(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_EXEC_FAIL", "Not logged in. Run codex login.")
    monkeypatch.setenv("FAKE_CODEX_EXEC_EXIT", "1")
    res = adapter(fake.bin, Reads(limits())).run_round("go", make_ctx(tmp_path))
    assert res.outcome == AUTH


def test_agent_text_mentioning_limits_does_not_classify(fake, tmp_path, monkeypatch):
    # Only error events and non-JSON lines are scanned, never what the agent says.
    monkeypatch.setenv("FAKE_CODEX_EXEC_EXIT", "1")
    res = adapter(fake.bin, Reads(limits())).run_round("go", make_ctx(tmp_path))
    assert res.outcome == TRANSIENT  # fake printed an agent_message and exited 1


def test_missing_binary_is_transient(tmp_path):
    a = adapter(str(tmp_path / "nope"), Reads(limits()))
    res = a.run_round("go", make_ctx(tmp_path))
    assert res.outcome == TRANSIENT


# --- the order of the guard's checks ---------------------------------------------------------


def without(result, *path):
    """A copy of a limits result with one nested key removed (makes a field unreadable)."""
    out = json.loads(json.dumps(result))
    node = out
    for key in path[:-1]:
        node = node[key]
    del node[path[-1]]
    return out


@pytest.mark.parametrize(
    "blind_read",
    [
        without(limits(balance="9"), "rateLimits", "rateLimitReachedType"),
        without(limits(balance="9"), "ordinaryUsageAllowed"),
        limits(balance="9", primary=None),
        limits(balance="9", secondary="oops"),
    ],
    ids=["no-reached-type", "no-allowed-flag", "no-primary", "bad-secondary"],
)
def test_balance_drop_is_seen_even_when_another_field_is_blind(
    fake, tmp_path, monkeypatch, blind_read
):
    monkeypatch.setenv("FAKE_CODEX_EXEC_SLEEP", "30")
    # Only one blind read ever happens before the round must end: it is not counted as the
    # "second consecutive failure" that would have stopped it anyway.
    reads = Reads(limits(balance="10"), blind_read, limits(balance="9"))
    pids = []
    res = adapter(fake.bin, reads).run_round("go", make_ctx(tmp_path, on_spawn=pids.append))
    assert res.outcome == CREDIT_GUARD and "dropped" in res.reason
    assert reads.calls == 2
    assert wait_dead(pids[0])


def test_clear_refusal_is_honoured_when_another_field_is_blind(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_EXEC_SLEEP", "30")
    reset = time.time() + 3600
    saturated_and_blind = without(
        limits(balance="10", primary=100, primary_reset=reset), "rateLimits", "rateLimitReachedType"
    )
    # The third read is the closing one; it must be readable or the round turns credit_guard.
    reads = Reads(limits(balance="10"), saturated_and_blind, limits(balance="10"))
    res = adapter(fake.bin, reads).run_round("go", make_ctx(tmp_path))
    assert res.outcome == LIMIT and res.until == reset
    assert reads.calls == 3


def test_flag_refusal_without_a_balance_is_honoured(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_EXEC_SLEEP", "30")
    refused = limits(allowed=False)
    refused["rateLimits"]["credits"] = None
    reads = Reads(limits(), refused, limits())
    res = adapter(fake.bin, reads).run_round("go", make_ctx(tmp_path))
    assert res.outcome == LIMIT
    assert reads.calls == 3


def test_check_limits_reports_refusal_even_with_blind_fields():
    r = without(limits(primary=95, primary_reset=NOW + 500), "rateLimits", "rateLimitReachedType")
    a = check_limits(r, 90, NOW)
    assert not a.ok and a.until == NOW + 500 and "primary window at 95%" in a.reason
    assert "rateLimitReachedType" in a.reason  # the blind field is still reported


# --- the closing read ------------------------------------------------------------------------


def closing_adapter(fake, *reads) -> tuple[CodexAdapter, Reads]:
    r = Reads(*reads)
    # poll clock frozen: no tick ever polls, so the closing read is the only read after the baseline
    return CodexAdapter(make_cfg(fake.bin), read=r, poll_clock=lambda: 0.0), r


def test_closing_read_is_retried_and_still_sees_the_debit(fake, tmp_path):
    a, reads = closing_adapter(fake, limits(balance="10"), LimitsError("blip"), limits(balance="9"))
    res = a.run_round("go", make_ctx(tmp_path))
    assert res.outcome == CREDIT_GUARD and "dropped" in res.reason
    assert reads.calls == 3


def test_closing_read_failing_twice_is_credit_guard(fake, tmp_path):
    a, reads = closing_adapter(fake, limits(), LimitsError("down"))
    res = a.run_round("go", make_ctx(tmp_path))
    assert res.outcome == CREDIT_GUARD and "cannot confirm" in res.reason
    assert reads.calls == 3


def test_closing_read_unparseable_twice_is_credit_guard(fake, tmp_path):
    a, _ = closing_adapter(fake, limits(), limits(balance=None))
    assert a.run_round("go", make_ctx(tmp_path)).outcome == CREDIT_GUARD


def test_closing_read_failing_once_then_healthy_keeps_the_outcome(fake, tmp_path):
    a, reads = closing_adapter(fake, limits(), LimitsError("blip"), limits())
    assert a.run_round("go", make_ctx(tmp_path)).outcome == COMPLETED
    assert reads.calls == 3


def test_closing_read_also_guards_a_stopped_round(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_EXEC_SLEEP", "30")
    reads = Reads(limits(), LimitsError("x"))
    a = CodexAdapter(make_cfg(fake.bin), read=reads, poll_clock=lambda: 0.0)
    a.tick_s = 0.05
    # The stop request ends the round as COMPLETED; the closing reads then fail twice.
    res = a.run_round("go", make_ctx(tmp_path, should_kill=lambda: True))
    assert res.outcome == CREDIT_GUARD


# --- runner failures and wiring --------------------------------------------------------------


def test_oserror_after_spawn_is_reported_as_such_and_still_gets_a_closing_read(
    tmp_path, monkeypatch
):
    from baton.adapters import codex

    def broken(argv, cwd, stdin_text, ctx, on_line, tick, tick_s, **extra):
        ctx.on_spawn(4242)
        raise OSError("log disk full")

    monkeypatch.setattr(codex, "run_streaming", broken)
    reads = Reads(limits(balance="10"), limits(balance="9"))
    spawned = []
    a = CodexAdapter(make_cfg(), read=reads, poll_clock=lambda: 0.0)
    res = a.run_round("go", make_ctx(tmp_path, on_spawn=spawned.append))
    assert spawned == [4242]
    assert res.outcome == CREDIT_GUARD  # the closing read saw the debit

    ok = CodexAdapter(make_cfg(), read=Reads(limits()), poll_clock=lambda: 0.0)
    res = ok.run_round("go", make_ctx(tmp_path))
    assert res.outcome == TRANSIENT
    assert "after the agent started" in res.reason and "log disk full" in res.reason


def test_oserror_before_spawn_says_cannot_start(tmp_path, monkeypatch):
    from baton.adapters import codex

    def broken(argv, cwd, stdin_text, ctx, on_line, tick, tick_s, **extra):
        raise OSError("no such file")

    monkeypatch.setattr(codex, "run_streaming", broken)
    reads = Reads(limits())
    res = CodexAdapter(make_cfg(), read=reads).run_round("go", make_ctx(tmp_path))
    assert res.outcome == TRANSIENT and "cannot start" in res.reason
    assert reads.calls == 1  # nothing ran, so no closing read


def test_default_read_wiring_uses_cfg_bin_for_baseline_and_closing_read(fake, tmp_path):
    # No injected `read`: the adapter must reach the (fake) app-server through cfg.bin.
    fake.scenario(limits(balance="10"), limits(balance="9"))
    a = CodexAdapter(make_cfg(fake.bin), poll_clock=lambda: 0.0)
    res = a.run_round("go", make_ctx(tmp_path))
    assert res.outcome == CREDIT_GUARD and "10" in res.reason and "9" in res.reason
    initializes = [x for x in fake.rpc.read_text().splitlines() if '"initialize"' in x]
    assert len(initializes) == 2  # baseline and closing read, one fresh server each


def test_default_read_wiring_availability(fake):
    fake.scenario(limits(primary=95))
    assert not CodexAdapter(make_cfg(fake.bin)).availability().ok
    fake.scenario(limits())
    (Path(os.environ["FAKE_CODEX_SCENARIO"] + ".cursor")).unlink()
    assert CodexAdapter(make_cfg(fake.bin)).availability().ok


def test_only_the_last_error_lines_are_kept(fake, tmp_path, monkeypatch):
    from baton.adapters import codex

    monkeypatch.setattr(codex, "MAX_PROBLEMS", 3)
    noise = "\n".join(f"noise {i}" for i in range(50))
    monkeypatch.setenv("FAKE_CODEX_EXEC_FAIL", "usage limit hit\n" + noise)
    monkeypatch.setenv("FAKE_CODEX_EXEC_EXIT", "1")
    res = adapter(fake.bin, Reads(limits())).run_round("go", make_ctx(tmp_path))
    assert res.outcome == TRANSIENT  # the limit line was pushed out of the bounded window


# --- fix round 2 -----------------------------------------------------------------------------


def test_refusal_on_partial_data_with_no_reset_retries_in_five_minutes():
    r = limits(allowed=False, primary=None, secondary=None)
    a = check_limits(r, 90, NOW)
    assert not a.ok and a.until == NOW + 300


def test_refusal_on_clean_data_with_no_reset_keeps_until_unknown():
    r = limits(primary=95, secondary=None)
    del r["rateLimits"]["primary"]["resetsAt"]
    a = check_limits(r, 90, NOW)
    assert not a.ok and a.until is None


def test_closing_read_with_readable_balance_ignores_a_stubborn_blind_field(fake, tmp_path):
    blind = without(limits(balance="10"), "rateLimits", "rateLimitReachedType")
    a, reads = closing_adapter(fake, limits(balance="10"), blind)
    res = a.run_round("go", make_ctx(tmp_path))
    assert res.outcome == COMPLETED
    assert reads.calls == 2  # the balance parsed and held: no retry, no false credit_guard


def test_closing_read_with_blind_field_still_sees_a_drop(fake, tmp_path):
    blind_drop = without(limits(balance="9"), "rateLimits", "rateLimitReachedType")
    a, _ = closing_adapter(fake, limits(balance="10"), blind_drop)
    assert a.run_round("go", make_ctx(tmp_path)).outcome == CREDIT_GUARD


@pytest.mark.parametrize(
    ("line", "work"),
    [
        ('{"type":"item.completed","item":{"type":"agent_message","text":"done"}}', True),
        ('{"type":"item.started","item":{"type":"command_execution","command":"ls"}}', True),
        ('{"type":"error","message":"429 Too Many Requests"}', False),
        ('{"type":"turn.failed","error":{"message":"rate limit"}}', False),
        ("429 Too Many Requests", False),
    ],
)
def test_only_the_agent_acting_counts_as_work(line, work):
    adapter = CodexAdapter(make_cfg(), read=lambda: {})
    assert adapter.render(line)  # the person sees all of them
    assert adapter.is_work(line) is work


# --- a read that cannot be judged must not lose the balance ----------------------------------


def _guard(reads):
    from baton.adapters.codex import evaluate
    from baton.adapters.guard import Guard

    clock = iter(range(0, 10_000, 100))
    it = iter(reads)
    return Guard(make_cfg(), lambda: next(it), evaluate, lambda: next(clock), lambda: NOW)


def _flag_without_reached_type(balance):
    return without(limits(balance=balance, allowed=False), "rateLimits", "rateLimitReachedType")


def test_the_flag_false_without_a_reached_type_does_not_raise():
    a = check_limits(_flag_without_reached_type("9"), 90, NOW)
    assert not a.ok and "rateLimitReachedType is missing" in a.reason and a.until == NOW + 300


def test_a_balance_drop_in_a_read_with_no_reached_type_and_the_flag_false_is_credit_guard():
    guard = _guard([limits(balance="10"), _flag_without_reached_type("9")])
    assert guard.start() is None
    res = guard.tick()
    assert res is not None and res.outcome == CREDIT_GUARD and "dropped" in res.reason


def test_the_closing_read_sees_a_drop_in_a_read_with_no_reached_type_and_the_flag_false():
    guard = _guard([limits(balance="10"), _flag_without_reached_type("9")])
    assert guard.start() is None
    res = guard.close()
    assert res is not None and res.outcome == CREDIT_GUARD and "dropped" in res.reason


def test_a_reset_too_large_for_a_float_is_not_a_reset():
    r = limits(primary=95, primary_reset=10**400, secondary_reset=10**400)
    a = check_limits(r, 90, NOW)
    assert not a.ok and a.until is None  # refused, reset unknown: the scheduler backs off


# --- a read that cannot be judged counts as unreadable ---------------------------------------


def test_a_used_percent_beyond_a_float_is_a_blind_field_not_an_exception():
    huge = Decimal("1e5000")
    a = check_limits(limits(primary=huge), 90, NOW)
    assert not a.ok and "primary window is missing or unreadable" in a.reason


def test_a_balance_drop_beside_a_used_percent_beyond_a_float_is_credit_guard_in_tick():
    guard = _guard([limits(balance="100"), limits(primary=Decimal("1e5000"), balance="90")])
    assert guard.start() is None
    res = guard.tick()
    assert res is not None and res.outcome == CREDIT_GUARD and "dropped" in res.reason


def test_a_balance_drop_beside_a_used_percent_beyond_a_float_is_credit_guard_in_close():
    guard = _guard([limits(balance="100"), limits(primary=Decimal("1e5000"), balance="90")])
    assert guard.start() is None
    res = guard.close()
    assert res is not None and res.outcome == CREDIT_GUARD and "dropped" in res.reason


def test_an_evaluate_that_raises_is_a_blind_read_in_tick_and_credit_guard_on_the_second():
    from baton.adapters.guard import Guard

    def boom(result, threshold, now):
        raise ValueError("boom")

    clock = iter(range(0, 10_000, 100))
    guard = Guard(make_cfg(), lambda: {}, boom, lambda: next(clock), lambda: NOW)
    guard.baseline = Decimal(100)
    guard.last_poll = -1000.0
    assert guard.tick() is None and guard.failures == 1
    res = guard.tick()
    assert res is not None and res.outcome == CREDIT_GUARD and "could not be judged" in res.reason


def test_an_evaluate_that_raises_is_credit_guard_from_close():
    from baton.adapters.guard import Guard

    def boom(result, threshold, now):
        raise ValueError("boom")

    reads = []
    guard = Guard(make_cfg(), lambda: reads.append(1) or {}, boom, lambda: 0.0, lambda: NOW)
    guard.baseline = Decimal(100)
    res = guard.close()
    assert res is not None and res.outcome == CREDIT_GUARD and "could not be judged" in res.reason
    assert len(reads) == 2  # retried once


def test_an_evaluate_that_raises_at_start_is_a_limit_and_nothing_spawns(tmp_path):
    from baton.adapters.guard import Guard, run_guarded

    def boom(result, threshold, now):
        raise ValueError("boom")

    guard = Guard(make_cfg(), lambda: {}, boom, lambda: 0.0, lambda: NOW)
    ran = []
    res = run_guarded(guard, make_ctx(tmp_path), "codex", lambda c: ran.append(1) or None)
    assert res.outcome == LIMIT and res.until == NOW + 300 and "could not be judged" in res.reason
    assert not ran


# --- the subagent guard: the hook blocks, the stream only logs ------------------------------------


def collab(model=None, ident="item_1", tool="spawn_agent", kind="item.started"):
    item = {"id": ident, "type": "collab_tool_call", "tool": tool}
    if model is not None:
        item["model"] = model
    return {"type": kind, "item": item}


def subagent_round(fake, tmp_path, monkeypatch, events, **ctx_kw):
    monkeypatch.setenv("FAKE_CODEX_EXTRA", json.dumps(events))
    seen = []
    a = adapter(fake.bin, Reads(limits()))
    ctx = make_ctx(tmp_path, on_subagent=lambda t, m: seen.append((t, m)), **ctx_kw)
    return a.run_round("go", ctx), seen


@pytest.mark.parametrize("model", ["gpt-6.1-sol", "gpt-6.1-terra", "gpt-5-mini", None])
@pytest.mark.parametrize("tier", ["strong", "build"])
def test_the_stream_logs_every_subagent_and_never_ends_the_round(
    fake, tmp_path, monkeypatch, model, tier
):
    result, seen = subagent_round(fake, tmp_path, monkeypatch, [collab(model)], tier=tier)
    assert result.outcome == COMPLETED  # the hook already denied what it had to
    assert seen == [("spawn_agent", model)]


def test_a_resumed_round_logs_too(fake, tmp_path, monkeypatch):
    result, seen = subagent_round(
        fake, tmp_path, monkeypatch, [collab("gpt-5-mini")], session_id="thread-abc"
    )
    assert result.outcome == COMPLETED and seen == [("spawn_agent", "gpt-5-mini")]


def test_the_model_is_also_found_in_the_arguments(fake, tmp_path, monkeypatch):
    event = collab(None)
    event["item"]["arguments"] = {"model": "gpt-5-mini", "message": "x"}
    _, seen = subagent_round(fake, tmp_path, monkeypatch, [event])
    assert seen == [("spawn_agent", "gpt-5-mini")]


def test_one_call_started_and_completed_with_its_model_seen_is_reported_once(
    fake, tmp_path, monkeypatch
):
    events = [collab("gpt-6.1-terra"), collab("gpt-6.1-terra", kind="item.completed")]
    _, seen = subagent_round(fake, tmp_path, monkeypatch, events)
    assert len(seen) == 1


def test_a_call_whose_model_only_shows_when_it_completes_is_reported_again(
    fake, tmp_path, monkeypatch
):
    events = [collab(None), collab("gpt-5-mini", kind="item.completed")]
    _, seen = subagent_round(fake, tmp_path, monkeypatch, events)
    assert seen == [("spawn_agent", None), ("spawn_agent", "gpt-5-mini")]


def test_calls_without_an_id_are_never_deduplicated(fake, tmp_path, monkeypatch):
    def anonymous(model):
        event = collab(model)
        del event["item"]["id"]
        return event

    _, seen = subagent_round(
        fake, tmp_path, monkeypatch, [anonymous("gpt-6.1-terra"), anonymous("gpt-6.1-terra")]
    )
    assert len(seen) == 2


# --- the tool watch --------------------------------------------------------------------------


def item_line(kind, ident, item_type="command_execution"):
    return json.dumps({"type": kind, "item": {"id": ident, "type": item_type, "command": "ls"}})


def test_a_command_is_busy_from_started_to_completed():
    watch = CodexAdapter(make_cfg()).tool_watch()
    assert watch.busy is False
    watch.feed(item_line("item.started", "item_1"))
    assert watch.busy is True
    watch.feed(item_line("item.completed", "item_1"))
    assert watch.busy is False


def test_a_parallel_pair_of_commands_stays_busy_until_both_end():
    watch = CodexAdapter(make_cfg()).tool_watch()
    watch.feed(item_line("item.started", "item_1"))
    watch.feed(item_line("item.started", "item_2"))
    watch.feed(item_line("item.completed", "item_1"))
    assert watch.busy is True
    watch.feed(item_line("item.completed", "item_2"))
    assert watch.busy is False


def test_an_agent_message_and_noise_are_not_a_tool():
    watch = CodexAdapter(make_cfg()).tool_watch()
    for line in (
        item_line("item.started", "item_3", "agent_message"),
        json.dumps({"type": "turn.started"}),
        '{"type":"item.started","item":{"type":"command_execution"}}',  # no id to close it by
        "plain stderr text",
    ):
        watch.feed(line)
    assert watch.busy is False

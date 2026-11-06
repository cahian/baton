import json
import os
import shlex
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from conftest import codex_limits

from baton import subagent_guard
from baton.adapters import claude_api
from baton.adapters.base import (
    AUTH,
    COMPLETED,
    CREDIT_GUARD,
    LIMIT,
    TIMEOUT,
    TRANSIENT,
    RoundContext,
)
from baton.adapters.claude_api import (
    ClaudeApiAdapter,
    CostReportError,
    KeyFileError,
    evaluate_line,
    fetch_cost_report,
    guard_settings,
    make_cost_report,
    read_key,
)
from baton.config import AgentConfig
from baton.ledger import Ledger
from baton.proc import SCRUBBED_ENV

FAKE = Path(__file__).parent / "fakes" / "fake_claude.py"
KEY = "fake-ant-api03-TESTKEY0123456789abcdefghij"
ADMIN_KEY = "fake-ant-admin01-ADMINKEY0123456789abcdef"
NOW = datetime(2026, 11, 4, 12, tzinfo=UTC).timestamp()
NEXT_PERIOD = datetime(2026, 12, 1, tzinfo=UTC).timestamp()
CEILING = Decimal("180")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A `claude` executable that runs the fake, a key file, a ledger and a recorded call log."""
    wrapper = tmp_path / "claude"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    wrapper.chmod(0o755)
    key_file = tmp_path / "anthropic.key"
    key_file.write_text(KEY + "\n")
    key_file.chmod(0o600)
    calls = tmp_path / "claude-calls.jsonl"
    for var in SCRUBBED_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FAKE_CLAUDE_CALLS", str(calls))
    monkeypatch.setenv("FAKE_CLAUDE_PIDFILE", str(tmp_path / "pid"))

    class Env:
        bin = str(wrapper)
        key = key_file
        pidfile = tmp_path / "pid"
        ledger = Ledger(tmp_path / "state" / "claude-spend.json", CEILING, 1, lambda: NOW)
        root = tmp_path

        @staticmethod
        def calls():
            if not calls.exists():
                return []
            return [json.loads(x) for x in calls.read_text().splitlines()]

    return Env


def make_cfg(env, **kw) -> AgentConfig:
    d = dict(
        name="claude_api",
        enabled=True,
        model="claude-opus-5-5",
        effort="medium",
        build_model="claude-sonnet-5-5",
        build_effort="high",
        threshold_pct=95,
        poll_s=15,
        bin=env.bin,
        full_access=True,
        key_file=str(env.key),
        round_budget_usd=Decimal("10"),  # the adapter's mechanics; the default has its own test
    )
    d.update(kw)
    return AgentConfig(**d)


def make_adapter(env, cost_report=None, **kw) -> ClaudeApiAdapter:
    return ClaudeApiAdapter(make_cfg(env, **kw), env.ledger, cost_report, wall_clock=lambda: NOW)


def make_ctx(tmp_path: Path, **kw) -> RoundContext:
    d = dict(
        workdir=tmp_path,
        log_path=tmp_path / "logs" / "1-claude_api.log",
        round_max_s=30,
        idle_s=30,
        on_output=lambda line: None,
        should_kill=lambda: False,
        on_spawn=lambda pid: None,
    )
    d.update(kw)
    return RoundContext(**d)


def run(env, adapter=None, prompt="do the thing", **ctx_kw):
    adapter = adapter or make_adapter(env)
    return adapter.run_round(prompt, make_ctx(env.root, **ctx_kw))


def init_line(source="ANTHROPIC_API_KEY", **extra) -> str:
    return json.dumps(
        {"type": "system", "subtype": "init", "session_id": "s1", "apiKeySource": source, **extra}
    )


# --- the key file ----------------------------------------------------------------------------


def test_read_key_strips_the_newline(env):
    assert read_key(env.key) == KEY


def test_read_key_accepts_a_path_with_tilde(env, monkeypatch):
    monkeypatch.setenv("HOME", str(env.root))
    assert read_key(Path("~/anthropic.key")) == KEY


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660, 0o666, 0o700, 0o400])
def test_read_key_refuses_a_file_that_is_not_mode_600(env, mode):
    env.key.chmod(mode)
    with pytest.raises(KeyFileError, match="600") as err:
        read_key(env.key)
    assert KEY not in str(err.value)


def test_read_key_missing_file(env):
    with pytest.raises(KeyFileError, match="not found"):
        read_key(env.root / "nope.key")


def test_read_key_refuses_a_directory_and_a_symlink(env):
    directory = env.root / "dir"
    directory.mkdir(mode=0o600)
    with pytest.raises(KeyFileError, match="regular file"):
        read_key(directory)
    link = env.root / "link.key"
    link.symlink_to(env.key)
    with pytest.raises(KeyFileError):
        read_key(link)


@pytest.mark.parametrize("content", ["", "\n", "two words\n", "tab\there\n", "line\nbreak\n"])
def test_read_key_refuses_a_file_that_is_not_one_token(env, content):
    env.key.write_text(content)
    env.key.chmod(0o600)
    with pytest.raises(KeyFileError) as err:
        read_key(env.key)
    for word in content.split():
        assert word not in str(err.value)


def test_read_key_error_never_includes_content(env):
    env.key.write_bytes(b"\xff\xfe" + KEY.encode())
    env.key.chmod(0o600)
    with pytest.raises(KeyFileError) as err:
        read_key(env.key)
    assert KEY not in str(err.value) and KEY not in repr(err.value)


# --- reading a line --------------------------------------------------------------------------


def test_evaluate_line_accepts_the_key_source():
    assert evaluate_line(init_line("ANTHROPIC_API_KEY")) is None


@pytest.mark.parametrize(
    "source", ["none", "apiKeyHelper", "/login managed key", "ANTHROPIC_AUTH_TOKEN", "", None, 5]
)
def test_evaluate_line_stops_any_other_key_source(source):
    result = evaluate_line(init_line(source))
    assert result is not None and result.outcome == AUTH
    assert "apiKeySource" in result.reason


def test_evaluate_line_stops_an_init_without_a_key_source():
    line = json.dumps({"type": "system", "subtype": "init", "session_id": "s1"})
    result = evaluate_line(line)
    assert result is not None and result.outcome == AUTH


@pytest.mark.parametrize(
    "line",
    [
        "",
        "not json",
        "[1]",
        json.dumps({"type": "assistant", "message": {"content": []}}),
        json.dumps({"type": "system", "subtype": "other", "apiKeySource": "none"}),
        json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 0.1}),
    ],
)
def test_evaluate_line_ignores_everything_else(line):
    assert evaluate_line(line) is None


# --- the command -----------------------------------------------------------------------------


def expected_argv(model, effort, cap="10.00", resume=None):
    argv = [
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--setting-sources=",
        "--strict-mcp-config",
        "--model",
        model,
        "--effort",
        effort,
        "--dangerously-skip-permissions",
        "--tools=Bash,Edit,Read,Write,Agent",
        "--allowedTools=Bash,Edit,Read,Write,Agent",
        f"--settings={guard_settings()}",
        "--max-budget-usd",
        cap,
    ]
    return argv + ["--resume", resume] if resume else argv


@pytest.mark.parametrize(
    "tier,model,effort",
    [("strong", "claude-opus-5-5", "medium"), ("build", "claude-sonnet-5-5", "high")],
)
@pytest.mark.parametrize("session", [None, "abc-123"])
def test_the_exact_argv_fresh_and_resumed_on_both_tiers(env, tier, model, effort, session):
    assert run(env, tier=tier, session_id=session).outcome == COMPLETED
    (call,) = env.calls()
    assert call["argv"] == expected_argv(model, effort, resume=session)
    assert "--bare" not in call["argv"]  # it would stop --tools and --settings from working
    assert call["stdin"] == "do the thing"  # the prompt goes on stdin, not on the command line
    assert call["cwd"] == str(env.root.resolve()) or call["cwd"] == str(env.root)


def test_the_settings_hook_runs_the_guard_with_this_python_in_isolated_mode_and_blocks_on_failure(
    env,
):
    run(env)
    (arg,) = [a for a in env.calls()[0]["argv"] if a.startswith("--settings=")]
    settings = json.loads(arg.removeprefix("--settings="))
    (entry,) = settings["hooks"]["PreToolUse"]
    assert entry["matcher"] == "Agent|Task"
    (hook,) = entry["hooks"]
    assert hook["type"] == "command" and hook["onFailure"] == "block"
    assert shlex.split(hook["command"]) == [
        sys.executable,
        "-I",
        str(Path(subagent_guard.__file__)),
    ]
    assert set(settings) == {"hooks"}


def test_the_round_env_names_the_allowed_models_and_the_default_for_subagents(env):
    run(env)
    child = env.calls()[0]["env"]
    assert child["BATON_ALLOWED_MODELS"] == "sonnet,claude-sonnet-5-5,opus,claude-opus-5-5"
    assert child["CLAUDE_CODE_SUBAGENT_MODEL"] == "claude-sonnet-5-5"
    assert child["CLAUDE_CODE_SUBAGENT_MODEL_FORCE"] == "1"  # every subagent on the working model
    assert child["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert child["ANTHROPIC_API_KEY"] == KEY  # the key is there, and it is the only credential
    assert "ANTHROPIC_AUTH_TOKEN" not in child


@pytest.mark.parametrize(
    "name",
    [
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_FOUNDRY_API_KEY",
        "ANTHROPIC_CUSTOM_HEADERS",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
        "CLAUDE_CODE_OAUTH_SCOPES",
        "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
        "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
        "CLAUDE_CODE_WEBSOCKET_AUTH_FILE_DESCRIPTOR",
        "AWS_BEARER_TOKEN_BEDROCK",
    ],
)
def test_no_other_credential_or_provider_reaches_the_round(env, monkeypatch, name):
    monkeypatch.setenv(name, "stray-value-from-the-parent")
    monkeypatch.setenv("CLAUDE_CODE_SOMETHING_ELSE", "kept")
    run(env)
    child = env.calls()[0]["env"]
    assert name not in child
    assert child["ANTHROPIC_API_KEY"] == KEY  # added back after the scrub, from the key file
    assert child["CLAUDE_CODE_SOMETHING_ELSE"] == "kept"  # only credential names go


def config_dir():
    from baton.store import state_base

    return state_base() / ".claude-config"


@pytest.mark.parametrize("session", [None, "abc-123"])
def test_the_round_uses_a_config_dir_of_baton_s_own_so_the_users_file_cannot_swap_the_key(
    env, monkeypatch, session
):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/somewhere/else")  # a parent's own is replaced
    run(env, session_id=session)
    child = env.calls()[0]["env"]
    assert child["CLAUDE_CONFIG_DIR"] == str(config_dir())
    assert config_dir().is_dir() and (config_dir().stat().st_mode & 0o777) == 0o700


def test_every_round_uses_the_same_config_dir_so_sessions_resume(env):
    run(env)
    run(env, session_id="sess-1")
    first, second = (c["env"]["CLAUDE_CONFIG_DIR"] for c in env.calls())
    assert first == second == str(config_dir())


def test_an_existing_config_dir_with_loose_mode_is_tightened(env):
    config_dir().mkdir(parents=True)
    config_dir().chmod(0o755)
    run(env)
    assert (config_dir().stat().st_mode & 0o777) == 0o700


def test_a_config_dir_that_cannot_be_made_is_a_transient_round_that_spent_nothing(env):
    config_dir().parent.mkdir(parents=True, exist_ok=True)
    config_dir().write_text("a file where the directory should be")
    result = run(env)
    assert result.outcome == TRANSIENT and "config" in result.reason
    assert env.calls() == [] and env.ledger.spent() == 0


def test_a_parent_api_key_never_replaces_the_key_file(env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-ant-api03-FROM-THE-PARENT-ENV-0000")
    run(env)
    assert env.calls()[0]["env"]["ANTHROPIC_API_KEY"] == KEY


def test_the_allowed_models_follow_the_configured_pair(env):
    run(env, adapter=make_adapter(env, model="claude-opus-9", build_model="claude-haiku-9"))
    child = env.calls()[0]["env"]
    assert child["BATON_ALLOWED_MODELS"] == "haiku,claude-haiku-9,opus,claude-opus-9"
    assert child["CLAUDE_CODE_SUBAGENT_MODEL"] == "claude-haiku-9"


def test_the_hook_denies_a_third_model_end_to_end(env):
    """The hook the argv names, run the way Claude Code runs it, with this round's env."""
    run(env)
    call = env.calls()[0]
    (arg,) = [a for a in call["argv"] if a.startswith("--settings=")]
    hooks = json.loads(arg.removeprefix("--settings="))["hooks"]["PreToolUse"][0]["hooks"]
    event = json.dumps({"tool_name": "Agent", "tool_input": {"model": "haiku"}})
    done = subprocess.run(
        shlex.split(hooks[0]["command"]),
        input=event,
        capture_output=True,
        text=True,
        env={"BATON_ALLOWED_MODELS": call["env"]["BATON_ALLOWED_MODELS"]},
    )
    assert json.loads(done.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_round_cap_is_what_the_ledger_granted(env):
    rid, _ = env.ledger.reserve(Decimal("170"))
    env.ledger.settle(rid, Decimal("172.5"))
    run(env)
    assert env.calls()[0]["argv"][-1] == "7.50"


@pytest.mark.parametrize("session_id", [None, "abc-123"])
def test_allowed_tools_and_env_scrub_travel_together(env, session_id):
    """The scrub forces the default permission mode, so only the explicit tool list lets the agent
    write; dropping either half leaves rounds that succeed and change nothing."""
    assert run(env, session_id=session_id).outcome == COMPLETED
    (call,) = env.calls()
    assert "--allowedTools=Bash,Edit,Read,Write,Agent" in call["argv"]
    assert call["env"]["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"] == "1"


def test_session_id_from_init(env):
    seen = []
    run(env, on_session=seen.append)
    assert seen == ["sess-1"]


def test_failed_resume_sets_resume_failed(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_RESUME_FAIL", "1")
    result = run(env, session_id="gone")
    assert result.outcome == TRANSIENT and result.resume_failed is True


def test_failed_fresh_round_is_not_a_failed_resume(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_NO_RESULT", "1")
    result = run(env)
    assert result.outcome == TRANSIENT and result.resume_failed is False


def test_an_error_before_any_init_on_resume_sets_resume_failed(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_NO_INIT", "1")
    monkeypatch.setenv("FAKE_CLAUDE_NO_ASSISTANT", "1")
    monkeypatch.setenv("FAKE_CLAUDE_IS_ERROR", "1")
    monkeypatch.setenv("FAKE_CLAUDE_ERROR_TEXT", "No conversation found")
    monkeypatch.setenv("FAKE_CLAUDE_COST", "0")
    result = run(env, session_id="abc")
    assert result.outcome == TRANSIENT and result.resume_failed is True


def test_a_failure_after_a_confirmed_init_is_not_a_failed_resume(env, monkeypatch):
    """The session loaded and the key was confirmed: whatever went wrong later is not the resume,
    and a fresh retry would repeat work that already happened."""
    monkeypatch.setenv("FAKE_CLAUDE_IS_ERROR", "1")
    monkeypatch.setenv("FAKE_CLAUDE_ERROR_TEXT", "something odd happened")
    result = run(env, session_id="abc")
    assert result.outcome == TRANSIENT and result.resume_failed is False
    monkeypatch.setenv("FAKE_CLAUDE_NO_RESULT", "1")  # and a crash after init is the same
    monkeypatch.setenv("FAKE_CLAUDE_EXIT", "1")
    result = run(env, session_id="abc")
    assert result.outcome == TRANSIENT and result.resume_failed is False


# --- the key travels alone -------------------------------------------------------------------


def test_key_reaches_only_its_process(env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://evil.example")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token-from-the-shell")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key-from-the-shell-0000")
    run(env)
    child_env = env.calls()[0]["env"]
    assert child_env["ANTHROPIC_API_KEY"] == KEY  # the file's key, not the shell's
    assert "ANTHROPIC_BASE_URL" not in child_env and "ANTHROPIC_AUTH_TOKEN" not in child_env
    assert child_env["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"] == "1"
    assert KEY not in os.environ.values()  # not in baton's own environment either


def test_key_is_never_on_the_command_line(env):
    run(env, session_id="abc")
    assert KEY not in json.dumps(env.calls()[0]["argv"])


def test_the_key_does_not_reach_the_next_agents_process(fakes, monkeypatch):
    from baton.cli import main

    fakes.claude_key(KEY)
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "0")
    # Codex is full for two seconds, so Claude, the fallback, takes this round.
    fakes.codex_scenario(codex_limits(95, reset=time.time() + 2), codex_limits(12))
    goal = fakes.goal(claude="", order=("codex", "claude_api"), agy="enabled = false")
    assert main(["run", str(goal)]) == 0  # no verify: finishes after the Claude round
    assert fakes.claude_calls()[0]["env"]["ANTHROPIC_API_KEY"] == KEY
    # a Codex round afterwards: it sees no Anthropic variable at all
    goal = fakes.goal(name="second", order=("codex",))
    assert main(["run", str(goal)]) == 0
    assert fakes.codex_calls()[0]["anthropic_env"] == []


def test_key_never_in_log_events_or_state(fakes, monkeypatch):
    from baton.cli import main

    fakes.claude_key(KEY)
    monkeypatch.setenv("FAKE_CLAUDE_ECHO_KEY", "1")  # the agent prints it into its own output
    goal = fakes.goal(claude="", order=("claude_api",))
    assert main(["run", str(goal)]) == 0
    haystack = []
    for base in (fakes.state, fakes.work, fakes.ledger_file.parent):
        for path in base.rglob("*"):
            if path.is_file():
                haystack.append((path, path.read_bytes()))
    assert any(p.name == "events.jsonl" for p, _ in haystack)
    assert any(p.name == "claude-spend.json" for p, _ in haystack)
    log = next(data for p, data in haystack if p.suffix == ".log")
    assert b"key=[redacted]" in log
    for path, data in haystack:
        assert KEY.encode() not in data, path


def test_key_never_in_an_error_event(fakes):
    from baton.cli import main

    fakes.claude_key(KEY)
    goal = fakes.goal(claude=f'bin = "{fakes.root}/missing-claude"\n', order=("claude_api",))
    # the binary does not exist: the round fails to start, then waits; stop it right away
    from baton.store import Store, state_dir

    Store(state_dir("demo")).request_stop()
    assert main(["run", str(goal)]) == 0
    for path in fakes.state.rglob("*"):
        if path.is_file():
            assert KEY.encode() not in path.read_bytes(), path


# --- availability ----------------------------------------------------------------------------


def test_reserve_is_the_configured_one_and_off_by_default(env):
    assert make_adapter(env).reserve is False
    assert make_adapter(env, reserve=True).reserve is True
    assert make_adapter(env).metered is True  # paid by use, whether or not it waits


def test_availability_is_ok_and_describes_the_spend(env):
    avail = make_adapter(env).availability()
    assert avail.ok
    assert avail.usage == {"spent_usd": "0", "ceiling_usd": "180", "left_usd": "180"}
    json.dumps(avail.usage)  # it goes into state.json: plain strings, no Decimal, no float


def test_disabled_is_unavailable(env):
    avail = make_adapter(env, enabled=False).availability()
    assert not avail.ok and "disabled" in avail.reason and avail.permanent
    assert env.calls() == []


def test_a_round_budget_below_the_minimum_is_permanently_unavailable(env):
    avail = make_adapter(env, round_budget_usd=Decimal("0.01")).availability()
    assert not avail.ok and "minimum" in avail.reason and avail.permanent


def test_wrong_mode_key_is_unavailable(env):
    env.key.chmod(0o644)
    avail = make_adapter(env).availability()
    assert not avail.ok and "600" in avail.reason and avail.permanent
    assert avail.until == NOW + 3600
    assert KEY not in avail.reason


def test_missing_key_is_unavailable_and_nonblocking(env, tmp_path):
    env.key.unlink()
    adapter = make_adapter(env)
    avail = adapter.availability()
    assert not avail.ok and "not found" in avail.reason and avail.until == NOW + 3600
    assert avail.permanent  # no key: it will not run until the person adds one

    from baton.adapters.base import Availability
    from baton.scheduler import Scheduler
    from baton.store import Store

    class Other:
        name = "codex"
        metered = False
        reserve = False

        def availability(self):
            return Availability(True)

    sched = Scheduler(
        ("claude_api", "codex"),
        {"claude_api": adapter, "codex": Other()},
        Store(tmp_path / "goal"),
        lambda: NOW,
    )
    assert sched.pick().name == "codex"  # the missing key never stands in the others' way
    assert env.calls() == []


def test_a_key_file_that_is_a_symlink_is_unavailable(env):
    link = env.root / "link.key"
    link.symlink_to(env.key)
    assert not make_adapter(env, key_file=str(link)).availability().ok


def test_ceiling_reached_is_unavailable_until_next_period(env):
    env.ledger.settle("x", Decimal("179.5"))
    avail = make_adapter(env).availability()
    assert not avail.ok and avail.until == NEXT_PERIOD
    assert "179.5" in avail.reason and "180" in avail.reason


def test_a_live_reservation_counts_against_availability(env):
    env.ledger.reserve(Decimal("179.5"))
    assert not make_adapter(env).availability().ok


def test_round_budget_below_the_minimum_grant_is_unavailable(env):
    avail = make_adapter(env, round_budget_usd=Decimal("4.99")).availability()
    assert not avail.ok and "5 USD" in avail.reason
    assert make_adapter(env, round_budget_usd=Decimal("5")).availability().ok


def test_corrupt_ledger_is_unavailable_and_kept(env):
    env.ledger.path.parent.mkdir(parents=True)
    env.ledger.path.write_text("{garbage")
    adapter = make_adapter(env)
    avail = adapter.availability()
    assert not avail.ok and "claude-spend.json" in avail.reason
    result = run(env, adapter)
    assert result.outcome == LIMIT
    assert env.ledger.path.read_text() == "{garbage"  # never recreated
    assert env.calls() == []


def test_round_does_not_start_past_the_ceiling(env):
    env.ledger.settle("x", Decimal("180"))
    result = run(env)
    assert result.outcome == LIMIT and result.until == NEXT_PERIOD
    assert env.calls() == []


def test_two_adapters_share_one_ceiling(env):
    a, b = make_adapter(env), make_adapter(env)
    env.ledger.settle("x", Decimal("165"))
    first = env.ledger.reserve(Decimal("10"))  # another goal is mid-round
    assert first is not None
    run(env, a)  # only 5 USD are left for this one
    assert env.calls()[0]["argv"][-1] == "5.00"
    assert not b.availability().ok or b.availability().usage["left_usd"] != "180"


def test_cost_report_larger_than_ledger_wins(env):
    adapter = make_adapter(env, cost_report=lambda: Decimal("175"))
    avail = adapter.availability()
    assert avail.ok and avail.usage["spent_usd"] == "175" and avail.usage["left_usd"] == "5"
    run(env, adapter)
    assert env.calls()[0]["argv"][-1] == "5.00"  # the round is shrunk to what the report leaves
    assert not make_adapter(env, cost_report=lambda: Decimal("180")).availability().ok


def test_less_than_five_dollars_left_is_unavailable_until_next_period(env):
    env.ledger.settle("x", Decimal("175.01"))  # 4.99 left
    avail = make_adapter(env).availability()
    assert not avail.ok and avail.until == NEXT_PERIOD
    assert run(env).outcome == LIMIT and env.calls() == []
    assert not make_adapter(env, cost_report=lambda: Decimal("175.01")).availability().ok


def test_exactly_five_dollars_left_runs_a_five_dollar_round(env):
    env.ledger.settle("x", Decimal("175"))
    assert make_adapter(env).availability().ok
    assert run(env).outcome == COMPLETED
    assert env.calls()[0]["argv"][-1] == "5.00"


def test_cost_report_smaller_than_ledger_never_lowers_it(env):
    env.ledger.settle("x", Decimal("100"))
    avail = make_adapter(env, cost_report=lambda: Decimal("3")).availability()
    assert avail.usage["spent_usd"] == "100"


def test_cost_report_failure_is_unavailable(env):
    def broken():
        raise CostReportError("HTTP 500")

    adapter = make_adapter(env, cost_report=broken)
    avail = adapter.availability()
    assert not avail.ok and "HTTP 500" in avail.reason
    assert avail.until is not None and avail.until <= NOW + 3600
    assert run(env, adapter).outcome == LIMIT
    assert env.calls() == []  # no report, no round
    assert env.ledger.spent() == 0  # and nothing stays reserved


def test_cost_report_that_raises_anything_is_unavailable(env):
    def broken():
        raise RuntimeError("boom")

    assert not make_adapter(env, cost_report=broken).availability().ok


# --- what a round costs ----------------------------------------------------------------------


def test_result_settles_actual_cost(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_COST", "0.123456789012345678901")
    assert run(env).outcome == COMPLETED
    assert env.ledger.spent() == Decimal("0.123456789012345678901")  # exact, never a float
    assert json.loads(env.ledger.path.read_text())["reservations"] == {}


def test_the_result_carries_what_the_stream_reported_as_cost(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_COST", "0.125")
    assert run(env).cost_usd == 0.125
    monkeypatch.setenv("FAKE_CLAUDE_COST", "12.5")
    assert run(env).cost_usd == 12.5  # over the cap, as the ledger charges it


def test_a_round_that_kept_its_reservation_carries_the_reservation_as_cost(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_NO_RESULT", "1")
    result = run(env)
    assert result.outcome == TRANSIENT and result.cost_usd == 10.0  # what the ledger counts
    assert env.ledger.spent() == Decimal("10.00")


def test_a_timeout_carries_the_reservation_as_cost(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    result = run(env, round_max_s=1)
    assert result.outcome == TIMEOUT and result.cost_usd == 10.0


def test_a_killed_round_carries_the_reservation_as_cost(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    seen = threading.Event()
    result = run(env, should_kill=seen.is_set, on_spawn=lambda pid: seen.set())
    assert result.cost_usd == 10.0
    assert env.ledger.spent() == Decimal("10.00")


def test_a_process_that_started_and_died_carries_the_reservation_as_cost(env, monkeypatch):
    def boom(argv, workdir, prompt, ctx, *args, **kwargs):
        ctx.on_spawn(1)
        raise OSError("gone")

    monkeypatch.setattr(claude_api, "run_streaming", boom)
    result = run(env)
    assert result.outcome == TRANSIENT and result.cost_usd == 10.0


def test_a_round_that_never_reached_the_api_costs_nothing(env):
    adapter = make_adapter(env, bin=str(env.root / "missing"))
    result = run(env, adapter)
    assert result.cost_usd == 0.0 and env.ledger.spent() == 0


def test_the_reservation_that_shrank_is_the_cost_that_is_carried(env, monkeypatch):
    env.ledger.reserve(Decimal("174"))  # 6 USD of the 180 ceiling are left
    monkeypatch.setenv("FAKE_CLAUDE_NO_RESULT", "1")
    assert run(env).cost_usd == 6.0


def test_a_failed_resume_carries_the_cost_it_reported(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_NO_INIT", "1")
    monkeypatch.setenv("FAKE_CLAUDE_NO_ASSISTANT", "1")
    monkeypatch.setenv("FAKE_CLAUDE_IS_ERROR", "1")
    monkeypatch.setenv("FAKE_CLAUDE_ERROR_TEXT", "No conversation found")
    monkeypatch.setenv("FAKE_CLAUDE_COST", "0.75")
    result = run(env, session_id="gone")
    assert result.resume_failed is True and result.cost_usd == 0.75


def test_cost_over_the_cap_is_charged_in_full(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_COST", "12.5")
    run(env)
    assert env.ledger.spent() == Decimal("12.5")


def test_budget_cap_reached_settles_and_counts_as_completed(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_IS_ERROR", "1")
    monkeypatch.setenv("FAKE_CLAUDE_SUBTYPE", "error_max_budget_usd")
    monkeypatch.setenv("FAKE_CLAUDE_ERROR_TEXT", "Reached maximum budget ($10.00)")
    monkeypatch.setenv("FAKE_CLAUDE_COST", "10.0042")
    result = run(env)
    assert result.outcome == COMPLETED and "budget" in result.reason
    assert env.ledger.spent() == Decimal("10.0042")


def test_no_result_event_keeps_full_reservation(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_NO_RESULT", "1")
    result = run(env)
    assert result.outcome == TRANSIENT
    assert env.ledger.spent() == Decimal("10.00")  # overcount, never undercount


@pytest.mark.parametrize("cost", ["-0.5", "NaN", "Infinity", '"0.5"', "null", "true"])
def test_an_unusable_cost_keeps_full_reservation(env, monkeypatch, cost):
    monkeypatch.setenv("FAKE_CLAUDE_COST", cost)
    run(env)
    assert env.ledger.spent() == Decimal("10.00")


def test_a_crash_inside_the_adapter_keeps_the_reservation(env, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("runner exploded")

    monkeypatch.setattr(claude_api, "run_streaming", boom)
    with pytest.raises(RuntimeError):
        run(env)
    assert env.ledger.spent() == Decimal("10.00")


def test_a_process_that_never_started_releases_its_reservation(env):
    adapter = make_adapter(env, bin=str(env.root / "missing"))
    result = run(env, adapter)
    assert result.outcome == TRANSIENT and "cannot start" in result.reason
    assert env.ledger.spent() == 0  # nothing ran, so nothing was spent
    assert KEY not in result.reason


def test_a_process_that_started_and_died_keeps_its_reservation(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_NO_RESULT", "1")
    monkeypatch.setenv("FAKE_CLAUDE_EXIT", "3")
    run(env)
    assert env.ledger.spent() == Decimal("10.00")


def test_api_key_source_other_than_key_stops_round(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_API_KEY_SOURCE", "none")
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    result = run(env)
    assert result.outcome == AUTH and "apiKeySource" in result.reason
    pid = int(env.pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # the group was killed, it did not run for 30 s
    # the wrong-source init came before any API call: nothing was spent, nothing stays reserved
    assert env.ledger.spent() == 0


def test_an_event_before_init_stops_the_round(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_ASSISTANT_FIRST", "1")
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    result = run(env)
    assert result.outcome == CREDIT_GUARD and "init" in result.reason
    assert env.ledger.spent() == Decimal("10.00")  # the model was already talking: it stays


def test_no_init_at_all_is_not_a_clean_round(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_NO_INIT", "1")
    result = run(env)
    assert result.outcome == CREDIT_GUARD
    assert env.ledger.spent() != 0  # whatever it cost, it is counted


def test_a_stop_request_ends_the_round_and_keeps_the_reservation(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    seen = threading.Event()
    result = run(env, should_kill=seen.is_set, on_spawn=lambda pid: seen.set())
    assert result.outcome == COMPLETED
    assert env.ledger.spent() == Decimal("10.00")


def test_a_refusal_already_on_the_stream_wins_over_a_stop_that_lands_before_the_exit(
    env, monkeypatch
):
    monkeypatch.setenv("FAKE_CLAUDE_IS_ERROR", "1")
    monkeypatch.setenv("FAKE_CLAUDE_ERROR_TEXT", "Credit balance is too low")
    monkeypatch.setenv("FAKE_CLAUDE_COST", "0")
    monkeypatch.setenv("FAKE_CLAUDE_LINGER", "30")
    seen = threading.Event()
    adapter = make_adapter(env)
    result = run(
        env,
        adapter,
        on_output=lambda line: seen.set() if "Credit balance" in line else None,
        should_kill=seen.is_set,  # the error result is on the stream, the process is still alive
    )
    assert result.outcome == LIMIT and result.until == NEXT_PERIOD
    assert env.ledger.exhausted()
    assert not adapter.availability().ok


def test_a_stop_on_a_healthy_stream_is_still_a_stop(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_LINGER", "30")
    seen = threading.Event()
    result = run(
        env,
        on_output=lambda line: seen.set() if "working" in line else None,
        should_kill=seen.is_set,
    )
    assert result.outcome == COMPLETED and result.reason == "stop requested"
    assert not env.ledger.exhausted()


def test_a_timeout_is_a_timeout_and_keeps_the_reservation(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    result = run(env, round_max_s=1)
    assert result.outcome == TIMEOUT
    assert env.ledger.spent() == Decimal("10.00")


# --- refusals --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Credit balance is too low",
        "Your credit balance is too low to access the Anthropic API",
        "billing error: payment required",
        "You have reached your workspace spend limit",
    ],
)
def test_credit_balance_error_is_limit_until_next_period(env, monkeypatch, text):
    monkeypatch.setenv("FAKE_CLAUDE_IS_ERROR", "1")
    monkeypatch.setenv("FAKE_CLAUDE_ERROR_TEXT", text)
    monkeypatch.setenv("FAKE_CLAUDE_COST", "0")
    adapter = make_adapter(env)
    result = run(env, adapter)
    assert result.outcome == LIMIT and result.until == NEXT_PERIOD
    # and it stays that way: the next availability check does not offer the agent again
    avail = adapter.availability()
    assert not avail.ok and avail.until == NEXT_PERIOD
    assert run(env, adapter).outcome == LIMIT and len(env.calls()) == 1


def test_exhaustion_is_shared_with_other_adapters_and_ends_with_the_period(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_IS_ERROR", "1")
    monkeypatch.setenv("FAKE_CLAUDE_ERROR_TEXT", "Credit balance is too low")
    run(env)
    other = Ledger(env.ledger.path, CEILING, 1, lambda: NEXT_PERIOD)
    assert not make_adapter(env).availability().ok
    assert not other.exhausted()


@pytest.mark.parametrize(
    "text", ["API Error: 429 Too Many Requests", "Overloaded", "overloaded_error", "rate limit hit"]
)
def test_429_is_limit_without_reset(env, monkeypatch, text):
    monkeypatch.setenv("FAKE_CLAUDE_IS_ERROR", "1")
    monkeypatch.setenv("FAKE_CLAUDE_ERROR_TEXT", text)
    result = run(env)
    assert result.outcome == LIMIT and result.until is None
    assert not env.ledger.exhausted()  # a rate limit says nothing about the credit


def test_an_unauthorized_key_is_auth(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_IS_ERROR", "1")
    monkeypatch.setenv("FAKE_CLAUDE_ERROR_TEXT", "Invalid API key · Please run /login")
    assert run(env).outcome == AUTH


def test_unknown_error_is_transient(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_IS_ERROR", "1")
    monkeypatch.setenv("FAKE_CLAUDE_ERROR_TEXT", "the pipe is broken")
    result = run(env)
    assert result.outcome == TRANSIENT and "pipe is broken" in result.reason


def test_error_text_on_stderr_without_a_result_is_classified(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_NO_RESULT", "1")
    monkeypatch.setenv("FAKE_CLAUDE_TEXT", "fine")
    monkeypatch.setenv("FAKE_CLAUDE_EXIT", "1")
    assert run(env).outcome == TRANSIENT


def test_words_in_a_successful_round_are_not_a_refusal(env, monkeypatch):
    monkeypatch.setenv(
        "FAKE_CLAUDE_TEXT", "Fixed the billing page: credit balance, 429 and spend limit handled"
    )
    result = run(env)
    assert result.outcome == COMPLETED
    assert not env.ledger.exhausted()


def test_refusal_wins_over_a_failed_resume(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_IS_ERROR", "1")
    monkeypatch.setenv("FAKE_CLAUDE_ERROR_TEXT", "Credit balance is too low")
    result = run(env, session_id="abc")
    assert result.outcome == LIMIT and result.resume_failed is False


# --- rendering -------------------------------------------------------------------------------


def test_render_shows_text_and_tools_and_errors():
    adapter = ClaudeApiAdapter.__new__(ClaudeApiAdapter)
    text = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Looking at it."},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                ]
            },
        }
    )
    assert adapter.render(text) == "Looking at it.\n[Bash]"
    assert adapter.render(init_line()) is None
    assert adapter.render(json.dumps({"type": "result", "is_error": False, "result": "x"})) is None
    err = json.dumps({"type": "result", "is_error": True, "result": "bad thing"})
    assert adapter.render(err) == "error: bad thing"
    assert adapter.render("a plain line") == "a plain line"
    assert adapter.render("") is None


# --- the cost report -------------------------------------------------------------------------


class Stub:
    """A local HTTP server standing in for the Admin API."""

    def __init__(self, pages, status=200):
        self.pages, self.status, self.requests = pages, status, []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                stub.requests.append((self.path, dict(self.headers)))
                index = len(stub.requests) - 1
                body = stub.pages[min(index, len(stub.pages) - 1)]
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(stub.status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def bucket(*amounts, currency="USD"):
    return {
        "starting_at": "2026-11-01T00:00:00Z",
        "ending_at": "2026-11-02T00:00:00Z",
        "results": [{"amount": a, "currency": currency} for a in amounts],
    }


@pytest.fixture
def admin(env):
    env.root.joinpath("admin.key").write_text(ADMIN_KEY + "\n")
    env.root.joinpath("admin.key").chmod(0o600)
    return env.root / "admin.key"


@pytest.fixture
def stub():
    made = []

    def make(*pages, **kw):
        made.append(Stub(list(pages), **kw))
        return made[-1]

    yield make
    for s in made:
        s.close()


def test_cost_report_sums_buckets_and_converts_cents_to_dollars(admin, stub):
    server = stub(
        {"data": [bucket("12345.5", "100"), bucket("5")], "has_more": False, "next_page": None}
    )
    total = fetch_cost_report(admin, NOW - 86400, base_url=server.url)
    assert total == Decimal("124.505")  # (12345.5 + 100 + 5) cents
    path, headers = server.requests[0]
    assert path.startswith("/v1/organizations/cost_report?")
    assert "starting_at=2026-11-03T12%3A00%3A00Z" in path or "starting_at=2026-11-03T" in path
    lowered = {k.lower(): v for k, v in headers.items()}
    assert lowered["x-api-key"] == ADMIN_KEY and "anthropic-version" in lowered


def test_cost_report_follows_pages(admin, stub):
    server = stub(
        {"data": [bucket("100")], "has_more": True, "next_page": "page_2"},
        {"data": [bucket("250")], "has_more": False, "next_page": None},
    )
    assert fetch_cost_report(admin, NOW, base_url=server.url) == Decimal("3.50")
    assert "page=page_2" in server.requests[1][0]


@pytest.mark.parametrize(
    "page",
    [
        {"data": [bucket("1")], "has_more": True, "next_page": None},
        {"data": [bucket("1")]},
        {"data": [bucket("abc")], "has_more": False},
        {"data": [bucket("-5")], "has_more": False},
        {"data": [bucket("NaN")], "has_more": False},
        {"data": [bucket("1", currency="EUR")], "has_more": False},
        {"data": "x", "has_more": False},
        {"data": [{"results": "x"}], "has_more": False},
        [],
        b"not json",
    ],
)
def test_cost_report_that_cannot_be_understood_raises(admin, stub, page):
    server = stub(page)
    with pytest.raises(CostReportError) as err:
        fetch_cost_report(admin, NOW, base_url=server.url)
    assert ADMIN_KEY not in str(err.value)


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_cost_report_http_error_raises_without_the_key(admin, stub, status):
    server = stub({"error": "no"}, status=status)
    with pytest.raises(CostReportError, match=str(status)) as err:
        fetch_cost_report(admin, NOW, base_url=server.url)
    assert ADMIN_KEY not in str(err.value) and ADMIN_KEY not in repr(err.value)


def test_cost_report_connection_failure_raises_without_the_key(admin):
    with pytest.raises(CostReportError) as err:
        fetch_cost_report(admin, NOW, base_url="http://127.0.0.1:9", timeout=2)
    assert ADMIN_KEY not in str(err.value)


def test_cost_report_never_sends_the_key_in_clear_to_a_remote_host(admin):
    with pytest.raises(CostReportError, match="https"):
        fetch_cost_report(admin, NOW, base_url="http://api.example.com")


def test_cost_report_with_a_bad_key_file_raises(env):
    with pytest.raises(CostReportError):
        fetch_cost_report(env.root / "nope", NOW)


def test_make_cost_report_is_none_without_an_admin_key_file(env):
    assert make_cost_report(make_cfg(env), env.ledger) is None
    assert make_cost_report(make_cfg(env, admin_key_file=""), env.ledger) is None


def test_make_cost_report_reads_from_the_start_of_the_period(env, admin, stub, monkeypatch):
    seen = []

    def fake_fetch(path, since, **kw):
        seen.append((path, since))
        return Decimal("42")

    monkeypatch.setattr(claude_api, "fetch_cost_report", fake_fetch)
    report = make_cost_report(make_cfg(env, admin_key_file=str(admin)), env.ledger)
    assert report() == Decimal("42")
    assert seen == [(admin, datetime(2026, 11, 1, tzinfo=UTC).timestamp())]


def test_admin_key_file_must_be_mode_600_too(env, admin):
    admin.chmod(0o644)
    report = make_cost_report(make_cfg(env, admin_key_file=str(admin)), env.ledger)
    adapter = ClaudeApiAdapter(make_cfg(env), env.ledger, report, wall_clock=lambda: NOW)
    avail = adapter.availability()
    assert not avail.ok and ADMIN_KEY not in avail.reason


def test_signal_is_not_swallowed(env, monkeypatch):
    """A SIGTERM mid-round (Terminated is a BaseException) leaves the reservation in place."""

    class Stop(BaseException):
        pass

    def boom(*a, **k):
        raise Stop()

    monkeypatch.setattr(claude_api, "run_streaming", boom)
    with pytest.raises(Stop):
        run(env)
    assert env.ledger.spent() == Decimal("10.00")


# --- a round that never reached the API ------------------------------------------------------


def test_a_broken_binary_cannot_burn_the_ceiling(env, monkeypatch):
    """18 failing rounds used to reserve 180 USD for nothing and lock Claude out for the month."""
    monkeypatch.setenv("FAKE_CLAUDE_EARLY_EXIT", "127")
    monkeypatch.setenv("FAKE_CLAUDE_STDERR", "claude: broken installation")
    adapter = make_adapter(env)
    for _ in range(25):
        result = run(env, adapter)
        assert result.outcome == TRANSIENT and "exited before the init event" in result.reason
    assert env.ledger.spent() == 0
    assert adapter.availability().ok
    assert len(env.calls()) == 25


def test_a_pre_init_exit_with_a_huge_output_keeps_the_reservation(env, monkeypatch):
    """A 64 KB chunk is real output: not proof that nothing happened."""
    monkeypatch.setenv("FAKE_CLAUDE_EARLY_BYTES", "70000")
    monkeypatch.setenv("FAKE_CLAUDE_EARLY_EXIT", "1")
    run(env)
    assert env.ledger.spent() == Decimal("10.00")


@pytest.mark.parametrize(
    "event",
    [
        {"type": "system", "subtype": "api_retry", "attempt": 1, "error_status": 529},
        {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}},
        {"type": "system", "subtype": "hook_started"},
        {"type": "something_new"},
    ],
)
def test_any_event_before_the_exit_keeps_the_reservation(env, monkeypatch, event):
    """An api_retry or a rate_limit_event means a request went out: nothing may be released."""
    monkeypatch.setenv("FAKE_CLAUDE_PRE_EVENT", json.dumps(event))
    monkeypatch.setenv("FAKE_CLAUDE_EARLY_EXIT", "1")
    result = run(env)
    assert result.outcome == TRANSIENT
    assert env.ledger.spent() == Decimal("10.00")


def test_a_valid_init_then_an_exit_keeps_the_reservation(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_NO_ASSISTANT", "1")
    monkeypatch.setenv("FAKE_CLAUDE_NO_RESULT", "1")
    monkeypatch.setenv("FAKE_CLAUDE_EXIT", "1")
    run(env)
    assert env.ledger.spent() == Decimal("10.00")


def test_noise_lines_before_a_wrong_source_init_are_still_the_only_event(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_STDERR", "warning: something harmless")
    monkeypatch.setenv("FAKE_CLAUDE_API_KEY_SOURCE", "none")
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    assert run(env).outcome == AUTH
    assert env.ledger.spent() == 0


def test_an_event_before_a_wrong_source_init_keeps_the_reservation(env, monkeypatch):
    monkeypatch.setenv(
        "FAKE_CLAUDE_PRE_EVENT", json.dumps({"type": "system", "subtype": "api_retry"})
    )
    monkeypatch.setenv("FAKE_CLAUDE_API_KEY_SOURCE", "none")
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    assert run(env).outcome == AUTH
    assert env.ledger.spent() == Decimal("10.00")  # the init was not the only event


def test_a_pre_init_timeout_keeps_the_reservation(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_NO_INIT", "1")
    monkeypatch.setenv("FAKE_CLAUDE_NO_ASSISTANT", "1")
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    monkeypatch.setenv("FAKE_CLAUDE_NO_RESULT", "1")
    assert run(env, round_max_s=1).outcome == TIMEOUT
    assert env.ledger.spent() == Decimal("10.00")  # it may have been mid-call


def test_a_stop_before_any_event_keeps_the_reservation(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_NO_INIT", "1")
    monkeypatch.setenv("FAKE_CLAUDE_NO_ASSISTANT", "1")
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    monkeypatch.setenv("FAKE_CLAUDE_NO_RESULT", "1")
    run(env, should_kill=lambda: True)
    assert env.ledger.spent() == Decimal("10.00")


def test_a_cost_after_a_pre_init_result_is_charged(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_NO_INIT", "1")
    monkeypatch.setenv("FAKE_CLAUDE_NO_ASSISTANT", "1")
    monkeypatch.setenv("FAKE_CLAUDE_COST", "2.5")
    run(env)
    assert env.ledger.spent() == Decimal("2.5")


# --- error text: structured fields and the process's own stderr only -------------------------


def test_billing_words_inside_a_big_event_before_a_crash_are_not_a_refusal(env, monkeypatch):
    """A 200 KB file the agent wrote, full of billing words, then a crash: that is agent output."""
    monkeypatch.setenv("FAKE_CLAUDE_BIG_WRITE", "200000")
    adapter = make_adapter(env)
    result = run(env, adapter)
    assert result.outcome == TRANSIENT and result.until is None
    assert not env.ledger.exhausted()
    assert adapter.availability().ok
    assert env.ledger.spent() == Decimal("10.00")  # it did work: the reservation stays


def test_billing_words_in_a_long_stderr_line_are_not_a_refusal(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_STDERR", "credit balance billing " + "padding " * 60)
    monkeypatch.setenv("FAKE_CLAUDE_NO_RESULT", "1")
    monkeypatch.setenv("FAKE_CLAUDE_EXIT", "1")
    assert run(env).outcome == TRANSIENT
    assert not env.ledger.exhausted()


def test_a_short_stderr_line_ends_the_round_but_never_blocks_every_goal(env, monkeypatch):
    """Plain stderr is not a structured API error: it limits this agent until the next period for
    now, but only the API's own error fields may mark the credit gone for everyone."""
    monkeypatch.setenv("FAKE_CLAUDE_STDERR", "Error: Credit balance is too low")
    monkeypatch.setenv("FAKE_CLAUDE_NO_RESULT", "1")
    monkeypatch.setenv("FAKE_CLAUDE_EXIT", "1")
    result = run(env)
    assert result.outcome == LIMIT and result.until == NEXT_PERIOD
    assert not env.ledger.exhausted()


def test_a_structured_billing_error_marks_the_credit_gone(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_IS_ERROR", "1")
    monkeypatch.setenv("FAKE_CLAUDE_ERROR_TEXT", "Your credit balance is too low")
    monkeypatch.setenv("FAKE_CLAUDE_COST", "0")
    assert run(env).outcome == LIMIT
    assert env.ledger.exhausted()


def test_the_cut_of_a_multibyte_line_is_not_read_as_an_error(env, monkeypatch):
    """A 64 KB event of Chinese text is about 21 000 characters, so a size in characters called it
    small, and its short last piece, ending in credit-balance words, marked every goal exhausted."""
    monkeypatch.setenv("FAKE_CLAUDE_BIG_CJK", "1")
    adapter = make_adapter(env)
    result = run(env, adapter)
    assert result.outcome == TRANSIENT and result.until is None
    assert not env.ledger.exhausted()
    assert adapter.availability().ok
    assert env.ledger.spent() == Decimal("10.00")  # a big event went by: the round did work


def test_a_short_stderr_line_before_init_is_still_a_refusal(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_STDERR", "Error: Credit balance is too low")
    monkeypatch.setenv("FAKE_CLAUDE_EARLY_EXIT", "1")
    assert run(env).outcome == LIMIT


# --- one ledger, global settings, no silent reset --------------------------------------------


def test_ledger_for_uses_the_global_path_and_the_budget_settings(env, monkeypatch):
    from baton.ledger import default_path

    cfg = make_cfg(env, monthly_budget_usd=Decimal("100"), reserve_pct=20, budget_reset_day=15)
    ledger = claude_api.ledger_for(cfg)
    assert ledger.path == default_path() == Path(os.environ["BATON_LEDGER"])
    assert ledger.ceiling == Decimal("80") and ledger.reset_day == 15
    assert claude_api.ledger_for(make_cfg(env)).ceiling == Decimal("180")


def test_a_goal_with_other_budget_settings_is_unavailable_with_a_way_out(env):
    other = Ledger(env.ledger.path, Decimal("360"), 15, lambda: NOW)
    other.reserve(Decimal("10"))
    before = env.ledger.path.read_text()
    adapter = make_adapter(env)
    avail = adapter.availability()
    assert not avail.ok and "defaults.toml" in avail.reason and "360" in avail.reason
    result = run(env, adapter)
    assert result.outcome == LIMIT and env.calls() == []
    assert env.ledger.path.read_text() == before  # nothing wiped


def test_a_deleted_ledger_makes_claude_unavailable(env):
    adapter = make_adapter(env)
    run(env, adapter)
    env.ledger.path.unlink()
    avail = adapter.availability()
    assert not avail.ok and "deleted" in avail.reason
    calls = len(env.calls())
    assert run(env, adapter).outcome == LIMIT and len(env.calls()) == calls
    assert not env.ledger.path.exists()


# --- a reservation lives as long as its round -------------------------------------------------


def test_the_reservation_outlives_the_rounds_time_limit_by_an_hour(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_NO_RESULT", "1")  # so the reservation is still there
    run(env, round_max_s=7200)
    (stored,) = json.loads(env.ledger.path.read_text())["reservations"].values()
    assert stored["expires_at"] == NOW + 7200 + 3600


def test_a_long_round_keeps_counting_after_the_month_turns(env, monkeypatch):
    """round_max_s of three days: the 24-hour rule used to drop this reservation at midnight."""
    monkeypatch.setenv("FAKE_CLAUDE_NO_RESULT", "1")
    evening = datetime(2026, 11, 30, 23, 0, tzinfo=UTC).timestamp()
    ledger = Ledger(env.ledger.path, CEILING, 1, lambda: evening)
    ClaudeApiAdapter(make_cfg(env), ledger, wall_clock=lambda: evening).run_round(
        "go", make_ctx(env.root, round_max_s=3 * 86400)
    )
    later = Ledger(env.ledger.path, CEILING, 1, lambda: NEXT_PERIOD + 86400)
    assert later.spent() == Decimal("10.00")  # the new month still counts the live round
    assert later.reserve(Decimal("175"))[1] == Decimal("170")  # 10 + 170, never 190


def assistant(text, **fields):
    message = {"content": [{"type": "text", "text": text}]}
    return json.dumps({"type": "assistant", "message": message, **fields})


@pytest.mark.parametrize(
    ("line", "work"),
    [
        (assistant("reading the code"), True),
        (assistant("API Error: 429", error="rate_limit"), False),
        (json.dumps({"type": "result", "is_error": True, "result": "API Error: 429"}), False),
        ("429 Too Many Requests", False),
    ],
)
def test_only_the_agent_acting_counts_as_work(env, line, work):
    adapter = make_adapter(env)
    assert adapter.render(line)  # the person sees all of them
    assert adapter.is_work(line) is work


# --- the subagent guard: the hook is the first layer, the stream the second -------------------


def agent_use(model=None, name="Agent", ident="toolu_1"):
    block = {"type": "tool_use", "id": ident, "name": name, "input": {"prompt": "do it"}}
    if model is not None:
        block["input"]["model"] = model
    if ident is None:
        del block["id"]
    return {"type": "assistant", "message": {"role": "assistant", "content": [block]}}


def claude_round(env, monkeypatch, events, **ctx_kw):
    monkeypatch.setenv("FAKE_CLAUDE_EXTRA", json.dumps(events))
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    seen = []
    started = time.monotonic()
    result = run(env, on_subagent=lambda t, m: seen.append((t, m)), **ctx_kw)
    return result, seen, time.monotonic() - started


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-sonnet-5-5", "sonnet", "opus"])
def test_a_subagent_on_either_model_or_its_alias_is_allowed(env, monkeypatch, model):
    monkeypatch.setenv("FAKE_CLAUDE_EXTRA", json.dumps([agent_use(model)]))
    seen = []
    result = run(env, on_subagent=lambda t, m: seen.append((t, m)))
    assert result.outcome == COMPLETED and seen == [("Agent", model)]


@pytest.mark.parametrize("model", ["claude-haiku-4-5", "haiku", "gpt-5", "inherit", None])
@pytest.mark.parametrize("name", ["Agent", "Task"])
def test_the_stream_only_logs_a_subagent_the_hook_denies_it_does_not_end_the_round(
    env, monkeypatch, model, name
):
    monkeypatch.setenv("FAKE_CLAUDE_EXTRA", json.dumps([agent_use(model, name=name)]))
    seen = []
    result = run(env, on_subagent=lambda t, m: seen.append((t, m)))
    assert result.outcome == COMPLETED  # a round it would have ended once cost a 5 USD reservation
    assert seen == [(name, model)]


def test_a_subagent_with_no_model_is_logged_and_allowed(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_EXTRA", json.dumps([agent_use(None)]))
    seen = []
    result = run(env, on_subagent=lambda t, m: seen.append((t, m)))
    assert result.outcome == COMPLETED and seen == [("Agent", None)]


def test_a_call_with_no_id_is_never_skipped_as_a_repeat(env, monkeypatch):
    event = agent_use("sonnet", ident=None)
    monkeypatch.setenv("FAKE_CLAUDE_EXTRA", json.dumps([event, event]))
    seen = []
    run(env, on_subagent=lambda t, m: seen.append((t, m)))
    assert len(seen) == 2


def test_another_tool_is_not_a_subagent(env, monkeypatch):
    block = {"type": "tool_use", "id": "t", "name": "Write", "input": {"model": "haiku"}}
    event = {"type": "assistant", "message": {"content": [block]}}
    monkeypatch.setenv("FAKE_CLAUDE_EXTRA", json.dumps([event]))
    seen = []
    result = run(env, on_subagent=lambda t, m: seen.append((t, m)))
    assert result.outcome == COMPLETED and seen == []


@pytest.mark.parametrize("source", ["none", "claude.ai", "oauth", "/login managed key"])
def test_a_subscription_login_is_refused_at_once_before_any_spend(env, monkeypatch, source):
    monkeypatch.setenv("FAKE_CLAUDE_API_KEY_SOURCE", source)
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    started = time.monotonic()
    result = run(env)
    assert result.outcome == AUTH and "apiKeySource" in result.reason
    assert time.monotonic() - started < 15  # the group was stopped, not waited for
    assert env.ledger.spent() == 0  # no model call was made


def test_a_round_cut_off_by_its_budget_is_bounded_but_still_completed_for_the_scheduler(
    env, monkeypatch
):
    monkeypatch.setenv("FAKE_CLAUDE_IS_ERROR", "1")
    monkeypatch.setenv("FAKE_CLAUDE_SUBTYPE", "error_max_budget_usd")
    result = run(env)
    assert result.outcome == COMPLETED and result.bounded is True


def test_a_normal_round_is_not_bounded(env):
    assert run(env).bounded is False


# --- the tool watch --------------------------------------------------------------------------


def assistant_blocks(*blocks):
    return json.dumps({"type": "assistant", "message": {"role": "assistant", "content": blocks}})


def user_blocks(*blocks):
    return json.dumps({"type": "user", "message": {"role": "user", "content": blocks}})


def use(ident, name="Bash"):
    return {"type": "tool_use", "id": ident, "name": name, "input": {"command": "ls"}}


def returned(ident):
    return {"type": "tool_result", "tool_use_id": ident, "content": "ok"}


def new_watch(env):
    return make_adapter(env).tool_watch()


def test_a_tool_use_is_busy_until_its_result_comes_back(env):
    watch = new_watch(env)
    assert watch.busy is False
    watch.feed(assistant_blocks({"type": "text", "text": "running it"}, use("toolu_1")))
    assert watch.busy is True
    watch.feed(user_blocks(returned("toolu_1")))
    assert watch.busy is False


def test_a_parallel_pair_of_tool_uses_stays_busy_until_both_return(env):
    watch = new_watch(env)
    watch.feed(assistant_blocks(use("toolu_1"), use("toolu_2", "Read")))
    watch.feed(user_blocks(returned("toolu_2")))
    assert watch.busy is True
    watch.feed(user_blocks(returned("toolu_1")))
    assert watch.busy is False


def test_text_only_events_and_noise_are_not_busy(env):
    watch = new_watch(env)
    for line in (
        assistant_blocks({"type": "text", "text": "hello"}),
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps({"type": "assistant", "message": "not a dict"}),
        json.dumps({"type": "user", "message": {"content": "just a string"}}),
        assistant_blocks("junk", {"type": "tool_use", "name": "Bash"}),  # no id to close it by
        "plain stderr text",
    ):
        watch.feed(line)
    assert watch.busy is False

import os
from decimal import Decimal

import pytest
from conftest import agy_credits, agy_usage, codex_limits, iso

from baton import doctor
from baton.config import load_goal
from baton.doctor import Check, Proc, interactive_codex, run_checks
from baton.memory import Memory

SECRET = "fake-very-secret-value-12345"


@pytest.fixture(autouse=True)
def no_real_processes(monkeypatch):
    """The machine running the tests may have a real interactive codex open."""
    monkeypatch.setattr(doctor, "list_processes", lambda: [])


def check(checks: list[Check], needle: str) -> Check:
    found = [c for c in checks if needle in c.name]
    assert len(found) == 1, [c.name for c in checks]
    return found[0]


def blocking_failures(checks):
    return [c for c in checks if c.blocking and not c.ok]


def run(fakes, **kw):
    return run_checks(load_goal(fakes.goal(**kw)), os.environ)


def test_flags_stray_api_key(fakes, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "")
    checks = run(fakes)
    flagged = check(checks, "OPENAI_API_KEY")
    assert not flagged.ok and not flagged.blocking  # baton removes it from agent processes
    assert not check(checks, "CODEX_ACCESS_TOKEN").ok  # present, even if empty
    for c in checks:
        assert SECRET not in c.name and SECRET not in c.detail


def test_clean_environment_is_reported_ok(fakes):
    checks = run(fakes)
    assert check(checks, "environment").ok
    assert not [c for c in checks if "_API_KEY" in c.name]


def test_flags_missing_binary(fakes):
    checks = run(fakes, codex='bin = "/nonexistent/codex"')
    missing = check(checks, "codex binary")
    assert not missing.ok and missing.blocking
    assert "/nonexistent/codex" in missing.detail
    assert check(checks, "antigravity binary").ok
    # Without a binary there is nothing to read limits from.
    assert not check(checks, "codex limits").ok


def test_reports_codex_limits(fakes):
    fakes.codex_scenario(codex_limits(primary=12, secondary=31, balance="1000.5000000000"))
    c = check(run(fakes), "codex limits")
    assert c.ok and c.blocking
    assert "12%" in c.detail and "31%" in c.detail
    assert "1000.5000000000" in c.detail
    assert iso(4_000_000_000) in c.detail  # when the windows reset


def test_codex_over_threshold_is_a_warning_not_a_blocker(fakes):
    fakes.codex_scenario(codex_limits(primary=95))
    c = check(run(fakes), "codex limits")
    assert not c.ok and not c.blocking
    assert "95%" in c.detail and "primary" in c.detail


def test_unreadable_codex_limits_block(fakes):
    fakes.codex_scenario("garbage")
    c = check(run(fakes), "codex limits")
    assert not c.ok and c.blocking


def test_reports_antigravity_usage_and_credits(fakes):
    fakes.agy_files(usage=agy_usage(five=80, weekly=60), credits=agy_credits("7.5"))
    c = check(run(fakes), "antigravity limits")
    assert c.ok and c.blocking
    assert "Five Hour" in c.detail and "20%" in c.detail  # 80% remaining is 20% used
    assert "Weekly" in c.detail and "40%" in c.detail
    assert "7.5" in c.detail
    assert iso(4_000_000_000) in c.detail
    assert "Claude" not in c.detail  # not part of the rotation


def test_antigravity_over_threshold_is_a_warning(fakes):
    fakes.agy_files(usage=agy_usage(five=5))
    c = check(run(fakes), "antigravity limits")
    assert not c.ok and not c.blocking and "95%" in c.detail


def test_unreadable_antigravity_credits_block(fakes):
    fakes.agy_files(credits="no balance here\n")
    c = check(run(fakes), "antigravity limits")
    assert not c.ok and c.blocking


def test_antigravity_disabled_is_nonblocking(fakes):
    checks = run(fakes, agy='enabled = false\nbin = "/nonexistent/agy"')
    assert not blocking_failures(checks)
    assert "disabled" in check(checks, "antigravity binary").detail
    assert not fakes.agy_read_log.exists()  # a disabled agent is not even read


def test_doctor_never_runs_a_round(fakes):
    run(fakes)
    run_checks(None, os.environ)
    assert fakes.codex_calls() == []
    assert fakes.agy_calls() == []
    reads = fakes.agy_read_log.read_text().split()
    assert set(reads) == {"/usage", "/credits"}


def test_without_a_goal_both_defaults_are_checked(fakes):
    checks = run_checks(None, os.environ)
    assert check(checks, "codex limits").ok and check(checks, "antigravity limits").ok
    assert not blocking_failures(checks)


def test_missing_workdir_blocks(fakes):
    fakes.work.rmdir()
    c = check(run(fakes), "workdir")
    assert not c.ok and c.blocking


def test_warns_about_other_interactive_codex(fakes, monkeypatch):
    monkeypatch.setattr(
        doctor,
        "list_processes",
        lambda: [
            Proc(111, 1, "codex --yolo"),
            Proc(222, 1, "codex exec --json --skip-git-repo-check -"),
            Proc(333, 1, "codex app-server"),
            Proc(444, 1, "node /opt/lib/node_modules/@openai/codex/bin/codex resume abc"),
            Proc(555, 1, "vim codex.md"),
        ],
    )
    c = check(run(fakes), "interactive codex")
    assert not c.ok and not c.blocking
    assert "111" in c.detail and "444" in c.detail
    assert "222" not in c.detail and "333" not in c.detail and "555" not in c.detail
    assert "share" in c.detail


def test_no_interactive_codex_is_ok(fakes):
    assert check(run(fakes), "interactive codex").ok


def test_codex_disabled_skips_the_interactive_warning(fakes, monkeypatch):
    monkeypatch.setattr(doctor, "list_processes", lambda: [Proc(111, 1, "codex")])
    checks = run(fakes, codex="enabled = false")
    assert not [c for c in checks if "interactive codex" in c.name]


def test_launcher_and_its_native_child_count_once():
    procs = [
        Proc(10, 1, "node /lib/node_modules/@openai/codex/bin/codex --yolo"),
        Proc(11, 10, "/lib/vendor/bin/codex --yolo"),
        Proc(20, 1, "/lib/vendor/bin/codex"),
    ]
    assert [p.pid for p in interactive_codex(procs)] == [10, 20]


def test_interactive_codex_filter():
    procs = [
        Proc(1, 0, "/usr/local/bin/codex"),
        Proc(2, 0, "codex -m gpt-6.1-sol"),
        Proc(3, 0, "codex exec resume abc -"),
        Proc(4, 0, "codex app-server"),
        Proc(5, 0, "codex mcp-server"),
        Proc(6, 0, "/bin/zsh -c codex"),
        Proc(os.getpid(), 0, "codex"),  # ourselves is never "another"
    ]
    assert [p.pid for p in interactive_codex(procs)] == [1, 2]


@pytest.mark.parametrize("balance", ["abc", "NaN", None, ""])
def test_codex_unreadable_balance_blocks(fakes, balance):
    fakes.codex_scenario(codex_limits(balance=balance))
    c = check(run(fakes), "codex limits")
    assert not c.ok and c.blocking


def test_codex_missing_window_blocks(fakes):
    fakes.codex_scenario(codex_limits(primary=None))
    c = check(run(fakes), "codex limits")
    assert not c.ok and c.blocking


def test_antigravity_with_only_the_weekly_row_blocks_and_says_which_is_missing(fakes):
    fakes.agy_files(usage="Gemini Models\tWeekly Limit Remaining\t100%\t2096-01-01T00:00:00Z\n")
    c = check(run(fakes), "antigravity limits")
    assert not c.ok and c.blocking
    assert "Five Hour" in c.detail


@pytest.mark.parametrize(
    "usage", ["garbage\n", "Claude and GPT models\tWeekly\t100%\t2030-01-01T00:00:00Z\n"]
)
def test_antigravity_unreadable_usage_blocks(fakes, usage):
    fakes.agy_files(usage=usage)
    c = check(run(fakes), "antigravity limits")
    assert not c.ok and c.blocking


@pytest.mark.parametrize("credits", ["Remaining credits\tabc\n", "Remaining credits\n", ""])
def test_antigravity_unreadable_balance_blocks(fakes, credits):
    fakes.agy_files(credits=credits)
    c = check(run(fakes), "antigravity limits")
    assert not c.ok and c.blocking


# --- Claude API -------------------------------------------------------------------------------

CLAUDE_KEY = "fake-ant-api03-DOCTORKEY0123456789abcdef"


def claude_ledger(fakes, spent="0", reserved=None):
    """The ledger the goal's defaults point at, with some spend in it."""
    from baton.adapters.claude_api import ledger_for
    from baton.config import default_agent

    ledger = ledger_for(default_agent("claude_api"))
    if Decimal(spent):
        ledger.settle("x", Decimal(spent))
    if reserved:
        ledger.reserve(Decimal(reserved))
    return ledger


def claude(fakes, claude_extra="", **kw):
    fakes.claude_key(CLAUDE_KEY)
    return run(fakes, claude=claude_extra, **kw)


def test_reports_claude_spend_and_console_reminder(fakes):
    claude_ledger(fakes, spent="12.5", reserved="10")
    checks = claude(fakes)
    spend = check(checks, "claude_api spend")
    assert spend.ok and not spend.blocking
    assert "22.5" in spend.detail and "180" in spend.detail  # spent plus the live reservation
    assert "157.5" in spend.detail  # what is left
    assert "10%" in spend.detail and "200" in spend.detail  # budget and reserve
    assert "2026-" in spend.detail or "20" in spend.detail  # when the period resets
    reminder = check(checks, "claude_api console")
    assert reminder.ok and not reminder.blocking
    for word in ("auto-reload", "purchased credits", "workspace", "monthly spend limit", "180"):
        assert word in reminder.detail
    assert check(checks, "claude_api binary").ok
    assert check(checks, "claude_api key").ok


def test_claude_doctor_never_prints_the_key(fakes, capsys):
    from baton.cli import main

    checks = claude(fakes)
    for c in checks:
        assert CLAUDE_KEY not in c.name and CLAUDE_KEY not in c.detail
    fakes.claude_key(CLAUDE_KEY)
    assert main(["doctor", str(fakes.goal(claude=""))]) == 0
    out = capsys.readouterr()
    assert CLAUDE_KEY not in out.out + out.err


def test_claude_doctor_never_runs_a_round_or_creates_the_ledger(fakes):
    claude(fakes)
    assert fakes.claude_calls() == []
    assert not fakes.ledger_file.exists()
    assert not fakes.ledger_file.with_name("claude-spend.json.lock").exists()


def test_stray_anthropic_variables_are_flagged(fakes, monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        monkeypatch.setenv(var, SECRET)
    checks = run(fakes)
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        flagged = check(checks, var)
        assert not flagged.ok and not flagged.blocking
    for c in checks:
        assert SECRET not in c.name and SECRET not in c.detail


def test_claude_subscription_tokens_are_flagged_and_never_shown(fakes, monkeypatch):
    names = ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_REFRESH_TOKEN")
    for var in names:
        monkeypatch.setenv(var, SECRET)
    checks = run(fakes)
    for var in names:
        flagged = check(checks, var)
        assert not flagged.ok and not flagged.blocking
    for c in checks:
        assert SECRET not in c.name and SECRET not in c.detail


def test_claude_without_a_key_is_no_problem_when_others_can_run(fakes):
    checks = run(fakes, claude="")  # no key file written: Claude is optional
    assert check(checks, "claude_api").ok
    assert not blocking_failures(checks)


def test_claude_without_a_key_blocks_when_it_is_the_only_agent(fakes):
    checks = run(fakes, claude="", order=("claude_api",))
    assert check(checks, "claude_api key").blocking
    assert blocking_failures(checks)


def test_claude_key_with_wrong_mode_is_flagged(fakes):
    fakes.claude_key(CLAUDE_KEY, mode=0o644)
    key = check(run(fakes, claude=""), "claude_api key")
    assert not key.ok and "600" in key.detail and CLAUDE_KEY not in key.detail


def test_claude_missing_binary(fakes):
    fakes.claude_key(CLAUDE_KEY)
    checks = run(fakes, claude='bin = "/nonexistent/claude"\n')
    binary = check(checks, "claude_api binary")
    assert not binary.ok and not binary.blocking and "/nonexistent/claude" in binary.detail


NOT_CONFIGURED = "not configured (optional): rounds use the other agents only"


def lines(checks):
    return [c.name for c in checks if c.name.startswith("claude_api")]


def test_claude_with_no_key_file_is_one_ok_line(fakes):
    checks = run(fakes, claude="")  # the fake claude binary is on PATH, no key file
    line = check(checks, "claude_api")
    assert line.name == "claude_api" and line.ok and not line.blocking
    assert line.detail == NOT_CONFIGURED
    assert lines(checks) == ["claude_api"]  # no binary, key, spend or console line
    assert not blocking_failures(checks)


def test_claude_with_neither_binary_nor_key_is_the_same_one_line(fakes):
    checks = run(fakes, claude='bin = "/nonexistent/claude"\n')
    assert check(checks, "claude_api").detail == NOT_CONFIGURED
    assert lines(checks) == ["claude_api"]


def test_the_not_configured_line_names_no_agent(fakes):
    detail = check(run(fakes, claude=""), "claude_api").detail
    assert "Codex" not in detail and "Antigravity" not in detail


def test_claude_not_configured_prints_exactly_that_line(fakes, capsys):
    from baton.cli import main

    path = fakes.goal(claude="")
    assert main(["doctor", str(path)]) == 0
    out = capsys.readouterr().out
    assert f"[  ok] claude_api: {NOT_CONFIGURED}\n" in out
    for word in ("binary", "key", "spend", "console"):
        assert f"claude_api {word}" not in out


def test_claude_with_a_key_but_no_binary_keeps_the_warnings(fakes):
    fakes.claude_key(CLAUDE_KEY)
    checks = run(fakes, claude='bin = "/nonexistent/claude"\n')
    assert "claude_api" not in lines(checks)
    assert not check(checks, "claude_api binary").ok
    assert check(checks, "claude_api key").ok


def test_claude_with_a_key_of_the_wrong_mode_keeps_the_warning(fakes):
    fakes.claude_key(CLAUDE_KEY, mode=0o644)
    checks = run(fakes, claude="")
    assert "claude_api" not in lines(checks)
    key = check(checks, "claude_api key")
    assert not key.ok and "600" in key.detail


def test_claude_disabled_keeps_its_one_disabled_line(fakes):
    checks = run(fakes)  # enabled = false
    assert check(checks, "claude_api binary").detail == "disabled in the goal file"
    assert "claude_api" not in lines(checks)


def test_claude_as_the_only_agent_keeps_its_blocking_checks_even_with_no_key(fakes):
    checks = run(fakes, claude="", order=("claude_api",))
    assert "claude_api" not in lines(checks)
    assert check(checks, "claude_api key").blocking
    assert blocking_failures(checks)


def test_claude_is_the_only_agent_when_the_others_are_disabled(fakes):
    checks = run(
        fakes,
        claude="",
        codex="enabled = false",
        agy="enabled = false",
        order=("codex", "antigravity", "claude_api"),
    )
    assert "claude_api" not in lines(checks)
    assert blocking_failures(checks)


def test_claude_not_configured_without_a_goal(fakes, monkeypatch):
    monkeypatch.setenv("HOME", str(fakes.root / "home"))
    checks = run_checks(None, os.environ)
    assert check(checks, "claude_api").detail == NOT_CONFIGURED


def test_claude_outside_the_order_never_blocks(fakes):
    checks = run(fakes, claude='bin = "/nonexistent/claude"\n')  # default order has no claude_api
    assert not blocking_failures(checks)


def test_claude_at_the_ceiling_is_a_warning(fakes):
    claude_ledger(fakes, spent="179.5")
    spend = check(claude(fakes), "claude_api spend")
    assert not spend.ok and not spend.blocking
    assert "179.5" in spend.detail and "ceiling" in spend.detail


def test_claude_corrupt_ledger_is_flagged_and_kept(fakes):
    fakes.ledger_file.parent.mkdir(parents=True)
    ledger = fakes.ledger_file
    ledger.write_text("{garbage")
    spend = check(claude(fakes), "claude_api spend")
    assert not spend.ok and "claude-spend.json" in spend.detail
    assert ledger.read_text() == "{garbage"
    blocked = check(claude(fakes, order=("claude_api",)), "claude_api spend")
    assert blocked.blocking


def test_claude_uses_the_cost_report_when_an_admin_key_is_set(fakes, monkeypatch):
    from baton.adapters import claude_api

    admin = fakes.root / "admin.key"
    admin.write_text("fake-ant-admin01-ADMINKEY\n")
    admin.chmod(0o600)
    monkeypatch.setattr(claude_api, "fetch_cost_report", lambda path, since, **kw: Decimal("170"))
    claude_ledger(fakes, spent="20")
    checks = claude(fakes, claude_extra=f'admin_key_file = "{admin}"\n')
    spend = check(checks, "claude_api spend")
    assert "170" in spend.detail and "10" in spend.detail  # the larger figure wins
    assert check(checks, "claude_api cost report").ok


def test_claude_cost_report_failure_is_flagged(fakes, monkeypatch):
    from baton.adapters import claude_api

    admin = fakes.root / "admin.key"
    admin.write_text("fake-ant-admin01-ADMINKEY\n")
    admin.chmod(0o600)

    def broken(path, since, **kw):
        raise claude_api.CostReportError("the Admin API answered HTTP 500")

    monkeypatch.setattr(claude_api, "fetch_cost_report", broken)
    checks = claude(fakes, claude_extra=f'admin_key_file = "{admin}"\n')
    report = check(checks, "claude_api cost report")
    assert not report.ok and "HTTP 500" in report.detail
    assert "ADMINKEY" not in report.detail


def test_claude_disabled_is_not_checked(fakes):
    checks = run(fakes, claude="enabled = false\n")
    assert "disabled" in check(checks, "claude_api binary").detail
    assert not [c for c in checks if c.name.startswith("claude_api") and "binary" not in c.name]


def test_claude_ledger_with_other_budget_settings_is_flagged_with_a_way_out(fakes):
    from baton.ledger import Ledger

    Ledger(fakes.ledger_file, Decimal("360"), 15).reserve(Decimal("10"))
    spend = check(claude(fakes), "claude_api spend")
    assert not spend.ok and "defaults.toml" in spend.detail and "360" in spend.detail


def test_claude_deleted_ledger_is_flagged(fakes):
    claude_ledger(fakes, spent="20")
    fakes.ledger_file.unlink()
    spend = check(claude(fakes), "claude_api spend")
    assert not spend.ok and "deleted" in spend.detail
    assert not fakes.ledger_file.exists()


def test_doctor_reads_the_budget_from_the_defaults_file(fakes):
    fakes.defaults("[agents.claude_api]\nmonthly_budget_usd = 100\nreserve_pct = 20\n")
    spend = check(claude(fakes), "claude_api spend")
    assert "80" in spend.detail and "budget 100" in spend.detail and "20%" in spend.detail
    assert "spend limit of 80 USD" in check(claude(fakes), "claude_api console").detail


# long-term memory


@pytest.fixture
def probe(monkeypatch):
    """Memory switched on, with its server probe, its processes and its binary lookup faked, so
    that no test here depends on whether ai-memory is installed on the machine running it.
    `found` maps what `which` is asked for to the path it answers; nothing in it, no binary."""
    monkeypatch.delenv("BATON_MEMORY", raising=False)
    state = {"up": False, "started": [], "found": {}}

    def which(command, path=None):
        return state["found"].get(command)

    def build(cfg, workdir, job):
        def refuse(*a, **k):
            state["started"].append(a)
            raise AssertionError("doctor must start nothing")

        return Memory(
            cfg,
            workdir,
            job,
            run=refuse,
            popen=refuse,
            health=lambda port: state["up"],
            which=which,
        )

    monkeypatch.setattr(doctor, "Memory", build)
    return state


def memory_check(checks):
    return check(checks, "memory")


def fake_binary(fakes, probe, name="ai-memory"):
    """An ai-memory that the faked lookup finds, under `name` and under its full path."""
    path = fakes.bindir / name
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    probe["found"][name] = probe["found"][str(path)] = str(path)
    return path


def test_memory_off_by_the_environment_is_reported_and_not_a_problem(fakes):
    c = memory_check(run(fakes))  # BATON_MEMORY=off from the autouse fixture
    assert c.ok and not c.blocking and "off" in c.detail and "BATON_MEMORY" in c.detail


@pytest.mark.parametrize("value", ["off", "OFF", " off ", "Off\n"])
def test_the_environment_reason_is_given_for_every_spelling_that_turns_memory_off(
    fakes, monkeypatch, value
):
    monkeypatch.setenv("BATON_MEMORY", value)
    c = memory_check(run(fakes))
    assert c.ok and "BATON_MEMORY=off" in c.detail and "enabled = false" not in c.detail


@pytest.mark.parametrize("value", ["on", "0", "false", "", " "])
def test_a_variable_that_does_not_turn_memory_off_is_not_the_reason(fakes, monkeypatch, value):
    """Only what `load_memory` treats as off is named as the reason: with anything else set, an
    `enabled = false` in the settings is what turned it off."""
    monkeypatch.setenv("BATON_MEMORY", value)
    fakes.defaults("[memory]\nenabled = false\n")
    c = memory_check(run(fakes))
    assert c.ok and "enabled = false" in c.detail and "BATON_MEMORY" not in c.detail


def test_memory_off_in_the_settings_is_reported(fakes, monkeypatch):
    monkeypatch.delenv("BATON_MEMORY")
    fakes.defaults("[memory]\nenabled = false\n")
    c = memory_check(run(fakes))
    assert c.ok and "off" in c.detail and "enabled = false" in c.detail


def test_memory_off_in_the_goal_is_reported(fakes, probe):
    c = memory_check(run(fakes, extra="memory = false"))
    assert c.ok and "off" in c.detail and "goal" in c.detail


def test_a_missing_binary_is_a_warning_that_says_rounds_run_without_memory(fakes, probe):
    c = memory_check(run(fakes))
    assert not c.ok and not c.blocking
    assert "without memory" in c.detail and "ai-memory" in c.detail
    assert not blocking_failures(run(fakes))


def test_an_ai_memory_on_the_real_path_is_not_seen_by_the_memory_tests(fakes, probe):
    """These tests answer for themselves, whatever is installed where they run."""
    real = fakes.bindir / "ai-memory"
    real.write_text("#!/bin/sh\n")
    real.chmod(0o755)
    c = memory_check(run(fakes))
    assert not c.ok and "without memory" in c.detail and str(real) not in c.detail


def test_an_installed_binary_is_reported_with_its_path_and_the_server_state(fakes, probe):
    path = fake_binary(fakes, probe)
    down = memory_check(run(fakes))
    assert down.ok and str(path) in down.detail and "not running" in down.detail
    probe["up"] = True
    up = memory_check(run(fakes))
    assert up.ok and str(path) in up.detail and "up" in up.detail and "not running" not in up.detail
    assert probe["started"] == []


def test_an_old_server_in_the_way_is_a_warning_with_the_same_words(fakes, probe, monkeypatch):
    from baton.store import state_base

    fake_binary(fakes, probe)
    data = state_base() / ".ai-memory"
    data.mkdir(parents=True)
    (data / "config.toml").write_text('bind = "127.0.0.1:19999"\n')

    def build(cfg, workdir, job):
        return Memory(
            cfg,
            workdir,
            job,
            health=lambda port: port == 19999,
            which=lambda command, path=None: probe["found"].get(command),
        )

    monkeypatch.setattr(doctor, "Memory", build)
    c = memory_check(run(fakes))
    assert not c.ok and not c.blocking
    assert "stop the old server" in c.detail and "19999" in c.detail
    assert not blocking_failures(run(fakes))


def test_a_bin_from_the_settings_is_the_one_looked_up(fakes, probe):
    path = fake_binary(fakes, probe, "my-ai-memory")
    fakes.defaults(f'[memory]\nbin = "{path}"\n')
    assert str(path) in memory_check(run(fakes)).detail


def test_bad_memory_settings_are_a_warning_not_a_blocker(fakes, probe):
    fakes.defaults("[memory]\nport = 5\n")
    checks = run(fakes)
    c = memory_check(checks)
    assert not c.ok and not c.blocking
    assert "port" in c.detail and "without memory" in c.detail
    assert not blocking_failures(checks)


def test_the_memory_check_is_there_without_a_goal_too(fakes, probe):
    checks = run_checks(None, os.environ)
    assert len([c for c in checks if c.name == "memory"]) == 1

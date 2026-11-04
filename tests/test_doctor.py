import os
from decimal import Decimal

import pytest
from conftest import agy_credits, agy_usage, codex_limits, iso

from baton import doctor
from baton.config import load_goal
from baton.doctor import Check, Proc, interactive_codex, run_checks

SECRET = "sk-very-secret-value-12345"


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

CLAUDE_KEY = "sk-ant-api03-DOCTORKEY0123456789abcdef"


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
    assert not (fakes.state / "claude-spend.json").exists()
    assert not (fakes.state / "claude-spend.json.lock").exists()


def test_stray_anthropic_variables_are_flagged(fakes, monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        monkeypatch.setenv(var, SECRET)
    checks = run(fakes)
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        flagged = check(checks, var)
        assert not flagged.ok and not flagged.blocking
    for c in checks:
        assert SECRET not in c.name and SECRET not in c.detail


def test_claude_without_a_key_is_a_warning_when_others_can_run(fakes):
    checks = run(fakes, claude="")  # no key file written
    key = check(checks, "claude_api key")
    assert not key.ok and not key.blocking and "not found" in key.detail
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


def test_claude_outside_the_order_never_blocks(fakes):
    checks = run(fakes, claude='bin = "/nonexistent/claude"\n')  # default order has no claude_api
    assert not blocking_failures(checks)


def test_claude_at_the_ceiling_is_a_warning(fakes):
    claude_ledger(fakes, spent="179.5")
    spend = check(claude(fakes), "claude_api spend")
    assert not spend.ok and not spend.blocking
    assert "179.5" in spend.detail and "ceiling" in spend.detail


def test_claude_corrupt_ledger_is_flagged_and_kept(fakes):
    fakes.state.mkdir(parents=True)
    ledger = fakes.state / "claude-spend.json"
    ledger.write_text("{garbage")
    spend = check(claude(fakes), "claude_api spend")
    assert not spend.ok and "claude-spend.json" in spend.detail
    assert ledger.read_text() == "{garbage"
    blocked = check(claude(fakes, order=("claude_api",)), "claude_api spend")
    assert blocked.blocking


def test_claude_uses_the_cost_report_when_an_admin_key_is_set(fakes, monkeypatch):
    from baton.adapters import claude_api

    admin = fakes.root / "admin.key"
    admin.write_text("sk-ant-admin01-ADMINKEY\n")
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
    admin.write_text("sk-ant-admin01-ADMINKEY\n")
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

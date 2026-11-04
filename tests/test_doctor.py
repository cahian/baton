import os

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

import json
import os
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from baton.adapters.antigravity import (
    GEMINI_MODELS,
    AntigravityAdapter,
    LimitsError,
    Reading,
    _parse_usage_lenient,
    check_usage,
    classify,
    evaluate,
    parse_credits,
    parse_usage,
    read_text,
)
from baton.adapters.base import (
    AUTH,
    COMPLETED,
    CREDIT_GUARD,
    LIMIT,
    TIMEOUT,
    TRANSIENT,
    RoundContext,
)
from baton.config import AgentConfig

FAKE = Path(__file__).parent / "fakes" / "fake_agy.py"
NOW = 1_700_000_000.0

# The sample printed by the real `/usage` and `/credits` commands (one run, 04/11/2026).
REAL_USAGE = (
    "Gemini Models\tWeekly Limit Remaining\t100%\t2026-11-11T17:56:19Z\n"
    "Gemini Models\tFive Hour Limit Remaining\t100%\t2026-11-04T22:56:19Z\n"
    "Claude and GPT models\tWeekly Limit Remaining\t100%\t2026-11-11T17:56:19Z\n"
    "Gemini 4 Argon\tGemini 4 Argon Weekly Limit\t99%\t2026-11-11T17:56:01Z\n"
)
REAL_CREDITS = "Remaining credits\t0\nUpgrade\thttps://antigravity.google/g1-upgrade\n"


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def usage(
    five=100, weekly=100, model=99, claude=100, five_reset=NOW + 1000, weekly_reset=NOW + 2000
):
    """A `/usage` text; the numbers are percent REMAINING, as the command prints them."""
    return (
        f"Gemini Models\tWeekly Limit Remaining\t{weekly}%\t{iso(weekly_reset)}\n"
        f"Gemini Models\tFive Hour Limit Remaining\t{five}%\t{iso(five_reset)}\n"
        f"Claude and GPT models\tWeekly Limit Remaining\t{claude}%\t{iso(weekly_reset)}\n"
        f"Gemini 4 Argon\tGemini 4 Argon Weekly Limit\t{model}%\t{iso(weekly_reset)}\n"
    )


def credits(balance="5") -> str:
    return f"Remaining credits\t{balance}\nUpgrade\thttps://antigravity.google/g1-upgrade\n"


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """An `agy` executable that runs the fake, plus the files it reads and writes."""
    wrapper = tmp_path / "agy"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    wrapper.chmod(0o755)
    calls = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_AGY_CALLS", str(calls))
    monkeypatch.setenv("FAKE_AGY_USAGE", str(tmp_path / "usage.txt"))
    monkeypatch.setenv("FAKE_AGY_CREDITS", str(tmp_path / "credits.txt"))
    monkeypatch.setenv("FAKE_AGY_READ_LOG", str(tmp_path / "reads.log"))
    monkeypatch.setenv("FAKE_AGY_PIDFILE", str(tmp_path / "pid"))
    (tmp_path / "usage.txt").write_text(usage())
    (tmp_path / "credits.txt").write_text(credits())

    class Fake:
        bin = str(wrapper)
        pidfile = tmp_path / "pid"
        read_log = tmp_path / "reads.log"

        @staticmethod
        def calls():
            if not calls.exists():
                return []
            return [json.loads(x) for x in calls.read_text().splitlines()]

    return Fake


def make_cfg(bin_path="agy", **kw) -> AgentConfig:
    d = dict(
        name="antigravity",
        enabled=True,
        model="gemini-4-argon-medium",
        effort="medium",
        threshold_pct=90,
        poll_s=30,
        bin=bin_path,
        full_access=True,
    )
    d.update(kw)
    return AgentConfig(**d)


def make_ctx(tmp_path: Path, **kw) -> RoundContext:
    d = dict(
        workdir=tmp_path,
        log_path=tmp_path / "logs" / "1-antigravity.log",
        round_max_s=30,
        idle_s=30,
        on_output=lambda line: None,
        should_kill=lambda: False,
        on_spawn=lambda pid: None,
    )
    d.update(kw)
    return RoundContext(**d)


class Reads:
    """A scripted `read(command)`: per command a list of texts, the last one repeats.

    An Exception entry is raised. `calls` counts reads per command.
    """

    def __init__(self, usage_=None, credits_=None):
        self.script = {
            "/usage": list(usage_ if usage_ is not None else [usage()]),
            "/credits": list(credits_ if credits_ is not None else [credits()]),
        }
        self.calls = {"/usage": 0, "/credits": 0}

    def __call__(self, command: str) -> str:
        entries = self.script[command]
        entry = entries[min(self.calls[command], len(entries) - 1)]
        self.calls[command] += 1
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


def adapter(bin_path, reads, **kw) -> AntigravityAdapter:
    a = AntigravityAdapter(make_cfg(bin_path, **kw), read=reads, poll_clock=Clock())
    a.tick_s = 0.05
    return a


def frozen_adapter(bin_path, reads, **kw) -> AntigravityAdapter:
    """Poll clock frozen: no tick ever polls, so the closing read is the only read after the
    baseline."""
    return AntigravityAdapter(make_cfg(bin_path, **kw), read=reads, poll_clock=lambda: 0.0)


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


# --- parsing ---------------------------------------------------------------------------------


def test_parse_real_usage_sample():
    rows = parse_usage(REAL_USAGE)
    assert [(r["group"], r["label"]) for r in rows] == [
        ("Gemini Models", "Weekly Limit Remaining"),
        ("Gemini Models", "Five Hour Limit Remaining"),
        ("Claude and GPT models", "Weekly Limit Remaining"),
        ("Gemini 4 Argon", "Gemini 4 Argon Weekly Limit"),
    ]
    assert [r["used_pct"] for r in rows] == [0, 0, 0, 1]
    assert rows[0]["resets_at"] == datetime(2026, 11, 11, 17, 56, 19, tzinfo=UTC).timestamp()
    assert rows[3]["resets_at"] == datetime(2026, 11, 11, 17, 56, 1, tzinfo=UTC).timestamp()


def test_parse_real_credits_sample():
    assert parse_credits(REAL_CREDITS) == Decimal(0)


def test_parse_credits():
    assert parse_credits("Remaining credits\t5") == Decimal("5")
    assert parse_credits("Remaining credits\t4.99\n") == Decimal("4.99")
    assert parse_credits("noise\nRemaining credits\t1000.50\nUpgrade\tx\n") == Decimal("1000.50")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   \n",
        "garbage",
        "Remaining credits\n",
        "Remaining credits\t",
        "Remaining credits\tmany",
        "Remaining credits\t1_000",
        "Remaining credits\tNaN",
        "Remaining credits\tInfinity",
        "Remaining credits\t-1",
        "Remaining credits\t1,5",
        "Remaining credits\t5\nRemaining credits\t6",
        "Credits left\t5",
    ],
)
def test_parse_credits_raises_on_anything_unclear(text):
    with pytest.raises(LimitsError):
        parse_credits(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "\n\n",
        "garbage without tabs",
        "Claude and GPT models\tWeekly Limit Remaining\t100%\t2026-11-11T17:56:19Z\n",
        "Gemini 4 Argon\tGemini 4 Argon Weekly Limit\t99%\t2026-11-11T17:56:01Z\n",
        "Gemini Models\tWeekly Limit Remaining\tlots\t2026-11-11T17:56:19Z\n",
        "Gemini Models\tWeekly Limit Remaining\t120%\t2026-11-11T17:56:19Z\n",
        "Gemini Models\tWeekly Limit Remaining\t100%\tsoon\n",
        "Gemini Models\tWeekly Limit Remaining\t100%\t2026-11-11T17:56:19\n",
        "Gemini Models\tWeekly Limit Remaining\t100%\n",
    ],
    ids=[
        "empty",
        "blank",
        "garbage",
        "no-gemini-models",
        "model-row-only",
        "bad-percent",
        "percent-over-100",
        "bad-date",
        "naive-date",
        "missing-column",
    ],
)
def test_parse_usage_raises_on_anything_unclear(text):
    with pytest.raises(LimitsError):
        parse_usage(text)


def test_parse_usage_ignores_malformed_rows_of_other_groups():
    text = REAL_USAGE + "Claude and GPT models\tbroken\n\nsome banner line\n"
    assert len(parse_usage(text)) == 4


# --- check_usage -----------------------------------------------------------------------------


def rows_of(**kw):
    return parse_usage(usage(**kw))


def test_check_usage_all_clear():
    a = check_usage(rows_of(), 90, NOW)
    assert a.ok and a.until is None


def test_check_usage_ignores_claude_and_gpt_models_at_zero_remaining():
    a = check_usage(rows_of(claude=0), 90, NOW)
    assert a.ok


def test_check_usage_eleven_percent_remaining_is_ok_ten_is_not():
    assert check_usage(rows_of(five=11), 90, NOW).ok
    a = check_usage(rows_of(five=10, five_reset=NOW + 500), 90, NOW)
    assert not a.ok and a.until == NOW + 500


def test_check_usage_model_row_counts():
    a = check_usage(rows_of(model=5), 90, NOW)
    assert not a.ok and "Gemini 4 Argon" in a.reason


def test_check_usage_until_is_the_latest_saturated_reset():
    a = check_usage(
        rows_of(five=0, weekly=0, five_reset=NOW + 500, weekly_reset=NOW + 9000), 90, NOW
    )
    assert not a.ok and a.until == NOW + 9000


def test_check_usage_ignores_the_reset_of_rows_that_are_not_saturated():
    a = check_usage(rows_of(five=0, five_reset=NOW + 500, weekly_reset=NOW + 9000), 90, NOW)
    assert a.until == NOW + 500


def test_check_usage_stale_reset_does_not_become_a_busy_loop():
    a = check_usage(rows_of(five=0, five_reset=NOW - 5), 90, NOW)
    assert not a.ok and a.until == NOW + 300


def test_check_usage_honours_the_threshold():
    assert check_usage(rows_of(five=30), 90, NOW).ok
    assert not check_usage(rows_of(five=30), 70, NOW).ok


ONLY_WEEKLY = "Gemini Models\tWeekly Limit Remaining\t100%\t2026-11-11T17:56:19Z\n"
ONLY_FIVE_HOUR = "Gemini Models\tFive Hour Limit Remaining\t100%\t2026-11-04T22:56:19Z\n"


@pytest.mark.parametrize(
    ("text", "missing"), [(ONLY_WEEKLY, "Five Hour"), (ONLY_FIVE_HOUR, "Weekly")]
)
def test_one_gemini_models_row_alone_is_not_enough(text, missing):
    with pytest.raises(LimitsError, match=missing):
        parse_usage(text)
    rows, problems = _parse_usage_lenient(text)
    assert len(rows) == 1 and missing in problems[0]
    verdict = evaluate(Reading(usage=text, credits="Remaining credits\t0\n"), 90, NOW)
    assert not verdict.avail.ok and verdict.blind and missing in verdict.avail.reason
    assert verdict.avail.until == NOW + 300


@pytest.mark.parametrize(
    ("text", "missing"), [(ONLY_WEEKLY, "Five Hour"), (ONLY_FIVE_HOUR, "Weekly")]
)
def test_check_usage_needs_both_gemini_models_rows(text, missing):
    rows, _ = _parse_usage_lenient(text)
    avail = check_usage(rows, 90, NOW)
    assert not avail.ok and missing in avail.reason


def test_a_model_row_cannot_stand_in_for_a_gemini_models_row():
    text = (
        ONLY_WEEKLY + "Gemini 4 Argon\tGemini 4 Argon Five Hour Limit\t99%\t2026-11-11T17:56:01Z\n"
    )
    with pytest.raises(LimitsError, match="Five Hour"):
        parse_usage(text)


def test_check_usage_without_gemini_models_rows_is_unavailable():
    a = check_usage([], 90, NOW)
    assert not a.ok and a.until == NOW + 300
    only_model = [r for r in rows_of() if r["group"] == "Gemini 4 Argon"]
    assert not check_usage(only_model, 90, NOW).ok


# --- classify --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "RESOURCE_EXHAUSTED: try later",
        "quota exceeded for this model",
        "You hit a rate limit",
        "rate-limit reached",
        "HTTP 429 Too Many Requests",
    ],
)
def test_classify_limit(text):
    res = classify(text, NOW)
    assert res is not None and res.outcome == LIMIT


@pytest.mark.parametrize(
    "text",
    [
        "authentication failed or timed out",
        "Run 'agy' to log in",
        "please sign in again",
        "UNAUTHENTICATED",
        "you must reauthenticate",
        "login_required",
    ],
)
def test_classify_auth(text):
    res = classify(text, NOW)
    assert res is not None and res.outcome == AUTH


@pytest.mark.parametrize(
    "text",
    [
        "",
        "boom",
        "conversation not found",
        "internal error 500",
        "could not assign interface",
        "assign in order",
        "catalog in memory",
        "the catalog index is stale",
        "design signing in progress elsewhere",
    ],
)
def test_classify_other_text_is_left_to_the_caller(text):
    assert classify(text, NOW) is None


# --- evaluating and the adapter surface ------------------------------------------------------


def test_disabled_is_unavailable():
    a = AntigravityAdapter(make_cfg(enabled=False), read=Reads())
    res = a.availability()
    assert not res.ok and "disabled" in res.reason


def test_availability_reads_both_commands_and_checks():
    reads = Reads()
    a = AntigravityAdapter(make_cfg(), read=reads)
    res = a.availability()
    assert res.ok and res.usage["balance"] == "5"
    assert reads.calls == {"/usage": 1, "/credits": 1}
    assert not AntigravityAdapter(make_cfg(), read=Reads([usage(five=0)])).availability().ok


@pytest.mark.parametrize(
    "reads",
    [
        Reads([RuntimeError("down")]),
        Reads(credits_=[RuntimeError("down")]),
        Reads(["garbage"]),
        Reads(credits_=["garbage"]),
        Reads([usage().replace("Gemini Models", "Other")]),
    ],
    ids=["usage-fails", "credits-fail", "usage-garbage", "credits-garbage", "no-gemini-models"],
)
def test_availability_unknown_means_unavailable_for_five_minutes(reads):
    now = time.time()
    res = AntigravityAdapter(make_cfg(), read=reads).availability()
    assert not res.ok
    assert res.until is not None and 290 < res.until - now < 310


def test_availability_refusal_reports_the_reset_even_when_credits_are_unreadable():
    reset = time.time() + 3600
    reads = Reads([usage(five=0, five_reset=reset)], credits_=["garbage"])
    res = AntigravityAdapter(make_cfg(), read=reads).availability()
    assert not res.ok and abs(res.until - reset) < 1


def test_render_extracts_text_delta():
    a = AntigravityAdapter(make_cfg(), read=Reads())
    line = json.dumps(
        {
            "event": "step_update",
            "step_update": {"conversation_id": "c", "step_index": 1, "text_delta": "ok"},
        }
    )
    assert a.render(line) == "ok"
    assert a.render(json.dumps({"event": "init", "init": {"conversation_id": "c"}})) is None
    assert a.render('{"event":"step_update","step_update":{"step_index":2}}') is None
    assert a.render('{"event":"result","result":{"status":"SUCCESS","response":"ok\\n"}}') is None
    assert (
        a.render('{"event":"result","result":{"status":"ERROR","error":"boom"}}') == "error: boom"
    )
    assert a.render("plain text line") == "plain text line"
    assert a.render("   ") is None


def test_argv_fresh_and_resume():
    a = AntigravityAdapter(make_cfg("/opt/agy"), read=Reads())
    assert a.argv("do it", None) == [
        "/opt/agy",
        "-p",
        "do it",
        "--output-format",
        "stream-json",
        "--model",
        "gemini-4-argon-medium",
        "--dangerously-skip-permissions",
    ]
    assert a.argv("do it", "conv-9")[-2:] == ["--conversation", "conv-9"]
    assert "--dangerously-skip-permissions" in a.argv("do it", "conv-9")


def test_argv_without_full_access_drops_the_bypass_flag():
    a = AntigravityAdapter(make_cfg(full_access=False), read=Reads())
    assert "--dangerously-skip-permissions" not in a.argv("x", None)


def test_a_prompt_cannot_turn_into_a_slash_command_or_break_exec():
    a = AntigravityAdapter(make_cfg(), read=Reads())
    assert a.argv("/credits", None)[2] != "/credits"
    assert a.argv("  /usage please", None)[2] != "  /usage please"
    assert a.argv("a\x00b", None)[2] == "ab"


# --- rounds ----------------------------------------------------------------------------------


def test_round_completes_when_nothing_moves(fake, tmp_path):
    reads = Reads()
    lines = []
    res = adapter(fake.bin, reads).run_round("go", make_ctx(tmp_path, on_output=lines.append))
    assert res.outcome == COMPLETED
    # baseline + closing read, plus a poll on every tick of the jumping clock; both commands
    # are always read together
    assert reads.calls["/usage"] >= 2 and reads.calls["/usage"] == reads.calls["/credits"]
    assert any("text_delta" in x for x in lines)


def test_stdin_closed(fake, tmp_path):
    # The fake exits 3 if stdin yields data and 4 if it stays open: either would not be COMPLETED.
    res = adapter(fake.bin, Reads()).run_round("go", make_ctx(tmp_path))
    assert res.outcome == COMPLETED and res.reason == ""


def test_round_runs_in_the_workdir_with_the_prompt_in_argv(fake, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    adapter(fake.bin, Reads()).run_round("fix the bug", make_ctx(tmp_path, workdir=work))
    (call,) = fake.calls()
    assert call["cwd"] == str(work.resolve()) or call["cwd"] == str(work)
    assert call["argv"][:2] == ["-p", "fix the bug"]
    assert "--output-format" in call["argv"] and "stream-json" in call["argv"]


def test_session_id_from_init(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_CONVERSATION_ID", "conv-42")
    seen = []
    res = adapter(fake.bin, Reads()).run_round("go", make_ctx(tmp_path, on_session=seen.append))
    assert res.outcome == COMPLETED
    assert seen == ["conv-42"]


def test_resume_argv(fake, tmp_path):
    seen = []
    adapter(fake.bin, Reads()).run_round(
        "again", make_ctx(tmp_path, session_id="conv-7", on_session=seen.append)
    )
    (call,) = fake.calls()
    assert call["argv"][-2:] == ["--conversation", "conv-7"]
    assert seen == ["conv-7"]


def test_failed_resume_sets_resume_failed(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_RESULT_STATUS", "ERROR")
    monkeypatch.setenv("FAKE_AGY_RESULT_ERROR", "conversation not found")
    monkeypatch.setenv("FAKE_AGY_EXEC_EXIT", "1")
    res = adapter(fake.bin, Reads()).run_round("go", make_ctx(tmp_path, session_id="gone"))
    assert res.outcome == TRANSIENT and res.resume_failed
    assert "conversation not found" in res.reason


def test_failed_fresh_round_is_plain_transient(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_RESULT_STATUS", "ERROR")
    monkeypatch.setenv("FAKE_AGY_EXEC_EXIT", "1")
    res = adapter(fake.bin, Reads()).run_round("go", make_ctx(tmp_path))
    assert res.outcome == TRANSIENT and not res.resume_failed and "boom" in res.reason


def test_resume_hitting_a_limit_is_a_limit_not_a_resume_failure(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_RESULT_STATUS", "ERROR")
    monkeypatch.setenv("FAKE_AGY_RESULT_ERROR", "RESOURCE_EXHAUSTED: quota exceeded")
    res = adapter(fake.bin, Reads()).run_round("go", make_ctx(tmp_path, session_id="x"))
    assert res.outcome == LIMIT and not res.resume_failed


def test_login_error_is_auth(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_RESULT_STATUS", "ERROR")
    monkeypatch.setenv("FAKE_AGY_RESULT_ERROR", "authentication failed or timed out")
    res = adapter(fake.bin, Reads()).run_round("go", make_ctx(tmp_path))
    assert res.outcome == AUTH


def test_non_json_line_is_scanned_for_a_failure(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_RAW_LINE", "Run 'agy' to log in")
    monkeypatch.setenv("FAKE_AGY_NO_RESULT", "1")
    monkeypatch.setenv("FAKE_AGY_EXEC_EXIT", "1")
    assert adapter(fake.bin, Reads()).run_round("go", make_ctx(tmp_path)).outcome == AUTH


def test_agent_text_mentioning_limits_does_not_classify(fake, tmp_path, monkeypatch):
    # Only the result's error and non-JSON lines are scanned, never what the agent says.
    monkeypatch.setenv("FAKE_AGY_TEXT", "I hit a quota wall, HTTP 429, please log in again")
    monkeypatch.setenv("FAKE_AGY_RESULT_STATUS", "ERROR")
    monkeypatch.setenv("FAKE_AGY_EXEC_EXIT", "1")
    reads = Reads()
    res = adapter(fake.bin, reads).run_round("go", make_ctx(tmp_path))
    assert res.outcome == TRANSIENT  # the delta said quota/429/log in, the error said "boom"


def test_exit_without_a_result_event_is_transient(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_NO_RESULT", "1")
    res = adapter(fake.bin, Reads()).run_round("go", make_ctx(tmp_path))
    assert res.outcome == TRANSIENT and "result" in res.reason


def test_round_timeout_is_timeout(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_EXEC_SLEEP", "30")
    res = adapter(fake.bin, Reads()).run_round("go", make_ctx(tmp_path, round_max_s=1))
    assert res.outcome == TIMEOUT


def test_missing_binary_is_transient(tmp_path):
    a = adapter(str(tmp_path / "nope"), Reads())
    res = a.run_round("go", make_ctx(tmp_path))
    assert res.outcome == TRANSIENT and "cannot start" in res.reason


# --- the credit guard ------------------------------------------------------------------------


def test_round_stops_on_credit_drop(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_EXEC_SLEEP", "30")
    reads = Reads(credits_=[credits("5"), credits("4.99")])
    pids = []
    res = adapter(fake.bin, reads).run_round("go", make_ctx(tmp_path, on_spawn=pids.append))
    assert res.outcome == CREDIT_GUARD
    assert "5" in res.reason and "4.99" in res.reason
    assert wait_dead(pids[0])


def test_credit_drop_beats_threshold(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_EXEC_SLEEP", "30")
    reset = time.time() + 3600
    reads = Reads([usage(), usage(five=0, five_reset=reset)], [credits("5"), credits("4")])
    res = adapter(fake.bin, reads).run_round("go", make_ctx(tmp_path))
    assert res.outcome == CREDIT_GUARD
    assert abs(res.until - reset) < 1  # a cooldown is still known


def test_credit_increase_is_not_a_drop(fake, tmp_path):
    reads = Reads(credits_=[credits("5"), credits("6")])
    assert adapter(fake.bin, reads).run_round("go", make_ctx(tmp_path)).outcome == COMPLETED


def test_round_stops_at_threshold(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_EXEC_SLEEP", "30")
    reset = time.time() + 3600
    reads = Reads([usage(), usage(five=10, five_reset=reset)])
    pids = []
    res = adapter(fake.bin, reads).run_round("go", make_ctx(tmp_path, on_spawn=pids.append))
    assert res.outcome == LIMIT and abs(res.until - reset) < 1
    assert wait_dead(pids[0])


def test_two_failed_reads_stop_round_as_credit_guard(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_EXEC_SLEEP", "30")
    reads = Reads(credits_=[credits(), LimitsError("down")])
    pids = []
    res = adapter(fake.bin, reads).run_round("go", make_ctx(tmp_path, on_spawn=pids.append))
    assert res.outcome == CREDIT_GUARD and "2 times" in res.reason
    assert wait_dead(pids[0])


def test_two_unparseable_reads_stop_round_as_credit_guard(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_EXEC_SLEEP", "30")
    reads = Reads([usage(), "garbage"])
    assert adapter(fake.bin, reads).run_round("go", make_ctx(tmp_path)).outcome == CREDIT_GUARD


def test_one_failed_read_keeps_going(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_EXEC_SLEEP", "1")
    reads = Reads(credits_=[credits(), LimitsError("blip"), credits()])
    a = adapter(fake.bin, reads)
    a.tick_s = 0.3
    res = a.run_round("go", make_ctx(tmp_path))
    assert res.outcome == COMPLETED
    assert reads.calls["/credits"] >= 3


def test_failures_must_be_consecutive(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_EXEC_SLEEP", "1.5")
    bad = LimitsError("blip")
    reads = Reads(credits_=[credits(), bad, credits(), bad, credits()])
    a = adapter(fake.bin, reads)
    a.tick_s = 0.3
    assert a.run_round("go", make_ctx(tmp_path)).outcome == COMPLETED


def test_final_read_catches_debit_after_a_short_round(fake, tmp_path):
    reads = Reads(credits_=[credits("5"), credits("4")])
    res = frozen_adapter(fake.bin, reads).run_round("go", make_ctx(tmp_path))
    assert res.outcome == CREDIT_GUARD and "dropped" in res.reason


def test_round_not_started_when_precheck_fails(fake, tmp_path):
    reset = time.time() + 3600
    reads = Reads([usage(five=0, five_reset=reset)])
    res = adapter(fake.bin, reads).run_round("go", make_ctx(tmp_path))
    assert res.outcome == LIMIT and abs(res.until - reset) < 1
    assert fake.calls() == []


def test_round_not_started_when_credits_unreadable(fake, tmp_path):
    for reads in (Reads(credits_=["garbage"]), Reads(credits_=[LimitsError("down")])):
        res = adapter(fake.bin, reads).run_round("go", make_ctx(tmp_path))
        assert res.outcome == LIMIT and res.until is not None
    assert fake.calls() == []


def test_round_not_started_when_usage_has_no_gemini_models(fake, tmp_path):
    reads = Reads([usage().replace("Gemini Models", "Other")])
    assert adapter(fake.bin, reads).run_round("go", make_ctx(tmp_path)).outcome == LIMIT
    assert fake.calls() == []


def test_polls_only_every_poll_s(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_EXEC_SLEEP", "1")
    reads = Reads()
    a = AntigravityAdapter(make_cfg(fake.bin), read=reads, poll_clock=lambda: 0.0)
    a.tick_s = 0.05
    assert a.run_round("go", make_ctx(tmp_path)).outcome == COMPLETED
    assert reads.calls == {"/usage": 2, "/credits": 2}  # baseline + closing, never a poll


# --- the order of the guard's checks ---------------------------------------------------------


@pytest.mark.parametrize(
    "blind_usage",
    [
        "garbage",
        usage().replace("Gemini Models", "Other"),
        LimitsError("usage read failed"),
    ],
    ids=["garbage", "no-gemini-models", "read-fails"],
)
def test_credit_drop_is_seen_even_when_usage_is_blind(fake, tmp_path, monkeypatch, blind_usage):
    monkeypatch.setenv("FAKE_AGY_EXEC_SLEEP", "30")
    # One blind read is not yet "two in a row"; the drop is what ends the round.
    reads = Reads([usage(), blind_usage], [credits("10"), credits("9")])
    pids = []
    res = adapter(fake.bin, reads).run_round("go", make_ctx(tmp_path, on_spawn=pids.append))
    assert res.outcome == CREDIT_GUARD and "dropped" in res.reason
    assert reads.calls["/credits"] == 2
    assert wait_dead(pids[0])


def test_clear_refusal_is_honoured_when_credits_are_blind(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_EXEC_SLEEP", "30")
    reset = time.time() + 3600
    reads = Reads(
        [usage(), usage(five=0, five_reset=reset), usage()],
        [credits(), "garbage", credits()],
    )
    res = adapter(fake.bin, reads).run_round("go", make_ctx(tmp_path))
    assert res.outcome == LIMIT and abs(res.until - reset) < 1
    assert reads.calls["/usage"] == 3  # the third is the closing read


# --- one broken row must not hide a clear refusal --------------------------------------------

BROKEN_ROW = "Gemini 4 Argon\tGemini 4 Argon Weekly Limit\tlots\t2026-11-11T17:56:01Z\n"


def test_saturated_row_beside_a_broken_gemini_row_still_refuses_with_its_reset():
    reset = time.time() + 3600
    text = usage(five=0, five_reset=reset) + BROKEN_ROW
    res = AntigravityAdapter(make_cfg(), read=Reads([text])).availability()
    assert not res.ok and abs(res.until - reset) < 1  # the reset, not a 300 s pause
    assert "Five Hour" in res.reason and "unreadable" in res.reason


def test_saturated_model_row_without_gemini_models_rows_refuses_with_its_reset():
    reset = time.time() + 3600
    only_model = f"Gemini 4 Argon\tGemini 4 Argon Weekly Limit\t0%\t{iso(reset)}\n"
    res = AntigravityAdapter(make_cfg(), read=Reads([only_model])).availability()
    assert not res.ok and abs(res.until - reset) < 1


def test_check_usage_saturated_model_row_wins_without_gemini_models_rows():
    only_model = [
        r for r in rows_of(model=0, weekly_reset=NOW + 700) if r["group"] != GEMINI_MODELS
    ]
    a = check_usage(only_model, 90, NOW)
    assert not a.ok and a.until == NOW + 700


def test_round_stops_at_threshold_even_when_another_gemini_row_is_broken(
    fake, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_AGY_EXEC_SLEEP", "30")
    reset = time.time() + 3600
    reads = Reads([usage(), usage(five=10, five_reset=reset) + BROKEN_ROW, usage()])
    pids = []
    res = adapter(fake.bin, reads).run_round("go", make_ctx(tmp_path, on_spawn=pids.append))
    # One blind poll is not the second in a row; the clear refusal must stop the round now.
    assert res.outcome == LIMIT and abs(res.until - reset) < 1
    assert reads.calls["/usage"] == 3  # baseline, the refusing poll, the closing read
    assert wait_dead(pids[0])


def test_a_broken_gemini_row_alone_is_still_unknown_so_unavailable():
    res = AntigravityAdapter(make_cfg(), read=Reads([usage() + BROKEN_ROW])).availability()
    assert not res.ok and "unreadable" in res.reason


def test_default_read_timeout_is_thirty_seconds():
    import inspect

    from baton.adapters import antigravity

    assert antigravity.READ_TIMEOUT_S == 30
    assert inspect.signature(read_text).parameters["timeout_s"].default == 30


# --- the closing read ------------------------------------------------------------------------


def test_closing_read_is_retried_and_still_sees_the_debit(fake, tmp_path):
    reads = Reads(credits_=[credits("10"), LimitsError("blip"), credits("9")])
    res = frozen_adapter(fake.bin, reads).run_round("go", make_ctx(tmp_path))
    assert res.outcome == CREDIT_GUARD and "dropped" in res.reason
    assert reads.calls["/credits"] == 3


def test_closing_read_failing_twice_is_credit_guard(fake, tmp_path):
    reads = Reads(credits_=[credits(), LimitsError("down")])
    res = frozen_adapter(fake.bin, reads).run_round("go", make_ctx(tmp_path))
    assert res.outcome == CREDIT_GUARD and "cannot confirm" in res.reason
    assert reads.calls["/credits"] == 3


def test_closing_read_unparseable_twice_is_credit_guard(fake, tmp_path):
    reads = Reads(credits_=[credits(), "garbage"])
    assert frozen_adapter(fake.bin, reads).run_round("go", make_ctx(tmp_path)).outcome == (
        CREDIT_GUARD
    )


def test_closing_read_failing_once_then_healthy_keeps_the_outcome(fake, tmp_path):
    reads = Reads(credits_=[credits(), LimitsError("blip"), credits()])
    assert frozen_adapter(fake.bin, reads).run_round("go", make_ctx(tmp_path)).outcome == COMPLETED
    assert reads.calls["/credits"] == 3


def test_closing_read_also_guards_a_stopped_round(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_EXEC_SLEEP", "30")
    reads = Reads(credits_=[credits(), LimitsError("x")])
    a = frozen_adapter(fake.bin, reads)
    a.tick_s = 0.05
    # The stop request ends the round as COMPLETED; the closing reads then fail twice.
    res = a.run_round("go", make_ctx(tmp_path, should_kill=lambda: True))
    assert res.outcome == CREDIT_GUARD


def test_closing_read_with_readable_balance_ignores_a_stubborn_blind_usage(fake, tmp_path):
    reads = Reads([usage(), "garbage"], [credits("10")])
    res = frozen_adapter(fake.bin, reads).run_round("go", make_ctx(tmp_path))
    assert res.outcome == COMPLETED
    assert reads.calls["/credits"] == 2  # the balance parsed and held: no retry


def test_closing_read_with_blind_usage_still_sees_a_drop(fake, tmp_path):
    reads = Reads([usage(), "garbage"], [credits("10"), credits("9")])
    assert frozen_adapter(fake.bin, reads).run_round("go", make_ctx(tmp_path)).outcome == (
        CREDIT_GUARD
    )


# --- runner failures and wiring --------------------------------------------------------------


def test_oserror_after_spawn_is_reported_and_still_gets_a_closing_read(tmp_path, monkeypatch):
    from baton.adapters import antigravity

    def broken(argv, cwd, stdin_text, ctx, on_line, tick, tick_s):
        ctx.on_spawn(4242)
        raise OSError("log disk full")

    monkeypatch.setattr(antigravity, "run_streaming", broken)
    spawned = []
    reads = Reads(credits_=[credits("10"), credits("9")])
    res = frozen_adapter("agy", reads).run_round("go", make_ctx(tmp_path, on_spawn=spawned.append))
    assert spawned == [4242]
    assert res.outcome == CREDIT_GUARD  # the closing read saw the debit

    res = frozen_adapter("agy", Reads()).run_round("go", make_ctx(tmp_path))
    assert res.outcome == TRANSIENT
    assert "after the agent started" in res.reason and "log disk full" in res.reason


def test_stdin_is_not_given_to_the_runner(tmp_path, monkeypatch):
    from baton.adapters import antigravity

    seen = {}

    def spy(argv, cwd, stdin_text, ctx, on_line, tick, tick_s):
        seen["stdin"] = stdin_text
        raise OSError("stop here")

    monkeypatch.setattr(antigravity, "run_streaming", spy)
    frozen_adapter("agy", Reads()).run_round("go", make_ctx(tmp_path))
    assert seen["stdin"] is None


def test_default_read_wiring_uses_cfg_bin_for_baseline_and_closing_read(fake, tmp_path):
    a = AntigravityAdapter(make_cfg(fake.bin), poll_clock=lambda: 0.0)
    res = a.run_round("go", make_ctx(tmp_path))
    assert res.outcome == COMPLETED
    assert fake.read_log.read_text().split() == ["/usage", "/credits", "/usage", "/credits"]


def test_default_read_wiring_availability(fake, tmp_path):
    assert AntigravityAdapter(make_cfg(fake.bin)).availability().ok
    (tmp_path / "usage.txt").write_text(usage(five=0))
    assert not AntigravityAdapter(make_cfg(fake.bin)).availability().ok


# --- read_text -------------------------------------------------------------------------------


def test_read_text_runs_the_command_with_text_output_and_closed_stdin(fake):
    text = read_text(fake.bin, "/usage", timeout_s=10)
    assert text == usage()
    assert read_text(fake.bin, "/credits", timeout_s=10) == credits()


def test_read_text_failure_raises(fake, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_READ_MODE", "fail")
    with pytest.raises(LimitsError, match="exited with 1"):
        read_text(fake.bin, "/usage", timeout_s=10)


def test_read_text_missing_binary_raises(tmp_path):
    with pytest.raises(LimitsError, match="cannot start"):
        read_text(str(tmp_path / "nope"), "/usage", timeout_s=10)


def test_read_text_hang_times_out_and_kills_the_process(fake, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_READ_MODE", "hang")
    t0 = time.monotonic()
    with pytest.raises(LimitsError, match="did not finish"):
        read_text(fake.bin, "/usage", timeout_s=0.5)
    assert time.monotonic() - t0 < 5
    assert wait_dead(int(fake.pidfile.read_text()))


def test_read_text_flooding_process_is_cut_off(fake, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_READ_MODE", "flood")
    t0 = time.monotonic()
    with pytest.raises(LimitsError):
        read_text(fake.bin, "/usage", timeout_s=5)
    assert time.monotonic() - t0 < 5
    assert wait_dead(int(fake.pidfile.read_text()))


def test_read_text_sees_a_scrubbed_environment(fake, tmp_path, monkeypatch):
    # The read must not carry the API keys that would let agy bill an API account.
    script = tmp_path / "envagy"
    script.write_text(
        f'#!/bin/sh\n[ -z "$GEMINI_API_KEY" ] || exit 7\nexec "{sys.executable}" "{FAKE}" "$@"\n'
    )
    script.chmod(0o755)
    monkeypatch.setenv("GEMINI_API_KEY", "secret")
    assert read_text(str(script), "/usage", timeout_s=10) == usage()


@pytest.mark.parametrize(
    ("line", "work"),
    [
        ('{"event":"step_update","step_update":{"text_delta":"reading the code"}}', True),
        ('{"event":"result","result":{"status":"ERROR","error":"RESOURCE_EXHAUSTED"}}', False),
        ("429 Too Many Requests", False),
    ],
)
def test_only_the_agent_acting_counts_as_work(line, work):
    adapter = AntigravityAdapter(make_cfg())
    assert adapter.render(line)  # the person sees all of them
    assert adapter.is_work(line) is work

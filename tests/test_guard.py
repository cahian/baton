"""run_guarded: the part of a round every credit-guarded adapter shares."""

from pathlib import Path

from baton.adapters.base import COMPLETED, CREDIT_GUARD, LIMIT, TRANSIENT, RoundContext, RoundResult
from baton.adapters.guard import run_guarded

OK = RoundResult(COMPLETED)


class StubGuard:
    def __init__(self, start=None, close=None):
        self._start, self._close = start, close
        self.closed = 0

    def start(self):
        return self._start

    def close(self):
        self.closed += 1
        return self._close


def make_ctx(tmp_path: Path, **kw) -> RoundContext:
    d = dict(
        workdir=tmp_path,
        log_path=tmp_path / "log",
        round_max_s=30,
        idle_s=30,
        on_output=lambda line: None,
        should_kill=lambda: False,
        on_spawn=lambda pid: None,
    )
    d.update(kw)
    return RoundContext(**d)


def test_refused_start_runs_nothing(tmp_path):
    refusal = RoundResult(LIMIT, reason="no")
    ran = []
    guard = StubGuard(start=refusal)
    res = run_guarded(
        guard, make_ctx(tmp_path), "x", lambda c: ran.append(1) or RoundResult(COMPLETED)
    )
    assert res is refusal and not ran and guard.closed == 0


def test_oserror_before_the_process_existed_is_plain_transient_without_a_closing_read(tmp_path):
    def start(ctx):
        raise OSError("no such file")

    guard = StubGuard()
    res = run_guarded(guard, make_ctx(tmp_path), "agy", start)
    assert res.outcome == TRANSIENT and "cannot start agy" in res.reason
    assert guard.closed == 0


def test_oserror_after_spawn_still_gets_the_closing_read(tmp_path):
    spawned = []

    def start(ctx):
        ctx.on_spawn(4242)
        raise OSError("log disk full")

    late = RoundResult(CREDIT_GUARD, reason="dropped")
    guard = StubGuard(close=late)
    res = run_guarded(guard, make_ctx(tmp_path, on_spawn=spawned.append), "agy", start)
    assert spawned == [4242] and res is late and guard.closed == 1

    guard = StubGuard()
    res = run_guarded(guard, make_ctx(tmp_path), "agy", start)
    assert res.outcome == TRANSIENT and "after the agent started" in res.reason
    assert guard.closed == 1


def test_a_credit_guard_result_skips_the_closing_read(tmp_path):
    stopped = RoundResult(CREDIT_GUARD, reason="dropped")
    guard = StubGuard(close=RoundResult(CREDIT_GUARD, reason="other"))
    res = run_guarded(guard, make_ctx(tmp_path), "x", lambda c: stopped)
    assert res is stopped and guard.closed == 0


def test_the_closing_read_can_override_any_other_outcome(tmp_path):
    late = RoundResult(CREDIT_GUARD, reason="cannot confirm")
    res = run_guarded(
        StubGuard(close=late), make_ctx(tmp_path), "x", lambda c: RoundResult(COMPLETED)
    )
    assert res is late
    res = run_guarded(StubGuard(), make_ctx(tmp_path), "x", lambda c: OK)
    assert res.outcome == COMPLETED

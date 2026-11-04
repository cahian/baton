import contextlib
import errno
import io
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from baton import proc
from baton.adapters.base import COMPLETED, LIMIT, TRANSIENT, RoundContext, RoundResult
from baton.proc import SCRUBBED_ENV, clean_env, run_streaming


def make_ctx(tmp_path: Path, **kw) -> RoundContext:
    defaults = dict(
        workdir=tmp_path,
        log_path=tmp_path / "logs" / "1-x.log",
        round_max_s=30,
        idle_s=30,
        on_output=lambda line: None,
        should_kill=lambda: False,
        on_spawn=lambda pid: None,
    )
    defaults.update(kw)
    return RoundContext(**defaults)


def py(code: str) -> list[str]:
    return [sys.executable, "-u", "-c", code]


def never(_line):
    return None


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


def test_clean_env_removes_every_key():
    base = {k: "secret" for k in SCRUBBED_ENV} | {"PATH": "/bin", "HOME": "/h"}
    out = clean_env(base)
    assert out == {"PATH": "/bin", "HOME": "/h"}
    assert base["OPENAI_API_KEY"] == "secret"  # input not mutated


@pytest.mark.parametrize(
    "key",
    [
        "CODEX_API_KEY",
        "CODEX_ACCESS_TOKEN",
        "CODEX_GUARDIAN_DECISIONS_API_KEY",
        "OPENAI_IDENTITY_TOKEN_FILE",
        "OPENAI_FEDERATION_RULE_ID",
        "OPENAI_WORKLOAD_IDENTITY_CONTEXT",
    ],
)
def test_codex_credential_variables_are_scrubbed(key):
    assert key in SCRUBBED_ENV
    assert key not in clean_env({key: "secret", "PATH": "/bin"})


def test_anthropic_variables_are_no_longer_scrubbed():
    # Claude left the rotation; nothing here launches it, so its variables are the user's own.
    assert "ANTHROPIC_API_KEY" not in SCRUBBED_ENV
    assert "ANTHROPIC_AUTH_TOKEN" not in SCRUBBED_ENV


def test_clean_env_defaults_to_process_environment(monkeypatch):
    for k in SCRUBBED_ENV:
        monkeypatch.setenv(k, "secret")
    out = clean_env()
    assert not set(SCRUBBED_ENV) & set(out)
    assert "PATH" in out


def test_child_does_not_see_scrubbed_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    lines = []
    ex = run_streaming(
        py("import os; print(os.environ.get('OPENAI_API_KEY', 'absent'))"),
        tmp_path,
        None,
        make_ctx(tmp_path),
        lambda line: lines.append(line) and None,
        lambda: None,
    )
    assert lines == ["absent"]
    assert ex.returncode == 0 and ex.stopped is None and not ex.timed_out


def test_stdin_text_reaches_child(tmp_path):
    lines = []
    run_streaming(
        py("import sys; print(sys.stdin.read().upper())"),
        tmp_path,
        "hello",
        make_ctx(tmp_path),
        lambda line: lines.append(line) and None,
        lambda: None,
    )
    assert lines == ["HELLO"]


def test_on_line_stop_kills_group_including_grandchild(tmp_path):
    code = (
        "import subprocess, sys, time\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print('GC', g.pid, flush=True)\n"
        "print('GUARD', flush=True)\n"
        "time.sleep(60)\n"
    )
    seen = {}
    stop = RoundResult(LIMIT, reason="guard")

    def on_line(line):
        if line.startswith("GC "):
            seen["gc"] = int(line.split()[1])
        return stop if line == "GUARD" else None

    t0 = time.monotonic()
    ex = run_streaming(py(code), tmp_path, None, make_ctx(tmp_path), on_line, lambda: None)
    assert ex.stopped is stop
    assert time.monotonic() - t0 < 8
    assert wait_dead(seen["gc"])


def test_tick_fires_while_child_is_silent(tmp_path):
    ticks = []
    stop = RoundResult(COMPLETED, reason="enough")

    def tick():
        ticks.append(time.monotonic())
        return stop if len(ticks) >= 3 else None

    t0 = time.monotonic()
    ex = run_streaming(
        py("import time; time.sleep(30)"),
        tmp_path,
        None,
        make_ctx(tmp_path),
        never,
        tick,
        tick_s=0.1,
    )
    assert ex.stopped is stop
    assert len(ticks) == 3
    assert time.monotonic() - t0 < 5


def test_round_max_timeout(tmp_path):
    ex = run_streaming(
        py("import time\nfor _ in range(200):\n    print('x', flush=True); time.sleep(0.05)"),
        tmp_path,
        None,
        make_ctx(tmp_path, round_max_s=1, idle_s=30),
        never,
        lambda: None,
        tick_s=0.1,
    )
    assert ex.timed_out and ex.stopped is None


def test_idle_timeout(tmp_path):
    t0 = time.monotonic()
    ex = run_streaming(
        py("import time; print('one', flush=True); time.sleep(30)"),
        tmp_path,
        None,
        make_ctx(tmp_path, round_max_s=30, idle_s=1),
        never,
        lambda: None,
        tick_s=0.1,
    )
    assert ex.timed_out and ex.stopped is None
    assert time.monotonic() - t0 < 8


def test_sigterm_ignored_escalates_to_sigkill(tmp_path, monkeypatch):
    monkeypatch.setattr(proc, "KILL_GRACE_S", 0.2)
    code = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)\n"
    )
    stop = RoundResult(LIMIT)
    t0 = time.monotonic()
    ex = run_streaming(
        py(code),
        tmp_path,
        None,
        make_ctx(tmp_path),
        lambda line: stop if line == "ready" else None,
        lambda: None,
    )
    assert ex.stopped is stop
    assert ex.returncode == -9
    assert time.monotonic() - t0 < 5


def test_invalid_utf8_and_1mb_line_survive(tmp_path):
    code = (
        "import sys\n"
        "sys.stdout.buffer.write(b'\\xff\\xfe\\x80' * 10 + b'x' * 1_000_000 + b'\\n')\n"
        "sys.stdout.buffer.write(b'GUARD\\n')\n"
        "sys.stdout.buffer.flush()\n"
        "import time; time.sleep(60)\n"
    )
    lengths = []
    stop = RoundResult(LIMIT, reason="guard")

    def on_line(line):
        lengths.append(len(line))
        return stop if line == "GUARD" else None

    ex = run_streaming(py(code), tmp_path, None, make_ctx(tmp_path), on_line, lambda: None)
    assert ex.stopped is stop
    assert max(lengths) <= 65536
    assert sum(lengths) >= 1_000_000


def test_should_kill_stops_round(tmp_path):
    flag = {"v": False}
    n = {"ticks": 0}

    def tick():
        n["ticks"] += 1
        flag["v"] = n["ticks"] >= 2
        return None

    ex = run_streaming(
        py("import time; time.sleep(60)"),
        tmp_path,
        None,
        make_ctx(tmp_path, should_kill=lambda: flag["v"]),
        never,
        tick,
        tick_s=0.1,
    )
    assert ex.stopped is not None
    assert ex.stopped.outcome == COMPLETED
    assert ex.stopped.reason == "stop requested"


def test_writes_raw_log(tmp_path):
    ctx = make_ctx(tmp_path)
    got = []
    ctx.on_output = got.append
    ex = run_streaming(
        py("import sys; sys.stdout.buffer.write(b'a\\xff\\nb\\n'); print('err', file=sys.stderr)"),
        tmp_path,
        None,
        ctx,
        never,
        lambda: None,
    )
    data = ctx.log_path.read_bytes()
    assert b"a\xff\n" in data and b"b\n" in data and b"err\n" in data
    assert len(got) == 3
    assert "err" in ex.tail


def test_tail_is_last_4000_chars(tmp_path):
    ex = run_streaming(
        py("for i in range(2000): print('line', i)"),
        tmp_path,
        None,
        make_ctx(tmp_path),
        never,
        lambda: None,
    )
    assert len(ex.tail) == 4000
    assert ex.tail.rstrip().endswith("line 1999")


def test_on_spawn_reports_pgid(tmp_path):
    spawned = []
    ctx = make_ctx(tmp_path, on_spawn=spawned.append)
    seen = {}
    stop = RoundResult(COMPLETED, reason="seen")

    def on_line(line):
        seen["pgid"] = os.getpgid(int(line))
        return stop

    # The child stays alive until stopped, so getpgid cannot hit a zombie.
    run_streaming(
        py("import os, time; print(os.getpid(), flush=True); time.sleep(60)"),
        tmp_path,
        None,
        ctx,
        on_line,
        lambda: None,
    )
    assert len(spawned) == 1
    assert seen["pgid"] == spawned[0]  # child leads its own session/group
    assert spawned[0] != os.getpgrp()


def test_kill_group_on_dead_group_is_a_noop():
    done = subprocess.Popen(py("pass"), start_new_session=True)
    done.wait()  # reaped: this group id no longer exists
    t0 = time.monotonic()
    proc.kill_group(done.pid, grace_s=0.1)
    assert time.monotonic() - t0 < 2


def test_nonzero_exit_reported(tmp_path):
    ex = run_streaming(
        py("import sys; sys.exit(3)"), tmp_path, None, make_ctx(tmp_path), never, lambda: None
    )
    assert ex.returncode == 3 and ex.stopped is None


def test_missing_binary_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        run_streaming(
            ["/nonexistent/baton-bin"], tmp_path, None, make_ctx(tmp_path), never, lambda: None
        )


def test_unopenable_log_raises_before_spawning(tmp_path):
    marker = tmp_path / "ran"
    bad_log = tmp_path / "a-directory"
    bad_log.mkdir()
    spawned = []
    ctx = make_ctx(tmp_path, log_path=bad_log, on_spawn=spawned.append)
    with pytest.raises(OSError):
        run_streaming(
            py(f"import pathlib, time; pathlib.Path({str(marker)!r}).touch(); time.sleep(5)"),
            tmp_path,
            None,
            ctx,
            never,
            lambda: None,
        )
    time.sleep(0.5)
    assert spawned == []
    assert not marker.exists()  # the child was never started


def test_grandchild_holding_stdout_does_not_time_out_a_finished_leader(tmp_path):
    code = (
        "import subprocess, sys\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print('GC', g.pid, flush=True)\n"
    )
    seen = {}

    def on_line(line):
        seen["gc"] = int(line.split()[1])

    t0 = time.monotonic()
    ex = run_streaming(
        py(code),
        tmp_path,
        None,
        make_ctx(tmp_path, round_max_s=30, idle_s=30),
        on_line,
        lambda: None,
        tick_s=0.1,
    )
    assert ex.returncode == 0 and not ex.timed_out and ex.stopped is None
    assert time.monotonic() - t0 < 8
    assert wait_dead(seen["gc"])  # the sweep killed it


def test_grandchild_with_redirected_stdout_is_killed_when_leader_exits(tmp_path):
    code = (
        "import subprocess, sys\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],\n"
        "                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "print('GC', g.pid, flush=True)\n"
    )
    seen = {}

    def on_line(line):
        seen["gc"] = int(line.split()[1])

    ex = run_streaming(
        py(code), tmp_path, None, make_ctx(tmp_path), on_line, lambda: None, tick_s=0.1
    )
    assert ex.returncode == 0 and not ex.timed_out
    assert wait_dead(seen["gc"])


def test_setsid_survivor_holding_stdout_does_not_hang_the_runner(tmp_path):
    code = (
        "import subprocess, sys\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],\n"
        "                     start_new_session=True)\n"
        "print('GC', g.pid, flush=True)\n"
    )
    seen = {}

    def on_line(line):
        seen["gc"] = int(line.split()[1])

    t0 = time.monotonic()
    try:
        ex = run_streaming(
            py(code), tmp_path, None, make_ctx(tmp_path), on_line, lambda: None, tick_s=0.1
        )
        elapsed = time.monotonic() - t0
    finally:
        if "gc" in seen:
            with contextlib.suppress(ProcessLookupError):
                os.kill(seen["gc"], signal.SIGKILL)
    assert ex.returncode == 0 and not ex.timed_out
    assert elapsed < 5


def test_setsid_infinite_producer_does_not_wedge_the_runner(tmp_path):
    flood = "import os; b = b'y\\n' * 4096\nwhile True:\n    os.write(1, b)"
    code = (
        "import subprocess, sys\n"
        f"g = subprocess.Popen([sys.executable, '-c', {flood!r}], start_new_session=True)\n"
        "print('GC', g.pid, flush=True)\n"
    )
    seen = {}

    def on_line(line):
        if line.startswith("GC "):
            seen["gc"] = int(line.split()[1])

    t0 = time.monotonic()
    try:
        ex = run_streaming(
            py(code), tmp_path, None, make_ctx(tmp_path), on_line, lambda: None, tick_s=0.1
        )
        elapsed = time.monotonic() - t0
    finally:
        if "gc" in seen:
            with contextlib.suppress(ProcessLookupError):
                os.kill(seen["gc"], signal.SIGKILL)
    assert ex.returncode == 0 and not ex.timed_out
    assert elapsed < 8


def test_every_line_written_before_leader_exit_reaches_on_line(tmp_path):
    n = 4000
    got = []

    def slow_on_line(line):
        got.append(line)
        time.sleep(0.0002)  # far slower than the drain window allows for all lines
        return None

    ex = run_streaming(
        py(f"for i in range({n}): print('L', i)\nprint('GUARD')"),
        tmp_path,
        None,
        make_ctx(tmp_path),
        slow_on_line,
        lambda: None,
        tick_s=0.1,
    )
    assert ex.returncode == 0
    assert len(got) == n + 1
    assert got[-1] == "GUARD"


def test_round_context_and_result_defaults(tmp_path):
    assert RoundResult(COMPLETED).resume_failed is False
    ctx = make_ctx(tmp_path)
    assert ctx.session_id is None
    ctx.on_session("abc")  # default callback accepts a session id and does nothing


def test_sigterm_handler_farewell_is_drained_not_blocked(tmp_path):
    code = (
        "import signal, sys, time\n"
        "def bye(*_):\n"
        "    time.sleep(0.5)\n"
        "    sys.stdout.write(('x' * 99 + '\\n') * 12000)\n"
        "    sys.stdout.write('BYE\\n')\n"
        "    sys.stdout.flush()\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, bye)\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)\n"
    )
    stop = RoundResult(LIMIT, reason="guard")
    t0 = time.monotonic()
    ex = run_streaming(
        py(code),
        tmp_path,
        None,
        make_ctx(tmp_path),
        lambda line: stop if line == "ready" else None,
        lambda: None,
    )
    assert ex.stopped is stop
    assert time.monotonic() - t0 < 4
    assert ex.returncode == 0  # exited by itself, was not SIGKILLed
    assert ex.tail.rstrip().endswith("BYE")


def test_slow_on_line_with_setsid_infinite_producer_still_returns(tmp_path):
    flood = "import os; b = b'y\\n' * 4096\nwhile True:\n    os.write(1, b)"
    code = (
        "import subprocess, sys\n"
        f"g = subprocess.Popen([sys.executable, '-c', {flood!r}], start_new_session=True)\n"
        "print('GC', g.pid, flush=True)\n"
    )
    seen = {}
    delivered = []

    def on_line(line):
        if line.startswith("GC "):
            seen["gc"] = int(line.split()[1])
        delivered.append(line)
        time.sleep(0.0002)

    t0 = time.monotonic()
    try:
        ex = run_streaming(
            py(code), tmp_path, None, make_ctx(tmp_path), on_line, lambda: None, tick_s=0.1
        )
        elapsed = time.monotonic() - t0
    finally:
        if "gc" in seen:
            with contextlib.suppress(ProcessLookupError):
                os.kill(seen["gc"], signal.SIGKILL)
    assert ex.returncode == 0 and not ex.timed_out
    assert elapsed < 8
    assert delivered[0].startswith("GC ")


def _burst_then_guard(n: int) -> list[str]:
    return py(f"for i in range({n}): print('L', i)\nprint('GUARD')")


def test_guard_stop_during_drain_wins_over_a_tick_stop(tmp_path):
    guard = RoundResult(LIMIT, reason="guard")
    tick_stop = RoundResult(TRANSIENT, reason="tick")
    start = time.monotonic()

    def on_line(line):
        time.sleep(0.0003)
        return guard if line == "GUARD" else None

    def tick():
        # fires only once the leader is long gone and lines are still being worked off
        return tick_stop if time.monotonic() - start > 0.6 else None

    ex = run_streaming(
        _burst_then_guard(4000), tmp_path, None, make_ctx(tmp_path), on_line, tick, tick_s=0.1
    )
    assert ex.stopped is guard


def test_tick_stop_during_drain_is_kept_when_no_guard_follows(tmp_path):
    tick_stop = RoundResult(TRANSIENT, reason="tick")
    start = time.monotonic()

    def on_line(line):
        time.sleep(0.0003)

    def tick():
        return tick_stop if time.monotonic() - start > 0.6 else None

    ex = run_streaming(
        _burst_then_guard(4000), tmp_path, None, make_ctx(tmp_path), on_line, tick, tick_s=0.1
    )
    assert ex.stopped is tick_stop


class BrokenLog:
    """A log on a full disk: every operation fails once `failing()` says so."""

    def __init__(self, failing):
        self.failing = failing

    def _check(self):
        if self.failing():
            raise OSError(errno.EFBIG, "File too large")

    def write(self, data):
        self._check()
        return len(data)

    def flush(self):
        self._check()

    def close(self):
        self._check()


def broken_log_path(path: Path, failing) -> Path:
    class BrokenLogPath(type(path)):
        def open(self, *args, **kwargs):
            return BrokenLog(failing)

    return BrokenLogPath(path)


def test_log_failure_while_stopping_still_kills_a_sigterm_ignoring_group(tmp_path, monkeypatch):
    monkeypatch.setattr(proc, "KILL_GRACE_S", 0.2)
    code = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('ready', flush=True)\n"
        "while True:\n"
        "    print('still here', flush=True); time.sleep(0.01)\n"
    )
    stop = RoundResult(LIMIT, reason="guard")
    broken = threading.Event()
    spawned = []

    def on_line(line):
        if line == "ready":
            broken.set()  # the disk fills up as the stop is decided
            return stop
        return None

    ctx = make_ctx(
        tmp_path,
        log_path=broken_log_path(tmp_path / "x.log", broken.is_set),
        on_spawn=spawned.append,
    )
    try:
        ex = run_streaming(py(code), tmp_path, None, ctx, on_line, lambda: None)
    finally:
        if spawned:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(spawned[0], signal.SIGKILL)
    assert ex.stopped is stop
    assert ex.returncode == -9


def test_log_failure_does_not_stop_on_line_delivery(tmp_path):
    guard = RoundResult(LIMIT, reason="guard")
    got = []

    def on_line(line):
        got.append(line)
        return guard if line == "GUARD" else None

    ctx = make_ctx(tmp_path, log_path=broken_log_path(tmp_path / "x.log", lambda: True))
    ex = run_streaming(_burst_then_guard(50), tmp_path, None, ctx, on_line, lambda: None)
    assert ex.stopped is guard
    assert len(got) == 51 and got[-1] == "GUARD"
    assert ex.tail.rstrip().endswith("GUARD")


def test_hard_cap_never_discards_queued_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(proc, "HARD_DRAIN_S", 0.05)
    n = 1000
    guard = RoundResult(LIMIT, reason="guard")
    got = []

    def on_line(line):
        got.append(line)
        time.sleep(0.001)  # about 1.3 s of work, far past the cap
        return guard if line == "GUARD" else None

    ex = run_streaming(
        _burst_then_guard(n), tmp_path, None, make_ctx(tmp_path), on_line, lambda: None, tick_s=0.1
    )
    assert ex.stopped is guard
    assert got == [f"L {i}" for i in range(n)] + ["GUARD"]


def test_timeout_never_discards_queued_lines(tmp_path):
    n = 200
    guard = RoundResult(LIMIT, reason="guard")
    ctx = make_ctx(tmp_path)
    got = []

    def on_line(line):
        if not got:
            ctx.round_max_s = 0  # the limit passes with the whole burst already queued
            time.sleep(0.3)
        got.append(line)
        return guard if line == "GUARD" else None

    code = (
        f"for i in range({n}): print('L', i)\n"
        "print('GUARD', flush=True)\n"
        "import time; time.sleep(60)\n"
    )
    ex = run_streaming(py(code), tmp_path, None, ctx, on_line, lambda: None, tick_s=0.01)
    assert ex.timed_out
    assert ex.stopped is guard
    assert got == [f"L {i}" for i in range(n)] + ["GUARD"]


def test_reader_told_to_stop_queues_the_chunk_it_already_read(tmp_path):
    lines = queue.Queue(maxsize=1)
    lines.put(b"first\n")
    stop, abandon = threading.Event(), threading.Event()
    stream = io.BytesIO(b"second\nthird\n")
    reader = threading.Thread(target=proc._reader, args=(stream, lines, stop, abandon), daemon=True)
    reader.start()
    end = time.monotonic() + 5
    while stream.tell() == 0 and time.monotonic() < end:  # now it holds "second", waits for room
        time.sleep(0.001)
    stop.set()
    time.sleep(proc.PUT_TIMEOUT_S * 4)  # room appears only after the stop was seen
    got = [lines.get(timeout=2), lines.get(timeout=2), lines.get(timeout=2)]
    reader.join(timeout=2)
    assert got == [b"first\n", b"second\n", proc._EOF]  # "third" was never read
    assert not reader.is_alive()


def test_raising_on_line_still_stops_the_group(tmp_path):
    spawned = []

    def on_line(line):
        raise RuntimeError("boom")

    code = "print('ready', flush=True)\nimport time; time.sleep(60)\n"
    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="boom"):
        run_streaming(
            py(code),
            tmp_path,
            None,
            make_ctx(tmp_path, on_spawn=spawned.append),
            on_line,
            lambda: None,
        )
    assert time.monotonic() - t0 < 5
    assert wait_dead(spawned[0])

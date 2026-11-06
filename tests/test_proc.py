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


@pytest.mark.parametrize("key", ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"])
def test_anthropic_variables_are_scrubbed(key):
    assert key in SCRUBBED_ENV
    assert key not in clean_env({key: "secret", "PATH": "/bin"})


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


def test_process_start_is_stable_and_empty_for_a_dead_pid():
    from baton.proc import process_start

    first = process_start(os.getpid())
    assert first and first == process_start(os.getpid())
    child = subprocess.Popen(["true"])
    child.wait()
    assert process_start(child.pid) == ""


def test_process_start_does_not_depend_on_the_timezone(monkeypatch):
    from baton.proc import process_start

    monkeypatch.setenv("TZ", "Asia/Tokyo")
    tokyo = process_start(os.getpid())
    monkeypatch.setenv("TZ", "America/Sao_Paulo")
    assert tokyo and process_start(os.getpid()) == tokyo


def test_check_group_never_accepts_unsafe_ids():
    from baton.proc import FOREIGN, check_group

    for bad in (0, 1, -5, True, None, "12", os.getpgrp()):
        assert check_group(bad, "x") == FOREIGN


def test_check_group_classifies_a_group():
    from baton.proc import DEAD, FOREIGN, OURS, UNKNOWN, check_group, process_start

    child = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        started = process_start(child.pid)
        assert check_group(child.pid, started) == OURS
        assert check_group(child.pid, "Mon Jan  1 00:00:00 2001") == FOREIGN
        # Alive but nothing recorded to compare with: never signalled, never forgotten.
        assert check_group(child.pid, None) == UNKNOWN
        assert check_group(child.pid, "") == UNKNOWN
        monkey = pytest.MonkeyPatch()
        monkey.setattr(proc, "process_start", lambda pid: None)  # ps cannot answer
        try:
            assert check_group(child.pid, started) == UNKNOWN
        finally:
            monkey.undo()
    finally:
        child.kill()
        child.wait()
    assert check_group(child.pid, started) == DEAD


def test_process_start_treats_a_ps_error_as_unknown(monkeypatch):
    from baton.proc import UNKNOWN, check_group, process_start

    def fake_run(rc, err):
        return lambda *a, **k: subprocess.CompletedProcess(a, rc, stdout="", stderr=err)

    monkeypatch.setattr(subprocess, "run", fake_run(1, "ps: some failure\n"))
    assert process_start(123) is None
    monkeypatch.setattr(subprocess, "run", fake_run(1, ""))
    assert process_start(123) == ""  # a silent 1 is "no such process"

    # A live group whose ps call errored must be unknown, never ours.
    child = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        monkeypatch.setattr(subprocess, "run", fake_run(1, "ps: some failure\n"))
        assert check_group(child.pid, "Mon Jan  1 00:00:00 2001") == UNKNOWN
    finally:
        child.kill()
        child.wait()


def test_extra_env_reaches_only_that_child(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-the-shell")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://evil.example")
    code = (
        "import os; k = os.environ.get('ANTHROPIC_API_KEY');"
        "print(k[::-1] if k else k, os.environ.get('ANTHROPIC_BASE_URL'))"  # reversed: not redacted
    )
    mine, other = [], []
    run_streaming(
        py(code),
        tmp_path,
        None,
        make_ctx(tmp_path),
        lambda line: mine.append(line) and None,
        lambda: None,
        extra_env={"ANTHROPIC_API_KEY": "its-own-key-12345"},
    )
    run_streaming(
        py(code),
        tmp_path,
        None,
        make_ctx(tmp_path),
        lambda line: other.append(line) and None,
        lambda: None,
    )
    assert mine == ["54321-yek-nwo-sti None"]  # applied after scrubbing, and only what was asked
    assert other == ["None None"]
    assert "its-own-key-12345" not in os.environ.values()


def test_extra_env_values_are_redacted_from_log_and_lines(tmp_path):
    secret = "fake-ant-api03-SECRETSECRET"
    ctx = make_ctx(tmp_path)
    shown, parsed = [], []
    ctx.on_output = shown.append
    ex = run_streaming(
        py("import os; print('key=' + os.environ['K']); print('again ' + os.environ['K'])"),
        tmp_path,
        None,
        ctx,
        lambda line: parsed.append(line) and None,
        lambda: None,
        extra_env={"K": secret},
    )
    assert secret not in ctx.log_path.read_text()
    assert secret not in "".join(shown + parsed) and secret not in ex.tail
    assert "key=[redacted]" in shown and "again [redacted]" in shown


def test_plain_env_reaches_the_child_and_is_not_redacted(tmp_path):
    ctx = make_ctx(tmp_path)
    shown = []
    ctx.on_output = shown.append
    run_streaming(
        py("import os; print('model=' + os.environ['M'])"),
        tmp_path,
        None,
        ctx,
        never,
        lambda: None,
        plain_env={"M": "claude-sonnet-5-5"},
    )
    assert "model=claude-sonnet-5-5" in shown


def test_a_secret_split_across_two_chunks_is_still_redacted(tmp_path):
    secret = "fake-ant-api03-SPLITSPLITSPLIT"
    # one line, longer than the 64 KB chunk, with the secret straddling the cut
    code = (
        "import os, sys\n"
        "k = os.environ['K']\n"
        "sys.stdout.write('A' * (65536 - 10) + k + 'B' * 100 + '\\n')\n"
        "sys.stdout.write('after\\n')\n"
    )
    ctx = make_ctx(tmp_path)
    shown, parsed = [], []
    ctx.on_output = shown.append
    ex = run_streaming(
        py(code),
        tmp_path,
        None,
        ctx,
        lambda line: parsed.append(line) and None,
        lambda: None,
        extra_env={"K": secret},
    )
    log = ctx.log_path.read_text()
    assert secret not in log and "SPLITSPLIT" not in log
    assert "[redacted]" in log
    assert "SPLITSPLIT" not in "".join(shown + parsed) + ex.tail
    assert log.count("A") == 65536 - 10 and log.count("B") == 100  # nothing else was lost
    assert "after" in parsed


def test_a_cut_line_without_a_secret_arrives_whole_and_in_order(tmp_path):
    ctx = make_ctx(tmp_path)
    parsed = []
    run_streaming(
        py("import sys; sys.stdout.write('x' * 200000 + '\\nend\\n')"),
        tmp_path,
        None,
        ctx,
        lambda line: parsed.append(line) and None,
        lambda: None,
        extra_env={"K": "fake-ant-api03-NOTPRINTED"},
    )
    assert "".join(parsed[:-1]) == "x" * 200000 and parsed[-1] == "end"
    assert ctx.log_path.read_text() == "x" * 200000 + "\nend\n"


def test_the_held_back_end_of_an_unfinished_last_line_still_reaches_the_log(tmp_path):
    ctx = make_ctx(tmp_path)
    run_streaming(
        py("import sys; sys.stdout.write('y' * 65536)"),  # exactly one chunk, no newline, then EOF
        tmp_path,
        None,
        ctx,
        never,
        lambda: None,
        extra_env={"K": "fake-ant-api03-NOTPRINTED"},
    )
    assert ctx.log_path.read_text() == "y" * 65536


# --- a secret at the edge of a stopped round's log ---------------------------------------------

SECRET = "fake-ant-api03-EDGEEDGEEDGEEDGE"
KEEP = len(SECRET) - 1  # what the redactor holds back from the end of a cut line


def stop_at_first_line(line):
    return RoundResult(LIMIT, reason="stop") if line == "go" else None


def edge_run(tmp_path, monkeypatch, code, cap=None, grace=0.2):
    if cap is not None:
        monkeypatch.setattr(proc, "STOP_LOG_BYTES", cap)
    monkeypatch.setattr(proc, "KILL_GRACE_S", grace)
    ctx = make_ctx(tmp_path)
    run_streaming(
        py(code),
        tmp_path,
        None,
        ctx,
        stop_at_first_line,
        lambda: None,
        extra_env={"K": SECRET},
    )
    return ctx.log_path.read_text()


def test_a_key_split_at_the_log_cap_of_a_stopped_round_is_not_written(tmp_path, monkeypatch):
    """After a stop the log takes at most STOP_LOG_BYTES more. The last chunk it took ended with
    the key minus one character, held back; the cap then cut the rest off, and the end of the
    round used to flush that held tail into the log. The stop comes at the first line, so the
    child must outlive it to write the rest: it ignores SIGTERM and ends by itself, and the grace
    period never runs out. Otherwise a stop that wins the race leaves nothing to check."""
    code = (
        "import os, signal, sys\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "k = os.environ['K']\n"
        "sys.stdout.write('go\\n'); sys.stdout.flush()\n"
        f"sys.stdout.write('A' * (65536 - {KEEP}) + k[:{KEEP}]); sys.stdout.flush()\n"
        "sys.stdout.write('B' * 70000 + '\\n'); sys.stdout.flush()\n"
    )
    log = edge_run(tmp_path, monkeypatch, code, cap=65536, grace=30)
    assert SECRET[:8] not in log and SECRET[2:12] not in log
    assert log == "go\n" + "A" * (65536 - KEEP)  # the part before it was logged, and nothing after


def test_the_held_tail_is_dropped_when_the_log_is_full(tmp_path, monkeypatch):
    """The `B` chunk is what shows the log is full, so the child must outlive the stop until it
    has written it: it ignores SIGTERM, ends by itself, and the grace period never runs out."""
    code = (
        "import signal, sys\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "sys.stdout.write('go\\n'); sys.stdout.flush()\n"
        f"sys.stdout.write('A' * (65536 - {KEEP}) + 'Z' * {KEEP}); sys.stdout.flush()\n"
        "sys.stdout.write('B' * 70000 + '\\n'); sys.stdout.flush()\n"
    )
    log = edge_run(tmp_path, monkeypatch, code, cap=65536, grace=30)
    assert "Z" not in log  # nothing more is written once the cap is reached


def test_a_held_tail_that_starts_the_secret_is_not_flushed_at_the_end(tmp_path, monkeypatch):
    """No cap involved: the process just ends after a chunk whose end is the key's beginning."""
    code = (
        "import os, sys\n"
        "k = os.environ['K']\n"
        f"sys.stdout.write('A' * (65536 - 3 - {KEEP}) + 'xyz' + k[:{KEEP}])\n"
    )
    log = edge_run(tmp_path, monkeypatch, code)
    assert SECRET[:8] not in log
    assert log == "A" * (65536 - 3 - KEEP) + "xyz"  # the harmless part is kept


def test_a_held_tail_that_cannot_start_the_secret_is_still_flushed_at_the_end(
    tmp_path, monkeypatch
):
    code = f"import sys; sys.stdout.write('A' * (65536 - {KEEP}) + 'Z' * {KEEP})"
    assert edge_run(tmp_path, monkeypatch, code) == "A" * (65536 - KEEP) + "Z" * KEEP


# --- the killer thread cannot start -----------------------------------------------------------


def test_a_killer_thread_that_cannot_start_still_kills_the_group(tmp_path, monkeypatch):
    """Thread exhaustion at the worst moment: without the synchronous fallback the sweep is
    skipped and `killer.join()` raises, so the agent goes on running with nobody watching."""
    real_start = threading.Thread.start

    def start(thread):
        if thread._target is proc._kill_group:
            raise RuntimeError("can't start new thread")
        real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", start)
    monkeypatch.setattr(proc, "KILL_GRACE_S", 0.2)
    ctx = make_ctx(tmp_path)
    groups = []
    ctx.on_spawn = groups.append
    started = time.monotonic()
    ex = run_streaming(
        py("import sys, time\nprint('go', flush=True)\ntime.sleep(25)\n"),
        tmp_path,
        None,
        ctx,
        stop_at_first_line,
        lambda: None,
    )
    assert time.monotonic() - started < 15  # killed, rather than waited for
    assert ex.stopped is not None and ex.stopped.reason == "stop"
    with pytest.raises(ProcessLookupError):
        os.killpg(groups[0], 0)


def test_a_failed_killer_start_does_not_hide_the_round_result(tmp_path, monkeypatch):
    """The fallback runs even when the round already raised, and that error is the one seen."""
    real_start = threading.Thread.start

    def start(thread):
        if thread._target is proc._kill_group:
            raise MemoryError
        real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", start)
    monkeypatch.setattr(proc, "KILL_GRACE_S", 0.2)
    ctx = make_ctx(tmp_path)
    groups = []

    def boom(pgid):
        groups.append(pgid)
        raise ValueError("on_spawn failed")

    ctx.on_spawn = boom
    started = time.monotonic()
    with pytest.raises(ValueError, match="on_spawn failed"):
        run_streaming(
            py("import time\ntime.sleep(25)\n"), tmp_path, None, ctx, lambda _: None, lambda: None
        )
    assert time.monotonic() - started < 15
    with pytest.raises(ProcessLookupError):
        os.killpg(groups[0], 0)


# --- the last piece of output, when it has no newline -----------------------------------------


def final_piece(tmp_path, monkeypatch, code):
    """Run a child whose output ends without a newline; (log, lines seen, shown, tail)."""
    monkeypatch.setattr(proc, "KILL_GRACE_S", 0.2)
    ctx = make_ctx(tmp_path)
    shown, parsed = [], []
    ctx.on_output = shown.append
    ex = run_streaming(
        py(code),
        tmp_path,
        None,
        ctx,
        lambda line: parsed.append(line) and None,
        lambda: None,
        extra_env={"K": SECRET},
    )
    return ctx.log_path.read_text(), parsed, shown, ex.tail


def test_a_short_last_piece_ending_in_the_key_minus_one_character_is_not_written(
    tmp_path, monkeypatch
):
    """`hello ` plus the key without its last character, then EOF: the chunk is short and not
    cut, so it was written unchecked, to the log and to the tail."""
    log, parsed, shown, tail = final_piece(
        tmp_path, monkeypatch, "import os, sys; sys.stdout.write('hello ' + os.environ['K'][:-1])"
    )
    for place in (log, "".join(parsed), "".join(shown), tail):
        assert SECRET[:8] not in place and SECRET[:-1] not in place
    assert log == "hello "
    assert parsed == ["hello "] and tail == "hello \n"


@pytest.mark.parametrize("size", [1, 5, KEEP])
def test_any_ending_that_starts_the_key_is_trimmed_from_a_last_piece(tmp_path, monkeypatch, size):
    log, parsed, _, tail = final_piece(
        tmp_path,
        monkeypatch,
        f"import os, sys; sys.stdout.write('line one\\nlast ' + os.environ['K'][:{size}])",
    )
    assert log == "line one\nlast "
    assert parsed == ["line one", "last "]
    assert SECRET[:size] not in tail


def test_a_last_piece_with_the_whole_key_is_redacted_not_trimmed(tmp_path, monkeypatch):
    log, parsed, _, tail = final_piece(
        tmp_path, monkeypatch, "import os, sys; sys.stdout.write('hello ' + os.environ['K'])"
    )
    assert log == "hello [redacted]" and parsed == ["hello [redacted]"]
    assert SECRET not in tail


def test_a_harmless_last_piece_without_a_newline_is_kept_whole(tmp_path, monkeypatch):
    log, parsed, _, tail = final_piece(
        tmp_path, monkeypatch, "import sys; sys.stdout.write('first\\nno newline here')"
    )
    assert log == "first\nno newline here"
    assert parsed == ["first", "no newline here"] and tail.endswith("no newline here\n")


def test_a_last_piece_after_a_cut_line_is_checked_together_with_what_was_held(
    tmp_path, monkeypatch
):
    code = (
        "import os, sys\n"
        "k = os.environ['K']\n"
        "sys.stdout.write('A' * (65536 - 4) + k[:4])\n"  # a cut chunk ending in the key's start
        "sys.stdout.write(k[4:-1])\n"  # and the rest of the key, minus its last character
    )
    log, parsed, _, tail = final_piece(tmp_path, monkeypatch, code)
    assert SECRET[:8] not in log and SECRET[:8] not in tail
    assert log == "A" * (65536 - 4)


def test_scrub_drops_every_variable_that_matches_before_the_adapter_adds_its_own(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XX_ONE", "1")
    monkeypatch.setenv("XX_TWO", "2")
    monkeypatch.setenv("YY_KEEP", "kept")
    ctx = make_ctx(tmp_path)
    shown = []
    ctx.on_output = shown.append
    run_streaming(
        py(
            "import os; print(sorted(k for k in os.environ if k[:3] in ('XX_', 'YY_')));"
            " print(os.environ.get('XX_TWO'))"
        ),
        tmp_path,
        None,
        ctx,
        never,
        lambda: None,
        scrub=("XX_*",),
        extra_env={"XX_TWO": "its-own-value-123"},  # added back after the scrub
    )
    assert shown[0] == "['XX_TWO', 'YY_KEEP']"


# masking what an agent printed


@pytest.mark.parametrize(
    "text, shown",
    [
        ("Authorization: Bearer abc123def456ghi", "Authorization: Bearer [masked]"),
        ("use Bearer abc123def456ghi.", "use Bearer [masked]."),
        ("password=hunter2hunter", "password=[masked]"),
        ("API_KEY: abcdef0123456789", "API_KEY: [masked]"),
        ('{"token": "abcdef0123456789"}', '{"token": "[masked]"}'),
        ('{\\"token\\":\\"abcdef0123456789\\"}', '{\\"token\\":\\"[masked]\\"}'),
        ("export GITHUB_TOKEN=ghp_" + "a1" * 12, "export GITHUB_TOKEN=[masked]"),
        ("SECRET_KEY=abc123def456", "SECRET_KEY=[masked]"),
        ("AWS_SECRET_ACCESS_KEY=abc123def456", "AWS_SECRET_ACCESS_KEY=[masked]"),
        ("DB_PASSWORD=abc123def456", "DB_PASSWORD=[masked]"),
        ("key sk-" + "Ab3" * 10 + " end", "key [masked] end"),
        ("ghp_" + "a1" * 12, "[masked]"),
        ("id AK" + "IAABCDEFGHIJKLMNOP.", "id [masked]."),
        ("xo" + "xb-1234567890-abcdefghij", "[masked]"),
        # a quoted value runs to its closing quote, spaces included, and is masked at 8 characters
        ('"password": "correct horse battery staple"', '"password": "[masked]"'),
        ("password = 'correct horse'", "password = '[masked]'"),
        ('password="abcdefgh" next', 'password="[masked]" next'),
        # an unquoted one may hold , ( ) and ends at white space, & or #
        ("password=abc,def123456", "password=[masked]"),
        ("password=Ab(cd)ef12345", "password=[masked]"),
        ("password=abc12345, then", "password=[masked], then"),
        ("url?token=abc12345&x=1", "url?token=[masked]&x=1"),
        ("token=abc12345#comment", "token=[masked]#comment"),
        ("password=$ecretPass1!", "password=[masked]"),
        ("secret: Zz9+/abcdefgh=", "secret: [masked]"),
    ],
)
def test_mask_secrets_keeps_the_name_and_hides_the_value(text, shown):
    assert proc.mask_secrets(text) == shown


@pytest.mark.parametrize(
    "text",
    [
        "$ ls -la",
        "$ git status",
        "run the tests and fix the failing ones",
        "def token_count(items): return len(items)",
        "password = os.environ['PASSWORD']",
        "token = get_token(self)",
        "token=short",
        "secret: abc",
        "password=$PASSWORD",
        "password=${PASSWORD},",
        "token=${TOKEN}",
        "api_key: <token>",
        'api_key: "<your api key>"',
        'token: "${GITHUB_TOKEN}"',
        "Authorization: Bearer <token>",
        "Authorization: Bearer $TOKEN",
        "secret=xxxxxxxxxxxx",
        "secret=********",
        "use Bearer authentication for the API",
        "use Bearer authentication.",
        "task-force-assessment-of-the-project and risk-assessment-table",
        "x = self.password",
        "password = self.password",
        "DB_PASSWORD = settings.DB_PASSWORD",
        "secret: process.env.SECRET,",
        "token = AuthToken",
        "const token = readToken(req);",
        "max_tokens=4096",
        "token_url=https://example.com/oauth/token",
        "password_file=/etc/app/password",
        "",
    ],
)
def test_mask_secrets_leaves_ordinary_text_alone(text):
    assert proc.mask_secrets(text) == text


def test_mask_secrets_is_idempotent():
    once = proc.mask_secrets('password=hunter2hunter, "token": "a long quoted one"')
    assert proc.mask_secrets(once) == once


@pytest.mark.parametrize(
    "line",
    [
        "a" * 65536,
        "token_" * 11000,
        "password=" * 8000,
        'token="' * 9000,
        "token=" + "x1" * 32768,
        "Bearer " + "a1" * 32768,
        'password="' + "x" * 65536,
        "-" * 65536,
    ],
)
def test_mask_secrets_is_fast_on_a_64k_line(line):
    started = time.monotonic()
    proc.mask_secrets(line)
    assert time.monotonic() - started < 1.0


def test_a_value_is_masked_up_to_4096_characters():
    assert proc.mask_secrets("password=" + "a1" * 4000) == "password=[masked]" + "a1" * 1952

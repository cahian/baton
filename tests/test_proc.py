import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from baton import proc
from baton.adapters.base import COMPLETED, LIMIT, RoundContext, RoundResult
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


def test_round_context_and_result_defaults(tmp_path):
    assert RoundResult(COMPLETED).resume_failed is False
    ctx = make_ctx(tmp_path)
    assert ctx.session_id is None
    ctx.on_session("abc")  # default callback accepts a session id and does nothing

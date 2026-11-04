"""Guarded process runner: own session, scrubbed env, streamed output, group kill."""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from baton.adapters.base import COMPLETED, RoundContext, RoundResult

SCRUBBED_ENV = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
)

# Seconds between SIGTERM and SIGKILL. A module constant so tests can shorten it.
KILL_GRACE_S = 10.0
# Output lines longer than this are split into chunks.
MAX_LINE_BYTES = 64 * 1024
TAIL_CHARS = 4000


def clean_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Copy of the environment without any key that could unlock paid API access."""
    src = os.environ if base is None else base
    return {k: v for k, v in src.items() if k not in SCRUBBED_ENV}


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _kill_group(pgid: int, grace_s: float, reap: Callable[[], object] | None) -> None:
    """SIGTERM the group, then SIGKILL whatever survives `grace_s`.

    `reap` collects the group leader when it is our own child: an unreaped zombie leader keeps
    the group looking alive and would always burn the whole grace period.
    """
    _signal_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if reap is not None:
            reap()
        if not _group_alive(pgid):
            return
        time.sleep(0.02)
    _signal_group(pgid, signal.SIGKILL)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if reap is not None:
            reap()
        if not _group_alive(pgid):
            return
        time.sleep(0.02)


def kill_group(pgid: int, grace_s: float = 10) -> None:
    """Stop a process group: SIGTERM, then SIGKILL after `grace_s`. Missing group is fine."""
    _kill_group(pgid, grace_s, None)


@dataclass
class Exit:
    returncode: int | None
    stopped: RoundResult | None
    timed_out: bool
    tail: str


_EOF = object()


def _reader(stream, out: queue.Queue) -> None:
    try:
        for chunk in iter(lambda: stream.readline(MAX_LINE_BYTES), b""):
            out.put(chunk)
    except (OSError, ValueError):
        pass
    finally:
        out.put(_EOF)


def _feed_stdin(stream, text: str) -> None:
    try:
        stream.write(text.encode("utf-8", errors="replace"))
        stream.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except (BrokenPipeError, OSError, ValueError):
            pass


def run_streaming(
    argv: Sequence[str],
    cwd: Path,
    stdin_text: str | None,
    ctx: RoundContext,
    on_line: Callable[[str], RoundResult | None],
    tick: Callable[[], RoundResult | None],
    tick_s: float = 1.0,
) -> Exit:
    """Run `argv` in its own session and stream merged stdout/stderr line by line.

    `on_line` receives each line without its line ending; lines over 64 KB arrive in chunks.
    `on_line` and `tick` may return a RoundResult, which stops the whole process group and is
    returned as `Exit.stopped`. `ctx.should_kill()` is polled every `tick_s` and stops the round
    with COMPLETED / "stop requested". `ctx.round_max_s` and `ctx.idle_s` end the round with
    `timed_out=True`. Raw bytes are appended to `ctx.log_path`.
    """
    ctx.log_path.parent.mkdir(parents=True, exist_ok=True)
    child = subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=clean_env(),
        stdin=subprocess.DEVNULL if stdin_text is None else subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    pgid = child.pid
    lines: queue.Queue = queue.Queue()
    reader = threading.Thread(target=_reader, args=(child.stdout, lines), daemon=True)
    reader.start()
    if stdin_text is not None and child.stdin is not None:
        threading.Thread(target=_feed_stdin, args=(child.stdin, stdin_text), daemon=True).start()

    tail = ""
    stopped: RoundResult | None = None
    timed_out = False

    def record(chunk: bytes, log) -> str:
        nonlocal tail
        log.write(chunk)
        log.flush()
        line = chunk.decode("utf-8", errors="replace").rstrip("\r\n")
        tail = (tail + line + "\n")[-TAIL_CHARS:]
        return line

    with ctx.log_path.open("ab") as log:
        try:
            ctx.on_spawn(pgid)
            started = last_output = last_check = time.monotonic()
            eof = False
            while stopped is None and not timed_out:
                if eof:
                    try:
                        child.wait(timeout=tick_s)
                        break
                    except subprocess.TimeoutExpired:
                        item = None
                else:
                    try:
                        item = lines.get(timeout=tick_s)
                    except queue.Empty:
                        item = None
                now = time.monotonic()
                if item is _EOF:
                    eof = True
                elif item is not None:
                    last_output = now
                    line = record(item, log)
                    ctx.on_output(line)
                    stopped = on_line(line)
                    if stopped is not None:
                        break
                if now - last_check >= tick_s:
                    last_check = now
                    if ctx.should_kill():
                        stopped = RoundResult(COMPLETED, reason="stop requested")
                    else:
                        stopped = tick()
                    if stopped is None:
                        if now - started >= ctx.round_max_s or now - last_output >= ctx.idle_s:
                            timed_out = True
        finally:
            if stopped is not None or timed_out or child.poll() is None:
                _kill_group(pgid, KILL_GRACE_S, child.poll)
            returncode = child.wait()
            reader.join(timeout=2)
            # Whatever the child wrote before dying still belongs in the log and the tail.
            while True:
                try:
                    item = lines.get_nowait()
                except queue.Empty:
                    break
                if item is not _EOF:
                    record(item, log)
            for stream in (child.stdout, child.stdin):
                try:
                    if stream is not None:
                        stream.close()
                except (OSError, ValueError):
                    pass

    return Exit(returncode=returncode, stopped=stopped, timed_out=timed_out, tail=tail)

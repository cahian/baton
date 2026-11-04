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

# Variables that make a vendor CLI authenticate with something other than the subscription login.
# The Codex entries come from the strings of its binary: it reads CODEX_API_KEY and
# CODEX_ACCESS_TOKEN ahead of the stored login, and workload-identity federation from OPENAI_*.
# Cloud credentials (AWS_*, AZURE_*, GOOGLE_APPLICATION_CREDENTIALS) are left alone on purpose: the
# agent's own work may need them, and nothing selects a cloud provider unless its config says so.
SCRUBBED_ENV = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CODEX_API_KEY",
    "CODEX_ACCESS_TOKEN",
    "CODEX_GUARDIAN_DECISIONS_API_KEY",
    "OPENAI_IDENTITY_TOKEN_FILE",
    "OPENAI_FEDERATION_RULE_ID",
    "OPENAI_WORKLOAD_IDENTITY_CONTEXT",
)

# Seconds between SIGTERM and SIGKILL. A module constant so tests can shorten it.
KILL_GRACE_S = 10.0
# Output lines longer than this are split into chunks.
MAX_LINE_BYTES = 64 * 1024
TAIL_CHARS = 4000
# How often the loop looks for the leader's exit while the child is silent.
POLL_S = 0.1
# After the leader exits, how long to keep reading what is still in the pipe.
DRAIN_S = 0.25
# How long to wait for the reader thread once the group is dead.
JOIN_S = 1.0


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


def _leader_exited(child: subprocess.Popen) -> bool:
    """True once the leader has exited, without reaping it so its process group stays valid."""
    if hasattr(os, "waitid") and hasattr(os, "WNOWAIT"):
        try:
            return os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
        except ChildProcessError:
            return True
    return child.poll() is not None


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


def _reader(stream, out: queue.Queue, limit: list[float]) -> None:
    """Feed `out` with raw chunks until EOF or until a chunk is read after `limit[0]`.

    The limit is what makes the queue finite once the leader is gone: a process outside the
    group (setsid survivor) can hold the pipe open and write forever, and nothing it writes
    after the deadline is wanted.
    """
    try:
        for chunk in iter(lambda: stream.readline(MAX_LINE_BYTES), b""):
            if time.monotonic() > limit[0]:
                break
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
    # Open the log before spawning: a failure here must not leave a running, unwatched child.
    log = ctx.log_path.open("ab")
    try:
        child = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=clean_env(),
            stdin=subprocess.DEVNULL if stdin_text is None else subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except BaseException:
        log.close()
        raise
    pgid = child.pid
    tail = ""
    stopped: RoundResult | None = None
    timed_out = False
    reader: threading.Thread | None = None
    lines: queue.Queue = queue.Queue()
    # Chunks read after this monotonic time are dropped by the reader. Infinite until the
    # leader exits or the round is being stopped.
    limit = [float("inf")]

    def record(chunk: bytes) -> str:
        nonlocal tail
        log.write(chunk)
        line = chunk.decode("utf-8", errors="replace").rstrip("\r\n")
        tail = (tail + line + "\n")[-TAIL_CHARS:]
        return line

    try:
        reader = threading.Thread(target=_reader, args=(child.stdout, lines, limit), daemon=True)
        reader.start()
        if stdin_text is not None and child.stdin is not None:
            feeder = threading.Thread(target=_feed_stdin, args=(child.stdin, stdin_text))
            feeder.daemon = True
            feeder.start()
        ctx.on_spawn(pgid)
        started = last_output = last_check = time.monotonic()
        eof = False
        exited_at: float | None = None
        while stopped is None and not timed_out:
            try:
                item = lines.get(timeout=min(tick_s, POLL_S))
            except queue.Empty:
                item = None
            now = time.monotonic()
            if item is None or lines.empty():
                log.flush()  # buffered while a backlog is being worked off
            if item is _EOF:
                eof = True
            elif item is not None:
                last_output = now
                line = record(item)
                ctx.on_output(line)
                stopped = on_line(line)
                if stopped is not None:
                    break
            # The leader's exit ends the round even if a grandchild still holds the pipe.
            # Everything the reader got before exit + DRAIN_S still reaches on_line, however
            # slow on_line is; the reader refuses later data, so that backlog is finite.
            if exited_at is None and _leader_exited(child):
                exited_at = now
                limit[0] = min(limit[0], now + DRAIN_S)
            if exited_at is not None and (eof or (lines.empty() and now >= limit[0])):
                break
            if now - last_check >= tick_s:
                last_check = now
                if ctx.should_kill():
                    stopped = RoundResult(COMPLETED, reason="stop requested")
                else:
                    stopped = tick()
                if stopped is None and exited_at is None:
                    if now - started >= ctx.round_max_s or now - last_output >= ctx.idle_s:
                        timed_out = True
    finally:
        try:
            # A stop or timeout also gets a bounded window to collect what is already in flight.
            limit[0] = min(limit[0], time.monotonic() + DRAIN_S)
            # Always sweep the group, also after a normal exit, so nothing outlives the round.
            _kill_group(pgid, KILL_GRACE_S, child.poll)
            returncode = child.wait()
            reader_alive = False
            if reader is not None:
                reader.join(timeout=JOIN_S)
                reader_alive = reader.is_alive()
            # Whatever the child wrote before dying still belongs in the log and the tail.
            # Finite: the reader stopped at `limit`, which has passed by now.
            while True:
                try:
                    item = lines.get_nowait()
                except queue.Empty:
                    break
                if item is not _EOF:
                    record(item)
            # Closing the pipe would block on the reader's buffer lock if a process outside the
            # group (setsid survivor) still holds it; leave it to the daemon thread then.
            if not reader_alive and child.stdout is not None:
                try:
                    child.stdout.close()
                except (OSError, ValueError):
                    pass
        finally:
            log.close()  # flushes

    return Exit(returncode=returncode, stopped=stopped, timed_out=timed_out, tail=tail)

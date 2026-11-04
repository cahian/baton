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
# After the leader exits, the drain ends once nothing new arrived for this long.
DRAIN_S = 0.25
# Hard cap on the drain: a setsid survivor can keep the pipe full forever.
HARD_DRAIN_S = 3.0
# Chunks held between the reader thread and the loop. The reader blocks when it is full, so
# a slow on_line slows the reader down instead of growing memory.
QUEUE_MAX = 4096
PUT_TIMEOUT_S = 0.05
# While a group is being killed the loop keeps the pipe flowing so a SIGTERM handler that
# writes a farewell does not block; this much of it is kept in the log.
PUMP_LOG_BYTES = 16 * 1024 * 1024
# How long to wait for the reader thread once the group is dead.
JOIN_S = 0.5


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


def _kill_group(
    pgid: int,
    grace_s: float,
    reap: Callable[[], object] | None,
    pump: Callable[[], object] | None = None,
) -> None:
    """SIGTERM the group, then SIGKILL whatever survives `grace_s`.

    `reap` collects the group leader when it is our own child: an unreaped zombie leader keeps
    the group looking alive and would always burn the whole grace period. `pump` is called on
    every poll so the caller can keep reading the dying group's output.
    """
    _signal_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if pump is not None:
            pump()
        if reap is not None:
            reap()
        if not _group_alive(pgid):
            return
        time.sleep(0.02)
    _signal_group(pgid, signal.SIGKILL)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if pump is not None:
            pump()
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


def _put(out: queue.Queue, item: object, halt: threading.Event) -> bool:
    """Block until `item` is queued; False if the runner halted first."""
    while not halt.is_set():
        try:
            out.put(item, timeout=PUT_TIMEOUT_S)
            return True
        except queue.Full:
            continue
    return False


def _reader(stream, out: queue.Queue, halt: threading.Event) -> None:
    """Feed `out` with raw chunks until EOF or until `halt` is set."""
    try:
        for chunk in iter(lambda: stream.readline(MAX_LINE_BYTES), b""):
            if not _put(out, chunk, halt):
                return
    except (OSError, ValueError):
        pass
    _put(out, _EOF, halt)


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
    lines: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)
    halt = threading.Event()  # set when the runner is done; releases a blocked reader
    pumped = 0

    def record(chunk: bytes) -> str:
        nonlocal tail
        log.write(chunk)
        line = chunk.decode("utf-8", errors="replace").rstrip("\r\n")
        tail = (tail + line + "\n")[-TAIL_CHARS:]
        return line

    def pump() -> None:
        """Keep reading while the group is being killed (log only, no on_line)."""
        nonlocal pumped
        while True:
            try:
                item = lines.get_nowait()
            except queue.Empty:
                return
            if item is not _EOF and pumped < PUMP_LOG_BYTES:
                pumped += len(item)
                record(item)

    try:
        reader = threading.Thread(target=_reader, args=(child.stdout, lines, halt), daemon=True)
        reader.start()
        if stdin_text is not None and child.stdin is not None:
            feeder = threading.Thread(target=_feed_stdin, args=(child.stdin, stdin_text))
            feeder.daemon = True
            feeder.start()
        ctx.on_spawn(pgid)
        started = last_output = last_check = time.monotonic()
        eof = False
        exited_at: float | None = None
        deferred: RoundResult | None = None
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
            # Whatever is queued keeps flowing to on_line, however slow it is, until the pipe
            # goes quiet, EOF, or the hard cap.
            if exited_at is None and _leader_exited(child):
                exited_at = now
            if exited_at is not None:
                if eof or now - exited_at >= HARD_DRAIN_S:
                    break
                if lines.empty() and now - max(exited_at, last_output) >= DRAIN_S:
                    break
            if now - last_check >= tick_s:
                last_check = now
                result = (
                    RoundResult(COMPLETED, reason="stop requested") if ctx.should_kill() else tick()
                )
                if result is not None:
                    if exited_at is None:
                        stopped = result
                    elif deferred is None:
                        # Mid-drain: lines still queued may carry a guard result, which wins.
                        deferred = result
                elif exited_at is None:
                    if now - started >= ctx.round_max_s or now - last_output >= ctx.idle_s:
                        timed_out = True
        if stopped is None:
            stopped = deferred
    finally:
        try:
            # Always sweep the group, also after a normal exit, so nothing outlives the round.
            # The reader keeps running meanwhile: a SIGTERM handler's last words must not
            # block on a full pipe.
            _kill_group(pgid, KILL_GRACE_S, child.poll, pump)
            returncode = child.wait()
            reader_alive = False
            if reader is not None:
                end = time.monotonic() + JOIN_S  # EOF arrives once the group is dead
                while reader.is_alive() and time.monotonic() < end:
                    pump()
                    reader.join(timeout=0.02)
                pump()
                halt.set()
                reader.join(timeout=0.05)
                reader_alive = reader.is_alive()
            else:
                halt.set()
            pump()  # whatever the child wrote before dying still belongs in the log and tail
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

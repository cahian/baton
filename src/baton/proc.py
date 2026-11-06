"""Guarded process runner: own session, scrubbed env, streamed output, group kill."""

from __future__ import annotations

import fnmatch
import os
import queue
import re
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
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "CODEX_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",  # a Claude subscription login: only the API key may be used
    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
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
# After the leader exits, the round ends once nothing new arrived for this long.
DRAIN_S = 0.25
# Limit on that wait: a setsid survivor can keep the pipe full forever.
HARD_DRAIN_S = 3.0
# Chunks held between the reader thread and the loop. The reader blocks when it is full, so
# a slow on_line slows the reader down instead of growing memory, and the backlog that is
# always delivered is finite.
QUEUE_MAX = 4096
PUT_TIMEOUT_S = 0.05
# Output that arrives after a stop still goes to the log, up to this much. It is read either
# way, so a SIGTERM handler that writes a farewell does not block.
STOP_LOG_BYTES = 16 * 1024 * 1024
# How long reading continues once the group is dead, to reach EOF.
JOIN_S = 0.5
# What replaces a credential that a child printed, and the shortest value worth hiding.
REDACTED = b"[redacted]"
MIN_SECRET_CHARS = 8


# What replaces the value of a credential in text that is shown or kept (an agent's output).
MASKED = "[masked]"
# The longest value masked: a longer line cannot cost more than this per credential.
MAX_VALUE_CHARS = 4096
_MAX = str(MAX_VALUE_CHARS)
_MIN = str(MIN_SECRET_CHARS)
_TOKEN_RES = (
    re.compile(r"(?<![\w/-])sk-[A-Za-z0-9_-]{20," + _MAX + "}"),
    re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{20," + _MAX + "}"),
    re.compile(r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}(?![0-9A-Z])"),
    re.compile(r"(?<![A-Za-z0-9])xox[a-z]-[A-Za-z0-9-]{10," + _MAX + "}"),
)
_BEARER_RE = re.compile(
    r"(?<![A-Za-z0-9])(Bearer[ \t]+)([A-Za-z0-9._~+/=-]{" + _MIN + "," + _MAX + "})", re.I
)
# `name = value`, found one identifier at a time: a name is a run of word characters that starts
# where a run starts, so a long line is read once. Whether the name is one that holds a credential
# is decided on the name alone.
_HEAD_RE = re.compile(r"(?<![A-Za-z0-9_-])(?P<name>[A-Za-z0-9_-]++)(?:\\?[\"'])?[ \t]*[=:][ \t]*")
_NAME_RE = re.compile(
    r"[A-Za-z0-9_-]*?(?:token|api[_-]?key|secret|password|passwd)(?:[_-][A-Za-z0-9_-]*)?", re.I
)
_QUOTE_RE = re.compile(r"\\?[\"']")
_UNQUOTED_RE = re.compile(r"[^\s&#\"']{1," + _MAX + "}")
_PLACEHOLDER_RE = re.compile(
    r"\$[A-Za-z_][A-Za-z0-9_]*|\$\{[A-Za-z_][A-Za-z0-9_]*\}|<[\w .-]+>|[xX*.]+"
)
# An unquoted value is taken for a credential only when it holds a digit or one of these: letters
# with . _ - are names of things (`self.password`, `settings.DB_PASSWORD`, `AuthToken`).
_SECRET_SYMBOLS = frozenset("!@$%^*+=~|?")
_TRAILING = ",);]"


def _secret_like(value: str) -> bool:
    return any(c.isdigit() or c in _SECRET_SYMBOLS for c in value)


def _mask_named(text: str) -> str:
    """Mask the value of `token`, `api_key`, `secret`, `password` and `passwd` (also inside a longer
    name: `DB_PASSWORD`, `SECRET_KEY`). A quoted value runs to its closing quote and is masked from
    MIN_SECRET_CHARS on; an unquoted one ends at white space, `&`, `#` or a quote, loses its
    trailing `,);]`, and is masked only when it looks like a credential. A placeholder never is."""
    out: list[str] = []
    pos = 0
    for head in _HEAD_RE.finditer(text):
        if head.start() < pos or not _NAME_RE.fullmatch(head.group("name")):
            continue
        start = head.end()
        quote = _QUOTE_RE.match(text, start)
        if quote:
            start += len(quote.group())
            window = text[start : start + MAX_VALUE_CHARS]
            newline = window.find("\n")
            stop = start + (newline if newline >= 0 else len(window))
            end = text.find(quote.group(), start, stop)
            if end < 0:
                end = stop  # an unclosed quote (a cut fragment): to the end of the line
            value = text[start:end]
            masked = len(value) >= MIN_SECRET_CHARS and not _PLACEHOLDER_RE.fullmatch(value)
        else:
            found = _UNQUOTED_RE.match(text, start)
            if not found:
                continue
            value = found.group().rstrip(_TRAILING)
            end = start + len(value)
            masked = (
                len(value) >= MIN_SECRET_CHARS
                and not _PLACEHOLDER_RE.fullmatch(value)
                and _secret_like(value)
            )
        if masked:
            out += [text[pos:start], MASKED]
            pos = end
    return "".join(out) + text[pos:] if out else text


def _mask_bearer(found: re.Match[str]) -> str:
    value = found.group(2)
    core = value.rstrip(".,;:")
    if len(core) < MIN_SECRET_CHARS or not _secret_like(core) or _PLACEHOLDER_RE.fullmatch(core):
        return found.group(0)
    return found.group(1) + MASKED + value[len(core) :]


def mask_secrets(text: str) -> str:
    """`text` with the value of every credential in it replaced by `[masked]`, name kept: a bearer
    token, `token`/`api_key`/`secret`/`password` given a value of at least MIN_SECRET_CHARS
    characters, and the well-known token shapes (`sk-`, `ghp_`, `AKIA`, `xoxb-`). A placeholder
    (`$TOKEN`, `${TOKEN}`, `<token>`, `xxxxxxxx`) is not a value. Known limit: an unquoted value of
    letters only (with . _ -) is read as a name in code and kept."""
    if not text:
        return text
    for pattern in _TOKEN_RES:
        text = pattern.sub(MASKED, text)
    return _mask_named(_BEARER_RE.sub(_mask_bearer, text))


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


def _wait_group_gone(pgid: int, seconds: float, reap: Callable[[], object] | None) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if reap is not None:
            reap()
        if not _group_alive(pgid):
            return True
        time.sleep(0.02)
    return False


def _kill_group(pgid: int, grace_s: float, reap: Callable[[], object] | None) -> None:
    """SIGTERM the group, then SIGKILL whatever survives `grace_s`.

    `reap` collects the group leader when it is our own child: an unreaped zombie leader keeps
    the group looking alive and would always burn the whole grace period.
    """
    _signal_group(pgid, signal.SIGTERM)
    if not _wait_group_gone(pgid, grace_s, reap):
        _signal_group(pgid, signal.SIGKILL)
        _wait_group_gone(pgid, 2, reap)


def kill_group(pgid: int, grace_s: float = 10) -> None:
    """Stop a process group: SIGTERM, then SIGKILL after `grace_s`. Missing group is fine."""
    _kill_group(pgid, grace_s, None)


PS_TIMEOUT_S = 5


def process_start(pid: int) -> str | None:
    """When process `pid` started, as `ps` prints it ("" when `ps` says there is no such process).

    None when `ps` itself could not be run or answered nonsense. Two processes that ever held the
    same pid have different start times, which is what tells a recycled number from ours.
    """
    try:
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(int(pid))],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=PS_TIMEOUT_S,
            env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},  # lstart is local time
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if out.returncode not in (0, 1):
        return None
    if out.returncode == 1 and out.stderr.strip():
        return None  # exit 1 with a message is an error; only a silent 1 means "no such process"
    return " ".join(out.stdout.split())


OURS, DEAD, FOREIGN, UNKNOWN = "ours", "dead", "foreign", "unknown"


def check_group(pgid: object, recorded_start: object) -> str:
    """What the group recorded in state.json is now.

    `dead`: nothing is running in it. `ours`: it is the group baton started, so it may be killed.
    `foreign`: the number now belongs to something else (our own group, 0, 1, or a start time that
    no longer matches). `unknown`: it is alive but cannot be proven ours, because no start time
    was recorded or `ps` could not answer. Unknown must never be signalled and never be forgotten.

    A leader that is gone while the group lives is ours: a pid cannot be reused while a group
    still carries it.
    """
    if isinstance(pgid, bool) or not isinstance(pgid, int) or pgid <= 1 or pgid == os.getpgrp():
        return FOREIGN
    if not _group_alive(pgid):
        return DEAD
    if not isinstance(recorded_start, str) or not recorded_start:
        return UNKNOWN
    now = process_start(pgid)
    if now is None:
        return UNKNOWN
    return OURS if now == "" or now == recorded_start else FOREIGN


@dataclass
class Exit:
    returncode: int | None
    stopped: RoundResult | None
    timed_out: bool
    tail: str


_EOF = object()


def _put(out: queue.Queue, item: object, abandon: threading.Event) -> bool:
    """Block until `item` is queued; False if the runner abandoned the reader first."""
    while not abandon.is_set():
        try:
            out.put(item, timeout=PUT_TIMEOUT_S)
            return True
        except queue.Full:
            continue
    return False


def _reader(stream, out: queue.Queue, stop: threading.Event, abandon: threading.Event) -> None:
    """Feed `out` with raw chunks until EOF or `stop`, then queue _EOF.

    `stop` only ends reading: a chunk already read is still queued. `abandon` is the runner's
    way out and releases a blocked put.
    """
    try:
        while not stop.is_set():
            chunk = stream.readline(MAX_LINE_BYTES)
            if not chunk:
                break
            if not _put(out, chunk, abandon):
                return
    except (OSError, ValueError):
        pass
    _put(out, _EOF, abandon)


def _without_secret_start(data: bytes, secrets: Sequence[bytes]) -> bytes:
    """`data` without its longest ending that is the beginning of one of the secrets."""
    for size in range(min(len(data), max(len(x) for x in secrets) - 1), 0, -1):
        ending = data[-size:]
        if any(x.startswith(ending) for x in secrets):
            return data[:-size]
    return data


def _log_quietly(operation: Callable[..., object], *args: object) -> None:
    """Run a log operation: a full disk must never reach the kill or the on_line path."""
    try:
        operation(*args)
    except (OSError, ValueError):
        pass


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
    extra_env: Mapping[str, str] | None = None,
    plain_env: Mapping[str, str] | None = None,
    scrub: Sequence[str] = (),
) -> Exit:
    """Run `argv` in its own session and stream merged stdout/stderr line by line.

    `on_line` receives each line without its line ending; lines over 64 KB arrive in chunks.
    `on_line` and `tick` may return a RoundResult, which stops the whole process group and is
    returned as `Exit.stopped`. `ctx.should_kill()` is polled every `tick_s` and stops the round
    with COMPLETED / "stop requested". `ctx.round_max_s` and `ctx.idle_s` end the round with
    `timed_out=True`. Raw bytes are appended to `ctx.log_path`; failing to write them never
    affects the run.

    `extra_env` is added to the scrubbed environment of this child only: the way an adapter passes
    its own credential. Its values are replaced by `[redacted]` in everything that leaves this
    function (the log, `on_output`, `on_line`, the tail), in case the child prints them.
    `scrub` names (patterns) of further environment variables to drop first, before `extra_env`
    puts the adapter's own back.
    `plain_env` is added the same way but is no secret (a model name, a switch), so it is not
    replaced: the stream has to be able to show it.

    Time limits only stop reading. Every queued line reaches `on_line` unless a stop was already
    decided, and the queue is bounded, so that is finite. The group is killed from its own
    thread, so a slow `on_line` or a broken log cannot delay it.
    """
    ctx.log_path.parent.mkdir(parents=True, exist_ok=True)
    env = clean_env()
    for pattern in scrub:  # shell-style names: whatever the CLI could take a credential from
        for name in [k for k in env if fnmatch.fnmatchcase(k, pattern)]:
            del env[name]
    secrets = []
    for value in (extra_env or {}).values():
        env_value = str(value)
        if len(env_value) >= MIN_SECRET_CHARS:
            secrets.append(env_value.encode("utf-8", errors="replace"))
    if extra_env:
        env.update(extra_env)
    if plain_env:
        env.update(plain_env)
    # Open the log before spawning: a failure here must not leave a running, unwatched child.
    log = ctx.log_path.open("ab")
    try:
        child = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=env,
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
    lines: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)
    stop_reading = threading.Event()
    abandon = threading.Event()  # releases a reader blocked on a full queue when we are done
    reader = threading.Thread(
        target=_reader, args=(child.stdout, lines, stop_reading, abandon), daemon=True
    )
    # Started when the round ends, so nothing that goes wrong in delivery can stand in its way.
    killer = threading.Thread(
        target=_kill_group, args=(pgid, KILL_GRACE_S, child.poll), daemon=True
    )
    killer_running = False
    delivering = True  # false once a stop is decided or on_line raised
    eof = False
    last_output = time.monotonic()
    logged_after_stop = 0
    held = b""  # the end of a cut line, not yet written: see record()

    def record(chunk: bytes) -> str:
        nonlocal tail, held
        cut = len(chunk) >= MAX_LINE_BYTES and not chunk.endswith(b"\n")
        chunk, held = held + chunk, b""
        for secret in secrets:
            chunk = chunk.replace(secret, REDACTED)
        if secrets and not chunk.endswith(b"\n"):
            if cut:
                # A line is cut into 64 KB chunks and a secret can straddle the cut: keep back
                # the last bytes that could be the start of one, and look at them again with
                # the next.
                keep = max(len(x) for x in secrets) - 1
                chunk, held = chunk[:-keep], chunk[-keep:]
            else:
                # The last piece of the output: nothing follows to complete a match, so an
                # ending that could be the start of a secret is left out, of the log, of the
                # tail and of what the callbacks see.
                chunk = _without_secret_start(chunk, secrets)
        _log_quietly(log.write, chunk)
        line = chunk.decode("utf-8", errors="replace").rstrip("\r\n")
        tail = (tail + line + "\n")[-TAIL_CHARS:]
        return line

    def take(timeout: float) -> bool:
        """Handle the next queued item; False if none arrived within `timeout`."""
        nonlocal stopped, delivering, eof, last_output, logged_after_stop, held
        try:
            item = lines.get(timeout=timeout)
        except queue.Empty:
            item = None
        if item is _EOF:
            eof = True
        elif item is not None:
            last_output = time.monotonic()
            if delivering:
                delivering = False
                line = record(item)
                ctx.on_output(line)
                stopped = on_line(line)
                delivering = stopped is None
            elif logged_after_stop < STOP_LOG_BYTES:
                logged_after_stop += len(item)
                record(item)
            else:
                # The log is full and this line's next piece is dropped, so the end held back
                # from the last one can never be checked against it: it may be the start of a
                # secret, and it is dropped too.
                held = b""
        if lines.empty():
            _log_quietly(log.flush)  # buffered while a backlog is being worked off
        return item is not None

    def sweep() -> None:
        """End the group from the killer thread or, when no thread can be had, from this one:
        the delivery of the queued lines waits then, but the agent dies regardless."""
        nonlocal killer_running
        try:
            killer.start()
            killer_running = True
        except Exception:
            _kill_group(pgid, KILL_GRACE_S, child.poll)

    def finish() -> None:
        """Work off the queue while the group dies, then stop reading and empty it."""
        ending_at = None
        while not eof:
            took = take(POLL_S)
            if killer.is_alive():
                continue
            now = time.monotonic()
            ending_at = ending_at or now + JOIN_S  # EOF arrives once the group is dead
            if now >= ending_at:
                stop_reading.set()
                if not took:
                    return  # the reader sits in a read that a setsid survivor keeps open

    try:
        try:
            reader.start()
            if stdin_text is not None and child.stdin is not None:
                feeder = threading.Thread(target=_feed_stdin, args=(child.stdin, stdin_text))
                feeder.daemon = True
                feeder.start()
            ctx.on_spawn(pgid)
            started = last_check = last_output = time.monotonic()
            exited_at: float | None = None
            deferred: RoundResult | None = None
            while stopped is None and not timed_out:
                take(min(tick_s, POLL_S))
                if stopped is not None:
                    break
                now = time.monotonic()
                # The leader's exit ends the round even if a grandchild still holds the pipe.
                if exited_at is None and _leader_exited(child):
                    exited_at = now
                if exited_at is not None:
                    quiet = lines.empty() and now - max(exited_at, last_output) >= DRAIN_S
                    if eof or quiet or now - exited_at >= HARD_DRAIN_S:
                        break
                if now - last_check >= tick_s:
                    last_check = now
                    result = (
                        RoundResult(COMPLETED, reason="stop requested")
                        if ctx.should_kill()
                        else tick()
                    )
                    if result is not None:
                        if exited_at is None:
                            stopped = result
                            delivering = False
                        elif deferred is None:
                            # Lines still queued may carry a guard result, which wins.
                            deferred = result
                    elif exited_at is None:
                        if now - started >= ctx.round_max_s or now - last_output >= ctx.idle_s:
                            timed_out = True
        finally:
            # Always sweep the group, also after a normal exit, so nothing outlives the round.
            sweep()
            finish()
        if stopped is None:
            stopped = deferred
    finally:
        abandon.set()
        if killer_running:
            killer.join()
        returncode = child.wait()
        reader.join(timeout=0.05)
        # Closing the pipe would block on the reader's buffer lock if a process outside the
        # group (setsid survivor) still holds it; leave it to the daemon thread then.
        if not reader.is_alive() and child.stdout is not None:
            try:
                child.stdout.close()
            except (OSError, ValueError):
                pass
        if held:
            # The end of a last line that never got its next chunk. Written, but never a stretch
            # that could be the start of a secret: with nothing after it to complete the match,
            # that stretch cannot be told from the secret's own beginning.
            _log_quietly(log.write, _without_secret_start(held, secrets))
        _log_quietly(log.close)

    return Exit(returncode=returncode, stopped=stopped, timed_out=timed_out, tail=tail)

"""The OpenCode gateway: the whole rotation behind one OpenAI-compatible model, `baton/auto`.

One job per OpenCode session runs `run_goal` in a daemon thread of this process. The HTTP
connection only watches the job: dropping it never stops the work, and the next request for the
same session either interrupts the running job with a note or starts a new one.
"""

from __future__ import annotations

import json
import os
import re
import select
import signal
import socket
import sqlite3
import threading
import time
import tomllib
import uuid
from collections.abc import Callable, Iterator, Mapping
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

from baton.adapters.base import Adapter
from baton.config import Goal, GoalError, check_repo_table, goal_from_table
from baton.proc import OURS, check_group, kill_group
from baton.runner import run_goal
from baton.store import AlreadyRunning, StateError, Store, state_dir

HOST = "127.0.0.1"
DEFAULT_PORT = 18777
MODEL_ID = "auto"
# An empty-content delta this often keeps long waits from tripping a client's idle timeout.
HEARTBEAT_S = 15.0
HISTORY_MAX_CHARS = 24000
TITLE_WORDS = 6
TITLE_PREFIX = "Here is the first message of the conversation:"
MAX_BODY_BYTES = 32 * 1024 * 1024
# What a job keeps in memory for late readers; older output is still in the round logs.
FEED_MAX_ITEMS = 5000
# A job that is wrapping up has this long to release its lock before a new one is refused.
HANDOVER_S = 10.0
SHUTDOWN_S = 25.0
# A message that lands in the inbox in the instant a goal finishes starts the goal again, up to
# this many times in a row.
MAX_RESTARTS = 5
SETTLE_POLL_S = 0.05
GATE_SHOWN_CHARS = 1500
NAME_MAX_CHARS = 100

_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]")
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


class GatewayError(Exception):
    """Something to tell the person in the chat, in plain words."""


# request parsing


def _unquote(text: str) -> str:
    """OpenCode sometimes sends a string JSON-quoted ("\\"oi\\""): give back the text inside."""
    stripped = text.strip()
    if len(stripped) >= 2 and stripped[0] == '"' and stripped[-1] == '"':
        try:
            inner = json.loads(stripped)
        except ValueError:
            return text
        if isinstance(inner, str):
            return inner
    return text


def content_text(content: object) -> str:
    """The text of a message's `content`: a string, a JSON-quoted string or a list of parts."""
    if isinstance(content, str):
        return _unquote(content)
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(_unquote(part))
            elif isinstance(part, Mapping) and part.get("type", "text") == "text":
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(_unquote(text))
        return "\n".join(p for p in parts if p)
    return ""


def _role(message: object) -> str:
    return message.get("role", "") if isinstance(message, Mapping) else ""


def _text_of(message: object) -> str:
    return content_text(message.get("content")) if isinstance(message, Mapping) else ""


def last_user_index(messages: list) -> int | None:
    for i in range(len(messages) - 1, -1, -1):
        if _role(messages[i]) == "user":
            return i
    return None


def history_text(messages: list) -> str:
    """The conversation as plain text, the last HISTORY_MAX_CHARS of it."""
    lines: list[str] = []
    for message in messages:
        role = _role(message)
        if role not in ("user", "assistant"):
            continue  # the system prompt and tool traffic belong to OpenCode's own agent
        text = _text_of(message).strip()
        if text:
            lines.append(f"{'Person' if role == 'user' else 'Assistant'}: {text}")
    joined = "\n\n".join(lines)
    if len(joined) > HISTORY_MAX_CHARS:
        joined = "..." + joined[-HISTORY_MAX_CHARS:]
    return joined


def title_for(messages: list) -> str:
    """Up to six words from the first user message, for OpenCode's session-title request."""
    texts = [_text_of(m) for m in messages if _role(m) == "user"]
    last = texts[-1] if texts else ""
    source = last.split(TITLE_PREFIX, 1)[1] if TITLE_PREFIX in last else (texts[0] if texts else "")
    words = [w for w in re.sub(r"</?[A-Za-z_][^>\n]*>", " ", source).split() if w.strip("\"'`")]
    title = " ".join(w.strip("\"'`") for w in words[:TITLE_WORDS])
    return title or "New session"


# the session's directory


def opencode_db() -> Path:
    override = os.environ.get("BATON_OPENCODE_DB")
    return (
        Path(override).expanduser()
        if override
        else Path.home() / ".local/share/opencode/opencode.db"
    )


def session_directory(session: str) -> Path:
    """The directory OpenCode recorded for `session`. Never guessed: raises GatewayError."""
    db = opencode_db()
    if not db.is_file():
        raise GatewayError(
            f"baton cannot find OpenCode's database at {db}, so it cannot tell which directory "
            "this session works in (set BATON_OPENCODE_DB if it lives elsewhere)."
        )
    try:
        conn = sqlite3.connect(f"file:{quote(str(db))}?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute(
                "select directory from session_v2 where id = ?", (session,)
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise GatewayError(f"baton could not read OpenCode's database {db}: {exc}") from None
    directory = row[0] if row else None
    if not isinstance(directory, str) or not directory.strip():
        raise GatewayError(
            f"OpenCode's database has no directory for session {session}, so baton does not "
            "know where to work."
        )
    path = Path(directory)
    if not path.is_dir():
        raise GatewayError(f"the directory of session {session} does not exist: {path}")
    return path


def job_name(session: str) -> str:
    return ("session-" + _UNSAFE_RE.sub("_", session))[:NAME_MAX_CHARS]


# the goal


def build_goal(name: str, workdir: Path, objective: str) -> Goal:
    """The goal for a session job, in memory, through the same builder as a goal file. The
    workdir's `baton.toml` supplies the verify command, the agent settings and the timings;
    `name`, `workdir` and `objective` in it are ignored, since the session decides those, and it
    may not choose executables or credentials. Without one there is no gate. A `baton.toml` that
    cannot be read or is invalid is an error, never a silent fall back to no gate."""
    path = workdir / "baton.toml"
    data: dict = {}
    if path.exists():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise GoalError(f"invalid {path}: {exc}") from None
    rest = {k: v for k, v in data.items() if k not in ("name", "workdir", "objective")}
    try:
        check_repo_table(rest)
        return goal_from_table(rest, name=name, workdir=workdir, objective=objective, path=path)
    except GoalError as exc:
        raise GoalError(f"{path}: {exc}" if data else str(exc)) from None


# what the person reads


def _duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds >= 3600:
        return f"{seconds // 3600} h {seconds % 3600 // 60:02d} min"
    if seconds >= 60:
        return f"{seconds // 60} min"
    return f"{seconds} s"


def render_event(kind: str, f: Mapping) -> str:
    """The chat text for one run event, "" for events the person does not need to see."""
    if kind == "output":
        return f"{f.get('text', '')}\n"
    if kind == "round_start":
        agent, previous, n = f.get("agent"), f.get("previous"), f.get("round")
        if previous and previous != agent:
            return f"\n[baton] rotation: {previous} -> {agent} (round {n})\n\n"
        resumed = " (resumed)" if f.get("resumed") else ""
        return f"\n[baton] round {n}: {agent}{resumed}\n\n"
    if kind == "round_end":
        outcome = f.get("outcome")
        if outcome == "completed" and not f.get("interrupted"):
            return ""
        why = f" ({f['reason']})" if f.get("reason") else ""
        how = "interrupted by your message" if f.get("interrupted") else str(outcome)
        return f"\n[baton] round {f.get('round')} ({f.get('agent')}) ended: {how}{why}\n\n"
    if kind == "wait":
        why = f.get("reason") or "every agent is resting"
        return f"\n[baton] waiting {_duration(f.get('seconds') or 0)}: {why}\n\n"
    if kind == "gate":
        if f.get("passed"):
            return "\n[baton] verify passed\n\n"
        tail = str(f.get("tail") or "").strip()[-GATE_SHOWN_CHARS:]
        shown = f"\n```\n{tail}\n```" if tail else ""
        return f"\n[baton] verify did not pass{shown}\n\n"
    if kind == "alert":
        return f"\n[baton] alert: {f.get('message') or f.get('reason') or f.get('outcome')}\n\n"
    if kind == "error":
        return f"\n[baton] error: {f.get('error')}\n\n"
    if kind == "finished":
        return f"\n[baton] done: {f.get('reason')} (round {f.get('round')}).\n"
    if kind == "stopped":
        return f"\n[baton] stopped after round {f.get('round')}.\n"
    return ""


def compose_objective(before: list, text: str) -> str:
    """The objective for `text` as the newest message: the conversation first, when there is
    one. It is the whole briefing of an agent with no session of its own."""
    history = history_text(before)
    if not history:
        return text
    return f"Conversation so far (oldest first):\n{history}\n\nLatest message:\n{text}"


# jobs


class _JobStore(Store):
    """A Store that also tells its job about every event, so the stream needs no disk polling."""

    def __init__(self, root: Path, listener: Callable[[str, dict], None]) -> None:
        super().__init__(root)
        self._listener = listener

    def emit(self, kind: str, **fields: object) -> None:
        try:
            self._listener(kind, fields)
        except Exception:
            pass
        super().emit(kind, **fields)


class Job:
    """One session's run of `run_goal`, plus the text it has produced so far."""

    def __init__(
        self,
        session: str,
        name: str,
        workdir: Path,
        goal: Goal,
        adapters_for: Callable[[Goal], Mapping[str, Adapter]],
        context: list[dict],
    ) -> None:
        self.session = session
        self.name = name
        self.workdir = workdir
        self.resolved = workdir.resolve()
        self.goal = goal
        # The conversation up to the newest message handed to this job.
        self.context = context
        self._adapters_for = adapters_for
        self._cond = threading.Condition()
        self._items: list[str] = []
        self._first = 1  # number of _items[0]; item numbers start at 1 and never repeat
        self._closing = False  # the run has said it is over
        self._done = False
        self._delivered = 0
        self._end = ""  # how the last run ended: "finished", "stopped" or ""
        self.store = _JobStore(state_dir(name), self._on_event)
        self._thread = threading.Thread(target=self._run, name=f"baton-{name}", daemon=True)

    # life

    def start(self) -> None:
        self._thread.start()

    def is_running(self) -> bool:
        with self._cond:
            return not self._done

    def join(self, timeout: float) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _run(self) -> None:
        try:
            adapters = self._adapters_for(self.goal)
            for attempt in range(MAX_RESTARTS + 1):
                self._end = ""
                run_goal(self.goal, adapters, self.store, objective=self.objective)
                # The run only finishes with no note left unread, but a message can land in the
                # inbox between that check and the end of the run: start again for it.
                if not self._note_waiting():
                    break
                if attempt == MAX_RESTARTS:
                    self._push("\n[baton] a message was left unread: send it again\n")
                    break
                self._push("\n[baton] a message arrived as the goal finished: continuing\n\n")
                with self._cond:
                    self._closing = False
        except AlreadyRunning as exc:
            self._push(f"\n[baton] cannot start: {exc}\n")
        except Exception as exc:  # one job crashing must never take the server with it
            self._push(f"\n[baton] the job crashed: {type(exc).__name__}: {exc}\n")
        finally:
            with self._cond:
                self._done = True
                self._cond.notify_all()

    def _note_waiting(self) -> bool:
        """True when a run that finished left a note in the inbox. A run that was stopped
        leaves notes alone."""
        if self._end != "finished":
            return False
        try:
            return bool((self.store.root / "inbox.md").read_text(encoding="utf-8").strip())
        except OSError:
            return False

    def objective(self) -> str:
        """The briefing for a fresh round: the conversation so far, including every message
        accepted while the job runs."""
        with self._cond:
            return compose_objective(self.context[:-1], self.context[-1]["content"])

    def request_stop(self, now: bool = True) -> None:
        try:
            self.store.request_stop(now=now)
        except OSError:
            pass

    # feed

    def _push(self, text: str) -> None:
        if not text:
            return
        with self._cond:
            self._items.append(text)
            if len(self._items) > FEED_MAX_ITEMS:
                drop = len(self._items) - FEED_MAX_ITEMS
                del self._items[:drop]
                self._first += drop
            self._cond.notify_all()

    def _on_event(self, kind: str, fields: dict) -> None:
        if kind in ("finished", "stopped"):
            with self._cond:
                self._closing = True
                self._end = kind
        self._push(render_event(kind, fields))

    def add_note(self, text: str) -> bool:
        """Queue a new message for the running job and end the current round, so the next one
        reads it. False when the job is over or about to be."""
        with self._cond:
            if self._done or self._closing:
                return False
            self.store.say(text)
            self.store.request_interrupt()
            self.context.append({"role": "user", "content": text})
        self._push(
            "\n[baton] message received: ending the current round so the next one reads it\n\n"
        )
        return True

    def settle(self, text: str, timeout: float) -> str:
        """For a job that is wrapping up: wait until it takes the note ("accepted", it went
        on with a follow-up) or is over ("gone"). "timeout" when it is still neither."""
        deadline = time.monotonic() + timeout
        while True:
            if self.add_note(text):
                return "accepted"
            if not self.is_running():
                return "gone"
            if time.monotonic() >= deadline:
                return "timeout"
            time.sleep(SETTLE_POLL_S)

    @property
    def delivered(self) -> int:
        """The number of the last item a reader has taken, so a reader that attaches later
        continues where the one that dropped stopped."""
        with self._cond:
            return self._delivered

    def follow(self, after: int = 0) -> Iterator[str]:
        """Text produced after item `after`, as it appears, until the job is over. Yields ""
        when HEARTBEAT_S pass without news. An item counts as delivered once the caller asks
        for the next one, so a write that failed is offered again to the next reader."""
        sent = after
        while True:
            with self._cond:
                heartbeat = False
                while True:
                    last = self._first + len(self._items) - 1
                    if last > sent or self._done:
                        break
                    if not self._cond.wait(timeout=HEARTBEAT_S):
                        heartbeat = True
                        break
                start = max(sent + 1, self._first)
                chunk = "".join(self._items[start - self._first :]) if last >= start else ""
                over = self._done and last <= sent
            if chunk:
                yield chunk
                sent = last
                with self._cond:
                    self._delivered = max(self._delivered, sent)
            elif over:
                return
            elif heartbeat:
                yield ""
            else:
                sent = max(sent, last)


class JobManager:
    def __init__(self, adapters_for: Callable[[Goal], Mapping[str, Adapter]]) -> None:
        self._adapters_for = adapters_for
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._closed = False

    def get(self, session: str) -> Job | None:
        with self._lock:
            return self._jobs.get(session)

    def submit(self, session: str, workdir: Path, messages: list[dict]) -> Job:
        """Deliver the last user message to the session's job: as a note with an interrupt when
        one is running, otherwise as the objective of a new one. At most one job runs in a
        directory. Raises GatewayError or GoalError when nothing can be started."""
        index = last_user_index(messages)
        text = _text_of(messages[index]).strip() if index is not None else ""
        if index is None or not text:
            raise GatewayError("the last message is empty, so there is nothing to work on.")
        while True:
            with self._lock:
                if self._closed:
                    raise GatewayError("baton is shutting down, so it started nothing.")
                old = self._jobs.get(session)
                if old is None or not old.is_running():
                    self._refuse_if_busy(session, workdir)
                    job = self._create(session, workdir, messages[:index], text)
                    self._jobs[session] = job
                    job.start()
                    return job
                if old.add_note(text):
                    return old
            # It is wrapping up and still holds the goal's lock: wait outside our own lock, so
            # other sessions are not held up.
            outcome = old.settle(text, HANDOVER_S)
            if outcome == "accepted":
                return old
            if outcome == "timeout":
                raise GatewayError(
                    "the previous run of this session is still shutting down; send the "
                    "message again in a moment."
                )

    def _refuse_if_busy(self, session: str, workdir: Path) -> None:
        resolved = workdir.resolve()
        for other in self._jobs.values():
            if other.session != session and other.resolved == resolved and other.is_running():
                raise GatewayError(
                    f"a baton job is already running in {workdir} (OpenCode session "
                    f"{other.session}), and two jobs in one directory would commit each "
                    "other's work. Nothing was started for this message. Follow that job in its "
                    f"OpenCode session or in {state_dir(other.name) / 'events.jsonl'}, and send "
                    "this again when it is done."
                )

    def _briefing(self, name: str, workdir: Path, before: list, text: str) -> tuple[Goal, bool]:
        """The goal for `text` as the newest message, and whether an agent has a session of its
        own to resume (its prompt then carries only notes, so `text` must also go in the inbox)."""
        try:
            state = Store(state_dir(name)).load_state()
        except StateError as exc:
            raise GatewayError(str(exc)) from None
        sessions = state.get("sessions")
        resumed = (
            state.get("workdir") == str(workdir) and isinstance(sessions, dict) and bool(sessions)
        )
        objective = compose_objective(before, text)
        return build_goal(name, workdir, objective), resumed

    def _create(self, session: str, workdir: Path, before: list, text: str) -> Job:
        name = job_name(session)
        goal, resumed = self._briefing(name, workdir, before, text)
        context = [*before, {"role": "user", "content": text}]
        job = Job(session, name, workdir, goal, self._adapters_for, context)

        # A stop or interrupt left by something earlier is not about this message.
        job.store.clear_stop()
        job.store.take_interrupt()
        if resumed:
            job.store.say(text)  # the resumed prompt carries no objective, only notes
        return job

    def shutdown(self, timeout: float = SHUTDOWN_S) -> None:
        """Stop every running job, killing its agent's process group, and wait for them."""
        with self._lock:
            self._closed = True  # from here on submit starts nothing
            jobs = list(self._jobs.values())
        running = [j for j in jobs if j.is_running()]
        for job in running:
            job.request_stop(now=True)
        deadline = time.monotonic() + timeout
        for job in running:
            job.join(max(0.0, deadline - time.monotonic()))
        for job in running:
            if job.is_running():
                self._force_kill(job)

    @staticmethod
    def _force_kill(job: Job) -> None:
        try:
            state = job.store.load_state()
            group = state.get("group")
            if (
                isinstance(group, int)
                and not isinstance(group, bool)
                and check_group(group, state.get("group_start")) == OURS
            ):
                kill_group(group)
        except Exception:
            pass


# the server


def _loopback_host(header: str | None) -> bool:
    if not header:
        return False
    host = header.rsplit(":", 1)[0] if not header.endswith("]") else header
    return host in _LOOPBACK_HOSTS


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "baton"
    manager: JobManager  # set on the subclass made by make_server

    def log_message(self, format: str, *args: object) -> None:
        pass

    # plumbing

    def _json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self.close_connection = True  # the request body may be unread
        self._json(status, {"error": {"message": message, "type": "invalid_request_error"}})

    def _guard(self) -> bool:
        # A page in a browser can reach 127.0.0.1 too: refuse any Host that is not loopback
        # (DNS rebinding). Cross-site POSTs are stopped by the custom session header below.
        if _loopback_host(self.headers.get("Host")):
            return True
        self._error(403, "baton only answers on the loopback address")
        return False

    # routes

    def do_GET(self) -> None:
        if not self._guard():
            return
        if self.path.split("?", 1)[0].rstrip("/") == "/v1/models":
            self._json(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": MODEL_ID, "object": "model", "created": 0, "owned_by": "baton"}
                    ],
                },
            )
        else:
            self._error(404, "not found")

    def do_POST(self) -> None:
        if not self._guard():
            return
        if self.path.split("?", 1)[0].rstrip("/") != "/v1/chat/completions":
            self._error(404, "not found")
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._error(411, "Content-Length is required")
            return
        if not 0 <= length <= MAX_BODY_BYTES:
            self._error(413, "request too large")
            return
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except ValueError:
            self._error(400, "the body is not JSON")
            return
        messages = body.get("messages") if isinstance(body, dict) else None
        if not isinstance(messages, list) or not messages:
            self._error(400, "messages must be a non-empty list")
            return
        stream = body.get("stream") is True
        if not body.get("tools"):
            self._reply_text(stream, title_for(messages))
            return
        session = self.headers.get("x-opencode-session", "").strip()
        if not session:
            self._error(400, "x-opencode-session header is required")
            return
        try:
            job = self._job_for(session, messages)
        except (GatewayError, GoalError) as exc:
            self._reply_text(stream, f"[baton] {exc}")
            return
        except Exception as exc:
            problem = f"{type(exc).__name__}: {exc}"
            self._reply_text(stream, f"[baton] could not start the job: {problem}")
            return
        self._reply_job(stream, job)

    def _job_for(self, session: str, messages: list) -> Job:
        manager = self.manager
        existing = manager.get(session)
        if existing is not None and existing.is_running():
            workdir = existing.workdir
        else:
            workdir = session_directory(session)
        return manager.submit(session, workdir, messages)

    # replies

    def _chunk_json(self, cid: str, created: int, delta: dict, finish: str | None) -> bytes:
        payload = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": MODEL_ID,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return b"data: " + json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n\n"

    def _write_chunk(self, data: bytes) -> None:
        self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
        self.wfile.flush()

    def _client_gone(self) -> bool:
        """True when the client has closed its end. A write into a closed socket usually
        succeeds, so without this look the text it swallowed would count as delivered."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            if not readable:
                return False
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    def _begin_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _stream(self, texts: Iterator[str]) -> None:
        cid, created = f"chatcmpl-{uuid.uuid4().hex}", int(time.time())
        try:
            self._begin_sse()
            self._write_chunk(
                self._chunk_json(cid, created, {"role": "assistant", "content": ""}, None)
            )
            for text in texts:
                if self._client_gone():
                    return  # not written, so not delivered: the next reader gets it
                self._write_chunk(self._chunk_json(cid, created, {"content": text}, None))
            self._write_chunk(self._chunk_json(cid, created, {}, "stop"))
            self._write_chunk(b"data: [DONE]\n\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionError, TimeoutError, OSError):
            pass  # the client left; a job it was watching carries on
        finally:
            close = getattr(texts, "close", None)
            if close:
                close()

    def _completion(self, text: str) -> None:
        self._json(
            200,
            {
                "id": f"chatcmpl-{uuid.uuid4().hex}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": MODEL_ID,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            },
        )

    def _reply_text(self, stream: bool, text: str) -> None:
        if stream:
            self._stream(iter([text]))
        else:
            self._completion(text)

    def _reply_job(self, stream: bool, job: Job) -> None:
        texts = job.follow(job.delivered)
        if stream:
            self._stream(texts)
        else:
            try:
                self._completion("".join(texts))
            finally:
                texts.close()


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True


def make_server(
    port: int, adapters_for: Callable[[Goal], Mapping[str, Adapter]]
) -> tuple[ThreadingHTTPServer, JobManager]:
    """A server bound to the loopback address (port 0 picks a free one) and its job manager."""
    manager = JobManager(adapters_for)
    handler = type("Handler", (_Handler,), {"manager": manager})
    return _Server((HOST, port), handler), manager


class _Shutdown:
    """Turns the first SIGTERM, SIGINT or SIGHUP into KeyboardInterrupt in the main thread and
    ignores the rest, so the cleanup that stops the agents cannot be cut short."""

    SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)

    def __init__(self) -> None:
        self._previous: dict[int, object] = {}
        self._fired = False
        self._armed = threading.current_thread() is threading.main_thread()

    def __enter__(self) -> _Shutdown:
        if not self._armed:
            return self
        for sig in self.SIGNALS:
            if sig == signal.SIGHUP and signal.getsignal(sig) == signal.SIG_IGN:
                continue  # `nohup baton serve` starts with SIGHUP ignored on purpose
            self._previous[sig] = signal.signal(sig, self._handle)
        return self

    def __exit__(self, *exc: object) -> None:
        for sig, previous in self._previous.items():
            signal.signal(sig, previous)  # type: ignore[arg-type]

    def _handle(self, signum: int, frame: object) -> None:
        if self._fired:
            return
        self._fired = True
        raise KeyboardInterrupt


def serve(port: int, adapters_for: Callable[[Goal], Mapping[str, Adapter]]) -> None:
    """Serve until SIGTERM or Ctrl-C, then stop every job's agent and return."""
    server, manager = make_server(port, adapters_for)
    host, bound = server.server_address[0], server.server_address[1]
    print(f"baton: gateway listening on http://{host}:{bound}/v1", flush=True)
    with _Shutdown():
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass
        finally:
            manager.shutdown()
            server.server_close()


# opencode.json


def opencode_config(port: int = DEFAULT_PORT) -> dict:
    return {
        "provider": {
            "baton": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "baton",
                "options": {"baseURL": f"http://{HOST}:{port}/v1", "apiKey": "local"},
                "models": {MODEL_ID: {"name": "Auto"}},
            }
        }
    }

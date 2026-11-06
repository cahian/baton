"""The OpenCode gateway: the whole rotation behind one OpenAI-compatible model, `baton/auto`.

One job per OpenCode session runs `run_goal` in a daemon thread of this process. The HTTP
connection only watches the job: dropping it never stops the work. The next request for the same
session attaches to the stream when it carries no new user message; otherwise it interrupts the
running job with a note, or starts a new one. `job.json` lets the next server take a job up again.
"""

from __future__ import annotations

import hashlib
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
from collections.abc import Callable, Iterable, Iterator, Mapping
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

from baton import runner as runner_module
from baton.adapters.base import Adapter
from baton.commands import GOAL, GOAL_OFF, NEXT, Command, parse_command
from baton.config import Goal, GoalError, check_repo_table, goal_from_table
from baton.proc import DEAD, OURS, UNKNOWN, check_group, kill_group
from baton.runner import file_large_note, pin_goal, preempted_sentence, run_goal
from baton.store import (
    AlreadyRunning,
    StateError,
    Store,
    clear_restart,
    overlapping,
    restart_path,
    restart_request,
    state_base,
    state_dir,
    workdir_conflict,
)

HOST = "127.0.0.1"
DEFAULT_PORT = 18777
MODEL_ID = "auto"
# An empty-content delta this often keeps long waits from tripping a client's idle timeout.
HEARTBEAT_S = 15.0
HISTORY_MAX_CHARS = 24000
# A person's message in an over-long history keeps this much of its start: a long spec stays.
PERSON_MAX_CHARS = 8000
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
# While a job runs, OpenCode's inbox is read this often, with this long to wait on a lock.
INBOX_POLL_S = 2.0
# How often the gateway looks for a restart request whose jobs are all held.
RESTART_POLL_S = 1.0
INBOX_DB_TIMEOUT_S = 1.0
# What a note taken from the inbox waits for its echo, the later POST of the same words, before
# it is forgotten (the person may have cancelled that message in OpenCode, which sends nothing).
INBOX_TAKEN_TTL_S = 7 * 24 * 3600.0
INBOX_SEEN_MAX = 500
DELIVERED = "that message was already delivered"
# What an agent is told when the person clears the goal: a resumed one still remembers it.
GOAL_CLEARED_NOTE = "The person cleared the goal."
GATE_SHOWN_CHARS = 1500
NAME_MAX_CHARS = 100
# job.json `status`: anything but RUNNING is left alone when `baton serve` starts.
RUNNING, FINISHED, STOPPED, REFUSED = "running", "finished", "stopped", "refused"

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


def text_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8", errors="replace")).hexdigest()


def message_mark(messages: list, index: int) -> tuple[int, str]:
    """What identifies the user message at `index`: its position among the user messages, and a
    hash of its text. A retry repeats both; the same words said again come at a later position."""
    position = sum(1 for message in messages[:index] if _role(message) == "user")
    return position, text_hash(_text_of(messages[index]))


def _consume_echoes(
    messages: list, accepted: set[tuple[int, str]], taken: dict[str, dict], now: float
) -> tuple[bool, list[tuple[int, str]]]:
    """Match the user messages of a request, newer than what the job knows, against the notes it
    took from OpenCode's inbox: each match uses up one of that text's `n` (an entry is dropped at
    zero, or after INBOX_TAKEN_TTL_S). Changes `taken`. Returns whether the newest user message
    was one of them, which makes the request the echo of a note already delivered, and the marks
    that matched, which the job must now accept."""
    for digest in [h for h, e in taken.items() if now - e["at"] > INBOX_TAKEN_TTL_S]:
        del taken[digest]
    newest = last_user_index(messages)
    echoed = False
    marks: list[tuple[int, str]] = []
    # Newest first: an older message with the same words must not use up the echo's entry.
    for index in range(len(messages) - 1, -1, -1):
        if _role(messages[index]) != "user":
            continue
        mark = message_mark(messages, index)
        entry = taken.get(mark[1])
        if mark in accepted or entry is None:
            continue
        entry["n"] -= 1
        if entry["n"] <= 0:
            del taken[mark[1]]
        marks.append(mark)
        echoed = echoed or index == newest
    marks.reverse()
    return echoed, marks


GAP = "..."


def history_text(messages: list) -> str:
    """The conversation as plain text. Over HISTORY_MAX_CHARS the person's words are kept first,
    newest first, each cut to its first PERSON_MAX_CHARS, so a long reply cannot push out the goal;
    assistant messages share what is left, newest first, the last one cut to its tail. Each place
    where whole messages or the start of a reply were left out holds a `...`."""
    turns: list[tuple[str, str]] = []
    for message in messages:
        role = _role(message)
        if role not in ("user", "assistant"):
            continue  # the system prompt and tool traffic belong to OpenCode's own agent
        text = _text_of(message).strip()
        if role == "user" and parse_command(text) is not None:
            continue  # an old `goal off` or `next` would only contradict the goal now pinned
        if text:
            turns.append((role, text))
    prefix = {"user": "Person: ", "assistant": "Assistant: "}
    joined = "\n\n".join(prefix[role] + text for role, text in turns)
    if len(joined) <= HISTORY_MAX_CHARS:
        return joined
    # What each message costs in the limit: its text and the blank line that joins it.
    keep: dict[int, str] = {}
    left = HISTORY_MAX_CHARS
    for i in range(len(turns) - 1, -1, -1):
        role, text = turns[i]
        if role != "user":
            continue
        if len(text) > PERSON_MAX_CHARS:
            text = text[:PERSON_MAX_CHARS] + " ..."
        if len(text) + 2 > left:
            continue
        keep[i] = text
        left -= len(text) + 2
    for i in range(len(turns) - 1, -1, -1):
        role, text = turns[i]
        if role != "assistant":
            continue
        if len(text) + 2 <= left:
            keep[i] = text
            left -= len(text) + 2
            continue
        if left > 2:
            keep[i] = text[-(left - 2) :]  # the end of a reply is where it got to
        break
    parts: list[str] = []
    gap = False
    for i, (role, text) in enumerate(turns):
        if i not in keep:
            gap = True
            continue
        if gap or (role == "assistant" and keep[i] != text):
            parts.append(GAP)
            gap = False
        parts.append(prefix[role] + keep[i])
    if gap:
        parts.append(GAP)
    return "\n\n".join(parts)


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


def _reasons(event: Mapping) -> str:
    """One line per agent saying why it cannot run, from a `wait` event."""
    agents = event.get("agents")
    lines = []
    for name, info in agents.items() if isinstance(agents, Mapping) else ():
        reason = info.get("reason") if isinstance(info, Mapping) else ""
        lines.append(f"- {name}: {reason or 'it gave no reason'}")
    return "\n".join(lines)


def render_event(kind: str, f: Mapping) -> str:
    """The chat text for one run event, "" for events the person does not need to see."""
    if kind == "output":
        text = f.get("text", "")
        return text if f.get("fragment") else f"{text}\n"
    if kind == "round_start":
        agent, previous, n = f.get("agent"), f.get("previous"), f.get("round")
        # A `rotation` event just said it, with its reason: the line is not shown twice.
        if previous and previous != agent and not f.get("announced") and not f.get("consensus"):
            return f"\n[baton] rotation: {previous} -> {agent} (round {n})\n\n"
        resumed = " (resumed)" if f.get("resumed") else ""
        if f.get("consensus"):  # not a turn in the relay: it only checks the goal
            return f"\n[baton] round {n}: {agent} (consensus)\n\n"
        return f"\n[baton] round {n}: {agent}{resumed}\n\n"
    if kind == "rotation":
        return (
            f"\n[baton] rotation: {f.get('previous')} -> {f.get('agent')} ({f.get('reason')})\n\n"
        )
    if kind == "round_end":
        outcome = f.get("outcome")
        if outcome == "preempted":
            return f"\n[baton] {preempted_sentence(f)}.\n\n"
        if outcome == "completed" and not f.get("interrupted"):
            return ""
        why = f" ({f['reason']})" if f.get("reason") else ""
        how = "interrupted by your message" if f.get("interrupted") else str(outcome)
        return f"\n[baton] round {f.get('round')} ({f.get('agent')}) ended: {how}{why}\n\n"
    if kind == "wait":
        how_long = _duration(f.get("seconds") or 0)
        if f.get("reason"):
            return f"\n[baton] waiting {how_long}: {f['reason']}\n\n"
        return f"\n[baton] no agent is available, waiting {how_long}:\n{_reasons(f)}\n\n"
    if kind == "gate":
        if f.get("passed"):
            return "\n[baton] verify passed\n\n"
        tail = str(f.get("tail") or "").strip()[-GATE_SHOWN_CHARS:]
        shown = f"\n```\n{tail}\n```" if tail else ""
        return f"\n[baton] verify did not pass{shown}\n\n"
    if kind == "phase":
        cycle = f", review cycle {f['cycle']}" if f.get("cycle") else ""
        return f"\n[baton] phase: {f.get('phase')}{cycle}: {f.get('reason')}\n\n"
    if kind == "verify_unrunnable":
        return f"\n[baton] warning: {f.get('message')}\n\n"
    if kind == "notice":
        return f"\n[baton] {f.get('message')}\n\n"
    if kind == "alert":
        return f"\n[baton] alert: {f.get('message') or f.get('reason') or f.get('outcome')}\n\n"
    if kind == "error":
        return f"\n[baton] error: {f.get('error')}\n\n"
    if kind == "finished":
        return f"\n[baton] done: {f.get('reason')} (round {f.get('round')}).\n"
    if kind == "stopped":
        return f"\n[baton] stopped after round {f.get('round')}.\n"
    if kind == "held":
        return (
            "\n[baton] a `baton restart` is waiting: this job holds between rounds and goes on "
            "when baton is back\n\n"
        )
    return ""


def slim_conversation(context: list) -> list[dict]:
    """The part of a conversation that a briefing can use, as plain text: `job.json` is rewritten
    with each message, and requests can carry megabytes of tool traffic. Every message of the
    person stays, each cut to its first HISTORY_MAX_CHARS; assistant text is dropped, the oldest
    first, once it passes the limit."""
    kept: list[dict] = []
    size = 0
    for message in reversed(context):
        role, text = _role(message), _text_of(message)
        if role not in ("user", "assistant") or not text.strip():
            continue
        if role == "user":
            text = text[:HISTORY_MAX_CHARS]
        else:
            if size > HISTORY_MAX_CHARS:
                continue
            size += len(text)
        kept.append({"role": role, "content": text})
    return kept[::-1]


def compose_objective(before: list, text: str) -> str:
    """The objective for `text` as the newest message: the conversation first, when there is
    one. It is the whole briefing of an agent with no session of its own."""
    history = history_text(before)
    if not history:
        return text
    return f"Conversation so far (oldest first):\n{history}\n\nLatest message:\n{text}"


# jobs


def _notes_unread(store: Store) -> bool:
    """True when a note is still waiting for an agent: in the inbox, or already collected into the
    carried notes of state.json. What cannot be read counts as unread, so no note is lost."""
    try:
        return bool(
            store.read_inbox().strip() or str(store.load_state().get("notes") or "").strip()
        )
    except (OSError, StateError):
        return True


class _JobStore(Store):
    """A Store that also tells its job about every event, so the stream needs no disk polling."""

    def __init__(self, root: Path, listener: Callable[[str, dict], None]) -> None:
        super().__init__(root)
        self._listener = listener
        self.halted = False
        self.held = False  # at a point between rounds, with a restart waiting

    def halt(self) -> None:
        """Stop the run now because the server is going away. Held in memory and not in the stop
        file: that file is a person's decision, and this job is to resume on the next start."""
        self.halted = True

    def stop_request(self) -> str:
        return "now" if self.halted else super().stop_request()

    def clear_stop(self) -> None:
        if not self.halted:  # a person's own stop waits for the next start
            super().clear_stop()

    def consume_stop(self) -> None:
        """Nothing yet: until job.json says `stopped`, the file is all that keeps a restart from
        resuming this job. The job clears it after saving that status."""

    def restart_pending(self) -> bool:
        return restart_request() != ""

    def report_held(self, held: bool) -> None:
        self.held = held

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
        accepted: Iterable[tuple[int, str]] = (),
        restarted: bool = False,
        inbox_seen: Iterable[str] = (),
        inbox_taken: Mapping[str, dict] | None = None,
    ) -> None:
        self.session = session
        self.name = name
        self.workdir = workdir
        self.resolved = workdir.resolve()
        self.goal = goal
        # The conversation up to the newest message handed to this job.
        self.context = context
        self.restarted = restarted  # taken up again by a new server, not started by a message
        self._accepted = set(accepted)  # the marks of every user message this session has given
        self._last_accepted: tuple[int, str] | None = None
        # The rows of OpenCode's inbox this job took as notes, by id, and how many of each text
        # (`n`, last taken at `at`) still wait for OpenCode to post them as requests.
        self._inbox_seen: list[str] = list(inbox_seen)[-INBOX_SEEN_MAX:]
        self._inbox_taken: dict[str, dict] = {h: dict(e) for h, e in (inbox_taken or {}).items()}
        self._echoed: set[tuple[int, str]] = set()
        self._status = RUNNING
        self._refused = False
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
        if self.restarted:
            self._push("\n[baton] baton restarted: continuing this job\n\n")
        self._thread.start()

    def is_running(self) -> bool:
        with self._cond:
            return not self._done

    def held(self) -> bool:
        """Between rounds, with a restart of the gateway waiting: nothing is in flight."""
        return self.store.held and self.is_running()

    def join(self, timeout: float) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _run(self) -> None:
        try:
            adapters = self._adapters_for(self.goal)
            for attempt in range(MAX_RESTARTS + 1):
                self._end = ""
                run_goal(
                    self.goal,
                    adapters,
                    self.store,
                    objective=self.objective,
                    owes_round=attempt == 0 and not self.restarted,
                )
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
            self._refused = True
            self._forget_last_message()
            self._push(f"\n[baton] cannot start: {exc}\n")
        except Exception as exc:  # one job crashing must never take the server with it
            self._forget_last_message()
            self._push(f"\n[baton] the job crashed: {type(exc).__name__}: {exc}\n")
        finally:
            with self._cond:
                status = self._status = self._final_status()
                if self.save(status) and status == STOPPED:
                    self._clear_stop()  # before _done, so it cannot remove a newer job's stop
                self._done = True
                self._cond.notify_all()

    def _clear_stop(self) -> None:
        try:
            self.store.clear_stop()
        except OSError as exc:
            self._push(f"\n[baton] alert: could not clear the stop request: {exc}\n\n")

    def _forget_last_message(self) -> None:
        """Nothing was done with it, so sending it again is not a retry of something accepted."""
        with self._cond:
            self._accepted.discard(self._last_accepted)

    def _final_status(self) -> str:
        """What the next `baton serve` should do. A run the server cut short, or one that
        crashed, has not finished, and a person's stop is the only stop that lasts."""
        if self._refused and not self.restarted:
            return REFUSED
        if self._end == "finished":
            return FINISHED
        if self._end == "stopped" and not self.store.halted:
            return STOPPED
        return RUNNING

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

    def halt(self) -> None:
        self.store.halt()

    # what survives a restart

    def save(self, status: str | None = None) -> bool:
        """Write job.json, True when it was, with the job's own status unless `status` says one.
        Failing to is no reason to stop working, only to say so."""
        with self._cond:
            status = self._status if status is None else status
            record = {
                "session": self.session,
                "workdir": str(self.workdir),
                "objective": self.objective(),
                "conversation": slim_conversation(self.context),
                "accepted": sorted(self._accepted),
                "inbox_seen": list(self._inbox_seen),
                "inbox_taken": {h: dict(e) for h, e in self._inbox_taken.items()},
                "status": status,
            }
            try:
                self.store.save_job(record)
            except (OSError, ValueError) as exc:
                self._push(f"\n[baton] alert: could not save this job: {exc}\n\n")
                return False
            return True

    def knows(self, mark: tuple[int, str]) -> bool:
        with self._cond:
            return mark in self._accepted

    def inbox_state(self) -> tuple[list[str], dict[str, dict]]:
        """What a later job of this session must know of OpenCode's inbox."""
        with self._cond:
            return list(self._inbox_seen), {h: dict(e) for h, e in self._inbox_taken.items()}

    def accepted(self) -> set[tuple[int, str]]:
        with self._cond:
            return set(self._accepted)

    def accept(self, mark: tuple[int, str]) -> None:
        with self._cond:
            self._accepted.add(mark)
            self._last_accepted = mark
            self.save()

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

    def add_note(
        self,
        text: str,
        mark: tuple[int, str] | None,
        row: str | None = None,
        original: str | None = None,
    ) -> bool:
        """Queue a new message for the running job and end the current round once the tool in
        flight is done, so the next one reads it. `row` is the id of the OpenCode inbox row it
        came from, in place of a request's `mark`; `original` is the words OpenCode will post
        again when they differ from the note's (a goal command's note is only its goal). A note
        over NOTE_INLINE_CHARS goes to a file and only its start is kept, so no prompt carries it
        whole. False when the job is over or about to be."""
        with self._cond:
            if self._done or self._closing:
                return False
            short, _ = file_large_note(self.workdir, text, time.time())
            self.store.say(short)
            self.context.append({"role": "user", "content": short})
            # Right after the note is safe: nothing later may make it be taken again.
            self._took_row(text if original is None else original, row)
            try:
                self.store.request_interrupt()
            except OSError:
                pass  # the note is in the inbox: the next round reads it
            self._accept_or_save(mark)
        self._push(
            "\n[baton] message received: it reaches the agent after the current tool call or in "
            "the next round\n\n"
        )
        return True

    def _took_row(self, original: str, row: str | None) -> None:
        """Remember an inbox row as taken, and its words as waiting for their echo. Called with
        the lock held."""
        if row is None:
            return
        self._inbox_seen = [*self._inbox_seen, row][-INBOX_SEEN_MAX:]
        entry = self._inbox_taken.setdefault(text_hash(original), {"n": 0, "at": 0.0})
        entry.update(n=entry["n"] + 1, at=time.time())

    def _accept_or_save(self, mark: tuple[int, str] | None) -> None:
        if mark is not None:
            self.accept(mark)
        else:
            self.save()

    def deliver(self, text: str, mark: tuple[int, str] | None, row: str | None = None) -> bool:
        """Hand the person's message to the job: a `baton:` command is carried out, anything else
        becomes a note. The one door for a request and for an inbox row, so both are parsed alike.
        False when the job is over or about to be."""
        command = parse_command(text)
        if command is None:
            return self.add_note(text, mark, row)
        return self.command(command, text, mark, row)

    def command(
        self, command: Command, original: str, mark: tuple[int, str] | None, row: str | None = None
    ) -> bool:
        """Carry out a command on the running job. A goal and `goal off` are also notes, so the
        round in flight ends and the next one reads them (a resumed agent is told that the goal
        changed); `next` is no note. False when the job is over or about to be."""
        with self._cond:
            if self._done or self._closing:
                return False
            if command.kind == GOAL:
                refusal = runner_module.goal_refusal(self.goal)
                if refusal:
                    self._push(f"\n[baton] {refusal}\n\n")
                    self._took_row(original, row)
                    self._accept_or_save(mark)
                    return True
                # A big goal is filed once, and the note says no more than what is pinned.
                short = pin_goal(self.store, self.workdir, command.text, time.time())
                self._push("\n[baton] goal set\n\n")
                return self.add_note(short, mark, row, original)
            if command.kind == GOAL_OFF:
                self.store.clear_goal()
                self._push("\n[baton] goal cleared\n\n")
                return self.add_note(GOAL_CLEARED_NOTE, mark, row, original)
            if command.kind == NEXT:
                self.store.request_next(command.now)
                if command.now:
                    try:
                        self.store.request_interrupt()
                    except OSError:
                        pass  # the request is stored: the round that ends next takes it
                    self._push(
                        "\n[baton] next now requested: it moves on after the current tool call\n\n"
                    )
                else:
                    self._push("\n[baton] next requested: it moves on when this round ends\n\n")
            self._took_row(original, row)
            self._accept_or_save(mark)
            return True

    def take_row(self, row_id: str, text: str) -> bool:
        """A message OpenCode holds in its inbox for this session, delivered like a request: a
        command is carried out and anything else becomes a note. False when this row was taken
        before, or the job cannot take it."""
        with self._cond:
            if row_id in self._inbox_seen:
                return False
            return self.deliver(text.strip(), None, row_id)

    def echo(self, messages: list) -> bool:
        """Account for a request's messages against the notes taken from OpenCode's inbox. True
        when the newest one is the echo of a note already delivered: the request then says
        nothing new, and its mark is accepted. Earlier echoes in the same request, as `promote`
        posts all queued messages at once, are accepted too."""
        with self._cond:
            newest, marks = _consume_echoes(
                messages, self._accepted, self._inbox_taken, time.time()
            )
            if marks:
                self._accepted.update(marks)
                if newest:
                    self._echoed.add(marks[-1])
                self.save()
            return newest

    def finished(self) -> bool:
        """True for a job that ended with its goal met: nothing it was told is left unread. A job
        that was stopped or crashed may still hold notes no agent has read."""
        with self._cond:
            return self._status == FINISHED

    def echoed(self, mark: tuple[int, str]) -> bool:
        with self._cond:
            return mark in self._echoed

    def settle(self, text: str, mark: tuple[int, str], timeout: float) -> str:
        """For a job that is wrapping up: wait until it takes the note ("accepted", it went
        on with a follow-up) or is over ("gone"). "timeout" when it is still neither."""
        deadline = time.monotonic() + timeout
        while True:
            if self.deliver(text, mark):
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


def _marks(raw: object) -> set[tuple[int, str]]:
    """The accepted-message marks of a job.json, which holds them as lists."""
    return {
        (mark[0], mark[1])
        for mark in (raw if isinstance(raw, list) else ())
        if isinstance(mark, list) and len(mark) == 2
    }


def _inbox_seen_ids(raw: object) -> list[str]:
    return [e for e in (raw if isinstance(raw, list) else ()) if isinstance(e, str)]


def _taken(raw: object) -> dict[str, dict]:
    """The notes a job.json says were taken from OpenCode's inbox: a count and a time per hash."""
    out: dict[str, dict] = {}
    for digest, entry in raw.items() if isinstance(raw, dict) else ():
        n, at = (entry.get("n"), entry.get("at")) if isinstance(entry, dict) else (None, None)
        if (
            isinstance(digest, str)
            and isinstance(n, int)
            and not isinstance(n, bool)
            and n > 0
            and isinstance(at, int | float)
            and not isinstance(at, bool)
        ):
            out[digest] = {"n": n, "at": float(at)}
    return out


def _inbox_text(payload: object) -> str:
    """The text of an inbox row's JSON payload, "" when it has none."""
    try:
        data = json.loads(payload) if isinstance(payload, str | bytes) else None
    except ValueError:
        return ""
    text = data.get("text") if isinstance(data, dict) else None
    # Through the same `_unquote` as the text of the request that echoes it.
    return _unquote(text).strip() if isinstance(text, str) else ""


class JobManager:
    def __init__(self, adapters_for: Callable[[Goal], Mapping[str, Adapter]]) -> None:
        self._adapters_for = adapters_for
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._closed = False
        # Echoes of a note that a job which did not finish had taken: (session, mark) of the
        # request that starts the next job, which must not say the note again.
        self._echoes: set[tuple[str, tuple[int, str]]] = set()
        self._poller: threading.Thread | None = None
        self._poller_stop = threading.Event()
        self._restarter: threading.Thread | None = None
        self._restart_unclearable = False  # said once

    # a restart asked for with `baton restart`

    def forget_restart(self) -> None:
        """Drop a request left by the last run, before its jobs are taken up again. A file that
        cannot be removed is said once in the log and never stops `serve`."""
        try:
            clear_restart()
        except OSError as exc:
            print(
                f"baton: could not remove the restart request {restart_path()}: {exc}", flush=True
            )

    def watch_restart(self) -> None:
        """Start the thread that acts on restart requests, once the jobs are up again."""
        self._restarter = threading.Thread(
            target=self._restart_loop, name="baton-restart", daemon=True
        )
        self._restarter.start()

    def _restart_loop(self) -> None:
        while not self._poller_stop.wait(RESTART_POLL_S):
            try:
                if self.restart_tick():
                    return
            except Exception:
                pass  # looked at again on the next tick

    def restart_tick(self) -> bool:
        """Restart the gateway, by sending it the SIGTERM that `launchctl` would, if a restart is
        asked for and every running job is held between rounds (or the request says `now`). The
        request is removed first, so it never outlives the process it was for; one that cannot be
        removed signals nothing, since it would restart the next process too. True when sent."""
        request = restart_request()
        if not request:
            return False
        with self._lock:
            running = [job for job in self._jobs.values() if job.is_running()]
        if request != "now" and not all(job.held() for job in running):
            return False
        try:
            clear_restart()
        except OSError as exc:
            if not self._restart_unclearable:
                self._restart_unclearable = True
                print(
                    f"baton: restart not done, the request {restart_path()} cannot be removed: "
                    f"{exc}",
                    flush=True,
                )
            return False
        os.kill(os.getpid(), signal.SIGTERM)
        return True

    # OpenCode's inbox

    def _watch_inbox(self) -> None:
        """Start the thread that reads OpenCode's inbox, once. Called with the lock held."""
        if self._poller is None or not self._poller.is_alive():
            self._poller_stop.clear()
            self._poller = threading.Thread(target=self._poll_loop, name="baton-inbox", daemon=True)
            self._poller.start()

    def _poll_loop(self) -> None:
        while not self._poller_stop.wait(INBOX_POLL_S):
            try:
                self.poll_inbox()
            except Exception:  # its schema is not a promised interface: retry on the next tick
                pass

    def poll_inbox(self) -> None:
        """Take the messages typed into OpenCode while a reply streams, which it posts only when
        that reply ends, as notes of the running job. The database is opened read-only, and
        anything wrong with it (no file, no table, a lock) just means nothing was read."""
        with self._lock:
            running = [job for job in self._jobs.values() if job.is_running()]
        db = opencode_db()
        if not running or not db.is_file():
            return
        try:
            conn = sqlite3.connect(
                f"file:{quote(str(db))}?mode=ro", uri=True, timeout=INBOX_DB_TIMEOUT_S
            )
            try:
                for job in running:
                    rows = conn.execute(
                        "select id, payload from session_inbox "
                        "where session_id = ? and type = 'user' order by enqueued_seq, id",
                        (job.session,),
                    ).fetchall()
                    for row_id, payload in rows:
                        text = _inbox_text(payload)
                        if text and isinstance(row_id, str):
                            job.take_row(row_id, text)
            finally:
                conn.close()
        except sqlite3.Error:
            return

    def get(self, session: str) -> Job | None:
        with self._lock:
            return self._jobs.get(session)

    def attach(self, session: str, messages: list) -> Job | None:
        """The session's job when this request brings no new user message, so a client that
        retries reads the stream again and changes nothing. None when the message is new. Raises
        GatewayError for a message that a server before this one already finished with, and for
        the echo of a note already delivered to a job that is over with nothing unread: OpenCode
        posts the messages it queued once its reply ends, and baton took them as notes when they
        were typed. The echo of a note a job never read is not an answer: it starts the next job."""
        index = last_user_index(messages)
        if index is None:
            return None
        mark = message_mark(messages, index)
        with self._lock:
            job = self._jobs.get(session)
            if job is None:
                return self._attach_record(session, messages, mark)
            if job.knows(mark):
                if job.echoed(mark) and not job.is_running():
                    return self._echo_of(session, mark, job)
                return job
            if job.echo(messages):
                return self._echo_of(session, mark, job)
            return None

    def _echo_of(self, session: str, mark: tuple[int, str], job: Job) -> Job | None:
        """What an echo is answered with: the job's stream while it runs, one line when it
        finished or has no unread note left (a person's stop ends the reply too, and the note the
        agent read must not start the job again). None for a job that ended with a note still
        unread: the request goes on to start the next job, without saying the note a second
        time."""
        if job.is_running():
            return job
        if job.finished() or not _notes_unread(job.store):
            raise GatewayError(DELIVERED)
        self._echoes.add((session, mark))
        return None

    def _attach_record(self, session: str, messages: list, mark: tuple[int, str]) -> None:
        """`attach` for a session with no job in memory: only its job.json is left to go by."""
        echoed, delivered = self._echo_record(session, messages)
        if echoed:
            if delivered:
                raise GatewayError(DELIVERED)
            self._echoes.add((session, mark))
            return None
        if mark in self._recorded_marks(session):
            raise GatewayError(
                "this message was already handled, in a job that has ended. Send a new message "
                "to go on."
            )

    def _echo_record(self, session: str, messages: list) -> tuple[bool, bool]:
        """`Job.echo` on a job.json alone, which keeps its status, so a job that finished stays
        finished: whether the newest message is an echo, and whether the answer is that it was
        delivered (the job finished, or no note is left unread). Called with the lock held."""
        store = Store(state_dir(job_name(session)))
        try:
            record = store.load_job()
        except StateError:
            return False, False
        if not record:
            return False, False
        accepted, taken = _marks(record.get("accepted")), _taken(record.get("inbox_taken"))
        newest, marks = _consume_echoes(messages, accepted, taken, time.time())
        if marks:
            record["accepted"] = sorted(accepted | set(marks))
            record["inbox_taken"] = taken
            try:
                store.save_job(record)
            except (OSError, ValueError):
                pass  # the entry is then used up again by the next request, to the same end
        delivered = record.get("status") == FINISHED or not _notes_unread(store)
        return newest, delivered

    @staticmethod
    def _record(session: str) -> dict:
        """The session's job.json. Unreadable counts as none: a message is then taken for new,
        which at worst repeats work, where the opposite would drop it."""
        try:
            return Store(state_dir(job_name(session))).load_job() or {}
        except StateError:
            return {}

    @classmethod
    def _recorded_marks(cls, session: str) -> set[tuple[int, str]]:
        return _marks(cls._record(session).get("accepted"))

    def submit(self, session: str, workdir: Path, messages: list[dict]) -> Job:
        """Deliver the last user message to the session's job: as a note with an interrupt when
        one is running, otherwise as the objective of a new one, and not at all when it was
        delivered before. At most one job runs in a directory. Raises GatewayError or GoalError
        when nothing can be started."""
        index = last_user_index(messages)
        text = _text_of(messages[index]).strip() if index is not None else ""
        if index is None or not text:
            raise GatewayError("the last message is empty, so there is nothing to work on.")
        mark = message_mark(messages, index)
        command = parse_command(text)
        while True:
            with self._lock:
                if self._closed:
                    raise GatewayError("baton is shutting down, so it started nothing.")
                old = self._jobs.get(session)
                # `attach` may have found the echo of a note this job never read: it is what
                # starts the next job, so the mark the echo gave the old one is no answer.
                echo_known = (session, mark) in self._echoes
                if old is not None and old.knows(mark) and not echo_known:
                    return old
                if old is not None and not echo_known and old.echo(messages):
                    following = self._echo_of(session, mark, old)
                    if following is not None:
                        return following
                if old is None:
                    is_echo, delivered = self._echo_record(session, messages)
                    if is_echo and delivered:
                        raise GatewayError(DELIVERED)
                    if is_echo:
                        self._echoes.add((session, mark))
                if old is None or not old.is_running():
                    # The echo of a command row already carried out (the job it ran in has
                    # stopped) never runs it again: it only answers.
                    said = (session, mark) in self._echoes
                    if command is not None and command.kind != GOAL:
                        self._echoes.discard((session, mark))
                        self._answer_command(session, command, mark, old, delivered=said)
                    self._refuse_if_busy(session, workdir)
                    if old is not None:
                        accepted, inbox = old.accepted(), old.inbox_state()
                    else:
                        record = self._record(session)
                        accepted = _marks(record.get("accepted"))
                        inbox = (
                            _inbox_seen_ids(record.get("inbox_seen")),
                            _taken(record.get("inbox_taken")),
                        )
                    self._echoes.discard((session, mark))
                    # A goal is its own objective: the command's words are not work to do. A big
                    # one is filed here, once, and `_create` and the pin both take its start.
                    objective = text
                    if command is not None:
                        objective, _ = file_large_note(workdir, command.text, time.time())
                    if command is not None and not said:
                        # A goal ends in consensus: refused where that cannot be, before a job.
                        refusal = runner_module.goal_refusal(
                            build_goal(job_name(session), workdir, objective)
                        )
                        if refusal:
                            self._save_mark(session, mark, old)
                            raise GatewayError(refusal)
                    job = self._create(
                        session, workdir, messages[:index], objective, accepted, inbox, say=not said
                    )
                    if command is not None and not said:  # an echo's goal was set when it was taken
                        job.store.set_goal(objective)
                        job._push("\n[baton] goal set\n\n")
                    job.accept(mark)
                    self._jobs[session] = job
                    job.start()
                    self._watch_inbox()
                    return job
                if old.deliver(text, mark):
                    return old
            # It is wrapping up and still holds the goal's lock: wait outside our own lock, so
            # other sessions are not held up.
            outcome = old.settle(text, mark, HANDOVER_S)
            if outcome == "accepted":
                return old
            if outcome == "timeout":
                raise GatewayError(
                    "the previous run of this session is still shutting down; send the "
                    "message again in a moment."
                )

    def _answer_command(
        self,
        session: str,
        command: Command,
        mark: tuple[int, str],
        old: Job | None,
        delivered: bool,
    ) -> None:
        """A command with no job running to carry it out: `goal off` still clears the session's
        goal, and `next` has nothing to move on and stores nothing, so it cannot surprise a later
        job. The mark is saved with the job's record, status untouched, so a retry does nothing.
        The echo of a command row (`delivered`) was carried out when the row was taken: it only
        answers. Raises GatewayError, which is how a one-line answer reaches the chat."""
        if delivered:
            raise GatewayError(DELIVERED)
        self._save_mark(session, mark, old)
        if command.kind == GOAL_OFF:
            root = state_dir(job_name(session))
            if root.is_dir():
                Store(root).clear_goal()
            raise GatewayError("goal cleared")
        raise GatewayError("no job is running, so there is nothing to move on")

    def _save_mark(self, session: str, mark: tuple[int, str], old: Job | None) -> None:
        """Accept `mark` in the session's job.json, if it has one, and leave its status alone."""
        if old is not None:
            old.accept(mark)  # saves with the status the job ended with
            return
        root = state_dir(job_name(session))
        if not (root / "job.json").exists():
            return
        store = Store(root)
        try:
            record = store.load_job()
            if record is None:
                return
            record["accepted"] = sorted(_marks(record.get("accepted")) | {mark})
            store.save_job(record)
        except (StateError, OSError, ValueError):
            pass  # a retry then repeats a command that is safe to repeat

    def _refuse_if_busy(self, session: str, workdir: Path) -> None:
        resolved = workdir.resolve()
        for other in self._jobs.values():
            if (
                other.session != session
                and overlapping(other.resolved, resolved)
                and other.is_running()
            ):
                raise GatewayError(
                    f"a baton job is already running in {other.workdir} (OpenCode session "
                    f"{other.session}), and two jobs in one directory would commit each "
                    "other's work. Nothing was started for this message. Follow that job in its "
                    f"OpenCode session or in {state_dir(other.name) / 'events.jsonl'}, and send "
                    "this again when it is done."
                )
        conflict = workdir_conflict(workdir)
        if conflict:
            raise GatewayError(
                f"{conflict}. Nothing was started for this message: send it again when that is "
                "done."
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

    def _create(
        self,
        session: str,
        workdir: Path,
        before: list,
        text: str,
        accepted: Iterable[tuple[int, str]],
        inbox: tuple[list[str], dict[str, dict]] = ([], {}),
        say: bool = True,
    ) -> Job:
        name = job_name(session)
        # A huge message goes to a file, and only its start is in the objective, which travels
        # in a command line.
        text, _ = file_large_note(workdir, text, time.time())
        goal, resumed = self._briefing(name, workdir, before, text)
        context = [*before, {"role": "user", "content": text}]
        job = Job(
            session, name, workdir, goal, self._adapters_for, context, accepted, False, *inbox
        )

        # A stop or interrupt left by something earlier is not about this message.
        job.store.clear_stop()
        job.store.take_interrupt()
        job.store.take_next()  # a `next` meant for an older job is stale
        if resumed and say:
            job.store.say(text)  # the resumed prompt carries no objective, only notes
        return job

    # after a restart

    def recover(self) -> int:
        """Kill every recorded agent group that is verifiably ours and alive, then resume every
        job that had not finished; how many were resumed. For `baton serve` as it starts.

        Each goal is handled under its own lock, so a live runner's agent is never touched: a
        second server, or a `baton run`, simply keeps what it holds. Nothing is signalled unless
        `check_group` proves it ours, and one job that cannot be handled never stops the others.
        """
        base = state_base()
        roots = sorted(
            root
            for root in (base.iterdir() if base.is_dir() else ())
            if root.is_dir() and ((root / "state.json").exists() or (root / "job.json").exists())
        )
        resumed = 0
        for root in roots:
            try:
                resumed += self._recover(root)
            except Exception as exc:
                print(f"baton: {root.name} was not recovered: {exc}", flush=True)
        return resumed

    def _recover(self, root: Path) -> bool:
        store = Store(root)
        try:
            with store.lock():
                self._kill_leftover(store)
        except AlreadyRunning:
            return False
        record = store.load_job()
        if record is None or record.get("status") != RUNNING:
            return False
        try:
            self._resume(root.name, record)
        except (GatewayError, GoalError, OSError) as exc:
            store.emit("alert", message=f"this job could not be resumed: {exc}")
            raise
        return True

    @staticmethod
    def _kill_leftover(store: Store) -> None:
        """End the group a dead run left behind. Called with the goal's lock held."""
        state = store.load_state()
        group, start = state.get("group"), state.get("group_start")
        if group is None:
            return
        found = check_group(group, start)
        if found == OURS:
            kill_group(group)
            store.emit("alert", message=f"stopped process group {group}, left by a previous run")
        elif found == UNKNOWN:
            store.emit(
                "alert",
                message=f"process group {group} from the last run is alive but could not be "
                "verified, so it was not killed",
            )
        if check_group(group, start) == DEAD:
            state.update(group=None, group_start=None)
            store.save_state(state)

    def _resume(self, name: str, record: dict) -> None:
        session, directory = record.get("session"), record.get("workdir")
        conversation = record.get("conversation")
        if (
            not isinstance(session, str)
            or job_name(session) != name
            or not isinstance(directory, str)
            or not isinstance(conversation, list)
            or not conversation
            or not isinstance(conversation[-1], dict)
            or not isinstance(conversation[-1].get("content"), str)
        ):
            raise GatewayError(f"its job.json does not describe job {name}")
        workdir = Path(directory)
        if not workdir.is_dir():
            raise GatewayError(f"its directory does not exist: {workdir}")
        objective = compose_objective(conversation[:-1], conversation[-1]["content"])
        goal = build_goal(name, workdir, objective)
        job = Job(
            session,
            name,
            workdir,
            goal,
            self._adapters_for,
            conversation,
            _marks(record.get("accepted")),
            restarted=True,
            inbox_seen=_inbox_seen_ids(record.get("inbox_seen")),
            inbox_taken=_taken(record.get("inbox_taken")),
        )
        with self._lock:
            if self._closed:
                return
            self._jobs[session] = job
            job.start()
            self._watch_inbox()

    def shutdown(self, timeout: float = SHUTDOWN_S) -> None:
        """Stop every running job, killing its agent's process group, and wait for them. The
        jobs have not finished, so the next `baton serve` resumes them."""
        with self._lock:
            self._closed = True  # from here on submit starts nothing
            jobs = list(self._jobs.values())
        self._poller_stop.set()
        running = [j for j in jobs if j.is_running()]
        for job in running:
            job.halt()
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
        attached = manager.attach(session, messages)
        if attached is not None:
            return attached
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
            manager.forget_restart()
            resumed = manager.recover()
            if resumed:
                print(f"baton: resumed {resumed} unfinished job(s)", flush=True)
            manager.watch_restart()
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

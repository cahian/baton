"""On-disk state for one goal: events, state, round logs, inbox, stop request, lock."""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

# Rotate events.jsonl once it grows past this many bytes.
MAX_EVENTS_BYTES = 50 * 1024 * 1024

_SAFE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class AlreadyRunning(Exception):
    """Another runner holds the lock for this goal."""


class StateError(Exception):
    """state.json exists but cannot be read. The file is left untouched."""


def state_base() -> Path:
    """Where baton keeps state shared by every goal. `BATON_STATE_DIR` overrides
    `~/.local/state/baton`."""
    override = os.environ.get("BATON_STATE_DIR")
    return Path(override).expanduser() if override else Path.home() / ".local/state/baton"


def state_dir(name: str) -> Path:
    """State directory for a goal."""
    if not _SAFE_RE.fullmatch(name):
        raise ValueError(f"unsafe goal name: {name!r}")
    return state_base() / name


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


class Store:
    """Files under one goal's state directory.

    Events are written by a single runner. Each carries a monotonic `seq`, which
    `events(after=seq)` uses as a cursor, and survives rotation.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.root / "rounds").mkdir(exist_ok=True, mode=0o700)
        self._events = self.root / "events.jsonl"
        self._rotated = self.root / "events.jsonl.1"
        self._state = self.root / "state.json"
        self._inbox = self.root / "inbox.md"
        self._stop = self.root / "stop"
        self._interrupt = self.root / "interrupt"
        self._seq = max(self._last_seq(self._events), self._last_seq(self._rotated))

    # events

    @staticmethod
    def _last_seq(path: Path) -> int:
        try:
            with path.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                window = 1 << 16
                while True:
                    start = max(0, size - window)
                    fh.seek(start)
                    lines = fh.read(size - start).split(b"\n")
                    if start > 0:
                        lines = lines[1:]  # first line may be cut
                    for line in reversed(lines):
                        try:
                            return int(json.loads(line)["seq"])
                        except (ValueError, KeyError, TypeError):
                            continue
                    if start == 0:
                        return 0
                    window *= 4
        except FileNotFoundError:
            return 0

    def emit(self, kind: str, **fields: Any) -> None:
        if "ts" in fields or "seq" in fields:
            raise ValueError("'ts' and 'seq' are reserved event fields")
        try:
            if self._events.stat().st_size >= MAX_EVENTS_BYTES:
                os.replace(self._events, self._rotated)
        except FileNotFoundError:
            pass
        self._seq += 1
        record = {"ts": round(time.time(), 3), "seq": self._seq, "kind": kind, **fields}
        line = (json.dumps(record, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        fd = os.open(self._events, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)

    @staticmethod
    def _read_events(path: Path, after: int) -> list[dict]:
        out: list[dict] = []
        try:
            with path.open("rb") as fh:
                for raw in fh:
                    try:
                        rec = json.loads(raw)
                    except ValueError:
                        continue  # partial line from a concurrent write
                    if isinstance(rec, dict) and int(rec.get("seq", 0)) > after:
                        out.append(rec)
        except FileNotFoundError:
            pass
        return out

    def events(self, after: int = 0) -> list[dict]:
        """Events with `seq` greater than `after`, oldest first."""
        current = self._read_events(self._events, after)
        first = int(current[0]["seq"]) if current else self._seq + 1
        if first > after + 1:
            return self._read_events(self._rotated, after) + current
        return current

    # state

    def load_state(self) -> dict:
        """Saved state, or {} when none exists yet.

        Raises StateError when the file exists but cannot be read or parsed. It is never
        moved or reset here: cooldowns and the active group live in it, and any reader
        (including the read-only panel) may call this.
        """
        try:
            text = self._state.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except UnicodeDecodeError as exc:
            raise StateError(f"corrupt state file {self._state}: not valid UTF-8") from exc
        except OSError as exc:
            raise StateError(f"cannot read {self._state}: {exc}") from exc
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise StateError(f"corrupt state file {self._state}: {exc}") from exc
        if not isinstance(data, dict):
            raise StateError(f"corrupt state file {self._state}: not a JSON object")
        return data

    def save_state(self, state: dict) -> None:
        _atomic_write(self._state, json.dumps(state, indent=2, sort_keys=True) + "\n")

    # round logs

    def round_log(self, n: int, agent: str) -> Path:
        if not _SAFE_RE.fullmatch(agent):
            raise ValueError(f"unsafe agent name: {agent!r}")
        rounds = self.root / "rounds"
        rounds.mkdir(exist_ok=True, mode=0o700)
        return rounds / f"{int(n)}-{agent}.log"

    # inbox

    @contextmanager
    def _inbox_lock(self) -> Iterator[None]:
        fd = os.open(self.root / "inbox.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def say(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        with self._inbox_lock(), self._inbox.open("a", encoding="utf-8") as fh:
            fh.write(text + "\n\n")

    def take_inbox(self) -> str:
        """Return the unread notes and clear them."""
        with self._inbox_lock():
            try:
                text = self._inbox.read_text(encoding="utf-8")
            except FileNotFoundError:
                return ""
            self._inbox.unlink(missing_ok=True)
        return text

    # stop

    def request_stop(self, now: bool = False) -> None:
        if not now and self.stop_request() == "now":
            return  # never downgrade
        _atomic_write(self._stop, "now\n" if now else "soon\n")

    def stop_request(self) -> str:
        """ "" when none, "now" when forced, otherwise "soon"."""
        try:
            content = self._stop.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""
        return "now" if content == "now" else "soon"

    def clear_stop(self) -> None:
        self._stop.unlink(missing_ok=True)

    # interrupt

    def request_interrupt(self) -> None:
        """Ask the runner to end the current round now."""
        _atomic_write(self._interrupt, "1\n")

    def take_interrupt(self) -> bool:
        """True, and clears the request, when one was pending."""
        try:
            self._interrupt.unlink()
        except FileNotFoundError:
            return False
        return True

    # lock

    def lock(self) -> AbstractContextManager[None]:
        return self._lock()

    @contextmanager
    def _lock(self) -> Iterator[None]:
        fd = os.open(self.root / "lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise AlreadyRunning(f"another baton runner holds {self.root}") from exc
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
            try:
                yield
            finally:
                try:
                    os.ftruncate(fd, 0)  # a pid left behind would name a runner that is gone
                except OSError:
                    pass
        finally:
            os.close(fd)  # closing releases the flock

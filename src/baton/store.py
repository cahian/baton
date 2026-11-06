"""On-disk state for one goal: events, state, round logs, inbox, stop request, lock."""

from __future__ import annotations

import fcntl
import hashlib
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


class WorkdirBusy(AlreadyRunning):
    """Another runner works in this directory, or in one inside it or around it."""


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


# Directories in use, across every process: one claim file per running goal, flocked by its
# runner for as long as it runs, so a claim ends with the process however that dies.
WORKDIRS_DIR = ".workdirs"


def overlapping(one: Path, other: Path) -> bool:
    """True for the same directory, or one inside the other. Both already resolved."""
    return one == other or one in other.parents or other in one.parents


@contextmanager
def _workdir_registry() -> Iterator[Path]:
    """Hold the one lock under which claims are read and made, so that looking at a claim can
    never collide with a runner taking it. Held for moments only."""
    base = state_base() / WORKDIRS_DIR
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(base / "registry.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield base
    finally:
        os.close(fd)


def _read_claim(path: Path) -> tuple[Path, str]:
    try:
        claim = json.loads(path.read_text(encoding="utf-8"))
        return Path(claim["workdir"]), str(claim["owner"])
    except (OSError, ValueError, KeyError, TypeError):
        return Path("/"), "a runner whose claim cannot be read"  # unknown means busy


def _live_claims(base: Path) -> list[tuple[Path, str]]:
    """The directory and owner of every claim still held. Abandoned ones are removed."""
    live: list[tuple[Path, str]] = []
    for path in base.glob("*.claim"):
        fd = os.open(path, os.O_RDWR)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                live.append(_read_claim(path))
            else:
                path.unlink(missing_ok=True)
        finally:
            os.close(fd)
    return live


def _conflict(base: Path, resolved: Path) -> str | None:
    for other, owner in _live_claims(base):
        if overlapping(resolved, other):
            return f"baton is already working in {other} ({owner})"
    return None


def workdir_conflict(workdir: Path) -> str | None:
    """What keeps a goal from running in `workdir` now, in words, or None."""
    with _workdir_registry() as base:
        return _conflict(base, Path(workdir).resolve())


@contextmanager
def workdir_lock(workdir: Path, owner: str) -> Iterator[None]:
    """Claim `workdir` for one runner, across processes. Raises WorkdirBusy when it, a directory
    inside it or one around it is claimed: two runners there would commit each other's work."""
    resolved = Path(workdir).resolve()
    with _workdir_registry() as base:
        conflict = _conflict(base, resolved)
        if conflict:
            raise WorkdirBusy(conflict)
        name = hashlib.sha256(str(resolved).encode("utf-8", errors="surrogateescape")).hexdigest()
        fd = os.open(base / f"{name}.claim", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(fd, 0)
            claim = {"workdir": str(resolved), "owner": owner}
            os.write(fd, json.dumps(claim, ensure_ascii=False).encode("utf-8"))
        except BaseException:
            os.close(fd)
            raise
    try:
        yield
    finally:
        os.close(fd)  # closing releases the flock; the next look removes the file


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
        self._job = self.root / "job.json"
        self._goal = self.root / "goal.md"
        self._goal_done = self.root / "goal.done.md"
        self._next = self.root / "next"
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

    # job

    def save_job(self, record: dict) -> None:
        """Write what `baton serve` needs to take a job up again after a restart."""
        _atomic_write(self._job, json.dumps(record, ensure_ascii=False) + "\n")

    def load_job(self) -> dict | None:
        """The job record, or None when this is not a job. Raises StateError when unreadable."""
        try:
            data = json.loads(self._job.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise StateError(f"cannot read {self._job}: {exc}") from exc
        if not isinstance(data, dict):
            raise StateError(f"corrupt job file {self._job}: not a JSON object")
        return data

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

    def read_inbox(self) -> str:
        """The unread notes, left in place until `clear_inbox` says they are safe elsewhere."""
        with self._inbox_lock():
            try:
                return self._inbox.read_text(encoding="utf-8")
            except FileNotFoundError:
                return ""

    def clear_inbox(self, read: str) -> None:
        """Delete `read`, as `read_inbox` returned it. A note that arrived since stays."""
        with self._inbox_lock():
            try:
                rest = self._inbox.read_text(encoding="utf-8").removeprefix(read)
            except FileNotFoundError:
                return
            if rest:
                _atomic_write(self._inbox, rest)
            else:
                self._inbox.unlink(missing_ok=True)

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

    def consume_stop(self) -> None:
        """The runner has honoured the stop."""
        self.clear_stop()

    # interrupt

    def request_interrupt(self) -> None:
        """Ask the runner to end the current round, once the tool call in flight is done."""
        _atomic_write(self._interrupt, "1\n")

    def take_interrupt(self) -> bool:
        """True, and clears the request, when one was pending."""
        try:
            self._interrupt.unlink()
        except FileNotFoundError:
            return False
        return True

    # goal

    def set_goal(self, text: str) -> None:
        """Pin the goal the person fixed: every prompt opens with it until they change it."""
        _atomic_write(self._goal, text.strip() + "\n")

    def goal(self) -> str:
        """The pinned goal, "" when none is set (the goal is on while this is not empty)."""
        try:
            return self._goal.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""  # unreadable is no goal, as an unreadable handoff is none

    def clear_goal(self) -> None:
        self._goal.unlink(missing_ok=True)

    def archive_goal(self) -> None:
        """Keep a goal that ended in `goal.done.md` and leave none pinned."""
        try:
            os.replace(self._goal, self._goal_done)
        except FileNotFoundError:
            pass

    # next

    def request_next(self, now: bool = False) -> None:
        """Ask for the next agent in the queue. `now` does not wait for the round to end."""
        if not now and self._next.exists():
            return  # a request already waits, and never downgrade a `now`
        _atomic_write(self._next, "now\n" if now else "soon\n")

    def take_next(self) -> str:
        """The pending request, cleared: "" when none, "now" when forced, otherwise "soon"."""
        try:
            content = self._next.read_text(encoding="utf-8").strip()
            self._next.unlink()
        except FileNotFoundError:
            return ""
        return "now" if content == "now" else "soon"

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

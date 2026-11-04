"""The spend ledger behind the Claude API agent's ceiling.

There is no API that returns the Console credit balance, so the budget is baton's own: a file that
every goal and every process shares, updated under a lock. Nothing in it is a float: money is
`Decimal` in memory and a string on disk.

The rules that keep a round from passing the ceiling:

- A round first *reserves* its budget. Spent plus every live reservation plus the new one may not
  pass the ceiling; the grant shrinks to what is left and below 5 USD there is none.
- A finished round *settles*: its reservation is replaced by the actual cost. A round that never
  settles (a crash, a kill) keeps its whole reservation counted, so a failure overcounts. The
  period rolling does not release a reservation: one still within its `expires_at` (the round's
  time limit plus a margin) belongs to a round that may be running, and counts against the new
  period. Only one past its expiry, whose round is surely dead, is dropped when the period rolls.
- A ledger that cannot be read or understood raises `LedgerError`, and the file is never touched or
  recreated. The only empty ledger is one that has never existed: a lock file with no ledger beside
  it means the ledger was deleted, and that is an error too.
- The ceiling and reset day are written into the file. Goals must agree on them (they are global
  settings), so a goal that disagrees gets a `LedgerError` instead of a different idea of the
  period or the budget.
- Writes are atomic and made under an exclusive `flock` (plus a process-wide lock, since `flock`
  alone is not enough to order threads that share one descriptor table in every platform).
  Reads need no lock, because a write is a rename.
"""

from __future__ import annotations

import calendar
import fcntl
import json
import math
import os
import pwd
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path

__all__ = ["MIN_GRANT_USD", "Ledger", "LedgerError", "ceiling_for", "default_path"]

# Below this, a round is not started and the agent is unavailable until the next period. The
# CLI's `--max-budget-usd` is checked between steps, so a round can overshoot its cap; a grant this
# large keeps the overshoot inside the reserve.
MIN_GRANT_USD = Decimal("5")
# How long a reservation made without an explicit lifetime is considered to belong to a live round.
LIVE_RESERVATION_S = 24 * 3600
_CENTS = Decimal("0.01")
_PROCESS_LOCK = threading.RLock()


class LedgerError(Exception):
    """The ledger cannot be read, understood or written. The file is left as it is."""


def default_path() -> Path:
    """Where the one ledger lives: under the real user home, whatever `HOME` and
    `BATON_STATE_DIR` say, so that no goal can have a ledger of its own. `BATON_LEDGER` is the
    explicit override (for tests)."""
    override = os.environ.get("BATON_LEDGER")
    if override:
        return Path(override).expanduser()
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local/state/baton/claude-spend.json"


def ceiling_for(monthly_budget_usd: Decimal, reserve_pct: int) -> Decimal:
    """The spend ceiling: the monthly budget minus the reserve that absorbs estimation error."""
    return monthly_budget_usd * (100 - reserve_pct) / Decimal(100)


def _money(value: object, what: str) -> Decimal:
    if not isinstance(value, str):
        raise LedgerError(f"{what} is not a string")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise LedgerError(f"{what} is not a number") from None
    if not number.is_finite() or number < 0:
        raise LedgerError(f"{what} is not a non-negative amount")
    return number


def _plain(value: Decimal) -> str:
    return format(value.normalize() if value == value.to_integral_value() else value, "f")


def _reserved(state: dict) -> Decimal:
    return sum((r["usd"] for r in state["reservations"].values()), Decimal(0))


def _amount(value: Decimal, what: str, positive: bool) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError(f"{what} must be a finite, non-negative Decimal")
    if positive and value == 0:
        raise ValueError(f"{what} must be greater than zero")
    return value


class Ledger:
    """Spend shared by every goal. `ceiling_usd` and `reset_day` belong to the caller; the file
    holds only what has been spent in the current period."""

    def __init__(
        self,
        path: Path,
        ceiling_usd: Decimal,
        reset_day: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self.ceiling = ceiling_usd
        self.reset_day = reset_day
        self._clock = clock

    # periods

    def _start_of(self, year: int, month: int) -> datetime:
        day = min(self.reset_day, calendar.monthrange(year, month)[1])
        return datetime(year, month, day, tzinfo=UTC)

    def _period(self) -> tuple[datetime, datetime]:
        """(start, next start) of the period the clock is in. A period starts on `reset_day` at
        00:00 UTC, or on the last day of a month that is shorter than that."""
        now = datetime.fromtimestamp(self._clock(), UTC)
        year, month = now.year, now.month
        start = self._start_of(year, month)
        if start > now:
            year, month = (year - 1, 12) if month == 1 else (year, month - 1)
            start = self._start_of(year, month)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        return start, self._start_of(year, month)

    def period_start(self) -> float:
        """Epoch of the start of the current period."""
        return self._period()[0].timestamp()

    def next_period(self) -> float:
        """Epoch of the start of the next period: when spent, reservations and any exhaustion are
        forgotten."""
        return self._period()[1].timestamp()

    # reading and writing

    def _lock_path(self) -> Path:
        return self.path.with_name(self.path.name + ".lock")

    def _read(self) -> dict | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            if self._lock_path().exists():
                raise LedgerError(
                    f"{self.path.name} is missing but its lock file exists: the ledger was "
                    "deleted. Its spend is unknown, so Claude stays unavailable. Restore the "
                    f"file, or delete {self._lock_path().name} to start a new ledger (this "
                    "forgets the spend so far)"
                ) from None
            return None
        except (OSError, UnicodeDecodeError) as exc:
            raise LedgerError(f"cannot read {self.path.name}: {type(exc).__name__}") from None
        try:
            data = json.loads(raw)
        except ValueError:
            raise LedgerError(f"{self.path.name} is not valid JSON") from None
        if not isinstance(data, dict):
            raise LedgerError(f"{self.path.name} is not an object")
        period = data.get("period")
        try:
            date.fromisoformat(period)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise LedgerError(f"{self.path.name} has no valid period") from None
        reservations = data.get("reservations")
        if not isinstance(reservations, dict):
            raise LedgerError(f"{self.path.name} has no reservations table")
        exhausted = data.get("exhausted", False)
        if not isinstance(exhausted, bool):
            raise LedgerError(f"{self.path.name} has a bad exhausted flag")
        state = {
            "period": period,
            "spent": _money(data.get("spent"), "spent"),
            "reservations": {},
            "exhausted": exhausted,
        }
        for key, item in reservations.items():
            expires = item.get("expires_at") if isinstance(item, dict) else None
            if isinstance(expires, bool) or not isinstance(expires, int | float):
                raise LedgerError(f"{self.path.name} has a reservation without an expiry")
            state["reservations"][str(key)] = {
                "usd": _money(item.get("usd"), "a reservation"),
                "expires_at": float(expires),
            }
        self._check_settings(data)
        return state

    def _check_settings(self, data: dict) -> None:
        stored_ceiling = _money(data.get("ceiling"), "ceiling")
        stored_day = data.get("reset_day")
        if isinstance(stored_day, bool) or not isinstance(stored_day, int):
            raise LedgerError(f"{self.path.name} has no valid reset_day")
        if stored_ceiling != self.ceiling or stored_day != self.reset_day:
            raise LedgerError(
                f"{self.path.name} was created with a ceiling of {_plain(stored_ceiling)} USD and "
                f"reset day {stored_day}, but this goal has {_plain(self.ceiling)} USD and day "
                f"{self.reset_day}. The budget settings are global: monthly_budget_usd, "
                "reserve_pct and budget_reset_day must be the same in "
                "~/.config/baton/defaults.toml for every goal. Put them back, or, for a "
                f"deliberate change, delete {self.path.name} and {self._lock_path().name} "
                "(this forgets the spend so far)"
            )

    def _empty(self) -> dict:
        period = self._period()[0].date().isoformat()
        return {"period": period, "spent": Decimal(0), "reservations": {}, "exhausted": False}

    def _state(self) -> dict:
        """The ledger as of the current period. When the stored period is over, spend and the
        exhausted flag are forgotten, and only reservations young enough to belong to a round that
        may still be running are carried over. A stored period in the future (the clock went back)
        is kept, never discarded."""
        period = self._period()[0].date().isoformat()
        state = self._read()
        if state is None:
            return self._empty()
        if state["period"] < period:
            now = self._clock()
            state = {
                "period": period,
                "spent": Decimal(0),
                "reservations": {
                    k: r for k, r in state["reservations"].items() if r["expires_at"] > now
                },
                "exhausted": False,
            }
        return state

    def _write(self, state: dict) -> None:
        text = json.dumps(
            {
                "ceiling": format(self.ceiling, "f"),
                "reset_day": self.reset_day,
                "period": state["period"],
                "spent": format(state["spent"], "f"),
                "reservations": {
                    k: {"usd": format(r["usd"], "f"), "expires_at": r["expires_at"]}
                    for k, r in state["reservations"].items()
                },
                "exhausted": state["exhausted"],
            },
            indent=2,
            sort_keys=True,
        )
        try:
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".claude-spend.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(text + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass
                raise
        except OSError as exc:
            raise LedgerError(f"cannot write {self.path.name}: {type(exc).__name__}") from None

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold the exclusive lock. The first ever use creates the lock file and, before anyone
        else can look, the ledger beside it (a short lock on the directory orders that): from then
        on a lock file without a ledger can only mean the ledger was deleted."""
        lock_path = self._lock_path()
        dir_fd = lock_fd = -1
        try:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                dir_fd = os.open(self.path.parent, os.O_RDONLY)
                fcntl.flock(dir_fd, fcntl.LOCK_EX)
                try:
                    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
                    created = True
                except FileExistsError:
                    lock_fd = os.open(lock_path, os.O_RDWR)
                    created = False
                if created and not self.path.exists():
                    self._write(self._empty())
            except OSError as exc:
                raise LedgerError(f"cannot lock {self.path.name}: {type(exc).__name__}") from None
            finally:
                if dir_fd >= 0:
                    os.close(dir_fd)  # closing releases the directory lock
                    dir_fd = -1
            with _PROCESS_LOCK:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                yield
        finally:
            if lock_fd >= 0:
                os.close(lock_fd)  # closing releases the flock

    # the interface

    def spent(self, floor: Decimal | None = None) -> Decimal:
        """What counts against the ceiling: spend (at least `floor`, an outside figure that may
        lag but is never allowed to lower it) plus every live reservation."""
        state = self._state()
        base = state["spent"]
        if floor is not None:
            base = max(base, _amount(floor, "floor", positive=False))
        return base + _reserved(state)

    def exhausted(self) -> bool:
        """True when the Console said the credit is gone, until the period rolls."""
        return bool(self._state()["exhausted"])

    def reserve(
        self, usd: Decimal, floor: Decimal | None = None, live_for_s: float = LIVE_RESERVATION_S
    ) -> tuple[str, Decimal] | None:
        """Reserve up to `usd` for one round: (id, granted), or None when less than 5 USD fits
        under the ceiling (or the credit is exhausted). The grant is whole cents, rounded down.

        `live_for_s` is how long the round can run (its time limit plus a margin). Until then the
        reservation survives a period rollover; it is never dropped within its period."""
        _amount(usd, "usd", positive=True)
        if (
            not isinstance(live_for_s, int | float)
            or isinstance(live_for_s, bool)
            or not math.isfinite(live_for_s)
            or live_for_s <= 0
        ):
            raise ValueError("live_for_s must be a positive, finite number of seconds")
        if floor is not None:
            _amount(floor, "floor", positive=False)
        with self._locked():
            state = self._state()
            if state["exhausted"]:
                return None
            base = state["spent"] if floor is None else max(state["spent"], floor)
            committed = base + _reserved(state)
            grant = min(usd, self.ceiling - committed).quantize(_CENTS, rounding=ROUND_DOWN)
            if grant < MIN_GRANT_USD:
                return None
            reservation_id = uuid.uuid4().hex
            state["reservations"][reservation_id] = {
                "usd": grant,
                "expires_at": self._clock() + live_for_s,
            }
            self._write(state)
            return reservation_id, grant

    def settle(self, reservation_id: str, actual_usd: Decimal) -> None:
        """Replace a reservation with what the round really cost. An id that is no longer there
        (the period rolled, say) is charged to the current period anyway: never undercount."""
        _amount(actual_usd, "actual_usd", positive=False)
        with self._locked():
            state = self._state()
            state["reservations"].pop(reservation_id, None)
            state["spent"] += actual_usd
            self._write(state)

    def mark_exhausted(self) -> None:
        """Record that the Console refused for lack of credit: no round until the next period."""
        with self._locked():
            state = self._state()
            state["exhausted"] = True
            self._write(state)

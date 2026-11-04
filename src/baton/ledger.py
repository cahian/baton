"""The spend ledger behind the Claude API agent's ceiling.

There is no API that returns the Console credit balance, so the budget is baton's own: a file that
every goal and every process shares, updated under a lock. Nothing in it is a float: money is
`Decimal` in memory and a string on disk.

The rules that keep a round from passing the ceiling:

- A round first *reserves* its budget. Spent plus every live reservation plus the new one may not
  pass the ceiling; the grant shrinks to what is left and below 1 USD there is none.
- A finished round *settles*: its reservation is replaced by the actual cost. A round that never
  settles (a crash, a kill) keeps its whole reservation counted, so a failure overcounts.
- A ledger that cannot be read or understood raises `LedgerError`, and the file is never touched or
  recreated. A missing file is the only empty ledger.
- Writes are atomic and made under an exclusive `flock` (plus a process-wide lock, since `flock`
  alone is not enough to order threads that share one descriptor table in every platform).
  Reads need no lock, because a write is a rename.
"""

from __future__ import annotations

import calendar
import fcntl
import json
import os
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path

__all__ = ["MIN_GRANT_USD", "Ledger", "LedgerError", "ceiling_for"]

# Below this, a round is not worth starting and the agent is unavailable until the next period.
MIN_GRANT_USD = Decimal("1")
_CENTS = Decimal("0.01")
_PROCESS_LOCK = threading.RLock()


class LedgerError(Exception):
    """The ledger cannot be read, understood or written. The file is left as it is."""


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

    def _read(self) -> dict | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
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
        return {
            "period": period,
            "spent": _money(data.get("spent"), "spent"),
            "reservations": {str(k): _money(v, "a reservation") for k, v in reservations.items()},
            "exhausted": exhausted,
        }

    def _state(self) -> dict:
        """The ledger as of the current period; a stored period that is over reads as empty. A
        stored period in the future (the clock went back) is kept, never discarded."""
        start, _ = self._period()
        period = start.date().isoformat()
        state = self._read()
        if state is None or state["period"] < period:
            return {"period": period, "spent": Decimal(0), "reservations": {}, "exhausted": False}
        return state

    def _write(self, state: dict) -> None:
        text = json.dumps(
            {
                "period": state["period"],
                "spent": format(state["spent"], "f"),
                "reservations": {k: format(v, "f") for k, v in state["reservations"].items()},
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
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock_path = self.path.with_name(self.path.name + ".lock")
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise LedgerError(f"cannot lock {self.path.name}: {type(exc).__name__}") from None
        try:
            with _PROCESS_LOCK:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
        finally:
            os.close(fd)  # closing releases the flock

    # the interface

    def spent(self, floor: Decimal | None = None) -> Decimal:
        """What counts against the ceiling: spend (at least `floor`, an outside figure that may
        lag but is never allowed to lower it) plus every live reservation."""
        state = self._state()
        base = state["spent"]
        if floor is not None:
            base = max(base, _amount(floor, "floor", positive=False))
        return base + sum(state["reservations"].values(), Decimal(0))

    def exhausted(self) -> bool:
        """True when the Console said the credit is gone, until the period rolls."""
        return bool(self._state()["exhausted"])

    def reserve(self, usd: Decimal, floor: Decimal | None = None) -> tuple[str, Decimal] | None:
        """Reserve up to `usd` for one round: (id, granted), or None when less than 1 USD fits
        under the ceiling (or the credit is exhausted). The grant is whole cents, rounded down."""
        _amount(usd, "usd", positive=True)
        if floor is not None:
            _amount(floor, "floor", positive=False)
        with self._locked():
            state = self._state()
            if state["exhausted"]:
                return None
            base = state["spent"] if floor is None else max(state["spent"], floor)
            committed = base + sum(state["reservations"].values(), Decimal(0))
            grant = min(usd, self.ceiling - committed).quantize(_CENTS, rounding=ROUND_DOWN)
            if grant < MIN_GRANT_USD:
                return None
            reservation_id = uuid.uuid4().hex
            state["reservations"][reservation_id] = grant
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

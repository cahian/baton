import json
import os
import stat
import subprocess
import sys
import threading
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from baton.ledger import Ledger, LedgerError, ceiling_for, default_path

CEILING = Decimal("180")


def at(year, month, day, hour=0, minute=0) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp()


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return Clock(at(2026, 11, 4, 12))


@pytest.fixture
def path(tmp_path):
    return tmp_path / "state" / "claude-spend.json"


def make(path, clock, ceiling=CEILING, reset_day=1) -> Ledger:
    return Ledger(path, ceiling, reset_day, clock)


def test_ceiling_is_budget_minus_reserve():
    assert ceiling_for(Decimal("200"), 10) == Decimal("180")
    assert ceiling_for(Decimal("200"), 1) == Decimal("198")
    assert ceiling_for(Decimal("0.10"), 50) == Decimal("0.05")
    assert isinstance(ceiling_for(Decimal("200"), 10), Decimal)


def test_reserve_grants_what_is_asked_and_counts_it(path, clock):
    ledger = make(path, clock)
    rid, granted = ledger.reserve(Decimal("10"))
    assert granted == Decimal("10") and rid
    assert ledger.spent() == Decimal("10")


def test_reserve_shrinks_to_what_is_left(path, clock):
    ledger = make(path, clock)
    rid, granted = ledger.reserve(Decimal("175"))
    ledger.settle(rid, Decimal("175"))
    assert granted == Decimal("175")
    _, granted = ledger.reserve(Decimal("10"))
    assert granted == Decimal("5")


def test_grant_is_rounded_down_to_cents(path, clock):
    ledger = make(path, clock)
    rid, _ = ledger.reserve(Decimal("170"))
    ledger.settle(rid, Decimal("170.9999"))
    _, granted = ledger.reserve(Decimal("10"))
    assert granted == Decimal("9.00")  # 180 - 170.9999 = 9.0001
    assert granted.as_tuple().exponent == -2


def test_refuses_below_five_dollars(path, clock):
    ledger = make(path, clock)
    rid, _ = ledger.reserve(Decimal("10"))
    ledger.settle(rid, Decimal("175.01"))  # 4.99 left
    assert ledger.reserve(Decimal("10")) is None
    assert ledger.spent() == Decimal("175.01")  # a refusal reserves nothing


def test_exactly_five_dollars_left_is_granted(path, clock):
    ledger = make(path, clock)
    rid, _ = ledger.reserve(Decimal("10"))
    ledger.settle(rid, Decimal("175"))
    granted = ledger.reserve(Decimal("10"))
    assert granted is not None and granted[1] == Decimal("5.00")


def test_a_small_request_is_not_granted_either(path, clock):
    assert make(path, clock).reserve(Decimal("4.99")) is None


def test_spent_over_the_ceiling_refuses(path, clock):
    ledger = make(path, clock)
    rid, _ = ledger.reserve(Decimal("10"))
    ledger.settle(rid, Decimal("185"))  # a round may overshoot its cap
    assert ledger.reserve(Decimal("10")) is None
    assert ledger.spent() == Decimal("185")


@pytest.mark.parametrize("bad", [Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity")])
def test_reserve_rejects_nonsense_amounts(path, clock, bad):
    ledger = make(path, clock)
    with pytest.raises(ValueError):
        ledger.reserve(bad)
    assert ledger.spent() == 0


def test_concurrent_reservations_never_pass_ceiling(path, clock):
    ledger = make(path, clock)
    barrier = threading.Barrier(40)
    results = []
    lock = threading.Lock()

    def worker():
        mine = make(path, clock)  # each thread has its own instance, like separate goals
        barrier.wait()
        got = mine.reserve(Decimal("10"))
        with lock:
            results.append(got)

    threads = [threading.Thread(target=worker) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    granted = [g for r in results if r for g in [r[1]]]
    assert sum(granted) <= CEILING
    assert sum(granted) == CEILING and len(granted) == 18
    assert ledger.spent() == CEILING
    assert len({r[0] for r in results if r}) == 18  # ids are unique


def test_concurrent_reservations_never_pass_ceiling_across_processes(path):
    code = (
        "import sys, time\n"
        "from decimal import Decimal\n"
        "from baton.ledger import Ledger\n"
        "ledger = Ledger(__import__('pathlib').Path(sys.argv[1]), Decimal('180'), 1, time.time)\n"
        "got = []\n"
        "for _ in range(12):\n"
        "    r = ledger.reserve(Decimal('10'))\n"
        "    if r: got.append(str(r[1]))\n"
        "print(' '.join(got))\n"
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", code, str(path)], stdout=subprocess.PIPE, text=True)
        for _ in range(6)
    ]
    granted = []
    for p in procs:
        out, _ = p.communicate(timeout=60)
        assert p.returncode == 0
        granted += [Decimal(x) for x in out.split()]
    assert sum(granted) == CEILING and len(granted) == 18
    assert Ledger(path, CEILING, 1).spent() == CEILING


def test_unsettled_reservation_stays_counted_across_restart(path, clock):
    make(path, clock).reserve(Decimal("10"))  # the process dies here
    again = make(path, clock)
    assert again.spent() == Decimal("10")
    _, granted = again.reserve(Decimal("175"))
    assert granted == Decimal("170")


def test_settle_replaces_reservation(path, clock):
    ledger = make(path, clock)
    rid, _ = ledger.reserve(Decimal("10"))
    ledger.settle(rid, Decimal("2.5"))
    assert ledger.spent() == Decimal("2.5")
    rid, _ = ledger.reserve(Decimal("10"))
    assert ledger.spent() == Decimal("12.5")
    ledger.settle(rid, Decimal("12"))  # a round can cost more than its cap: the actual wins
    assert ledger.spent() == Decimal("14.5")


@pytest.mark.parametrize("bad", [Decimal("-0.01"), Decimal("NaN"), Decimal("Infinity")])
def test_settle_rejects_nonsense_and_keeps_the_reservation(path, clock, bad):
    ledger = make(path, clock)
    rid, _ = ledger.reserve(Decimal("10"))
    with pytest.raises(ValueError):
        ledger.settle(rid, bad)
    assert ledger.spent() == Decimal("10")


def test_settle_of_an_unknown_id_is_charged_in_full(path, clock):
    ledger = make(path, clock)
    ledger.settle("not-an-id", Decimal("3"))
    assert ledger.spent() == Decimal("3")  # never undercount


def test_money_is_written_as_strings(path, clock):
    ledger = make(path, clock)
    rid, _ = ledger.reserve(Decimal("10"))
    ledger.settle(rid, Decimal("0.123456789012345678901"))
    raw = json.loads(path.read_text())
    assert raw["spent"] == "0.123456789012345678901"
    assert ledger.spent() == Decimal("0.123456789012345678901")


def test_file_is_private(path, clock):
    make(path, clock).reserve(Decimal("10"))
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700


def document(**changes) -> str:
    base = {
        "ceiling": "180",
        "reset_day": 1,
        "period": "2026-11-01",
        "spent": "0",
        "reservations": {},
        "exhausted": False,
    }
    base.update(changes)
    return json.dumps(base)


def without(key) -> str:
    raw = json.loads(document())
    del raw[key]
    return json.dumps(raw)


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "",
        "[]",
        document(spent=5),
        document(spent="-5"),
        document(spent="NaN"),
        document(reservations=[]),
        document(reservations={"a": "10"}),
        document(reservations={"a": {"usd": "10", "at": 1}}),  # the old shape: "at", no expiry
        document(reservations={"a": {"usd": "x", "expires_at": 1}}),
        document(reservations={"a": {"usd": "10"}}),
        document(reservations={"a": {"usd": "10", "expires_at": True}}),
        document(reservations={"a": {"usd": "10", "expires_at": "soon"}}),
        document(period="yesterday"),
        document(exhausted="yes"),
        document(ceiling=180),
        document(ceiling="-1"),
        document(reset_day="1"),
        document(reset_day=True),
        without("period"),
        without("ceiling"),
        without("reset_day"),
    ],
)
def test_corrupt_ledger_raises_and_keeps_file(path, clock, content):
    path.parent.mkdir(parents=True)
    path.write_text(content)
    ledger = make(path, clock)
    with pytest.raises(LedgerError):
        ledger.spent()
    with pytest.raises(LedgerError):
        ledger.exhausted()
    with pytest.raises(LedgerError):
        ledger.reserve(Decimal("10"))
    with pytest.raises(LedgerError):
        ledger.settle("x", Decimal("1"))
    with pytest.raises(LedgerError):
        ledger.mark_exhausted()
    assert path.read_text() == content


def test_a_directory_in_place_of_the_file_is_an_error(path, clock):
    path.mkdir(parents=True)
    with pytest.raises(LedgerError):
        make(path, clock).spent()


def test_missing_ledger_is_an_empty_one_and_reading_creates_nothing(tmp_path, clock):
    ledger = make(tmp_path / "nope" / "claude-spend.json", clock)
    assert ledger.spent() == 0
    assert not (tmp_path / "nope").exists()


def test_period_rolls_on_reset_day(path, clock):
    clock.now = at(2026, 11, 14, 23, 59)
    ledger = make(path, clock, reset_day=15)
    ledger.settle("x", Decimal("100"))
    ledger.mark_exhausted()
    assert ledger.spent() == Decimal("100")
    assert ledger.next_period() == at(2026, 11, 15)
    clock.now = at(2026, 11, 15)
    assert ledger.spent() == 0  # the old period's spend is gone
    assert not ledger.exhausted()
    assert ledger.next_period() == at(2026, 12, 15)
    _, granted = ledger.reserve(Decimal("10"))
    assert granted == Decimal("10")


def test_a_live_reservation_survives_the_rollover(path, clock):
    clock.now = at(2026, 11, 30, 23, 59)
    ledger = make(path, clock)
    rid, _ = ledger.reserve(Decimal("10"))  # a round is running across midnight
    ledger.settle("x", Decimal("100"))
    clock.now = at(2026, 12, 1, 0, 5)
    assert ledger.spent() == Decimal("10")  # the spend is forgotten, the round is not
    _, granted = ledger.reserve(Decimal("175"))
    assert granted == Decimal("170")  # 180 - 10
    ledger.settle(rid, Decimal("3"))
    assert ledger.spent() == Decimal("170") + Decimal("3")


def test_a_stale_reservation_does_not_survive_the_rollover(path, clock):
    clock.now = at(2026, 11, 29, 12)
    ledger = make(path, clock)
    ledger.reserve(Decimal("10"))  # that round died long ago
    clock.now = at(2026, 12, 1, 0, 5)  # more than a day later
    assert ledger.spent() == 0


def test_period_default_reset_day_is_the_first(path, clock):
    ledger = make(path, clock)
    assert ledger.next_period() == at(2026, 12, 1)
    clock.now = at(2026, 12, 31, 23, 59)
    assert ledger.next_period() == at(2027, 1, 1)


def test_reset_day_31_in_short_month(path, clock):
    ledger = make(path, clock, reset_day=31)
    clock.now = at(2026, 2, 10)
    assert ledger.next_period() == at(2026, 2, 28)
    ledger.settle("x", Decimal("50"))
    clock.now = at(2026, 2, 27, 23, 59)
    assert ledger.spent() == Decimal("50")
    clock.now = at(2026, 2, 28)
    assert ledger.spent() == 0
    assert ledger.next_period() == at(2026, 3, 31)
    clock.now = at(2026, 4, 30, 12)  # April has 30 days
    assert ledger.next_period() == at(2026, 5, 31)
    clock.now = at(2026, 12, 31, 12)
    assert ledger.next_period() == at(2027, 1, 31)


def test_settle_after_the_period_rolled_is_charged_to_the_new_period(path, clock):
    clock.now = at(2026, 11, 30, 23, 59)
    ledger = make(path, clock)
    rid, _ = ledger.reserve(Decimal("10"))
    clock.now = at(2026, 12, 1, 0, 5)
    ledger.settle(rid, Decimal("3"))
    assert ledger.spent() == Decimal("3")  # overcounts the new period, never undercounts
    ledger.settle("gone", Decimal("2"))
    assert ledger.spent() == Decimal("5")


def test_exhausted_blocks_until_the_period_rolls(path, clock):
    ledger = make(path, clock)
    assert not ledger.exhausted()
    ledger.mark_exhausted()
    assert ledger.exhausted() and make(path, clock).exhausted()
    assert ledger.reserve(Decimal("10")) is None
    clock.now = at(2026, 12, 1)
    assert not ledger.exhausted()
    assert ledger.reserve(Decimal("10")) is not None


def test_floor_raises_spend_but_never_lowers_it(path, clock):
    ledger = make(path, clock)
    ledger.settle("x", Decimal("20"))
    assert ledger.spent(floor=Decimal("150")) == Decimal("150")
    assert ledger.spent(floor=Decimal("5")) == Decimal("20")
    _, granted = ledger.reserve(Decimal("10"), floor=Decimal("175"))
    assert granted == Decimal("5")
    assert ledger.spent(floor=Decimal("175")) == Decimal("180")
    assert ledger.reserve(Decimal("10"), floor=Decimal("175")) is None


# --- global settings, and a ledger that must not disappear -------------------------------------


def test_the_ceiling_and_reset_day_are_stored_in_the_file(path, clock):
    make(path, clock, reset_day=15).reserve(Decimal("10"))
    raw = json.loads(path.read_text())
    assert raw["ceiling"] == "180" and raw["reset_day"] == 15


@pytest.mark.parametrize(
    "ceiling, day", [(Decimal("360"), 1), (Decimal("180"), 15), (Decimal("90"), 31)]
)
def test_a_goal_with_other_budget_settings_gets_an_error_not_its_own_period(
    path, clock, ceiling, day
):
    """Day-1 and day-15 goals used to wipe each other's spend. Now the second one is refused."""
    first = make(path, clock)
    rid, _ = first.reserve(Decimal("10"))
    first.settle(rid, Decimal("50"))
    before = path.read_text()
    other = make(path, clock, ceiling=ceiling, reset_day=day)
    for call in (
        lambda: other.spent(),
        lambda: other.exhausted(),
        lambda: other.reserve(Decimal("10")),
        lambda: other.settle("x", Decimal("1")),
        lambda: other.mark_exhausted(),
    ):
        with pytest.raises(LedgerError, match="defaults.toml"):
            call()
    assert path.read_text() == before  # nothing was wiped or rewritten
    assert first.spent() == Decimal("50")


def test_an_equal_ceiling_written_differently_is_the_same_ceiling(path, clock):
    make(path, clock, ceiling=Decimal("180")).reserve(Decimal("10"))
    assert make(path, clock, ceiling=Decimal("180.00")).spent() == Decimal("10")


def test_a_deleted_ledger_is_an_error_not_an_empty_one(path, clock):
    ledger = make(path, clock)
    rid, _ = ledger.reserve(Decimal("10"))
    ledger.settle(rid, Decimal("150"))
    path.unlink()
    for call in (
        lambda: ledger.spent(),
        lambda: ledger.exhausted(),
        lambda: ledger.reserve(Decimal("10")),
        lambda: ledger.settle("x", Decimal("1")),
        lambda: make(path, clock).reserve(Decimal("10")),
    ):
        with pytest.raises(LedgerError, match="deleted"):
            call()
    assert not path.exists()  # and nothing recreated it


def test_the_ledger_exists_as_soon_as_its_lock_does(path, clock):
    ledger = make(path, clock, ceiling=Decimal("3"))  # nothing can ever be granted
    assert ledger.reserve(Decimal("10")) is None
    assert path.exists()
    assert ledger.spent() == 0 and ledger.reserve(Decimal("10")) is None  # and it reads fine


def test_a_lock_file_alone_is_a_deleted_ledger(path, clock):
    path.parent.mkdir(parents=True)
    path.with_name(path.name + ".lock").write_text("")
    with pytest.raises(LedgerError, match="deleted"):
        make(path, clock).spent()


def test_deleting_the_lock_as_well_starts_over(path, clock):
    ledger = make(path, clock)
    ledger.settle("x", Decimal("50"))
    path.unlink()
    path.with_name(path.name + ".lock").unlink()
    assert ledger.spent() == 0  # the documented way out of a deleted ledger


def test_first_use_from_many_threads_is_not_mistaken_for_a_deletion(path, clock):
    barrier = threading.Barrier(12)
    errors = []

    def worker():
        mine = make(path, clock)
        barrier.wait()
        try:
            mine.reserve(Decimal("10"))
        except LedgerError as exc:  # pragma: no cover - the failure being tested
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert make(path, clock).spent() == Decimal("120")


def test_default_path_is_under_the_real_home_and_ignores_the_environment(monkeypatch, tmp_path):
    import pwd

    monkeypatch.delenv("BATON_LEDGER", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.setenv("BATON_STATE_DIR", str(tmp_path / "state"))
    real = Path(pwd.getpwuid(os.getuid()).pw_dir)
    assert default_path() == real / ".local/state/baton/claude-spend.json"


def test_baton_ledger_overrides_the_default_path(monkeypatch, tmp_path):
    monkeypatch.setenv("BATON_LEDGER", str(tmp_path / "elsewhere.json"))
    assert default_path() == tmp_path / "elsewhere.json"


# --- reservations live as long as their round -------------------------------------------------

DAY = 86400


def test_a_reservation_stores_its_expiry(path, clock):
    make(path, clock).reserve(Decimal("10"), live_for_s=3600 + 600)
    (stored,) = json.loads(path.read_text())["reservations"].values()
    assert stored["expires_at"] == clock.now + 4200


@pytest.mark.parametrize("bad", [0, -1, True, "1", float("nan")])
def test_a_reservation_needs_a_positive_lifetime(path, clock, bad):
    with pytest.raises(ValueError):
        make(path, clock).reserve(Decimal("10"), live_for_s=bad)
    assert not path.exists() or make(path, clock).spent() == 0


def test_a_long_round_across_the_rollover_keeps_its_reservation(path, clock):
    """A round allowed three days (and 10 USD) that is still running two days after the month
    turned: 24 hours used to drop it, and the new month could then be granted 180 more."""
    clock.now = at(2026, 11, 30, 23, 59)
    ledger = make(path, clock)
    rid, _ = ledger.reserve(Decimal("10"), live_for_s=3 * DAY + 3600)
    ledger.settle("old", Decimal("100"))
    clock.now = at(2026, 12, 2, 12)  # 36 hours later, round_max not yet reached
    assert ledger.spent() == Decimal("10")  # last month's spend is gone, the live round is not
    _, granted = ledger.reserve(Decimal("175"))
    assert granted == Decimal("170")
    assert ledger.spent() == Decimal("180")  # never 190
    ledger.settle(rid, Decimal("9"))
    assert ledger.spent() == Decimal("179")


def test_a_reservation_survives_several_rollovers_until_its_expiry(path, clock):
    clock.now = at(2026, 11, 20)
    ledger = make(path, clock)
    ledger.reserve(Decimal("10"), live_for_s=50 * DAY)
    clock.now = at(2027, 1, 5)  # two rollovers later, still inside its 50 days
    assert ledger.spent() == Decimal("10")
    clock.now = at(2027, 1, 20)  # past its expiry, and the period has rolled once more
    clock.now = at(2027, 2, 1)
    assert ledger.spent() == 0


def test_an_expired_reservation_is_dropped_only_by_a_rollover(path, clock):
    """Within its period a reservation is never dropped, however old: a round that died may have
    spent the money, and the ledger errs on the side of counting it."""
    clock.now = at(2026, 11, 2)
    ledger = make(path, clock)
    ledger.reserve(Decimal("10"), live_for_s=60)
    clock.now = at(2026, 11, 28)  # long past its expiry, same period
    assert ledger.spent() == Decimal("10")
    clock.now = at(2026, 12, 1)
    assert ledger.spent() == 0


def test_the_expiry_is_exclusive(path, clock):
    clock.now = at(2026, 11, 30, 12)
    ledger = make(path, clock)
    ledger.reserve(Decimal("10"), live_for_s=12 * 3600)  # expires exactly at the rollover
    clock.now = at(2026, 12, 1)
    assert ledger.spent() == 0

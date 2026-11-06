import json
import os
import subprocess
import sys

import pytest

from baton import store as store_mod
from baton.store import (
    AlreadyRunning,
    StateError,
    Store,
    WorkdirBusy,
    state_dir,
    workdir_conflict,
    workdir_lock,
)


@pytest.fixture
def st(tmp_path):
    return Store(tmp_path / "goal")


def test_state_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("BATON_STATE_DIR", str(tmp_path))
    assert state_dir("demo") == tmp_path / "demo"
    monkeypatch.delenv("BATON_STATE_DIR")
    assert state_dir("demo").parts[-4:] == (".local", "state", "baton", "demo")
    with pytest.raises(ValueError):
        state_dir("../escape")


def test_events_roundtrip_and_rotation(st, monkeypatch):
    st.emit("a", x=1)
    st.emit("b", y="é")
    evs = st.events()
    assert [e["kind"] for e in evs] == ["a", "b"]
    assert evs[0]["x"] == 1 and evs[1]["y"] == "é"
    assert all("ts" in e for e in evs)
    assert [e["kind"] for e in st.events(after=evs[0]["seq"])] == ["b"]

    monkeypatch.setattr(store_mod, "MAX_EVENTS_BYTES", 200)
    for i in range(10):
        st.emit("fill", i=i, pad="x" * 50)
    assert (st.root / "events.jsonl.1").exists()
    seqs = [e["seq"] for e in st.events()]
    assert seqs == sorted(seqs) and seqs[-1] == 12
    # the cursor survives rotation and restarts
    again = Store(st.root)
    again.emit("after-restart")
    assert again.events()[-1]["seq"] == 13
    assert [e["kind"] for e in again.events(after=12)] == ["after-restart"]


def test_events_merge_rotated_file(st, monkeypatch):
    for i in range(3):
        st.emit("old", i=i)
    monkeypatch.setattr(store_mod, "MAX_EVENTS_BYTES", (st.root / "events.jsonl").stat().st_size)
    st.emit("new")  # rotates seq 1-3 into events.jsonl.1, current holds seq 4
    monkeypatch.undo()
    st.emit("newer")
    assert (st.root / "events.jsonl.1").exists()
    assert [e["seq"] for e in st.events()] == [1, 2, 3, 4, 5]
    assert [e["seq"] for e in st.events(after=1)] == [2, 3, 4, 5]
    assert [e["seq"] for e in st.events(after=3)] == [4, 5]
    assert st.events(after=5) == []


def test_events_skip_partial_line(st):
    st.emit("ok")
    with (st.root / "events.jsonl").open("a") as fh:
        fh.write('{"seq": 99, "kind": "cut')
    assert [e["kind"] for e in st.events()] == ["ok"]


def test_emit_rejects_reserved_fields(st):
    with pytest.raises(ValueError):
        st.emit("x", ts=1)


def test_state_roundtrip_and_missing(st):
    assert st.load_state() == {}
    st.save_state({"round": 3, "agents": {"codex": {"cooldown_until": 5}}})
    assert st.load_state()["round"] == 3


def test_save_state_atomic(st, monkeypatch):
    st.save_state({"round": 1})

    def boom(*a, **k):
        raise OSError("replace failed")

    monkeypatch.setattr(store_mod.os, "replace", boom)
    with pytest.raises(OSError):
        st.save_state({"round": 2})
    monkeypatch.undo()
    assert st.load_state() == {"round": 1}
    assert [p.name for p in st.root.iterdir() if p.name.endswith(".tmp")] == []


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", ""])
def test_corrupt_state_raises_and_file_is_untouched(st, content):
    path = st.root / "state.json"
    path.write_text(content)
    with pytest.raises(StateError):
        st.load_state()
    assert path.read_text() == content
    assert [p.name for p in st.root.iterdir() if "corrupt" in p.name] == []


def test_non_utf8_state_raises_and_file_is_untouched(st):
    path = st.root / "state.json"
    path.write_bytes(b'{"round": "\xff\xfe"}')
    before = path.read_bytes()
    with pytest.raises(StateError):
        st.load_state()
    assert path.read_bytes() == before


def test_unreadable_state_raises_and_file_is_untouched(st, monkeypatch):
    st.save_state({"round": 5})
    path = st.root / "state.json"
    before = path.read_bytes()
    real = type(path).read_text

    def denied(self, *a, **k):
        if self.name == "state.json":
            raise PermissionError("denied")
        return real(self, *a, **k)

    monkeypatch.setattr(type(path), "read_text", denied)
    with pytest.raises(StateError):
        st.load_state()
    monkeypatch.undo()
    assert path.read_bytes() == before
    assert st.load_state() == {"round": 5}


def test_round_log_path(st):
    p = st.round_log(7, "codex")
    assert p == st.root / "rounds" / "7-codex.log"
    assert p.parent.is_dir()
    with pytest.raises(ValueError):
        st.round_log(1, "../x")


def test_reading_the_inbox_leaves_it(st):
    assert st.read_inbox() == ""
    st.say("first")
    st.say("second")
    text = st.read_inbox()
    assert "first" in text and "second" in text and text.index("first") < text.index("second")
    assert st.read_inbox() == text


def test_clearing_the_inbox_removes_only_what_was_read(st):
    st.say("first")
    text = st.read_inbox()
    st.say("second")
    st.clear_inbox(text)
    assert st.read_inbox() == "second\n\n"
    st.clear_inbox("second\n\n")
    assert not (st.root / "inbox.md").exists()
    st.clear_inbox("second\n\n")  # nothing left is fine


def test_stop_soon_and_now(st):
    assert st.stop_request() == ""
    st.request_stop()
    assert st.stop_request() == "soon"
    st.request_stop(now=True)
    assert st.stop_request() == "now"
    st.request_stop()  # no downgrade
    assert st.stop_request() == "now"
    st.clear_stop()
    assert st.stop_request() == ""
    st.clear_stop()  # idempotent


def test_bare_stop_file_means_soon(st):
    (st.root / "stop").write_text("")
    assert st.stop_request() == "soon"


def test_lock_rejects_second_holder(st):
    with st.lock():
        with pytest.raises(AlreadyRunning):
            with Store(st.root).lock():
                pass
        assert (st.root / "lock").read_text().strip() == str(os.getpid())
    with Store(st.root).lock():  # released
        pass


def test_events_file_is_valid_jsonl(st):
    st.emit("a")
    for line in (st.root / "events.jsonl").read_text().splitlines():
        json.loads(line)


def test_interrupt_is_taken_once(st):
    assert st.take_interrupt() is False
    st.request_interrupt()
    st.request_interrupt()
    assert st.take_interrupt() is True
    assert st.take_interrupt() is False


def test_lock_file_is_emptied_on_release(tmp_path):
    store = Store(tmp_path / "s")
    with store.lock():
        assert (store.root / "lock").read_text().strip() == str(os.getpid())
    assert (store.root / "lock").read_text() == ""


# the claim on a working directory


def test_a_directory_claim_is_refused_twice(tmp_path):
    with workdir_lock(tmp_path, "one"):
        with pytest.raises(WorkdirBusy, match="one"):
            with workdir_lock(tmp_path, "two"):
                pass


def test_a_claim_is_free_again_after_it_is_released(tmp_path):
    with workdir_lock(tmp_path, "one"):
        pass
    with workdir_lock(tmp_path, "two"):
        assert "two" in workdir_conflict(tmp_path)


@pytest.mark.parametrize(("held", "wanted"), [("a", "a/b"), ("a/b", "a"), ("a", "a/b/c/d")])
def test_a_directory_inside_or_around_a_claim_is_busy(tmp_path, held, wanted):
    with workdir_lock(tmp_path / held, "one"):
        with pytest.raises(WorkdirBusy):
            with workdir_lock(tmp_path / wanted, "two"):
                pass


@pytest.mark.parametrize(("held", "wanted"), [("a", "ab"), ("a/x", "a/y"), ("ab", "a")])
def test_neighbouring_directories_do_not_conflict(tmp_path, held, wanted):
    with workdir_lock(tmp_path / held, "one"), workdir_lock(tmp_path / wanted, "two"):
        pass


def test_a_claim_is_by_resolved_path(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    with workdir_lock(tmp_path / "real", "one"):
        with pytest.raises(WorkdirBusy):
            with workdir_lock(tmp_path / "link", "two"):
                pass


def test_a_claim_is_seen_from_another_process(tmp_path):
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from baton.store import workdir_lock\n"
        "with workdir_lock(Path(sys.argv[1]), 'other-process'):\n"
        "    print('held', flush=True)\n"
        "    sys.stdin.read()\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "held"
        assert "other-process" in workdir_conflict(tmp_path)
    finally:
        child.kill()
        child.wait()
        child.stdin.close()
        child.stdout.close()
    assert workdir_conflict(tmp_path) is None  # it died without any cleanup: the claim ended too


def test_abandoned_claims_are_removed(tmp_path):
    with workdir_lock(tmp_path, "one"):
        pass
    workdir_conflict(tmp_path)
    base = store_mod.state_base() / store_mod.WORKDIRS_DIR
    assert list(base.glob("*.claim")) == []


def test_a_claim_that_cannot_be_read_counts_as_busy(tmp_path):
    with workdir_lock(tmp_path, "one"):
        base = store_mod.state_base() / store_mod.WORKDIRS_DIR
        next(base.glob("*.claim")).write_text("{ not json")
        assert workdir_conflict(tmp_path / "anything") is not None


def test_no_goal_until_one_is_set(st):
    assert st.goal() == ""
    st.set_goal("  ship the parser\nand its tests \n")
    assert st.goal() == "ship the parser\nand its tests"
    assert (st.root / "goal.md").read_text(encoding="utf-8").strip() == st.goal()


def test_a_goal_set_again_replaces_the_old_one_atomically(st):
    st.set_goal("one")
    st.set_goal("two")
    assert st.goal() == "two"
    assert [p.name for p in st.root.iterdir() if p.name.startswith(".goal")] == []


def test_a_blank_goal_counts_as_none(st):
    st.set_goal("x")
    (st.root / "goal.md").write_text(" \n\n", encoding="utf-8")
    assert st.goal() == ""


def test_clear_goal_is_safe_with_none(st):
    st.clear_goal()
    st.set_goal("x")
    st.clear_goal()
    assert st.goal() == "" and not (st.root / "goal.md").exists()


def test_archive_goal_moves_it_aside_for_the_record(st):
    st.archive_goal()  # nothing to move
    assert not (st.root / "goal.done.md").exists()
    st.set_goal("done work")
    st.archive_goal()
    assert st.goal() == ""
    assert (st.root / "goal.done.md").read_text(encoding="utf-8").strip() == "done work"
    st.set_goal("second goal")
    st.archive_goal()
    assert (st.root / "goal.done.md").read_text(encoding="utf-8").strip() == "second goal"


def test_next_is_taken_once_and_remembers_now(st):
    assert st.take_next() == ""
    st.request_next(False)
    assert st.take_next() == "soon"
    assert st.take_next() == ""
    st.request_next(True)
    assert st.take_next() == "now"


def test_read_goal_tells_no_goal_from_a_goal_that_cannot_be_read(st):
    assert st.read_goal() == ""  # none set
    st.set_goal("ship it")
    assert st.read_goal() == "ship it"
    st.clear_goal()
    (st.root / "goal.md").mkdir()  # whatever makes it unreadable
    assert st.read_goal() is None
    assert st.goal() == ""  # the old reading, unchanged


def test_next_pending_looks_without_taking(st):
    assert st.next_pending() is False
    st.request_next(False)
    assert st.next_pending() is True and st.next_pending() is True
    assert st.take_next() == "soon" and st.next_pending() is False


def test_a_later_plain_next_does_not_downgrade_a_pending_next_now(st):
    st.request_next(True)
    st.request_next(False)
    assert st.take_next() == "now"
    st.request_next(False)
    st.request_next(True)  # but an upgrade is kept
    assert st.take_next() == "now"


def test_an_unreadable_goal_file_counts_as_no_goal(st):
    (st.root / "goal.md").mkdir()  # reading it is an OSError, not a missing file
    assert st.goal() == ""

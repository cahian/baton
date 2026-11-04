import json
import os

import pytest

from baton import store as store_mod
from baton.store import AlreadyRunning, StateError, Store, state_dir


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


def test_inbox_take_clears(st):
    assert st.take_inbox() == ""
    st.say("first")
    st.say("second")
    text = st.take_inbox()
    assert "first" in text and "second" in text and text.index("first") < text.index("second")
    assert st.take_inbox() == ""


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

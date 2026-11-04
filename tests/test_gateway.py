"""The OpenCode gateway, over a real HTTP server on an ephemeral port with scripted adapters."""

import http.client
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from baton import gateway, runner
from baton.adapters.base import COMPLETED, LIMIT, Availability, RoundResult
from baton.cli import main
from baton.gateway import content_text, make_server
from baton.store import Store, state_dir

SESSION = "ses_abc123"
TOOLS = [{"type": "function", "function": {"name": "bash", "parameters": {}}}]


class Fake:
    """Scripted adapter. A step is `fn(agent, prompt, ctx) -> RoundResult`."""

    def __init__(self, name):
        self.name = name
        self.script = []
        self.prompts = []
        self.sessions = []
        self.workdirs = []
        self.announce = None

    def availability(self):
        return Availability(True)

    def run_round(self, prompt, ctx):
        self.prompts.append(prompt)
        self.sessions.append(ctx.session_id)
        self.workdirs.append(ctx.workdir)
        ctx.on_spawn(1)
        if self.announce:
            ctx.on_session(self.announce)
        step = self.script.pop(0) if self.script else None
        return step(self, prompt, ctx) if step else RoundResult(COMPLETED)

    def render(self, line):
        return line


def say(*lines, then=None):
    def step(agent, prompt, ctx):
        for line in lines:
            ctx.on_output(line)
        return then(agent, prompt, ctx) if then else RoundResult(COMPLETED)

    return step


def wait_event(event, *lines):
    """Say `lines`, wait for `event`, then complete."""

    def step(agent, prompt, ctx):
        for line in lines:
            ctx.on_output(line)
        assert event.wait(20), "the test never released the agent"
        return RoundResult(COMPLETED)

    return step


def until_killed(*lines):
    """Say `lines`, then work until the round is ended (an interrupt or a stop)."""

    def step(agent, prompt, ctx):
        for line in lines:
            ctx.on_output(line)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if ctx.should_kill():
                return RoundResult(COMPLETED)
            time.sleep(0.02)
        raise AssertionError("the round was never ended")

    return step


def wait_for(predicate, seconds=20.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def request(*texts, tools=True, stream=True, history=()):
    """A chat completion body the way OpenCode sends one."""
    messages = [{"role": "system", "content": "You are OpenCode."}, *history]
    messages += [{"role": "user", "content": t} for t in texts]
    body = {"model": "auto", "messages": messages, "stream": stream}
    if tools:
        body["tools"] = TOOLS
    return body


class Stream:
    """A client reading the SSE response."""

    def __init__(self, port, body, session=SESSION, host=None):
        self.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        headers = {"Content-Type": "application/json"}
        if session:
            headers["x-opencode-session"] = session
        if host:
            headers["Host"] = host
        self.conn.request("POST", "/v1/chat/completions", json.dumps(body), headers)
        self.resp = self.conn.getresponse()
        self.chunks: list = []
        self.text = ""

    def next(self):
        """The next event: a parsed chunk, "[DONE]" or None at the end of the stream."""
        while True:
            line = self.resp.readline()
            if not line:
                return None
            line = line.decode().strip()
            if not line:
                continue
            assert line.startswith("data: "), line
            payload = line[len("data: ") :]
            if payload == "[DONE]":
                self.chunks.append("[DONE]")
                return "[DONE]"
            chunk = json.loads(payload)
            self.chunks.append(chunk)
            self.text += chunk["choices"][0]["delta"].get("content") or ""
            return chunk

    def read_until(self, needle):
        while needle not in self.text:
            if self.next() in (None, "[DONE]"):
                raise AssertionError(f"{needle!r} never arrived; got {self.text!r}")
        return self.text

    def read_all(self):
        while self.next() not in (None, "[DONE]"):
            pass
        return self.text

    def close(self):
        self.resp.close()  # the response holds the socket open until it is closed too
        self.conn.close()


class Gateway:
    def __init__(self, port, manager, agents, work):
        self.port = port
        self.manager = manager
        self.agents = agents
        self.work = work

    def stream(self, *texts, **kw):
        session = kw.pop("session", SESSION)
        return Stream(self.port, request(*texts, **kw), session=session)

    def job(self, session=SESSION):
        return self.manager.get(session)

    def events(self, session=SESSION):
        return Store(state_dir(gateway.job_name(session))).events()


def make_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute("create table session_v2 (id text primary key, directory text)")
    conn.executemany("insert into session_v2 values (?, ?)", rows)
    conn.commit()
    conn.close()


@pytest.fixture
def gw(fakes, monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "MIN_ROUND_S", 0)
    db = tmp_path / "opencode.db"
    make_db(db, [(SESSION, str(fakes.work)), ("ses_other", str(fakes.work))])
    monkeypatch.setenv("BATON_OPENCODE_DB", str(db))
    agents = {"claude_api": Fake("claude_api"), "codex": Fake("codex")}

    def adapters_for(goal):  # like build_adapters: only what the goal enables
        return {n: a for n, a in agents.items() if goal.agents[n].enabled}

    server, manager = make_server(0, adapters_for)
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    ).start()
    yield Gateway(server.server_address[1], manager, agents, fakes.work)
    manager.shutdown(timeout=10)
    server.shutdown()
    server.server_close()


def get(port, path, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", path, headers={"Host": host} if host else {})
    resp = conn.getresponse()
    return resp.status, resp.read()


# the surface


def test_models_lists_auto(gw):
    status, body = get(gw.port, "/v1/models")
    assert status == 200
    data = json.loads(body)
    assert [m["id"] for m in data["data"]] == ["auto"]


def test_binds_loopback_only(gw):
    assert gw.manager is not None
    server, _ = make_server(0, lambda goal: {})
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


def test_a_foreign_host_header_is_refused(gw):
    status, _ = get(gw.port, "/v1/models", host="evil.example:80")
    assert status == 403
    s = Stream(gw.port, request("hi"), host="evil.example")
    assert s.resp.status == 403
    assert gw.job() is None


def test_post_needs_the_session_header(gw):
    s = Stream(gw.port, request("hi"), session=None)
    assert s.resp.status == 400


def test_unknown_route_is_404(gw):
    assert get(gw.port, "/v1/nothing")[0] == 404


def test_title_request_calls_no_agent(gw):
    first = "Here is the first message of the conversation:\nfix the login bug in the api server"
    s = Stream(gw.port, request(first, tools=False))
    assert s.read_all() == "fix the login bug in the"
    assert gw.job() is None
    assert gw.agents["claude_api"].prompts == [] and gw.agents["codex"].prompts == []
    assert not state_dir(gateway.job_name(SESSION)).exists()


def test_title_request_without_stream_is_a_plain_completion(gw):
    conn = http.client.HTTPConnection("127.0.0.1", gw.port, timeout=10)
    body = request("rename the helper to something clearer", tools=False, stream=False)
    conn.request("POST", "/v1/chat/completions", json.dumps(body), {"x-opencode-session": "s"})
    data = json.loads(conn.getresponse().read())
    assert data["choices"][0]["message"]["content"] == "rename the helper to something clearer"
    assert data["choices"][0]["finish_reason"] == "stop"


def test_stream_is_valid_sse_and_ends_with_done(gw):
    gw.agents["claude_api"].script = [say("hello from the agent")]
    s = gw.stream("do the thing")
    assert s.resp.status == 200
    assert s.resp.getheader("Content-Type") == "text/event-stream"
    assert s.resp.getheader("Transfer-Encoding") == "chunked"
    s.read_all()
    *chunks, done = s.chunks
    assert done == "[DONE]"
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    assert all(c["object"] == "chat.completion.chunk" and c["model"] == "auto" for c in chunks)
    last = chunks[-1]["choices"][0]
    assert last["finish_reason"] == "stop" and last["delta"] == {}
    assert [c["choices"][0]["finish_reason"] for c in chunks[:-1]] == [None] * (len(chunks) - 1)
    assert "hello from the agent" in s.text


def test_rotation_and_text_reach_stream(gw):
    gw.agents["claude_api"].script = [
        say("claude is working", then=lambda a, p, c: RoundResult(LIMIT, until=time.time() + 3600))
    ]
    gw.agents["codex"].script = [say("codex took over")]
    text = gw.stream("do the thing").read_all()
    assert "claude is working" in text
    assert "rotation: claude_api -> codex" in text
    assert "codex took over" in text
    assert "limit" in text
    assert "[baton] done" in text


def test_no_gate_finishes_on_completed_round(gw):
    text = gw.stream("do the thing").read_all()
    assert len(gw.agents["claude_api"].prompts) == 1
    assert "There is no verify command" in gw.agents["claude_api"].prompts[0]
    assert "[baton] done: round completed" in text
    assert wait_for(lambda: not gw.job().is_running())


def test_baton_toml_gate_holds_stream_open_until_pass(gw):
    (gw.work / "baton.toml").write_text(
        'name = "ignored"\nworkdir = "/nowhere"\nobjective = "ignored"\n'
        'order = ["codex"]\nsnapshot = false\n'
        '[verify]\ncommand = "test -f DONE"\n'
        "[agents.codex]\npoll_s = 1\n"
    )

    def finish(agent, prompt, ctx):
        (gw.work / "DONE").write_text("x")
        return RoundResult(COMPLETED)

    gw.agents["codex"].script = [say("not yet"), say("now", then=finish)]
    s = gw.stream("make DONE exist")
    text = s.read_all()
    assert len(gw.agents["codex"].prompts) == 2
    assert gw.agents["claude_api"].prompts == []  # order in baton.toml
    assert "Make DONE exist".lower() in gw.agents["codex"].prompts[0].lower()
    assert "test -f DONE" in gw.agents["codex"].prompts[0]
    assert text.index("verify did not pass") < text.index("verify passed") < text.index("done")
    assert "ignored" not in gw.agents["codex"].prompts[0]


def test_invalid_baton_toml_is_reported_not_ignored(gw):
    (gw.work / "baton.toml").write_text("[verify]\ntimeout_s = 0\n")
    text = gw.stream("go").read_all()
    assert "timeout_s" in text
    assert gw.agents["claude_api"].prompts == []
    (gw.work / "baton.toml").write_text("this is [not toml")
    assert "invalid" in gw.stream("go").read_all()


def test_baton_toml_cannot_set_a_global_setting(gw):
    (gw.work / "baton.toml").write_text("[agents.claude_api]\nmonthly_budget_usd = 99999\n")
    text = gw.stream("go").read_all()
    assert "global setting" in text and "monthly_budget_usd" in text
    assert gw.agents["claude_api"].prompts == [] and gw.agents["codex"].prompts == []


def test_the_defaults_file_applies_to_a_session_job(gw, tmp_path, monkeypatch):
    defaults = tmp_path / "defaults.toml"
    defaults.write_text("[agents.claude_api]\nenabled = false\n")
    monkeypatch.setenv("BATON_CONFIG", str(defaults))
    text = gw.stream("go").read_all()
    assert gw.agents["claude_api"].prompts == []
    assert len(gw.agents["codex"].prompts) == 1 and "[baton] done" in text


def test_a_broken_defaults_file_is_reported(gw, tmp_path, monkeypatch):
    defaults = tmp_path / "defaults.toml"
    defaults.write_text("[agents.claude_api]\nbogus = 1\n")
    monkeypatch.setenv("BATON_CONFIG", str(defaults))
    assert "bogus" in gw.stream("go").read_all()
    assert gw.agents["claude_api"].prompts == []


# jobs and connections


def test_disconnect_does_not_stop_job(gw):
    release = threading.Event()
    gw.agents["claude_api"].script = [wait_event(release, "first part")]
    s = gw.stream("go")
    s.read_until("first part")
    s.close()
    time.sleep(0.2)
    assert gw.job().is_running()
    release.set()
    assert wait_for(lambda: not gw.job().is_running())
    kinds = [e["kind"] for e in gw.events()]
    assert kinds[-1] == "finished" and "stopped" not in kinds


def test_second_message_interrupts_and_reaches_agent(gw):
    gw.agents["claude_api"].script = [until_killed("working on the first")]
    first = gw.stream("first request")
    first.read_until("working on the first")
    second = gw.stream("second request, changed my mind")
    text = second.read_all()
    prompts = gw.agents["claude_api"].prompts
    assert len(prompts) == 2
    assert "first request" in prompts[0]
    assert "second request, changed my mind" in prompts[1]
    assert "Notes from the person" in prompts[1]
    assert "message received" in text
    ended = [e for e in gw.events() if e["kind"] == "round_end"]
    assert ended[0]["interrupted"] is True
    assert "[baton] done" in text
    first.close()


def test_reattach_after_disconnect_streams_remaining_output(gw):
    go = threading.Event()

    def alpha_then_beta(agent, prompt, ctx):
        ctx.on_output("alpha")
        assert go.wait(20)
        return until_killed("beta")(agent, prompt, ctx)

    gw.agents["claude_api"].script = [alpha_then_beta]
    first = gw.stream("start")
    first.read_until("alpha")
    first.close()
    time.sleep(0.2)
    go.set()
    assert wait_for(lambda: any(e.get("text") == "beta" for e in gw.events()))
    again = gw.stream("keep going")
    text = again.read_all()
    assert "beta" in text and "alpha" not in text
    assert "[baton] done" in text


def _second_session_in(gw, tmp_path, directory, session="ses_second"):
    conn = sqlite3.connect(os.environ["BATON_OPENCODE_DB"])
    conn.execute("insert into session_v2 values (?, ?)", (session, str(directory)))
    conn.commit()
    conn.close()
    return session


def test_one_running_job_per_directory(gw, tmp_path):
    gw.agents["claude_api"].script = [until_killed("busy here")]
    first = gw.stream("first session work")
    first.read_until("busy here")
    # the same directory through a symlink is the same directory
    link = tmp_path / "link"
    link.symlink_to(gw.work)
    other = _second_session_in(gw, tmp_path, link)
    text = gw.stream("second session work", session=other).read_all()
    assert "already running" in text and SESSION in text and "events.jsonl" in text
    assert gw.job(other) is None
    assert len(gw.agents["claude_api"].prompts) == 1
    # a different directory is not in the way
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    free = _second_session_in(gw, tmp_path, elsewhere, "ses_free")
    assert "[baton] done" in gw.stream("third", session=free).read_all()
    # and the directory is free again once the first job is over
    gw.stream("stop that", session=SESSION)  # interrupts the busy round
    assert wait_for(lambda: not gw.job().is_running())
    assert "[baton] done" in gw.stream("second session work", session=other).read_all()
    first.close()


def test_submit_after_shutdown_starts_nothing(gw):
    gw.manager.shutdown(timeout=5)
    text = gw.stream("late message").read_all()
    assert "shutting down" in text
    assert gw.job() is None
    assert gw.agents["claude_api"].prompts == []


def test_a_message_during_a_passing_verify_gets_a_follow_up_run(gw):
    (gw.work / "baton.toml").write_text(
        'order = ["claude_api"]\nsnapshot = false\n[verify]\ncommand = "test -f GATE"\n'
    )

    def open_the_gate_then_work(agent, prompt, ctx):
        (gw.work / "GATE").write_text("x")
        return until_killed("working")(agent, prompt, ctx)

    gw.agents["claude_api"].script = [open_the_gate_then_work]
    first = gw.stream("first request")
    first.read_until("working")
    second = gw.stream("a late correction nobody has read")
    text = second.read_all()
    prompts = gw.agents["claude_api"].prompts
    assert len(prompts) == 2 and "a late correction nobody has read" in prompts[1]
    assert "follow-up" in text and text.count("[baton] done") == 2
    assert "to deliver it" not in text
    assert Store(state_dir(gateway.job_name(SESSION))).take_inbox() == ""
    first.close()


def test_a_message_never_read_is_not_reported_as_delivered(gw):
    # a job that is stopped does not start a follow-up for what it never read
    gw.agents["claude_api"].script = [until_killed("busy")]
    s = gw.stream("go")
    s.read_until("busy")
    job = gw.job()
    job.request_stop(now=True)
    assert wait_for(lambda: not job.is_running())
    assert len(gw.agents["claude_api"].prompts) == 1
    assert "follow-up" not in s.read_all()


def test_other_sessions_are_not_held_up_while_one_hands_over(gw, tmp_path, monkeypatch):
    real = gateway.Job._unread_notes

    def slow(self):
        time.sleep(1.5)
        return real(self)

    monkeypatch.setattr(gateway.Job, "_unread_notes", slow)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    free = _second_session_in(gw, tmp_path, elsewhere, "ses_free")
    gw.stream("one").read_until("[baton] done")  # the job is now wrapping up for 1.5 s
    waiting = threading.Thread(target=lambda: gw.stream("two").read_all(), daemon=True)
    waiting.start()
    time.sleep(0.2)  # the second message of SESSION is now waiting for the handover
    started = time.monotonic()
    gw.stream("quick", session=free).read_until("[baton] round 1")
    assert time.monotonic() - started < 1.0
    waiting.join(20)


def test_a_finished_job_is_replaced_by_a_new_one(gw):
    gw.stream("one").read_all()
    first = gw.job()
    gw.stream("two").read_all()
    assert gw.job() is not first
    assert len(gw.agents["claude_api"].prompts) == 2


def test_heartbeat_during_wait(gw, monkeypatch):
    monkeypatch.setattr(gateway, "HEARTBEAT_S", 0.1)
    release = threading.Event()
    gw.agents["claude_api"].script = [wait_event(release, "quiet now")]
    s = gw.stream("go")
    s.read_until("quiet now")
    beats = 0
    while beats < 3:
        chunk = s.next()
        assert chunk not in (None, "[DONE]")
        delta = chunk["choices"][0]["delta"]
        assert delta == {"content": ""}
        beats += 1
    release.set()
    s.read_all()
    assert s.chunks[-1] == "[DONE]"


def test_fresh_job_includes_history_resumed_job_does_not(gw):
    history = [
        {"role": "user", "content": "earlier question about parsers"},
        {"role": "assistant", "content": "earlier answer about tokens"},
        {"role": "assistant", "content": None, "tool_calls": []},
        {"role": "tool", "content": "tool noise"},
    ]
    agent = gw.agents["claude_api"]
    agent.announce = "vendor-session-1"
    gw.stream("now fix the lexer", history=history).read_all()
    fresh = agent.prompts[0]
    assert "earlier question about parsers" in fresh
    assert "earlier answer about tokens" in fresh
    assert "now fix the lexer" in fresh
    assert "tool noise" not in fresh and "You are OpenCode" not in fresh
    assert agent.sessions[0] is None

    assert wait_for(lambda: not gw.job().is_running())
    history += [{"role": "user", "content": "now fix the lexer"}]
    gw.stream("and add a test", history=history).read_all()
    resumed = agent.prompts[1]
    assert agent.sessions[1] == "vendor-session-1"
    assert "and add a test" in resumed
    assert "earlier question about parsers" not in resumed
    assert "Conversation so far" not in resumed


def test_an_agent_without_a_session_in_a_resumed_job_still_gets_the_history(gw):
    history = [
        {"role": "user", "content": "earlier question about parsers"},
        {"role": "assistant", "content": "earlier answer about tokens"},
    ]
    gw.agents["claude_api"].announce = "vendor-session-1"
    gw.stream("first ask", history=history).read_all()
    assert wait_for(lambda: not gw.job().is_running())
    gw.agents["claude_api"].script = [lambda a, p, c: RoundResult(LIMIT, until=time.time() + 3600)]
    gw.stream("and add a test", history=history).read_all()
    fresh = gw.agents["codex"].prompts[0]
    assert "and add a test" in fresh
    assert "earlier question about parsers" in fresh and "earlier answer about tokens" in fresh


def test_a_stop_left_over_does_not_end_a_new_job(gw):
    store = Store(state_dir(gateway.job_name(SESSION)))
    store.request_stop(now=True)
    store.request_interrupt()
    text = gw.stream("go").read_all()
    assert "[baton] done: round completed" in text
    assert len(gw.agents["claude_api"].prompts) == 1


def test_history_is_capped(gw):
    big = [{"role": "user", "content": "OLDEST " + "x" * 30000}]
    big += [{"role": "assistant", "content": "NEWEST reply"}]
    gw.stream("go on", history=big).read_all()
    prompt = gw.agents["claude_api"].prompts[0]
    assert "NEWEST reply" in prompt and "OLDEST" not in prompt
    assert len(prompt) < gateway.HISTORY_MAX_CHARS + 6000


def test_quoted_string_and_content_parts_are_understood(gw):
    assert content_text('"oi"') == "oi"
    assert content_text("plain") == "plain"
    assert content_text('"unterminated') == '"unterminated'
    parts = [{"type": "text", "text": '"a"'}, {"type": "image_url"}, {"type": "text", "text": "b"}]
    assert content_text(parts) == "a\nb"
    assert content_text(None) == ""
    gw.stream('"hello there"').read_all()
    assert "Objective:\nhello there\n" in gw.agents["claude_api"].prompts[0]
    s = Stream(
        gw.port,
        {
            "messages": [{"role": "user", "content": [{"type": "text", "text": "from parts"}]}],
            "tools": TOOLS,
            "stream": True,
        },
        session="ses_other",
    )
    s.read_all()
    assert "from parts" in gw.agents["claude_api"].prompts[-1]


def test_a_crashing_job_is_reported_and_the_server_keeps_serving(gw):
    server, manager = make_server(0, lambda goal: 1 / 0)
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    ).start()
    try:
        port = server.server_address[1]
        text = Stream(port, request("go")).read_all()
        assert "crashed" in text and "ZeroDivisionError" in text
        assert get(port, "/v1/models")[0] == 200
        assert not manager.get(SESSION).is_running()
    finally:
        server.shutdown()
        server.server_close()


def test_a_session_whose_goal_is_locked_elsewhere_says_so(gw):
    with Store(state_dir(gateway.job_name(SESSION))).lock():
        text = gw.stream("go").read_all()
    assert "cannot start" in text


# the session's directory


def test_workdir_comes_from_opencode_db(gw, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    conn = sqlite3.connect(os.environ["BATON_OPENCODE_DB"])
    conn.execute("insert into session_v2 values ('ses_x', ?)", (str(other),))
    conn.commit()
    conn.close()
    gw.stream("go", session="ses_x").read_all()
    assert gw.agents["claude_api"].workdirs == [other]
    assert (other / ".baton").is_dir()


def test_missing_workdir_returns_clear_error(gw):
    text = gw.stream("go", session="ses_unknown").read_all()
    assert "no directory for session ses_unknown" in text
    assert gw.job("ses_unknown") is None
    assert gw.agents["claude_api"].prompts == []


def test_missing_database_returns_clear_error(gw, monkeypatch, tmp_path):
    monkeypatch.setenv("BATON_OPENCODE_DB", str(tmp_path / "nope.db"))
    assert "cannot find OpenCode's database" in gw.stream("go").read_all()


def test_a_directory_that_is_gone_is_an_error(gw, tmp_path):
    conn = sqlite3.connect(os.environ["BATON_OPENCODE_DB"])
    conn.execute("insert into session_v2 values ('ses_gone', ?)", (str(tmp_path / "gone"),))
    conn.commit()
    conn.close()
    assert "does not exist" in gw.stream("go", session="ses_gone").read_all()


def test_the_database_is_opened_read_only(gw, tmp_path):
    db = tmp_path / "ro.db"
    make_db(db, [("s", str(tmp_path))])
    before = db.read_bytes()
    os.environ["BATON_OPENCODE_DB"] = str(db)
    assert gateway.session_directory("s") == tmp_path
    assert db.read_bytes() == before
    assert not (tmp_path / "ro.db-journal").exists()


def test_job_names_are_safe():
    assert gateway.job_name("ses_ab-1.2") == "session-ses_ab-1.2"
    assert gateway.job_name("../../x y") == "session-.._.._x_y"
    assert len(gateway.job_name("a" * 500)) <= gateway.NAME_MAX_CHARS


# shutdown


def test_shutdown_stops_a_running_job(gw):
    gw.agents["claude_api"].script = [until_killed("busy")]
    s = gw.stream("go")
    s.read_until("busy")
    gw.manager.shutdown(timeout=10)
    assert not gw.job().is_running()
    assert "stopped" in s.read_all()


def test_serve_stops_the_agent_group_on_sigterm(fakes, monkeypatch, tmp_path):
    db = tmp_path / "opencode.db"
    make_db(db, [(SESSION, str(fakes.work))])
    monkeypatch.setenv("BATON_OPENCODE_DB", str(db))
    monkeypatch.setenv("FAKE_CODEX_EXEC_SLEEP", "60")
    (fakes.work / "baton.toml").write_text(
        'order = ["codex"]\nsnapshot = false\n'
        "[agents.antigravity]\nenabled = false\n[agents.claude_api]\nenabled = false\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "baton.cli", "serve", "--port", "0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        line = proc.stdout.readline()
        assert "127.0.0.1" in line, line
        port = int(line.rsplit(":", 1)[1].split("/")[0])
        s = Stream(port, request("work for a long time"))
        assert wait_for(fakes.codex_pidfile.exists)
        pid = int(fakes.codex_pidfile.read_text())
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=30) == 0
        assert wait_for(lambda: not _alive(pid))
        assert "stopped" in s.read_all()
        assert "work for a long time" in fakes.codex_calls()[0]["stdin"]
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        for stream in (proc.stdout, proc.stderr):
            stream.close()


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# opencode.json


def test_opencode_config_output_is_valid_json_with_auto_model(capsys):
    assert main(["opencode-config", "--port", "18777"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data == {
        "provider": {
            "baton": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "baton",
                "options": {"baseURL": "http://127.0.0.1:18777/v1", "apiKey": "local"},
                "models": {"auto": {"name": "Auto"}},
            }
        }
    }


def test_serve_on_a_busy_port_exits_1(capsys):
    import socket

    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        assert main(["serve", "--port", str(busy.getsockname()[1])]) == 1
    assert "cannot serve" in capsys.readouterr().err


def test_opencode_config_follows_the_port(capsys):
    main(["opencode-config", "--port", "4321"])
    data = json.loads(capsys.readouterr().out)
    assert data["provider"]["baton"]["options"]["baseURL"] == "http://127.0.0.1:4321/v1"

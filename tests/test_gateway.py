"""The OpenCode gateway, over a real HTTP server on an ephemeral port with scripted adapters."""

import contextlib
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
from baton.cli import build_adapters, main
from baton.gateway import content_text, make_server
from baton.proc import process_start
from baton.store import Store, state_dir, workdir_lock

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
        step = self.script.pop(0) if self.script else None
        if not getattr(step, "refuses", False):
            ctx.on_spawn(1)  # the process started; a refused start never gets here
        if self.announce:
            ctx.on_session(self.announce)
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


def test_a_message_gets_a_round_even_when_the_gate_already_passes(gw):
    (gw.work / "baton.toml").write_text('[verify]\ncommand = "true"\n')
    gw.agents["claude_api"].script = [say("on it")]
    text = gw.stream("add a test").read_all()
    assert "add a test" in gw.agents["claude_api"].prompts[0]
    assert "on it" in text
    assert "done: verify passed (round 1)" in text


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


@pytest.mark.parametrize(
    "line",
    [
        '[agents.codex]\nbin = "./evil"\n',
        '[agents.antigravity]\nbin = "./evil"\n',
        '[agents.claude_api]\nbin = "./evil"\n',
        '[agents.claude_api]\nkey_file = "./stolen.key"\n',
        '[agents.claude_api]\nadmin_key_file = "./stolen.key"\n',
        "[agents.codex]\nfull_access = true\n",
        "[agents.antigravity]\nfull_access = false\n",
        "[agents.codex]\npoll_s = 3600\n",
        "[agents.antigravity]\npoll_s = 3600\n",
    ],
)
def test_baton_toml_cannot_choose_executables_or_credentials(gw, line):
    (gw.work / "baton.toml").write_text(line)
    text = gw.stream("go").read_all()
    assert "may not be set here" in text
    assert gw.agents["codex"].prompts == [] and gw.agents["claude_api"].prompts == []


def test_the_defaults_file_may_still_set_bin(fakes, monkeypatch):
    fakes.defaults('[agents.codex]\nbin = "/opt/codex"\n')
    goal = gateway.build_goal("session-x", fakes.work, "go")
    assert goal.agents["codex"].bin == "/opt/codex"


@pytest.mark.parametrize(
    ("line", "why"),
    [
        ("[agents.codex]\nthreshold_pct = 91\n", "between 1 and 90"),
        ('[agents.antigravity]\nmodel = "gpt-6"\n', "must start with 'gemini-'"),
    ],
)
def test_baton_toml_obeys_the_same_rules_as_a_goal_file(gw, line, why):
    (gw.work / "baton.toml").write_text(line)
    assert why in gw.stream("go").read_all()


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


def test_a_message_during_a_passing_verify_is_read_before_done(gw):
    (gw.work / "baton.toml").write_text(
        'order = ["claude_api", "codex"]\nsnapshot = false\n[verify]\ncommand = "test -f GATE"\n'
    )

    def open_the_gate_then_work(agent, prompt, ctx):
        (gw.work / "GATE").write_text("x")
        return until_killed("working")(agent, prompt, ctx)

    def refused_start(agent, prompt, ctx):
        return RoundResult(LIMIT, until=time.time() + 3600)

    refused_start.refuses = True
    # claude refuses to start when asked again: the note must reach codex, not be dropped
    gw.agents["claude_api"].script = [open_the_gate_then_work, refused_start]
    first = gw.stream("first request")
    first.read_until("working")
    second = gw.stream("a late correction nobody has read")
    text = second.read_all()
    assert "a late correction nobody has read" in gw.agents["codex"].prompts[0]
    assert text.count("[baton] done") == 1
    assert Store(state_dir(gateway.job_name(SESSION))).read_inbox() == ""
    first.close()


def test_a_message_that_lands_as_the_goal_finishes_restarts_it(gw, monkeypatch):
    real = gateway.Job._on_event
    fired = []

    def late(self, kind, fields):
        real(self, kind, fields)
        if kind == "finished" and not fired:
            fired.append(1)
            self.store.say("a note that arrived too late for the check")

    monkeypatch.setattr(gateway.Job, "_on_event", late)
    text = gw.stream("go").read_all()
    prompts = gw.agents["claude_api"].prompts
    assert len(prompts) == 2
    assert "a note that arrived too late for the check" in prompts[1]
    assert "continuing" in text and text.count("[baton] done") == 2


def test_a_message_never_read_by_a_stopped_job_is_not_run_again(gw):
    gw.agents["claude_api"].script = [until_killed("busy")]
    s = gw.stream("go")
    s.read_until("busy")
    job = gw.job()
    # the stop and the message arrive together: the round ends, the stop wins, the note is unread
    Store(state_dir(gateway.job_name(SESSION))).request_stop(now=True)
    assert job.add_note("a message that no agent will read", (9, "unread"))
    assert wait_for(lambda: not job.is_running())
    assert len(gw.agents["claude_api"].prompts) == 1
    text = s.read_all()
    assert "continuing" not in text and "stopped" in text
    assert "delivered" not in text


def test_a_message_accepted_while_running_is_in_the_briefing_of_a_fresh_agent(gw):
    gw.agents["claude_api"].script = [
        until_killed("busy"),
        lambda a, p, c: RoundResult(LIMIT, until=time.time() + 3600),
    ]
    first = gw.stream("first request")
    first.read_until("busy")
    second = gw.stream("then also handle the edge case")
    second.read_all()
    fresh = gw.agents["codex"].prompts[0]  # no session of its own: only the objective
    assert "then also handle the edge case" in fresh and "first request" in fresh
    first.close()


def test_other_sessions_are_not_held_up_while_one_hands_over(gw, tmp_path, monkeypatch):
    real = gateway.Job._note_waiting
    wrapping_up = threading.Event()
    release = threading.Event()

    def held(self):
        if self.session == SESSION:
            wrapping_up.set()
            assert release.wait(30)
        return real(self)

    monkeypatch.setattr(gateway.Job, "_note_waiting", held)
    monkeypatch.setattr(gateway, "HANDOVER_S", 30.0)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    free = _second_session_in(gw, tmp_path, elsewhere, "ses_free")
    first = gw.stream("one")
    assert wrapping_up.wait(10)  # the job has finished its run and is held in its wrap-up
    waiting = threading.Thread(target=lambda: gw.stream("two").read_all(), daemon=True)
    waiting.start()
    quick_done = threading.Event()

    def quick():
        gw.stream("quick", session=free).read_until("[baton] round 1")
        quick_done.set()

    threading.Thread(target=quick, daemon=True).start()
    try:
        # SESSION's second message sits in its handover; another session still gets through
        assert quick_done.wait(10), "a handover in progress blocked another session"
    finally:
        release.set()
    waiting.join(20)
    first.read_all()


class Refusing(Fake):
    """An agent that says why it cannot run."""

    def __init__(self, name, reason):
        super().__init__(name)
        self.reason = reason

    def availability(self):
        return Availability(False, until=time.time() + 3600, reason=self.reason)


def test_a_wait_states_each_agent_s_real_reason(gw):
    gw.agents["claude_api"] = Refusing("claude_api", "key file ~/x.key not found")
    gw.agents["codex"] = Refusing("codex", "codex was not found on PATH")
    s = gw.stream("go")
    text = s.read_until("codex was not found on PATH")
    assert "no agent is available" in text
    assert "- claude_api: key file ~/x.key not found" in text
    assert "- codex: codex was not found on PATH" in text
    assert "resting" not in text
    gw.job().halt()


def test_an_agent_that_gives_no_reason_is_not_left_out():
    text = gateway.render_event("wait", {"seconds": 60, "agents": {"codex": {"reason": ""}}})
    assert "- codex: it gave no reason" in text


def test_a_wait_with_its_own_reason_keeps_it():
    text = gateway.render_event(
        "wait", {"seconds": 60, "reason": "the last round ended at once", "agents": {}}
    )
    assert "waiting 1 min: the last round ended at once" in text


def test_real_adapters_report_what_is_wrong_with_each_agent(fakes, tmp_path, monkeypatch):
    """A service with no binary in reach: the stream says so, for each agent, in its own words."""
    db = tmp_path / "opencode.db"
    make_db(db, [(SESSION, str(fakes.work))])
    monkeypatch.setenv("BATON_OPENCODE_DB", str(db))
    fakes.defaults(
        '[agents.codex]\nbin = "/nonexistent/codex"\n'
        '[agents.antigravity]\nbin = "/nonexistent/agy"\n'
    )
    server, manager = make_server(0, build_adapters)
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    ).start()
    try:
        s = Stream(server.server_address[1], request("go"))
        text = s.read_until("- codex:") + s.read_until("\n\n")
        line = {x.split(":")[0]: x for x in text.splitlines() if x.startswith("- ")}
        assert "/nonexistent/agy" in line["- antigravity"] or "No such" in line["- antigravity"]
        assert "/nonexistent/codex" in line["- codex"] or "No such" in line["- codex"]
        assert "key file" in line["- claude_api"]
    finally:
        manager.shutdown(timeout=10)
        server.shutdown()
        server.server_close()


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


def test_a_parent_or_child_directory_of_a_running_job_is_busy(gw, tmp_path, monkeypatch):
    child = gw.work / "pkg"
    child.mkdir()
    db = tmp_path / "opencode.db"
    db.unlink()
    make_db(db, [(SESSION, str(gw.work)), ("ses_child", str(child))])
    gw.agents["claude_api"].script = [until_killed("busy")]
    gw.stream("go").read_until("busy")
    text = gw.stream("elsewhere", session="ses_child").read_all()
    assert "already running in" in text and "Nothing was started" in text
    assert gw.job("ses_child") is None
    gw.job().halt()


def test_a_directory_claimed_by_a_baton_run_is_refused_at_once(gw):
    with workdir_lock(gw.work / "pkg", "a-goal"):
        text = gw.stream("go").read_all()
    assert "a-goal" in text and "Nothing was started" in text
    assert gw.job() is None and gw.agents["claude_api"].prompts == []


def test_a_directory_claimed_as_the_job_starts_is_refused_by_the_job(gw, monkeypatch):
    """The look before starting can be overtaken: the lock itself still holds."""
    monkeypatch.setattr(gateway, "workdir_conflict", lambda _: None)
    with workdir_lock(gw.work, "a-goal"):
        text = gw.stream("go").read_all()
    assert "cannot start" in text and "a-goal" in text
    assert gw.agents["claude_api"].prompts == []
    assert json.loads((gw.job().store.root / "job.json").read_text())["status"] == "refused"


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


# a client that retries


def test_a_retry_attaches_without_interrupting_or_adding_a_note(gw):
    release = threading.Event()
    gw.agents["claude_api"].script = [wait_event(release, "first")]
    s = gw.stream("go")
    s.read_until("first")
    job = gw.job()
    again = gw.stream("go")  # the same POST again: no new user message
    assert gw.job() is job
    assert not (job.store.root / "interrupt").exists()
    assert not (job.store.root / "inbox.md").exists()
    assert sum(1 for m in job.context if m["role"] == "user") == 1
    release.set()
    assert "done" in again.read_all()
    assert len(gw.agents["claude_api"].prompts) == 1
    assert not any(e.get("interrupted") for e in gw.events() if e["kind"] == "round_end")


def test_two_copies_of_a_post_that_race_deliver_one_message(gw):
    """Both can get past the look before `submit`, so `submit` must look again under its lock."""
    gw.agents["claude_api"].script = [until_killed("busy")]
    gw.stream("go").read_until("busy")
    job = gw.job()
    messages = [{"role": "user", "content": "go"}]
    assert gw.manager.submit(SESSION, gw.work, messages) is job
    assert [m["content"] for m in job.context if m["role"] == "user"] == ["go"]
    assert not (job.store.root / "interrupt").exists()
    job.halt()


def test_a_retry_does_not_kill_the_round(gw):
    gw.agents["claude_api"].script = [until_killed("busy")]
    s = gw.stream("go")
    s.read_until("busy")
    gw.stream("go")
    time.sleep(1.2)  # should_kill is polled constantly: an interrupt would have been seen by now
    assert gw.job().is_running()
    assert [e["kind"] for e in gw.events()].count("round_end") == 0
    gw.job().halt()


def test_the_same_words_at_a_later_position_are_a_new_message(gw):
    gw.agents["claude_api"].script = [until_killed("busy"), say("again")]
    s = gw.stream("continue")
    s.read_until("busy")
    second = gw.stream(
        "continue",
        history=[{"role": "user", "content": "continue"}, {"role": "assistant", "content": "ok"}],
    )
    assert "interrupted by your message" in second.read_until("interrupted by your message")
    second.read_all()
    assert len(gw.agents["claude_api"].prompts) == 2


def test_another_message_at_the_same_position_is_new(gw):
    gw.agents["claude_api"].script = [until_killed("busy"), say("again")]
    s = gw.stream("first")
    s.read_until("busy")
    gw.stream("second").read_all()
    assert "second" in gw.agents["claude_api"].prompts[1]


def test_a_retry_after_the_job_finished_starts_nothing(gw):
    gw.agents["claude_api"].script = [say("first")]
    assert "first" in gw.stream("go").read_all()
    job = gw.job()
    gw.stream("go").read_all()
    assert gw.job() is job
    assert len(gw.agents["claude_api"].prompts) == 1


def test_a_retry_of_a_job_that_ended_under_an_earlier_server_starts_nothing(gw):
    gw.agents["claude_api"].script = [say("first")]
    gw.stream("go").read_all()
    gw.manager._jobs.clear()  # what a new server knows: only the state directory
    text = gw.stream("go").read_all()
    assert "already handled" in text
    assert gw.job() is None
    assert len(gw.agents["claude_api"].prompts) == 1


def test_a_retry_needs_no_session_directory(gw, monkeypatch):
    gw.stream("go").read_all()
    monkeypatch.setattr(
        gateway, "session_directory", lambda _: pytest.fail("a retry must not look it up")
    )
    gw.stream("go").read_all()


def test_a_message_that_could_not_start_is_not_a_retry(gw):
    (gw.work / "baton.toml").write_text("not toml =\n")
    assert "invalid" in gw.stream("go").read_all()
    (gw.work / "baton.toml").unlink()
    gw.agents["claude_api"].script = [say("second try")]
    assert "second try" in gw.stream("go").read_all()


def test_a_message_refused_by_a_lock_can_be_sent_again(gw):
    gw.agents["claude_api"].script = [say("second try")]
    with Store(state_dir(gateway.job_name(SESSION))).lock():
        assert "cannot start" in gw.stream("go").read_all()
    assert "second try" in gw.stream("go").read_all()
    assert json.loads((gw.job().store.root / "job.json").read_text())["status"] == "finished"


def test_a_crashed_job_can_be_told_again(gw, monkeypatch):
    real = gw.manager._adapters_for

    def broken(goal):
        gw.manager._adapters_for = real
        raise RuntimeError("no adapters")

    gw.manager._adapters_for = broken
    assert "crashed" in gw.stream("go").read_all()
    gw.agents["claude_api"].script = [say("second try")]
    assert "second try" in gw.stream("go").read_all()


def test_message_mark_ignores_what_is_around_the_message():
    one = [{"role": "system", "content": "a"}, {"role": "user", "content": "hi"}]
    two = [
        {"role": "system", "content": "b"},
        {"role": "assistant", "content": "x"},
        {"role": "user", "content": " hi "},
    ]
    assert gateway.message_mark(one, 1) == gateway.message_mark(two, 2)
    assert gateway.message_mark(one, 1)[0] == 0


# restarting


def test_a_message_held_through_a_wait_survives_a_service_restart(gw):
    (gw.work / "baton.toml").write_text('[verify]\ncommand = "sleep 1"\n')
    agent = gw.agents["claude_api"]
    agent.announce = "vendor-session"
    spent = threading.Event()

    def resting():
        return Availability(False, until=time.time() + 3600, reason="spent")

    agent.availability = lambda: resting() if spent.is_set() else Availability(True)
    gw.agents["codex"].availability = resting
    gw.stream("fix the bug")
    assert wait_for(lambda: any(e["kind"] == "round_end" for e in gw.events()))
    spent.set()
    history = [
        {"role": "user", "content": "fix the bug"},
        {"role": "assistant", "content": "ok"},
    ]
    gw.stream("wait, also do the docs", history=history)
    assert wait_for(lambda: any(e["kind"] == "wait" for e in gw.events()))

    gw.manager.shutdown(timeout=10)  # launchd's clean SIGTERM, not even a kill -9
    agent.availability = lambda: Availability(True)
    store = Store(state_dir(gateway.job_name(SESSION)))
    state = store.load_state()
    state["agents"]["claude_api"]["until"] = 0.0  # the reset arrived while the server was down
    store.save_state(state)
    gw.manager._closed = False
    manager = restart(gw)
    assert manager.recover() == 1
    assert wait_for(lambda: not manager.get(SESSION).is_running())
    assert any("also do the docs" in p for p in agent.prompts)


def test_a_stop_file_outlives_the_save_of_the_stopped_status(gw, monkeypatch):
    gw.agents["claude_api"].script = [until_killed("busy")]
    s = gw.stream("go")
    s.read_until("busy")
    job = gw.job()
    real = job.store.save_job
    seen = []

    def save_job(record):
        if record["status"] == gateway.STOPPED:
            seen.append(job.store.stop_request())
        real(record)

    monkeypatch.setattr(job.store, "save_job", save_job)
    job.store.request_stop(now=True)
    assert wait_for(lambda: not job.is_running())
    assert seen == ["now"]
    assert job_json(job.store.root)["status"] == gateway.STOPPED
    assert job.store.stop_request() == ""


def test_a_stop_is_honoured_again_when_its_status_never_reached_disk(gw, monkeypatch):
    gw.agents["claude_api"].script = [until_killed("busy")]
    s = gw.stream("go")
    s.read_until("busy")
    job = gw.job()
    real = job.store.save_job

    def save_job(record):
        if record["status"] == gateway.STOPPED:
            raise OSError("no space left on device")
        real(record)

    monkeypatch.setattr(job.store, "save_job", save_job)
    job.store.request_stop(now=True)
    assert wait_for(lambda: not job.is_running())
    assert job.store.stop_request() == "now"

    manager = restart(gw)
    assert manager.recover() == 1
    assert wait_for(lambda: not manager.get(SESSION).is_running())
    assert len(gw.agents["claude_api"].prompts) == 1
    assert job_json(job.store.root)["status"] == gateway.STOPPED
    assert job.store.stop_request() == ""


def test_a_stop_file_that_cannot_be_cleared_still_ends_the_job(gw, monkeypatch):
    gw.agents["claude_api"].script = [until_killed("busy")]
    s = gw.stream("go")
    s.read_until("busy")
    job = gw.job()

    def clear_stop():
        raise OSError("read-only file system")

    monkeypatch.setattr(job.store, "clear_stop", clear_stop)
    job.store.request_stop(now=True)
    assert wait_for(lambda: not job.is_running())
    assert job_json(job.store.root)["status"] == gateway.STOPPED


def test_a_restart_during_a_person_s_stop_keeps_the_stop_file(gw):
    gw.agents["claude_api"].script = [until_killed("busy")]
    s = gw.stream("go")
    s.read_until("busy")
    job = gw.job()
    job.store.request_stop()
    gw.manager.shutdown(timeout=10)
    assert job.store.stop_request() == "now"  # halted, with the person's file underneath
    assert Store(job.store.root).stop_request() == "soon"
    assert job_json(job.store.root)["status"] == gateway.RUNNING


def test_a_verify_command_that_cannot_run_is_explained_in_the_chat(gw):
    (gw.work / "baton.toml").write_text('[verify]\ncommand = "test -f DONE || no-such-tool-xyz"\n')

    def finish(agent, prompt, ctx):
        (gw.work / "DONE").write_text("x")
        return RoundResult(COMPLETED)

    gw.agents["claude_api"].script = [say("trying"), say("trying"), say("now", then=finish)]
    text = gw.stream("go").read_all()
    assert text.count("not found or not executable") == 1
    assert "agent, node and system directories" in text
    assert "verify passed" in text


def job_json(root):
    return json.loads((root / "job.json").read_text())


def restart(gw):
    """The server as a new process finds it: nothing in memory, the state directory on disk."""
    gw.manager._jobs.clear()
    return gw.manager


def leftover(name, workdir, status="running", session=SESSION, **state):
    """The state directory of a job whose server died."""
    store = Store(state_dir(name))
    store.save_state({"workdir": str(workdir), **state})
    store.save_job(
        {
            "session": session,
            "workdir": str(workdir),
            "objective": "fix the bug",
            "conversation": [{"role": "user", "content": "fix the bug"}],
            "accepted": [
                list(gateway.message_mark([{"role": "user", "content": "fix the bug"}], 0))
            ],
            "status": status,
        }
    )
    return store


@pytest.fixture
def stray():
    """A live process group that is not a child waiting to be reaped by anyone but a thread."""
    made = []

    def spawn():
        proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
        threading.Thread(target=proc.wait, daemon=True).start()
        made.append(proc)
        return proc

    yield spawn
    for proc in made:
        if proc.poll() is None:
            proc.kill()


def group_of(proc):
    return {"group": proc.pid, "group_start": process_start(proc.pid)}


def test_job_json_holds_what_a_restart_needs(gw):
    gw.agents["claude_api"].script = [until_killed("busy"), say("done now")]
    s = gw.stream("first")
    s.read_until("busy")
    record = job_json(gw.job().store.root)
    assert record["session"] == SESSION
    assert record["workdir"] == str(gw.work)
    assert record["status"] == "running"
    assert record["objective"] == "first"
    assert record["conversation"] == [{"role": "user", "content": "first"}]
    gw.stream("second").read_all()
    record = job_json(gw.job().store.root)
    assert [m["content"] for m in record["conversation"]] == ["first", "second"]
    assert len(record["accepted"]) == 2
    assert record["status"] == "finished"


def test_job_json_is_not_a_copy_of_the_whole_request(gw):
    huge = [
        {"role": "tool", "content": "x" * 300_000},
        {"role": "assistant", "content": "y" * 300_000},
        {"role": "user", "content": "z" * 300_000},
    ]
    gw.stream("go", history=huge).read_all()
    assert (gw.job().store.root / "job.json").stat().st_size < 400_000


def test_a_person_stopping_a_job_is_final(gw):
    gw.agents["claude_api"].script = [until_killed("busy")]
    s = gw.stream("go")
    s.read_until("busy")
    gw.job().store.request_stop(now=True)
    s.read_all()
    assert job_json(gw.job().store.root)["status"] == "stopped"
    manager = restart(gw)
    assert manager.recover() == 0


def test_a_server_shutdown_is_not_a_stop(gw):
    gw.agents["claude_api"].script = [until_killed("busy")]
    s = gw.stream("go")
    s.read_until("busy")
    root = gw.job().store.root
    gw.manager.shutdown(timeout=10)
    assert "stopped" in s.read_all()
    assert job_json(root)["status"] == "running"
    assert not (root / "stop").exists()


def test_a_person_s_stop_survives_a_shutdown_of_the_server(gw):
    gw.agents["claude_api"].script = [until_killed("busy")]
    s = gw.stream("go")
    s.read_until("busy")
    job = gw.job()
    job.halt()
    outside = Store(job.store.root)  # `baton stop`, from another process
    outside.request_stop()
    s.read_all()
    assert outside.stop_request() == "soon"


def test_recover_kills_a_verified_group_and_resumes_the_job(gw, stray):
    agent = stray()
    store = leftover(gateway.job_name(SESSION), gw.work, **group_of(agent))
    gw.agents["claude_api"].script = [say("back at it")]
    manager = restart(gw)
    assert manager.recover() == 1
    job = manager.get(SESSION)
    assert wait_for(lambda: not job.is_running())
    assert agent.poll() is not None
    assert "fix the bug" in gw.agents["claude_api"].prompts[0]
    assert store.load_state()["group"] is None
    assert job_json(store.root)["status"] == "finished"
    assert any("left by a previous run" in str(e.get("message")) for e in store.events())


def test_recover_kills_a_group_of_a_job_that_will_not_be_resumed(gw, stray):
    agent = stray()
    store = leftover("session-old", gw.work, status="finished", session="old", **group_of(agent))
    manager = restart(gw)
    assert manager.recover() == 0
    assert wait_for(lambda: agent.poll() is not None)
    assert store.load_state()["group"] is None
    assert gw.agents["claude_api"].prompts == []


def test_recover_kills_a_group_left_by_a_goal_run(gw, stray):
    agent = stray()
    Store(state_dir("a-goal")).save_state({"workdir": str(gw.work), **group_of(agent)})
    assert restart(gw).recover() == 0
    assert wait_for(lambda: agent.poll() is not None)


def test_recover_leaves_a_live_runner_alone(gw, stray):
    agent = stray()
    store = leftover(gateway.job_name(SESSION), gw.work, **group_of(agent))
    manager = restart(gw)
    with store.lock():
        assert manager.recover() == 0
    assert agent.poll() is None
    assert manager.get(SESSION) is None


def test_recover_does_not_touch_a_group_it_cannot_verify(gw, stray):
    agent = stray()
    store = leftover("session-old", gw.work, status="finished", session="old", group=agent.pid)
    assert restart(gw).recover() == 0
    assert agent.poll() is None
    assert store.load_state()["group"] == agent.pid
    assert any("could not be verified" in str(e.get("message")) for e in store.events())


def test_recover_does_not_touch_a_group_whose_number_was_recycled(gw, stray):
    agent = stray()
    store = leftover(
        "session-old",
        gw.work,
        status="finished",
        session="old",
        group=agent.pid,
        group_start="Mon Jan  1 00:00:00 2001",
    )
    assert restart(gw).recover() == 0
    assert agent.poll() is None
    assert store.load_state()["group"] == agent.pid


def test_recover_leaves_finished_and_stopped_jobs(gw):
    for name, status in (("finished", "finished"), ("stopped", "stopped"), ("refused", "refused")):
        leftover(f"session-{name}", gw.work, status=status, session=name)
    manager = restart(gw)
    assert manager.recover() == 0
    assert gw.agents["claude_api"].prompts == []


def test_recover_goes_on_past_a_job_it_cannot_read(gw, capsys):
    bad = Store(state_dir("session-bad"))
    (bad.root / "job.json").write_text("{ not json")
    leftover("session-gone", gw.work / "gone", session="gone")
    leftover(gateway.job_name(SESSION), gw.work)
    gw.agents["claude_api"].script = [say("still works")]
    manager = restart(gw)
    assert manager.recover() == 1
    out = capsys.readouterr().out
    assert "session-bad" in out and "session-gone" in out
    assert wait_for(lambda: not manager.get(SESSION).is_running())


def test_a_job_json_that_names_another_session_is_not_resumed(gw, capsys):
    leftover("session-other", gw.work, session="not-this-one")
    assert restart(gw).recover() == 0
    assert gw.agents["claude_api"].prompts == []


def test_a_resumed_job_honours_a_stop_asked_for_while_the_server_was_down(gw):
    store = leftover(gateway.job_name(SESSION), gw.work)
    store.request_stop()
    manager = restart(gw)
    assert manager.recover() == 1
    assert wait_for(lambda: not manager.get(SESSION).is_running())
    assert gw.agents["claude_api"].prompts == []
    assert job_json(store.root)["status"] == "stopped"


def test_a_resumed_job_does_not_owe_another_round(gw):
    (gw.work / "baton.toml").write_text('[verify]\ncommand = "true"\n')
    store = leftover(gateway.job_name(SESSION), gw.work)
    manager = restart(gw)
    assert manager.recover() == 1
    assert wait_for(lambda: not manager.get(SESSION).is_running())
    assert gw.agents["claude_api"].prompts == []
    assert job_json(store.root)["status"] == "finished"


def test_a_resumed_job_keeps_the_round_it_owed(gw):
    (gw.work / "baton.toml").write_text('[verify]\ncommand = "true"\n')
    leftover(gateway.job_name(SESSION), gw.work, owes_round=True)
    manager = restart(gw)
    assert manager.recover() == 1
    assert wait_for(lambda: not manager.get(SESSION).is_running())
    assert len(gw.agents["claude_api"].prompts) == 1


@pytest.fixture
def resumed(gw):
    """A server taken over by a new manager while a job's round is running."""
    leftover(gateway.job_name(SESSION), gw.work)
    gw.agents["claude_api"].script = [until_killed("resumed work"), say("finished it")]
    assert restart(gw).recover() == 1
    return gw


def test_reattaching_to_a_resumed_job_streams_it_without_interrupting(resumed):
    gw = resumed
    s = gw.stream("fix the bug")  # the client retries what it was waiting on
    assert "baton restarted" in s.read_until("resumed work")
    assert [m["content"] for m in gw.job().context] == ["fix the bug"]  # no note was added
    assert not (gw.job().store.root / "inbox.md").exists()
    gw.job().halt()
    s.read_all()
    assert not any(e.get("interrupted") for e in gw.events() if e["kind"] == "round_end")


def test_a_new_message_reaches_a_resumed_job(resumed):
    gw = resumed
    gw.stream("fix the bug").read_until("resumed work")
    s = gw.stream(
        "and the docs",
        history=[
            {"role": "user", "content": "fix the bug"},
            {"role": "assistant", "content": "working"},
        ],
    )
    text = s.read_all()
    assert "interrupted by your message" in text and "finished it" in text
    assert "and the docs" in gw.agents["claude_api"].prompts[1]


def test_serve_kill_9_kills_the_orphan_and_resumes_the_job(fakes, monkeypatch, tmp_path):
    """The whole repro, with real processes: the agent outlives the server, and the next server
    ends it and carries the job on."""
    db = tmp_path / "opencode.db"
    make_db(db, [(SESSION, str(fakes.work))])
    monkeypatch.setenv("BATON_OPENCODE_DB", str(db))
    (fakes.work / "baton.toml").write_text(
        'order = ["codex"]\nsnapshot = false\n'
        "[agents.antigravity]\nenabled = false\n[agents.claude_api]\nenabled = false\n"
    )

    def start(sleep):
        env = {**os.environ, "FAKE_CODEX_EXEC_SLEEP": sleep}
        proc = subprocess.Popen(
            [sys.executable, "-m", "baton.cli", "serve", "--port", "0"],
            stdout=subprocess.PIPE,
            text=True,
            env=env,
        )
        line = proc.stdout.readline()
        assert "127.0.0.1" in line, line
        return proc, int(line.rsplit(":", 1)[1].split("/")[0])

    first, port = start("60")
    second = None
    try:
        Stream(port, request("work for a long time"))
        assert wait_for(fakes.codex_pidfile.exists)
        pid = int(fakes.codex_pidfile.read_text())
        state = Store(state_dir(gateway.job_name(SESSION)))
        assert wait_for(lambda: state.load_state().get("group") == pid)
        first.kill()
        first.wait()
        assert _alive(pid), "the agent should outlive a server killed with SIGKILL"

        second, port = start("0")
        assert wait_for(lambda: not _alive(pid))
        assert wait_for(lambda: job_json(state.root)["status"] == "finished")
        assert len(fakes.codex_calls()) == 2
        assert "resume" in fakes.codex_calls()[1]["argv"]  # its own conversation goes on
        Stream(port, request("work for a long time")).read_all()
        assert len(fakes.codex_calls()) == 2  # the retry attached: no third agent call
    finally:
        for proc in (first, second):
            if proc is None:
                continue
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            proc.stdout.close()
        if fakes.codex_pidfile.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(fakes.codex_pidfile.read_text()), signal.SIGKILL)


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

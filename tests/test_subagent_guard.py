"""The PreToolUse hook that keeps Claude's and Codex's subagents on the two allowed models."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from baton import subagent_guard
from baton.subagent_guard import decide

ALLOWED = "sonnet,claude-sonnet-5-5,opus,claude-opus-5-5"
GUARD = Path(subagent_guard.__file__)


def call(tool="Agent", **tool_input):
    return json.dumps(
        {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input}
    )


def denied(answer):
    out = answer["hookSpecificOutput"]
    return out["hookEventName"] == "PreToolUse" and out["permissionDecision"] == "deny"


@pytest.mark.parametrize(
    "model", ["sonnet", "claude-sonnet-5-5", "opus", "claude-opus-5-5", "Sonnet"]
)
def test_a_subagent_on_an_allowed_model_is_left_allowed(model):
    assert decide(call(model=model, prompt="x"), ALLOWED) is None


@pytest.mark.parametrize("tool", ["Agent", "Task"])
def test_a_subagent_with_no_model_is_allowed_and_takes_the_default(tool):
    assert decide(call(tool, prompt="x"), ALLOWED) is None
    assert decide(call(tool, prompt="x", model=None), ALLOWED) is None


@pytest.mark.parametrize(
    "model", ["haiku", "claude-haiku-4-5", "sonnet-5", "gpt-5", "", 5, ["sonnet"]]
)
@pytest.mark.parametrize("tool", ["Agent", "Task"])
def test_every_other_model_is_denied_and_the_reason_names_the_allowed_ones(tool, model):
    answer = decide(call(tool, model=model), ALLOWED)
    assert denied(answer)
    reason = answer["hookSpecificOutput"]["permissionDecisionReason"]
    assert "claude-sonnet-5-5" in reason and "claude-opus-5-5" in reason


@pytest.mark.parametrize(
    "tool",
    ["collaborationspawn_agent", "collaboration.spawn_agent", "spawn_agent", "x_spawn_agent_y"],
)
def test_codex_names_are_matched_by_the_part_spawn_agent(tool):
    assert denied(decide(call(tool, model="gpt-6-luna"), "gpt-6.1-sol,gpt-6.1-terra"))
    assert decide(call(tool, model="gpt-6.1-terra"), "gpt-6.1-sol,gpt-6.1-terra") is None
    assert decide(call(tool, message="x"), "gpt-6.1-sol,gpt-6.1-terra") is None  # inherited


def test_the_deny_for_codex_names_both_allowed_models():
    answer = decide(
        call("collaborationspawn_agent", model="gpt-6-luna"), "gpt-6.1-sol,gpt-6.1-terra"
    )
    reason = answer["hookSpecificOutput"]["permissionDecisionReason"]
    assert "gpt-6.1-sol" in reason and "gpt-6.1-terra" in reason


@pytest.mark.parametrize("tool", ["Agent", "Task", "collaborationspawn_agent"])
def test_with_an_empty_allowed_list_every_subagent_call_is_denied_even_with_no_model(tool):
    assert denied(decide(call(tool, prompt="x"), ""))
    assert denied(decide(call(tool, model="sonnet"), " , "))


def test_other_tools_are_not_the_guard_s_business():
    assert decide(call("Bash", model="haiku"), ALLOWED) is None


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not json",
        "[1, 2]",
        "null",
        '{"tool_name": "Agent"}',
        '{"tool_name": "Agent", "tool_input": "x"}',
    ],
)
def test_bad_input_for_a_subagent_or_unreadable_input_is_denied(raw):
    answer = decide(raw, ALLOWED)
    assert answer is not None and denied(answer)


def run_hook(stdin, env_value=ALLOWED):
    env = {"PATH": "/usr/bin:/bin"}
    if env_value is not None:
        env["BATON_ALLOWED_MODELS"] = env_value
    return subprocess.run(
        [sys.executable, "-I", str(GUARD)], input=stdin, capture_output=True, text=True, env=env
    )


def test_as_a_process_a_denied_call_prints_the_deny_document_and_exits_zero():
    done = run_hook(call(model="haiku"))
    assert done.returncode == 0
    assert denied(json.loads(done.stdout))


def test_as_a_process_an_allowed_call_prints_nothing():
    done = run_hook(call(model="sonnet"))
    assert done.returncode == 0 and done.stdout == ""


def test_as_a_process_garbage_is_a_deny_and_a_missing_setting_denies_a_named_model():
    assert denied(json.loads(run_hook("{{{").stdout))
    assert denied(json.loads(run_hook(call(model="sonnet"), env_value=None).stdout))


def test_it_fails_closed_when_the_decision_itself_raises(monkeypatch, capsys):
    def boom(raw, allowed):
        raise RuntimeError("bug")

    monkeypatch.setattr(subagent_guard, "decide", boom)
    monkeypatch.setattr(sys, "stdin", open(__file__))
    assert subagent_guard.main() == 0
    assert denied(json.loads(capsys.readouterr().out))


def test_the_module_uses_only_the_standard_library():
    text = GUARD.read_text()
    imports = [line for line in text.splitlines() if line.startswith(("import ", "from "))]
    assert all(line.split()[1] in {"__future__", "json", "os", "sys"} for line in imports), imports

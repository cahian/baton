"""Which models a subagent may use: exactly the provider's two, and nothing that merely looks like
one."""

import pytest
from test_antigravity import make_cfg as agy_cfg
from test_codex import make_cfg as codex_cfg

from baton.adapters.base import RoundContext, check_subagent, model_allowed


@pytest.mark.parametrize(
    "model", ["gpt-6.1-sol", "gpt-6.1-terra", "GPT-6.1-TERRA", " gpt-6.1-sol "]
)
def test_codex_allows_its_two_models_exactly(model):
    assert model_allowed(codex_cfg(), model)


@pytest.mark.parametrize(
    "model",
    ["gpt-6.1", "sol", "terra", "gpt-6.1-sol-mini", "xgpt-6.1-terra", "gpt-5-mini", "inherit", ""],
)
def test_codex_refuses_everything_else_with_no_substring_match(model):
    assert not model_allowed(codex_cfg(), model)


@pytest.mark.parametrize(
    "model",
    ["gemini-4-argon-medium", "gemini-3.8-flash-high", "gemini-4-argon", "gemini-3.8-flash"],
)
def test_antigravity_allows_its_two_ids_with_or_without_the_effort_suffix(model):
    assert model_allowed(agy_cfg(), model)


@pytest.mark.parametrize(
    "model",
    ["gemini-4", "gemini-3.8", "argon", "flash", "gemini-3.8-flash-low", "gemini-4-argon-x"],
)
def test_antigravity_refuses_the_rest(model):
    assert not model_allowed(agy_cfg(), model)


def test_the_effort_suffix_is_only_dropped_for_antigravity():
    assert not model_allowed(codex_cfg(model="gpt-6.1-sol-high"), "gpt-6.1-sol")


def ctx(tmp_path, seen):
    return RoundContext(
        workdir=tmp_path,
        log_path=tmp_path / "l",
        round_max_s=1,
        idle_s=1,
        on_output=lambda s: None,
        should_kill=lambda: False,
        on_spawn=lambda p: None,
        on_subagent=lambda t, m: seen.append((t, m)),
    )


def test_a_subagent_with_no_visible_model_is_reported_and_allowed(tmp_path):
    seen = []
    assert check_subagent(codex_cfg(), ctx(tmp_path, seen), "spawn_agent", None) is None
    assert seen == [("spawn_agent", None)]


def claude_cfg(**kw):
    from baton.config import AgentConfig

    d = dict(
        name="claude_api",
        enabled=True,
        model="claude-opus-5-5",
        effort="medium",
        build_model="claude-sonnet-5-5",
        build_effort="high",
        threshold_pct=95,
        poll_s=15,
        bin="claude",
        full_access=True,
    )
    d.update(kw)
    return AgentConfig(**d)


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-sonnet-5-5", "opus", "sonnet"])
def test_claude_allows_its_two_ids_and_their_family_aliases(model):
    assert model_allowed(claude_cfg(), model)


@pytest.mark.parametrize(
    "model", ["haiku", "claude-haiku-4-5", "claude-sonnet-5", "sonnet-5-5", "inherit", "son"]
)
def test_claude_refuses_everything_else(model):
    assert not model_allowed(claude_cfg(), model)


def test_the_allowed_names_are_the_ids_and_their_families_working_model_first():
    from baton.adapters.base import claude_allowed_models

    assert claude_allowed_models(claude_cfg()) == [
        "sonnet",
        "claude-sonnet-5-5",
        "opus",
        "claude-opus-5-5",
    ]
    assert claude_allowed_models(claude_cfg(model="x", build_model="y")) == ["y", "x"]


def test_without_enforce_a_forbidden_model_is_only_logged(tmp_path):
    seen = []
    result = check_subagent(codex_cfg(), ctx(tmp_path, seen), "spawn_agent", "gpt-5-mini")
    assert result is None and seen == [("spawn_agent", "gpt-5-mini")]


def test_with_enforce_a_forbidden_visible_model_ends_the_round(tmp_path):
    from baton.adapters.base import SUBAGENT

    seen = []
    result = check_subagent(
        codex_cfg(), ctx(tmp_path, seen), "spawn_agent", "gpt-5-mini", enforce=True
    )
    assert result is not None and result.outcome == SUBAGENT and "gpt-5-mini" in result.reason
    assert check_subagent(codex_cfg(), ctx(tmp_path, []), "t", None, enforce=True) is None

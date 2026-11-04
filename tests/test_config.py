from pathlib import Path

import pytest

from baton.config import GoalError, load_goal

BASE = {
    "name": 'name = "demo"',
    "workdir": 'workdir = "/work/demo"',
    "objective": 'objective = "Ship it."',
}
VERIFY = '[verify]\ncommand = "./check.sh"\n'


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "goal.toml"
    p.write_text(text)
    return p


def minimal(skip: str | None = None) -> str:
    parts = [v for k, v in BASE.items() if k != skip]
    if skip != "verify.command":
        parts.append(VERIFY)
    return "\n".join(parts) + "\n"


def test_defaults_match_spec(tmp_path):
    goal = load_goal(write(tmp_path, minimal()))
    assert goal.order == ("codex", "antigravity", "claude")
    assert goal.workdir == Path("/work/demo")
    assert (goal.round_max_s, goal.idle_s, goal.snapshot) == (3600, 1200, True)
    assert (goal.verify_command, goal.verify_timeout_s) == ("./check.sh", 3600)
    codex, agy, claude = (goal.agents[n] for n in ("codex", "antigravity", "claude"))
    assert codex.enabled and codex.threshold_pct == 90 and codex.poll_s == 15
    assert (codex.model, codex.effort, codex.bin, codex.full_access) == (
        "gpt-6.1-sol",
        "medium",
        "codex",
        True,
    )
    assert agy.enabled is True
    assert (agy.model, agy.bin) == ("gemini-4-argon-medium", "agy")
    assert (agy.threshold_pct, agy.poll_s) == (90, 30)
    assert claude.enabled and claude.threshold_pct == 95
    assert (claude.model, claude.effort, claude.bin) == ("sonnet", "high", "claude")
    assert goal.path == tmp_path / "goal.toml"


@pytest.mark.parametrize("key", ["name", "workdir", "objective", "verify.command"])
def test_missing_required_key_raises_goalerror(tmp_path, key):
    with pytest.raises(GoalError, match=key.replace(".", r"\.")):
        load_goal(write(tmp_path, minimal(skip=key)))


def test_unknown_agent_in_order_raises(tmp_path):
    text = minimal().replace('objective = "Ship it."', 'objective = "x"\norder = ["codex", "gpt"]')
    with pytest.raises(GoalError, match="gpt"):
        load_goal(write(tmp_path, text))


def test_overrides_are_applied(tmp_path):
    text = minimal() + (
        '[agents.codex]\nthreshold_pct = 80\nfull_access = false\nbin = "/opt/codex"\n'
        "[agents.antigravity]\nenabled = false\n"
    )
    goal = load_goal(write(tmp_path, text))
    assert goal.agents["codex"].threshold_pct == 80
    assert goal.agents["codex"].full_access is False
    assert goal.agents["codex"].bin == "/opt/codex"
    assert goal.agents["antigravity"].enabled is False


@pytest.mark.parametrize(
    "extra",
    [
        "[agents.codex]\nthreshold_pct = 0\n",
        "[agents.claude]\nthreshold_pct = 100\n",
        "[agents.codex]\nthreshold_pct = 'high'\n",
        "[agents.codex]\nenabled = 1\n",
        "[agents.nope]\nenabled = true\n",
        "[agents.codex]\ntypo = 1\n",
    ],
)
def test_invalid_values_raise(tmp_path, extra):
    with pytest.raises(GoalError):
        load_goal(write(tmp_path, minimal() + extra))


@pytest.mark.parametrize("bad", ['name = "../x"', 'name = "a/b"', 'name = ""'])
def test_unsafe_name_raises(tmp_path, bad):
    text = minimal().replace('name = "demo"', bad)
    with pytest.raises(GoalError):
        load_goal(write(tmp_path, text))


def test_malformed_toml_and_missing_file(tmp_path):
    with pytest.raises(GoalError):
        load_goal(write(tmp_path, "name = "))
    with pytest.raises(GoalError):
        load_goal(tmp_path / "nope.toml")


def test_relative_workdir_resolves_against_goal_file(tmp_path):
    text = minimal().replace('workdir = "/work/demo"', 'workdir = "proj"')
    goal = load_goal(write(tmp_path, text))
    assert goal.workdir == tmp_path / "proj"


def test_relative_goal_path_gives_absolute_workdir(tmp_path, monkeypatch):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "goal.toml").write_text(minimal().replace("/work/demo", "proj"))
    monkeypatch.chdir(tmp_path)
    goal = load_goal(Path("sub/goal.toml"))
    assert goal.workdir.is_absolute()
    assert goal.workdir == (tmp_path / "sub" / "proj").resolve()


def test_all_ordered_agents_disabled_raises(tmp_path):
    text = (
        minimal()
        + "[agents.codex]\nenabled = false\n[agents.claude]\nenabled = false\n"
        + "[agents.antigravity]\nenabled = false\n"
    )
    with pytest.raises(GoalError, match="disabled"):
        load_goal(write(tmp_path, text))


def test_enabled_agent_outside_order_does_not_count(tmp_path):
    text = minimal().replace('objective = "Ship it."', 'objective = "x"\norder = ["codex"]')
    text += "[agents.codex]\nenabled = false\n"
    with pytest.raises(GoalError, match="disabled"):
        load_goal(write(tmp_path, text))


def test_threshold_99_is_accepted(tmp_path):
    goal = load_goal(write(tmp_path, minimal() + "[agents.codex]\nthreshold_pct = 99\n"))
    assert goal.agents["codex"].threshold_pct == 99

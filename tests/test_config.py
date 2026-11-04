from decimal import Decimal
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
    parts.append(VERIFY)
    return "\n".join(parts) + "\n"


def test_defaults_match_spec(tmp_path):
    goal = load_goal(write(tmp_path, minimal()))
    assert goal.order == ("claude_api", "antigravity", "codex")
    assert set(goal.agents) == {"claude_api", "antigravity", "codex"}
    assert goal.workdir == Path("/work/demo")
    assert (goal.round_max_s, goal.idle_s, goal.snapshot) == (3600, 1200, True)
    assert (goal.verify_command, goal.verify_timeout_s) == ("./check.sh", 3600)
    codex, agy = (goal.agents[n] for n in ("codex", "antigravity"))
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
    assert goal.path == tmp_path / "goal.toml"


@pytest.mark.parametrize("key", ["name", "workdir", "objective"])
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
        "[agents.antigravity]\nthreshold_pct = 100\n",
        "[agents.claude]\nenabled = true\n",
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
    text = minimal() + "".join(
        f"[agents.{a}]\nenabled = false\n" for a in ("codex", "antigravity", "claude_api")
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


def test_verify_command_is_optional(tmp_path):
    text = minimal().replace(VERIFY, "")
    assert load_goal(write(tmp_path, text)).verify_command is None
    text = minimal().replace(VERIFY, "[verify]\ntimeout_s = 60\n")
    goal = load_goal(write(tmp_path, text))
    assert (goal.verify_command, goal.verify_timeout_s) == (None, 60)


def test_empty_verify_command_still_raises(tmp_path):
    text = minimal().replace('"./check.sh"', '""')
    with pytest.raises(GoalError, match=r"verify\.command"):
        load_goal(write(tmp_path, text))


def test_claude_is_no_longer_an_agent(tmp_path):
    text = minimal().replace(
        'objective = "Ship it."', 'objective = "x"\norder = ["codex", "claude"]'
    )
    with pytest.raises(GoalError, match="claude"):
        load_goal(write(tmp_path, text))


def claude(**fields) -> str:
    return "[agents.claude_api]\n" + "".join(f"{k} = {v}\n" for k, v in fields.items())


def test_claude_api_defaults_match_spec(tmp_path):
    cfg = load_goal(write(tmp_path, minimal())).agents["claude_api"]
    assert cfg.enabled is True
    assert (cfg.model, cfg.effort, cfg.bin) == ("claude-opus-5-5", "medium", "claude")
    assert cfg.monthly_budget_usd == Decimal("200") and isinstance(cfg.monthly_budget_usd, Decimal)
    assert cfg.reserve_pct == 10
    assert cfg.round_budget_usd == Decimal("10") and isinstance(cfg.round_budget_usd, Decimal)
    assert cfg.budget_reset_day == 1
    assert cfg.key_file == "~/.config/baton/anthropic.key"
    assert cfg.admin_key_file == ""


def test_claude_api_overrides_are_applied_as_decimals(tmp_path):
    text = minimal() + claude(
        monthly_budget_usd="150.55",
        reserve_pct=20,
        round_budget_usd=2.5,
        budget_reset_day=31,
        key_file='"/k/key"',
        admin_key_file='"/k/admin"',
        model='"claude-sonnet-5-5"',
        effort='"max"',
        bin='"/opt/claude"',
        enabled="false",
    ).replace('"150.55"', "150.55")
    cfg = load_goal(write(tmp_path, text)).agents["claude_api"]
    assert cfg.monthly_budget_usd == Decimal("150.55")  # exactly, not through a float
    assert cfg.round_budget_usd == Decimal("2.5")
    assert (cfg.reserve_pct, cfg.budget_reset_day) == (20, 31)
    assert (cfg.key_file, cfg.admin_key_file) == ("/k/key", "/k/admin")
    assert (cfg.model, cfg.effort, cfg.bin, cfg.enabled) == (
        "claude-sonnet-5-5",
        "max",
        "/opt/claude",
        False,
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"monthly_budget_usd": 0},
        {"monthly_budget_usd": -5},
        {"monthly_budget_usd": "nan"},
        {"monthly_budget_usd": "inf"},
        {"monthly_budget_usd": '"200"'},
        {"monthly_budget_usd": "true"},
        {"reserve_pct": 0},
        {"reserve_pct": 100},
        {"reserve_pct": 10.5},
        {"round_budget_usd": 0},
        {"round_budget_usd": -1},
        {"round_budget_usd": "nan"},
        {"budget_reset_day": 0},
        {"budget_reset_day": 32},
        {"budget_reset_day": 1.5},
        {"key_file": '""'},
        {"admin_key_file": "5"},
        {"threshold_pct": 90},
        {"poll_s": 15},
        {"full_access": "false"},
        {"typo": 1},
    ],
)
def test_invalid_claude_api_values_raise(tmp_path, fields):
    with pytest.raises(GoalError):
        load_goal(write(tmp_path, minimal() + claude(**fields)))


@pytest.mark.parametrize("agent", ["codex", "antigravity"])
@pytest.mark.parametrize(
    "field",
    [
        "monthly_budget_usd = 5",
        "reserve_pct = 10",
        "round_budget_usd = 1",
        "budget_reset_day = 1",
        'key_file = "x"',
        'admin_key_file = "x"',
    ],
)
def test_claude_fields_are_rejected_on_other_agents(tmp_path, agent, field):
    with pytest.raises(GoalError, match=field.split(" ")[0]):
        load_goal(write(tmp_path, minimal() + f"[agents.{agent}]\n{field}\n"))


def test_bounds_of_claude_api_values_are_accepted(tmp_path):
    text = minimal() + claude(
        monthly_budget_usd="0.01", reserve_pct=1, round_budget_usd="0.01", budget_reset_day=31
    ).replace('"0.01"', "0.01")
    load_goal(write(tmp_path, text))
    load_goal(write(tmp_path, minimal() + claude(reserve_pct=99, budget_reset_day=1)))


def test_claude_api_can_be_the_only_agent(tmp_path):
    text = minimal().replace('objective = "Ship it."', 'objective = "x"\norder = ["claude_api"]')
    assert load_goal(write(tmp_path, text)).order == ("claude_api",)

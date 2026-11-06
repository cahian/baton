from decimal import Decimal
from pathlib import Path

import pytest

from baton.config import (
    GoalError,
    MemoryConfig,
    check_repo_table,
    default_agent,
    defaults_path,
    goal_from_table,
    load_defaults,
    load_goal,
    load_memory,
)

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
    assert goal.order == ("codex", "antigravity", "claude_api")
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


def test_threshold_90_is_accepted(tmp_path):
    goal = load_goal(write(tmp_path, minimal() + "[agents.codex]\nthreshold_pct = 90\n"))
    assert goal.agents["codex"].threshold_pct == 90


@pytest.mark.parametrize("agent", ["codex", "antigravity"])
def test_a_threshold_above_90_is_refused_in_a_goal(tmp_path, agent):
    with pytest.raises(GoalError, match="threshold_pct must be between 1 and 90"):
        load_goal(write(tmp_path, minimal() + f"[agents.{agent}]\nthreshold_pct = 91\n"))


def test_a_threshold_above_90_is_refused_in_the_defaults_file(tmp_path, monkeypatch):
    path = tmp_path / "defaults.toml"
    path.write_text("[agents.codex]\nthreshold_pct = 91\n")
    monkeypatch.setenv("BATON_CONFIG", str(path))
    with pytest.raises(GoalError, match="threshold_pct must be between 1 and 90"):
        load_defaults()


@pytest.mark.parametrize("model", ["gpt-6.1-sol", "claude-opus-5-5", "gemini", "Gemini-4"])
def test_an_antigravity_model_that_is_not_gemini_is_refused(tmp_path, model):
    text = minimal() + f'[agents.antigravity]\nmodel = "{model}"\n'
    with pytest.raises(GoalError, match="must start with 'gemini-'"):
        load_goal(write(tmp_path, text))


def test_the_gemini_prefix_only_binds_antigravity(tmp_path):
    goal = load_goal(write(tmp_path, minimal() + '[agents.codex]\nmodel = "gpt-7"\n'))
    assert goal.agents["codex"].model == "gpt-7"


def test_a_tilde_in_bin_is_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "me"))
    text = (
        minimal()
        + '[agents.codex]\nbin = "~/bin/codex"\n[agents.claude_api]\nbin = "~/bin/claude"\n'
    )
    goal = load_goal(write(tmp_path, text))
    assert goal.agents["codex"].bin == str(tmp_path / "me/bin/codex")
    assert goal.agents["claude_api"].bin == str(tmp_path / "me/bin/claude")


def test_a_bare_bin_stays_a_command_name(tmp_path):
    assert load_goal(write(tmp_path, minimal())).agents["codex"].bin == "codex"


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
    assert cfg.round_budget_usd == Decimal("5") and isinstance(cfg.round_budget_usd, Decimal)
    assert cfg.budget_reset_day == 1
    assert cfg.key_file == "~/.config/baton/anthropic.key"
    assert cfg.admin_key_file == ""


def defaults(tmp_path, monkeypatch, text: str) -> Path:
    """Write the defaults file that BATON_CONFIG points at."""
    path = tmp_path / "defaults.toml"
    path.write_text(text)
    monkeypatch.setenv("BATON_CONFIG", str(path))
    return path


def test_claude_api_overrides_are_applied_as_decimals(tmp_path, monkeypatch):
    defaults(
        tmp_path,
        monkeypatch,
        "[agents.claude_api]\nmonthly_budget_usd = 150.55\nreserve_pct = 20\n"
        "budget_reset_day = 31\n",
    )
    text = minimal() + claude(
        round_budget_usd=2.5,
        key_file='"/k/key"',
        admin_key_file='"/k/admin"',
        model='"claude-sonnet-5-5"',
        effort='"max"',
        bin='"/opt/claude"',
        enabled="false",
    )
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
    "key", ["monthly_budget_usd = 100", "reserve_pct = 20", "budget_reset_day = 15"]
)
def test_budget_settings_are_rejected_in_a_goal_file(tmp_path, key):
    """They describe the one shared ledger: a goal that sets its own wipes the others' spend."""
    with pytest.raises(GoalError, match=r"defaults\.toml") as err:
        load_goal(write(tmp_path, minimal() + f"[agents.claude_api]\n{key}\n"))
    assert key.split(" ")[0] in str(err.value)


def test_budget_settings_in_defaults_apply_to_every_goal(tmp_path, monkeypatch):
    defaults(tmp_path, monkeypatch, "[agents.claude_api]\nbudget_reset_day = 15\n")
    first = load_goal(write(tmp_path, minimal())).agents["claude_api"]
    other = tmp_path / "other"
    other.mkdir()
    second = load_goal(write(other, minimal().replace("demo", "second"))).agents["claude_api"]
    assert first.budget_reset_day == second.budget_reset_day == 15


def test_a_goal_overrides_defaults_for_everything_but_the_budget(tmp_path, monkeypatch):
    defaults(
        tmp_path,
        monkeypatch,
        '[agents.claude_api]\nmodel = "claude-sonnet-5-5"\nround_budget_usd = 20\n'
        "[agents.codex]\nthreshold_pct = 70\n",
    )
    goal = load_goal(write(tmp_path, minimal() + claude(round_budget_usd=7)))
    assert goal.agents["claude_api"].model == "claude-sonnet-5-5"  # from the defaults
    assert goal.agents["claude_api"].round_budget_usd == Decimal("7")  # the goal wins
    assert goal.agents["codex"].threshold_pct == 70


def test_no_defaults_file_is_no_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("BATON_CONFIG", str(tmp_path / "missing.toml"))
    assert load_defaults() == {}
    assert default_agent("claude_api").budget_reset_day == 1


def test_load_defaults_returns_the_tables_by_agent(tmp_path, monkeypatch):
    defaults(
        tmp_path,
        monkeypatch,
        "[agents.claude_api]\nmonthly_budget_usd = 300\n[agents.antigravity]\npoll_s = 60\n",
    )
    found = load_defaults()
    assert set(found) == {"claude_api", "antigravity"}
    assert found["claude_api"]["monthly_budget_usd"] == 300
    assert default_agent("claude_api").monthly_budget_usd == Decimal("300")
    assert default_agent("antigravity").poll_s == 60


def test_the_defaults_file_defaults_to_the_config_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("BATON_CONFIG")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert defaults_path() == tmp_path / ".config/baton/defaults.toml"


@pytest.mark.parametrize(
    "text",
    [
        "[agents.claude_api]\nmonthly_budget_usd = 0\n",
        "[agents.claude_api]\nmonthly_budget_usd = -5\n",
        "[agents.claude_api]\nmonthly_budget_usd = nan\n",
        "[agents.claude_api]\nmonthly_budget_usd = inf\n",
        '[agents.claude_api]\nmonthly_budget_usd = "200"\n',
        "[agents.claude_api]\nmonthly_budget_usd = true\n",
        "[agents.claude_api]\nreserve_pct = 0\n",
        "[agents.claude_api]\nreserve_pct = 100\n",
        "[agents.claude_api]\nreserve_pct = 10.5\n",
        "[agents.claude_api]\nround_budget_usd = 0\n",
        "[agents.claude_api]\nround_budget_usd = -1\n",
        "[agents.claude_api]\nround_budget_usd = nan\n",
        "[agents.claude_api]\nbudget_reset_day = 0\n",
        "[agents.claude_api]\nbudget_reset_day = 32\n",
        "[agents.claude_api]\nbudget_reset_day = 1.5\n",
        '[agents.claude_api]\nkey_file = ""\n',
        "[agents.claude_api]\nadmin_key_file = 5\n",
        "[agents.claude_api]\nthreshold_pct = 90\n",
        "[agents.claude_api]\npoll_s = 15\n",
        "[agents.claude_api]\nfull_access = false\n",
        "[agents.claude_api]\ntypo = 1\n",
        "[agents.codex]\nmonthly_budget_usd = 5\n",
        "[agents.nope]\nenabled = true\n",
        "[agents.claude_api]\nenabled = 1\n",
        'agents = "x"\n',
        "[other]\nx = 1\n",
        "name = ",
    ],
)
def test_invalid_defaults_raise(tmp_path, monkeypatch, text):
    path = defaults(tmp_path, monkeypatch, text)
    with pytest.raises(GoalError, match="defaults.toml"):
        load_defaults()
    with pytest.raises(GoalError):
        load_goal(write(tmp_path, minimal()))
    with pytest.raises(GoalError):
        default_agent("codex")
    assert path.exists()


def test_an_unreadable_defaults_file_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("BATON_CONFIG", str(tmp_path))  # a directory
    with pytest.raises(GoalError):
        load_defaults()


@pytest.mark.parametrize(
    "fields",
    [
        {"round_budget_usd": 0},
        {"round_budget_usd": -1},
        {"round_budget_usd": "nan"},
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


def test_bounds_of_claude_api_values_are_accepted(tmp_path, monkeypatch):
    defaults(
        tmp_path,
        monkeypatch,
        "[agents.claude_api]\nmonthly_budget_usd = 0.01\nreserve_pct = 1\nbudget_reset_day = 31\n",
    )
    load_goal(write(tmp_path, minimal() + claude(round_budget_usd=0.01)))
    defaults(tmp_path, monkeypatch, "[agents.claude_api]\nreserve_pct = 99\nbudget_reset_day = 1\n")
    load_goal(write(tmp_path, minimal()))


def test_claude_api_can_be_the_only_agent(tmp_path):
    text = minimal().replace('objective = "Ship it."', 'objective = "x"\norder = ["claude_api"]')
    assert load_goal(write(tmp_path, text)).order == ("claude_api",)


def test_a_goal_file_may_set_poll_s(tmp_path):
    path = tmp_path / "goal.toml"
    path.write_text(
        f'name = "demo"\nworkdir = "{tmp_path}"\nobjective = "x"\n'
        "[agents.codex]\npoll_s = 120\n[agents.antigravity]\npoll_s = 240\n"
    )
    goal = load_goal(path)
    assert (goal.agents["codex"].poll_s, goal.agents["antigravity"].poll_s) == (120, 240)


# long-term memory


@pytest.fixture(autouse=True)
def memory_not_off(monkeypatch):
    """The suite runs with BATON_MEMORY=off; load_memory is read here as a person would have it."""
    monkeypatch.delenv("BATON_MEMORY", raising=False)


def test_memory_defaults_match_spec(tmp_path):
    assert load_memory() == MemoryConfig(
        enabled=True, bin="ai-memory", port=18778, recall_chars=6000
    )


def test_memory_table_is_read_from_the_defaults_file(tmp_path, monkeypatch):
    defaults(
        tmp_path,
        monkeypatch,
        '[memory]\nenabled = false\nbin = "~/bin/ai-memory"\nport = 20000\nrecall_chars = 0\n',
    )
    cfg = load_memory()
    assert not cfg.enabled and cfg.port == 20000 and cfg.recall_chars == 0
    assert cfg.bin == str(Path("~/bin/ai-memory").expanduser())


@pytest.mark.parametrize(
    "line",
    [
        "bogus = 1",
        "port = 1023",
        "port = 65536",
        "port = true",
        'port = "18778"',
        "recall_chars = -1",
        "recall_chars = 20001",
        "recall_chars = 1.5",
        'enabled = "yes"',
        'bin = ""',
    ],
)
def test_a_bad_memory_value_is_refused(tmp_path, monkeypatch, line):
    defaults(tmp_path, monkeypatch, f"[memory]\n{line}\n")
    with pytest.raises(GoalError, match=r"defaults\.toml"):
        load_memory()


@pytest.mark.parametrize("line", ["port = 1024", "port = 65535", "recall_chars = 20000"])
def test_memory_bounds_are_accepted(tmp_path, monkeypatch, line):
    defaults(tmp_path, monkeypatch, f"[memory]\n{line}\n")
    load_memory()


def test_memory_must_be_a_table(tmp_path, monkeypatch):
    defaults(tmp_path, monkeypatch, "memory = 1\n")
    with pytest.raises(GoalError, match="memory must be a table"):
        load_memory()


def test_the_defaults_file_accepts_a_memory_table_without_judging_it(tmp_path, monkeypatch):
    """A bad [memory] must not keep a goal from loading: the runner turns memory off instead."""
    defaults(tmp_path, monkeypatch, "[memory]\nport = 5\n[agents.codex]\npoll_s = 7\n")
    assert load_defaults() == {"codex": {"poll_s": 7}}
    assert load_goal(write(tmp_path, minimal())).agents["codex"].poll_s == 7


def test_baton_memory_off_wins_over_the_file(tmp_path, monkeypatch):
    defaults(tmp_path, monkeypatch, "[memory]\nenabled = true\nport = 20000\n")
    monkeypatch.setenv("BATON_MEMORY", "off")
    assert load_memory().enabled is False
    monkeypatch.setenv("BATON_MEMORY", "OFF")
    assert load_memory().enabled is False
    monkeypatch.setenv("BATON_MEMORY", "on")
    assert load_memory().enabled is True


def test_a_goal_has_memory_on_unless_it_says_otherwise(tmp_path):
    assert load_goal(write(tmp_path, minimal())).memory is True
    off = minimal().replace('objective = "Ship it."', 'objective = "x"\nmemory = false')
    assert load_goal(write(tmp_path, off)).memory is False


def test_goal_memory_must_be_a_bool(tmp_path):
    bad = minimal().replace('objective = "Ship it."', 'objective = "x"\nmemory = "off"')
    with pytest.raises(GoalError, match="memory must be true or false"):
        load_goal(write(tmp_path, bad))


def test_a_repository_table_may_set_memory(tmp_path):
    goal = goal_from_table(
        {"memory": False},
        name="demo",
        workdir=tmp_path,
        objective="x",
        path=tmp_path / "baton.toml",
    )
    assert goal.memory is False


# --- phases ----------------------------------------------------------------------------------


@pytest.mark.phases
def test_phases_default_to_on_and_can_be_turned_off(tmp_path):
    assert load_goal(write(tmp_path, minimal())).phases is True
    off = minimal().replace('objective = "Ship it."', 'objective = "x"\nphases = false')
    assert load_goal(write(tmp_path, off)).phases is False


def test_the_test_default_is_the_plain_relay(tmp_path):
    # the autouse fixture turns phases off for every test that is not about them
    assert load_goal(write(tmp_path, minimal())).phases is False


@pytest.mark.phases
def test_phases_must_be_a_boolean(tmp_path):
    bad = minimal().replace('objective = "Ship it."', 'objective = "x"\nphases = "yes"')
    with pytest.raises(GoalError, match="phases"):
        load_goal(write(tmp_path, bad))


def test_every_agent_has_a_build_tier_by_default(tmp_path):
    agents = load_goal(write(tmp_path, minimal())).agents
    assert (agents["codex"].model, agents["codex"].effort) == ("gpt-6.1-sol", "medium")
    assert (agents["codex"].build_model, agents["codex"].build_effort) == ("gpt-6.1-terra", "high")
    assert agents["antigravity"].model == "gemini-4-argon-medium"
    assert agents["antigravity"].build_model == "gemini-3.8-flash-high"
    assert (agents["claude_api"].model, agents["claude_api"].effort) == (
        "claude-opus-5-5",
        "medium",
    )
    assert (agents["claude_api"].build_model, agents["claude_api"].build_effort) == (
        "claude-sonnet-5-5",
        "high",
    )


def test_build_models_can_be_set_in_a_goal_and_in_the_defaults(tmp_path, monkeypatch):
    defaults(tmp_path, monkeypatch, '[agents.codex]\nbuild_model = "x-terra"\n')
    text = minimal() + '[agents.codex]\nbuild_effort = "low"\n[agents.claude_api]\n'
    text += 'build_model = "claude-sonnet-9"\n'
    agents = load_goal(write(tmp_path, text)).agents
    assert (agents["codex"].build_model, agents["codex"].build_effort) == ("x-terra", "low")
    assert agents["claude_api"].build_model == "claude-sonnet-9"


def test_the_antigravity_build_model_must_be_a_gemini(tmp_path):
    text = minimal() + '[agents.antigravity]\nbuild_model = "gpt-6.1-terra"\n'
    with pytest.raises(GoalError, match="build_model"):
        load_goal(write(tmp_path, text))


def test_a_repository_file_may_set_phases_and_build_models():
    check_repo_table({"phases": False, "agents": {"codex": {"build_model": "m"}}})


# --- one-shot goal ---------------------------------------------------------------------------


def test_oneshot_is_off_by_default_and_can_be_turned_on(tmp_path):
    assert load_goal(write(tmp_path, minimal())).oneshot is False
    on = minimal().replace('objective = "Ship it."', 'objective = "x"\noneshot = true')
    assert load_goal(write(tmp_path, on)).oneshot is True


def test_oneshot_must_be_a_boolean(tmp_path):
    bad = minimal().replace('objective = "Ship it."', 'objective = "x"\noneshot = "yes"')
    with pytest.raises(GoalError, match="oneshot"):
        load_goal(write(tmp_path, bad))


def test_a_repo_table_may_not_set_oneshot(tmp_path):
    # It would pin text an agent wrote as the person's goal: only the person's goal file may.
    with pytest.raises(GoalError, match="oneshot"):
        check_repo_table({"oneshot": True})
    check_repo_table({"phases": True})  # the rest of the table is judged elsewhere

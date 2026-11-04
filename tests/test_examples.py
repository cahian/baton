"""The examples in the repository must stay valid: they are what the README tells people to copy."""

import plistlib
from decimal import Decimal
from pathlib import Path

from baton.config import load_defaults, load_goal

EXAMPLES = Path(__file__).parent.parent / "examples"


def test_example_goal_loads(monkeypatch, tmp_path):
    monkeypatch.setenv("BATON_CONFIG", str(tmp_path / "none.toml"))
    goal = load_goal(EXAMPLES / "goal.toml")
    assert goal.order == ("claude_api", "antigravity", "codex")
    assert goal.verify_command == "./check.sh"
    assert goal.agents["claude_api"].model == "claude-opus-5-5"


def test_example_defaults_load(monkeypatch):
    monkeypatch.setenv("BATON_CONFIG", str(EXAMPLES / "defaults.toml"))
    tables = load_defaults()
    assert tables["claude_api"]["monthly_budget_usd"] == Decimal("200")
    assert set(tables) == {"claude_api", "antigravity", "codex"}


def test_example_goal_and_defaults_compose(monkeypatch):
    monkeypatch.setenv("BATON_CONFIG", str(EXAMPLES / "defaults.toml"))
    goal = load_goal(EXAMPLES / "goal.toml")
    assert goal.agents["claude_api"].monthly_budget_usd == Decimal("200")


def test_example_plist_runs_baton_serve_and_keeps_it_alive():
    plist = plistlib.loads((EXAMPLES / "baton-serve.plist").read_bytes())
    assert plist["ProgramArguments"][1] == "serve"
    assert plist["KeepAlive"] is True

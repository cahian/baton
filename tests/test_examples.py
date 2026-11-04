"""The examples in the repository must stay valid: they are what the README tells people to copy."""

import plistlib
from decimal import Decimal
from pathlib import Path

from baton.config import load_defaults, load_goal
from baton.gateway import SHUTDOWN_S

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
    assert plist["ExitTimeOut"] > SHUTDOWN_S


def test_example_plist_holds_placeholders_and_absolute_paths():
    plist = plistlib.loads((EXAMPLES / "baton-serve.plist").read_bytes())
    assert plist["ProgramArguments"][0].startswith("/path/to/")
    assert plist["StandardOutPath"].startswith("/path/to/")
    path = plist["EnvironmentVariables"]["PATH"].split(":")
    assert "/path/to/node/bin" in path and "/path/to/agent/bin" in path


def test_readme_points_launchd_users_at_install_service():
    text = (EXAMPLES.parent / "README.md").read_text()
    assert "baton install-service" in text
    assert "`node`" in text


ROOT = EXAMPLES.parent


def test_ci_installs_from_the_lock_file_and_is_ascii():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "uv sync --locked" in workflow
    assert workflow.isascii()


def test_readme_states_what_state_dir_does_not_move_and_documents_jobs():
    text = (ROOT / "README.md").read_text()
    assert "`BATON_STATE_DIR` does not move the Claude ledger" in text
    assert "`baton jobs`" in text
    assert "`baton status` and reattaching still work" not in text

"""The examples in the repository must stay valid: they are what the README tells people to copy."""

import plistlib
from decimal import Decimal
from pathlib import Path

from baton.config import MemoryConfig, load_defaults, load_goal, load_memory
from baton.gateway import SHUTDOWN_S

EXAMPLES = Path(__file__).parent.parent / "examples"


def test_example_goal_loads(monkeypatch, tmp_path):
    monkeypatch.setenv("BATON_CONFIG", str(tmp_path / "none.toml"))
    goal = load_goal(EXAMPLES / "goal.toml")
    assert goal.order == ("codex", "antigravity", "claude_api")
    assert goal.verify_command == "./check.sh"
    assert goal.agents["claude_api"].model == "claude-opus-5-5"
    assert goal.agents["claude_api"].round_budget_usd == Decimal("5")


def test_example_defaults_load(monkeypatch):
    monkeypatch.setenv("BATON_CONFIG", str(EXAMPLES / "defaults.toml"))
    tables = load_defaults()
    assert tables["claude_api"]["monthly_budget_usd"] == Decimal("200")
    assert tables["claude_api"]["round_budget_usd"] == Decimal("5")
    assert set(tables) == {"claude_api", "antigravity", "codex"}


def test_example_goal_and_defaults_compose(monkeypatch):
    monkeypatch.setenv("BATON_CONFIG", str(EXAMPLES / "defaults.toml"))
    goal = load_goal(EXAMPLES / "goal.toml")
    assert goal.agents["claude_api"].monthly_budget_usd == Decimal("200")


def test_example_memory_settings_are_the_defaults(monkeypatch):
    monkeypatch.setenv("BATON_CONFIG", str(EXAMPLES / "defaults.toml"))
    monkeypatch.delenv("BATON_MEMORY", raising=False)
    assert load_memory() == MemoryConfig()
    assert load_goal(EXAMPLES / "goal.toml").memory is True


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


def test_readme_explains_the_memory_without_wiring_global_hooks():
    text = (ROOT / "README.md").read_text()
    section = text.split("## Long-term memory", 1)[1].split("\n## ", 1)[0]
    assert "ai-memory setup" in section  # named, to say it is never run
    assert "BATON_MEMORY=off" in section and "memory = false" in section
    quick_start = text.split("## Quick start", 1)[1].split("\n## ", 1)[0]
    assert "ai-memory" in quick_start and "Optional" in quick_start


def test_readme_says_plainly_that_claude_is_optional():
    text = (ROOT / "README.md").read_text()
    intro = text.split("## Quick start", 1)[0]
    assert "works with Codex and Antigravity alone" in intro and "optional" in intro
    quick_start = text.split("## Quick start", 1)[1].split("\n## ", 1)[0]
    assert "### 3. Add the Claude key (optional)" in quick_start
    assert "(only with the backup)" not in quick_start
    assert (
        "| `claude` |" in quick_start and "optional" in quick_start.split("| `claude` |")[1][:120]
    )
    step2 = quick_start.split("### 2.", 1)[1].split("### 3.", 1)[0]
    assert "Claude is optional" in step2 and "skip step 3" in step2
    rotation = text.split("## The rotation", 1)[1].split("\n## ", 1)[0]
    assert "**Claude is optional.**" in rotation and "enabled = false" in rotation


def test_the_spec_mirrors_the_claude_optional_line_and_the_memory_changes():
    spec = (ROOT / "docs/specs/2026-11-04-baton-design.md").read_text()
    assert "claude_api: not configured (optional): rounds use the other agents only" in spec
    assert "Codex and Antigravity only" not in spec
    assert "Claude is optional" in spec
    assert "<state base>/.ai-memory" in spec and "<state base>/memory" not in spec
    assert "at most 16 from the last gate output" in spec


def test_readme_names_the_dot_directory_for_the_memory_data():
    text = (ROOT / "README.md").read_text()
    assert "~/.local/state/baton/.ai-memory" in text
    assert "baton/memory`" not in text


def test_readme_and_spec_say_the_memory_fixes():
    readme = (ROOT / "README.md").read_text()
    spec = (ROOT / "docs/specs/2026-11-04-baton-design.md").read_text()
    assert "re-run `baton install-service`" in readme
    assert "stop the old server" in readme or "starts no second one" in readme
    assert "is lost" not in readme  # a killed runner no longer loses the round's page
    assert "--- BEGIN BATON MEMORY ---" in readme and "--- BEGIN BATON MEMORY ---" in spec
    assert "`interrupted`" in readme and "`interrupted`" in spec
    assert "re-run it after installing ai-memory" in spec
    assert "the END of each text" in spec

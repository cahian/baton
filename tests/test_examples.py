"""The examples in the repository must stay valid: they are what the README tells people to copy."""

import plistlib
from decimal import Decimal
from pathlib import Path

from baton.config import MemoryConfig, load_defaults, load_goal, load_memory, load_superpowers
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


def what_you_need(text):
    return text.split("## What you need", 1)[1].split("\n## ", 1)[0]


def row(table, name):
    (line,) = [x for x in table.splitlines() if x.startswith("|") and name in x.split("|")[1]]
    return line


def test_readme_says_plainly_that_claude_is_optional():
    text = (ROOT / "README.md").read_text()
    needs = what_you_need(text)
    intro = text.split("## Quick start", 1)[0]
    assert "works with Codex and Antigravity alone" in intro and "optional" in intro
    quick_start = text.split("## Quick start", 1)[1].split("\n## ", 1)[0]
    assert "### 3. Add the Claude key (optional)" in quick_start
    assert "(only with the backup)" not in quick_start
    assert "Claude Code" in needs and "optional" in needs.split("Claude Code")[1][:200]
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


def test_what_you_need_comes_between_the_intro_and_the_quick_start():
    text = (ROOT / "README.md").read_text()
    assert text.index("## What you need") < text.index("## Quick start")
    assert text.index("# baton") < text.index("## What you need")
    assert "**How the pieces fit.**" in what_you_need(text)
    assert text.rstrip().split("\n## ")[-1].startswith("Credits")


def test_what_you_need_pins_the_superpowers_row():
    superpowers = row(what_you_need((ROOT / "README.md").read_text()), "Superpowers")
    assert "https://github.com/obra/superpowers" in superpowers
    assert "**required**" in superpowers and "no round runs without it" in superpowers
    assert "#superpowers-is-required" in superpowers
    assert (
        "git clone https://github.com/obra/superpowers ~/.config/baton/superpowers" in superpowers
    )


def test_what_you_need_pins_the_ai_memory_row():
    ai_memory = row(what_you_need((ROOT / "README.md").read_text()), "ai-memory")
    assert "https://github.com/akitaonrails/ai-memory" in ai_memory
    assert "optional" in ai_memory and "release binary" in ai_memory
    assert "ai-memory setup" in ai_memory and "global hooks" in ai_memory


def test_what_you_need_lists_every_project():
    table = what_you_need((ROOT / "README.md").read_text())
    for name in ("uv", "OpenCode", "Codex CLI", "Antigravity CLI", "Claude Code", "launchd"):
        assert row(table, name)
    assert "baton run" in row(table, "OpenCode")
    assert "Console API key" in row(table, "Claude Code")


def test_the_credits_name_both_projects_and_say_none_is_bundled():
    credits = (ROOT / "README.md").read_text().split("## Credits", 1)[1]
    assert "Jesse Vincent" in credits and "MIT" in credits
    assert "ai-memory" in credits and "bundles none" in credits and "copies none" in credits


def test_the_spec_and_readme_carry_the_review_guards_and_the_subagent_rule():
    readme = (ROOT / "README.md").read_text()
    spec = (ROOT / "docs/specs/2026-11-04-baton-design.md").read_text()
    for text in (readme, spec):
        assert "reviewer changed the code" in text
        assert "Subagent" in text or "subagent" in text
    assert "### Subagent guard" in spec and "### Superpowers" in spec
    assert "Stale verdict" in spec and "A reviewer that edits code" in spec


def test_example_files_carry_the_phase_settings(monkeypatch):
    monkeypatch.setenv("BATON_CONFIG", str(EXAMPLES / "defaults.toml"))
    goal = load_goal(EXAMPLES / "goal.toml")
    assert goal.phases is True
    assert goal.agents["codex"].build_model == "gpt-6.1-terra"
    assert goal.agents["antigravity"].build_model == "gemini-3.8-flash-high"
    assert goal.agents["claude_api"].build_model == "claude-sonnet-5-5"
    assert load_superpowers().dir == ""


def test_the_readme_has_a_section_that_says_superpowers_is_required():
    text = (ROOT / "README.md").read_text()
    assert "\n## Superpowers is required\n" in text
    section = " ".join(
        text.split("\n## Superpowers is required\n", 1)[1].split("\n## ", 1)[0].split()
    )
    assert "no setting to turn it off" in section
    assert "No round runs without it" in section
    assert "working method for every provider" in section
    assert text.index("## What you need") < text.index("## Superpowers is required")
    assert text.index("## Superpowers is required") < text.index("## Quick start")
    # the table row, the Quick start step and the Phases section say the same
    assert (
        "no round runs without Superpowers"
        in text.split("### 1. Install baton, and Superpowers")[1].split("###")[0]
    )
    phases = text.split("## Phases: plan, build, review", 1)[1].split("\n## ", 1)[0]
    assert "required" in phases and "(#superpowers-is-required)" in phases


def test_the_readme_and_spec_say_claude_keeps_subagents_with_a_native_block():
    readme = (ROOT / "README.md").read_text()
    spec = (ROOT / "docs/specs/2026-11-04-baton-design.md").read_text()
    for text in (readme, spec):
        assert "PreToolUse" in text and "soft limit" in text
        assert "--bare" not in text.replace("There is no `--bare`", "").replace("not --bare", "")
    assert "subagent_guard.py" in spec and "fails closed" in spec
    assert "a gitignored file is not detected" in spec.replace(
        "editing a gitignored file", "a gitignored file"
    )


def test_docs_say_claude_and_codex_block_and_antigravity_ends_the_round():
    readme = (ROOT / "README.md").read_text()
    spec = (ROOT / "docs/specs/2026-11-04-baton-design.md").read_text()
    flat = " ".join(readme.split())
    assert "**Antigravity:** no hook" in flat and "Antigravity has no hook" in spec
    assert "only logs" in flat and "CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1" in spec
    assert "hooks.PreToolUse" in spec and "--dangerously-bypass-hook-trust" in spec
    assert "CLAUDE_CODE_DISABLE_AUTO_MEMORY=1" in spec
    for text in (flat, " ".join(spec.split())):
        assert "AWS_BEARER_TOKEN_BEDROCK" in text and "CLAUDE_CODE_OAUTH_*" in text


def test_superpowers_is_found_automatically_only_from_two_places_and_the_docs_say_so():
    readme = " ".join((ROOT / "README.md").read_text().split())
    defaults = " ".join((EXAMPLES / "defaults.toml").read_text().split())
    assert "official Claude Code plugin install" in readme and "needs `[superpowers] dir`" in readme
    assert "official Claude Code plugin install" in defaults and "needs `dir`" in defaults


def test_what_you_need_states_the_versions_it_was_tested_with():
    needs = " ".join(what_you_need((ROOT / "README.md").read_text()).split())
    for version in ("Codex CLI 0.166.0", "Claude Code 2.1.318", "ai-memory 3.4.1"):
        assert version in needs


def test_docs_say_what_the_subagent_guard_really_does_per_provider():
    flat = " ".join((ROOT / "README.md").read_text().split())
    spec = " ".join((ROOT / "docs/specs/2026-11-04-baton-design.md").read_text().split())
    for text in (flat, spec):
        assert "drop the `model` parameter" in text  # Claude: replaced, not denied
        assert "may not appear in the stream" in text or "does not appear in the stream" in text
        assert "unless a role pins one" in text
    assert "at most 2 times" in flat and "third `CHANGES` ends the cycle" in flat
    assert "--dangerously-bypass-hook-trust" in flat and "no new capability" in flat
    assert "features.hooks=true" in spec and "CLAUDE_CONFIG_DIR" in spec

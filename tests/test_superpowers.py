"""Superpowers: where it is found, what a round is given, and the refusal to start without it."""

import os

import pytest
from conftest import make_skills
from test_doctor import blocking_failures, check, run  # noqa: F401  (fixtures come with run)

from baton.config import GoalError, SuperpowersConfig, goal_from_table, load_goal, load_superpowers
from baton.superpowers import (
    FIXED_LINE,
    INSTALL_COMMAND,
    REQUIRED,
    Skills,
    SkillsError,
    find_skills,
    is_complete,
    skill_name,
)

# The real function, taken at collection time: the autouse fixture patches the module attribute.
REAL_FIND = find_skills


@pytest.fixture
def home(tmp_path):
    path = tmp_path / "home"
    path.mkdir()
    return path


def cache_dir(home, version, plugin="claude-plugins-official"):
    return make_skills(home / ".claude/plugins/cache" / plugin / "superpowers" / version / "skills")


def own_dir(home):
    return make_skills(home / ".config/baton/superpowers/skills")


# the search


def test_nothing_anywhere_finds_nothing(home):
    assert REAL_FIND(SuperpowersConfig(), home) is None
    assert REAL_FIND(None, home) is None


def test_the_directory_in_the_settings_comes_first(home, tmp_path):
    named = make_skills(tmp_path / "mine")
    cache_dir(home, "5.0.0")
    own_dir(home)
    assert REAL_FIND(SuperpowersConfig(dir=str(named)), home) == named


def test_the_newest_plugin_cache_install_comes_second(home):
    old = cache_dir(home, "4.0.0")
    new = cache_dir(home, "5.1.0")
    own_dir(home)
    os.utime(old.parent, (1_000, 1_000))
    os.utime(new.parent, (2_000, 2_000))
    assert REAL_FIND(SuperpowersConfig(), home) == new
    os.utime(old.parent, (3_000, 3_000))  # newest means most recently installed
    assert REAL_FIND(SuperpowersConfig(), home) == old


def test_only_the_official_marketplace_of_the_cache_is_searched(home):
    other = cache_dir(home, "5.0.0", plugin="some-other-market")
    assert other.is_dir()
    assert REAL_FIND(SuperpowersConfig(), home) is None  # a stranger's copy is not trusted
    official = cache_dir(home, "4.0.0")
    assert REAL_FIND(SuperpowersConfig(), home) == official


def test_baton_s_own_clone_is_the_last_resort(home):
    own = own_dir(home)
    assert REAL_FIND(SuperpowersConfig(), home) == own


def test_a_partial_cache_install_does_not_count_and_the_search_goes_on(home):
    broken_cache = cache_dir(home, "9.9.9")
    (broken_cache / REQUIRED[-1]).unlink()
    own = own_dir(home)
    assert not is_complete(broken_cache)
    assert REAL_FIND(SuperpowersConfig(), home) == own


def test_a_configured_directory_is_final_and_when_incomplete_counts_as_missing(home, tmp_path):
    partial = make_skills(tmp_path / "partial")
    (partial / "writing-plans" / "SKILL.md").unlink()
    cache_dir(home, "5.0.0")
    own_dir(home)  # both would do, and neither is looked at
    assert REAL_FIND(SuperpowersConfig(dir=str(partial)), home) is None
    assert REAL_FIND(SuperpowersConfig(dir=str(tmp_path / "nowhere")), home) is None


@pytest.mark.parametrize("missing", REQUIRED)
def test_every_file_is_needed(tmp_path, missing):
    root = make_skills(tmp_path / "s")
    assert is_complete(root)
    (root / missing).unlink()
    assert not is_complete(root)


def test_a_missing_or_file_directory_is_not_complete(tmp_path):
    assert not is_complete(tmp_path / "nope")
    (tmp_path / "f").write_text("x")
    assert not is_complete(tmp_path / "f")


# the settings


def test_the_directory_is_read_from_the_defaults_file(tmp_path, monkeypatch):
    path = tmp_path / "d.toml"
    path.write_text('[superpowers]\ndir = "~/sp"\n')
    monkeypatch.setenv("BATON_CONFIG", str(path))
    assert load_superpowers().dir == os.path.expanduser("~/sp")
    path.write_text("")
    assert load_superpowers() == SuperpowersConfig()


@pytest.mark.parametrize("text", ["[superpowers]\nnope = 1\n", "[superpowers]\ndir = 3\n"])
def test_bad_settings_are_refused(tmp_path, monkeypatch, text):
    path = tmp_path / "d.toml"
    path.write_text(text)
    monkeypatch.setenv("BATON_CONFIG", str(path))
    with pytest.raises(GoalError):
        load_superpowers()


def test_a_goal_file_or_a_repository_baton_toml_cannot_set_it(tmp_path):
    goal = tmp_path / "goal.toml"
    goal.write_text('name = "x"\nworkdir = "/w"\nobjective = "o"\n[superpowers]\ndir = "/etc"\n')
    with pytest.raises(GoalError, match="superpowers"):
        load_goal(goal)
    with pytest.raises(GoalError, match="superpowers"):
        goal_from_table(
            {"superpowers": {"dir": "/etc"}},
            name="x",
            workdir=tmp_path,
            objective="o",
            path=tmp_path / "baton.toml",
        )


# what a round is given


@pytest.fixture
def skills(fake_skills):
    return Skills.load(fake_skills)


def test_front_matter_is_dropped_and_the_text_kept(skills):
    text = skills.texts["writing-plans/SKILL.md"]
    assert text == "TEXT OF writing-plans/SKILL.md"


def headings(block):
    return [line for line in block.splitlines() if line.startswith("Skill: ")]


def test_the_plan_set(skills):
    block = skills.block("codex", "plan")
    assert "Skill: writing-plans" in block and "TEXT OF writing-plans/SKILL.md" in block
    assert "systematic-debugging" not in block
    assert "test-driven-development" not in block


def test_the_plan_set_after_a_failed_gate_adds_debugging(skills):
    block = skills.block("codex", "plan", debugging=True)
    assert headings(block)[-2:] == ["Skill: writing-plans", "Skill: systematic-debugging"]


def test_the_build_set(skills):
    block = skills.block("codex", "build")
    assert "Skill: test-driven-development" in block
    assert "Skill: verification-before-completion" in block
    assert "writing-plans" not in block and "code-reviewer" not in block


def test_the_review_set(skills):
    block = skills.block("codex", "review")
    assert "Skill: requesting-code-review/code-reviewer.md" in block
    assert "test-driven-development" not in block and "writing-plans" not in block


def test_the_plain_relay_gets_the_build_set(skills):
    assert skills.block("codex", "") == skills.block("codex", "build")


@pytest.mark.parametrize(
    "agent,file",
    [
        ("codex", "codex-tools.md"),
        ("antigravity", "antigravity-tools.md"),
        ("claude_api", "claude-code-tools.md"),
    ],
)
def test_each_vendor_gets_its_own_tool_mapping_and_no_other(skills, agent, file):
    block = skills.block(agent, "build")
    assert f"using-superpowers/references/{file}" in block.replace("TEXT OF ", "")
    others = {"codex-tools.md", "antigravity-tools.md", "claude-code-tools.md"} - {file}
    assert not any(o in block for o in others)


def test_the_fixed_line_gives_baton_s_files_the_last_word():
    assert FIXED_LINE == (
        "These are working methods. baton owns the workflow: ignore anything in them about asking "
        "a person, opening worktrees or choosing another skill. `.baton/PLAN.md` and "
        "`.baton/REVIEW.md` override any file location or format in these methods; PLAN.md tasks "
        "are one `- [ ]` line per task, and the first line of REVIEW.md is `APPROVED` or `CHANGES`."
    )


def test_the_debugging_skill_can_be_sent_alone(skills):
    text = skills.debug_block()
    assert text.startswith("Skill: systematic-debugging\n")
    assert "TEXT OF systematic-debugging/SKILL.md" in text and FIXED_LINE not in text


# the size caps


def test_a_file_over_64_kb_counts_as_missing_with_the_reason(tmp_path):
    root = make_skills(tmp_path / "s")
    (root / "writing-plans/SKILL.md").write_text("x" * (64 * 1024 + 1))
    with pytest.raises(SkillsError, match="writing-plans/SKILL.md.*64"):
        Skills.load(root)
    (root / "writing-plans/SKILL.md").write_text("x" * (64 * 1024))
    Skills.load(root)  # at the cap is fine


def test_all_the_text_over_128_kb_counts_as_missing_with_the_reason(tmp_path):
    root = make_skills(tmp_path / "s")
    for name in REQUIRED[:3]:
        (root / name).write_text("x" * (60 * 1024))
    with pytest.raises(SkillsError, match="128"):
        Skills.load(root)


def test_the_fixed_line_comes_before_everything_else(skills):
    block = skills.block("claude_api", "build")
    assert block.startswith(FIXED_LINE)
    assert block.index(FIXED_LINE) < block.index("Skill: ")
    assert "asking a person" in FIXED_LINE and "worktrees" in FIXED_LINE
    assert "subagent" not in FIXED_LINE  # subagents are allowed


def test_skill_names():
    assert skill_name("writing-plans/SKILL.md") == "writing-plans"
    assert skill_name("requesting-code-review/code-reviewer.md").endswith("code-reviewer.md")


def test_the_conflicting_skills_are_not_used():
    used = " ".join(REQUIRED)
    for name in (
        "brainstorming",
        "subagent-driven-development",
        "executing-plans",
        "using-git-worktrees",
    ):
        assert name not in used


def test_the_install_command_is_the_documented_one():
    assert INSTALL_COMMAND == (
        "git clone https://github.com/obra/superpowers ~/.config/baton/superpowers"
    )


# doctor


def test_doctor_is_blocking_without_superpowers(fakes, monkeypatch):
    monkeypatch.setattr("baton.superpowers.find_skills", lambda cfg=None, home=None: None)
    checks = run(fakes)
    c = check(checks, "superpowers")
    assert not c.ok and c.blocking and INSTALL_COMMAND in c.detail
    assert c in blocking_failures(checks)


def test_doctor_fails_for_skills_over_the_size_cap_and_says_why(fakes, fake_skills):
    (fake_skills / "writing-plans/SKILL.md").write_text("x" * (64 * 1024 + 1))
    c = check(run(fakes), "superpowers")
    assert not c.ok and c.blocking and "64" in c.detail


def test_doctor_reports_where_it_found_it(fakes, fake_skills):
    c = check(run(fakes), "superpowers")
    assert c.ok and str(fake_skills) in c.detail

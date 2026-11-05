"""Superpowers: the working methods every round is given, found on disk and read once.

baton owns the workflow. It takes the skill files of https://github.com/obra/superpowers that
fit a relay (planning, test-driven building, verification, code review) and puts their text in a
round's prompt, under the phase that uses them. Skills that conflict with baton's workflow are not
used. The files are read from the user's own install, never copied into this repository.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from baton.config import SuperpowersConfig

INSTALL_COMMAND = "git clone https://github.com/obra/superpowers ~/.config/baton/superpowers"
FIXED_LINE = (
    "These are working methods. baton owns the workflow: ignore anything in them about asking a "
    "person, opening worktrees or choosing another skill. `.baton/PLAN.md` and "
    "`.baton/REVIEW.md` override any file location or format in these methods; PLAN.md tasks are "
    "one `- [ ]` line per task, and the first line of REVIEW.md is `APPROVED` or `CHANGES`."
)
# What a prompt can carry: a bigger file, or bigger text in all, counts as not installed.
MAX_FILE_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 128 * 1024

# What each phase is given, in order. A skill is a directory with SKILL.md, or a plain file path.
PLAN_SKILLS = ("writing-plans/SKILL.md",)
DEBUG_SKILLS = ("systematic-debugging/SKILL.md",)
BUILD_SKILLS = ("test-driven-development/SKILL.md", "verification-before-completion/SKILL.md")
REVIEW_SKILLS = ("requesting-code-review/code-reviewer.md",)
# The vendor's tool names, so a skill written for Claude Code can be followed on another CLI.
TOOL_MAPS = {
    "codex": "using-superpowers/references/codex-tools.md",
    "antigravity": "using-superpowers/references/antigravity-tools.md",
    "claude_api": "using-superpowers/references/claude-code-tools.md",
}
REQUIRED = (*PLAN_SKILLS, *DEBUG_SKILLS, *BUILD_SKILLS, *REVIEW_SKILLS, *TOOL_MAPS.values())

# Where to look, after the `[superpowers] dir` of the defaults file.
CACHE_GLOB = ".claude/plugins/cache/claude-plugins-official/superpowers/*/skills"
OWN_COPY = ".config/baton/superpowers/skills"

_FRONT_MATTER = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)


def is_complete(path: Path) -> bool:
    """A directory counts only when it holds every skill file baton uses."""
    try:
        return path.is_dir() and all((path / name).is_file() for name in REQUIRED)
    except OSError:
        return False


def _newest_first(paths: list[Path]) -> list[Path]:
    def age(path: Path) -> float:
        try:
            return path.parent.stat().st_mtime  # the version directory: when it was installed
        except OSError:
            return 0.0

    return sorted(paths, key=lambda p: (age(p), str(p)), reverse=True)


def find_skills(cfg: SuperpowersConfig | None = None, home: Path | None = None) -> Path | None:
    """The skills directory to use, or None. A directory named in `[superpowers] dir` is final:
    when it is incomplete, Superpowers counts as missing and nothing else is tried. Otherwise the
    newest install under the official Claude Code plugin cache, else baton's own clone; each must
    be complete."""
    home = home if home is not None else Path.home()
    directory = cfg.dir if cfg is not None else ""
    if directory.strip():
        named = Path(directory).expanduser()
        return named if is_complete(named) else None
    try:
        cached = _newest_first(list(home.glob(CACHE_GLOB)))
    except OSError:
        cached = []
    for path in cached:
        if is_complete(path):
            return path
    own = home / OWN_COPY
    return own if is_complete(own) else None


class SkillsError(Exception):
    """The skills cannot be used: they are too big for a prompt."""


@dataclass(frozen=True)
class Skills:
    """The text of the files, read once."""

    texts: dict[str, str]
    directory: Path

    @classmethod
    def load(cls, directory: Path) -> Skills:
        texts = {}
        total = 0
        for name in REQUIRED:
            data = (directory / name).read_bytes()
            if len(data) > MAX_FILE_BYTES:
                raise SkillsError(
                    f"{name} is over {MAX_FILE_BYTES // 1024} KB, so it counts as missing"
                )
            raw = data.decode("utf-8", errors="replace")
            texts[name] = _FRONT_MATTER.sub("", raw, count=1).strip()
            total += len(texts[name].encode("utf-8"))
        if total > MAX_TOTAL_BYTES:
            raise SkillsError(
                f"the skills hold over {MAX_TOTAL_BYTES // 1024} KB of text in all, so they count "
                "as missing"
            )
        return cls(texts, directory)

    def debug_block(self) -> str:
        """`systematic-debugging` alone: for the plan round that follows a failed gate, even in a
        conversation that already holds the rest."""
        name = DEBUG_SKILLS[0]
        return f"Skill: {skill_name(name)}\n{self.texts[name]}"

    def names(self, phase: str, debugging: bool = False) -> tuple[str, ...]:
        """The files for `phase` ("plan", "build", "review", or "" for the plain relay, which
        gets the build set)."""
        if phase == "plan":
            return PLAN_SKILLS + (DEBUG_SKILLS if debugging else ())
        if phase == "review":
            return REVIEW_SKILLS
        return BUILD_SKILLS

    def block(self, agent: str, phase: str, debugging: bool = False) -> str:
        """The prompt text for a round with a fresh conversation: the fixed line, the vendor's
        tool names, then each skill under a `Skill: <name>` heading."""
        parts = [FIXED_LINE]
        tools = TOOL_MAPS.get(agent)
        if tools:
            parts.append(f"Skill: tool names ({agent})\n{self.texts[tools]}")
        for name in self.names(phase, debugging):
            parts.append(f"Skill: {skill_name(name)}\n{self.texts[name]}")
        return "\n\n".join(parts)


def skill_name(path: str) -> str:
    """`writing-plans` for `writing-plans/SKILL.md`, and `requesting-code-review/code-reviewer.md`
    as it is."""
    return path.removesuffix("/SKILL.md")

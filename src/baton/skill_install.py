"""Install Baton’s portable one-shot skill into the native skill folders for its CLIs."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from baton.store import state_base

SKILL_NAME = "baton-oneshot"
SKILL_SOURCE = Path(__file__).parent / "skills" / SKILL_NAME / "SKILL.md"


class SkillInstallError(Exception):
    """The skill could not be installed without overwriting a different skill."""


def skill_targets(home: Path | None = None) -> list[Path]:
    """Native global skill locations, including the isolated Claude config Baton invokes."""
    home = (home or Path.home()).expanduser()
    codex_home = Path(os.environ.get("CODEX_HOME", str(home / ".codex"))).expanduser()
    roots = [
        codex_home / "skills",
        home / ".agents" / "skills",
        home / ".claude" / "skills",
        state_base() / ".claude-config" / "skills",
        home / ".gemini" / "config" / "skills",
    ]
    found: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        target = root / SKILL_NAME / "SKILL.md"
        key = target.resolve(strict=False)
        if key not in seen:
            seen.add(key)
            found.append(target)
    return found


def install_skill(*, force: bool = False, home: Path | None = None) -> list[tuple[Path, str]]:
    """Copy the packaged skill to each CLI. Existing differing skills require ``force``."""
    try:
        content = SKILL_SOURCE.read_text(encoding="utf-8")
    except OSError as exc:
        raise SkillInstallError(f"cannot read the packaged skill: {exc}") from exc

    targets = skill_targets(home)
    actions: list[tuple[Path, str]] = []
    conflicts: list[Path] = []
    for target in targets:
        try:
            current = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            actions.append((target, "installed"))
        except (OSError, UnicodeError) as exc:
            raise SkillInstallError(f"cannot inspect {target}: {exc}") from exc
        else:
            if current == content:
                actions.append((target, "already current"))
            elif force:
                actions.append((target, "updated"))
            else:
                conflicts.append(target)

    if conflicts:
        locations = "\n".join(f"  {path}" for path in conflicts)
        raise SkillInstallError(
            "a different baton-oneshot skill already exists at:\n"
            f"{locations}\nRerun with `baton skills install --force` to replace it."
        )

    for target, action in actions:
        if action == "already current":
            continue
        temporary: str | None = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".baton-oneshot-", dir=target.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(content)
            os.chmod(temporary, 0o644)
            os.replace(temporary, target)
        except OSError as exc:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
            raise SkillInstallError(f"cannot install the skill at {target}: {exc}") from exc
    return actions

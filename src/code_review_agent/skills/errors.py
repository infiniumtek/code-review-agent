"""Skill registry exceptions."""

from __future__ import annotations

from code_review_agent.utils.state import SkillKind


class MissingSkillError(RuntimeError):
    """Raised when a detected programming language has no matching skill."""

    def __init__(self, skill_key: str, *, kind: SkillKind | None = None) -> None:
        self.skill_key = skill_key
        self.kind = kind
        label = f"{kind} skill" if kind is not None else "skill"
        super().__init__(f"Missing required {label}: {skill_key}")


class SkillBodyLoadError(RuntimeError):
    """Raised when an indexed skill body cannot be loaded later."""

    def __init__(self, skill_key: str, path: str) -> None:
        self.skill_key = skill_key
        self.path = path
        super().__init__(f"Unable to load SKILL.md body for {skill_key}: {path}")

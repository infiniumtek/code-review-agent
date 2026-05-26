"""Provider-agnostic portable SKILL.md loader.

The loader consumes SKILL.md packages as prompt-only review knowledge. It never
executes files from a skill directory: discovery reads only SKILL.md
frontmatter (Level 1), and the body is loaded lazily (Level 2) when a selected
review unit is later prompted.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast

import structlog
import yaml
from pydantic import BaseModel, Field, ValidationError

from code_review_agent.config import (
    ReviewConfig,
    Settings,
    get_settings,
    load_review_config,
    resolved_extra_paths,
)
from code_review_agent.skills.errors import SkillBodyLoadError
from code_review_agent.utils.detect import CI_SKILL_KEYS, EXTENSION_SKILL_KEYS
from code_review_agent.utils.diffing import normalize_repo_path
from code_review_agent.utils.state import ChangedFile, SkillKind, SkillRef

log = structlog.get_logger(__name__)


class SkillFrontmatter(BaseModel):
    """The Level-1 SKILL.md metadata used to build the registry index."""

    name: str
    description: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class IndexedSkill:
    """A registry entry plus all detector keys that can resolve to it."""

    ref: SkillRef
    match_keys: tuple[str, ...]
    extensions: tuple[str, ...]


class SkillRegistry:
    """In-memory index from detected skill key/path metadata to frontmatter."""

    def __init__(self, skills: Iterable[IndexedSkill]) -> None:
        self._by_key: dict[str, IndexedSkill] = {}
        self._by_extension: dict[str, IndexedSkill] = {}
        for skill in skills:
            for key in skill.match_keys:
                normalized = normalize_skill_key(key)
                if not normalized:
                    continue
                if normalized in self._by_key:
                    log.warning(
                        "duplicate_skill_key_ignored",
                        skill_key=normalized,
                        existing_path=self._by_key[normalized].ref.path,
                        ignored_path=skill.ref.path,
                    )
                    continue
                self._by_key[normalized] = skill
            for extension in skill.extensions:
                if extension in self._by_extension:
                    log.warning(
                        "duplicate_skill_extension_ignored",
                        extension=extension,
                        existing_path=self._by_extension[extension].ref.path,
                        ignored_path=skill.ref.path,
                    )
                    continue
                self._by_extension[extension] = skill
        self._extensions_by_length = tuple(sorted(self._by_extension, key=len, reverse=True))

    @classmethod
    def discover(cls, search_paths: Iterable[Path]) -> SkillRegistry:
        """Build a registry from all SKILL.md files under ``search_paths``."""

        indexed: list[IndexedSkill] = []
        seen_paths: set[Path] = set()
        for root in search_paths:
            for skill_path in discover_skill_files(root):
                resolved = skill_path.resolve()
                if resolved in seen_paths:
                    continue
                seen_paths.add(resolved)
                skill = load_skill_index(skill_path)
                if skill is not None:
                    indexed.append(skill)
        return cls(indexed)

    def resolve(self, key: str) -> SkillRef | None:
        """Resolve a detected language/target key to a skill reference."""

        normalized = normalize_skill_key(key)
        if not normalized:
            return None
        indexed = self._by_key.get(normalized)
        if indexed is None:
            return None
        return indexed.ref.model_copy()

    def resolve_file(self, file: ChangedFile) -> SkillRef | None:
        """Resolve a language skill by extension when static detection has no opinion."""

        safe_path = normalize_repo_path(file.path)
        if safe_path is None:
            return None
        path = PurePosixPath(safe_path).as_posix().lower()
        for extension in self._extensions_by_length:
            if path.endswith(extension):
                indexed = self._by_extension[extension]
                if indexed.ref.kind != "language":
                    continue
                return indexed.ref.model_copy()
        return None

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        return normalize_skill_key(key) in self._by_key


def load_skill_registry(
    review_config: ReviewConfig | None = None,
    settings: Settings | None = None,
) -> SkillRegistry:
    """Discover bundled skills plus gated repo-local extra paths."""

    settings = settings or get_settings()
    config = review_config or load_review_config(settings)
    return SkillRegistry.discover(skill_search_paths(config, settings))


def skill_search_paths(config: ReviewConfig, settings: Settings) -> tuple[Path, ...]:
    """Return effective skill roots under the project trust model."""

    paths = [settings.skills_path]
    paths.extend(Path(path) for path in resolved_extra_paths(config, settings))
    return tuple(paths)


def discover_skill_files(root: Path) -> tuple[Path, ...]:
    """Return SKILL.md files under ``root`` without executing package content."""

    if not root.exists():
        log.warning("skills_path_missing", path=str(root))
        return ()
    if root.is_file():
        if root.name == "SKILL.md":
            return (root,)
        log.warning("skills_path_not_directory", path=str(root))
        return ()

    direct = root / "SKILL.md"
    paths: list[Path] = []
    if direct.is_file():
        paths.append(direct)
    paths.extend(sorted(path for path in root.rglob("SKILL.md") if path != direct))
    return tuple(paths)


def load_skill_index(path: Path) -> IndexedSkill | None:
    """Parse one SKILL.md frontmatter block into an indexed registry entry."""

    try:
        frontmatter = _read_frontmatter(path)
    except OSError as exc:
        log.warning("skill_frontmatter_read_failed", path=str(path), error=str(exc))
        return None
    if frontmatter is None:
        log.warning("skill_frontmatter_missing", path=str(path))
        return None

    try:
        raw = yaml.safe_load(frontmatter) or {}
    except yaml.YAMLError as exc:
        log.warning("skill_frontmatter_parse_failed", path=str(path), error=str(exc))
        return None
    if not isinstance(raw, dict):
        log.warning("skill_frontmatter_invalid", path=str(path), reason="not_mapping")
        return None

    try:
        metadata = SkillFrontmatter.model_validate(cast(dict[str, Any], raw))
    except ValidationError as exc:
        log.warning("skill_frontmatter_invalid", path=str(path), error=str(exc))
        return None

    package_key = normalize_skill_key(path.parent.name)
    kind = _skill_kind(metadata.metadata, package_key)
    ref = SkillRef(
        key=package_key,
        name=metadata.name,
        description=metadata.description,
        kind=kind,
        path=str(path),
    )
    extensions = _extensions(metadata.metadata.get("extensions"))
    return IndexedSkill(
        ref=ref,
        match_keys=_match_keys(package_key, metadata.metadata, extensions),
        extensions=extensions,
    )


def load_skill_body(skill: SkillRef) -> str:
    """Load the selected SKILL.md body, excluding YAML frontmatter."""

    try:
        text = Path(skill.path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        log.warning(
            "skill_body_load_failed",
            skill_key=skill.key,
            path=skill.path,
            error=str(exc),
        )
        raise SkillBodyLoadError(skill.key, skill.path) from exc
    return _strip_frontmatter(text).lstrip("\n")


def normalize_skill_key(value: str) -> str:
    """Normalize keys from detector output, directory names, and metadata."""

    return value.strip().lower().replace("_", "-").replace(" ", "-")


def _read_frontmatter(path: Path) -> str | None:
    with path.open(encoding="utf-8") as handle:
        first = handle.readline()
        if not _is_frontmatter_delimiter(first):
            return None
        lines: list[str] = []
        for line in handle:
            if _is_frontmatter_delimiter(line):
                return "".join(lines)
            lines.append(line)
    return None


def _strip_frontmatter(text: str) -> str:
    lines = text.splitlines(keepends=True)
    if not lines or not _is_frontmatter_delimiter(lines[0]):
        return text
    for index, line in enumerate(lines[1:], start=1):
        if _is_frontmatter_delimiter(line):
            return "".join(lines[index + 1 :])
    return text


def _is_frontmatter_delimiter(line: str) -> bool:
    return line in ("---\n", "---\r\n", "---")


def _skill_kind(metadata: dict[str, Any], package_key: str) -> SkillKind:
    raw_kind = metadata.get("kind")
    if isinstance(raw_kind, str):
        normalized = normalize_skill_key(raw_kind)
        if normalized in ("language", "ci"):
            return cast(SkillKind, normalized)
    if package_key in CI_SKILL_KEYS:
        return "ci"
    return "language"


def _match_keys(
    package_key: str,
    metadata: dict[str, Any],
    extensions: tuple[str, ...],
) -> tuple[str, ...]:
    keys: list[str] = [package_key]
    for field in ("key", "keys", "skill_key", "skill_keys", "languages", "targets"):
        keys.extend(normalize_skill_key(value) for value in _string_values(metadata.get(field)))
    keys.extend(_extension_skill_keys(extensions))

    seen: set[str] = set()
    normalized_keys: list[str] = []
    for key in keys:
        if not key or key in seen:
            continue
        seen.add(key)
        normalized_keys.append(key)
    return tuple(normalized_keys)


def _extensions(value: object) -> tuple[str, ...]:
    extensions: list[str] = []
    for extension in _string_values(value):
        normalized = _normalize_extension(extension)
        if normalized:
            extensions.append(normalized)
    return tuple(_dedupe(extensions))


def _normalize_extension(value: str) -> str:
    extension = value.strip().lower()
    if not extension:
        return ""
    if not extension.startswith("."):
        extension = f".{extension}"
    return extension


def _extension_skill_keys(extensions: Iterable[str]) -> list[str]:
    keys: list[str] = []
    for extension in extensions:
        extension_key = normalize_skill_key(extension.lstrip("."))
        detected_key = EXTENSION_SKILL_KEYS.get(extension)
        if detected_key is not None:
            keys.append(detected_key)
        if extension_key:
            keys.append(extension_key)
    return keys


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _string_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []

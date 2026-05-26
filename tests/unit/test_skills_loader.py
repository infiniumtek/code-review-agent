"""Unit tests for Phase 6 SKILL.md registry and detect-node grouping."""

from __future__ import annotations

from pathlib import Path

import pytest

from code_review_agent.config import ReviewConfig, Settings
from code_review_agent.skills.errors import MissingSkillError, SkillBodyLoadError
from code_review_agent.skills.loader import (
    load_skill_body,
    load_skill_registry,
)
from code_review_agent.utils.detect import detect_skill_key
from code_review_agent.utils.nodes import detect_units
from code_review_agent.utils.state import AgentState, ChangedFile


def _settings(skills_path: Path, *, allow_repo_skills: bool = False) -> Settings:
    return Settings(
        skills_path=skills_path,
        allow_repo_skills=allow_repo_skills,
        trusted_config_ref="",
        _env_file=None,
    )


def _review_config(
    *,
    enabled: list[str] | None = None,
    extra_paths: list[str] | None = None,
) -> ReviewConfig:
    return ReviewConfig.model_validate(
        {
            "skills": {
                "enable": enabled or [],
                "extra_paths": extra_paths or [],
            }
        }
    )


def _changed(path: str, *, diff: str = "+x\n", new_content: str | None = None) -> ChangedFile:
    return ChangedFile(path=path, kind="added", diff=diff, new_content=new_content)


def _write_skill(
    root: Path,
    key: str,
    *,
    name: str | None = None,
    kind: str = "language",
    languages: list[str] | None = None,
    extensions: list[str] | None = None,
    body: str = "Review carefully.\n",
) -> Path:
    skill_dir = root / key
    skill_dir.mkdir(parents=True)
    languages_block = _yaml_list("languages", languages)
    extensions_block = _yaml_list("extensions", extensions)
    path = skill_dir / "SKILL.md"
    path.write_text(
        f"""---
name: {name or key.title()} Reviewer
description: Reviews {key}.
metadata:
  kind: {kind}
{languages_block}{extensions_block}---
{body}""",
        encoding="utf-8",
    )
    return path


def _yaml_list(field: str, values: list[str] | None) -> str:
    if not values:
        return ""
    lines = [f"  {field}:\n"]
    lines.extend(f"    - {value}\n" for value in values)
    return "".join(lines)


def test_registry_resolves_by_directory_name_and_loads_body_lazily(tmp_path: Path) -> None:
    skill_path = _write_skill(tmp_path, "python", body="Python-specific review body.\n")
    registry = load_skill_registry(_review_config(), _settings(tmp_path))

    skill = registry.resolve("python")

    assert skill is not None
    assert skill.key == "python"
    assert skill.name == "Python Reviewer"
    assert skill.path == str(skill_path)
    assert load_skill_body(skill) == "Python-specific review body.\n"


def test_registry_resolves_by_frontmatter_languages_and_extensions(tmp_path: Path) -> None:
    skill_path = _write_skill(
        tmp_path,
        "python-expert",
        name="Python Expert",
        languages=["Python"],
        extensions=[".py"],
    )
    registry = load_skill_registry(_review_config(), _settings(tmp_path))

    by_language = registry.resolve("python")
    by_extension = registry.resolve("py")

    assert by_language is not None
    assert by_language.key == "python-expert"
    assert by_language.path == str(skill_path)
    assert by_extension is not None
    assert by_extension.key == "python-expert"
    assert by_extension.path == str(skill_path)


def test_detect_falls_back_to_registry_extensions_for_new_language(tmp_path: Path) -> None:
    _write_skill(tmp_path, "go", extensions=[".go"])
    file = _changed("cmd/server/main.go")
    state = AgentState(files=[file])

    assert detect_skill_key(file) is None

    units = detect_units(
        state,
        review_config=_review_config(),
        settings=_settings(tmp_path),
    )

    assert len(units) == 1
    assert units[0].skill.key == "go"
    assert units[0].skill.kind == "language"
    assert units[0].files == [file]


def test_registry_extension_fallback_does_not_match_ci_skills_by_generic_extension(
    tmp_path: Path,
) -> None:
    _write_skill(tmp_path, "github-actions", kind="ci", extensions=[".yml"])
    state = AgentState(files=[_changed("config/settings.yml")])

    units = detect_units(
        state,
        review_config=_review_config(enabled=["github-actions"]),
        settings=_settings(tmp_path),
    )

    assert units == []


def test_registry_resolve_returns_independent_skill_refs(tmp_path: Path) -> None:
    _write_skill(tmp_path, "python")
    registry = load_skill_registry(_review_config(), _settings(tmp_path))

    first = registry.resolve("python")
    second = registry.resolve("python")
    assert first is not None
    assert second is not None
    assert first is not second

    first.name = "MUTATED"
    third = registry.resolve("python")

    assert third is not None
    assert third.name == "Python Reviewer"


def test_detect_missing_language_skill_raises(tmp_path: Path) -> None:
    state = AgentState(files=[_changed("src/app.py")])

    with pytest.raises(MissingSkillError) as exc:
        detect_units(
            state,
            review_config=_review_config(),
            settings=_settings(tmp_path),
        )

    assert exc.value.skill_key == "python"
    assert exc.value.kind == "language"


def test_detect_disabled_ci_skill_is_skipped_even_when_present(tmp_path: Path) -> None:
    _write_skill(tmp_path, "dockerfile", kind="ci")
    state = AgentState(files=[_changed("Dockerfile")])

    units = detect_units(
        state,
        review_config=_review_config(enabled=[]),
        settings=_settings(tmp_path),
    )

    assert units == []


def test_detect_enabled_present_ci_skill_is_loaded(tmp_path: Path) -> None:
    _write_skill(tmp_path, "dockerfile", kind="ci")
    state = AgentState(files=[_changed("Dockerfile")])

    units = detect_units(
        state,
        review_config=_review_config(enabled=["dockerfile"]),
        settings=_settings(tmp_path),
    )

    assert len(units) == 1
    assert units[0].skill.key == "dockerfile"
    assert units[0].skill.kind == "ci"
    assert units[0].files[0].path == "Dockerfile"


def test_detector_kind_wins_when_frontmatter_disagrees(tmp_path: Path) -> None:
    _write_skill(tmp_path, "python", kind="ci")
    state = AgentState(files=[_changed("src/app.py")])

    units = detect_units(
        state,
        review_config=_review_config(enabled=["python"]),
        settings=_settings(tmp_path),
    )

    assert len(units) == 1
    assert units[0].skill.key == "python"
    assert units[0].skill.kind == "language"


def test_extra_paths_ignored_when_allow_repo_skills_unset(tmp_path: Path) -> None:
    bundled = tmp_path / "bundled"
    extra = tmp_path / "repo-skills"
    bundled.mkdir()
    _write_skill(extra, "python")
    state = AgentState(files=[_changed("src/app.py")])

    with pytest.raises(MissingSkillError):
        detect_units(
            state,
            review_config=_review_config(extra_paths=[str(extra)]),
            settings=_settings(bundled, allow_repo_skills=False),
        )


def test_extra_paths_honored_when_allow_repo_skills_set(tmp_path: Path) -> None:
    bundled = tmp_path / "bundled"
    extra = tmp_path / "repo-skills"
    bundled.mkdir()
    _write_skill(extra, "python")
    state = AgentState(files=[_changed("src/app.py")])

    units = detect_units(
        state,
        review_config=_review_config(extra_paths=[str(extra)]),
        settings=_settings(bundled, allow_repo_skills=True),
    )

    assert len(units) == 1
    assert units[0].skill.key == "python"
    assert units[0].files[0].path == "src/app.py"


def test_indented_frontmatter_marker_inside_block_scalar_is_not_a_delimiter(
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / "python"
    skill_dir.mkdir()
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(
        """---
name: Py
description: |
  Line one.
  ---
  Line three.
metadata:
  kind: language
---
Real body starts here.
""",
        encoding="utf-8",
    )
    registry = load_skill_registry(_review_config(), _settings(tmp_path))

    skill = registry.resolve("python")

    assert skill is not None
    assert skill.description == "Line one.\n---\nLine three.\n"
    assert skill.kind == "language"
    assert load_skill_body(skill) == "Real body starts here.\n"


def test_load_skill_body_raises_typed_error_when_l2_read_fails(tmp_path: Path) -> None:
    skill_path = _write_skill(tmp_path, "python")
    registry = load_skill_registry(_review_config(), _settings(tmp_path))
    skill = registry.resolve("python")
    assert skill is not None

    skill_path.write_bytes(
        b"---\nname: Python\ndescription: Reviews Python.\nmetadata:\n  kind: language\n---\n\xff"
    )

    with pytest.raises(SkillBodyLoadError) as exc:
        load_skill_body(skill)

    assert exc.value.skill_key == "python"
    assert exc.value.path == str(skill_path)

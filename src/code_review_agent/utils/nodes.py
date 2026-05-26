"""LangGraph node functions for the review pipeline."""

from __future__ import annotations

import structlog

from code_review_agent.config import ReviewConfig, Settings, load_review_config
from code_review_agent.skills.errors import MissingSkillError
from code_review_agent.skills.loader import SkillRegistry, load_skill_registry, normalize_skill_key
from code_review_agent.utils.detect import detect_skill_key, skill_key_kind
from code_review_agent.utils.diffing import (
    ContentResolver,
    git_show_resolver,
    parse_diff,
    working_tree_resolver,
)
from code_review_agent.utils.state import AgentState, ChangedFile, ReviewUnit, SkillRef

log = structlog.get_logger(__name__)


def select_content_resolver(state: AgentState) -> ContentResolver | None:
    """Select the resolver for ``ingest`` from graph input.

    A range/CI run sets ``head_ref`` and reads content with ``git show``. A local
    run with only ``repo_root`` reads the working tree. With no repo root, ingest
    stays diff-only.
    """

    if state.repo_root is None:
        return None
    if state.head_ref:
        return git_show_resolver(state.head_ref, repo_root=state.repo_root)
    return working_tree_resolver(state.repo_root)


def ingest_files(
    state: AgentState,
    *,
    review_config: ReviewConfig | None = None,
    settings: Settings | None = None,
) -> list[ChangedFile]:
    """Parse ``state.diff`` and attach new-side content when resolvable."""

    config = review_config or load_review_config(settings)
    return parse_diff(
        state.diff,
        resolver=select_content_resolver(state),
        ignore_globs=config.review.ignore,
    )


def ingest(state: AgentState) -> dict[str, list[ChangedFile]]:
    """LangGraph ingest node: diff text → ``AgentState.files``."""

    return {"files": ingest_files(state)}


def detect_units(
    state: AgentState,
    *,
    registry: SkillRegistry | None = None,
    review_config: ReviewConfig | None = None,
    settings: Settings | None = None,
) -> list[ReviewUnit]:
    """Classify changed files and group them into skill-backed review units.

    Programming-language files are required: a detected language without a
    matching skill raises ``MissingSkillError``. Optional CI/infra targets run
    only when enabled in review.toml and present in the registry; otherwise they
    are skipped.
    """

    config = review_config or load_review_config(settings)
    skill_registry = registry or load_skill_registry(config, settings)
    enabled_ci = {normalize_skill_key(key) for key in config.skills.enable}

    units_by_key: dict[str, ReviewUnit] = {}
    for file in state.files:
        skill = resolve_file_skill(file, skill_registry, enabled_ci)
        if skill is None:
            continue

        existing = units_by_key.get(skill.key)
        if existing is None:
            units_by_key[skill.key] = ReviewUnit(skill=skill, files=[file])
        else:
            existing.files.append(file)

    return list(units_by_key.values())


def resolve_file_skill(
    file: ChangedFile,
    registry: SkillRegistry,
    enabled_ci: set[str],
) -> SkillRef | None:
    """Resolve the skill for one file, with static detection taking priority."""

    skill_key = detect_skill_key(file)
    if skill_key is None:
        skill = registry.resolve_file(file)
        return skill

    detector_kind = skill_key_kind(skill_key)
    if detector_kind == "ci" and normalize_skill_key(skill_key) not in enabled_ci:
        return None

    skill = registry.resolve(skill_key)
    if skill is None:
        if detector_kind == "language":
            raise MissingSkillError(skill_key, kind="language")
        if detector_kind == "ci":
            log.warning("enabled_ci_skill_missing", skill_key=skill_key)
        return None

    if detector_kind is not None and skill.kind != detector_kind:
        log.warning(
            "skill_kind_mismatch",
            skill_key=skill_key,
            detector_kind=detector_kind,
            frontmatter_kind=skill.kind,
            path=skill.path,
        )
        return skill.model_copy(update={"kind": detector_kind})
    return skill


def detect(state: AgentState) -> dict[str, list[ReviewUnit]]:
    """LangGraph detect node: changed files → ``ReviewUnit`` groups."""

    return {"units": detect_units(state)}

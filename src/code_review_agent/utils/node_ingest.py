"""Ingest node helpers for the review pipeline."""

from __future__ import annotations

from code_review_agent.config import ReviewConfig, Settings, load_review_config
from code_review_agent.utils.diffing import (
    ContentResolver,
    git_show_resolver,
    parse_diff,
    working_tree_resolver,
)
from code_review_agent.utils.state import AgentState, ChangedFile


def select_content_resolver(state: AgentState) -> ContentResolver | None:
    """Select the resolver for ``ingest`` from graph input.

    Explicit two-dot/three-dot ranges set ``head_ref`` and read content with
    ``git show``. A local run, including ``git diff <single-ref>``, reads the
    working tree. With no repo root, ingest stays diff-only.
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
    """LangGraph ingest node: diff text -> ``AgentState.files``."""

    return {"files": ingest_files(state)}

"""Aggregation node helpers for the review pipeline."""

from __future__ import annotations

import structlog
from langgraph.types import Overwrite

from code_review_agent.utils.diffing import normalize_repo_path
from code_review_agent.utils.state import AgentState, Category, Finding, ReviewUnit, Severity

log = structlog.get_logger(__name__)

_SEVERITY_ORDER: tuple[Severity, ...] = ("critical", "high", "medium", "low", "info")
_CATEGORY_ORDER: tuple[Category, ...] = ("security", "bug", "performance", "improvement")
_SEVERITY_RANK: dict[Severity, int] = {
    severity: index for index, severity in enumerate(_SEVERITY_ORDER)
}
_CATEGORY_RANK: dict[Category, int] = {
    category: index for index, category in enumerate(_CATEGORY_ORDER)
}


def aggregate_findings(state: AgentState) -> list[Finding]:
    """Filter, dedupe, and sort all review findings deterministically.

    ``Finding.path`` is LLM output, so attribution is checked against the files
    that were actually reviewed before the report sees it. The path is never
    used for filesystem access; this is report hygiene for hallucinated or
    misattributed findings.
    """

    reviewed_paths = _reviewed_path_map(state.units)
    scoped_findings = _filter_findings_to_reviewed_paths(state.findings, reviewed_paths)
    deduped_findings = _dedupe_findings(scoped_findings)
    return sorted(deduped_findings, key=_finding_sort_key)


def aggregate(state: AgentState) -> dict[str, object]:
    """LangGraph aggregate node: rewrite ``AgentState.findings``.

    ``findings`` uses an ``add`` reducer during review fan-out. ``Overwrite`` is
    required here so aggregation replaces the accumulated list rather than
    appending the aggregated copy to it.
    """

    return {"findings": Overwrite(value=aggregate_findings(state))}


def _reviewed_path_map(units: list[ReviewUnit]) -> dict[str, str]:
    reviewed_paths: dict[str, str] = {}
    for unit in units:
        for file in unit.files:
            normalized = normalize_repo_path(file.path)
            if normalized is not None and normalized not in reviewed_paths:
                reviewed_paths[normalized] = file.path
    return reviewed_paths


def _filter_findings_to_reviewed_paths(
    findings: list[Finding],
    reviewed_paths: dict[str, str],
) -> list[Finding]:
    scoped: list[Finding] = []
    for finding in findings:
        normalized = normalize_repo_path(finding.path)
        reviewed_path = reviewed_paths.get(normalized or "")
        if reviewed_path is None:
            log.warning(
                "finding_attribution_dropped",
                path=finding.path,
                skill_key=finding.skill_key,
                severity=finding.severity,
                title=finding.title,
                reason="path_not_in_review_unit",
            )
            continue
        if finding.path != reviewed_path:
            scoped.append(finding.model_copy(update={"path": reviewed_path}))
        else:
            scoped.append(finding)
    return scoped


def _dedupe_findings(findings: list[Finding]) -> list[Finding]:
    seen: set[tuple[str, int | None, str, str, str, str, str]] = set()
    deduped: list[Finding] = []
    for finding in findings:
        key = _finding_dedupe_key(finding)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(finding)
    return deduped


def _finding_dedupe_key(finding: Finding) -> tuple[str, int | None, str, str, str, str, str]:
    return (
        finding.path,
        finding.line,
        finding.severity,
        finding.category,
        finding.title,
        finding.detail,
        finding.skill_key,
    )


def _finding_sort_key(finding: Finding) -> tuple[int, str, tuple[int, int], int, str, str, str]:
    return (
        _SEVERITY_RANK[finding.severity],
        finding.path,
        _line_sort_key(finding.line),
        _CATEGORY_RANK[finding.category],
        finding.skill_key,
        finding.title,
        finding.detail,
    )


def _line_sort_key(line: int | None) -> tuple[int, int]:
    if line is None:
        return (1, 0)
    return (0, line)

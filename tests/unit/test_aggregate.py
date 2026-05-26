"""Unit tests for the Phase 9 aggregate node."""

from __future__ import annotations

from typing import get_args

import pytest
from langgraph.types import Overwrite

from code_review_agent.utils import nodes
from code_review_agent.utils.nodes import (
    _CATEGORY_RANK,
    _SEVERITY_RANK,
    aggregate,
    aggregate_findings,
)
from code_review_agent.utils.state import (
    AgentState,
    Category,
    ChangedFile,
    Finding,
    ReviewUnit,
    Severity,
    SkillRef,
)


class _FakeLog:
    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, object]]] = []

    def warning(self, event: str, **kwargs: object) -> None:
        self.warnings.append((event, kwargs))


def _skill(key: str = "python") -> SkillRef:
    return SkillRef(
        key=key,
        name=f"{key} reviewer",
        description=f"Review {key}.",
        kind="language",
        path=f"/skills/{key}/SKILL.md",
    )


def _unit(*paths: str, key: str = "python") -> ReviewUnit:
    return ReviewUnit(
        skill=_skill(key),
        files=[ChangedFile(path=path, kind="added", diff="+x\n") for path in paths],
    )


def _finding(
    path: str,
    *,
    line: int | None = 1,
    severity: Severity = "medium",
    category: Category = "bug",
    title: str = "Issue",
    detail: str = "Fix it.",
    skill_key: str = "python",
) -> Finding:
    return Finding(
        path=path,
        line=line,
        severity=severity,
        category=category,
        title=title,
        detail=detail,
        skill_key=skill_key,
    )


def test_aggregate_dedupes_exact_duplicate_findings() -> None:
    duplicate = _finding("src/app.py", line=7, title="Repeated")
    state = AgentState(
        units=[_unit("src/app.py")],
        findings=[
            duplicate,
            _finding("src/app.py", line=7, title="Repeated"),
            _finding("src/app.py", line=7, title="Same location, different issue"),
        ],
    )

    findings = aggregate_findings(state)

    assert [finding.title for finding in findings] == [
        "Repeated",
        "Same location, different issue",
    ]


def test_aggregate_dedupes_after_path_canonicalization() -> None:
    state = AgentState(
        units=[_unit("src/app.py")],
        findings=[
            _finding("src/app.py", line=7, title="Repeated"),
            _finding("./src/app.py", line=7, title="Repeated"),
        ],
    )

    findings = aggregate_findings(state)

    assert len(findings) == 1
    assert findings[0].path == "src/app.py"
    assert findings[0].title == "Repeated"


def test_aggregate_sorts_deterministically_with_line_none_last() -> None:
    state = AgentState(
        units=[_unit("src/app.py")],
        findings=[
            _finding("src/app.py", line=None, title="file-level"),
            _finding("src/app.py", line=3, title="same-line-a"),
            _finding("src/app.py", line=1, title="line-one"),
            _finding("src/app.py", line=20, severity="critical", title="critical"),
            _finding("src/app.py", line=3, title="same-line-b"),
        ],
    )

    findings = aggregate_findings(state)

    assert [finding.title for finding in findings] == [
        "critical",
        "line-one",
        "same-line-a",
        "same-line-b",
        "file-level",
    ]


def test_aggregate_sort_uses_category_skill_title_and_detail_tiebreakers() -> None:
    state = AgentState(
        units=[_unit("src/app.py")],
        findings=[
            _finding("src/app.py", category="improvement", title="improvement"),
            _finding("src/app.py", category="security", title="security"),
            _finding("src/app.py", category="bug", skill_key="python-b", title="skill-b"),
            _finding("src/app.py", category="bug", skill_key="python-a", title="z-title"),
            _finding(
                "src/app.py",
                category="bug",
                skill_key="python-a",
                title="a-title",
                detail="b-detail",
            ),
            _finding(
                "src/app.py",
                category="bug",
                skill_key="python-a",
                title="a-title",
                detail="a-detail",
            ),
        ],
    )

    findings = aggregate_findings(state)

    assert [
        (finding.category, finding.skill_key, finding.title, finding.detail) for finding in findings
    ] == [
        ("security", "python", "security", "Fix it."),
        ("bug", "python-a", "a-title", "a-detail"),
        ("bug", "python-a", "a-title", "b-detail"),
        ("bug", "python-a", "z-title", "Fix it."),
        ("bug", "python-b", "skill-b", "Fix it."),
        ("improvement", "python", "improvement", "Fix it."),
    ]


def test_aggregate_rank_tables_cover_state_literals() -> None:
    assert set(_SEVERITY_RANK) == set(get_args(Severity))
    assert set(_CATEGORY_RANK) == set(get_args(Category))


def test_aggregate_drops_findings_for_paths_outside_reviewed_units() -> None:
    state = AgentState(
        units=[_unit("src/app.py", "Dockerfile")],
        findings=[
            _finding("./src/app.py", title="canonicalized"),
            _finding("src/other.py", title="hallucinated"),
            _finding("../src/app.py", title="unsafe"),
            _finding("Dockerfile", line=None, title="ci"),
        ],
    )

    findings = aggregate_findings(state)

    assert [(finding.path, finding.title) for finding in findings] == [
        ("Dockerfile", "ci"),
        ("src/app.py", "canonicalized"),
    ]


def test_aggregate_logs_severity_and_title_for_dropped_findings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_log = _FakeLog()
    monkeypatch.setattr(nodes, "log", fake_log)
    state = AgentState(
        units=[_unit("src/app.py")],
        findings=[
            _finding(
                "src/other.py",
                severity="critical",
                title="Wrong-path critical issue",
            )
        ],
    )

    findings = aggregate_findings(state)

    assert findings == []
    assert fake_log.warnings == [
        (
            "finding_attribution_dropped",
            {
                "path": "src/other.py",
                "skill_key": "python",
                "severity": "critical",
                "title": "Wrong-path critical issue",
                "reason": "path_not_in_review_unit",
            },
        )
    ]


def test_aggregate_node_overwrites_reducer_backed_findings_channel() -> None:
    state = AgentState(
        units=[_unit("src/app.py")],
        findings=[_finding("src/app.py", title="kept")],
    )

    update = aggregate(state)

    assert isinstance(update["findings"], Overwrite)
    assert update["findings"].value == aggregate_findings(state)

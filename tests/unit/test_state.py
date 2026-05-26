"""Unit tests for the typed graph state and the ``findings`` fan-out reducer."""

from __future__ import annotations

from operator import add
from typing import get_args, get_type_hints

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import ValidationError

from code_review_agent.utils.state import (
    AgentState,
    ChangedFile,
    Finding,
    ReviewResult,
    ReviewTaskState,
    ReviewUnit,
    SkillRef,
)


def _skill(key: str = "python") -> SkillRef:
    return SkillRef(
        key=key,
        name=f"{key} reviewer",
        description=f"Review {key} code.",
        kind="language",
        path=f"/skills/{key}/SKILL.md",
    )


def _finding(key: str = "python", title: str = "issue") -> Finding:
    return Finding(
        path=f"{key}/mod.py",
        line=12,
        severity="medium",
        category="bug",
        title=title,
        detail="explanation",
        skill_key=key,
    )


# --- ChangedFile -------------------------------------------------------------


def test_changed_file_new_content_defaults_none() -> None:
    cf = ChangedFile(path="a.py", kind="added", diff="+x")
    assert cf.new_content is None


def test_changed_file_carries_new_content_for_modified() -> None:
    cf = ChangedFile(path="a.py", kind="modified", diff="@@", new_content="full text")
    assert cf.new_content == "full text"


def test_changed_file_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        ChangedFile(path="a.py", kind="exploded", diff="+x")  # type: ignore[arg-type]


# --- SkillRef / ReviewUnit ---------------------------------------------------


def test_skill_ref_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        SkillRef(key="x", name="x", description="d", kind="wizard", path="/p")  # type: ignore[arg-type]


def test_review_unit_groups_files_under_skill() -> None:
    files = [ChangedFile(path="a.py", kind="added", diff="+x")]
    unit = ReviewUnit(skill=_skill(), files=files)
    assert unit.skill.key == "python"
    assert unit.files == files


# --- Finding -----------------------------------------------------------------


def test_finding_line_optional() -> None:
    f = Finding(
        path="a.py",
        severity="info",
        category="improvement",
        title="t",
        detail="d",
        skill_key="python",
    )
    assert f.line is None


@pytest.mark.parametrize("field,bad", [("severity", "catastrophic"), ("category", "style")])
def test_finding_rejects_unknown_enum(field: str, bad: str) -> None:
    kwargs: dict[str, object] = {
        "path": "a.py",
        "severity": "low",
        "category": "bug",
        "title": "t",
        "detail": "d",
        "skill_key": "python",
    }
    kwargs[field] = bad
    with pytest.raises(ValidationError):
        Finding(**kwargs)  # type: ignore[arg-type]


def test_finding_descriptions_present_for_structured_output() -> None:
    # Finding backs with_structured_output (Phase 8); field descriptions are part
    # of the schema handed to the LLM, so they must not silently disappear.
    props = Finding.model_json_schema()["properties"]
    assert all("description" in props[name] for name in props)


# --- ReviewResult ------------------------------------------------------------


def test_review_result_defaults_to_empty_findings() -> None:
    assert ReviewResult().findings == []


def test_review_result_validates_nested_findings() -> None:
    result = ReviewResult.model_validate({"findings": [_finding().model_dump()]})
    assert result.findings[0].skill_key == "python"


# --- AgentState defaults -----------------------------------------------------


def test_agent_state_defaults() -> None:
    state = AgentState()
    assert state.diff == ""
    assert state.repo_root is None
    assert state.head_ref is None
    assert state.llm_provider_override is None
    assert state.llm_model_override is None
    assert state.reporter_override is None
    assert state.fail_on_override is None
    assert state.files == []
    assert state.units == []
    assert state.findings == []
    assert state.report == ""


def test_review_task_state_override_defaults() -> None:
    state = ReviewTaskState(unit=ReviewUnit(skill=_skill(), files=[]))
    assert state.llm_provider_override is None
    assert state.llm_model_override is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"llm_provider_override": "bogus"},
        {"fail_on_override": "urgent"},
    ],
)
def test_agent_state_rejects_unknown_override_values(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AgentState(**kwargs)


# --- the findings reducer ----------------------------------------------------


@pytest.mark.parametrize("model", [AgentState, ReviewTaskState])
def test_findings_field_carries_add_reducer(model: type) -> None:
    """Both ``findings`` fields are annotated with ``operator.add`` so LangGraph
    concatenates concurrent branch returns instead of overwriting them."""
    hint = get_type_hints(model, include_extras=True)["findings"]
    assert add in get_args(hint)


def test_add_reducer_concatenates_findings() -> None:
    branch_a = [_finding("python", "a")]
    branch_b = [_finding("javascript", "b")]
    merged = add(branch_a, branch_b)
    assert [f.title for f in merged] == ["a", "b"]


def test_send_fan_out_accumulates_findings_into_agent_state() -> None:
    """End-to-end reducer check: a ``Send`` fan-out over units routes one
    ``ReviewTaskState`` per branch into the ``review`` node, and every branch's
    ``{"findings": [...]}`` merges into ``AgentState.findings`` via the reducer.
    """

    def review(task: ReviewTaskState) -> dict[str, list[Finding]]:
        key = task.unit.skill.key
        return {"findings": [_finding(key, f"from-{key}")]}

    def fan_out(state: AgentState) -> list[Send]:
        return [Send("review", ReviewTaskState(unit=u)) for u in state.units]

    graph = StateGraph(AgentState)
    graph.add_node("review", review, input_schema=ReviewTaskState)
    graph.add_conditional_edges(START, fan_out, ["review"])
    graph.add_edge("review", END)
    compiled = graph.compile()

    units = [
        ReviewUnit(
            skill=_skill("python"), files=[ChangedFile(path="a.py", kind="added", diff="+")]
        ),
        ReviewUnit(
            skill=_skill("java"), files=[ChangedFile(path="B.java", kind="added", diff="+")]
        ),
    ]
    result = compiled.invoke({"units": units})

    titles = sorted(f.title for f in result["findings"])
    assert titles == ["from-java", "from-python"]

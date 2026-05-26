"""Unit tests for compiled-graph routing behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from code_review_agent import config
from code_review_agent.agent import agent, route_review_units
from code_review_agent.utils.state import (
    AgentState,
    ChangedFile,
    ReviewTaskState,
    ReviewUnit,
    SkillRef,
)


def _skill(key: str = "python") -> SkillRef:
    return SkillRef(
        key=key,
        name=f"{key} reviewer",
        description=f"Review {key}.",
        kind="language",
        path=f"/skills/{key}/SKILL.md",
    )


def _unit(key: str = "python") -> ReviewUnit:
    return ReviewUnit(
        skill=_skill(key),
        files=[ChangedFile(path="src/app.py", kind="added", diff="+x\n")],
    )


@pytest.fixture(autouse=True)
def _clear_config_caches() -> None:
    config.get_settings.cache_clear()
    config._load_review_config_cached.cache_clear()
    yield
    config.get_settings.cache_clear()
    config._load_review_config_cached.cache_clear()


def test_agent_empty_diff_runs_full_pipeline_and_preserves_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REPORT_DIR", str(tmp_path))
    result = agent.invoke(
        {
            "diff": "",
            "reporter_override": "terminal,file",
            "fail_on_override": "low",
            "llm_provider_override": "google",
            "llm_model_override": "gemini-2.5-pro",
        }
    )

    assert result["diff"] == ""
    assert result["reporter_override"] == "terminal,file"
    assert result["fail_on_override"] == "low"
    assert result["llm_provider_override"] == "google"
    assert result["llm_model_override"] == "gemini-2.5-pro"
    assert result["files"] == []
    assert result["units"] == []
    assert result["findings"] == []
    assert "No findings." in result["report"]


def test_route_review_units_skips_to_aggregate_when_no_units() -> None:
    assert route_review_units(AgentState()) == "aggregate"


def test_route_review_units_fans_out_review_tasks_and_carries_llm_overrides() -> None:
    routes = route_review_units(
        AgentState(
            units=[_unit("python"), _unit("java")],
            llm_provider_override="anthropic",
            llm_model_override="claude-sonnet-4-5",
        )
    )

    assert isinstance(routes, list)
    assert [route.node for route in routes] == ["review", "review"]
    assert all(isinstance(route.arg, ReviewTaskState) for route in routes)
    assert [route.arg.unit.skill.key for route in routes] == ["python", "java"]
    assert [route.arg.llm_provider_override for route in routes] == ["anthropic", "anthropic"]
    assert [route.arg.llm_model_override for route in routes] == [
        "claude-sonnet-4-5",
        "claude-sonnet-4-5",
    ]

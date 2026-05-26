"""Integration tests for the compiled LangGraph review pipeline."""

from __future__ import annotations

from pathlib import Path

import pytest

from code_review_agent import config
from code_review_agent.agent import build_agent
from code_review_agent.utils import nodes
from code_review_agent.utils.state import Finding, ReviewResult


class _FakeStructuredRunnable:
    def __init__(self, result: ReviewResult) -> None:
        self.result = result
        self.calls = 0

    def invoke(self, messages: object) -> ReviewResult:
        self.calls += 1
        return self.result


class _FakeLLM:
    def __init__(self, result: ReviewResult) -> None:
        self.structured = _FakeStructuredRunnable(result)
        self.schema: object | None = None
        self.structured_kwargs: dict[str, object] = {}

    def with_structured_output(self, schema: object, **kwargs: object) -> _FakeStructuredRunnable:
        self.schema = schema
        self.structured_kwargs = kwargs
        return self.structured

    def invoke(self, messages: object) -> object:
        raise AssertionError("raw fallback LLM path should not be called")


@pytest.fixture(autouse=True)
def _clear_config_caches() -> None:
    config.get_settings.cache_clear()
    config._load_review_config_cached.cache_clear()
    yield
    config.get_settings.cache_clear()
    config._load_review_config_cached.cache_clear()


def test_compiled_graph_reviews_recorded_diff_with_mocked_llm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for marker in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "JENKINS_URL"):
        monkeypatch.delenv(marker, raising=False)

    skills_dir = tmp_path / "skills"
    _write_python_skill(skills_dir)
    review_config = tmp_path / "review.toml"
    review_config.write_text("[review]\nmax_unit_tokens = 100000\n", encoding="utf-8")

    monkeypatch.setenv("SKILLS_PATH", str(skills_dir))
    monkeypatch.setenv("REVIEW_CONFIG", str(review_config))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    fake_llm = _FakeLLM(
        ReviewResult(
            findings=[
                Finding(
                    path="src/app.py",
                    line=2,
                    severity="high",
                    category="bug",
                    title="Division by zero is unchecked",
                    detail="Guard or document the zero-denominator behavior before dividing.",
                    skill_key="python",
                )
            ]
        )
    )

    def fake_get_llm(**_: object) -> _FakeLLM:
        return fake_llm

    monkeypatch.setattr(nodes, "get_llm", fake_get_llm)

    fixture = Path(__file__).parent / "fixtures" / "python_added.diff"
    result = build_agent().invoke({"diff": fixture.read_text(encoding="utf-8")})

    assert [file.path for file in result["files"]] == ["src/app.py"]
    assert [unit.skill.key for unit in result["units"]] == ["python"]
    assert [finding.title for finding in result["findings"]] == ["Division by zero is unchecked"]
    assert "Advisory:" in result["report"]
    assert "src/app.py:2" in result["report"]
    assert fake_llm.schema is ReviewResult
    assert fake_llm.structured_kwargs["method"] == "json_schema"
    assert fake_llm.structured.calls == 1


def _write_python_skill(root: Path) -> None:
    skill_dir = root / "python"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: Python Reviewer
description: Reviews Python changes.
metadata:
  kind: language
  languages:
    - python
  extensions:
    - .py
---
Review Python changes for correctness and security.
""",
        encoding="utf-8",
    )

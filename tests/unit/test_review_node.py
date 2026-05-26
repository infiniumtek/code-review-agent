"""Unit tests for the Phase 8 review node."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from code_review_agent.config import Provider, ReviewConfig, Settings
from code_review_agent.utils import nodes
from code_review_agent.utils.nodes import (
    _RAW_RESPONSE_LOG_LIMIT,
    _is_context_length_error,
    review_unit_findings,
)
from code_review_agent.utils.state import (
    ChangedFile,
    Finding,
    ReviewResult,
    ReviewTaskState,
    ReviewUnit,
    SkillRef,
)


class _FakeRunnable:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls = 0
        self.messages: list[object] = []

    def invoke(self, messages: object) -> object:
        self.calls += 1
        self.messages.append(messages)
        if not self.responses:
            raise AssertionError("unexpected structured LLM call")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class _FakeLLM:
    def __init__(self, *, structured: list[object], raw: list[object] | None = None) -> None:
        self.structured = _FakeRunnable(structured)
        self.raw = _FakeRunnable(raw or [])
        self.schema: object | None = None
        self.structured_kwargs: dict[str, object] = {}

    def with_structured_output(self, schema: object, **kwargs: object) -> _FakeRunnable:
        self.schema = schema
        self.structured_kwargs = kwargs
        return self.structured

    def invoke(self, messages: object) -> object:
        return self.raw.invoke(messages)


class _FakeLog:
    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, object]]] = []

    def warning(self, event: str, **kwargs: object) -> None:
        self.warnings.append((event, kwargs))


def _settings(provider: Provider = "openai", *, retries: int = 0) -> Settings:
    return Settings(
        openai_api_key="sk-openai",
        anthropic_api_key="sk-anthropic",
        google_api_key="sk-google",
        default_llm_provider=provider,
        llm_max_retries=retries,
        trusted_config_ref="",
        _env_file=None,
    )


def _task(*, file_count: int = 1) -> ReviewTaskState:
    skill = SkillRef(
        key="python",
        name="Python Reviewer",
        description="Review Python changes.",
        kind="language",
        path="/skills/python/SKILL.md",
    )
    files = [
        ChangedFile(
            path=f"src/app_{index}.py",
            kind="added",
            diff=f"@@ -0,0 +1,2 @@\n+def answer_{index}() -> int:\n+    return {index}\n",
        )
        for index in range(file_count)
    ]
    return ReviewTaskState(unit=ReviewUnit(skill=skill, files=files))


def _review_config(*, max_unit_tokens: int = 100_000) -> ReviewConfig:
    return ReviewConfig.model_validate({"review": {"max_unit_tokens": max_unit_tokens}})


def _skill_body_loader(_: SkillRef) -> str:
    return "Review Python changes for correctness and security."


def _finding_payload(*, line: int | None = 2, title: str = "Bad answer") -> dict[str, object]:
    return {
        "path": "src/app.py",
        "line": line,
        "severity": "medium",
        "category": "bug",
        "title": title,
        "detail": "The changed behavior is incorrect.",
        "skill_key": "python",
    }


def _run_review(
    fake_llm: _FakeLLM,
    *,
    provider: Provider = "openai",
    retries: int = 0,
    task: ReviewTaskState | None = None,
    review_config: ReviewConfig | None = None,
) -> list[Finding]:
    return review_unit_findings(
        task or _task(),
        llm=fake_llm,
        review_config=review_config or _review_config(),
        settings=_settings(provider, retries=retries),
        skill_body_loader=_skill_body_loader,
    )


@pytest.mark.parametrize(
    ("provider", "method"),
    [
        ("openai", "json_schema"),
        ("anthropic", "function_calling"),
        ("google", "json_schema"),
    ],
)
def test_review_node_uses_provider_structured_output_method(
    provider: Provider,
    method: str,
) -> None:
    fake_llm = _FakeLLM(
        structured=[ReviewResult(findings=[Finding.model_validate(_finding_payload())])]
    )

    findings = _run_review(fake_llm, provider=provider)

    assert [finding.title for finding in findings] == ["Bad answer"]
    assert fake_llm.schema is ReviewResult
    assert fake_llm.structured_kwargs["method"] == method
    assert fake_llm.raw.calls == 0


def test_malformed_but_salvageable_json_uses_fallback() -> None:
    raw = """The structured parser failed, but here is the review JSON:

```json
{"findings":[{
  "path":"src/app.py",
  "line":4,
  "severity":"high",
  "category":"bug",
  "title":"Bug",
  "detail":"Fix it.",
  "skill_key":"python",
},]}
```"""
    fake_llm = _FakeLLM(
        structured=[RuntimeError("structured parser failed")],
        raw=[AIMessage(content=raw)],
    )

    findings = _run_review(fake_llm)

    assert len(findings) == 1
    assert findings[0].severity == "high"
    assert findings[0].line == 4
    assert fake_llm.raw.calls == 1
    fallback_messages = fake_llm.raw.messages[0]
    assert isinstance(fallback_messages, list)
    assert "Respond with ONLY a JSON object" in fallback_messages[-1].content
    assert "Schema:" in fallback_messages[-1].content


def test_fallback_skips_unrelated_json_before_review_payload() -> None:
    raw = """
The changed code contains this unrelated object: {"debug": true}.

{"findings":[{
  "path":"src/app.py",
  "line":8,
  "severity":"medium",
  "category":"bug",
  "title":"Real finding",
  "detail":"Use the actual review payload.",
  "skill_key":"wrong"
}]}
"""
    fake_llm = _FakeLLM(
        structured=[RuntimeError("structured parser failed")],
        raw=[AIMessage(content=raw)],
    )

    findings = _run_review(fake_llm)

    assert len(findings) == 1
    assert findings[0].title == "Real finding"
    assert findings[0].skill_key == "python"


def test_fallback_trailing_comma_repair_preserves_commas_inside_strings() -> None:
    raw = """Here is the JSON payload:

{"findings":[{
  "path":"src/app.py",
  "line":9,
  "severity":"medium",
  "category":"bug",
  "title":"Preserve punctuation",
  "detail":"check items x, ] then stop",
  "skill_key":"python",
},]}"""
    fake_llm = _FakeLLM(
        structured=[RuntimeError("structured parser failed")],
        raw=[AIMessage(content=raw)],
    )

    findings = _run_review(fake_llm)

    assert len(findings) == 1
    assert findings[0].detail == "check items x, ] then stop"


def test_unsalvageable_fallback_logs_raw_response_and_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_log = _FakeLog()
    monkeypatch.setattr(nodes, "log", fake_log)
    fake_llm = _FakeLLM(
        structured=[RuntimeError("structured parser failed")],
        raw=[AIMessage(content="not json")],
    )

    findings = _run_review(fake_llm)

    assert findings == []
    assert any(
        event == "llm_fallback_parse_failed"
        and kwargs["raw_response"] == "not json"
        and isinstance(kwargs["error"], str)
        for event, kwargs in fake_log.warnings
    )


def test_unsalvageable_fallback_logs_capped_raw_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_log = _FakeLog()
    monkeypatch.setattr(nodes, "log", fake_log)
    raw = "not json " + ("x" * (_RAW_RESPONSE_LOG_LIMIT + 50))
    fake_llm = _FakeLLM(
        structured=[RuntimeError("structured parser failed")],
        raw=[AIMessage(content=raw)],
    )

    findings = _run_review(fake_llm)

    assert findings == []
    parse_logs = [
        kwargs["raw_response"]
        for event, kwargs in fake_log.warnings
        if event == "llm_fallback_parse_failed"
    ]
    assert len(parse_logs) == 1
    assert isinstance(parse_logs[0], str)
    assert len(parse_logs[0]) < len(raw)
    assert "truncated" in parse_logs[0]


def test_invalid_finding_logs_capped_raw_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_log = _FakeLog()
    monkeypatch.setattr(nodes, "log", fake_log)
    raw = (
        '{"findings":[{"path":"src/app.py","line":1,"severity":"not-real",'
        '"category":"bug","title":"Bad","detail":"'
        + ("x" * (_RAW_RESPONSE_LOG_LIMIT + 50))
        + '","skill_key":"python"}]}'
    )
    fake_llm = _FakeLLM(
        structured=[RuntimeError("structured parser failed")],
        raw=[AIMessage(content=raw)],
    )

    findings = _run_review(fake_llm)

    assert findings == []
    validation_logs = [
        kwargs["raw_response"]
        for event, kwargs in fake_log.warnings
        if event == "llm_finding_validation_failed"
    ]
    assert len(validation_logs) == 1
    assert isinstance(validation_logs[0], str)
    assert len(validation_logs[0]) < len(raw)
    assert "truncated" in validation_logs[0]


def test_context_length_error_logs_and_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_log = _FakeLog()
    monkeypatch.setattr(nodes, "log", fake_log)
    fake_llm = _FakeLLM(
        structured=[RuntimeError("This model's maximum context length is 8192 tokens")]
    )

    findings = _run_review(fake_llm)

    assert findings == []
    assert fake_llm.raw.calls == 0
    events = [event for event, _ in fake_log.warnings]
    assert "llm_context_length_exceeded" in events


def test_context_length_error_skips_only_the_offending_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_log = _FakeLog()
    monkeypatch.setattr(nodes, "log", fake_log)
    fake_llm = _FakeLLM(
        structured=[
            ReviewResult(findings=[Finding.model_validate(_finding_payload(title="First"))]),
            RuntimeError("This model's maximum context length is 8192 tokens"),
            ReviewResult(findings=[Finding.model_validate(_finding_payload(title="Third"))]),
        ]
    )

    findings = _run_review(
        fake_llm,
        task=_task(file_count=3),
        review_config=_review_config(max_unit_tokens=1),
    )

    assert [finding.title for finding in findings] == ["First", "Third"]
    assert fake_llm.structured.calls == 3
    events = [event for event, _ in fake_log.warnings]
    assert events.count("llm_context_length_exceeded") == 1


def test_retry_path_succeeds_after_transient_structured_error() -> None:
    fake_llm = _FakeLLM(
        structured=[
            RuntimeError("temporary provider error"),
            ReviewResult(findings=[Finding.model_validate(_finding_payload(title="Retried"))]),
        ]
    )

    findings = _run_review(fake_llm, retries=1)

    assert [finding.title for finding in findings] == ["Retried"]
    assert fake_llm.structured.calls == 2
    assert fake_llm.raw.calls == 0


def test_non_positive_llm_lines_are_coerced_to_none_and_retained() -> None:
    fake_llm = _FakeLLM(
        structured=[
            {
                "findings": [
                    _finding_payload(line=0, title="Zero line"),
                    _finding_payload(line=-7, title="Negative line"),
                    _finding_payload(line=5, title="Positive line"),
                ]
            }
        ]
    )

    findings = _run_review(fake_llm)

    assert [(finding.title, finding.line) for finding in findings] == [
        ("Zero line", None),
        ("Negative line", None),
        ("Positive line", 5),
    ]


def test_skill_key_is_stamped_from_review_unit() -> None:
    payload = _finding_payload(title="Wrong key")
    payload["skill_key"] = "diff-injected-key"
    fake_llm = _FakeLLM(structured=[{"findings": [payload]}])

    findings = _run_review(fake_llm)

    assert len(findings) == 1
    assert findings[0].skill_key == "python"


def test_missing_llm_skill_key_is_filled_from_review_unit() -> None:
    payload = _finding_payload(title="Missing key")
    del payload["skill_key"]
    fake_llm = _FakeLLM(structured=[{"findings": [payload]}])

    findings = _run_review(fake_llm)

    assert len(findings) == 1
    assert findings[0].skill_key == "python"


def test_ambiguous_rate_limit_text_is_not_context_length() -> None:
    assert not _is_context_length_error(RuntimeError("Request too large: token limit per minute"))


def test_provider_context_error_code_is_context_length() -> None:
    class ProviderError(RuntimeError):
        def __init__(self, message: str) -> None:
            super().__init__(message)
            self.body = {"error": {"code": "context_length_exceeded"}}

    assert _is_context_length_error(ProviderError("bad request"))

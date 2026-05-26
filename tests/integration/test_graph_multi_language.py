"""End-to-end integration test across multiple languages + one CI target.

Exercises the full compiled pipeline (ingest -> detect -> Send fan-out ->
review -> aggregate -> report) on a recorded diff that touches Python,
JavaScript, and Java source plus a GitHub Actions workflow. The real bundled
``skills/`` directory is used so the seed language skills and the optional
``github-actions`` CI skill resolve through the actual loader; only the LLM is
mocked. The fake model routes a distinct finding to each unit by inspecting the
file path embedded in the (untrusted-data) review prompt.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import BaseMessage

from code_review_agent import config
from code_review_agent.agent import build_agent
from code_review_agent.reporters import file as file_reporter
from code_review_agent.utils import node_review
from code_review_agent.utils.state import Finding, ReviewResult

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BUNDLED_SKILLS = _REPO_ROOT / "skills"

# One finding per reviewed path, with deliberately mixed severities so the
# aggregate node's deterministic sort is observable end-to-end.
_FINDINGS_BY_PATH: dict[str, Finding] = {
    "src/calc.py": Finding(
        path="src/calc.py",
        line=3,
        severity="high",
        category="bug",
        title="Unchecked division by zero",
        detail="denominator can be 0; guard before dividing.",
        skill_key="python",
    ),
    "web/checkout.js": Finding(
        path="web/checkout.js",
        line=2,
        severity="critical",
        category="security",
        title="innerHTML assignment enables XSS",
        detail="comment is rendered as HTML without sanitization.",
        skill_key="javascript",
    ),
    "service/src/main/java/App.java": Finding(
        path="service/src/main/java/App.java",
        line=3,
        severity="medium",
        category="performance",
        title="String concatenation builds throwaway objects",
        detail="prefer a StringBuilder or formatted template in hot paths.",
        skill_key="java",
    ),
    ".github/workflows/ci.yml": Finding(
        path=".github/workflows/ci.yml",
        line=8,
        severity="high",
        category="security",
        title="Untrusted PR title interpolated into a run script",
        detail="github.event.pull_request.title enables shell injection.",
        skill_key="github-actions",
    ),
}


class _RoutingStructuredRunnable:
    """Structured-output stub that returns a finding matching the prompt's file."""

    def __init__(self) -> None:
        self.calls = 0
        self.seen_paths: list[str] = []

    def invoke(self, messages: list[BaseMessage]) -> ReviewResult:
        self.calls += 1
        user_text = _user_message_text(messages)
        matched = [finding for path, finding in _FINDINGS_BY_PATH.items() if path in user_text]
        self.seen_paths.extend(finding.path for finding in matched)
        return ReviewResult(findings=matched)


class _RoutingLLM:
    def __init__(self) -> None:
        self.structured = _RoutingStructuredRunnable()
        self.methods: list[object] = []

    def with_structured_output(
        self, schema: object, **kwargs: object
    ) -> _RoutingStructuredRunnable:
        self.methods.append(kwargs.get("method"))
        return self.structured

    def invoke(self, messages: object) -> object:
        raise AssertionError("raw fallback LLM path should not be called")


def _user_message_text(messages: list[BaseMessage]) -> str:
    user = messages[-1]
    content = user.content
    return content if isinstance(content, str) else str(content)


@pytest.fixture(autouse=True)
def _clear_config_caches() -> None:
    config.get_settings.cache_clear()
    config._load_review_config_cached.cache_clear()
    yield
    config.get_settings.cache_clear()
    config._load_review_config_cached.cache_clear()


def test_compiled_graph_reviews_multi_language_diff_with_ci_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for marker in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "JENKINS_URL"):
        monkeypatch.delenv(marker, raising=False)

    # Trusted bundled skills + a local review.toml that enables the optional
    # github-actions CI skill so the workflow file produces a review unit.
    review_config = tmp_path / "review.toml"
    review_config.write_text(
        '[skills]\nenable = ["github-actions"]\n\n[review]\nmax_unit_tokens = 100000\n',
        encoding="utf-8",
    )
    report_dir = tmp_path / "artifacts"

    monkeypatch.setenv("SKILLS_PATH", str(_BUNDLED_SKILLS))
    monkeypatch.setenv("REVIEW_CONFIG", str(review_config))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("DEFAULT_LLM_PROVIDER", "openai")
    monkeypatch.setenv("REPORTER", "terminal,file")
    monkeypatch.setenv("REPORT_DIR", str(report_dir))

    routing_llm = _RoutingLLM()
    monkeypatch.setattr(node_review, "get_llm", lambda **_: routing_llm)

    fixture = Path(__file__).parent / "fixtures" / "multi_language.diff"
    result = build_agent().invoke({"diff": fixture.read_text(encoding="utf-8")})

    # ingest classified every changed file in diff order.
    assert [file.path for file in result["files"]] == [
        "src/calc.py",
        "web/checkout.js",
        "service/src/main/java/App.java",
        ".github/workflows/ci.yml",
    ]

    # detect fanned out one unit per skill, including the optional CI target.
    assert [unit.skill.key for unit in result["units"]] == [
        "python",
        "javascript",
        "java",
        "github-actions",
    ]
    assert routing_llm.structured.calls == 4

    # aggregate produced the deterministic severity-then-path sort.
    assert [finding.title for finding in result["findings"]] == [
        "innerHTML assignment enables XSS",  # critical
        "Untrusted PR title interpolated into a run script",  # high, ".github" path
        "Unchecked division by zero",  # high, "src" path
        "String concatenation builds throwaway objects",  # medium
    ]
    assert {finding.skill_key for finding in result["findings"]} == {
        "python",
        "javascript",
        "java",
        "github-actions",
    }

    # report rendered once and is shared across reporters.
    report = result["report"]
    assert "Advisory:" in report
    assert "src/calc.py:3" in report
    assert "web/checkout.js:2" in report
    assert "service/src/main/java/App.java:3" in report
    assert ".github/workflows/ci.yml:8" in report

    # file reporter wrote durable artifacts into the configured REPORT_DIR.
    markdown_artifact = report_dir / file_reporter.MARKDOWN_REPORT_FILENAME
    json_artifact = report_dir / file_reporter.JSON_REPORT_FILENAME
    assert markdown_artifact.read_text(encoding="utf-8").startswith("# Code Review Report")
    assert '"finding_count": 4' in json_artifact.read_text(encoding="utf-8")

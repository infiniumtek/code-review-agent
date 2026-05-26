"""Unit tests for Phase 11 reporters."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_review_agent.config import ReviewConfig, Settings
from code_review_agent.reporters import ReportContext
from code_review_agent.reporters.file import (
    JSON_REPORT_FILENAME,
    MARKDOWN_REPORT_FILENAME,
    write_report,
)
from code_review_agent.reporters.registry import (
    resolve_report_dir,
    resolve_reporter_names,
    run_reporters,
)
from code_review_agent.reporters.terminal import write_report as write_terminal_report
from code_review_agent.utils.node_report import ADVISORY_DISCLAIMER, render_report
from code_review_agent.utils.state import AgentState, Category, Finding, Severity


@pytest.fixture(autouse=True)
def _clear_reporter_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "REPORTER",
        "REPORT_DIR",
        "GITHUB_ACTIONS",
        "GITLAB_CI",
        "JENKINS_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def _finding(
    *,
    path: str = "src/app.py",
    severity: Severity = "high",
    category: Category = "bug",
    title: str = "Unchecked division",
    detail: str = "Guard the denominator before dividing.",
) -> Finding:
    return Finding(
        path=path,
        line=12,
        severity=severity,
        category=category,
        title=title,
        detail=detail,
        skill_key="python",
    )


def _context(tmp_path: Path) -> ReportContext:
    findings = [_finding()]
    return ReportContext(
        report=render_report(AgentState(findings=findings)),
        findings=findings,
        report_dir=tmp_path,
        advisory_disclaimer=ADVISORY_DISCLAIMER,
    )


def test_terminal_reporter_writes_markdown_to_stdout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_terminal_report(_context(tmp_path))

    captured = capsys.readouterr()
    assert "# Code Review Report" in captured.out
    assert "Unchecked division" in captured.out


def test_file_reporter_writes_markdown_and_json(tmp_path: Path) -> None:
    context = _context(tmp_path)

    write_report(context)

    markdown = (tmp_path / MARKDOWN_REPORT_FILENAME).read_text(encoding="utf-8")
    payload = json.loads((tmp_path / JSON_REPORT_FILENAME).read_text(encoding="utf-8"))
    assert markdown.startswith("# Code Review Report")
    assert ADVISORY_DISCLAIMER in markdown
    assert payload["advisory"] == ADVISORY_DISCLAIMER
    assert payload["rendering"] == {
        "safe_markdown_field": "report",
        "raw_structured_fields": ["findings"],
    }
    assert payload["summary"]["finding_count"] == 1
    assert payload["summary"]["by_severity"] == {"high": 1}
    assert payload["findings"][0]["path"] == "src/app.py"
    assert payload["report"] == context.report


def test_file_reporter_json_marks_raw_findings_and_safe_report_surface(tmp_path: Path) -> None:
    finding = _finding(
        path="src/app.py",
        title="Bad <!-- code-review-agent --> @team <b>",
        detail="Do not publish <!-- code-review-agent --> @team <script>x</script>",
    )
    context = ReportContext(
        report=render_report(AgentState(findings=[finding])),
        findings=[finding],
        report_dir=tmp_path,
        advisory_disclaimer=ADVISORY_DISCLAIMER,
    )

    write_report(context)

    payload = json.loads((tmp_path / JSON_REPORT_FILENAME).read_text(encoding="utf-8"))
    assert payload["rendering"]["safe_markdown_field"] == "report"
    assert payload["rendering"]["raw_structured_fields"] == ["findings"]
    assert "<!-- code-review-agent -->" in payload["findings"][0]["detail"]
    assert "@team" in payload["findings"][0]["detail"]
    assert "<script>x</script>" in payload["findings"][0]["detail"]
    assert "<!-- code-review-agent -->" not in payload["report"]
    assert "@team" not in payload["report"]
    assert "&lt;!-- code-review-agent --&gt;" in payload["report"]
    assert "&#64;team" in payload["report"]


def test_file_reporter_orders_summary_counts_by_review_rank(tmp_path: Path) -> None:
    findings = [
        _finding(severity="info", category="improvement", title="Info"),
        _finding(severity="medium", category="performance", title="Medium"),
        _finding(severity="critical", category="security", title="Critical"),
        _finding(severity="low", category="bug", title="Low"),
        _finding(severity="high", category="bug", title="High"),
    ]
    context = ReportContext(
        report=render_report(AgentState(findings=findings)),
        findings=findings,
        report_dir=tmp_path,
        advisory_disclaimer=ADVISORY_DISCLAIMER,
    )

    write_report(context)

    payload = json.loads((tmp_path / JSON_REPORT_FILENAME).read_text(encoding="utf-8"))
    assert list(payload["summary"]["by_severity"]) == [
        "critical",
        "high",
        "medium",
        "low",
        "info",
    ]
    assert list(payload["summary"]["by_category"]) == [
        "security",
        "bug",
        "performance",
        "improvement",
    ]


def test_run_reporters_dispatches_composable_list_and_keeps_going_after_failure(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def first(context: ReportContext) -> None:
        calls.append(f"first:{context.report_dir}")

    def failing(context: ReportContext) -> None:
        calls.append(f"failing:{context.report_dir}")
        raise RuntimeError("boom")

    def second(context: ReportContext) -> None:
        calls.append(f"second:{context.report_dir}")

    run_reporters(
        _context(tmp_path),
        ["first", "failing", "second"],
        registry={"first": first, "failing": failing, "second": second},
    )

    assert calls == [
        f"first:{tmp_path}",
        f"failing:{tmp_path}",
        f"second:{tmp_path}",
    ]


def test_run_reporters_falls_back_to_terminal_when_all_requested_are_unknown(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def terminal(context: ReportContext) -> None:
        calls.append(f"terminal:{context.report_dir}")

    run_reporters(_context(tmp_path), ["github"], registry={"terminal": terminal})

    assert calls == [f"terminal:{tmp_path}"]


def test_resolve_reporter_names_precedence_and_csv_parsing(tmp_path: Path) -> None:
    review_config = ReviewConfig.model_validate({"report": {"reporters": ["file"]}})
    default_settings = Settings(review_config=tmp_path / "review.toml", _env_file=None)
    env_settings = Settings(
        reporter="terminal,file",
        review_config=tmp_path / "review.toml",
        _env_file=None,
    )

    assert resolve_reporter_names(AgentState(), default_settings, review_config) == ["file"]
    assert resolve_reporter_names(AgentState(), env_settings, review_config) == [
        "terminal",
        "file",
    ]
    assert resolve_reporter_names(
        AgentState(reporter_override="file,terminal,file"),
        env_settings,
        review_config,
    ) == ["file", "terminal"]


def test_stock_dotenv_reporter_defaults_do_not_shadow_review_toml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".env").write_text("REPORTER=auto\nREPORT_DIR=.\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    settings = Settings()
    review_config = ReviewConfig.model_validate(
        {"report": {"reporters": ["terminal", "file"], "report_dir": "build/reports"}}
    )

    assert "reporter" in settings.model_fields_set
    assert "report_dir" in settings.model_fields_set
    assert resolve_reporter_names(AgentState(), settings, review_config) == [
        "terminal",
        "file",
    ]
    assert resolve_report_dir(settings, review_config) == Path("build/reports")


def test_real_env_sentinel_values_still_override_review_toml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REPORTER", "auto")
    monkeypatch.setenv("REPORT_DIR", ".")
    settings = Settings(review_config=tmp_path / "review.toml", _env_file=None)
    review_config = ReviewConfig.model_validate(
        {"report": {"reporters": ["terminal", "file"], "report_dir": "build/reports"}}
    )

    assert resolve_reporter_names(AgentState(), settings, review_config) == [
        "terminal",
        "file",
    ]
    assert resolve_report_dir(settings, review_config) == Path(".")


def test_edited_dotenv_reporter_values_override_review_toml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".env").write_text(
        "REPORTER=file\nREPORT_DIR=dotenv-reports\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    settings = Settings()
    review_config = ReviewConfig.model_validate(
        {"report": {"reporters": ["terminal"], "report_dir": "build/reports"}}
    )

    assert resolve_reporter_names(AgentState(), settings, review_config) == ["file"]
    assert resolve_report_dir(settings, review_config) == Path("dotenv-reports")


def test_resolve_auto_reporter_defaults_to_terminal_and_file_for_unknown_ci(
    tmp_path: Path,
) -> None:
    settings = Settings(review_config=tmp_path / "review.toml", _env_file=None)
    review_config = ReviewConfig()

    assert resolve_reporter_names(AgentState(), settings, review_config) == [
        "terminal",
        "file",
    ]


@pytest.mark.parametrize(
    ("env_name", "expected"),
    [
        ("GITHUB_ACTIONS", ["github", "terminal"]),
        ("GITLAB_CI", ["gitlab", "terminal"]),
        ("JENKINS_URL", ["terminal", "file"]),
    ],
)
def test_resolve_auto_reporter_names_from_platform_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
    expected: list[str],
) -> None:
    monkeypatch.setenv(env_name, "true")
    settings = Settings(review_config=tmp_path / "review.toml", _env_file=None)

    assert resolve_reporter_names(AgentState(), settings, ReviewConfig()) == expected


def test_explicit_empty_reporter_list_disables_reporters(tmp_path: Path) -> None:
    settings = Settings(review_config=tmp_path / "review.toml", _env_file=None)
    review_config = ReviewConfig.model_validate({"report": {"reporters": []}})

    assert resolve_reporter_names(AgentState(), settings, review_config) == []


def test_empty_reporter_overrides_fall_back_to_review_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(review_config=tmp_path / "review.toml", _env_file=None)
    review_config = ReviewConfig.model_validate({"report": {"reporters": ["file"]}})

    assert resolve_reporter_names(
        AgentState(reporter_override=""),
        settings,
        review_config,
    ) == ["file"]

    monkeypatch.setenv("REPORTER", "")
    env_settings = Settings(review_config=tmp_path / "review.toml", _env_file=None)

    assert resolve_reporter_names(AgentState(), env_settings, review_config) == ["file"]


def test_resolve_report_dir_uses_env_when_explicit_else_review_config(tmp_path: Path) -> None:
    review_config = ReviewConfig.model_validate({"report": {"report_dir": "from-config"}})
    default_settings = Settings(review_config=tmp_path / "review.toml", _env_file=None)
    env_settings = Settings(
        report_dir=tmp_path / "from-env",
        review_config=tmp_path / "review.toml",
        _env_file=None,
    )

    assert resolve_report_dir(default_settings, review_config) == Path("from-config")
    assert resolve_report_dir(env_settings, review_config) == tmp_path / "from-env"

"""Unit tests for the Phase 13 Typer CLI."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from code_review_agent import cli, config
from code_review_agent.config import UntrustedConfigError
from code_review_agent.utils.state import Finding, Severity

runner = CliRunner()


class _FakeAgent:
    def __init__(
        self,
        *,
        findings: list[Finding] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.findings = findings or []
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def invoke(self, state: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(state)
        if self.error is not None:
            raise self.error
        return {"findings": self.findings, "report": "# Code Review Report\n"}


@pytest.fixture(autouse=True)
def _clear_cli_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ALLOW_REPO_SKILLS",
        "CI",
        "GITHUB_ACTIONS",
        "GITLAB_CI",
        "JENKINS_URL",
        "REPORTER",
        "REPORT_DIR",
        "REVIEW_CONFIG",
        "SKILLS_PATH",
        "TRUSTED_CONFIG_PATH",
        "TRUSTED_CONFIG_REF",
    ):
        monkeypatch.delenv(name, raising=False)
    config.get_settings.cache_clear()
    config._load_review_config_cached.cache_clear()
    yield
    config.get_settings.cache_clear()
    config._load_review_config_cached.cache_clear()


def _finding(severity: Severity = "high") -> Finding:
    return Finding(
        path="src/app.py",
        line=3,
        severity=severity,
        category="bug",
        title="Problem",
        detail="Fix it.",
        skill_key="python",
    )


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return proc.stdout.strip()


def _init_repo(repo: Path) -> None:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Tester")


def test_cli_reads_stdin_and_passes_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_agent = _FakeAgent()
    monkeypatch.setattr(cli, "agent", fake_agent)
    review_config = tmp_path / "review.toml"
    diff = "diff --git a/src/app.py b/src/app.py\n@@ -1 +1 @@\n-old\n+new\n"

    result = runner.invoke(
        cli.app,
        [
            "--repo",
            str(tmp_path),
            "--reporter",
            "terminal,file",
            "--config",
            str(review_config),
            "--provider",
            "google",
            "--model",
            "gemini-2.5-pro",
            "--fail-on",
            "off",
            "--allow-repo-skills",
        ],
        input=diff,
    )

    assert result.exit_code == 0
    assert fake_agent.calls == [
        {
            "diff": diff,
            "repo_root": str(tmp_path.resolve()),
            "head_ref": None,
            "reporter_override": "terminal,file",
            "llm_provider_override": "google",
            "llm_model_override": "gemini-2.5-pro",
            "fail_on_override": "off",
        }
    ]
    assert os.environ.get("REVIEW_CONFIG") is None
    assert os.environ.get("ALLOW_REPO_SKILLS") is None


def test_cli_rejects_unknown_provider_before_running_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_agent = _FakeAgent()
    monkeypatch.setattr(cli, "agent", fake_agent)

    result = runner.invoke(
        cli.app,
        ["--provider", "ollama", "--fail-on", "off"],
        input="diff --git a/a.py b/a.py\n",
    )

    assert result.exit_code == 2
    assert "Unsupported provider" in result.output
    assert fake_agent.calls == []


def test_cli_git_range_sets_head_ref_for_git_show_resolver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("old\n", encoding="utf-8")
    _git(tmp_path, "add", "src/app.py")
    _git(tmp_path, "commit", "-q", "-m", "base")
    base_ref = _git(tmp_path, "rev-parse", "HEAD")

    target.write_text("new\n", encoding="utf-8")
    _git(tmp_path, "commit", "-am", "head", "-q")
    head_ref = _git(tmp_path, "rev-parse", "HEAD")

    fake_agent = _FakeAgent()
    monkeypatch.setattr(cli, "agent", fake_agent)

    result = runner.invoke(
        cli.app,
        [
            f"{base_ref}...{head_ref}",
            "--repo",
            str(tmp_path),
            "--reporter",
            "terminal",
            "--fail-on",
            "off",
        ],
    )

    assert result.exit_code == 0
    assert len(fake_agent.calls) == 1
    assert fake_agent.calls[0]["repo_root"] == str(tmp_path.resolve())
    assert fake_agent.calls[0]["head_ref"] == head_ref
    assert "diff --git a/src/app.py b/src/app.py" in fake_agent.calls[0]["diff"]
    assert "+new" in fake_agent.calls[0]["diff"]


def test_cli_single_ref_diff_uses_working_tree_new_side(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("committed\n", encoding="utf-8")
    _git(tmp_path, "add", "src/app.py")
    _git(tmp_path, "commit", "-q", "-m", "base")

    target.write_text("working tree\n", encoding="utf-8")

    fake_agent = _FakeAgent()
    monkeypatch.setattr(cli, "agent", fake_agent)

    result = runner.invoke(
        cli.app,
        [
            "HEAD",
            "--repo",
            str(tmp_path),
            "--reporter",
            "terminal",
            "--fail-on",
            "off",
        ],
    )

    assert result.exit_code == 0
    assert len(fake_agent.calls) == 1
    assert fake_agent.calls[0]["head_ref"] is None
    assert "+working tree" in fake_agent.calls[0]["diff"]


def test_cli_exit_code_follows_fail_on_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_agent = _FakeAgent(findings=[_finding("high")])
    monkeypatch.setattr(cli, "agent", fake_agent)

    failing = runner.invoke(
        cli.app,
        ["--fail-on", "high"],
        input="diff --git a/src/app.py b/src/app.py\n",
    )
    passing = runner.invoke(
        cli.app,
        ["--fail-on", "critical"],
        input="diff --git a/src/app.py b/src/app.py\n",
    )

    assert failing.exit_code == 1
    assert passing.exit_code == 0


def test_cli_surfaces_untrusted_config_as_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_agent = _FakeAgent(error=UntrustedConfigError("refuse untrusted config"))
    monkeypatch.setattr(cli, "agent", fake_agent)

    result = runner.invoke(
        cli.app,
        ["--fail-on", "off"],
        input="diff --git a/src/app.py b/src/app.py\n",
    )

    assert result.exit_code == 1
    assert "refuse untrusted config" in result.output


def test_cli_warns_that_config_path_is_ignored_in_ci(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("TRUSTED_CONFIG_REF", "main")
    fake_agent = _FakeAgent()
    monkeypatch.setattr(cli, "agent", fake_agent)

    result = runner.invoke(
        cli.app,
        [
            "--config",
            str(tmp_path / "review.toml"),
            "--fail-on",
            "off",
        ],
        input="diff --git a/src/app.py b/src/app.py\n",
    )

    assert result.exit_code == 0
    assert "--config is ignored in CI" in result.output
    assert len(fake_agent.calls) == 1

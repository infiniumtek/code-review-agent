"""Unit tests for settings + the trust-aware review.toml loader."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from code_review_agent import config
from code_review_agent.config import (
    ReviewConfig,
    ReviewSettings,
    Settings,
    UntrustedConfigError,
    load_review_config,
    resolved_extra_paths,
)

CI_MARKERS = ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "JENKINS_URL")


@pytest.fixture(autouse=True)
def _clear_ci_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Baseline = a local (non-CI) run, regardless of where the suite executes."""
    for var in CI_MARKERS:
        monkeypatch.delenv(var, raising=False)


WORKING_TREE_TOML = """
[skills]
enable = ["dockerfile"]
extra_paths = ["./repo-skills"]

[review]
max_unit_tokens = 4242

[report]
reporters = ["terminal"]
fail_on = "low"
"""

TRUSTED_TOML = """
[skills]
enable = ["github-actions"]

[review]
max_unit_tokens = 99999

[report]
reporters = ["file"]
fail_on = "critical"
"""


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "review_config": tmp_path / "review.toml",
        "trusted_config_ref": "",
        "allow_repo_skills": False,
        "_env_file": None,
    }
    base.update(overrides)
    return Settings(**base)


def test_loads_working_tree_config_when_no_trusted_ref(tmp_path: Path) -> None:
    (tmp_path / "review.toml").write_text(WORKING_TREE_TOML, encoding="utf-8")
    cfg = load_review_config(_settings(tmp_path))
    assert cfg.review.max_unit_tokens == 4242
    assert cfg.report.reporters == ["terminal"]
    assert cfg.skills.enable == ["dockerfile"]


def test_missing_config_returns_defaults(tmp_path: Path) -> None:
    cfg = load_review_config(_settings(tmp_path))  # file never written
    assert cfg == ReviewConfig()
    assert cfg.report.fail_on == "high"


def test_malformed_toml_returns_defaults(tmp_path: Path) -> None:
    (tmp_path / "review.toml").write_text("this is = = not toml", encoding="utf-8")
    cfg = load_review_config(_settings(tmp_path))
    assert cfg == ReviewConfig()


@pytest.mark.parametrize("value", [0, -5])
def test_review_settings_rejects_non_positive_token_budget(value: int) -> None:
    with pytest.raises(ValidationError, match="greater than 0"):
        ReviewSettings(max_unit_tokens=value)


def test_non_positive_token_budget_fails_at_config_load(tmp_path: Path) -> None:
    (tmp_path / "review.toml").write_text(
        "[review]\nmax_unit_tokens = 0\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="max_unit_tokens"):
        load_review_config(_settings(tmp_path))


def test_trusted_ref_ignores_working_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With TRUSTED_CONFIG_REF set, the loader reads the trusted ref and never
    the (untrusted) PR-head working tree."""
    (tmp_path / "review.toml").write_text(WORKING_TREE_TOML, encoding="utf-8")  # untrusted PR head

    def fake_git_show(ref: str, path: Path) -> str:
        assert ref == "origin/main"
        return TRUSTED_TOML

    monkeypatch.setattr(config, "_git_show", fake_git_show)
    cfg = load_review_config(_settings(tmp_path, trusted_config_ref="origin/main"))

    # Trusted content wins; the working-tree values are not observed.
    assert cfg.review.max_unit_tokens == 99999
    assert cfg.report.reporters == ["file"]
    assert cfg.skills.enable == ["github-actions"]


def test_trusted_ref_unavailable_falls_back_to_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "_git_show", lambda ref, path: None)
    cfg = load_review_config(_settings(tmp_path, trusted_config_ref="missing-ref"))
    assert cfg == ReviewConfig()


def test_load_review_config_caches_trusted_ref_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def fake_git_show(ref: str, repo_path: str) -> str:
        nonlocal calls
        calls += 1
        return TRUSTED_TOML

    monkeypatch.setattr(config, "_git_show", fake_git_show)
    settings = _settings(tmp_path, trusted_config_ref="origin/cache-test")

    first = load_review_config(settings)
    second = load_review_config(settings)

    assert calls == 1
    assert first == second
    assert first is not second


def test_trusted_ref_reads_repo_relative_path_not_review_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trusted-ref read uses the repo-relative ``trusted_config_path``, never
    the filesystem ``review_config`` (which may be an absolute bundled path that
    is invalid as a ``git show`` repo path)."""
    captured: dict[str, str] = {}

    def fake_git_show(ref: str, repo_path: str) -> str:
        captured["ref"] = ref
        captured["repo_path"] = repo_path
        return TRUSTED_TOML

    monkeypatch.setattr(config, "_git_show", fake_git_show)
    settings = _settings(
        tmp_path,
        review_config=Path("/app/review.toml"),  # absolute bundled path (Dockerfile)
        trusted_config_ref="origin/main",  # trusted_config_path defaults to "review.toml"
    )
    cfg = load_review_config(settings)

    assert captured["repo_path"] == "review.toml"  # repo-relative, NOT /app/review.toml
    assert cfg.review.max_unit_tokens == 99999


@pytest.mark.parametrize("repo_path", ["review.toml", "./review.toml", "/review.toml"])
def test_trusted_ref_end_to_end_with_real_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo_path: str
) -> None:
    """Real ``git show`` path: committed config is trusted, the dirty working
    tree is ignored — independent of checkout state. ``review_config`` is the
    absolute bundled path (the container case), proving it is not fed to git;
    leading ``./`` and ``/`` on the repo path are normalized away."""

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "Tester")
    (tmp_path / "review.toml").write_text(TRUSTED_TOML, encoding="utf-8")
    git("add", "review.toml")
    git("commit", "-q", "-m", "add trusted config")
    # Now dirty the working tree with malicious content the PR "author" controls.
    (tmp_path / "review.toml").write_text(WORKING_TREE_TOML, encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    settings = _settings(
        tmp_path,
        review_config=Path("/app/review.toml"),  # bundled absolute path, must not reach git
        trusted_config_ref="HEAD",
        trusted_config_path=repo_path,
    )
    cfg = load_review_config(settings)

    assert cfg.review.max_unit_tokens == 99999  # from the committed (trusted) file
    assert cfg.report.fail_on == "critical"


def test_resolved_extra_paths_ignored_without_allow_flag(tmp_path: Path) -> None:
    cfg = ReviewConfig.model_validate({"skills": {"extra_paths": ["./a", "./b"]}})
    assert resolved_extra_paths(cfg, _settings(tmp_path, allow_repo_skills=False)) == []


def test_resolved_extra_paths_honored_with_allow_flag(tmp_path: Path) -> None:
    cfg = ReviewConfig.model_validate({"skills": {"extra_paths": ["./a", "./b"]}})
    assert resolved_extra_paths(cfg, _settings(tmp_path, allow_repo_skills=True)) == ["./a", "./b"]


# --- P2: CI fail-closed when no trusted ref ---------------------------------


@pytest.mark.parametrize("marker", CI_MARKERS)
def test_ci_without_trusted_ref_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marker: str
) -> None:
    monkeypatch.setenv(marker, "true")
    (tmp_path / "review.toml").write_text(WORKING_TREE_TOML, encoding="utf-8")  # PR-controlled
    with pytest.raises(UntrustedConfigError):
        load_review_config(_settings(tmp_path))  # trusted_config_ref defaults to ""


def test_ci_with_trusted_ref_reads_trusted_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(config, "_git_show", lambda ref, path: TRUSTED_TOML)
    cfg = load_review_config(_settings(tmp_path, trusted_config_ref="origin/main"))
    assert cfg.review.max_unit_tokens == 99999


def test_local_without_trusted_ref_still_reads_working_tree(tmp_path: Path) -> None:
    # No CI markers (autouse fixture) → local run reads the working tree as before.
    (tmp_path / "review.toml").write_text(WORKING_TREE_TOML, encoding="utf-8")
    cfg = load_review_config(_settings(tmp_path))
    assert cfg.review.max_unit_tokens == 4242


# --- P1: a PR-supplied checkout .env cannot set operator-only fields in CI ---


def test_dotenv_loaded_locally(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEFAULT_LLM_MODEL", raising=False)
    (tmp_path / ".env").write_text("DEFAULT_LLM_MODEL=from-dotenv\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert Settings().default_llm_model == "from-dotenv"  # cwd .env honored locally


def test_real_env_values_tolerate_preserved_inline_comments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Some env-file loaders pass inline comments through as real env values."""
    monkeypatch.setenv("DEFAULT_LLM_PROVIDER", "openai           # openai | anthropic | google")
    monkeypatch.setenv("DEFAULT_LLM_TEMPERATURE", "0.0           # comment")
    monkeypatch.setenv("SKILLS_PATH", "./skills                  # comment")
    monkeypatch.setenv("REVIEW_CONFIG", "./review.toml           # comment")
    monkeypatch.setenv("ALLOW_REPO_SKILLS", "false               # comment")
    monkeypatch.setenv("TRUSTED_CONFIG_REF", "                   # comment")
    monkeypatch.setenv("TRUSTED_CONFIG_PATH", "review.toml       # comment")
    monkeypatch.setenv("REPORTER", "auto                         # comment")
    monkeypatch.setenv("REPORT_DIR", ".                          # comment")
    monkeypatch.setenv("LANGSMITH_TRACING", "false               # comment")
    monkeypatch.setenv("ENVIRONMENT", "development               # comment")

    settings = Settings(_env_file=None)

    assert settings.default_llm_provider == "openai"
    assert settings.default_llm_temperature == 0.0
    assert settings.skills_path == Path("skills")
    assert settings.review_config == Path("review.toml")
    assert settings.allow_repo_skills is False
    assert settings.trusted_config_ref == ""
    assert settings.trusted_config_path == "review.toml"
    assert settings.reporter == "auto"
    assert settings.report_dir == Path(".")
    assert settings.langsmith_tracing is False
    assert settings.environment == "development"


def test_env_comment_normalization_preserves_hash_without_comment_space(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGSMITH_PROJECT", "team#project")
    assert Settings(_env_file=None).langsmith_project == "team#project"


def test_dotenv_ignored_in_ci(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEFAULT_LLM_MODEL", raising=False)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    (tmp_path / ".env").write_text(
        "DEFAULT_LLM_MODEL=pr-injected\nALLOW_REPO_SKILLS=true\nSKILLS_PATH=./pr-skills\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    settings = Settings()
    # A checkout .env is not a config source under CI: defaults hold.
    assert settings.default_llm_model == "gpt-5-mini"
    assert settings.allow_repo_skills is False
    assert settings.skills_path == Path("./skills")


def test_real_env_var_still_overrides_in_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    # Operator-injected real env vars remain authoritative in CI.
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("DEFAULT_LLM_MODEL", "operator-pinned")
    assert Settings(_env_file=None).default_llm_model == "operator-pinned"

"""Unit tests for settings + the trust-aware review.toml loader."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from code_review_agent import config
from code_review_agent.config import (
    ReviewConfig,
    Settings,
    load_review_config,
    resolved_extra_paths,
)

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


def test_trusted_ref_end_to_end_with_real_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real ``git show`` path: committed config is trusted, the dirty working
    tree is ignored — independent of checkout state."""

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
    settings = _settings(tmp_path, review_config=Path("review.toml"), trusted_config_ref="HEAD")
    cfg = load_review_config(settings)

    assert cfg.review.max_unit_tokens == 99999  # from the committed (trusted) file
    assert cfg.report.fail_on == "critical"


def test_resolved_extra_paths_ignored_without_allow_flag(tmp_path: Path) -> None:
    cfg = ReviewConfig.model_validate({"skills": {"extra_paths": ["./a", "./b"]}})
    assert resolved_extra_paths(cfg, _settings(tmp_path, allow_repo_skills=False)) == []


def test_resolved_extra_paths_honored_with_allow_flag(tmp_path: Path) -> None:
    cfg = ReviewConfig.model_validate({"skills": {"extra_paths": ["./a", "./b"]}})
    assert resolved_extra_paths(cfg, _settings(tmp_path, allow_repo_skills=True)) == ["./a", "./b"]

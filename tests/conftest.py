"""Shared pytest fixtures for the whole suite.

The suite is written to assume a **local (non-CI) run**: it reads the
working-tree ``review.toml`` and honors checkout ``.env`` files. But the CI
trust model in :mod:`code_review_agent.config` keys two behaviors off the
ambient CI markers (``CI``, ``GITHUB_ACTIONS``, …): it fails closed on a
working-tree ``review.toml`` (:class:`~code_review_agent.config.UntrustedConfigError`)
and drops the ``.env`` settings source. GitHub Actions sets ``CI=true``, so
without this baseline those tests pass locally and fail in CI.

Clearing the markers here gives every test a deterministic non-CI baseline
regardless of where the suite executes. Tests that exercise CI behavior set the
markers explicitly in their own body (which runs after this autouse fixture).
"""

from __future__ import annotations

import pytest

# Mirror code_review_agent.config._CI_ENV_MARKERS.
_CI_MARKERS = ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "JENKINS_URL")


@pytest.fixture(autouse=True)
def _local_non_ci_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Baseline = a local (non-CI) run, regardless of where the suite executes."""
    for marker in _CI_MARKERS:
        monkeypatch.delenv(marker, raising=False)

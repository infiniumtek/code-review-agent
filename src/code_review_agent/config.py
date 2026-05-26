"""Configuration: environment settings + the ``review.toml`` loader.

Two layers, by design (see ``CLAUDE.md`` §6 and ``PLAN.md`` "Security & trust
model"):

* **Secrets / runtime knobs** come from the environment (``.env`` locally, CI
  env in pipelines) via :class:`Settings` — a ``pydantic-settings`` model. These
  include the operator-only trust switches ``ALLOW_REPO_SKILLS`` and
  ``TRUSTED_CONFIG_REF`` that a PR author cannot influence.
* **File-level review behavior** comes from ``review.toml`` via
  :func:`load_review_config`. In CI this file is **untrusted** (a PR author owns
  the repo contents), so it is read from the *trusted* base ref named by
  ``TRUSTED_CONFIG_REF`` (``git show <ref>:review.toml``) — never the PR-head
  working tree. Local runs read the working-tree copy.

The ``review.toml`` ``[skills].extra_paths`` gating against ``ALLOW_REPO_SKILLS``
is deliberately *not* applied here; the parsed config is returned faithfully and
the skills loader (Phase 6) resolves effective paths via
:func:`resolved_extra_paths`, keeping the trust decision in one place.
"""

from __future__ import annotations

import os
import re
import subprocess  # read-only `git show` of a trusted ref; fixed argv, no shell
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import structlog
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from code_review_agent.types import FailOnThreshold, ProviderName

log = structlog.get_logger(__name__)

Provider = ProviderName
FailOn = FailOnThreshold
Environment = Literal["development", "staging", "production"]

# Env markers set by the supported CI platforms (plus the generic ``CI``). Used
# to fail closed on the trust boundary: in CI the checked-out working tree is
# PR-controlled, so we neither load a checkout ``.env`` nor read a working-tree
# ``review.toml`` without an explicit trusted ref.
_CI_ENV_MARKERS: tuple[str, ...] = ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "JENKINS_URL")
_INLINE_COMMENT_RE = re.compile(r"\s+#.*\Z")


def _is_ci() -> bool:
    """True when running under a recognized CI platform."""
    return any(os.environ.get(marker) for marker in _CI_ENV_MARKERS)


def _strip_inline_env_comment(value: Any) -> Any:
    """Strip shell-style inline comments that some env-file loaders preserve."""
    if not isinstance(value, str):
        return value
    return _INLINE_COMMENT_RE.sub("", value).strip()


class UntrustedConfigError(RuntimeError):
    """Raised when CI would otherwise read PR-controlled ``review.toml``.

    In CI the working-tree ``review.toml`` is owned by the PR author (untrusted),
    so reading it would let a PR rewrite its own review rules (e.g. ``fail_on``,
    ignore globs). Failing closed forces the operator to set
    ``TRUSTED_CONFIG_REF`` to a trusted base ref — or explicitly to the current
    ref to opt in to working-tree config.
    """


class Settings(BaseSettings):
    """Environment-sourced settings (secrets + runtime knobs).

    Field names map case-insensitively to the env vars in ``.env.example``
    (e.g. ``openai_api_key`` ← ``OPENAI_API_KEY``). Every field has a default so
    import never fails on a missing key; the LLM factory validates that the key
    for the *selected* provider is actually present at call time.

    Precedence is init kwargs > real env vars > ``.env`` file. The ``.env`` file
    is a **local-dev convenience only**: it is resolved relative to the current
    working directory, which in CI may be the untrusted PR checkout, so it is
    skipped entirely under CI (see :meth:`settings_customise_sources`). Operators
    inject config via real env vars in CI.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Drop the ``.env`` source under CI so a PR-supplied checkout ``.env``
        cannot set operator-only fields (``SKILLS_PATH``, ``ALLOW_REPO_SKILLS``,
        ``TRUSTED_CONFIG_REF``, …). Real env vars and init kwargs are unaffected.
        """
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
        if not _is_ci():
            sources.append(dotenv_settings)
        sources.append(file_secret_settings)
        return tuple(sources)

    # --- LLM provider keys (at least one required, validated in llm.py) ---
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    google_api_key: str | None = None

    # --- LLM selection ---
    default_llm_provider: Provider = "openai"
    default_llm_model: str = "gpt-5-mini"
    default_llm_temperature: float = 0.0

    # --- Skills & review behavior (file-level config lives in review.toml) ---
    skills_path: Path = Path("./skills")
    # Filesystem path read for LOCAL (non-CI) runs and as the bundled in-image
    # default; may be absolute (e.g. /app/review.toml). NOT used for the CI
    # trusted-ref read — that uses the repo-relative `trusted_config_path` below.
    review_config: Path = Path("./review.toml")
    # Operator-only trust switches — a PR author cannot set these.
    allow_repo_skills: bool = False
    trusted_config_ref: str = ""
    # Repo-relative path of review.toml *within* `trusted_config_ref`, passed to
    # `git show <ref>:<path>`. Distinct from `review_config` (a filesystem path):
    # an absolute filesystem path is not a valid `git show` repo path.
    trusted_config_path: str = "review.toml"

    # --- LLM resilience ---
    llm_max_retries: int = 2
    llm_timeout_seconds: int = 60

    # --- Reporters (precedence resolved later: CLI > this > review.toml > auto) ---
    reporter: str = "auto"
    report_dir: Path = Path(".")

    # --- Observability ---
    langsmith_api_key: str | None = None
    langsmith_tracing: bool = False
    langsmith_project: str = "code-review-agent"

    # --- Misc ---
    log_level: str = "INFO"
    environment: Environment = "development"

    @field_validator(
        "default_llm_provider",
        "default_llm_model",
        "default_llm_temperature",
        "skills_path",
        "review_config",
        "allow_repo_skills",
        "trusted_config_ref",
        "trusted_config_path",
        "llm_max_retries",
        "llm_timeout_seconds",
        "reporter",
        "report_dir",
        "langsmith_tracing",
        "langsmith_project",
        "log_level",
        "environment",
        mode="before",
    )
    @classmethod
    def _normalize_env_file_comments(cls, value: Any) -> Any:
        return _strip_inline_env_comment(value)


class SkillsConfig(BaseModel):
    """``[skills]`` table — optional CI/infra skills + (gated) extra paths."""

    enable: list[str] = Field(default_factory=list)
    extra_paths: list[str] = Field(default_factory=list)


class ReviewSettings(BaseModel):
    """``[review]`` table — per-unit token budget + ignore globs."""

    max_unit_tokens: int = Field(default=100_000, gt=0)
    ignore: list[str] = Field(default_factory=list)


class ReportConfig(BaseModel):
    """``[report]`` table — reporter list, artifact dir, fail threshold."""

    reporters: list[str] = Field(default_factory=lambda: ["auto"])
    report_dir: str = "."
    fail_on: FailOn = "high"


class ReviewConfig(BaseModel):
    """Parsed ``review.toml``. Sub-tables default to empty so a missing or
    partial file still yields a usable config."""

    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    review: ReviewSettings = Field(default_factory=ReviewSettings)
    report: ReportConfig = Field(default_factory=ReportConfig)


def _git_show(ref: str, repo_path: str) -> str | None:
    """Return ``git show <ref>:<repo_path>`` text, or ``None`` if unavailable.

    ``repo_path`` is **repo-relative** (e.g. ``review.toml``), not a filesystem
    path — ``git show`` resolves it from the repo root, so a leading ``./`` or
    ``/`` is stripped. Read-only; never touches the working tree, so it is
    correct regardless of checkout state.
    """
    repo_path = repo_path.removeprefix("./").lstrip("/")
    spec = f"{ref}:{repo_path}"
    try:
        proc = subprocess.run(
            ["git", "show", spec],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        log.warning("git_unavailable", ref=ref, path=repo_path)
        return None
    if proc.returncode != 0:
        log.warning(
            "trusted_config_unavailable",
            ref=ref,
            path=repo_path,
            stderr=proc.stderr.strip(),
        )
        return None
    return proc.stdout


def _read_review_config_source(
    *,
    trusted_config_ref: str,
    trusted_config_path: str,
    review_config: Path,
    in_ci: bool,
) -> str | None:
    """Return raw ``review.toml`` text from the trusted source, or ``None``.

    Trusted ref set → ``git show <ref>:<trusted_config_path>`` (repo-relative),
    never the PR head and never the filesystem ``review_config``. CI with no
    trusted ref → :class:`UntrustedConfigError` (fail closed, never read the
    PR-controlled working tree). Local with no trusted ref → the filesystem
    ``review_config`` file.
    """
    ref = trusted_config_ref.strip()
    if ref:
        return _git_show(ref, trusted_config_path)
    if in_ci:
        raise UntrustedConfigError(
            "Running in CI without TRUSTED_CONFIG_REF: refusing to read the "
            "PR-controlled working-tree review.toml. Set TRUSTED_CONFIG_REF to a "
            "trusted base ref (or to the current ref to explicitly opt in)."
        )
    try:
        return review_config.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.warning("review_config_missing", path=str(review_config))
        return None


@lru_cache(maxsize=16)
def _load_review_config_cached(
    trusted_config_ref: str,
    trusted_config_path: str,
    review_config: str,
    in_ci: bool,
) -> ReviewConfig:
    raw = _read_review_config_source(
        trusted_config_ref=trusted_config_ref,
        trusted_config_path=trusted_config_path,
        review_config=Path(review_config),
        in_ci=in_ci,
    )
    if raw is None:
        return ReviewConfig()
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        log.warning("review_config_parse_error", error=str(exc))
        return ReviewConfig()
    return ReviewConfig.model_validate(data)


def load_review_config(settings: Settings | None = None) -> ReviewConfig:
    """Load and parse ``review.toml`` from the trust-appropriate source.

    Returns a default :class:`ReviewConfig` when the file is absent or
    unparseable (logged), so the pipeline degrades gracefully rather than
    crashing on a missing/broken config. The returned config is faithful to the
    file; ``extra_paths`` gating is applied separately (see
    :func:`resolved_extra_paths`).

    Raises :class:`UntrustedConfigError` in CI when no trusted ref is configured
    — a deliberate fail-closed exit, not a graceful degradation.
    """
    settings = settings or get_settings()
    return _load_review_config_cached(
        settings.trusted_config_ref,
        settings.trusted_config_path,
        str(settings.review_config),
        _is_ci(),
    ).model_copy(deep=True)


def resolved_extra_paths(config: ReviewConfig, settings: Settings | None = None) -> list[str]:
    """Effective repo-local skill ``extra_paths`` under the trust model.

    Repo-provided skill dirs feed the reviewer's system prompt and are untrusted
    in CI, so they are honored **only** when the operator sets
    ``ALLOW_REPO_SKILLS=true``; otherwise they are ignored (and warned). Phase 6
    (the skills loader) calls this; centralizing it keeps the gate in one place.
    """
    settings = settings or get_settings()
    extra = config.skills.extra_paths
    if not extra:
        return []
    if not settings.allow_repo_skills:
        log.warning(
            "repo_skills_ignored",
            extra_paths=extra,
            reason="ALLOW_REPO_SKILLS is false",
        )
        return []
    return list(extra)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide :class:`Settings` singleton (env read once)."""
    return Settings()


# Module-level convenience handle referenced as ``config.settings`` elsewhere.
settings = get_settings()

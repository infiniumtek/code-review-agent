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

import subprocess  # read-only `git show` of a trusted ref; fixed argv, no shell
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Literal

import structlog
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

log = structlog.get_logger(__name__)

Provider = Literal["openai", "anthropic", "google"]
FailOn = Literal["off", "info", "low", "medium", "high", "critical"]
Environment = Literal["development", "staging", "production"]


class Settings(BaseSettings):
    """Environment-sourced settings (secrets + runtime knobs).

    Field names map case-insensitively to the env vars in ``.env.example``
    (e.g. ``openai_api_key`` ← ``OPENAI_API_KEY``). Every field has a default so
    import never fails on a missing key; the LLM factory validates that the key
    for the *selected* provider is actually present at call time.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

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
    review_config: Path = Path("./review.toml")
    # Operator-only trust switches — a PR author cannot set these.
    allow_repo_skills: bool = False
    trusted_config_ref: str = ""

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


class SkillsConfig(BaseModel):
    """``[skills]`` table — optional CI/infra skills + (gated) extra paths."""

    enable: list[str] = Field(default_factory=list)
    extra_paths: list[str] = Field(default_factory=list)


class ReviewSettings(BaseModel):
    """``[review]`` table — per-unit token budget + ignore globs."""

    max_unit_tokens: int = 100_000
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


def _git_show(ref: str, path: Path) -> str | None:
    """Return ``git show <ref>:<path>`` text, or ``None`` if unavailable.

    Used to read ``review.toml`` from a trusted ref in CI. Read-only; never
    touches the working tree, so it is correct regardless of checkout state.
    """
    # git wants a repo-relative path with no leading "./" (Path already collapses it).
    spec = f"{ref}:{path.as_posix()}"
    try:
        proc = subprocess.run(
            ["git", "show", spec],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        log.warning("git_unavailable", ref=ref, path=path.as_posix())
        return None
    if proc.returncode != 0:
        log.warning(
            "trusted_config_unavailable",
            ref=ref,
            path=path.as_posix(),
            stderr=proc.stderr.strip(),
        )
        return None
    return proc.stdout


def _read_review_config_source(settings: Settings) -> str | None:
    """Return raw ``review.toml`` text from the trusted source, or ``None``.

    CI (``TRUSTED_CONFIG_REF`` set) → the trusted base ref, never the PR head.
    Local (ref empty) → the working-tree file.
    """
    ref = settings.trusted_config_ref.strip()
    path = settings.review_config
    if ref:
        return _git_show(ref, path)
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.warning("review_config_missing", path=str(path))
        return None


def load_review_config(settings: Settings | None = None) -> ReviewConfig:
    """Load and parse ``review.toml`` from the trust-appropriate source.

    Returns a default :class:`ReviewConfig` when the file is absent or
    unparseable (logged), so the pipeline degrades gracefully rather than
    crashing on a missing/broken config. The returned config is faithful to the
    file; ``extra_paths`` gating is applied separately (see
    :func:`resolved_extra_paths`).
    """
    settings = settings or get_settings()
    raw = _read_review_config_source(settings)
    if raw is None:
        return ReviewConfig()
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        log.warning("review_config_parse_error", error=str(exc))
        return ReviewConfig()
    return ReviewConfig.model_validate(data)


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

"""Typer CLI and container entrypoint for code-review-agent."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any

import structlog
import typer
from pydantic import ValidationError

from code_review_agent import config as config_module
from code_review_agent.agent import agent
from code_review_agent.config import UntrustedConfigError, load_review_config
from code_review_agent.llm import MissingAPIKeyError
from code_review_agent.skills.errors import MissingSkillError, SkillBodyLoadError
from code_review_agent.types import FailOnThreshold, ProviderName
from code_review_agent.utils.state import Finding

app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    help="Review a git diff with the LangGraph code-review agent.",
)
log = structlog.get_logger(__name__)

_PROVIDERS: tuple[ProviderName, ...] = ("openai", "anthropic", "google")
_FAIL_THRESHOLDS: tuple[FailOnThreshold, ...] = (
    "off",
    "info",
    "low",
    "medium",
    "high",
    "critical",
)
_SEVERITY_VALUE = {
    "info": 1,
    "low": 2,
    "medium": 3,
    "high": 4,
    "critical": 5,
}


class CliUsageError(RuntimeError):
    """Raised for user-correctable CLI input errors."""


@app.command()
def main(
    range_spec: Annotated[
        str | None,
        typer.Argument(
            help=(
                "Optional git diff argument, for example 'origin/main...HEAD' or "
                "'origin/main'. Two-dot and three-dot ranges read new-side content "
                "from the range head via git show; a single ref compares against "
                "the working tree."
            ),
        ),
    ] = None,
    repo: Annotated[
        Path | None,
        typer.Option(
            "--repo",
            "-C",
            help=(
                "Checkout to review. Git commands use this path with git -C; "
                "stdin diffs use it only for read-only new-side context."
            ),
        ),
    ] = None,
    reporter: Annotated[
        str | None,
        typer.Option(
            "--reporter",
            help=(
                "Reporter selection override: auto or comma-separated terminal,file,github,gitlab."
            ),
        ),
    ] = None,
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            help="Filesystem review.toml path for local/bundled config reads.",
        ),
    ] = None,
    provider: Annotated[
        str | None,
        typer.Option(
            "--provider",
            help="LLM provider override: openai, anthropic, or google.",
        ),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option("--model", help="LLM model override for this run."),
    ] = None,
    fail_on: Annotated[
        str | None,
        typer.Option(
            "--fail-on",
            help="Severity threshold for non-zero exit: off, info, low, medium, high, critical.",
        ),
    ] = None,
    allow_repo_skills: Annotated[
        bool | None,
        typer.Option(
            "--allow-repo-skills/--no-allow-repo-skills",
            help="Honor review.toml [skills].extra_paths for this run.",
        ),
    ] = None,
) -> None:
    """Run a one-shot review and exit according to the configured severity policy."""

    env_overrides = _env_overrides(config_path=config, allow_repo_skills=allow_repo_skills)
    with _temporary_config_env(env_overrides):
        try:
            _warn_if_config_ignored_in_ci(config)
            provider_override = _provider_override(provider)
            fail_on_override = _fail_on_override(fail_on)
            repo_root = _resolve_repo_root(repo)
            diff, head_ref = _load_diff(range_spec, repo_root)
            result = _invoke_agent(
                diff=diff,
                repo_root=repo_root,
                head_ref=head_ref,
                reporter_override=reporter,
                provider_override=provider_override,
                model_override=model,
                fail_on_override=fail_on_override,
            )
            threshold = _resolve_fail_on_threshold(fail_on_override)
            raise typer.Exit(code=_exit_code_for_findings(_findings_from_result(result), threshold))
        except typer.Exit:
            raise
        except CliUsageError as exc:
            _exit_with_error(str(exc), code=2)
        except (
            MissingAPIKeyError,
            MissingSkillError,
            SkillBodyLoadError,
            UntrustedConfigError,
            ValidationError,
        ) as exc:
            _exit_with_error(str(exc), code=1)
        except Exception as exc:  # pragma: no cover - exact LLM/provider failures vary
            log.warning("cli_run_failed", error=str(exc), exc_info=True)
            _exit_with_error(str(exc), code=1)


def _env_overrides(
    *,
    config_path: Path | None,
    allow_repo_skills: bool | None,
) -> dict[str, str]:
    overrides: dict[str, str] = {}
    if config_path is not None:
        overrides["REVIEW_CONFIG"] = str(config_path)
    if allow_repo_skills is not None:
        overrides["ALLOW_REPO_SKILLS"] = "true" if allow_repo_skills else "false"
    return overrides


@contextmanager
def _temporary_config_env(overrides: Mapping[str, str]) -> Iterator[None]:
    original = {key: os.environ.get(key) for key in overrides}
    try:
        for key, override_value in overrides.items():
            os.environ[key] = override_value
        _clear_config_caches()
        yield
    finally:
        for key, original_value in original.items():
            if original_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original_value
        _clear_config_caches()


def _clear_config_caches() -> None:
    config_module.get_settings.cache_clear()
    config_module._load_review_config_cached.cache_clear()


def _provider_override(value: str | None) -> ProviderName | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized not in _PROVIDERS:
        raise CliUsageError(
            f"Unsupported provider {value!r}; expected one of: {', '.join(_PROVIDERS)}."
        )
    return normalized


def _fail_on_override(value: str | None) -> FailOnThreshold | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized not in _FAIL_THRESHOLDS:
        raise CliUsageError(
            f"Unsupported fail threshold {value!r}; expected one of: {', '.join(_FAIL_THRESHOLDS)}."
        )
    return normalized


def _resolve_repo_root(repo: Path | None) -> Path | None:
    """Return the top-level checkout path when known, else ``None`` for diff-only runs."""

    if repo is not None:
        candidate = repo.expanduser().resolve()
        if not candidate.is_dir():
            raise CliUsageError(
                f"Repository path does not exist or is not a directory: {candidate}"
            )
        return _git_root_or_path(candidate)
    return _try_git_root(Path.cwd())


def _git_root_or_path(path: Path) -> Path:
    return _try_git_root(path) or path


def _try_git_root(path: Path) -> Path | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except FileNotFoundError:
        return None
    if proc.returncode != 0:
        return None
    root = proc.stdout.strip()
    return Path(root).resolve() if root else None


def _load_diff(range_spec: str | None, repo_root: Path | None) -> tuple[str, str | None]:
    """Load the diff from a range, stdin, or ``git diff`` in that order."""

    normalized_range = _normalize_range(range_spec)
    if normalized_range is not None:
        repo = _require_git_repo(repo_root, reason="a git range was provided")
        return _git_diff(repo, normalized_range), _head_ref_from_range(normalized_range)

    stdin_diff = _read_stdin_diff()
    if stdin_diff is not None and stdin_diff != "":
        return stdin_diff, None

    repo = _require_git_repo(repo_root, reason="no stdin diff was provided")
    return _git_diff(repo, None), None


def _normalize_range(range_spec: str | None) -> str | None:
    if range_spec is None:
        return None
    normalized = range_spec.strip()
    if not normalized:
        return None
    if normalized.startswith("-"):
        raise CliUsageError("Git range must not start with '-'.")
    return normalized


def _read_stdin_diff() -> str | None:
    if sys.stdin is None or sys.stdin.isatty():
        return None
    return sys.stdin.read()


def _require_git_repo(repo_root: Path | None, *, reason: str) -> Path:
    if repo_root is None:
        raise CliUsageError(
            f"Unable to run git diff because {reason} and the current directory is not a git repo. "
            "Pass --repo PATH or pipe a unified diff on stdin."
        )
    git_root = _try_git_root(repo_root)
    if git_root is None:
        raise CliUsageError(f"Unable to run git diff: {repo_root} is not a git repository.")
    return git_root


def _git_diff(repo_root: Path, range_spec: str | None) -> str:
    args = ["git", "-C", str(repo_root), "diff", "--no-ext-diff"]
    if range_spec is not None:
        args.append(range_spec)
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        proc = subprocess.run(
            args,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError as exc:
        raise CliUsageError("git is not available on PATH.") from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip() or "unknown git diff failure"
        raise CliUsageError(f"git diff failed: {detail}")
    return proc.stdout


def _head_ref_from_range(range_spec: str) -> str | None:
    if "..." in range_spec:
        head = range_spec.rsplit("...", maxsplit=1)[1].strip()
        return head or "HEAD"
    if ".." in range_spec:
        head = range_spec.rsplit("..", maxsplit=1)[1].strip()
        return head or "HEAD"
    return None


def _warn_if_config_ignored_in_ci(config_path: Path | None) -> None:
    if config_path is None or not config_module._is_ci():
        return

    log.warning(
        "cli_config_ignored_in_ci",
        config=str(config_path),
        trusted_config_ref=os.environ.get("TRUSTED_CONFIG_REF", ""),
        trusted_config_path=os.environ.get("TRUSTED_CONFIG_PATH", "review.toml"),
    )
    typer.echo(
        "Warning: --config is ignored in CI; review.toml is read from "
        "TRUSTED_CONFIG_REF/TRUSTED_CONFIG_PATH instead.",
        err=True,
    )


def _invoke_agent(
    *,
    diff: str,
    repo_root: Path | None,
    head_ref: str | None,
    reporter_override: str | None,
    provider_override: ProviderName | None,
    model_override: str | None,
    fail_on_override: FailOnThreshold | None,
) -> Mapping[str, Any]:
    result = agent.invoke(
        {
            "diff": diff,
            "repo_root": str(repo_root) if repo_root is not None else None,
            "head_ref": head_ref,
            "reporter_override": reporter_override,
            "llm_provider_override": provider_override,
            "llm_model_override": model_override,
            "fail_on_override": fail_on_override,
        }
    )
    if not isinstance(result, Mapping):
        raise RuntimeError(f"Graph returned unexpected result type: {type(result).__name__}")
    return result


def _resolve_fail_on_threshold(override: FailOnThreshold | None) -> FailOnThreshold:
    if override is not None:
        return override
    return load_review_config().report.fail_on


def _findings_from_result(result: Mapping[str, Any]) -> list[Finding]:
    raw_findings = result.get("findings", [])
    if not isinstance(raw_findings, list):
        return []

    findings: list[Finding] = []
    for raw in raw_findings:
        if isinstance(raw, Finding):
            findings.append(raw)
        else:
            findings.append(Finding.model_validate(raw))
    return findings


def _exit_code_for_findings(findings: list[Finding], threshold: FailOnThreshold) -> int:
    if threshold == "off":
        return 0
    threshold_value = _SEVERITY_VALUE[threshold]
    return (
        1
        if any(_SEVERITY_VALUE[finding.severity] >= threshold_value for finding in findings)
        else 0
    )


def _exit_with_error(message: str, *, code: int) -> None:
    typer.echo(message, err=True)
    raise typer.Exit(code=code)

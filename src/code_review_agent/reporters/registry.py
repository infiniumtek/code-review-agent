"""Reporter selection and dispatch."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path

import structlog

from code_review_agent.config import ReviewConfig, Settings
from code_review_agent.reporters.base import ReportContext, ReporterFn
from code_review_agent.reporters.file import write_report as write_file_report
from code_review_agent.reporters.github import write_report as write_github_report
from code_review_agent.reporters.gitlab import write_report as write_gitlab_report
from code_review_agent.reporters.terminal import write_report as write_terminal_report
from code_review_agent.utils.state import AgentState

log = structlog.get_logger(__name__)

_DEFAULT_REPORTER = "auto"
_DEFAULT_REPORT_DIR = Path(".")

BUILTIN_REPORTERS: dict[str, ReporterFn] = {
    "terminal": write_terminal_report,
    "file": write_file_report,
    "github": write_github_report,
    "gitlab": write_gitlab_report,
}


def resolve_reporter_names(
    state: AgentState,
    settings: Settings,
    review_config: ReviewConfig,
) -> list[str]:
    """Resolve reporter names with precedence CLI > env > review.toml > auto."""

    selection: str | Sequence[str]
    if state.reporter_override is not None:
        selection = state.reporter_override
    elif _settings_reporter_overrides_config(settings):
        selection = settings.reporter
    else:
        selection = review_config.report.reporters
    names = _parse_reporter_selection(selection)
    return _dedupe_preserving_order(_expand_auto_reporters(names))


def resolve_report_dir(settings: Settings, review_config: ReviewConfig) -> Path:
    """Resolve artifact directory, with env overriding ``review.toml``."""

    if _settings_report_dir_overrides_config(settings):
        return settings.report_dir
    return Path(review_config.report.report_dir)


def run_reporters(
    context: ReportContext,
    reporter_names: Sequence[str],
    *,
    registry: Mapping[str, ReporterFn] | None = None,
) -> None:
    """Run every selected reporter independently.

    Unknown reporter names and reporter exceptions are logged and do not stop
    later reporters from running.
    """

    reporters = registry or BUILTIN_REPORTERS
    recognized = 0
    for name in reporter_names:
        reporter = reporters.get(name)
        if reporter is None:
            log.warning("reporter_unknown", reporter=name)
            continue
        recognized += 1
        _run_reporter(name, reporter, context)

    if reporter_names and recognized == 0 and "terminal" in reporters:
        log.warning(
            "reporter_fallback",
            requested=list(reporter_names),
            fallback="terminal",
            reason="no_requested_reporters_are_registered",
        )
        _run_reporter("terminal", reporters["terminal"], context)


def _run_reporter(name: str, reporter: ReporterFn, context: ReportContext) -> None:
    try:
        reporter(context)
    except Exception as exc:  # pragma: no cover - exact failures are reporter-specific
        log.warning("reporter_failed", reporter=name, error=str(exc), exc_info=True)


def _settings_reporter_overrides_config(settings: Settings) -> bool:
    if _real_env_var_is_set("REPORTER"):
        return True
    if "reporter" not in settings.model_fields_set:
        return False
    return _parse_reporter_selection(settings.reporter) != [_DEFAULT_REPORTER]


def _settings_report_dir_overrides_config(settings: Settings) -> bool:
    if _real_env_var_is_set("REPORT_DIR"):
        return True
    if "report_dir" not in settings.model_fields_set:
        return False
    return settings.report_dir != _DEFAULT_REPORT_DIR


def _real_env_var_is_set(name: str) -> bool:
    return any(key.upper() == name for key in os.environ)


def _parse_reporter_selection(selection: str | Sequence[str]) -> list[str]:
    raw_parts = [selection] if isinstance(selection, str) else list(selection)
    names: list[str] = []
    for raw_part in raw_parts:
        names.extend(part.strip().lower() for part in raw_part.split(",") if part.strip())
    return names


def _expand_auto_reporters(names: Sequence[str]) -> list[str]:
    expanded: list[str] = []
    for name in names:
        if name == "auto":
            expanded.extend(_auto_reporter_names())
        else:
            expanded.append(name)
    return expanded


def _auto_reporter_names() -> list[str]:
    if os.environ.get("GITHUB_ACTIONS"):
        return ["github", "terminal"]
    if os.environ.get("GITLAB_CI"):
        return ["gitlab", "terminal"]
    if os.environ.get("JENKINS_URL"):
        return ["terminal", "file"]
    return ["terminal", "file"]


def _dedupe_preserving_order(names: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        deduped.append(name)
    return deduped

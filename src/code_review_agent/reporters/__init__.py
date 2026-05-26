"""Reporter registry and built-in reporter exports."""

from __future__ import annotations

from code_review_agent.reporters.base import ReportContext, ReporterFn
from code_review_agent.reporters.registry import (
    BUILTIN_REPORTERS,
    resolve_report_dir,
    resolve_reporter_names,
    run_reporters,
)
from code_review_agent.reporters.scm import BOT_COMMENT_MARKER

__all__ = [
    "BOT_COMMENT_MARKER",
    "BUILTIN_REPORTERS",
    "ReportContext",
    "ReporterFn",
    "resolve_report_dir",
    "resolve_reporter_names",
    "run_reporters",
]

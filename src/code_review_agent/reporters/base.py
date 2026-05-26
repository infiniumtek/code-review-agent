"""Shared reporter data shapes."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, Field

from code_review_agent.utils.state import Finding


class ReportContext(BaseModel):
    """Everything a reporter needs to publish a rendered review report."""

    report: str
    findings: list[Finding] = Field(default_factory=list)
    report_dir: Path = Path(".")
    advisory_disclaimer: str


ReporterFn = Callable[[ReportContext], None]

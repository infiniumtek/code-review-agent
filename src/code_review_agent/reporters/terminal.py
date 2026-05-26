"""Terminal reporter."""

from __future__ import annotations

import sys

from code_review_agent.reporters.base import ReportContext


def write_report(context: ReportContext) -> None:
    """Write the Markdown report to stdout for CI logs and local runs."""

    sys.stdout.write(context.report)
    if not context.report.endswith("\n"):
        sys.stdout.write("\n")

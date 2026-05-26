"""Durable Markdown and JSON artifact reporter."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from code_review_agent.reporters.base import ReportContext
from code_review_agent.utils.state import Category, Severity

MARKDOWN_REPORT_FILENAME = "review-report.md"
JSON_REPORT_FILENAME = "review-report.json"
_SEVERITY_ORDER: tuple[Severity, ...] = ("critical", "high", "medium", "low", "info")
_CATEGORY_ORDER: tuple[Category, ...] = ("security", "bug", "performance", "improvement")


def write_report(context: ReportContext) -> None:
    """Write review artifacts under ``context.report_dir``."""

    context.report_dir.mkdir(parents=True, exist_ok=True)
    markdown = context.report if context.report.endswith("\n") else f"{context.report}\n"
    (context.report_dir / MARKDOWN_REPORT_FILENAME).write_text(markdown, encoding="utf-8")
    (context.report_dir / JSON_REPORT_FILENAME).write_text(
        f"{json.dumps(_json_payload(context), indent=2)}\n",
        encoding="utf-8",
    )


def _json_payload(context: ReportContext) -> dict[str, Any]:
    """Build the durable JSON artifact payload.

    ``report`` is the only safe-to-render Markdown surface. ``findings`` preserve
    raw structured model output for machine consumers; escape those strings
    before rendering them into HTML, Markdown, or SCM comments.
    """

    severity_counts = Counter(finding.severity for finding in context.findings)
    category_counts = Counter(finding.category for finding in context.findings)
    return {
        "advisory": context.advisory_disclaimer,
        "rendering": {
            "safe_markdown_field": "report",
            "raw_structured_fields": ["findings"],
        },
        "summary": {
            "finding_count": len(context.findings),
            "by_severity": _ordered_counts(severity_counts, _SEVERITY_ORDER),
            "by_category": _ordered_counts(category_counts, _CATEGORY_ORDER),
        },
        "findings": [finding.model_dump(mode="json") for finding in context.findings],
        "report": context.report,
    }


def _ordered_counts[T: str](counts: Counter[T], order: tuple[T, ...]) -> dict[T, int]:
    return {key: counts[key] for key in order if counts[key]}

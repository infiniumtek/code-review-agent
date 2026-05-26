"""Shared helpers for source-control-management comment reporters."""

from __future__ import annotations

from code_review_agent.reporters.base import ReportContext

BOT_COMMENT_MARKER = "<!-- code-review-agent -->"


def marked_report_body(context: ReportContext) -> str:
    """Return the Markdown body stored in an idempotent SCM comment."""

    report = context.report.rstrip()
    return f"{BOT_COMMENT_MARKER}\n{report}\n"


def find_marked_resource_id(resources: object) -> int | None:
    """Find the first resource whose ``body`` contains our hidden marker.

    If duplicate marked comments exist, callers update the first one returned
    by the SCM API; duplicate reconciliation is intentionally out of scope here.
    """

    if not isinstance(resources, list):
        return None
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        if resource.get("system") is True:
            continue
        body = resource.get("body")
        if not isinstance(body, str) or BOT_COMMENT_MARKER not in body:
            continue
        resource_id = positive_int(resource.get("id"))
        if resource_id is not None:
            return resource_id
    return None


def positive_int(value: object) -> int | None:
    """Coerce positive integer IDs from JSON payload values."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


def nonempty(value: str | None) -> str | None:
    """Strip env values and normalize empty strings to ``None``."""

    if value is None:
        return None
    stripped = value.strip()
    return stripped or None

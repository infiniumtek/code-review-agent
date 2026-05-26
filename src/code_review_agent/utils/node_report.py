"""Report node helpers for the review pipeline."""

from __future__ import annotations

from code_review_agent.utils.state import AgentState, Finding

ADVISORY_DISCLAIMER = (
    "Advisory: code-review-agent findings are generated assistance; verify them before acting."
)


def render_report(state: AgentState) -> str:
    """Render the aggregated findings into a stable Markdown report string."""

    lines = [
        "# Code Review Report",
        "",
        ADVISORY_DISCLAIMER,
        "",
    ]
    if not state.findings:
        lines.append("No findings.")
        return "\n".join(lines)

    finding_label = "finding" if len(state.findings) == 1 else "findings"
    lines.append(f"{len(state.findings)} {finding_label}.")
    for finding in state.findings:
        lines.extend(_render_finding(finding))
    return "\n".join(lines)


def report(state: AgentState) -> dict[str, str]:
    """LangGraph report node: materialize the rendered report.

    Reporter dispatch and side effects are Phase 11. Phase 10 keeps this node
    pure so the fully wired graph can terminate with ``AgentState.report`` set.
    """

    return {"report": render_report(state)}


def _render_finding(finding: Finding) -> list[str]:
    location = finding.path if finding.line is None else f"{finding.path}:{finding.line}"
    return [
        "",
        f"## [{finding.severity}] {_single_line_report_text(finding.title)}",
        "",
        f"- Path: {_inline_code(location)}",
        f"- Category: `{finding.category}`",
        f"- Skill: `{finding.skill_key}`",
        "",
        _detail_block(finding.detail),
    ]


def _single_line_report_text(value: str) -> str:
    """Render untrusted text into one structural Markdown line."""

    collapsed = " ".join(_escaped_report_text(value).split())
    return collapsed or "(untitled finding)"


def _escaped_report_text(value: str) -> str:
    """Neutralize raw HTML/comment markers and mentions in LLM-controlled text."""

    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("@", "&#64;")
    )


def _detail_block(value: str) -> str:
    text = _escaped_report_text(value)
    fence = "`" * max(3, _longest_backtick_run(text) + 1)
    trailing_newline = "" if text.endswith("\n") else "\n"
    return f"{fence}\n{text}{trailing_newline}{fence}"


def _inline_code(value: str) -> str:
    text = _single_line_plain_text(value)
    delimiter = "`" * (_longest_backtick_run(text) + 1)
    padding = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{delimiter}{padding}{text}{padding}{delimiter}"


def _single_line_plain_text(value: str) -> str:
    return " ".join(value.split()) or "(empty)"


def _longest_backtick_run(value: str) -> int:
    longest = 0
    current = 0
    for char in value:
        if char == "`":
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest

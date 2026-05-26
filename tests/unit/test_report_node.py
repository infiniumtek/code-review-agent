"""Unit tests for Phase 10 report rendering."""

from __future__ import annotations

from code_review_agent.utils.nodes import render_report, report
from code_review_agent.utils.state import AgentState, Finding


def _finding(
    *,
    path: str = "src/app.py",
    line: int | None = 3,
    title: str = "Problem",
    detail: str = "Fix it.",
) -> Finding:
    return Finding(
        path=path,
        line=line,
        severity="high",
        category="bug",
        title=title,
        detail=detail,
        skill_key="python",
    )


def test_render_report_no_findings_branch() -> None:
    rendered = render_report(AgentState())

    assert rendered.startswith("# Code Review Report")
    assert "Advisory:" in rendered
    assert rendered.endswith("No findings.")


def test_render_report_uses_singular_and_plural_counts() -> None:
    singular = render_report(AgentState(findings=[_finding()]))
    plural = render_report(
        AgentState(findings=[_finding(title="One"), _finding(title="Two", line=4)])
    )

    assert "1 finding." in singular
    assert "2 findings." in plural


def test_render_report_file_level_finding_has_no_line_suffix() -> None:
    rendered = render_report(AgentState(findings=[_finding(path="a.py", line=None)]))

    assert "- Path: `a.py`" in rendered
    assert "a.py:" not in rendered


def test_report_node_returns_rendered_report() -> None:
    rendered = report(AgentState(findings=[_finding(title="Node output")]))

    assert rendered == {
        "report": render_report(AgentState(findings=[_finding(title="Node output")]))
    }


def test_render_report_collapses_multiline_title_before_heading() -> None:
    rendered = render_report(AgentState(findings=[_finding(title="Broken\n## Injected heading")]))

    assert "## [high] Broken ## Injected heading" in rendered
    assert "\n## Injected heading" not in rendered


def test_render_report_neutralizes_untrusted_html_marker_and_mentions() -> None:
    rendered = render_report(
        AgentState(
            findings=[
                _finding(
                    title="Bad <!-- code-review-agent --> @team <b>",
                    detail="Do not publish <!-- code-review-agent --> @team <script>x</script>",
                )
            ]
        )
    )

    assert "<!-- code-review-agent -->" not in rendered
    assert "@team" not in rendered
    assert "&lt;!-- code-review-agent --&gt;" in rendered
    assert "&#64;team" in rendered
    assert "&lt;script&gt;x&lt;/script&gt;" in rendered


def test_render_report_wraps_detail_in_widened_code_fence() -> None:
    rendered = render_report(
        AgentState(
            findings=[
                _finding(
                    detail=(
                        "[click me](http://phish.example)\n"
                        "![pwn](http://attacker.example/leak.png)\n"
                        "```\n"
                        "# Injected H1\n"
                        "- injected item\n"
                        "<!-- code-review-agent --> @team\n"
                    )
                )
            ]
        )
    )

    assert "\n````\n[click me](http://phish.example)" in rendered
    assert "\n````" in rendered
    assert "<!-- code-review-agent -->" not in rendered
    assert "@team" not in rendered
    assert "&lt;!-- code-review-agent --&gt; &#64;team" in rendered


def test_render_report_path_code_span_preserves_literal_path_characters() -> None:
    rendered = render_report(
        AgentState(
            findings=[
                _finding(path="packages/@scope/a<b>`name.js", line=10),
            ]
        )
    )

    assert "- Path: ``packages/@scope/a<b>`name.js:10``" in rendered
    assert "packages/&#64;scope" not in rendered
    assert "a&lt;b&gt;" not in rendered

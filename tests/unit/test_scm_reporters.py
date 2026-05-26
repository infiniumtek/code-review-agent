"""Unit tests for GitHub and GitLab SCM reporters."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from code_review_agent.reporters import ReportContext
from code_review_agent.reporters import gitlab as gitlab_reporter
from code_review_agent.reporters.github import _COMMENT_BODY_LIMIT
from code_review_agent.reporters.github import publish_report as publish_github_report
from code_review_agent.reporters.gitlab import publish_report as publish_gitlab_report
from code_review_agent.reporters.http import JsonHttpResponse, JsonHttpTransport
from code_review_agent.reporters.scm import BOT_COMMENT_MARKER, find_marked_resource_id
from code_review_agent.utils.node_report import ADVISORY_DISCLAIMER, render_report
from code_review_agent.utils.state import AgentState, Finding


@dataclass(frozen=True)
class RequestRecord:
    method: str
    url: str
    headers: Mapping[str, str]
    payload: Mapping[str, object] | None


class FakeLog:
    def __init__(self) -> None:
        self.infos: list[tuple[str, dict[str, object]]] = []
        self.warnings: list[tuple[str, dict[str, object]]] = []

    def info(self, event: str, **kwargs: object) -> None:
        self.infos.append((event, kwargs))

    def warning(self, event: str, **kwargs: object) -> None:
        self.warnings.append((event, kwargs))


class FakeTransport(JsonHttpTransport):
    def __init__(self, responses: list[object]) -> None:
        self._responses = responses
        self.requests: list[RequestRecord] = []

    def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, object] | None = None,
    ) -> object:
        return self.request_json_response(
            method,
            url,
            headers=headers,
            payload=payload,
        ).payload

    def request_json_response(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, object] | None = None,
    ) -> JsonHttpResponse:
        self.requests.append(
            RequestRecord(method=method, url=url, headers=dict(headers), payload=payload)
        )
        raw_response = self._responses.pop(0)
        if isinstance(raw_response, tuple) and len(raw_response) == 2:
            response_payload, response_headers = raw_response
            if isinstance(response_headers, Mapping):
                return JsonHttpResponse(
                    payload=response_payload,
                    headers=dict(response_headers),
                )
        return JsonHttpResponse(payload=raw_response, headers={})


def _context(tmp_path: Path) -> ReportContext:
    findings = [
        Finding(
            path="src/app.py",
            line=12,
            severity="high",
            category="bug",
            title="Unchecked division",
            detail="Guard the denominator before dividing.",
            skill_key="python",
        )
    ]
    return ReportContext(
        report=render_report(AgentState(findings=findings)),
        findings=findings,
        report_dir=tmp_path,
        advisory_disclaimer=ADVISORY_DISCLAIMER,
    )


def test_github_reporter_creates_then_updates_marked_pr_comment(tmp_path: Path) -> None:
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps({"pull_request": {"number": 7}}), encoding="utf-8")
    env = {
        "GITHUB_TOKEN": "gh-token",
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_EVENT_PATH": str(event_path),
        "GITHUB_API_URL": "https://api.github.test",
    }
    transport = FakeTransport(
        [
            [],
            {"id": 42},
            [{"id": 42, "body": f"{BOT_COMMENT_MARKER}\nold"}],
            {"id": 42},
        ]
    )

    publish_github_report(_context(tmp_path), transport=transport, environ=env)
    publish_github_report(_context(tmp_path), transport=transport, environ=env)

    assert [request.method for request in transport.requests] == ["GET", "POST", "GET", "PATCH"]
    assert transport.requests[0].url == (
        "https://api.github.test/repos/owner/repo/issues/7/comments?per_page=100"
    )
    assert transport.requests[1].url == (
        "https://api.github.test/repos/owner/repo/issues/7/comments"
    )
    assert transport.requests[3].url == (
        "https://api.github.test/repos/owner/repo/issues/comments/42"
    )
    assert transport.requests[0].headers["Authorization"] == "Bearer gh-token"
    assert transport.requests[1].payload is not None
    assert str(transport.requests[1].payload["body"]).startswith(BOT_COMMENT_MARKER)


def test_github_reporter_follows_next_link_to_update_page_two_comment(tmp_path: Path) -> None:
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps({"pull_request": {"number": 7}}), encoding="utf-8")
    env = {
        "GITHUB_TOKEN": "gh-token",
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_EVENT_PATH": str(event_path),
        "GITHUB_API_URL": "https://api.github.test",
    }
    page_two_url = "https://api.github.test/repos/owner/repo/issues/7/comments?per_page=100&page=2"
    transport = FakeTransport(
        [
            (
                [{"id": 1, "body": "older discussion"}],
                {"Link": f'<{page_two_url}>; rel="next"'},
            ),
            [{"id": 42, "body": f"{BOT_COMMENT_MARKER}\nold"}],
            {"id": 42},
        ]
    )

    publish_github_report(_context(tmp_path), transport=transport, environ=env)

    assert [request.method for request in transport.requests] == ["GET", "GET", "PATCH"]
    assert transport.requests[0].url == (
        "https://api.github.test/repos/owner/repo/issues/7/comments?per_page=100"
    )
    assert transport.requests[1].url == page_two_url
    assert transport.requests[2].url == (
        "https://api.github.test/repos/owner/repo/issues/comments/42"
    )


def test_github_reporter_truncates_comment_body_to_platform_limit(tmp_path: Path) -> None:
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps({"pull_request": {"number": 7}}), encoding="utf-8")
    env = {
        "GITHUB_TOKEN": "gh-token",
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_EVENT_PATH": str(event_path),
    }
    context = ReportContext(
        report=f"# Code Review Report\n\n{'x' * 70_000}",
        findings=[],
        report_dir=tmp_path,
        advisory_disclaimer=ADVISORY_DISCLAIMER,
    )
    transport = FakeTransport([[], {"id": 42}])

    publish_github_report(context, transport=transport, environ=env)

    assert transport.requests[1].payload is not None
    posted_body = transport.requests[1].payload["body"]
    assert isinstance(posted_body, str)
    assert len(posted_body) <= _COMMENT_BODY_LIMIT
    assert posted_body.startswith(BOT_COMMENT_MARKER)
    assert "Report truncated" in posted_body


def test_github_reporter_uses_pull_ref_when_event_path_is_absent(tmp_path: Path) -> None:
    env = {
        "GITHUB_TOKEN": "gh-token",
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_REF": "refs/pull/55/merge",
    }
    transport = FakeTransport([[], {"id": 55}])

    publish_github_report(_context(tmp_path), transport=transport, environ=env)

    assert transport.requests[0].url.endswith("/issues/55/comments?per_page=100")


def test_github_reporter_skips_when_required_env_is_missing(tmp_path: Path) -> None:
    transport = FakeTransport([])

    publish_github_report(_context(tmp_path), transport=transport, environ={})

    assert transport.requests == []


def test_gitlab_reporter_creates_then_updates_marked_mr_note(tmp_path: Path) -> None:
    env = {
        "GITLAB_TOKEN": "gl-token",
        "CI_PROJECT_ID": "123",
        "CI_MERGE_REQUEST_IID": "8",
        "CI_API_V4_URL": "https://gitlab.test/api/v4",
    }
    transport = FakeTransport(
        [
            [],
            {"id": 77},
            [{"id": 77, "body": f"{BOT_COMMENT_MARKER}\nold"}],
            {"id": 77},
        ]
    )

    publish_gitlab_report(_context(tmp_path), transport=transport, environ=env)
    publish_gitlab_report(_context(tmp_path), transport=transport, environ=env)

    assert [request.method for request in transport.requests] == ["GET", "POST", "GET", "PUT"]
    assert transport.requests[0].url == (
        "https://gitlab.test/api/v4/projects/123/merge_requests/8/notes?per_page=100&page=1"
    )
    assert transport.requests[1].url == (
        "https://gitlab.test/api/v4/projects/123/merge_requests/8/notes"
    )
    assert transport.requests[3].url == (
        "https://gitlab.test/api/v4/projects/123/merge_requests/8/notes/77"
    )
    assert transport.requests[0].headers["PRIVATE-TOKEN"] == "gl-token"
    assert transport.requests[1].payload is not None
    assert str(transport.requests[1].payload["body"]).startswith(BOT_COMMENT_MARKER)


def test_gitlab_reporter_updates_marked_mr_note_on_page_two(tmp_path: Path) -> None:
    env = {
        "GITLAB_TOKEN": "gl-token",
        "CI_PROJECT_ID": "123",
        "CI_MERGE_REQUEST_IID": "8",
        "CI_API_V4_URL": "https://gitlab.test/api/v4",
    }
    transport = FakeTransport(
        [
            [
                {
                    "id": 1,
                    "body": f"{BOT_COMMENT_MARKER}\nquoted in a system note",
                    "system": True,
                }
            ],
            [{"id": 77, "body": f"{BOT_COMMENT_MARKER}\nold"}],
            {"id": 77},
        ]
    )

    publish_gitlab_report(_context(tmp_path), transport=transport, environ=env)

    assert [request.method for request in transport.requests] == ["GET", "GET", "PUT"]
    assert transport.requests[0].url == (
        "https://gitlab.test/api/v4/projects/123/merge_requests/8/notes?per_page=100&page=1"
    )
    assert transport.requests[1].url == (
        "https://gitlab.test/api/v4/projects/123/merge_requests/8/notes?per_page=100&page=2"
    )
    assert transport.requests[2].url == (
        "https://gitlab.test/api/v4/projects/123/merge_requests/8/notes/77"
    )


def test_gitlab_reporter_url_encodes_project_path(tmp_path: Path) -> None:
    env = {
        "GITLAB_TOKEN": "gl-token",
        "CI_PROJECT_PATH": "group/sub/repo",
        "CI_MERGE_REQUEST_IID": "9",
    }
    transport = FakeTransport([[], {"id": 78}])

    publish_gitlab_report(_context(tmp_path), transport=transport, environ=env)

    assert "/projects/group%2Fsub%2Frepo/merge_requests/9/notes" in transport.requests[0].url


def test_gitlab_reporter_can_use_ci_job_token_with_warning_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_log = FakeLog()
    monkeypatch.setattr(gitlab_reporter, "log", fake_log)
    env = {
        "CI_JOB_TOKEN": "job-token",
        "CI_PROJECT_ID": "123",
        "CI_MERGE_REQUEST_IID": "8",
    }
    transport = FakeTransport([[], {"id": 79}])

    publish_gitlab_report(_context(tmp_path), transport=transport, environ=env)

    assert transport.requests[0].headers["JOB-TOKEN"] == "job-token"
    assert "PRIVATE-TOKEN" not in transport.requests[0].headers
    assert fake_log.warnings == [
        (
            "gitlab_reporter_job_token_may_not_create_notes",
            {
                "detail": (
                    "CI_JOB_TOKEN usually cannot create or update merge request notes; "
                    "set GITLAB_TOKEN."
                )
            },
        )
    ]


def test_gitlab_reporter_skips_when_required_env_is_missing(tmp_path: Path) -> None:
    transport = FakeTransport([])

    publish_gitlab_report(_context(tmp_path), transport=transport, environ={})

    assert transport.requests == []


def test_find_marked_resource_id_ignores_non_comment_shapes_and_system_notes() -> None:
    resources = [
        None,
        "not a resource",
        {"id": 1, "body": None},
        {"id": 2, "body": f"{BOT_COMMENT_MARKER}\nquoted", "system": True},
        {"id": "3", "body": f"{BOT_COMMENT_MARKER}\nactual"},
    ]

    assert find_marked_resource_id(resources) == 3

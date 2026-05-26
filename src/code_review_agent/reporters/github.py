"""GitHub pull request comment reporter."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import structlog

from code_review_agent.reporters.base import ReportContext
from code_review_agent.reporters.http import (
    JsonHttpTransport,
    UrllibJsonHttpTransport,
    request_json_response,
)
from code_review_agent.reporters.scm import (
    BOT_COMMENT_MARKER,
    find_marked_resource_id,
    marked_report_body,
    nonempty,
    positive_int,
)

log = structlog.get_logger(__name__)

_DEFAULT_API_URL = "https://api.github.com"
_PULL_REF_RE = re.compile(r"(?:refs/pull/)?(?P<number>\d+)/(?:merge|head)$")
_COMMENT_BODY_LIMIT = 65_536
_TRUNCATION_FOOTER = (
    "\n\n_Report truncated because GitHub issue comments are limited to "
    "65,536 characters. See the CI log or file artifact for the full report._\n"
)


@dataclass(frozen=True)
class GitHubTarget:
    """Resolved GitHub API target for a PR issue comment."""

    api_url: str
    token: str
    repository: str
    issue_number: int


def write_report(context: ReportContext) -> None:
    """Publish the report as an idempotent GitHub PR comment."""

    publish_report(context)


def publish_report(
    context: ReportContext,
    *,
    transport: JsonHttpTransport | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Create or update the single marked GitHub PR comment."""

    env = environ or os.environ
    target = _target_from_env(env)
    if target is None:
        return

    http = transport or UrllibJsonHttpTransport()
    headers = _headers(target.token)
    repo_path = _quoted_repository(target.repository)
    comments_url = f"{target.api_url}/repos/{repo_path}/issues/{target.issue_number}/comments"
    body = {"body": _github_comment_body(context)}

    comment_id = _find_existing_comment_id(http, comments_url, headers)
    if comment_id is None:
        created = http.request_json("POST", comments_url, headers=headers, payload=body)
        log.info(
            "github_report_created",
            issue_number=target.issue_number,
            comment_id=_json_id(created),
        )
        return

    http.request_json(
        "PATCH",
        f"{target.api_url}/repos/{repo_path}/issues/comments/{comment_id}",
        headers=headers,
        payload=body,
    )
    log.info(
        "github_report_updated",
        issue_number=target.issue_number,
        comment_id=comment_id,
    )


def _target_from_env(environ: Mapping[str, str]) -> GitHubTarget | None:
    token = nonempty(environ.get("GITHUB_TOKEN"))
    repository = nonempty(environ.get("GITHUB_REPOSITORY"))
    issue_number = _issue_number_from_env(environ)
    missing: list[str] = []

    if token is None:
        missing.append("GITHUB_TOKEN")
    if repository is None or not _valid_repository(repository):
        missing.append("GITHUB_REPOSITORY")
    if issue_number is None:
        missing.append("pull_request.number")

    if missing:
        log.warning("github_reporter_unconfigured", missing=missing)
        return None

    assert token is not None
    assert repository is not None
    assert issue_number is not None
    return GitHubTarget(
        api_url=(nonempty(environ.get("GITHUB_API_URL")) or _DEFAULT_API_URL).rstrip("/"),
        token=token,
        repository=repository,
        issue_number=issue_number,
    )


def _issue_number_from_env(environ: Mapping[str, str]) -> int | None:
    event_number = _issue_number_from_event_path(nonempty(environ.get("GITHUB_EVENT_PATH")))
    if event_number is not None:
        return event_number
    for ref_var in ("GITHUB_REF", "GITHUB_REF_NAME"):
        ref = nonempty(environ.get(ref_var))
        if ref is None:
            continue
        match = _PULL_REF_RE.search(ref)
        if match is not None:
            return positive_int(match.group("number"))
    return None


def _issue_number_from_event_path(event_path: str | None) -> int | None:
    if event_path is None:
        return None
    try:
        payload = json.loads(Path(event_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("github_event_unreadable", path=event_path, error=str(exc))
        return None

    if not isinstance(payload, dict):
        return None
    pull_request = payload.get("pull_request")
    if isinstance(pull_request, dict):
        number = positive_int(pull_request.get("number"))
        if number is not None:
            return number
    issue = payload.get("issue")
    if isinstance(issue, dict):
        number = positive_int(issue.get("number"))
        if number is not None:
            return number
    return positive_int(payload.get("number"))


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "code-review-agent",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _find_existing_comment_id(
    http: JsonHttpTransport,
    comments_url: str,
    headers: Mapping[str, str],
) -> int | None:
    url: str | None = f"{comments_url}?per_page=100"
    while url is not None:
        response = request_json_response(http, "GET", url, headers=headers)
        comment_id = find_marked_resource_id(response.payload)
        if comment_id is not None:
            return comment_id
        url = _next_link_url(response.headers)
    return None


def _next_link_url(headers: Mapping[str, str]) -> str | None:
    link_header = _header_value(headers, "Link")
    if link_header is None:
        return None
    for part in link_header.split(","):
        url_part, *params = part.split(";")
        url = url_part.strip()
        if not url.startswith("<") or not url.endswith(">"):
            continue
        for param in params:
            name, separator, value = param.strip().partition("=")
            if separator and name.lower() == "rel" and value.strip('"').lower() == "next":
                return url[1:-1]
    return None


def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


def _github_comment_body(context: ReportContext) -> str:
    body = marked_report_body(context)
    if len(body) <= _COMMENT_BODY_LIMIT:
        return body

    prefix = f"{BOT_COMMENT_MARKER}\n"
    available_report_chars = _COMMENT_BODY_LIMIT - len(prefix) - len(_TRUNCATION_FOOTER)
    truncated_report = context.report.rstrip()[:available_report_chars].rstrip()
    truncated_body = f"{prefix}{truncated_report}{_TRUNCATION_FOOTER}"
    log.warning(
        "github_report_truncated",
        original_chars=len(body),
        posted_chars=len(truncated_body),
        limit_chars=_COMMENT_BODY_LIMIT,
    )
    return truncated_body


def _quoted_repository(repository: str) -> str:
    owner, name = repository.split("/", 1)
    return f"{quote(owner, safe='')}/{quote(name, safe='')}"


def _valid_repository(repository: str) -> bool:
    owner, separator, name = repository.partition("/")
    return bool(owner and separator and name)


def _json_id(payload: object) -> int | None:
    if not isinstance(payload, dict):
        return None
    return positive_int(payload.get("id"))

"""GitLab merge request note reporter."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import quote

import structlog

from code_review_agent.reporters.base import ReportContext
from code_review_agent.reporters.http import JsonHttpTransport, UrllibJsonHttpTransport
from code_review_agent.reporters.scm import (
    find_marked_resource_id,
    marked_report_body,
    nonempty,
    positive_int,
)

log = structlog.get_logger(__name__)

_DEFAULT_API_URL = "https://gitlab.com/api/v4"


@dataclass(frozen=True)
class GitLabTarget:
    """Resolved GitLab API target for an MR note."""

    api_url: str
    auth_headers: Mapping[str, str]
    project_id: str
    merge_request_iid: str


def write_report(context: ReportContext) -> None:
    """Publish the report as an idempotent GitLab MR note."""

    publish_report(context)


def publish_report(
    context: ReportContext,
    *,
    transport: JsonHttpTransport | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Create or update the single marked GitLab MR note."""

    env = environ or os.environ
    target = _target_from_env(env)
    if target is None:
        return

    http = transport or UrllibJsonHttpTransport()
    headers = _headers(target.auth_headers)
    notes_url = (
        f"{target.api_url}/projects/{quote(target.project_id, safe='')}"
        f"/merge_requests/{quote(target.merge_request_iid, safe='')}/notes"
    )
    body = {"body": marked_report_body(context)}

    note_id = _find_existing_note_id(http, notes_url, headers)
    if note_id is None:
        created = http.request_json("POST", notes_url, headers=headers, payload=body)
        log.info(
            "gitlab_report_created",
            merge_request_iid=target.merge_request_iid,
            note_id=_json_id(created),
        )
        return

    http.request_json(
        "PUT",
        f"{notes_url}/{note_id}",
        headers=headers,
        payload=body,
    )
    log.info(
        "gitlab_report_updated",
        merge_request_iid=target.merge_request_iid,
        note_id=note_id,
    )


def _target_from_env(environ: Mapping[str, str]) -> GitLabTarget | None:
    auth_headers = _auth_headers_from_env(environ)
    project_id = (
        nonempty(environ.get("CI_PROJECT_ID"))
        or nonempty(environ.get("GITLAB_PROJECT_ID"))
        or nonempty(environ.get("CI_PROJECT_PATH"))
    )
    merge_request_iid = nonempty(environ.get("CI_MERGE_REQUEST_IID")) or nonempty(
        environ.get("GITLAB_MERGE_REQUEST_IID")
    )
    missing: list[str] = []

    if auth_headers is None:
        missing.append("GITLAB_TOKEN")
    if project_id is None:
        missing.append("CI_PROJECT_ID")
    if merge_request_iid is None:
        missing.append("CI_MERGE_REQUEST_IID")

    if missing:
        log.warning("gitlab_reporter_unconfigured", missing=missing)
        return None

    assert auth_headers is not None
    assert project_id is not None
    assert merge_request_iid is not None
    api_url = (
        nonempty(environ.get("CI_API_V4_URL"))
        or nonempty(environ.get("GITLAB_API_URL"))
        or _DEFAULT_API_URL
    )
    return GitLabTarget(
        api_url=api_url.rstrip("/"),
        auth_headers=auth_headers,
        project_id=project_id,
        merge_request_iid=merge_request_iid,
    )


def _auth_headers_from_env(environ: Mapping[str, str]) -> dict[str, str] | None:
    token = nonempty(environ.get("GITLAB_TOKEN"))
    if token is not None:
        return {"PRIVATE-TOKEN": token}
    job_token = nonempty(environ.get("CI_JOB_TOKEN"))
    if job_token is not None:
        log.warning(
            "gitlab_reporter_job_token_may_not_create_notes",
            detail=(
                "CI_JOB_TOKEN usually cannot create or update merge request notes; "
                "set GITLAB_TOKEN."
            ),
        )
        return {"JOB-TOKEN": job_token}
    return None


def _find_existing_note_id(
    http: JsonHttpTransport,
    notes_url: str,
    headers: Mapping[str, str],
) -> int | None:
    page = 1
    while True:
        notes = http.request_json("GET", f"{notes_url}?per_page=100&page={page}", headers=headers)
        note_id = find_marked_resource_id(notes)
        if note_id is not None:
            return note_id
        if not isinstance(notes, list) or not notes:
            return None
        page += 1


def _headers(auth_headers: Mapping[str, str]) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "User-Agent": "code-review-agent",
        **auth_headers,
    }


def _json_id(payload: object) -> int | None:
    if not isinstance(payload, dict):
        return None
    return positive_int(payload.get("id"))

"""Small JSON-over-HTTP transport used by SCM reporters."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class JsonHttpResponse:
    """Decoded JSON payload plus response headers."""

    payload: object
    headers: Mapping[str, str]


class JsonHttpTransport(Protocol):
    """Minimal transport protocol so reporter tests can mock HTTP cleanly."""

    def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, object] | None = None,
    ) -> object:
        """Send one JSON request and return the decoded JSON response."""


@runtime_checkable
class JsonHttpTransportWithHeaders(Protocol):
    """Optional transport extension for APIs that page via response headers."""

    def request_json_response(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, object] | None = None,
    ) -> JsonHttpResponse:
        """Send one JSON request and return decoded JSON plus response headers."""


def request_json_response(
    transport: JsonHttpTransport,
    method: str,
    url: str,
    *,
    headers: Mapping[str, str],
    payload: Mapping[str, object] | None = None,
) -> JsonHttpResponse:
    """Request JSON and preserve headers when the transport supports it."""

    if isinstance(transport, JsonHttpTransportWithHeaders):
        return transport.request_json_response(method, url, headers=headers, payload=payload)
    return JsonHttpResponse(
        payload=transport.request_json(method, url, headers=headers, payload=payload),
        headers={},
    )


class UrllibJsonHttpTransport:
    """Stdlib JSON transport; avoids adding an HTTP client dependency."""

    def __init__(self, *, timeout_seconds: int = 30) -> None:
        self._timeout_seconds = timeout_seconds

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
        request_headers = dict(headers)
        data: bytes | None = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")

        request = Request(url, data=data, headers=request_headers, method=method)
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                raw = response.read()
                response_headers = dict(response.headers.items())
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"{method} {url} failed with HTTP {exc.code}: {detail[:500]}"
            ) from exc
        except URLError as exc:
            raise RuntimeError(f"{method} {url} failed: {exc}") from exc

        if not raw.strip():
            return JsonHttpResponse(payload=None, headers=response_headers)
        try:
            payload_json = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{method} {url} returned non-JSON response") from exc
        return JsonHttpResponse(payload=payload_json, headers=response_headers)

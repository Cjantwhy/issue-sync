"""Thin async HTTP client for Jira Cloud REST API v3.

Handles authentication (API token over HTTP Basic), timeouts and the mapping of HTTP
failures onto the backend error types. It does not retry: retries happen in one place,
the arq job that called the backend, with backoff and a retry limit.
"""

import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx

from issue_sync.tickets.base import (
    TicketAuthError,
    TicketBackendError,
    TicketBackendUnavailable,
    TicketNotFound,
    TicketRequestRejected,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
API_PREFIX = "/rest/api/3"

_LOGIN_REASON_HEADER = "X-Seraph-LoginReason"
_LOGIN_FAILED = {"AUTHENTICATED_FAILED", "AUTHENTICATION_DENIED"}


class JiraClient:
    def __init__(
        self,
        base_url: str,
        email: str,
        api_token: str,
        *,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self.base_url + API_PREFIX,
            auth=httpx.BasicAuth(email, api_token),
            headers={"Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "JiraClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    def browse_url(self, key: str) -> str:
        return f"{self.base_url}/browse/{key}"

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        """Send a request; return the decoded JSON body, or None for an empty response."""
        try:
            response = await self._http.request(method, path, json=json, params=params)
        except httpx.TimeoutException as exc:
            raise TicketBackendUnavailable(f"Jira {method} {path} timed out") from exc
        except httpx.TransportError as exc:
            raise TicketBackendUnavailable(f"Jira {method} {path} failed: {exc!r}") from exc

        if response.is_success:
            return response.json() if response.content else None
        raise _error_for(method, path, response)

    async def paginate(
        self, path: str, *, items_key: str, params: Mapping[str, Any] | None = None
    ) -> AsyncIterator[Any]:
        """Iterate a startAt/maxResults paginated endpoint."""
        start_at = 0
        while True:
            page = await self.request(
                "GET", path, params={**(params or {}), "startAt": start_at, "maxResults": 50}
            )
            items = page.get(items_key) or []
            for item in items:
                yield item
            start_at += len(items)
            is_last = page.get("isLast")
            total = page.get("total")
            if (
                not items
                or is_last
                or (is_last is None and total is not None and start_at >= total)
            ):
                return


def _error_for(method: str, path: str, response: httpx.Response) -> TicketBackendError:
    status = response.status_code
    message = f"Jira {method} {path} returned {status}: {_error_details(response)}"
    if status == 429 or status >= 500:
        return TicketBackendUnavailable(message, retry_after=_retry_after(response))
    # A rejected token makes Jira Cloud treat the request as anonymous: resource
    # endpoints then answer 404, not 401. Only this header tells the two apart.
    if status in (401, 403) or response.headers.get(_LOGIN_REASON_HEADER) in _LOGIN_FAILED:
        return TicketAuthError(message)
    if status == 404:
        return TicketNotFound(message)
    return TicketRequestRejected(message)


def _error_details(response: httpx.Response) -> str:
    """Jira's error body: {"errorMessages": [...], "errors": {"field": "message"}}."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:500] or response.reason_phrase
    if not isinstance(body, dict):
        return str(body)[:500]
    parts = list(body.get("errorMessages") or [])
    parts += [f"{field}: {msg}" for field, msg in (body.get("errors") or {}).items()]
    return "; ".join(parts) or str(body)[:500]


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None  # HTTP-date form is not used by Jira Cloud; let the caller back off

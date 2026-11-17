"""OAuth 2.0 client credentials for an Atlassian service account.

The service account's client id and secret are exchanged at auth.atlassian.com for an
access token valid for 60 minutes, sent as a Bearer token to the API gateway
(api.atlassian.com/ex/jira/<cloudId>). Tokens are cached and refreshed shortly before
they expire; a 401 triggers one refresh and retry, in case a token was revoked early.
"""

import asyncio
import time
from collections.abc import AsyncGenerator, Callable, Generator

import httpx

from issue_sync.tickets.base import TicketAuthError, TicketBackendUnavailable

ATLASSIAN_TOKEN_URL = "https://auth.atlassian.com/oauth/token"
GATEWAY_URL = "https://api.atlassian.com/ex/jira/{cloud_id}"
REFRESH_MARGIN_SECONDS = 120


def gateway_url(cloud_id: str) -> str:
    return GATEWAY_URL.format(cloud_id=cloud_id)


class OAuthClientCredentials(httpx.Auth):
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        token_url: str = ATLASSIAN_TOKEN_URL,
        clock: Callable[[], float] = time.time,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_url = token_url
        self._clock = clock
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0), transport=transport
        )
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    def sync_auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response]:
        raise RuntimeError("OAuthClientCredentials only supports async clients")

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        request.headers["Authorization"] = f"Bearer {await self._access_token()}"
        response = yield request
        if response.status_code == 401:
            self._token = None
            request.headers["Authorization"] = f"Bearer {await self._access_token()}"
            yield request

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _access_token(self) -> str:
        async with self._lock:
            if self._token is None or self._clock() >= self._expires_at - REFRESH_MARGIN_SECONDS:
                await self._fetch()
            assert self._token is not None
            return self._token

    async def _fetch(self) -> None:
        now = self._clock()
        try:
            response = await self._http.post(
                self._token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
            )
        except httpx.HTTPError as exc:
            raise TicketBackendUnavailable(f"Atlassian token request failed: {exc!r}") from None
        if response.status_code == 429 or response.status_code >= 500:
            raise TicketBackendUnavailable(
                f"Atlassian token request returned {response.status_code}"
            )
        if response.status_code != 200:
            # e.g. invalid_client: a wrong or revoked credential. The body never echoes
            # the secret, but keep the message short anyway.
            raise TicketAuthError(
                f"Atlassian token request returned {response.status_code}: {response.text[:200]}"
            )
        body = response.json()
        self._token = body["access_token"]
        self._expires_at = now + float(body.get("expires_in", 3600))

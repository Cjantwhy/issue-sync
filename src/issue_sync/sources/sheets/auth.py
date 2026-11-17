"""OAuth access tokens for a Google service account (JWT bearer grant, RFC 7523).

google-auth is used only to sign the JWT; the token exchange goes through httpx, so it
is async and can be mocked in tests like every other HTTP call.
"""

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from google.auth import crypt
from google.auth import jwt as google_jwt

from issue_sync.sources.sheets.errors import SheetsError

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
TOKEN_LIFETIME_SECONDS = 3600
# Refresh this long before expiry so a token never expires mid-request.
REFRESH_MARGIN_SECONDS = 300


class ServiceAccountKey:
    def __init__(self, info: dict[str, Any]) -> None:
        missing = [k for k in ("client_email", "private_key", "token_uri") if not info.get(k)]
        if missing:
            raise ValueError(f"service account key is missing: {', '.join(missing)}")
        self.client_email: str = info["client_email"]
        self.token_uri: str = info["token_uri"]
        self._signer = crypt.RSASigner.from_service_account_info(info)

    @classmethod
    def from_file(cls, path: Path) -> "ServiceAccountKey":
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ValueError(f"service account key file not found: {path}") from None
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read service account key {path}: {exc}") from None
        return cls(info)

    def assertion(self, now: int) -> str:
        payload = {
            "iss": self.client_email,
            "scope": SHEETS_SCOPE,
            "aud": self.token_uri,
            "iat": now,
            "exp": now + TOKEN_LIFETIME_SECONDS,
        }
        return google_jwt.encode(self._signer, payload).decode()


class TokenProvider:
    """Caches one access token and refreshes it shortly before it expires."""

    def __init__(
        self,
        key: ServiceAccountKey,
        http: httpx.AsyncClient,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._key = key
        self._http = http
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def client_email(self) -> str:
        return self._key.client_email

    async def token(self) -> str:
        async with self._lock:
            if self._token is None or self._clock() >= self._expires_at - REFRESH_MARGIN_SECONDS:
                await self._refresh()
            assert self._token is not None
            return self._token

    def invalidate(self) -> None:
        self._token = None

    async def _refresh(self) -> None:
        now = int(self._clock())
        try:
            response = await self._http.post(
                self._key.token_uri,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": self._key.assertion(now),
                },
            )
        except httpx.HTTPError as exc:
            raise SheetsError(f"Google token request failed: {exc!r}", retryable=True) from None
        if response.status_code != 200:
            # invalid_grant here means a revoked/deleted key or a wrong clock.
            raise SheetsError(
                f"Google token request returned {response.status_code}: {response.text[:300]}",
                retryable=response.status_code >= 500,
            )
        body = response.json()
        self._token = body["access_token"]
        self._expires_at = now + float(body.get("expires_in", TOKEN_LIFETIME_SECONDS))

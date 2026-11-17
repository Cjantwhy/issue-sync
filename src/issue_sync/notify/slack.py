"""Minimal Slack Web API client: `chat.postMessage` with a bot token.

Slack answers most failures with HTTP 200 and `{"ok": false, "error": "..."}`; rate
limits come as HTTP 429 with Retry-After. Like the other clients, this one classifies
errors and leaves retrying to the arq job.
"""

from typing import Any

import httpx

from issue_sync.settings import SlackSettings

SLACK_API = "https://slack.com/api"
DEFAULT_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
# Slack error codes worth retrying; everything else (invalid_auth, channel_not_found,
# not_in_channel, ...) needs someone to fix the config.
RETRYABLE_ERRORS = {
    "ratelimited",
    "internal_error",
    "fatal_error",
    "service_unavailable",
    "request_timeout",
}


class SlackError(Exception):
    def __init__(
        self, message: str, *, retryable: bool = False, retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


class SlackClient:
    def __init__(self, http: httpx.AsyncClient, token: str, base_url: str = SLACK_API) -> None:
        self._http = http
        self._token = token
        self._base_url = base_url

    async def post_message(self, channel: str, text: str, blocks: list[dict[str, Any]]) -> str:
        """Post a message; returns its timestamp id."""
        body = await self._call(
            "chat.postMessage",
            {
                "channel": channel,
                "text": text,  # notification and accessibility fallback for the blocks
                "blocks": blocks,
                "unfurl_links": False,
                "unfurl_media": False,
            },
        )
        return str(body.get("ts", ""))

    async def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._http.post(
                f"{self._base_url}/{method}",
                json=payload,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=DEFAULT_TIMEOUT,
            )
        except httpx.TimeoutException:
            raise SlackError(f"Slack {method} timed out", retryable=True) from None
        except httpx.TransportError as exc:
            raise SlackError(f"Slack {method} failed: {exc!r}", retryable=True) from None
        if response.status_code == 429 or response.status_code >= 500:
            retry_after = response.headers.get("Retry-After")
            raise SlackError(
                f"Slack {method} returned {response.status_code}",
                retryable=True,
                retry_after=float(retry_after) if retry_after and retry_after.isdigit() else None,
            )
        try:
            body = response.json()
        except ValueError:
            raise SlackError(f"Slack {method} returned a non-JSON body", retryable=True) from None
        if not body.get("ok"):
            error = str(body.get("error", "unknown_error"))
            raise SlackError(f"Slack {method} failed: {error}", retryable=error in RETRYABLE_ERRORS)
        return body


def create_slack_client(settings: SlackSettings, http: httpx.AsyncClient) -> SlackClient:
    return SlackClient(http, settings.slack_bot_token.get_secret_value())

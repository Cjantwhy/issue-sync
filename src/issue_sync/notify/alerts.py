"""Operational alerts to Slack, de-duplicated so a lasting problem is not re-sent every run.

Problems found in one run are combined into one message. Each problem (kind + subject)
is reported at most once per `ALERT_TTL_SECONDS`. The "already reported" marks live in
Redis with a TTL: losing them costs at most one repeated alert, so they do not need a
database column.
"""

from dataclasses import dataclass
from typing import Any, Protocol

from issue_sync.notify.message import escape
from issue_sync.notify.slack import SlackClient

ALERT_TTL_SECONDS = 24 * 3600
MAX_LINES = 20


@dataclass(frozen=True)
class Alert:
    kind: str  # e.g. "push_failed", "mass_deletion"
    subject: str  # what it is about, e.g. "record:42"; with kind, the de-duplication key
    message: str


class AlertMarks(Protocol):
    async def seen(self, key: str) -> bool: ...

    async def remember(self, key: str, ttl_seconds: int) -> None: ...


class RedisAlertMarks:
    PREFIX = "issue-sync:alert:"

    def __init__(self, redis: Any) -> None:
        self._redis = redis

    async def seen(self, key: str) -> bool:
        return bool(await self._redis.exists(self.PREFIX + key))

    async def remember(self, key: str, ttl_seconds: int) -> None:
        await self._redis.set(self.PREFIX + key, "1", ex=ttl_seconds)


class Alerter:
    def __init__(self, slack: SlackClient, channel: str, marks: AlertMarks) -> None:
        self._slack = slack
        self._channel = channel
        self._marks = marks

    async def send(self, alerts: list[Alert]) -> int:
        """Post the alerts not reported recently; returns how many were posted."""
        fresh: dict[str, Alert] = {}
        for alert in alerts:
            key = f"{alert.kind}:{alert.subject}"
            if key not in fresh and not await self._marks.seen(key):
                fresh[key] = alert
        if not fresh:
            return 0
        text, blocks = build_alert_message(list(fresh.values()))
        await self._slack.post_message(self._channel, text, blocks)
        # Marked only after Slack accepted the message, so a failed post is retried.
        for key in fresh:
            await self._marks.remember(key, ALERT_TTL_SECONDS)
        return len(fresh)


def build_alert_message(alerts: list[Alert]) -> tuple[str, list[dict[str, Any]]]:
    title = f":warning: issue-sync found {len(alerts)} problem(s) that need attention"
    lines = [f"• {escape(a.message)}" for a in alerts[:MAX_LINES]]
    if len(alerts) > MAX_LINES:
        lines.append(f"…and {len(alerts) - MAX_LINES} more (see the worker log)")
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*"}},
        # A section's text is limited to 3000 characters.
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)[:2900]}},
    ]
    return title, blocks

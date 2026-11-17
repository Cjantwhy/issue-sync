"""Jira Cloud webhook payloads: signature check, slimming and parsing.

Registered as an admin webhook (Settings > System > WebHooks) with a secret, for the
events "Issue updated" and "Comment created". Jira signs each body with HMAC-SHA256 and
sends `X-Hub-Signature: sha256=<hex>`.
"""

import hashlib
import hmac
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from issue_sync.adapter_config import JiraConfig
from issue_sync.tickets.base import TicketComment, TicketEvent, TicketState
from issue_sync.tickets.jira import adf

SIGNATURE_HEADER = "X-Hub-Signature"
SUPPORTED_EVENTS = {"jira:issue_updated": "issue_updated", "comment_created": "comment_created"}
COMMENT_MAX_LENGTH = 10_000
_ISSUE_KEY = re.compile(r"^[A-Z][A-Z0-9_]+-[1-9][0-9]*$")

# (status object from the payload) -> (internal status or None, name in jira.yaml's language)
StatusResolver = Callable[[dict[str, Any]], Awaitable[tuple[str | None, str]]]


def valid_signature(body: bytes, header: str | None, secret: str) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def slim_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep only what parsing needs: full issue payloads can be hundreds of KB."""
    issue = payload.get("issue") or {}
    fields = issue.get("fields") or {}
    return {
        "timestamp": payload.get("timestamp"),
        "webhookEvent": payload.get("webhookEvent"),
        "user": payload.get("user"),
        "issue": {
            "key": issue.get("key"),
            "fields": {
                name: fields.get(name) for name in ("project", "status", "priority", "assignee")
            },
        },
        "comment": payload.get("comment"),
    }


def actor_id(payload: dict[str, Any]) -> str | None:
    comment = payload.get("comment") or {}
    person = (comment.get("author") if comment else None) or payload.get("user") or {}
    return person.get("accountId")


async def parse_jira_webhook(
    payload: dict[str, Any], config: JiraConfig, resolve_status: StatusResolver
) -> TicketEvent | None:
    kind = SUPPORTED_EVENTS.get(str(payload.get("webhookEvent")))
    issue = payload.get("issue") or {}
    key = str(issue.get("key") or "")
    if kind is None or not _ISSUE_KEY.fullmatch(key):
        return None
    fields = issue.get("fields") or {}
    project = (fields.get("project") or {}).get("key") or key.rsplit("-", 1)[0]
    if project != config.project_key:
        return None

    comment_data = payload.get("comment") or {}
    person = (comment_data.get("author") if kind == "comment_created" else None) or (
        payload.get("user") or {}
    )
    who = person.get("accountId")
    event: dict[str, Any] = {
        "kind": kind,
        "key": key,
        "occurred_at": _from_millis(payload.get("timestamp")),
        "actor_id": who,
        "actor_name": person.get("displayName"),
        "from_service_account": who == config.service_account_id,
    }
    if kind == "issue_updated":
        status_obj = fields.get("status") or {}
        if not status_obj:
            return None
        internal, name = await resolve_status(status_obj)
        priority = fields.get("priority") or {}
        assignee = fields.get("assignee") or {}
        event["state"] = TicketState(
            external_status=name,
            status=internal,
            priority=priority.get("name"),
            assignee_id=assignee.get("accountId"),
            assignee_name=assignee.get("displayName"),
        )
    else:
        if not comment_data.get("id"):
            return None
        body = comment_data.get("body")
        # Webhooks use the v2 representation (a wiki-markup string); accept ADF too.
        text = adf.to_plain_text(body) if isinstance(body, dict) else str(body or "").strip()
        event["comment"] = TicketComment(
            id=str(comment_data["id"]),
            author_id=who,
            author_name=person.get("displayName"),
            text=adf.clean_text(text)[:COMMENT_MAX_LENGTH],
            created_at=_parse_time(comment_data.get("created")) or event["occurred_at"],
        )
    return TicketEvent.model_validate(event)


def _from_millis(value: object) -> datetime:
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    return datetime.now(UTC)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None

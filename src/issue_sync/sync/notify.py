"""Slack notification for a newly created ticket, sent once per record.

`ticket_sync.notified_at` is the durable "sent" flag. The row is locked while sending,
so concurrent jobs cannot both post; a crash after Slack accepted the message but before
the commit can repeat it once (at-least-once, never silently lost).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from issue_sync.adapter_config import AssignmentConfig, SlackConfig
from issue_sync.db.models import DocumentRecord, TicketSync
from issue_sync.notify.message import TicketNotice, build_message
from issue_sync.notify.slack import SlackClient
from issue_sync.rules import decide
from issue_sync.sync.inbound import detail_url
from issue_sync.sync.push import latest_triage, request_facts

logger = logging.getLogger(__name__)

NotifyOutcome = Literal["sent", "already_sent", "no_ticket", "missing"]


@dataclass(frozen=True)
class Notifier:
    slack: SlackClient
    slack_config: SlackConfig
    rules: AssignmentConfig
    ticket_url: Callable[[str], str]
    public_base_url: str


async def notify_record(
    sessionmaker: async_sessionmaker[AsyncSession], notifier: Notifier, record_id: int
) -> NotifyOutcome:
    """Raises SlackError on failure; the caller decides whether to retry."""
    async with sessionmaker() as session, session.begin():
        sync = await session.scalar(
            select(TicketSync).where(TicketSync.document_record_id == record_id).with_for_update()
        )
        record = await session.get(DocumentRecord, record_id)
        if sync is None or record is None:
            return "missing"
        if sync.notified_at is not None:
            return "already_sent"
        if sync.issue_key is None:
            return "no_ticket"

        triage = await latest_triage(session, record_id)
        # Same rules as at creation, so the message names the team the ticket went to.
        assignment = decide(notifier.rules, triage, request_facts(record))
        role = assignment.assignee_role
        text, blocks = build_message(
            TicketNotice(
                issue_key=sync.issue_key,
                issue_url=notifier.ticket_url(sync.issue_key),
                detail_url=detail_url(notifier.public_base_url, record.system_id),
                title=record.title,
                reporter=record.reporter,
                urgency=assignment.urgency,
                assignee_role=role,
                slack_user=notifier.slack_config.users.get(role) if role else None,
                ai_summary=triage.summary if triage else None,
                rule_note=assignment.explain(triage),
            )
        )
        await notifier.slack.post_message(notifier.slack_config.default_channel, text, blocks)
        sync.notified_at = datetime.now(UTC)
        logger.info("notified Slack about %s (record %s)", sync.issue_key, record_id)
        return "sent"

"""Ticket system -> database: apply one webhook event.

Loop prevention, in order:
1. Events made by this system's service account are ignored.
2. Field events no newer than `ticket_sync.ticket_updated_at` are dropped (late or
   repeated deliveries). Each event carries the ticket's full state, so dropping an older
   one never loses a change: the newer event already contained it.
3. Values equal to the database are skipped, so no history event is written.
Comments are not ordered by time; they are de-duplicated by the Jira comment id.
"""

import logging
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from issue_sync.db.models import (
    DocumentRecord,
    DocumentRecordHistory,
    DocumentRecordStatus,
    TicketSync,
)
from issue_sync.tickets.base import (
    TicketAttachment,
    TicketComment,
    TicketEvent,
    TicketSnapshot,
    TicketState,
)

logger = logging.getLogger(__name__)

Outcome = Literal["updated", "unchanged", "stale", "own_change", "unknown_ticket", "duplicate"]


@dataclass(frozen=True)
class ApplyResult:
    outcome: Outcome
    document_id: int | None = None
    # True when something shown in the sheet (status, latest note) changed.
    sheet_changed: bool = False


async def apply_ticket_event(
    sessionmaker: async_sessionmaker[AsyncSession], event: TicketEvent
) -> ApplyResult:
    if event.from_service_account:
        return ApplyResult("own_change")
    async with sessionmaker() as session, session.begin():
        sync = await session.scalar(
            select(TicketSync).where(TicketSync.issue_key == event.key).with_for_update()
        )
        if sync is None:
            return ApplyResult("unknown_ticket")
        record = await session.scalar(
            select(DocumentRecord)
            .where(DocumentRecord.id == sync.document_record_id)
            .with_for_update()
        )
        assert record is not None  # ticket_sync.document_record_id is a foreign key

        if event.comment is not None:
            return await _add_comment(session, record, event)
        assert event.state is not None
        # Attachments are keyed by id, not ordered by time: even a stale event may name
        # one that was missed, and a repeated one is skipped.
        added = await add_attachments(session, record, event.attachments, service_account_id=None)
        if sync.ticket_updated_at is not None and event.occurred_at <= sync.ticket_updated_at:
            return ApplyResult("updated" if added else "stale", record.document_id)
        sync.ticket_updated_at = event.occurred_at
        before = (record.current_priority, record.current_assignee)
        status_changed = await apply_state(
            session, record, event.state, actor=event.actor_name or event.actor_id
        )
        changed = (
            status_changed
            or added > 0
            or before != (record.current_priority, record.current_assignee)
        )
        return ApplyResult(
            "updated" if changed else "unchanged",
            record.document_id,
            sheet_changed=status_changed,
        )


async def apply_state(
    session: AsyncSession, record: DocumentRecord, state: TicketState, *, actor: str | None
) -> bool:
    """Bring the record's ticket-owned fields in line with `state`.

    Returns True if the status changed (which writes a `status_changed` event).
    Priority and assignee are snapshots only: no event type exists for them.
    """
    record.current_priority = state.priority
    record.current_assignee = state.assignee_name
    record.current_assignee_id = state.assignee_id
    if state.status is None:
        logger.warning(
            "record %s: Jira status %r has no mapping in jira.yaml; status left unchanged",
            record.id,
            state.external_status,
        )
        return False
    status_id = await session.scalar(
        select(DocumentRecordStatus.id).where(DocumentRecordStatus.status_name == state.status)
    )
    if status_id is None or status_id == record.status_id:
        return False
    session.add(
        DocumentRecordHistory(
            document_record_id=record.id,
            event_type="status_changed",
            from_status_id=record.status_id,
            to_status_id=status_id,
            created_by=actor or "jira",
            detail={"jira_status": state.external_status},
        )
    )
    record.status_id = status_id
    return True


async def apply_snapshot(
    session: AsyncSession, record: DocumentRecord, sync: TicketSync, snapshot: TicketSnapshot
) -> bool:
    """Baseline right after creation (or from reconciliation): same rules as events."""
    if sync.ticket_updated_at is None or snapshot.updated_at > sync.ticket_updated_at:
        sync.ticket_updated_at = snapshot.updated_at
    state = TicketState(
        external_status=snapshot.external_status,
        status=snapshot.status,
        priority=snapshot.priority,
        assignee_id=snapshot.assignee_id,
        assignee_name=snapshot.assignee_name,
    )
    # The first status is not a change anyone made: record it without an event.
    if record.status_id is None and state.status is not None:
        record.status_id = await session.scalar(
            select(DocumentRecordStatus.id).where(DocumentRecordStatus.status_name == state.status)
        )
        record.current_priority = state.priority
        record.current_assignee = state.assignee_name
        record.current_assignee_id = state.assignee_id
        return True
    return await apply_state(session, record, state, actor="jira")


async def _add_comment(
    session: AsyncSession, record: DocumentRecord, event: TicketEvent
) -> ApplyResult:
    assert event.comment is not None
    if not await add_comment(session, record, event.comment):
        return ApplyResult("duplicate", record.document_id)
    return ApplyResult("updated", record.document_id, sheet_changed=True)


async def recorded_attachment_ids(session: AsyncSession, record_id: int) -> set[str]:
    rows = await session.scalars(
        select(DocumentRecordHistory.detail["jira_attachment_id"].astext).where(
            DocumentRecordHistory.document_record_id == record_id,
            DocumentRecordHistory.event_type == "attachment",
        )
    )
    return {r for r in rows if r}


async def add_attachments(
    session: AsyncSession,
    record: DocumentRecord,
    attachments: list[TicketAttachment],
    *,
    service_account_id: str | None,
) -> int:
    """Log each ticket attachment once (keyed by its Jira id). Returns how many were new.

    Files added by `service_account_id` are skipped: this system does not upload files
    today, and if it ever does, they must not echo back as events.
    """
    if not attachments:
        return 0
    known = await recorded_attachment_ids(session, record.id)
    added = 0
    for item in sorted(attachments, key=lambda a: a.created_at):
        if item.id in known or (service_account_id and item.author_id == service_account_id):
            continue
        known.add(item.id)
        session.add(
            DocumentRecordHistory(
                document_record_id=record.id,
                event_type="attachment",
                attachments=[
                    {
                        "name": item.filename,
                        "url": item.url,
                        "source": "jira",
                        "size": item.size,
                        "mime_type": item.mime_type,
                    }
                ],
                created_by=item.author_name or item.author_id or "jira",
                detail={"jira_attachment_id": item.id},
                create_time=item.created_at,  # when it was added in Jira
            )
        )
        added += 1
    return added


async def recorded_comment_ids(session: AsyncSession, record_id: int) -> set[str]:
    rows = await session.scalars(
        select(DocumentRecordHistory.detail["jira_comment_id"].astext).where(
            DocumentRecordHistory.document_record_id == record_id,
            DocumentRecordHistory.event_type == "comment",
        )
    )
    return {r for r in rows if r}


async def add_comment(
    session: AsyncSession,
    record: DocumentRecord,
    comment: TicketComment,
    known_ids: set[str] | None = None,
) -> bool:
    """Log a ticket comment once (keyed by its Jira id). Returns False if already logged."""
    known = known_ids if known_ids is not None else await recorded_comment_ids(session, record.id)
    if comment.id in known:
        return False
    known.add(comment.id)
    session.add(
        DocumentRecordHistory(
            document_record_id=record.id,
            event_type="comment",
            comment=comment.text,
            created_by=comment.author_name or comment.author_id or "jira",
            detail={"jira_comment_id": comment.id},
            create_time=comment.created_at,  # when it was written in Jira
        )
    )
    return True

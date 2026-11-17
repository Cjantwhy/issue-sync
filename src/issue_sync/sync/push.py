"""Outbound flow: database -> ticket system, one record per call.

The record's `ticket_sync` row is locked with `SELECT ... FOR UPDATE` for the whole push,
including the HTTP calls, so two pushes of the same record never overlap. That is what
makes "never open two tickets for one row" hold under concurrent jobs.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from issue_sync.db.models import DocumentRecord, TicketSync
from issue_sync.sources.sheets.layout import content_hash
from issue_sync.sync.inbound import detail_url
from issue_sync.tickets.base import TicketBackend, TicketBackendError, TicketContent, TicketDraft

logger = logging.getLogger(__name__)

LAST_ERROR_MAX_LENGTH = 2000

Outcome = Literal["created", "linked", "updated", "unchanged", "failed", "missing"]


@dataclass(frozen=True)
class PushResult:
    outcome: Outcome
    issue_key: str | None = None
    error: Exception | None = None

    @property
    def retryable(self) -> bool:
        return isinstance(self.error, TicketBackendError) and self.error.retryable

    @property
    def retry_after(self) -> float | None:
        return self.error.retry_after if isinstance(self.error, TicketBackendError) else None


def ticket_content(record: DocumentRecord, public_base_url: str) -> TicketContent:
    return TicketContent(
        title=record.title,
        description=record.description,
        reporter=record.reporter,
        contact=record.contact,
        detail_url=detail_url(public_base_url, record.system_id),
    )


async def push_record(
    sessionmaker: async_sessionmaker[AsyncSession],
    backend: TicketBackend,
    record_id: int,
    public_base_url: str,
) -> PushResult:
    async with sessionmaker() as session, session.begin():
        sync = await _locked_ticket_sync(session, record_id)
        record = await session.get(DocumentRecord, record_id)
        if record is None or sync is None:
            logger.warning("push skipped: record %s does not exist", record_id)
            return PushResult("missing")

        content = ticket_content(record, public_base_url)
        pushed_hash = content_hash(content.model_dump(mode="json"))
        if sync.issue_key and sync.pushed_hash == pushed_hash:
            # e.g. an edit that was reverted before the push ran
            sync.push_status = "success"
            return PushResult("unchanged", sync.issue_key)

        outcome: Outcome
        try:
            if sync.issue_key is None:
                # Always look first: an earlier attempt may have created the ticket and
                # then timed out or crashed before the key was stored.
                ref = await backend.find_by_system_id(record.system_id)
                if ref is None:
                    draft = TicketDraft(system_id=record.system_id, **content.model_dump())
                    ref = await backend.create_ticket(draft)
                    outcome = "created"
                else:
                    await backend.update_content(ref.key, content)
                    outcome = "linked"
                sync.issue_key = ref.key
            else:
                await backend.update_content(sync.issue_key, content)
                outcome = "updated"
        except (TicketBackendError, ValueError) as exc:
            sync.push_status = "failed"
            sync.last_error = f"{type(exc).__name__}: {exc}"[:LAST_ERROR_MAX_LENGTH]
            sync.retry_count += 1
            logger.warning(
                "push of record %s failed (attempt %d): %s", record_id, sync.retry_count, exc
            )
            return PushResult("failed", sync.issue_key, exc)

        sync.push_status = "success"
        sync.pushed_hash = pushed_hash
        sync.pushed_at = datetime.now(UTC)
        sync.last_error = None
        sync.retry_count = 0
        logger.info("record %s %s ticket %s", record_id, outcome, sync.issue_key)
        return PushResult(outcome, sync.issue_key)


async def _locked_ticket_sync(session: AsyncSession, record_id: int) -> TicketSync | None:
    query = select(TicketSync).where(TicketSync.document_record_id == record_id).with_for_update()
    sync = await session.scalar(query)
    if sync is None and await session.get(DocumentRecord, record_id) is not None:
        # Records are created with their ticket_sync row; this only repairs a gap.
        await session.execute(
            pg_insert(TicketSync)
            .values(document_record_id=record_id)
            .on_conflict_do_nothing(index_elements=["document_record_id"])
        )
        sync = await session.scalar(query)
    return sync

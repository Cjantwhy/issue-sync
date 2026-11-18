"""Scheduled reconciliation: repair what event-driven sync missed or could not do.

1. Tickets: pull state and comments from the ticket system for every synced record and
   apply what webhooks missed (same rules as webhook events). Restore titles and
   descriptions that were edited in the ticket system, since the sheet owns them.
2. Sheets: mark records whose row disappeared as source-deleted (the ticket is left
   alone), un-mark rows that came back, and report duplicate row identities.
3. Health: re-queue failed or stuck pushes and report what needs an operator.

It only records and reports; the caller enqueues the follow-up jobs and sends alerts.
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from issue_sync.db.models import Document, DocumentRecord, DocumentRecordHistory, TicketSync
from issue_sync.notify.alerts import Alert
from issue_sync.sources.sheets.errors import SheetLayoutError, SheetsError
from issue_sync.sources.sheets.layout import DocumentMapping, parse_sheet
from issue_sync.sync.inbound import SheetsSource, record_links
from issue_sync.sync.push import latest_triage, ticket_content
from issue_sync.sync.ticket_events import (
    add_attachments,
    add_comment,
    apply_snapshot,
    apply_state,
    recorded_comment_ids,
)
from issue_sync.tickets.base import TicketBackend, TicketBackendError, TicketReport, TicketState

logger = logging.getLogger(__name__)

ACTOR = "reconciliation"
# Refuse to mark more rows deleted than this share of a sheet's records in one run
# (once more than MASS_DELETION_MIN_ROWS rows are affected): it is far more likely that
# the wrong worksheet was read or rows were filtered than that half the requests went.
MASS_DELETION_SHARE = 0.5
MASS_DELETION_MIN_ROWS = 5


@dataclass
class ReconcileReport:
    tickets_checked: int = 0
    tickets_updated: int = 0
    comments_added: int = 0
    attachments_added: int = 0
    content_restored: int = 0
    rows_deleted: int = 0
    rows_restored: int = 0
    pushes_requeued: int = 0
    documents_to_sync: set[int] = field(default_factory=set)
    records_to_push: list[int] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        return {
            "tickets_checked": self.tickets_checked,
            "tickets_updated": self.tickets_updated,
            "comments_added": self.comments_added,
            "attachments_added": self.attachments_added,
            "content_restored": self.content_restored,
            "rows_deleted": self.rows_deleted,
            "rows_restored": self.rows_restored,
            "pushes_requeued": self.pushes_requeued,
            "alerts": len(self.alerts),
        }


class Reconciler:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        backend: TicketBackend,
        sheets: SheetsSource,
        *,
        public_base_url: str,
        service_account_id: str,
        retry_limit: int,
        stale_after: timedelta = timedelta(minutes=30),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessionmaker = sessionmaker
        self._backend = backend
        self._sheets = sheets
        self._public_base_url = public_base_url
        self._service_account_id = service_account_id
        self._retry_limit = retry_limit
        self._stale_after = stale_after
        self._clock = clock

    async def run(self) -> ReconcileReport:
        report = ReconcileReport()
        await self._reconcile_tickets(report)
        await self._reconcile_sheets(report)
        await self._check_health(report)
        logger.info("reconciliation: %s", report.summary())
        return report

    # ---------- 1. tickets ----------

    async def _reconcile_tickets(self, report: ReconcileReport) -> None:
        async with self._sessionmaker() as session:
            rows = (
                await session.execute(
                    select(DocumentRecord.id, DocumentRecord.system_id, TicketSync.issue_key)
                    .join(TicketSync, TicketSync.document_record_id == DocumentRecord.id)
                    .where(TicketSync.issue_key.is_not(None))
                    .order_by(DocumentRecord.id)
                )
            ).all()
        if not rows:
            return
        try:
            tickets = {
                t.system_id: t for t in await self._backend.find_tickets([r[1] for r in rows])
            }
        except TicketBackendError as exc:
            report.alerts.append(
                Alert(
                    "ticket_system_unreachable",
                    "all",
                    f"Reconciliation could not read tickets: {exc}",
                )
            )
            return
        for record_id, system_id, issue_key in rows:
            ticket = tickets.get(system_id)
            if ticket is None or ticket.key != issue_key:
                report.alerts.append(
                    Alert(
                        "ticket_missing",
                        f"record:{record_id}",
                        f"{issue_key} (record {record_id}) no longer exists in the ticket "
                        "system, or lost its issue-sync label",
                    )
                )
                continue
            report.tickets_checked += 1
            await self._reconcile_ticket(record_id, ticket, report)

    async def _reconcile_ticket(
        self, record_id: int, ticket: TicketReport, report: ReconcileReport
    ) -> None:
        async with self._sessionmaker() as session, session.begin():
            # Same lock order as webhook events and pushes: ticket_sync, then the record.
            sync = await session.scalar(
                select(TicketSync)
                .where(TicketSync.document_record_id == record_id)
                .with_for_update()
            )
            record = await session.scalar(
                select(DocumentRecord).where(DocumentRecord.id == record_id).with_for_update()
            )
            assert sync is not None and record is not None
            sheet_changed = False

            if sync.ticket_updated_at is None or ticket.updated_at > sync.ticket_updated_at:
                before = (record.status_id, record.current_priority, record.current_assignee)
                if record.status_id is None:
                    sheet_changed |= await apply_snapshot(session, record, sync, ticket)
                else:
                    sheet_changed |= await apply_state(session, record, _state(ticket), actor=ACTOR)
                    sync.ticket_updated_at = ticket.updated_at
                if before != (record.status_id, record.current_priority, record.current_assignee):
                    report.tickets_updated += 1
            elif (
                record.current_assignee_id is None
                and ticket.assignee_id is not None
                and record.current_assignee == ticket.assignee_name
            ):
                # Records from before assignee ids were kept: fill the id in without
                # waiting for the ticket to change. Same person only, so no state moves.
                record.current_assignee_id = ticket.assignee_id

            known = await recorded_comment_ids(session, record.id)
            for comment in sorted(ticket.comments, key=lambda c: c.created_at):
                if comment.author_id == self._service_account_id:
                    continue
                if await add_comment(session, record, comment, known):
                    report.comments_added += 1
                    sheet_changed = True
            report.attachments_added += await add_attachments(
                session, record, ticket.attachments, service_account_id=self._service_account_id
            )

            if sync.push_status == "success":
                content = ticket_content(
                    record,
                    self._public_base_url,
                    await latest_triage(session, record.id),
                    await record_links(session, record),
                )
                if not self._backend.content_matches(content, ticket):
                    logger.warning(
                        "%s: title or description was edited in the ticket system; "
                        "restoring the sheet's version",
                        ticket.key,
                    )
                    sync.push_status = "pending"
                    sync.pushed_hash = None
                    report.content_restored += 1
                    report.records_to_push.append(record.id)

            if sheet_changed:
                report.documents_to_sync.add(record.document_id)

    # ---------- 2. sheets ----------

    async def _reconcile_sheets(self, report: ReconcileReport) -> None:
        async with self._sessionmaker() as session:
            documents = list(
                await session.scalars(
                    select(Document.id).where(Document.sync_enabled).order_by(Document.id)
                )
            )
        for document_id in documents:
            try:
                await self._reconcile_document(document_id, report)
            except (SheetsError, SheetLayoutError, ValidationError) as exc:
                report.alerts.append(
                    Alert(
                        "sheet_unreadable",
                        f"document:{document_id}",
                        f"Document {document_id} could not be checked: {exc}",
                    )
                )

    async def _reconcile_document(self, document_id: int, report: ReconcileReport) -> None:
        # Holds the same lock as a sync run, so rows cannot be assigned ids meanwhile.
        async with self._sessionmaker() as lock_session, lock_session.begin():
            document = await lock_session.scalar(
                select(Document).where(Document.id == document_id).with_for_update(key_share=True)
            )
            if document is None:
                return
            mapping = DocumentMapping.model_validate(document.mapping)
            layout = parse_sheet(
                await self._sheets.read_values(document.spreadsheet_id, document.worksheet_name),
                mapping,
            )
            rows_by_id: dict[uuid.UUID, list[int]] = {}
            for row in layout.rows:
                try:
                    system_id = row.parsed_system_id()
                except ValueError:
                    continue
                if system_id is not None:
                    rows_by_id.setdefault(system_id, []).append(row.row_number)

            await self._report_duplicates(document, rows_by_id, report)
            await self._mark_deleted_rows(document, set(rows_by_id), report)

    async def _report_duplicates(
        self, document: Document, rows_by_id: dict[uuid.UUID, list[int]], report: ReconcileReport
    ) -> None:
        for system_id, rows in rows_by_id.items():
            if len(rows) > 1:
                report.alerts.append(
                    Alert(
                        "duplicate_identity",
                        f"{document.id}:{system_id}",
                        f"System ID {system_id} appears in rows "
                        f"{', '.join(map(str, rows))} of '{document.name}'; only row "
                        f"{rows[0]} is synced. Clear the System ID of copied rows.",
                    )
                )
        async with self._sessionmaker() as session:
            elsewhere = (
                await session.execute(
                    select(DocumentRecord.system_id, Document.name)
                    .join(Document, Document.id == DocumentRecord.document_id)
                    .where(DocumentRecord.system_id.in_(list(rows_by_id)))
                    .where(DocumentRecord.document_id != document.id)
                )
            ).all()
        for system_id, other_name in elsewhere:
            report.alerts.append(
                Alert(
                    "duplicate_identity",
                    f"{document.id}:{system_id}",
                    f"Row {rows_by_id[system_id][0]} of '{document.name}' carries System ID "
                    f"{system_id}, which belongs to '{other_name}'; the row is not synced.",
                )
            )

    async def _mark_deleted_rows(
        self, document: Document, present: set[uuid.UUID], report: ReconcileReport
    ) -> None:
        async with self._sessionmaker() as session, session.begin():
            records = list(
                await session.scalars(
                    select(DocumentRecord)
                    .where(DocumentRecord.document_id == document.id)
                    .order_by(DocumentRecord.id)
                    .with_for_update()
                )
            )
            active = [r for r in records if r.source_deleted_at is None]
            missing = [r for r in active if r.system_id not in present]
            returned = [
                r for r in records if r.source_deleted_at is not None and r.system_id in present
            ]

            if (
                len(missing) > MASS_DELETION_MIN_ROWS
                and len(missing) > len(active) * MASS_DELETION_SHARE
            ):
                report.alerts.append(
                    Alert(
                        "mass_deletion",
                        f"document:{document.id}",
                        f"{len(missing)} of {len(active)} rows of '{document.name}' are missing "
                        "from the sheet. Nothing was marked deleted; check that the right "
                        "worksheet is registered and no filter or broken header hides rows.",
                    )
                )
                missing = []

            now = self._clock()
            for record in missing:
                record.source_deleted_at = now
                session.add(
                    DocumentRecordHistory(
                        document_record_id=record.id,
                        event_type="source_deleted",
                        created_by=ACTOR,
                        detail={"reason": "row not found in the sheet"},
                    )
                )
            for record in returned:
                record.source_deleted_at = None
                session.add(
                    DocumentRecordHistory(
                        document_record_id=record.id,
                        event_type="source_modified",
                        created_by=ACTOR,
                        detail={"restored": True},
                    )
                )
            report.rows_deleted += len(missing)
            report.rows_restored += len(returned)

    # ---------- 3. health ----------

    async def _check_health(self, report: ReconcileReport) -> None:
        cutoff = self._clock() - self._stale_after
        async with self._sessionmaker() as session:
            stuck = (
                await session.execute(
                    select(
                        TicketSync.document_record_id,
                        TicketSync.push_status,
                        TicketSync.retry_count,
                        TicketSync.last_error,
                    )
                    .where(TicketSync.push_status != "success")
                    .where(TicketSync.update_time < cutoff)
                    .order_by(TicketSync.document_record_id)
                )
            ).all()
            unnotified = list(
                await session.scalars(
                    select(TicketSync.issue_key)
                    .where(TicketSync.issue_key.is_not(None), TicketSync.notified_at.is_(None))
                    .where(TicketSync.pushed_at < cutoff)
                )
            )
        for record_id, status, retry_count, last_error in stuck:
            if status == "failed" and retry_count >= self._retry_limit:
                report.alerts.append(
                    Alert(
                        "push_failed",
                        f"record:{record_id}",
                        f"Record {record_id} could not be pushed after {retry_count} attempts "
                        f"and is no longer retried automatically: {last_error}",
                    )
                )
                continue
            report.records_to_push.append(record_id)
            report.pushes_requeued += 1
            if status == "pending":
                report.alerts.append(
                    Alert(
                        "push_pending",
                        f"record:{record_id}",
                        f"Record {record_id} has waited over "
                        f"{int(self._stale_after.total_seconds() // 60)} minutes for its "
                        "ticket push; re-queued. Is the worker running?",
                    )
                )
        for issue_key in unnotified:
            report.alerts.append(
                Alert(
                    "notification_missing",
                    f"ticket:{issue_key}",
                    f"The Slack notification for {issue_key} has not been sent; check "
                    "SLACK_BOT_TOKEN and that the bot is in the channel.",
                )
            )


def _state(ticket: TicketReport) -> TicketState:
    return TicketState(
        external_status=ticket.external_status,
        status=ticket.status,
        priority=ticket.priority,
        assignee_id=ticket.assignee_id,
        assignee_name=ticket.assignee_name,
    )

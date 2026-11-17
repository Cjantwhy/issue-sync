"""Inbound flow: worksheet -> database.

One run of `sync_document` reads the whole worksheet and, for each row:
identity (system_id) -> change detection (raw_hash) -> record + history event.
It returns the records that need a ticket push; the caller enqueues those jobs.

Concurrency: a run holds a row lock on its `document` for its whole duration, so runs
for the same worksheet (an Apps Script push racing the poll) are serialized. Each row is
then written in its own short transaction under `SELECT ... FOR UPDATE` on the record.
"""

import logging
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from issue_sync.db.models import (
    Document,
    DocumentRecord,
    DocumentRecordHistory,
    DocumentRecordStatus,
    TicketSync,
)
from issue_sync.sources.sheets.client import CellWrite
from issue_sync.sources.sheets.errors import SheetLayoutError
from issue_sync.sources.sheets.layout import (
    SHEET_FIELDS,
    DocumentMapping,
    SheetLayout,
    SheetRow,
    content_hash,
    parse_sheet,
)

logger = logging.getLogger(__name__)

CREATED_BY_SHEET = "sheet"
# Column sizes in db/schema.sql; longer values are cut (the full row stays in `raw`).
FIELD_LIMITS = {"title": 255, "reporter": 255, "contact": 255}


class SheetsSource(Protocol):
    def read_values(self, spreadsheet_id: str, worksheet: str) -> Awaitable[list[list[str]]]: ...

    def write_cells(
        self, spreadsheet_id: str, worksheet: str, cells: list[CellWrite]
    ) -> Awaitable[None]: ...


@dataclass
class SyncReport:
    created: int = 0
    modified: int = 0
    unchanged: int = 0
    skipped: int = 0
    ids_assigned: int = 0
    cells_written: int = 0
    # Records whose ticket_sync is pending: the caller enqueues a push for each.
    pending_record_ids: list[int] = field(default_factory=list)
    # Records with a ticket but no Slack notification yet: the caller enqueues one each.
    unnotified_record_ids: list[int] = field(default_factory=list)


def detail_url(public_base_url: str, system_id: uuid.UUID) -> str:
    return f"{public_base_url.rstrip('/')}/r/{system_id}"


NOTE_MAX_LENGTH = 500


def status_label(status_name: str | None) -> str:
    """Internal status as shown in the sheet: in_progress -> "In progress"."""
    return status_name.replace("_", " ").capitalize() if status_name else ""


def latest_note(text: str | None, author: str, created: datetime) -> str:
    """The newest ticket comment, on one line: "2026-11-17 Olga: Fixed in 1.4"."""
    body = " ".join((text or "").split())
    note = f"{created.astimezone(UTC):%Y-%m-%d} {author}: {body}"
    return note if len(note) <= NOTE_MAX_LENGTH else note[: NOTE_MAX_LENGTH - 1] + "…"


def record_values(row: SheetRow) -> dict[str, str | None]:
    values = {}
    for name in SHEET_FIELDS:
        value = row.fields[name]
        limit = FIELD_LIMITS.get(name)
        values[name] = value[:limit] if value and limit else value
    return values


class InboundSync:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        sheets: SheetsSource,
        public_base_url: str,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._sheets = sheets
        self._public_base_url = public_base_url
        self._last_unmapped: dict[int, list[str]] = {}

    @property
    def sheets(self) -> SheetsSource:
        return self._sheets

    async def sync_document(self, document_id: int) -> SyncReport | None:
        """Returns None if the document does not exist or has sync disabled."""
        async with self._sessionmaker() as lock_session, lock_session.begin():
            # FOR NO KEY UPDATE, not FOR UPDATE: inserting records takes a KEY SHARE lock
            # on this row (foreign key check), which FOR UPDATE would block.
            document = await lock_session.scalar(
                select(Document).where(Document.id == document_id).with_for_update(key_share=True)
            )
            if document is None:
                logger.warning("document %s does not exist", document_id)
                return None
            if not document.sync_enabled:
                logger.info("document %s has sync disabled; skipped", document_id)
                return None
            try:
                mapping = DocumentMapping.model_validate(document.mapping)
            except ValidationError as exc:
                raise SheetLayoutError(
                    f"document {document_id} has an invalid mapping: {exc}"
                ) from None
            report = await self._sync_locked(document, mapping)
        logger.info(
            "synced document %s: %d created, %d modified, %d unchanged, %d skipped, "
            "%d ids assigned, %d cells written",
            document_id,
            report.created,
            report.modified,
            report.unchanged,
            report.skipped,
            report.ids_assigned,
            report.cells_written,
        )
        return report

    async def _sync_locked(self, document: Document, mapping: DocumentMapping) -> SyncReport:
        report = SyncReport()
        values = await self._sheets.read_values(document.spreadsheet_id, document.worksheet_name)
        layout = parse_sheet(values, mapping)
        self._warn_layout(document, layout)

        identified: dict[uuid.UUID, SheetRow] = {}
        new_rows: list[SheetRow] = []
        for row in layout.rows:
            try:
                system_id = row.parsed_system_id()
            except ValueError:
                logger.warning(
                    "document %s row %d: system_id %r is not a UUID; row skipped",
                    document.id,
                    row.row_number,
                    row.system_id_cell,
                )
                report.skipped += 1
                continue
            if system_id is None:
                if row.has_content:
                    new_rows.append(row)
            elif system_id in identified:
                logger.warning(
                    "document %s: system_id %s appears in rows %d and %d; row %d skipped",
                    document.id,
                    system_id,
                    identified[system_id].row_number,
                    row.row_number,
                    row.row_number,
                )
                report.skipped += 1
            else:
                identified[system_id] = row

        # Identity first: the UUID is in the sheet before any record exists, so a failure
        # later in the run cannot make the next run invent a second UUID for the row.
        written: dict[uuid.UUID, dict[str, str]] = {}
        if new_rows:
            assigned = await self._assign_ids(document, mapping, layout, new_rows)
            report.ids_assigned = len(assigned)
            report.cells_written += 2 * len(assigned)
            for system_id, row in assigned.items():
                identified[system_id] = row
                written[system_id] = self._system_cells(system_id)

        for system_id, row in identified.items():
            await self._upsert(document, system_id, row, report)

        fixed, count = await self._fix_system_cells(document, layout, identified, written)
        report.cells_written += count
        written.update(fixed)
        await self._mark_written(written)
        report.pending_record_ids = await self._pending_records(document.id)
        report.unnotified_record_ids = await self._unnotified_records(document.id)
        return report

    # ---------- identity ----------

    async def _assign_ids(
        self,
        document: Document,
        mapping: DocumentMapping,
        layout: SheetLayout,
        new_rows: list[SheetRow],
    ) -> dict[uuid.UUID, SheetRow]:
        """Write fresh UUIDs (and detail links) into rows that have none.

        The sheet is re-read just before writing, and a row is only claimed if it is
        still at the same position with the same content and an empty system_id cell.
        This guards against rows shifting (insert, delete, sort) since the first read.
        """
        fresh = parse_sheet(
            await self._sheets.read_values(document.spreadsheet_id, document.worksheet_name),
            mapping,
        )
        if fresh.columns != layout.columns:
            logger.warning("document %s: columns moved during sync; ids deferred", document.id)
            return {}
        current = {row.row_number: row for row in fresh.rows}
        assigned: dict[uuid.UUID, SheetRow] = {}
        cells: list[CellWrite] = []
        for row in new_rows:
            now = current.get(row.row_number)
            if now is None or now.system_id_cell or now.raw_hash != row.raw_hash:
                logger.info(
                    "document %s row %d changed during sync; id deferred to the next run",
                    document.id,
                    row.row_number,
                )
                continue
            system_id = uuid.uuid4()
            assigned[system_id] = row
            cells += [
                CellWrite(row.row_number, layout.columns["system_id"], str(system_id)),
                CellWrite(
                    row.row_number,
                    layout.columns["detail_url"],
                    detail_url(self._public_base_url, system_id),
                ),
            ]
        await self._sheets.write_cells(document.spreadsheet_id, document.worksheet_name, cells)
        return assigned

    def _system_cells(self, system_id: uuid.UUID) -> dict[str, str]:
        return {
            "system_id": str(system_id),
            "detail_url": detail_url(self._public_base_url, system_id),
        }

    async def _fix_system_cells(
        self,
        document: Document,
        layout: SheetLayout,
        identified: dict[uuid.UUID, SheetRow],
        already_written: dict[uuid.UUID, dict[str, str]],
    ) -> tuple[dict[uuid.UUID, dict[str, str]], int]:
        """Make the system-owned columns match the database: detail link, status and
        latest note. Covers both new values from Jira and cells edited by hand.

        Unlike ids, these are not re-verified before writing: if rows shift meanwhile,
        a wrong cell is corrected by the next run, and no identity is touched.
        Returns the expected cells per written record and the number of cells written.
        """
        pending = [sid for sid in identified if sid not in already_written]
        expected_by_id = await self._expected_system_cells(document.id, layout, pending)
        cells, written = [], {}
        for system_id, expected in expected_by_id.items():
            row = identified[system_id]
            stale = [f for f, value in expected.items() if row.system_cells.get(f, "") != value]
            if stale:
                cells += [CellWrite(row.row_number, layout.columns[f], expected[f]) for f in stale]
                written[system_id] = expected
        await self._sheets.write_cells(document.spreadsheet_id, document.worksheet_name, cells)
        return written, len(cells)

    async def _expected_system_cells(
        self, document_id: int, layout: SheetLayout, system_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, dict[str, str]]:
        if not system_ids:
            return {}
        async with self._sessionmaker() as session:
            records = (
                await session.execute(
                    select(
                        DocumentRecord.id,
                        DocumentRecord.system_id,
                        DocumentRecordStatus.status_name,
                    )
                    .outerjoin(
                        DocumentRecordStatus, DocumentRecordStatus.id == DocumentRecord.status_id
                    )
                    .where(DocumentRecord.document_id == document_id)
                    .where(DocumentRecord.system_id.in_(system_ids))
                )
            ).all()
            notes: dict[int, str] = {}
            if "latest_note" in layout.columns and records:
                rows = await session.execute(
                    select(
                        DocumentRecordHistory.document_record_id,
                        DocumentRecordHistory.comment,
                        DocumentRecordHistory.created_by,
                        DocumentRecordHistory.create_time,
                    )
                    .where(DocumentRecordHistory.event_type == "comment")
                    .where(DocumentRecordHistory.document_record_id.in_([r.id for r in records]))
                    .order_by(
                        DocumentRecordHistory.document_record_id,
                        DocumentRecordHistory.create_time.desc(),
                        DocumentRecordHistory.id.desc(),
                    )
                    .ext(distinct_on(DocumentRecordHistory.document_record_id))
                )
                notes = {r[0]: latest_note(r[1], r[2], r[3]) for r in rows}
        expected: dict[uuid.UUID, dict[str, str]] = {}
        for record_id, system_id, status_name in records:
            cells = self._system_cells(system_id)
            if "status" in layout.columns:
                cells["status"] = status_label(status_name)
            if "latest_note" in layout.columns:
                cells["latest_note"] = notes.get(record_id, "")
            expected[system_id] = cells
        return expected

    async def _mark_written(self, written: dict[uuid.UUID, dict[str, str]]) -> None:
        if not written:
            return
        now = datetime.now(UTC)
        async with self._sessionmaker() as session, session.begin():
            for system_id, cells in written.items():
                await session.execute(
                    update(DocumentRecord)
                    .where(DocumentRecord.system_id == system_id)
                    .values(sheet_written_hash=content_hash(cells), sheet_written_at=now)
                )

    # ---------- records ----------

    async def _upsert(
        self, document: Document, system_id: uuid.UUID, row: SheetRow, report: SyncReport
    ) -> None:
        values = record_values(row)
        async with self._sessionmaker() as session, session.begin():
            record = await self._locked_record(session, system_id)
            if record is None:
                record_id = await session.scalar(
                    pg_insert(DocumentRecord)
                    .values(
                        document_id=document.id,
                        system_id=system_id,
                        raw=row.raw,
                        raw_hash=row.raw_hash,
                        **values,
                    )
                    .on_conflict_do_nothing(index_elements=["system_id"])
                    .returning(DocumentRecord.id)
                )
                if record_id is not None:
                    session.add_all(
                        [
                            DocumentRecordHistory(
                                document_record_id=record_id,
                                event_type="created",
                                created_by=CREATED_BY_SHEET,
                                detail={"row": row.row_number},
                            ),
                            TicketSync(document_record_id=record_id),
                        ]
                    )
                    report.created += 1
                    return
                # Inserted concurrently (only possible across documents): treat as existing.
                record = await self._locked_record(session, system_id)
                assert record is not None

            if record.document_id != document.id:
                logger.warning(
                    "system_id %s in document %s row %d belongs to document %s; row skipped",
                    system_id,
                    document.id,
                    row.row_number,
                    record.document_id,
                )
                report.skipped += 1
                return
            if record.raw_hash == row.raw_hash:
                report.unchanged += 1
                return

            changes = {
                name: {"from": getattr(record, name), "to": value}
                for name, value in values.items()
                if getattr(record, name) != value
            }
            record.raw = row.raw
            record.raw_hash = row.raw_hash
            for name, value in values.items():
                setattr(record, name, value)
            if not changes:  # only unmapped columns changed: keep raw current, nothing to push
                report.unchanged += 1
                return
            session.add(
                DocumentRecordHistory(
                    document_record_id=record.id,
                    event_type="source_modified",
                    created_by=CREATED_BY_SHEET,
                    detail={"row": row.row_number, "changes": changes},
                )
            )
            await session.execute(
                update(TicketSync)
                .where(TicketSync.document_record_id == record.id)
                .values(push_status="pending")
            )
            report.modified += 1

    @staticmethod
    async def _locked_record(session: AsyncSession, system_id: uuid.UUID) -> DocumentRecord | None:
        return await session.scalar(
            select(DocumentRecord).where(DocumentRecord.system_id == system_id).with_for_update()
        )

    async def _pending_records(self, document_id: int) -> list[int]:
        """Every record of the document still waiting for a push.

        Includes records from earlier runs whose push job was never enqueued (e.g. Redis
        was down right after the commit), so a lost enqueue heals on the next run.
        Pushing is idempotent, so enqueueing an already-queued record is harmless.
        """
        async with self._sessionmaker() as session:
            rows = await session.scalars(
                select(TicketSync.document_record_id)
                .join(DocumentRecord, DocumentRecord.id == TicketSync.document_record_id)
                .where(DocumentRecord.document_id == document_id)
                .where(TicketSync.push_status == "pending")
                .order_by(TicketSync.document_record_id)
            )
            return list(rows)

    async def _unnotified_records(self, document_id: int) -> list[int]:
        """Tickets whose Slack notification was never sent (e.g. its job was lost)."""
        async with self._sessionmaker() as session:
            rows = await session.scalars(
                select(TicketSync.document_record_id)
                .join(DocumentRecord, DocumentRecord.id == TicketSync.document_record_id)
                .where(DocumentRecord.document_id == document_id)
                .where(TicketSync.issue_key.is_not(None), TicketSync.notified_at.is_(None))
                .order_by(TicketSync.document_record_id)
            )
            return list(rows)

    def _warn_layout(self, document: Document, layout: SheetLayout) -> None:
        # Logged when the set changes, not on every poll.
        if layout.unmapped_headers != self._last_unmapped.get(document.id):
            self._last_unmapped[document.id] = layout.unmapped_headers
            if layout.unmapped_headers:
                logger.warning(
                    "document %s: columns not in the mapping are ignored: %s",
                    document.id,
                    ", ".join(layout.unmapped_headers),
                )
        if layout.missing_optional:
            logger.debug(
                "document %s: mapped optional headers not found: %s",
                document.id,
                ", ".join(layout.missing_optional),
            )

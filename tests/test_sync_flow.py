"""Sheet -> database -> ticket flow against a real PostgreSQL (see tests/db_fixtures.py).

The worksheet and the ticket system are in-memory fakes; the database is real, because
the guarantees under test (row locks, unique constraints, one ticket per row) live there.
"""

import asyncio
import copy
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from sqlalchemy import func, pool, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from issue_sync.adapter_config import AssignmentConfig, SlackConfig
from issue_sync.classify.base import Classification, ClassifierError, ClassifierInput, Judgement
from issue_sync.db.models import (
    AiJudgement,
    Document,
    DocumentRecord,
    DocumentRecordHistory,
    TicketSync,
)
from issue_sync.db.session import create_sessionmaker
from issue_sync.notify.slack import SlackError
from issue_sync.sources.sheets.client import CellWrite
from issue_sync.sources.sheets.errors import SheetLayoutError
from issue_sync.sync.inbound import InboundSync
from issue_sync.sync.notify import Notifier, notify_record
from issue_sync.sync.push import Triage, push_record
from issue_sync.sync.ticket_events import apply_ticket_event
from issue_sync.tickets.base import (
    TicketBackendUnavailable,
    TicketComment,
    TicketContent,
    TicketDraft,
    TicketEvent,
    TicketRef,
    TicketRequestRejected,
    TicketSnapshot,
    TicketState,
)
from tests.db_fixtures import alembic_config, integration, throwaway_database

pytestmark = integration

BASE_URL = "https://sync.example.com"
TICKET_CREATED_AT = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)
HEADER = ["Title", "Description", "Reporter", "Team", "System ID", "Detail link"]
MAPPING = {
    "title": "Title",
    "description": "Description",
    "reporter": "Reporter",
    "system_id": "System ID",
    "detail_url": "Detail link",
}
SID_COL, LINK_COL = 4, 5


# ---------- fakes ----------


class FakeSheet:
    """In-memory worksheet with the Sheets API's read/write behaviour."""

    def __init__(self, rows: list[list[str]]) -> None:
        self.grid = [list(r) for r in rows]
        self.reads = 0
        self.writes: list[list[CellWrite]] = []
        self.on_read: Callable[[int], None] | None = None

    async def read_values(self, spreadsheet_id: str, worksheet: str) -> list[list[str]]:
        self.reads += 1
        if self.on_read:
            self.on_read(self.reads)
        await asyncio.sleep(0)  # yield, so concurrent runs really interleave
        rows = [list(r) for r in self.grid]
        for row in rows:  # the API omits trailing empty cells and rows
            while row and row[-1] == "":
                row.pop()
        while rows and not rows[-1]:
            rows.pop()
        return rows

    async def write_cells(
        self, spreadsheet_id: str, worksheet: str, cells: list[CellWrite]
    ) -> None:
        if not cells:
            return
        await asyncio.sleep(0)
        for cell in cells:
            while len(self.grid) < cell.row:
                self.grid.append([])
            row = self.grid[cell.row - 1]
            row.extend([""] * (cell.column + 1 - len(row)))
            row[cell.column] = cell.value
        self.writes.append(cells)

    def cell(self, row: int, column: int) -> str:
        cells = self.grid[row - 1]
        return cells[column] if column < len(cells) else ""


class FakeTickets:
    """Ticket system fake that can lose a create response, like a timeout after success."""

    def __init__(self) -> None:
        self.tickets: dict[str, dict] = {}
        self.calls: list[str] = []
        self.lose_next_create_response = False
        self.reject_creates = False
        self.created_drafts: list[TicketDraft] = []
        self.comments: list[tuple[str, str]] = []

    async def find_by_system_id(self, system_id: uuid.UUID) -> TicketRef | None:
        self.calls.append("find")
        for key, ticket in self.tickets.items():
            if ticket["system_id"] == system_id:
                return TicketRef(key=key, url=f"https://jira/browse/{key}")
        return None

    async def create_ticket(self, draft: TicketDraft) -> TicketRef:
        self.calls.append("create")
        if self.reject_creates:
            raise TicketRequestRejected("Field 'priority' cannot be set")
        await asyncio.sleep(0.01)  # a slow request: overlapping pushes would both get here
        self.created_drafts.append(draft)
        key = f"OPS-{len(self.tickets) + 1}"
        self.tickets[key] = {"system_id": draft.system_id, "content": draft}
        if self.lose_next_create_response:
            self.lose_next_create_response = False
            raise TicketBackendUnavailable("read timed out")
        return TicketRef(key=key, url=f"https://jira/browse/{key}")

    async def update_content(self, key: str, content: TicketContent) -> None:
        self.calls.append("update")
        self.tickets[key]["content"] = content

    async def transition(self, key: str, status: str) -> bool:
        raise NotImplementedError

    async def add_comment(self, key: str, text: str) -> None:
        self.calls.append("comment")
        self.comments.append((key, text))

    def ticket_url(self, key: str) -> str:
        return f"https://jira/browse/{key}"

    async def get_ticket(self, key: str) -> TicketSnapshot:
        self.calls.append("get")
        return TicketSnapshot(
            key=key,
            url=self.ticket_url(key),
            external_status="To Do",
            status="open",
            priority="High",
            assignee_id="acc-1",
            assignee_name="Olga Ops",
            updated_at=TICKET_CREATED_AT,
        )

    async def parse_webhook(self, payload: dict) -> None:
        raise NotImplementedError


# ---------- fixtures ----------


@pytest.fixture(scope="module")
def migrated_url() -> Iterator[str]:
    with throwaway_database() as url:
        command.upgrade(alembic_config(url), "head")
        yield url


@pytest.fixture
async def sessionmaker(migrated_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(migrated_url, poolclass=pool.NullPool)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE document, document_record, document_record_history, "
                "ai_judgement, ticket_sync RESTART IDENTITY CASCADE"
            )
        )
    try:
        yield create_sessionmaker(engine)
    finally:
        await engine.dispose()


async def _add_document(
    sessionmaker: async_sessionmaker[AsyncSession],
    worksheet: str = "Requests",
    mapping: dict[str, str] = MAPPING,
    **extra: object,
) -> int:
    async with sessionmaker() as session, session.begin():
        type_id = await session.scalar(text("SELECT id FROM document_type LIMIT 1"))
        document = Document(
            document_type_id=type_id,
            name=worksheet,
            spreadsheet_id="1AbC",
            worksheet_name=worksheet,
            mapping=mapping,
            **extra,
        )
        session.add(document)
        await session.flush()
        return document.id


@pytest.fixture
async def document_id(sessionmaker: async_sessionmaker[AsyncSession]) -> int:
    return await _add_document(sessionmaker)


def _sync(sessionmaker: async_sessionmaker[AsyncSession], sheet: FakeSheet) -> InboundSync:
    return InboundSync(sessionmaker, sheet, BASE_URL)


async def _records(sessionmaker: async_sessionmaker[AsyncSession]) -> list[DocumentRecord]:
    async with sessionmaker() as session:
        return list(await session.scalars(select(DocumentRecord).order_by(DocumentRecord.id)))


async def _events(sessionmaker: async_sessionmaker[AsyncSession]) -> list[tuple[int, str]]:
    async with sessionmaker() as session:
        rows = await session.execute(
            select(DocumentRecordHistory.document_record_id, DocumentRecordHistory.event_type)
            .order_by(DocumentRecordHistory.id)
        )  # fmt: skip
        return [tuple(r) for r in rows]  # type: ignore[misc]


async def _ticket_sync(
    sessionmaker: async_sessionmaker[AsyncSession], record_id: int
) -> TicketSync:
    async with sessionmaker() as session:
        sync = await session.scalar(
            select(TicketSync).where(TicketSync.document_record_id == record_id)
        )
        assert sync is not None
        return sync


def _two_new_rows() -> FakeSheet:
    return FakeSheet(
        [
            HEADER,
            ["Export fails", "Clicking export errors", "Alice", "Ops"],
            [],
            ["Login slow", "", "Bob"],
        ]
    )


# ---------- inbound: identity, records, events ----------


async def test_new_rows_get_ids_links_records_and_events(
    sessionmaker: async_sessionmaker[AsyncSession], document_id: int
) -> None:
    sheet = _two_new_rows()

    report = await _sync(sessionmaker, sheet).sync_document(document_id)

    assert report is not None
    assert (report.created, report.ids_assigned) == (2, 2)
    records = await _records(sessionmaker)
    assert [(r.title, r.description, r.reporter) for r in records] == [
        ("Export fails", "Clicking export errors", "Alice"),
        ("Login slow", None, "Bob"),
    ]
    for record, row in zip(records, (2, 4), strict=True):
        assert sheet.cell(row, SID_COL) == str(record.system_id)
        assert sheet.cell(row, LINK_COL) == f"{BASE_URL}/r/{record.system_id}"
        assert record.raw["Team"] == ("Ops" if row == 2 else "")
        assert record.sheet_written_hash is not None
    assert await _events(sessionmaker) == [(records[0].id, "created"), (records[1].id, "created")]
    assert report.pending_record_ids == [r.id for r in records]
    assert (await _ticket_sync(sessionmaker, records[0].id)).push_status == "pending"


async def test_second_run_without_changes_writes_nothing(
    sessionmaker: async_sessionmaker[AsyncSession], document_id: int
) -> None:
    sheet = _two_new_rows()
    sync = _sync(sessionmaker, sheet)
    await sync.sync_document(document_id)
    writes = len(sheet.writes)

    report = await sync.sync_document(document_id)

    assert report is not None
    assert (report.created, report.modified, report.unchanged) == (0, 0, 2)
    assert len(sheet.writes) == writes
    assert len(await _events(sessionmaker)) == 2


async def test_edit_records_source_modified_with_before_and_after(
    sessionmaker: async_sessionmaker[AsyncSession], document_id: int
) -> None:
    sheet = _two_new_rows()
    sync = _sync(sessionmaker, sheet)
    await sync.sync_document(document_id)
    first = (await _records(sessionmaker))[0]
    async with sessionmaker() as session, session.begin():
        await session.execute(text("UPDATE ticket_sync SET push_status = 'success'"))

    sheet.grid[1][0] = "Export fails for CSV"
    report = await sync.sync_document(document_id)

    assert report is not None and report.modified == 1
    async with sessionmaker() as session:
        event = await session.scalar(
            select(DocumentRecordHistory).where(
                DocumentRecordHistory.event_type == "source_modified"
            )
        )
    assert event is not None and event.document_record_id == first.id
    assert event.detail == {
        "row": 2,
        "changes": {"title": {"from": "Export fails", "to": "Export fails for CSV"}},
    }
    assert (await _ticket_sync(sessionmaker, first.id)).push_status == "pending"
    assert report.pending_record_ids == [first.id]


async def test_unmapped_column_change_updates_raw_only(
    sessionmaker: async_sessionmaker[AsyncSession], document_id: int
) -> None:
    sheet = _two_new_rows()
    sync = _sync(sessionmaker, sheet)
    await sync.sync_document(document_id)

    sheet.grid[1][3] = "Support"  # "Team" is not in the mapping
    report = await sync.sync_document(document_id)

    assert report is not None and (report.modified, report.unchanged) == (0, 2)
    assert (await _records(sessionmaker))[0].raw["Team"] == "Support"
    assert len(await _events(sessionmaker)) == 2


async def test_bad_and_duplicate_ids_are_skipped_not_reassigned(
    sessionmaker: async_sessionmaker[AsyncSession], document_id: int
) -> None:
    dup = str(uuid.uuid4())
    sheet = FakeSheet(
        [
            HEADER,
            ["Typo id", "", "", "", "not-a-uuid"],
            ["Original", "", "", "", dup],
            ["Copied row", "", "", "", dup],
        ]
    )

    report = await _sync(sessionmaker, sheet).sync_document(document_id)

    assert report is not None and (report.created, report.skipped) == (1, 2)
    assert [r.title for r in await _records(sessionmaker)] == ["Original"]
    assert sheet.cell(2, SID_COL) == "not-a-uuid"  # never overwritten blindly


async def test_hand_edited_link_is_restored(
    sessionmaker: async_sessionmaker[AsyncSession], document_id: int
) -> None:
    sheet = _two_new_rows()
    sync = _sync(sessionmaker, sheet)
    await sync.sync_document(document_id)
    record = (await _records(sessionmaker))[0]

    sheet.grid[1][LINK_COL] = "https://evil.example/phish"
    report = await sync.sync_document(document_id)

    assert report is not None and report.cells_written == 1
    assert sheet.cell(2, LINK_COL) == f"{BASE_URL}/r/{record.system_id}"


async def test_row_moved_during_sync_is_not_given_someone_elses_id(
    sessionmaker: async_sessionmaker[AsyncSession], document_id: int
) -> None:
    sheet = FakeSheet([HEADER, ["New request"]])

    def someone_inserts_a_row(read_number: int) -> None:
        if read_number == 2:  # between the first read and the pre-write re-read
            sheet.grid.insert(1, ["Inserted above"])

    sheet.on_read = someone_inserts_a_row
    sync = _sync(sessionmaker, sheet)

    report = await sync.sync_document(document_id)

    assert report is not None and report.ids_assigned == 0
    assert sheet.writes == []
    sheet.on_read = None
    await sync.sync_document(document_id)
    assert sorted(r.title for r in await _records(sessionmaker)) == [
        "Inserted above",
        "New request",
    ]
    assert sheet.cell(2, SID_COL) and sheet.cell(3, SID_COL)


async def test_id_already_used_by_another_document_is_skipped(
    sessionmaker: async_sessionmaker[AsyncSession], document_id: int
) -> None:
    sheet = _two_new_rows()
    await _sync(sessionmaker, sheet).sync_document(document_id)
    copied = copy.deepcopy(sheet.grid[:2])
    other_id = await _add_document(sessionmaker, worksheet="Copy")

    report = await _sync(sessionmaker, FakeSheet(copied)).sync_document(other_id)

    assert report is not None and (report.created, report.skipped) == (0, 1)
    assert len(await _records(sessionmaker)) == 2


async def test_concurrent_runs_create_each_record_once(
    sessionmaker: async_sessionmaker[AsyncSession], document_id: int
) -> None:
    # An Apps Script push and the poll firing at the same moment.
    sheet = _two_new_rows()
    reports = await asyncio.gather(
        _sync(sessionmaker, sheet).sync_document(document_id),
        _sync(sessionmaker, sheet).sync_document(document_id),
    )

    records = await _records(sessionmaker)
    assert len(records) == 2
    assert sum(r.created for r in reports if r) == 2
    assert {sheet.cell(2, SID_COL), sheet.cell(4, SID_COL)} == {str(r.system_id) for r in records}
    assert len(await _events(sessionmaker)) == 2


async def test_disabled_document_and_layout_errors(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    disabled = await _add_document(sessionmaker, worksheet="Off", sync_enabled=False)
    assert await _sync(sessionmaker, _two_new_rows()).sync_document(disabled) is None

    broken = await _add_document(sessionmaker, worksheet="Broken")
    with pytest.raises(SheetLayoutError, match="Detail link"):
        await _sync(sessionmaker, FakeSheet([["Title", "System ID"]])).sync_document(broken)


# ---------- push ----------


async def _one_record(sessionmaker: async_sessionmaker[AsyncSession], document_id: int) -> int:
    sheet = FakeSheet([HEADER, ["Export fails", "Details", "Alice"]])
    await _sync(sessionmaker, sheet).sync_document(document_id)
    return (await _records(sessionmaker))[0].id


async def test_push_creates_then_skips_then_updates(
    sessionmaker: async_sessionmaker[AsyncSession], document_id: int
) -> None:
    record_id = await _one_record(sessionmaker, document_id)
    tickets = FakeTickets()

    created = await push_record(sessionmaker, tickets, record_id, BASE_URL)
    again = await push_record(sessionmaker, tickets, record_id, BASE_URL)
    async with sessionmaker() as session, session.begin():
        await session.execute(text("UPDATE document_record SET title = 'Export fails (CSV)'"))
    updated = await push_record(sessionmaker, tickets, record_id, BASE_URL)

    assert (created.outcome, again.outcome, updated.outcome) == ("created", "unchanged", "updated")
    assert tickets.calls == ["find", "create", "get", "update"]
    draft = tickets.tickets["OPS-1"]["content"]
    assert draft.title == "Export fails (CSV)"
    sync = await _ticket_sync(sessionmaker, record_id)
    assert (sync.issue_key, sync.push_status, sync.retry_count) == ("OPS-1", "success", 0)
    assert sync.pushed_hash and sync.pushed_at


async def test_concurrent_pushes_open_one_ticket(
    sessionmaker: async_sessionmaker[AsyncSession], document_id: int
) -> None:
    record_id = await _one_record(sessionmaker, document_id)
    tickets = FakeTickets()

    results = await asyncio.gather(
        *(push_record(sessionmaker, tickets, record_id, BASE_URL) for _ in range(3))
    )

    assert sorted(r.outcome for r in results) == ["created", "unchanged", "unchanged"]
    assert list(tickets.tickets) == ["OPS-1"]


async def test_create_that_timed_out_after_success_is_not_repeated(
    sessionmaker: async_sessionmaker[AsyncSession], document_id: int
) -> None:
    record_id = await _one_record(sessionmaker, document_id)
    tickets = FakeTickets()
    tickets.lose_next_create_response = True

    first = await push_record(sessionmaker, tickets, record_id, BASE_URL)
    sync = await _ticket_sync(sessionmaker, record_id)
    assert first.outcome == "failed" and first.retryable
    assert (sync.push_status, sync.retry_count, sync.issue_key) == ("failed", 1, None)
    assert "read timed out" in (sync.last_error or "")

    retry = await push_record(sessionmaker, tickets, record_id, BASE_URL)

    assert retry.outcome == "linked"
    assert list(tickets.tickets) == ["OPS-1"]
    sync = await _ticket_sync(sessionmaker, record_id)
    assert (sync.push_status, sync.retry_count, sync.issue_key) == ("success", 0, "OPS-1")


async def test_rejected_push_is_not_retryable(
    sessionmaker: async_sessionmaker[AsyncSession], document_id: int
) -> None:
    record_id = await _one_record(sessionmaker, document_id)
    tickets = FakeTickets()
    tickets.reject_creates = True

    result = await push_record(sessionmaker, tickets, record_id, BASE_URL)

    assert result.outcome == "failed" and not result.retryable
    assert (await _ticket_sync(sessionmaker, record_id)).push_status == "failed"


async def test_pending_records_are_offered_again_until_pushed(
    sessionmaker: async_sessionmaker[AsyncSession], document_id: int
) -> None:
    # A push job lost between commit and enqueue is recovered by the next run.
    sheet = FakeSheet([HEADER, ["Export fails"]])
    sync = _sync(sessionmaker, sheet)
    first = await sync.sync_document(document_id)
    second = await sync.sync_document(document_id)
    assert first and second and first.pending_record_ids == second.pending_record_ids != []

    await push_record(sessionmaker, FakeTickets(), first.pending_record_ids[0], BASE_URL)
    third = await sync.sync_document(document_id)
    assert third is not None and third.pending_record_ids == []


async def test_long_titles_are_cut_to_the_column_size(
    sessionmaker: async_sessionmaker[AsyncSession], document_id: int
) -> None:
    sheet = FakeSheet([HEADER, ["x" * 300]])
    await _sync(sessionmaker, sheet).sync_document(document_id)
    async with sessionmaker() as session:
        lengths = await session.execute(
            select(func.length(DocumentRecord.title), DocumentRecord.raw["Title"].astext)
        )
        title_length, raw_title = lengths.one()
    assert title_length == 255
    assert len(raw_title) == 300


# ---------- AI triage before create ----------

JUDGEMENT = Judgement(
    type="bug",
    urgency="high",
    assignee_role="backend",
    summary="Export is broken.",
    reason="Finance is blocked.",
)


class FakeClassifier:
    def __init__(self, *outcomes: Judgement | ClassifierError) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[ClassifierInput] = []

    async def classify(self, request: ClassifierInput) -> Classification:
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, ClassifierError):
            raise outcome
        return Classification(judgement=outcome, model="fake-model")


async def _judgements(sessionmaker: async_sessionmaker[AsyncSession]) -> list[AiJudgement]:
    async with sessionmaker() as session:
        return list(await session.scalars(select(AiJudgement)))


async def test_new_ticket_is_triaged_once_and_carries_the_judgement(
    sessionmaker: async_sessionmaker[AsyncSession], document_id: int
) -> None:
    record_id = await _one_record(sessionmaker, document_id)
    tickets, classifier = FakeTickets(), FakeClassifier(JUDGEMENT)
    triage = Triage(classifier, max_attempts=3)

    created = await push_record(sessionmaker, tickets, record_id, BASE_URL, triage=triage)
    async with sessionmaker() as session, session.begin():
        await session.execute(text("UPDATE document_record SET title = 'Export fails (CSV)'"))
    updated = await push_record(sessionmaker, tickets, record_id, BASE_URL, triage=triage)

    assert (created.outcome, updated.outcome) == ("created", "updated")
    assert classifier.requests == [ClassifierInput(title="Export fails", description="Details")]
    draft = tickets.created_drafts[0]
    assert (draft.urgency, draft.assignee_role) == ("high", "backend")
    assert draft.triage is not None and draft.triage.model == "fake-model"
    # The update keeps showing the same judgement; it is not re-classified.
    assert tickets.tickets["OPS-1"]["content"].triage == draft.triage
    [stored] = await _judgements(sessionmaker)
    assert (stored.summary, stored.model) == ("Export is broken.", "fake-model")


async def test_transient_llm_failures_retry_then_create_without_triage(
    sessionmaker: async_sessionmaker[AsyncSession], document_id: int
) -> None:
    record_id = await _one_record(sessionmaker, document_id)
    down = ClassifierError("LLM API returned 503", retryable=True)
    tickets, classifier = FakeTickets(), FakeClassifier(down, down, down)
    triage = Triage(classifier, max_attempts=3)

    first = await push_record(sessionmaker, tickets, record_id, BASE_URL, triage=triage)
    second = await push_record(sessionmaker, tickets, record_id, BASE_URL, triage=triage)
    assert (first.outcome, second.outcome) == ("failed", "failed")
    assert first.retryable and tickets.calls == []
    assert "503" in ((await _ticket_sync(sessionmaker, record_id)).last_error or "")

    third = await push_record(sessionmaker, tickets, record_id, BASE_URL, triage=triage)

    assert third.outcome == "created"
    draft = tickets.created_drafts[0]
    assert (draft.urgency, draft.assignee_role, draft.triage) == (None, None, None)
    assert await _judgements(sessionmaker) == []
    assert (await _ticket_sync(sessionmaker, record_id)).retry_count == 0


async def test_permanent_llm_failure_creates_without_triage_immediately(
    sessionmaker: async_sessionmaker[AsyncSession], document_id: int
) -> None:
    record_id = await _one_record(sessionmaker, document_id)
    tickets = FakeTickets()
    classifier = FakeClassifier(ClassifierError("LLM API returned 401: invalid api key"))

    result = await push_record(
        sessionmaker, tickets, record_id, BASE_URL, triage=Triage(classifier, max_attempts=3)
    )

    assert result.outcome == "created"
    assert tickets.created_drafts[0].urgency is None


async def test_judgement_survives_a_jira_failure(
    sessionmaker: async_sessionmaker[AsyncSession], document_id: int
) -> None:
    # The LLM answered, then Jira timed out: the retry must reuse the stored judgement.
    record_id = await _one_record(sessionmaker, document_id)
    tickets, classifier = FakeTickets(), FakeClassifier(JUDGEMENT)
    tickets.reject_creates = True
    triage = Triage(classifier, max_attempts=3)

    assert (
        await push_record(sessionmaker, tickets, record_id, BASE_URL, triage=triage)
    ).outcome == "failed"
    tickets.reject_creates = False
    assert (
        await push_record(sessionmaker, tickets, record_id, BASE_URL, triage=triage)
    ).outcome == "created"

    assert len(classifier.requests) == 1
    assert len(await _judgements(sessionmaker)) == 1
    assert tickets.created_drafts[-1].urgency == "high"


# ---------- assignment rules and Slack notification ----------


class FakeSlack:
    def __init__(self, *errors: Exception) -> None:
        self.errors = list(errors)
        self.messages: list[tuple[str, str, list]] = []

    async def post_message(self, channel: str, text: str, blocks: list) -> str:
        await asyncio.sleep(0.01)  # slow, so overlapping jobs would both get here
        if self.errors:
            raise self.errors.pop(0)
        self.messages.append((channel, text, blocks))
        return "1.0"


SLACK_CONFIG = SlackConfig(
    default_channel="C-TRIAGE",
    users={"backend": "U-BE", "frontend": "U-FE", "ops": "U-OPS", "support": "U-SUP"},
)
RULES = AssignmentConfig.model_validate(
    {"rules": [{"name": "Exports go to ops", "when": {"keywords": "export"}, "assign": "ops"}]}
)


def _notifier(slack: FakeSlack, rules: AssignmentConfig = RULES) -> Notifier:
    return Notifier(
        slack=slack,  # type: ignore[arg-type]
        slack_config=SLACK_CONFIG,
        rules=rules,
        ticket_url=lambda key: f"https://jira/browse/{key}",
        public_base_url=BASE_URL,
    )


async def test_rule_overrides_ai_team_and_explains_it_in_a_comment(
    sessionmaker: async_sessionmaker[AsyncSession], document_id: int
) -> None:
    record_id = await _one_record(sessionmaker, document_id)
    tickets = FakeTickets()

    await push_record(
        sessionmaker,
        tickets,
        record_id,
        BASE_URL,
        triage=Triage(FakeClassifier(JUDGEMENT)),
        rules=RULES,
    )

    draft = tickets.created_drafts[0]
    assert (draft.urgency, draft.assignee_role) == ("high", "ops")
    assert tickets.comments == [
        ("OPS-1", "Assignment: Rule 'Exports go to ops': team backend -> ops")
    ]
    [stored] = await _judgements(sessionmaker)  # the AI's own answer is kept as it was
    async with sessionmaker() as session:
        role = await session.scalar(
            text("SELECT role_name FROM assignee_role WHERE id = :id"),
            {"id": stored.assignee_role_id},
        )
    assert role == "backend"


async def _ticketed_record(sessionmaker: async_sessionmaker[AsyncSession], document_id: int) -> int:
    record_id = await _one_record(sessionmaker, document_id)
    await push_record(
        sessionmaker, FakeTickets(), record_id, BASE_URL, triage=Triage(FakeClassifier(JUDGEMENT))
    )
    return record_id


async def test_notification_mentions_the_final_assignee_once(
    sessionmaker: async_sessionmaker[AsyncSession], document_id: int
) -> None:
    record_id = await _ticketed_record(sessionmaker, document_id)
    slack = FakeSlack()

    outcomes = await asyncio.gather(
        *(notify_record(sessionmaker, _notifier(slack), record_id) for _ in range(3))
    )

    assert sorted(outcomes) == ["already_sent", "already_sent", "sent"]
    [(channel, fallback, _blocks)] = slack.messages
    assert channel == "C-TRIAGE"
    assert fallback == "New ticket OPS-1: Export fails - assigned to <@U-OPS>"
    assert (await _ticket_sync(sessionmaker, record_id)).notified_at is not None


async def test_failed_notification_stays_pending_and_is_offered_again(
    sessionmaker: async_sessionmaker[AsyncSession], document_id: int
) -> None:
    record_id = await _ticketed_record(sessionmaker, document_id)
    slack = FakeSlack(SlackError("Slack chat.postMessage failed: not_in_channel"))

    with pytest.raises(SlackError):
        await notify_record(sessionmaker, _notifier(slack), record_id)

    assert (await _ticket_sync(sessionmaker, record_id)).notified_at is None
    system_id = str((await _records(sessionmaker))[0].system_id)
    sheet = FakeSheet([HEADER, ["Export fails", "Details", "Alice", "", system_id]])
    report = await _sync(sessionmaker, sheet).sync_document(document_id)
    assert report is not None and report.unnotified_record_ids == [record_id]

    assert await notify_record(sessionmaker, _notifier(slack), record_id) == "sent"
    report = await _sync(sessionmaker, sheet).sync_document(document_id)
    assert report is not None and report.unnotified_record_ids == []


async def test_no_notification_before_the_ticket_exists(
    sessionmaker: async_sessionmaker[AsyncSession], document_id: int
) -> None:
    record_id = await _one_record(sessionmaker, document_id)
    slack = FakeSlack()
    assert await notify_record(sessionmaker, _notifier(slack), record_id) == "no_ticket"
    assert slack.messages == []


async def test_without_ai_or_rule_the_channel_is_asked_to_triage(
    sessionmaker: async_sessionmaker[AsyncSession], document_id: int
) -> None:
    record_id = await _one_record(sessionmaker, document_id)
    await push_record(sessionmaker, FakeTickets(), record_id, BASE_URL)
    slack = FakeSlack()

    await notify_record(sessionmaker, _notifier(slack, AssignmentConfig()), record_id)

    assert slack.messages[0][1].endswith("needs manual triage")


# ---------- Jira events -> database -> sheet ----------

HEADER_WB = [*HEADER, "Status", "Latest note"]
STATUS_COL, NOTE_COL = 6, 7
MAPPING_WB = {**MAPPING, "status": "Status", "latest_note": "Latest note"}


def _event(
    *,
    seconds: int = 60,
    status: str | None = "in_progress",
    external: str = "In Progress",
    priority: str = "High",
    own: bool = False,
    comment: TicketComment | None = None,
) -> TicketEvent:
    state = None
    if comment is None:
        state = TicketState(
            external_status=external,
            status=status,
            priority=priority,
            assignee_id="acc-1",
            assignee_name="Olga Ops",
        )
    return TicketEvent(
        kind="comment_created" if comment else "issue_updated",
        key="OPS-1",
        occurred_at=TICKET_CREATED_AT + timedelta(seconds=seconds),
        actor_id="acc-human",
        actor_name="Olga Ops",
        from_service_account=own,
        state=state,
        comment=comment,
    )


def _comment(comment_id: str = "c1", body: str = "Fixed in 1.4", hours: int = 0) -> TicketComment:
    return TicketComment(
        id=comment_id,
        author_id="acc-human",
        author_name="Olga Ops",
        text=body,
        created_at=datetime(2026, 11, 17, 10, 0, tzinfo=UTC) + timedelta(hours=hours),
    )


async def _status_of(sessionmaker: async_sessionmaker[AsyncSession], record_id: int) -> str | None:
    async with sessionmaker() as session:
        return await session.scalar(
            text(
                "SELECT s.status_name FROM document_record r "
                "LEFT JOIN document_record_status s ON s.id = r.status_id WHERE r.id = :id"
            ),
            {"id": record_id},
        )


async def _sheet_with_ticket(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> tuple[int, int, FakeSheet, InboundSync]:
    document_id = await _add_document(sessionmaker, worksheet="Tracked", mapping=MAPPING_WB)
    sheet = FakeSheet([HEADER_WB, ["Export fails", "Details", "Alice"]])
    sync = _sync(sessionmaker, sheet)
    await sync.sync_document(document_id)
    record_id = (await _records(sessionmaker))[0].id
    await push_record(sessionmaker, FakeTickets(), record_id, BASE_URL)
    return document_id, record_id, sheet, sync


async def test_creation_reads_back_a_baseline_without_an_event(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    document_id, record_id, sheet, sync = await _sheet_with_ticket(sessionmaker)

    record = (await _records(sessionmaker))[0]
    assert await _status_of(sessionmaker, record_id) == "open"
    assert (record.current_priority, record.current_assignee) == ("High", "Olga Ops")
    assert (await _ticket_sync(sessionmaker, record_id)).ticket_updated_at == TICKET_CREATED_AT
    assert [e for _, e in await _events(sessionmaker)] == ["created"]
    # The next sync run shows the initial status in the sheet.
    await sync.sync_document(document_id)
    assert sheet.cell(2, STATUS_COL) == "Open"


async def test_status_change_is_recorded_and_written_to_the_sheet(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    document_id, record_id, sheet, sync = await _sheet_with_ticket(sessionmaker)

    result = await apply_ticket_event(sessionmaker, _event())

    assert (result.outcome, result.document_id, result.sheet_changed) == (
        "updated",
        document_id,
        True,
    )
    assert await _status_of(sessionmaker, record_id) == "in_progress"
    async with sessionmaker() as session:
        event = await session.scalar(
            select(DocumentRecordHistory).where(
                DocumentRecordHistory.event_type == "status_changed"
            )
        )
    assert event is not None and event.created_by == "Olga Ops"
    assert event.detail == {"jira_status": "In Progress"}
    await sync.sync_document(document_id)
    assert sheet.cell(2, STATUS_COL) == "In progress"
    assert (await _records(sessionmaker))[0].sheet_written_hash is not None


async def test_loop_guards(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    _, record_id, _, _ = await _sheet_with_ticket(sessionmaker)

    own = await apply_ticket_event(sessionmaker, _event(own=True, status="done"))
    before_creation = await apply_ticket_event(sessionmaker, _event(seconds=-5, status="done"))
    same = await apply_ticket_event(sessionmaker, _event(status="open", external="To Do"))
    unknown = await apply_ticket_event(sessionmaker, _event().model_copy(update={"key": "OPS-404"}))

    assert (own.outcome, before_creation.outcome, same.outcome, unknown.outcome) == (
        "own_change",
        "stale",
        "unchanged",
        "unknown_ticket",
    )
    assert await _status_of(sessionmaker, record_id) == "open"
    assert [e for _, e in await _events(sessionmaker)] == ["created"]


async def test_out_of_order_events_converge_on_the_newest_state(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _, record_id, _, _ = await _sheet_with_ticket(sessionmaker)
    # t+120 says Done/Highest; the older t+60 (In Progress/High) arrives afterwards.
    newer = _event(seconds=120, status="done", external="Done", priority="Highest")
    older = _event(seconds=60)

    assert (await apply_ticket_event(sessionmaker, newer)).outcome == "updated"
    assert (await apply_ticket_event(sessionmaker, older)).outcome == "stale"
    assert (await apply_ticket_event(sessionmaker, newer)).outcome == "stale"  # redelivery

    assert await _status_of(sessionmaker, record_id) == "done"
    assert (await _records(sessionmaker))[0].current_priority == "Highest"


async def test_unmapped_status_keeps_the_status_but_takes_the_rest(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _, record_id, _, _ = await _sheet_with_ticket(sessionmaker)

    result = await apply_ticket_event(
        sessionmaker, _event(status=None, external="In Review", priority="Low")
    )

    assert (result.outcome, result.sheet_changed) == ("updated", False)
    assert await _status_of(sessionmaker, record_id) == "open"
    assert (await _records(sessionmaker))[0].current_priority == "Low"


async def test_comments_are_logged_once_and_become_the_latest_note(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    document_id, _, sheet, sync = await _sheet_with_ticket(sessionmaker)
    multiline = "Deployed." + chr(10) + "Please  check"

    first = await apply_ticket_event(sessionmaker, _event(comment=_comment()))
    again = await apply_ticket_event(sessionmaker, _event(comment=_comment()))
    await apply_ticket_event(sessionmaker, _event(comment=_comment("c2", multiline, hours=1)))

    assert (first.outcome, first.sheet_changed, again.outcome) == ("updated", True, "duplicate")
    async with sessionmaker() as session:
        comments = list(
            await session.scalars(
                select(DocumentRecordHistory.comment)
                .where(DocumentRecordHistory.event_type == "comment")
                .order_by(DocumentRecordHistory.create_time)
            )
        )
    assert comments == ["Fixed in 1.4", multiline]
    await sync.sync_document(document_id)
    assert sheet.cell(2, NOTE_COL) == "2026-11-17 Olga Ops: Deployed. Please check"


async def test_hand_edited_status_cell_is_restored(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    document_id, _, sheet, sync = await _sheet_with_ticket(sessionmaker)
    await sync.sync_document(document_id)
    sheet.grid[1][STATUS_COL] = "Done!!"

    report = await sync.sync_document(document_id)

    assert report is not None and report.cells_written == 1
    assert sheet.cell(2, STATUS_COL) == "Open"

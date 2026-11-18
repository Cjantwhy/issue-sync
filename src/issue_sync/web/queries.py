"""Read-only queries behind the dashboard and the detail page.

One statement lists records with everything a row shows: the newest AI judgement is
joined through a `DISTINCT ON` subquery, so a page costs one query plus a count, never
one query per record.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from issue_sync.adapter_config import JiraConfig
from issue_sync.db.models import (
    AiJudgement,
    AiJudgementType,
    AiJudgementUrgency,
    AssigneeRole,
    Document,
    DocumentRecord,
    DocumentRecordHistory,
    DocumentRecordStatus,
    TicketSync,
)
from issue_sync.sources.sheets.layout import links_in_raw
from issue_sync.sync.inbound import sheet_attachments
from issue_sync.web.comparison import DIFFERS, translate, verdict

PAGE_SIZE = 50
SEARCH_MAX_LENGTH = 200

# Comparison filter values: "differs" means rule or human.
COMPARISON_FILTERS = ("differs", "rule", "human", "agree")


@dataclass(frozen=True)
class Filters:
    status: str | None = None
    type: str | None = None
    urgency: str | None = None
    document_id: int | None = None
    comparison: str | None = None
    search: str | None = None  # part of the title, case-insensitive
    attention: bool = False  # push failed, or the row was deleted from the sheet
    page: int = 1

    def query(self, **overrides: Any) -> dict[str, str]:
        """Non-empty values as query parameters (for links and the pushed URL)."""
        values = {
            "q": self.search,
            "attention": "1" if self.attention else None,
            "status": self.status,
            "type": self.type,
            "urgency": self.urgency,
            "document": self.document_id,
            "comparison": self.comparison,
            "page": self.page if self.page > 1 else None,
        } | overrides
        return {k: str(v) for k, v in values.items() if v not in (None, "")}


@dataclass(frozen=True)
class RecordRow:
    system_id: uuid.UUID
    title: str | None
    document_name: str
    create_time: datetime
    source_deleted_at: datetime | None
    status: str | None
    status_terminal: bool
    current_priority: str | None
    current_assignee: str | None
    issue_key: str | None
    push_status: str | None
    ai_type: str | None
    ai_urgency: str | None
    ai_role: str | None
    ai_model: str | None
    ai_priority: str | None  # AI urgency as a Jira priority name
    assigned_urgency: str | None
    assigned_role: str | None
    assigned_priority: str | None
    assignment_rule: str | None
    priority_verdict: str
    assignee_verdict: str
    jira_attachments: int = 0
    sheet_links: tuple[str, ...] = ()

    @property
    def attachment_count(self) -> int:
        return self.jira_attachments + len(self.sheet_links)


def _row(values: dict[str, Any]) -> RecordRow:
    """A RecordRow from a select_rows() result: links come from the stored sheet row."""
    raw = values.pop("raw")
    mapping = values.pop("mapping") or {}
    header = mapping.get("attachments")
    return RecordRow(
        **values, sheet_links=links_in_raw(raw, header if isinstance(header, str) else None)
    )


@dataclass(frozen=True)
class RecordPage:
    rows: list[RecordRow]
    total: int
    page: int
    pages: int


@dataclass(frozen=True)
class ModelStats:
    model: str
    compared: int
    priority: dict[str, int]
    assignee: dict[str, int]


@dataclass(frozen=True)
class Judgement:
    type: str
    urgency: str
    assignee_role: str
    summary: str
    reason: str
    model: str
    create_time: datetime


@dataclass(frozen=True)
class TimelineEvent:
    event_type: str
    from_status: str | None
    to_status: str | None
    comment: str | None
    attachments: list[dict[str, Any]]
    detail: dict[str, Any]
    created_by: str
    create_time: datetime


@dataclass(frozen=True)
class RecordDetail:
    row: RecordRow
    description: str | None
    reporter: str | None
    judgements: list[Judgement]  # newest first
    timeline: list[TimelineEvent] = field(default_factory=list)

    @property
    def sheet_attachments(self) -> list[dict[str, Any]]:
        """The links the sheet row has now."""
        return sheet_attachments(self.row.sheet_links) or []

    @property
    def jira_attachments(self) -> list[dict[str, Any]]:
        """Every file added to the ticket, oldest first, with who added it and when."""
        return [
            {**item, "added_by": e.created_by, "added_at": e.create_time}
            for e in self.timeline
            if e.event_type == "attachment"
            for item in e.attachments
            if isinstance(item, dict)
        ]


@dataclass(frozen=True)
class FilterOptions:
    statuses: list[str]
    types: list[str]
    urgencies: list[str]
    documents: list[tuple[int, str]]


class _Columns:
    """The aliased tables and computed columns shared by every record query."""

    def __init__(self, jira: JiraConfig) -> None:
        latest = (
            select(AiJudgement)
            .ext(distinct_on(AiJudgement.document_record_id))
            .order_by(
                AiJudgement.document_record_id,
                AiJudgement.create_time.desc(),
                AiJudgement.id.desc(),
            )
            .subquery("latest_judgement")
        )
        self.latest = latest
        self.ai_type = aliased(AiJudgementType, name="ai_type")
        self.ai_urgency = aliased(AiJudgementUrgency, name="ai_urgency")
        self.ai_role = aliased(AssigneeRole, name="ai_role")
        self.assigned_urgency = aliased(AiJudgementUrgency, name="assigned_urgency")
        self.assigned_role = aliased(AssigneeRole, name="assigned_role")

        ticket_read = TicketSync.ticket_updated_at.is_not(None)
        self.priority_verdict = verdict(
            ai=self.ai_urgency.urgency_level_name,
            assigned=self.assigned_urgency.urgency_level_name,
            current=DocumentRecord.current_priority,
            mapping=jira.priorities,
            ticket_read=ticket_read,
        ).label("priority_verdict")
        self.assignee_verdict = verdict(
            ai=self.ai_role.role_name,
            assigned=self.assigned_role.role_name,
            current=DocumentRecord.current_assignee_id,
            mapping=jira.assignees,
            ticket_read=ticket_read,
            # A name without an id: recorded before ids were kept, not yet filled in.
            current_unknown=and_(
                DocumentRecord.current_assignee.is_not(None),
                DocumentRecord.current_assignee_id.is_(None),
            ),
        ).label("assignee_verdict")
        self.ai_priority = translate(jira.priorities, self.ai_urgency.urgency_level_name)
        self.assigned_priority = translate(
            jira.priorities, self.assigned_urgency.urgency_level_name
        )

    def select_rows(self) -> Select[Any]:
        latest = self.latest
        return (
            select(
                DocumentRecord.system_id,
                DocumentRecord.title,
                Document.name.label("document_name"),
                DocumentRecord.create_time,
                DocumentRecord.source_deleted_at,
                DocumentRecordStatus.status_name.label("status"),
                func.coalesce(DocumentRecordStatus.is_terminal, False).label("status_terminal"),
                DocumentRecord.current_priority,
                DocumentRecord.current_assignee,
                TicketSync.issue_key,
                TicketSync.push_status,
                self.ai_type.type_name.label("ai_type"),
                self.ai_urgency.urgency_level_name.label("ai_urgency"),
                self.ai_role.role_name.label("ai_role"),
                latest.c.model.label("ai_model"),
                self.ai_priority.label("ai_priority"),
                self.assigned_urgency.urgency_level_name.label("assigned_urgency"),
                self.assigned_role.role_name.label("assigned_role"),
                self.assigned_priority.label("assigned_priority"),
                TicketSync.assignment_rule,
                self.priority_verdict,
                self.assignee_verdict,
                select(func.count())
                .where(
                    DocumentRecordHistory.document_record_id == DocumentRecord.id,
                    DocumentRecordHistory.event_type == "attachment",
                )
                .scalar_subquery()
                .label("jira_attachments"),
                DocumentRecord.raw,
                Document.mapping,
            )
            .select_from(DocumentRecord)
            .join(Document, Document.id == DocumentRecord.document_id)
            .outerjoin(DocumentRecordStatus, DocumentRecordStatus.id == DocumentRecord.status_id)
            .outerjoin(TicketSync, TicketSync.document_record_id == DocumentRecord.id)
            .outerjoin(latest, latest.c.document_record_id == DocumentRecord.id)
            .outerjoin(self.ai_type, self.ai_type.id == latest.c.type_id)
            .outerjoin(self.ai_urgency, self.ai_urgency.id == latest.c.urgency_id)
            .outerjoin(self.ai_role, self.ai_role.id == latest.c.assignee_role_id)
            .outerjoin(
                self.assigned_urgency, self.assigned_urgency.id == TicketSync.assigned_urgency_id
            )
            .outerjoin(self.assigned_role, self.assigned_role.id == TicketSync.assigned_role_id)
        )

    def apply(self, query: Select[Any], filters: Filters) -> Select[Any]:
        if filters.search:
            query = query.where(DocumentRecord.title.icontains(filters.search, autoescape=True))
        if filters.attention:
            query = query.where(
                or_(
                    TicketSync.push_status == "failed",
                    DocumentRecord.source_deleted_at.is_not(None),
                )
            )
        if filters.status:
            query = query.where(DocumentRecordStatus.status_name == filters.status)
        if filters.type:
            query = query.where(self.ai_type.type_name == filters.type)
        if filters.urgency:
            query = query.where(self.ai_urgency.urgency_level_name == filters.urgency)
        if filters.document_id is not None:
            query = query.where(DocumentRecord.document_id == filters.document_id)
        if filters.comparison == "differs":
            query = query.where(
                or_(self.priority_verdict.in_(DIFFERS), self.assignee_verdict.in_(DIFFERS))
            )
        elif filters.comparison in ("rule", "human"):
            query = query.where(
                or_(
                    self.priority_verdict == filters.comparison,
                    self.assignee_verdict == filters.comparison,
                )
            )
        elif filters.comparison == "agree":
            query = query.where(self.priority_verdict == "agree", self.assignee_verdict == "agree")
        return query


async def list_records(session: AsyncSession, jira: JiraConfig, filters: Filters) -> RecordPage:
    cols = _Columns(jira)
    query = cols.apply(cols.select_rows(), filters)
    total = await session.scalar(select(func.count()).select_from(query.subquery())) or 0
    pages = max(1, -(-total // PAGE_SIZE))
    page = min(max(filters.page, 1), pages)
    result = await session.execute(
        query.order_by(DocumentRecord.create_time.desc(), DocumentRecord.id.desc())
        .limit(PAGE_SIZE)
        .offset((page - 1) * PAGE_SIZE)
    )
    rows = [_row(row._asdict()) for row in result]
    return RecordPage(rows=rows, total=total, page=page, pages=pages)


async def model_stats(session: AsyncSession, jira: JiraConfig) -> list[ModelStats]:
    """Per model: how often the ticket still has what the AI suggested.

    Counts only records whose verdict is agree, rule or human for a field; records with
    nothing to compare yet are left out.
    """
    cols = _Columns(jira)
    rows = cols.select_rows().subquery()
    counted = ("agree", "rule", "human")
    columns = [rows.c.ai_model]
    for name in ("priority", "assignee"):
        verdict_col = rows.c[f"{name}_verdict"]
        columns += [func.count().filter(verdict_col == v).label(f"{name}_{v}") for v in counted]
    columns.append(
        func.count()
        .filter(or_(rows.c.priority_verdict.in_(counted), rows.c.assignee_verdict.in_(counted)))
        .label("compared")
    )
    result = await session.execute(
        select(*columns)
        .where(rows.c.ai_model.is_not(None))
        .group_by(rows.c.ai_model)
        .order_by(rows.c.ai_model)
    )
    stats = []
    for row in result:
        m = row._mapping
        if not m["compared"]:
            continue
        stats.append(
            ModelStats(
                model=m["ai_model"],
                compared=m["compared"],
                priority={v: m[f"priority_{v}"] for v in counted},
                assignee={v: m[f"assignee_{v}"] for v in counted},
            )
        )
    return stats


async def filter_options(session: AsyncSession) -> FilterOptions:
    statuses = await session.scalars(
        select(DocumentRecordStatus.status_name).order_by(DocumentRecordStatus.id)
    )
    types = await session.scalars(select(AiJudgementType.type_name).order_by(AiJudgementType.id))
    urgencies = await session.scalars(
        select(AiJudgementUrgency.urgency_level_name).order_by(AiJudgementUrgency.id)
    )
    documents = await session.execute(select(Document.id, Document.name).order_by(Document.name))
    return FilterOptions(
        statuses=list(statuses),
        types=list(types),
        urgencies=list(urgencies),
        documents=[(d.id, d.name) for d in documents],
    )


async def record_detail(
    session: AsyncSession, jira: JiraConfig, system_id: uuid.UUID
) -> RecordDetail | None:
    cols = _Columns(jira)
    row = (
        await session.execute(
            cols.select_rows()
            .add_columns(
                DocumentRecord.id.label("record_id"),
                DocumentRecord.description,
                DocumentRecord.reporter,
            )
            .where(DocumentRecord.system_id == system_id)
        )
    ).first()
    if row is None:
        return None
    values = row._asdict()
    record_id = values.pop("record_id")
    description = values.pop("description")
    reporter = values.pop("reporter")

    judgements = await session.execute(
        select(
            AiJudgementType.type_name,
            AiJudgementUrgency.urgency_level_name,
            AssigneeRole.role_name,
            AiJudgement.summary,
            AiJudgement.reason,
            AiJudgement.model,
            AiJudgement.create_time,
        )
        .join(AiJudgementType, AiJudgementType.id == AiJudgement.type_id)
        .join(AiJudgementUrgency, AiJudgementUrgency.id == AiJudgement.urgency_id)
        .join(AssigneeRole, AssigneeRole.id == AiJudgement.assignee_role_id)
        .where(AiJudgement.document_record_id == record_id)
        .order_by(AiJudgement.create_time.desc(), AiJudgement.id.desc())
    )
    from_status = aliased(DocumentRecordStatus, name="from_status")
    to_status = aliased(DocumentRecordStatus, name="to_status")
    events = await session.execute(
        select(
            DocumentRecordHistory.event_type,
            from_status.status_name,
            to_status.status_name,
            DocumentRecordHistory.comment,
            DocumentRecordHistory.attachments,
            DocumentRecordHistory.detail,
            DocumentRecordHistory.created_by,
            DocumentRecordHistory.create_time,
        )
        .outerjoin(from_status, from_status.id == DocumentRecordHistory.from_status_id)
        .outerjoin(to_status, to_status.id == DocumentRecordHistory.to_status_id)
        .where(DocumentRecordHistory.document_record_id == record_id)
        .order_by(DocumentRecordHistory.create_time, DocumentRecordHistory.id)
    )
    return RecordDetail(
        row=_row(values),
        description=description,
        reporter=reporter,
        judgements=[Judgement(*j) for j in judgements],
        timeline=[
            TimelineEvent(
                event_type=e[0],
                from_status=e[1],
                to_status=e[2],
                comment=e[3],
                attachments=e[4] if isinstance(e[4], list) else [],
                detail=e[5] if isinstance(e[5], dict) else {},
                created_by=e[6],
                create_time=e[7],
            )
            for e in events
        ],
    )

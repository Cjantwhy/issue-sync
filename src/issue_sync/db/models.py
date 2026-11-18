"""ORM models mirroring `db/schema.sql` exactly.

`db/schema.sql` is the source of truth; these models must not drift from it. The
integration test `tests/test_migrations.py` diffs them against a freshly migrated database.

Relationships use `lazy="raise"` so accidental implicit IO under asyncio fails loudly;
load related rows explicitly with `selectinload` / `joinedload`.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Matches PostgreSQL's own default names for the unnamed constraints in schema.sql,
# so names emitted by the ORM and names found in the database are identical.
NAMING_CONVENTION = {
    "pk": "%(table_name)s_pkey",
    "uq": "%(table_name)s_%(column_0_N_name)s_key",
    "fk": "%(table_name)s_%(column_0_name)s_fkey",
    "ck": "%(table_name)s_%(constraint_name)s_check",
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
}

PUSH_STATUSES = ("pending", "success", "failed")
HISTORY_EVENT_TYPES = ("created", "status_changed", "source_modified", "comment", "source_deleted")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _int_pk() -> Mapped[int]:
    return mapped_column(Integer, Identity(always=True), primary_key=True)


def _bigint_pk() -> Mapped[int]:
    return mapped_column(BigInteger, Identity(always=True), primary_key=True)


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def _updated() -> Mapped[datetime]:
    # Maintained by the set_update_time() trigger (db/extras.sql), not by the ORM.
    return mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        server_onupdate=func.now(),  # tells the ORM to expire the value after UPDATE
    )


# ---------- Constant tables ----------


class DocumentType(Base):
    __tablename__ = "document_type"

    id: Mapped[int] = _int_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)


class DocumentRecordStatus(Base):
    __tablename__ = "document_record_status"

    id: Mapped[int] = _int_pk()
    status_name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    is_terminal: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class AiJudgementType(Base):
    __tablename__ = "ai_judgement_type"

    id: Mapped[int] = _int_pk()
    type_name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)


class AiJudgementUrgency(Base):
    __tablename__ = "ai_judgement_urgency"

    id: Mapped[int] = _int_pk()
    urgency_level_name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)


class AssigneeRole(Base):
    __tablename__ = "assignee_role"

    id: Mapped[int] = _int_pk()
    role_name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)


# ---------- Source spreadsheets ----------


class Document(Base):
    __tablename__ = "document"
    __table_args__ = (
        UniqueConstraint("spreadsheet_id", "worksheet_name"),
        Index("idx_document_document_type", "document_type_id"),
    )

    id: Mapped[int] = _int_pk()
    document_type_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("document_type.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    spreadsheet_id: Mapped[str] = mapped_column(String(255), nullable=False)
    worksheet_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # predefined field name -> sheet header name
    mapping: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False)
    sync_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    create_time: Mapped[datetime] = _created()
    update_time: Mapped[datetime] = _updated()

    document_type: Mapped[DocumentType] = relationship(lazy="raise")
    records: Mapped[list["DocumentRecord"]] = relationship(back_populates="document", lazy="raise")


# ---------- Ticket records (current snapshot) ----------


class DocumentRecord(Base):
    __tablename__ = "document_record"
    __table_args__ = (
        Index("idx_record_document", "document_id"),
        Index("idx_record_status", "status_id"),
    )

    id: Mapped[int] = _bigint_pk()
    document_id: Mapped[int] = mapped_column(Integer, ForeignKey("document.id"), nullable=False)
    system_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        unique=True,
        server_default=text("gen_random_uuid()"),
    )

    # Owned by the spreadsheet
    title: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    reporter: Mapped[str | None] = mapped_column(String(255))
    contact: Mapped[str | None] = mapped_column(String(255))

    # Owned by the ticket system
    status_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("document_record_status.id"))
    current_priority: Mapped[str | None] = mapped_column(String(255))
    current_assignee: Mapped[str | None] = mapped_column(String(255))
    # The ticket system's account id of current_assignee (the name is for display).
    current_assignee_id: Mapped[str | None] = mapped_column(String(255))

    # Raw data and change detection
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    raw_hash: Mapped[str | None] = mapped_column(CHAR(64))
    sheet_written_hash: Mapped[str | None] = mapped_column(CHAR(64))
    sheet_written_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    create_time: Mapped[datetime] = _created()
    update_time: Mapped[datetime] = _updated()

    document: Mapped[Document] = relationship(back_populates="records", lazy="raise")
    status: Mapped[DocumentRecordStatus | None] = relationship(lazy="raise")
    history: Mapped[list["DocumentRecordHistory"]] = relationship(
        back_populates="record", lazy="raise", order_by="DocumentRecordHistory.create_time"
    )
    judgements: Mapped[list["AiJudgement"]] = relationship(
        back_populates="record", lazy="raise", order_by="AiJudgement.create_time"
    )
    ticket_sync: Mapped["TicketSync | None"] = relationship(
        back_populates="record", lazy="raise", uselist=False
    )


# ---------- Event log (append-only) ----------


class DocumentRecordHistory(Base):
    __tablename__ = "document_record_history"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('created', 'status_changed', 'source_modified', "
            "'comment', 'source_deleted')",
            name="event_type",
        ),
        Index("idx_history_record_time", "document_record_id", "create_time"),
    )

    id: Mapped[int] = _bigint_pk()
    document_record_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("document_record.id"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    from_status_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("document_record_status.id")
    )
    to_status_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("document_record_status.id")
    )
    comment: Mapped[str | None] = mapped_column(Text)
    # [{"name": "...", "url": "..."}]
    attachments: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    create_time: Mapped[datetime] = _created()

    record: Mapped[DocumentRecord] = relationship(back_populates="history", lazy="raise")


# ---------- AI judgements ----------


class AiJudgement(Base):
    __tablename__ = "ai_judgement"
    __table_args__ = (Index("idx_ai_judgement_record_time", "document_record_id", "create_time"),)

    id: Mapped[int] = _bigint_pk()
    document_record_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("document_record.id"), nullable=False
    )
    type_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("ai_judgement_type.id"), nullable=False
    )
    urgency_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("ai_judgement_urgency.id"), nullable=False
    )
    assignee_role_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("assignee_role.id"), nullable=False
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    create_time: Mapped[datetime] = _created()

    record: Mapped[DocumentRecord] = relationship(back_populates="judgements", lazy="raise")
    type: Mapped[AiJudgementType] = relationship(lazy="raise")
    urgency: Mapped[AiJudgementUrgency] = relationship(lazy="raise")
    assignee_role: Mapped[AssigneeRole] = relationship(lazy="raise")


# ---------- Ticket sync state ----------


class TicketSync(Base):
    __tablename__ = "ticket_sync"
    __table_args__ = (
        CheckConstraint("push_status IN ('pending', 'success', 'failed')", name="push_status"),
        Index(
            "idx_ticket_sync_unfinished",
            "push_status",
            postgresql_where=text("push_status <> 'success'"),
        ),
    )

    id: Mapped[int] = _bigint_pk()
    document_record_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("document_record.id"), nullable=False, unique=True
    )
    issue_key: Mapped[str | None] = mapped_column(String(50), unique=True)

    # Outbound to the ticket system
    push_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    pushed_hash: Mapped[str | None] = mapped_column(CHAR(64))
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # When the Slack notification for the new ticket was sent; NULL until then.
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Assignment applied when the ticket was created (AI judgement after the assignment
    # rules). NULL for linked tickets and tickets created before it was kept.
    assigned_urgency_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("ai_judgement_urgency.id")
    )
    assigned_role_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("assignee_role.id"))
    assignment_rule: Mapped[str | None] = mapped_column(String(255))  # matched rule's name

    # Inbound from the ticket system
    ticket_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    create_time: Mapped[datetime] = _created()
    update_time: Mapped[datetime] = _updated()

    record: Mapped[DocumentRecord] = relationship(back_populates="ticket_sync", lazy="raise")

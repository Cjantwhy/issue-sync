"""Outbound flow: database -> ticket system, one record per call.

The record's `ticket_sync` row is locked with `SELECT ... FOR UPDATE` for the whole push,
including the HTTP calls, so two pushes of the same record never overlap. That is what
makes "never open two tickets for one row" hold under concurrent jobs.

Before a record's ticket is created it is classified once (AI triage). The judgement,
adjusted by the assignment rules, sets the ticket's priority and assignee. If
classification keeps failing, the ticket is created without one rather than held back.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from issue_sync.adapter_config import AssignmentConfig
from issue_sync.classify.base import Classification, Classifier, ClassifierError, ClassifierInput
from issue_sync.db.models import (
    AiJudgement,
    AiJudgementType,
    AiJudgementUrgency,
    AssigneeRole,
    DocumentRecord,
    TicketSync,
)
from issue_sync.rules import Assignment, RequestFacts, decide
from issue_sync.sources.sheets.layout import content_hash
from issue_sync.sync.inbound import detail_url
from issue_sync.sync.ticket_events import apply_snapshot
from issue_sync.tickets.base import (
    TicketBackend,
    TicketBackendError,
    TicketContent,
    TicketDraft,
    TriageNote,
)

logger = logging.getLogger(__name__)

LAST_ERROR_MAX_LENGTH = 2000

Outcome = Literal["created", "linked", "updated", "unchanged", "failed", "missing"]


@dataclass(frozen=True)
class Triage:
    classifier: Classifier
    max_attempts: int = 3  # classification attempts before creating without a judgement


@dataclass(frozen=True)
class PushResult:
    outcome: Outcome
    issue_key: str | None = None
    error: Exception | None = None

    @property
    def retryable(self) -> bool:
        return bool(getattr(self.error, "retryable", False))

    @property
    def retry_after(self) -> float | None:
        return getattr(self.error, "retry_after", None)


def ticket_content(
    record: DocumentRecord, public_base_url: str, triage: TriageNote | None = None
) -> TicketContent:
    return TicketContent(
        title=record.title,
        description=record.description,
        reporter=record.reporter,
        contact=record.contact,
        detail_url=detail_url(public_base_url, record.system_id),
        triage=triage,
    )


async def push_record(
    sessionmaker: async_sessionmaker[AsyncSession],
    backend: TicketBackend,
    record_id: int,
    public_base_url: str,
    *,
    triage: Triage | None = None,
    rules: AssignmentConfig | None = None,
) -> PushResult:
    rules = rules or AssignmentConfig()
    async with sessionmaker() as session, session.begin():
        sync = await _locked_ticket_sync(session, record_id)
        record = await session.get(DocumentRecord, record_id)
        if record is None or sync is None:
            logger.warning("push skipped: record %s does not exist", record_id)
            return PushResult("missing")

        note = await latest_triage(session, record_id)
        if sync.issue_key is None and note is None and triage is not None:
            try:
                note = await _classify(session, record, triage.classifier)
            except ClassifierError as exc:
                if exc.retryable and sync.retry_count + 1 < triage.max_attempts:
                    return _failed(sync, record_id, exc)
                logger.warning(
                    "creating the ticket for record %s without AI triage (attempt %d): %s",
                    record_id,
                    sync.retry_count + 1,
                    exc,
                )

        content = ticket_content(record, public_base_url, note)
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
                    assignment = decide(rules, note, request_facts(record))
                    draft = TicketDraft(
                        system_id=record.system_id,
                        urgency=assignment.urgency,
                        assignee_role=assignment.assignee_role,
                        **content.model_dump(),
                    )
                    ref = await backend.create_ticket(draft)
                    outcome = "created"
                    await _record_assignment(session, sync, assignment)
                    await _explain_rule(backend, ref.key, assignment.explain(note))
                else:
                    await backend.update_content(ref.key, content)
                    outcome = "linked"
                sync.issue_key = ref.key
            else:
                await backend.update_content(sync.issue_key, content)
                outcome = "updated"
        except (TicketBackendError, ValueError) as exc:
            return _failed(sync, record_id, exc)

        if outcome in ("created", "linked"):
            await _baseline(session, backend, record, sync)
        sync.push_status = "success"
        sync.pushed_hash = pushed_hash
        sync.pushed_at = datetime.now(UTC)
        sync.last_error = None
        sync.retry_count = 0
        logger.info("record %s %s ticket %s", record_id, outcome, sync.issue_key)
        return PushResult(outcome, sync.issue_key)


def request_facts(record: DocumentRecord) -> RequestFacts:
    return RequestFacts(
        title=record.title, description=record.description, reporter=record.reporter
    )


async def _record_assignment(
    session: AsyncSession, sync: TicketSync, assignment: Assignment
) -> None:
    """Keep the assignment the ticket was created with, for the AI-vs-human comparison.

    Stored as decided at this moment: the rules may change later, and recomputing them
    would misattribute old tickets.
    """
    if assignment.urgency is not None:
        sync.assigned_urgency_id = await session.scalar(
            select(AiJudgementUrgency.id).where(
                AiJudgementUrgency.urgency_level_name == assignment.urgency
            )
        )
    if assignment.assignee_role is not None:
        sync.assigned_role_id = await session.scalar(
            select(AssigneeRole.id).where(AssigneeRole.role_name == assignment.assignee_role)
        )
    sync.assignment_rule = assignment.rule


async def _explain_rule(backend: TicketBackend, key: str, explanation: str | None) -> None:
    """Leave a comment when a rule overrode the AI, so the assignee knows why.

    A comment rather than part of the description: it records the decision made at
    creation and never changes, while the description is rebuilt on every push.
    Best effort: the ticket exists, so a failed comment must not fail the push.
    """
    if explanation is None:
        return
    try:
        await backend.add_comment(key, f"Assignment: {explanation}")
    except (TicketBackendError, ValueError) as exc:
        logger.warning("could not comment the assignment rule on %s: %s", key, exc)


async def _baseline(
    session: AsyncSession, backend: TicketBackend, record: DocumentRecord, sync: TicketSync
) -> None:
    """Read the new ticket's status, priority and assignee into the record.

    Gives the sheet an initial status right away and sets `ticket_updated_at`, the
    watermark that later webhook events are compared with. Best effort: the ticket
    exists, so a failure here must not fail the push; the next event fills it in.
    """
    assert sync.issue_key is not None
    try:
        snapshot = await backend.get_ticket(sync.issue_key)
    except (TicketBackendError, ValueError) as exc:
        logger.warning("could not read back %s after creating it: %s", sync.issue_key, exc)
        return
    await apply_snapshot(session, record, sync, snapshot)


def _failed(sync: TicketSync, record_id: int, exc: Exception) -> PushResult:
    sync.push_status = "failed"
    sync.last_error = f"{type(exc).__name__}: {exc}"[:LAST_ERROR_MAX_LENGTH]
    sync.retry_count += 1
    logger.warning("push of record %s failed (attempt %d): %s", record_id, sync.retry_count, exc)
    return PushResult("failed", sync.issue_key, exc)


async def latest_triage(session: AsyncSession, record_id: int) -> TriageNote | None:
    """The current judgement is the newest one (a record may be judged several times)."""
    row = (
        await session.execute(
            select(
                AiJudgementType.type_name,
                AiJudgementUrgency.urgency_level_name,
                AssigneeRole.role_name,
                AiJudgement.summary,
                AiJudgement.reason,
                AiJudgement.model,
            )
            .join(AiJudgementType, AiJudgementType.id == AiJudgement.type_id)
            .join(AiJudgementUrgency, AiJudgementUrgency.id == AiJudgement.urgency_id)
            .join(AssigneeRole, AssigneeRole.id == AiJudgement.assignee_role_id)
            .where(AiJudgement.document_record_id == record_id)
            .order_by(AiJudgement.create_time.desc(), AiJudgement.id.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    return TriageNote(
        type=row[0],
        urgency=row[1],
        assignee_role=row[2],
        summary=row[3],
        reason=row[4],
        model=row[5],
    )


async def _classify(
    session: AsyncSession, record: DocumentRecord, classifier: Classifier
) -> TriageNote:
    result = await classifier.classify(
        ClassifierInput(title=record.title, description=record.description)
    )
    await _store_judgement(session, record.id, result)
    j = result.judgement
    return TriageNote(
        type=j.type,
        urgency=j.urgency,
        assignee_role=j.assignee_role,
        summary=j.summary,
        reason=j.reason,
        model=result.model,
    )


async def _store_judgement(session: AsyncSession, record_id: int, result: Classification) -> None:
    j = result.judgement
    type_id = await session.scalar(
        select(AiJudgementType.id).where(AiJudgementType.type_name == j.type)
    )
    urgency_id = await session.scalar(
        select(AiJudgementUrgency.id).where(AiJudgementUrgency.urgency_level_name == j.urgency)
    )
    role_id = await session.scalar(
        select(AssigneeRole.id).where(AssigneeRole.role_name == j.assignee_role)
    )
    if type_id is None or urgency_id is None or role_id is None:
        # Startup checks make this unreachable; never store a half-resolved judgement.
        raise ClassifierError(f"judgement uses values missing from the database: {j}")
    session.add(
        AiJudgement(
            document_record_id=record_id,
            type_id=type_id,
            urgency_id=urgency_id,
            assignee_role_id=role_id,
            summary=j.summary,
            reason=j.reason,
            model=result.model,
        )
    )
    await session.flush()


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

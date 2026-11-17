"""arq worker: background jobs, retries and scheduled tasks.

Run with: arq issue_sync.worker.WorkerSettings

Jobs:
- `poll_documents` (cron): enqueue `sync_document` for every enabled worksheet.
- `sync_document`: worksheet -> database; enqueues `push_ticket` for pending records.
- `push_ticket`: AI triage and assignment rules (new records), then database -> ticket
  system for one record, retried with backoff. Enqueues `notify_ticket` for new tickets.
- `notify_ticket`: Slack message for a new ticket, sent once (ticket_sync.notified_at).
- `ticket_event`: a Jira webhook event -> database; enqueues `sync_document` to write
  the new status or latest note back to the sheet.
"""

import logging
from typing import Any, ClassVar

import httpx
from arq import Retry, cron, func
from arq.connections import RedisSettings
from sqlalchemy import select

from issue_sync.classify.openai_compat import create_classifier
from issue_sync.db.models import Document
from issue_sync.db.session import create_engine, create_sessionmaker
from issue_sync.log_setup import configure_logging
from issue_sync.notify.slack import SlackError, create_slack_client
from issue_sync.settings import Settings, get_settings
from issue_sync.sources.sheets.client import create_sheets_client
from issue_sync.sources.sheets.errors import SheetLayoutError, SheetsError
from issue_sync.startup_checks import run_startup_checks
from issue_sync.sync.inbound import InboundSync
from issue_sync.sync.notify import Notifier, notify_record
from issue_sync.sync.push import Triage, push_record
from issue_sync.sync.retry import poll_schedule, retry_delay
from issue_sync.sync.ticket_events import apply_ticket_event
from issue_sync.tickets.base import TicketBackendError
from issue_sync.tickets.jira.backend import JiraBackend, create_jira_client

logger = logging.getLogger(__name__)

SYNC_MAX_TRIES = 5
NOTIFY_MAX_TRIES = 5
TICKET_EVENT_MAX_TRIES = 5


async def enqueue_notify(ctx: dict[str, Any], record_id: int) -> None:
    # A fixed job id: arq ignores the enqueue while the job is queued, running or its
    # result is kept (1 hour), so the per-poll sweep cannot pile up duplicate jobs.
    await ctx["redis"].enqueue_job("notify_ticket", record_id, _job_id=f"notify:{record_id}")


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = create_engine(settings.database_url)
    ctx["engine"] = engine
    # Same fail-fast check as the api: the worker must not process jobs it cannot translate.
    configs = await run_startup_checks(settings, engine)
    sessionmaker = create_sessionmaker(engine)
    http = httpx.AsyncClient()
    ctx.update(
        settings=settings,
        adapter_configs=configs,
        sessionmaker=sessionmaker,
        http=http,
        ticket_backend=JiraBackend(create_jira_client(settings), configs.jira),
        triage=Triage(
            create_classifier(settings, configs.classifier, http),
            max_attempts=settings.llm_max_attempts,
        ),
        inbound=InboundSync(
            sessionmaker, create_sheets_client(settings, http), str(settings.public_base_url)
        ),
    )
    ctx["notifier"] = Notifier(
        slack=create_slack_client(settings, http),
        slack_config=configs.slack,
        rules=configs.assignment,
        ticket_url=ctx["ticket_backend"].ticket_url,
        public_base_url=str(settings.public_base_url),
    )


async def shutdown(ctx: dict[str, Any]) -> None:
    if (http := ctx.get("http")) is not None:
        await http.aclose()
    if (backend := ctx.get("ticket_backend")) is not None:
        await backend.aclose()
    if (engine := ctx.get("engine")) is not None:
        await engine.dispose()


async def ping(ctx: dict[str, Any]) -> str:
    """Trivial job to verify end-to-end that the worker consumes the queue."""
    return "pong"


async def poll_documents(ctx: dict[str, Any]) -> int:
    """Safety net for missed Apps Script pushes: sync every enabled worksheet."""
    async with ctx["sessionmaker"]() as session:
        ids = list(
            await session.scalars(
                select(Document.id).where(Document.sync_enabled).order_by(Document.id)
            )
        )
    for document_id in ids:
        await ctx["redis"].enqueue_job("sync_document", document_id)
    return len(ids)


async def sync_document(ctx: dict[str, Any], document_id: int) -> dict[str, int] | None:
    try:
        report = await ctx["inbound"].sync_document(document_id)
    except SheetsError as exc:
        delay = retry_delay(
            retryable=exc.retryable,
            attempt=ctx["job_try"],
            max_attempts=SYNC_MAX_TRIES,
            retry_after=exc.retry_after,
        )
        if delay is not None:
            raise Retry(defer=delay) from exc
        # The next poll tries again; nothing is lost by giving up on this run.
        logger.error("sync of document %s failed: %s", document_id, exc)
        return None
    except SheetLayoutError as exc:
        logger.error("sync of document %s stopped: %s", document_id, exc)
        return None
    if report is None:
        return None
    for record_id in report.pending_record_ids:
        await ctx["redis"].enqueue_job("push_ticket", record_id)
    for record_id in report.unnotified_record_ids:
        await enqueue_notify(ctx, record_id)
    return {
        "created": report.created,
        "modified": report.modified,
        "pushes_enqueued": len(report.pending_record_ids),
        "notifications_enqueued": len(report.unnotified_record_ids),
    }


async def push_ticket(ctx: dict[str, Any], record_id: int) -> str:
    settings: Settings = ctx["settings"]
    result = await push_record(
        ctx["sessionmaker"],
        ctx["ticket_backend"],
        record_id,
        str(settings.public_base_url),
        triage=ctx["triage"],
        rules=ctx["adapter_configs"].assignment,
    )
    if result.outcome in ("created", "linked"):
        await enqueue_notify(ctx, record_id)
    if result.outcome != "failed":
        return result.outcome
    delay = retry_delay(
        retryable=result.retryable,
        attempt=ctx["job_try"],
        max_attempts=settings.push_max_attempts,
        retry_after=result.retry_after,
    )
    if delay is not None:
        raise Retry(defer=delay)
    # Left as push_status=failed with last_error; reconciliation (milestone 7) alerts on it.
    logger.error(
        "giving up on pushing record %s after %d attempt(s): %s",
        record_id,
        ctx["job_try"],
        result.error,
    )
    return result.outcome


async def notify_ticket(ctx: dict[str, Any], record_id: int) -> str:
    try:
        return await notify_record(ctx["sessionmaker"], ctx["notifier"], record_id)
    except SlackError as exc:
        delay = retry_delay(
            retryable=exc.retryable,
            attempt=ctx["job_try"],
            max_attempts=NOTIFY_MAX_TRIES,
            retry_after=exc.retry_after,
        )
        if delay is not None:
            raise Retry(defer=delay) from exc
        # notified_at stays NULL: the poll's sweep re-enqueues it (at most hourly).
        logger.error("Slack notification for record %s failed: %s", record_id, exc)
        return "failed"


async def ticket_event(ctx: dict[str, Any], payload: dict[str, Any]) -> str:
    try:
        event = await ctx["ticket_backend"].parse_webhook(payload)
    except TicketBackendError as exc:  # loading the project's statuses failed
        delay = retry_delay(
            retryable=exc.retryable,
            attempt=ctx["job_try"],
            max_attempts=TICKET_EVENT_MAX_TRIES,
            retry_after=exc.retry_after,
        )
        if delay is not None:
            raise Retry(defer=delay) from exc
        logger.error("could not parse a Jira event: %s", exc)
        return "failed"
    if event is None:
        return "ignored"
    result = await apply_ticket_event(ctx["sessionmaker"], event)
    if result.sheet_changed and result.document_id is not None:
        await ctx["redis"].enqueue_job("sync_document", result.document_id)
    logger.info("Jira %s on %s: %s", event.kind, event.key, result.outcome)
    return result.outcome


class WorkerSettings:
    functions: ClassVar[list[Any]] = [
        ping,
        func(sync_document, max_tries=SYNC_MAX_TRIES),
        func(push_ticket, max_tries=get_settings().push_max_attempts),
        func(notify_ticket, max_tries=NOTIFY_MAX_TRIES),
        func(ticket_event, max_tries=TICKET_EVENT_MAX_TRIES),
    ]
    cron_jobs: ClassVar[list[Any]] = [
        cron(
            poll_documents,
            run_at_startup=True,
            **poll_schedule(get_settings().sheets_poll_interval_seconds),  # type: ignore[arg-type]
        )
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)

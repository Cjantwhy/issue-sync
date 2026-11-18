"""Inbound webhooks.

`POST /webhooks/sheets` is called by the Apps Script trigger in `apps_script/Code.gs`.
The payload only says *which* worksheet changed; its contents are never trusted. The
handler enqueues a sync job, and the job re-reads the worksheet through the API.

`POST /webhooks/jira` is called by a Jira admin webhook. The HMAC signature is checked
over the raw body, events from the service account are dropped, and the rest are queued
for the worker, so Jira gets its answer quickly and never times out and re-sends.
"""

import hmac
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from issue_sync.db.models import Document
from issue_sync.tickets.jira.webhook import (
    SIGNATURE_HEADER,
    SUPPORTED_EVENTS,
    actor_id,
    slim_payload,
    valid_signature,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks")

SECRET_HEADER = "X-Issue-Sync-Secret"

# (spreadsheet_id, worksheet_name) -> (document id, sync_enabled), or None if unknown
DocumentFinder = Callable[[str, str], Awaitable[tuple[int, bool] | None]]
# (document id, row a form submission just wrote or None)
Enqueue = Callable[[int, int | None], Awaitable[None]]
EnqueueEvent = Callable[[dict[str, Any]], Awaitable[None]]

MAX_JIRA_BODY_BYTES = 2_000_000


class SheetsEvent(BaseModel):
    spreadsheet_id: str = Field(min_length=1, max_length=255)
    worksheet_name: str = Field(min_length=1, max_length=255)
    # "form": a form submission wrote `row` in one go, so it need not settle.
    source: Literal["edit", "change", "form"] = "edit"
    row: int | None = Field(default=None, ge=2, le=10_000_000)

    @property
    def form_row(self) -> int | None:
        return self.row if self.source == "form" else None


class WebhookResponse(BaseModel):
    status: Literal["queued", "ignored"]


def require_sheets_secret(
    request: Request,
    secret: Annotated[str | None, Header(alias=SECRET_HEADER)] = None,
) -> None:
    expected = request.app.state.settings.sheets_webhook_secret.get_secret_value()
    # Constant-time comparison: the response time must not reveal how much matched.
    if secret is None or not hmac.compare_digest(secret.encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing secret")


def get_document_finder(request: Request) -> DocumentFinder:
    sessionmaker = request.app.state.sessionmaker

    async def find(spreadsheet_id: str, worksheet_name: str) -> tuple[int, bool] | None:
        async with sessionmaker() as session:
            row = (
                await session.execute(
                    select(Document.id, Document.sync_enabled).where(
                        Document.spreadsheet_id == spreadsheet_id,
                        Document.worksheet_name == worksheet_name,
                    )
                )
            ).first()
        return (row.id, row.sync_enabled) if row else None

    return find


def get_sync_enqueuer(request: Request) -> Enqueue:
    redis = request.app.state.redis

    async def enqueue(document_id: int, form_row: int | None) -> None:
        await redis.enqueue_job("sync_document", document_id, form_row)

    return enqueue


@router.post(
    "/sheets",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_sheets_secret)],
)
async def sheets_webhook(
    event: SheetsEvent,
    find_document: Annotated[DocumentFinder, Depends(get_document_finder)],
    enqueue_sync: Annotated[Enqueue, Depends(get_sync_enqueuer)],
) -> WebhookResponse:
    found = await find_document(event.spreadsheet_id, event.worksheet_name)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "worksheet is not registered")
    document_id, enabled = found
    if not enabled:
        return WebhookResponse(status="ignored")
    await enqueue_sync(document_id, event.form_row)
    return WebhookResponse(status="queued")


def get_service_account_id(request: Request) -> str:
    return request.app.state.adapter_configs.jira.service_account_id


def get_ticket_event_enqueuer(request: Request) -> EnqueueEvent:
    redis = request.app.state.redis

    async def enqueue(payload: dict[str, Any]) -> None:
        await redis.enqueue_job("ticket_event", payload)

    return enqueue


@router.post("/jira", status_code=status.HTTP_202_ACCEPTED)
async def jira_webhook(
    request: Request,
    service_account_id: Annotated[str, Depends(get_service_account_id)],
    enqueue_event: Annotated[EnqueueEvent, Depends(get_ticket_event_enqueuer)],
) -> WebhookResponse:
    body = await request.body()
    if len(body) > MAX_JIRA_BODY_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "payload too large")
    secret = request.app.state.settings.jira_webhook_secret.get_secret_value()
    if not valid_signature(body, request.headers.get(SIGNATURE_HEADER), secret):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing signature")
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body is not JSON") from None
    if not isinstance(payload, dict) or payload.get("webhookEvent") not in SUPPORTED_EVENTS:
        return WebhookResponse(status="ignored")
    if actor_id(payload) == service_account_id:
        # The system's own change coming back: the first loop guard.
        return WebhookResponse(status="ignored")
    await enqueue_event(slim_payload(payload))
    return WebhookResponse(status="queued")

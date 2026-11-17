"""Inbound webhooks.

`POST /webhooks/sheets` is called by the Apps Script trigger in `apps_script/Code.gs`.
The payload only says *which* worksheet changed; its contents are never trusted. The
handler enqueues a sync job, and the job re-reads the worksheet through the API.
"""

import hmac
import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from issue_sync.db.models import Document

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks")

SECRET_HEADER = "X-Issue-Sync-Secret"

# (spreadsheet_id, worksheet_name) -> (document id, sync_enabled), or None if unknown
DocumentFinder = Callable[[str, str], Awaitable[tuple[int, bool] | None]]
Enqueue = Callable[[int], Awaitable[None]]


class SheetsEvent(BaseModel):
    spreadsheet_id: str = Field(min_length=1, max_length=255)
    worksheet_name: str = Field(min_length=1, max_length=255)


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

    async def enqueue(document_id: int) -> None:
        await redis.enqueue_job("sync_document", document_id)

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
    await enqueue_sync(document_id)
    return WebhookResponse(status="queued")

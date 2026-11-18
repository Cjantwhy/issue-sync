"""Ticket backend interface.

The sync flow talks to the ticket system only through `TicketBackend`, using internal
values (status names, urgency levels, assignee roles). Translating those into a specific
system's vocabulary is the backend's job. Jira Cloud is the only implementation; this
layer exists to keep Jira details out of the business flow and to make it easy to fake
in tests, not to anticipate other backends.
"""

import uuid
from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class TriageNote(_Model):
    """The AI judgement, shown to the assignee in the ticket. Internal values."""

    type: str
    urgency: str
    assignee_role: str
    summary: str
    reason: str
    model: str


class TicketContent(_Model):
    """Fields this system owns in the ticket. Pushed on create and on every sheet-side change."""

    title: str | None
    description: str | None
    reporter: str | None
    contact: str | None
    detail_url: str
    # None: the record has no AI judgement (classification failed, or it predates AI triage).
    triage: TriageNote | None = None
    # Links from the sheet's attachments column (e.g. Google Drive files), in order.
    links: tuple[str, ...] = ()


class TicketDraft(TicketContent):
    """Everything needed to open a new ticket."""

    system_id: uuid.UUID
    # Internal values; None leaves the ticket system's default in place.
    urgency: str | None = None
    assignee_role: str | None = None


class TicketRef(_Model):
    key: str
    url: str


class TicketSnapshot(_Model):
    """Current ticket-owned state, as the ticket system reports it."""

    key: str
    url: str
    external_status: str
    status: str | None  # internal status, None if the external status is not mapped
    priority: str | None  # the ticket system's priority name
    assignee_id: str | None
    assignee_name: str | None
    updated_at: datetime


class TicketState(_Model):
    """Ticket-owned fields as of one event."""

    external_status: str
    status: str | None  # internal status, None if the external status is not mapped
    priority: str | None
    assignee_id: str | None
    assignee_name: str | None


class TicketComment(_Model):
    id: str
    author_id: str | None
    author_name: str | None
    text: str
    created_at: datetime


class TicketAttachment(_Model):
    """A file added to the ticket. `url` opens it for people signed in to the ticket system."""

    id: str
    filename: str
    url: str
    author_id: str | None
    author_name: str | None
    created_at: datetime
    size: int | None = None
    mime_type: str | None = None


class TicketReport(TicketSnapshot):
    """Everything reconciliation compares: state, owned content and comments."""

    system_id: uuid.UUID  # from the ticket's issue-sync label
    summary: str
    description_text: str  # the description as plain text
    comments: list[TicketComment]
    attachments: list[TicketAttachment] = []


class TicketEvent(_Model):
    """A change reported by the ticket system's webhook, in internal values."""

    kind: Literal["issue_updated", "comment_created"]
    key: str
    occurred_at: datetime
    actor_id: str | None
    actor_name: str | None
    # Made by this system's own account: ignored, or the sync would loop.
    from_service_account: bool
    # issue_updated: the full state at `occurred_at` (not just what changed), so events
    # applied out of order still converge on the newest state.
    state: TicketState | None = None
    comment: TicketComment | None = None
    # issue_updated: the ticket's attachments as the event reports them. Logged once
    # each (by id), whether or not the event is stale.
    attachments: list[TicketAttachment] = []


# ---------- Errors ----------


class TicketBackendError(Exception):
    """Base class. `retryable` tells the job runner whether retrying can help."""

    retryable: bool = False

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TicketBackendUnavailable(TicketBackendError):
    """Network failure, timeout, rate limit or server error: retry with backoff."""

    retryable = True


class TicketAuthError(TicketBackendError):
    """Credentials rejected or missing permission. Needs an operator, not a retry."""


class TicketRequestRejected(TicketBackendError):
    """The ticket system refused the request as invalid (e.g. a field not on the screen)."""


class TicketNotFound(TicketBackendError):
    """The ticket does not exist, or the account cannot see it."""


class TransitionNotAvailable(TicketBackendError):
    """The workflow has no transition from the current status to the requested one."""


class DuplicateTickets(TicketBackendError):
    """More than one ticket is tagged with the same system_id. Needs an operator."""


# ---------- Interface ----------


class TicketBackend(Protocol):
    async def create_ticket(self, draft: TicketDraft) -> TicketRef:
        """Open a ticket tagged with `draft.system_id`.

        Not idempotent by itself: if a create call fails with a retryable error, the
        ticket may still have been created. Callers must call `find_by_system_id`
        before retrying a create.
        """
        ...

    async def find_by_system_id(self, system_id: uuid.UUID) -> TicketRef | None:
        """Find the ticket tagged with `system_id`. Raises DuplicateTickets if several are."""
        ...

    async def update_content(self, key: str, content: TicketContent) -> None:
        """Overwrite the spreadsheet-owned fields (title, description)."""
        ...

    async def transition(self, key: str, status: str) -> bool:
        """Move the ticket to an internal status. Returns False if it was already there."""
        ...

    async def add_comment(self, key: str, text: str) -> None: ...

    async def get_ticket(self, key: str) -> TicketSnapshot: ...

    async def find_tickets(self, system_ids: list[uuid.UUID]) -> list[TicketReport]:
        """Current state of the tickets tagged with these system_ids (missing ones omitted)."""
        ...

    def content_matches(self, content: TicketContent, ticket: TicketReport) -> bool:
        """Whether the ticket still shows exactly the content this system pushed."""
        ...

    async def parse_webhook(self, payload: dict[str, Any]) -> TicketEvent | None:
        """Translate a webhook payload. None for events this system does not track."""
        ...

    def ticket_url(self, key: str) -> str:
        """Link to the ticket for people (e.g. in notifications)."""
        ...

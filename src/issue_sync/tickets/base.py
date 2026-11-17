"""Ticket backend interface.

The sync flow talks to the ticket system only through `TicketBackend`, using internal
values (status names, urgency levels, assignee roles). Translating those into a specific
system's vocabulary is the backend's job. Jira Cloud is the only implementation; this
layer exists to keep Jira details out of the business flow and to make it easy to fake
in tests, not to anticipate other backends.
"""

import uuid
from datetime import datetime
from typing import Protocol

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

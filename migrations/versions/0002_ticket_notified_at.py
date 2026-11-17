"""Add ticket_sync.notified_at: when the Slack notification for a new ticket was sent.

Tickets that already exist are marked as notified, so deploying this revision does not
send a burst of notifications for old tickets.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ticket_sync", sa.Column("notified_at", sa.DateTime(timezone=True)))
    op.execute(
        "UPDATE ticket_sync SET notified_at = COALESCE(pushed_at, now()) "
        "WHERE issue_key IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("ticket_sync", "notified_at")

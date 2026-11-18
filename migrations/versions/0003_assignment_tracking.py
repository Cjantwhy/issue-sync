"""Keep what the dashboard compares the AI judgement with.

- document_record.current_assignee_id: the ticket assignee's account id. The display name
  alone cannot be matched against the account ids that roles map to.
- ticket_sync.assigned_urgency_id / assigned_role_id / assignment_rule: the assignment
  applied when the ticket was created (the AI judgement after the assignment rules).

Existing rows stay NULL. Rule results are not reconstructed from today's rules, which may
differ from the ones in force when the ticket was created. Assignee ids are filled in by
the next ticket event or reconciliation run.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("document_record", sa.Column("current_assignee_id", sa.String(255)))
    op.add_column(
        "ticket_sync",
        sa.Column(
            "assigned_urgency_id",
            sa.Integer,
            sa.ForeignKey("ai_judgement_urgency.id", name="ticket_sync_assigned_urgency_id_fkey"),
        ),
    )
    op.add_column(
        "ticket_sync",
        sa.Column(
            "assigned_role_id",
            sa.Integer,
            sa.ForeignKey("assignee_role.id", name="ticket_sync_assigned_role_id_fkey"),
        ),
    )
    op.add_column("ticket_sync", sa.Column("assignment_rule", sa.String(255)))


def downgrade() -> None:
    op.drop_column("ticket_sync", "assignment_rule")
    op.drop_column("ticket_sync", "assigned_role_id")
    op.drop_column("ticket_sync", "assigned_urgency_id")
    op.drop_column("document_record", "current_assignee_id")

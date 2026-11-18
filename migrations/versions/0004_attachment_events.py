"""Allow 'attachment' history events: a file added to the ticket without a comment.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "document_record_history_event_type_check"
BEFORE = "'created', 'status_changed', 'source_modified', 'comment', 'source_deleted'"
AFTER = (
    "'created', 'status_changed', 'source_modified', 'comment', 'attachment', 'source_deleted'"
)


def _replace_check(values: str) -> None:
    op.drop_constraint(op.f(CONSTRAINT), "document_record_history", type_="check")
    op.create_check_constraint(
        op.f(CONSTRAINT), "document_record_history", f"event_type IN ({values})"
    )


def upgrade() -> None:
    _replace_check(AFTER)


def downgrade() -> None:
    # Attachment events cannot be expressed in the old schema; they are removed.
    op.execute("DELETE FROM document_record_history WHERE event_type = 'attachment'")
    _replace_check(BEFORE)

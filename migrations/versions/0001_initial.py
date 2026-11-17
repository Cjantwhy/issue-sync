"""Initial schema: run db/schema.sql then db/extras.sql verbatim.

The SQL files are the source of truth for the schema; this migration does not
re-describe them with op.create_table().

Revision ID: 0001
Revises:
Create Date: 2026-11-16
"""

from collections.abc import Sequence
from pathlib import Path

from alembic import context, op
from sqlalchemy.util import await_only

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DB_DIR = Path(__file__).resolve().parents[2] / "db"

# Reverse dependency order.
TABLES = (
    "ticket_sync",
    "ai_judgement",
    "document_record_history",
    "document_record",
    "document",
    "assignee_role",
    "ai_judgement_urgency",
    "ai_judgement_type",
    "document_record_status",
    "document_type",
)


def _execute_script(sql: str) -> None:
    """Run a multi-statement SQL script inside the current migration transaction."""
    if context.is_offline_mode():
        op.execute(sql)
        return
    # SQLAlchemy's asyncpg dialect uses prepared statements, which allow only one
    # statement each. asyncpg's Connection.execute() without arguments uses the simple
    # query protocol, which accepts a full script (including the $$-quoted function body).
    # The call runs on the same connection, so it joins the migration transaction.
    driver_conn = op.get_bind().connection.driver_connection
    await_only(driver_conn.execute(sql))


def upgrade() -> None:
    _execute_script((DB_DIR / "schema.sql").read_text(encoding="utf-8"))
    _execute_script((DB_DIR / "extras.sql").read_text(encoding="utf-8"))


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table}")
    op.execute("DROP FUNCTION IF EXISTS set_update_time()")

"""Alembic environment (async engine, asyncpg).

Concurrency: migrations are normally run once by the one-shot `migrate` compose service.
As a second line of defence, every run takes a PostgreSQL transaction-level advisory lock
before reading the current revision, so two concurrent `alembic upgrade` runs serialize:
the second one waits, then sees the database is already at head and does nothing.
"""

import asyncio
import logging.config

from alembic import context
from sqlalchemy import Connection, pool, text
from sqlalchemy.ext.asyncio import create_async_engine

from issue_sync.db.models import Base
from issue_sync.settings import DatabaseSettings

# Arbitrary constant identifying "issue-sync schema migration" in pg_locks.
MIGRATION_LOCK_KEY = 7_303_011_502

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    logging.config.fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    # Programmatic callers (tests) pass the URL via attributes; otherwise use DATABASE_URL.
    url = config.attributes.get("database_url")
    return url or DatabaseSettings().database_url  # type: ignore[call-arg]


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        # Taken inside the migration transaction and released on commit/rollback.
        # Must happen before run_migrations() reads alembic_version.
        connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": MIGRATION_LOCK_KEY})
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_database_url(), poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())

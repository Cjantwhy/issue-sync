"""Throwaway PostgreSQL databases for integration tests.

Set TEST_DATABASE_URL to a server where the tests may create and drop databases, e.g.
postgresql+asyncpg://issue_sync:change-me@localhost:5432/postgres. Each test module run
creates a uniquely named database and drops it afterwards, so no existing data is
touched. Tests using these fixtures are skipped when TEST_DATABASE_URL is unset.
"""

import asyncio
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import make_url, pool, text
from sqlalchemy.ext.asyncio import create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

integration = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set"),
]


async def _admin_execute(sql: str) -> None:
    assert TEST_DATABASE_URL
    engine = create_async_engine(
        TEST_DATABASE_URL, isolation_level="AUTOCOMMIT", poolclass=pool.NullPool
    )
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


@contextmanager
def throwaway_database() -> Iterator[str]:
    assert TEST_DATABASE_URL
    name = f"issue_sync_test_{uuid.uuid4().hex[:12]}"
    asyncio.run(_admin_execute(f'CREATE DATABASE "{name}"'))
    try:
        yield make_url(TEST_DATABASE_URL).set(database=name).render_as_string(hide_password=False)
    finally:
        asyncio.run(_admin_execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


def alembic_config(database_url: str) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.attributes["database_url"] = database_url
    config.attributes["configure_logger"] = False
    return config

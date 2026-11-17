"""Migration tests against a real PostgreSQL server (see tests/db_fixtures.py)."""

import asyncio
import os
import subprocess
import sys
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import create_async_engine

from issue_sync.adapter_config import load_adapter_configs
from issue_sync.db.models import Base
from issue_sync.startup_checks import check_mappings, load_internal_constants
from tests.db_fixtures import REPO_ROOT, alembic_config, integration, throwaway_database

CONFIG_DIR = REPO_ROOT / "config"

pytestmark = integration


@pytest.fixture(scope="module")
def database_url() -> Iterator[str]:
    with throwaway_database() as url:
        yield url


async def _run_sync_on(database_url: str, fn):  # type: ignore[no-untyped-def]
    engine = create_async_engine(database_url, poolclass=pool.NullPool)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(fn)
    finally:
        await engine.dispose()


def _schema_diff(database_url: str) -> list[object]:
    def diff(sync_conn):  # type: ignore[no-untyped-def]
        ctx = MigrationContext.configure(sync_conn, opts={"compare_type": True})
        return compare_metadata(ctx, Base.metadata)

    return asyncio.run(_run_sync_on(database_url, diff))


def _table_names(database_url: str) -> set[str]:
    async def fetch() -> set[str]:
        engine = create_async_engine(database_url, poolclass=pool.NullPool)
        try:
            async with engine.connect() as conn:
                rows = await conn.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                )
                return {r[0] for r in rows}
        finally:
            await engine.dispose()

    return asyncio.run(fetch())


def _current_revision(database_url: str) -> str | None:
    return asyncio.run(
        _run_sync_on(database_url, lambda c: MigrationContext.configure(c).get_current_revision())
    )


def test_concurrent_upgrades_serialize(database_url: str) -> None:
    # Two `alembic upgrade head` processes racing on an empty database, like two migrate
    # containers. The advisory lock makes the second wait and then find nothing to do,
    # instead of failing on CREATE TABLE. Separate processes are required: alembic's
    # `context`/`op` are process-global, so in-process threads would corrupt each other.
    env = {**os.environ, "DATABASE_URL": database_url}
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for _ in range(2)
    ]
    for proc in procs:
        output, _ = proc.communicate(timeout=120)
        assert proc.returncode == 0, output
    assert _current_revision(database_url) == "0001"


def test_orm_models_match_migrated_schema(database_url: str) -> None:
    command.upgrade(alembic_config(database_url), "head")
    assert _schema_diff(database_url) == []


def test_example_configs_cover_seeded_constants(database_url: str) -> None:
    command.upgrade(alembic_config(database_url), "head")

    async def load():  # type: ignore[no-untyped-def]
        engine = create_async_engine(database_url, poolclass=pool.NullPool)
        try:
            return await load_internal_constants(engine)
        finally:
            await engine.dispose()

    constants = asyncio.run(load())
    configs = load_adapter_configs(
        CONFIG_DIR / "jira.example.yaml", CONFIG_DIR / "slack.example.yaml"
    )
    report = check_mappings(constants, configs)
    assert report.ok, report.missing
    assert report.unknown == []


def test_downgrade_then_upgrade_round_trip(database_url: str) -> None:
    config = alembic_config(database_url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    assert _table_names(database_url) == {"alembic_version"}
    command.upgrade(config, "head")
    assert "ticket_sync" in _table_names(database_url)
    assert _schema_diff(database_url) == []

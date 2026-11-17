"""FastAPI application factory.

Run with: uvicorn issue_sync.api.app:create_app --factory
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI

from issue_sync.api import health
from issue_sync.db.session import create_engine, create_sessionmaker
from issue_sync.log_setup import configure_logging
from issue_sync.settings import Settings, get_settings
from issue_sync.startup_checks import run_startup_checks


def _lifespan(settings: Settings):
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level)
        engine = create_engine(settings.database_url)
        try:
            # Fail fast: refuse to serve if adapter configs do not cover the database.
            app.state.adapter_configs = await run_startup_checks(settings, engine)
            app.state.engine = engine
            app.state.sessionmaker = create_sessionmaker(engine)
            # ArqRedis is a redis.asyncio.Redis: used for health pings and, later, enqueuing jobs.
            app.state.redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
            try:
                yield
            finally:
                await app.state.redis.aclose()
        finally:
            await engine.dispose()

    return lifespan


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="issue-sync", lifespan=_lifespan(settings))
    app.include_router(health.router)
    return app

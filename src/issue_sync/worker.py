"""arq worker: background jobs, retries and scheduled tasks.

Run with: arq issue_sync.worker.WorkerSettings
"""

import logging
from typing import Any, ClassVar

from arq.connections import RedisSettings

from issue_sync.db.session import create_engine, create_sessionmaker
from issue_sync.log_setup import configure_logging
from issue_sync.settings import get_settings
from issue_sync.startup_checks import run_startup_checks

logger = logging.getLogger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = create_engine(settings.database_url)
    ctx["engine"] = engine
    # Same fail-fast check as the api: the worker must not process jobs it cannot translate.
    ctx["adapter_configs"] = await run_startup_checks(settings, engine)
    ctx["sessionmaker"] = create_sessionmaker(engine)


async def shutdown(ctx: dict[str, Any]) -> None:
    engine = ctx.get("engine")
    if engine is not None:
        await engine.dispose()


async def ping(ctx: dict[str, Any]) -> str:
    """Trivial job to verify end-to-end that the worker consumes the queue."""
    return "pong"


class WorkerSettings:
    functions: ClassVar[list[Any]] = [ping]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)

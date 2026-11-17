import asyncio
import logging
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel
from redis.asyncio import Redis

from issue_sync.db.session import ping_database

logger = logging.getLogger(__name__)

router = APIRouter()

PROBE_TIMEOUT_SECONDS = 2.0

Probe = Callable[[], Awaitable[object]]
CheckResult = Literal["ok", "error"]


class HealthResponse(BaseModel):
    status: Literal["ok", "unavailable"]
    checks: dict[str, CheckResult]


async def ping_redis(redis: Redis) -> None:
    await redis.ping()


def get_probes(request: Request) -> dict[str, Probe]:
    state = request.app.state
    return {
        "database": partial(ping_database, state.engine),
        "redis": partial(ping_redis, state.redis),
    }


async def _run_probe(name: str, probe: Probe) -> CheckResult:
    try:
        await asyncio.wait_for(probe(), timeout=PROBE_TIMEOUT_SECONDS)
    except Exception:
        # Details go to the log only; /health is unauthenticated.
        logger.exception("Health probe %r failed", name)
        return "error"
    return "ok"


@router.get("/health", response_model=HealthResponse)
async def health(
    response: Response, probes: Annotated[dict[str, Probe], Depends(get_probes)]
) -> HealthResponse:
    names = list(probes)
    results = await asyncio.gather(*(_run_probe(n, probes[n]) for n in names))
    checks = dict(zip(names, results, strict=True))
    healthy = all(r == "ok" for r in results)
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(status="ok" if healthy else "unavailable", checks=checks)

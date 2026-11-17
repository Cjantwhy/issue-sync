import asyncio
from collections.abc import Awaitable, Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from issue_sync.api import health
from issue_sync.api.app import create_app
from tests.settings_factory import make_settings


async def _ok() -> None:
    return None


async def _fail() -> None:
    raise ConnectionError("connection refused")


async def _hang() -> None:
    await asyncio.sleep(10)


@pytest.fixture
def app() -> FastAPI:
    settings = make_settings()
    return create_app(settings)


def _client(app: FastAPI, **probes: Callable[[], Awaitable[None]]) -> TestClient:
    app.dependency_overrides[health.get_probes] = lambda: probes
    # Not entered as a context manager, so the lifespan (real DB/Redis) does not run.
    return TestClient(app)


def test_all_healthy(app: FastAPI) -> None:
    response = _client(app, database=_ok, redis=_ok).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"database": "ok", "redis": "ok"}}


@pytest.mark.parametrize("broken", ["database", "redis"])
def test_one_dependency_down_returns_503(app: FastAPI, broken: str) -> None:
    probes = {"database": _ok, "redis": _ok, broken: _fail}
    response = _client(app, **probes).get("/health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["checks"][broken] == "error"
    assert "connection refused" not in response.text  # internals stay in the log


def test_hanging_probe_times_out(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "PROBE_TIMEOUT_SECONDS", 0.05)
    response = _client(app, database=_hang, redis=_ok).get("/health")
    assert response.status_code == 503
    assert response.json()["checks"] == {"database": "error", "redis": "ok"}

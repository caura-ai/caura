"""/readyz is ready only when the database the role serves from answers (M-59).

/readyz checked only that the shared secret was configured. On the reader role
nothing at startup touches the database either, and both engines are created
lazily, so a reader whose DSN was unreachable or mistyped passed /readyz, went
into rotation, and failed every data request with a 500. On every role, a
database lost after boot never failed it. /healthz stays the shallow liveness
probe.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import create_async_engine

from core_storage_api.app import create_app
from core_storage_api.config import settings
from core_storage_api.database import init as db_init
from core_storage_api.routers import health

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

# Nothing listens on port 1, so a connect is refused at once.
_UNREACHABLE = "postgresql+asyncpg://caura:caura@127.0.0.1:1/caura"


async def _probe(path: str = "/readyz") -> httpx.Response:
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
        return await client.get(path)


@pytest.fixture
async def unreachable():
    engine = create_async_engine(_UNREACHABLE)
    yield engine
    await engine.dispose()


async def test_ready_when_the_database_answers() -> None:
    resp = await _probe()
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.parametrize("role", ["writer", "hybrid"])
async def test_not_ready_when_the_database_is_unreachable(monkeypatch, unreachable, role) -> None:
    monkeypatch.setattr(settings, "core_storage_role", role)
    monkeypatch.setattr(db_init, "_engine", unreachable)

    resp = await _probe()

    assert resp.status_code == 503
    assert resp.json() == {"detail": "database unavailable"}
    # Liveness stays shallow: the process is up, so it is not restarted.
    assert (await _probe("/healthz")).status_code == 200


async def test_a_reader_checks_the_read_database(monkeypatch, unreachable) -> None:
    monkeypatch.setattr(settings, "core_storage_role", "reader")
    monkeypatch.setattr(settings, "read_database_url", SecretStr(_UNREACHABLE))
    monkeypatch.setattr(db_init, "_read_engine", unreachable)

    resp = await _probe()

    assert resp.status_code == 503


async def test_a_writer_does_not_check_the_read_database(monkeypatch, unreachable) -> None:
    """Control: the probe follows the role's own engine."""
    monkeypatch.setattr(settings, "core_storage_role", "writer")
    monkeypatch.setattr(settings, "read_database_url", SecretStr(_UNREACHABLE))
    monkeypatch.setattr(db_init, "_read_engine", unreachable)

    resp = await _probe()

    assert resp.status_code == 200


class _HangingEngine:
    """A database that accepts the connection and never answers, on an idle pool."""

    def __init__(self, pool):
        self.pool = pool

    @asynccontextmanager
    async def connect(self):
        await asyncio.sleep(60)
        yield


async def test_a_database_that_hangs_fails_within_the_bound(monkeypatch, unreachable) -> None:
    monkeypatch.setattr(settings, "core_storage_role", "hybrid")
    monkeypatch.setattr(db_init, "_engine", _HangingEngine(unreachable.pool))
    monkeypatch.setattr(health, "_READY_TIMEOUT_SECONDS", 0.1, raising=False)

    resp = await asyncio.wait_for(_probe(), timeout=5.0)

    assert resp.status_code == 503


async def test_a_busy_pool_is_still_ready(monkeypatch) -> None:
    """A probe that times out queueing behind requests found a busy pod, not a lost
    database. Failing it took every busy pod out of rotation at once under load.
    """
    engine = create_async_engine(settings.database_url.get_secret_value(), pool_size=1, max_overflow=0)
    monkeypatch.setattr(settings, "core_storage_role", "hybrid")
    monkeypatch.setattr(db_init, "_engine", engine)
    monkeypatch.setattr(health, "_READY_TIMEOUT_SECONDS", 0.2, raising=False)
    try:
        # A request holds the pool's only connection.
        async with engine.connect():
            resp = await asyncio.wait_for(_probe(), timeout=5.0)
    finally:
        await engine.dispose()

    assert resp.status_code == 200

"""Exhaustion retains a durable reference rather than deleting it."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.sql.dml import Delete, Update

from core_storage_api.routers import background_work as router

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.mark.parametrize("path", ["claim", "finish"])
async def test_exhaustion_parks_the_reference_without_deleting_it(monkeypatch, path):
    work = SimpleNamespace(
        tenant_id="tenant-a",
        memory_id=uuid4(),
        kind="extraction",
        generation=9,
        attempts=8,
        terminal_reason=None,
    )
    result = MagicMock()
    result.scalar_one.return_value = datetime.now(UTC)
    result.scalar_one_or_none.return_value = work
    result.scalars.return_value.all.return_value = [work]
    session = SimpleNamespace(execute=AsyncMock(return_value=result))

    @asynccontextmanager
    async def transaction():
        yield session

    monkeypatch.setattr(router, "get_session", transaction)
    body = {"tenant_id": work.tenant_id}
    if path == "finish":
        body.update(
            memory_id=str(work.memory_id), kind=work.kind, generation=9, retry=True
        )
    request = SimpleNamespace(json=AsyncMock(return_value=body))
    await getattr(router, path)(request)
    assert work.terminal_reason == "retry_exhausted"
    assert not any(
        isinstance(call.args[0], Delete) for call in session.execute.await_args_list
    )


@pytest.mark.parametrize("attempts", [1, 7, 8])
async def test_admission_deferral_does_not_exhaust_a_reference(monkeypatch, attempts):
    work = SimpleNamespace(
        tenant_id="tenant-a",
        memory_id=uuid4(),
        kind="extraction",
        generation=9,
        attempts=attempts,
        terminal_reason=None,
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = work
    session = SimpleNamespace(execute=AsyncMock(return_value=result))

    @asynccontextmanager
    async def transaction():
        yield session

    monkeypatch.setattr(router, "get_session", transaction)
    request = SimpleNamespace(
        json=AsyncMock(
            return_value={
                "tenant_id": work.tenant_id,
                "memory_id": str(work.memory_id),
                "kind": work.kind,
                "generation": work.generation,
                "retry": True,
                "deferred": True,
                "retry_after_seconds": 3,
            }
        )
    )
    await router.finish(request)
    assert work.terminal_reason is None
    [statement] = [
        c.args[0]
        for c in session.execute.await_args_list
        if isinstance(c.args[0], Update)
    ]
    params = statement.compile().params
    assert params["attempts"] == attempts - 1
    assert params["generation"] == 10
    assert timedelta(seconds=3) in params.values()

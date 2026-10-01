"""Contradiction detection reads the row it was just handed from the primary.

Path A re-read the just-committed memory with the default ``read=True``,
which is served from the read pool. Under replica lag the row was missing
(detection silently skipped) or stale (an edit judged on its old text). The
``read=False`` route went to the storage writer, but the writer's by-id GET
also used the read pool, so read-your-writes never actually held.
"""

from __future__ import annotations

import contextlib
import logging
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("role", "expected"),
    [("writer", "primary"), ("reader", "replica"), ("hybrid", "replica")],
)
async def test_by_id_read_uses_the_primary_on_the_writer(monkeypatch, role, expected):
    from core_storage_api.config import settings
    from core_storage_api.services import postgres_service as ps

    used: list[str] = []

    def _factory(name):
        @contextlib.asynccontextmanager
        async def _session():
            used.append(name)
            session = AsyncMock()
            session.execute.return_value.scalar_one_or_none.return_value = None
            yield session

        return _session

    monkeypatch.setattr(settings, "core_storage_role", role)
    monkeypatch.setattr(ps, "get_session", _factory("primary"))
    monkeypatch.setattr(ps, "get_read_session", _factory("replica"))
    await ps.PostgresService().memory_get_by_id_for_tenant(uuid4(), "t1")
    assert used == [expected]


async def test_path_a_reads_through_the_writer_and_logs_a_missing_row(caplog):
    from core_api.services import contradiction_detector as cd

    sc = AsyncMock()
    sc.get_memory = AsyncMock(return_value=None)
    gate = AsyncMock()
    with (
        patch.object(cd, "get_storage_client", return_value=sc),
        patch.object(cd, "_acquire_detection_slot", AsyncMock(return_value=(gate, 0))),
        caplog.at_level(logging.WARNING),
    ):
        await cd.detect_contradictions_async(
            memory_id=uuid4(),
            tenant_id="t1",
            fleet_id=None,
            content="x",
            embedding=[0.0],
        )
    sc.get_memory.assert_awaited_once()
    assert sc.get_memory.await_args.kwargs.get("read") is False
    assert any(
        "contradiction_detection_skipped_row_missing" in r.getMessage()
        for r in caplog.records
    )

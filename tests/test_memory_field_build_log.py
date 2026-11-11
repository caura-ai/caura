"""A memory map build says where its time went.

A cold ``GET /memories/field`` pages up to 50,000 rows with their embeddings,
decodes them and lays them out, under a 40-second deadline. Request latency
alone cannot tell those parts apart, or a cold build from a cached one, so a
load test of the map had nothing to read. Each finished layout logs one line:
the points and pages, and the milliseconds spent in storage, decoding and the
layout. A request that stops waiting at the deadline logs that too.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging

import numpy as np
import pytest

from core_api.services import memory_field

pytestmark = pytest.mark.unit

TENANT = "map-timing"
SCOPE = {"tenant_id": TENANT, "include_scope_agent": True}


def _row(i: int) -> dict:
    vec = np.zeros(8, dtype="<f4")
    vec[i % 8] = 1.0
    return {
        "id": f"m-{i}",
        "title": None,
        "preview": f"memory {i}",
        "created_at": "2026-11-10T00:00:00+00:00",
        "entity_ids": [],
        "embedding_b64": base64.b64encode(vec.tobytes()).decode(),
    }


class _Storage:
    """Serves ``rows`` a page at a time, as core-storage's field page does."""

    def __init__(self, rows: list[dict], *, page_delay: float = 0.0) -> None:
        self.rows = rows
        self.page_delay = page_delay

    async def memory_field_signature(self, scope: dict) -> dict:
        return {
            "count": len(self.rows),
            "embedded": len(self.rows),
            "newest": "t",
            "revision": 1,
        }

    async def memory_field_page(self, body: dict) -> dict:
        await asyncio.sleep(self.page_delay)
        start = 0
        if "cursor_id" in body:
            start = (
                next(i for i, r in enumerate(self.rows) if r["id"] == body["cursor_id"])
                + 1
            )
        page = [dict(r) for r in self.rows[start : start + body["limit"]]]
        more = start + len(page) < len(self.rows)
        cursor = (
            {"ts": page[-1]["created_at"], "id": page[-1]["id"]}
            if page and more
            else None
        )
        return {"rows": page, "entities": [], "next_cursor": cursor}


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    def clear() -> None:
        memory_field.field_cache.clear()
        memory_field._signature_cache.clear()
        memory_field._build_started.clear()
        memory_field._builds.clear()
        memory_field._building_tenants.clear()
        memory_field._layouts.clear()

    @contextlib.asynccontextmanager
    async def no_slot(*_args, **_kwargs):
        yield

    monkeypatch.setattr(memory_field, "per_tenant_slot", no_slot)
    clear()
    yield
    clear()


async def test_a_build_logs_its_points_pages_and_where_the_time_went(
    monkeypatch, caplog
):
    monkeypatch.setattr(memory_field, "FIELD_PAGE_SIZE", 2)
    monkeypatch.setattr(
        memory_field,
        "get_storage_client",
        lambda: _Storage([_row(i) for i in range(5)]),
    )

    with caplog.at_level(logging.INFO, logger=memory_field.logger.name):
        payload = await memory_field._get_memory_field(SCOPE, limit=20000)
        # The log line is written as the layout's task completes.
        for _ in range(3):
            await asyncio.sleep(0)

    assert len(payload["points"]) == 5
    (record,) = [
        r for r in caplog.records if getattr(r, "path", None) == "memory-field-build"
    ]
    assert record.levelno == logging.INFO
    assert TENANT in record.getMessage()
    assert (record.tenant_id, record.points, record.embedded, record.pages) == (
        TENANT,
        5,
        5,
        3,
    )
    for part in ("storage_ms", "decode_ms", "layout_ms", "total_ms"):
        assert isinstance(getattr(record, part), int) and getattr(record, part) >= 0
    assert record.total_ms >= record.layout_ms


async def test_a_cached_map_logs_no_build(monkeypatch, caplog):
    monkeypatch.setattr(
        memory_field,
        "get_storage_client",
        lambda: _Storage([_row(i) for i in range(3)]),
    )
    await memory_field._get_memory_field(SCOPE, limit=20000)
    for _ in range(3):
        await asyncio.sleep(0)
    caplog.clear()  # the first, uncached build's line

    with caplog.at_level(logging.INFO, logger=memory_field.logger.name):
        await memory_field._get_memory_field(SCOPE, limit=20000)
        await asyncio.sleep(0)

    assert not [
        r for r in caplog.records if getattr(r, "path", None) == "memory-field-build"
    ]


async def test_a_request_that_stops_waiting_at_the_deadline_says_so(
    monkeypatch, caplog
):
    monkeypatch.setattr(memory_field, "FIELD_BUILD_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(
        memory_field,
        "get_storage_client",
        lambda: _Storage([_row(i) for i in range(3)], page_delay=1.0),
    )

    with caplog.at_level(logging.WARNING, logger=memory_field.logger.name):
        with pytest.raises(memory_field.FieldBusyError):
            await memory_field._get_memory_field(SCOPE, limit=20000)

    (record,) = [
        r
        for r in caplog.records
        if getattr(r, "path", None) == "memory-field-build-deadline"
    ]
    assert record.levelno == logging.WARNING
    assert TENANT in record.getMessage()
    assert record.tenant_id == TENANT
    assert record.deadline_s == 0.05

"""B7 x C25 — the metadata_patch JSONB merge deep-merges the ``_system``
namespace instead of replacing it.

``||`` is shallow: a worker patch carrying ``_system: {enrichment_pending:
false}`` would otherwise REPLACE the stored sub-object, clobbering sibling
platform keys (write_latency_ms, write_mode, …) every time a *_pending flag
clears. Runs against the real Postgres fixture — jsonb_set semantics are the
thing under test.
"""

import uuid

import pytest

from core_storage_api.services.postgres_service import PostgresService, get_session

pytestmark = pytest.mark.asyncio


async def _insert_memory(svc: PostgresService, tenant: str, metadata: dict):
    row = await svc.memory_add(
        {
            "tenant_id": tenant,
            "agent_id": "b7-tester",
            "content": f"b7 canary {uuid.uuid4()}",
            "memory_type": "fact",
            "weight": 0.5,
            "metadata_": metadata,
            "status": "active",
            "visibility": "scope_team",
        }
    )
    return row


async def _get_metadata(memory_id, tenant):
    from sqlalchemy import text

    async with get_session() as s:
        res = await s.execute(
            text("SELECT metadata FROM memories WHERE id = :i AND tenant_id = :t"),
            {"i": str(memory_id), "t": tenant},
        )
        return res.scalar_one()


async def test_system_namespace_deep_merges():
    svc = PostgresService()
    tenant = f"b7-{uuid.uuid4().hex[:8]}"
    stored = {
        "write_mode": "fast",
        "enrichment_pending": True,
        "_system": {
            "write_mode": "fast",
            "enrichment_pending": True,
            "write_latency_ms": 123,
        },
        "caller_key": "kept",
    }
    row = await _insert_memory(svc, tenant, stored)

    ok = await svc.memory_update(
        row.id,
        tenant,
        {"metadata_patch": {"enrichment_pending": False, "_system": {"enrichment_pending": False}}},
    )
    assert ok

    md = await _get_metadata(row.id, tenant)
    assert md["enrichment_pending"] is False
    sysm = md["_system"]
    assert sysm["enrichment_pending"] is False  # cleared
    assert sysm["write_latency_ms"] == 123  # sibling SURVIVED (the deep-merge point)
    assert sysm["write_mode"] == "fast"
    assert md["caller_key"] == "kept"


async def test_patch_without_system_keeps_shallow_behavior():
    svc = PostgresService()
    tenant = f"b7-{uuid.uuid4().hex[:8]}"
    row = await _insert_memory(svc, tenant, {"a": 1})
    ok = await svc.memory_update(row.id, tenant, {"metadata_patch": {"b": 2}})
    assert ok
    md = await _get_metadata(row.id, tenant)
    assert md == {"a": 1, "b": 2}
    assert "_system" not in md  # no phantom namespace injected

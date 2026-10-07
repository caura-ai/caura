"""lme-0929-m-03 (SIDE-56) — "is my store settled?" must be answerable.

A LongMemEval store ingested through ``/memories/bulk`` kept changing for days:
embedding, enrichment and atomic-fact fan-out all run after the write returns.
A benchmark measured it hours after ingest and again two weeks later and got
different answers, and nothing could have told it the first measurement was
premature.

``memory_stats_breakdown(include_pending=True)`` answers it from durable row
state. Against real Postgres on the MIGRATED schema, because two of the three
markers live in ``metadata``, which the migration chain builds as ``json`` while
``create_all`` builds ``jsonb`` — a predicate that only works on one of them
would pass the root suite and fail in production, or the reverse. The markers
are cleared here through ``memory_update`` with the exact patches the writers
send (core-worker's enrich PATCH, core-api's fan-out consumer), so the test
covers "the marker clears", not just "the marker counts".
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from core_storage_api.services.postgres_service import PostgresService
from tests.conftest import plan_with_only_index

pytestmark = pytest.mark.asyncio

_VEC = [0.1] * 1024


async def _add(svc: PostgresService, tenant: str, *, agent: str = "m03-a", embedding=_VEC, metadata=None):
    row = await svc.memory_add(
        {
            "tenant_id": tenant,
            "fleet_id": "m03-fleet",
            "agent_id": agent,
            "content": f"m03 {uuid.uuid4()}",
            "memory_type": "fact",
            "weight": 0.5,
            "status": "active",
            "visibility": "scope_team",
            "embedding": embedding,
            "metadata_": metadata or {},
        }
    )
    return row.id


async def _pending(svc: PostgresService, tenant: str, **kw) -> dict:
    return await svc.memory_stats_breakdown(tenant_id=tenant, include_pending=True, **kw)


async def test_each_marker_counts_and_clears_through_the_writers_own_patch(_ensure_schema):
    svc = PostgresService()
    tenant = f"m03-{uuid.uuid4().hex[:8]}"

    await _add(svc, tenant)  # settled: vector present, no markers
    await _add(svc, tenant, metadata={"enrichment_pending": False, "_system": {"enrichment_pending": False}})
    no_vec = await _add(svc, tenant, embedding=None, metadata={"embedding_pending": True})
    enrich = await _add(
        svc, tenant, metadata={"enrichment_pending": True, "_system": {"enrichment_pending": True}}
    )
    legacy = await _add(svc, tenant, metadata={"enrichment_pending": True})  # pre-C25 row: top-level only
    # C25 precedence: ``_system`` wins, so a stale top-level True is NOT pending.
    await _add(svc, tenant, metadata={"enrichment_pending": True, "_system": {"enrichment_pending": False}})
    fanout = await _add(svc, tenant, metadata={"atomic_facts": [{"content": "a fact"}]})
    gone = await _add(svc, tenant, embedding=None)
    await svc.memory_update(gone, tenant, {"deleted_at": datetime(2026, 9, 29, tzinfo=UTC)})

    stats = await _pending(svc, tenant)
    assert stats["pending"] == {"embedding": 1, "enrichment": 2, "fanout": 1}
    assert stats["settled"] is False
    assert stats["total"] == 7  # the soft-deleted row counts nowhere

    # core-worker's enrich PATCH (``_build_patch``): false into both homes.
    clear_enrich = {"metadata_patch": {"enrichment_pending": False, "_system": {"enrichment_pending": False}}}
    await svc.memory_update(enrich, tenant, clear_enrich)
    await svc.memory_update(legacy, tenant, clear_enrich)
    # core-api's fan-out consumer: overwrite with JSON null.
    await svc.memory_update(fanout, tenant, {"metadata_patch": {"atomic_facts": None}})
    stats = await _pending(svc, tenant)
    assert stats["pending"] == {"embedding": 1, "enrichment": 0, "fanout": 0}
    assert stats["settled"] is False

    await svc.memory_update(
        no_vec, tenant, {"embedding": _VEC, "metadata_patch": {"embedding_pending": False}}
    )
    stats = await _pending(svc, tenant)
    assert stats["pending"] == {"embedding": 0, "enrichment": 0, "fanout": 0}
    assert stats["settled"] is True


async def test_scoping_by_agent_and_fleet(_ensure_schema):
    svc = PostgresService()
    tenant = f"m03s-{uuid.uuid4().hex[:8]}"
    await _add(svc, tenant, agent="busy", embedding=None)
    await _add(svc, tenant, agent="busy", metadata={"_system": {"enrichment_pending": True}})
    await _add(svc, tenant, agent="done")

    busy = await _pending(svc, tenant, agent_id="busy")
    assert busy["pending"] == {"embedding": 1, "enrichment": 1, "fanout": 0}
    assert busy["settled"] is False

    done = await _pending(svc, tenant, agent_id="done")
    assert done["pending"] == {"embedding": 0, "enrichment": 0, "fanout": 0}
    assert done["settled"] is True

    assert (await _pending(svc, tenant, fleet_id="m03-fleet"))["settled"] is False
    assert (await _pending(svc, tenant, fleet_id="other-fleet"))["settled"] is True
    # Another tenant's pending rows never leak into this one's answer.
    assert (await _pending(svc, f"m03-empty-{uuid.uuid4().hex[:8]}"))["settled"] is True


async def test_off_by_default_so_existing_callers_keep_their_shape(_ensure_schema):
    """MCP ``caura_stats`` and the reports call this without the flag."""
    svc = PostgresService()
    tenant = f"m03d-{uuid.uuid4().hex[:8]}"
    await _add(svc, tenant, embedding=None)
    stats = await svc.memory_stats_breakdown(tenant_id=tenant)
    assert set(stats) == {"total", "by_type", "by_agent", "by_status"}


async def test_the_route_forwards_the_flag(client):
    tenant = f"m03r-{uuid.uuid4().hex[:8]}"
    await _add(PostgresService(), tenant, embedding=None)
    resp = await client.post(
        "/api/v1/storage/memories/stats-breakdown", json={"tenant_id": tenant, "include_pending": True}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["pending"]["embedding"] == 1
    assert body["settled"] is False
    plain = await client.post("/api/v1/storage/memories/stats-breakdown", json={"tenant_id": tenant})
    assert "pending" not in plain.json()


async def test_the_count_can_be_served_by_the_partial_index(_ensure_schema):
    """The cost claim, checked against the planner rather than asserted.

    The partial index is only usable if the query's WHERE implies its predicate,
    which is why both are built from the same constant. Planned with that index
    as the table's only one (``plan_with_only_index``): the question here is
    whether the index CAN serve the query.
    """
    from common.models import Memory
    from core_storage_api.services.postgres_service import pending_work_count_stmt

    # The filter shape ``memory_stats_breakdown`` passes for a tenant-scoped,
    # fleet-less, agent-less call.
    stmt = pending_work_count_stmt(
        [Memory.deleted_at.is_(None), Memory.tenant_id == "m03-plan", Memory.visibility != "scope_agent"]
    )
    plan = await plan_with_only_index(stmt, "ix_memories_pending_work")
    assert "ix_memories_pending_work" in plan, plan

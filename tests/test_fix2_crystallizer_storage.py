"""Fix 2 final-cleanup PR3a — crystallizer enrichments routed through core-storage-api.

Two new READ endpoints on the memories router, replacing the last two
service-level direct-DB sites in ``crystallizer_service`` (_compute_health /
_compute_usage):

- GET /memories/entity-coverage  → {"memories_with_entities": int}
      (distinct memories with >=1 entity link; caller divides by total for pct)
- GET /memories/audit-usage      → {"agent_activity": [...], "peak_hours": [...]}
      (ports the two audit_log queries; search_write_ratio dropped — its
       usage_counters table does not exist in the OSS schema)

Rows are seeded via raw committed INSERTs on an independent ``get_session()``
(storage commits on its own connection, so the rolled-back ``db`` fixture is
invisible to it). A unique tenant per test keeps concurrent suite runs isolated.
Mirrors test_ph6_entity_linking_storage.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from common.models import AuditLog, Entity, Memory, MemoryEntityLink
from core_storage_api.services.postgres_service import get_session

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_BASE = "/api/v1/storage/memories"


def _t() -> str:
    return f"test-tenant-fix2-cryst-{uuid4().hex[:8]}"


async def _seed_memory(
    *,
    tenant_id: str,
    fleet_id: str | None = None,
    deleted_at: datetime | None = None,
) -> str:
    mem_id = uuid4()
    async with get_session() as session:
        session.add(
            Memory(
                id=mem_id,
                tenant_id=tenant_id,
                fleet_id=fleet_id,
                agent_id="agent-1",
                memory_type="fact",
                content="x",
                status="active",
                deleted_at=deleted_at,
            )
        )
    return str(mem_id)


async def _seed_entity(*, tenant_id: str) -> str:
    ent_id = uuid4()
    async with get_session() as session:
        session.add(
            Entity(
                id=ent_id,
                tenant_id=tenant_id,
                entity_type="organization",
                canonical_name=f"ent-{uuid4().hex[:6]}",
            )
        )
    return str(ent_id)


async def _seed_link(*, memory_id: str, entity_id: str) -> None:
    async with get_session() as session:
        session.add(
            MemoryEntityLink(memory_id=memory_id, entity_id=entity_id, role="mentioned")
        )


async def _seed_audit(
    *,
    tenant_id: str,
    agent_id: str | None,
    action: str,
    resource_type: str = "memory",
    hour: int = 12,
) -> None:
    async with get_session() as session:
        session.add(
            AuditLog(
                id=uuid4(),
                tenant_id=tenant_id,
                agent_id=agent_id,
                action=action,
                resource_type=resource_type,
                created_at=datetime(2026, 1, 15, hour, 30, tzinfo=UTC),
            )
        )


# ===========================================================================
# entity-coverage
# ===========================================================================


async def test_entity_coverage_counts_distinct_memories(storage_http):
    t = _t()
    m1 = await _seed_memory(tenant_id=t)
    m2 = await _seed_memory(tenant_id=t)
    await _seed_memory(tenant_id=t)  # m3 — unlinked
    e1 = await _seed_entity(tenant_id=t)
    e2 = await _seed_entity(tenant_id=t)
    await _seed_link(memory_id=m1, entity_id=e1)
    await _seed_link(
        memory_id=m1, entity_id=e2
    )  # m1 linked twice → DISTINCT counts once
    await _seed_link(memory_id=m2, entity_id=e1)

    resp = await storage_http.get(f"{_BASE}/entity-coverage", params={"tenant_id": t})
    assert resp.status_code == 200
    assert resp.json() == {"memories_with_entities": 2}


async def test_entity_coverage_excludes_soft_deleted(storage_http):
    t = _t()
    live = await _seed_memory(tenant_id=t)
    gone = await _seed_memory(tenant_id=t, deleted_at=datetime(2026, 1, 1, tzinfo=UTC))
    e = await _seed_entity(tenant_id=t)
    await _seed_link(memory_id=live, entity_id=e)
    await _seed_link(memory_id=gone, entity_id=e)

    resp = await storage_http.get(f"{_BASE}/entity-coverage", params={"tenant_id": t})
    assert resp.json() == {"memories_with_entities": 1}


async def test_entity_coverage_tenant_isolation(storage_http):
    t, other = _t(), _t()
    m = await _seed_memory(tenant_id=t)
    e = await _seed_entity(tenant_id=t)
    await _seed_link(memory_id=m, entity_id=e)

    resp = await storage_http.get(
        f"{_BASE}/entity-coverage", params={"tenant_id": other}
    )
    assert resp.json() == {"memories_with_entities": 0}


async def test_entity_coverage_fleet_filter(storage_http):
    t = _t()
    in_fleet = await _seed_memory(tenant_id=t, fleet_id="fleet-x")
    out_fleet = await _seed_memory(tenant_id=t, fleet_id="fleet-y")
    e = await _seed_entity(tenant_id=t)
    await _seed_link(memory_id=in_fleet, entity_id=e)
    await _seed_link(memory_id=out_fleet, entity_id=e)

    resp = await storage_http.get(
        f"{_BASE}/entity-coverage", params={"tenant_id": t, "fleet_id": "fleet-x"}
    )
    assert resp.json() == {"memories_with_entities": 1}


async def test_entity_coverage_missing_tenant_422(storage_http):
    resp = await storage_http.get(f"{_BASE}/entity-coverage")
    assert resp.status_code == 422


# ===========================================================================
# audit-usage
# ===========================================================================


async def test_audit_usage_agent_activity_ordered_by_writes(storage_http):
    t = _t()
    # agent-a: 2 writes + 3 searches; agent-b: 1 write
    for _ in range(2):
        await _seed_audit(tenant_id=t, agent_id="agent-a", action="create")
    for _ in range(3):
        await _seed_audit(tenant_id=t, agent_id="agent-a", action="search")
    await _seed_audit(tenant_id=t, agent_id="agent-b", action="create")

    resp = await storage_http.get(f"{_BASE}/audit-usage", params={"tenant_id": t})
    assert resp.status_code == 200
    activity = resp.json()["agent_activity"]
    assert activity[0] == {"agent_id": "agent-a", "writes": 2, "searches": 3}
    assert {"agent_id": "agent-b", "writes": 1, "searches": 0} in activity


async def test_audit_usage_peak_hours(storage_http):
    t = _t()
    for _ in range(3):
        await _seed_audit(tenant_id=t, agent_id="agent-a", action="search", hour=14)
    await _seed_audit(tenant_id=t, agent_id="agent-a", action="search", hour=9)

    resp = await storage_http.get(f"{_BASE}/audit-usage", params={"tenant_id": t})
    peak = resp.json()["peak_hours"]
    assert peak[0] == {"hour": 14, "count": 3}


async def test_audit_usage_peak_hours_excludes_non_memory(storage_http):
    t = _t()
    await _seed_audit(
        tenant_id=t, agent_id="agent-a", action="create", hour=10
    )  # memory
    for _ in range(3):  # more non-memory events at a different hour — must NOT dominate
        await _seed_audit(
            tenant_id=t,
            agent_id="agent-a",
            action="search",
            resource_type="entity",
            hour=20,
        )

    resp = await storage_http.get(f"{_BASE}/audit-usage", params={"tenant_id": t})
    peak = resp.json()["peak_hours"]
    assert peak == [{"hour": 10, "count": 1}]


async def test_audit_usage_excludes_null_agent_and_non_memory_searches(storage_http):
    t = _t()
    await _seed_audit(tenant_id=t, agent_id="agent-a", action="create")  # 1 write
    await _seed_audit(
        tenant_id=t, agent_id="agent-a", action="search"
    )  # memory search → counted
    # non-memory search → NOT counted (searches FILTER is scoped to resource_type='memory')
    await _seed_audit(
        tenant_id=t, agent_id="agent-a", action="search", resource_type="entity"
    )
    # unattributed event → excluded from the per-agent breakdown (agent_id IS NOT NULL)
    await _seed_audit(tenant_id=t, agent_id=None, action="create")

    resp = await storage_http.get(f"{_BASE}/audit-usage", params={"tenant_id": t})
    activity = resp.json()["agent_activity"]
    assert [a["agent_id"] for a in activity] == ["agent-a"]  # no null-agent row
    assert activity[0] == {"agent_id": "agent-a", "writes": 1, "searches": 1}


async def test_audit_usage_tenant_isolation(storage_http):
    t, other = _t(), _t()
    await _seed_audit(tenant_id=t, agent_id="agent-a", action="create")

    resp = await storage_http.get(f"{_BASE}/audit-usage", params={"tenant_id": other})
    assert resp.json() == {"agent_activity": [], "peak_hours": []}


async def test_audit_usage_missing_tenant_422(storage_http):
    resp = await storage_http.get(f"{_BASE}/audit-usage")
    assert resp.status_code == 422

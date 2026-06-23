"""Fix 2 Ph5b (PR2) — evolve scope-filter + weight-adjust routed through core-storage-api.

Exercises the 2 new core-storage-api endpoints via the typed storage client
(bridged in-process to the storage app by the conftest ASGI fixture, against
the test DB):

- POST /evolve/filter-by-scope   (sc.evolve_filter_by_scope)  — scope SELECT
- POST /evolve/apply-weights     (sc.evolve_apply_weights)    — clamp CTE + jsonb_set backfill (ONE txn)

Rows are seeded via a raw committed INSERT on an independent session (the
public create endpoint doesn't expose weight / agent_id / fleet_id / metadata
directly, which these passes filter + mutate). A unique tenant per test keeps
concurrent suite runs isolated. Mirrors test_ph5b_insights_storage.py.
"""

from __future__ import annotations

import json as _json
from uuid import uuid4

import pytest
from sqlalchemy import text

from core_storage_api.services.postgres_service import get_session

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _t() -> str:
    return f"test-tenant-ph5b-evolve-{uuid4().hex[:8]}"


async def _seed_memory(
    *,
    tenant_id: str,
    content: str = "x",
    agent_id: str = "agent-1",
    fleet_id: str | None = None,
    memory_type: str = "fact",
    status: str = "active",
    weight: float = 0.5,
    metadata: dict | None = None,
    visibility: str = "scope_team",
) -> str:
    """Raw committed INSERT covering the columns the evolve passes touch."""
    mem_id = str(uuid4())
    async with get_session() as session:
        await session.execute(
            text(
                """
                INSERT INTO memories
                    (id, tenant_id, fleet_id, agent_id, content, memory_type,
                     status, weight, metadata, visibility)
                VALUES
                    (CAST(:id AS uuid), :tenant_id, :fleet_id, :agent_id, :content, :memory_type,
                     :status, :weight, CAST(:metadata AS jsonb), :visibility)
                """
            ),
            {
                "id": mem_id,
                "tenant_id": tenant_id,
                "fleet_id": fleet_id,
                "agent_id": agent_id,
                "content": content,
                "memory_type": memory_type,
                "status": status,
                "weight": weight,
                "metadata": _json.dumps(metadata) if metadata is not None else None,
                "visibility": visibility,
            },
        )
    return mem_id


async def _weight(mem_id: str) -> float:
    async with get_session() as session:
        row = (
            await session.execute(
                text("SELECT weight FROM memories WHERE id = CAST(:id AS uuid)"), {"id": mem_id}
            )
        ).fetchone()
    return float(row.weight)


async def _metadata(mem_id: str) -> dict | None:
    async with get_session() as session:
        row = (
            await session.execute(
                text("SELECT metadata FROM memories WHERE id = CAST(:id AS uuid)"), {"id": mem_id}
            )
        ).fetchone()
    meta = row.metadata
    # asyncpg's JSONB codec may hand jsonb back as a JSON string on a raw
    # text() read; normalise to a dict so the assertions read uniformly.
    if isinstance(meta, str):
        meta = _json.loads(meta)
    return meta


async def _soft_delete(mem_id: str) -> None:
    async with get_session() as session:
        await session.execute(
            text("UPDATE memories SET deleted_at = now() WHERE id = CAST(:id AS uuid)"),
            {"id": mem_id},
        )


# ===========================================================================
# A. filter-by-scope
# ===========================================================================


async def test_filter_scope_agent_keeps_only_caller(sc):
    tenant = _t()
    mine_a = await _seed_memory(tenant_id=tenant, agent_id="a1", content="mine-a")
    mine_b = await _seed_memory(tenant_id=tenant, agent_id="a1", content="mine-b")
    other = await _seed_memory(tenant_id=tenant, agent_id="a2", content="other")
    allowed = await sc.evolve_filter_by_scope(
        tenant_id=tenant,
        caller_agent_id="a1",
        fleet_id=None,
        scope="agent",
        ids=[mine_a, mine_b, other],
    )
    assert set(allowed) == {mine_a, mine_b}


async def test_filter_scope_fleet_keeps_only_fleet(sc):
    tenant = _t()
    ours = await _seed_memory(tenant_id=tenant, agent_id="a", fleet_id="fleet-x", content="ours")
    theirs = await _seed_memory(tenant_id=tenant, agent_id="b", fleet_id="fleet-y", content="theirs")
    allowed = await sc.evolve_filter_by_scope(
        tenant_id=tenant,
        caller_agent_id="a",
        fleet_id="fleet-x",
        scope="fleet",
        ids=[ours, theirs],
    )
    assert allowed == [ours]


async def test_filter_scope_all_keeps_everything_in_tenant(sc):
    tenant = _t()
    m1 = await _seed_memory(tenant_id=tenant, agent_id="a", fleet_id="fa", content="m1")
    m2 = await _seed_memory(tenant_id=tenant, agent_id="b", fleet_id="fb", content="m2")
    allowed = await sc.evolve_filter_by_scope(
        tenant_id=tenant,
        caller_agent_id="a",
        fleet_id=None,
        scope="all",
        ids=[m1, m2],
    )
    assert set(allowed) == {m1, m2}


async def test_filter_drops_soft_deleted_and_missing(sc):
    tenant = _t()
    mine = await _seed_memory(tenant_id=tenant, agent_id="a", content="mine")
    gone = await _seed_memory(tenant_id=tenant, agent_id="a", content="gone")
    await _soft_delete(gone)
    missing = str(uuid4())
    allowed = await sc.evolve_filter_by_scope(
        tenant_id=tenant,
        caller_agent_id="a",
        fleet_id=None,
        scope="agent",
        ids=[mine, gone, missing],
    )
    assert allowed == [mine]


async def test_filter_tenant_isolation(sc):
    t_a, t_b = _t(), _t()
    mine = await _seed_memory(tenant_id=t_a, agent_id="a", content="a")
    # Same id queried under a different tenant must not match.
    allowed = await sc.evolve_filter_by_scope(
        tenant_id=t_b,
        caller_agent_id="a",
        fleet_id=None,
        scope="all",
        ids=[mine],
    )
    assert allowed == []


async def test_filter_empty_ids(sc):
    allowed = await sc.evolve_filter_by_scope(
        tenant_id=_t(),
        caller_agent_id="a",
        fleet_id=None,
        scope="agent",
        ids=[],
    )
    assert allowed == []


# ===========================================================================
# B. apply-weights — clamp CTE + RETURNING + backfill
# ===========================================================================


async def test_apply_weights_returns_old_and_new(sc):
    tenant = _t()
    mid = await _seed_memory(tenant_id=tenant, agent_id="a", weight=0.5)
    resp = await sc.evolve_apply_weights(
        tenant_id=tenant, ids=[mid], delta=0.1, floor=0.05, cap=1.0
    )
    assert resp["backfilled"] is False
    assert len(resp["adjustments"]) == 1
    adj = resp["adjustments"][0]
    assert adj["id"] == mid
    assert adj["old_weight"] == pytest.approx(0.5)
    assert adj["new_weight"] == pytest.approx(0.6)
    assert await _weight(mid) == pytest.approx(0.6)


async def test_apply_weights_clamps_at_floor(sc):
    tenant = _t()
    mid = await _seed_memory(tenant_id=tenant, agent_id="a", weight=0.1)
    resp = await sc.evolve_apply_weights(
        tenant_id=tenant, ids=[mid], delta=-0.5, floor=0.05, cap=1.0
    )
    assert resp["adjustments"][0]["new_weight"] == pytest.approx(0.05)
    assert await _weight(mid) == pytest.approx(0.05)


async def test_apply_weights_clamps_at_cap(sc):
    tenant = _t()
    mid = await _seed_memory(tenant_id=tenant, agent_id="a", weight=0.95)
    resp = await sc.evolve_apply_weights(
        tenant_id=tenant, ids=[mid], delta=0.5, floor=0.05, cap=1.0
    )
    assert resp["adjustments"][0]["new_weight"] == pytest.approx(1.0)
    assert await _weight(mid) == pytest.approx(1.0)


async def test_apply_weights_backfills_source_outcome_id(sc):
    tenant = _t()
    target = await _seed_memory(tenant_id=tenant, agent_id="a", weight=0.5)
    rule = await _seed_memory(tenant_id=tenant, agent_id="a", memory_type="rule", metadata={"generated_by": "evolve"})
    outcome_id = "11111111-1111-1111-1111-111111111111"
    resp = await sc.evolve_apply_weights(
        tenant_id=tenant,
        ids=[target],
        delta=0.1,
        floor=0.05,
        cap=1.0,
        rule_id=rule,
        outcome_id=outcome_id,
    )
    assert resp["backfilled"] is True
    meta = await _metadata(rule)
    assert meta.get("source_outcome_id") == outcome_id
    # The pre-existing key must be preserved (jsonb_set, not replace).
    assert meta.get("generated_by") == "evolve"


async def test_apply_weights_no_backfill_when_rule_or_outcome_absent(sc):
    tenant = _t()
    target = await _seed_memory(tenant_id=tenant, agent_id="a", weight=0.5)
    rule = await _seed_memory(tenant_id=tenant, agent_id="a", memory_type="rule")
    # rule_id present but no outcome_id → no backfill.
    resp = await sc.evolve_apply_weights(
        tenant_id=tenant, ids=[target], delta=0.1, floor=0.05, cap=1.0, rule_id=rule
    )
    assert resp["backfilled"] is False
    meta = await _metadata(rule)
    assert meta is None or "source_outcome_id" not in meta


async def test_apply_weights_tenant_isolation(sc):
    t_a, t_b = _t(), _t()
    mid = await _seed_memory(tenant_id=t_a, agent_id="a", weight=0.5)
    # Tenant B cannot move tenant A's weight.
    resp = await sc.evolve_apply_weights(
        tenant_id=t_b, ids=[mid], delta=0.5, floor=0.05, cap=1.0
    )
    assert resp["adjustments"] == []
    assert await _weight(mid) == pytest.approx(0.5)


async def test_apply_weights_skips_soft_deleted(sc):
    tenant = _t()
    mid = await _seed_memory(tenant_id=tenant, agent_id="a", weight=0.5)
    await _soft_delete(mid)
    resp = await sc.evolve_apply_weights(
        tenant_id=tenant, ids=[mid], delta=0.1, floor=0.05, cap=1.0
    )
    assert resp["adjustments"] == []


async def test_apply_weights_empty_ids(sc):
    resp = await sc.evolve_apply_weights(
        tenant_id=_t(), ids=[], delta=0.1, floor=0.05, cap=1.0
    )
    assert resp == {"adjustments": [], "backfilled": False}


# ===========================================================================
# C. 422 input-validation guards (raw httpx — typed client never sends these)
# ===========================================================================


async def test_filter_missing_tenant_422(storage_http):
    resp = await storage_http.post(
        "/api/v1/storage/evolve/filter-by-scope",
        json={"caller_agent_id": "a", "scope": "agent", "ids": []},
    )
    assert resp.status_code == 422


async def test_filter_missing_caller_agent_422(storage_http):
    resp = await storage_http.post(
        "/api/v1/storage/evolve/filter-by-scope",
        json={"tenant_id": "t", "scope": "agent", "ids": []},
    )
    assert resp.status_code == 422


async def test_filter_missing_ids_422(storage_http):
    resp = await storage_http.post(
        "/api/v1/storage/evolve/filter-by-scope",
        json={"tenant_id": "t", "caller_agent_id": "a", "scope": "agent"},
    )
    assert resp.status_code == 422


async def test_filter_fleet_scope_missing_fleet_id_422(storage_http):
    # scope='fleet' without fleet_id would raise ValueError in the service
    # (-> 500); the router guards it as a 422 for direct storage callers. A
    # valid ids list isolates this to the fleet guard, not the ids check.
    resp = await storage_http.post(
        "/api/v1/storage/evolve/filter-by-scope",
        json={"tenant_id": "t", "caller_agent_id": "a", "scope": "fleet", "ids": ["x"]},
    )
    assert resp.status_code == 422


async def test_filter_invalid_uuid_in_ids_422(storage_http):
    # A non-UUID id raises ValueError in the service (UUID parse); the router
    # converts it to a 422 for direct storage callers instead of a 500.
    resp = await storage_http.post(
        "/api/v1/storage/evolve/filter-by-scope",
        json={"tenant_id": "t", "caller_agent_id": "a", "scope": "agent", "ids": ["not-a-uuid"]},
    )
    assert resp.status_code == 422


async def test_filter_invalid_scope_422(storage_http):
    # An unrecognized scope must 422, not silently fall through to scope='all'
    # (no-extra-filter) behavior in the service — that would be fail-open.
    resp = await storage_http.post(
        "/api/v1/storage/evolve/filter-by-scope",
        json={"tenant_id": "t", "caller_agent_id": "a", "scope": "AGENT", "ids": ["x"]},
    )
    assert resp.status_code == 422


async def test_apply_weights_missing_tenant_422(storage_http):
    resp = await storage_http.post(
        "/api/v1/storage/evolve/apply-weights",
        json={"ids": [], "delta": 0.1, "floor": 0.05, "cap": 1.0},
    )
    assert resp.status_code == 422


async def test_apply_weights_missing_delta_422(storage_http):
    resp = await storage_http.post(
        "/api/v1/storage/evolve/apply-weights",
        json={"tenant_id": "t", "ids": [], "floor": 0.05, "cap": 1.0},
    )
    assert resp.status_code == 422

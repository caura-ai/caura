"""Fix 2 Ph5b — insights analytics routed through core-storage-api.

Exercises the 9 new core-storage-api endpoints via the typed storage client
(bridged in-process to the storage app by the conftest ASGI fixture, against
the test DB):

- POST /insights/contradictions     (sc.insights_query_contradictions)
- POST /insights/failures           (sc.insights_query_failures)
- POST /insights/stale              (sc.insights_query_stale)
- POST /insights/divergence         (sc.insights_query_divergence)
- POST /insights/patterns           (sc.insights_query_patterns)
- POST /insights/discover-sample    (sc.insights_discover_sample)   — embedding
- POST /insights/supersede-priors   (sc.insights_supersede_priors)  — JSONB select + outdate
- POST /insights/restore-priors     (sc.insights_restore_priors)
- POST /insights/activity-gate      (sc.insights_activity_gate)

Rows are seeded via a raw committed INSERT (independent session) — the public
create endpoint doesn't expose status / recall_count / weight / embedding /
subject_entity_id / object_value / metadata, which the analytic reads filter
on. A unique tenant per test keeps concurrent suite runs isolated.
"""

from __future__ import annotations

import json as _json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from common.constants import VECTOR_DIM
from core_api.constants import INSIGHTS_DISCOVER_SAMPLE_SIZE, INSIGHTS_MAX_MEMORIES
from core_storage_api.services.postgres_service import get_session

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _t() -> str:
    return f"test-tenant-ph5b-{uuid4().hex[:8]}"


async def _seed_memory(
    *,
    tenant_id: str,
    content: str = "x",
    agent_id: str = "agent-1",
    fleet_id: str | None = None,
    memory_type: str = "fact",
    status: str = "active",
    weight: float = 0.5,
    recall_count: int = 0,
    created_at: datetime | None = None,
    last_recalled_at: datetime | None = None,
    supersedes_id: str | None = None,
    subject_entity_id: str | None = None,
    object_value: str | None = None,
    embedding: list[float] | None = None,
    metadata: dict | None = None,
    visibility: str = "scope_team",
) -> str:
    """Raw committed INSERT covering the columns the analytic reads filter on."""
    created = created_at or datetime.now(UTC)
    mem_id = str(uuid4())
    emb_literal = None
    if embedding is not None:
        emb_literal = "[" + ",".join(str(float(x)) for x in embedding) + "]"
    async with get_session() as session:
        await session.execute(
            text(
                """
                INSERT INTO memories
                    (id, tenant_id, fleet_id, agent_id, content, memory_type,
                     status, weight, recall_count, created_at, last_recalled_at,
                     supersedes_id, subject_entity_id, object_value, embedding,
                     metadata, visibility)
                VALUES
                    (CAST(:id AS uuid), :tenant_id, :fleet_id, :agent_id, :content, :memory_type,
                     :status, :weight, :recall_count, :created_at, :last_recalled_at,
                     CAST(:supersedes_id AS uuid), CAST(:subject_entity_id AS uuid), :object_value,
                     CAST(:embedding AS vector), CAST(:metadata AS jsonb), :visibility)
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
                "recall_count": recall_count,
                "created_at": created,
                "last_recalled_at": last_recalled_at,
                "supersedes_id": supersedes_id,
                "subject_entity_id": subject_entity_id,
                "object_value": object_value,
                "embedding": emb_literal,
                "metadata": _json.dumps(metadata) if metadata is not None else None,
                "visibility": visibility,
            },
        )
    return mem_id


async def _status(mem_id: str) -> str:
    async with get_session() as session:
        row = (
            await session.execute(
                text("SELECT status FROM memories WHERE id = CAST(:id AS uuid)"), {"id": mem_id}
            )
        ).fetchone()
    return row.status


# ===========================================================================
# A. Per-focus analytic reads
# ===========================================================================


async def test_patterns_returns_recent_active(sc):
    tenant = _t()
    for i in range(3):
        await _seed_memory(tenant_id=tenant, agent_id="a1", content=f"p{i}")
    # An insight-type memory must be excluded (feedback-loop guard).
    await _seed_memory(tenant_id=tenant, agent_id="a1", memory_type="insight", content="ins")
    rows = await sc.insights_query_patterns(
        tenant_id=tenant, fleet_id=None, agent_id="a1", scope="agent", max_memories=INSIGHTS_MAX_MEMORIES
    )
    assert len(rows) == 3
    assert all(r["memory_type"] == "fact" for r in rows)
    # No embedding leaks into the prompt-shape dict.
    assert "embedding" not in rows[0]


async def test_patterns_tenant_isolation(sc):
    t_a, t_b = _t(), _t()
    await _seed_memory(tenant_id=t_a, agent_id="a1", content="a")
    await _seed_memory(tenant_id=t_b, agent_id="a1", content="b")
    rows = await sc.insights_query_patterns(
        tenant_id=t_a, fleet_id=None, agent_id="a1", scope="agent", max_memories=INSIGHTS_MAX_MEMORIES
    )
    assert {r["content"] for r in rows} == {"a"}


async def test_patterns_scope_agent_filters_by_agent(sc):
    tenant = _t()
    await _seed_memory(tenant_id=tenant, agent_id="a1", content="mine")
    await _seed_memory(tenant_id=tenant, agent_id="a2", content="theirs")
    # scope='agent' → only a1's row.
    rows = await sc.insights_query_patterns(
        tenant_id=tenant, fleet_id=None, agent_id="a1", scope="agent", max_memories=INSIGHTS_MAX_MEMORIES
    )
    assert {r["content"] for r in rows} == {"mine"}
    # scope='all' → both, regardless of agent_id.
    rows_all = await sc.insights_query_patterns(
        tenant_id=tenant, fleet_id=None, agent_id="a1", scope="all", max_memories=INSIGHTS_MAX_MEMORIES
    )
    assert {r["content"] for r in rows_all} == {"mine", "theirs"}


async def test_patterns_scope_fleet_filters_by_fleet(sc):
    tenant = _t()
    await _seed_memory(tenant_id=tenant, agent_id="a1", fleet_id="f1", content="f1mem")
    await _seed_memory(tenant_id=tenant, agent_id="a2", fleet_id="f2", content="f2mem")
    rows = await sc.insights_query_patterns(
        tenant_id=tenant, fleet_id="f1", agent_id="a1", scope="fleet", max_memories=INSIGHTS_MAX_MEMORIES
    )
    assert {r["content"] for r in rows} == {"f1mem"}


async def test_failures_low_weight_recalled(sc):
    tenant = _t()
    # weight<0.3, recall_count>0, active → returned.
    await _seed_memory(tenant_id=tenant, agent_id="a1", content="weak", weight=0.1, recall_count=5)
    # high weight → excluded.
    await _seed_memory(tenant_id=tenant, agent_id="a1", content="strong", weight=0.9, recall_count=5)
    # never recalled → excluded.
    await _seed_memory(tenant_id=tenant, agent_id="a1", content="unread", weight=0.1, recall_count=0)
    rows = await sc.insights_query_failures(
        tenant_id=tenant, fleet_id=None, agent_id="a1", scope="agent", max_memories=INSIGHTS_MAX_MEMORIES
    )
    assert {r["content"] for r in rows} == {"weak"}


async def test_stale_old_unrecalled(sc):
    tenant = _t()
    now = datetime.now(UTC)
    # zero recalls + >30d old → stale.
    old = await _seed_memory(
        tenant_id=tenant, agent_id="a1", content="old", recall_count=0,
        created_at=now - timedelta(days=60),
    )
    # recent → not stale.
    await _seed_memory(tenant_id=tenant, agent_id="a1", content="fresh", recall_count=0, created_at=now)
    rows = await sc.insights_query_stale(
        tenant_id=tenant,
        fleet_id=None,
        agent_id="a1",
        scope="agent",
        thirty_days_ago=now - timedelta(days=30),
        fourteen_days_ago=now - timedelta(days=14),
        max_memories=INSIGHTS_MAX_MEMORIES,
    )
    ids = {r["id"] for r in rows}
    assert old in ids
    assert all(r["content"] != "fresh" for r in rows)


async def test_contradictions_supersedes_and_superseded(sc):
    tenant = _t()
    now = datetime.now(UTC)
    old = await _seed_memory(
        tenant_id=tenant, agent_id="a1", content="old value", created_at=now - timedelta(days=2)
    )
    new = await _seed_memory(
        tenant_id=tenant, agent_id="a1", content="new value", created_at=now - timedelta(hours=1),
        supersedes_id=old,
    )
    rows = await sc.insights_query_contradictions(
        tenant_id=tenant, fleet_id=None, agent_id="a1", scope="agent", max_memories=INSIGHTS_MAX_MEMORIES
    )
    ids = {r["id"] for r in rows}
    # Both the supersedor and the superseded row are pulled in (the LLM needs
    # both sides of the contradiction).
    assert new in ids
    assert old in ids


async def test_contradictions_entity_divergence_group_by_having(sc):
    tenant = _t()
    ent = str(uuid4())
    # Same subject_entity_id, two different object_values → HAVING COUNT(DISTINCT
    # object_value) > 1 selects the entity, then both rows are fetched.
    await _seed_memory(
        tenant_id=tenant, agent_id="a1", content="x is 1", subject_entity_id=ent, object_value="1"
    )
    await _seed_memory(
        tenant_id=tenant, agent_id="a1", content="x is 2", subject_entity_id=ent, object_value="2"
    )
    # A different entity with a single object_value must NOT qualify.
    ent2 = str(uuid4())
    await _seed_memory(
        tenant_id=tenant, agent_id="a1", content="y is 9", subject_entity_id=ent2, object_value="9"
    )
    rows = await sc.insights_query_contradictions(
        tenant_id=tenant, fleet_id=None, agent_id="a1", scope="agent", max_memories=INSIGHTS_MAX_MEMORIES
    )
    contents = {r["content"] for r in rows}
    assert "x is 1" in contents and "x is 2" in contents
    assert "y is 9" not in contents


async def test_divergence_group_by_having_count_agents(sc):
    tenant = _t()
    ent = str(uuid4())
    # Same entity referenced by 2 distinct agents → HAVING COUNT(DISTINCT
    # agent_id) >= 2 selects it.
    await _seed_memory(
        tenant_id=tenant, agent_id="a1", content="agent1 view", subject_entity_id=ent, object_value="v1"
    )
    await _seed_memory(
        tenant_id=tenant, agent_id="a2", content="agent2 view", subject_entity_id=ent, object_value="v2"
    )
    rows = await sc.insights_query_divergence(
        tenant_id=tenant, fleet_id=None, agent_id="a1", scope="all", max_memories=INSIGHTS_MAX_MEMORIES
    )
    assert {r["content"] for r in rows} == {"agent1 view", "agent2 view"}


async def test_divergence_empty_when_single_agent(sc):
    tenant = _t()
    ent = str(uuid4())
    # Only one agent references the entity → no divergence → [].
    await _seed_memory(
        tenant_id=tenant, agent_id="a1", content="only one", subject_entity_id=ent, object_value="v"
    )
    rows = await sc.insights_query_divergence(
        tenant_id=tenant, fleet_id=None, agent_id="a1", scope="all", max_memories=INSIGHTS_MAX_MEMORIES
    )
    assert rows == []


async def test_discover_sample_includes_embedding(sc):
    tenant = _t()
    emb = [0.1] * VECTOR_DIM
    await _seed_memory(tenant_id=tenant, agent_id="a1", content="with-emb", embedding=emb)
    # No embedding → excluded.
    await _seed_memory(tenant_id=tenant, agent_id="a1", content="no-emb", embedding=None)
    rows = await sc.insights_discover_sample(
        tenant_id=tenant,
        fleet_id=None,
        agent_id="a1",
        scope="agent",
        sample_size=INSIGHTS_DISCOVER_SAMPLE_SIZE,
    )
    assert len(rows) == 1
    assert rows[0]["content"] == "with-emb"
    assert "embedding" in rows[0]
    assert rows[0]["embedding"] is not None
    assert len(rows[0]["embedding"]) == VECTOR_DIM


# ===========================================================================
# B. Supersede / restore priors
# ===========================================================================


async def test_supersede_priors_selects_by_jsonb_metadata(sc):
    tenant = _t()
    agent = "a1"
    match = await _seed_memory(
        tenant_id=tenant, agent_id=agent, memory_type="insight", content="prior match",
        metadata={"insight_focus": "patterns", "insight_scope": "agent"},
    )
    # Different focus → must NOT be outdated.
    other_focus = await _seed_memory(
        tenant_id=tenant, agent_id=agent, memory_type="insight", content="other focus",
        metadata={"insight_focus": "stale", "insight_scope": "agent"},
    )
    result = await sc.insights_supersede_priors(
        tenant_id=tenant, agent_id=agent, focus="patterns", scope="agent", fleet_id=None
    )
    assert match in result["prior_ids"]
    assert other_focus not in result["prior_ids"]
    assert result["outdated_count"] == 1
    assert await _status(match) == "outdated"
    assert await _status(other_focus) == "active"


async def test_supersede_priors_fleet_arm(sc):
    tenant = _t()
    agent = "a1"
    # fleet_id=None prior, selected only when the request also has fleet_id=None.
    fleetless = await _seed_memory(
        tenant_id=tenant, agent_id=agent, fleet_id=None, memory_type="insight", content="fleetless",
        metadata={"insight_focus": "patterns", "insight_scope": "all"},
    )
    fleeted = await _seed_memory(
        tenant_id=tenant, agent_id=agent, fleet_id="f1", memory_type="insight", content="fleeted",
        metadata={"insight_focus": "patterns", "insight_scope": "fleet"},
    )
    # Request fleet_id=None, scope='all' → only the fleetless prior.
    res_none = await sc.insights_supersede_priors(
        tenant_id=tenant, agent_id=agent, focus="patterns", scope="all", fleet_id=None
    )
    assert res_none["prior_ids"] == [fleetless]
    # Request fleet_id='f1', scope='fleet' → only the fleeted prior.
    res_f1 = await sc.insights_supersede_priors(
        tenant_id=tenant, agent_id=agent, focus="patterns", scope="fleet", fleet_id="f1"
    )
    assert res_f1["prior_ids"] == [fleeted]


async def test_supersede_priors_tenant_isolation(sc):
    t_a, t_b = _t(), _t()
    prior_a = await _seed_memory(
        tenant_id=t_a, agent_id="a1", memory_type="insight", content="A",
        metadata={"insight_focus": "patterns", "insight_scope": "agent"},
    )
    # Tenant B's supersede must not touch tenant A's prior.
    res = await sc.insights_supersede_priors(
        tenant_id=t_b, agent_id="a1", focus="patterns", scope="agent", fleet_id=None
    )
    assert res["prior_ids"] == []
    assert await _status(prior_a) == "active"


async def test_supersede_priors_no_match_returns_empty(sc):
    tenant = _t()
    res = await sc.insights_supersede_priors(
        tenant_id=tenant, agent_id="a1", focus="patterns", scope="agent", fleet_id=None
    )
    assert res == {"prior_ids": [], "outdated_count": 0}


async def test_restore_priors(sc):
    tenant = _t()
    agent = "a1"
    prior = await _seed_memory(
        tenant_id=tenant, agent_id=agent, memory_type="insight", content="p", status="outdated",
        metadata={"insight_focus": "patterns", "insight_scope": "agent"},
    )
    res = await sc.insights_restore_priors(tenant_id=tenant, prior_ids=[prior])
    assert res["restored"] == 1
    assert await _status(prior) == "active"


async def test_restore_priors_tenant_isolation(sc):
    t_a, t_b = _t(), _t()
    prior = await _seed_memory(
        tenant_id=t_a, agent_id="a1", memory_type="insight", content="p", status="outdated",
    )
    # Tenant B can't restore tenant A's row.
    res = await sc.insights_restore_priors(tenant_id=t_b, prior_ids=[prior])
    assert res["restored"] == 0
    assert await _status(prior) == "outdated"


async def test_restore_priors_empty(sc):
    res = await sc.insights_restore_priors(tenant_id=_t(), prior_ids=[])
    assert res == {"restored": 0}


# ===========================================================================
# C. Activity gate
# ===========================================================================


async def test_activity_gate_reports_max_created_at(sc):
    tenant = _t()
    now = datetime.now(UTC)
    await _seed_memory(
        tenant_id=tenant, agent_id="a1", memory_type="fact", content="f",
        created_at=now - timedelta(hours=2),
    )
    await _seed_memory(
        tenant_id=tenant, agent_id="a1", memory_type="insight", content="i",
        created_at=now - timedelta(hours=3),
    )
    gate = await sc.insights_activity_gate(tenant_id=tenant, fleet_id=None)
    assert gate["latest_non_insight"] is not None
    assert gate["latest_insight"] is not None
    # The fact is newer than the insight → growth since last insight.
    assert datetime.fromisoformat(gate["latest_non_insight"]) > datetime.fromisoformat(
        gate["latest_insight"]
    )


async def test_activity_gate_empty_tenant(sc):
    gate = await sc.insights_activity_gate(tenant_id=_t(), fleet_id=None)
    assert gate == {"latest_non_insight": None, "latest_insight": None}


# ===========================================================================
# D. 422 input-validation guards (raw httpx — typed client never sends these)
# ===========================================================================


async def test_missing_tenant_422(storage_http):
    resp = await storage_http.post(
        "/api/v1/storage/insights/patterns",
        json={"agent_id": "a1", "scope": "agent", "max_memories": 50},
    )
    assert resp.status_code == 422


async def test_missing_agent_422(storage_http):
    resp = await storage_http.post(
        "/api/v1/storage/insights/patterns",
        json={"tenant_id": "t", "scope": "agent", "max_memories": 50},
    )
    assert resp.status_code == 422


async def test_supersede_missing_focus_422(storage_http):
    resp = await storage_http.post(
        "/api/v1/storage/insights/supersede-priors",
        json={"tenant_id": "t", "agent_id": "a1", "scope": "agent"},
    )
    assert resp.status_code == 422


async def test_activity_gate_missing_tenant_422(storage_http):
    resp = await storage_http.post("/api/v1/storage/insights/activity-gate", json={})
    assert resp.status_code == 422

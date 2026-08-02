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
    title: str | None = None,
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
                    (id, tenant_id, fleet_id, agent_id, content, title, memory_type,
                     status, weight, recall_count, created_at, last_recalled_at,
                     supersedes_id, subject_entity_id, object_value, embedding,
                     metadata, visibility)
                VALUES
                    (CAST(:id AS uuid), :tenant_id, :fleet_id, :agent_id, :content, :title, :memory_type,
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
                "title": title,
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
                text("SELECT status FROM memories WHERE id = CAST(:id AS uuid)"),
                {"id": mem_id},
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
    await _seed_memory(
        tenant_id=tenant, agent_id="a1", memory_type="insight", content="ins"
    )
    rows = await sc.insights_query_patterns(
        tenant_id=tenant,
        fleet_id=None,
        agent_id="a1",
        scope="agent",
        max_memories=INSIGHTS_MAX_MEMORIES,
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
        tenant_id=t_a,
        fleet_id=None,
        agent_id="a1",
        scope="agent",
        max_memories=INSIGHTS_MAX_MEMORIES,
    )
    assert {r["content"] for r in rows} == {"a"}


async def test_patterns_scope_agent_filters_by_agent(sc):
    tenant = _t()
    await _seed_memory(tenant_id=tenant, agent_id="a1", content="mine")
    await _seed_memory(tenant_id=tenant, agent_id="a2", content="theirs")
    # scope='agent' → only a1's row.
    rows = await sc.insights_query_patterns(
        tenant_id=tenant,
        fleet_id=None,
        agent_id="a1",
        scope="agent",
        max_memories=INSIGHTS_MAX_MEMORIES,
    )
    assert {r["content"] for r in rows} == {"mine"}
    # scope='all' → both, regardless of agent_id.
    rows_all = await sc.insights_query_patterns(
        tenant_id=tenant,
        fleet_id=None,
        agent_id="a1",
        scope="all",
        max_memories=INSIGHTS_MAX_MEMORIES,
    )
    assert {r["content"] for r in rows_all} == {"mine", "theirs"}


async def test_patterns_scope_fleet_filters_by_fleet(sc):
    tenant = _t()
    await _seed_memory(tenant_id=tenant, agent_id="a1", fleet_id="f1", content="f1mem")
    await _seed_memory(tenant_id=tenant, agent_id="a2", fleet_id="f2", content="f2mem")
    rows = await sc.insights_query_patterns(
        tenant_id=tenant,
        fleet_id="f1",
        agent_id="a1",
        scope="fleet",
        max_memories=INSIGHTS_MAX_MEMORIES,
    )
    assert {r["content"] for r in rows} == {"f1mem"}


async def test_failures_low_weight_recalled(sc):
    tenant = _t()
    # weight<0.3, recall_count>0, active → returned.
    await _seed_memory(
        tenant_id=tenant, agent_id="a1", content="weak", weight=0.1, recall_count=5
    )
    # high weight → excluded.
    await _seed_memory(
        tenant_id=tenant, agent_id="a1", content="strong", weight=0.9, recall_count=5
    )
    # never recalled → excluded.
    await _seed_memory(
        tenant_id=tenant, agent_id="a1", content="unread", weight=0.1, recall_count=0
    )
    rows = await sc.insights_query_failures(
        tenant_id=tenant,
        fleet_id=None,
        agent_id="a1",
        scope="agent",
        max_memories=INSIGHTS_MAX_MEMORIES,
    )
    assert {r["content"] for r in rows} == {"weak"}


async def test_stale_old_unrecalled(sc):
    tenant = _t()
    now = datetime.now(UTC)
    # zero recalls + >30d old → stale.
    old = await _seed_memory(
        tenant_id=tenant,
        agent_id="a1",
        content="old",
        recall_count=0,
        created_at=now - timedelta(days=60),
    )
    # recent → not stale.
    await _seed_memory(
        tenant_id=tenant, agent_id="a1", content="fresh", recall_count=0, created_at=now
    )
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
        tenant_id=tenant,
        agent_id="a1",
        content="old value",
        created_at=now - timedelta(days=2),
    )
    new = await _seed_memory(
        tenant_id=tenant,
        agent_id="a1",
        content="new value",
        created_at=now - timedelta(hours=1),
        supersedes_id=old,
    )
    rows = await sc.insights_query_contradictions(
        tenant_id=tenant,
        fleet_id=None,
        agent_id="a1",
        scope="agent",
        max_memories=INSIGHTS_MAX_MEMORIES,
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
        tenant_id=tenant,
        agent_id="a1",
        content="x is 1",
        subject_entity_id=ent,
        object_value="1",
    )
    await _seed_memory(
        tenant_id=tenant,
        agent_id="a1",
        content="x is 2",
        subject_entity_id=ent,
        object_value="2",
    )
    # A different entity with a single object_value must NOT qualify.
    ent2 = str(uuid4())
    await _seed_memory(
        tenant_id=tenant,
        agent_id="a1",
        content="y is 9",
        subject_entity_id=ent2,
        object_value="9",
    )
    rows = await sc.insights_query_contradictions(
        tenant_id=tenant,
        fleet_id=None,
        agent_id="a1",
        scope="agent",
        max_memories=INSIGHTS_MAX_MEMORIES,
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
        tenant_id=tenant,
        agent_id="a1",
        content="agent1 view",
        subject_entity_id=ent,
        object_value="v1",
    )
    await _seed_memory(
        tenant_id=tenant,
        agent_id="a2",
        content="agent2 view",
        subject_entity_id=ent,
        object_value="v2",
    )
    rows = await sc.insights_query_divergence(
        tenant_id=tenant,
        fleet_id=None,
        agent_id="a1",
        scope="all",
        max_memories=INSIGHTS_MAX_MEMORIES,
    )
    assert {r["content"] for r in rows} == {"agent1 view", "agent2 view"}


async def test_divergence_empty_when_single_agent(sc):
    tenant = _t()
    ent = str(uuid4())
    # Only one agent references the entity → no divergence → [].
    await _seed_memory(
        tenant_id=tenant,
        agent_id="a1",
        content="only one",
        subject_entity_id=ent,
        object_value="v",
    )
    rows = await sc.insights_query_divergence(
        tenant_id=tenant,
        fleet_id=None,
        agent_id="a1",
        scope="all",
        max_memories=INSIGHTS_MAX_MEMORIES,
    )
    assert rows == []


async def test_discover_sample_includes_embedding(sc):
    tenant = _t()
    emb = [0.1] * VECTOR_DIM
    await _seed_memory(
        tenant_id=tenant, agent_id="a1", content="with-emb", embedding=emb
    )
    # No embedding → excluded.
    await _seed_memory(
        tenant_id=tenant, agent_id="a1", content="no-emb", embedding=None
    )
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
        tenant_id=tenant,
        agent_id=agent,
        memory_type="insight",
        content="prior match",
        metadata={"insight_focus": "patterns", "insight_scope": "agent"},
    )
    # Different focus → must NOT be outdated.
    other_focus = await _seed_memory(
        tenant_id=tenant,
        agent_id=agent,
        memory_type="insight",
        content="other focus",
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
        tenant_id=tenant,
        agent_id=agent,
        fleet_id=None,
        memory_type="insight",
        content="fleetless",
        metadata={"insight_focus": "patterns", "insight_scope": "all"},
    )
    fleeted = await _seed_memory(
        tenant_id=tenant,
        agent_id=agent,
        fleet_id="f1",
        memory_type="insight",
        content="fleeted",
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
        tenant_id=t_a,
        agent_id="a1",
        memory_type="insight",
        content="A",
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
        tenant_id=tenant,
        agent_id=agent,
        memory_type="insight",
        content="p",
        status="outdated",
        metadata={"insight_focus": "patterns", "insight_scope": "agent"},
    )
    res = await sc.insights_restore_priors(tenant_id=tenant, prior_ids=[prior])
    assert res["restored"] == 1
    assert await _status(prior) == "active"


async def test_restore_priors_tenant_isolation(sc):
    t_a, t_b = _t(), _t()
    prior = await _seed_memory(
        tenant_id=t_a,
        agent_id="a1",
        memory_type="insight",
        content="p",
        status="outdated",
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
        tenant_id=tenant,
        agent_id="a1",
        memory_type="fact",
        content="f",
        created_at=now - timedelta(hours=2),
    )
    await _seed_memory(
        tenant_id=tenant,
        agent_id="a1",
        memory_type="insight",
        content="i",
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


async def test_discover_naive_window_start_422(storage_http):
    # created_at is timestamptz — a naive bound would silently shift the
    # window by the server's UTC offset, so the router rejects it.
    resp = await storage_http.post(
        "/api/v1/storage/insights/discover-sample",
        json={
            "tenant_id": "t",
            "agent_id": "a1",
            "scope": "agent",
            "sample_size": 200,
            "window_start": "2026-07-01T02:00:00",
        },
    )
    assert resp.status_code == 422
    assert "timezone-aware" in resp.json()["detail"]


async def test_discover_invalid_window_start_422(storage_http):
    resp = await storage_http.post(
        "/api/v1/storage/insights/discover-sample",
        json={
            "tenant_id": "t",
            "agent_id": "a1",
            "scope": "agent",
            "sample_size": 200,
            "window_start": "not-a-date",
        },
    )
    assert resp.status_code == 422


async def test_empty_string_window_start_422(storage_http):
    # Only None/absent means "no window" (legacy fallback); an explicit ""
    # is a caller bug and must 422 like any other invalid value, not
    # silently run the unbounded read.
    resp = await storage_http.post(
        "/api/v1/storage/insights/patterns",
        json={
            "tenant_id": "t",
            "agent_id": "a1",
            "scope": "agent",
            "max_memories": 50,
            "window_start": "",
        },
    )
    assert resp.status_code == 422
    assert "Invalid ISO datetime" in resp.json()["detail"]


async def test_patterns_naive_window_start_422(storage_http):
    # patterns shares the _window_start guard with discover-sample.
    resp = await storage_http.post(
        "/api/v1/storage/insights/patterns",
        json={
            "tenant_id": "t",
            "agent_id": "a1",
            "scope": "agent",
            "max_memories": 50,
            "window_start": "2026-07-01T02:00:00",
        },
    )
    assert resp.status_code == 422
    assert "timezone-aware" in resp.json()["detail"]


async def test_supersede_missing_focus_422(storage_http):
    resp = await storage_http.post(
        "/api/v1/storage/insights/supersede-priors",
        json={"tenant_id": "t", "agent_id": "a1", "scope": "agent"},
    )
    assert resp.status_code == 422


async def test_activity_gate_missing_tenant_422(storage_http):
    resp = await storage_http.post("/api/v1/storage/insights/activity-gate", json={})
    assert resp.status_code == 422


# ===========================================================================
# E. Input noise filter — opaque-payload exclusion, title-dedup, discover window
# ===========================================================================

# Content-free reasoning artifact (empty thinking block + encrypted signature):
# matches PostgresService._INSIGHTS_OPAQUE_CONTENT_PREFIX and must be invisible
# to every insights read, while staying untouched in storage.
_OPAQUE = '[{"type":"thinking","thinking":"","thinkingSignature":"rs_abc123"}]'

_EMB = [0.0] * VECTOR_DIM


async def test_opaque_payload_excluded_from_all_reads(sc):
    tenant = _t()
    now = datetime.now(UTC)
    old = now - timedelta(days=40)
    # One opaque row shaped to qualify for EVERY read: active, low weight,
    # recalled, old, embedded. Plus a second conflicted opaque row for the
    # contradictions candidate set.
    await _seed_memory(
        tenant_id=tenant,
        agent_id="a1",
        content=_OPAQUE,
        title="Encrypted reasoning payload stored",
        memory_type="episode",
        weight=0.1,
        recall_count=3,
        created_at=old,
        embedding=_EMB,
    )
    await _seed_memory(
        tenant_id=tenant,
        agent_id="a1",
        content=_OPAQUE,
        title="Encrypted reasoning payload stored",
        memory_type="episode",
        status="conflicted",
        weight=0.1,
        created_at=old,
    )
    keep_id = await _seed_memory(
        tenant_id=tenant,
        agent_id="a1",
        content="real work happened",
        title="Deployed rate-limit fix",
        memory_type="episode",
        weight=0.1,
        recall_count=3,
        created_at=old,
        embedding=_EMB,
    )

    patterns = await sc.insights_query_patterns(
        tenant_id=tenant,
        fleet_id=None,
        agent_id="a1",
        scope="agent",
        max_memories=INSIGHTS_MAX_MEMORIES,
    )
    assert {r["id"] for r in patterns} == {keep_id}

    failures = await sc.insights_query_failures(
        tenant_id=tenant,
        fleet_id=None,
        agent_id="a1",
        scope="agent",
        max_memories=INSIGHTS_MAX_MEMORIES,
    )
    assert {r["id"] for r in failures} == {keep_id}

    stale = await sc.insights_query_stale(
        tenant_id=tenant,
        fleet_id=None,
        agent_id="a1",
        scope="agent",
        thirty_days_ago=now - timedelta(days=30),
        fourteen_days_ago=now - timedelta(days=14),
        max_memories=INSIGHTS_MAX_MEMORIES,
    )
    assert {r["id"] for r in stale} == {keep_id}

    discover = await sc.insights_discover_sample(
        tenant_id=tenant,
        fleet_id=None,
        agent_id="a1",
        scope="agent",
        sample_size=INSIGHTS_DISCOVER_SAMPLE_SIZE,
    )
    assert {r["id"] for r in discover} == {keep_id}

    contradictions = await sc.insights_query_contradictions(
        tenant_id=tenant,
        fleet_id=None,
        agent_id="a1",
        scope="agent",
        max_memories=INSIGHTS_MAX_MEMORIES,
    )
    assert all(r["content"] != _OPAQUE for r in contradictions)


async def test_patterns_dedup_one_exemplar_per_title_with_annotations(sc):
    tenant = _t()
    now = datetime.now(UTC)
    oldest = now - timedelta(days=5)
    # 4 same-titled routine rows (newest wins) + 1 distinct row.
    dup_ids = []
    for i in range(4):
        dup_ids.append(
            await _seed_memory(
                tenant_id=tenant,
                agent_id="a1",
                content=f"heartbeat run {i}",
                title="Heartbeat OK",
                memory_type="episode",
                created_at=oldest + timedelta(days=i),
            )
        )
    solo_id = await _seed_memory(
        tenant_id=tenant,
        agent_id="a1",
        content="fixed the flaky test",
        title="Fixed flaky auth test",
        created_at=now - timedelta(days=1),
    )

    # Dedup runs on the windowed path (window_start=None is the legacy
    # pre-dedup query).
    rows = await sc.insights_query_patterns(
        tenant_id=tenant,
        fleet_id=None,
        agent_id="a1",
        scope="agent",
        max_memories=INSIGHTS_MAX_MEMORIES,
        window_start=now - timedelta(days=30),
    )
    by_id = {r["id"]: r for r in rows}
    # One exemplar per title: the NEWEST of the dup group + the solo row.
    assert set(by_id) == {dup_ids[-1], solo_id}
    exemplar = by_id[dup_ids[-1]]
    assert exemplar["dup_count"] == 4
    assert datetime.fromisoformat(exemplar["first_seen"]) == oldest
    assert by_id[solo_id]["dup_count"] == 1


async def test_dedup_null_and_empty_titles_not_collapsed(sc):
    tenant = _t()
    ids = set()
    for i, title in enumerate([None, None, "", ""]):
        ids.add(
            await _seed_memory(
                tenant_id=tenant,
                agent_id="a1",
                content=f"untitled {i}",
                title=title,
                created_at=datetime.now(UTC) - timedelta(minutes=i),
            )
        )
    rows = await sc.insights_query_patterns(
        tenant_id=tenant,
        fleet_id=None,
        agent_id="a1",
        scope="agent",
        max_memories=INSIGHTS_MAX_MEMORIES,
        window_start=datetime.now(UTC) - timedelta(days=30),
    )
    # NULL/'' titles fall back to the row id as dedup key — all rows survive.
    assert {r["id"] for r in rows} == ids
    assert all(r["dup_count"] == 1 for r in rows)


async def test_contradictions_not_deduped_same_title_pair_survives(sc):
    tenant = _t()
    # A supersede pair sharing a title: same-titled disagreement is exactly
    # what contradictions mode must see — dedup must NOT apply here.
    old_id = await _seed_memory(
        tenant_id=tenant,
        agent_id="a1",
        content="endpoint is https://old.example",
        title="Gateway endpoint",
        created_at=datetime.now(UTC) - timedelta(days=2),
    )
    new_id = await _seed_memory(
        tenant_id=tenant,
        agent_id="a1",
        content="endpoint is https://new.example",
        title="Gateway endpoint",
        supersedes_id=old_id,
        created_at=datetime.now(UTC) - timedelta(days=1),
    )
    rows = await sc.insights_query_contradictions(
        tenant_id=tenant,
        fleet_id=None,
        agent_id="a1",
        scope="agent",
        max_memories=INSIGHTS_MAX_MEMORIES,
    )
    assert {old_id, new_id} <= {r["id"] for r in rows}


async def test_discover_window_bounds_and_legacy_fallback(sc):
    tenant = _t()
    now = datetime.now(UTC)
    in_window = await _seed_memory(
        tenant_id=tenant,
        agent_id="a1",
        content="recent embedded",
        title="Recent work",
        created_at=now - timedelta(days=3),
        embedding=_EMB,
    )
    out_of_window = await _seed_memory(
        tenant_id=tenant,
        agent_id="a1",
        content="ancient embedded",
        title="Ancient work",
        created_at=now - timedelta(days=90),
        embedding=_EMB,
    )

    windowed = await sc.insights_discover_sample(
        tenant_id=tenant,
        fleet_id=None,
        agent_id="a1",
        scope="agent",
        sample_size=INSIGHTS_DISCOVER_SAMPLE_SIZE,
        window_start=now - timedelta(days=30),
    )
    assert {r["id"] for r in windowed} == {in_window}
    assert windowed[0]["embedding"] is not None

    # window_start omitted → legacy newest-first behaviour, both rows visible
    # (back-compat for an older core-api against a newer storage).
    legacy = await sc.insights_discover_sample(
        tenant_id=tenant,
        fleet_id=None,
        agent_id="a1",
        scope="agent",
        sample_size=INSIGHTS_DISCOVER_SAMPLE_SIZE,
    )
    assert {r["id"] for r in legacy} == {in_window, out_of_window}


async def test_patterns_window_bounds_and_legacy_fallback(sc):
    tenant = _t()
    now = datetime.now(UTC)
    in_window = await _seed_memory(
        tenant_id=tenant,
        agent_id="a1",
        content="recent pattern",
        title="Recent work",
        created_at=now - timedelta(days=3),
    )
    out_of_window = await _seed_memory(
        tenant_id=tenant,
        agent_id="a1",
        content="ancient pattern",
        title="Ancient work",
        created_at=now - timedelta(days=90),
    )

    windowed = await sc.insights_query_patterns(
        tenant_id=tenant,
        fleet_id=None,
        agent_id="a1",
        scope="agent",
        max_memories=INSIGHTS_MAX_MEMORIES,
        window_start=now - timedelta(days=30),
    )
    assert {r["id"] for r in windowed} == {in_window}

    # window_start omitted → the ORIGINAL pre-dedup query (back-compat for
    # an older core-api against a newer storage): unbounded, un-annotated,
    # no window-function scan.
    legacy = await sc.insights_query_patterns(
        tenant_id=tenant,
        fleet_id=None,
        agent_id="a1",
        scope="agent",
        max_memories=INSIGHTS_MAX_MEMORIES,
    )
    assert {r["id"] for r in legacy} == {in_window, out_of_window}
    assert all("dup_count" not in r for r in legacy)


async def test_failures_window_bounds_and_legacy_fallback(sc):
    tenant = _t()
    now = datetime.now(UTC)
    in_window = await _seed_memory(
        tenant_id=tenant,
        agent_id="a1",
        content="recent weak recall",
        title="Recent weak",
        weight=0.1,
        recall_count=2,
        created_at=now - timedelta(days=10),
    )
    out_of_window = await _seed_memory(
        tenant_id=tenant,
        agent_id="a1",
        content="ancient weak recall",
        title="Ancient weak",
        weight=0.1,
        recall_count=2,
        created_at=now - timedelta(days=120),
    )

    windowed = await sc.insights_query_failures(
        tenant_id=tenant,
        fleet_id=None,
        agent_id="a1",
        scope="agent",
        max_memories=INSIGHTS_MAX_MEMORIES,
        window_start=now - timedelta(days=90),
    )
    assert {r["id"] for r in windowed} == {in_window}

    legacy = await sc.insights_query_failures(
        tenant_id=tenant,
        fleet_id=None,
        agent_id="a1",
        scope="agent",
        max_memories=INSIGHTS_MAX_MEMORIES,
    )
    assert {r["id"] for r in legacy} == {in_window, out_of_window}


async def test_stale_window_bounds_and_legacy_fallback(sc):
    tenant = _t()
    now = datetime.now(UTC)
    # Stale rows must be OLDER than the 30-day threshold to qualify, so the
    # window (90d) is deliberately wider: the windowed read reports the
    # 30-90-day "recently became stale" band.
    in_band = await _seed_memory(
        tenant_id=tenant,
        agent_id="a1",
        content="became stale recently",
        title="Recently stale",
        recall_count=0,
        created_at=now - timedelta(days=40),
    )
    ancient = await _seed_memory(
        tenant_id=tenant,
        agent_id="a1",
        content="ancient never recalled",
        title="Ancient stale",
        recall_count=0,
        created_at=now - timedelta(days=200),
    )

    windowed = await sc.insights_query_stale(
        tenant_id=tenant,
        fleet_id=None,
        agent_id="a1",
        scope="agent",
        thirty_days_ago=now - timedelta(days=30),
        fourteen_days_ago=now - timedelta(days=14),
        max_memories=INSIGHTS_MAX_MEMORIES,
        window_start=now - timedelta(days=90),
    )
    assert {r["id"] for r in windowed} == {in_band}

    legacy = await sc.insights_query_stale(
        tenant_id=tenant,
        fleet_id=None,
        agent_id="a1",
        scope="agent",
        thirty_days_ago=now - timedelta(days=30),
        fourteen_days_ago=now - timedelta(days=14),
        max_memories=INSIGHTS_MAX_MEMORIES,
    )
    assert {r["id"] for r in legacy} == {in_band, ancient}


async def test_failures_dedup_keeps_mode_ordering(sc):
    tenant = _t()
    now = datetime.now(UTC)
    # Two dup groups with different recall_count; failures orders by
    # recall_count DESC — the ordering applies to the deduped exemplars.
    for i in range(3):
        await _seed_memory(
            tenant_id=tenant,
            agent_id="a1",
            content=f"blob {i}",
            title="Opaque-ish recalled blob",
            weight=0.1,
            recall_count=9,
            created_at=now - timedelta(hours=i + 1),
        )
    for i in range(2):
        await _seed_memory(
            tenant_id=tenant,
            agent_id="a1",
            content=f"weak {i}",
            title="Weak evidence row",
            weight=0.2,
            recall_count=2,
            created_at=now - timedelta(hours=i + 1),
        )
    rows = await sc.insights_query_failures(
        tenant_id=tenant,
        fleet_id=None,
        agent_id="a1",
        scope="agent",
        max_memories=INSIGHTS_MAX_MEMORIES,
        window_start=now - timedelta(days=90),
    )
    assert [r["title"] for r in rows] == [
        "Opaque-ish recalled blob",
        "Weak evidence row",
    ]
    assert [r["dup_count"] for r in rows] == [3, 2]


async def test_failures_dupcount_breaks_recall_ties(sc):
    tenant = _t()
    now = datetime.now(UTC)
    # The exemplar is the NEWEST row per title, whose individual
    # recall_count may understate a frequent pattern — dup_count is the
    # secondary sort key so, at equal exemplar recall_count, the
    # 3-instance pattern outranks the one-off and isn't cut by LIMIT.
    for i in range(3):
        await _seed_memory(
            tenant_id=tenant,
            agent_id="a1",
            content=f"frequent {i}",
            title="Frequent weak pattern",
            weight=0.1,
            recall_count=5,
            created_at=now - timedelta(hours=i + 1),
        )
    await _seed_memory(
        tenant_id=tenant,
        agent_id="a1",
        content="one-off",
        title="One-off weak row",
        weight=0.1,
        recall_count=5,
        created_at=now - timedelta(hours=1),
    )
    rows = await sc.insights_query_failures(
        tenant_id=tenant,
        fleet_id=None,
        agent_id="a1",
        scope="agent",
        max_memories=INSIGHTS_MAX_MEMORIES,
        window_start=now - timedelta(days=90),
    )
    assert [r["title"] for r in rows] == [
        "Frequent weak pattern",
        "One-off weak row",
    ]

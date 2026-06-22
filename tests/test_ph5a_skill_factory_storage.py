"""Fix 2 Ph5a — skill-factory pipeline routed through core-storage-api.

Exercises the new core-storage-api endpoints via the typed storage client
(bridged in-process to the storage app by the conftest ASGI fixture, against
the test DB):

- POST /documents/update-status                 (sc.update_document_status)  — CAS
- POST /forge/rejected-fingerprints             (sc.forge_write_rejected_fingerprint)
- POST /forge/rejected-fingerprints/check       (sc.forge_is_fingerprint_poisoned)
- POST /session-traces/upsert                   (sc.upsert_session_traces)   — ON CONFLICT
- POST /session-traces/memories-window          (sc.session_memories_in_window)
- POST /session-traces/entity-links             (sc.session_trace_entity_links)
- POST /forge/memories-content                  (sc.forge_memory_content_by_ids)
- POST /outcome-signals/contradictions          (sc.outcome_contradiction_signals)
- POST /outcome-signals/supersessions           (sc.outcome_supersession_signals)
- POST /outcome-signals/cross-agent-reuse       (sc.outcome_cross_agent_reuse_signals)
- POST /outcome-signals/terminal-memory         (sc.outcome_terminal_memory_signals)

Rows are seeded via the storage write paths (committed, independent session)
— ``sc.upsert_document`` for documents, ``sc.create_memory`` for memories,
and ``PostgresService`` directly for ``session_traces`` /
``forge_rejected_fingerprints`` (no client seed path), plus the raw text
helpers for the memory columns (status / recall_count / supersedes_id) the
public create endpoint doesn't expose. A unique tenant per test keeps
concurrent suite runs isolated.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from core_storage_api.services.postgres_service import (
    PostgresService,
    get_session,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _t() -> str:
    """Unique tenant id per test so concurrent suite runs don't collide."""
    return f"test-tenant-ph5a-{uuid4().hex[:8]}"


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ---------------------------------------------------------------------------
# Memory seeding helper — the public create endpoint doesn't expose
# run_id / status / recall_count / supersedes_id / updated_at, so seed the
# columns the analytic reads filter on via a raw INSERT in a committed
# (independent) session, mirroring the storage write path.
# ---------------------------------------------------------------------------


async def _seed_memory(
    *,
    tenant_id: str,
    content: str = "x",
    run_id: str | None = None,
    agent_id: str = "agent-1",
    fleet_id: str | None = None,
    status: str = "active",
    recall_count: int = 0,
    created_at: datetime | None = None,
    last_recalled_at: datetime | None = None,
    supersedes_id: str | None = None,
) -> str:
    # NOTE: the OSS ``memories`` table has no ``updated_at`` column — the
    # contradiction read windows on ``created_at`` (see the DEVIATION note
    # in PostgresService.outcome_contradiction_signals), so seeding uses
    # ``created_at`` for the status-transition time too.
    created = created_at or datetime.now(UTC)
    mem_id = str(uuid4())
    async with get_session() as session:
        await session.execute(
            text(
                """
                INSERT INTO memories
                    (id, tenant_id, fleet_id, agent_id, run_id, content,
                     memory_type, status, recall_count, created_at,
                     last_recalled_at, supersedes_id)
                VALUES
                    (CAST(:id AS uuid), :tenant_id, :fleet_id, :agent_id, :run_id, :content,
                     'fact', :status, :recall_count, :created_at,
                     :last_recalled_at, CAST(:supersedes_id AS uuid))
                """
            ),
            {
                "id": mem_id,
                "tenant_id": tenant_id,
                "fleet_id": fleet_id,
                "agent_id": agent_id,
                "run_id": run_id,
                "content": content,
                "status": status,
                "recall_count": recall_count,
                "created_at": created,
                "last_recalled_at": last_recalled_at,
                "supersedes_id": supersedes_id,
            },
        )
    return mem_id


async def _seed_entity_link(*, memory_id: str, entity_id: str, tenant_id: str) -> None:
    """Seed an entity + a memory→entity link via raw INSERT (committed).

    Columns match the ``entities`` / ``memory_entity_links`` models:
    entities has ``entity_type`` + ``canonical_name`` (no ``name`` /
    ``created_at``); the link table has only ``memory_id, entity_id, role``.
    """
    async with get_session() as session:
        await session.execute(
            text(
                """
                INSERT INTO entities (id, tenant_id, entity_type, canonical_name)
                VALUES (CAST(:id AS uuid), :tenant_id, 'concept', :name)
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {"id": entity_id, "tenant_id": tenant_id, "name": f"ent-{entity_id[:6]}"},
        )
        await session.execute(
            text(
                """
                INSERT INTO memory_entity_links (memory_id, entity_id, role)
                VALUES (CAST(:memory_id AS uuid), CAST(:entity_id AS uuid), 'mention')
                ON CONFLICT DO NOTHING
                """
            ),
            {"memory_id": memory_id, "entity_id": entity_id},
        )


# ===========================================================================
# A. POST /documents/update-status — conditional CAS
# ===========================================================================


async def _seed_doc(sc, tenant, doc_id, *, status, fleet_id=None):
    payload = {
        "tenant_id": tenant,
        "collection": "skills",
        "doc_id": doc_id,
        "data": {"status": status, "source": "forge", "created_at": "2026-05-01T00:00:00+00:00"},
    }
    if fleet_id is not None:
        payload["fleet_id"] = fleet_id
    return await sc.upsert_document(payload)


async def test_update_status_cas_hit(sc):
    tenant = _t()
    await _seed_doc(sc, tenant, "forge/skill-1", status="candidate")

    result = await sc.update_document_status(
        tenant_id=tenant,
        collection="skills",
        doc_id="forge/skill-1",
        new_status="staged",
        expected_status="candidate",
    )
    assert result is not None
    assert result["updated"] is True
    assert result["doc_id"] == "forge/skill-1"

    # The status flipped and ``staged_at`` was stamped.
    doc = await sc.get_document(tenant_id=tenant, collection="skills", doc_id="forge/skill-1", read=False)
    assert doc["data"]["status"] == "staged"
    assert "staged_at" in doc["data"]


async def test_update_status_cas_miss_returns_none(sc):
    # Doc is already ``staged`` — a CAS narrowed on ``candidate`` matches
    # zero rows → storage 404 → client returns None (caller raises
    # AlreadyTransitionedError).
    tenant = _t()
    await _seed_doc(sc, tenant, "forge/skill-2", status="staged")

    result = await sc.update_document_status(
        tenant_id=tenant,
        collection="skills",
        doc_id="forge/skill-2",
        new_status="staged",
        expected_status="candidate",
    )
    assert result is None
    # Status unchanged.
    doc = await sc.get_document(tenant_id=tenant, collection="skills", doc_id="forge/skill-2", read=False)
    assert doc["data"]["status"] == "staged"


async def test_update_status_tenant_isolation(sc):
    # Tenant B cannot flip tenant A's doc by id+collection.
    t_a, t_b = _t(), _t()
    await _seed_doc(sc, t_a, "forge/shared", status="candidate")

    result = await sc.update_document_status(
        tenant_id=t_b,
        collection="skills",
        doc_id="forge/shared",
        new_status="staged",
        expected_status="candidate",
    )
    assert result is None
    doc = await sc.get_document(tenant_id=t_a, collection="skills", doc_id="forge/shared", read=False)
    assert doc["data"]["status"] == "candidate", "tenant A's doc untouched"


async def test_update_status_missing_tenant_422(storage_http):
    resp = await storage_http.post(
        "/api/v1/storage/documents/update-status",
        json={"collection": "skills", "doc_id": "x", "new_status": "staged", "expected_status": "candidate"},
    )
    assert resp.status_code == 422


# ===========================================================================
# B. Forge rejected fingerprints — write + cooloff-window check
# ===========================================================================


async def test_fingerprint_write_then_poisoned(sc):
    tenant = _t()
    fp = f"fp:v1:{uuid4().hex}"
    new_id = await sc.forge_write_rejected_fingerprint(
        tenant_id=tenant,
        fleet_id=None,
        cluster_fingerprint=fp,
        rejected_by_agent="human:eldad",
        cooloff_days=30,
        reason="not useful",
    )
    assert new_id and new_id != "unknown"

    poisoned = await sc.forge_is_fingerprint_poisoned(
        tenant_id=tenant, fleet_id=None, cluster_fingerprint=fp
    )
    assert poisoned is True


async def test_fingerprint_not_poisoned_when_absent(sc):
    tenant = _t()
    poisoned = await sc.forge_is_fingerprint_poisoned(
        tenant_id=tenant, fleet_id=None, cluster_fingerprint=f"fp:v1:{uuid4().hex}"
    )
    assert poisoned is False


async def test_fingerprint_cooloff_window_expired(sc):
    # A rejection whose ``rejected_at + cooloff_days`` is in the past must
    # NOT poison. Seed via PostgresService raw INSERT with a backdated
    # rejected_at (the public write path always uses now()).
    tenant = _t()
    fp = f"fp:v1:{uuid4().hex}"
    async with get_session() as session:
        await session.execute(
            text(
                """
                INSERT INTO forge_rejected_fingerprints
                    (tenant_id, fleet_id, cluster_fingerprint, rejected_by_agent,
                     rejected_at, cooloff_days, reason)
                VALUES
                    (:tenant_id, NULL, :fp, 'human:eldad',
                     now() - interval '40 days', 30, 'stale')
                """
            ),
            {"tenant_id": tenant, "fp": fp},
        )
    poisoned = await sc.forge_is_fingerprint_poisoned(
        tenant_id=tenant, fleet_id=None, cluster_fingerprint=fp
    )
    assert poisoned is False, "40d-old rejection with 30d cooloff has expired"


async def test_fingerprint_fleet_predicate(sc):
    # Three-arm fleet predicate: a fleet-A rejection blocks a fleet-A scan
    # AND a tenant-wide scan, but NOT a fleet-B scan.
    tenant = _t()
    fp = f"fp:v1:{uuid4().hex}"
    await sc.forge_write_rejected_fingerprint(
        tenant_id=tenant,
        fleet_id="fleet-A",
        cluster_fingerprint=fp,
        rejected_by_agent="human:eldad",
        cooloff_days=30,
    )
    # Same fleet → poisoned.
    assert await sc.forge_is_fingerprint_poisoned(tenant_id=tenant, fleet_id="fleet-A", cluster_fingerprint=fp)
    # Tenant-wide scan (fleet_id=None) → poisoned (the :fleet_id IS NULL arm).
    assert await sc.forge_is_fingerprint_poisoned(tenant_id=tenant, fleet_id=None, cluster_fingerprint=fp)
    # Different fleet → NOT poisoned.
    assert not await sc.forge_is_fingerprint_poisoned(
        tenant_id=tenant, fleet_id="fleet-B", cluster_fingerprint=fp
    )


async def test_fingerprint_tenant_isolation(sc):
    t_a, t_b = _t(), _t()
    fp = f"fp:v1:{uuid4().hex}"
    await sc.forge_write_rejected_fingerprint(
        tenant_id=t_a, fleet_id=None, cluster_fingerprint=fp, rejected_by_agent="h", cooloff_days=30
    )
    assert await sc.forge_is_fingerprint_poisoned(tenant_id=t_a, fleet_id=None, cluster_fingerprint=fp)
    assert not await sc.forge_is_fingerprint_poisoned(tenant_id=t_b, fleet_id=None, cluster_fingerprint=fp)


# ===========================================================================
# C. Session traces — batch upsert + ON CONFLICT refresh
# ===========================================================================


def _trace(run_id: str, agent_id: str, *, label: str = "success", fleet_id=None) -> dict:
    now = datetime.now(UTC)
    return {
        "fleet_id": fleet_id,
        "run_id": run_id,
        "agent_id": agent_id,
        "outcome_label": label,
        "memory_ids": [str(uuid4())],
        "entity_ids": [str(uuid4())],
        "signals_summary": {"terminal_memory": {"firings": []}},
        "goal_phrase": None,
        "started_at": _iso(now - timedelta(hours=1)),
        "ended_at": _iso(now),
    }


async def _count_traces(tenant: str, run_id: str, agent_id: str) -> int:
    async with get_session() as session:
        row = (
            await session.execute(
                text(
                    "SELECT COUNT(*) AS c FROM session_traces "
                    "WHERE tenant_id = :t AND run_id = :r AND agent_id = :a"
                ),
                {"t": tenant, "r": run_id, "a": agent_id},
            )
        ).fetchone()
    return int(row.c)


async def _trace_label(tenant: str, run_id: str, agent_id: str) -> str:
    async with get_session() as session:
        row = (
            await session.execute(
                text(
                    "SELECT outcome_label AS l FROM session_traces "
                    "WHERE tenant_id = :t AND run_id = :r AND agent_id = :a"
                ),
                {"t": tenant, "r": run_id, "a": agent_id},
            )
        ).fetchone()
    return row.l


async def test_session_traces_batch_insert(sc):
    tenant = _t()
    await sc.upsert_session_traces(
        tenant_id=tenant,
        traces=[_trace("run-1", "agent-1"), _trace("run-2", "agent-1")],
    )
    assert await _count_traces(tenant, "run-1", "agent-1") == 1
    assert await _count_traces(tenant, "run-2", "agent-1") == 1


async def test_session_traces_on_conflict_refreshes(sc):
    # Re-upserting the same (tenant, run_id, agent_id) updates in place
    # (one row), refreshing outcome_label — idempotent forge re-runs.
    tenant = _t()
    await sc.upsert_session_traces(tenant_id=tenant, traces=[_trace("run-x", "agent-1", label="unknown")])
    await sc.upsert_session_traces(tenant_id=tenant, traces=[_trace("run-x", "agent-1", label="success")])
    assert await _count_traces(tenant, "run-x", "agent-1") == 1, "ON CONFLICT collapses to one row"
    assert await _trace_label(tenant, "run-x", "agent-1") == "success", "label refreshed"


async def test_session_traces_tenant_forced(sc):
    # The batch tenant_id is forced onto every row server-side: even if a
    # caller smuggled a foreign tenant into a trace dict, the row lands
    # under the batch tenant.
    tenant = _t()
    smuggled = _trace("run-s", "agent-1")
    smuggled["tenant_id"] = "some-other-tenant"  # ignored server-side
    await sc.upsert_session_traces(tenant_id=tenant, traces=[smuggled])
    assert await _count_traces(tenant, "run-s", "agent-1") == 1
    assert await _count_traces("some-other-tenant", "run-s", "agent-1") == 0


async def test_session_traces_empty_batch_noop(sc):
    tenant = _t()
    await sc.upsert_session_traces(tenant_id=tenant, traces=[])  # no error


# ===========================================================================
# D. Bespoke memory-window / signal reads
# ===========================================================================


async def test_session_memories_in_window(sc):
    tenant = _t()
    now = datetime.now(UTC)
    # In-window, run-scoped → returned.
    await _seed_memory(tenant_id=tenant, run_id="run-A", agent_id="a1", created_at=now - timedelta(hours=2))
    await _seed_memory(tenant_id=tenant, run_id="run-A", agent_id="a1", created_at=now - timedelta(hours=1))
    # No run_id → SKIPPED.
    await _seed_memory(tenant_id=tenant, run_id=None, agent_id="a1", created_at=now - timedelta(hours=1))
    # Out of window → excluded.
    await _seed_memory(tenant_id=tenant, run_id="run-B", agent_id="a1", created_at=now - timedelta(days=40))

    rows = await sc.session_memories_in_window(
        tenant_id=tenant,
        fleet_id=None,
        window_start=now - timedelta(days=1),
        window_end=now + timedelta(minutes=1),
    )
    by_run = {r["run_id"] for r in rows}
    assert by_run == {"run-A"}, "only in-window run-scoped memories"
    assert len(rows) == 2


async def test_session_memories_window_tenant_isolation(sc):
    t_a, t_b = _t(), _t()
    now = datetime.now(UTC)
    await _seed_memory(tenant_id=t_a, run_id="run-A", created_at=now)
    await _seed_memory(tenant_id=t_b, run_id="run-B", created_at=now)
    rows = await sc.session_memories_in_window(
        tenant_id=t_a, fleet_id=None, window_start=now - timedelta(days=1), window_end=now + timedelta(minutes=1)
    )
    assert {r["run_id"] for r in rows} == {"run-A"}


async def test_entity_links_batch(sc):
    tenant = _t()
    m1 = await _seed_memory(tenant_id=tenant, run_id="r", content="m1")
    m2 = await _seed_memory(tenant_id=tenant, run_id="r", content="m2")
    e1, e2 = str(uuid4()), str(uuid4())
    await _seed_entity_link(memory_id=m1, entity_id=e1, tenant_id=tenant)
    await _seed_entity_link(memory_id=m1, entity_id=e2, tenant_id=tenant)
    await _seed_entity_link(memory_id=m2, entity_id=e1, tenant_id=tenant)

    rows = await sc.session_trace_entity_links(tenant_id=tenant, memory_ids=[m1, m2])
    pairs = {(r["memory_id"], r["entity_id"]) for r in rows}
    assert pairs == {(m1, e1), (m1, e2), (m2, e1)}
    # Wrong tenant sees nothing — the join scopes links to the owning tenant.
    assert await sc.session_trace_entity_links(tenant_id=_t(), memory_ids=[m1, m2]) == []


async def test_entity_links_batch_empty(sc):
    rows = await sc.session_trace_entity_links(tenant_id=_t(), memory_ids=[])
    assert rows == []


async def test_forge_memory_content_by_ids(sc):
    tenant = _t()
    m1 = await _seed_memory(tenant_id=tenant, run_id="r", content="hello")
    m2 = await _seed_memory(tenant_id=tenant, run_id="r", content="world")
    rows = await sc.forge_memory_content_by_ids(tenant_id=tenant, memory_ids=[m1, m2])
    by_id = {r["id"]: r["content"] for r in rows}
    assert by_id == {m1: "hello", m2: "world"}
    # Wrong tenant sees nothing.
    assert await sc.forge_memory_content_by_ids(tenant_id=_t(), memory_ids=[m1, m2]) == []


async def test_outcome_contradiction_signals(sc):
    tenant = _t()
    now = datetime.now(UTC)
    # outdated within window → returned.
    await _seed_memory(
        tenant_id=tenant, run_id="run-A", status="outdated",
        created_at=now - timedelta(hours=2),
    )
    # active → excluded.
    await _seed_memory(tenant_id=tenant, run_id="run-B", status="active", created_at=now - timedelta(hours=2))
    rows = await sc.outcome_contradiction_signals(
        tenant_id=tenant,
        fleet_id=None,
        window_start=now - timedelta(days=1),
        window_end=now + timedelta(minutes=1),
        contradicted_statuses=["outdated", "conflicted"],
    )
    assert {r["run_id"] for r in rows} == {"run-A"}
    assert rows[0]["status"] == "outdated"


async def test_outcome_supersession_signals(sc):
    tenant = _t()
    now = datetime.now(UTC)
    old = await _seed_memory(
        tenant_id=tenant, run_id="run-old", agent_id="a-old",
        created_at=now - timedelta(days=5),
    )
    new = await _seed_memory(
        tenant_id=tenant, run_id="run-new", agent_id="a-new",
        created_at=now - timedelta(hours=1), supersedes_id=old,
    )
    rows = await sc.outcome_supersession_signals(
        tenant_id=tenant,
        fleet_id=None,
        window_start=now - timedelta(days=1),
        window_end=now + timedelta(minutes=1),
    )
    assert len(rows) == 1
    # FAILURE lands on the OLD (superseded) memory's trace.
    assert rows[0]["superseded_id"] == old
    assert rows[0]["by_id"] == new
    assert rows[0]["run_id"] == "run-old"


async def test_outcome_cross_agent_reuse_signals(sc):
    tenant = _t()
    now = datetime.now(UTC)
    await _seed_memory(
        tenant_id=tenant, run_id="run-hot", recall_count=8, created_at=now - timedelta(hours=2)
    )
    await _seed_memory(
        tenant_id=tenant, run_id="run-cold", recall_count=2, created_at=now - timedelta(hours=2)
    )
    rows = await sc.outcome_cross_agent_reuse_signals(
        tenant_id=tenant,
        fleet_id=None,
        window_start=now - timedelta(days=1),
        window_end=now + timedelta(minutes=1),
        threshold=5,
    )
    assert {r["run_id"] for r in rows} == {"run-hot"}
    assert rows[0]["recall_count"] == 8


async def test_outcome_terminal_memory_signals(sc):
    tenant = _t()
    now = datetime.now(UTC)
    # Two memories in the same session — the DISTINCT ON returns only the
    # LATEST per (run_id, agent_id).
    await _seed_memory(
        tenant_id=tenant, run_id="run-A", agent_id="a1", content="first step",
        created_at=now - timedelta(hours=2),
    )
    await _seed_memory(
        tenant_id=tenant, run_id="run-A", agent_id="a1", content="Shipped to prod",
        created_at=now - timedelta(minutes=5),
    )
    rows = await sc.outcome_terminal_memory_signals(
        tenant_id=tenant,
        fleet_id=None,
        window_start=now - timedelta(days=1),
        window_end=now + timedelta(minutes=1),
    )
    assert len(rows) == 1, "one terminal per (run_id, agent_id)"
    assert rows[0]["content"] == "Shipped to prod", "latest memory wins"


async def test_outcome_signals_missing_tenant_422(storage_http):
    resp = await storage_http.post(
        "/api/v1/storage/outcome-signals/terminal-memory",
        json={"window_start": "2026-05-01T00:00:00+00:00", "window_end": "2026-05-15T00:00:00+00:00"},
    )
    assert resp.status_code == 422


async def test_outcome_signals_bad_window_422(storage_http):
    resp = await storage_http.post(
        "/api/v1/storage/outcome-signals/terminal-memory",
        json={"tenant_id": "t", "window_start": "tomorrow", "window_end": "2026-05-15T00:00:00+00:00"},
    )
    assert resp.status_code == 422


async def test_forge_rejected_fingerprints_missing_tenant_422(storage_http):
    resp = await storage_http.post(
        "/api/v1/storage/forge/rejected-fingerprints/check",
        json={"cluster_fingerprint": "fp:v1:abc"},
    )
    assert resp.status_code == 422

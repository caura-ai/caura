"""Fix 2 final-cleanup (PR1) — four new core-storage-api endpoints.

Exercises the storage side of the pool-deletion cutover (the core-api consumers
land in PR2/PR3). Each endpoint folds a former core-api direct-DB site behind
HTTP:

- POST /memories/increment-recall          → PostgresService.memory_increment_recall
- POST /memories/recall-log                 → PostgresService.recall_log_write
- POST /capability-usage                    → PostgresService.capability_usage_insert
- POST /memories/prior-ingest-by-doc-hash   → PostgresService.find_prior_ingest_by_doc_hash

Rows are seeded via raw committed INSERTs on an independent ``get_session()``
and asserted the same way — storage commits on its own connection, so the
rolled-back ``db`` fixture is invisible to it (seed + assert via
``get_session`` / the ``storage_http`` raw client). A unique tenant per test
keeps concurrent suite runs isolated. Mirrors test_ph6_entity_linking_storage.py.

The ``RecallEvent``/``RecallCandidate`` import below is load-bearing: the
conftest's ``_setup_schema`` runs ``Base.metadata.create_all`` after collecting
test modules, and ``common.models.__init__`` does NOT export the recall-log
tables. Importing them here at module scope registers them in ``Base.metadata``
before the schema is created so ``recall_event``/``recall_candidate`` exist in
the test DB. ``CapabilityUsage`` is already exported by ``common.models``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text

from common.models import Memory
from common.models.recall_log import RecallCandidate, RecallEvent  # noqa: F401 — registers tables
from core_storage_api.services.postgres_service import get_session

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_PREFIX = "/api/v1/storage"


def _t() -> str:
    return f"test-tenant-fix2-cleanup-{uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Raw committed seed/assert helpers (independent session)
# ---------------------------------------------------------------------------


async def _seed_memory(
    *,
    tenant_id: str,
    content: str = "seed",
    memory_type: str = "fact",
    run_id: str | None = None,
    source_uri: str | None = None,
    metadata_: dict | None = None,
    status: str = "active",
    deleted: bool = False,
) -> str:
    mem_id = uuid4()
    async with get_session() as session:
        session.add(
            Memory(
                id=mem_id,
                tenant_id=tenant_id,
                agent_id="agent-1",
                memory_type=memory_type,
                content=content,
                run_id=run_id,
                source_uri=source_uri,
                metadata_=metadata_,
                status=status,
                deleted_at=datetime.now(UTC) if deleted else None,
            )
        )
    return str(mem_id)


async def _recall_count(memory_id: str) -> int:
    async with get_session() as session:
        row = (
            await session.execute(
                text("SELECT recall_count FROM memories WHERE id = CAST(:id AS uuid)"),
                {"id": memory_id},
            )
        ).fetchone()
    return int(row[0])


async def _last_recalled_at(memory_id: str):
    async with get_session() as session:
        row = (
            await session.execute(
                text("SELECT last_recalled_at FROM memories WHERE id = CAST(:id AS uuid)"),
                {"id": memory_id},
            )
        ).fetchone()
    return row[0]


async def _recall_event_row(event_id: str) -> dict | None:
    async with get_session() as session:
        row = (
            await session.execute(
                text("SELECT tenant_id, source, query_text, result_count FROM recall_event "
                     "WHERE id = CAST(:id AS uuid)"),
                {"id": event_id},
            )
        ).fetchone()
    if row is None:
        return None
    return {"tenant_id": row[0], "source": row[1], "query_text": row[2], "result_count": row[3]}


async def _recall_candidate_count(event_id: str) -> int:
    async with get_session() as session:
        row = (
            await session.execute(
                text("SELECT COUNT(*) FROM recall_candidate WHERE recall_event_id = CAST(:id AS uuid)"),
                {"id": event_id},
            )
        ).fetchone()
    return int(row[0])


async def _capability_usage_count(tenant_id: str) -> int:
    async with get_session() as session:
        row = (
            await session.execute(
                text("SELECT COUNT(*) FROM capability_usage WHERE tenant_id = :t"),
                {"t": tenant_id},
            )
        ).fetchone()
    return int(row[0])


# ===========================================================================
# A. increment-recall — expose the existing PostgresService method
# ===========================================================================


async def test_increment_recall_bumps_count(storage_http):
    tenant = _t()
    m1 = await _seed_memory(tenant_id=tenant)
    m2 = await _seed_memory(tenant_id=tenant)
    assert await _recall_count(m1) == 0

    resp = await storage_http.post(
        f"{_PREFIX}/memories/increment-recall",
        json={"memory_ids": [m1, m2]},
    )
    assert resp.status_code == 200
    assert resp.json() == {"updated": 2}

    assert await _recall_count(m1) == 1
    assert await _recall_count(m2) == 1
    # last_recalled_at is now stamped (was NULL on seed).
    assert await _last_recalled_at(m1) is not None


async def test_increment_recall_stale_id_not_counted(storage_http):
    # A non-existent id matches no row; ``updated`` reflects rows actually
    # updated (rowcount), not the submitted count.
    tenant = _t()
    m1 = await _seed_memory(tenant_id=tenant)
    resp = await storage_http.post(
        f"{_PREFIX}/memories/increment-recall",
        json={"memory_ids": [m1, str(uuid4())]},
    )
    assert resp.status_code == 200
    assert resp.json() == {"updated": 1}


async def test_increment_recall_empty_list_noop(storage_http):
    resp = await storage_http.post(
        f"{_PREFIX}/memories/increment-recall",
        json={"memory_ids": []},
    )
    assert resp.status_code == 200
    assert resp.json() == {"updated": 0}


async def test_increment_recall_missing_field_422(storage_http):
    resp = await storage_http.post(f"{_PREFIX}/memories/increment-recall", json={})
    assert resp.status_code == 422


async def test_increment_recall_non_list_422(storage_http):
    resp = await storage_http.post(
        f"{_PREFIX}/memories/increment-recall",
        json={"memory_ids": "not-a-list"},
    )
    assert resp.status_code == 422


async def test_increment_recall_invalid_uuid_422(storage_http):
    resp = await storage_http.post(
        f"{_PREFIX}/memories/increment-recall",
        json={"memory_ids": ["not-a-uuid"]},
    )
    assert resp.status_code == 422


# ===========================================================================
# B. recall-log — write recall_event + recall_candidate rows
# ===========================================================================


async def test_recall_log_writes_event_and_candidates(storage_http):
    tenant = _t()
    m1 = await _seed_memory(tenant_id=tenant)
    m2 = await _seed_memory(tenant_id=tenant)

    event = {
        "tenant_id": tenant,
        "agent_id": "agent-1",
        "source": "mcp_recall",
        "query_text": "what is acme",
        "strategy": "hybrid",
        "filter_agent_id": None,
        "fleet_scope": None,
        "top_k": 5,
        "min_similarity": 0.2,
        "result_count": 1,
        "top_score": 0.91,
    }
    candidates = [
        {
            "rank": 1,
            "memory_id": m1,
            "vec_sim": 0.91,
            "final_score": 0.95,
            "recall_boost": 1.1,
            "returned": True,
        },
        {
            "rank": 2,
            "memory_id": m2,
            "vec_sim": 0.18,
            "final_score": 0.18,
            "recall_boost": None,
            "returned": False,
        },
    ]

    resp = await storage_http.post(
        f"{_PREFIX}/memories/recall-log",
        json={"event": event, "candidates": candidates},
    )
    assert resp.status_code == 200
    event_id = resp.json()["recall_event_id"]
    assert event_id

    row = await _recall_event_row(event_id)
    assert row is not None
    assert row["tenant_id"] == tenant
    assert row["source"] == "mcp_recall"
    assert row["query_text"] == "what is acme"
    assert row["result_count"] == 1
    # Both candidates (returned + near-miss) written, linked to the event.
    assert await _recall_candidate_count(event_id) == 2


async def test_recall_log_no_candidates_ok(storage_http):
    tenant = _t()
    resp = await storage_http.post(
        f"{_PREFIX}/memories/recall-log",
        json={"event": {"tenant_id": tenant, "source": "mcp_recall"}},
    )
    assert resp.status_code == 200
    event_id = resp.json()["recall_event_id"]
    assert await _recall_candidate_count(event_id) == 0


async def test_recall_log_missing_event_422(storage_http):
    resp = await storage_http.post(f"{_PREFIX}/memories/recall-log", json={"candidates": []})
    assert resp.status_code == 422


async def test_recall_log_missing_tenant_422(storage_http):
    resp = await storage_http.post(
        f"{_PREFIX}/memories/recall-log",
        json={"event": {"source": "mcp_recall"}},
    )
    assert resp.status_code == 422


async def test_recall_log_missing_source_422(storage_http):
    resp = await storage_http.post(
        f"{_PREFIX}/memories/recall-log",
        json={"event": {"tenant_id": "t"}},
    )
    assert resp.status_code == 422


async def test_recall_log_non_list_candidates_422(storage_http):
    resp = await storage_http.post(
        f"{_PREFIX}/memories/recall-log",
        json={"event": {"tenant_id": "t", "source": "mcp_recall"}, "candidates": "nope"},
    )
    assert resp.status_code == 422


async def test_recall_log_falsy_non_list_candidates_422(storage_http):
    # A falsy non-None ``candidates`` (0/False/"") must 422, not be coerced to [].
    resp = await storage_http.post(
        f"{_PREFIX}/memories/recall-log",
        json={"event": {"tenant_id": "t", "source": "mcp_recall"}, "candidates": 0},
    )
    assert resp.status_code == 422


# ===========================================================================
# C. capability-usage — cross-tenant bulk flush (RLS-free)
# ===========================================================================


async def test_capability_usage_inserts_rows(storage_http):
    t_a, t_b = _t(), _t()
    ts = datetime.now(UTC).replace(second=0, microsecond=0).isoformat()
    rows = [
        {
            "tenant_id": t_a,
            "capability": "recall",
            "op": None,
            "transport": "mcp",
            "ts_bucket": ts,
            "count": 3,
            "error_count": 0,
            "duration_ms_sum": 120,
        },
        {
            "tenant_id": t_b,
            "capability": "write",
            "op": "create",
            "transport": "rest",
            "ts_bucket": ts,
            "count": 1,
            "error_count": 1,
            "duration_ms_sum": 40,
        },
    ]
    resp = await storage_http.post(f"{_PREFIX}/capability-usage", json={"rows": rows})
    assert resp.status_code == 200
    assert resp.json() == {"inserted": 2}

    # Cross-tenant: rows for two distinct tenants landed in one batch.
    assert await _capability_usage_count(t_a) == 1
    assert await _capability_usage_count(t_b) == 1


async def test_capability_usage_empty_rows_noop(storage_http):
    resp = await storage_http.post(f"{_PREFIX}/capability-usage", json={"rows": []})
    assert resp.status_code == 200
    assert resp.json() == {"inserted": 0}


async def test_capability_usage_missing_rows_422(storage_http):
    resp = await storage_http.post(f"{_PREFIX}/capability-usage", json={})
    assert resp.status_code == 422


async def test_capability_usage_non_list_rows_422(storage_http):
    resp = await storage_http.post(f"{_PREFIX}/capability-usage", json={"rows": "nope"})
    assert resp.status_code == 422


async def test_capability_usage_bad_ts_bucket_422(storage_http):
    resp = await storage_http.post(
        f"{_PREFIX}/capability-usage",
        json={
            "rows": [
                {
                    "tenant_id": "t",
                    "capability": "recall",
                    "transport": "mcp",
                    "ts_bucket": "not-a-date",
                    "count": 1,
                }
            ]
        },
    )
    assert resp.status_code == 422


# ===========================================================================
# D. prior-ingest-by-doc-hash — write-path idempotency lookup
# ===========================================================================


async def test_prior_ingest_returns_newest_run(storage_http):
    tenant = _t()
    doc_hash = f"hash-{uuid4().hex}"
    # Two prior ingests of the same content; the lookup returns only the
    # NEWEST run's memories.
    old = await _seed_memory(
        tenant_id=tenant,
        content="old run fact",
        run_id="run-old",
        source_uri="upload:doc.md",
        metadata_={"doc_hash": doc_hash, "source": "ingest", "salience": 0.5},
    )
    new = await _seed_memory(
        tenant_id=tenant,
        content="new run fact",
        run_id="run-new",
        source_uri="upload:doc.md",
        metadata_={"doc_hash": doc_hash, "source": "ingest"},
    )

    resp = await storage_http.post(
        f"{_PREFIX}/memories/prior-ingest-by-doc-hash",
        json={"tenant_id": tenant, "doc_hash": doc_hash},
    )
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    ids = {r["id"] for r in rows}
    # Newest run wins; the older run's memory is excluded.
    assert ids == {new}
    assert old not in ids
    # Shape carries what ingest_preview consumes (run_id, content, no vector).
    r = rows[0]
    assert r["run_id"] == "run-new"
    assert r["content"] == "new run fact"
    assert "embedding" not in r


async def test_prior_ingest_no_match_returns_empty(storage_http):
    tenant = _t()
    await _seed_memory(
        tenant_id=tenant,
        content="some fact",
        run_id="run-1",
        metadata_={"doc_hash": "hash-A", "source": "ingest"},
    )
    resp = await storage_http.post(
        f"{_PREFIX}/memories/prior-ingest-by-doc-hash",
        json={"tenant_id": tenant, "doc_hash": "hash-DIFFERENT"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"rows": []}


async def test_prior_ingest_ignores_deleted_and_non_ingest(storage_http):
    tenant = _t()
    doc_hash = f"hash-{uuid4().hex}"
    # A soft-deleted match and a non-ingest-source match are both excluded.
    await _seed_memory(
        tenant_id=tenant,
        content="deleted",
        run_id="run-del",
        metadata_={"doc_hash": doc_hash, "source": "ingest"},
        deleted=True,
    )
    await _seed_memory(
        tenant_id=tenant,
        content="not ingest",
        run_id="run-other",
        metadata_={"doc_hash": doc_hash, "source": "mcp_write"},
    )
    resp = await storage_http.post(
        f"{_PREFIX}/memories/prior-ingest-by-doc-hash",
        json={"tenant_id": tenant, "doc_hash": doc_hash},
    )
    assert resp.status_code == 200
    assert resp.json() == {"rows": []}


async def test_prior_ingest_null_run_id_returns_single_newest(storage_http):
    # When matching rows have run_id IS NULL, the newest-run filter must NOT
    # collapse every null-run_id ingest into one result (Python None==None).
    # The guard returns only the single newest row.
    tenant = _t()
    doc_hash = f"hash-{uuid4().hex}"
    await _seed_memory(
        tenant_id=tenant, content="anon old", run_id=None,
        metadata_={"doc_hash": doc_hash, "source": "ingest"},
    )
    new = await _seed_memory(
        tenant_id=tenant, content="anon new", run_id=None,
        metadata_={"doc_hash": doc_hash, "source": "ingest"},
    )
    resp = await storage_http.post(
        f"{_PREFIX}/memories/prior-ingest-by-doc-hash",
        json={"tenant_id": tenant, "doc_hash": doc_hash},
    )
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert {r["id"] for r in rows} == {new}


async def test_malformed_json_body_returns_422(storage_http):
    # Bare ``await request.json()`` would 500 on invalid JSON; the app-level
    # handler converts it to a fail-closed 422 across all storage endpoints.
    resp = await storage_http.post(
        f"{_PREFIX}/memories/increment-recall",
        content=b"{not valid json",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 422


async def test_prior_ingest_tenant_isolation(storage_http):
    t_a, t_b = _t(), _t()
    doc_hash = f"hash-{uuid4().hex}"
    await _seed_memory(
        tenant_id=t_a,
        content="tenant A doc",
        run_id="run-a",
        metadata_={"doc_hash": doc_hash, "source": "ingest"},
    )
    # Tenant B asks for the same doc_hash → must not see tenant A's memory.
    resp = await storage_http.post(
        f"{_PREFIX}/memories/prior-ingest-by-doc-hash",
        json={"tenant_id": t_b, "doc_hash": doc_hash},
    )
    assert resp.status_code == 200
    assert resp.json() == {"rows": []}


async def test_prior_ingest_missing_tenant_422(storage_http):
    resp = await storage_http.post(
        f"{_PREFIX}/memories/prior-ingest-by-doc-hash",
        json={"doc_hash": "h"},
    )
    assert resp.status_code == 422


async def test_prior_ingest_missing_doc_hash_422(storage_http):
    resp = await storage_http.post(
        f"{_PREFIX}/memories/prior-ingest-by-doc-hash",
        json={"tenant_id": "t"},
    )
    assert resp.status_code == 422

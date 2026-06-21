"""Fix 2 Phase 2 — core-api memory handlers now read/write through core-storage-api.

These exercise the full path (core-api routes/memories.py + storage_client ->
the in-process storage-api app via the conftest ASGI bridge -> test DB). Rows
are seeded via the storage client (committed, independent session) — NOT via the
``db`` fixture, whose outer transaction rolls back and is invisible to the
storage session's separate connection.

Covered:
- GET /memories/fleet-distribution (list_fleets + admin_list_fleets)
- GET /memories/admin-stats
- POST /memories/admin-list (cursor + limit+1 slicing)
- GET /memories/{id}/detail (server-side embedding stats; no raw vector)
- GET /memories/{id}/contradictions (3-read bundle + cross-tenant older guard)
- POST /memories/soft-delete-by-ids
- POST /memories/soft-delete-by-filter (JSONB bound-param predicate)
- POST /memories/soft-delete-by-run
- POST /memories/redistribute (one txn: move + auto-promote + skip + not_found)
- storage-router 422 input validation (raw bodies the typed client never sends)
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from core_api.constants import VECTOR_DIM

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _t() -> str:
    """Unique tenant id per test so concurrent suite runs don't collide."""
    return f"test-tenant-p2-{uuid4().hex[:8]}"


async def _write_memory(
    sc,
    tenant_id: str,
    *,
    content: str = "hello world",
    agent_id: str = "test-agent",
    fleet_id: str | None = "fleet-a",
    memory_type: str = "fact",
    status: str = "active",
    visibility: str = "scope_team",
    metadata: dict | None = None,
    run_id: str | None = None,
    embedding: list[float] | None = None,
) -> dict:
    """Create a committed memory and return the storage-side dict."""
    payload: dict = {
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "content": content,
        "memory_type": memory_type,
        "weight": 0.5,
        "status": status,
        "visibility": visibility,
    }
    if fleet_id is not None:
        payload["fleet_id"] = fleet_id
    if metadata is not None:
        payload["metadata_"] = metadata
    if run_id is not None:
        payload["run_id"] = run_id
    if embedding is not None:
        payload["embedding"] = embedding
    return await sc.create_memory(payload)


# ---------------------------------------------------------------------------
# fleet-distribution
# ---------------------------------------------------------------------------


async def test_fleet_distribution_counts(sc):
    tid = _t()
    await _write_memory(sc, tid, agent_id="a1", fleet_id="fleet-x")
    await _write_memory(sc, tid, agent_id="a2", fleet_id="fleet-x", content="other")
    await _write_memory(sc, tid, agent_id="a3", fleet_id="fleet-y", content="third")

    rows = await sc.memory_fleet_distribution(tid, exclude_scope_agent=False)
    by_fleet = {r["fleet_id"]: r for r in rows}
    assert by_fleet["fleet-x"]["memory_count"] == 2
    assert by_fleet["fleet-x"]["agent_count"] == 2
    assert by_fleet["fleet-y"]["memory_count"] == 1
    # Desc by memory_count: fleet-x (2) precedes fleet-y (1).
    fleet_ids = [r["fleet_id"] for r in rows if r["fleet_id"] in ("fleet-x", "fleet-y")]
    assert fleet_ids.index("fleet-x") < fleet_ids.index("fleet-y")


async def test_fleet_distribution_excludes_scope_agent(sc):
    """``exclude_scope_agent=True`` (the /fleets path) drops scope_agent rows."""
    tid = _t()
    await _write_memory(sc, tid, fleet_id="fleet-z", visibility="scope_team")
    await _write_memory(
        sc, tid, fleet_id="fleet-z", visibility="scope_agent", content="private"
    )

    incl = await sc.memory_fleet_distribution(tid, exclude_scope_agent=False)
    excl = await sc.memory_fleet_distribution(tid, exclude_scope_agent=True)
    assert {r["fleet_id"]: r["memory_count"] for r in incl}["fleet-z"] == 2
    assert {r["fleet_id"]: r["memory_count"] for r in excl}["fleet-z"] == 1


# ---------------------------------------------------------------------------
# admin-stats
# ---------------------------------------------------------------------------


async def test_admin_stats_shape_and_counts(sc):
    tid = _t()
    await _write_memory(sc, tid, agent_id="agent-1", memory_type="fact")
    await _write_memory(sc, tid, agent_id="agent-1", memory_type="fact", content="b")
    await _write_memory(
        sc, tid, agent_id="agent-2", memory_type="semantic", content="c"
    )

    stats = await sc.admin_memory_stats(tid)
    assert stats["total"] == 3
    assert stats["by_type"] == {"fact": 2, "semantic": 1}
    assert stats["by_agent"] == {"agent-1": 2, "agent-2": 1}
    # All seeded rows are active.
    assert stats["by_status"].get("active") == 3
    # Distinct from health-stats: by_agent is present, no type_distribution key.
    assert "by_agent" in stats
    assert "type_distribution" not in stats


async def test_admin_stats_empty_tenant_is_zeroed(sc):
    stats = await sc.admin_memory_stats(_t())
    assert stats == {"total": 0, "by_type": {}, "by_agent": {}, "by_status": {}}


# ---------------------------------------------------------------------------
# admin-list (cursor + limit+1 slicing)
# ---------------------------------------------------------------------------


async def test_admin_list_returns_rows_for_tenant(sc):
    tid = _t()
    await _write_memory(sc, tid, content="m1")
    await _write_memory(sc, tid, content="m2")

    rows = await sc.admin_list_memories(
        {"tenant_id": tid, "sort": "created_at", "order": "desc", "limit": 51}
    )
    assert {r["tenant_id"] for r in rows} == {tid}
    assert len(rows) == 2


async def test_admin_list_limit_plus_one_detects_more(sc):
    tid = _t()
    for i in range(3):
        await _write_memory(sc, tid, content=f"row-{i}")

    # Request limit=2 widened to 3 — the route uses the extra row to set
    # has_more + the next cursor.
    rows = await sc.admin_list_memories(
        {"tenant_id": tid, "sort": "created_at", "order": "desc", "limit": 3}
    )
    assert len(rows) == 3


# ---------------------------------------------------------------------------
# get_memory_detail — server-side embedding stats, no raw vector
# ---------------------------------------------------------------------------


async def test_get_memory_detail_embedding_stats_no_raw_vector(sc):
    tid = _t()
    emb = [0.0] * VECTOR_DIM
    emb[0] = 1.0
    emb[1] = -0.5
    mem = await _write_memory(sc, tid, embedding=emb)

    detail = await sc.get_memory_detail(tid, mem["id"])
    assert detail is not None
    # Raw pgvector must NOT cross the wire.
    assert "embedding" not in detail["memory"]
    assert "search_vector" not in detail["memory"]
    stats = detail["embedding_stats"]
    assert stats["dimensions"] == VECTOR_DIM
    assert stats["max"] == 1.0
    assert stats["min"] == -0.5
    assert stats["non_zero"] == 2
    # Preview is the first 20 components only.
    assert detail["embedding_preview"][:2] == [1.0, -0.5]
    assert len(detail["embedding_preview"]) == 20


async def test_get_memory_detail_missing_returns_none(sc):
    assert await sc.get_memory_detail(_t(), str(uuid4())) is None


async def test_get_memory_detail_cross_tenant_returns_none(sc):
    tid_a, tid_b = _t(), _t()
    mem = await _write_memory(sc, tid_a)
    # Asking for tenant B's view of tenant A's row → not found.
    assert await sc.get_memory_detail(tid_b, mem["id"]) is None


# ---------------------------------------------------------------------------
# get_memory_contradictions — 3-read bundle
# ---------------------------------------------------------------------------


async def test_contradictions_bundle_supersessor(sc):
    tid = _t()
    older = await _write_memory(sc, tid, content="older fact", status="outdated")
    newer = await _write_memory(sc, tid, content="newer fact")
    # newer supersedes older
    await sc.update_memory_status(
        newer["id"], "active", tenant_id=tid, supersedes_id=older["id"]
    )

    # Older row sees the newer one as a supersessor.
    bundle = await sc.get_memory_contradictions(tid, older["id"])
    assert bundle is not None
    assert bundle["memory"]["id"] == older["id"]
    sup_ids = {s["id"] for s in bundle["supersessors"]}
    assert newer["id"] in sup_ids

    # Newer row points back at older via ``older``.
    bundle2 = await sc.get_memory_contradictions(tid, newer["id"])
    assert bundle2["older"] is not None
    assert bundle2["older"]["id"] == older["id"]


async def test_contradictions_missing_returns_none(sc):
    assert await sc.get_memory_contradictions(_t(), str(uuid4())) is None


# ---------------------------------------------------------------------------
# soft-delete-by-ids
# ---------------------------------------------------------------------------


async def test_soft_delete_by_ids(sc):
    tid = _t()
    a = await _write_memory(sc, tid, content="del-a")
    b = await _write_memory(sc, tid, content="del-b")
    c = await _write_memory(sc, tid, content="keep-c")

    deleted = await sc.soft_delete_by_ids(tid, [a["id"], b["id"]])
    assert deleted == 2

    # Re-deleting already-deleted rows is a no-op (deleted_at filter).
    assert await sc.soft_delete_by_ids(tid, [a["id"], b["id"]]) == 0
    # The untouched row is still live.
    assert await sc.get_memory_for_tenant(tid, c["id"]) is not None
    assert await sc.get_memory_for_tenant(tid, a["id"]) is None


async def test_soft_delete_by_ids_tenant_scoped(sc):
    tid_a, tid_b = _t(), _t()
    mem = await _write_memory(sc, tid_a)
    # Deleting under the wrong tenant matches nothing.
    assert await sc.soft_delete_by_ids(tid_b, [mem["id"]]) == 0
    assert await sc.get_memory_for_tenant(tid_a, mem["id"]) is not None


# ---------------------------------------------------------------------------
# soft-delete-by-filter — JSONB bound-param predicate
# ---------------------------------------------------------------------------


async def test_soft_delete_by_filter_metadata_exact_match(sc):
    tid = _t()
    keep = await _write_memory(sc, tid, content="keep", metadata={"run": "r1"})
    drop = await _write_memory(sc, tid, content="drop", metadata={"run": "r2"})

    deleted = await sc.soft_delete_by_filter(
        {"tenant_id": tid, "metadata_filter": {"run": "r2"}}
    )
    assert deleted == 1
    assert await sc.get_memory_for_tenant(tid, drop["id"]) is None
    assert await sc.get_memory_for_tenant(tid, keep["id"]) is not None


async def test_soft_delete_by_filter_multi_pair_and_semantics(sc):
    """Multiple metadata pairs combine with AND (distinct bound params)."""
    tid = _t()
    match = await _write_memory(sc, tid, content="m", metadata={"k1": "v1", "k2": "v2"})
    partial = await _write_memory(
        sc, tid, content="p", metadata={"k1": "v1", "k2": "x"}
    )

    deleted = await sc.soft_delete_by_filter(
        {"tenant_id": tid, "metadata_filter": {"k1": "v1", "k2": "v2"}}
    )
    assert deleted == 1
    assert await sc.get_memory_for_tenant(tid, match["id"]) is None
    assert await sc.get_memory_for_tenant(tid, partial["id"]) is not None


async def test_soft_delete_by_filter_exclude_ids(sc):
    tid = _t()
    a = await _write_memory(sc, tid, content="a", agent_id="ag")
    b = await _write_memory(sc, tid, content="b", agent_id="ag")

    deleted = await sc.soft_delete_by_filter(
        {"tenant_id": tid, "agent_id": "ag", "exclude_ids": [a["id"]]}
    )
    assert deleted == 1
    assert await sc.get_memory_for_tenant(tid, a["id"]) is not None
    assert await sc.get_memory_for_tenant(tid, b["id"]) is None


# ---------------------------------------------------------------------------
# soft-delete-by-run
# ---------------------------------------------------------------------------


async def test_soft_delete_by_run_requires_ingest_source(sc):
    tid = _t()
    run = f"run-{uuid4().hex[:8]}"
    ingest = await _write_memory(
        sc, tid, content="ingest", run_id=run, metadata={"source": "ingest"}
    )
    other = await _write_memory(
        sc, tid, content="manual", run_id=run, metadata={"source": "manual"}
    )

    deleted = await sc.soft_delete_by_run(tid, run, metadata_source="ingest")
    assert deleted == 1
    assert await sc.get_memory_for_tenant(tid, ingest["id"]) is None
    # Same run_id but non-ingest source is untouched (belt-and-braces).
    assert await sc.get_memory_for_tenant(tid, other["id"]) is not None


# ---------------------------------------------------------------------------
# redistribute — one transaction: move + auto-promote + skip + not_found
# ---------------------------------------------------------------------------


async def test_redistribute_moves_promotes_skips_and_reports_not_found(sc):
    tid = _t()
    team = await _write_memory(sc, tid, agent_id="old", visibility="scope_team")
    private = await _write_memory(
        sc, tid, agent_id="old", visibility="scope_agent", content="private"
    )
    already = await _write_memory(sc, tid, agent_id="target", content="already there")
    ghost = str(uuid4())

    outcome = await sc.redistribute_memories(
        tid, [team["id"], private["id"], already["id"], ghost], "target"
    )
    assert outcome["moved"] == 2  # team + private
    assert outcome["promoted"] == 1  # scope_agent -> scope_team
    assert outcome["skipped"] == 1  # already owned by target
    assert outcome["from_agents"] == ["old"]
    assert outcome["not_found"] == [ghost]

    # The scope_agent row was promoted and reassigned.
    moved_private = await sc.get_memory_for_tenant(tid, private["id"])
    assert moved_private["agent_id"] == "target"
    assert moved_private["visibility"] == "scope_team"
    # The scope_team row kept its visibility.
    moved_team = await sc.get_memory_for_tenant(tid, team["id"])
    assert moved_team["agent_id"] == "target"
    assert moved_team["visibility"] == "scope_team"


# ---------------------------------------------------------------------------
# Storage-router input validation (raw bodies the typed client never sends)
# ---------------------------------------------------------------------------


def _path(suffix: str) -> str:
    return f"/api/v1/storage/memories/{suffix}"


async def test_soft_delete_by_ids_missing_tenant_422(storage_http):
    resp = await storage_http.post(_path("soft-delete-by-ids"), json={"ids": []})
    assert resp.status_code == 422, resp.text


async def test_soft_delete_by_filter_bad_exclude_uuid_422(storage_http):
    resp = await storage_http.post(
        _path("soft-delete-by-filter"),
        json={"tenant_id": _t(), "exclude_ids": ["not-a-uuid"]},
    )
    assert resp.status_code == 422, resp.text


async def test_redistribute_missing_target_422(storage_http):
    resp = await storage_http.post(
        _path("redistribute"), json={"tenant_id": _t(), "memory_ids": []}
    )
    assert resp.status_code == 422, resp.text


async def test_admin_list_malformed_cursor_422(storage_http):
    """A malformed cursor in the raw body must 422, not 500 (claude-review)."""
    resp = await storage_http.post(
        _path("admin-list"), json={"tenant_id": _t(), "cursor_ts": "not-a-date"}
    )
    assert resp.status_code == 422, resp.text


async def test_admin_list_unknown_sort_falls_back_not_500(storage_http):
    """An unrecognised ``sort`` must fall back to created_at, not AttributeError
    (500) at getattr(Memory, sort) (claude-review)."""
    resp = await storage_http.post(
        _path("admin-list"), json={"tenant_id": _t(), "sort": "definitely_not_a_column"}
    )
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json(), list)

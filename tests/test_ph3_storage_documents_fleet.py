"""Fix 2 Phase 3 — documents + fleet residuals routed through core-storage-api.

Exercises the 5 new core-storage-api endpoints via the typed storage client
(bridged in-process to the storage app by the conftest ASGI fixture, against
the test DB):

- POST /documents/upsert-xmax   (sc.upsert_document_xmax)
- GET  /documents/collections   (sc.list_document_collections)
- POST /documents/search        (sc.search_documents_vector)
- GET  /fleet/commands/in-flight-deploy     (sc.fleet_in_flight_deploy)
- GET  /fleet/commands/deploy-attempt-count (sc.fleet_deploy_attempt_count)

Rows are seeded via the storage write paths (committed, independent session)
— NOT the ``db`` fixture, whose outer transaction rolls back and is invisible
to the storage session's separate connection.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from common.embedding import fake_embedding
from core_storage_api.services.postgres_service import PostgresService

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _t() -> str:
    """Unique tenant id per test so concurrent suite runs don't collide."""
    return f"test-tenant-ph3-{uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# A1 — POST /documents/upsert-xmax
# ---------------------------------------------------------------------------


async def test_upsert_xmax_insert_then_update(sc):
    tenant = _t()
    base = {
        "tenant_id": tenant,
        "collection": "things",
        "doc_id": "doc-1",
        "data": {"v": 1},
    }
    first = await sc.upsert_document_xmax(base)
    assert first["xmax"] == 0, "first upsert is an INSERT → xmax == 0"
    assert first["id"]

    second = await sc.upsert_document_xmax({**base, "data": {"v": 2}})
    assert second["xmax"] != 0, "second upsert (same key) is an UPDATE → xmax != 0"
    assert second["id"] == first["id"], "same row updated in place"


async def test_upsert_xmax_stores_and_clears_embedding(sc):
    tenant = _t()
    emb = fake_embedding("hello world")
    base = {
        "tenant_id": tenant,
        "collection": "things",
        "doc_id": "doc-emb",
        "data": {"summary": "hello world"},
        "embedding": emb,
    }
    await sc.upsert_document_xmax(base)

    # Embedding present → the doc is searchable.
    pairs = await sc.search_documents_vector(
        {"tenant_id": tenant, "query_embedding": emb, "top_k": 5}
    )
    assert [r["doc_id"] for r in pairs] == ["doc-emb"]

    # Re-upsert WITHOUT embedding (None) clears the vector — doc drops out of
    # search (the intentional opt-in semantic).
    await sc.upsert_document_xmax({**base, "embedding": None})
    pairs_after = await sc.search_documents_vector(
        {"tenant_id": tenant, "query_embedding": emb, "top_k": 5}
    )
    assert pairs_after == [], "embedding=None on re-write clears the prior vector"


async def test_upsert_xmax_rejects_system_collection(sc):
    # Mirror document_upsert's guard: the public upsert-xmax path must not be
    # able to write ``_``-prefixed system collections (e.g. ``_keystones``).
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await sc.upsert_document_xmax(
            {
                "tenant_id": _t(),
                "collection": "_keystones",
                "doc_id": "doc-sys",
                "data": {"v": 1},
            }
        )
    assert exc_info.value.response.status_code == 400


# ---------------------------------------------------------------------------
# A2 — GET /documents/collections
# ---------------------------------------------------------------------------


async def test_collections_per_collection_counts(sc):
    tenant = _t()
    for i in range(2):
        await sc.upsert_document_xmax(
            {"tenant_id": tenant, "collection": "alpha", "doc_id": f"a{i}", "data": {}}
        )
    await sc.upsert_document_xmax(
        {"tenant_id": tenant, "collection": "beta", "doc_id": "b0", "data": {}}
    )

    result = await sc.list_document_collections(tenant_id=tenant)
    by_name = {c["name"]: c["count"] for c in result["collections"]}
    assert by_name == {"alpha": 2, "beta": 1}
    assert result["count"] == 2
    # Sorted alphabetically.
    assert [c["name"] for c in result["collections"]] == ["alpha", "beta"]


async def test_collections_fleet_scoping(sc):
    tenant = _t()
    await sc.upsert_document_xmax(
        {"tenant_id": tenant, "fleet_id": "fleet-a", "collection": "c", "doc_id": "x", "data": {}}
    )
    await sc.upsert_document_xmax(
        {"tenant_id": tenant, "fleet_id": "fleet-b", "collection": "c", "doc_id": "y", "data": {}}
    )

    scoped = await sc.list_document_collections(tenant_id=tenant, fleet_id="fleet-a")
    assert {c["name"]: c["count"] for c in scoped["collections"]} == {"c": 1}

    unscoped = await sc.list_document_collections(tenant_id=tenant)
    assert {c["name"]: c["count"] for c in unscoped["collections"]} == {"c": 2}


async def test_collections_readable_tenant_ids_merges(sc):
    t1, t2 = _t(), _t()
    await sc.upsert_document_xmax(
        {"tenant_id": t1, "collection": "shared", "doc_id": "x", "data": {}}
    )
    await sc.upsert_document_xmax(
        {"tenant_id": t2, "collection": "shared", "doc_id": "y", "data": {}}
    )

    merged = await sc.list_document_collections(
        tenant_id=t1, readable_tenant_ids=[t1, t2]
    )
    by_name = {c["name"]: c["count"] for c in merged["collections"]}
    assert by_name == {"shared": 2}, "same-named collections across tenants merge"


# ---------------------------------------------------------------------------
# A3 — POST /documents/search
# ---------------------------------------------------------------------------


async def _seed_doc(sc, tenant, collection, doc_id, text, *, status=None, fleet_id=None):
    data: dict = {"summary": text}
    if status is not None:
        data["status"] = status
    payload = {
        "tenant_id": tenant,
        "collection": collection,
        "doc_id": doc_id,
        "data": data,
        "embedding": fake_embedding(text),
    }
    if fleet_id is not None:
        payload["fleet_id"] = fleet_id
    return await sc.upsert_document_xmax(payload)


async def test_search_cosine_ordering(sc):
    tenant = _t()
    await _seed_doc(sc, tenant, "c", "near", "apple banana cherry")
    await _seed_doc(sc, tenant, "c", "far", "zebra giraffe lion")

    pairs = await sc.search_documents_vector(
        {"tenant_id": tenant, "query_embedding": fake_embedding("apple banana cherry"), "top_k": 5}
    )
    assert [r["doc_id"] for r in pairs] == ["near", "far"], "closest vector first"
    assert pairs[0]["similarity"] >= pairs[1]["similarity"]


async def test_search_collection_none_spans_all(sc):
    tenant = _t()
    await _seed_doc(sc, tenant, "alpha", "a", "shared word one")
    await _seed_doc(sc, tenant, "beta", "b", "shared word two")

    spanning = await sc.search_documents_vector(
        {"tenant_id": tenant, "query_embedding": fake_embedding("shared word"), "top_k": 5}
    )
    assert {r["collection"] for r in spanning} == {"alpha", "beta"}

    scoped = await sc.search_documents_vector(
        {
            "tenant_id": tenant,
            "query_embedding": fake_embedding("shared word"),
            "collection": "alpha",
            "top_k": 5,
        }
    )
    assert {r["collection"] for r in scoped} == {"alpha"}


async def test_search_excludes_null_embedding(sc):
    tenant = _t()
    await _seed_doc(sc, tenant, "c", "indexed", "findable text here")
    # No embedding → not indexed → invisible to search.
    await sc.upsert_document_xmax(
        {"tenant_id": tenant, "collection": "c", "doc_id": "unindexed", "data": {"summary": "x"}}
    )
    pairs = await sc.search_documents_vector(
        {"tenant_id": tenant, "query_embedding": fake_embedding("findable text here"), "top_k": 5}
    )
    assert [r["doc_id"] for r in pairs] == ["indexed"]


async def test_search_status_filter(sc):
    tenant = _t()
    await _seed_doc(sc, tenant, "skills", "active", "deploy helper", status="active")
    await _seed_doc(sc, tenant, "skills", "draft", "deploy helper", status="draft")

    active_only = await sc.search_documents_vector(
        {
            "tenant_id": tenant,
            "query_embedding": fake_embedding("deploy helper"),
            "status": "active",
            "top_k": 5,
        }
    )
    assert [r["doc_id"] for r in active_only] == ["active"]


async def test_search_readable_tenant_ids_widens(sc):
    t1, t2 = _t(), _t()
    await _seed_doc(sc, t1, "c", "x", "common phrase here")
    await _seed_doc(sc, t2, "c", "y", "common phrase here")

    narrow = await sc.search_documents_vector(
        {"tenant_id": t1, "query_embedding": fake_embedding("common phrase here"), "top_k": 5}
    )
    assert {r["tenant_id"] for r in narrow} == {t1}

    wide = await sc.search_documents_vector(
        {
            "tenant_id": t1,
            "query_embedding": fake_embedding("common phrase here"),
            "readable_tenant_ids": [t1, t2],
            "top_k": 5,
        }
    )
    assert {r["tenant_id"] for r in wide} == {t1, t2}


# ---------------------------------------------------------------------------
# A4 / A5 — fleet gates
# ---------------------------------------------------------------------------


async def _make_node(svc: PostgresService, tenant: str) -> str:
    node_id = await svc.fleet_upsert_node(
        values={"tenant_id": tenant, "node_name": f"node-{uuid4().hex[:8]}", "fleet_id": "fleet-a"}
    )
    return str(node_id)


async def _add_deploy_command(
    svc: PostgresService,
    tenant: str,
    node_id: str,
    *,
    status: str,
    created_at: datetime,
    target_version: str = "2.4.0",
) -> None:
    await svc.fleet_add_command(
        {
            "tenant_id": tenant,
            "node_id": node_id,
            "command": "deploy",
            "status": status,
            "payload": {"target_version": target_version},
            "created_at": created_at,
        }
    )


async def test_fleet_in_flight_deploy(sc):
    svc = PostgresService()
    tenant = _t()
    node_id = await _make_node(svc, tenant)
    now = datetime.now(UTC)
    since = now - timedelta(minutes=10)

    # Pending within the window → in flight.
    await _add_deploy_command(svc, tenant, node_id, status="pending", created_at=now)
    assert await sc.fleet_in_flight_deploy(node_id=node_id, since=since) is True


async def test_fleet_in_flight_deploy_acked_counts(sc):
    svc = PostgresService()
    tenant = _t()
    node_id = await _make_node(svc, tenant)
    now = datetime.now(UTC)
    since = now - timedelta(minutes=10)

    # Acked within the window also counts as in flight.
    await _add_deploy_command(svc, tenant, node_id, status="acked", created_at=now)
    assert await sc.fleet_in_flight_deploy(node_id=node_id, since=since) is True


async def test_fleet_in_flight_deploy_false_when_older_or_none(sc):
    svc = PostgresService()
    tenant = _t()
    node_id = await _make_node(svc, tenant)
    now = datetime.now(UTC)
    since = now - timedelta(minutes=10)

    # No commands at all → not in flight.
    assert await sc.fleet_in_flight_deploy(node_id=node_id, since=since) is False

    # A pending deploy older than the window is abandoned → not in flight.
    await _add_deploy_command(
        svc, tenant, node_id, status="pending", created_at=now - timedelta(minutes=30)
    )
    assert await sc.fleet_in_flight_deploy(node_id=node_id, since=since) is False

    # A done deploy (terminal) within the window is not in flight.
    await _add_deploy_command(svc, tenant, node_id, status="done", created_at=now)
    assert await sc.fleet_in_flight_deploy(node_id=node_id, since=since) is False


async def test_fleet_deploy_attempt_count_all_statuses_per_target(sc):
    svc = PostgresService()
    tenant = _t()
    node_id = await _make_node(svc, tenant)
    now = datetime.now(UTC)
    since = now - timedelta(hours=24)

    # Three deploys for target 2.4.0 spanning ALL statuses, within window.
    await _add_deploy_command(svc, tenant, node_id, status="pending", created_at=now, target_version="2.4.0")
    await _add_deploy_command(svc, tenant, node_id, status="done", created_at=now, target_version="2.4.0")
    await _add_deploy_command(svc, tenant, node_id, status="failed", created_at=now, target_version="2.4.0")
    # A different target_version is excluded from this target's budget.
    await _add_deploy_command(svc, tenant, node_id, status="done", created_at=now, target_version="2.5.0")
    # An older one (outside the window) is excluded.
    await _add_deploy_command(
        svc, tenant, node_id, status="done", created_at=now - timedelta(hours=48), target_version="2.4.0"
    )

    count = await sc.fleet_deploy_attempt_count(
        node_id=node_id, target_version="2.4.0", since=since
    )
    assert count == 3, "counts all statuses for this target within the window"

    # Zero for a brand-new target → fresh budget.
    fresh = await sc.fleet_deploy_attempt_count(
        node_id=node_id, target_version="9.9.9", since=since
    )
    assert fresh == 0

"""Fix 2 Ph6 — entity-linking pipeline routed through core-storage-api.

Exercises the 5 new core-storage-api endpoints via the typed storage client
(bridged in-process to the storage app by the conftest ASGI fixture, against the
test DB):

- POST /entities/resolve              (sc.resolve_entities)            — merge dupes, ONE txn + SAVEPOINT per dupe
- POST /entities/discover-cross-links (sc.discover_cross_links)        — targeted + batch, ONE txn
- POST /entities/infer-relations      (sc.infer_relations)             — create + reinforce, ONE txn
- POST /entities/list-null-embeddings (sc.list_null_embedding_entities) — read NULL-embedding rows
- POST /entities/set-embeddings       (sc.set_entity_embeddings)        — write embeddings back, ONE txn

Rows are seeded via raw committed INSERTs on an independent ``get_session()``
(storage commits on its own connection, so the rolled-back ``db`` fixture is
invisible to it — seed + assert via ``get_session`` / the ``sc`` client). The
test DB uses ``EMBEDDING_PROVIDER=fake``; vectors come from ``fake_embedding``
and are seeded directly via the ORM models (the pgvector column binds a list).
Seeding duplicate entities uses identical embeddings but distinct
``canonical_name``s so the ``uq_entities_tenant_type_name_fleet`` index is not
tripped while the pair-find still sees cosine similarity = 1.0. A unique tenant
per test keeps concurrent suite runs isolated. Mirrors test_ph5b_evolve_storage.py.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from common.embedding.providers.fake import fake_embedding
from common.models import Entity, Memory, MemoryEntityLink, Relation
from core_storage_api.services.postgres_service import get_session

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _t() -> str:
    return f"test-tenant-ph6-entlink-{uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Raw committed seed helpers (independent session — visible to the sc client)
# ---------------------------------------------------------------------------


async def _seed_entity(
    *,
    tenant_id: str,
    canonical_name: str,
    entity_type: str = "organization",
    fleet_id: str | None = None,
    name_embedding: list[float] | None = None,
    attributes: dict | None = None,
) -> str:
    ent_id = uuid4()
    async with get_session() as session:
        session.add(
            Entity(
                id=ent_id,
                tenant_id=tenant_id,
                fleet_id=fleet_id,
                entity_type=entity_type,
                canonical_name=canonical_name,
                attributes=attributes,
                name_embedding=name_embedding,
            )
        )
    return str(ent_id)


async def _seed_memory(
    *,
    tenant_id: str,
    content: str,
    fleet_id: str | None = None,
    embedding: list[float] | None = None,
    status: str = "active",
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
                content=content,
                embedding=embedding,
                status=status,
            )
        )
    return str(mem_id)


async def _seed_link(*, memory_id: str, entity_id: str, role: str = "mentioned") -> None:
    async with get_session() as session:
        session.add(
            MemoryEntityLink(
                memory_id=memory_id,
                entity_id=entity_id,
                role=role,
            )
        )


async def _seed_relation(
    *,
    tenant_id: str,
    from_entity_id: str,
    to_entity_id: str,
    relation_type: str = "related_to",
    weight: float = 0.5,
    fleet_id: str | None = None,
) -> str:
    rel_id = uuid4()
    async with get_session() as session:
        session.add(
            Relation(
                id=rel_id,
                tenant_id=tenant_id,
                fleet_id=fleet_id,
                from_entity_id=from_entity_id,
                relation_type=relation_type,
                to_entity_id=to_entity_id,
                weight=weight,
            )
        )
    return str(rel_id)


async def _entity_exists(entity_id: str) -> bool:
    async with get_session() as session:
        row = (
            await session.execute(
                text("SELECT 1 FROM entities WHERE id = CAST(:id AS uuid)"), {"id": entity_id}
            )
        ).fetchone()
    return row is not None


async def _entity_attrs(entity_id: str) -> dict | None:
    async with get_session() as session:
        row = (
            await session.execute(
                text("SELECT attributes FROM entities WHERE id = CAST(:id AS uuid)"),
                {"id": entity_id},
            )
        ).fetchone()
    return row.attributes if row else None


async def _link_entity_ids(memory_id: str) -> set[str]:
    async with get_session() as session:
        rows = (
            await session.execute(
                text("SELECT entity_id FROM memory_entity_links WHERE memory_id = CAST(:id AS uuid)"),
                {"id": memory_id},
            )
        ).all()
    return {str(r[0]) for r in rows}


async def _relation_weight(rel_id: str) -> float:
    async with get_session() as session:
        row = (
            await session.execute(
                text("SELECT weight FROM relations WHERE id = CAST(:id AS uuid)"), {"id": rel_id}
            )
        ).fetchone()
    return float(row.weight)


async def _count_relations(tenant_id: str) -> int:
    async with get_session() as session:
        row = (
            await session.execute(
                text("SELECT COUNT(*) FROM relations WHERE tenant_id = :t"), {"t": tenant_id}
            )
        ).fetchone()
    return int(row[0])


# ===========================================================================
# A. resolve — merge a duplicate pair
# ===========================================================================


async def test_resolve_merges_duplicate_pair(sc):
    tenant = _t()
    emb = fake_embedding("acme")
    # Identical embeddings (sim=1.0) but distinct names so the unique index
    # is not tripped. Canonical pick = longest name → "Acme Corporation".
    canonical = await _seed_entity(tenant_id=tenant, canonical_name="Acme Corporation", name_embedding=emb)
    dupe = await _seed_entity(tenant_id=tenant, canonical_name="Acme", name_embedding=emb)

    # A memory linked to the DUPE should be re-pointed to the canonical.
    mem = await _seed_memory(tenant_id=tenant, content="about acme")
    await _seed_link(memory_id=mem, entity_id=dupe)

    resp = await sc.resolve_entities(
        tenant_id=tenant,
        fleet_id=None,
        batch_size=100,
        threshold=0.85,
        candidate_limit=3,
    )

    assert resp["merge_count"] == 1
    assert resp["clusters"] == 1
    assert resp["cluster_errors"] == 0
    assert resp["merged_entity_ids"] == [dupe]

    # Dupe entity deleted, canonical survives.
    assert await _entity_exists(dupe) is False
    assert await _entity_exists(canonical) is True
    # The link was re-pointed off the dupe onto the canonical.
    assert await _link_entity_ids(mem) == {canonical}
    # Aliases merged onto the canonical's attributes.
    attrs = await _entity_attrs(canonical)
    assert "Acme" in attrs["_aliases"]
    assert "Acme Corporation" in attrs["_aliases"]


async def test_resolve_repoints_relations_and_preserves_higher_weight(sc):
    tenant = _t()
    emb = fake_embedding("globex")
    canonical = await _seed_entity(tenant_id=tenant, canonical_name="Globex Incorporated", name_embedding=emb)
    dupe = await _seed_entity(tenant_id=tenant, canonical_name="Globex", name_embedding=emb)
    other = await _seed_entity(tenant_id=tenant, canonical_name="Initech", name_embedding=fake_embedding("initech"))

    # canonical -> other (weight 0.3) and dupe -> other (weight 0.9): after the
    # merge the conflicting dupe out-relation is dropped and the survivor keeps
    # the GREATEST weight (0.9).
    rel_canonical = await _seed_relation(
        tenant_id=tenant, from_entity_id=canonical, to_entity_id=other, weight=0.3
    )
    await _seed_relation(tenant_id=tenant, from_entity_id=dupe, to_entity_id=other, weight=0.9)

    resp = await sc.resolve_entities(
        tenant_id=tenant, fleet_id=None, batch_size=100, threshold=0.85, candidate_limit=3
    )
    assert resp["merge_count"] == 1
    assert await _entity_exists(dupe) is False
    # canonical -> other survives with the preserved higher weight.
    assert await _relation_weight(rel_canonical) == pytest.approx(0.9)


async def test_resolve_no_pairs_returns_zero(sc):
    tenant = _t()
    await _seed_entity(tenant_id=tenant, canonical_name="Solo", name_embedding=fake_embedding("solo"))
    resp = await sc.resolve_entities(
        tenant_id=tenant, fleet_id=None, batch_size=100, threshold=0.85, candidate_limit=3
    )
    assert resp["skipped"] is True
    assert resp["merge_count"] == 0
    assert resp["merged_entity_ids"] == []


async def test_resolve_tenant_isolation(sc):
    t_a, t_b = _t(), _t()
    emb = fake_embedding("dupe")
    # Two similar entities but in DIFFERENT tenants → must not pair/merge.
    a = await _seed_entity(tenant_id=t_a, canonical_name="Dupe One", name_embedding=emb)
    await _seed_entity(tenant_id=t_b, canonical_name="Dupe Two", name_embedding=emb)
    resp = await sc.resolve_entities(
        tenant_id=t_a, fleet_id=None, batch_size=100, threshold=0.85, candidate_limit=3
    )
    assert resp["merge_count"] == 0
    assert await _entity_exists(a) is True


# ===========================================================================
# B. discover-cross-links
# ===========================================================================


async def test_discover_targeted_creates_link(sc):
    tenant = _t()
    # Entity name appears in the memory content (text-verify passes), and the
    # memory embedding matches the entity name embedding (lateral match).
    emb = fake_embedding("alice")
    ent = await _seed_entity(tenant_id=tenant, canonical_name="Alice", entity_type="person", name_embedding=emb)
    mem = await _seed_memory(tenant_id=tenant, content="Alice loves coffee", embedding=emb)

    resp = await sc.discover_cross_links(
        tenant_id=tenant,
        fleet_id=None,
        batch_size=200,
        threshold=0.75,
        text_verify=True,
        target_memory_ids=[mem],
    )
    assert resp["links_created"] == 1
    assert await _link_entity_ids(mem) == {ent}


async def test_discover_batch_creates_link(sc):
    tenant = _t()
    emb = fake_embedding("bob")
    ent = await _seed_entity(tenant_id=tenant, canonical_name="Bob", entity_type="person", name_embedding=emb)
    mem = await _seed_memory(tenant_id=tenant, content="Bob plays guitar", embedding=emb)

    # Batch mode (no target_memory_ids): picks under-connected memories.
    resp = await sc.discover_cross_links(
        tenant_id=tenant,
        fleet_id=None,
        batch_size=200,
        threshold=0.75,
        text_verify=True,
        target_memory_ids=None,
    )
    assert resp["links_created"] == 1
    assert await _link_entity_ids(mem) == {ent}


async def test_discover_text_verify_rejects_absent_name(sc):
    tenant = _t()
    # Entity embedding matches the memory embedding, but the entity name does
    # NOT appear in the content → text-verify drops it.
    emb = fake_embedding("carol")
    await _seed_entity(tenant_id=tenant, canonical_name="Carol", entity_type="person", name_embedding=emb)
    mem = await _seed_memory(tenant_id=tenant, content="someone plays guitar", embedding=emb)

    resp = await sc.discover_cross_links(
        tenant_id=tenant,
        fleet_id=None,
        batch_size=200,
        threshold=0.75,
        text_verify=True,
        target_memory_ids=[mem],
    )
    assert resp["links_created"] == 0
    assert await _link_entity_ids(mem) == set()


async def test_discover_no_candidates_returns_zero(sc):
    tenant = _t()
    resp = await sc.discover_cross_links(
        tenant_id=tenant,
        fleet_id=None,
        batch_size=200,
        threshold=0.75,
        text_verify=True,
        target_memory_ids=None,
    )
    assert resp == {"skipped": True, "links_created": 0}


# ===========================================================================
# C. infer-relations — create + reinforce
# ===========================================================================


async def test_infer_creates_relation_from_cooccurrence(sc):
    tenant = _t()
    e1 = await _seed_entity(tenant_id=tenant, canonical_name="X", name_embedding=fake_embedding("x"))
    e2 = await _seed_entity(tenant_id=tenant, canonical_name="Y", name_embedding=fake_embedding("y"))
    # Two memories each linking BOTH entities → cooccur=2 (>= min 2).
    for i in range(2):
        m = await _seed_memory(tenant_id=tenant, content=f"m{i}")
        await _seed_link(memory_id=m, entity_id=e1)
        await _seed_link(memory_id=m, entity_id=e2)

    resp = await sc.infer_relations(
        tenant_id=tenant,
        fleet_id=None,
        batch_size=500,
        min_cooccurrence=2,
        reinforce_delta=0.1,
        max_relation_weight=1.0,
    )
    assert resp["relations_created"] == 1
    assert resp["relations_reinforced"] == 0
    assert await _count_relations(tenant) == 1


async def test_infer_reinforces_existing_relation_clamped(sc):
    tenant = _t()
    e1 = await _seed_entity(tenant_id=tenant, canonical_name="P", name_embedding=fake_embedding("p"))
    e2 = await _seed_entity(tenant_id=tenant, canonical_name="Q", name_embedding=fake_embedding("q"))
    # Pre-existing related_to relation; co-occurrence will reinforce it.
    # Natural key orders from<to, so seed from = min(e1,e2).
    lo, hi = sorted([e1, e2])
    rel = await _seed_relation(
        tenant_id=tenant, from_entity_id=lo, to_entity_id=hi, relation_type="related_to", weight=0.95
    )
    for i in range(3):
        m = await _seed_memory(tenant_id=tenant, content=f"m{i}")
        await _seed_link(memory_id=m, entity_id=e1)
        await _seed_link(memory_id=m, entity_id=e2)

    resp = await sc.infer_relations(
        tenant_id=tenant,
        fleet_id=None,
        batch_size=500,
        min_cooccurrence=2,
        reinforce_delta=0.1,
        max_relation_weight=1.0,
    )
    assert resp["relations_created"] == 0
    assert resp["relations_reinforced"] == 1
    # 0.95 + 3*0.1 = 1.25 → clamped in Python to max 1.0 (NOT a SQL LEAST).
    assert await _relation_weight(rel) == pytest.approx(1.0)


async def test_infer_no_cooccurrence_returns_zero(sc):
    tenant = _t()
    e1 = await _seed_entity(tenant_id=tenant, canonical_name="A", name_embedding=fake_embedding("a"))
    e2 = await _seed_entity(tenant_id=tenant, canonical_name="B", name_embedding=fake_embedding("b"))
    # Only one shared memory → cooccur=1 < min 2.
    m = await _seed_memory(tenant_id=tenant, content="m")
    await _seed_link(memory_id=m, entity_id=e1)
    await _seed_link(memory_id=m, entity_id=e2)

    resp = await sc.infer_relations(
        tenant_id=tenant,
        fleet_id=None,
        batch_size=500,
        min_cooccurrence=2,
        reinforce_delta=0.1,
        max_relation_weight=1.0,
    )
    assert resp == {"skipped": True, "relations_created": 0, "relations_reinforced": 0}


# ===========================================================================
# D. list-null-embeddings + set-embeddings
# ===========================================================================


async def test_list_null_embeddings_returns_only_null_rows(sc):
    tenant = _t()
    null_a = await _seed_entity(tenant_id=tenant, canonical_name="NeedsEmbed A", name_embedding=None)
    null_b = await _seed_entity(tenant_id=tenant, canonical_name="NeedsEmbed B", name_embedding=None)
    await _seed_entity(tenant_id=tenant, canonical_name="HasEmbed", name_embedding=fake_embedding("has"))

    rows = await sc.list_null_embedding_entities(tenant_id=tenant, fleet_id=None, batch_size=100)
    ids = {r["id"] for r in rows}
    assert ids == {null_a, null_b}
    names = {r["canonical_name"] for r in rows}
    assert names == {"NeedsEmbed A", "NeedsEmbed B"}


async def test_set_embeddings_writes_back(sc):
    tenant = _t()
    eid = await _seed_entity(tenant_id=tenant, canonical_name="ToEmbed", name_embedding=None)
    emb = fake_embedding("toembed")

    count = await sc.set_entity_embeddings(
        tenant_id=tenant, updates=[{"id": eid, "embedding": emb}]
    )
    assert count == 1
    # The row no longer appears in the NULL-embedding list.
    rows = await sc.list_null_embedding_entities(tenant_id=tenant, fleet_id=None, batch_size=100)
    assert eid not in {r["id"] for r in rows}


async def test_set_embeddings_tenant_isolation(sc):
    t_a, t_b = _t(), _t()
    eid = await _seed_entity(tenant_id=t_a, canonical_name="A-only", name_embedding=None)
    # Tenant B cannot write tenant A's embedding (count reports attempted len,
    # but the tenant-scoped WHERE matches nothing → row stays NULL).
    await sc.set_entity_embeddings(tenant_id=t_b, updates=[{"id": eid, "embedding": fake_embedding("x")}])
    rows = await sc.list_null_embedding_entities(tenant_id=t_a, fleet_id=None, batch_size=100)
    assert eid in {r["id"] for r in rows}


# ===========================================================================
# E. 422 fail-closed guards (raw httpx — typed client never sends these)
# ===========================================================================

_PREFIX = "/api/v1/storage/entities"


async def test_resolve_missing_tenant_422(storage_http):
    resp = await storage_http.post(
        f"{_PREFIX}/resolve",
        json={"batch_size": 100, "threshold": 0.85, "candidate_limit": 3},
    )
    assert resp.status_code == 422


async def test_resolve_non_numeric_threshold_422(storage_http):
    resp = await storage_http.post(
        f"{_PREFIX}/resolve",
        json={"tenant_id": "t", "batch_size": 100, "threshold": "high", "candidate_limit": 3},
    )
    assert resp.status_code == 422


async def test_discover_missing_tenant_422(storage_http):
    resp = await storage_http.post(
        f"{_PREFIX}/discover-cross-links",
        json={"batch_size": 200, "threshold": 0.75, "text_verify": True},
    )
    assert resp.status_code == 422


async def test_discover_invalid_target_memory_id_422(storage_http):
    resp = await storage_http.post(
        f"{_PREFIX}/discover-cross-links",
        json={
            "tenant_id": "t",
            "batch_size": 200,
            "threshold": 0.75,
            "text_verify": True,
            "target_memory_ids": ["not-a-uuid"],
        },
    )
    assert resp.status_code == 422


async def test_infer_missing_tenant_422(storage_http):
    resp = await storage_http.post(
        f"{_PREFIX}/infer-relations",
        json={"batch_size": 500, "min_cooccurrence": 2, "reinforce_delta": 0.1, "max_relation_weight": 1.0},
    )
    assert resp.status_code == 422


async def test_infer_non_numeric_min_cooccurrence_422(storage_http):
    resp = await storage_http.post(
        f"{_PREFIX}/infer-relations",
        json={
            "tenant_id": "t",
            "batch_size": 500,
            "min_cooccurrence": "two",
            "reinforce_delta": 0.1,
            "max_relation_weight": 1.0,
        },
    )
    assert resp.status_code == 422


async def test_list_null_embeddings_missing_tenant_422(storage_http):
    resp = await storage_http.post(
        f"{_PREFIX}/list-null-embeddings",
        json={"batch_size": 100},
    )
    assert resp.status_code == 422


async def test_set_embeddings_missing_tenant_422(storage_http):
    resp = await storage_http.post(
        f"{_PREFIX}/set-embeddings",
        json={"updates": []},
    )
    assert resp.status_code == 422


async def test_set_embeddings_non_list_updates_422(storage_http):
    resp = await storage_http.post(
        f"{_PREFIX}/set-embeddings",
        json={"tenant_id": "t", "updates": "not-a-list"},
    )
    assert resp.status_code == 422


async def test_set_embeddings_invalid_uuid_in_updates_422(storage_http):
    resp = await storage_http.post(
        f"{_PREFIX}/set-embeddings",
        json={"tenant_id": "t", "updates": [{"id": "not-a-uuid", "embedding": [0.1]}]},
    )
    assert resp.status_code == 422

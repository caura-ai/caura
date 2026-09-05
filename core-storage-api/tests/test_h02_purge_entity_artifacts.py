"""H-02 — removing the graph rows mined out of a governance-dropped memory.

Against real Postgres, because the thing under test is a three-statement delete
whose correctness is entirely about what it does and does NOT reach. A stub
would assert the code calls itself.

The schema already says these rows must not outlive the memory:
``memory_entity_links.memory_id`` is ``ON DELETE CASCADE`` and
``relations.evidence_memory_id`` is ``ON DELETE SET NULL``. Both fire on a HARD
delete only. Governance soft-deletes, so neither ever fires — and the entity
names mined from dropped content stay listable tenant-wide.

The negative cases carry the weight here. This deletes rows, so a query that
reaches one row too far is worse than the leak it closes.
"""

import uuid

import pytest
from sqlalchemy import select

from common.models import Entity, MemoryEntityLink, Relation
from core_storage_api.services.postgres_service import PostgresService, get_session

pytestmark = pytest.mark.asyncio


async def _memory(svc: PostgresService, tenant: str):
    return await svc.memory_add(
        {
            "tenant_id": tenant,
            "agent_id": "h02-tester",
            "content": f"h02 canary {uuid.uuid4()}",
            "memory_type": "fact",
            "weight": 0.5,
            "status": "active",
            "visibility": "scope_team",
        }
    )


async def _entity(svc: PostgresService, tenant: str, name: str):
    return await svc.entity_add({"tenant_id": tenant, "entity_type": "person", "canonical_name": name})


async def _link(memory_id, entity_id, role: str = "subject"):
    async with get_session() as s:
        s.add(MemoryEntityLink(memory_id=memory_id, entity_id=entity_id, role=role))


async def _relation(tenant: str, frm, to, evidence):
    async with get_session() as s:
        rel = Relation(
            tenant_id=tenant,
            from_entity_id=frm,
            relation_type="knows",
            to_entity_id=to,
            evidence_memory_id=evidence,
        )
        s.add(rel)


async def _entity_names(tenant: str) -> set[str]:
    async with get_session() as s:
        rows = await s.execute(select(Entity.canonical_name).where(Entity.tenant_id == tenant))
        return set(rows.scalars().all())


async def _link_entity_ids(memory_id) -> set:
    async with get_session() as s:
        rows = await s.execute(
            select(MemoryEntityLink.entity_id).where(MemoryEntityLink.memory_id == memory_id)
        )
        return set(rows.scalars().all())


async def test_purges_links_relations_and_the_orphaned_entity():
    """The whole point: the PII name must stop being listable."""
    svc = PostgresService()
    tenant = f"h02-{uuid.uuid4().hex[:8]}"
    mem = await _memory(svc, tenant)
    alice = await _entity(svc, tenant, f"Alice {uuid.uuid4().hex[:6]}")
    bob = await _entity(svc, tenant, f"Bob {uuid.uuid4().hex[:6]}")
    await _link(mem.id, alice.id)
    await _link(mem.id, bob.id)
    await _relation(tenant, alice.id, bob.id, mem.id)

    counts = await svc.memory_purge_entity_artifacts(tenant_id=tenant, memory_id=mem.id)

    assert counts == {"links": 2, "relations": 1, "entities": 2}, counts
    assert await _entity_names(tenant) == set()


async def test_keeps_an_entity_another_live_memory_still_asserts():
    """The name is not this memory's to remove once something else asserts it.

    This is the assertion that separates a targeted purge from a graph wipe.
    """
    svc = PostgresService()
    tenant = f"h02-{uuid.uuid4().hex[:8]}"
    dropped = await _memory(svc, tenant)
    survivor = await _memory(svc, tenant)
    shared = await _entity(svc, tenant, f"Shared {uuid.uuid4().hex[:6]}")
    await _link(dropped.id, shared.id)
    await _link(survivor.id, shared.id)

    counts = await svc.memory_purge_entity_artifacts(tenant_id=tenant, memory_id=dropped.id)

    assert counts["links"] == 1
    assert counts["entities"] == 0, "an entity another memory still links to was deleted"
    assert len(await _entity_names(tenant)) == 1


async def test_leaves_unrelated_orphans_alone():
    """OVER-DELETION GUARD, and the reason the candidate set is bounded.

    A first draft of this deleted every entity in the tenant with no links —
    which would sweep entities orphaned for unrelated reasons and race an entity
    a concurrent write had created but not yet linked. The purge must only
    consider entities THIS memory touched.
    """
    svc = PostgresService()
    tenant = f"h02-{uuid.uuid4().hex[:8]}"
    mem = await _memory(svc, tenant)
    mine = await _entity(svc, tenant, f"Mine {uuid.uuid4().hex[:6]}")
    await _link(mem.id, mine.id)
    # Linked to nothing, touched by nobody — exactly the shape a broad
    # "delete orphans" query would take with it.
    stranger_name = f"Stranger {uuid.uuid4().hex[:6]}"
    await _entity(svc, tenant, stranger_name)

    counts = await svc.memory_purge_entity_artifacts(tenant_id=tenant, memory_id=mem.id)

    assert counts["entities"] == 1
    assert await _entity_names(tenant) == {stranger_name}


async def test_does_not_cross_tenants():
    """One tenant's drop must not reach another tenant's graph."""
    svc = PostgresService()
    tenant_a = f"h02a-{uuid.uuid4().hex[:8]}"
    tenant_b = f"h02b-{uuid.uuid4().hex[:8]}"
    mem_a = await _memory(svc, tenant_a)
    ent_a = await _entity(svc, tenant_a, f"A {uuid.uuid4().hex[:6]}")
    await _link(mem_a.id, ent_a.id)

    b_name = f"B {uuid.uuid4().hex[:6]}"
    ent_b = await _entity(svc, tenant_b, b_name)
    mem_b = await _memory(svc, tenant_b)
    await _link(mem_b.id, ent_b.id)
    await _relation(tenant_b, ent_b.id, ent_b.id, mem_b.id)

    await svc.memory_purge_entity_artifacts(tenant_id=tenant_a, memory_id=mem_a.id)

    assert await _entity_names(tenant_b) == {b_name}


async def test_a_tenant_that_does_not_own_the_memory_deletes_nothing():
    """The pairing is an authorisation, not just an identifier.

    ``test_does_not_cross_tenants`` above passes a MATCHED tenant/memory pair, so
    it never exercised this: keyed on ``memory_id`` alone, the link delete removed
    the owning tenant's rows for any memory_id a caller could name, and returned a
    success response saying so. ``memory_entity_links`` has no ``tenant_id`` column
    to make that visible, which is exactly why it has to be joined for.
    """
    svc = PostgresService()
    owner = f"h02own-{uuid.uuid4().hex[:8]}"
    other = f"h02oth-{uuid.uuid4().hex[:8]}"

    mem = await _memory(svc, owner)
    name = f"Owned {uuid.uuid4().hex[:6]}"
    ent = await _entity(svc, owner, name)
    await _link(mem.id, ent.id)
    await _relation(owner, ent.id, ent.id, mem.id)

    # ``other`` names a memory it does not own.
    counts = await svc.memory_purge_entity_artifacts(tenant_id=other, memory_id=mem.id)

    assert counts == {"links": 0, "relations": 0, "entities": 0}
    assert await _link_entity_ids(mem.id) == {ent.id}
    assert await _entity_names(owner) == {name}


async def test_a_link_to_another_tenants_entity_is_still_removed():
    """Pins the deliberate choice of the memory end over ``_link_within_tenant``.

    That helper requires BOTH ends in the tenant, because a read returning a
    straddling row hands back the other tenant's UUID. Deleting is a different
    question: this link points at content we are dropping, so it goes. The
    foreign ENTITY stays — the tenant-scoped entity delete never reaches it.

    Such rows are historical (the write path has refused to create them since
    #1085/#1124), which is precisely why requiring both ends would strand them.
    """
    svc = PostgresService()
    owner = f"h02str-{uuid.uuid4().hex[:8]}"
    other = f"h02frn-{uuid.uuid4().hex[:8]}"

    mem = await _memory(svc, owner)
    foreign_name = f"Foreign {uuid.uuid4().hex[:6]}"
    foreign_ent = await _entity(svc, other, foreign_name)
    await _link(mem.id, foreign_ent.id)

    counts = await svc.memory_purge_entity_artifacts(tenant_id=owner, memory_id=mem.id)

    assert counts["links"] == 1
    assert await _link_entity_ids(mem.id) == set()
    # Not ours to delete, and it is still listable in the tenant that owns it.
    assert counts["entities"] == 0
    assert await _entity_names(other) == {foreign_name}


async def test_a_relation_in_another_tenant_still_protects_the_entity():
    """The reference survives even when the row asserting it lives elsewhere.

    The "still referenced?" anti-joins are narrowed by the ENTITY's tenant, not
    by the referencing row's own ``tenant_id``. Scoping relations on
    ``Relation.tenant_id`` instead — the obvious narrowing, since the column is
    right there — would drop this straddling row out of the anti-join and delete
    an entity something still points at. Over-deleting is the direction that does
    not come back.
    """
    svc = PostgresService()
    owner = f"h02rel-{uuid.uuid4().hex[:8]}"
    other = f"h02out-{uuid.uuid4().hex[:8]}"

    mem = await _memory(svc, owner)
    name = f"Cited {uuid.uuid4().hex[:6]}"
    ent = await _entity(svc, owner, name)
    await _link(mem.id, ent.id)

    # A relation in ANOTHER tenant naming this tenant's entity. Historical shape:
    # the write path guards endpoints now, but old rows can still straddle.
    other_mem = await _memory(svc, other)
    await _relation(other, ent.id, ent.id, other_mem.id)

    counts = await svc.memory_purge_entity_artifacts(tenant_id=owner, memory_id=mem.id)

    assert counts["links"] == 1
    # The link went; the entity stayed, because something still asserts it.
    assert counts["entities"] == 0
    assert await _entity_names(owner) == {name}


async def test_a_memory_with_no_graph_rows_is_a_no_op():
    """The common case. Most dropped memories were never extracted from."""
    svc = PostgresService()
    tenant = f"h02-{uuid.uuid4().hex[:8]}"
    mem = await _memory(svc, tenant)
    kept = f"Untouched {uuid.uuid4().hex[:6]}"
    await _entity(svc, tenant, kept)

    counts = await svc.memory_purge_entity_artifacts(tenant_id=tenant, memory_id=mem.id)

    assert counts == {"links": 0, "relations": 0, "entities": 0}
    assert await _entity_names(tenant) == {kept}

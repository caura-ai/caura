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

from common.models import Entity, Memory, MemoryEntityLink, Relation
from common.models.entity import LINK_SOURCE_CALLER, LINK_SOURCE_EXTRACTION
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


async def _dropped_memory(svc: PostgresService, tenant: str):
    """A memory in the state the purge actually runs against.

    Governance soft-deletes and THEN purges, and the purge now refuses to run
    against a live row. Tests that skipped the delete were exercising a state no
    caller produces.
    """
    mem = await _memory(svc, tenant)
    await svc.memory_soft_delete_by_ids(tenant, [mem.id])
    return mem


async def _entity(svc: PostgresService, tenant: str, name: str):
    return await svc.entity_add({"tenant_id": tenant, "entity_type": "person", "canonical_name": name})


async def _link(memory_id, entity_id, role: str = "subject", source: str = LINK_SOURCE_EXTRACTION):
    """Seed one link. ``source`` defaults to extraction — what mining produces.

    Explicit in every call below rather than left to the column default, because
    the column default is deliberately the OPPOSITE (``caller``, the
    conservative value for rows that predate the column) and a test that
    silently inherited it would be asserting against a provenance it never
    chose.
    """
    async with get_session() as s:
        s.add(MemoryEntityLink(memory_id=memory_id, entity_id=entity_id, role=role, source=source))


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
    mem = await _dropped_memory(svc, tenant)
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
    dropped = await _dropped_memory(svc, tenant)
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
    mem = await _dropped_memory(svc, tenant)
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
    mem_a = await _dropped_memory(svc, tenant_a)
    ent_a = await _entity(svc, tenant_a, f"A {uuid.uuid4().hex[:6]}")
    await _link(mem_a.id, ent_a.id)

    b_name = f"B {uuid.uuid4().hex[:6]}"
    ent_b = await _entity(svc, tenant_b, b_name)
    mem_b = await _memory(svc, tenant_b)
    await _link(mem_b.id, ent_b.id)
    await _relation(tenant_b, ent_b.id, ent_b.id, mem_b.id)

    await svc.memory_purge_entity_artifacts(tenant_id=tenant_a, memory_id=mem_a.id)

    assert await _entity_names(tenant_b) == {b_name}


async def test_a_live_memory_is_never_purged():
    """The invariant enforced where the deleting happens, not in the callers.

    Both callers check that the memory is dropped before calling. This asserts
    the method does not TRUST them: it deletes across three tables and cannot be
    undone, so a stale call, a reordering, or a future caller written from the
    method name alone must not be able to wipe a live memory's graph.

    Covers all three deletes, not just the links — the relation delete never took
    the ownership subquery, so guarding only the link path would have left a live
    memory losing its relations while its links and entities survived.
    """
    svc = PostgresService()
    tenant = f"h02live-{uuid.uuid4().hex[:8]}"

    mem = await _memory(svc, tenant)  # LIVE — deliberately not dropped
    name = f"Live {uuid.uuid4().hex[:6]}"
    ent = await _entity(svc, tenant, name)
    await _link(mem.id, ent.id)
    await _relation(tenant, ent.id, ent.id, mem.id)

    counts = await svc.memory_purge_entity_artifacts(tenant_id=tenant, memory_id=mem.id)

    assert counts == {"links": 0, "relations": 0, "entities": 0}
    assert await _link_entity_ids(mem.id) == {ent.id}
    assert await _entity_names(tenant) == {name}
    async with get_session() as s:
        rels = await s.execute(select(Relation.id).where(Relation.evidence_memory_id == mem.id))
        assert len(list(rels.scalars().all())) == 1, "a live memory's relation was purged"


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

    mem = await _dropped_memory(svc, owner)
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

    mem = await _dropped_memory(svc, owner)
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

    mem = await _dropped_memory(svc, owner)
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
    mem = await _dropped_memory(svc, tenant)
    kept = f"Untouched {uuid.uuid4().hex[:6]}"
    await _entity(svc, tenant, kept)

    counts = await svc.memory_purge_entity_artifacts(tenant_id=tenant, memory_id=mem.id)

    assert counts == {"links": 0, "relations": 0, "entities": 0}
    assert await _entity_names(tenant) == {kept}


# ---------------------------------------------------------------------------
# The fourth reference — ``memories.subject_entity_id``
# ---------------------------------------------------------------------------


async def test_an_entity_another_memory_calls_its_subject_is_kept():
    """The reference a link-and-relation-only anti-join misses.

    ``memories.subject_entity_id`` is the RDF subject pointer and a FK with
    ``ON DELETE SET NULL``. An entity that is some other live memory's subject
    but holds no links and no relations satisfied the three original anti-joins,
    so it was deleted and that memory's subject silently became NULL — a row
    losing a field nobody asked to change, recorded nowhere.

    Rare while this only ran on governance drops; routine once the reset path
    runs the same sequence on ordinary content edits.
    """
    svc = PostgresService()
    tenant = f"h02-{uuid.uuid4().hex[:8]}"
    dropped = await _dropped_memory(svc, tenant)
    subject_owner = await _memory(svc, tenant)
    shared = await _entity(svc, tenant, f"Subject {uuid.uuid4().hex[:6]}")

    # The ONLY thing keeping this entity alive is the other row's subject
    # pointer: one link from the dropped memory, no relations, no other link.
    await _link(dropped.id, shared.id)
    await svc.memory_update(str(subject_owner.id), tenant, {"subject_entity_id": str(shared.id)})

    counts = await svc.memory_purge_entity_artifacts(tenant_id=tenant, memory_id=dropped.id)

    assert counts["links"] == 1
    assert counts["entities"] == 0, "deleted an entity another memory names as its subject"
    assert len(await _entity_names(tenant)) == 1

    async with get_session() as s:
        still = (
            await s.execute(select(Memory.subject_entity_id).where(Memory.id == subject_owner.id))
        ).scalar_one()
    assert still == shared.id, "another memory's subject was silently set to NULL"


async def test_the_subject_anti_join_does_not_disable_deletion():
    """The over-fix guard, and it is a live NULL trap.

    ``subject_entity_id`` is nullable and almost always NULL. An unbounded
    ``NOT IN`` over a set containing NULL matches NOTHING in SQL, so writing
    this anti-join without restricting it to the candidate set would have turned
    entity deletion off entirely — every orphan silently kept, with every test
    above still passing on counts of links and relations.
    """
    svc = PostgresService()
    tenant = f"h02-{uuid.uuid4().hex[:8]}"
    dropped = await _dropped_memory(svc, tenant)
    # A second row with a NULL subject, which is what the real table is full of.
    await _memory(svc, tenant)
    orphan = await _entity(svc, tenant, f"Orphan {uuid.uuid4().hex[:6]}")
    await _link(dropped.id, orphan.id)

    counts = await svc.memory_purge_entity_artifacts(tenant_id=tenant, memory_id=dropped.id)

    assert counts["entities"] == 1, "NULL subjects suppressed the orphan delete"
    assert await _entity_names(tenant) == set()


# ---------------------------------------------------------------------------
# The live-row twin
# ---------------------------------------------------------------------------


async def test_the_reset_clears_a_live_rows_graph():
    """``memory_reset_entity_artifacts`` — the same sequence, opposite guard.

    An edited memory keeps the links extraction mined from content it no longer
    holds, because extraction only ever adds.
    """
    svc = PostgresService()
    tenant = f"h02-{uuid.uuid4().hex[:8]}"
    mem = await _memory(svc, tenant)
    alice = await _entity(svc, tenant, f"Alice {uuid.uuid4().hex[:6]}")
    bob = await _entity(svc, tenant, f"Bob {uuid.uuid4().hex[:6]}")
    await _link(mem.id, alice.id)
    await _link(mem.id, bob.id)
    await _relation(tenant, alice.id, bob.id, mem.id)

    counts = await svc.memory_reset_entity_artifacts(tenant_id=tenant, memory_id=mem.id)

    assert counts == {"links": 2, "relations": 1, "entities": 2}, counts
    assert await _entity_names(tenant) == set()


async def test_the_reset_refuses_a_dropped_row():
    """Each name carries its own guard; neither can do the other's job.

    Reaching a soft-deleted row through the reset would clear the graph of a
    governance-dropped memory under a name that says nothing about drops.
    """
    svc = PostgresService()
    tenant = f"h02-{uuid.uuid4().hex[:8]}"
    mem = await _dropped_memory(svc, tenant)
    alice = await _entity(svc, tenant, f"Alice {uuid.uuid4().hex[:6]}")
    await _link(mem.id, alice.id)

    counts = await svc.memory_reset_entity_artifacts(tenant_id=tenant, memory_id=mem.id)

    assert counts == {"links": 0, "relations": 0, "entities": 0}
    assert await _link_entity_ids(mem.id) == {alice.id}


async def test_the_purge_refuses_a_live_row():
    """The complement, restated now that a live row HAS a supported path.

    The guard is about provenance, not safety: clearing a live row's graph is
    supported and has its own name. What this refuses is doing it under a name
    that reads as governance.
    """
    svc = PostgresService()
    tenant = f"h02-{uuid.uuid4().hex[:8]}"
    mem = await _memory(svc, tenant)
    alice = await _entity(svc, tenant, f"Alice {uuid.uuid4().hex[:6]}")
    await _link(mem.id, alice.id)

    counts = await svc.memory_purge_entity_artifacts(tenant_id=tenant, memory_id=mem.id)

    assert counts == {"links": 0, "relations": 0, "entities": 0}
    assert await _link_entity_ids(mem.id) == {alice.id}


async def test_the_reset_does_not_cross_tenants():
    """The guard is an authorisation, not a lookup — same as the purge's."""
    svc = PostgresService()
    owner = f"h02-{uuid.uuid4().hex[:8]}"
    other = f"h02-{uuid.uuid4().hex[:8]}"
    mem = await _memory(svc, owner)
    alice = await _entity(svc, owner, f"Alice {uuid.uuid4().hex[:6]}")
    await _link(mem.id, alice.id)

    counts = await svc.memory_reset_entity_artifacts(tenant_id=other, memory_id=mem.id)

    assert counts == {"links": 0, "relations": 0, "entities": 0}
    assert await _link_entity_ids(mem.id) == {alice.id}


# ---------------------------------------------------------------------------
# Provenance — the reset spares what a caller curated
# ---------------------------------------------------------------------------


async def test_the_reset_keeps_a_caller_curated_link():
    """``entity_links`` is a caller-owned additive API, and extraction mines text.

    So a link a caller added to tag a memory with a project or person its text
    never names is one extraction will NEVER recreate. A reset that cleared
    every link destroyed it permanently on the next content edit, with no signal
    to the caller — whose PATCH need not have mentioned ``entity_links`` at all.
    ``tests/test_entity_links_are_additive.py`` already ruled on this shape: a
    shipped endpoint must not silently delete links a caller did not name.
    """
    svc = PostgresService()
    tenant = f"h02-{uuid.uuid4().hex[:8]}"
    mem = await _memory(svc, tenant)
    curated = await _entity(svc, tenant, f"Curated {uuid.uuid4().hex[:6]}")
    mined = await _entity(svc, tenant, f"Mined {uuid.uuid4().hex[:6]}")
    await _link(mem.id, curated.id, source=LINK_SOURCE_CALLER)
    await _link(mem.id, mined.id, source=LINK_SOURCE_EXTRACTION)

    counts = await svc.memory_reset_entity_artifacts(tenant_id=tenant, memory_id=mem.id)

    assert counts["links"] == 1, "the reset took a link that was not extraction's"
    assert await _link_entity_ids(mem.id) == {curated.id}


async def test_an_entity_held_only_by_a_caller_link_is_not_swept():
    """Narrowing the links must narrow the orphan candidates with them.

    Otherwise the entity behind a surviving caller link would still be a
    candidate, and — holding no other links or relations — would be hard
    deleted, taking its attributes and embeddings with it and leaving the link
    pointing nowhere.
    """
    svc = PostgresService()
    tenant = f"h02-{uuid.uuid4().hex[:8]}"
    mem = await _memory(svc, tenant)
    curated = await _entity(svc, tenant, f"Curated {uuid.uuid4().hex[:6]}")
    await _link(mem.id, curated.id, source=LINK_SOURCE_CALLER)

    counts = await svc.memory_reset_entity_artifacts(tenant_id=tenant, memory_id=mem.id)

    assert counts == {"links": 0, "relations": 0, "entities": 0}
    assert len(await _entity_names(tenant)) == 1


async def test_the_purge_takes_caller_links_too():
    """No carve-out on the drop path, and that is the correct asymmetry.

    Governance dropped the memory, so nothing mined OR curated from it has any
    justification left — the whole point of H-02 is that a name from dropped
    content stops being listable, and "the caller asked for this one" is not an
    exemption from a retention decision.
    """
    svc = PostgresService()
    tenant = f"h02-{uuid.uuid4().hex[:8]}"
    mem = await _dropped_memory(svc, tenant)
    curated = await _entity(svc, tenant, f"Curated {uuid.uuid4().hex[:6]}")
    await _link(mem.id, curated.id, source=LINK_SOURCE_CALLER)

    counts = await svc.memory_purge_entity_artifacts(tenant_id=tenant, memory_id=mem.id)

    assert counts["links"] == 1
    assert counts["entities"] == 1
    assert await _entity_names(tenant) == set()


async def test_extraction_does_not_take_over_a_caller_link():
    """Re-mining an entity a caller curated must not reclassify the row.

    The caller asked for that link to exist; a later edit dropping the mention
    must not delete it. ``entity_bulk_upsert_links`` therefore leaves ``source``
    out of its ON CONFLICT SET.
    """
    svc = PostgresService()
    tenant = f"h02-{uuid.uuid4().hex[:8]}"
    mem = await _memory(svc, tenant)
    shared = await _entity(svc, tenant, f"Shared {uuid.uuid4().hex[:6]}")
    await _link(mem.id, shared.id, source=LINK_SOURCE_CALLER)

    await svc.entity_bulk_upsert_links(
        tenant,
        [{"input_idx": 0, "memory_id": mem.id, "entity_id": shared.id, "role": "object"}],
    )

    counts = await svc.memory_reset_entity_artifacts(tenant_id=tenant, memory_id=mem.id)

    assert counts["links"] == 0, "extraction reclassified a caller's link and the reset took it"
    assert await _link_entity_ids(mem.id) == {shared.id}


async def test_extraction_writes_extraction_provenance():
    """The over-fix guard. Sparing caller links must not spare everything.

    If ``entity_bulk_upsert_links`` stopped stamping its own rows they would
    inherit the conservative ``caller`` default and become undeletable — the
    stale-link bug back in full, with every count assertion above still passing.
    """
    svc = PostgresService()
    tenant = f"h02-{uuid.uuid4().hex[:8]}"
    mem = await _memory(svc, tenant)
    mined = await _entity(svc, tenant, f"Mined {uuid.uuid4().hex[:6]}")

    await svc.entity_bulk_upsert_links(
        tenant,
        [{"input_idx": 0, "memory_id": mem.id, "entity_id": mined.id, "role": "subject"}],
    )

    counts = await svc.memory_reset_entity_artifacts(tenant_id=tenant, memory_id=mem.id)

    assert counts["links"] == 1, "an extraction-written link survived the reset"
    assert await _link_entity_ids(mem.id) == set()


async def test_the_patch_link_writer_stamps_caller():
    """The other half: ``PATCH /memories/{id}`` entity_links must be caller-sourced.

    If ``memory_add_entity_links`` stopped stamping, its rows would still get
    the ``caller`` default today and this would pass for the wrong reason — so
    the assertion is on the stored value, not on survival.
    """
    svc = PostgresService()
    tenant = f"h02-{uuid.uuid4().hex[:8]}"
    mem = await _memory(svc, tenant)
    tagged = await _entity(svc, tenant, f"Tagged {uuid.uuid4().hex[:6]}")

    assert await svc.memory_add_entity_links(mem.id, tenant, [{"entity_id": tagged.id, "role": "subject"}])

    async with get_session() as s:
        stored = (
            await s.execute(select(MemoryEntityLink.source).where(MemoryEntityLink.memory_id == mem.id))
        ).scalar_one()
    assert stored == LINK_SOURCE_CALLER

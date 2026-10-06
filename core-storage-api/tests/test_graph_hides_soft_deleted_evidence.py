"""Graph data mined from a soft-deleted memory is hidden from every reader (M-92).

Agents already lost it when the memory was soft-deleted: their entity lists keep
only entities linked to a memory they may read, and their relation filter drops
edges whose evidence they cannot read. Tenant, user and admin readers kept all
of it until the retention purge hard-deleted the memory, so a deleted note's
names and triples stayed on the dashboard for the whole undo window.

Now an entity whose every link is to a soft-deleted memory is hidden from the
entity list and the graph for those readers too, and a relation is hidden from
every reader once it has evidence and none of that evidence is live. Both come
back if the delete is undone. Entities and relations with no memory behind them
stay visible, as they do for agents.

Search's graph expansion (``entity_expand_graph``) walked every edge, so one
mined only from a deleted memory still decided which entities, and so which
memories, graph boosting lifted. It follows the same rule now.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import update

from common.models import Memory, MemoryEntityLink, Relation, RelationEvidence
from common.models.entity import LINK_SOURCE_EXTRACTION
from core_storage_api.services.postgres_service import PostgresService, get_session

pytestmark = pytest.mark.asyncio


def _tenant() -> str:
    return f"test-tenant-{uuid.uuid4().hex[:8]}"


async def _memory(svc: PostgresService, tenant: str):
    return await svc.memory_add(
        {
            "tenant_id": tenant,
            "agent_id": "m92-tester",
            "content": f"m92 canary {uuid.uuid4()}",
            "memory_type": "fact",
            "weight": 0.5,
            "status": "active",
            "visibility": "scope_team",
        }
    )


async def _entity(svc: PostgresService, tenant: str, name: str):
    return await svc.entity_add(
        {"tenant_id": tenant, "entity_type": "person", "canonical_name": f"{name} {uuid.uuid4().hex[:6]}"}
    )


async def _link(memory_id: uuid.UUID, entity_id: uuid.UUID) -> None:
    link = MemoryEntityLink(
        memory_id=memory_id, entity_id=entity_id, role="subject", source=LINK_SOURCE_EXTRACTION
    )
    async with get_session() as s:
        s.add(link)


async def _relation(tenant: str, frm: uuid.UUID, to: uuid.UUID, *evidence: uuid.UUID) -> uuid.UUID:
    """An edge whose latest evidence is the first memory given; every one is in ``relation_evidence``."""
    async with get_session() as s:
        rel = Relation(
            tenant_id=tenant,
            from_entity_id=frm,
            relation_type=f"knows-{uuid.uuid4().hex[:6]}",
            to_entity_id=to,
            evidence_memory_id=evidence[0] if evidence else None,
        )
        s.add(rel)
        await s.flush()
        for memory_id in evidence:
            s.add(RelationEvidence(relation_id=rel.id, memory_id=memory_id))
        return rel.id


async def _undo_delete(memory_id: uuid.UUID) -> None:
    """There is no restore endpoint, so an undo is the row's ``deleted_at`` cleared."""
    async with get_session() as s:
        await s.execute(update(Memory).where(Memory.id == memory_id).values(deleted_at=None, status="active"))


async def _outgoing_ids(svc: PostgresService, entity_id: uuid.UUID, tenant: str) -> set[uuid.UUID]:
    return {rel.id for rel, _target in await svc.relation_get_outgoing(entity_id, tenant)}


async def test_a_tenant_reader_stops_listing_an_entity_mined_only_from_deleted_memories() -> None:
    svc = PostgresService()
    tenant = _tenant()
    note, kept = await _memory(svc, tenant), await _memory(svc, tenant)
    mined = await _entity(svc, tenant, "Zenith")
    shared = await _entity(svc, tenant, "Quarterly")
    manual = await _entity(svc, tenant, "Manual")
    await _link(note.id, mined.id)
    await _link(note.id, shared.id)
    await _link(kept.id, shared.id)

    await svc.memory_soft_delete_by_ids(tenant, [note.id])

    listed = {e.id for e in await svc.entity_list(tenant)}
    assert mined.id not in listed
    assert {shared.id, manual.id} <= listed


async def test_the_tenant_graph_hides_those_entities_and_every_edge_that_names_them() -> None:
    svc = PostgresService()
    tenant = _tenant()
    note, kept = await _memory(svc, tenant), await _memory(svc, tenant)
    mined = await _entity(svc, tenant, "Zenith")
    shared = await _entity(svc, tenant, "Quarterly")
    manual = await _entity(svc, tenant, "Manual")
    await _link(note.id, mined.id)
    await _link(kept.id, shared.id)
    to_hidden_node = await _relation(tenant, shared.id, mined.id, kept.id)
    deleted_evidence = await _relation(tenant, shared.id, manual.id, note.id)
    no_evidence = await _relation(tenant, manual.id, shared.id)

    await svc.memory_soft_delete_by_ids(tenant, [note.id])

    entities, relations = await svc.entity_get_full_graph(tenant)
    assert mined.id not in {e.id for e in entities}
    edges = {r.id for r in relations}
    assert to_hidden_node not in edges
    assert deleted_evidence not in edges
    assert no_evidence in edges


async def test_a_relation_with_no_live_evidence_is_hidden_from_every_reader() -> None:
    svc = PostgresService()
    tenant = _tenant()
    note, kept = await _memory(svc, tenant), await _memory(svc, tenant)
    hub = await _entity(svc, tenant, "Hub")
    targets = [await _entity(svc, tenant, f"Target {i}") for i in range(3)]
    for target in (hub, *targets):
        await _link(kept.id, target.id)
    only_deleted = await _relation(tenant, hub.id, targets[0].id, note.id)
    # Its latest evidence is the deleted note, but a live memory asserted it too.
    still_asserted = await _relation(tenant, hub.id, targets[1].id, note.id, kept.id)
    no_evidence = await _relation(tenant, hub.id, targets[2].id)

    await svc.memory_soft_delete_by_ids(tenant, [note.id])

    assert await _outgoing_ids(svc, hub.id, tenant) == {still_asserted, no_evidence}
    for caller in ({}, {"caller_agent_id": "m92-tester"}):
        _entities, relations = await svc.entity_get_full_graph(tenant, **caller)
        assert only_deleted not in {r.id for r in relations}, caller


async def test_undoing_the_delete_brings_the_entity_and_its_edges_back() -> None:
    svc = PostgresService()
    tenant = _tenant()
    note, kept = await _memory(svc, tenant), await _memory(svc, tenant)
    mined = await _entity(svc, tenant, "Zenith")
    shared = await _entity(svc, tenant, "Quarterly")
    await _link(note.id, mined.id)
    await _link(kept.id, shared.id)
    edge = await _relation(tenant, shared.id, mined.id, note.id)
    await svc.memory_soft_delete_by_ids(tenant, [note.id])

    await _undo_delete(note.id)

    assert mined.id in {e.id for e in await svc.entity_list(tenant)}
    assert edge in await _outgoing_ids(svc, shared.id, tenant)
    _entities, relations = await svc.entity_get_full_graph(tenant)
    assert edge in {r.id for r in relations}


@pytest.mark.parametrize("use_union", [False, True])
async def test_graph_expansion_walks_only_edges_with_live_evidence(use_union: bool) -> None:
    svc = PostgresService()
    tenant = _tenant()
    note, kept = await _memory(svc, tenant), await _memory(svc, tenant)
    hub = await _entity(svc, tenant, "Hub")
    dead_out, live_out, bare_out, dead_in, live_in, beyond = [
        (await _entity(svc, tenant, name)).id
        for name in ("Dead out", "Live out", "Bare out", "Dead in", "Live in", "Beyond")
    ]
    await _relation(tenant, hub.id, dead_out, note.id)
    # Its latest evidence is the deleted note, but a live memory asserted it too.
    await _relation(tenant, hub.id, live_out, note.id, kept.id)
    await _relation(tenant, hub.id, bare_out)
    await _relation(tenant, dead_in, hub.id, note.id)
    await _relation(tenant, live_in, hub.id, kept.id)
    # A live edge, reachable only through the dead one: the second hop must not start there.
    await _relation(tenant, dead_out, beyond, kept.id)

    await svc.memory_soft_delete_by_ids(tenant, [note.id])

    hops = await svc.entity_expand_graph([hub.id], tenant, None, max_hops=2, use_union=use_union)
    assert set(hops) == {hub.id, live_out, bare_out, live_in}

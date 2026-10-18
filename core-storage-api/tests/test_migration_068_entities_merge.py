"""Migration 068: production's entities get the natural-key index (O24).

Production's ``entities`` table predates the migration chain and never got
``uq_entities_tenant_type_name_fleet``, which migration 001 creates. Without it,
``entity_add``'s ``ON CONFLICT`` cannot be inferred, and nothing stops two writers
creating one name twice: 11796 groups held 23881 rows on 2026-10-18. 068 merges
each group into its oldest entity and then builds the index.

The tests drop the index to fabricate duplicates, as production's table holds
them, and run the migration's own ``MERGE_SQL`` (loaded, not re-typed).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from core_storage_api.services.postgres_service import get_session
from tests.conftest import (
    ENTITY_MERGE_TENANT_PREFIX,
    UQ_ENTITIES_NATURAL_KEY,
    UQ_ENTITIES_NATURAL_KEY_SQL,
    load_migration_068,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

JAN = datetime(2026, 1, 1, tzinfo=UTC)
FEB = datetime(2026, 2, 1, tzinfo=UTC)
MAR = datetime(2026, 3, 1, tzinfo=UTC)


def _tenant() -> str:
    return f"{ENTITY_MERGE_TENANT_PREFIX}{uuid.uuid4().hex[:8]}"


async def _merge() -> None:
    async with get_session() as session:
        for statement in load_migration_068().MERGE_SQL:
            await session.execute(text(statement))


async def _entity(
    tenant: str,
    name: str,
    *,
    created_at: datetime = JAN,
    entity_type: str = "organization",
    fleet: str | None = None,
    attributes: object = None,
    entity_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """An entity inserted directly: ``entity_add`` cannot run without the index."""
    entity_id = entity_id or uuid.uuid4()
    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO entities "
                "(id, tenant_id, fleet_id, entity_type, canonical_name, attributes, created_at) "
                "VALUES (:id, :tenant, :fleet, :type, :name, CAST(:attributes AS json), :created_at)"
            ),
            {
                "id": entity_id,
                "tenant": tenant,
                "fleet": fleet,
                "type": entity_type,
                "name": name,
                "attributes": None if attributes is None else json.dumps(attributes),
                "created_at": created_at,
            },
        )
    return entity_id


async def _memory(tenant: str, *, subject: uuid.UUID | None = None) -> uuid.UUID:
    memory_id = uuid.uuid4()
    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO memories "
                "(id, tenant_id, agent_id, memory_type, content, content_hash, status, subject_entity_id) "
                "VALUES (:id, :tenant, 'o24-agent', 'fact', :content, :hash, 'active', :subject)"
            ),
            {
                "id": memory_id,
                "tenant": tenant,
                "content": f"body {memory_id}",
                "hash": memory_id.hex,
                "subject": subject,
            },
        )
    return memory_id


async def _link(memory_id: uuid.UUID, entity_id: uuid.UUID, role: str, source: str) -> None:
    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO memory_entity_links (memory_id, entity_id, role, source) "
                "VALUES (:m, :e, :role, :source)"
            ),
            {"m": memory_id, "e": entity_id, "role": role, "source": source},
        )


async def _relation(
    tenant: str,
    from_id: uuid.UUID,
    relation_type: str,
    to_id: uuid.UUID,
    *,
    weight: float = 1.0,
    evidence: tuple[uuid.UUID, ...] = (),
) -> uuid.UUID:
    relation_id = uuid.uuid4()
    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO relations (id, tenant_id, from_entity_id, relation_type, to_entity_id, weight) "
                "VALUES (:id, :tenant, :f, :type, :t, :w)"
            ),
            {
                "id": relation_id,
                "tenant": tenant,
                "f": from_id,
                "type": relation_type,
                "t": to_id,
                "w": weight,
            },
        )
        for memory_id in evidence:
            await session.execute(
                text("INSERT INTO relation_evidence (relation_id, memory_id) VALUES (:r, :m)"),
                {"r": relation_id, "m": memory_id},
            )
    return relation_id


async def _rows(sql: str, **params: object) -> list:
    async with get_session() as session:
        return list((await session.execute(text(sql), params)).all())


async def _entities(tenant: str) -> dict[uuid.UUID, tuple]:
    rows = await _rows(
        "SELECT id, canonical_name, attributes::jsonb AS attributes FROM entities WHERE tenant_id = :t",
        t=tenant,
    )
    return {
        r.id: (r.canonical_name, json.loads(r.attributes) if isinstance(r.attributes, str) else r.attributes)
        for r in rows
    }


async def _links(memory_id: uuid.UUID) -> set[tuple]:
    rows = await _rows(
        "SELECT entity_id, role, source FROM memory_entity_links WHERE memory_id = :m", m=memory_id
    )
    return {(r.entity_id, r.role, r.source) for r in rows}


async def _relations(tenant: str) -> dict[tuple, tuple]:
    rows = await _rows(
        "SELECT r.id, r.from_entity_id, r.relation_type, r.to_entity_id, r.weight, "
        "array_remove(array_agg(ev.memory_id), NULL) AS evidence FROM relations r "
        "LEFT JOIN relation_evidence ev ON ev.relation_id = r.id WHERE r.tenant_id = :t "
        "GROUP BY r.id",
        t=tenant,
    )
    return {
        (r.from_entity_id, r.relation_type, r.to_entity_id): (r.id, r.weight, set(r.evidence)) for r in rows
    }


async def test_the_oldest_survives_with_every_name_and_its_own_values_first(
    without_entities_natural_key_index,
) -> None:
    tenant = _tenant()
    oldest = await _entity(
        tenant,
        "Acme",
        created_at=JAN,
        attributes={"_aliases": ["Acme Inc"], "industry": "tools", "hq": "ohio"},
    )
    middle = await _entity(
        tenant,
        "ACME",
        created_at=FEB,
        attributes={"_aliases": ["acme corp"], "industry": "widgets", "size": "small"},
    )
    # No attributes at all: the merge reads a missing object as an empty one.
    newest = await _entity(tenant, "acme", created_at=MAR, attributes=None)

    await _merge()

    entities = await _entities(tenant)
    assert set(entities) == {oldest}, "the duplicates must go, and the oldest must stay"
    name, attributes = entities[oldest]
    assert name == "Acme", "the survivor keeps its own spelling"
    assert attributes["industry"] == "tools", "where both name a key, the survivor's value stays"
    assert attributes["size"] == "small", "a key only a duplicate held outlives it"
    assert attributes["hq"] == "ohio"
    assert attributes["_aliases"] == sorted({"Acme", "Acme Inc", "ACME", "acme corp", "acme"}), (
        "every member's name and aliases join, sorted as the nightly merge sorts them"
    )
    assert middle not in entities and newest not in entities


async def test_a_created_at_tie_keeps_the_lowest_id_in_every_group(
    without_entities_natural_key_index,
) -> None:
    """Rows from before migration 060 share ``created_at``, so the tie is common.
    One group cannot show the tie-break holds, since which tied row a sort emits
    is up to the plan; twenty groups can."""
    tenant = _tenant()
    groups: dict[str, list[uuid.UUID]] = {}
    for group in range(20):
        name = f"tie {group}"
        ids = sorted(uuid.uuid4() for _ in range(3))
        groups[name] = ids
        for entity_id in reversed(ids):
            await _entity(tenant, name, created_at=JAN, entity_id=entity_id)

    await _merge()

    survivors = {name: entity_id for entity_id, (name, _) in (await _entities(tenant)).items()}
    assert survivors == {name: ids[0] for name, ids in groups.items()}


async def test_links_move_to_the_survivor_once_keeping_a_callers_tag(
    without_entities_natural_key_index,
) -> None:
    tenant = _tenant()
    survivor = await _entity(tenant, "Globex", created_at=JAN)
    first, second = sorted(uuid.uuid4() for _ in range(2))
    await _entity(tenant, "globex", created_at=FEB, entity_id=first)
    await _entity(tenant, "GLOBEX", created_at=MAR, entity_id=second)

    only_a_duplicate = await _memory(tenant)
    await _link(only_a_duplicate, first, "subject", "caller")
    both = await _memory(tenant)
    await _link(both, survivor, "mentioned", "extraction")
    await _link(both, first, "subject", "caller")
    two_duplicates = await _memory(tenant)
    await _link(two_duplicates, first, "mentioned", "extraction")
    await _link(two_duplicates, second, "subject", "caller")
    extraction_only = await _memory(tenant)
    await _link(extraction_only, survivor, "mentioned", "extraction")
    await _link(extraction_only, second, "mentioned", "extraction")

    await _merge()

    assert await _links(only_a_duplicate) == {(survivor, "subject", "caller")}
    assert await _links(both) == {(survivor, "mentioned", "caller")}, (
        "one link stays, with the survivor's role; a caller's tag on the duplicate keeps it a caller's, "
        "so a content edit does not clear it"
    )
    assert await _links(two_duplicates) == {(survivor, "mentioned", "caller")}, (
        "the lower id's link stays, as the nightly merge keeps the first duplicate it moves"
    )
    assert await _links(extraction_only) == {(survivor, "mentioned", "extraction")}


async def test_relations_move_with_their_evidence_and_weight_and_lose_self_loops(
    without_entities_natural_key_index,
) -> None:
    tenant = _tenant()
    survivor = await _entity(tenant, "Initech", created_at=JAN)
    duplicate = await _entity(tenant, "initech", created_at=FEB)
    other = await _entity(tenant, "Hooli", created_at=JAN)
    third = await _entity(tenant, "Umbrella", created_at=JAN)
    target = await _entity(tenant, "Wayne", created_at=JAN)
    target_duplicate = await _entity(tenant, "wayne", created_at=FEB)
    said_once, said_again = await _memory(tenant), await _memory(tenant)

    kept = await _relation(tenant, survivor, "competes_with", other, weight=0.4, evidence=(said_once,))
    await _relation(tenant, duplicate, "competes_with", other, weight=0.9, evidence=(said_again,))
    outgoing = await _relation(tenant, duplicate, "partners_with", third, weight=0.5)
    incoming = await _relation(tenant, other, "acquired", duplicate, weight=0.7)
    await _relation(tenant, duplicate, "same_as", survivor)
    both_ends = await _relation(tenant, duplicate, "supplies", target_duplicate, weight=0.3)

    await _merge()

    relations = await _relations(tenant)
    assert relations[(survivor, "competes_with", other)] == (kept, 0.9, {said_once, said_again}), (
        "the colliding duplicate folds into the survivor's relation: higher weight, every asserting memory"
    )
    assert relations[(survivor, "partners_with", third)][0] == outgoing
    assert relations[(other, "acquired", survivor)][0] == incoming
    assert relations[(survivor, "supplies", target)][0] == both_ends, (
        "a relation between two groups moves at both ends"
    )
    assert (survivor, "same_as", survivor) not in relations, (
        "a relation between two members becomes a self-loop, and goes"
    )
    assert len(relations) == 4


async def test_subjects_follow_the_survivor(without_entities_natural_key_index) -> None:
    tenant = _tenant()
    survivor = await _entity(tenant, "Soylent", created_at=JAN)
    duplicate = await _entity(tenant, "soylent", created_at=FEB)
    about_the_duplicate = await _memory(tenant, subject=duplicate)
    about_the_survivor = await _memory(tenant, subject=survivor)

    await _merge()

    rows = await _rows(
        "SELECT id, subject_entity_id FROM memories WHERE id = ANY(:ids)",
        ids=[about_the_duplicate, about_the_survivor],
    )
    assert {r.id: r.subject_entity_id for r in rows} == {
        about_the_duplicate: survivor,
        about_the_survivor: survivor,
    }


async def test_only_rows_sharing_the_whole_key_merge(without_entities_natural_key_index) -> None:
    """Type, fleet and tenant are part of the key; case is not, and a NULL fleet
    and ``''`` are one fleet, as the index's ``COALESCE`` makes them."""
    tenant, other_tenant = _tenant(), _tenant()
    person = await _entity(tenant, "Stark", entity_type="person", created_at=JAN)
    company = await _entity(tenant, "Stark", entity_type="organization", created_at=JAN)
    fleet_a = await _entity(tenant, "Stark", entity_type="project", fleet="fleet-a", created_at=JAN)
    fleet_b = await _entity(tenant, "Stark", entity_type="project", fleet="fleet-b", created_at=JAN)
    no_fleet = await _entity(tenant, "Pym", fleet=None, created_at=JAN)
    await _entity(tenant, "PYM", fleet="", created_at=FEB)
    elsewhere = await _entity(other_tenant, "Stark", entity_type="person", created_at=FEB)

    await _merge()

    assert set(await _entities(tenant)) == {person, company, fleet_a, fleet_b, no_fleet}
    assert set(await _entities(other_tenant)) == {elsewhere}


async def test_a_second_run_changes_nothing(without_entities_natural_key_index) -> None:
    tenant = _tenant()
    survivor = await _entity(tenant, "Tyrell", created_at=JAN, attributes={"_aliases": ["Tyrell Corp"]})
    duplicate = await _entity(tenant, "tyrell", created_at=FEB)
    memory = await _memory(tenant, subject=duplicate)
    await _link(memory, duplicate, "subject", "caller")
    other = await _entity(tenant, "Weyland", created_at=JAN)
    await _relation(tenant, duplicate, "rivals", other)

    await _merge()
    first = (await _entities(tenant), await _links(memory), await _relations(tenant))
    await _merge()
    second = (await _entities(tenant), await _links(memory), await _relations(tenant))

    assert first == second
    assert survivor in first[0]


async def test_the_index_builds_once_the_merge_has_run(without_entities_natural_key_index) -> None:
    tenant = _tenant()
    for offset in range(3):
        await _entity(tenant, "Cyberdyne", created_at=JAN + timedelta(days=offset))

    async with get_session() as session:
        with pytest.raises(Exception, match=r"could not create unique index|is duplicated"):
            await session.execute(text(UQ_ENTITIES_NATURAL_KEY_SQL))

    await _merge()

    # Not CONCURRENTLY: a session is a transaction. The migration's own build is.
    async with get_session() as session:
        await session.execute(text(UQ_ENTITIES_NATURAL_KEY_SQL))
    assert len(await _entities(tenant)) == 1


async def test_the_migration_sees_whether_a_valid_index_is_there(without_entities_natural_key_index) -> None:
    """068 skips its merge where the index stands, as on every database the
    chain built: there can be no duplicate to merge."""
    query = load_migration_068().VALID_INDEX_SQL

    assert await _rows(query, name=UQ_ENTITIES_NATURAL_KEY) == []

    async with get_session() as session:
        await session.execute(text(UQ_ENTITIES_NATURAL_KEY_SQL))
    assert len(await _rows(query, name=UQ_ENTITIES_NATURAL_KEY)) == 1

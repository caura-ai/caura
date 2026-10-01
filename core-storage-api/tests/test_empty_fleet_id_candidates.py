"""``fleet_id = ''`` is one fleet scope with NULL for every candidate lookup.

The exact-hash gate already groups the way ``uq_memories_live_content_hash``
does, on ``COALESCE(fleet_id, '')`` (``test_write_path_races.py`` pins that).
The semantic-duplicate gate and the two contradiction-candidate lookups chose
their predicate on falsiness instead: ``''`` is falsy, so a caller passing it
filtered ``IS NULL`` and a row STORED as ``''`` matched no caller at all. It was
never refused as a paraphrase and never offered as a contradiction candidate,
while an identical write of it was refused by the exact gate.

Nothing on the write path normalises ``''`` to NULL, so such rows exist
whenever a REST caller sends ``"fleet_id": ""``.

Against real Postgres, because what is under test is which rows a predicate
reaches.
"""

from __future__ import annotations

import uuid

import pytest

from core_storage_api.services.postgres_service import PostgresService

pytestmark = pytest.mark.asyncio

# 1024 is the column's dimensionality; identical vectors are similarity 1.0, so
# the threshold never decides these tests — the fleet predicate does.
_VEC = [0.1] * 1024


async def _memory(svc: PostgresService, tenant: str, fleet_id: str | None, **extra):
    return await svc.memory_add(
        {
            "tenant_id": tenant,
            "fleet_id": fleet_id,
            "agent_id": "fleet-scope-tester",
            "content": f"fleet scope canary {uuid.uuid4()}",
            "memory_type": "fact",
            "weight": 0.5,
            "status": "active",
            "visibility": "scope_team",
            "embedding": _VEC,
            **extra,
        }
    )


@pytest.mark.parametrize("asked_with", ["", None])
@pytest.mark.parametrize("stored_with", ["", None])
async def test_semantic_duplicate_groups_empty_and_null_fleet(
    _ensure_schema, stored_with: str | None, asked_with: str | None
) -> None:
    svc = PostgresService()
    tenant = f"t-fsc-{uuid.uuid4().hex[:8]}"
    stored = await _memory(svc, tenant, stored_with)

    hit = await svc.memory_find_semantic_duplicate(
        tenant_id=tenant, fleet_id=asked_with, embedding=_VEC, agent_id="fleet-scope-tester"
    )

    assert hit is not None, (
        f"a row stored with fleet_id={stored_with!r} was invisible to fleet_id={asked_with!r}"
    )
    assert hit[0].id == stored.id


@pytest.mark.parametrize("asked_with", ["", None])
async def test_similar_candidates_groups_empty_and_null_fleet(_ensure_schema, asked_with: str | None) -> None:
    svc = PostgresService()
    tenant = f"t-fsc-{uuid.uuid4().hex[:8]}"
    stored = await _memory(svc, tenant, "")
    writer = await _memory(svc, tenant, asked_with)

    found = await svc.memory_find_similar_candidates(
        tenant_id=tenant, fleet_id=asked_with, embedding=_VEC, memory_id=writer.id
    )

    assert [m.id for m in found] == [stored.id]


@pytest.mark.parametrize("asked_with", ["", None])
async def test_rdf_conflicts_groups_empty_and_null_fleet(_ensure_schema, asked_with: str | None) -> None:
    svc = PostgresService()
    tenant = f"t-fsc-{uuid.uuid4().hex[:8]}"
    subject = await svc.entity_add(
        {"tenant_id": tenant, "entity_type": "service", "canonical_name": "billing"}
    )
    stored = await _memory(
        svc, tenant, "", subject_entity_id=subject.id, predicate="status", object_value="up"
    )
    writer = await _memory(
        svc, tenant, asked_with, subject_entity_id=subject.id, predicate="status", object_value="down"
    )

    found = await svc.memory_find_rdf_conflicts(
        tenant_id=tenant,
        subject_entity_id=subject.id,
        predicate="status",
        object_value="down",
        memory_id=writer.id,
        fleet_id=asked_with,
    )

    assert [m.id for m in found] == [stored.id]


async def test_a_named_fleet_still_excludes_the_fleetless(_ensure_schema) -> None:
    """The grouping must not widen a named fleet's scope to the fleetless rows."""
    svc = PostgresService()
    tenant = f"t-fsc-{uuid.uuid4().hex[:8]}"
    await _memory(svc, tenant, "")
    await _memory(svc, tenant, None)

    hit = await svc.memory_find_semantic_duplicate(tenant_id=tenant, fleet_id="fleet-a", embedding=_VEC)

    assert hit is None

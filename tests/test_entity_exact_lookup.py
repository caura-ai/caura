"""Entity exact lookups agree with the natural-key index (M-40, M-25, L-47).

``uq_entities_tenant_type_name_fleet`` keys an entity on tenant, type,
``lower(canonical_name)`` and ``COALESCE(fleet_id, '')``. The exact lookups did
not:

- M-40: ``entity_find_exact`` compared ``canonical_name`` case-sensitively. A
  REST upsert of "Acme Corp" over an existing "acme corp" missed it, took the
  create path, collided with the index, and got the existing row back
  unchanged: its attributes and the new alias were dropped.
- M-25: EmitMemoryTriple's proper-noun subject lookup sent no ``entity_type``,
  so storage filtered on type ``default``, which extraction never writes, and
  extraction lowercases names: the lookup could never match. It now matches any
  type, case-insensitively; a name held by more than one type is ambiguous and
  skipped.
- M-119: that lookup then fell back to tenant-shared entities, while extraction
  resolves in the write's own fleet. A fleet's first mention of a name a
  fleet-less entity holds took that entity as subject, extraction created the
  fleet's own, and later mentions took that one, so the contradiction check
  never compared the two rows. The lookup stays in the write's fleet now (owner
  decision 2026-10-06), and a fleet-less write still finds tenant-shared ones.
- L-47: the lookups split ``fleet_id`` into ``== value`` and ``IS NULL``, so
  ``''`` and NULL, one key in the index, missed each other.

Real storage throughout, through the in-process bridge, because each claim is
about which row a query reaches.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest

from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepOutcome
from core_api.pipeline.steps.write.emit_memory_triple import EmitMemoryTriple
from core_api.schemas import EntityUpsert, MemoryCreate
from core_api.services.entity_service import find_entity_by_exact_name, upsert_entity

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _tenant() -> str:
    return f"test-tenant-exact-{uuid4().hex[:8]}"


async def _entity(sc, tenant_id, name, entity_type, *, fleet_id=None, attrs=None):
    return await sc.create_entity(
        {
            "tenant_id": tenant_id,
            "fleet_id": fleet_id,
            "entity_type": entity_type,
            "canonical_name": name,
            "attributes": attrs or {},
        }
    )


# ── M-40: a case-variant upsert merges ────────────────────────────────────


async def test_a_case_variant_upsert_merges_into_the_existing_entity(sc):
    tenant = _tenant()
    existing = await _entity(sc, tenant, "acme corp", "organization", attrs={"a": 1})

    out = await upsert_entity(
        EntityUpsert(
            tenant_id=tenant,
            entity_type="organization",
            canonical_name="Acme Corp",
            attributes={"hq": "Berlin"},
        )
    )

    assert str(out.id) == str(existing["id"])
    stored = await sc.get_entity(str(existing["id"]), tenant)
    assert stored["attributes"]["hq"] == "Berlin"
    assert stored["attributes"]["a"] == 1
    assert "Acme Corp" in stored["attributes"]["_aliases"]


# ── L-47: '' and NULL are one fleet key ───────────────────────────────────


async def test_an_exact_lookup_treats_an_empty_fleet_as_tenant_shared(sc):
    tenant = _tenant()
    existing = await _entity(sc, tenant, "postgres", "technology")
    found = await sc.find_exact_entity(
        tenant, "postgres", fleet_id="", entity_type="technology"
    )
    assert found is not None and found["id"] == existing["id"]


async def test_bulk_resolve_treats_an_empty_fleet_as_tenant_shared(sc):
    tenant = _tenant()
    existing = await _entity(sc, tenant, "postgres", "technology")
    item = {
        "input_idx": 0,
        "fleet_id": "",
        "canonical_name": "postgres",
        "entity_type": "technology",
        "name_embedding": None,
    }
    [resolved] = await sc.bulk_resolve_entities(tenant, [item], threshold=0.9)
    assert resolved is not None and resolved["entity_id"] == str(existing["id"])


# ── M-25: the untyped lookup ──────────────────────────────────────────────


async def test_an_untyped_lookup_matches_any_type_case_insensitively(sc):
    tenant = _tenant()
    existing = await _entity(sc, tenant, "atlas", "project")
    found = await sc.find_exact_entity(tenant, "Atlas")
    assert found is not None and found["id"] == existing["id"]


async def test_an_untyped_lookup_of_a_name_two_types_share_is_ambiguous(sc):
    tenant = _tenant()
    await _entity(sc, tenant, "atlas", "project")
    await _entity(sc, tenant, "atlas", "person")
    with pytest.raises(httpx.HTTPStatusError) as raised:
        await sc.find_exact_entity(tenant, "Atlas")
    assert raised.value.response.status_code == 409


async def test_a_typed_lookup_is_never_ambiguous(sc):
    """The type is part of the key, so naming it picks one row, whatever the case."""
    tenant = _tenant()
    await _entity(sc, tenant, "atlas", "project")
    person = await _entity(sc, tenant, "atlas", "person")
    found = await sc.find_exact_entity(tenant, "Atlas", entity_type="person")
    assert found is not None and found["id"] == person["id"]


async def test_a_fleet_writes_proper_noun_ignores_a_tenant_shared_entity(sc):
    """M-119: extraction would create the fleet's own 'atlas'; the subject
    must not be a different row from the one later mentions resolve to."""
    tenant = _tenant()
    await _entity(sc, tenant, "atlas", "project")
    found = await find_entity_by_exact_name(
        tenant_id=tenant, canonical_name="Atlas", fleet_id="fleet-a"
    )
    assert found is None


async def test_a_fleet_less_writes_proper_noun_resolves_a_tenant_shared_entity(sc):
    tenant = _tenant()
    shared = await _entity(sc, tenant, "atlas", "project")
    found = await find_entity_by_exact_name(
        tenant_id=tenant, canonical_name="Atlas", fleet_id=None
    )
    assert found == UUID(str(shared["id"]))


async def test_a_proper_noun_prefers_the_writes_own_fleet(sc):
    tenant = _tenant()
    await _entity(sc, tenant, "atlas", "project")
    own = await _entity(sc, tenant, "atlas", "project", fleet_id="fleet-a")
    found = await find_entity_by_exact_name(
        tenant_id=tenant, canonical_name="Atlas", fleet_id="fleet-a"
    )
    assert found == UUID(str(own["id"]))


def _triple_ctx(tenant: str) -> tuple[MemoryCreate, PipelineContext]:
    data = MemoryCreate(
        tenant_id=tenant,
        fleet_id="fleet-a",
        agent_id="test-agent",
        content="Atlas has release date 2027-05-01",
    )
    ctx = PipelineContext(
        data={"input": data, "memory_fields": {"metadata": {}}},
        tenant_config=SimpleNamespace(triple_emission_enabled=True),
    )
    return data, ctx


async def test_a_known_proper_noun_fills_the_subject_end_to_end(sc):
    tenant = _tenant()
    existing = await _entity(sc, tenant, "atlas", "project", fleet_id="fleet-a")
    data, ctx = _triple_ctx(tenant)
    result = await EmitMemoryTriple().execute(ctx)
    assert result is None, result
    assert data.subject_entity_id == UUID(str(existing["id"]))


async def test_a_proper_noun_two_types_share_is_skipped_as_ambiguous(sc):
    tenant = _tenant()
    await _entity(sc, tenant, "atlas", "project", fleet_id="fleet-a")
    await _entity(sc, tenant, "atlas", "person", fleet_id="fleet-a")
    data, ctx = _triple_ctx(tenant)
    result = await EmitMemoryTriple().execute(ctx)
    assert result.outcome == StepOutcome.SKIPPED
    assert result.detail["reason"] == "ambiguous_subject"
    assert data.subject_entity_id is None


async def test_a_fleet_write_leaves_a_name_only_a_shared_entity_holds_to_extraction(sc):
    """M-119 end to end: no subject at write time, so extraction's write-back
    sets the fleet's own entity, the one every later mention resolves to."""
    tenant = _tenant()
    await _entity(sc, tenant, "atlas", "project")
    data, ctx = _triple_ctx(tenant)
    result = await EmitMemoryTriple().execute(ctx)
    assert data.subject_entity_id is None
    assert result is not None and result.outcome == StepOutcome.SKIPPED, result
    assert result.detail["reason"] == "no_subject_match"

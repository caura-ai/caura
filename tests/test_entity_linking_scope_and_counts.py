"""Entity-linking pipeline storage: fleet scope, merge counts, backfill paging.

L-149. The linking methods used a strict ``fleet_id = :fleet_id`` whenever a
fleet was passed, so fleet-scoped runs (a fleet-scoped lifecycle trigger, and
every write's per-memory cross-link discovery) never saw tenant-shared
NULL-fleet entities, which wire contract D4 makes readable by every fleet.
Decided 2026-10-04: the read-side methods include them (the null-embedding
backfill, and the entity side of cross-link discovery); duplicate resolution
and relation inference stay strict, so a fleet's job never merges or relates
tenant-shared entities.

L-51. ``entity_resolve_duplicates`` merges each duplicate in its own SAVEPOINT
and counted a cluster only when every merge in it succeeded. A cluster that
failed part-way kept its earlier merges but counted none, and a run whose every
cluster did that reported "all clusters failed to merge", which core-api turns
into a FAILED step, for a run that changed the graph.

L-174. The backfill scan had no ORDER BY or cursor, and the step embedded one
name per awaited provider call. Names that fail every night came back first
every night and could stall the rows behind them. The scan is now ordered and
resumable, and the step pages past failures with one provider call per page.

M-122. The nightly entity-link fan-out passes no fleet, and duplicate resolution
added its fleet clause only when one was passed, so the nightly pass merged
same-named entities of different fleets, and a fleet's entity into a fleet-less
one. The write path resolves within one fleet (or among fleet-less entities), so
the pair-find now keeps both sides of a pair in the same fleet.

Real storage through the in-process bridge, except the step test.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from common.constants import VECTOR_DIM
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.steps.entity_linking import backfill_entity_embeddings
from core_storage_api.services.postgres_service import PostgresService

FLEET = "fleet-a"


def _tenant() -> str:
    return f"test-linking-{uuid4().hex[:8]}"


def _unit(i: int) -> list[float]:
    vec = [0.0] * VECTOR_DIM
    vec[i] = 1.0
    return vec


async def _entity(sc, tenant, name, *, fleet_id=None, embedding=None) -> str:
    row = await sc.create_entity(
        {
            "tenant_id": tenant,
            "fleet_id": fleet_id,
            "entity_type": "organization",
            "canonical_name": name,
            "attributes": {},
            "name_embedding": embedding,
        }
    )
    return str(row["id"])


# ── L-149: read-side fleet runs see tenant-shared entities ────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_fleet_backfill_lists_tenant_shared_entities(sc):
    tenant = _tenant()
    shared = await _entity(sc, tenant, "globex")
    own = await _entity(sc, tenant, "initech", fleet_id=FLEET)

    rows = await sc.list_null_embedding_entities(
        tenant_id=tenant, fleet_id=FLEET, batch_size=10
    )

    assert {r["id"] for r in rows} == {shared, own}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_fleet_memory_cross_links_to_a_tenant_shared_entity(sc):
    tenant = _tenant()
    shared = await _entity(sc, tenant, "globex", embedding=_unit(0))
    memory = await sc.create_memory(
        {
            "tenant_id": tenant,
            "fleet_id": FLEET,
            "agent_id": "test-agent",
            "memory_type": "fact",
            "content": "Globex shipped the new release.",
            "embedding": _unit(0),
            "status": "active",
            "visibility": "scope_team",
        }
    )
    memory_id = str(memory["id"])

    out = await sc.discover_cross_links(
        tenant_id=tenant,
        fleet_id=FLEET,
        batch_size=10,
        threshold=0.9,
        text_verify=True,
        target_memory_ids=[memory_id],
    )

    assert out["links_created"] == 1
    links = await sc.get_entity_links_for_memories([memory_id], tenant)
    assert [link["entity_id"] for link in links[memory_id]] == [shared]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_fleet_duplicate_run_still_leaves_shared_entities_alone(sc):
    """The control for the decision: resolution stays strict."""
    tenant = _tenant()
    await _entity(sc, tenant, "globex", fleet_id=FLEET, embedding=_unit(0))
    await _entity(sc, tenant, "globex corp", embedding=_unit(0))

    out = await sc.resolve_entities(
        tenant_id=tenant,
        fleet_id=FLEET,
        batch_size=10,
        threshold=0.9,
        candidate_limit=10,
    )

    assert out["merge_count"] == 0
    assert len(await sc.list_entities(tenant)) == 2


# ── L-51: a cluster that fails part-way still counts what it merged ───────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_cluster_that_fails_part_way_reports_the_merges_it_kept(sc):
    tenant = _tenant()
    for name in ("globex", "globex corp", "globex company"):
        await _entity(sc, tenant, name, embedding=_unit(0))
    merge_one = PostgresService._entity_merge_dupe_into_canonical
    calls: list[int] = []

    async def fail_the_second(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("merge failed")
        return await merge_one(self, *args, **kwargs)

    with patch.object(
        PostgresService, "_entity_merge_dupe_into_canonical", new=fail_the_second
    ):
        out = await sc.resolve_entities(
            tenant_id=tenant,
            fleet_id=None,
            batch_size=10,
            threshold=0.9,
            candidate_limit=10,
        )

    assert "error" not in out, out
    assert out["merge_count"] == len(out["merged_entity_ids"]) == 1
    assert (out["clusters"], out["cluster_errors"]) == (1, 1)
    assert len(await sc.list_entities(tenant)) == 2


# ── M-122: a run with no fleet merges within each fleet ───────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_run_with_no_fleet_merges_within_a_fleet_and_never_across(sc):
    tenant = _tenant()
    # Same name, three scopes: two fleets and the fleet-less one.
    await _entity(sc, tenant, "globex", fleet_id=FLEET, embedding=_unit(0))
    await _entity(sc, tenant, "globex corp", fleet_id="fleet-b", embedding=_unit(0))
    await _entity(sc, tenant, "globex company", embedding=_unit(0))
    # Controls: a pair inside one fleet, and a fleet-less pair, still merge.
    await _entity(sc, tenant, "initech", fleet_id=FLEET, embedding=_unit(1))
    await _entity(sc, tenant, "initech corp", fleet_id=FLEET, embedding=_unit(1))
    await _entity(sc, tenant, "umbrella", embedding=_unit(2))
    await _entity(sc, tenant, "umbrella corp", embedding=_unit(2))

    out = await sc.resolve_entities(
        tenant_id=tenant,
        fleet_id=None,
        batch_size=10,
        threshold=0.9,
        candidate_limit=10,
    )

    assert out["merge_count"] == 2, out
    names = {e["canonical_name"] for e in await sc.list_entities(tenant)}
    # Each merged pair keeps its first seen name (H-05).
    assert names == {
        "globex",
        "globex corp",
        "globex company",
        "initech",
        "umbrella",
    }


# ── L-174: an ordered, resumable scan and a step that pages past failures ─


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_null_embedding_scan_is_ordered_and_resumable(sc):
    tenant = _tenant()
    ids = sorted([await _entity(sc, tenant, f"vendor {i}") for i in range(3)])

    first = await sc.list_null_embedding_entities(
        tenant_id=tenant, fleet_id=None, batch_size=2
    )
    rest = await sc.list_null_embedding_entities(
        tenant_id=tenant, fleet_id=None, batch_size=2, after_id=first[-1]["id"]
    )

    assert [r["id"] for r in first] == ids[:2]
    assert [r["id"] for r in rest] == ids[2:]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_backfill_pages_past_names_that_fail_with_one_call_per_page():
    rows = [
        {"id": "00000000-0000-4000-8000-000000000001", "canonical_name": "poison a"},
        {"id": "00000000-0000-4000-8000-000000000002", "canonical_name": "poison b"},
        {"id": "00000000-0000-4000-8000-000000000003", "canonical_name": "globex"},
    ]

    async def list_null(*, tenant_id, fleet_id, batch_size, after_id=None):
        ids = [r["id"] for r in rows]
        start = 0 if after_id is None else ids.index(after_id) + 1
        return rows[start : start + batch_size]

    def _vector(name: str) -> list[float]:
        if name.startswith("poison"):
            raise ValueError(f"provider rejects {name!r}")
        return _unit(0)

    async def embed_one(name, *args, **kwargs):
        return _vector(name)

    async def embed_batch(names, *args, **kwargs):
        return [_vector(name) for name in names]

    storage = MagicMock(
        list_null_embedding_entities=AsyncMock(side_effect=list_null),
        set_entity_embeddings=AsyncMock(
            side_effect=lambda *, tenant_id, updates: len(updates)
        ),
    )
    batch = AsyncMock(side_effect=embed_batch)
    ctx = PipelineContext(
        data={"tenant_id": "t1", "entity_embedding_backfill_batch_size": 2},
        tenant_config=SimpleNamespace(),
    )
    module = backfill_entity_embeddings

    with (
        patch.object(module, "get_storage_client", return_value=storage),
        patch.object(module, "get_embedding", new=AsyncMock(side_effect=embed_one)),
        patch.object(module, "get_embeddings_batch", new=batch, create=True),
    ):
        result = await module.BackfillEntityEmbeddings().execute(ctx)

    assert result.detail["backfill_count"] == 1
    [call] = storage.set_entity_embeddings.await_args_list
    assert [u["id"] for u in call.kwargs["updates"]] == [rows[2]["id"]]
    assert batch.await_count == 2, "one provider call per page"

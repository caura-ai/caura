"""M-61, entities index: a filtered nearest-entity lookup must reach the caller's
own entities when other tenants' entities are nearer.

``ix_entities_name_embedding_hnsw`` is one index across every tenant, like the
memories index #1951 fixed. Without ``hnsw.iterative_scan`` an HNSW scan hands
back one ``ef_search`` batch and the tenant filter runs on that batch alone, so
entity resolution minted a duplicate of an entity the tenant already had, the
nightly duplicate pass left the pair unmerged, and cross-link discovery linked
nothing.

Same setup as the memories test: 60 entities of another tenant nearer the probe
than the caller's own, and each lookup planned through the HNSW index (seq scan
and sort off, a 10-row ``ef_search`` batch) in its transaction.
"""

import contextlib

import pytest
from sqlalchemy import text

import core_storage_api.services.postgres_service as ps
from core_api.clients.storage_client import get_storage_client
from tests import test_m61_filtered_ann_lookups_reach_sparse_tenants as m61
from tests.conftest import new_tenant_id

_TYPE = "concept"


async def _seed_entities(
    tenant_id: str, embeddings: list[list[float]], names: list[str] | None = None
) -> list[str]:
    names = names or [f"m61 entity {i}" for i in range(len(embeddings))]
    created = await get_storage_client().bulk_upsert_entities(
        [
            {
                "input_idx": i,
                "action": "create",
                "tenant_id": tenant_id,
                "fleet_id": None,
                "entity_type": _TYPE,
                "canonical_name": name,
                "attributes": {},
                "name_embedding": embedding,
            }
            for i, (name, embedding) in enumerate(zip(names, embeddings, strict=True))
        ]
    )
    ids = [row["entity_id"] for row in sorted(created, key=lambda r: r["input_idx"])]
    assert all(ids), "the bulk upsert did not return an id for every entity"
    return ids


def _plan_through_hnsw(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both session factories, planned as a large table plans them."""
    gucs = [
        text("SET LOCAL enable_seqscan = off"),
        text("SET LOCAL enable_sort = off"),
        text(f"SET LOCAL hnsw.ef_search = {m61._EF_SEARCH}"),
    ]
    for name in ("get_session", "get_read_session"):

        @contextlib.asynccontextmanager
        async def through_hnsw(real=getattr(ps, name)):
            async with real() as session:
                for guc in gucs:
                    await session.execute(guc)
                yield session

        monkeypatch.setattr(ps, name, through_hnsw)


@pytest.fixture(autouse=True)
def _real_probe(monkeypatch: pytest.MonkeyPatch):
    """Each test starts unprobed, so the real DB answers (CI: pgvector 0.8+)."""
    monkeypatch.setattr(ps, "_pgvector_version", None)


async def test_resolution_finds_the_entity_behind_other_tenants(
    tenant_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    space = m61._Space(tenant_id)
    await _seed_entities(new_tenant_id(), space.others())
    [own] = await _seed_entities(tenant_id, space.own(1))
    _plan_through_hnsw(monkeypatch)

    found = await ps.PostgresService().entity_find_by_embedding_similarity(
        tenant_id=tenant_id, entity_type=_TYPE, name_embedding=space.probe
    )

    assert [str(entity.id) for entity, _ in found] == [own]


async def test_bulk_resolution_matches_the_entity_behind_other_tenants(
    tenant_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    space = m61._Space(tenant_id)
    await _seed_entities(new_tenant_id(), space.others())
    [own] = await _seed_entities(tenant_id, space.own(1))
    _plan_through_hnsw(monkeypatch)

    [match] = await ps.PostgresService().entity_bulk_resolve(
        tenant_id=tenant_id,
        items=[
            {
                "input_idx": 0,
                "fleet_id": None,
                "entity_type": _TYPE,
                "canonical_name": "a name no entity has",
                "name_embedding": space.probe,
            }
        ],
        threshold=0.9,
    )

    assert match is not None, "resolution would mint a duplicate of the entity"
    assert match["entity_id"] == own
    assert match["matched_by"] == "similarity"


async def test_the_duplicate_pass_merges_a_pair_behind_other_tenants(
    tenant_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    space = m61._Space(tenant_id)
    await _seed_entities(new_tenant_id(), space.others())
    await _seed_entities(
        tenant_id, space.own(2), names=["northwind traders", "northwind trading"]
    )
    _plan_through_hnsw(monkeypatch)

    result = await ps.PostgresService().entity_resolve_duplicates(
        tenant_id=tenant_id,
        fleet_id=None,
        batch_size=10,
        threshold=0.95,
        candidate_limit=5,
    )

    assert result.get("merge_count") == 1, result


async def test_cross_link_discovery_links_the_entity_behind_other_tenants(
    tenant_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    space = m61._Space(tenant_id)
    await _seed_entities(new_tenant_id(), space.others())
    await _seed_entities(tenant_id, space.own(1))
    [memory] = await m61._seed(tenant_id, [space.probe])
    _plan_through_hnsw(monkeypatch)

    result = await ps.PostgresService().entity_discover_cross_links(
        tenant_id=tenant_id,
        fleet_id=None,
        batch_size=10,
        threshold=0.9,
        text_verify=False,
        target_memory_ids=[memory],
    )

    assert result.get("links_created") == 1, result

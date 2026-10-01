"""Background lifecycle passes keep private memories private and keep
identifier-distinct entities apart.

* Crystallizer: the near-duplicate pair query only pairs team/org-visible rows,
  never across fleets. A private (``scope_agent``) row would otherwise be merged
  into a team-visible crystal and archived.
* Insights: every scope applies the read visibility rule, so another agent's
  private rows never reach the corpus that is sent to the LLM and persisted.
* Entity resolution: the nightly merge applies the same identifier guard as the
  extraction worker, including clusters that would chain through a plain name.

Rows are seeded with committed INSERTs on an independent session, like the
other storage tests (the storage service commits on its own connection).
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from common.embedding.providers.fake import fake_embedding
from common.models import Entity, Memory
from core_api.constants import INSIGHTS_MAX_MEMORIES
from core_storage_api.services.postgres_service import PostgresService, get_session

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_svc = PostgresService()


def _t(tag: str) -> str:
    return f"test-tenant-lifevis-{tag}-{uuid4().hex[:8]}"


async def _seed_memory(
    *,
    tenant_id: str,
    agent_id: str,
    visibility: str,
    content: str = "the launch moved to thursday",
    fleet_id: str | None = None,
    embedding: list[float] | None = None,
) -> UUID:
    mem_id = uuid4()
    async with get_session() as session:
        session.add(
            Memory(
                id=mem_id,
                tenant_id=tenant_id,
                fleet_id=fleet_id,
                agent_id=agent_id,
                memory_type="fact",
                content=content,
                status="active",
                visibility=visibility,
                embedding=embedding
                if embedding is not None
                else fake_embedding("same-note"),
            )
        )
    return mem_id


async def _pairs(tenant_id: str, fleet_id: str | None = None) -> set[frozenset]:
    rows = await _svc.memory_find_near_duplicate_pairs(
        tenant_id, fleet_id, batch_size=100, threshold=0.95
    )
    return {frozenset((r[0], r[1])) for r in rows if r[1] is not None}


# ── crystallizer pair query ─────────────────────────────────────────


async def test_private_rows_never_pair_for_crystallization():
    tenant = _t("cryst-private")
    ids = [
        await _seed_memory(
            tenant_id=tenant, agent_id="agent-a", visibility="scope_agent"
        )
        for _ in range(3)
    ]
    pairs = await _pairs(tenant)
    assert not any(set(p) & set(ids) for p in pairs), pairs


async def test_shared_rows_still_pair_within_one_fleet():
    tenant = _t("cryst-shared")
    ids = [
        await _seed_memory(
            tenant_id=tenant,
            agent_id=f"agent-{i}",
            visibility="scope_team",
            fleet_id="f1",
        )
        for i in range(3)
    ]
    pairs = await _pairs(tenant)
    assert any(set(p) <= set(ids) for p in pairs), (
        "team-visible duplicates must still cluster"
    )


async def test_pairs_never_span_fleets_or_mix_null_fleet():
    tenant = _t("cryst-fleets")
    f1 = await _seed_memory(
        tenant_id=tenant, agent_id="a", visibility="scope_team", fleet_id="f1"
    )
    f2 = await _seed_memory(
        tenant_id=tenant, agent_id="b", visibility="scope_team", fleet_id="f2"
    )
    nofleet = await _seed_memory(
        tenant_id=tenant, agent_id="c", visibility="scope_org", fleet_id=None
    )
    pairs = await _pairs(tenant)
    for a, b in [(f1, f2), (f1, nofleet), (f2, nofleet)]:
        assert frozenset((a, b)) not in pairs


# ── insights corpus ─────────────────────────────────────────────────


@pytest.mark.parametrize("scope", ["all", "fleet"])
async def test_insights_corpus_excludes_other_agents_private_rows(sc, scope):
    tenant = _t(f"insights-{scope}")
    private_b = await _seed_memory(
        tenant_id=tenant,
        agent_id="agent-b",
        visibility="scope_agent",
        content="b private",
        fleet_id="f1",
    )
    own_private = await _seed_memory(
        tenant_id=tenant,
        agent_id="agent-a",
        visibility="scope_agent",
        content="a private",
        fleet_id="f1",
    )
    team = await _seed_memory(
        tenant_id=tenant,
        agent_id="agent-b",
        visibility="scope_team",
        content="team",
        fleet_id="f1",
    )

    rows = await sc.insights_query_patterns(
        tenant_id=tenant,
        fleet_id="f1",
        agent_id="agent-a",
        scope=scope,
        max_memories=INSIGHTS_MAX_MEMORIES,
    )
    ids = {str(r["id"]) for r in rows}
    assert str(private_b) not in ids
    assert str(team) in ids
    assert str(own_private) in ids  # the caller's own private rows stay visible to it


# ── nightly entity resolution ───────────────────────────────────────


async def _seed_entity(tenant_id: str, name: str) -> str:
    ent_id = uuid4()
    async with get_session() as session:
        session.add(
            Entity(
                id=ent_id,
                tenant_id=tenant_id,
                entity_type="organization",
                canonical_name=name,
                # Identical vectors: similarity 1.0, the worst case for the guard.
                name_embedding=fake_embedding("look-alike"),
            )
        )
    return str(ent_id)


async def _surviving(tenant_id: str) -> set[str]:
    from sqlalchemy import select

    async with get_session() as session:
        names = (
            await session.execute(
                select(Entity.canonical_name).where(Entity.tenant_id == tenant_id)
            )
        ).scalars()
        return set(names)


@pytest.mark.parametrize(
    "names",
    [
        ["CAURA-712", "CAURA-713"],
        ["caura v1.0.2", "caura v1.0.3"],
        ["comet #0002", "comet #0012"],
        ["acme (ohio)", "acme (delaware)"],
    ],
)
async def test_identifier_distinct_entities_are_not_merged(sc, names):
    tenant = _t("ent-distinct")
    for n in names:
        await _seed_entity(tenant, n)
    resp = await sc.resolve_entities(
        tenant_id=tenant,
        fleet_id=None,
        batch_size=100,
        threshold=0.85,
        candidate_limit=5,
    )
    assert resp.get("merge_count", 0) == 0, resp
    assert await _surviving(tenant) == set(names)


async def test_plain_name_does_not_bridge_two_qualified_entities(sc):
    tenant = _t("ent-bridge")
    for n in ["acme (ohio)", "acme (delaware)", "acme"]:
        await _seed_entity(tenant, n)
    await sc.resolve_entities(
        tenant_id=tenant,
        fleet_id=None,
        batch_size=100,
        threshold=0.85,
        candidate_limit=5,
    )
    left = await _surviving(tenant)
    assert {"acme (ohio)", "acme (delaware)"} <= left, left
    assert len(left) == 2  # 'acme' merged into exactly one of them


async def test_true_duplicates_still_merge(sc):
    tenant = _t("ent-dupe")
    for n in ["Acme Corporation", "Acme"]:
        await _seed_entity(tenant, n)
    resp = await sc.resolve_entities(
        tenant_id=tenant,
        fleet_id=None,
        batch_size=100,
        threshold=0.85,
        candidate_limit=5,
    )
    assert resp["merge_count"] == 1
    assert await _surviving(tenant) == {"Acme Corporation"}


async def test_qualified_entities_stay_apart_across_repeated_nightly_runs(sc):
    """A bare canonical would lose the qualifier and let the next run merge."""
    tenant = _t("ent-repeat")
    for n in ["Acme Corporation", "acme (delaware)", "acme (ohio)", "acme"]:
        await _seed_entity(tenant, n)
    for _ in range(3):
        await sc.resolve_entities(
            tenant_id=tenant,
            fleet_id=None,
            batch_size=100,
            threshold=0.85,
            candidate_limit=5,
        )
    left = await _surviving(tenant)
    assert {"acme (delaware)", "acme (ohio)"} <= left, left

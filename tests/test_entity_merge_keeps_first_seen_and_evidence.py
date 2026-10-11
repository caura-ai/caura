"""The nightly entity merge keeps what the duplicates carried.

``entity_resolve_duplicates`` folds each cluster of near-identical names into one
canonical entity. It got three things wrong:

- H-05: between two compatible unqualified names it kept the longer one, where
  the write path keeps the first it saw. A qualified or identifier-bearing name
  still wins first. First seen is ``entities.created_at`` (migration 060), owner
  decision 2026-10-06.
- L-233: a duplicate's relation that matched one of the canonical's was deleted,
  and its ``relation_evidence`` rows with it by FK cascade, so the canonical edge
  lost the memories that asserted it through the duplicate.
- L-234: the cluster was read without a lock and the canonical's attributes
  written back from that snapshot, so a key another writer merged in meanwhile
  was overwritten. The duplicate's own keys, other than its aliases, were
  dropped outright.

Real storage through the in-process bridge, seeded with the helpers of
``test_ph6_entity_linking_storage``. Where the canonical is not what a test is
about, the longer name is seeded first, so the longest-name rule and first seen
pick the same row.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import asyncpg
import pytest
from sqlalchemy import text

from common.embedding.providers.fake import fake_embedding
from core_storage_api.services.postgres_service import get_session
from tests.conftest import TEST_DB_URL
from tests.test_ph6_entity_linking_storage import (
    _entity_attrs,
    _entity_exists,
    _seed_entity,
    _seed_memory,
    _seed_relation,
    _t,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_ACME = fake_embedding("acme")


async def _resolve(sc, tenant: str) -> dict:
    return await sc.resolve_entities(
        tenant_id=tenant,
        fleet_id=None,
        batch_size=100,
        threshold=0.85,
        candidate_limit=3,
    )


async def _acme(tenant: str, name: str, attributes: dict | None = None) -> str:
    return await _seed_entity(
        tenant_id=tenant,
        canonical_name=name,
        name_embedding=_ACME,
        attributes=attributes,
    )


async def test_the_first_seen_unqualified_name_stays_canonical(sc):
    """H-05: 'Acme' was seen first, so the later, longer 'Acme Corporation'
    folds into it, as the write path would have done."""
    tenant = _t()
    first = await _acme(tenant, "Acme")
    later = await _acme(tenant, "Acme Corporation")

    resp = await _resolve(sc, tenant)

    assert resp["merged_entity_ids"] == [later]
    assert await _entity_exists(first)
    assert "Acme Corporation" in (await _entity_attrs(first))["_aliases"]


async def test_a_qualified_name_still_wins_over_an_earlier_bare_one(sc):
    """Control: 'acme (delaware)' says which Acme it is. Keeping it is what stops
    the next run merging 'acme (ohio)' into the same row."""
    tenant = _t()
    bare = await _acme(tenant, "acme")
    qualified = await _acme(tenant, "acme (delaware)")

    resp = await _resolve(sc, tenant)

    assert resp["merged_entity_ids"] == [bare]
    assert await _entity_exists(qualified)


async def _record_evidence(relation_id: str, memory_id: str) -> None:
    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO relation_evidence (relation_id, memory_id) "
                "VALUES (CAST(:r AS uuid), CAST(:m AS uuid))"
            ),
            {"r": relation_id, "m": memory_id},
        )


async def _evidence(relation_id: str) -> set[str]:
    async with get_session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT memory_id FROM relation_evidence "
                    "WHERE relation_id = CAST(:r AS uuid)"
                ),
                {"r": relation_id},
            )
        ).all()
    return {str(r[0]) for r in rows}


@pytest.mark.parametrize("direction", ["outgoing", "incoming"])
async def test_a_merged_relation_keeps_the_duplicates_evidence(sc, direction):
    """L-233: the duplicate's edge to (or from) the same entity as one of the
    canonical's was deleted with its evidence. The canonical edge now records
    the memories that asserted either."""
    tenant = _t()
    canonical = await _acme(tenant, "Acme Corporation")
    dupe = await _acme(tenant, "Acme")
    other = await _seed_entity(
        tenant_id=tenant,
        canonical_name="Globex",
        name_embedding=fake_embedding("globex"),
    )

    async def edge(entity: str) -> str:
        ends = (entity, other) if direction == "outgoing" else (other, entity)
        return await _seed_relation(
            tenant_id=tenant, from_entity_id=ends[0], to_entity_id=ends[1]
        )

    kept, folded = await edge(canonical), await edge(dupe)
    said_kept = await _seed_memory(tenant_id=tenant, content="Acme Corporation, Globex")
    said_folded = await _seed_memory(tenant_id=tenant, content="Acme, Globex")
    await _record_evidence(kept, said_kept)
    await _record_evidence(folded, said_folded)

    resp = await _resolve(sc, tenant)

    assert resp["merged_entity_ids"] == [dupe]
    assert await _evidence(kept) == {said_kept, said_folded}


async def test_the_duplicates_attributes_survive_the_merge(sc):
    """L-234: only the duplicate's aliases were carried over. Its other keys now
    are too, and where both rows name a key the canonical's value stays."""
    tenant = _t()
    canonical = await _acme(
        tenant, "Acme Corporation", {"hq": "Boston", "ticker": "ACME"}
    )
    await _acme(
        tenant, "Acme", {"ticker": "ACM", "founded": "1999", "_aliases": ["ACME Inc"]}
    )

    await _resolve(sc, tenant)

    attrs = await _entity_attrs(canonical)
    assert attrs["hq"] == "Boston"
    assert attrs["ticker"] == "ACME"
    assert attrs.get("founded") == "1999", attrs
    assert {"Acme", "ACME Inc", "Acme Corporation"} <= set(attrs["_aliases"])


async def _until_blocked_by(dsn: str, pid: int) -> None:
    """Return once some backend waits on a lock ``pid`` holds."""
    probe = await asyncpg.connect(dsn)
    try:
        deadline = time.monotonic() + 15
        while not await probe.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
            "WHERE $1 = ANY(pg_blocking_pids(pid)))",
            pid,
        ):
            assert time.monotonic() < deadline, "the merge never waited on the writer"
            await asyncio.sleep(0.05)
    finally:
        await probe.close()


async def test_a_key_committed_while_the_merge_waits_is_kept(sc):
    """L-234: a writer holds the canonical row with a new key while the merge
    runs. The merge used to read the row before that commit and write its
    attributes back after it, dropping the key. It now locks the cluster rows
    before it reads them, so it reads the committed key.

    The writer commits only once the merge is waiting on its lock, so neither
    order depends on timing."""
    tenant = _t()
    canonical = await _acme(tenant, "Acme Corporation", {"hq": "Boston"})
    await _acme(tenant, "Acme")
    dsn = TEST_DB_URL.replace("postgresql+asyncpg://", "postgresql://")

    writer = await asyncpg.connect(dsn)
    try:
        tx = writer.transaction()
        await tx.start()
        await writer.execute(
            "UPDATE entities SET attributes = CAST($1 AS json) WHERE id = $2",
            json.dumps({"hq": "Boston", "ceo": "Ada"}),
            uuid.UUID(canonical),
        )
        writer_pid = await writer.fetchval("SELECT pg_backend_pid()")
        merge = asyncio.create_task(_resolve(sc, tenant))
        try:
            await _until_blocked_by(dsn, writer_pid)
        finally:
            await tx.commit()
        resp = await merge
    finally:
        await writer.close()

    assert resp["merge_count"] == 1, resp
    assert (await _entity_attrs(canonical)).get("ceo") == "Ada"

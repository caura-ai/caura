"""Reads whose callers never see a vector leave it in the database (audit 2026-10-01, B33).

A memory's ``embedding`` is 1024 floats that Postgres renders as text and pgvector parses back into a
list, and ``search_vector`` is its tsvector. Search's by-id load and successor lookup (L-188), the
contradiction candidates and bulk-get (L-189), and the memory lists and the insights reads (L-194)
loaded both for every row, then sent or dictified the row without them. Each now leaves both unloaded.
Bulk-get loads the embedding when asked, for the bulk re-embed, and the insights discover sample keeps
the embedding it clusters on.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import inspect, update

from common.models import Memory, MemoryEntityLink
from common.models.entity import LINK_SOURCE_EXTRACTION
from core_storage_api.services.postgres_service import PostgresService, get_session
from tests.test_integration import PREFIX, _memory_payload, fake_embedding

pytestmark = [pytest.mark.integration]

_VECTORS = {"embedding", "search_vector"}
_svc = PostgresService()


def _tenant() -> str:
    """A fresh tenant, under the prefix the end-of-run sweep removes."""
    return f"test-tenant-{uuid.uuid4().hex[:8]}"


def _vectors_loaded(rows: Iterable[Memory]) -> list[set[str]]:
    """Which of the two large columns each row loaded."""
    return [_VECTORS - inspect(row).unloaded for row in rows]


async def _memory(client: AsyncClient, tenant: str, fleet_id: str, **fields) -> str:
    """A live memory with an embedding; each one is newer than the last."""
    resp = await client.post(f"{PREFIX}/memories", json={**_memory_payload(tenant, fleet_id), **fields})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _status(client: AsyncClient, tenant: str, memory_id: str, status: str, **edge: str) -> None:
    resp = await client.patch(
        f"{PREFIX}/memories/{memory_id}/status", json={"tenant_id": tenant, "status": status, **edge}
    )
    assert resp.status_code == 200, resp.text


async def _record(client: AsyncClient, tenant: str, fleet_id: str, new_id: str, old_id: str) -> None:
    resp = await client.post(
        f"{PREFIX}/memories/conflicts",
        json={
            "tenant_id": tenant,
            "fleet_id": fleet_id,
            "new_memory_id": new_id,
            "old_memory_id": old_id,
            "relationship": "exact_value",
            "action": "supersede",
        },
    )
    assert resp.status_code == 200, resp.text


async def _entity(tenant: str, name: str) -> uuid.UUID:
    entity = await _svc.entity_add(
        {"tenant_id": tenant, "entity_type": "concept", "canonical_name": f"{name} {uuid.uuid4().hex[:6]}"}
    )
    return entity.id


async def _link(memory_id: str, entity_id: uuid.UUID) -> None:
    async with get_session() as session:
        session.add(
            MemoryEntityLink(
                memory_id=uuid.UUID(memory_id),
                entity_id=entity_id,
                role="subject",
                source=LINK_SOURCE_EXTRACTION,
            )
        )


async def _recalled(memory_id: str, times: int) -> None:
    async with get_session() as session:
        await session.execute(
            update(Memory).where(Memory.id == uuid.UUID(memory_id)).values(recall_count=times)
        )


@pytest.fixture
def dictified(monkeypatch: pytest.MonkeyPatch) -> list[Memory]:
    """The rows the insights reads hand to ``_insights_rows_to_dicts``, which every one of them calls."""
    rows: list[Memory] = []
    real = PostgresService._insights_rows_to_dicts

    def spy(batch, **kwargs) -> list[dict]:
        batch = list(batch)
        rows.extend(batch)
        return real(batch, **kwargs)

    monkeypatch.setattr(PostgresService, "_insights_rows_to_dicts", staticmethod(spy))
    return rows


async def test_l188_the_entity_route_load_leaves_the_vectors(client: AsyncClient, fleet_id: str) -> None:
    tenant = _tenant()
    memory_id = await _memory(client, tenant, fleet_id)

    rows = await _svc.memory_load_by_ids([uuid.UUID(memory_id)], tenant)

    assert [str(row.id) for row in rows] == [memory_id]
    assert _vectors_loaded(rows) == [set()]


async def test_l188_both_successor_lookups_leave_the_vectors(client: AsyncClient, fleet_id: str) -> None:
    """The edge's successor, and the winner only a ``memory_conflicts`` record names (M-34)."""
    tenant = _tenant()
    replaced = await _memory(client, tenant, fleet_id)
    by_edge = await _memory(client, tenant, fleet_id)
    await _status(client, tenant, by_edge, "active", supersedes_id=replaced)
    loser = await _memory(client, tenant, fleet_id)
    by_record = await _memory(client, tenant, fleet_id)
    await _status(client, tenant, loser, "conflicted")
    await _record(client, tenant, fleet_id, by_record, loser)

    found = await _svc.memory_find_successors([uuid.UUID(replaced), uuid.UUID(loser)], tenant)

    assert sorted(str(row.id) for row, _ in found) == sorted([by_edge, by_record])
    assert _vectors_loaded(row for row, _ in found) == [set(), set()]


async def test_l189_contradiction_candidates_leave_the_vectors(client: AsyncClient, fleet_id: str) -> None:
    """Paths A (similar vectors), B (the same subject and predicate) and C (a shared entity)."""
    tenant = _tenant()
    subject = await _entity(tenant, "candidate subject")
    vector = fake_embedding(f"candidates {uuid.uuid4()}")
    claim = {"embedding": vector, "subject_entity_id": str(subject), "predicate": "status"}
    new = await _memory(client, tenant, fleet_id, object_value="on", **claim)
    old = await _memory(client, tenant, fleet_id, object_value="off", **claim)
    for memory_id in (new, old):
        await _link(memory_id, subject)

    similar = await _svc.memory_find_similar_candidates(
        tenant, fleet_id, vector, uuid.UUID(new), threshold=0.5
    )
    conflicting = await _svc.memory_find_rdf_conflicts(
        tenant, subject, "status", "on", uuid.UUID(new), fleet_id
    )
    overlapping = await _svc.memory_find_entity_overlap_candidates(uuid.UUID(new), tenant, fleet_id)

    for rows in (similar, conflicting, overlapping):
        assert [str(row.id) for row in rows] == [old]
        assert _vectors_loaded(rows) == [set()]


async def test_l189_bulk_get_leaves_the_vectors_unless_asked(client: AsyncClient, fleet_id: str) -> None:
    tenant = _tenant()
    memory_id = uuid.UUID(await _memory(client, tenant, fleet_id))

    plain = await _svc.memory_get_memories_by_ids([memory_id], tenant_id=tenant)
    embedded = await _svc.memory_get_memories_by_ids([memory_id], tenant_id=tenant, with_embedding=True)

    assert _vectors_loaded(plain.values()) == [set()]
    assert _vectors_loaded(embedded.values()) == [{"embedding"}]


async def test_l194_the_memory_lists_leave_the_vectors(client: AsyncClient, fleet_id: str) -> None:
    tenant = _tenant()
    memory_id = await _memory(client, tenant, fleet_id)

    listed = await _svc.memory_list_by_filters(tenant_id=tenant)
    administered = await _svc.memory_admin_list(tenant_id=tenant)

    for rows in (listed, administered):
        assert [str(row.id) for row in rows] == [memory_id]
        assert _vectors_loaded(rows) == [set()]


async def test_l194_the_insights_reads_leave_the_vectors(
    client: AsyncClient, fleet_id: str, dictified: list[Memory]
) -> None:
    """Every read but the discover sample, each with a row to return."""
    tenant = _tenant()
    subject = await _entity(tenant, "insights subject")
    about = {"subject_entity_id": str(subject), "predicate": "color"}
    weak = await _memory(
        client, tenant, fleet_id, agent_id="insights-a", weight=0.2, object_value="blue", **about
    )
    disputed = await _memory(client, tenant, fleet_id, agent_id="insights-b", object_value="red", **about)
    await _status(client, tenant, disputed, "conflicted")
    await _recalled(weak, 2)
    now = datetime.now(UTC)
    scope = {"tenant_id": tenant, "fleet_id": None, "agent_id": "insights-reader", "scope": "all"}

    assert await _svc.insights_query_contradictions(**scope, max_memories=10)
    assert await _svc.insights_query_divergence(**scope, max_memories=10)
    for window_start in (None, now - timedelta(days=1)):
        assert await _svc.insights_query_failures(**scope, max_memories=10, window_start=window_start)
        assert await _svc.insights_query_patterns(**scope, max_memories=10, window_start=window_start)
        assert await _svc.insights_query_stale(
            **scope, thirty_days_ago=now, fourteen_days_ago=now, max_memories=10, window_start=window_start
        )

    assert {str(row.id) for row in dictified} == {weak, disputed}
    assert all(loaded == set() for loaded in _vectors_loaded(dictified))


async def test_l194_the_discover_sample_keeps_only_the_embedding(
    client: AsyncClient, fleet_id: str, dictified: list[Memory]
) -> None:
    tenant = _tenant()
    memory_id = await _memory(client, tenant, fleet_id)
    scope = {"tenant_id": tenant, "fleet_id": None, "agent_id": "insights-reader", "scope": "all"}

    sampled = await _svc.insights_discover_sample(**scope, sample_size=10)
    windowed = await _svc.insights_discover_sample(
        **scope, sample_size=10, window_start=datetime.now(UTC) - timedelta(days=1)
    )

    assert [row["id"] for row in sampled + windowed] == [memory_id, memory_id]
    assert all(row["embedding"] is not None for row in sampled + windowed)
    assert _vectors_loaded(dictified) == [{"embedding"}, {"embedding"}]

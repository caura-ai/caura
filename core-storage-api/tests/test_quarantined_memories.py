"""A held memory (``quarantined``) is seen by a person reviewing it and by
nothing else.

It is written, but it is not live until a person releases it (``active``) or
rejects it (``cancelled``). So no read returns it (recall, lists, the graph,
reads by id unless asked), no count includes it, no delete takes it out of the
review queue, and no writer moves it except release and reject. Each read here
is checked against a live memory beside it, so a read that returns nothing at
all can't pass for one that hides the held row.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from common.constants import (
    LIVE_MEMORY_STATUSES,
    QUARANTINE_EXITS,
    QUARANTINE_REJECTED,
    QUARANTINED_MEMORY_STATUS,
    SEARCH_KNOBS,
)
from common.enrichment.constants import MEMORY_STATUSES
from core_storage_api.services.postgres_service import PostgresService, get_session

pytestmark = pytest.mark.asyncio

_P = "/api/v1/storage"
_EMBEDDING = [0.1] * 1024
_AGENT = "agent-held"
_FLEET = "fleet-held"


def _tenant() -> str:
    return f"t-held-{uuid.uuid4().hex[:8]}"


async def _seed(
    tenant: str,
    *,
    status: str = "active",
    content: str = "the release train leaves on fridays",
    agent: str = _AGENT,
    content_hash: str | None = None,
    metadata: dict | None = None,
) -> uuid.UUID:
    memory_id = uuid.uuid4()
    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO memories (id, tenant_id, fleet_id, agent_id, memory_type, content, "
                "content_hash, status, embedding, weight, visibility, metadata) "
                "VALUES (:id, :t, :f, :a, 'fact', :c, :h, :s, CAST(:e AS vector), 0.5, 'scope_team', "
                "CAST(:m AS jsonb))"
            ),
            {
                "id": memory_id,
                "t": tenant,
                "f": _FLEET,
                "a": agent,
                "c": content,
                "h": content_hash,
                "s": status,
                "e": str(_EMBEDDING),
                "m": json.dumps(metadata) if metadata is not None else None,
            },
        )
    return memory_id


async def _status(memory_id: uuid.UUID) -> tuple[str, bool]:
    async with get_session() as session:
        row = (
            await session.execute(
                text("SELECT status, deleted_at IS NOT NULL FROM memories WHERE id = :id"),
                {"id": memory_id},
            )
        ).one()
    return row[0], row[1]


def _ids(rows: Any) -> set[str]:
    """The memory ids in whatever a read returned."""
    if isinstance(rows, dict):
        rows = list(rows.values())
    found = set()
    for row in rows:
        if isinstance(row, dict):
            value = row.get("id")
        elif isinstance(row, (str, uuid.UUID)):
            value = row
        else:  # an ORM row, or recall's namespace around one
            value = getattr(row, "Memory", row).id
        found.add(str(value))
    return found


async def test_the_held_status_is_outside_every_vocabulary_that_would_release_it() -> None:
    """Not live, so recall and the list defaults leave it out; not in the
    enrichment vocabulary, so a classifier status can never stand in for a
    person's release; and spelt as raw SQL spells it."""
    assert QUARANTINED_MEMORY_STATUS == "quarantined"
    assert QUARANTINED_MEMORY_STATUS not in LIVE_MEMORY_STATUSES
    assert QUARANTINED_MEMORY_STATUS not in MEMORY_STATUSES
    assert set(QUARANTINE_EXITS) == {"active", "cancelled"}


# ── Reads that return memories ──

_ROW_READS = {
    "recall": lambda svc, t, ids: svc.memory_scored_search(
        t,
        _EMBEDDING,
        "release train fridays",
        search_params={k: knob.value_type(knob.bounds[0]) for k, knob in SEARCH_KNOBS.items()},
        top_k=10,
    ),
    "list": lambda svc, t, ids: svc.memory_list_by_filters(tenant_id=t),
    "admin list": lambda svc, t, ids: svc.memory_admin_list(tenant_id=t),
    # With the deleted rows in, held ones still stay out: a held row is never
    # deleted, so asking for deleted rows must not reach it.
    "list with deleted": lambda svc, t, ids: svc.memory_list_by_filters(tenant_id=t, include_deleted=True),
    "admin list with deleted": lambda svc, t, ids: svc.memory_admin_list(tenant_id=t, include_deleted=True),
    "load by ids": lambda svc, t, ids: svc.memory_load_by_ids(ids, t),
    "get by ids": lambda svc, t, ids: svc.memory_get_memories_by_ids(ids, tenant_id=t),
    "evolve scope": lambda svc, t, ids: svc.evolve_filter_by_scope(
        tenant_id=t, caller_agent_id=_AGENT, fleet_id=None, scope="all", ids=[str(i) for i in ids]
    ),
}


@pytest.mark.parametrize("read", sorted(_ROW_READS))
async def test_no_read_returns_a_held_memory(client, read: str) -> None:
    tenant = _tenant()
    live = await _seed(tenant)
    held = await _seed(tenant, status=QUARANTINED_MEMORY_STATUS)

    found = _ids(await _ROW_READS[read](PostgresService(), tenant, [live, held]))

    assert str(live) in found, f"{read} returned nothing to compare against"
    assert str(held) not in found, f"{read} returned a held memory"


async def test_asking_for_the_held_status_does_not_reveal_held_memories(client) -> None:
    """The generic reads never return one, whatever status is asked for. The
    review queue reads held memories through its own route."""
    tenant = _tenant()
    await _seed(tenant, status=QUARANTINED_MEMORY_STATUS)
    svc = PostgresService()

    assert await svc.memory_list_by_filters(tenant_id=tenant, status=QUARANTINED_MEMORY_STATUS) == []
    assert await svc.memory_admin_list(tenant_id=tenant, status=QUARANTINED_MEMORY_STATUS) == []
    assert await svc.memory_count_active(tenant, status=QUARANTINED_MEMORY_STATUS) == 0


async def test_a_held_memory_opens_only_for_whoever_asks_for_held_rows(client) -> None:
    """core-api asks for held rows on a person's behalf only."""
    tenant = _tenant()
    held = await _seed(tenant, status=QUARANTINED_MEMORY_STATUS)
    svc = PostgresService()

    assert await svc.memory_get_by_id_for_tenant(held, tenant) is None
    assert (await svc.memory_get_by_id_for_tenant(held, tenant, include_held=True)).id == held
    assert await svc.memory_get_detail(held, tenant) is None
    assert (await svc.memory_get_detail(held, tenant, include_held=True))["memory"]["id"] == str(held)
    assert await svc.memory_contradiction_rows(held, tenant) is None

    for path in (f"/memories/{held}", f"/memories/{held}/detail"):
        assert (await client.get(f"{_P}{path}", params={"tenant_id": tenant})).status_code == 404
        response = await client.get(f"{_P}{path}", params={"tenant_id": tenant, "include_held": "true"})
        assert response.status_code == 200, response.text


async def test_the_graph_does_not_reach_a_held_memory(client) -> None:
    tenant = _tenant()
    live = await _seed(tenant)
    held = await _seed(tenant, status=QUARANTINED_MEMORY_STATUS)
    entity = uuid.uuid4()
    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO entities (id, tenant_id, entity_type, canonical_name) "
                "VALUES (:id, :t, 'project', 'release train')"
            ),
            {"id": entity, "t": tenant},
        )
        for memory_id in (live, held):
            await session.execute(
                text(
                    "INSERT INTO memory_entity_links (memory_id, entity_id, role) VALUES (:m, :e, 'subject')"
                ),
                {"m": memory_id, "e": entity},
            )
    svc = PostgresService()

    linked = {str(memory.id) for _, memory in await svc.entity_get_linked_memories(entity, tenant)}
    assert linked == {str(live)}
    assert await svc.entity_count_memories_per_entity([entity], tenant) == {entity: 1}
    assert await svc.memory_entity_coverage_count(tenant) == 1


# ── Counts ──


def _since() -> datetime:
    return datetime.now(UTC) - timedelta(days=1)


_COUNTS = {
    "count active": lambda svc, t: svc.memory_count_active(t),
    "short content": lambda svc, t: svc.memory_find_short_content(t, None, 10_000),
    "health stats": lambda svc, t: svc.memory_compute_health_stats(t, None),
    "fleet distribution": lambda svc, t: svc.memory_fleet_distribution(t, exclude_scope_agent=False),
    "admin stats": lambda svc, t: svc.memory_admin_stats(t, None),
    "stats breakdown": lambda svc, t: svc.memory_stats_breakdown(tenant_id=t),
    "stats breakdown with deleted": lambda svc, t: svc.memory_stats_breakdown(
        tenant_id=t, include_deleted=True
    ),
    "daily durable counts": lambda svc, t: svc.memory_daily_durable_counts(tenant_id=t, since=_since()),
    "quality metrics": lambda svc, t: svc.memory_quality_metrics(tenant_id=t),
    "fleet agent stats": lambda svc, t: svc.fleet_agent_stats(t, None),
    "all memories": lambda svc, t: svc.memory_count_all(),
    "distinct agents": lambda svc, t: svc.memory_distinct_agent_count(),
    "distinct tenants": lambda svc, t: svc.memory_distinct_tenant_count(),
}


@pytest.mark.parametrize("count", sorted(_COUNTS))
async def test_a_held_memory_changes_no_count(client, count: str) -> None:
    """Holding a memory changes nothing a count reports, and writing a live one
    does, so the count is one that sees the rows seeded here."""
    tenant = _tenant()
    await _seed(tenant)
    svc = PostgresService()
    before = await _COUNTS[count](svc, tenant)

    # A new agent too, for the counts of distinct agents, and a tenant holding
    # nothing else, for the count of distinct tenants.
    await _seed(tenant, status=QUARANTINED_MEMORY_STATUS, agent=f"held-{uuid.uuid4().hex[:6]}", content="x")
    await _seed(_tenant(), status=QUARANTINED_MEMORY_STATUS, content="x")
    assert await _COUNTS[count](svc, tenant) == before, f"{count} counted a held memory"

    other = _tenant()
    await _seed(other, agent=f"live-{uuid.uuid4().hex[:6]}", content="y")
    await _seed(tenant, agent=f"live-{uuid.uuid4().hex[:6]}", content="z")
    assert await _COUNTS[count](svc, tenant) != before, f"{count} does not see the rows this test seeds"


async def test_a_tenant_whose_only_memory_is_held_looks_empty(client) -> None:
    """The cheap gates the background passes check first, and the probe that
    tells an agent whether its search can find anything."""
    held_tenant, live_tenant = _tenant(), _tenant()
    await _seed(held_tenant, status=QUARANTINED_MEMORY_STATUS)
    await _seed(live_tenant)
    svc = PostgresService()

    for gate in (svc.insights_activity_gate, svc.crystallizer_activity_gate):
        assert await gate(tenant_id=held_tenant, fleet_id=None) != await gate(
            tenant_id=live_tenant, fleet_id=None
        )
    held_probe = await svc.memory_agent_scope_probe(tenant_id=held_tenant, agent_id=_AGENT)
    live_probe = await svc.memory_agent_scope_probe(tenant_id=live_tenant, agent_id=_AGENT)
    assert held_probe != live_probe


async def test_a_held_memory_still_dedups_its_own_agents_retry(client) -> None:
    """Deliberately NOT excluded: the live content-hash constraint is keyed per
    agent, so a held write blocks only the same agent writing the same content
    again, which would otherwise queue the same write twice."""
    tenant = _tenant()
    held = await _seed(tenant, status=QUARANTINED_MEMORY_STATUS, content_hash="h-held")

    found = await PostgresService().memory_find_by_content_hash(
        tenant, "h-held", fleet_id=_FLEET, agent_id=_AGENT
    )

    assert found is not None and found.id == held


# ── Writes ──


async def test_no_writer_moves_a_held_memory(client) -> None:
    """Contradiction detection, the near-duplicate merge, the crystallizer and
    a caller's transition all flip status through here."""
    tenant = _tenant()
    held = await _seed(tenant, status=QUARANTINED_MEMORY_STATUS)
    other = await _seed(tenant)
    svc = PostgresService()

    assert await svc.memory_update_status(held, "outdated", tenant_id=tenant) is False
    assert await svc.memory_update_status(held, "active", tenant_id=tenant, supersedes_id=other) is False
    assert await _status(held) == (QUARANTINED_MEMORY_STATUS, False)


@pytest.mark.parametrize("exit_status", QUARANTINE_EXITS)
async def test_release_and_reject_are_the_way_out(client, exit_status: str) -> None:
    tenant = _tenant()
    held = await _seed(tenant, status=QUARANTINED_MEMORY_STATUS)
    live = await _seed(tenant)
    svc = PostgresService()

    assert await svc.memory_update_status(held, exit_status, tenant_id=tenant, release_hold=True) is True
    # A rejected one is deleted too: no read returns it.
    assert await _status(held) == (exit_status, exit_status == QUARANTINE_REJECTED)
    # Only a held memory leaves quarantine.
    assert await svc.memory_update_status(live, exit_status, tenant_id=tenant, release_hold=True) is False


async def test_nothing_enters_quarantine_or_leaves_it_elsewhere(client) -> None:
    tenant = _tenant()
    held = await _seed(tenant, status=QUARANTINED_MEMORY_STATUS)
    live = await _seed(tenant)
    svc = PostgresService()

    with pytest.raises(ValueError, match="held when it is written"):
        await svc.memory_update_status(live, QUARANTINED_MEMORY_STATUS, tenant_id=tenant)
    with pytest.raises(ValueError, match="held memory becomes one of"):
        await svc.memory_update_status(held, "outdated", tenant_id=tenant, release_hold=True)
    assert await _status(held) == (QUARANTINED_MEMORY_STATUS, False)
    assert await _status(live) == ("active", False)


async def test_the_status_route_moves_a_held_memory_only_by_release_or_reject(client) -> None:
    tenant = _tenant()
    held = await _seed(tenant, status=QUARANTINED_MEMORY_STATUS)
    other = await _seed(tenant)
    url = f"{_P}/memories/{held}/status"

    refused = [
        {"status": "outdated"},
        {"status": "active", "supersedes_id": str(other)},
        {"status": "active", "unset_supersedes": True, "expected_supersedes_id": str(other)},
    ]
    for body in refused:
        response = await client.patch(url, json={"tenant_id": tenant, **body})
        assert response.status_code == 404, (body, response.text)
    invalid = [
        {"status": QUARANTINED_MEMORY_STATUS},
        {"status": "outdated", "release_hold": True},
        {"status": "active", "release_hold": True, "supersedes_id": str(other)},
    ]
    for body in invalid:
        response = await client.patch(url, json={"tenant_id": tenant, **body})
        assert response.status_code == 422, (body, response.text)
    assert await _status(held) == (QUARANTINED_MEMORY_STATUS, False)

    response = await client.patch(url, json={"tenant_id": tenant, "status": "active", "release_hold": True})
    assert response.status_code == 200, response.text
    assert await _status(held) == ("active", False)


async def test_no_delete_takes_a_held_memory_out_of_the_queue(client) -> None:
    """Not even its writer's: it leaves only by a person's release or reject."""
    tenant = _tenant()
    held = await _seed(tenant, status=QUARANTINED_MEMORY_STATUS)
    live = await _seed(tenant)
    other = await _seed(tenant)
    svc = PostgresService()

    assert await svc.memory_soft_delete_by_ids(tenant, [held, live]) == 1
    # Nor the tenant-wide delete, the dashboard's "reset workspace".
    assert await svc.memory_soft_delete_by_filter(tenant_id=tenant) == 1

    assert await _status(held) == (QUARANTINED_MEMORY_STATUS, False)
    assert await _status(live) == ("deleted", True)
    assert await _status(other) == ("deleted", True)


async def test_a_reject_or_a_purge_removes_a_held_memory(client) -> None:
    """The ways out of the queue that the API reference names for a delete."""
    tenant = _tenant()
    svc = PostgresService()

    rejected = await _seed(tenant, status=QUARANTINED_MEMORY_STATUS)
    assert await svc.memory_update_status(rejected, QUARANTINE_REJECTED, tenant_id=tenant, release_hold=True)
    assert await _status(rejected) == (QUARANTINE_REJECTED, True)
    # Already deleted, so a delete finds nothing left to do.
    assert await svc.memory_soft_delete_by_ids(tenant, [rejected]) == 0

    for purge in (lambda: svc.purge_fleet_data(tenant, _FLEET), lambda: svc.purge_tenant_data(tenant)):
        held = await _seed(tenant, status=QUARANTINED_MEMORY_STATUS)
        assert await _status(held) == (QUARANTINED_MEMORY_STATUS, False)
        await purge()
        async with get_session() as session:
            row = await session.execute(text("SELECT 1 FROM memories WHERE id = :id"), {"id": held})
            assert row.first() is None


# ── A held write's auto-chunks (g2.8) ──


async def _held_write_with_chunks(
    tenant: str, metadata: dict | None = None
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    parent = await _seed(
        tenant, status=QUARANTINED_MEMORY_STATUS, content="the whole held write", metadata=metadata
    )
    chunks = [
        await _seed(
            tenant,
            status=QUARANTINED_MEMORY_STATUS,
            content=f"held chunk {n}",
            metadata={"parent_memory_id": str(parent), "source": "auto_chunk"},
        )
        for n in range(2)
    ]
    return parent, chunks


@pytest.mark.parametrize("exit_status", QUARANTINE_EXITS)
async def test_a_held_writes_chunks_leave_quarantine_with_it(client, exit_status: str) -> None:
    tenant = _tenant()
    parent, chunks = await _held_write_with_chunks(tenant)
    other = await _seed(tenant, status=QUARANTINED_MEMORY_STATUS, content="another held write")

    assert await PostgresService().memory_update_status(
        parent, exit_status, tenant_id=tenant, release_hold=True
    )

    assert [await _status(m) for m in (parent, *chunks)] == [
        (exit_status, exit_status == QUARANTINE_REJECTED)
    ] * 3
    assert await _status(other) == (QUARANTINED_MEMORY_STATUS, False)


async def test_the_queue_lists_a_held_write_once_not_its_chunks(client) -> None:
    tenant = _tenant()
    parent, _chunks = await _held_write_with_chunks(tenant)

    rows, total = await PostgresService().memory_list_held(tenant)

    assert [row.id for row in rows] == [parent]
    assert total == 1


async def test_a_rollback_cancels_a_held_writes_chunks_with_it(client) -> None:
    tenant = _tenant()
    parent, chunks = await _held_write_with_chunks(tenant, metadata={"session_id": "s-held"})

    changed = await PostgresService().memory_rollback_session(tenant, "s-held")

    assert sorted(changed["cancelled"]) == sorted(str(m) for m in (parent, *chunks))
    assert [await _status(m) for m in (parent, *chunks)] == [(QUARANTINE_REJECTED, True)] * 3


# ── A write a person turned down ──

# Every read that leaves deleted rows out. The two that ask for deleted rows
# return a rejected write as the deleted row it is.
_LIVE_ROW_READS = {name: read for name, read in _ROW_READS.items() if "with deleted" not in name}


@pytest.mark.parametrize("read", sorted(_LIVE_ROW_READS))
async def test_no_read_returns_a_write_a_person_turned_down(client, read: str) -> None:
    """Rejected, or held in a session that was rolled back, it never goes live.
    Its status is ``cancelled``, which recall returns for an ordinary memory,
    so the reject has to take it out of the reads itself."""
    tenant = _tenant()
    svc = PostgresService()
    live = await _seed(tenant)
    rejected = await _seed(tenant, status=QUARANTINED_MEMORY_STATUS)
    rolled_back = await _seed(tenant, status=QUARANTINED_MEMORY_STATUS, metadata={"session_id": "s-gone"})
    assert await svc.memory_update_status(rejected, QUARANTINE_REJECTED, tenant_id=tenant, release_hold=True)
    assert (await svc.memory_rollback_session(tenant, "s-gone"))["cancelled"] == [str(rolled_back)]

    found = _ids(await _LIVE_ROW_READS[read](svc, tenant, [live, rejected, rolled_back]))

    assert str(live) in found, f"{read} returned nothing to compare against"
    assert str(rejected) not in found, f"{read} returned a rejected write"
    assert str(rolled_back) not in found, f"{read} returned a rolled-back held write"


async def test_recall_still_returns_an_ordinary_cancelled_memory(client) -> None:
    """Only a person's reject deletes. A memory enrichment calls cancelled
    ("the offsite is cancelled"), or one a caller moves to cancelled, was never
    held, and stays findable."""
    tenant = _tenant()
    svc = PostgresService()
    enriched = await _seed(tenant, status=QUARANTINE_REJECTED)
    moved = await _seed(tenant)
    assert await svc.memory_update_status(moved, QUARANTINE_REJECTED, tenant_id=tenant) is True

    found = _ids(await _ROW_READS["recall"](svc, tenant, [enriched, moved]))

    assert {str(enriched), str(moved)} <= found
    assert [await _status(m) for m in (enriched, moved)] == [(QUARANTINE_REJECTED, False)] * 2

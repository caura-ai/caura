"""The daily purge ages out a tenant's recall log and its spent replays too.

* Recall events (the opt-in recall log, which keeps each query's text) older
  than the org's retention window go, with their candidates. Newer ones stay.
* Idempotency responses past their expiry, which replay no longer serves, go.
  Live ones stay.
* Both are swept whether or not any memory was due, for the purged tenant
  only, and purge discovery lists a tenant that has either kind of row.
* The sweeps go a batch per transaction until a batch comes back short, so a
  backlog larger than one batch is cleared in one purge.

Full path: core-api storage client -> the in-process storage app (conftest
ASGI bridge) -> test DB.
"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import text

from core_api.clients.storage_client import get_storage_client
from core_api.services.tenants import list_tenants_with_purgeable_memories
from core_storage_api.services import postgres_service
from core_storage_api.services.postgres_service import get_session


def _tenant() -> str:
    return f"test-tenant-aging-{uuid4().hex[:8]}"


async def _recall_event(tenant_id: str, *, hours_ago: int) -> str:
    """A recall logged that long ago, with one candidate."""
    event_id = str(uuid4())
    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO recall_event (id, tenant_id, ts, source, query_text) "
                "VALUES (CAST(:id AS uuid), :tenant_id, "
                "now() - make_interval(hours => CAST(:hours AS integer)), "
                "'mcp_recall', 'when is the offsite')"
            ),
            {"id": event_id, "tenant_id": tenant_id, "hours": hours_ago},
        )
        await session.execute(
            text(
                "INSERT INTO recall_candidate (recall_event_id, rank, memory_id) "
                "VALUES (CAST(:id AS uuid), 1, CAST(:memory_id AS uuid))"
            ),
            {"id": event_id, "memory_id": str(uuid4())},
        )
    return event_id


async def _replay(tenant_id: str, *, expires_in_minutes: int) -> str:
    """A stored idempotency response expiring that many minutes from now."""
    key = f"key-{uuid4().hex[:8]}"
    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO idempotency_responses "
                "(tenant_id, idempotency_key, request_hash, status_code, expires_at) "
                "VALUES (:tenant_id, :key, 'hash', 201, "
                "now() + make_interval(mins => CAST(:minutes AS integer)))"
            ),
            {"tenant_id": tenant_id, "key": key, "minutes": expires_in_minutes},
        )
    return key


async def _column(sql: str, value: str) -> set[str]:
    async with get_session() as session:
        rows = (await session.execute(text(sql), {"value": value})).all()
    return {str(row[0]) for row in rows}


async def _events(tenant_id: str) -> set[str]:
    return await _column(
        "SELECT id FROM recall_event WHERE tenant_id = :value", tenant_id
    )


async def _candidates(event_id: str) -> set[str]:
    return await _column(
        "SELECT id FROM recall_candidate WHERE recall_event_id = CAST(:value AS uuid)",
        event_id,
    )


async def _replays(tenant_id: str) -> set[str]:
    return await _column(
        "SELECT idempotency_key FROM idempotency_responses WHERE tenant_id = :value",
        tenant_id,
    )


async def test_the_purge_ages_out_old_recalls_and_spent_replays():
    tenant, other = _tenant(), _tenant()
    old = await _recall_event(tenant, hours_ago=8 * 24)
    recent = await _recall_event(tenant, hours_ago=6 * 24)
    await _replay(tenant, expires_in_minutes=-5)
    live = await _replay(tenant, expires_in_minutes=60)
    others_old = await _recall_event(other, hours_ago=8 * 24)
    others_spent = await _replay(other, expires_in_minutes=-5)

    # None of the tenant's memories is due, so only the sweeps have work.
    purged = await get_storage_client().purge_soft_deleted(tenant, retention_days=7)

    assert purged == 0
    assert await _events(tenant) == {recent}
    assert await _candidates(old) == set()
    assert await _replays(tenant) == {live}
    assert await _events(other) == {others_old}
    assert await _replays(other) == {others_spent}


async def test_purge_discovery_lists_tenants_with_old_recalls_or_spent_replays():
    recalls, replays, fresh = _tenant(), _tenant(), _tenant()
    await _recall_event(recalls, hours_ago=2 * 24)
    await _replay(replays, expires_in_minutes=-5)
    await _recall_event(fresh, hours_ago=1)
    await _replay(fresh, expires_in_minutes=60)

    listed = set(await list_tenants_with_purgeable_memories())

    assert recalls in listed
    assert replays in listed
    assert fresh not in listed


async def test_a_backlog_larger_than_a_batch_is_cleared_in_one_purge(monkeypatch):
    monkeypatch.setattr(postgres_service, "_RETENTION_SWEEP_BATCH", 2)
    tenant = _tenant()
    for _ in range(5):
        await _recall_event(tenant, hours_ago=8 * 24)
        await _replay(tenant, expires_in_minutes=-5)
    recent = await _recall_event(tenant, hours_ago=1)

    await get_storage_client().purge_soft_deleted(tenant, retention_days=7)

    assert await _events(tenant) == {recent}
    assert await _replays(tenant) == set()

"""Memory exposure's storage half (migration 072), against a live PostgreSQL.

One row per (tenant, memory, agent): a read inserts it or adds one to its
count, keeps the earliest ``first_seen`` and the latest ``last_seen``. Ids that
name no memory of the tenant are skipped, and the rows go with the memory.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from core_storage_api.services.postgres_service import get_session

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

PREFIX = "/api/v1/storage"
T0 = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)


def _embedding(content: str) -> list[float]:
    h = hashlib.sha256(content.encode()).digest()
    return [((h[i % len(h)] / 255.0) - 0.5) for i in range(1024)]


async def _memory(client: AsyncClient, tenant: str) -> str:
    content = f"exposure test {tenant} {uuid.uuid4().hex}"
    r = await client.post(
        f"{PREFIX}/memories",
        json={
            "tenant_id": tenant,
            "agent_id": "writer",
            "memory_type": "fact",
            "content": content,
            "embedding": _embedding(content),
            "content_hash": hashlib.sha256(content.encode()).hexdigest(),
            "weight": 0.7,
            "visibility": "scope_team",
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _record(
    client: AsyncClient, tenant: str, agent: str, ids: list[str], at: datetime, trace: str | None = None
) -> int:
    r = await client.post(
        f"{PREFIX}/memory-exposure",
        params={"tenant_id": tenant},
        json={"agent_id": agent, "memory_ids": ids, "seen_at": at.isoformat(), "trace_id": trace},
    )
    assert r.status_code == 200, r.text
    return r.json()["recorded"]


async def _agents(client: AsyncClient, tenant: str, memory_id: str) -> list[dict]:
    r = await client.get(f"{PREFIX}/memory-exposure/{memory_id}", params={"tenant_id": tenant})
    assert r.status_code == 200, r.text
    return r.json()["agents"]


async def test_reads_are_counted_per_agent_with_first_and_last_seen(client: AsyncClient):
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    m1, m2 = await _memory(client, tenant), await _memory(client, tenant)

    assert await _record(client, tenant, "a1", [m1, m2], T0, "trace-1") == 2
    assert await _record(client, tenant, "a1", [m1], T0 + timedelta(minutes=5), "trace-2") == 1
    # A read without a trace keeps the last one known.
    assert await _record(client, tenant, "a1", [m1], T0 + timedelta(minutes=9)) == 1
    # A late-arriving earlier read moves first_seen back, not last_seen.
    assert await _record(client, tenant, "a2", [m1], T0 + timedelta(minutes=7), "trace-b") == 1
    assert await _record(client, tenant, "a2", [m1], T0 + timedelta(minutes=1), "trace-a") == 1

    agents = {a["agent_id"]: a for a in await _agents(client, tenant, m1)}
    assert set(agents) == {"a1", "a2"}
    assert agents["a1"]["count"] == 3
    assert datetime.fromisoformat(agents["a1"]["first_seen"]) == T0
    assert datetime.fromisoformat(agents["a1"]["last_seen"]) == T0 + timedelta(minutes=9)
    assert agents["a1"]["last_trace_id"] == "trace-2"
    assert agents["a2"]["count"] == 2
    assert datetime.fromisoformat(agents["a2"]["first_seen"]) == T0 + timedelta(minutes=1)
    assert datetime.fromisoformat(agents["a2"]["last_seen"]) == T0 + timedelta(minutes=7)
    # Latest read first.
    assert [a["agent_id"] for a in await _agents(client, tenant, m1)] == ["a1", "a2"]
    assert [a["count"] for a in await _agents(client, tenant, m2)] == [1]


async def test_a_duplicate_id_in_one_read_counts_once(client: AsyncClient):
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    m = await _memory(client, tenant)
    assert await _record(client, tenant, "a1", [m, m], T0) == 1
    assert (await _agents(client, tenant, m))[0]["count"] == 1


async def test_ids_outside_the_tenant_are_skipped_not_refused(client: AsyncClient):
    tenant, other = f"t-{uuid.uuid4().hex[:8]}", f"t-{uuid.uuid4().hex[:8]}"
    mine, theirs = await _memory(client, tenant), await _memory(client, other)

    assert await _record(client, tenant, "a1", [mine, theirs, str(uuid.uuid4())], T0) == 1
    assert await _agents(client, other, theirs) == []
    # The tenant scopes the read as well as the write.
    assert await _agents(client, other, mine) == []


async def test_rows_go_with_the_memory(client: AsyncClient):
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    m = await _memory(client, tenant)
    await _record(client, tenant, "a1", [m], T0)
    async with get_session() as session:
        await session.execute(text("DELETE FROM memories WHERE id = :id"), {"id": m})
    async with get_session() as session:
        left = await session.execute(
            text("SELECT count(*) FROM memory_exposure WHERE memory_id = :id"), {"id": m}
        )
        assert left.scalar_one() == 0


async def test_the_batch_is_validated(client: AsyncClient):
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    url = f"{PREFIX}/memory-exposure"
    ok = {"agent_id": "a1", "memory_ids": [str(uuid.uuid4())], "seen_at": T0.isoformat()}
    assert (
        await client.post(url, params={"tenant_id": tenant}, json={**ok, "memory_ids": []})
    ).status_code == 422
    assert (
        await client.post(url, params={"tenant_id": tenant}, json={**ok, "agent_id": ""})
    ).status_code == 422
    assert (
        await client.post(url, params={"tenant_id": tenant}, json={**ok, "memory_ids": ["x"]})
    ).status_code == 422
    assert (await client.post(url, params={"tenant_id": ""}, json=ok)).status_code == 422

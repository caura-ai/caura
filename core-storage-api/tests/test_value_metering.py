"""Value metering's storage half (migration 067), against a live PostgreSQL.

The rules the figures rest on: a session's origin time runs from its start or
its previous write; a memory is reused once per session, never by the session
that wrote it; cross-agent reuse and the teachers/learners lists come from
writer versus reader; sessions merge across instances.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

PREFIX = "/api/v1/storage"
T0 = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)


def _embedding(text: str) -> list[float]:
    h = hashlib.sha256(text.encode()).digest()
    return [((h[i % len(h)] / 255.0) - 0.5) for i in range(1024)]


async def _memory(client: AsyncClient, tenant: str, agent: str, metadata: dict | None = None) -> str:
    content = f"value metering test {tenant} {uuid.uuid4().hex}"
    r = await client.post(
        f"{PREFIX}/memories",
        json={
            "tenant_id": tenant,
            "agent_id": agent,
            "memory_type": "fact",
            "content": content,
            "embedding": _embedding(content),
            "content_hash": hashlib.sha256(content.encode()).hexdigest(),
            "weight": 0.7,
            "visibility": "scope_team",
            **({"metadata_": metadata} if metadata else {}),
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _post(client: AsyncClient, path: str, tenant: str, body: dict) -> dict:
    r = await client.post(f"{PREFIX}/value/{path}", params={"tenant_id": tenant}, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _iso(t: datetime) -> str:
    return t.isoformat()


async def _value(client: AsyncClient, tenant: str) -> dict:
    return await _post(
        client, "query", tenant, {"start": _iso(T0 - timedelta(days=1)), "end": _iso(T0 + timedelta(days=2))}
    )


async def test_origin_time_runs_from_the_session_start_then_from_the_previous_write(client: AsyncClient):
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    await _post(
        client,
        "sessions",
        tenant,
        {
            "sessions": [
                {
                    "session_id": "s1",
                    "agent_id": "a1",
                    "source": "mcp",
                    "first_seen": _iso(T0),
                    "last_seen": _iso(T0),
                    "calls": 3,
                }
            ]
        },
    )
    m1, m2 = await _memory(client, tenant, "a1"), await _memory(client, tenant, "a1")
    first = await _post(
        client,
        "origins",
        tenant,
        {
            "memory_id": m1,
            "session_id": "s1",
            "agent_id": "a1",
            "written_at": _iso(T0 + timedelta(minutes=12)),
            "origin_calls": 3,
        },
    )
    second = await _post(
        client,
        "origins",
        tenant,
        {
            "memory_id": m2,
            "session_id": "s1",
            "agent_id": "a1",
            "written_at": _iso(T0 + timedelta(minutes=20)),
        },
    )
    assert first == {"recorded": True, "origin_seconds": 720}
    assert second["origin_seconds"] == 480
    # Recorded once: a retry changes nothing.
    again = await _post(
        client,
        "origins",
        tenant,
        {"memory_id": m1, "session_id": "s1", "written_at": _iso(T0 + timedelta(minutes=50))},
    )
    assert again["recorded"] is False


async def test_a_memory_is_reused_once_per_session_and_never_by_its_own_session(client: AsyncClient):
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    await _post(
        client,
        "sessions",
        tenant,
        {"sessions": [{"session_id": "writer", "first_seen": _iso(T0), "last_seen": _iso(T0)}]},
    )
    m = await _memory(client, tenant, "teacher")
    await _post(
        client,
        "origins",
        tenant,
        {
            "memory_id": m,
            "session_id": "writer",
            "agent_id": "teacher",
            "written_at": _iso(T0 + timedelta(minutes=30)),
            "origin_tokens_in": 9000,
            "origin_tokens_out": 1000,
            "origin_turns": 7,
            "origin_source": "hook",
        },
    )
    at = _iso(T0 + timedelta(hours=1))
    assert (
        await _post(
            client,
            "reuse",
            tenant,
            {"session_id": "writer", "reader_agent_id": "teacher", "memory_ids": [m], "recalled_at": at},
        )
    )["recorded"] == 0
    assert (
        await _post(
            client,
            "reuse",
            tenant,
            {"session_id": "reader", "reader_agent_id": "learner", "memory_ids": [m], "recalled_at": at},
        )
    )["recorded"] == 1
    assert (
        await _post(
            client,
            "reuse",
            tenant,
            {"session_id": "reader", "reader_agent_id": "learner", "memory_ids": [m, m], "recalled_at": at},
        )
    )["recorded"] == 0
    v = await _value(client, tenant)
    t = v["totals"]
    assert (t["reuses"], t["sessions_helped"], t["memories_reused"]) == (1, 1, 1)
    assert (
        t["origin_seconds"],
        t["origin_tokens_in"],
        t["origin_tokens_out"],
        t["origin_turns"],
        t["reuses_with_tokens"],
    ) == (1800, 9000, 1000, 7, 1)
    assert t["cross_agent_reuses"] == 1
    assert [x["agent_id"] for x in v["teachers"]] == ["teacher"]
    assert [x["agent_id"] for x in v["learners"]] == ["learner"]


async def test_the_brokers_session_on_the_memory_counts_when_no_origin_was_recorded(client: AsyncClient):
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    m = await _memory(client, tenant, "broker-agent", {"session_id": "broker-1"})
    at = _iso(T0 + timedelta(hours=1))
    assert (
        await _post(client, "reuse", tenant, {"session_id": "broker-1", "memory_ids": [m], "recalled_at": at})
    )["recorded"] == 0
    assert (
        await _post(client, "reuse", tenant, {"session_id": "other", "memory_ids": [m], "recalled_at": at})
    )["recorded"] == 1


async def test_sessions_merge_across_instances(client: AsyncClient):
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    a = {
        "session_id": "s1",
        "agent_id": "",
        "source": "mcp",
        "first_seen": _iso(T0 + timedelta(minutes=5)),
        "last_seen": _iso(T0 + timedelta(minutes=6)),
        "calls": 2,
    }
    b = {
        "session_id": "s1",
        "agent_id": "a9",
        "source": "mcp",
        "first_seen": _iso(T0),
        "last_seen": _iso(T0 + timedelta(minutes=9)),
        "calls": 3,
    }
    await _post(client, "sessions", tenant, {"sessions": [a]})
    await _post(client, "sessions", tenant, {"sessions": [b]})
    assert (await _value(client, tenant))["sessions"] == 1


async def test_another_tenants_memory_is_not_reused_here(client: AsyncClient):
    tenant, other = f"t-{uuid.uuid4().hex[:8]}", f"t-{uuid.uuid4().hex[:8]}"
    m = await _memory(client, other, "a1")
    r = await _post(client, "reuse", tenant, {"session_id": "s", "memory_ids": [m], "recalled_at": _iso(T0)})
    assert r["recorded"] == 0


async def test_every_route_needs_a_tenant(client: AsyncClient):
    r = await client.post(
        f"{PREFIX}/value/query", json={"start": _iso(T0), "end": _iso(T0 + timedelta(days=1))}
    )
    assert r.status_code == 422


async def test_recalling_a_memory_older_than_value_metering_is_reuse_but_not_help(client: AsyncClient):
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    old = await _memory(client, tenant, "a1")  # no origin: written before metering
    new = await _memory(client, tenant, "a1")
    await _post(
        client,
        "origins",
        tenant,
        {"memory_id": new, "session_id": "w", "agent_id": "a1", "written_at": _iso(T0)},
    )
    at = _iso(T0 + timedelta(hours=1))
    await _post(
        client,
        "reuse",
        tenant,
        {"session_id": "only-old", "reader_agent_id": "a2", "memory_ids": [old], "recalled_at": at},
    )
    await _post(
        client,
        "reuse",
        tenant,
        {"session_id": "with-new", "reader_agent_id": "a2", "memory_ids": [new], "recalled_at": at},
    )
    t = (await _value(client, tenant))["totals"]
    assert (t["reuses"], t["reuses_with_origin"], t["sessions_helped"]) == (2, 1, 1)


async def test_only_resolved_contradictions_count_as_prevented(client: AsyncClient):
    from sqlalchemy import text

    from core_storage_api.services.postgres_service import get_session

    tenant = f"t-{uuid.uuid4().hex[:8]}"
    a, b = await _memory(client, tenant, "a1"), await _memory(client, tenant, "a1")
    async with get_session() as s:
        for status in ("resolved", "dismissed", "pending", "pending"):
            await s.execute(
                text(
                    "INSERT INTO memory_conflicts (id, tenant_id, new_memory_id, old_memory_id, relationship, review_status, resolved_at, created_at) "
                    "VALUES (gen_random_uuid(), :t, :a, :b, 'exact_value', :st, CASE WHEN :st = 'pending' THEN NULL ELSE CAST(:at AS timestamptz) END, :at)"
                ),
                {"t": tenant, "a": a, "b": b, "st": status, "at": T0},
            )
    p = (await _value(client, tenant))["prevented"]
    assert (p["conflicts_resolved"], p["conflicts_waiting"]) == (1, 2)

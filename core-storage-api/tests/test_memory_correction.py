"""Memory corrections' storage half (migration 073), against a live PostgreSQL.

A correction lists the agents that read the memory before it was corrected
(``memory_exposure.first_seen < corrected_at``), less the correcting agent,
latest read first, at most 50, and counts the rest in ``truncated``. One record
per event, every route bound to one tenant, and the rows go with the memory.
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
CORRECTED_AT = T0 + timedelta(hours=1)


def _embedding(content: str) -> list[float]:
    h = hashlib.sha256(content.encode()).digest()
    return [((h[i % len(h)] / 255.0) - 0.5) for i in range(1024)]


async def _memory(client: AsyncClient, tenant: str) -> str:
    content = f"correction test {tenant} {uuid.uuid4().hex}"
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


async def _read(client: AsyncClient, tenant: str, agent: str, memory_id: str, at: datetime) -> None:
    r = await client.post(
        f"{PREFIX}/memory-exposure",
        params={"tenant_id": tenant},
        json={
            "agent_id": agent,
            "memory_ids": [memory_id],
            "seen_at": at.isoformat(),
            "trace_id": f"tr-{agent}",
        },
    )
    assert r.status_code == 200, r.text


def _body(memory_id: str, **over) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "memory_id": memory_id,
        "kind": "content_edit",
        "actor_kind": "agent",
        "actor_id": "editor",
        "corrected_at": CORRECTED_AT.isoformat(),
        "old_summary": "deploys on Tuesday",
        "new_summary": "deploys on Thursday",
        "detail": {},
        **over,
    }


async def _record(client: AsyncClient, tenant: str, body: dict) -> dict:
    r = await client.post(f"{PREFIX}/memory-corrections", params={"tenant_id": tenant}, json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def _get(client: AsyncClient, tenant: str, correction_id: str):
    return await client.get(f"{PREFIX}/memory-corrections/{correction_id}", params={"tenant_id": tenant})


async def test_readers_before_the_correction_are_listed_less_the_editor(client: AsyncClient):
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    m = await _memory(client, tenant)
    await _read(client, tenant, "reader-a", m, T0)
    await _read(client, tenant, "reader-b", m, T0 + timedelta(minutes=10))
    await _read(client, tenant, "reader-b", m, T0 + timedelta(minutes=20))
    await _read(client, tenant, "editor", m, T0 + timedelta(minutes=5))
    # First read after the correction: it saw the new version only.
    await _read(client, tenant, "late", m, CORRECTED_AT + timedelta(minutes=1))

    out = await _record(client, tenant, _body(m))
    assert out["created"] is True
    assert (out["recipients"], out["truncated"]) == (2, 0)

    r = await _get(client, tenant, out["id"])
    assert r.status_code == 200, r.text
    c = r.json()
    assert c["memory_id"] == m
    assert c["status"] == "preview"
    assert c["actor"] == {"kind": "agent", "id": "editor"}
    assert (c["old_summary"], c["new_summary"]) == ("deploys on Tuesday", "deploys on Thursday")
    # Latest read first.
    assert [x["agent_id"] for x in c["recipients"]] == ["reader-b", "reader-a"]
    b = c["recipients"][0]
    assert (b["reads"], b["notice_state"], b["last_trace_id"]) == (2, "pending", "tr-reader-b")
    assert c["counts"] == {"recipients": 2, "truncated": 0, "readers": 2, "pending": 2}


async def test_a_person_correcting_excludes_no_agent(client: AsyncClient):
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    m = await _memory(client, tenant)
    await _read(client, tenant, "editor", m, T0)
    out = await _record(client, tenant, _body(m, actor_kind="user", actor_id="editor"))
    assert out["recipients"] == 1


async def test_a_redelivered_event_writes_nothing_twice(client: AsyncClient):
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    m = await _memory(client, tenant)
    await _read(client, tenant, "reader-a", m, T0)
    body = _body(m)
    first = await _record(client, tenant, body)
    again = await _record(client, tenant, body)
    assert again == {"id": first["id"], "created": False}

    r = await client.get(f"{PREFIX}/memory-corrections/by-memory/{m}", params={"tenant_id": tenant})
    assert r.status_code == 200, r.text
    [only] = r.json()["corrections"]
    assert only["id"] == first["id"]
    assert only["counts"]["recipients"] == 1


async def test_at_most_fifty_recipients_and_the_rest_are_counted(client: AsyncClient):
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    m = await _memory(client, tenant)
    for i in range(53):
        await _read(client, tenant, f"agent-{i:02d}", m, T0 + timedelta(seconds=i))
    out = await _record(client, tenant, _body(m))
    assert (out["recipients"], out["truncated"]) == (50, 3)
    c = (await _get(client, tenant, out["id"])).json()
    assert len(c["recipients"]) == 50
    # The three earliest readers are the ones past the cap.
    assert "agent-02" not in {x["agent_id"] for x in c["recipients"]}
    assert c["recipients"][0]["agent_id"] == "agent-52"
    assert c["counts"] == {"recipients": 50, "truncated": 3, "readers": 53, "pending": 50}


async def test_a_correction_is_bound_to_its_tenant(client: AsyncClient):
    tenant, other = f"t-{uuid.uuid4().hex[:8]}", f"t-{uuid.uuid4().hex[:8]}"
    m = await _memory(client, tenant)
    await _read(client, tenant, "reader-a", m, T0)
    out = await _record(client, tenant, _body(m))

    assert (await _get(client, other, out["id"])).status_code == 404
    r = await client.get(f"{PREFIX}/memory-corrections/by-memory/{m}", params={"tenant_id": other})
    assert r.json() == {"corrections": []}
    # Another tenant's event about this memory records nothing.
    assert await _record(client, other, _body(m)) == {"id": None, "created": False}


async def test_a_memory_that_is_gone_records_nothing(client: AsyncClient):
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    assert await _record(client, tenant, _body(str(uuid.uuid4()))) == {"id": None, "created": False}


async def test_rows_go_with_the_memory(client: AsyncClient):
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    m = await _memory(client, tenant)
    await _read(client, tenant, "reader-a", m, T0)
    out = await _record(client, tenant, _body(m))
    async with get_session() as session:
        await session.execute(text("DELETE FROM memories WHERE id = :id"), {"id": m})
    async with get_session() as session:
        left = await session.execute(
            text(
                "SELECT (SELECT count(*) FROM memory_correction WHERE id = :c)"
                " + (SELECT count(*) FROM memory_correction_recipient WHERE correction_id = :c)"
            ),
            {"c": out["id"]},
        )
        assert left.scalar_one() == 0


async def test_the_body_is_validated(client: AsyncClient):
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    url = f"{PREFIX}/memory-corrections"
    ok = _body(str(uuid.uuid4()))
    assert (
        await client.post(url, params={"tenant_id": tenant}, json={**ok, "kind": "auto"})
    ).status_code == 422
    assert (
        await client.post(url, params={"tenant_id": tenant}, json={**ok, "old_summary": "x" * 281})
    ).status_code == 422
    assert (await client.post(url, params={"tenant_id": ""}, json=ok)).status_code == 422

"""Correction notices on recall (CP-13), the storage half, against a live PostgreSQL.

``GET /memory-corrections/notices`` lists what one agent still has to hear
about: it is a recipient of a sent correction (past ``preview``) younger than 7 days, has not
confirmed, and the bus is not carrying the notice (``channel=recall``, or the
bus state is ``failed`` / ``unreachable``). Newest first, at most ``limit``,
bound to the tenant. Sending (CP-11) is another PR, so the rows are set here
directly.
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


def _embedding(content: str) -> list[float]:
    h = hashlib.sha256(content.encode()).digest()
    return [((h[i % len(h)] / 255.0) - 0.5) for i in range(1024)]


async def _memory(client: AsyncClient, tenant: str) -> str:
    content = f"notice test {tenant} {uuid.uuid4().hex}"
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


async def _correction(
    client: AsyncClient, tenant: str, readers: list[str], *, ago: timedelta = timedelta(hours=1)
) -> str:
    """A correction of a fresh memory, ``ago`` in the past, read first by ``readers``."""
    m = await _memory(client, tenant)
    corrected_at = datetime.now(UTC) - ago
    for agent in readers:
        r = await client.post(
            f"{PREFIX}/memory-exposure",
            params={"tenant_id": tenant},
            json={
                "agent_id": agent,
                "memory_ids": [m],
                "seen_at": (corrected_at - timedelta(minutes=5)).isoformat(),
                "trace_id": None,
            },
        )
        assert r.status_code == 200, r.text
    r = await client.post(
        f"{PREFIX}/memory-corrections",
        params={"tenant_id": tenant},
        json={
            "event_id": str(uuid.uuid4()),
            "memory_id": m,
            "kind": "content_edit",
            "actor_kind": "user",
            "actor_id": "person-1",
            "corrected_at": corrected_at.isoformat(),
            "old_summary": "deploys on Tuesday",
            "new_summary": "deploys on Thursday",
            "detail": {},
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _set(correction_id: str, *, status: str = "sent", **recipient: str) -> None:
    async with get_session() as session:
        await session.execute(
            text("UPDATE memory_correction SET status = :s WHERE id = :id"),
            {"s": status, "id": correction_id},
        )
        if recipient:
            sets = ", ".join(f"{k} = :{k}" for k in recipient)
            await session.execute(
                text(f"UPDATE memory_correction_recipient SET {sets} WHERE correction_id = :id"),
                {**recipient, "id": correction_id},
            )


async def _notices(client: AsyncClient, tenant: str, agent: str, **params) -> list[dict]:
    r = await client.get(
        f"{PREFIX}/memory-corrections/notices", params={"tenant_id": tenant, "agent_id": agent, **params}
    )
    assert r.status_code == 200, r.text
    return r.json()["notices"]


async def test_a_recall_channel_recipient_sees_its_notice_and_no_one_else_does(client: AsyncClient):
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    cid = await _correction(client, tenant, ["a", "b"])
    await _set(cid, channel="recall", notice_state="sent")

    for agent in ("a", "b"):
        (n,) = await _notices(client, tenant, agent)
        assert n["correction_id"] == cid
        assert (n["old_summary"], n["new_summary"]) == ("deploys on Tuesday", "deploys on Thursday")
        assert set(n) == {"correction_id", "memory_id", "old_summary", "new_summary", "corrected_at"}
    assert await _notices(client, tenant, "not-a-reader") == []
    # Another tenant's agent of the same name sees nothing.
    assert await _notices(client, f"t-{uuid.uuid4().hex[:8]}", "a") == []


async def test_confirmed_preview_bus_and_old_corrections_are_not_notices(client: AsyncClient):
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    confirmed = await _correction(client, tenant, ["a"])
    await _set(confirmed, channel="recall", notice_state="confirmed")
    preview = await _correction(client, tenant, ["a"])
    await _set(preview, status="preview", channel="recall")
    on_the_bus = await _correction(client, tenant, ["a"])
    await _set(on_the_bus, channel="bus", notice_state="delivered")
    stale = await _correction(client, tenant, ["a"], ago=timedelta(days=8))
    await _set(stale, channel="recall", notice_state="sent")

    assert await _notices(client, tenant, "a") == []


async def test_a_bus_notice_that_failed_falls_back_to_recall(client: AsyncClient):
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    failed = await _correction(client, tenant, ["a"], ago=timedelta(hours=2))
    await _set(failed, channel="bus", notice_state="failed")
    unreachable = await _correction(client, tenant, ["a"], ago=timedelta(hours=1))
    await _set(unreachable, channel="bus", notice_state="unreachable")

    assert [n["correction_id"] for n in await _notices(client, tenant, "a")] == [unreachable, failed]


async def test_newest_first_and_capped(client: AsyncClient):
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    ids = []
    for hours in (5, 4, 3, 2, 1):
        cid = await _correction(client, tenant, ["a"], ago=timedelta(hours=hours))
        await _set(cid, channel="recall", notice_state="sent")
        ids.append(cid)

    assert [n["correction_id"] for n in await _notices(client, tenant, "a")] == ids[::-1][:3]
    assert len(await _notices(client, tenant, "a", limit=10)) == 5


async def test_the_lookup_uses_the_agent_index(client: AsyncClient):
    async with get_session() as session:
        indexdef = (
            await session.execute(
                text(
                    "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_memory_correction_recipient_agent'"
                )
            )
        ).scalar_one()
    assert "(tenant_id, agent_id)" in indexdef


async def test_a_correction_still_sending_or_closed_still_owes_its_recall_notices(client: AsyncClient):
    """``sending``: a peer is throttled on the bus, the recall channel is already set.
    ``closed``: every recipient confirmed or failed, and a failed one is owed this."""
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    sending = await _correction(client, tenant, ["a"], ago=timedelta(hours=2))
    await _set(sending, status="sending", channel="recall", notice_state="pending")
    closed = await _correction(client, tenant, ["a"], ago=timedelta(hours=1))
    await _set(closed, status="closed", channel="bus", notice_state="failed")

    assert [n["correction_id"] for n in await _notices(client, tenant, "a")] == [closed, sending]

"""Handoff baton notices (HB-09), the storage half, against a live PostgreSQL.

The bus records a baton's receivers (``POST /batons/{id}/recipients``); a
recall lists the ones still waiting on an agent (``GET /batons/notices``):
``delivered``, younger than 7 days, newest first, at most ``limit``, bound to
the tenant. ``/accept`` takes one up (from the receiver or the bus), and
``/notices/checked`` records when core-api last read the bus, and the bus's
status read lists a baton's rows.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text

from common.models.baton_recipient import BatonRecipient
from core_storage_api.services.postgres_service import get_session
from tests.conftest import plan_with_only_index

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

PREFIX = "/api/v1/storage"


async def _baton(
    client: AsyncClient,
    tenant: str,
    receivers: list[str],
    *,
    ago: timedelta = timedelta(hours=1),
    goal: str = "Finish the csv-diff edge cases",
) -> str:
    baton_id = str(uuid.uuid4())
    r = await client.post(
        f"{PREFIX}/batons/{baton_id}/recipients",
        params={"tenant_id": tenant},
        json={
            "from_agent": "sender",
            "goal": goal,
            "created_at": (datetime.now(UTC) - ago).isoformat(),
            "recipients": [{"agent_id": a, "message_id": f"msg-{a}"} for a in receivers],
        },
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"recorded": len(receivers)}
    return baton_id


async def _notices(client: AsyncClient, tenant: str, agent: str, limit: int = 2) -> list[dict]:
    r = await client.get(
        f"{PREFIX}/batons/notices", params={"tenant_id": tenant, "agent_id": agent, "limit": limit}
    )
    assert r.status_code == 200, r.text
    return r.json()["notices"]


async def _accept(client: AsyncClient, tenant: str, baton_id: str, agent: str, **body) -> tuple[int, dict]:
    r = await client.post(
        f"{PREFIX}/batons/{baton_id}/accept",
        params={"tenant_id": tenant},
        json={"agent_id": agent, **body},
    )
    return r.status_code, r.json()


async def test_a_receiver_sees_its_baton_and_no_one_else_does(client: AsyncClient):
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    baton_id = await _baton(client, tenant, ["a", "b"])

    [notice] = await _notices(client, tenant, "a")
    assert notice["baton_id"] == baton_id
    assert notice["from_agent"] == "sender"
    assert notice["goal"] == "Finish the csv-diff edge cases"
    assert notice["message_id"] == "msg-a"
    assert notice["bus_checked_at"] is None
    assert [n["baton_id"] for n in await _notices(client, tenant, "b")] == [baton_id]
    assert await _notices(client, tenant, "sender") == []
    assert await _notices(client, tenant, "c") == []
    assert await _notices(client, f"other-{tenant}", "a") == []


async def test_a_retried_send_keeps_the_first_rows_and_their_state(client: AsyncClient):
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    baton_id = await _baton(client, tenant, ["a"])
    assert (await _accept(client, tenant, baton_id, "a"))[0] == 200
    r = await client.post(
        f"{PREFIX}/batons/{baton_id}/recipients",
        params={"tenant_id": tenant},
        json={
            "from_agent": "sender",
            "goal": "retried",
            "created_at": datetime.now(UTC).isoformat(),
            "recipients": [{"agent_id": "a", "message_id": "msg-a"}],
        },
    )
    assert r.json() == {"recorded": 0}
    assert await _notices(client, tenant, "a") == []


async def test_accept_stops_the_notice_for_that_receiver_only(client: AsyncClient):
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    baton_id = await _baton(client, tenant, ["a", "b"])

    status, row = await _accept(client, tenant, baton_id, "a")
    assert status == 200
    assert row["state"] == "accepted" and row["accepted_at"] and row["from_agent"] == "sender"
    assert await _notices(client, tenant, "a") == []
    assert len(await _notices(client, tenant, "b")) == 1

    # Idempotent: the first accept stands, and a later bus reply does not move it.
    first = row["accepted_at"]
    status, again = await _accept(client, tenant, baton_id, "a", via="bus", state="replied")
    assert status == 200 and again["accepted_at"] == first and again["state"] == "accepted"
    async with get_session() as session:
        via = await session.scalar(
            select(BatonRecipient.accepted_via).where(
                BatonRecipient.tenant_id == tenant, BatonRecipient.agent_id == "a"
            )
        )
    assert via == "recall"


async def test_a_bus_reply_is_recorded_as_replied(client: AsyncClient):
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    baton_id = await _baton(client, tenant, ["a"])
    status, row = await _accept(client, tenant, baton_id, "a", via="bus", state="replied")
    assert status == 200 and row["state"] == "replied"
    assert await _notices(client, tenant, "a") == []


async def test_a_non_receiver_or_another_tenant_cannot_accept(client: AsyncClient):
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    baton_id = await _baton(client, tenant, ["a"])
    assert (await _accept(client, tenant, baton_id, "c"))[0] == 404
    assert (await _accept(client, tenant, baton_id, "sender"))[0] == 404
    assert (await _accept(client, f"other-{tenant}", baton_id, "a"))[0] == 404
    assert (await _accept(client, tenant, str(uuid.uuid4()), "a"))[0] == 404
    assert len(await _notices(client, tenant, "a")) == 1


async def test_a_baton_older_than_seven_days_is_not_shown(client: AsyncClient):
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    await _baton(client, tenant, ["a"], ago=timedelta(days=8))
    fresh = await _baton(client, tenant, ["a"], ago=timedelta(days=6))
    assert [n["baton_id"] for n in await _notices(client, tenant, "a")] == [fresh]


async def test_newest_first_and_capped(client: AsyncClient):
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    ids = [await _baton(client, tenant, ["a"], ago=timedelta(hours=h)) for h in (4, 3, 2, 1)]
    assert [n["baton_id"] for n in await _notices(client, tenant, "a")] == ids[::-1][:2]
    assert len(await _notices(client, tenant, "a", limit=10)) == 4


async def test_checked_records_when_the_bus_was_read(client: AsyncClient):
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    baton_id = await _baton(client, tenant, ["a", "b"])
    r = await client.post(
        f"{PREFIX}/batons/notices/checked",
        params={"tenant_id": tenant},
        json={"agent_id": "a", "baton_ids": [baton_id]},
    )
    assert r.json() == {"checked": 1}
    [mine] = await _notices(client, tenant, "a")
    assert mine["bus_checked_at"] is not None
    [theirs] = await _notices(client, tenant, "b")
    assert theirs["bus_checked_at"] is None


async def test_the_receivers_must_be_named(client: AsyncClient):
    r = await client.post(
        f"{PREFIX}/batons/{uuid.uuid4()}/recipients",
        params={"tenant_id": "t"},
        json={"from_agent": "s", "goal": "g", "created_at": datetime.now(UTC).isoformat(), "recipients": []},
    )
    assert r.status_code == 422


async def test_the_lookup_can_use_the_waiting_index(client: AsyncClient):
    r = BatonRecipient
    stmt = select(r.baton_id).where(r.tenant_id == "t", r.agent_id == "a", r.state == "delivered")
    plan = await plan_with_only_index(stmt, "ix_baton_recipient_waiting", table="baton_recipient")
    assert "ix_baton_recipient_waiting" in plan, plan
    async with get_session() as session:
        indexdef = (
            await session.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_baton_recipient_waiting'")
            )
        ).scalar_one()
    assert "WHERE (state = 'delivered'::text)" in indexdef


async def test_the_bus_status_lists_a_batons_rows(client: AsyncClient):
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    baton_id = await _baton(client, tenant, ["b", "a"])
    await _accept(client, tenant, baton_id, "a")

    r = await client.get(f"{PREFIX}/batons/{baton_id}/recipients", params={"tenant_id": tenant})
    assert r.status_code == 200, r.text
    rows = r.json()["recipients"]
    assert [(x["agent_id"], x["state"], x["accepted_via"]) for x in rows] == [
        ("a", "accepted", "recall"),
        ("b", "delivered", None),
    ]
    assert rows[0]["accepted_at"] and rows[1]["accepted_at"] is None
    other = await client.get(f"{PREFIX}/batons/{baton_id}/recipients", params={"tenant_id": f"o-{tenant}"})
    assert other.json() == {"recipients": []}

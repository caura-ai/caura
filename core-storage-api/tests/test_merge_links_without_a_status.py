"""The near-duplicate merge's link writes the link and nothing else (audit 2026-10-01, B33, L-19).

``POST /memories/{id}/supersedes`` points a live row at the memory it supersedes when the row points
nowhere yet, and answers whether it did. The merge used to link through ``PATCH /memories/{id}/status``,
which also wrote the status ``active`` over the one the writer chose, and answered ``ok`` whether or not
the link landed.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from core_storage_api.database.init import get_engine
from tests.test_integration import PREFIX, _memory_payload

pytestmark = [pytest.mark.integration]


def _tenant() -> str:
    """A fresh tenant, under the prefix the end-of-run sweep removes."""
    return f"test-tenant-{uuid.uuid4().hex[:8]}"


async def _memory(client: AsyncClient, tenant: str, **fields: str) -> str:
    payload = {**_memory_payload(tenant, "f1"), **fields}
    resp = await client.post(f"{PREFIX}/memories", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _row(memory_id: str) -> dict:
    async with get_engine().connect() as conn:
        result = await conn.execute(
            text("SELECT status, status_changed_at, supersedes_id FROM memories WHERE id = :id"),
            {"id": uuid.UUID(memory_id)},
        )
        return dict(result.mappings().one())


async def _link(client: AsyncClient, tenant: str, memory_id: str, supersedes_id: str) -> dict:
    resp = await client.post(
        f"{PREFIX}/memories/{memory_id}/supersedes",
        json={"tenant_id": tenant, "supersedes_id": supersedes_id},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_l19_a_link_leaves_the_status_its_writer_chose(client: AsyncClient) -> None:
    tenant = _tenant()
    old = await _memory(client, tenant)
    new = await _memory(client, tenant, status="confirmed")
    before = await _row(new)

    assert await _link(client, tenant, new, old) == {"updated": True}

    after = await _row(new)
    assert str(after["supersedes_id"]) == old
    assert after["status"] == "confirmed"
    assert after["status_changed_at"] == before["status_changed_at"]


async def test_l19_a_row_that_already_supersedes_another_keeps_its_link(client: AsyncClient) -> None:
    """The first writer owns the chain: a contradiction verdict that linked the row first wins."""
    tenant = _tenant()
    first = await _memory(client, tenant)
    second = await _memory(client, tenant)
    new = await _memory(client, tenant)

    assert await _link(client, tenant, new, first) == {"updated": True}
    assert await _link(client, tenant, new, second) == {"updated": False}

    assert str((await _row(new))["supersedes_id"]) == first


async def test_l19_a_held_row_is_not_linked(client: AsyncClient) -> None:
    """Only a person's release moves a held memory, and the release replays the merge."""
    tenant = _tenant()
    old = await _memory(client, tenant)
    held = await _memory(client, tenant)
    async with get_engine().begin() as conn:
        await conn.execute(
            text("UPDATE memories SET status = 'quarantined' WHERE id = :id"), {"id": uuid.UUID(held)}
        )

    assert await _link(client, tenant, held, old) == {"updated": False}

    assert (await _row(held))["supersedes_id"] is None


async def test_l19_a_target_in_another_tenant_is_not_linked(client: AsyncClient) -> None:
    tenant = _tenant()
    foreign = await _memory(client, _tenant())
    new = await _memory(client, tenant)

    assert await _link(client, tenant, new, foreign) == {"updated": False}

    assert (await _row(new))["supersedes_id"] is None


@pytest.mark.parametrize(
    "body",
    [{"supersedes_id": str(uuid.uuid4())}, {"tenant_id": "t"}, {"tenant_id": "t", "supersedes_id": "nope"}],
    ids=["no tenant", "no target", "target not a UUID"],
)
async def test_l19_a_link_names_a_tenant_and_a_target(client: AsyncClient, body: dict) -> None:
    resp = await client.post(f"{PREFIX}/memories/{uuid.uuid4()}/supersedes", json=body)

    assert resp.status_code == 422, resp.text

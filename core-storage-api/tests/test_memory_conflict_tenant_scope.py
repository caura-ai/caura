"""Tenant binding for ``POST /memories/conflicts``.

``memory_conflicts.{new,old}_memory_id`` are FKs to ``memories.id`` with no
cross-column tenant constraint, so before the route guard an insert naming
another tenant's memory satisfied the FK and landed — and an unknown UUID
raised ForeignKeyViolationError as an unhandled 500 while a real one returned
200, which made the route an existence oracle for memory ids.

Both cases must now be an indistinguishable 404, the same way
``POST /fleet/commands`` treats a foreign node id.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tests.test_integration import PREFIX, _memory_payload

pytestmark = pytest.mark.asyncio


def _conflict_payload(tenant_id: str, new_memory_id: str, old_memory_id: str) -> dict:
    return {
        "tenant_id": tenant_id,
        "new_memory_id": new_memory_id,
        "old_memory_id": old_memory_id,
        "relationship": "exact_value",
        "diagnosis": "temporal_change",
        "evidence_strength": "explicit",
        "action": "supersede",
    }


async def _make_memory(client: AsyncClient, tenant_id: str, fleet_id: str | None) -> str:
    response = await client.post(f"{PREFIX}/memories", json=_memory_payload(tenant_id, fleet_id))
    assert response.status_code == 200, response.text
    return response.json()["id"]


async def test_foreign_memory_id_is_404_not_a_recorded_conflict(
    client: AsyncClient, tenant_id: str, fleet_id: str
) -> None:
    """A memory owned by another tenant must not be referenceable."""
    mine = await _make_memory(client, tenant_id, fleet_id)
    theirs = await _make_memory(client, f"other-{uuid.uuid4()}", None)

    response = await client.post(
        f"{PREFIX}/memories/conflicts",
        json=_conflict_payload(tenant_id, mine, theirs),
    )

    assert response.status_code == 404, response.text


async def test_foreign_memory_id_in_the_new_slot_is_also_404(
    client: AsyncClient, tenant_id: str, fleet_id: str
) -> None:
    """Both id slots are checked, not just ``old_memory_id``."""
    mine = await _make_memory(client, tenant_id, fleet_id)
    theirs = await _make_memory(client, f"other-{uuid.uuid4()}", None)

    response = await client.post(
        f"{PREFIX}/memories/conflicts",
        json=_conflict_payload(tenant_id, theirs, mine),
    )

    assert response.status_code == 404, response.text


async def test_unknown_memory_id_is_404_not_a_500(client: AsyncClient, tenant_id: str, fleet_id: str) -> None:
    """An id that exists nowhere must not be distinguishable from one that
    exists in another tenant — before the guard this was an unhandled 500 from
    the FK violation, which is what made the route an existence oracle."""
    mine = await _make_memory(client, tenant_id, fleet_id)

    response = await client.post(
        f"{PREFIX}/memories/conflicts",
        json=_conflict_payload(tenant_id, mine, str(uuid.uuid4())),
    )

    assert response.status_code == 404, response.text


async def test_unknown_and_foreign_are_indistinguishable(
    client: AsyncClient, tenant_id: str, fleet_id: str
) -> None:
    """The oracle is closed only if the two answers are byte-identical."""
    mine = await _make_memory(client, tenant_id, fleet_id)
    theirs = await _make_memory(client, f"other-{uuid.uuid4()}", None)

    foreign = await client.post(
        f"{PREFIX}/memories/conflicts",
        json=_conflict_payload(tenant_id, mine, theirs),
    )
    unknown = await client.post(
        f"{PREFIX}/memories/conflicts",
        json=_conflict_payload(tenant_id, mine, str(uuid.uuid4())),
    )

    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json() == unknown.json()


async def test_same_tenant_pair_still_records(client: AsyncClient, tenant_id: str, fleet_id: str) -> None:
    """The guard must not break the legitimate path."""
    a = await _make_memory(client, tenant_id, fleet_id)
    b = await _make_memory(client, tenant_id, fleet_id)

    response = await client.post(
        f"{PREFIX}/memories/conflicts",
        json=_conflict_payload(tenant_id, a, b),
    )

    assert response.status_code == 200, response.text
    row = response.json()
    assert row["new_memory_id"] == a
    assert row["old_memory_id"] == b


async def test_missing_memory_id_is_422_not_a_500(client: AsyncClient, tenant_id: str, fleet_id: str) -> None:
    """A missing id used to reach ``payload["new_memory_id"]`` and raise
    KeyError as an unhandled 500; the fail-closed guard makes it a 422."""
    mine = await _make_memory(client, tenant_id, fleet_id)

    response = await client.post(
        f"{PREFIX}/memories/conflicts",
        json={
            "tenant_id": tenant_id,
            "old_memory_id": mine,
            "relationship": "exact_value",
        },
    )

    assert response.status_code == 422, response.text

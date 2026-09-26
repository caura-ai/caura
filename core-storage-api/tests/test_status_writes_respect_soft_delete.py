"""OSS audit 08/14 L-18 — status writes reached soft-deleted memories.

``memory_update_status`` scoped its UPDATE to id + tenant with no
``deleted_at`` guard, so a soft-deleted row matched, was rewritten, and
reported success. Its sibling ``memory_update`` has always guarded, and so do
the read paths — a deleted memory is gone as far as every one of them is
concerned.

The route made the contradiction explicit rather than implied. Above the call
it states:

    ``memory_update_status`` returns False when the target row doesn't exist
    (or was already deleted); surface as 404 so the caller doesn't silently
    treat a no-op as success.

It did not. A deleted row matched, updated, returned True, and the route
answered 200 for a write its own comment says is impossible — while rewriting
the ``status`` and ``supersedes_id`` of a memory nothing can read back, which
is the lineage the contradiction detector reasons over.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from core_storage_api.services.postgres_service import get_session

pytestmark = pytest.mark.asyncio

_P = "/api/v1/storage"


async def _seed_memory(tenant: str, *, deleted: bool) -> uuid.UUID:
    memory_id = uuid.uuid4()
    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO memories "
                "(id, tenant_id, agent_id, memory_type, content, status, deleted_at) "
                "VALUES (:id, :t, 'l18-probe', 'semantic', 'probe', 'active', "
                "CASE WHEN :deleted THEN now() ELSE NULL END)"
            ),
            {"id": memory_id, "t": tenant, "deleted": deleted},
        )
    return memory_id


async def _row(memory_id: uuid.UUID) -> tuple[str, uuid.UUID | None]:
    async with get_session() as session:
        result = await session.execute(
            text("SELECT status, supersedes_id FROM memories WHERE id = :id"),
            {"id": memory_id},
        )
        return result.one()


async def test_a_soft_deleted_memory_cannot_have_its_status_rewritten(client) -> None:
    tenant = f"t-l18-{uuid.uuid4().hex[:8]}"
    memory_id = await _seed_memory(tenant, deleted=True)

    response = await client.patch(
        f"{_P}/memories/{memory_id}/status",
        json={"tenant_id": tenant, "status": "outdated"},
    )

    assert response.status_code == 404, (
        "the route documents a 404 when the row 'was already deleted'; "
        f"got {response.status_code}: {response.text}"
    )
    status, _ = await _row(memory_id)
    assert status == "active", f"the status of a soft-deleted memory was rewritten to {status!r}"


async def test_a_soft_deleted_memory_cannot_have_its_lineage_repointed(client) -> None:
    """The ``supersedes_id`` half, which is the one that outlives the row.

    Status is a property of the deleted memory alone. ``supersedes_id`` is an
    edge into a memory that is still live, so writing it here changes what the
    contradiction detector reads about a row nobody deleted.
    """
    tenant = f"t-l18-{uuid.uuid4().hex[:8]}"
    deleted_id = await _seed_memory(tenant, deleted=True)
    live_id = await _seed_memory(tenant, deleted=False)

    response = await client.patch(
        f"{_P}/memories/{deleted_id}/status",
        json={
            "tenant_id": tenant,
            "status": "active",
            "supersedes_id": str(live_id),
        },
    )

    assert response.status_code == 404, response.text
    _, supersedes = await _row(deleted_id)
    assert supersedes is None, f"a soft-deleted memory was pointed at a live one: supersedes_id={supersedes}"


async def test_a_retraction_on_a_deleted_memory_is_a_404_not_a_conflict(client) -> None:
    """The retraction path writes its own SQL, so it needed its own guard.

    It must also answer honestly. Zero rows is reached three ways — missing,
    deleted, or genuinely taken by another writer — and only the last is a
    conflict the caller can fix by re-reading. Reporting ``stale_retraction``
    for a deleted row would send them to re-read a row that is not there.
    """
    tenant = f"t-l18-{uuid.uuid4().hex[:8]}"
    memory_id = await _seed_memory(tenant, deleted=True)

    response = await client.patch(
        f"{_P}/memories/{memory_id}/status",
        json={
            "tenant_id": tenant,
            "status": "active",
            "unset_supersedes": True,
            "expected_supersedes_id": str(uuid.uuid4()),
        },
    )

    assert response.status_code == 404, (
        f"expected 404 for a deleted row, got {response.status_code}: {response.text}"
    )


async def test_a_live_memory_still_updates(client) -> None:
    """The guard must not cost the ordinary path — without this, 'deleted_at
    IS NOT NULL' would pass every test above."""
    tenant = f"t-l18-{uuid.uuid4().hex[:8]}"
    memory_id = await _seed_memory(tenant, deleted=False)

    response = await client.patch(
        f"{_P}/memories/{memory_id}/status",
        json={"tenant_id": tenant, "status": "outdated"},
    )

    assert response.status_code == 200, response.text
    status, _ = await _row(memory_id)
    assert status == "outdated", f"a live memory was not updated: {status!r}"


async def test_a_stale_retraction_on_a_live_memory_is_still_a_conflict(client) -> None:
    """The other half of the classification: a live row whose pointer moved on
    must still get 409, or the fix would have traded a wrong 409 for a wrong
    404 and lost the signal telling the caller to re-read."""
    tenant = f"t-l18-{uuid.uuid4().hex[:8]}"
    target_id = await _seed_memory(tenant, deleted=False)
    other_id = await _seed_memory(tenant, deleted=False)

    async with get_session() as session:
        await session.execute(
            text("UPDATE memories SET supersedes_id = :s WHERE id = :id"),
            {"s": other_id, "id": target_id},
        )

    response = await client.patch(
        f"{_P}/memories/{target_id}/status",
        json={
            "tenant_id": tenant,
            "status": "active",
            "unset_supersedes": True,
            "expected_supersedes_id": str(uuid.uuid4()),
        },
    )

    assert response.status_code == 409, (
        f"a live row with a moved pointer must still report a conflict, got "
        f"{response.status_code}: {response.text}"
    )
    assert response.json()["detail"]["error"] == "stale_retraction"

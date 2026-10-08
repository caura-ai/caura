"""What was held in a window, and what people decided in it (g4.3).

The write gate's part of a tenant's weekly pilot report. Held memories are
counted by why they were held, whatever became of them since: a release or
reject leaves the hold in ``_system``. The decisions come from the audit log:
releases, rejects, and the held memories a session rollback rejected.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from common.constants import QUARANTINED_MEMORY_STATUS
from core_storage_api.services.postgres_service import get_session

pytestmark = pytest.mark.asyncio

_P = "/api/v1/storage"
_EMBEDDING = [0.1] * 1024
_SINCE = datetime(2026, 10, 5, tzinfo=UTC)
_UNTIL = _SINCE + timedelta(days=7)
_HELD = QUARANTINED_MEMORY_STATUS


def _tenant() -> str:
    return f"t-held-counts-{uuid.uuid4().hex[:8]}"


async def _memory(
    tenant: str,
    at: datetime,
    *,
    reason: str | None = None,
    status: str = "active",
    parent: str | None = None,
    deleted: bool = False,
) -> str:
    """A memory written at ``at``; held for ``reason`` if one is given."""
    memory_id = uuid.uuid4()
    metadata: dict = {}
    if reason is not None:
        metadata["_system"] = {"hold": {"reason": reason}}
    if parent is not None:
        metadata["parent_memory_id"] = parent
    async with get_session() as db:
        await db.execute(
            text(
                "INSERT INTO memories (id, tenant_id, fleet_id, agent_id, memory_type, content, "
                "status, embedding, weight, visibility, metadata, created_at, deleted_at) "
                "VALUES (:id, :t, 'fleet-c', 'agent-c', 'fact', :c, :s, CAST(:e AS vector), 0.5, "
                "'scope_team', :m, :at, :gone)"
            ),
            {
                "id": memory_id,
                "t": tenant,
                "c": f"memory {memory_id}",
                "s": status,
                "e": str(_EMBEDDING),
                "m": json.dumps(metadata),
                "at": at,
                "gone": at if deleted else None,
            },
        )
    return str(memory_id)


async def _audit(tenant: str, action: str, at: datetime, memory: str | None = None, **detail: str) -> None:
    """An audit row on ``memory``, or on a memory that isn't there."""
    async with get_session() as db:
        await db.execute(
            text(
                "INSERT INTO audit_log (tenant_id, action, resource_type, resource_id, detail, created_at) "
                "VALUES (:t, :a, 'memory', :r, CAST(:d AS json), :at)"
            ),
            {
                "t": tenant,
                "a": action,
                "r": uuid.UUID(memory) if memory else uuid.uuid4(),
                "d": json.dumps(detail),
                "at": at,
            },
        )


async def _counts(client, tenant: str, since: datetime = _SINCE, until: datetime = _UNTIL) -> dict:
    response = await client.get(
        f"{_P}/memories/held/counts",
        params={"tenant_id": tenant, "since": since.isoformat(), "until": until.isoformat()},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_held_counts_the_windows_held_writes_by_reason_whatever_became_of_them(client) -> None:
    tenant = _tenant()
    await _memory(tenant, _SINCE, reason="below_trust", status=_HELD)  # still held, at the start
    await _memory(tenant, _SINCE + timedelta(days=1), reason="write_gate")  # released
    rejected = await _memory(
        tenant, _SINCE + timedelta(days=2), reason="write_gate", status="cancelled", deleted=True
    )
    # Its auto-chunk carries the hold too, and moves with it.
    await _memory(
        tenant,
        _SINCE + timedelta(days=2),
        reason="write_gate",
        status="cancelled",
        parent=rejected,
        deleted=True,
    )
    await _memory(tenant, _SINCE + timedelta(days=3))  # never held
    await _memory(tenant, _SINCE - timedelta(microseconds=1), reason="below_trust", status=_HELD)
    await _memory(tenant, _UNTIL, reason="below_trust", status=_HELD)
    await _memory(_tenant(), _SINCE + timedelta(days=1), reason="write_gate", status=_HELD)

    counts = await _counts(client, tenant)

    assert counts["held"] == {"below_trust": 1, "write_gate": 2}


async def test_the_decisions_are_releases_rejects_and_the_held_writes_a_rollback_rejected(client) -> None:
    tenant = _tenant()
    for day in (0, 3):
        await _audit(tenant, "quarantine.release", _SINCE + timedelta(days=day))
    await _audit(tenant, "quarantine.reject", _SINCE + timedelta(days=4))
    await _audit(tenant, "session.rollback", _SINCE + timedelta(days=5), new_status="cancelled")
    # A rollback's live writes and what it restored are not held writes.
    await _audit(tenant, "session.rollback", _SINCE + timedelta(days=5), new_status="outdated")
    await _audit(tenant, "session.rollback", _SINCE + timedelta(days=5), new_status="active")
    await _audit(tenant, "status_update", _SINCE + timedelta(days=5), new_status="cancelled")
    await _audit(tenant, "quarantine.release", _SINCE - timedelta(microseconds=1))
    await _audit(tenant, "quarantine.reject", _UNTIL)
    await _audit(_tenant(), "quarantine.release", _SINCE + timedelta(days=1))

    counts = await _counts(client, tenant)

    assert (counts["released"], counts["rejected"], counts["rolled_back"]) == (2, 1, 1)


async def test_a_rolled_back_write_counts_once_however_many_chunks_went_with_it(client) -> None:
    """A rollback audits each held memory it rejects, the write's auto-chunks
    included, and soft-deletes them all."""
    tenant = _tenant()
    at = _SINCE + timedelta(days=1)
    write = await _memory(tenant, at, reason="write_gate", status="cancelled", deleted=True)
    chunks = [
        await _memory(tenant, at, reason="write_gate", status="cancelled", parent=write, deleted=True)
        for _ in range(2)
    ]
    for memory in (write, *chunks):
        await _audit(tenant, "session.rollback", at, memory=memory, new_status="cancelled")
    # The same chunk ids in another tenant say nothing about this one's rows.
    other = _tenant()
    for memory in chunks:
        await _audit(other, "session.rollback", at, memory=memory, new_status="cancelled")

    assert (await _counts(client, tenant))["rolled_back"] == 1
    assert (await _counts(client, other))["rolled_back"] == 2


async def test_a_quiet_window_counts_nothing(client) -> None:
    tenant = _tenant()
    await _memory(tenant, _SINCE + timedelta(days=1))
    await _audit(tenant, "memory.create", _SINCE + timedelta(days=1))

    assert await _counts(client, tenant) == {"held": {}, "released": 0, "rejected": 0, "rolled_back": 0}


@pytest.mark.parametrize(
    ("since", "until"),
    [
        (_SINCE.isoformat(), _SINCE.isoformat()),  # empty
        (_UNTIL.isoformat(), _SINCE.isoformat()),  # backwards
        ("2026-10-05T00:00:00", _UNTIL.isoformat()),  # no offset
        (_SINCE.isoformat(), "2026-10-12T00:00:00"),
    ],
)
async def test_the_counts_refuse_a_window_they_cannot_place(client, since: str, until: str) -> None:
    response = await client.get(
        f"{_P}/memories/held/counts", params={"tenant_id": _tenant(), "since": since, "until": until}
    )

    assert response.status_code == 422, response.text

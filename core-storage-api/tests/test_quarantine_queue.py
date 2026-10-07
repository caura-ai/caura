"""The write gate's review queue and session rollback (g2.9).

A person works through a tenant's held memories (g2.7), newest first, and can
undo what one broker session wrote. Every other read leaves held memories out,
so the queue is the only bulk read that returns them. A rollback outdates the
session's live memories and the rows derived from them, which carry only
``metadata.parent_memory_id``, and rejects its held ones, the one way besides a
release that a held memory leaves quarantine.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql

from common.constants import QUARANTINED_MEMORY_STATUS
from common.models import Memory
from core_storage_api.database.init import get_engine
from core_storage_api.services import postgres_service
from core_storage_api.services.postgres_service import get_session

pytestmark = pytest.mark.asyncio

_P = "/api/v1/storage"
_EMBEDDING = [0.1] * 1024
_T0 = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)
_HELD = QUARANTINED_MEMORY_STATUS


def _tenant() -> str:
    return f"t-queue-{uuid.uuid4().hex[:8]}"


async def _seed(
    tenant: str,
    *,
    status: str = "active",
    session: str | None = None,
    parent: str | None = None,
    minute: int = 0,
    deleted: bool = False,
    supersedes: str | None = None,
) -> str:
    """A memory as the broker writes one: its session in ``metadata.session_id``,
    or, for a derived row, its parent in ``metadata.parent_memory_id``; and the
    memory it superseded, if any."""
    memory_id = uuid.uuid4()
    metadata: dict[str, str] = {}
    if session is not None:
        metadata["session_id"] = session
    if parent is not None:
        metadata["parent_memory_id"] = parent
    async with get_session() as db:
        await db.execute(
            text(
                "INSERT INTO memories (id, tenant_id, fleet_id, agent_id, memory_type, content, "
                "status, embedding, weight, visibility, metadata, created_at, deleted_at, supersedes_id) "
                "VALUES (:id, :t, 'fleet-q', 'agent-q', 'fact', :c, :s, CAST(:e AS vector), 0.5, "
                "'scope_team', :m, :at, :gone, :sup)"
            ),
            {
                "id": memory_id,
                "t": tenant,
                "c": f"memory {memory_id}",
                "s": "deleted" if deleted else status,
                "e": str(_EMBEDDING),
                "m": json.dumps(metadata),
                "at": _T0 + timedelta(minutes=minute),
                "gone": _T0 if deleted else None,
                "sup": uuid.UUID(supersedes) if supersedes else None,
            },
        )
    return str(memory_id)


async def _status(memory_id: str) -> str:
    async with get_session() as db:
        return (
            await db.execute(text("SELECT status FROM memories WHERE id = :id"), {"id": memory_id})
        ).scalar_one()


async def _conflict(tenant: str, *, winner: str, loser: str) -> None:
    """A contradiction's record with no ``supersedes_id`` edge beside it: what the
    detector writes for each loser past the first (M-34)."""
    async with get_session() as db:
        await db.execute(
            text(
                "INSERT INTO memory_conflicts (tenant_id, new_memory_id, old_memory_id, relationship) "
                "VALUES (:t, :new, :old, 'negation')"
            ),
            {"t": tenant, "new": uuid.UUID(winner), "old": uuid.UUID(loser)},
        )


async def _queue(client, tenant: str, **params) -> dict:
    response = await client.get(f"{_P}/memories/held", params={"tenant_id": tenant, **params})
    assert response.status_code == 200, response.text
    return response.json()


def _ids(page: dict) -> list[str]:
    return [item["id"] for item in page["items"]]


# ── The queue ──


async def test_the_queue_lists_the_tenants_held_memories_newest_first(client) -> None:
    tenant = _tenant()
    older = await _seed(tenant, status=_HELD, minute=1)
    newer = await _seed(tenant, status=_HELD, minute=2)
    await _seed(tenant)  # live
    await _seed(tenant, status="cancelled")  # rejected already
    await _seed(tenant, status=_HELD, deleted=True)  # purged
    await _seed(_tenant(), status=_HELD)  # another tenant's

    page = await _queue(client, tenant)

    assert _ids(page) == [newer, older]
    assert page["total"] == 2
    assert {item["status"] for item in page["items"]} == {_HELD}


async def test_the_queue_pages_on_its_cursor_and_counts_the_whole_queue(client) -> None:
    tenant = _tenant()
    held = [await _seed(tenant, status=_HELD, minute=m) for m in range(3)]

    first = await _queue(client, tenant, limit=2)
    last_row = first["items"][-1]
    rest = await _queue(client, tenant, limit=2, cursor_ts=last_row["created_at"], cursor_id=last_row["id"])

    assert _ids(first) == [held[2], held[1]]
    assert _ids(rest) == [held[0]]
    assert first["total"] == rest["total"] == 3


async def test_a_session_narrows_the_queue_to_what_it_held(client) -> None:
    tenant = _tenant()
    mine = [await _seed(tenant, status=_HELD, session="s-mine", minute=m) for m in range(2)]
    await _seed(tenant, status=_HELD, session="s-other")
    await _seed(tenant, status=_HELD)  # held, no session

    page = await _queue(client, tenant, session_id="s-mine")

    assert _ids(page) == mine[::-1]
    assert page["total"] == 2


async def test_the_queue_refuses_a_limit_it_cannot_serve(client) -> None:
    for limit in (0, 1002):
        response = await client.get(f"{_P}/memories/held", params={"tenant_id": _tenant(), "limit": limit})
        assert response.status_code == 422, (limit, response.text)


# ── Session rollback ──


async def test_a_rollback_outdates_a_sessions_live_writes_and_rejects_its_held_ones(client) -> None:
    tenant = _tenant()
    active = await _seed(tenant, session="s-1")
    confirmed = await _seed(tenant, status="confirmed", session="s-1")
    pending = await _seed(tenant, status="pending", session="s-1")
    held = await _seed(tenant, status=_HELD, session="s-1")
    # Derived rows carry no session, only their parent; so do theirs.
    child = await _seed(tenant, parent=active)
    grandchild = await _seed(tenant, parent=child)
    # Out of play already, so left as they are.
    outdated = await _seed(tenant, status="outdated", session="s-1")
    archived = await _seed(tenant, status="archived", session="s-1")
    deleted = await _seed(tenant, session="s-1", deleted=True)
    dead_child = await _seed(tenant, status="cancelled", parent=active)
    # Not this session's, or not this tenant's.
    other_session = await _seed(tenant, session="s-2")
    other_tenant = await _seed(_tenant(), session="s-1")

    response = await client.post(
        f"{_P}/memories/rollback-session", json={"tenant_id": tenant, "session_id": "s-1"}
    )

    assert response.status_code == 200, response.text
    changed = response.json()
    assert set(changed["outdated"]) == {active, confirmed, pending, child, grandchild}
    assert changed["cancelled"] == [held]
    for memory_id in changed["outdated"]:
        assert await _status(memory_id) == "outdated"
    assert await _status(held) == "cancelled"
    assert [await _status(m) for m in (outdated, archived, deleted, dead_child)] == [
        "outdated",
        "archived",
        "deleted",
        "cancelled",
    ]
    assert await _status(other_session) == "active"
    assert await _status(other_tenant) == "active"


async def test_a_second_rollback_changes_nothing(client) -> None:
    tenant = _tenant()
    superseded = await _seed(tenant, status="outdated")
    await _seed(tenant, session="s-again", supersedes=superseded)
    await _seed(tenant, status=_HELD, session="s-again")
    body = {"tenant_id": tenant, "session_id": "s-again"}

    first = await client.post(f"{_P}/memories/rollback-session", json=body)
    again = await client.post(f"{_P}/memories/rollback-session", json=body)

    assert first.json()["restored"] == [superseded]
    assert again.status_code == 200, again.text
    assert again.json() == {"outdated": [], "restored": [], "cancelled": []}


async def test_a_rollback_brings_back_what_the_sessions_writes_superseded(client) -> None:
    tenant = _tenant()
    # A near-duplicate merge outdates the memory a write supersedes; a
    # contradiction verdict can leave it conflicted.
    merged = await _seed(tenant, status="outdated")
    contradicted = await _seed(tenant, status="conflicted")
    by_a_child = await _seed(tenant, status="outdated")
    # A further loser of the same contradiction, named only by its record.
    unlinked = await _seed(tenant, status="conflicted")
    write = await _seed(tenant, session="s-1", supersedes=merged)
    await _conflict(tenant, winner=write, loser=unlinked)
    await _seed(tenant, session="s-1", supersedes=contradicted)
    await _seed(tenant, parent=write, supersedes=by_a_child)

    response = await client.post(
        f"{_P}/memories/rollback-session", json={"tenant_id": tenant, "session_id": "s-1"}
    )

    assert response.status_code == 200, response.text
    assert set(response.json()["restored"]) == {merged, contradicted, by_a_child, unlinked}
    assert [await _status(m) for m in (merged, contradicted, by_a_child, unlinked)] == ["active"] * 4


async def test_a_rollback_brings_back_only_what_its_writes_held_down(client) -> None:
    tenant = _tenant()
    # Moved since by something else: archived, or deleted.
    archived = await _seed(tenant, status="archived")
    deleted = await _seed(tenant, status="outdated")
    # Still superseded, or still contradicted, by a write the rollback leaves live.
    still_superseded = await _seed(tenant, status="outdated")
    await _seed(tenant, session="s-2", supersedes=still_superseded)
    still_contradicted = await _seed(tenant, status="conflicted")
    await _conflict(tenant, winner=await _seed(tenant, session="s-2"), loser=still_contradicted)
    await _conflict(tenant, winner=await _seed(tenant, session="s-1"), loser=still_contradicted)
    # The session's own: an earlier write its later one superseded.
    own_earlier = await _seed(tenant, status="outdated", session="s-1")
    for target in (archived, deleted, still_superseded, own_earlier):
        await _seed(tenant, session="s-1", supersedes=target)
    # A derived row its sibling superseded, both rolled back now.
    write = await _seed(tenant, session="s-1")
    sibling = await _seed(tenant, parent=write)
    await _seed(tenant, parent=write, supersedes=sibling)
    # A write already out of play: its supersession is not this rollback's.
    behind_it = await _seed(tenant, status="outdated")
    await _seed(tenant, status="outdated", session="s-1", supersedes=behind_it)
    async with get_session() as db:
        await db.execute(
            text("UPDATE memories SET deleted_at = :at WHERE id = :id"), {"at": _T0, "id": uuid.UUID(deleted)}
        )

    response = await client.post(
        f"{_P}/memories/rollback-session", json={"tenant_id": tenant, "session_id": "s-1"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["restored"] == []
    assert [
        await _status(m)
        for m in (archived, deleted, still_superseded, still_contradicted, own_earlier, sibling, behind_it)
    ] == [
        "archived",
        "outdated",
        "outdated",
        "conflicted",
        "outdated",
        "outdated",
        "outdated",
    ]


async def test_a_rollback_names_a_tenant_and_a_session(client) -> None:
    for body in (
        {"session_id": "s-1"},
        {"tenant_id": _tenant()},
        {"tenant_id": _tenant(), "session_id": ""},
        {"tenant_id": _tenant(), "session_id": ["s-1"]},
    ):
        response = await client.post(f"{_P}/memories/rollback-session", json=body)
        assert response.status_code == 422, (body, response.text)


# ── The indexes, on the migrated schema ──


async def _plan(stmt) -> str:
    sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    async with get_engine().connect() as conn:
        # On a near-empty table a sequential scan is legitimately cheaper; the
        # question is whether migration 062's index CAN serve the predicate.
        await conn.execute(text("SET LOCAL enable_seqscan = off"))
        return "\n".join(row[0] for row in (await conn.execute(text(f"EXPLAIN {sql}"))).all())


async def test_a_sessions_rows_are_found_by_index(_ensure_schema) -> None:
    plan = await _plan(select(Memory.id).where(*postgres_service.session_rows_where("t-plan", "s-plan")))
    assert "ix_memories_session" in plan, plan


async def test_the_queue_is_read_by_index(_ensure_schema) -> None:
    stmt = (
        select(Memory.id)
        .where(*postgres_service.held_rows_where("t-plan"))
        .order_by(Memory.created_at.desc(), Memory.id.desc())
        .limit(51)
    )
    plan = await _plan(stmt)
    assert "ix_memories_held" in plan, plan

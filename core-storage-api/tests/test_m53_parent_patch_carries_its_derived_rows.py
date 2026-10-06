"""M-53: a parent PATCH changes the rows derived from it in its own transaction.

``memory_update`` applies the synthetic ``derived`` key under the parent's row
lock, with the parent's own UPDATEs. So a failure anywhere in that write leaves
the parent and its children as they were, and the caller's retry applies the
whole edit. core-api used to change the children in a request of their own,
ahead of the parent's PATCH, so a PATCH that then failed left the parent
unedited and its children already deleted, narrowed or re-expired.

Real Postgres, because the rollback is what is under test. The failure is a
metadata merge that cannot be serialised: it raises inside the transaction,
after the derived rows and the parent's columns are written.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from core_storage_api.database.init import get_engine
from core_storage_api.services.postgres_service import PostgresService

pytestmark = pytest.mark.asyncio

LATER = datetime(2027, 1, 1, tzinfo=UTC)
UNSERIALISABLE = {"metadata_patch": {"bad": object()}}


async def _add(svc: PostgresService, tenant: str, content: str, *, parent=None):
    metadata = {"parent_memory_id": str(parent), "source": "auto_chunk"} if parent else {}
    row = await svc.memory_add(
        {
            "tenant_id": tenant,
            "agent_id": "m53-agent",
            "content": content,
            "memory_type": "fact",
            "status": "active",
            "visibility": "scope_team",
            "metadata_": metadata,
        }
    )
    return row.id


async def _family(svc: PostgresService, tenant: str):
    """A parent and two rows derived from it."""
    parent = await _add(svc, tenant, "The plan covers hiring and the budget.")
    children = [await _add(svc, tenant, f"The plan covers {t}.", parent=parent) for t in ("hiring", "budget")]
    return parent, children


async def _state(*ids) -> list[tuple]:
    """(live, visibility, expires_at, content) per id, in order."""
    async with get_engine().connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT id, deleted_at IS NULL, visibility, expires_at, content "
                "FROM memories WHERE id = ANY(:ids)"
            ),
            {"ids": list(ids)},
        )
        by_id = {r[0]: tuple(r[1:]) for r in rows}
    return [by_id[i] for i in ids]


async def test_a_content_edit_deletes_the_children_in_its_own_write(_ensure_schema):
    """The children go, with the rows derived from them, as every delete takes."""
    svc = PostgresService()
    tenant = f"m53-{uuid.uuid4().hex[:8]}"
    parent, children = await _family(svc, tenant)
    grandchild = await _add(svc, tenant, "Hiring is in the plan.", parent=children[0])
    before = await _state(parent, *children, grandchild)
    edit = {"content": "The plan covers the launch.", "derived": {"soft_delete_children": True}}

    with pytest.raises(TypeError):
        await svc.memory_update(parent, tenant, {**edit, **UNSERIALISABLE})
    assert await _state(parent, *children, grandchild) == before

    deleted: list[dict] = []
    assert await svc.memory_update(parent, tenant, edit, derived_deleted=deleted) is True
    (live, _, _, content), *derived = await _state(parent, *children, grandchild)
    assert (live, content) == (True, "The plan covers the launch.")
    assert [row[0] for row in derived] == [False, False, False]
    # Every live child, whoever wrote it, is named for the audit; the grandchild
    # goes with its parent, as on every delete, and is not.
    assert sorted(d["id"] for d in deleted) == sorted(str(c) for c in children)


async def test_a_narrowing_and_a_ttl_reach_the_children_in_its_own_write(_ensure_schema):
    svc = PostgresService()
    tenant = f"m53-{uuid.uuid4().hex[:8]}"
    parent, children = await _family(svc, tenant)
    before = await _state(parent, *children)
    edit = {
        "visibility": "scope_agent",
        "expires_at": LATER,
        "derived": {
            "visibility": "scope_agent",
            "wider": ["scope_team", "scope_org"],
            "mirror_expires_at": True,
            "expires_at": LATER,
        },
    }

    with pytest.raises(TypeError):
        await svc.memory_update(parent, tenant, {**edit, **UNSERIALISABLE})
    assert await _state(parent, *children) == before

    assert await svc.memory_update(parent, tenant, edit) is True
    assert [row[:3] for row in await _state(parent, *children)] == [(True, "scope_agent", LATER)] * 3

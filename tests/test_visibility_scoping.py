"""Tests for the list_by_filters visibility predicate.

The ``scope_agent`` visibility gap (pre-Phase-4: any caller could browse
any agent's ``scope_agent`` memories) is closed by the
``list_by_filters`` repo method. These tests prove:

1. ``scope_agent`` memories are invisible to callers who aren't the author.
2. ``scope_agent`` memories ARE visible when ``caller_agent_id`` matches.
3. ``scope_team`` and ``scope_org`` memories are always visible.
4. When ``caller_agent_id`` is None, all ``scope_agent`` memories are hidden.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.memory import Memory
from core_api.repositories import memory_repo

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _mem(
    tenant: str,
    agent: str,
    visibility: str,
    content: str | None = None,
) -> Memory:
    return Memory(
        id=uuid4(),
        tenant_id=tenant,
        agent_id=agent,
        fleet_id=None,
        memory_type="fact",
        content=content or f"memory-{uuid4().hex[:6]}",
        weight=0.5,
        status="active",
        visibility=visibility,
        created_at=datetime.now(UTC),
    )


@pytest.fixture
async def seeded_db(db: AsyncSession):
    """Seed 4 memories: 2 scope_team, 1 scope_org, 1 scope_agent (by alice)."""
    tenant = f"t-{uuid4().hex[:6]}"
    mems = [
        _mem(tenant, "alice", "scope_team", "team fact by alice"),
        _mem(tenant, "bob", "scope_team", "team fact by bob"),
        _mem(tenant, "alice", "scope_org", "org-wide fact by alice"),
        _mem(tenant, "alice", "scope_agent", "alice private note"),
    ]
    for m in mems:
        db.add(m)
    await db.flush()
    return tenant, mems


async def test_scope_agent_hidden_from_other_agents(seeded_db, db):
    tenant, _ = seeded_db
    rows = await memory_repo.list_by_filters(
        db, tenant_id=tenant, caller_agent_id="bob"
    )
    contents = {m.content for m in rows}
    assert "alice private note" not in contents, "scope_agent leaked to bob"
    assert "team fact by alice" in contents
    assert "team fact by bob" in contents
    assert "org-wide fact by alice" in contents


async def test_scope_agent_visible_to_author(seeded_db, db):
    tenant, _ = seeded_db
    rows = await memory_repo.list_by_filters(
        db, tenant_id=tenant, caller_agent_id="alice"
    )
    contents = {m.content for m in rows}
    assert "alice private note" in contents


async def test_scope_agent_hidden_when_caller_unknown(seeded_db, db):
    tenant, _ = seeded_db
    rows = await memory_repo.list_by_filters(
        db, tenant_id=tenant, caller_agent_id=None
    )
    contents = {m.content for m in rows}
    assert "alice private note" not in contents


async def test_team_and_org_always_visible(seeded_db, db):
    tenant, _ = seeded_db
    for caller in ("alice", "bob", "charlie", None):
        rows = await memory_repo.list_by_filters(
            db, tenant_id=tenant, caller_agent_id=caller
        )
        contents = {m.content for m in rows}
        assert "team fact by alice" in contents
        assert "team fact by bob" in contents
        assert "org-wide fact by alice" in contents

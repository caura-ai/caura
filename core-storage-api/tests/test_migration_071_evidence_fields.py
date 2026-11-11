"""Migration 071: the evidence-contract columns on ``memories``.

Six columns (``observed_at``, ``trace_id``, ``tool_call_id``,
``verification_status``, ``verified_by``, ``verified_at``), a CHECK holding
``verification_status`` to the five v1 values, and a partial index for the
planned ``verified_only`` filter. The down/up test runs on a throwaway database,
as ``test_migration_retry`` does: the shared one is always at head.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from common.models.memory import VERIFICATION_STATUSES, Memory
from core_storage_api.config import settings
from core_storage_api.database import init as init_mod
from core_storage_api.services.postgres_service import get_session
from tests.conftest import plan_with_only_index

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_MIGRATIONS = Path(init_mod.__file__).parent / "migrations"
_COLUMNS = {
    "observed_at": ("timestamp with time zone", "YES", None),
    "trace_id": ("text", "YES", None),
    "tool_call_id": ("text", "YES", None),
    "verification_status": ("text", "NO", "'unverified'::text"),
    "verified_by": ("text", "YES", None),
    "verified_at": ("timestamp with time zone", "YES", None),
}


@pytest.fixture
async def scratch_engine() -> AsyncIterator[AsyncEngine]:
    base = make_url(settings.database_url.get_secret_value())
    name = f"{base.database}_mig071_{uuid.uuid4().hex[:8]}"
    admin = create_async_engine(base, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_async_engine(base.set(database=name))
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        yield engine
    finally:
        await engine.dispose()
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


async def _alembic(engine: AsyncEngine, action: str, revision: str) -> None:
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS))

    def run(connection) -> None:
        cfg.attributes["connection"] = connection
        getattr(command, action)(cfg, revision)

    async with engine.connect() as conn:
        await conn.run_sync(run)


async def _shape(conn) -> dict:
    rows = await conn.execute(
        text(
            "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'memories' AND column_name = ANY(:cols)"
        ),
        {"cols": list(_COLUMNS)},
    )
    return {r.column_name: (r.data_type, r.is_nullable, r.column_default) for r in rows}


async def _has(conn, sql: str) -> bool:
    return bool(await conn.scalar(text(sql)))


_VALID_INDEX = (
    "SELECT EXISTS (SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
    "WHERE c.relname = 'ix_memories_verification' AND i.indisvalid)"
)
_VALIDATED_CHECK = (
    "SELECT EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname = 'ck_memories_verification_status' AND convalidated)"
)


async def test_upgrade_downgrade_upgrade(scratch_engine: AsyncEngine) -> None:
    await _alembic(scratch_engine, "upgrade", "070")
    # A row written before 071 must read back ``unverified`` after it.
    async with scratch_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO memories (tenant_id, agent_id, memory_type, content) "
                "VALUES ('t-071', 'a', 'fact', 'written before 071')"
            )
        )

    await _alembic(scratch_engine, "upgrade", "071")
    async with scratch_engine.connect() as conn:
        assert await _shape(conn) == _COLUMNS
        assert await _has(conn, _VALID_INDEX)
        assert await _has(conn, _VALIDATED_CHECK)
        assert (
            await conn.scalar(text("SELECT verification_status FROM memories WHERE tenant_id = 't-071'"))
            == "unverified"
        )

    await _alembic(scratch_engine, "downgrade", "070")
    async with scratch_engine.connect() as conn:
        assert await _shape(conn) == {}
        assert not await _has(conn, _VALID_INDEX)
        assert not await _has(
            conn,
            "SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_memories_verification_status')",
        )

    await _alembic(scratch_engine, "upgrade", "071")
    async with scratch_engine.connect() as conn:
        assert await _shape(conn) == _COLUMNS
        assert await _has(conn, _VALID_INDEX)


async def test_the_check_refuses_a_status_outside_the_contract(_ensure_schema) -> None:
    with pytest.raises(IntegrityError, match="ck_memories_verification_status"):
        async with get_session() as session:
            await session.execute(
                text(
                    "INSERT INTO memories (tenant_id, agent_id, memory_type, content, verification_status) "
                    "VALUES ('t-071-check', 'a', 'fact', 'self-certified', 'verified')"
                )
            )
    async with get_session() as session:
        for status in VERIFICATION_STATUSES:
            await session.execute(
                text(
                    "INSERT INTO memories (tenant_id, agent_id, memory_type, content, verification_status) "
                    "VALUES ('t-071-check', 'a', 'fact', :c, :s)"
                ),
                {"c": f"status {status} {uuid.uuid4().hex}", "s": status},
            )
        await session.execute(text("DELETE FROM memories WHERE tenant_id = 't-071-check'"))


async def test_the_storage_insert_round_trips_the_evidence(client) -> None:
    tenant = f"t-071-rt-{uuid.uuid4().hex[:8]}"
    r = await client.post(
        "/api/v1/storage/memories",
        json={
            "tenant_id": tenant,
            "agent_id": "a",
            "memory_type": "fact",
            "content": "evidence round trip through storage",
            "observed_at": "2026-11-01T09:30:00+00:00",
            "trace_id": "mcp-abc",
            "tool_call_id": "toolu_1",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["observed_at"] == "2026-11-01T09:30:00+00:00"
    assert (body["trace_id"], body["tool_call_id"]) == ("mcp-abc", "toolu_1")
    assert body["verification_status"] == "unverified"
    assert body["verified_by"] is None and body["verified_at"] is None


async def test_the_verification_index_can_serve_a_verified_only_filter(_ensure_schema) -> None:
    stmt = select(Memory.id).where(
        Memory.tenant_id == "t1",
        Memory.verification_status.in_(("tool_observed", "human_verified")),
    )
    plan = await plan_with_only_index(stmt, "ix_memories_verification")
    assert "ix_memories_verification" in plan, plan

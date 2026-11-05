"""Migration 075: ``ix_memories_baton`` serves the ``baton_id`` read filter.

Handoff batons, HB-10. The filter is ``_baton_clause``, shared by the list, the
scored search, the by-id load and the successor lookup; the index is keyed on
the expression it compares, partial on rows that carry a baton.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text

from common.models.memory import Memory
from core_storage_api.database.init import get_engine
from core_storage_api.services.postgres_service import _baton_clause
from tests.conftest import plan_with_only_index

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_the_baton_index_is_built_valid(_ensure_schema) -> None:
    async with get_engine().connect() as conn:
        valid = await conn.scalar(
            text(
                "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = 'ix_memories_baton'"
            )
        )
    assert valid is True


async def test_the_baton_index_can_serve_the_baton_filter(_ensure_schema) -> None:
    stmt = select(Memory.id).where(Memory.tenant_id == "t1", _baton_clause("b-1"))
    plan = await plan_with_only_index(stmt, "ix_memories_baton")
    assert "ix_memories_baton" in plan, plan

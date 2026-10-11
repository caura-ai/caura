"""``plan_with_only_index`` answers no as well as yes.

Every index plan check in this suite asserts that the index it names appears in
the plan. That only means something if an index that cannot serve the statement
stays out of it, and if an index the migrations never built fails the check.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from common.models import Memory
from core_storage_api.services import postgres_service
from tests.conftest import plan_with_only_index

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("_ensure_schema")]

_QUEUE = select(Memory.id).where(*postgres_service.held_rows_where("t-plan"))


async def test_an_index_that_cannot_serve_the_statement_is_not_used() -> None:
    # The session index covers live rows with a session id; the queue asks for
    # held rows, which that predicate does not imply.
    plan = await plan_with_only_index(_QUEUE, "ix_memories_session")
    assert "ix_memories_session" not in plan, plan
    assert "Seq Scan" in plan, plan


async def test_an_index_the_migrations_never_built_fails_the_check() -> None:
    with pytest.raises(AssertionError, match="is not on the migrated memories table"):
        await plan_with_only_index(_QUEUE, "ix_memories_never_built")

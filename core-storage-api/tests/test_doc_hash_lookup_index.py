"""L-193 on the migrated schema: the ingest doc-hash lookup has an index.

Every /ingest/preview and /ingest/file looks the document's hash up before any
LLM work (``find_prior_ingest_by_doc_hash``). Nothing indexed
``metadata ->> 'doc_hash'``, so on a cache miss, the common case for a new
document, each preview filtered every live row of the tenant on the primary.
Migration 059 adds a partial index the lookup's predicate can use. Run against
the real migration chain, where ``memories.metadata`` is ``json``, because that
is what the predicate and the index expression have to agree on in production.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql

from common.models import Memory
from core_storage_api.database.init import get_engine
from core_storage_api.services import postgres_service

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("fleet_id", ["fleet-a", None])
async def test_the_doc_hash_lookup_can_use_the_index(_ensure_schema, fleet_id):
    """The cost claim, checked against the planner rather than asserted.

    ``enable_seqscan`` is off because on a near-empty table a sequential scan is
    legitimately cheaper; the question is whether migration 059's index CAN serve
    the predicate ``find_prior_ingest_by_doc_hash`` runs, with a fleet and without.
    """
    where = postgres_service.prior_ingest_where(
        "l193-plan", "sha256:l193", fleet_id=fleet_id, agent_id="agent-1"
    )
    stmt = select(Memory.id).where(*where)
    sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    async with get_engine().connect() as conn:
        await conn.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(r[0] for r in (await conn.execute(text(f"EXPLAIN {sql}"))).all())
    assert "ix_memories_ingest_doc_hash" in plan, plan

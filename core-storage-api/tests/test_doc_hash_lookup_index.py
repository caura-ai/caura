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
from sqlalchemy import select

from common.models import Memory
from core_storage_api.services import postgres_service
from tests.conftest import plan_with_only_index

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("fleet_id", ["fleet-a", None])
async def test_the_doc_hash_lookup_can_use_the_index(_ensure_schema, fleet_id):
    """The cost claim, checked against the planner rather than asserted.

    Planned with migration 059's index as the table's only one
    (``plan_with_only_index``): the question is whether that index CAN serve the
    predicate ``find_prior_ingest_by_doc_hash`` runs, with a fleet and without.
    """
    where = postgres_service.prior_ingest_where(
        "l193-plan", "sha256:l193", fleet_id=fleet_id, agent_id="agent-1"
    )
    plan = await plan_with_only_index(select(Memory.id).where(*where), "ix_memories_ingest_doc_hash")
    assert "ix_memories_ingest_doc_hash" in plan, plan

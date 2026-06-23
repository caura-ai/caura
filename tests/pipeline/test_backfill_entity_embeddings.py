"""Unit tests for the BackfillEntityEmbeddings pipeline step.

Partially DB-free as of Fix 2 Ph6: the NULL-embedding read and the embedding
write-back route through core-storage-api (``sc.list_null_embedding_entities``
/ ``sc.set_entity_embeddings``), but the LLM ``get_embedding`` loop stays in
core-api. These mock the storage client + embedder and assert the step:
read → per-row embed → write, mapping ``backfill_count`` into ``ctx.data``.

The Core ``update(Entity.__table__)`` executemany regression anchor (prod
2026-06-16: ``update(Entity)`` routed to ORM Bulk UPDATE by Primary Key and
raised "No primary key value supplied for column(s) entities.id") now lives
storage-side in ``tests/test_ph6_entity_linking_storage.py`` against the real DB.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepOutcome
from core_api.pipeline.steps.entity_linking.backfill_entity_embeddings import (
    BackfillEntityEmbeddings,
)

TENANT = "test-tenant"


def _ctx(**extra):
    return PipelineContext(db=None, data={"tenant_id": TENANT, **extra})


@pytest.mark.asyncio
async def test_read_embed_write_roundtrip():
    """Read NULL-embedding rows → embed each → write back via storage."""
    eid = str(uuid.uuid4())
    sc = MagicMock()
    sc.list_null_embedding_entities = AsyncMock(return_value=[{"id": eid, "canonical_name": "Globex"}])
    sc.set_entity_embeddings = AsyncMock(return_value=1)

    async def _fake_embed(text, tenant_config):
        return [0.1] * 8

    with (
        patch(
            "core_api.pipeline.steps.entity_linking.backfill_entity_embeddings.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.pipeline.steps.entity_linking.backfill_entity_embeddings.get_embedding",
            new=_fake_embed,
        ),
    ):
        ctx = _ctx()
        result = await BackfillEntityEmbeddings().execute(ctx)

    assert result.outcome == StepOutcome.SUCCESS
    assert ctx.data["backfill_count"] == 1
    # The write carries the {id, embedding} shape the set-embeddings endpoint
    # expects — storage binds the PK off a custom ``eid`` bindparam.
    write_kwargs = sc.set_entity_embeddings.await_args.kwargs
    assert write_kwargs["tenant_id"] == TENANT
    assert write_kwargs["updates"] == [{"id": eid, "embedding": [0.1] * 8}]


@pytest.mark.asyncio
async def test_no_null_embedding_entities_skips():
    sc = MagicMock()
    sc.list_null_embedding_entities = AsyncMock(return_value=[])
    sc.set_entity_embeddings = AsyncMock()
    with patch(
        "core_api.pipeline.steps.entity_linking.backfill_entity_embeddings.get_storage_client",
        return_value=sc,
    ):
        result = await BackfillEntityEmbeddings().execute(_ctx())
    assert result.outcome == StepOutcome.SKIPPED
    sc.set_entity_embeddings.assert_not_awaited()


@pytest.mark.asyncio
async def test_embed_failure_skips_row_but_does_not_fail_step():
    eid = str(uuid.uuid4())
    sc = MagicMock()
    sc.list_null_embedding_entities = AsyncMock(return_value=[{"id": eid, "canonical_name": "Globex"}])
    sc.set_entity_embeddings = AsyncMock(return_value=0)

    async def _boom(text, tenant_config):
        raise RuntimeError("provider down")

    with (
        patch(
            "core_api.pipeline.steps.entity_linking.backfill_entity_embeddings.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.pipeline.steps.entity_linking.backfill_entity_embeddings.get_embedding",
            new=_boom,
        ),
    ):
        ctx = _ctx()
        result = await BackfillEntityEmbeddings().execute(ctx)

    # All rows failed to embed → no write call, count stays 0, step succeeds.
    assert result.outcome == StepOutcome.SUCCESS
    assert ctx.data["backfill_count"] == 0
    sc.set_entity_embeddings.assert_not_awaited()

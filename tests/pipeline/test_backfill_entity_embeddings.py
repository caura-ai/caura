"""Unit tests for BackfillEntityEmbeddings.

Regression anchor (prod 2026-06-13): the bulk-update execute raised
``sqlalchemy.exc.InvalidRequestError: bulk synchronize of persistent
objects not supported when using bulk update with additional WHERE
criteria`` — an ORM ``update(Entity).where(...).values(...)`` over an
executemany param list needs ``synchronize_session=False``. Every
backfill batch failed (6 occurrences in ~10h; entity name_embeddings
silently not backfilled). Same ``entity_linking_full`` family as the
discover_cross_links (#337) and infer_relations (#341) fixes.

Mock-based — assert the statement carries synchronize_session=False.
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


def _mock_result(rows):
    m = MagicMock()
    m.all.return_value = rows
    return m


def _ctx(db, **extra):
    ctx = PipelineContext(db=db, data={"tenant_id": TENANT, **extra})
    return ctx


@pytest.mark.asyncio
async def test_bulk_update_sets_synchronize_session_false():
    """The backfill UPDATE must carry execution_option
    synchronize_session=False, or SQLAlchemy raises InvalidRequestError
    on the bulk-update-with-WHERE path."""
    eid = uuid.uuid4()
    db = AsyncMock()
    db.execute.side_effect = [
        _mock_result([(eid, "Globex")]),  # 1. select NULL-embedding entities
        _mock_result([]),  # 2. the bulk UPDATE
    ]
    db.flush = AsyncMock()

    async def _fake_embed(text, tenant_config):
        return [0.1] * 8

    with patch(
        "core_api.pipeline.steps.entity_linking.backfill_entity_embeddings.get_embedding",
        new=_fake_embed,
    ):
        ctx = _ctx(db)  # tenant_config defaults to None
        result = await BackfillEntityEmbeddings().execute(ctx)

    assert result.outcome == StepOutcome.SUCCESS
    assert ctx.data["backfill_count"] == 1
    update_stmt = db.execute.call_args_list[1].args[0]
    assert update_stmt.get_execution_options().get("synchronize_session") is False


@pytest.mark.asyncio
async def test_no_null_embedding_entities_skips():
    db = AsyncMock()
    db.execute.side_effect = [_mock_result([])]
    db.flush = AsyncMock()
    result = await BackfillEntityEmbeddings().execute(_ctx(db))
    assert result.outcome == StepOutcome.SKIPPED

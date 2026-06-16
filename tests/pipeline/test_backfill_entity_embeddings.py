"""Unit tests for BackfillEntityEmbeddings.

Regression anchor (prod 2026-06-16): the bulk-update execute raised
``sqlalchemy.exc.InvalidRequestError: No primary key value supplied for
column(s) entities.id; per-row ORM Bulk UPDATE by Primary Key requires
that records contain primary key values``. ``session.execute(update(Entity),
[param dicts])`` routes to SQLAlchemy's ORM Bulk UPDATE by Primary Key, which
needs each dict keyed by ``id`` — but the dicts key the PK off a custom
``eid`` bindparam, so it failed. (The earlier prod 2026-06-13 ``InvalidRequestError:
bulk synchronize ...`` on the same ORM path was only silenced by
``synchronize_session=False``, which masked this second failure mode.) The fix:
target the Core table ``update(Entity.__table__)`` — a plain executemany UPDATE
with no ORM bulk-by-PK behaviour. Every backfill batch failed (6 occurrences in
~10h; entity name_embeddings silently not backfilled). Same ``entity_linking_full``
family as the discover_cross_links (#337) and infer_relations (#341) fixes.

Mock-based — assert the statement is a Core UPDATE, not an ORM-enabled one.
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
async def test_bulk_update_targets_core_table_not_orm_entity():
    """The backfill UPDATE must be a Core ``update(Entity.__table__)``, not an
    ORM ``update(Entity)``. The ORM form routes ``session.execute(stmt, [params])``
    through "ORM Bulk UPDATE by Primary Key" and raised "No primary key value
    supplied for column(s) entities.id" in prod (2026-06-16). A Core statement
    carries no ORM compile plugin, so it never takes that path."""
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

    update_stmt, exec_params = db.execute.call_args_list[1].args
    assert update_stmt.is_dml
    assert update_stmt.table.name == "entities"
    # No ORM compile plugin => plain Core UPDATE => no "ORM Bulk UPDATE by
    # Primary Key" path (which is what raised in prod).
    assert "compile_state_plugin" not in update_stmt._propagate_attrs
    # The custom-bindparam executemany param list is passed through unchanged.
    assert exec_params == [{"eid": eid, "emb": [0.1] * 8}]


@pytest.mark.asyncio
async def test_no_null_embedding_entities_skips():
    db = AsyncMock()
    db.execute.side_effect = [_mock_result([])]
    db.flush = AsyncMock()
    result = await BackfillEntityEmbeddings().execute(_ctx(db))
    assert result.outcome == StepOutcome.SKIPPED

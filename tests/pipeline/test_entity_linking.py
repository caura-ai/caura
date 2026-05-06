"""Unit tests for DiscoverCrossLinks pipeline step."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepOutcome
from core_api.pipeline.steps.entity_linking.discover_cross_links import (
    DiscoverCrossLinks,
)

TENANT = "test-tenant"


def _mock_result(rows, rowcount: int | None = None):
    """Build a mock DB result with .all() returning *rows*."""
    mock = MagicMock()
    mock.all.return_value = rows
    if rowcount is not None:
        mock.rowcount = rowcount
    else:
        mock.rowcount = len(rows)
    return mock


def _make_ctx(db, **extra_data):
    return PipelineContext(
        db=db,
        data={"tenant_id": TENANT, **extra_data},
    )


@pytest.mark.asyncio
async def test_discover_skips_when_no_candidates():
    db = AsyncMock()
    db.execute.return_value = _mock_result([])

    ctx = _make_ctx(db)
    step = DiscoverCrossLinks()
    result = await step.execute(ctx)

    assert result.outcome == StepOutcome.SKIPPED


@pytest.mark.asyncio
async def test_discover_creates_links():
    """Happy path: candidates + lateral matches -> INSERT RETURNING counts inserted rows."""
    mem_id = uuid.uuid4()
    ent_id = uuid.uuid4()
    embedding = [0.1] * 10

    # First call: candidate memories
    candidates = [(mem_id, "Alice loves coffee", embedding)]
    # Second call: lateral join results
    lateral = [(mem_id, ent_id, "Alice", None, 0.95)]
    # Third call: bulk INSERT RETURNING — one row inserted
    inserted = [(uuid.uuid4(),)]

    db = AsyncMock()
    db.execute.side_effect = [
        _mock_result(candidates),
        _mock_result(lateral),
        _mock_result(inserted),
    ]
    db.flush = AsyncMock()

    ctx = _make_ctx(db, cross_link_text_verify=False)
    step = DiscoverCrossLinks()
    result = await step.execute(ctx)

    assert result.outcome == StepOutcome.SUCCESS
    assert ctx.data["links_created"] == 1


@pytest.mark.asyncio
async def test_discover_text_verify_filters():
    """Text-verify rejects entities whose name doesn't appear in memory content."""
    mem_id = uuid.uuid4()
    ent_id = uuid.uuid4()
    embedding = [0.1] * 10

    candidates = [(mem_id, "Alice loves coffee", embedding)]
    lateral = [(mem_id, ent_id, "Bob", None, 0.90)]

    db = AsyncMock()
    db.execute.side_effect = [
        _mock_result(candidates),
        _mock_result(lateral),
    ]
    db.flush = AsyncMock()

    ctx = _make_ctx(db, cross_link_text_verify=True)
    step = DiscoverCrossLinks()
    result = await step.execute(ctx)

    assert result.outcome == StepOutcome.SUCCESS
    assert ctx.data["links_created"] == 0


@pytest.mark.asyncio
async def test_discover_conflict_counts_only_actually_inserted():
    """ON CONFLICT DO NOTHING: counts only actually-inserted rows via RETURNING."""
    mem_id = uuid.uuid4()
    ent_id = uuid.uuid4()
    embedding = [0.1] * 10

    candidates = [(mem_id, "Alice loves coffee", embedding)]
    lateral = [(mem_id, ent_id, "Alice", None, 0.90)]

    db = AsyncMock()
    db.execute.side_effect = [
        _mock_result(candidates),
        _mock_result(lateral),
        # bulk INSERT RETURNING — all conflicts, nothing inserted
        _mock_result([]),
    ]
    db.flush = AsyncMock()

    ctx = _make_ctx(db, cross_link_text_verify=False)
    step = DiscoverCrossLinks()
    result = await step.execute(ctx)

    assert result.outcome == StepOutcome.SUCCESS
    assert ctx.data["links_created"] == 0


@pytest.mark.asyncio
async def test_discover_with_target_memory_ids():
    """Targeted mode uses WHERE m.id = ANY(:memory_ids) instead of HAVING/LIMIT."""
    mem_id = uuid.uuid4()
    ent_id = uuid.uuid4()
    embedding = [0.1] * 10

    candidates = [(mem_id, "Alice loves coffee", embedding)]
    lateral = [(mem_id, ent_id, "Alice", None, 0.92)]
    inserted = [(uuid.uuid4(),)]

    db = AsyncMock()
    db.execute.side_effect = [
        _mock_result(candidates),
        _mock_result(lateral),
        _mock_result(inserted),
    ]
    db.flush = AsyncMock()

    ctx = _make_ctx(
        db,
        target_memory_ids=[mem_id],
        cross_link_text_verify=False,
    )
    step = DiscoverCrossLinks()
    result = await step.execute(ctx)

    assert result.outcome == StepOutcome.SUCCESS
    assert ctx.data["links_created"] == 1

    # Targeted mode binds :memory_ids; ``::uuid[]`` form mis-parsed as a second
    # bind in production (CAURA-675), so guard against its reintroduction.
    first_sql = str(db.execute.call_args_list[0][0][0].text)
    assert ":memory_ids" in first_sql
    assert "::uuid[]" not in first_sql
    assert "HAVING" not in first_sql
    assert "LIMIT" not in first_sql


@pytest.mark.asyncio
async def test_discover_without_target_memory_ids_unchanged():
    """Batch mode (no target_memory_ids) uses HAVING/LIMIT as before."""
    db = AsyncMock()
    db.execute.return_value = _mock_result([])

    ctx = _make_ctx(db)
    step = DiscoverCrossLinks()
    await step.execute(ctx)

    first_sql = str(db.execute.call_args_list[0][0][0].text)
    assert "HAVING" in first_sql
    assert "LIMIT" in first_sql
    assert "ANY(:memory_ids" not in first_sql

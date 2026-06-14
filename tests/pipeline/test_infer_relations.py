"""Unit tests for the InferRelations pipeline step.

Regression anchor (prod 2026-06-13): the reinforce UPDATE used
``SET weight = LEAST(:new_weight, :max_weight)``. Postgres resolves
``LEAST`` over two untyped bind params as ``text`` and then rejects the
assignment to the double-precision ``weight`` column —
``asyncpg.exceptions.DatatypeMismatchError: column "weight" is of type
double precision but expression is of type text`` — so every reinforce
batch failed (45 occurrences in 9h across prod+staging; relations
silently not reinforced). ``new_weight`` is already clamped to
MAX_RELATION_WEIGHT in Python, so the fix binds it directly
(``SET weight = :new_weight``), matching the INSERT path's ``:weight``.

Mock-based — these never execute real SQL, so they assert the statement
SHAPE + the Python clamp the fix now relies on.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from core_api.constants import MAX_RELATION_WEIGHT
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepOutcome
from core_api.pipeline.steps.entity_linking.infer_relations import InferRelations

TENANT = "test-tenant"


def _mock_result(rows, rowcount: int | None = None):
    m = MagicMock()
    m.all.return_value = rows
    m.rowcount = rowcount if rowcount is not None else len(rows)
    return m


def _ctx(db, **extra):
    return PipelineContext(db=db, data={"tenant_id": TENANT, **extra})


@pytest.mark.asyncio
async def test_reinforce_binds_new_weight_directly_no_least():
    """The reinforce UPDATE must bind ``:new_weight`` directly — never
    ``LEAST(:new_weight, :max_weight)`` (the text-type-coercion bug)."""
    from_id, to_id, rel_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    cooccurrences = [(from_id, to_id, 5)]  # cooccur=5
    existing = [(from_id, to_id, rel_id, 0.5)]  # current weight 0.5

    db = AsyncMock()
    db.execute.side_effect = [
        _mock_result(cooccurrences),  # 1. co-occurrence scan
        _mock_result(existing),  # 2. existing relations → reinforce path
        _mock_result([], rowcount=1),  # 3. UPDATE
    ]
    db.flush = AsyncMock()

    result = await InferRelations().execute(_ctx(db))

    assert result.outcome == StepOutcome.SUCCESS
    assert db.execute.await_count == 3  # no INSERT (pair already exists)

    update_call = db.execute.call_args_list[2]
    sql = update_call.args[0].text
    assert "SET weight = :new_weight" in sql
    assert "LEAST" not in sql

    batch = update_call.args[1]
    assert len(batch) == 1
    # new_weight = min(0.5 + 5*0.1, 1.0) = 1.0, and no stray max_weight param.
    assert batch[0]["new_weight"] == pytest.approx(1.0)
    assert "max_weight" not in batch[0]


@pytest.mark.asyncio
async def test_reinforce_weight_clamped_in_python():
    """The fix relies on the Python clamp (the SQL no longer clamps), so
    pin it: an over-budget reinforcement never exceeds MAX_RELATION_WEIGHT."""
    from_id, to_id, rel_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    # current 0.95 + cooccur 9 * 0.1 = 1.85 → must clamp to MAX (1.0).
    db = AsyncMock()
    db.execute.side_effect = [
        _mock_result([(from_id, to_id, 9)]),
        _mock_result([(from_id, to_id, rel_id, 0.95)]),
        _mock_result([], rowcount=1),
    ]
    db.flush = AsyncMock()

    await InferRelations().execute(_ctx(db))

    batch = db.execute.call_args_list[2].args[1]
    assert batch[0]["new_weight"] == pytest.approx(MAX_RELATION_WEIGHT)


@pytest.mark.asyncio
async def test_insert_path_unaffected():
    """New pairs (no existing relation) still INSERT with a direct
    ``:weight`` bind — unchanged by the fix."""
    from_id, to_id = uuid.uuid4(), uuid.uuid4()
    db = AsyncMock()
    db.execute.side_effect = [
        _mock_result([(from_id, to_id, 3)]),
        _mock_result([]),  # no existing → insert path
        _mock_result([], rowcount=1),  # INSERT
    ]
    db.flush = AsyncMock()

    result = await InferRelations().execute(_ctx(db))

    assert result.outcome == StepOutcome.SUCCESS
    assert db.execute.await_count == 3
    insert_sql = db.execute.call_args_list[2].args[0].text
    assert "INSERT INTO relations" in insert_sql
    assert ":weight" in insert_sql
    assert db.execute.call_args_list[2].args[1][0]["weight"] == pytest.approx(0.3)


@pytest.mark.asyncio
async def test_no_cooccurrences_skips():
    db = AsyncMock()
    db.execute.side_effect = [_mock_result([])]
    db.flush = AsyncMock()

    result = await InferRelations().execute(_ctx(db))

    assert result.outcome == StepOutcome.SKIPPED

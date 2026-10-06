"""oss-0927-m-03 — the recorders that closed the gate's known-open entries.

``tests/test_m03_swallowed_failure_gate.py`` proves each fixed give-up now
leaves a ``background_task_log`` row. It does not prove the row is the RIGHT
row, nor that the writer stays silent when nothing failed — and a failure
table with a writer that fires on success is worse than no table. These are
the counterweights, one per new recorder:

* ``contradiction_stranded`` — ``detect_contradictions_async`` (Path A) and
  ``detect_contradictions_by_entities_async`` (Path C);
* ``fanout_stranded`` — ``_enrich_memory_background``'s atomic-fact fan-out.
"""

from __future__ import annotations

import uuid
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from common.enrichment.schema import AtomicFact, EnrichmentResult
from core_api.constants import VECTOR_DIM
from core_api.services import contradiction_detector as cd
from core_api.services import memory_service, task_tracker

pytestmark = pytest.mark.asyncio

TENANT = f"test-m03r-{uuid.uuid4().hex[:8]}"
_VEC = [0.0] * VECTOR_DIM


def _row(**over):
    base = {
        "id": str(uuid.uuid4()),
        "memory_type": "fact",
        "status": "active",
        "weight": 0.5,
        "ts_valid_start": None,
        "ts_valid_end": None,
        "metadata_": {},
        "deleted_at": None,
        "fleet_id": "f1",
        "embedding": None,
        "content": "body",
        "visibility": "scope_team",
    }
    base.update(over)
    return base


def _log_sink(stack: ExitStack) -> MagicMock:
    """Capture what ``record_task_failure`` sends to storage."""
    log_sc = MagicMock()
    log_sc.add_task_failure = AsyncMock()
    stack.enter_context(
        patch.object(task_tracker, "get_storage_client", return_value=log_sc)
    )
    return log_sc


def _broken_log_sink(stack: ExitStack) -> None:
    stack.enter_context(
        patch.object(
            task_tracker, "get_storage_client", side_effect=RuntimeError("down")
        )
    )


# ── contradiction detection ───────────────────────────────────────────────


def _detector_env(stack: ExitStack, *, get_memory: AsyncMock) -> None:
    sc = MagicMock()
    sc.get_memory = get_memory
    stack.enter_context(patch.object(cd, "get_storage_client", lambda: sc))
    stack.enter_context(
        patch.object(cd, "_acquire_content_lock", AsyncMock(return_value=True))
    )
    stack.enter_context(patch.object(cd, "_release_lock", AsyncMock()))
    stack.enter_context(
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=SimpleNamespace()),
        )
    )


async def _path_a(memory_id: uuid.UUID) -> None:
    await cd.detect_contradictions_async(memory_id, TENANT, "f1", "c", _VEC)


async def _path_c(memory_id: uuid.UUID) -> None:
    await cd.detect_contradictions_by_entities_async(memory_id, TENANT, "f1")


@pytest.mark.parametrize(("run", "path"), [(_path_a, "content"), (_path_c, "entity")])
async def test_a_failed_detection_is_recorded(run, path) -> None:
    memory_id = uuid.uuid4()
    with ExitStack() as stack:
        _detector_env(stack, get_memory=AsyncMock(side_effect=RuntimeError("x")))
        log_sc = _log_sink(stack)
        await run(memory_id)

    log_sc.add_task_failure.assert_awaited_once()
    row = log_sc.add_task_failure.await_args.args[0]
    assert row["task_name"] == cd._CONTRADICTION_STRANDED_TASK
    assert row["memory_id"] == str(memory_id)
    assert row["tenant_id"] == TENANT
    assert row["error_message"].startswith(f"{path} detection failed: RuntimeError")
    # Recorded after the ``finally``, outside the ``except`` — the traceback
    # must be the one captured inside it, not ``format_exc()``'s "NoneType".
    assert "NoneType: None" not in row["error_traceback"]
    assert "RuntimeError: x" in row["error_traceback"]


@pytest.mark.parametrize("run", [_path_a, _path_c])
async def test_the_detection_slot_is_free_before_recording(run) -> None:
    """The record is a storage write; holding the A19 slot across it would
    let a degraded storage stall every queued detection pass."""
    seen: list[int] = []

    async def record(_row):
        seen.append(cd._detection_gate()._value)

    with ExitStack() as stack:
        _detector_env(stack, get_memory=AsyncMock(side_effect=RuntimeError("x")))
        log_sc = _log_sink(stack)
        log_sc.add_task_failure.side_effect = record
        before = cd._detection_gate()._value
        await run(uuid.uuid4())

    assert seen == [before]


async def test_a_concluded_path_a_run_records_nothing() -> None:
    with ExitStack() as stack:
        _detector_env(stack, get_memory=AsyncMock(return_value=_row()))
        stack.enter_context(patch.object(cd, "_detect", AsyncMock(return_value=[])))
        log_sc = _log_sink(stack)
        await _path_a(uuid.uuid4())

    log_sc.add_task_failure.assert_not_awaited()


@pytest.mark.parametrize("run", [_path_a, _path_c])
async def test_a_deleted_row_records_nothing(run) -> None:
    """The row went away under us — nothing to check, not a failure."""
    with ExitStack() as stack:
        _detector_env(
            stack, get_memory=AsyncMock(return_value=_row(deleted_at="2026-01-01"))
        )
        log_sc = _log_sink(stack)
        await run(uuid.uuid4())

    log_sc.add_task_failure.assert_not_awaited()


async def test_a_lock_skipped_path_c_run_records_nothing() -> None:
    """A duplicate delivery that lost the idempotency lock is the lock doing
    its job, and the common case on the back-channel."""
    with ExitStack() as stack:
        _detector_env(stack, get_memory=AsyncMock(return_value=_row()))
        stack.enter_context(
            patch.object(cd, "_acquire_entity_lock", AsyncMock(return_value=False))
        )
        log_sc = _log_sink(stack)
        await _path_c(uuid.uuid4())

    log_sc.add_task_failure.assert_not_awaited()


@pytest.mark.parametrize("run", [_path_a, _path_c])
async def test_detection_recording_does_not_break_fire_and_forget(run) -> None:
    with ExitStack() as stack:
        _detector_env(stack, get_memory=AsyncMock(side_effect=RuntimeError("x")))
        _broken_log_sink(stack)
        # No exception escapes.
        await run(uuid.uuid4())


# ── atomic-fact fan-out ───────────────────────────────────────────────────


def _fanout_env(stack: ExitStack, fan_out: AsyncMock) -> None:
    sc = MagicMock()
    sc.get_memory = AsyncMock(return_value=_row())
    sc.update_memory = AsyncMock()
    sc.update_memory_status = AsyncMock()
    config = SimpleNamespace(
        enrichment_enabled=True,
        enrichment_provider="fake",
        entity_extraction_enabled=False,
        auto_chunk_enabled=False,
        atomic_fact_fanout_enabled=True,
        entity_blocklist=[],
    )
    result = EnrichmentResult(
        title="t", atomic_facts=[AtomicFact(content="a"), AtomicFact(content="b")]
    )
    for ctx in (
        patch.object(memory_service, "get_storage_client", lambda: sc),
        patch.object(memory_service, "track_task", MagicMock()),
        patch.object(memory_service, "fan_out_atomic_facts", new=fan_out),
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=config),
        ),
        patch(
            "core_api.services.memory_enrichment.enrich_memory",
            new=AsyncMock(return_value=result),
        ),
    ):
        stack.enter_context(ctx)


async def _enrich(memory_id: uuid.UUID):
    return await memory_service._enrich_memory_background(
        memory_id, "body", TENANT, "f1", "a"
    )


async def test_a_failed_fanout_is_recorded_against_the_parent() -> None:
    memory_id = uuid.uuid4()
    with ExitStack() as stack:
        _fanout_env(stack, AsyncMock(side_effect=RuntimeError("children lost")))
        log_sc = _log_sink(stack)
        await _enrich(memory_id)

    log_sc.add_task_failure.assert_awaited_once()
    row = log_sc.add_task_failure.await_args.args[0]
    assert row["task_name"] == memory_service._FANOUT_STRANDED_TASK
    assert row["memory_id"] == str(memory_id)
    assert row["tenant_id"] == TENANT
    assert row["error_message"] == "children lost"


async def test_a_successful_fanout_records_nothing() -> None:
    fan_out = AsyncMock()
    with ExitStack() as stack:
        _fanout_env(stack, fan_out)
        log_sc = _log_sink(stack)
        await _enrich(uuid.uuid4())

    fan_out.assert_awaited_once()
    log_sc.add_task_failure.assert_not_awaited()


async def test_fanout_recording_does_not_break_fire_and_forget() -> None:
    with ExitStack() as stack:
        _fanout_env(stack, AsyncMock(side_effect=RuntimeError("children lost")))
        _broken_log_sink(stack)
        # No exception escapes, and the governed row still comes back: the
        # parent WAS enriched, whatever happened to its children.
        assert await _enrich(uuid.uuid4()) is not None

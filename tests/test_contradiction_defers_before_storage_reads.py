"""A contradiction check its judge's budget cannot serve defers before reading storage.

Both detectors query for candidates before their judge, and a judge refused by
the tenant's background budget (#2418) defers the whole run. Its replay then
queried again, as often as once a second while the tenant's model slots stayed
busy: one memory's entity-overlap query ran 301 times in 48 hours on the read
pool. The detectors now ask first (``require_background_admission``) and defer
before any storage read. Storage backs consecutive deferrals off; that half is
tested against PostgreSQL in ``core-storage-api/tests/test_background_work_lifecycle.py``.
"""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from common.background_work import BackgroundWorkDeferred
from common.llm import retry
from common.usage import usage_scope
from core_api.constants import VECTOR_DIM
from core_api.services import background_retry as replay
from core_api.services import contradiction_detector as cd
from core_api.services import task_tracker

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

TENANT = f"test-precheck-{uuid.uuid4().hex[:8]}"
_BUSY = {"allowed": False, "retry_after_seconds": 1, "reason": "tenant_slots_busy"}


# ── the check ─────────────────────────────────────────────────────────────


async def test_a_refused_check_defers_with_the_answer_storage_gave(monkeypatch, caplog):
    check = AsyncMock(return_value=_BUSY)
    monkeypatch.setattr(retry, "check_background_admission", check)
    with caplog.at_level(logging.INFO, logger=retry.logger.name):
        with usage_scope(
            tenant_id="tenant-a", operation="contradiction", background=True
        ):
            with pytest.raises(BackgroundWorkDeferred) as info:
                await retry.require_background_admission()
    check.assert_awaited_once_with(tenant_id="tenant-a")
    assert (info.value.reason, info.value.retry_after_seconds) == (
        "tenant_slots_busy",
        1,
    )
    [record] = [
        r for r in caplog.records if r.getMessage() == "background model work deferred"
    ]
    assert record.stage == "precheck"


async def test_a_spent_allowance_defers_until_the_window_frees(monkeypatch):
    monkeypatch.setattr(
        retry,
        "check_background_admission",
        AsyncMock(return_value={"allowed": False, "retry_after_seconds": 37}),
    )
    with usage_scope(tenant_id="tenant-a", operation="contradiction", background=True):
        with pytest.raises(BackgroundWorkDeferred) as info:
            await retry.require_background_admission()
    assert (info.value.reason, info.value.retry_after_seconds) == (
        "budget_exhausted",
        37,
    )


@pytest.mark.parametrize(
    "answer",
    [
        {"allowed": True, "retry_after_seconds": 0},
        None,
        {},
        {"allowed": 0},
        {"allowed": False, "retry_after_seconds": -1},
        RuntimeError("storage unavailable"),
        TimeoutError(),
    ],
    ids=[
        "allowed",
        "no-checker",
        "empty",
        "not-a-bool",
        "bad-delay",
        "error",
        "timeout",
    ],
)
async def test_the_check_fails_open(monkeypatch, answer):
    """The reservation still gates the model call, so an unclear answer is not a refusal."""
    check = (
        AsyncMock(side_effect=answer)
        if isinstance(answer, BaseException)
        else AsyncMock(return_value=answer)
    )
    monkeypatch.setattr(retry, "check_background_admission", check)
    with usage_scope(tenant_id="tenant-a", operation="contradiction", background=True):
        await retry.require_background_admission()
    check.assert_awaited_once()


async def test_foreground_work_is_not_checked(monkeypatch):
    check = AsyncMock(return_value=_BUSY)
    monkeypatch.setattr(retry, "check_background_admission", check)
    with usage_scope(tenant_id="tenant-a", operation="contradiction", background=False):
        await retry.require_background_admission()
    check.assert_not_awaited()


# ── the detectors ─────────────────────────────────────────────────────────


@pytest.fixture
def busy(monkeypatch):
    """A tenant whose model slots are all busy, and a storage client that records every call."""
    monkeypatch.setattr(cd.settings, "contradiction_provider", "openai")
    check = AsyncMock(return_value=_BUSY)
    monkeypatch.setattr(retry, "check_background_admission", check)
    sc = AsyncMock()
    sc.defer_background_work.return_value = {"queued": True}
    sc.finish_background_work.return_value = {"finished": True}
    sc.is_tenant_suppressed.return_value = False
    sc.find_entity_overlap_candidates.return_value = []
    sc.find_similar_candidates.return_value = []
    monkeypatch.setattr(cd, "get_storage_client", lambda: sc)
    monkeypatch.setattr(replay, "get_storage_client", lambda: sc)
    monkeypatch.setattr(task_tracker, "get_storage_client", lambda: sc)
    monkeypatch.setattr(cd, "_acquire_entity_lock", AsyncMock(return_value=True))
    monkeypatch.setattr(cd, "_acquire_content_lock", AsyncMock(return_value=True))
    monkeypatch.setattr(cd, "_release_lock", AsyncMock())
    with patch(
        "core_api.services.organization_settings.resolve_config",
        new=AsyncMock(return_value=SimpleNamespace()),
    ):
        yield sc, check


def _row(memory_id: uuid.UUID) -> dict:
    return {
        "id": str(memory_id),
        "tenant_id": TENANT,
        "fleet_id": "f1",
        "agent_id": "writer",
        "content": "Alice lives in Boston",
        "memory_type": "fact",
        "status": "active",
        "visibility": "scope_team",
        "deleted_at": None,
    }


def _storage_calls(sc) -> list[str]:
    return [name for name, *_ in sc.mock_calls]


async def test_path_c_defers_before_any_storage_read(busy):
    sc, check = busy
    memory_id = uuid.uuid4()
    sc.get_memory.return_value = _row(memory_id)
    await cd.detect_contradictions_by_entities_async(memory_id, TENANT, "f1")
    assert _storage_calls(sc) == ["defer_background_work"]
    check.assert_awaited_once_with(tenant_id=TENANT)
    deferred = sc.defer_background_work.await_args.args[0]
    assert (deferred["kind"], deferred["retry_after_seconds"]) == (
        "contradiction_entity",
        1,
    )


async def test_path_a_defers_before_any_storage_read(busy):
    sc, check = busy
    memory_id = uuid.uuid4()
    sc.get_memory.return_value = _row(memory_id)
    await cd.detect_contradictions_async(
        memory_id, TENANT, "f1", "Alice lives in Boston", [0.0] * VECTOR_DIM
    )
    assert _storage_calls(sc) == ["defer_background_work"]
    check.assert_awaited_once_with(tenant_id=TENANT)
    assert (
        sc.defer_background_work.await_args.args[0]["kind"] == "contradiction_content"
    )


async def test_a_replayed_check_finishes_deferred_without_querying_again(busy):
    """The loop itself: a replay meets busy slots, reads only what replay reads, and
    hands the deferral to storage, which decides when it is due again."""
    sc, check = busy
    memory_id = uuid.uuid4()
    sc.get_memory.return_value = _row(memory_id)
    work = {
        "tenant_id": TENANT,
        "memory_id": str(memory_id),
        "kind": "contradiction_entity",
        "generation": 3,
    }
    await replay.retry_one(work)
    # Replay's own liveness reads, then its completion. Nothing of the detector's.
    assert _storage_calls(sc) == [
        "is_tenant_suppressed",
        "get_memory",
        "finish_background_work",
    ]
    check.assert_awaited_once()
    finished = sc.finish_background_work.await_args.args[0]
    assert (finished["retry"], finished["deferred"], finished["generation"]) == (
        True,
        True,
        3,
    )


@pytest.mark.parametrize("provider", ["none", "fake"])
async def test_a_judge_that_calls_no_model_is_not_checked(busy, monkeypatch, provider):
    """``none`` and ``fake`` never reserve a slot, so busy slots must not defer them."""
    sc, check = busy
    monkeypatch.setattr(cd.settings, "contradiction_provider", provider)
    sc.get_memory.return_value = None  # ends the run right after its first read
    await cd.detect_contradictions_by_entities_async(uuid.uuid4(), TENANT, "f1")
    check.assert_not_awaited()
    sc.get_memory.assert_awaited_once()
    sc.defer_background_work.assert_not_awaited()

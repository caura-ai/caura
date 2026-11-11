"""A hot tenant has a bounded queue and cannot occupy every detection slot."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from common.background_work import BackgroundWorkDeferred
from core_api.services import background_retry as replay
from core_api.services import contradiction_detector as detector

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.mark.parametrize("kind", ["content", "entity"])
async def test_detection_releases_capacity_before_persisting_a_deferral(
    harness, monkeypatch, kind
):
    entered, release = asyncio.Event(), asyncio.Event()

    async def persist(_payload):
        entered.set()
        await release.wait()
        return {"queued": True}

    harness.defer_background_work.side_effect = persist
    harness.get_memory.side_effect = BackgroundWorkDeferred("budget_exhausted")
    mid = uuid4()
    if kind == "content":
        run = detector.detect_contradictions_async(
            mid, "tenant-a", "fleet", "body", [0.1] * 8
        )
    else:
        run = detector.detect_contradictions_by_entities_async(mid, "tenant-a", "fleet")
    task = asyncio.create_task(run)
    try:
        await asyncio.wait_for(entered.wait(), 1)
        permit = await asyncio.wait_for(
            detector._detection_gate().acquire("tenant-b"), 0.1
        )
        permit.release()
    finally:
        release.set()
        await asyncio.gather(task)


async def _turns():
    for _ in range(40):
        await asyncio.sleep(0)


async def test_a_hundred_item_bulk_drains_shed_references_in_bounded_turns(
    harness, monkeypatch
):
    monkeypatch.setattr(detector.settings, "contradiction_detection_concurrency", 4)
    monkeypatch.setattr(
        detector.settings, "contradiction_detection_per_tenant_concurrency", 4
    )
    monkeypatch.setattr(detector.settings, "contradiction_detection_tenant_backlog", 64)
    monkeypatch.setattr(
        detector.settings, "contradiction_detection_global_backlog", 256
    )
    release = asyncio.Event()

    async def detect(*_a, **_kw):
        await release.wait()
        return []

    monkeypatch.setattr(detector, "_detect", detect)
    tasks = [_run("hot") for _ in range(100)]
    try:
        await _turns()
        queue = [call.args[0] for call in harness.defer_background_work.await_args_list]
        assert len(queue) == 32
    finally:
        release.set()
        await asyncio.gather(*tasks)

    async def due(*, after_tenant=None, limit=32):
        return ["hot"] if queue and after_tenant is None else []

    async def claim(_tenant, *, limit=1):
        picked = queue[:limit]
        del queue[:limit]
        return picked

    harness.list_due_background_tenants = due
    harness.claim_background_work = claim
    monkeypatch.setattr(replay, "get_storage_client", lambda: harness)
    done = asyncio.Event()
    processed = []

    async def one(work):
        processed.append(work)
        if len(processed) == 32:
            done.set()

    monkeypatch.setattr(replay, "retry_one", one)
    sleep = asyncio.sleep
    turns = []

    async def tick(delay):
        turns.append(delay)
        await sleep(0)

    monkeypatch.setattr(replay.asyncio, "sleep", tick)
    task = asyncio.create_task(replay.retry_loop())
    try:
        await asyncio.wait_for(done.wait(), 1)
        assert len(turns) <= 10
        assert all(delay == 1 for delay in turns)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.fixture
def harness(monkeypatch):
    monkeypatch.setattr(detector, "_DETECTION_GATE", None)
    monkeypatch.setattr(detector.settings, "contradiction_detection_concurrency", 1)
    for key, value in {
        "contradiction_detection_per_tenant_concurrency": 1,
        "contradiction_detection_tenant_backlog": 2,
        "contradiction_detection_global_backlog": 4,
    }.items():
        monkeypatch.setitem(detector.settings.__dict__, key, value)
    monkeypatch.setattr(
        detector, "_detection_disabled_for_tenant", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(detector, "cache_set_nx", AsyncMock(return_value=True))
    monkeypatch.setattr(detector, "cache_delete_if", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "core_api.services.organization_settings.resolve_config",
        AsyncMock(return_value=None),
    )
    sc = MagicMock()
    sc.defer_background_work = AsyncMock(return_value={"queued": True})
    sc.get_memory = AsyncMock(
        side_effect=lambda mid, tenant, **_kw: _memory(mid, tenant)
    )
    monkeypatch.setattr(detector, "get_storage_client", lambda: sc)
    return sc


def _memory(mid, tenant):
    return {
        "id": str(mid),
        "tenant_id": tenant,
        "fleet_id": "fleet",
        "agent_id": "writer",
        "content": "synthetic fact",
        "status": "active",
        "visibility": "scope_team",
        "deleted_at": None,
        "supersedes_id": None,
    }


def _run(tenant):
    mid = uuid4()
    return asyncio.create_task(
        detector.detect_contradictions_async(
            mid,
            tenant,
            "fleet",
            "synthetic fact",
            [0.1] * 8,
            new_memory=_memory(mid, tenant),
        )
    )


async def test_another_tenant_can_run_while_the_hot_tenant_is_busy(
    harness, monkeypatch
):
    monkeypatch.setattr(detector.settings, "contradiction_detection_concurrency", 2)
    release = asyncio.Event()
    entered = []

    async def detect(memory, *_a, **_kw):
        entered.append(memory["tenant_id"])
        await release.wait()
        return []

    monkeypatch.setattr(detector, "_detect", detect)
    tasks = [_run("hot"), _run("hot")]
    await _turns()
    tasks.append(_run("calm"))
    try:
        await _turns()
        before_release = list(entered)
    finally:
        release.set()
        await asyncio.gather(*tasks)

    assert before_release.count("hot") == 1, "one tenant occupied every global slot"
    assert "calm" in before_release, "the other tenant waited for the hot tenant"


async def test_round_robin_precedes_a_hot_tenants_existing_backlog(
    harness, monkeypatch
):
    monkeypatch.setitem(
        detector.settings.__dict__, "contradiction_detection_tenant_backlog", 20
    )
    monkeypatch.setitem(
        detector.settings.__dict__, "contradiction_detection_global_backlog", 30
    )
    first_release = asyncio.Event()
    entered = []

    async def detect(memory, *_a, **_kw):
        entered.append(memory["tenant_id"])
        if len(entered) == 1:
            await first_release.wait()
        return []

    monkeypatch.setattr(detector, "_detect", detect)
    tasks = [_run("hot")]
    await _turns()
    tasks += [_run("hot") for _ in range(5)]
    await _turns()
    tasks.append(_run("calm"))
    try:
        await _turns()
    finally:
        first_release.set()
        await asyncio.gather(*tasks)

    assert "calm" in entered
    assert entered.index("calm") <= 2, f"global FIFO delayed a tenant behind {entered}"


async def test_a_full_tenant_queue_sheds_to_durable_references(harness, monkeypatch):
    release = asyncio.Event()

    async def detect(*_a, **_kw):
        await release.wait()
        return []

    monkeypatch.setattr(detector, "_detect", detect)
    tasks = [_run("hot")]
    await _turns()
    tasks += [_run("hot") for _ in range(8)]
    try:
        await _turns()
        finished = sum(t.done() for t in tasks)
        deferred = list(harness.defer_background_work.await_args_list)
    finally:
        release.set()
        await asyncio.gather(*tasks)

    assert finished >= 6, "excess detection coroutines remained in the unbounded queue"
    assert len(deferred) >= 6, "shed work had no durable retry reference"
    for call in deferred:
        payload = call.args[0] if call.args else call.kwargs
        assert payload["tenant_id"] == "hot"
        assert payload["memory_id"]
        assert "content" not in payload and "embedding" not in payload


async def test_the_global_backlog_is_bounded_across_distinct_tenants(
    harness, monkeypatch
):
    release = asyncio.Event()

    async def detect(*_a, **_kw):
        await release.wait()
        return []

    monkeypatch.setattr(detector, "_detect", detect)
    tasks = [_run("first")]
    await _turns()
    tasks += [_run(f"tenant-{n}") for n in range(12)]
    try:
        await _turns()
        finished = sum(t.done() for t in tasks)
        deferred = harness.defer_background_work.await_count
    finally:
        release.set()
        await asyncio.gather(*tasks)

    assert finished >= 8, "distinct tenant ids bypassed the global backlog bound"
    assert deferred >= 8, "globally shed detections were not retained for retry"

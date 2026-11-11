"""Lease cleanup, reference-only replay, and admission failures need no database."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from common.background_work import BackgroundWorkDeferred
from common.llm import retry
from common.usage import usage_scope
from core_api.services import background_retry as replay
from core_api.services.background_admission import FairGate

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
async def test_a_reserved_provider_slot_is_released_on_every_exit(monkeypatch, outcome):
    reserve = AsyncMock(return_value={"allowed": True})
    release = AsyncMock()
    monkeypatch.setattr(retry, "reserve_background_attempt", reserve)
    monkeypatch.setattr(retry, "release_background_attempt", release)

    async def provider():
        if outcome == "failure":
            raise RuntimeError("synthetic provider failure")
        if outcome == "cancel":
            raise asyncio.CancelledError
        return "answer"

    with usage_scope(tenant_id="tenant-a", operation="enrichment", background=True):
        if outcome == "success":
            assert (
                await retry.call_with_retry(provider, "test", max_attempts=1)
                == "answer"
            )
        else:
            with pytest.raises(
                RuntimeError if outcome == "failure" else asyncio.CancelledError
            ):
                await retry.call_with_retry(provider, "test", max_attempts=1)
    release.assert_awaited_once()
    assert (
        release.call_args.kwargs["attempt_id"] == reserve.call_args.kwargs["attempt_id"]
    )
    assert release.call_args.kwargs["tenant_id"] == "tenant-a"


async def test_a_lost_slot_release_does_not_repeat_the_provider_call(monkeypatch):
    monkeypatch.setattr(
        retry, "reserve_background_attempt", AsyncMock(return_value={"allowed": True})
    )
    monkeypatch.setattr(
        retry,
        "release_background_attempt",
        AsyncMock(side_effect=RuntimeError("unavailable")),
    )
    provider = AsyncMock(return_value="answer")
    with usage_scope(tenant_id="tenant-a", operation="enrichment", background=True):
        assert await retry.call_with_retry(provider, "test", max_attempts=3) == "answer"
    provider.assert_awaited_once()


async def test_a_transient_release_failure_is_retried_without_repeating_the_provider(
    monkeypatch,
):
    monkeypatch.setattr(
        retry, "reserve_background_attempt", AsyncMock(return_value={"allowed": True})
    )
    release = AsyncMock(side_effect=[RuntimeError("unavailable"), {"released": True}])
    monkeypatch.setattr(retry, "release_background_attempt", release)
    provider = AsyncMock(return_value="answer")
    with usage_scope(tenant_id="tenant-a", operation="enrichment", background=True):
        assert await retry.call_with_retry(provider, "test", max_attempts=3) == "answer"
    provider.assert_awaited_once()
    assert release.await_count == 2
    assert release.await_args_list[0].kwargs == release.await_args_list[1].kwargs


@pytest.mark.parametrize(
    "decision", [{}, {"allowed": 1}, {"allowed": False, "retry_after_seconds": -1}]
)
async def test_malformed_authority_responses_never_authorize_calls(
    monkeypatch, decision
):
    monkeypatch.setattr(
        retry, "reserve_background_attempt", AsyncMock(return_value=decision)
    )
    provider = AsyncMock()
    with usage_scope(tenant_id="tenant-a", operation="enrichment", background=True):
        with pytest.raises(BackgroundWorkDeferred, match="budget_unavailable"):
            await retry.call_with_retry(provider, "test", max_attempts=1)
    provider.assert_not_awaited()


@pytest.fixture
def queued(monkeypatch):
    work = {
        "tenant_id": "tenant-a",
        "memory_id": str(uuid4()),
        "kind": "extraction",
        "generation": 4,
    }
    sc = MagicMock()
    sc.is_tenant_suppressed = AsyncMock(return_value=False)
    sc.get_memory = AsyncMock(
        return_value={
            "id": work["memory_id"],
            "tenant_id": "tenant-a",
            "fleet_id": "fleet",
            "agent_id": "writer",
            "content": "latest edited fact",
            "memory_type": "fact",
            "status": "active",
            "deleted_at": None,
        }
    )
    sc.finish_background_work = AsyncMock(return_value={"finished": True})
    sc.find_embedding_by_content_hash = AsyncMock(return_value=None)
    monkeypatch.setattr(replay, "get_storage_client", lambda: sc)
    return work, sc


@pytest.mark.parametrize("state", ["suppressed", "missing", "deleted", "quarantined"])
async def test_replay_discards_work_that_is_no_longer_live(queued, monkeypatch, state):
    work, sc = queued
    if state == "suppressed":
        sc.is_tenant_suppressed.return_value = True
    elif state == "missing":
        sc.get_memory.return_value = None
    else:
        sc.get_memory.return_value["status"] = state
    process = AsyncMock()
    monkeypatch.setattr(
        "core_api.services.entity_extraction_worker.process_entity_extraction", process
    )
    await replay.retry_one(work)
    process.assert_not_awaited()
    assert sc.finish_background_work.call_args.args[0]["retry"] is False


@pytest.mark.parametrize("outcome", ["model", "failed", "deferred"])
async def test_replay_rehydrates_current_content_and_finishes_only_its_generation(
    queued, monkeypatch, outcome
):
    work, sc = queued
    process = AsyncMock(return_value=outcome)
    monkeypatch.setattr(
        "core_api.services.entity_extraction_worker.process_entity_extraction", process
    )
    await replay.retry_one(work)
    assert process.call_args.args[4] == "latest edited fact"
    sc.get_memory.assert_awaited_once_with(work["memory_id"], "tenant-a", read=False)
    finished = sc.finish_background_work.call_args.args[0]
    assert finished["generation"] == 4
    assert finished["retry"] is (outcome in {"failed", "deferred"})
    assert "content" not in finished


async def test_pending_embedding_keeps_the_reference_for_retry(queued):
    work, sc = queued
    work["kind"] = "contradiction_content"
    await replay.retry_one(work)
    assert sc.finish_background_work.call_args.args[0]["retry"] is True
    assert sc.finish_background_work.call_args.args[0]["deferred"] is True
    assert sc.finish_background_work.call_args.args[0]["retry_after_seconds"] == 60


async def test_a_failed_new_deferral_cannot_discard_an_existing_durable_reference(
    queued, monkeypatch
):
    work, sc = queued
    work["kind"] = "contradiction_entity"
    sc.defer_background_work = AsyncMock(
        side_effect=RuntimeError("storage unavailable")
    )

    async def detect(*_a, **_kw):
        await replay.defer_work(
            sc,
            "tenant-a",
            work["memory_id"],
            work["kind"],
            BackgroundWorkDeferred("budget_exhausted"),
        )

    monkeypatch.setattr(
        "core_api.services.contradiction.run_contradiction_detection", detect
    )
    await replay.retry_one(work)
    assert sc.finish_background_work.call_args.args[0]["retry"] is True


async def test_cancellation_leaves_the_durable_claim_to_expire(queued):
    work, sc = queued
    entered = asyncio.Event()

    async def blocked(*_a, **_kw):
        entered.set()
        await asyncio.Event().wait()

    sc.get_memory.side_effect = blocked
    task = asyncio.create_task(replay.retry_one(work))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    sc.finish_background_work.assert_not_awaited()


async def test_defer_failure_is_explicit_and_never_breaks_the_committed_write(caplog):
    sc = MagicMock()
    sc.defer_background_work = AsyncMock(
        side_effect=RuntimeError("synthetic body to withhold")
    )
    await replay.defer_work(
        sc,
        "tenant-a",
        uuid4(),
        "extraction",
        BackgroundWorkDeferred("budget_exhausted"),
    )
    assert "retry_unavailable" in [getattr(r, "reason", None) for r in caplog.records]
    assert "synthetic body to withhold" not in caplog.text


async def test_cancelling_a_waiter_frees_queue_capacity():
    gate = FairGate(1, 1, 1, 1)
    permit = await gate.acquire("tenant-a")
    queued = asyncio.create_task(gate.acquire("tenant-a"))
    await asyncio.sleep(0)
    queued.cancel()
    await asyncio.gather(queued, return_exceptions=True)
    assert gate.queued == 0
    permit.release()
    another = await asyncio.wait_for(gate.acquire("tenant-b"), 1)
    another.release()
    assert gate.active == 0 and gate.by_tenant == {}


async def test_a_full_hot_queue_cannot_block_an_unused_slot_for_another_tenant():
    gate = FairGate(2, 1, 1, 1)
    first = await gate.acquire("hot")
    waiting = asyncio.create_task(gate.acquire("hot"))
    await asyncio.sleep(0)
    try:
        other = await asyncio.wait_for(gate.acquire("calm"), 1)
        other.release()
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        first.release()


async def test_a_slow_retry_does_not_block_the_next_tenants_turn(monkeypatch):
    tenants = [f"tenant-{n:02d}" for n in range(20)]
    active_tenants = AsyncMock(
        side_effect=AssertionError("must discover only due tenants")
    )
    monkeypatch.setattr(
        "core_api.services.tenants.list_active_tenant_ids",
        active_tenants,
    )
    sc = MagicMock()

    async def due(*, after_tenant=None, limit=32):
        return [
            tenant
            for tenant in tenants
            if after_tenant is None or tenant > after_tenant
        ][:limit]

    sc.list_due_background_tenants = AsyncMock(side_effect=due)

    async def claim(tenant, *, limit=1):
        return [{"tenant_id": tenant}]

    sc.claim_background_work = claim
    monkeypatch.setattr(replay, "get_storage_client", lambda: sc)
    entered, release = asyncio.Event(), asyncio.Event()
    all_calm = asyncio.Event()
    seen = set()
    gauge = {"active": 0, "max": 0}

    async def one(work):
        gauge["active"] += 1
        gauge["max"] = max(gauge["max"], gauge["active"])
        try:
            tenant = work["tenant_id"]
            if tenant == tenants[0]:
                entered.set()
                await release.wait()
            else:
                seen.add(tenant)
                if len(seen) == len(tenants) - 1:
                    all_calm.set()
        finally:
            gauge["active"] -= 1

    monkeypatch.setattr(replay, "retry_one", one)
    sleep = asyncio.sleep

    async def tick(_delay):
        await sleep(0)

    monkeypatch.setattr(replay.asyncio, "sleep", tick)
    task = asyncio.create_task(replay.retry_loop())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(all_calm.wait(), 1)
        assert not release.is_set()
        active_tenants.assert_not_awaited()
        assert gauge["max"] <= 16
    finally:
        task.cancel()
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_replay_reschedules_its_claim_without_creating_a_new_generation(
    queued, monkeypatch
):
    work, sc = queued
    sc.defer_background_work = AsyncMock(return_value={"queued": True})

    async def extract(*_a, **_kw):
        await replay.defer_work(
            sc,
            "tenant-a",
            work["memory_id"],
            "extraction",
            BackgroundWorkDeferred("budget_exhausted", retry_after_seconds=300),
        )
        return "deferred"

    monkeypatch.setattr(
        "core_api.services.entity_extraction_worker.process_entity_extraction", extract
    )
    await replay.retry_one(work)
    sc.defer_background_work.assert_not_awaited()
    finished = sc.finish_background_work.call_args.args[0]
    assert finished["retry"] is True
    assert finished["generation"] == work["generation"]
    assert finished["retry_after_seconds"] == 300


@pytest.mark.parametrize(
    "reason", ["budget_exhausted", "tenant_slots_busy", "backlog_full"]
)
@pytest.mark.parametrize("outcome", ["deferred", "raised", "failed", "exception"])
async def test_only_scheduling_deferrals_are_exempt_from_the_failure_cap(
    queued, monkeypatch, reason, outcome
):
    work, sc = queued

    async def extract(*_a, **_kw):
        refusal = BackgroundWorkDeferred(reason, retry_after_seconds=3)
        if outcome == "raised":
            raise refusal
        await replay.defer_work(
            sc, work["tenant_id"], work["memory_id"], work["kind"], refusal
        )
        if outcome == "exception":
            raise RuntimeError("synthetic replay failure after deferral")
        return outcome

    monkeypatch.setattr(
        "core_api.services.entity_extraction_worker.process_entity_extraction", extract
    )
    await replay.retry_one(work)
    finished = sc.finish_background_work.call_args.args[0]
    assert finished["retry"] is True
    assert finished["deferred"] is (outcome in {"deferred", "raised"})
    assert finished["retry_after_seconds"] == 3


@pytest.mark.parametrize("kind", ["enrichment", "contradiction_entity"])
@pytest.mark.parametrize("failed", [False, True])
async def test_enrichment_and_contradiction_report_pure_admission_deferrals(
    queued, monkeypatch, kind, failed
):
    work, sc = queued
    work["kind"] = kind

    async def process(*_a, **_kw):
        await replay.defer_work(
            sc,
            work["tenant_id"],
            work["memory_id"],
            kind,
            BackgroundWorkDeferred("tenant_slots_busy", retry_after_seconds=2),
        )
        if failed:
            raise RuntimeError("synthetic failure after scheduling refusal")

    if kind == "enrichment":
        monkeypatch.setattr(
            "core_api.services.memory_service._schedule_enrich_or_inline", process
        )
        monkeypatch.setattr(
            "core_api.services.organization_settings.resolve_config",
            AsyncMock(return_value={}),
        )
    else:
        monkeypatch.setattr(
            "core_api.services.contradiction.run_contradiction_detection", process
        )
    await replay.retry_one(work)
    finished = sc.finish_background_work.call_args.args[0]
    assert finished["retry"] is True
    assert finished["deferred"] is not failed
    assert finished["retry_after_seconds"] == 2


@pytest.mark.parametrize("different", ["tenant_id", "memory_id", "kind"])
async def test_a_spawned_child_persists_work_for_a_different_claim(
    queued, monkeypatch, different
):
    work, sc = queued
    child_work = {
        **work,
        different: {
            "tenant_id": "tenant-b",
            "memory_id": str(uuid4()),
            "kind": "contradiction_entity",
        }[different],
    }
    sc.defer_background_work = AsyncMock(return_value={"queued": True})

    async def extract(*_a, **_kw):
        await asyncio.create_task(
            replay.defer_work(
                sc,
                child_work["tenant_id"],
                child_work["memory_id"],
                child_work["kind"],
                BackgroundWorkDeferred("budget_exhausted"),
            )
        )
        return "model"

    monkeypatch.setattr(
        "core_api.services.entity_extraction_worker.process_entity_extraction", extract
    )
    await replay.retry_one(work)
    sc.defer_background_work.assert_awaited_once()
    assert sc.defer_background_work.call_args.args[0] == {
        "tenant_id": child_work["tenant_id"],
        "memory_id": child_work["memory_id"],
        "kind": child_work["kind"],
        "retry_after_seconds": 60,
    }
    assert sc.finish_background_work.call_args.args[0]["retry"] is False


@pytest.mark.parametrize("phase", ["completed", "finishing", "cancelled"])
async def test_a_late_matching_child_persists_after_replay_closes(
    queued, monkeypatch, phase
):
    work, sc = queued
    sc.defer_background_work = AsyncMock(return_value={"queued": True})
    created, release_child, finishing, release_finish = (
        asyncio.Event() for _ in range(4)
    )
    children = []

    async def child():
        await release_child.wait()
        await replay.defer_work(
            sc,
            work["tenant_id"],
            work["memory_id"],
            work["kind"],
            BackgroundWorkDeferred("budget_exhausted"),
        )

    async def extract(*_a, **_kw):
        children.append(asyncio.create_task(child()))
        created.set()
        if phase == "cancelled":
            await asyncio.Event().wait()
        return "model"

    async def finish(_payload):
        finishing.set()
        if phase == "finishing":
            await release_finish.wait()
        return {"finished": True}

    sc.finish_background_work.side_effect = finish
    monkeypatch.setattr(
        "core_api.services.entity_extraction_worker.process_entity_extraction", extract
    )
    parent = asyncio.create_task(replay.retry_one(work))
    try:
        await asyncio.wait_for(created.wait(), 1)
        if phase == "cancelled":
            parent.cancel()
            await asyncio.gather(parent, return_exceptions=True)
            sc.finish_background_work.assert_not_awaited()
        elif phase == "finishing":
            await asyncio.wait_for(finishing.wait(), 1)
            assert not parent.done()
        else:
            await asyncio.wait_for(parent, 1)
        release_child.set()
        await asyncio.wait_for(children[0], 1)
        sc.defer_background_work.assert_awaited_once()
    finally:
        release_child.set()
        release_finish.set()
        if not parent.done():
            parent.cancel()
        await asyncio.gather(parent, *children, return_exceptions=True)


async def test_budget_fixture_does_not_capture_a_client_at_setup(monkeypatch):
    from types import SimpleNamespace

    from common import background_work
    from core_api.clients import storage_client
    from tests.conftest import _background_budget_authority

    first, second = MagicMock(), MagicMock()
    for client in (first, second):
        client.reserve_background_attempt = AsyncMock(return_value={"allowed": True})
        client.release_background_attempt = AsyncMock(return_value={"released": True})
    getter = MagicMock(return_value=first)
    monkeypatch.setattr(storage_client, "get_storage_client", getter)
    request = SimpleNamespace(node=SimpleNamespace(get_closest_marker=lambda *_a: None))
    previous = background_work._reserver, background_work._releaser
    fixture = _background_budget_authority.__wrapped__(request, None)
    next(fixture)
    try:
        getter.assert_not_called()
        await background_work._reserver(tenant_id="tenant-a")
        getter.return_value = second
        await background_work._releaser(tenant_id="tenant-a")
        first.reserve_background_attempt.assert_awaited_once()
        first.release_background_attempt.assert_not_awaited()
        second.release_background_attempt.assert_awaited_once()
    finally:
        fixture.close()
    assert (background_work._reserver, background_work._releaser) == previous


async def test_due_discovery_uses_bounded_primary_query_parameters(monkeypatch):
    from core_api.clients.storage_client import CoreStorageClient

    sc = object.__new__(CoreStorageClient)
    get = AsyncMock(return_value={"tenant_ids": ["tenant-b"]})
    monkeypatch.setattr(sc, "_get", get)
    assert await sc.list_due_background_tenants(after_tenant="tenant-a", limit=1) == [
        "tenant-b"
    ]
    get.assert_awaited_once_with(
        "/tenants/active",
        read=False,
        background_due=True,
        after_tenant="tenant-a",
        limit=1,
    )


@pytest.mark.parametrize("per_tenant", [1, 4, 8])
async def test_replay_pool_keeps_capacity_for_cold_tenants(monkeypatch, per_tenant):
    monkeypatch.setattr(
        replay.settings, "background_retry_per_tenant_concurrency", per_tenant
    )
    available = ["hot"]
    sc = MagicMock()

    async def due(*, after_tenant=None, limit=32):
        return sorted(t for t in available if after_tenant is None or t > after_tenant)[
            :limit
        ]

    async def claim(tenant, *, limit=1):
        return [{"tenant_id": tenant} for _ in range(limit)]

    sc.list_due_background_tenants, sc.claim_background_work = due, claim
    monkeypatch.setattr(replay, "get_storage_client", lambda: sc)
    held, full, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    running = {}

    async def one(work):
        tenant = work["tenant_id"]
        running[tenant] = running.get(tenant, 0) + 1
        if running.get("hot", 0) == per_tenant:
            held.set()
        if sum(running.values()) == 16:
            full.set()
        try:
            await release.wait()
        finally:
            running[tenant] -= 1

    monkeypatch.setattr(replay, "retry_one", one)
    sleep = asyncio.sleep

    async def tick(_delay):
        await sleep(0)

    monkeypatch.setattr(replay.asyncio, "sleep", tick)
    task = asyncio.create_task(replay.retry_loop())
    try:
        await asyncio.wait_for(held.wait(), 1)
        assert running["hot"] == per_tenant
        available.extend(f"cold-{n:02d}" for n in range(20))
        await asyncio.wait_for(full.wait(), 1)
        assert sum(running.values()) == 16
        assert running["hot"] == per_tenant
        assert all(count <= per_tenant for count in running.values())
    finally:
        task.cancel()
        release.set()
        await asyncio.gather(task, return_exceptions=True)

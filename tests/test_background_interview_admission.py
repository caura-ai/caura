"""Async interview jobs use independent tenant slots and shared model admission."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from common.background_work import BackgroundWorkDeferred
from core_api.middleware import per_tenant_concurrency as slots
from core_api.routes import interview as route
from core_api.services import interview_service as service

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_foreground_pressure_does_not_consume_background_job_capacity(
    fresh_slots, jobs
):
    async with slots.per_tenant_slot("llm", "tenant-a"):
        await service.process_interview_job("tenant-a", "job")
    jobs.synthesize.assert_awaited_once()


async def test_a_background_job_leaves_foreground_capacity_free(fresh_slots, jobs):
    entered, release = asyncio.Event(), asyncio.Event()

    async def synthesize(**_kw):
        entered.set()
        await release.wait()
        return {"status": "committed", "memories_written": 0}

    jobs.synthesize.side_effect = synthesize
    task = asyncio.create_task(service.process_interview_job("tenant-a", "job"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        async with slots.per_tenant_slot("llm", "tenant-a"):
            assert not task.done()
    finally:
        release.set()
        await asyncio.gather(task)


@pytest.mark.parametrize("failure", ["once", "always", "cancel"])
async def test_deferral_refund_failure_preserves_the_job_contract(
    fresh_slots, jobs, caplog, failure
):
    jobs.synthesize.side_effect = BackgroundWorkDeferred("budget_exhausted")
    sc = service.get_storage_client()
    original = sc.upsert_document.side_effect
    failures = []

    async def put(payload):
        if payload["data"]["status"] == "pending" and (
            not failures or failure == "always"
        ):
            failures.append(True)
            if failure == "cancel":
                raise asyncio.CancelledError
            raise RuntimeError("storage body to withhold")
        await original(payload)

    sc.upsert_document.side_effect = put
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await service.process_interview_job("tenant-a", "job")
    else:
        assert await service.process_interview_job("tenant-a", "job") is None
        assert any(
            getattr(r, "reason", None) == "refund_unavailable" for r in caplog.records
        )
        assert "storage body to withhold" not in caplog.text
    if failure != "always":
        assert jobs.state("tenant-a", "job")["status"] == "pending"
        assert jobs.state("tenant-a", "job")["attempts"] == 2
    else:
        assert len(failures) >= 2


@pytest.fixture
def fresh_slots(monkeypatch):
    slots._reset_for_tests()
    monkeypatch.setattr(slots.settings, "per_tenant_llm_concurrency", 1)
    monkeypatch.setattr(slots.settings, "per_tenant_background_llm_concurrency", 1)
    monkeypatch.setattr(slots.settings, "per_tenant_acquire_timeout_seconds", 0.01)
    monkeypatch.setattr(route, "synthesis_sem", asyncio.Semaphore(10))
    yield
    slots._reset_for_tests()


@pytest.fixture
def jobs(monkeypatch):
    states = {}

    def state(tenant, doc):
        return states.setdefault(
            (tenant, doc),
            {
                "status": "pending",
                "attempts": 2,
                "events": [{"content": "synthetic event"}],
            },
        )

    async def get(tenant, _collection, doc, **_kw):
        return {"data": deepcopy(state(tenant, doc))}

    async def put(payload):
        states[(payload["tenant_id"], payload["doc_id"])] = deepcopy(payload["data"])

    sc = MagicMock()
    sc.get_document = AsyncMock(side_effect=get)
    sc.upsert_document = AsyncMock(side_effect=put)
    sc.defer_background_work = AsyncMock(return_value={"queued": True})
    monkeypatch.setattr(service, "get_storage_client", lambda: sc)
    synthesize = AsyncMock(return_value={"status": "committed", "memories_written": 0})
    monkeypatch.setattr(service, "_synthesize_and_write", synthesize)
    return SimpleNamespace(state=state, synthesize=synthesize)


async def test_async_submit_does_not_synthesize_without_a_tenant_slot(
    fresh_slots, jobs
):
    async with slots.per_tenant_slot("background_llm", "tenant-a"):
        await route._bounded_process("tenant-a", "durable-job")
    assert jobs.synthesize.await_count == 0, (
        "async submit bypassed the occupied tenant model slot"
    )


async def test_a_busy_tenant_does_not_stop_another_tenants_job(fresh_slots, jobs):
    async with slots.per_tenant_slot("background_llm", "tenant-a"):
        await asyncio.gather(
            route._bounded_process("tenant-a", "blocked-job"),
            route._bounded_process("tenant-b", "other-job"),
        )
    assert [c.kwargs["tenant_id"] for c in jobs.synthesize.await_args_list] == [
        "tenant-b"
    ]


async def test_a_refused_slot_does_not_escape_the_accepted_background_task(
    fresh_slots, jobs, monkeypatch
):
    from fastapi import HTTPException

    @asynccontextmanager
    async def unavailable(*_a, **_kw):
        raise HTTPException(status_code=429, detail="tenant model slots busy")
        yield  # pragma: no cover

    monkeypatch.setattr(route, "per_tenant_slot", unavailable)
    monkeypatch.setattr(service, "per_tenant_slot", unavailable, raising=False)
    # The job is already durable. Returning leaves it for the sweep; propagating
    # the slot exception would classify accepted work as a failed request/task.
    await route._bounded_process("tenant-a", "durable-job")
    assert jobs.synthesize.await_count == 0


async def test_sweep_processor_deferral_preserves_the_job_and_attempt_count(
    fresh_slots, jobs
):
    original = deepcopy(jobs.state("tenant-a", "durable-job"))
    async with slots.per_tenant_slot("background_llm", "tenant-a"):
        await service.process_interview_job("tenant-a", "durable-job")

    assert jobs.synthesize.await_count == 0, "the sweep bypassed the tenant slot"
    state = jobs.state("tenant-a", "durable-job")
    assert state["status"] == "pending"
    assert state["attempts"] == original["attempts"], (
        "admission deferral spent a provider-failure attempt"
    )
    assert state["events"] == original["events"]


async def test_cancellation_releases_the_slot_after_holding_it_for_synthesis(
    fresh_slots, jobs
):
    observed = []

    async def cancel(**_kw):
        observed.append(slots._get_semaphore("background_llm", "tenant-a").locked())
        raise asyncio.CancelledError

    jobs.synthesize.side_effect = cancel
    with pytest.raises(asyncio.CancelledError):
        await route._bounded_process("tenant-a", "durable-job")
    assert observed == [True], "synthesis ran outside the tenant slot"
    assert not slots._get_semaphore("background_llm", "tenant-a").locked()


async def test_sweep_completes_a_deferred_job_after_its_tenant_slot_is_free(
    fresh_slots, jobs, monkeypatch
):
    monkeypatch.setattr(
        service,
        "list_tenants_with_interviewer_enabled",
        AsyncMock(return_value=["tenant-a", "tenant-b"]),
    )
    monkeypatch.setattr(
        service, "list_tenants_with_interviewer_disabled", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(service, "_purge_terminal_jobs", AsyncMock(return_value=0))

    async def query(payload):
        tenant = payload["tenant_id"]
        if jobs.state(tenant, "job")["status"] == payload["where"]["status"]:
            return [{"doc_id": "job", "data": deepcopy(jobs.state(tenant, "job"))}]
        return []

    service.get_storage_client().query_documents = AsyncMock(side_effect=query)
    before = deepcopy(jobs.state("tenant-a", "job"))
    async with slots.per_tenant_slot("background_llm", "tenant-a"):
        first = await service.process_pending_interview_jobs()

    assert first["jobs_done"] == 1
    assert first["jobs_skipped"] == 1
    assert jobs.state("tenant-a", "job") == before
    assert jobs.state("tenant-b", "job")["status"] == "done"
    second = await service.process_pending_interview_jobs()
    assert second["jobs_done"] == 1
    assert jobs.state("tenant-a", "job")["status"] == "done"
    assert [c.kwargs["tenant_id"] for c in jobs.synthesize.await_args_list] == [
        "tenant-b",
        "tenant-a",
    ]

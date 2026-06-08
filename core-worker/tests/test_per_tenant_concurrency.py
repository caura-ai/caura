"""Per-tenant storage slot in core-worker — semaphore behaviour."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from core_worker import per_tenant_concurrency
from core_worker.config import Settings, settings


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    per_tenant_concurrency._reset_for_tests()


@pytest.fixture
def cap() -> int:
    """Live cap reader, so a future test that monkeypatches the
    setting sees the override rather than an import-time snapshot."""
    return settings.per_tenant_storage_write_concurrency


@pytest.mark.asyncio
async def test_first_acquire_creates_semaphore_with_configured_cap(cap: int) -> None:
    async with per_tenant_concurrency.per_tenant_storage_slot("tenant-A"):
        sem = per_tenant_concurrency._TENANT_SEMAPHORES["tenant-A"]
        # Inside the context, one permit is taken — remaining should be cap-1.
        assert sem._value == cap - 1


@pytest.mark.asyncio
async def test_separate_tenants_have_independent_semaphores() -> None:
    async with per_tenant_concurrency.per_tenant_storage_slot("tenant-A"):
        async with per_tenant_concurrency.per_tenant_storage_slot("tenant-B"):
            assert (
                per_tenant_concurrency._TENANT_SEMAPHORES["tenant-A"]
                is not per_tenant_concurrency._TENANT_SEMAPHORES["tenant-B"]
            )


@pytest.mark.asyncio
async def test_releases_on_normal_exit(cap: int) -> None:
    async with per_tenant_concurrency.per_tenant_storage_slot("tenant-A"):
        pass
    sem = per_tenant_concurrency._TENANT_SEMAPHORES["tenant-A"]
    assert sem._value == cap


@pytest.mark.asyncio
async def test_releases_on_exception(cap: int) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        async with per_tenant_concurrency.per_tenant_storage_slot("tenant-A"):
            raise RuntimeError("boom")
    sem = per_tenant_concurrency._TENANT_SEMAPHORES["tenant-A"]
    assert sem._value == cap


@pytest.mark.asyncio
async def test_third_concurrent_acquire_for_same_tenant_queues(cap: int) -> None:
    """With cap=2, the third in-flight call for one tenant must wait
    for one of the first two to release. A fourth caller for a *different*
    tenant must NOT wait — that's the noisy-neighbor invariant."""
    assert cap == 2, "Test assumes default cap of 2"

    enter_events = [asyncio.Event() for _ in range(4)]
    release_events = [asyncio.Event() for _ in range(4)]

    async def runner(tenant: str, idx: int) -> None:
        async with per_tenant_concurrency.per_tenant_storage_slot(tenant):
            enter_events[idx].set()
            await release_events[idx].wait()

    t0 = asyncio.create_task(runner("tenant-A", 0))
    t1 = asyncio.create_task(runner("tenant-A", 1))
    t2 = asyncio.create_task(runner("tenant-A", 2))
    t3 = asyncio.create_task(runner("tenant-B", 3))

    # Yield enough times for runnable tasks to actually enter their
    # slots; ``asyncio.sleep(0)`` only progresses one step per await.
    for _ in range(20):
        await asyncio.sleep(0)

    assert enter_events[0].is_set()
    assert enter_events[1].is_set()
    assert not enter_events[2].is_set(), "Third tenant-A caller must queue"
    assert enter_events[3].is_set(), "Tenant-B must not be blocked by tenant-A"

    # Release one tenant-A slot; the queued tenant-A caller should now
    # be able to enter on the next loop turn.
    release_events[0].set()
    for _ in range(20):
        await asyncio.sleep(0)
    assert enter_events[2].is_set()

    # Drain.
    for ev in release_events:
        ev.set()
    await asyncio.gather(t0, t1, t2, t3)


@pytest.mark.asyncio
async def test_lru_evicts_oldest_when_registry_full(monkeypatch: pytest.MonkeyPatch) -> None:
    """With max=3, the 4th distinct tenant evicts the LRU entry; an
    entry touched in between is preserved."""
    monkeypatch.setattr(per_tenant_concurrency, "_MAX_TRACKED_TENANTS", 3)

    for tid in ("A", "B", "C"):
        async with per_tenant_concurrency.per_tenant_storage_slot(tid):
            pass

    # Touch A so B becomes LRU.
    async with per_tenant_concurrency.per_tenant_storage_slot("A"):
        pass

    # Adding D should evict B (LRU), keep A, C, D.
    async with per_tenant_concurrency.per_tenant_storage_slot("D"):
        pass

    keys = list(per_tenant_concurrency._TENANT_SEMAPHORES.keys())
    assert "B" not in keys
    assert set(keys) == {"A", "C", "D"}


@pytest.mark.asyncio
async def test_evicted_tenant_reacquire_creates_fresh_semaphore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After eviction, re-acquiring the same tenant installs a NEW
    semaphore (the old object isn't recovered from cold storage)."""
    monkeypatch.setattr(per_tenant_concurrency, "_MAX_TRACKED_TENANTS", 1)

    async with per_tenant_concurrency.per_tenant_storage_slot("A"):
        pass
    sem_a_v1 = per_tenant_concurrency._TENANT_SEMAPHORES["A"]

    # Adding B evicts A.
    async with per_tenant_concurrency.per_tenant_storage_slot("B"):
        pass
    assert "A" not in per_tenant_concurrency._TENANT_SEMAPHORES

    # Re-acquiring A installs a fresh semaphore (and evicts B in turn).
    async with per_tenant_concurrency.per_tenant_storage_slot("A"):
        pass
    sem_a_v2 = per_tenant_concurrency._TENANT_SEMAPHORES["A"]
    assert sem_a_v2 is not sem_a_v1


def test_settings_rejects_zero_per_tenant_cap() -> None:
    """A misconfigured ``per_tenant_storage_write_concurrency=0`` would
    silently deadlock every PATCH-back. Validate at config load."""
    with pytest.raises(ValidationError, match="must be >= 1"):
        Settings(per_tenant_storage_write_concurrency=0)  # type: ignore[call-arg]


@pytest.mark.parametrize("bad", [0, -1, -100])
def test_settings_rejects_non_positive_per_tenant_cap(bad: int) -> None:
    with pytest.raises(ValidationError, match="must be >= 1"):
        Settings(per_tenant_storage_write_concurrency=bad)  # type: ignore[call-arg]

"""Tests for the per-tenant in-flight concurrency cap.

The cap is exercised at the route layer; we drive concurrent requests
against the live ASGI client and assert that excess requests get a
429 instead of queueing past the worker layer.
"""

import asyncio

import pytest

from core_api.config import settings
from core_api.middleware import per_tenant_concurrency
from core_api.middleware.per_tenant_concurrency import (
    per_tenant_slot,
    per_tenant_storage_slot,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _reset_semaphore_state():
    """Each test starts with empty per-tenant tracking. Otherwise a
    leftover semaphore from a prior test (with a stale cap) leaks into
    the next."""
    per_tenant_concurrency._reset_for_tests()
    yield
    per_tenant_concurrency._reset_for_tests()


@pytest.fixture
def tight_caps(monkeypatch):
    """Shrink the per-tenant caps to small values so a handful of
    concurrent in-flight slots exercises the exhaustion path. Tests
    request whichever cap they need via the returned setter."""

    def _set(
        *,
        write: int = 2,
        search: int = 2,
        storage_write: int = 2,
        storage_search: int = 2,
    ) -> None:
        monkeypatch.setattr(settings, "per_tenant_write_concurrency", write)
        monkeypatch.setattr(settings, "per_tenant_search_concurrency", search)
        monkeypatch.setattr(settings, "per_tenant_storage_write_concurrency", storage_write)
        monkeypatch.setattr(settings, "per_tenant_storage_search_concurrency", storage_search)
        per_tenant_concurrency._reset_for_tests()

    return _set


async def test_slot_grants_when_under_cap(tight_caps):
    """Within-cap acquires succeed, even back-to-back."""
    tight_caps(write=2)
    async with per_tenant_slot("write", "tenant-a"):
        async with per_tenant_slot("write", "tenant-a"):
            pass


async def test_slot_429s_when_cap_exhausted(tight_caps):
    """The first ``cap`` slots succeed; the next attempt raises 429."""
    tight_caps(write=2)

    async def hold_slot(release: asyncio.Event) -> None:
        async with per_tenant_slot("write", "tenant-a"):
            await release.wait()

    release = asyncio.Event()
    holders = [
        asyncio.create_task(hold_slot(release)),
        asyncio.create_task(hold_slot(release)),
    ]
    # Yield to let the holders acquire their slots before we try.
    await asyncio.sleep(0.01)
    try:
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            async with per_tenant_slot("write", "tenant-a"):
                pass
        assert exc.value.status_code == 429
        assert "write" in exc.value.detail
    finally:
        release.set()
        await asyncio.gather(*holders)


async def test_slot_release_on_exception(tight_caps):
    """An exception inside the slot releases it for the next caller."""
    tight_caps(write=1)
    with pytest.raises(RuntimeError):
        async with per_tenant_slot("write", "tenant-a"):
            raise RuntimeError("boom")
    # Cap was 1; if the slot wasn't released we'd 429 here.
    async with per_tenant_slot("write", "tenant-a"):
        pass


async def test_scope_isolated(tight_caps):
    """``write`` and ``search`` track separate caps for the same tenant."""
    tight_caps(write=1, search=1)

    async def hold_write(release: asyncio.Event) -> None:
        async with per_tenant_slot("write", "tenant-a"):
            await release.wait()

    release = asyncio.Event()
    holder = asyncio.create_task(hold_write(release))
    await asyncio.sleep(0.01)
    try:
        # search slot for the same tenant must still be free.
        async with per_tenant_slot("search", "tenant-a"):
            pass
    finally:
        release.set()
        await holder


async def test_tenants_isolated(tight_caps):
    """Saturating tenant A's cap doesn't affect tenant B."""
    tight_caps(write=1)

    async def hold(release: asyncio.Event) -> None:
        async with per_tenant_slot("write", "tenant-a"):
            await release.wait()

    release = asyncio.Event()
    holder = asyncio.create_task(hold(release))
    await asyncio.sleep(0.01)
    try:
        # tenant-b's slot is independent.
        async with per_tenant_slot("write", "tenant-b"):
            pass
    finally:
        release.set()
        await holder


# ── Storage-call slot (CAURA-602 follow-up) ──


async def test_storage_slot_queues_unboundedly(tight_caps):
    """Unlike the route-entry slot, the storage slot queues without a
    fast-fail timeout. The third caller waits until tenant A's two
    in-flight writes release the slot — the route layer's outer budget
    is what actually caps total wait time."""
    tight_caps(storage_write=2)

    held_count = 0
    release = asyncio.Event()

    async def hold_storage() -> None:
        nonlocal held_count
        async with per_tenant_storage_slot("storage_write", "tenant-a"):
            held_count += 1
            await release.wait()

    holders = [asyncio.create_task(hold_storage()) for _ in range(2)]
    await asyncio.sleep(0.01)
    assert held_count == 2

    third = asyncio.create_task(hold_storage())
    await asyncio.sleep(0.01)
    # Third caller is queued (not raised, not fast-failed).
    assert held_count == 2
    assert not third.done()

    release.set()
    await asyncio.gather(*holders, third)
    assert held_count == 3


async def test_storage_slot_isolates_tenants_under_storm(tight_caps):
    """Tenant A saturating its storage cap doesn't block tenant B —
    this is the noisy-neighbor target case for the bulkhead."""
    tight_caps(storage_write=1)

    release = asyncio.Event()

    async def hold_a() -> None:
        async with per_tenant_storage_slot("storage_write", "tenant-a"):
            await release.wait()

    holder = asyncio.create_task(hold_a())
    await asyncio.sleep(0.01)
    try:
        # tenant-b's storage slot is a separate semaphore.
        async with per_tenant_storage_slot("storage_write", "tenant-b"):
            pass
    finally:
        release.set()
        await holder


async def test_storage_slot_release_on_exception(tight_caps):
    """An exception inside the storage slot releases it cleanly so a
    subsequent acquire doesn't deadlock."""
    tight_caps(storage_write=1)

    with pytest.raises(RuntimeError):
        async with per_tenant_storage_slot("storage_write", "tenant-a"):
            raise RuntimeError("boom")
    # Cap was 1; if the slot wasn't released the next acquire would
    # block forever rather than entering the body.
    async with asyncio.timeout(1.0):
        async with per_tenant_storage_slot("storage_write", "tenant-a"):
            pass

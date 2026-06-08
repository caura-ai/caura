"""Unit tests for the core-api suppression boundary guard (CAURA-694).

Covers the TTL cache + fail-open contract in
``core_api.suppression`` and the ``_block_if_suppressed`` integration
point in ``core_api.auth``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from core_api import suppression
from core_api.auth import _block_if_any_readable_suppressed, _block_if_suppressed


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    """Each test starts with an empty cache so cross-test bleed can't
    mask a real bug in the TTL / lookup path."""
    suppression.reset_cache_for_testing()
    yield
    suppression.reset_cache_for_testing()


@pytest.mark.asyncio
async def test_cache_miss_calls_storage_and_caches(monkeypatch) -> None:
    sc = AsyncMock()
    sc.is_tenant_suppressed.return_value = True
    monkeypatch.setattr(suppression, "get_storage_client", lambda: sc)

    assert await suppression.is_tenant_suppressed("t1") is True
    # Second call within TTL is a cache hit — storage stub called once.
    assert await suppression.is_tenant_suppressed("t1") is True
    assert sc.is_tenant_suppressed.await_count == 1


@pytest.mark.asyncio
async def test_cache_expires_after_ttl(monkeypatch) -> None:
    sc = AsyncMock()
    sc.is_tenant_suppressed.return_value = False
    monkeypatch.setattr(suppression, "get_storage_client", lambda: sc)

    # First call → miss → storage hit.
    await suppression.is_tenant_suppressed("t1")
    # Fast-forward monotonic clock past the TTL.
    real_monotonic = suppression.time.monotonic
    now = real_monotonic()
    monkeypatch.setattr(
        suppression.time,
        "monotonic",
        lambda: now + suppression._TTL_SECONDS + 1,
    )
    # Expired → second storage hit.
    await suppression.is_tenant_suppressed("t1")
    assert sc.is_tenant_suppressed.await_count == 2


@pytest.mark.asyncio
async def test_storage_failure_fails_open(monkeypatch, caplog) -> None:
    """A transport failure MUST NOT 403 the request — log + fall through
    so a storage flap doesn't take core-api down."""
    sc = AsyncMock()
    sc.is_tenant_suppressed.side_effect = RuntimeError("storage down")
    monkeypatch.setattr(suppression, "get_storage_client", lambda: sc)

    with caplog.at_level("WARNING"):
        result = await suppression.is_tenant_suppressed("t1")
    assert result is False
    assert any("tenant suppression check failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_storage_failure_caches_false_to_rate_limit_retries(monkeypatch) -> None:
    """Thundering-herd guard: when storage raises, the cache must be
    written with a short error-TTL so subsequent requests during the
    outage are absorbed by the cache rather than hammering storage on
    every authenticated request. Bot review round 1 on PR #244."""
    sc = AsyncMock()
    sc.is_tenant_suppressed.side_effect = RuntimeError("storage down")
    monkeypatch.setattr(suppression, "get_storage_client", lambda: sc)

    # First call: storage hit + cache write with short TTL.
    await suppression.is_tenant_suppressed("t1")
    # Second call within the error-TTL window: cache hit, NO new storage call.
    await suppression.is_tenant_suppressed("t1")
    assert sc.is_tenant_suppressed.await_count == 1


@pytest.mark.asyncio
async def test_error_cache_uses_short_ttl(monkeypatch) -> None:
    """The error-TTL must be shorter than the success-TTL so the cache
    self-heals quickly once storage recovers — a 30 s wedge of cached
    ``False`` would mask a long-running suppression decision once
    storage is back."""
    assert suppression._ERROR_TTL_SECONDS < suppression._TTL_SECONDS

    sc = AsyncMock()
    sc.is_tenant_suppressed.side_effect = RuntimeError("storage down")
    monkeypatch.setattr(suppression, "get_storage_client", lambda: sc)

    # Miss → cache populated with the short TTL.
    await suppression.is_tenant_suppressed("t1")
    real_monotonic = suppression.time.monotonic
    now = real_monotonic()
    # Fast-forward past the SHORT TTL but well within the LONG TTL.
    monkeypatch.setattr(
        suppression.time,
        "monotonic",
        lambda: now + suppression._ERROR_TTL_SECONDS + 1,
    )
    # Storage call retried → cache served via short-TTL expiry.
    await suppression.is_tenant_suppressed("t1")
    assert sc.is_tenant_suppressed.await_count == 2


@pytest.mark.asyncio
async def test_block_if_suppressed_raises_403_when_suppressed(
    monkeypatch,
) -> None:
    sc = AsyncMock()
    sc.is_tenant_suppressed.return_value = True
    monkeypatch.setattr(suppression, "get_storage_client", lambda: sc)

    with pytest.raises(HTTPException) as exc:
        await _block_if_suppressed("t1")
    assert exc.value.status_code == 403
    # Detail wording is intentionally generic — avoids leaking the
    # specific lifecycle state to a partner whose key was provisioned
    # under that org.
    assert "suspended" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_block_if_suppressed_passes_when_live(monkeypatch) -> None:
    sc = AsyncMock()
    sc.is_tenant_suppressed.return_value = False
    monkeypatch.setattr(suppression, "get_storage_client", lambda: sc)
    await _block_if_suppressed("t1")  # no raise


@pytest.mark.asyncio
async def test_block_if_suppressed_skips_none_tenant() -> None:
    """Admin paths reach auth with ``tenant_id=None``; the guard MUST
    short-circuit so admins don't accidentally trip the boundary."""
    # No monkeypatch needed — should never reach storage.
    await _block_if_suppressed(None)  # no raise


@pytest.mark.asyncio
async def test_block_if_any_readable_suppressed_checks_each(monkeypatch) -> None:
    """Bot review round 2 on PR #244: a cross-tenant credential whose
    readable set spans a suppressed org would otherwise pass the
    home-only guard. The helper must call ``is_tenant_suppressed`` for
    every entry in ``readable_tenants`` (minus the home tenant which
    the caller already checked) and 403 on the first hit."""
    sc = AsyncMock()
    # Home tenant "home" lives; "co-1" lives; "co-2" SUPPRESSED.
    sc.is_tenant_suppressed.side_effect = lambda tid: tid == "co-2"
    monkeypatch.setattr(suppression, "get_storage_client", lambda: sc)

    with pytest.raises(HTTPException) as exc:
        await _block_if_any_readable_suppressed("home", ["home", "co-1", "co-2"])
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_block_if_any_readable_suppressed_skips_home(monkeypatch) -> None:
    """Home tenant_id must NOT be re-checked here — the caller
    invokes ``_block_if_suppressed`` on it separately, and a duplicate
    check would double the cache traffic per request on
    enterprise-ingress paths."""
    sc = AsyncMock()
    sc.is_tenant_suppressed.return_value = False
    monkeypatch.setattr(suppression, "get_storage_client", lambda: sc)

    await _block_if_any_readable_suppressed("home", ["home", "co-1"])
    # Only "co-1" should have generated a storage call; "home" skipped.
    called_tids = [c.args[0] for c in sc.is_tenant_suppressed.await_args_list]
    assert "home" not in called_tids
    assert "co-1" in called_tids


@pytest.mark.asyncio
async def test_block_if_any_readable_suppressed_empty_list_noop(
    monkeypatch,
) -> None:
    """An empty ``readable_tenants`` list (the single-tenant credential
    shape) must short-circuit cleanly — no storage call, no raise."""
    sc = AsyncMock()
    monkeypatch.setattr(suppression, "get_storage_client", lambda: sc)
    await _block_if_any_readable_suppressed("home", [])
    sc.is_tenant_suppressed.assert_not_awaited()


@pytest.mark.asyncio
async def test_cache_pruning_evicts_expired_entries(monkeypatch) -> None:
    """Bot review round 2 on PR #244: a long-running process with high
    tenant turnover would accumulate expired entries forever because
    they're only overwritten on re-lookup of the SAME tenant. The
    on-write pruning step must evict any entry whose TTL has elapsed,
    so cache size stays bounded by active tenants in the TTL window."""
    sc = AsyncMock()
    sc.is_tenant_suppressed.return_value = False
    monkeypatch.setattr(suppression, "get_storage_client", lambda: sc)

    # Populate the cache with an entry that will expire in the
    # fast-forward step below.
    await suppression.is_tenant_suppressed("t-old")
    assert "t-old" in suppression._cache

    # Fast-forward past the success-TTL so "t-old" is now stale.
    real_monotonic = suppression.time.monotonic
    now = real_monotonic()
    monkeypatch.setattr(
        suppression.time,
        "monotonic",
        lambda: now + suppression._TTL_SECONDS + 1,
    )
    # Trigger another tenant's lookup — the pre-write prune step must
    # evict "t-old" even though no one ever asks for it again.
    await suppression.is_tenant_suppressed("t-new")
    assert "t-old" not in suppression._cache
    assert "t-new" in suppression._cache

"""The storage-backed usage meter — the half that makes counters actually move.

caura-ai/caura-enterprise#83. ``usage_service`` (#824) added the seam; this
covers the implementation wired into it.

What matters here is not that a counter goes up — it is the three properties
that make buffered counting safe for billing, each pinned separately:

* coalescing must not lose the **count** (a bulk write of 20 is not 1),
* a failed flush must **return** the counts, not drop them,
* shutdown must flush, or a clean restart silently costs an interval.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from core_api.services.usage_meter import UsageMeter, current_period_start

pytestmark = pytest.mark.unit


@pytest.fixture
def sc(monkeypatch):
    client = AsyncMock()
    client.increment_tenant_usage = AsyncMock(return_value=1)
    monkeypatch.setattr(
        "core_api.services.usage_meter.get_storage_client", lambda: client
    )
    return client


def _rows(sc):
    return sc.increment_tenant_usage.await_args.args[0]


# ── The period key ───────────────────────────────────────────────────────────


def test_period_start_is_the_utc_month():
    got = current_period_start(datetime(2026, 8, 18, 14, 32, 9, tzinfo=UTC))
    assert got == datetime(2026, 8, 1, 0, 0, 0, tzinfo=UTC)


def test_period_start_normalises_a_non_utc_instant():
    """A row must land in the period the caller counted in, not the one the
    server's local clock suggests."""
    from datetime import timedelta, timezone

    # 2026-09-01 00:30 at +02:00 is still 2026-08-31 in UTC.
    late_august = datetime(2026, 9, 1, 0, 30, tzinfo=timezone(timedelta(hours=2)))
    assert current_period_start(late_august) == datetime(2026, 8, 1, tzinfo=UTC)


# ── Coalescing ───────────────────────────────────────────────────────────────


async def test_repeated_operations_become_one_row_with_the_summed_count(sc):
    meter = UsageMeter()
    for _ in range(5):
        await meter.record(tenant_id="t-1", operation="write")
    await meter.flush()

    rows = _rows(sc)
    assert len(rows) == 1
    assert rows[0]["tenant_id"] == "t-1"
    assert rows[0]["operation"] == "write"
    assert rows[0]["count"] == 5


async def test_a_bulk_count_is_carried_not_flattened(sc):
    """A bulk write of 20 items is 20, not 1. Losing the count here would
    under-bill by the factor that makes bulk worth having."""
    meter = UsageMeter()
    await meter.record(tenant_id="t-1", operation="write", count=20)
    await meter.record(tenant_id="t-1", operation="write", count=3)
    await meter.flush()

    assert _rows(sc)[0]["count"] == 23


async def test_tenants_and_operations_stay_separate(sc):
    meter = UsageMeter()
    await meter.record(tenant_id="t-1", operation="write")
    await meter.record(tenant_id="t-1", operation="search")
    await meter.record(tenant_id="t-2", operation="write")
    await meter.flush()

    keys = {(r["tenant_id"], r["operation"]) for r in _rows(sc)}
    assert keys == {("t-1", "write"), ("t-1", "search"), ("t-2", "write")}


async def test_the_buffer_is_cleared_so_counts_are_not_written_twice(sc):
    meter = UsageMeter()
    await meter.record(tenant_id="t-1", operation="write")
    await meter.flush()
    await meter.flush()

    assert sc.increment_tenant_usage.await_count == 1, "second flush re-sent counts"


async def test_an_empty_buffer_makes_no_call(sc):
    assert await UsageMeter().flush() == 0
    sc.increment_tenant_usage.assert_not_awaited()


# ── Failure behaviour ────────────────────────────────────────────────────────


async def test_a_failed_flush_returns_the_counts_to_the_buffer(sc):
    """The upsert is additive, so re-sending is safe — and dropping is not.

    A storage blip must cost latency, not billing accuracy.
    """
    sc.increment_tenant_usage.side_effect = RuntimeError("storage down")
    meter = UsageMeter()
    await meter.record(tenant_id="t-1", operation="write", count=7)
    assert await meter.flush() == 0

    sc.increment_tenant_usage.side_effect = None
    sc.increment_tenant_usage.return_value = 1
    await meter.flush()

    assert _rows(sc)[0]["count"] == 7, "counts were dropped by the failed flush"


async def test_counts_arriving_during_a_failed_flush_are_merged_not_lost(sc):
    sc.increment_tenant_usage.side_effect = RuntimeError("storage down")
    meter = UsageMeter()
    await meter.record(tenant_id="t-1", operation="write", count=2)
    await meter.flush()
    await meter.record(tenant_id="t-1", operation="write", count=3)

    sc.increment_tenant_usage.side_effect = None
    await meter.flush()

    assert _rows(sc)[0]["count"] == 5


# ── Lifecycle ────────────────────────────────────────────────────────────────


async def test_stop_flushes_what_is_buffered(sc):
    """Without the final flush a clean shutdown silently costs up to one
    interval of counts."""
    meter = UsageMeter(flush_interval=3600)
    meter.start()
    await meter.record(tenant_id="t-1", operation="write", count=4)
    await meter.stop()

    assert _rows(sc)[0]["count"] == 4


async def test_a_shutdown_landing_inside_a_flush_does_not_drop_the_batch(sc):
    """``stop()`` cancels the loop, and the cancel can land in the flush's
    network call — the one window where the restore-on-failure path is the
    difference between billing accuracy and silence.

    Regression: ``CancelledError`` is a ``BaseException``, so an
    ``except Exception:`` restore never fired for it, and the batch — already
    swapped out of the buffer — was gone before ``stop()``'s final flush looked.
    """
    in_flight = asyncio.Event()

    async def hang(rows):
        in_flight.set()
        await asyncio.Event().wait()  # never returns; the cancel lands here

    sc.increment_tenant_usage.side_effect = hang

    meter = UsageMeter(flush_interval=0.01)
    await meter.record(tenant_id="t-1", operation="write", count=9)
    meter.start()
    await asyncio.wait_for(in_flight.wait(), timeout=5)

    sc.increment_tenant_usage.side_effect = None
    sc.increment_tenant_usage.return_value = 1
    await meter.stop()

    assert sc.increment_tenant_usage.await_count == 2, (
        "the cancelled batch was never re-sent — stop() found an empty buffer"
    )
    assert _rows(sc)[0]["count"] == 9


async def test_a_wedged_storage_does_not_hold_shutdown_open(sc):
    """``stop()`` runs inside core-api's shutdown chain, ahead of the event bus
    and the storage client's own close.

    The storage client waits 120s on a read — far past the grace period Cloud
    Run gives a terminating revision — so an unbounded final flush would lose
    these counts to SIGKILL *and* strand every step behind it. Bounded, it
    costs only the counts.
    """

    async def hang(rows):
        await asyncio.Event().wait()

    sc.increment_tenant_usage.side_effect = hang

    meter = UsageMeter(flush_interval=3600)
    await meter.record(tenant_id="t-1", operation="write", count=6)
    await meter.stop(timeout=0.05)  # returns rather than hanging

    assert meter._counts, "the batch was dropped instead of returned to the buffer"


async def test_the_hook_signature_matches_what_usage_service_calls(sc):
    """``record`` is handed to ``ServiceHooks.usage_meter``, which
    ``usage_service._meter`` invokes by keyword. A rename here would fail
    open at runtime — silently, since the meter's errors are swallowed there."""
    from core_api.services.hooks import ServiceHooks, configure_hooks, reset_hooks
    from core_api.services.usage_service import check_and_increment

    meter = UsageMeter()
    configure_hooks(ServiceHooks(usage_meter=meter.record))
    try:
        result = await check_and_increment("t-1", "write", 3)
        await meter.flush()
    finally:
        reset_hooks()

    assert result.allowed is True  # metering never blocks
    assert _rows(sc)[0] == {
        "tenant_id": "t-1",
        "operation": "write",
        "period_start": current_period_start().isoformat(),
        "count": 3,
    }


# ── The storage endpoint's validation ────────────────────────────────────────


async def test_a_row_missing_period_start_is_a_422_not_a_500():
    """Regression: the check indexed ``r["period_start"]``.

    A row omitting the key raised KeyError inside the validation loop, which
    sits OUTSIDE the try/except below it — so the miss surfaced as exactly the
    500 the coercion block exists to prevent. Caught in review.
    """
    from core_storage_api.routers.tenant_usage import increment_tenant_usage
    from fastapi import HTTPException

    class _Req:
        async def json(self):
            return {"rows": [{"tenant_id": "t-1", "operation": "write"}]}

    with pytest.raises(HTTPException) as exc:
        await increment_tenant_usage(_Req())
    assert exc.value.status_code == 422
    assert "period_start" in str(exc.value.detail)

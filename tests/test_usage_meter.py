"""The storage-backed usage meter — the half that makes counters actually move.

caura-ai/caura-enterprise#83. ``usage_service`` (#824) added the seam; this
covers the implementation wired into it.

What matters here is not that a counter goes up — it is the three properties
that make buffered counting safe for billing, each pinned separately:

* coalescing must not lose the **count** (a bulk write of 20 is not 1),
* a flush that failed BEFORE transmission must **return** the counts, while
  one that failed ambiguously must drop them rather than replay an additive
  upsert that may already have committed (OSS 09/02 L-44),
* shutdown must flush, or a clean restart silently costs an interval.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
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
    """A blip that never reached storage must cost latency, not billing accuracy.

    ``ConnectError`` rather than a bare ``RuntimeError`` since OSS 09/02 L-44:
    "storage down" IS a connection-phase failure, and that class is the one
    where re-sending an additive upsert is provably safe because the request
    was never transmitted. The generic exception used to stand in for it, which
    made this test read as though re-sending were safe after ANY failure — the
    belief the finding was about.
    """
    sc.increment_tenant_usage.side_effect = httpx.ConnectError("storage down")
    meter = UsageMeter()
    await meter.record(tenant_id="t-1", operation="write", count=7)
    assert await meter.flush() == 0

    sc.increment_tenant_usage.side_effect = None
    sc.increment_tenant_usage.return_value = 1
    await meter.flush()

    # ``await_count`` first: ``_rows`` reads ``await_args``, i.e. the LAST call,
    # and when the counts are dropped there IS no second call — so the failed
    # first call's rows (which still carry count=7) satisfied the assertion
    # below whether or not the replay happened.
    assert sc.increment_tenant_usage.await_count == 2, (
        "the buffered counts were never re-sent"
    )
    assert _rows(sc)[0]["count"] == 7, "counts were dropped by the failed flush"


async def test_counts_arriving_during_a_failed_flush_are_merged_not_lost(sc):
    sc.increment_tenant_usage.side_effect = httpx.ConnectError("storage down")
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


async def test_a_failed_final_flush_logs_the_stranded_counter(sc, caplog):
    sc.increment_tenant_usage.side_effect = httpx.ConnectError("storage down")
    meter = UsageMeter()
    await meter.record(tenant_id="tenant-a", operation="search", count=7)
    period_start = next(iter(meter._counts))[2].isoformat()

    with caplog.at_level(logging.ERROR, logger="core_api.services.usage_meter"):
        await meter.stop()

    stranded = [
        record
        for record in caplog.records
        if f"tenant=tenant-a operation=search period_start={period_start} count=7"
        in record.getMessage()
    ]
    assert len(stranded) == 1
    assert stranded[0].levelno == logging.ERROR


async def test_a_shutdown_landing_inside_a_flush_does_not_replay_the_batch(sc):
    """``stop()`` cancels the loop, and the cancel can land in the flush's
    network call. CONTRACT REVERSED by OSS 09/02 L-44 — this used to assert the
    batch was re-sent.

    ``CoreStorageClient._cancel_safe`` shields every request on purpose, so the
    cancelled POST runs to completion and may commit after the frame unwinds —
    including when storage looks wedged, since the shielded request outlives
    our giving up on it. Re-sending it therefore double-bills. The batch is
    dropped, logged per row and counted instead, which is the under-count-
    rather-than-over-count direction the module argues for throughout.

    The original regression this guarded — the batch vanishing with nobody the
    wiser, because ``CancelledError`` walks past ``except Exception`` — is still
    pinned, now by ``dropped_rows`` rather than by a re-send.
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

    assert sc.increment_tenant_usage.await_count == 1, (
        "the cancelled batch was replayed — the shielded write may already have landed"
    )
    assert meter.dropped_rows == 1, "dropping it must still be visible"
    assert _rows(sc)[0]["count"] == 9


async def test_a_wedged_storage_does_not_hold_shutdown_open(sc, caplog):
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
    with caplog.at_level(logging.WARNING, logger="core_api.services.usage_meter"):
        await meter.stop(timeout=0.05)  # returns rather than hanging

    # The point of this test is that ``stop()`` RETURNS rather than hanging;
    # reaching this line at all is the assertion. What happens to the batch
    # changed with OSS 09/02 L-44: the wedged request is shielded and still
    # in flight, so it may yet commit, and the counts are dropped rather than
    # queued for a replay that could double-bill.
    assert not meter._counts
    assert meter.dropped_rows == 1, "the shortfall must be counted, not silent"

    # ...and the deadline warning has to SAY how much was lost. It used to read
    # ``len(self._counts)``, which was right only while a failed flush put its
    # batch back. Now that the batch is dropped instead, ``_counts`` is empty by
    # the time this branch runs, so the old line reported "0 rows lost" on
    # precisely the path that loses the most.
    deadline = [
        r for r in caplog.records if "did not complete within" in r.getMessage()
    ]
    assert len(deadline) == 1, [r.getMessage() for r in caplog.records]
    msg = deadline[0].getMessage()
    assert "1 counter rows dropped" in msg, msg


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
    from fastapi import HTTPException

    from core_storage_api.routers.tenant_usage import increment_tenant_usage

    class _Req:
        async def json(self):
            return {"rows": [{"tenant_id": "t-1", "operation": "write"}]}

    with pytest.raises(HTTPException) as exc:
        await increment_tenant_usage(_Req())
    assert exc.value.status_code == 422
    assert "period_start" in str(exc.value.detail)


# ── OSS 09/02 L-44: replay only what provably never landed ───────────────────


async def test_an_ambiguous_flush_is_not_replayed(sc):
    """A ReadTimeout may follow a COMMITTED write; replaying it bills twice.

    ``/tenant-usage/increment`` is an additive upsert with no storage-side
    dedupe, which is why ``increment_tenant_usage`` goes out at the
    ``idempotent=False`` default. The meter replayed on any exception one layer
    up, which is the thing ``common/http_retry``'s policy refuses.
    """
    sc.increment_tenant_usage.side_effect = httpx.ReadTimeout("no response")
    meter = UsageMeter()
    await meter.record(tenant_id="t-1", operation="write", count=7)
    assert await meter.flush() == 0

    sc.increment_tenant_usage.side_effect = None
    await meter.flush()

    assert sc.increment_tenant_usage.await_count == 1, (
        "the ambiguous batch was replayed and would have double-billed"
    )
    assert meter.dropped_rows == 1


async def test_a_cancelled_flush_is_not_replayed_either(sc):
    """The shutdown path is the one that looks safe to exempt, and isn't.

    ``CoreStorageClient._cancel_safe`` shields every request ON PURPOSE, so a
    cancelled flush's POST runs to completion and very likely COMMITS after
    this frame unwinds. Re-buffering would hand it to ``stop()``'s final flush
    and bill it twice — the precise failure this finding is about.
    """
    sc.increment_tenant_usage.side_effect = asyncio.CancelledError()
    meter = UsageMeter()
    await meter.record(tenant_id="t-1", operation="write", count=7)
    with pytest.raises(asyncio.CancelledError):
        await meter.flush()

    assert not meter._counts, (
        "a cancelled flush must not return its batch to the buffer"
    )
    assert meter.dropped_rows == 1


async def test_an_ambiguous_flush_logs_every_dropped_row(sc, caplog):
    """Dropping is only defensible if the shortfall is recoverable by hand."""
    sc.increment_tenant_usage.side_effect = httpx.ReadTimeout("no response")
    meter = UsageMeter()
    await meter.record(tenant_id="tenant-a", operation="search", count=7)
    period_start = next(iter(meter._counts))[2].isoformat()

    with caplog.at_level(logging.ERROR, logger="core_api.services.usage_meter"):
        await meter.flush()

    assert any(
        f"tenant=tenant-a operation=search period_start={period_start} count=7"
        in r.getMessage()
        for r in caplog.records
    ), "a dropped billing row must name itself in the log"


async def test_dropped_row_logging_is_capped(sc, caplog):
    """``tenant_id`` is unbounded, so one flush can carry hundreds of rows.

    ``usage_service`` refuses the uncapped version of this for the same reason
    (``_METER_FAILURE_LOG_EVERY``); a meter outage must not become a log-volume
    incident on top of it.
    """
    sc.increment_tenant_usage.side_effect = httpx.ReadTimeout("no response")
    meter = UsageMeter()
    for i in range(60):
        await meter.record(tenant_id=f"tenant-{i}", operation="write", count=1)

    with caplog.at_level(logging.ERROR, logger="core_api.services.usage_meter"):
        await meter.flush()

    per_row = [r for r in caplog.records if "counter row dropped" in r.getMessage()]
    assert len(per_row) == 20, f"expected the cap, got {len(per_row)} lines for 60 rows"
    assert any("further counter rows dropped" in r.getMessage() for r in caplog.records)
    assert meter.dropped_rows == 60, "the counter still sees every row"

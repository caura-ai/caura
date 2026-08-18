"""Usage metering routes through ``ServiceHooks``, and OSS standalone is unchanged.

Rationale lives in the ``usage_service`` module docstring.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from core_api.services import usage_service
from core_api.services.hooks import ServiceHooks, configure_hooks
from core_api.services.usage_service import (
    UsageCheckResult,
    bulk_check_and_increment,
    check_and_increment,
    check_and_increment_by_tenant,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

# Only ``limit``/``remaining`` are ever read; the rest default.
_METERED = UsageCheckResult(allowed=True, operation="write", limit=100, remaining=93)


@pytest.fixture(autouse=True)
def _reset_failure_throttle():
    """The log throttle counts process-wide, so a prior test's failures would
    otherwise decide whether this one logs."""
    usage_service._meter_failures = 0


# ── OSS standalone: unchanged ────────────────────────────────────────────────


async def test_no_hook_reports_unlimited_and_records_nothing():
    for call in (
        check_and_increment("t-1", "write"),
        check_and_increment_by_tenant("t-1", "search"),
        bulk_check_and_increment("t-1", 20),
    ):
        result = await call
        assert result.allowed is True
        assert result.limit is None
        assert result.remaining is None


# ── Wired: the call reaches the meter ────────────────────────────────────────


async def test_a_wired_meter_receives_tenant_operation_and_count():
    meter = AsyncMock(return_value=_METERED)
    configure_hooks(ServiceHooks(usage_meter=meter))

    await check_and_increment("t-1", "write")

    meter.assert_awaited_once_with(tenant_id="t-1", operation="write", count=1)


async def test_the_bulk_path_passes_its_COUNT():
    """The bulk write is one call for N items; metering it as 1 loses N-1."""
    meter = AsyncMock(return_value=_METERED)
    configure_hooks(ServiceHooks(usage_meter=meter))

    await bulk_check_and_increment("t-1", 20)

    meter.assert_awaited_once_with(tenant_id="t-1", operation="write", count=20)


async def test_the_meters_counters_reach_the_caller():
    """The result feeds ``X-RateLimit-*``; a swallowed one makes headers lie."""
    configure_hooks(ServiceHooks(usage_meter=AsyncMock(return_value=_METERED)))

    result = await check_and_increment("t-1", "write")

    assert result.limit == 100
    assert result.remaining == 93


async def test_a_meter_that_reports_nothing_degrades_to_unlimited():
    """Recording without reporting counters is legitimate, not an error."""
    configure_hooks(ServiceHooks(usage_meter=AsyncMock(return_value=None)))

    result = await check_and_increment("t-1", "write")

    assert result.allowed is True
    assert result.limit is None


# ── Failure behaviour ────────────────────────────────────────────────────────


async def test_a_failing_meter_does_not_fail_the_write(caplog):
    """Fail OPEN, and loudly. This sits on the write path of every metered
    route: a metering backend that is down must cost a count, not a customer
    write — but the lost count still has to reach ops."""
    configure_hooks(
        ServiceHooks(usage_meter=AsyncMock(side_effect=RuntimeError("meter down")))
    )

    with caplog.at_level("ERROR"):
        result = await check_and_increment("t-1", "write")

    assert result.allowed is True
    assert result.limit is None
    assert any("usage meter failed" in r.message for r in caplog.records)


async def test_repeated_failures_are_throttled(caplog):
    """A dead meter must not emit one traceback per write.

    Without the throttle a metering outage becomes a log-volume incident on top
    of a metering one — the failure mode ``audit_queue`` already guards against.
    """
    configure_hooks(
        ServiceHooks(usage_meter=AsyncMock(side_effect=RuntimeError("meter down")))
    )

    with caplog.at_level("ERROR"):
        for _ in range(30):
            await check_and_increment("t-1", "write")

    logged = [r for r in caplog.records if "usage meter failed" in r.message]
    assert len(logged) == 1, f"30 failures produced {len(logged)} log records"


# ── One function, two names ──────────────────────────────────────────────────


async def test_both_entry_points_are_the_same_function():
    """Callers import them interchangeably — several as
    ``check_and_increment_by_tenant as check_and_increment`` — so a meter wired
    for one and not the other would silently drop half the traffic."""
    assert check_and_increment_by_tenant is check_and_increment

    meter = AsyncMock(return_value=_METERED)
    configure_hooks(ServiceHooks(usage_meter=meter))

    await check_and_increment("t-1", "write")
    await check_and_increment_by_tenant("t-1", "search")

    assert [c.kwargs["operation"] for c in meter.await_args_list] == ["write", "search"]
    assert {c.kwargs["tenant_id"] for c in meter.await_args_list} == {"t-1"}

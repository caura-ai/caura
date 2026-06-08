"""Tests for CAURA-602 request-timing observability.

All tests are ``async def`` even though most don't await — if we mix sync
tests into this file the pytest-asyncio session-scoped event loop never
gets initialised, and every async test that runs *after* us crashes with
``RuntimeError: There is no current event loop``. Keeping the whole file
async is the simplest fix.
"""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from core_storage_api.observability import (
    SLOW_QUERY_THRESHOLD_MS,
    Timer,
    bind_timer,
    db_measure,
    log_request,
)


async def test_timer_accumulates_across_multiple_blocks() -> None:
    t = Timer()
    with t.measure():
        time.sleep(0.01)
    with t.measure():
        time.sleep(0.01)
    # Two 10ms sleeps with a bit of slack for scheduler jitter
    assert t.total_ms >= 18.0


async def test_timer_total_ms_is_zero_without_measure() -> None:
    assert Timer().total_ms == 0.0


async def test_nested_measure_blocks_both_contribute() -> None:
    """Re-entering measure() must add both elapsed windows — the shared
    Timer instance can be visited through nested ``db_measure`` blocks if
    a service method ever calls another."""
    t = Timer()
    with t.measure():
        time.sleep(0.005)
        with t.measure():
            time.sleep(0.005)
    assert t.total_ms >= 13.0  # 5ms + (5ms + nested 5ms)


async def test_db_measure_is_noop_without_bound_timer() -> None:
    with db_measure():
        time.sleep(0.002)


async def test_db_measure_records_to_bound_timer() -> None:
    with bind_timer() as t:
        with db_measure():
            time.sleep(0.005)
        with db_measure():
            time.sleep(0.005)
    assert t.total_ms >= 8.0


async def test_bind_timer_isolates_per_task() -> None:
    """ContextVar means concurrent tasks see their own timer, not each
    other's — a regression here would mean concurrent requests all land
    in the same accumulator and produce garbage db_ms numbers."""

    async def _task(sleep_ms: int) -> float:
        with bind_timer() as t:
            with db_measure():
                await asyncio.sleep(sleep_ms / 1000)
        return t.total_ms

    results = await asyncio.gather(_task(10), _task(30), _task(20))
    assert results[0] < 25.0
    assert 25.0 <= results[1] < 50.0
    assert 15.0 <= results[2] < 35.0


async def test_log_request_uses_info_for_fast_queries(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="memclaw.observability")
    log_request("scored-search", tenant_id="t1", db_ms=10.0, total_ms=12.0, row_count=3)
    rec = caplog.records[-1]
    assert rec.levelno == logging.INFO
    assert rec.path == "scored-search"
    assert rec.tenant_id == "t1"
    assert rec.db_ms == 10.0
    assert rec.row_count == 3
    assert rec.slow is False


async def test_log_request_upgrades_to_warning_when_slow(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="memclaw.observability")
    log_request(
        "scored-search",
        tenant_id="t1",
        db_ms=SLOW_QUERY_THRESHOLD_MS + 1,
        total_ms=500.0,
    )
    rec = caplog.records[-1]
    assert rec.levelno == logging.WARNING
    assert rec.slow is True


async def test_log_request_rejects_reserved_kwargs() -> None:
    """``slow`` is computed inside log_request; a caller accidentally
    passing it would silently overwrite the computed value and drop the
    WARNING upgrade for slow queries. (``path`` is a positional param,
    so Python's own binding rejects ``path=x`` as a duplicate — no
    observability-side test needed.)"""
    with pytest.raises(ValueError, match="reserved"):
        log_request("scored-search", slow=True, db_ms=10.0)


async def test_log_request_without_db_ms_stays_info(caplog: pytest.LogCaptureFixture) -> None:
    """GET hits that miss the cache don't always carry db_ms — the helper
    must not crash or upgrade severity when the field isn't present."""
    caplog.set_level(logging.INFO, logger="memclaw.observability")
    log_request("memory-get", tenant_id="t1", total_ms=5.0, hit=False)
    rec = caplog.records[-1]
    assert rec.levelno == logging.INFO
    assert rec.slow is False


async def test_end_to_end_memory_get_404_emits_log_line(
    sc, caplog: pytest.LogCaptureFixture
) -> None:
    """End-to-end wiring: GET /memories/{id} miss flows through bind_timer,
    db_measure, and log_request; the log line carries db_ms from a real
    session.get() call."""
    from uuid import uuid4

    caplog.set_level(logging.INFO, logger="memclaw.observability")
    result = await sc.get_memory(uuid4())
    assert result is None

    obs_records = [r for r in caplog.records if r.name == "memclaw.observability"]
    assert obs_records, "expected at least one memclaw.observability log record"
    rec = obs_records[-1]
    assert rec.path == "memory-get"
    assert rec.hit is False
    assert rec.total_ms > 0
    assert rec.db_ms > 0
    assert rec.db_ms <= rec.total_ms

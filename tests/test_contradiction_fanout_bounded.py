"""The Path-C entity-context fan-out is concurrency-bounded.

Guards the connector-residual reliability fix: ``_bounded_gather`` must cap how
many entity-context coroutines run at once (so the fan-out can't open dozens of
simultaneous storage handshakes and saturate the VPC connector) while preserving
result order.
"""

from __future__ import annotations

import asyncio

import pytest

from core_api.services.contradiction_detector import _bounded_gather

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_bounded_gather_caps_concurrency_and_preserves_order():
    active = 0
    peak = 0

    async def _work(i: int) -> int:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.005)
            return i
        finally:
            active -= 1

    results = await _bounded_gather([_work(i) for i in range(30)], limit=5)

    assert results == list(range(30)), "order must be preserved (gather semantics)"
    assert peak <= 5, f"concurrency exceeded the cap: peak={peak}"
    assert peak > 1, "sanity: work did run concurrently up to the cap"

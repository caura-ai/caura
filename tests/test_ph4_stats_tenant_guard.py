"""Fix 2 Phase 4 — cross-tenant guard on /memories/stats-breakdown.

Regression test for the claude-review finding on PR #432: the stats-breakdown
endpoint must reject a request with no tenant scope (mirroring /list) rather than
running its aggregation unscoped across every tenant. Exercised through the typed
storage client bridged in-process to the storage app by the conftest ASGI fixture.
"""

from __future__ import annotations

import httpx
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_stats_breakdown_requires_tenant_id(sc):
    # A body lacking tenant_id (and readable_tenant_ids) must 422 — pre-fix this
    # produced WHERE-unscoped aggregates spanning all tenants.
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await sc.memory_stats_breakdown({})
    assert exc_info.value.response.status_code == 422

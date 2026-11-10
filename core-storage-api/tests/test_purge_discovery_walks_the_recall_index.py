"""Purge discovery finds tenants with old recall events by tenant, not by scan.

``recall_event``'s only index is ``(tenant_id, ts)``, so a filter on ``ts``
alone reads the whole table, and that table grows with every logged recall.
Discovery walks the index a tenant at a time instead. Planned with that index
as the table's only one (``plan_with_only_index``), every probe of the table is
keyed by ``tenant_id``. The control shows the check is not vacuous: the
ts-only filter it replaced fails it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from common.events.lifecycle_purge_request import MEMORY_RETENTION_MIN_DAYS
from core_storage_api.services import postgres_service
from tests.conftest import plan_with_only_index

pytestmark = pytest.mark.asyncio

_INDEX = "ix_recall_event_tenant_ts"


def _probes_by_tenant(plan: str) -> bool:
    conditions = [line for line in plan.splitlines() if "Index Cond" in line]
    return (
        "Seq Scan" not in plan
        and bool(conditions)
        and all("tenant_id" in condition for condition in conditions)
    )


async def test_the_recall_event_tenants_are_found_by_walking_the_index(_ensure_schema):
    walk = postgres_service._TENANTS_WITH_RECALL_EVENTS_BEFORE.bindparams(days=MEMORY_RETENTION_MIN_DAYS)
    plan = await plan_with_only_index(walk, _INDEX, table="recall_event")
    assert _probes_by_tenant(plan), plan

    scan = text("SELECT DISTINCT tenant_id FROM recall_event WHERE ts < now() - interval '1 day'")
    control = await plan_with_only_index(scan, _INDEX, table="recall_event")
    assert not _probes_by_tenant(control), control

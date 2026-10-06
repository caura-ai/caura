"""L-44 and L-52 — two crystallizer sweep queries that answered for the wrong rows.

Against real Postgres, because both findings are about which rows a predicate
or a sort reaches.

L-44: the dedup sweep pages its candidates with ``LIMIT``/``OFFSET`` over
``ORDER BY created_at`` alone. Rows written in one transaction share
``created_at``, and Postgres promises no order among tied rows from one page's
query to the next, so a page boundary inside a tie can offer one row twice and
skip another until a later sweep. The page now also sorts by ``id``, the order
the ``ix_memories_tenant_created_active`` index already keeps.

L-52: the nightly sweep asks ``crystallizer_activity_gate`` with no fleet,
meaning the whole tenant, but the report side then matched a completed sweep of
ANY fleet. A fleet-scoped manual run after the last write closed the
tenant-wide gate although the other fleets were never swept. With no fleet the
gate now reads only tenant-wide sweeps, as ``report_find_running`` does.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime

from core_storage_api.services.postgres_service import PostgresService

# No blanket ``pytest.mark.asyncio``: ``asyncio_mode = auto`` covers the async
# tests, and one test here is a plain source check.

_VEC = [0.1] * 1024


def _tenant() -> str:
    """The prefix the root suite's sweep reclaims by."""
    return f"test-tenant-{uuid.uuid4().hex[:8]}"


async def _memory(
    svc: PostgresService,
    tenant: str,
    *,
    fleet_id: str | None = None,
    created_at: datetime | None = None,
):
    data = {
        "tenant_id": tenant,
        "fleet_id": fleet_id,
        "agent_id": "sweep-scope-tester",
        "content": f"sweep scope canary {uuid.uuid4()}",
        "memory_type": "fact",
        "weight": 0.5,
        "status": "active",
        "visibility": "scope_team",
        "embedding": _VEC,
    }
    if created_at is not None:
        data["created_at"] = created_at
    return await svc.memory_add(data)


async def _completed_sweep(svc: PostgresService, tenant: str, fleet_id: str | None):
    return await svc.report_add(
        {
            "tenant_id": tenant,
            "fleet_id": fleet_id,
            "trigger": "manual",
            "status": "completed",
            "completed_at": datetime.now(UTC),
        }
    )


# ── L-44: the candidate page ──────────────────────────────────────────────


def test_the_candidate_page_has_a_total_order():
    """A tie on ``created_at`` must not leave the page boundary to the executor.
    A source check, because a test database may well return tied rows in a
    stable order by luck, which is exactly what the query must not rely on."""
    src = " ".join(inspect.getsource(PostgresService.memory_find_near_duplicate_pairs).split())
    assert "ORDER BY m.created_at DESC, m.id DESC LIMIT :batch_size OFFSET :batch_offset" in src


async def test_tied_rows_are_each_swept_exactly_once_across_pages():
    svc = PostgresService()
    tenant = _tenant()
    stamp = datetime.now(UTC)
    written = {(await _memory(svc, tenant, created_at=stamp)).id for _ in range(7)}

    seen: list = []
    for offset in range(0, 8, 2):
        rows = await svc.memory_find_near_duplicate_pairs(
            tenant_id=tenant, fleet_id=None, batch_size=2, offset=offset
        )
        seen.extend({r[0] for r in rows})

    assert len(seen) == len(set(seen)), f"a tied row was offered twice: {seen}"
    assert set(seen) == written


# ── L-52: the tenant-wide activity gate ───────────────────────────────────


async def test_a_fleet_sweep_does_not_close_the_tenant_wide_gate():
    svc = PostgresService()
    tenant = _tenant()
    await _memory(svc, tenant, fleet_id="fleet-y")
    await _completed_sweep(svc, tenant, "fleet-x")

    gate = await svc.crystallizer_activity_gate(tenant_id=tenant, fleet_id=None)

    assert gate["latest_memory_at"] is not None
    assert gate["last_sweep_at"] is None, "fleet-x's sweep answered for the whole tenant"


async def test_a_tenant_wide_sweep_still_closes_the_tenant_wide_gate():
    svc = PostgresService()
    tenant = _tenant()
    await _memory(svc, tenant, fleet_id="fleet-y")
    await _completed_sweep(svc, tenant, None)

    gate = await svc.crystallizer_activity_gate(tenant_id=tenant, fleet_id=None)

    assert gate["last_sweep_at"] is not None


async def test_a_fleet_gate_still_reads_its_own_fleet():
    svc = PostgresService()
    tenant = _tenant()
    await _memory(svc, tenant, fleet_id="fleet-x")
    await _completed_sweep(svc, tenant, "fleet-x")

    own = await svc.crystallizer_activity_gate(tenant_id=tenant, fleet_id="fleet-x")
    sibling = await svc.crystallizer_activity_gate(tenant_id=tenant, fleet_id="fleet-y")

    assert own["last_sweep_at"] is not None
    assert sibling["last_sweep_at"] is None

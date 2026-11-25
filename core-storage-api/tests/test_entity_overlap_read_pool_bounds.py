"""/memories/entity-overlap-candidates is bounded on the read pool.

On 2026-11-18 this query took 80% of the read pool's execution time and pinned
its CPU at 100%, so customer searches on the same pool timed out. Requests ran
to Cloud Run's 120s limit, and the query went on running after it. Two changes:

* the query caps its own transaction with ``statement_timeout``
  (``Settings.entity_overlap_statement_timeout_ms``), and the route answers 504
  when the cap fires;
* the candidate entities are filtered by tenant. No index leads with the name,
  so finding the seed's names meant scanning every tenant's entities, and the
  planner walked the tenant's memories and all their links instead.
"""

from __future__ import annotations

import contextlib
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text

import core_storage_api.services.postgres_service as ps
from core_storage_api.config import settings as storage_settings
from tests.conftest import plan_with_only_index
from tests.test_integration import PREFIX, _memory_payload

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

FLEET = "fleet-overlap-bounds"


async def _entity(client: AsyncClient, tenant: str, name: str) -> str:
    resp = await client.post(
        f"{PREFIX}/entities",
        json={"tenant_id": tenant, "entity_type": "person", "canonical_name": name},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _memory(client: AsyncClient, tenant: str) -> str:
    resp = await client.post(f"{PREFIX}/memories", json=_memory_payload(tenant, FLEET))
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _link(client: AsyncClient, tenant: str, memory_id: str, entity_id: str) -> None:
    item = {"input_idx": 0, "memory_id": memory_id, "entity_id": entity_id, "role": "subject"}
    resp = await client.post(f"{PREFIX}/entities/links/bulk", json={"tenant_id": tenant, "items": [item]})
    assert resp.status_code == 200, resp.text
    assert resp.json()[0].get("error") is None, resp.text


def _candidates_request(tenant: str, memory_id: str) -> dict:
    return {"tenant_id": tenant, "fleet_id": FLEET, "memory_id": memory_id}


def _wrap_session(monkeypatch, before_execute):
    """Await ``before_execute(session, stmt)`` ahead of each statement the
    finder issues on its session."""
    real_get_session = ps.get_session

    @contextlib.asynccontextmanager
    async def _wrapped():
        async with real_get_session() as session:

            class _Spy:
                async def execute(self, stmt, *args, **kwargs):
                    await before_execute(session, stmt)
                    return await session.execute(stmt, *args, **kwargs)

            yield _Spy()

    monkeypatch.setattr(ps, "get_session", _wrapped)


def _plan_node(plan: str, marker: str) -> list[str]:
    """The lines of the plan node whose line holds ``marker``, children included."""
    lines = plan.splitlines()
    start = next((i for i, line in enumerate(lines) if marker in line), None)
    assert start is not None, f"no {marker!r} node in the plan:\n{plan}"

    def depth(line: str) -> int:
        return len(line) - len(line.lstrip())

    end = next(
        (i for i in range(start + 1, len(lines)) if depth(lines[i]) <= depth(lines[start])), len(lines)
    )
    return lines[start:end]


# ── statement timeout ─────────────────────────────────────────────────────


async def test_statement_timeout_is_live_when_the_query_runs(tenant_id, monkeypatch) -> None:
    monkeypatch.setattr(storage_settings, "entity_overlap_statement_timeout_ms", 1234)
    observed: list[str] = []

    async def _record(session, stmt):
        if "set_config" not in str(stmt):
            observed.append((await session.execute(text("SHOW statement_timeout"))).scalar())

    _wrap_session(monkeypatch, _record)
    # An unknown seed finds nothing; the assertion is the GUC.
    assert await ps.PostgresService().memory_find_entity_overlap_candidates(uuid.uuid4(), tenant_id) == []
    assert observed and set(observed) == {"1234ms"}, (
        f"statement_timeout was not live when the overlap query ran: {observed!r}"
    )


async def test_a_query_past_its_timeout_answers_504(client: AsyncClient, tenant_id, monkeypatch) -> None:
    monkeypatch.setattr(storage_settings, "entity_overlap_statement_timeout_ms", 100)

    async def _stall(session, stmt):
        if "set_config" not in str(stmt):
            # Stands in for a scan slower than the cap.
            await session.execute(text("SELECT pg_sleep(1)"))

    _wrap_session(monkeypatch, _stall)
    resp = await client.post(
        f"{PREFIX}/memories/entity-overlap-candidates",
        json=_candidates_request(tenant_id, str(uuid.uuid4())),
    )
    assert resp.status_code == 504, resp.text
    assert "statement timeout" in resp.json()["detail"]


# ── tenant filter on the candidate entities ───────────────────────────────


async def test_a_link_to_another_tenants_entity_makes_no_candidate(client: AsyncClient) -> None:
    """The filter's only effect on results: a link that crosses tenants.

    Every current writer refuses such a link, so it is inserted with raw SQL,
    standing in for one written before those checks. The control row shows the
    name match itself still works.
    """
    tenant = f"test-tenant-{uuid.uuid4().hex[:8]}"
    other = f"test-tenant-{uuid.uuid4().hex[:8]}"
    name = f"Overlap-{uuid.uuid4().hex}"
    own_entity = await _entity(client, tenant, name)
    foreign_entity = await _entity(client, other, name)

    seed = await _memory(client, tenant)
    await _link(client, tenant, seed, own_entity)
    control = await _memory(client, tenant)
    await _link(client, tenant, control, own_entity)
    crossed = await _memory(client, tenant)
    async with ps.get_session() as session:
        await session.execute(
            text(
                "INSERT INTO memory_entity_links (memory_id, entity_id, role) "
                "VALUES (CAST(:m AS uuid), CAST(:e AS uuid), 'subject')"
            ),
            {"m": crossed, "e": foreign_entity},
        )

    resp = await client.post(
        f"{PREFIX}/memories/entity-overlap-candidates", json=_candidates_request(tenant, seed)
    )
    assert resp.status_code == 200, resp.text
    assert {row["id"] for row in resp.json()} == {control}


@pytest.mark.parametrize("index", ["ix_entities_tenant_id", "uq_entities_tenant_type_name_fleet"])
async def test_a_tenant_index_can_drive_the_candidate_entities(tenant_id, monkeypatch, index: str) -> None:
    """The point of the filter: an index leading with ``tenant_id`` can serve
    the candidate side. Both such indexes are checked, because production's
    ``entities`` predates the migration chain and need not have 001's
    ``ix_entities_tenant_id`` (see migration 068)."""
    captured = []

    async def _capture(_session, stmt):
        captured.append(stmt)

    _wrap_session(monkeypatch, _capture)
    await ps.PostgresService().memory_find_entity_overlap_candidates(uuid.uuid4(), tenant_id, fleet_id=FLEET)
    stmt = next(s for s in captured if "other_ent" in str(s))

    plan = await plan_with_only_index(stmt, index, table="entities")
    node = _plan_node(plan, "on entities other_ent")
    assert any(index in line for line in node), f"{index} does not serve the candidate entities:\n{plan}"
    assert any("Index Cond:" in line and f"tenant_id = '{tenant_id}'" in line for line in node), (
        f"the candidate entities are not narrowed to the tenant by {index}:\n{plan}"
    )

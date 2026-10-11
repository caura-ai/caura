"""M-26: a bulletin STM write with no fleet is refused, not parked under 'default'.

``scope_team`` and ``scope_org`` short-term writes go to a fleet bulletin. When
neither the request nor the agent named a fleet, the write landed under fleet
'default' and answered 201 with a TTL. Nothing reads that key back for the
writer: search injects only the caller's fleets, and ``GET /stm/bulletin``
refuses 'default' to a trust-1 agent, which has no home fleet to match it. The
receipt promised an entry the writer could never see.
"""

from __future__ import annotations

import pytest

import core_api.services.stm_service as stm_service
from core_api.config import settings
from tests.conftest import get_test_auth
from tests.conftest import uid as _uid

pytestmark = pytest.mark.asyncio


@pytest.fixture
def stm_on(monkeypatch):
    """STM on, over a fresh in-memory backend."""
    monkeypatch.setattr(settings, "use_stm", True)
    monkeypatch.setattr(settings, "stm_backend", "memory")
    monkeypatch.setattr(stm_service, "_stm_instance", None)


async def _write(client, headers, **body):
    return await client.post(
        "/api/v1/memories",
        json={"content": "a short-term note for the team", "write_mode": "stm", **body},
        headers=headers,
    )


async def test_a_bulletin_write_with_no_fleet_is_refused(client, stm_on):
    tenant_id, headers = get_test_auth()
    agent = f"m26-fleetless-{_uid()}"

    resp = await _write(
        client, headers, tenant_id=tenant_id, agent_id=agent, visibility="scope_team"
    )

    assert resp.status_code == 422, resp.text
    assert "fleet" in resp.text
    assert await stm_service.read_bulletin(tenant_id, "default") == []


async def test_a_bulletin_write_names_its_fleet(client, stm_on):
    """Unchanged: a fleet in the request is where the entry goes."""
    tenant_id, headers = get_test_auth()
    fleet = f"m26-fleet-{_uid()}"

    resp = await _write(
        client,
        headers,
        tenant_id=tenant_id,
        agent_id=f"m26-agent-{_uid()}",
        fleet_id=fleet,
        visibility="scope_org",
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["target"] == "bulletin"
    entries = await stm_service.read_bulletin(tenant_id, fleet)
    assert [e["id"] for e in entries] == [resp.json()["id"]]


async def test_a_private_note_needs_no_fleet(client, stm_on):
    """Unchanged: ``scope_agent`` goes to the agent's own notes, fleet or not."""
    tenant_id, headers = get_test_auth()
    agent = f"m26-fleetless-{_uid()}"

    resp = await _write(
        client, headers, tenant_id=tenant_id, agent_id=agent, visibility="scope_agent"
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["target"] == "notes"
    notes = await stm_service.read_notes(tenant_id, agent)
    assert [n["id"] for n in notes] == [resp.json()["id"]]

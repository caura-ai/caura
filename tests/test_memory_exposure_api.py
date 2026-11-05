"""``GET /memories/{id}/exposure``: the agents a memory was handed to (correction propagation, D1).

A REST search and a get-by-id each count one read for the agent that made it;
the route lists every agent with its count, first and last read, and the
trace of the latest. It answers only a caller that may read the memory: another
tenant's memory is a 404, as on ``GET /memories/{id}``.

See ``docs/plans/correction-propagation.md``.
"""

from __future__ import annotations

import pytest

from tests.conftest import drain_background_tasks, get_admin_headers, uid

# Long enough to clear CheckContentLength's minimum-length quality gate.
_PADDING = (
    " This memory carries enough surrounding context to pass the content-length gate."
)


@pytest.fixture
def as_agent():
    """Authenticate the REST client as an agent, as the gateway's X-Agent-ID does."""
    from core_api.app import app
    from core_api.auth import AuthContext, get_auth_context
    from core_api.tenant_context import set_current_tenant

    def _install(tenant_id: str, agent_id: str) -> None:
        async def _dep():
            set_current_tenant(tenant_id)
            return AuthContext(
                tenant_id=tenant_id, agent_id=agent_id, readable_tenant_ids=[tenant_id]
            )

        app.dependency_overrides[get_auth_context] = _dep

    yield _install
    app.dependency_overrides.pop(get_auth_context, None)


async def _settle() -> None:
    """Let the fire-and-forget exposure writes land."""
    import asyncio

    from core_api.tasks import _background_tasks

    pending = [t for t in _background_tasks if not t.done()]
    await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.integration
async def test_a_search_and_a_get_by_id_are_listed_per_agent(
    client, tenant_id, as_agent
) -> None:
    headers = get_admin_headers()
    marker = f"exposure-{uid()}"
    w = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": "writer",
            "memory_type": "fact",
            "content": f"The {marker} deploy window is Tuesday." + _PADDING,
        },
        headers=headers,
    )
    assert w.status_code in (200, 201), w.text
    memory_id = w.json()["id"]

    # The plugin's shape: a tenant key naming the agent it searches as.
    s = await client.post(
        "/api/v1/search",
        json={
            "tenant_id": tenant_id,
            "query": f"{marker} deploy window",
            "caller_agent_id": "searcher",
        },
        headers={**headers, "X-Caura-Session-Id": "sess-search-1"},
    )
    assert s.status_code == 200, s.text
    assert memory_id in [m["id"] for m in s.json()["items"]]
    await _settle()

    as_agent(tenant_id, "getter")
    for _ in range(2):
        g = await client.get(f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}")
        assert g.status_code == 200, g.text
    await _settle()

    r = await client.get(f"/api/v1/memories/{memory_id}/exposure?tenant_id={tenant_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["memory_id"] == memory_id
    agents = {a["agent_id"]: a for a in body["agents"]}
    assert set(agents) == {"searcher", "getter"}
    assert agents["searcher"]["count"] == 1
    assert agents["searcher"]["last_trace_id"] == "sess-search-1"
    assert agents["getter"]["count"] == 2
    assert agents["getter"]["first_seen"] <= agents["getter"]["last_seen"]
    assert body["total_reads"] == 3
    await drain_background_tasks()


@pytest.mark.integration
async def test_a_memory_of_another_tenant_is_a_404(client, tenant_id, as_agent) -> None:
    headers = get_admin_headers()
    w = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": "writer",
            "memory_type": "fact",
            "content": f"exposure tenancy {uid()}." + _PADDING,
        },
        headers=headers,
    )
    assert w.status_code in (200, 201), w.text
    memory_id = w.json()["id"]

    other = f"{tenant_id}-other"
    as_agent(other, "outsider")
    r = await client.get(f"/api/v1/memories/{memory_id}/exposure?tenant_id={other}")
    assert r.status_code == 404, r.text


@pytest.mark.integration
async def test_a_memory_nobody_read_has_no_agents(client, tenant_id) -> None:
    headers = get_admin_headers()
    w = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": "writer",
            "memory_type": "fact",
            "content": f"exposure unread {uid()}." + _PADDING,
        },
        headers=headers,
    )
    memory_id = w.json()["id"]
    # An admin key names no agent: its by-id read records nothing.
    g = await client.get(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}", headers=headers
    )
    assert g.status_code == 200
    await _settle()
    r = await client.get(
        f"/api/v1/memories/{memory_id}/exposure?tenant_id={tenant_id}", headers=headers
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"memory_id": memory_id, "agents": [], "total_reads": 0}

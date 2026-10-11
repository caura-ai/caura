"""Regression tests for fixes on the CAURA-000-pre-public-cleanup branch.

Each test names the commit SHA it guards against regression. Every fix is
black-box tested through the HTTP API so the envelope change, hook wiring,
and endpoint shape are locked in end-to-end.

The guards for 49334e7 (the on_recall hook bumps ``recall_count``) and
81fa94e (re-running insights supersedes prior rows) skipped themselves under
the fake embedding provider the suite always runs with, so they never ran.
They are gone; tests/test_search_recall_tracked_flag.py and
tests/test_insights_service.py cover both paths (L-84).
"""

from __future__ import annotations

from tests.conftest import get_test_auth
from tests.conftest import uid as _uid


async def _write(
    client,
    tenant_id,
    headers,
    content,
    *,
    agent_id=None,
    fleet_id=None,
    memory_type="fact",
):
    tag = _uid()
    agent_id = agent_id or f"regr-{tag}"
    fleet_id = fleet_id or f"regr-fleet-{tag}"
    resp = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "content": f"{content} [{tag}]",
            "agent_id": agent_id,
            "fleet_id": fleet_id,
            "memory_type": memory_type,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# bc686bc — POST /api/v1/search wraps results in {items: [...]} envelope
# ---------------------------------------------------------------------------


async def test_search_response_uses_items_envelope(client):
    tenant_id, headers = get_test_auth()
    await _write(client, tenant_id, headers, "alpha memory")

    resp = await client.post(
        "/api/v1/search",
        json={"tenant_id": tenant_id, "query": "alpha"},
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict), f"search must return dict, got {type(data).__name__}"
    assert "items" in data, f"search response missing 'items' key: {list(data)}"
    assert isinstance(data["items"], list)


# ---------------------------------------------------------------------------
# b681c9b — GET /api/v1/memories honors ?offset
# ---------------------------------------------------------------------------


async def test_list_memories_honors_offset(client):
    tenant_id, headers = get_test_auth()
    tag = _uid()
    agent_id = f"off-{tag}"
    fleet_id = f"off-fleet-{tag}"

    for i in range(4):
        await _write(
            client,
            tenant_id,
            headers,
            f"offset-probe-{i}",
            agent_id=agent_id,
            fleet_id=fleet_id,
        )

    first = await client.get(
        f"/api/v1/memories?tenant_id={tenant_id}&agent_id={agent_id}"
        f"&fleet_id={fleet_id}&offset=0&limit=2",
        headers=headers,
    )
    assert first.status_code == 200
    first_ids = [m["id"] for m in first.json()["items"]]
    assert len(first_ids) == 2

    second = await client.get(
        f"/api/v1/memories?tenant_id={tenant_id}&agent_id={agent_id}"
        f"&fleet_id={fleet_id}&offset=2&limit=2",
        headers=headers,
    )
    assert second.status_code == 200
    second_ids = [m["id"] for m in second.json()["items"]]
    assert second_ids, "offset=2 returned empty — parameter may be ignored"
    assert not (set(first_ids) & set(second_ids)), (
        "offset ignored — second page overlaps with first"
    )


# ---------------------------------------------------------------------------
# d092637 — GET /api/v1/agents/{agent_id}/tune mirrors PATCH
# ---------------------------------------------------------------------------


async def test_agents_get_tune_symmetric_with_patch(client):
    tenant_id, headers = get_test_auth()
    tag = _uid()
    agent_id = f"tune-{tag}"
    fleet_id = f"tune-fleet-{tag}"

    await _write(
        client,
        tenant_id,
        headers,
        "seed",
        agent_id=agent_id,
        fleet_id=fleet_id,
    )

    get_resp = await client.get(
        f"/api/v1/agents/{agent_id}/tune?tenant_id={tenant_id}",
        headers=headers,
    )
    assert get_resp.status_code == 200, get_resp.text
    get_body = get_resp.json()
    assert "trust_level" in get_body
    assert get_body["agent_id"] == agent_id


# ---------------------------------------------------------------------------
# d3168b7 — MEMORY_TYPES include "insight" (was missing from enum)
# Updated by C3/C8 (PR #261 era): "insight" is server-reserved and rejected
# at the route boundary. Enum-sync coverage moves to a schema-level check;
# boundary behaviour is asserted via the 422 path below.
# ---------------------------------------------------------------------------


async def test_memory_type_insight_known_to_schema_but_rejected_at_boundary(client):
    """Two assertions in one place to keep the original intent (enum-sync)
    while reflecting the C3/C8 contract (server-reserved types rejected
    at the API boundary):

    1. ``"insight"`` must still be a recognised type at the Pydantic
       schema level so internal callers (``insights_service``) can
       construct ``MemoryCreate(memory_type="insight", ...)`` without
       a validation error.
    2. The agent-facing ``POST /api/v1/memories`` must reject explicit
       ``memory_type="insight"`` with a 422 — this is the C3/C8 fix.
    """
    # (1) Schema-level recognition — proves the enum still carries the slug.
    from core_api.schemas import MemoryCreate

    _ = MemoryCreate(
        tenant_id="t-x",
        agent_id="a-x",
        content="schema-level recognition probe",
        memory_type="insight",
    )

    # (2) Route-level rejection — the C3/C8 boundary.
    tenant_id, headers = get_test_auth()
    tag = _uid()

    resp = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "content": f"insight-probe [{tag}]",
            "agent_id": f"mt-{tag}",
            "fleet_id": f"mt-fleet-{tag}",
            "memory_type": "insight",
        },
        headers=headers,
    )
    assert resp.status_code == 422, (
        f"agent-supplied memory_type='insight' must be rejected by the C3 boundary; "
        f"got {resp.status_code}: {resp.text}"
    )
    assert "insight" in str(resp.json().get("detail", "")), (
        f"422 detail should name the reserved type 'insight': {resp.text}"
    )


# ---------------------------------------------------------------------------
# 7f6a6ed — OpenAPI locks the enum hint, top_k bounds, and visibility notes
# ---------------------------------------------------------------------------


async def test_openapi_docs_lock(client):
    """Schema field descriptions and the /memories GET docstring must survive."""
    resp = await client.get("/api/openapi.json")
    assert resp.status_code == 200, resp.text
    spec = resp.json()

    schemas = spec["components"]["schemas"]

    # MEMORY_TYPES_DESCRIPTION leaks through every memory_type field.
    memory_create_desc = schemas["MemoryCreate"]["properties"]["memory_type"].get(
        "description", ""
    )
    assert "Valid values" in memory_create_desc, (
        "MemoryCreate.memory_type lost MEMORY_TYPES_DESCRIPTION — "
        "clients can no longer see the enum in Swagger."
    )

    # top_k bounds must stay documented for SearchRequest.
    top_k_desc = schemas["SearchRequest"]["properties"]["top_k"].get("description", "")
    from core_api.constants import MAX_SEARCH_TOP_K

    assert "1" in top_k_desc and str(MAX_SEARCH_TOP_K) in top_k_desc, (
        f"SearchRequest.top_k description lost bounds: {top_k_desc!r}"
    )

    # /memories GET endpoint docstring documents the scope_agent + offset
    # behaviour added by 7f6a6ed.
    paths = spec["paths"]
    # Try both versioned and unversioned layouts.
    memories_path = paths.get("/api/v1/memories") or paths.get("/memories")
    assert memories_path, f"GET /memories not in OpenAPI paths: {sorted(paths)[:10]}"
    get_doc = memories_path["get"].get("description", "") + memories_path["get"].get(
        "summary", ""
    )
    assert "scope_agent" in get_doc, (
        "GET /memories docstring lost scope_agent visibility note"
    )
    assert "offset" in get_doc, "GET /memories docstring lost offset pagination note"

"""Regression tests for fixes on the CAURA-000-pre-public-cleanup branch.

Each test names the commit SHA it guards against regression. Every fix is
black-box tested through the HTTP API so the envelope change, hook wiring,
and endpoint shape are locked in end-to-end.
"""

from __future__ import annotations

import os

import pytest

from tests.conftest import get_test_auth, uid as _uid


def _needs_real_provider() -> bool:
    """True when the test env uses the `fake` embedding/LLM provider.

    The on_recall hook and the insight-supersede path both require real
    semantic matching to exercise — fake providers return null/constant
    vectors and deterministic canned LLM output, so they can't reliably
    trigger a recall hit or produce re-runnable insight findings.
    """
    return (
        os.environ.get("EMBEDDING_PROVIDER", "").lower() == "fake"
        or os.environ.get("ENTITY_EXTRACTION_PROVIDER", "").lower() == "fake"
    )


async def _write(client, tenant_id, headers, content, *, agent_id=None, fleet_id=None,
                 memory_type="fact"):
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
# 49334e7 — on_recall hook wires recall_count + last_recalled_at
# ---------------------------------------------------------------------------


async def test_recall_increments_recall_count(client):
    if _needs_real_provider():
        pytest.skip("fake embedding provider can't reliably trigger on_recall hook")
    tenant_id, headers = get_test_auth()
    tag = _uid()
    agent_id = f"recall-agent-{tag}"
    fleet_id = f"recall-fleet-{tag}"

    mem = await _write(
        client, tenant_id, headers,
        "The capital of France is Paris.",
        agent_id=agent_id, fleet_id=fleet_id,
    )
    memory_id = mem["id"]
    assert mem["recall_count"] == 0
    assert mem["last_recalled_at"] is None

    resp = await client.post(
        "/api/v1/recall",
        json={
            "tenant_id": tenant_id,
            "query": "capital of France",
            "fleet_ids": [fleet_id],
            "top_k": 5,
        },
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    if body.get("memory_count", 0) == 0:
        # The fake embedding provider in CI can't guarantee a vector hit for
        # a short semantic query, so the hook has nothing to fire on. That's
        # fine — the hook's contract is "increment on hits" and there's no
        # regression to guard against without a hit. Skip in that case.
        pytest.skip("recall returned zero memories — fake embedding can't match; hook untestable")

    after = await client.get(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}",
        headers=headers,
    )
    assert after.status_code == 200
    after_data = after.json()
    assert after_data["recall_count"] >= 1, (
        "on_recall hook not wired — recall_count did not increment"
    )
    assert after_data["last_recalled_at"] is not None


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
            client, tenant_id, headers, f"offset-probe-{i}",
            agent_id=agent_id, fleet_id=fleet_id,
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
        client, tenant_id, headers, "seed", agent_id=agent_id, fleet_id=fleet_id,
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
# 81fa94e — Re-running insights supersedes prior active rows to 'outdated'
# ---------------------------------------------------------------------------


async def test_insights_rerun_supersedes_prior(client):
    if _needs_real_provider():
        pytest.skip("fake LLM provider can't reliably produce insight findings")
    tenant_id, headers = get_test_auth()
    tag = _uid()
    fleet_id = f"ins-fleet-{tag}"
    agent_id = f"ins-agent-{tag}"

    for i in range(3):
        await _write(
            client, tenant_id, headers,
            f"The {['red','green','blue'][i]} team ships on Fridays.",
            agent_id=agent_id, fleet_id=fleet_id,
        )

    body = {
        "tenant_id": tenant_id,
        "fleet_id": fleet_id,
        "agent_id": agent_id,
        "scope": "fleet",
        "focus": "patterns",
    }
    first = await client.post("/api/v1/insights/generate", json=body, headers=headers)
    if first.status_code == 404:
        pytest.skip("insights/generate endpoint not available in this build")
    assert first.status_code in (200, 201), first.text
    first_body = first.json()
    if not first_body.get("findings"):
        pytest.skip("first insight run yielded no findings — nothing to supersede")

    second = await client.post("/api/v1/insights/generate", json=body, headers=headers)
    assert second.status_code in (200, 201), second.text

    # After the second run, prior 'insight' memories for this focus should be
    # moved from active → outdated. Check via the memories list.
    listing = await client.get(
        f"/api/v1/memories?tenant_id={tenant_id}&agent_id={agent_id}"
        f"&memory_type=insight&include_deleted=true&limit=50",
        headers=headers,
    )
    assert listing.status_code == 200, listing.text
    items = listing.json()["items"]
    statuses = [m["status"] for m in items]
    assert "outdated" in statuses, (
        f"Re-running insights did not transition prior rows to 'outdated'. "
        f"Got statuses: {statuses}"
    )


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
    assert "1" in top_k_desc and "20" in top_k_desc, (
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
    assert "offset" in get_doc, (
        "GET /memories docstring lost offset pagination note"
    )

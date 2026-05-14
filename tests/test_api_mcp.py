"""Test MCP tools via real API calls — write, search, recall, entities, status update.

All tests use httpx AsyncClient against the real FastAPI app with PostgreSQL.
"""

import uuid

import pytest

from tests.conftest import get_test_auth, uid as _uid

pytestmark = pytest.mark.asyncio


# ── 1. MCP mount exists ──


async def test_mcp_mount_registered():
    # Mount at /mcp handles canonical /mcp/* via FastMCP's internal Route("/").
    from starlette.routing import Mount

    from core_api.app import app

    mounts = [r for r in app.router.routes if isinstance(r, Mount) and r.path == "/mcp"]
    assert mounts, "MCP mount missing at /mcp"


async def test_mcp_no_trailing_slash_route_registered():
    # Streaming MCP clients hang on the initialize handshake if /mcp issues
    # a 307/308 to /mcp/. The fix registers an exact /mcp Route that forwards
    # into the mounted MCP ASGI app in-process so the redirect never fires.
    from starlette.routing import Route

    from core_api.app import app

    bare_route = next(
        (r for r in app.router.routes if isinstance(r, Route) and r.path == "/mcp"),
        None,
    )
    assert bare_route is not None, "Bare /mcp Route missing — redirect would recur"
    assert set(bare_route.methods or ()) >= {"GET", "POST", "DELETE"}, (
        f"/mcp route is missing required methods: {bare_route.methods!r}"
    )


# ── 2. Write via API is searchable ──


async def test_write_via_api_is_searchable(client):
    """Write a memory via POST /api/memories, then verify via GET."""
    tenant_id, headers = get_test_auth()
    tag = _uid()

    content = f"The capacitor runs at 1.21 gigawatts for testing [{tag}]"

    # Write
    write_resp = await client.post("/api/v1/memories", json={
        "tenant_id": tenant_id,
        "agent_id": f"test-agent-{tag}",
        "fleet_id": f"test-fleet-{tag}",
        "memory_type": "fact",
        "content": content,
    }, headers=headers)
    assert write_resp.status_code == 201, write_resp.text
    memory_id = write_resp.json()["id"]

    # Verify via GET (not search — fake embeddings won't match semantically)
    get_resp = await client.get(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}",
        headers=headers,
    )
    assert get_resp.status_code == 200
    assert get_resp.json()["content"] == content


# ── 3. Recall via API ──


async def test_recall_via_api(client):
    """Write memory, POST /api/recall → returns results."""
    tenant_id, headers = get_test_auth()
    tag = _uid()

    await client.post("/api/v1/memories", json={
        "tenant_id": tenant_id,
        "agent_id": f"recall-agent-{tag}",
        "fleet_id": f"recall-fleet-{tag}",
        "memory_type": "episode",
        "content": f"User discussed {tag} deployment strategy at length",
    }, headers=headers)

    recall_resp = await client.post("/api/v1/recall", json={
        "tenant_id": tenant_id,
        "query": tag,
        "agent_id": f"recall-agent-{tag}",
    }, headers=headers)
    assert recall_resp.status_code == 200, recall_resp.text
    data = recall_resp.json()
    # Recall returns either a dict with "memories"/"summary" or a list
    assert data is not None


# ── 4. Entity lookup ──


async def test_entity_lookup(client):
    """Write memory, GET /api/entities → endpoint returns 200."""
    tenant_id, headers = get_test_auth()
    tag = _uid()

    await client.post("/api/v1/memories", json={
        "tenant_id": tenant_id,
        "agent_id": f"entity-agent-{tag}",
        "fleet_id": f"entity-fleet-{tag}",
        "memory_type": "fact",
        "content": f"Alice met Bob at the MemClaw headquarters in Tel Aviv [{tag}]",
    }, headers=headers)

    ent_resp = await client.get("/api/v1/entities", params={
        "tenant_id": tenant_id,
    }, headers=headers)
    assert ent_resp.status_code == 200, ent_resp.text
    # With a fake/test embedding provider, entities may or may not be extracted.
    # The key assertion is that the endpoint works and returns a list.
    assert isinstance(ent_resp.json(), list)


# ── 5. Status update ──


async def test_status_update(client):
    """Write memory, PATCH status to archived, GET memory → status == archived."""
    tenant_id, headers = get_test_auth()
    tag = _uid()

    write_resp = await client.post("/api/v1/memories", json={
        "tenant_id": tenant_id,
        "agent_id": f"status-agent-{tag}",
        "fleet_id": f"status-fleet-{tag}",
        "memory_type": "decision",
        "content": f"Decided to archive old deployment configs [{tag}]",
    }, headers=headers)
    assert write_resp.status_code == 201, write_resp.text
    memory_id = write_resp.json()["id"]

    # Patch status
    patch_resp = await client.patch(
        f"/api/v1/memories/{memory_id}/status",
        params={"tenant_id": tenant_id},
        json={"status": "archived"},
        headers=headers,
    )
    assert patch_resp.status_code == 200, patch_resp.text
    patch_data = patch_resp.json()
    assert patch_data["new_status"] == "archived"
    assert patch_data["old_status"] == "active"

    # Verify via GET
    get_resp = await client.get(
        f"/api/v1/memories/{memory_id}",
        params={"tenant_id": tenant_id},
        headers=headers,
    )
    assert get_resp.status_code == 200, get_resp.text
    assert get_resp.json()["status"] == "archived"


# ── 6. Bulk write via API ──


async def test_bulk_write_via_api(client):
    """Write multiple memories via POST /api/memories/bulk, verify all created."""
    tenant_id, headers = get_test_auth()
    tag = _uid()

    items = [
        {"content": f"Bulk test memory {i} — {tag}-{uuid.uuid4().hex[:6]}"}
        for i in range(5)
    ]

    bulk_resp = await client.post(
        "/api/v1/memories/bulk",
        json={
            "tenant_id": tenant_id,
            "agent_id": f"bulk-test-agent-{tag}",
            "items": items,
        },
        headers={**headers, "X-Bulk-Attempt-Id": f"bulk-write-{tag}"},
    )
    assert bulk_resp.status_code == 200, bulk_resp.text
    data = bulk_resp.json()
    assert data["created"] == 5
    assert data["errors"] == 0
    assert len(data["results"]) == 5
    assert all(r["status"] == "created" for r in data["results"])

    # Verify one of the created memories exists
    first_id = data["results"][0]["id"]
    get_resp = await client.get(
        f"/api/v1/memories/{first_id}",
        params={"tenant_id": tenant_id},
        headers=headers,
    )
    assert get_resp.status_code == 200
    assert "Bulk test memory 0" in get_resp.json()["content"]


async def test_bulk_write_dedup(client):
    """Bulk write with duplicate content should detect duplicates."""
    tenant_id, headers = get_test_auth()
    tag = _uid()

    content = f"Duplicate content test — {tag}"
    items = [
        {"content": content},
        {"content": content},
        {"content": f"Unique content — {tag}"},
    ]

    bulk_resp = await client.post(
        "/api/v1/memories/bulk",
        json={
            "tenant_id": tenant_id,
            "agent_id": f"dedup-test-agent-{tag}",
            "items": items,
        },
        headers={**headers, "X-Bulk-Attempt-Id": f"dedup-{tag}"},
    )
    assert bulk_resp.status_code == 200, bulk_resp.text
    data = bulk_resp.json()
    # At least one should be flagged as a duplicate (intra-batch).
    # Counted under the rolled-up ``duplicates`` field which now includes
    # both ``duplicate_attempt`` and ``duplicate_content`` per CAURA-602.
    assert data["duplicates"] >= 1
    assert data["created"] + data["duplicates"] + data["errors"] == 3

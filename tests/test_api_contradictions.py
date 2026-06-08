"""Tests for GET /memories/{memory_id}/contradictions (CAURA-604).

Covers the wiring between the contradiction detector's persisted
side-effects (memory.status + memory.supersedes_id) and the GET
endpoint's response shape:

- ``test_contradictions_pending_when_no_findings``: a freshly-written
  memory with no detected contradictions returns
  ``detection_status == "pending"`` and ``contradictions == []``.
- ``test_contradictions_completed_with_supersedes_chain``: after the
  detector marks an older memory ``outdated`` and points the newer
  memory's ``supersedes_id`` at it, the GET endpoint returns
  ``detection_status == "completed"`` and surfaces the chain in the
  ``contradictions`` list with the right ``direction``.
"""

from tests.conftest import get_test_auth, uid as _uid


async def _write_memory(
    client, tenant_id, headers, content, *, agent_id=None, fleet_id=None
):
    tag = _uid()
    if agent_id is None:
        agent_id = f"test-agent-{tag}"
    if fleet_id is None:
        fleet_id = f"test-fleet-{tag}"
    resp = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "content": f"{content} [{tag}]",
            "agent_id": agent_id,
            "fleet_id": fleet_id,
            "memory_type": "fact",
        },
        headers=headers,
    )
    assert resp.status_code == 201, f"Write failed: {resp.text}"
    return resp.json()


async def test_contradictions_pending_when_no_findings(client):
    """A memory with no detector evidence -> detection_status=pending, empty list."""
    tenant_id, headers = get_test_auth()

    mem = await _write_memory(client, tenant_id, headers, "User likes oat milk")

    resp = await client.get(
        f"/api/v1/memories/{mem['id']}/contradictions?tenant_id={tenant_id}",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["memory_id"] == mem["id"]
    assert data["detection_status"] == "pending"
    assert data["contradictions"] == []
    # Back-compat: existing fields preserved.
    assert data["superseded_by"] is None
    assert data["superseded_memories"] == []


async def test_contradictions_completed_with_supersedes_chain(client, sc):
    """Detector evidence in the chain -> detection_status=completed, contradictions populated.

    Simulates what the detector does post-commit: marks the older row
    outdated and points the newer row's ``supersedes_id`` at it. The
    endpoint should then surface both directions of the chain.
    """
    tenant_id, headers = get_test_auth()
    shared_agent = f"agent-{_uid()}"
    shared_fleet = f"fleet-{_uid()}"

    old = await _write_memory(
        client,
        tenant_id,
        headers,
        "User lives in Tel Aviv",
        agent_id=shared_agent,
        fleet_id=shared_fleet,
    )
    new = await _write_memory(
        client,
        tenant_id,
        headers,
        "User lives in Haifa",
        agent_id=shared_agent,
        fleet_id=shared_fleet,
    )

    # Simulate detector side-effects via the storage client (same path
    # the real detector uses): old becomes outdated, new.supersedes_id
    # points at old.
    await sc.update_memory_status(old["id"], "outdated")
    await sc.update_memory_status(new["id"], "active", supersedes_id=old["id"])

    # ---- From the OLDER memory's perspective: it was superseded.
    resp_old = await client.get(
        f"/api/v1/memories/{old['id']}/contradictions?tenant_id={tenant_id}",
        headers=headers,
    )
    assert resp_old.status_code == 200, resp_old.text
    data_old = resp_old.json()
    assert data_old["detection_status"] == "completed"
    # The newer memory shows up as a "superseded_by" entry pointing at new.
    superseded_by_entries = [
        c for c in data_old["contradictions"] if c["direction"] == "superseded_by"
    ]
    assert len(superseded_by_entries) == 1
    assert superseded_by_entries[0]["memory_id"] == new["id"]
    # Back-compat: supersessor list still populated.
    assert any(m["id"] == new["id"] for m in data_old["superseded_memories"])

    # ---- From the NEWER memory's perspective: it supersedes the old one.
    resp_new = await client.get(
        f"/api/v1/memories/{new['id']}/contradictions?tenant_id={tenant_id}",
        headers=headers,
    )
    assert resp_new.status_code == 200, resp_new.text
    data_new = resp_new.json()
    assert data_new["detection_status"] == "completed"
    supersedes_entries = [
        c for c in data_new["contradictions"] if c["direction"] == "supersedes"
    ]
    assert len(supersedes_entries) == 1
    assert supersedes_entries[0]["memory_id"] == old["id"]
    # The reason is inferred from the older row's status (outdated -> rdf_conflict).
    assert supersedes_entries[0]["reason"] == "rdf_conflict"
    # Back-compat: superseded_by still populated and points to old.
    assert data_new["superseded_by"] is not None
    assert data_new["superseded_by"]["id"] == old["id"]

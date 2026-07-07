"""Storage-layer tests for the procedural-memory domain (PM-01).

Exercises the procedures router end-to-end against the real app +
Postgres: create (with stats seed) → get → list → update-stats.
"""

from __future__ import annotations

import pytest


def _procedure_body(tenant_id: str, fleet_id: str, **overrides) -> dict:
    body = {
        "tenant_id": tenant_id,
        "fleet_id": fleet_id,
        "agent_id": "agent-pm01",
        "name": "Deploy to eu-west · fallback DNS at step 7",
        "pattern_signature": "deploy:eu-west:dns-fallback",
        "tools_sequence": ["bash:deploy", "bash:check-dns", "bash:retry"],
        "context_features": {"framework": "terraform", "region": "eu-west"},
    }
    body.update(overrides)
    return body


@pytest.mark.asyncio
async def test_create_get_procedure_round_trip(client, tenant_id, fleet_id):
    resp = await client.post("/api/v1/storage/procedures", json=_procedure_body(tenant_id, fleet_id))
    assert resp.status_code == 200, resp.text
    created = resp.json()
    pid = created["id"]
    assert created["name"].startswith("Deploy to eu-west")
    assert created["tools_sequence"] == ["bash:deploy", "bash:check-dns", "bash:retry"]
    # A stats row is always created with server defaults.
    assert created["stats"] is not None
    assert created["stats"]["reliability_score"] == 0.5
    assert created["stats"]["is_quarantined"] is False

    got = await client.get(f"/api/v1/storage/procedures/{pid}")
    assert got.status_code == 200, got.text
    assert got.json()["id"] == pid
    assert got.json()["stats"]["success_count"] == 0


@pytest.mark.asyncio
async def test_create_with_stats_seed(client, tenant_id, fleet_id):
    """Forge bridge (PM-04) seeds reliability from a trace outcome at mint."""
    body = _procedure_body(
        tenant_id,
        fleet_id,
        name="Seeded-from-success-trace",
        stats={"reliability_score": 0.9, "success_count": 3},
    )
    resp = await client.post("/api/v1/storage/procedures", json=body)
    assert resp.status_code == 200, resp.text
    stats = resp.json()["stats"]
    assert stats["reliability_score"] == 0.9
    assert stats["success_count"] == 3


@pytest.mark.asyncio
async def test_update_stats_and_quarantine(client, tenant_id, fleet_id):
    resp = await client.post("/api/v1/storage/procedures", json=_procedure_body(tenant_id, fleet_id))
    pid = resp.json()["id"]

    patch = {
        "success_count": 5,
        "failure_count": 1,
        "reliability_score": 0.83,
        "is_quarantined": True,
    }
    upd = await client.patch(f"/api/v1/storage/procedures/{pid}/stats", json=patch)
    assert upd.status_code == 200, upd.text
    body = upd.json()
    assert body["reliability_score"] == 0.83
    assert body["is_quarantined"] is True
    assert body["success_count"] == 5


@pytest.mark.asyncio
async def test_list_excludes_quarantined_by_default(client, tenant_id, fleet_id):
    # One healthy, one quarantined procedure in a dedicated fleet.
    fleet = f"{fleet_id}-listcheck"
    keep = await client.post(
        "/api/v1/storage/procedures",
        json=_procedure_body(tenant_id, fleet, name="healthy"),
    )
    drop = await client.post(
        "/api/v1/storage/procedures",
        json=_procedure_body(tenant_id, fleet, name="bad"),
    )
    await client.patch(
        f"/api/v1/storage/procedures/{drop.json()['id']}/stats",
        json={"is_quarantined": True},
    )

    listed = await client.get(
        "/api/v1/storage/procedures",
        params={"tenant_id": tenant_id, "fleet_id": fleet},
    )
    assert listed.status_code == 200, listed.text
    names = {p["name"] for p in listed.json()}
    assert "healthy" in names
    assert "bad" not in names

    with_q = await client.get(
        "/api/v1/storage/procedures",
        params={
            "tenant_id": tenant_id,
            "fleet_id": fleet,
            "include_quarantined": True,
        },
    )
    names_q = {p["name"] for p in with_q.json()}
    assert {"healthy", "bad"} <= names_q


@pytest.mark.asyncio
async def test_quarantine_toggle_then_delete(client, tenant_id, fleet_id):
    """Manual quarantine hides from the ranker path; unquarantine restores;
    delete removes the procedure and its stats row (BP-01)."""
    fleet = f"{fleet_id}-lifecycle"
    resp = await client.post(
        "/api/v1/storage/procedures",
        json=_procedure_body(tenant_id, fleet, name="toggle-me"),
    )
    pid = resp.json()["id"]

    async def _listed() -> set[str]:
        r = await client.get(
            "/api/v1/storage/procedures",
            params={"tenant_id": tenant_id, "fleet_id": fleet},
        )
        return {p["name"] for p in r.json()}

    # Quarantine → excluded from the default (ranker) list.
    await client.patch(
        f"/api/v1/storage/procedures/{pid}/stats",
        json={"is_quarantined": True},
    )
    assert "toggle-me" not in await _listed()

    # Unquarantine → back in the list.
    await client.patch(
        f"/api/v1/storage/procedures/{pid}/stats",
        json={"is_quarantined": False},
    )
    assert "toggle-me" in await _listed()

    # Delete → gone, and its stats row 404s.
    dele = await client.delete(f"/api/v1/storage/procedures/{pid}")
    assert dele.status_code == 200, dele.text
    assert dele.json()["deleted"] == pid
    assert (await client.get(f"/api/v1/storage/procedures/{pid}")).status_code == 404
    # Second delete is a 404 (idempotent-safe surface).
    assert (await client.delete(f"/api/v1/storage/procedures/{pid}")).status_code == 404


@pytest.mark.asyncio
async def test_invalidate_excludes_from_ranker_path(client, tenant_id, fleet_id):
    """status='invalidated' retires a procedure from the default list but it
    remains visible with include_quarantined=True (BP-01)."""
    fleet = f"{fleet_id}-invalidate"
    resp = await client.post(
        "/api/v1/storage/procedures",
        json=_procedure_body(tenant_id, fleet, name="retire-me"),
    )
    pid = resp.json()["id"]

    patched = await client.patch(f"/api/v1/storage/procedures/{pid}", json={"status": "invalidated"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["status"] == "invalidated"

    default = await client.get(
        "/api/v1/storage/procedures",
        params={"tenant_id": tenant_id, "fleet_id": fleet},
    )
    assert "retire-me" not in {p["name"] for p in default.json()}

    with_retired = await client.get(
        "/api/v1/storage/procedures",
        params={
            "tenant_id": tenant_id,
            "fleet_id": fleet,
            "include_quarantined": True,
        },
    )
    assert "retire-me" in {p["name"] for p in with_retired.json()}

    # Missing-id PATCH and unknown status handling.
    assert (await client.patch(f"/api/v1/storage/procedures/{pid}", json={})).status_code == 422

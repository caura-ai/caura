"""Cross-tenant embedding coverage — the operator view of the sweep's backlog.

Before this endpoint the count lived only inside the VPC: both storage services
are internal-ingress and no metric carried it, so "how many rows are still
unembedded" required an AlloyDB Auth Proxy session and a hand-written COUNT.

Two properties are worth pinning:

* the aggregate must AGREE with the per-tenant ``/embedding-coverage`` for the
  same tenant — they share a population predicate, and a divergence sends
  someone hunting a phantom bug;
* the route is cross-tenant, so the admin gate is load-bearing. The admin key
  resolves to ``tenant_id=None``; without the gate there is no tenant scope to
  fall back on and every tenant's row counts leak to any caller.
"""

import pytest
from fastapi import HTTPException

from core_api.auth import AuthContext
from core_api.clients.storage_client import get_storage_client
from core_api.routes.lifecycle import embedding_coverage_all_tenants
from tests.conftest import get_test_auth, uid as _uid

pytestmark = pytest.mark.asyncio


async def _write(client, tenant_id, headers, fleet_id, agent_id, content):
    resp = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "fleet_id": fleet_id,
            "memory_type": "fact",
            "visibility": "scope_team",
            "content": content,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_aggregate_agrees_with_per_tenant_endpoint(client, monkeypatch):
    tenant_id, headers = get_test_auth()
    tag = _uid()
    fleet_id, agent_id = f"cov-fleet-{tag}", f"cov-agent-{tag}"

    # Two embedded rows.
    await _write(client, tenant_id, headers, fleet_id, agent_id, f"embedded row {tag}")
    target = await _write(
        client, tenant_id, headers, fleet_id, agent_id, f"second row {tag}"
    )

    # Null one of them through the content-change path: on a failed re-embed
    # ``update_memory`` writes ``embedding=NULL`` so the sweep can find the row.
    # Using the update path rather than the write path on purpose — writes embed
    # in a background task, so a monkeypatch there races the assertion.
    async def _failed_embed(*args, **kwargs):
        return None

    monkeypatch.setattr("core_api.services.memory_service.get_embedding", _failed_embed)
    resp = await client.patch(
        f"/api/v1/memories/{target}?tenant_id={tenant_id}",
        json={"content": f"rewritten row {tag}"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    monkeypatch.undo()

    sc = get_storage_client()
    per_tenant = await sc.get_embedding_coverage(tenant_id, fleet_id)
    assert per_tenant["missing_embeddings"] == 1, per_tenant

    aggregate = await sc.get_embedding_coverage_all()
    row = next((r for r in aggregate["tenants"] if r["tenant_id"] == tenant_id), None)
    assert row is not None, aggregate

    # The aggregate is tenant-wide and the per-tenant call was fleet-scoped, so
    # compare the property that must hold across both: this fleet's missing row
    # is counted in the tenant's total, and the deployment total covers it.
    assert row["missing_embeddings"] >= per_tenant["missing_embeddings"]
    assert row["total_active"] >= per_tenant["total_active"]
    assert aggregate["missing_embeddings"] >= row["missing_embeddings"]
    assert aggregate["tenants_with_missing"] >= 1


async def test_aggregate_is_worst_first(client):
    """Ordering is the contract the per-tenant log cap relies on — it drops the
    tail, so the tail must be the tenants nobody would act on."""
    aggregate = await get_storage_client().get_embedding_coverage_all()
    missing = [r["missing_embeddings"] for r in aggregate["tenants"]]
    assert missing == sorted(missing, reverse=True), missing


async def test_admin_route_rejects_non_admin():
    """The gate, asserted directly: a tenant credential must not read the
    cross-tenant aggregate."""
    tenant_auth = AuthContext(tenant_id="some-tenant", is_admin=False)
    with pytest.raises(HTTPException) as exc:
        await embedding_coverage_all_tenants(auth=tenant_auth)
    assert exc.value.status_code == 403


async def test_admin_route_returns_totals(client):
    admin = AuthContext(tenant_id=None, is_admin=True)
    body = await embedding_coverage_all_tenants(auth=admin)
    assert set(body) >= {
        "tenants",
        "total_active",
        "missing_embeddings",
        "tenants_with_missing",
    }
    assert body["total_active"] >= 0
    assert body["missing_embeddings"] >= 0

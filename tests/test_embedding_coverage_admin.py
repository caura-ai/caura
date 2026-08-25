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
    tail, so the tail must be the tenants nobody would act on.

    Asserted on ``stale + missing``, which is what the query actually orders by
    (``memory_embedding_coverage_by_tenant``: ``.order_by((stale + missing).desc())``,
    documented there as "a stale row is actively wrong, a missing one is merely
    absent, so stale leads"). This previously asserted ``missing`` alone, which
    is a DIFFERENT invariant and only coincides while every tenant has zero
    stale rows: a tenant with (stale=3, missing=0) correctly sorts above one
    with (stale=0, missing=2), and the old assertion read that as [0, 2] and
    failed. It surfaced as an intermittent failure that depended on whether
    anything earlier in the session happened to leave a stale row behind.
    """
    aggregate = await get_storage_client().get_embedding_coverage_all()
    worst_first = [r["stale_embeddings"] + r["missing_embeddings"] for r in aggregate["tenants"]]
    assert worst_first == sorted(worst_first, reverse=True), aggregate["tenants"]


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


# ---------------------------------------------------------------------------
# Stale detection — a vector computed from text the row no longer holds
# ---------------------------------------------------------------------------


async def _coverage_for(tenant_id: str) -> dict:
    agg = await get_storage_client().get_embedding_coverage_all()
    return next((r for r in agg["tenants"] if r["tenant_id"] == tenant_id), {})


async def test_content_change_without_reembed_is_detected_as_stale(client):
    """The failure the NULL-sweep can never see.

    Rewriting ``content`` (+ ``content_hash``) while leaving ``embedding``
    untouched is exactly what ``update_memory`` used to do on a failed
    re-embed. The row stays non-NULL, so no sweep finds it; only comparing
    the vector's provenance against the current content reveals it.
    """
    tenant_id, headers = get_test_auth()
    tag = _uid()
    fleet_id, agent_id = f"stale-fleet-{tag}", f"stale-agent-{tag}"

    memory_id = await _write(
        client, tenant_id, headers, fleet_id, agent_id, f"original text {tag}"
    )

    before = await _coverage_for(tenant_id)
    stale_before = before.get("stale_embeddings", 0)

    # Move the content WITHOUT touching the embedding — the vector now
    # describes text the row no longer holds.
    sc = get_storage_client()
    updated = await sc.update_memory(
        memory_id,
        tenant_id,
        {
            "content": f"REPLACED text {tag}",
            "content_hash": f"hash-of-replaced-{tag}",
        },
    )
    assert updated is not None

    after = await _coverage_for(tenant_id)
    assert after["stale_embeddings"] == stale_before + 1, after
    # It is NOT missing — that is the whole point. A NULL-based sweep is blind
    # to this row.
    assert after["missing_embeddings"] == before.get("missing_embeddings", 0), after


async def test_normal_write_records_provenance_and_is_not_stale(client):
    """A row embedded at write time must not be reported as stale or unknown."""
    tenant_id, headers = get_test_auth()
    tag = _uid()
    fleet_id, agent_id = f"fresh-fleet-{tag}", f"fresh-agent-{tag}"

    before = await _coverage_for(tenant_id)
    await _write(client, tenant_id, headers, fleet_id, agent_id, f"fresh text {tag}")
    after = await _coverage_for(tenant_id)

    # The new row adds to the total but to neither defect bucket, and it does
    # NOT land in unknown_provenance — the insert stamped the hash.
    assert after["total_active"] == before.get("total_active", 0) + 1, after
    assert after["stale_embeddings"] == before.get("stale_embeddings", 0), after
    assert after["unknown_provenance"] == before.get("unknown_provenance", 0), after


async def test_null_content_hash_row_is_not_lost_from_every_bucket(client):
    """A row with known provenance but NULL ``content_hash`` must still land
    somewhere.

    ``content_hash`` is nullable. Comparing with ``!=`` compiles to ``<>``,
    which yields NULL against a NULL and is dropped by ``COUNT(*) FILTER`` — so
    such a row was counted in ``total_active`` and in NO defect bucket: not
    stale (NULL comparison), not unknown (its provenance IS known), not missing
    (it has a vector). Silently unaccounted for by the detector itself.
    """
    tenant_id, headers = get_test_auth()
    tag = _uid()
    fleet_id, agent_id = f"nullhash-fleet-{tag}", f"nullhash-agent-{tag}"

    memory_id = await _write(
        client, tenant_id, headers, fleet_id, agent_id, f"content {tag}"
    )

    # Clear content_hash while leaving the embedding and its provenance intact.
    sc = get_storage_client()
    assert (
        await sc.update_memory(memory_id, tenant_id, {"content_hash": None}) is not None
    )

    cov = await sc.get_embedding_coverage(tenant_id, fleet_id)
    buckets = (
        cov["missing_embeddings"] + cov["stale_embeddings"] + cov["unknown_provenance"]
    )
    assert cov["total_active"] == 1, cov
    # The row must be accounted for — it is stale (its vector describes content
    # whose hash no longer matches), not invisible.
    assert cov["stale_embeddings"] == 1, cov
    assert buckets == 1, cov


async def test_per_tenant_and_aggregate_report_the_same_fields(client):
    """Both coverage views must expose the same provenance keys.

    If the per-tenant view omits them, a caller comparing it against the
    aggregate reads absence as zero and concludes a flagged tenant is clean.
    """
    tenant_id, _ = get_test_auth()
    sc = get_storage_client()
    per_tenant = await sc.get_embedding_coverage(tenant_id)
    aggregate_row = await _coverage_for(tenant_id)

    provenance_keys = {"missing_embeddings", "stale_embeddings", "unknown_provenance"}
    assert provenance_keys <= set(per_tenant), per_tenant
    assert provenance_keys <= set(aggregate_row), aggregate_row
    # And they must AGREE for the same tenant, not merely both be present.
    for key in provenance_keys:
        assert per_tenant[key] == aggregate_row[key], (key, per_tenant, aggregate_row)


async def test_explicit_provenance_wins_over_derivation_on_update(client):
    """A caller-supplied ``embedded_content_hash`` must survive a patch that
    also carries ``content`` + ``content_hash``.

    The insert path documents "explicit caller value wins"; the update path must
    agree. Overriding the caller here would stamp the row's NEW hash onto a
    vector the caller told us came from OLD text — marking a genuinely stale
    write as freshly embedded, the exact inversion this column exists to catch.

    Latent today (core-worker sends provenance without ``content_hash``), so
    this test is what stops a future caller silently losing its stamp.
    """
    tenant_id, headers = get_test_auth()
    tag = _uid()
    fleet_id, agent_id = f"explicit-fleet-{tag}", f"explicit-agent-{tag}"

    memory_id = await _write(
        client, tenant_id, headers, fleet_id, agent_id, f"orig {tag}"
    )
    sc = get_storage_client()

    # All FOUR keys at once. ``embedding`` is required to reach the derivation
    # branch at all — without it the block never fires and the assertion below
    # would hold either way, which is no test.
    from common.constants import VECTOR_DIM

    assert (
        await sc.update_memory(
            memory_id,
            tenant_id,
            {
                "content": f"new text {tag}",
                "content_hash": f"hash-new-{tag}",
                "embedding": [0.25] * VECTOR_DIM,
                "embedded_content_hash": f"hash-OLD-{tag}",
            },
        )
        is not None
    )

    # The explicit stamp survived, so the row reads as stale — not silently
    # "refreshed" by the derivation.
    cov = await sc.get_embedding_coverage(tenant_id, fleet_id)
    assert cov["stale_embeddings"] == 1, cov

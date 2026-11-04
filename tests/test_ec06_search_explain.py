"""EC-06 — ``POST /api/v1/search/explain``: why a memory did not come back.

Two halves:

* End to end through the real app, pipeline and storage (in-process bridge):
  one test per removal reason the black box can produce, each asserting the
  explanation AND that ``POST /search`` with the same body really drops the
  memory — an explanation of a search that would have returned it is a lie.
* ``classify_explanation`` on hand-built pipeline contexts, for the reasons a
  black-box run cannot stage cheaply (entity-lookup short-circuit, derived
  exclusion, the cross-tenant collapse).

Storage's per-filter predicates have their own suite:
``core-storage-api/tests/test_scored_search_explain.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from core_api.clients.storage_client import get_storage_client
from core_api.config import settings
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.steps.search.explain_memory_filters import (
    EXPLAIN_RANK_WINDOW,
    ExplainMemoryFilters,
)
from core_api.pipeline.steps.search.retrieval_types import (
    RetrievalPlan,
    RetrievalStrategy,
)
from core_api.services.recall_explain import FILTER_REASONS, classify_explanation
from tests._legacy_contracts import LEGACY_API_KEY_FIELD
from tests.conftest import get_test_auth
from tests.conftest import uid as _uid

GATEWAY_SECRET = "test-gateway-secret"


def _tenant() -> str:
    return f"test-tenant-ec06-{_uid()}"


async def _write(
    client, tenant, headers, content, *, agent_id="writer-a", **fields
) -> str:
    body = {
        "tenant_id": tenant,
        "content": content,
        "agent_id": agent_id,
        "memory_type": "fact",
        "visibility": "scope_team",
        "write_mode": "strong",
        **fields,
    }
    resp = await client.post("/api/v1/memories", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _search_ids(client, headers, body) -> list[str]:
    resp = await client.post("/api/v1/search", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    return [m["id"] for m in resp.json()["items"]]


async def _explain(client, headers, body, memory_id, **extra) -> dict:
    resp = await client.post(
        "/api/v1/search/explain",
        json={**body, "memory_id": memory_id, **extra},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


async def test_returned(client):
    tenant, headers = get_test_auth(_tenant())
    q = f"kestrel nesting ledge {_uid()}"
    mid = await _write(client, tenant, headers, q)
    body = {"tenant_id": tenant, "query": q}
    assert mid in await _search_ids(client, headers, body)
    ex = await _explain(client, headers, body, mid)
    assert ex["returned"] is True
    assert ex["stage"] == "returned"
    assert ex["removed_by"] is None
    assert ex["rank"] == 1
    assert all(c["passed"] for c in ex["checks"])


async def test_explain_does_not_bump_recall_count(client):
    tenant, headers = get_test_auth(_tenant())
    q = f"osprey dive {_uid()}"
    mid = await _write(client, tenant, headers, q)
    await _explain(
        client,
        headers,
        {"tenant_id": tenant, "query": q, "caller_agent_id": "writer-a"},
        mid,
    )
    row = await get_storage_client().get_memory(mid, tenant)
    assert (row or {}).get("recall_count", 0) == 0


async def test_not_found(client):
    tenant, headers = get_test_auth(_tenant())
    ex = await _explain(
        client, headers, {"tenant_id": tenant, "query": "anything"}, str(uuid4())
    )
    assert ex["removed_by"] == "not_found"
    assert ex["checks"] == []


async def test_tenant_scope_named_for_the_admin_key(client):
    tenant, headers = get_test_auth(_tenant())
    q = f"heron reeds {_uid()}"
    mid = await _write(client, tenant, headers, q)
    other = _tenant()
    body = {"tenant_id": other, "query": q}
    assert mid not in await _search_ids(client, headers, body)
    ex = await _explain(client, headers, body, mid)
    assert ex["removed_by"] == "tenant_scope"
    assert ex["memory"]["tenant_id"] == tenant


async def test_deleted(client):
    tenant, headers = get_test_auth(_tenant())
    q = f"plover shore {_uid()}"
    mid = await _write(client, tenant, headers, q)
    resp = await client.delete(
        f"/api/v1/memories/{mid}?tenant_id={tenant}", headers=headers
    )
    assert resp.status_code in (200, 204), resp.text
    body = {"tenant_id": tenant, "query": q}
    assert mid not in await _search_ids(client, headers, body)
    ex = await _explain(client, headers, body, mid)
    assert ex["stage"] == "filter"
    assert ex["removed_by"] == "deleted"


async def test_agent_filter_auto_recall(client):
    """Auto-recall reads an agent's OWN writes; another agent wrote this one."""
    tenant, headers = get_test_auth(_tenant())
    q = f"wren hedge {_uid()}"
    mid = await _write(client, tenant, headers, q, agent_id="writer-a")
    body = {"tenant_id": tenant, "query": q, "filter_agent_id": "reader-b"}
    assert mid not in await _search_ids(client, headers, body)
    ex = await _explain(client, headers, body, mid)
    assert ex["removed_by"] == "agent_filter"
    assert ex["details"] == {
        "memory_agent_id": "writer-a",
        "filter_agent_id": "reader-b",
    }


async def test_visibility_scope_as_another_agent(client):
    tenant, headers = get_test_auth(_tenant())
    q = f"finch seed {_uid()}"
    mid = await _write(
        client, tenant, headers, q, agent_id="owner-a", visibility="scope_agent"
    )
    body = {"tenant_id": tenant, "query": q, "caller_agent_id": "peer-b"}
    assert mid not in await _search_ids(client, headers, body)
    ex = await _explain(
        client, headers, {"tenant_id": tenant, "query": q}, mid, as_agent_id="peer-b"
    )
    assert ex["removed_by"] == "visibility_scope"
    assert ex["searched_as_agent_id"] == "peer-b"
    assert ex["details"]["memory_visibility"] == "scope_agent"
    owner = await _explain(
        client, headers, {"tenant_id": tenant, "query": q}, mid, as_agent_id="owner-a"
    )
    assert owner["returned"] is True


async def test_fleet_scope(client):
    tenant, headers = get_test_auth(_tenant())
    q = f"swift chimney {_uid()}"
    mid = await _write(client, tenant, headers, q, fleet_id="fleet-a")
    body = {"tenant_id": tenant, "query": q, "fleet_ids": ["fleet-b"]}
    assert mid not in await _search_ids(client, headers, body)
    ex = await _explain(client, headers, body, mid)
    assert ex["removed_by"] == "fleet_scope"
    assert ex["details"]["memory_fleet_id"] == "fleet-a"


async def test_status_superseded(client):
    tenant, headers = get_test_auth(_tenant())
    q = f"lapwing field {_uid()}"
    mid = await _write(client, tenant, headers, q)
    await get_storage_client().update_memory(mid, tenant, {"status": "outdated"})
    body = {"tenant_id": tenant, "query": "zqxv unrelated lexeme"}
    assert mid not in await _search_ids(client, headers, body)
    ex = await _explain(client, headers, body, mid)
    assert ex["removed_by"] == "status_superseded"
    assert ex["details"]["memory_status"] == "outdated"


async def test_status_filter_and_memory_type_filter(client):
    tenant, headers = get_test_auth(_tenant())
    q = f"curlew marsh {_uid()}"
    mid = await _write(client, tenant, headers, q)
    for extra, name in (
        ({"status_filter": "archived"}, "status_filter"),
        ({"memory_type_filter": "decision"}, "memory_type_filter"),
    ):
        body = {"tenant_id": tenant, "query": q, **extra}
        assert mid not in await _search_ids(client, headers, body)
        ex = await _explain(client, headers, body, mid)
        assert ex["removed_by"] == name


async def test_temporal_valid_at(client):
    tenant, headers = get_test_auth(_tenant())
    q = f"godwit estuary {_uid()}"
    mid = await _write(
        client, tenant, headers, q, ts_valid_start="2030-01-01T00:00:00Z"
    )
    body = {"tenant_id": tenant, "query": q, "valid_at": "2026-06-01T00:00:00Z"}
    assert mid not in await _search_ids(client, headers, body)
    ex = await _explain(client, headers, body, mid)
    assert ex["removed_by"] == "temporal_valid_at"


async def test_below_min_similarity(client):
    tenant, headers = get_test_auth(_tenant())
    tag = _uid()
    mid = await _write(client, tenant, headers, f"dunlin flock mudflat {tag}")
    body = {
        "tenant_id": tenant,
        "query": f"dunlin migration route timing {tag}x",
        "min_similarity": 0.95,
    }
    assert mid not in await _search_ids(client, headers, body)
    ex = await _explain(client, headers, body, mid)
    assert ex["stage"] == "ranking"
    assert ex["removed_by"] == "below_min_similarity"
    assert ex["details"]["min_similarity"] == 0.95
    assert ex["details"]["vec_sim"] < 0.95


async def test_beyond_top_k_in_candidate_window(client):
    tenant, headers = get_test_auth(_tenant())
    # Fixed text, no random word: a random word moves the fake embedding, and
    # in about 1 run in 15 it took the second memory's similarity below the
    # 0.0 floor, so it was dropped for that instead. The tenant is already
    # unique. This text scores 0.22.
    q = "sanderling surf line"
    await _write(client, tenant, headers, q)
    mid = await _write(client, tenant, headers, "sanderling winter plumage notes")
    body = {"tenant_id": tenant, "query": q, "top_k": 1, "min_similarity": 0.0}
    assert mid not in await _search_ids(client, headers, body)
    ex = await _explain(client, headers, body, mid)
    assert ex["removed_by"] == "beyond_top_k"
    assert ex["rank"] == 2
    assert ex["top_k"] == 1


async def test_beyond_top_k_past_the_candidate_window(client):
    """Ranked past ``top_k * overfetch``: the widened probe finds the rank."""
    tenant, headers = get_test_auth(_tenant())
    # Fixed text, as in the test above: the target scores 0.41 and every note
    # at least 0.84, so it ranks fifth.
    q = "turnstone pebble beach tideline"
    for i in range(4):
        await _write(client, tenant, headers, f"{q} note{i}")
    mid = await _write(client, tenant, headers, "turnstone unrelated memo")
    body = {"tenant_id": tenant, "query": q, "top_k": 1, "min_similarity": 0.0}
    assert mid not in await _search_ids(client, headers, body)
    ex = await _explain(client, headers, body, mid)
    assert ex["removed_by"] == "beyond_top_k"
    assert ex["rank"] is not None and ex["rank"] > 2
    assert ex["details"]["rank_window"] == EXPLAIN_RANK_WINDOW


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


@pytest.fixture
def gateway(monkeypatch):
    monkeypatch.setattr(settings, "is_standalone", False)
    monkeypatch.setattr(settings, LEGACY_API_KEY_FIELD, "")
    monkeypatch.setattr(settings, "gateway_shared_secret", GATEWAY_SECRET)


def _gw(tenant: str, **extra: str) -> dict[str, str]:
    return {"X-Gateway-Secret": GATEWAY_SECRET, "X-Tenant-ID": tenant, **extra}


async def _agent_at_trust(client, tenant, agent_id, trust) -> None:
    _, admin = get_test_auth(tenant)
    await _write(
        client, tenant, admin, f"seed for {agent_id} {_uid()}", agent_id=agent_id
    )
    resp = await client.patch(
        f"/api/v1/agents/{agent_id}/trust?tenant_id={tenant}",
        json={"trust_level": trust},
        headers=admin,
    )
    assert resp.status_code == 200, resp.text


async def test_access_agent_trust_gate(client, gateway):
    tenant = _tenant()
    await _agent_at_trust(client, tenant, "low-agent", 2)
    await _agent_at_trust(client, tenant, "high-agent", 3)
    body = {"tenant_id": tenant, "query": "anything", "memory_id": str(uuid4())}
    low = await client.post(
        "/api/v1/search/explain",
        json=body,
        headers=_gw(tenant, **{"X-Agent-ID": "low-agent"}),
    )
    assert low.status_code == 403, low.text
    assert low.json()["error"]["code"] == "AGENT_TRUST_TOO_LOW"
    high = await client.post(
        "/api/v1/search/explain",
        json=body,
        headers=_gw(tenant, **{"X-Agent-ID": "high-agent"}),
    )
    assert high.status_code == 200, high.text


async def test_access_unregistered_agent_refused(client, gateway):
    tenant = _tenant()
    body = {"tenant_id": tenant, "query": "anything", "memory_id": str(uuid4())}
    resp = await client.post(
        "/api/v1/search/explain",
        json=body,
        headers=_gw(tenant, **{"X-Agent-ID": "ghost"}),
    )
    assert resp.status_code == 403, resp.text


async def test_access_org_member_refused_org_admin_allowed(client, gateway):
    tenant = _tenant()
    body = {"tenant_id": tenant, "query": "anything", "memory_id": str(uuid4())}
    member = await client.post(
        "/api/v1/search/explain",
        json=body,
        headers=_gw(tenant, **{"X-Org-Role": "member", "X-User-ID": "p1"}),
    )
    assert member.status_code == 403, member.text
    admin = await client.post(
        "/api/v1/search/explain",
        json=body,
        headers=_gw(tenant, **{"X-Org-Role": "admin", "X-User-ID": "p2"}),
    )
    assert admin.status_code == 200, admin.text
    # Not the admin key: another tenant's memory is "not found", never named.
    assert admin.json()["removed_by"] == "not_found"


async def test_access_cross_tenant_credential_refused(client, gateway):
    tenant = _tenant()
    headers = _gw(
        tenant,
        **{
            "X-Caura-Credential-Kind": "cross_tenant",
            "X-Readable-Tenant-IDs": f"{tenant},{tenant}-source",
            "X-Capabilities": "read,write",
        },
    )
    body = {"tenant_id": tenant, "query": "anything", "memory_id": str(uuid4())}
    resp = await client.post("/api/v1/search/explain", json=body, headers=headers)
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# classify_explanation on hand-built contexts
# ---------------------------------------------------------------------------


def _row(mid, vec_sim=0.9, metadata=None):
    return SimpleNamespace(
        Memory=SimpleNamespace(id=mid, metadata_=metadata),
        vec_sim=vec_sim,
        score=vec_sim,
        has_embedding=True,
        fts_match=False,
    )


def _ctx(
    mid, *, checks=None, raw_rows=(), filtered=(), results=(), plan=None, **extra
) -> dict:
    return {
        "tenant_id": "t1",
        "search_params": {"min_similarity": 0.3, "fts_weight": 0.0, "top_k": 2},
        "final_top_k": 2,
        "explain_filters": {
            "exists": True,
            "checks": checks
            if checks is not None
            else [{"filter": "tenant_scope", "passed": True}],
            "memory": {"tenant_id": "t1", "agent_id": "a"},
        },
        "raw_rows": list(raw_rows),
        "filtered_rows": list(filtered),
        "results": list(results),
        "retrieval_plan": plan
        or RetrievalPlan(strategy=RetrievalStrategy.SEMANTIC_SEARCH),
        **extra,
    }


@pytest.mark.parametrize("name", list(FILTER_REASONS))
def test_every_filter_reason_is_named(name):
    mid = uuid4()
    checks = [{"filter": "deleted", "passed": True}, {"filter": name, "passed": False}]
    out = classify_explanation(_ctx(mid, checks=checks), mid, reveal_cross_tenant=True)
    assert out["stage"] == "filter"
    assert out["removed_by"] == name
    assert out["reason"] == FILTER_REASONS[name]


def test_unknown_filter_is_reported_by_name_not_dropped():
    mid = uuid4()
    out = classify_explanation(
        _ctx(mid, checks=[{"filter": "future_filter", "passed": False}]),
        mid,
        reveal_cross_tenant=True,
    )
    assert out["removed_by"] == "future_filter"


def test_cross_tenant_collapses_to_not_found_without_admin():
    mid = uuid4()
    ctx = _ctx(mid, checks=[{"filter": "tenant_scope", "passed": False}])
    out = classify_explanation(ctx, mid, reveal_cross_tenant=False)
    assert out["removed_by"] == "not_found"
    assert out["memory"] is None and out["checks"] == []


def test_entity_lookup_short_circuit():
    mid = uuid4()
    eid = uuid4()
    plan = RetrievalPlan(
        strategy=RetrievalStrategy.ENTITY_LOOKUP,
        matched_entity_ids=[eid],
        skip_scored_search=True,
    )
    out = classify_explanation(_ctx(mid, plan=plan), mid, reveal_cross_tenant=True)
    assert out["removed_by"] == "entity_lookup_short_circuit"
    assert out["details"]["matched_entity_ids"] == [str(eid)]


def test_derived_excluded():
    mid = uuid4()
    row = _row(mid, metadata={"parent_memory_id": "p", "source": "atomic_fact_fanout"})
    out = classify_explanation(
        _ctx(mid, raw_rows=[row], include_derived=False), mid, reveal_cross_tenant=True
    )
    assert out["removed_by"] == "derived_excluded"
    assert out["rank"] == 1


def test_below_floor_wins_over_derived():
    """PostFilterResults floors before the derived exclusion; so does explain."""
    mid = uuid4()
    row = _row(
        mid,
        vec_sim=0.1,
        metadata={"parent_memory_id": "p", "source": "atomic_fact_fanout"},
    )
    out = classify_explanation(
        _ctx(mid, raw_rows=[row], include_derived=False), mid, reveal_cross_tenant=True
    )
    assert out["removed_by"] == "below_min_similarity"


def test_beyond_rank_window():
    mid = uuid4()
    out = classify_explanation(
        _ctx(mid, explain_rank=None, explain_rank_window=EXPLAIN_RANK_WINDOW),
        mid,
        reveal_cross_tenant=True,
    )
    assert out["removed_by"] == "beyond_top_k"
    assert out["rank"] is None
    assert str(EXPLAIN_RANK_WINDOW) in out["reason"]


# ---------------------------------------------------------------------------
# ExplainMemoryFilters step
# ---------------------------------------------------------------------------

_STEP_SC = "core_api.pipeline.steps.search.explain_memory_filters.get_storage_client"


@pytest.mark.unit
async def test_step_reuses_the_search_payload_and_probes_rank_only_when_needed():
    mid = uuid4()
    payload = {"tenant_id": "t1", "query": "q", "fleet_ids": ["f1"], "top_k": 4}
    sc = AsyncMock()
    sc.explain_scored_search_filters = AsyncMock(
        return_value={"exists": True, "checks": [{"filter": "x", "passed": True}]}
    )
    sc.scored_search = AsyncMock(return_value=[{"id": str(uuid4())}, {"id": str(mid)}])
    ctx = PipelineContext(
        data={
            "explain_memory_id": mid,
            "tenant_id": "t1",
            "search_params": {"top_k": 2},
            "scored_search_payload": payload,
            "raw_rows": [],
        }
    )
    with patch(_STEP_SC, return_value=sc):
        await ExplainMemoryFilters().execute(ctx)
    sent = sc.explain_scored_search_filters.await_args.args[0]
    assert sent == {**payload, "memory_id": str(mid)}
    assert sc.scored_search.await_args.args[0] == {
        **payload,
        "top_k": EXPLAIN_RANK_WINDOW,
    }
    assert ctx.data["explain_rank"] == 2

    # In the candidate window already: no probe.
    sc.scored_search.reset_mock()
    ctx.data["raw_rows"] = [_row(mid)]
    with patch(_STEP_SC, return_value=sc):
        await ExplainMemoryFilters().execute(ctx)
    sc.scored_search.assert_not_awaited()


@pytest.mark.unit
async def test_step_skipped_outside_explain():
    ctx = PipelineContext(data={"tenant_id": "t1"})
    result = await ExplainMemoryFilters().execute(ctx)
    assert result is not None and result.outcome.value == "skipped"

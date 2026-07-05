"""Integration tests for GET /api/v1/reports — the governed two-check read.

Real FastAPI app + in-process storage (see conftest). Validates:
- durable corpus filter (excludes the ``episode`` type and the ``main`` firehose),
- destination narrowing (owner_1to1 = self, internal_group = fleet, external = fail-closed),
- period validation.
"""

import pytest

from core_api.app import app
from core_api.auth import AuthContext, get_auth_context
from core_api.clients.storage_client import get_storage_client
from tests.conftest import get_test_auth, uid as _uid

pytestmark = pytest.mark.asyncio


async def _register(tenant_id, fleet, *agents):
    sc = get_storage_client()
    for a in agents:
        await sc.create_or_update_agent(
            {"tenant_id": tenant_id, "agent_id": a, "fleet_id": fleet, "trust_level": 1}
        )


async def _seed(client, headers, tenant_id, fleet, agent, mtype, n=1):
    for i in range(n):
        r = await client.post(
            "/api/v1/memories",
            json={
                "tenant_id": tenant_id,
                "agent_id": agent,
                "fleet_id": fleet,
                "memory_type": mtype,
                "visibility": "scope_team",
                "content": f"{mtype} by {agent} #{i} {_uid()}",
            },
            headers=headers,
        )
        assert r.status_code == 201, r.text


async def test_report_internal_group_durable_filter(client):
    tenant_id, headers = get_test_auth()
    tag = _uid()
    fleet, a1, a2 = f"rep-fleet-{tag}", f"rep-a1-{tag}", f"rep-a2-{tag}"
    await _register(tenant_id, fleet, a1, a2)
    await _seed(client, headers, tenant_id, fleet, a1, "decision", 2)
    await _seed(client, headers, tenant_id, fleet, a2, "fact", 1)
    await _seed(
        client, headers, tenant_id, fleet, a1, "episode", 1
    )  # excluded: episodic
    await _seed(
        client, headers, tenant_id, fleet, "main", "fact", 1
    )  # excluded: firehose

    resp = await client.get(
        "/api/v1/reports",
        params={
            "tenant_id": tenant_id,
            "period": "week",
            "destination": "internal_group",
            "agent_id": a1,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["summary"]["durable_memories_written"] == 3, body
    assert body["summary"]["active_agents"] == 2, body
    agents = {p["agent_id"]: p["durable_writes"] for p in body["per_agent"]}
    assert agents == {a1: 2, a2: 1}, agents
    assert "main" not in agents
    assert "episode" not in body["summary"]["by_type"], body["summary"]["by_type"]
    # weekly extras: value_highlights (top durable) + spotlight (top contributor)
    assert len(body["value_highlights"]) == 3, body["value_highlights"]
    assert all(
        "episode" != h["type"] and h["agent_id"] != "main"
        for h in body["value_highlights"]
    )
    assert body["spotlight"]["agent_id"] == a1, body["spotlight"]
    assert body["spotlight"]["durable_writes"] == 2
    assert body["spotlight"]["headline"] is not None
    # activity-over-time trend (14 daily buckets) + working-on lanes
    assert len(body["trend"]) == 14, body["trend"]
    assert sum(pt["count"] for pt in body["trend"]) == 3, body["trend"]
    assert set(body["working_on"]) == {"Governing", "Building", "Operating"}
    assert body["working_on"]["Governing"]["count"] == 2, body[
        "working_on"
    ]  # 2 decisions
    assert body["working_on"]["Building"]["count"] == 1, body["working_on"]  # 1 fact


async def test_report_owner_1to1_is_self(client):
    tenant_id, headers = get_test_auth()
    tag = _uid()
    fleet, a1, a2 = f"rep-fleet-{tag}", f"rep-a1-{tag}", f"rep-a2-{tag}"
    await _register(tenant_id, fleet, a1, a2)
    await _seed(client, headers, tenant_id, fleet, a1, "decision", 2)
    await _seed(client, headers, tenant_id, fleet, a2, "fact", 1)

    resp = await client.get(
        "/api/v1/reports",
        params={
            "tenant_id": tenant_id,
            "period": "week",
            "destination": "owner_1to1",
            "agent_id": a1,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["meta"]["scope"] == "self", body["meta"]
    assert body["summary"]["durable_memories_written"] == 2, body  # only a1's own
    # List-derived sections are author-scoped to the caller too (self path),
    # consistent with the breakdown counts — a2's team-visible fact must NOT leak.
    assert all(h["agent_id"] == a1 for h in body["value_highlights"]), body[
        "value_highlights"
    ]
    assert body["spotlight"] is None or body["spotlight"]["agent_id"] == a1, body[
        "spotlight"
    ]


async def test_report_external_is_fail_closed(client):
    tenant_id, headers = get_test_auth()
    tag = _uid()
    fleet, a1 = f"rep-fleet-{tag}", f"rep-a1-{tag}"
    await _register(tenant_id, fleet, a1)
    await _seed(client, headers, tenant_id, fleet, a1, "decision", 2)

    resp = await client.get(
        "/api/v1/reports",
        params={
            "tenant_id": tenant_id,
            "period": "week",
            "destination": "external",
            "agent_id": a1,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["meta"]["destination"] == "external"
    assert body["per_agent"] == [], body  # fail-closed: no per-agent detail
    assert body["learning"] == [], body


async def test_report_unknown_destination_and_invalid_period(client):
    tenant_id, headers = get_test_auth()
    a1 = f"rep-a1-{_uid()}"
    # Unknown destination → coerced to most-restrictive ``external`` (still 200).
    r1 = await client.get(
        "/api/v1/reports",
        params={
            "tenant_id": tenant_id,
            "period": "week",
            "destination": "bogus",
            "agent_id": a1,
        },
        headers=headers,
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["meta"]["destination"] == "external"
    # Invalid period → 422.
    r2 = await client.get(
        "/api/v1/reports",
        params={"tenant_id": tenant_id, "period": "month", "agent_id": a1},
        headers=headers,
    )
    assert r2.status_code == 422, r2.text


async def test_report_no_agent_caller_is_group_view(client):
    """Human/tenant caller (no agent_id) → tenant group view, NOT a 403.

    Regression guard for the auth fix: the trust gate only runs for agent
    callers; a tenant member/admin with no agent identity is authorized for the
    group view by enforce_tenant. Uses a dedicated tenant so the tenant-wide
    (no-fleet) group view is isolated from other tests.
    """
    tag = _uid()
    tenant_id, headers = get_test_auth(f"rep-tenant-{tag}")
    fleet, a1, a2 = f"rep-fleet-{tag}", f"rep-a1-{tag}", f"rep-a2-{tag}"
    await _register(tenant_id, fleet, a1, a2)
    await _seed(client, headers, tenant_id, fleet, a1, "decision", 2)
    await _seed(client, headers, tenant_id, fleet, a2, "fact", 1)
    await _seed(client, headers, tenant_id, fleet, "main", "fact", 2)  # excluded

    resp = await client.get(
        "/api/v1/reports",
        params={
            "tenant_id": tenant_id,
            "period": "week",
            "destination": "internal_group",
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text  # the fix: not 403
    body = resp.json()
    assert body["meta"]["scope"] == "group"
    agents = {p["agent_id"]: p["durable_writes"] for p in body["per_agent"]}
    assert agents == {a1: 2, a2: 1}, agents
    assert body["summary"]["durable_memories_written"] == 3, body


async def test_report_org_scope_aggregates_across_tenants(client):
    """scope=org with a cross-tenant read credential aggregates across the
    readable tenant set and returns a per-tenant breakdown."""
    tag = _uid()
    t1, t2 = f"rep-org1-{tag}", f"rep-org2-{tag}"
    _, headers = get_test_auth(
        t1
    )  # admin key — used only to SEED (before the override)
    await _register(t1, f"f1-{tag}", f"a1-{tag}")
    await _register(t2, f"f2-{tag}", f"a2-{tag}")
    await _seed(client, headers, t1, f"f1-{tag}", f"a1-{tag}", "decision", 2)
    await _seed(client, headers, t2, f"f2-{tag}", f"a2-{tag}", "fact", 3)

    ctx = AuthContext(tenant_id=t1, readable_tenant_ids=[t1, t2])  # cross-tenant reader
    app.dependency_overrides[get_auth_context] = lambda: ctx
    try:
        resp = await client.get(
            "/api/v1/reports",
            params={
                "tenant_id": t1,
                "period": "week",
                "destination": "internal_group",
                "scope": "org",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["meta"]["scope"] == "org"
        assert body["summary"]["durable_memories_written"] == 5, body["summary"]
        assert body["summary"]["by_tenant"] == {t1: 2, t2: 3}, body["summary"][
            "by_tenant"
        ]
    finally:
        app.dependency_overrides.pop(get_auth_context, None)


async def test_report_org_scope_admin_readable_param(client):
    """The internal admin credential may pass an explicit ``readable_tenant_ids``
    (the org-report proxy path). A non-admin caller's value is ignored — asserted
    by the sibling override tests, which never pass the param.
    """
    tag = _uid()
    t1, t2 = f"rep-adm1-{tag}", f"rep-adm2-{tag}"
    _, headers = get_test_auth(t1)  # admin key (is_admin=True)
    await _register(t1, f"f1-{tag}", f"a1-{tag}")
    await _register(t2, f"f2-{tag}", f"a2-{tag}")
    await _seed(client, headers, t1, f"f1-{tag}", f"a1-{tag}", "decision", 2)
    await _seed(client, headers, t2, f"f2-{tag}", f"a2-{tag}", "fact", 3)

    resp = await client.get(
        "/api/v1/reports",
        params={
            "tenant_id": t1,
            "period": "week",
            "destination": "internal_group",
            "scope": "org",
            "readable_tenant_ids": f"{t1},{t2}",
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["meta"]["scope"] == "org"
    assert body["summary"]["durable_memories_written"] == 5, body["summary"]
    assert body["summary"]["by_tenant"] == {t1: 2, t2: 3}, body["summary"]["by_tenant"]


async def _seed_titled(client, headers, tenant_id, fleet, agent, mtype, title):
    """Create a durable memory then set an exact title via the storage client.

    Enrichment runs inline in tests and would auto-title, so we PATCH the title
    directly to make the cohesive-filter assertions enrichment-independent.
    Returns the memory id.
    """
    r = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": agent,
            "fleet_id": fleet,
            "memory_type": mtype,
            "visibility": "scope_team",
            "content": f"{title} {_uid()}",
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    mid = r.json()["id"]
    await get_storage_client().update_memory(mid, tenant_id, {"title": title})
    return mid


async def test_report_excludes_noncohesive_titles(client):
    """Non-episode heartbeat / health / status memories are excluded.

    The durable filter (episode + ``main``) is not sufficient: monitoring pings
    are written as NON-episode rows (a ``decision``/``outcome``/``action`` titled
    "Heartbeat check…", "GPU health check…", "Gateway healthy…"). The cohesive
    title filter drops them so the per-agent leaderboard and highlights reflect
    real work, not pings.
    """
    tenant_id, headers = get_test_auth()
    tag = _uid()
    fleet, a1 = f"rep-fleet-{tag}", f"rep-a1-{tag}"
    await _register(tenant_id, fleet, a1)

    # Genuine durable work — kept. (Only decision/fact/semantic are writable
    # directly; outcome/rule/insight are server-reserved. The noise the filter
    # targets is in the TITLE, not the type, so non-episode types suffice.)
    await _seed_titled(
        client,
        headers,
        tenant_id,
        fleet,
        a1,
        "decision",
        "Chose Postgres over Mongo for the signal store",
    )
    await _seed_titled(
        client,
        headers,
        tenant_id,
        fleet,
        a1,
        "semantic",
        "Verify a project URL via the proxy before sharing it",
    )
    # Non-episode monitoring noise — excluded by the cohesive title filter.
    await _seed_titled(
        client,
        headers,
        tenant_id,
        fleet,
        a1,
        "decision",
        "Heartbeat check for GoodDollar L2 builder status",
    )
    await _seed_titled(
        client,
        headers,
        tenant_id,
        fleet,
        a1,
        "fact",
        "GPU health check recorded no_change",
    )
    await _seed_titled(
        client,
        headers,
        tenant_id,
        fleet,
        a1,
        "semantic",
        "Gateway healthy: zero auth errors",
    )

    resp = await client.get(
        "/api/v1/reports",
        params={
            "tenant_id": tenant_id,
            "period": "week",
            "destination": "internal_group",
            "agent_id": a1,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Only the 2 genuine memories survive the cohesive filter.
    assert body["summary"]["durable_memories_written"] == 2, body["summary"]
    assert {p["agent_id"]: p["durable_writes"] for p in body["per_agent"]} == {a1: 2}
    titles = " ".join(h["title"].lower() for h in body["value_highlights"])
    assert "heartbeat" not in titles and "health check" not in titles, body[
        "value_highlights"
    ]
    assert "gateway healthy" not in titles, body["value_highlights"]
    assert len(body["value_highlights"]) == 2, body["value_highlights"]
    # Trend counts share the cohesive corpus (noise excluded there too).
    assert sum(pt["count"] for pt in body["trend"]) == 2, body["trend"]


async def test_report_value_highlights_ranked_by_recall(client):
    """value_highlights is the true top-by-recall in the window (dedicated
    recall-sorted fetch), not merely the most-reused among the most-recent rows —
    it surfaces an older-but-heavily-reused memory above newer, unreused ones.
    """
    tenant_id, headers = get_test_auth()
    tag = _uid()
    fleet, a1 = f"rep-fleet-{tag}", f"rep-a1-{tag}"
    await _register(tenant_id, fleet, a1)
    sc = get_storage_client()

    older = await _seed_titled(
        client,
        headers,
        tenant_id,
        fleet,
        a1,
        "decision",
        "Older but heavily-reused architecture decision",
    )
    # Newer, unreused writes created AFTER the high-recall one.
    for i in range(3):
        await _seed_titled(
            client, headers, tenant_id, fleet, a1, "fact", f"Newer reference fact {i}"
        )
    # Bump the older memory's lifetime recall so it is the top by recall.
    for _ in range(3):
        assert await sc.increment_recall([older]) == 1

    resp = await client.get(
        "/api/v1/reports",
        params={
            "tenant_id": tenant_id,
            "period": "week",
            "destination": "internal_group",
            "agent_id": a1,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["value_highlights"], body
    top = body["value_highlights"][0]
    assert top["title"] == "Older but heavily-reused architecture decision", body[
        "value_highlights"
    ]
    assert top["recall_count"] == 3, top
    # Spotlight headline is the top contributor's highest-recall memory.
    assert body["spotlight"]["agent_id"] == a1
    assert (
        body["spotlight"]["headline"]["title"]
        == "Older but heavily-reused architecture decision"
    ), body["spotlight"]


async def test_report_quality_metrics(client):
    """Quality block: reuse-rate by type, never-recalled %, recall concentration,
    the write→durable→reused funnel, and insight-freshness structure.

    Seeds 4 durable memories (2 decisions, 2 facts) with one of each reused, plus
    one episode (full-corpus only), so every quality figure is deterministic.
    """
    tenant_id, headers = get_test_auth()
    tag = _uid()
    fleet, a1 = f"rep-fleet-{tag}", f"rep-a1-{tag}"
    await _register(tenant_id, fleet, a1)
    sc = get_storage_client()

    d1 = await _seed_titled(
        client, headers, tenant_id, fleet, a1, "decision", f"Decision one {tag}"
    )
    await _seed_titled(
        client, headers, tenant_id, fleet, a1, "decision", f"Decision two {tag}"
    )
    f1 = await _seed_titled(
        client, headers, tenant_id, fleet, a1, "fact", f"Fact one {tag}"
    )
    await _seed_titled(client, headers, tenant_id, fleet, a1, "fact", f"Fact two {tag}")
    # One episode — counts toward the funnel's "written" but not the durable corpus.
    await _seed(client, headers, tenant_id, fleet, a1, "episode", 1)
    # Reuse: d1 twice, f1 once → 2 of 4 durable memories ever reused; 3 total recalls.
    for _ in range(2):
        assert await sc.increment_recall([d1]) == 1
    assert await sc.increment_recall([f1]) == 1

    resp = await client.get(
        "/api/v1/reports",
        params={
            "tenant_id": tenant_id,
            "period": "week",
            "destination": "internal_group",
            "agent_id": a1,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    q = resp.json()["quality"]
    # Funnel: 5 written (4 durable + 1 episode), 4 durable, 2 ever-reused.
    assert q["funnel"] == {"written": 5, "durable": 4, "reused": 2}, q["funnel"]
    # Never-recalled: 2 of 4 durable → 50%.
    assert q["never_recalled_pct"] == 50.0, q
    # Reuse rate by type: decision 1/2, fact 1/2 → 50% each.
    rbt = {r["type"]: r["reuse_pct"] for r in q["reuse_by_type"]}
    assert rbt.get("decision") == 50.0 and rbt.get("fact") == 50.0, q["reuse_by_type"]
    # Concentration: total recalls 3 (=2+1); top-6 captures all → 100%.
    assert q["total_recalls"] == 3, q
    assert q["recall_concentration_pct"] == 100.0, q
    # Insight freshness present and well-formed (no insights seeded here).
    assert q["insight_freshness"]["total"] == 0, q["insight_freshness"]
    assert q["insight_freshness"]["stale_pct"] == 0.0, q["insight_freshness"]


async def test_report_org_scope_requires_cross_tenant_key(client):
    """scope=org without a cross-tenant read credential → 403 (home-only key can't widen)."""
    tag = _uid()
    t1 = f"rep-org-solo-{tag}"
    ctx = AuthContext(tenant_id=t1)  # single-tenant, non-admin
    app.dependency_overrides[get_auth_context] = lambda: ctx
    try:
        resp = await client.get(
            "/api/v1/reports",
            params={"tenant_id": t1, "period": "week", "scope": "org"},
        )
        assert resp.status_code == 403, resp.text
    finally:
        app.dependency_overrides.pop(get_auth_context, None)

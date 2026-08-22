"""The read contract the plugin and the MCP tools advertise, over REST.

Regression cover for H-10 (#820). ``plugin/src/tool-definitions.ts`` declares
``scope``/``written_by``/``weight_min``/``weight_max`` on ``caura_list`` and
``scope`` on ``caura_stats`` — mirroring ``plugin/tools.json``, which is
generated from the MCP tool specs — then dispatches both to REST routes that
declared none of them. FastAPI drops undeclared query params silently, so the
calls never failed; they answered a different question than the one asked.
``caura_list {agent_id:'me', written_by:'agent-b'}`` returned the caller's OWN
memories, labelled as agent-b's.

Two things are asserted here, and they pull in opposite directions:

1. the params now take effect (the tests that would pass trivially before the
   route accepted them are written so the WRONG answer is the pre-fix answer);
2. ``scope`` is trust-gated by the same ladder as the MCP tools, so making the
   param real did not turn a wrong-data bug into a cheaper way to reach rows
   the MCP surface gates behind trust ≥ 2.

Also covers two gaps this found in ``GET /memories/stats``, which never
received hardening its list sibling already had: no fleet-read gate at all, and
an ``agent_id`` that an agent credential could point at a peer.
"""

import contextlib

import pytest

from tests.conftest import get_test_auth, uid as _uid


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _write(client, tenant_id, headers, *, content, agent_id, fleet_id, weight=None):
    body = {
        "tenant_id": tenant_id,
        "content": content,
        "agent_id": agent_id,
        "fleet_id": fleet_id,
        "memory_type": "fact",
        # scope_org so the rows are visible to any caller in the tenant: these
        # tests are about the author/scope/weight FILTERS, not about the
        # visibility predicate, which has its own cover in test_visibility.py.
        "visibility": "scope_org",
    }
    if weight is not None:
        body["weight"] = weight
    resp = await client.post("/api/v1/memories", json=body, headers=headers)
    assert resp.status_code == 201, f"Write failed: {resp.text}"
    return resp.json()


@contextlib.contextmanager
def _as_agent(tenant_id: str, agent_id: str):
    """Run the enclosed requests as an AGENT credential (``auth.agent_id`` set).

    No auth path available to the OSS tests produces one: the admin-key branch
    returns ``AuthContext(tenant_id=None, is_admin=True)`` and the standalone
    branch (which wins here, before the gateway branch) returns
    ``AuthContext(tenant_id=..., org_role="admin")`` — both discard X-Agent-ID.
    ``test_api_keystones`` documents the same limitation.

    In production the plugin arrives on the gateway path, which sets tenant AND
    agent, and that is the shape the trust ladder gates on. Overriding the
    dependency reproduces it while leaving the route body — the code under
    test — fully exercised end to end, storage included.
    """
    from core_api.app import app
    from core_api.auth import AuthContext, get_auth_context

    app.dependency_overrides[get_auth_context] = lambda: AuthContext(
        tenant_id=tenant_id, agent_id=agent_id
    )
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_auth_context, None)


async def _set_trust(sc, tenant_id, agent_id, fleet_id, trust_level):
    """Pin an agent's trust level.

    Called AFTER the writes: ``get_or_create_agent`` on the write path
    registers an unknown agent at ``DEFAULT_TRUST_LEVEL``, so setting trust
    first and writing second would leave the row at whatever the write path
    decided rather than the level the test is exercising.
    """
    await sc.create_or_update_agent(
        {
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "fleet_id": fleet_id,
            "trust_level": trust_level,
        }
    )


def _contents(payload):
    return {item["content"] for item in payload["items"]}


# ---------------------------------------------------------------------------
# The advertised params now take effect
# ---------------------------------------------------------------------------


async def test_written_by_selects_the_author_not_the_caller(client, sc):
    """``written_by`` is the author filter, distinct from the caller identity.

    Pre-fix this returned ALICE's memory: ``written_by`` was dropped and
    ``agent_id`` doubled as the author filter. The answer looked plausible —
    a populated list — which is why it went unnoticed.
    """
    tenant_id, headers = get_test_auth()
    tag = _uid()
    alice, bob = f"alice-{tag}", f"bob-{tag}"
    fleet = f"wb-fleet-{tag}"

    await _write(client, tenant_id, headers, content=f"by alice [{tag}]", agent_id=alice, fleet_id=fleet)
    await _write(client, tenant_id, headers, content=f"by bob [{tag}]", agent_id=bob, fleet_id=fleet)
    await _set_trust(sc, tenant_id, alice, fleet, 2)

    resp = await client.get(
        f"/api/v1/memories?tenant_id={tenant_id}&fleet_id={fleet}&agent_id={alice}&written_by={bob}",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert _contents(resp.json()) == {f"by bob [{tag}]"}


async def test_weight_bounds_filter_the_result_set(client, sc):
    """``weight_min`` / ``weight_max`` narrow by weight.

    Pre-fix both were dropped and all three rows came back.
    """
    tenant_id, headers = get_test_auth()
    tag = _uid()
    agent = f"w-agent-{tag}"
    fleet = f"w-fleet-{tag}"

    for weight, label in ((0.1, "low"), (0.5, "mid"), (0.9, "high")):
        await _write(
            client, tenant_id, headers,
            content=f"{label} weight [{tag}]", agent_id=agent, fleet_id=fleet, weight=weight,
        )

    resp = await client.get(
        f"/api/v1/memories?tenant_id={tenant_id}&fleet_id={fleet}&weight_min=0.4&weight_max=0.6",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert _contents(resp.json()) == {f"mid weight [{tag}]"}


async def test_weight_bounds_outside_zero_to_one_are_rejected(client):
    """The documented range is 0-1; a value outside it is a 422, not a filter
    that silently matches everything."""
    tenant_id, headers = get_test_auth()
    resp = await client.get(f"/api/v1/memories?tenant_id={tenant_id}&weight_min=1.5", headers=headers)
    assert resp.status_code == 422, resp.text


async def test_an_inverted_weight_range_is_rejected(client):
    """min > max matches nothing. An empty page reads as "no such memories",
    which is the same silent-wrong-answer shape this whole file is about."""
    tenant_id, headers = get_test_auth()
    resp = await client.get(
        f"/api/v1/memories?tenant_id={tenant_id}&weight_min=0.9&weight_max=0.1", headers=headers
    )
    assert resp.status_code == 422, resp.text


async def test_scope_all_drops_the_author_filter(client, sc):
    """``scope='all'`` is tenant-wide: the caller's own agent_id stops acting
    as an author filter.

    Pre-fix ``scope`` was dropped, so this returned only alice's row — the
    exact failure the issue describes as "results stay author-filtered to 'me'
    instead of tenant-wide".
    """
    tenant_id, headers = get_test_auth()
    tag = _uid()
    alice, bob = f"alice-{tag}", f"bob-{tag}"
    fleet = f"sa-fleet-{tag}"

    await _write(client, tenant_id, headers, content=f"by alice [{tag}]", agent_id=alice, fleet_id=fleet)
    await _write(client, tenant_id, headers, content=f"by bob [{tag}]", agent_id=bob, fleet_id=fleet)
    await _set_trust(sc, tenant_id, alice, fleet, 2)  # scope='all' needs trust >= 2

    resp = await client.get(
        f"/api/v1/memories?tenant_id={tenant_id}&fleet_id={fleet}&agent_id={alice}&scope=all",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert _contents(resp.json()) == {f"by alice [{tag}]", f"by bob [{tag}]"}


async def test_scope_agent_pins_the_author_filter_to_the_caller(client, sc):
    """``scope='agent'`` means "my own memories" even when no ``agent_id``
    query param is supplied — the authenticated identity is the filter.

    Pre-fix, ``scope`` was dropped and no author filter was left, so this
    returned bob's row too: the caller asked to NARROW and was silently WIDENED.
    """
    tenant_id, headers = get_test_auth()
    tag = _uid()
    alice, bob = f"alice-{tag}", f"bob-{tag}"
    fleet = f"sag-fleet-{tag}"

    await _write(client, tenant_id, headers, content=f"by alice [{tag}]", agent_id=alice, fleet_id=fleet)
    await _write(client, tenant_id, headers, content=f"by bob [{tag}]", agent_id=bob, fleet_id=fleet)
    await _set_trust(sc, tenant_id, alice, fleet, 1)  # scope='agent' only needs trust >= 1

    with _as_agent(tenant_id, alice):
        resp = await client.get(f"/api/v1/memories?tenant_id={tenant_id}&fleet_id={fleet}&scope=agent")
    assert resp.status_code == 200, resp.text
    assert _contents(resp.json()) == {f"by alice [{tag}]"}


async def test_scope_fleet_without_a_fleet_id_pins_to_the_home_fleet(client, sc):
    """A constrained caller that asks for ``scope='fleet'`` without naming one
    is pinned to its OWN fleet rather than fanning out across every fleet's
    shared rows (``resolve_read_fleet_gate`` case (a)).

    Pre-fix ``scope`` was dropped, no fleet filter was applied, and the other
    fleet's row came back.
    """
    tenant_id, headers = get_test_auth()
    tag = _uid()
    alice, bob = f"alice-{tag}", f"bob-{tag}"
    home, other = f"home-{tag}", f"other-{tag}"

    await _write(client, tenant_id, headers, content=f"in home [{tag}]", agent_id=alice, fleet_id=home)
    await _write(client, tenant_id, headers, content=f"in other [{tag}]", agent_id=bob, fleet_id=other)
    await _set_trust(sc, tenant_id, alice, home, 1)

    with _as_agent(tenant_id, alice):
        resp = await client.get(f"/api/v1/memories?tenant_id={tenant_id}&scope=fleet")
    assert resp.status_code == 200, resp.text
    contents = _contents(resp.json())
    assert f"in home [{tag}]" in contents
    assert f"in other [{tag}]" not in contents


# ---------------------------------------------------------------------------
# scope is trust-gated — the same ladder the MCP tools use
# ---------------------------------------------------------------------------


async def test_scope_all_requires_trust_2(client, sc):
    """Making ``scope`` real must not make REST a cheaper tenant-wide read than
    MCP. Pre-fix this returned 200 (the param was ignored entirely)."""
    tenant_id, headers = get_test_auth()
    tag = _uid()
    alice = f"alice-{tag}"
    fleet = f"t1-fleet-{tag}"

    await _write(client, tenant_id, headers, content=f"row [{tag}]", agent_id=alice, fleet_id=fleet)
    await _set_trust(sc, tenant_id, alice, fleet, 1)

    with _as_agent(tenant_id, alice):
        resp = await client.get(f"/api/v1/memories?tenant_id={tenant_id}&scope=all")
    assert resp.status_code == 403, resp.text


async def test_scope_fleet_targeting_another_fleet_requires_trust_2(client, sc):
    """A pin, not a bug demonstration: ``enforce_fleet_read`` already refused
    this shape, so it passes with or without the scope work. It is here so a
    future refactor that routes cross-fleet reads through ``scope`` and drops
    the older gate still has to produce a 403."""
    tenant_id, headers = get_test_auth()
    tag = _uid()
    alice = f"alice-{tag}"
    home, other = f"home-{tag}", f"other-{tag}"

    await _write(client, tenant_id, headers, content=f"row [{tag}]", agent_id=alice, fleet_id=home)
    await _set_trust(sc, tenant_id, alice, home, 1)

    with _as_agent(tenant_id, alice):
        resp = await client.get(f"/api/v1/memories?tenant_id={tenant_id}&scope=fleet&fleet_id={other}")
    assert resp.status_code == 403, resp.text


async def test_scope_fleet_from_an_unregistered_caller_requires_trust_2(client):
    """The case the older gate does NOT cover.

    ``enforce_fleet_read`` returns early for an unknown agent ("registration
    happens on writes"), so an unregistered caller naming any fleet was allowed
    straight through. The scope ladder resolves it to L2 instead — it cannot
    prove membership in the fleet it named — and ``require_trust`` rejects it at
    effective trust 1. Pre-fix: 200.
    """
    tenant_id, _ = get_test_auth()
    tag = _uid()

    with _as_agent(tenant_id, f"ghost-{tag}"):
        resp = await client.get(
            f"/api/v1/memories?tenant_id={tenant_id}&scope=fleet&fleet_id=some-fleet-{tag}"
        )
    assert resp.status_code == 403, resp.text


async def test_scope_agent_still_cannot_name_another_fleet(client, sc):
    """``enforce_fleet_read`` is load-bearing even with the ladder in place.

    ``resolve_read_fleet_gate`` short-circuits ``scope='agent'`` to L1 *without
    inspecting the fleet it was handed* — deliberately, since scope='agent'
    pins the author filter to the caller. So the older gate is the only thing
    refusing ``scope=agent&fleet_id=<another fleet>`` here. Pinned as a test
    because the three agent lookups on this path look redundant from the
    outside, and dropping this one to save a round-trip would open a hole.
    """
    tenant_id, headers = get_test_auth()
    tag = _uid()
    alice = f"alice-{tag}"
    home, other = f"home-{tag}", f"other-{tag}"

    await _write(client, tenant_id, headers, content=f"row [{tag}]", agent_id=alice, fleet_id=home)
    await _set_trust(sc, tenant_id, alice, home, 1)

    with _as_agent(tenant_id, alice):
        resp = await client.get(f"/api/v1/memories?tenant_id={tenant_id}&scope=agent&fleet_id={other}")
    assert resp.status_code == 403, resp.text


async def test_scope_agent_rejects_a_foreign_written_by(client, sc):
    """Mirrors the MCP handler: ``written_by`` must be omitted or be yourself
    when scope='agent'. Rejecting beats silently overriding — the caller
    otherwise gets a result set that does not match what it asked for."""
    tenant_id, headers = get_test_auth()
    tag = _uid()
    alice, bob = f"alice-{tag}", f"bob-{tag}"
    fleet = f"fw-fleet-{tag}"

    await _write(client, tenant_id, headers, content=f"row [{tag}]", agent_id=alice, fleet_id=fleet)
    await _set_trust(sc, tenant_id, alice, fleet, 1)

    with _as_agent(tenant_id, alice):
        resp = await client.get(f"/api/v1/memories?tenant_id={tenant_id}&scope=agent&written_by={bob}")
    assert resp.status_code == 400, resp.text
    assert "written_by" in resp.text


async def test_scope_agent_without_an_agent_identity_is_rejected(client):
    """``scope='agent'`` with no resolvable caller must not silently degrade to
    "no author filter" — that would widen the very read it asks to narrow."""
    tenant_id, headers = get_test_auth()
    resp = await client.get(f"/api/v1/memories?tenant_id={tenant_id}&scope=agent", headers=headers)
    assert resp.status_code == 400, resp.text


async def test_invalid_scope_is_rejected(client):
    tenant_id, headers = get_test_auth()
    resp = await client.get(f"/api/v1/memories?tenant_id={tenant_id}&scope=everything", headers=headers)
    assert resp.status_code == 422, resp.text


async def test_both_read_surfaces_share_one_trust_ladder():
    """The MCP alias and the REST import must resolve to the SAME function.

    The ladder was duplicated-by-move once already; if someone reintroduces a
    private copy in ``mcp_server``, the two surfaces can drift apart silently
    and only one of them gets the next fix.
    """
    from core_api import mcp_server
    from core_api.services import agent_service

    assert mcp_server._resolve_read_fleet_gate is agent_service.resolve_read_fleet_gate


# ---------------------------------------------------------------------------
# GET /memories/stats — the hardening its list sibling already had
# ---------------------------------------------------------------------------


async def test_stats_cross_fleet_aggregate_is_gated(client, sc):
    """A fleet-scoped aggregate is a fleet-scoped read.

    ``GET /memories`` has called ``enforce_fleet_read`` all along; stats never
    did, so a trust-1 agent could read the breakdown of a fleet whose rows the
    list route would have refused to show it. Pre-fix: 200.
    """
    tenant_id, headers = get_test_auth()
    tag = _uid()
    alice = f"alice-{tag}"
    home, other = f"home-{tag}", f"other-{tag}"

    await _write(client, tenant_id, headers, content=f"row [{tag}]", agent_id=alice, fleet_id=home)
    await _set_trust(sc, tenant_id, alice, home, 1)

    with _as_agent(tenant_id, alice):
        resp = await client.get(f"/api/v1/memories/stats?tenant_id={tenant_id}&fleet_id={other}")
    assert resp.status_code == 403, resp.text


async def test_stats_rejects_a_peer_agent_id(client, sc):
    """``agent_id`` on stats is the visibility identity as well as the author
    filter, so an agent credential naming a PEER learned that peer's private
    per-type/status counts. Pre-fix: 200 with bob's numbers.
    """
    tenant_id, headers = get_test_auth()
    tag = _uid()
    alice, bob = f"alice-{tag}", f"bob-{tag}"
    fleet = f"peer-fleet-{tag}"

    await _write(client, tenant_id, headers, content=f"row [{tag}]", agent_id=alice, fleet_id=fleet)
    await _set_trust(sc, tenant_id, alice, fleet, 1)

    with _as_agent(tenant_id, alice):
        resp = await client.get(f"/api/v1/memories/stats?tenant_id={tenant_id}&agent_id={bob}")
    assert resp.status_code == 403, resp.text


async def test_stats_allows_naming_yourself(client, sc):
    """The rejection above must not break the legitimate call — an agent asking
    for its own breakdown, which is what the plugin sends on every caura_stats."""
    tenant_id, headers = get_test_auth()
    tag = _uid()
    alice = f"alice-{tag}"
    fleet = f"self-fleet-{tag}"

    await _write(client, tenant_id, headers, content=f"row [{tag}]", agent_id=alice, fleet_id=fleet)
    await _set_trust(sc, tenant_id, alice, fleet, 1)

    with _as_agent(tenant_id, alice):
        resp = await client.get(f"/api/v1/memories/stats?tenant_id={tenant_id}&agent_id={alice}")
    assert resp.status_code == 200, resp.text


async def test_stats_scope_all_requires_trust_2(client, sc):
    tenant_id, headers = get_test_auth()
    tag = _uid()
    alice = f"alice-{tag}"
    fleet = f"ss-fleet-{tag}"

    await _write(client, tenant_id, headers, content=f"row [{tag}]", agent_id=alice, fleet_id=fleet)
    await _set_trust(sc, tenant_id, alice, fleet, 1)

    with _as_agent(tenant_id, alice):
        resp = await client.get(f"/api/v1/memories/stats?tenant_id={tenant_id}&scope=all")
    assert resp.status_code == 403, resp.text


async def test_stats_without_a_scope_is_unchanged(client):
    """The legacy shape — no ``scope``, no agent identity — must keep working
    exactly as before; the dashboard calls it that way."""
    tenant_id, headers = get_test_auth()
    resp = await client.get(f"/api/v1/memories/stats?tenant_id={tenant_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    assert "total" in resp.json()


# ---------------------------------------------------------------------------
# The drift guard: what the plugin advertises must be what the route accepts
# ---------------------------------------------------------------------------

# The plugin dispatches each tool to one core-api endpoint
# (``ENDPOINT_DISPATCH`` in plugin/src/tool-definitions.ts). Tools that send
# their params as QUERY args are the ones exposed to this failure mode: FastAPI
# ignores an undeclared query param instead of rejecting it. Tools that send a
# JSON BODY are validated by a Pydantic model and are not silently dropped the
# same way, so they are classified rather than checked here.
_QUERY_PARAM_TOOLS = {
    # caura_keystones dispatches to the /api/v1/memclaw legacy alias, which is
    # mounted with include_in_schema=False; the canonical path below is the
    # same router and therefore the same signature.
    "caura_keystones": "/api/v1/keystones",
    "caura_list": "/api/v1/memories",
    "caura_stats": "/api/v1/memories/stats",
    "caura_entity_get": "/api/v1/entities/{entity_id}",
}

_BODY_DISPATCH_TOOLS = {
    "caura_doc",      # POST /documents*, GET /documents/collections
    "caura_evolve",   # POST /evolve/report
    "caura_insights",  # POST /insights/generate
    "caura_manage",   # op-dispatched across several endpoints
    "caura_recall",   # POST /search (+ /recall)
    "caura_tune",     # PATCH /agents/{id}/tune
    "caura_write",    # POST /memories, POST /memories/bulk
}

# Advertised params that are deliberately NOT query params on the target route,
# each with the reason. Kept explicit — and checked below for staleness — so it
# cannot quietly become a place to hide a real gap (same shape as the purge
# coverage guard in the storage suite).
_NOT_QUERY_PARAMS = {
    ("caura_entity_get", "entity_id"): "path segment, not a query param",
}


def _accepted_query_params(spec: dict, path: str) -> set[str]:
    """Query-param names a GET route accepts.

    Read from ``app.openapi()`` rather than by walking ``app.routes``: FastAPI
    0.137 mounts ``include_router(prefix=...)`` as an opaque ``_IncludedRouter``
    with no public ``.routes``, so the prefixed paths are not reachable from the
    route table at all. The same reasoning is spelled out at the
    ``_TIMEOUT_OPT_OUT_PATHS`` guard in ``core_api/app.py``.
    """
    node = spec["paths"].get(path, {}).get("get")
    assert node is not None, f"No GET route registered at {path}"
    return {p["name"] for p in node.get("parameters", []) if p.get("in") == "query"}


def _plugin_tool_specs():
    import json
    from pathlib import Path

    specs_path = Path(__file__).resolve().parents[1] / "plugin" / "tools.json"
    return {t["name"]: t for t in json.loads(specs_path.read_text())}


@pytest.mark.unit
def test_every_plugin_advertised_query_param_is_accepted_by_its_route():
    """A param the plugin advertises must be one the route actually reads.

    This is the guard the H-10 class needed: FastAPI drops undeclared query
    params silently, so the only symptom was a plausible wrong answer. Adding a
    param to a tool schema without adding it to the route now fails here.
    """
    from core_api.app import app

    spec = app.openapi()
    specs = _plugin_tool_specs()
    gaps = {}
    for tool, path in _QUERY_PARAM_TOOLS.items():
        accepted = _accepted_query_params(spec, path)
        advertised = {p["name"] for p in specs[tool]["params"]}
        excused = {name for (t, name) in _NOT_QUERY_PARAMS if t == tool}
        missing = advertised - accepted - excused
        if missing:
            gaps[tool] = sorted(missing)
    assert not gaps, f"advertised by the plugin but dropped by the route: {gaps}"


@pytest.mark.unit
def test_the_not_query_params_excuse_list_cannot_hide_a_real_gap():
    """An entry that names a param the route DOES accept is stale, and would
    mask the next real gap on that tool."""
    from core_api.app import app

    spec = app.openapi()
    for (tool, name), reason in _NOT_QUERY_PARAMS.items():
        assert reason, f"{tool}.{name} needs a reason"
        accepted = _accepted_query_params(spec, _QUERY_PARAM_TOOLS[tool])
        assert name not in accepted, f"stale excuse: {tool}.{name} IS accepted by the route"


@pytest.mark.unit
def test_every_plugin_exposed_tool_is_classified():
    """A new plugin tool must be sorted into query-dispatch or body-dispatch, so
    it cannot escape the check above just by being new."""
    specs = _plugin_tool_specs()
    exposed = {name for name, spec in specs.items() if spec["plugin_exposed"]}
    classified = set(_QUERY_PARAM_TOOLS) | _BODY_DISPATCH_TOOLS
    assert exposed - classified == set(), f"unclassified plugin tools: {sorted(exposed - classified)}"
    assert classified - exposed == set(), f"classified but no longer exposed: {sorted(classified - exposed)}"

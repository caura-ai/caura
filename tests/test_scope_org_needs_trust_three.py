"""A ``scope_org`` memory is written, or edited, only at trust level 3.

Every fleet of the tenant reads a ``scope_org`` row, in recall and in briefs, so
writing one is a write into every fleet. The trust ladder keeps writes into
another fleet for level 3, and the visibility tier is chosen by the writer, so
it is held to the same bar on every surface that takes one: ``POST /memories``,
``POST /memories/bulk``, ``caura_write`` (one memory or a batch), the STM
promote, and the edits (``PATCH /memories/{id}``, ``caura_manage`` op=update).
An edit that keeps a row ``scope_org`` needs it too; one that narrows the row
away from ``scope_org`` does not. Insights and evolve save what they find at
``scope='all'`` as ``scope_org`` rows, so that scope needs level 3 as well, over
REST and MCP; ``agent`` and ``fleet`` keep their levels (1 and 2).

A tenant credential writes as the agent it names, so that agent's level is the
one ranked, as for a cross-fleet write. On the edit path a credential with no
agent identity keeps the authority it holds, as for every other edit gate. The
admin key is not gated, as it is not for a cross-fleet write.
"""

from __future__ import annotations

import contextlib
import re
from unittest.mock import AsyncMock

import pytest

from core_api import mcp_server
from core_api.errors import AUTH_AGENT_TRUST_TOO_LOW, AUTH_FLEET_SCOPE_FORBIDDEN
from core_api.services import agent_service
from core_api.tools._registry import get_spec
from tests.conftest import new_tenant_id, parse_envelope, uid
from tests.test_memory_edit_status_gates import _row, _via_mcp, _via_rest

_FLEET = "fleet-alpha"  # the fleet ``_row`` puts every edited row in


@contextlib.contextmanager
def _as_tenant_key(tenant_id: str):
    """A tenant credential: no agent of its own, so the request names one.

    The suite's default headers carry the admin key, which no trust gate ranks.
    """
    from core_api.app import app
    from core_api.auth import AuthContext, get_auth_context

    app.dependency_overrides[get_auth_context] = lambda: AuthContext(
        tenant_id=tenant_id, readable_tenant_ids=[tenant_id]
    )
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_auth_context, None)


async def _register(
    sc, tenant_id: str, agent_id: str, fleet_id: str, trust: int
) -> None:
    await sc.create_or_update_agent(
        {
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "fleet_id": fleet_id,
            "trust_level": trust,
        }
    )


def _memory(tenant_id: str, agent_id: str, fleet_id: str, visibility: str) -> dict:
    return {
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "fleet_id": fleet_id,
        "content": f"Quarterly planning moves to the first Monday [{uid()}]",
        "memory_type": "fact",
        "visibility": visibility,
    }


# --- REST create ----------------------------------------------------------------


@pytest.mark.parametrize("trust", [1, 2, 3])
async def test_a_rest_write_at_scope_org_needs_trust_3(client, sc, trust):
    tenant_id = new_tenant_id()
    agent, fleet = f"writer-{uid()}", f"fleet-{uid()}"
    await _register(sc, tenant_id, agent, fleet, trust)

    with _as_tenant_key(tenant_id):
        resp = await client.post(
            "/api/v1/memories", json=_memory(tenant_id, agent, fleet, "scope_org")
        )

    if trust < 3:
        assert resp.status_code == 403, resp.text
        assert resp.json()["error"]["code"] == AUTH_FLEET_SCOPE_FORBIDDEN
    else:
        assert resp.status_code == 201, resp.text
        assert resp.json()["visibility"] == "scope_org"


@pytest.mark.parametrize("visibility", ["scope_team", "scope_agent"])
async def test_a_rest_write_below_scope_org_is_unchanged(client, sc, visibility):
    tenant_id = new_tenant_id()
    agent, fleet = f"writer-{uid()}", f"fleet-{uid()}"
    await _register(sc, tenant_id, agent, fleet, 1)

    with _as_tenant_key(tenant_id):
        resp = await client.post(
            "/api/v1/memories", json=_memory(tenant_id, agent, fleet, visibility)
        )

    assert resp.status_code == 201, resp.text


@pytest.mark.parametrize("trust", [1, 3])
async def test_a_rest_bulk_write_at_scope_org_needs_trust_3(client, sc, trust):
    tenant_id = new_tenant_id()
    agent, fleet = f"writer-{uid()}", f"fleet-{uid()}"
    await _register(sc, tenant_id, agent, fleet, trust)

    with _as_tenant_key(tenant_id):
        resp = await client.post(
            "/api/v1/memories/bulk",
            json={
                "tenant_id": tenant_id,
                "agent_id": agent,
                "fleet_id": fleet,
                "visibility": "scope_org",
                "items": [{"content": f"Release freeze starts on the 20th [{uid()}]"}],
            },
            headers={"X-Bulk-Attempt-Id": f"attempt-{uid()}"},
        )

    if trust < 3:
        assert resp.status_code == 403, resp.text
        assert resp.json()["error"]["code"] == AUTH_FLEET_SCOPE_FORBIDDEN
    else:
        assert resp.status_code in (200, 207), resp.text
        assert resp.json()["created"] == 1


@pytest.mark.parametrize("trust", [1, 3])
async def test_an_stm_promote_at_scope_org_needs_trust_3(
    client, sc, monkeypatch, trust
):
    from core_api.routes import stm as stm_route
    from core_api.services import stm_service

    monkeypatch.setattr(stm_route.settings, "use_stm", True)
    promote = AsyncMock(return_value={"id": "promoted"})
    monkeypatch.setattr(stm_service, "promote", promote)
    tenant_id = new_tenant_id()
    agent, fleet = f"writer-{uid()}", f"fleet-{uid()}"
    await _register(sc, tenant_id, agent, fleet, trust)

    with _as_tenant_key(tenant_id):
        resp = await client.post(
            f"/api/v1/stm/promote?tenant_id={tenant_id}",
            json={
                "agent_id": agent,
                "fleet_id": fleet,
                "content": "The vendor contract renews in March",
                "visibility": "scope_org",
            },
        )

    if trust < 3:
        assert resp.status_code == 403, resp.text
        promote.assert_not_awaited()
    else:
        assert resp.status_code == 200, resp.text
        promote.assert_awaited_once()


# --- MCP caura_write -----------------------------------------------------------


class _Created:
    def model_dump(self, mode: str = "python") -> dict:
        return {"id": "m-1", "status": "created"}


def _write_agent_at(monkeypatch, trust: int) -> None:
    """``caura_write`` resolves its writer through ``resolve_write_agent``."""

    async def _resolve(chosen_agent_id, tenant_id, fleet_id, **_kw):
        return (
            {
                "agent_id": chosen_agent_id,
                "tenant_id": tenant_id,
                "fleet_id": fleet_id,
                "trust_level": trust,
            },
            chosen_agent_id,
        )

    monkeypatch.setattr(mcp_server, "resolve_write_agent", _resolve)


@pytest.mark.parametrize("trust", [1, 2, 3])
async def test_caura_write_at_scope_org_needs_trust_3(mcp_env, monkeypatch, trust):
    _write_agent_at(monkeypatch, trust)
    create = mcp_env["service"]("create_memory")
    create.return_value = _Created()

    out = await mcp_server.caura_write(
        content="Office moves in May", agent_id="writer", visibility="scope_org"
    )

    if trust < 3:
        assert parse_envelope(out)["error"]["code"] == AUTH_FLEET_SCOPE_FORBIDDEN
        create.assert_not_awaited()
    else:
        create.assert_awaited_once()


@pytest.mark.parametrize("trust", [1, 3])
async def test_a_caura_write_batch_at_scope_org_needs_trust_3(
    mcp_env, monkeypatch, trust
):
    _write_agent_at(monkeypatch, trust)
    bulk = mcp_env["service"]("create_memories_bulk")
    bulk.return_value = _Created()
    monkeypatch.setattr(mcp_server, "bulk_check_and_increment", AsyncMock())

    out = await mcp_server.caura_write(
        items=[{"content": "Office moves in May"}],
        agent_id="writer",
        visibility="scope_org",
    )

    if trust < 3:
        assert parse_envelope(out)["error"]["code"] == AUTH_FLEET_SCOPE_FORBIDDEN
        bulk.assert_not_awaited()
    else:
        bulk.assert_awaited_once()


# --- insights and evolve -------------------------------------------------------
#
# Both run for real against an empty tenant: insights finds nothing to analyse
# and returns, and a ``success`` outcome with no related ids saves its outcome
# memory without calling a model.


def _run_body(tool: str, tenant_id: str, agent: str, fleet: str, scope: str) -> dict:
    body = {"tenant_id": tenant_id, "agent_id": agent, "scope": scope}
    if scope == "fleet":
        body["fleet_id"] = fleet
    if tool == "insights":
        return {**body, "focus": "stale"}
    return {
        **body,
        "outcome": "The rollout went out cleanly",
        "outcome_type": "success",
    }


_RUN_PATH = {"insights": "/api/v1/insights/generate", "evolve": "/api/v1/evolve/report"}


@pytest.mark.parametrize("tool", ["insights", "evolve"])
@pytest.mark.parametrize("trust", [2, 3])
async def test_a_rest_run_at_scope_all_needs_trust_3(client, sc, tool, trust):
    tenant_id = new_tenant_id()
    agent, fleet = f"analyst-{uid()}", f"fleet-{uid()}"
    await _register(sc, tenant_id, agent, fleet, trust)

    with _as_tenant_key(tenant_id):
        resp = await client.post(
            _RUN_PATH[tool], json=_run_body(tool, tenant_id, agent, fleet, "all")
        )

    if trust < 3:
        assert resp.status_code == 403, resp.text
        assert resp.json()["error"]["code"] == AUTH_AGENT_TRUST_TOO_LOW
        assert "required 3" in resp.json()["error"]["message"]
    else:
        assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("tool", ["insights", "evolve"])
@pytest.mark.parametrize(
    ("scope", "trust", "allowed"),
    [("agent", 0, False), ("agent", 1, True), ("fleet", 1, False), ("fleet", 2, True)],
)
async def test_a_rest_run_below_scope_all_keeps_its_level(
    client, sc, tool, scope, trust, allowed
):
    tenant_id = new_tenant_id()
    agent, fleet = f"analyst-{uid()}", f"fleet-{uid()}"
    await _register(sc, tenant_id, agent, fleet, trust)

    with _as_tenant_key(tenant_id):
        resp = await client.post(
            _RUN_PATH[tool], json=_run_body(tool, tenant_id, agent, fleet, scope)
        )

    assert resp.status_code == (200 if allowed else 403), resp.text


@pytest.fixture
def mcp_tenant(monkeypatch):
    """The MCP request context of a tenant credential, over real storage."""
    tenant_id = new_tenant_id()
    monkeypatch.setattr(mcp_server, "_check_auth", lambda: None)
    monkeypatch.setattr(mcp_server, "_check_write_scope", lambda: None)
    monkeypatch.setattr(mcp_server, "_get_tenant", lambda: tenant_id)
    monkeypatch.setattr(mcp_server, "_get_agent_id", lambda: None)
    monkeypatch.setattr(mcp_server, "_is_install_credential", lambda: False)
    monkeypatch.setattr(mcp_server, "_get_install_uuid", lambda: None)
    return tenant_id


async def _run_tool(tool: str, agent: str, fleet: str, scope: str) -> str:
    fleet_id = fleet if scope == "fleet" else None
    if tool == "insights":
        return await mcp_server.caura_insights(
            focus="stale", scope=scope, fleet_id=fleet_id, agent_id=agent
        )
    return await mcp_server.caura_evolve(
        outcome="The rollout went out cleanly",
        outcome_type="success",
        scope=scope,
        fleet_id=fleet_id,
        agent_id=agent,
    )


@pytest.mark.parametrize("tool", ["caura_insights", "caura_evolve"])
@pytest.mark.parametrize("trust", [2, 3])
async def test_an_mcp_run_at_scope_all_needs_trust_3(mcp_tenant, sc, tool, trust):
    agent, fleet = f"analyst-{uid()}", f"fleet-{uid()}"
    await _register(sc, mcp_tenant, agent, fleet, trust)

    out = parse_envelope(
        await _run_tool(tool.removeprefix("caura_"), agent, fleet, "all")
    )

    if trust < 3:
        assert out.get("error", {}).get("code") == "FORBIDDEN", out
        assert "required 3" in out["error"]["message"]
    else:
        assert "error" not in out, out


@pytest.mark.parametrize("tool", ["caura_insights", "caura_evolve"])
@pytest.mark.parametrize(
    ("scope", "trust", "allowed"),
    [("agent", 0, False), ("agent", 1, True), ("fleet", 1, False), ("fleet", 2, True)],
)
async def test_an_mcp_run_below_scope_all_keeps_its_level(
    mcp_tenant, sc, tool, scope, trust, allowed
):
    agent, fleet = f"analyst-{uid()}", f"fleet-{uid()}"
    await _register(sc, mcp_tenant, agent, fleet, trust)

    out = parse_envelope(
        await _run_tool(tool.removeprefix("caura_"), agent, fleet, scope)
    )

    assert ("error" not in out) is allowed, out


# --- edits, REST and MCP -------------------------------------------------------

_TRUST = {"alice": 1, "dana": 2, "carol": 3}


@pytest.fixture
def edit(request, monkeypatch, mcp_env):
    async def _lookup(tenant_id, agent_id, *, read=True):
        if agent_id not in _TRUST:
            return None
        return {
            "agent_id": agent_id,
            "fleet_id": _FLEET,
            "trust_level": _TRUST[agent_id],
        }

    monkeypatch.setattr(agent_service, "lookup_agent", _lookup)
    surface = {"rest": _via_rest, "mcp": _via_mcp}[request.param]

    async def _call(caller, row, fields):
        return await surface(monkeypatch, caller, row, fields)

    return _call


def _scope_org_row(owner: str) -> dict:
    row = _row(owner)
    row["visibility"] = "scope_org"
    return row


# ``caura_manage`` op=update takes no ``visibility``, so only REST can widen.
@pytest.mark.parametrize("edit", ["rest"], indirect=True)
@pytest.mark.parametrize("caller", ["alice", "dana"])
async def test_an_edit_cannot_widen_a_row_to_scope_org_below_trust_3(edit, caller):
    outcome = await edit(caller, _row(caller), {"visibility": "scope_org"})

    assert outcome.code == AUTH_FLEET_SCOPE_FORBIDDEN
    outcome.stored.assert_not_awaited()


@pytest.mark.parametrize("edit", ["rest"], indirect=True)
async def test_a_trust_3_agent_may_widen_its_row_to_scope_org(edit):
    outcome = await edit("carol", _row("carol"), {"visibility": "scope_org"})

    assert outcome.code is None
    assert outcome.stored.await_args.args[2]["visibility"] == "scope_org"


@pytest.mark.parametrize("edit", ["rest", "mcp"], indirect=True)
@pytest.mark.parametrize(
    "fields",
    [{"content": "Quarterly planning moves to Tuesday"}, {"title": "planning"}],
    ids=["content", "title"],
)
async def test_an_edit_that_keeps_a_row_scope_org_needs_trust_3(edit, fields):
    """An earlier ``scope_org`` row of a trust-1 agent stays as it is unless
    the agent narrows it: editing it in place republishes to every fleet."""
    outcome = await edit("alice", _scope_org_row("alice"), fields)

    assert outcome.code == AUTH_FLEET_SCOPE_FORBIDDEN
    outcome.stored.assert_not_awaited()


@pytest.mark.parametrize("edit", ["rest"], indirect=True)
async def test_an_agent_may_narrow_its_scope_org_row_below_trust_3(edit):
    outcome = await edit("alice", _scope_org_row("alice"), {"visibility": "scope_team"})

    assert outcome.code is None
    assert outcome.stored.await_args.args[2]["visibility"] == "scope_team"


@pytest.mark.parametrize("edit", ["rest"], indirect=True)
async def test_a_credential_without_an_agent_keeps_its_edit_authority(edit):
    outcome = await edit(None, _row("alice"), {"visibility": "scope_org"})

    assert outcome.code is None


# --- the advice agents read ----------------------------------------------------


def test_caura_write_does_not_steer_default_trust_agents_to_scope_org():
    """The description is in every session's ``tools/list``. It may name
    ``scope_org``, but only with the trust it needs, and it must not recommend
    it for sharing to the agents (trust 1 by default) who cannot write it."""
    description = get_spec("caura_write").description

    assert "scope_org (trust ≥ 3)" in description
    assert not re.search(r"[Pp]refer [^.]*\borg\b", description), description


@pytest.mark.parametrize("tool", ["caura_insights", "caura_evolve"])
def test_insights_and_evolve_name_trust_3_for_scope_all(tool):
    description = get_spec(tool).description

    assert "all (trust ≥ 3" in description, description

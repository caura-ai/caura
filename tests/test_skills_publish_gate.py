"""A skill reaches a node's disk only after a person approves it.

The OpenClaw plugin writes what ``POST /skills/installable`` returns into
``<plugin>/skills/<slug>/SKILL.md`` on every heartbeat, so that catalog is the
boundary. Whatever ``skills_factory.enabled`` says (it defaults to off):

- the catalog serves ``status='active'`` rows only;
- every skills write runs the lifecycle validator, so a write lands ``staged``
  (the inbox) unless an org admin sets ``active``;
- an agent needs trust 2 to write a skill, the cross-agent tier keystones use;
- an MCP skills write leaves the same audit row a REST write does.
"""

from __future__ import annotations

import pytest

from core_api import mcp_server
from tests._mcp_test_helpers import parse_envelope
from tests.conftest import get_admin_headers, get_test_auth, new_tenant_id, uid


def _skill(slug: str, **overrides) -> dict:
    data = {
        "name": slug,
        "slug": slug,
        "description": "Use when rotating the deploy keys.",
        "domain": "ops",
        "kind": "create",
        "source": "agent",
        "content": "# Rotate keys\n\nAsk the on-call engineer first.",
    }
    data.update(overrides)
    return data


async def _installable_slugs(client, tenant_id: str) -> set[str]:
    resp = await client.post(
        "/api/v1/skills/installable",
        json={"tenant_id": tenant_id},
        headers=get_admin_headers(),
    )
    assert resp.status_code == 200, resp.text
    return {d["doc_id"] for d in resp.json()}


@pytest.fixture
def as_agent():
    from core_api.app import app
    from core_api.auth import AuthContext, get_auth_context
    from core_api.tenant_context import set_current_tenant

    def _install(tenant_id: str, agent_id: str) -> None:
        async def _dep():
            set_current_tenant(tenant_id)
            return AuthContext(
                tenant_id=tenant_id, agent_id=agent_id, readable_tenant_ids=[tenant_id]
            )

        app.dependency_overrides[get_auth_context] = _dep

    yield _install
    app.dependency_overrides.pop(get_auth_context, None)


async def _set_trust(tenant_id: str, agent_id: str, level: int) -> None:
    # The service call the admin trust route makes; the route itself would see
    # the agent credential this module installs.
    from core_api.services.agent_service import update_trust_level

    await update_trust_level(tenant_id, agent_id, level)


async def test_a_trust_one_agent_rest_skill_write_never_reaches_a_node(
    client, as_agent
):
    """Flag off (the default). The agent is approved at trust 1."""
    tenant_id = new_tenant_id()
    agent_id = f"agent-{uid()}"
    slug = f"rotate-{uid()[:8]}"

    # First contact registers the agent at the default trust level (1).
    as_agent(tenant_id, agent_id)
    resp = await client.post(
        "/api/v1/documents",
        json={
            "tenant_id": tenant_id,
            "collection": "skills",
            "doc_id": slug,
            "data": _skill(slug),
        },
    )
    assert resp.status_code == 403, resp.text
    assert slug not in await _installable_slugs(client, tenant_id)


async def test_a_trust_one_agent_mcp_skill_write_never_reaches_a_node(
    client, mcp_env, monkeypatch
):
    """Flag off. The real storage stack, an agent at trust 1."""
    tenant_id = new_tenant_id()
    slug = f"rotate-{uid()[:8]}"
    monkeypatch.setattr(mcp_server, "_get_tenant", lambda: tenant_id)

    async def _trust_one(*args, **kwargs):
        agent = {
            "agent_id": "skill-writer",
            "tenant_id": tenant_id,
            "fleet_id": None,
            "trust_level": 1,
        }
        return (agent, "skill-writer") if "is_install_credential" in kwargs else agent

    monkeypatch.setattr(mcp_server, "resolve_write_agent", _trust_one)
    monkeypatch.setattr(mcp_server, "enforce_fleet_write", _trust_one)

    out = await mcp_server.caura_doc(
        op="write",
        collection="skills",
        doc_id=slug,
        data=_skill(slug),
        agent_id="skill-writer",
    )
    assert "error" in parse_envelope(out), out
    assert slug not in await _installable_slugs(client, tenant_id)


async def test_a_trust_two_agent_skill_lands_staged_and_is_not_installed(
    client, as_agent
):
    """Trust 2 may propose a skill. It waits in the inbox, off every node."""
    tenant_id = new_tenant_id()
    agent_id = f"agent-{uid()}"
    slug = f"rotate-{uid()[:8]}"
    as_agent(tenant_id, agent_id)
    # Register the agent, then raise it to trust 2.
    await client.post(
        "/api/v1/documents",
        json={
            "tenant_id": tenant_id,
            "collection": "notes",
            "doc_id": "hello",
            "data": {"x": 1},
        },
    )
    await _set_trust(tenant_id, agent_id, 2)

    resp = await client.post(
        "/api/v1/documents",
        json={
            "tenant_id": tenant_id,
            "collection": "skills",
            "doc_id": slug,
            "data": _skill(slug),
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "staged"
    # Asking for ``active`` is an admin's call.
    resp = await client.post(
        "/api/v1/documents",
        json={
            "tenant_id": tenant_id,
            "collection": "skills",
            "doc_id": f"{slug}-b",
            "data": _skill(f"{slug}-b", status="active"),
        },
    )
    assert resp.status_code == 403, resp.text
    assert slug not in await _installable_slugs(client, tenant_id)


async def test_a_tenant_key_skill_write_is_validated_with_the_flag_off(client):
    """No agent identity: the tenant's own key. The validator still runs, so an
    over-cap body is refused and a valid one is stamped with a scan."""
    tenant_id, headers = get_test_auth(new_tenant_id())
    slug = f"big-{uid()[:8]}"
    resp = await client.post(
        "/api/v1/documents",
        json={
            "tenant_id": tenant_id,
            "collection": "skills",
            "doc_id": slug,
            "data": _skill(slug, content="x" * 40_001),
        },
        headers=headers,
    )
    assert resp.status_code == 422, resp.text

    slug = f"ok-{uid()[:8]}"
    resp = await client.post(
        "/api/v1/documents",
        json={
            "tenant_id": tenant_id,
            "collection": "skills",
            "doc_id": slug,
            "data": _skill(slug),
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert "scan" in resp.json()["data"]


async def test_an_mcp_skill_write_leaves_an_audit_row(mcp_env, monkeypatch):
    from unittest.mock import AsyncMock

    from tests._mcp_test_helpers import stub_storage_client

    monkeypatch.setattr("common.embedding.get_embedding", AsyncMock(return_value=None))
    monkeypatch.setattr("common.embedding.embedding_configured", lambda _cfg: False)
    stub_storage_client(
        monkeypatch, upsert_document_xmax={"xmax": 0, "id": "doc-1"}, get_document=None
    )
    audit = AsyncMock()
    monkeypatch.setattr(mcp_server, "log_action", audit)
    monkeypatch.setattr(
        mcp_server,
        "get_settings_for_display",
        AsyncMock(return_value={"skills_factory": {"enabled": False}}),
    )

    out = await mcp_server.caura_doc(
        op="write", collection="skills", doc_id="rotate", data=_skill("rotate")
    )
    assert parse_envelope(out)["ok"] is True, out
    actions = [c.kwargs.get("action") for c in audit.await_args_list]
    assert actions == ["doc_upsert"]
    detail = audit.await_args_list[0].kwargs["detail"]
    assert detail["collection"] == "skills" and detail["doc_id"] == "rotate"

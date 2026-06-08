"""Agent-management & settings are admin-plane: agent credentials are blocked.

Regression for the trust self-escalation gap — PATCH /agents/{id}/trust (and the
fleet/delete/settings mutators) authorized on tenant only, so any agent key could
PATCH its own trust_level to 3 and unlock the entire trust ladder. Fixed with
AuthContext.enforce_not_agent_credential (agent-scoped creds blocked; tenant/user/
admin creds — no gateway X-Agent-ID — unaffected).
"""

import uuid

import pytest
from fastapi import HTTPException

from core_api.auth import AuthContext


# ---------------------------------------------------------------------------
# Unit: the helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_enforce_not_agent_credential_blocks_agent():
    ctx = AuthContext(tenant_id="t", agent_id="bob")
    with pytest.raises(HTTPException) as exc:
        ctx.enforce_not_agent_credential("change agent trust levels")
    assert exc.value.status_code == 403


@pytest.mark.unit
def test_enforce_not_agent_credential_allows_user_and_admin():
    AuthContext(tenant_id="t", agent_id=None).enforce_not_agent_credential()  # tenant/user key
    AuthContext(tenant_id=None, is_admin=True, agent_id="x").enforce_not_agent_credential()  # admin bypass


# ---------------------------------------------------------------------------
# API: agent credentials are 403'd on the admin-plane endpoints
# ---------------------------------------------------------------------------


@pytest.fixture
def as_agent():
    from core_api.app import app
    from core_api.auth import AuthContext as _AC, get_auth_context
    from core_api.db.session import set_current_tenant

    def _install(tenant_id, agent_id):
        async def _dep():
            set_current_tenant(tenant_id)
            return _AC(tenant_id=tenant_id, agent_id=agent_id, readable_tenant_ids=[tenant_id])

        app.dependency_overrides[get_auth_context] = _dep

    yield _install
    from core_api.app import app as _app
    from core_api.auth import get_auth_context as _gac

    _app.dependency_overrides.pop(_gac, None)


@pytest.mark.integration
async def test_agent_cannot_escalate_own_trust(client, as_agent):
    from tests.conftest import get_test_auth

    tenant_id, _ = get_test_auth()
    as_agent(tenant_id, "bob")
    r = await client.patch(f"/api/v1/agents/bob/trust?tenant_id={tenant_id}", json={"trust_level": 3})
    assert r.status_code == 403, r.text


@pytest.mark.integration
async def test_agent_cannot_change_fleet_or_delete_or_settings(client, as_agent):
    from tests.conftest import get_test_auth

    tenant_id, _ = get_test_auth()
    as_agent(tenant_id, "bob")
    r = await client.patch(f"/api/v1/agents/bob/fleet?tenant_id={tenant_id}", json={"fleet_id": "f2"})
    assert r.status_code == 403, r.text
    r = await client.delete(f"/api/v1/agents/victim?tenant_id={tenant_id}")
    assert r.status_code == 403, r.text
    r = await client.put(f"/api/v1/settings?tenant_id={tenant_id}", json={"recall": {}})
    assert r.status_code == 403, r.text
    # A hard fleet purge is admin-plane too — an agent key must not wipe a fleet.
    r = await client.post(f"/api/v1/fleet/some-fleet/purge?tenant_id={tenant_id}")
    assert r.status_code == 403, r.text


@pytest.mark.integration
async def test_agent_tune_self_allowed_peer_blocked(client, as_agent):
    from tests.conftest import get_test_auth

    tenant_id, _ = get_test_auth()
    as_agent(tenant_id, "bob")
    # peer → 403
    r = await client.patch(f"/api/v1/agents/alice/tune?tenant_id={tenant_id}", json={"top_k": 5})
    assert r.status_code == 403, r.text
    # self → passes the gate (404 because 'bob' isn't registered in test storage, NOT 403)
    r = await client.patch(f"/api/v1/agents/bob/tune?tenant_id={tenant_id}", json={"top_k": 5})
    assert r.status_code != 403, r.text


@pytest.mark.integration
async def test_admin_key_can_still_manage_agents(client):
    """Control: a non-agent (admin/tenant) credential is NOT blocked by the gate."""
    from tests.conftest import get_test_auth

    tenant_id, headers = get_test_auth()  # admin key → is_admin, agent_id=None
    # gate passes; reaches update_trust_level → not a 403 (404/200/422 depending on storage)
    r = await client.patch(
        f"/api/v1/agents/nonexistent-{uuid.uuid4().hex[:6]}/trust?tenant_id={tenant_id}",
        json={"trust_level": 2},
        headers=headers,
    )
    assert r.status_code != 403, r.text

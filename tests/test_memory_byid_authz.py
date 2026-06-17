"""By-id memory authorization (fleet / scope_agent / trust ladder).

Regression coverage for the BOLA/IDOR gap where *by-id* memory handlers
(``GET/PATCH/DELETE /memories/{id}`` and the MCP ``read``/``lineage``/
``transition``/``update``/``delete`` ops) authorized on ``tenant_id`` alone,
while the list/search paths additionally enforce ``scope_agent`` ownership and
the cross-fleet trust ladder. A same-tenant agent credential that learned a
peer's ``memory_id`` (e.g. via search) could read or mutate a row outside its
fleet/agent scope.

The fix routes every by-id handler through
``agent_service.authorize_memory_access`` (and ``enforce_memory_read``). These
tests lock the contract at three levels: the helper itself (unit), the
agent-facing MCP surface, and the REST endpoint.
"""

import uuid

import pytest

from core_api.services import agent_service
from core_api.services.agent_service import authorize_memory_access

from tests.conftest import parse_envelope  # noqa: F401  (re-exported MCP helper)


# ---------------------------------------------------------------------------
# Unit: authorize_memory_access matrix (no DB; lookup_agent is mocked)
# ---------------------------------------------------------------------------

pytestmark_unit = pytest.mark.unit


class _FakeMem:
    def __init__(self, *, visibility, agent_id, fleet_id, **extra):
        self.visibility = visibility
        self.agent_id = agent_id
        self.fleet_id = fleet_id
        for k, v in extra.items():
            setattr(self, k, v)


@pytest.fixture
def patch_lookup(monkeypatch):
    """Install a fake ``lookup_agent`` returning a controlled agent dict."""

    def _set(*, fleet_id=None, trust_level=0, exists=True):
        async def fake_lookup(db, tenant_id, agent_id):
            if not exists:
                return None
            return {"agent_id": agent_id, "fleet_id": fleet_id, "trust_level": trust_level}

        monkeypatch.setattr(agent_service, "lookup_agent", fake_lookup)

    return _set


async def _call(caller, visibility, owner, fleet, *, write=False):
    return await authorize_memory_access(
        None,
        "tenant-x",
        caller,
        visibility=visibility,
        owner_agent_id=owner,
        fleet_id=fleet,
        write=write,
    )


@pytest.mark.unit
async def test_no_agent_identity_allows_everything():
    # Tenant-scoped user/dashboard credential (no X-Agent-ID) keeps full access.
    assert await _call(None, "scope_agent", "alice", "fleet-alpha") is True


@pytest.mark.unit
async def test_scope_agent_author_allowed():
    assert await _call("alice", "scope_agent", "alice", "fleet-alpha") is True


@pytest.mark.unit
async def test_scope_agent_non_author_denied():
    assert await _call("bob", "scope_agent", "alice", "fleet-alpha") is False


@pytest.mark.unit
async def test_scope_org_is_tenant_global(patch_lookup):
    patch_lookup(fleet_id="fleet-beta", trust_level=0)
    assert await _call("bob", "scope_org", "alice", "fleet-alpha") is True


@pytest.mark.unit
async def test_scope_team_same_fleet_allowed(patch_lookup):
    patch_lookup(fleet_id="fleet-alpha", trust_level=0)
    assert await _call("bob", "scope_team", "alice", "fleet-alpha") is True


@pytest.mark.unit
async def test_scope_team_fleetless_row_allowed(patch_lookup):
    patch_lookup(fleet_id="fleet-beta", trust_level=0)
    assert await _call("bob", "scope_team", "alice", None) is True


@pytest.mark.unit
async def test_scope_team_cross_fleet_low_trust_denied(patch_lookup):
    patch_lookup(fleet_id="fleet-beta", trust_level=1)
    assert await _call("bob", "scope_team", "alice", "fleet-alpha") is False


@pytest.mark.unit
async def test_scope_team_cross_fleet_trust2_read_allowed(patch_lookup):
    patch_lookup(fleet_id="fleet-beta", trust_level=2)
    assert await _call("bob", "scope_team", "alice", "fleet-alpha") is True


@pytest.mark.unit
async def test_cross_fleet_write_requires_trust3(patch_lookup):
    patch_lookup(fleet_id="fleet-beta", trust_level=2)
    assert await _call("bob", "scope_team", "alice", "fleet-alpha", write=True) is False
    patch_lookup(fleet_id="fleet-beta", trust_level=3)
    assert await _call("bob", "scope_team", "alice", "fleet-alpha", write=True) is True


@pytest.mark.unit
async def test_unknown_agent_allowed(patch_lookup):
    # Mirrors enforce_fleet_read's allow-on-unknown (registration is a write path).
    patch_lookup(exists=False)
    assert await _call("ghost", "scope_team", "alice", "fleet-alpha") is True


# ---------------------------------------------------------------------------
# MCP surface: op=read honors fleet/scope (the agent-facing path)
# ---------------------------------------------------------------------------


def _fake_read_row(*, visibility, agent_id, fleet_id):
    mid = uuid.uuid4()
    return mid, _FakeMem(
        visibility=visibility,
        agent_id=agent_id,
        fleet_id=fleet_id,
        id=mid,
        content="cross-fleet secret",
        memory_type="fact",
        status="active",
        weight=0.5,
        title=None,
        created_at=None,
        last_recalled_at=None,
        recall_count=0,
        deleted_at=None,
        metadata_=None,
    )


async def _mcp_read(mcp_env, monkeypatch, *, caller, row):
    from core_api import mcp_server
    from core_api.repositories import memory_repo

    mid, mem = row

    async def fake_get(db, _uid, _tenant):
        return mem

    monkeypatch.setattr(memory_repo, "get_by_id_for_tenant", fake_get)
    monkeypatch.setattr(mcp_server, "_get_agent_id", lambda: caller)
    return await mcp_server.memclaw_manage(op="read", memory_id=str(mid))


@pytest.mark.unit
async def test_mcp_read_cross_fleet_low_trust_denied(mcp_env, monkeypatch, patch_lookup):
    patch_lookup(fleet_id="fleet-beta", trust_level=1)
    row = _fake_read_row(visibility="scope_team", agent_id="alice", fleet_id="fleet-alpha")
    env = parse_envelope(await _mcp_read(mcp_env, monkeypatch, caller="bob", row=row))
    assert env["error"]["code"] == "NOT_FOUND"


@pytest.mark.unit
async def test_mcp_read_scope_agent_non_author_denied(mcp_env, monkeypatch):
    row = _fake_read_row(visibility="scope_agent", agent_id="alice", fleet_id="fleet-alpha")
    env = parse_envelope(await _mcp_read(mcp_env, monkeypatch, caller="bob", row=row))
    assert env["error"]["code"] == "NOT_FOUND"


@pytest.mark.unit
async def test_mcp_read_same_fleet_allowed(mcp_env, monkeypatch, patch_lookup):
    patch_lookup(fleet_id="fleet-alpha", trust_level=1)
    row = _fake_read_row(visibility="scope_team", agent_id="alice", fleet_id="fleet-alpha")
    env = parse_envelope(await _mcp_read(mcp_env, monkeypatch, caller="bob", row=row))
    assert "error" not in env
    assert env["content"] == "cross-fleet secret"


# ---------------------------------------------------------------------------
# REST surface: GET /memories/{id} honors fleet/scope (integration; needs PG)
# ---------------------------------------------------------------------------


@pytest.fixture
def as_agent(monkeypatch):
    """Override get_auth_context to authenticate as a given agent identity.

    Mirrors what the enterprise gateway does (X-Agent-ID → AuthContext.agent_id)
    without needing a real gateway; standalone mode otherwise leaves agent_id None.
    """
    from core_api.app import app
    from core_api.auth import AuthContext, get_auth_context
    from core_api.db.session import set_current_tenant

    def _install(tenant_id: str, agent_id: str | None):
        async def _dep():
            set_current_tenant(tenant_id)
            return AuthContext(
                tenant_id=tenant_id,
                agent_id=agent_id,
                readable_tenant_ids=[tenant_id],
            )

        app.dependency_overrides[get_auth_context] = _dep

    yield _install
    from core_api.app import app as _app
    from core_api.auth import get_auth_context as _gac

    _app.dependency_overrides.pop(_gac, None)


async def _write(client, headers, tenant_id, *, agent_id, fleet_id, visibility, content=None, write_mode=None):
    body = {
        "tenant_id": tenant_id,
        "content": content or f"row {uuid.uuid4().hex[:8]}",
        "agent_id": agent_id,
        "fleet_id": fleet_id,
        "visibility": visibility,
        "memory_type": "fact",
    }
    # write_mode="strong" keeps embedding/indexing synchronous, so a search
    # immediately after the write is deterministic (see test_a13). Omitted by
    # default — only search-after-write tests need it; the by-id/list tests
    # read from the DB directly and are unaffected.
    if write_mode is not None:
        body["write_mode"] = write_mode
    resp = await client.post("/api/v1/memories", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.mark.integration
async def test_rest_get_cross_fleet_denied_then_allowed_by_trust(client, as_agent, patch_lookup):
    from tests.conftest import get_test_auth

    tenant_id, headers = get_test_auth()
    mid = await _write(
        client, headers, tenant_id, agent_id="alice", fleet_id="fleet-alpha", visibility="scope_team"
    )

    as_agent(tenant_id, "bob")
    patch_lookup(fleet_id="fleet-beta", trust_level=1)
    resp = await client.get(f"/api/v1/memories/{mid}?tenant_id={tenant_id}")
    assert resp.status_code == 404, resp.text

    patch_lookup(fleet_id="fleet-beta", trust_level=2)
    resp = await client.get(f"/api/v1/memories/{mid}?tenant_id={tenant_id}")
    assert resp.status_code == 200, resp.text


@pytest.mark.integration
async def test_rest_get_scope_agent_non_author_denied(client, as_agent):
    from tests.conftest import get_test_auth

    tenant_id, headers = get_test_auth()
    mid = await _write(
        client, headers, tenant_id, agent_id="alice", fleet_id="fleet-alpha", visibility="scope_agent"
    )

    as_agent(tenant_id, "bob")
    resp = await client.get(f"/api/v1/memories/{mid}?tenant_id={tenant_id}")
    assert resp.status_code == 404, resp.text

    as_agent(tenant_id, "alice")  # the author
    resp = await client.get(f"/api/v1/memories/{mid}?tenant_id={tenant_id}")
    assert resp.status_code == 200, resp.text


@pytest.mark.integration
async def test_rest_get_dashboard_no_agent_keeps_full_access(client):
    """Tenant-scoped credential (no X-Agent-ID) is unchanged — no regression."""
    from tests.conftest import get_test_auth

    tenant_id, headers = get_test_auth()
    mid = await _write(
        client, headers, tenant_id, agent_id="alice", fleet_id="fleet-alpha", visibility="scope_agent"
    )
    resp = await client.get(f"/api/v1/memories/{mid}?tenant_id={tenant_id}", headers=headers)
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# list/search: identity comes from the authenticated agent, not the param
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_rest_list_uses_authenticated_identity_not_param(client, as_agent):
    """An agent credential can't see a peer's scope_agent rows by passing the
    peer's agent_id as the ?agent_id= query param — the visibility identity is
    auth.agent_id, the param is only the author filter."""
    from tests.conftest import get_test_auth

    tenant_id, headers = get_test_auth()
    priv = await _write(
        client, headers, tenant_id, agent_id="alice", fleet_id="fleet-alpha", visibility="scope_agent"
    )

    # Bob (authenticated agent) tries to harvest alice's private rows by passing
    # agent_id=alice. Pre-fix this set caller_agent_id=alice and leaked them.
    as_agent(tenant_id, "bob")
    resp = await client.get(f"/api/v1/memories?tenant_id={tenant_id}&agent_id=alice")
    assert resp.status_code == 200, resp.text
    ids = [m["id"] for m in resp.json()["items"]]
    assert priv not in ids, "scope_agent row leaked via spoofed agent_id query param"

    # Alice herself sees her own scope_agent row.
    as_agent(tenant_id, "alice")
    resp = await client.get(f"/api/v1/memories?tenant_id={tenant_id}&agent_id=alice")
    assert resp.status_code == 200, resp.text
    assert priv in [m["id"] for m in resp.json()["items"]]


@pytest.mark.integration
async def test_rest_search_scope_agent_uses_authenticated_identity(client, as_agent):
    """/search without filter_agent_id scopes scope_agent visibility to the
    authenticated agent (auth.agent_id), not tenant-wide."""
    from tests.conftest import get_test_auth

    tenant_id, headers = get_test_auth()
    marker = f"PRIVATEMARKER{uuid.uuid4().hex[:10]}"
    priv = await _write(
        client, headers, tenant_id, agent_id="alice", fleet_id="fleet-alpha",
        visibility="scope_agent", content=f"alice private note {marker}",
        write_mode="strong",  # synchronous embedding ⇒ the row is searchable immediately (de-flakes)
    )

    async def _search(query):
        r = await client.post("/api/v1/search", json={"tenant_id": tenant_id, "query": query, "top_k": 20})
        assert r.status_code == 200, r.text
        return [m["id"] for m in r.json()["items"]]

    as_agent(tenant_id, "bob")
    assert priv not in await _search(marker), "scope_agent row leaked to another agent in search"

    as_agent(tenant_id, "alice")
    assert priv in await _search(marker), "author cannot see own scope_agent row in search"


# ---------------------------------------------------------------------------
# F2 — bulk/whole-tenant delete requires admin-trust for agent credentials (BFLA)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_rest_delete_all_blocked_for_low_trust_agent(client, as_agent, patch_lookup):
    """A trust-1 agent key must not be able to wipe the tenant via DELETE /memories."""
    from tests.conftest import get_test_auth

    tenant_id, _ = get_test_auth()
    as_agent(tenant_id, "bob")
    patch_lookup(fleet_id="fleet-beta", trust_level=1)
    r = await client.delete(f"/api/v1/memories?tenant_id={tenant_id}")
    assert r.status_code == 403, r.text

    # Admin-trust (>=3) agent is allowed (scoped to a non-existent fleet → deletes 0).
    patch_lookup(fleet_id="fleet-beta", trust_level=3)
    r = await client.delete(f"/api/v1/memories?tenant_id={tenant_id}&fleet_id=nonexistent-{uuid.uuid4().hex[:6]}")
    assert r.status_code == 204, r.text


@pytest.mark.integration
async def test_rest_bulk_delete_by_ids_blocked_for_low_trust_agent(client, as_agent, patch_lookup):
    from tests.conftest import get_test_auth

    tenant_id, _ = get_test_auth()
    as_agent(tenant_id, "bob")
    patch_lookup(fleet_id="fleet-beta", trust_level=1)
    r = await client.post(
        "/api/v1/memories/bulk-delete",
        json={"tenant_id": tenant_id, "ids": [str(uuid.uuid4())]},
    )
    assert r.status_code == 403, r.text


@pytest.mark.integration
async def test_rest_delete_all_tenant_key_unchanged(client):
    """Tenant/admin credential (no X-Agent-ID) keeps full delete reach — no regression
    to dashboard reset / tagged cleanup. Scoped to a non-existent fleet to stay inert."""
    from tests.conftest import get_test_auth

    tenant_id, headers = get_test_auth()
    r = await client.delete(
        f"/api/v1/memories?tenant_id={tenant_id}&fleet_id=nonexistent-{uuid.uuid4().hex[:6]}",
        headers=headers,
    )
    assert r.status_code == 204, r.text

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
from tests._mcp_test_helpers import stub_storage_client
from tests.conftest import parse_envelope

# ---------------------------------------------------------------------------
# Unit: authorize_memory_access matrix (no DB; lookup_agent is mocked)
# ---------------------------------------------------------------------------

pytestmark_unit = pytest.mark.unit


@pytest.fixture
def patch_lookup(monkeypatch):
    """Install a fake ``lookup_agent`` returning a controlled agent dict."""

    def _set(*, fleet_id=None, trust_level=0, exists=True):
        async def fake_lookup(tenant_id, agent_id):
            if not exists:
                return None
            return {
                "agent_id": agent_id,
                "fleet_id": fleet_id,
                "trust_level": trust_level,
            }

        monkeypatch.setattr(agent_service, "lookup_agent", fake_lookup)

    return _set


async def _call(caller, visibility, owner, fleet, *, write=False):
    return await authorize_memory_access(
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
    # Fix 2 Phase 4: ``caura_manage`` op=read fetches via the storage client
    # (``sc.get_memory``), which returns a plain dict — not an ORM
    # row. The authz contract under test (``authorize_memory_access`` over the
    # row's visibility / owner / fleet) is unchanged; only the row SHAPE is.
    mid = uuid.uuid4()
    return mid, {
        "id": str(mid),
        "visibility": visibility,
        "agent_id": agent_id,
        "fleet_id": fleet_id,
        "content": "cross-fleet secret",
        "memory_type": "fact",
        "status": "active",
        "weight": 0.5,
        "title": None,
        "created_at": None,
        "last_recalled_at": None,
        "recall_count": 0,
        "deleted_at": None,
        "metadata_": None,
    }


async def _mcp_read(mcp_env, monkeypatch, *, caller, row):
    from core_api import mcp_server

    mid, mem = row

    # Stub the storage client's by-id read; the real ``authorize_memory_access``
    # then runs over the returned row (``lookup_agent`` is controlled by
    # ``patch_lookup``), exactly as the pre-migration test exercised it.
    stub_storage_client(monkeypatch, get_memory=mem)
    monkeypatch.setattr(mcp_server, "_get_agent_id", lambda: caller)
    return await mcp_server.caura_manage(op="read", memory_id=str(mid))


@pytest.mark.unit
async def test_mcp_read_cross_fleet_low_trust_denied(
    mcp_env, monkeypatch, patch_lookup
):
    patch_lookup(fleet_id="fleet-beta", trust_level=1)
    row = _fake_read_row(
        visibility="scope_team", agent_id="alice", fleet_id="fleet-alpha"
    )
    env = parse_envelope(await _mcp_read(mcp_env, monkeypatch, caller="bob", row=row))
    assert env["error"]["code"] == "NOT_FOUND"


@pytest.mark.unit
async def test_mcp_read_scope_agent_non_author_denied(mcp_env, monkeypatch):
    row = _fake_read_row(
        visibility="scope_agent", agent_id="alice", fleet_id="fleet-alpha"
    )
    env = parse_envelope(await _mcp_read(mcp_env, monkeypatch, caller="bob", row=row))
    assert env["error"]["code"] == "NOT_FOUND"


@pytest.mark.unit
async def test_mcp_read_same_fleet_allowed(mcp_env, monkeypatch, patch_lookup):
    patch_lookup(fleet_id="fleet-alpha", trust_level=1)
    row = _fake_read_row(
        visibility="scope_team", agent_id="alice", fleet_id="fleet-alpha"
    )
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
    from core_api.tenant_context import set_current_tenant

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


@pytest.fixture
def as_tenant_key(monkeypatch):
    """Authenticate as a TENANT-scoped credential: a tenant, and no agent identity.

    USE THIS, NOT ``get_test_auth()``, for anything touching the delete trust
    gate. ``get_test_auth()`` returns the ADMIN key, which resolves to
    ``AuthContext(tenant_id=None, is_admin=True)`` — so ``if auth.tenant_id and
    caller_agent_id:`` is false on the ``tenant_id`` half before the identity
    half is even read, and **the admin key cannot exercise this gate at all.**

    Why that matters more than it looks: a test written with the admin key
    passes identically before and after any change to the gate, because it
    never reaches it. It does not fail loudly — it succeeds for the wrong
    reason, which is indistinguishable from working. The first draft of the
    tests below used it and would have been worthless in exactly that way;
    reading the auth paths is the only way to catch it, since running it looks
    like a pass.

    The credential that actually reaches the gate without an agent identity is
    a tenant key: ``tenant_id`` set, ``agent_id`` None. That is the shape the
    REST/MCP parity smoke measured and the one this fixture installs.
    """
    from core_api.app import app
    from core_api.auth import AuthContext, get_auth_context
    from core_api.tenant_context import set_current_tenant

    def _install(tenant_id: str):
        async def _dep():
            set_current_tenant(tenant_id)
            return AuthContext(
                tenant_id=tenant_id,
                agent_id=None,
                readable_tenant_ids=[tenant_id],
            )

        app.dependency_overrides[get_auth_context] = _dep

    yield _install
    from core_api.app import app as _app
    from core_api.auth import get_auth_context as _gac

    _app.dependency_overrides.pop(_gac, None)


async def _write(
    client,
    headers,
    tenant_id,
    *,
    agent_id,
    fleet_id,
    visibility,
    content=None,
    write_mode=None,
):
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
async def test_rest_get_cross_fleet_denied_then_allowed_by_trust(
    client, as_agent, patch_lookup
):
    from tests.conftest import get_test_auth

    tenant_id, headers = get_test_auth()
    mid = await _write(
        client,
        headers,
        tenant_id,
        agent_id="alice",
        fleet_id="fleet-alpha",
        visibility="scope_team",
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
        client,
        headers,
        tenant_id,
        agent_id="alice",
        fleet_id="fleet-alpha",
        visibility="scope_agent",
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
        client,
        headers,
        tenant_id,
        agent_id="alice",
        fleet_id="fleet-alpha",
        visibility="scope_agent",
    )
    resp = await client.get(
        f"/api/v1/memories/{mid}?tenant_id={tenant_id}", headers=headers
    )
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
        client,
        headers,
        tenant_id,
        agent_id="alice",
        fleet_id="fleet-alpha",
        visibility="scope_agent",
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
    content = f"alice private note {marker}"
    priv = await _write(
        client,
        headers,
        tenant_id,
        agent_id="alice",
        fleet_id="fleet-alpha",
        visibility="scope_agent",
        content=content,
        write_mode="strong",  # synchronous embedding ⇒ the row is searchable immediately (de-flakes)
    )

    async def _search(query):
        r = await client.post(
            "/api/v1/search", json={"tenant_id": tenant_id, "query": query, "top_k": 20}
        )
        assert r.status_code == 200, r.text
        return [m["id"] for m in r.json()["items"]]

    # Query the row's EXACT content, not just the bare marker. The deterministic
    # word-set fake embedder makes that a similarity-1.0 hit, so alice's freshly
    # written row clears any relevance cutoff and is returned — the assertions
    # then depend only on the scope_agent VISIBILITY filter, not on stochastic
    # vector-rank ordering over the shared test corpus. (A marker-only query
    # ranked the row out of top_k and flaked: "author cannot see own row".)
    as_agent(tenant_id, "bob")
    assert priv not in await _search(content), (
        "scope_agent row leaked to another agent in search"
    )

    as_agent(tenant_id, "alice")
    assert priv in await _search(content), (
        "author cannot see own scope_agent row in search"
    )


# ---------------------------------------------------------------------------
# F2 — bulk/whole-tenant delete requires admin-trust for agent credentials (BFLA)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_rest_delete_all_blocked_for_low_trust_agent(
    client, as_agent, patch_lookup
):
    """A trust-1 agent key must not be able to wipe the tenant via DELETE /memories."""
    from tests.conftest import get_test_auth

    tenant_id, _ = get_test_auth()
    as_agent(tenant_id, "bob")
    patch_lookup(fleet_id="fleet-beta", trust_level=1)
    r = await client.delete(f"/api/v1/memories?tenant_id={tenant_id}")
    assert r.status_code == 403, r.text

    # Admin-trust (>=3) agent is allowed (scoped to a non-existent fleet → deletes 0).
    patch_lookup(fleet_id="fleet-beta", trust_level=3)
    r = await client.delete(
        f"/api/v1/memories?tenant_id={tenant_id}&fleet_id=nonexistent-{uuid.uuid4().hex[:6]}"
    )
    assert r.status_code == 204, r.text


@pytest.mark.integration
async def test_rest_bulk_delete_by_ids_blocked_for_low_trust_agent(
    client, as_agent, patch_lookup
):
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


# ---------------------------------------------------------------------------
# The delete gate's scope: agent credentials only
#
# Decided 2026-09-03. REST and MCP were measured returning opposite answers to
# the same call — the same credential, tenant and ``agent_id`` got 403 on
# ``DELETE /memories/{id}`` and success on ``caura_manage op=delete``. REST was
# the surface in the wrong: it built its authorization principal as
# ``auth.agent_id or agent_id``, promoting a caller-supplied query parameter to
# a principal. That gave a tenant key the choice of whether the gate applied —
# omit the param to skip it, or name any trust>=3 agent to pass it — so the only
# caller it reliably refused was the honest one naming a low-trust identity.
#
# The ruling: the trust ladder governs AGENT credentials; tenant scope governs
# TENANT keys. A tenant key holds no trust level to compare and is authorized by
# tenant scope, exactly as it already is on this route's bulk siblings
# (``test_rest_delete_all_tenant_key_unchanged`` above) and on MCP read /
# update / lineage / bulk_delete.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_rest_delete_byid_tenant_key_not_trust_gated(
    client, as_tenant_key, patch_lookup
):
    """A tenant key naming a low-trust agent may delete; the gate is not its gate.

    THIS IS THE TEST THAT FAILS WITHOUT THE FIX. Before the change the route
    read the ``agent_id`` query param as its principal, so ``yanki-smoke``
    (trust 1) was looked up, failed ``enforce_delete``'s trust>=3 bar, and the
    request got 403 — while the identical call over MCP succeeded. Confirmed
    failing on the parent commit: 403 != 204.
    """
    from tests.conftest import get_test_auth

    tenant_id, headers = get_test_auth()
    mem_id = await _write(
        client,
        headers,
        tenant_id,
        agent_id="yanki-smoke",
        fleet_id=None,
        visibility="scope_team",
    )
    # Resolve ``yanki-smoke`` as a real but low-trust agent. If the param were
    # still a principal this is what would refuse the call.
    patch_lookup(fleet_id=None, trust_level=1)
    as_tenant_key(tenant_id)

    r = await client.delete(
        f"/api/v1/memories/{mem_id}?tenant_id={tenant_id}&agent_id=yanki-smoke",
    )
    assert r.status_code == 204, r.text


@pytest.mark.integration
async def test_rest_delete_byid_tenant_key_still_attributed_to_named_agent(
    client, as_tenant_key, patch_lookup, monkeypatch
):
    """Dropping the param as a PRINCIPAL must not drop it as ATTRIBUTION.

    The two uses sat one line apart behind a single variable, which is how the
    defect was born; this pins them apart. The audit row must still name
    ``yanki-smoke`` — attributing a tenant key's deletes to ``None`` would trade
    an authorization bug for an accountability one.
    """
    from core_api.routes import memories as memories_route
    from tests.conftest import get_test_auth

    tenant_id, headers = get_test_auth()
    mem_id = await _write(
        client,
        headers,
        tenant_id,
        agent_id="yanki-smoke",
        fleet_id=None,
        visibility="scope_team",
    )
    patch_lookup(fleet_id=None, trust_level=1)

    seen: list[dict] = []
    real_log_action = memories_route.log_action

    async def _spy(**kwargs):
        seen.append(kwargs)
        return await real_log_action(**kwargs)

    monkeypatch.setattr(memories_route, "log_action", _spy)
    as_tenant_key(tenant_id)

    r = await client.delete(
        f"/api/v1/memories/{mem_id}?tenant_id={tenant_id}&agent_id=yanki-smoke",
    )
    assert r.status_code == 204, r.text

    deletes = [k for k in seen if k.get("action") == "delete"]
    assert deletes, f"no delete audit row logged; saw {[k.get('action') for k in seen]}"
    assert deletes[0]["agent_id"] == "yanki-smoke", (
        "tenant key's delete must stay attributed to the agent_id it supplied, "
        f"got {deletes[0]['agent_id']!r}"
    )


@pytest.mark.integration
async def test_rest_delete_byid_low_trust_agent_credential_still_403(
    client, as_agent, patch_lookup
):
    """GUARD, NOT EVIDENCE — this passes both before and after the fix.

    It exists so a future change cannot quietly widen the tenant-key exemption
    into an agent-credential one. An authenticated trust-1 agent (identity from
    the gateway's X-Agent-ID, never from the query param) is still refused.
    Do not read this test's green as confirmation that the fix works: the test
    above is the one that changes behaviour.
    """
    from tests.conftest import get_test_auth

    tenant_id, headers = get_test_auth()
    mem_id = await _write(
        client,
        headers,
        tenant_id,
        agent_id="bob",
        fleet_id="fleet-beta",
        visibility="scope_team",
    )
    as_agent(tenant_id, "bob")
    patch_lookup(fleet_id="fleet-beta", trust_level=1)

    r = await client.delete(f"/api/v1/memories/{mem_id}?tenant_id={tenant_id}")
    assert r.status_code == 403, r.text
    assert "not permitted to delete" in r.text


@pytest.mark.integration
async def test_rest_delete_byid_agent_credential_cannot_spoof_via_param(
    client, as_agent, patch_lookup
):
    """GUARD, NOT EVIDENCE — passes before and after.

    The precedence rule the old comment claimed (``auth.agent_id`` wins over the
    param) held for agent credentials and still holds, now because the param is
    not consulted for authorization at all. A trust-1 agent naming a trust-3
    identity in the query string is still refused on its own trust level.
    """
    from tests.conftest import get_test_auth

    tenant_id, headers = get_test_auth()
    mem_id = await _write(
        client,
        headers,
        tenant_id,
        agent_id="bob",
        fleet_id="fleet-beta",
        visibility="scope_team",
    )
    as_agent(tenant_id, "bob")
    patch_lookup(fleet_id="fleet-beta", trust_level=1)

    r = await client.delete(
        f"/api/v1/memories/{mem_id}?tenant_id={tenant_id}&agent_id=admin-agent"
    )
    assert r.status_code == 403, r.text

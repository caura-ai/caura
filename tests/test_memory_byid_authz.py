"""By-id memory authorization (fleet / scope_agent / trust ladder).

Regression coverage for the BOLA/IDOR gap where *by-id* memory handlers
(``GET/PATCH/DELETE /memories/{id}`` and the MCP ``read``/``lineage``/
``transition``/``update``/``delete`` ops) authorized on ``tenant_id`` alone,
while the list/search paths additionally enforce ``scope_agent`` ownership and
the cross-fleet trust ladder. A same-tenant agent credential that learned a
peer's ``memory_id`` (e.g. via search) could read or mutate a row outside its
fleet/agent scope.

The fix routes every by-id handler through
``agent_service.authorize_memory_access``. These
tests lock the contract at three levels: the helper itself (unit), the
agent-facing MCP surface, and the REST endpoint.
"""

import uuid
from unittest.mock import AsyncMock

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
        async def fake_lookup(tenant_id, agent_id, *, read=True):
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
async def test_scope_agent_author_allowed(patch_lookup):
    patch_lookup(fleet_id="fleet-alpha", trust_level=1)
    assert await _call("alice", "scope_agent", "alice", "fleet-alpha") is True


@pytest.mark.unit
async def test_scope_agent_non_author_denied(patch_lookup):
    patch_lookup(fleet_id="fleet-alpha", trust_level=1)
    assert await _call("bob", "scope_agent", "alice", "fleet-alpha") is False


@pytest.mark.unit
async def test_scope_org_is_tenant_global(patch_lookup):
    patch_lookup(fleet_id="fleet-beta", trust_level=1)
    assert await _call("bob", "scope_org", "alice", "fleet-alpha") is True


@pytest.mark.unit
async def test_scope_team_same_fleet_allowed(patch_lookup):
    patch_lookup(fleet_id="fleet-alpha", trust_level=1)
    assert await _call("bob", "scope_team", "alice", "fleet-alpha") is True


@pytest.mark.unit
async def test_scope_team_fleetless_row_allowed(patch_lookup):
    patch_lookup(fleet_id="fleet-beta", trust_level=1)
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
async def test_mcp_read_scope_agent_non_author_denied(
    mcp_env, monkeypatch, patch_lookup
):
    patch_lookup(fleet_id="fleet-alpha", trust_level=1)
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


# ---------------------------------------------------------------------------
# Install credentials: another install's agents' private rows stay private
#
# An install (broker) credential carries no agent identity. On shared rows it
# keeps the tenant-wide reach its by-id calls have always had, since the broker
# names no agent there. A ``scope_agent`` row, though, is private to its author,
# so it opens to an install only when the install owns that author. And where
# an install names an agent, that agent must be one its install owns, as every
# broker write already requires.
# ---------------------------------------------------------------------------


@pytest.fixture
def install_owners(monkeypatch):
    """Run MCP calls as a gateway install credential over a fixed agent table.

    ``_set(install_uuid, owners)``: ``owners`` maps each registered agent to the
    install that owns it (None for an agent no install has claimed).
    """
    from core_api import mcp_server

    def _set(install_uuid: str, owners: dict[str, str | None]):
        mcp_server._via_gateway_var.set(True)
        mcp_server._credential_kind_var.set("install_credential")
        mcp_server._install_uuid_var.set(install_uuid)

        async def _lookup(tenant_id, agent_id, *, read=True):
            if agent_id not in owners:
                return None
            return {
                "agent_id": agent_id,
                "owner_install_uuid": owners[agent_id],
                "fleet_id": "fleet-alpha",
                "trust_level": 3,
            }

        monkeypatch.setattr(agent_service, "lookup_agent", _lookup)

    return _set


def _code(env) -> str | None:
    """The envelope's error code, or None for a success (of any shape)."""
    return (env.get("error") or {}).get("code") if isinstance(env, dict) else None


def _private_row_of(author):
    return _fake_read_row(
        visibility="scope_agent", agent_id=author, fleet_id="fleet-alpha"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("install", "readable"), [("install-a", False), ("install-b", True)]
)
async def test_mcp_install_reads_a_private_row_only_of_an_agent_it_owns(
    mcp_env, monkeypatch, install_owners, install, readable
):
    from core_api import mcp_server

    install_owners(install, {"victim": "install-b"})
    mid, mem = _private_row_of("victim")
    stub_storage_client(monkeypatch, get_memory=mem)

    env = parse_envelope(
        await mcp_server.caura_manage(op="read", memory_id=str(mid), agent_id="mine")
    )

    if readable:
        assert env["content"] == "cross-fleet secret"
    else:
        assert _code(env) == "NOT_FOUND"


@pytest.mark.unit
async def test_mcp_install_reads_a_shared_row_of_any_agent(
    mcp_env, monkeypatch, install_owners
):
    from core_api import mcp_server

    install_owners("install-a", {"victim": "install-b"})
    mid, mem = _fake_read_row(
        visibility="scope_team", agent_id="victim", fleet_id="fleet-alpha"
    )
    stub_storage_client(monkeypatch, get_memory=mem)

    env = parse_envelope(
        await mcp_server.caura_manage(op="read", memory_id=str(mid), agent_id="mine")
    )

    assert env["content"] == "cross-fleet secret"


@pytest.mark.unit
async def test_mcp_install_lineage_hides_another_installs_private_row(
    mcp_env, monkeypatch, install_owners
):
    from core_api import mcp_server

    install_owners("install-a", {"victim": "install-b"})
    mid, mem = _private_row_of("victim")
    stub_storage_client(
        monkeypatch,
        get_memory_contradictions={"memory": mem, "older": None, "supersessors": []},
    )

    env = parse_envelope(
        await mcp_server.caura_manage(op="lineage", memory_id=str(mid), agent_id="mine")
    )

    assert _code(env) == "NOT_FOUND"


@pytest.mark.unit
async def test_mcp_install_lineage_drops_linked_private_rows_it_does_not_own(
    mcp_env, monkeypatch, install_owners
):
    from core_api import mcp_server

    install_owners("install-a", {"mine": "install-a", "victim": "install-b"})
    mid, mem = _private_row_of("mine")
    _, theirs = _private_row_of("victim")
    _, own = _private_row_of("mine")
    stub_storage_client(
        monkeypatch,
        get_memory_contradictions={
            "memory": mem,
            "older": None,
            "supersessors": [theirs, own],
        },
    )

    env = parse_envelope(
        await mcp_server.caura_manage(op="lineage", memory_id=str(mid), agent_id="mine")
    )

    assert [row["id"] for row in env["supersessors"]] == [own["id"]]


@pytest.mark.unit
async def test_mcp_install_cannot_transition_another_installs_private_row(
    mcp_env, monkeypatch, install_owners
):
    from core_api import mcp_server

    install_owners("install-a", {"mine": "install-a", "victim": "install-b"})
    mid, mem = _private_row_of("victim")
    sc = stub_storage_client(monkeypatch, get_memory=mem, update_memory_status=None)
    monkeypatch.setattr(mcp_server, "log_action", AsyncMock())

    env = parse_envelope(
        await mcp_server.caura_manage(
            op="transition", memory_id=str(mid), status="archived", agent_id="mine"
        )
    )

    assert _code(env) == "PERMISSION_DENIED"
    sc.update_memory_status.assert_not_awaited()


@pytest.mark.unit
async def test_mcp_install_cannot_delete_another_installs_private_row(
    mcp_env, monkeypatch, install_owners
):
    from core_api import mcp_server

    install_owners("install-a", {"mine": "install-a", "victim": "install-b"})
    mid, mem = _private_row_of("victim")
    stub_storage_client(monkeypatch, get_memory=mem)
    soft_delete = AsyncMock()
    monkeypatch.setattr(mcp_server, "soft_delete_memory", soft_delete)
    monkeypatch.setattr(mcp_server, "log_action", AsyncMock())

    env = parse_envelope(
        await mcp_server.caura_manage(op="delete", memory_id=str(mid), agent_id="mine")
    )

    assert _code(env) == "PERMISSION_DENIED"
    soft_delete.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.parametrize("owner", ["install-b", None])
async def test_mcp_install_cannot_update_as_an_agent_it_does_not_own(
    mcp_env, monkeypatch, install_owners, owner
):
    from core_api import mcp_server

    install_owners("install-a", {"victim": owner})
    mid, mem = _private_row_of("victim")
    stub_storage_client(monkeypatch, get_memory=mem)
    update = AsyncMock()
    monkeypatch.setattr(mcp_server, "update_memory", update)

    env = parse_envelope(
        await mcp_server.caura_manage(
            op="update", memory_id=str(mid), content="rewritten", agent_id="victim"
        )
    )

    assert _code(env) == "AGENT_IDENTITY_MISMATCH"
    update.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("tool", "kwargs", "storage_call", "returns"),
    [
        ("caura_list", {"fleet_id": "fleet-alpha"}, "list_memories_by_filters", []),
        ("caura_stats", {"fleet_id": "fleet-alpha"}, "memory_stats_breakdown", {}),
        ("caura_keystones", {"fleet_id": "fleet-alpha"}, "list_keystones", ([], False)),
    ],
)
async def test_mcp_install_cannot_read_as_an_agent_it_does_not_own(
    mcp_env, monkeypatch, install_owners, tool, kwargs, storage_call, returns
):
    from core_api import mcp_server

    install_owners("install-a", {"victim": "install-b"})
    sc = stub_storage_client(monkeypatch, get_agent=None, **{storage_call: returns})

    env = parse_envelope(await getattr(mcp_server, tool)(agent_id="victim", **kwargs))

    getattr(sc, storage_call).assert_not_awaited()
    assert _code(env) == "AGENT_IDENTITY_MISMATCH"


@pytest.mark.unit
async def test_mcp_install_cannot_recall_as_an_agent_it_does_not_own(
    mcp_env, monkeypatch, install_owners
):
    from core_api import mcp_server

    install_owners("install-a", {"victim": "install-b"})
    stub_storage_client(monkeypatch, get_agent=None)
    search = mcp_env["service"]("search_memories")
    search.return_value = []

    env = parse_envelope(
        await mcp_server.caura_recall(query="secrets", agent_id="victim")
    )

    search.assert_not_called()
    assert _code(env) == "AGENT_IDENTITY_MISMATCH"


@pytest.mark.unit
async def test_mcp_agent_credential_delete_still_needs_delete_trust(
    mcp_env, monkeypatch, patch_lookup
):
    """An install credential is held to the row check alone; an agent
    credential still clears ``enforce_delete`` first."""
    from core_api import mcp_server

    patch_lookup(fleet_id="fleet-alpha", trust_level=1)
    mid, mem = _fake_read_row(
        visibility="scope_team", agent_id="alice", fleet_id="fleet-alpha"
    )
    stub_storage_client(monkeypatch, get_memory=mem)
    soft_delete = AsyncMock()
    monkeypatch.setattr(mcp_server, "soft_delete_memory", soft_delete)
    monkeypatch.setattr(mcp_server, "_get_agent_id", lambda: "alice")

    env = parse_envelope(await mcp_server.caura_manage(op="delete", memory_id=str(mid)))

    assert _code(env) == "AGENT_TRUST_TOO_LOW"
    soft_delete.assert_not_awaited()


@pytest.mark.unit
async def test_mcp_install_reads_as_an_agent_it_owns(
    mcp_env, monkeypatch, install_owners
):
    from core_api import mcp_server

    install_owners("install-a", {"mine": "install-a"})
    sc = stub_storage_client(monkeypatch, list_memories_by_filters=[])

    env = parse_envelope(await mcp_server.caura_list(agent_id="mine"))

    assert "error" not in env
    sc.list_memories_by_filters.assert_awaited_once()


@pytest.fixture
def as_install(monkeypatch):
    """Authenticate REST calls as a gateway install credential: a tenant, an
    install id, and no agent identity, as the gateway sends them."""
    from core_api.app import app
    from core_api.auth import AuthContext, get_auth_context
    from core_api.tenant_context import set_current_tenant

    def _install(tenant_id: str, install_uuid: str):
        async def _dep():
            set_current_tenant(tenant_id)
            return AuthContext(
                tenant_id=tenant_id,
                agent_id=None,
                is_install_credential=True,
                install_uuid=install_uuid,
                readable_tenant_ids=[tenant_id],
            )

        app.dependency_overrides[get_auth_context] = _dep

    yield _install
    from core_api.app import app as _app
    from core_api.auth import get_auth_context as _gac

    _app.dependency_overrides.pop(_gac, None)


async def _install_private_row(client, *, visibility="scope_agent"):
    """A row written by ``victim-…``, an agent ``install-b`` owns."""
    from tests.conftest import get_test_auth

    tenant_id, headers = get_test_auth()
    victim = f"victim-{uuid.uuid4().hex[:8]}"
    await agent_service.get_or_create_agent(
        tenant_id, victim, owner_install_uuid="install-b"
    )
    mid = await _write(
        client,
        headers,
        tenant_id,
        agent_id=victim,
        fleet_id=None,
        visibility=visibility,
    )
    return tenant_id, headers, victim, mid


@pytest.mark.integration
async def test_rest_install_reads_a_private_row_only_of_an_agent_it_owns(
    client, as_install
):
    tenant_id, _, _, mid = await _install_private_row(client)

    as_install(tenant_id, "install-a")
    resp = await client.get(f"/api/v1/memories/{mid}?tenant_id={tenant_id}")
    assert resp.status_code == 404, resp.text
    resp = await client.get(
        f"/api/v1/memories/{mid}/contradictions?tenant_id={tenant_id}"
    )
    assert resp.status_code == 404, resp.text

    as_install(tenant_id, "install-b")
    resp = await client.get(f"/api/v1/memories/{mid}?tenant_id={tenant_id}")
    assert resp.status_code == 200, resp.text


@pytest.mark.integration
async def test_rest_install_reads_a_shared_row_of_any_agent(client, as_install):
    tenant_id, _, _, mid = await _install_private_row(client, visibility="scope_team")

    as_install(tenant_id, "install-a")
    resp = await client.get(f"/api/v1/memories/{mid}?tenant_id={tenant_id}")
    assert resp.status_code == 200, resp.text


@pytest.mark.integration
async def test_rest_install_cannot_delete_another_installs_private_row(
    client, as_install
):
    tenant_id, _, _, mid = await _install_private_row(client)

    as_install(tenant_id, "install-a")
    resp = await client.delete(f"/api/v1/memories/{mid}?tenant_id={tenant_id}")
    assert resp.status_code == 403, resp.text

    as_install(tenant_id, "install-b")
    resp = await client.get(f"/api/v1/memories/{mid}?tenant_id={tenant_id}")
    assert resp.status_code == 200, resp.text  # still there
    resp = await client.delete(f"/api/v1/memories/{mid}?tenant_id={tenant_id}")
    assert resp.status_code == 204, resp.text


@pytest.mark.integration
async def test_rest_install_cannot_rewrite_another_installs_private_row(
    client, as_install
):
    tenant_id, _, victim, mid = await _install_private_row(client)

    as_install(tenant_id, "install-a")
    resp = await client.patch(
        f"/api/v1/memories/{mid}?tenant_id={tenant_id}", json={"title": "rewritten"}
    )
    assert resp.status_code == 403, resp.text
    resp = await client.patch(
        f"/api/v1/memories/{mid}?tenant_id={tenant_id}&agent_id={victim}",
        json={"title": "rewritten"},
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "AGENT_IDENTITY_MISMATCH"
    resp = await client.patch(
        f"/api/v1/memories/{mid}/status?tenant_id={tenant_id}",
        json={"status": "archived"},
    )
    assert resp.status_code == 403, resp.text

    as_install(tenant_id, "install-b")
    resp = await client.patch(
        f"/api/v1/memories/{mid}?tenant_id={tenant_id}", json={"title": "rewritten"}
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.integration
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/v1/memories?tenant_id={t}&agent_id={a}", None),
        ("GET", "/api/v1/memories/stats?tenant_id={t}&agent_id={a}", None),
        ("GET", "/api/v1/memories/field?tenant_id={t}&agent_id={a}&field=title", None),
        ("POST", "/api/v1/search", {"query": "row", "caller_agent_id": "{a}"}),
        ("POST", "/api/v1/recall", {"query": "row", "caller_agent_id": "{a}"}),
    ],
)
async def test_rest_install_cannot_read_as_an_agent_it_does_not_own(
    client, as_install, method, path, body
):
    tenant_id, _, victim, _ = await _install_private_row(client)

    as_install(tenant_id, "install-a")
    url = path.format(t=tenant_id, a=victim)
    if body is None:
        resp = await client.request(method, url)
    else:
        payload = {k: v.format(a=victim) for k, v in body.items()}
        resp = await client.request(
            method, url, json={"tenant_id": tenant_id, **payload}
        )

    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "AGENT_IDENTITY_MISMATCH"


@pytest.mark.integration
async def test_rest_install_lists_as_an_agent_it_owns(client, as_install):
    tenant_id, _, victim, mid = await _install_private_row(client)

    as_install(tenant_id, "install-b")
    resp = await client.get(f"/api/v1/memories?tenant_id={tenant_id}&agent_id={victim}")

    assert resp.status_code == 200, resp.text
    assert mid in [row["id"] for row in resp.json()["items"]]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/v1/memories?tenant_id={t}", None),
        ("GET", "/api/v1/memories/stats?tenant_id={t}", None),
        ("GET", "/api/v1/memories/field?tenant_id={t}&field=title", None),
        ("POST", "/api/v1/search", {"query": "row"}),
        ("POST", "/api/v1/recall", {"query": "row"}),
    ],
)
async def test_rest_install_naming_no_agent_reads_no_private_rows(
    client, as_install, method, path, body
):
    """How the broker reads: no agent named, so no visibility identity, and a
    reader with none is entitled to no ``scope_agent`` row (storage's
    ``_visibility_scope_clause``). The owning install naming its agent is the
    control: the row is there to be found."""
    tenant_id, _, victim, mid = await _install_private_row(client)

    async def _read(install: str, agent: str | None):
        as_install(tenant_id, install)
        url = path.format(t=tenant_id) + (
            f"&agent_id={agent}" if agent and body is None else ""
        )
        if body is None:
            return await client.request(method, url)
        named = {"caller_agent_id": agent} if agent else {}
        return await client.request(
            method, url, json={"tenant_id": tenant_id, **body, **named}
        )

    resp = await _read("install-a", None)
    assert resp.status_code == 200, resp.text
    assert mid not in resp.text
    assert victim not in resp.text

    if "/memories/field?" in path:
        # These authorization checks use two scopes; cooldown is tested separately.
        from core_api.services.memory_field import _build_started

        _build_started.clear()
    resp = await _read("install-b", victim)
    assert resp.status_code == 200, resp.text
    assert victim in resp.text

"""Which credentials may run a tenant's admin plane, and who the settings audit names.

The tenant-admin routes (tenant settings; agent trust, fleet and deletion; fleet
create, purge and delete; fleet commands and node release; the tenant-wide,
fleet-wide and by-id bulk memory deletes; document delete; ingest undo) answer
to a tenant's admin. Two machine credentials reach them through the enterprise
gateway with ``write`` and no org role, and neither is one:

* an **install credential** (``kind=install_credential``), which the Broker
  holds on every machine that runs it; and
* a **cross-tenant credential** (``kind=cross_tenant``, or any key whose read
  scope spans tenants), which widens reads for reporting and plugins.

Both are refused with ``CREDENTIAL_KIND_FORBIDDEN``. A tenant API key keeps the
OSS contract (tenant key = admin), as do a signed-in org admin, the admin key
and the shared ``CAURA_API_KEY``. The Broker's own calls, the operations frozen
in ``core-api/openapi.broker.json``, keep working with an install credential.

``GET /settings`` refuses the same two credentials and org members: core-api
cannot see the enterprise permissions that decide which settings groups a
member may read, so it shows a member none. The dashboard reads settings through
admin-api, which narrows them per permission.

A settings change records the credential that made it, in two nullable columns
of ``organization_settings_audit``: ``actor_kind`` always, and
``actor_credential_id`` when core-api knows which credential it was (the
gateway's ``X-Caura-Credential-ID`` behind the gateway secret, or the configured
key core-api matched itself).

Requests here go through the real auth paths, headers and all, rather than an
overridden ``get_auth_context``, so the header plumbing is under test too.
"""

from __future__ import annotations

import json
import pathlib
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from core_api.auth import get_auth_context
from core_api.config import settings
from tests._legacy_contracts import LEGACY_API_KEY_FIELD
from tests.conftest import uid

REFUSED = "CREDENTIAL_KIND_FORBIDDEN"
GATEWAY_SECRET = "test-gateway-secret"
CAURA_KEY = "test-caura-key"
ADMIN_KEY = "test-admin-key"  # tests/conftest.py's ADMIN_API_KEY
INSTALL_UUID = "5d4b0c9e-6f0a-4c43-9a5e-2f1f0f6b7a11"


@pytest.fixture
def gateway(monkeypatch):
    """Auth Path 4 behind a gateway secret, the way the enterprise gateway calls core-api."""
    monkeypatch.setattr(settings, "is_standalone", False)
    monkeypatch.setattr(settings, LEGACY_API_KEY_FIELD, "")
    monkeypatch.setattr(settings, "gateway_shared_secret", GATEWAY_SECRET)


def _gateway_headers(tenant: str, **extra: str) -> dict[str, str]:
    return {"X-Gateway-Secret": GATEWAY_SECRET, "X-Tenant-ID": tenant, **extra}


# What the gateway's /validate stamps for each credential (platform-auth-api).
def _install_credential(tenant: str) -> dict[str, str]:
    return _gateway_headers(
        tenant,
        **{
            "X-Caura-Credential-Kind": "install_credential",
            "X-Caura-Credential-ID": "cred-install",
            "X-Install-UUID": INSTALL_UUID,
            "X-Capabilities": "read,write",
        },
    )


def _cross_tenant_credential(tenant: str) -> dict[str, str]:
    return _gateway_headers(
        tenant,
        **{
            "X-Caura-Credential-Kind": "cross_tenant",
            "X-Caura-Credential-ID": "cred-cross",
            "X-Readable-Tenant-IDs": f"{tenant},{tenant}-source",
            "X-Capabilities": "read,write",
        },
    )


def _widened_tenant_key(tenant: str) -> dict[str, str]:
    """A tenant key minted with source tenants before admin-api refused that shape."""
    return _gateway_headers(
        tenant,
        **{
            "X-Caura-Credential-Kind": "user_api_key",
            "X-Caura-Credential-ID": "cred-widened",
            "X-Readable-Tenant-IDs": f"{tenant},{tenant}-source",
        },
    )


def _tenant_key(tenant: str) -> dict[str, str]:
    return _gateway_headers(
        tenant,
        **{
            "X-Caura-Credential-Kind": "user_api_key",
            "X-Caura-Credential-ID": "cred-tenant",
        },
    )


def _org_admin_session(tenant: str) -> dict[str, str]:
    return _gateway_headers(
        tenant, **{"X-Org-Role": "admin", "X-User-ID": "person-admin"}
    )


def _org_member_session(tenant: str) -> dict[str, str]:
    return _gateway_headers(
        tenant, **{"X-Org-Role": "member", "X-User-ID": "person-member"}
    )


REFUSED_CREDENTIALS = [
    pytest.param(_install_credential, id="install-credential"),
    pytest.param(_cross_tenant_credential, id="cross-tenant-credential"),
    pytest.param(_widened_tenant_key, id="tenant-key-reading-other-tenants"),
]
ADMIN_CREDENTIALS = [
    pytest.param(_tenant_key, id="tenant-key"),
    pytest.param(_org_admin_session, id="org-admin-session"),
]


def _route(name, method, path, *, params=None, body=None):
    """One admin-plane request. ``{t}`` in the path, params or body is the tenant."""
    return pytest.param(method, path, params or {}, body, id=name)


_NODE = "0b7e3f39-3c5e-4a3e-9f1b-6c0d2b6f0a42"
ADMIN_ROUTES = [
    _route(
        "put-settings",
        "PUT",
        "/api/v1/settings",
        params={"tenant_id": "{t}"},
        body={"agents": {"require_agent_approval": False}},
    ),
    _route(
        "agent-trust",
        "PATCH",
        "/api/v1/agents/agent-x/trust",
        params={"tenant_id": "{t}"},
        body={"trust_level": 3},
    ),
    _route(
        "agent-fleet",
        "PATCH",
        "/api/v1/agents/agent-x/fleet",
        params={"tenant_id": "{t}"},
        body={"fleet_id": "fleet-x"},
    ),
    _route(
        "agent-delete", "DELETE", "/api/v1/agents/agent-x", params={"tenant_id": "{t}"}
    ),
    _route(
        "fleet-create",
        "POST",
        "/api/v1/fleet",
        body={"tenant_id": "{t}", "fleet_id": "fleet-x"},
    ),
    _route(
        "fleet-purge",
        "POST",
        "/api/v1/fleet/fleet-x/purge",
        params={"tenant_id": "{t}"},
    ),
    _route(
        "fleet-delete", "DELETE", "/api/v1/fleet/fleet-x", params={"tenant_id": "{t}"}
    ),
    _route(
        "fleet-command",
        "POST",
        "/api/v1/fleet/commands",
        body={"tenant_id": "{t}", "node_id": _NODE, "command": "ping", "payload": {}},
    ),
    _route(
        "node-release",
        "POST",
        f"/api/v1/fleet/nodes/{_NODE}/release",
        params={"tenant_id": "{t}"},
    ),
    _route(
        "delete-tenant-memories",
        "DELETE",
        "/api/v1/memories",
        params={"tenant_id": "{t}", "confirm_scope": "tenant"},
    ),
    _route(
        "delete-fleet-memories",
        "DELETE",
        "/api/v1/memories",
        params={"tenant_id": "{t}", "fleet_id": "fleet-x"},
    ),
    _route(
        "bulk-delete-memories",
        "POST",
        "/api/v1/memories/bulk-delete",
        body={"tenant_id": "{t}", "ids": [str(uuid.UUID(int=1))]},
    ),
    _route(
        "document-delete",
        "DELETE",
        "/api/v1/documents/doc-x",
        params={"tenant_id": "{t}", "collection": "notes"},
    ),
    _route(
        "ingest-undo", "POST", "/api/v1/ingest/undo/run-x", params={"tenant_id": "{t}"}
    ),
]


def _fill(value, tenant: str):
    if isinstance(value, str):
        return value.replace("{t}", tenant)
    if isinstance(value, dict):
        return {k: _fill(v, tenant) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, tenant) for v in value]
    return value


async def _send(client, method, path, params, body, headers, tenant):
    return await client.request(
        method,
        path,
        params=_fill(params, tenant),
        json=_fill(body, tenant) if body is not None else None,
        headers=headers,
    )


@pytest.mark.parametrize("credential", REFUSED_CREDENTIALS)
@pytest.mark.parametrize(("method", "path", "params", "body"), ADMIN_ROUTES)
async def test_machine_credentials_cannot_run_the_admin_plane(
    client, gateway, tenant_id, credential, method, path, params, body
):
    resp = await _send(
        client, method, path, params, body, credential(tenant_id), tenant_id
    )

    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == REFUSED, resp.text


@pytest.mark.parametrize("credential", ADMIN_CREDENTIALS)
@pytest.mark.parametrize(("method", "path", "params", "body"), ADMIN_ROUTES)
async def test_tenant_admins_keep_the_admin_plane(
    client, gateway, tenant_id, credential, method, path, params, body
):
    """Tenant key = admin (the OSS contract), and a signed-in org admin.

    The targets don't exist in this fresh tenant, so some routes answer 404:
    what matters is that the request got past the credential gates.
    """
    resp = await _send(
        client, method, path, params, body, credential(tenant_id), tenant_id
    )

    assert resp.status_code in (200, 201, 204, 404), resp.text


async def test_the_admin_key_and_the_shared_caura_key_keep_the_admin_plane(
    client, monkeypatch, tenant_id
):
    """The OSS credentials carry no kind and stay admins of their tenant."""
    monkeypatch.setattr(settings, "is_standalone", False)
    monkeypatch.setattr(settings, LEGACY_API_KEY_FIELD, CAURA_KEY)
    body = {"agents": {"require_agent_approval": True}}

    shared = await client.put(
        "/api/v1/settings",
        params={"tenant_id": tenant_id},
        json=body,
        headers={"X-API-Key": CAURA_KEY, "X-Tenant-ID": tenant_id},
    )
    admin = await client.post(
        "/api/v1/fleet/fleet-x/purge",
        params={"tenant_id": tenant_id},
        headers={"X-API-Key": ADMIN_KEY},
    )

    assert shared.status_code == 200, shared.text
    assert admin.status_code == 200, admin.text


# ---------------------------------------------------------------------------
# GET /settings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("credential", "code"),
    [
        pytest.param(_install_credential, REFUSED, id="install-credential"),
        pytest.param(_cross_tenant_credential, REFUSED, id="cross-tenant-credential"),
        pytest.param(
            _org_member_session, "ORG_ADMIN_REQUIRED", id="org-member-session"
        ),
    ],
)
async def test_reading_settings_needs_a_tenant_admin(
    client, gateway, tenant_id, credential, code
):
    resp = await client.get(
        "/api/v1/settings",
        params={"tenant_id": tenant_id},
        headers=credential(tenant_id),
    )

    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == code, resp.text


@pytest.mark.parametrize("credential", ADMIN_CREDENTIALS)
async def test_tenant_admins_still_read_settings(
    client, gateway, tenant_id, credential
):
    resp = await client.get(
        "/api/v1/settings",
        params={"tenant_id": tenant_id},
        headers=credential(tenant_id),
    )

    assert resp.status_code == 200, resp.text
    assert "agents" in resp.json()


# ---------------------------------------------------------------------------
# The Broker's own calls keep working with its install credential
# ---------------------------------------------------------------------------

_BROKER_CONTRACT = (
    pathlib.Path(__file__).resolve().parent.parent / "core-api" / "openapi.broker.json"
)
_BROKER_OPERATIONS_EXERCISED = {
    ("GET", "/api/v1/health"),
    ("GET", "/api/v1/version"),
    ("POST", "/api/v1/memories/bulk"),
    ("GET", "/api/v1/memories"),
    ("GET", "/api/v1/memories/{memory_id}"),
    ("PATCH", "/api/v1/memories/{memory_id}"),
    ("POST", "/api/v1/search"),
    ("GET", "/api/v1/keystones"),
    ("DELETE", "/api/v1/memories/{memory_id}"),
}


def test_the_broker_flow_covers_every_broker_operation():
    """The flow below must track the frozen contract, or it stops proving anything."""
    contract = json.loads(_BROKER_CONTRACT.read_text())
    operations = {
        (method.upper(), path)
        for path, item in contract["paths"].items()
        for method in item
        if method in ("get", "post", "patch", "delete", "put")
    }
    assert operations == _BROKER_OPERATIONS_EXERCISED


async def test_the_broker_calls_still_work_with_an_install_credential(
    client, gateway, tenant_id
):
    headers = _install_credential(tenant_id)
    q = {"tenant_id": tenant_id}

    for path in ("/api/v1/health", "/api/v1/version"):
        resp = await client.get(path, headers=headers)
        assert resp.status_code == 200, (path, resp.text)

    written = await client.post(
        "/api/v1/memories/bulk",
        json={
            "tenant_id": tenant_id,
            "items": [{"content": f"The broker kept this note for run {uid()}."}],
        },
        headers=headers,
    )
    assert written.status_code == 200, written.text
    memory_id = written.json()["results"][0]["id"]

    listed = await client.get(
        "/api/v1/memories", params={**q, "limit": 10}, headers=headers
    )
    assert listed.status_code == 200, listed.text

    got = await client.get(f"/api/v1/memories/{memory_id}", params=q, headers=headers)
    assert got.status_code == 200, got.text

    patched = await client.patch(
        f"/api/v1/memories/{memory_id}",
        params=q,
        json={"metadata": {"source": "broker-test"}},
        headers=headers,
    )
    assert patched.status_code == 200, patched.text

    searched = await client.post(
        "/api/v1/search",
        json={"tenant_id": tenant_id, "query": "broker note"},
        headers=headers,
    )
    assert searched.status_code == 200, searched.text

    keystones = await client.get(
        "/api/v1/keystones", params={**q, "envelope": "true"}, headers=headers
    )
    assert keystones.status_code == 200, keystones.text

    deleted = await client.delete(
        f"/api/v1/memories/{memory_id}", params=q, headers=headers
    )
    assert deleted.status_code == 204, deleted.text


# ---------------------------------------------------------------------------
# The settings audit names the credential behind a change
# ---------------------------------------------------------------------------


async def _last_settings_audit(engine, tenant: str) -> dict:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT * FROM organization_settings_audit "
                    "WHERE org_id = :org ORDER BY id DESC LIMIT 1"
                ),
                {"org": tenant},
            )
        ).one()
    return dict(row._mapping)


async def _put_settings(client, tenant: str, headers: dict[str, str], **params: str):
    resp = await client.put(
        "/api/v1/settings",
        params={"tenant_id": tenant, **params},
        json={"agents": {"require_agent_approval": True}},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text


async def test_a_gateway_key_change_records_its_credential(
    client, gateway, tenant_id, _engine
):
    await _put_settings(client, tenant_id, _tenant_key(tenant_id))

    row = await _last_settings_audit(_engine, tenant_id)
    assert row["changed_by"] is None
    assert row.get("actor_kind") == "user_api_key"
    assert row.get("actor_credential_id") == "cred-tenant"


async def test_a_signed_in_admin_change_records_the_person(
    client, gateway, tenant_id, _engine
):
    await _put_settings(client, tenant_id, _org_admin_session(tenant_id))

    row = await _last_settings_audit(_engine, tenant_id)
    assert row["changed_by"] == "person-admin"
    assert row.get("actor_kind") == "person"
    assert row.get("actor_credential_id") is None


async def test_the_credential_id_is_trusted_only_behind_the_gateway_secret(
    client, monkeypatch, tenant_id, _engine
):
    """With no gateway secret the headers are the caller's own claim: no id is recorded."""
    monkeypatch.setattr(settings, "is_standalone", False)
    monkeypatch.setattr(settings, LEGACY_API_KEY_FIELD, "")
    monkeypatch.setattr(settings, "gateway_shared_secret", None)
    await _put_settings(
        client,
        tenant_id,
        {
            "X-Tenant-ID": tenant_id,
            "X-Caura-Credential-Kind": "user_api_key",
            "X-Caura-Credential-ID": "claimed-credential",
        },
    )

    row = await _last_settings_audit(_engine, tenant_id)
    assert row["changed_by"] is None
    assert row.get("actor_kind") == "tenant_key"
    assert row.get("actor_credential_id") is None


async def test_a_shared_caura_key_change_records_that_key(
    client, monkeypatch, tenant_id, _engine
):
    monkeypatch.setattr(settings, "is_standalone", False)
    monkeypatch.setattr(settings, LEGACY_API_KEY_FIELD, CAURA_KEY)
    await _put_settings(
        client, tenant_id, {"X-API-Key": CAURA_KEY, "X-Tenant-ID": tenant_id}
    )

    row = await _last_settings_audit(_engine, tenant_id)
    assert row["changed_by"] is None
    assert row.get("actor_kind") == "tenant_key"
    assert row.get("actor_credential_id") == "config:caura_api_key"


async def test_an_admin_key_change_records_that_key_and_the_named_person(
    client, monkeypatch, tenant_id, _engine
):
    """The enterprise admin-api proxy: the admin key, naming the person in X-Changed-By."""
    monkeypatch.setattr(settings, "is_standalone", False)
    await _put_settings(
        client,
        tenant_id,
        {"X-API-Key": ADMIN_KEY, "X-Changed-By": "person-via-admin-api"},
    )

    row = await _last_settings_audit(_engine, tenant_id)
    assert row["changed_by"] == "person-via-admin-api"
    assert row.get("actor_kind") == "admin_key"
    assert row.get("actor_credential_id") == "config:admin_api_key"


@pytest.mark.parametrize(
    ("standalone", "key", "headers", "expected"),
    [
        pytest.param(False, ADMIN_KEY, {}, "config:admin_api_key", id="admin-key"),
        pytest.param(
            True, CAURA_KEY, {}, "config:caura_api_key", id="caura-key-standalone"
        ),
        pytest.param(
            False,
            CAURA_KEY,
            {"x-tenant-id": "tenant-a"},
            "config:caura_api_key",
            id="caura-key-with-tenant",
        ),
        pytest.param(
            False, CAURA_KEY, {}, "config:caura_api_key", id="caura-key-no-tenant"
        ),
    ],
)
async def test_a_key_core_api_matched_itself_is_named_by_its_setting(
    monkeypatch, standalone, key, headers, expected
):
    """Every path that matches a configured key names the setting, never the key."""
    monkeypatch.setattr(settings, "is_standalone", standalone)
    monkeypatch.setattr(settings, LEGACY_API_KEY_FIELD, CAURA_KEY)
    ctx = await get_auth_context(SimpleNamespace(headers=headers), key=key)

    assert ctx.credential_id == expected
    assert key not in ctx.credential_id

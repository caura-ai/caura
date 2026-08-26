"""WT-4, second half — tenant resolution on the STM routes.

``core_api.routes.stm`` carried its own private ``_require_tenant``, a
copy of the skills-inbox helper with the same flaw: every
``AuthContext`` without a ``tenant_id`` was treated as unauthenticated.
The OSS admin path (``core_api.auth`` Path 1) deliberately builds
``AuthContext(tenant_id=None, is_admin=True)`` — so all five STM
operations answered the **admin API key** with a 401, and any
``?tenant_id=`` the operator passed was silently dropped because no STM
route declared the parameter.

#987 fixed the inbox copy and left this one out of scope. These tests
pin the same three-case resolution here:

- tenant-scoped credential → its own tenant; a conflicting explicit
  tenant is 403 ``TENANT_MISMATCH``;
- admin credential → the tenant it names; naming none is **400**, never
  401;
- neither → the original 401.

Pure unit tests: the app is the STM router alone (no
``StandaloneTenantMiddleware``, which would otherwise inject a
``tenant_id`` into the query string of every request and mask the
admin-without-a-tenant case), and the STM service + write-path seams
are patched at the module boundary. No DB.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core_api.auth import AuthContext, get_auth_context
from core_api.routes import stm

pytestmark = pytest.mark.unit

TENANT = "t-acme"
OTHER = "t-other"
AGENT = "agent-1"
FLEET = "fleet-1"


@pytest.fixture
def stm_on(monkeypatch):
    """Enable STM. Every route calls ``_check_stm_enabled`` first, and it
    422s by default — so without this the tenant branch is unreachable."""
    from core_api.config import settings

    monkeypatch.setattr(settings, "use_stm", True)


class Seams:
    """Records what the routes asked the service layer to do."""

    def __init__(self):
        self.read_notes: list[tuple] = []
        self.clear_notes: list[tuple] = []
        self.read_bulletin: list[tuple] = []
        self.clear_bulletin: list[tuple] = []
        self.promoted: list[dict] = []
        self.fleet_writes: list[tuple] = []
        self.metered: list[tuple] = []


@pytest.fixture
def seams(monkeypatch):
    s = Seams()

    async def _read_notes(tenant_id, agent_id, limit=50):
        s.read_notes.append((tenant_id, agent_id))
        return [{"content": "a note"}]

    async def _clear_notes(tenant_id, agent_id):
        s.clear_notes.append((tenant_id, agent_id))

    async def _read_bulletin(tenant_id, fleet_id, limit=100):
        s.read_bulletin.append((tenant_id, fleet_id))
        return [{"content": "an entry"}]

    async def _clear_bulletin(tenant_id, fleet_id):
        s.clear_bulletin.append((tenant_id, fleet_id))

    async def _promote(**kwargs):
        s.promoted.append(kwargs)
        return {"id": "m-1"}

    monkeypatch.setattr("core_api.services.stm_service.read_notes", _read_notes)
    monkeypatch.setattr("core_api.services.stm_service.clear_notes", _clear_notes)
    monkeypatch.setattr("core_api.services.stm_service.read_bulletin", _read_bulletin)
    monkeypatch.setattr("core_api.services.stm_service.clear_bulletin", _clear_bulletin)
    monkeypatch.setattr("core_api.services.stm_service.promote", _promote)

    # Promote's LTM write path (parity with POST /memories).
    class _Config:
        require_agent_approval = False

    async def _resolve_config(tenant_id):
        return _Config()

    async def _resolve_write_agent(chosen_agent_id, tenant_id, fleet_id, **kwargs):
        return {"trust_level": 2, "fleet_id": fleet_id}, chosen_agent_id

    async def _enforce_fleet_write(tenant_id, agent_id, fleet_id):
        s.fleet_writes.append((tenant_id, agent_id, fleet_id))

    async def _check_and_increment(tenant_id, kind):
        s.metered.append((tenant_id, kind))

    monkeypatch.setattr(
        "core_api.services.organization_settings.resolve_config", _resolve_config
    )
    monkeypatch.setattr(stm, "resolve_write_agent", _resolve_write_agent)
    monkeypatch.setattr(stm, "enforce_fleet_write", _enforce_fleet_write)
    monkeypatch.setattr(stm, "check_and_increment", _check_and_increment)
    return s


def make_client(
    *,
    # ``tenant_id=None`` + ``is_admin=True`` reproduces the OSS admin
    # credential (auth Path 1) — the WT-4 shape.
    tenant_id: str | None = TENANT,
    is_admin: bool = False,
) -> AsyncClient:
    app = FastAPI()
    app.include_router(stm.router, prefix="/api/v1")
    auth = AuthContext(tenant_id=tenant_id, is_admin=is_admin)

    async def _auth_dep():
        return auth

    app.dependency_overrides[get_auth_context] = _auth_dep
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def admin_client() -> AsyncClient:
    return make_client(tenant_id=None, is_admin=True)


PROMOTE_BODY = {"agent_id": AGENT, "content": "a durable note"}


# ---------------------------------------------------------------------------
# Admin credential + an explicit tenant — the operation it was refused
# ---------------------------------------------------------------------------


async def test_admin_key_with_explicit_tenant_reads(stm_on, seams):
    async with admin_client() as client:
        notes = await client.get(
            "/api/v1/stm/notes", params={"agent_id": AGENT, "tenant_id": TENANT}
        )
        bulletin = await client.get(
            "/api/v1/stm/bulletin", params={"fleet_id": FLEET, "tenant_id": TENANT}
        )
    assert notes.status_code == 200, notes.text
    assert notes.json()["tenant_id"] == TENANT
    assert bulletin.status_code == 200, bulletin.text
    assert bulletin.json()["tenant_id"] == TENANT
    assert seams.read_notes == [(TENANT, AGENT)]
    assert seams.read_bulletin == [(TENANT, FLEET)]


async def test_admin_key_with_explicit_tenant_can_clear(stm_on, seams):
    async with admin_client() as client:
        notes = await client.delete(
            "/api/v1/stm/notes", params={"agent_id": AGENT, "tenant_id": TENANT}
        )
        bulletin = await client.delete(
            "/api/v1/stm/bulletin", params={"fleet_id": FLEET, "tenant_id": TENANT}
        )
    assert notes.status_code == 200, notes.text
    assert bulletin.status_code == 200, bulletin.text
    assert seams.clear_notes == [(TENANT, AGENT)]
    assert seams.clear_bulletin == [(TENANT, FLEET)]


async def test_admin_key_with_explicit_tenant_can_promote(stm_on, seams):
    async with admin_client() as client:
        r = await client.post(
            "/api/v1/stm/promote",
            params={"tenant_id": TENANT},
            json=PROMOTE_BODY,
        )
    assert r.status_code == 200, r.text
    assert seams.promoted[0]["tenant_id"] == TENANT
    # The admin carve-out below the resolution (``if auth.tenant_id:``) was
    # dead code until now: fleet-write enforcement and metering stay off for
    # an admin credential, exactly as POST /memories does.
    assert seams.fleet_writes == []
    assert seams.metered == []


# ---------------------------------------------------------------------------
# Admin credential naming no tenant — 400, never 401
# ---------------------------------------------------------------------------


async def test_admin_key_without_tenant_is_400_not_401(stm_on, seams):
    """The WT-4 regression proper: the admin key IS authenticated, so
    failing to say WHICH tenant is a request problem (400)."""
    async with admin_client() as client:
        responses = {
            "get_notes": await client.get(
                "/api/v1/stm/notes", params={"agent_id": AGENT}
            ),
            "clear_notes": await client.delete(
                "/api/v1/stm/notes", params={"agent_id": AGENT}
            ),
            "get_bulletin": await client.get(
                "/api/v1/stm/bulletin", params={"fleet_id": FLEET}
            ),
            "clear_bulletin": await client.delete(
                "/api/v1/stm/bulletin", params={"fleet_id": FLEET}
            ),
            "promote": await client.post("/api/v1/stm/promote", json=PROMOTE_BODY),
        }
    for name, r in responses.items():
        assert r.status_code == 400, f"{name}: {r.status_code} {r.text}"
        assert "tenant" in r.json()["detail"], name
    # Nothing reached the service layer.
    assert seams.read_notes == seams.clear_notes == []
    assert seams.read_bulletin == seams.clear_bulletin == []
    assert seams.promoted == []


# ---------------------------------------------------------------------------
# Tenant-scoped credential
# ---------------------------------------------------------------------------


async def test_tenant_key_with_conflicting_tenant_is_403(stm_on, seams):
    """A tenant-scoped key must not act on ANOTHER tenant via ?tenant_id=."""
    async with make_client() as client:
        read = await client.get(
            "/api/v1/stm/notes", params={"agent_id": AGENT, "tenant_id": OTHER}
        )
        clear = await client.delete(
            "/api/v1/stm/bulletin", params={"fleet_id": FLEET, "tenant_id": OTHER}
        )
        promote = await client.post(
            "/api/v1/stm/promote", params={"tenant_id": OTHER}, json=PROMOTE_BODY
        )
    for r in (read, clear, promote):
        assert r.status_code == 403, r.text
        detail = r.json()["detail"]
        # The machine-readable prefix is the contract clients branch on.
        assert detail.startswith("TENANT_MISMATCH")
        # The prose discloses NEITHER id: not the credential's own tenant
        # (a binding an embedded/shared key's holder may never have been
        # told) and not the caller-supplied one (attacker-controlled input
        # reflected into a body that also lands in logs).
        assert TENANT not in detail, detail
        assert OTHER not in detail, detail
    assert seams.read_notes == []
    assert seams.clear_bulletin == []
    assert seams.promoted == []


async def test_tenant_key_with_matching_tenant_still_works(stm_on, seams):
    async with make_client() as client:
        r = await client.get(
            "/api/v1/stm/notes", params={"agent_id": AGENT, "tenant_id": TENANT}
        )
    assert r.status_code == 200, r.text
    assert seams.read_notes == [(TENANT, AGENT)]


async def test_tenant_key_without_param_unchanged(stm_on, seams):
    """No ?tenant_id= → the key's own tenant wins, exactly as before."""
    async with make_client() as client:
        read = await client.get("/api/v1/stm/notes", params={"agent_id": AGENT})
        promote = await client.post("/api/v1/stm/promote", json=PROMOTE_BODY)
    assert read.status_code == 200, read.text
    assert read.json()["tenant_id"] == TENANT
    assert promote.status_code == 200, promote.text
    assert seams.promoted[0]["tenant_id"] == TENANT
    # A tenant-scoped credential still pays fleet enforcement + metering.
    assert seams.fleet_writes == [(TENANT, AGENT, None)]
    assert seams.metered == [(TENANT, "write")]


# ---------------------------------------------------------------------------
# Neither — the 401 that was always correct
# ---------------------------------------------------------------------------


async def test_no_tenant_and_no_admin_still_401(stm_on, seams):
    """Genuinely unauthenticated bootstrap context keeps the 401."""
    async with make_client(tenant_id=None, is_admin=False) as client:
        read = await client.get("/api/v1/stm/notes", params={"agent_id": AGENT})
        promote = await client.post("/api/v1/stm/promote", json=PROMOTE_BODY)
    for r in (read, promote):
        assert r.status_code == 401, r.text
        assert r.json()["detail"].startswith("UNAUTHENTICATED")


# ---------------------------------------------------------------------------
# The published contract — the parameter description is the only doc a
# caller gets (STM has no markdown page by standing decision).
# ---------------------------------------------------------------------------


async def test_tenant_selector_is_documented_on_every_stm_operation():
    from core_api.app import app

    schema = app.openapi()
    operations = [
        (path, method, op)
        for path, item in schema["paths"].items()
        if path.startswith("/api/v1/stm/")
        for method, op in item.items()
    ]
    assert operations, "no STM operations in the schema — did the routes move?"
    for path, method, op in operations:
        params = {p["name"]: p for p in op.get("parameters", [])}
        assert "tenant_id" in params, f"{method.upper()} {path} has no tenant selector"
        description = (params["tenant_id"].get("description") or "").lower()
        # An admin caller must learn from the docs BOTH that the parameter
        # is theirs to pass and what omitting it costs.
        assert "admin" in description, f"{method.upper()} {path}"
        assert "400" in description, f"{method.upper()} {path}"

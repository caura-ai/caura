"""X-Org-Role plumbing on the gateway auth path (Skills Inbox A2).

The enterprise gateway's /_auth subrequest resolves the signed-in
user's org-membership role (``org_members``: admin | member) and
forwards it as ``X-Org-Role``; Path 4 of ``get_auth_context`` reads it
into ``AuthContext.org_role`` so admin gates on operator surfaces
(Skills Inbox actions) work for session/JWT principals.

Pins:
- admin / member parse (with case + whitespace normalization),
- the allowlist — values outside the org-membership model are dropped
  (a smuggled third role must never reach ``== "admin"`` gates),
- absent header → ``org_role is None`` (pre-rollout gateways),
- the CAURA_API_KEY path (Path 2) ignores the header — API-key
  callers must NOT gain inbox-action rights from a spoofable header,
- end-to-end: a gateway-shaped request opens the Skills Inbox admin
  gate with the role and is 403'd without it.
"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core_api import auth as auth_mod
from core_api.routes import skills_inbox as si

pytestmark = pytest.mark.unit


class _Req:
    def __init__(self, headers):
        self.headers = headers


async def _path4_ctx(monkeypatch, headers):
    """Resolve get_auth_context down Path 4 (gateway header trust)."""
    monkeypatch.setattr(auth_mod.settings, "gateway_shared_secret", None)
    monkeypatch.setattr(auth_mod.settings, "is_standalone", False)
    monkeypatch.setattr(auth_mod.settings, "memclaw_api_key", None)
    monkeypatch.setattr(auth_mod, "get_admin_key", lambda: None)

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(auth_mod, "_block_if_suppressed", _noop)
    monkeypatch.setattr(auth_mod, "_block_if_any_readable_suppressed", _noop)
    return await auth_mod.get_auth_context(_Req(headers), key=None)


async def test_path4_reads_admin_and_member(monkeypatch):
    ctx = await _path4_ctx(monkeypatch, {"x-tenant-id": "t", "x-org-role": "admin"})
    assert ctx.org_role == "admin"
    ctx = await _path4_ctx(monkeypatch, {"x-tenant-id": "t", "x-org-role": "member"})
    assert ctx.org_role == "member"


async def test_path4_normalizes_case_and_whitespace(monkeypatch):
    ctx = await _path4_ctx(monkeypatch, {"x-tenant-id": "t", "x-org-role": " Admin "})
    assert ctx.org_role == "admin"


@pytest.mark.parametrize("smuggled", ["owner", "superadmin", "admin,member", "1", ""])
async def test_path4_drops_unknown_roles(monkeypatch, smuggled):
    ctx = await _path4_ctx(monkeypatch, {"x-tenant-id": "t", "x-org-role": smuggled})
    assert ctx.org_role is None


async def test_path4_absent_header_is_none(monkeypatch):
    """Pre-rollout gateways don't send the header — behavior unchanged."""
    ctx = await _path4_ctx(monkeypatch, {"x-tenant-id": "t"})
    assert ctx.org_role is None


async def test_memclaw_key_path_ignores_org_role_header(monkeypatch):
    """Path 2 (API-key callers) must not gain a role from the header —
    approving skills stays a human decision; agent keys can't act on
    admin-only routes by self-asserting X-Org-Role."""
    monkeypatch.setattr(auth_mod.settings, "gateway_shared_secret", None)
    monkeypatch.setattr(auth_mod.settings, "is_standalone", False)
    monkeypatch.setattr(auth_mod.settings, "memclaw_api_key", "sekrit")
    monkeypatch.setattr(auth_mod, "get_admin_key", lambda: None)

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(auth_mod, "_block_if_suppressed", _noop)
    monkeypatch.setattr(auth_mod, "_block_if_any_readable_suppressed", _noop)
    ctx = await auth_mod.get_auth_context(
        _Req({"x-tenant-id": "t", "x-org-role": "admin"}), key="sekrit"
    )
    assert ctx.tenant_id == "t"
    assert ctx.org_role is None


# ---------------------------------------------------------------------------
# End-to-end: gateway-shaped request → Skills Inbox admin gate
# ---------------------------------------------------------------------------


@pytest.fixture
def inbox_app(monkeypatch):
    """Skills Inbox router behind the REAL get_auth_context, resolved
    down Path 4, with the route's service seams stubbed."""
    monkeypatch.setattr(auth_mod.settings, "gateway_shared_secret", None)
    monkeypatch.setattr(auth_mod.settings, "is_standalone", False)
    monkeypatch.setattr(auth_mod.settings, "memclaw_api_key", None)
    monkeypatch.setattr(auth_mod, "get_admin_key", lambda: None)

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(auth_mod, "_block_if_suppressed", _noop)
    monkeypatch.setattr(auth_mod, "_block_if_any_readable_suppressed", _noop)

    async def raw_settings(tenant_id):
        return {"skills_factory": {"enabled": True}}

    async def display_settings(tenant_id):
        return {"skills_factory": {"enabled": True}}

    monkeypatch.setattr(si, "get_raw_settings", raw_settings)
    monkeypatch.setattr(si, "get_settings_for_display", display_settings)
    monkeypatch.setattr(si, "log_action", _noop)

    staged_doc = {
        "doc_id": "forge/s1",
        "fleet_id": None,
        "data": {"status": "staged", "content_hash": "sha256:x"},
    }

    class _Storage:
        async def get_document(self, *, tenant_id, collection, doc_id):
            return staged_doc

        async def upsert_document(self, payload):
            return None

    monkeypatch.setattr(si, "get_storage_client", lambda: _Storage())

    app = FastAPI()
    app.include_router(si.router, prefix="/api/v1")
    return app


async def test_gateway_admin_header_opens_inbox_actions(inbox_app):
    transport = ASGITransport(app=inbox_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/v1/skills-inbox/forge/s1/defer",
            headers={"x-tenant-id": "t", "x-org-role": "admin"},
        )
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("role_headers", [{}, {"x-org-role": "member"}])
async def test_gateway_non_admin_still_403(inbox_app, role_headers):
    transport = ASGITransport(app=inbox_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/v1/skills-inbox/forge/s1/defer",
            headers={"x-tenant-id": "t", **role_headers},
        )
    assert r.status_code == 403, r.text
    assert r.json()["detail"].startswith("SKILLS_INBOX_FORBIDDEN")

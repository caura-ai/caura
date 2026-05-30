"""Perimeter gate: the header-trust auth path requires X-Gateway-Secret.

CRITICAL-1 mitigation — core-api is publicly reachable on its run.app URL and
Path 4 trusts X-Tenant-ID with no credential. When GATEWAY_SHARED_SECRET is
configured, a request must carry the gateway-injected X-Gateway-Secret, so a
direct (gateway-bypassing) caller can't impersonate a tenant by setting the
identity header itself. Unset = no-op (OSS/standalone/dev).
"""

import pytest
from fastapi import HTTPException

from core_api import auth as auth_mod


class _Req:
    def __init__(self, headers):
        self.headers = headers


async def _ctx(monkeypatch, headers, *, secret):
    monkeypatch.setattr(auth_mod.settings, "gateway_shared_secret", secret)
    monkeypatch.setattr(auth_mod.settings, "is_standalone", False)
    monkeypatch.setattr(auth_mod.settings, "memclaw_api_key", None)
    monkeypatch.setattr(auth_mod, "get_admin_key", lambda: None)

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(auth_mod, "_block_if_suppressed", _noop)
    monkeypatch.setattr(auth_mod, "_block_if_any_readable_suppressed", _noop)
    return await auth_mod.get_auth_context(_Req(headers), key=None)


@pytest.mark.unit
async def test_path4_requires_gateway_secret_when_configured(monkeypatch):
    # missing header → 401
    with pytest.raises(HTTPException) as e:
        await _ctx(monkeypatch, {"x-tenant-id": "t"}, secret="topsecret")
    assert e.value.status_code == 401

    # wrong secret → 401
    with pytest.raises(HTTPException) as e:
        await _ctx(
            monkeypatch,
            {"x-tenant-id": "t", "x-gateway-secret": "nope"},
            secret="topsecret",
        )
    assert e.value.status_code == 401

    # correct secret → resolves the tenant
    ctx = await _ctx(
        monkeypatch,
        {"x-tenant-id": "t", "x-gateway-secret": "topsecret"},
        secret="topsecret",
    )
    assert ctx.tenant_id == "t"


@pytest.mark.unit
async def test_path4_noop_when_secret_unset(monkeypatch):
    # No shared secret configured (OSS/dev) → X-Tenant-ID alone still works.
    ctx = await _ctx(monkeypatch, {"x-tenant-id": "t"}, secret=None)
    assert ctx.tenant_id == "t"

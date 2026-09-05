"""Unit tests for the MCP middleware perimeter (gateway-secret check),
home-tenant prepend on the readable set, and gateway-only trust of the
identity headers.

The audit found ``MCPAuthMiddleware`` trusting ``X-Tenant-ID`` /
``X-Agent-ID`` / ``X-Readable-Tenant-IDs`` / ``X-Capabilities`` verbatim
with no ``X-Gateway-Secret`` validation — while REST ``get_auth_context``
refuses the header-trust path unless the gateway secret matches. Anyone
who could reach core-api's URL at ``/mcp`` directly could impersonate any
tenant. These tests pin the contract:

- secret configured + missing/wrong header → 401, downstream app never runs
- secret configured + correct header → request proceeds, tenant honored
- secret NOT configured (OSS / standalone / dev) → header trust unchanged
- readable set gets the home tenant prepended (parity with REST)
- identity headers are ignored on the non-gateway (direct) paths
"""

from __future__ import annotations

import json

import pytest

from core_api import mcp_server
from core_api.config import settings

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.fixture(autouse=True)
def _reset_mcp_context_vars():
    """The session-scoped event loop shares one context across tests —
    leave the middleware's identity vars clean so values set here can't
    bleed into later tests in the run."""
    yield
    mcp_server._tenant_id_var.set(mcp_server._UNAUTH)
    mcp_server._agent_id_var.set(None)
    mcp_server._readable_tenant_ids_var.set(None)
    mcp_server._scopes_var.set(None)
    mcp_server._via_gateway_var.set(False)
    mcp_server._org_read_only_var.set(False)


# The settings attribute is the RESOLVED dual-read field (CAURA_API_KEY
# falling back to the legacy env var), which is the one ``auth.py`` Path 2
# reads and therefore the one the MCP gate must read too. Named once here so
# the tests below carry the current name rather than nine legacy references.
_SHARED_KEY_FIELD = "memclaw_api_key"  # legacy-name-ok: rule 3 dual-read field


async def _call_middleware(headers: list[tuple[bytes, bytes]]):
    """Invoke ``MCPAuthMiddleware`` once with a synthetic ASGI scope.

    Returns ``(app_called, sends)`` so callers can assert both the
    context-var side effects and whether the request was short-circuited
    with a response before reaching the downstream app.
    """
    called = {"app": False}

    async def _noop_app(scope, receive, send):
        called["app"] = True

    async def _recv():
        return {"type": "http.request", "body": b"", "more_body": False}

    sends: list[dict] = []

    async def _send(message):
        sends.append(message)

    mw = mcp_server.MCPAuthMiddleware(_noop_app)
    scope = {"type": "http", "headers": headers}
    await mw(scope, _recv, _send)
    return called["app"], sends


# ---------------------------------------------------------------------------
# S1 — gateway-secret perimeter
# ---------------------------------------------------------------------------


async def test_tenant_header_rejected_without_secret(monkeypatch):
    monkeypatch.setattr(settings, "gateway_shared_secret", "s3cret")
    mcp_server._tenant_id_var.set(mcp_server._UNAUTH)

    app_called, sends = await _call_middleware(
        [
            (b"x-tenant-id", b"victim-tenant"),
            (b"x-capabilities", b"read,write"),
        ]
    )

    assert app_called is False
    start = next(m for m in sends if m["type"] == "http.response.start")
    assert start["status"] == 401
    body = next(m for m in sends if m["type"] == "http.response.body")
    payload = json.loads(body["body"])
    assert payload["error"]["code"] == "UNAUTHORIZED"
    # The spoofed tenant must not have been honored.
    assert mcp_server._get_tenant() != "victim-tenant"


async def test_tenant_header_rejected_with_wrong_secret(monkeypatch):
    monkeypatch.setattr(settings, "gateway_shared_secret", "s3cret")
    mcp_server._tenant_id_var.set(mcp_server._UNAUTH)

    app_called, sends = await _call_middleware(
        [
            (b"x-tenant-id", b"victim-tenant"),
            (b"x-gateway-secret", b"guess"),
        ]
    )

    assert app_called is False
    start = next(m for m in sends if m["type"] == "http.response.start")
    assert start["status"] == 401
    assert mcp_server._get_tenant() != "victim-tenant"


async def test_tenant_header_honored_with_correct_secret(monkeypatch):
    monkeypatch.setattr(settings, "gateway_shared_secret", "s3cret")

    app_called, sends = await _call_middleware(
        [
            (b"x-tenant-id", b"tenant-A"),
            (b"x-gateway-secret", b"s3cret"),
        ]
    )

    assert app_called is True
    assert not any(m["type"] == "http.response.start" for m in sends)
    assert mcp_server._get_tenant() == "tenant-A"


async def test_tenant_header_honored_when_secret_unset(monkeypatch):
    """OSS / standalone / dev deployments don't configure the shared
    secret — the header-trust path must keep working there (no-op
    perimeter, same as REST)."""
    monkeypatch.setattr(settings, "gateway_shared_secret", None)

    app_called, _ = await _call_middleware([(b"x-tenant-id", b"tenant-A")])

    assert app_called is True
    assert mcp_server._get_tenant() == "tenant-A"


# ---------------------------------------------------------------------------
# M1 — home-tenant prepend on the readable set (parity with REST)
# ---------------------------------------------------------------------------


async def test_readable_set_prepends_home_tenant(monkeypatch):
    monkeypatch.setattr(settings, "gateway_shared_secret", None)
    mcp_server._readable_tenant_ids_var.set(None)

    await _call_middleware(
        [
            (b"x-tenant-id", b"tenant-A"),
            (b"x-readable-tenant-ids", b"tenant-B,tenant-C"),
        ]
    )
    assert mcp_server._get_readable_tenants() == ["tenant-A", "tenant-B", "tenant-C"]


async def test_readable_set_does_not_duplicate_home_tenant(monkeypatch):
    monkeypatch.setattr(settings, "gateway_shared_secret", None)
    mcp_server._readable_tenant_ids_var.set(None)

    await _call_middleware(
        [
            (b"x-tenant-id", b"tenant-A"),
            (b"x-readable-tenant-ids", b"tenant-A,tenant-B"),
        ]
    )
    assert mcp_server._get_readable_tenants() == ["tenant-A", "tenant-B"]


# ---------------------------------------------------------------------------
# Identity headers are gateway-only — ignored on the direct paths
# ---------------------------------------------------------------------------


async def test_identity_headers_ignored_without_tenant_header(monkeypatch):
    """On the direct (non-gateway) paths a client must not be able to
    self-assert a cross-tenant read set, an agent identity, or a
    capability set by sending the gateway headers itself."""
    monkeypatch.setattr(settings, "gateway_shared_secret", None)
    # Pin non-standalone so the unknown-key path resolves to _UNAUTH
    # instead of requiring init_standalone() (conftest sets IS_STANDALONE).
    monkeypatch.setattr(settings, "is_standalone", False)
    mcp_server._readable_tenant_ids_var.set(None)
    mcp_server._scopes_var.set(None)
    mcp_server._agent_id_var.set(None)

    await _call_middleware(
        [
            (b"x-api-key", b"some-key"),
            (b"x-agent-id", b"spoofed-agent"),
            (b"x-readable-tenant-ids", b"tenant-B,tenant-C"),
            (b"x-capabilities", b"read,write"),
        ]
    )

    assert mcp_server._get_agent_id() is None
    assert mcp_server._get_readable_tenants() == []
    assert mcp_server._get_scopes() is None


# ---------------------------------------------------------------------------
# S2 — plan-limit read-only mode (X-Org-Read-Only)
#
# The gateway computes "over plan" from the persisted counters and stamps this
# header; REST turns it into a 403 via ``AuthContext.is_read_only``. The MCP
# middleware did not read it at all before caura-ai/caura#1205, which is why
# these tests exist; it does now, and refuses on it when
# ``enforce_mcp_plan_limits`` is on. What is tested HERE is only that the value
# is trusted on the gateway path and not off it — the refusal itself lives in
# ``test_mcp_plan_limit_enforcement.py``.
#
# The risk here runs the OPPOSITE way to the identity headers above. There,
# self-assertion buys access; here, a direct caller simply OMITTING the header
# would clear its own read-only flag. Both are closed by the same rule: the
# value is honoured only on the gateway-verified path.
# ---------------------------------------------------------------------------


async def test_org_read_only_is_honored_on_the_gateway_path(monkeypatch):
    monkeypatch.setattr(settings, "gateway_shared_secret", None)
    await _call_middleware(
        [(b"x-tenant-id", b"tenant-A"), (b"x-org-read-only", b"true")]
    )
    assert mcp_server._is_org_read_only() is True


async def test_org_read_only_is_ignored_off_the_gateway_path(monkeypatch):
    """A direct caller must not be able to SET one either."""
    monkeypatch.setattr(settings, "gateway_shared_secret", None)
    monkeypatch.setattr(settings, "is_standalone", False)
    await _call_middleware([(b"x-api-key", b"some-key"), (b"x-org-read-only", b"true")])
    assert mcp_server._is_org_read_only() is False


async def test_org_read_only_does_not_bleed_between_requests(monkeypatch):
    """The billing-bypass case: a stale False would let an over-plan tenant
    through, a stale True would refuse a paying one. Both silent, so the var is
    assigned on every request rather than only when the header is present."""
    monkeypatch.setattr(settings, "gateway_shared_secret", None)
    await _call_middleware(
        [(b"x-tenant-id", b"tenant-A"), (b"x-org-read-only", b"true")]
    )
    assert mcp_server._is_org_read_only() is True

    await _call_middleware([(b"x-tenant-id", b"tenant-A")])
    assert mcp_server._is_org_read_only() is False


async def test_org_read_only_requires_the_gateway_secret_when_configured(monkeypatch):
    """Rejected before any context var is set — the perimeter runs first."""
    monkeypatch.setattr(settings, "gateway_shared_secret", "s3cret")
    app_called, _ = await _call_middleware(
        [(b"x-tenant-id", b"tenant-A"), (b"x-org-read-only", b"true")]
    )
    assert app_called is False
    assert mcp_server._is_org_read_only() is False


# ---------------------------------------------------------------------------
# S2 — CAURA_API_KEY shared gate on /mcp
#
# The middleware never read the shared-key setting at all. ``/mcp`` is a
# separate ASGI mount, so REST's Path 2 does not cover it, and the docs plus
# ``app.py``'s production boot guard both treat CAURA_API_KEY as a sufficient
# perimeter on the strength of that path making header-trust UNREACHABLE.
#
# Three consequences, all pinned below:
#   1. keyless caller + X-Tenant-ID  → was believed verbatim (cross-tenant)
#   2. keyless caller + standalone   → was handed the standalone tenant
#   3. CORRECT key, no standalone    → was refused (_UNAUTH), so the
#      documented shared-gate client could not use MCP at all
#
# Note these tests set ``is_standalone`` explicitly. The suite runs with
# IS_STANDALONE=true, which silently decides several of these branches.
# ---------------------------------------------------------------------------


async def test_shared_key_gate_refuses_a_keyless_tenant_header(monkeypatch):
    """THE CROSS-TENANT HOLE. Keyless caller naming a tenant must be refused.

    With CAURA_API_KEY set and no gateway secret — the documented
    network-exposed OSS shape, blessed by app.py's boot guard — the old
    middleware honored X-Tenant-ID verbatim, so anyone who could reach /mcp
    became any tenant they named. Fails without the fix: the request reached
    the app with tenant ``victim`` resolved.
    """
    monkeypatch.setattr(settings, _SHARED_KEY_FIELD, "sh4red")
    monkeypatch.setattr(settings, "gateway_shared_secret", None)
    monkeypatch.setattr(settings, "is_standalone", False)

    app_called, sends = await _call_middleware([(b"x-tenant-id", b"victim")])

    assert app_called is False, "request reached the app without the shared key"
    start = next(m for m in sends if m["type"] == "http.response.start")
    assert start["status"] == 401
    assert mcp_server._get_tenant() != "victim"


async def test_shared_key_gate_refuses_a_keyless_caller_in_standalone(monkeypatch):
    """Keyless caller must not be handed the standalone tenant.

    ``elif settings.is_standalone`` used to fire before any key comparison, so
    a standalone deployment that set CAURA_API_KEY precisely to expose itself
    to a network granted every unauthenticated caller full read/write/delete
    of the standalone tenant.
    """
    monkeypatch.setattr(settings, _SHARED_KEY_FIELD, "sh4red")
    monkeypatch.setattr(settings, "gateway_shared_secret", None)
    monkeypatch.setattr(settings, "is_standalone", True)

    app_called, sends = await _call_middleware([])

    assert app_called is False, "keyless caller reached the app in standalone"
    start = next(m for m in sends if m["type"] == "http.response.start")
    assert start["status"] == 401


async def test_shared_key_gate_refuses_a_wrong_key(monkeypatch):
    monkeypatch.setattr(settings, _SHARED_KEY_FIELD, "sh4red")
    monkeypatch.setattr(settings, "gateway_shared_secret", None)
    monkeypatch.setattr(settings, "is_standalone", True)

    app_called, sends = await _call_middleware([(b"x-api-key", b"wrong")])

    assert app_called is False
    start = next(m for m in sends if m["type"] == "http.response.start")
    assert start["status"] == 401


async def test_shared_key_gate_accepts_the_correct_key_with_tenant_header(monkeypatch):
    """GUARD, NOT EVIDENCE — passes before and after the fix.

    The documented shared-gate client (X-API-Key + X-Tenant-ID) reached the
    app before this change too, but by falling through to HEADER-TRUST: the
    key was never compared, and an attacker omitting it got the same result.
    So this pins that the accept path still works, not that the gate exists.
    The refusal tests above are the evidence.

    Worth recording because the finding overstated this half. H-01 claimed a
    correct key "falls to _UNAUTH" in shared-gate mode; that is true only for
    a caller sending NO X-Tenant-ID, and such a caller still resolves no
    tenant after the fix — there is nothing to resolve. The documented client
    sends both headers and always worked. The security half of the finding is
    the real defect.
    """
    monkeypatch.setattr(settings, _SHARED_KEY_FIELD, "sh4red")
    monkeypatch.setattr(settings, "gateway_shared_secret", None)
    monkeypatch.setattr(settings, "is_standalone", False)

    app_called, sends = await _call_middleware(
        [(b"x-api-key", b"sh4red"), (b"x-tenant-id", b"tenant-A")]
    )

    assert app_called is True
    assert not any(m["type"] == "http.response.start" for m in sends)
    assert mcp_server._get_tenant() == "tenant-A"


async def test_shared_key_accepted_as_a_bearer_token(monkeypatch):
    """GUARD, NOT EVIDENCE — passes before and after, same reason as above.

    Pins that the Bearer form of the key is accepted, since the install docs
    show both shapes and the gate reads them through one code path.
    """
    monkeypatch.setattr(settings, _SHARED_KEY_FIELD, "sh4red")
    monkeypatch.setattr(settings, "gateway_shared_secret", None)
    monkeypatch.setattr(settings, "is_standalone", False)

    app_called, _ = await _call_middleware(
        [(b"authorization", b"Bearer sh4red"), (b"x-tenant-id", b"tenant-A")]
    )

    assert app_called is True
    assert mcp_server._get_tenant() == "tenant-A"


async def test_shared_key_bearer_form_is_actually_verified(monkeypatch):
    """The Bearer path must be GATED, not just accepted.

    Distinguishes the guard above from real coverage: a wrong Bearer token
    with a tenant header used to sail through on header-trust. Fails without
    the fix.
    """
    monkeypatch.setattr(settings, _SHARED_KEY_FIELD, "sh4red")
    monkeypatch.setattr(settings, "gateway_shared_secret", None)
    monkeypatch.setattr(settings, "is_standalone", False)

    app_called, sends = await _call_middleware(
        [(b"authorization", b"Bearer wrong"), (b"x-tenant-id", b"tenant-A")]
    )

    assert app_called is False
    start = next(m for m in sends if m["type"] == "http.response.start")
    assert start["status"] == 401


async def test_shared_key_holder_cannot_self_assert_an_agent_identity(monkeypatch):
    """A tenant-wide key must not pick an agent identity.

    The shared key proves the caller may reach this deployment, not WHICH
    agent it is. ``X-Agent-ID`` is the authorization principal for scope checks
    and (since #1259) the delete trust gate, so honoring it here would let any
    shared-key holder borrow any agent's scope.

    This is the trap in the fix: ``via_gateway`` was derived downstream as
    ``bool(tenant_header)``, which would have silently re-honored the header
    for exactly this caller. Fails if that derivation comes back.
    """
    monkeypatch.setattr(settings, _SHARED_KEY_FIELD, "sh4red")
    monkeypatch.setattr(settings, "gateway_shared_secret", None)
    monkeypatch.setattr(settings, "is_standalone", False)

    app_called, _ = await _call_middleware(
        [
            (b"x-api-key", b"sh4red"),
            (b"x-tenant-id", b"tenant-A"),
            (b"x-agent-id", b"privileged-agent"),
            (b"x-capabilities", b"read,write"),
            (b"x-readable-tenant-ids", b"tenant-B,tenant-C"),
        ]
    )

    assert app_called is True
    assert mcp_server._get_tenant() == "tenant-A"
    assert mcp_server._get_agent_id() is None, "shared key self-asserted an agent id"
    assert mcp_server._get_scopes() is None
    assert mcp_server._get_readable_tenants() == []


async def test_shared_key_unset_leaves_every_path_unchanged(monkeypatch):
    """Scope guard: with CAURA_API_KEY unset nothing about this middleware moves.

    That is the entire existing deployment surface — enterprise (gateway
    secret) and plain standalone both leave the key unset — so this pins that
    the new gate is inert for them rather than a behaviour change everyone
    absorbs.
    """
    monkeypatch.setattr(settings, _SHARED_KEY_FIELD, None)
    monkeypatch.setattr(settings, "gateway_shared_secret", None)
    monkeypatch.setattr(settings, "is_standalone", True)

    app_called, sends = await _call_middleware([(b"x-tenant-id", b"tenant-A")])

    assert app_called is True
    assert not any(m["type"] == "http.response.start" for m in sends)
    assert mcp_server._get_tenant() == "tenant-A"


async def test_admin_key_still_works_when_the_shared_gate_is_configured(monkeypatch):
    """Admin is Path 1 and stays ahead of the gate, as in REST.

    Without this ordering an operator holding the admin key would be refused
    by the shared-gate comparison on a deployment that configures both.
    """
    monkeypatch.setattr(settings, _SHARED_KEY_FIELD, "sh4red")
    monkeypatch.setattr(settings, "gateway_shared_secret", None)
    monkeypatch.setattr(settings, "is_standalone", False)
    monkeypatch.setattr(mcp_server, "get_admin_key", lambda: "adm1n")

    app_called, _ = await _call_middleware([(b"x-api-key", b"adm1n")])

    assert app_called is True
    assert mcp_server._get_tenant() == mcp_server._ADMIN

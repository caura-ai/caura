"""E2E ``/api/v1/whoami`` — identity probe for SDK bootstrap debugging."""

from __future__ import annotations


async def test_whoami_with_gateway_headers(client):
    # Gateway-routed path: X-Tenant-ID (+ optional X-Agent-ID) injected by
    # auth_validate → returned verbatim with auth_source=gateway-header.
    resp = await client.get(
        "/api/v1/whoami",
        headers={"X-Tenant-ID": "probe-tenant", "X-Agent-ID": "probe-agent"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["tenant_id"] == "probe-tenant"
    assert data["agent_id"] == "probe-agent"
    assert data["auth_source"] == "gateway-header"
    assert data["via_gateway"] is True


async def test_whoami_with_tenant_only(client):
    # mc_ tenant-key path: gateway sets X-Tenant-ID but no X-Agent-ID.
    resp = await client.get(
        "/api/v1/whoami",
        headers={"X-Tenant-ID": "probe-tenant"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["tenant_id"] == "probe-tenant"
    assert data["agent_id"] is None
    assert data["auth_source"] == "gateway-header"


async def test_whoami_standalone_or_anonymous(client):
    # No gateway headers — tests run in standalone (auto-resolves) or
    # anonymous mode. Either way the endpoint must return 200 and a
    # structured envelope, never 401: it's a debug probe.
    resp = await client.get("/api/v1/whoami")
    assert resp.status_code == 200
    data = resp.json()
    assert data["via_gateway"] is False
    assert data["auth_source"] in {"standalone", "anonymous"}


# --- key_kind ---------------------------------------------------------------
#
# The gateway already resolves credential provenance and sends it as
# ``x-caura-credential-kind`` (``auth.py`` and the MCP middleware both read it).
# Surfacing it saves a caller guessing why an install-scoped key behaves
# differently from a plain agent key.
#
# ECHO ONLY, and that is load-bearing rather than lazy: ``/whoami`` has no auth
# dependency, so it is safe precisely because it looks nothing up. The gateway
# perimeter check added below narrows who may claim a gateway identity, but it
# does not lift this: with no shared secret configured (OSS self-hosted) the
# header path is trusted by design, so a field resolving caller-supplied ids
# against storage would still let anyone read another tenant's attributes.
# ``trust_level`` therefore remains NOT here — see #1202.


async def test_whoami_reports_key_kind_from_the_gateway(client):
    resp = await client.get(
        "/api/v1/whoami",
        headers={
            "X-Tenant-ID": "probe-tenant",
            "X-Agent-ID": "probe-agent",
            "X-Caura-Credential-Kind": "Install_Credential",
        },
    )
    assert resp.status_code == 200, resp.text
    # Normalised, matching how ``auth.py`` compares it.
    assert resp.json()["key_kind"] == "install_credential"


async def test_whoami_key_kind_is_none_when_the_gateway_sends_none(client):
    resp = await client.get("/api/v1/whoami", headers={"X-Tenant-ID": "probe-tenant"})
    assert resp.status_code == 200
    assert resp.json()["key_kind"] is None


async def test_whoami_key_kind_present_on_every_branch(client):
    """Stable shape: a caller reads the key without branching on auth_source."""
    resp = await client.get("/api/v1/whoami")
    assert resp.status_code == 200
    assert "key_kind" in resp.json()


async def test_whoami_still_looks_nothing_up(client):
    """An unknown tenant/agent is echoed, not resolved — no storage, no 404.

    Pins the property that makes the missing perimeter check survivable. If
    someone adds a lookup here, this stops being true and the endpoint becomes
    a cross-tenant probe.
    """
    resp = await client.get(
        "/api/v1/whoami",
        headers={
            "X-Tenant-ID": "tenant-that-does-not-exist",
            "X-Agent-ID": "agent-that-does-not-exist",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["tenant_id"] == "tenant-that-does-not-exist"
    assert data["agent_id"] == "agent-that-does-not-exist"


# --- gateway perimeter ------------------------------------------------------
#
# ``via_gateway`` is not an echo like the fields beside it. ``tenant_id`` /
# ``capabilities`` / ``key_kind`` report what the caller sent; ``via_gateway``
# is core-api's own claim about how the request arrived. Returning True on the
# strength of a header the caller set themselves made the probe assert a
# provenance it never verified — on the endpoint whose stated job is telling an
# integrator how their request actually resolves.
#
# The check mirrors ``MCPAuthMiddleware``: required only when a shared secret
# is configured, so OSS self-hosted deployments (and this suite's other tests)
# are unaffected.


async def test_whoami_does_not_claim_gateway_identity_without_the_secret(
    client, monkeypatch
):
    """The spoof: identity headers with no secret must not report a gateway."""
    from core_api.config import settings

    monkeypatch.setattr(settings, "gateway_shared_secret", "s3cret")
    resp = await client.get(
        "/api/v1/whoami",
        headers={"X-Tenant-ID": "victim-tenant", "X-Agent-ID": "victim-agent"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["via_gateway"] is False
    assert data["auth_source"] != "gateway-header"
    # The unverified identity must not be reflected back either — echoing it
    # under auth_source=anonymous would still tell a caller its spoof "took".
    assert data["tenant_id"] != "victim-tenant"
    assert data["agent_id"] is None


async def test_whoami_does_not_claim_gateway_identity_with_a_wrong_secret(
    client, monkeypatch
):
    from core_api.config import settings

    monkeypatch.setattr(settings, "gateway_shared_secret", "s3cret")
    resp = await client.get(
        "/api/v1/whoami",
        headers={"X-Tenant-ID": "victim-tenant", "X-Gateway-Secret": "wrong"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["via_gateway"] is False


async def test_whoami_honors_the_gateway_identity_with_the_correct_secret(
    client, monkeypatch
):
    """The real gateway path keeps working — this is a narrowing, not a block."""
    from core_api.config import settings

    monkeypatch.setattr(settings, "gateway_shared_secret", "s3cret")
    resp = await client.get(
        "/api/v1/whoami",
        headers={
            "X-Tenant-ID": "real-tenant",
            "X-Agent-ID": "real-agent",
            "X-Gateway-Secret": "s3cret",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["via_gateway"] is True
    assert data["auth_source"] == "gateway-header"
    assert data["tenant_id"] == "real-tenant"


async def test_whoami_trusts_headers_when_no_secret_is_configured(client, monkeypatch):
    """OSS self-hosted is untouched: no secret configured, no new check.

    Pins the scope of this change. Without it a self-hosted deployment that
    never sets a gateway secret would lose its identity probe entirely.
    """
    from core_api.config import settings

    monkeypatch.setattr(settings, "gateway_shared_secret", None)
    resp = await client.get(
        "/api/v1/whoami",
        headers={"X-Tenant-ID": "solo-tenant", "X-Agent-ID": "solo-agent"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["via_gateway"] is True
    assert data["tenant_id"] == "solo-tenant"
